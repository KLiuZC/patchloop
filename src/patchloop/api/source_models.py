"""Immutable public GitHub Issue input, independent of repair execution."""

from dataclasses import dataclass
import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator


_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,99}"
_REPO = r"[A-Za-z0-9_.-]{1,100}"
_ISSUE_PATH = re.compile(rf"^/({_OWNER})/({_REPO})/issues/([0-9]{{1,19}})/?$")


@dataclass(frozen=True)
class IssueReference:
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"

    @property
    def repository_api_url(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def api_url(self) -> str:
        return f"{self.repository_api_url}/issues/{self.number}"


def parse_issue_url(value: str) -> IssueReference:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("use an HTTPS GitHub Issue URL without control characters")
    parts = urlsplit(value.strip())
    if parts.scheme.lower() != "https" or parts.netloc.lower() != "github.com":
        raise ValueError("only https://github.com/<owner>/<repo>/issues/<number> is supported")
    match = _ISSUE_PATH.fullmatch(unquote(parts.path))
    if not match or match[2] in (".", "..") or not 1 <= int(match[3]) <= 2**63 - 1:
        raise ValueError("use a GitHub Issue link, not a pull request or repository link")
    # The source is the issue itself; query strings/comment anchors are not
    # fetched or persisted, and equivalent case/number spellings share an ID key.
    return IssueReference(match[1].lower(), match[2].lower(), int(match[3]))


class IssueImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    url: Annotated[str, StringConstraints(min_length=1, max_length=4096)]

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return parse_issue_url(value).url


class GitHubRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0, strict=True)
    full_name: str
    html_url: str
    default_branch: str | None
    archived: bool
    visibility: Literal["public"] = "public"


class GitHubIssueSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str
    repository: dict[str, Any]
    issue: dict[str, Any]


class IssueSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    request_id: UUID
    kind: Literal["github_issue"] = "github_issue"
    requested_url: str
    url: str
    api_url: str
    repository: GitHubRepository
    issue_id: int = Field(gt=0, strict=True)
    issue_number: int = Field(gt=0, strict=True)
    title: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    body: Annotated[str, StringConstraints(max_length=200000)]
    state: Literal["open", "closed"]
    issue_created_at: AwareDatetime
    issue_updated_at: AwareDatetime
    fetched_at: AwareDatetime
    api_version: str
    snapshot_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class IssueImportResult(BaseModel):
    source: IssueSource
    replayed: bool
