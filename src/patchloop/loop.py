import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .llm import LLM
from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, bash
from .trajectory import Trajectory


@dataclass
class RunResult:
    run_id: str
    turns: int = 0
    finished: bool = False  # the model stopped on its own rather than hitting max_turns
    final_message: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    patch: str = ""


def run(
    task: str,
    llm: LLM,
    runner,
    max_turns: int = 30,
    timeout: int = 60,
    traj_dir: Path = Path("traj"),
    on_turn: Callable[[int, dict, list[dict]], None] | None = None,
) -> RunResult:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    result = RunResult(run_id=run_id)
    traj_dir = Path(traj_dir)
    traj = Trajectory(
        traj_dir / f"{run_id}.jsonl",
        meta={
            "task": task,
            **runner.describe(),
            "provider": llm.provider,
            "model": llm.model,
            "max_turns": max_turns,
        },
    )
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(environment=runner.environment, workdir=runner.workdir),
        },
        {"role": "user", "content": task},
    ]

    for turn in range(1, max_turns + 1):
        started = time.time()
        msg, usage = llm.chat(messages, TOOLS)
        assistant = _assistant_message(msg)
        messages.append(assistant)

        tool_records = []
        for call in msg.tool_calls or []:
            output = _execute(call, runner, timeout)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            tool_records.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                    "output": output,
                }
            )

        result.turns = turn
        if usage:
            result.prompt_tokens += usage.prompt_tokens
            result.completion_tokens += usage.completion_tokens
        traj.turn(
            turn=turn,
            assistant=assistant,
            tools=tool_records,
            usage=(
                {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens}
                if usage
                else None
            ),
            seconds=round(time.time() - started, 2),
        )
        if on_turn:
            on_turn(turn, assistant, tool_records)

        if not msg.tool_calls:
            result.finished = True
            result.final_message = msg.content or ""
            break

    result.patch = runner.diff()
    if result.patch:
        (traj_dir / f"{run_id}.patch").write_text(result.patch, encoding="utf-8")
    traj.close(result)
    return result


def _assistant_message(msg) -> dict:
    """Normalise the SDK message into the plain dict the next request (and the log) needs."""
    out = {"role": "assistant", "content": msg.content or ""}
    # thinking models put their chain of thought here; DeepSeek rejects later
    # tool-calling requests with a 400 unless every earlier turn's copy is sent back
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        out["reasoning_content"] = reasoning
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in msg.tool_calls
        ]
    return out


def _execute(call, runner, timeout: int) -> str:
    if call.function.name != "bash":
        return f"unknown tool {call.function.name!r}; only `bash` is available"
    try:
        cmd = json.loads(call.function.arguments)["cmd"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # fed back to the model so it can retry with well-formed arguments
        return f"invalid arguments ({e}); expected a JSON object with a string field 'cmd'"
    if not isinstance(cmd, str):
        return "invalid arguments: 'cmd' must be a string"
    return bash(cmd, runner, timeout=timeout)
