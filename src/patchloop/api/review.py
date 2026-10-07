"""Bind human decisions to saved repair evidence without changing repair results."""

import hashlib
import json
from pathlib import Path

from .models import ReviewEvidence, ReviewRecord, ReviewState, TaskRecord


class ReviewConflict(ValueError):
    pass


class ReviewNotFound(LookupError):
    pass


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value) -> bytes:
    # Python dictionary equality considers True == 1 and False == 0.
    # Serialized equality preserves JSON types when checking saved evidence.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _read_artifacts(root: Path) -> dict[str, bytes]:
    contents = {}
    for name in ("task.json", "summary.json", "validation.json", "patch.diff"):
        path = root / name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("repair evidence is missing or outside its artifact directory")
        contents[name] = path.read_bytes()
    return contents


def _snapshot(task: TaskRecord) -> tuple[ReviewEvidence, bool]:
    if task.status not in ("completed", "interrupted", "error"):
        raise ValueError("task has not finished")
    if task.cleanup_pending:
        raise ValueError("task cleanup is not complete")
    if task.artifacts_dir is None or task.result is None:
        raise ValueError("task has no saved repair evidence")
    root = Path(task.artifacts_dir).resolve()
    contents = _read_artifacts(root)
    request, summary, validation = (json.loads(contents[name]) for name in (
        "task.json", "summary.json", "validation.json",
    ))
    if not all(isinstance(value, dict) for value in (request, summary, validation)):
        raise ValueError("repair evidence must contain JSON objects")
    # Compare the on-disk JSON representation (tuples become arrays).
    expected = json.loads(json.dumps(task.to_repair_task().to_dict()))
    if _json_bytes(request) != _json_bytes(expected):
        raise ValueError("saved task differs from the task record")
    verification = request.get("verification")
    if not isinstance(verification, dict) or verification.get("command") != task.verification_command:
        raise ValueError("saved verification command differs from the task record")
    if _json_bytes(summary) != _json_bytes(task.result) or summary.get("task_id") != str(task.task_id):
        raise ValueError("repair summary differs from the task record")
    if summary.get("execution_status") != task.status or summary.get("validation_status") != task.validation_status:
        raise ValueError("repair statuses differ from the task record")
    patch = contents["patch.diff"].decode("utf-8")
    if not patch.strip():
        raise ValueError("empty patch cannot be reviewed")
    if summary.get("patch") != patch:
        raise ValueError("patch differs from the saved repair summary")
    patch_hash = _digest(contents["patch.diff"])
    if (_json_bytes(summary.get("validation")) != _json_bytes(validation) or validation.get("status") != task.validation_status
            or validation.get("patch_sha256") != patch_hash):
        raise ValueError("validation does not match the saved patch and task result")
    profile = {"base_sha": task.base_sha, "command": task.verification_command,
               "profile_id": verification.get("id")}
    if not isinstance(profile["profile_id"], str):
        raise ValueError("verification profile has no identifier")
    if any(validation.get(key) != value for key, value in profile.items()):
        raise ValueError("validation uses a different base or verification profile")
    baseline, candidate = validation.get("baseline"), validation.get("candidate")
    for report in (baseline, candidate):
        if report is not None and (not isinstance(report, dict)
                or any(report.get(key) != value for key, value in profile.items())):
            raise ValueError("baseline or candidate uses a different verification profile")
    if candidate is not None and candidate.get("patch_sha256") != patch_hash:
        raise ValueError("candidate validation uses a different patch")

    def confirmed(report: dict | None, status: str, exit_code: int) -> bool:
        return (isinstance(report, dict) and report.get("status") == status
                and type(report.get("exit_code")) is int and report["exit_code"] == exit_code
                and report.get("error") == "")

    can_accept = (task.status in ("completed", "interrupted") and not task.error
                  and summary.get("error") == "" and confirmed(validation, "passed", 0)
                  and confirmed(baseline, "failed", 1) and confirmed(candidate, "passed", 0))
    if _read_artifacts(root) != contents:
        raise ValueError("repair evidence changed while it was being read")
    record_bytes = _json_bytes(task.model_dump(mode="json"))
    return ReviewEvidence(
        base_sha=task.base_sha, patch_sha256=patch_hash,
        validation_sha256=_digest(contents["validation.json"]), task_sha256=_digest(contents["task.json"]),
        summary_sha256=_digest(contents["summary.json"]), record_sha256=_digest(record_bytes),
    ), can_accept


def review_state(task: TaskRecord, latest: ReviewRecord | None) -> ReviewState:
    state = ReviewState(task_id=task.task_id, revision=latest.revision if latest else 0, latest_review=latest)
    try:
        state.evidence, state.can_accept = _snapshot(task)
        state.can_reject = True
        if not state.can_accept:
            state.reason = "acceptance requires a failed baseline and a verified passing candidate"
    except (OSError, ValueError, RuntimeError) as exc:
        state.reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else "repair evidence is unavailable or unreadable"
    if latest:
        if state.evidence is None or latest.evidence != state.evidence:
            state.status = "stale"
            state.reason = "saved review no longer matches current repair evidence; " + (state.reason or "review the current evidence again")
        else:
            state.status = latest.decision
    return state
