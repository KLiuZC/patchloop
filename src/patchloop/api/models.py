from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from ..llm import PROVIDERS
from ..repair.contracts import Limits, RepairTask, ReviewStatus, ValidationStatus, VerificationProfile


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

    def to_repair_task(self) -> RepairTask:
        """Use the same frozen verification and limits for execution and review."""
        config = self.execution
        if config is None:
            raise ValueError("task has no execution configuration")
        return RepairTask(
            task_id=str(self.task_id), repo_path=Path(self.repo_path), workdir=self.workdir,
            base_sha=self.base_sha, goal=self.goal,
            verification=VerificationProfile(id="api", command=self.verification_command,
                                             timeout_seconds=config.verification_timeout_seconds),
            limits=Limits(max_model_calls=config.max_model_calls, command_timeout_seconds=config.command_timeout_seconds),
            budget_reminders=config.budget_reminders, explicit_finish=config.explicit_finish,
        )


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


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_sha: Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    patch_sha256: Sha256
    validation_sha256: Sha256
    task_sha256: Sha256
    summary_sha256: Sha256
    record_sha256: Sha256


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    decision: Literal["accepted", "rejected"]
    comment: Annotated[str, StringConstraints(max_length=4000)] = ""
    evidence: ReviewEvidence

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("null bytes are not allowed")
        return value


class ReviewRecord(BaseModel):
    review_id: UUID
    task_id: UUID
    revision: int
    request_id: UUID
    decision: Literal["accepted", "rejected"]
    comment: str
    evidence: ReviewEvidence
    created_at: datetime


class ReviewState(BaseModel):
    task_id: UUID
    status: ReviewStatus = "pending"
    revision: int = 0
    evidence: ReviewEvidence | None = None
    can_accept: bool = False
    can_reject: bool = False
    reason: str = ""
    latest_review: ReviewRecord | None = None


class ReviewHistory(BaseModel):
    task_id: UUID
    items: list[ReviewRecord]


class ReviewResponse(BaseModel):
    review: ReviewRecord
    current: ReviewState
    replayed: bool
