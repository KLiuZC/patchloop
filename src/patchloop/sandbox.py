import os
import platform
import shlex
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Callable

GitCommand = Callable[[list[str]], subprocess.CompletedProcess]

_IGNORED_PATHS = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
)


def _collect_patch(git: GitCommand) -> str:
    """Stage everything under the working directory, read the diff, then unstage again."""
    if git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        return ""
    pathspec = [".", *_IGNORED_PATHS]
    git(["add", "-A", "--", *pathspec])
    patch = git(["diff", "--cached", "--", *pathspec]).stdout
    git(["reset", "-q", "--", "."])
    return patch


class LocalRunner:
    """Runs commands directly on this machine, inside a fixed working directory."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir).resolve()

    @property
    def environment(self) -> str:
        system = {"Darwin": "macOS"}.get(platform.system(), platform.system())
        return f"{system} shell on the host machine"

    def describe(self) -> dict:
        return {"sandbox": "local", "workdir": str(self.workdir)}

    def start(self) -> "LocalRunner":
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> "LocalRunner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, cmd: str, timeout: int) -> str:
        # own process group, so a timeout kills the whole pipeline rather than just the shell
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=self.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            return (out or "") + f"\n[command timed out after {timeout}s]"
        if proc.returncode != 0:
            out += f"\n[exit code {proc.returncode}]"
        return out

    def put(self, path: str, content: str) -> None:
        target = Path(path) if Path(path).is_absolute() else self.workdir / path
        target.write_text(content, encoding="utf-8")

    def diff(self) -> str:
        def git(args: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-c", "core.fileMode=false", *args],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )

        return _collect_patch(git)


class DockerRunner:
    """Runs commands inside a throwaway container; the host never executes them.

    Commands go through a login shell so the image's own environment (conda etc.)
    is active, and through `timeout` inside the container so a runaway command is
    killed where it lives.
    """

    def __init__(
        self,
        image: str,
        workdir: str = "/testbed",
        mount: Path | None = None,
        platform: str | None = None,
        memory: str = "2g",
        cpus: float = 2.0,
        network: str = "none",
    ):
        self.image = image
        self.workdir = workdir
        self.mount = Path(mount).resolve() if mount else None
        self.platform = platform
        self.memory = memory
        self.cpus = cpus
        self.network = network
        self.name = f"patchloop-{uuid.uuid4().hex[:8]}"
        self._started = False

    @property
    def environment(self) -> str:
        return "Linux Docker container"

    def describe(self) -> dict:
        return {"sandbox": "docker", "image": self.image, "workdir": self.workdir, "container": self.name}

    def start(self) -> "DockerRunner":
        cmd = [
            "docker", "run", "-d",
            "--name", self.name,
            "--network", self.network,
            "--memory", self.memory,
            "--cpus", str(self.cpus),
            "--workdir", self.workdir,
        ]
        if self.platform:
            cmd += ["--platform", self.platform]
        if self.mount:
            cmd += ["--volume", f"{self.mount}:{self.workdir}"]
        cmd += [self.image, "sleep", "infinity"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")
        self._started = True
        # the repository may be owned by a different uid than the one running git
        self._exec(["git", "config", "--global", "--add", "safe.directory", "*"], timeout=30)
        return self

    def close(self) -> None:
        if self._started:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            self._started = False

    def __enter__(self) -> "DockerRunner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, cmd: str, timeout: int) -> str:
        if not self._started:
            raise RuntimeError("container is not running; call start() first")
        proc = self._exec(["timeout", "-s", "KILL", str(timeout), "bash", "-lc", cmd], timeout=timeout + 15)
        out = proc.stdout
        if proc.returncode in (124, 137):
            return out + f"\n[command timed out after {timeout}s]"
        if proc.returncode != 0:
            out += f"\n[exit code {proc.returncode}]"
        return out

    def put(self, path: str, content: str) -> None:
        proc = subprocess.run(
            ["docker", "exec", "-i", "-w", self.workdir, self.name, "bash", "-c", f"cat > {shlex.quote(path)}"],
            input=content,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker exec failed: {proc.stderr.strip()}")

    def diff(self) -> str:
        def git(args: list[str]) -> subprocess.CompletedProcess:
            return self._exec(["git", "-c", "core.fileMode=false", *args], timeout=60, merge=False)

        return _collect_patch(git)

    def _exec(self, argv: list[str], timeout: int, merge: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            ["docker", "exec", "-w", self.workdir, self.name, *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            text=True,
            errors="replace",
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return subprocess.CompletedProcess(argv, 137, out or "", err or "")
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
