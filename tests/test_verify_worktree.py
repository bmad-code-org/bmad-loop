"""Phase 2: low-level git worktree / branch / merge / diff primitives.

Exercised against the conftest `project` sandbox (a real git repo at
`project.project` with `main` checked out and one initial commit). These
helpers carry no engine wiring yet — they are the plumbing Phase 3 builds on.
"""

import pytest
from conftest import git

from bmad_loop import verify


def commit(repo, name, content="x\n", msg="work"):
    (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------- branches


def test_current_branch(project):
    assert verify.current_branch(project.project) == "main"


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
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    commit(wt, "f.txt", "f\n", "feat work")
    commit(repo, "m.txt", "m\n", "main work")  # main diverges → no ff possible

    with pytest.raises(verify.GitError):
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


def test_clean_incoming_collisions_refuses_stray_dirt(project, tmp_path):
    repo = project.project
    _branch_with(repo, tmp_path, adds={"leak.cs": "branch\n"})
    (repo / "leak.cs").write_text("editor leaked\n")  # within branch set
    (repo / "operator.txt").write_text("real work\n")  # stray, NOT in branch set

    with pytest.raises(verify.GitError) as ei:
        verify.clean_incoming_collisions(repo, "main", "feat")
    assert "operator.txt" in str(ei.value)
    # nothing was cleaned — both files remain
    assert (repo / "leak.cs").exists() and (repo / "operator.txt").exists()


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
