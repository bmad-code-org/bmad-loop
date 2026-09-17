"""Importable twin of the hardened event write in ``data/bmad_loop_hook.py``.

The hook script is COPIED into every target project and runs inside the coding
CLI's process under whatever interpreter the host has, so it is stdlib-only by
contract (its docstring says so) and cannot import ``bmad_loop`` to reach this
module — and this module cannot import it back, since it ships as package DATA
rather than as an importable module. Hence a twin rather than shared code:
``_LINK_REPARSE_TAGS``, ``_first_workspace``, ``_is_link_like``, ``_write_all``
and ``_write_event`` below are byte-identical copies of the hook's, pinned that
way by ``tests/test_events.py::test_the_twinned_source_is_identical`` — which
AST-extracts both sides and compares the source segments, so a fix applied to one
writer of the events control plane and not the other cannot pass review silently.

Because they are byte-identical, their docstrings and comments are written from
the hook script's vantage point ("this relay runs under whatever interpreter the
host has") and stay that way on purpose: rewording either side to suit its own
file breaks parity, and diverging is exactly what two separately-hardened writers
of one control plane must not do. What the shared text says about the attack, the
platform branches, and the residual Windows windows holds for both.

The rest of the module is this side's own: the payload shaping the hook does
inline in its ``main()``, and :func:`relay`, which backs ``bmad-loop relay
<Event>`` — the #461 Phase 2 hook target, an installed console script instead of
a file path inside the workspace that a branch switch can take away.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import IO, Any

# The whitelist adapters/profile.py already enforces at hook-registration time
# (SessionStart/Stop/SessionEnd/PreCompact), duplicated here because this file
# is stdlib-only and cannot import bmad_loop.adapters.profile -- same twin
# constraint as the copied hook script above. Not a gate: an event_name outside
# this set still gets written exactly as before, only with an added drift
# warning, since a hand-edited hook config or a profile that skipped that
# validation is the only way one reaches this relay at all.
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact"}

# Windows reparse tags that make a directory entry REDIRECT somewhere else,
# compared against os.lstat().st_reparse_tag (Windows, 3.8+). Deliberately not
# os.path.isjunction(), which is 3.12+ — this relay runs under whatever
# interpreter the host has, not under the orchestrator's. Deliberately not "any
# reparse tag" either: cloud placeholders (OneDrive) and dedup stubs are reparse
# points too, and refusing those would stall a legitimate run. Empty on POSIX.
_LINK_REPARSE_TAGS = tuple(
    tag
    for tag in (
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", None),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
    )
    if tag is not None
)


def _first_workspace(payload):
    paths = payload.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


def _is_link_like(path):
    """True when `path` redirects elsewhere: a POSIX symlink, or a Windows
    symlink OR DIRECTORY JUNCTION.

    `os.path.islink()` is False for a junction — junctions are a distinct
    reparse kind, which is why `os.path.isjunction()` exists at all. On Windows
    the junction is the arm that matters: `mklink /J` needs no elevation, while
    a directory symlink needs SeCreateSymbolicLinkPrivilege or Developer Mode —
    so the unprivileged attack is exactly the one `islink()` misses.
    """
    if os.path.islink(path):
        return True
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) in _LINK_REPARSE_TAGS
    except OSError:
        return False


def _write_all(fd, data):
    """Write every byte of `data` to `fd`.

    `os.write()` may write FEWER bytes than asked and simply return the count. A
    truncated event file is not merely retried, it is lost: `SignalWatcher.poll`
    adds a filename to its consumed set BEFORE parsing it (signals.py), so
    malformed JSON is skipped and never re-read — the session's Stop signal is
    gone for good and the run waits out `session_timeout_min`. The buffered
    `open()` this replaced looped internally; the raw fd needed for
    O_NOFOLLOW/dir_fd does not, so loop here.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # not observed in practice; a spinning hook is worse
            raise OSError("short write to the event file")
        view = view[written:]


def _write_event(events_dir, name, event):
    """Write one event file into `events_dir`, refusing to follow a redirect.

    The events dir is the orchestrator's control plane. A driven session has
    write access to the project, so it could plant `<run_dir>/events` as a
    symlink (or, on Windows, a junction) and redirect — or swallow — the
    completion signal, stalling the run to `session_timeout_min` instead of
    completing. `os.makedirs(exist_ok=True)` `isdir()`-checks THROUGH such a
    link, so the refusal has to come before it. That refusal works on every
    platform.

    Where the platform has them, the create+replace is anchored to a dir_fd
    opened O_NOFOLLOW: every later operation goes through that fd, so a swap
    after the check cannot reach the write. Windows has neither
    O_NOFOLLOW/O_DIRECTORY nor a handle-relative open (`os.supports_dir_fd` is
    empty — dir_fd is implemented with the POSIX `*at` calls), so its fallback
    re-resolves the path and the check-to-write window stays open there. It is
    NARROWED, not closed: the redirect check runs again after the payload is
    written and before it is published, so a swap still in place is refused and
    the temp file removed. Two windows stay open on that path (#494), both
    measured: a swap-and-restore around the create is undetectable from stdlib
    Python, and a swap landing after the second check leaves the path-based
    publish unable to find the temp file it wrote — it either raises or renames
    a file the attacker planted inside the attacker's own directory. Neither
    redirects the payload, and both end where a refusal ends: no event, so the
    run waits out session_timeout_min. That is the same outcome an attacker gets
    for free by leaving a redirect in place, which is refused without any race —
    winning the race buys no capability, which is why the residual is accepted
    rather than chased into ctypes/NtCreateFile inside a stdlib-only relay.

    Mode is 0o600 (narrowed from the umask-derived mode an ordinary `open()`
    produced): only the operator running the loop reads these.

    Raises OSError on any refusal or failure; the caller degrades to a no-op.
    """
    if _is_link_like(events_dir):
        raise OSError(f"refusing to write events into a redirected directory: {events_dir}")
    os.makedirs(events_dir, exist_ok=True)
    data = json.dumps(event).encode("utf-8")
    tmp = name + ".tmp"
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    # O_BINARY is a no-op flag on POSIX; on Windows it stops the fd from
    # newline-translating what os.write() puts through it.
    create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | o_nofollow | getattr(os, "O_BINARY", 0)
    # Probe os.rename, not os.replace: CPython omits os.replace from
    # supports_dir_fd on Linux even though it accepts src_dir_fd/dst_dir_fd, so
    # probing it would leave this whole branch dead everywhere. This branch is
    # POSIX-only by construction, and there rename(2) IS the atomic-replace
    # primitive os.replace wraps — probe the function actually called.
    if o_nofollow and o_directory and {os.open, os.rename} <= os.supports_dir_fd:
        dir_fd = os.open(events_dir, os.O_RDONLY | o_directory | o_nofollow)
        try:
            fd = os.open(tmp, create, 0o600, dir_fd=dir_fd)
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        return
    # Fallback (Windows): no dir_fd to anchor to, so the create below re-resolves
    # events_dir by path. A swap into a junction between the check above and this
    # create would have put the temp file inside the attacker's directory. Check
    # again before publishing, so a swap that is still in place is refused rather
    # than followed — the realistic shape, since a junction has to persist to
    # capture the events the attacker is after.
    tmp_path = os.path.join(events_dir, tmp)
    fd = os.open(tmp_path, create, 0o600)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    if _is_link_like(events_dir):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise OSError(f"events directory was redirected mid-write: {events_dir}")
    os.replace(tmp_path, os.path.join(events_dir, name))


# --------------------------------------------------------------- this side only


def event_file_name(ts: int, task_id: str, event_name: str) -> str:
    """The event file's name. Sorted-by-time by construction (``ts`` first, fixed
    width in practice), and carrying the task id so ``SignalWatcher`` can attribute
    a file without opening it. Mirrors the hook's f-string exactly; the twin above
    stops at the write, so this and :func:`shape_event` are pinned behaviorally
    instead (``test_relay_and_hook_produce_the_same_event``)."""
    return f"{ts}-{task_id}-{event_name}.json"


def shape_event(ts: int, event_name: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The event record the orchestrator consumes, built from one hook payload."""
    return {
        "ts": ts,
        "event": event_name,
        "task_id": task_id,
        # Payload keys vary by CLI: snake_case (claude/codex), conversation_id
        # (cursor), or camelCase (copilot's sessionId/transcriptPath, agy's
        # conversationId). Try each.
        "session_id": (
            payload.get("session_id")
            or payload.get("conversation_id")
            or payload.get("sessionId")
            or payload.get("conversationId")
        ),
        "transcript_path": payload.get("transcript_path") or payload.get("transcriptPath"),
        # agy sends no cwd — it sends workspacePaths, a list of workspace roots.
        "cwd": payload.get("cwd") or _first_workspace(payload),
    }


def _read_payload(stdin: IO[str]) -> dict[str, Any]:
    """The hook payload, or an empty dict for anything unreadable.

    A hook that fires with nothing on stdin, half a JSON document, undecodable
    bytes, or a bare list is not an error the operator can act on — the event still
    has to be written, because the run's completion signal rides on it. Every
    non-dict outcome collapses to ``{}`` and the shaped event simply carries nulls.
    ``UnicodeDecodeError`` and ``json.JSONDecodeError`` are both ``ValueError``
    subclasses and ride the same arm; ``OSError`` covers a closed or unreadable
    descriptor.
    """
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def relay(event_name: str, stdin: IO[str]) -> int:
    """Write one event file for the session this process was spawned inside.

    The contract is the hook script's, because the hook config points at one or
    the other and the orchestrator must not be able to tell which ran: a silent
    no-op when the session was not spawned by bmad-loop (the env vars are the
    detector), garbage stdin tolerated, and any ``OSError`` from a hostile or
    broken events dir degrading to rc 0. Never anything on stdout — the CLI hosts
    parse hook stdout — and never a non-zero rc, which several of them surface as
    a failed tool call inside the very session whose completion this reports.

    Returns 0 unconditionally. Nothing here is worth failing a session over: the
    orchestrator's fallback for a missing event is ``session_timeout_min``, and an
    attacker who can suppress the event can already get that outcome by planting
    the redirect ``_write_event`` refuses.
    """
    run_dir = os.environ.get("BMAD_LOOP_RUN_DIR")
    task_id = os.environ.get("BMAD_LOOP_TASK_ID")
    if not run_dir or not task_id:
        return 0
    if event_name not in CANONICAL_EVENTS:
        # Drift, not a gate: profile.py already enforces this whitelist before a
        # hook is ever registered, so reaching here means a hand-edited hook
        # config or a profile that skipped that validation. The event is still
        # written exactly as below -- never withhold the signal the orchestrator
        # waits on -- same never-fail treatment as the hooks.relay-stale check.
        print(
            f"bmad_loop relay: event_name {event_name!r} is outside the canonical set {sorted(CANONICAL_EVENTS)}",
            file=sys.stderr,
        )
    ts = time.time_ns()
    event = shape_event(ts, event_name, task_id, _read_payload(stdin))
    # $BMAD_LOOP_EVENTS_DIR when the orchestrator names one (#494 moved the
    # channel out of the project tree), else the legacy in-tree location — the
    # same preference, spelled the same way, as the copied hook script's `main()`.
    # `or`, not a presence test: an exported-but-empty value names the launch cwd.
    # The no-op detector above stays RUN_DIR + TASK_ID for the reason the hook's
    # docstring gives: an older orchestrator sets neither the new variable nor any
    # expectation that this relay needs it, and its sessions must still complete.
    events_dir = os.environ.get("BMAD_LOOP_EVENTS_DIR") or os.path.join(run_dir, "events")
    try:
        _write_event(events_dir, event_file_name(ts, task_id, event_name), event)
    except OSError:
        # A hostile or broken events dir must degrade to the orchestrator's normal
        # session_timeout_min path, never surface as a hook failure that fails the
        # CLI window (mirrors the hook script's own write wrap).
        return 0
    return 0
