"""Inventory explicit reads of the known xarray cache in saved trajectories.

This is an evidence index, not a complete shell interpreter or a clean-room proof.
Run from the repository root; existing trajectories and verdicts are never edited.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

KNOWN_CACHE = "/opt/miniconda3/pkgs/xarray-2025.4.0-py310h06a4308_0"
GROUPS = {
    "baseline": [f"v30-deepseek-r{i}" for i in (1, 2, 3)],
    "budget_reminders": ["v30-budget-r1-retry", "v30-budget-r2", "v30-budget-r3"],
    "explicit_finish": [f"v30-finish-r{i}" for i in (1, 2, 3)],
    "glm": ["v30-glm"],
}


def content_read(command, output):
    if KNOWN_CACHE not in command or not output.strip():
        return False
    # A file read must return source lines. Listings and failed probes remain
    # recorded as observations, but never count as confirmed content exposure.
    reader = re.search(r"\b(?:grep|sed|cat|awk|head|tail)\b", command)
    source = re.search(r"(?m)^(?:\d+[:-])?\s*(?:def |class |from |import |return |assert |assert_\w+\(|[\w.]+\s*=)", output)
    return bool(reader and source and ".py" in command)


def build_inventory(results_root):
    root = Path(results_root)
    groups = {}
    attempts = []
    for group, names in GROUPS.items():
        total = successes = exposed_successes = exposed_attempts = 0
        for name in names:
            run = root / name
            summary_path = run / "summary.json"
            summary = json.loads(summary_path.read_text())
            verdicts = {r["instance_id"]: r["resolved"] for r in summary["results"]}
            total += len(verdicts)
            successes += sum(v is True for v in verdicts.values())
            for iid, resolved in sorted(verdicts.items()):
                trajectory = run / "traj" / f"{iid}.jsonl"
                if not trajectory.is_file():
                    raise ValueError(f"missing trajectory: {trajectory}")
                observations = []
                for line_number, line in enumerate(trajectory.read_text().splitlines(), 1):
                    event = json.loads(line)
                    for tool in event.get("tools", []):
                        args = tool.get("arguments", {})
                        try:
                            args = json.loads(args) if isinstance(args, str) else args
                            command = args.get("cmd", args.get("command", ""))
                        except (ValueError, AttributeError):
                            continue
                        if not isinstance(command, str) or KNOWN_CACHE not in command:
                            continue
                        output = str(tool.get("output", ""))
                        observations.append({
                            "line": line_number, "turn": event.get("turn"), "tool_call_id": tool.get("id"),
                            "command": command, "content_returned": content_read(command, output),
                            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                            "output_excerpt": output[:400],
                        })
                if not observations:
                    continue
                exposed = any(o["content_returned"] for o in observations)
                exposed_attempts += exposed
                exposed_successes += exposed and resolved is True
                attempts.append({
                    "group": group, "run_id": name, "instance_id": iid, "resolved": resolved,
                    "content_exposure": exposed, "trajectory": f"evals/results/{name}/traj/{iid}.jsonl",
                    "trajectory_sha256": hashlib.sha256(trajectory.read_bytes()).hexdigest(),
                    "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                    "harness_run_id": summary["harness_run_id"], "observations": observations,
                })
        groups[group] = {"attempts": total, "resolved": successes, "exposed_attempts": exposed_attempts,
                         "exposed_successes": exposed_successes,
                         "original_rate": successes / total,
                         "sensitivity_rate": (successes - exposed_successes) / total}
    return {"schema_version": 1, "scope": "explicit source reads from the known xarray 2025.4.0 cache",
            "known_cache": KNOWN_CACHE, "groups": groups, "attempts": attempts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("evals/results"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = build_inventory(args.results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(inventory, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(inventory["groups"], ensure_ascii=False, indent=2))
