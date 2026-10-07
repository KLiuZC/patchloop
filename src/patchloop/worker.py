"""Serial task execution for the local API, using one worker per SQLite file."""

import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4

from .api.models import TaskRecord
from .api.store import TaskStore
from .llm import LLM
from .repair import RepairResult, RepairRunner
from .sandbox import DockerRunner


@contextmanager
def worker_lock(database: Path):
    """Do not unlink this file: an existing inode must keep all workers mutually exclusive."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with database.with_name(database.name + ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError("another worker is already using this database") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def worker_signals():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_task(record: TaskRecord, artifacts: Path) -> RepairResult:
    config = record.execution
    if config is None:
        raise ValueError("task has no execution configuration")
    inspected = subprocess.run(
        ["docker", "image", "inspect", config.image, "--format", "{{.Id}} {{.Os}}/{{.Architecture}}"],
        capture_output=True, text=True, timeout=30,
    )
    image = inspected.stdout.split()
    if inspected.returncode != 0 or len(image) != 2 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image[0]):
        raise RuntimeError("Docker image is not available locally; start Docker and prepare the requested image")
    image_id, platform = image
    if config.platform is not None and config.platform != platform:
        raise ValueError(f"requested platform {config.platform} differs from local image platform {platform}")
    llm = LLM(config.provider, config.model)
    runtime = {"task_id": str(record.task_id), "image": config.image, "image_id": image_id, "platform": platform, "containers": []}

    def runner_factory(root: Path, workdir: str):
        runner = DockerRunner(
            image_id, workdir="/work" if workdir == "." else f"/work/{workdir}",
            mount=root, mount_target="/work", platform=platform,
            memory=f"{config.memory_mb}m", network="none",
            labels={"patchloop.task_id": str(record.task_id)},
        )
        runtime["containers"].append(runner.name)
        # RepairRunner has already initialized task.json before constructing a runner.
        _write_json(artifacts / "runtime.json", runtime)
        return runner

    task = record.to_repair_task()
    return RepairRunner(task, artifacts, runner_factory).run(llm)


def cleanup_task(record: TaskRecord) -> None:
    """Remove only containers labelled for a task that reached Docker execution."""
    if not record.artifacts_dir or not (Path(record.artifacts_dir) / "runtime.json").is_file():
        return
    found = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=patchloop.task_id={record.task_id}"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    ids = found.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True, timeout=30, check=True)


def _saved_result(task: TaskRecord) -> dict | None:
    """Recover only a complete, consistent RepairRunner archive for this request."""
    if not task.artifacts_dir:
        return None
    root = Path(task.artifacts_dir)
    try:
        summary = json.loads((root / "summary.json").read_text())
        request = json.loads((root / "task.json").read_text())
        validation = json.loads((root / "validation.json").read_text())
        patch = (root / "patch.diff").read_bytes().decode("utf-8")
        if not all(isinstance(value, dict) for value in (summary, request, validation)):
            return None
        expected = {"task_id": str(task.task_id), "repo_path": task.repo_path, "workdir": task.workdir,
                    "base_sha": task.base_sha, "goal": task.goal}
        if any(request.get(key) != value for key, value in expected.items()):
            return None
        if not isinstance(request.get("verification"), dict) or request["verification"].get("command") != task.verification_command:
            return None
        if summary.get("task_id") != str(task.task_id) or summary.get("patch") != patch:
            return None
        if summary.get("execution_status") not in ("completed", "interrupted", "error"):
            return None
        if summary.get("validation_status") not in ("passed", "failed", "inconclusive", "not_run"):
            return None
        patch_hash = hashlib.sha256(patch.encode()).hexdigest() if patch else None
        if (summary.get("validation") != validation or validation.get("status") != summary["validation_status"]
                or validation.get("base_sha") != task.base_sha or validation.get("patch_sha256") != patch_hash
                or validation.get("command") != task.verification_command or not isinstance(summary.get("error"), str)):
            return None
        return summary
    except (OSError, ValueError):
        return None


class TaskWorker:
    def __init__(
        self, store: TaskStore, artifacts_root: Path | None = None,
        executor: Callable[[TaskRecord, Path], RepairResult] | None = None,
        log: Callable[[str], None] = print,
    ):
        self.store = store
        self.artifacts_root = (artifacts_root or store.path.parent / "task-runs").expanduser().resolve()
        self.executor = executor or execute_task
        self.log = log

    def _recover(self, reason: str) -> None:
        for task in self.store.running():
            result = _saved_result(task)
            if result is not None:
                self.store.finish(task.task_id, status=result["execution_status"], validation_status=result["validation_status"],
                                  error=result["error"], result=result)
                self.log(f"task={task.task_id} restored saved result: {result['execution_status']}")
            else:
                self.store.finish(task.task_id, status="interrupted", validation_status="inconclusive", error=reason)
                self.log(f"task={task.task_id} status=interrupted")
        for task in self.store.pending_cleanup():
            self._cleanup(task)

    def _cleanup(self, task: TaskRecord) -> None:
        try:
            cleanup_task(task)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"task={task.task_id} container cleanup incomplete; restart worker when Docker is available") from exc
        self.store.mark_clean(task.task_id)

    def _execute(self, task: TaskRecord) -> None:
        artifacts = Path(task.artifacts_dir)
        self.log(f"task={task.task_id} status=running artifacts={artifacts}")
        try:
            result = self.executor(task, artifacts)
            if result.task_id != str(task.task_id):
                raise ValueError("executor returned a result for a different task")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                _write_json(artifacts / "worker-error.json", {"task_id": str(task.task_id), "error": error})
            except OSError as write_error:
                error += f"; error artifact unavailable: {type(write_error).__name__}"
            self.store.finish(task.task_id, status="error", validation_status="inconclusive", error=error)
        else:
            self.store.finish(task.task_id, status=result.execution_status,
                              validation_status=result.validation_status, error=result.error, result=result.to_dict())
        self._cleanup(task)
        finished = self.store.get(task.task_id)
        self.log(f"task={task.task_id} status={finished.status} validation={finished.validation_status}")

    def run(self, *, once: bool = False, task_id: UUID | None = None, poll_interval: float = 1.0) -> int:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        count = 0
        with worker_lock(self.store.path):
            self.store.initialize()
            try:
                self._recover("worker exited before saving the final database state; task was not retried")
                if task_id is not None:
                    selected = self.store.get(task_id)
                    if selected is None or selected.status != "queued" or selected.execution is None:
                        raise ValueError("task must be queued and include execution configuration; submit a new request if needed")
                self.log(f"worker ready database={self.store.path}")
                while True:
                    task = self.store.claim(self.artifacts_root, task_id)
                    if task is not None:
                        self._execute(task)
                        count += 1
                    if once or task_id is not None:
                        if task is None:
                            self.log("no runnable queued tasks")
                        return count
                    if task is None:
                        time.sleep(poll_interval)
            except KeyboardInterrupt:
                self._recover("worker was stopped; task was not retried")
                raise
