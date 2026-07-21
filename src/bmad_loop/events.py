"""Canonical BMAD relay event: the schema plus its env-gated atomic write.

The orchestrator ingests each coding-CLI completion signal as one JSON file
under ``<run_dir>/events/`` (read back by :mod:`bmad_loop.signals`). This module
owns that event schema and the gated, atomic write. Two callers share the shape:

* the ``bmad-loop relay <Event>`` CLI command (:func:`bmad_loop.cli.cmd_relay`) —
  the in-tree, general counterpart to ``data/bmad_loop_hook.py`` for
  global/user-scoped-hook adapters. Such an adapter points its native hook at
  ``bmad-loop relay <Event>`` and needs **zero** per-adapter relay code.
* conceptually, ``data/bmad_loop_hook.py``. That script CANNOT import this module:
  it is installed into a target project as a lone, stdlib-only file
  (``.bmad-loop/bmad_loop_hook.py``) and run as a bare subprocess by the coding
  CLI, where the ``bmad_loop`` package need not be importable. It therefore keeps
  a byte-for-byte inlined copy of :func:`build_event` / :func:`_first_workspace`;
  ``tests/test_relay_event.py`` pins the two implementations to the same schema so
  they cannot drift.

The write is gated on ``BMAD_LOOP_RUN_DIR`` + ``BMAD_LOOP_TASK_ID`` (set on the
tmux window the orchestrator spawns), so a normal interactive session — where a
user runs the same hook — is a silent no-op.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _first_workspace(payload: Mapping[str, Any]) -> str | None:
    """agy carries no ``cwd`` — it sends ``workspacePaths``, a list of roots."""
    paths = payload.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


def build_event(
    event_name: str, payload: Mapping[str, Any], *, ts: int, task_id: str
) -> dict[str, Any]:
    """The canonical relay-event dict.

    ``event_name`` is always the CANONICAL event name — every CLI's hook config
    maps its native event onto one of :data:`bmad_loop.adapters.profile.CANONICAL_EVENTS`
    before the relay ever sees it. Payload key casing varies by CLI: snake_case
    (claude/codex), ``conversation_id`` (cursor), or camelCase (copilot's
    ``sessionId``/``transcriptPath``, agy's ``conversationId``/``transcriptPath`` —
    protojson encoding); each field extraction tries every spelling.
    """
    return {
        "ts": ts,
        "event": event_name,
        "task_id": task_id,
        "session_id": (
            payload.get("session_id")
            or payload.get("conversation_id")
            or payload.get("sessionId")
            or payload.get("conversationId")
        ),
        "transcript_path": payload.get("transcript_path") or payload.get("transcriptPath"),
        "cwd": payload.get("cwd") or _first_workspace(payload),
    }


def write_relay_event(
    event_name: str,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
) -> Path | None:
    """Atomically write one canonical event — only from an active BMAD session.

    Returns the written file's path, or ``None`` when ``environ`` is not an active
    bmad-loop session (``BMAD_LOOP_RUN_DIR`` / ``BMAD_LOOP_TASK_ID`` unset), so a
    normal interactive session running the same hook is unaffected.
    """
    run_dir = environ.get("BMAD_LOOP_RUN_DIR")
    task_id = environ.get("BMAD_LOOP_TASK_ID")
    if not run_dir or not task_id:
        return None

    ts = time.time_ns()
    event = build_event(event_name, payload, ts=ts, task_id=task_id)
    events_dir = Path(run_dir) / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    final = events_dir / f"{ts}-{task_id}-{event_name}.json"
    tmp = Path(f"{final}.tmp")
    tmp.write_text(json.dumps(event), encoding="utf-8")
    os.replace(tmp, final)
    return final
