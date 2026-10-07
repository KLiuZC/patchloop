import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from .models import TaskRecord


class TaskStore:
    """SQLite task records, with a short-lived connection per operation."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"unsupported task database schema version: {version}")
            connection.execute("PRAGMA journal_mode=WAL")
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
            connection.execute("PRAGMA user_version=1")

    def create(self, record: TaskRecord) -> None:
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO tasks (
                    task_id, repo_path, workdir, base_sha, goal, verification_command,
                    status, created_at, updated_at
                ) VALUES (
                    :task_id, :repo_path, :workdir, :base_sha, :goal, :verification_command,
                    :status, :created_at, :updated_at
                )
            """, record.model_dump(mode="json"))

    def get(self, task_id: UUID) -> TaskRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        return TaskRecord.model_validate(dict(row)) if row is not None else None
