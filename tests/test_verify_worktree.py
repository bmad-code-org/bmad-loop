"""Phase 2: low-level git worktree / branch / merge / diff primitives.

Exercised against the conftest `project` sandbox (a real git repo at
`project.project` with `main` checked out and one initial commit). These
helpers carry no engine wiring yet — they are the plumbing Phase 3 builds on.
"""

import subprocess

import pytest
from conftest import git, make_git_noisy, refuse_to_resolve

from bmad_loop import verify


def commit(repo, name, content="x\n", msg="work"):
    (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- branches


def test_current_branch(project):
    assert verify.current_branch(project.project) == "main"


def test_current_branch_reads_stdout_alone_under_host_noise(project):
    """git exits 0 while still writing an advisory to stderr, so against `_git`'s
    stdout+stderr merge the branch name comes back with the warning appended (#442).
    `make_git_noisy` sets an unknown VALUE for a known config KEY, which is exactly
    that shape and not an error path.

    The substring assertion is not implied by the equality: it is what distinguishes
    "the value is clean" from "the oracle is corrupted the same way".

    Ablation target: put `current_branch` back on `_git` (the merge) and this fails
    alone — the two sibling rows in tests/test_verify.py stay green, since each site
    is converted separately."""
    repo = project.project
    warning = make_git_noisy(repo)

    branch = verify.current_branch(repo)

    assert branch == "main"
    assert warning not in branch


def test_branch_exists(project):
    assert verify.branch_exists(project.project, "main")
    assert not verify.branch_exists(project.project, "nope")


def test_create_and_delete_branch(project):
    repo = project.project
    verify.create_branch(repo, "feat", "main")
    assert verify.branch_exists(repo, "feat")
    verify.delete_branch(repo, "feat")
    assert not verify.branch_exists(repo, "feat")


def test_create_branch_duplicate_raises(project):
    with pytest.raises(verify.GitError):
        verify.create_branch(project.project, "main", "main")


# ---------------------------------------------------------------- worktrees


def test_worktree_add_list_remove(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt1"

    verify.worktree_add(repo, wt, "feat", "main")
    assert verify.branch_exists(repo, "feat")
    assert wt.is_dir()
    assert (wt / "src.txt").read_text() == "original\n"  # full checkout

    listed = verify.worktree_list(repo)
    assert repo.resolve() in [p.resolve() for p in listed]
    assert wt.resolve() in [p.resolve() for p in listed]

    verify.worktree_remove(repo, wt)
    assert not wt.exists()
    assert wt.resolve() not in [p.resolve() for p in verify.worktree_list(repo)]


def test_worktree_list_reads_stdout_alone(project, monkeypatch):
    """SEAM axis, deliberately — unlike its `current_branch` neighbour above, this row
    cannot be reddened by the real host noise, and a test that cannot redden is not
    evidence. `make_git_noisy`'s warning does not start with `"worktree "`, so the
    `startswith` filter screens it out and the parse is correct BY ACCIDENT; #442's
    claim that this probe gains "an unparseable extra record" does not hold for that
    shape (measured at git 2.55.0). The synthetic stderr line is chosen to survive the
    filter, which is exactly what the filter cannot promise about every future advisory.

    The filter stays in place as a second, independent screen; this asserts the read
    no longer DEPENDS on it.

    Ablation target: put `worktree_list` back on `_git` (the stdout+stderr merge) and
    this fails alone, on a `/phantom` path appended to the list — the four sibling #442
    rows in tests/test_verify.py stay green, since each site is converted separately."""
    repo = project.project
    real_run = verify.subprocess.run

    def noisy_run(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if not isinstance(proc.stderr, str):  # a binary=True spawn passes through
            return proc
        return verify.subprocess.CompletedProcess(
            proc.args, proc.returncode, proc.stdout, "worktree /phantom\n" + proc.stderr
        )

    monkeypatch.setattr(verify.subprocess, "run", noisy_run)

    assert [p.resolve() for p in verify.worktree_list(repo)] == [repo.resolve()]


def test_worktree_add_create_defaults_to_head(project, tmp_path):
    """create=True with no `base` cuts the branch from HEAD (git's own default)
    instead of passing None into git and crashing."""
    repo = project.project
    head = verify.rev_parse_head(repo)
    wt = tmp_path / "wt-head"

    verify.worktree_add(repo, wt, "feat", create=True)
    assert verify.branch_exists(repo, "feat")
    assert verify.rev_parse_head(wt) == head


def test_worktree_add_existing_path_raises(project, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "occupied").write_text("x")
    with pytest.raises(verify.GitError):
        verify.worktree_add(project.project, wt, "feat", "main")


def test_worktree_remove_dirty_needs_force(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    (wt / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(verify.GitError):
        verify.worktree_remove(repo, wt)  # refuses to drop unsaved work
    verify.worktree_remove(repo, wt, force=True)
    assert not wt.exists()


def test_worktree_prune_swallows_git_error(project, monkeypatch):
    """worktree_prune is best-effort and must never raise — the teardown degrade
    paths (close_unit_workspace / discard_worktree) call it from inside their own
    GitError guards. Since #156 `_git` can *raise* GitError on a timeout, so prune
    must swallow it, not merely ignore the return code (gh-139)."""

    def boom(*a, **k):
        raise verify.GitError("git worktree prune timed out")

    monkeypatch.setattr(verify, "_git", boom)
    verify.worktree_prune(project.project)  # returns without raising


def test_worktree_prune_swallows_os_error(project, monkeypatch):
    """Since #343 a spawn failure arrives typed as GitSpawnError (a GitError),
    but prune's never-raise contract keeps its own plain-OSError net as the belt
    for any untyped fault — its callers invoke it from inside `except GitError`
    guards and lean on it never raising, whatever the cause."""

    def boom(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(verify, "_git", boom)
    verify.worktree_prune(project.project)  # returns without raising


def test_checkout_detach_frees_branch(project, tmp_path):
    """A worktree checked out on a branch holds that branch — git refuses to mount
    it elsewhere. Detaching the worktree's HEAD frees the branch name for a sibling
    worktree while preserving the branch ref, the working tree, and uncommitted
    changes (issue #138)."""
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    (wt / "dirty.txt").write_text("uncommitted\n")  # local edit that must survive

    # while 'feat' is checked out in wt, a sibling mount of it is refused
    wt2 = tmp_path / "wt2"
    with pytest.raises(verify.GitError):
        verify.worktree_add(repo, wt2, "feat", create=False)

    verify.checkout_detach(wt)

    assert verify.current_branch(wt) == "HEAD"  # detached
    assert verify.branch_exists(repo, "feat")  # branch ref preserved
    assert (wt / "dirty.txt").read_text() == "uncommitted\n"  # working tree preserved
    # branch name is now free → the sibling mount succeeds
    verify.worktree_add(repo, wt2, "feat", create=False)
    assert wt2.is_dir()


# ---------------------------------------------------------------- merge


def test_merge_ff(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "new.txt", "hi\n", "feat work")

    verify.merge_branch(repo, "feat", strategy="ff")
    assert (repo / "new.txt").read_text() == "hi\n"
    # fast-forward: no merge commit
    assert git(repo, "log", "--oneline", "--merges") == ""


def test_merge_ff_diverged_raises(project, tmp_path):
    """A diverged target is a pre-flight refusal with nothing to resolve (#619).

    Narrower than it looks, and deliberately so. `--ff-only` declines the TOPOLOGY
    question before touching anything, which is what this row pins; it does NOT
    follow that the flag never touches the tree, and the row further down that
    kills a fast-forward mid-checkout is the counterexample.

    Ablation: put this leg back on a bare `GitError` and this fails alone; the
    conflict rows below stay green."""
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "f.txt", "f\n", "feat work")
    commit(repo, "m.txt", "m\n", "main work")  # main diverges → no ff possible

    with pytest.raises(verify.MergePreflightError):
        verify.merge_branch(repo, "feat", strategy="ff")


def test_merge_no_ff_creates_merge_commit(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "f.txt", "f\n", "feat work")
    commit(repo, "m.txt", "m\n", "main work")

    verify.merge_branch(repo, "feat", strategy="merge")
    assert (repo / "f.txt").exists() and (repo / "m.txt").exists()
    assert git(repo, "log", "--oneline", "--merges") != ""


def test_merge_squash_no_merge_commit(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "f.txt", "f\n", "feat work one")
    commit(wt, "g.txt", "g\n", "feat work two")
    commit(repo, "m.txt", "m\n", "main work")

    verify.merge_branch(repo, "feat", strategy="squash", message="squash feat")
    assert (repo / "f.txt").exists() and (repo / "g.txt").exists()
    assert git(repo, "log", "--oneline", "--merges") == ""  # squash → linear history
    assert "squash feat" in git(repo, "log", "-1", "--pretty=%s")


def test_merge_conflict_raises_and_restores(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "src.txt", "feat change\n", "feat edits src")
    commit(repo, "src.txt", "main change\n", "main edits src")  # same file, conflict

    with pytest.raises(verify.GitError):
        verify.merge_branch(repo, "feat", strategy="merge")
    assert verify.worktree_clean(repo)  # aborted, tree restored
    assert (repo / "src.txt").read_text() == "main change\n"


def test_merge_squash_conflict_restores(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "src.txt", "feat change\n", "feat edits src")
    commit(repo, "src.txt", "main change\n", "main edits src")

    with pytest.raises(verify.GitError):
        verify.merge_branch(repo, "feat", strategy="squash")
    assert verify.worktree_clean(repo)
    assert (repo / "src.txt").read_text() == "main change\n"


def test_merge_unknown_strategy_raises(project):
    with pytest.raises(verify.GitError):
        verify.merge_branch(project.project, "main", strategy="bogus")


def test_merge_preflight_refused_no_abort_tail(project, tmp_path):
    """A merge git refuses at pre-flight (an untracked main-tree file would be
    overwritten by an incoming file) creates no MERGE_HEAD: the error carries the
    raw git text and NOT the misleading 'repo left mid-merge' tail, and leaves no
    merge in progress."""
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "leak.txt", "from branch\n", "feat adds leak.txt")
    # same path appears untracked in the main tree -> git refuses pre-flight
    (repo / "leak.txt").write_text("editor-leaked\n")

    with pytest.raises(verify.GitError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")
    msg = str(ei.value)
    assert "would be overwritten by merge" in msg
    assert "repo left mid-merge" not in msg
    assert not verify._merge_in_progress(repo)  # nothing to abort was ever started


# ------------------------------------------- #619 merge failure taxonomy
#
# `merge_branch` fails for two materially different reasons and used to label
# both a content conflict. These rows pin the split. The helpers below are the
# three pre-flight shapes git refuses on; `_branch_with` (defined further down)
# cuts the `feat` branch each one merges.


def _preflight_untracked_overwrite(repo, tmp_path):
    """The incoming commit adds a path that already sits UNTRACKED in the target."""
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    (repo / "leak.cs").write_text("operator\n")


def _preflight_staged_on_incoming_path(repo, tmp_path):
    """The target holds a STAGED edit to a file the incoming commit rewrites."""
    _branch_with(repo, tmp_path, modifies={"src.txt": "branch\n"})
    (repo / "src.txt").write_text("operator staged\n")
    git(repo, "add", "src.txt")


def _preflight_shape_clash(repo, tmp_path):
    """An untracked FILE stands where the incoming commit needs a DIRECTORY."""
    _branch_with(repo, tmp_path, adds={"Assets/Tests/Leak.cs": "branch\n"})
    (repo / "Assets").write_text("operator\n")


_PREFLIGHT_SHAPES = [
    (_preflight_untracked_overwrite, "untracked-overwrite"),
    (_preflight_staged_on_incoming_path, "staged-on-incoming-path"),
    (_preflight_shape_clash, "shape-clash"),
]


@pytest.mark.parametrize("strategy", ["merge", "squash"])
@pytest.mark.parametrize(
    "setup", [fn for fn, _ in _PREFLIGHT_SHAPES], ids=[name for _, name in _PREFLIGHT_SHAPES]
)
def test_merge_preflight_refusals_raise_merge_preflight_error(project, tmp_path, strategy, setup):
    """Every shape git declines BEFORE the merge begins raises the subclass, under
    both strategies. Nothing was merged and there is nothing to resolve, so calling
    these a content conflict sends the operator hunting for markers that do not
    exist (#619).

    The HEAD assertion is not decoration: it is what makes "pre-flight" a claim
    about the repo rather than about the exception's name.

    Ablation: make every `merge_branch` failure raise a bare `GitError` and all six
    rows fail; the conflict rows below stay green."""
    repo = project.project
    setup(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(verify.MergePreflightError):
        verify.merge_branch(repo, "feat", strategy=strategy)

    assert git(repo, "rev-parse", "HEAD") == head_before  # nothing landed
    assert not verify._merge_in_progress(repo)  # and nothing is mid-flight


@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_merge_content_conflict_is_not_a_preflight_refusal(project, tmp_path, strategy):
    """The other side of the split: both branches commit a different change to the
    same file, git really merges, and the failure IS a conflict to resolve.

    The raised type carries the whole test: a conflict is `MergeConflictError`,
    measured from the unmerged stages, so the caller's last arm never has to read
    "bare GitError" as "conflict" — whatever arrives untyped there is a state
    nothing measured, and gets an honest minimum instead of this class's
    resolve-by-hand remedy (#619).

    Ablation (typing): put the conflict raise back on bare `GitError` and both
    rows fail on the raised type. Ablation (probe): classify with
    `_merge_in_progress` instead of `_index_unmerged` and the squash row fails
    alone — a conflicted `--squash` writes unmerged index stages but no
    MERGE_HEAD, so MERGE_HEAD reads every squash conflict as a refusal. The
    `merge` row cannot catch that: MERGE_HEAD is exact there."""
    repo = project.project
    _branch_with(repo, tmp_path, modifies={"src.txt": "branch\n"})
    commit(repo, "src.txt", "main change\n", "main edits src")

    with pytest.raises(verify.MergeConflictError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy)

    assert not isinstance(ei.value, verify.MergePreflightError)


def _cleanly_mergeable_branch(repo, tmp_path):
    """A `feat` branch that merges into main with nothing to reconcile: it adds one
    path neither main nor the working tree carries. Deliberately NOT `_branch_with`,
    which mirrors its dirt into the main checkout on purpose — here the merge has to
    SUCCEED at content, so that whatever fails after it is the commit and not the
    merge."""
    wt = tmp_path / "clean-wt"
    verify.worktree_add(repo, wt, "feat", "main")
    (wt / "feature.txt").write_text("feature\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "feat work")
    verify.worktree_remove(repo, wt, force=True)


def test_merge_whose_commit_git_refused_is_not_a_preflight_refusal(project, tmp_path):
    """#619's THIRD state, and the one `_index_unmerged` alone cannot see.

    A `--no-ff` whose COMMIT is declined — by a `pre-merge-commit` or `commit-msg`
    hook, or by a signing step that cannot sign — has already merged: the index holds
    the resolved tree and MERGE_HEAD exists. Nothing conflicted, so it leaves no
    unmerged stages, and a classifier reading only the index calls it "refused before
    starting". `merge_local` then tells the operator that nothing was merged and that
    a target-state clash must be cleared — every clause false, and the clash they are
    sent to find does not exist.

    Staged through `gpg.program` rather than a hook file on purpose: it needs no
    shell, no exec bit and no gpg installed, so the row grades identically on the
    Windows legs. A rejecting `pre-merge-commit` hook reaches the same state (rc 1
    rather than 128, MERGE_HEAD set, no unmerged stages), and so does `commit-msg`.
    Both config writes are repo-LOCAL, so nothing outside this sandbox signs anything.

    The `not isinstance` assertion carries the row: `GitError` alone passes for all
    three states, `MergePreflightError` being a subclass too.

    Ablation: drop the `started` arm from `merge_branch`'s discriminator and this row
    fails on that assertion, while every pre-flight and conflict row above stays
    green — those cannot reach this state.
    """
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert "refused before starting" not in str(ei.value)
    # it really merged, and was rolled back rather than never started: the incoming
    # path reached the tree and the abort took it away again.
    assert not verify._merge_in_progress(repo)
    assert not (repo / "feature.txt").exists()
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert ei.value.restored is True  # the abort ran and worked; the sibling row is the other half


def test_merge_commit_refusal_whose_abort_also_failed_reports_the_tree_unrestored(
    project, tmp_path, monkeypatch
):
    """The repair write can fail too, and then the classification's implied claim —
    "the checkout is back as it was" — is false about the one thing the operator has
    to do FIRST. A resume over a mid-merge checkout dies on the merge state however
    well they fix the hook that declined the commit, so `merge_branch` carries whether
    the abort actually worked rather than letting the exception's type imply it.

    The abort is failed through the `_git` seam because there is no portable way to
    make a real `git merge --abort` fail on demand; everything else in the row is the
    genuine article, including the merge and the signing refusal that precede it. The
    delegation is by argv rather than call count, so it stays pinned to the abort even
    if the surrounding code grows another git call.

    The last assertion is the point of failing it at all: the repo really is left
    mid-merge, so the flag is reporting the tree's state and not just echoing a
    branch it was told to take.

    Ablation: drop the `restored` argument at the raise and this row fails on the
    flag, while the sibling above — which asserts the True half — stays green."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")
    real_git = verify._git

    def failing_abort(r, *args):
        if args[:2] == ("merge", "--abort"):
            return 1, "fatal: could not abort"
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", failing_abort)

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    assert ei.value.restored is False
    assert "repo left mid-merge" in str(ei.value)
    assert verify._merge_in_progress(repo)  # the tree really is unrestored


def test_squash_commit_refusal_is_classified_and_rolled_back(project, tmp_path):
    """The commit-refused state's SECOND door, and the sixth mislabeled git state
    this classification produced: the squash leg's own `git commit`.

    "The squash leg cannot reach the commit-refused state" was measured of the
    MERGE invocation — `merge --squash` exits 0 under a rejecting
    `pre-merge-commit` hook — and over-read onto the leg, whose own plain
    `git commit` runs hooks and `commit.gpgsign` like any other. A refusal there
    raised bare `GitError`, which the caller's last arm dressed as a content
    conflict, with the squash result silently left STAGED: no unmerged stages
    exist and no MERGE_HEAD ever did, so nothing else claimed it either.

    Same portable staging as the `--no-ff` rows above: `gpg.program` pointing at
    a program that does not exist, repo-LOCAL, no shell, no exec bit, no gpg.

    The tree assertions carry the restore half: the pre-merge reading found the
    checkout clean, so `reset --hard HEAD` may and does clear the staged result,
    leaving the checkout exactly as before the squash.

    Ablation (classify): put the commit raise back on bare `GitError` and this
    row and its two siblings below fail on the type; every `--no-ff`
    commit-refused row above stays green — different call site. Ablation (gate):
    force the rollback off (`if pre_dirty` → always) and this row fails on
    `restored`/the clean tree while the dirty-tree sibling stays green."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert "refused before starting" not in str(ei.value)
    assert "merged, but git refused the commit" in str(ei.value)
    assert ei.value.restored is True
    assert ei.value.staged is False  # nothing left staged once the rollback ran
    # the rollback really ran: the staged squash result is gone, tree pristine
    assert not (repo / "feature.txt").exists()
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_squash_commit_refusal_never_resets_a_tree_it_found_dirty(project, tmp_path):
    """DATA-SAFETY PIN, the commit step's half. The rollback for a refused squash
    commit is `reset --hard HEAD`, which flattens the operator's uncommitted work
    together with the staged result — so it stays gated on the same pre-merge
    dirtiness reading the failure arm uses, and a checkout that already carried
    an unstaged edit is never reset. The result is left STAGED instead, the
    exception says so (`staged`), and the operator's edit survives.

    Ablation: reset unconditionally at the commit step and this row fails on the
    operator's bytes — destruction made loud — while the clean-tree sibling
    above stays green."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    (repo / "src.txt").write_text("operator edit\n")  # unstaged, tracked, outside `feat`
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    assert ei.value.restored is False
    assert ei.value.staged is True
    assert "left staged" in str(ei.value)
    assert (repo / "src.txt").read_text() == "operator edit\n"  # the edit survives
    # ...and the squash result really is still sitting staged
    assert "feature.txt" in git(repo, "diff", "--cached", "--name-only").split()


def test_squash_commit_refusal_whose_reset_also_failed_reports_staged(
    project, tmp_path, monkeypatch
):
    """The repair write can fail here too, exactly as the `--no-ff` abort can —
    and then `restored` must report the tree's true state rather than the
    branch the code took, with `staged` naming WHERE the checkout stands: no
    MERGE_HEAD exists on this leg, so "recover the merge" would be fiction and
    the honest first step is clearing the staged result.

    The reset is failed through the `_git` seam for its sibling's reason: there
    is no portable way to make a real `git reset --hard HEAD` fail on demand.
    Delegation by argv, not call count, so it stays pinned to the reset.

    Ablation: hardcode `staged=False` at the raise and this row and the
    dirty-tree sibling fail on the flag; the clean-tree sibling stays green —
    its rollback worked, so it never claims a staged result."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")
    real_git = verify._git

    def failing_reset(r, *args):
        if args[:2] == ("reset", "--hard"):
            return 1, "fatal: could not reset"
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", failing_reset)

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    assert ei.value.restored is False
    assert ei.value.staged is True
    assert "tree not restored" in str(ei.value)
    assert "left staged" in str(ei.value)
    # the tree really is unrestored: the squash result still sits staged
    assert "feature.txt" in git(repo, "diff", "--cached", "--name-only").split()


@pytest.mark.parametrize("diverged", [False, True], ids=["ff-able", "diverged"])
def test_squash_preflight_refusal_never_resets_a_tree_it_found_dirty(project, tmp_path, diverged):
    """DATA-SAFETY PIN. `--squash` has no `--abort`, so its restore is
    `reset --hard HEAD` — which discards the operator's uncommitted work along with
    the merge's. The old guard asked `_tree_dirty_vs_head` AFTER the squash and read
    a checkout that was already dirty as "the squash acted", so a merge git refused
    without touching a byte still triggered the reset and destroyed an unstaged edit
    to a file no branch involved ever mentions (#619).

    Both topologies are covered because the refusal renders differently when the
    merge would have been a fast-forward, and neither rendering may reset.

    Ablation: restore the unconditional `if _tree_dirty_vs_head(repo)` reset and
    both rows fail on the last assertion — with the operator's bytes gone, which is
    the point: this test exists to make that destruction loud."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    if diverged:
        commit(repo, "m.txt", "m\n", "main work")  # commit BEFORE the dirt exists
    (repo / "leak.cs").write_text("operator\n")  # untracked → git refuses at pre-flight
    (repo / "src.txt").write_text("operator edit\n")  # unstaged, tracked, outside `feat`

    with pytest.raises(verify.MergePreflightError):
        verify.merge_branch(repo, "feat", strategy="squash")

    assert (repo / "src.txt").read_text() == "operator edit\n"
    assert (repo / "leak.cs").read_text() == "operator\n"


def _branch_whose_checkout_dies_partway(repo, tmp_path, *, tracked=False):
    """Cut a `feat` branch git cannot finish CHECKING OUT, and arm the failure.

    A **required** filter that cannot run is the portable way to kill a merge in
    the middle of its checkout: it needs no shell, no exec bit, no special file
    mode and no chmod, so this grades identically on the Windows legs — git runs
    filter commands through its own bundled sh, where a command that does not
    exist fails exactly as it does here. Same argument as the `gpg.program`
    staging used by the commit-refused row above.

    Both filenames are load-bearing, and so is their ORDER. git materializes the
    incoming paths in index order, so `aaa.txt` — which no attribute matches — is
    written into the working tree BEFORE `zzz.dat` reaches the filter and kills
    the merge. Rename either side of that boundary and git dies before writing
    anything, which is a genuine pre-flight refusal and not this shape at all.

    ``tracked`` decides which residue axis the failure leaves behind: False adds
    `aaa.txt` on the branch only (it lands untracked, and nothing restores it),
    True commits it on main first so the branch REWRITES it (` M aaa.txt`, which
    `reset --hard HEAD` undoes).

    `.gitattributes` is committed BEFORE the branch is cut, so both sides carry it
    and it is in force in the TARGET at merge time; the filter config is armed
    AFTER the branch is built, so the branch's own `git add` never runs it. Both
    config writes are repo-LOCAL, so nothing outside this sandbox filters anything.
    """
    (repo / ".gitattributes").write_text("*.dat filter=boom\n")
    seed = [".gitattributes"]
    if tracked:
        # The residue axis flips with this: an incoming path the target ALREADY
        # tracks is rewritten in place (` M aaa.txt`) instead of appearing as an
        # untracked add, and only the tracked axis is restorable.
        (repo / "aaa.txt").write_text("original aaa\n")
        seed.append("aaa.txt")
    # `-A` and not the paths would sweep in whatever stray the CALLER staged the
    # scene with, and one row's whole point is a stray that stays untracked.
    git(repo, "add", "--", *seed)
    git(repo, "commit", "-q", "-m", "attributes")
    wt = tmp_path / "partway-wt"
    verify.worktree_add(repo, wt, "feat", "main")
    (wt / "aaa.txt").write_text("incoming aaa\n")
    (wt / "zzz.dat").write_text("incoming zzz\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "feat work")
    verify.worktree_remove(repo, wt, force=True)
    git(repo, "config", "filter.boom.smudge", "bmad-loop-no-such-filter")
    git(repo, "config", "filter.boom.clean", "cat")
    git(repo, "config", "filter.boom.required", "true")


@pytest.mark.parametrize("strategy", ["merge", "squash"])
@pytest.mark.parametrize("diverged", [False, True], ids=["ff-able", "diverged"])
def test_merge_that_died_partway_through_checkout_is_not_a_preflight_refusal(
    project, tmp_path, strategy, diverged
):
    """The FOURTH #619 shape, and the one no index- or HEAD-based probe can see.

    git can die in the middle of writing the incoming files out. When it does it
    rolls the INDEX back and stops, leaving the files it already wrote in the
    working tree as UNTRACKED — so `ls-files -u` is empty, `MERGE_HEAD` is absent,
    and `git diff --quiet HEAD --` exits 0 because an untracked file is in neither
    HEAD nor the index. Every probe the classifier had said "refused before
    starting", and `merge_local` then told the operator their checkout was
    unchanged while the residue sat there.

    The residue is the harm, not the wording: it is exactly the shape git refuses
    the NEXT merge over (`untracked working tree files would be overwritten`), so
    the run fails identically on every resume, over paths no message had named.
    Neither restore reaches it — `git reset --hard` and `git merge --abort` both
    leave untracked files alone (measured) — which is why this is classified and
    reported rather than cleaned.

    Both strategies, because both legs check out and both were affected; both
    topologies, because the refusal renders differently when the merge would have
    been a fast-forward.

    The `not isinstance` assertion carries the row: `GitError` alone passes for all
    four states and `MergePreflightError` is a subclass of neither. The `paths`
    assertion is the second half — a correct class carrying nothing to act on
    leaves the operator exactly as stuck.

    Ablation (predicate axis): drop the `materialized` arm from `merge_branch`'s
    discriminator and exactly these five rows fail, every other verify-layer row
    staying green. Dropping the DELTA instead is a different ablation with a wider
    blast radius — see the row below, which is the one that pins it."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path)
    if diverged:
        commit(repo, "m.txt", "m\n", "main work")
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy)

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert "refused before starting" not in str(ei.value)
    assert ei.value.paths == ("aaa.txt",)  # written before the filter killed the merge
    # ...and it really is on disk, untracked, and survives the leg's own restore
    assert (repo / "aaa.txt").read_text() == "incoming aaa\n"
    assert "aaa.txt" in git(repo, "ls-files", "--others", "--exclude-standard").split()
    assert git(repo, "rev-parse", "HEAD") == head_before  # nothing landed
    assert not verify._merge_in_progress(repo)


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_partway_checkout_restores_the_tracked_files_it_rewrote(project, tmp_path, strategy):
    """The residue's SECOND axis, and the one the untracked delta is blind to by
    construction.

    An incoming path the target does not already track lands as an untracked add.
    An incoming path it DOES track is rewritten in place, so `ls-files --others`
    never mentions it and the delta is empty — while the checkout now holds
    incoming content on a tracked path. That is the same harm in a different
    shape: git refuses the next merge over it ("Your local changes to the
    following files would be overwritten by merge"), so a run told its checkout
    was unchanged fails on every resume.

    Unlike the untracked axis, this one IS restorable, and `reset --hard HEAD`
    under #619's clean-tree gate is what restores it. So the row asserts both
    halves: the classification is `MergeHalfAppliedError` (the CAUSE is a stopped
    checkout, not a target-state clash, and the remedies differ), and the tree is
    genuinely put back.

    All three strategies, because all three check out. `ff` is the row that
    matters most: its leg carried an explicit "--ff-only never starts a merge"
    premise and did no residue detection at all, so a fast-forward killed
    mid-checkout left the target rewritten with nothing to restore it.

    Ablation (predicate): force `tracked_dirtied` False in `_merge_residue` and
    these three rows fail on the class. Ablation (repair): drop the
    `_restore_tracked_residue` call from all three legs and they fail instead on
    the file contents, which is the half a classification-only fix would have
    missed. Measured, BOTH also redden `test_merge_squash_conflict_restores`, and
    that is worth knowing rather than trimming out of the record: `tracked_dirtied`
    is the same value that gates #619's pre-existing squash-conflict restore, so
    the two behaviours share one predicate and a change to it moves both. The
    untracked rows above stay green under either, having no tracked residue to
    see."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path, tracked=True)
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy, message="m")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert "refused before starting" not in str(ei.value)
    assert ei.value.paths == ()  # nothing untracked was left, so nothing to hand over
    assert ei.value.restored  # ...and the tracked rewrite was rolled back
    assert (repo / "aaa.txt").read_text() == "original aaa\n"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_ff_only_killed_mid_checkout_is_not_a_preflight_refusal(project, tmp_path):
    """`--ff-only` declines the TOPOLOGY question before touching anything — which
    is true, and was over-read into "so it never touches the tree", which is not.

    Once the fast-forward IS possible git checks the incoming tree out like any
    other merge, and a failure during that write leaves residue with HEAD still
    where it was. This leg had no residue detection at all and an explicit comment
    asserting it needed none, so every such failure was a flat
    `MergePreflightError`.

    The untracked axis is the one asserted here because it is the one nothing can
    restore: the operator is handed the path or they never learn it. The tracked
    axis for this same leg is covered by the row above.

    Ablation: restore the bare `raise MergePreflightError(...)` on the `ff` leg and
    exactly two rows fail — this one and the tracked-axis row's `ff` case, i.e. both
    residue axes for this leg and nothing else. `test_merge_ff_diverged_raises`
    stays green throughout, which is the point: it pins the topology refusal, and
    that one really does decline before reaching a checkout."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path)  # untracked axis
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy="ff")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert ei.value.paths == ("aaa.txt",)
    assert (repo / "aaa.txt").read_text() == "incoming aaa\n"  # really left behind
    assert git(repo, "rev-parse", "HEAD") == head_before  # and the ff never landed


def test_partway_checkout_failure_names_only_what_git_wrote(project, tmp_path):
    """The residue is reported as a before/after DELTA, so the operator's own
    pre-existing strays are never handed to them as git's doing.

    An absolute post-merge reading of `ls-files --others` would name every
    untracked file in the checkout — and the message tells the operator to clear
    what it names, over a checkout the guard deliberately tolerates strays in
    (#460). Naming one is how a correct fix to the classification would have
    become a worse bug than the one it replaced.

    Ablation: drop the `- pre_untracked` subtraction and this row fails on the
    equality — and so, measured, do SIX pre-existing rows that predate this fix:
    both `untracked-overwrite` shapes, both `shape-clash` shapes, and both
    topologies of the reset data-safety pin. That is not incidental breakage, it is
    the second half of the same defect: each of those stages a genuine pre-flight
    refusal with an untracked stray already in the checkout, so an absolute reading
    finds that stray afterwards and reclassifies a merge git refused without
    touching a byte as one that half-applied. The subtraction is what keeps the two
    classes disjoint, not merely what keeps this row's message tidy."""
    repo = project.project
    (repo / "operator-notes.txt").write_text("mine\n")  # untracked, predates the merge
    _branch_whose_checkout_dies_partway(repo, tmp_path)

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    assert ei.value.paths == ("aaa.txt",)
    assert "operator-notes.txt" not in str(ei.value)
    assert (repo / "operator-notes.txt").read_text() == "mine\n"  # and left alone


def _probe_that_dies_on_its_second_reading(monkeypatch):
    """Fail `_untracked_paths` on its POST-merge reading only.

    Reading number two is the one `_merge_residue` takes after the merge has
    failed; number one is `_residue_snapshot`'s, which runs while nothing has
    been mutated and deliberately KEEPS its raise — failing it would abort the
    merge outright and never reach the classification these rows pin. Counted
    rather than argv-matched because both readings run the same git command;
    only their position tells them apart."""
    real_probe = verify._untracked_paths
    reads = {"n": 0}

    def dying_probe(repo):
        reads["n"] += 1
        if reads["n"] >= 2:
            raise verify.GitError(f"git ls-files --others failed in {repo}: probe boom")
        return real_probe(repo)

    monkeypatch.setattr(verify, "_untracked_paths", dying_probe)


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_partway_checkout_with_a_dead_probe_is_reported_unverified(
    project, tmp_path, strategy, monkeypatch
):
    """The classification's terminal state: the merge failed AND the post-merge
    residue reading failed, so no verdict exists — and the raise says THAT,
    rather than letting the probe error escape or its empty degrade impersonate
    a verdict.

    Unwrapped, the probe's raise escapes `merge_branch` as its own `GitError`
    wearing a probe error's text, which `merge_local`'s last arm reads as a
    content conflict. Degraded silently, the empty reading lands in
    `MergePreflightError`, whose load-bearing clause — the checkout was never
    touched — is exactly what stopped being known. `MergeResidueUnreadError` is
    the honest remainder: it carries git's own failure text AND the probe's, and
    claims nothing about the tree.

    The scene is a genuine part-way checkout (the rows above prove what it
    leaves behind), so these rows also pin the safe side of the degrade: the
    residue that IS there goes unreported rather than misreported, and nothing
    is reset on an unproven attribution.

    Ablation (wrap axis): re-raise instead of catch in `_merge_residue` and
    these three rows fail on the raised type — the probe's own `GitError`
    escapes. Ablation (claim axis): route the unread case to
    `MergePreflightError` instead and they fail on the phrase assertions. The
    commit-refused row below stays green through the claim ablation, which is
    what parts the wrap from the claim."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    _probe_that_dies_on_its_second_reading(monkeypatch)

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy)

    assert not isinstance(ei.value, verify.MergePreflightError)
    msg = str(ei.value)
    assert "checkout state unverified" in msg
    assert "AND the residue probe failed" in msg and "probe boom" in msg
    assert "refused before starting" not in msg
    assert "left untracked" not in msg  # unread — so nothing is (mis)reported either
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert not verify._merge_in_progress(repo)
    # the safe side of the degrade: the residue survives, unread rather than reset
    assert (repo / "aaa.txt").read_text() == "incoming aaa\n"


def test_commit_refusal_with_a_dead_probe_still_aborts_the_merge(project, tmp_path, monkeypatch):
    """The cleanup half of the wrap, and the scenario that motivated it: the
    residue probe dies AFTER `--no-ff` has already created MERGE_HEAD. Unwrapped,
    that raise escapes BEFORE the abort block runs, so the target is stranded
    mid-merge — over a commit git had already refused for an unrelated reason —
    and every resume then dies on the merge state instead of the policy.

    The classification owes nothing to the residue pair here: MERGE_HEAD was
    read before the probe, so the commit-refused verdict stands on its own
    measurement, and this row pins that a dead probe changes NEITHER the class
    NOR the abort. Only the choice between pre-flight and half-applied ever
    rested on the residue reading (the rows above).

    Ablation: re-raise instead of catch in `_merge_residue` and this row fails
    twice over — the type collapses to the probe's `GitError` and MERGE_HEAD
    survives the escape. The claim-axis ablation (unread routed to pre-flight)
    leaves it green, which is what makes it the wrap's row rather than the
    claim's."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")
    _probe_that_dies_on_its_second_reading(monkeypatch)

    with pytest.raises(verify.MergeCommitRefusedError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    assert ei.value.restored is True
    assert not verify._merge_in_progress(repo)  # the abort still ran
    assert not (repo / "feature.txt").exists()
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_squash_replay_ignores_preexisting_unstaged_dirt(project, tmp_path):
    """`allow_empty_squash` recognises a replay by "the squash staged nothing" — the
    target already carries the merged tree. Asking that of the WORKING TREE let a
    pre-existing unstaged edit answer for the squash: the clean early return was
    skipped, `git commit` found nothing staged, and a host-loss recovery was reported
    as a failed merge. The index is the honest question (#619).

    Ablation: put the gate back on `_tree_dirty_vs_head` and this fails with a
    GitError naming "no changes added to commit"."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"f.txt": "branch\n"})
    verify.merge_branch(repo, "feat", strategy="squash", message="squash feat")  # the lost commit
    (repo / "src.txt").write_text("operator edit\n")  # unstaged, tracked, outside `feat`
    head_before = git(repo, "rev-parse", "HEAD")

    verify.merge_branch(repo, "feat", strategy="squash", allow_empty_squash=True)  # must not raise

    assert git(repo, "rev-parse", "HEAD") == head_before  # no empty commit manufactured
    assert (repo / "src.txt").read_text() == "operator edit\n"


def test_no_ff_conflict_with_preexisting_dirt_aborts_and_keeps_it(project, tmp_path):
    """The `merge` leg's restore is `git merge --abort`, which — unlike the squash
    leg's `reset --hard` — leaves an unstaged edit to an untouched tracked file
    alone. So a genuine conflict still aborts even with the checkout dirty, and the
    operator keeps both their edit and the conflict to resolve (#619).

    Ablation: none of the #619 guards can redden this row; it is the control that
    proves the squash-leg fix did not have to be applied here too."""
    repo = project.project
    commit(repo, "other.txt", "committed\n", "add other.txt")
    _branch_with(repo, tmp_path, modifies={"src.txt": "branch\n"})
    commit(repo, "src.txt", "main change\n", "main edits src")
    (repo / "other.txt").write_text("operator edit\n")  # neither side touches it

    with pytest.raises(verify.GitError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert not verify._merge_in_progress(repo)  # the abort ran
    assert (repo / "other.txt").read_text() == "operator edit\n"
    assert (repo / "src.txt").read_text() == "main change\n"  # conflict markers rolled back


# ---------------------------------------------------- dirty_paths / incoming


def test_dirty_paths_reports_untracked_and_modified(project):
    repo = project.project
    (repo / "src.txt").write_text("modified\n")  # tracked edit -> " M"
    (repo / "new.txt").write_text("brand new\n")  # untracked -> "??"
    dp = verify.dirty_paths(repo)
    assert dp.get("new.txt") == "??"
    assert dp.get("src.txt", "").strip() == "M"


def test_dirty_paths_clean_tree_is_empty(project):
    assert verify.dirty_paths(project.project) == {}


def test_dirty_paths_ignores_policy_file(project):
    repo = project.project
    policy = repo / verify.POLICY_FILE_REL
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("changed = true\n")
    assert verify.dirty_paths(repo) == {}  # policy.toml excluded like worktree_clean


def test_branch_incoming_paths(project, tmp_path):
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "added.txt", "a\n", "feat adds")
    (wt / "src.txt").write_text("changed\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "feat edits src")
    incoming = verify.branch_incoming_paths(repo, "main", "feat")
    assert incoming == {"added.txt", "src.txt"}


# ---------------------------------------------------- clean_incoming_collisions


def _branch_with(repo, tmp_path, *, adds=None, modifies=None):
    """Cut a `feat` branch (worktree) that adds/modifies files, then mirror that
    same dirt into the main checkout (untracked add / tracked-modified) to model
    an Editor leak. Returns nothing; the main tree is left dirty."""
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    for name, content in {**(adds or {}), **(modifies or {})}.items():
        fp = wt / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "feat work")
    verify.worktree_remove(repo, wt, force=True)


def test_clean_incoming_collisions_cleans_within_branch_set(project, tmp_path):
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"}, modifies={"src.txt": "branch\n"})
    # editor leaked the same files into the main tree
    (repo / "leak.cs").write_text("editor leaked\n")  # untracked
    (repo / "src.txt").write_text("editor edited\n")  # tracked-modified

    cleaned = verify.clean_incoming_collisions(repo, "main", "feat")
    assert sorted(cleaned) == ["leak.cs", "src.txt"]
    assert not (repo / "leak.cs").exists()  # untracked leak deleted
    assert (repo / "src.txt").read_text() == "original\n"  # restored to HEAD
    assert verify.worktree_clean(repo)
    # and the merge now lands cleanly
    verify.merge_branch(repo, "feat", strategy="merge")
    assert (repo / "leak.cs").read_text() == "branch\n"


def test_clean_incoming_collisions_tolerates_untracked_stray(project, tmp_path):
    """#460: an UNTRACKED dirty path outside the branch's incoming set is inert —
    the merge writes only paths that differ between target and branch, and git
    never stages an untracked file into a merge or squash commit. It is left
    exactly where it is and does not stop the merge."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    (repo / "leak.cs").write_text("editor leaked\n")  # within branch set
    (repo / "operator-notes.txt").write_text("real work\n")  # untracked, NOT in the set

    cleaned = verify.clean_incoming_collisions(repo, "main", "feat")  # no GitError
    assert cleaned == ["leak.cs"]  # only the leak; the stray is not even reported
    assert not (repo / "leak.cs").exists()
    assert (repo / "operator-notes.txt").read_text() == "real work\n"  # bytes intact
    # The merge is the point of this test: surviving *our* guard is not enough, the
    # tolerated file must also not trip git's OWN merge pre-flight. If it did, the
    # narrowing would have moved the halt rather than removed it.
    verify.merge_branch(repo, "feat", strategy="merge")
    assert (repo / "leak.cs").read_text() == "branch\n"
    assert (repo / "operator-notes.txt").read_text() == "real work\n"


@pytest.mark.parametrize(
    ("incoming_path", "stray_path"),
    [
        ("Assets/Leak.cs", "Assets"),  # untracked FILE standing where the merge needs a DIR
        ("notes", "notes/keep.txt"),  # untracked DIR standing where the merge needs a FILE
    ],
    ids=["file-where-dir-needed", "dir-where-file-needed"],
)
def test_clean_incoming_collisions_shape_clash_stops_at_gits_own_preflight(
    project, tmp_path, incoming_path, stray_path
):
    """The BOUNDARY of #460's tolerance, both directions. An untracked stray whose
    *path* is outside the incoming set can still clash with it STRUCTURALLY — an
    untracked file standing where the merge needs a directory, or the reverse. Such a
    path is not inert, and this guard deliberately does not try to detect it: git's
    own pre-flight is the authority on what a merge would overwrite, it names the
    exact path, and a hand-rolled ancestor/descendant predicate here could only drift
    from git's real rules.

    What this test pins is that deferring is SAFE — the halt is not lost, only moved
    one call later, and the operator's bytes survive it. Were tolerance ever widened
    to swallow git's refusal too, this test goes red rather than a run silently
    destroying operator data. The two labelling gaps this shape used to leave behind
    are now closed one layer up, and this row stays the fixture both were measured
    against: #619 (the escalation called a pre-flight refusal a "content conflict")
    by the `MergePreflightError` split asserted above, and #623 (`merge-target-
    tolerated` journaled for a stray that then blocked the merge) by the corrective
    `merge-preflight-refused` event — see
    `test_merge_shape_clash_journals_the_corrective_refusal` in
    tests/test_engine_worktree.py, which drives these same two shapes end to end."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={incoming_path: "branch\n"})
    stray = repo / stray_path
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("operator\n")
    head_before = git(repo, "rev-parse", "HEAD")

    # Our guard walks past it: the stray's path is not in the incoming set, and it is
    # untracked, so by the letter of the predicate it is tolerated. Nothing is cleaned.
    calls: list[list[str]] = []
    assert verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append) == []
    assert calls == [[stray_path]]

    # ...and git stops it anyway, one call later, naming the colliding path itself.
    with pytest.raises(verify.GitError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")
    assert stray_path.split("/")[0] in str(ei.value)

    # What makes deferring acceptable: the operator's bytes are intact and the merge
    # applied NOTHING. Deliberately NOT asserted via `.git/MERGE_HEAD` — that file is
    # absent after a genuine content conflict too (`merge_branch` runs `merge --abort`),
    # so it would pass for every reason and discriminate nothing. `is_file()` carries
    # the shape half: landing this merge has to convert `Assets` file->dir (row 1) or
    # delete `notes/` to make room for a file (row 2), so either way this goes red.
    assert stray.is_file() and stray.read_text() == "operator\n"
    assert git(repo, "rev-parse", "HEAD") == head_before  # and no merge commit exists


@pytest.mark.parametrize("stage", [True, False], ids=["staged", "unstaged"])
def test_clean_incoming_collisions_splits_tracked_stray_on_the_index(project, tmp_path, stage):
    """The half of #460 that #618 re-cut. Trackedness was never the axis: what a
    merge can write into a commit is what git has STAGED. Measured on git 2.55 across
    both topologies and both strategies — a staged stray outside the incoming set is
    refused by `merge --no-ff` and by a divergent `merge --squash`, and folded into
    the story's commit by a fast-forwardable one; an UNSTAGED one is inert in every
    cell (rc 0, absent from the commit, still uncommitted afterwards).

    So the staged row refuses and the unstaged row proceeds. The unstaged row also
    pins that it is REPORTED: were `tolerated` left on the untracked test it used to
    carry, this stray would answer neither list and the merge would proceed with no
    journal trace at all."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})  # `feat` never touches src.txt
    (repo / "leak.cs").write_text("editor leaked\n")  # within branch set
    (repo / "src.txt").write_text("operator edit\n")  # tracked-modified, NOT in the set
    if stage:
        git(repo, "add", "src.txt")

    calls: list[list[str]] = []
    if stage:
        with pytest.raises(verify.GitError) as ei:
            verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append)
        assert "src.txt" in str(ei.value)
        assert "tracked" in str(ei.value)  # the refusal names which half it is about
        assert calls == []  # a refusal reports no tolerance
        # nothing was cleaned — the leak still sits there and the edit is unreverted
        assert (repo / "leak.cs").exists()
    else:
        cleaned = verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append)
        assert cleaned == ["leak.cs"]  # the incoming leak is still reconciled
        assert calls == [["src.txt"]]  # and the stray it walked past is on the record
    assert (repo / "src.txt").read_text() == "operator edit\n"  # untouched either way


# ------------------------------------------------- #618 porcelain grid + parse pins


def _feat_adding_leak(repo, tmp_path):
    """Cut `feat` adding one file the main checkout never touches, so the incoming
    set is exactly {"leak.cs"} and dirt made anywhere else is a stray."""
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})


def _xy_unstaged_modify(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").write_text("operator edit\n")
    return "src.txt"


def _xy_staged_modify(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").write_text("operator edit\n")
    git(repo, "add", "src.txt")
    return "src.txt"


def _xy_staged_and_unstaged_modify(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").write_text("staged half\n")
    git(repo, "add", "src.txt")
    (repo / "src.txt").write_text("and an unstaged half\n")
    return "src.txt"


def _xy_staged_add(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "new.txt").write_text("operator\n")
    git(repo, "add", "new.txt")
    return "new.txt"


def _xy_staged_delete(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    git(repo, "rm", "-q", "src.txt")
    return "src.txt"


def _xy_unstaged_delete(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").unlink()
    return "src.txt"


def _xy_staged_rename(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    git(repo, "mv", "src.txt", "renamed.txt")
    return "renamed.txt"  # `dirty_paths` records the DESTINATION


def _xy_untracked(repo, tmp_path):
    _feat_adding_leak(repo, tmp_path)
    (repo / "operator-notes.txt").write_text("real work\n")
    return "operator-notes.txt"


def _xy_unmerged(repo, tmp_path):
    """A real conflicted merge, left half-resolved in the MAIN checkout.

    The ordering is load-bearing: both sides of the conflict land before `feat` is
    cut, so `conflict.txt` is identical on the two tips and stays OUT of the incoming
    set. Cut `feat` first and main's later commits would pull it in, and the row
    would grade cleaning rather than blocking."""
    commit(repo, "conflict.txt", "base\n", "conflict base")
    git(repo, "checkout", "-q", "-b", "theirs")
    commit(repo, "conflict.txt", "theirs\n", "their edit")
    git(repo, "checkout", "-q", "main")
    commit(repo, "conflict.txt", "ours\n", "our edit")
    _feat_adding_leak(repo, tmp_path)
    with pytest.raises(subprocess.CalledProcessError):  # the conflict is the fixture
        git(repo, "merge", "theirs")
    return "conflict.txt"


@pytest.mark.parametrize(
    ("xy", "setup", "blocks"),
    [
        (" M", _xy_unstaged_modify, False),
        ("M ", _xy_staged_modify, True),
        ("MM", _xy_staged_and_unstaged_modify, True),
        ("A ", _xy_staged_add, True),
        ("D ", _xy_staged_delete, True),
        (" D", _xy_unstaged_delete, False),
        ("R ", _xy_staged_rename, True),
        ("??", _xy_untracked, False),
        ("UU", _xy_unmerged, True),
    ],
    ids=[
        "unstaged-modify",
        "staged-modify",
        "staged-and-unstaged-modify",
        "staged-add",
        "staged-delete",
        "unstaged-delete",
        "staged-rename",
        "untracked",
        "unmerged",
    ],
)
def test_clean_incoming_collisions_porcelain_grid(project, tmp_path, xy, setup, blocks):
    """One row per porcelain XY a stray can wear (#618). The split is the INDEX
    column alone: a letter there is work git would carry into the merge's commit, a
    space or a `?` is work only the working tree holds. Unmerged stages block for the
    same reason — every one of git's seven combinations puts a letter in X.

    Deliberately silent about `on_tolerated`: the proceeding rows assert only that no
    refusal happened. Reporting is pinned by
    `test_clean_incoming_collisions_splits_tracked_stray_on_the_index`, and asserting
    it here too would make the `blocking` and `tolerated` ablations redden one
    indistinguishable set instead of two."""
    repo = project.project
    stray = setup(repo, tmp_path)

    # Prove the fixture built a STRAY and wore the XY the row claims. A path that
    # drifted into the incoming set would be cleaned rather than judged, and the row
    # would pass for a reason that has nothing to do with the predicate.
    assert verify.branch_incoming_paths(repo, "main", "feat") == {"leak.cs"}
    assert verify.dirty_paths(repo) == {stray: xy}

    if blocks:
        with pytest.raises(verify.GitError) as ei:
            verify.clean_incoming_collisions(repo, "main", "feat")
        assert stray in str(ei.value)
    else:
        assert verify.clean_incoming_collisions(repo, "main", "feat") == []


def test_clean_incoming_collisions_rename_stray_names_the_destination(project, tmp_path):
    """A rename is the one entry whose porcelain record has two paths, and under `-z`
    git emits them destination-first — the INVERSE of plain porcelain's `old -> new`.
    `dirty_paths` consumes the second field as the source, so the path that reaches
    the operator is the one now on disk, which is the one they have to deal with.

    Ablation target: drop the `"R" in xy or "C" in xy` skip in `dirty_paths` and the
    source field is re-parsed as its own entry — `xy=tok[:2]`, `path=tok[3:]` turns
    `src.txt` into a phantom `.txt` stray — which the dict equality below catches."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)
    git(repo, "mv", "src.txt", "renamed.txt")

    assert verify.dirty_paths(repo) == {"renamed.txt": "R "}

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat")
    assert "renamed.txt" in str(ei.value)
    assert "src.txt" not in str(ei.value)  # the source is not what is on disk


def test_clean_incoming_collisions_copy_stray_names_the_destination(project, tmp_path):
    """The `C` half of that same two-path branch, which nothing else covers.

    Both conjuncts are needed to make git emit one at all: `status.renames=copies`
    AND a MODIFIED source. With an unmodified source git reports a plain `A` and the
    branch is never entered, so a fixture missing either half grades nothing."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)
    git(repo, "config", "status.renames", "copies")
    (repo / "copy.txt").write_text("original\n")  # byte copy of src.txt's committed content
    (repo / "src.txt").write_text("operator edit\n")  # the modified source half
    git(repo, "add", "copy.txt", "src.txt")

    dirty = verify.dirty_paths(repo)
    assert set(dirty) == {"copy.txt", "src.txt"}  # no phantom entry from the source field
    assert dirty["copy.txt"].startswith("C")  # the fixture really produced a copy entry

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat")
    assert "copy.txt" in str(ei.value)


# ------------------------------------------------------------- #618 `protected`


def test_clean_incoming_collisions_protected_blocks_unstaged_dirt(project, tmp_path):
    """`protected` is not about the merge. The merge would walk past this unstaged
    edit harmlessly; what would not is `commit_paths`, which the run's carry
    bookkeeping calls with this exact path — `git add` then a pathspec commit stages
    whatever the working tree holds, so the operator's private edit would land in
    history under a `chore(...): carry ...` message with the tree left clean.

    The refusal names it under the CARRY clause, not the staged one: unstaging is not
    a remedy for a path this run is going to commit either way."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").write_text("operator edit\n")
    assert verify.dirty_paths(repo) == {"src.txt": " M"}  # inert for the merge itself

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat", protected=("src.txt",))
    msg = str(ei.value)
    assert "src.txt" in msg
    assert "bookkeeping commit" in msg
    assert "staged changes" not in msg  # the other clause is absent, not merely joined
    assert (repo / "src.txt").read_text() == "operator edit\n"  # nothing touched


def test_clean_incoming_collisions_protected_names_both_groups_separately(project, tmp_path):
    """One raise, two remedies. A run can hit both at once, and an undifferentiated
    path list would send the operator to the wrong fix for one of them."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)
    (repo / "staged.txt").write_text("operator\n")
    git(repo, "add", "staged.txt")
    (repo / "src.txt").write_text("operator edit\n")  # unstaged, but carried

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat", protected=("src.txt",))
    msg = str(ei.value)
    staged_clause, _, carry_clause = msg.partition("; and ")
    assert "staged.txt" in staged_clause and "src.txt" not in staged_clause
    assert "src.txt" in carry_clause and "staged.txt" not in carry_clause


def test_clean_incoming_collisions_names_a_staged_carried_path_under_both_clauses(
    project, tmp_path
):
    """The OVERLAP the sibling above does not cover: one path that is staged AND
    carried. The two clauses carry different remedies, and only one of them removes
    this path's hazard — the carry stages whatever the working tree holds, so
    "commit or unstage it" leaves the operator's bytes exactly where the carry will
    find them. Naming it under the staged clause alone therefore sends them to a fix
    that does not fix it.

    Both clauses have to exist for the row to mean anything, which is why the raise
    is partitioned rather than searched: `"src.txt" in msg` would pass on a message
    carrying only one of them.

    Ablation: compute `swept` from the `staged` complement again and this row fails
    on the carry clause, while both sibling rows — disjoint paths, and carried-only —
    stay green, because neither has a path in both sets."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)
    (repo / "src.txt").write_text("operator edit\n")
    git(repo, "add", "src.txt")  # staged AND named as carried below
    assert verify.dirty_paths(repo) == {"src.txt": "M "}

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat", protected=("src.txt",))
    msg = str(ei.value)
    staged_clause, sep, carry_clause = msg.partition("; and ")
    assert sep, f"expected both clauses, got: {msg}"
    assert "src.txt" in staged_clause
    assert "src.txt" in carry_clause
    assert (repo / "src.txt").read_text() == "operator edit\n"  # nothing touched


def test_clean_incoming_collisions_protected_is_paths_not_a_mode(project, tmp_path):
    """Naming a path the operator has not dirtied changes nothing, and naming one
    does not make an unrelated stray block either — `protected` intersects the
    strays, it does not switch the guard into a stricter mode."""
    repo = project.project
    _feat_adding_leak(repo, tmp_path)

    # row (a): the protected path is clean, and so is everything else
    assert verify.clean_incoming_collisions(repo, "main", "feat", protected=("src.txt",)) == []

    # row (b): the protected path is still clean; the dirt is somewhere else entirely
    (repo / "operator-notes.txt").write_text("real work\n")
    calls: list[list[str]] = []
    cleaned = verify.clean_incoming_collisions(
        repo, "main", "feat", protected=("src.txt",), on_tolerated=calls.append
    )
    assert cleaned == []
    assert calls == [["operator-notes.txt"]]


def test_clean_incoming_collisions_reports_tolerated_paths(project, tmp_path):
    """#460's observability half. The strays the guard walks past are handed to
    `on_tolerated` — the mirror of the returned `cleaned` list — so a merge that
    proceeded over operator dirt leaves the same kind of trace as one that cleaned a
    leak, instead of walking past it silently."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    (repo / "leak.cs").write_text("editor leaked\n")  # within branch set — cleaned
    # written out of alphabetical order: the callback's list must be sorted by the
    # helper, not by the order the filesystem happens to hand them back.
    (repo / "b-notes.txt").write_text("real work\n")
    (repo / "a-notes.txt").write_text("more real work\n")

    calls: list[list[str]] = []
    cleaned = verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append)

    assert len(calls) == 1  # exactly once, not once per stray
    assert calls[0] == ["a-notes.txt", "b-notes.txt"]  # sorted; the leak is NOT here
    assert cleaned == ["leak.cs"]  # the two lists are disjoint halves of the dirt
    assert (repo / "a-notes.txt").exists() and (repo / "b-notes.txt").exists()


def test_clean_incoming_collisions_no_tolerated_callback_when_clean(project, tmp_path):
    """`on_tolerated` fires only when there is something to report. An empty call
    would journal a no-op `merge-target-tolerated` on every clean merge, which is
    noise an operator would learn to ignore. Two rows: a clean tree (row a), and a
    tree whose only dirt IS the incoming leak (row b) — the second is the one that
    reaches the callback site at all, since a clean tree returns before it."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    calls: list[list[str]] = []

    # row (a): nothing dirty at all
    assert verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append) == []
    assert calls == []

    # row (b): dirty, but every dirty path is inside the branch's incoming set
    (repo / "leak.cs").write_text("editor leaked\n")
    cleaned = verify.clean_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append)
    assert cleaned == ["leak.cs"]
    assert calls == []


def test_clean_incoming_collisions_clean_tree_noop(project, tmp_path):
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    assert verify.clean_incoming_collisions(repo, "main", "feat") == []


def test_clean_incoming_collisions_ignores_policy_file(project, tmp_path):
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    policy = repo / verify.POLICY_FILE_REL
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("changed = true\n")  # dirty but excluded
    assert verify.clean_incoming_collisions(repo, "main", "feat") == []
    assert policy.read_text() == "changed = true\n"  # left untouched


def test_clean_incoming_collisions_prunes_emptied_dirs(project, tmp_path):
    repo = project.project
    _branch_with(repo, tmp_path, adds={"Assets/Tests/Leak.cs": "branch\n"})
    leak = repo / "Assets" / "Tests" / "Leak.cs"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("editor leaked\n")  # untracked, in a fresh subtree

    cleaned = verify.clean_incoming_collisions(repo, "main", "feat")
    assert cleaned == ["Assets/Tests/Leak.cs"]
    assert not (repo / "Assets").exists()  # emptied dirs pruned back to root


@pytest.mark.parametrize("refused", ["repo-root", "prune-parent"])
def test_clean_incoming_collisions_resolution_fault_precedes_deletion(
    project, tmp_path, monkeypatch, refused
):
    """Repo-root and prune-parent uncertainty propagate as direct filesystem
    failures before the incoming untracked path is unlinked.

    Ablation target: move the prune-parent resolve back below `fp.unlink`, and the
    `prune-parent` row fails because the injected fault arrives after the leak was
    deleted; move repo-root resolution below cleanup and the `repo-root` row fails
    for the same destructive-first reason.
    """
    repo = project.project
    _branch_with(repo, tmp_path, adds={"Assets/Tests/Leak.cs": "branch\n"})
    leak = repo / "Assets" / "Tests" / "Leak.cs"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("editor leaked\n")
    refuse_to_resolve(monkeypatch, repo if refused == "repo-root" else leak.parent)

    with pytest.raises(OSError):
        verify.clean_incoming_collisions(repo, "main", "feat")

    assert leak.read_text() == "editor leaked\n"  # uncertain cleanup never ran
    assert leak.parent.is_dir()  # nor did its prune chain start


def test_clean_incoming_collisions_prune_keeps_dir_holding_a_stray(project, tmp_path):
    """The directory-prune half of #460's tolerance. A passing
    `..._tolerates_untracked_stray` does not imply this one: that stray sits at the
    repo root, where the `rmdir` walk-up never runs. Here the tolerated stray shares
    a directory with the cleaned leak, so the prune tail walks straight into it."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"Assets/Tests/Leak.cs": "branch\n"})
    leak = repo / "Assets" / "Tests" / "Leak.cs"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("editor leaked\n")  # untracked, within the branch set
    keep = repo / "Assets" / "Tests" / "keep.txt"
    keep.write_text("operator\n")  # untracked stray in the SAME directory

    cleaned = verify.clean_incoming_collisions(repo, "main", "feat")
    assert cleaned == ["Assets/Tests/Leak.cs"]
    assert not leak.exists()
    assert keep.read_text() == "operator\n"  # tolerated, bytes intact
    assert keep.parent.is_dir()  # the prune stopped at a directory that is not empty


# ---------------------------------------------------------------- capture_diff


def test_capture_diff_includes_tracked_and_untracked(project):
    repo = project.project
    base = verify.rev_parse_head(repo)
    (repo / "src.txt").write_text("modified\n")  # tracked edit
    (repo / "untracked.txt").write_text("brand new\n")  # untracked add

    diff = verify.capture_diff(repo, base)
    assert "modified" in diff  # tracked change present
    assert "untracked.txt" in diff and "brand new" in diff  # untracked included


def test_capture_diff_empty_when_clean(project):
    repo = project.project
    base = verify.rev_parse_head(repo)
    assert verify.capture_diff(repo, base) == ""


def test_capture_diff_ignores_gitignored(project):
    repo = project.project
    base = verify.rev_parse_head(repo)
    # .gitignore (from the fixture) excludes .bmad-loop/runs/
    run_dir = repo / ".bmad-loop" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    assert verify.capture_diff(repo, base) == ""


def test_capture_diff_caps_large_untracked_file(project):
    repo = project.project
    base = verify.rev_parse_head(repo)
    (repo / "small.txt").write_text("tiny\n")
    (repo / "big.bin").write_text("x" * 200_000)  # ~200 KB

    diff = verify.capture_diff(repo, base, max_file_bytes=100_000)
    # the small file is captured in full; the big one is skipped with a marker
    assert "small.txt" in diff and "tiny" in diff
    assert "skipped untracked file 'big.bin'" in diff
    assert "x" * 1000 not in diff  # the oversized blob was not inlined
    assert "scm.failed_diff_unlimited" in diff  # marker tells the user how to lift the cap


def test_capture_diff_uncapped_includes_large_file(project):
    repo = project.project
    base = verify.rev_parse_head(repo)
    (repo / "big.bin").write_text("x" * 200_000)
    diff = verify.capture_diff(repo, base, max_file_bytes=None)  # no cap
    assert "big.bin" in diff and "skipped" not in diff
