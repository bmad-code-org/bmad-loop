"""Watch the per-run events directory for hook-written event files.

The hook script (hooks/bmad_loop_hook.py) writes one JSON file per event,
atomically (tmp + rename), named "<ts_ns>-<task_id>-<event>.json". Plain
polling of a near-empty directory is cheap and crash-safe; no inotify.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class HookEvent:
    ts: int
    event: str  # Stop | SessionStart | SessionEnd | PreCompact
    task_id: str
    session_id: str | None
    transcript_path: str | None
    path: Path


class SignalWatcher:
    def __init__(self, events_dir: Path):
        self.events_dir = events_dir
        self._consumed: set[str] = set()
        self._pending: list[HookEvent] = []  # polled but not yet delivered via wait_for
        events_dir.mkdir(parents=True, exist_ok=True)

    def poll(self) -> list[HookEvent]:
        """Return new, well-formed events since the last poll, oldest first."""
        events: list[HookEvent] = []
        for entry in self.events_dir.iterdir():
            if entry.name in self._consumed or entry.suffix != ".json":
                continue
            self._consumed.add(entry.name)
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or "event" not in data or "task_id" not in data:
                continue
            events.append(
                HookEvent(
                    ts=int(data.get("ts", 0)),
                    event=str(data["event"]),
                    task_id=str(data["task_id"]),
                    session_id=data.get("session_id"),
                    transcript_path=data.get("transcript_path"),
                    path=entry,
                )
            )
        events.sort(key=lambda e: e.ts)
        return events

    def wait_for(
        self,
        task_id: str,
        kinds: set[str],
        timeout_s: float,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        since_ns: int = 0,
    ) -> HookEvent | None:
        """Block until an event for task_id with kind in `kinds` arrives, or timeout.

        Events polled but not matched stay buffered for later wait_for calls —
        several events often land in one poll (e.g. SessionStart + Stop) and
        none may be lost.

        Events older than `since_ns` (wall-clock ns, the session's launch time)
        are dropped: a resumed/re-armed run reuses task_ids, and a fresh watcher
        re-sees the events directory from scratch, so a prior cycle's Stop event
        would otherwise replay instantly and the old result.json be read as a
        bogus completion. Sessions run sequentially, so since_ns only advances;
        anything below the current floor is genuinely stale and safe to discard.
        """
        deadline = clock() + timeout_s
        while True:
            self._pending.extend(self.poll())
            if since_ns:
                self._pending = [e for e in self._pending if e.ts >= since_ns]
            for i, event in enumerate(self._pending):
                if event.task_id == task_id and event.event in kinds:
                    return self._pending.pop(i)
            if clock() >= deadline:
                return None
            sleep(poll_interval)
