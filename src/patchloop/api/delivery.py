"""Publish an approved patch as a local branch and an independent Git worktree."""

import fcntl
import hashlib
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .models import DeliveryRecord, DeliveryResponse, DeliveryState, DeliverySubmission, PathPolicy
from .review import ReviewConflict, review_state
from .store import TaskStore
from ..repair.patch_policy import PatchPolicyError, inspect_patch


class DeliveryConflict(ReviewConflict):
    pass


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_ATTR_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1",
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def git(repo: Path, *args: str, input: bytes | None = None, extra_env=None, check=True) -> subprocess.CompletedProcess:
    env = git_env()
    if extra_env:
        env.update(extra_env)
    filters = subprocess.run(["git", "-C", str(repo), "config", "--null", "--name-only", "--get-regexp",
                              r"^filter\..*\.(smudge|clean|process|required)$"],
                             capture_output=True, env=env, timeout=10)
    filter_options = []
    for key in filters.stdout.decode("utf-8").split("\x00"):
        if key:
            filter_options.extend(("-c", key + ("=false" if key.endswith(".required") else "=")))
    command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
               "-c", "commit.gpgSign=false", "-c", "core.autocrlf=false",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.allow=never",
               "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true", *filter_options, "-C", str(repo), *args]
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryConflict("Git operation failed or timed out") from exc
    if check and result.returncode:
        raise DeliveryConflict(result.stderr.decode("utf-8", errors="replace").strip() or "Git operation failed")
    return result


def oid(repo: Path, revision: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", "--end-of-options", revision, check=False)
    value = result.stdout.decode().strip()
    return value if result.returncode == 0 and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) else None


def ref_exists(repo: Path, ref: str) -> bool:
    # Resolving an OID alone misses dangling symbolic refs. Both forms reserve
    # the name; errors also fail closed rather than treating it as available.
    direct = git(repo, "show-ref", "--verify", "--quiet", ref, check=False)
    symbolic = git(repo, "symbolic-ref", "-q", ref, check=False)
    return direct.returncode != 1 or symbolic.returncode != 1


def _source_state(repo: Path) -> tuple:
    index = Path(os.fsdecode(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.removesuffix(b"\n")))
    return (oid(repo, "HEAD^{commit}"), git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout,
            hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None,
            git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout)


def _identity(repo: Path) -> dict[str, str]:
    # Use the user's configured Git identity, including their normal global config.
    env = git_env()
    env.pop("GIT_CONFIG_GLOBAL")
    result = subprocess.run(["git", "-C", str(repo), "var", "GIT_AUTHOR_IDENT"],
                            capture_output=True, env=env, timeout=10)
    match = re.fullmatch(r"(.+) <([^<>\n]+)> \d+ [+-]\d{4}\n?", result.stdout.decode("utf-8"))
    if result.returncode or not match:
        raise DeliveryConflict("configure Git user.name and user.email before delivery")
    return {"GIT_AUTHOR_NAME": match[1], "GIT_COMMITTER_NAME": match[1],
            "GIT_AUTHOR_EMAIL": match[2], "GIT_COMMITTER_EMAIL": match[2]}


def _owned_worktree(repo: Path, path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    other = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    top = os.fsdecode(git(path, "rev-parse", "--show-toplevel", check=False).stdout.removesuffix(b"\n"))
    return common == other and Path(top).resolve() == path.resolve()


class DeliveryService:
    def __init__(self, store: TaskStore, root: Path | None = None):
        self.store = store
        self.root = (root or store.path.parent / "deliveries").resolve()

    @contextmanager
    def _lock(self):
        path = self.store.path.with_name(self.store.path.name + ".delivery.lock")
        with path.open("a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeliveryConflict("another delivery is running; query the result before retrying") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _decode(row) -> DeliveryRecord | None:
        return DeliveryRecord.model_validate_json(row["record_json"]) if row else None

    def _record(self, connection, task_id: UUID, review_id: UUID | None = None):
        if review_id is not None:
            row = connection.execute("SELECT record_json FROM task_deliveries WHERE task_id=? AND review_id=?",
                                     (str(task_id), str(review_id))).fetchone()
        else:
            row = connection.execute("SELECT record_json FROM task_deliveries WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                                     (str(task_id),)).fetchone()
        return self._decode(row)

    @staticmethod
    def _save(connection, record: DeliveryRecord):
        record.updated_at = datetime.now(timezone.utc)
        connection.execute("UPDATE task_deliveries SET record_json=? WHERE delivery_id=?",
                           (record.model_dump_json(), str(record.delivery_id)))

    def _destination(self, repo: Path, task_id: UUID, revision: int) -> Path:
        path = self.root / str(task_id) / f"r{revision}"
        if not path.resolve().is_relative_to(self.root) or self.root == repo:
            raise DeliveryConflict("delivery directory must be a separate managed worktree")
        if path.is_relative_to(repo):
            ignored = git(repo, "check-ignore", "-q", "--", str(path), check=False)
            if ignored.returncode:
                raise DeliveryConflict("delivery directory inside source repository must be Git-ignored")
        return path

    def _repo(self, task) -> Path:
        repo = Path(task.repo_path).resolve()
        actual = os.fsdecode(git(repo, "rev-parse", "--show-toplevel").stdout.removesuffix(b"\n"))
        if Path(actual).resolve() != repo:
            raise DeliveryConflict("repository location changed")
        return repo

    @staticmethod
    def _intact(repo: Path, record: DeliveryRecord) -> bool:
        worktree = Path(record.worktree_path)
        return bool(record.commit_sha and worktree.is_dir()
                    and _owned_worktree(repo, worktree)
                    and oid(repo, f"refs/heads/{record.branch}") == record.commit_sha
                    and oid(worktree, "HEAD^{commit}") == record.commit_sha
                    and git(worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() == f"refs/heads/{record.branch}"
                    and not git(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout)

    def inspect(self, task_id: UUID) -> DeliveryState:
        with self.store._connection() as connection:
            connection.execute("BEGIN")
            task, latest = self.store._review_context(connection, task_id)
            state = review_state(task, latest)
            record = self._record(connection, task_id, latest.review_id if latest else None)
            previous = record or self._record(connection, task_id)
        policy = task.path_policy or PathPolicy()
        result = DeliveryState(task_id=task_id, review_revision=state.revision,
                               review_id=latest.review_id if latest else None, evidence=state.evidence,
                               policy=policy, delivery=previous)
        try:
            if previous and previous.status == "completed":
                result.delivery_intact = self._intact(self._repo(task), previous)
            if record and record.status == "completed":
                repo = self._repo(task)
                result.target_head = oid(repo, "HEAD^{commit}")
                result.dirty = bool(_source_state(repo)[3])
                result.branch = record.branch
                result.worktree_path = record.worktree_path
                if record.commit_sha and oid(repo, f"{record.commit_sha}^{{commit}}"):
                    raw = git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                              record.target_head, record.commit_sha, "--").stdout
                    result.changed_paths = [os.fsdecode(path) for path in raw.split(b"\0") if path]
                result.reason = "delivery already completed" if result.delivery_intact else "delivered branch or worktree has changed; it will not be overwritten"
                return result
            if state.status != "accepted" or not state.can_accept or latest is None:
                result.reason = "a current accepted review with passing validation is required"
                return result
            repo = self._repo(task)
            result.target_head = oid(repo, "HEAD^{commit}")
            if result.target_head != task.base_sha:
                raise DeliveryConflict("repository HEAD differs from the validated base; create a task from the current version and review it")
            result.branch = f"patchloop/{task_id}/r{latest.revision}"
            destination = self._destination(repo, task_id, latest.revision)
            result.worktree_path = str(destination)
            if record and Path(record.worktree_path) != destination:
                raise DeliveryConflict("delivery output configuration changed; restore the recorded output directory before resuming")
            result.dirty = bool(_source_state(repo)[3])
            inspected = inspect_patch(repo, task.base_sha, task.result["patch"].encode("utf-8"),
                                      allowed_paths=tuple(policy.allowed_paths), protected_paths=tuple(policy.protected_paths))
            result.changed_paths = inspected.changed_paths
            if record:
                if record.status == "failed":
                    result.reason = record.error
                    return result
                result.can_deliver = True
                result.reason = "delivery may be running; retry the same request to reconcile an interrupted attempt"
                return result
            if ref_exists(repo, f"refs/heads/{result.branch}") or os.path.lexists(destination):
                raise DeliveryConflict("delivery branch or directory already exists and is not owned by this delivery")
            result.can_deliver = True
        except (DeliveryConflict, PatchPolicyError, OSError, ValueError) as exc:
            result.reason = str(exc)
        return result

    @staticmethod
    def _matches(record: DeliveryRecord, submission: DeliverySubmission) -> bool:
        return (record.review_id == submission.review_id and record.review_revision == submission.review_revision
                and record.evidence == submission.evidence and record.target_head == submission.target_head)

    def _check_approval(self, connection, task_id, submission):
        task, latest = self.store._review_context(connection, task_id)
        state = review_state(task, latest)
        if (state.status != "accepted" or not state.can_accept or latest is None
                or latest.review_id != submission.review_id or state.revision != submission.review_revision
                or state.evidence != submission.evidence):
            raise DeliveryConflict("review or repair evidence changed; reload and review before delivery")
        if submission.target_head != task.base_sha or oid(self._repo(task), "HEAD^{commit}") != submission.target_head:
            raise DeliveryConflict("delivery target changed; the approved patch cannot be retargeted")
        return task

    def _recover(self, repo: Path, record: DeliveryRecord) -> bool:
        if not record.commit_sha or not record.tree_sha:
            return False
        if oid(repo, f"refs/heads/{record.branch}") != record.commit_sha:
            return False
        if git(repo, "symbolic-ref", "-q", f"refs/heads/{record.branch}", check=False).returncode == 0:
            raise DeliveryConflict("delivery ref is now symbolic; it will not be adopted or overwritten")
        worktree = Path(record.worktree_path)
        if (not _owned_worktree(repo, worktree) or oid(worktree, "HEAD^{commit}") != record.commit_sha
                or oid(repo, f"{record.commit_sha}^{{tree}}") != record.tree_sha
                or oid(repo, f"{record.commit_sha}^") != record.target_head):
            raise DeliveryConflict("interrupted delivery requires inspection; existing output will not be overwritten")
        current_ref = git(worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        if current_ref not in ("", f"refs/heads/{record.branch}"):
            raise DeliveryConflict("delivery worktree is now attached to a different branch")
        git(worktree, "symbolic-ref", "HEAD", f"refs/heads/{record.branch}")
        record.status = "completed"
        record.error = ""
        with self.store._connection() as connection:
            self._save(connection, record)
        return True

    def deliver(self, task_id: UUID, submission: DeliverySubmission) -> DeliveryResponse:
        # No remote publication, checkout, reset or stash in the original working tree.
        with self._lock():
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task, _ = self.store._review_context(connection, task_id)
                row = connection.execute("SELECT record_json FROM task_deliveries WHERE task_id=? AND request_id=?",
                                         (str(task_id), str(submission.request_id))).fetchone()
                record = self._decode(row) or self._record(connection, task_id, submission.review_id)
                if record and not self._matches(record, submission):
                    raise DeliveryConflict("delivery request was already used with different approval or target")
                if record and record.status in ("completed", "failed"):
                    return DeliveryResponse(delivery=record, replayed=True)
            repo = self._repo(task)
            if record and self._recover(repo, record):
                return DeliveryResponse(delivery=record, replayed=True)
            plan = self.inspect(task_id)
            if not plan.can_deliver or plan.review_id != submission.review_id or plan.evidence != submission.evidence:
                raise DeliveryConflict(plan.reason or "delivery plan changed")
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = self._check_approval(connection, task_id, submission)
                if record is None:
                    now = datetime.now(timezone.utc)
                    record = DeliveryRecord(delivery_id=uuid4(), task_id=task_id, request_id=submission.request_id,
                                            review_id=submission.review_id, review_revision=submission.review_revision,
                                            evidence=submission.evidence, target_head=submission.target_head,
                                            branch=plan.branch, worktree_path=plan.worktree_path,
                                            status="preparing", created_at=now, updated_at=now)
                    connection.execute("INSERT INTO task_deliveries VALUES (?, ?, ?, ?, ?)",
                                       (str(record.delivery_id), str(task_id), str(record.request_id), str(record.review_id), record.model_dump_json()))
            try:
                self._prepare_and_publish(task, submission, record)
            except Exception as exc:
                # If publishing succeeded before an interruption, reconcile the exact
                # recorded commit instead of making another branch or undoing it.
                if self._recover(repo, record):
                    return DeliveryResponse(delivery=record, replayed=True)
                record.status = "failed"
                record.error = str(exc) or type(exc).__name__
                with self.store._connection() as connection:
                    self._save(connection, record)
            return DeliveryResponse(delivery=record, replayed=False)

    def _prepare_and_publish(self, task, submission, record):
        repo = self._repo(task)
        source = _source_state(repo)
        destination = self._destination(repo, task.task_id, record.review_revision)
        if Path(record.worktree_path) != destination:
            raise DeliveryConflict("delivery output directory differs from the recorded attempt")
        policy = task.path_policy or PathPolicy()
        inspected = inspect_patch(repo, task.base_sha, task.result["patch"].encode("utf-8"),
                                  allowed_paths=tuple(policy.allowed_paths), protected_paths=tuple(policy.protected_paths), write_objects=True)
        if record.commit_sha is None:
            identity = _identity(repo)
            identity.update(GIT_AUTHOR_DATE=record.created_at.isoformat(), GIT_COMMITTER_DATE=record.created_at.isoformat())
            message = f"修复：{task.goal.splitlines()[0][:100]}\n\nTask: {task.task_id}\nReview: {record.review_id}\nPatch-SHA256: {record.evidence.patch_sha256}\n"
            record.tree_sha = inspected.tree_sha
            record.commit_sha = git(repo, "commit-tree", inspected.tree_sha, "-p", task.base_sha,
                                    input=message.encode("utf-8"), extra_env=identity).stdout.decode().strip()
            with self.store._connection() as connection:
                self._save(connection, record)
        if record.tree_sha != inspected.tree_sha or ref_exists(repo, f"refs/heads/{record.branch}"):
            raise DeliveryConflict("delivery tree or branch changed")
        if not os.path.lexists(destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "--detach", "--no-checkout", str(destination), record.commit_sha)
        # A crash immediately after worktree add leaves only its .git link and
        # no index. Filling that owned, empty worktree cannot overwrite a file.
        if (_owned_worktree(repo, destination) and oid(destination, "HEAD^{commit}") == record.commit_sha
                and git(destination, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
                and {entry.name for entry in destination.iterdir()} == {".git"}):
            index = Path(os.fsdecode(git(destination, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.removesuffix(b"\n")))
            if index.exists():
                raise DeliveryConflict("empty delivery worktree has an existing index; retained for inspection")
            git(destination, "read-tree", "--reset", "-u", record.commit_sha)
        if (not _owned_worktree(repo, destination) or oid(destination, "HEAD^{commit}") != record.commit_sha
                or git(destination, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() not in ("", f"refs/heads/{record.branch}")
                or git(destination, "status", "--porcelain=v1", "--untracked-files=all").stdout
                or git(destination, "write-tree").stdout.decode().strip() != record.tree_sha):
            raise DeliveryConflict("delivery worktree is incomplete or changed; retained for inspection")
        if _source_state(repo) != source:
            raise DeliveryConflict("source HEAD, index or working-tree state changed during delivery")
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_approval(connection, task.task_id, submission)
            if _source_state(repo) != source:
                raise DeliveryConflict("source repository changed before publishing the branch")
            git(repo, "update-ref", "--no-deref", f"refs/heads/{record.branch}", record.commit_sha, "0" * len(record.commit_sha))
            git(destination, "symbolic-ref", "HEAD", f"refs/heads/{record.branch}")
            record.status = "completed"
            record.error = ""
            self._save(connection, record)
