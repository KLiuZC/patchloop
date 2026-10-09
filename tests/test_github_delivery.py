"""PR preview checks use temporary Git repositories and SQLite, with no publication."""

import hashlib
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api import github_delivery as preview_module
from patchloop.api.models import TaskSource
from test_delivery import (client, repository, make_candidate, decide, delivery_body,
                           post_delivery, rev, git, files, source_snapshot, branches)
from test_repository_store import make_source


@pytest.fixture(autouse=True)
def forbid_external_execution(monkeypatch):
    from patchloop.llm import LLM
    from patchloop.repair import RepairRunner
    from patchloop.sandbox import DockerRunner, LocalRunner

    def forbidden(*args, **kwargs):
        pytest.fail("PR preview must not use the network, models, runners, or Docker")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(LLM, "__init__", forbidden)
    monkeypatch.setattr(RepairRunner, "run", forbidden)
    monkeypatch.setattr(DockerRunner, "start", forbidden)
    monkeypatch.setattr(LocalRunner, "run_result", forbidden)
    original_run = subprocess.run

    def local_only(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command[0] == "git":
            assert not {"push", "fetch", "pull", "clone", "ls-remote"}.intersection(command)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_only)


@pytest.fixture
def candidate_factory(make_candidate, repository):
    def make(*, with_source=True, decision="accepted", title=None, legacy=False):
        issue, _ = make_source()
        if title is not None:
            issue.title = title
        source = TaskSource(
            preparation_id=uuid4(), issue=issue, repo_path=str(repository), base_sha=rev(repository),
            resolved_ref="main", resolved_at=datetime.now(timezone.utc),
        ) if with_source else None
        return make_candidate(source=source, decision=decision, legacy=legacy)
    return make


@pytest.fixture
def delivered(client, candidate_factory):
    candidate = candidate_factory()
    result = post_delivery(client, candidate, delivery_body(client, candidate))
    assert result["delivery"]["status"] == "completed"
    return candidate, result["delivery"]


def preview(client, candidate, **changes):
    return client.post(f"/tasks/{candidate.task.task_id}/github-delivery/preview",
                       json={"base_branch": "main", **changes})


def dump_database(client):
    with client.app.state.task_store._connection() as connection:
        return list(connection.iterdump())


def update_delivery(client, record, **changes):
    changed = {**record, **changes}
    with client.app.state.task_store._connection() as connection:
        connection.execute("UPDATE task_deliveries SET record_json=? WHERE delivery_id=?",
                           (json.dumps(changed), record["delivery_id"]))
    return changed


def test_preview_binds_real_commit_and_evidence_without_mutating_anything(client, delivered):
    candidate, record = delivered
    worktree = Path(record["worktree_path"])
    before = (dump_database(client), source_snapshot(candidate.repo), files(candidate.repo, include_git=True),
              files(worktree), branches(candidate.repo), files(Path(candidate.task.artifacts_dir)))
    response = preview(client, candidate)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "preview" and value["publish_enabled"] is False
    assert value["remote_checks"] == "not_checked"
    plan = value["plan"]
    assert plan["task_id"] == str(candidate.task.task_id)
    assert plan["source_id"] == str(candidate.task.source.issue.source_id)
    assert plan["preparation_id"] == str(candidate.task.source.preparation_id)
    assert plan["repository"] == candidate.task.source.issue.repository.model_dump(mode="json")
    assert plan["issue_url"] == candidate.task.source.issue.url
    assert plan["review_id"] == record["review_id"] and plan["review_revision"] == 1
    assert plan["delivery_id"] == record["delivery_id"]
    assert plan["evidence"] == record["evidence"]
    assert plan["base_branch"] == "main" and plan["base_sha"] == candidate.task.base_sha
    assert plan["head_branch"] == record["branch"] and plan["head_sha"] == record["commit_sha"]
    assert plan["tree_sha"] == candidate.tree
    assert plan["changed_paths"] == candidate.changed_paths
    assert plan["verification"] == {"command": candidate.task.verification_command,
        "baseline_status": "failed", "baseline_exit_code": 1, "candidate_status": "passed", "candidate_exit_code": 0}
    assert plan["draft"] is True
    assert plan["title"] == "修复 #42：保留历史问题"
    for content in [candidate.task.goal, candidate.task.verification_command, plan["issue_url"], "退出码 1", "退出码 0"]:
        assert content in plan["body"]
    assert str(candidate.repo) not in plan["body"] and str(worktree) not in plan["body"]
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert value["plan_sha256"] == hashlib.sha256(encoded).hexdigest()
    after = (dump_database(client), source_snapshot(candidate.repo), files(candidate.repo, include_git=True),
             files(worktree), branches(candidate.repo), files(Path(candidate.task.artifacts_dir)))
    assert after == before


def test_repeat_and_restart_are_read_only_and_keep_the_plan_fingerprint(client, delivered):
    candidate, _ = delivered
    first = preview(client, candidate).json()
    second = preview(client, candidate).json()
    assert first["plan"] == second["plan"]
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["checked_at"] != second["checked_at"]
    with TestClient(create_app(client.app.state.task_store.path)) as restarted:
        restored = preview(restarted, candidate)
        assert restored.status_code == 200, restored.text
        assert restored.json()["plan_sha256"] == first["plan_sha256"]


@pytest.mark.parametrize("changes", [
    {"title": "  手工调整标题 🐛  "}, {"body": "## 修改说明\n\n保留我的原文。\n"},
    {"base_branch": "release/1.x"}, {"draft": False},
])
def test_edits_change_fingerprint_but_not_the_approved_patch(client, delivered, changes):
    candidate, _ = delivered
    original = preview(client, candidate).json()
    response = preview(client, candidate, **changes)
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["plan_sha256"] != original["plan_sha256"]
    assert {key: edited["plan"][key] for key in changes} == changes
    for key in ["head_sha", "tree_sha", "evidence", "delivery_id", "review_id"]:
        assert edited["plan"][key] == original["plan"][key]
    assert edited["remote_checks"] == "not_checked"


@pytest.mark.parametrize("payload", [
    {}, {"base_branch": ""}, {"base_branch": "-main"}, {"base_branch": "main\nother"},
    {"base_branch": "main..next"}, {"base_branch": "main@{1}"}, {"base_branch": "refs/heads/main"},
    {"base_branch": "HEAD"}, {"base_branch": "x.lock"}, {"base_branch": "a//b"},
    {"base_branch": "a/b "}, {"base_branch": "main", "title": " "},
    {"base_branch": "main", "title": "a\nb"}, {"base_branch": "main", "title": "x" * 257},
    {"base_branch": "main", "body": "\0"}, {"base_branch": "main", "body": "\ud800"},
    {"base_branch": "main", "body": "x" * 60001}, {"base_branch": "main", "draft": "false"},
    {"base_branch": "main", "repository": "other/repo"}, {"base_branch": "main", "head_sha": "a" * 40},
    {"base_branch": "main", "token": "not-a-credential"},
])
def test_invalid_or_retargeted_inputs_are_rejected_before_lookup(client, payload):
    response = client.post(f"/tasks/{uuid4()}/github-delivery/preview",
                           content=json.dumps(payload), headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text


def test_unknown_task_returns_404_and_publication_requires_confirmation(client):
    url = f"/tasks/{uuid4()}/github-delivery"
    assert client.post(url + "/preview", json={"base_branch": "main"}).status_code == 404
    assert client.post(url, json={}).status_code == 422


@pytest.mark.parametrize("decision", [None, "rejected", "accepted"])
def test_current_accepted_review_and_completed_delivery_are_required(client, candidate_factory, decision):
    candidate = candidate_factory(decision=decision)
    response = preview(client, candidate)
    assert response.status_code == 409, response.text


def test_local_task_without_issue_provenance_cannot_choose_a_remote(client, candidate_factory):
    candidate = candidate_factory(with_source=False)
    post_delivery(client, candidate, delivery_body(client, candidate))
    response = preview(client, candidate)
    assert response.status_code == 409 and "GitHub Issue source" in response.text


@pytest.mark.parametrize("decision", ["rejected", "accepted"])
def test_old_delivery_cannot_be_reused_after_a_new_review(client, delivered, decision):
    candidate, _ = delivered
    decide(client, candidate, decision)
    assert preview(client, candidate).status_code == 409


@pytest.mark.parametrize("file", ["task.json", "summary.json", "validation.json", "patch.diff"])
def test_changed_evidence_is_not_previewed(client, delivered, file):
    candidate, _ = delivered
    path = Path(candidate.task.artifacts_dir) / file
    path.write_bytes(path.read_bytes() + b"\n")
    assert preview(client, candidate).status_code == 409


@pytest.mark.parametrize("mutation", ["worktree-dirty", "worktree-missing", "branch-moved", "symbolic-ref", "source-head"])
def test_changed_local_delivery_is_blocked_without_repairing_it(client, delivered, mutation):
    candidate, record = delivered
    worktree = Path(record["worktree_path"])
    if mutation == "worktree-dirty":
        (worktree / "src/value.py").write_text("unreviewed\n")
    elif mutation == "worktree-missing":
        worktree.rename(worktree.with_name("moved"))
    elif mutation == "branch-moved":
        git(candidate.repo, "update-ref", "refs/heads/" + record["branch"], candidate.task.base_sha)
    elif mutation == "symbolic-ref":
        git(candidate.repo, "update-ref", "refs/heads/alias", record["commit_sha"])
        git(candidate.repo, "symbolic-ref", "refs/heads/" + record["branch"], "refs/heads/alias")
    else:
        git(candidate.repo, "commit", "--allow-empty", "-qm", "new base")
    before = files(candidate.repo, include_git=True)
    assert preview(client, candidate).status_code == 409
    assert files(candidate.repo, include_git=True) == before


@pytest.mark.parametrize("changes", [
    {"status": "preparing"}, {"status": "failed", "error": "interrupted"},
    {"commit_sha": None}, {"tree_sha": None}, {"review_revision": 99},
    {"target_head": "a" * 40}, {"task_id": str(uuid4())}, {"branch": "unreviewed"},
])
def test_incomplete_or_mismatched_delivery_records_are_blocked(client, delivered, changes):
    candidate, record = delivered
    update_delivery(client, record, **changes)
    assert preview(client, candidate).status_code == 409


@pytest.mark.parametrize("mutation", ["wrong-parent", "merge-parent", "wrong-tree"])
def test_commit_must_have_exactly_the_approved_parent_and_tree(client, delivered, mutation):
    candidate, record = delivered
    tree = rev(candidate.repo, candidate.task.base_sha + "^{tree}") if mutation == "wrong-tree" else record["tree_sha"]
    parent = record["commit_sha"] if mutation == "wrong-parent" else candidate.task.base_sha
    parents = ["-p", parent]
    if mutation == "merge-parent":
        parents += ["-p", record["commit_sha"]]
    changed_sha = git(candidate.repo, "commit-tree", tree, *parents, input=b"changed commit\n").decode().strip()
    git(Path(record["worktree_path"]), "reset", "--hard", changed_sha)
    update_delivery(client, record, commit_sha=changed_sha, tree_sha=tree)
    response = preview(client, candidate)
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("mutation", ["review", "evidence", "delivery"])
def test_changes_during_inspection_invalidate_the_preview(client, delivered, monkeypatch, mutation):
    candidate, record = delivered
    original = preview_module.inspect_patch

    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == "review":
            decide(client, candidate, "rejected")
        elif mutation == "evidence":
            path = Path(candidate.task.artifacts_dir) / "validation.json"
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            (Path(record["worktree_path"]) / "src/value.py").write_text("concurrent edit\n")
        return result

    monkeypatch.setattr(preview_module, "inspect_patch", racing)
    assert preview(client, candidate).status_code == 409


def test_base_cannot_be_the_delivery_branch(client, delivered):
    candidate, record = delivered
    assert preview(client, candidate, base_branch=record["branch"]).status_code == 409


def test_configured_remote_is_not_used_as_the_publication_target(client, delivered):
    candidate, _ = delivered
    git(candidate.repo, "remote", "add", "origin", "https://example.invalid/unrelated/repo.git")
    response = preview(client, candidate)
    assert response.status_code == 200, response.text
    assert response.json()["plan"]["repository"]["full_name"] == "example/public-repo"


def test_generated_title_is_bounded_and_legacy_path_policy_is_supported(client, candidate_factory):
    candidate = candidate_factory(title="中文标题" * 200, legacy=True)
    post_delivery(client, candidate, delivery_body(client, candidate))
    response = preview(client, candidate)
    assert response.status_code == 200, response.text
    assert len(response.json()["plan"]["title"]) == 256


def test_markdown_fences_preserve_embedded_commands_and_paths():
    value = "line one\n```shell\necho untouched\n```\n````"
    fenced = preview_module._fence(value, "shell")
    assert fenced == f"`````shell\n{value}\n`````"
