import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from patchloop.repair.contracts import RepairTask, VerificationProfile
from patchloop.repair.runner import RepairRunner
from patchloop.sandbox import LocalRunner


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class ScriptedLLM:
    provider = "fake"
    model = "fake"

    def __init__(self, replies):
        self.replies = iter(replies)

    def chat(self, messages, tools):
        return next(self.replies), SimpleNamespace(prompt_tokens=10, completion_tokens=5)


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def response(*calls):
    return SimpleNamespace(content=None, tool_calls=list(calls))


def test_repair_runner_validates_a_patch_from_a_clean_base(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "calc.py").write_text("def parse_version(s):\n    return tuple(int(p) for p in s.split('.'))\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import parse_version\n\n"
        "def test_version():\n    assert parse_version('v1.2') == (1, 2)\n"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    fixed_source = "def parse_version(s):\n    return tuple(int(p) for p in s.removeprefix('v').split('.'))\n"
    edit = json.dumps({"cmd": f"cat > calc.py <<'EOF'\n{fixed_source}EOF"})
    task = RepairTask(
        task_id="toy-version-parser-001",
        repo_path=tmp_path,
        base_sha=base_sha,
        goal="Support a leading v in version strings.",
        verification=VerificationProfile(id="pytest", command=f"{sys.executable} -m pytest -q"),
        explicit_finish=True,
    )
    llm = ScriptedLLM([
        response(tool_call("edit", "bash", edit)),
        response(tool_call("test", "bash", json.dumps({"cmd": "pytest -q"}))),
        response(tool_call("finish", "finish", json.dumps({"summary": "Fixed version prefix handling."}))),
    ])

    result = RepairRunner(
        task,
        artifacts_root=tmp_path.parent / "repair-artifacts",
        runner_factory=lambda root, workdir: LocalRunner(root / workdir),
    ).run(llm)

    assert result.execution_status == "completed"
    assert result.validation_status == "passed"
    assert result.review_status == "pending"
    assert result.patch
    assert result.validation is not None and result.validation.exit_code == 0
    assert result.validation.baseline is not None
    assert result.validation.baseline.status == "failed"
    assert result.validation.baseline.exit_code == 1
    validation = json.loads((tmp_path.parent / "repair-artifacts" / "validation.json").read_text())
    assert validation["baseline"]["status"] == "failed"
    assert validation["baseline"]["exit_code"] == 1
    assert validation["candidate"]["status"] == "passed"
    assert (tmp_path / "calc.py").read_text().startswith("def parse_version(s):\n    return tuple(int(p)")
    assert git(tmp_path, "status", "--porcelain").stdout == ""
    for name in ["task.json", "events.jsonl", "patch.diff", "validation.json", "summary.json"]:
        assert (tmp_path.parent / "repair-artifacts" / name).exists(), name


def make_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "calc.py").write_text("def value():\n    return 1\n")
    (tmp_path / "test_calc.py").write_text("from calc import value\n\ndef test_value():\n    assert value() == 2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def repair_task(tmp_path, base_sha, command=None, **kwargs):
    return RepairTask(
        task_id="failure-case",
        repo_path=tmp_path,
        base_sha=base_sha,
        goal="Fix value.",
        verification=VerificationProfile(id="pytest", command=command or f"{sys.executable} -m pytest -q"),
        explicit_finish=True,
        **kwargs,
    )


def test_failed_candidate_is_saved_as_failed_validation(tmp_path):
    base_sha = make_repo(tmp_path)
    edit = json.dumps({"cmd": "python -c \"from pathlib import Path; Path('calc.py').write_text('def value():\\n    return 3\\n')\""})
    llm = ScriptedLLM([response(tool_call("edit", "bash", edit)), response(tool_call("end", "finish", '{"summary":"attempted"}'))])
    result = RepairRunner(repair_task(tmp_path, base_sha), tmp_path.parent / "failed-artifacts", lambda root, workdir: LocalRunner(root / workdir)).run(llm)
    assert result.execution_status == "completed"
    assert result.validation_status == "failed"
    assert result.validation is not None and result.validation.exit_code == 1


def test_empty_patch_is_inconclusive_and_original_dirty_state_survives(tmp_path):
    base_sha = make_repo(tmp_path)
    (tmp_path / "local.txt").write_text("keep me")
    git(tmp_path, "add", "local.txt")
    (tmp_path / "local.txt").write_text("keep this staged file")
    (tmp_path / "scratch.txt").write_text("untracked")
    llm = ScriptedLLM([response(tool_call("end", "finish", '{"summary":"No change"}'))])
    result = RepairRunner(repair_task(tmp_path, base_sha), tmp_path.parent / "empty-artifacts", lambda root, workdir: LocalRunner(root / workdir)).run(llm)
    assert result.validation_status == "inconclusive"
    assert result.validation is not None and result.validation.error == "empty patch"
    status = git(tmp_path, "status", "--porcelain").stdout
    assert "AM local.txt" in status and "?? scratch.txt" in status
    assert (tmp_path / "local.txt").read_text() == "keep this staged file"


def test_protected_path_is_rejected_before_verification(tmp_path):
    base_sha = make_repo(tmp_path)
    edit = json.dumps({"cmd": "python -c \"from pathlib import Path; Path('test_calc.py').write_text('def test_value():\\n    assert True\\n')\""})
    llm = ScriptedLLM([response(tool_call("edit", "bash", edit)), response(tool_call("end", "finish", '{"summary":"changed test"}'))])
    result = RepairRunner(
        repair_task(tmp_path, base_sha, protected_paths=("test_*.py",)),
        tmp_path.parent / "protected-artifacts",
        lambda root, workdir: LocalRunner(root / workdir),
    ).run(llm)
    assert result.validation_status == "failed"
    assert result.validation is not None and "path protected" in result.validation.error


def test_repair_task_can_target_a_subdirectory_of_the_git_root(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    target = tmp_path / "examples" / "toy_repo"
    target.mkdir(parents=True)
    (target / "calc.py").write_text("def value():\n    return 1\n")
    (target / "test_calc.py").write_text("from calc import value\n\ndef test_value():\n    assert value() == 2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    edit = json.dumps({"cmd": "python -c \"from pathlib import Path; Path('calc.py').write_text('def value():\\n    return 2\\n')\""})
    task = RepairTask(
        task_id="nested-repo",
        repo_path=tmp_path,
        workdir="examples/toy_repo",
        base_sha=base_sha,
        goal="Fix value.",
        verification=VerificationProfile(id="pytest", command=f"{sys.executable} -m pytest -q"),
        explicit_finish=True,
    )
    llm = ScriptedLLM([response(tool_call("edit", "bash", edit)), response(tool_call("end", "finish", '{"summary":"fixed"}'))])
    result = RepairRunner(
        task,
        tmp_path.parent / "nested-artifacts",
        lambda root, workdir: LocalRunner(root / workdir),
    ).run(llm)
    assert result.validation_status == "passed"
    assert result.patch and "examples/toy_repo/calc.py" in result.patch
    assert git(tmp_path, "status", "--porcelain").stdout == ""


def test_execution_error_keeps_partial_patch_usage_and_terminal_artifacts(tmp_path):
    base = make_repo(tmp_path)
    # The first reply edits a file; the next model request raises StopIteration.
    llm = ScriptedLLM([response(tool_call("edit", "bash", '{"cmd":"echo changed >> calc.py"}'))])
    root = tmp_path / "artifacts"
    result = RepairRunner(repair_task(tmp_path, base), root, lambda p, w: LocalRunner(p / w)).run(llm)
    assert result.execution_status == "error"
    assert "+changed" in result.patch
    assert result.prompt_tokens == 10 and result.completion_tokens == 5
    assert (root / "patch.diff").read_text() == result.patch
    validation = json.loads((root / "validation.json").read_text())
    assert validation["status"] == "inconclusive"
    assert validation["candidate"]["status"] == "not_run"
    summary = json.loads((root / "summary.json").read_text())
    assert summary["patch"] == result.patch
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == "run_finished"
    trace = [json.loads(line) for line in next((root / "trajectory").glob("*.jsonl")).read_text().splitlines()]
    assert trace[-1]["type"] == "result" and trace[-1]["stop_reason"] == "error"


def test_repair_refuses_to_overwrite_existing_artifacts(tmp_path):
    base = make_repo(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "patch.diff").write_text("previous attempt")
    with pytest.raises(ValueError, match="new.*artifacts"):
        RepairRunner(repair_task(tmp_path, base), root, lambda p, w: LocalRunner(p / w)).run(ScriptedLLM([]))
    assert (root / "patch.diff").read_text() == "previous attempt"


@pytest.mark.parametrize("baseline_exit", [0, 2, 4, 5, 124, 127])
def test_unreproduced_baseline_does_not_hide_candidate_result(tmp_path, baseline_exit):
    from patchloop.repair.verifier import verify_patch

    base = make_repo(tmp_path)
    (tmp_path / "fixed").write_text("yes")
    patch = LocalRunner(tmp_path).diff()
    command = f"if test -f fixed; then exit 0; else exit {baseline_exit}; fi"
    result = verify_patch(repair_task(tmp_path, base, command), patch, lambda p, w: LocalRunner(p / w))
    saved = result.to_dict()
    assert saved["status"] == "inconclusive"
    assert saved["baseline"]["status"] == ("passed" if baseline_exit == 0 else "inconclusive")
    assert saved["candidate"]["status"] == "passed"
    assert saved["candidate"]["error"] == ""
    if baseline_exit:
        assert "already passed" not in saved["error"]


def test_verification_uses_real_exit_code_not_text_markers(tmp_path):
    from patchloop.repair.verifier import verify_patch

    base = make_repo(tmp_path)
    (tmp_path / "fixed").write_text("yes")
    patch = LocalRunner(tmp_path).diff()
    command = "if test -f fixed; then printf '\\n[exit code 127]'; else exit 1; fi"
    result = verify_patch(repair_task(tmp_path, base, command), patch, lambda p, w: LocalRunner(p / w))
    assert result.status == "passed"
    assert result.exit_code == 0


def test_candidate_startup_error_retains_completed_baseline(tmp_path):
    from patchloop.repair.verifier import verify_patch

    base = make_repo(tmp_path)
    (tmp_path / "fixed").write_text("yes")
    patch = LocalRunner(tmp_path).diff()
    starts = 0

    def factory(p, w):
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("startup failed")
        return LocalRunner(p / w)

    result = verify_patch(repair_task(tmp_path, base, "exit 1"), patch, factory)
    assert result.status == "inconclusive"
    assert result.baseline.status == "failed" and result.baseline.exit_code == 1
    assert "startup failed" in result.error
