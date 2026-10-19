import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .llm import LLM, estimate_cost
from .eval_usage import normalize_usage
from .command_runtime import COUNT_FIELDS, CommandPolicy, count_policy_event
from .prompts import FINISH_REQUIRED, build_system_prompt, budget_reminder
from .tools import FINISH_TOOL, TOOLS, bash
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
    stop_reason: str = "max_turns"
    error: str = ""
    command_policy_counts: dict[str, int] = field(default_factory=dict)
    model_calls_attempted: int = 0
    http_attempts: int | None = 0
    unreported_http_attempts: int = 0
    missing_usage: bool = False
    known_cost: float | None = None


class RunInterrupted(KeyboardInterrupt):
    def __init__(self, result: RunResult):
        super().__init__("run interrupted")
        self.result = result


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
    explicit_finish: bool = False,
    capture_errors: bool = False,
    command_policy: CommandPolicy | None = None,
    on_policy: Callable[[dict], None] | None = None,
) -> RunResult:
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_started = time.time()
    result = RunResult(run_id=run_id)
    if command_policy is not None and command_policy.mode != "off":
        if command_policy.context.cwd != (str(runner.workdir).rstrip("/") or "/"):
            raise ValueError("command policy cwd must match the runner workdir")
        result.command_policy_counts = dict.fromkeys(COUNT_FIELDS, 0)
    traj_dir = Path(traj_dir)
    system_prompt = build_system_prompt(runner.environment, runner.workdir, max_turns, explicit_finish)
    tools = [*TOOLS, FINISH_TOOL] if explicit_finish else TOOLS
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
            "explicit_finish": explicit_finish,
            "system_prompt": system_prompt,
            "command_policy": command_policy.metadata() if command_policy else {"mode": "off"},
        },
    )
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {"role": "user", "content": task},
    ]

    failure = None
    try:
        for turn in range(1, max_turns + 1):
            started = time.time()
            reminder = budget_reminder(turn, max_turns, explicit_finish) if budget_reminders else None
            if reminder:
                # All previous tool results are already in messages; never split a tool-call group.
                messages.append(reminder)
                traj.budget_reminder(turn, max_turns - turn + 1, reminder)
                result.budget_reminders_sent += 1
            result.model_calls_attempted += 1
            usage = None
            try:
                msg, usage = llm.chat(messages, tools)
            except (Exception, KeyboardInterrupt):
                usage = getattr(llm, "last_usage", None)
                if getattr(llm, "last_response_received", False):
                    result.turns = turn
                    turn_usage = _accumulate_usage(result, usage)
                    traj.turn(turn=turn, assistant={"role": "assistant", "content": ""}, tools=[],
                              usage=turn_usage, error="model response decoding failed",
                              seconds=round(time.time() - started, 2))
                raise
            finally:
                attempts = getattr(llm, "last_http_attempts", None)
                if type(attempts) is int and attempts >= 0:
                    if result.http_attempts is not None:
                        result.http_attempts += attempts
                    result.unreported_http_attempts += max(0, attempts - int(normalize_usage(usage) is not None))
                else:
                    result.http_attempts = None
            result.turns = turn
            turn_usage = _accumulate_usage(result, usage)
            try:
                assistant = _assistant_message(msg)
            except (Exception, KeyboardInterrupt):
                traj.turn(turn=turn, assistant={"role": "assistant", "content": ""}, tools=[],
                          usage=turn_usage, error="assistant message normalization failed",
                          seconds=round(time.time() - started, 2))
                raise
            messages.append(assistant)
            tool_records = []
            calls = msg.tool_calls or []
            submission = None
            def record_policy(event):
                traj.command_policy(turn=turn, **event)
                count_policy_event(result.command_policy_counts, event)
                if on_policy:
                    on_policy({"turn": turn, **event})
            try:
                for call in calls:
                    if explicit_finish and call.function.name == "finish":
                        if len(calls) != 1:
                            output = "finish was not accepted: call it alone after reading all other tool results."
                        else:
                            try:
                                submission = _finish_summary(call.function.arguments)
                            except ValueError as e:
                                output = f"invalid finish arguments: {e}"
                            else:
                                output = "Submission accepted. Evaluation has not been performed by this tool."
                    else:
                        output = _execute(call, runner, timeout, command_policy=command_policy, on_policy=record_policy)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                    tool_records.append(
                        {
                            "id": call.id,
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                            "output": output,
                        }
                    )

            finally:
                traj.turn(
                    turn=turn,
                    assistant=assistant,
                    tools=tool_records,
                    usage=turn_usage,
                    seconds=round(time.time() - started, 2),
                )
            if on_turn:
                on_turn(turn, assistant, tool_records)

            if submission is not None:
                result.finished = True
                result.final_message = submission
                result.stop_reason = "finish"
                break
            if not calls:
                if not explicit_finish:
                    result.finished = True
                    result.final_message = msg.content or ""
                    result.stop_reason = "no_tool_calls"
                    break
                if turn < max_turns:
                    feedback = {"role": "user", "content": FINISH_REQUIRED}
                    messages.append(feedback)
                    traj.finish_required(turn + 1, feedback)

    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
        result.finished = False
        result.stop_reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        result.error = f"{type(exc).__name__}: {exc}"

    # Repair callers own disposable workspaces and opt into snapshots on error.
    # Legacy callers may use the user's index: preserve it on a failed request.
    if failure is None or capture_errors:
        try:
            result.patch = runner.diff()
            if result.patch:
                (traj_dir / f"{run_id}.patch").write_text(result.patch, encoding="utf-8")
        except KeyboardInterrupt as exc:
            failure = exc
            result.finished = False
            result.stop_reason = "interrupted"
            result.error += ("; " if result.error else "") + "patch snapshot interrupted"
            if capture_errors:
                # The caller still owns a live disposable checkout. Retry once
                # before cleanup, and retain received usage even if retry fails.
                try:
                    result.patch = runner.diff()
                    if result.patch:
                        (traj_dir / f"{run_id}.patch").write_text(result.patch, encoding="utf-8")
                except (Exception, KeyboardInterrupt) as retry_error:
                    result.error += f"; patch snapshot retry failed: {type(retry_error).__name__}: {retry_error}"
        except Exception as exc:
            failure = failure or exc
            result.finished = False
            result.stop_reason = "error"
            result.error += ("; " if result.error else "") + f"patch snapshot failed: {type(exc).__name__}: {exc}"
    result.seconds = round(time.time() - run_started, 1)
    result.known_cost = estimate_cost(llm.model, result.prompt_tokens, result.cached_tokens, result.completion_tokens)
    result.cost = result.known_cost if not result.missing_usage and not result.unreported_http_attempts and failure is None else None
    try:
        traj.close(result)
    except KeyboardInterrupt as exc:
        result.finished = False
        result.stop_reason = "interrupted"
        result.error += ("; " if result.error else "") + "trajectory finalization interrupted"
        result.cost = None
        if capture_errors:
            raise RunInterrupted(result) from exc
        raise
    if isinstance(failure, KeyboardInterrupt) and capture_errors:
        raise RunInterrupted(result) from failure
    if failure is not None and not capture_errors:
        raise failure
    return result


def _accumulate_usage(result, usage):
    counts = normalize_usage(usage)
    if counts is not None:
        for name, value in counts.items():
            setattr(result, name, getattr(result, name) + value)
    else:
        result.missing_usage = True
    return counts


def _finish_summary(arguments: str) -> str:
    try:
        args = json.loads(arguments)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("expected JSON with a non-empty string field 'summary'") from e
    if not isinstance(args, dict) or set(args) != {"summary"}:
        raise ValueError("expected exactly one field: 'summary'")
    summary = args["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("'summary' must be a non-empty string")
    return summary.strip()


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


def _execute(call, runner, timeout: int, *, command_policy=None, on_policy=None) -> str:
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
    if command_policy is not None:
        event = command_policy.inspect(cmd, call.id)
        if event is not None:
            if on_policy:
                on_policy(event)
            if event["action"] == "block":
                return command_policy.rejection(event)
    return bash(cmd, runner, timeout=timeout)
