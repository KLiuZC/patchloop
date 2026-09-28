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


def bash(cmd: str, runner, timeout: int = 60) -> str:
    return truncate(runner.run(cmd, timeout)) or "[no output]"
