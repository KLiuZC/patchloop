import json
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api.models import ExecutionConfig, TaskRecord
from patchloop.api.store import TaskStore
from patchloop.repair import RepairResult
from patchloop.worker import TaskWorker, execute_task, worker_lock


@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    return store


def enqueue(store, *, configured=True, **overrides):
    now = datetime.now(timezone.utc)
    record = TaskRecord(
        task_id=uuid4(), repo_path=str(store.path.parent), workdir=".", base_sha="a" * 40,
        goal="修复测试", verification_command="python -m pytest -q", status="queued",
        created_at=now, updated_at=now,
        execution=ExecutionConfig(image="fake-image") if configured else None,
    ).model_copy(update=overrides)
    store.create(record)
    return record


def test_v1_database_migration_keeps_original_tasks_storage_only(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    task_id = uuid4()
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, workdir TEXT NOT NULL,
                base_sha TEXT NOT NULL, goal TEXT NOT NULL, verification_command TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)
        connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (str(task_id), str(tmp_path), ".", "a" * 40, "保留旧记录", "exit 1", "queued", now, now))
    store = TaskStore(database)
    store.initialize()
    store.initialize()
    saved = store.get(task_id)
    assert saved.goal == "保留旧记录" and saved.status == "queued"
    assert saved.execution is None and saved.validation_status == "not_run"
    assert store.claim(tmp_path / "runs") is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_concurrent_claims_never_duplicate_a_task(store):
    queued = enqueue(store)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: TaskStore(store.path).claim(store.path.parent / "runs"), range(8)))
    claimed = [record for record in claims if record]
    assert [record.task_id for record in claimed] == [queued.task_id]
    assert claimed[0].status == "running" and claimed[0].started_at is not None


def test_api_creation_does_not_recover_an_active_worker(store):
    queued = enqueue(store)
    store.claim(store.path.parent / "runs")
    with TestClient(create_app(store.path)) as client:
        assert client.get(f"/tasks/{queued.task_id}").json()["status"] == "running"


@pytest.mark.parametrize("validation", ["passed", "failed", "inconclusive"])
def test_completed_execution_keeps_validation_separate_and_artifacts_queryable(store, validation):
    queued = enqueue(store)

    def executor(task, directory):
        assert store.get(task.task_id).status == "running"
        directory.mkdir(parents=True)
        (directory / "patch.diff").write_text("saved patch")
        return RepairResult(task_id=str(task.task_id), execution_status="completed", validation_status=validation,
                            patch="saved patch", artifacts_dir=directory, turns=2, prompt_tokens=20)

    assert TaskWorker(store, executor=executor).run(once=True) == 1
    with TestClient(create_app(store.path)) as client:
        saved = client.get(f"/tasks/{queued.task_id}").json()
        assert saved["status"] == "completed" and saved["validation_status"] == validation
        assert saved["result"]["prompt_tokens"] == 20
        assert saved["finished_at"] is not None
        assert client.get(f"/tasks/{queued.task_id}/artifacts/patch.diff").text == "saved patch"
        assert client.get(f"/tasks/{queued.task_id}/artifacts/unknown").status_code == 404
        # An artifact symlink must not expose files outside this task directory.
        outside = store.path.parent / "private.txt"
        outside.write_text("private content")
        (Path(saved["artifacts_dir"]) / "summary.json").symlink_to(outside)
        assert client.get(f"/tasks/{queued.task_id}/artifacts/summary.json").status_code == 404


def test_worker_keeps_error_and_does_not_retry_or_consume_legacy_tasks(store):
    legacy = enqueue(store, configured=False)
    bad = enqueue(store)
    good = enqueue(store)
    seen = []

    def executor(task, directory):
        seen.append(task.task_id)
        if task.task_id == bad.task_id:
            raise RuntimeError("model unavailable")
        return RepairResult(task_id=str(task.task_id), execution_status="completed", validation_status="passed")

    worker = TaskWorker(store, executor=executor)
    worker.run(once=True)
    assert store.get(bad.task_id).status == "error"
    error_file = Path(store.get(bad.task_id).artifacts_dir) / "worker-error.json"
    assert "model unavailable" in json.loads(error_file.read_text())["error"]
    worker.run(once=True)
    worker.run(once=True)
    assert seen == [bad.task_id, good.task_id]
    assert store.get(legacy.task_id).status == "queued"


def test_targeted_run_does_not_claim_other_queued_tasks(store):
    other = enqueue(store)
    target = enqueue(store)
    seen = []

    def executor(task, directory):
        seen.append(task.task_id)
        return RepairResult(task_id=str(task.task_id), execution_status="completed", validation_status="passed")

    TaskWorker(store, executor=executor).run(task_id=target.task_id)
    assert seen == [target.task_id] and store.get(other.task_id).status == "queued"


def test_second_worker_cannot_recover_or_claim_while_first_worker_holds_lock(store):
    task = enqueue(store)
    with worker_lock(store.path):
        store.claim(store.path.parent / "runs")
        with pytest.raises(RuntimeError, match="another worker"):
            TaskWorker(store).run(once=True)
        assert store.get(task.task_id).status == "running"


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGKILL])
def test_worker_process_exit_recovers_without_repeating_model_work(store, stop_signal):
    task = enqueue(store)
    script = '''
import sys, time
from pathlib import Path
from patchloop.api.store import TaskStore
from patchloop.worker import TaskWorker, worker_signals
def busy(task, directory):
    directory.mkdir(parents=True)
    (directory / "partial.log").write_text("work already performed")
    try:
        time.sleep(60)
    finally:
        (directory / "cleanup.log").write_text("cleanup reached")
try:
    with worker_signals():
        TaskWorker(TaskStore(Path(sys.argv[1])), executor=busy).run(once=True)
except KeyboardInterrupt:
    sys.exit(130)
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(store.path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            saved = store.get(task.task_id)
            if saved.artifacts_dir and (Path(saved.artifacts_dir) / "partial.log").exists():
                break
            assert process.poll() is None
            time.sleep(0.05)
        else:
            pytest.fail("worker did not start")
        process.send_signal(stop_signal)
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    worker = TaskWorker(store, executor=lambda *args: pytest.fail("interrupted work must not be retried"))
    assert worker.run(once=True) == 0
    saved = store.get(task.task_id)
    assert saved.status == "interrupted" and saved.validation_status == "inconclusive"
    assert (Path(saved.artifacts_dir) / "partial.log").read_text() == "work already performed"
    if stop_signal == signal.SIGTERM:
        assert (Path(saved.artifacts_dir) / "cleanup.log").exists()


def test_worker_wont_write_a_result_for_another_task(store):
    task = enqueue(store)
    worker = TaskWorker(store, executor=lambda *args: RepairResult(
        task_id=str(uuid4()), execution_status="completed", validation_status="passed",
    ))
    worker.run(once=True)
    assert store.get(task.task_id).status == "error"
    assert "different task" in store.get(task.task_id).error


def test_platform_mismatch_is_rejected_before_model_setup(store, monkeypatch):
    import patchloop.worker as module

    record = enqueue(store, execution=ExecutionConfig(image="toy", platform="linux/amd64"))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="sha256:" + "a" * 64 + " linux/arm64\n",
    ))
    monkeypatch.setattr(module, "LLM", lambda *a: pytest.fail("model must not be created for invalid platform"))
    with pytest.raises(ValueError, match="differs"):
        execute_task(record, store.path.parent / "runs")


def local_repair_executor(store, *, interrupt=False, interrupt_snapshot=False):
    from patchloop.repair import RepairRunner, RepairTask, VerificationProfile
    from patchloop.sandbox import LocalRunner

    repo = store.path.parent / "repo"
    repo.mkdir()
    (repo / "value.txt").write_text("one\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    task = enqueue(store, repo_path=str(repo), base_sha=base, verification_command='test "$(cat value.txt)" = two')

    def executor(record, artifacts):
        class Runner(LocalRunner):
            snapshot_calls = 0

            def diff(self):
                self.snapshot_calls += 1
                if interrupt_snapshot and self.snapshot_calls == 1:
                    raise KeyboardInterrupt
                return super().diff()

        class ScriptedModel:
            provider = model = "fake"
            turn = 0

            def chat(self, *args):
                self.turn += 1
                if self.turn == 1:
                    msg = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="edit", function=SimpleNamespace(
                        name="bash", arguments=json.dumps({"cmd": "echo two > value.txt"}),
                    ))])
                elif interrupt:
                    # Exercise the same exception used by the worker's SIGTERM handler.
                    raise KeyboardInterrupt
                else:
                    msg = SimpleNamespace(content="done", tool_calls=[])
                return msg, SimpleNamespace(prompt_tokens=11, completion_tokens=3)

        repair_task = RepairTask(task_id=str(record.task_id), repo_path=Path(record.repo_path), base_sha=record.base_sha,
                                 goal=record.goal, workdir=record.workdir,
                                 verification=VerificationProfile(command=record.verification_command))
        return RepairRunner(repair_task, artifacts, lambda root, workdir: Runner(root / workdir)).run(ScriptedModel())

    return task, executor


def test_graceful_stop_archives_partial_patch_and_usage_before_checkout_cleanup(store):
    task, executor = local_repair_executor(store, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        TaskWorker(store, executor=executor).run(once=True)
    saved = store.get(task.task_id)
    assert saved.status == "interrupted" and saved.result["stop_reason"] == "interrupted"
    assert "+two" in saved.result["patch"]
    assert saved.result["prompt_tokens"] == 11 and saved.result["completion_tokens"] == 3
    assert (Path(saved.artifacts_dir) / "patch.diff").read_text() == saved.result["patch"]
    assert (Path(task.repo_path) / "value.txt").read_text() == "one\n"
    assert not saved.cleanup_pending


def test_stop_during_diff_keeps_received_usage_and_retries_snapshot_before_cleanup(store):
    task, executor = local_repair_executor(store, interrupt_snapshot=True)
    with pytest.raises(KeyboardInterrupt):
        TaskWorker(store, executor=executor).run(once=True)
    saved = store.get(task.task_id)
    assert saved.status == "interrupted" and saved.result["stop_reason"] == "interrupted"
    assert saved.result["prompt_tokens"] == 22
    assert saved.result["completion_tokens"] == 6
    assert "+two" in saved.result["patch"]
    assert (Path(saved.artifacts_dir) / "patch.diff").read_text() == saved.result["patch"]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, sqlite3.OperationalError])
def test_final_database_write_failure_recovers_exact_archived_result(store, monkeypatch, failure):
    task, executor = local_repair_executor(store)
    original = store.finish
    writes = 0

    def fail_once(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise failure("interrupted database write")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "finish", fail_once)
    with pytest.raises(failure):
        TaskWorker(store, executor=executor).run(once=True)
    TaskWorker(store, executor=lambda *args: pytest.fail("must recover without new model work")).run(once=True)
    saved = store.get(task.task_id)
    assert saved.status == "completed" and saved.validation_status == "passed"
    assert saved.result == json.loads((Path(saved.artifacts_dir) / "summary.json").read_text())


def test_cleanup_failure_is_retried_before_new_tasks_can_start(store, monkeypatch):
    import patchloop.worker as module

    stale = enqueue(store)
    store.claim(store.path.parent / "runs")
    waiting = enqueue(store)
    cleanup_calls = []

    def unavailable(task):
        cleanup_calls.append(task.task_id)
        raise OSError("Docker temporarily unavailable")

    monkeypatch.setattr(module, "cleanup_task", unavailable)
    executor = lambda *args: pytest.fail("must not run while cleanup is pending")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            TaskWorker(store, executor=executor).run(once=True)
        assert store.get(stale.task_id).cleanup_pending
        assert store.get(waiting.task_id).status == "queued"
    assert cleanup_calls == [stale.task_id, stale.task_id]
    monkeypatch.setattr(module, "cleanup_task", lambda task: None)
    TaskWorker(store, executor=lambda task, directory: RepairResult(
        task_id=str(task.task_id), execution_status="completed", validation_status="passed",
    )).run(once=True)
    assert not store.get(stale.task_id).cleanup_pending
    assert store.get(waiting.task_id).status == "completed"


def test_recovery_rejects_archive_with_changed_patch(store, monkeypatch):
    task, executor = local_repair_executor(store)
    original = store.finish
    monkeypatch.setattr(store, "finish", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        TaskWorker(store, executor=executor).run(once=True)
    artifact = Path(store.get(task.task_id).artifacts_dir) / "patch.diff"
    artifact.write_text("different patch")
    monkeypatch.setattr(store, "finish", original)
    TaskWorker(store, executor=lambda *args: pytest.fail("do not retry")).run(once=True)
    assert store.get(task.task_id).status == "interrupted"
    assert store.get(task.task_id).result is None


def test_api_worker_repair_in_docker_with_scripted_model(tmp_path, monkeypatch):
    import patchloop.worker as module

    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode:
            pytest.skip("Docker not available")
        if subprocess.run(["docker", "image", "inspect", "patchloop-toy"], capture_output=True, timeout=10).returncode:
            pytest.skip("patchloop-toy image not available")
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker not available")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    nested = repo / "example"
    nested.mkdir()
    (nested / "value.txt").write_text("one\n")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    calls = iter([
        SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="edit", function=SimpleNamespace(
            name="bash", arguments=json.dumps({"cmd": "echo two > value.txt"}),
        ))]),
        SimpleNamespace(content="done", tool_calls=[]),
    ])
    llm = SimpleNamespace(provider="fake", model="fake", chat=lambda *args: (next(calls), None))
    monkeypatch.setattr(module, "LLM", lambda *args: llm)
    database = tmp_path / "tasks.sqlite3"
    with TestClient(create_app(database)) as client:
        response = client.post("/tasks", json={
            "repo_path": str(nested), "goal": "change one to two", "verification_command": 'test "$(cat value.txt)" = two',
            "execution": {"image": "patchloop-toy", "max_model_calls": 2},
        })
        assert response.status_code == 201, response.text
        task_id = response.json()["task_id"]
        assert TaskWorker(TaskStore(database), executor=execute_task).run(once=True) == 1
        saved = client.get(f"/tasks/{task_id}").json()
        assert saved["status"] == "completed", saved
        assert saved["validation_status"] == "passed", saved
        validation = client.get(f"/tasks/{task_id}/artifacts/validation.json").json()
        assert validation["baseline"]["status"] == "failed"
        assert validation["candidate"]["status"] == "passed"
        assert "example/value.txt" in client.get(f"/tasks/{task_id}/artifacts/patch.diff").text
        runtime = client.get(f"/tasks/{task_id}/artifacts/runtime.json").json()
        assert runtime["image_id"].startswith("sha256:") and len(runtime["containers"]) == 3
    assert (nested / "value.txt").read_text() == "one\n"
    containers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=patchloop.task_id={task_id}"],
                                capture_output=True, text=True, check=True)
    assert not containers.stdout.strip()
