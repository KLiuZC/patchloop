"""Publication intent and recovery checkpoints, separate from repair evidence."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from .github_delivery import GitHubDeliveryConflict
from .github_delivery_models import GitHubPublicationRecord
from .store import TaskStore


def plan_digest(plan) -> str:
    content = json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


class GitHubPublicationStore:
    def __init__(self, tasks: TaskStore):
        self.tasks = tasks

    @staticmethod
    def _decode(row) -> GitHubPublicationRecord | None:
        if row is None:
            return None
        try:
            value = GitHubPublicationRecord.model_validate_json(row["record_json"])
            if (any(str(getattr(value, key)) != row[key] for key in ("publication_id", "task_id", "request_id"))
                    or str(value.plan.delivery_id) != row["delivery_id"]
                    or value.plan.task_id != value.task_id or value.submission.request_id != value.request_id
                    or value.submission.plan_sha256 != value.plan_sha256 or plan_digest(value.plan) != value.plan_sha256
                    or (value.status == "completed" and (value.pull_request is None or value.phase != "completed"))):
                raise ValueError()
            return value
        except ValueError as exc:
            raise GitHubDeliveryConflict("saved publication record is inconsistent") from exc

    def find(self, task_id: UUID, *, request_id: UUID | None = None, delivery_id: UUID | None = None):
        key, value = ("request_id", request_id) if request_id is not None else ("delivery_id", delivery_id)
        with self.tasks._connection() as connection:
            return self._decode(connection.execute(
                f"SELECT * FROM github_publications WHERE task_id=? AND {key}=?", (str(task_id), str(value)),
            ).fetchone())

    def list(self, task_id: UUID):
        with self.tasks._connection() as connection:
            self.tasks._review_context(connection, task_id)
            return [self._decode(row) for row in connection.execute(
                "SELECT * FROM github_publications WHERE task_id=? ORDER BY rowid", (str(task_id),),
            )]

    def create(self, value: GitHubPublicationRecord):
        with self.tasks._connection() as connection:
            connection.execute("INSERT INTO github_publications VALUES (?, ?, ?, ?, ?)",
                               (str(value.publication_id), str(value.task_id), str(value.request_id),
                                str(value.plan.delivery_id), value.model_dump_json()))

    def save(self, value: GitHubPublicationRecord):
        value.updated_at = datetime.now(timezone.utc)
        with self.tasks._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            original = self._decode(connection.execute(
                "SELECT * FROM github_publications WHERE publication_id=?", (str(value.publication_id),),
            ).fetchone())
            if original is None or original.status == "completed":
                raise GitHubDeliveryConflict("publication is missing or already completed")
            for key in ("publication_id", "task_id", "request_id", "submission", "plan_sha256", "plan", "created_at"):
                if getattr(value, key) != getattr(original, key):
                    raise GitHubDeliveryConflict("publication intent cannot be changed during recovery")
            connection.execute("UPDATE github_publications SET record_json=? WHERE publication_id=?",
                               (value.model_dump_json(), str(value.publication_id)))
