"""Standard-library environment checks, executed inside the disposable container."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def inside(path, root):
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)


def restrict_history(root, base_commit):
    """Retain the exact base commit and tree, but no other commits or reflogs."""
    root = Path(root)
    if root.is_symlink() or not (root / ".git").is_dir() or (root / ".git").is_symlink():
        raise ValueError("expected a standalone repository inside the container")
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ValueError("base_commit must be a full SHA-1")
    git(root, "cat-file", "-e", base_commit + "^{commit}")
    git(root, "checkout", "--detach", "--force", base_commit)
    git(root, "reset", "--hard", base_commit)
    tree = git(root, "rev-parse", base_commit + "^{tree}")
    # --no-local transfers only the requested shallow history, without hardlinks
    # or alternates back to the image's original object database.
    with tempfile.TemporaryDirectory(prefix="patchloop-base-") as directory:
        clone = Path(directory) / "repo"
        git(root, "clone", "--no-local", "--depth", "1", "--no-tags", "--single-branch",
            root.as_uri(), str(clone))
        git(clone, "remote", "remove", "origin")
        git(clone, "reflog", "expire", "--expire=now", "--all")
        if git(clone, "rev-parse", "HEAD") != base_commit:
            raise RuntimeError("shallow clone does not match the requested base")
        shutil.rmtree(root / ".git")
        shutil.move(str(clone / ".git"), str(root / ".git"))
    if git(root, "rev-parse", "HEAD^{tree}") != tree:
        raise RuntimeError("repository tree changed during history isolation")


def scan_files(roots, repository, modules):
    """Locate named project copies and archives; never classify a scan error as clean."""
    findings = []
    errors = []
    visited = 0
    names = set(modules)
    archive_suffixes = (".conda", ".whl", ".zip", ".tar", ".tar.gz", ".tar.bz2", ".tgz")
    material_names = {"test.patch", "gold.patch", "predictions.jsonl", "eval.sh"}

    def onerror(exc):
        errors.append(str(exc))

    for root in roots:
        if not Path(root).exists():
            continue
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            if inside(directory, repository):
                dirs[:] = []
                continue
            visited += 1
            for name in list(dirs):
                path = Path(directory) / name
                if name in names and not inside(path, repository):
                    findings.append({"kind": "external_project_directory", "path": str(path),
                                     "resolved_path": str(path.resolve())})
                    dirs.remove(name)
            for name in files:
                path = Path(directory) / name
                normalized = name.lower().replace("_", "-")
                if name in material_names:
                    findings.append({"kind": "evaluation_material", "path": str(path)})
                if name in {m + ".py" for m in names} and not inside(path, repository):
                    findings.append({"kind": "external_project_source", "path": str(path)})
                if name.endswith(archive_suffixes) and any(
                    normalized.startswith(m.lower().replace("_", "-") + "-") for m in names
                ):
                    findings.append({"kind": "project_archive", "path": str(path)})
    return {"findings": findings, "scan_errors": errors, "directories_scanned": visited}


def inspect_environment(config):
    root = Path(config["workdir"])
    report = scan_files(config["scan_roots"], root, config["modules"])
    findings = report["findings"]
    commit = git(root, "rev-parse", "HEAD")
    objects = git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype)").splitlines()
    report["git"] = {
        "head": commit, "tree": git(root, "rev-parse", "HEAD^{tree}"),
        "commit_objects": objects.count("commit"), "remotes": git(root, "remote"),
        "reflog": git(root, "reflog", "--all", "--format=%H").splitlines(),
        "alternates_present": (root / ".git/objects/info/alternates").exists(),
        "tracked_changes": git(root, "status", "--porcelain", "--untracked-files=no"),
    }
    if config.get("prepared"):
        if commit != config["base_commit"] or report["git"]["tracked_changes"]:
            findings.append({"kind": "base_mismatch", "path": str(root)})
        if (objects.count("commit") != 1 or report["git"]["remotes"] or
                report["git"]["reflog"] or report["git"]["alternates_present"]):
            findings.append({"kind": "extra_git_history", "path": str(root / ".git")})
        for path in config["hidden_paths"]:
            if not Path(path).is_dir() or list(Path(path).iterdir()):
                findings.append({"kind": "cache_not_hidden", "path": path})
    # Check untracked evaluation material in the working tree too.
    for path in git(root, "ls-files", "--others", "-z").split("\0"):
        if Path(path).name in {"test.patch", "gold.patch", "predictions.jsonl", "eval.sh"}:
            findings.append({"kind": "evaluation_material", "path": str(root / path)})
    imports = {}
    for name in [*config["modules"], "pytest"]:
        try:
            module = importlib.import_module(name)
            origin = str(Path(module.__file__).resolve()) if getattr(module, "__file__", None) else None
            imports[name] = {"origin": origin, "version": str(getattr(module, "__version__", ""))}
            if name in config["modules"] and (not origin or not inside(origin, root)):
                findings.append({"kind": "wrong_import_origin", "module": name, "path": origin})
        except Exception as exc:
            imports[name] = {"error": f"{type(exc).__name__}: {exc}"}
            findings.append({"kind": "import_error", "module": name})
    report["imports"] = imports
    report["python"] = {"executable": sys.executable, "version": sys.version.split()[0]}
    report["status"] = "passed" if not findings and not report["scan_errors"] else "failed"
    return report


if __name__ == "__main__":
    try:
        payload = json.loads(sys.argv[1])
        if payload.get("prepare"):
            restrict_history(payload["workdir"], payload["base_commit"])
        result = inspect_environment(payload)
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    print("PATCHLOOP_ENVIRONMENT_REPORT=" + json.dumps(result, sort_keys=True))
