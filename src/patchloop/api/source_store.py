"""Persist GitHub Issue sources and JSON snapshots, not verbatim HTTP bytes."""

import hashlib
import json
import sqlite3
from uuid import UUID

from .source_models import GitHubIssueSnapshot, IssueImportResult, IssueSource
from .store import TaskStore


class SourceConflict(ValueError):
    """An import identifier already belongs to another source."""


class SourceCorrupt(RuntimeError):
    """A source or its JSON snapshot failed integrity validation."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _snapshot_json(snapshot: GitHubIssueSnapshot) -> str:
    try:
        return json.dumps(
            snapshot.model_dump(mode="python"), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceCorrupt("Issue snapshot is not valid canonical JSON") from exc


def snapshot_digest(snapshot: GitHubIssueSnapshot) -> str:
    """SHA-256 of canonical UTF-8 JSON, independent of HTTP response formatting."""
    try:
        return hashlib.sha256(_snapshot_json(snapshot).encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise SourceCorrupt("Issue snapshot is not valid UTF-8") from exc


class IssueSourceStore:
    """Immutable source records sharing TaskStore's short-lived connections."""

    def __init__(self, task_store: TaskStore):
        self.task_store = task_store

    @staticmethod
    def _validate_snapshot(source: IssueSource, snapshot: GitHubIssueSnapshot) -> None:
        if source.api_version != snapshot.api_version:
            raise SourceCorrupt("Issue source and snapshot API versions differ")
        if source.snapshot_sha256 != snapshot_digest(snapshot):
            raise SourceCorrupt("Issue snapshot SHA-256 does not match its source")

    @classmethod
    def _decode(cls, row: sqlite3.Row) -> tuple[IssueSource, GitHubIssueSnapshot]:
        try:
            source = IssueSource.model_validate(json.loads(row["record_json"], parse_constant=_reject_json_constant))
            snapshot = GitHubIssueSnapshot.model_validate(
                json.loads(row["snapshot_json"], parse_constant=_reject_json_constant),
            )
        except (TypeError, ValueError) as exc:
            raise SourceCorrupt("Stored issue source or snapshot is invalid JSON data") from exc
        if (str(source.source_id) != row["source_id"]
                or str(source.request_id) != row["request_id"]
                or source.requested_url != row["requested_url"]):
            raise SourceCorrupt("Stored issue source does not match its lookup fields")
        cls._validate_snapshot(source, snapshot)
        return source, snapshot

    def get(self, source_id: UUID) -> IssueSource | None:
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM github_issue_sources WHERE source_id = ?", (str(source_id),),
            ).fetchone()
        return self._decode(row)[0] if row is not None else None

    def get_snapshot(self, source_id: UUID) -> GitHubIssueSnapshot | None:
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM github_issue_sources WHERE source_id = ?", (str(source_id),),
            ).fetchone()
        return self._decode(row)[1] if row is not None else None

    def get_for_request(self, request_id: UUID) -> IssueSource | None:
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM github_issue_sources WHERE request_id = ?", (str(request_id),),
            ).fetchone()
        return self._decode(row)[0] if row is not None else None

    def save(self, source: IssueSource, snapshot: GitHubIssueSnapshot) -> IssueImportResult:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM github_issue_sources WHERE request_id = ?", (str(source.request_id),),
            ).fetchone()
            if existing is not None:
                original, _ = self._decode(existing)
                if original.requested_url != source.requested_url:
                    raise SourceConflict("request_id was already used for a different Issue URL")
                return IssueImportResult(source=original, replayed=True)

            self._validate_snapshot(source, snapshot)
            if connection.execute(
                "SELECT 1 FROM github_issue_sources WHERE source_id = ?", (str(source.source_id),),
            ).fetchone() is not None:
                raise SourceConflict("source_id was already used for another import")
            connection.execute("""
                INSERT INTO github_issue_sources (
                    source_id, request_id, requested_url, record_json, snapshot_json
                ) VALUES (?, ?, ?, ?, ?)
            """, (str(source.source_id), str(source.request_id), source.requested_url,
                  source.model_dump_json(), _snapshot_json(snapshot)))
        return IssueImportResult(source=source, replayed=False)
