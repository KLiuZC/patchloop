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


def write_events(path, events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def corpus(tmp_path, calls, turns=1):
    instance = {"instance_id": "example__repo-1", "repo": "pydata/xarray",
                "base_commit": "a" * 40, "image_id": "sha256:fake", "image": "example:latest"}
    settings = {"provider": "fake", "model": "fake", "budget_reminders": False, "explicit_finish": False}
    environment_policy = {"version": "swebench-isolation-v1", "hidden_paths": ["/root/.cache"]}
    plan = {"instances": [instance], "runs": [{"run_id": "example-run", "group": "base", **settings}],
            "settings": {"max_turns": 40, "command_timeout_seconds": 180, "mode": "agent"},
            "environment_policy": environment_policy}
    root = tmp_path / "results"
    run = root / "example-run"
    write_json(run / "run_config.json", {"config": {
        **settings, "instance_ids": [instance["instance_id"]], "max_turns": 40, "timeout": 180,
        "mode": "agent", "platform": "linux/amd64", "environment_policy": environment_policy,
    }})
    write_json(run / "summary.json", {"run_id": "example-run", **settings, "mode": "agent", "instances": 1, "results": [
        {"instance_id": instance["instance_id"], "resolved": False, "turns": turns, "mode": "agent"},
    ]})
    write_json(run / "environment" / f"{instance['instance_id']}.json", {
        "instance_id": instance["instance_id"], "base_commit": instance["base_commit"],
        "image": {"id": instance["image_id"], "requested": instance["image"], "platform": "linux/amd64"},
        "status": "passed", "policy": environment_policy,
        "checks": {"status": "passed", "imports": {
            "pytest": {"origin": "/opt/env/lib/python3.10/site-packages/pytest/__init__.py"},
            "xarray": {"origin": "/testbed/xarray/__init__.py"},
        }},
    })
    events = [
        {"type": "meta", "workdir": "/testbed", **settings, "image": instance["image_id"],
         "sandbox": "docker", "task": "Fix the issue in pydata/xarray.", "max_turns": 40,
         "timeout": 180, "hidden_paths": environment_policy["hidden_paths"]},
        *({"type": "turn", "turn": turn, "assistant": {"tool_calls": calls}, "tools": []}
          for turn in range(1, turns + 1)),
        {"type": "result", "run_id": instance["instance_id"], "turns": turns},
    ]
    path = run / "traj" / f"{instance['instance_id']}.jsonl"
    path.parent.mkdir()
    write_events(path, events)
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


@pytest.mark.parametrize("damage", [
    "deleted", "duplicated", "reordered", "renumbered", "terminal_count", "summary_count",
    "missing_turn", "missing_terminal_count", "missing_summary_count", "boolean_turn", "float_count",
])
def test_turn_sequence_and_counts_must_agree(tmp_path, damage):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")], turns=3)
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    if damage in {"deleted", "renumbered"}:
        del events[2]
        if damage == "renumbered":
            events[2]["turn"] = 2
    elif damage == "duplicated":
        events.insert(2, events[2])
    elif damage == "reordered":
        events[1], events[2] = events[2], events[1]
    elif damage in {"terminal_count", "float_count"}:
        events[-1]["turns"] = 2 if damage == "terminal_count" else 3.0
    elif damage in {"summary_count", "missing_summary_count"}:
        summary_path = trajectory.parents[1] / "summary.json"
        summary = json.loads(summary_path.read_text())
        if damage == "summary_count":
            summary["results"][0]["turns"] = 2
        else:
            del summary["results"][0]["turns"]
        write_json(summary_path, summary)
    elif damage == "missing_terminal_count":
        del events[-1]["turns"]
    elif damage == "missing_turn":
        del events[2]["turn"]
    else:
        events[1]["turn"] = True
    write_events(trajectory, events)
    with pytest.raises(ValueError, match="turn"):
        load_audit().build_audit(root, plan, [])


def test_zero_turn_attempt_is_retained(tmp_path):
    root, plan, _ = corpus(tmp_path, [], turns=0)
    report = load_audit().build_audit(root, plan, [])
    assert len(report["attempts"]) == 1
    assert report["totals"]["bash_requests"] == 0


@pytest.mark.parametrize(("source", "field", "value"), [
    ("terminal", "run_id", "example__repo-2"),
    ("terminal", "run_id", None),
    ("meta", "image", "sha256:other"),
    ("meta", "provider", "other"),
    ("meta", "model", "other"),
    ("meta", "max_turns", 41),
    ("meta", "timeout", 181),
    ("meta", "budget_reminders", True),
    ("meta", "explicit_finish", True),
    ("meta", "workdir", "/other"),
    ("meta", "hidden_paths", []),
    ("meta", "instance_id", "example__repo-2"),
    ("meta", "base_commit", "b" * 40),
    ("meta", "repo", "other/repo"),
    ("summary", "run_id", "other-run"),
    ("summary", "instances", 2),
    ("summary", "budget_reminders", True),
    ("summary", "mode", "single-shot"),
    ("summary_result", "mode", "single-shot"),
    ("config", "instance_ids", ["example__repo-2"]),
    ("config", "instance_ids", ["example__repo-1", "example__repo-1"]),
    ("config", "max_turns", 41),
    ("config", "timeout", 181),
    ("config", "platform", "linux/arm64"),
    ("planned", "provider", "other"),
    ("planned", "model", "other"),
    ("planned", "budget_reminders", True),
    ("planned", "explicit_finish", True),
    ("image", "requested", "other:latest"),
    ("image", "platform", "linux/arm64"),
    ("environment_policy", "hidden_paths", []),
    ("environment_policy", "version", "other"),
])
def test_cross_file_identity_and_settings_must_agree(tmp_path, source, field, value):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    run = trajectory.parents[1]
    if source in {"meta", "terminal"}:
        events[0 if source == "meta" else -1][field] = value
        write_events(trajectory, events)
    elif source == "planned":
        plan["runs"][0][field] = value
    else:
        filename = {"config": "run_config.json", "summary": "summary.json", "summary_result": "summary.json"}
        path = run / filename.get(source, "environment/example__repo-1.json")
        document = json.loads(path.read_text())
        target = {"config": document.get("config"), "summary": document,
                  "summary_result": document.get("results", [None])[0], "image": document.get("image"),
                  "environment_policy": document.get("policy")}[source]
        target[field] = value
        write_json(path, document)
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


def test_historical_corpus_recomputes_without_replay_or_writes(monkeypatch):
    import socket
    import subprocess

    audit = load_audit()
    plan = json.loads(audit.DEFAULT_PLAN.read_text())
    root = audit.ROOT / "evals/results"
    if not all((root / run["run_id"] / "summary.json").exists() for run in plan["runs"]):
        pytest.skip("historical results are not installed")

    def forbidden(*args, **kwargs):
        raise AssertionError("historical audit must remain read-only and offline")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    report = audit.build_audit(root, plan, [])
    assert len(report["attempts"]) == 30
    assert report["totals"]["bash_requests"] == 985
    assert report["replayed_commands"] == report["model_requests"] == 0


@pytest.mark.parametrize("damage", ["deleted", "duplicated", "swapped"])
def test_historical_trajectory_damage_is_rejected_in_memory(monkeypatch, damage):
    audit = load_audit()
    plan = json.loads(audit.DEFAULT_PLAN.read_text())
    root = audit.ROOT / "evals/results"
    if not all((root / run["run_id"] / "summary.json").exists() for run in plan["runs"]):
        pytest.skip("historical results are not installed")
    read_input = audit.read_input
    target = Path(plan["runs"][0]["run_id"]) / "traj/pydata__xarray-2905.jsonl"

    def damaged_input(results_root, relative, hashes, *, jsonl=False):
        events = read_input(results_root, relative, hashes, jsonl=jsonl)
        if relative == target:
            if damage == "swapped":
                replacement = target.with_name("pydata__xarray-3993.jsonl")
                events = read_input(results_root, replacement, hashes, jsonl=True)
            else:
                position = next(index for index, (_, event) in enumerate(events)
                                if event.get("type") == "turn" and event["turn"] == 2)
                if damage == "deleted":
                    del events[position]
                else:
                    events.insert(position, events[position])
        return events

    monkeypatch.setattr(audit, "read_input", damaged_input)
    with pytest.raises(ValueError, match="run_id" if damage == "swapped" else "turn"):
        audit.build_audit(root, plan, [])


@pytest.mark.parametrize("arguments", [
    "bad JSON", "[]", '{"cmd":null,"command":"pip download xarray"}', '{"cmd":4}', '{}',
    None, [], {"cmd": "git diff"},
])
def test_invalid_arguments_stay_in_denominator_but_are_not_classified(tmp_path, arguments):
    invalid = operation("invalid", "unused")
    invalid["function"]["arguments"] = arguments
    root, plan, _ = corpus(tmp_path, [invalid, operation("valid", "git diff")])
    report = load_audit().build_audit(root, plan, [])
    assert report["totals"]["bash_requests"] == 2
    assert report["totals"]["invalid_arguments"] == 1
    assert report["totals"]["classified_requests"] == 1
    assert report["totals"]["classifications"] == {"allow": 1, "block": 0, "unknown": 0}


@pytest.mark.parametrize("arguments", [
    '{"cmd":"git diff","cmd":"pip download xarray"}',
    '{"command":"git diff","command":"pip download xarray"}',
    '{"cmd":"pip download xarray","extra":NaN}',
])
def test_tool_argument_decoding_matches_the_execution_entry(tmp_path, arguments):
    from types import SimpleNamespace
    from patchloop.loop import _execute

    commands = []
    runner = SimpleNamespace(run=lambda command, timeout: commands.append(command) or "output")
    call = SimpleNamespace(id="request", function=SimpleNamespace(name="bash", arguments=arguments))
    _execute(call, runner, timeout=10)
    request = {"id": call.id, "function": {"name": "bash", "arguments": arguments}}
    root, plan, _ = corpus(tmp_path, [request])
    report = load_audit().build_audit(root, plan, [])
    assert commands == ["pip download xarray"]
    assert report["records"][0]["command"] == commands[0]
    assert report["totals"]["invalid_arguments"] == 0
    assert report["totals"]["classifications"]["block"] == 1


@pytest.mark.parametrize("damage", [
    "empty_imports", "missing_pytest", "missing_target", "missing_origin", "null_origin",
    "relative_origin", "external_target", "import_error", "failed_checks", "missing_status",
])
def test_incomplete_preflight_evidence_is_not_a_new_context(tmp_path, damage):
    root, plan, _ = corpus(tmp_path, [])
    path = root / "example-run/environment/example__repo-1.json"
    environment = json.loads(path.read_text())
    checks = environment["checks"]
    if damage == "empty_imports":
        checks["imports"] = {}
    elif damage in {"missing_pytest", "missing_target"}:
        del checks["imports"]["pytest" if damage == "missing_pytest" else "xarray"]
    elif damage == "missing_origin":
        del checks["imports"]["pytest"]["origin"]
    elif damage in {"null_origin", "relative_origin"}:
        checks["imports"]["pytest"]["origin"] = None if damage == "null_origin" else "pytest/__init__.py"
    elif damage == "external_target":
        checks["imports"]["xarray"]["origin"] = "/testbed/../other/xarray/__init__.py"
    elif damage == "import_error":
        checks["imports"]["pytest"]["error"] = "failed"
    elif damage == "failed_checks":
        checks["status"] = "failed"
    else:
        del checks["status"]
    write_json(path, environment)
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize("summary_policy", ["enforce", {"mode": "enforce"}, {}, None, "typo"])
def test_summary_policy_must_agree_with_config_and_trajectory(tmp_path, summary_policy):
    root, plan, trajectory = corpus(tmp_path, [])
    config_path = root / "example-run/run_config.json"
    config = json.loads(config_path.read_text())
    config["config"]["command_policy"] = {"mode": "off"}
    write_json(config_path, config)
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    events[0]["command_policy"] = {"mode": "off"}
    write_events(trajectory, events)
    summary_path = root / "example-run/summary.json"
    summary = json.loads(summary_path.read_text())
    summary["command_policy"] = summary_policy
    write_json(summary_path, summary)
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize("summary_policy", ["off", {"mode": "off"}])
def test_summary_policy_accepts_matching_record_formats(tmp_path, summary_policy):
    root, plan, _ = corpus(tmp_path, [])
    config_path = root / "example-run/run_config.json"
    config = json.loads(config_path.read_text())
    config["config"]["command_policy"] = {"mode": "off"}
    write_json(config_path, config)
    summary_path = root / "example-run/summary.json"
    summary = json.loads(summary_path.read_text())
    summary["command_policy"] = summary_policy
    write_json(summary_path, summary)
    assert len(load_audit().build_audit(root, plan, [])["attempts"]) == 1


@pytest.mark.parametrize("config_policy,summary_policy,meta_policy,valid", [
    (None, "enforce", {"mode": "off"}, False),
    ({"mode": "off"}, {"mode": "off", "policy_version": "v3"}, {"mode": "off", "policy_version": "v2"}, False),
    (None, "off", {"mode": "off"}, True),
    ({"mode": "off"}, {"mode": "off", "policy_version": "v3"}, {"mode": "off", "policy_version": "v3"}, True),
])
def test_missing_policy_fields_do_not_break_cross_source_comparisons(tmp_path, config_policy, summary_policy, meta_policy, valid):
    root, plan, trajectory = corpus(tmp_path, [])
    if config_policy is not None:
        path = root / "example-run/run_config.json"
        config = json.loads(path.read_text())
        config["config"]["command_policy"] = config_policy
        write_json(path, config)
    path = root / "example-run/summary.json"
    summary = json.loads(path.read_text())
    summary["command_policy"] = summary_policy
    write_json(path, summary)
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    events[0]["command_policy"] = meta_policy
    write_events(trajectory, events)
    if valid:
        assert len(load_audit().build_audit(root, plan, [])["attempts"]) == 1
    else:
        with pytest.raises(ValueError):
            load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize("event", [None, [], "turn", {}, {"type": "other"}, {"type": "meta"}, {"type": "result"}])
def test_invalid_or_extra_events_fail_closed(tmp_path, event):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    events.insert(1, event)
    write_events(trajectory, events)
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize(("field", "value"), [
    ("assistant", None), ("assistant", []), ("tools", None), ("tools", {}),
    ("tool_calls", {}), ("tool_calls", False), ("tool_calls", ""), ("tool_calls", [None]),
    ("tool_calls", [{"id": "bad", "function": None}]),
    ("tool_calls", [{"id": None, "function": {"name": "bash", "arguments": "{}"}}]),
])
def test_malformed_tool_containers_cannot_hide_requests(tmp_path, field, value):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    target = events[1]["assistant"] if field == "tool_calls" else events[1]
    target[field] = value
    write_events(trajectory, events)
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize("raw", [
    '{"type":"turn","turn":1,"assistant":{"tool_calls":[],"tool_calls":null},"tools":[]}',
    '{"type":"turn","turn":1,"assistant":{"tool_calls":[]},"tools":[],"seconds":NaN}',
])
def test_ambiguous_saved_json_fails_closed(tmp_path, raw):
    root, plan, trajectory = corpus(tmp_path, [])
    lines = trajectory.read_text().splitlines()
    trajectory.write_text("\n".join([lines[0], raw, lines[-1]]) + "\n")
    with pytest.raises(ValueError):
        load_audit().build_audit(root, plan, [])


def test_auxiliary_events_and_absent_tool_results_remain_auditable(tmp_path):
    root, plan, trajectory = corpus(tmp_path, [operation("allowed", "git diff")])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    events.insert(1, {"type": "budget_reminder", "turn": 1, "remaining": 40,
                      "message": {"role": "user", "content": "40 calls remain"}})
    del events[2]["tools"]
    write_events(trajectory, events)
    report = load_audit().build_audit(root, plan, [])
    assert report["totals"]["bash_requests"] == report["totals"]["missing_tool_results"] == 1
    assert report["records"][0]["line"] == 3


@pytest.mark.parametrize("turn", [None, True, 1.0, 0, 2])
def test_auxiliary_events_must_reference_the_next_turn(tmp_path, turn):
    root, plan, trajectory = corpus(tmp_path, [])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    events.insert(1, {"type": "budget_reminder", "turn": turn, "remaining": 40,
                      "message": {"role": "user", "content": "40 calls remain"}})
    write_events(trajectory, events)
    with pytest.raises(ValueError, match="turn"):
        load_audit().build_audit(root, plan, [])


def test_null_plan_settings_fail_closed(tmp_path):
    root, plan, _ = corpus(tmp_path, [])
    plan["settings"] = None
    with pytest.raises(ValueError, match="settings"):
        load_audit().build_audit(root, plan, [])


@pytest.mark.parametrize("field", ["tool_calls", "tools"])
def test_absent_optional_tool_fields_remain_auditable(tmp_path, field):
    root, plan, trajectory = corpus(tmp_path, [])
    events = [json.loads(line) for line in trajectory.read_text().splitlines()]
    target = events[1]["assistant"] if field == "tool_calls" else events[1]
    del target[field]
    write_events(trajectory, events)
    report = load_audit().build_audit(root, plan, [])
    assert len(report["attempts"]) == 1
    assert report["totals"]["bash_requests"] == 0


@pytest.mark.parametrize("filename", ["summary.json", "run_config.json", "environment/example__repo-1.json"])
def test_cli_rejects_non_object_documents_without_report(tmp_path, filename, capsys):
    root, plan, trajectory = corpus(tmp_path, [])
    write_json(trajectory.parents[1] / filename, [])
    plan_path, cases_path = tmp_path / "plan.json", tmp_path / "cases.json"
    write_json(plan_path, plan)
    write_json(cases_path, {"cases": []})
    destination = tmp_path / "audit.json"
    with pytest.raises(SystemExit) as error:
        load_audit().main(["--results-root", str(root), "--plan", str(plan_path),
                           "--cases", str(cases_path), "--output", str(destination)])
    assert error.value.code == 2
    assert "Offline audit failed:" in capsys.readouterr().err
    assert not destination.exists()


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
