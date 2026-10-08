"""Resolve public GitHub repository refs without changing imported Issue input."""

from datetime import datetime, timezone
import re
import time
from urllib.parse import quote

import httpx

from .github import API_VERSION, IMPORT_TIMEOUT_SECONDS, GitHubIssueClient, GitHubReadError, _positive_id, _same_url
from .repository_models import ResolvedRepository, validate_ref
from .source_models import GitHubRepository, IssueSource, parse_issue_url


class RepositoryResolver:
    def __init__(self, github_client: GitHubIssueClient):
        self.github_client = github_client

    def resolve(self, source: IssueSource, ref: str | None = None) -> ResolvedRepository:
        if ref is not None:
            try:
                if not isinstance(ref, str):
                    raise ValueError("ref must be a branch, tag or full commit SHA")
                validate_ref(ref)
            except ValueError as exc:
                raise GitHubReadError(422, str(exc)) from exc

        deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "Accept-Encoding": "identity", "User-Agent": "patchloop"}
        try:
            reference = parse_issue_url(source.requested_url)
            full_name = f"{reference.owner}/{reference.repo}"
            html_url = f"https://github.com/{full_name}"
            if (source.repository.full_name.casefold() != full_name
                    or not _same_url(source.repository.html_url, html_url)):
                raise GitHubReadError(502, "saved repository identity does not match the requested Issue URL")

            # All URLs originate from the validated Issue reference, never from
            # provider links. No credentials or redirects.
            with httpx.Client(headers=headers, timeout=httpx.Timeout(10, connect=5),
                              follow_redirects=False,
                              transport=self.github_client.transport) as client:
                repository = self.github_client._json(client, reference.repository_api_url, deadline)
                current_name = repository.get("full_name")
                if (not isinstance(current_name, str) or current_name.casefold() != full_name
                        or not _same_url(repository.get("url"), reference.repository_api_url)
                        or not _same_url(repository.get("html_url"), html_url)):
                    raise GitHubReadError(502, "GitHub repository identity changed or was renamed; import its current Issue URL")
                repository_id = _positive_id(repository.get("id"))
                if repository_id != source.repository.id:
                    raise GitHubReadError(502, "GitHub repository was replaced: its ID differs from the imported source")
                if type(repository.get("private")) is not bool:
                    raise ValueError("repository public status is missing")
                if repository["private"] or repository.get("visibility", "public") != "public":
                    raise GitHubReadError(422, "only public GitHub repositories are supported")
                if type(repository.get("archived")) is not bool:
                    raise ValueError("repository archived flag is invalid")
                branch = repository.get("default_branch")
                if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 1024):
                    raise ValueError("repository default branch is invalid")
                repo = GitHubRepository(id=repository_id, full_name=current_name,
                                        html_url=repository["html_url"], default_branch=branch,
                                        archived=repository["archived"])

                resolved_ref = ref if ref is not None else branch
                if resolved_ref is None:
                    raise GitHubReadError(422, "GitHub repository has no default branch; specify a branch, tag or full commit SHA")
                validate_ref(resolved_ref)
                commit_url = f"{reference.repository_api_url}/commits/{quote(resolved_ref, safe='')}"
                commit = self.github_client._json(client, f"{commit_url}?per_page=1", deadline)

            sha = commit.get("sha")
            if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
                raise GitHubReadError(502, "GitHub returned an invalid commit SHA; expected 40 lowercase hexadecimal characters")
            if (not _same_url(commit.get("url"), f"{reference.repository_api_url}/commits/{sha}")
                    or not _same_url(commit.get("html_url"), f"{html_url}/commit/{sha}")):
                raise GitHubReadError(502, "GitHub commit identity does not match the requested repository and resolved SHA")
            if ref is not None and re.fullmatch(r"[0-9a-fA-F]{40}", ref) and sha != ref.lower():
                raise GitHubReadError(502, "GitHub commit SHA does not match the explicitly requested full commit SHA")
            result = ResolvedRepository(repository=repo, ref=resolved_ref, base_sha=sha,
                                        resolved_at=datetime.now(timezone.utc))
            if time.monotonic() >= deadline:
                raise GitHubReadError(504, "GitHub resolution time budget exceeded; repository was not resolved")
            return result
        except GitHubReadError:
            raise
        except httpx.TimeoutException as exc:
            raise GitHubReadError(504, "GitHub request timed out; retry the same request later") from exc
        except httpx.RequestError as exc:
            raise GitHubReadError(502, "GitHub connection failed; repository was not resolved") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise GitHubReadError(502, "GitHub response is incomplete or inconsistent; repository was not resolved") from exc
