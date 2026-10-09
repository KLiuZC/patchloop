"""Publication against a simulated GitHub API and isolated local Git repositories."""

import json
import socket
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api.github_delivery_models import GitHubDeliveryPlan
from patchloop.api.github_publication_remote import GitHubPublicationError, GitHubPublicationRemote
from patchloop.api.github_push import GitHubPusher
from patchloop.api.store import TaskStore
from test_delivery import (client, repository, make_candidate, decide, git, rev, files, source_snapshot,
                           post_delivery, delivery_body)
from test_github_delivery import candidate_factory, delivered, preview


REAL_GIT_RUN = GitHubPusher._run
TOKEN = "test-publication-token"


@pytest.fixture(autouse=True)
def forbid_live_work(monkeypatch):
    from patchloop.llm import LLM
    from patchloop.repair import RepairRunner
    def forbidden(*args, **kwargs):
        pytest.fail("publication tests must not contact real services or call models")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(LLM, "__init__", forbidden)
    monkeypatch.setattr(RepairRunner, "run", forbidden)
    monkeypatch.setattr(GitHubPusher, "_run", forbidden)


class Remote:
    def __init__(self, plan):
        self.plan = plan
        self.refs = {plan["base_branch"]: plan["base_sha"]}
        self.actor = {"id": 77, "login": "publisher"}
        self.repo = {"id": plan["repository"]["id"], "full_name": plan["repository"]["full_name"],
            "html_url": plan["repository"]["html_url"], "url": "https://api.github.com/repos/" + plan["repository"]["full_name"],
            "private": False, "visibility": "public", "archived": False, "disabled": False, "permissions": {"push": True}}
        self.prs = []
        self.requests = []
        self.creates = 0
        self.pushes = 0
        self.drop_push = False
        self.drop_create = False
        self.save_pr = True
        self.hide_prs = False
        self.create_status = None
        self.base_drift = False
        self.after_push = lambda: None
        self.on_push = lambda: None

    def pr(self, body):
        return {"number": 12, "html_url": self.repo["html_url"] + "/pull/12", "state": "open",
            "user": self.actor.copy(), "title": body["title"], "body": body["body"], "draft": body["draft"],
            "head": {"ref": body["head"], "sha": self.refs[body["head"]], "repo": {"id": self.repo["id"]}},
            "base": {"ref": body["base"], "sha": self.refs[body["base"]], "repo": {"id": self.repo["id"]}}}

    def handle(self, request):
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        prefix = "/repos/" + self.plan["repository"]["full_name"]
        if path == "/user":
            return httpx.Response(200, json=self.actor)
        if path == prefix:
            return httpx.Response(200, json=self.repo)
        if path.startswith(prefix + "/git/ref/heads/"):
            branch = path[len(prefix + "/git/ref/heads/"):]
            if branch not in self.refs:
                return httpx.Response(404)
            return httpx.Response(200, json={"ref": "refs/heads/" + branch,
                                          "object": {"type": "commit", "sha": self.refs[branch]}})
        if path == prefix + "/pulls":
            if request.method == "GET":
                return httpx.Response(200, json=[] if self.hide_prs else self.prs)
            self.creates += 1
            if self.create_status:
                return httpx.Response(self.create_status, json={"message": TOKEN})
            body = json.loads(request.content)
            if self.base_drift:
                self.refs[body["base"]] = "d" * 40
            pr = self.pr(body)
            if self.save_pr:
                self.prs.append(pr)
            if self.drop_create:
                raise httpx.ReadTimeout("lost response " + TOKEN, request=request)
            return httpx.Response(201, json=pr)
        pytest.fail(f"unexpected simulated endpoint: {path}")

    def push(self, repo, plan, token, before_push):
        assert token == TOKEN
        before_push()
        self.on_push()
        self.pushes += 1
        assert plan.head_branch not in self.refs
        self.refs[plan.head_branch] = plan.head_sha
        self.after_push()
        if self.drop_push:
            raise GitHubPublicationError("lost push response", uncertain=True)


@pytest.fixture
def publication(client, delivered):
    candidate, _ = delivered
    data = preview(client, candidate).json()
    remote = Remote(data["plan"])
    service = client.app.state.github_publication_service
    service.transport = httpx.MockTransport(remote.handle)
    service.token_provider = lambda: TOKEN
    service.pusher = remote
    body = {"request_id": str(uuid4()), "preview": {"base_branch": "main"},
            "plan_sha256": data["plan_sha256"], "confirmed": True}
    return candidate, remote, body


def url(candidate):
    return f"/tasks/{candidate.task.task_id}/github-delivery"


def post(client, publication, body=None):
    candidate, _, original = publication
    response = client.post(url(candidate), json=body or original)
    assert response.status_code in (200, 201), response.text
    return response.json()["publication"]


def test_check_is_read_only_and_publish_is_exact_then_idempotent(client, publication):
    candidate, remote, body = publication
    before = (source_snapshot(candidate.repo), files(candidate.repo, include_git=True), files(Path(candidate.task.artifacts_dir)))
    check = client.post(url(candidate) + "/check", json=body["preview"])
    assert check.status_code == 200, check.text
    assert check.json()["can_publish"] is True
    assert remote.pushes == remote.creates == 0
    assert client.get(url(candidate)).json()["items"] == []
    result = post(client, publication)
    assert result["status"] == "completed" and result["phase"] == "completed"
    assert result["pull_request"]["url"] == "https://github.com/example/public-repo/pull/12"
    assert result["plan_sha256"] == body["plan_sha256"]
    assert remote.pushes == remote.creates == 1
    assert remote.prs[0]["title"] == result["plan"]["title"]
    assert remote.prs[0]["body"] == result["plan"]["body"]
    requests = list(remote.requests)
    assert post(client, publication) == result
    assert remote.requests == requests
    assert post(client, publication, {**body, "request_id": str(uuid4())}) == result
    assert remote.pushes == remote.creates == 1
    assert client.get(url(candidate)).json()["items"] == [result]
    assert TOKEN not in client.get(url(candidate)).text
    after = (source_snapshot(candidate.repo), files(candidate.repo, include_git=True), files(Path(candidate.task.artifacts_dir)))
    assert before == after


@pytest.mark.parametrize("confirmed", [False, 1, "true", None])
def test_no_write_without_explicit_boolean_confirmation(client, publication, confirmed):
    candidate, remote, body = publication
    response = client.post(url(candidate), json={**body, "confirmed": confirmed})
    assert response.status_code == 422
    assert not remote.requests and remote.pushes == 0


def test_rejects_changed_hash_and_conflicting_request_payload(client, publication):
    candidate, remote, body = publication
    assert client.post(url(candidate), json={**body, "plan_sha256": "f" * 64}).status_code == 409
    assert not remote.requests
    post(client, publication)
    assert client.post(url(candidate), json={**body, "preview": {"base_branch": "other"}}).status_code == 409


@pytest.mark.parametrize("field,value", [("id", 999), ("private", True), ("archived", True),
    ("visibility", "private"), ("permissions", {"push": False}), ("html_url", "https://other.invalid/repo")])
def test_refuses_wrong_or_unwritable_repository(client, publication, field, value):
    _, remote, _ = publication
    remote.repo[field] = value
    result = post(client, publication)
    assert result["status"] == "blocked"
    assert remote.creates == remote.pushes == 0


def test_base_movement_stops_publication(client, publication):
    _, remote, _ = publication
    remote.refs["main"] = "e" * 40
    result = post(client, publication)
    assert result["status"] == "blocked" and "base" in result["error"]
    assert remote.pushes == remote.creates == 0


@pytest.mark.parametrize("same_sha", [False, True])
def test_preexisting_head_is_not_overwritten_or_adopted(client, publication, same_sha):
    _, remote, _ = publication
    remote.refs[remote.plan["head_branch"]] = remote.plan["head_sha"] if same_sha else "b" * 40
    assert post(client, publication)["status"] == "blocked"
    assert remote.pushes == remote.creates == 0


def test_missing_credential_is_recorded_and_can_be_retried(client, publication):
    _, remote, _ = publication
    service = client.app.state.github_publication_service
    service.token_provider = lambda: ""
    assert post(client, publication)["status"] == "blocked"
    assert not remote.requests
    service.token_provider = lambda: TOKEN
    assert post(client, publication)["status"] == "completed"


def test_push_timeout_is_reconciled_without_pushing_twice(client, publication):
    _, remote, _ = publication
    remote.drop_push = True
    assert post(client, publication)["status"] == "uncertain"
    assert remote.pushes == 1 and remote.creates == 0
    assert post(client, publication)["status"] == "completed"
    assert remote.pushes == remote.creates == 1


def test_pr_timeout_recovers_after_restart_without_reposting(client, publication):
    candidate, remote, body = publication
    remote.drop_create = True
    result = post(client, publication)
    assert result["status"] == "uncertain"
    assert TOKEN not in json.dumps(result)
    count = len(remote.requests)
    assert client.get(url(candidate)).json()["items"][0] == result
    assert len(remote.requests) == count
    with TestClient(create_app(client.app.state.task_store.path, github_token_provider=lambda: TOKEN,
            github_publish_transport=httpx.MockTransport(remote.handle), github_pusher=remote)) as restarted:
        assert len(remote.requests) == count
        response = restarted.post(url(candidate), json=body)
        assert response.status_code == 200
        assert response.json()["publication"]["status"] == "completed"
    assert remote.creates == remote.pushes == 1


def test_invisible_uncertain_pr_is_only_queried_not_recreated(client, publication):
    _, remote, _ = publication
    remote.drop_create = True
    remote.hide_prs = True
    assert post(client, publication)["status"] == "uncertain"
    assert post(client, publication)["status"] == "uncertain"
    assert remote.creates == 1
    remote.hide_prs = False
    assert post(client, publication)["status"] == "completed"


def test_definitive_pr_refusal_can_be_retried_without_leaking_response(client, publication):
    _, remote, _ = publication
    remote.create_status = 403
    first = post(client, publication)
    assert first["status"] == "blocked" and first["branch_pushed"] is True
    assert first["pr_attempted"] is False and TOKEN not in json.dumps(first)
    remote.create_status = None
    assert post(client, publication)["status"] == "completed"
    assert remote.pushes == 1 and remote.creates == 2


@pytest.mark.parametrize("change", ["base", "review"])
def test_change_after_push_stops_pr_and_retains_branch(client, publication, change):
    candidate, remote, _ = publication
    def mutate():
        if change == "base":
            remote.refs["main"] = "d" * 40
        else:
            decide(client, candidate, "rejected")
    remote.after_push = mutate
    result = post(client, publication)
    assert result["status"] == "blocked" and result["branch_pushed"] is True
    assert remote.creates == 0 and remote.refs[remote.plan["head_branch"]] == remote.plan["head_sha"]


def test_base_race_during_create_retains_pr_link_and_flags_revalidation(client, publication):
    _, remote, _ = publication
    remote.base_drift = True
    result = post(client, publication)
    assert result["status"] == "blocked" and result["pull_request"]["number"] == 12
    assert "base moved" in result["error"]
    assert post(client, publication)["status"] == "blocked"
    assert remote.creates == 1


def test_recovery_can_record_an_already_created_pr_after_review_changed(client, publication):
    candidate, remote, _ = publication
    remote.drop_create = True
    post(client, publication)
    decide(client, candidate, "rejected")
    assert post(client, publication)["status"] == "completed"
    assert remote.creates == remote.pushes == 1


def test_wrong_account_cannot_resume(client, publication):
    _, remote, _ = publication
    remote.drop_push = True
    post(client, publication)
    remote.actor = {"id": 88, "login": "different"}
    assert post(client, publication)["status"] == "blocked"
    assert remote.creates == 0


def test_changed_pr_is_not_adopted_or_duplicated(client, publication):
    _, remote, _ = publication
    remote.drop_create = True
    post(client, publication)
    remote.prs[0]["title"] = "edited outside"
    assert post(client, publication)["status"] == "uncertain"
    assert remote.creates == 1


def test_concurrent_publish_returns_conflict_while_get_remains_available(client, publication):
    candidate, remote, body = publication
    entered, release = Event(), Event()
    def pause():
        entered.set()
        assert release.wait(20)
    remote.on_push = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.post, url(candidate), json=body)
        try:
            assert entered.wait(20)
            assert client.get(url(candidate)).json()["items"][0]["phase"] == "push"
            assert client.post(url(candidate), json=body).status_code == 409
        finally:
            release.set()
        assert running.result().json()["publication"]["status"] == "completed"
    assert remote.creates == remote.pushes == 1


@pytest.mark.parametrize("shallow", [False, True])
def test_real_pusher_uses_isolated_copy_and_create_only_lease(client, repository, candidate_factory, tmp_path, shallow):
    parent = rev(repository)
    git(repository, "commit", "--allow-empty", "-qm", "pinned base")
    candidate = candidate_factory()
    post_delivery(client, candidate, delivery_body(client, candidate))
    plan = GitHubDeliveryPlan.model_validate(preview(client, candidate).json()["plan"])
    target = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(target))
    git(candidate.repo, "push", str(target), "main")
    if shallow:
        (candidate.repo / ".git/shallow").write_text(plan.base_sha + "\n")
        (candidate.repo / ".git/objects" / parent[:2] / parent[2:]).unlink()
        assert git(candidate.repo, "rev-parse", "--is-shallow-repository").strip() == b"true"
    before = (source_snapshot(candidate.repo), files(candidate.repo, include_git=True))
    commands = []
    checks = []
    class LocalPush(GitHubPusher):
        def _run(self, args, *, env, cwd, timeout=120):
            commands.append(args)
            assert TOKEN not in " ".join(args)
            assert env["LC_ALL"] == "C"
            if args[0] == "push":
                assert env["PATCHLOOP_PUSH_TOKEN"] == TOKEN
                assert TOKEN not in Path(env["GIT_ASKPASS"]).read_text()
                assert Path(cwd).resolve() != candidate.repo.resolve()
                assert plan.repository.html_url + ".git" in args
                args = ["-c", "protocol.file.allow=always", *[str(target) if arg == plan.repository.html_url + ".git" else arg for arg in args]]
                assert not any(arg.startswith("https://") for arg in args)
            return REAL_GIT_RUN(self, args, env=env, cwd=cwd, timeout=timeout)
    LocalPush().push(candidate.repo, plan, TOKEN, lambda: checks.append(True))
    assert rev(target, "refs/heads/" + plan.head_branch) == plan.head_sha
    assert checks == [True]
    assert f"--force-with-lease=refs/heads/{plan.head_branch}:" in commands[-1]
    assert (source_snapshot(candidate.repo), files(candidate.repo, include_git=True)) == before
    # An existing branch cannot be advanced even to the intended commit.
    git(target, "update-ref", "refs/heads/" + plan.head_branch, plan.base_sha)
    with pytest.raises(GitHubPublicationError):
        LocalPush().push(candidate.repo, plan, TOKEN, lambda: None)
    assert rev(target, "refs/heads/" + plan.head_branch) == plan.base_sha


@pytest.mark.parametrize("host,allowed", [("github.com", True), ("other.invalid", False)])
def test_push_authentication_uses_real_git_askpass_without_terminal_prompts(client, delivered, monkeypatch, host, allowed):
    candidate, _ = delivered
    plan = GitHubDeliveryPlan.model_validate(preview(client, candidate).json()["plan"])
    original_popen = subprocess.Popen
    probes = []
    helpers = []
    # Exercise Git's real credential acquisition with the exact push options
    # and generated helper, without starting a network transport or publishing.
    with tempfile.TemporaryFile() as credential_input, tempfile.TemporaryFile() as credential_output:
        credential_input.write(f"protocol=https\nhost={host}\n\n".encode())
        credential_input.seek(0)

        def credential_probe(command, **kwargs):
            assert "push" in command
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            helper = Path(kwargs["env"]["GIT_ASKPASS"])
            helpers.append(helper)
            assert TOKEN not in helper.read_text() and TOKEN not in " ".join(command)
            probes.append(True)
            # Retain all production -c flags, changing only the subcommand.
            command = [*command[:command.index("push")], "credential", "fill"]
            return original_popen(command, **{**kwargs, "stdin": credential_input, "stdout": credential_output})

        class CredentialProbe(GitHubPusher):
            def _run(self, args, **kwargs):
                if args[0] != "push":
                    return REAL_GIT_RUN(self, args, **kwargs)
                with monkeypatch.context() as patch:
                    patch.setattr(subprocess, "Popen", credential_probe)
                    return REAL_GIT_RUN(self, args, **kwargs)

        if allowed:
            CredentialProbe().push(candidate.repo, plan, TOKEN, lambda: None)
        else:
            with pytest.raises(GitHubPublicationError):
                CredentialProbe().push(candidate.repo, plan, TOKEN, lambda: None)
        credential_output.seek(0)
        credentials = credential_output.read()
        if allowed:
            assert b"username=x-access-token\n" in credentials
            assert ("password=" + TOKEN + "\n").encode() in credentials
        else:
            assert TOKEN.encode() not in credentials
        assert probes == [True]
    assert all(not helper.exists() for helper in helpers)


def test_v9_migration_preserves_all_existing_rows(client, delivered):
    store = client.app.state.task_store
    with store._connection() as connection:
        connection.execute("DROP TABLE github_publications")
        connection.execute("PRAGMA user_version=9")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}"')] for name in tables}
    TaskStore(store.path).initialize()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables} == before
        assert connection.execute("SELECT count(*) FROM github_publications").fetchone()[0] == 0


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://other.invalid/"}),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
    httpx.Response(200, content=b"{}", headers={"Content-Encoding": "unexpected"}),
])
def test_remote_read_rejects_redirects_invalid_and_oversized_responses(response):
    calls = []
    def handle(request):
        calls.append(request)
        return response
    with GitHubPublicationRemote(TOKEN, transport=httpx.MockTransport(handle)) as remote:
        with pytest.raises(GitHubPublicationError) as caught:
            remote._request("GET", "/user")
    assert len(calls) == 1 and calls[0].url.host == "api.github.com"
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("status,uncertain", [(403, False), (422, False), (429, False), (500, True), (302, True)])
def test_remote_write_error_classification_does_not_echo_secrets(status, uncertain):
    with GitHubPublicationRemote(TOKEN, transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"message": TOKEN}))) as remote:
        with pytest.raises(GitHubPublicationError) as caught:
            remote._request("POST", "/repos/example/public-repo/pulls", json={})
    assert caught.value.uncertain is uncertain
    assert TOKEN not in str(caught.value)
