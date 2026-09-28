import re

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


def single_shot(instance: dict, llm: LLM, runner) -> tuple[str, bool, object]:
    """One model call and no tools: the model writes a diff blind, which is then applied in the sandbox.

    Returns (patch, applied, usage). The patch is re-read from git so it has the same shape as agent patches.
    """
    prompt = SINGLE_SHOT_PROMPT.format(repo=instance["repo"], problem_statement=instance["problem_statement"].strip())
    msg, usage = llm.chat([{"role": "user", "content": prompt}])
    diff = extract_diff(msg.content or "")
    if not diff:
        return "", False, usage
    runner.put("/tmp/model.patch", diff)
    out = runner.run("git apply --whitespace=nowarn /tmp/model.patch", timeout=60)
    if "[exit code" in out:
        return "", False, usage
    return runner.diff(), True, usage
