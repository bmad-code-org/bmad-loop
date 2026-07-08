"""Interactive escalation resolution.

When a run pauses on a CRITICAL escalation the agent that raised it is already
gone (its tmux window was killed on completion), so there is nothing to talk
to. This module instead launches a *fresh* interactive agent — the
`bmad-loop-resolve` skill — attached to the caller's terminal, seeded with the
escalation detail and the frozen spec. The human and the agent disambiguate the
spec; the agent writes a `resolution.json` marker. The caller (cli.cmd_resolve)
then re-arms the story (runs.rearm_escalation) and resumes the run.

The orchestrator never parses the conversation: the durable output is the
edited frozen spec on disk plus the resolution marker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .adapters.base import SessionSpec
from .model import RunState

RESOLVE_DIR = "resolve"


def _story_dir(run_dir: Path, story_key: str) -> Path:
    return run_dir / RESOLVE_DIR / story_key


def context_path(run_dir: Path, story_key: str) -> Path:
    return _story_dir(run_dir, story_key) / "context.json"


def resolution_path(run_dir: Path, story_key: str) -> Path:
    return _story_dir(run_dir, story_key) / "resolution.json"


def _gather_escalations(run_dir: Path, state: RunState, story_key: str) -> list[dict[str, Any]]:
    """The CRITICAL escalations recorded by this story's sessions, newest first.

    Reads each session's tasks/<task_id>/result.json (and escalation.json) — the
    same files the engine inspected when it decided to pause."""
    task = state.tasks.get(story_key)
    found: list[dict[str, Any]] = []
    if task is None:
        return found
    for session in reversed(task.sessions):
        task_dir = run_dir / "tasks" / session.task_id
        for fname in ("result.json", "escalation.json"):
            fpath = task_dir / fname
            if not fpath.is_file():
                continue
            try:
                doc = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for esc in doc.get("escalations", []) if isinstance(doc, dict) else []:
                if isinstance(esc, dict) and str(esc.get("severity", "")).upper() == "CRITICAL":
                    found.append(esc)
    return found


def build_context(state: RunState, run_dir: Path, story_key: str) -> Path:
    """Write resolve/<story_key>/context.json for the resolve skill to read."""
    task = state.tasks.get(story_key)
    context = {
        "story_key": story_key,
        "run_id": state.run_id,
        "spec_file": task.spec_file if task else None,
        "baseline_commit": task.baseline_commit if task else None,
        "paused_reason": state.paused_reason,
        "escalations": _gather_escalations(run_dir, state, story_key),
        # as_posix so the context contract is the same string on every OS (the
        # path is consumed by the agent, and Python/tools accept '/' on Windows).
        "resolution_path": resolution_path(run_dir, story_key).as_posix(),
    }
    # Stories mode: hand the resolver the manifest intent (the story entry) and a
    # sentinel indicator, so it sees WHAT the story is meant to do and WHETHER the
    # frozen spec even exists yet (a sentinel has no plan to edit — resolve the
    # underlying ambiguity instead). Sprint mode leaves the context unchanged.
    if state.source == "stories":
        stories_ctx = _stories_context(state, story_key)
        if stories_ctx:
            context["stories"] = stories_ctx
    path = context_path(run_dir, story_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    return path


def _stories_context(state: RunState, story_key: str) -> dict[str, Any]:
    """The stories-mode extension of the resolve context: the spec folder, the
    manifest entry for the story (title/description/checkpoint flags/invoke_dev_with),
    and — when the escalated spec is a fixed-slug pre-planning-halt sentinel — a
    sentinel indicator with its kind and recorded blocking condition. Best-effort:
    an unreadable manifest just yields the folder (resolve still runs)."""
    from . import stories

    project = Path(state.project)
    folder = stories.resolve_spec_folder(project, state.spec_folder)
    ctx: dict[str, Any] = {"spec_folder": state.spec_folder}
    try:
        entry = stories.load_stories(folder).get(story_key)
    except (stories.StoriesError, OSError, UnicodeDecodeError):
        entry = None
    if entry is not None:
        ctx["story"] = {
            "id": entry.id,
            "title": entry.title,
            "description": entry.description,
            "spec_checkpoint": entry.spec_checkpoint,
            "done_checkpoint": entry.done_checkpoint,
            "invoke_dev_with": entry.invoke_dev_with,
        }
    try:
        st = stories.resolve_story_spec(folder, story_key)
    except (OSError, UnicodeDecodeError):
        st = None
    if st is not None and st.kind == stories.KIND_SENTINEL and st.path is not None:
        try:
            condition = stories.recorded_blocking_condition(st.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            condition = ""
        ctx["sentinel"] = {
            "kind": st.sentinel_kind,
            "path": st.path.as_posix(),
            "blocking_condition": condition,
        }
    return ctx


def run_session(adapter, project: Path, run_dir: Path, story_key: str, *, model: str = "") -> bool:
    """Launch the interactive resolve agent attached to the caller's terminal.

    Blocks until the agent session exits. Returns whether the agent produced a
    resolution marker. The context file must already be written (build_context).
    """
    spec = SessionSpec(
        task_id=f"{story_key}-resolve-1",
        role="dev",
        prompt=f"/bmad-loop-resolve {story_key}",
        cwd=project,
        env={
            # deliberately NOT BMAD_LOOP_MODE: this session is interactive, a
            # human is present, the skill must be allowed to ask.
            "BMAD_LOOP_RUN_DIR": str(run_dir),
            "BMAD_LOOP_STORY_KEY": story_key,
            "BMAD_LOOP_RESOLVE_CONTEXT": str(context_path(run_dir, story_key)),
        },
        model=model,
    )
    # Drop any marker from a previous resolve of this story: otherwise the agent
    # sees it and reports "already resolved", and a session that records nothing
    # would still look like it produced a resolution.
    marker = resolution_path(run_dir, story_key)
    marker.unlink(missing_ok=True)
    argv = adapter.interactive_argv(spec)
    env = {**os.environ, **adapter.interactive_env(spec)}
    subprocess.run(argv, cwd=str(project), env=env)  # noqa: S603 - attached, inherited stdio
    return marker.is_file()
