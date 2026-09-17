#!/usr/bin/env python3
"""Coding-CLI hook relay for bmad-loop. Stdlib only.

Each CLI's hook config registers this script under its native event names
(Claude/Codex: SessionStart/Stop/..., Gemini: AfterAgent for Stop, Copilot:
agentStop for Stop) but always passes the CANONICAL event name as argv[1] — the
orchestrator only ever sees canonical events. Payload keys vary too: snake_case
(claude/codex), conversation_id (cursor), or camelCase (copilot's sessionId/
transcriptPath, agy's conversationId/transcriptPath — protojson encoding); the
field extraction below tries each. agy alone carries no cwd, sending the
workspacePaths list instead. Reads the hook payload
from stdin and writes one event file
into the orchestrator's events directory. No-ops (exit 0) unless the session was
spawned by bmad-loop (detected via env vars set on the tmux window), so
normal interactive sessions are unaffected.

Where that directory is: $BMAD_LOOP_EVENTS_DIR when the orchestrator names one,
else the legacy $BMAD_LOOP_RUN_DIR/events. The channel moved out of the project
tree in #494 so a branch switch or a worktree mount cannot take a live run's
control plane away, and this script is COPIED into the target project at init
time — so an upgraded orchestrator regularly drives sessions whose installed
copy is this file's older self, which knows only the legacy path. That pairing
is what the orchestrator's own dual poll (signals.SignalWatcher) covers.

$BMAD_LOOP_EVENTS_DIR is deliberately NOT part of the no-op detector above: the
mirror-image skew — an older orchestrator that sets only RUN_DIR/TASK_ID driving
a session whose installed relay is this newer file — must still write its
events, and requiring the new variable would silently stall every one of those
runs instead.
"""

import json
import os
import stat
import sys
import time

# The whitelist adapters/profile.py already enforces at hook-registration time
# (SessionStart/Stop/SessionEnd/PreCompact), duplicated here because this file
# is stdlib-only and cannot import bmad_loop.adapters.profile -- same twin
# constraint as events.py below. Not a gate: an event_name outside this set
# still gets written exactly as before, only with an added drift warning,
# since a hand-edited hook config or a profile that skipped that validation is
# the only way one reaches this script at all.
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


def main() -> int:
    run_dir = os.environ.get("BMAD_LOOP_RUN_DIR")
    task_id = os.environ.get("BMAD_LOOP_TASK_ID")
    if not run_dir or not task_id:
        return 0
    event_name = sys.argv[1] if len(sys.argv) > 1 else "Unknown"
    if event_name not in CANONICAL_EVENTS:
        # Drift, not a gate: profile.py already enforces this whitelist before a
        # hook is ever registered, so reaching here means a hand-edited hook
        # config or a profile that skipped that validation. The event is still
        # written exactly as below -- never withhold the signal the orchestrator
        # waits on -- same never-fail treatment as the hooks.relay-stale check.
        print(
            f"bmad_loop_hook: event_name {event_name!r} is outside the canonical set {sorted(CANONICAL_EVENTS)}",
            file=sys.stderr,
        )
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    ts = time.time_ns()
    event = {
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
    # The orchestrator's own events dir when it named one, else the legacy
    # in-tree location this file's older selves are still installed at (see the
    # module docstring). `or`, not a presence test: an exported-but-empty value
    # names the launch cwd, which is not a control plane.
    events_dir = os.environ.get("BMAD_LOOP_EVENTS_DIR") or os.path.join(run_dir, "events")
    try:
        _write_event(events_dir, f"{ts}-{task_id}-{event_name}.json", event)
    except OSError:
        # A hostile or broken events dir must degrade to the orchestrator's
        # normal session_timeout_min path, never surface as a hook failure that
        # fails the CLI window (mirrors bmad_loop_probe_hook.py's write wrap).
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
