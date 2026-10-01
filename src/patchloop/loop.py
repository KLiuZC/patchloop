import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .llm import LLM, estimate_cost
from .prompts import SYSTEM_PROMPT, budget_reminder
from .tools import TOOLS, bash
from .trajectory import Trajectory


@dataclass
class RunResult:
    run_id: str
    turns: int = 0
    finished: bool = False  # the model stopped on its own rather than hitting max_turns
    final_message: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    seconds: float = 0.0
    patch: str = ""
    budget_reminders_sent: int = 0


def cached_tokens_of(usage) -> int:
    """Prompt tokens served from the provider's cache; the field name differs per provider."""
    hit = getattr(usage, "prompt_cache_hit_tokens", None)  # DeepSeek
    if hit is None:
        details = getattr(usage, "prompt_tokens_details", None)  # OpenAI-style, e.g. GLM
        hit = getattr(details, "cached_tokens", None) if details else None
    return int(hit or 0)


def run(
    task: str,
    llm: LLM,
    runner,
    max_turns: int = 30,
    timeout: int = 60,
    traj_dir: Path = Path("traj"),
    on_turn: Callable[[int, dict, list[dict]], None] | None = None,
    run_id: str | None = None,
    budget_reminders: bool = False,
) -> RunResult:
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_started = time.time()
    result = RunResult(run_id=run_id)
    traj_dir = Path(traj_dir)
    system_prompt = SYSTEM_PROMPT.format(environment=runner.environment, workdir=runner.workdir, max_turns=max_turns)
    traj = Trajectory(
        traj_dir / f"{run_id}.jsonl",
        meta={
            "task": task,
            **runner.describe(),
            "provider": llm.provider,
            "model": llm.model,
            "max_turns": max_turns,
            "timeout": timeout,
            "budget_reminders": budget_reminders,
            "system_prompt": system_prompt,
        },
    )
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {"role": "user", "content": task},
    ]

    for turn in range(1, max_turns + 1):
        started = time.time()
        reminder = budget_reminder(turn, max_turns) if budget_reminders else None
        if reminder:
            # All previous tool results are already in messages; never split a tool-call group.
            messages.append(reminder)
            traj.budget_reminder(turn, max_turns - turn + 1, reminder)
            result.budget_reminders_sent += 1
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
        turn_usage = None
        if usage:
            cached = cached_tokens_of(usage)
            result.prompt_tokens += usage.prompt_tokens
            result.cached_tokens += cached
            result.completion_tokens += usage.completion_tokens
            turn_usage = {
                "prompt_tokens": usage.prompt_tokens,
                "cached_tokens": cached,
                "completion_tokens": usage.completion_tokens,
            }
        traj.turn(
            turn=turn,
            assistant=assistant,
            tools=tool_records,
            usage=turn_usage,
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
    result.seconds = round(time.time() - run_started, 1)
    result.cost = estimate_cost(llm.model, result.prompt_tokens, result.cached_tokens, result.completion_tokens)
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
        args = json.loads(call.function.arguments)
        cmd = args.get("cmd", args.get("command"))  # models drift to "command" now and then
        if cmd is None:
            raise KeyError("cmd")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        # fed back to the model so it can retry with well-formed arguments
        return f"invalid arguments ({e}); expected a JSON object with a string field 'cmd'"
    if not isinstance(cmd, str):
        return "invalid arguments: 'cmd' must be a string"
    return bash(cmd, runner, timeout=timeout)
