from patchloop.repair.swebench import SWEbenchTask, _profile_status
import json

import pytest

from patchloop.loop import RunResult
from patchloop.repair.artifacts import sha256_text
from patchloop.repair.swebench import SWEbenchRepairRunner
from patchloop.sandbox import LocalRunner


def test_swebench_task_keeps_issue_public_and_test_data_private():
    task = SWEbenchTask.from_instance(
        {
            "instance_id": "pytest-dev__pytest-7205",
            "repo": "pytest-dev/pytest",
            "problem_statement": "Fix fixture output.",
            "image": "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-7205",
            "base_commit": "abc123",
            "FAIL_TO_PASS": ["hidden::test_target"],
            "test_patch": "secret patch",
        },
        verification_command="pytest -q testing/test_setuponly.py::test_show_fixture_action_with_bytes",
    )

    assert task.image.endswith(":latest")
    assert task.workdir == "/testbed"
    assert task.base_commit == "abc123"
    assert "Fix fixture output." in task.prompt
    assert "hidden::test_target" not in task.prompt
    assert "secret patch" not in task.prompt
    assert task.to_dict()["instance_id"] == "pytest-dev__pytest-7205"


def test_invalid_test_selector_is_inconclusive_not_a_patch_failure():
    output = "no tests ran\nERROR: not found: testing/test_missing.py::test_missing"
    assert _profile_status(output, 4) == "inconclusive"
    assert _profile_status("1 failed", 1) == "failed"
    assert _profile_status("1 passed", 0) == "passed"


@pytest.mark.parametrize("code", [2, 3, 4, 5, 124, 126, 127, 137, -9])
def test_profile_setup_errors_are_inconclusive(code):
    assert _profile_status("command could not complete verification", code) == "inconclusive"


@pytest.mark.parametrize("failed_start", [2, 3])
def test_validation_startup_error_keeps_patch_and_finishes_report(tmp_path, monkeypatch, failed_start):
    import patchloop.repair.swebench as module

    task = SWEbenchTask("pytest-dev__pytest-7205", "pytest-dev/pytest", "fix", "fake", "a" * 40,
                        verification_command="exit 1")
    root = tmp_path / "artifacts"
    repair = SWEbenchRepairRunner(task, root)
    starts = 0

    def factory(**kwargs):
        nonlocal starts
        starts += 1
        if starts == failed_start:
            raise RuntimeError("container unavailable")
        return LocalRunner(tmp_path)

    monkeypatch.setattr(repair, "_runner", factory)
    monkeypatch.setattr(module, "inspect_image", lambda *a: {"id": "pinned"})
    monkeypatch.setattr(module, "prepare_swebench_environment", lambda *a: None)
    monkeypatch.setattr(repair, "_check_base", lambda runner: None)
    monkeypatch.setattr(module, "run", lambda *a, **kw: RunResult(
        run_id=task.instance_id, finished=True, stop_reason="no_tool_calls", patch="saved patch", prompt_tokens=20,
    ))
    result = repair.run(object())
    assert result.execution_status == "completed"
    assert result.validation_status == "inconclusive"
    assert result.patch == "saved patch" and result.prompt_tokens == 20
    validation = json.loads((root / "validation.json").read_text())
    assert validation["patch_sha256"] == sha256_text(result.patch)
    assert "container unavailable" in validation["error"]
    if failed_start == 3:
        assert validation["baseline"]["status"] == "failed"
    summary = json.loads((root / "summary.json").read_text())
    assert summary["validation"] == validation
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == "run_finished"
