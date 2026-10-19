import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import patchloop.command_runtime as runtime
from patchloop.command_policy import PolicyContext
from patchloop.command_runtime import CommandPolicy
from patchloop.loop import run


class ScriptedModel:
    provider = "fake"
    model = "fake"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.tool_definitions = []

    def chat(self, messages, tools):
        self.requests.append([dict(message) for message in messages])
        self.tool_definitions.append(tools)
        return next(self.responses), SimpleNamespace(prompt_tokens=10, completion_tokens=5)


class RecordingRunner:
    workdir = "/testbed"
    environment = "Linux Docker container"

    def __init__(self):
        self.commands = []

    def describe(self):
        return {"sandbox": "fake", "workdir": self.workdir}

    def run(self, command, timeout):
        self.commands.append((command, timeout))
        return "original tool output"

    def diff(self):
        return ""


def reply(*calls, content=None):
    return SimpleNamespace(content=content, tool_calls=list(calls))


def call(identifier, command, *, key="cmd", tool="bash"):
    return SimpleNamespace(id=identifier, function=SimpleNamespace(
        name=tool, arguments=json.dumps({key: command}),
    ))


def records(directory, result):
    return [json.loads(line) for line in (directory / f"{result.run_id}.jsonl").read_text().splitlines()]


def test_enforce_returns_rejection_without_executing_any_part(tmp_path):
    command = "printf touched > /tmp/policy-marker; pip download xarray"
    model = ScriptedModel([reply(call("blocked", command)), reply(content="done")])
    runner = RecordingRunner()
    policy = CommandPolicy("enforce", PolicyContext("/testbed", "/testbed"))
    result = run("fix", model, runner, max_turns=2, traj_dir=tmp_path, command_policy=policy)
    assert runner.commands == []
    feedback = model.requests[1][-1]
    assert feedback["tool_call_id"] == "blocked" and "Command blocked" in feedback["content"]
    assert result.turns == 2 and result.stop_reason == "no_tool_calls" and not result.error
    events = records(tmp_path, result)
    event = next(row for row in events if row["type"] == "command_policy")
    assert event["turn"] == 1 and event["tool_call_id"] == "blocked"
    assert event["classification"] == "block" and event["action"] == "block"
    assert events.index(event) < next(index for index, row in enumerate(events) if row["type"] == "turn")
    assert result.command_policy_counts["blocked"] == 1


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize("key", ["cmd", "command"])
@pytest.mark.parametrize("command,classification", [
    ("cat xarray/core/variable.py", "allow"),
    ("pip download xarray", "block"),
    ("python -c 'print(1)'", "unknown"),
    ("cat 'unfinished", "unknown"),
])
def test_modes_preserve_raw_commands_and_report_actions(tmp_path, monkeypatch, mode, key, command, classification):
    if mode == "off":
        monkeypatch.setattr(runtime, "assess_command", lambda *args: pytest.fail("off must not classify"))
    model = ScriptedModel([reply(call("operation", command, key=key)), reply(content="done")])
    runner = RecordingRunner()
    policy = CommandPolicy(mode, PolicyContext("/testbed", "/testbed"))
    result = run("fix", model, runner, traj_dir=tmp_path, command_policy=policy)
    blocked = mode == "enforce" and classification == "block"
    assert runner.commands == ([] if blocked else [(command, 60)])
    feedback = model.requests[1][-1]
    assert feedback["tool_call_id"] == "operation"
    if not blocked:
        assert feedback["content"] == "original tool output"
    events = [row for row in records(tmp_path, result) if row["type"] == "command_policy"]
    if mode == "off":
        assert not events and result.command_policy_counts == {}
    else:
        assert len(events) == 1 and events[0]["classification"] == classification
        assert events[0]["action"] == ("block" if blocked else "execute")
        assert events[0]["would_block"] == (mode == "audit" and classification == "block")
        assert result.command_policy_counts["checked"] == 1
        assert result.command_policy_counts["blocked"] == int(blocked)
        assert result.command_policy_counts["execution_requests"] == int(not blocked)


def test_rejections_consume_budget_without_retrying(tmp_path):
    model = ScriptedModel([reply(call("blocked", "find / -name '*.py'"))])
    runner = RecordingRunner()
    result = run("fix", model, runner, max_turns=1, traj_dir=tmp_path,
                 command_policy=CommandPolicy("enforce", PolicyContext("/testbed", "/testbed")))
    assert result.turns == 1 and result.stop_reason == "max_turns" and not result.error
    assert result.prompt_tokens == 10 and len(model.requests) == 1
    assert not runner.commands


def test_multiple_calls_and_finish_keep_tool_results_paired(tmp_path):
    model = ScriptedModel([
        reply(call("allowed", "git diff"), call("blocked", "pip download xarray")),
        reply(call("finished", "Fixed", key="summary", tool="finish")),
    ])
    runner = RecordingRunner()
    result = run("fix", model, runner, explicit_finish=True, traj_dir=tmp_path,
                 command_policy=CommandPolicy("enforce", PolicyContext("/testbed", "/testbed")))
    assert result.stop_reason == "finish" and result.turns == 2
    assert runner.commands == [("git diff", 60)]
    assert [row["tool_call_id"] for row in model.requests[1] if row["role"] == "tool"] == ["allowed", "blocked"]
    assert result.command_policy_counts["checked"] == 2
    assert result.command_policy_counts["blocked"] == 1


@pytest.mark.parametrize("arguments", ["bad json", "[]", '{"cmd": null}', '{"cmd": 3}', '{}'])
def test_invalid_arguments_do_not_reach_policy_or_runner(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(runtime, "assess_command", lambda *args: pytest.fail("invalid arguments must not classify"))
    operation = call("bad", "unused")
    operation.function.arguments = arguments
    model = ScriptedModel([reply(operation), reply(content="done")])
    runner = RecordingRunner()
    result = run("fix", model, runner, traj_dir=tmp_path,
                 command_policy=CommandPolicy("enforce", PolicyContext("/testbed", "/testbed")))
    assert not runner.commands and result.command_policy_counts["checked"] == 0
    assert "invalid arguments" in model.requests[1][-1]["content"]


def test_policy_does_not_change_the_prompt_or_tool_definitions(tmp_path):
    requests = []
    definitions = []
    for mode in ("off", "audit", "enforce"):
        model = ScriptedModel([reply(content="done")])
        run("same task", model, RecordingRunner(), traj_dir=tmp_path / mode,
            command_policy=CommandPolicy(mode, PolicyContext("/testbed", "/testbed")))
        requests.append(model.requests[0])
        definitions.append(model.tool_definitions[0])
    assert requests[0] == requests[1] == requests[2]
    assert definitions[0] == definitions[1] == definitions[2]


def test_runner_failure_preserves_the_preexecution_event(tmp_path):
    class BrokenRunner(RecordingRunner):
        def run(self, command, timeout):
            raise RuntimeError("runner stopped")

    model = ScriptedModel([reply(call("allowed", "git diff"))])
    result = run("fix", model, BrokenRunner(), traj_dir=tmp_path, capture_errors=True,
                 command_policy=CommandPolicy("enforce", PolicyContext("/testbed", "/testbed")))
    assert result.stop_reason == "error"
    assert result.command_policy_counts["execution_requests"] == 1
    assert result.command_policy_counts["blocked"] == 0
    assert any(row["type"] == "command_policy" for row in records(tmp_path, result))


def test_policy_errors_do_not_fall_back_to_execution(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("classifier failed")

    monkeypatch.setattr(runtime, "assess_command", fail)
    runner = RecordingRunner()
    result = run("fix", ScriptedModel([reply(call("operation", "git diff"))]), runner,
                 traj_dir=tmp_path, capture_errors=True,
                 command_policy=CommandPolicy("enforce", PolicyContext("/testbed", "/testbed")))
    assert not runner.commands and result.stop_reason == "error"


def test_policy_context_must_match_runner_before_model_call(tmp_path):
    model = ScriptedModel([])
    with pytest.raises(ValueError, match="cwd"):
        run("fix", model, RecordingRunner(), traj_dir=tmp_path,
            command_policy=CommandPolicy("enforce", PolicyContext("/other", "/other")))
    assert not model.requests


def test_context_is_built_from_passed_import_origins_not_host_environment():
    instance = {"instance_id": "xarray-1", "repo": "pydata/xarray", "base_commit": "a" * 40}
    report = {"status": "passed", "checks": {"imports": {
        "xarray": {"origin": "/testbed/xarray/__init__.py"},
        "pytest": {"origin": "/opt/miniconda3/envs/testbed/lib/python3.10/site-packages/pytest/__init__.py"},
    }}}
    policy = runtime.swebench_command_policy("enforce", instance, report, "/testbed")
    assert policy.context.target_modules == ("xarray",)
    assert policy.context.dependency_roots == ("/opt/miniconda3/envs/testbed/lib/python3.10/site-packages",)
    assert "/opt/miniconda3/pkgs" in policy.context.cache_roots
    assert policy.inspect("cat /opt/miniconda3/envs/testbed/lib/python3.10/site-packages/numpy/__init__.py", "dependency")["classification"] == "allow"
    assert policy.inspect("cat /opt/miniconda3/envs/testbed/lib/python3.10/site-packages/xarray/core/variable.py", "extra")["classification"] == "block"
    meta = policy.metadata()
    assert len(meta["context_sha256"]) == 64
    assert policy.inspect("git diff", "diff")["context_sha256"] == meta["context_sha256"]
    assert runtime.swebench_command_policy("off", {}, None, "/testbed") is None
    fixture = json.loads((Path(__file__).parents[1] / "evals/profiles/command-policy/cases.json").read_text())
    assert meta["development_cases_version"] == fixture["suite_version"]


@pytest.mark.parametrize("mode", ["audit", "enforce"])
def test_unverified_context_is_rejected(mode):
    from patchloop.environment import EnvironmentCheckError
    with pytest.raises(EnvironmentCheckError, match="preflight"):
        runtime.swebench_command_policy(mode, {"repo": "pydata/xarray"}, {"status": "failed"}, "/testbed")


def test_counters_are_per_run_even_when_policy_is_reused(tmp_path):
    policy = CommandPolicy("enforce", PolicyContext("/testbed", "/testbed"))
    for number in range(2):
        model = ScriptedModel([reply(call("operation", "git diff")), reply(content="done")])
        result = run("fix", model, RecordingRunner(), traj_dir=tmp_path / str(number), command_policy=policy)
        assert result.command_policy_counts["checked"] == 1


@pytest.mark.parametrize("failure", [False, True])
def test_eval_solve_preserves_policy_counts_and_preflight_order(tmp_path, monkeypatch, failure):
    import patchloop.eval as evaluation
    order = []

    class Runner(RecordingRunner):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, command, timeout):
            order.append("execute")
            if failure:
                raise RuntimeError("runner stopped")
            return super().run(command, timeout)

    runner = Runner()
    monkeypatch.setattr(evaluation, "solver_runner", lambda *args, **kwargs: runner)
    monkeypatch.setattr(evaluation, "inspect_image", lambda *args: {"id": "pinned"})
    def preflight(*args):
        order.append("preflight")
        return {"status": "passed", "checks": {"imports": {"pytest": {"origin": "/usr/lib/python3/dist-packages/pytest/__init__.py"}}}}
    monkeypatch.setattr(evaluation, "prepare_swebench_environment", preflight)
    model = ScriptedModel([
        reply(call("blocked", "pip download xarray")),
        reply(call("allowed", "git diff")), reply(content="done"),
    ])
    result, patch = evaluation.solve(
        {"instance_id": "xarray-1", "repo": "pydata/xarray", "base_commit": "a" * 40, "problem_statement": "fix"},
        model, "agent", 3, 60, tmp_path, command_policy="enforce",
    )
    assert order == ["preflight", "execute"]
    assert result.command_policy_counts["blocked"] == 1
    assert result.command_policy_counts["execution_requests"] == 1
    assert result.command_policy_counts["checked"] == 2
    assert bool(result.error) == failure
    assert result.environment_status == "passed"


def test_eval_does_not_start_policy_or_model_after_preflight_failure(tmp_path, monkeypatch):
    import patchloop.eval as evaluation
    from patchloop.environment import EnvironmentCheckError

    class Runner(RecordingRunner):
        def __enter__(self):return self
        def __exit__(self, *args):pass

    monkeypatch.setattr(evaluation, "solver_runner", lambda *args, **kwargs: Runner())
    monkeypatch.setattr(evaluation, "inspect_image", lambda *args: {"id": "pinned"})
    def preflight(*args):
        raise EnvironmentCheckError("preflight rejected")
    monkeypatch.setattr(evaluation, "prepare_swebench_environment", preflight)
    monkeypatch.setattr(runtime, "assess_command", lambda *args: pytest.fail("no classification"))
    model = ScriptedModel([])
    result, _ = evaluation.solve(
        {"instance_id": "xarray-1", "repo": "pydata/xarray"}, model,
        "agent", 3, 60, tmp_path, command_policy="enforce",
    )
    assert not model.requests and not result.command_policy_counts
    assert result.stop_reason == "environment_failed"


@pytest.mark.parametrize("isolated", [False, True])
def test_bounded_metadata_and_read_execute_together_only_with_isolation(tmp_path, isolated):
    command = "git log --oneline -5 && cat xarray/core/variable.py"
    model = ScriptedModel([reply(call("metadata", command)), reply(content="done")])
    runner = RecordingRunner()
    context = replace(PolicyContext("/testbed", "/testbed"), isolated_history=isolated)
    result = run("fix", model, runner, traj_dir=tmp_path, command_policy=CommandPolicy("enforce", context))
    assert runner.commands == ([(command, 60)] if isolated else [])
    assert result.command_policy_counts["blocked"] == (0 if isolated else 1)
    assert result.command_policy_counts["execution_requests"] == (1 if isolated else 0)
    assert records(tmp_path, result)[0]["command_policy"]["context"]["isolated_history"] == isolated


def test_allowed_metadata_does_not_unblock_a_forbidden_tail(tmp_path):
    model = ScriptedModel([reply(call("combined", "git log -3; printf touched > /tmp/marker; pip download xarray"))])
    runner = RecordingRunner()
    context = replace(PolicyContext("/testbed", "/testbed"), isolated_history=True)
    result = run("fix", model, runner, max_turns=1, traj_dir=tmp_path, command_policy=CommandPolicy("enforce", context))
    assert runner.commands == [] and result.command_policy_counts["blocked"] == 1
