"""Where code+git work happens, decoupled from where run state lives.

A Workspace pairs the directory sessions run in (and git operates on) with the
artifact paths rebased onto it. Run state (run_dir, journal, state.json) always
lives in the main repo and is passed separately — it never moves.

- isolation = none → Workspace.default(paths): root = paths.repo_root, behavior
  identical to operating directly on the project.
- isolation = worktree → per unit: a git worktree mounted under the run dir
  (.bmad-loop/runs/<run_id>/worktrees/, which `bmad-loop init` gitignores, so it
  stays invisible to the main checkout's `git status`), with paths rebased onto
  it. open_unit_workspace / close_unit_workspace manage the branch + worktree
  lifecycle; the engine merges the unit branch back into the target branch from
  the main repo between units.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import verify
from .bmadconfig import ProjectPaths
from .platform_util import safe_ref_segment, safe_segment
from .recovery_flow import attempt_preserve_ref_name

# Per-unit worktrees live under the run dir (.bmad-loop/runs/<run_id>/worktrees/),
# which `bmad-loop init` already gitignores — so unit checkouts never show up as
# untracked files in the main checkout. Crucially they must NOT live under .git/:
# a cwd inside .git/ is treated as git-internal by the coding CLIs (Claude Code),
# which then refuse to load the project's bmad-loop-* skills — breaking every
# worktree session (`Unknown command: /bmad-build-auto`).
WORKTREE_DIRNAME = "worktrees"


def unit_worktrees_dir(run_dir: Path) -> Path:
    """The parent dir holding this run's per-unit worktrees."""
    return run_dir / WORKTREE_DIRNAME


def _rmtree_confined(wt: Path, run_dir: Path) -> bool:
    """rmtree `wt` only when it resolves to a strict descendant of this run's
    worktrees dir; returns whether deletion was attempted. The teardown fallbacks
    reach for rmtree with paths that can arrive from persisted task state
    (`task.worktree_path` via discard_worktree and `_reopen_unit`), and rmtree —
    unlike `git worktree remove` — performs no validation of its own, so a
    corrupt or hand-edited state entry must not be able to point it at the repo
    root or anywhere else outside the run's scaffolding (same doctrine as
    runs.reconcile_orphan_worktrees / resolve_run_dir)."""
    try:
        root = unit_worktrees_dir(run_dir).resolve()
        target = wt.resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return False
    if target == root:
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


@dataclass(frozen=True)
class Workspace:
    root: Path  # where sessions run (cwd) and git operates
    paths: ProjectPaths  # artifact paths rebased onto `root`

    @classmethod
    def default(cls, paths: ProjectPaths) -> Workspace:
        """The zero-config workspace: work happens in the repo root in place."""
        return cls(root=paths.repo_root, paths=paths)


@dataclass(frozen=True)
class UnitWorkspace:
    """A per-unit worktree workspace plus the bookkeeping needed to merge it
    back and tear it down from the main repo."""

    workspace: Workspace  # rebased onto the worktree dir
    repo_root: Path  # the main repo (where merges + worktree removal happen)
    branch: str  # the unit branch checked out in the worktree
    path: Path  # the worktree dir
    baseline: str  # commit the worktree was cut from (for failed-diff capture)


def unit_branch_name(run_id: str, unit_key: str, branch_per: str) -> str:
    """branch_per=run shares one branch across the whole run; branch_per=story
    gives each unit its own branch.

    Both segments are ref-sanitized: `--run-id` is user-suppliable and a unit key is
    a sprint-board / ledger id, so either can carry ref-illegal sequences (`:`, `..`,
    `@{`, a trailing `.lock`) that git rejects at branch-creation time. Clean ids —
    every auto-generated run id and every conventional story key — pass through
    byte-identical. This is the single source of the name: `open_unit_workspace` is
    the sole caller and stores the result on `task.branch`, which every consumer
    (`_merge_local`, `discard_worktree`, `close_unit_workspace`) reuses.
    """
    if branch_per == "run":
        return f"bmad-loop/{safe_ref_segment(run_id)}"
    return f"bmad-loop/{safe_ref_segment(run_id)}/{safe_ref_segment(unit_key)}"


def open_unit_workspace(
    repo_root: Path,
    paths: ProjectPaths,
    run_id: str,
    unit_key: str,
    base: str,
    branch_per: str,
    run_dir: Path,
) -> UnitWorkspace:
    """Mount a fresh worktree for `unit_key` and return its rebased workspace.

    The worktree is mounted under the run dir (see unit_worktrees_dir), not under
    .git/. A new unit branch is cut from a pinned resolution of ``base``. Existing
    run-scoped branches reattach at their own pinned tip so earlier landed units
    remain reachable. Existing story-scoped branches are abandoned-attempt state:
    commits unique to their named tip are parked under ``attempt-preserve/*``, then
    the story branch is compare-and-swap reset to the pinned base before remount.
    """
    branch = unit_branch_name(run_id, unit_key, branch_per)
    unresolved_wt = unit_worktrees_dir(run_dir) / safe_segment(unit_key)
    try:
        wt = unresolved_wt.resolve()
    except (OSError, RuntimeError) as e:
        raise verify.GitError(
            f"cannot resolve worktree mount path for {unit_key} ({unresolved_wt}): {e}"
        ) from e
    # Pin every moving input before the first mutation. A story branch is an
    # attempt-local name: reclaim preserves any commits unique to its old tip,
    # then resets it to the requested base with compare-and-swap semantics. A run
    # branch is cumulative and deliberately keeps its own tip across remounts.
    pinned_base = verify.rev_parse_revision(repo_root, base)
    branch_tip: str | None = None
    if verify.branch_exists(repo_root, branch):
        branch_tip = verify.rev_parse_revision(repo_root, f"refs/heads/{branch}")
    if branch_tip is not None and branch_per == "story":
        commits = verify.commits_above(repo_root, pinned_base, branch_tip)
        if commits:
            preserve_ref = attempt_preserve_ref_name(run_id, branch_tip)
            verify.preserve_commits(
                repo_root,
                pinned_base,
                preserve_ref,
                commits=commits,
                revision=branch_tip,
            )
        verify.reset_branch_if_tip(repo_root, branch, pinned_base, branch_tip)
    wt.parent.mkdir(parents=True, exist_ok=True)
    # Reclaim whatever still occupies this unit's mount point before adding.
    # `wt` and `branch` are both DETERMINISTIC in (run_id, unit_key, run_dir), so a
    # re-mount targets the exact path a previous mount used — and `worktree_add`
    # refuses a target that exists or a branch checked out elsewhere, which makes a
    # leftover registration a hard `GitError` rather than a recoverable state.
    # `engine._finish_inflight` reaches that shape by design: when live policy leaves
    # isolation it releases the mount's state and clears the task's claim but
    # deliberately LEAVES the directory standing (the journal names it), so a later
    # flip back to `worktree` re-derives this same path and used to be unrecoverable
    # through the normal run flow. Reclaiming here rather than deleting at the flip
    # keeps that preservation intact for the in-place run and spends the orphan only
    # when a mount actually needs its path.
    #
    # The BRANCH is deliberately not passed: `discard_worktree` would force-delete it.
    # A run-scoped name carries commits earlier units landed; a story-scoped name has
    # already been safely reset above. Dropping only the worktree frees the checkout
    # that blocks `worktree_add` without introducing a second ref mutation here.
    discard_worktree(repo_root, str(wt), "", run_dir=run_dir)
    if branch_tip is not None:
        verify.worktree_add(repo_root, wt, branch, create=False)
        if branch_per == "story":
            try:
                mounted_tip = verify.rev_parse_head(wt)
                current_tip = verify.rev_parse_revision(repo_root, f"refs/heads/{branch}")
                if mounted_tip != pinned_base or current_tip != pinned_base:
                    raise verify.GitError(
                        f"story branch {branch} moved after reclaim reset: "
                        f"expected {pinned_base}, mounted {mounted_tip}, current {current_tip}"
                    )
            except (verify.GitError, OSError):
                # The branch may have moved after the reset CAS but before checkout.
                # Drop only the mount we just created; the rival ref is evidence and
                # must not be reset or deleted by this failure cleanup.
                discard_worktree(repo_root, str(wt), "", run_dir=run_dir)
                raise
    else:
        verify.worktree_add(repo_root, wt, branch, base=pinned_base, create=True)
    # A story checkout was already verified against the pinned base above.  Do
    # not re-read its symbolic HEAD after that boundary: a rival ref move in this
    # final window would record unverified history as the attempt baseline even
    # though the mounted index and files still represent ``pinned_base``.
    baseline = pinned_base if branch_per == "story" else verify.rev_parse_head(wt)
    return UnitWorkspace(
        workspace=Workspace(root=wt, paths=paths.rebased(wt)),
        repo_root=repo_root,
        branch=branch,
        path=wt,
        baseline=baseline,
    )


def close_unit_workspace(
    unit: UnitWorkspace,
    *,
    success: bool,
    keep_failed: bool,
    run_dir: Path,
    unit_key: str,
    delete_branch: bool = True,
    detach_kept: bool = False,
    diff_max_file_bytes: int | None = None,
    on_teardown_degraded: Callable[[str], None] | None = None,
) -> Path | None:
    """Tear down (or preserve) a unit's worktree.

    On failure the unit's full diff against its baseline is written to
    `run_dir/failed/<unit_key>/changes.patch` for forensics; when keep_failed is
    set the worktree + branch are left mounted for inspection and nothing else
    happens. On success (or failure without keep_failed) the worktree is removed
    and, if delete_branch, the branch deleted. Returns the patch path it wrote,
    or None.

    Invariant: the teardown tail is post-merge/post-capture housekeeping — the
    unit's content is already safe (merged on success, patch-captured on failure)
    before it runs, so no git teardown failure escapes it. Every `GitError` from
    the worktree removal or branch deletion degrades to a call of
    on_teardown_degraded (given the failure message) instead of crashing the run;
    a clean or force-retried removal is silent (see the teardown tail below). A
    failed diff *capture* breaks the tail's premise instead — the worktree would
    then hold the only copy of the unit's changes — so it is reported the same
    way but preserves the worktree + branch rather than tearing them down. The
    callback itself is the caller's (the engine's `journal.append`, whose OSError
    is engine-wide journal semantics, deliberately unguarded here).

    detach_kept (branch_per=run only): when keep_failed preserves the worktree,
    detach its HEAD so the shared run branch it holds is freed for the next unit
    to mount — otherwise every later unit's `git worktree add` collides on the
    already-checked-out branch. Best effort; see the keep_failed branch below.

    diff_max_file_bytes caps the per-untracked-file size in that forensic patch
    (None = no cap); see verify.capture_diff.
    """
    patch: Path | None = None
    if not success:
        capture_err: verify.GitError | None = None
        try:
            diff = (
                verify.capture_diff(unit.path, unit.baseline, max_file_bytes=diff_max_file_bytes)
                if unit.baseline
                else ""
            )
        except verify.GitError as e:
            capture_err = e
            diff = ""
        if diff:
            patch = run_dir / "failed" / safe_segment(unit_key) / "changes.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(diff, encoding="utf-8")
        if capture_err is not None and on_teardown_degraded is not None:
            # the forensic patch is the only copy of a dropped unit's changes; a
            # failed capture means the teardown below would destroy them, so the
            # unit is preserved as if keep_failed (the `or` on the branch below).
            on_teardown_degraded(
                f"diff capture failed for {unit.path}: {capture_err}; "
                "worktree and branch preserved (uncaptured changes)"
            )
        if keep_failed or capture_err is not None:
            if detach_kept:
                # branch_per=run shares one branch across the run; a kept worktree
                # left checked out on it blocks every later unit's `git worktree
                # add` (git refuses a branch checked out elsewhere). Detach HEAD to
                # free the shared branch name while preserving the working tree,
                # uncommitted changes, and the branch ref (still at the kept commit)
                # for inspection. Best effort: on failure the later unit still
                # surfaces the collision via the worktree-open-failed defer path.
                try:
                    verify.checkout_detach(unit.path)
                except verify.GitError:
                    pass
            return patch  # leave the worktree mounted (branch detached if shared)

    # success, or a failure we are not keeping: remove the worktree. A failed
    # tree is dirty, so force; a successful unit was committed + merged, so its
    # tree is clean, but force is harmless and tolerant of stray artifacts.
    try:
        verify.worktree_remove(unit.repo_root, unit.path, force=not success)
    except verify.GitError as first_err:
        try:
            # Ordinary dirty-tree case: the plain remove refused stray untracked
            # artifacts, which --force clears. Not a degradation — stay silent.
            verify.worktree_remove(unit.repo_root, unit.path, force=True)
        except verify.GitError as retry_err:
            # gh-139 fingerprint the retry CAN'T fix: a process the just-ended
            # session left running (e.g. pytest recreating `.pytest_cache`) makes
            # the plain remove fail with ENOTEMPTY (first_err), and by then git has
            # already deleted its admin entry `.git/worktrees/<id>` — so the --force
            # retry fails with "is not a working tree" (retry_err). Force can't
            # restore a dropped admin entry, so fall back to git's own reclaim path
            # (plain rmtree + prune) and degrade to a warning: the content already
            # landed, so this is housekeeping, not a run failure. Both git errors
            # go into the message — each half of the fingerprint is diagnostic.
            # The rmtree is confined to the run's worktrees dir: on resume the
            # path comes from persisted state (_reopen_unit), which git validates
            # but rmtree would not.
            removed = _rmtree_confined(unit.path, run_dir)
            verify.worktree_prune(unit.repo_root)
            msg = (
                f"worktree remove failed for {unit.path} ({first_err}); "
                f"force retry failed ({retry_err}); fell back to rmtree+prune"
            )
            if not removed:
                msg += f" (rmtree refused: {unit.path} resolves outside this run's worktrees dir)"
            elif unit.path.exists():
                # rmtree lost the race too (the writer recreated files under it):
                # the dir survives under the gitignored run dir and is reclaimed
                # later by trim_run_dir / clean — note it, don't block on it.
                msg += f" (dir still present: {unit.path})"
            if on_teardown_degraded is not None:
                on_teardown_degraded(msg)
    try:
        if delete_branch and verify.branch_exists(unit.repo_root, unit.branch):
            # the unit's content is already on the target branch (success) or saved
            # to a patch (failure), so a force delete loses nothing — and squash
            # merges leave the branch looking "unmerged" to `git branch -d`.
            # prune above frees a branch whose worktree dir was rmtree'd, so this
            # still runs after a degraded removal.
            verify.delete_branch(unit.repo_root, unit.branch, force=True)
    except verify.GitError as e:
        # second crash door in this teardown tail: branch deletion is likewise
        # post-merge housekeeping, so degrade it rather than raise past here.
        if on_teardown_degraded is not None:
            on_teardown_degraded(f"branch delete failed for {unit.branch}: {e}")
    return patch


def discard_worktree(repo_root: Path, worktree_path: str, branch: str, *, run_dir: Path) -> None:
    """Best-effort force teardown of a worktree + branch by path/name, for
    resume-restart of a crashed/interrupted unit. Tolerant of partial state.
    `worktree_path` arrives from persisted task state, so the rmtree fallback is
    confined to `run_dir`'s worktrees dir (see _rmtree_confined)."""
    if worktree_path:
        wt = Path(worktree_path)
        try:
            if wt.exists():
                verify.worktree_remove(repo_root, wt, force=True)
        except verify.GitError:
            # same gh-139 hazard as close_unit_workspace: a stray process (or a
            # dropped admin entry) can keep `git worktree remove` from clearing
            # the dir. A leftover dir breaks the resume re-mount at this same path
            # (`git worktree add` refuses a non-empty target), so drop to rmtree.
            _rmtree_confined(wt, run_dir)
        # prune git's admin entry regardless (harmless when nothing is stale) so a
        # half-removed worktree can't block that re-mount.
        verify.worktree_prune(repo_root)
    if branch:
        try:
            if verify.branch_exists(repo_root, branch):
                verify.delete_branch(repo_root, branch, force=True)
        except verify.GitError:
            pass
