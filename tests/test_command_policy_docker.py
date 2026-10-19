import json
import subprocess
from types import SimpleNamespace

import pytest

from patchloop.command_policy import PolicyContext
from patchloop.command_runtime import CommandPolicy
from patchloop.loop import run
from patchloop.sandbox import DockerRunner


@pytest.mark.parametrize("explicit_finish", [False, True])
@pytest.mark.parametrize("blocked_command", [
    "git log --all",
    "cat <<'EOF' && git log --all\nexample\nEOF",
    "git diff origin/main --",
    "cd /tmp; git -C /work log --all",
])
def test_docker_blocks_whole_command_and_still_repairs(tmp_path, explicit_finish, blocked_command):
    try:
        image = subprocess.run(["docker", "image", "inspect", "patchloop-toy"],
                               capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker or existing toy image unavailable; no downloads")
    if image.returncode:
        pytest.skip("existing toy image unavailable; no downloads")
    image_id = json.loads(image.stdout)[0]["Id"]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def parse_version(value):\n    return tuple(int(part) for part in value.split('.'))\n")
    (repo / "test_calc.py").write_text(
        "from calc import parse_version\n"
        "def test_plain():\n    assert parse_version('1.2') == (1, 2)\n"
        "def test_prefix():\n    assert parse_version('v1.2') == (1, 2)\n"
    )
    for arguments in [("init", "-q"), ("config", "user.name", "test"),
                      ("config", "user.email", "test@example.com"), ("add", "."), ("commit", "-qm", "base")]:
        subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True)
    commands = [
        "cat calc.py",
        f"printf touched > policy-marker; {blocked_command}",
        "python - <<'PY'\nfrom pathlib import Path\npath = Path('calc.py')\n"
        "path.write_text(path.read_text().replace(\"value.split('.')\", \"value.removeprefix('v').split('.')\"))\nPY",
        "python -m pytest -q",
    ]
    responses = [SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
        id=f"operation-{number}", function=SimpleNamespace(name="bash", arguments=json.dumps({"command": command})),
    )]) for number, command in enumerate(commands)]
    responses.append(SimpleNamespace(content="done", tool_calls=[SimpleNamespace(
        id="finished", function=SimpleNamespace(name="finish", arguments='{"summary":"Fixed prefix"}'),
    )] if explicit_finish else []))

    class ScriptedModel:
        provider = "fake"
        model = "fake"

        def __init__(self):
            self.replies = iter(responses)
            self.requests = []

        def chat(self, messages, tools):
            self.requests.append([dict(message) for message in messages])
            return next(self.replies), None

    model = ScriptedModel()
    with DockerRunner(image_id, workdir="/work", mount=repo, network="none") as runner:
        assert runner.run_result("python -m pytest -q", timeout=60).exit_code == 1
        result = run("fix prefix", model, runner, max_turns=6, traj_dir=tmp_path / "traj",
                     explicit_finish=explicit_finish,
                     command_policy=CommandPolicy("enforce", PolicyContext("/work", "/work")))
        assert runner.run_result("test ! -e /work/policy-marker", timeout=10).exit_code == 0
        assert runner.run_result("python -m pytest -q", timeout=60).exit_code == 0
    assert result.finished and not result.error
    assert result.stop_reason == ("finish" if explicit_finish else "no_tool_calls")
    assert result.command_policy_counts["blocked"] == 1
    assert result.command_policy_counts["unknown"] == 1
    assert result.command_policy_counts["checked"] == 4
    assert "+    return tuple" in result.patch and "test_calc.py" not in result.patch
    assert "policy-marker" not in result.patch
    assert "Command blocked" in model.requests[2][-1]["content"]
    assert "2 passed" in model.requests[4][-1]["content"]
