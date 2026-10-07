import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api.models import TaskRecord


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "tasks.sqlite3")) as client:
        yield client


def add_task(client, number, *, status="queued", seconds=None):
    created = datetime(2026, 10, 7, tzinfo=timezone.utc) + timedelta(seconds=number if seconds is None else seconds)
    task = TaskRecord(
        task_id=UUID(int=number), repo_path="/example/repo", workdir=".", base_sha="a" * 40,
        goal=f"修复任务 {number}", verification_command="pytest -q", status=status,
        created_at=created, updated_at=created,
    )
    client.app.state.task_store.create(task)
    return str(task.task_id)


def test_empty_list_is_a_successful_empty_page(client):
    response = client.get("/tasks")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_pages_are_newest_first_with_stable_id_tiebreaker(client):
    ids = [add_task(client, number, seconds=number // 2) for number in (3, 1, 5, 2, 4)]
    expected = sorted(ids, reverse=True)
    seen = []
    cursor = None
    for expected_length in (2, 2, 1):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/tasks", params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == expected_length
        seen.extend(item["task_id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert cursor is None
    assert seen == expected


def test_whole_and_fractional_seconds_sort_chronologically(client):
    older = add_task(client, 1, seconds=0)
    newer = add_task(client, 2, seconds=0.1)
    first = client.get("/tasks", params={"limit": 1}).json()
    assert first["items"][0]["task_id"] == newer
    second = client.get("/tasks", params={"cursor": first["next_cursor"]}).json()
    assert [item["task_id"] for item in second["items"]] == [older]


def test_legacy_timestamp_migration_preserves_records_and_corrects_order(client):
    older = add_task(client, 1)
    newer = add_task(client, 2)
    db_path = client.app.state.task_store.path
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", ("2026-10-07T00:00:00Z", older))
        db.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", ("2026-10-07T00:00:00.1+00:00", newer))
        db.execute("PRAGMA user_version=3")
    before = {task_id: client.get(f"/tasks/{task_id}").json() for task_id in (older, newer)}
    with TestClient(create_app(db_path)) as restarted:
        page = restarted.get("/tasks").json()
        assert [item["task_id"] for item in page["items"]] == [newer, older]
        for task_id in (older, newer):
            assert restarted.get(f"/tasks/{task_id}").json() == before[task_id]


def test_new_task_between_pages_does_not_duplicate_older_results(client):
    older = [add_task(client, number) for number in range(1, 5)]
    first = client.get("/tasks", params={"limit": 2}).json()
    newer = add_task(client, 5)
    second = client.get("/tasks", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [item["task_id"] for item in first["items"] + second["items"]] == older[::-1]
    assert second["next_cursor"] is None
    assert client.get("/tasks").json()["items"][0]["task_id"] == newer


@pytest.mark.parametrize("status", ["queued", "running", "completed", "interrupted", "error"])
def test_status_filter_works_for_all_execution_states(client, status):
    states = ["queued", "running", "completed", "interrupted", "error"]
    for number, state in enumerate(states, 1):
        add_task(client, number, status=state)
    page = client.get("/tasks", params={"status": status}).json()
    assert len(page["items"]) == 1 and page["items"][0]["status"] == status
    assert page["next_cursor"] is None


def test_filter_cannot_change_mid_pagination_but_anchor_status_can(client):
    for number in range(1, 4):
        add_task(client, number)
    first = client.get("/tasks", params={"limit": 1, "status": "queued"}).json()
    cursor = first["next_cursor"]
    assert client.get("/tasks", params={"cursor": cursor}).status_code == 422
    assert client.get("/tasks", params={"cursor": cursor, "status": "completed"}).status_code == 422
    with sqlite3.connect(client.app.state.task_store.path) as db:
        db.execute("UPDATE tasks SET status = 'completed' WHERE task_id = ?", (first["items"][0]["task_id"],))
    second = client.get("/tasks", params={"cursor": cursor, "status": "queued"})
    assert second.status_code == 200
    assert [item["task_id"] for item in second.json()["items"]] == [str(UUID(int=2)), str(UUID(int=1))]


def test_list_avoids_large_result_payloads_and_preserves_database(client, monkeypatch):
    task_id = add_task(client, 1, status="completed")
    store = client.app.state.task_store
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET validation_status = 'failed', result_json = ? WHERE task_id = ?",
                   (json.dumps({"patch": "x" * 200000}), task_id))
        before = list(db.iterdump())
    monkeypatch.setattr(store, "_decode", lambda *args: pytest.fail("list must not decode full task records"))
    monkeypatch.setattr(store, "claim", lambda *args: pytest.fail("list must not execute tasks"))
    response = client.get("/tasks")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "completed" and item["validation_status"] == "failed"
    assert not {"result", "execution", "patch", "verification_command"}.intersection(item)
    assert len(response.content) < 2000
    with sqlite3.connect(store.path) as db:
        assert list(db.iterdump()) == before


def test_existing_cursor_survives_api_restart(client):
    for number in range(1, 4):
        add_task(client, number)
    page = client.get("/tasks", params={"limit": 1}).json()
    with TestClient(create_app(client.app.state.task_store.path)) as restarted:
        response = restarted.get("/tasks", params={"cursor": page["next_cursor"]})
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": "invalid"},
    {"status": "passed"}, {"cursor": ""}, {"cursor": "!invalid!"}, {"cursor": "x" * 513},
])
def test_invalid_query_parameters_are_rejected(client, params):
    assert client.get("/tasks", params=params).status_code == 422


@pytest.mark.parametrize("payload", [
    [], {}, {"version": 2, "created_at": "2026-10-07T00:00:00Z", "task_id": str(UUID(int=1)), "status": None},
    {"created_at": "not-a-date", "task_id": str(UUID(int=1)), "status": None},
    {"created_at": "2026-10-07T00:00:00", "task_id": str(UUID(int=1)), "status": None},
    {"created_at": "2026-10-07T00:00:00Z", "task_id": "invalid", "status": None},
])
def test_malformed_cursor_payload_is_rejected(client, payload):
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    assert client.get("/tasks", params={"cursor": cursor}).status_code == 422


def test_default_and_maximum_page_sizes_are_bounded(client):
    for number in range(1, 102):
        add_task(client, number)
    page = client.get("/tasks").json()
    assert len(page["items"]) == 20 and page["next_cursor"]
    page = client.get("/tasks", params={"limit": 100}).json()
    assert len(page["items"]) == 100 and page["next_cursor"]
