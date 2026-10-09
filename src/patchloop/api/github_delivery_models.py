"""Local PR previews. A preview is neither a publication nor permission to publish."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .models import ReviewEvidence, Sha256
from .repository_models import CommitSHA, validate_ref
from .source_models import GitHubRepository


class GitHubDeliveryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_branch: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    body: Annotated[str, StringConstraints(min_length=1, max_length=60000)] | None = None
    draft: bool = Field(default=True, strict=True)

    @field_validator("base_branch")
    @classmethod
    def check_branch(cls, value: str) -> str:
        validate_ref(value)
        if value == "HEAD" or value.startswith("refs/"):
            raise ValueError("base_branch must be a short branch name, such as main or release/1.x")
        return value

    @field_validator("title", "body")
    @classmethod
    def check_text(cls, value: str | None, info) -> str | None:
        if value is not None:
            if not value.strip() or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("PR text must contain valid, nonempty Unicode text")
            allowed = "\n\r\t" if info.field_name == "body" else ""
            if any((ord(char) < 32 or ord(char) == 127) and char not in allowed for char in value):
                raise ValueError("PR text contains unsupported control characters")
        return value


class GitHubPreviewVerification(BaseModel):
    command: str
    baseline_status: Literal["failed"] = "failed"
    baseline_exit_code: Literal[1] = 1
    candidate_status: Literal["passed"] = "passed"
    candidate_exit_code: Literal[0] = 0


class GitHubDeliveryPlan(BaseModel):
    version: Literal[1] = 1
    task_id: UUID
    source_id: UUID
    preparation_id: UUID
    issue_url: str
    repository: GitHubRepository
    base_branch: str
    base_sha: CommitSHA
    head_branch: str
    head_sha: CommitSHA
    tree_sha: CommitSHA
    delivery_id: UUID
    review_id: UUID
    review_revision: int
    evidence: ReviewEvidence
    changed_paths: list[str]
    verification: GitHubPreviewVerification
    title: str
    body: str
    draft: bool


class GitHubDeliveryPreview(BaseModel):
    status: Literal["preview"] = "preview"
    publish_enabled: Literal[False] = False
    remote_checks: Literal["not_checked"] = "not_checked"
    checked_at: AwareDatetime
    plan_sha256: Sha256
    plan: GitHubDeliveryPlan


class GitHubPublicationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    preview: GitHubDeliveryPreviewRequest
    plan_sha256: Sha256
    confirmed: bool = Field(strict=True)

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("explicit publication confirmation is required")
        return value


class GitHubActor(BaseModel):
    id: int = Field(gt=0, strict=True)
    login: str


class GitHubPullRequest(BaseModel):
    number: int = Field(gt=0, strict=True)
    url: str
    state: Literal["open", "closed"]
    base_sha: CommitSHA
    head_sha: CommitSHA


class GitHubPublicationRecord(BaseModel):
    publication_id: UUID
    task_id: UUID
    request_id: UUID
    submission: GitHubPublicationSubmission
    plan_sha256: Sha256
    plan: GitHubDeliveryPlan
    status: Literal["pending", "blocked", "uncertain", "completed"] = "pending"
    phase: Literal["check", "push", "pull_request", "completed"] = "check"
    actor: GitHubActor | None = None
    push_attempted: bool = False
    branch_pushed: bool = False
    pr_attempted: bool = False
    pull_request: GitHubPullRequest | None = None
    error: str = ""
    created_at: AwareDatetime
    updated_at: AwareDatetime


class GitHubPublicationResponse(BaseModel):
    publication: GitHubPublicationRecord
    replayed: bool


class GitHubPublicationState(BaseModel):
    task_id: UUID
    configured: bool
    items: list[GitHubPublicationRecord]


class GitHubPublicationCheck(BaseModel):
    preview: GitHubDeliveryPreview
    actor: GitHubActor
    remote_base_sha: CommitSHA
    remote_head_sha: CommitSHA | None
    can_publish: bool
    reason: str = ""
