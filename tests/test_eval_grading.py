import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchloop import eval_grading as grading


INSTANCE = "example__project-1"
SECOND = "example__project-2"
EXTRA = "other__project-3"
PATCH = "diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-bad\n+fixed\n"
DATASET = "SWE-bench/SWE-bench_Verified"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def predictions(tmp_path, patches=None):
    path = tmp_path / "predictions.jsonl"
    path.write_text("".join(json.dumps({
        "instance_id": instance_id, "model_name_or_path": "original-model", "model_patch": patch,
    }) + "\n" for instance_id, patch in (patches or {INSTANCE: PATCH}).items()), encoding="utf-8")
    return path


def grade(path, instance_ids=None, **kwargs):
    options = dict(predictions_path=path, instance_ids=[INSTANCE] if instance_ids is None else instance_ids, run_id="evaluation",
                   dataset=DATASET, harness_dir=path.parent / "harness", task_repo=None,
                   workers=2, log=lambda message: None)
    options.update(kwargs)
    return grading.grade_predictions(**options)


@pytest.fixture(autouse=True)
def no_real_harness(monkeypatch):
    monkeypatch.setattr(grading.shutil, "which", lambda name: "/fake/bin/swebench")
    monkeypatch.setattr(grading.subprocess, "run", lambda *args, **kwargs: pytest.fail("real harness forbidden"))


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(calls=[], mutate=None, returncode=0)

    def run(command, *, cwd, stdout, stderr, check):
        state.calls.append(command)
        state.snapshot = Path(command[command.index("-p") + 1])
        state.rows = [json.loads(line) for line in state.snapshot.read_text().splitlines()]
        state.run_id = command[command.index("--run-id") + 1]
        state.report_root = Path(cwd) / "logs/evaluation" / state.run_id
        state.overall_path = state.report_root / "results.json"
        state.instance_dirs = {}
        overall = {field: [] for field in (
            "submitted_ids", "completed_ids", "resolved_ids", "unresolved_ids", "empty_patch_ids",
            "error_ids", "incomplete_ids", "infra_failure_ids", "ambiguous_failure_ids",
        )}
        overall["failure_reasons"] = {}
        for row in state.rows:
            instance_id = row["instance_id"]
            overall["submitted_ids"].append(instance_id)
            if not row["model_patch"].strip():
                overall["empty_patch_ids"].append(instance_id)
                continue
            overall["completed_ids"].append(instance_id)
            overall["resolved_ids"].append(instance_id)
            directory = state.report_root / row["model_name_or_path"] / instance_id
            state.instance_dirs[instance_id] = directory
            write_json(directory / "report.json", {instance_id: {
                "resolved": True, "patch_successfully_applied": True, "infra_failure": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": ["test_fix"], "failure": []},
                    "PASS_TO_PASS": {"success": ["test_regression"], "failure": []},
                },
            }})
            (directory / "test_output.txt").write_text("test_fix PASSED\ntest_regression PASSED\n")
            (directory / "patch.diff").write_text(row["model_patch"], encoding="utf-8")
            (directory / "run_instance.log").write_text("applied patch; ran tests\n")
        write_json(state.overall_path, overall)
        stdout.write("fake harness finished\n")
        if state.mutate:
            state.mutate(state)
        return SimpleNamespace(returncode=state.returncode)

    monkeypatch.setattr(grading.subprocess, "run", run)
    return state


def test_batch_grades_only_requested_snapshot_and_preserves_evidence(tmp_path, harness):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH, EXTRA: PATCH})
    original = source.read_bytes()
    messages = []
    verdicts, run_id = grade(source, [INSTANCE, SECOND], task_repo=tmp_path / "tasks", log=messages.append)
    assert len(harness.calls) == 1
    assert set(verdicts) == {INSTANCE, SECOND}
    assert all(verdict["status"] == "passed" and verdict["resolved"] is True
               and verdict["patch_applied"] is True and not verdict["error"] for verdict in verdicts.values())
    assert harness.snapshot != source
    assert [row["instance_id"] for row in harness.rows] == [INSTANCE, SECOND]
    assert all(row["model_name_or_path"] == run_id for row in harness.rows)
    assert source.read_bytes() == original
    assert harness.calls[0][1:3] == ["eval", "verified"]
    assert harness.calls[0].count("-i") == 2
    assert harness.calls[0][harness.calls[0].index("--task-repo") + 1] == str(tmp_path / "tasks")
    attempt = source.parent / "official" / run_id
    saved = json.loads((attempt / "grade.json").read_text())
    assert saved["verdicts"] == verdicts
    assert saved["harness_run_id"] == run_id
    assert saved["harness_report_dir"] == str(harness.report_root)
    assert saved["predictions_sha256"] == hashlib.sha256(original).hexdigest()
    assert saved["snapshot_sha256"] == hashlib.sha256(harness.snapshot.read_bytes()).hexdigest()
    assert (attempt / "harness.log").read_text() == "fake harness finished\n"
    assert (attempt / "reports" / INSTANCE / "harness.patch.diff").read_text() == PATCH
    assert (attempt / "reports" / SECOND / "report.json").exists()
    assert any(str(attempt / "grade.json") in message for message in messages)


@pytest.mark.parametrize("target,restore", [("source", False), ("snapshot", False), ("source", True), ("snapshot", True)])
def test_changed_inputs_invalidate_entire_batch(tmp_path, harness, target, restore):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        changed = source if target == "source" else state.snapshot
        original = changed.read_bytes()
        changed.chmod(0o644)
        changed.write_bytes(original + b"\n")
        if restore:
            changed.write_bytes(original)

    harness.mutate = mutate
    verdicts, run_id = grade(source, [INSTANCE, SECOND])
    assert all(verdict["status"] == "error" and verdict["resolved"] is None
               and verdict["patch_applied"] is None and "changed" in verdict["error"]
               for verdict in verdicts.values())
    saved = json.loads((tmp_path / "official" / run_id / "grade.json").read_text())
    assert saved["verdicts"] == verdicts
    assert (tmp_path / "official" / run_id / "results.json").exists()


def test_snapshot_is_read_only_and_regrading_preserves_previous_attempt(tmp_path, harness):
    source = predictions(tmp_path)
    first, first_id = grade(source)
    assert harness.snapshot.stat().st_mode & 0o222 == 0
    first_directory = tmp_path / "official" / first_id
    saved = {path.relative_to(first_directory): path.read_bytes()
             for path in first_directory.rglob("*") if path.is_file()}
    second, second_id = grade(source)
    assert first_id != second_id
    assert len(first_id.rsplit("-", 1)[1]) == 32
    assert first[INSTANCE]["status"] == second[INSTANCE]["status"] == "passed"
    assert all((first_directory / name).read_bytes() == content for name, content in saved.items())


def test_mutation_before_launch_is_rejected(tmp_path, harness):
    source = predictions(tmp_path)

    def log(message):
        if message.startswith("judging with:"):
            source.write_bytes(source.read_bytes() + b"\n")

    verdicts, _ = grade(source, log=log)
    assert verdicts[INSTANCE]["status"] == "error"
    assert "changed" in verdicts[INSTANCE]["error"]
    assert not harness.calls


@pytest.mark.parametrize("fault", [
    "missing_patch", "different_patch", "missing_output", "empty_output", "blank_output", "no_collection",
    "empty_tests", "duplicate_tests", "conflicting_tests", "blank_test",
    "resolved_and_unresolved", "nonempty_marked_empty", "unresolved_all_passed", "invalid_infra_boolean",
    "invalid_patch_exists", "wrong_patch_exists", "wrong_patch_is_none",
])
def test_invalid_evidence_errors_only_the_affected_instance(tmp_path, harness, fault):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        directory = state.instance_dirs[INSTANCE]
        report = json.loads((directory / "report.json").read_text())
        verdict = report[INSTANCE]
        overall = json.loads(state.overall_path.read_text())
        if fault == "missing_patch":
            (directory / "patch.diff").unlink()
        elif fault == "different_patch":
            (directory / "patch.diff").write_text(PATCH + "\n")
        elif fault == "missing_output":
            (directory / "test_output.txt").unlink()
        elif fault in {"empty_output", "blank_output", "no_collection"}:
            output = {"empty_output": "", "blank_output": " \n\t", "no_collection": "collected 0 items\nno tests ran in 0.01s"}
            (directory / "test_output.txt").write_text(output[fault])
        elif fault == "collection_reason":
            overall["failure_reasons"][INSTANCE] = "no_tests_collected"
        elif fault == "empty_tests":
            for group in verdict["tests_status"].values():
                group["success"] = []
        elif fault == "duplicate_tests":
            verdict["tests_status"]["FAIL_TO_PASS"]["success"] = ["test_fix", "test_fix"]
        elif fault == "conflicting_tests":
            verdict["resolved"] = False
            overall["resolved_ids"].remove(INSTANCE)
            overall["unresolved_ids"].append(INSTANCE)
            verdict["tests_status"]["FAIL_TO_PASS"]["failure"] = ["test_fix"]
        elif fault == "blank_test":
            verdict["tests_status"]["FAIL_TO_PASS"]["success"] = [""]
        elif fault == "resolved_and_unresolved":
            overall["unresolved_ids"].append(INSTANCE)
        elif fault == "nonempty_marked_empty":
            overall["empty_patch_ids"].append(INSTANCE)
        elif fault == "unresolved_all_passed":
            verdict["resolved"] = False
            overall["resolved_ids"].remove(INSTANCE)
            overall["unresolved_ids"].append(INSTANCE)
        elif fault == "invalid_infra_boolean":
            verdict["infra_failure"] = 0
        elif fault == "invalid_patch_exists":
            verdict["patch_exists"] = "true"
        elif fault == "wrong_patch_exists":
            verdict["patch_exists"] = False
        elif fault == "wrong_patch_is_none":
            verdict["patch_is_None"] = True
        write_json(directory / "report.json", report)
        write_json(state.overall_path, overall)

    harness.mutate = mutate
    verdicts, run_id = grade(source, [INSTANCE, SECOND])
    assert verdicts[INSTANCE]["status"] == "error", verdicts[INSTANCE]
    assert verdicts[INSTANCE]["resolved"] is None and verdicts[INSTANCE]["patch_applied"] is None
    assert verdicts[INSTANCE]["error"]
    assert verdicts[SECOND]["status"] == "passed"
    assert json.loads((tmp_path / "official" / run_id / "grade.json").read_text())["verdicts"] == verdicts


@pytest.mark.parametrize("field", [
    "submitted_ids", "completed_ids", "resolved_ids", "unresolved_ids", "error_ids",
    "incomplete_ids", "infra_failure_ids", "empty_patch_ids", "ambiguous_failure_ids",
])
@pytest.mark.parametrize("value", [None, "not-a-list", [1], [INSTANCE, INSTANCE], [EXTRA]])
def test_invalid_summary_lists_invalidate_batch(tmp_path, harness, field, value):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        overall = json.loads(state.overall_path.read_text())
        overall[field] = value
        write_json(state.overall_path, overall)

    harness.mutate = mutate
    verdicts, _ = grade(source, [INSTANCE, SECOND])
    assert all(verdict["status"] == "error" and verdict["resolved"] is None for verdict in verdicts.values())


@pytest.mark.parametrize("field", ["error_ids", "incomplete_ids", "infra_failure_ids"])
def test_official_errors_remain_local_to_one_instance(tmp_path, harness, field):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        overall = json.loads(state.overall_path.read_text())
        overall[field] = [INSTANCE]
        write_json(state.overall_path, overall)

    harness.mutate = mutate
    verdicts, _ = grade(source, [INSTANCE, SECOND])
    assert verdicts[INSTANCE]["status"] == "error" and field in verdicts[INSTANCE]["error"]
    assert verdicts[SECOND]["status"] == "passed"


@pytest.mark.parametrize("fault", [
    "missing_report", "bad_report", "list_report", "missing_instance", "null_verdict", "string_resolved",
    "integer_applied", "missing_tests", "null_tests", "missing_f2p", "bad_p2p", "resolved_failed_test",
    "infra_failure", "not_submitted", "not_completed", "resolved_disagreement",
])
def test_malformed_per_instance_reports_do_not_become_failures(tmp_path, harness, fault):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        report_path = state.instance_dirs[INSTANCE] / "report.json"
        report = json.loads(report_path.read_text())
        details = report[INSTANCE]
        if fault == "missing_report":
            report_path.unlink()
            return
        if fault == "bad_report":
            report_path.write_text("{not-json")
            return
        if fault == "list_report":
            report = []
        elif fault == "missing_instance":
            report = {}
        elif fault == "null_verdict":
            report[INSTANCE] = None
        elif fault == "string_resolved":
            details["resolved"] = "false"
        elif fault == "integer_applied":
            details["patch_successfully_applied"] = 1
        elif fault == "missing_tests":
            details.pop("tests_status")
        elif fault == "null_tests":
            details["tests_status"] = None
        elif fault == "missing_f2p":
            details["tests_status"].pop("FAIL_TO_PASS")
        elif fault == "bad_p2p":
            details["tests_status"]["PASS_TO_PASS"]["success"] = [None]
        elif fault == "resolved_failed_test":
            details["tests_status"]["PASS_TO_PASS"]["failure"] = ["test_other"]
        elif fault == "infra_failure":
            details["infra_failure"] = True
        else:
            overall = json.loads(state.overall_path.read_text())
            field = {"not_submitted": "submitted_ids", "not_completed": "completed_ids",
                     "resolved_disagreement": "resolved_ids"}[fault]
            overall[field].remove(INSTANCE)
            write_json(state.overall_path, overall)
        write_json(report_path, report)

    harness.mutate = mutate
    verdicts, _ = grade(source, [INSTANCE, SECOND])
    assert verdicts[INSTANCE]["status"] == "error" and verdicts[INSTANCE]["resolved"] is None
    assert verdicts[SECOND]["status"] == "passed"


@pytest.mark.parametrize("fault", ["missing_cli", "launch", "timeout", "nonzero", "missing_overall", "bad_json", "list_json", "bad_reasons"])
def test_global_errors_leave_traceable_error_for_every_instance(tmp_path, monkeypatch, harness, fault):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})
    if fault == "missing_cli":
        monkeypatch.setattr(grading.shutil, "which", lambda name: None)
    elif fault in {"launch", "timeout"}:
        def run(*args, **kwargs):
            if fault == "timeout":
                raise grading.subprocess.TimeoutExpired("swebench", 1)
            raise OSError("cannot launch")
        monkeypatch.setattr(grading.subprocess, "run", run)
    elif fault == "nonzero":
        harness.returncode = 7
    else:
        def mutate(state):
            if fault == "missing_overall":
                state.overall_path.unlink()
            elif fault == "bad_json":
                state.overall_path.write_text("{")
            elif fault == "list_json":
                write_json(state.overall_path, [])
            else:
                overall = json.loads(state.overall_path.read_text())
                overall["failure_reasons"] = {INSTANCE: None}
                write_json(state.overall_path, overall)
        harness.mutate = mutate
    verdicts, run_id = grade(source, [INSTANCE, SECOND])
    assert all(verdict["status"] == "error" and verdict["resolved"] is None
               and verdict["patch_applied"] is None and verdict["error"] for verdict in verdicts.values())
    attempt = tmp_path / "official" / run_id
    assert (attempt / "harness.log").exists()
    saved = json.loads((attempt / "grade.json").read_text())
    assert saved["verdicts"] == verdicts and saved["harness_report_dir"]
    if fault == "nonzero":
        assert saved["harness_exit_code"] == 7
        assert (attempt / "reports" / INSTANCE / "report.json").exists()


@pytest.mark.parametrize("outcome", ["failed_tests", "not_applied", "empty", "whitespace", "one_empty_test_group"])
def test_valid_failures_and_empty_groups_are_not_grading_errors(tmp_path, harness, outcome):
    patch = "" if outcome == "empty" else " \n\t" if outcome == "whitespace" else PATCH
    source = predictions(tmp_path, {INSTANCE: patch})

    def mutate(state):
        if outcome in {"empty", "whitespace"}:
            assert not state.instance_dirs
            return
        directory = state.instance_dirs[INSTANCE]
        report = json.loads((directory / "report.json").read_text())
        details = report[INSTANCE]
        if outcome == "one_empty_test_group":
            details["tests_status"]["PASS_TO_PASS"]["success"] = []
        else:
            overall = json.loads(state.overall_path.read_text())
            overall["resolved_ids"] = []
            overall["unresolved_ids"] = [INSTANCE]
            write_json(state.overall_path, overall)
            details["resolved"] = False
            if outcome == "not_applied":
                details["patch_successfully_applied"] = False
                details.pop("tests_status")
                (directory / "test_output.txt").unlink()
            else:
                details["tests_status"]["FAIL_TO_PASS"] = {"success": [], "failure": ["test_fix"]}
                (directory / "test_output.txt").write_text("test_fix FAILED\ntest_regression PASSED")
        write_json(directory / "report.json", report)

    harness.mutate = mutate
    verdicts, _ = grade(source)
    verdict = verdicts[INSTANCE]
    assert verdict["status"] == ("passed" if outcome == "one_empty_test_group" else "failed"), verdict
    assert verdict["resolved"] is (outcome == "one_empty_test_group")
    assert verdict["patch_applied"] is (outcome in {"failed_tests", "one_empty_test_group"})
    assert not verdict["error"]
    if outcome in {"empty", "whitespace"}:
        assert verdict["reason"] == "empty_patch" and verdict["tests"] == {}


@pytest.mark.parametrize("options", [
    {"instance_ids": []}, {"instance_ids": [INSTANCE, INSTANCE]},
    {"instance_ids": [INSTANCE, INSTANCE.upper()]}, {"instance_ids": ["../escape"]},
    {"instance_ids": ["/absolute__task"]}, {"instance_ids": ["--option"]}, {"instance_ids": [None]},
    {"instance_ids": "example__project-1"}, {"run_id": "../escape"}, {"run_id": "/absolute"},
    {"run_id": "--option"}, {"run_id": ""}, {"run_id": "long" * 100},
    {"workers": 0}, {"workers": -1}, {"workers": True}, {"workers": 1.5},
    {"dataset": "--help"}, {"dataset": ""},
])
def test_invalid_arguments_fail_before_any_harness_or_artifacts(tmp_path, harness, options):
    source = predictions(tmp_path)
    with pytest.raises(ValueError):
        grade(source, **options)
    assert not harness.calls
    assert not (tmp_path / "official").exists()


@pytest.mark.parametrize("fault", ["duplicate", "missing_instance", "bad_patch", "null_patch", "bad_id", "missing_id", "list_row", "bad_json", "missing_file"])
def test_invalid_predictions_are_recorded_without_launch(tmp_path, harness, fault):
    source = predictions(tmp_path)
    row = json.loads(source.read_text())
    if fault == "duplicate":
        source.write_text(source.read_text() * 2)
    elif fault == "missing_instance":
        row["instance_id"] = SECOND
    elif fault in {"bad_patch", "null_patch"}:
        row["model_patch"] = 1 if fault == "bad_patch" else None
    elif fault == "bad_id":
        row["instance_id"] = "../outside"
    elif fault == "missing_id":
        row.pop("instance_id")
    elif fault == "list_row":
        row = []
    elif fault == "bad_json":
        source.write_text("{broken")
    elif fault == "missing_file":
        source.unlink()
    if fault not in {"duplicate", "bad_json", "missing_file"}:
        write_json(source, row)
    verdicts, run_id = grade(source)
    assert verdicts[INSTANCE]["status"] == "error" and verdicts[INSTANCE]["resolved"] is None
    assert not harness.calls
    assert json.loads((tmp_path / "official" / run_id / "grade.json").read_text())["verdicts"] == verdicts


def test_untrusted_model_paths_are_replaced_not_used(tmp_path, harness):
    source = predictions(tmp_path)
    row = json.loads(source.read_text())
    row["model_name_or_path"] = "../../escape"
    row["unneeded"] = "not forwarded"
    write_json(source, row)
    verdicts, run_id = grade(source)
    assert verdicts[INSTANCE]["status"] == "passed"
    assert harness.rows[0]["model_name_or_path"] == run_id
    assert set(harness.rows[0]) == {"instance_id", "model_name_or_path", "model_patch"}


@pytest.mark.parametrize("name", ["report.json", "patch.diff", "test_output.txt", "run_instance.log", "directory"])
def test_symlink_evidence_is_not_trusted(tmp_path, harness, name):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def mutate(state):
        directory = state.instance_dirs[INSTANCE]
        target = directory if name == "directory" else directory / name
        external = tmp_path / "external"
        target.rename(external)
        target.symlink_to(external, target_is_directory=name == "directory")

    harness.mutate = mutate
    verdicts, _ = grade(source, [INSTANCE, SECOND])
    assert verdicts[INSTANCE]["status"] == "error"
    assert "symlink" in verdicts[INSTANCE]["error"]
    assert verdicts[SECOND]["status"] == "passed"


def test_symlink_report_root_is_rejected_before_launch(tmp_path, harness):
    source = predictions(tmp_path)
    workdir = tmp_path / "harness"
    workdir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (workdir / "logs").symlink_to(external, target_is_directory=True)
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error"
    assert not harness.calls
    assert not list(external.iterdir())


def test_mutation_during_attempt_creation_is_rejected(tmp_path, monkeypatch, harness):
    source = predictions(tmp_path)
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path.parent == tmp_path / "official":
            source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir)
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error" and "changed" in verdicts[INSTANCE]["error"]
    assert not harness.calls


@pytest.mark.parametrize("target", ["predictions", "overall", "report"])
def test_duplicate_json_fields_are_rejected(tmp_path, harness, target):
    source = predictions(tmp_path)
    if target == "predictions":
        source.write_text(source.read_text().replace('"model_patch":', '"model_patch": "", "model_patch":'))
    else:
        def mutate(state):
            path = state.overall_path if target == "overall" else state.instance_dirs[INSTANCE] / "report.json"
            field = "resolved_ids" if target == "overall" else "resolved"
            content = path.read_text().replace(f'"{field}":', f'"{field}": null, "{field}":')
            path.write_text(content)
        harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error"
    assert "duplicate" in verdicts[INSTANCE]["error"]
    if target == "predictions":
        assert not harness.calls


@pytest.mark.parametrize("fault", ["missing_marker", "resolved", "error", "applied_report", "mismatched_patch"])
def test_empty_patch_still_requires_consistent_official_evidence(tmp_path, harness, fault):
    source = predictions(tmp_path, {INSTANCE: ""})

    def mutate(state):
        overall = json.loads(state.overall_path.read_text())
        if fault == "missing_marker":
            overall["empty_patch_ids"] = []
        elif fault == "resolved":
            overall["resolved_ids"] = [INSTANCE]
        elif fault == "error":
            overall["error_ids"] = [INSTANCE]
        else:
            directory = state.report_root / state.run_id / INSTANCE
            directory.mkdir(parents=True)
            if fault == "applied_report":
                write_json(directory / "report.json", {INSTANCE: {
                    "resolved": True, "patch_successfully_applied": True,
                }})
            else:
                (directory / "patch.diff").write_text(PATCH)
        write_json(state.overall_path, overall)

    harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error"
    assert verdicts[INSTANCE]["resolved"] is None


def test_per_instance_ambiguous_reason_does_not_override_reported_failure(tmp_path, harness):
    source = predictions(tmp_path)

    def mutate(state):
        path = state.instance_dirs[INSTANCE] / "report.json"
        report = json.loads(path.read_text())
        report[INSTANCE]["resolved"] = False
        report[INSTANCE]["failure_reason"] = "no_tests_collected"
        report[INSTANCE]["tests_status"]["FAIL_TO_PASS"] = {"success": [], "failure": ["test_fix"]}
        write_json(path, report)
        overall = json.loads(state.overall_path.read_text())
        overall["resolved_ids"] = []
        overall["unresolved_ids"] = [INSTANCE]
        write_json(state.overall_path, overall)

    harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "failed"
    assert verdicts[INSTANCE]["resolved"] is False and not verdicts[INSTANCE]["error"]


def test_zero_collection_text_inside_failed_test_output_is_not_an_infra_verdict(tmp_path, harness):
    source = predictions(tmp_path)
    def mutate(state):
        path = state.instance_dirs[INSTANCE] / "report.json"
        report = json.loads(path.read_text())
        report[INSTANCE]["resolved"] = False
        report[INSTANCE]["tests_status"]["FAIL_TO_PASS"] = {"success": [], "failure": ["test_fix"]}
        write_json(path, report)
        overall = json.loads(state.overall_path.read_text())
        overall.update(resolved_ids=[], unresolved_ids=[INSTANCE], ambiguous_failure_ids=[INSTANCE], failure_reasons={INSTANCE: "no_tests_collected"})
        write_json(state.overall_path, overall)
        (state.instance_dirs[INSTANCE] / "test_output.txt").write_text("E assert 'no tests collected' == actual\n==== 1 failed, 1 passed in 0.1s ====\n")
    harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "failed" and verdicts[INSTANCE]["resolved"] is False
    assert verdicts[INSTANCE]["ambiguous_failure"] is True


@pytest.mark.parametrize("output", [
    "collected 0 items / 1 error\n==== 1 error in 0.1s ====\n",
    "collected 0 items\nE assert '1 passed' in result\nno tests ran in 0.1s\n",
    "\x1b[31mcollected 0 items\x1b[0m\nno tests ran in 0.1s\n",
])
def test_collection_errors_and_assertion_text_are_not_execution_evidence(tmp_path, harness, output):
    source = predictions(tmp_path)
    harness.mutate = lambda state: (state.instance_dirs[INSTANCE] / "test_output.txt").write_text(output)
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error" and verdicts[INSTANCE]["resolved"] is None


def test_report_for_unrequested_instance_is_not_accepted(tmp_path, harness):
    source = predictions(tmp_path)

    def mutate(state):
        path = state.instance_dirs[INSTANCE] / "report.json"
        report = json.loads(path.read_text())
        report[EXTRA] = report[INSTANCE]
        write_json(path, report)

    harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error"


def test_loading_grading_module_does_not_import_repair(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if "repair" in name:
            pytest.fail("repair imports must be deferred until grade_predictions is called")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec = importlib.util.spec_from_file_location("patchloop._grading_import_check", grading.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.grade_predictions)


def test_interrupt_saves_unknown_verdicts_before_propagating(tmp_path, monkeypatch, harness):
    source = predictions(tmp_path, {INSTANCE: PATCH, SECOND: PATCH})

    def run(*args, **kwargs):
        kwargs["stdout"].write("interrupted harness\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(grading.subprocess, "run", run)
    with pytest.raises(KeyboardInterrupt):
        grade(source, [INSTANCE, SECOND])
    attempts = list((tmp_path / "official").iterdir())
    assert len(attempts) == 1
    saved = json.loads((attempts[0] / "grade.json").read_text())
    assert all(verdict["status"] == "error" and verdict["reason"] == "interrupted"
               and verdict["resolved"] is None and verdict["patch_applied"] is None
               for verdict in saved["verdicts"].values())
    assert (attempts[0] / "harness.log").read_text() == "interrupted harness\n"


def test_existing_harness_run_is_never_reused(tmp_path, monkeypatch, harness):
    source = predictions(tmp_path)
    run_id = "evaluation-grade-" + "a" * 32
    monkeypatch.setattr(grading.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    old_report = tmp_path / "harness/logs/evaluation" / run_id / "results.json"
    write_json(old_report, {"old": "report"})
    original = old_report.read_bytes()
    verdicts, actual_id = grade(source)
    assert actual_id == run_id
    assert verdicts[INSTANCE]["status"] == "error"
    assert old_report.read_bytes() == original
    assert not harness.calls


@pytest.mark.parametrize("target", ["source", "overall"])
def test_symlink_inputs_and_summary_fail_closed(tmp_path, harness, target):
    source = predictions(tmp_path)
    if target == "source":
        real = tmp_path / "real-predictions.jsonl"
        source.rename(real)
        source.symlink_to(real)
    else:
        def mutate(state):
            real = tmp_path / "external-results.json"
            state.overall_path.rename(real)
            state.overall_path.symlink_to(real)
        harness.mutate = mutate
    verdicts, _ = grade(source)
    assert verdicts[INSTANCE]["status"] == "error"
    assert "symlink" in verdicts[INSTANCE]["error"]
    if target == "source":
        assert not harness.calls


def test_retargeting_source_directory_invalidates_original_binding(tmp_path, harness):
    original_directory = tmp_path / "original"
    original_directory.mkdir()
    source = predictions(original_directory)
    alternate_directory = tmp_path / "alternate"
    alternate_directory.mkdir()
    predictions(alternate_directory)
    alias = tmp_path / "alias"
    alias.symlink_to(original_directory, target_is_directory=True)

    def mutate(state):
        alias.unlink()
        alias.symlink_to(alternate_directory, target_is_directory=True)

    harness.mutate = mutate
    verdicts, _ = grade(alias / source.name)
    assert verdicts[INSTANCE]["status"] == "error"
    assert "changed" in verdicts[INSTANCE]["error"]
