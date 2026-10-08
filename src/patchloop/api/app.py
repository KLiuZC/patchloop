import logging
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .models import (ReviewHistory, ReviewResponse, ReviewState, ReviewSubmission,
                     DeliveryResponse, DeliveryState, DeliverySubmission,
                     TaskPage, TaskRecord, TaskStatus, TaskSubmission)
from .delivery import DeliveryService
from .review import ReviewConflict, ReviewNotFound
from .store import InvalidTaskCursor, TaskStore
from .github import GitHubIssueClient, GitHubReadError
from .source_models import GitHubIssueSnapshot, IssueImport, IssueImportResult, IssueSource
from .source_store import IssueSourceStore, SourceConflict, SourceCorrupt


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


def create_app(db_path: Path = Path("data/tasks.sqlite3"), *, deliveries_root: Path | None = None,
               github_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """Create the task API. No worker, model client or Docker container is started."""
    store = TaskStore(Path(db_path))
    delivery_service = DeliveryService(store, deliveries_root)
    source_store = IssueSourceStore(store)
    github = GitHubIssueClient(transport=github_transport)
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
    app.state.delivery_service = delivery_service
    app.state.issue_source_store = source_store
    app.state.github_client = github

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        logger.error("Task storage operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Task storage is unavailable; try again later"})

    @app.exception_handler(GitHubReadError)
    async def github_error(request: Request, exc: GitHubReadError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)}, headers=headers)

    @app.exception_handler(SourceConflict)
    async def source_conflict(request: Request, exc: SourceConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SourceCorrupt)
    async def source_corrupt(request: Request, exc: SourceCorrupt):
        return JSONResponse(status_code=500, content={"detail": "Saved Issue source is inconsistent; existing snapshots were not changed"})

    @app.post("/sources/github/issues", response_model=IssueImportResult, status_code=201)
    def import_issue(submission: IssueImport, response: Response) -> IssueImportResult:
        """Read a public Issue into an immutable snapshot, without creating a task."""
        existing = source_store.get_for_request(submission.request_id)
        if existing is not None:
            if existing.requested_url != submission.url:
                raise SourceConflict("request_id was already used for a different Issue URL")
            result = IssueImportResult(source=existing, replayed=True)
        else:
            source, snapshot = github.fetch(submission)
            result = source_store.save(source, snapshot)
        if result.replayed:
            response.status_code = 200
        response.headers["Location"] = f"/sources/github/issues/{result.source.source_id}"
        return result

    @app.get("/sources/github/issues/{source_id}", response_model=IssueSource)
    def get_issue_source(source_id: UUID) -> IssueSource:
        source = source_store.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Issue source not found")
        return source

    @app.get("/sources/github/issues/{source_id}/snapshot", response_model=GitHubIssueSnapshot)
    def get_issue_snapshot(source_id: UUID) -> GitHubIssueSnapshot:
        snapshot = source_store.get_snapshot(source_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Issue source not found")
        return snapshot

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
            path_policy=submission.path_policy,
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

    @app.exception_handler(ReviewNotFound)
    async def review_not_found(request: Request, exc: ReviewNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ReviewConflict)
    async def review_conflict(request: Request, exc: ReviewConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/tasks/{task_id}/review", response_model=ReviewState)
    def get_review(task_id: UUID) -> ReviewState:
        """Read the effective review and the exact evidence required for a decision."""
        return store.get_review(task_id)

    @app.get("/tasks/{task_id}/reviews", response_model=ReviewHistory)
    def list_reviews(task_id: UUID) -> ReviewHistory:
        """Read immutable decisions in revision order, independently of repair results."""
        return store.list_reviews(task_id)

    @app.post("/tasks/{task_id}/reviews", response_model=ReviewResponse, status_code=201)
    def submit_review(task_id: UUID, submission: ReviewSubmission, response: Response) -> ReviewResponse:
        result = store.submit_review(task_id, submission)
        if result.replayed:
            response.status_code = 200
        return result

    @app.get("/tasks/{task_id}/delivery", response_model=DeliveryState)
    def get_delivery(task_id: UUID) -> DeliveryState:
        return delivery_service.inspect(task_id)

    @app.post("/tasks/{task_id}/delivery", response_model=DeliveryResponse, status_code=201)
    def deliver_patch(task_id: UUID, submission: DeliverySubmission, response: Response) -> DeliveryResponse:
        result = delivery_service.deliver(task_id, submission)
        if result.replayed:
            response.status_code = 200
        return result

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
