"""Hermes user-config hooks and the package-owned BMAD event relay."""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HERMES_RELAY_COMMAND = "bmad-loop relay Stop"
HERMES_RELAY_HOOK = {"command": HERMES_RELAY_COMMAND, "timeout": 10}


def hermes_config_path(environ: Mapping[str, str]) -> Path:
    """Return the active Hermes config without reading credentials or config content."""
    home = environ.get("HERMES_HOME", "~/.hermes")
    return Path(home).expanduser() / "config.yaml"


def _copy_hook_entries(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    updated = copy.deepcopy(config)
    hooks = updated.get("hooks")
    if hooks is None:
        hooks = {}
        updated["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("Hermes config hooks must be a mapping")
    entries = hooks.get("post_llm_call")
    if entries is None:
        entries = []
        hooks["post_llm_call"] = entries
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("Hermes hooks.post_llm_call must be a list of mappings")
    return updated, entries


def merge_hermes_stop_hook(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Add the managed relay once while preserving all unrelated Hermes hooks."""
    updated, entries = _copy_hook_entries(config)
    if any(entry.get("command") == HERMES_RELAY_COMMAND for entry in entries):
        return updated, False
    entries.append(copy.deepcopy(HERMES_RELAY_HOOK))
    return updated, True


def remove_hermes_stop_hook(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Remove only the managed relay, pruning empty maps left by that removal."""
    updated = copy.deepcopy(config)
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated, False
    entries = hooks.get("post_llm_call")
    if not isinstance(entries, list):
        return updated, False
    kept = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("command") == HERMES_RELAY_COMMAND)]
    if len(kept) == len(entries):
        return updated, False
    if kept:
        hooks["post_llm_call"] = kept
    else:
        hooks.pop("post_llm_call", None)
    if not hooks:
        updated.pop("hooks", None)
    return updated, True


def _first_workspace(payload: Mapping[str, Any]) -> str | None:
    paths = payload.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


def relay_event(event_name: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> bool:
    """Atomically write a canonical BMAD event only from an active BMAD session."""
    run_dir = environ.get("BMAD_LOOP_RUN_DIR")
    task_id = environ.get("BMAD_LOOP_TASK_ID")
    if not run_dir or not task_id:
        return False

    ts = time.time_ns()
    event = {
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
    events_dir = Path(run_dir) / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    final = events_dir / f"{ts}-{task_id}-{event_name}.json"
    temporary = final.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(event), encoding="utf-8")
    os.replace(temporary, final)
    return True
