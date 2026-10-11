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
a board that cannot be read (DW-533). The mint-time board read does not cover a replay
run resumed later (`--no-resume`, or paused and resumed after another run finished the
story), so `WorktreeFlow.refuse_superseded_replay` re-reads the main checkout's board
right before the replay run's merge starts: a done row or an unreadable board journals
``replay-merge-refused`` and keeps the branch and escalates, leaving the replay run paused
(DW-534). No LLM call anywhere: this is deterministic orchestration.

Closing a replay run whose unit escalated (DW-535): `bmad-loop resolve <replay-run>
--abandon`. Plain ``resume`` re-pauses at the escalation, ``--adopt-branch`` meets the
same board gate again and a re-arm re-drives a superseded story, so the operator
vouches the work is superseded and `abandon_replay_run` closes the run instead. Scoped
to a replay run paused at an escalation whose named task is ESCALATED
(`abandon_refusal` holds the replay-run, phase and kept-work checks; the pause check
stays with each caller). Under the replay run's state lock it parks any
uncommitted state of the kept worktree (or a detached HEAD's commits the unit branch
lacks) on a ``refs/attempt-preserve-dirty/*-abandon`` ref
(`workspace.preserve_abandoned_unit`) and journals it as ``replay-abandon-snapshot``,
removes the worktree with
`workspace.discard_worktree` passing no branch — the kept branch always survives —
verifies the worktree is gone from disk and from git's list (a repair write: a
leftover raises and the run stays paused), journals ``replay-abandoned``, then saves
the run ``finished`` with its pause cleared. The task stays ESCALATED with its
``branch`` and ``worktree_path`` as the record. A worktree already gone (an earlier
abandon crashed after the teardown) skips straight to the state write, reporting the
snapshot ref that attempt journaled. The sprint
board, the deferred-work ledger, the finished origin run and the run's mux session are
never touched; no engine runs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import runs, runsetup, verify
from .bmadconfig import ProjectPaths
from .journal import Journal, load_state, save_state, state_lock
from .model import PAUSE_ESCALATION, Phase, RunState, StoryTask
from .platform_util import pinned_root_identity, root_identity_record, safe_segment
from .policy import Policy
from .workspace import (
    discard_worktree,
    preserve_abandoned_unit,
    unit_worktrees_dir,
)


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
            # The finished run's records are launch-time and never re-stamped: after a
            # project move the worktree and the code root follow the live project
            # (`standalone_replay_refusal` located them there). The
            # recorded mount stays the base the task's own absolute spellings re-home
            # from — they were written while the run ran at the old path.
            recorded_src = Path(task.worktree_path)
            src = runs.rebase_recorded_project_path(recorded_src, finished, project)
            code_root = runs.rebase_recorded_code_root(finished, project)
            dst = unit_worktrees_dir(new_dir) / safe_segment(story_key)
            dst.parent.mkdir()
            # Recorded BEFORE the move: git renames the tree before it updates its
            # admin files, so a move that raises (a git die, a `_run_git` timeout, an
            # interrupt) can still have moved it — the except arm checks `dst`.
            moved = (code_root, src, dst)
            try:
                verify.worktree_move(code_root, src, dst)
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
            src=recorded_src,
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


# ------------------------------------------------- resolve --abandon (DW-535)


class AbandonError(Exception):
    """The replay run could not be abandoned. Raised before any write leaves state,
    journal, worktree and branch untouched; raised after the snapshot or the teardown
    leaves the run paused, with nothing journaled beyond the snapshot's
    ``replay-abandon-snapshot`` row."""


@dataclass(frozen=True)
class AbandonOutcome:
    """What `abandon_replay_run` kept: the unit branch (and whether it still exists
    after the teardown) and the snapshot ref of uncommitted state (or of a detached
    HEAD's commits), if any."""

    branch: str
    branch_kept: bool
    ref: str | None
    # A retry recovered ``ref`` from the journal but it no longer exists: pruned
    # under ``scm.preserve_keep`` between the attempts, its work unrecoverable.
    ref_pruned: bool = False


def abandon_refusal(state: RunState, task: StoryTask, story_key: str) -> str | None:
    """Why `resolve --abandon` cannot close this run for ``story_key``, or None when it
    can (DW-535).

    Shared by `cli._resolve_abandon`'s early exit and `abandon_replay_run`'s locked
    re-check, so the two cannot drift. The pause check stays with each caller, which
    words it for its surface (as `runs.adopt_refusal` does)."""
    if not state.replay_of:
        return (
            f"--abandon closes replay runs only, and run {state.run_id} is not one; "
            f"re-arm the escalation with `bmad-loop resolve {state.run_id}`, or finish "
            f"it from its kept branch with `bmad-loop resolve {state.run_id} "
            "--adopt-branch`"
        )
    if task.phase != Phase.ESCALATED:
        return f"story {story_key} is not escalated (phase: {task.phase})"
    if not task.worktree_path or not task.branch:
        return f"story {story_key} has no kept worktree unit to abandon"
    return None


def abandon_replay_run(run_dir: Path, story_key: str, *, paths: ProjectPaths) -> AbandonOutcome:
    """Close the escalated replay run at ``run_dir`` without merging its unit, under
    the run's state lock (reentrant, so a caller already holding it may call this).

    Order: re-check → registered and owned (`_foreign_worktree_refusal`) →
    snapshot → journal the snapshot → teardown → verify gone, with no worktree left
    under the run dir → journal → save state. The snapshot row is written BEFORE the teardown so the ref
    is durably on record while the worktree that held the work still stands. A crash
    or fault after the teardown leaves a paused run with no worktree; re-running
    abandon completes it, reporting the newest snapshot row's ref (after a crash
    between the journal and the save, with a second ``replay-abandoned`` row). ``paths`` is the current config: ``paths.repo_root`` is the
    repository the worktree is registered with (as `WorktreeFlow.reopen_unit` reads
    it). Liveness and the task generation are the CLI caller's locked checks.
    Raises AbandonError."""
    with state_lock(run_dir):
        state = load_state(run_dir)
        if state.paused_stage != PAUSE_ESCALATION:
            raise AbandonError(runs.not_escalation_pause_message(run_dir.name, state.paused_stage))
        task = state.tasks.get(story_key)
        if task is None:
            raise AbandonError(f"run {run_dir.name} has no task for story {story_key}")
        if (refusal := abandon_refusal(state, task, story_key)) is not None:
            raise AbandonError(refusal)
        repo_root = paths.repo_root
        # The recorded mount is launch-time: after a project move, the bare path names
        # a tree that is gone and would send a standing worktree down the
        # already-gone arm — finished unsnapshotted, its contents left to `delete`.
        wt = runs.rebase_recorded_project_path(Path(task.worktree_path), state, paths.project)
        ref: str | None = None
        ref_pruned = False
        if os.path.lexists(wt):
            try:
                registered = verify.worktree_is_registered(repo_root, wt)
            except verify.GitError as e:
                raise AbandonError(
                    f"cannot tell whether {wt} is a registered worktree ({e}); not abandoning"
                ) from e
            if not registered:
                # Not git's linked worktree: git run inside it would address the
                # project checkout, so nothing in it can be snapshotted — and the
                # teardown's rmtree fallback would delete it unsaved.
                raise AbandonError(
                    f"the kept worktree path {wt} exists but is not a registered git "
                    f"worktree of {repo_root}, so its contents cannot be preserved; "
                    "inspect it, remove it by hand, then re-run abandon"
                )
            if (foreign := _foreign_worktree_refusal(run_dir, wt, task)) is not None:
                raise AbandonError(foreign)
            try:
                ref = preserve_abandoned_unit(
                    repo_root, wt, state.run_id, story_key, paths, task.spec_file, task.branch
                )
            except verify.GitError as e:
                raise AbandonError(f"{e}; nothing was abandoned") from e
            if ref is not None:
                Journal(run_dir).append(
                    "replay-abandon-snapshot",
                    story_key=story_key,
                    worktree=str(wt),
                    ref=ref,
                )
        try:
            if os.path.lexists(wt):
                discard_worktree(repo_root, str(wt), "", run_dir=run_dir)
            else:
                # Gone already (a crashed earlier abandon, or removed by hand): drop
                # any stale admin entry so the listing below can answer, and recover
                # the snapshot an earlier attempt parked before failing after its
                # teardown — else the journal and stdout read as a clean abandon.
                verify.worktree_prune(repo_root)
                ref = _earlier_abandon_ref(run_dir, story_key)
                # Journaled is not kept: another run's retention may have pruned it
                # since, and reporting it as parked would point at nothing.
                ref_pruned = ref is not None and not verify.ref_exists_checked(repo_root, ref)
            _verify_worktree_gone(repo_root, wt)
            _refuse_worktrees_left_under_run(repo_root, run_dir, wt)
            # Checked, not `branch_exists`: an unreadable answer must not be journaled
            # as `branch_kept: false` and printed as a deleted recovery branch.
            branch_kept = verify.branch_exists_checked(repo_root, task.branch)
        except verify.GitError as e:
            raise AbandonError(
                f"tearing down the kept worktree {wt} failed ({e}); the run is still "
                "paused — re-run abandon"
            ) from e
        Journal(run_dir).append(
            "replay-abandoned",
            story_key=story_key,
            replay_of=state.replay_of,
            branch=task.branch,
            branch_kept=branch_kept,
            worktree=str(wt),
            ref=ref,
            ref_pruned=ref_pruned,
        )
        state.clear_pause()
        state.finished = True
        save_state(run_dir, state)
        return AbandonOutcome(
            branch=task.branch, branch_kept=branch_kept, ref=ref, ref_pruned=ref_pruned
        )


def _foreign_worktree_refusal(run_dir: Path, wt: Path, task: StoryTask) -> str | None:
    """Why the standing tree at ``wt`` may not be this task's unit worktree, or None.

    `verify.worktree_is_registered` proves only that ``wt`` is SOME linked worktree
    of the repository, and the teardown removes whatever it is. So ``wt`` must sit
    directly under this run's own worktrees dir, where the replay mint and the engine
    mount units, and be the directory recorded at its mint (``task.worktree_identity``,
    DW-446). The identity compare is inode-only, as the locked
    `runs.reconcile_root_identities` re-bind is, so a reboot that renumbered
    ``st_dev`` does not refuse. A ``None`` record (an unpinnable or zero-inode mount)
    leaves nothing to compare; the placement check stands alone."""
    if _canonical(wt.parent) != _canonical(unit_worktrees_dir(run_dir)):
        return (
            f"the kept worktree path {wt} is not under run {run_dir.name}'s worktrees "
            "directory, so it may not be this run's unit; inspect it, remove it by hand "
            "if it is, then re-run abandon"
        )
    if task.worktree_identity is None:
        return None
    info = pinned_root_identity(wt)
    if info is None or info.st_ino != task.worktree_identity[1]:
        return (
            f"the kept worktree path {wt} no longer holds the directory this run "
            "minted there (a link, or a different tree — possibly another worktree); "
            "inspect it, remove it by hand if it is disposable, then re-run abandon"
        )
    return None


def _earlier_abandon_ref(run_dir: Path, story_key: str) -> str | None:
    """The ref of the newest ``replay-abandon-snapshot`` row an earlier abandon of
    ``story_key`` journaled before its teardown, or None when no attempt parked
    anything. Read from the journal rather than matched against ref names, so a
    serialized ``-r<n>`` snapshot is found and a ref pruned since is still named —
    the caller checks it still exists and reports ``ref_pruned`` when it does not.
    Raises AbandonError when the journal cannot be read — that is not "nothing was
    parked"."""
    try:
        entries = Journal(run_dir).entries()
    except (OSError, UnicodeDecodeError) as e:
        raise AbandonError(
            f"cannot read the journal of run {run_dir.name} to recover the snapshot an "
            f"earlier abandon parked ({e}); the run is still paused — re-run abandon"
        ) from e
    for entry in reversed(entries):
        if (
            isinstance(entry, dict)
            and entry.get("kind") == "replay-abandon-snapshot"
            and entry.get("story_key") == story_key
            and isinstance(entry.get("ref"), str)
        ):
            return entry["ref"]
    return None


def _verify_worktree_gone(repo_root: Path, wt: Path) -> None:
    """Raise AbandonError unless ``wt`` is off disk AND off ``repo_root``'s worktree
    list. A repair write's check: `discard_worktree` is best effort (its rmtree
    fallback and prune swallow failures), so the run is not recorded finished over a
    worktree that survived it."""
    if os.path.lexists(wt):
        raise AbandonError(
            f"the kept worktree {wt} is still on disk after its removal; the run is "
            "still paused — remove it by hand, then re-run abandon"
        )
    try:
        listed = verify.worktree_list(repo_root)
    except verify.GitError as e:
        raise AbandonError(
            f"cannot list the worktrees of {repo_root} to confirm {wt} was removed ({e}); "
            "the run is still paused — re-run abandon"
        ) from e
    target = _canonical(wt)
    if any(_canonical(p) == target for p in listed):
        raise AbandonError(
            f"the kept worktree {wt} is still registered with git after its removal; "
            f"the run is still paused — if it is locked, `git worktree unlock {wt}` "
            "first; then `git worktree prune` and re-run abandon"
        )


def _refuse_worktrees_left_under_run(repo_root: Path, run_dir: Path, wt: Path) -> None:
    """Raise AbandonError while any worktree registered with ``repo_root`` still sits
    under ``run_dir``. A kept unit moved with `git worktree move` leaves its recorded
    path absent, which reads as already gone, while the tree, registered at its new
    path, still holds the work. Under the run dir, a later `delete`'s rmtree would
    destroy it unsnapshotted."""
    try:
        listed = verify.worktree_list(repo_root)
    except verify.GitError as e:
        raise AbandonError(
            f"cannot list the worktrees of {repo_root} to confirm none is left under run "
            f"{run_dir.name} ({e}); the run is still paused — re-run abandon"
        ) from e
    root = _canonical(run_dir)
    left = [
        str(p)
        for p in listed
        if (c := _canonical(p)) == root or c.startswith(root.rstrip(os.sep) + os.sep)
    ]
    if left:
        raise AbandonError(
            f"worktree {', '.join(left)} is still registered under run {run_dir.name}'s "
            "directory — the kept unit may have been moved there; the run is still "
            f"paused — move it back to {wt} with `git worktree move` (or remove it), "
            "then re-run abandon"
        )


def _canonical(path: Path) -> str:
    """A comparable spelling of a path that may no longer exist: the existing prefix
    resolved (symlinked tmp dirs), case-folded where the filesystem folds."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    return os.path.normcase(str(resolved))
