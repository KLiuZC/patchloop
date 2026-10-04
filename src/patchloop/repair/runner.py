import time
from pathlib import Path
from typing import Callable

from ..loop import run
from .artifacts import ArtifactStore
from .contracts import RepairResult, RepairTask
from .verifier import RunnerFactory, verify_patch
from .workspace import WorktreeWorkspace


class RepairRunner:
    """Run one coding attempt, then verify its exact patch from a clean base."""

    def __init__(self, task: RepairTask, artifacts_root: Path, runner_factory: RunnerFactory):
        self.task = task
        self.artifacts = ArtifactStore(Path(artifacts_root))
        self.runner_factory = runner_factory

    def run(self, llm) -> RepairResult:
        started = time.time()
        self.artifacts.write_task(self.task)
        self.artifacts.event("run_started", task_id=self.task.task_id, base_sha=self.task.base_sha)
        patch = ""
        execution = None
        error = ""
        try:
            with WorktreeWorkspace(self.task.repo_path, self.task.base_sha) as workspace:
                self.artifacts.event("workspace_ready", base_sha=self.task.base_sha)
                with self.runner_factory(workspace.path) as runner:
                    execution = run(
                        self.task.goal,
                        llm,
                        runner,
                        max_turns=self.task.limits.max_model_calls,
                        timeout=self.task.limits.command_timeout_seconds,
                        traj_dir=self.artifacts.root / "trajectory",
                        budget_reminders=self.task.budget_reminders,
                        explicit_finish=self.task.explicit_finish,
                        run_id=self.task.task_id,
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

        patch_hash = self.artifacts.write_patch(patch)
        if execution is None:
            result = RepairResult(
                task_id=self.task.task_id,
                execution_status="error",
                validation_status="inconclusive",
                stop_reason="error",
                patch=patch,
                artifacts_dir=self.artifacts.root,
                error=error,
            )
            self.artifacts.write_summary(result.to_dict())
            return result

        validation = verify_patch(self.task, patch, self.runner_factory)
        self.artifacts.write_validation(validation)
        self.artifacts.event(
            "validation_finished",
            status=validation.status,
            patch_sha256=patch_hash or None,
            exit_code=validation.exit_code,
        )
        result = RepairResult(
            task_id=self.task.task_id,
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
        self.artifacts.event(
            "run_finished",
            execution_status=result.execution_status,
            validation_status=result.validation_status,
        )
        return result
