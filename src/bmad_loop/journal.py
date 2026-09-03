"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .model import RunState
from .platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_replace,
    atomic_write_text,
    atomic_write_text_at,
    file_lock,
    is_link_like,
    open_dir_confined,
)

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
LOGS_DIR = "logs"
# Verifier subprocess streams, deliberately NOT under LOGS_DIR — see
# Journal.write_verify_stream for why sharing that directory is a TUI bug.
VERIFY_DIR = "verify"
# The cycle-scoped artifacts a session writes into ``tasks/<task_id>/``: the ONE
# list the three sites that touch them share. Both adapters clear these in
# ``start_session`` (a caller-supplied task_id may be reused, and a silent session
# must not inherit a stale predecessor's outputs) and
# ``resolve._gather_escalations`` reads them back. Spelled here rather than three
# times, because a fourth artifact added to the reader alone would silently miss
# both adapters — which is the shape the parity was in before.
#
# ``result.json`` is the dev/review contract's own result file. ``escalation.json``
# is the SWEEP SKILL's: its automation contract
# (``data/skills/bmad-loop-sweep/automation-mode.md``) tells a sweep session to
# write that file and then mirror the same entries into ``result.json``'s
# ``escalations``. That sentence lived in both adapters' comments and nowhere else,
# and it is the whole reason the reader opens two names rather than one.
#
# ORDER IS LOAD-BEARING — but NOT because of the mirroring, which is the obvious
# reading and the wrong one: ``_gather_escalations`` keys its map on canonical
# JSON, so a mirrored entry's STORED value is byte-identical whichever copy is read
# first. What the order fixes is the POSITION of DISTINCT entries in the
# newest-first list the operator is shown — result.json's entries precede
# escalation.json's, and a repeat keeps its first occurrence's slot. Swap these two
# and ``tests/test_resolve.py``'s
# ``test_gather_escalations_preserves_result_before_escalation_file_order`` and
# ``test_gather_escalations_keeps_a_duplicates_first_position`` redden (measured,
# not reasoned about).
#
# Appending a name is bounded twice, so it is not free. ``_gather_escalations``
# JSON-parses every name here and skips anything that is not an
# ``{"escalations": [...]}`` document, so a name that does not carry that shape
# buys the reader nothing. And both adapters run this unlink loop AFTER
# ``start_session`` has already written ``prompt.txt`` into the same directory, so
# a name an earlier step of that method writes would be deleted on the way out.
#
# Four other cycle-scoped files live in ``tasks/<task_id>/`` and are deliberately
# NOT here, because each is owned and read by ONE adapter rather than shared:
# ``heartbeat.json``, ``resultless-stops.jsonl`` and ``session-lifecycle.jsonl``
# (``adapters/generic.py``) and ``messages.json`` (``adapters/opencode_http.py``).
TASK_CYCLE_ARTIFACTS: tuple[str, ...] = ("result.json", "escalation.json")

# The field names ``Journal.append`` stamps onto an entry ITSELF, rather than taking
# from its caller's keywords — see the ``setdefault`` pair in that method. No call
# site spells either one, which makes them invisible to anything reading call sites
# and easy for a consumer to mistake for a producer-supplied field.
#
# Spelled here, at the minting site, because two consumers need exactly this set and
# a third copy is how they drift: ``diagnostics._scrub_entry`` must exempt them from
# the fail-closed arm it applies to a declared-schema kind (they are engine-minted,
# never LLM-authored, so collapsing ``log_pos`` to a presence marker would throw away
# a byte offset for no safety gain), and ``tests/test_portability_guard.py`` needs
# them to keep its static call-site scan from calling them dead. Both import this
# name; neither restates the pair.
SELF_MINTED_FIELDS: frozenset[str] = frozenset({"log_task", "log_pos"})

_STATE_LOCK_LOCAL = threading.local()


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
        check-then-write, and the check is :func:`is_link_like` rather than
        ``is_symlink()`` — on Windows the redirect that matters is a DIRECTORY
        JUNCTION, which ``is_symlink()`` reports False for and which ``mklink /J``
        creates with no elevation at all, while a directory symlink needs
        SeCreateSymbolicLinkPrivilege or Developer Mode.  Checking only for
        symlinks there would leave the unprivileged half of the same escape open,
        and with no race to win.  The residual is the platform's: a path check is
        stale the moment it returns, but the planting session runs as the same uid
        as this writer and the names here are engine-minted, so the exposure is a
        redirected diagnostic rather than a foothold.

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
            if is_link_like(verify_dir):
                raise OSError(f"refusing to write into a redirected verify directory: {verify_dir}")
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


@contextmanager
def state_lock(run_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    """Serialize one run's state mutations, re-entering only for the same run.

    ``blocking=False`` gives up instead of waiting, raising
    :class:`platform_util.LockUnavailableError` when another holder has the run.
    It is for a caller whose own semantics already say "in use ⇒ leave it alone"
    and which must not stall on one busy run — ``cli.cmd_clean`` sweeping many.
    The default stays blocking, because every other writer here is mutating one
    run it means to mutate, and for those giving up is data loss, not politeness.
    That error propagates out of this function UNCAUGHT and unwrapped: the whole
    point is that the caller gets to tell contention apart from a real fault, and
    a translation here would take that back.

    The sidecar identity comes from :func:`runs.lock_path_for`, so alternate path
    spellings of one ``state.json`` rendezvous on the same out-of-tree lock.  The
    import is deliberately lazy: ``runs`` imports this module's persistence helpers.

    Reentrancy is thread-local and intentionally limited to one run.  An outer
    read-modify-write transaction can call the self-locking :func:`save_state`
    without acquiring the OS lock twice, while nested mutation of another run is
    refused before a second lock can introduce an ordering cycle.  A re-entrant
    acquisition ignores ``blocking`` because it acquires nothing: this thread
    already holds the run, so there is no one to wait for and nothing to refuse.
    """
    from . import runs

    lock_path = runs.lock_path_for(run_dir / STATE_FILE, follow_final_symlink=False)
    held_path = getattr(_STATE_LOCK_LOCAL, "path", None)
    if held_path is not None:
        if held_path != lock_path:
            raise RuntimeError(
                f"cannot nest run-state locks for different runs: {held_path} then {lock_path}"
            )
        _STATE_LOCK_LOCAL.depth += 1
        try:
            yield
        finally:
            _STATE_LOCK_LOCAL.depth -= 1
        return

    with file_lock(lock_path, blocking=blocking):
        _STATE_LOCK_LOCAL.path = lock_path
        _STATE_LOCK_LOCAL.depth = 1
        try:
            yield
        finally:
            del _STATE_LOCK_LOCAL.depth
            del _STATE_LOCK_LOCAL.path


def save_state(run_dir: Path, state: RunState) -> None:
    with state_lock(run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / STATE_FILE
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        atomic_replace(tmp, target)


def load_state(run_dir: Path) -> RunState:
    target = run_dir / STATE_FILE
    return RunState.from_dict(json.loads(target.read_text(encoding="utf-8")))
