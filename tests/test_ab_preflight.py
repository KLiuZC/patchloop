import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import patchloop.ab_preflight as preparation


IMAGE = "sha256:" + "b" * 64
INSTANCE = {"instance_id": "example__repo-1", "repo": "psf/requests", "base_commit": "a" * 40, "patch": "PRIVATE GOLD", "test_patch": "PRIVATE TEST"}


def setup(monkeypatch):
    plan = {"instances": [{**INSTANCE, "image": "example:latest", "image_id": None}],
            "settings": {"platform": "linux/amd64", "memory": "3g", "cpus": 2.0, "command_timeout_seconds": 180},
            "preflight_task_repository_commit": "c" * 40}
    monkeypatch.setattr(preparation, "load_inputs", lambda *args: (plan, [INSTANCE], "draft-hash"))
    monkeypatch.setattr(preparation, "local_image", lambda *args: {"id": IMAGE, "platform": "linux/amd64"})
    monkeypatch.setattr(preparation, "task_repository_state", lambda *args: {"commit": "c" * 40, "files_sha256": {}})
    monkeypatch.setattr(preparation, "harness_interpreter", lambda *args: Path("/fake/python"))
    return plan


def test_default_only_inspects_and_does_not_run_models_or_containers(monkeypatch, capsys):
    setup(monkeypatch)
    monkeypatch.setattr(preparation, "execute_preflight", lambda *args, **kwargs: pytest.fail("must not execute"))
    monkeypatch.setattr(preparation, "preflight_instance", lambda *args, **kwargs: pytest.fail("must not create containers"))
    assert preparation.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "check_only" and not report["ready_for_model_run"]
    assert report["model_requests"] == 0 and "PRIVATE" not in json.dumps(report)


def test_explicit_execution_requires_an_instance_and_new_artifacts(monkeypatch):
    monkeypatch.setattr(preparation, "load_inputs", lambda *args: pytest.fail("arguments first"))
    assert preparation.main(["--execute"]) == 2


def test_missing_image_fails_before_creating_artifacts(tmp_path, monkeypatch):
    setup(monkeypatch)
    def missing(*args):
        raise RuntimeError("missing image")
    monkeypatch.setattr(preparation, "local_image", missing)
    output = tmp_path / "new"
    assert preparation.main(["--execute", "--instance", INSTANCE["instance_id"], "--artifacts", str(output)]) == 2
    assert not output.exists()


@pytest.mark.parametrize("fault", [None, "environment", "dirty_base", "bridge_exit", "wrong_identity", "changed_request", "unbound_report"])
def test_validation_separates_hidden_inputs_and_fails_closed(tmp_path, monkeypatch, fault):
    setup(monkeypatch)
    seen = []
    def environment(public, image, output, **kwargs):
        assert "patch" not in public and "test_patch" not in public
        assert image == IMAGE
        return {"status": "failed" if fault == "environment" else "passed", "image": {"id": IMAGE},
                "before": {"git": {"head": INSTANCE["base_commit"], "tracked_changes": " M file.py" if fault == "dirty_base" else ""}}}
    monkeypatch.setattr(preparation, "preflight_instance", environment)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-key")
    monkeypatch.setenv("GH_TOKEN", "private-github-token")
    def controls(command, **kwargs):
        seen.append(command)
        assert "DEEPSEEK_API_KEY" not in kwargs["env"] and "GH_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        request = Path(command[command.index("--input") + 1])
        content = request.read_bytes()
        assert json.loads(content)["instance"]["patch"] == "PRIVATE GOLD"
        assert request.stat().st_mode & 0o777 == 0o600
        output = Path(command[command.index("--output") + 1])
        output.mkdir()
        report = {"status": "passed", "request_sha256": hashlib.sha256(content).hexdigest(),
                  "instance_id": INSTANCE["instance_id"], "base_commit": INSTANCE["base_commit"],
                  "image_id": IMAGE, "platform": "linux/amd64"}
        if fault == "wrong_identity":
            report["image_id"] = "other"
        if fault == "changed_request":
            request.write_text("{}")
        if fault == "unbound_report":
            del report["request_sha256"]
        (output / "result.json").write_text(json.dumps(report))
        return SimpleNamespace(returncode=1 if fault == "bridge_exit" else 0)
    monkeypatch.setattr(preparation.subprocess, "run", controls)
    output = tmp_path / "private"
    code = preparation.main(["--execute", "--instance", INSTANCE["instance_id"], "--artifacts", str(output)])
    assert code == (0 if fault is None else 2)
    result = json.loads((output / "result.json").read_text())
    assert result["model_requests"] == 0
    assert result["status"] == ("passed" if fault is None else "error")
    assert "PRIVATE" not in json.dumps(result)
    assert output.stat().st_mode & 0o777 == 0o700
    assert len(seen) == (0 if fault in {"environment", "dirty_base"} else 1)


def test_existing_artifacts_are_not_overwritten(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        preparation.private_output(tmp_path)
    assert marker.read_text() == "preserve"


def test_validation_material_cannot_be_written_under_source():
    with pytest.raises(ValueError, match="private"):
        preparation.private_output(preparation.ROOT / "evals/reports/should-not-exist")


def test_explicit_virtualenv_interpreter_path_is_not_dereferenced(tmp_path):
    target = tmp_path / "global-python"
    target.write_text("executable")
    target.chmod(0o755)
    executable = tmp_path / "venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(target)
    assert preparation.harness_interpreter(executable) == executable


def test_failed_container_cleanup_is_reported_and_can_be_retried(monkeypatch):
    from patchloop.sandbox import DockerRunner

    runner = DockerRunner("local-image", local_only=True)
    runner._started = True
    monkeypatch.setattr("patchloop.sandbox.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="daemon unavailable", stdout=""))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        runner.close()
    assert runner._started
    monkeypatch.setattr("patchloop.sandbox.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""))
    runner.close()
    assert not runner._started


def test_initialization_failure_removes_the_started_container(monkeypatch):
    from patchloop.sandbox import DockerRunner

    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="container", stderr="")
    monkeypatch.setattr("patchloop.sandbox.subprocess.run", execute)
    def fail(*args, **kwargs):
        raise RuntimeError("initialization failed")
    monkeypatch.setattr(DockerRunner, "_exec", fail)
    runner = DockerRunner("local-image", local_only=True)
    with pytest.raises(RuntimeError, match="initialization failed"):
        runner.start()
    assert any(command[:3] == ["docker", "rm", "-f"] for command in commands)
    assert not runner._started


def test_missing_docker_socket_is_not_proof_container_is_gone(monkeypatch):
    from patchloop.sandbox import DockerRunner

    runner = DockerRunner("local-image")
    runner._started = True
    monkeypatch.setattr("patchloop.sandbox.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="dial unix /tmp/docker.sock: connect: no such file or directory"))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        runner.close()
    assert runner._started


def test_environment_cleanup_failure_cannot_leave_a_passed_status(tmp_path, monkeypatch):
    import patchloop.environment as environment

    class Raw:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Solver(Raw):
        def __exit__(self, *args):
            raise RuntimeError("container cleanup failed")
        def run_result(self, *args, **kwargs):
            return SimpleNamespace(exit_code=0, timed_out=False, output="healthy")
    monkeypatch.setattr(environment, "inspect_image", lambda *args: {"id": IMAGE})
    monkeypatch.setattr(environment, "DockerRunner", Raw)
    monkeypatch.setattr(environment, "solver_runner", lambda *args, **kwargs: Solver())
    monkeypatch.setattr(environment, "probe_environment", lambda *args: {"status": "passed"})
    monkeypatch.setattr(environment, "prepare_swebench_environment", lambda *args: {"checks": {"status": "passed"}})
    result = environment.preflight_instance(INSTANCE, IMAGE, tmp_path / "new", verify="true")
    assert result["status"] == "error" and "cleanup" in result["error"]
