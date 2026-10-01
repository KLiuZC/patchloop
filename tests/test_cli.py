from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import patchloop.cli as cli


@pytest.mark.parametrize("flag,expected", [([], False), (["--budget-reminders"], True)])
@pytest.mark.parametrize("command", ["run", "eval"])
def test_cli_forwards_reminders(monkeypatch, tmp_path, flag, expected, command):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a: SimpleNamespace())
    seen = []
    if command == "run":
        def fake_run(*args, **kwargs):
            seen.append(kwargs["budget_reminders"])
            return SimpleNamespace(finished=True, patch="", turns=1, prompt_tokens=10, completion_tokens=1, run_id="fake")

        monkeypatch.setattr(cli, "run", fake_run)
        args = ["run", "-w", str(tmp_path), "-t", "task", *flag]
    else:
        monkeypatch.setattr(cli, "load_instances", lambda *a: [{"instance_id": "x__y-1"}])
        monkeypatch.setattr(cli, "_print_summary", lambda *a: None)

        def fake_evaluate(*args, **kwargs):
            seen.append(kwargs["budget_reminders"])
            return {}

        monkeypatch.setattr(cli, "evaluate", fake_evaluate)
        args = ["eval", "-r", "fake", "-i", "x__y-1", "--no-judge", *flag]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [expected]


def test_cli_rejects_single_shot_reminders_before_model_setup(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a: pytest.fail("must reject before creating model client"))
    result = CliRunner().invoke(cli.app, ["eval", "-r", "fake", "-i", "x__y-1", "--mode", "single-shot", "--budget-reminders"])
    assert result.exit_code == 2
    assert "requires --mode agent" in result.output
