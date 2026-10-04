import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class WorktreeWorkspace:
    """A disposable standalone checkout rooted at one immutable base commit."""

    def __init__(self, repo_path: Path, base_sha: str, root: Path | None = None):
        self.repo_path = Path(repo_path).resolve()
        self.base_sha = base_sha
        self._root = Path(root).resolve() if root else None
        self.path: Path | None = None
        self._owns_root = root is None

    def __enter__(self) -> "WorktreeWorkspace":
        if not self.repo_path.is_dir():
            raise ValueError(f"repository does not exist: {self.repo_path}")
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if check.returncode != 0 or check.stdout.strip() != "true":
            raise ValueError(f"not a git worktree: {self.repo_path}")
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="patchloop-repair-"))
        else:
            self._root.mkdir(parents=True, exist_ok=True)
        self.path = self._root / f"attempt-{uuid.uuid4().hex[:10]}"
        # A standalone clone keeps its own .git directory. This matters when the
        # checkout is bind-mounted into Docker: a git worktree's .git file points
        # at an absolute host path that is unavailable inside the container.
        subprocess.run(
            ["git", "clone", "--no-hardlinks", "--no-local", str(self.repo_path), str(self.path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach", self.base_sha],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        )
        return self

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
        if self._owns_root and self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
