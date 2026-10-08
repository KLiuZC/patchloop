"""HTTP regressions for public Issue imports; all I/O stays in mocks/tmp_path."""

import gzip
import hashlib
import json
import socket
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from patchloop.api import create_app
from patchloop.api import github as github_module
from patchloop.api.source_models import GitHubIssueSnapshot, IssueImportResult
from patchloop.api.source_store import snapshot_digest


IMPORT = "/sources/github/issues"
ISSUE_URL = "https://github.com/octocat/hello-world/issues/42"
REPO_API = "https://api.github.com/repos/octocat/hello-world"
ISSUE_API = f"{REPO_API}/issues/42"
API_VERSION = "2026-03-10"
MAX_BYTES = 2 * 1024 * 1024
BODY = '复现 🐛\n<script>alert("原样保存")</script>\n<b>不是 HTML 页面</b> &amp; `pytest -q`\n'


class GitHubResponses:
    def __init__(self):
        self.repository = {
            "id": 1296269,
            "name": "hello-world",
            "full_name": "octocat/hello-world",
            "url": REPO_API,
            "html_url": "https://github.com/octocat/hello-world",
            "owner": {"login": "octocat", "id": 1},
            "private": False,
            "visibility": "public",
            "default_branch": "main",
            "archived": False,
        }
        self.issue = {
            "id": 900042,
            "number": 42,
            "url": ISSUE_API,
            "html_url": ISSUE_URL,
            "repository_url": REPO_API,
            "title": "修复 Unicode 🐛 <em>Issue</em>",
            "body": BODY,
            "state": "open",
            "created_at": "2026-09-01T02:03:04Z",
            "updated_at": "2026-10-01T08:09:10+08:00",
            "user": {"login": "reporter", "id": 2},
            "labels": [{"name": "缺陷", "color": "d73a4a"}],
            "assignee": None,
            # Untrusted links may be preserved, but must never be fetched.
            "comments_url": "https://untrusted.invalid/comments",
        }
        self.requests = []
        self.reply = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        assert str(request.url) in (REPO_API, ISSUE_API)
        assert request.headers["x-github-api-version"] == API_VERSION
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers
        assert "proxy-authorization" not in request.headers
        assert not request.content
        if self.reply is not None:
            response = self.reply(request)
            if response is not None:
                return response
        payload = self.repository if str(request.url) == REPO_API else self.issue
        return httpx.Response(200, json=payload)


class ByteStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


@pytest.fixture(autouse=True)
def prohibit_execution_and_real_network(monkeypatch):
    from patchloop.api.store import TaskStore
    from patchloop.llm import LLM
    from patchloop.repair.runner import RepairRunner
    from patchloop.worker import TaskWorker

    def forbidden(*args, **kwargs):
        pytest.fail("Issue import must not use real network, create tasks, or execute work")

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-must-not-be-sent")
    monkeypatch.setenv("GH_TOKEN", "another-test-token-must-not-be-sent")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(TaskStore, "create", forbidden)
    monkeypatch.setattr(TaskWorker, "__init__", forbidden)
    monkeypatch.setattr(RepairRunner, "run", forbidden)
    monkeypatch.setattr(LLM, "__init__", forbidden)


@pytest.fixture
def github():
    return GitHubResponses()


@pytest.fixture
def open_api(tmp_path, github):
    @contextmanager
    def open_client():
        app = create_app(
            tmp_path / "data" / "tasks.sqlite3",
            github_transport=httpx.MockTransport(github),
        )
        with TestClient(app) as client:
            yield client

    return open_client


@pytest.fixture
def client(open_api):
    with open_api() as client:
        yield client


def submission(*, request_id=None, url=ISSUE_URL):
    return {"request_id": str(request_id or uuid4()), "url": url}


def source_rows(client):
    path = client.app.state.task_store.path
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        return connection.execute("SELECT * FROM github_issue_sources ORDER BY source_id").fetchall()


def assert_error(response, status):
    assert response.status_code == status, response.text
    assert response.json()["detail"]
    assert "source" not in response.json()
    assert "location" not in response.headers


def assert_fetches(github, expected):
    assert [str(request.url) for request in github.requests] == expected


def test_import_preserves_text_metadata_and_verifiable_raw_snapshot(client, github):
    payload = submission()
    before = datetime.now(timezone.utc)
    response = client.post(IMPORT, json=payload)
    after = datetime.now(timezone.utc)
    assert response.status_code == 201, response.text
    result = IssueImportResult.model_validate(response.json())
    source = result.source
    assert result.replayed is False
    assert source.request_id == UUID(payload["request_id"])
    assert source.kind == "github_issue"
    assert source.requested_url == source.url == ISSUE_URL
    assert source.api_url == ISSUE_API
    assert source.api_version == API_VERSION
    assert source.issue_id == github.issue["id"]
    assert source.issue_number == github.issue["number"]
    assert source.title == github.issue["title"]
    assert source.body == BODY
    assert source.state == github.issue["state"]
    assert source.issue_created_at == datetime.fromisoformat(github.issue["created_at"])
    assert source.issue_updated_at == datetime.fromisoformat(github.issue["updated_at"])
    assert before <= source.fetched_at <= after
    assert source.repository.model_dump() == {
        key: github.repository[key]
        for key in ("id", "full_name", "html_url", "default_branch", "archived", "visibility")
    }
    location = f"{IMPORT}/{source.source_id}"
    assert response.headers["location"] == location
    stored = client.get(location)
    assert stored.status_code == 200
    assert stored.json() == response.json()["source"]
    raw = client.get(f"{location}/snapshot")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("application/json")
    expected = {"api_version": API_VERSION, "repository": github.repository, "issue": github.issue}
    assert raw.json() == expected
    canonical = json.dumps(expected, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert source.snapshot_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert snapshot_digest(GitHubIssueSnapshot.model_validate(raw.json())) == source.snapshot_sha256
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert len(source_rows(client)) == 1
    tasks = client.get("/tasks")
    assert tasks.status_code == 200 and tasks.json()["items"] == []


@pytest.mark.parametrize("body", [None, ""])
def test_empty_body_is_normalized_only_in_metadata(client, github, body):
    github.issue["body"] = body
    response = client.post(IMPORT, json=submission())
    assert response.status_code == 201, response.text
    assert response.json()["source"]["body"] == ""
    raw = client.get(f"{response.headers['location']}/snapshot")
    assert raw.status_code == 200
    assert raw.json()["issue"]["body"] == body
    assert len(source_rows(client)) == 1


def test_public_repository_may_omit_visibility_and_have_no_default_branch(client, github):
    del github.repository["visibility"]
    github.repository["default_branch"] = None
    github.repository["archived"] = True
    github.issue["state"] = "closed"
    response = client.post(IMPORT, json=submission())
    assert response.status_code == 201, response.text
    source = response.json()["source"]
    assert source["repository"]["visibility"] == "public"
    assert source["repository"]["default_branch"] is None
    assert source["repository"]["archived"] is True
    assert source["state"] == "closed"
    raw = client.get(f"{response.headers['location']}/snapshot").json()
    assert "visibility" not in raw["repository"]


def test_normalized_replay_survives_restart_without_network(open_api, github):
    payload = submission(url="https://GITHUB.COM/OctoCat/Hello-World/issues/00042/?tab=comments#issuecomment-9")
    with open_api() as first:
        response = first.post(IMPORT, json=payload)
        assert response.status_code == 201, response.text
        original = response.json()["source"]
        location = response.headers["location"]
        snapshot = first.get(f"{location}/snapshot").json()
        rows = source_rows(first)
    assert original["requested_url"] == original["url"] == ISSUE_URL
    assert_fetches(github, [REPO_API, ISSUE_API])

    def offline(request):
        pytest.fail("replay and GET must use the persisted snapshot, even when GitHub is unavailable")

    github.reply = offline
    with open_api() as restarted:
        for url in (ISSUE_URL, "https://github.com/OCTOCAT/HELLO-WORLD/issues/42/#new-comment"):
            replay = restarted.post(IMPORT, json={**payload, "url": url})
            assert replay.status_code == 200, replay.text
            assert replay.json() == {"source": original, "replayed": True}
            assert replay.headers["location"] == location
        assert restarted.get(location).json() == original
        assert restarted.get(f"{location}/snapshot").json() == snapshot
        assert source_rows(restarted) == rows
    assert_fetches(github, [REPO_API, ISSUE_API])


def test_same_request_id_with_different_issue_conflicts_without_network(client, github):
    payload = submission()
    original = client.post(IMPORT, json=payload)
    assert original.status_code == 201
    rows = source_rows(client)
    conflict = client.post(IMPORT, json={**payload, "url": ISSUE_URL.replace("/42", "/43")})
    assert_error(conflict, 409)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == rows
    replay = client.post(IMPORT, json=payload)
    assert replay.status_code == 200
    assert replay.json() == {"source": original.json()["source"], "replayed": True}


def test_new_request_id_fetches_new_immutable_snapshot(client, github):
    original = client.post(IMPORT, json=submission())
    assert original.status_code == 201
    first = original.json()["source"]
    snapshot = client.get(f"{original.headers['location']}/snapshot").json()
    old_rows = source_rows(client)
    github.issue.update(title="新标题 🐞", body="第二次快照", state="closed", updated_at="2026-10-08T00:00:00Z")
    github.repository["default_branch"] = "renamed-main"
    new = client.post(IMPORT, json=submission())
    assert new.status_code == 201 and new.json()["replayed"] is False
    second = new.json()["source"]
    assert first["request_id"] != second["request_id"]
    assert first["source_id"] != second["source_id"]
    assert first["issue_id"] == second["issue_id"]
    assert first["url"] == second["url"]
    assert first["snapshot_sha256"] != second["snapshot_sha256"]
    assert second["body"] == github.issue["body"]
    assert second["title"] == github.issue["title"]
    assert second["state"] == "closed"
    assert second["repository"]["default_branch"] == "renamed-main"
    assert client.get(original.headers["location"]).json() == first
    assert client.get(f"{original.headers['location']}/snapshot").json() == snapshot
    new_snapshot = client.get(f"{new.headers['location']}/snapshot").json()
    assert new_snapshot["issue"] == github.issue
    assert new_snapshot["repository"] == github.repository
    assert second["snapshot_sha256"] == snapshot_digest(GitHubIssueSnapshot.model_validate(new_snapshot))
    assert all(row in source_rows(client) for row in old_rows)
    assert len(source_rows(client)) == 2
    assert_fetches(github, [REPO_API, ISSUE_API] * 2)


def test_snapshot_hash_ignores_http_json_formatting_and_key_order(client, github):
    first = client.post(IMPORT, json=submission())
    assert first.status_code == 201

    def pretty_json(request):
        data = github.repository if str(request.url) == REPO_API else github.issue
        data = dict(reversed(list(data.items())))
        return httpx.Response(200, content=json.dumps(data, ensure_ascii=True, indent=4).encode("utf-8"))

    github.reply = pretty_json
    second = client.post(IMPORT, json=submission())
    assert second.status_code == 201 and second.json()["replayed"] is False
    assert first.json()["source"]["source_id"] != second.json()["source"]["source_id"]
    assert first.json()["source"]["snapshot_sha256"] == second.json()["source"]["snapshot_sha256"]
    assert client.get(f"{first.headers['location']}/snapshot").json() == client.get(
        f"{second.headers['location']}/snapshot"
    ).json()
    assert len(source_rows(client)) == 2


def test_source_endpoints_are_read_only_and_do_not_refetch(client, github):
    imported = client.post(IMPORT, json=submission())
    assert imported.status_code == 201
    location = imported.headers["location"]
    before = source_rows(client)
    source = imported.json()["source"]
    expected_snapshot = {"api_version": API_VERSION, "repository": deepcopy(github.repository), "issue": deepcopy(github.issue)}
    github.issue["body"] = "This live change must not appear on GET"
    for _ in range(2):
        assert client.get(location).json() == source
        assert client.get(f"{location}/snapshot").json() == expected_snapshot
    for method in ("put", "patch", "delete"):
        for endpoint in (location, f"{location}/snapshot"):
            assert client.request(method, endpoint, json={"body": "overwrite"}).status_code == 405
    assert source_rows(client) == before
    assert_fetches(github, [REPO_API, ISSUE_API])


@pytest.mark.parametrize("source_id,status", [(str(uuid4()), 404), ("not-a-uuid", 422)])
def test_unknown_and_invalid_source_ids_do_not_contact_github(client, github, source_id, status):
    for suffix in ("", "/snapshot"):
        assert_error(client.get(f"{IMPORT}/{source_id}{suffix}"), status)
    assert_fetches(github, [])
    assert source_rows(client) == []


@pytest.mark.parametrize("url", [
    pytest.param("http://github.com/octocat/hello-world/issues/42", id="http"),
    pytest.param("https://127.0.0.1/octocat/hello-world/issues/42", id="other-host"),
    pytest.param("https://github.com.attacker.invalid/octocat/hello-world/issues/42", id="host-suffix"),
    pytest.param("https://user:pass@github.com/octocat/hello-world/issues/42", id="userinfo"),
    pytest.param("https://github.com:443/octocat/hello-world/issues/42", id="explicit-port"),
    pytest.param("https://github.com/octocat/hello-world/pull/42", id="pull-path"),
    pytest.param("https://github.com/octocat/hello-world", id="repository-only"),
    pytest.param("https://github.com/octocat/hello-world/issues/0", id="zero-number"),
    pytest.param("https://github.com/octocat/hello-world/issues/42/comments", id="extra-path"),
    pytest.param("https://github.com/octocat/%2e%2e/issues/42", id="encoded-traversal"),
    pytest.param("https://github.com/octocat/hello-world/issues/4\n2", id="control-character"),
])
def test_invalid_url_is_rejected_before_any_network_or_storage(client, github, url):
    assert_error(client.post(IMPORT, json=submission(url=url)), 422)
    assert_fetches(github, [])
    assert source_rows(client) == []


@pytest.mark.parametrize("invalid", [
    {"request_id": "not-a-uuid", "url": ISSUE_URL},
    {"url": ISSUE_URL},
    {"request_id": str(uuid4()), "url": None},
    {"request_id": str(uuid4()), "url": ISSUE_URL, "token": "do-not-accept-tokens"},
])
def test_invalid_import_dto_is_rejected_without_network(client, github, invalid):
    assert_error(client.post(IMPORT, json=invalid), 422)
    assert_fetches(github, [])
    assert source_rows(client) == []


@pytest.mark.parametrize("changes,status", [
    pytest.param({"private": True}, 422, id="private-repo"),
    pytest.param({"private": "false"}, 502, id="private-string"),
    pytest.param({"private": 0}, 502, id="private-integer"),
    pytest.param({"private": None}, 502, id="private-null"),
    pytest.param({}, 502, id="private-missing"),
    pytest.param({"visibility": "internal"}, 422, id="internal-visibility"),
    pytest.param({"visibility": None}, 422, id="null-visibility"),
])
def test_repository_must_be_explicitly_public_before_fetching_issue(client, github, changes, status):
    if changes:
        github.repository.update(changes)
    else:
        del github.repository["private"]
    assert_error(client.post(IMPORT, json=submission()), status)
    assert_fetches(github, [REPO_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("pull_request", [None, {"url": "https://api.github.com/repos/octocat/hello-world/pulls/42"}])
def test_pull_request_key_is_rejected_even_when_null(client, github, pull_request):
    github.issue["pull_request"] = pull_request
    assert_error(client.post(IMPORT, json=submission()), 422)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("part,field,value", [
    ("repository", "full_name", "attacker/hello-world"),
    ("repository", "url", "https://untrusted.invalid/repos/octocat/hello-world"),
    ("repository", "html_url", "https://github.com/octocat/other-repository"),
    ("issue", "number", 43),
    ("issue", "url", ISSUE_API.replace("/42", "/43")),
    ("issue", "repository_url", f"{REPO_API}-other"),
    ("issue", "html_url", ISSUE_URL.replace("/42", "/43")),
])
def test_mismatched_github_identity_cannot_be_persisted(client, github, part, field, value):
    getattr(github, part)[field] = value
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert_fetches(github, [REPO_API] if part == "repository" else [REPO_API, ISSUE_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("field,value", [("id", True), ("body", {"html": "bad type"}), ("updated_at", "2026-10-08T00:00:00")])
def test_malformed_required_issue_fields_are_upstream_errors(client, github, field, value):
    github.issue[field] = value
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert source_rows(client) == []


@pytest.mark.parametrize("endpoint,failure,status", [
    pytest.param(REPO_API, httpx.ReadTimeout, 504, id="repository-timeout"),
    pytest.param(ISSUE_API, httpx.ReadTimeout, 504, id="issue-timeout"),
    pytest.param(ISSUE_API, httpx.ConnectError, 502, id="connection-failure"),
    pytest.param(REPO_API, 404, 404, id="repository-not-found"),
    pytest.param(ISSUE_API, 410, 404, id="issue-gone"),
    pytest.param(ISSUE_API, 500, 502, id="upstream-server-error"),
    pytest.param(ISSUE_API, 403, 502, id="forbidden-without-rate-limit"),
])
def test_upstream_failure_saves_nothing_and_same_request_can_retry(client, github, endpoint, failure, status):
    def failing(request):
        if str(request.url) != endpoint:
            return None
        if isinstance(failure, int):
            return httpx.Response(failure, json={"message": "request failed"})
        raise failure("simulated GitHub failure", request=request)

    github.reply = failing
    payload = submission()
    assert_error(client.post(IMPORT, json=payload), status)
    assert source_rows(client) == []
    failed_requests = [REPO_API] if endpoint == REPO_API else [REPO_API, ISSUE_API]
    assert_fetches(github, failed_requests)
    github.reply = None
    retry = client.post(IMPORT, json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["replayed"] is False
    assert retry.json()["source"]["request_id"] == payload["request_id"]
    replay = client.post(IMPORT, json=payload)
    assert replay.status_code == 200
    assert replay.json() == {"source": retry.json()["source"], "replayed": True}
    assert_fetches(github, failed_requests + [REPO_API, ISSUE_API])
    assert len(source_rows(client)) == 1


@pytest.mark.parametrize("endpoint,code,headers,message,delay", [
    pytest.param(REPO_API, 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000030"},
                 "API rate limit exceeded", "30", id="primary-limit"),
    pytest.param(ISSUE_API, 403, {"Retry-After": "17"}, "secondary rate limit", "17", id="secondary-limit"),
    pytest.param(ISSUE_API, 403, {}, "You have exceeded a secondary RATE LIMIT.", None, id="message-only-limit"),
    pytest.param(ISSUE_API, 429, {"Retry-After": "5"}, "too many requests", "5", id="http-429"),
    pytest.param(ISSUE_API, 429, {"Retry-After": "-9"}, "too many requests", None, id="invalid-retry-after"),
    pytest.param(ISSUE_API, 429, {"Retry-After": "9999999999"}, "too many requests", "86400", id="bounded-retry-after"),
])
def test_rate_limits_have_safe_retry_after_and_no_partial_source(client, github, monkeypatch,
                                                               endpoint, code, headers, message, delay):
    monkeypatch.setattr(github_module, "time", SimpleNamespace(monotonic=time.monotonic, time=lambda: 1700000000))
    github.reply = lambda request: (
        httpx.Response(code, headers=headers, json={"message": message}) if str(request.url) == endpoint else None
    )
    response = client.post(IMPORT, json=submission())
    assert_error(response, 429)
    assert response.headers.get("retry-after") == delay
    if delay is not None:
        assert delay.isascii() and delay.isdigit() and 0 < int(delay) <= 86400
    assert_fetches(github, [REPO_API] if endpoint == REPO_API else [REPO_API, ISSUE_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("endpoint,code", [(REPO_API, 301), (ISSUE_API, 307)])
def test_redirect_is_not_followed_and_requires_canonical_issue_url(client, github, endpoint, code):
    github.reply = lambda request: (
        httpx.Response(code, headers={"Location": "https://untrusted.invalid/redirected"})
        if str(request.url) == endpoint else None
    )
    response = client.post(IMPORT, json=submission())
    assert_error(response, 422)
    assert "canonical" in response.json()["detail"].lower()
    assert_fetches(github, [REPO_API] if endpoint == REPO_API else [REPO_API, ISSUE_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("malformation", [
    "invalid-json", "non-object", "invalid-utf8", "NaN", "Infinity", "-Infinity", "1e999", "lone-surrogate",
])
def test_invalid_json_and_uncanonicalizable_snapshots_are_bad_gateway(client, github, malformation):
    if malformation == "invalid-json":
        content = b'{"number":42,'
    elif malformation == "non-object":
        content = b"[]"
    elif malformation == "invalid-utf8":
        content = b'{"body":"\xff"}'
    elif malformation in ("NaN", "Infinity", "-Infinity", "1e999"):
        # Preserve the numeric token on the wire: dumping float("1e999")
        # would produce Infinity and miss the parse_float overflow path.
        content = (json.dumps(github.issue, ensure_ascii=True)[:-1]
                   + ', "extension":' + malformation + '}').encode("ascii")
    else:
        # Unknown raw fields are retained, so canonicalization must validate them too.
        data = {**github.issue, "extension": "\ud800"}
        content = json.dumps(data, ensure_ascii=True).encode("ascii")
    github.reply = lambda request: httpx.Response(200, content=content) if str(request.url) == ISSUE_API else None
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []


def test_declared_response_over_two_mib_is_rejected_before_reading(client, github):
    def must_not_read():
        pytest.fail("an oversized Content-Length must be rejected before streaming the body")
        yield b""

    github.reply = lambda request: httpx.Response(
        200, headers={"Content-Length": str(MAX_BYTES + 1)}, stream=ByteStream(must_not_read()),
    )
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert_fetches(github, [REPO_API])
    assert source_rows(client) == []


def test_streamed_response_over_two_mib_cannot_hide_behind_content_length(client, github):
    content = json.dumps({**github.issue, "extension": "x" * MAX_BYTES}).encode("utf-8")
    chunks = (content[offset:offset + 65536] for offset in range(0, len(content), 65536))
    github.reply = lambda request: (
        httpx.Response(200, headers={"Content-Length": "1"}, stream=ByteStream(chunks))
        if str(request.url) == ISSUE_API else None
    )
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []


def test_gzip_response_is_rejected_without_iterating_or_decompressing_body(client, github):
    compressed = gzip.compress(json.dumps(github.issue).encode("utf-8"))

    def must_not_read():
        pytest.fail("a gzip response must be rejected before its stream is read or decompressed")
        yield compressed

    github.reply = lambda request: (
        httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
            stream=ByteStream(must_not_read()),
        ) if str(request.url) == ISSUE_API else None
    )
    assert_error(client.post(IMPORT, json=submission()), 502)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []


@pytest.mark.parametrize("stage", ["between-requests", "slow-issue-stream"])
def test_total_deadline_is_enforced_without_sleeping(client, github, monkeypatch, stage):
    clock = [1000.0]
    monkeypatch.setattr(github_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    elapsed = github_module.IMPORT_TIMEOUT_SECONDS + 1

    def slow_chunks():
        if stage == "between-requests":
            yield json.dumps(github.repository).encode("utf-8")
            # Finish the first response after the shared deadline. A fresh
            # per-request timeout must not allow the second request to start.
            clock[0] += elapsed
        else:
            content = json.dumps(github.issue).encode("utf-8")
            yield content[:1]
            clock[0] += elapsed
            yield content[1:2]
            pytest.fail("small slow chunks must be timed out before buffering the rest")

    endpoint = REPO_API if stage == "between-requests" else ISSUE_API
    github.reply = lambda request: (
        httpx.Response(200, stream=ByteStream(slow_chunks())) if str(request.url) == endpoint else None
    )
    assert_error(client.post(IMPORT, json=submission()), 504)
    assert_fetches(github, [REPO_API] if endpoint == REPO_API else [REPO_API, ISSUE_API])
    assert source_rows(client) == []


def test_issue_eof_after_deadline_returns_timeout_without_saving(client, github, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(github_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    def late_eof():
        yield json.dumps(github.issue).encode("utf-8")
        # The complete JSON arrives in time, but EOF arrives over budget.
        # No additional chunk exists to trigger a per-chunk deadline check.
        clock[0] += github_module.IMPORT_TIMEOUT_SECONDS + 1

    github.reply = lambda request: (
        httpx.Response(200, stream=ByteStream(late_eof())) if str(request.url) == ISSUE_API else None
    )
    assert_error(client.post(IMPORT, json=submission()), 504)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []


def test_second_request_io_timeouts_do_not_exceed_remaining_budget(client, github, monkeypatch):
    clock = [1000.0]
    deadline = clock[0] + github_module.IMPORT_TIMEOUT_SECONDS
    remaining = 0.25
    observed = []
    monkeypatch.setattr(github_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    def consume_repository_budget(request):
        observed.append((deadline - clock[0], dict(request.extensions["timeout"])))
        if str(request.url) == REPO_API:
            clock[0] = deadline - remaining
        return None

    # Inspect timeout configuration at each I/O start; MockTransport does not
    # simulate interrupting a blocked socket or require real sleeping.
    github.reply = consume_repository_budget
    response = client.post(IMPORT, json=submission())
    assert response.status_code == 201, response.text
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert observed[1][0] == remaining
    for budget, timeouts in observed:
        assert set(timeouts) == {"connect", "read", "write", "pool"}
        assert all(timeout is not None and 0 < timeout <= budget for timeout in timeouts.values())
    assert len(source_rows(client)) == 1


def test_failed_storage_lookup_returns_unavailable_without_fetching(client, github, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    payload = submission()
    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.issue_source_store, "get_for_request", unavailable)
        assert_error(client.post(IMPORT, json=payload), 503)
    assert_fetches(github, [])
    assert source_rows(client) == []
    retry = client.post(IMPORT, json=payload)
    assert retry.status_code == 201 and retry.json()["replayed"] is False
    assert len(source_rows(client)) == 1


def test_database_insert_failure_rolls_back_and_does_not_consume_request_id(client, github):
    path = client.app.state.task_store.path
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_source_insert AFTER INSERT ON github_issue_sources
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
    payload = submission()
    assert_error(client.post(IMPORT, json=payload), 503)
    assert_fetches(github, [REPO_API, ISSUE_API])
    assert source_rows(client) == []
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_source_insert")
    retry = client.post(IMPORT, json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["replayed"] is False
    source = retry.json()["source"]
    assert client.get(retry.headers["location"]).json() == source
    assert client.get(f"{retry.headers['location']}/snapshot").status_code == 200
    assert len(source_rows(client)) == 1


def test_unavailable_storage_reads_are_not_reported_as_missing_or_success(client, github, monkeypatch):
    imported = client.post(IMPORT, json=submission())
    assert imported.status_code == 201
    before = source_rows(client)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.task_store, "_connection", unavailable)
        assert_error(client.get(imported.headers["location"]), 503)
        assert_error(client.get(f"{imported.headers['location']}/snapshot"), 503)
    assert source_rows(client) == before
    assert_fetches(github, [REPO_API, ISSUE_API])


def test_corrupt_saved_snapshot_fails_reads_and_replays_without_refetching(client, github):
    payload = submission()
    imported = client.post(IMPORT, json=payload)
    assert imported.status_code == 201
    location = imported.headers["location"]
    snapshot = client.get(f"{location}/snapshot").json()
    snapshot["issue"]["body"] = "tampered after import"
    with sqlite3.connect(client.app.state.task_store.path) as connection:
        connection.execute("UPDATE github_issue_sources SET snapshot_json = ?", (json.dumps(snapshot),))
    corrupt_rows = source_rows(client)
    assert_error(client.get(location), 500)
    assert_error(client.get(f"{location}/snapshot"), 500)
    assert_error(client.post(IMPORT, json=payload), 500)
    assert source_rows(client) == corrupt_rows
    assert_fetches(github, [REPO_API, ISSUE_API])


def test_concurrent_same_request_returns_one_created_and_one_replay(client, github):
    barrier = Barrier(2)

    def concurrent_issue(request):
        if str(request.url) == ISSUE_API:
            barrier.wait(timeout=5)
        return None

    github.reply = concurrent_issue
    payload = submission()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(IMPORT, json=payload), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    created = next(response for response in responses if response.status_code == 201)
    replayed = next(response for response in responses if response.status_code == 200)
    assert created.json()["replayed"] is False
    assert replayed.json() == {"source": created.json()["source"], "replayed": True}
    assert replayed.headers["location"] == created.headers["location"]
    assert len(github.requests) == 4
    assert len(source_rows(client)) == 1
