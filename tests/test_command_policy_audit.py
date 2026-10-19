import importlib.util
import json
from pathlib import Path

import pytest


def load_audit():
    path = Path(__file__).parents[1] / "evals/audit_command_policy.py"
    spec = importlib.util.spec_from_file_location("command_policy_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def corpus(tmp_path, calls):
    instance = {"instance_id": "example__repo-1", "repo": "pydata/xarray",
                "base_commit": "a" * 40, "image_id": "sha256:fake"}
    plan = {"instances": [instance], "runs": [{"run_id": "example-run", "group": "base"}]}
    root = tmp_path / "results"
    run = root / "example-run"
    write_json(run / "run_config.json", {"config": {"provider": "fake", "model": "fake"}})
    write_json(run / "summary.json", {"provider": "fake", "model": "fake", "results": [
        {"instance_id": instance["instance_id"], "resolved": False},
    ]})
    write_json(run / "environment" / f"{instance['instance_id']}.json", {
        "instance_id": instance["instance_id"], "base_commit": instance["base_commit"],
        "image": {"id": instance["image_id"]}, "status": "passed",
        "policy": {"version": "swebench-isolation-v1"},
        "checks": {"imports": {"pytest": {"origin": "/opt/env/lib/python3.10/site-packages/pytest/__init__.py"}}},
    })
    events = [
        {"type": "meta", "workdir": "/testbed"},
        {"type": "turn", "turn": 1, "assistant": {"tool_calls": calls}, "tools": []},
        {"type": "result", "turns": 1},
    ]
    path = run / "traj" / f"{instance['instance_id']}.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return root, plan, path


def operation(identifier, command, name="bash", key="cmd"):
    return {"id": identifier, "function": {"name": name, "arguments": json.dumps({key: command})}}


def test_offline_audit_counts_requests_without_executing_them(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess

    audit = load_audit()
    marker = tmp_path / "never-created"
    root, plan, trajectory = corpus(tmp_path, [
        operation("allowed", "cat xarray/core/variable.py"),
        operation("blocked", f"printf touched > {marker}; pip download xarray"),
        operation("unknown", "python -c 'print(1)'", key="command"),
        operation("finished", "done", name="finish", key="summary"),
    ])
    before = trajectory.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("offline analysis must not execute commands or use network")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = audit.build_audit(root, plan, [])
    assert result["totals"]["bash_requests"] == 3
    assert result["totals"]["non_bash_requests"] == 1
    assert result["totals"]["classifications"] == {"allow": 1, "block": 1, "unknown": 1}
    assert result["totals"]["missing_tool_results"] == 3
    assert result["replayed_commands"] == result["model_requests"] == 0
    assert trajectory.read_bytes() == before and not marker.exists()


def test_blank_lines_do_not_change_evidence_line_numbers(tmp_path):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    original = trajectory.read_text().splitlines()
    trajectory.write_text(original[0] + "\n\n" + "\n".join(original[1:]) + "\n")
    report = load_audit().build_audit(root, plan, [])
    assert report["records"][0]["line"] == 3


@pytest.mark.parametrize("arguments", ["bad JSON", "[]", '{"cmd":null,"command":"pip download xarray"}', '{"cmd":4}', '{}'])
def test_invalid_arguments_stay_in_denominator_but_are_not_classified(tmp_path, arguments):
    invalid = operation("invalid", "unused")
    invalid["function"]["arguments"] = arguments
    root, plan, _ = corpus(tmp_path, [invalid, operation("valid", "git diff")])
    report = load_audit().build_audit(root, plan, [])
    assert report["totals"]["bash_requests"] == 2
    assert report["totals"]["invalid_arguments"] == 1
    assert report["totals"]["classified_requests"] == 1
    assert report["totals"]["classifications"] == {"allow": 1, "block": 0, "unknown": 0}


@pytest.mark.parametrize("damage", ["missing", "incomplete", "broken_json", "mismatched_output", "duplicate_call"])
def test_missing_or_corrupted_trajectories_are_not_silently_excluded(tmp_path, damage):
    root, plan, trajectory = corpus(tmp_path, [operation("operation", "git diff")])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    if damage == "missing":
        trajectory.unlink()
    elif damage == "incomplete":
        trajectory.write_text("\n".join(json.dumps(event) for event in events[:-1]))
    elif damage == "broken_json":
        trajectory.write_text("not json\n")
    else:
        if damage == "mismatched_output":
            events[1]["tools"] = [{"id": "operation", "name": "bash", "arguments": '{"cmd":"different"}', "output": ""}]
        else:
            events[1]["assistant"]["tool_calls"] *= 2
        trajectory.write_text("\n".join(json.dumps(event) for event in events))
    with pytest.raises((ValueError, FileNotFoundError)):
        load_audit().build_audit(root, plan, [])


def test_duplicate_commands_keep_occurrences_and_development_overlap(tmp_path):
    root, plan, _ = corpus(tmp_path, [operation("first", "git diff"), operation("second", "git diff")])
    audit = load_audit()
    cases = [{"id": "sample", "cmd": "git diff"}]
    first = audit.build_audit(root, plan, cases)
    second = audit.build_audit(root, plan, cases)
    assert first == second
    assert len(first["records"]) == 2 and first["totals"]["development_command_matches"] == 2
    assert first["records"][0]["id"] != first["records"][1]["id"]


def test_cli_writes_only_new_external_report(tmp_path):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    plan_path, cases_path = tmp_path / "plan.json", tmp_path / "cases.json"
    write_json(plan_path, plan)
    write_json(cases_path, {"cases": []})
    destination = tmp_path / "audit.json"
    audit = load_audit()
    args = ["--results-root", str(root), "--plan", str(plan_path), "--cases", str(cases_path)]
    assert audit.main([*args, "--output", str(destination)]) == 0
    saved = destination.read_bytes()
    with pytest.raises(SystemExit) as error:
        audit.main([*args, "--output", str(destination)])
    assert error.value.code == 2 and destination.read_bytes() == saved
    with pytest.raises(SystemExit) as error:
        audit.main([*args, "--output", str(root / "new.json")])
    assert error.value.code == 2 and not (root / "new.json").exists()
    assert trajectory.exists()
