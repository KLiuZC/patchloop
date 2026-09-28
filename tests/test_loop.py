import json
from types import SimpleNamespace

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
