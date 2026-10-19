import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def git(root, *arguments, input_data=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *arguments],
        env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=input_data,
    ).stdout


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.filemode", "true")
    (root / "module.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root, git(root, "rev-parse", "HEAD").decode().strip()


def test_mode_only_builder_commit_normalizes_to_exact_clean_base(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    (root / "module.py").chmod(0o755)
    git(root, "commit", "-qam", "builder permissions")
    before = git(root, "rev-parse", "HEAD").decode().strip()
    base_tree = git(root, "rev-parse", base + "^{tree}").decode().strip()
    report = normalize_baseline(root, base)
    assert report == {
        "status": "passed", "base_commit": base, "before_head": before,
        "after_head": base, "base_tree": base_tree, "after_tree": base_tree,
        "mode_changes": 1,
    }
    assert git(root, "rev-parse", "HEAD").decode().strip() == base
    assert git(root, "status", "--porcelain", "--untracked-files=no") == b""
    assert (root / "module.py").stat().st_mode & 0o111 == 0


def test_image_layer_stat_changes_do_not_count_as_source_edits(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    source = root / "module.py"
    source.chmod(0o755)
    git(root, "commit", "-qam", "builder permissions")
    metadata = source.stat()
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000_000))
    result = normalize_baseline(root, base)
    assert result["status"] == "passed" and result["mode_changes"] == 1
    assert source.read_text() == "value = 1\n"


def snapshot(root):
    entries = {}
    for parent, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            entries[str(path.relative_to(root))] = (mode, content)
    return entries


def test_content_change_reverted_before_head_is_rejected_without_reset(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    (root / "module.py").write_text("PRIVATE SOURCE\n")
    git(root, "commit", "-qam", "content change")
    (root / "module.py").write_text("value = 1\n")
    git(root, "commit", "-qam", "restore content")
    before = snapshot(root)
    report = normalize_baseline(root, base)
    assert report["status"] == "error"
    assert "PRIVATE SOURCE" not in str(report)
    assert snapshot(root) == before


@pytest.mark.parametrize("dirty", ["worktree", "index", "mode", "missing", "symlink", "assume", "skip", "stat"])
def test_dirty_or_hidden_tracked_state_is_rejected_without_mutation(repository, dirty):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    git(root, "commit", "--allow-empty", "-qm", "builder")
    source = root / "module.py"
    original_stat = source.stat()
    if dirty in {"worktree", "index", "assume", "skip", "stat"}:
        source.write_text("value = 2\n")
    if dirty == "index":
        git(root, "add", "module.py")
        source.write_text("value = 1\n")
    if dirty == "mode":
        git(root, "config", "core.filemode", "false")
        source.chmod(0o755)
    if dirty == "missing":
        source.unlink()
    if dirty == "symlink":
        source.unlink()
        source.symlink_to("elsewhere")
    if dirty in {"assume", "skip"}:
        git(root, "update-index", "--assume-unchanged" if dirty == "assume" else "--skip-worktree", "module.py")
    if dirty == "stat":
        git(root, "config", "core.trustctime", "false")
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


@pytest.mark.parametrize("payload_kind", ["valid", "dirty", "invalid_json", "missing", "list", "wrong_type"])
def test_standalone_python_c_emits_only_the_json_report(repository, payload_kind, tmp_path):
    import patchloop.ab_baseline as baseline

    root, base = repository
    git(root, "commit", "--allow-empty", "-qm", "builder")
    if payload_kind == "dirty":
        (root / "module.py").write_text("PRIVATE SOURCE\n")
    payload = json.dumps({"root": str(root), "base_commit": base})
    if payload_kind == "invalid_json":
        payload = "PRIVATE SOURCE"
    elif payload_kind == "missing":
        payload = "{}"
    elif payload_kind == "list":
        payload = "[]"
    elif payload_kind == "wrong_type":
        payload = json.dumps({"root": [], "base_commit": base})
    source = Path(baseline.__file__).read_text()
    ast.parse(source, feature_version=(3, 8))
    before = snapshot(root)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", source, payload],
        cwd=tmp_path, capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("PATCHLOOP_BASELINE_REPORT=")
    report = json.loads(lines[0].split("=", 1)[1])
    assert report["status"] == ("passed" if payload_kind == "valid" else "error")
    assert result.stderr == "" and "PRIVATE SOURCE" not in result.stdout
    if payload_kind == "valid":
        assert result.returncode == 0 and report["after_head"] == base
    else:
        assert snapshot(root) == before


@pytest.mark.parametrize("layout", ["root_link", "git_link", "gitfile", "redirect", "index_link", "commondir"])
def test_nonstandalone_or_symlink_layouts_are_rejected(repository, tmp_path, layout):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    git(root, "commit", "--allow-empty", "-qm", "builder")
    target = root
    if layout == "root_link":
        target = tmp_path / "alias"
        target.symlink_to(root, target_is_directory=True)
    elif layout in {"git_link", "gitfile"}:
        moved = tmp_path / "metadata"
        (root / ".git").rename(moved)
        if layout == "git_link":
            (root / ".git").symlink_to(moved, target_is_directory=True)
        else:
            (root / ".git").write_text("gitdir: " + str(moved) + "\n")
    elif layout == "redirect":
        other = tmp_path / "other"
        other.mkdir()
        (other / "module.py").write_text("value = 1\n")
        git(root, "config", "core.worktree", str(other))
    elif layout == "index_link":
        moved = tmp_path / "index"
        (root / ".git/index").rename(moved)
        (root / ".git/index").symlink_to(moved)
    elif layout == "commondir":
        (root / ".git/commondir").write_text(".\n")
    before = snapshot(tmp_path)
    assert normalize_baseline(target, base)["status"] == "error"
    assert snapshot(tmp_path) == before


def test_checkout_filter_is_rejected_before_it_can_run(repository, tmp_path):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    (root / ".gitattributes").write_text("module.py filter=unsafe\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "attributes")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "module.py").chmod(0o755)
    git(root, "commit", "-qam", "builder")
    marker = tmp_path / "executed"
    git(root, "config", "filter.unsafe.smudge", "touch " + str(marker) + "; cat")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert not marker.exists()
    assert snapshot(root) == before


@pytest.mark.parametrize("change", ["content", "add", "delete", "rename", "file_type", "symlink", "gitlink"])
def test_committed_content_path_and_type_changes_are_rejected(repository, change):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    if change in {"symlink", "gitlink"}:
        if change == "symlink":
            (root / "link").symlink_to("module.py")
            git(root, "add", "link")
        else:
            git(root, "update-index", "--add", "--cacheinfo", "160000," + base + ",vendor")
        git(root, "commit", "-qm", "special entry base")
        base = git(root, "rev-parse", "HEAD").decode().strip()
    if change == "content":
        (root / "module.py").write_text("different\n")
    elif change == "add":
        (root / "added.py").write_text("added\n")
    elif change == "delete":
        (root / "module.py").unlink()
    elif change == "rename":
        (root / "module.py").rename(root / "renamed.py")
    elif change == "file_type":
        (root / "module.py").unlink()
        (root / "module.py").symlink_to("value = 1\n")
    elif change == "symlink":
        (root / "link").unlink()
        (root / "link").symlink_to("different")
    elif change == "gitlink":
        git(root, "update-index", "--cacheinfo", "160000," + base + ",vendor")
    if change != "gitlink":
        git(root, "add", "-A")
    git(root, "commit", "-qm", "disallowed change")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


@pytest.mark.parametrize("invalid", ["short", "ref", "option", "missing", "tree", "tag", "unrelated"])
def test_invalid_or_nonancestor_baselines_are_rejected(repository, invalid):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    if invalid == "short":
        base = base[:12]
    elif invalid == "ref":
        base = "HEAD"
    elif invalid == "option":
        base = "--help"
    elif invalid == "missing":
        base = "f" * 40
    elif invalid == "tree":
        base = git(root, "rev-parse", "HEAD^{tree}").decode().strip()
    elif invalid == "tag":
        git(root, "tag", "-am", "tag", "tag")
        base = git(root, "rev-parse", "tag").decode().strip()
    elif invalid == "unrelated":
        git(root, "checkout", "--orphan", "other")
        git(root, "commit", "-qm", "other root")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


@pytest.mark.parametrize("history", ["base", "empty", "roundtrip", "merge"])
def test_clean_allowed_histories_preserve_untracked_cache(repository, history):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    if history == "empty":
        git(root, "commit", "--allow-empty", "-qm", "empty")
    elif history == "roundtrip":
        (root / "module.py").chmod(0o755)
        git(root, "commit", "-qam", "executable")
        (root / "module.py").chmod(0o644)
        git(root, "commit", "-qam", "non executable")
    elif history == "merge":
        git(root, "checkout", "-qb", "side")
        git(root, "commit", "--allow-empty", "-qm", "side")
        git(root, "checkout", "-qb", "mainline", base)
        git(root, "commit", "--allow-empty", "-qm", "mainline")
        git(root, "merge", "--no-ff", "-qm", "merge", "side")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/module.pyc").write_bytes(b"cache")
    before = git(root, "rev-parse", "HEAD").decode().strip()
    report = normalize_baseline(root, base.upper())
    assert report["status"] == "passed"
    assert report["before_head"] == before and report["after_head"] == base
    assert report["mode_changes"] == 0 and report["base_tree"] == report["after_tree"]
    assert (root / "__pycache__/module.pyc").read_bytes() == b"cache"
    assert git(root, "status", "--porcelain", "--untracked-files=no") == b""


def test_untracked_obstruction_is_not_deleted(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    git(root, "commit", "--allow-empty", "-qm", "empty")
    (root / "module.py").unlink()
    (root / "module.py").mkdir()
    (root / "module.py/cache").write_text("preserve")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


def test_nul_delimited_paths_and_unchanged_special_entries(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    special = root / "directory\nwith\ttabs" / "--odd name\n.py"
    special.parent.mkdir()
    special.write_text("source\n")
    (root / "link").symlink_to("module.py")
    git(root, "add", ".")
    git(root, "update-index", "--add", "--cacheinfo", "160000," + base + ",vendor")
    git(root, "commit", "-qm", "base with special entries")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "vendor").mkdir()
    special.chmod(0o755)
    git(root, "add", "--", str(special.relative_to(root)))
    git(root, "commit", "-qm", "mode")
    report = normalize_baseline(root, base)
    assert report["status"] == "passed" and report["mode_changes"] == 1, report
    assert os.readlink(root / "link") == "module.py"
    assert special.read_text() == "source\n"


def test_hooks_fsmonitor_diff_and_environment_cannot_execute_or_redirect(repository, tmp_path, monkeypatch):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    (root / "module.py").chmod(0o755)
    git(root, "commit", "-qam", "builder")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "executed"
    for name in ("reference-transaction", "post-checkout", "fsmonitor"):
        hook = hooks / name
        hook.write_text("#!/bin/sh\nprintf executed >> '" + str(marker) + "'\n")
        hook.chmod(0o755)
    git(root, "config", "core.hooksPath", str(hooks))
    git(root, "config", "core.fsmonitor", str(hooks / "fsmonitor"))
    git(root, "config", "diff.external", str(hooks / "fsmonitor"))
    other_index = tmp_path / "foreign-index"
    other_index.write_bytes(b"do not touch")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_index))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    report = normalize_baseline(root, base)
    assert report["status"] == "passed"
    assert not marker.exists() and other_index.read_bytes() == b"do not touch"


def test_merge_does_not_hide_disallowed_side_branch_commits(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    git(root, "checkout", "-qb", "side")
    (root / "module.py").write_text("disallowed\n")
    git(root, "commit", "-qam", "content")
    (root / "module.py").write_text("value = 1\n")
    git(root, "commit", "-qam", "reverted")
    git(root, "checkout", "-qb", "mainline", base)
    git(root, "merge", "--no-ff", "-qm", "merge", "side")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


def test_empty_tree_path_addition_is_not_mistaken_for_an_empty_commit(repository):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    empty = git(root, "mktree", input_data=b"").strip()
    tree = git(root, "mktree", input_data=git(root, "ls-tree", base) + b"040000 tree " + empty + b"\tempty\n").strip()
    commit = git(root, "commit-tree", tree.decode(), "-p", base, "-m", "empty directory").strip()
    git(root, "reset", "--hard", commit.decode())
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before


@pytest.mark.parametrize("attribute", ["eol=crlf", "ident", "working-tree-encoding=UTF-16LE"])
def test_checkout_transformations_cannot_rewrite_validated_source(repository, attribute):
    from patchloop.ab_baseline import normalize_baseline

    root, base = repository
    (root / "module.py").write_text("$Id$\n")
    git(root, "commit", "-qam", "base")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "module.py").chmod(0o755)
    git(root, "commit", "-qam", "builder")
    (root / ".git/info/attributes").write_text("module.py " + attribute + "\n")
    before = snapshot(root)
    assert normalize_baseline(root, base)["status"] == "error"
    assert snapshot(root) == before
