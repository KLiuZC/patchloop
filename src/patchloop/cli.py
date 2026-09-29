import json
import os
from pathlib import Path
from typing import List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .eval import DEFAULT_DATASET, evaluate, load_instances, read_id_file
from .llm import LLM, PROVIDERS
from .loop import run
from .sandbox import DockerRunner, LocalRunner
from .subset import DEFAULT_REPOS, DIFFICULTY_MIX, breakdown, select_subset, write_subset

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


@app.command("eval")
def eval_command(
    run_id: str = typer.Option(..., "--run-id", "-r", help="Names the results directory evals/results/<run-id>."),
    instances: Optional[List[str]] = typer.Option(None, "--instance", "-i", help="Instance id (repeatable)."),
    instances_file: Optional[Path] = typer.Option(None, "--instances-file", "-f", help="File with one instance id per line."),
    mode: str = typer.Option("agent", "--mode", help="agent or single-shot."),
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset", help="HuggingFace dataset id."),
    provider: str = typer.Option("glm", "--provider", "-p", help="One of: " + ", ".join(PROVIDERS)),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the provider's default model."),
    max_turns: int = typer.Option(40, "--max-turns"),
    timeout: int = typer.Option(180, "--timeout", help="Per-command timeout in seconds."),
    workers: int = typer.Option(1, "--workers", "-j", help="Instances solved in parallel."),
    memory: str = typer.Option("2g", "--memory", help="Memory limit per container."),
    judge: bool = typer.Option(True, "--judge/--no-judge", help="Run the SWE-bench harness afterwards."),
    judge_workers: int = typer.Option(1, "--judge-workers"),
    harness_dir: Optional[Path] = typer.Option(
        None, "--harness-dir", help="Where the harness writes logs and finds swe-bench-tasks ($SWEBENCH_WORKDIR, default ~/code/swebench-work)."
    ),
):
    """Solve SWE-bench instances in their own images, then grade the patches with the official harness."""
    load_dotenv()
    ids = list(instances or [])
    if instances_file:
        ids += read_id_file(instances_file)
    if not ids:
        raise typer.BadParameter("give at least one --instance or an --instances-file", param_hint="--instance")
    if mode not in ("agent", "single-shot"):
        raise typer.BadParameter("expected 'agent' or 'single-shot'", param_hint="--mode")

    harness_dir = harness_dir or Path(os.environ.get("SWEBENCH_WORKDIR", "~/code/swebench-work")).expanduser()
    task_repo = harness_dir / "swe-bench-tasks"
    if judge and not task_repo.is_dir():
        console.print(f"[yellow]no task repo at {task_repo}; the harness will pull images instead of building them[/]")
        task_repo = None

    try:
        llm = LLM(provider, model)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)

    console.print(f"loading {len(ids)} instance(s) from {dataset} ...")
    loaded = load_instances(ids, dataset)
    out_dir = Path("evals/results") / run_id
    summary = evaluate(
        loaded, llm, run_id, out_dir,
        mode=mode, max_turns=max_turns, timeout=timeout, workers=workers, memory=memory,
        judge=judge, harness_dir=harness_dir, task_repo=task_repo, judge_workers=judge_workers,
        dataset=dataset, log=lambda line: console.print(escape(line), highlight=False),
    )
    _print_summary(summary, out_dir)


def _print_summary(summary: dict, out_dir: Path) -> None:
    table = Table(title=f"{summary['run_id']} · {summary['mode']} · {summary['model']}")
    for col in ("instance", "turns", "prompt/cached/completion", "cost", "patch", "resolved", "note"):
        table.add_column(col)
    for r in summary["results"]:
        if r["resolved"] is None:
            verdict = "-"
        else:
            verdict = "[green]yes[/]" if r["resolved"] else "[red]no[/]"
        notes = [n for n in (r["error"], r.get("note", ""), "" if r["finished"] else "max_turns") if n]
        if not r["error"] and not r["patch_lines"] and not r.get("note"):
            notes.append("no patch")
        if r["patch_applied"] is False and "did not apply" not in " ".join(notes):
            notes.append("patch did not apply")
        note = "; ".join(notes)
        table.add_row(
            r["instance_id"], str(r["turns"]),
            f"{r['prompt_tokens'] // 1000}k / {r['cached_tokens'] // 1000}k / {r['completion_tokens']}",
            f"¥{r['cost']:.2f}" if r["cost"] is not None else "-",
            f"+{r['patch_lines']}" if r["patch_lines"] else "none",
            verdict, escape(note),
        )
    console.print(table)
    rate = f"{summary['resolved']}/{summary['judged']} ({summary['resolve_rate'] * 100:.0f}%)" if summary["judged"] else "not judged"
    console.print(
        f"resolved {rate} · ¥{summary['cost_cny']:.2f} · {summary['seconds'] / 60:.0f} min · "
        f"errors {summary['errors']} · {out_dir}/summary.json"
    )
    console.print(
        "[dim]EXPERIMENTS.md: | <date> | " + escape(summary["run_id"]) + f" ({summary['instances']} 题, {summary['mode']}) | "
        f"{summary['provider']} / {summary['model']} | - | {summary['prompt_tokens'] // 1000}k / {summary['completion_tokens'] // 1000}k | "
        f"{rate} | ¥{summary['cost_cny']:.2f}, {summary['seconds'] / 60:.0f} min |[/]"
    )


@app.command("subset")
def subset_command(
    out: Path = typer.Option(..., "--out", "-o", help="Where to write the id list."),
    size: int = typer.Option(30, "--size"),
    seed: int = typer.Option(1, "--seed"),
    cap: int = typer.Option(7, "--cap", help="Max instances per repository."),
    repo: Optional[List[str]] = typer.Option(None, "--repo", help="Allowed repository (repeatable). Default: pure-Python repos."),
    exclude_file: Optional[List[Path]] = typer.Option(None, "--exclude-file", "-x", help="Ids to leave out (repeatable), e.g. the dev set."),
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset"),
):
    """Pick a reproducible evaluation subset: stratified by difficulty, spread across repositories."""
    from datasets import load_dataset  # slow import

    repos = repo or DEFAULT_REPOS
    exclude = [i for f in (exclude_file or []) for i in read_id_file(f)]
    rows = list(load_dataset(dataset, split="test"))
    chosen = select_subset(rows, size=size, seed=seed, repos=repos, cap=cap, exclude=exclude)
    header = [
        f"{dataset} subset: size={len(chosen)} seed={seed} cap_per_repo={cap}",
        "repos: " + ", ".join(repos),
        "difficulty mix: " + ", ".join(f"{k} {v:.0%}" for k, v in DIFFICULTY_MIX.items()),
        "excluded: " + (", ".join(str(f) for f in exclude_file) if exclude_file else "none"),
        f"regenerate: agent subset -o {out} --size {size} --seed {seed} --cap {cap}"
        + "".join(f" -x {f}" for f in (exclude_file or [])),
    ]
    write_subset(chosen, out, header)

    table = Table(title=f"{len(chosen)} instances → {out}")
    table.add_column("repo")
    for d in DIFFICULTY_MIX:
        table.add_column(d, justify="right")
    table.add_column("total", justify="right")
    for r, counts in sorted(breakdown(chosen).items()):
        table.add_row(r, *[str(counts.get(d, 0)) for d in DIFFICULTY_MIX], str(sum(counts.values())))
    totals = [sum(1 for c in chosen if c["difficulty"] == d) for d in DIFFICULTY_MIX]
    table.add_row("total", *[str(t) for t in totals], str(len(chosen)), style="bold")
    console.print(table)
    if len(chosen) < size:
        console.print(f"[yellow]only {len(chosen)} instances satisfy the constraints[/]")
