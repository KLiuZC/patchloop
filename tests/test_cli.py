from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import patchloop.cli as cli


@pytest.mark.parametrize("flag,expected", [
    ([], (False, False)),
    (["--budget-reminders"], (True, False)),
    (["--explicit-finish"], (False, True)),
    (["--budget-reminders", "--explicit-finish"], (True, True)),
])
@pytest.mark.parametrize("command", ["run", "eval"])
def test_cli_forwards_reminders(monkeypatch, tmp_path, flag, expected, command):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a: SimpleNamespace())
    seen = []
    if command == "run":
        def fake_run(*args, **kwargs):
            seen.append((kwargs["budget_reminders"], kwargs["explicit_finish"]))
            return SimpleNamespace(finished=True, patch="", turns=1, prompt_tokens=10, completion_tokens=1,
                                   run_id="fake", final_message="done")

        monkeypatch.setattr(cli, "run", fake_run)
        args = ["run", "-w", str(tmp_path), "-t", "task", *flag]
    else:
        monkeypatch.setattr(cli, "load_instances", lambda *a: [{"instance_id": "x__y-1"}])
        monkeypatch.setattr(cli, "_print_summary", lambda *a: None)

        def fake_evaluate(*args, **kwargs):
            seen.append((kwargs["budget_reminders"], kwargs["explicit_finish"]))
            return {}

        monkeypatch.setattr(cli, "evaluate", fake_evaluate)
        args = ["eval", "-r", "fake", "-i", "x__y-1", "--no-judge", *flag]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [expected]


@pytest.mark.parametrize("option", ["--budget-reminders", "--explicit-finish"])
def test_cli_rejects_single_shot_agent_options_before_model_setup(monkeypatch, option):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a: pytest.fail("must reject before creating model client"))
    result = CliRunner().invoke(cli.app, ["eval", "-r", "fake", "-i", "x__y-1", "--mode", "single-shot", option])
    assert result.exit_code == 2
    assert "requires --mode agent" in result.output


@pytest.mark.parametrize("policy", ["off", "audit", "enforce"])
def test_cli_forwards_command_policy(monkeypatch, policy):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *args: object())
    monkeypatch.setattr(cli, "load_instances", lambda *args: [])
    monkeypatch.setattr(cli, "_print_summary", lambda *args: None)
    seen = []
    def evaluate(*args, **kwargs):
        seen.append(kwargs["command_policy"])
        return {}
    monkeypatch.setattr(cli, "evaluate", evaluate)
    result = CliRunner().invoke(cli.app, ["eval", "-r", "test", "-i", "x-1", "--no-judge", "--command-policy", policy])
    assert result.exit_code == 0, result.output
    assert seen == [policy]


def test_eval_command_policy_defaults_off():
    from typer.main import get_command
    command = get_command(cli.app).commands["eval"]
    with command.make_context("eval", ["-r", "test", "-i", "x-1"]) as context:
        assert context.params["command_policy"] == "off"


@pytest.mark.parametrize("command", ["run", "repair", "repair-swebench", "worker"])
def test_non_eval_commands_do_not_expose_policy(command):
    from typer.main import get_command
    options = get_command(cli.app).commands[command].params
    assert not any(parameter.name == "command_policy" for parameter in options)


@pytest.mark.parametrize("arguments", [["--command-policy", "typo"],
    ["--command-policy", "audit", "--mode", "single-shot"],
    ["--command-policy", "enforce", "--mode", "single-shot"]])
def test_cli_rejects_invalid_policy_before_model_setup(monkeypatch, arguments):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *args: pytest.fail("must not initialize a model"))
    result = CliRunner().invoke(cli.app, ["eval", "-r", "test", "-i", "x-1", *arguments])
    assert result.exit_code == 2 and "command-policy" in result.output


def test_serve_uses_local_address_and_selected_database(monkeypatch, tmp_path):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr(cli, "LLM", lambda *a: pytest.fail("serving tasks must not create a model client"))
    monkeypatch.setattr(cli, "load_dotenv", lambda: pytest.fail("task API does not need provider credentials"))
    monkeypatch.setattr(cli, "dotenv_values", lambda path: {})
    database = tmp_path / "records.sqlite3"
    result = CliRunner().invoke(cli.app, ["serve", "--db", str(database), "--port", "8123"])
    assert result.exit_code == 0, result.output
    app, options = calls[0]
    assert options == {"host": "127.0.0.1", "port": 8123}
    assert app.state.task_store.path == database


@pytest.mark.parametrize("environment_token", [None, "environment-publication-token"])
def test_serve_loads_only_publication_configuration(monkeypatch, tmp_path, environment_token):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(app))
    monkeypatch.setattr(cli, "load_dotenv", lambda: pytest.fail("must not load provider keys into the API environment"))
    monkeypatch.setattr(cli, "dotenv_values", lambda path: {
        "PATCHLOOP_GITHUB_TOKEN": "file-publication-token", "DEEPSEEK_API_KEY": "provider-placeholder",
    })
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    if environment_token is None:
        monkeypatch.delenv("PATCHLOOP_GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PATCHLOOP_GITHUB_TOKEN", environment_token)
    result = CliRunner().invoke(cli.app, ["serve", "--db", str(tmp_path / "records.sqlite3")])
    assert result.exit_code == 0, result.output
    assert calls[0].state.github_publication_service.token_provider() == (environment_token or "file-publication-token")
    assert "DEEPSEEK_API_KEY" not in cli.os.environ
    assert "publication-token" not in result.output


def test_serve_rejects_invalid_port():
    result = CliRunner().invoke(cli.app, ["serve", "--port", "65536"])
    assert result.exit_code == 2


def test_worker_cli_forwards_target_database_and_single_task(monkeypatch, tmp_path):
    from uuid import uuid4
    import patchloop.worker as module

    seen = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)

    class Worker:
        def __init__(self, store, artifacts, log):
            seen.append((store.path, artifacts))

        def run(self, **kwargs):
            seen.append(kwargs)

    monkeypatch.setattr(module, "TaskWorker", Worker)
    task_id = uuid4()
    result = CliRunner().invoke(cli.app, ["worker", "--db", str(tmp_path / "tasks.sqlite3"),
                                         "--task-id", str(task_id), "--once"])
    assert result.exit_code == 0, result.output
    assert seen == [(tmp_path / "tasks.sqlite3", None), {"once": True, "task_id": task_id}]
