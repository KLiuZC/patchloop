"""Recover recorded usage without treating missing billing evidence as zero."""

import json
from pathlib import Path

from .llm import estimate_cost


TOKEN_FIELDS = ("prompt_tokens", "cached_tokens", "completion_tokens")


def normalize_usage(usage):
    if usage is None:
        return None
    cached = getattr(usage, "prompt_cache_hit_tokens", None)
    if cached is None:
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)
    try:
        return _counts({"prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "cached_tokens": 0 if cached is None else cached})
    except ValueError:
        return None


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _counts(record):
    _require(isinstance(record, dict), "invalid usage object")
    values = {name: record.get(name) for name in TOKEN_FIELDS}
    _require(all(type(value) is int and value >= 0 for value in values.values()), "invalid token counters")
    _require(values["cached_tokens"] <= values["prompt_tokens"], "cached tokens exceed prompt tokens")
    return values


def recover_usage(path: Path, instance_id: str, model: str) -> dict:
    try:
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        _require(events and all(isinstance(event, dict) for event in events), "invalid trajectory")
        _require(events[0].get("type") == "meta" and events[0].get("model") == model, "trajectory metadata mismatch")
        terminal = events[-1] if events[-1].get("type") == "result" else None
        _require(not any(event.get("type") in {"meta", "result"} for event in events[1:-1]), "duplicate trajectory boundaries")
        turns = [event for event in events if event.get("type") == "turn"]
        totals = dict.fromkeys(TOKEN_FIELDS, 0)
        missing = 0
        for number, turn in enumerate(turns, 1):
            _require(type(turn.get("turn")) is int and turn["turn"] == number, "missing or duplicate trajectory turn")
            if turn.get("usage") is None:
                missing += 1
                continue
            for name, value in _counts(turn["usage"]).items():
                totals[name] += value
        if terminal is not None:
            _require(terminal.get("run_id") == instance_id, "trajectory instance mismatch")
            _require(type(terminal.get("turns")) is int and terminal["turns"] == len(turns), "terminal turn count mismatch")
            _require(_counts(terminal) == totals, "terminal and turn usage disagree")
        attempted = terminal.get("model_calls_attempted") if terminal else None
        if attempted is not None:
            _require(type(attempted) is int and len(turns) <= attempted <= len(turns) + 1, "invalid attempted model call count")
        unreported = terminal.get("unreported_http_attempts", 0) if terminal else 0
        _require(type(unreported) is int and unreported >= 0, "invalid unreported HTTP attempts")
        http_attempts = terminal.get("http_attempts") if terminal else None
        if http_attempts is not None:
            _require(type(http_attempts) is int and http_attempts >= unreported, "invalid HTTP attempt count")
            _require(http_attempts == len(turns) - missing + unreported, "HTTP attempts and reported responses disagree")
        complete = bool(terminal is not None and not missing and not terminal.get("error") and
                        not unreported and (attempted is None or attempted == len(turns)) and
                        terminal.get("stop_reason") in {"no_tool_calls", "finish", "max_turns"})
        known_cost = estimate_cost(model, **totals)
        return {**totals, "turns": len(turns), "model_calls_attempted": attempted,
                "http_attempts": terminal.get("http_attempts") if terminal else None,
                "unreported_http_attempts": unreported, "usage_status": "recorded" if complete else "incomplete",
                "usage_error": "" if complete else "Trajectory has missing usage or an interrupted/error request; billing requires review.",
                "known_cost": known_cost, "cost": known_cost if complete else None}
    except (OSError, ValueError, TypeError) as exc:
        return {"usage_status": "invalid", "usage_error": str(exc), "known_cost": None, "cost": None}
