"""Issue-to-task integration using local Git, temporary databases and scripted models."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from patchloop.api import create_app
from patchloop.api.models import IssueTaskSubmission
from patchloop.api.repository_git import _CONFIG
from patchloop.api.repository_models import RepositoryPreparation
from patchloop.api.source_store import IssueSourceStore
from patchloop.repair.runner import RepairRunner
from patchloop.sandbox import LocalRunner
from patchloop.worker import TaskWorker, _saved_result, execute_task
from test_repository_store import make_source
from test_repair import ScriptedLLM, response, tool_call


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def prepared(tmp_path):
    def no_network(request):
        pytest.fail("Issue task creation must not contact GitHub")
    app = create_app(tmp_path / "data/tasks.sqlite3", github_transport=httpx.MockTransport(no_network))
    store = app.state.task_store
    store.initialize()
    source, snapshot = make_source()
    source.body = "Calling value() returns 1; expected 2."
    snapshot.issue["body"] = source.body
    from patchloop.api.source_store import snapshot_digest
    source.snapshot_sha256 = snapshot_digest(snapshot)
    IssueSourceStore(store).save(source, snapshot)
    service = app.state.repository_service
    preparation_id = uuid4()
    repo = service.root / str(preparation_id) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "--template=")
    (repo / "calc.py").write_text("def value():\n    return 1\n")
    (repo / "test_calc.py").write_text("from calc import value\n\ndef test_value():\n    assert value() == 2\n")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    sha = git(repo, "rev-parse", "HEAD")
    (repo / ".git/config").write_bytes(_CONFIG)
    now = datetime.now(timezone.utc)
    prep = RepositoryPreparation(
        preparation_id=preparation_id, request_id=uuid4(), source_id=source.source_id,
        repository=source.repository, requested_ref=None, clone_url=source.repository.html_url + ".git",
        repo_path=str(repo), created_at=now, updated_at=now,
    )
    service.store.create(prep)
    prep = prep.model_copy(update={"status": "ready", "resolved_ref": "main", "resolved_at": now,
                                   "base_sha": sha, "finished_at": now})
    service.store.update(prep)
    with TestClient(app) as client:
        yield SimpleNamespace(app=app, client=client, store=store, repo=repo, preparation=prep,
                              source=source, tmp=tmp_path)


def payload(prepared, **changes):
    return {"request_id": str(uuid4()), "preparation_id": str(prepared.preparation.preparation_id),
            "goal": "Fix value(); only modify calc.py.", "verification_command": f"{sys.executable} -m pytest -q",
            "execution": {"image": "patchloop-toy", "max_model_calls": 10},
            "path_policy": {"allowed_paths": ["calc.py"]}, **changes}


def test_create_query_restart_replay_and_freeze(prepared):
    body = payload(prepared)
    first = prepared.client.post("/tasks/from-issue", json=body)
    assert first.status_code == 201, first.text
    task = first.json()["task"]
    assert task["status"] == "queued" and task["validation_status"] == "not_run"
    assert task["base_sha"] == prepared.preparation.base_sha
    assert task["repo_path"] == str(prepared.repo) and task["workdir"] == "."
    assert task["source"]["issue"] == prepared.source.model_dump(mode="json")
    assert task["source"]["preparation_id"] == body["preparation_id"]
    assert task["execution"]["model"] is not None
    saved = prepared.store.get(UUID(task["task_id"]))
    prompt = saved.to_repair_task().goal
    assert prompt.startswith(body["goal"])
    assert prepared.source.title in prompt and prepared.source.body in prompt
    assert prepared.source.snapshot_sha256 in prompt and prepared.preparation.base_sha in prompt
    location = first.headers["location"]
    assert prepared.client.get(location).json() == task
    # Replay must not refetch, rerun or require a still-existing checkout.
    (prepared.repo / "calc.py").write_text("local changes")
    replay = prepared.client.post("/tasks/from-issue", json=body)
    assert replay.status_code == 200
    assert replay.json() == {"task": task, "replayed": True}
    with TestClient(create_app(prepared.store.path)) as restarted:
        assert restarted.get(location).json() == task
        assert restarted.post("/tasks/from-issue", json=body).json() == replay.json()
    assert len(prepared.client.get("/tasks").json()["items"]) == 1


@pytest.mark.parametrize("field,value", [("goal", "different"), ("verification_command", "exit 0"),
                                          ("execution", {"image": "other"}), ("workdir", "src"),
                                          ("path_policy", {"protected_paths": []}), ("preparation_id", str(uuid4()))])
def test_conflicting_retry_never_creates_second_task(prepared, field, value):
    body = payload(prepared)
    assert prepared.client.post("/tasks/from-issue", json=body).status_code == 201
    assert prepared.client.post("/tasks/from-issue", json={**body, field: value}).status_code == 409
    assert len(prepared.client.get("/tasks").json()["items"]) == 1


@pytest.mark.parametrize("changes", [{"goal": " "}, {"verification_command": ""}, {"execution": None},
                                    {"execution": {}}, {"repo_path": "/tmp"}, {"base_sha": "a" * 40},
                                    {"source_id": str(uuid4())}, {"workdir": "../escape"},
                                    {"workdir": "/tmp"}, {"workdir": "src//foo"}])
def test_invalid_inputs_do_not_enqueue(prepared, changes):
    assert prepared.client.post("/tasks/from-issue", json=payload(prepared, **changes)).status_code == 422
    assert prepared.client.get("/tasks").json()["items"] == []


@pytest.mark.parametrize("mutation", ["dirty", "untracked", "head", "config", "missing", "symlink", "not-ready"])
def test_unusable_preparation_cannot_create_task(prepared, mutation):
    if mutation == "dirty":
        (prepared.repo / "calc.py").write_text("changed")
    elif mutation == "untracked":
        (prepared.repo / "new.txt").write_text("new")
    elif mutation == "head":
        git(prepared.repo, "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-qm", "next")
    elif mutation == "config":
        git(prepared.repo, "config", "core.filemode", "false")
    elif mutation == "missing":
        (prepared.repo / ".git/HEAD").unlink()
    elif mutation == "symlink":
        old = prepared.repo.with_name("old")
        prepared.repo.rename(old)
        prepared.repo.symlink_to(old, target_is_directory=True)
    else:
        with prepared.store._connection() as db:
            value = prepared.preparation.model_dump(mode="json")
            value["status"] = "preparing"
            db.execute("UPDATE repository_preparations SET record_json = ?", (json.dumps(value),))
    response_ = prepared.client.post("/tasks/from-issue", json=payload(prepared))
    assert response_.status_code == 409, response_.text
    assert prepared.client.get("/tasks").json()["items"] == []


def test_missing_preparation(prepared):
    assert prepared.client.post("/tasks/from-issue", json=payload(prepared, preparation_id=str(uuid4()))).status_code == 404


@pytest.mark.parametrize("workdir", ["absent", ".git", ".git/objects", ".GIT", ".Git/objects", "calc.py"])
def test_workdir_must_be_a_real_code_directory(prepared, workdir):
    response_ = prepared.client.post("/tasks/from-issue", json=payload(prepared, workdir=workdir))
    assert response_.status_code == 409


def test_concurrent_retries_make_one_runnable_task(prepared):
    submission = IssueTaskSubmission.model_validate(payload(prepared))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: prepared.app.state.issue_task_service.submit(submission), range(4)))
    assert len({item.task.task_id for item in results}) == 1
    assert sum(not item.replayed for item in results) == 1
    assert len(prepared.client.get("/tasks").json()["items"]) == 1


def test_worker_detects_checkout_changes_before_model_or_docker(prepared, monkeypatch):
    task = prepared.client.post("/tasks/from-issue", json=payload(prepared)).json()["task"]
    (prepared.repo / "calc.py").write_text("changed after submission")
    def forbidden(*args, **kwargs):
        pytest.fail("Must reject changed checkout before Docker or model initialization")
    monkeypatch.setattr("patchloop.worker.LLM", forbidden)
    monkeypatch.setattr("patchloop.worker.subprocess.run", forbidden)
    # RepositoryCheckout uses Popen, not worker's docker subprocess.run.
    record = prepared.store.get(UUID(task["task_id"]))
    with pytest.raises(ValueError, match="local changes"):
        execute_task(record, prepared.tmp / "artifacts")


def complete_repair_review(prepared):
    created = prepared.client.post("/tasks/from-issue", json=payload(prepared)).json()["task"]
    task_id = UUID(created["task_id"])
    llm = ScriptedLLM([
        response(tool_call("edit", "bash", json.dumps({"cmd": "printf 'def value():\\n    return 2\\n' > calc.py"}))),
        SimpleNamespace(content="Fixed.", tool_calls=[]),
    ])
    def executor(record, artifacts):
        return RepairRunner(record.to_repair_task(), artifacts,
                            lambda root, wd: LocalRunner(root / wd)).run(llm)
    assert TaskWorker(prepared.store, executor=executor, log=lambda _: None).run(task_id=task_id) == 1
    task = prepared.store.get(task_id)
    assert task.status == "completed" and task.validation_status == "passed"
    assert task.source.issue == prepared.source
    assert _saved_result(task) == task.result
    review = prepared.client.get(f"/tasks/{task_id}/review").json()
    assert review["can_accept"] is True, review
    accepted = prepared.client.post(f"/tasks/{task_id}/reviews", json={
        "request_id": str(uuid4()), "expected_revision": 0, "decision": "accepted",
        "evidence": review["evidence"],
    })
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["current"]["status"] == "accepted"
    assert (prepared.repo / "calc.py").read_text() == "def value():\n    return 1\n"
    return task


def test_complete_repair_review_and_recovery_with_frozen_context(prepared):
    complete_repair_review(prepared)


def test_failed_transaction_does_not_leave_orphan_task(prepared):
    with prepared.store._connection() as db:
        db.execute("CREATE TRIGGER reject_request BEFORE INSERT ON issue_task_requests BEGIN SELECT RAISE(ABORT, 'test fault'); END")
    result = prepared.client.post("/tasks/from-issue", json=payload(prepared))
    assert result.status_code == 503
    assert prepared.client.get("/tasks").json()["items"] == []


def test_repository_inspection_does_not_hold_database_write_lock(prepared, monkeypatch):
    original = prepared.app.state.repository_service.checkout.inspect
    def inspect(*args, **kwargs):
        # A competing worker write must remain possible during Git inspection.
        with prepared.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
        return original(*args, **kwargs)
    monkeypatch.setattr(prepared.app.state.repository_service.checkout, "inspect", inspect)
    assert prepared.client.post("/tasks/from-issue", json=payload(prepared)).status_code == 201


@pytest.mark.parametrize("field", ["protected_paths", "limits", "verification"])
def test_recovery_rejects_different_execution_contract(prepared, field):
    task = complete_repair_review(prepared)
    path = Path(task.artifacts_dir) / "task.json"
    request = json.loads(path.read_text())
    if field == "protected_paths":
        request[field] = []
    elif field == "limits":
        request[field]["max_model_calls"] += 1
    else:
        request[field]["timeout_seconds"] += 1
    path.write_text(json.dumps(request))
    assert _saved_result(task) is None
