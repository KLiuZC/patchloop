from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


ExecutionStatus = Literal["completed", "interrupted", "error"]
ValidationStatus = Literal["not_run", "passed", "failed", "inconclusive"]
BaselineStatus = Literal["passed", "failed", "inconclusive", "error"]
ReviewStatus = Literal["pending", "accepted", "rejected", "stale"]


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 30
    command_timeout_seconds: int = 60
    task_timeout_seconds: int = 600  # Reserved; only call/command limits are currently enforced.


@dataclass(frozen=True)
class VerificationProfile:
    id: str = "default"
    command: str = "pytest -q"
    timeout_seconds: int = 180


@dataclass(frozen=True)
class RepairTask:
    task_id: str
    repo_path: Path
    base_sha: str
    goal: str
    workdir: str = "."
    issue: str | None = None
    allowed_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    verification: VerificationProfile = field(default_factory=VerificationProfile)
    limits: Limits = field(default_factory=Limits)
    budget_reminders: bool = False
    explicit_finish: bool = False

    def to_dict(self) -> dict:
        value = asdict(self)
        value["repo_path"] = str(self.repo_path)
        return value

    def validate_patch_paths(self, patch: str) -> list[str]:
        """Return changed paths that violate the task's path policy."""
        if not patch or not (self.allowed_paths or self.protected_paths):
            return []
        from .patch_policy import PatchPolicyError, inspect_patch
        try:
            inspect_patch(self.repo_path, self.base_sha, patch.encode("utf-8"),
                          allowed_paths=self.allowed_paths, protected_paths=self.protected_paths)
        except (PatchPolicyError, OSError, ValueError) as exc:
            return [str(exc)]
        return []


@dataclass
class BaselineResult:
    status: BaselineStatus
    profile_id: str
    command: str
    base_sha: str
    exit_code: int | None = None
    output: str = ""
    seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationResult:
    status: ValidationStatus
    profile_id: str
    command: str
    base_sha: str
    patch_sha256: str | None = None
    exit_code: int | None = None
    output: str = ""
    seconds: float = 0.0
    error: str = ""
    baseline: BaselineResult | None = None
    candidate_status: ValidationStatus = "not_run"
    candidate_error: str = ""

    def to_dict(self) -> dict:
        value = asdict(self)
        baseline = value.pop("baseline", None)
        candidate_status = value.pop("candidate_status")
        candidate_error = value.pop("candidate_error")
        value["candidate"] = {**value, "status": candidate_status, "error": candidate_error}
        value["baseline"] = baseline
        return value


@dataclass
class RepairResult:
    task_id: str
    execution_status: ExecutionStatus
    validation_status: ValidationStatus
    review_status: ReviewStatus = "pending"
    stop_reason: str = ""
    patch: str = ""
    validation: ValidationResult | None = None
    artifacts_dir: Path | None = None
    error: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    seconds: float = 0.0
    turns: int = 0

    def to_dict(self) -> dict:
        value = asdict(self)
        if self.validation is not None:
            value["validation"] = self.validation.to_dict()
        if self.artifacts_dir is not None:
            value["artifacts_dir"] = str(self.artifacts_dir)
        return value
