import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.main import get_command

from patchloop.cli import app

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "evals/profiles/xarray3-isolation"


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("xarray_recheck_launcher", PROFILE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_checks_without_starting_any_model_process(launcher, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(launcher, "check_inputs", lambda plan: seen.append(plan["campaign"]))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw: pytest.fail("check-only must not launch eval"))
    assert launcher.main([]) == 0
    assert seen == ["xarray3-isolation-v1"]
    assert "no model requests" in capsys.readouterr().out


def test_unknown_or_existing_run_is_rejected_before_checks(launcher, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "check_inputs", lambda *a: pytest.fail("must reject before preflight"))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch eval"))
    assert launcher.main(["--run", "not-in-plan"]) == 2
    (tmp_path / "evals/results/x3-clean-ds-base-r1").mkdir(parents=True)
    assert launcher.main(["--run", "x3-clean-ds-base-r1"]) == 2


def test_explicit_run_starts_exactly_one_checked_cli_process(launcher, monkeypatch, tmp_path):
    events = []
    binary = tmp_path / "bin/agent"
    binary.parent.mkdir()
    binary.write_text("placeholder")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher.sys, "executable", str(binary.parent / "python"))
    monkeypatch.setattr(launcher, "check_inputs", lambda plan: events.append("checked"))
    def run(command, **kwargs):
        events.append(command)
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["cwd"] == tmp_path
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main(["--run", "x3-clean-ds-finish-r2"]) == 2
    assert len(events) == 2 and events[0] == "checked"
    command = events[1]
    assert command[0] == str(binary)
    assert command[command.index("--run-id") + 1] == "x3-clean-ds-finish-r2"
    assert "--budget-reminders" in command and "--explicit-finish" in command


def test_source_drift_rejects_before_docker_or_dataset(launcher, monkeypatch, tmp_path):
    import patchloop.environment as environment
    import patchloop.eval as evaluation
    monkeypatch.setattr(environment, "inspect_image", lambda *a: pytest.fail("no Docker inspection"))
    monkeypatch.setattr(evaluation, "load_instances", lambda *a: pytest.fail("no dataset read"))
    (tmp_path / "source.py").write_text("changed")
    with pytest.raises(ValueError, match="frozen input changed"):
        launcher.check_inputs({"input_sha256": {"source.py": "original"}}, tmp_path)


def test_image_drift_rejects_execution(launcher, monkeypatch):
    import patchloop.environment as environment
    import patchloop.eval as evaluation
    plan = json.loads((PROFILE / "plan.json").read_text())
    rows = [{k: row[k] for k in ("instance_id", "repo", "base_commit")} for row in plan["instances"]]
    plan["selected_instances_sha256"] = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    monkeypatch.setattr(evaluation, "load_instances", lambda *a: rows)
    monkeypatch.setattr(environment, "inspect_image", lambda *a: {"id": "different", "platform": "linux/amd64"})
    with pytest.raises(ValueError, match="image changed"):
        launcher.check_inputs(plan)


def test_all_planned_commands_parse_as_the_intended_conditions(launcher):
    plan = json.loads((PROFILE / "plan.json").read_text())
    assert len(plan["runs"]) == 10 and plan["planned_model_attempts"] == 30
    assert len({r["run_id"] for r in plan["runs"]}) == 10
    eval_cli = get_command(app).commands["eval"]
    for run in plan["runs"]:
        args = launcher.command_for(plan, run)
        with eval_cli.make_context("eval", args[2:]) as ctx:
            parsed = ctx.params
        assert parsed["provider"] == run["provider"] and parsed["model"] == run["model"]
        assert parsed["budget_reminders"] == run["budget_reminders"]
        assert parsed["explicit_finish"] == run["explicit_finish"]
        assert parsed["max_turns"] == 40 and parsed["timeout"] == 180
        assert parsed["workers"] == 2 and parsed["memory"] == "3g"
        assert parsed["judge"] is True and parsed["judge_workers"] == 1
