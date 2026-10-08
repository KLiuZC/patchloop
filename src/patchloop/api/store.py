import base64
import binascii
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .models import (ReviewHistory, ReviewRecord, ReviewResponse, ReviewState, ReviewSubmission,
                     TaskCursor, TaskPage, TaskRecord, TaskStatus, TaskSummary, utc_timestamp)
from .review import ReviewConflict, ReviewNotFound, review_state


class InvalidTaskCursor(ValueError):
    pass


class TaskStore:
    """SQLite task records, with a short-lived connection per operation."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8):
                raise RuntimeError(f"unsupported task database schema version: {version}")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    workdir TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    verification_command TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'error')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            if version < 2:
                for definition in (
                    "execution_json TEXT", "result_json TEXT", "artifacts_dir TEXT",
                    "started_at TEXT", "finished_at TEXT", "error TEXT NOT NULL DEFAULT ''",
                    "validation_status TEXT NOT NULL DEFAULT 'not_run'",
                ):
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {definition}")
            if version < 3:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
                if "cleanup_pending" not in columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE tasks SET cleanup_pending = 1 WHERE status = 'running' OR artifacts_dir IS NOT NULL")
                connection.execute("PRAGMA user_version=3")
            if version < 4:
                # A fixed UTC representation makes text ordering match chronological
                # ordering, including timestamps with zero fractional seconds.
                rows = connection.execute("SELECT task_id, created_at FROM tasks").fetchall()
                connection.executemany("UPDATE tasks SET created_at = ? WHERE task_id = ?", [
                    (utc_timestamp(datetime.fromisoformat(row["created_at"])), row["task_id"]) for row in rows
                ])
                connection.execute("PRAGMA user_version=4")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_created_order ON tasks(created_at DESC, task_id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_status_created_order ON tasks(status, created_at DESC, task_id DESC)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    request_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected')),
                    comment TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, revision),
                    UNIQUE(task_id, request_id)
                )
            """)
            if version < 6:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
                if "path_policy_json" not in columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN path_policy_json TEXT")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    request_id TEXT NOT NULL,
                    review_id TEXT NOT NULL REFERENCES task_reviews(review_id),
                    record_json TEXT NOT NULL,
                    UNIQUE(task_id, review_id),
                    UNIQUE(task_id, request_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS github_issue_sources (
                    source_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    requested_url TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS repository_preparations (
                    preparation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL REFERENCES github_issue_sources(source_id),
                    record_json TEXT NOT NULL
                )
            """)
            connection.execute("PRAGMA user_version=8")

    def create(self, record: TaskRecord) -> None:
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO tasks (
                    task_id, repo_path, workdir, base_sha, goal, verification_command,
                    status, created_at, updated_at, execution_json, path_policy_json
                ) VALUES (
                    :task_id, :repo_path, :workdir, :base_sha, :goal, :verification_command,
                    :status, :created_at, :updated_at, :execution_json, :path_policy_json
                )
            """, {**record.model_dump(mode="json"), "created_at": utc_timestamp(record.created_at),
                  "execution_json": record.execution.model_dump_json() if record.execution else None,
                  "path_policy_json": record.path_policy.model_dump_json() if record.path_policy else None})

    def get(self, task_id: UUID) -> TaskRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        return self._decode(row) if row is not None else None

    def list_tasks(self, *, limit: int = 20, status: TaskStatus | None = None, cursor: str | None = None) -> TaskPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        where = []
        parameters = []
        if status is not None:
            where.append("status = ?")
            parameters.append(status)
        if cursor is not None:
            try:
                if not 1 <= len(cursor) <= 512:
                    raise ValueError("invalid cursor length")
                decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
                position = TaskCursor.model_validate_json(decoded)
            except (ValueError, binascii.Error) as exc:
                raise InvalidTaskCursor("invalid task cursor") from exc
            if position.status != status:
                raise InvalidTaskCursor("cursor belongs to a different status filter; restart from the first page")
            where.append("(created_at < ? OR (created_at = ? AND task_id < ?))")
            parameters.extend((position.created_at, position.created_at, str(position.task_id)))
        sql = """
            SELECT task_id, repo_path, workdir, goal, status, validation_status,
                created_at, updated_at, started_at, finished_at, error, cleanup_pending
            FROM tasks
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, task_id DESC LIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(sql, (*parameters, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more:
            last = rows[-1]
            position = TaskCursor(created_at=last["created_at"], task_id=last["task_id"], status=status)
            next_cursor = base64.urlsafe_b64encode(position.model_dump_json().encode()).decode().rstrip("=")
        return TaskPage(items=[TaskSummary.model_validate(dict(row)) for row in rows], next_cursor=next_cursor)

    @staticmethod
    def _decode(row) -> TaskRecord:
        value = dict(row)
        policy = value.pop("path_policy_json", None)
        value["path_policy"] = json.loads(policy) if policy is not None else None
        for column, field in (("execution_json", "execution"), ("result_json", "result")):
            raw = value.pop(column)
            value[field] = json.loads(raw) if raw is not None else None
        return TaskRecord.model_validate(value)

    def claim(self, artifacts_root: Path, task_id: UUID | None = None) -> TaskRecord | None:
        """Atomically move one runnable request from queued to running."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM tasks WHERE cleanup_pending = 1 LIMIT 1").fetchone():
                return None
            row = connection.execute("""
                SELECT * FROM tasks WHERE status = 'queued' AND execution_json IS NOT NULL
                AND (? IS NULL OR task_id = ?) ORDER BY created_at, task_id LIMIT 1
            """, (str(task_id) if task_id else None,) * 2).fetchone()
            if row is None:
                return None
            now = datetime.now(timezone.utc).isoformat()
            artifacts_dir = str(artifacts_root.resolve() / row["task_id"])
            connection.execute("""
                UPDATE tasks SET status = 'running', started_at = ?, updated_at = ?, artifacts_dir = ?, cleanup_pending = 1
                WHERE task_id = ? AND status = 'queued'
            """, (now, now, artifacts_dir, row["task_id"]))
            claimed = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)).fetchone()
            return self._decode(claimed)

    def finish(self, task_id: UUID, *, status: str, validation_status: str, error: str = "", result: dict | None = None) -> None:
        if status not in ("completed", "interrupted", "error"):
            raise ValueError("finish requires a terminal status")
        if validation_status not in ("not_run", "passed", "failed", "inconclusive"):
            raise ValueError("invalid validation status")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            changed = connection.execute("""
                UPDATE tasks SET status = ?, validation_status = ?, error = ?, result_json = ?,
                    finished_at = ?, updated_at = ? WHERE task_id = ? AND status = 'running'
            """, (status, validation_status, error, json.dumps(result, ensure_ascii=False) if result else None,
                  now, now, str(task_id)))
            if changed.rowcount != 1:
                raise RuntimeError("task is no longer running; result was not written")

    def running(self) -> list[TaskRecord]:
        with self._connection() as connection:
            return [self._decode(row) for row in connection.execute("SELECT * FROM tasks WHERE status = 'running'")]

    def pending_cleanup(self) -> list[TaskRecord]:
        with self._connection() as connection:
            return [self._decode(row) for row in connection.execute("SELECT * FROM tasks WHERE cleanup_pending = 1")]

    def mark_clean(self, task_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE tasks SET cleanup_pending = 0 WHERE task_id = ?", (str(task_id),))

    @staticmethod
    def _decode_review(row) -> ReviewRecord:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        return ReviewRecord.model_validate(value)

    def _review_context(self, connection, task_id: UUID) -> tuple[TaskRecord, ReviewRecord | None]:
        row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        if row is None:
            raise ReviewNotFound("Task not found")
        latest = connection.execute(
            "SELECT * FROM task_reviews WHERE task_id = ? ORDER BY revision DESC LIMIT 1", (str(task_id),),
        ).fetchone()
        return self._decode(row), self._decode_review(latest) if latest else None

    def get_review(self, task_id: UUID) -> ReviewState:
        with self._connection() as connection:
            connection.execute("BEGIN")
            task, latest = self._review_context(connection, task_id)
            return review_state(task, latest)

    def list_reviews(self, task_id: UUID) -> ReviewHistory:
        with self._connection() as connection:
            connection.execute("BEGIN")
            self._review_context(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_reviews WHERE task_id = ? ORDER BY revision", (str(task_id),),
            ).fetchall()
            return ReviewHistory(task_id=task_id, items=[self._decode_review(row) for row in rows])

    def submit_review(self, task_id: UUID, submission: ReviewSubmission) -> ReviewResponse:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task, latest = self._review_context(connection, task_id)
            existing = connection.execute(
                "SELECT * FROM task_reviews WHERE task_id = ? AND request_id = ?",
                (str(task_id), str(submission.request_id)),
            ).fetchone()
            if existing:
                record = self._decode_review(existing)
                if (record.revision != submission.expected_revision + 1 or record.decision != submission.decision
                        or record.comment != submission.comment or record.evidence != submission.evidence):
                    raise ReviewConflict("request_id was already used for a different review")
                return ReviewResponse(review=record, current=review_state(task, latest), replayed=True)

            state = review_state(task, latest)
            if submission.expected_revision != state.revision:
                raise ReviewConflict("review changed; reload the current review before submitting")
            if state.evidence is None or submission.evidence != state.evidence:
                raise ReviewConflict("repair evidence changed or is unavailable; reload before reviewing")
            if not (state.can_accept if submission.decision == "accepted" else state.can_reject):
                raise ReviewConflict(state.reason or "review is not allowed for this task")
            # Recheck immediately before saving; later delivery must check again.
            verified = review_state(task, latest)
            if verified.evidence != state.evidence:
                raise ReviewConflict("repair evidence changed while submitting the review")
            record = ReviewRecord(
                review_id=uuid4(), task_id=task_id, revision=state.revision + 1,
                request_id=submission.request_id, decision=submission.decision, comment=submission.comment,
                evidence=state.evidence, created_at=datetime.now(timezone.utc),
            )
            connection.execute("""
                INSERT INTO task_reviews
                    (review_id, task_id, revision, request_id, decision, comment, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(record.review_id), str(task_id), record.revision, str(record.request_id), record.decision,
                  record.comment, record.evidence.model_dump_json(), utc_timestamp(record.created_at)))
            return ReviewResponse(review=record, current=review_state(task, record), replayed=False)
