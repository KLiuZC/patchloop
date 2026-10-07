import json
import shlex
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..eval import task_text
from ..loop import run
from ..sandbox import DockerRunner
from .artifacts import ArtifactStore, sha256_text
from .contracts import BaselineResult, RepairResult, ValidationResult
from .verifier import _exit_code


@dataclass(frozen=True)
class SWEbenchTask:
    instance_id: str
    repo: str
    problem_statement: str
    image: str
    base_commit: str
    workdir: str = "/testbed"
    verification_command: str = "pytest -q"

    @classmethod
    def from_instance(cls, instance: dict[str, Any], verification_command: str = "pytest -q") -> "SWEbenchTask":
        image = str(instance.get("image") or "").strip()
        if not image:
            raise ValueError(f"instance has no image: {instance.get('instance_id')}")
        if ":" not in image.rsplit("/", 1)[-1]:
            image += ":latest"
        base_commit = str(instance.get("base_commit") or "").strip()
        if not base_commit:
            raise ValueError(f"instance has no base_commit: {instance.get('instance_id')}")
        return cls(
            instance_id=str(instance["instance_id"]),
            repo=str(instance["repo"]),
            problem_statement=str(instance["problem_statement"]),
            image=image,
            base_commit=base_commit,
            verification_command=verification_command,
        )

    @property
    def prompt(self) -> str:
        return task_text(
            {
                "repo": self.repo,
                "problem_statement": self.problem_statement,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _profile_status(output: str, exit_code: int) -> str:
    """Separate an invalid test selector from an actual test failure."""
    if exit_code == 0:
        return "passed"
    if exit_code == 4 and ("not found" in output or "no tests ran" in output):
        return "inconclusive"
    return "failed"


class SWEbenchRepairRunner:
    """Run one SWE-bench task in Docker and independently verify its patch."""

    def __init__(
        self,
        task: SWEbenchTask,
        artifacts_root: Path,
        memory: str = "3g",
        platform: str = "linux/amd64",
    ):
        self.task = task
        self.artifacts = ArtifactStore(Path(artifacts_root))
        self.memory = memory
        self.platform = platform

    def _runner(self) -> DockerRunner:
        return DockerRunner(
            self.task.image,
            workdir=self.task.workdir,
            platform=self.platform,
            memory=self.memory,
            network="none",
        )

    def _check_base(self, runner: DockerRunner) -> None:
        exists = runner.run(
            f"git cat-file -e {shlex.quote(self.task.base_commit)}^{{commit}}",
            timeout=30,
        )
        if "[exit code" in exists or "[command timed out" in exists:
            raise RuntimeError(f"base commit is not available in image: {self.task.base_commit}")
        checkout = runner.run(
            f"git checkout --detach {shlex.quote(self.task.base_commit)}",
            timeout=60,
        )
        if "[exit code" in checkout or "[command timed out" in checkout:
            raise RuntimeError(f"could not checkout base commit: {self.task.base_commit}\n{checkout}")
        actual = runner.run("git rev-parse HEAD", timeout=30).strip()
        if actual != self.task.base_commit:
            raise RuntimeError(f"base commit mismatch after checkout: expected {self.task.base_commit}, got {actual}")

    def _run_profile(self, runner: DockerRunner) -> tuple[str, int]:
        output = runner.run(self.task.verification_command, timeout=180)
        return output, _exit_code(output)

    def run(
        self,
        llm,
        *,
        max_model_calls: int = 40,
        command_timeout: int = 180,
        budget_reminders: bool = True,
        explicit_finish: bool = False,
    ) -> RepairResult:
        if any(self.artifacts.root.iterdir()):
            raise ValueError("use a new --artifacts directory for a new repair, or repair-judge to grade a saved patch")
        started = time.time()
        self.artifacts.write_task(self.task)
        self.artifacts.event("run_started", instance_id=self.task.instance_id, base_commit=self.task.base_commit)
        patch = ""
        execution = None
        try:
            with self._runner() as runner:
                self._check_base(runner)
                self.artifacts.event("workspace_ready", image=self.task.image, workdir=self.task.workdir)
                execution = run(
                    self.task.prompt,
                    llm,
                    runner,
                    max_turns=max_model_calls,
                    timeout=command_timeout,
                    traj_dir=self.artifacts.root / "trajectory",
                    run_id=self.task.instance_id,
                    budget_reminders=budget_reminders,
                    explicit_finish=explicit_finish,
                )
                patch = execution.patch
                self.artifacts.event(
                    "execution_finished",
                    stop_reason=execution.stop_reason,
                    finished=execution.finished,
                    patch_present=bool(patch),
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.artifacts.event("execution_error", error=error)
            result = RepairResult(
                task_id=self.task.instance_id,
                execution_status="error",
                validation_status="inconclusive",
                stop_reason="error",
                artifacts_dir=self.artifacts.root,
                error=error,
                seconds=round(time.time() - started, 1),
            )
            self.artifacts.write_patch("")
            self.artifacts.write_summary(result.to_dict())
            return result

        self.artifacts.write_patch(patch)
        baseline = self._clean_verification()
        if not patch:
            validation = ValidationResult(
                status="inconclusive",
                profile_id="swebench",
                command=self.task.verification_command,
                base_sha=self.task.base_commit,
                error="empty patch",
                baseline=baseline,
            )
        else:
            validation = self._candidate_verification(patch, baseline)
        self.artifacts.write_validation(validation)
        self.artifacts.event("validation_finished", status=validation.status, exit_code=validation.exit_code)
        result = RepairResult(
            task_id=self.task.instance_id,
            execution_status="completed" if execution.finished else "interrupted",
            validation_status=validation.status,
            stop_reason=execution.stop_reason,
            patch=patch,
            validation=validation,
            artifacts_dir=self.artifacts.root,
            prompt_tokens=execution.prompt_tokens,
            cached_tokens=execution.cached_tokens,
            completion_tokens=execution.completion_tokens,
            cost=execution.cost,
            seconds=round(time.time() - started, 1),
        )
        self.artifacts.write_summary(result.to_dict())
        self.artifacts.event("run_finished", execution_status=result.execution_status, validation_status=result.validation_status)
        return result

    def _clean_verification(self) -> BaselineResult:
        started = time.time()
        with self._runner() as runner:
            self._check_base(runner)
            output, exit_code = self._run_profile(runner)
        return BaselineResult(
            status=_profile_status(output, exit_code),
            profile_id="swebench",
            command=self.task.verification_command,
            base_sha=self.task.base_commit,
            exit_code=exit_code,
            output=output,
            seconds=round(time.time() - started, 1),
        )

    def _candidate_verification(self, patch: str, baseline: BaselineResult) -> ValidationResult:
        started = time.time()
        with self._runner() as runner:
            self._check_base(runner)
            runner.put("/tmp/patchloop-candidate.patch", patch)
            apply_output = runner.run(
                "git apply --binary --whitespace=nowarn /tmp/patchloop-candidate.patch",
                timeout=60,
            )
            apply_exit = _exit_code(apply_output)
            if apply_exit != 0:
                return ValidationResult(
                    status="failed",
                    profile_id="swebench",
                    command=self.task.verification_command,
                    base_sha=self.task.base_commit,
                    exit_code=apply_exit,
                    output=apply_output,
                    seconds=round(time.time() - started, 1),
                    error="patch did not apply",
                    baseline=baseline,
                )
            output, exit_code = self._run_profile(runner)
        candidate_status = _profile_status(output, exit_code)
        status = candidate_status if baseline.status == "failed" else "inconclusive"
        return ValidationResult(
            status=status,
            profile_id="swebench",
            command=self.task.verification_command,
            base_sha=self.task.base_commit,
            patch_sha256=sha256_text(patch),
            exit_code=exit_code,
            output=output,
            seconds=round(time.time() - started, 1),
            error="" if status == candidate_status else "baseline verification already passed",
            baseline=baseline,
        )
