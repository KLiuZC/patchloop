"""Classify saved tool requests offline; never replay commands or call a model."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from patchloop.command_policy import assess_command
from patchloop.command_runtime import command_policy_metadata, swebench_command_policy


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "evals/profiles/xarray3-isolation/plan.json"
DEFAULT_CASES = ROOT / "evals/profiles/command-policy/cases.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_id(value):
    require(isinstance(value, str) and value not in {"", ".", ".."} and
            not any(character in value for character in ("/", "\\", "\0")), "invalid run or instance ID")
    return value


def read_input(root, relative, hashes, *, jsonl=False):
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), f"input escapes results directory: {relative}")
    raw = path.read_bytes()
    hashes[str(relative)] = sha256(raw)
    try:
        return [(number, json.loads(line)) for number, line in enumerate(raw.decode().splitlines(), 1)
                if line.strip()] if jsonl else json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"invalid saved JSON: {relative}") from exc


def summarize(records, non_bash):
    counts = Counter({"allow": 0, "block": 0, "unknown": 0})
    counts.update(row["classification"] for row in records if row["classification"] is not None)
    classified = sum(counts.values())
    rules = Counter(rule for row in records for rule in row["rule_ids"])
    return {
        "bash_requests": len(records), "classified_requests": classified,
        "invalid_arguments": sum(row["classification"] is None for row in records),
        "non_bash_requests": len(non_bash), "classifications": dict(counts),
        "classified_scope_rate": (counts["allow"] + counts["block"]) / classified if classified else None,
        "unknown_rate": counts["unknown"] / classified if classified else None,
        "would_block_rate": counts["block"] / classified if classified else None,
        "rule_counts": dict(sorted(rules.items())),
        "missing_tool_results": sum(not row["recorded_result"] for row in records),
        "development_command_matches": sum(bool(row["development_case_ids"]) for row in records),
    }


def build_audit(results_root, plan, development_cases):
    root = Path(results_root)
    instances = {safe_id(row["instance_id"]): row for row in plan["instances"]}
    require(len(instances) == len(plan["instances"]) and bool(instances), "duplicate or empty instance list")
    run_ids = [safe_id(row["run_id"]) for row in plan["runs"]]
    require(len(run_ids) == len(set(run_ids)) and bool(run_ids), "duplicate or empty run list")
    matches = {}
    for case in development_cases:
        matches.setdefault(sha256(case["cmd"].encode()), []).append(case["id"])
    hashes = {}
    records = []
    non_bash = []
    attempts = []
    runs = []
    for planned in plan["runs"]:
        run_id = planned["run_id"]
        run_path = Path(run_id)
        summary = read_input(root, run_path / "summary.json", hashes)
        config = read_input(root, run_path / "run_config.json", hashes)
        require({row["instance_id"] for row in summary["results"]} == set(instances) and
                len(summary["results"]) == len(instances), f"summary task mismatch: {run_id}")
        require(config["config"]["model"] == summary["model"] and
                config["config"]["provider"] == summary["provider"], f"provider/model mismatch: {run_id}")
        for iid, instance in instances.items():
            trajectory_path = run_path / "traj" / f"{iid}.jsonl"
            environment = read_input(root, run_path / "environment" / f"{iid}.json", hashes)
            indexed_events = read_input(root, trajectory_path, hashes, jsonl=True)
            events = [event for _, event in indexed_events]
            require(events and events[0].get("type") == "meta" and events[-1].get("type") == "result",
                    f"incomplete trajectory: {trajectory_path}")
            require(environment["instance_id"] == iid and environment["base_commit"] == instance["base_commit"] and
                    environment["image"]["id"] == instance["image_id"], f"environment task/image mismatch: {iid}")
            require(isinstance(environment.get("checks", {}).get("imports"), dict), f"missing import origins: {iid}")
            policy = swebench_command_policy("enforce", instance, environment, events[0]["workdir"])
            context = asdict(policy.context)
            attempt_key = f"{run_id}/{iid}"
            attempts.append({
                "attempt_id": attempt_key, "run_id": run_id, "instance_id": iid,
                "trajectory": str(trajectory_path), "context": context,
                "context_sha256": policy.context_sha256(),
                "source_environment_policy": environment.get("policy", {}).get("version", "not_recorded"),
                "source_command_policy": config["config"].get("command_policy", "not_recorded"),
            })
            for line_number, event in indexed_events:
                if event.get("type") != "turn":
                    continue
                calls = event["assistant"].get("tool_calls") or []
                returned = event.get("tools", [])
                require(len({tool["id"] for tool in returned}) == len(returned), f"duplicate tool results: {attempt_key}:{line_number}")
                require(len({call["id"] for call in calls}) == len(calls), f"duplicate tool calls: {attempt_key}:{line_number}")
                require({tool["id"] for tool in returned} <= {call["id"] for call in calls}, f"orphan tool result: {attempt_key}:{line_number}")
                outputs = {tool["id"]: tool for tool in returned}
                for call_index, call in enumerate(calls):
                    function = call["function"]
                    identifier = f"{attempt_key}:{line_number}:{call_index}"
                    if function["name"] != "bash":
                        non_bash.append({"id": identifier, "attempt_id": attempt_key, "run_id": run_id,
                                         "tool_call_id": call["id"], "tool": function["name"]})
                        continue
                    output = outputs.get(call["id"])
                    if output is not None:
                        require(output["name"] == function["name"] and output["arguments"] == function["arguments"],
                                f"tool result does not match request: {identifier}")
                    row = {
                        "id": identifier, "attempt_id": attempt_key, "run_id": run_id,
                        "instance_id": iid, "trajectory": str(trajectory_path), "line": line_number,
                        "turn": event["turn"], "tool_call_id": call["id"],
                        "arguments_sha256": sha256(json.dumps(function["arguments"], ensure_ascii=False).encode()),
                        "recorded_result": output is not None,
                        "output_sha256": sha256(output["output"].encode()) if output is not None else None,
                        "command": None, "command_sha256": None, "classification": None,
                        "rule_ids": [], "reason": "invalid tool arguments; not classified",
                        "development_case_ids": [],
                    }
                    try:
                        args = json.loads(function["arguments"])
                        command = args.get("cmd", args.get("command"))
                        if not isinstance(command, str):
                            raise ValueError("command is not a string")
                    except (ValueError, TypeError, AttributeError):
                        records.append(row)
                        continue
                    decision = assess_command(command, policy.context)
                    row.update({"command": command, "command_sha256": sha256(command.encode()),
                                "classification": decision.classification, "rule_ids": list(decision.rule_ids),
                                "reason": decision.reason, "development_case_ids": matches.get(sha256(command.encode()), [])})
                    records.append(row)
        selected = [row for row in records if row["run_id"] == run_id]
        others = [row for row in non_bash if row["run_id"] == run_id]
        runs.append({"run_id": run_id, "group": planned["group"], "provider": summary["provider"],
                     "model": summary["model"], **summarize(selected, others)})
    groups = []
    for provider, group in dict.fromkeys((row["provider"], row["group"]) for row in runs):
        selected_runs = {row["run_id"] for row in runs if (row["provider"], row["group"]) == (provider, group)}
        groups.append({"provider": provider, "group": group, **summarize(
            [row for row in records if row["run_id"] in selected_runs],
            [row for row in non_bash if row["run_id"] in selected_runs],
        )})
    source_paths = ["evals/audit_command_policy.py", "src/patchloop/command_policy.py",
                    "src/patchloop/command_runtime.py", "src/patchloop/environment.py", "uv.lock"]
    return {
        "schema_version": 1, "analysis": "offline_command_classification",
        "replayed_commands": 0, "model_requests": 0,
        "counterfactual_policy": command_policy_metadata("enforce"),
        "source_sha256": {name: sha256((ROOT / name).read_bytes()) for name in source_paths},
        "input_sha256": hashes, "input_path_root": "results_root argument",
        "attempts": attempts, "totals": summarize(records, non_bash), "runs": runs, "groups": groups,
        "records": records, "non_bash_records": non_bash,
        "limitations": [
            "Old command sequences were generated without the current feedback; no counterfactual repair score is computed.",
            "Source environments remain their recorded versions; this is not a new v2 environment model evaluation.",
            "Some commands were used to design this policy; this corpus is not an independent holdout.",
            "Classification is not a ground-truth intent label; false positives require separate review.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=ROOT / "evals/results")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(not args.output.resolve().is_relative_to(args.results_root.resolve()), "output must be outside source results")
        require(not args.output.exists(), "output already exists; use a new file")
        plan_bytes = args.plan.read_bytes()
        case_bytes = args.cases.read_bytes()
        report = build_audit(args.results_root, json.loads(plan_bytes), json.loads(case_bytes)["cases"])
        report["plan_sha256"] = sha256(plan_bytes)
        report["development_cases_sha256"] = sha256(case_bytes)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Offline audit failed: {exc}\n")
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
