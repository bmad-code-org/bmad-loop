#!/usr/bin/env python3
"""Full-payload capture hook for `bmad-loop probe-adapter --probe`. Stdlib only.

A throwaway sibling of bmad_loop_hook.py used ONLY during an opt-in live probe.
It no-ops (exit 0) unless BMAD_LOOP_PROBE_CAPTURE_DIR is set — a DISTINCT env
var from the real relay's BMAD_LOOP_RUN_DIR, so the capture hook and the signal
relay can never fire in each other's context (a normal interactive session sees
neither).

For every event it writes two files atomically into the capture dir:

  <ts>-<event>.signal.json   SignalWatcher-shaped {ts,event,task_id,session_id,
                             transcript_path,cwd} so the probe's completion poll
                             (a plain SignalWatcher over the capture dir) works
                             with no change to the watcher.
  <ts>-<event>.payload.json  the ENTIRE raw stdin payload plus an injected
                             "argv_event" (the native event name from argv, for
                             native->canonical pairing) so a maintainer can read
                             the CLI's exact field names and casing. The probe
                             command sanitizes this before it is ever shown;
                             nothing written here is displayed raw.

Tolerant of empty/garbage stdin and of write errors — it must never crash the
CLI window it is hooked into.

The write path is a deliberate twin of bmad_loop_hook.py's _write_event: this
script is stdlib-only package data (no import of bmad_loop_hook or
bmad_loop.events is possible), so the symlink/junction refusal, dir_fd-anchored
create+rename, 0o600 mode, and short-write-safe loop are duplicated here rather
than shared. This capture dir holds the FULL raw CLI payload (more sensitive
than the production relay's trimmed event), so the same hardening applies.
"""

import json
import os
import stat
import sys
import time

# Windows reparse tags that make a directory entry REDIRECT somewhere else,
# compared against os.lstat().st_reparse_tag (Windows, 3.8+). Deliberately not
# os.path.isjunction(), which is 3.12+ — this hook runs under whatever
# interpreter the host has, not under the orchestrator's. Deliberately not "any
# reparse tag" either: cloud placeholders (OneDrive) and dedup stubs are reparse
# points too, and refusing those would stall a legitimate probe. Empty on POSIX.
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
    truncated capture file is not merely retried, it is lost — and the raw fd
    needed for O_NOFOLLOW/dir_fd cannot use the buffered `open()` that used to
    loop internally, so loop here instead.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # not observed in practice; a spinning hook is worse
            raise OSError("short write to the capture file")
        view = view[written:]


def _atomic_write(capture_dir, name, obj) -> None:
    """Write one capture file into `capture_dir`, refusing to follow a redirect.

    Mirrors bmad_loop_hook.py's _write_event: the capture dir sits in a
    session-writable location, so a driven session could plant it as a symlink
    (or, on Windows, a junction) and redirect or swallow the capture — this
    refuses that before ever touching the redirected target, and anchors the
    create+rename to a dir_fd opened O_NOFOLLOW where the platform has one.
    Windows has neither O_NOFOLLOW/O_DIRECTORY nor dir_fd support, so its
    fallback re-resolves `capture_dir` by path and re-checks for a redirect
    after the payload is written and before it is published.

    Mode is 0o600 (narrowed from the umask-derived mode a plain `open()`
    produces): the probe's capture dir holds the full raw CLI payload.

    Raises OSError on any refusal or failure; the caller degrades to a no-op.
    """
    if _is_link_like(capture_dir):
        raise OSError(f"refusing to write capture files into a redirected directory: {capture_dir}")
    os.makedirs(capture_dir, exist_ok=True)
    data = json.dumps(obj).encode("utf-8")
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
        dir_fd = os.open(capture_dir, os.O_RDONLY | o_directory | o_nofollow)
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
    # capture_dir by path. A swap into a junction between the check above and
    # this create would have put the temp file inside the attacker's directory.
    # Check again before publishing, so a swap that is still in place is refused
    # rather than followed.
    tmp_path = os.path.join(capture_dir, tmp)
    fd = os.open(tmp_path, create, 0o600)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    if _is_link_like(capture_dir):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise OSError(f"capture directory was redirected mid-write: {capture_dir}")
    os.replace(tmp_path, os.path.join(capture_dir, name))


def main() -> int:
    capture_dir = os.environ.get("BMAD_LOOP_PROBE_CAPTURE_DIR")
    if not capture_dir:
        return 0
    task_id = os.environ.get("BMAD_LOOP_TASK_ID", "probe")
    event_name = sys.argv[1] if len(sys.argv) > 1 else "Unknown"
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    ts = time.time_ns()
    try:
        signal = {
            "ts": ts,
            "event": event_name,
            "task_id": task_id,
            # Casing varies by CLI, exactly as in bmad_loop_hook.py: snake_case
            # (claude/codex), conversation_id (cursor), camelCase (copilot,
            # agy). agy sends workspacePaths rather than a cwd.
            "session_id": (
                payload.get("session_id")
                or payload.get("conversation_id")
                or payload.get("sessionId")
                or payload.get("conversationId")
            ),
            "transcript_path": payload.get("transcript_path") or payload.get("transcriptPath"),
            "cwd": payload.get("cwd") or _first_workspace(payload),
        }
        _atomic_write(capture_dir, f"{ts}-{event_name}.signal.json", signal)
        captured = dict(payload)
        captured["argv_event"] = event_name
        _atomic_write(capture_dir, f"{ts}-{event_name}.payload.json", captured)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
