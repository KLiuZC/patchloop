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
