from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from ..llm import PROVIDERS
from ..repair.contracts import ValidationStatus


TaskStatus = Literal["queued", "running", "completed", "interrupted", "error"]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=20000)]


def utc_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    provider: Literal["deepseek", "glm"] = "deepseek"
    model: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    platform: Literal["linux/amd64", "linux/arm64"] | None = None
    memory_mb: int = Field(default=2048, ge=256, le=32768)
    max_model_calls: int = Field(default=30, ge=1, le=200)
    command_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    verification_timeout_seconds: int = Field(default=180, ge=1, le=3600)
    budget_reminders: bool = True
    explicit_finish: bool = False

    @field_validator("image", "model")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(char.isspace() or char == "\x00" for char in value)):
            raise ValueError("identifier must not contain whitespace or null bytes")
        return value

    @model_validator(mode="after")
    def freeze_model(self):
        if self.model is None:
            self.model = PROVIDERS[self.provider]["model"]
        return self


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    goal: NonEmptyText
    verification_command: NonEmptyText
    base_sha: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    execution: ExecutionConfig | None = None

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
    execution: ExecutionConfig | None = None
    validation_status: ValidationStatus = "not_run"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifacts_dir: str | None = None
    error: str = ""
    result: dict | None = None
    cleanup_pending: bool = False


class TaskSummary(BaseModel):
    """List metadata; large repair results and execution configuration stay in the detail endpoint."""

    task_id: UUID
    repo_path: str
    workdir: str
    goal: str
    status: TaskStatus
    validation_status: ValidationStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str
    cleanup_pending: bool


class TaskPage(BaseModel):
    items: list[TaskSummary]
    next_cursor: str | None


class TaskCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    created_at: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    task_id: UUID
    status: TaskStatus | None

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("cursor timestamp must be UTC")
        return utc_timestamp(parsed)
