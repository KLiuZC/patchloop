import hashlib
import json
import platform
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Callable

from .baseline import single_shot
from .llm import LLM, estimate_cost
from .loop import cached_tokens_of, run
from .prompts import TASK_TEMPLATE
from .sandbox import DockerRunner

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
HARNESS_ALIASES = {
    "SWE-bench/SWE-bench_Verified": "verified",
    "SWE-bench/SWE-bench_Lite": "lite",
    "SWE-bench/SWE-bench": "full",
}


def load_instances(ids: list[str], dataset: str = DEFAULT_DATASET, split: str = "test") -> list[dict]:
    from datasets import load_dataset  # slow import, only needed here

    wanted = set(ids)
    rows = {r["instance_id"]: r for r in load_dataset(dataset, split=split) if r["instance_id"] in wanted}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise ValueError(f"not found in {dataset}: {missing}")
    return [rows[i] for i in ids]


def read_id_file(path: Path) -> list[str]:
    ids = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def image_name(instance: dict) -> str:
    name = instance.get("image") or f"swebench/sweb.eval.x86_64.{instance['instance_id'].replace('__', '_1776_')}"
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def image_present(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def task_text(instance: dict) -> str:
    # only the issue text: hints, the test patch and the test names stay hidden from the agent
    return TASK_TEMPLATE.format(repo=instance["repo"], problem_statement=instance["problem_statement"].strip())


@dataclass
class InstanceResult:
    instance_id: str
    mode: str
    turns: int = 0
    finished: bool = False
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    patch_lines: int = 0
    seconds: float = 0.0
    error: str = ""
    note: str = ""
    resolved: bool | None = None
    patch_applied: bool | None = None
    budget_reminders_sent: int = 0


def solve(
    instance: dict,
    llm: LLM,
    mode: str,
    max_turns: int,
    timeout: int,
    traj_dir: Path,
    memory: str = "2g",
    platform: str = "linux/amd64",
    on_turn: Callable | None = None,
    budget_reminders: bool = False,
) -> tuple[InstanceResult, str]:
    iid = instance["instance_id"]
    result = InstanceResult(instance_id=iid, mode=mode)
    started = time.time()
    patch = ""
    try:
        with DockerRunner(image_name(instance), workdir="/testbed", platform=platform, memory=memory) as runner:
            if mode == "agent":
                r = run(
                    task_text(instance), llm, runner,
                    max_turns=max_turns, timeout=timeout, traj_dir=traj_dir, on_turn=on_turn, run_id=iid,
                    budget_reminders=budget_reminders,
                )
                patch = r.patch
                result.turns, result.finished = r.turns, r.finished
                result.budget_reminders_sent = r.budget_reminders_sent
                result.prompt_tokens, result.cached_tokens, result.completion_tokens = (
                    r.prompt_tokens, r.cached_tokens, r.completion_tokens,
                )
            elif mode == "single-shot":
                patch, note, usage = single_shot(instance, llm, runner, record_path=traj_dir / f"{iid}.single-shot.json")
                result.turns, result.finished, result.note = 1, True, note
                result.patch_applied = bool(patch) if patch or note == "patch did not apply" else None
                result.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                result.cached_tokens = cached_tokens_of(usage) if usage else 0
                result.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                if patch:
                    (traj_dir / f"{iid}.patch").write_text(patch, encoding="utf-8")
            else:
                raise ValueError(f"unknown mode {mode!r}")
    except Exception as e:  # one broken instance must not take the whole run down
        result.error = f"{type(e).__name__}: {e}"
    result.seconds = round(time.time() - started, 1)
    result.patch_lines = patch.count("\n")
    result.cost = estimate_cost(llm.model, result.prompt_tokens, result.cached_tokens, result.completion_tokens)
    return result, patch


def evaluate(
    instances: list[dict],
    llm: LLM,
    run_id: str,
    out_dir: Path,
    mode: str = "agent",
    max_turns: int = 40,
    timeout: int = 180,
    workers: int = 1,
    memory: str = "2g",
    judge: bool = True,
    harness_dir: Path | None = None,
    task_repo: Path | None = None,
    judge_workers: int = 1,
    dataset: str = DEFAULT_DATASET,
    log: Callable[[str], None] = print,
    budget_reminders: bool = False,
) -> dict:
    if budget_reminders and mode != "agent":
        raise ValueError("--budget-reminders requires --mode agent")
    out_dir.mkdir(parents=True, exist_ok=True)
    config = _evaluation_config(
        instances, llm, dataset=dataset, mode=mode, max_turns=max_turns, timeout=timeout,
        workers=workers, memory=memory, judge_workers=judge_workers, budget_reminders=budget_reminders,
    )
    _save_run_config(out_dir, config)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    traj_dir = out_dir / "traj"
    traj_dir.mkdir(exist_ok=True)
    predictions_path = out_dir / "predictions.jsonl"
    results_path = out_dir / "results.jsonl"
    lock = threading.Lock()

    done = {r["instance_id"] for r in _read_jsonl(predictions_path)}
    todo = [i for i in instances if i["instance_id"] not in done]
    if done:
        log(f"{len(done)} instance(s) already solved in {out_dir}, skipping them")

    def work(instance: dict) -> InstanceResult:
        iid = instance["instance_id"]
        log(f"> {iid}")
        on_turn = _turn_logger(iid, log) if workers == 1 else None
        result, patch = solve(
            instance, llm, mode, max_turns, timeout, traj_dir,
            memory=memory, on_turn=on_turn, budget_reminders=budget_reminders,
        )
        with lock:
            _append_jsonl(predictions_path, {"instance_id": iid, "model_name_or_path": run_id, "model_patch": patch})
            _append_jsonl(results_path, asdict(result))
        outcome = f"error {result.error}" if result.error else f"{result.turns} turns, patch {result.patch_lines} lines"
        log(f"< {iid}: {outcome} ({result.seconds}s)")
        return result

    if workers == 1:
        for instance in todo:
            work(instance)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, todo))

    results = {r["instance_id"]: InstanceResult(**r) for r in _read_jsonl(results_path)}
    ordered = [results[i["instance_id"]] for i in instances if i["instance_id"] in results]

    harness_run_id = None
    if judge and ordered:
        by_id = {i["instance_id"]: i for i in instances}
        missing = [r.instance_id for r in ordered if not image_present(image_name(by_id[r.instance_id]))]
        if missing:
            log(f"{len(missing)} image(s) missing locally; the harness will build them from the task repo")
        else:
            # with a task repo the harness rebuilds every image, whether or not it already exists
            task_repo = None
        verdicts, harness_run_id = judge_predictions(
            predictions_path, [r.instance_id for r in ordered], run_id, dataset, harness_dir, task_repo, judge_workers, log,
        )
        for r in ordered:
            if r.instance_id in verdicts:
                r.resolved = verdicts[r.instance_id]["resolved"]
                if verdicts[r.instance_id]["patch_applied"] is not None:
                    r.patch_applied = verdicts[r.instance_id]["patch_applied"]

    summary = summarize(ordered, run_id, mode, llm)
    summary["harness_run_id"] = harness_run_id
    summary["budget_reminders"] = budget_reminders
    summary["invocation"] = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "wall_seconds": round(time.monotonic() - started, 1),
        "resumed_instances": len(done),
        "judge": judge,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _evaluation_config(instances: list[dict], llm: LLM, **settings) -> dict:
    """Record reproducibility inputs without credentials, environment variables or gold text."""
    source = Path(__file__).parent
    lock = source.parents[1] / "uv.lock"
    selected = json.dumps(instances, sort_keys=True, ensure_ascii=False).encode()
    return {
        "schema_version": 1,
        "provider": llm.provider, "model": llm.model,
        "temperature": llm.temperature,
        "max_tokens": None,
        "api_timeout_seconds": getattr(llm.client.timeout, "read", llm.client.timeout),
        "api_max_retries": llm.client.max_retries,
        "platform": "linux/amd64", "container_cpus": 2.0, "container_network": "none",
        **settings,
        "instance_ids": [i["instance_id"] for i in instances],
        "selected_instances_sha256": hashlib.sha256(selected).hexdigest(),
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob("*.py"))},
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "python_version": platform.python_version(),
        "packages": {name: version(name) for name in ("openai", "datasets", "typer")},
    }


def _save_run_config(out_dir: Path, config: dict) -> None:
    path = out_dir / "run_config.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["config"] != config:
            raise ValueError("run configuration or source changed; use a new --run-id to keep experiments separate")
    elif any((out_dir / name).exists() for name in ("predictions.jsonl", "results.jsonl", "summary.json")):
        raise ValueError("existing run has no run_config.json; use a new --run-id instead of mixing legacy results")
    else:
        path.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(), "config": config,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def judge_predictions(
    predictions_path: Path,
    instance_ids: list[str],
    run_id: str,
    dataset: str,
    harness_dir: Path | None,
    task_repo: Path | None,
    workers: int,
    log: Callable[[str], None],
) -> tuple[dict[str, dict], str]:
    """Run the official SWE-bench harness on the predictions and read back its verdicts."""
    exe = shutil.which("swebench")
    if not exe:
        raise RuntimeError("swebench CLI not found on PATH (uv tool install git+https://github.com/SWE-bench/SWE-bench)")
    harness_dir = Path(harness_dir or ".").resolve()
    # the harness caches by run id, so a fresh id per judging pass makes sure new patches are really re-run
    harness_run_id = f"{run_id}-{time.strftime('%m%d-%H%M%S')}"
    cmd = [
        exe, "eval", HARNESS_ALIASES.get(dataset, dataset),
        "-p", str(predictions_path.resolve()),
        "--run-id", harness_run_id,
        "-j", str(workers),
    ]
    if task_repo:
        cmd += ["--task-repo", str(Path(task_repo).resolve())]
    for iid in instance_ids:
        cmd += ["-i", iid]
    log("judging with: " + " ".join(cmd))
    subprocess.run(cmd, cwd=harness_dir)

    report_dir = harness_dir / "logs" / "evaluation" / harness_run_id
    results_file = report_dir / "results.json"
    if not results_file.exists():
        raise RuntimeError(f"harness produced no {results_file}")
    overall = json.loads(results_file.read_text())
    verdicts = {}
    for iid in instance_ids:
        report = report_dir / run_id / iid / "report.json"
        if report.exists():
            d = json.loads(report.read_text()).get(iid, {})
            verdicts[iid] = {"resolved": bool(d.get("resolved")), "patch_applied": bool(d.get("patch_successfully_applied"))}
        else:
            verdicts[iid] = {"resolved": iid in overall.get("resolved_ids", []), "patch_applied": None}
    return verdicts, harness_run_id


def summarize(results: list[InstanceResult], run_id: str, mode: str, llm: LLM) -> dict:
    judged = [r for r in results if r.resolved is not None]
    resolved = sum(1 for r in judged if r.resolved)
    return {
        "run_id": run_id,
        "mode": mode,
        "provider": llm.provider,
        "model": llm.model,
        "instances": len(results),
        "judged": len(judged),
        "resolved": resolved,
        "resolve_rate": round(resolved / len(judged), 3) if judged else None,
        "errors": sum(1 for r in results if r.error),
        "prompt_tokens": sum(r.prompt_tokens for r in results),
        "cached_tokens": sum(r.cached_tokens for r in results),
        "completion_tokens": sum(r.completion_tokens for r in results),
        "cost_cny": round(sum(r.cost or 0 for r in results), 3),
        "seconds": round(sum(r.seconds for r in results), 1),
        "results": [asdict(r) for r in results],
    }


def _turn_logger(iid: str, log: Callable[[str], None]):
    def on_turn(turn: int, assistant: dict, tools: list[dict]) -> None:
        for tool in tools:
            try:
                cmd = json.loads(tool["arguments"])["cmd"]
            except Exception:
                cmd = tool["arguments"]
            log(f"  [{iid} #{turn}] $ {cmd.strip()[:100]}")
        if not tools:
            log(f"  [{iid} #{turn}] (finished) {assistant['content'].strip()[:100]}")

    return on_turn


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
