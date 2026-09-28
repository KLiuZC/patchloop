import json
import re
from pathlib import Path

from .llm import LLM
from .prompts import SINGLE_SHOT_PROMPT

_FENCE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.DOTALL)


def extract_diff(text: str) -> str:
    """Pull a unified diff out of a model reply: a fenced block first, a bare diff second."""
    for block in _FENCE.findall(text):
        if "--- " in block and "+++ " in block:
            return block if block.endswith("\n") else block + "\n"
    start = text.find("diff --git")
    if start < 0:
        start = text.find("--- a/")
    return text[start:].rstrip() + "\n" if start >= 0 else ""


def single_shot(instance: dict, llm: LLM, runner, record_path: Path | None = None) -> tuple[str, str, object]:
    """One model call and no tools: the model writes a diff blind, which is then applied in the sandbox.

    Returns (patch, note, usage); note is empty on success and otherwise says what went wrong.
    The patch is re-read from git so it has the same shape as agent patches.
    """
    prompt = SINGLE_SHOT_PROMPT.format(repo=instance["repo"], problem_statement=instance["problem_statement"].strip())
    resp = llm.chat_raw([{"role": "user", "content": prompt}])
    choice = resp.choices[0]
    content = choice.message.content or ""
    diff = extract_diff(content)
    note = ""
    if choice.finish_reason == "length":
        note = "reply truncated at the output limit"
    elif not diff:
        note = "no diff in reply"

    patch, applied = "", None
    if diff:
        runner.put("/tmp/model.patch", diff)
        out = runner.run("git apply --whitespace=nowarn /tmp/model.patch", timeout=60)
        applied = "[exit code" not in out
        if applied:
            patch = runner.diff()
        elif not note:
            note = "patch did not apply"

    if record_path:
        record_path.write_text(json.dumps({
            "instance_id": instance["instance_id"],
            "finish_reason": choice.finish_reason,
            "reasoning_chars": len(getattr(choice.message, "reasoning_content", None) or ""),
            "content_chars": len(content),
            "diff_found": bool(diff),
            "applied": applied,
            "note": note,
            "content": content,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return patch, note, resp.usage
