import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import RepairTask, ValidationResult


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArtifactStore:
    """Small filesystem artifact store for one repair attempt."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "events.jsonl"

    def write_task(self, task: RepairTask) -> None:
        self._write_json("task.json", task.to_dict())

    def event(self, event_type: str, **payload: Any) -> None:
        record = {"type": event_type, "at": time.time(), **payload}
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_patch(self, patch: str) -> str:
        patch_hash = sha256_text(patch) if patch else None
        (self.root / "patch.diff").write_text(patch, encoding="utf-8")
        return patch_hash or ""

    def write_validation(self, result: ValidationResult) -> None:
        self._write_json("validation.json", result.to_dict())

    def write_summary(self, result: dict) -> None:
        self._write_json("summary.json", result)

    def _write_json(self, name: str, value: Any) -> None:
        (self.root / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
