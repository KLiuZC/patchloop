import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.com")
    nested = root / "example"
    nested.mkdir()
    (nested / "calc.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data" / "tasks.sqlite3")) as client:
        yield client


def payload(repository, **overrides):
    return {
        "repo_path": str(repository),
        "goal": "支持版本号的 v 前缀",
        "verification_command": "python -m pytest -q",
        **overrides,
    }


def test_submit_and_query_freeze_git_base_without_running_commands(client, repository, tmp_path, monkeypatch):
    import patchloop.llm
    import patchloop.repair.runner

    monkeypatch.setattr(patchloop.llm, "LLM", lambda *a, **kw: pytest.fail("API must not call a model"))
    monkeypatch.setattr(patchloop.repair.runner.RepairRunner, "run", lambda *a, **kw: pytest.fail("worker not enabled"))
    marker = tmp_path / "command-must-not-run"
    before = git(repository, "rev-parse", "HEAD")
    submitted = client.post("/tasks", json=payload(repository / "example", verification_command=f"touch {marker}"))
    assert submitted.status_code == 201, submitted.text
    record = submitted.json()
    assert record["status"] == "queued"
    assert record["repo_path"] == str(repository.resolve())
    assert record["workdir"] == "example"
    assert record["base_sha"] == before
    assert record["goal"] == "支持版本号的 v 前缀"
    assert record["created_at"] == record["updated_at"]
    assert not marker.exists()
    assert git(repository, "status", "--porcelain") == ""

    # A later commit must not silently change the already submitted task.
    (repository / "example" / "calc.py").write_text("value = 2\n")
    git(repository, "commit", "-am", "later")
    response = client.get(submitted.headers["location"])
    assert response.status_code == 200 and response.json() == record


def test_task_survives_app_restart_and_changed_working_directory(tmp_path, repository, monkeypatch):
    db_path = tmp_path / "data" / "tasks.sqlite3"
    monkeypatch.chdir(repository)
    with TestClient(create_app(db_path)) as first:
        command = "test 'foo ' = foo\\ "
        response = first.post("/tasks", json=payload("example", verification_command=command))
        assert response.status_code == 201
        record = response.json()
        assert record["verification_command"] == command
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(db_path)) as restarted:
        response = restarted.get(f"/tasks/{record['task_id']}")
        assert response.status_code == 200 and response.json() == record
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_repeated_submissions_have_separate_ids(client, repository):
    first = client.post("/tasks", json=payload(repository)).json()
    second = client.post("/tasks", json=payload(repository)).json()
    assert first["task_id"] != second["task_id"]
    assert client.get(f"/tasks/{first['task_id']}").json() == first


def test_explicit_base_is_resolved_to_an_immutable_commit(client, repository):
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "starting-point")
    (repository / "example" / "calc.py").write_text("value = 3\n")
    git(repository, "commit", "-am", "newer")
    response = client.post("/tasks", json=payload(repository, base_sha="starting-point"))
    assert response.status_code == 201
    assert response.json()["base_sha"] == base


def test_trailing_space_in_repository_path_is_significant(client, repository, tmp_path):
    alias = tmp_path / "alias"
    spaced_alias = tmp_path / "alias "
    alias.symlink_to(repository, target_is_directory=True)
    spaced_alias.symlink_to(repository / "example", target_is_directory=True)
    response = client.post("/tasks", json=payload(spaced_alias))
    assert response.status_code == 201
    assert response.json()["workdir"] == "example"


@pytest.mark.parametrize("path", ["~patchloop_no_such_user_9742/repo", "/tmp/" + "x" * 256])
def test_invalid_os_paths_return_validation_error(client, path):
    response = client.post("/tasks", json=payload(path))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("invalid", [
    {"goal": " \n "},
    {"goal": 42},
    {"verification_command": ""},
    {"verification_command": "echo\x00broken"},
    {"repo_path": ""},
    {"base_sha": "not-a-real-revision"},
    {"base_sha": "--help"},
    {"status": "completed"},
    {"task_id": "user-supplied-id"},
])
def test_invalid_submission_is_not_saved(client, repository, invalid):
    response = client.post("/tasks", json=payload(repository, **invalid))
    assert response.status_code == 422, response.text
    with sqlite3.connect(client.app.state.task_store.path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["missing", "plain_directory", "empty_git"])
def test_repository_must_have_a_commit(client, tmp_path, kind):
    path = tmp_path / kind
    if kind != "missing":
        path.mkdir()
    if kind == "empty_git":
        git(path, "init", "-q")
    response = client.post("/tasks", json=payload(path))
    assert response.status_code == 422, response.text


def test_missing_task_returns_404(client):
    response = client.get(f"/tasks/{uuid4()}")
    assert response.status_code == 404


def test_simultaneous_submissions_are_persisted(client, repository):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/tasks", json=payload(repository)), range(8)))
    assert all(response.status_code == 201 for response in responses)
    ids = {response.json()["task_id"] for response in responses}
    assert len(ids) == 8
    for task_id in ids:
        assert client.get(f"/tasks/{task_id}").status_code == 200


def test_unavailable_database_does_not_return_false_success(client, repository, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client.app.state.task_store, "create", unavailable)
    response = client.post("/tasks", json=payload(repository))
    assert response.status_code == 503
    assert "task_id" not in response.json()


def test_execution_configuration_is_frozen_at_submission(client, repository, monkeypatch):
    from patchloop.llm import PROVIDERS

    response = client.post("/tasks", json=payload(repository, execution={"image": "patchloop-toy", "provider": "glm"}))
    assert response.status_code == 201
    record = response.json()
    expected_model = PROVIDERS["glm"]["model"]
    assert record["execution"]["model"] == expected_model
    monkeypatch.setitem(PROVIDERS["glm"], "model", "future-default")
    assert client.get(f"/tasks/{record['task_id']}").json()["execution"]["model"] == expected_model


@pytest.mark.parametrize("execution", [
    {}, {"image": " "}, {"image": "bad\x00image"}, {"image": "toy", "provider": "unknown"},
    {"image": "toy", "max_model_calls": 0}, {"image": "toy", "command_timeout_seconds": -1},
    {"image": "toy", "verification_timeout_seconds": 0}, {"image": "toy", "memory_mb": 0},
    {"image": "toy", "api_key": "must-not-be-stored"},
])
def test_invalid_execution_configuration_is_rejected(client, repository, execution):
    assert client.post("/tasks", json=payload(repository, execution=execution)).status_code == 422
