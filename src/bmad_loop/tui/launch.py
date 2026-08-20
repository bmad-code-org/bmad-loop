"""Detached launching of bmad-loop commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated tmux control session (bmad-loop-ctl) so they survive
TUI exit, and the dashboard observes them through run-dir artifacts exactly
like runs started from a plain shell. Fast read-only commands (validate,
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
from enum import StrEnum
from pathlib import Path

from .. import runs
from ..adapters.multiplexer import MultiplexerError, get_multiplexer, mux_usable
from ..journal import Journal
from ..platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_write_text,
    atomic_write_text_at,
    open_dir_confined,
)

CTL_SESSION = "bmad-loop-ctl"

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
    no-op). With no record at all the answer is the first match, exactly as
    before.

    Scoped to `project` by the PROJECT_OPTION tag, on the same rule as
    _ctl_window_candidates: the control session is shared across projects, and a
    run id is only unique within one (`--run-id` is caller-supplied), so a
    same-id window belonging to another project would otherwise be a legal match
    here — for `x` that means killing a *live* orchestrator next door. An
    untagged window is admitted when this project has the run dir, which keeps a
    window whose (best-effort) tag write failed reachable by its own project
    rather than by nobody.

    Untagged is a *fallback*, not a peer: an untagged window proves nothing
    about who owns it, so it is consulted only when nothing carries this
    project's tag. Merged into one listing-ordered list they would compete on
    index, and a neighbouring project's untagged window listed first would beat
    this project's correctly tagged one — for `x`, killing next door's
    orchestrator. That case is not hypothetical: the record cannot break the tie
    for a fresh `run`, where recording is deliberately skipped."""
    if not mux_available():
        return None
    mine = runs.accepted_tags(project)
    local = runs.is_run(runs.run_dir_for(project, run_id))
    tagged: list[str] = []
    untagged: list[str] = []
    rows = get_multiplexer().list_windows(
        CTL_SESSION, ["window_id", "window_name", runs.PROJECT_OPTION]
    )
    for win_id, name, tag in rows:
        # win_id can be "": psmux's qualifier passes a falsy id through. An
        # empty id must never become a target — an empty `-t` resolves against
        # the *current* window. (The base's short-row padding CAN produce an
        # empty *tag* — it fills trailing fields — which is exactly the untagged
        # case below; window_id stays field 0 of 3.)
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
        if tag in mine:
            tagged.append(win_id)
        elif not tag and local:
            # untagged, and this project holds the run dir — ownership is
            # plausible but unproven, so it only counts if nothing is tagged
            untagged.append(win_id)
    matches = tagged or untagged
    if not matches:
        return None
    # Membership in `matches`, not mere presence in the listing: it re-proves the
    # name and the project too, so neither a backend that reuses a freed window
    # id nor a record naming a neighbouring project's window can be replayed.
    recorded = _read_ctl_window(project, run_id)
    return recorded if recorded in matches else matches[0]


def ctl_window_recorded(project: Path, run_id: str, win_id: str) -> bool:
    """Whether `ctl_window_id` now answers `win_id` for this run — i.e. whether
    the launch's disambiguation actually took.

    False means the launch itself succeeded but the lookup is back on the
    ambiguous first-match scan, which is exactly #482's symptom and so is
    operator-visible: every launcher that mints a second window under a run id
    should report it rather than let an unqualified success toast imply the
    targeting is sound. Split out of resume_detached's return so the resolve
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


def ctl_target() -> str:
    """Seam-canonical target token for the control session; see
    :meth:`TerminalMultiplexer.target`. Windows are targeted by stable id
    (ctl_window_id), never by name through this token."""
    return get_multiplexer().target(CTL_SESSION)


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
    multiplexer, so no direct TMUX/HERDR_* env sniffing happens here."""
    return current_session() == CTL_SESSION


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
    still someone at this terminal" are independent, and the two failures point
    opposite ways. A failed *switch* leaves the client sitting in this very
    window; a failed *detach* reports no verified hand-back, which is not the
    same claim and does not license the same response."""

    RETURNED = "returned"
    #: No hand-back, but a human may still be here: nothing was recorded to
    #: return to (a plain foreground sweep), the backend is unusable, or the
    #: switch failed — usually with the client still in this window, though a
    #: backend may also answer False for a switch it could not vouch for (a
    #: timed-out verb, an unreadable client count) where the client may in fact
    #: have left. The conservative answer either way — a caller must keep
    #: talking to the terminal.
    ATTENDED = "attended"
    #: A hand-back was attempted and did not verifiably happen: the detach found
    #: nothing attached, the effect could not be observed, or the backend has no
    #: detach verb at all (herdr). A caller must not rely on anyone answering a
    #: prompt in this window — a policy for the uncertainty, not a proof that
    #: the window is empty (see return_attached_client for why it is the safe
    #: way to be wrong).
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
    ReturnOutcome and not a bool. A failed switch is normally evidence that the
    client is still in this window, so ATTENDED keeps the caller prompting —
    though not proof: a backend may answer False for a switch it could not
    vouch for (psmux's timed-out verb or unreadable client count), and the
    return option surviving a False is what keeps the parked trailer's retry
    available for exactly that case. A
    failed detach carries no such evidence in general: on tmux it does
    (`detach-client` fails with "no current client"), but off tmux False also
    covers an effect the backend could not observe and a detach verb it does
    not have at all — herdr, whose False rather than None is exactly what the
    seam's widened return type buys. UNREACHABLE is the policy for all three,
    because the two ways of being wrong are not equally bad: prompting into a
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


def attach_plan(project: Path, run_id: str) -> tuple[list[str], str | None] | None:
    """Pick where an interactive attach should land for this run and which window
    (if any) to record a return target on. Shared by the CLI `attach` command and
    mirroring the TUI's action_attach logic: prefer the orchestrator's ctl window
    when a sweep is blocked on a decision or no agent session is live, else the
    live agent session. Returns (tmux argv, return_window) or None when there is
    nothing to attach to."""
    session = runs.session_name(run_id)
    win_id = ctl_window_id(project, run_id)
    agent_live = session_exists(session)
    if win_id is not None and (
        decision_pending(runs.run_dir_for(project, run_id)) or not agent_live
    ):
        select_ctl_window_id(win_id)
        return runs.attach_target_argv(ctl_target()), win_id
    if agent_live:
        return runs.attach_target_argv(runs.session_target(run_id)), None
    return None


def kill_ctl_window(project: Path, run_id: str) -> None:
    """Kill the control-session window hosting this run's orchestrator process,
    if any. A no-op when the run was not launched from the TUI or tmux is gone."""
    win_id = ctl_window_id(project, run_id)
    if win_id is not None:
        get_multiplexer().kill_window(win_id)


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
    """
    mux = get_multiplexer()
    if not mux_usable(mux) or not session_exists(CTL_SESSION):
        return []
    current = mux.current_window_id()
    rows = mux.list_windows(CTL_SESSION, ["window_id", "window_name", runs.PROJECT_OPTION])
    mine = runs.accepted_tags(project)
    candidates: list[tuple[str, str]] = []
    for win_id, name, tag in rows:
        if not win_id or win_id == current:
            continue
        m = _CTL_WINDOW_RE.match(name)
        if m is None:
            continue  # not a run window (e.g. the session's initial shell)
        if not runs.is_valid_run_id(m.group(1)):
            continue  # a foreign/mangled window name must not steer a run-dir path
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

    Two ceilings, both deliberate:

    - The membership test pairs list_windows' `window_id` column with
      list_window_ids. The seam states its symmetry rules pairwise and this pair
      is stated because of THIS caller — a backend qualifying one side and not
      the other reads every candidate as removed, which is #435 restored on the
      optimistic side, with no error anywhere.
    - `[]` is read as "the session went with its last window". The seam's `[]`
      is wider than that: BaseTmuxBackend folds EVERY nonzero exit to `[]`, so a
      server that errors while its windows live would report them removed. The
      common cause by far is the session really being gone, and pessimism there
      would invent a phantom survivor on every future sweep. Narrowing the
      sentinel is a change to the engine's liveness probe, not to this function.
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
        live = set(mux.list_window_ids(CTL_SESSION))
    except MultiplexerError:
        # The kills may well have landed; nothing here can say so. Claiming the
        # optimistic half is exactly the bug — the next cleanup pass retries.
        return [], [], [name for _win_id, name in candidates]
    removed = [name for win_id, name in candidates if win_id not in live]
    survived = [name for win_id, name in candidates if win_id in live]
    return removed, survived, []


def _ensure_ctl_session(project: Path) -> None:
    mux = get_multiplexer()
    # has_session is raiser-side (a server-backed backend can fail the probe after
    # the availability pre-gate). Keep it inside the try so a transport failure
    # converts to LaunchError, which the TUI launch/resume/resolve handlers already
    # catch — otherwise the raw MultiplexerError slips past them and crashes the app.
    try:
        if mux.has_session(CTL_SESSION):
            return
        mux.new_session(CTL_SESSION, project)
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer ctl-session setup failed: {e}") from e


def cli_argv(*tail: str) -> list[str]:
    """`sys.executable -m bmad_loop.cli ...` — immune to PATH/venv drift
    inside tmux windows."""
    return [sys.executable, "-m", "bmad_loop.cli", *tail]


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
    """
    mux = get_multiplexer()
    if not mux_usable(mux):
        raise LaunchError(
            "multiplexer backend unavailable (binary missing, version unsupported, "
            "or a required helper absent)"
        )
    _ensure_ctl_session(project)
    try:
        win_id = (
            mux.new_parked_window(
                CTL_SESSION,
                f"{kind}-{run_id}",
                project,
                cli_argv(*argv_tail),
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


def start_run_detached(
    project: Path,
    run_id: str,
    *,
    spec: str | None = None,
    epic: int | None = None,
    story: str | None = None,
    max_stories: int | None = None,
) -> None:
    tail = ["run", "--project", str(project), "--run-id", run_id]
    if spec:
        tail += ["--spec", spec]  # forces stories mode (folder+id dispatch)
    if epic is not None:
        tail += ["--epic", str(epic)]
    if story:
        tail += ["--story", story]
    if max_stories is not None:
        tail += ["--max-stories", str(max_stories)]
    start_detached(project, tail, run_id, "run")


def start_sweep_detached(
    project: Path,
    run_id: str,
    *,
    no_prompt: bool = False,
    decisions_only: bool = False,
    max_bundles: int | None = None,
) -> None:
    tail = ["sweep", "--project", str(project), "--run-id", run_id]
    if no_prompt:
        tail.append("--no-prompt")
    if decisions_only:
        tail.append("--decisions-only")
    if max_bundles is not None:
        tail += ["--max-bundles", str(max_bundles)]
    start_detached(project, tail, run_id, "sweep")


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
    if win_id and not ctl_window_recorded(project, run_id, win_id):
        return None
    return win_id


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
