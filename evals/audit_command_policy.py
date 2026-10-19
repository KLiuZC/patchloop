"""Classify saved tool requests offline; never replay commands or call a model."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import posixpath

from patchloop.command_policy import assess_command
from patchloop.command_runtime import command_policy_metadata, swebench_command_policy
from patchloop.environment import MODULES


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


def json_object(value, label):
    require(isinstance(value, dict), f"invalid {label}: expected an object")
    return value


def object_rows(value, label):
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value),
            f"invalid {label}: expected a list of objects")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def parse_json(value):
    return json.loads(value, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def read_input(root, relative, hashes, *, jsonl=False):
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), f"input escapes results directory: {relative}")
    raw = path.read_bytes()
    hashes[str(relative)] = sha256(raw)
    try:
        return [(number, json_object(parse_json(line), f"event at {relative}:{number}"))
                for number, line in enumerate(raw.decode().splitlines(), 1)
                if line.strip()] if jsonl else json_object(parse_json(raw), relative)
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


def validate_turns(events, result, trajectory_path):
    require(all(event.get("type") in ("turn", "budget_reminder", "finish_required", "command_policy")
                for event in events[1:-1]), f"unexpected trajectory event: {trajectory_path}")
    next_turn = 1
    for event in events[1:-1]:
        require(type(event.get("turn")) is int and event["turn"] == next_turn,
                f"invalid event turn: {trajectory_path}")
        if event["type"] == "turn":
            next_turn += 1
        else:
            require(event["turn"] <= events[0]["max_turns"], f"auxiliary turn exceeds budget: {trajectory_path}")
    turns = [event for event in events if event.get("type") == "turn"]
    require(all(type(source.get("turns")) is int and source["turns"] == len(turns)
                for source in (events[-1], result)), f"turn count mismatch: {trajectory_path}")


def matching_fields(fields, *sources):
    require(all(isinstance(source, dict) for source in sources), "invalid metadata object")
    for field in fields:
        values = [source[field] for source in sources if field in source]
        require(all(type(value) is type(values[0]) and value == values[0] for value in values),
                f"metadata mismatch: {field}")


def source_command_policy(source):
    if "command_policy" not in source:
        return {}
    policy = source["command_policy"]
    policy = {"mode": policy} if isinstance(policy, str) else json_object(policy, "command policy")
    require(policy.get("mode") in ("off", "audit", "enforce"), "invalid source command policy mode")
    return policy


def validate_run(plan, planned, config, summary, instances):
    run_id = planned["run_id"]
    require(summary.get("run_id") == run_id, f"summary run_id mismatch: {run_id}")
    selected = config.get("instance_ids")
    require(isinstance(selected, list) and all(isinstance(value, str) for value in selected) and
            len(selected) == len(instances) and set(selected) == set(instances),
            f"config task mismatch: {run_id}")
    if "instances" in summary:
        require(type(summary["instances"]) is int and summary["instances"] == len(instances),
                f"summary instance count mismatch: {run_id}")
    aliases = {"command_timeout_seconds": "timeout", "cpus": "container_cpus", "network": "container_network",
               "sdk_max_retries": "api_max_retries"}
    settings = {aliases.get(key, key): value for key, value in json_object(plan.get("settings", {}), "plan settings").items()}
    matching_fields(("provider", "model", "mode", "max_turns", "timeout", "budget_reminders", "explicit_finish",
                     "platform", "container_cpus", "container_network", "workers", "memory", "judge_workers",
                     "api_timeout_seconds", "api_max_retries"), config, summary, planned, settings)
    matching_fields(("dataset", "selected_instances_sha256"), config, plan)
    matching_fields(("mode", "policy_version", "profile_version", "parser_version"),
                    *(source_command_policy(source) for source in (config, summary, planned)))
    require(config.get("mode") == "agent", f"unsupported audit mode: {run_id}")
    for field in ("provider", "model"):
        require(isinstance(config.get(field), str) and bool(config[field]), f"invalid {field}: {run_id}")
    for field in ("max_turns", "timeout"):
        require(type(config.get(field)) is int and config[field] > 0, f"invalid {field}: {run_id}")


def validate_metadata(plan, instance, config, summary, environment, events, trajectory_path, *, run_sources=()):
    meta, terminal = events[0], events[-1]
    iid = instance["instance_id"]
    require(terminal.get("run_id") == iid, f"trajectory result.run_id mismatch: {trajectory_path}")
    json_object(environment.get("image"), "environment image")
    checks = json_object(environment.get("checks"), "environment checks")
    imports = json_object(checks.get("imports"), "import origins")
    require(all(isinstance(imported, dict) for imported in imports.values()), f"invalid import origins: {iid}")
    require(environment.get("status") == checks.get("status") == "passed", f"environment preflight did not pass: {iid}")
    modules = MODULES.get(instance.get("repo"), ())
    require(bool(modules) and set((*modules, "pytest")) <= imports.keys(), f"missing required import origins: {iid}")
    for module, imported in imports.items():
        origin = imported.get("origin")
        require("error" not in imported and isinstance(origin, str) and origin.startswith("/") and "\0" not in origin,
                f"invalid import origin: {iid}:{module}")
        if module in modules:
            require(posixpath.normpath(origin).startswith("/testbed/"), f"external target import: {iid}:{module}")
    require(environment["instance_id"] == iid and environment["base_commit"] == instance["base_commit"] and
            environment["image"]["id"] == instance["image_id"] and meta.get("image") == instance["image_id"],
            f"environment/trajectory task/image mismatch: {iid}")
    matching_fields(("instance_id", "base_commit", "repo", "task"), instance, environment, meta, terminal)
    matching_fields(("requested",), environment["image"], {"requested": instance["image"]} if "image" in instance else {})
    matching_fields(("platform",), config, environment["image"], meta)
    require(meta.get("workdir") == "/testbed" and meta.get("sandbox", "docker") == "docker",
            f"unexpected trajectory workspace: {iid}")
    for field in ("provider", "model", "max_turns", "timeout"):
        require(field in meta, f"missing trajectory {field}: {iid}")
    matching_fields(("provider", "model", "mode", "max_turns", "timeout", "budget_reminders", "explicit_finish",
                     "temperature", "max_tokens"), config, meta, summary)
    for source in (config, meta):
        for field in ("budget_reminders", "explicit_finish"):
            if field in source:
                require(type(source[field]) is bool, f"invalid {field}: {iid}")
    require(type(terminal.get("turns")) is int and terminal["turns"] <= config["max_turns"],
            f"invalid terminal turns: {iid}")
    policies = (plan.get("environment_policy", {}), config.get("environment_policy", {}), environment.get("policy", {}))
    matching_fields(("version", "hidden_paths", "scan_roots", "probe_sha256"), *policies)
    matching_fields(("hidden_paths",), meta, *policies)
    matching_fields(("mode", "policy_version", "profile_version", "parser_version"),
                    *(source_command_policy(source) for source in (config, meta, *run_sources)))


def build_audit(results_root, plan, development_cases):
    root = Path(results_root)
    json_object(plan, "plan")
    instances = {safe_id(row.get("instance_id")): row for row in object_rows(plan.get("instances"), "plan instances")}
    require(len(instances) == len(plan["instances"]) and bool(instances), "duplicate or empty instance list")
    run_ids = [safe_id(row.get("run_id")) for row in object_rows(plan.get("runs"), "plan runs")]
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
        json_object(config.get("config"), "run config")
        object_rows(summary.get("results"), "summary results")
        require({safe_id(row.get("instance_id")) for row in summary["results"]} == set(instances) and
                len(summary["results"]) == len(instances), f"summary task mismatch: {run_id}")
        require(config["config"]["model"] == summary["model"] and
                config["config"]["provider"] == summary["provider"], f"provider/model mismatch: {run_id}")
        validate_run(plan, planned, config["config"], summary, instances)
        for iid, instance in instances.items():
            trajectory_path = run_path / "traj" / f"{iid}.jsonl"
            environment = read_input(root, run_path / "environment" / f"{iid}.json", hashes)
            indexed_events = read_input(root, trajectory_path, hashes, jsonl=True)
            events = [event for _, event in indexed_events]
            require(events and events[0].get("type") == "meta" and events[-1].get("type") == "result",
                    f"incomplete trajectory: {trajectory_path}")
            result = next(row for row in summary["results"] if row["instance_id"] == iid)
            validate_metadata(plan, instance, config["config"], result, environment, events, trajectory_path,
                              run_sources=(summary, planned))
            validate_turns(events, result, trajectory_path)
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
                assistant = json_object(event.get("assistant"), "turn assistant")
                calls = assistant.get("tool_calls")
                calls = object_rows([] if calls is None else calls, "tool calls")
                returned = object_rows(event.get("tools", []), "tool results")
                require(all(isinstance(item.get("id"), str) and bool(item["id"]) for item in [*calls, *returned]),
                        f"invalid tool ID: {attempt_key}:{line_number}")
                require(len({tool["id"] for tool in returned}) == len(returned), f"duplicate tool results: {attempt_key}:{line_number}")
                require(len({call["id"] for call in calls}) == len(calls), f"duplicate tool calls: {attempt_key}:{line_number}")
                require({tool["id"] for tool in returned} <= {call["id"] for call in calls}, f"orphan tool result: {attempt_key}:{line_number}")
                outputs = {tool["id"]: tool for tool in returned}
                for call_index, call in enumerate(calls):
                    function = json_object(call.get("function"), "tool function")
                    require(isinstance(function.get("name"), str) and bool(function["name"]), "invalid tool name")
                    arguments = function.get("arguments")
                    identifier = f"{attempt_key}:{line_number}:{call_index}"
                    output = outputs.get(call["id"])
                    if output is not None:
                        require(output.get("name") == function["name"] and output.get("arguments") == arguments and
                                isinstance(output.get("output"), str), f"tool result does not match request: {identifier}")
                    if function["name"] != "bash":
                        non_bash.append({"id": identifier, "attempt_id": attempt_key, "run_id": run_id,
                                         "tool_call_id": call["id"], "tool": function["name"]})
                        continue
                    row = {
                        "id": identifier, "attempt_id": attempt_key, "run_id": run_id,
                        "instance_id": iid, "trajectory": str(trajectory_path), "line": line_number,
                        "turn": event["turn"], "tool_call_id": call["id"],
                        "arguments_sha256": sha256(json.dumps(arguments, ensure_ascii=False).encode()),
                        "recorded_result": output is not None,
                        "output_sha256": sha256(output["output"].encode()) if output is not None else None,
                        "command": None, "command_sha256": None, "classification": None,
                        "rule_ids": [], "reason": "invalid tool arguments; not classified",
                        "development_case_ids": [],
                    }
                    try:
                        args = json.loads(arguments)
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
        report = build_audit(args.results_root, parse_json(plan_bytes), parse_json(case_bytes)["cases"])
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
