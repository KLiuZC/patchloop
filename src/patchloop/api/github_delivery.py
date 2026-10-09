"""Build PR previews from a current approval and an exact local delivery, offline."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from .delivery import DeliveryService, git, oid
from .github_delivery_models import (GitHubDeliveryPlan, GitHubDeliveryPreview,
                                     GitHubDeliveryPreviewRequest, GitHubPreviewVerification)
from .models import PathPolicy
from .review import ReviewConflict, review_state
from .source_models import parse_issue_url
from .store import TaskStore
from ..repair.patch_policy import inspect_patch


class GitHubDeliveryConflict(ReviewConflict):
    pass


def _fence(text: str, language: str = "text") -> str:
    # Preserve commands and paths even when they contain Markdown fences.
    width = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    marker = "`" * max(3, width + 1)
    return f"{marker}{language}\n{text}\n{marker}"


def _default_body(task, issue_url: str, paths: list[str]) -> str:
    listed = "\n".join(json.dumps(path, ensure_ascii=False) for path in paths[:50])
    more = f"\n另有 {len(paths) - 50} 个文件，见完整补丁。\n" if len(paths) > 50 else ""
    return (
        f"## 修复要求\n\n{_fence(task.goal)}\n\n"
        f"## 问题来源\n\n{issue_url}\n\n"
        f"## 修改文件\n\n{_fence(listed)}\n{more}\n"
        f"## 验证\n\n{_fence(task.verification_command, 'shell')}\n\n"
        "- 修复前：失败，退出码 1。\n- 修复后：通过，退出码 0。\n\n"
        f"验证基线：`{task.base_sha}`。验证范围为上述命令覆盖的检查。\n"
    )


class GitHubDeliveryService:
    def __init__(self, store: TaskStore, deliveries: DeliveryService):
        self.store = store
        self.deliveries = deliveries

    def _context(self, task_id: UUID):
        with self.store._connection() as connection:
            connection.execute("BEGIN")
            task, latest = self.store._review_context(connection, task_id)
            record = self.deliveries._record(connection, task_id, latest.review_id) if latest else None
        state = review_state(task, latest)
        if task.source is None:
            raise GitHubDeliveryConflict("a frozen GitHub Issue source is required for a PR preview")
        if state.status != "accepted" or not state.can_accept or latest is None:
            raise GitHubDeliveryConflict("a current accepted review with passing validation is required")
        if record is None or record.status != "completed":
            raise GitHubDeliveryConflict("complete local delivery for the current review before previewing a PR")
        if (record.task_id != task_id or record.review_id != latest.review_id
                or record.review_revision != latest.revision or record.evidence != state.evidence
                or record.target_head != task.base_sha or record.error
                or record.branch != f"patchloop/{task_id}/r{latest.revision}"
                or not re.fullmatch(r"[0-9a-f]{40}", record.commit_sha or "")
                or not re.fullmatch(r"[0-9a-f]{40}", record.tree_sha or "")):
            raise GitHubDeliveryConflict("local delivery does not match the approved task and evidence")
        return task, latest, record, state.evidence

    def _check_local(self, task, record) -> Path:
        repo = self.deliveries._repo(task)
        worktree = Path(record.worktree_path)
        if (repo != Path(task.repo_path) or worktree.resolve() != worktree
                or oid(repo, "HEAD^{commit}") != task.base_sha
                or git(repo, "symbolic-ref", "-q", f"refs/heads/{record.branch}", check=False).returncode != 1
                or not self.deliveries._intact(repo, record)):
            raise GitHubDeliveryConflict("local delivery or repository base changed; restore it and review again")
        return repo

    def preview(self, task_id: UUID, request: GitHubDeliveryPreviewRequest) -> GitHubDeliveryPreview:
        try:
            return self._preview(task_id, request)
        except GitHubDeliveryConflict:
            raise
        except (OSError, ValueError) as exc:
            # Keep inspection failures at the same conflict boundary as delivery.
            raise GitHubDeliveryConflict(str(exc) or "could not inspect the approved delivery") from exc

    def _preview(self, task_id: UUID, request: GitHubDeliveryPreviewRequest) -> GitHubDeliveryPreview:
        context = self._context(task_id)
        task, review, record, evidence = context
        source = task.source
        issue = source.issue
        reference = parse_issue_url(issue.url)
        full_name = f"{reference.owner}/{reference.repo}"
        # The publication target comes only from frozen provenance, not a remote
        # configured in the local checkout or a caller-supplied repository URL.
        if (issue.repository.full_name.casefold() != full_name
                or issue.repository.html_url.casefold() != f"https://github.com/{full_name}"
                or issue.issue_number != reference.number
                or parse_issue_url(issue.requested_url) != reference
                or issue.api_url.casefold() != reference.api_url):
            raise GitHubDeliveryConflict("saved Issue repository identity is inconsistent")
        if request.base_branch == record.branch:
            raise GitHubDeliveryConflict("PR base and head branches must differ")

        repo = self._check_local(task, record)
        if git(repo, "check-ref-format", "--branch", request.base_branch, check=False).returncode:
            raise GitHubDeliveryConflict("PR base branch is not a valid Git branch name")
        headers = git(repo, "cat-file", "commit", record.commit_sha).stdout.split(b"\n\n", 1)[0].splitlines()
        parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
        trees = [line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")]
        if parents != [task.base_sha] or trees != [record.tree_sha]:
            raise GitHubDeliveryConflict("delivery commit parent or tree differs from the approved base")
        policy = task.path_policy or PathPolicy()
        inspected = inspect_patch(repo, task.base_sha, task.result["patch"].encode("utf-8"),
                                  allowed_paths=tuple(policy.allowed_paths),
                                  protected_paths=tuple(policy.protected_paths))
        if inspected.tree_sha != record.tree_sha:
            raise GitHubDeliveryConflict("delivery commit does not contain the exact approved patch")

        title = request.title
        if title is None:
            title = f"修复 #{issue.issue_number}：{' '.join(issue.title.split())}"[:256]
        body = request.body if request.body is not None else _default_body(task, reference.url, inspected.changed_paths)
        # Apply the same content constraints to generated and edited text.
        GitHubDeliveryPreviewRequest(base_branch=request.base_branch, title=title, body=body, draft=request.draft)
        plan = GitHubDeliveryPlan(
            task_id=task_id, source_id=issue.source_id, preparation_id=source.preparation_id,
            issue_url=reference.url, repository=issue.repository, base_branch=request.base_branch,
            base_sha=task.base_sha, head_branch=record.branch, head_sha=record.commit_sha, tree_sha=record.tree_sha,
            delivery_id=record.delivery_id, review_id=review.review_id, review_revision=review.revision,
            evidence=evidence, changed_paths=inspected.changed_paths,
            verification=GitHubPreviewVerification(command=task.verification_command),
            title=title, body=body, draft=request.draft,
        )
        # A preview is a checked snapshot, not a durable authorization. Recheck
        # after Git inspection; future publication must perform its own checks.
        if self._context(task_id) != context:
            raise GitHubDeliveryConflict("approval or delivery changed while preparing the preview; reload")
        self._check_local(task, record)
        content = json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return GitHubDeliveryPreview(checked_at=datetime.now(timezone.utc),
                                     plan_sha256=hashlib.sha256(content).hexdigest(), plan=plan)
