import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from patchloop.api.source_models import GitHubIssueSnapshot, GitHubRepository, IssueSource
from patchloop.api.source_store import IssueSourceStore, SourceConflict, SourceCorrupt, snapshot_digest
from patchloop.api.store import TaskStore


def canonical_json(snapshot):
    return json.dumps(
        snapshot.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def make_issue(*, request_id=None, number=42, revision=0):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=revision)
    repository = {
        "id": 1296269, "full_name": "octocat/hello-world",
        "html_url": "https://github.com/octocat/hello-world",
        "url": "https://api.github.com/repos/octocat/hello-world",
        "default_branch": "main", "archived": False, "visibility": "public", "private": False,
        "owner": {"login": "octocat", "id": 1},
    }
    issue = {
        "id": 100000 + number, "number": number,
        "html_url": f"{repository['html_url']}/issues/{number}",
        "url": f"{repository['url']}/issues/{number}",
        "title": f"修复公开 Issue {revision}", "body": f"复现步骤：运行测试。\n版本 {revision} 🐛",
        "state": "open", "created_at": "2026-10-01T00:00:00Z", "updated_at": now.isoformat(),
        "labels": [{"name": "bug", "color": "d73a4a"}], "assignee": None,
    }
    snapshot = GitHubIssueSnapshot(api_version="2022-11-28", repository=repository, issue=issue)
    source = IssueSource(
        source_id=uuid4(), request_id=request_id or uuid4(), requested_url=issue["html_url"],
        url=issue["html_url"], api_url=issue["url"],
        repository=GitHubRepository(**{key: repository[key] for key in (
            "id", "full_name", "html_url", "default_branch", "archived", "visibility",
        )}),
        issue_id=issue["id"], issue_number=number, title=issue["title"], body=issue["body"],
        state=issue["state"], issue_created_at=issue["created_at"], issue_updated_at=now,
        fetched_at=now + timedelta(seconds=3), api_version=snapshot.api_version,
        snapshot_sha256=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    )
    return source, snapshot


@pytest.fixture
def task_store(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    return store


@pytest.fixture
def source_store(task_store):
    return IssueSourceStore(task_store)


def source_rows(task_store):
    with task_store._connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM github_issue_sources ORDER BY source_id")]


def test_missing_source_and_request_return_none_without_creating_tasks(source_store, task_store):
    assert source_store.get(uuid4()) is None
    assert source_store.get_snapshot(uuid4()) is None
    assert source_store.get_for_request(uuid4()) is None
    assert source_rows(task_store) == []
    with task_store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_snapshot_digest_is_canonical_utf8_json_not_http_bytes():
    source, snapshot = make_issue()
    reordered = GitHubIssueSnapshot(
        api_version=snapshot.api_version,
        repository=dict(reversed(list(snapshot.repository.items()))),
        issue=dict(reversed(list(snapshot.issue.items()))),
    )
    reordered.repository["owner"] = {"id": 1, "login": "octocat"}
    reordered.issue["labels"] = [{"color": "d73a4a", "name": "bug"}]
    assert snapshot_digest(snapshot) == source.snapshot_sha256 == snapshot_digest(reordered)
    formatted_response = json.dumps(snapshot.model_dump(mode="json"), indent=2).encode("utf-8")
    assert snapshot_digest(snapshot) != hashlib.sha256(formatted_response).hexdigest()
    snapshot.issue["body"] += " changed"
    assert snapshot_digest(snapshot) != source.snapshot_sha256


def test_round_trip_and_restart_preserve_source_and_model_snapshot(source_store, task_store):
    source, snapshot = make_issue()
    result = source_store.save(source, snapshot)
    assert result.source == source and result.replayed is False
    assert source_store.get(source.source_id) == source
    assert source_store.get_for_request(source.request_id) == source
    assert source_rows(task_store)[0]["snapshot_json"] == canonical_json(snapshot)

    restarted_tasks = TaskStore(task_store.path)
    restarted_tasks.initialize()
    restarted = IssueSourceStore(restarted_tasks)
    assert restarted.get(source.source_id) == source
    assert restarted.get_for_request(source.request_id) == source
    saved = restarted.get_snapshot(source.source_id)
    assert isinstance(saved, GitHubIssueSnapshot) and saved == snapshot
    saved.issue["title"] = "local mutation"
    assert restarted.get_snapshot(source.source_id) == snapshot
    assert restarted.save(*make_issue(request_id=source.request_id, revision=1)).source == source
    assert restarted.save(source, snapshot).replayed is True


def test_replay_keeps_original_contents_ids_and_times(source_store, task_store):
    source, snapshot = make_issue()
    source_store.save(source, snapshot)
    before = source_rows(task_store)
    changed, changed_snapshot = make_issue(request_id=source.request_id, revision=2)
    result = source_store.save(changed, changed_snapshot)
    assert result.replayed is True and result.source == source
    assert result.source.fetched_at != changed.fetched_at
    assert source_store.get(changed.source_id) is None
    assert source_store.get_snapshot(source.source_id) == snapshot
    assert source_rows(task_store) == before


def test_request_conflict_does_not_overwrite_or_consume_new_source_id(source_store, task_store):
    source, snapshot = make_issue()
    source_store.save(source, snapshot)
    before = source_rows(task_store)
    changed, changed_snapshot = make_issue(request_id=source.request_id, number=43)
    with pytest.raises(SourceConflict, match="request_id"):
        source_store.save(changed, changed_snapshot)
    assert source_rows(task_store) == before
    assert source_store.get(changed.source_id) is None
    changed.request_id = uuid4()
    assert source_store.save(changed, changed_snapshot).replayed is False


def test_source_id_collision_is_a_conflict_and_rolls_back(source_store, task_store):
    source, snapshot = make_issue()
    source_store.save(source, snapshot)
    changed, changed_snapshot = make_issue(revision=1)
    changed.source_id = source.source_id
    with pytest.raises(SourceConflict, match="source_id"):
        source_store.save(changed, changed_snapshot)
    assert source_store.get_for_request(changed.request_id) is None
    assert source_store.get_snapshot(source.source_id) == snapshot
    assert len(source_rows(task_store)) == 1
    changed.source_id = uuid4()
    assert source_store.save(changed, changed_snapshot).replayed is False


def test_new_request_for_same_issue_keeps_old_snapshot(source_store, task_store):
    original, original_snapshot = make_issue()
    changed, changed_snapshot = make_issue(revision=4)
    assert original.url == changed.url and original.issue_id == changed.issue_id
    assert source_store.save(original, original_snapshot).replayed is False
    before = source_rows(task_store)[0]
    assert source_store.save(changed, changed_snapshot).replayed is False
    task_store.initialize()
    assert source_store.get_for_request(original.request_id) == original
    assert source_store.get_for_request(changed.request_id) == changed
    assert source_store.get_snapshot(original.source_id) == original_snapshot
    assert source_store.get_snapshot(changed.source_id) == changed_snapshot
    assert before in source_rows(task_store)
    assert len(source_rows(task_store)) == 2


@pytest.mark.parametrize("conflicting", [False, True], ids=["same-url", "different-urls"])
def test_concurrent_same_key_is_atomic_across_store_instances(task_store, conflicting):
    request_id = uuid4()
    inputs = [make_issue(request_id=request_id, number=42 + (i % 2 if conflicting else 0), revision=i)
              for i in range(8)]
    barrier = Barrier(len(inputs))

    def save(pair):
        store = IssueSourceStore(TaskStore(task_store.path))
        barrier.wait(timeout=10)
        try:
            return store.save(*pair)
        except SourceConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(inputs)) as pool:
        results = list(pool.map(save, inputs))
    successes = [result for result in results if not isinstance(result, SourceConflict)]
    assert len(successes) == (4 if conflicting else 8)
    assert sum(not result.replayed for result in successes) == 1
    winner = next(result.source for result in successes if not result.replayed)
    assert all(result.source == winner for result in successes)
    for (candidate, _), result in zip(inputs, results):
        assert isinstance(result, SourceConflict) == (candidate.requested_url != winner.requested_url)
    store = IssueSourceStore(TaskStore(task_store.path))
    assert store.get_for_request(request_id) == winner
    assert store.get_snapshot(winner.source_id) == next(
        snapshot for source, snapshot in inputs if source.source_id == winner.source_id
    )
    assert len(source_rows(task_store)) == 1


@pytest.mark.parametrize("mismatch", ["hash", "version"])
def test_save_rejects_snapshot_mismatch_without_consuming_identifiers(source_store, task_store, mismatch):
    source, snapshot = make_issue()
    invalid = source.model_copy(update={
        "snapshot_sha256": "0" * 64,
    } if mismatch == "hash" else {"api_version": "2099-01-01"})
    with pytest.raises(SourceCorrupt):
        source_store.save(invalid, snapshot)
    assert source_rows(task_store) == []
    assert source_store.get_for_request(source.request_id) is None
    assert source_store.save(source, snapshot).replayed is False


@pytest.mark.parametrize("invalid", [object(), "\ud800"])
def test_non_json_snapshot_is_rejected(source_store, task_store, invalid):
    source, snapshot = make_issue()
    snapshot.issue["invalid"] = invalid
    with pytest.raises(SourceCorrupt):
        snapshot_digest(snapshot)
    with pytest.raises(SourceCorrupt):
        source_store.save(source, snapshot)
    assert source_rows(task_store) == []


@pytest.mark.parametrize("corruption", [
    "record-json", "snapshot-json", "record-shape", "snapshot-shape", "hash", "body", "version",
    "source-id", "request-id", "requested-url", "NaN", "Infinity", "-Infinity",
])
def test_all_reads_and_replays_reject_corrupted_storage(source_store, task_store, corruption):
    source, snapshot = make_issue()
    source_store.save(source, snapshot)
    record_data = source.model_dump(mode="json")
    snapshot_data = snapshot.model_dump(mode="json")
    if corruption in ("record-json", "snapshot-json", "record-shape", "snapshot-shape"):
        column = "record_json" if corruption.startswith("record") else "snapshot_json"
        raw = "{" if corruption.endswith("json") else "{}"
    elif corruption in ("NaN", "Infinity", "-Infinity"):
        column = "snapshot_json"
        # Pydantic's JSON dump maps non-finite numbers back to null. Strict JSON
        # decoding must reject this even though reserialization would match the hash.
        raw = canonical_json(snapshot).replace('"assignee":null', f'"assignee":{corruption}')
    elif corruption in ("body", "version"):
        column = "snapshot_json"
        if corruption == "version":
            snapshot_data["api_version"] = "2099-01-01"
            # Keep the hash valid so version validation is tested independently.
            record_data["snapshot_sha256"] = snapshot_digest(GitHubIssueSnapshot(**snapshot_data))
            with task_store._connection() as connection:
                connection.execute("UPDATE github_issue_sources SET record_json = ?", (json.dumps(record_data),))
        else:
            snapshot_data["issue"]["body"] = "tampered"
        raw = json.dumps(snapshot_data)
    else:
        column = "record_json"
        field = {"hash": "snapshot_sha256", "source-id": "source_id",
                 "request-id": "request_id", "requested-url": "requested_url"}[corruption]
        record_data[field] = "0" * 64 if corruption == "hash" else (
            "https://github.com/octocat/hello-world/issues/43" if corruption == "requested-url" else str(uuid4())
        )
        raw = json.dumps(record_data)
    with task_store._connection() as connection:
        connection.execute(f"UPDATE github_issue_sources SET {column} = ?", (raw,))
    before = source_rows(task_store)
    for read in (
        lambda: source_store.get(source.source_id),
        lambda: source_store.get_snapshot(source.source_id),
        lambda: source_store.get_for_request(source.request_id),
        lambda: source_store.save(source, snapshot),
    ):
        with pytest.raises(SourceCorrupt):
            read()
    assert source_rows(task_store) == before


def test_v6_migration_is_additive_and_reentrant_with_existing_rows(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    task_id, review_id, delivery_id = (str(uuid4()) for _ in range(3))
    timestamp = "2026-10-07T03:04:05+08:00"
    with sqlite3.connect(database) as connection:
        # Build v6 directly so this test cannot inherit the new migration's schema.
        connection.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, workdir TEXT NOT NULL,
                base_sha TEXT NOT NULL, goal TEXT NOT NULL, verification_command TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'error')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                execution_json TEXT, result_json TEXT, artifacts_dir TEXT,
                started_at TEXT, finished_at TEXT, error TEXT NOT NULL DEFAULT '',
                validation_status TEXT NOT NULL DEFAULT 'not_run', cleanup_pending INTEGER NOT NULL DEFAULT 0,
                path_policy_json TEXT
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
            PRAGMA user_version=6;
        """)
        connection.execute("""
            INSERT INTO tasks (
                task_id, repo_path, workdir, base_sha, goal, verification_command, status,
                created_at, updated_at, execution_json, result_json, artifacts_dir, started_at,
                finished_at, error, validation_status, cleanup_pending, path_policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, "/example/public-repo", ".", "a" * 40, "保留历史任务", "pytest -q", "completed",
              timestamp, timestamp, '{ "image": "fake-image" }', '{ "patch": "历史结果" }', "/example/runs/task",
              timestamp, timestamp, "", "passed", 1,
              '{ "allowed_paths": ["src/**"], "protected_paths": ["tests/**"] }'))
        connection.execute("INSERT INTO task_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            review_id, task_id, 1, str(uuid4()), "accepted", "历史审查", '{ "base_sha": "original" }', timestamp,
        ))
        connection.execute("INSERT INTO task_deliveries VALUES (?, ?, ?, ?, ?)", (
            delivery_id, task_id, str(uuid4()), review_id, '{ "status": "delivered", "branch": "old-branch" }',
        ))
        original_schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        original_rows = {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                         for table in ("tasks", "task_reviews", "task_deliveries")}

    task_store = TaskStore(database)
    task_store.initialize()
    task_store.initialize()
    source, snapshot = make_issue()
    source_store = IssueSourceStore(task_store)
    source_store.save(source, snapshot)
    original_sources = source_rows(task_store)
    TaskStore(database).initialize()
    assert source_rows(task_store) == original_sources
    assert source_store.get(source.source_id) == source
    assert source_store.get_snapshot(source.source_id) == snapshot
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"tasks", "task_reviews", "task_deliveries", "github_issue_sources", "repository_preparations", "issue_task_requests"}
        assert [row[1] for row in connection.execute("PRAGMA table_info(github_issue_sources)")] == [
            "source_id", "request_id", "requested_url", "record_json", "snapshot_json",
        ]
        upgraded_schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        assert all(definition in upgraded_schema for definition in original_schema if definition[:2] != ("table", "tasks"))
        for table, rows in original_rows.items():
            upgraded_rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            if table == "tasks":
                assert all(row[-1] is None for row in upgraded_rows)
                upgraded_rows = [row[:-1] for row in upgraded_rows]
            assert upgraded_rows == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("duplicate", ["source_id", "request_id"])
def test_database_enforces_unique_identifiers(source_store, task_store, duplicate):
    source, snapshot = make_issue()
    source_store.save(source, snapshot)
    with pytest.raises(sqlite3.IntegrityError), task_store._connection() as connection:
        connection.execute("""
            INSERT INTO github_issue_sources (source_id, request_id, requested_url, record_json, snapshot_json)
            VALUES (?, ?, ?, ?, ?)
        """, (str(source.source_id) if duplicate == "source_id" else str(uuid4()),
              str(source.request_id) if duplicate == "request_id" else str(uuid4()), source.requested_url,
              source.model_dump_json(), canonical_json(snapshot)))
    assert len(source_rows(task_store)) == 1
