"""Repository resolution uses mocked public GETs and never changes source input."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

from patchloop.api import github as github_module
from patchloop.api import repository_github as resolver_module
from patchloop.api.github import API_VERSION, MAX_RESPONSE_BYTES, GitHubIssueClient, GitHubReadError
from patchloop.api.repository_github import RepositoryResolver
from patchloop.api.repository_models import ResolvedRepository
from patchloop.api.source_models import GitHubRepository, IssueSource


REPO_API = "https://api.github.com/repos/octocat/hello-world"
REPO_HTML = "https://github.com/octocat/hello-world"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "a" * 40


def commit_endpoint(ref):
    return f"{REPO_API}/commits/{quote(ref, safe='')}?per_page=1"


class ByteStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


class GitHubResponses:
    def __init__(self):
        self.repository = {
            "id": 1296269, "full_name": "octocat/hello-world",
            "url": REPO_API, "html_url": REPO_HTML,
            "private": False, "visibility": "public",
            "default_branch": "main", "archived": False,
        }
        self.commit = {
            "sha": SHA, "url": f"{REPO_API}/commits/{SHA}",
            "html_url": f"{REPO_HTML}/commit/{SHA}",
            "comments_url": "https://untrusted.invalid/never-fetch",
        }
        self.requests = []
        self.reply = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.url.scheme == "https"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == API_VERSION
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["user-agent"] == "patchloop"
        assert "authorization" not in request.headers
        assert "proxy-authorization" not in request.headers
        assert not request.content
        if self.reply is not None:
            response = self.reply(request)
            if response is not None:
                return response
        payload = self.repository if str(request.url) == REPO_API else self.commit
        return httpx.Response(200, json=deepcopy(payload))

    @property
    def urls(self):
        return [str(request.url) for request in self.requests]


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("repository resolution must not access real sockets, databases or subprocesses")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.chdir(tmp_path)
    yield
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def source():
    timestamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    value = IssueSource(
        source_id=uuid4(), request_id=uuid4(), requested_url=f"{REPO_HTML}/issues/42",
        url=f"{REPO_HTML}/issues/42", api_url=f"{REPO_API}/issues/42",
        repository=GitHubRepository(id=1296269, full_name="octocat/hello-world",
                                    html_url=REPO_HTML, default_branch="old-main", archived=False),
        issue_id=900042, issue_number=42, title="Example issue", body="Original issue body",
        state="open", issue_created_at=timestamp, issue_updated_at=timestamp,
        fetched_at=timestamp, api_version=API_VERSION, snapshot_sha256="a" * 64,
    )
    before = value.model_dump(mode="json")
    yield value
    assert value.model_dump(mode="json") == before


@pytest.fixture
def github():
    return GitHubResponses()


@pytest.fixture
def resolver(github):
    return RepositoryResolver(GitHubIssueClient(transport=httpx.MockTransport(github)))


def assert_error(resolver, source, status, match=None, ref=None):
    with pytest.raises(GitHubReadError, match=match) as caught:
        resolver.resolve(source, ref)
    assert caught.value.status_code == status
    return caught.value


def test_default_uses_current_repository_and_preserves_source(resolver, github, source, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "never-send-this")
    monkeypatch.setenv("GH_TOKEN", "never-send-this-either")
    github.repository.update(default_branch="trunk", archived=True)
    before = datetime.now(timezone.utc)
    result = resolver.resolve(source, None)
    assert isinstance(result, ResolvedRepository)
    assert result.ref == "trunk"
    assert result.base_sha == SHA
    assert result.repository == GitHubRepository(
        id=1296269, full_name="octocat/hello-world", html_url=REPO_HTML,
        default_branch="trunk", archived=True,
    )
    assert result.repository is not source.repository
    assert before <= result.resolved_at <= datetime.now(timezone.utc)
    assert result.resolved_at.utcoffset() == timedelta(0)
    assert github.urls == [REPO_API, commit_endpoint("trunk")]


@pytest.mark.parametrize("ref", ["develop", "v1.2.3", "feature/fix", "feature/修复#100%", SHA, SHA.upper()])
def test_explicit_refs_are_encoded_and_resolved(resolver, github, source, ref):
    result = resolver.resolve(source, ref)
    assert result.ref == ref
    assert result.base_sha == SHA
    assert github.urls == [REPO_API, commit_endpoint(ref)]
    if "/" in ref:
        assert b"%2F" in github.requests[-1].url.raw_path


def test_only_requested_issue_url_determines_endpoints(resolver, github, source):
    changed = source.model_copy(update={
        "requested_url": "https://GitHub.com/Octocat/Hello-World/issues/042?x=1#comment",
        "url": "https://untrusted.invalid/issue", "api_url": "https://untrusted.invalid/api",
    })
    github.repository["full_name"] = "Octocat/Hello-World"
    assert resolver.resolve(changed).repository.full_name == "Octocat/Hello-World"
    assert github.urls == [REPO_API, commit_endpoint("main")]


@pytest.mark.parametrize("ref", [
    "", "HEAD~1", "main^", "main..other", "main@{1}", "-main", " main", "a b", "a\nb",
    "/main", "main/", "a//b", "refs/.hidden", "a.lock", "a:b", "a?b", "a*b", "a[b",
    "a\\b", "a.", "@", "x" * 201, 42,
])
def test_invalid_refs_fail_before_network(resolver, github, source, ref):
    assert_error(resolver, source, 422, "ref must", ref=ref)
    assert github.urls == []


@pytest.mark.parametrize("field,value,match", [
    ("id", 1296270, "replaced"),
    ("full_name", "octocat/renamed", "renamed"),
    ("full_name", "another/hello-world", "identity"),
    ("url", "https://untrusted.invalid/repo", "identity"),
    ("url", f"{REPO_API}?redirect=1", "identity"),
    ("html_url", "https://github.com/other/repo", "identity"),
])
def test_changed_repository_identity_stops_before_commit(resolver, github, source, field, value, match):
    github.repository[field] = value
    assert_error(resolver, source, 502, match)
    assert github.urls == [REPO_API]


@pytest.mark.parametrize("field,value,status", [
    ("private", True, 422), ("private", None, 502), ("private", "false", 502), ("private", 0, 502),
    ("visibility", "private", 422), ("visibility", "internal", 422), ("visibility", None, 422),
    ("id", True, 502), ("id", "1296269", 502), ("id", 0, 502), ("id", 2**63, 502),
    ("archived", "false", 502), ("archived", 0, 502),
    ("default_branch", 42, 502), ("default_branch", "", 502),
    ("default_branch", "x" * 1025, 502), ("default_branch", "HEAD~1", 502),
])
def test_visibility_and_required_metadata_are_validated(resolver, github, source, field, value, status):
    github.repository[field] = value
    assert_error(resolver, source, status)
    assert github.urls == [REPO_API]


@pytest.mark.parametrize("field", ["id", "full_name", "url", "html_url", "private", "archived"])
def test_missing_required_repository_fields(resolver, github, source, field):
    del github.repository[field]
    assert_error(resolver, source, 502)
    assert github.urls == [REPO_API]


@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("ref", [None, "v1.0"])
def test_missing_default_branch_requires_explicit_ref(resolver, github, source, missing, ref):
    if missing:
        del github.repository["default_branch"]
    else:
        github.repository["default_branch"] = None
    if ref is None:
        assert_error(resolver, source, 422, "no default branch")
        assert github.urls == [REPO_API]
    else:
        result = resolver.resolve(source, ref)
        assert result.ref == ref
        assert result.repository.default_branch is None
        assert github.urls == [REPO_API, commit_endpoint(ref)]


@pytest.mark.parametrize("field,value", [
    ("sha", "a" * 39), ("sha", "a" * 41), ("sha", "z" * 40), ("sha", SHA.upper()),
    ("sha", SHA + "\n"), ("sha", None), ("sha", 123),
    ("url", f"{REPO_API}/commits/{OTHER_SHA}"),
    ("url", f"https://api.github.com/repos/attacker/repo/commits/{SHA}"),
    ("url", f"https://untrusted.invalid/commits/{SHA}"),
    ("url", f"{REPO_API}/commits/main"),
    ("html_url", f"{REPO_HTML}/commit/{OTHER_SHA}"),
    ("html_url", f"https://github.com/attacker/repo/commit/{SHA}"),
    ("html_url", f"{REPO_HTML}/commit/{SHA}?extra=1"),
    ("html_url", None),
])
def test_malformed_or_mismatched_commit_identity(resolver, github, source, field, value):
    github.commit[field] = value
    assert_error(resolver, source, 502, "commit")
    assert github.urls == [REPO_API, commit_endpoint("main")]


@pytest.mark.parametrize("field", ["sha", "url", "html_url"])
def test_missing_commit_identity_fields(resolver, github, source, field):
    del github.commit[field]
    assert_error(resolver, source, 502, "commit")


@pytest.mark.parametrize("ref", [OTHER_SHA, OTHER_SHA.upper()])
def test_full_sha_requires_equality_ignoring_case(resolver, github, source, ref):
    assert_error(resolver, source, 502, "explicitly requested", ref=ref)
    assert github.urls == [REPO_API, commit_endpoint(ref)]


@pytest.mark.parametrize("endpoint", [REPO_API, commit_endpoint("main")])
@pytest.mark.parametrize("failure,status", [
    (httpx.ReadTimeout, 504), (httpx.ConnectTimeout, 504), (httpx.ConnectError, 502),
    (httpx.RemoteProtocolError, 502), (404, 404), (410, 404), (500, 502), (403, 502),
])
def test_network_and_http_errors_preserve_source_client_semantics(resolver, github, source,
                                                               endpoint, failure, status):
    def fail(request):
        if str(request.url) == endpoint:
            if isinstance(failure, int):
                return httpx.Response(failure, json={"message": "failed"})
            raise failure("simulated failure", request=request)

    github.reply = fail
    assert_error(resolver, source, status)
    assert github.urls == ([REPO_API] if endpoint == REPO_API else [REPO_API, endpoint])


@pytest.mark.parametrize("endpoint", [REPO_API, commit_endpoint("main")])
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirects_are_never_followed(resolver, github, source, endpoint, code):
    github.reply = lambda request: (
        httpx.Response(code, headers={"Location": "https://untrusted.invalid/redirect"})
        if str(request.url) == endpoint else None
    )
    assert_error(resolver, source, 422, "redirected")
    assert github.urls == ([REPO_API] if endpoint == REPO_API else [REPO_API, endpoint])


@pytest.mark.parametrize("code,headers,message,delay", [
    (429, {"Retry-After": "17"}, "rate limit", 17),
    (403, {"X-RateLimit-Remaining": "0", "Retry-After": "7"}, "rate limit", 7),
    (403, {"Retry-After": "9"}, "secondary rate limit", 9),
    (403, {}, "secondary RATE LIMIT reached", None),
])
def test_rate_limit_errors_preserve_retry_after(resolver, github, source, code, headers, message, delay):
    github.reply = lambda request: httpx.Response(code, headers=headers, json={"message": message})
    error = assert_error(resolver, source, 429, "rate limit")
    assert error.retry_after == delay
    assert github.urls == [REPO_API]


@pytest.mark.parametrize("content", [
    b'{"sha":', b"[]", b"null", b'{"extra":"\xff"}',
    b'{"extra":NaN}', b'{"extra":Infinity}', b'{"extra":-Infinity}', b'{"extra":1e999}',
])
def test_invalid_json_is_rejected_by_shared_reader(resolver, github, source, content):
    github.reply = lambda request: (
        httpx.Response(200, content=content) if str(request.url) != REPO_API else None
    )
    assert_error(resolver, source, 502, "JSON")


@pytest.mark.parametrize("headers", [
    {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, {"Content-Encoding": "gzip"},
])
def test_size_and_encoding_headers_are_checked_before_body(resolver, github, source, headers):
    def must_not_read():
        pytest.fail("rejected response must not be read or decompressed")
        yield b""

    stream = ByteStream(must_not_read())
    github.reply = lambda request: httpx.Response(200, headers=headers, stream=stream)
    assert_error(resolver, source, 502)
    assert github.urls == [REPO_API]
    assert stream.closed


def test_stream_size_limit_ignores_understated_content_length(resolver, github, source):
    def chunks():
        for _ in range(MAX_RESPONSE_BYTES // 65536 + 1):
            yield b"x" * 65536
        pytest.fail("the response must be stopped at the byte limit")

    stream = ByteStream(chunks())
    github.reply = lambda request: (
        httpx.Response(200, headers={"Content-Length": "1"}, stream=stream)
        if str(request.url) != REPO_API else None
    )
    assert_error(resolver, source, 502, "size limit")
    assert stream.closed


@pytest.fixture
def clock(monkeypatch):
    ticks = [1000.0]
    fake = SimpleNamespace(monotonic=lambda: ticks[0], time=time.time)
    monkeypatch.setattr(github_module, "time", fake)
    monkeypatch.setattr(resolver_module, "time", fake)
    return ticks


@pytest.mark.parametrize("endpoint", [REPO_API, commit_endpoint("main")])
@pytest.mark.parametrize("late_eof", [False, True])
def test_receive_deadline_covers_slow_chunks_and_eof(resolver, github, source, clock, endpoint, late_eof):
    def chunks():
        payload = github.repository if endpoint == REPO_API else github.commit
        content = json.dumps(payload).encode("utf-8")
        yield content if late_eof else content[:1]
        clock[0] += 25
        if not late_eof:
            yield content[1:2]
            pytest.fail("the stream must stop as soon as the shared budget expires")

    stream = ByteStream(chunks())
    github.reply = lambda request: httpx.Response(200, stream=stream) if str(request.url) == endpoint else None
    assert_error(resolver, source, 504, "timed out")
    assert github.urls == ([REPO_API] if endpoint == REPO_API else [REPO_API, endpoint])
    assert stream.closed


def test_both_reads_share_25_second_budget_and_remaining_io_timeouts(resolver, github, source, clock, monkeypatch):
    deadlines = []
    observed = []
    read = resolver.github_client._json

    def track_reader(client, url, deadline):
        deadlines.append(deadline)
        return read(client, url, deadline)

    def spend_budget(request):
        observed.append(dict(request.extensions["timeout"]))
        if str(request.url) == REPO_API:
            clock[0] += 24.75

    monkeypatch.setattr(resolver.github_client, "_json", track_reader)
    github.reply = spend_budget
    assert resolver.resolve(source).base_sha == SHA
    assert deadlines == [1025.0, 1025.0]
    assert all(0 < timeout <= 0.25 for timeout in observed[1].values())
    assert set(observed[1]) == {"connect", "read", "write", "pool"}


def test_budget_exhausted_between_reads_prevents_commit_request(resolver, github, source, clock, monkeypatch):
    read = resolver.github_client._json

    def expire_after_repository(client, url, deadline):
        result = read(client, url, deadline)
        clock[0] = deadline
        return result

    monkeypatch.setattr(resolver.github_client, "_json", expire_after_repository)
    assert_error(resolver, source, 504, "timed out")
    assert github.urls == [REPO_API]


def test_budget_exhausted_after_commit_still_fails(resolver, github, source, clock, monkeypatch):
    read = resolver.github_client._json

    def expire_after_commit(client, url, deadline):
        result = read(client, url, deadline)
        if url != REPO_API:
            clock[0] = deadline
        return result

    monkeypatch.setattr(resolver.github_client, "_json", expire_after_commit)
    assert_error(resolver, source, 504, "time budget")
