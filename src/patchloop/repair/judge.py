"""Official SWE-bench grading of saved repair artifacts; never calls a model."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _read_json(path: Path) -> dict:
    return _json_object(path.read_bytes(), path.name)


def _json_object(content: bytes, name: str) -> dict:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {name}")
    return value


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_verdict(overall: dict, report: dict | None, iid: str, empty_patch: bool) -> dict:
    for field in ("submitted_ids", "completed_ids", "resolved_ids", "unresolved_ids", "error_ids",
                  "incomplete_ids", "infra_failure_ids", "empty_patch_ids", "ambiguous_failure_ids"):
        ids = overall.get(field, [])
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            raise ValueError(f"official report has invalid {field}")
    reasons = overall.get("failure_reasons", {})
    if not isinstance(reasons, dict) or not all(isinstance(value, str) for value in reasons.values()):
        raise ValueError("official report has invalid failure_reasons")
    if iid not in overall.get("submitted_ids", []):
        raise ValueError("official report does not include the submitted instance")
    for field in ("error_ids", "incomplete_ids", "infra_failure_ids"):
        if iid in overall.get(field, []):
            raise RuntimeError(f"official harness reported {field} for {iid}")
    common = {
        "ambiguous_failure": iid in overall.get("ambiguous_failure_ids", []),
        "harness_failure_reason": overall.get("failure_reasons", {}).get(iid),
    }
    if empty_patch:
        if iid not in overall.get("empty_patch_ids", []) or iid in overall.get("resolved_ids", []):
            raise ValueError("official report does not confirm an empty patch")
        return {**common, "status": "failed", "resolved": False, "patch_applied": False,
                "reason": "empty_patch", "tests": {}}
    if iid not in overall.get("completed_ids", []) or report is None or iid not in report:
        raise ValueError("official per-instance report is missing or incomplete")
    verdict = report[iid]
    if not isinstance(verdict, dict):
        raise ValueError("official per-instance verdict must be an object")
    if verdict.get("infra_failure"):
        raise RuntimeError("official per-instance report marks an infrastructure failure")
    resolved, applied = verdict.get("resolved"), verdict.get("patch_successfully_applied")
    if not isinstance(resolved, bool) or not isinstance(applied, bool):
        raise ValueError("official report has no boolean verdict / patch application result")
    if resolved != (iid in overall.get("resolved_ids", [])):
        raise ValueError("overall and per-instance resolved verdicts disagree")
    if not resolved and iid not in overall.get("unresolved_ids", []):
        raise ValueError("overall report does not account for this unresolved instance")
    tests = {}
    statuses = verdict.get("tests_status", {})
    if not isinstance(statuses, dict):
        raise ValueError("official tests_status must be an object")
    for group in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        cases = statuses.get(group)
        if cases is None and not applied:
            continue
        if not isinstance(cases, dict) or not all(
            isinstance(cases.get(k), list) and all(isinstance(test, str) for test in cases[k])
            for k in ("success", "failure")
        ):
            raise ValueError(f"official report is missing {group} test results")
        tests[group] = {"passed": len(cases["success"]), "failed": len(cases["failure"])}
    if resolved and (not applied or any(t["failed"] for t in tests.values())):
        raise ValueError("resolved verdict conflicts with patch or test results")
    return {**common, "status": "passed" if resolved else "failed", "resolved": resolved,
            "patch_applied": applied, "tests": tests,
            "reason": "resolved" if resolved else "tests_not_resolved" if applied else "patch_did_not_apply"}


def judge_repair(artifacts_dir: Path, *, harness_dir: Path | None = None, timeout: int = 1800) -> dict:
    """Grade an exact saved patch, keeping visible validation and every grading attempt intact."""
    root = Path(artifacts_dir).expanduser().resolve()
    inputs = {root / name: (root / name).read_bytes() for name in ("task.json", "summary.json", "patch.diff")}
    if (root / "validation.json").exists():
        inputs[root / "validation.json"] = (root / "validation.json").read_bytes()
    task = _json_object(inputs[root / "task.json"], "task.json")
    summary = _json_object(inputs[root / "summary.json"], "summary.json")
    iid, base = task.get("instance_id"), task.get("base_commit")
    if not isinstance(iid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*__[A-Za-z0-9_.-]+", iid):
        raise ValueError("artifacts must contain a SWE-bench instance_id")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", base):
        raise ValueError("task must contain the full base_commit")
    if summary.get("task_id") != iid or summary.get("execution_status") not in ("completed", "interrupted"):
        raise ValueError("no completed repair attempt to grade; execution errors require a new repair attempt")
    if timeout <= 0:
        raise ValueError("harness timeout must be positive")
    patch_bytes = inputs[root / "patch.diff"]
    patch = patch_bytes.decode("utf-8")
    patch_hash = hashlib.sha256(patch_bytes).hexdigest()
    if not isinstance(summary.get("patch"), str) or summary["patch"].encode("utf-8") != patch_bytes:
        raise ValueError("patch.diff differs from the saved repair summary; use a new repair attempt")
    visible = _json_object(inputs[root / "validation.json"], "validation.json") if root / "validation.json" in inputs else {}
    if visible.get("patch_sha256") and visible["patch_sha256"] != patch_hash:
        raise ValueError("patch.diff differs from the visible validation's patch hash")
    if visible.get("base_sha") and visible["base_sha"] != base:
        raise ValueError("visible validation and task base commits differ")

    run_id = "repair-judge-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    attempt = root / "official" / run_id
    attempt.mkdir(parents=True)
    workdir = Path(harness_dir or os.environ.get("SWEBENCH_WORKDIR", "~/code/swebench-work")).expanduser().resolve()
    report_root = workdir / "logs/evaluation" / run_id
    instance_report = report_root / run_id / iid
    (attempt / "patch.diff").write_bytes(patch_bytes)
    predictions = attempt / "predictions.jsonl"
    predictions.write_text(json.dumps({
        "instance_id": iid, "model_name_or_path": run_id, "model_patch": patch,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    result = {
        "schema_version": 1, "instance_id": iid, "dataset": "SWE-bench/SWE-bench_Verified",
        "base_sha": base, "image": task.get("image"), "patch_sha256": patch_hash,
        "task_sha256": hashlib.sha256(inputs[root / "task.json"]).hexdigest(),
        "visible_validation_sha256": hashlib.sha256(inputs[root / "validation.json"]).hexdigest() if root / "validation.json" in inputs else None,
        "visible_validation_status": visible.get("status", summary.get("validation_status", "not_run")),
        "status": "running", "resolved": None, "patch_applied": None, "tests": {},
        "harness_run_id": run_id, "harness_report_dir": str(report_root),
        "attempt_dir": str(attempt.relative_to(root)),
        "started_at": datetime.now(timezone.utc).isoformat(), "error": "",
    }
    # Original artifacts and the submitted snapshot must stay bound to the same bytes.
    inputs[attempt / "patch.diff"] = patch_bytes
    inputs[predictions] = predictions.read_bytes()
    _write_json(attempt / "result.json", result)
    _write_json(root / "official.json", result)
    started = time.monotonic()
    try:
        if any(not p.exists() or p.read_bytes() != content for p, content in inputs.items()):
            raise ValueError("repair artifacts changed while creating grading snapshot")
        exe = shutil.which("swebench")
        if not exe:
            raise RuntimeError("swebench CLI not found on PATH")
        workdir.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "eval", "verified", "-p", str(predictions), "--run-id", run_id,
               "-i", iid, "-j", "1", "--timeout", str(timeout)]
        result["command"] = cmd
        with (attempt / "harness.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, check=False)
        result["harness_exit_code"] = process.returncode
        for source, dest in [
            (report_root / "results.json", "results.json"),
            (instance_report / "report.json", "report.json"),
            (instance_report / "test_output.txt", "test_output.txt"),
            (instance_report / "run_instance.log", "run_instance.log"),
            (instance_report / "patch.diff", "harness.patch.diff"),
        ]:
            if source.exists():
                shutil.copyfile(source, attempt / dest)
        if any(not p.exists() or p.read_bytes() != content for p, content in inputs.items()):
            raise ValueError("repair artifacts or submitted snapshot changed during grading")
        submitted = attempt / "harness.patch.diff"
        if submitted.exists() and submitted.read_bytes() != patch_bytes:
            raise ValueError("harness evaluated a different patch")
        if process.returncode != 0:
            raise RuntimeError(f"swebench exited with code {process.returncode}; see harness.log")
        overall = _read_json(attempt / "results.json")
        report = _read_json(attempt / "report.json") if (attempt / "report.json").exists() else None
        result.update(_parse_verdict(overall, report, iid, empty_patch=not patch.strip()))
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(status="error", resolved=None, reason="grading_error", error=f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        result.update(status="error", resolved=None, reason="interrupted", error="Official grading interrupted")
        raise
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        _write_json(attempt / "result.json", result)
        _write_json(root / "official.json", result)
    return result
