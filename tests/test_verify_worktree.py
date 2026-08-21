"""Phase 2: low-level git worktree / branch / merge / diff primitives.

Exercised against the conftest `project` sandbox (a real git repo at
`project.project` with `main` checked out and one initial commit). These
helpers carry no engine wiring yet — they are the plumbing Phase 3 builds on.
"""

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
    """`--ff-only` either fast-forwards or declines — it never starts a merge, so a
    diverged target is a pre-flight refusal with nothing to resolve (#619).

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

    The `not isinstance` assertion carries the whole test — `GitError` alone would
    pass for the pre-flight rows too, since `MergePreflightError` is a subclass.

    Ablation: classify with `_merge_in_progress` instead of `_index_unmerged` and
    the squash row fails alone — a conflicted `--squash` writes unmerged index
    stages but no MERGE_HEAD, so MERGE_HEAD reads every squash conflict as a
    refusal. The `merge` row cannot catch that: MERGE_HEAD is exact there."""
    repo = project.project
    _branch_with(repo, tmp_path, modifies={"src.txt": "branch\n"})
    commit(repo, "src.txt", "main change\n", "main edits src")

    with pytest.raises(verify.GitError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy)

    assert not isinstance(ei.value, verify.MergePreflightError)


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
    destroying operator data. The two labelling gaps this shape leaves behind are
    filed, not fixed here: #619 (the escalation calls a pre-flight refusal a "content
    conflict") and #623 (`merge-target-tolerated` is journaled for a stray that then
    blocked the merge)."""
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


def test_clean_incoming_collisions_still_refuses_tracked_stray(project, tmp_path):
    """The other half of #460: uncommitted changes to a TRACKED file outside the
    incoming set are not inert — git refuses a merge outright once the change is
    staged, and `merge --squash` + `commit` folds it into the story's commit — so
    they still refuse, and still clean nothing."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})  # `feat` never touches src.txt
    (repo / "leak.cs").write_text("editor leaked\n")  # within branch set
    (repo / "src.txt").write_text("operator edit\n")  # tracked-modified, NOT in the set

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat")
    assert "src.txt" in str(ei.value)
    assert "tracked" in str(ei.value)  # the refusal names which half it is about
    # nothing was cleaned — the leak still sits there and the edit is unreverted
    assert (repo / "leak.cs").exists()
    assert (repo / "src.txt").read_text() == "operator edit\n"


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
