import logging
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .models import TaskRecord, TaskSubmission
from .store import TaskStore


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
        description="Submit and query saved repair requests. Tasks remain queued until worker support is added.",
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
        )
        store.create(task)
        response.headers["Location"] = f"/tasks/{task.task_id}"
        return task

    @app.get("/tasks/{task_id}", response_model=TaskRecord)
    def get_task(task_id: UUID) -> TaskRecord:
        task = store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    return app
