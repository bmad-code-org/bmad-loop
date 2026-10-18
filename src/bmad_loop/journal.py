"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .model import RunState
from .platform_util import (
    AT_DIRECTORY,
    AT_NOFOLLOW,
    DIR_FD_ANCHORED_WRITES,
    HANDLE_ANCHORED_WRITES,
    RootIdentityRecord,
    UnconfinedWriteError,
    append_line_confined,
    atomic_replace,
    atomic_write_text,
    atomic_write_text_at,
    file_lock,
    is_link_like,
    open_at,
    open_dir_confined,
    recorded_root_identity,
    require_root_pinned,
    stale_device_record,
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

# The one kind in this file NO producer writes: both journal readers mint it in place
# of a line they could not JSON-decode, so a lost record is counted and rendered
# instead of vanishing under a bare ``continue``.
#
# Spelled here, next to ``SELF_MINTED_FIELDS`` and for the same reason: two readers
# (``Journal.entries`` and ``tui.data.JournalTail.read_new``) need exactly one shape,
# and a second copy is how they drift. ``tui.widgets.journal_line`` imports the constant
# too and matches it by EQUALITY, so the styling rule cannot outlive a rename and cannot
# leak red onto a producer kind that merely contains this spelling.
#
# NOT a ``tests/test_portability_guard.py`` ``JOURNAL_KINDS`` row, deliberately: that
# inventory is scanned from the literal kinds passed to ``Journal.append``, so a
# reader-minted kind would sit there as a row nothing writes and redden
# ``test_journal_kind_inventory_is_complete``'s staleness arm.
UNREADABLE_LINE_KIND = "journal-line-unreadable"

# The one row :meth:`Journal.append` writes on its own behalf: written once per
# ``Journal`` object, on the first append whose run dir cannot be pinned, so a
# confined-but-unpinned write is visible rather than indistinguishable from a pinned
# one. ``reason`` is ``"no-record"`` (the pinned ``RunState`` carries no
# ``run_dir_identity`` — a pre-upgrade run never resumed, or a zero-inode filesystem)
# or ``"stale-device"`` (the record misses the intact run dir only by a renumbered
# ``st_dev``, the shape ``runs.reconcile_root_identities`` re-binds).
#
# A journal built with no ``pin`` at all stays silent: outside tests that shape is a
# read-only one (the writers pass their ``RunState``), so a notice there would be
# noise on every reader that happens to append nothing.
UNPINNED_NOTICE_KIND = "journal-unpinned"


def unreadable_line_entry(byte_len: int) -> dict[str, Any]:
    """One unreadable journal line, reported in the stream position it occupied.

    Deliberately one line, not one record: a marker stands for a LINE the reader could
    not decode, and how many records that line cost is unknowable from the line alone.
    A torn record that its own next append healed costs one; a line written before this
    heal existed — or one a rival appender tore inside :meth:`Journal.append`'s
    probe-to-write window — can be two records concatenated into a single unparseable
    line. The ``bytes`` count is what is actually known.

    Deliberately minimal. No ``ts``: ``diagnostics.summarize_journal`` derives
    ``first_ts``/``last_ts``/``duration_s`` from entry timestamps, so a fabricated one
    would corrupt them, and the true write time of an unparseable line is unknowable
    (a ``ts``-less entry is simply skipped for those). No content from the offending
    line, which can carry session text — a bounded byte count only. No line number:
    ``entries()`` counts from the file and ``JournalTail`` from its chunk, so the same
    field would mean two different things; the list position already carries it.

    A plain ``dict``, so it survives ``runs.journal_entries_or_none``'s
    ``isinstance(e, dict)`` filter and keeps that reader's ``len(before)`` watermark
    exact across two reads of an unchanged file.
    """
    return {"kind": UNREADABLE_LINE_KIND, "bytes": byte_len}


_STATE_LOCK_LOCAL = threading.local()


class Journal:
    # Class-level defaults, not just ``__init__`` state: ``cli._ConfirmMarkLog`` skips
    # ``super().__init__`` (it writes nothing), and these keep it a complete Journal.
    pin: RunState | None = None
    _unpinned_noticed: bool = False

    def __init__(self, run_dir: Path, *, pin: RunState | None = None):
        self.run_dir = run_dir
        self.path = run_dir / JOURNAL_FILE
        self.pin = pin
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

    def _pin_identity(self) -> tuple[os.stat_result | None, str | None]:
        """The run dir identity this append is pinned to, or ``None`` with the notice
        reason an unpinned write records (``None`` reason: silent).

        Read from ``self.pin`` on EVERY call, never cached: resume and re-arm backfill
        ``run_dir_identity`` on the very ``RunState`` this journal was built with, and a
        snapshot taken at construction would keep writing unpinned after the record
        landed."""
        if self.pin is None:
            return None, None
        record = self.pin.run_dir_identity
        if record is None:
            return None, "no-record"
        return recorded_root_identity(record), None

    def append(self, kind: str, **fields: Any) -> None:
        """Append one record as LF-terminated UTF-8 bytes, healing an unterminated
        tail first.

        The write goes through :func:`~bmad_loop.platform_util.append_line_confined`:
        the run dir is opened as a directory descriptor and ``journal.jsonl`` under it
        with an append-only, link-refusing open, so a link AT the run dir or AT
        ``journal.jsonl`` refuses rather than redirecting the record. Three arms, chosen
        per call from the live ``pin`` (:meth:`_pin_identity`):

        - **pinned** — the pin's ``run_dir_identity`` is recorded: the opened run dir
          must be that directory. A refusal raises
          :class:`~bmad_loop.platform_util.UnconfinedWriteError`, an ``OSError``, so
          the DW-54 contract holds — an append that cannot land fails loud and is never
          folded into a silent skip.
        - **unpinned + notice** — ``pin`` carries no record: the run dir is still
          opened ``O_NOFOLLOW`` as the last component of a walk from its parent (its
          ancestors are followed), and the first such append on this object writes one
          :data:`UNPINNED_NOTICE_KIND` row with ``reason="no-record"``. A journal with
          no ``pin`` at all takes this arm silently.
        - **stale-device + notice** — the pinned open refused, and the only mismatch is
          a renumbered ``st_dev`` on the intact directory with the recorded nonzero
          inode (:func:`~bmad_loop.platform_util.stale_device_record`): the write is
          retried unpinned with a ``reason="stale-device"`` notice. That is exactly the
          residual ``runs.reconcile_root_identities`` accepts when it re-binds, so it
          widens nothing.

        ``append`` never creates the run dir (only the constructor does). A run dir
        deleted under a live journal raises instead of being resurrected —
        ``runsetup._unwind_composition`` relies on that to leave a removed run dir
        removed.

        The fault the heal closes (DW-97) costs TWO records, not one. This is the only
        writer in this module without a durable path — ``save_state`` uses
        ``atomic_replace``, ``write_verify_stream`` uses ``atomic_write_text`` — so a
        partially flushed write leaves a fragment with no trailing newline. Without
        the heal the NEXT append concatenates its own record onto that fragment, and
        both readers then drop the combined line: one fault, two lost records, and one
        of them may be an ``unit-merged`` that
        ``engine._replay_unlatched_ledger_carries`` needs to avoid re-driving merged
        work. Prepending the newline keeps the damage to the one record that was
        actually torn. The probe reads the append descriptor itself; an unknown tail
        fails toward the heal (:func:`~bmad_loop.platform_util.tail_is_terminated_fd`).

        Not routed through ``atomic_write_text``/``atomic_replace``: those rewrite the
        whole target, and this journal is append-only and grows for a whole run, so an
        "atomic append" would re-copy the entire file per record. The heal is the
        in-repo precedent at ``install.py``'s exclude-file writer
        (``existing if not existing or existing.endswith(b"\\n") else existing + b"\\n"``)
        applied at O(1) cost.

        Bytes, LF on every platform — no text-mode translation, so Windows no longer
        writes ``\\r\\n``. Both readers accept either ending, which keeps a legacy CRLF
        journal readable: ``entries`` splits with ``splitlines`` and strips each line,
        and ``tui.data.JournalTail`` splits on ``\\n`` and strips.

        The probe-then-append window is racy, and the heal BOUNDS the loss rather than
        eliminating it — do not read it as a guarantee. POSIX ``O_APPEND`` (and, on
        Windows, a handle holding ``FILE_APPEND_DATA`` without ``FILE_WRITE_DATA``)
        places every write at the current end, so a rival appender cannot interleave
        INTO this record; but it can tear its own write inside the window, after the
        probe has already answered "terminated". That leaves a fresh fragment this
        append then concatenates onto, and the two-record loss recurs for that one
        pair. A rival that completes its line in the window is harmless: the tail stays
        terminated and the prepended newline, if any, is a blank line both readers
        skip. Closing the race needs a lock, which is deliberately out of scope here
        (as is fsync): this defect is the SEQUENTIAL concatenation — one writer's own
        torn record absorbing its own next one — which the probe closes completely.
        """
        identity, reason = self._pin_identity()
        entry = {"ts": time.time(), "kind": kind, **fields}
        if self._log_path is not None:
            try:
                size = self._log_path.stat().st_size
            except OSError:
                size = 0  # pipe-pane has not created the file yet
            entry.setdefault("log_task", self._log_task)
            entry.setdefault("log_pos", size)
        line = (json.dumps(entry, default=str) + "\n").encode("utf-8")
        self._write(line, identity, reason)

    def _write(self, line: bytes, identity: os.stat_result | None, reason: str | None) -> None:
        """:meth:`append`'s three arms: unpinned (with the notice ``reason`` owes),
        pinned, and pinned-refused-on-a-stale-device retried unpinned."""
        if identity is None:
            self._notice_once(reason)
            append_line_confined(self.run_dir, JOURNAL_FILE, line, root_identity=None)
            return
        try:
            append_line_confined(self.run_dir, JOURNAL_FILE, line, root_identity=identity)
        except UnconfinedWriteError:
            record = self.pin.run_dir_identity if self.pin is not None else None
            if not stale_device_record(self.run_dir, record):
                raise
            self._notice_once("stale-device")
            append_line_confined(self.run_dir, JOURNAL_FILE, line, root_identity=None)

    def _notice_once(self, reason: str | None) -> None:
        """Write the :data:`UNPINNED_NOTICE_KIND` row, once per object.

        The flag is set BEFORE the write, which is what bounds the recursion: the notice
        goes through the public :meth:`append` — so a subclass that classifies its rows
        (``runs._RearmJournal``) sees it exactly once, and so the kind-inventory scan
        reads it at an ``.append(`` receiver — and that append re-enters here.

        The kind is spelled as a literal, not as :data:`UNPINNED_NOTICE_KIND`: that
        scan reads literal kinds only, and ``tests/test_portability_guard.py`` holds
        the constant to the inventory so the two spellings cannot drift."""
        if reason is None or self._unpinned_noticed:
            return
        self._unpinned_noticed = True
        self.append("journal-unpinned", reason=reason)

    def write_verify_stream(
        self, name: str, content: str, *, run_dir_identity: RootIdentityRecord | None
    ) -> str:
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
        it.  ``verify/`` is created relative to the pinned run-dir descriptor, so a
        planted link there makes the ``mkdir`` answer ``FileExistsError`` and the
        ``O_NOFOLLOW`` open that follows refuses it.

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

        The run dir itself is PINNED to its MINT-TIME identity (DW-338, DW-446):
        ``run_dir_identity`` is ``RunState.run_dir_identity``, the ``(st_dev,
        st_ino)`` the composer recorded when it claimed the directory. A fresh
        ``lstat`` would refuse a link only at the run dir itself; ``runs/`` (or
        the run dir) swapped for a link to a tree holding a real ``<id>/`` would
        answer the outside directory, and every record would land there. On the
        dir-fd arm the run dir is OPENED first and its ``fstat`` compared against
        the record, before ``verify/`` is created — relative to that descriptor —
        so a swapped run dir gets no ``verify/`` in its target, and ``verify/``
        is then walked ``O_NOFOLLOW`` from the same descriptor. The win32 arm
        re-``lstat``s the run dir against the record before its ``mkdir`` and
        again before the write (check-then-write, beside its
        :func:`is_link_like` checks). A missing record (``None``: a pre-upgrade
        state not yet backfilled, a zero-inode claim) refuses outright — it never
        degrades to an unpinned open. That is no change on the dir-fd arm (a zero
        inode never matched), but the win32 arm, which before DW-446 wrote through
        a zero-inode run dir, now REFUSES it (accepted 2026-09-27): a refusal, not a
        degrade, surfaced by the caller as the verify record's ``capture_error``.
        Trust-on-first-use: the record is what sat at the run dir when it was
        minted; only a locked resume/re-arm (``runs.reconcile_root_identities``)
        backfills a missing one or re-binds an ``st_dev`` a reboot renumbered.

        Raises ``OSError`` — including when confinement cannot be established, so
        an unconfined ``verify/`` REFUSES rather than writing through the link.
        The caller degrades (this is observation), it does not swallow it here:
        the record still lands, with a null pointer and ``capture_error``.
        """
        verify_dir = self.run_dir / VERIFY_DIR
        root_identity = recorded_root_identity(run_dir_identity)
        # Pin (or refuse) the run dir BEFORE the mkdir: a swapped run dir must not
        # get a `verify/` created in its target ahead of the refusal.
        if DIR_FD_ANCHORED_WRITES:
            run_fd = open_dir_confined(self.run_dir, self.run_dir, root_identity=root_identity)
            if run_fd is None:
                raise OSError(
                    f"refusing to write into an unconfined verify directory: {verify_dir}"
                )
            try:
                try:
                    os.mkdir(VERIFY_DIR, 0o777, dir_fd=run_fd)
                except FileExistsError:
                    pass  # an existing entry — the O_NOFOLLOW open below vets it
                try:
                    dir_fd = open_at(run_fd, VERIFY_DIR, os.O_RDONLY | AT_DIRECTORY | AT_NOFOLLOW)
                except OSError as e:
                    raise OSError(
                        f"refusing to write into an unconfined verify directory: {verify_dir}"
                    ) from e
            finally:
                os.close(run_fd)
            try:
                atomic_write_text_at(dir_fd, name, content)
            finally:
                os.close(dir_fd)
        else:
            require_root_pinned(self.run_dir, root_identity)
            if is_link_like(self.run_dir):
                raise OSError(f"refusing to write into a redirected verify directory: {verify_dir}")
            verify_dir.mkdir(parents=True, exist_ok=True)
            require_root_pinned(self.run_dir, root_identity)
            if is_link_like(self.run_dir) or is_link_like(verify_dir):
                raise OSError(f"refusing to write into a redirected verify directory: {verify_dir}")
            atomic_write_text(verify_dir / name, content, follow_symlinks=False)
        return (verify_dir / name).relative_to(self.run_dir).as_posix()

    def entries(self) -> list[dict[str, Any]]:
        """Every record in the journal, with an unparseable line reported rather than
        skipped: :func:`unreadable_line_entry` takes its place at the same stream
        position, so a lost record is counted on the surfaces that read this list
        instead of vanishing silently.

        Blank lines are still skipped — a blank line lost no record, and the heal in
        :meth:`append` can introduce one. Invalid UTF-8 still raises out of
        ``read_text``: ``runs.journal_entries_or_none`` catches ``UnicodeDecodeError``
        deliberately, so widening this to swallow it would take away a caller's ability
        to tell a corrupt journal from an empty one. Only ``JSONDecodeError`` becomes a
        marker.
        """
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # The raw line's own byte length, not the stripped spelling's: the
                # count reports what sits in the file.
                out.append(unreadable_line_entry(len(raw_line.encode("utf-8"))))
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
    """Atomically persist ``state`` as ``run_dir/state.json`` under :func:`state_lock`.

    The write goes through a descriptor on the run dir pinned to
    ``state.run_dir_identity``, the mint-time ``(st_dev, st_ino)`` record, read
    from ``state`` on every call. That is the same rule :meth:`Journal.append`
    follows. On both arms of :data:`HANDLE_ANCHORED_WRITES`:

    - **Pinned** (a record): the run dir is opened by :func:`open_dir_confined`
      and must be the recorded directory. A run dir that is missing, a link, or
      a different directory raises :class:`UnconfinedWriteError`. Nothing is
      created and nothing is written.
    - **Unpinned** (no record: a pre-upgrade state not yet backfilled, a
      zero-inode claim, or a bare ``RunState``): the run dir is created if absent,
      then opened as the last component of a walk from its parent. That open is
      ``O_NOFOLLOW``, so a link AT the run dir still refuses.
      ``mkdir(exist_ok=True)`` accepts a symlink-to-directory, so the open, not
      the ``mkdir``, is what refuses it. Ancestors are followed.
    - **Stale device** (the record misses an intact run dir only by a renumbered
      ``st_dev``, :func:`stale_device_record`): the unpinned open, the residual
      ``runs.reconcile_root_identities`` already accepts when it re-binds. No row
      is written here, because this function has no journal. Every production
      writer that saves a run's state also appends to that run's journal with
      the same ``RunState``, so the journal's ``journal-unpinned`` row covers
      both records.

    :func:`atomic_write_text_at` stages at a unique, unguessable temp name beside
    ``state.json``, fsyncs, then replaces relative to the descriptor. On Windows
    the replace retries the transient sharing violation a concurrent reader
    raises. The file lands ``0600`` and inherits no prior mode: every reader of
    ``state.json`` runs as the same user.

    Hosts with neither anchored arm get the path-based fallback, mirroring
    :meth:`Journal.write_verify_stream`'s: the pin pre-check
    (:func:`require_root_pinned`), the ``mkdir`` when unpinned, a refusal when the
    run dir is link-like, a fixed temp sibling, the pin checked again, then
    :func:`atomic_replace` (the temp is removed when either fails). That is
    check-then-write, the residual :func:`path_is_confined` documents.
    """
    with state_lock(run_dir):
        text = json.dumps(state.to_dict(), indent=2)
        record = state.run_dir_identity
        if HANDLE_ANCHORED_WRITES:
            if record is None:
                run_dir.mkdir(parents=True, exist_ok=True)
                dir_fd = open_dir_confined(run_dir.parent, run_dir)
            else:
                dir_fd = open_dir_confined(
                    run_dir, run_dir, root_identity=recorded_root_identity(record)
                )
                if dir_fd is None and stale_device_record(run_dir, record):
                    dir_fd = open_dir_confined(run_dir.parent, run_dir)
            if dir_fd is None:
                raise UnconfinedWriteError(
                    f"refusing to write {STATE_FILE} into an unconfined or replaced run dir: "
                    f"{run_dir}"
                )
            try:
                atomic_write_text_at(dir_fd, STATE_FILE, text)
            finally:
                os.close(dir_fd)
            return
        identity = (
            None
            if record is None or stale_device_record(run_dir, record)
            else recorded_root_identity(record)
        )
        require_root_pinned(run_dir, identity)
        if identity is None:
            run_dir.mkdir(parents=True, exist_ok=True)
        if is_link_like(run_dir):
            raise UnconfinedWriteError(
                f"refusing to write {STATE_FILE} into a redirected run dir: {run_dir}"
            )
        target = run_dir / STATE_FILE
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            require_root_pinned(run_dir, identity)
            atomic_replace(tmp, target)
        except BaseException:
            with suppress(OSError):
                tmp.unlink()
            raise


def load_state(run_dir: Path) -> RunState:
    target = run_dir / STATE_FILE
    return RunState.from_dict(json.loads(target.read_text(encoding="utf-8")))
