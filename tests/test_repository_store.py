"""Offline storage regressions using synthetic records and temporary SQLite files."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from patchloop.api.repository_models import RepositoryPreparation, RepositoryPreparationResult
from patchloop.api.repository_store import PreparationConflict, PreparationCorrupt, RepositoryPreparationStore
from patchloop.api.source_models import GitHubIssueSnapshot, GitHubRepository, IssueSource
from patchloop.api.source_store import IssueSourceStore, snapshot_digest
from patchloop.api.store import TaskStore


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def make_source():
    repository = GitHubRepository(
        id=123, full_name="example/public-repo", html_url="https://github.com/example/public-repo",
        default_branch="main", archived=False,
    )
    snapshot = GitHubIssueSnapshot(
        api_version="2022-11-28", repository=repository.model_dump(mode="json"),
        issue={"id": 456, "number": 42, "title": "保留历史问题", "body": "复现步骤 🐛"},
    )
    source = IssueSource(
        source_id=uuid4(), request_id=uuid4(), requested_url=f"{repository.html_url}/issues/42",
        url=f"{repository.html_url}/issues/42", api_url="https://api.github.com/repos/example/public-repo/issues/42",
        repository=repository, issue_id=456, issue_number=42, title=snapshot.issue["title"],
        body=snapshot.issue["body"], state="open", issue_created_at=NOW, issue_updated_at=NOW,
        fetched_at=NOW, api_version=snapshot.api_version, snapshot_sha256=snapshot_digest(snapshot),
    )
    return source, snapshot


def make_preparation(source, **overrides):
    preparation_id = uuid4()
    return RepositoryPreparation.model_validate({
        "preparation_id": preparation_id, "request_id": uuid4(), "source_id": source.source_id,
        "repository": source.repository, "requested_ref": None,
        "clone_url": f"{source.repository.html_url}.git", "repo_path": f"/synthetic/repos/{preparation_id}",
        "created_at": NOW, "updated_at": NOW, **overrides,
    })


def resolved(record, **overrides):
    return record.model_copy(update={
        "resolved_ref": "main", "base_sha": "a" * 40, "resolved_at": NOW + timedelta(seconds=1),
        "updated_at": NOW + timedelta(seconds=1), **overrides,
    })


def terminal(record, status):
    return record.model_copy(update={
        "status": status, "updated_at": NOW + timedelta(seconds=2), "finished_at": NOW + timedelta(seconds=2),
        "error": "checkout failed" if status == "failed" else "",
        "error_status": 502 if status == "failed" else None,
    })


@pytest.fixture
def task_store(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    return store


@pytest.fixture
def store(task_store):
    return RepositoryPreparationStore(task_store)


@pytest.fixture
def source(task_store):
    source, snapshot = make_source()
    IssueSourceStore(task_store).save(source, snapshot)
    return source


def preparation_rows(task_store):
    with task_store._connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM repository_preparations ORDER BY preparation_id")]


def test_missing_records_and_pending_scan_do_not_create_tasks(store, task_store):
    assert store.get(uuid4()) is None
    assert store.get_for_request(uuid4()) is None
    assert store.preparing() == []
    assert preparation_rows(task_store) == []
    with task_store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_round_trip_restart_and_replay_preserve_original_record(store, task_store, source):
    record = make_preparation(source)
    result = store.create(record)
    assert isinstance(result, RepositoryPreparationResult)
    assert result.preparation == record and result.replayed is False
    before = preparation_rows(task_store)
    restarted_tasks = TaskStore(task_store.path)
    restarted_tasks.initialize()
    restarted = RepositoryPreparationStore(restarted_tasks)
    assert restarted.get(record.preparation_id) == record
    assert restarted.get_for_request(record.request_id) == record
    assert restarted.preparing() == [record]

    replay = make_preparation(source, request_id=record.request_id, repo_path="/different/path",
                              clone_url="https://github.com/example/different.git", created_at=NOW + timedelta(days=1))
    replay.repository = source.repository.model_copy(update={"default_branch": "next"})
    result = restarted.create(replay)
    assert result.replayed is True and result.preparation == record
    assert restarted.get(replay.preparation_id) is None
    assert preparation_rows(task_store) == before
    # Returned models must not act as mutable storage caches.
    result.preparation.repository.full_name = "changed/locally"
    assert restarted.get(record.preparation_id) == record


@pytest.mark.parametrize("field", ["source_id", "requested_ref"])
def test_request_conflict_rolls_back_and_does_not_consume_preparation_id(store, task_store, source, field):
    record = make_preparation(source)
    store.create(record)
    other_source, snapshot = make_source()
    IssueSourceStore(task_store).save(other_source, snapshot)
    changed = make_preparation(source, request_id=record.request_id, **{
        field: other_source.source_id if field == "source_id" else "main",
    })
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict, match="request_id"):
        store.create(changed)
    assert preparation_rows(task_store) == before
    assert store.get(changed.preparation_id) is None
    assert store.create(changed.model_copy(update={"request_id": uuid4()})).replayed is False


def test_preparation_id_collision_is_conflict_but_same_request_replays(store, task_store, source):
    record = make_preparation(source)
    store.create(record)
    assert store.create(record).replayed is True
    changed = make_preparation(source, preparation_id=record.preparation_id)
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict, match="preparation_id"):
        store.create(changed)
    assert store.get_for_request(changed.request_id) is None
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("conflicting", [None, "source_id", "requested_ref"])
def test_concurrent_request_id_is_atomic_across_connections(task_store, source, conflicting):
    other_source, snapshot = make_source()
    IssueSourceStore(task_store).save(other_source, snapshot)
    request_id = uuid4()
    records = [make_preparation(
        other_source if conflicting == "source_id" and i % 2 else source,
        request_id=request_id, requested_ref="main" if conflicting == "requested_ref" and i % 2 else None,
    ) for i in range(8)]
    barrier = Barrier(len(records))

    def create(record):
        store = RepositoryPreparationStore(TaskStore(task_store.path))
        barrier.wait(timeout=10)
        try:
            return store.create(record)
        except PreparationConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        results = list(pool.map(create, records))
    successes = [result for result in results if isinstance(result, RepositoryPreparationResult)]
    assert len(successes) == (4 if conflicting else 8)
    assert sum(not result.replayed for result in successes) == 1
    winner = next(result.preparation for result in successes if not result.replayed)
    assert all(result.preparation == winner for result in successes)
    for record, result in zip(records, results):
        mismatch = (record.source_id, record.requested_ref) != (winner.source_id, winner.requested_ref)
        assert isinstance(result, PreparationConflict) == mismatch
    assert len(preparation_rows(task_store)) == 1
    assert RepositoryPreparationStore(task_store).get_for_request(request_id) == winner


def test_concurrent_preparation_id_collision_cannot_consume_losing_requests(task_store, source):
    preparation_id = uuid4()
    records = [make_preparation(source, preparation_id=preparation_id) for _ in range(6)]
    barrier = Barrier(len(records))

    def create(record):
        store = RepositoryPreparationStore(TaskStore(task_store.path))
        barrier.wait(timeout=10)
        try:
            return store.create(record)
        except PreparationConflict:
            return None

    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        results = list(pool.map(create, records))
    successes = [result for result in results if result is not None]
    assert len(successes) == 1 and successes[0].replayed is False
    winner = successes[0].preparation
    store = RepositoryPreparationStore(task_store)
    for record in records:
        assert store.get_for_request(record.request_id) == (winner if record == winner else None)
    assert len(preparation_rows(task_store)) == 1


def test_update_requires_existing_preparation(store, task_store, source):
    with pytest.raises(PreparationConflict, match="does not exist"):
        store.update(make_preparation(source))
    assert preparation_rows(task_store) == []


@pytest.mark.parametrize("field", [
    "preparation_id", "request_id", "source_id", "repository", "requested_ref", "clone_url", "repo_path", "created_at",
])
def test_update_rejects_mutated_identity_inputs_and_paths(store, task_store, source, field):
    record = make_preparation(source)
    store.create(record)
    changed_values = {
        "preparation_id": uuid4(), "request_id": uuid4(), "source_id": uuid4(),
        "repository": source.repository.model_copy(update={"archived": True}), "requested_ref": "release",
        "clone_url": "https://github.com/example/other.git", "repo_path": "/other/path", "created_at": NOW + timedelta(days=1),
    }
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict):
        store.update(record.model_copy(update={field: changed_values[field]}))
    assert preparation_rows(task_store) == before


def test_initial_resolution_refreshes_repository_metadata_then_preserves_it(store, task_store, source):
    record = make_preparation(source)
    store.create(record)
    refreshed = record.repository.model_copy(update={
        "full_name": "Example/Public-Repo", "default_branch": "trunk", "archived": True,
        "html_url": "https://github.com/Example/Public-Repo",
    })
    pinned = resolved(record, repository=refreshed, resolved_ref="trunk")
    store.update(pinned)
    assert store.get(record.preparation_id) == pinned
    assert store.preparing() == [pinned]
    assert store.create(record).preparation == pinned
    finished = terminal(pinned, "ready")
    store.update(finished)
    assert store.get(record.preparation_id) == finished
    assert IssueSourceStore(task_store).get(source.source_id) == source


@pytest.mark.parametrize("changes", [{"id": 999}, {"full_name": "example/other-repo"}])
def test_initial_resolution_cannot_replace_repository_identity(store, task_store, source, changes):
    record = make_preparation(source)
    store.create(record)
    before = preparation_rows(task_store)
    changed = resolved(record, repository=record.repository.model_copy(update=changes))
    with pytest.raises(PreparationConflict, match="identity"):
        store.update(changed)
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("missing", ["base_sha", "resolved_ref", "resolved_at"])
def test_metadata_refresh_requires_complete_initial_resolution(store, task_store, source, missing):
    record = make_preparation(source)
    store.create(record)
    changed = resolved(record, repository=record.repository.model_copy(update={"archived": True}), **{missing: None})
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict, match="metadata"):
        store.update(changed)
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("changes", [
    {"default_branch": "trunk"}, {"archived": True}, {"full_name": "Example/Public-Repo"},
    {"html_url": "https://github.com/Example/Public-Repo"},
])
@pytest.mark.parametrize("status", ["preparing", "ready", "failed"])
def test_repository_metadata_freezes_after_resolution(store, task_store, source, changes, status):
    record = make_preparation(source)
    store.create(record)
    pinned = resolved(record)
    store.update(pinned)
    candidate = pinned if status == "preparing" else terminal(pinned, status)
    candidate = candidate.model_copy(update={"repository": candidate.repository.model_copy(update=changes)})
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict, match="metadata"):
        store.update(candidate)
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_resolution_then_terminal_update_preserves_pin_and_replay(store, task_store, source, status):
    record = make_preparation(source, requested_ref="main")
    pending = make_preparation(source)
    store.create(record)
    store.create(pending)
    pinned = resolved(record)
    store.update(pinned)
    restarted = RepositoryPreparationStore(TaskStore(task_store.path))
    assert restarted.get(record.preparation_id) == pinned
    assert {item.preparation_id for item in restarted.preparing()} == {record.preparation_id, pending.preparation_id}
    finished = terminal(pinned, status)
    restarted.update(finished)
    assert restarted.get(record.preparation_id) == finished
    assert restarted.preparing() == [pending]
    before = preparation_rows(task_store)
    result = restarted.create(record)
    assert result.replayed is True and result.preparation == finished
    assert preparation_rows(task_store) == before
    with task_store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_failure_can_be_recorded_before_resolution(store, source):
    record = make_preparation(source)
    store.create(record)
    failed = terminal(record, "failed")
    store.update(failed)
    assert store.get(record.preparation_id) == failed
    assert store.preparing() == []


@pytest.mark.parametrize("field", ["resolved_ref", "base_sha", "resolved_at"])
@pytest.mark.parametrize("clear", [False, True], ids=["replace", "clear"])
@pytest.mark.parametrize("status", ["preparing", "ready", "failed"])
def test_pinned_resolution_cannot_change_or_disappear(store, task_store, source, field, clear, status):
    record = make_preparation(source)
    store.create(record)
    pinned = resolved(record)
    store.update(pinned)
    new_values = {"resolved_ref": "next", "base_sha": "b" * 40, "resolved_at": NOW + timedelta(minutes=1)}
    candidate = pinned if status == "preparing" else terminal(pinned, status)
    candidate = candidate.model_copy(update={field: None if clear else new_values[field]})
    before = preparation_rows(task_store)
    with pytest.raises(PreparationConflict, match=field):
        store.update(candidate)
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("status", ["ready", "failed"])
@pytest.mark.parametrize("next_status", ["preparing", "ready", "failed"])
def test_terminal_records_reject_all_updates_including_identical_record(store, task_store, source, status, next_status):
    record = make_preparation(source)
    store.create(record)
    pinned = resolved(record)
    store.update(pinned)
    finished = terminal(pinned, status)
    store.update(finished)
    before = preparation_rows(task_store)
    for candidate in (finished, finished.model_copy(update={"status": next_status, "error": "late writer"})):
        with pytest.raises(PreparationConflict, match="no longer preparing"):
            store.update(candidate)
    assert preparation_rows(task_store) == before


def test_stale_update_cannot_erase_a_resolution_pin(store, task_store, source):
    record = make_preparation(source)
    store.create(record)
    pinned = resolved(record)
    store.update(pinned)
    with pytest.raises(PreparationConflict, match="pinned"):
        store.update(terminal(record, "failed"))
    assert store.get(record.preparation_id) == pinned


@pytest.mark.parametrize("phase", ["resolution", "metadata", "finish"])
def test_concurrent_updates_preserve_first_pin_or_terminal_record(store, task_store, source, phase):
    record = make_preparation(source)
    store.create(record)
    if phase == "resolution":
        candidates = [resolved(record, base_sha=str(i) * 40) for i in range(6)]
    elif phase == "metadata":
        candidates = [resolved(record, repository=record.repository.model_copy(update={"default_branch": f"branch-{i}"}))
                      for i in range(6)]
    else:
        pinned = resolved(record)
        store.update(pinned)
        candidates = [terminal(pinned, "ready" if i % 2 else "failed") for i in range(6)]
    barrier = Barrier(len(candidates))

    def update(candidate):
        independent = RepositoryPreparationStore(TaskStore(task_store.path))
        barrier.wait(timeout=10)
        try:
            independent.update(candidate)
            return candidate
        except PreparationConflict:
            return None

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = list(pool.map(update, candidates))
    winners = [candidate for candidate in results if candidate is not None]
    assert len(winners) == 1
    assert store.get(record.preparation_id) == winners[0]
    assert len(preparation_rows(task_store)) == 1


@pytest.mark.parametrize("field", ["resolved_ref", "base_sha", "resolved_at", "finished_at"])
def test_ready_requires_complete_resolution(store, task_store, source, field):
    record = make_preparation(source)
    store.create(record)
    candidate = terminal(resolved(record), "ready").model_copy(update={field: None})
    with pytest.raises(PreparationCorrupt, match="ready"):
        store.update(candidate)
    assert store.get(record.preparation_id) == record


@pytest.mark.parametrize("changes", [{"error": ""}, {"error": "   "}, {"finished_at": None}])
def test_failed_requires_error_and_finished_at(store, task_store, source, changes):
    record = make_preparation(source)
    store.create(record)
    candidate = terminal(record, "failed").model_copy(update=changes)
    before = preparation_rows(task_store)
    with pytest.raises(PreparationCorrupt, match="failed"):
        store.update(candidate)
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("status, changes", [
    ("ready", {"base_sha": None}), ("ready", {"resolved_ref": None}),
    ("ready", {"resolved_at": None}), ("ready", {"finished_at": None}),
    ("failed", {"error": ""}), ("failed", {"error": "   "}), ("failed", {"finished_at": None}),
])
def test_decode_rejects_corrupt_terminal_state(store, task_store, source, status, changes):
    record = make_preparation(source)
    store.create(record)
    invalid = terminal(resolved(record), status).model_copy(update=changes)
    with task_store._connection() as connection:
        connection.execute("UPDATE repository_preparations SET record_json = ?", (invalid.model_dump_json(),))
    before = preparation_rows(task_store)
    for operation in (
        lambda: store.get(record.preparation_id), lambda: store.get_for_request(record.request_id),
        store.preparing, lambda: store.create(record), lambda: store.update(record),
    ):
        with pytest.raises(PreparationCorrupt, match=status):
            operation()
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_new_preparation_must_start_preparing(store, task_store, source, status):
    record = terminal(resolved(make_preparation(source)), status)
    with pytest.raises(PreparationConflict, match="must be preparing"):
        store.create(record)
    assert preparation_rows(task_store) == []


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("changes", [{"base_sha": "not-a-sha"}, {"updated_at": NOW.replace(tzinfo=None)}])
def test_writes_revalidate_mutable_models_without_consuming_or_overwriting_record(store, task_store, source, operation, changes):
    record = make_preparation(source)
    if operation == "update":
        store.create(record)
    before = preparation_rows(task_store)
    with pytest.raises(PreparationCorrupt):
        getattr(store, operation)(record.model_copy(update=changes))
    assert preparation_rows(task_store) == before
    if operation == "create":
        assert store.create(record).replayed is False


@pytest.mark.parametrize("corruption", [
    "json", "shape", "array", "extra-field", "invalid-status", "invalid-sha", "invalid-repository", "naive-time",
    "preparation_id", "request_id", "source_id", "NaN", "Infinity", "-Infinity",
])
def test_all_reads_replays_and_updates_detect_corrupt_records(store, task_store, source, corruption):
    record = make_preparation(source)
    store.create(record)
    data = record.model_dump(mode="json")
    if corruption in ("json", "shape", "array"):
        raw = {"json": "{", "shape": "{}", "array": "[]"}[corruption]
    elif corruption in ("NaN", "Infinity", "-Infinity"):
        raw = record.model_dump_json().replace('"error_status":null', f'"error_status":{corruption}')
    else:
        field, value = {
            "extra-field": ("unexpected", "value"), "invalid-status": ("status", "unknown"),
            "invalid-sha": ("base_sha", "short"), "invalid-repository": ("repository", {}),
            "naive-time": ("updated_at", "2026-10-08T12:00:00"),
            **{name: (name, str(uuid4())) for name in ("preparation_id", "request_id", "source_id")},
        }[corruption]
        data[field] = value
        raw = json.dumps(data)
    with task_store._connection() as connection:
        connection.execute("UPDATE repository_preparations SET record_json = ?", (raw,))
    before = preparation_rows(task_store)
    for operation in (
        lambda: store.get(record.preparation_id), lambda: store.get_for_request(record.request_id),
        store.preparing, lambda: store.create(record), lambda: store.update(resolved(record)),
    ):
        with pytest.raises(PreparationCorrupt):
            operation()
    assert preparation_rows(task_store) == before


@pytest.mark.parametrize("duplicate", ["preparation_id", "request_id"])
def test_database_enforces_unique_identifiers(store, task_store, source, duplicate):
    record = make_preparation(source)
    store.create(record)
    candidate = make_preparation(source, **{duplicate: getattr(record, duplicate)})
    with pytest.raises(sqlite3.IntegrityError), task_store._connection() as connection:
        connection.execute("INSERT INTO repository_preparations VALUES (?, ?, ?, ?)", (
            str(candidate.preparation_id), str(candidate.request_id), str(candidate.source_id), candidate.model_dump_json(),
        ))
    assert store.get(record.preparation_id) == record
    assert len(preparation_rows(task_store)) == 1


def test_foreign_key_rejects_missing_source_and_protects_referenced_source(store, task_store, source):
    record = make_preparation(source)
    with pytest.raises(sqlite3.IntegrityError):
        store.create(record.model_copy(update={"source_id": uuid4()}))
    assert preparation_rows(task_store) == []
    assert store.create(record).replayed is False
    with pytest.raises(sqlite3.IntegrityError), task_store._connection() as connection:
        connection.execute("DELETE FROM github_issue_sources WHERE source_id = ?", (str(source.source_id),))
    assert store.get(record.preparation_id) == record
    assert IssueSourceStore(task_store).get(source.source_id) == source


def test_v7_to_v8_migration_is_additive_and_preserves_all_existing_rows(tmp_path):
    database = tmp_path / "v7.sqlite3"
    task_id, review_id, delivery_id = (str(uuid4()) for _ in range(3))
    source, snapshot = make_source()
    timestamp = "2026-10-07T03:04:05+08:00"
    with sqlite3.connect(database) as connection:
        # Independently define the v7 schema, without running the new initializer.
        connection.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, workdir TEXT NOT NULL,
                base_sha TEXT NOT NULL, goal TEXT NOT NULL, verification_command TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'error')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                execution_json TEXT, result_json TEXT, artifacts_dir TEXT, started_at TEXT, finished_at TEXT,
                error TEXT NOT NULL DEFAULT '', validation_status TEXT NOT NULL DEFAULT 'not_run',
                cleanup_pending INTEGER NOT NULL DEFAULT 0, path_policy_json TEXT
            );
            CREATE INDEX tasks_created_order ON tasks(created_at DESC, task_id DESC);
            CREATE INDEX tasks_status_created_order ON tasks(status, created_at DESC, task_id DESC);
            CREATE TABLE task_reviews (
                review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                revision INTEGER NOT NULL CHECK(revision > 0), request_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected')),
                comment TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id, revision), UNIQUE(task_id, request_id)
            );
            CREATE TABLE task_deliveries (
                delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                request_id TEXT NOT NULL, review_id TEXT NOT NULL REFERENCES task_reviews(review_id),
                record_json TEXT NOT NULL, UNIQUE(task_id, review_id), UNIQUE(task_id, request_id)
            );
            CREATE TABLE github_issue_sources (
                source_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, requested_url TEXT NOT NULL,
                record_json TEXT NOT NULL, snapshot_json TEXT NOT NULL
            );
            PRAGMA user_version=7;
        """)
        connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            task_id, "/synthetic/old-repo", ".", "a" * 40, "保留历史任务", "pytest -q", "completed",
            timestamp, timestamp, '{ "image": "fake-image" }', '{ "patch": "历史结果" }', "/synthetic/runs/task",
            timestamp, timestamp, "", "passed", 1,
            '{ "allowed_paths": ["src/**"], "protected_paths": ["tests/**"] }',
        ))
        connection.execute("""
            INSERT INTO tasks (task_id, repo_path, workdir, base_sha, goal, verification_command,
                               status, created_at, updated_at, cleanup_pending)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid4()), "/synthetic/running", ".", "b" * 40, "running", "pytest", "running", timestamp, timestamp, 0))
        connection.execute("INSERT INTO task_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            review_id, task_id, 1, str(uuid4()), "accepted", "历史审查", '{ "base_sha": "original" }', timestamp,
        ))
        connection.execute("INSERT INTO task_deliveries VALUES (?, ?, ?, ?, ?)", (
            delivery_id, task_id, str(uuid4()), review_id, '{ "status": "completed", "branch": "old-branch" }',
        ))
        connection.execute("INSERT INTO github_issue_sources VALUES (?, ?, ?, ?, ?)", (
            str(source.source_id), str(source.request_id), source.requested_url,
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2),
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=4),
        ))
        original_schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        original_rows = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                         for table in ("tasks", "task_reviews", "task_deliveries", "github_issue_sources")}

    task_store = TaskStore(database)
    task_store.initialize()
    task_store.initialize()
    store = RepositoryPreparationStore(task_store)
    assert store.preparing() == []
    record = make_preparation(source)
    assert store.create(record).replayed is False
    original_preparations = preparation_rows(task_store)
    TaskStore(database).initialize()
    assert preparation_rows(task_store) == original_preparations
    assert store.get(record.preparation_id) == record
    assert IssueSourceStore(task_store).get(source.source_id) == source
    assert IssueSourceStore(task_store).get_snapshot(source.source_id) == snapshot
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {*original_rows, "repository_preparations", "issue_task_requests"}
        assert [row[1] for row in connection.execute("PRAGMA table_info(repository_preparations)")] == [
            "preparation_id", "request_id", "source_id", "record_json",
        ]
        upgraded_schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        assert all(definition in upgraded_schema for definition in original_schema if definition[:2] != ("table", "tasks"))
        for table, rows in original_rows.items():
            upgraded_rows = connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            if table == "tasks":
                assert all(row[-1] is None for row in upgraded_rows)
                upgraded_rows = [row[:-1] for row in upgraded_rows]
            assert upgraded_rows == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
