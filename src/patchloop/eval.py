import hashlib
import json
import platform
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Callable

from .baseline import single_shot
from .command_runtime import (
    COUNT_FIELDS, command_policy_metadata, count_policy_event,
    swebench_command_policy, validate_command_policy,
)
from .llm import LLM, estimate_cost
from .loop import RunInterrupted, run
from .eval_usage import normalize_usage, recover_usage
from .prompts import TASK_TEMPLATE
from .environment import (EnvironmentCheckError, MODULES, inspect_image, policy_metadata,
                          prepare_swebench_environment, solver_runner)

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
    stop_reason: str = ""
    final_message: str = ""
    environment_status: str = "not_checked"
    command_policy_counts: dict[str, int] = field(default_factory=dict)
    usage_status: str = "not_recorded"
    usage_error: str = ""
    known_cost: float | None = None
    grading_status: str = "not_run"
    grading_error: str = ""
    model_calls_attempted: int | None = None
    http_attempts: int | None = None
    unreported_http_attempts: int = 0


class SolveInterrupted(KeyboardInterrupt):
    def __init__(self, result, patch):
        super().__init__("evaluation interrupted")
        self.result = result
        self.patch = patch


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
    explicit_finish: bool = False,
    command_policy: str = "off",
) -> tuple[InstanceResult, str]:
    validate_command_policy(command_policy, mode)
    iid = instance["instance_id"]
    result = InstanceResult(instance_id=iid, mode=mode)
    started = time.time()
    patch = ""
    model_started = False
    interruption = None
    try:
        image = inspect_image(image_name(instance))
        with solver_runner(image["id"], workdir="/testbed", platform=platform, memory=memory) as runner:
            environment_report = prepare_swebench_environment(
                runner, instance, traj_dir.parent / "environment" / f"{iid}.json", image,
            )
            result.environment_status = "passed"
            if mode == "agent":
                policy = swebench_command_policy(command_policy, instance, environment_report,
                                                 getattr(runner, "workdir", "/testbed"))
                model_started = True
                try:
                    r = run(
                        task_text(instance), llm, runner,
                        max_turns=max_turns, timeout=timeout, traj_dir=traj_dir, on_turn=on_turn, run_id=iid,
                        budget_reminders=budget_reminders,
                        explicit_finish=explicit_finish,
                        command_policy=policy,
                        capture_errors=True,
                        on_policy=lambda event: count_policy_event(result.command_policy_counts, event),
                    )
                except RunInterrupted as exc:
                    r = exc.result
                    interruption = exc
                patch = r.patch
                result.turns, result.finished = r.turns, r.finished
                result.budget_reminders_sent = r.budget_reminders_sent
                result.stop_reason, result.final_message = r.stop_reason, r.final_message
                result.prompt_tokens, result.cached_tokens, result.completion_tokens = (
                    r.prompt_tokens, r.cached_tokens, r.completion_tokens,
                )
                result.error = getattr(r, "error", "")
            elif mode == "single-shot":
                model_started = True
                result.model_calls_attempted = 1
                def record_usage(usage):
                    result.turns = 1
                    counts = normalize_usage(usage)
                    result.usage_status = "recorded" if counts is not None else "incomplete"
                    for name, value in (counts or {}).items():
                        setattr(result, name, value)
                patch, note, usage = single_shot(instance, llm, runner, record_path=traj_dir / f"{iid}.single-shot.json", on_usage=record_usage)
                result.turns, result.finished, result.note = 1, True, note
                result.stop_reason = "single_shot"
                result.patch_applied = bool(patch) if patch or note == "patch did not apply" else None
                if patch:
                    (traj_dir / f"{iid}.patch").write_text(patch, encoding="utf-8")
            else:
                raise ValueError(f"unknown mode {mode!r}")
    except KeyboardInterrupt as exc:
        interruption = exc
        result.error = "Evaluation interrupted"
        result.stop_reason = "interrupted"
        result.finished = False
    except EnvironmentCheckError as e:
        result.environment_status = "failed"
        result.error = f"{type(e).__name__}: {e}"
        result.stop_reason = "environment_failed"
    except Exception as e:  # one broken instance must not take the whole run down
        result.error = f"{type(e).__name__}: {e}"
        if result.environment_status != "passed":
            result.environment_status = "failed"
            result.stop_reason = "environment_failed"
        else:
            result.stop_reason = "error"
    result.seconds = round(time.time() - started, 1)
    result.patch_lines = patch.count("\n")
    if model_started and mode == "agent":
        usage = recover_usage(Path(traj_dir) / f"{iid}.jsonl", iid, llm.model)
        for name, value in usage.items():
            setattr(result, name, value)
    elif not model_started:
        result.model_calls_attempted = 0
        result.usage_status = "not_started"
        result.cost = result.known_cost = 0.0
    else:
        result.known_cost = estimate_cost(llm.model, result.prompt_tokens, result.cached_tokens, result.completion_tokens)
        result.http_attempts = getattr(llm, "last_http_attempts", None)
        if result.http_attempts is not None:
            result.unreported_http_attempts = max(0, result.http_attempts - int(result.usage_status == "recorded"))
        if result.unreported_http_attempts:
            result.usage_status = "incomplete"
        result.cost = result.known_cost if result.usage_status == "recorded" and not result.error else None
        if result.error:
            result.usage_status = "incomplete"
    if interruption is not None:
        result.stop_reason = "interrupted"
        result.finished = False
        result.cost = None if model_started else 0.0
        raise SolveInterrupted(result, patch) from interruption
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
    explicit_finish: bool = False,
    command_policy: str = "off",
) -> dict:
    validate_command_policy(command_policy, mode)
    if budget_reminders and mode != "agent":
        raise ValueError("--budget-reminders requires --mode agent")
    if explicit_finish and mode != "agent":
        raise ValueError("--explicit-finish requires --mode agent")
    out_dir.mkdir(parents=True, exist_ok=True)
    config = _evaluation_config(
        instances, llm, dataset=dataset, mode=mode, max_turns=max_turns, timeout=timeout,
        workers=workers, memory=memory, judge_workers=judge_workers, budget_reminders=budget_reminders,
        explicit_finish=explicit_finish,
        command_policy=command_policy_metadata(command_policy),
        command_policy_targets={row["instance_id"]: list(MODULES.get(row.get("repo"), ())) for row in instances},
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
        on_turn = _turn_logger(iid, log, explicit_finish) if workers == 1 else None
        interrupted = None
        try:
            result, patch = solve(
                instance, llm, mode, max_turns, timeout, traj_dir,
                memory=memory, on_turn=on_turn, budget_reminders=budget_reminders,
                explicit_finish=explicit_finish,
                command_policy=command_policy,
            )
        except SolveInterrupted as exc:
            result, patch, interrupted = exc.result, exc.patch, exc
        with lock:
            _append_jsonl(predictions_path, {"instance_id": iid, "model_name_or_path": run_id, "model_patch": patch})
            _append_jsonl(results_path, asdict(result))
        if interrupted is not None:
            raise interrupted
        outcome = f"error {result.error}" if result.error else f"{result.turns} turns, patch {result.patch_lines} lines"
        log(f"< {iid}: {outcome} ({result.seconds}s)")
        return result

    interruption = None
    try:
        if workers == 1:
            for instance in todo:
                work(instance)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, todo))
    except KeyboardInterrupt as exc:
        interruption = exc

    results = {r["instance_id"]: InstanceResult(**r) for r in _read_jsonl(results_path)}
    ordered = [results[i["instance_id"]] for i in instances if i["instance_id"] in results]

    harness_run_id = None
    judgeable = [r for r in ordered if r.environment_status != "failed"]
    if judge and judgeable and interruption is None:
        by_id = {i["instance_id"]: i for i in instances}
        missing = [r.instance_id for r in judgeable if not image_present(image_name(by_id[r.instance_id]))]
        if missing:
            log(f"{len(missing)} image(s) missing locally; the harness will build them from the task repo")
        else:
            # with a task repo the harness rebuilds every image, whether or not it already exists
            task_repo = None
        try:
            verdicts, harness_run_id = judge_predictions(
                predictions_path, [r.instance_id for r in judgeable], run_id, dataset, harness_dir, task_repo, judge_workers, log,
            )
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                interruption = exc
            verdicts = {row.instance_id: {"resolved": None, "patch_applied": None, "status": "error",
                                         "error": f"{type(exc).__name__}: {exc}"} for row in judgeable}
        for r in ordered:
            if r.instance_id in verdicts:
                r.resolved = verdicts[r.instance_id]["resolved"]
                r.grading_status = verdicts[r.instance_id].get("status", "passed" if r.resolved else "failed")
                r.grading_error = verdicts[r.instance_id].get("error", "")
                if verdicts[r.instance_id]["patch_applied"] is not None:
                    r.patch_applied = verdicts[r.instance_id]["patch_applied"]

    summary = summarize(ordered, run_id, mode, llm)
    summary["harness_run_id"] = harness_run_id
    summary["budget_reminders"] = budget_reminders
    summary["explicit_finish"] = explicit_finish
    summary["command_policy"] = command_policy
    summary["planned_instances"] = len(instances)
    summary["pending_instance_ids"] = [item["instance_id"] for item in instances if item["instance_id"] not in results]
    if not judge:
        summary["requires_review"] = bool(summary["environment_failures"] or summary["unknown_cost_instances"] or summary["errors"])
    if summary["pending_instance_ids"]:
        summary["requires_review"] = True
        summary["resolve_rate"] = None
    summary["invocation"] = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "wall_seconds": round(time.monotonic() - started, 1),
        "resumed_instances": len(done),
        "judge": judge,
        "interrupted": interruption is not None,
    }
    if interruption is not None:
        summary["requires_review"] = True
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if interruption is not None:
        raise interruption
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
        "environment_policy": policy_metadata(),
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
    """Grade immutable predictions and retain unknown results on harness errors."""
    from .eval_grading import grade_predictions
    return grade_predictions(predictions_path, instance_ids, run_id, dataset, harness_dir, task_repo, workers, log)


def summarize(results: list[InstanceResult], run_id: str, mode: str, llm: LLM) -> dict:
    judged = [r for r in results if r.resolved is not None]
    resolved = sum(1 for r in judged if r.resolved)
    environment_failures = sum(r.environment_status == "failed" for r in results)
    grading_errors = sum(row.grading_status == "error" for row in results)
    unknown_costs = sum(row.cost is None for row in results)
    known_cost = sum(row.known_cost if row.known_cost is not None else row.cost or 0 for row in results)
    return {
        "run_id": run_id,
        "mode": mode,
        "provider": llm.provider,
        "model": llm.model,
        "instances": len(results),
        "judged": len(judged),
        "resolved": resolved,
        "resolve_rate": round(resolved / len(results), 3) if judged and len(judged) == len(results) and not environment_failures and not grading_errors else None,
        "errors": sum(1 for r in results if r.error),
        "environment_failures": environment_failures,
        "grading_errors": grading_errors,
        "unknown_cost_instances": unknown_costs,
        "known_cost_cny": round(known_cost, 3),
        "model_calls_attempted": sum(row.model_calls_attempted for row in results) if all(row.model_calls_attempted is not None for row in results) else None,
        "unreported_http_attempts": sum(row.unreported_http_attempts for row in results),
        "requires_review": bool(environment_failures or grading_errors or unknown_costs or any(row.error for row in results) or len(judged) != len(results)),
        "command_policy_counts": {name: sum(row.command_policy_counts.get(name, 0) for row in results)
                                  for name in COUNT_FIELDS},
        "prompt_tokens": sum(r.prompt_tokens for r in results),
        "cached_tokens": sum(r.cached_tokens for r in results),
        "completion_tokens": sum(r.completion_tokens for r in results),
        "cost_cny": round(sum(row.cost for row in results), 3) if not unknown_costs else None,
        "seconds": round(sum(r.seconds for r in results), 1),
        "results": [asdict(r) for r in results],
    }


def _turn_logger(iid: str, log: Callable[[str], None], explicit_finish: bool = False):
    def on_turn(turn: int, assistant: dict, tools: list[dict]) -> None:
        for tool in tools:
            if tool["name"] == "finish":
                log(f"  [{iid} #{turn}] finish: {tool['output'].strip()[:160]}")
                continue
            try:
                cmd = json.loads(tool["arguments"])["cmd"]
            except Exception:
                cmd = tool["arguments"]
            log(f"  [{iid} #{turn}] $ {cmd.strip()[:100]}")
        if not tools:
            status = "text reply; finish required" if explicit_finish else "finished"
            log(f"  [{iid} #{turn}] ({status}) {assistant['content'].strip()[:100]}")

    return on_turn


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
