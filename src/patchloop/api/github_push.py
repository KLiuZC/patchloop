"""Create one remote branch from an isolated bare copy; never overwrite a ref."""

import os
import signal
import subprocess
import tempfile
from pathlib import Path

from .delivery import git_env
from .github_publication_remote import GitHubPublicationError


class GitHubPusher:
    def _run(self, args, *, env, cwd, timeout=120):
        # Discard Git diagnostics: authentication failures can quote credentials.
        try:
            process = subprocess.Popen(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                "-c", "credential.helper=", "-c", "credential.interactive=false", "-c", "http.extraHeader=",
                "-c", "http.followRedirects=false", "-c", "http.sslVerify=true", "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                *args], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubPublicationError("Git preparation or push did not complete; query the remote branch before retrying",
                                         uncertain=True) from exc
        if code:
            raise GitHubPublicationError("Git preparation or push failed; query the remote branch before retrying",
                                         uncertain=True)

    def push(self, repo: Path, plan, token: str, before_push):
        env = git_env()
        env.pop("PATCHLOOP_GITHUB_TOKEN", None)
        env.pop("PATCHLOOP_PUSH_TOKEN", None)
        env["LC_ALL"] = "C"
        with tempfile.TemporaryDirectory(prefix="patchloop-push-") as directory:
            root = Path(directory)
            bare = root / "repo.git"
            # --local copies objects without executing the source's upload-pack.
            self._run(["-c", "protocol.file.allow=always", "clone", "--quiet", "--bare", "--local", "--no-hardlinks", "--template=", "--", str(repo), str(bare)],
                      cwd=root, env=env)
            (bare / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
            from .delivery import oid
            if oid(bare, plan.head_sha + "^{tree}") != plan.tree_sha:
                raise GitHubPublicationError("isolated push commit does not match the confirmed file tree")
            askpass = root / "askpass.sh"
            askpass.write_text("#!/bin/sh\ncase \"$1\" in\n"
                "  \"Username for 'https://github.com\"*) printf '%s\\n' x-access-token ;;\n"
                "  \"Password for 'https://x-access-token@github.com\"*) printf '%s\\n' \"$PATCHLOOP_PUSH_TOKEN\" ;;\n"
                "  *) exit 1 ;;\nesac\n")
            askpass.chmod(0o700)
            before_push()
            env.update(GIT_ASKPASS=str(askpass), PATCHLOOP_PUSH_TOKEN=token)
            ref = f"refs/heads/{plan.head_branch}"
            # Empty expected value is an atomic create-only lease, never a force overwrite.
            self._run(["push", "--porcelain", "--no-verify", "--no-follow-tags", f"--force-with-lease={ref}:",
                       f"https://github.com/{plan.repository.full_name}.git", f"{plan.head_sha}:{ref}"], cwd=bare, env=env)
