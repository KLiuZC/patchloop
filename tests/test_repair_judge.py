import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchloop.repair import judge as judging


IID = "pytest-dev__pytest-7205"
PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-bad\n+fixed\n"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def saved_attempt(tmp_path, patch=PATCH):
    root = tmp_path / "repair"
    root.mkdir()
    write_json(root / "task.json", {"instance_id": IID, "base_commit": "a" * 40, "image": "test:latest"})
    write_json(root / "summary.json", {"task_id": IID, "execution_status": "completed", "patch": patch,
                                       "validation_status": "passed", "cost": 0.15})
    write_json(root / "validation.json", {"status": "passed", "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()})
    (root / "patch.diff").write_text(patch)
    (root / "events.jsonl").write_text('{"type": "run_finished"}\n')
    return root


def fake_harness(monkeypatch, *, resolved=False, failure_reason="no_tests_collected"):
    calls = []
    monkeypatch.setattr(judging.shutil, "which", lambda name: "/test/bin/swebench")

    def run(cmd, *, cwd, stdout, stderr, **kwargs):
        calls.append(cmd)
        run_id = cmd[cmd.index("--run-id") + 1]
        prediction = json.loads(Path(cmd[cmd.index("-p") + 1]).read_text())
        assert prediction["model_patch"] == PATCH
        assert cmd[cmd.index("-i") + 1] == IID
        assert "--task-repo" not in cmd
        report_dir = Path(cwd) / "logs/evaluation" / run_id
        write_json(report_dir / "results.json", {
            "submitted_ids": [IID], "completed_ids": [IID],
            "resolved_ids": [IID] if resolved else [],
            "unresolved_ids": [] if resolved else [IID],
            "error_ids": [], "incomplete_ids": [], "empty_patch_ids": [], "infra_failure_ids": [],
            "ambiguous_failure_ids": [IID] if failure_reason else [],
            "failure_reasons": {IID: failure_reason} if failure_reason else {},
        })
        instance_dir = report_dir / prediction["model_name_or_path"] / IID
        write_json(instance_dir / "report.json", {IID: {
            "resolved": resolved, "patch_successfully_applied": True,
            "infra_failure": False,
            "tests_status": {
                "FAIL_TO_PASS": {"success": ["bytes"], "failure": [] if resolved else ["quoted-string"]},
                "PASS_TO_PASS": {"success": ["regression"], "failure": []},
            },
        }})
        (instance_dir / "test_output.txt").write_text("visible reproduction passes, string assertion fails")
        (instance_dir / "patch.diff").write_text(prediction["model_patch"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(judging.subprocess, "run", run)
    return calls


def test_judge_keeps_visible_results_and_binds_official_report_to_patch(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path)
    originals = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    calls = fake_harness(monkeypatch)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert len(calls) == 1
    assert result["status"] == "failed" and result["resolved"] is False
    assert result["visible_validation_status"] == "passed"
    assert result["patch_sha256"] == hashlib.sha256(PATCH.encode()).hexdigest()
    assert result["base_sha"] == "a" * 40
    assert result["tests"]["FAIL_TO_PASS"] == {"passed": 1, "failed": 1}
    assert result["tests"]["PASS_TO_PASS"] == {"passed": 1, "failed": 0}
    assert result["harness_failure_reason"] == "no_tests_collected"
    assert result["ambiguous_failure"] is True
    for name, content in originals.items():
        assert (root / name).read_bytes() == content
    assert json.loads((root / "official.json").read_text()) == result
    attempt = root / result["attempt_dir"]
    assert (attempt / "patch.diff").read_text() == PATCH
    assert (attempt / "report.json").exists()
    assert (attempt / "results.json").exists()
    assert (attempt / "test_output.txt").exists()


def test_regrading_uses_new_id_and_preserves_previous_attempt(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path)
    calls = fake_harness(monkeypatch, resolved=True, failure_reason=None)
    first = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    old = root / first["attempt_dir"] / "result.json"
    contents = old.read_bytes()
    second = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert len(calls) == 2
    assert first["harness_run_id"] != second["harness_run_id"]
    assert old.read_bytes() == contents
    assert second["status"] == "passed" and second["resolved"] is True
    assert json.loads((root / "official.json").read_text()) == second


@pytest.mark.parametrize("fault", ["nonzero", "missing_report", "missing_overall", "bad_json", "null_instance", "null_tests", "invalid_reasons", "invalid_ids", "infra", "incomplete", "wrong_id", "contradiction"])
def test_judge_errors_do_not_become_failed_repairs(tmp_path, monkeypatch, fault):
    root = saved_attempt(tmp_path)
    fake_harness(monkeypatch)
    normal_run = judging.subprocess.run

    def run(cmd, **kwargs):
        process = normal_run(cmd, **kwargs)
        run_id = cmd[cmd.index("--run-id") + 1]
        d = Path(kwargs["cwd"]) / "logs/evaluation" / run_id
        report = d / run_id / IID / "report.json"
        if fault == "nonzero":
            return SimpleNamespace(returncode=1)
        if fault == "missing_report":
            report.unlink()
        elif fault == "missing_overall":
            (d / "results.json").unlink()
        elif fault == "bad_json":
            report.write_text("{incomplete")
        elif fault == "null_instance":
            write_json(report, {IID: None})
        elif fault == "null_tests":
            content = json.loads(report.read_text())
            content[IID]["tests_status"] = None
            write_json(report, content)
        else:
            overall = json.loads((d / "results.json").read_text())
            if fault == "invalid_reasons":
                overall["failure_reasons"] = []
            elif fault == "invalid_ids":
                overall["submitted_ids"] = None
            elif fault == "infra":
                overall["infra_failure_ids"] = [IID]
            elif fault == "incomplete":
                overall["incomplete_ids"] = [IID]
            elif fault == "wrong_id":
                overall["submitted_ids"] = ["unrelated__task-1"]
            elif fault == "contradiction":
                overall["resolved_ids"] = [IID]
            write_json(d / "results.json", overall)
        return process

    monkeypatch.setattr(judging.subprocess, "run", run)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert result["status"] == "error" and result["resolved"] is None
    assert result["error"]
    assert (root / result["attempt_dir"] / "result.json").exists()
    assert json.loads((root / "validation.json").read_text())["status"] == "passed"


def test_empty_patch_is_a_failed_submission_without_fake_test_counts(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path, patch="")
    monkeypatch.setattr(judging.shutil, "which", lambda name: "/test/bin/swebench")

    def run(cmd, **kwargs):
        rid = cmd[cmd.index("--run-id") + 1]
        write_json(Path(kwargs["cwd"]) / "logs/evaluation" / rid / "results.json", {
            "submitted_ids": [IID], "empty_patch_ids": [IID], "resolved_ids": [],
        })
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(judging.subprocess, "run", run)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert result["status"] == "failed" and result["resolved"] is False
    assert result["reason"] == "empty_patch" and result["tests"] == {}


@pytest.mark.parametrize("changed", ["patch", "visible_hash", "base"])
def test_changed_artifacts_rejected_before_harness_is_called(tmp_path, monkeypatch, changed):
    root = saved_attempt(tmp_path)
    monkeypatch.setattr(judging.subprocess, "run", lambda *a, **k: pytest.fail("must not run harness"))
    if changed == "patch":
        (root / "patch.diff").write_text(PATCH + "changed")
    else:
        v = json.loads((root / "validation.json").read_text())
        v["patch_sha256" if changed == "visible_hash" else "base_sha"] = "b" * 64
        write_json(root / "validation.json", v)
    with pytest.raises(ValueError, match="differ"):
        judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert not (root / "official.json").exists()


def test_mutation_during_grading_cannot_get_a_current_success(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path)
    fake_harness(monkeypatch, resolved=True)
    normal_run = judging.subprocess.run

    def run(cmd, **kwargs):
        process = normal_run(cmd, **kwargs)
        (root / "patch.diff").write_text(PATCH + "changed")
        return process

    monkeypatch.setattr(judging.subprocess, "run", run)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert result["status"] == "error" and result["resolved"] is None
    assert "changed during grading" in result["error"]
    assert (root / result["attempt_dir"] / "patch.diff").read_text() == PATCH


def test_mutation_while_snapshot_is_created_is_detected(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path)
    fake_harness(monkeypatch, resolved=True)
    original_mkdir = Path.mkdir
    changed = False

    def mkdir(path, *args, **kwargs):
        nonlocal changed
        original_mkdir(path, *args, **kwargs)
        if path.parent == root / "official" and not changed:
            changed = True
            new_patch = PATCH + "changed"
            (root / "patch.diff").write_text(new_patch)
            summary = json.loads((root / "summary.json").read_text())
            summary["patch"] = new_patch
            write_json(root / "summary.json", summary)
            write_json(root / "validation.json", {"status": "passed", "patch_sha256": hashlib.sha256(new_patch.encode()).hexdigest()})

    monkeypatch.setattr(Path, "mkdir", mkdir)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert changed
    assert result["status"] == "error" and result["resolved"] is None
    assert "changed" in result["error"]


def test_missing_harness_still_saves_error_artifact(tmp_path, monkeypatch):
    root = saved_attempt(tmp_path)
    monkeypatch.setattr(judging.shutil, "which", lambda name: None)
    result = judging.judge_repair(root, harness_dir=tmp_path / "harness")
    assert result["status"] == "error" and "not found" in result["error"]
    assert json.loads((root / "official.json").read_text())["resolved"] is None


@pytest.mark.parametrize("resolved,code", [(True, 0), (False, 1)])
def test_judge_cli_never_initializes_model(tmp_path, monkeypatch, resolved, code):
    from typer.testing import CliRunner
    import patchloop.cli as cli

    root = saved_attempt(tmp_path)
    fake_harness(monkeypatch, resolved=resolved)
    monkeypatch.setattr(cli, "LLM", lambda *a, **k: pytest.fail("judge-only must not initialize model"))
    monkeypatch.setattr(cli, "load_instances", lambda *a, **k: pytest.fail("judge-only must use saved task"))
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    result = CliRunner().invoke(cli.app, ["repair-judge", "--artifacts", str(root), "--harness-dir", str(tmp_path / "harness")])
    assert result.exit_code == code, result.output
    assert "visible=passed" in result.output
    assert "F2P=" in result.output and "P2P=" in result.output


def test_judge_cli_reports_harness_error_with_distinct_exit_code(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import patchloop.cli as cli

    root = saved_attempt(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(judging.shutil, "which", lambda name: None)
    result = CliRunner().invoke(cli.app, ["repair-judge", "--artifacts", str(root)])
    assert result.exit_code == 2
    assert "official=error" in result.output and "resolved=unknown" in result.output


@pytest.mark.parametrize("enabled,visible_status,execution", [(True, "passed", "completed"), (True, "failed", "completed"), (False, "passed", "completed"), (True, "inconclusive", "error")])
def test_repair_cli_can_grade_after_solving_without_feeding_back_hidden_tests(tmp_path, monkeypatch, enabled, visible_status, execution):
    from typer.testing import CliRunner
    import patchloop.cli as cli

    root = saved_attempt(tmp_path)
    fake_harness(monkeypatch)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a, **k: object())
    monkeypatch.setattr(cli, "load_instances", lambda *a: [{}])
    monkeypatch.setattr(cli.SWEbenchTask, "from_instance", lambda *a, **k: object())
    calls = []

    class Repair:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            calls.append("repair")
            return SimpleNamespace(execution_status=execution, validation_status=visible_status,
                                   stop_reason="no_tool_calls", artifacts_dir=root)

    monkeypatch.setattr(cli, "SWEbenchRepairRunner", Repair)
    actual_judge = cli.judge_repair

    def grade(*args, **kwargs):
        assert calls == ["repair"]
        calls.append("judge")
        return actual_judge(*args, **kwargs)

    monkeypatch.setattr(cli, "judge_repair", grade)
    args = ["repair-swebench", "-i", IID, "--harness-dir", str(tmp_path / "harness")]
    if enabled:
        args.append("--judge")
    result = CliRunner().invoke(cli.app, args)
    if enabled and execution != "error":
        assert result.exit_code == 1, result.output
        assert calls == ["repair", "judge"]
        assert "official=failed" in result.output
    else:
        assert calls == ["repair"]
        assert result.exit_code == (2 if execution == "error" else 0)


def test_new_repair_refuses_to_overwrite_saved_and_graded_artifacts(tmp_path, monkeypatch):
    from patchloop.repair.swebench import SWEbenchTask, SWEbenchRepairRunner

    root = saved_attempt(tmp_path)
    monkeypatch.setattr(SWEbenchRepairRunner, "_runner", lambda *a: pytest.fail("must reject before Docker starts"))
    original = (root / "patch.diff").read_bytes()
    task = SWEbenchTask(instance_id=IID, repo="pytest-dev/pytest", problem_statement="task", image="test:latest", base_commit="a" * 40)
    with pytest.raises(ValueError, match="new.*artifacts"):
        SWEbenchRepairRunner(task, root).run(object())
    assert (root / "patch.diff").read_bytes() == original
