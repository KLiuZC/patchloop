"""Delivery integration regressions: real Git/SQLite, archived contracts, no runner."""

import hashlib
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api import delivery as delivery_module
from patchloop.api.models import (
    DeliverySubmission,
    ExecutionConfig,
    PathPolicy,
    ReviewSubmission,
    TaskRecord,
)
from patchloop.api.store import TaskStore
from patchloop.repair.contracts import BaselineResult, RepairResult, ValidationResult


@pytest.fixture(autouse=True)
def forbid_execution(monkeypatch):
    from patchloop.llm import LLM
    from patchloop.repair import RepairRunner
    from patchloop.sandbox import DockerRunner, LocalRunner

    def forbidden(*args, **kwargs):
        pytest.fail("delivery must not execute models, repair runners, or verification")

    monkeypatch.setattr(LLM, "__init__", forbidden)
    monkeypatch.setattr(RepairRunner, "run", forbidden)
    monkeypatch.setattr(DockerRunner, "start", forbidden)
    monkeypatch.setattr(LocalRunner, "run_result", forbidden)


def git(repo, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
         "-c", "commit.gpgSign=false", "-C", str(repo), *args],
        input=input, capture_output=True, check=True, timeout=15, env=env,
    ).stdout


def rev(repo, name="HEAD"):
    return git(repo, "rev-parse", name).decode().strip()


def files(root, *, include_git=False):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and (include_git or ".git" not in path.relative_to(root).parts)
    }


def source_snapshot(repo):
    return {
        "head": rev(repo),
        "branch": git(repo, "symbolic-ref", "HEAD"),
        "index": (repo / ".git/index").read_bytes(),
        "index_mtime": (repo / ".git/index").stat().st_mtime_ns,
        "head_file": (repo / ".git/HEAD").read_bytes(),
        "status": git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        "staged": git(repo, "ls-files", "--stage", "-z"),
        "files": files(repo),
    }


def branches(repo):
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").splitlines()


def records(client, task):
    store = client.app.state.task_store
    with store._connection() as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT record_json FROM task_deliveries WHERE task_id=?", (str(task.task_id),),
        )]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "--template=", "--initial-branch=main")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.com")
    for name, content in {
        "src/value.py": "value = 1\n",
        "obsolete.txt": "remove this file\n",
        "notes.txt": "original notes\n",
        "tests/test_value.py": "assert True\n",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "tasks.sqlite3", deliveries_root=tmp_path / "deliveries")) as value:
        yield value


@dataclass
class Candidate:
    task: TaskRecord
    repo: Path
    patch: bytes
    tree: str
    changed_paths: list[str]


def decide(client, candidate, decision="accepted"):
    task_id = candidate.task.task_id
    state = client.get(f"/tasks/{task_id}/review").json()
    assert state["can_accept"] is True, state
    response = client.post(f"/tasks/{task_id}/reviews", json={
        "request_id": str(uuid4()), "expected_revision": state["revision"],
        "decision": decision, "evidence": state["evidence"], "comment": "reviewed offline",
    })
    assert response.status_code == 201, response.text
    assert response.json()["current"]["status"] == decision
    return response.json()["review"]


@pytest.fixture
def make_candidate(client, repository, tmp_path):
    def make(*, decision="accepted", protected=False, legacy=False):
        store: TaskStore = client.app.state.task_store
        task_id = uuid4()
        builder = tmp_path / f"builder-{task_id}"
        shutil.copytree(repository, builder)
        if protected:
            (builder / "tests/test_value.py").write_text("assert False\n", encoding="utf-8")
            changed_paths = ["tests/test_value.py"]
        else:
            (builder / "src/value.py").write_text("value = 2\n", encoding="utf-8")
            (builder / "obsolete.txt").unlink()
            (builder / "src/new.py").write_text("#!/usr/bin/env python3\nprint('新增')\n", encoding="utf-8")
            (builder / "src/new.py").chmod(0o755)
            (builder / "src/blob.bin").write_bytes(b"\x00\xffcandidate\x00\x01")
            changed_paths = ["obsolete.txt", "src/blob.bin", "src/new.py", "src/value.py"]
        git(builder, "add", "-A")
        base = rev(repository)
        patch = git(builder, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
                    "--no-textconv", "--no-renames", base, "--")
        tree = git(builder, "write-tree").decode().strip()
        now = datetime.now(timezone.utc)
        queued = TaskRecord(
            task_id=task_id, repo_path=str(repository), workdir=".", base_sha=base,
            goal="交付已经人工审查的精确补丁", verification_command="python -m pytest -q",
            status="queued", created_at=now, updated_at=now,
            execution=ExecutionConfig(image="offline-delivery-fixture", model="offline-fixture"),
            path_policy=None if legacy else PathPolicy(),
        )
        store.create(queued)
        claimed = store.claim(tmp_path / "artifacts", task_id)
        assert claimed is not None and claimed.task_id == task_id
        repair_task = claimed.to_repair_task()
        root = Path(claimed.artifacts_dir)
        root.mkdir(parents=True)
        profile = dict(profile_id=repair_task.verification.id, command=repair_task.verification.command,
                       base_sha=base)
        validation = ValidationResult(
            **profile, status="passed", candidate_status="passed", exit_code=0,
            output="fixture: candidate passed", patch_sha256=hashlib.sha256(patch).hexdigest(),
            baseline=BaselineResult(**profile, status="failed", exit_code=1, output="fixture: baseline failed"),
        )
        result = RepairResult(
            task_id=str(task_id), execution_status="completed", validation_status="passed",
            patch=patch.decode("utf-8"), validation=validation, artifacts_dir=root, stop_reason="finished",
        )
        write_json(root / "task.json", repair_task.to_dict())
        write_json(root / "summary.json", result.to_dict())
        write_json(root / "validation.json", validation.to_dict())
        (root / "patch.diff").write_bytes(patch)
        store.finish(task_id, status="completed", validation_status="passed", result=result.to_dict())
        store.mark_clean(task_id)
        candidate = Candidate(store.get(task_id), repository, patch, tree, changed_paths)
        if decision is not None:
            decide(client, candidate, decision)
        return candidate
    return make


def delivery_state(client, candidate):
    response = client.get(f"/tasks/{candidate.task.task_id}/delivery")
    assert response.status_code == 200, response.text
    return response.json()


def delivery_body(client, candidate):
    state = client.get(f"/tasks/{candidate.task.task_id}/review").json()
    review = state["latest_review"]
    return {
        "request_id": str(uuid4()), "review_id": review["review_id"] if review else str(uuid4()),
        "review_revision": review["revision"] if review else 1,
        "evidence": state["evidence"], "target_head": candidate.task.base_sha,
    }


def post_delivery(client, candidate, body, *, expected=201):
    response = client.post(f"/tasks/{candidate.task.task_id}/delivery", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def dirty_source(repo):
    (repo / "src/value.py").write_text("value = 'staged user edit'\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged addition\n", encoding="utf-8")
    git(repo, "add", "src/value.py", "staged.txt")
    (repo / "src/value.py").write_text("value = 'unstaged user edit'\n", encoding="utf-8")
    (repo / "notes.txt").write_text("dirty notes\n", encoding="utf-8")
    (repo / "src/new.py").write_text("untracked collision with delivered addition\n", encoding="utf-8")
    (repo / "scratch.txt").write_bytes(b"untracked\x00data")


def assert_completed(client, candidate, response):
    record = response["delivery"]
    assert record["status"] == "completed", record
    assert record["error"] == ""
    assert record["tree_sha"] == candidate.tree
    assert rev(candidate.repo, f"{record['commit_sha']}^{{tree}}") == candidate.tree
    assert git(candidate.repo, "show", "-s", "--format=%P", record["commit_sha"]).decode().strip() == candidate.task.base_sha
    assert rev(candidate.repo, f"refs/heads/{record['branch']}") == record["commit_sha"]
    worktree = Path(record["worktree_path"])
    assert rev(worktree) == record["commit_sha"]
    assert git(worktree, "symbolic-ref", "HEAD").decode().strip() == f"refs/heads/{record['branch']}"
    assert git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir") == git(
        candidate.repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
    )
    assert git(worktree, "status", "--porcelain=v1", "--untracked-files=all") == b""
    assert len(records(client, candidate.task)) == 1
    return record


def test_get_delivery_plans_exact_paths_without_writing_repo_or_artifacts(client, make_candidate, tmp_path):
    candidate = make_candidate()
    dirty_source(candidate.repo)
    repo_before = files(candidate.repo, include_git=True)
    artifacts_before = files(Path(candidate.task.artifacts_dir))

    state = delivery_state(client, candidate)

    assert state["can_deliver"] is True, state
    assert state["dirty"] is True
    assert state["changed_paths"] == candidate.changed_paths
    assert state["target_head"] == candidate.task.base_sha
    assert state["branch"] == f"patchloop/{candidate.task.task_id}/r1"
    assert Path(state["worktree_path"]) == tmp_path / "deliveries" / str(candidate.task.task_id) / "r1"
    assert state["delivery"] is None and state["delivery_intact"] is None
    assert state["policy"] == PathPolicy().model_dump()
    assert files(candidate.repo, include_git=True) == repo_before
    assert files(Path(candidate.task.artifacts_dir)) == artifacts_before
    assert records(client, candidate.task) == []
    assert not (tmp_path / "deliveries").exists()


def test_delivery_publishes_exact_patch_and_keeps_original_dirty_index_head_and_evidence(client, make_candidate):
    candidate = make_candidate()
    dirty_source(candidate.repo)
    before = source_snapshot(candidate.repo)
    artifacts_before = files(Path(candidate.task.artifacts_dir))
    detail_before = client.get(f"/tasks/{candidate.task.task_id}").json()
    reviews_before = client.get(f"/tasks/{candidate.task.task_id}/reviews").json()
    body = delivery_body(client, candidate)

    response = post_delivery(client, candidate, body)

    assert response["replayed"] is False
    record = assert_completed(client, candidate, response)
    assert record["request_id"] == body["request_id"]
    assert record["review_id"] == body["review_id"]
    assert record["review_revision"] == body["review_revision"]
    assert record["evidence"] == body["evidence"]
    assert record["target_head"] == candidate.task.base_sha
    assert git(candidate.repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
               "--no-renames", candidate.task.base_sha, record["commit_sha"], "--") == candidate.patch
    worktree = Path(record["worktree_path"])
    assert (worktree / "src/value.py").read_bytes() == b"value = 2\n"
    assert (worktree / "src/blob.bin").read_bytes() == b"\x00\xffcandidate\x00\x01"
    assert (worktree / "src/new.py").stat().st_mode & 0o111
    assert not (worktree / "obsolete.txt").exists()
    assert not (worktree / "staged.txt").exists()
    assert not (worktree / "scratch.txt").exists()
    assert source_snapshot(candidate.repo) == before
    assert files(Path(candidate.task.artifacts_dir)) == artifacts_before
    assert client.get(f"/tasks/{candidate.task.task_id}").json() == detail_before
    assert client.get(f"/tasks/{candidate.task.task_id}/reviews").json() == reviews_before
    state = delivery_state(client, candidate)
    assert state["delivery"] == record and state["delivery_intact"] is True
    assert state["can_deliver"] is False
    assert state["dirty"] is True
    assert state["changed_paths"] == candidate.changed_paths
    assert state["target_head"] == before["head"]
    assert state["branch"] == record["branch"]
    assert state["worktree_path"] == record["worktree_path"]


@pytest.mark.parametrize("new_request_id", [False, True], ids=["same-request", "same-review"])
def test_replay_after_restart_does_not_create_another_commit_branch_or_record(client, make_candidate, tmp_path, new_request_id):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    original = post_delivery(client, candidate, body)
    record = assert_completed(client, candidate, original)
    before = files(candidate.repo, include_git=True)
    output_before = files(Path(record["worktree_path"]), include_git=True)
    if new_request_id:
        body = {**body, "request_id": str(uuid4())}

    with TestClient(create_app(client.app.state.task_store.path, deliveries_root=tmp_path / "deliveries")) as restarted:
        replay = post_delivery(restarted, candidate, body, expected=200)
        assert replay == {"delivery": record, "replayed": True}
        assert records(restarted, candidate.task) == [record]

    assert files(candidate.repo, include_git=True) == before
    assert files(Path(record["worktree_path"]), include_git=True) == output_before


def test_reused_request_cannot_change_target_review_or_evidence(client, make_candidate):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    record = assert_completed(client, candidate, post_delivery(client, candidate, body))
    before = files(candidate.repo, include_git=True)
    for changes in (
        {"target_head": "0" * len(candidate.task.base_sha)},
        {"review_id": str(uuid4())},
        {"review_revision": body["review_revision"] + 1},
        {"evidence": {**body["evidence"], "patch_sha256": "0" * 64}},
    ):
        response = post_delivery(client, candidate, {**body, **changes}, expected=409)
        assert "different" in response["detail"]
    assert records(client, candidate.task) == [record]
    assert files(candidate.repo, include_git=True) == before


@pytest.mark.parametrize("review_status", ["pending", "rejected", "stale"])
def test_unaccepted_or_stale_review_never_creates_output(client, make_candidate, tmp_path, review_status):
    candidate = make_candidate(decision=None if review_status == "pending" else "accepted")
    body = delivery_body(client, candidate)
    if review_status == "rejected":
        decide(client, candidate, "rejected")
    elif review_status == "stale":
        path = Path(candidate.task.artifacts_dir) / "validation.json"
        path.write_bytes(path.read_bytes() + b"\n")
    before = files(candidate.repo, include_git=True)

    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False
    assert state["reason"]
    post_delivery(client, candidate, body, expected=409)

    assert client.get(f"/tasks/{candidate.task.task_id}/review").json()["status"] == review_status
    assert records(client, candidate.task) == []
    assert files(candidate.repo, include_git=True) == before
    assert not (tmp_path / "deliveries").exists()


def test_moved_source_head_refuses_delivery_without_retargeting(client, make_candidate, tmp_path):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    git(candidate.repo, "commit", "--allow-empty", "-qm", "user moved HEAD")
    before = files(candidate.repo, include_git=True)

    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False and "HEAD" in state["reason"]
    assert state["target_head"] != candidate.task.base_sha
    post_delivery(client, candidate, body, expected=409)

    assert files(candidate.repo, include_git=True) == before
    assert records(client, candidate.task) == []
    assert not (tmp_path / "deliveries").exists()


def test_new_delivery_request_must_match_approved_review_evidence_and_base(client, make_candidate, tmp_path):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    before = files(candidate.repo, include_git=True)
    for changes in (
        {"target_head": "0" * len(candidate.task.base_sha)},
        {"review_id": str(uuid4())},
        {"review_revision": body["review_revision"] + 1},
        {"evidence": {**body["evidence"], "validation_sha256": "0" * 64}},
    ):
        post_delivery(client, candidate, {**body, "request_id": str(uuid4()), **changes}, expected=409)
        assert records(client, candidate.task) == []
        assert not (tmp_path / "deliveries").exists()
        assert files(candidate.repo, include_git=True) == before


@pytest.mark.parametrize("legacy", [False, True], ids=["default-policy", "legacy-fallback"])
def test_delivery_blocks_test_changes_even_with_accepted_review(client, make_candidate, tmp_path, legacy):
    candidate = make_candidate(protected=True, legacy=legacy)
    body = delivery_body(client, candidate)
    before = files(candidate.repo, include_git=True)

    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False
    assert "protected" in state["reason"] and "tests/test_value.py" in state["reason"]
    assert state["policy"] == PathPolicy().model_dump()
    post_delivery(client, candidate, body, expected=409)

    assert files(candidate.repo, include_git=True) == before
    assert records(client, candidate.task) == []
    assert not (tmp_path / "deliveries").exists()


@pytest.mark.parametrize("conflict", ["branch", "directory"])
def test_preexisting_destination_is_never_adopted_or_overwritten(client, make_candidate, tmp_path, conflict):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    plan = delivery_state(client, candidate)
    if conflict == "branch":
        git(candidate.repo, "branch", plan["branch"], candidate.task.base_sha)
    else:
        destination = Path(plan["worktree_path"])
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_text("belongs to the user\n", encoding="utf-8")
    before = files(candidate.repo, include_git=True)
    output_before = files(tmp_path / "deliveries", include_git=True)

    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False and "exists" in state["reason"]
    post_delivery(client, candidate, body, expected=409)

    assert files(candidate.repo, include_git=True) == before
    assert files(tmp_path / "deliveries", include_git=True) == output_before
    assert records(client, candidate.task) == []


def test_foreign_worktree_appearing_during_preparation_is_retained_unchanged(client, make_candidate, tmp_path, monkeypatch):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    plan = delivery_state(client, candidate)
    destination = Path(plan["worktree_path"])
    foreign = tmp_path / "unrelated-repository"
    shutil.copytree(candidate.repo, foreign)
    before = source_snapshot(candidate.repo)
    before_branches = branches(candidate.repo)
    original_git = delivery_module.git
    collision = {}

    def create_collision_after_commit(repo, *args, **kwargs):
        result = original_git(repo, *args, **kwargs)
        if args[0] == "commit-tree" and not collision:
            destination.parent.mkdir(parents=True)
            git(foreign, "worktree", "add", "--detach", str(destination), candidate.task.base_sha)
            (destination / "keep.txt").write_text("unrelated worktree notes\n", encoding="utf-8")
            collision["foreign"] = files(foreign, include_git=True)
            collision["worktree"] = files(destination, include_git=True)
        return result

    monkeypatch.setattr(delivery_module, "git", create_collision_after_commit)
    response = post_delivery(client, candidate, body)

    assert collision
    record = response["delivery"]
    assert record["status"] == "failed" and record["error"]
    assert source_snapshot(candidate.repo) == before
    assert branches(candidate.repo) == before_branches
    assert files(foreign, include_git=True) == collision["foreign"]
    assert files(destination, include_git=True) == collision["worktree"]
    assert post_delivery(client, candidate, body, expected=200) == {"delivery": record, "replayed": True}
    assert records(client, candidate.task) == [record]
    assert files(foreign, include_git=True) == collision["foreign"]
    assert files(destination, include_git=True) == collision["worktree"]


@pytest.mark.parametrize("change", ["rejected", "evidence"])
def test_final_publish_rechecks_approval_and_retains_failed_worktree(client, make_candidate, monkeypatch, change):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    original_git = delivery_module.git
    changed = []
    before = source_snapshot(candidate.repo)
    before_branches = branches(candidate.repo)
    store = client.app.state.task_store

    def change_after_checkout(repo, *args, **kwargs):
        result = original_git(repo, *args, **kwargs)
        if args[:3] == ("read-tree", "--reset", "-u") and not changed:
            preparing = records(client, candidate.task)
            assert len(preparing) == 1 and preparing[0]["status"] == "preparing"
            assert Path(preparing[0]["worktree_path"]).is_dir()
            if change == "rejected":
                current = store.get_review(candidate.task.task_id)
                store.submit_review(candidate.task.task_id, ReviewSubmission(
                    request_id=uuid4(), expected_revision=current.revision,
                    decision="rejected", evidence=current.evidence,
                ))
            else:
                path = Path(candidate.task.artifacts_dir) / "validation.json"
                path.write_bytes(path.read_bytes() + b"\n")
            changed.append(True)
        return result

    monkeypatch.setattr(delivery_module, "git", change_after_checkout)
    response = post_delivery(client, candidate, body)

    assert changed == [True]
    assert response["replayed"] is False
    record = response["delivery"]
    assert record["status"] == "failed"
    assert "evidence changed" in record["error"]
    assert branches(candidate.repo) == before_branches
    assert source_snapshot(candidate.repo) == before
    worktree = Path(record["worktree_path"])
    assert rev(worktree) == record["commit_sha"]
    assert (worktree / "src/value.py").read_bytes() == b"value = 2\n"
    (worktree / "keep.txt").write_text("retain my inspection notes\n", encoding="utf-8")
    retained = files(worktree, include_git=True)
    for request_id in (body["request_id"], str(uuid4())):
        replay = post_delivery(client, candidate, {**body, "request_id": request_id}, expected=200)
        assert replay == {"delivery": record, "replayed": True}
        assert files(worktree, include_git=True) == retained
    assert records(client, candidate.task) == [record]
    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False and state["delivery"]["status"] == "failed"
    assert branches(candidate.repo) == before_branches


def test_concurrent_services_publish_only_one_record_and_branch(client, make_candidate, tmp_path, monkeypatch):
    candidate = make_candidate()
    first_body = delivery_body(client, candidate)
    second_body = {**first_body, "request_id": str(uuid4())}
    prepared, release = Event(), Event()
    original_git = delivery_module.git

    def pause_after_checkout(repo, *args, **kwargs):
        result = original_git(repo, *args, **kwargs)
        if args[:3] == ("read-tree", "--reset", "-u"):
            prepared.set()
            assert release.wait(10), "concurrent delivery did not release the first publisher"
        return result

    monkeypatch.setattr(delivery_module, "git", pause_after_checkout)
    with TestClient(create_app(client.app.state.task_store.path, deliveries_root=tmp_path / "deliveries")) as other:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(post_delivery, client, candidate, first_body)
            try:
                assert prepared.wait(10), "first delivery never prepared a worktree"
                assert delivery_state(other, candidate)["delivery"]["status"] == "preparing"
                response = post_delivery(other, candidate, second_body, expected=409)
                assert "running" in response["detail"]
            finally:
                release.set()
            first = pending.result(timeout=15)
        record = assert_completed(client, candidate, first)
        assert post_delivery(other, candidate, second_body, expected=200) == {"delivery": record, "replayed": True}

    assert branches(candidate.repo) == sorted([
        f"refs/heads/main {candidate.task.base_sha}".encode(),
        f"refs/heads/{record['branch']} {record['commit_sha']}".encode(),
    ])
    assert records(client, candidate.task) == [record]
    assert git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ") == 2


def test_delivery_respects_database_lock_held_by_another_process(client, make_candidate):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    before = files(candidate.repo, include_git=True)
    db_path = client.app.state.task_store.path
    lock_path = db_path.with_name(db_path.name + ".delivery.lock")
    script = (
        "import fcntl, sys\n"
        "with open(sys.argv[1], 'a') as lock:\n"
        "    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read(1)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(lock_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "child did not acquire delivery lock"
        assert process.stdout.readline() == b"locked\n"
        response = post_delivery(client, candidate, body, expected=409)
        assert "running" in response["detail"]
        assert records(client, candidate.task) == []
        assert files(candidate.repo, include_git=True) == before
    finally:
        try:
            process.communicate(input=b"x", timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert process.returncode == 0
    assert_completed(client, candidate, post_delivery(client, candidate, body))


def test_preparing_record_recovers_already_published_commit_after_crash(client, make_candidate, tmp_path, monkeypatch):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    original_git = delivery_module.git
    published_ref = f"refs/heads/patchloop/{candidate.task.task_id}/r{body['review_revision']}"

    class SimulatedCrash(BaseException):
        pass

    def crash_after_ref_publication(repo, *args, **kwargs):
        result = original_git(repo, *args, **kwargs)
        if args[0] == "update-ref" and published_ref in args[1:]:
            raise SimulatedCrash("published ref before completion was persisted")
        return result

    with monkeypatch.context() as fault:
        fault.setattr(delivery_module, "git", crash_after_ref_publication)
        with pytest.raises(SimulatedCrash):
            client.app.state.delivery_service.deliver(candidate.task.task_id, DeliverySubmission.model_validate(body))
    preparing, = records(client, candidate.task)
    assert preparing["status"] == "preparing" and preparing["commit_sha"]
    assert rev(candidate.repo, f"refs/heads/{preparing['branch']}") == preparing["commit_sha"]
    worktree = Path(preparing["worktree_path"])
    assert rev(worktree) == preparing["commit_sha"]
    before_branches = branches(candidate.repo)
    before_worktrees = git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ")
    source_before = source_snapshot(candidate.repo)

    def must_recover(*args, **kwargs):
        pytest.fail("recovery must not prepare a second commit or worktree")

    monkeypatch.setattr(delivery_module.DeliveryService, "_prepare_and_publish", must_recover)
    with TestClient(create_app(client.app.state.task_store.path, deliveries_root=tmp_path / "deliveries")) as restarted:
        response = post_delivery(restarted, candidate, body, expected=200)
        assert response["replayed"] is True
        record = assert_completed(restarted, candidate, response)
        assert record["delivery_id"] == preparing["delivery_id"]
        assert record["commit_sha"] == preparing["commit_sha"]
        assert record["created_at"] == preparing["created_at"]
        assert delivery_state(restarted, candidate)["delivery_intact"] is True
    assert branches(candidate.repo) == before_branches
    assert git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ") == before_worktrees == 2
    assert source_snapshot(candidate.repo) == source_before


@pytest.mark.parametrize("change", ["branch", "dirty-worktree", "detached-worktree"])
def test_modified_completed_output_reports_not_intact_and_replay_never_resets(client, make_candidate, change):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    record = assert_completed(client, candidate, post_delivery(client, candidate, body))
    worktree = Path(record["worktree_path"])
    if change == "branch":
        git(candidate.repo, "update-ref", f"refs/heads/{record['branch']}", candidate.task.base_sha, record["commit_sha"])
    elif change == "dirty-worktree":
        (worktree / "src/value.py").write_text("user edits after delivery\n", encoding="utf-8")
        (worktree / "keep.txt").write_text("untracked notes\n", encoding="utf-8")
    else:
        git(worktree, "checkout", "--detach", candidate.task.base_sha)
    output_before = files(worktree, include_git=True)
    repo_before = files(candidate.repo, include_git=True)

    state = delivery_state(client, candidate)
    assert state["delivery"] == record
    assert state["delivery_intact"] is False and state["can_deliver"] is False
    assert "changed" in state["reason"]
    for request_id in (body["request_id"], str(uuid4())):
        replay = post_delivery(client, candidate, {**body, "request_id": request_id}, expected=200)
        assert replay == {"delivery": record, "replayed": True}
        assert files(worktree, include_git=True) == output_before
        assert files(candidate.repo, include_git=True) == repo_before
    assert records(client, candidate.task) == [record]


def test_delivery_never_executes_repository_hooks_filters_or_fsmonitor(client, make_candidate, tmp_path):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    marker = tmp_path / "must-not-run"
    executable = tmp_path / "repository-command"
    executable.write_text(f"#!/bin/sh\nprintf invoked >> {shlex.quote(str(marker))}\nexit 97\n", encoding="utf-8")
    executable.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout", "reference-transaction"):
        shutil.copy2(executable, hooks / name)
    git(candidate.repo, "config", "core.hooksPath", str(hooks))
    git(candidate.repo, "config", "core.fsmonitor", str(executable))
    for operation in ("clean", "smudge", "process"):
        git(candidate.repo, "config", f"filter.sentinel.{operation}", shlex.quote(str(executable)))
    git(candidate.repo, "config", "filter.sentinel.required", "true")
    attributes = candidate.repo / ".git/info/attributes"
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text("*.py filter=sentinel\n", encoding="utf-8")
    before_files = files(candidate.repo)
    before_index = (candidate.repo / ".git/index").read_bytes()

    assert delivery_state(client, candidate)["can_deliver"] is True
    response = post_delivery(client, candidate, body)

    assert response["delivery"]["status"] == "completed", response
    assert response["delivery"]["tree_sha"] == candidate.tree
    assert not marker.exists(), "delivery executed repository-controlled code"
    assert delivery_state(client, candidate)["delivery_intact"] is True
    assert not marker.exists(), "integrity inspection executed repository-controlled code"
    assert files(candidate.repo) == before_files
    assert (candidate.repo / ".git/index").read_bytes() == before_index


@pytest.fixture
def submodules(repository, tmp_path):
    origin = tmp_path / "module-origin"
    origin.mkdir()
    git(origin, "init", "-q", "--template=", "--initial-branch=main")
    git(origin, "config", "user.name", "test")
    git(origin, "config", "user.email", "test@example.com")
    (origin / "payload.txt").write_bytes(b"old content\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "module base")

    outer_path = "modules/outer space"
    nested_path = "nested\tmodule"
    outer_origin = tmp_path / "outer-origin"
    shutil.copytree(origin, outer_origin)
    git(outer_origin, "-c", "protocol.file.allow=always", "submodule", "add",
        "--name", "nested", str(origin), nested_path)
    git(outer_origin, "commit", "-qam", "nested module")
    for name, path, remote in (("outer", outer_path, outer_origin),
                               ("uninitialized", "modules/uninitialized", origin)):
        git(repository, "-c", "protocol.file.allow=always", "submodule", "add",
            "--name", name, str(remote), path)
    outer = repository / outer_path
    git(outer, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "modules")
    git(repository, "submodule", "deinit", "-f", "--", "modules/uninitialized")
    return Path(outer_path), Path(outer_path) / nested_path


def arm_submodule_filter(module, tmp_path):
    marker = tmp_path / "submodule-command-ran"
    executable = tmp_path / "submodule-command"
    executable.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n", encoding="utf-8")
    executable.chmod(0o755)
    # This driver exists only in this module's config, not its parent's.
    for operation in ("clean", "smudge"):
        git(module, "config", f"filter.module_only.{operation}", shlex.quote(str(executable)))
    git(module, "config", "filter.module_only.required", "true")
    git(module, "config", "core.fsmonitor", str(executable))
    attributes = Path(os.fsdecode(git(module, "rev-parse", "--path-format=absolute",
                                     "--git-path", "info/attributes").removesuffix(b"\n")))
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text("payload.txt filter=module_only\n", encoding="utf-8")
    payload = module / "payload.txt"
    stat = payload.stat()
    payload.write_bytes(b"new content\n")
    assert payload.stat().st_size == stat.st_size
    os.utime(payload, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    return marker


@pytest.mark.parametrize("nested", [False, True])
def test_delivery_detects_dirty_submodules_without_filters_or_source_writes(
    client, make_candidate, submodules, tmp_path, nested,
):
    candidate = make_candidate()
    before = files(candidate.repo, include_git=True)
    state = delivery_state(client, candidate)
    assert state["can_deliver"] is True and state["dirty"] is False, state
    assert files(candidate.repo, include_git=True) == before
    assert not (candidate.repo / "modules/uninitialized/.git").exists()

    module = candidate.repo / submodules[int(nested)]
    marker = arm_submodule_filter(module, tmp_path)
    before = files(candidate.repo, include_git=True)
    # This root status must not visit even a nested module's working files.
    assert delivery_module.git(candidate.repo, "status", "--porcelain=v1", "-z",
                               "--ignore-submodules=dirty", "--untracked-files=all").stdout == b""
    assert not marker.exists()
    state = delivery_state(client, candidate)
    assert not marker.exists(), "GET delivery ran a submodule's clean filter"
    assert state["can_deliver"] is True and state["dirty"] is True, state
    assert state["changed_paths"] == candidate.changed_paths
    assert files(candidate.repo, include_git=True) == before

    response = post_delivery(client, candidate, delivery_body(client, candidate))
    assert_completed(client, candidate, response)
    assert delivery_state(client, candidate)["dirty"] is True
    assert not marker.exists(), "delivery ran a submodule command"
    after = files(candidate.repo, include_git=True)
    assert {name: after[name] for name in before} == before


def test_delivery_integrity_checks_initialized_nested_submodules_safely(
    client, make_candidate, submodules, tmp_path,
):
    candidate = make_candidate()
    record = assert_completed(client, candidate, post_delivery(client, candidate, delivery_body(client, candidate)))
    worktree = Path(record["worktree_path"])
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    assert delivery_state(client, candidate)["delivery_intact"] is True
    marker = arm_submodule_filter(worktree / submodules[1], tmp_path)
    source_before = files(candidate.repo, include_git=True)
    output_before = files(worktree, include_git=True)

    state = delivery_state(client, candidate)

    assert not marker.exists(), "integrity inspection ran a nested submodule filter"
    assert state["delivery_intact"] is False and state["dirty"] is False, state
    assert files(candidate.repo, include_git=True) == source_before
    assert files(worktree, include_git=True) == output_before


def test_delivery_final_check_rejects_dirty_nested_submodule_without_running_filter(
    client, make_candidate, submodules, tmp_path, monkeypatch,
):
    candidate = make_candidate()
    original_git = delivery_module.git
    observed = {}
    branches_before = branches(candidate.repo)

    def initialize_dirty_module(repo, *args, **kwargs):
        result = original_git(repo, *args, **kwargs)
        if args[:2] == ("read-tree", "--reset"):
            git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
            observed["marker"] = arm_submodule_filter(repo / submodules[1], tmp_path)
            observed["source"] = files(candidate.repo, include_git=True)
            observed["output"] = files(repo, include_git=True)
        return result

    monkeypatch.setattr(delivery_module, "git", initialize_dirty_module)
    response = post_delivery(client, candidate, delivery_body(client, candidate))

    assert not observed["marker"].exists(), "final worktree inspection ran a submodule filter"
    record = response["delivery"]
    assert record["status"] == "failed" and "incomplete or changed" in record["error"], record
    assert branches(candidate.repo) == branches_before
    assert files(candidate.repo, include_git=True) == observed["source"]
    assert files(Path(record["worktree_path"]), include_git=True) == observed["output"]


def test_client_cannot_override_server_managed_delivery_paths(client, make_candidate, tmp_path):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    outside = tmp_path / "client-chosen-output"
    before = files(candidate.repo, include_git=True)

    for field in ("deliveries_root", "worktree_path", "branch"):
        post_delivery(client, candidate, {**body, field: str(outside)}, expected=422)

    assert not outside.exists()
    assert not (tmp_path / "deliveries").exists()
    assert records(client, candidate.task) == []
    assert files(candidate.repo, include_git=True) == before


def test_task_api_defaults_and_normalizes_policy_persisted_across_restart(client, repository, tmp_path):
    payload = {
        "repo_path": str(repository), "goal": "freeze the task path policy",
        "verification_command": "python -m pytest -q",
        "execution": {"image": "offline-delivery-fixture", "model": "offline-fixture"},
    }
    default_response = client.post("/tasks", json=payload)
    assert default_response.status_code == 201, default_response.text
    default = default_response.json()
    assert default["path_policy"]["allowed_paths"] == []
    assert "tests/**" in default["path_policy"]["protected_paths"]
    assert "**/tests/**" in default["path_policy"]["protected_paths"]

    explicit_response = client.post("/tasks", json={
        **payload, "path_policy": {
            "allowed_paths": ["src/", "src/**", "docs/", "src/"],
            "protected_paths": ["tests/", "tests/**", "**/tests/", "tests/"],
        },
    })
    assert explicit_response.status_code == 201, explicit_response.text
    explicit = explicit_response.json()
    assert explicit["path_policy"] == {
        "allowed_paths": ["src/**", "docs/**"],
        "protected_paths": ["tests/**", "**/tests/**"],
    }
    store = client.app.state.task_store
    repair_tasks = {}
    for submitted in (default, explicit):
        task = TaskRecord.model_validate(submitted)
        saved = store.get(task.task_id)
        assert saved == task
        repair_task = saved.to_repair_task()
        assert repair_task.allowed_paths == tuple(submitted["path_policy"]["allowed_paths"])
        assert repair_task.protected_paths == tuple(submitted["path_policy"]["protected_paths"])
        repair_tasks[submitted["task_id"]] = repair_task
        with store._connection() as connection:
            row = connection.execute(
                "SELECT path_policy_json FROM tasks WHERE task_id=?", (submitted["task_id"],),
            ).fetchone()
            assert json.loads(row["path_policy_json"]) == submitted["path_policy"]

    with TestClient(create_app(store.path, deliveries_root=tmp_path / "deliveries")) as restarted:
        for submitted in (default, explicit):
            response = restarted.get(f"/tasks/{submitted['task_id']}")
            assert response.status_code == 200, response.text
            assert response.json() == submitted
            task = restarted.app.state.task_store.get(TaskRecord.model_validate(submitted).task_id)
            assert task.to_repair_task() == repair_tasks[submitted["task_id"]]


def test_v5_accepted_review_migrates_without_changing_historical_evidence_or_old_columns(client, make_candidate, tmp_path):
    candidate = make_candidate(legacy=True)
    task = candidate.task
    store = client.app.state.task_store
    artifacts = Path(task.artifacts_dir)
    artifacts_before = files(artifacts)
    repo_before = files(candidate.repo, include_git=True)
    review_before = client.get(f"/tasks/{task.task_id}/review").json()
    history_before = client.get(f"/tasks/{task.task_id}/reviews").json()

    # Reconstruct the v5 hash independently: the old record has no policy key.
    historical_record = task.model_dump(mode="json")
    assert historical_record.pop("path_policy") is None
    historical_bytes = json.dumps(
        historical_record, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    historical_evidence = {
        "base_sha": task.base_sha,
        "record_sha256": hashlib.sha256(historical_bytes).hexdigest(),
        **{field: hashlib.sha256((artifacts / name).read_bytes()).hexdigest() for name, field in (
            ("patch.diff", "patch_sha256"), ("task.json", "task_sha256"),
            ("summary.json", "summary_sha256"), ("validation.json", "validation_sha256"),
        )},
    }
    assert review_before["status"] == "accepted"
    assert review_before["evidence"] == historical_evidence
    assert review_before["latest_review"]["evidence"] == historical_evidence

    # Only this test's temporary database is downgraded to the v5 shape.
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE task_deliveries")
        connection.execute("ALTER TABLE tasks DROP COLUMN path_policy_json")
        connection.execute("PRAGMA user_version=5")
        old_columns = [row["name"] for row in connection.execute("PRAGMA table_info(tasks)")]
        assert "path_policy_json" not in old_columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        old_tasks = [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY task_id")]
        old_reviews = [dict(row) for row in connection.execute("SELECT * FROM task_reviews ORDER BY review_id")]

    with TestClient(create_app(store.path, deliveries_root=tmp_path / "deliveries")) as restarted:
        migrated_store = restarted.app.state.task_store
        with migrated_store._connection() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
            assert [row["name"] for row in connection.execute("PRAGMA table_info(tasks)")] == [
                *old_columns, "path_policy_json",
            ]
            migrated_tasks = [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY task_id")]
            for row in migrated_tasks:
                assert row.pop("path_policy_json") is None
            assert migrated_tasks == old_tasks
            assert [dict(row) for row in connection.execute(
                "SELECT * FROM task_reviews ORDER BY review_id",
            )] == old_reviews
            assert connection.execute("SELECT count(*) FROM task_deliveries").fetchone()[0] == 0

        response = restarted.get(f"/tasks/{task.task_id}/review")
        assert response.status_code == 200, response.text
        assert response.json() == review_before
        assert response.json()["status"] == "accepted"
        assert response.json()["can_accept"] is True
        assert response.json()["evidence"] == historical_evidence
        history = restarted.get(f"/tasks/{task.task_id}/reviews")
        assert history.status_code == 200 and history.json() == history_before
        detail = restarted.get(f"/tasks/{task.task_id}")
        assert detail.status_code == 200, detail.text
        migrated_record = detail.json()
        assert migrated_record.pop("path_policy") is None
        assert migrated_record == historical_record
        assert migrated_store.get(task.task_id) == task

    assert files(artifacts) == artifacts_before
    assert files(candidate.repo, include_git=True) == repo_before


def test_dangling_symbolic_delivery_ref_reserves_branch_without_creating_its_target(client, make_candidate, tmp_path):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    plan = delivery_state(client, candidate)
    delivery_ref = f"refs/heads/{plan['branch']}"
    reserved_ref = "refs/heads/user-reserved"
    git(candidate.repo, "symbolic-ref", delivery_ref, reserved_ref)
    before = files(candidate.repo, include_git=True)

    state = delivery_state(client, candidate)
    assert state["can_deliver"] is False and "exists" in state["reason"]
    post_delivery(client, candidate, body, expected=409)

    assert git(candidate.repo, "symbolic-ref", "-q", delivery_ref).decode().strip() == reserved_ref
    assert git(candidate.repo, "for-each-ref", "--format=%(refname)", reserved_ref) == b""
    assert not (candidate.repo / ".git" / reserved_ref).exists()
    assert files(candidate.repo, include_git=True) == before
    assert records(client, candidate.task) == []
    assert not (tmp_path / "deliveries").exists()


def test_dangling_symbolic_ref_inserted_at_publish_is_not_overwritten_or_dereferenced(client, make_candidate, monkeypatch):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    plan = delivery_state(client, candidate)
    delivery_ref = f"refs/heads/{plan['branch']}"
    reserved_ref = "refs/heads/user-reserved"
    original_git = delivery_module.git
    source_before = source_snapshot(candidate.repo)
    attempted = []
    reserved_state = {}

    def insert_ref_before_publication(repo, *args, **kwargs):
        if args[0] == "update-ref" and delivery_ref in args[1:]:
            preparing, = records(client, candidate.task)
            assert preparing["status"] == "preparing" and preparing["commit_sha"]
            assert git(Path(preparing["worktree_path"]), "write-tree").decode().strip() == candidate.tree
            git(candidate.repo, "symbolic-ref", delivery_ref, reserved_ref)
            reserved_state.update(files(candidate.repo / ".git/refs", include_git=True))
            attempted.append(args)
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(delivery_module, "git", insert_ref_before_publication)
    response = post_delivery(client, candidate, body)

    assert len(attempted) == 1
    assert "--no-deref" in attempted[0]
    assert attempted[0][-1] == "0" * len(candidate.task.base_sha)
    record = response["delivery"]
    assert record["status"] == "failed" and record["error"], record
    assert git(candidate.repo, "symbolic-ref", "-q", delivery_ref).decode().strip() == reserved_ref
    assert git(candidate.repo, "for-each-ref", "--format=%(refname)", reserved_ref) == b""
    assert not (candidate.repo / ".git" / reserved_ref).exists()
    assert files(candidate.repo / ".git/refs", include_git=True) == reserved_state
    assert source_snapshot(candidate.repo) == source_before
    assert rev(Path(record["worktree_path"])) == record["commit_sha"]
    assert records(client, candidate.task) == [record]


def test_retry_fills_only_owned_empty_worktree_after_add_crash_and_preserves_user_files(client, make_candidate, tmp_path, monkeypatch):
    original_git = delivery_module.git
    for add_user_file in (False, True):
        candidate = make_candidate()
        body = delivery_body(client, candidate)
        source_before = source_snapshot(candidate.repo)
        branches_before = branches(candidate.repo)
        worktree_count = git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ")

        def crash_after_worktree_add(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "add") and "--no-checkout" in args:
                raise SystemExit("crash after worktree add, before read-tree")
            return result

        with monkeypatch.context() as fault:
            fault.setattr(delivery_module, "git", crash_after_worktree_add)
            with pytest.raises(SystemExit, match="before read-tree"):
                client.app.state.delivery_service.deliver(candidate.task.task_id, DeliverySubmission.model_validate(body))

        preparing, = records(client, candidate.task)
        assert preparing["status"] == "preparing"
        assert preparing["tree_sha"] == candidate.tree and preparing["commit_sha"]
        worktree = Path(preparing["worktree_path"])
        assert {entry.name for entry in worktree.iterdir()} == {".git"}
        assert rev(worktree) == preparing["commit_sha"]
        assert git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir") == git(
            candidate.repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
        )
        index = Path(os.fsdecode(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index").removesuffix(b"\n")))
        assert not index.exists()
        if add_user_file:
            (worktree / "src").mkdir()
            (worktree / "src/value.py").write_bytes(b"user content colliding with the patch\n")
        retained = files(worktree, include_git=True)
        checkouts = []

        def observe_resumed_checkout(repo, *args, **kwargs):
            if args[0] == "commit-tree" or args[:2] == ("worktree", "add"):
                pytest.fail("retry must reuse its recorded commit and existing worktree")
            if args[0] == "read-tree":
                checkouts.append(args)
            return original_git(repo, *args, **kwargs)

        with monkeypatch.context() as observer:
            observer.setattr(delivery_module, "git", observe_resumed_checkout)
            with TestClient(create_app(client.app.state.task_store.path, deliveries_root=tmp_path / "deliveries")) as restarted:
                response = post_delivery(restarted, candidate, body)
                record = response["delivery"]
                if add_user_file:
                    assert record["status"] == "failed" and record["error"], record
                    assert checkouts == []
                    assert not index.exists()
                    assert files(worktree, include_git=True) == retained
                    assert branches(candidate.repo) == branches_before
                else:
                    assert_completed(restarted, candidate, response)
                    assert len(checkouts) == 1
                    assert index.is_file()
                    assert (worktree / "src/value.py").read_bytes() == b"value = 2\n"
                    assert delivery_state(restarted, candidate)["delivery_intact"] is True
                assert record["delivery_id"] == preparing["delivery_id"]
                assert record["commit_sha"] == preparing["commit_sha"]
                assert record["worktree_path"] == preparing["worktree_path"]
                assert record["created_at"] == preparing["created_at"]
                assert records(restarted, candidate.task) == [record]

        assert git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ") == worktree_count + 1
        assert source_snapshot(candidate.repo) == source_before


def test_preparing_delivery_rejects_changed_root_until_original_root_is_restored(client, make_candidate, tmp_path, monkeypatch):
    candidate = make_candidate()
    body = delivery_body(client, candidate)
    original_git = delivery_module.git
    source_before = source_snapshot(candidate.repo)

    def crash_before_worktree_add(repo, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise SystemExit("crash with recorded commit, before worktree creation")
        return original_git(repo, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(delivery_module, "git", crash_before_worktree_add)
        with pytest.raises(SystemExit, match="before worktree creation"):
            client.app.state.delivery_service.deliver(candidate.task.task_id, DeliverySubmission.model_validate(body))

    preparing, = records(client, candidate.task)
    assert preparing["status"] == "preparing"
    assert preparing["tree_sha"] == candidate.tree and preparing["commit_sha"]
    assert rev(candidate.repo, f"{preparing['commit_sha']}^{{tree}}") == candidate.tree
    destination = Path(preparing["worktree_path"])
    assert not destination.exists()
    repo_before = files(candidate.repo, include_git=True)
    changed_root = tmp_path / "changed-deliveries"

    with TestClient(create_app(client.app.state.task_store.path, deliveries_root=changed_root)) as relocated:
        state = delivery_state(relocated, candidate)
        assert state["can_deliver"] is False and "configuration changed" in state["reason"]
        assert state["delivery"] == preparing
        response = post_delivery(relocated, candidate, body, expected=409)
        assert "configuration changed" in response["detail"]
        assert records(relocated, candidate.task) == [preparing]
    assert not changed_root.exists()
    assert not destination.exists()
    assert files(candidate.repo, include_git=True) == repo_before

    def reuse_recorded_commit(repo, *args, **kwargs):
        if args[0] == "commit-tree":
            pytest.fail("restoring the original root must reuse the persisted commit")
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(delivery_module, "git", reuse_recorded_commit)
    with TestClient(create_app(client.app.state.task_store.path, deliveries_root=tmp_path / "deliveries")) as restored:
        record = assert_completed(restored, candidate, post_delivery(restored, candidate, body))
        assert record["delivery_id"] == preparing["delivery_id"]
        assert record["commit_sha"] == preparing["commit_sha"]
        assert record["worktree_path"] == preparing["worktree_path"]
        assert record["created_at"] == preparing["created_at"]
        assert delivery_state(restored, candidate)["delivery_intact"] is True
    assert not changed_root.exists()
    assert destination.is_dir()
    assert git(candidate.repo, "worktree", "list", "--porcelain").count(b"worktree ") == 2
    assert source_snapshot(candidate.repo) == source_before
