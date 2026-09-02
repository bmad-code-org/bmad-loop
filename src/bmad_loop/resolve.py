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
from .engine import _session_task_id
from .escalation import critical_escalations
from .journal import TASK_CYCLE_ARTIFACTS
from .model import RunState
from .platform_util import safe_segment
from .runs import (
    redrive_base_ref,
    spec_reaches_the_redrive,
    task_spec_path,
    task_stories_root,
    validate_restore_latch,
)

RESOLVE_DIR = "resolve"


def _story_dir(run_dir: Path, story_key: str) -> Path:
    return run_dir / RESOLVE_DIR / safe_segment(story_key)


def context_path(run_dir: Path, story_key: str) -> Path:
    return _story_dir(run_dir, story_key) / "context.json"


def resolution_path(run_dir: Path, story_key: str) -> Path:
    return _story_dir(run_dir, story_key) / "resolution.json"


class ResolutionError(Exception):
    """resolution.json exists but cannot be used (unreadable / not a JSON object)."""


def read_resolution(run_dir: Path, story_key: str) -> dict[str, Any] | None:
    """Parse the resolve agent's ``resolution.json`` marker. Returns None ONLY
    when the marker is absent; a present-but-unusable marker (unreadable, bad
    JSON, non-object top level) raises ResolutionError instead. The file is the
    agent's recorded decision — possibly including a ``restore_patch`` (the
    intent-gap patch-restore path, BMAD-METHOD #2564) — and a re-arm consumes
    the escalation, so collapsing corruption into "nothing recorded" would
    silently downgrade a confirmed restore to an unrecoverable from-scratch
    re-drive. The caller validates the ``restore_patch`` path itself before
    acting on it."""
    path = resolution_path(run_dir, story_key)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        # UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 marker
        # must raise the clean ResolutionError, not crash (same class as the
        # read_frontmatter / stories read-path hardening).
        raise ResolutionError(f"resolution marker {path} is unreadable ({e})") from e
    if not isinstance(doc, dict):
        raise ResolutionError(f"resolution marker {path} is not a JSON object")
    return doc


def _gather_escalations(
    run_dir: Path,
    state: RunState,
    story_key: str,
    *,
    start: int = 0,
    skipped: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The CRITICAL escalations recorded by this story's sessions, newest first,
    each DISTINCT escalation exactly once, paired with how many DISTINCT entries
    were withheld as already answered.

    ``start`` is ``task.escalations_resolved_upto`` — a position in the append-only
    ``task.sessions`` list, stamped by ``runs.rearm_escalation`` when a resolve cycle
    recorded a resolution (DW-11). Records BELOW it were already put to the human and
    answered, so their escalations are not shown again; the count of those the human
    can no longer see is returned for the operator, never written into
    ``context.json`` (the agent-facing contract is the unanswered set alone). The
    default 0 reproduces the pre-DW-11 walk byte-for-byte, which is what a
    pre-upgrade ``state.json`` deserializes to.

    Reads each session's tasks/<task_id>/ artifacts — the same files the engine
    inspected when it decided to pause. WHICH files is not spelled here: it is
    ``journal.TASK_CYCLE_ARTIFACTS``, the one list this reader shares with the two
    adapters that clear the same directory in ``start_session``, so a name added
    there reaches all three sites at once. Ordering is `reversed(task.sessions)`
    and, within a directory, that constant's own order — result.json before
    escalation.json; a duplicate keeps its FIRST occurrence's position, which is
    what preserves "newest first". Three guards, each for a defect this reader
    hit on the way to the operator:

    * ``seen_ids`` — ``task.sessions`` is append-only and a re-arm deliberately
      does NOT clear it, so state can carry two records under one ``task_id``.
      ``sweep._rearm_generation`` bumps the id namespace for new restarts but
      does not migrate records already persisted, and both records address the
      SAME mutable ``tasks/<id>/escalation.json`` — reading it per record
      attributes the abandoned cycle's escalation to the fresh session too.
      Open each directory once.
    * the content-keyed map — the sweep skill's own contract
      (``data/skills/bmad-loop-sweep/automation-mode.md``) tells a producer to
      write ``escalation.json`` and then mirror the same entries into
      ``result.json`` ``escalations``. That mirroring is deliberate and stays;
      the READER absorbs it, so a compliant producer is not shown to the human
      twice. The key is canonical JSON because the two copies are parsed
      separately — identity cannot see the mirroring and ``dict`` is unhashable
      — and ``setdefault`` makes the first occurrence win. De-duplication is
      global across the pass, not per directory; it removes only exact repeats,
      so a directory holding CRITICAL A in one file and A + B in the other still
      yields both.
    * the ``except`` tuple and the ``list`` check — ``build_context`` is an
      OBSERVATION path: a malformed artifact must cost its own contents and
      nothing more, never raise out to the interactive resolve command.
      ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError`` (the same
      rationale recorded on ``read_resolution`` above), and ``json.loads`` can
      also raise a plain ``ValueError`` when an integer exceeds Python's configured
      digit limit. Deeply nested input can raise ``RecursionError`` while either
      parsing the document or canonicalizing an entry, so both operations live
      under the same artifact-level guard. Meanwhile,
      ``critical_escalations`` iterates ``escalations`` with no list guard of its
      own, so a ``{"escalations": null}`` artifact would raise ``TypeError``
      here. The guard belongs in this caller; the shared predicate stays the
      single definition of CRITICAL.

    The watermark is a FOURTH concern layered onto that same single walk, not a
    second pass: ``reversed(task.sessions)`` reaches the unanswered tail first, so
    entries are routed into two content-keyed maps by the record's own index and the
    suppressed count is the answered keys that never appeared in the shown map. Two
    consequences are deliberate. An entry raised on BOTH sides of the watermark is
    shown and counted 0 — "not shown" is the claim the number makes, so it must never
    count something the operator can see. And ``start`` only SELECTS a map; nothing is
    indexed with it, so a watermark past the end of the list yields an empty shown
    list rather than an IndexError. A ``task_id`` repeated across the watermark is
    opened once by ``seen_ids``, at its newest occurrence — the shown side, the
    conservative direction.

    ``skipped`` is an OUT-parameter, and it exists because the degrade above is
    silent in exactly the place silence is unaffordable. An artifact dropped by the
    ``except`` costs its own escalations — but ``runs.rearm_escalation`` then stamps
    ``escalations_resolved_upto = len(task.sessions)``, which covers the session that
    artifact belonged to. A transient read fault (a network mount, a truncated write
    still in flight) therefore buries every escalation in it FOREVER: the next cycle
    reads the file fine and withholds it as already answered. The caller refuses to
    record coverage when this set is non-empty, which is the same
    observe-and-degrade contract this reader already keeps — it just stops the
    degrade from being laundered into a durable claim.

    It is an out-parameter rather than a third return value on purpose: the
    ``(list, int)`` pair is asserted by ~20 tests and read positionally by
    ``build_context``, and this signal has one consumer. Only skips on the SHOWN
    side are recorded — ``target is found`` — because those are the records the
    watermark would NEWLY cover. A skip below ``start`` was already covered by the
    previous cycle's watermark, so re-covering it buries nothing; it costs only the
    withheld COUNT, which claims nothing durable. Paths are collected rather than a
    bare tally so a duplicate artifact cannot inflate the answer."""
    task = state.tasks.get(story_key)
    if task is None:
        return [], 0
    seen_ids: set[str] = set()
    found: dict[str, dict[str, Any]] = {}
    answered: dict[str, dict[str, Any]] = {}
    last = len(task.sessions) - 1
    for offset, session in enumerate(reversed(task.sessions)):
        if session.task_id in seen_ids:
            continue
        seen_ids.add(session.task_id)
        target = found if last - offset >= start else answered
        task_dir = run_dir / "tasks" / session.task_id
        for fname in TASK_CYCLE_ARTIFACTS:
            fpath = task_dir / fname
            if not fpath.is_file():
                continue
            try:
                doc = json.loads(fpath.read_text(encoding="utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("artifact is not a JSON object")
                if "escalations" not in doc:
                    # The ordinary shape of a clean ``result.json``: nothing was
                    # raised, so there is nothing to show and nothing hidden. NOT a
                    # skip — counting it as one would withhold coverage from every
                    # resolve cycle, permanently.
                    continue
                if not isinstance(doc["escalations"], list):
                    raise ValueError("'escalations' is not a list")
                artifact_entries: dict[str, dict[str, Any]] = {}
                for esc in critical_escalations(doc):
                    artifact_entries.setdefault(json.dumps(esc, sort_keys=True), esc)
            except (OSError, ValueError, RecursionError):
                if skipped is not None and target is found:
                    skipped.add(str(fpath))
                continue
            for key, esc in artifact_entries.items():
                target.setdefault(key, esc)
    return list(found.values()), sum(1 for key in answered if key not in found)


def build_context(
    state: RunState, run_dir: Path, story_key: str, *, isolation: str
) -> tuple[Path, int, int]:
    """Write resolve/<story_key>/context.json for the resolve skill to read, and
    return it beside the number of already-answered escalations withheld from it and
    the number of session artifacts this walk could NOT read.

    The withheld count is for the OPERATOR's terminal (`cli.cmd_resolve` prints it) and
    is deliberately not a `context.json` field: the skill's contract is singular —
    resolve the escalation you are shown — and a count of things the agent cannot see is
    not something it can act on. It comes from the same single walk that produced the
    shown list, never from a second `_gather_escalations` call subtracting lengths.

    The unreadable count rides the SAME walk for the same reason, and it is a third
    return value rather than a second out-parameter because it has to cross a process's
    worth of control flow: `cli.cmd_resolve` is the only surface that can advance
    `escalations_resolved_upto` (the TUI hard-codes `resolution_recorded=False`), and a
    non-zero count there means this cycle showed the human strictly less than the
    watermark would claim they answered. The caller withholds coverage on it — see
    `_gather_escalations`' `skipped` for why the alternative is permanent burial. Zero
    on every ordinary run, so the coverage path is unchanged whenever the run-dir reads
    cleanly.

    `isolation` is the LIVE policy's `scm.isolation`, and it is required rather than
    defaulted for the reason this surface exists at all: three of the fields below —
    `restore_supported`, `spec_reaches_the_redrive`, `redrive_base_ref` — are claims
    about a re-drive that has NOT happened yet, and run state cannot answer them. The
    mode is re-read at every resume and a mid-run change is journalled, never refused,
    so the recorded `task.worktree_path` says only how the escalated attempt RAN. A
    defaulted mode would hand the agent an in-place answer for a run that mounts (or the
    reverse) with nothing to signal it — the defect being fixed, re-introduced at the
    seam that reports it."""
    task = state.tasks.get(story_key)
    isolated_redrive = isolation == "worktree"
    # Patch-restore availability (#2564): the shared `validate_restore_latch`
    # verdict, not a local copy of one leg. Any of them — worktree isolation (the
    # re-drive discards and re-mounts the unit's worktree), a spec-less escalation,
    # a pre-planning sentinel wedge — means the orchestrator would reject a
    # `restore_patch` after the session; told to the agent up front so it never
    # negotiates a restore it can't honor.
    restore_supported = task is not None and (
        validate_restore_latch(state, task, story_key, worktree_isolation=isolated_redrive) is None
    )
    # Which tree holds this run's STORY MANIFEST — the workspace root, answered by
    # `task_stories_root` rather than by `task_spec_root`. The latter answers a
    # write-confinement question about `spec_file` and falls back to the project for an
    # out-of-mount spec; borrowing it here pointed the sentinel and the stories block at
    # the main checkout while `stories_engine._stories_folder` was still the mount, so
    # one `context.json` could name two trees.
    stories_root = task_stories_root(task, state)
    # DW-11: hide what an earlier resolve cycle already answered. `start` is the task's
    # own watermark — 0 for a task never resolved, and for every pre-upgrade
    # `state.json`, which is the unfiltered pre-DW-11 walk.
    unreadable: set[str] = set()
    escalations, withheld = _gather_escalations(
        run_dir,
        state,
        story_key,
        start=task.escalations_resolved_upto if task else 0,
        skipped=unreadable,
    )
    context = {
        "story_key": story_key,
        "run_id": state.run_id,
        # Absolute, matching the shape `bmad-loop-resolve/SKILL.md` documents: an
        # isolated unit's `spec_file` is persisted RELATIVE to the mounted worktree
        # (`model.StoryTask._serialized_worktree_path`) and the agent session runs
        # from the project root, where the main checkout carries the same
        # `_bmad-output/specs/...` layout — the raw value would name the wrong
        # tree's copy. `task_spec_path` is the same re-anchor `rearm_escalation`
        # writes through, so the agent edits the file the re-arm will flip.
        # as_posix() for the same reason `resolution_path` below uses it — the
        # context contract is one string on every OS — and because the value this
        # replaces was ALREADY posix under isolation: `_serialized_worktree_path`
        # persists the relative form with `.as_posix()`. It also normalizes the
        # NON-isolated absolute case, which was previously emitted verbatim: on Windows
        # that changes `C:\\...\\spec.md` to `C:/.../spec.md`. Deliberate — one
        # spelling for every reader — and consumed by an agent, which accepts '/'.
        "spec_file": (task_spec_path(task, state).as_posix() if task and task.spec_file else None),
        "baseline_commit": task.baseline_commit if task else None,
        "paused_reason": state.paused_reason,
        "escalations": escalations,
        # as_posix so the context contract is the same string on every OS (the
        # path is consumed by the agent, and Python/tools accept '/' on Windows).
        "resolution_path": resolution_path(run_dir, story_key).as_posix(),
        "restore_supported": restore_supported,
        # Whether an edit to `spec_file` survives to the re-drive that reads it. Under
        # isolation the mount is discarded by `engine._finish_inflight`, so the agent
        # can otherwise spend a whole session editing a file with no future and see
        # every write succeed. `rearm_escalation` already journals
        # `rearm-spec-write-unreachable` on this same verdict; naming it here is what
        # lets the session act on it instead of learning it afterwards.
        # Guarded on `task.spec_file` exactly as `spec_file` above is: a verdict about
        # whether an edit SURVIVES is meaningless beside a `"spec_file": null`, and
        # emitting one invited the session to act on a reachability answer for a file
        # the same document says does not exist. Both fields are one claim.
        "spec_reaches_the_redrive": (
            spec_reaches_the_redrive(task, state, isolated_redrive=isolated_redrive)
            if task and task.spec_file
            else None
        ),
        # WHERE a correction has to land to be read. Emitted beside the verdict
        # because on its own `spec_reaches_the_redrive: false` states a problem with
        # no remedy: the session is told the edit is doomed, and the obvious repair
        # (commit it) is wrong in two different ways for an isolated unit. Committing
        # from the main checkout cannot include a file that lives in the linked unit
        # worktree, and committing on the unit's own branch does not put it on the ref
        # the replacement worktree is cut from. That ref is this one — the run's
        # PINNED `target_branch` when the re-drive will MOUNT, `HEAD` otherwise — and
        # it is the same value `rearm_escalation`'s unreachable-write record names, so
        # the session and the orchestrator quote one answer.
        #
        # Keyed on the LIVE isolation mode, not on the recorded mount. This file is
        # written by a SEPARATE process, before the resume runs, so it is the one
        # consumer no amount of resume-time bookkeeping on `task.worktree_path` could
        # reach: reading the mount here sent the session to commit on the pinned branch
        # for a run whose policy had since flipped to `none`, where the re-drive reads
        # `HEAD` in the main checkout and never looks — and answered `HEAD` for the
        # mirror flip, where it mounts and never reads a working tree at all.
        "redrive_base_ref": (
            redrive_base_ref(state, isolated_redrive=isolated_redrive) if task else None
        ),
    }
    # Stories mode: hand the resolver the manifest intent (the story entry) and a
    # sentinel indicator, so it sees WHAT the story is meant to do and WHETHER the
    # frozen spec even exists yet (a sentinel has no plan to edit — resolve the
    # underlying ambiguity instead). Sprint mode leaves the context unchanged.
    if state.source == "stories":
        stories_ctx = _stories_context(state, story_key, stories_root)
        if stories_ctx:
            context["stories"] = stories_ctx
    path = context_path(run_dir, story_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    return path, withheld, len(unreadable)


def _stories_context(state: RunState, story_key: str, root: Path) -> dict[str, Any]:
    """The stories-mode extension of the resolve context: the spec folder, the
    manifest entry for the story (title/description/checkpoint flags/invoke_dev_with),
    and — when the escalated spec is a fixed-slug pre-planning-halt sentinel — a
    sentinel indicator with its kind and recorded blocking condition. Best-effort:
    an unreadable manifest just yields the folder (resolve still runs)."""
    from . import stories

    # `root`, not `Path(state.project)`: the caller resolved it with
    # `task_stories_root`, so this block reads the manifest and sentinel out of the tree
    # the RUN owns. One `context.json` that names two trees is worse than one that names
    # the wrong one — `sentinel.path` and `blocking_condition` would otherwise describe a
    # file the re-arm will never touch, or vanish entirely because the main checkout has
    # no sentinel while the mount does.
    folder = stories.resolve_spec_folder(root, state.spec_folder)
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


def run_session(
    adapter,
    project: Path,
    run_dir: Path,
    story_key: str,
    *,
    generation: int,
    model: str = "",
) -> bool:
    """Launch the interactive resolve agent attached to the caller's terminal.

    Blocks until the agent session exits. Returns whether the agent produced a
    resolution marker. The context file must already be written (build_context).

    Nothing consumes this session's ``task_id``: no task dir is created,
    ``interactive_argv`` ignores it, ``interactive_env`` does not export it, and no
    ``SessionRecord`` is appended. It is minted through ``engine._session_task_id``
    anyway because this was the FOURTH hand-mint site outside that chokepoint and
    there are now none. The property being restored is whole-composition
    sanitization: sanitizing ``story_key`` alone and concatenating the suffix AFTER
    can push a key already at ``MAX_SEGMENT`` back past it, and ``safe_segment``'s
    digest differs between the two orders. Since nothing reads the id, no collision
    follows from one — ``seq`` is a hardcoded 1 and several ``cmd_resolve`` paths
    return before the re-arm, so a later resolve of the same story may legitimately
    re-mint a byte-identical id.

    ``generation`` is required with no default for the reason that docstring gives —
    an implicit 0 is right in every run that never re-armed and wrong only on the one
    that did, so a default here would reproduce the defect at this seam. ``cmd_resolve``
    CALLS this before ``runs.rearm_escalation``, so the value it passes is the one still
    on disk, i.e. pre-bump. Not because the read is ordered against the bump: the re-arm
    reloads state and mutates its own copy, leaving the caller's ``task`` untouched.
    """
    spec = SessionSpec(
        task_id=_session_task_id(story_key, "resolve", 1, generation),
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
    subprocess.run(argv, cwd=str(project), env=env)  # attached, inherited stdio
    return marker.is_file()
