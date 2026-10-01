import json
from dataclasses import asdict
from pathlib import Path


class Trajectory:
    """Append-only JSONL: metadata, optional reminders, turns, then a result."""

    def __init__(self, path: Path, meta: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", encoding="utf-8")
        self._write({"type": "meta", **meta})

    def _write(self, record: dict) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def turn(self, **record) -> None:
        self._write({"type": "turn", **record})

    def budget_reminder(self, turn: int, remaining: int, message: dict) -> None:
        self._write({"type": "budget_reminder", "turn": turn, "remaining": remaining, "message": message})

    def close(self, result) -> None:
        self._write({"type": "result", **asdict(result)})
        self._file.close()
