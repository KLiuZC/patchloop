import subprocess
import time
from pathlib import Path
from typing import Callable

from .artifacts import sha256_text
from .contracts import BaselineResult, RepairTask, ValidationResult
from .workspace import WorktreeWorkspace


RunnerFactory = Callable[[Path, str], object]

def profile_status(exit_code: int) -> str:
    """Profiles use 0 for pass, 1 for assertion failure, other codes for setup/errors."""
    return {0: "passed", 1: "failed"}.get(exit_code, "inconclusive")


def baseline_error(baseline: BaselineResult) -> str:
    if baseline.status == "failed":
        return ""
    if baseline.status == "passed":
        return "baseline verification already passed"
    return baseline.error or "baseline verification did not establish a test failure"


def verify_patch(task: RepairTask, patch: str, runner_factory: RunnerFactory) -> ValidationResult:
    """Apply a candidate patch to a fresh base worktree and run its fixed profile."""
    patch_hash = sha256_text(patch) if patch else None
    violations = task.validate_patch_paths(patch)
    if violations:
        return ValidationResult(
            status="failed",
            profile_id=task.verification.id,
            command=task.verification.command,
            base_sha=task.base_sha,
            patch_sha256=patch_hash,
            error="; ".join(violations),
        )

    started = time.time()
    baseline = None
    try:
        with WorktreeWorkspace(task.repo_path, task.base_sha) as baseline_workspace:
            with runner_factory(baseline_workspace.path, task.workdir) as runner:
                baseline_command = runner.run_result(task.verification.command, task.verification.timeout_seconds)
            baseline_exit_code = baseline_command.exit_code
            baseline = BaselineResult(
                status=profile_status(baseline_exit_code),
                profile_id=task.verification.id,
                command=task.verification.command,
                base_sha=task.base_sha,
                exit_code=baseline_exit_code,
                output=baseline_command.display(task.verification.timeout_seconds),
                seconds=round(time.time() - started, 1),
            )

            if not patch:
                return ValidationResult(
                    status="inconclusive",
                    profile_id=task.verification.id,
                    command=task.verification.command,
                    base_sha=task.base_sha,
                    patch_sha256=None,
                    error="empty patch",
                    baseline=baseline,
                    seconds=round(time.time() - started, 1),
                )


        # Use a second worktree so bytecode caches, generated files and any stateful
        # test side effects from the baseline run cannot affect the candidate run.
        with WorktreeWorkspace(task.repo_path, task.base_sha) as candidate_workspace:
            applied = subprocess.run(
                ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                cwd=candidate_workspace.path,
                input=patch,
                capture_output=True,
                text=True,
            )
            if applied.returncode != 0:
                return ValidationResult(
                    status="failed",
                    profile_id=task.verification.id,
                    command=task.verification.command,
                    base_sha=task.base_sha,
                    patch_sha256=patch_hash,
                    exit_code=applied.returncode,
                    output=applied.stderr or applied.stdout,
                    seconds=round(time.time() - started, 1),
                    error="patch did not apply",
                    candidate_status="failed",
                    candidate_error="patch did not apply",
                    baseline=baseline,
                )

            with runner_factory(candidate_workspace.path, task.workdir) as runner:
                command = runner.run_result(task.verification.command, task.verification.timeout_seconds)
        exit_code = command.exit_code
        candidate_status = profile_status(exit_code)
        status = candidate_status if baseline.status == "failed" else "inconclusive"
        error = baseline_error(baseline)
        return ValidationResult(
            status=status,
            profile_id=task.verification.id,
            command=task.verification.command,
            base_sha=task.base_sha,
            patch_sha256=patch_hash,
            exit_code=exit_code,
            output=command.display(task.verification.timeout_seconds),
            seconds=round(time.time() - started, 1),
            error=error,
            baseline=baseline,
            candidate_status=candidate_status,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        candidate_status = "inconclusive" if baseline else "not_run"
        return ValidationResult(
            status="inconclusive", profile_id=task.verification.id,
            command=task.verification.command, base_sha=task.base_sha,
            patch_sha256=patch_hash, error=error,
            baseline=baseline or BaselineResult(
                status="error", profile_id=task.verification.id,
                command=task.verification.command, base_sha=task.base_sha, error=error,
            ),
            candidate_status=candidate_status,
            candidate_error=error if baseline else "",
            seconds=round(time.time() - started, 1),
        )
