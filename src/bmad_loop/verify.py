"""Deterministic post-session verification. Never trust LLM self-reports.

verify_dev / verify_review check artifacts on disk and git state against
what the session's result.json claims; run_verify_commands executes the
policy's test/lint gates with the orchestrator's own subprocess calls.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import deferredwork
from .bmadconfig import ProjectPaths
from .frontmatter import read_frontmatter_or_none  # noqa: F401 — re-export
from .frontmatter import set_frontmatter_status  # noqa: F401 — re-export
from .frontmatter import _split_frontmatter, read_frontmatter, status_of
from .model import StoryTask, VerifyOutcome
from .policy import POLICY_FILE, Policy
from .sprintstatus import story_status

GIT_TIMEOUT_S = 120
COMMAND_TIMEOUT_S = 30 * 60

# Current bound on a single git subprocess. Module state rather than a per-call
# parameter so the ~40 git helpers need no threading; the engine overrides it
# from `limits.git_timeout_s` at startup, everything else keeps the default.
_git_timeout_s = GIT_TIMEOUT_S


def configure_git_timeout(seconds: int) -> None:
    """Set the per-git-call timeout (`limits.git_timeout_s`). Called once by the
    engine when it binds its policy; standalone verify users keep GIT_TIMEOUT_S."""
    global _git_timeout_s
    _git_timeout_s = seconds


# How git's own diff format names the absent side of a creation/deletion. A
# protocol token git emits verbatim on every platform, Windows included — never
# opened, never joined onto. Only `patch_new_files` reads it.
_DIFF_ABSENT = "/dev/null"  # portability: git diff-format token, not a real path

# result.json `workflow` value for the dev pass. A machine contract: the
# orchestrator forges this value in `devcontract` when synthesizing the dev
# result from the spec the bmad-dev-auto session leaves on disk; a mismatch
# means the wrong artifacts, so we reject rather than trust them. (Sweep's
# triage/migrate workflows have their
# own constants in sweep.py; the review skill is verified by on-disk artifacts
# only and is not handed its result.json.)
DEV_WORKFLOW = "auto-dev"

# Repo-relative posix path of the orchestrator config, for git pathspecs.
POLICY_FILE_REL = POLICY_FILE.as_posix()
# The orchestrator's own working dir (.bmad-loop/) — config, ledger, run state,
# engine plugins. Excluded wholesale from merge-collision detection: none of it
# is ever a unit branch's merged content, so a dirty .bmad-loop/ must neither
# block a merge as "stray work" nor be auto-cleaned.
AUTOMATOR_DIR_REL = POLICY_FILE.parent.as_posix()


class GitError(Exception):
    pass


def _run_git(
    cmd: list[str], repo: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Sole spawn point for git subprocesses. A timeout is raised by
    `subprocess.run` *before* any return code exists, so left uncaught it would
    bypass every `except GitError` guard and crash the run (#156); translating
    it here puts timeouts in the same taxonomy as any other git failure —
    observation guards degrade, unguarded paths fail typed.

    Every git child runs with `LC_ALL=C` so messages stay stable English: the one
    place that inspects git *text* rather than a return code — `safe_rollback`'s
    benign "pathspec did not match" tolerance — must not misread a translated
    message under a localized git (#236). Merged last so it wins over both the
    inherited environment and any explicit `env` (the `_git_env` callers' throwaway
    `GIT_INDEX_FILE` / synthetic identity vars are preserved by the spread)."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_git_timeout_s,
            env={**(env if env is not None else os.environ), "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {cmd[3]} timed out after {_git_timeout_s}s in {repo}") from exc


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = _run_git(["git", "-C", str(repo), *args], repo)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git_raw(repo: Path, *args: str) -> tuple[int, str]:
    """Like `_git` but returns stdout verbatim (no strip, no stderr merge) — for
    NUL-delimited (`-z`) output whose records can begin with a space (porcelain
    status codes like ' M'), which `_git`'s strip() would corrupt."""
    proc = _run_git(["git", "-C", str(repo), *args], repo)
    return proc.returncode, proc.stdout


def _git_env(repo: Path, *args: str, env: dict[str, str]) -> tuple[int, str]:
    """Like `_git` but runs with an explicit environment — used to point git at a
    throwaway `GIT_INDEX_FILE` so a snapshot can stage the tree without touching
    the real index."""
    proc = _run_git(["git", "-C", str(repo), *args], repo, env=env)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def rev_parse_head(repo: Path) -> str:
    rc, out = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        raise GitError(f"git rev-parse HEAD failed in {repo}: {out}")
    return out


def worktree_clean(repo: Path) -> bool:
    # The orchestrator's own config file (.bmad-loop/policy.toml) is excluded:
    # the TUI settings editor rewrites it, and a tracked config edit must not
    # count as a "dirty tree" that blocks run/sweep/validate or forces a commit.
    # Scope is policy.toml only — the deferred-work ledger also lives under
    # .bmad-loop/ and is meant to be committed (see sweep._commit_ledger).
    rc, out = _git(repo, "status", "--porcelain", "--", ".", f":(exclude){POLICY_FILE_REL}")
    if rc != 0:
        raise GitError(f"git status failed in {repo}: {out}")
    return out == ""


def same_commit(a: str, b: str) -> bool:
    """Hash equality tolerant of abbreviated forms (>= 7 chars, git's default
    --short length); sessions sometimes report `git rev-parse --short HEAD`."""
    if len(a) < 7 or len(b) < 7:
        return a == b
    return a.startswith(b) or b.startswith(a)


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is an ancestor of (or equal to) `descendant`.

    Any git failure — unknown ref, shallow history, not a repo, a timeout
    (surfacing as GitError since `_run_git` translates it, #156) — reads as
    False: callers use this to *relax* a gate, so uncertainty must keep the
    gate strict."""
    try:
        code, _ = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    except (OSError, GitError):
        return False
    return code == 0


def has_changes_since(
    repo: Path,
    baseline: str,
    exclude: tuple[str, ...] = (),
    *,
    baseline_untracked: list[str] | None = None,
) -> bool:
    """True if tracked changes since baseline OR untracked files exist.

    `exclude` is repo-relative posix dir prefixes whose changes don't count —
    used by the dev/bundle proof-of-work gate to ignore the orchestrator-owned
    BMAD artifact folders (see `artifact_relpaths`), so a session that only
    rewrites its own spec (e.g. the frontmatter-status reconcile) under those
    folders doesn't register as real implementation work. Mirrors
    `attempt_dirty`'s exclusion. Default `()` keeps the unscoped behavior.

    `baseline_untracked` is the untracked-file snapshot taken when the baseline
    was recorded; when given, those files already existed before the session ran
    and are subtracted, so pre-session residue (e.g. an earlier halt's saved
    intent-gap patch, which `_protected_relpaths` shields from every reset) can
    never masquerade as this session's work.

    `None` means count EVERY untracked file — deliberately the *opposite* of
    `attempt_dirty`'s `None` = ignore-all, and not an oversight. The two gates
    fail open in opposite directions: a proof-of-work gate must fail open toward
    "work happened" (a pre-snapshot run must not have its gate silently
    weakened into never seeing new files), while a rollback gate must fail open
    toward "nothing to remove" (never delete a file it cannot prove this attempt
    created). Keep it that way."""
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", ".", *_exclude_specs(exclude))
    if rc != 0:
        return True
    created = untracked_files(repo)
    if baseline_untracked is not None:
        created -= set(baseline_untracked)
    created = {p for p in created if not _path_under_any(p, exclude)}
    return bool(created)


def attempt_dirty(
    repo: Path,
    baseline: str,
    baseline_untracked: list[str] | None,
    exclude: tuple[str, ...] = (),
) -> bool:
    """True if a `safe_rollback` to `baseline` would change anything: tracked
    changes since baseline, or untracked files created since the baseline
    snapshot. `baseline_untracked=None` (a pre-snapshot run) means untracked
    files are never this attempt's to remove, so only tracked diff counts. This
    mirrors `safe_rollback`'s notion of what *this attempt* touched, so callers
    can skip a no-op reset/pause when the tree is already at baseline.

    `exclude` is repo-relative posix dir prefixes (e.g. the BMAD artifact
    folders) whose changes are orchestrator-owned and never count as a dev
    attempt's dirtiness — they pair with `safe_rollback`'s `preserve`, so a
    change confined to those folders reads as clean.

    `policy.toml` (the operator's orchestration config) is *always* excluded: it
    is never a dev attempt's change, `safe_rollback` always restores it, and a
    lone policy edit must not read as dirtiness — otherwise the manual-recovery
    loop could never terminate. Mirrors `worktree_clean`'s exclusion."""
    exclude = (POLICY_FILE_REL, *exclude)
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", ".", *_exclude_specs(exclude))
    if rc != 0:
        return True
    if baseline_untracked is None:
        return False
    created = untracked_files(repo) - set(baseline_untracked)
    created = {p for p in created if not _path_under_any(p, exclude)}
    return bool(created)


def _exclude_specs(dirs: tuple[str, ...]) -> list[str]:
    """git pathspec `:(exclude)<dir>` args for each repo-relative dir prefix."""
    return [f":(exclude){d}" for d in dirs]


def _path_under_any(path: str, prefixes: tuple[str, ...]) -> bool:
    """True if repo-relative posix `path` equals or sits under any `prefixes` dir."""
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def untracked_files(repo: Path) -> set[str]:
    """Untracked, non-ignored paths (repo-relative posix), mirroring what a
    plain `git clean -fd` (no -x) treats as removable. Ignored files are
    excluded, so they are never rollback candidates."""
    rc, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        raise GitError(f"git ls-files --others failed in {repo}: {out}")
    return {line.strip() for line in out.splitlines() if line.strip()}


def commits_above(repo: Path, baseline: str) -> list[str]:
    """Commit shas reachable from HEAD but not from ``baseline`` — the commits an
    attempt added on top of its pre-attempt baseline, in ``git rev-list`` order (do
    not assume a strict newest-first / HEAD-first ordering across merges or clock
    skew; callers that need the tip should read HEAD directly). Empty when HEAD is
    at or behind baseline. Raises GitError on a git failure (a bad baseline is a
    real error, never quietly "no commits")."""
    rc, out = _git(repo, "rev-list", f"{baseline}..HEAD")
    if rc != 0:
        raise GitError(f"git rev-list {baseline}..HEAD failed in {repo}: {out}")
    return [line for line in out.splitlines() if line]


def preserve_commits(
    repo: Path, baseline: str, ref_name: str, commits: list[str] | None = None
) -> str | None:
    """Park the commits an attempt made above ``baseline`` under a branch at HEAD
    so a following ``git reset --hard baseline`` cannot orphan them — they survive
    `git gc` and are recoverable by name, not just via the reflog. Returns
    ``ref_name`` on success; ``None`` when there is nothing to preserve (HEAD at/
    below baseline) or the branch could not be created (the caller must then refuse
    to reset rather than silently destroy committed work). ``-f`` because a retry
    within the same run may re-preserve the same head under the same name.

    ``commits`` lets a caller that already ran :func:`commits_above` pass the result
    in to skip a second ``git rev-list`` subprocess; ``None`` self-fetches (keeps the
    helper standalone/testable).

    ``None`` means *nothing to preserve* — never a failure. If commits exist but the
    branch cannot be created this raises :class:`GitError` (consistent with the rest
    of this module), so a caller can never mistake a preservation failure for a
    harmless no-op and reset past committed work."""
    if commits is None:
        commits = commits_above(repo, baseline)
    if not commits:
        return None
    rc, out = _git(repo, "branch", "-f", ref_name, "HEAD")
    if rc != 0:
        raise GitError(f"git branch -f {ref_name} HEAD failed in {repo}: {out}")
    return ref_name


class PrunePreserveError(GitError):
    """Partial :func:`prune_preserve_refs` / :func:`prune_preserve_dirty_refs`
    failure. The prune is per-ref best-effort, so refs may already be gone when
    a later one sticks — the ``deleted`` list keeps that destructive half
    structurally auditable (a caller can journal it, not just grep the message)
    and ``failed`` names each stuck ref with its git detail."""

    def __init__(self, message: str, *, deleted: list[str], failed: list[str]) -> None:
        super().__init__(message)
        self.deleted = deleted
        self.failed = failed


def _prune_refs(
    repo: Path,
    keep: int,
    prefix: str,
    *,
    label: str,
    strip: str,
    delete: Callable[[str], None],
) -> list[str]:
    """Shared retention loop behind the per-family pruners: list the refs under
    ``prefix``, keep the ``keep`` newest by committer date, best-effort delete
    the tail via ``delete``, and return the deleted names. ``strip`` is removed
    from each refname before it is deleted/reported (``refs/heads/`` for the
    branch family, nothing for bare refs). ``keep <= 0`` means "never prune" —
    returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` — after attempting every tail ref — when any
    individual delete failed. One stuck ref must not wedge the retention for
    everything behind it, so deletes are per-ref best-effort and the error
    carries both what was deleted and what was not."""
    if keep <= 0:
        return []
    rc, out = _git(
        repo,
        "for-each-ref",
        # ties on committerdate (same-second rollbacks) break by ascending
        # refname — an explicit, observable order rather than git's implicit
        # stable-sort fallback. Last --sort key is the primary one.
        "--sort=refname",
        "--sort=-committerdate",
        # full refname, not :short — a tag or remote ref sharing the name would
        # make :short emit an ambiguous form the deleter can't use
        "--format=%(refname)",
        prefix,
    )
    if rc != 0:
        raise GitError(f"git for-each-ref {label} failed in {repo}: {out}")
    refs = [line.removeprefix(strip) for line in out.splitlines() if line]
    deleted: list[str] = []
    failed: list[str] = []
    for name in refs[keep:]:
        try:
            delete(name)
        except Exception as exc:  # a git timeout/OSError on one ref
            # must not wedge the tail behind it any more than a GitError does; the
            # per-ref best-effort contract holds for the whole subprocess surface
            failed.append(f"{name} ({exc})")
            continue
        deleted.append(name)
    if failed:
        raise PrunePreserveError(
            f"{label} prune in {repo}: deleted {deleted or 'nothing'}, "
            f"could not delete {'; '.join(failed)}",
            deleted=deleted,
            failed=failed,
        )
    return deleted


def prune_preserve_refs(repo: Path, keep: int) -> list[str]:
    """Bounded retention for the ``attempt-preserve/*`` recovery branches that
    :func:`preserve_commits` parks before an auto-rollback reset: keep the
    ``keep`` most recent refs by committer date, force-delete the rest, and
    return the deleted branch names (empty when nothing is over budget). Only
    ``refs/heads/attempt-preserve/`` is ever listed, so branches outside that
    prefix and the ``refs/attempt-preserve-dirty/*`` snapshot refs are
    untouchable by construction — but the prefix itself is owned by the pruner:
    anything parked under it, however it got there, is subject to deletion.
    ``keep <= 0`` means "never prune" — returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` — after attempting every tail ref — when any
    individual delete failed (e.g. the ref is checked out here or in a
    worktree); see :func:`_prune_refs` for the best-effort contract."""
    return _prune_refs(
        repo,
        keep,
        "refs/heads/attempt-preserve/",
        label="attempt-preserve",
        strip="refs/heads/",
        delete=lambda name: delete_branch(repo, name, force=True),
    )


def prune_preserve_dirty_refs(repo: Path, keep: int) -> list[str]:
    """Bounded retention for the ``refs/attempt-preserve-dirty/*`` worktree
    snapshots that :func:`snapshot_worktree` parks before an auto-rollback
    reset: keep the ``keep`` most recent by committer date (the snapshot
    commit's committer date is its park time), delete the rest via
    ``git update-ref -d``, and return the deleted names. These refs live
    outside ``refs/heads/`` — they are not branches, so ``branch -D`` cannot
    touch them and the reported names are full refnames (there is no
    ``refs/heads/`` to strip). Only ``refs/attempt-preserve-dirty/`` is ever
    listed, so branches and every other ref are untouchable by construction.
    ``keep <= 0`` means "never prune" — returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` on a partial delete failure; see
    :func:`_prune_refs` for the best-effort contract."""

    def _delete(refname: str) -> None:
        rc, out = _git(repo, "update-ref", "-d", refname)
        if rc != 0:
            raise GitError(f"git update-ref -d {refname} failed in {repo}: {out}")

    return _prune_refs(
        repo,
        keep,
        "refs/attempt-preserve-dirty/",
        label="attempt-preserve-dirty",
        strip="",
        delete=_delete,
    )


def snapshot_worktree(
    repo: Path, ref_name: str, *, baseline_untracked: list[str] | None
) -> str | None:
    """Park the current *uncommitted* working-tree state — tracked edits/deletions
    AND run-created untracked files — under ``ref_name`` as a commit object, so a
    following ``git reset --hard`` (whose post-reset cleanup in
    :func:`safe_rollback` also deletes run-created untracked files) cannot
    silently destroy an attempt's in-progress work. The snapshot survives
    ``git gc`` and is recoverable by name (``git checkout <ref> -- .`` or
    ``git diff HEAD <ref>``).

    Captured through a throwaway temp index so the real index and working tree
    are left untouched: seed the temp index from HEAD, ``add -u`` the tracked
    edits/deletions, then stage only the untracked files *this run* created —
    ``untracked_files(repo)`` minus ``baseline_untracked`` (the snapshot taken
    when the baseline was captured). This mirrors :func:`safe_rollback`'s scope
    exactly: the snapshot holds precisely what the reset would destroy and never
    a pre-existing user untracked file. When ``baseline_untracked`` is ``None`` (a
    pre-upgrade/resumed run with no snapshot) no untracked file is staged — matching
    :func:`safe_rollback`, which then deletes none — so tracked edits are still
    parked but untracked files are left untouched. Ignored files are excluded throughout
    (``add -u`` only touches tracked paths; ``untracked_files`` honours
    ``.gitignore``). A tree is written and ``commit-tree``'d parented at HEAD
    under a synthetic ``bmad-loop`` identity so the snapshot commit succeeds even
    when no local/global git ``user.name``/``user.email`` is configured, then
    ``ref_name`` is pointed at the result. Compares only against HEAD — committed
    work above baseline is already parked by :func:`preserve_commits`, so this
    captures exactly what is not yet committed.

    Returns ``ref_name`` on success, or ``None`` when the tree is clean relative
    to HEAD (nothing to preserve — the intended non-destructive uncommitted-revert
    case). Raises :class:`GitError` on any git failure — the raise *surfaces* the
    capture failure so the caller can decide: the commit-preservation caller
    refuses to reset past unpreserved work, while the best-effort worktree caller
    journals the failure and proceeds (the recovery ref is a safety net, not a
    gate)."""
    head = rev_parse_head(repo)
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
        for args in (("read-tree", head), ("add", "-u")):
            rc, out = _git_env(repo, *args, env=env)
            if rc != 0:
                raise GitError(f"git {args[0]} (snapshot) failed in {repo}: {out}")
        # None baseline (pre-upgrade/resumed run, no snapshot): safe_rollback deletes
        # no untracked files, so park none either — coercing None to [] would instead
        # stage every current untracked file, including the user's pre-existing ones.
        if baseline_untracked is None:
            new: list[str] = []
        else:
            new = sorted(untracked_files(repo) - set(baseline_untracked))
        if new:
            rc, out = _git_env(repo, "add", "--", *new, env=env)
            if rc != 0:
                raise GitError(f"git add (snapshot untracked) failed in {repo}: {out}")
        rc, tree = _git_env(repo, "write-tree", env=env)
        if rc != 0:
            raise GitError(f"git write-tree (snapshot) failed in {repo}: {tree}")
    tree = tree.strip()
    rc, head_tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    if rc != 0:
        raise GitError(f"git rev-parse {head}^{{tree}} failed in {repo}: {head_tree}")
    if tree == head_tree.strip():
        return None  # working tree identical to HEAD — nothing uncommitted to park
    # A synthetic identity (merged over os.environ) so the snapshot commit succeeds
    # with no git user.name/user.email configured — else the best-effort caller would
    # catch the GitError and reset past the very work this ref exists to preserve.
    ident = {
        **os.environ,
        "GIT_AUTHOR_NAME": "bmad-loop",
        "GIT_AUTHOR_EMAIL": "bmad-loop@localhost",
        "GIT_COMMITTER_NAME": "bmad-loop",
        "GIT_COMMITTER_EMAIL": "bmad-loop@localhost",
    }
    rc, snap = _git_env(
        repo, "commit-tree", tree, "-p", head, "-m", "attempt worktree snapshot", env=ident
    )
    if rc != 0:
        raise GitError(f"git commit-tree (snapshot) failed in {repo}: {snap}")
    snap = snap.strip()
    rc, out = _git(repo, "update-ref", ref_name, snap)
    if rc != 0:
        raise GitError(f"git update-ref {ref_name} {snap[:12]} failed in {repo}: {out}")
    return ref_name


def safe_rollback(
    repo: Path,
    baseline: str,
    *,
    baseline_untracked: list[str] | None,
    keep: tuple[str, ...] = (".bmad-loop",),
    preserve: tuple[str, ...] = (),
) -> None:
    """Undo a failed attempt WITHOUT a blanket `git clean`.

    Reverts tracked changes to `baseline` (the dev attempt's commits/edits),
    then removes only untracked files that appeared since `baseline` — i.e.
    files this run created. Untracked files already present at baseline, every
    ignored file, and anything under a `keep` dir are preserved. The orchestrator
    therefore never runs `git clean -fd`, so it can't eat a user's pre-existing
    untracked work. `baseline_untracked` is the snapshot taken when the baseline
    was captured; None (a pre-upgrade run with no snapshot) removes nothing.

    `preserve` is repo-relative posix dir prefixes (the BMAD artifact folders)
    whose *tracked* content must survive the hard reset — e.g. a frozen spec the
    resolve workflow just corrected. The `git reset --hard` would otherwise
    revert them (keep only guards untracked deletion). We snapshot the current
    tree with `git stash create`, reset, then restore those paths from the
    snapshot. Untracked artifacts need no special handling: the reset leaves them
    alone and the cleanup below skips `keep` dirs.

    `policy.toml` (the operator's orchestration config) is *always* restored,
    regardless of `preserve`. It lives inside the kept `.bmad-loop` dir but is
    *tracked*, so a plain `git reset --hard` would silently revert it — an
    uncommitted edit (e.g. a freshly enabled `scm.rollback_on_failure`, gone
    before it ever takes effect) or a change committed after `baseline`. `keep`
    only guards untracked deletion, not tracked reverts. We can't ride the stash
    snapshot for it: `git stash create` emits an empty snapshot for a clean tree,
    so a policy change living in a *commit* (with no other working-tree dirt)
    would skip the restore and be lost. Instead we read policy.toml's on-disk
    content before the reset and write it straight back after — independent of
    the snapshot, covering both the uncommitted and committed cases.
    """
    # policy.toml: capture on-disk content now, restore unconditionally below.
    policy_path = repo / POLICY_FILE_REL
    policy_content = policy_path.read_bytes() if policy_path.is_file() else None

    rc, out = _git(repo, "stash", "create")
    snapshot = out.strip() if rc == 0 else ""
    rc, out = _git(repo, "reset", "--hard", baseline)
    if rc != 0:
        raise GitError(f"git reset --hard {baseline} failed: {out}")
    if snapshot:
        # Restore each preserve dir's pre-reset content from the snapshot tree. A
        # path with no tracked content in the snapshot makes `git checkout` exit
        # non-zero ("pathspec did not match") — benign (a preserve dir holding
        # only untracked files). Any other failure means a protected path wasn't
        # restored: raise instead of silently dropping a resolved re-drive's
        # corrected spec (which would regress the re-drive into a recovery loop).
        # `_run_git` pins LC_ALL=C, so this English substring is stable under a
        # localized git (#236) — never translated out from under the match.
        for d in preserve:
            rc, out = _git(repo, "checkout", snapshot, "--", d)
            if rc != 0 and "did not match" not in out:
                raise GitError(f"git checkout {snapshot[:12]} -- {d} failed: {out}")
    if policy_content is not None:
        current = policy_path.read_bytes() if policy_path.is_file() else None
        if current != policy_content:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_bytes(policy_content)
    if baseline_untracked is None:
        return  # no snapshot to diff against: never delete untracked files
    created = untracked_files(repo) - set(baseline_untracked)
    repo = repo.resolve()
    keep_roots = [(repo / k).resolve() for k in keep]
    for rel in sorted(created):
        path = (repo / rel).resolve()
        if any(path == root or path.is_relative_to(root) for root in keep_roots):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        _prune_empty_parents(path.parent, repo)


def _prune_empty_parents(start: Path, repo: Path) -> None:
    """Remove now-empty directories from `start` up to (not including) `repo`."""
    d = start.resolve()
    while d != repo and d.is_relative_to(repo):
        try:
            d.rmdir()  # succeeds only when empty
        except OSError:
            break
        d = d.parent


# --------------------------------------------------------------------------
# git worktree / branch / merge / diff primitives (Phase 2)
#
# Low-level helpers for the worktree-isolation pipeline. Each raises GitError
# on failure. No engine wiring yet — these are unit-tested in isolation and
# wired into open/close_unit_workspace + merge-back in Phase 3.
# --------------------------------------------------------------------------


def current_branch(repo: Path) -> str:
    """The branch name HEAD points at, or "HEAD" when detached."""
    rc, out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        raise GitError(f"git rev-parse --abbrev-ref HEAD failed in {repo}: {out}")
    return out


def branch_exists(repo: Path, name: str) -> bool:
    rc, _ = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    return rc == 0


def create_branch(repo: Path, name: str, base: str) -> None:
    """Create branch `name` at `base` without checking it out."""
    rc, out = _git(repo, "branch", name, base)
    if rc != 0:
        raise GitError(f"git branch {name} {base} failed in {repo}: {out}")


def delete_branch(repo: Path, name: str, force: bool = False) -> None:
    rc, out = _git(repo, "branch", "-D" if force else "-d", name)
    if rc != 0:
        raise GitError(f"git branch -d {name} failed in {repo}: {out}")


def worktree_add(
    repo: Path, path: Path, branch: str, base: str | None = None, *, create: bool = True
) -> None:
    """Check `branch` out in a new worktree at `path` (which must not exist).

    create=True (default) cuts a fresh `branch` at `base`, or from HEAD when
    `base` is None (git's own default start-point). create=False mounts an
    existing `branch` (used to re-mount a shared run branch across serial units);
    `base` is ignored. Either way the branch must not already be checked out in
    another worktree — git refuses that.
    """
    if create:
        # `git worktree add -b <branch> <path> [<base>]`: cut the new branch at the
        # caller's start-point, or from HEAD when none is given (git's own default).
        cmd = ["worktree", "add", "-b", branch, str(path)]
        if base is not None:
            cmd.append(base)
        rc, out = _git(repo, *cmd)
    else:
        rc, out = _git(repo, "worktree", "add", str(path), branch)
    if rc != 0:
        raise GitError(f"git worktree add {path} ({branch} from {base}) failed: {out}")


def checkout_branch(repo: Path, name: str) -> None:
    """Switch the repo's checkout to `name`. Requires a clean tree."""
    rc, out = _git(repo, "checkout", name)
    if rc != 0:
        raise GitError(f"git checkout {name} failed in {repo}: {out}")


def checkout_detach(repo: Path) -> None:
    """Detach HEAD at its current commit, leaving working tree + index untouched.

    Frees a shared branch name held by a kept worktree so a sibling worktree can
    check that branch out (git refuses a branch checked out in another worktree).
    """
    rc, out = _git(repo, "checkout", "--detach")
    if rc != 0:
        raise GitError(f"git checkout --detach failed in {repo}: {out}")


def worktree_remove(repo: Path, path: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    rc, out = _git(repo, *args)
    if rc != 0:
        raise GitError(f"git worktree remove {path} failed: {out}")


def worktree_prune(repo: Path) -> None:
    """Drop administrative entries for worktrees whose directories are gone.
    Best-effort housekeeping — never raises. The return code is already ignored,
    but since #156 `_git` can *raise* GitError on a timeout, which would bypass
    this never-raise contract (and the teardown degrade paths that lean on it —
    close_unit_workspace / discard_worktree call prune from inside their own
    GitError guards). `subprocess.run` can also raise OSError outright (a spawn
    failure — EMFILE, ENOMEM — happens before any return code exists), which the
    #156 translation doesn't cover. Swallow both here so the contract holds at
    its source."""
    try:
        _git(repo, "worktree", "prune")
    except (GitError, OSError):
        pass


def worktree_list(repo: Path) -> list[Path]:
    """Paths of every worktree attached to `repo` (the main checkout first)."""
    rc, out = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        raise GitError(f"git worktree list failed in {repo}: {out}")
    paths = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :]))
    return paths


def dirty_paths(repo: Path) -> dict[str, str]:
    """Repo-relative posix path -> two-char porcelain XY status for every dirty
    entry in `repo`'s working tree. Excludes the orchestrator's own working dir
    (.bmad-loop/) — config, ledger, run state, engine plugins — none of which is
    ever a unit's merged content. NUL-delimited (`-z`) so paths with spaces/unicode
    and rename forms parse without C-quoting; for a rename the *destination* path
    (the one now on disk) is what's recorded. `-uall` lists individual untracked
    files (not a collapsed parent dir) so each entry can be matched 1:1 against a
    branch's incoming paths."""
    rc, out = _git_raw(
        repo, "status", "--porcelain", "-z", "-uall", "--", ".", f":(exclude){AUTOMATOR_DIR_REL}"
    )
    if rc != 0:
        raise GitError(f"git status failed in {repo}")
    tokens = out.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        xy, path = tok[:2], tok[3:]
        # rename/copy entries carry the original path as the next NUL field; the
        # destination (`path` above) is what's on disk, so consume and skip it.
        if "R" in xy or "C" in xy:
            i += 1
        result[path] = xy
        i += 1
    return result


def branch_incoming_paths(repo: Path, target: str, branch: str) -> set[str]:
    """The set of repo-relative posix paths a merge of `branch` into `target`
    would introduce or modify (`git diff --name-only target branch`)."""
    rc, out = _git_raw(repo, "diff", "--name-only", "-z", target, branch)
    if rc != 0:
        raise GitError(f"git diff --name-only {target} {branch} failed in {repo}")
    return {p for p in out.split("\0") if p}


def clean_incoming_collisions(repo: Path, target: str, branch: str) -> list[str]:
    """Reconcile a target checkout dirtied by a per-worktree Unity Editor so the
    merge of `branch` can proceed, returning the cleaned paths (empty when the
    tree was already clean).

    Background: with engine `editor_mode = "per_worktree"`, a competing Editor
    can leak asset writes (`.cs.meta` GUIDs, asmdef auto-edits) into the *main*
    checkout. The merge then aborts pre-flight ("local changes / untracked files
    would be overwritten"). Those leaked copies are Editor-generated duplicates of
    content already committed on `branch`, so cleaning them is safe — the merge
    re-creates the canonical versions.

    Guard: only paths that lie within the branch's incoming set are cleaned. Any
    dirty path *outside* that set could be real operator work, so we refuse and
    raise GitError naming the stray paths without touching anything.
    """
    dirty = dirty_paths(repo)
    if not dirty:
        return []
    incoming = branch_incoming_paths(repo, target, branch)
    stray = sorted(p for p in dirty if p not in incoming)
    if stray:
        raise GitError(
            "the target checkout has uncommitted changes outside this branch's "
            f"files (not introduced by the merge): {', '.join(stray)}"
        )
    repo_res = repo.resolve()
    cleaned: list[str] = []
    for path, xy in sorted(dirty.items()):
        if xy.startswith("??"):  # untracked: delete it, then prune emptied dirs
            fp = repo / path
            fp.unlink(missing_ok=True)
            parent = fp.parent
            while parent.resolve() != repo_res and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        else:  # tracked-modified: restore to the target's committed version
            rc, out = _git(repo, "checkout", "--", path)
            if rc != 0:
                raise GitError(f"git checkout -- {path} failed in {repo}: {out}")
        cleaned.append(path)
    return cleaned


def _merge_in_progress(repo: Path) -> bool:
    """True when a merge is mid-flight (MERGE_HEAD exists). A merge git refused at
    pre-flight (e.g. untracked files would be overwritten) leaves no MERGE_HEAD,
    so there is nothing to `--abort`."""
    rc, _ = _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return rc == 0


def _tree_dirty_vs_head(repo: Path) -> bool:
    """True when tracked tree/index differs from HEAD — i.e. a squash actually
    touched things and needs a reset. A pre-flight-refused squash leaves HEAD's
    tree intact, so this stays False and we skip the bogus reset."""
    rc, _ = _git(repo, "diff", "--quiet", "HEAD", "--")
    return rc != 0


def merge_branch(
    repo: Path, branch: str, *, strategy: str = "merge", message: str | None = None
) -> None:
    """Merge `branch` into the branch currently checked out in `repo`.

    strategy: "ff" (fast-forward only), "merge" (always a merge commit), or
    "squash" (collapse to one commit). Raises GitError on conflict or when an
    ff-only merge can't fast-forward, restoring the tree to its pre-merge state.
    Expects the target checkout to be clean; the worktree pipeline reconciles
    Editor-induced dirt first via `clean_incoming_collisions`. When git refuses
    a merge at pre-flight (no MERGE_HEAD created) the tree was never touched, so
    no abort/reset is attempted and the raw git error is raised verbatim.
    """
    if strategy == "ff":
        rc, out = _git(repo, "merge", "--ff-only", branch)
        if rc != 0:
            raise GitError(f"git merge --ff-only {branch} failed in {repo}: {out}")
        return
    if strategy == "merge":
        msg = message or f"Merge branch '{branch}'"
        rc, out = _git(repo, "merge", "--no-ff", "-m", msg, branch)
        if rc != 0:
            detail = f"git merge --no-ff {branch} failed in {repo} (conflict?): {out}"
            if _merge_in_progress(repo):  # only abort a merge that actually started
                abort_rc, abort_out = _git(repo, "merge", "--abort")  # restore pre-merge HEAD
                if abort_rc != 0:
                    detail += f"; AND git merge --abort failed (repo left mid-merge): {abort_out}"
            raise GitError(detail)
        return
    if strategy == "squash":
        rc, out = _git(repo, "merge", "--squash", branch)
        if rc != 0:
            detail = f"git merge --squash {branch} failed in {repo} (conflict?): {out}"
            # squash leaves no MERGE_HEAD; only reset if it actually modified the
            # tree/index (a pre-flight refusal leaves HEAD's tree untouched).
            if _tree_dirty_vs_head(repo):
                reset_rc, reset_out = _git(repo, "reset", "--hard", "HEAD")
                if reset_rc != 0:
                    detail += f"; AND git reset --hard HEAD failed (tree not restored): {reset_out}"
            raise GitError(detail)
        msg = message or f"Squash-merge branch '{branch}'"
        rc, out = _git(repo, "commit", "-m", msg)
        if rc != 0:
            raise GitError(f"git commit (squash {branch}) failed in {repo}: {out}")
        return
    raise GitError(f"unknown merge strategy: {strategy!r}")


def capture_diff(repo: Path, baseline: str, *, max_file_bytes: int | None = None) -> str:
    """Full unified diff of `repo`'s working tree against `baseline`, including
    untracked (but not ignored) files. Used to preserve a failed unit's changes
    for forensics. Returns "" when there is nothing to capture.

    Unlike `_git`, the tracked diff is read from stdout alone and left verbatim
    (no strip, no stderr merge) so the patch stays applyable.

    max_file_bytes caps the size of each *untracked* file included: a file larger
    than the cap is skipped and replaced with a one-line marker naming it and its
    size, so a stray build dir or huge log can't balloon the patch. None lifts the
    cap (capture everything regardless of size).
    """
    proc = _run_git(["git", "-C", str(repo), "diff", baseline, "--"], repo)
    if proc.returncode != 0:
        raise GitError(f"git diff {baseline} failed in {repo}: {proc.stderr.strip()}")
    parts = [proc.stdout]

    rc, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        raise GitError(f"git ls-files --others failed in {repo}: {out}")
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        if max_file_bytes is not None:
            try:
                size = (repo / rel).stat().st_size
            except OSError:
                size = 0
            if size > max_file_bytes:
                parts.append(
                    f"# bmad-loop: skipped untracked file {rel!r} — "
                    f"{size / 1_048_576:.1f} MB exceeds the {max_file_bytes / 1_048_576:.1f} MB "
                    "cap (raise scm.failed_diff_max_mb or set scm.failed_diff_unlimited = true)\n"
                )
                continue
        # --no-index synthesizes an add-from-empty diff for the untracked file;
        # it exits 1 precisely because the files differ — expected here. Any other
        # non-zero code is a real failure (bad path, internal error), not "files
        # differ", so don't silently fold it into the patch.
        u = _run_git(["git", "-C", str(repo), "diff", "--no-index", "--", os.devnull, rel], repo)
        if u.returncode not in (0, 1):
            raise GitError(
                f"git diff --no-index for untracked {rel!r} failed in {repo}: {u.stderr.strip()}"
            )
        parts.append(u.stdout)
    return "".join(parts)


def set_frontmatter_field(path: Path, key: str, value: str) -> bool:
    """Rewrite (or insert) a scalar ``<key>:`` line in a spec's `---`…`---`
    frontmatter block.

    Same minimal in-place line surgery as `set_frontmatter_status` (no YAML
    round-trip) so the spec's formatting, comments, and field order survive.
    Unlike the status helper, a missing key is INSERTED as the block's last
    line: callers assert a field's value whether or not the skill wrote one
    (the patch-restore re-arm re-stamps ``baseline_revision``, which only the
    skill's step-03 writes). Returns True when the file was rewritten; False
    when it has no frontmatter or already carries the exact value.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    split = _split_frontmatter(text)
    if split is None:
        return False
    before, block, after = split
    block_lines = block.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(block_lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}:"):
            indent = line[: len(line) - len(stripped)]
            newline = "\n" if line.endswith("\n") else ""
            block_lines[i] = f"{indent}{key}: {value}{newline}"
            replaced = True
            break
    if not replaced:
        block_lines.append(f"{key}: {value}\n")
    rebuilt = before + "".join(block_lines) + after
    if rebuilt == text:  # already at the target value — idempotent no-op
        return False
    path.write_text(rebuilt, encoding="utf-8")
    return True


def artifact_relpaths(paths: ProjectPaths) -> tuple[str, ...]:
    """Repo-relative posix prefixes of the orchestrator-owned BMAD artifact
    folders (the output root and the implementation/planning artifact dirs),
    relative to ``paths.project``. Folders configured outside the project tree
    are skipped — nothing to exclude there. The same set as
    ``Engine._protected_relpaths``; the dev/bundle proof-of-work gate passes
    these to ``has_changes_since`` so spec-only edits never count as real work."""
    out: list[str] = []
    for folder in (
        paths.output_folder,
        paths.implementation_artifacts,
        paths.planning_artifacts,
    ):
        try:
            rel = folder.relative_to(paths.project).as_posix()
        except ValueError:
            continue  # configured outside the project tree; nothing to exclude here
        # A folder == project root yields ".", which as an exclude prefix would
        # disable change detection for the whole tree — drop it.
        if rel and rel != ".":
            out.append(rel)
    return tuple(out)


def verify_dev_exclude_relpaths(
    paths: ProjectPaths, spec_path: Path, restore_patch: str | None = None
) -> tuple[str, ...]:
    """Repo-relative posix paths the dev/bundle proof-of-work gate excludes from
    `has_changes_since` — file-granularity, unlike `artifact_relpaths`' whole-folder
    exclusion (still used as-is by `Engine._protected_relpaths` for rollback
    protection, a different job). Deliberately does NOT exclude `output_folder`:
    in the standard layout it is the parent directory of `implementation_artifacts`/
    `planning_artifacts`, so excluding it as a directory prefix would swallow those
    two folders' content right back out of view via the same git-pathspec prefix
    match this function exists to avoid.

    Excludes only what a session rewrites regardless of whether it did any real
    work: `paths.sprint_status` (every session advances it as routine bookkeeping)
    and the session's own claimed `spec_path` (so a bare frontmatter status flip on
    it doesn't count). Sibling content under the implementation/planning artifact
    dirs — the deferred-work ledger, other stories' specs — is deliberately left
    un-excluded, so a story whose entire authorized scope is ledger/spec
    reconciliation registers as real work instead of a permanent false "no changes
    since baseline".

    `restore_patch` (the task's latched intent-gap patch file, BMAD-METHOD #2564)
    is excluded too when set: the patch is untracked halt residue under the
    protected artifact dirs that survives every reset, so counting it would let a
    restore re-drive whose session produced nothing pass the gate on the patch
    file's mere presence — the gate must key on the APPLIED work (the tracked diff
    from baseline), not on the orchestrator-owned patch that carried it.

    `spec_path` comes from a session-reported (untrusted) `spec_file` string, so
    it is `.resolve()`d before deriving the relpath, same as `spec_within_roots`:
    an un-normalized `..`/`.` segment would still resolve to the real on-disk
    file (the OS resolves it), but as a raw string it wouldn't match git's own
    normalized path output, silently defeating this exclude and letting a bare
    status flip on the session's own spec count as real work."""
    candidates: list[Path] = [paths.sprint_status, spec_path]
    if restore_patch:
        candidates.append(resolve_restore_path(restore_patch, paths.project))
    out: list[str] = []
    project = paths.project.resolve()
    for path in candidates:
        try:
            rel = path.resolve().relative_to(project).as_posix()
        except ValueError:
            continue  # outside the project tree; nothing to exclude here
        if rel and rel != ".":
            out.append(rel)
    return tuple(out)


def spec_within_roots(spec_path: Path, paths: ProjectPaths) -> bool:
    """True if ``spec_path`` is, or sits under, an orchestrator-owned root (the
    project root or an artifact dir). A mutating repair (the frontmatter-status
    reconcile) must refuse a session-reported ``spec_file`` that resolves outside
    these roots, so a surprising path can never be silently rewritten. Artifact
    dirs configured outside ``project`` are roots too, so a legitimately
    out-of-project spec is still allowed."""
    sp = spec_path.resolve()
    roots = (
        paths.project,
        paths.output_folder,
        paths.implementation_artifacts,
        paths.planning_artifacts,
    )
    return any(sp == r.resolve() or sp.is_relative_to(r.resolve()) for r in roots)


def resolve_spec_path(spec_file: str, paths: ProjectPaths) -> Path:
    p = Path(spec_file)
    if p.is_absolute():
        return p
    candidate = paths.project / p
    if candidate.is_file():
        return candidate
    return paths.implementation_artifacts / p


def _gate_frontmatter(spec_path: Path) -> dict[str, Any] | VerifyOutcome:
    """Read a spec's frontmatter for a verify gate, degrading an unreadable spec
    to a retryable :class:`VerifyOutcome` instead of a whole-run crash.

    Every verify gate reads the spec back while the dev skill may still be
    rewriting it, so an OSError here (a TOCTOU truncation, a transient lock, a
    momentarily unsearchable parent) is a fault with a *designed* transient
    producer — not a broken orchestrator. `read_frontmatter` itself keeps raising
    (repair callers depend on that); only these observation gates degrade.

    The reason is deliberately distinct from a status mismatch: returning ``{}``
    here would read as status ``""`` and let a read fault masquerade as "the
    skill forgot to set the status", sending a repair session after a bug that
    is not there. Retries are not silent — the reason lands in the journal via
    `dev-decision` / `review-verify-failed`, and a persistent fault is bounded
    into DEFER (or PAUSE) by `escalation.decide_dev` / `decide_review_session`.
    """
    try:
        return read_frontmatter(spec_path)
    except OSError as e:
        return VerifyOutcome.retry(f"spec unreadable ({e.__class__.__name__}: {e}): {spec_path}")


def _verify_shared_gates(
    spec_path: Path,
    rj: dict[str, Any],
    task: StoryTask,
    paths: ProjectPaths,
    *,
    expected_status: str,
    extra_exclude: tuple[str, ...] | None,
    allow_ancestor_baseline: bool = False,
) -> VerifyOutcome | None:
    """The workflow-tag, expected-status, baseline-match, and proof-of-work gates
    shared verbatim by :func:`verify_dev`, :func:`verify_dev_bundle`, and
    :func:`verify_dev_stories` — factored out so the sprint-mode and stories-mode
    gates can't silently drift. Reads frontmatter once (callers must not re-read
    it). Returns a failing :class:`VerifyOutcome`, or ``None`` when every gate
    passes and the caller may run its mode-specific tail.

    The proof-of-work exclude is derived here from the `task` this gate already
    receives (`verify_dev_exclude_relpaths`, which needs the latched restore patch);
    ``extra_exclude`` carries only what a mode adds on top — ``()`` for sprint and
    bundle, the story record + manifest for stories. Threading the restore patch in
    from three call sites instead left a default-None foot-gun for a future fourth
    mode, which would silently let a restore re-drive pass proof-of-work on the
    patch file's mere presence. ``extra_exclude=None`` still skips the gate outright
    (a plan-halt leg produced only its own spec)."""
    workflow = rj.get("workflow")
    if workflow != DEV_WORKFLOW:
        return VerifyOutcome.retry(
            f"dev result.json workflow is {workflow!r}, expected {DEV_WORKFLOW!r}"
        )

    fm = _gate_frontmatter(spec_path)
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != expected_status:
        return VerifyOutcome.retry(
            f"spec status is {status!r}, expected {expected_status!r}: {spec_path}"
        )

    # The generic bmad-dev-auto skill stamps `baseline_revision`, never
    # `baseline_commit` — that name exists only in the result.json devcontract
    # synthesizes, which this gate does not consult (it re-reads frontmatter).
    # An absent key skips the check below, so reading `baseline_commit` alone
    # made this gate dead code for every generic-skill session. Read both, the
    # same idiom as `devcontract.synthesize_result`.
    claimed_baseline = str(fm.get("baseline_commit", fm.get("baseline_revision", ""))).strip()
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        if not same_commit(claimed_baseline, task.baseline_commit):
            # A deferred-work bundle may legitimately adopt a pre-existing story
            # spec: bmad-dev-auto routes a "follow-up review of story X" bundle
            # into that story's done spec, whose baseline_revision is the
            # story's original dev baseline — necessarily older than the unit
            # worktree cut for the bundle (#161). An *ancestor* baseline means
            # the session diffed from an earlier commit on the unit's own
            # history (a superset of the unit's changes), which is sound; a
            # diverged or unknown baseline still fails.
            if not (
                allow_ancestor_baseline
                and is_ancestor(paths.project, claimed_baseline, task.baseline_commit)
            ):
                return VerifyOutcome.retry(
                    f"spec baseline {claimed_baseline[:12]} does not match "
                    f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
                )

    if extra_exclude is not None and task.baseline_commit:
        exclude = verify_dev_exclude_relpaths(paths, spec_path, task.restore_patch) + extra_exclude
        try:
            if not has_changes_since(
                paths.project,
                task.baseline_commit,
                exclude=exclude,
                baseline_untracked=task.baseline_untracked,
            ):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

    return None


def verify_dev(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
) -> VerifyOutcome:
    """Verify a dev session's on-disk artifacts against its result.json claims.

    Checks the claimed spec exists, carries the fixed ``auto-dev`` workflow tag,
    sits at the expected status (``in-review`` when a separate review session
    follows, ``done`` when review is disabled), records a baseline matching the
    orchestrator's, has produced changes since that baseline, and that the
    story's sprint-status was advanced to the matching stage. Returns a retryable
    VerifyOutcome on any mismatch, escalates on git failure, passes otherwise.
    """
    rj = result_json or {}
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # With review disabled, the dev session runs its own internal review and
    # finalizes straight to done; otherwise it hands off at in-review.
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status="in-review" if review_enabled else "done",
        extra_exclude=(),
    )
    if gate is not None:
        return gate

    expected_sprint = "review" if review_enabled else "done"
    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != expected_sprint:
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected {expected_sprint!r}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


def verify_dev_bundle(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
) -> VerifyOutcome:
    """verify_dev for a deferred-work bundle: bundles have no sprint-status
    entry. The orchestrator owns the bundle→dw-id binding (``task.dw_ids``,
    marked done by ``SweepEngine``'s ledger sync); the generic ``bmad-dev-auto``
    primitive never authors dw ids. So the dw_ids cross-check is enforced only
    when the session actually claims them — an empty/absent claim is the normal
    generic path and passes."""
    rj = result_json or {}
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # With review disabled, the dev session finalizes the bundle straight to done.
    # allow_ancestor_baseline: a bundle that adopts a pre-existing story spec
    # (follow-up review) carries that spec's older-but-ancestral baseline (#161).
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status="in-review" if review_enabled else "done",
        extra_exclude=(),
        allow_ancestor_baseline=True,
    )
    if gate is not None:
        return gate

    claimed_ids = {str(i) for i in (rj.get("dw_ids") or [])}
    if claimed_ids and claimed_ids != set(task.dw_ids):
        return VerifyOutcome.retry(
            f"result.json dw_ids {sorted(claimed_ids)} do not match the bundle's "
            f"{sorted(task.dw_ids)}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


# A spec_checkpoint story's plan-halt leg leaves the spec at this status (the
# skill HALTs after the Ready-for-Development gate); mirrors
# devcontract.PLAN_HALT_STATUS, kept literal here to avoid a verify<-devcontract
# import cycle (devcontract imports verify).
PLAN_HALT_STATUS = "ready-for-dev"


def verify_dev_stories(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    *,
    spec_folder: Path,
    review_enabled: bool = True,
    plan_halt: bool = False,
) -> VerifyOutcome:
    """verify_dev for stories mode: the story spec lives at the id-keyed path
    ``<spec-folder>/stories/<id>-<slug>.md`` and there is no sprint-status entry.

    Same gates as :func:`verify_dev` — workflow tag, expected frontmatter status,
    baseline match, proof-of-work since baseline — with two differences: the spec
    is resolved **deterministically by id** (``task.story_key``) via
    ``stories.resolve_story_spec`` rather than trusting the session-claimed path,
    and the sprint-status gate is dropped (stories mode has no sprint board).
    A resolution that is pending / ambiguous / a sentinel is a retryable failure,
    and the resolved filename's id prefix is asserted to equal the task id.

    ``plan_halt`` verifies a spec_checkpoint story's plan-halt leg instead of an
    implementation: the expected status is ``ready-for-dev`` (the plan is done,
    not the code) and the proof-of-work gate is skipped — a plan writes only its
    own spec, which proof-of-work already excludes, so requiring code changes
    would spuriously fail every plan leg. The spec-resolution, id-prefix, workflow,
    and baseline gates still run, and ``task.spec_file`` is still recorded. A
    ``plan_halt`` leg also requires the ``result_json`` to carry the ``plan_halt``
    marker ``devcontract`` emits on a clean plan-halt, so a died-mid-flight
    ``ready-for-dev`` can't be mistaken for a successful plan.
    """
    # Deferred to avoid a verify<->stories import cycle: stories imports
    # read_frontmatter/status_of from this module at top level, so verify must not
    # import stories at module scope (keep this local on any future refactor).
    from . import stories

    rj = result_json or {}
    story_id = str(task.story_key).strip()
    state = stories.resolve_story_spec(spec_folder, story_id)
    if state.kind == stories.KIND_PENDING:
        return VerifyOutcome.retry(f"no story spec found for id {story_id!r} under {spec_folder}")
    if state.kind == stories.KIND_AMBIGUOUS:
        names = ", ".join(p.name for p in state.paths)
        return VerifyOutcome.retry(f"ambiguous story file match for id {story_id!r}: {names}")
    if state.kind == stories.KIND_SENTINEL:
        return VerifyOutcome.retry(
            f"story {story_id!r} resolved to a {state.sentinel_kind} sentinel: {state.path}"
        )
    spec_path = state.path
    # The glob is `<id>-*.md`, so this holds by construction — assert it anyway as
    # a defensive gate against a future resolver change silently widening the match.
    if spec_path is None or not spec_path.name.startswith(f"{story_id}-"):
        return VerifyOutcome.retry(
            f"resolved story spec {spec_path} does not match id {story_id!r}"
        )
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # Generic path always self-finalizes to done (no in-review handoff); the
    # review_enabled arm mirrors verify_dev for symmetry. A plan-halt leg instead
    # expects the ready-for-dev plan gate (the plan is done, not the code).
    if plan_halt:
        # A clean plan-halt also carries devcontract's plan_halt marker; a
        # died-mid-flight ready-for-dev (synthesized without plan_halt) never
        # does. Cross-check the verify-side flag against the synth-side result so
        # a caller can't unilaterally promote a mid-flight spec to a "successful
        # plan" — mirrors the defensive id-prefix gate above.
        if rj.get("plan_halt") is not True:
            return VerifyOutcome.retry(
                "plan_halt verification requested but result.json carries no plan_halt marker"
            )
        expected = PLAN_HALT_STATUS
    else:
        expected = "in-review" if review_enabled else "done"

    # A plan-halt leg produced only its own spec (the plan), which proof-of-work
    # already excludes; skip it (extra_exclude=None) and record the plan spec.
    # Otherwise stories mode adds the spec folder's stories/ subdir + stories.yaml
    # on top of the gate's own file-granular exclude — NOT the whole-folder
    # artifact_relpaths, so a story whose entire authorized scope is ledger/spec
    # reconciliation doesn't register as a false "no changes".
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status=expected,
        extra_exclude=(None if plan_halt else _stories_relpaths(paths.project, spec_folder)),
    )
    if gate is not None:
        return gate

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


def _stories_relpaths(project: Path, spec_folder: Path) -> tuple[str, ...]:
    """Proof-of-work exclude prefixes for the story record + manifest: the spec
    folder's ``stories/`` subdir and its ``stories.yaml``, project-relative. Empty
    when the spec folder is outside the project tree (nothing to exclude there)."""
    from .stories import STORIES_FILENAME, STORIES_SUBDIR

    try:
        rel = spec_folder.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return ()
    base = "" if rel == "." else f"{rel}/"
    return (f"{base}{STORIES_SUBDIR}", f"{base}{STORIES_FILENAME}")


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    output_tail: str


# sh launcher convention (verify commands run shell=True): 126 = command found
# but not executable, 127 = command not found. Both are environment faults —
# deterministic for a given tree, unfixable by a repair session (issue #126:
# seeded worktrees that lost +x burned dev attempts on no-op repairs). Windows
# cmd signals these as 9009/1 instead, so it keeps the charged behavior.
ENV_FAULT_RCS = frozenset({126, 127})


def run_verify_commands(policy: Policy, cwd: Path) -> list[CommandResult]:
    results = []
    for command in policy.verify.commands:
        try:
            # Verify commands are operator-authored shell strings from the project's
            # policy (e.g. "pytest -q && ruff check"); shell=True is intentional here.
            proc = subprocess.run(  # nosec B602
                command,
                shell=True,  # portability: operator-authored verify command — sanctioned shell-out (see plan out-of-scope)
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
            )
            output = (proc.stdout + proc.stderr)[-2000:]
            results.append(CommandResult(command, proc.returncode, output))
        except subprocess.TimeoutExpired:
            results.append(CommandResult(command, -1, "timed out"))
    return results


def verify_commands_outcome(policy: Policy, cwd: Path) -> VerifyOutcome:
    """Run the policy's deterministic verify commands. Failures are fixable:
    the captured output is concrete feedback a repair session can act on —
    except environment faults (ENV_FAULT_RCS), which escalate so the run
    pauses for an environment fix instead of burning story budgets. An env
    fault anywhere in the run wins over earlier ordinary failures: a repair
    session dispatched for the ordinary failure would still run in the
    broken environment."""
    results = run_verify_commands(policy, cwd)
    for result in results:
        if result.returncode in ENV_FAULT_RCS:
            return VerifyOutcome.escalate(
                f"verify environment fault (rc={result.returncode}): {result.command}\n"
                "command not found / not executable — this is the run environment, "
                "not the story; fix the environment, then re-arm the escalation "
                "(the attempt budget resets on re-arm)\n"
                f"{result.output_tail}",
                env_fault=True,
            )
    for result in results:
        if result.returncode != 0:
            return VerifyOutcome.retry(
                f"verify command failed (rc={result.returncode}): {result.command}\n"
                f"{result.output_tail}",
                fixable=True,
            )
    return VerifyOutcome.passed()


def verify_review(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != "done":
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected 'done'"
        )

    return verify_commands_outcome(policy, paths.project)


def verify_review_stories(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    """verify_review for stories mode: same spec-done + verify-commands gates,
    minus the sprint-status gate (stories mode has no sprint board — the story
    spec's own frontmatter status is authoritative). ``task.spec_file`` is the
    id-keyed story spec ``verify_dev_stories`` recorded on the dev pass."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")
    return verify_commands_outcome(policy, paths.project)


def verify_review_bundle(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    """verify_review for a deferred-work bundle: no sprint-status check, but
    every dw id the bundle owns must be marked done in the ledger on disk. The
    legacy --dw-bundle skill flips them; on the generic bmad-dev-auto path the
    orchestrator flips them after dev and, if review rewrites the ledger diff,
    again immediately before this review gate. Either way this gate is why we
    can trust it happened."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    ledger = paths.deferred_work
    # Same TOCTOU class as the spec read above: the ledger is rewritten by the
    # orchestrator's own mark_done between the dev and review gates.
    try:
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    except OSError as exc:
        return VerifyOutcome.retry(
            f"deferred-work ledger unreadable ({exc.__class__.__name__}: {exc}): {ledger}"
        )
    entries = {e.id: e for e in deferredwork.parse_ledger(text)}
    not_done = sorted(
        i for i in task.dw_ids if i not in entries or not entries[i].status.startswith("done")
    )
    if not_done:
        return VerifyOutcome.retry(
            "deferred-work entries not marked done in "
            f"{ledger}: {', '.join(not_done)} — set each to `status: done <date>` "
            "with a `resolution:` line",
            fixable=True,
        )

    return verify_commands_outcome(policy, paths.project)


def commit_story(repo: Path, message: str) -> str:
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


def finalize_commit(repo: Path, baseline: str | None, message: str) -> str | None:
    """Collapse everything since `baseline` into ONE commit with `message`.

    bmad-dev-auto now commits its own work at the end of each iteration (one
    commit for the dev pass, one for each follow-up review pass), while the
    orchestrator still writes its own bookkeeping (sprint-status.yaml for
    stories, the deferred-work ledger for sweep bundles) into the working tree
    uncommitted. This squashes that whole chain — the skill's per-iteration
    commits PLUS the orchestrator's uncommitted writes — back onto `baseline`
    as a single commit carrying the orchestrator's message, so the one-commit-
    per-story invariant and the message template / pre_commit hook stay
    authoritative regardless of how many times the skill committed.

    Mechanics: stage the working tree (`add -A`), move HEAD back to `baseline`
    keeping the index (`reset --soft`), then commit the accumulated index. The
    working tree is never touched, so a failure leaves the chain intact.

    Residual-artifacts note (BMAD-METHOD #2563): the skill now commits every file
    of the reviewed diff and deliberately leaves unrelated `git status` residue
    uncommitted (files outside the change's scope). The `add -A` here sweeps that
    residue into the story commit too — an intentional divergence from the skill's
    scoped commit. The loop must end each story on a clean tree because story
    N+1's step-01 HALTs on a dirty tree, so the orchestrator squashes EVERYTHING
    since baseline (skill commits + its own bookkeeping + any residue) into the one
    story commit rather than leaving the tree dirty for the next story to trip on.

    Returns the new HEAD sha, or None when there is nothing to finalize: no
    version control (`baseline` falsy or NO_VCS) or the tree already equals
    `baseline` (no skill commits and no bookkeeping delta)."""
    if not baseline or baseline == "NO_VCS":
        return None
    original_head = rev_parse_head(repo)
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "reset", "--soft", baseline)
    if rc != 0:
        raise GitError(f"git reset --soft {baseline} failed: {out}")
    # index now holds the cumulative diff vs baseline; nothing staged → no-op
    rc, _ = _git(repo, "diff", "--cached", "--quiet")
    if rc == 0:
        return None
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        # The soft reset already rewound HEAD to baseline; a failed commit would
        # otherwise leave the branch pointer there, dropping the skill commit chain
        # from HEAD. Restore HEAD (the working tree is untouched) before raising.
        restore_rc, restore_out = _git(repo, "reset", "--soft", original_head)
        if restore_rc != 0:
            raise GitError(
                f"git commit failed: {out}; additionally failed to restore HEAD "
                f"to {original_head[:12]}: {restore_out}"
            )
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


def resolve_restore_path(raw: str, root: Path) -> Path:
    """The latched intent-gap patch (`StoryTask.restore_patch`) as a concrete path:
    absolute values pass through, relative ones are anchored on `root`.

    `model.StoryTask.restore_patch` documents the field as repo-relative-or-absolute,
    and every consumer must resolve it against the base it actually reads the tree
    from — the engine's live workspace root (the unit worktree under isolation),
    `paths.project` for the proof-of-work exclude, the CLI's `--project`. Hence the
    caller-supplied `root` rather than one baked-in base.

    In practice `cli._resolve_restore_patch` always latches an already-`.resolve()`d
    absolute path, so the relative branch is exercised only by a hand-written state
    file or a future non-CLI latcher; it is kept because the field's contract
    promises it. Deliberately does NOT `.resolve()` the result — callers that need
    symlink/`..` normalization (path containment checks) do it themselves, and the
    apply/exclude paths match the pre-existing behavior byte-for-byte without it.
    """
    p = Path(raw)
    return p if p.is_absolute() else root / p


def apply_patch(repo: Path, patch_path: Path) -> None:
    """Apply a saved patch to `repo`'s working tree (`git apply`), raising on failure.

    The intent-gap patch-restore re-drive (BMAD-METHOD #2564) uses this to re-lay
    the attempted change bmad-dev-auto saved before reverting. New files in the
    patch are created (they land untracked, matching how the original attempt sat
    before its revert).

    A clean apply is likely but NOT guaranteed: the patch was diffed from the
    story's ORIGINAL baseline, while re-arm advances the re-drive's baseline to the
    project's post-resolve HEAD (runs.rearm_escalation) — so the apply holds only
    while the resolve session left the patched files untouched. A resolve session
    that committed changes to those files makes `git apply` fail, deliberately
    loudly: silently merging the human's resolution with the stale attempt could
    reproduce the very gap being resolved. A non-zero `git apply` — that overlap, a
    missing/corrupt patch, any other drift — raises `GitError` with git's output;
    the caller escalates rather than dispatch a session onto a half-applied tree,
    and the human re-resolves (typically re-arming without a restore, since the
    resolution commits already carry the overlapping work).
    """
    if not patch_path.is_file():
        raise GitError(f"restore patch not found: {patch_path}")
    rc, out = _git(repo, "apply", str(patch_path))
    if rc != 0:
        raise GitError(f"git apply {patch_path} failed: {out}")


def patch_new_files(patch_path: Path) -> set[str]:
    """Repo-relative posix paths the saved patch *creates* — the untracked residue
    an `apply_patch` leaves behind (see `runs.rearm_escalation`).

    Text-parse, not `git apply --numstat`: the caller runs after the tree has moved
    on, so the patch may no longer apply, and a creation list must still come back.
    Within each `diff --git` block, an old-side `---` header naming `_DIFF_ABSENT`
    marks a creation, and the `+++ <prefix>/<path>` after it names the file. The
    prefix is stripped by mirroring what `apply_patch`'s plain `git apply` (default
    -p1) did when it laid the residue down: drop the first path component whatever
    it is — `b/` standard, `w/`/`i/`/`c/` under diff.mnemonicPrefix, `2/` from
    --no-index. A target -p1 cannot strip (no `/`, e.g. --no-prefix output) is
    skipped: that apply failed outright, so no residue exists. Deletions (the
    absent token on the *new* side) are never returned — the caller feeds this to an
    *exclusion* set, and excluding a path the human later re-created would make the
    next rollback delete their file. For the same reason every ambiguous entry is
    skipped rather than guessed: quoted paths (`+++ "b/wéird"`, core.quotePath),
    renames, and non-`git diff` unified diffs with no `diff --git` header yield fewer
    results, never wrong ones. Under-reporting degrades to the pre-#90 behavior;
    over-reporting deletes user data.

    Raises OSError / UnicodeDecodeError when the patch cannot be read; the caller
    decides (rearm treats it as best-effort and journals `stale-restore-unparseable`).
    """
    new_files: set[str] = set()
    in_hunk = False  # past the first `@@`, a `--- x` line is content, not a header
    creating = False
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git "):
            in_hunk = creating = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            continue
        elif line.startswith("--- "):
            creating = line[4:].strip() == _DIFF_ABSENT
        elif line.startswith("+++ ") and creating:
            creating = False
            target = line[4:].split("\t", 1)[0].strip()
            if target == _DIFF_ABSENT or target.startswith('"') or "/" not in target:
                continue  # delete-then-create pair, quoted path, or un-strippable target
            rel = target.split("/", 1)[1]  # mirror `git apply`'s default -p1
            if rel:
                new_files.add(rel)
    return new_files


def stage_path(repo: Path, path: Path) -> bool:
    """Stage one path so the index matches what is on disk. Returns False when the
    path is outside `repo` or git refused; never raises.

    For repairing an index a failed commit left behind. `finalize_commit` stages
    with `add -A` *before* the commit a native hook can still reject, so undoing
    that commit's bookkeeping in the working tree alone leaves the index holding
    the version that was rolled back — which an operator who fixes the hook and
    runs `git commit` by hand would then publish. Advisory by design: this runs on
    a path that is already escalating a commit failure, and a repair that raises
    would mask it — so "never raises" has to hold against every step, not just the
    containment check that motivated the guard.

    Both halves can fail in ways that are not `ValueError`. `resolve()` raises
    `RuntimeError` on a symlink loop under Python 3.11 and 3.12 (3.13 returns the
    unresolved path instead, so the exposure is version-dependent and the floor is
    the affected one), and `OSError` when a path component is inaccessible. `_git`
    raises `GitError` on a timeout and lets a raw `OSError` from the spawn itself
    through — `_run_git` translates only `TimeoutExpired`. Any of the three would
    replace the commit escalation being reported with a generic crash
    (#284 round-7 review, finding 4)."""
    try:
        rel = str(Path(path).resolve().relative_to(repo.resolve()))
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        rc, _ = _git(repo, "add", "--", rel)
    except (GitError, OSError):
        return False
    return rc == 0


def commit_paths(repo: Path, message: str, paths: list[Path]) -> str | None:
    """Commit exactly `paths` (and nothing else), leaving any unrelated working
    or staged changes untouched. Unlike commit_story's `add -A`, this is safe to
    call out of band (e.g. `bmad-loop decisions`) when the tree may hold the
    user's own uncommitted work. Returns the new HEAD sha, or None when the
    given paths had no changes to commit. Paths outside the repo are ignored."""
    rels: list[str] = []
    repo_root = repo.resolve()
    for p in paths:
        try:
            rels.append(str(Path(p).resolve().relative_to(repo_root)))
        except ValueError:
            continue
    if not rels:
        return None
    rc, out = _git(repo, "add", "--", *rels)
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "status", "--porcelain", "--", *rels)
    if rc != 0:
        raise GitError(f"git status failed: {out}")
    if not out:
        return None  # nothing changed in these paths
    # pathspec form commits only `rels`, ignoring any other staged changes
    rc, out = _git(repo, "commit", "-m", message, "--", *rels)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)
