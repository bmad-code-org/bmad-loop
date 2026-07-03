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

Tolerant of empty/garbage stdin and of write errors — it must never crash the
CLI window it is hooked into.
"""

import json
import os
import sys
import time


def _atomic_write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


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
        os.makedirs(capture_dir, exist_ok=True)
        signal = {
            "ts": ts,
            "event": event_name,
            "task_id": task_id,
            "session_id": payload.get("session_id") or payload.get("conversation_id"),
            "transcript_path": payload.get("transcript_path"),
            "cwd": payload.get("cwd"),
        }
        _atomic_write(os.path.join(capture_dir, f"{ts}-{event_name}.signal.json"), signal)
        captured = dict(payload)
        captured["argv_event"] = event_name
        _atomic_write(os.path.join(capture_dir, f"{ts}-{event_name}.payload.json"), captured)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
