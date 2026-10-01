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

    def chat(self, messages, tools):
        self.requests.append([dict(m) for m in messages])
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


def test_patch_is_collected_and_saved(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    llm = FakeLLM([
        message(tool_calls=[tool_call("c1", '{"cmd": "echo two >> f.txt"}')]),
        message(content="done"),
    ])
    result = run("task", llm, LocalRunner(tmp_path), traj_dir=tmp_path / "traj")
    assert "+two" in result.patch
    assert (tmp_path / "traj" / f"{result.run_id}.patch").read_text() == result.patch


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
