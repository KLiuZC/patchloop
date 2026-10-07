import logging
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .models import TaskPage, TaskRecord, TaskStatus, TaskSubmission
from .store import InvalidTaskCursor, TaskStore


logger = logging.getLogger(__name__)


def _repository_base(submission: TaskSubmission, server_directory: Path) -> tuple[Path, str, str]:
    try:
        target = Path(submission.repo_path).expanduser()
        if not target.is_absolute():
            target = server_directory / target
        target = target.resolve()
        if not target.is_dir():
            raise ValueError("repo_path must be an existing directory on this server")
    except (OSError, RuntimeError) as exc:
        raise ValueError("repo_path is invalid or inaccessible") from exc

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(target), *args], capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("could not inspect repository with git") from exc
        if result.returncode != 0:
            raise ValueError("repo_path must be in a Git working tree and base_sha must resolve to a commit")
        return result.stdout.strip()

    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    # Freeze a full commit id now; a queued task must not follow a moving branch.
    revision = (submission.base_sha or "HEAD") + "^{commit}"
    base = git("rev-parse", "--verify", "--end-of-options", revision)
    workdir = target.relative_to(root).as_posix()
    return root, workdir, base


def create_app(db_path: Path = Path("data/tasks.sqlite3")) -> FastAPI:
    """Create the task API. No worker, model client or Docker container is started."""
    store = TaskStore(Path(db_path))
    server_directory = Path.cwd().resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        yield

    app = FastAPI(
        title="Patchloop Task API",
        version="0.1.0",
        description="Submit repair requests, query worker progress and download repair artifacts.",
        lifespan=lifespan,
    )
    app.state.task_store = store

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        logger.error("Task storage operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Task storage is unavailable; try again later"})

    @app.post("/tasks", response_model=TaskRecord, status_code=201)
    def submit_task(submission: TaskSubmission, response: Response) -> TaskRecord:
        try:
            root, workdir, base = _repository_base(submission, server_directory)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        now = datetime.now(timezone.utc)
        task = TaskRecord(
            task_id=uuid4(), repo_path=str(root), workdir=workdir, base_sha=base,
            goal=submission.goal, verification_command=submission.verification_command,
            status="queued", created_at=now, updated_at=now,
            execution=submission.execution,
        )
        store.create(task)
        response.headers["Location"] = f"/tasks/{task.task_id}"
        return task

    @app.get("/tasks", response_model=TaskPage)
    def list_tasks(
        limit: int = Query(default=20, ge=1, le=100),
        status: TaskStatus | None = None,
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
    ) -> TaskPage:
        """List newest tasks first; pass next_cursor to continue with the same filter."""
        try:
            return store.list_tasks(limit=limit, status=status, cursor=cursor)
        except InvalidTaskCursor as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}", response_model=TaskRecord)
    def get_task(task_id: UUID) -> TaskRecord:
        task = store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.get("/tasks/{task_id}/artifacts/{name}", response_class=FileResponse)
    def get_artifact(task_id: UUID, name: str):
        task = get_task(task_id)
        allowed = {
            "task.json": "task.json", "summary.json": "summary.json", "validation.json": "validation.json",
            "patch.diff": "patch.diff", "events.jsonl": "events.jsonl", "worker-error.json": "worker-error.json",
            "runtime.json": "runtime.json", "trajectory.jsonl": f"trajectory/{task_id}.jsonl",
        }
        if name not in allowed or task.artifacts_dir is None:
            raise HTTPException(status_code=404, detail="Artifact not available")
        root = Path(task.artifacts_dir).resolve()
        path = (root / allowed[name]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not available")
        return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)

    return app
