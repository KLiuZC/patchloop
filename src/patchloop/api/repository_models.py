"""Public repository preparation, kept separate from executable repair tasks."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .source_models import GitHubRepository


def validate_ref(value: str) -> str:
    # A branch, tag or full SHA, not a Git revision expression or an option.
    if (not value or len(value) > 200 or value.startswith(("-", "/"))
            or value.endswith(("/", ".")) or ".." in value or "//" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "~^:?*[\\" for char in value)
            or "@{" in value or value == "@"
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise ValueError("ref must be a branch, tag or full commit SHA, not a revision expression")
    return value


CommitSHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class RepositoryPreparationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    source_id: UUID
    ref: str | None = None

    @field_validator("ref")
    @classmethod
    def check_ref(cls, value: str | None) -> str | None:
        return validate_ref(value) if value is not None else None


class ResolvedRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: GitHubRepository
    ref: str
    base_sha: CommitSHA
    resolved_at: AwareDatetime


class RepositoryPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preparation_id: UUID
    request_id: UUID
    source_id: UUID
    repository: GitHubRepository
    requested_ref: str | None
    resolved_ref: str | None = None
    base_sha: CommitSHA | None = None
    resolved_at: AwareDatetime | None = None
    clone_url: str
    repo_path: str
    status: Literal["preparing", "ready", "failed"] = "preparing"
    created_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    error: str = ""
    error_status: int | None = Field(default=None, ge=400, le=599)


class RepositoryPreparationResult(BaseModel):
    preparation: RepositoryPreparation
    replayed: bool


class RepositoryPreparationState(BaseModel):
    preparation: RepositoryPreparation
    checkout_intact: bool | None = None
    reason: str = ""
