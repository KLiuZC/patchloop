SYSTEM_PROMPT = """\
You are an autonomous software engineer working in a {environment}.
The repository is at {workdir}, which is already the current directory.

You act only through the `bash` tool. Rules:
- Run one command per turn and read its output before deciding the next step.
- Long output is truncated in the middle. Locate what you need with grep, head or sed -n before reading whole files.
- Make small, targeted edits (sed, a python one-liner, or a heredoc). Do not rewrite whole files unless necessary.
- After every edit, rerun the relevant tests to confirm the fix.
- Do not modify files under tests/ unless the task explicitly asks for it.
- The environment is already set up: do not install packages or access the network.
- You have a budget of {max_turns} commands in total. Spend them on making and verifying a fix, not on exhaustive exploration.
- When the task is done, stop calling tools and reply with one sentence describing the change.
"""

TASK_TEMPLATE = """\
Fix the following issue in the {repo} repository.

<issue>
{problem_statement}
</issue>

Guidance:
- Locate the relevant code first; reproduce the problem with a short script if that helps.
- Keep the fix minimal and in the library code. Do not modify existing tests: the fix is verified with hidden tests.
- The fix is not in the repository, its git history or anywhere else on this machine: write it yourself. Any reasonable fix that resolves the issue is acceptable; you do not need to match a particular upstream change.
- Run the existing tests around the change to make sure nothing else breaks.
- Delete any scratch files you created before finishing.
"""

SINGLE_SHOT_PROMPT = """\
Below is an issue from the {repo} repository. Write the fix as a single unified diff in git format \
(`--- a/path` / `+++ b/path`, paths relative to the repository root). You cannot look at the code, \
so rely on your knowledge of the project. Reply with only the diff inside a ```diff code block.

<issue>
{problem_statement}
</issue>
"""

BUDGET_REMINDER = """\
Budget reminder: {remaining} model calls remain, including the next response.
Prioritize implementing a targeted fix and running relevant tests with the remaining calls.
If the task is complete, stop calling tools and summarize the change. Do not claim tests passed unless you ran them.
"""


FINISH_REQUIRED = (
    "A text reply does not submit the task. If your work is complete, call `finish` alone "
    "with a non-empty `summary`; otherwise continue using `bash`. The original call budget still applies."
)


def build_system_prompt(environment: str, workdir: str, max_turns: int, explicit_finish: bool = False) -> str:
    template = SYSTEM_PROMPT
    if explicit_finish:
        template = template.replace(
            "You act only through the `bash` tool. Rules:",
            "Use `bash` to work on the repository and `finish` to submit the task. Rules:",
        ).replace(
            "When the task is done, stop calling tools and reply with one sentence describing the change.",
            "When the task is done, call `finish` alone with a non-empty `summary` describing the change. "
            "Do not combine `finish` with other tools in the same response. A text reply does not end the task.",
        )
    return template.format(environment=environment, workdir=workdir, max_turns=max_turns)


def budget_reminder(turn: int, max_turns: int, explicit_finish: bool = False) -> dict | None:
    """Warn once at 20 and 5 remaining calls, only after some budget has been used."""
    remaining = max_turns - turn + 1
    if turn <= 1 or remaining not in (20, 5):
        return None
    content = BUDGET_REMINDER.format(remaining=remaining)
    if explicit_finish:
        content = content.replace(
            "If the task is complete, stop calling tools and summarize the change.",
            "If the task is complete, call `finish` alone with a summary of the change.",
        )
    return {"role": "user", "content": content}
