"""Normalize disposable task worktrees without accepting changes to their sources."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class _Rejected(Exception):
    pass


class _Git:
    def __init__(self, root):
        self.root = root
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        self.environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1",
        )
        self.command = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs"]
        for setting in (
            "core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.fileMode=true",
            "core.symlinks=true", "core.ignoreStat=false", "submodule.recurse=false",
            "core.attributesFile=" + os.devnull, "core.autocrlf=false", "core.eol=lf",
            "diff.external=", "protocol.allow=never", "gc.auto=0", "maintenance.auto=false",
        ):
            self.command.extend(["-c", setting])

    def run(self, *arguments, input_data=None):
        result = subprocess.run(
            self.command + list(arguments), cwd=str(self.root), env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, input=input_data,
        )
        if result.returncode:
            raise _Rejected("git validation failed")
        return result.stdout

    def oid(self, revision):
        value = self.run("rev-parse", "--verify", revision).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise _Rejected("expected a SHA-1 object identifier")
        return value

    def tree(self, revision):
        entries = {}
        directories = set()
        for record in self.run("ls-tree", "-r", "-t", "-z", revision).split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, kind, blob = metadata.split()
                if path in entries or path in directories:
                    raise _Rejected("duplicate tree paths are not allowed")
                if kind == b"tree" and mode == b"040000":
                    directories.add(path)
                else:
                    entries[path] = (mode, kind, blob)
        expected_directories = set()
        for path in entries:
            components = path.split(b"/")
            for length in range(1, len(components)):
                expected_directories.add(b"/".join(components[:length]))
        if directories != expected_directories:
            raise _Rejected("empty or unsupported tree entries are not allowed")
        return entries


def _validate_repository(repository):
    root = repository.root
    metadata = root / ".git"
    for name in (
        "commondir", "gitdir", "worktrees", "shallow", "info/grafts", "objects/info/alternates",
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock",
    ):
        if os.path.lexists(str(metadata / name)):
            raise _Rejected("repository is shared, incomplete, or has an operation in progress")
    for name in ("HEAD", "ORIG_HEAD", "index", "config", "config.worktree", "objects", "refs", "logs", "info"):
        if (metadata / name).is_symlink():
            raise _Rejected("symlinked git metadata is not allowed")
    for directory in (metadata / "refs", metadata / "logs"):
        for parent, directories, files in os.walk(str(directory)):
            if any((Path(parent) / name).is_symlink() for name in directories + files):
                raise _Rejected("symlinked git references are not allowed")
    if repository.run("rev-parse", "--is-bare-repository").strip() != b"false":
        raise _Rejected("bare repositories are not allowed")
    for argument, expected in (
        ("--show-toplevel", root), ("--absolute-git-dir", metadata), ("--git-common-dir", metadata),
    ):
        reported = Path(os.fsdecode(repository.run("rev-parse", argument).rstrip(b"\n")))
        if not reported.is_absolute():
            reported = root / reported
        if reported.resolve() != expected:
            raise _Rejected("git repository or worktree is redirected")
    configuration = repository.run("config", "--null", "--list")
    if any(record.split(b"\n", 1)[0].lower().startswith(b"filter.") for record in configuration.split(b"\0")):
        raise _Rejected("checkout filters are not allowed")


def _compare(before, after):
    if before.keys() != after.keys():
        raise _Rejected("committed paths changed")
    changes = 0
    for path, original in before.items():
        current = after[path]
        if original[1:] != current[1:]:
            raise _Rejected("committed object type or blob changed")
        if original[0] != current[0]:
            if {original[0], current[0]} != {b"100644", b"100755"}:
                raise _Rejected("committed file type changed")
            changes += 1
    return changes


def _blob_id(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest().encode("ascii")


def _validate_checkout(repository, entries):
    attributes = repository.run(
        "check-attr", "--all", "-z", "--stdin", input_data=b"".join(path + b"\0" for path in entries),
    ).split(b"\0")[:-1]
    for offset in range(0, len(attributes), 3):
        attribute, value = attributes[offset + 1:offset + 3]
        if value not in {b"unset", b"unspecified"} and (
            attribute in {b"ident", b"working-tree-encoding"} or (attribute == b"eol" and value != b"lf")
        ):
            raise _Rejected("checkout content transformations are not allowed")


def _clean(repository, entries):
    flags = repository.run("ls-files", "-v", "-z").split(b"\0")
    if any(record and not record.startswith(b"H ") for record in flags):
        raise _Rejected("hidden index flags or sparse entries are not allowed")
    index = {}
    for record in repository.run("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, blob, stage = metadata.split()
        if stage != b"0" or path in index:
            raise _Rejected("unmerged index entries are not allowed")
        index[path] = (mode, b"commit" if mode == b"160000" else b"blob", blob)
    if index != entries:
        raise _Rejected("index differs from HEAD")
    for path, (mode, kind, blob) in entries.items():
        components = path.split(b"/")
        if any(component in {b"", b".", b".."} or component.lower() == b".git" for component in components):
            raise _Rejected("unsafe tracked path")
        location = repository.root
        for component in components[:-1]:
            location = location / os.fsdecode(component)
            if not stat.S_ISDIR(location.lstat().st_mode):
                raise _Rejected("tracked parent is not a real directory")
        location = location / os.fsdecode(components[-1])
        if mode == b"160000" and kind == b"commit":
            if location.is_symlink() or (location.exists() and (not location.is_dir() or any(location.iterdir()))):
                raise _Rejected("populated submodules require separate validation")
            continue
        metadata = location.lstat()
        if mode == b"120000" and kind == b"blob":
            if not stat.S_ISLNK(metadata.st_mode) or _blob_id(os.fsencode(os.readlink(location))) != blob:
                raise _Rejected("tracked symlink differs from HEAD")
        elif mode in {b"100644", b"100755"} and kind == b"blob":
            if not stat.S_ISREG(metadata.st_mode):
                raise _Rejected("tracked file type differs from HEAD")
            if bool(metadata.st_mode & stat.S_IXUSR) != (mode == b"100755"):
                raise _Rejected("tracked executable bit differs from HEAD")
            descriptor = os.open(str(location), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise _Rejected("tracked file changed during validation")
                digest = hashlib.sha1(b"blob " + str(opened.st_size).encode("ascii") + b"\0")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().encode("ascii") != blob:
                raise _Rejected("tracked contents differ from HEAD")
        else:
            raise _Rejected("unsupported tracked file type")


def normalize_baseline(root, base_commit):
    """Validate then reset a disposable, exclusively owned standalone worktree.

    mode_changes counts paths with net executable-bit differences from base.
    Untracked files are preserved. Populated submodules, checkout filters, shared
    repositories and incomplete histories fail closed. Errors contain no source.
    """
    try:
        if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_commit):
            raise _Rejected("base_commit must be a full 40-hex commit identifier")
        if not isinstance(os.fspath(root), str) or not os.fspath(root):
            raise _Rejected("root must be a nonempty filesystem path")
        root = Path(root).absolute()
        if not root.is_dir() or any(location.is_symlink() for location in (root,) + tuple(root.parents)):
            raise _Rejected("root must be a non-symlink directory")
        root = root.resolve()
        if (root / ".git").is_symlink() or not (root / ".git").is_dir():
            raise _Rejected("a standalone non-symlink .git directory is required")
        repository = _Git(root)
        _validate_repository(repository)
        base = repository.oid(base_commit + "^{commit}")
        if base != base_commit.lower():
            raise _Rejected("base_commit must identify a commit, not a tag")
        before = repository.oid("HEAD^{commit}")
        repository.run("merge-base", "--is-ancestor", base, before)
        base_entries = repository.tree(base)
        before_entries = repository.tree(before)
        changes = _compare(base_entries, before_entries)
        trees = {base: base_entries}
        for line in repository.run("rev-list", "--parents", base + ".." + before).splitlines():
            revisions = line.decode("ascii").split()
            if len(revisions) < 2:
                raise _Rejected("history contains an unrelated root commit")
            for revision in revisions:
                if revision not in trees:
                    trees[revision] = repository.tree(revision)
                _compare(base_entries, trees[revision])
        base_tree = repository.oid(base + "^{tree}")
        _clean(repository, before_entries)
        _validate_checkout(repository, before_entries)
        if repository.oid("HEAD^{commit}") != before:
            raise _Rejected("HEAD changed during validation")
        if before != base:
            repository.run("reset", "--hard", base)
        after = repository.oid("HEAD^{commit}")
        after_tree = repository.oid("HEAD^{tree}")
        if after != base or after_tree != base_tree:
            raise _Rejected("normalization postconditions failed")
        _clean(repository, base_entries)
        _validate_checkout(repository, base_entries)
        return {
            "status": "passed", "base_commit": base, "before_head": before,
            "after_head": after, "base_tree": base_tree, "after_tree": after_tree,
            "mode_changes": changes,
        }
    except _Rejected as error:
        return {"status": "error", "error": str(error)}
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        return {"status": "error", "error": "baseline validation failed"}


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        payload = json.loads(sys.argv[1])
        if not isinstance(payload, dict) or set(payload) != {"root", "base_commit"}:
            raise ValueError()
        report = normalize_baseline(payload["root"], payload["base_commit"])
    except (ValueError, TypeError):
        report = {"status": "error", "error": "expected JSON with root and base_commit"}
    print("PATCHLOOP_BASELINE_REPORT=" + json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
