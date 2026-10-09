"""Create executable requests from frozen Issue and repository preparations."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import IssueTaskResult, IssueTaskSubmission, TaskRecord, TaskSource
from .repositories import RepositoryPreparationService
from .repository_git import RepositoryCheckout
from .source_store import IssueSourceStore
from .store import TaskStore


class IssueTaskConflict(ValueError):
    pass


def _check_workdir(root: Path, workdir: str) -> None:
    target = root
    if workdir != ".":
        for part in workdir.split("/"):
            target = target / part
            if target.is_symlink():
                raise IssueTaskConflict("Task workdir must not contain a symbolic link")
    if not target.is_dir() or target.resolve() != target or not target.is_relative_to(root):
        raise IssueTaskConflict("Task workdir is missing or outside the prepared repository")
    # Git metadata is never an execution directory.
    if any(part.casefold() == ".git" for part in target.relative_to(root).parts):
        raise IssueTaskConflict("Task workdir must not be inside Git metadata")


def inspect_issue_task(task: TaskRecord) -> None:
    """Recheck the managed base before the worker initializes a model client."""
    source = task.source
    if source is None:
        return
    if task.base_sha != source.base_sha or task.repo_path != source.repo_path:
        raise IssueTaskConflict("Task source no longer matches its prepared base")
    root = Path(task.repo_path)
    intact, reason = RepositoryCheckout().inspect(root, task.base_sha)
    if not intact:
        raise IssueTaskConflict(reason or "Prepared repository is no longer intact")
    _check_workdir(root, task.workdir)


class IssueTaskService:
    def __init__(self, store: TaskStore, repositories: RepositoryPreparationService):
        self.store = store
        self.repositories = repositories
        self.sources = IssueSourceStore(store)

    def _replay(self, connection, submission: IssueTaskSubmission) -> IssueTaskResult | None:
        row = connection.execute(
            "SELECT * FROM issue_task_requests WHERE request_id = ?", (str(submission.request_id),),
        ).fetchone()
        if row is None:
            return None
        if row["submission_json"] != submission.model_dump_json():
            raise IssueTaskConflict("request_id was already used for a different task configuration")
        saved = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)).fetchone()
        if saved is None:
            raise IssueTaskConflict("Saved Issue task is unavailable")
        task = self.store._decode(saved)
        if task.source is None or str(task.source.preparation_id) != row["preparation_id"]:
            raise IssueTaskConflict("Saved Issue task source is inconsistent")
        return IssueTaskResult(task=task, replayed=True)

    def submit(self, submission: IssueTaskSubmission) -> IssueTaskResult:
        # Replays return the current task state, never enqueue a second attempt.
        with self.store._connection() as connection:
            replay = self._replay(connection, submission)
            if replay is not None:
                return replay
        state = self.repositories.get(submission.preparation_id)
        preparation = state.preparation
        if preparation.status != "ready" or state.checkout_intact is not True:
            raise IssueTaskConflict(state.reason or "Repository preparation is not ready")
        source = self.sources.get(preparation.source_id)
        if source is None:
            raise IssueTaskConflict("Prepared Issue source is unavailable")
        if (source.repository.id != preparation.repository.id
                or source.repository.full_name.casefold() != preparation.repository.full_name.casefold()):
            raise IssueTaskConflict("Issue and prepared repository identities differ")
        root = Path(preparation.repo_path)
        _check_workdir(root, submission.workdir)
        now = datetime.now(timezone.utc)
        task = TaskRecord(
            task_id=uuid4(), repo_path=preparation.repo_path, workdir=submission.workdir,
            base_sha=preparation.base_sha, goal=submission.goal,
            verification_command=submission.verification_command, execution=submission.execution,
            path_policy=submission.path_policy, status="queued", created_at=now, updated_at=now,
            source=TaskSource(preparation_id=preparation.preparation_id, issue=source,
                              repo_path=preparation.repo_path, base_sha=preparation.base_sha,
                              resolved_ref=preparation.resolved_ref, resolved_at=preparation.resolved_at),
        )
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(connection, submission)
            if replay is not None:
                return replay
            # Ready preparations are immutable through this API. Do not perform
            # bounded-but-slow Git inspection while holding the database write
            # lock; the worker checks again before any model invocation.
            latest = self.repositories.store.get(submission.preparation_id)
            if latest != preparation:
                raise IssueTaskConflict("Repository preparation changed during task creation")
            self.store._insert(connection, task)
            connection.execute("""
                INSERT INTO issue_task_requests (request_id, task_id, preparation_id, submission_json)
                VALUES (?, ?, ?, ?)
            """, (str(submission.request_id), str(task.task_id), str(submission.preparation_id),
                  submission.model_dump_json()))
        return IssueTaskResult(task=task, replayed=False)
