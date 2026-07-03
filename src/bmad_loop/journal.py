"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .model import RunState

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
LOGS_DIR = "logs"


class Journal:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / JOURNAL_FILE
        self._log_task: str | None = None
        self._log_path: Path | None = None
        run_dir.mkdir(parents=True, exist_ok=True)

    def set_active_log(self, task_id: str) -> None:
        """Entries from now on carry log_task/log_pos: the pane log of this
        task and its byte size at append time. Deliberately not cleared on
        session end — post-session entries (decisions, story-done) point at
        the end of the log they are about; the next session replaces it."""
        self._log_task = task_id
        self._log_path = self.run_dir / LOGS_DIR / f"{task_id}.log"

    def append(self, kind: str, **fields: Any) -> None:
        entry = {"ts": time.time(), "kind": kind, **fields}
        if self._log_path is not None:
            try:
                size = self._log_path.stat().st_size
            except OSError:
                size = 0  # pipe-pane has not created the file yet
            entry.setdefault("log_task", self._log_task)
            entry.setdefault("log_pos", size)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def save_state(run_dir: Path, state: RunState) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / STATE_FILE
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)


def load_state(run_dir: Path) -> RunState:
    target = run_dir / STATE_FILE
    return RunState.from_dict(json.loads(target.read_text(encoding="utf-8")))
