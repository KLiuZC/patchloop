import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape

from .llm import LLM, PROVIDERS
from .loop import run
from .sandbox import DockerRunner, LocalRunner

# no locals in tracebacks: they would include the API key
app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()


@app.callback()
def main() -> None:
    """A minimal coding agent."""


@app.command("run")
def run_command(
    task: str = typer.Option(..., "--task", "-t", help="What the agent should do."),
    workdir: Optional[Path] = typer.Option(
        None, "--workdir", "-w", help="Repository on this machine (default: current directory; mounted into the container in docker mode)."
    ),
    sandbox: str = typer.Option("local", "--sandbox", "-s", help="Where commands run: local or docker."),
    image: Optional[str] = typer.Option(None, "--image", help="Docker image (docker mode)."),
    container_dir: Optional[str] = typer.Option(
        None, "--container-dir", help="Working directory inside the container (default: /work when --workdir is mounted, else /testbed)."
    ),
    platform: Optional[str] = typer.Option(None, "--platform", help="Docker platform, e.g. linux/amd64."),
    provider: str = typer.Option("glm", "--provider", "-p", help="One of: " + ", ".join(PROVIDERS)),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the provider's default model."),
    max_turns: int = typer.Option(30, "--max-turns", help="Stop after this many model calls."),
    timeout: int = typer.Option(60, "--timeout", help="Per-command timeout in seconds."),
):
    """Run the agent on a task inside a repository."""
    load_dotenv()
    if workdir is not None and not workdir.is_dir():
        raise typer.BadParameter(f"{workdir} is not a directory", param_hint="--workdir")

    if sandbox == "local":
        runner = LocalRunner(workdir or Path("."))
    elif sandbox == "docker":
        if not image:
            raise typer.BadParameter("--image is required in docker mode", param_hint="--image")
        runner = DockerRunner(
            image,
            workdir=container_dir or ("/work" if workdir else "/testbed"),
            mount=workdir,
            platform=platform,
        )
    else:
        raise typer.BadParameter("expected 'local' or 'docker'", param_hint="--sandbox")

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

    try:
        with runner:
            result = run(task, llm, runner, max_turns=max_turns, timeout=timeout, on_turn=show)
    except RuntimeError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)

    status = "[green]finished[/]" if result.finished else f"[red]stopped at max_turns={max_turns}[/]"
    patch_note = f" · patch {result.patch.count(chr(10))} lines" if result.patch else " · no changes"
    console.print(
        f"\n{status} after {result.turns} turns · "
        f"{result.prompt_tokens} prompt / {result.completion_tokens} completion tokens"
        f"{patch_note} · traj/{result.run_id}.jsonl"
    )
    raise typer.Exit(code=0 if result.finished else 1)


def _cmd_of(arguments: str) -> str:
    try:
        return json.loads(arguments)["cmd"]
    except Exception:
        return arguments
