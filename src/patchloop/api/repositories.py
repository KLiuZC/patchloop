"""Register, pin and prepare independent public repository checkouts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
from uuid import UUID, uuid4

from .github import GitHubIssueClient, GitHubReadError
from .repository_git import CheckoutError, RepositoryCheckout
from .repository_github import RepositoryResolver
from .repository_models import (RepositoryPreparation, RepositoryPreparationResult,
                                RepositoryPreparationState, RepositoryPreparationSubmission)
from .repository_store import PreparationConflict, RepositoryPreparationStore
from .source_models import parse_issue_url
from .source_store import IssueSourceStore
from .store import TaskStore


class PreparationNotFound(ValueError):
    pass


class RepositoryPreparationService:
    def __init__(self, store: TaskStore, github: GitHubIssueClient, *, root: Path | None = None):
        self.tasks = store
        self.store = RepositoryPreparationStore(store)
        self.sources = IssueSourceStore(store)
        self.resolver = RepositoryResolver(github)
        self.checkout = RepositoryCheckout()
        self.root = (root or store.path.parent / "repositories").resolve()

    @contextmanager
    def _lock(self):
        path = self.tasks.path.with_name(self.tasks.path.name + ".repositories.lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _fail_interrupted(self) -> None:
        for record in self.store.preparing():
            self._failed(record, "Repository preparation was interrupted; use a new request_id to retry", 503)

    def recover_interrupted(self) -> None:
        # A second API process must not mark another process's active fetch failed.
        with self._lock() as acquired:
            if acquired:
                self._fail_interrupted()

    @staticmethod
    def _replay(record: RepositoryPreparation, submission: RepositoryPreparationSubmission) -> RepositoryPreparationResult:
        if record.source_id != submission.source_id or record.requested_ref != submission.ref:
            raise PreparationConflict("request_id was already used for a different source or ref")
        return RepositoryPreparationResult(preparation=record, replayed=True)

    def _failed(self, record: RepositoryPreparation, error: str, status: int) -> RepositoryPreparation:
        now = datetime.now(timezone.utc)
        failed = record.model_copy(update={"status": "failed", "error": error, "error_status": status,
                                           "updated_at": now, "finished_at": now})
        self.store.update(failed)
        return failed

    def prepare(self, submission: RepositoryPreparationSubmission) -> RepositoryPreparationResult:
        existing = self.store.get_for_request(submission.request_id)
        if existing is not None:
            self._replay(existing, submission)
            if existing.status == "preparing":
                with self._lock() as acquired:
                    if acquired:
                        self._fail_interrupted()
                        existing = self.store.get_for_request(submission.request_id)
            return self._replay(existing, submission)
        source = self.sources.get(submission.source_id)
        if source is None:
            raise PreparationNotFound("Issue source not found; import the Issue first")

        with self._lock() as acquired:
            existing = self.store.get_for_request(submission.request_id)
            if existing is not None:
                return self._replay(existing, submission)
            if not acquired:
                raise PreparationConflict("Another repository is being prepared; retry this request later")
            self._fail_interrupted()
            reference = parse_issue_url(source.requested_url)
            now = datetime.now(timezone.utc)
            preparation_id = uuid4()
            path = self.root / str(preparation_id) / "repo"
            record = RepositoryPreparation(
                preparation_id=preparation_id, request_id=submission.request_id, source_id=submission.source_id,
                repository=source.repository, requested_ref=submission.ref,
                clone_url=f"https://github.com/{reference.owner}/{reference.repo}.git", repo_path=str(path),
                created_at=now, updated_at=now,
            )
            saved = self.store.create(record)
            if saved.replayed:
                return saved
            try:
                resolved = self.resolver.resolve(source, submission.ref)
                record = record.model_copy(update={
                    "repository": resolved.repository, "resolved_ref": resolved.ref,
                    "base_sha": resolved.base_sha, "resolved_at": resolved.resolved_at,
                    "updated_at": datetime.now(timezone.utc),
                })
                # Persist the SHA before any fetch: a moving branch cannot change
                # which version this preparation represents.
                self.store.update(record)
                if self.root.resolve() != self.root:
                    raise CheckoutError("Repository storage root changed")
                self.root.mkdir(parents=True, exist_ok=True)
                path.parent.mkdir(exist_ok=False)
                self.checkout.prepare(path, record.clone_url, record.base_sha)
                now = datetime.now(timezone.utc)
                record = record.model_copy(update={"status": "ready", "updated_at": now, "finished_at": now})
                self.store.update(record)
            except GitHubReadError as exc:
                record = self._failed(record, str(exc), exc.status_code)
            except (CheckoutError, OSError) as exc:
                record = self._failed(record, str(exc) or "Repository storage is unavailable", 502)
            return RepositoryPreparationResult(preparation=record, replayed=False)

    def get(self, preparation_id: UUID) -> RepositoryPreparationState:
        record = self.store.get(preparation_id)
        if record is None:
            raise PreparationNotFound("Repository preparation not found")
        if record.status != "ready":
            return RepositoryPreparationState(preparation=record, reason=record.error)
        expected = self.root / str(record.preparation_id) / "repo"
        if record.repo_path != str(expected) or expected.resolve() != expected:
            return RepositoryPreparationState(preparation=record, checkout_intact=False,
                                              reason="Repository storage path changed")
        intact, reason = self.checkout.inspect(expected, record.base_sha)
        return RepositoryPreparationState(preparation=record, checkout_intact=intact, reason=reason)
