"""Detached launching of bmad-loop commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated control session (bmad-loop-ctl on tmux, a per-registry
name on psmux — see the CTL_SESSION comment below) so they survive TUI exit,
and the dashboard observes them through run-dir artifacts exactly like runs
started from a plain shell. Fast read-only commands (validate,
--dry-run) are captured instead, for display in a modal.

No textual imports here — everything drives the multiplexer seam (or a plain
subprocess for the captured read-only commands) and is unit-testable.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from .. import policy as policy_mod
from .. import runs
from ..adapters.multiplexer import (
    MultiplexerError,
    TerminalMultiplexer,
    get_multiplexer,
    mux_usable,
)
from ..journal import Journal
from ..platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_write_text,
    atomic_write_text_at,
    open_dir_confined,
)

CTL_SESSION = runs.CTL_SESSION
# The control-session NAME is the transport's business, resolved per call
# through `runs.ctl_session_for(project)`: the fixed name on tmux, where one
# server serves the machine and the session really is machine-wide (scoped by
# the per-window PROJECT_OPTION tag below), and a per-registry name on psmux,
# whose duplicate-server mutex is keyed on the session name alone, across
# every registry in the login session (`Local\` is a per-login-session object
# namespace) — so a fixed name would let only ONE registry there hold a control
# session and every other project's launch would fail as a duplicate.
# The constant survives as the fixed base name (display fallbacks, tmux argv
# pins); anything that addresses a live session resolves the name instead.

# control-session windows are named <kind>-<run_id> (see start_detached)
_CTL_WINDOW_RE = re.compile(r"^(?:run|sweep|resume|resolve)-(.+)$")


class LaunchError(Exception):
    pass


def mux_available() -> bool:
    # Forced-aware (mux_usable, not raw available()): a pinned backend must look
    # the same to observers (attach, ctl-window lookup, prune) as it does to the
    # launch preflight, or a launched run becomes invisible to the rest of the TUI.
    return mux_usable(get_multiplexer())


def session_exists(session: str) -> bool:
    return get_multiplexer().has_session(session)


def agent_session_exists(session: str) -> bool:
    """:func:`session_exists` for a run's AGENT session, the one an attach lands
    on: in a registry shared with other projects (#729) a same-named session
    may be another project's, and it does not exist as far as this project is
    concerned. Saying why is the difference between that and "no session".

    Kept off :func:`session_exists` itself, which also answers for the control
    session — a per-project name already (`runs.ctl_session_for`), whose
    session carries no project tag at all."""
    if not session_exists(session):
        return False
    refusal = runs.foreign_session_refusal(session, get_multiplexer())
    if refusal is not None:
        print(f"warning: treating {session} as absent — {refusal}", file=sys.stderr)
        return False
    return True


# Run-dir sidecar naming the ctl-session window start_detached minted last for
# this run. `<kind>-<run_id>` is not unique across the four kinds, so the window
# listing alone cannot tell a live resume window from the parked run window it
# superseded — this file names the one we actually created. A hint, never a
# target on its own: ctl_window_id re-proves it against the live listing.
_CTL_WINDOW_FILE = "ctl-window"


# Generous ceiling on the hint: the value is a window id (`@7`, or a
# session-qualified `bmad-loop-ctl:@7`), and anything longer is already not one.
_MAX_RECORD_BYTES = 256


def _read_ctl_window(project: Path, run_id: str) -> str | None:
    """The window id recorded by the run's last launch, or None when there is
    none / it cannot be read. Never raises, and that includes decoding: a torn
    record can raise UnicodeDecodeError, a ValueError rather than an OSError,
    which action_attach (no covering except at all) and _stop_run_worker (whose
    except does not include it) would let escape. An unreadable hint is not an
    error — it just leaves the caller with the name scan.

    The file is the only channel on purpose: `bmad-loop attach` resolves the same
    run from its own process, and one resolve feeding every consumer is the
    property ctl_window_id sells. A per-process memo of what this process last
    minted would answer a different window than the CLI does.

    Deliberately not `read_text`. The record sits under the project root every
    coding session can write, and this read runs on Textual's event loop
    (`action_attach` calls it directly), so the *shape* of what is at the path
    has to be established before any bytes are consumed:

    * `O_NONBLOCK` + an `S_ISREG` check on the opened descriptor. Opening a FIFO
      for reading otherwise blocks until someone writes — indefinitely, freezing
      the dashboard on a keypress.
    * `O_NOFOLLOW`, so the name is read rather than wherever it points.
    * At most `_MAX_RECORD_BYTES`. A record pointed at an endless source reads
      forever otherwise, and it raises `MemoryError` rather than the OSError
      this promises never to leak — `Exception` would catch that but also mask
      real bugs, where a cap removes the condition instead of absorbing it.

    The check is on the descriptor, not the path, so it cannot be raced: fstat
    describes the object actually opened. The POSIX-only flags degrade to 0 on
    win32, which has neither FIFOs at these paths nor O_NOFOLLOW; the size cap
    and the regular-file check carry there on their own."""
    record = runs.run_dir_for(project, run_id) / _CTL_WINDOW_FILE
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)  # win32: no CRLF translation on the raw fd
    try:
        fd = os.open(record, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _MAX_RECORD_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


def _forget_ctl_window(project: Path, run_id: str) -> None:
    """Drop the record. A launch that cannot name the window it just minted must
    not leave the *previous* launch's id authoritative — that id now names a
    superseded window, and the honest answer is no record at all, which puts the
    lookup back on the name scan.

    Ceiling: when the removal fails, or is declined because the path cannot be
    vouched for, the superseded id survives on disk. It still has to pass
    ctl_window_id's re-prove, so the worst it can answer is a live window
    carrying this run's name — the pre-fix by-name result, never a wilder target.
    Retaining a stale hint is strictly the cheaper failure here, which is why
    this declines rather than deleting on a path it cannot stand behind.

    Anchored exactly like the write in _record_ctl_window, and for a sharper
    reason: a delete needs no race at all. `unlink` does not follow a link at the
    *final* component, but the ancestors resolve normally, so a run dir standing
    as a link to an external directory makes `run_dir / ctl-window` name a file
    over there — another project's live record — and this deletes it. The write
    path's escape needed the attacker to win a window between check and write;
    a planted link just sits there until the next launch fails to capture an id.
    So the descriptor from `open_dir_confined` is what the unlink is relative to,
    and no path is named. win32 keeps the check-then-delete fallback on the same
    terms as the write — see `_run_dir_is_confined` for that residual.

    A plain unlink, not retrying_unlink: launches run on the Textual event
    loop, and dropping a best-effort hint is not worth ~5s of blocked win32
    backoff — the ceiling above already covers the miss."""
    run_dir = runs.run_dir_for(project, run_id)
    try:
        if DIR_FD_ANCHORED_WRITES:
            dir_fd = open_dir_confined(project, run_dir)
            if dir_fd is None:
                return  # a component we cannot vouch for — see the ceiling
            try:
                os.unlink(_CTL_WINDOW_FILE, dir_fd=dir_fd)
            except FileNotFoundError:
                pass  # already gone: missing_ok, by hand
            finally:
                os.close(dir_fd)
        else:
            if not _run_dir_is_confined(project, run_dir):
                return  # see the ceiling
            (run_dir / _CTL_WINDOW_FILE).unlink(missing_ok=True)
    except OSError:
        pass  # a removal we cannot force — see the ceiling


def _is_link_of_any_kind(path: Path) -> bool:
    """Whether `path` is a link that redirects traversal — symlink or, on win32,
    a junction. Raises `OSError` for a component that cannot be probed, which
    the caller turns into a refusal.

    `is_symlink()` alone is not enough, and the gap is win32-shaped. It answers
    for the symlink reparse tag only and returns **False** for a directory
    junction, which redirects traversal identically. A junction is also the
    *easier* plant of the two: `mklink /J` needs neither elevation nor Developer
    Mode, while a symlink needs one of them. So the check this backs would have
    been blind on win32 to the cheaper version of the very attack it exists for.

    Detected by the reparse-point attribute rather than `os.path.isjunction`,
    which only exists from 3.12 — this project supports 3.11, and that leg is
    one CI runs on win32. One `lstat`, no version branch: `st_file_attributes`
    is win32-only, so the bit test degrades to False on POSIX, where `S_ISLNK`
    is already the whole answer.

    Any reparse point counts, not just the junction tag. Other kinds (cloud
    placeholders, app-exec links) have no business being a run dir, and the
    failure this produces is a refusal to write a best-effort hint — the lookup
    degrades to the name scan. Over-refusing is the cheap direction here."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)  # win32-only field
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _run_dir_is_confined(project: Path, run_dir: Path) -> bool:
    """Whether `run_dir` is reached from `project` without traversing a link.

    `follow_symlinks=False` refuses a link at the *final* component only, which
    leaves the ancestors: a session that replaces `.bmad-loop/runs/<run_id>`
    with a link to an external directory holding a `state.json` passes
    `runs.is_run` — it follows the link — and then `mkstemp`/`os.replace` land
    the record inside the linked-to directory. The escape is narrower than the
    final-component one (the name written is always `ctl-window`, so the reach
    is another project's record rather than any file), but it is the same shape.

    Every component below `project` is checked, and `project` itself is not: the
    operator chooses where the project lives and may well keep it behind a link,
    while everything under it is session-writable. `lstat`-based throughout, so
    the check never resolves through what it is testing for.

    Each component goes through `_is_link_of_any_kind`, not `is_symlink()` —
    on win32 the latter is blind to a junction, which redirects the same way and
    is the easier of the two to plant. That also fixes a quieter gap: `Path`'s
    predicates swallow the `OSError` from a component that cannot be probed and
    answer False, so an unreadable ancestor used to be walked *past* as "not a
    link" — the opposite of the sentence below. Raising from the probe is what
    makes that sentence true.

    A check, not a race-free open: the portable answer would be to walk the
    components with `dir_fd`, which POSIX has and win32 does not, and this
    record is atomic precisely for the win32 leg. So the standing redirect —
    plant a link, wait for a launch — is what this removes; a session that
    re-plants inside the window between check and write still wins. That
    residual is bounded by the two facts above: same uid as the writer, and a
    fixed filename carrying a window id."""
    try:
        if not run_dir.is_relative_to(project):
            return False
        cursor = run_dir
        while cursor != project:
            if _is_link_of_any_kind(cursor):
                return False
            cursor = cursor.parent
    except OSError:
        return False  # a component we cannot probe is one we cannot vouch for
    return True


def _record_ctl_window(project: Path, run_id: str, win_id: str) -> None:
    """Record the window a launch just minted, so ctl_window_id can prefer it
    over an older window sharing the run id.

    Best-effort on purpose. The window is already running by the time this
    writes, so a failed write must not fail the launch — the lookup degrades to
    the name scan, i.e. to the behaviour before this record existed. A failure
    forgets the previous record rather than leaving it: degrading to the scan is
    the intended fallback, answering a superseded window is not.

    Skipped when there is no run yet: a fresh `run`/`sweep` mints the only
    window carrying its run id (nothing to disambiguate), and the run dir is
    created by the detached child — this record deliberately never mkdirs one,
    and must not be written into a run-dir-shaped directory (pruned, partial)
    that runs.is_run reports as not a run.

    That skip forgets too, and the "nothing to disambiguate" clause above is
    exactly why it must. The clause holds for the case it was written for —
    `new_run_id` mints a fresh id, so no other window carries it — but it does
    not hold for every way of reaching this branch. resume/resolve read state,
    raise a confirm modal, and launch from the callback, so anything that
    removes `state.json` inside that human-length window arrives here with a
    predecessor window live and a previous launch's record still on disk. That
    record names the window this launch just superseded, and ctl_window_id
    prefers any record that still resolves — so `a` and `x` would answer the
    parked predecessor while the orchestrator just minted keeps running, which
    is #482's symptom reintroduced by the record meant to fix it. Dropping it
    puts the lookup back on the name scan, which is this file's stated
    preference throughout: degrading to the scan is the intended fallback,
    answering a superseded window is not.

    Atomic, not a bare write_text: the record is read cross-process (`bmad-loop
    attach`), and on win32 an AV/indexer holding the previous record open fails
    a plain overwrite with a transient sharing violation — which would swallow
    into the forget path and quietly degrade the lookup. atomic_replace retries
    exactly that violation, turning most real-world failures into successes.

    The guard is type-agnostic on purpose, and `OSError` is not wide enough to
    hold it: `atomic_write_text` resolves the path before its own try, and below
    3.13 `Path.resolve` reports a symlink loop as `RuntimeError` — which would
    crash the launch this docstring promises to spare, on the interpreters the
    3.11/3.12 legs run. Same widening, same reason, as the engine's deferred-close
    rollback (`Engine._restore_deferred_closes`). `Exception` and not
    `BaseException`, so a genuine KeyboardInterrupt still gets out.

    `follow_symlinks=False`, so a symlink at the path is replaced rather than
    written through. Following one is the helper's default contract ("a ledger
    symlinked into the repo keeps being a symlink"), and it is right there — for
    an operator-curated ledger. This sidecar is the opposite: machine-minted,
    per-run, disposable, and living under the project root that every coding
    session can write. Honouring a link here would let a session aim a
    *host-side* write at any path the user can write — reach that the adapters
    confining a session to the workspace otherwise deny it. The payload is only
    a window id, so the primitive is truncation rather than injection, which
    bounds the damage without making it acceptable.

    Replacing rather than refusing, and no preflight `is_symlink` check: a check
    leaves the window between itself and the write, which a session that
    re-plants the link wins. `os.replace` does not dereference its destination,
    so the link is clobbered whenever it was planted. That also self-heals — the
    record ends up a plain file again — where a refusal would leave the planted
    link in place for the next launch to trip over.

    Anchored at a directory descriptor where the platform has one. The final
    component is covered by `follow_symlinks=False` above, but the *ancestors*
    are not, and a path check over them (`_run_dir_is_confined`) is answered
    about a path — stale the moment it returns, so a session that re-plants
    `.bmad-loop/runs/<run_id>` between check and write still redirects the
    record out of the workspace. `open_dir_confined` walks those components
    `O_NOFOLLOW` and hands back the descriptor for the directory it reached, and
    `atomic_write_text_at` then never names a path again — so a later swap
    renames something this no longer consults, and there is no window to win.

    win32 keeps the check-then-write path: it has no `*at()` family to anchor
    against (its CPython config defines neither HAVE_RENAMEAT nor HAVE_OPENAT),
    so the descriptor cannot be opened there at all. The residual is documented
    on `_run_dir_is_confined` and bounded by the two facts it names — same uid
    as the writer, and a fixed filename carrying a window id.
    """
    run_dir = runs.run_dir_for(project, run_id)
    if not runs.is_run(run_dir):
        _forget_ctl_window(project, run_id)
        return
    try:
        if DIR_FD_ANCHORED_WRITES:
            dir_fd = open_dir_confined(project, run_dir)
            if dir_fd is None:
                return  # unconfined, or a component we cannot vouch for
            try:
                atomic_write_text_at(dir_fd, _CTL_WINDOW_FILE, win_id)
            finally:
                os.close(dir_fd)
        else:
            if not _run_dir_is_confined(project, run_dir):
                return
            atomic_write_text(run_dir / _CTL_WINDOW_FILE, win_id, follow_symlinks=False)
    except Exception:
        _forget_ctl_window(project, run_id)


def ctl_window_id(project: Path, run_id: str) -> str | None:
    """Stable window id (bare `@N` on tmux, session-qualified on psmux) of the
    control-session window hosting this run's orchestrator process
    (start_detached names windows <kind>-<run_id>), or None when the run was
    not launched from the TUI or the session is gone.

    An id, not a name, because every consumer replays the value as a
    select/kill/option target: one resolve feeds all of them, so a rename or a
    window minted between two verbs cannot send them to different windows, and
    the value survives tmux's automatic-rename.

    `<kind>-<run_id>` is not unique — a resume launched over a still-parked run
    window shares the run id, and nothing reaps the parked one in between — so
    the name scan alone answers whichever match the listing emits first (tmux
    orders by window *index*, and it gives a new window the lowest free index,
    so a superseded window usually but not always sorts ahead of the live one).
    The id the run's last launch minted is recorded in the run dir and wins
    whenever the listing still shows it under this run id. A record that is gone
    (killed, pruned) or now carries another run's name is ignored rather than
    replayed: a target that no longer resolves is the dangerous kind of stale —
    on psmux an unresolvable `-t` lands on the *active* window (psmux/psmux#545;
    tmux merely errors, which the best-effort consumers turn into a silent
    no-op). With no record at all the answer is the first tagged match.

    Scoped to `project` by the PROJECT_OPTION tag, and by nothing else: only a
    row carrying one of this project's accepted tags is a candidate. The control
    session is shared across projects, and a run id is only unique within one
    (`--run-id` is caller-supplied), so a same-id window belonging to another
    project would otherwise be a legal match here — for `x` that means killing a
    *live* orchestrator next door. _ctl_window_candidates reads tags by the same
    rule, but only that half is shared: its untagged windows are still admitted
    by the run dir, because the pruning consumer of that shape is #419's.

    An untagged row is never a candidate, however it is vouched for (#750).
    Every proof tried for one was a fact the workspace can forge. The run dir is
    a coincidence of a caller-supplied id (#531). The record lives under the
    project root every coding session can write, and the window id it names is
    a reusable handle — tmux 3.4 and psmux 3.3.8 both restart at `@0`/`@1` after
    a server restart. A pane pid recorded beside it is observable rather than
    secret: any same-UID process reads it from the process table, along with the
    `TMUX` socket path in the pane's environment, and anything minted into the
    window at creation passes through the multiplexer client's argv, which the
    process table also shows. The tag is the one claim that needs a write
    through the multiplexer to forge, so it is the whole proof.

    The honest bar, then: forging ownership here requires a mux write, exactly
    as forging the tag does — no more, and no less. On a host where the coding
    session runs unsandboxed as the same user, that is any process that can
    find the socket, which the pane environment hands out; the gate is only as
    strong as the session's isolation from the multiplexer.

    What it costs is reach for a window whose tag cannot be read as ours: one
    whose best-effort tag write failed at launch (start_detached records before
    it tags, but the record admits nothing on its own), and — every window at
    once — a listing whose option column could not be read at all, which psmux
    folds to "" rather than failing (PsmuxMultiplexer.list_windows). The seam
    hands both back as the same empty tag, so neither this function nor its
    callers can tell them apart. Fail closed is still right — the alternative
    is not "reach my window" but "reach *a* window", possibly a neighbour's live
    orchestrator — but it must not be silent: a None here reads as "no window",
    and `x` would then report the run stopped while its window keeps running.
    So ctl_window_lookup also counts the same-run rows it refused for want of a
    readable tag, and `x` and `a` say so (see kill_ctl_window and the TUI's
    attach) instead of passing the refusal off as an absence.

    Read-only throughout: this never writes the record and never touches the
    tag — re-tagging on read is claiming, not proving, and would hand a
    neighbour's window this project's tag."""
    return ctl_window_lookup(project, run_id)[0]


def _list_ctl_windows(
    mux: TerminalMultiplexer, session: str, fields: list[str]
) -> list[tuple[str, ...]]:
    """`mux.list_windows`, made to fail loud. The seam's list_windows answers
    [] both for a session with no windows and for a query that failed — it
    only warns, on a stderr the TUI captures — so an empty answer here would
    read as a clean absence: `x` reporting a run stopped over a live window,
    a prune reporting nothing to close. An empty answer is therefore confirmed
    with list_window_ids, whose [] is a positive claim (the session listed
    empty, or is proven gone) and which raises MultiplexerError itself when
    its listing cannot be taken. Empty here but windows there is the failed
    read, and raises the same type.

    One extra query, and only when the listing came back empty. Ceiling: a
    window minted between the two reads also lands in the raise — loud, never
    silent, and the next look answers it."""
    rows = mux.list_windows(session, fields)
    if not rows and mux.list_window_ids(session):
        raise MultiplexerError(
            f"could not list the windows of {session}: the listing answered none "
            "while the session has some"
        )
    return rows


def ctl_window_lookup(project: Path, run_id: str) -> tuple[str | None, int]:
    """ctl_window_id's answer, plus how many windows carrying this run's name
    it refused because their tag read empty — unset, or unreadable (see
    ctl_window_id).

    A nonzero count is a degraded answer a caller must surface, with or
    without a window: no window is not "no window", and a window beside a
    refused row may be the parked predecessor of a relaunch whose own tag
    write failed — so the live orchestrator is the one refused, and a stop
    that closes the predecessor has still left it running. A refused row may
    equally be a neighbour's untagged window under the same caller-supplied
    run id, so the count is a notice, never a target.

    Raises MultiplexerError when the listing itself could not be read (see
    _list_ctl_windows): that is not "no window" either, and every caller
    surfaces it — the TUI's guarded attach, the stop worker, the CLI's error
    backstop, ctl_window_recorded's "could not confirm"."""
    if not mux_available():
        return None, 0
    mine = runs.accepted_tags(project)
    tagged: list[str] = []
    unproven = 0
    rows = _list_ctl_windows(
        get_multiplexer(), ctl_session(project), ["window_id", "window_name", runs.PROJECT_OPTION]
    )
    # Below the listing. Listing and record are two reads of a state a concurrent
    # relaunch can move between them, never one snapshot, so the ordering is the
    # only thing to get right and this keeps the one the record has always had.
    # Read FIRST, a relaunch landing in the gap leaves a record older than the
    # listing: it names the window that relaunch superseded, the listing shows it,
    # and `recorded in tagged` replays the corpse. Read here, the same relaunch
    # leaves a record newer than the listing, naming a window the listing does not
    # carry yet — so it fails the re-prove and the answer falls back to a match
    # that was at least live when the listing was taken. `rows` is materialized
    # (list_windows returns a list), so the loop pays nothing for the move.
    recorded = _read_ctl_window(project, run_id)
    for win_id, name, tag in rows:
        # win_id can be "": psmux's qualifier passes a falsy id through. An
        # empty id must never become a target — an empty `-t` resolves against
        # the *current* window. (The base's short-row padding CAN produce an
        # empty *tag* — it fills trailing fields — which the tag test below
        # refuses; window_id stays field 0 of 3.)
        if not win_id:
            continue
        # The whole run id, not a suffix of the name: RUN_ID_RE admits `-`, so
        # `--run-id other-RID` mints `run-other-RID`, which ends with `-RID` and
        # would answer a lookup for `RID` — and sorts ahead of it, so `x` kills
        # the neighbour's LIVE orchestrator. Parsed with the same regex
        # _ctl_window_candidates uses, which also confines a match to the four
        # kinds start_detached mints rather than any name ending this way.
        m = _CTL_WINDOW_RE.match(name)
        if m is None or m.group(1) != run_id:
            continue
        # Set membership, with no "the tag looks unsafe here" escape: the digest
        # arrives as it was written, so a nonempty tag outside the accepted set
        # belongs to another project and must not be a candidate — `x` resolves
        # through here, and admitting a foreign row lets a stop cross a project
        # boundary. The set is what keeps a window tagged by an earlier release
        # reachable: the control session is long-lived and survives the upgrade
        # that changes the tag's spelling, so comparing against the current
        # digest alone would strand this project's own orchestrator — prunable
        # by _ctl_window_candidates, which accepts the legacy tag, yet
        # unreachable by `a` and `x`, which resolve through here.
        # An empty tag is refused the same way (#750), but counted: it is unset
        # or unreadable, and the caller must be able to say so (see docstrings).
        if tag in mine:
            tagged.append(win_id)
        elif not tag:
            unproven += 1
    if not tagged:
        return None, unproven
    # Membership in `tagged`, not mere presence in the listing: it re-checks the
    # name and the project-scoping predicates, so a record whose id is absent from
    # the scoped matches — killed, pruned, renamed onto another run, or naming a
    # row this project cannot claim — is not replayed. A tie-break among rows the
    # tag already proved, never a proof on its own: it is what turns a stale id
    # from a replayed target into a fallthrough.
    return (recorded if recorded in tagged else tagged[0]), unproven


def unproven_ctl_window_notice(project: Path, run_id: str, count: int) -> str:
    """Operator wording for a nonzero ctl_window_lookup count: what was refused,
    the two causes the seam cannot tell apart, and what to do about it. Worded
    by count, so one window and several each read as a sentence."""
    where = f"in {ctl_session(project)} named for run {run_id}"
    if count == 1:
        return (
            f"a window {where} has no readable project tag, so it cannot be proven "
            "this project's: its tag could not be read or was never written. Left "
            "untouched — check it in the multiplexer and close it by hand if it is "
            "this run's"
        )
    return (
        f"{count} windows {where} have no readable project tag, so none can be "
        "proven this project's: their tags could not be read or were never written. "
        "Left untouched — check them in the multiplexer and close by hand any that "
        "are this run's"
    )


def ctl_window_recorded(project: Path, run_id: str, win_id: str) -> bool:
    """Whether `ctl_window_id` now answers `win_id` for this run — i.e. whether
    the launch's disambiguation actually took.

    False means the launch itself succeeded but the lookup will not answer
    the window it minted: either it is back on the ambiguous first-match scan,
    which is exactly #482's symptom, or — since #750 — the window's
    best-effort tag write did not land, so the lookup refuses it outright and
    `a`/`x` cannot reach it at all. Both are operator-visible: every launcher
    should report it rather than let an unqualified success toast imply the
    targeting is sound (start_run_detached and start_sweep_detached included,
    which mint the only window under a fresh run id but still depend on its
    tag). Split out of resume_detached's return so the resolve
    path can warn while still keeping the captured id it attaches with.

    Asks `ctl_window_id` rather than comparing the record to `win_id`, because
    a round-tripped record is not the same claim. A backend whose
    `new_parked_window` id is shaped differently from its `list_windows`
    `window_id` column — a divergence the seam explicitly tolerates — writes and
    reads the record back intact while `ctl_window_id` rejects it against the
    listing and falls through to the first match. File equality would report
    that as sound; it is the precise case the warning exists for.

    An unanswerable listing counts as not recorded. The probe is observation, so
    it degrades rather than raising into the launchers (neither has a handler
    for it), and "could not confirm" is closer to the warning's own hedge —
    attach/stop *may* target an older window — than silence would be."""
    try:
        return ctl_window_id(project, run_id) == win_id
    except MultiplexerError:
        return False


def ctl_target(project: Path) -> str:
    """Seam-canonical target token for this project's control session; see
    :meth:`TerminalMultiplexer.target`. Windows are targeted by stable id
    (ctl_window_id), never by name through this token."""
    return get_multiplexer().target(ctl_session(project))


def select_ctl_window_id(window_id: str) -> None:
    """Make the window with this id (from start_detached/ctl_window_id) the
    control session's current window, so a plain attach to the session lands
    on it (attach-session itself takes no window)."""
    get_multiplexer().select_window(window_id)


# Per-window tmux user option recording what an interactive attach should do
# with the client once the window's command exits (consumed by the multiplexer's
# parked-window return trailer; see start_detached and the tmux backend). Set by
# set_return_pane at attach time. Value is either a backend-composed pane
# target — replayed opaquely, so each backend records the form its own
# switch-client resolves: a bare pane id (%N) on tmux, =session:%N on psmux,
# whose one-server-per-session model cannot resolve a bare id from the control
# session (psmux/psmux#483) — used when the TUI runs inside the multiplexer and
# switched its own client over; or RETURN_DETACH, used when the TUI runs
# outside and a throwaway client was attached that must detach so the
# suspended TUI resumes.
RETURN_OPTION = "@bmad_return_pane"
RETURN_DETACH = "detach"  # pane targets are %N / =sess:%N, never "detach"


def current_return_target() -> str | None:
    """Backend-composed target of the pane this process runs in — the place an
    attach should return the client to — or None when not inside the
    multiplexer / it is unavailable. The value is opaque to callers: record it
    with set_return_pane, replay it via switch_client / the parked trailer.
    See TerminalMultiplexer.current_return_target for the composition
    contract."""
    return get_multiplexer().current_return_target()


def set_return_pane(window_target: str, target: str) -> None:
    """Record `target` (a current_return_target value or RETURN_DETACH) as the
    return move on a control-session window, so its trailing shell sends the
    client back there when the window's command exits. `window_target` is any
    window spec the backend accepts; callers pass the id from
    start_detached/ctl_window_id so the write lands on the window the caller
    already resolved, not on whatever a fresh by-name lookup answers."""
    get_multiplexer().set_window_option(window_target, RETURN_OPTION, target)


def current_session() -> str | None:
    """Name of the tmux session this process is running inside, or None when
    not in tmux / tmux is unavailable."""
    return get_multiplexer().current_session()


def in_ctl_session() -> bool:
    """True when we are running inside a control-session window (i.e. launched
    detached by the TUI), as opposed to a user's own shell. Backend-honest:
    current_session() is None whenever this process is not inside the selected
    multiplexer, so no direct TMUX/HERDR_* env sniffing happens here. The
    shape predicate rather than one project's resolved name: the question its
    callers ask is "am I in A control session", and on a namespacing transport
    the name carries a registry suffix (runs.ctl_session_for)."""
    session = current_session()
    return session is not None and runs.is_ctl_session_name(session)


def detach_client() -> bool:
    """Detach the tmux client viewing the current session, handing the terminal
    back to the user. Processes in the session keep running. Returns True iff a
    client was actually detached — False both when the transport failed and when
    there was nothing attached (see TerminalMultiplexer.detach_client for how
    each backend establishes that)."""
    return get_multiplexer().detach_client()


class ReturnOutcome(StrEnum):
    """What return_attached_client managed to do — and, for a caller that goes
    unattended on the strength of it, whether a human can still answer here.

    A plain boolean cannot carry that: "the hand-back succeeded" and "there is
    still someone at this terminal" are independent, and the ways of failing
    point in different directions. A *refused* switch leaves the client sitting
    in this very window; a switch the backend cannot vouch for may already have
    moved it; a failed *detach* reports no verified hand-back. Three claims, and
    they do not license the same response."""

    RETURNED = "returned"
    #: No hand-back, but a human may still be here: nothing was recorded to
    #: return to (a plain foreground sweep), the backend is unusable, or the
    #: switch failed with the client still in this window. The conservative
    #: answer — a caller must keep talking to the terminal.
    ATTENDED = "attended"
    #: A hand-back was attempted and did not verifiably happen: the detach found
    #: nothing attached, the switch could not be vouched for (a timed-out verb,
    #: an unreadable client count, nothing attached to move), the effect could
    #: not be observed, or the backend has no detach verb at all (herdr). A
    #: caller must not rely on anyone answering a prompt in this window — a
    #: policy for the uncertainty, not a proof that the window is empty (see
    #: return_attached_client for why it is the safe way to be wrong).
    UNREACHABLE = "unreachable"


def return_attached_client() -> ReturnOutcome:
    """Hand an attached client back to its origin *now*, mid-process — the
    parked-window return move (see start_detached) executed while the window's
    command keeps running in the background, instead of after it exits.

    Reads the RETURN_OPTION recorded on the current window by set_return_pane:
      - a pane target (backend-composed: bare %N on tmux, =session:%N on
        psmux): switch that client back there (`-l` fallback if it's gone);
      - RETURN_DETACH: detach the client so a blocking `tmux attach` returns;
      - unset/empty: nobody attached with a return target — do nothing.
    The option is cleared only on RETURNED: a real return must not make the
    parked window's trailer fire a second one, a failed return is left for the
    trailer to retry. That retry is a second chance, not a rescue —
    new_parked_window parks on a blocking read *before* the trailer, so it runs
    only once a human dismisses the park prompt, never in the unattended case.

    The two failures are not interchangeable, which is why this answers a
    ReturnOutcome and not a bool. A failed switch is positive evidence that the
    client is still in this window, so ATTENDED keeps the caller prompting —
    but only because the seam reserves False for that joint claim. A backend
    that merely cannot vouch for the move (psmux's timed-out verb, an
    unreadable client count, nothing attached to move) answers None, and that
    lands in UNREACHABLE instead. The routing is load-bearing, not tidiness:
    an ATTENDED the client has already walked away from cannot be recovered by
    the surviving return option, because a --repeat cycle prompting into the
    empty window blocks on input() before anyone can reach the trailer. A
    failed detach carries no such evidence in general: on tmux it does
    (`detach-client` fails with "no current client"), but off tmux False also
    covers an effect the backend could not observe and a detach verb it does
    not have at all — herdr, whose False rather than None is exactly what
    detach_client's own widening to a returned bool (#317) buys. That is a
    different widening from switch_client's third state above; detach_client is
    the verb that stays a bool. UNREACHABLE is the policy for all of them, the
    unvouched switch included, because the two ways of being wrong are not
    equally bad: prompting into a
    window no one is viewing blocks a --repeat sweep on input() forever, while
    going unattended in front of a human only defers this cycle's decisions to
    `bmad-loop decisions` or the next attended sweep."""
    mux = get_multiplexer()
    if not mux_usable(mux):
        return ReturnOutcome.ATTENDED
    win = mux.current_window_id()
    if win is None:
        return ReturnOutcome.ATTENDED
    ret = mux.show_window_option(win, RETURN_OPTION)
    if not ret:
        return ReturnOutcome.ATTENDED
    if ret == RETURN_DETACH:
        outcome = ReturnOutcome.RETURNED if mux.detach_client() else ReturnOutcome.UNREACHABLE
    else:
        switched = mux.switch_client(ret, last_fallback=True)
        if switched is None:
            outcome = ReturnOutcome.UNREACHABLE
        else:
            outcome = ReturnOutcome.RETURNED if switched else ReturnOutcome.ATTENDED
    if outcome is ReturnOutcome.RETURNED:
        mux.unset_window_option(win, RETURN_OPTION)
    return outcome


def decision_pending(run_dir: Path) -> bool:
    """True when the run's sweep is currently blocked on an interactive decision
    — its journal's last entry is a decision-pending announcement (the prompter
    blocks on input right after writing it, so any later entry means it moved
    on). Mirrors tui.data.pending_decision; kept here so the CLI can decide an
    attach target without importing the textual-laden data module."""
    entries = Journal(run_dir).entries()
    return bool(entries) and entries[-1].get("kind") == "decision-pending"


def attach_plan(
    project: Path, run_id: str, *, on_fault: Callable[[str], None] | None = None
) -> tuple[tuple[list[str], str | None] | None, int]:
    """Pick where an interactive attach should land for this run and which window
    (if any) to record a return target on. Shared by the CLI `attach` command and
    mirroring the TUI's action_attach logic: prefer the orchestrator's ctl window
    when a sweep is blocked on a decision or no agent session is live, else the
    live agent session. Returns ((tmux argv, return_window) or None when there
    is nothing to attach to, unproven) — `unproven` is ctl_window_lookup's count
    of same-run windows refused for an empty tag, carried out with the plan
    because a None plan is not "no window" when it is nonzero, and an agent
    plan may have bypassed the very window a pending decision is waiting in
    (#750). The caller must say so; see unproven_ctl_window_notice.

    A ctl lookup that raises (its listing could not be read) is handed to
    `on_fault` and the plan carries on as if there were no ctl window: the
    agent session may still be reachable, and an attach must not be refused
    for a window it could not check. With no `on_fault` the raise propagates."""
    session = runs.session_name(run_id)
    try:
        win_id, unproven = ctl_window_lookup(project, run_id)
    except MultiplexerError as e:
        if on_fault is None:
            raise
        on_fault(str(e))
        win_id, unproven = None, 0
    agent_live = agent_session_exists(session)
    if win_id is not None and (
        decision_pending(runs.run_dir_for(project, run_id)) or not agent_live
    ):
        select_ctl_window_id(win_id)
        return (runs.attach_target_argv(ctl_target(project)), win_id), unproven
    if agent_live:
        return (runs.attach_target_argv(runs.session_target(run_id)), None), unproven
    return None, unproven


def kill_ctl_window(project: Path, run_id: str) -> int:
    """Kill the control-session window hosting this run's orchestrator process,
    if any. A no-op when the run was not launched from the TUI or tmux is gone.

    Returns how many same-run windows were left alive because none could be
    proven this project's (ctl_window_lookup's count): 0 means the kill went
    through or there was nothing to kill, and anything else is a window the
    caller must report as possibly still running rather than as stopped.

    Raises MultiplexerError when the ctl listing could not be read at all
    (ctl_window_lookup), and when the window it killed is still listed
    afterwards: kill_window is best-effort by contract — a transport failure
    is a silent no-op — so the kill is confirmed against list_window_ids, the
    same membership verdict prune_ctl_windows takes. Neither may be reported
    as a clean stop (#750)."""
    win_id, unproven = ctl_window_lookup(project, run_id)
    if win_id is not None:
        mux = get_multiplexer()
        mux.kill_window(win_id)
        if win_id in mux.list_window_ids(ctl_session(project)):
            raise MultiplexerError(f"control window {win_id} survived the kill")
    return unproven


def _ctl_window_candidates(project: Path) -> list[tuple[str, str]]:
    """(window_id, window_name) for parked control-session run windows whose run
    is no longer live — the kill candidates for a prune.

    A `<kind>-<run_id>` window parks on a `read` prompt that never closes on its
    own; it is a candidate once its run has finished/stopped/crashed (or its run
    dir is gone). The current window is excluded so a prune triggered from inside
    the ctl session never targets itself; live runs and the session's own shell
    window are excluded too.

    The control session is shared across projects, so its per-window PROJECT_OPTION
    accepts current and legacy project tags; untagged windows still require a run
    directory under this project (mirrors runs.prunable_sessions).

    Residual, left visible rather than fixed: when psmux's option probe fails,
    PsmuxMultiplexer.list_windows reads every tag as empty, so an untagged row
    of ours whose run dir is gone is skipped here without a report. Telling an
    unreadable tag from an unset one needs an `on_fault` on list_windows, which
    is a seam change.
    """
    mux = get_multiplexer()
    ctl = runs.ctl_session_for(project, mux)
    if not mux_usable(mux):
        return []
    # A False has-session is weaker than it looks (its seam note): a refused
    # connect reads the same as a missing session. So it only short-circuits
    # when list_window_ids agrees there is nothing — whose [] is a positive
    # claim, and which raises when its own listing cannot be taken (#750).
    if not session_exists(ctl) and not mux.list_window_ids(ctl):
        return []
    current = mux.current_window_id()
    # Fail loud on a listing that failed: both prune callers already report a
    # raise from this scan, and an empty answer would read as nothing to prune.
    rows = _list_ctl_windows(mux, ctl, ["window_id", "window_name", runs.PROJECT_OPTION])
    mine = runs.accepted_tags(project)
    candidates: list[tuple[str, str]] = []
    for win_id, name, tag in rows:
        if not win_id or win_id == current:
            continue
        m = _CTL_WINDOW_RE.match(name)
        if m is None:
            continue  # not a run window (e.g. the session's initial shell)
        if not runs.is_parsable_run_id(m.group(1)):
            # A foreign/mangled window name must not steer a run-dir path. The
            # PARSE-side predicate: this window already exists, so the mint's
            # broad ctl reservation would leak every pre-upgrade `run-ctl-*`
            # window out of the sweep instead of closing it.
            continue
        run_dir = runs.run_dir_for(project, m.group(1))
        if tag:
            if tag not in mine:
                continue  # another project's window
        elif not runs.is_run(run_dir):
            continue  # untagged and no run dir here — ownership unprovable
        # boolean gate on purpose: an 'unknown' engine stays a candidate (unknown
        # never blocks cleanup) with no per-window warning — the session-level
        # unknown warning from prunable_sessions covers the operator surface.
        if runs.engine_alive(run_dir):
            continue
        candidates.append((win_id, name))
    return candidates


def prunable_ctl_windows(project: Path) -> list[str]:
    """Names of the control-session windows a prune would close (dry-run view)."""
    return [name for _, name in _ctl_window_candidates(project)]


def prune_ctl_windows(project: Path) -> tuple[list[str], list[str], list[str]]:
    """Close parked control-session windows whose run is no longer live; returns
    (removed, survived, unverifiable) window names (see _ctl_window_candidates).
    A three-list tuple like runs.prune_sessions, but do NOT read the arms across:
    that one partitions BEFORE its kills, so its `killed` is still an attempted
    kill, its `live` is "deliberately not touched" rather than "survived", and its
    `unknown` is a pid question and a SUBSET of `killed`. These three are disjoint
    (by window id — the values are names) and all three are about kill outcome.

    kill_window is best-effort by contract (a hang, a missing binary, and a
    refused kill are all the same silent no-op), so an attempted kill is not a
    removal and must not be reported as one (#435). The verdict is taken here
    rather than pushed into the seam because this is the caller that both needs
    it and already holds the session: kill_window(target) alone cannot verify
    anything on a backend whose liveness listing is session-scoped, which is all
    of them.

    ONE listing after the whole fan-out, not a probe per window: the answer is a
    set membership either way, so the verdict costs one extra round trip instead
    of N. A transport fault raises and nothing can be claimed there.

    One ceiling, deliberate: the membership test pairs list_windows' `window_id`
    column with list_window_ids. The seam states its symmetry rules pairwise and
    this pair is stated because of THIS caller — a backend qualifying one side
    and not the other reads every candidate as removed, which is #435 restored
    on the optimistic side, with no error anywhere.

    `[]` is read as "the session went with its last window", and since #525 the
    seam means exactly that: a listing that merely FAILED raises instead of
    folding to `[]`, so it lands in `unverifiable` below rather than reporting
    every candidate removed. The narrow reading is what keeps the pessimism
    honest in the other direction too — a genuinely vanished session still
    answers `[]`, so its kills are reported as removed instead of leaving a
    phantom survivor for every future sweep to re-report.
    """
    mux = get_multiplexer()
    candidates = _ctl_window_candidates(project)
    if not candidates:
        return [], [], []
    for win_id, _name in candidates:
        # kill_window is best-effort and reports nothing; a strict-POSIX decode
        # fault of the kill's own capture escapes its swallow tuple (#380) but
        # says exactly as little about the outcome — the command may well have
        # reached the server. More of the same nothing: the fan-out continues
        # and the one post-kill listing hands down the verdict either way. An
        # escape here would surface at the callers as a scan failure, an
        # empty-armed receipt denying kills that just fired.
        try:
            mux.kill_window(win_id)
        except UnicodeError:
            pass
    try:
        live = set(mux.list_window_ids(runs.ctl_session_for(project, mux)))
    except MultiplexerError:
        # The kills may well have landed; nothing here can say so. Claiming the
        # optimistic half is exactly the bug — the next cleanup pass retries.
        return [], [], [name for _win_id, name in candidates]
    removed = [name for win_id, name in candidates if win_id not in live]
    survived = [name for win_id, name in candidates if win_id in live]
    return removed, survived, []


def ctl_session(project: Path) -> str:
    """The control-session name for this project on the selected transport
    (see `runs.ctl_session_for`). The TUI-facing spelling, so `tui/app.py`
    and the screens name the session an operator would actually attach to."""
    return runs.ctl_session_for(project, get_multiplexer())


def _ensure_ctl_session(project: Path) -> str:
    mux = get_multiplexer()
    name = runs.ctl_session_for(project, mux)
    # has_session is raiser-side (a server-backed backend can fail the probe after
    # the availability pre-gate). Keep it inside the try so a transport failure
    # converts to LaunchError, which the TUI launch/resume/resolve handlers already
    # catch — otherwise the raw MultiplexerError slips past them and crashes the app.
    try:
        if not mux.has_session(name):
            mux.new_session(name, project)
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer ctl-session setup failed: {e}") from e
    return name


def cli_argv(*tail: str) -> list[str]:
    """`sys.executable -m bmad_loop.cli ...` — immune to PATH/venv drift
    inside tmux windows."""
    return [sys.executable, "-m", "bmad_loop.cli", *tail]


def _registry_drift(project: Path, mux: TerminalMultiplexer) -> str | None:
    """Why a run launched from this process would land in a registry this
    process does not watch, or ``None`` when it would not.

    The registry is settled once per process (`cli._configure_mux`), but the
    detached child re-reads ``[mux] honor_ambient_psmux_data_dir`` from
    policy.toml, which the settings editor can rewrite under a running TUI. A
    TUI that started honouring the operator's root and then had the switch
    turned off would launch children into the derived root while it goes on
    querying the old one: it could not see, attach to or stop what it started.
    So the child's answer is predicted here with the same pure rule it will
    apply (`runs.resolve_psmux_registry_root`), from the root it inherits —
    this process's root in force — and a disagreement refuses the launch.
    The prediction assumes inheritance. Under `PSMUX_BARE_ENV` a pane child
    inherits no `PSMUX_DATA_DIR` (psmux re-adds only its allowlist), so it
    derives. bmad-loop does not support that mode, and
    `PsmuxMultiplexer._warn_if_bare_env` says so.

    Asked only of a process that configured its registry for this project
    (`runs.settled_project`), which every CLI entry does: there is nothing to
    disagree with otherwise. The other direction (switch turned ON) leaves the
    child where this process is — it inherits the derived root, which the rule
    never honours as a pin — but no longer where the operator is: a TUI that
    overrode the operator's root R recorded it as displaced, and with the
    switch now on, every shell carrying R would honour it while this TUI and
    its children stay in the derived registry. That is refused too. A TUI
    started without R in its environment (from the Start menu, say) displaced
    nothing, cannot know R, and so has nothing to refuse."""
    if runs.settled_project() != project:
        return None
    try:
        if not mux.has_registry_namespace():
            return None
        root = mux.registry_root()
    except MultiplexerError:
        return None  # selection already proved usable; the launch reports its own faults
    if root is None:
        return None
    try:
        derived = str(runs.mux_registry_root(project))
    except (runs.StateRootError, OSError, RuntimeError):
        return None  # the child cannot derive either, and keeps the root it inherits
    fault: Exception | None = None
    try:
        honor = policy_mod.load(project / policy_mod.POLICY_FILE).mux.honor_ambient_psmux_data_dir
    except (policy_mod.PolicyError, OSError) as exc:
        honor = False  # what the child's `_configure_mux` falls back to as well
        fault = exc
    child = runs.resolve_psmux_registry_root(derived, root, honor_ambient=honor)
    if child == root:
        if honor and root == derived:
            displaced = runs.displaced_psmux_registry_root()
            if (
                displaced
                and runs.resolve_psmux_registry_root(derived, displaced, honor_ambient=True)
                == displaced
            ):
                return (
                    "[mux] honor_ambient_psmux_data_dir was turned on since this TUI "
                    f"started: a new run would stay in the derived registry {root}, while "
                    f"shells carrying your PSMUX_DATA_DIR now use {displaced} — restart "
                    "the TUI (bmad-loop tui), then launch"
                )
        return None
    if fault is not None:
        # The switch may not have changed at all: the child cannot read the
        # policy either, so it falls back to off. Name the real cause.
        return (
            f"policy.toml could not be read ({fault}); a new run would use the registry "
            f"{child}, but this TUI watches {root} — fix the policy, then launch"
        )
    return (
        f"[mux] honor_ambient_psmux_data_dir changed since this TUI started: a new run "
        f"would use the registry {child}, but this TUI watches {root} and could not "
        "see, attach to or stop it — restart the TUI (bmad-loop tui), then launch"
    )


def _forwardable_displaced_root(displaced: str | None, in_force: str | None) -> str | None:
    """The spelling of this process's displaced registry root to hand a detached
    child (see :func:`start_detached`), or ``None`` when there is nothing to
    forward, or nothing that would survive the trip.

    ``str(Path(...))`` drops a trailing separator everywhere but on a root. A
    value that still ends in one *and* contains whitespace — a share root such
    as ``\\\\srv\\my share\\`` — is the shape Windows PowerShell older than 7.3
    corrupts on the way into a parked window's argv (ADR 0001 §6, "Argv fidelity
    on psmux"), so it is not forwarded: a sweep that misses that root is the
    outcome before this forwarding existed, while a corrupted value would name
    a registry nobody used."""
    if not displaced or not os.path.isabs(displaced) or displaced == in_force:
        return None
    normalized = str(Path(displaced))
    if normalized.endswith(("/", "\\")) and any(c.isspace() for c in normalized):
        return None
    return normalized


def start_detached(project: Path, argv_tail: list[str], run_id: str, kind: str) -> str | None:
    """Run a bmad-loop command in a new window of the control session.

    The window parks after the command exits (keeping the exit status
    inspectable) and then returns an attached client to its origin pane — both
    handled by the multiplexer's parked-window primitive, keyed by the
    RETURN_OPTION recorded on the window by set_return_pane.

    Returns the new window's stable backend id (bare `@N` on tmux,
    session-qualified on psmux) so callers can target it unambiguously (window
    names collide when several kinds share a run_id). The same id is recorded in
    the run dir so ctl_window_id answers this window rather than an older one
    under the same run id — see _record_ctl_window.

    Refuses a run id that aliases a control session, FIRST — this is the one
    place every drive path converges on the mutation (the window mint and the
    ctl-window record overwrite): run/sweep launches with freshly validated
    ids, and resume/resolve replaying ids an older release persisted. Gating
    each button separately kept finding the path nobody gated (resolve was
    the fourth); gating the mutation cannot. Ahead of the mux probes so the
    refusal needs no transport to be phrased.

    Forwards this process's displaced psmux registry root, as the hidden
    top-level ``--displaced-registry-root`` ahead of the subcommand. The child
    inherits the derived root and so displaces nothing itself; without the
    option a TUI-launched resume or cleanup would never sweep the operator's
    pre-#537 registry, which only this process recorded. A root of the shape
    older PowerShell corrupts in transit is not forwarded
    (:func:`_forwardable_displaced_root`).
    """
    if runs.run_id_aliases_control_session(run_id):
        raise LaunchError(
            f"run {run_id}: its agent session name is the control session's own — "
            f"cannot be driven. Recover its work by hand, then `bmad-loop delete {run_id}`"
        )
    mux = get_multiplexer()
    if not mux_usable(mux):
        raise LaunchError(
            "multiplexer backend unavailable (binary missing, version unsupported, "
            "or a required helper absent)"
        )
    drift = _registry_drift(project, mux)
    if drift is not None:
        raise LaunchError(drift)
    argv = cli_argv(*argv_tail)
    try:
        forwarded = (
            _forwardable_displaced_root(runs.displaced_psmux_registry_root(), mux.registry_root())
            if mux.has_registry_namespace()
            else None
        )
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer registry query failed: {e}") from e
    if forwarded is not None:
        argv = cli_argv(f"--displaced-registry-root={forwarded}", *argv_tail)
    ctl = _ensure_ctl_session(project)
    try:
        win_id = (
            mux.new_parked_window(
                ctl,
                f"{kind}-{run_id}",
                project,
                argv,
                RETURN_OPTION,
            )
            or None
        )
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer new-window failed: {e}") from e
    if win_id:
        # Record before tagging: a window minted but unrecorded puts the lookup
        # back on the ambiguous scan, while an *untagged* window already has a
        # documented fallback in _ctl_window_candidates — so even a
        # non-conforming backend raising from the (contractually best-effort)
        # set_window_option must not cost the record.
        _record_ctl_window(project, run_id, win_id)
        # Tag the window with its project so a cleanup in another project never
        # closes it (the ctl session is shared across projects).
        mux.set_window_option(win_id, runs.PROJECT_OPTION, runs.project_tag(project))
    else:
        # No id to record: the backend did not capture one. Whatever the previous
        # launch recorded now names a superseded window, so drop it.
        _forget_ctl_window(project, run_id)
    return win_id


def _reachable_window(project: Path, run_id: str, win_id: str | None) -> str | None:
    """`win_id` when ctl_window_id will answer it for this run, else None — the
    launch-time check every launcher's caller turns into a warning.

    A fresh run or sweep mints the only window under its run id and writes no
    record, so the one thing that can still go wrong is the tag: start_detached
    stamps it best-effort, and since #750 an untagged window is refused by the
    lookup, so `a`/`x` would silently miss it. Re-reading through the lookup
    (ctl_window_recorded) asks the consumers' own question, and also catches an
    uncaptured id or a listing that could not be read."""
    if win_id and not ctl_window_recorded(project, run_id, win_id):
        return None
    return win_id


def start_run_detached(
    project: Path,
    run_id: str,
    *,
    spec: str | None = None,
    epic: int | None = None,
    story: str | None = None,
    max_stories: int | None = None,
) -> str | None:
    """Launch a run in a ctl-session window; returns the window id, or None
    when the lookup cannot reach it afterwards — see _reachable_window."""
    tail = ["run", "--project", str(project), "--run-id", run_id]
    if spec:
        tail += ["--spec", spec]  # forces stories mode (folder+id dispatch)
    if epic is not None:
        tail += ["--epic", str(epic)]
    if story:
        tail += ["--story", story]
    if max_stories is not None:
        tail += ["--max-stories", str(max_stories)]
    return _reachable_window(project, run_id, start_detached(project, tail, run_id, "run"))


def start_sweep_detached(
    project: Path,
    run_id: str,
    *,
    no_prompt: bool = False,
    decisions_only: bool = False,
    max_bundles: int | None = None,
) -> str | None:
    """Launch a sweep in a ctl-session window; returns the window id, or None
    when the lookup cannot reach it afterwards — see _reachable_window."""
    tail = ["sweep", "--project", str(project), "--run-id", run_id]
    if no_prompt:
        tail.append("--no-prompt")
    if decisions_only:
        tail.append("--decisions-only")
    if max_bundles is not None:
        tail += ["--max-bundles", str(max_bundles)]
    return _reachable_window(project, run_id, start_detached(project, tail, run_id, "sweep"))


def resume_detached(project: Path, run_id: str) -> str | None:
    """Resume in a ctl-session window; returns the window id, or None when the
    lookup cannot name that window afterwards — the caller should warn, because
    resume is the launch that mints a *second* window under the run id, so this
    is exactly when the ambiguous scan starts answering the superseded one while
    the launch itself succeeded.

    Two ways to land there, one signal: the backend captured no id, or it did but
    the record did not survive (refused, unwritable, run dir pruned mid-launch).
    Both leave `ctl_window_id` on the scan, so reporting only the first would let
    the rest degrade behind an unqualified success toast.

    Verified by re-reading rather than by threading the write's outcome up: it
    asks the question the consumers actually ask — will `ctl_window_id` prefer
    this window — of the same file they will read, instead of a proxy for it.

    Folded into the return here, rather than reported alongside the id as the
    resolve path does, because resume has no immediate use for a window it
    cannot record: it launches and leaves, where resolve attaches to the window
    it just minted and still needs that id to do so."""
    win_id = start_detached(
        project, ["resume", "--project", str(project), run_id], run_id, "resume"
    )
    return _reachable_window(project, run_id, win_id)


def start_resolve_detached(project: Path, run_id: str) -> str | None:
    """Run `bmad-loop resolve <run_id>` in a ctl-session window. The caller
    attaches to it: the resolve agent is interactive, and the post-session
    confirm + resume happen in that same window. Returns the window id so the
    caller attaches to exactly this window, not a stale same-run_id window."""
    return start_detached(
        project, ["resolve", "--project", str(project), run_id], run_id, "resolve"
    )


def run_captured_streams(argv_tail: list[str]) -> tuple[int, str, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its output
    with the two streams kept **apart**.

    Separation is the whole point of this seam. Anything parsing stdout as a
    whole document — a ``--json`` command, whose contract in :mod:`bmad_loop.machine`
    is that stdout is one JSON object and nothing else — cannot use the merged
    form: :func:`run_captured` appends stderr *after* stdout, so a single
    ``DeprecationWarning`` written to the child's stderr by any dependency turns
    ``json.loads`` into ``Extra data:``. That failure is environment-dependent —
    it needs the right interpreter, the right installed versions, the right
    warning filters — so it would pass everywhere it was tested and silently
    degrade the JSON path to the text one on a user's machine. A caller that
    genuinely wants one blob merges them itself; a caller that parses must never
    have been handed the option.

    Decoding is pinned to UTF-8 with ``errors="replace"`` rather than
    ``text=True``, which decodes with the *locale* encoding at ``errors="strict"``
    — the #200 family of failure already fixed CLI-side in :mod:`bmad_loop.machine`.
    A console in a non-UTF-8 code page must not turn a perfectly good document
    into a ``UnicodeDecodeError`` on the way in.
    """
    proc = subprocess.run(
        cli_argv(*argv_tail), capture_output=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_captured(argv_tail: list[str]) -> tuple[int, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its
    combined output for display.

    For text display only. Anything that parses the output must call
    :func:`run_captured_streams` — see its docstring on why the merge is
    unparseable.
    """
    rc, out, err = run_captured_streams(argv_tail)
    if err:
        if out and not out.endswith("\n"):
            out += "\n"
        out += err
    return rc, out
