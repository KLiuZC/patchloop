import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from patchloop.repair import patch_policy
from patchloop.repair.patch_policy import PatchInspection, PatchPolicyError, inspect_patch


def git(repo, *args, input=None, check=True):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *args],
        cwd=repo, env=env, input=input, capture_output=True, check=check, timeout=15,
    )


def make_repo(root, *, object_format="sha1"):
    root.mkdir()
    git(root, "init", "-q", "--template=", f"--object-format={object_format}")
    git(root, "config", "user.name", "Patch Policy Test")
    git(root, "config", "user.email", "patch-policy@example.invalid")
    for name in ("base.txt", "old.txt", "remove.txt", "src/keep.txt", "tests/case.py"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"original {name}\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return git(root, "rev-parse", "HEAD").stdout.decode().strip()


@pytest.fixture
def repos(tmp_path):
    repo = tmp_path / "original"
    base = make_repo(repo)
    builder = tmp_path / "builder"
    shutil.copytree(repo, builder)
    return repo, base, builder


def candidate(builder, base, *options):
    git(builder, "add", "-A")
    patch = git(
        builder, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
        *options, base, "--",
    ).stdout
    tree = git(builder, "write-tree").stdout.decode().strip()
    return patch, tree


def snapshot(root):
    """Include directories, file contents, modes, and mtimes (including index)."""
    result = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        if path.is_symlink():
            content = os.readlink(path)
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = None
        result[str(path.relative_to(root))] = (stat.st_mode, stat.st_mtime_ns, content)
    return result


def dirty_repo(repo):
    (repo / "staged.txt").write_text("staged content\n")
    git(repo, "add", "staged.txt")
    (repo / "staged.txt").write_text("unstaged content\n")
    (repo / "base.txt").write_text("unrelated dirty edit\n")
    (repo / "scratch.txt").write_text("untracked\n")
    # Also collide with a patch addition: the real index/worktree are irrelevant.
    (repo / "new.txt").write_text("untracked collision\n")


@pytest.mark.parametrize("write_objects", [False, True])
def test_inspects_actual_add_delete_edit_without_changing_dirty_repository(repos, write_objects):
    repo, base, builder = repos
    (builder / "base.txt").write_text("candidate edit\n")
    (builder / "new.txt").write_text("candidate addition\n")
    (builder / "remove.txt").unlink()
    patch, tree = candidate(builder, base)
    dirty_repo(repo)
    before = snapshot(repo)

    result = inspect_patch(repo, base, patch, write_objects=write_objects)

    assert result == PatchInspection(tree_sha=tree, changed_paths=["base.txt", "new.txt", "remove.txt"])
    after = snapshot(repo)
    if not write_objects:
        assert after == before
        assert git(repo, "cat-file", "-e", tree, check=False).returncode != 0
    else:
        # Existing objects are never rewritten, and all other repository state
        # (including refs, original index, dirty files, and config) is identical.
        outside_objects = lambda state: {k: v for k, v in state.items() if k != ".git/objects" and not k.startswith(".git/objects/")}
        assert outside_objects(after) == outside_objects(before)
        for name, state in before.items():
            if name.startswith(".git/objects/") and state[2] is not None:
                assert after[name] == state
        assert git(repo, "show", f"{tree}:new.txt").stdout == b"candidate addition\n"
        assert git(repo, "fsck", "--no-reflogs", tree).returncode == 0
        commit = git(repo, "commit-tree", tree, "-p", base, "-m", "parent integration").stdout.strip()
        assert git(repo, "rev-parse", commit.decode() + "^{tree}").stdout.strip().decode() == tree


@pytest.mark.parametrize("policy", ["protected", "allowed"])
@pytest.mark.parametrize("operation", ["add", "delete", "edit", "mode", "binary"])
def test_all_change_types_enforce_path_policy(repos, policy, operation):
    repo, base, builder = repos
    name = "src/new.txt" if operation == "add" else "src/keep.txt"
    target = builder / name
    if operation == "delete":
        target.unlink()
    elif operation == "mode":
        target.chmod(0o755)
    elif operation == "binary":
        target.write_bytes(b"\0binary\xffdata\0")
    else:
        target.write_text("changed\n")
    patch, _ = candidate(builder, base)
    kwargs = {"protected_paths": ("src/*",)} if policy == "protected" else {"allowed_paths": ("tests/*",)}
    before = snapshot(repo)
    with pytest.raises(PatchPolicyError, match="path protected|outside allowed_paths"):
        inspect_patch(repo, base, patch, **kwargs)
    assert snapshot(repo) == before


@pytest.mark.parametrize("old,new", [
    ("old.txt", "new.txt"),
    ("old.txt", 'src/新 名 "quoted".txt'),
])
def test_rename_checks_both_source_and_destination(repos, old, new):
    repo, base, builder = repos
    (builder / old).rename(builder / new)
    patch, tree = candidate(builder, base, "--find-renames")
    assert b"rename from" in patch and b"rename to" in patch
    result = inspect_patch(repo, base, patch, allowed_paths=(old, new))
    assert result.tree_sha == tree
    assert set(result.changed_paths) == {old, new}
    for endpoint in (old, new):
        with pytest.raises(PatchPolicyError, match="path protected"):
            inspect_patch(repo, base, patch, protected_paths=(endpoint,))
        with pytest.raises(PatchPolicyError, match="outside allowed_paths"):
            inspect_patch(repo, base, patch, allowed_paths=(endpoint,))


@pytest.mark.parametrize("name", [
    "src/with space.txt", 'src/with"quote.txt', "src/中文.txt", "src/with\ttab.txt",
    "src/with\nnewline.txt", "src/back\\slash.txt", "src/-option.txt", "src/trailing .txt",
])
def test_git_quoted_paths_are_decoded_before_policy_matching(repos, name):
    repo, base, builder = repos
    (builder / name).write_text("new content\n")
    patch, tree = candidate(builder, base)
    assert inspect_patch(repo, base, patch, allowed_paths=(name,)) == PatchInspection(tree, [name])
    with pytest.raises(PatchPolicyError, match="path protected"):
        inspect_patch(repo, base, patch, protected_paths=(name,))


def test_non_utf8_filename_round_trips(repos):
    repo, base, builder = repos
    name = "src/raw-\udcff.txt"
    # Build this in Git's index: macOS filesystems cannot store arbitrary bytes.
    blob = git(builder, "hash-object", "-w", "--stdin", input=b"content\n").stdout.strip()
    git(builder, "update-index", "-z", "--index-info", input=b"100644 " + blob + b"\t" + os.fsencode(name) + b"\0")
    patch = git(builder, "diff", "--cached", "--binary", "--full-index", base).stdout
    assert inspect_patch(repo, base, patch).changed_paths == [name]
    with pytest.raises(PatchPolicyError, match="path protected"):
        inspect_patch(repo, base, patch, protected_paths=(name,))


def test_binary_and_regular_executable_modes_are_supported(repos):
    repo, base, builder = repos
    (builder / "binary.dat").write_bytes(bytes(range(256)) * 10)
    (builder / "base.txt").chmod(0o755)
    patch, tree = candidate(builder, base)
    assert b"GIT binary patch" in patch and b"new mode 100755" in patch
    before = snapshot(repo)
    assert inspect_patch(repo, base, patch) == PatchInspection(tree, ["base.txt", "binary.dat"])
    assert snapshot(repo) == before


@pytest.mark.parametrize("write_objects", [False, True])
def test_binary_delta_applies_and_cannot_bypass_policy(repos, write_objects):
    repo, base, builder = repos
    original = random.Random(0).randbytes(8192)
    (builder / "payload.bin").write_bytes(original)
    git(builder, "add", "payload.bin")
    git(builder, "commit", "-qm", "binary base")
    git(repo, "fetch", str(builder), "HEAD")
    base = git(builder, "rev-parse", "HEAD").stdout.decode().strip()
    content = original[:4000] + b"changed bytes\0" + original[4014:]
    (builder / "payload.bin").write_bytes(content)
    patch, tree = candidate(builder, base)
    assert b"GIT binary patch" in patch and b"delta " in patch
    before = snapshot(repo)
    with pytest.raises(PatchPolicyError, match="path protected"):
        inspect_patch(repo, base, patch, protected_paths=("*.bin",), write_objects=write_objects)
    assert snapshot(repo) == before
    assert inspect_patch(repo, base, patch, write_objects=write_objects).tree_sha == tree
    if write_objects:
        assert git(repo, "show", f"{tree}:payload.bin").stdout == content
    else:
        assert snapshot(repo) == before


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("write_objects", [False, True])
def test_reused_objects_never_refresh_original_loose_objects_or_packs(repos, packed, write_objects):
    repo, base, builder = repos
    # This object exists outside the base tree: merely pre-copying the base's
    # loose blobs while keeping alternates would still allow Git to freshen it.
    reused = b"object already stored outside base\n"
    git(repo, "hash-object", "-w", "--stdin", input=reused)
    if packed:
        git(repo, "repack", "-ad")
    (builder / "base.txt").chmod(0o755)
    (builder / "old.txt").rename(builder / "renamed.txt")
    (builder / "new.txt").write_bytes(reused)
    patch, tree = candidate(builder, base, "--find-renames")
    before = snapshot(repo)
    result = inspect_patch(repo, base, patch, write_objects=write_objects)
    assert result.tree_sha == tree
    after = snapshot(repo)
    if write_objects:
        for name, state in before.items():
            if state[2] is not None:
                assert after[name] == state
        # Publishing an identical candidate again does not rewrite any objects.
        assert inspect_patch(repo, base, patch, write_objects=True) == result
        assert snapshot(repo) == after
    else:
        assert after == before


def test_plain_unified_patch_is_checked_by_git_paths(repos):
    repo, base, _ = repos
    patch = (
        b"--- a/tests/case.py\n+++ b/tests/case.py\n@@ -1 +1 @@\n"
        b"-original tests/case.py\n+changed tests\n"
    )
    assert inspect_patch(repo, base, patch).changed_paths == ["tests/case.py"]
    with pytest.raises(PatchPolicyError, match="path protected"):
        inspect_patch(repo, base, patch, protected_paths=("tests/*",))


def test_misleading_diff_header_cannot_hide_protected_path(repos):
    repo, base, builder = repos
    (builder / "tests/case.py").write_text("changed\n")
    patch, _ = candidate(builder, base)
    misleading = patch.replace(
        b"diff --git a/tests/case.py b/tests/case.py",
        b"diff --git a/src/keep.txt b/src/keep.txt",
    )
    before = snapshot(repo)
    # Git may reject inconsistent headers itself; otherwise its real result
    # must still hit the policy, regardless of the diff --git header's spelling.
    with pytest.raises(PatchPolicyError):
        inspect_patch(repo, base, misleading, protected_paths=("tests/*",))
    assert snapshot(repo) == before


@pytest.mark.parametrize("allowed,protected,reject", [
    (("src/*",), (), False),
    (("*.txt",), (), False),  # fnmatchcase '*' includes slash.
    (("SRC/*",), (), True),
    (("/src/*",), (), True),
    (("src/*",), ("src/keep.txt",), True),
    ((), ("SRC/*",), False),
    ((), ("**/keep.txt",), True),
])
def test_patterns_use_case_sensitive_root_relative_fnmatch(repos, allowed, protected, reject):
    repo, base, builder = repos
    (builder / "src/keep.txt").write_text("change\n")
    patch, _ = candidate(builder, base)
    if reject:
        with pytest.raises(PatchPolicyError):
            inspect_patch(repo, base, patch, allowed_paths=allowed, protected_paths=protected)
    else:
        assert inspect_patch(repo, base, patch, allowed_paths=allowed, protected_paths=protected).changed_paths == ["src/keep.txt"]


def test_module_has_no_implicit_test_file_protection(repos):
    repo, base, builder = repos
    (builder / "tests/case.py").write_text("changed test\n")
    patch, _ = candidate(builder, base)
    assert inspect_patch(repo, base, patch).changed_paths == ["tests/case.py"]


def test_unchanged_symlink_gitlink_and_control_files_do_not_block_regular_edit(repos):
    repo, base, builder = repos
    (builder / "existing-link").symlink_to("old.txt")
    (builder / ".gitattributes").write_text("* -text\n")
    (builder / ".gitmodules").write_text("# existing control file\n")
    git(builder, "add", "-A")
    git(builder, "update-index", "--add", "--cacheinfo", f"160000,{base},existing-module")
    git(builder, "commit", "-qm", "base with special entries")
    git(repo, "fetch", str(builder), "HEAD")
    base = git(builder, "rev-parse", "HEAD").stdout.decode().strip()
    (builder / "base.txt").write_text("regular edit\n")
    git(builder, "add", "base.txt")
    patch = git(builder, "diff", "--cached", "--binary", base).stdout
    before = snapshot(repo)
    assert inspect_patch(repo, base.upper(), patch).changed_paths == ["base.txt"]
    assert snapshot(repo) == before


@pytest.mark.parametrize("name", [".gitattributes", "nested/.gitattributes", ".gitmodules", "nested/.gitmodules"])
@pytest.mark.parametrize("operation", ["add", "edit", "delete", "rename"])
def test_git_control_files_cannot_change(repos, name, operation):
    repo, base, builder = repos
    target = builder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# original\n")
    if operation != "add":
        git(builder, "add", name)
        git(builder, "commit", "-qm", "control file base")
        # Import only this fixture base so it exists in the inspected repo.
        git(repo, "fetch", str(builder), "HEAD")
        base = git(builder, "rev-parse", "HEAD").stdout.decode().strip()
        if operation == "edit":
            target.write_text("# changed\n")
        elif operation == "delete":
            target.unlink()
        else:
            target.rename(builder / "harmless.txt")
    patch, _ = candidate(builder, base, "--find-renames")
    with pytest.raises(PatchPolicyError, match="Git control path"):
        inspect_patch(repo, base, patch, allowed_paths=("*",))


@pytest.mark.parametrize("path", [".git/config", "dir/.git/config", ".GIT/config", "dir/../escape", "/absolute"])
def test_unsafe_and_dot_git_paths_are_rejected(repos, path):
    repo, base, _ = repos
    patch = (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+unsafe\n"
    ).encode()
    before = snapshot(repo)
    with pytest.raises(PatchPolicyError):
        inspect_patch(repo, base, patch)
    assert snapshot(repo) == before


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
@pytest.mark.parametrize("operation", ["add", "edit", "delete", "to_regular", "from_regular", "rename"])
def test_any_changed_symlink_or_gitlink_is_rejected(repos, kind, operation):
    repo, base, builder = repos
    name = "special"
    mode = "120000" if kind == "symlink" else "160000"
    object_id = git(builder, "hash-object", "-w", "--stdin", input=b"old.txt").stdout.strip().decode() if kind == "symlink" else base

    def stage_special(oid=object_id, path=name):
        git(builder, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")

    if operation not in {"add", "from_regular"}:
        stage_special()
        git(builder, "commit", "-qm", "special base")
        git(repo, "fetch", str(builder), "HEAD")
        base = git(builder, "rev-parse", "HEAD").stdout.decode().strip()
    elif operation == "from_regular":
        name = "base.txt"

    if operation in {"add", "from_regular"}:
        stage_special(path=name)
    elif operation == "delete":
        git(builder, "update-index", "--force-remove", name)
    elif operation == "rename":
        git(builder, "update-index", "--force-remove", name)
        stage_special(path="renamed-special")
    else:
        replacement = git(builder, "hash-object", "-w", "--stdin", input=b"new target\n").stdout.strip().decode()
        if operation == "to_regular":
            git(builder, "update-index", "--cacheinfo", f"100644,{replacement},{name}")
        else:
            stage_special(oid=replacement if kind == "symlink" else base)
    patch = git(builder, "diff", "--cached", "--binary", "--full-index", "--find-renames", base, "--").stdout
    assert patch
    before = snapshot(repo)
    with pytest.raises(PatchPolicyError, match="symlink, gitlink"):
        inspect_patch(repo, base, patch)
    assert snapshot(repo) == before


@pytest.mark.parametrize("patch", [b"", b" \n\t", b"not a patch", b"diff --git a/x b/x\n@@ broken\n"])
def test_empty_or_malformed_patch_is_rejected(repos, patch):
    repo, base, _ = repos
    before = snapshot(repo)
    with pytest.raises(PatchPolicyError):
        inspect_patch(repo, base, patch)
    assert snapshot(repo) == before


def test_no_effect_and_non_applicable_patches_are_rejected(repos):
    repo, base, builder = repos
    no_effect = (
        b"diff --git a/base.txt b/base.txt\n--- a/base.txt\n+++ b/base.txt\n"
        b"@@ -1 +1 @@\n-original base.txt\n+original base.txt\n"
    )
    with pytest.raises(PatchPolicyError, match="no changes"):
        inspect_patch(repo, base, no_effect)
    (builder / "base.txt").write_text("different\n")
    patch, _ = candidate(builder, base)
    broken = patch.replace(b"-original base.txt", b"-wrong original")
    with pytest.raises(PatchPolicyError, match="git apply failed"):
        inspect_patch(repo, base, broken)


@pytest.mark.parametrize("base", ["HEAD", "abc123", "0" * 39, "0" * 41, "0" * 63, "0" * 65, "g" * 40, "--help", "0" * 40 + "\n"])
def test_base_must_be_full_hex_commit_id(repos, base):
    repo, _, _ = repos
    with pytest.raises(PatchPolicyError, match="40- or 64-digit"):
        inspect_patch(repo, base, b"some patch")


def test_missing_object_and_noncommit_base_are_rejected(repos):
    repo, base, builder = repos
    (builder / "base.txt").write_text("different\n")
    patch, _ = candidate(builder, base)
    with pytest.raises(PatchPolicyError, match="git cat-file failed"):
        inspect_patch(repo, "0" * 40, patch)
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    with pytest.raises(PatchPolicyError, match="must identify a commit"):
        inspect_patch(repo, tree, patch)


@pytest.mark.parametrize("write_objects", [False, True])
def test_sha256_repository(tmp_path, write_objects):
    repo = tmp_path / "sha256"
    base = make_repo(repo, object_format="sha256")
    builder = tmp_path / "builder"
    shutil.copytree(repo, builder)
    (builder / "base.txt").write_text("sha256 candidate\n")
    patch, tree = candidate(builder, base)
    assert len(base) == len(tree) == 64
    assert inspect_patch(repo, base, patch, write_objects=write_objects).tree_sha == tree
    assert (git(repo, "cat-file", "-e", tree, check=False).returncode == 0) == write_objects


@pytest.mark.parametrize("repo_kind", ["linked_worktree", "bare", "shared_clone", "subdirectory"])
def test_repository_layouts_and_alternates(repos, tmp_path, repo_kind):
    repo, base, builder = repos
    (builder / "base.txt").write_text("candidate\n")
    patch, tree = candidate(builder, base)
    if repo_kind == "linked_worktree":
        target = tmp_path / "linked"
        git(repo, "worktree", "add", "--detach", str(target), base)
    elif repo_kind in {"bare", "shared_clone"}:
        target = tmp_path / repo_kind
        git(tmp_path, "clone", "--bare" if repo_kind == "bare" else "--shared", str(repo), str(target))
    else:
        target = repo / "src"
    before = snapshot(tmp_path)
    assert inspect_patch(target, base, patch) == PatchInspection(tree, ["base.txt"])
    assert snapshot(tmp_path) == before


def test_alternate_paths_with_colons_quotes_newlines_and_unicode(repos, tmp_path):
    repo, base, builder = repos
    target = tmp_path / 'repository : "中文"\nwith newline '
    repo.rename(target)
    (builder / "base.txt").write_text("candidate\n")
    patch, tree = candidate(builder, base)
    before = snapshot(target)
    assert inspect_patch(target, base, patch).tree_sha == tree
    assert snapshot(target) == before


def test_inherited_git_environment_and_config_cannot_redirect_or_execute(repos, tmp_path, monkeypatch):
    repo, base, builder = repos
    (builder / "base.txt").write_text("candidate\n")
    patch, tree = candidate(builder, base)
    marker = tmp_path / "EXECUTED"
    script = tmp_path / "trap.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-index-change", "pre-auto-gc", "reference-transaction"):
        (hooks / name).symlink_to(script)
    for key, value in (
        ("core.hooksPath", str(hooks)), ("core.fsmonitor", str(script)),
        ("diff.external", str(script)), ("diff.hostile.textconv", str(script)),
        ("filter.hostile.clean", str(script)), ("filter.hostile.smudge", str(script)),
        ("filter.hostile.required", "true"), ("core.attributesFile", str(tmp_path / "attributes")),
    ):
        git(repo, "config", key, value)
    (tmp_path / "attributes").write_text("* diff=hostile filter=hostile\n")
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("* diff=hostile filter=hostile\n")
    config = tmp_path / "hostile.config"
    config.write_text(f"[core]\n fsmonitor = {script}\n hooksPath = {hooks}\n[diff]\n external = {script}\n")
    # A template hook would execute on temporary-index updates if init loaded it.
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    (template / "hooks/post-index-change").symlink_to(script)
    env = {
        "GIT_DIR": str(builder / ".git"), "GIT_COMMON_DIR": str(builder / ".git"),
        "GIT_WORK_TREE": str(builder), "GIT_INDEX_FILE": str(builder / ".git/index"),
        "GIT_OBJECT_DIRECTORY": str(builder / ".git/objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/nonexistent",
        "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config),
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(script),
        "GIT_CONFIG_PARAMETERS": "'core.bare=false'", "GIT_EXTERNAL_DIFF": str(script),
        "GIT_DIFF_OPTS": "--invalid", "GIT_TEMPLATE_DIR": str(template),
        "GIT_EXEC_PATH": "/nonexistent", "GIT_TRACE": str(marker), "GIT_NAMESPACE": "hostile",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    before = snapshot(tmp_path)
    assert inspect_patch(repo, base, patch).tree_sha == tree
    assert not marker.exists()
    assert snapshot(tmp_path) == before


def test_timeout_becomes_policy_error_and_leaves_repository_unchanged(repos, monkeypatch):
    repo, base, builder = repos
    (builder / "base.txt").write_text("candidate\n")
    patch, _ = candidate(builder, base)
    before = snapshot(repo)
    actual_run = subprocess.run
    timeouts = []

    def run(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if "apply" in args[0]:
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(patch_policy.subprocess, "run", run)
    with pytest.raises(PatchPolicyError, match="git apply timed out"):
        inspect_patch(repo, base, patch)
    assert timeouts and all(timeout and timeout > 0 for timeout in timeouts)
    assert snapshot(repo) == before


@pytest.mark.parametrize("process_filter", [False, True])
def test_git_helper_disables_filters_even_when_status_reads_original_index(repos, tmp_path, process_filter):
    repo, _, _ = repos
    marker = tmp_path / "FILTER_EXECUTED"
    script = tmp_path / "hostile-filter.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    # Exercise an included config and a dotted filter name as well as local
    # config: driver names cannot be safely extracted by splitting on all dots.
    config = tmp_path / "filters.config"
    config.write_text(
        '[filter "hostile.driver"]\n'
        f" clean = {script}\n smudge = {script}\n required = true\n"
        + (f" process = {script}\n" if process_filter else "")
    )
    git(repo, "config", "include.path", str(config))
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("base.txt filter=hostile.driver\n")
    content = (repo / "base.txt").read_bytes()
    (repo / "base.txt").write_bytes(content.replace(b"original", b"modified"))
    before = snapshot(repo)
    status = patch_policy._git(repo, patch_policy._git_environment(), "status", "--porcelain=v1", "-z")
    assert b" M base.txt\0" in status
    assert not marker.exists()
    assert snapshot(repo) == before
