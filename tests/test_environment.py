import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import patchloop.environment as environment
import patchloop.eval as evaluation
from patchloop.environment_probe import inspect_environment, restrict_history, scan_files
from patchloop.sandbox import CommandResult, DockerRunner


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_history_isolation_removes_future_and_unreachable_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "bug.py").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "bug.py").write_text("future fix\n")
    git(repo, "commit", "-qam", "future answer")
    future = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "answer")
    restrict_history(repo, base)
    assert (repo / "bug.py").read_text() == "original\n"
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "rev-list", "--all") == base
    assert not git(repo, "reflog", "--all")
    assert not git(repo, "tag") and not git(repo, "remote")
    objects = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    assert future not in objects
    assert sum(line.endswith(" commit") for line in objects.splitlines()) == 1
    (repo / "bug.py").write_text("candidate\n")
    assert "+candidate" in git(repo, "diff")


def test_scanner_finds_source_archive_and_material_without_flagging_worktree(tmp_path):
    repo = tmp_path / "testbed"
    (repo / "xarray").mkdir(parents=True)
    (repo / "xarray/__init__.py").write_text("base")
    cache = tmp_path / "pkgs/xarray-2025.4.0/lib/xarray"
    cache.mkdir(parents=True)
    (cache / "__init__.py").write_text("new source")
    (tmp_path / "pkgs/xarray-2025.4.0.conda").write_bytes(b"archive")
    (tmp_path / "test.patch").write_text("hidden tests")
    report = scan_files([str(tmp_path)], repo, ["xarray"])
    assert {f["kind"] for f in report["findings"]} == {
        "external_project_directory", "project_archive", "evaluation_material",
    }
    assert not report["scan_errors"]


def test_scanner_checks_external_symlink_and_allows_link_to_baseline(tmp_path):
    repo = tmp_path / "testbed"
    (repo / "xarray").mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    link = tmp_path / "installed/xarray"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    assert scan_files([str(link.parent)], repo, ["xarray"])["findings"]
    link.unlink()
    link.symlink_to(repo / "xarray", target_is_directory=True)
    assert not scan_files([str(link.parent)], repo, ["xarray"])["findings"]


@pytest.fixture
def inspected_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / ".gitignore").write_text("ignored/\n")
    (repo / "gold.patch").write_text("tracked project fixture\n")
    git(repo, "add", "-f", ".gitignore", "gold.patch")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    restrict_history(repo, base)
    config = {"workdir": str(repo), "base_commit": base, "prepared": True,
              "scan_roots": [str(tmp_path)], "hidden_paths": [], "modules": []}
    assert inspect_environment(config)["status"] == "passed"
    return repo, config


@pytest.mark.parametrize("ignore_source", ["none", "gitignore", "info_exclude", "global_excludes"])
@pytest.mark.parametrize("name", ["test.patch", "gold.patch", "predictions.jsonl", "eval.sh"])
@pytest.mark.parametrize("directory", ["plain", '中文 space\n"quoted"'])
def test_worktree_material_is_rejected_even_when_ignored(inspected_worktree, tmp_path, ignore_source, name, directory):
    repo, config = inspected_worktree
    parent = "ignored" if ignore_source == "gitignore" else "untracked"
    if ignore_source == "info_exclude":
        (repo / ".git/info/exclude").write_text("untracked/\n")
    elif ignore_source == "global_excludes":
        excludes = tmp_path / "global-ignore"
        excludes.write_text("untracked/\n")
        git(repo, "config", "core.excludesFile", str(excludes))
    path = repo / parent / directory / name
    path.parent.mkdir(parents=True)
    path.write_text("evaluation material\n")
    ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "--", str(path)], capture_output=True)
    assert ignored.returncode == (1 if ignore_source == "none" else 0)
    report = inspect_environment(config)
    assert report["status"] == "failed"
    assert report["findings"] == [{"kind": "evaluation_material", "path": str(path)}]
    assert path.read_text() == "evaluation material\n"


def test_ignored_cache_and_tracked_fixture_remain_allowed(inspected_worktree):
    repo, config = inspected_worktree
    cache = repo / "ignored/__pycache__/module.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"cache")
    report = inspect_environment(config)
    assert report["status"] == "passed"
    assert not report["findings"]
    assert (repo / "gold.patch").read_text() == "tracked project fixture\n"


def test_failed_or_broken_probe_is_saved_and_blocks_execution(tmp_path, monkeypatch):
    runner = SimpleNamespace(workdir="/testbed")
    instance = {"instance_id": "pydata__xarray-4687", "repo": "pydata/xarray", "base_commit": "a" * 40}
    monkeypatch.setattr(environment, "probe_environment", lambda *a, **kw: {
        "status": "failed", "findings": [{"kind": "project_archive", "path": "/tmp/xarray.whl"}],
    })
    report_path = tmp_path / "environment.json"
    with pytest.raises(environment.EnvironmentCheckError, match="preflight"):
        environment.prepare_swebench_environment(runner, instance, report_path, {"id": "image"})
    report = json.loads(report_path.read_text())
    assert report["status"] == "failed" and report["checks"]["findings"]
    assert report["policy"]["probe_sha256"]


def test_probe_timeout_and_invalid_output_never_pass(monkeypatch):
    instance = {"instance_id": "pydata__xarray-4687", "repo": "pydata/xarray", "base_commit": "a" * 40}
    for result in [CommandResult("", 124, True), CommandResult("not json", 0),
                   CommandResult('PATCHLOOP_ENVIRONMENT_REPORT={"status":"unknown"}', 0)]:
        runner = SimpleNamespace(workdir="/testbed", run_result=lambda *a, **kw: result)
        with pytest.raises(environment.EnvironmentCheckError):
            environment.probe_environment(runner, instance, prepare=True)


def test_preflight_command_does_not_initialize_model_or_load_provider_secrets(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import patchloop.cli as cli
    monkeypatch.setattr(cli, "LLM", lambda *a: pytest.fail("no model client during preflight"))
    monkeypatch.setattr(cli, "load_dotenv", lambda: pytest.fail("no provider credentials during preflight"))
    monkeypatch.setattr(cli, "load_instances", lambda *a: [{"instance_id": "x__y-1", "image": "fake"}])
    seen = []
    def preflight(instance, image, output, **kwargs):
        seen.append((instance, image, output, kwargs))
        return {"status": "failed"}
    monkeypatch.setattr(environment, "preflight_instance", preflight)
    result = CliRunner().invoke(cli.app, ["preflight-swebench", "-i", "x__y-1", "--verify", "pytest -q",
                                          "--artifacts", str(tmp_path)])
    assert result.exit_code == 2 and "preflight=failed" in result.output
    assert seen[0][3]["verify"] == "pytest -q"


def test_environment_failure_does_not_become_a_model_failure_or_inflate_rate(tmp_path, monkeypatch):
    instances = [{"instance_id": "bad", "repo": "x/y"}, {"instance_id": "ok", "repo": "x/y"}]
    llm = SimpleNamespace(provider="fake", model="fake", temperature=None,
                          client=SimpleNamespace(timeout=120, max_retries=3))
    def solve(instance, *args, **kwargs):
        failed = instance["instance_id"] == "bad"
        return evaluation.InstanceResult(instance["instance_id"], "agent",
                                         error="preflight failed" if failed else "",
                                         environment_status="failed" if failed else "passed"), ""
    monkeypatch.setattr(evaluation, "solve", solve)
    monkeypatch.setattr(evaluation, "image_present", lambda *a: True)
    def judge(path, ids, *args):
        assert ids == ["ok"]
        return {"ok": {"resolved": True, "patch_applied": True}}, "judge-id"
    monkeypatch.setattr(evaluation, "judge_predictions", judge)
    result = evaluation.evaluate(instances, llm, "r", tmp_path)
    assert result["environment_failures"] == 1 and result["resolve_rate"] is None
    assert result["results"][0]["resolved"] is None
    assert result["results"][1]["resolved"] is True


def test_eval_cli_returns_failure_for_incomplete_environment_checks(monkeypatch):
    from typer.testing import CliRunner
    import patchloop.cli as cli
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "LLM", lambda *a: object())
    monkeypatch.setattr(cli, "load_instances", lambda *a: [])
    monkeypatch.setattr(cli, "evaluate", lambda *a, **kw: {"environment_failures": 1})
    monkeypatch.setattr(cli, "_print_summary", lambda *a: None)
    result = CliRunner().invoke(cli.app, ["eval", "--run-id", "test", "-i", "x__y-1"])
    assert result.exit_code == 2


@pytest.mark.parametrize("number,base_commit,source_path,verify", [
    ("2905", "7c4e2ac83f7b4306296ff9b7b51aaf016e5ad614", "core/variable.py",
     "python -m pytest -q xarray/tests/test_variable.py -k setitem"),
    ("3993", "8cc34cb412ba89ebca12fc84f76a9e452628f1bc", "core/dataarray.py",
     "python -m pytest -q xarray/tests/test_dataset.py -k test_integrate"),
    ("4687", "d3b6aa6d8b997df115a53c001d00222a0f92f63a", "computation/computation.py",
     "python -m pytest -q xarray/tests/test_computation.py -k test_where"),
])
def test_real_xarray_cache_is_hidden_while_baseline_remains_executable(
    tmp_path, number, base_commit, source_path, verify,
):
    image = f"swebench/sweb.eval.x86_64.pydata_1776_xarray-{number}:latest"
    try:
        info = environment.inspect_image(image)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pytest.skip(f"xarray-{number} image is not available; no automatic downloads")
    instance = {"instance_id": f"pydata__xarray-{number}", "repo": "pydata/xarray",
                "base_commit": base_commit}
    cache = "/opt/miniconda3/pkgs/xarray-2025.4.0-py310h06a4308_0"
    path = f"{cache}/lib/python3.10/site-packages/xarray/{source_path}"
    with environment.solver_runner(info["id"]) as runner:
        report = environment.prepare_swebench_environment(runner, instance, tmp_path / "environment.json", info)
        assert report["status"] == "passed"
        assert report["checks"]["git"]["commit_objects"] == 1
        assert report["checks"]["git"]["head"] == base_commit
        assert runner.run_result(f"cat {path}", timeout=10).exit_code != 0
        assert runner.run_result(f"python -c 'open(\"{path}\").read()'", timeout=10).exit_code != 0
        assert runner.run_result(f"test -r {cache}.conda", timeout=10).exit_code != 0
        assert runner.run_result("umount /opt/miniconda3/pkgs", timeout=10).exit_code != 0
        assert runner.run_result("git status --porcelain --untracked-files=no", timeout=10).output == ""
        assert runner.run_result(verify, timeout=60).exit_code == 0
        runner.put("/tmp/patchloop-ignore", ".patchloop-probe/\n")
        assert runner.run_result("git config --global core.excludesFile /tmp/patchloop-ignore", timeout=10).exit_code == 0
        assert runner.run_result("mkdir .patchloop-probe", timeout=10).exit_code == 0
        runner.put("/testbed/.patchloop-probe/gold.patch", "test-only canary\n")
        assert runner.run_result("git check-ignore .patchloop-probe/gold.patch", timeout=10).exit_code == 0
        blocked_report = tmp_path / "blocked-environment.json"
        with pytest.raises(environment.EnvironmentCheckError, match="preflight"):
            environment.prepare_swebench_environment(runner, instance, blocked_report, info)
        blocked = json.loads(blocked_report.read_text())
        assert blocked["status"] == "failed"
        assert blocked["policy"]["version"] == "swebench-isolation-v2"
        assert {"kind": "evaluation_material", "path": "/testbed/.patchloop-probe/gold.patch"} in blocked["checks"]["findings"]


def test_isolation_mounts_and_reduced_privileges_are_passed_to_docker(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("patchloop.sandbox.subprocess.run", run)
    monkeypatch.setattr(DockerRunner, "_exec", lambda *a, **kw: None)
    runner = environment.solver_runner("image")
    runner.start()
    command = calls[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    for path in environment.HIDDEN_PATHS:
        assert f"{path}:ro,nosuid,nodev,noexec,size=1m" in command


def test_eval_never_calls_model_when_preflight_fails(tmp_path, monkeypatch):
    class Runner:
        workdir = "/testbed"
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(evaluation, "solver_runner", lambda *a, **kw: Runner(), raising=False)
    monkeypatch.setattr(evaluation, "inspect_image", lambda *a: {"id": "pinned", "platform": "linux/amd64"}, raising=False)
    def reject(*args, **kwargs):
        raise environment.EnvironmentCheckError("contaminated")
    monkeypatch.setattr(evaluation, "prepare_swebench_environment", reject, raising=False)
    monkeypatch.setattr(evaluation, "run", lambda *a, **kw: pytest.fail("model loop must not start"))
    instance = {"instance_id": "pydata__xarray-4687", "repo": "pydata/xarray", "base_commit": "a" * 40,
                "problem_statement": "fix"}
    result, patch = evaluation.solve(instance, SimpleNamespace(model="fake"), "agent", 2, 10, tmp_path)
    assert result.stop_reason == "environment_failed"
    assert result.turns == 0 and not patch


def test_repair_never_calls_model_when_preflight_fails(tmp_path, monkeypatch):
    import patchloop.repair.swebench as module
    task = module.SWEbenchTask("pydata__xarray-4687", "pydata/xarray", "fix", "fake", "a" * 40)
    repair = module.SWEbenchRepairRunner(task, tmp_path / "artifacts")
    monkeypatch.setattr(module, "inspect_image", lambda *a: {"id": "pinned"}, raising=False)
    class Runner:
        workdir = "/testbed"
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(repair, "_runner", lambda **kw: Runner())
    monkeypatch.setattr(repair, "_check_base", lambda *a: None)
    def reject(*args, **kwargs):
        raise environment.EnvironmentCheckError("contaminated")
    monkeypatch.setattr(module, "prepare_swebench_environment", reject, raising=False)
    monkeypatch.setattr(module, "run", lambda *a, **kw: pytest.fail("model loop must not start"))
    result = repair.run(object())
    assert result.execution_status == "error" and "contaminated" in result.error
    assert result.turns == 0 and result.validation_status == "inconclusive"
