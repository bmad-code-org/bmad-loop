"""Standalone kept-unit replay for a FINISHED run (DW-525).

`bmad-loop resolve <finished-run> --reverify --story <key>`. Under
``scm.isolation = "worktree"`` + ``scm.keep_failed`` a DEFERRED unit's worktree and
branch outlive the run, so the kept work is re-verifiable — but a finished run has no
resume for `resolve --reverify` to ride (DW-522 re-arms a PAUSED run). The contract:

* A NEW **replay run** is minted — fresh run id, dir, journal and state — seeded with
  ONE copy of the finished run's task, latched for a verify replay by the same
  `runs.latch_for_reverify` the paused re-arm uses, with ``RunState.replay_of`` naming
  the finished run (pinned at mint, never re-derived on resume). It is then resumed
  through the ordinary resume path, whose `Engine._finish_inflight` reverify arm
  reopens the unit, replays verify, reviews per policy and integrates.
  `Engine._loop` returns right after that recovery for a replay run: it never picks
  another story, runs the run-end retrospective or auto-sweeps.
* The replay run takes OWNERSHIP of the kept worktree: it is `git worktree move`d into
  ``<replay-run-dir>/worktrees/``. Teardown is confined to the owning run dir
  (`workspace._rmtree_confined`), and `runs.reconcile_stale_worktrees` force-removes
  every worktree under a finished run at the next `run`/`sweep` start, so a replay left
  mounting the finished run's tree would race that. The move is also the double-replay
  guard: a second replay of the same unit finds the worktree gone and refuses.
* The finished run is never re-opened: its ``state.json`` is read, never written (it
  stays ``finished``), and its journal gets exactly one ``unit-replay-handoff`` pointer
  naming the replay run. The replay run's journal opens with ``unit-replay-start``.
* The mint is a transaction. The locked re-check (state re-loaded, the refusal re-run,
  liveness, ``generation`` unchanged) and the worktree move run under the FINISHED
  run's state lock; the replay run's own publication follows outside it, because
  `journal.state_lock` refuses to nest two runs' locks (and `save_state` /
  `runs.delete_run` take the replay run's). Once moved, the finished run no longer
  holds the unit, so a rival replay refuses on "worktree gone" either way. Any failure
  after the move moves the worktree back — a repair write, so its own failure raises
  naming both paths and leaves the claimed dir in place (it holds the worktree) — and
  then removes the claimed run dir; nothing persisted in the finished run changes.

Refused, each with its own message (`runs.standalone_replay_refusal`): an in-place
(non-worktree) task, a detached `scm.branch_per = "run"` unit, sweep runs,
stories-mode runs, and a story already done on the main checkout's sprint board, or
a board that cannot be read (DW-533). No LLM call anywhere: this is deterministic orchestration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import runs, runsetup, verify
from .bmadconfig import ProjectPaths
from .journal import Journal, load_state, save_state, state_lock
from .model import RunState, StoryTask
from .platform_util import root_identity_record, safe_segment
from .policy import Policy
from .workspace import unit_worktrees_dir


class ReplayError(Exception):
    """The replay run could not be minted; nothing was left behind (or, when the
    worktree could not be moved back, the message names where it is)."""


def mint_replay_run(
    *,
    project: Path,
    paths: ProjectPaths,
    policy: Policy,
    finished_run_dir: Path,
    story_key: str,
    expected_generation: int,
    trusted_config_digest: str,
    force: bool = False,
) -> Path:
    """Mint the replay run for ``story_key``'s kept unit of the finished run at
    ``finished_run_dir`` and return the new run dir — persisted, resumable (neither
    paused nor finished), not yet driven. No engine is constructed here.

    ``expected_generation`` is the task generation the caller's lock-free checks
    saw; a different one under the lock refuses (the story changed meanwhile).
    ``force`` admits an ``unknown`` engine liveness, as `resolve --force` does; a
    provably-live engine always refuses. Raises ReplayError."""
    new_dir: Path | None = None
    claim = None
    moved: tuple[Path, Path, Path] | None = None  # (code root, src, dst)
    try:
        with state_lock(finished_run_dir):
            finished = load_state(finished_run_dir)
            task = finished.tasks.get(story_key)
            if task is None:
                raise ReplayError(f"run {finished.run_id} has no task for story {story_key}")
            if task.generation != expected_generation:
                raise ReplayError(
                    f"story {story_key} changed while resolve was in progress — not replaying"
                )
            refusal = runs.standalone_replay_refusal(
                finished, task, story_key, run_dir=finished_run_dir, project_root=project
            )
            if refusal is not None:
                raise ReplayError(refusal)
            live = runs.engine_liveness(finished_run_dir)
            if live == "alive":
                raise ReplayError(f"run {finished.run_id} is still live — stop it first")
            if live == "unknown" and not force:
                raise ReplayError(
                    f"run {finished.run_id}: engine may still be live (unverifiable pid) — "
                    "refusing to replay. Confirm the engine process is gone, then re-run "
                    "with --force (`stop` cannot verify or clear an unverifiable pid)."
                )
            run_id = runs.new_run_id()
            new_dir = project / runs.RUNS_DIR / run_id
            try:
                claim = runsetup._claim_run_dir(new_dir)
            except SystemExit as e:  # an id collision: the claim refuses, never adopts
                new_dir = None
                raise ReplayError(f"replay run id {run_id} already exists — retry") from e
            src = Path(task.worktree_path)
            dst = unit_worktrees_dir(new_dir) / safe_segment(story_key)
            dst.parent.mkdir()
            # Recorded BEFORE the move: git renames the tree before it updates its
            # admin files, so a move that raises (a git die, a `_run_git` timeout, an
            # interrupt) can still have moved it — the except arm checks `dst`.
            moved = (finished.code_root, src, dst)
            try:
                verify.worktree_move(finished.code_root, src, dst)
            except verify.GitError as e:
                raise ReplayError(
                    f"cannot move the kept worktree of story {story_key} into the replay "
                    f"run ({e})"
                ) from e
        state = _replay_state(
            finished,
            task,
            story_key,
            run_id=run_id,
            project=project,
            paths=paths,
            policy=policy,
            src=src,
            mount=dst,
            trusted_config_digest=trusted_config_digest,
            run_dir_identity=runsetup._claim_identity(claim),
        )
        _publish_replay_run(new_dir, state, story_key, src=src, project=project)
        _journal_handoff(finished_run_dir, story_key, replay_run=run_id, src=src, dst=dst)
    except BaseException as exc:
        if moved is not None and os.path.lexists(moved[2]):
            code_root, src, dst = moved
            try:
                verify.worktree_move(code_root, dst, src)
            except (verify.GitError, OSError) as restore_exc:
                # A repair write: raise, and keep the claimed dir — it holds the tree.
                raise ReplayError(
                    f"replay of story {story_key} failed after its kept worktree moved "
                    f"({type(exc).__name__}: {exc}), and moving it back failed too "
                    f"({restore_exc}): the worktree is at {dst}, its original path was "
                    f"{src} — move it back with `git worktree move {dst} {src}`"
                ) from restore_exc
        if claim is not None and new_dir is not None:
            runsetup._unwind_composition(project, new_dir, None, claim)
        raise
    return new_dir


def _replay_state(
    finished: RunState,
    task: StoryTask,
    story_key: str,
    *,
    run_id: str,
    project: Path,
    paths: ProjectPaths,
    policy: Policy,
    src: Path,
    mount: Path,
    trusted_config_digest: str,
    run_dir_identity: tuple[int, int] | None,
) -> RunState:
    """The replay run's launch state: one copy of ``task`` re-homed from ``src`` onto
    ``mount`` and latched for a verify replay, scoped to that story alone."""
    # A copy through the persisted form (JSON round-trip included), so nothing is
    # shared with the finished run's objects; spec paths come out relative to the
    # OLD mount project.
    seeded = StoryTask.from_dict(json.loads(json.dumps(task.to_dict(finished.mount_project(task)))))
    # Absolute spellings into the old mount must follow the move: the replay's
    # artifact gate reads the latest completed dev result's `spec_file`, which a
    # session reports as an absolute path in the tree it ran in.
    seeded.spec_file = _rehomed(seeded.spec_file, src, mount)
    seeded.dispatched_spec_file = _rehomed(seeded.dispatched_spec_file, src, mount)
    seeded.restore_patch = _rehomed(seeded.restore_patch, src, mount)
    for record in seeded.sessions:
        if record.result_json is not None and isinstance(record.result_json.get("spec_file"), str):
            record.result_json["spec_file"] = _rehomed(record.result_json["spec_file"], src, mount)
    state = runsetup.build_run_state(
        run_id=run_id,
        project=project,
        repo_root=paths.repo_root,
        policy=policy,
        epic_filter=None,
        story_filter=story_key,
        max_stories=1,
        stories_on=False,
        spec_folder="",
        trusted_config_digest=trusted_config_digest,
        run_dir_identity=run_dir_identity,
    )
    state.run_type = "story"
    state.replay_of = finished.run_id
    state.target_branch = finished.target_branch
    seeded.worktree_path = str(mount)
    # This process performed the move, so it re-records the mount's identity from
    # the moved path rather than trusting the finished run's record (DW-446).
    seeded.worktree_identity = root_identity_record(mount)
    state.tasks = {story_key: seeded}
    seeded.rebase_spec_paths_on(state.mount_project(seeded) or mount)
    runs.latch_for_reverify(seeded, "deferred")
    return state


def _rehomed(raw: str | None, src: Path, dst: Path) -> str | None:
    """``raw`` moved from under ``src`` to the same place under ``dst``; anything
    else (empty, relative, outside ``src``) unchanged."""
    if not raw or not Path(raw).is_absolute():
        return raw
    try:
        rel = Path(raw).relative_to(src)
    except ValueError:
        # Spelled through a symlinked prefix (macOS /var -> /private/var): compare
        # the resolved spellings before giving up.
        try:
            rel = Path(raw).resolve().relative_to(src.resolve())
        except (OSError, RuntimeError, ValueError):
            return raw
    return str(dst / rel)


def _publish_replay_run(
    run_dir: Path, state: RunState, story_key: str, *, src: Path, project: Path
) -> None:
    """Open the replay run's journal with `unit-replay-start`, then persist its state
    and the out-of-tree config pin (#498), as `runsetup.compose_run` does."""
    task = state.tasks[story_key]
    journal = Journal(run_dir)
    journal.append(
        "unit-replay-start",
        replay_of=state.replay_of,
        story_key=story_key,
        branch=task.branch,
        from_worktree=str(src),
        worktree=task.worktree_path,
        baseline=task.baseline_commit or "",
    )
    save_state(run_dir, state)
    runs.write_trusted_config_digest(project, state.run_id, state.trusted_config_digest)


def _journal_handoff(
    finished_run_dir: Path, story_key: str, *, replay_run: str, src: Path, dst: Path
) -> None:
    """The one entry the finished run gets: a pointer to the replay run that now owns
    its kept unit. Its state.json is never written."""
    journal = Journal(finished_run_dir)
    journal.append(
        "unit-replay-handoff",
        story_key=story_key,
        replay_run=replay_run,
        from_worktree=str(src),
        worktree=str(dst),
    )
