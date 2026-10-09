"""A disposable checkout must not run host templates, hooks or smudge filters."""

import subprocess

import pytest

from patchloop.repair.workspace import WorktreeWorkspace


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_checkout_ignores_host_hooks_templates_filters_and_git_environment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--template=")
    (repo / ".gitattributes").write_text("*.txt filter=probe\n")
    (repo / "value.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    sha = git(repo, "rev-parse", "HEAD")
    marker = tmp_path / "executed"
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    config = tmp_path / "global.gitconfig"
    config.write_text(f"[init]\n\ttemplateDir = {template}\n[core]\n\thooksPath = {template / 'hooks'}\n"
                      f"[filter \"probe\"]\n\tsmudge = touch '{marker}'; cat\n\trequired = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(template / "hooks"))
    with WorktreeWorkspace(repo, sha) as workspace:
        assert (workspace.path / "value.txt").read_text() == "original\n"
        assert not (workspace.path / ".git/hooks/post-checkout").exists()
        assert not marker.exists()
    assert not marker.exists()


def test_failed_checkout_cleans_disposable_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "base")
    workspace = WorktreeWorkspace(repo, "0" * 40)
    with pytest.raises(subprocess.CalledProcessError):
        workspace.__enter__()
    assert workspace.path is not None and not workspace.path.exists()
