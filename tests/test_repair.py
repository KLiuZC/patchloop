import json
import subprocess
import sys
from types import SimpleNamespace

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
        runner_factory=lambda workdir: LocalRunner(workdir),
    ).run(llm)

    assert result.execution_status == "completed"
    assert result.validation_status == "passed"
    assert result.review_status == "pending"
    assert result.patch
    assert result.validation is not None and result.validation.exit_code == 0
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
    result = RepairRunner(repair_task(tmp_path, base_sha), tmp_path.parent / "failed-artifacts", lambda p: LocalRunner(p)).run(llm)
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
    result = RepairRunner(repair_task(tmp_path, base_sha), tmp_path.parent / "empty-artifacts", lambda p: LocalRunner(p)).run(llm)
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
        lambda p: LocalRunner(p),
    ).run(llm)
    assert result.validation_status == "failed"
    assert result.validation is not None and "path protected" in result.validation.error
