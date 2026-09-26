import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape

from .llm import LLM, PROVIDERS
from .loop import run

# no locals in tracebacks: they would include the API key
app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()


@app.callback()
def main() -> None:
    """A minimal coding agent."""


@app.command("run")
def run_command(
    task: str = typer.Option(..., "--task", "-t", help="What the agent should do."),
    workdir: Path = typer.Option(Path("."), "--workdir", "-w", help="Repository the agent works in."),
    provider: str = typer.Option("glm", "--provider", "-p", help="One of: " + ", ".join(PROVIDERS)),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the provider's default model."),
    max_turns: int = typer.Option(30, "--max-turns", help="Stop after this many model calls."),
    timeout: int = typer.Option(60, "--timeout", help="Per-command timeout in seconds."),
):
    """Run the agent on a task inside a repository."""
    load_dotenv()
    if not workdir.is_dir():
        raise typer.BadParameter(f"{workdir} is not a directory", param_hint="--workdir")
    try:
        llm = LLM(provider, model)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)

    def show(turn: int, assistant: dict, tools: list[dict]) -> None:
        if assistant["content"]:
            console.print(f"[dim]{escape(assistant['content'])}[/dim]")
        for tool in tools:
            console.print(f"[bold cyan]\\[{turn}][/] $ {escape(_cmd_of(tool['arguments']))}")
            console.print(escape(tool["output"]), style="dim")

    result = run(task, workdir, llm, max_turns=max_turns, timeout=timeout, on_turn=show)

    status = "[green]finished[/]" if result.finished else f"[red]stopped at max_turns={max_turns}[/]"
    console.print(
        f"\n{status} after {result.turns} turns · "
        f"{result.prompt_tokens} prompt / {result.completion_tokens} completion tokens · "
        f"traj/{result.run_id}.jsonl"
    )
    raise typer.Exit(code=0 if result.finished else 1)


def _cmd_of(arguments: str) -> str:
    try:
        return json.loads(arguments)["cmd"]
    except Exception:
        return arguments
