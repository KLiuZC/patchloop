"""Local preparation and explicit no-model validation of an A/B candidate."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .command_runtime import command_policy_metadata
from .environment import EnvironmentCheckError, inspect_image, policy_metadata, preflight_instance


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DRAFT = ROOT / "evals/profiles/command-policy-ab6/draft.json"


def digest(content):
    return hashlib.sha256(content).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_inputs(draft_path, cache=None):
    plan_bytes = Path(draft_path).read_bytes()
    plan = json.loads(plan_bytes)
    require(plan.get("status") == "draft_preflight_required", "expected a preparation draft")
    ids = [item["instance_id"] for item in plan["instances"]]
    require(ids and len(ids) == len(set(ids)) and all(re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", item) for item in ids), "invalid candidate IDs")
    require(plan["environment_policy"] == policy_metadata(), "environment policy changed; review the draft")
    for mode in ("off", "enforce"):
        require(plan["command_policy"][mode] == command_policy_metadata(mode), "command policy changed; review the draft")
    snapshot = plan["dataset"]["cache_snapshot"]
    require(re.fullmatch(r"[0-9a-f]{40}", snapshot) is not None, "invalid local dataset snapshot")
    if cache is None:
        base = Path.home() / ".cache/huggingface/datasets/SWE-bench___swe-bench_verified"
        matches = list(base.glob(f"*/0.0.0/{snapshot}/*-test.arrow"))
        require(len(matches) == 1, "expected one existing dataset cache; supply --cache")
        cache = matches[0]
    cache = Path(cache)
    require(digest(cache.read_bytes()) == plan["dataset"]["arrow_sha256"], "dataset cache changed")
    from datasets import Dataset
    dataset = Dataset.from_file(str(cache), in_memory=True)
    require(len(dataset) == plan["dataset"]["rows"], "dataset row count changed")
    selected = {row["instance_id"]: dict(row) for row in dataset if row["instance_id"] in ids}
    require(len(selected) == len(ids), "missing candidate data")
    rows = [selected[item] for item in ids]
    require(ids == plan["dataset"]["selected_records_hash_order"], "candidate order changed")
    require(digest(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()) == plan["dataset"]["selected_records_sha256"], "candidate records changed")
    require(digest(cache.read_bytes()) == plan["dataset"]["arrow_sha256"], "dataset changed while reading")
    for expected, row in zip(plan["instances"], rows, strict=True):
        require(all(expected[key] == row[key] for key in ("instance_id", "repo", "base_commit")), "candidate identity changed")
    return plan, rows, digest(plan_bytes)


def task_repository_state(path, expected_commit, instance_id):
    path = Path(path).expanduser().resolve()
    process = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    require(process.returncode == 0 and process.stdout.strip() == expected_commit, "task repository commit differs from draft")
    changed = subprocess.run(["git", "-C", str(path), "diff", "--quiet", "HEAD"], capture_output=True, timeout=30)
    require(changed.returncode == 0, "task repository has tracked modifications")
    folder = path / "tasks" / instance_id
    require(folder.is_dir() and not folder.is_symlink(), "task definition is missing or a symlink")
    files = {}
    for source in sorted(folder.rglob("*")):
        require(not source.is_symlink(), "task definition contains a symlink")
        if source.is_file():
            files[str(source.relative_to(path))] = digest(source.read_bytes())
    require(bool(files), "task definition directory is empty")
    return {"commit": expected_commit, "files_sha256": files}


def local_image(candidate, platform):
    names = [candidate["image"]]
    identifier = candidate.get("instance_id")
    if identifier is not None:
        require(re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", identifier), "invalid image task ID")
        arch = {"linux/amd64": "x86_64", "linux/arm64": "arm64"}.get(platform)
        require(arch is not None, "unsupported image platform")
        local = f"sweb.eval.{arch}.{identifier}:latest"
        names.extend((local, "swebench/" + local))
    found = []
    for name in dict.fromkeys(names):
        try:
            found.append(inspect_image(name))
        except EnvironmentCheckError:
            continue
    require(bool(found), "image is not available locally: " + candidate["image"])
    require(len({image["id"] for image in found}) == 1, "conflicting local image aliases; resolve them explicitly")
    info = {**found[0], "dataset_image": candidate["image"], "available_aliases": [image["requested"] for image in found]}
    require(info["platform"] == platform, "image platform differs from draft")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", info["id"]) is not None, "expected an immutable image ID")
    if candidate.get("image_id"):
        require(info["id"] == candidate["image_id"], "image differs from pinned candidate")
    return info


def private_output(path):
    output = Path(path).expanduser().resolve()
    require(not output.is_relative_to(ROOT) or output.is_relative_to(ROOT / "data"), "private validation artifacts must be outside source or under data/")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    return output


def write_private(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def harness_interpreter(value=None):
    if value is not None:
        interpreter = Path(value).expanduser().absolute()
    else:
        executable = shutil.which("swebench")
        require(executable is not None, "swebench is not installed")
        interpreter = Path(executable).resolve().with_name("python")
    require(interpreter.is_file() and os.access(interpreter, os.X_OK), "harness Python is unavailable")
    return interpreter


def execute_preflight(plan, row, candidate, draft_hash, *, output, task_repo, interpreter, verify):
    settings = plan["settings"]
    info = local_image(candidate, settings["platform"])
    task_state = task_repository_state(task_repo, plan["preflight_task_repository_commit"], row["instance_id"])
    destination = private_output(output)
    result = {"status": "error", "instance_id": row["instance_id"], "base_commit": row["base_commit"],
              "image_id": info["id"], "platform": settings["platform"], "draft_sha256": draft_hash,
              "record_sha256": digest(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()),
              "task_repository": task_state, "image_resolution": info, "model_requests": 0, "error": ""}
    try:
        public = {key: row[key] for key in ("instance_id", "repo", "base_commit")}
        environment = preflight_instance(public, info["id"], destination / "environment", verify=verify,
                                         memory=settings["memory"], platform=settings["platform"], timeout=settings["command_timeout_seconds"],
                                         normalize_build=True)
        require(environment["status"] == "passed", "isolated environment or visible health check failed")
        require(environment["image"]["id"] == info["id"], "environment image identity mismatch")
        before = environment["before"]["git"]
        normalization = environment["baseline_normalization"]
        require(normalization.get("status") == "passed" and normalization.get("before_head") == before["head"] and
                normalization.get("base_commit") == row["base_commit"] and normalization.get("after_head") == row["base_commit"] and
                normalization.get("base_tree") and normalization.get("after_tree") == normalization["base_tree"] and
                before["tracked_changes"] == "", "raw image is not the clean task baseline")
        result["baseline_normalization"] = normalization
        after = environment["after"]["git"]
        require(after.get("head") == row["base_commit"] and after.get("tree") == normalization["base_tree"] and
                after.get("tracked_changes") == "", "isolated solver baseline differs from normalized control baseline")
        request = {"instance": row, "image_id": info["id"], "platform": settings["platform"],
                   "memory": settings["memory"], "cpus": settings["cpus"], "timeout": 1800,
                   "baseline_normalization_sha256": environment["baseline_normalization_sha256"]}
        request_path = destination / "request.json"
        write_private(request_path, request)
        request_hash = digest(request_path.read_bytes())
        bridge = Path(__file__).with_name("ab_preflight_harness.py")
        result["bridge_sha256"] = digest(bridge.read_bytes())
        normalizer = Path(__file__).with_name("ab_baseline.py")
        result["baseline_normalization_sha256"] = digest(normalizer.read_bytes())
        require(environment.get("baseline_normalization_sha256") == result["baseline_normalization_sha256"], "normalization implementation changed")
        controls = destination / "controls"
        allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        environment_vars = {key: value for key, value in os.environ.items() if key in allowed}
        environment_vars.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1")
        with (destination / "harness.log").open("x", encoding="utf-8") as stream:
            process = subprocess.run([str(interpreter), str(bridge), "--input", str(request_path), "--output", str(controls)],
                                     cwd=destination, env=environment_vars, stdout=stream, stderr=subprocess.STDOUT)
        require(process.returncode == 0, "official control preflight failed; see private harness.log")
        require(digest(request_path.read_bytes()) == request_hash and digest(bridge.read_bytes()) == result["bridge_sha256"], "control inputs changed during validation")
        require(digest(normalizer.read_bytes()) == result["baseline_normalization_sha256"], "normalization implementation changed")
        control = json.loads((controls / "result.json").read_text())
        require(control.get("status") == "passed" and control.get("request_sha256") == request_hash, "unverified control result")
        for key in ("instance_id", "base_commit", "image_id", "platform"):
            require(control.get(key) == result[key], "control result identity mismatch")
        for phase in ("baseline", "gold"):
            proof = control[phase]["baseline_normalization"]
            require(proof.get("status") == "passed" and proof.get("after_head") == row["base_commit"] and
                    proof.get("before_head") == normalization["before_head"] and
                    proof.get("after_tree") == normalization["base_tree"], "control baseline differs from environment")
        require(task_repository_state(task_repo, plan["preflight_task_repository_commit"], row["instance_id"]) == task_state, "task definitions changed during validation")
        require(local_image(candidate, settings["platform"])["id"] == info["id"], "image tag changed during validation")
        result.update(status="passed", environment="passed", controls="passed", request_sha256=request_hash)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    except KeyboardInterrupt:
        result["error"] = "preflight interrupted"
        raise
    finally:
        result["artifact_sha256"] = {str(path.relative_to(destination)): digest(path.read_bytes())
                                     for path in sorted(destination.rglob("*")) if path.is_file()}
        write_private(destination / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--instance")
    parser.add_argument("--execute", action="store_true", help="Run environment and baseline/gold controls without a model.")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--task-repo", type=Path, default=Path.home() / "code/swebench-work/swe-bench-tasks")
    parser.add_argument("--harness-python", type=Path)
    parser.add_argument("--verify", default="python -m pytest --version")
    args = parser.parse_args(argv)
    try:
        require(not args.execute or (args.instance and args.artifacts), "--execute requires --instance and a new --artifacts directory")
        plan, rows, draft_hash = load_inputs(args.draft, args.cache)
        require(args.instance is None or args.instance in {row["instance_id"] for row in rows}, "instance is not a planned candidate")
        chosen = [(candidate, row) for candidate, row in zip(plan["instances"], rows, strict=True)
                  if args.instance is None or row["instance_id"] == args.instance]
        if not args.execute:
            checks = []
            for candidate, row in chosen:
                try:
                    info = local_image(candidate, plan["settings"]["platform"])
                    task_repository_state(args.task_repo, plan["preflight_task_repository_commit"], row["instance_id"])
                    checks.append({"instance_id": row["instance_id"], "status": "available_not_validated", "image_id": info["id"]})
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    checks.append({"instance_id": row["instance_id"], "status": "blocked", "error": str(exc)})
            print(json.dumps({"mode": "check_only", "model_requests": 0, "ready_for_model_run": False, "candidates": checks}, ensure_ascii=False, indent=2))
            return 2 if any(row["status"] == "blocked" for row in checks) else 0
        candidate, row = chosen[0]
        result = execute_preflight(plan, row, candidate, draft_hash, output=args.artifacts, task_repo=args.task_repo,
                                   interpreter=harness_interpreter(args.harness_python), verify=args.verify)
        print(f"preflight={result['status']} · instance={row['instance_id']} · artifacts={args.artifacts}")
        return 0 if result["status"] == "passed" else 2
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Preparation blocked: {exc}", file=sys.stderr)
        return 2
