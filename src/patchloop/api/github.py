"""Read public Issue input from fixed GitHub REST endpoints; never execute it."""

from datetime import datetime, timezone
import json
import math
import time
from uuid import uuid4

import httpx

from .source_models import GitHubIssueSnapshot, GitHubRepository, IssueImport, IssueSource, parse_issue_url
from .source_store import SourceCorrupt, snapshot_digest


API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
IMPORT_TIMEOUT_SECONDS = 25


def _json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is not finite")
    return number


class GitHubReadError(RuntimeError):
    def __init__(self, status_code: int, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.retry_after = retry_after


def _positive_id(value) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("invalid GitHub identifier")
    return value


def _date(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid GitHub timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("GitHub timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _same_url(value, expected: str) -> bool:
    return isinstance(value, str) and value.casefold().rstrip("/") == expected.casefold()


def _retry_after(headers: httpx.Headers) -> int | None:
    delay = headers.get("retry-after", "")
    if delay.isascii() and delay.isdigit() and len(delay) <= 10:
        return min(max(int(delay), 1), 86400)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isascii() and reset.isdigit() and len(reset) <= 12:
        return min(max(int(reset) - int(time.time()), 1), 86400)
    return None


class GitHubIssueClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def _json(self, client: httpx.Client, url: str, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitHubReadError(504, "GitHub request timed out; retry the same request later")
        timeout = httpx.Timeout(min(10, remaining), connect=min(5, remaining))
        with client.stream("GET", url, timeout=timeout) as response:
            code = response.status_code
            if code == 429 or (code == 403 and (response.headers.get("x-ratelimit-remaining") == "0"
                                              or "retry-after" in response.headers)):
                raise GitHubReadError(429, "GitHub rate limit reached; retry later", _retry_after(response.headers))
            if code in (404, 410):
                raise GitHubReadError(404, "public GitHub repository or issue not found")
            if 300 <= code < 400:
                raise GitHubReadError(422, "GitHub resource redirected; use the issue's current canonical URL")
            if code not in (200, 403):
                raise GitHubReadError(502, f"GitHub returned HTTP {code}; source was not imported")
            # Ask for identity encoding and reject anything else before HTTPX
            # can decompress it. The byte cap must also bound decoded memory.
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise GitHubReadError(502, "GitHub response uses unsupported compressed encoding")
            length = response.headers.get("content-length", "")
            if length.isascii() and length.isdigit() and (len(length) > 10 or int(length) > MAX_RESPONSE_BYTES):
                raise GitHubReadError(502, "GitHub response exceeds the import size limit")
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise GitHubReadError(504, "GitHub request timed out; source was not imported")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise GitHubReadError(502, "GitHub response exceeds the import size limit")
                chunks.append(chunk)
            if time.monotonic() >= deadline:
                raise GitHubReadError(504, "GitHub request timed out; source was not imported")
            try:
                value = json.loads(b"".join(chunks), parse_constant=_json_constant, parse_float=_json_float)
            except (ValueError, UnicodeError) as exc:
                raise GitHubReadError(502, "GitHub returned invalid JSON; source was not imported") from exc
            if not isinstance(value, dict):
                raise GitHubReadError(502, "GitHub response must be a JSON object")
            if code == 403:
                message = value.get("message")
                if isinstance(message, str) and "rate limit" in message.lower():
                    raise GitHubReadError(429, "GitHub rate limit reached; retry later", _retry_after(response.headers))
                raise GitHubReadError(502, "GitHub refused the public source request")
            return value

    def fetch(self, submission: IssueImport) -> tuple[IssueSource, GitHubIssueSnapshot]:
        reference = parse_issue_url(submission.url)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "Accept-Encoding": "identity", "User-Agent": "patchloop"}
        deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
        try:
            # No token is read from the environment. Public-only input uses
            # unauthenticated GETs and never follows user/provider redirects.
            with httpx.Client(headers=headers, timeout=httpx.Timeout(10, connect=5),
                              follow_redirects=False, transport=self.transport) as client:
                repository = self._json(client, reference.repository_api_url, deadline)
                full_name = repository.get("full_name")
                if (not isinstance(full_name, str) or full_name.casefold() != f"{reference.owner}/{reference.repo}"
                        or not _same_url(repository.get("url"), reference.repository_api_url)
                        or not _same_url(repository.get("html_url"), f"https://github.com/{reference.owner}/{reference.repo}")):
                    raise ValueError("repository identity does not match the requested URL")
                if type(repository.get("private")) is not bool:
                    raise ValueError("repository public status is missing")
                if repository["private"] or repository.get("visibility", "public") != "public":
                    raise GitHubReadError(422, "only public GitHub repositories are supported")
                if type(repository.get("archived")) is not bool:
                    raise ValueError("repository archived flag is invalid")
                branch = repository.get("default_branch")
                if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 1024):
                    raise ValueError("repository default branch is invalid")
                repo = GitHubRepository(id=_positive_id(repository.get("id")), full_name=full_name,
                                        html_url=repository["html_url"], default_branch=branch,
                                        archived=repository["archived"])
                issue = self._json(client, reference.api_url, deadline)
            if "pull_request" in issue:
                raise GitHubReadError(422, "this URL identifies a pull request; import an Issue instead")
            if (_positive_id(issue.get("number")) != reference.number
                    or not _same_url(issue.get("url"), reference.api_url)
                    or not _same_url(issue.get("repository_url"), reference.repository_api_url)
                    or parse_issue_url(issue.get("html_url")).url != reference.url):
                raise ValueError("issue identity does not match the requested URL")
            if "body" not in issue or (issue["body"] is not None and not isinstance(issue["body"], str)):
                raise ValueError("issue body is invalid")
            if not isinstance(issue.get("title"), str) or not isinstance(issue.get("state"), str):
                raise ValueError("issue title or state is invalid")
            snapshot = GitHubIssueSnapshot(api_version=API_VERSION, repository=repository, issue=issue)
            source = IssueSource(
                source_id=uuid4(), request_id=submission.request_id, requested_url=reference.url,
                url=reference.url, api_url=reference.api_url, repository=repo,
                issue_id=_positive_id(issue.get("id")), issue_number=reference.number,
                title=issue["title"], body=issue["body"] or "", state=issue["state"],
                issue_created_at=_date(issue.get("created_at")), issue_updated_at=_date(issue.get("updated_at")),
                fetched_at=datetime.now(timezone.utc), api_version=API_VERSION,
                snapshot_sha256=snapshot_digest(snapshot),
            )
            if time.monotonic() >= deadline:
                raise GitHubReadError(504, "GitHub import time budget exceeded; source was not imported")
            return source, snapshot
        except GitHubReadError:
            raise
        except httpx.TimeoutException as exc:
            raise GitHubReadError(504, "GitHub request timed out; retry the same request later") from exc
        except httpx.RequestError as exc:
            raise GitHubReadError(502, "GitHub connection failed; source was not imported") from exc
        except (ValueError, TypeError, KeyError, SourceCorrupt) as exc:
            raise GitHubReadError(502, "GitHub response is incomplete or inconsistent; source was not imported") from exc
