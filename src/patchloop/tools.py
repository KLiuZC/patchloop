import os
import signal
import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 4000

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command in the working directory and return its stdout and stderr. "
            "Long output is truncated in the middle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The command to run."},
            },
            "required": ["cmd"],
        },
    },
}

TOOLS = [BASH_TOOL]


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n...[{omitted} chars omitted]...\n{text[-half:]}"


def bash(cmd: str, workdir: Path, timeout: int = 60) -> str:
    # own process group, so a timeout kills the whole pipeline rather than just the shell
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        out = (out or "") + f"\n[command timed out after {timeout}s]"
    else:
        if proc.returncode != 0:
            out += f"\n[exit code {proc.returncode}]"
    return truncate(out) or "[no output]"
