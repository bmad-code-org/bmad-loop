#!/usr/bin/env python3
"""Full-payload capture hook for `bmad-loop probe-adapter --probe`. Stdlib only.

A throwaway sibling of bmad_loop_hook.py used ONLY during an opt-in live probe.
It no-ops (exit 0) unless BMAD_LOOP_PROBE_CAPTURE_DIR is set — a DISTINCT env var
from the real relay's BMAD_LOOP_RUN_DIR, so the capture hook and the signal relay
can never fire in each other's context (a normal interactive session sees neither).

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
  <ts>-<event>.stdin.raw     the stdin bytes verbatim, kept so a payload that
                             does not parse (empty, truncated, undecodable) can
                             still be read with --keep-temp. Never displayed.

Stdin is read as bytes and parsed with ``json.loads``, which detects
UTF-8/16/32 itself, exactly as the relay's ``_read_payload`` does: a text-mode
read would decode with the locale code page (cp1252 on Windows) and garble or
reject a non-ASCII path.

Tolerant of empty/garbage stdin and of write errors — it must never crash the
CLI window it is hooked into.
"""

import json
import os
import sys
import time


def _first_workspace(payload):
    paths = payload.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


def _atomic_write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _read_stdin() -> bytes:
    """The raw stdin bytes; a text-only stream (no ``buffer``) is re-encoded as
    UTF-8, and an absent or unreadable stdin reads as empty."""
    stdin = sys.stdin
    buffer = getattr(stdin, "buffer", None)
    try:
        if buffer is not None:
            return buffer.read()
        if stdin is not None:
            return stdin.read().encode("utf-8", "surrogateescape")
    except (OSError, ValueError):
        pass
    return b""


def main() -> int:
    capture_dir = os.environ.get("BMAD_LOOP_PROBE_CAPTURE_DIR")
    if not capture_dir:
        return 0
    task_id = os.environ.get("BMAD_LOOP_TASK_ID", "probe")
    event_name = sys.argv[1] if len(sys.argv) > 1 else "Unknown"
    raw = _read_stdin()
    try:
        payload = json.loads(raw)
    # UnicodeDecodeError and JSONDecodeError are both ValueError subclasses.
    except (ValueError, RecursionError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    ts = time.time_ns()
    try:
        os.makedirs(capture_dir, exist_ok=True)
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
        _atomic_write(os.path.join(capture_dir, f"{ts}-{event_name}.signal.json"), signal)
        captured = dict(payload)
        captured["argv_event"] = event_name
        _atomic_write(os.path.join(capture_dir, f"{ts}-{event_name}.payload.json"), captured)
        _atomic_write_bytes(os.path.join(capture_dir, f"{ts}-{event_name}.stdin.raw"), raw)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
