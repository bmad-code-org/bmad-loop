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

from dataclasses import dataclass
from pathlib import Path

from . import verify
from .bmadconfig import ProjectPaths
from .platform_util import safe_ref_segment, safe_segment

# Per-unit worktrees live under the run dir (.bmad-loop/runs/<run_id>/worktrees/),
# which `bmad-loop init` already gitignores — so unit checkouts never show up as
# untracked files in the main checkout. Crucially they must NOT live under .git/:
# a cwd inside .git/ is treated as git-internal by the coding CLIs (Claude Code),
# which then refuse to load the project's bmad-loop-* skills — breaking every
# worktree session (`Unknown command: /bmad-dev-auto`).
WORKTREE_DIRNAME = "worktrees"


def unit_worktrees_dir(run_dir: Path) -> Path:
    """The parent dir holding this run's per-unit worktrees."""
    return run_dir / WORKTREE_DIRNAME


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
    .git/. The unit branch is cut from `base` (the target branch's HEAD). When the
    branch already exists (branch_per=run re-mounting the shared run branch
    across serial units) it is re-checked-out from its own HEAD instead, so it
    keeps the commits earlier units already landed on it.
    """
    branch = unit_branch_name(run_id, unit_key, branch_per)
    wt = (unit_worktrees_dir(run_dir) / safe_segment(unit_key)).resolve()
    wt.parent.mkdir(parents=True, exist_ok=True)
    if verify.branch_exists(repo_root, branch):
        verify.worktree_add(repo_root, wt, branch, create=False)
    else:
        verify.worktree_add(repo_root, wt, branch, base=base, create=True)
    baseline = verify.rev_parse_head(wt)
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
    diff_max_file_bytes: int | None = None,
) -> Path | None:
    """Tear down (or preserve) a unit's worktree.

    On failure the unit's full diff against its baseline is written to
    `run_dir/failed/<unit_key>/changes.patch` for forensics; when keep_failed is
    set the worktree + branch are left mounted for inspection and nothing else
    happens. On success (or failure without keep_failed) the worktree is removed
    and, if delete_branch, the branch deleted. Returns the patch path it wrote,
    or None.

    diff_max_file_bytes caps the per-untracked-file size in that forensic patch
    (None = no cap); see verify.capture_diff.
    """
    patch: Path | None = None
    if not success:
        try:
            diff = (
                verify.capture_diff(unit.path, unit.baseline, max_file_bytes=diff_max_file_bytes)
                if unit.baseline
                else ""
            )
        except verify.GitError:
            diff = ""
        if diff:
            patch = run_dir / "failed" / safe_segment(unit_key) / "changes.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(diff, encoding="utf-8")
        if keep_failed:
            return patch  # leave the worktree + branch mounted

    # success, or a failure we are not keeping: remove the worktree. A failed
    # tree is dirty, so force; a successful unit was committed + merged, so its
    # tree is clean, but force is harmless and tolerant of stray artifacts.
    try:
        verify.worktree_remove(unit.repo_root, unit.path, force=not success)
    except verify.GitError:
        verify.worktree_remove(unit.repo_root, unit.path, force=True)
    if delete_branch and verify.branch_exists(unit.repo_root, unit.branch):
        # the unit's content is already on the target branch (success) or saved
        # to a patch (failure), so a force delete loses nothing — and squash
        # merges leave the branch looking "unmerged" to `git branch -d`.
        verify.delete_branch(unit.repo_root, unit.branch, force=True)
    return patch


def discard_worktree(repo_root: Path, worktree_path: str, branch: str) -> None:
    """Best-effort force teardown of a worktree + branch by path/name, for
    resume-restart of a crashed/interrupted unit. Tolerant of partial state."""
    if worktree_path:
        wt = Path(worktree_path)
        try:
            if wt.exists():
                verify.worktree_remove(repo_root, wt, force=True)
        except verify.GitError:
            pass
    if branch:
        try:
            if verify.branch_exists(repo_root, branch):
                verify.delete_branch(repo_root, branch, force=True)
        except verify.GitError:
            pass
