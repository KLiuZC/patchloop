"""Repository preparations sharing TaskStore's database and transactions."""

import json
import sqlite3
from uuid import UUID

from .repository_models import RepositoryPreparation, RepositoryPreparationResult
from .store import TaskStore


class PreparationConflict(ValueError):
    """An identifier or state transition conflicts with a saved preparation."""


class PreparationCorrupt(RuntimeError):
    """A preparation failed record or lookup-field validation."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class RepositoryPreparationStore:
    """Idempotent preparation requests with immutable inputs and commit pins."""

    def __init__(self, task_store: TaskStore):
        self.task_store = task_store

    @staticmethod
    def _validate(record: RepositoryPreparation) -> RepositoryPreparation:
        try:
            # Models are mutable and model_copy(update=...) skips validation.
            return RepositoryPreparation.model_validate(record.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise PreparationCorrupt("Repository preparation is invalid model data") from exc

    @staticmethod
    def _validate_state(record: RepositoryPreparation) -> None:
        if record.status == "ready" and any(
            getattr(record, field) is None for field in ("base_sha", "resolved_ref", "resolved_at", "finished_at")
        ):
            raise PreparationCorrupt("A ready repository preparation requires a resolved ref, commit and finished_at")
        if record.status == "failed" and (not record.error.strip() or record.finished_at is None):
            raise PreparationCorrupt("A failed repository preparation requires error and finished_at")

    @classmethod
    def _decode(cls, row: sqlite3.Row) -> RepositoryPreparation:
        try:
            record = RepositoryPreparation.model_validate(
                json.loads(row["record_json"], parse_constant=_reject_json_constant),
            )
        except (TypeError, ValueError) as exc:
            raise PreparationCorrupt("Stored repository preparation is invalid JSON data") from exc
        if any(str(getattr(record, field)) != row[field]
               for field in ("preparation_id", "request_id", "source_id")):
            raise PreparationCorrupt("Stored repository preparation does not match its lookup fields")
        cls._validate_state(record)
        return record

    def get(self, preparation_id: UUID) -> RepositoryPreparation | None:
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repository_preparations WHERE preparation_id = ?", (str(preparation_id),),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def get_for_request(self, request_id: UUID) -> RepositoryPreparation | None:
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repository_preparations WHERE request_id = ?", (str(request_id),),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def create(self, record: RepositoryPreparation) -> RepositoryPreparationResult:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM repository_preparations WHERE request_id = ?", (str(record.request_id),),
            ).fetchone()
            if existing is not None:
                original = self._decode(existing)
                if original.source_id != record.source_id or original.requested_ref != record.requested_ref:
                    raise PreparationConflict("request_id was already used for a different source_id or requested_ref")
                return RepositoryPreparationResult(preparation=original, replayed=True)
            if connection.execute(
                "SELECT 1 FROM repository_preparations WHERE preparation_id = ?", (str(record.preparation_id),),
            ).fetchone() is not None:
                raise PreparationConflict("preparation_id was already used for another request")
            record = self._validate(record)
            if record.status != "preparing":
                raise PreparationConflict("New repository preparations must be preparing")
            connection.execute("""
                INSERT INTO repository_preparations (preparation_id, request_id, source_id, record_json)
                VALUES (?, ?, ?, ?)
            """, (str(record.preparation_id), str(record.request_id), str(record.source_id), record.model_dump_json()))
        return RepositoryPreparationResult(preparation=record, replayed=False)

    def update(self, record: RepositoryPreparation) -> None:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM repository_preparations WHERE preparation_id = ?", (str(record.preparation_id),),
            ).fetchone()
            if row is None:
                raise PreparationConflict("Repository preparation does not exist")
            original = self._decode(row)
            if original.status != "preparing":
                raise PreparationConflict("Repository preparation is no longer preparing")
            record = self._validate(record)
            for field in ("preparation_id", "request_id", "source_id", "requested_ref",
                          "clone_url", "repo_path", "created_at"):
                if getattr(record, field) != getattr(original, field):
                    raise PreparationConflict(f"Repository preparation {field} is immutable")
            if (record.repository.id != original.repository.id
                    or record.repository.full_name.casefold() != original.repository.full_name.casefold()):
                raise PreparationConflict("Repository preparation repository identity is immutable")
            resolution_fields = ("resolved_ref", "base_sha", "resolved_at")
            first_resolution = (all(getattr(original, field) is None for field in resolution_fields)
                                and all(getattr(record, field) is not None for field in resolution_fields))
            if record.repository != original.repository and not first_resolution:
                raise PreparationConflict("Repository preparation repository metadata is immutable outside initial resolution")
            for field in resolution_fields:
                pinned = getattr(original, field)
                if pinned is not None and getattr(record, field) != pinned:
                    raise PreparationConflict(f"Repository preparation {field} is already pinned")
            self._validate_state(record)
            connection.execute(
                "UPDATE repository_preparations SET record_json = ? WHERE preparation_id = ?",
                (record.model_dump_json(), str(record.preparation_id)),
            )

    def preparing(self) -> list[RepositoryPreparation]:
        with self.task_store._connection() as connection:
            # Decode every row before filtering: damaged records must not silently
            # disappear from recovery because their JSON status is missing/invalid.
            records = [self._decode(row) for row in connection.execute("SELECT * FROM repository_preparations")]
        return [record for record in records if record.status == "preparing"]
