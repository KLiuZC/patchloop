import signal
import subprocess
import time

import pytest

from patchloop.api import repository_git as module
from patchloop.api.repository_git import CheckoutError, RepositoryCheckout


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True).stdout


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    (path / "calc.py").write_text("value = 1\n")
    (path / ".gitignore").write_text("ignored/\n")
    (path / ".gitattributes").write_text("*.py filter=custom\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path, git(path, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def checkout(source):
    class LocalCheckout(RepositoryCheckout):
        def _fetch(self, path, url, sha, deadline):
            # Exercise real Git, including shallow object transfer, without network.
            self._run(path, "-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1",
                      "--no-tags", "--no-recurse-submodules", source[0].as_uri(), sha, deadline=deadline)

    return LocalCheckout()


def test_real_checkout_uses_exact_commit_even_after_branch_moves(tmp_path, source, checkout):
    origin, base = source
    (origin / "calc.py").write_text("value = 2\n")
    git(origin, "commit", "-qam", "new head")
    target = tmp_path / "checkout"
    checkout.prepare(target, "https://github.com/example/repo.git", base)
    assert (target / "calc.py").read_text() == "value = 1\n"
    assert git(target, "rev-parse", "HEAD").decode().strip() == base
    assert git(target, "rev-parse", "--is-shallow-repository").strip() == b"true"
    assert subprocess.run(["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode == 1
    assert checkout.inspect(target, base) == (True, "")
    assert git(origin, "rev-parse", "HEAD").decode().strip() != base


@pytest.mark.parametrize("url", ["file:///tmp/repo", "https://evil.test/repo.git", "https://github.com/a/../repo.git", "ext::command"])
def test_checkout_rejects_non_public_urls_before_touching_disk(tmp_path, source, url):
    target = tmp_path / "checkout"
    with pytest.raises(CheckoutError):
        RepositoryCheckout().prepare(target, url, source[1])
    assert not target.exists()


def test_existing_directory_and_contents_are_never_overwritten(tmp_path, source, checkout):
    target = tmp_path / "checkout"
    target.mkdir()
    (target / "draft").write_text("keep")
    with pytest.raises(FileExistsError):
        checkout.prepare(target, "https://github.com/example/repo.git", source[1])
    assert (target / "draft").read_text() == "keep"
    assert not (target / ".git").exists()


@pytest.mark.parametrize("change", ["tracked", "untracked", "ignored", "head", "config", "git-symlink",
                                    "assume-unchanged", "skip-worktree"])
def test_later_changes_are_reported_without_resetting_them(tmp_path, source, checkout, change):
    target = tmp_path / "checkout"
    checkout.prepare(target, "https://github.com/example/repo.git", source[1])
    marker = tmp_path / "must-not-execute"
    if change == "tracked":
        (target / "calc.py").write_text("value = 99\n")
    elif change == "untracked":
        (target / "draft").write_text("keep")
    elif change == "ignored":
        (target / "ignored").mkdir()
        (target / "ignored" / "draft").write_text("keep")
    elif change == "head":
        (target / ".git" / "HEAD").write_text("f" * 40 + "\n")
    elif change == "config":
        with (target / ".git" / "config").open("a") as stream:
            stream.write(f'[core]\nfsmonitor = "touch {marker}"\n')
    elif change in ("assume-unchanged", "skip-worktree"):
        git(target, "update-index", "--" + change, "calc.py")
        (target / "calc.py").write_text("value = 99\n")
    else:
        (target / ".git" / "external").symlink_to(tmp_path)
    before = (target / "calc.py").read_bytes()
    intact, reason = checkout.inspect(target, source[1])
    assert intact is False and reason
    assert (target / "calc.py").read_bytes() == before
    assert not marker.exists()


def test_global_config_hooks_and_filters_do_not_run(tmp_path, source, checkout, monkeypatch):
    marker = tmp_path / "must-not-execute"
    templates = tmp_path / "template" / "hooks"
    templates.mkdir(parents=True)
    hook = templates / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f'[init]\ntemplateDir = {templates.parent}\n[filter "custom"]\nsmudge = touch {marker}\n'
                      f'[url "ext::touch {marker}"]\ninsteadOf = file://\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}")
    checkout.prepare(tmp_path / "checkout", "https://github.com/example/repo.git", source[1])
    assert not marker.exists()
    assert (tmp_path / "checkout" / "calc.py").read_text() == "value = 1\n"


def test_submodule_repository_is_rejected_before_checkout(tmp_path, source, checkout):
    origin, base = source
    git(origin, "update-index", "--add", "--cacheinfo", f"160000,{base},module")
    git(origin, "commit", "-qm", "add gitlink")
    sha = git(origin, "rev-parse", "HEAD").decode().strip()
    target = tmp_path / "checkout"
    with pytest.raises(CheckoutError, match="submodules"):
        checkout.prepare(target, "https://github.com/example/repo.git", sha)
    assert not (target / "module").exists()


def test_fetch_failure_keeps_partial_directory_and_reports_reason(tmp_path, source, checkout, monkeypatch):
    def fail(*args):
        raise CheckoutError("download failed")
    monkeypatch.setattr(checkout, "_fetch", fail)
    target = tmp_path / "checkout"
    with pytest.raises(CheckoutError, match="download failed"):
        checkout.prepare(target, "https://github.com/example/repo.git", source[1])
    assert (target / ".git").is_dir()
    assert checkout.inspect(target, source[1])[0] is False


def test_timeout_kills_git_process_group_and_waits_for_parent(tmp_path, monkeypatch):
    calls = []
    class Process:
        pid = 54321
        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("git", timeout)
            return -9
    def popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        assert "shell" not in kwargs
        return Process()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: calls.append(("kill", pid, sig)))
    with pytest.raises(CheckoutError, match="timed out"):
        RepositoryCheckout()._run(tmp_path, "fetch", deadline=time.monotonic() + 10)
    assert ("kill", 54321, signal.SIGKILL) in calls
    assert calls[-1] == ("wait", None)
