"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .model import RunState
from .platform_util import atomic_replace

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
LOGS_DIR = "logs"
# Verifier subprocess streams, deliberately NOT under LOGS_DIR — see
# Journal.write_verify_stream for why sharing that directory is a TUI bug.
VERIFY_DIR = "verify"


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

    def write_verify_stream(self, name: str, content: str) -> str:
        """Atomically retain one verifier subprocess stream under ``verify/`` and
        return its run-relative pointer.  The journal records the pointer and byte
        count, never unbounded subprocess output inline.

        Its own directory, not ``logs/``: every other inhabitant of ``logs/`` is a
        coding-CLI pane capture named after a session task id.  The adapters own
        that namespace (they write ``{task_id}.log``) and the TUI reads the whole
        directory as one — with no session open, ``tui.data.active_task_id`` falls
        back to the newest ``logs/*.log`` and returns its stem as the live task,
        which the dashboard then reopens as ``logs/{stem}.log``.  Verifier streams
        land in exactly that window: session-end is journalled when the session
        ends, before its result reaches verification, so at the moment these files
        are newest no session is open and the fallback fires.  Under ``logs/`` that
        rendered verifier stderr in the agent log pane.  Keeping the store in a
        separate directory makes that unrepresentable, rather than a name filter
        every future reader of ``logs/`` would have to remember to apply.

        ``name`` is engine-generated (not plugin or command supplied), so it is
        safe to join below.  Callers retain the original stream separately in a
        hook context; this method is journal storage only.
        """
        target = self.run_dir / VERIFY_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        atomic_replace(tmp, target)
        return target.relative_to(self.run_dir).as_posix()

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
    atomic_replace(tmp, target)


def load_state(run_dir: Path) -> RunState:
    target = run_dir / STATE_FILE
    return RunState.from_dict(json.loads(target.read_text(encoding="utf-8")))
