import subprocess

import pytest

from patchloop.sandbox import DockerRunner, LocalRunner


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _image_present(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def _git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


# ---------------------------------------------------------------- local

def test_local_runs_in_workdir(tmp_path):
    (tmp_path / "marker.txt").write_text("")
    assert "marker.txt" in LocalRunner(tmp_path).run("ls", timeout=10)


def test_local_merges_stderr_and_reports_exit_code(tmp_path):
    out = LocalRunner(tmp_path).run("echo out; echo err >&2; exit 3", timeout=10)
    assert "out" in out and "err" in out and "[exit code 3]" in out


def test_local_timeout_kills_command(tmp_path):
    assert "timed out" in LocalRunner(tmp_path).run("sleep 5", timeout=1)


def test_local_diff_returns_staged_and_unstaged_changes(tmp_path):
    _git_repo(tmp_path)
    runner = LocalRunner(tmp_path)
    runner.run("echo two >> f.txt && echo new > g.txt", timeout=10)
    patch = runner.diff()
    assert "+two" in patch and "+new" in patch
    # diff() must not leave anything staged behind
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=tmp_path, capture_output=True, text=True)
    assert staged.stdout.strip() == ""


def test_local_diff_outside_git_is_empty(tmp_path):
    assert LocalRunner(tmp_path).diff() == ""


# ---------------------------------------------------------------- docker

needs_docker = pytest.mark.skipif(not _docker_available(), reason="docker is not running")
TOY_IMAGE = "patchloop-toy"


@needs_docker
@pytest.mark.skipif(not _image_present(TOY_IMAGE), reason=f"{TOY_IMAGE} not built (docker build -t patchloop-toy examples/toy_image)")
def test_docker_runs_commands_and_cleans_up():
    with DockerRunner(TOY_IMAGE, workdir="/tmp") as runner:
        assert runner.run("pwd", timeout=10).strip() == "/tmp"
        out = runner.run("echo out; echo err >&2; exit 3", timeout=10)
        assert "out" in out and "err" in out and "[exit code 3]" in out
        assert "timed out" in runner.run("sleep 5", timeout=1)
        name = runner.name
    gone = subprocess.run(["docker", "container", "inspect", name], capture_output=True)
    assert gone.returncode != 0


@needs_docker
@pytest.mark.skipif(not _image_present(TOY_IMAGE), reason=f"{TOY_IMAGE} not built (docker build -t patchloop-toy examples/toy_image)")
def test_docker_mounts_workdir_and_diffs(tmp_path):
    _git_repo(tmp_path)
    with DockerRunner(TOY_IMAGE, workdir="/work", mount=tmp_path) as runner:
        runner.run("echo two >> f.txt", timeout=10)
        assert "+two" in runner.diff()
    assert (tmp_path / "f.txt").read_text() == "one\ntwo\n"
