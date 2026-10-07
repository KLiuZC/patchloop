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


def test_serve_uses_local_address_and_selected_database(monkeypatch, tmp_path):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr(cli, "LLM", lambda *a: pytest.fail("serving tasks must not create a model client"))
    monkeypatch.setattr(cli, "load_dotenv", lambda: pytest.fail("task API does not need provider credentials"))
    database = tmp_path / "records.sqlite3"
    result = CliRunner().invoke(cli.app, ["serve", "--db", str(database), "--port", "8123"])
    assert result.exit_code == 0, result.output
    app, options = calls[0]
    assert options == {"host": "127.0.0.1", "port": 8123}
    assert app.state.task_store.path == database


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
