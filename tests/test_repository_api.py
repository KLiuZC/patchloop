"""HTTP/service regressions using temporary SQLite data and fake Git operations."""

import fcntl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api.github import GitHubReadError
from patchloop.api.models import TaskRecord
from patchloop.api.repository_git import CheckoutError
from patchloop.api.repository_models import RepositoryPreparation, ResolvedRepository
from patchloop.api.repository_store import RepositoryPreparationStore
from patchloop.api.source_store import IssueSourceStore
from patchloop.api.store import TaskStore
from test_issue_source_store import make_issue


SHA = "a" * 40
RESOLVED_AT = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
CONTENTS = "original checkout\n"


def unexpected_external_call(*args, **kwargs):
    pytest.fail("repository API tests must not use a network, model, worker, or subprocess")


def fake_resolve(source, ref):
    return ResolvedRepository(
        repository=source.repository, ref=ref or "main", base_sha=SHA,
        resolved_at=RESOLVED_AT,
    )


def fake_prepare(path, clone_url, base_sha):
    path.mkdir(exist_ok=False)
    (path / "README.md").write_text(CONTENTS)


def fake_inspect(path, base_sha):
    readme = path / "README.md"
    if not readme.is_file():
        return False, "Prepared repository is missing"
    if readme.read_text() != CONTENTS:
        return False, "Prepared working tree has local changes"
    return True, ""


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    import patchloop.llm
    import patchloop.repair.runner
    import subprocess

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", unexpected_external_call)
    monkeypatch.setattr(patchloop.llm, "LLM", unexpected_external_call)
    monkeypatch.setattr(patchloop.repair.runner.RepairRunner, "run", unexpected_external_call)

    def build():
        app = create_app(
            tmp_path / "data" / "tasks.sqlite3",
            repositories_root=tmp_path / "repositories",
            deliveries_root=tmp_path / "deliveries",
            github_transport=httpx.MockTransport(unexpected_external_call),
        )
        service = app.state.repository_service
        monkeypatch.setattr(service.resolver, "resolve", Mock(side_effect=fake_resolve))
        monkeypatch.setattr(service.checkout, "prepare", Mock(side_effect=fake_prepare))
        monkeypatch.setattr(service.checkout, "inspect", Mock(side_effect=fake_inspect))
        return app

    return build


def original_rows(tasks):
    with tasks._connection() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("github_issue_sources", "tasks", "task_reviews", "task_deliveries")
        }


@pytest.fixture
def seeded(app_factory, tmp_path):
    app = app_factory()
    tasks = app.state.task_store
    tasks.initialize()
    sources = IssueSourceStore(tasks)
    source, snapshot = make_issue()
    other_source, other_snapshot = make_issue(number=43)
    sources.save(source, snapshot)
    sources.save(other_source, other_snapshot)
    original = tmp_path / "original-repository"
    original.mkdir()
    (original / "README.md").write_text("existing task's repository\n")
    tasks.create(TaskRecord(
        task_id=uuid4(), repo_path=str(original), workdir=".", base_sha="b" * 40,
        goal="Preserve this existing task", verification_command="must-not-run",
        status="completed", created_at=RESOLVED_AT, updated_at=RESOLVED_AT,
        finished_at=RESOLVED_AT, result={"patch": "existing result"}, validation_status="passed",
    ))
    before = original_rows(tasks)
    yield SimpleNamespace(
        app=app, service=app.state.repository_service, tasks=tasks,
        source=source, other_source=other_source, original=original,
    )
    # Every success, failure, recovery and replay must leave imported inputs and
    # existing task rows intact, and must never create an executable task.
    assert original_rows(tasks) == before
    assert (original / "README.md").read_text() == "existing task's repository\n"


@pytest.fixture
def client(seeded):
    with TestClient(seeded.app) as client:
        yield client


def payload(seeded, **overrides):
    return {"request_id": str(uuid4()), "source_id": str(seeded.source.source_id), **overrides}


def preparation_rows(seeded):
    with seeded.tasks._connection() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM repository_preparations ORDER BY preparation_id",
        )]


def seed_interrupted(seeded, *, pinned=True):
    preparation_id = uuid4()
    record = RepositoryPreparation(
        preparation_id=preparation_id, request_id=uuid4(), source_id=seeded.source.source_id,
        repository=seeded.source.repository, requested_ref=None,
        resolved_ref="main" if pinned else None, base_sha=SHA if pinned else None,
        resolved_at=RESOLVED_AT if pinned else None,
        clone_url="https://github.com/octocat/hello-world.git",
        repo_path=str(seeded.service.root / str(preparation_id) / "repo"),
        created_at=RESOLVED_AT, updated_at=RESOLVED_AT,
    )
    seeded.service.store.create(record)
    return record


def test_prepare_persists_request_then_resolved_sha_before_checkout(client, seeded):
    body = payload(seeded)
    # Read through independent connections: an in-memory record or uncommitted
    # write does not satisfy the requirement to pin before downloading.
    observer = RepositoryPreparationStore(TaskStore(seeded.tasks.path))

    def resolve(source, ref):
        saved = observer.get_for_request(UUID(body["request_id"]))
        assert saved.status == "preparing"
        assert saved.source_id == source.source_id
        assert saved.base_sha is None and saved.resolved_at is None
        assert not Path(saved.repo_path).exists()
        return fake_resolve(source, ref)

    def prepare(path, url, sha):
        saved = observer.get_for_request(UUID(body["request_id"]))
        assert saved.status == "preparing" and saved.finished_at is None
        assert saved.resolved_ref == "main" and saved.base_sha == sha == SHA
        assert saved.resolved_at == RESOLVED_AT
        assert saved.repo_path == str(path) and saved.clone_url == url
        fake_prepare(path, url, sha)

    seeded.service.resolver.resolve.side_effect = resolve
    seeded.service.checkout.prepare.side_effect = prepare
    response = client.post("/repositories/prepare", json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert set(result) == {"preparation", "replayed"} and result["replayed"] is False
    record = result["preparation"]
    assert record["request_id"] == body["request_id"] and record["source_id"] == body["source_id"]
    assert record["status"] == "ready" and record["requested_ref"] is None
    assert record["error"] == "" and record["error_status"] is None
    assert record["finished_at"] is not None
    path = seeded.service.root / record["preparation_id"] / "repo"
    assert record["repo_path"] == str(path) and path.is_dir()
    assert record["clone_url"] == "https://github.com/octocat/hello-world.git"
    assert response.headers["location"] == f"/repositories/{record['preparation_id']}"
    state = client.get(response.headers["location"])
    assert state.status_code == 200
    assert state.json() == {"preparation": record, "checkout_intact": True, "reason": ""}
    seeded.service.resolver.resolve.assert_called_once_with(seeded.source, None)
    seeded.service.checkout.prepare.assert_called_once_with(path, record["clone_url"], SHA)
    seeded.service.checkout.inspect.assert_called_once_with(path, SHA)


@pytest.mark.parametrize("ref", ["feature/fix-parser", SHA], ids=["branch-with-slash", "full-sha"])
def test_explicit_ref_is_passed_to_resolver_and_saved(client, seeded, ref):
    response = client.post("/repositories/prepare", json=payload(seeded, ref=ref))
    assert response.status_code == 201, response.text
    record = response.json()["preparation"]
    assert record["requested_ref"] == record["resolved_ref"] == ref
    assert record["base_sha"] == SHA
    seeded.service.resolver.resolve.assert_called_once_with(seeded.source, ref)


def test_uuid_replay_preserves_entire_record_and_does_not_resolve_again(client, seeded):
    body = payload(seeded)
    first = client.post("/repositories/prepare", json=body)
    assert first.status_code == 201
    before = preparation_rows(seeded)
    # UUID spelling and an explicit null are the same request, not new work.
    equivalent = {
        "request_id": UUID(body["request_id"]).hex.upper(),
        "source_id": "{" + body["source_id"].upper() + "}", "ref": None,
    }
    replay = client.post("/repositories/prepare", json=equivalent)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {"preparation": first.json()["preparation"], "replayed": True}
    assert replay.headers["location"] == first.headers["location"]
    assert preparation_rows(seeded) == before
    assert seeded.service.resolver.resolve.call_count == seeded.service.checkout.prepare.call_count == 1
    seeded.service.checkout.inspect.assert_not_called()


@pytest.mark.parametrize("changed", ["source", "ref"])
def test_request_key_with_different_inputs_returns_409_without_mutation(client, seeded, changed):
    body = payload(seeded)
    first = client.post("/repositories/prepare", json=body)
    assert first.status_code == 201
    before = preparation_rows(seeded)
    conflict = dict(body)
    if changed == "source":
        conflict["source_id"] = str(seeded.other_source.source_id)
    else:
        # Even an explicitly named default branch is different from an omitted ref.
        conflict["ref"] = "main"
    response = client.post("/repositories/prepare", json=conflict)
    assert response.status_code == 409 and "request_id" in response.json()["detail"]
    assert preparation_rows(seeded) == before
    assert seeded.service.resolver.resolve.call_count == seeded.service.checkout.prepare.call_count == 1


def test_unknown_source_and_preparation_return_404_without_consuming_key(client, seeded):
    body = payload(seeded, source_id=str(uuid4()))
    assert client.post("/repositories/prepare", json=body).status_code == 404
    assert client.get(f"/repositories/{uuid4()}").status_code == 404
    assert preparation_rows(seeded) == []
    seeded.service.resolver.resolve.assert_not_called()
    seeded.service.checkout.prepare.assert_not_called()
    body["source_id"] = str(seeded.source.source_id)
    assert client.post("/repositories/prepare", json=body).status_code == 201


def test_invalid_ids_refs_and_extra_fields_return_422_before_service_work(client, seeded):
    for change in (
        {"request_id": "not-a-uuid"}, {"source_id": "not-a-uuid"},
        {"ref": ""}, {"ref": "--upload-pack=malicious"}, {"ref": "HEAD~1"},
        {"ref": "../outside"}, {"ref": 17}, {"repo_path": str(seeded.original)},
    ):
        response = client.post("/repositories/prepare", json=payload(seeded, **change))
        assert response.status_code == 422, (change, response.text)
    assert client.post("/repositories/prepare", json={"source_id": str(seeded.source.source_id)}).status_code == 422
    assert client.get("/repositories/not-a-uuid").status_code == 422
    assert preparation_rows(seeded) == []
    seeded.service.resolver.resolve.assert_not_called()
    seeded.service.checkout.prepare.assert_not_called()


@pytest.mark.parametrize("stage,error,status", [
    ("resolve", GitHubReadError(429, "rate limited", retry_after=30), 429),
    ("resolve", GitHubReadError(504, "resolution timed out"), 504),
    ("prepare", CheckoutError("checkout rejected"), 502),
    ("prepare", OSError("disk is full"), 502),
], ids=["rate-limit", "resolve-timeout", "checkout-error", "storage-error"])
def test_failure_is_saved_with_status_and_replays_without_retry(client, seeded, stage, error, status):
    operation = seeded.service.resolver.resolve if stage == "resolve" else seeded.service.checkout.prepare
    operation.side_effect = error
    body = payload(seeded)
    response = client.post("/repositories/prepare", json=body)
    assert response.status_code == 201, response.text
    record = response.json()["preparation"]
    assert response.json()["replayed"] is False
    assert record["status"] == "failed" and record["error_status"] == status
    assert record["error"] == str(error) and record["finished_at"] is not None
    assert record["base_sha"] == (SHA if stage == "prepare" else None)
    persisted = RepositoryPreparationStore(TaskStore(seeded.tasks.path)).get(UUID(record["preparation_id"]))
    assert persisted.model_dump(mode="json") == record
    before = preparation_rows(seeded)
    replay = client.post("/repositories/prepare", json=body)
    assert replay.status_code == 200
    assert replay.json() == {"preparation": record, "replayed": True}
    state = client.get(response.headers["location"])
    assert state.status_code == 200
    assert state.json() == {"preparation": record, "checkout_intact": None, "reason": str(error)}
    assert preparation_rows(seeded) == before
    assert seeded.service.resolver.resolve.call_count == 1
    assert seeded.service.checkout.prepare.call_count == (stage == "prepare")
    seeded.service.checkout.inspect.assert_not_called()


def test_new_request_key_gets_distinct_preparation_and_checkout(client, seeded):
    first = client.post("/repositories/prepare", json=payload(seeded))
    second = client.post("/repositories/prepare", json=payload(seeded))
    assert first.status_code == second.status_code == 201
    a, b = first.json()["preparation"], second.json()["preparation"]
    assert first.json()["replayed"] is second.json()["replayed"] is False
    assert a["preparation_id"] != b["preparation_id"] and a["repo_path"] != b["repo_path"]
    for record in (a, b):
        assert Path(record["repo_path"]) == seeded.service.root / record["preparation_id"] / "repo"
        assert (Path(record["repo_path"]) / "README.md").read_text() == CONTENTS
    assert client.get(first.headers["location"]).json()["preparation"] == a
    assert len(preparation_rows(seeded)) == 2
    assert seeded.service.resolver.resolve.call_count == seeded.service.checkout.prepare.call_count == 2


def test_new_key_after_partial_checkout_failure_preserves_old_path(client, seeded):
    def fail_after_writing(path, url, sha):
        fake_prepare(path, url, sha)
        (path / "README.md").write_text("partial checkout\n")
        raise CheckoutError("fetch interrupted")

    seeded.service.checkout.prepare.side_effect = fail_after_writing
    first = client.post("/repositories/prepare", json=payload(seeded))
    assert first.status_code == 201
    failed = first.json()["preparation"]
    assert failed["status"] == "failed"
    seeded.service.checkout.prepare.side_effect = fake_prepare
    second = client.post("/repositories/prepare", json=payload(seeded))
    assert second.status_code == 201
    ready = second.json()["preparation"]
    assert ready["status"] == "ready" and ready["preparation_id"] != failed["preparation_id"]
    assert ready["repo_path"] != failed["repo_path"]
    assert (Path(failed["repo_path"]) / "README.md").read_text() == "partial checkout\n"
    assert client.get(first.headers["location"]).json()["preparation"] == failed


def test_restart_preserves_ready_record_and_reports_local_edits(seeded, app_factory):
    body = payload(seeded)
    with TestClient(seeded.app) as first:
        response = first.post("/repositories/prepare", json=body)
        assert response.status_code == 201
        record = response.json()["preparation"]
        location = response.headers["location"]
    before = preparation_rows(seeded)
    restarted = app_factory()
    service = restarted.state.repository_service
    with TestClient(restarted) as client:
        intact = client.get(location)
        assert intact.status_code == 200 and intact.json()["checkout_intact"] is True
        readme = Path(record["repo_path"]) / "README.md"
        readme.write_text("local edit\n")
        changed = client.get(location)
        assert changed.status_code == 200
        assert changed.json() == {
            "preparation": record, "checkout_intact": False,
            "reason": "Prepared working tree has local changes",
        }
        replay = client.post("/repositories/prepare", json=body)
        assert replay.status_code == 200
        assert replay.json() == {"preparation": record, "replayed": True}
        assert readme.read_text() == "local edit\n"
    assert preparation_rows(seeded) == before
    service.resolver.resolve.assert_not_called()
    service.checkout.prepare.assert_not_called()
    assert service.checkout.inspect.call_count == 2


def test_startup_recovers_interrupted_preparation_without_retrying(seeded, app_factory):
    interrupted = seed_interrupted(seeded)
    path = Path(interrupted.repo_path)
    path.mkdir(parents=True)
    (path / "partial").write_text("keep partial files")
    restarted = app_factory()
    with TestClient(restarted) as client:
        response = client.get(f"/repositories/{interrupted.preparation_id}")
        assert response.status_code == 200
        state = response.json()
        failed = state["preparation"]
        assert failed["status"] == "failed" and failed["error_status"] == 503
        assert "interrupt" in failed["error"].lower() and "request_id" in failed["error"]
        assert failed["finished_at"] is not None and state["checkout_intact"] is None
        assert state["reason"] == failed["error"]
        assert failed["base_sha"] == SHA and failed["repo_path"] == str(path)
        assert failed["created_at"] == interrupted.model_dump(mode="json")["created_at"]
        replay = client.post("/repositories/prepare", json=payload(seeded, request_id=str(interrupted.request_id)))
        assert replay.status_code == 200 and replay.json() == {"preparation": failed, "replayed": True}
    # Recovery itself is idempotent, including its error and timestamps.
    with TestClient(app_factory()) as again:
        assert again.get(f"/repositories/{interrupted.preparation_id}").json() == state
    assert (path / "partial").read_text() == "keep partial files"
    restarted.state.repository_service.resolver.resolve.assert_not_called()
    restarted.state.repository_service.checkout.prepare.assert_not_called()
    restarted.state.repository_service.checkout.inspect.assert_not_called()


def test_startup_does_not_recover_preparing_record_while_flock_is_active(seeded, app_factory):
    interrupted = seed_interrupted(seeded, pinned=False)
    lock_path = seeded.tasks.path.with_name(seeded.tasks.path.name + ".repositories.lock")
    before = preparation_rows(seeded)
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            restarted = app_factory()
            with TestClient(restarted) as client:
                response = client.get(f"/repositories/{interrupted.preparation_id}")
                assert response.status_code == 200
                assert response.json() == {
                    "preparation": interrupted.model_dump(mode="json"), "checkout_intact": None, "reason": "",
                }
                replay = client.post("/repositories/prepare", json=payload(seeded, request_id=str(interrupted.request_id)))
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                assert replay.json()["preparation"] == interrupted.model_dump(mode="json")
                assert preparation_rows(seeded) == before
            restarted.state.repository_service.resolver.resolve.assert_not_called()
            restarted.state.repository_service.checkout.prepare.assert_not_called()
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    with TestClient(app_factory()) as client:
        assert client.get(f"/repositories/{interrupted.preparation_id}").json()["preparation"]["status"] == "failed"


def test_same_key_replay_recovers_orphan_created_after_startup_without_network(client, seeded):
    # Seed after the lifespan hook ran, as if another API process died while
    # preparing. Only replay itself can recover this record in the running app.
    orphan = seed_interrupted(seeded)
    body = payload(seeded, request_id=str(orphan.request_id))
    before = preparation_rows(seeded)
    conflict = client.post("/repositories/prepare", json={**body, "ref": "different"})
    assert conflict.status_code == 409
    assert preparation_rows(seeded) == before

    replay = client.post("/repositories/prepare", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    failed = replay.json()["preparation"]
    assert failed["status"] == "failed" and failed["error_status"] == 503
    assert "interrupt" in failed["error"].lower() and failed["finished_at"] is not None
    original = orphan.model_dump(mode="json")
    for field in ("preparation_id", "request_id", "source_id", "repo_path", "base_sha",
                  "resolved_ref", "resolved_at", "created_at"):
        assert failed[field] == original[field]
    assert replay.headers["location"] == f"/repositories/{orphan.preparation_id}"
    assert client.get(replay.headers["location"]).json() == {
        "preparation": failed, "checkout_intact": None, "reason": failed["error"],
    }
    persisted = preparation_rows(seeded)
    assert client.post("/repositories/prepare", json=body).json() == replay.json()
    assert preparation_rows(seeded) == persisted and len(persisted) == 1
    seeded.service.resolver.resolve.assert_not_called()
    seeded.service.checkout.prepare.assert_not_called()
    seeded.service.checkout.inspect.assert_not_called()


@pytest.mark.parametrize("pause_at", ["resolve", "prepare"])
def test_concurrent_apps_replay_same_key_and_reject_busy_key_without_duplicate_work(
    client, seeded, app_factory, pause_at,
):
    entered, release = Event(), Event()
    body, competing = payload(seeded), payload(seeded)
    operation = seeded.service.resolver.resolve if pause_at == "resolve" else seeded.service.checkout.prepare
    original = operation.side_effect

    def block(*args):
        entered.set()
        assert release.wait(timeout=10), "test did not release the preparation"
        return original(*args)

    operation.side_effect = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/repositories/prepare", json=body)
        try:
            assert entered.wait(timeout=5), "preparation did not reach the blocked operation"
            second = app_factory()
            with TestClient(second) as other:
                replay = other.post("/repositories/prepare", json=body)
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                preparing = replay.json()["preparation"]
                assert preparing["status"] == "preparing"
                assert preparing["base_sha"] == (SHA if pause_at == "prepare" else None)
                state = other.get(replay.headers["location"])
                assert state.json() == {"preparation": preparing, "checkout_intact": None, "reason": ""}
                busy = other.post("/repositories/prepare", json=competing)
                assert busy.status_code == 409, busy.text
                assert seeded.service.store.get_for_request(UUID(competing["request_id"])) is None
                assert other.post("/repositories/prepare", json={**body, "ref": "other"}).status_code == 409
                assert len(preparation_rows(seeded)) == 1
                second.state.repository_service.resolver.resolve.assert_not_called()
                second.state.repository_service.checkout.prepare.assert_not_called()
                release.set()
                completed = future.result(timeout=10)
                assert completed.status_code == 201
                ready = completed.json()["preparation"]
                assert ready["status"] == "ready" and ready["preparation_id"] == preparing["preparation_id"]
                assert other.post("/repositories/prepare", json=body).json() == {"preparation": ready, "replayed": True}
                # A busy response did not consume the competing request key.
                assert other.post("/repositories/prepare", json=competing).status_code == 201
        finally:
            release.set()
            future.result(timeout=10)
    assert seeded.service.resolver.resolve.call_count == seeded.service.checkout.prepare.call_count == 1
    assert second.state.repository_service.checkout.prepare.call_count == 1


@pytest.mark.parametrize("obstruction", ["directory", "symlink"])
def test_existing_preparation_path_is_never_reused_or_followed(client, seeded, monkeypatch, obstruction):
    preparation_id = uuid4()
    monkeypatch.setattr("patchloop.api.repositories.uuid4", lambda: preparation_id)
    seeded.service.root.mkdir()
    parent = seeded.service.root / str(preparation_id)
    if obstruction == "directory":
        parent.mkdir()
        protected = parent / "repo"
        protected.mkdir()
        (protected / "README.md").write_text("do not overwrite")
    else:
        parent.symlink_to(seeded.original, target_is_directory=True)
        protected = seeded.original
    before = {p.name: p.read_bytes() for p in protected.iterdir()}
    body = payload(seeded)
    response = client.post("/repositories/prepare", json=body)
    assert response.status_code == 201, response.text
    failed = response.json()["preparation"]
    assert failed["status"] == "failed" and failed["error_status"] == 502
    assert failed["base_sha"] == SHA and failed["error"]
    assert {p.name: p.read_bytes() for p in protected.iterdir()} == before
    assert parent.is_symlink() == (obstruction == "symlink")
    replay = client.post("/repositories/prepare", json=body)
    assert replay.status_code == 200 and replay.json() == {"preparation": failed, "replayed": True}
    seeded.service.checkout.prepare.assert_not_called()


def test_replaced_storage_root_symlink_cannot_write_to_original_repository(client, seeded):
    # The replacement happens after the service selected its canonical root.
    seeded.service.root.symlink_to(seeded.original, target_is_directory=True)
    response = client.post("/repositories/prepare", json=payload(seeded))
    assert response.status_code == 201, response.text
    failed = response.json()["preparation"]
    assert failed["status"] == "failed" and failed["error_status"] == 502
    assert "root" in failed["error"].lower()
    assert sorted(p.name for p in seeded.original.iterdir()) == ["README.md"]
    seeded.service.checkout.prepare.assert_not_called()


def test_ancestor_replaced_during_resolution_cannot_create_storage_outside_root(client, seeded, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    seeded.service.root = (managed / "repos").resolve()

    def replace_ancestor(source, ref):
        managed.rmdir()
        managed.symlink_to(outside, target_is_directory=True)
        return fake_resolve(source, ref)

    seeded.service.resolver.resolve.side_effect = replace_ancestor
    response = client.post("/repositories/prepare", json=payload(seeded))
    assert response.status_code == 201, response.text
    failed = response.json()["preparation"]
    assert failed["status"] == "failed" and failed["error_status"] == 502
    assert failed["base_sha"] == SHA and "root" in failed["error"].lower()
    assert managed.is_symlink()
    # Checking after mkdir would still report failure, but would already have
    # created this directory through the replaced ancestor.
    assert not (outside / "repos").exists()
    assert list(outside.iterdir()) == []
    seeded.service.resolver.resolve.assert_called_once_with(seeded.source, None)
    seeded.service.checkout.prepare.assert_not_called()


def test_query_rejects_checkout_symlink_before_inspection_and_keeps_ready_record(client, seeded):
    response = client.post("/repositories/prepare", json=payload(seeded))
    assert response.status_code == 201
    record = response.json()["preparation"]
    path = Path(record["repo_path"])
    saved_path = path.with_name("saved-checkout")
    path.rename(saved_path)
    path.symlink_to(seeded.original, target_is_directory=True)
    before = preparation_rows(seeded)
    state = client.get(response.headers["location"])
    assert state.status_code == 200
    assert state.json()["preparation"] == record
    assert state.json()["checkout_intact"] is False and "path" in state.json()["reason"].lower()
    assert preparation_rows(seeded) == before
    assert (saved_path / "README.md").read_text() == CONTENTS
    seeded.service.checkout.inspect.assert_not_called()


def test_lock_file_symlink_returns_503_without_touching_target(client, seeded):
    lock = seeded.tasks.path.with_name(seeded.tasks.path.name + ".repositories.lock")
    lock.unlink()  # Startup already created the regular lock file.
    lock.symlink_to(seeded.original / "README.md")
    response = client.post("/repositories/prepare", json=payload(seeded))
    assert response.status_code == 503, response.text
    assert preparation_rows(seeded) == []
    seeded.service.resolver.resolve.assert_not_called()
    seeded.service.checkout.prepare.assert_not_called()


def test_refreshed_default_branch_is_saved_without_rewriting_imported_source(client, seeded):
    refreshed = seeded.source.repository.model_copy(update={"default_branch": "stable"})
    seeded.service.resolver.resolve.side_effect = None
    seeded.service.resolver.resolve.return_value = ResolvedRepository(
        repository=refreshed, ref="stable", base_sha=SHA, resolved_at=RESOLVED_AT,
    )
    response = client.post("/repositories/prepare", json=payload(seeded))
    assert response.status_code == 201, response.text
    record = response.json()["preparation"]
    assert record["status"] == "ready" and record["resolved_ref"] == "stable"
    assert record["repository"] == refreshed.model_dump(mode="json")
    assert seeded.app.state.issue_source_store.get(seeded.source.source_id) == seeded.source
    seeded.service.checkout.prepare.assert_called_once()
