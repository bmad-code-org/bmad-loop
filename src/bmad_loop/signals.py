"""Watch the per-run events directory for hook-written event files.

The hook script (data/bmad_loop_hook.py) and its importable twin (:mod:`events`,
behind ``bmad-loop relay``) write one JSON file per event, atomically (tmp +
rename), named "<ts_ns>-<task_id>-<event>.json". Plain polling of a near-empty
directory is cheap and crash-safe; no inotify.

Two directories, not one (#494). The channel now lives out of the project tree,
under the user-scoped state root (``runs.events_dir_for``), because a branch
switch, a worktree mount or a rollback must not be able to take a live run's
control plane away. The *legacy* in-tree ``<run_dir>/events`` stays polled as
well, and that is the whole version-skew guard: the relay a target project has
installed is a COPY taken at init time, so an upgraded orchestrator routinely
drives sessions whose hook only knows the old location. Without the second poll
that pairing loses every Stop event and every session stalls to
``session_timeout_min`` — the loudest possible regression, delivered silently.
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
    """Poll one or two event directories for a run's hook events.

    ``events_dir`` is the primary — the out-of-tree channel this orchestrator
    directs its sessions to via ``BMAD_LOOP_EVENTS_DIR``, and the only one
    created here. ``legacy_dir`` is the pre-#494 in-tree ``<run_dir>/events``,
    polled when given so a project carrying an older installed relay still
    completes its sessions (see the module docstring). It is deliberately NOT
    created: an orchestrator that recreated the in-tree directory would undo the
    move for the operator's `git status` while gaining nothing — a legacy relay
    that writes there makes the directory itself.

    The single-positional-argument form is unchanged, and stays the shape the
    probe (``probe.py``, watching its own capture dir) and the unit tests use.
    """

    def __init__(self, events_dir: Path, legacy_dir: Path | None = None):
        self.events_dir = events_dir
        self.legacy_dir = legacy_dir
        # Keyed by (directory, filename), not by filename: one name identifies an
        # event only WITHIN a directory, and the two dirs are written by two
        # independent relays. Keying on the name alone would let a file consumed
        # from one dir mask a different event of the same name in the other, and a
        # masked event here is a lost Stop — the run then waits out
        # session_timeout_min with its completion signal sitting on disk.
        self._consumed: set[tuple[str, str]] = set()
        self._pending: list[HookEvent] = []  # polled but not yet delivered via wait_for
        events_dir.mkdir(parents=True, exist_ok=True)

    def _dirs(self) -> list[Path]:
        """Primary first, then the legacy dir when there is a distinct one. The
        equality check keeps a caller that passes the same path twice from
        double-scanning; it cannot produce duplicate events either way (the
        ``_consumed`` key would repeat), but the second scan would be pure waste."""
        if self.legacy_dir is None or self.legacy_dir == self.events_dir:
            return [self.events_dir]
        return [self.events_dir, self.legacy_dir]

    def poll(self) -> list[HookEvent]:
        """Return new, well-formed events since the last poll, oldest first.

        Ordering is by the parsed ``ts`` across BOTH directories, so which relay
        wrote an event never affects where it lands in the sequence.

        A missing *legacy* directory is the normal case (nothing installed writes
        there any more) and is skipped silently. A missing *primary* still raises,
        as it always has: this watcher created it, so its absence means something
        removed the live control plane out from under the run.
        """
        events: list[HookEvent] = []
        dirs = self._dirs()
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                if directory == self.events_dir:
                    raise
                continue  # legacy dir absent — the ordinary case
            for entry in entries:
                key = (str(directory), entry.name)
                if key in self._consumed or entry.suffix != ".json":
                    continue
                self._consumed.add(key)
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
        are dropped: a resumed run reuses task_ids, and a fresh watcher re-sees the
        events directory from scratch, so a prior cycle's Stop event would
        otherwise replay instantly and the old result.json be read as a bogus
        completion. Sessions run sequentially, so since_ns only advances; anything
        below the current floor is genuinely stale and safe to discard.

        A re-ARMED run no longer reuses them — `runs.rearm_escalation` bumps
        `StoryTask.generation`, which `engine._session_task_id` folds into the id
        (#705). That removes one source of collision; it does not make this floor
        redundant, because a plain resume re-mints the SAME id by design (that is
        how crash replay finds its record) and is the case this guard was written
        for.
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
