import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import List, Optional
from uuid import UUID

import typer
from dotenv import dotenv_values, load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .eval import DEFAULT_DATASET, evaluate, load_instances, read_id_file
from .llm import LLM, PROVIDERS
from .loop import run
from .repair import Limits, RepairRunner, RepairTask, SWEbenchRepairRunner, SWEbenchTask, VerificationProfile
from .repair.judge import judge_repair
from .sandbox import DockerRunner, LocalRunner
from .subset import DEFAULT_REPOS, DIFFICULTY_MIX, breakdown, select_subset, write_subset

# no locals in tracebacks: they would include the API key
app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()


@app.callback()
def main() -> None:
    """A minimal coding agent."""


@app.command("serve")
def serve_command(
    db: Path = typer.Option(Path("data/tasks.sqlite3"), "--db", help="Persistent SQLite task database."),
    port: int = typer.Option(8000, "--port", min=1, max=65535, help="Local HTTP port."),
):
    """Serve task submission and queries on 127.0.0.1; start a worker separately to execute tasks."""
    import uvicorn

    from .api import create_app

    token = os.environ.get("PATCHLOOP_GITHUB_TOKEN")
    if token is None:
        token = dotenv_values(".env").get("PATCHLOOP_GITHUB_TOKEN") or ""
    # The API uses only its publication credential; provider keys stay with the worker.
    uvicorn.run(create_app(db, github_token_provider=lambda: token), host="127.0.0.1", port=port)


@app.command("worker")
def worker_command(
    db: Path = typer.Option(Path("data/tasks.sqlite3"), "--db", help="Same SQLite database used by agent serve."),
    artifacts: Optional[Path] = typer.Option(None, "--artifacts", help="Default: task-runs beside the database."),
    once: bool = typer.Option(False, "--once", help="Process at most one queued task, then exit."),
    task_id: Optional[UUID] = typer.Option(None, "--task-id", help="Process only this queued task, then exit."),
):
    """Run queued repair tasks serially in Docker (macOS/Linux)."""
    from .api.store import TaskStore
    from .worker import TaskWorker, worker_signals

    load_dotenv()
    try:
        with worker_signals():
            TaskWorker(TaskStore(db), artifacts, log=lambda line: console.print(escape(line), highlight=False)).run(
                once=once, task_id=task_id,
            )
    except KeyboardInterrupt:
        console.print("Worker stopped.")
        raise typer.Exit(code=130)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        console.print(escape(f"{type(exc).__name__}: {exc}"), style="red")
        raise typer.Exit(code=2)


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
    budget_reminders: bool = typer.Option(False, "--budget-reminders", help="Remind the model at 20 and 5 calls remaining."),
    explicit_finish: bool = typer.Option(False, "--explicit-finish", help="Require the finish tool to submit the task."),
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
            if tool["name"] == "finish":
                console.print(f"[bold cyan]\\[{turn}][/] finish {escape(tool['arguments'])}")
            else:
                console.print(f"[bold cyan]\\[{turn}][/] $ {escape(_cmd_of(tool['arguments']))}")
            console.print(escape(tool["output"]), style="dim")

    try:
        with runner:
            result = run(
                task, llm, runner, max_turns=max_turns, timeout=timeout, on_turn=show,
                budget_reminders=budget_reminders,
                explicit_finish=explicit_finish,
            )
    except RuntimeError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)

    if explicit_finish and result.finished:
        console.print(escape(result.final_message))
    status = "[green]finished[/]" if result.finished else f"[red]stopped at max_turns={max_turns}[/]"
    patch_note = f" · patch {result.patch.count(chr(10))} lines" if result.patch else " · no changes"
    console.print(
        f"\n{status} after {result.turns} turns · "
        f"{result.prompt_tokens} prompt / {result.completion_tokens} completion tokens"
        f"{patch_note} · traj/{result.run_id}.jsonl"
    )
    raise typer.Exit(code=0 if result.finished else 1)


@app.command("repair")
def repair_command(
    goal: str = typer.Option(..., "--goal", "-t", help="The repair task to solve."),
    repo: Path = typer.Option(..., "--repo", "-r", help="Git repository to repair."),
    task_id: str = typer.Option("repair-run", "--task-id", help="Stable name for this repair attempt."),
    base_sha: Optional[str] = typer.Option(None, "--base-sha", help="Commit to repair; defaults to HEAD."),
    verify: str = typer.Option("pytest -q", "--verify", help="Fixed verification command for the clean candidate."),
    artifacts: Path = typer.Option(Path("repair-runs"), "--artifacts", help="Directory for task artifacts."),
    sandbox: str = typer.Option("local", "--sandbox", help="Where commands run: local or docker."),
    image: Optional[str] = typer.Option(None, "--image", help="Docker image for local repair and verification."),
    platform: Optional[str] = typer.Option(None, "--platform", help="Docker platform, e.g. linux/amd64."),
    memory: str = typer.Option("2g", "--memory", help="Docker memory limit."),
    provider: str = typer.Option("glm", "--provider", "-p", help="One of: " + ", ".join(PROVIDERS)),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the provider's default model."),
    max_model_calls: int = typer.Option(30, "--max-model-calls"),
    command_timeout: int = typer.Option(60, "--command-timeout"),
    verification_timeout: int = typer.Option(180, "--verification-timeout"),
    budget_reminders: bool = typer.Option(False, "--budget-reminders"),
    explicit_finish: bool = typer.Option(False, "--explicit-finish"),
):
    """Run one repair attempt and independently verify its exact patch from a clean base."""
    load_dotenv()
    repo = repo.resolve()
    if not repo.is_dir():
        raise typer.BadParameter(f"{repo} is not a directory", param_hint="--repo")
    if sandbox not in {"local", "docker"}:
        raise typer.BadParameter("expected 'local' or 'docker'", param_hint="--sandbox")
    if sandbox == "docker" and not image:
        raise typer.BadParameter("--image is required in docker mode", param_hint="--image")
    git_root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True
    )
    if git_root_result.returncode != 0:
        raise typer.BadParameter("repo is not inside a git repository", param_hint="--repo")
    git_root = Path(git_root_result.stdout.strip()).resolve()
    workdir = repo.relative_to(git_root).as_posix() or "."
    if base_sha is None:
        resolved = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_root, capture_output=True, text=True
        )
        if resolved.returncode != 0:
            raise typer.BadParameter("repo is not a git repository", param_hint="--repo")
        base_sha = resolved.stdout.strip()
    try:
        llm = LLM(provider, model)
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)

    def runner_factory(root: Path, relative_workdir: str):
        if sandbox == "local":
            return LocalRunner(root / relative_workdir)
        container_workdir = "/work" if relative_workdir == "." else f"/work/{relative_workdir}"
        return DockerRunner(
            image,
            workdir=container_workdir,
            mount=root,
            mount_target="/work",
            platform=platform,
            memory=memory,
        )

    task = RepairTask(
        task_id=task_id,
        repo_path=git_root,
        base_sha=base_sha,
        goal=goal,
        workdir=workdir,
        verification=VerificationProfile(id="cli", command=verify, timeout_seconds=verification_timeout),
        limits=Limits(max_model_calls=max_model_calls, command_timeout_seconds=command_timeout),
        budget_reminders=budget_reminders,
        explicit_finish=explicit_finish,
    )
    try:
        result = RepairRunner(task, artifacts / task_id, runner_factory).run(llm)
    except Exception as e:
        console.print(f"[red]{escape(f'{type(e).__name__}: {e}')}[/]")
        raise typer.Exit(code=2)
    console.print(
        f"execution={result.execution_status} · validation={result.validation_status} · "
        f"stop={result.stop_reason} · artifacts={result.artifacts_dir}"
    )
    raise typer.Exit(code=0 if result.validation_status == "passed" else 1)


@app.command("repair-swebench")
def repair_swebench_command(
    instance_id: str = typer.Option(..., "--instance", "-i", help="SWE-bench instance id."),
    verify: str = typer.Option("pytest -q", "--verify", help="Visible verification command inside /testbed."),
    artifacts: Path = typer.Option(Path("repair-runs"), "--artifacts", help="Directory for task artifacts."),
    provider: str = typer.Option("deepseek", "--provider", "-p", help="One of: " + ", ".join(PROVIDERS)),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the provider's default model."),
    memory: str = typer.Option("3g", "--memory"),
    max_model_calls: int = typer.Option(40, "--max-model-calls"),
    command_timeout: int = typer.Option(180, "--command-timeout"),
    budget_reminders: bool = typer.Option(True, "--budget-reminders/--no-budget-reminders"),
    explicit_finish: bool = typer.Option(False, "--explicit-finish"),
    judge: bool = typer.Option(False, "--judge/--no-judge", help="Grade the saved patch with the official harness after repair."),
    harness_dir: Optional[Path] = typer.Option(None, "--harness-dir", help="Official harness work directory ($SWEBENCH_WORKDIR)."),
    judge_timeout: int = typer.Option(1800, "--judge-timeout", min=1, help="Official per-instance test timeout in seconds."),
):
    """Run one SWE-bench repair attempt and save an independently verified patch."""
    load_dotenv()
    try:
        llm = LLM(provider, model)
        loaded = load_instances([instance_id], DEFAULT_DATASET)
        task = SWEbenchTask.from_instance(loaded[0], verification_command=verify)
        result = SWEbenchRepairRunner(
            task,
            artifacts / instance_id,
            memory=memory,
        ).run(
            llm,
            max_model_calls=max_model_calls,
            command_timeout=command_timeout,
            budget_reminders=budget_reminders,
            explicit_finish=explicit_finish,
        )
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)
    console.print(
        f"execution={result.execution_status} · visible={result.validation_status} · "
        f"stop={result.stop_reason} · artifacts={result.artifacts_dir}"
    )
    if judge:
        if result.execution_status == "error":
            console.print("official=not_run · repair execution failed")
            raise typer.Exit(code=2)
        _grade_saved_repair(Path(result.artifacts_dir), harness_dir, judge_timeout)
    raise typer.Exit(code=0 if result.validation_status == "passed" else 1)


@app.command("preflight-swebench")
def preflight_swebench_command(
    instance_id: str = typer.Option(..., "--instance", "-i"),
    verify: str = typer.Option(..., "--verify", help="Existing visible checks expected to exit 0; no gold or hidden tests."),
    artifacts: Path = typer.Option(..., "--artifacts", help="New directory for raw and isolated environment reports."),
    memory: str = typer.Option("3g", "--memory"),
    timeout: int = typer.Option(180, "--timeout", min=1),
):
    """Inspect a SWE-bench solving environment without calling a model."""
    from .environment import preflight_instance
    from .eval import image_name
    try:
        instance = load_instances([instance_id], DEFAULT_DATASET)[0]
        report = preflight_instance(instance, image_name(instance), artifacts, verify=verify,
                                    memory=memory, timeout=timeout)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(escape(f"{type(exc).__name__}: {exc}"), style="red")
        raise typer.Exit(code=2)
    console.print(f"preflight={report['status']} · artifacts={artifacts}")
    raise typer.Exit(code=0 if report["status"] == "passed" else 2)


@app.command("repair-judge")
def repair_judge_command(
    artifacts: Path = typer.Option(..., "--artifacts", exists=True, file_okay=False, help="Existing task directory containing task.json and patch.diff."),
    harness_dir: Optional[Path] = typer.Option(None, "--harness-dir", help="Official harness work directory ($SWEBENCH_WORKDIR, default ~/code/swebench-work)."),
    timeout: int = typer.Option(1800, "--timeout", min=1, help="Official per-instance test timeout in seconds."),
):
    """Grade an existing repair patch without calling the model or rerunning visible tests."""
    load_dotenv()
    _grade_saved_repair(artifacts, harness_dir, timeout)


def _grade_saved_repair(artifacts: Path, harness_dir: Path | None, timeout: int) -> None:
    console.print("Grading saved patch with the official harness; no model calls ...")
    try:
        result = judge_repair(artifacts, harness_dir=harness_dir, timeout=timeout)
    except (OSError, ValueError, RuntimeError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)
    counts = []
    for group, label in [("FAIL_TO_PASS", "F2P"), ("PASS_TO_PASS", "P2P")]:
        test = result["tests"].get(group)
        counts.append(f"{label}={test['passed']}/{test['passed'] + test['failed']}" if test else f"{label}=not_run")
    resolved = "unknown" if result["resolved"] is None else str(result["resolved"]).lower()
    console.print(escape(
        f"visible={result['visible_validation_status']} · official={result['status']} · "
        f"resolved={resolved} · {' · '.join(counts)}"
    ))
    if result.get("harness_failure_reason"):
        console.print(escape(f"harness label={result['harness_failure_reason']} (see per-instance report and test output)"))
    if result["error"]:
        console.print(escape(result["error"]), style="red")
    console.print(escape(f"patch_sha256={result['patch_sha256']} · {artifacts / 'official.json'}"))
    raise typer.Exit(code={"passed": 0, "failed": 1, "error": 2}[result["status"]])


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
    budget_reminders: bool = typer.Option(False, "--budget-reminders", help="Agent mode only: remind at 20 and 5 calls remaining."),
    explicit_finish: bool = typer.Option(False, "--explicit-finish", help="Agent mode only: require the finish tool to submit."),
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
    if budget_reminders and mode != "agent":
        raise typer.BadParameter("requires --mode agent", param_hint="--budget-reminders")
    if explicit_finish and mode != "agent":
        raise typer.BadParameter("requires --mode agent", param_hint="--explicit-finish")

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
    try:
        summary = evaluate(
            loaded, llm, run_id, out_dir,
            mode=mode, max_turns=max_turns, timeout=timeout, workers=workers, memory=memory,
            judge=judge, harness_dir=harness_dir, task_repo=task_repo, judge_workers=judge_workers,
            dataset=dataset, log=lambda line: console.print(escape(line), highlight=False),
            budget_reminders=budget_reminders,
            explicit_finish=explicit_finish,
        )
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(code=2)
    _print_summary(summary, out_dir)
    if summary.get("environment_failures"):
        raise typer.Exit(code=2)


def _print_summary(summary: dict, out_dir: Path) -> None:
    table = Table(title=f"{summary['run_id']} · {summary['mode']} · {summary['model']}")
    for col in ("instance", "turns", "reminders", "prompt/cached/completion", "cost", "patch", "resolved", "note"):
        table.add_column(col)
    for r in summary["results"]:
        if r["resolved"] is None:
            verdict = "-"
        else:
            verdict = "[green]yes[/]" if r["resolved"] else "[red]no[/]"
        limit_note = "max_turns" if not r["finished"] and not r["error"] else ""
        finish_note = "finish" if r.get("stop_reason") == "finish" else ""
        notes = [n for n in (r["error"], r.get("note", ""), limit_note, finish_note) if n]
        if not r["error"] and not r["patch_lines"] and not r.get("note"):
            notes.append("no patch")
        if r["patch_applied"] is False and "did not apply" not in " ".join(notes):
            notes.append("patch did not apply")
        note = "; ".join(notes)
        table.add_row(
            r["instance_id"], str(r["turns"]), str(r.get("budget_reminders_sent", 0)),
            f"{r['prompt_tokens'] // 1000}k / {r['cached_tokens'] // 1000}k / {r['completion_tokens']}",
            f"¥{r['cost']:.2f}" if r["cost"] is not None else "-",
            f"+{r['patch_lines']}" if r["patch_lines"] else "none",
            verdict, escape(note),
        )
    console.print(table)
    if summary.get("environment_failures"):
        rate = f"incomplete: {summary['environment_failures']} environment preflight failure(s)"
    else:
        rate = f"{summary['resolved']}/{summary['judged']} ({summary['resolve_rate'] * 100:.0f}%)" if summary["judged"] else "not judged"
    invocation = summary.get("invocation")
    time_note = (
        f"this invocation {invocation['wall_seconds'] / 60:.1f} min"
        if invocation else f"summed task time {summary['seconds'] / 60:.1f} min"
    )
    console.print(
        f"resolved {rate} · estimated ¥{summary['cost_cny']:.2f} · {time_note} · "
        f"errors {summary['errors']} · {out_dir}/summary.json"
    )
    console.print(
        "[dim]EXPERIMENTS.md: | <date> | " + escape(summary["run_id"]) + f" ({summary['instances']} 题, {summary['mode']}) | "
        f"{summary['provider']} / {summary['model']} | - | {summary['prompt_tokens'] // 1000}k / {summary['completion_tokens'] // 1000}k | "
        f"{rate} | estimated ¥{summary['cost_cny']:.2f}, {time_note} |[/]"
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
