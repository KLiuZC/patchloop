from dataclasses import asdict, dataclass, field
from pathlib import Path
import fnmatch
import re
from typing import Literal


ExecutionStatus = Literal["completed", "interrupted", "error"]
ValidationStatus = Literal["not_run", "passed", "failed", "inconclusive"]
ReviewStatus = Literal["pending", "accepted", "rejected", "stale"]


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 30
    command_timeout_seconds: int = 60
    task_timeout_seconds: int = 600


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
        paths: set[str] = set()
        for line in patch.splitlines():
            match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
            if match:
                paths.update(match.groups())
        violations: list[str] = []
        for path in sorted(paths):
            if self.allowed_paths and not any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_paths):
                violations.append(f"path not allowed: {path}")
            if any(fnmatch.fnmatch(path, pattern) for pattern in self.protected_paths):
                violations.append(f"path protected: {path}")
        return violations


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

    def to_dict(self) -> dict:
        return asdict(self)


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

    def to_dict(self) -> dict:
        value = asdict(self)
        if self.validation is not None:
            value["validation"] = self.validation.to_dict()
        if self.artifacts_dir is not None:
            value["artifacts_dir"] = str(self.artifacts_dir)
        return value
