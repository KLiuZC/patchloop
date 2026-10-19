import json
from types import SimpleNamespace

import pytest

import patchloop.eval as evaluation
from patchloop.eval import InstanceResult, summarize


class Runner:
    workdir = "/testbed"
    environment = "fake container"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def describe(self):
        return {"sandbox": "fake", "workdir": self.workdir}

    def run(self, command, timeout):
        return "ok"

    def diff(self):
        return "diff\n"


class Model:
    provider = "deepseek"
    model = "deepseek-flash"
    temperature = None
    client = SimpleNamespace(timeout=120.0, max_retries=3)

    def __init__(self, failure=None, usage=True):
        self.calls = 0
        self.failure = failure
        self.usage = usage

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 2 and self.failure:
            raise self.failure
        calls = [SimpleNamespace(id="read", function=SimpleNamespace(name="bash", arguments='{"cmd":"git diff"}'))] if self.calls == 1 else []
        return SimpleNamespace(content="done", tool_calls=calls), (
            SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_cache_hit_tokens=50) if self.usage else None
        )


def setup_runner(monkeypatch, runner=None):
    monkeypatch.setattr(evaluation, "inspect_image", lambda *args: {"id": "pinned"})
    monkeypatch.setattr(evaluation, "solver_runner", lambda *args, **kwargs: runner or Runner())
    monkeypatch.setattr(evaluation, "prepare_swebench_environment", lambda *args: {"status": "passed"})


INSTANCE = {"instance_id": "example__repo-1", "repo": "example/repo", "problem_statement": "fix"}


def test_api_error_preserves_received_usage_and_marks_remaining_bill_unknown(tmp_path, monkeypatch):
    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, Model(RuntimeError("request timed out")), "agent", 3, 10, tmp_path)
    assert result.error and result.turns == 1
    assert (result.prompt_tokens, result.cached_tokens, result.completion_tokens) == (100, 50, 20)
    assert result.usage_status == "incomplete"
    assert result.cost is None and result.known_cost > 0
    assert result.model_calls_attempted == 2


def test_tool_error_preserves_usage_without_double_counting(tmp_path, monkeypatch):
    class BrokenRunner(Runner):
        def run(self, command, timeout):
            raise RuntimeError("container stopped")

    setup_runner(monkeypatch, BrokenRunner())
    result, _ = evaluation.solve(INSTANCE, Model(), "agent", 3, 10, tmp_path)
    assert result.prompt_tokens == 100 and result.completion_tokens == 20
    assert result.error and result.known_cost > 0
    assert result.cost is None


def test_response_without_usage_is_not_reported_as_free(tmp_path, monkeypatch):
    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, Model(usage=False), "agent", 3, 10, tmp_path)
    assert result.finished and not result.error and result.turns == 2
    assert result.usage_status == "incomplete" and result.cost is None


def test_complete_usage_matches_terminal_and_turn_records(tmp_path, monkeypatch):
    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, Model(), "agent", 3, 10, tmp_path)
    assert result.usage_status == "recorded"
    assert result.prompt_tokens == 200 and result.completion_tokens == 40
    assert result.cost == result.known_cost and result.cost > 0


def test_unknown_cost_and_ungraded_rows_do_not_shrink_denominators():
    rows = [InstanceResult("first", "agent", resolved=True, cost=0.2),
            InstanceResult("second", "agent", error="API error", cost=None)]
    summary = summarize(rows, "run", "agent", Model())
    assert summary["resolve_rate"] is None
    assert summary["cost_cny"] is None
    assert summary["known_cost_cny"] == 0.2 and summary["unknown_cost_instances"] == 1
    assert summary["requires_review"] is True


def test_interruption_saves_received_usage_before_stopping_batch(tmp_path, monkeypatch):
    setup_runner(monkeypatch)
    model = Model(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        evaluation.evaluate([INSTANCE, {**INSTANCE, "instance_id": "example__repo-2"}], model, "interrupt", tmp_path, judge=False)
    saved = json.loads((tmp_path / "results.jsonl").read_text().splitlines()[0])
    assert saved["stop_reason"] == "interrupted" and saved["prompt_tokens"] == 100
    assert saved["cost"] is None and model.calls == 2
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["requires_review"] and summary["invocation"]["interrupted"]
    assert summary["planned_instances"] == 2 and summary["pending_instance_ids"] == ["example__repo-2"]


def test_cli_prints_unknown_cost_and_exits_for_review(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import patchloop.cli as cli

    summary = summarize([InstanceResult("example__repo-1", "agent", cost=None, error="API error")], "review", "agent", Model())
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *args: Model())
    monkeypatch.setattr(cli, "load_instances", lambda *args: [INSTANCE])
    monkeypatch.setattr(cli, "evaluate", lambda *args, **kwargs: summary)
    result = CliRunner().invoke(cli.app, ["eval", "-r", "review", "-i", "example__repo-1"])
    assert result.exit_code == 2 and "total unknown" in result.output


@pytest.mark.parametrize("grading_error", [False, True])
def test_batch_grading_errors_remain_unknown_and_require_review(tmp_path, monkeypatch, grading_error):
    setup_runner(monkeypatch)
    monkeypatch.setattr(evaluation, "image_present", lambda *args: True)
    monkeypatch.setattr(evaluation, "judge_predictions", lambda *args: ({INSTANCE["instance_id"]: {
        "resolved": None if grading_error else False, "patch_applied": None if grading_error else True,
        "status": "error" if grading_error else "failed", "error": "missing report" if grading_error else "",
    }}, "official-run"))
    result = evaluation.evaluate([INSTANCE], Model(), "graded", tmp_path)
    assert result["grading_errors"] == int(grading_error)
    assert result["requires_review"] is grading_error
    assert result["resolve_rate"] == (None if grading_error else 0.0)
    assert result["results"][0]["resolved"] is (None if grading_error else False)


def test_first_request_failure_is_one_attempt_not_free_work(tmp_path, monkeypatch):
    class FailingModel(Model):
        def chat(self, messages, tools):
            raise RuntimeError("API request failed")

    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, FailingModel(), "agent", 3, 10, tmp_path)
    assert result.model_calls_attempted == 1 and result.turns == 0
    assert result.cost is None and result.usage_status == "incomplete"


@pytest.mark.parametrize("damage", ["tokens", "turns", "identity", "missing_terminal", "missing_usage"])
def test_recovery_detects_inconsistent_or_incomplete_trajectory(tmp_path, monkeypatch, damage):
    from patchloop.eval_usage import recover_usage

    setup_runner(monkeypatch)
    evaluation.solve(INSTANCE, Model(), "agent", 3, 10, tmp_path)
    path = tmp_path / f"{INSTANCE['instance_id']}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "tokens":
        events[-1]["prompt_tokens"] += 1
    elif damage == "turns":
        events[1]["turn"] = 2
    elif damage == "identity":
        events[-1]["run_id"] = "other"
    elif damage == "missing_terminal":
        events.pop()
    else:
        events[1]["usage"] = None
    path.write_text("\n".join(json.dumps(row) for row in events) + "\n")
    result = recover_usage(path, INSTANCE["instance_id"], Model.model)
    assert result["usage_status"] != "recorded" and result["cost"] is None


def test_missing_result_on_resume_keeps_planned_denominator_unknown(tmp_path, monkeypatch):
    instances = [INSTANCE, {**INSTANCE, "instance_id": "example__repo-2"}]
    monkeypatch.setattr(evaluation, "solve", lambda instance, *args, **kwargs: (InstanceResult(instance["instance_id"], "agent", cost=0.1), "diff\n"))
    monkeypatch.setattr(evaluation, "image_present", lambda *args: True)
    monkeypatch.setattr(evaluation, "judge_predictions", lambda path, ids, *args: ({identifier: {"resolved": True, "patch_applied": True} for identifier in ids}, "grade"))
    evaluation.evaluate(instances, Model(), "resume", tmp_path)
    path = tmp_path / "results.jsonl"
    path.write_text(path.read_text().splitlines()[0] + "\n")
    monkeypatch.setattr(evaluation, "solve", lambda *args, **kwargs: pytest.fail("must not silently rerun"))
    summary = evaluation.evaluate(instances, Model(), "resume", tmp_path)
    assert summary["planned_instances"] == 2 and summary["judged"] == 1
    assert summary["requires_review"] and summary["resolve_rate"] is None


@pytest.mark.parametrize("failure", [RuntimeError("write failed"), KeyboardInterrupt()])
def test_single_shot_saves_usage_before_patch_operations(tmp_path, monkeypatch, failure):
    class SingleModel(Model):
        def chat_raw(self, messages):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="--- a/a.py\n+++ b/a.py\n"))],
                                   usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
    class BrokenRunner(Runner):
        def put(self, path, text):
            raise failure
    setup_runner(monkeypatch, BrokenRunner())
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(evaluation.SolveInterrupted) as caught:
            evaluation.solve(INSTANCE, SingleModel(), "single-shot", 1, 10, tmp_path)
        result = caught.value.result
    else:
        result, _ = evaluation.solve(INSTANCE, SingleModel(), "single-shot", 1, 10, tmp_path)
    assert result.prompt_tokens == 100 and result.completion_tokens == 20
    assert result.known_cost > 0 and result.cost is None


def test_single_shot_missing_usage_fields_are_unknown(tmp_path, monkeypatch):
    class SingleModel(Model):
        def chat_raw(self, messages):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="no patch"))], usage=SimpleNamespace())
    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, SingleModel(), "single-shot", 1, 10, tmp_path)
    assert result.usage_status == "incomplete" and result.cost is None


def test_bad_tool_response_does_not_drop_usage(tmp_path, monkeypatch):
    class BadModel(Model):
        def chat(self, messages, tools):
            return SimpleNamespace(content="bad", tool_calls=[SimpleNamespace(id="bad", function=None)]), SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    setup_runner(monkeypatch)
    result, _ = evaluation.solve(INSTANCE, BadModel(), "agent", 3, 10, tmp_path)
    assert result.error and result.prompt_tokens == 100 and result.completion_tokens == 20
    assert result.known_cost > 0 and result.cost is None


@pytest.mark.parametrize("http_attempts", [0, 3])
def test_http_counts_must_agree_with_recorded_response_usage(tmp_path, monkeypatch, http_attempts):
    from patchloop.eval_usage import recover_usage

    setup_runner(monkeypatch)
    evaluation.solve(INSTANCE, Model(), "agent", 3, 10, tmp_path)
    path = tmp_path / f"{INSTANCE['instance_id']}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[-1]["http_attempts"] = http_attempts
    path.write_text("\n".join(json.dumps(row) for row in events) + "\n")
    recovered = recover_usage(path, INSTANCE["instance_id"], Model.model)
    assert recovered["usage_status"] == "invalid" and recovered["cost"] is None


def test_trajectory_finalization_interrupt_keeps_known_estimate_but_not_total(tmp_path, monkeypatch):
    import patchloop.loop as loop

    original = loop.Trajectory.close
    def interrupted(self, result):
        original(self, result)
        raise KeyboardInterrupt()
    monkeypatch.setattr(loop.Trajectory, "close", interrupted)
    with pytest.raises(loop.RunInterrupted) as caught:
        loop.run("test", Model(), Runner(), traj_dir=tmp_path, capture_errors=True)
    assert caught.value.result.cost is None and caught.value.result.known_cost > 0
