"""Deterministic post-session verification. Never trust LLM self-reports.

verify_dev / verify_review check artifacts on disk and git state against
what the session's result.json claims; run_verify_commands executes the
policy's test/lint gates with the orchestrator's own subprocess calls.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import deferredwork
from .bmadconfig import ProjectPaths
from .model import StoryTask
from .policy import POLICY_FILE, Policy
from .sprintstatus import story_status

GIT_TIMEOUT_S = 120
COMMAND_TIMEOUT_S = 30 * 60

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


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    reason: str = ""
    severity: str = ""  # "" | "CRITICAL" | "PREFERENCE" — set when not retryable
    # fixable failures carry concrete evidence (failing command output) that a
    # feedback-driven repair session can act on; non-fixable retries start over
    fixable: bool = False

    @classmethod
    def passed(cls) -> "VerifyOutcome":
        return cls(ok=True)

    @classmethod
    def retry(cls, reason: str, fixable: bool = False) -> "VerifyOutcome":
        return cls(ok=False, reason=reason, fixable=fixable)

    @classmethod
    def escalate(cls, reason: str, severity: str = "CRITICAL") -> "VerifyOutcome":
        return cls(ok=False, reason=reason, severity=severity)

    @property
    def retryable(self) -> bool:
        return not self.ok and not self.severity


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git_raw(repo: Path, *args: str) -> tuple[int, str]:
    """Like `_git` but returns stdout verbatim (no strip, no stderr merge) — for
    NUL-delimited (`-z`) output whose records can begin with a space (porcelain
    status codes like ' M'), which `_git`'s strip() would corrupt."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    return proc.returncode, proc.stdout


def _git_env(repo: Path, *args: str, env: dict[str, str]) -> tuple[int, str]:
    """Like `_git` but runs with an explicit environment — used to point git at a
    throwaway `GIT_INDEX_FILE` so a snapshot can stage the tree without touching
    the real index."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        env=env,
    )
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


def has_changes_since(repo: Path, baseline: str, exclude: tuple[str, ...] = ()) -> bool:
    """True if tracked changes since baseline OR untracked files exist.

    `exclude` is repo-relative posix dir prefixes whose changes don't count —
    used by the dev/bundle proof-of-work gate to ignore the orchestrator-owned
    BMAD artifact folders (see `artifact_relpaths`), so a session that only
    rewrites its own spec (e.g. the frontmatter-status reconcile) under those
    folders doesn't register as real implementation work. Mirrors
    `attempt_dirty`'s exclusion. Default `()` keeps the unscoped behavior."""
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", ".", *_exclude_specs(exclude))
    if rc != 0:
        return True
    created = untracked_files(repo)
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
    """Partial :func:`prune_preserve_refs` failure. The prune is per-ref
    best-effort, so refs may already be gone when a later one sticks — the
    ``deleted`` list keeps that destructive half structurally auditable (a
    caller can journal it, not just grep the message) and ``failed`` names
    each stuck ref with its git detail."""

    def __init__(self, message: str, *, deleted: list[str], failed: list[str]) -> None:
        super().__init__(message)
        self.deleted = deleted
        self.failed = failed


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
    worktree). One stuck ref must not wedge the retention for everything
    behind it, so deletes are per-ref best-effort and the error carries both
    what was deleted and what was not."""
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
        # make :short emit "heads/attempt-preserve/x", which `branch -D` can't use
        "--format=%(refname)",
        "refs/heads/attempt-preserve/",
    )
    if rc != 0:
        raise GitError(f"git for-each-ref attempt-preserve failed in {repo}: {out}")
    refs = [line.removeprefix("refs/heads/") for line in out.splitlines() if line]
    deleted: list[str] = []
    failed: list[str] = []
    for name in refs[keep:]:
        try:
            delete_branch(repo, name, force=True)
        except GitError as exc:
            failed.append(f"{name} ({exc})")
            continue
        deleted.append(name)
    if failed:
        raise PrunePreserveError(
            f"attempt-preserve prune in {repo}: deleted {deleted or 'nothing'}, "
            f"could not delete {'; '.join(failed)}",
            deleted=deleted,
            failed=failed,
        )
    return deleted


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

    create=True (default) cuts a fresh `branch` at `base`. create=False mounts an
    existing `branch` (used to re-mount a shared run branch across serial units);
    `base` is ignored. Either way the branch must not already be checked out in
    another worktree — git refuses that.
    """
    if create:
        rc, out = _git(repo, "worktree", "add", "-b", branch, str(path), base)
    else:
        rc, out = _git(repo, "worktree", "add", str(path), branch)
    if rc != 0:
        raise GitError(f"git worktree add {path} ({branch} from {base}) failed: {out}")


def checkout_branch(repo: Path, name: str) -> None:
    """Switch the repo's checkout to `name`. Requires a clean tree."""
    rc, out = _git(repo, "checkout", name)
    if rc != 0:
        raise GitError(f"git checkout {name} failed in {repo}: {out}")


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
    Best-effort housekeeping — never raises."""
    _git(repo, "worktree", "prune")


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
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", baseline, "--"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
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
        u = subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-index", "--", os.devnull, rel],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if u.returncode not in (0, 1):
            raise GitError(
                f"git diff --no-index for untracked {rel!r} failed in {repo}: {u.stderr.strip()}"
            )
        parts.append(u.stdout)
    return "".join(parts)


def read_frontmatter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        doc = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def status_of(fm: dict[str, Any]) -> str:
    """Normalized spec status from a frontmatter dict: stripped + lowercased.

    The single point all spec-frontmatter status gates read through, so casing
    never decides a gate — the spec template and sprint-status tokens are
    lowercase, so a stray ``Done``/``In-Review`` from a hand-edited spec still
    matches. (``devcontract`` keeps its own lowercasing; it parses skill-written
    prose where casing genuinely varies.)
    """
    return str(fm.get("status", "")).strip().lower()


def set_frontmatter_status(path: Path, status: str) -> bool:
    """Rewrite the `status:` field in a spec's `---`…`---` frontmatter block.

    A minimal in-place line replacement (not a YAML round-trip) so the spec's
    formatting, comments, and field order survive — only the status value
    changes. Returns True when the file was rewritten, False when it has no
    frontmatter or already carries `status`. Idempotent.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    block_lines = parts[1].splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(block_lines):
        stripped = line.lstrip()
        if stripped.startswith("status:") and not stripped.startswith("status_"):
            indent = line[: len(line) - len(stripped)]
            newline = "\n" if line.endswith("\n") else ""
            block_lines[i] = f"{indent}status: {status}{newline}"
            replaced = True
            break
    if not replaced:
        return False
    rebuilt = parts[0] + "---" + "".join(block_lines) + "---" + parts[2]
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

    workflow = rj.get("workflow")
    if workflow != DEV_WORKFLOW:
        return VerifyOutcome.retry(
            f"dev result.json workflow is {workflow!r}, expected {DEV_WORKFLOW!r}"
        )

    # With review disabled, the dev session runs its own internal review and
    # finalizes straight to done; otherwise it hands off at in-review.
    expected = "in-review" if review_enabled else "done"
    fm = read_frontmatter(spec_path)
    status = status_of(fm)
    if status != expected:
        return VerifyOutcome.retry(f"spec status is {status!r}, expected {expected!r}: {spec_path}")

    claimed_baseline = str(fm.get("baseline_commit", "")).strip()
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        if not same_commit(claimed_baseline, task.baseline_commit):
            return VerifyOutcome.retry(
                f"spec baseline_commit {claimed_baseline[:12]} does not match "
                f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
            )

    if task.baseline_commit:
        try:
            if not has_changes_since(
                paths.project, task.baseline_commit, exclude=artifact_relpaths(paths)
            ):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

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

    workflow = rj.get("workflow")
    if workflow != DEV_WORKFLOW:
        return VerifyOutcome.retry(
            f"dev result.json workflow is {workflow!r}, expected {DEV_WORKFLOW!r}"
        )

    # With review disabled, the dev session finalizes the bundle straight to done.
    expected = "in-review" if review_enabled else "done"
    fm = read_frontmatter(spec_path)
    status = status_of(fm)
    if status != expected:
        return VerifyOutcome.retry(f"spec status is {status!r}, expected {expected!r}: {spec_path}")

    claimed_baseline = str(fm.get("baseline_commit", "")).strip()
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        if not same_commit(claimed_baseline, task.baseline_commit):
            return VerifyOutcome.retry(
                f"spec baseline_commit {claimed_baseline[:12]} does not match "
                f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
            )

    if task.baseline_commit:
        try:
            if not has_changes_since(
                paths.project, task.baseline_commit, exclude=artifact_relpaths(paths)
            ):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

    claimed_ids = {str(i) for i in (rj.get("dw_ids") or [])}
    if claimed_ids and claimed_ids != set(task.dw_ids):
        return VerifyOutcome.retry(
            f"result.json dw_ids {sorted(claimed_ids)} do not match the bundle's "
            f"{sorted(task.dw_ids)}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    output_tail: str


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
    the captured output is concrete feedback a repair session can act on."""
    for result in run_verify_commands(policy, cwd):
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
    fm = read_frontmatter(Path(task.spec_file))
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != "done":
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected 'done'"
        )

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
    fm = read_frontmatter(Path(task.spec_file))
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    ledger = paths.deferred_work
    text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
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
