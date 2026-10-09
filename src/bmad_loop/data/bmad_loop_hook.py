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

Every event also carries a `lineage` tag (DW-507): "match" when the CLI bmad-loop
launched (the tmux launch records its pid, or its forking shell's, in
$BMAD_LOOP_LAUNCH_PID) fired the hook itself, "mismatch" when something else
in between did — a nested coding CLI started from inside the session inherits
this whole environment, so its hooks reach here too — and "unknown" when that
cannot be read (no /proc, an older orchestrator that sets no launch pid). It
only tags: the event is written either way, and the orchestrator decides what
a mismatch means.
"""

import json
import os
import re
import stat
import sys
import time

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


def _notification_type(payload):
    # A Notification payload's subtype (DW-348), snake_case (claude) or camelCase.
    # Only a string is forwarded: anything else would reach the orchestrator as a
    # value no profile table can key on.
    value = payload.get("notification_type") or payload.get("notificationType")
    return value if isinstance(value, str) else None


def _source(payload):
    # A SessionStart payload's `source` (#767): claude/gemini send
    # startup|resume|clear|compact; codex/copilot send their own values. Only a
    # string is forwarded, like the notification subtype above.
    value = payload.get("source")
    return value if isinstance(value, str) else None


# How long after the launched CLI the rest of its launch chain may start
# (DW-507): a forking default-shell starts the CLI and a node shim starts the
# real binary at once. Limitation: anything the CLI itself starts at launch (an
# MCP server, a project SessionStart hook running another CLI) also falls inside
# the window and reads "match", so only the #767 rules apply to it; only what an
# agent starts after a model turn is reliably outside it.
_LAUNCH_SHIM_WINDOW_S = 5


def _proc_stat(pid):
    """`(ppid, starttime in clock ticks)` from `/proc/<pid>/stat`, or None when
    the process is gone or there is no `/proc` (Windows, macOS).

    The comm field is parenthesized and may itself hold spaces or ")", so the
    fields are split after the LAST ")": state is index 0 there, ppid index 1,
    and starttime index 19 (fields 4 and 22 of proc(5))."""
    path = f"/proc/{pid}/stat"  # portability: Linux-only; absent elsewhere -> None
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    fields = data[data.rfind(b")") + 1 :].split()
    try:
        return int(fields[1]), int(fields[19])
    except (IndexError, ValueError):
        return None


def _cmdline(pid):
    # The process's argv; [] when it cannot be read.
    path = f"/proc/{pid}/cmdline"  # portability: Linux-only; absent elsewhere -> []
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return []
    if not data:
        return []
    return [arg.decode("utf-8", "replace") for arg in data.rstrip(b"\0").split(b"\0")]


# Shells a hook host runs a registered hook command under (`<shell> -c <command>`),
# by basename; a login shell's argv[0] carries a leading "-".
_HOOK_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "fish"})


def _runs_relay(words, event_name, invocation):
    # Whether `words` hold `invocation` then `event_name` as consecutive words:
    # its program by basename (`/opt/bin/bmad-loop`), the rest literally.
    program, rest = invocation[0], [*invocation[1:], event_name]
    for index, word in enumerate(words):
        if word.rsplit("/", 1)[-1] == program and words[index + 1 : index + 1 + len(rest)] == rest:
            return True
    return False


def _is_hook_wrapper(argv, event_name, invocation):
    """Whether a process between the relay and the launched CLI only runs the
    registered hook command (DW-507), read structurally from its argv:
      - a process whose argv ENDS in the relay invocation and the event name
        (`uv run --no-project python .../bmad_loop_hook.py Stop`), or
      - a shell (`_HOOK_SHELLS`) whose `-c` command string holds them as
        consecutive words, split on whitespace, quotes and shell operators (so
        `sh -c '.../bmad-loop relay Stop && true'` counts).
    Nothing else in the argv is read: a nested CLI whose prompt, or whose
    launching shell's command, merely names the relay is not a wrapper — the
    nested CLI itself is never a shell running a `-c` string."""
    tail = argv[-len(invocation) - 1 :]
    if len(tail) == len(invocation) + 1 and _runs_relay(tail, event_name, invocation):
        return True
    if not argv or argv[0].rsplit("/", 1)[-1].lstrip("-") not in _HOOK_SHELLS:
        return False
    args = iter(argv[1:])
    for arg in args:
        if not arg.startswith(("-", "+")) or arg == "--":
            return False  # an operand before -c: a script file, not a command string
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            break
    else:
        return False
    for arg in args:
        if not arg.startswith(("-", "+")):
            words = [word for word in re.split(r"[\s;&|()<>'\"`]+", arg) if word]
            return _runs_relay(words, event_name, invocation)
    return False


def _lineage(event_name, invocation):
    """Whether the process that fired this hook is the CLI bmad-loop launched
    (DW-507): "match", "mismatch", or "unknown".

    A nested coding CLI started from inside the session inherits the relay
    environment, so its hooks land in the parent's event stream. The tmux
    launch records the launched pid in $BMAD_LOOP_LAUNCH_PID; this walks the
    relay's own parent chain toward it. Being a descendant is not enough — a
    CLI started from the launched CLI's Bash tool is one — so the walk skips
    only what a hook invocation legitimately puts in between:
      - the launch chain: any process started within _LAUNCH_SHIM_WINDOW_S of
        the launched pid. A forking default-shell (fish, dash) is the launched pid
        and the CLI its child; a node shim's real binary is a child again.
        Whatever the CLI starts at launch (MCP servers, a SessionStart hook
        running another CLI) is inside the window too and reads "match".
      - a hook-command wrapper running this relay's `invocation` (its program's
        basename, then its literal words) for this event: see
        `_is_hook_wrapper`.
    Any other process on the way is a nested CLI or its shell: "mismatch". So
    is reaching pid 1 or below without meeting the launched pid while it is
    alive: a process from another tree.

    "unknown" whenever the answer cannot be read: the variable unset or not a
    pid, no `/proc` (Windows, macOS), the launched process gone, a process
    vanishing mid-walk, more than 64 hops, or any unexpected fault. The tag
    only informs the orchestrator; it never stops the event from being written.
    """
    try:
        try:
            launch_pid = int(os.environ.get("BMAD_LOOP_LAUNCH_PID") or "")
        except ValueError:
            return "unknown"
        if launch_pid <= 1:
            return "unknown"
        launch = _proc_stat(launch_pid)
        if launch is None:
            return "unknown"
        try:
            ticks = getattr(os, "sysconf")("SC_CLK_TCK")  # POSIX-only
        except (AttributeError, OSError, ValueError):
            return "unknown"
        if ticks <= 0:
            return "unknown"
        chain_window = _LAUNCH_SHIM_WINDOW_S * ticks
        pid = os.getppid()
        for _hop in range(64):
            if pid == launch_pid:
                return "match"
            if pid <= 1:
                return "mismatch"
            current = _proc_stat(pid)
            if current is None:
                return "unknown"
            parent, started = current
            if 0 <= started - launch[1] <= chain_window:
                pid = parent
                continue
            if _is_hook_wrapper(_cmdline(pid), event_name, invocation):
                pid = parent
                continue
            return "mismatch"
        return "unknown"
    except Exception:
        return "unknown"


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
        # A Notification payload's subtype (DW-348): claude sends
        # `notification_type` (e.g. "permission_prompt"); the profile maps it onto
        # a parked kind. Kept only when it is a string; absent everywhere else.
        "notification_type": _notification_type(payload),
        # Why a SessionStart fired (#767): a "clear"/"compact" start with a new id
        # is still the launched session, so attribution rebinds instead of
        # reading it as a nested CLI. Kept only when it is a string.
        "source": _source(payload),
        # Whether the launched CLI itself fired this hook (DW-507): a tag, never
        # a filter — the orchestrator decides what a "mismatch" means.
        "lineage": _lineage(event_name, ("bmad_loop_hook.py",)),
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
