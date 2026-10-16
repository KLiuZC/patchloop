"""Validate the frozen three-task recheck plan; execute one run only with --run."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

PROFILE = Path(__file__).resolve().parent
ROOT = PROFILE.parents[2]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_inputs(plan, root=ROOT):
    from patchloop.environment import inspect_image, policy_metadata
    from patchloop.eval import load_instances, read_id_file

    for relative, expected in plan["input_sha256"].items():
        path = root / relative
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f"frozen input changed: {relative}; prepare a new plan")
    if policy_metadata() != plan["environment_policy"]:
        raise ValueError("environment policy changed; repeat preflight and prepare a new plan")
    runtime = {"python_version": platform.python_version(),
               "packages": {name: version(name) for name in plan["runtime"]["packages"]}}
    if runtime != plan["runtime"]:
        raise ValueError("Python or dependency versions changed; prepare a new plan")
    ids = read_id_file(root / plan["subset_file"])
    if ids != [i["instance_id"] for i in plan["instances"]]:
        raise ValueError("subset order or instance IDs changed")
    # Set before the lazy datasets import. No model client or dotenv loading here.
    os.environ["HF_HUB_OFFLINE"] = "1"
    instances = load_instances(ids, plan["dataset"])
    serialized = json.dumps(instances, sort_keys=True, ensure_ascii=False).encode()
    if hashlib.sha256(serialized).hexdigest() != plan["selected_instances_sha256"]:
        raise ValueError("cached dataset rows changed; do not mix datasets within the campaign")
    for expected, instance in zip(plan["instances"], instances):
        if instance["base_commit"] != expected["base_commit"] or instance["repo"] != expected["repo"]:
            raise ValueError(f"task metadata changed: {expected['instance_id']}")
        actual = inspect_image(expected["image"])
        if actual["id"] != expected["image_id"] or actual["platform"] != plan["settings"]["platform"]:
            raise ValueError(f"image changed: {expected['instance_id']}; repeat its preflight")
    if not shutil.which("swebench"):
        raise ValueError("swebench CLI is not available on PATH")
    if not Path(plan["settings"]["harness_dir"]).expanduser().is_dir():
        raise ValueError("the planned harness working directory does not exist")


def command_for(plan, run):
    settings = plan["settings"]
    args = ["agent", "eval", "--run-id", run["run_id"], "--instances-file", plan["subset_file"],
            "--dataset", plan["dataset"], "--provider", run["provider"], "--model", run["model"],
            "--mode", "agent", "--max-turns", str(settings["max_turns"]),
            "--timeout", str(settings["command_timeout_seconds"]), "--workers", str(settings["workers"]),
            "--memory", settings["memory"], "--judge-workers", str(settings["judge_workers"]), "--judge",
            "--harness-dir", str(Path(settings["harness_dir"]).expanduser())]
    if run["budget_reminders"]:
        args.append("--budget-reminders")
    if run["explicit_finish"]:
        args.append("--explicit-finish")
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="Run exactly one planned ID. Omit to check only.")
    args = parser.parse_args(argv)
    plan = json.loads((PROFILE / "plan.json").read_text())
    runs = {r["run_id"]: r for r in plan["runs"]}
    try:
        if args.run and args.run not in runs:
            raise ValueError("unknown run ID; use one of the IDs in plan.json")
        if args.run:
            output = ROOT / "evals/results" / args.run
            if output.exists():
                raise ValueError(f"run directory already exists: {args.run}; inspect it before resuming")
        check_inputs(plan)
        print("Frozen subset, source, environment policy, dataset and image IDs match.", flush=True)
        if not args.run:
            print("Check only; no model requests. Planned runs:")
            for run in plan["runs"]:
                print(f"  {run['run_id']} -> {run['legacy_run_id']}")
            return 0
        args_list = command_for(plan, runs[args.run])
        executable = Path(sys.executable).parent / "agent"
        if not executable.is_file():
            raise ValueError("run this script with uv run python so the project agent CLI is available")
        args_list[0] = str(executable)
        print(f"Starting one planned run: {args.run}", flush=True)
        environment = {**os.environ, "HF_HUB_OFFLINE": "1"}
        return subprocess.run(args_list, cwd=ROOT, env=environment).returncode
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
