"""Prepare a shallow public checkout without running repository code."""

import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time


PREPARATION_TIMEOUT_SECONDS = 180
_CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
_PUBLIC_URL = re.compile(r"https://github\.com/[a-z0-9][a-z0-9-]{0,99}/[a-z0-9_.-]{1,100}\.git")


class CheckoutError(RuntimeError):
    pass


def _environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false",
               GIT_OPTIONAL_LOCKS="0", GIT_ATTR_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1",
               GIT_NO_LAZY_FETCH="1", GIT_LFS_SKIP_SMUDGE="1")
    return env


class RepositoryCheckout:
    def _run(self, path: Path, *args: str, deadline: float, network: bool = False) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CheckoutError("Repository preparation timed out")
        command = [
            "git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
            "-c", f"core.attributesFile={os.devnull}", "-c", "core.autocrlf=false",
            "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false",
            "-c", "credential.helper=", "-c", "credential.interactive=false",
            "-c", "http.extraHeader=", "-c", "http.followRedirects=false",
            "-c", "http.sslVerify=true", "-c", "protocol.allow=never",
            "-c", f"protocol.https.allow={'always' if network else 'never'}",
            "-c", "submodule.recurse=false", "-C", str(path), *args,
        ]
        # Keep Git's potentially large diagnostics out of RAM. No shell, hooks,
        # credential helper, user Git config or submodule process is involved.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                           env=_environment(), start_new_session=True)
            except OSError as exc:
                raise CheckoutError("Could not start Git; check that Git is installed") from exc
            try:
                process.wait(timeout=remaining)
            except BaseException as exc:
                # Git can spawn remote-https/index-pack. Stop the whole group,
                # not only the parent, before releasing the preparation lock.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise CheckoutError("Repository preparation timed out") from exc
                raise
            if process.returncode:
                stderr.seek(0)
                detail = stderr.read(2000).decode("utf-8", errors="replace").strip()
                raise CheckoutError(f"Git operation failed: {detail}" if detail else "Git operation failed")
            if time.monotonic() >= deadline:
                raise CheckoutError("Repository preparation timed out")
            stdout.seek(0)
            output = stdout.read(1024 * 1024 + 1)
            if len(output) > 1024 * 1024:
                raise CheckoutError("Git inspection output exceeded its limit")
            return output

    def _fetch(self, path: Path, url: str, sha: str, deadline: float) -> None:
        self._run(path, "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
                  "--no-write-fetch-head", url, sha, deadline=deadline, network=True)

    def prepare(self, path: Path, url: str, sha: str) -> None:
        if not _PUBLIC_URL.fullmatch(url) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CheckoutError("Repository URL or pinned commit is invalid")
        deadline = time.monotonic() + PREPARATION_TIMEOUT_SECONDS
        # Never reuse or overwrite an existing checkout, even when empty.
        path.mkdir(exist_ok=False)
        self._run(path, "init", "--quiet", "--template=", "--initial-branch=base",
                  "--object-format=sha1", deadline=deadline)
        (path / ".git" / "config").write_bytes(_CONFIG)
        self._fetch(path, url, sha, deadline)
        commit = self._run(path, "rev-parse", "--verify", f"{sha}^{{commit}}", deadline=deadline).strip()
        if commit != sha.encode("ascii"):
            raise CheckoutError("Downloaded commit does not match the pinned version")
        entries = self._run(path, "ls-tree", "-r", "-z", sha, deadline=deadline)
        if any(entry.startswith(b"160000 ") for entry in entries.split(b"\0")):
            raise CheckoutError("Repositories with submodules are not supported yet")
        self._run(path, "checkout", "--quiet", "--detach", sha, deadline=deadline)
        intact, reason = self.inspect(path, sha, deadline=deadline)
        if not intact:
            raise CheckoutError(reason)

    def inspect(self, path: Path, sha: str, *, deadline: float | None = None) -> tuple[bool, str]:
        deadline = deadline if deadline is not None else time.monotonic() + 15
        try:
            if path.is_symlink() or not path.is_dir() or path.resolve() != path:
                return False, "Prepared repository path is missing or changed"
            gitdir = path / ".git"
            if gitdir.is_symlink() or not gitdir.is_dir():
                return False, "Prepared Git directory is missing or changed"
            # Only inspect a repository with the config created here. This also
            # prevents a later local edit from enabling filters or config includes.
            for directory, dirs, files in os.walk(gitdir):
                if time.monotonic() >= deadline:
                    return False, "Repository inspection timed out"
                if any((Path(directory) / name).is_symlink() for name in (*dirs, *files)):
                    return False, "Prepared Git metadata contains a symbolic link"
            if ((gitdir / "config").read_bytes() != _CONFIG
                    or (gitdir / "config.worktree").exists()
                    or (gitdir / "objects" / "info" / "alternates").exists()):
                return False, "Prepared Git configuration changed"
            head = self._run(path, "rev-parse", "--verify", "HEAD^{commit}", deadline=deadline).strip()
            if head != sha.encode("ascii"):
                return False, "Prepared HEAD differs from the pinned commit"
            tracked = self._run(path, "ls-files", "-v", "-z", deadline=deadline)
            if any(entry and not entry.startswith(b"H ") for entry in tracked.split(b"\0")):
                # assume-unchanged and skip-worktree can hide edits from status.
                # This managed checkout never sets either; do not silently trust them.
                return False, "Prepared Git index flags changed"
            status = self._run(path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                               "--ignored", "--ignore-submodules=all", deadline=deadline)
            if status:
                return False, "Prepared working tree has local changes"
            return True, ""
        except (OSError, CheckoutError) as exc:
            return False, str(exc) or "Could not inspect prepared repository"
