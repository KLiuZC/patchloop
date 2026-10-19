"""Batch official grading of saved predictions without invoking a model."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


HARNESS_ALIASES = {
    "SWE-bench/SWE-bench_Verified": "verified",
    "SWE-bench/SWE-bench_Lite": "lite",
    "SWE-bench/SWE-bench": "full",
}


def _validate_instance_id(instance_id: str) -> None:
    if not isinstance(instance_id, str) or len(instance_id) > 200 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*__[A-Za-z0-9_.-]+", instance_id,
    ):
        raise ValueError(f"invalid SWE-bench instance_id: {instance_id!r}")


def _validate_arguments(instance_ids: list[str], run_id: str, dataset: str, workers: int) -> None:
    if not isinstance(instance_ids, list) or not instance_ids:
        raise ValueError("instance_ids must be a nonempty list")
    for instance_id in instance_ids:
        _validate_instance_id(instance_id)
    if len(instance_ids) != len({instance_id.casefold() for instance_id in instance_ids}):
        raise ValueError("duplicate instance_ids")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id):
        raise ValueError("run_id must be a safe path component of at most 100 characters")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if not isinstance(dataset, str) or not dataset.strip() or dataset.startswith("-") or "\x00" in dataset:
        raise ValueError("invalid dataset")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _json_object(content: bytes) -> dict:
    value = json.loads(content, object_pairs_hook=_unique_fields)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _select_predictions(content: bytes, instance_ids: list[str]) -> dict[str, str]:
    selected = {}
    seen = set()
    for line in content.splitlines():
        if not line.strip():
            continue
        row = _json_object(line)
        instance_id = row.get("instance_id")
        _validate_instance_id(instance_id)
        if instance_id.casefold() in seen:
            raise ValueError(f"duplicate prediction instance_id: {instance_id}")
        seen.add(instance_id.casefold())
        if instance_id in instance_ids:
            patch = row.get("model_patch")
            if not isinstance(patch, str):
                raise ValueError(f"model_patch must be a string: {instance_id}")
            selected[instance_id] = patch
    if set(selected) != set(instance_ids):
        raise ValueError(f"missing requested predictions: {sorted(set(instance_ids) - set(selected))}")
    return selected


def _safe_path(root: Path, path: Path) -> Path:
    current = root
    for component in path.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"symlink is not allowed in grading artifacts: {current}")
    return path


def _archive(root: Path, source: Path, destination: Path) -> None:
    _safe_path(root, source)
    if source.exists():
        content, _ = _read_bound(source)
        with destination.open("xb") as stream:
            stream.write(content)


def _error(exc: Exception) -> dict:
    return {"status": "error", "resolved": None, "patch_applied": None, "reason": "grading_error",
            "error": f"{type(exc).__name__}: {exc}", "tests": {}}


def _fingerprint(metadata: os.stat_result) -> tuple:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns, metadata.st_mode)


def _read_bound(path: Path) -> tuple[bytes, tuple]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"expected a regular non-symlink file: {path}")
    with path.open("rb") as stream:
        content = stream.read()
        after = os.fstat(stream.fileno())
    if _fingerprint(before) != _fingerprint(after) or _fingerprint(after) != _fingerprint(path.lstat()):
        raise ValueError(f"file changed while reading: {path}")
    return content, _fingerprint(after)


def _check_inputs(inputs: dict[Path, tuple[bytes, tuple]]) -> None:
    for path, expected in inputs.items():
        try:
            unchanged = _read_bound(path) == expected
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            raise ValueError(f"predictions or submitted snapshot changed during grading: {path}")


def _validate_summary(overall: dict, instance_ids: list[str]) -> None:
    requested = set(instance_ids)
    for field in ("submitted_ids", "completed_ids", "resolved_ids", "unresolved_ids", "error_ids",
                  "incomplete_ids", "infra_failure_ids", "empty_patch_ids", "ambiguous_failure_ids"):
        values = overall.get(field, [])
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            if len(values) != len(set(values)) or not set(values) <= requested:
                raise ValueError(f"official {field} contains duplicate or unrequested IDs")
    reasons = overall.get("failure_reasons", {})
    if isinstance(reasons, dict) and not set(reasons) <= requested:
        raise ValueError("official failure_reasons contains unrequested IDs")


def _has_execution_summary(output):
    for line in output.splitlines():
        text = line.strip().strip("=").strip()
        summary = re.fullmatch(r"([0-9]+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)(?:, [0-9]+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?))*)(?: in [0-9.]+(?:s| seconds?)(?: \([0-9:]+\))?)?", text)
        if summary and re.search(r"\b[1-9][0-9]* (?:passed|failed)\b", summary[1]):
            return True
        if re.fullmatch(r"Ran [1-9][0-9]* tests? in [0-9.]+s", text):
            return True
        if re.fullmatch(r"\S+ (?:PASSED|FAILED)(?:\s+\[\s*[0-9]+%\])?", text):
            return True
    return False


def _validate_evidence(overall: dict, report: dict | None, instance_id: str,
                       patch: str, verdict: dict, archived: Path) -> None:
    empty = not patch.strip()
    if report is not None and set(report) != {instance_id}:
        raise ValueError("official per-instance report contains unexpected instance IDs")
    if instance_id in overall.get("resolved_ids", []) and instance_id in overall.get("unresolved_ids", []):
        raise ValueError("official resolved_ids and unresolved_ids overlap")
    if not empty and instance_id in overall.get("empty_patch_ids", []):
        raise ValueError("official report marks a nonempty patch as empty")
    submitted_patch = archived / "harness.patch.diff"
    if not empty or submitted_patch.exists():
        if submitted_patch.read_bytes() != patch.encode("utf-8"):
            raise ValueError("harness evaluated a different patch")
    if empty:
        if report is not None:
            details = report[instance_id]
            if not isinstance(details, dict) or any(
                details.get(field, False) is not False
                for field in ("resolved", "patch_successfully_applied", "infra_failure")
            ):
                raise ValueError("official per-instance verdict contradicts an empty patch")
        return
    details = report[instance_id]
    for field in ("infra_failure", "patch_exists", "patch_is_None"):
        if field in details and not isinstance(details[field], bool):
            raise ValueError(f"official {field} must be boolean")
    if details.get("patch_exists") is False or details.get("patch_is_None") is True:
        raise ValueError("official patch flags contradict the submitted patch")
    for group in verdict["tests"]:
        cases = details["tests_status"][group]
        for status in ("success", "failure"):
            names = cases[status]
            if any(not name.strip() for name in names) or len(names) != len(set(names)):
                raise ValueError(f"official {group} contains empty or duplicate test names")
        if set(cases["success"]) & set(cases["failure"]):
            raise ValueError(f"official {group} has conflicting test results")
    if not verdict["patch_applied"]:
        return
    counts = verdict["tests"].values()
    if not sum(count["passed"] + count["failed"] for count in counts):
        raise ValueError("official test sets are all empty")
    if not verdict["resolved"] and not any(count["failed"] for count in counts):
        raise ValueError("unresolved verdict conflicts with passing test results")
    output = (archived / "test_output.txt").read_bytes().decode("utf-8", errors="replace")
    if not output.strip():
        raise ValueError("official test_output.txt is empty")
    local_reason = details.get("failure_reason") or ""
    if not isinstance(local_reason, str):
        raise ValueError("official failure_reason must be a string")
    plain_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    zero_summary = re.search(r"(?im)^\s*(?:=+\s*)?(?:no tests (?:ran|collected)|collected\s+0\s+(?:items?|tests?)|ran\s+0\s+tests?)\b", plain_output)
    if zero_summary and not _has_execution_summary(plain_output):
        raise ValueError("test log has only zero-collection evidence, inconsistent with reported tests")


def grade_predictions(
    predictions_path: Path,
    instance_ids: list[str],
    run_id: str,
    dataset: str,
    harness_dir: Path | None,
    task_repo: Path | None,
    workers: int,
    log: Callable[[str], None],
) -> tuple[dict[str, dict], str]:
    """Return (verdicts keyed by instance ID, unique UUID-bearing harness run ID).

    Each verdict has status (passed/failed/error), resolved and patch_applied
    (bool or None), error, reason and test counts. Persist every attempt under
    predictions_path.parent/official/<harness_run_id>/, including grade.json,
    harness.log, filtered read-only predictions.jsonl and copied reports.
    The manifest records input hashes, command, exit code and report locations.
    Invalid IDs, run names or worker counts raise ValueError before launch. Input,
    harness and report failures return status=error with both booleans unknown;
    per-instance report errors do not discard other verdicts. Artifact-directory
    creation or persistence failures propagate rather than claim a saved grade.
    KeyboardInterrupt propagates after saving interrupted error verdicts.
    """
    from .repair.judge import _parse_verdict, _write_json

    _validate_arguments(instance_ids, run_id, dataset, workers)
    candidate = Path(predictions_path).expanduser().absolute()
    source = candidate.parent.resolve() / candidate.name
    workdir = Path(harness_dir or ".").expanduser().resolve()
    source_error = None
    try:
        original, source_identity = _read_bound(candidate)
    except Exception as exc:
        source_error = exc
    harness_run_id = f"{run_id}-grade-{uuid.uuid4().hex}"
    attempt = _safe_path(source.parent, source.parent / "official" / harness_run_id)
    attempt.mkdir(parents=True)
    report_root = workdir / "logs/evaluation" / harness_run_id
    snapshot = attempt / "predictions.jsonl"
    harness_log = attempt / "harness.log"
    harness_log.touch()
    verdicts = {instance_id: _error(RuntimeError("grading did not complete")) for instance_id in instance_ids}
    result = {
        "schema_version": 1, "run_id": run_id, "harness_run_id": harness_run_id,
        "dataset": dataset, "instance_ids": instance_ids, "predictions_path": str(source),
        "requested_predictions_path": str(candidate),
        "snapshot_path": str(snapshot), "attempt_dir": str(attempt),
        "harness_report_dir": str(report_root), "harness_log": str(harness_log),
        "started_at": datetime.now(timezone.utc).isoformat(), "verdicts": verdicts,
    }
    started = time.monotonic()
    try:
        if source_error:
            raise source_error
        result["predictions_sha256"] = hashlib.sha256(original).hexdigest()
        selected = _select_predictions(original, instance_ids)
        submitted = "".join(json.dumps({
            "instance_id": instance_id, "model_name_or_path": harness_run_id,
            "model_patch": selected[instance_id],
        }, ensure_ascii=False) + "\n" for instance_id in instance_ids).encode("utf-8")
        snapshot.write_bytes(submitted)
        snapshot.chmod(0o444)
        inputs = {candidate: (original, source_identity), snapshot: _read_bound(snapshot)}
        if inputs[snapshot][0] != submitted:
            raise ValueError("submitted snapshot changed while creating it")
        result["snapshot_sha256"] = hashlib.sha256(submitted).hexdigest()
        _write_json(attempt / "grade.json", result)
        executable = shutil.which("swebench")
        if not executable:
            raise RuntimeError("swebench CLI not found on PATH")
        workdir.mkdir(parents=True, exist_ok=True)
        _safe_path(workdir, report_root).mkdir(parents=True)
        command = [executable, "eval", HARNESS_ALIASES.get(dataset, dataset), "-p", str(snapshot),
                   "--run-id", harness_run_id, "-j", str(workers)]
        if task_repo:
            command += ["--task-repo", str(Path(task_repo).expanduser().resolve())]
        for instance_id in instance_ids:
            command += ["-i", instance_id]
        result["command"] = command
        log("judging with: " + " ".join(command))
        _check_inputs(inputs)
        with harness_log.open("w", encoding="utf-8") as output:
            process = subprocess.run(command, cwd=workdir, stdout=output, stderr=subprocess.STDOUT, check=False)
        result["harness_exit_code"] = process.returncode
        _archive(workdir, report_root / "results.json", attempt / "results.json")
        archive_errors = {}
        for instance_id in instance_ids:
            try:
                archived = _safe_path(attempt, attempt / "reports" / instance_id)
                archived.mkdir(parents=True)
                for name in ("report.json", "test_output.txt", "run_instance.log", "patch.diff"):
                    evidence = report_root / harness_run_id / instance_id / name
                    _archive(workdir, evidence, archived / ("harness.patch.diff" if name == "patch.diff" else name))
            except Exception as exc:
                archive_errors[instance_id] = exc
        _check_inputs(inputs)
        if process.returncode != 0:
            raise RuntimeError(f"swebench exited with code {process.returncode}; see {harness_log}")
        overall = _json_object((attempt / "results.json").read_bytes())
        _validate_summary(overall, instance_ids)
        for instance_id in instance_ids:
            try:
                if instance_id in archive_errors:
                    raise archive_errors[instance_id]
                report_path = attempt / "reports" / instance_id / "report.json"
                report = _json_object(report_path.read_bytes()) if report_path.exists() else None
                verdict = {**_parse_verdict(
                    overall, report, instance_id, empty_patch=not selected[instance_id].strip(),
                ), "error": ""}
                _validate_evidence(overall, report, instance_id, selected[instance_id], verdict, report_path.parent)
                verdicts[instance_id] = verdict
            except Exception as exc:
                verdicts[instance_id] = _error(exc)
        _check_inputs(inputs)
    except Exception as exc:
        verdicts.update({instance_id: _error(exc) for instance_id in instance_ids})
    except KeyboardInterrupt:
        verdicts.update({instance_id: {
            **_error(RuntimeError("Official grading interrupted")), "reason": "interrupted",
        } for instance_id in instance_ids})
        raise
    finally:
        result.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      wall_seconds=round(time.monotonic() - started, 3))
        _write_json(attempt / "grade.json", result)
    log(f"official grade: {attempt / 'grade.json'}; harness log: {harness_log}; reports: {report_root}")
    return verdicts, harness_run_id
