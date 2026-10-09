"""Review regressions using real contracts and isolated, unexecuted repair archives."""

import hashlib
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api.models import ExecutionConfig, TaskRecord
from patchloop.api.store import TaskStore
from patchloop.repair.contracts import BaselineResult, RepairResult, RepairTask, ValidationResult


PATCH = """diff --git a/example/value.py b/example/value.py
--- a/example/value.py
+++ b/example/value.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
ARTIFACTS = ("task.json", "summary.json", "validation.json", "patch.diff")


@pytest.fixture(autouse=True)
def forbid_execution(monkeypatch):
    """A review must never launch a model, repair runner, or external process."""
    from patchloop.llm import LLM
    from patchloop.repair import RepairRunner

    def forbidden(*args, **kwargs):
        pytest.fail("review tests must not execute models, repair runners, or subprocesses")

    monkeypatch.setattr(LLM, "__init__", forbidden)
    monkeypatch.setattr(RepairRunner, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "tasks.sqlite3")) as client:
        yield client


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def archive_task(store, tmp_path, *, status="completed", clean=True, patch=PATCH,
                 baseline_changes=None, validation_changes=None, error="", budget_reminders=False):
    """Follow the worker's persistence lifecycle without running any repair code."""
    task_id = uuid4()
    repo = tmp_path / "repositories" / str(task_id)
    (repo / "example").mkdir(parents=True)
    (repo / "example" / "value.py").write_text("value = 1\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    queued = TaskRecord(
        task_id=task_id, repo_path=str(repo), workdir="example", base_sha="a" * 40,
        goal="修复候选补丁，保留原始仓库", verification_command="python -m pytest -q",
        status="queued", created_at=now, updated_at=now,
        execution=ExecutionConfig(
            image="review-fixture-image", model="offline-fixture", max_model_calls=7,
            command_timeout_seconds=13, verification_timeout_seconds=41,
            budget_reminders=budget_reminders, explicit_finish=True,
        ),
    )
    store.create(queued)
    if status == "queued":
        return store.get(task_id)
    claimed = store.claim(tmp_path / "artifacts", task_id)
    assert claimed is not None and claimed.task_id == task_id
    if status == "running":
        return claimed
    repair_task = claimed.to_repair_task()
    assert isinstance(repair_task, RepairTask)
    root = Path(claimed.artifacts_dir)
    root.mkdir(parents=True)
    profile = dict(profile_id=repair_task.verification.id, command=repair_task.verification.command,
                   base_sha=repair_task.base_sha)
    baseline = BaselineResult(**{
        **profile, "status": "failed", "exit_code": 1, "output": "one failing test",
        **(baseline_changes or {}),
    })
    validation = ValidationResult(**{
        **profile, "status": "passed", "candidate_status": "passed", "exit_code": 0,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
        "baseline": baseline, "output": "one passing test", **(validation_changes or {}),
    })
    result = RepairResult(
        task_id=str(task_id), execution_status=status, validation_status=validation.status,
        patch=patch, validation=validation, artifacts_dir=root, error=error,
        prompt_tokens=123, completion_tokens=45, turns=2, stop_reason="finished",
    )
    write_json(root / "task.json", repair_task.to_dict())
    write_json(root / "summary.json", result.to_dict())
    write_json(root / "validation.json", validation.to_dict())
    (root / "patch.diff").write_text(patch, encoding="utf-8")
    store.finish(task_id, status=status, validation_status=validation.status,
                 error=error, result=result.to_dict())
    if clean:
        store.mark_clean(task_id)
    return store.get(task_id)


@pytest.fixture
def make_task(client, tmp_path):
    def make(**kwargs):
        return archive_task(client.app.state.task_store, tmp_path, **kwargs)
    return make


def state(client, task):
    response = client.get(f"/tasks/{task.task_id}/review")
    assert response.status_code == 200, response.text
    return response.json()


def history(client, task):
    response = client.get(f"/tasks/{task.task_id}/reviews")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["task_id"] == str(task.task_id)
    return value["items"]


def submission(client, task, **changes):
    current = state(client, task)
    assert current["evidence"] is not None, current
    return {
        "request_id": str(uuid4()), "expected_revision": current["revision"],
        "decision": "accepted", "comment": "人工检查通过 ✓", "evidence": current["evidence"],
        **changes,
    }


def post(client, task, body, expected=201):
    response = client.post(f"/tasks/{task.task_id}/reviews", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def file_snapshot(task):
    roots = (Path(task.repo_path), Path(task.artifacts_dir))
    return {str(path): path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}


def test_append_only_decisions_survive_restart_without_changing_execution_or_artifacts(client, make_task):
    task = make_task()
    detail_before = client.get(f"/tasks/{task.task_id}").json()
    list_before = client.get("/tasks").json()
    files_before = file_snapshot(task)
    artifact_responses = {name: client.get(f"/tasks/{task.task_id}/artifacts/{name}").content
                          for name in ARTIFACTS}
    initial = state(client, task)
    assert initial["status"] == "pending" and initial["revision"] == 0
    assert initial["latest_review"] is None and initial["reason"] == ""
    assert initial["can_accept"] is True and initial["can_reject"] is True
    assert history(client, task) == []
    assert detail_before["status"] == "completed"
    assert detail_before["result"]["review_status"] == "pending"
    for name, field in (("task.json", "task_sha256"), ("summary.json", "summary_sha256"),
                        ("validation.json", "validation_sha256"), ("patch.diff", "patch_sha256")):
        assert initial["evidence"][field] == hashlib.sha256(artifact_responses[name]).hexdigest()
    assert initial["evidence"]["base_sha"] == task.base_sha

    first_body = submission(client, task)
    first = post(client, task, first_body)
    assert first["replayed"] is False
    assert first["review"]["revision"] == 1 and first["review"]["decision"] == "accepted"
    assert first["review"]["evidence"] == initial["evidence"]
    assert first["review"]["request_id"] == first_body["request_id"]
    assert first["review"]["comment"] == first_body["comment"]
    assert UUID(first["review"]["review_id"])
    assert datetime.fromisoformat(first["review"]["created_at"]).utcoffset() is not None
    assert first["current"] == state(client, task)
    assert first["current"]["status"] == "accepted"

    second = post(client, task, submission(client, task, decision="rejected", comment="撤回接受，需补充边界测试"))
    assert second["replayed"] is False
    assert second["review"]["revision"] == 2 and second["review"]["decision"] == "rejected"
    assert second["review"]["review_id"] != first["review"]["review_id"]
    assert second["current"]["status"] == "rejected"
    assert second["current"]["latest_review"] == second["review"]
    expected_history = [first["review"], second["review"]]
    assert history(client, task) == expected_history

    with TestClient(create_app(client.app.state.task_store.path)) as restarted:
        assert history(restarted, task) == expected_history
        assert state(restarted, task) == second["current"]
        assert restarted.get(f"/tasks/{task.task_id}").json() == detail_before
        assert restarted.get("/tasks").json() == list_before
        replay = post(restarted, task, first_body, expected=200)
        assert replay["review"] == first["review"] and replay["replayed"] is True
        assert replay["current"] == second["current"]
        assert history(restarted, task) == expected_history
        for name, content in artifact_responses.items():
            response = restarted.get(f"/tasks/{task.task_id}/artifacts/{name}")
            assert response.status_code == 200 and response.content == content
    assert file_snapshot(task) == files_before
    assert client.app.state.task_store.get(task.task_id).model_dump() == task.model_dump()


def test_identical_request_replays_the_same_record(client, make_task):
    task = make_task()
    body = submission(client, task)
    first = post(client, task, body)
    replay = post(client, task, body, expected=200)
    assert replay == {**first, "replayed": True}
    assert history(client, task) == [first["review"]]


@pytest.mark.parametrize("field", ["decision", "comment", "expected_revision", "evidence"])
def test_reusing_request_id_with_changed_content_conflicts(client, make_task, field):
    task = make_task()
    body = submission(client, task)
    first = post(client, task, body)
    changed = json.loads(json.dumps(body))
    replacements = {"decision": "rejected", "comment": "different", "expected_revision": 1,
                    "evidence": {**body["evidence"], "patch_sha256": "b" * 64}}
    changed[field] = replacements[field]
    post(client, task, changed, expected=409)
    assert history(client, task) == [first["review"]]
    assert state(client, task) == first["current"]


def race_reviews(database, task, bodies):
    """Use independent app/store instances against the same SQLite database."""
    barrier = Barrier(len(bodies))
    with ExitStack() as stack:
        clients = [stack.enter_context(TestClient(create_app(database))) for _ in bodies]

        def send(pair):
            client, body = pair
            barrier.wait(timeout=10)
            return client.post(f"/tasks/{task.task_id}/reviews", json=body)

        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            futures = [pool.submit(send, pair) for pair in zip(clients, bodies)]
            return [future.result(timeout=15) for future in futures]


@pytest.mark.parametrize("same_request", [False, True], ids=["same-revision", "same-request-id"])
def test_concurrent_reviews_commit_exactly_once(client, make_task, same_request):
    task = make_task()
    body = submission(client, task)
    bodies = [body if same_request else {**body, "request_id": str(uuid4()),
              "decision": "accepted" if index % 2 else "rejected"} for index in range(6)]
    responses = race_reviews(client.app.state.task_store.path, task, bodies)
    codes = [response.status_code for response in responses]
    assert codes.count(201) == 1, [(r.status_code, r.text) for r in responses]
    assert codes.count(200 if same_request else 409) == 5
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert winner["replayed"] is False
    assert winner["review"]["revision"] == 1
    if same_request:
        for response in responses:
            assert response.json()["review"] == winner["review"]
            assert response.json()["replayed"] is (response.status_code == 200)
    assert history(client, task) == [winner["review"]]
    assert state(client, task) == winner["current"]
    assert client.app.state.task_store.get(task.task_id).result == task.result


def test_request_ids_and_evidence_are_scoped_to_each_task(client, make_task):
    first, second = make_task(), make_task()
    body = submission(client, first)
    reviewed = post(client, first, body)
    post(client, second, body, expected=409)
    assert history(client, second) == []
    second_result = post(client, second, submission(client, second, request_id=body["request_id"]))
    assert second_result["review"]["task_id"] == str(second.task_id)
    assert second_result["review"]["revision"] == 1
    assert second_result["review"]["review_id"] != reviewed["review"]["review_id"]
    assert history(client, first) == [reviewed["review"]]


@pytest.mark.parametrize("revision", [0, 2, 100])
def test_new_request_with_stale_or_future_revision_cannot_overwrite_decision(client, make_task, revision):
    task = make_task()
    first = post(client, task, submission(client, task))
    post(client, task, submission(client, task, expected_revision=revision, decision="rejected"), expected=409)
    assert state(client, task) == first["current"]
    assert history(client, task) == [first["review"]]


@pytest.mark.parametrize("field", ["base_sha", "patch_sha256", "validation_sha256",
                                    "task_sha256", "summary_sha256", "record_sha256"])
def test_every_evidence_component_is_required_to_match(client, make_task, field):
    task = make_task()
    body = submission(client, task)
    body["evidence"][field] = "b" * (40 if field == "base_sha" else 64)
    post(client, task, body, expected=409)
    assert state(client, task)["revision"] == 0
    assert history(client, task) == []


@pytest.mark.parametrize("name", ["task.json", "summary.json", "validation.json"])
def test_changed_evidence_bytes_require_fresh_review_even_when_json_is_equivalent(client, make_task, name):
    task = make_task()
    body = submission(client, task)
    accepted = post(client, task, body)
    path = Path(task.artifacts_dir) / name
    path.write_bytes(path.read_bytes() + b"\n")
    current = state(client, task)
    assert current["status"] == "stale" and current["revision"] == 1
    assert current["can_accept"] is True and current["can_reject"] is True
    assert current["evidence"] != body["evidence"]
    assert current["latest_review"] == accepted["review"]
    post(client, task, {**body, "request_id": str(uuid4()), "expected_revision": 1}, expected=409)
    replay = post(client, task, body, expected=200)
    assert replay["replayed"] is True and replay["current"] == current
    fresh = post(client, task, submission(client, task, decision="rejected"))
    assert fresh["current"]["status"] == "rejected"
    assert history(client, task) == [accepted["review"], fresh["review"]]
    assert client.app.state.task_store.get(task.task_id).result == task.result


@pytest.mark.parametrize("name", ARTIFACTS)
@pytest.mark.parametrize("damage", ["missing", "corrupt", "invalid-utf8", "directory", "outside-symlink"])
def test_missing_or_damaged_evidence_makes_saved_review_stale(client, make_task, tmp_path, name, damage):
    task = make_task()
    body = submission(client, task)
    accepted = post(client, task, body)
    path = Path(task.artifacts_dir) / name
    original = path.read_bytes()
    path.unlink()
    if damage == "corrupt":
        path.write_bytes(b"{broken")
    elif damage == "invalid-utf8":
        path.write_bytes(b"\xff\xfe")
    elif damage == "directory":
        path.mkdir()
    elif damage == "outside-symlink":
        outside = tmp_path / "outside-artifact"
        outside.write_bytes(original)
        path.symlink_to(outside)
    current = state(client, task)
    assert current["status"] == "stale" and current["revision"] == 1
    assert current["evidence"] is None and current["reason"]
    assert current["can_accept"] is False and current["can_reject"] is False
    assert current["latest_review"] == accepted["review"]
    for decision in ("accepted", "rejected"):
        post(client, task, {**body, "request_id": str(uuid4()), "expected_revision": 1,
                            "decision": decision}, expected=409)
    replay = post(client, task, body, expected=200)
    assert replay["review"] == accepted["review"] and replay["current"] == current
    assert history(client, task) == [accepted["review"]]
    assert client.app.state.task_store.get(task.task_id).result == task.result


@pytest.mark.parametrize("name", ["task.json", "summary.json", "validation.json"])
@pytest.mark.parametrize("value", [None, [], 42])
def test_json_artifacts_must_be_objects(client, make_task, name, value):
    task = make_task()
    body = submission(client, task)
    write_json(Path(task.artifacts_dir) / name, value)
    current = state(client, task)
    assert current["evidence"] is None and current["reason"]
    assert current["can_accept"] is False and current["can_reject"] is False
    post(client, task, body, expected=409)
    assert history(client, task) == []


@pytest.mark.parametrize("name", ["task.json", "summary.json", "validation.json"])
def test_json_boolean_and_number_are_not_interchangeable_evidence(client, make_task, name):
    task = make_task(budget_reminders=True)
    body = submission(client, task)
    assert state(client, task)["can_accept"] is True
    path = Path(task.artifacts_dir) / name
    saved = json.loads(path.read_text())
    if name == "task.json":
        assert saved["budget_reminders"] is True
        saved["budget_reminders"] = 1
    else:
        validation = saved["validation"] if name == "summary.json" else saved
        assert type(validation["baseline"]["exit_code"]) is int
        validation["baseline"]["exit_code"] = True
    # Change exactly one archive; the DB result and other files remain authoritative.
    write_json(path, saved)
    current = state(client, task)
    assert current["evidence"] is None
    assert current["can_accept"] is False and current["can_reject"] is False
    assert current["reason"]
    post(client, task, body, expected=409)
    assert history(client, task) == []
    assert client.app.state.task_store.get(task.task_id).result == task.result


@pytest.mark.parametrize("name", ["patch.diff", "summary.json", "validation.json"])
def test_well_formed_artifact_changes_cannot_reuse_saved_validation(client, make_task, name):
    task = make_task()
    body = submission(client, task)
    accepted = post(client, task, body)
    path = Path(task.artifacts_dir) / name
    if name == "patch.diff":
        path.write_text(PATCH.replace("+value = 2", "+value = 999"), encoding="utf-8")
    else:
        saved = json.loads(path.read_text())
        if name == "summary.json":
            saved["prompt_tokens"] += 1
        else:
            saved["candidate"]["output"] = "a different validation run"
        write_json(path, saved)
    current = state(client, task)
    assert current["status"] == "stale" and current["revision"] == 1
    assert current["evidence"] is None and current["reason"]
    assert current["can_accept"] is False and current["can_reject"] is False
    for decision in ("accepted", "rejected"):
        post(client, task, {**body, "request_id": str(uuid4()), "expected_revision": 1,
                            "decision": decision}, expected=409)
    assert history(client, task) == [accepted["review"]]
    assert client.app.state.task_store.get(task.task_id).result == task.result


@pytest.mark.parametrize(("field", "replacement"), [
    ("task_id", str(UUID(int=42))), ("repo_path", "/different/repo"), ("workdir", "."),
    ("base_sha", "b" * 40), ("goal", "a different goal"), ("issue", "different issue"),
    ("allowed_paths", ["elsewhere/*"]), ("protected_paths", ["example/*"]),
    ("verification.id", "different-profile"), ("verification.command", "true"),
    ("verification.timeout_seconds", 42), ("limits.max_model_calls", 8),
    ("limits.command_timeout_seconds", 14), ("limits.task_timeout_seconds", 601),
    ("budget_reminders", True), ("explicit_finish", False), ("unexpected", "injected"),
])
def test_all_saved_task_fields_must_match_frozen_contract(client, make_task, field, replacement):
    task = make_task()
    body = submission(client, task)
    accepted = post(client, task, body)
    path = Path(task.artifacts_dir) / "task.json"
    saved = json.loads(path.read_text())
    target = saved
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = replacement
    write_json(path, saved)
    current = state(client, task)
    assert current["status"] == "stale", field
    assert current["evidence"] is None, field
    assert current["can_accept"] is False and current["can_reject"] is False
    post(client, task, {**body, "request_id": str(uuid4()), "expected_revision": 1}, expected=409)
    assert history(client, task) == [accepted["review"]]


@pytest.mark.parametrize("blocked", ["queued", "running", "cleanup_pending", "empty", "whitespace"])
def test_unfinished_unclean_or_empty_patch_tasks_cannot_be_reviewed(client, make_task, blocked):
    eligible = make_task()
    valid_body = submission(client, eligible)
    options = {
        "queued": {"status": "queued"}, "running": {"status": "running"},
        "cleanup_pending": {"clean": False}, "empty": {"patch": ""}, "whitespace": {"patch": " \n\t"},
    }
    task = make_task(**options[blocked])
    current = state(client, task)
    assert current["status"] == "pending" and current["revision"] == 0
    assert current["evidence"] is None and current["reason"]
    assert current["can_accept"] is False and current["can_reject"] is False
    for decision in ("accepted", "rejected"):
        post(client, task, {**valid_body, "decision": decision}, expected=409)
    assert history(client, task) == []
    if blocked == "cleanup_pending":
        client.app.state.task_store.mark_clean(task.task_id)
        assert state(client, task)["can_accept"] is True
        assert post(client, task, submission(client, task))["current"]["status"] == "accepted"


@pytest.mark.parametrize("options", [
    {"validation_changes": {"status": "failed", "candidate_status": "failed", "exit_code": 1}},
    {"validation_changes": {"status": "inconclusive", "candidate_status": "inconclusive", "exit_code": None}},
    {"validation_changes": {"status": "not_run", "candidate_status": "not_run", "exit_code": None}},
    {"baseline_changes": {"status": "passed", "exit_code": 0}},
    {"baseline_changes": {"status": "inconclusive", "exit_code": None}},
    {"baseline_changes": {"status": "error", "error": "baseline crashed"}},
    {"baseline_changes": {"exit_code": 0}},
    {"baseline_changes": {"exit_code": True}},
    {"baseline_changes": {"error": "baseline timed out"}},
    {"validation_changes": {"baseline": None}},
    {"validation_changes": {"candidate_status": "failed"}},
    {"validation_changes": {"candidate_status": "not_run"}},
    {"validation_changes": {"candidate_error": "candidate crashed"}},
    {"validation_changes": {"exit_code": 1}},
    {"validation_changes": {"exit_code": False}},
    {"validation_changes": {"error": "validation timed out"}},
    {"validation_changes": {"status": "failed"}},
    {"status": "error", "error": "execution failed"},
    {"error": "execution reported an error"},
], ids=["candidate-failed", "inconclusive", "not-run", "baseline-already-passed",
        "baseline-inconclusive", "baseline-error", "baseline-exit-mismatch", "baseline-bool-exit",
        "baseline-has-error", "missing-baseline", "candidate-status-mismatch", "candidate-not-run",
        "candidate-has-error", "candidate-exit-mismatch", "candidate-bool-exit", "validation-error",
        "overall-validation-failed", "execution-error", "completed-with-error"])
def test_unproven_candidate_can_be_rejected_but_never_accepted(client, make_task, options):
    task = make_task(**options)
    current = state(client, task)
    assert current["status"] == "pending" and current["reason"]
    assert current["evidence"] is not None
    assert current["can_accept"] is False and current["can_reject"] is True
    body = submission(client, task)
    post(client, task, body, expected=409)
    assert history(client, task) == []
    # A failed acceptance must not reserve the request id.
    rejected = post(client, task, {**body, "decision": "rejected"})
    assert rejected["current"]["status"] == "rejected"
    assert rejected["review"]["revision"] == 1
    assert client.app.state.task_store.get(task.task_id).result == task.result


@pytest.mark.parametrize("status", ["completed", "interrupted"])
def test_acceptance_is_independent_of_execution_completion(client, make_task, status):
    task = make_task(status=status)
    assert state(client, task)["status"] == "pending"
    assert state(client, task)["can_accept"] is True
    accepted = post(client, task, submission(client, task))
    assert accepted["current"]["status"] == "accepted"
    saved = client.get(f"/tasks/{task.task_id}").json()
    assert saved["status"] == status and saved["validation_status"] == "passed"
    assert saved["result"] == task.result
    assert saved["result"]["review_status"] == "pending"


@pytest.mark.parametrize(("field", "value"), [
    ("base_sha", "b" * 40), ("command", "true"), ("profile_id", "different-profile"),
])
def test_baseline_from_a_different_verification_context_is_unreviewable(client, make_task, field, value):
    task = make_task(baseline_changes={field: value})
    current = state(client, task)
    assert current["evidence"] is None and current["reason"]
    assert current["can_accept"] is False and current["can_reject"] is False
    assert history(client, task) == []


@pytest.mark.parametrize("endpoint", ["review", "reviews"])
def test_missing_task_review_endpoints_return_404(client, endpoint):
    response = client.get(f"/tasks/{uuid4()}/{endpoint}")
    assert response.status_code == 404


def test_post_review_for_missing_task_returns_404(client, make_task):
    task = make_task()
    response = client.post(f"/tasks/{uuid4()}/reviews", json=submission(client, task))
    assert response.status_code == 404
    assert history(client, task) == []


@pytest.mark.parametrize("endpoint", ["review", "reviews"])
def test_malformed_task_id_returns_422(client, endpoint):
    assert client.get(f"/tasks/not-a-uuid/{endpoint}").status_code == 422


@pytest.mark.parametrize("changes", [
    {"request_id": "invalid"}, {"expected_revision": -1}, {"expected_revision": True},
    {"expected_revision": 0.0}, {"expected_revision": "0"}, {"decision": "pending"},
    {"decision": "stale"}, {"comment": "x" * 4001}, {"comment": "bad\x00comment"},
    {"comment": None}, {"evidence": None}, {"evidence": {}}, {"unexpected": True},
])
def test_malformed_review_body_returns_422_without_recording_review(client, make_task, changes):
    task = make_task()
    body = submission(client, task)
    post(client, task, {**body, **changes}, expected=422)
    assert history(client, task) == []
    assert state(client, task)["revision"] == 0


@pytest.mark.parametrize("field", ["request_id", "expected_revision", "decision", "evidence"])
def test_required_review_fields_cannot_be_omitted(client, make_task, field):
    task = make_task()
    body = submission(client, task)
    del body[field]
    post(client, task, body, expected=422)
    assert history(client, task) == []


@pytest.mark.parametrize("changes", [
    {"base_sha": "HEAD"}, {"patch_sha256": "x" * 64}, {"validation_sha256": "a" * 63},
    {"task_sha256": None}, {"summary_sha256": "A" * 64}, {"record_sha256": ""}, {"extra": "x"},
])
def test_malformed_evidence_returns_422(client, make_task, changes):
    task = make_task()
    body = submission(client, task)
    body["evidence"].update(changes)
    post(client, task, body, expected=422)
    assert history(client, task) == []


def test_malformed_json_and_post_task_id_return_422(client, make_task):
    task = make_task()
    response = client.post(f"/tasks/{task.task_id}/reviews", content="{",
                           headers={"content-type": "application/json"})
    assert response.status_code == 422
    response = client.post("/tasks/not-a-uuid/reviews", json=submission(client, task))
    assert response.status_code == 422
    assert history(client, task) == []


def test_v4_migration_preserves_existing_rows_results_and_is_reentrant(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    # Build the old schema directly: downgrading a new DB would leave review tables behind.
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, workdir TEXT NOT NULL,
                base_sha TEXT NOT NULL, goal TEXT NOT NULL, verification_command TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'error')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                execution_json TEXT, result_json TEXT, artifacts_dir TEXT,
                started_at TEXT, finished_at TEXT, error TEXT NOT NULL DEFAULT '',
                validation_status TEXT NOT NULL DEFAULT 'not_run', cleanup_pending INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX tasks_created_order ON tasks(created_at DESC, task_id DESC);
            CREATE INDEX tasks_status_created_order ON tasks(status, created_at DESC, task_id DESC);
            PRAGMA user_version=4;
        """)
    class LegacyStore(TaskStore):
        def create(self, record):
            # Reproduce the v4 writer before the path-policy column existed.
            with self._connection() as connection:
                connection.execute("""
                    INSERT INTO tasks (task_id, repo_path, workdir, base_sha, goal, verification_command,
                                       status, created_at, updated_at, execution_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (str(record.task_id), record.repo_path, record.workdir, record.base_sha, record.goal,
                      record.verification_command, record.status, record.created_at.isoformat(), record.updated_at.isoformat(),
                      record.execution.model_dump_json() if record.execution else None))

    old_store = LegacyStore(database)
    completed = archive_task(old_store, tmp_path)
    unconfigured = completed.model_copy(update={
        "task_id": uuid4(), "status": "queued", "execution": None, "result": None,
        "artifacts_dir": None, "started_at": None, "finished_at": None, "validation_status": "not_run",
    })
    old_store.create(unconfigured)
    expected_records = {task.task_id: old_store.get(task.task_id).model_dump(mode="json")
                        for task in (completed, unconfigured)}
    files_before = file_snapshot(completed)
    with sqlite3.connect(database) as connection:
        old_rows = connection.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'task_reviews'").fetchone() is None

    migrated = TaskStore(database)
    migrated.initialize()
    migrated.initialize()
    with TestClient(create_app(database)) as client:
        for task_id, expected in expected_records.items():
            assert client.get(f"/tasks/{task_id}").json() == expected
        assert state(client, completed)["status"] == "pending"
        assert state(client, completed)["can_accept"] is True
        assert state(client, unconfigured)["can_accept"] is False
        assert history(client, completed) == history(client, unconfigured) == []
        first_body = submission(client, completed)
        accepted = post(client, completed, first_body)
    migrated.initialize()
    with TestClient(create_app(database)) as restarted:
        assert history(restarted, completed) == [accepted["review"]]
        assert post(restarted, completed, first_body, expected=200)["review"] == accepted["review"]
        assert restarted.get(f"/tasks/{completed.task_id}").json() == expected_records[completed.task_id]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        upgraded_rows = connection.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        assert [row[:-2] for row in upgraded_rows] == old_rows
        assert all(row[-2:] == (None, None) for row in upgraded_rows)
        assert all(row[-1] is None for row in upgraded_rows)
        assert connection.execute("SELECT count(*) FROM task_reviews").fetchone()[0] == 1
    assert file_snapshot(completed) == files_before


@pytest.mark.parametrize("change_after_read", [1, 2], ids=["during-snapshot", "before-save"])
def test_evidence_change_during_submission_does_not_consume_revision_or_request_id(
    client, make_task, monkeypatch, change_after_read,
):
    import patchloop.api.review as review_module

    task = make_task()
    body = submission(client, task)
    path = Path(task.artifacts_dir) / "validation.json"
    original = path.read_bytes()
    read_artifacts = review_module._read_artifacts
    calls = 0

    def changed_while_reading(root):
        nonlocal calls
        contents = read_artifacts(root)
        calls += 1
        if calls == change_after_read:
            path.write_bytes(original + b"\n")
        return contents

    with monkeypatch.context() as scoped:
        scoped.setattr(review_module, "_read_artifacts", changed_while_reading)
        post(client, task, body, expected=409)
    assert history(client, task) == []
    assert state(client, task)["revision"] == 0
    path.write_bytes(original)
    saved = post(client, task, body)
    assert saved["review"]["revision"] == 1
    assert saved["replayed"] is False
