from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


TaskStatus = Literal["queued", "running", "completed", "interrupted", "error"]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=20000)]


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    goal: NonEmptyText
    verification_command: NonEmptyText
    base_sha: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None

    @field_validator("repo_path", "goal", "verification_command", "base_sha")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("must contain non-whitespace characters")
            if "\x00" in value:
                raise ValueError("null bytes are not allowed")
        return value


class TaskRecord(BaseModel):
    """A persisted request; completion and repair validation are separate concepts."""

    task_id: UUID
    repo_path: str
    workdir: str
    base_sha: str
    goal: str
    verification_command: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
