"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .model import RunState
from .platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_replace,
    atomic_write_text,
    atomic_write_text_at,
    open_dir_confined,
)

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
        counts, never unbounded subprocess output inline.

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
        safe to join below.  ``content`` arrives already bounded — the cap is
        ``verify.stream_capture_kb``, applied by the caller, which is also where
        the full-size and truncation bookkeeping lives; this method is journal
        storage only and never decides how much to keep.  Callers retain the
        original stream separately in a hook context.

        :func:`atomic_write_text`, never ``write_text`` (#379) — the rule
        ``install.py`` states flatly.  The fixed ``.tmp`` sibling this replaces is
        the collision that helper's own docstring exists to prevent, and its
        fsync-before-replace is what keeps a pointer from ever naming blocks that
        were never written.  ``follow_symlinks=False`` because these are
        machine-minted records under a run directory a coding-CLI session can
        reach: honouring a planted link would aim the write at a path of that
        session's choosing, and there is no operator-curated target here to
        preserve (contrast the ledgers the default was built for).

        Text mode is deliberate, and it is why the record's byte counts are
        defined over the *stream*, not the file: ``\\n`` is translated on Windows,
        so the file can be larger there than the count.  ``read_text`` normalizes
        it back, so the content round-trips either way.

        The write is **anchored at a directory descriptor** where the platform has
        one, because ``follow_symlinks=False`` covers the final component and
        nothing above it.  Sessions are handed this run directory outright
        (``BMAD_LOOP_RUN_DIR``, which is where they write ``result.json``), so a
        session that plants a symlink at ``verify/`` before verification redirects
        every record: ``mkdir(exist_ok=True)`` ACCEPTS a symlink-to-directory —
        it re-raises only when ``is_dir()`` is false, and that follows links — and
        the replace then lands wherever the link points, outside the run dir
        entirely.  Measured, not theorised.

        ``open_dir_confined`` is the fix the repo already keeps for exactly this
        (``tui/launch.py`` writes its control-window record the same way): it walks
        each component below the run dir ``O_NOFOLLOW`` and hands back a descriptor
        for the directory it actually reached, and :func:`atomic_write_text_at`
        never names a path again.  A path check would be answered *about a path*
        and stale the moment it returned — the session can re-plant the link
        between check and write — so this closes the window rather than narrowing
        it.  The ``mkdir`` above may still be fooled; that is harmless, because the
        confinement walk that follows is not, and refusal is what the fooled case
        produces.

        win32 has no ``*at()`` family to anchor against, so it keeps a
        check-then-write with the residual that implies: the planting session runs
        as the same uid as this writer, and the names here are engine-minted, so
        the exposure is a redirected diagnostic rather than a foothold.

        Raises ``OSError`` — including when confinement cannot be established, so
        an unconfined ``verify/`` REFUSES rather than writing through the link.
        The caller degrades (this is observation), it does not swallow it here:
        the record still lands, with a null pointer and ``capture_error``.
        """
        verify_dir = self.run_dir / VERIFY_DIR
        verify_dir.mkdir(parents=True, exist_ok=True)
        if DIR_FD_ANCHORED_WRITES:
            dir_fd = open_dir_confined(self.run_dir, verify_dir)
            if dir_fd is None:
                raise OSError(
                    f"refusing to write into an unconfined verify directory: {verify_dir}"
                )
            try:
                atomic_write_text_at(dir_fd, name, content)
            finally:
                os.close(dir_fd)
        else:
            if verify_dir.is_symlink():
                raise OSError(f"refusing to write into a symlinked verify directory: {verify_dir}")
            atomic_write_text(verify_dir / name, content, follow_symlinks=False)
        return (verify_dir / name).relative_to(self.run_dir).as_posix()

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
