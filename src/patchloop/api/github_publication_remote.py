"""Authenticated, bounded GitHub publication requests to fixed REST endpoints."""

import json
import re
import time
from urllib.parse import quote

import httpx

from .github import API_VERSION
from .github_delivery import GitHubDeliveryConflict
from .github_delivery_models import GitHubActor, GitHubPullRequest


class GitHubPublicationError(RuntimeError):
    def __init__(self, message: str, *, status_code=502, uncertain=False):
        super().__init__(message)
        self.status_code = status_code
        self.uncertain = uncertain


class GitHubPublicationRemote:
    def __init__(self, token: str, *, transport=None):
        if not token or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token):
            raise GitHubPublicationError("configure PATCHLOOP_GITHUB_TOKEN on the server", status_code=503)
        self.client = httpx.Client(base_url="https://api.github.com", trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5), transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "Accept-Encoding": "identity", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "patchloop"})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def _request(self, method, path, *, missing=False, **kwargs):
        writing = method != "GET"
        deadline = time.monotonic() + 25
        try:
            with self.client.stream(method, path, **kwargs) as response:
                if missing and response.status_code == 404:
                    return None
                expected = 201 if writing else 200
                if response.status_code != expected:
                    # Never persist provider text, URLs with credentials, or headers.
                    raise GitHubPublicationError(f"GitHub returned HTTP {response.status_code}; query before retrying",
                        uncertain=writing and response.status_code not in (400, 401, 403, 404, 409, 422, 429))
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise ValueError("compressed response")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024 or time.monotonic() >= deadline:
                        raise ValueError("response limit")
                if time.monotonic() >= deadline:
                    raise ValueError("response time limit")
                def reject(value):
                    raise ValueError("invalid JSON")
                return json.loads(body, parse_constant=reject)
        except GitHubPublicationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubPublicationError("GitHub response unavailable or invalid; query before retrying",
                                         uncertain=writing) from exc

    @staticmethod
    def _path(plan):
        return f"/repos/{plan.repository.full_name}"

    def identity(self, plan) -> GitHubActor:
        actor = self._request("GET", "/user")
        repo = self._request("GET", self._path(plan))
        if (not isinstance(actor, dict) or type(actor.get("id")) is not int or actor["id"] <= 0
                or not isinstance(actor.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", actor["login"])):
            raise GitHubPublicationError("GitHub account identity is invalid")
        name = plan.repository.full_name.casefold()
        if (not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] != plan.repository.id
                or str(repo.get("full_name", "")).casefold() != name
                or str(repo.get("html_url", "")).casefold() != f"https://github.com/{name}"
                or str(repo.get("url", "")).casefold() != f"https://api.github.com/repos/{name}"
                or repo.get("private") is not False or repo.get("visibility") != "public"
                or repo.get("archived") is not False or repo.get("disabled") is True
                or not isinstance(repo.get("permissions"), dict) or repo["permissions"].get("push") is not True):
            raise GitHubDeliveryConflict("remote repository identity, public status, or account write permission changed")
        return GitHubActor(id=actor["id"], login=actor["login"])

    def branch(self, plan, branch):
        value = self._request("GET", self._path(plan) + "/git/ref/heads/" + quote(branch, safe=""), missing=True)
        if value is None:
            return None
        obj = value.get("object") if isinstance(value, dict) else None
        if (not isinstance(obj, dict) or value.get("ref") != f"refs/heads/{branch}"
                or obj.get("type") != "commit" or not isinstance(obj.get("sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"])):
            raise GitHubPublicationError("GitHub returned an inconsistent branch reference")
        return obj["sha"]

    def require_base(self, plan):
        base = self.branch(plan, plan.base_branch)
        if base != plan.base_sha:
            raise GitHubDeliveryConflict("remote base differs from the validated base; create and review a task on the current version")
        return base

    def pulls(self, plan):
        result = []
        owner = plan.repository.full_name.split("/")[0]
        for page in range(1, 6):
            values = self._request("GET", self._path(plan) + "/pulls", params={
                "state": "all", "head": f"{owner}:{plan.head_branch}", "base": plan.base_branch,
                "per_page": 100, "page": page,
            })
            if not isinstance(values, list):
                raise GitHubPublicationError("GitHub returned an invalid PR list")
            # Verify the server-side filter instead of trusting it for recovery.
            for value in values:
                if not isinstance(value, dict):
                    raise GitHubPublicationError("GitHub returned an invalid PR entry")
                head, base = value.get("head"), value.get("base")
                if (not isinstance(head, dict) or not isinstance(base, dict)
                        or head.get("ref") != plan.head_branch or base.get("ref") != plan.base_branch):
                    raise GitHubPublicationError("GitHub returned a PR outside the requested branches")
            result.extend(values)
            if len(values) < 100:
                return result
        raise GitHubPublicationError("PR lookup exceeded its page limit; inspect GitHub before continuing")

    def read_pull(self, value, plan, actor) -> GitHubPullRequest:
        try:
            number = value["number"]
            if type(number) is not int or number <= 0:
                raise ValueError()
            if any(type(identifier) is not int for identifier in (
                    value["user"]["id"], value["head"]["repo"]["id"], value["base"]["repo"]["id"])):
                raise ValueError()
            if (value["html_url"] != f"https://github.com/{plan.repository.full_name}/pull/{number}"
                    or value["head"]["ref"] != plan.head_branch or value["base"]["ref"] != plan.base_branch
                    or value["head"]["sha"] != plan.head_sha or value["user"]["id"] != actor.id
                    or value["head"]["repo"]["id"] != plan.repository.id
                    or value["base"]["repo"]["id"] != plan.repository.id
                    or value["title"] != plan.title or value["body"] != plan.body or value["draft"] is not plan.draft):
                raise ValueError()
            return GitHubPullRequest(number=number, url=value["html_url"], state=value["state"],
                                     base_sha=value["base"]["sha"], head_sha=value["head"]["sha"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubDeliveryConflict("existing PR does not match the confirmed account, content, or commit; inspect GitHub") from exc

    def create_pull(self, plan, actor):
        value = self._request("POST", self._path(plan) + "/pulls", json={
            "title": plan.title, "body": plan.body, "base": plan.base_branch,
            "head": plan.head_branch, "draft": plan.draft,
        })
        try:
            return self.read_pull(value, plan, actor)
        except GitHubDeliveryConflict as exc:
            raise GitHubPublicationError("GitHub may have created the PR but returned inconsistent details; query before retrying",
                                         uncertain=True) from exc
