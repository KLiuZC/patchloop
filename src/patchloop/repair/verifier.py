import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .artifacts import sha256_text
from .contracts import RepairTask, ValidationResult
from .workspace import WorktreeWorkspace


RunnerFactory = Callable[[Path, str], object]
_EXIT_CODE = re.compile(r"\n\[exit code (-?\d+)\]\\?$")


def _exit_code(output: str) -> int:
    if "[command timed out after" in output:
        return 124
    match = _EXIT_CODE.search(output)
    return int(match.group(1)) if match else 0


def verify_patch(task: RepairTask, patch: str, runner_factory: RunnerFactory) -> ValidationResult:
    """Apply a candidate patch to a fresh base worktree and run its fixed profile."""
    patch_hash = sha256_text(patch) if patch else None
    if not patch:
        return ValidationResult(
            status="inconclusive",
            profile_id=task.verification.id,
            command=task.verification.command,
            base_sha=task.base_sha,
            patch_sha256=None,
            error="empty patch",
        )

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
    with WorktreeWorkspace(task.repo_path, task.base_sha) as workspace:
        applied = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=workspace.path,
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
            )

        with runner_factory(workspace.path, task.workdir) as runner:
            output = runner.run(task.verification.command, task.verification.timeout_seconds)
        exit_code = _exit_code(output)
        return ValidationResult(
            status="passed" if exit_code == 0 else "failed",
            profile_id=task.verification.id,
            command=task.verification.command,
            base_sha=task.base_sha,
            patch_sha256=patch_hash,
            exit_code=exit_code,
            output=output,
            seconds=round(time.time() - started, 1),
        )
