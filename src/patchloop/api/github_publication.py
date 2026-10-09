"""Explicit GitHub publication with durable intent and conservative reconciliation."""

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .github_delivery import GitHubDeliveryConflict
from .github_delivery_models import (GitHubDeliveryPreviewRequest, GitHubPublicationCheck,
    GitHubPublicationRecord, GitHubPublicationResponse, GitHubPublicationState)
from .github_publication_remote import GitHubPublicationError, GitHubPublicationRemote
from .github_publication_store import GitHubPublicationStore
from .github_push import GitHubPusher


class GitHubPublicationService:
    def __init__(self, tasks, previews, *, token_provider=None, transport=None, pusher=None):
        self.tasks = tasks
        self.previews = previews
        self.store = GitHubPublicationStore(tasks)
        self.token_provider = token_provider or (lambda: os.environ.get("PATCHLOOP_GITHUB_TOKEN", ""))
        self.transport = transport
        self.pusher = pusher or GitHubPusher()

    @contextmanager
    def _lock(self):
        path = self.tasks.path.with_name(self.tasks.path.name + ".github-publish.lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GitHubDeliveryConflict("a publication is running; query its saved state before retrying") from exc
            yield
        finally:
            os.close(descriptor)

    def get(self, task_id):
        return GitHubPublicationState(task_id=task_id, configured=bool(self.token_provider()), items=self.store.list(task_id))

    def check(self, task_id, request):
        preview = self.previews.preview(task_id, request)
        with GitHubPublicationRemote(self.token_provider(), transport=self.transport) as remote:
            actor = remote.identity(preview.plan)
            base = remote.require_base(preview.plan)
            head = remote.branch(preview.plan, preview.plan.head_branch)
            pulls = remote.pulls(preview.plan)
            existing = self.store.find(task_id, delivery_id=preview.plan.delivery_id)
            allowed = head is None and not pulls and existing is None
            if self.previews.preview(task_id, request).plan_sha256 != preview.plan_sha256:
                raise GitHubDeliveryConflict("local approval changed during remote checks; reload the preview")
            return GitHubPublicationCheck(preview=preview, actor=actor, remote_base_sha=base,
                remote_head_sha=head, can_publish=allowed,
                reason="" if allowed else "a branch, PR, or publication already exists; inspect the saved publication before retrying")

    def _validate_local(self, record):
        plan = record.plan
        request = GitHubDeliveryPreviewRequest(base_branch=plan.base_branch, title=plan.title,
                                               body=plan.body, draft=plan.draft)
        if self.previews.preview(record.task_id, request).plan_sha256 != record.plan_sha256:
            raise GitHubDeliveryConflict("approved publication plan changed; reload and review")

    def _identity(self, remote, record):
        actor = remote.identity(record.plan)
        if record.actor is not None and record.actor != actor:
            raise GitHubDeliveryConflict("publication account changed; restore the original account before recovery")
        if record.actor is None:
            record.actor = actor
            self.store.save(record)

    def _complete(self, record, pull):
        record.pull_request = pull
        if pull.base_sha != record.plan.base_sha:
            record.status = "blocked"
            record.error = "PR exists but its base moved; inspect the saved PR and revalidate before use"
        else:
            record.status = "completed"
            record.phase = "completed"
            record.error = ""
        self.store.save(record)

    def _find_pull(self, remote, record):
        values = remote.pulls(record.plan)
        if not values:
            return False
        if len(values) != 1 or not record.pr_attempted:
            raise GitHubDeliveryConflict("a PR already uses these branches; it will not be adopted or duplicated")
        self._complete(record, remote.read_pull(values[0], record.plan, record.actor))
        return True

    def _advance(self, record, remote, token):
        record.status = "pending"
        record.error = ""
        self.store.save(record)
        try:
            self._identity(remote, record)
            # Reconcile an already-issued write before considering further writes.
            if self._find_pull(remote, record):
                return
            if record.pr_attempted:
                record.status = "uncertain"
                record.error = "PR creation outcome is still unknown; this retry only queried GitHub and did not create another PR"
                self.store.save(record)
                return
            self._validate_local(record)
            remote.require_base(record.plan)
            head = remote.branch(record.plan, record.plan.head_branch)
            if head is not None and (head != record.plan.head_sha or not record.push_attempted):
                raise GitHubDeliveryConflict("remote delivery branch already exists or changed; it will not be overwritten")
            if head is None:
                if record.branch_pushed:
                    raise GitHubDeliveryConflict("published branch was removed; it will not be recreated automatically")
                record.phase = "push"
                record.push_attempted = True
                self.store.save(record)

                def before_push():
                    self._validate_local(record)
                    self._identity(remote, record)
                    remote.require_base(record.plan)
                    if remote.branch(record.plan, record.plan.head_branch) is not None:
                        raise GitHubDeliveryConflict("remote branch appeared during preparation; query before retrying")

                repo = self.tasks.get(record.task_id).repo_path
                self.pusher.push(Path(repo), record.plan, token, before_push)
                if remote.branch(record.plan, record.plan.head_branch) != record.plan.head_sha:
                    raise GitHubPublicationError("push outcome could not be verified; query before retrying", uncertain=True)
            record.branch_pushed = True
            record.phase = "pull_request"
            self.store.save(record)
            # Base/approval changes after a successful push stop PR creation. The
            # already published branch is retained, never deleted to hide failure.
            self._validate_local(record)
            self._identity(remote, record)
            remote.require_base(record.plan)
            if remote.branch(record.plan, record.plan.head_branch) != record.plan.head_sha:
                raise GitHubDeliveryConflict("remote branch changed after push; PR creation stopped")
            if self._find_pull(remote, record):
                return
            record.pr_attempted = True
            self.store.save(record)
            try:
                pull = remote.create_pull(record.plan, record.actor)
            except GitHubPublicationError as exc:
                # A definitive HTTP refusal can be retried after fixing access or
                # input. Lost responses retain intent and permit lookup only.
                if not exc.uncertain:
                    record.pr_attempted = False
                raise
            self._complete(record, pull)
        except (GitHubDeliveryConflict, GitHubPublicationError) as exc:
            record.status = "uncertain" if record.pr_attempted or getattr(exc, "uncertain", False) else "blocked"
            record.error = str(exc)
            self.store.save(record)

    def publish(self, task_id, submission):
        with self._lock():
            record = self.store.find(task_id, request_id=submission.request_id)
            replayed = record is not None
            if record is not None:
                if record.submission != submission:
                    raise GitHubDeliveryConflict("request_id was already used for another publication intent")
            else:
                preview = self.previews.preview(task_id, submission.preview)
                if preview.plan_sha256 != submission.plan_sha256:
                    raise GitHubDeliveryConflict("preview changed; explicitly confirm the current plan")
                record = self.store.find(task_id, delivery_id=preview.plan.delivery_id)
                if record is not None:
                    if record.plan_sha256 != submission.plan_sha256:
                        raise GitHubDeliveryConflict("this delivery already has a different confirmed publication plan")
                    replayed = True
                else:
                    now = datetime.now(timezone.utc)
                    record = GitHubPublicationRecord(publication_id=uuid4(), task_id=task_id,
                        request_id=submission.request_id, submission=submission, plan_sha256=preview.plan_sha256,
                        plan=preview.plan, created_at=now, updated_at=now)
                    self.store.create(record)
            if record.status == "completed":
                return GitHubPublicationResponse(publication=record, replayed=True)
            token = self.token_provider()
            try:
                with GitHubPublicationRemote(token, transport=self.transport) as remote:
                    self._advance(record, remote, token)
            except GitHubPublicationError as exc:
                record.status = "blocked"
                record.error = str(exc)
                self.store.save(record)
            return GitHubPublicationResponse(publication=record, replayed=replayed)
