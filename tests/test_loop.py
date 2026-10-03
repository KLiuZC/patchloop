import json
from types import SimpleNamespace

import pytest

from patchloop.loop import run
from patchloop.sandbox import LocalRunner


class FakeLLM:
    provider = "fake"
    model = "fake"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.tool_definitions = []

    def chat(self, messages, tools):
        self.requests.append([dict(m) for m in messages])
        self.tool_definitions.append(list(tools))
        return next(self.replies), SimpleNamespace(prompt_tokens=10, completion_tokens=5)


def message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def tool_call(call_id, arguments, name="bash"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_tool_output_is_fed_back_and_run_finishes(tmp_path):
    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", '{"cmd": "echo hi"}')]),
        message(content="done"),
    ])
    result = run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")

    assert result.finished
    assert result.turns == 2
    assert result.final_message == "done"
    tool_msg = llm.requests[1][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "c1"
    assert "hi" in tool_msg["content"]


def test_bad_arguments_are_reported_to_the_model(tmp_path):
    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", "{not json")]),
        message(content="ok"),
    ])
    run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")
    assert "invalid arguments" in llm.requests[1][-1]["content"]


def test_max_turns_stops_the_run(tmp_path):
    llm = FakeLLM([message(tool_calls=[tool_call(f"c{i}", '{"cmd": "true"}')]) for i in range(5)])
    result = run("task", llm, LocalRunner(tmp_path), max_turns=3, traj_dir=tmp_path / "traj")
    assert not result.finished
    assert result.turns == 3


def test_trajectory_has_meta_turns_and_result(tmp_path):
    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", '{"cmd": "true"}')]),
        message(content="done"),
    ])
    result = run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")
    lines = [json.loads(l) for l in (tmp_path / "traj" / f"{result.run_id}.jsonl").read_text().splitlines()]
    assert [l["type"] for l in lines] == ["meta", "turn", "turn", "result"]
    assert lines[0]["task"] == "task"
    assert lines[-1]["prompt_tokens"] == 20


def test_reasoning_content_is_echoed_back(tmp_path):
    first = message(tool_calls=[tool_call("c1", '{"cmd": "true"}')])
    first.reasoning_content = "think"
    llm = FakeLLM([first, message(content="ok")])
    run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")
    assistant = llm.requests[1][-2]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "think"


@pytest.mark.parametrize("explicit_finish", [False, True])
def test_patch_is_collected_and_saved(tmp_path, explicit_finish):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    last = message(tool_calls=[tool_call("end", '{"summary": "Added two"}', name="finish")]) if explicit_finish else message(content="done")
    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", '{"cmd": "echo two >> f.txt"}')]),
        last,
    ])
    result = run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj", explicit_finish=explicit_finish)
    assert "+two" in result.patch
    assert (tmp_path / "traj" / f"{result.run_id}.patch").read_text() == result.patch
    assert result.finished and result.stop_reason == ("finish" if explicit_finish else "no_tool_calls")


def test_run_id_names_the_trajectory_and_cache_hits_are_counted(tmp_path):
    class Usage:
        prompt_tokens = 100
        completion_tokens = 5
        prompt_cache_hit_tokens = 60

    class CachingLLM(FakeLLM):
        def chat(self, messages, tools):
            self.requests.append([dict(m) for m in messages])
            return next(self.replies), Usage()

    llm = CachingLLM([message(content="done")])
    result = run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj", run_id="my-instance")
    assert result.run_id == "my-instance"
    assert (tmp_path / "traj" / "my-instance.jsonl").exists()
    assert result.prompt_tokens == 100 and result.cached_tokens == 60


def test_command_key_is_accepted_as_alias_for_cmd(tmp_path):
    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", '{"command": "echo aliased"}')]),
        message(content="ok"),
    ])
    run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")
    assert "aliased" in llm.requests[1][-1]["content"]


class MemoryRunner(LocalRunner):
    def run(self, cmd, timeout):
        return "tool completed"

    def diff(self):
        return ""


def repeated_calls(count):
    # Two tool calls per response verify that budget is counted by model calls.
    return [message(tool_calls=[
        tool_call(f"{i}-a", '{"cmd": "true"}'),
        tool_call(f"{i}-b", '{"cmd": "true"}'),
    ]) for i in range(count)]


def test_reminders_follow_all_tool_results_at_exact_turns(tmp_path):
    llm = FakeLLM(repeated_calls(40))
    result = run("task", llm, MemoryRunner(tmp_path), max_turns=40,
                 traj_dir=tmp_path, run_id="enabled", budget_reminders=True)
    assert result.turns == 40 and not result.finished
    assert result.budget_reminders_sent == 2
    assert [i + 1 for i, req in enumerate(llm.requests) if req[-1]["role"] == "user"] == [1, 21, 36]
    for turn, remaining in [(21, 20), (36, 5)]:
        req = llm.requests[turn - 1]
        assert f"{remaining} model calls remain" in req[-1]["content"]
        assert [m["role"] for m in req[-4:]] == ["assistant", "tool", "tool", "user"]
        assert {m["tool_call_id"] for m in req[-3:-1]} == {c["id"] for c in req[-4]["tool_calls"]}
    rows = [json.loads(l) for l in (tmp_path / "enabled.jsonl").read_text().splitlines()]
    assert rows[0]["budget_reminders"] is True
    reminders = [r for r in rows if r["type"] == "budget_reminder"]
    assert [(r["turn"], r["remaining"]) for r in reminders] == [(21, 20), (36, 5)]
    for r in reminders:
        i = rows.index(r)
        assert rows[i - 1]["turn"] == r["turn"] - 1
        assert rows[i + 1]["type"] == "turn" and rows[i + 1]["turn"] == r["turn"]
        assert r["message"] == llm.requests[r["turn"] - 1][-1]


def test_default_off_preserves_original_conversation(tmp_path):
    implicit = FakeLLM(repeated_calls(40))
    explicit = FakeLLM(repeated_calls(40))
    for name, llm, options in [("implicit", implicit, {}), ("explicit", explicit, {"budget_reminders": False})]:
        result = run("task", llm, MemoryRunner(tmp_path), max_turns=40, traj_dir=tmp_path, run_id=name, **options)
        assert result.budget_reminders_sent == 0
        assert all(m["role"] != "user" for req in llm.requests for m in req[2:])
        rows = [json.loads(l) for l in (tmp_path / f"{name}.jsonl").read_text().splitlines()]
        assert all(r["type"] != "budget_reminder" for r in rows)
    assert implicit.requests == explicit.requests


def test_early_finish_does_not_trigger_later_reminder(tmp_path):
    llm = FakeLLM([*repeated_calls(19), message(content="done")])
    result = run("task", llm, MemoryRunner(tmp_path), max_turns=40,
                 traj_dir=tmp_path, budget_reminders=True)
    assert result.finished and result.turns == 20 and result.budget_reminders_sent == 0


@pytest.mark.parametrize("limit,turns", [(30, [11, 26]), (20, [16]), (6, [2]), (5, []), (4, [])])
def test_reminders_with_other_budgets(tmp_path, limit, turns):
    llm = FakeLLM(repeated_calls(limit))
    run("task", llm, MemoryRunner(tmp_path), max_turns=limit, traj_dir=tmp_path, budget_reminders=True)
    assert [i + 1 for i, req in enumerate(llm.requests) if i > 0 and req[-1]["role"] == "user"] == turns


def test_reminder_is_logged_before_a_failed_request(tmp_path):
    llm = FakeLLM(repeated_calls(20))
    with pytest.raises(StopIteration):
        run("task", llm, MemoryRunner(tmp_path), max_turns=40,
            traj_dir=tmp_path, run_id="interrupted", budget_reminders=True)
    rows = [json.loads(l) for l in (tmp_path / "interrupted.jsonl").read_text().splitlines()]
    assert rows[-1]["type"] == "budget_reminder" and rows[-1]["turn"] == 21


def finish_message(summary="Completed task"):
    return message(tool_calls=[tool_call("end", json.dumps({"summary": summary}), name="finish")])


def test_finish_submits_summary_without_running_a_command(tmp_path):
    class NoCommandRunner(MemoryRunner):
        def run(self, cmd, timeout):
            pytest.fail("finish must not execute a shell command")

    llm = FakeLLM([finish_message("  No changes needed  ")])
    result = run("task", llm, NoCommandRunner(tmp_path), explicit_finish=True, traj_dir=tmp_path, run_id="finish")
    assert result.finished and result.turns == 1 and result.stop_reason == "finish"
    assert result.final_message == "No changes needed" and result.patch == ""
    assert [t["function"]["name"] for t in llm.tool_definitions[0]] == ["bash", "finish"]
    rows = [json.loads(l) for l in (tmp_path / "finish.jsonl").read_text().splitlines()]
    assert rows[0]["explicit_finish"] is True
    assert rows[1]["tools"][0]["id"] == "end"
    assert "Submission accepted" in rows[1]["tools"][0]["output"]
    assert rows[-1]["final_message"] == "No changes needed"
    assert rows[-1]["stop_reason"] == "finish"


def test_text_reply_in_finish_mode_gets_feedback_then_can_submit(tmp_path):
    llm = FakeLLM([message(content="Done."), finish_message()])
    result = run("task", llm, MemoryRunner(tmp_path), explicit_finish=True, traj_dir=tmp_path, run_id="feedback")
    assert result.finished and result.turns == 2
    feedback = llm.requests[1][-1]
    assert feedback["role"] == "user" and "does not submit" in feedback["content"]
    rows = [json.loads(l) for l in (tmp_path / "feedback.jsonl").read_text().splitlines()]
    assert [r["type"] for r in rows] == ["meta", "turn", "finish_required", "turn", "result"]
    assert rows[2]["turn"] == 2 and rows[2]["message"] == feedback


def test_text_replies_cannot_bypass_turn_limit_in_finish_mode(tmp_path):
    llm = FakeLLM([message(content="Done.") for _ in range(3)])
    result = run("task", llm, MemoryRunner(tmp_path), explicit_finish=True, max_turns=3, traj_dir=tmp_path)
    assert not result.finished and result.turns == 3 and result.stop_reason == "max_turns"
    assert result.final_message == ""


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null", "{}", '{"summary": 3}', '{"summary": "  "}', '{"summary": "ok", "extra": 1}'])
def test_invalid_finish_does_not_submit_and_model_can_retry(tmp_path, arguments):
    llm = FakeLLM([message(tool_calls=[tool_call("invalid", arguments, "finish")]), finish_message()])
    result = run("task", llm, MemoryRunner(tmp_path), explicit_finish=True, traj_dir=tmp_path)
    assert result.finished and result.turns == 2
    assert llm.requests[1][-1]["tool_call_id"] == "invalid"
    assert "invalid finish arguments" in llm.requests[1][-1]["content"]


@pytest.mark.parametrize("finish_first", [True, False])
def test_finish_cannot_skip_other_calls_in_same_response(tmp_path, finish_first):
    calls = [tool_call("end", '{"summary":"done"}', "finish"), tool_call("work", '{"cmd":"true"}')]
    if not finish_first:
        calls.reverse()
    llm = FakeLLM([message(tool_calls=calls), finish_message()])
    result = run("task", llm, MemoryRunner(tmp_path), explicit_finish=True, traj_dir=tmp_path)
    assert result.finished and result.turns == 2
    outputs = {m["tool_call_id"]: m["content"] for m in llm.requests[1] if m["role"] == "tool"}
    assert set(outputs) == {"end", "work"}
    assert "call it alone" in outputs["end"] and outputs["work"] == "tool completed"


def test_disabled_finish_tool_does_not_change_original_protocol(tmp_path):
    from patchloop.prompts import SYSTEM_PROMPT, BUDGET_REMINDER, budget_reminder
    from patchloop.tools import TOOLS

    llm = FakeLLM([finish_message(), message(content="done")])
    runner = MemoryRunner(tmp_path)
    result = run("task", llm, runner, max_turns=40, traj_dir=tmp_path)
    assert [t["function"]["name"] for t in TOOLS] == ["bash"]
    assert all([t["function"]["name"] for t in tools] == ["bash"] for tools in llm.tool_definitions)
    assert result.stop_reason == "no_tool_calls" and result.turns == 2
    assert "unknown tool" in llm.requests[1][-1]["content"]
    assert llm.requests[0][0]["content"] == SYSTEM_PROMPT.format(environment=runner.environment, workdir=runner.workdir, max_turns=40)
    assert budget_reminder(21, 40)["content"] == BUDGET_REMINDER.format(remaining=20)


def test_budget_reminder_and_system_prompt_agree_on_finish(tmp_path):
    llm = FakeLLM([*repeated_calls(35), finish_message()])
    result = run("task", llm, MemoryRunner(tmp_path), explicit_finish=True,
                 budget_reminders=True, max_turns=40, traj_dir=tmp_path)
    assert result.finished and result.turns == 36 and result.budget_reminders_sent == 2
    assert "call `finish` alone" in llm.requests[0][0]["content"]
    assert "stop calling tools" not in llm.requests[0][0]["content"]
    for turn, remaining in [(21, 20), (36, 5)]:
        reminder = llm.requests[turn - 1][-1]["content"]
        assert f"{remaining} model calls remain" in reminder
        assert "call `finish` alone" in reminder and "stop calling tools" not in reminder
