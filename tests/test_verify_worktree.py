"""Phase 2: low-level git worktree / branch / merge / diff primitives.

Exercised against the conftest `project` sandbox (a real git repo at
`project.project` with `main` checked out and one initial commit). These
helpers carry no engine wiring yet — they are the plumbing Phase 3 builds on.
"""

import functools
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize("answer", ["git version 2.34.1", "no version reported"])
def test_worktree_list_keeps_the_newline_parse_below_git_2_36(
    project, tmp_path, monkeypatch, answer
):
    """`worktree list --porcelain -z` is a git 2.36 switch; the 2.34 support floor
    rejects it (`error: unknown switch `z'`, exit 129 — measured on Ubuntu 22.04's
    stock 2.34.1). Gated the other way every isolated-task resume reached
    `worktree_is_registered`, got a `GitError`, and escalated instead of reopening
    its recorded mount, and orphan reconciliation silently skipped its cleanup.
    An unreadable version answer takes the same arm: the generous failure here is
    the parse that works everywhere, not the one that needs the newer git.

    Ablation: make the `nul` gate unconditionally True and the argv assertion
    reddens; split on `\\0` regardless of the gate and the listing reddens."""
    repo = project.project
    wt = tmp_path / "plain"
    verify.worktree_add(repo, wt, "plain-path", "main")
    monkeypatch.setattr(verify, "git_below_floor", lambda _repo, _floor: answer)
    real = verify._run_git
    seen: list[list[str]] = []

    def spy(args, cwd, **kw):
        seen.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(verify, "_run_git", spy)

    listed = [path.resolve() for path in verify.worktree_list(repo)]

    assert seen and "-z" not in seen[-1] and "--porcelain" in seen[-1]
    assert listed == [repo.resolve(), wt.resolve()]


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 forbids newlines in filenames")
def test_worktree_list_preserves_newlines_in_paths(project, tmp_path):
    """NUL-delimited porcelain keeps a valid newline inside one path record."""
    repo = project.project
    wt = tmp_path / "wt\nline"
    verify.worktree_add(repo, wt, "newline-path", "main")

    assert wt.resolve() in [path.resolve() for path in verify.worktree_list(repo)]
    assert verify.worktree_is_registered(repo, wt)


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


@pytest.mark.parametrize("strategy", ["merge", "squash", "ff"])
def test_merge_operation_identity_resolves_exact_reflog_transition(project, tmp_path, strategy):
    repo = project.project
    wt = tmp_path / "receipt-wt"
    verify.worktree_add(repo, wt, "receipt-feat", "main")
    commit(wt, "receipt.txt", "feature\n", "receipt feature")
    if strategy != "ff":
        commit(repo, "target.txt", "target\n", "target advance")
    old = verify.rev_parse_head(repo)
    operation = f"operation-{strategy}"

    verify.merge_branch(
        repo,
        "receipt-feat",
        strategy=strategy,
        message="ordinary message",
        reflog_action=f"bmad-loop-integrate:{operation}",
    )

    update = verify.integration_ref_update(repo, "refs/heads/main", operation)
    assert update is not None
    assert update.old_revision == old
    assert update.new_revision == verify.rev_parse_head(repo)


def test_receipt_guarded_restore_preserves_unrelated_target_dirt(project, tmp_path):
    repo = project.project
    notes = repo / "operator-notes.txt"
    notes.write_text("baseline\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked operator notes")
    wt = tmp_path / "restore-wt"
    verify.worktree_add(repo, wt, "restore-feat", "main")
    commit(wt, "feature.txt", "feature\n", "feature work")
    old = verify.rev_parse_head(repo)
    notes.write_text("unrelated dirt\n")
    operation = "restore-operation"
    verify.merge_branch(
        repo,
        "restore-feat",
        strategy="merge",
        reflog_action=f"bmad-loop-integrate:{operation}",
    )
    update = verify.integration_ref_update(repo, "refs/heads/main", operation)
    assert update is not None

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=update.old_revision,
        new_revision=update.new_revision,
    )

    assert verify.rev_parse_head(repo) == old
    assert notes.read_text() == "unrelated dirt\n"
    assert not (repo / "feature.txt").exists()


def test_receipt_guarded_restore_never_discards_a_later_target_commit(project, tmp_path):
    repo = project.project
    wt = tmp_path / "moved-wt"
    verify.worktree_add(repo, wt, "moved-feat", "main")
    commit(wt, "feature.txt", "feature\n", "feature work")
    operation = "moved-operation"
    verify.merge_branch(
        repo,
        "moved-feat",
        strategy="merge",
        reflog_action=f"bmad-loop-integrate:{operation}",
    )
    update = verify.integration_ref_update(repo, "refs/heads/main", operation)
    assert update is not None
    commit(repo, "later.txt", "later\n", "later target commit")
    later = verify.rev_parse_head(repo)

    with pytest.raises(verify.IntegrationRestoreError, match="no restoration"):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=update.old_revision,
            new_revision=update.new_revision,
        )

    assert verify.rev_parse_head(repo) == later
    assert (repo / "later.txt").read_text() == "later\n"


def test_receipt_restore_cas_preserves_commit_winning_after_checkout_preparation(
    project, tmp_path, monkeypatch
):
    repo = project.project
    wt = tmp_path / "cas-race-wt"
    verify.worktree_add(repo, wt, "cas-race-feat", "main")
    commit(wt, "feature.txt", "feature\n", "feature work")
    operation = "cas-race-operation"
    verify.merge_branch(
        repo,
        "cas-race-feat",
        strategy="merge",
        reflog_action=f"bmad-loop-integrate:{operation}",
    )
    update = verify.integration_ref_update(repo, "refs/heads/main", operation)
    assert update is not None
    real_git = verify._git
    raced = []

    def concurrent_commit_before_cas(r, *args):
        if args[:2] == ("update-ref", "-m") and not raced:
            tree = git(repo, "rev-parse", f"{update.new_revision}^{{tree}}")
            later = git(
                repo,
                "commit-tree",
                tree,
                "-p",
                update.new_revision,
                "-m",
                "concurrent target commit",
            )
            git(repo, "update-ref", "refs/heads/main", later, update.new_revision)
            raced.append(later)
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", concurrent_commit_before_cas)

    with pytest.raises(verify.IntegrationRestoreError, match="later commit was preserved"):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=update.old_revision,
            new_revision=update.new_revision,
        )

    assert raced
    assert verify.ref_revision(repo, "refs/heads/main") == raced[0]
    assert git(repo, "cat-file", "-t", raced[0]) == "commit"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows paths are Unicode")
def test_receipt_restore_round_trips_non_utf8_changed_path(project):
    repo = project.project
    name = os.fsdecode(b"artifact-\xff.bin")
    artifact = repo / name
    artifact.write_bytes(b"old\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "non-utf8 baseline")
    old = verify.rev_parse_head(repo)
    artifact.write_bytes(b"integrated\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "non-utf8 integration")
    new = verify.rev_parse_head(repo)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
    )

    assert verify.rev_parse_head(repo) == old
    assert artifact.read_bytes() == b"old\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_receipt_sidecars_restore_bytes_trackedness_absence_and_dirty_state(project, tmp_path):
    repo = project.project
    tracked = repo / "tracked-dirt.txt"
    feature = repo / "feature.txt"
    tracked.write_bytes(b"tracked baseline\n")
    # No trailing newline on the tracked feature file: it comes back through a
    # git checkout (`restore --source=<old>`), and Git for Windows' system
    # `core.autocrlf=true` hands an LF-committed file back as CRLF. The newline
    # is not what this row grades; the restored bytes are.
    feature.write_bytes(b"feature baseline")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "receipt baseline")
    tracked.write_bytes(b"operator tracked dirt\x00")
    ignored = repo / "ignored.bin"
    ignored.write_bytes(b"operator ignored bytes\xff")
    absent = repo / "missing-parent" / "expected-absent.bin"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "a" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo,
        run_dir,
        operation,
        ("tracked-dirt.txt", "ignored.bin", "missing-parent/expected-absent.bin"),
    )
    old = verify.rev_parse_head(repo)
    feature.write_bytes(b"integrated feature")
    git(repo, "add", "--", feature)
    git(repo, "commit", "-q", "-m", "integrated target")
    new = verify.rev_parse_head(repo)
    tracked.write_bytes(b"hook rewrite\n")
    ignored.write_bytes(b"hook ignored rewrite\n")
    absent.parent.mkdir()
    absent.write_bytes(b"hook created\n")
    git(repo, "add", "-f", "--", tracked, ignored)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
    )

    assert verify.rev_parse_head(repo) == old
    assert tracked.read_bytes() == b"operator tracked dirt\x00"
    assert ignored.read_bytes() == b"operator ignored bytes\xff"
    assert git(repo, "ls-files", "--", "ignored.bin") == ""
    assert not absent.exists()
    assert feature.read_bytes() == b"feature baseline"
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert git(repo, "diff", "--name-only") == "tracked-dirt.txt"
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
    )
    absent.parent.mkdir()
    absent.write_bytes(b"reappeared after restore")
    assert not verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
    )


def test_receipt_snapshot_metadata_stays_bounded_for_large_target_file(project, tmp_path):
    target = project.project / "large.bin"
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    snapshots, _submodules = verify.capture_integration_state(
        project.project, run_dir, "b" * 32, ("large.bin",)
    )

    [entry] = snapshots
    assert entry["size"] == 2 * 1024 * 1024
    assert len(repr(entry)) < 500
    sidecar = run_dir / str(entry["sidecar"])
    assert sidecar.stat().st_size == 2 * 1024 * 1024


def test_receipt_capture_enforces_aggregate_sidecar_limit_before_mutation(project, tmp_path):
    repo = project.project
    (repo / "one.bin").write_bytes(b"a" * 8)
    (repo / "two.bin").write_bytes(b"b" * 8)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "9" * 32

    with pytest.raises(verify.IntegrationEvidenceError, match="aggregate artifact payload"):
        verify.capture_integration_state(
            repo,
            run_dir,
            operation,
            ("one.bin", "two.bin"),
            payload_max_bytes=15,
        )

    assert not (run_dir / "integration-snapshots" / operation).exists()


def test_receipt_restores_exact_staged_index_and_worktree_bytes(project, tmp_path):
    repo = project.project
    owned = repo / "owned.txt"
    owned.write_text("committed\n")
    git(repo, "add", "--", "owned.txt")
    git(repo, "commit", "-q", "-m", "index baseline")
    owned.write_text("operator staged\n")
    git(repo, "add", "--", "owned.txt")
    owned.write_text("operator worktree\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "8" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("owned.txt",)
    )
    old = verify.rev_parse_head(repo)
    (repo / "feature.txt").write_text("integrated\n")
    git(repo, "add", "--", "feature.txt")
    git(repo, "commit", "-q", "-m", "integrated")
    new = verify.rev_parse_head(repo)
    owned.write_text("hook staged\n")
    git(repo, "add", "--", "owned.txt")

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert git(repo, "show", ":owned.txt") == "operator staged"
    assert owned.read_text() == "operator worktree\n"
    assert git(repo, "diff", "--cached", "--name-only") == "owned.txt"
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )


def test_restoration_complete_ignores_operator_dirt_outside_the_receipt_inventory(
    project, tmp_path
):
    """`integration_restoration_complete` reads the worktree WHOLE-TREE on the
    receipt arm (`git diff` takes no stdin pathspec) and used to count every
    unstaged tracked edit outside the snapshot as residue — so a file the
    operator edited after the receipt was armed, which the restore never
    touched, turned a completed restore into "incomplete", at refusal time and
    on every replay until they cleared it (#796 review). The reading is now
    scoped to the receipt-attributable inventory, as the legacy arm's pathspec
    already scoped it; dirt ON that inventory still reads incomplete.

    Ablation: drop the `& set(paths)` and the bystander row reds."""
    repo = project.project
    bystander = repo / "bystander.txt"
    bystander.write_text("committed\n")
    (repo / "feature.txt").write_text("baseline\n")
    git(repo, "add", "--", "bystander.txt", "feature.txt")
    git(repo, "commit", "-q", "-m", "baseline")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "c" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("feature.txt",)
    )
    old = verify.rev_parse_head(repo)
    (repo / "feature.txt").write_text("integrated\n")
    git(repo, "add", "--", "feature.txt")
    git(repo, "commit", "-q", "-m", "integrated")
    new = verify.rev_parse_head(repo)
    # the operator's edit lands after the receipt was armed, on a path the
    # integration never touched
    bystander.write_text("operator edit during the merge window\n")

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert verify.rev_parse_head(repo) == old
    assert bystander.read_text() == "operator edit during the merge window\n"  # untouched
    assert git(repo, "diff", "--name-only") == "bystander.txt"
    complete = dict(
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )
    assert verify.integration_restoration_complete(repo, "refs/heads/main", **complete)
    # dirt on the inventory itself is still residue
    (repo / "feature.txt").write_text("residue\n")
    assert not verify.integration_restoration_complete(repo, "refs/heads/main", **complete)


def test_receipt_restores_intent_to_add_index_entry(project, tmp_path):
    repo = project.project
    candidate = repo / "intent.txt"
    candidate.write_text("operator bytes\n")
    git(repo, "add", "-N", "--", "intent.txt")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "7" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("intent.txt",)
    )
    git(repo, "add", "--", "intent.txt")

    verify.restore_integration_nonref_state(
        repo,
        "refs/heads/main",
        revision=verify.rev_parse_head(repo),
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    debug = git(repo, "ls-files", "--debug", "--", "intent.txt")
    assert "flags: 20004000" in debug
    assert candidate.read_text() == "operator bytes\n"


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_receipt_restores_extended_index_flags(project, tmp_path, flag):
    repo = project.project
    candidate = repo / "flagged.txt"
    candidate.write_text("tracked\n")
    git(repo, "add", "--", "flagged.txt")
    git(repo, "commit", "-q", "-m", "flag baseline")
    git(repo, "update-index", f"--{flag}", "--", "flagged.txt")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = ("a" if flag == "assume-unchanged" else "b") * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("flagged.txt",)
    )
    git(repo, "update-index", f"--no-{flag}", "--", "flagged.txt")

    verify.restore_integration_nonref_state(
        repo,
        "refs/heads/main",
        revision=verify.rev_parse_head(repo),
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert verify.integration_nonref_state_unchanged(
        repo,
        run_dir,
        snapshots,
        submodules,
        operation_identity=operation,
    )


def test_receipt_restores_conflicted_index_stages(project, tmp_path):
    repo = project.project
    conflict = repo / "conflict.txt"
    conflict.write_text("base\n")
    git(repo, "add", "--", "conflict.txt")
    git(repo, "commit", "-q", "-m", "conflict base")
    git(repo, "checkout", "-q", "-b", "other")
    conflict.write_text("theirs\n")
    git(repo, "commit", "-q", "-am", "theirs")
    git(repo, "checkout", "-q", "main")
    conflict.write_text("ours\n")
    git(repo, "commit", "-q", "-am", "ours")
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True, check=False)
    before = verify.git_bytes(repo, "ls-files", "--stage", "-z", "--", "conflict.txt").stdout
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "6" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("conflict.txt",)
    )
    conflict.write_text("resolved by hook\n")
    git(repo, "add", "--", "conflict.txt")

    verify.restore_integration_nonref_state(
        repo,
        "refs/heads/main",
        revision=verify.rev_parse_head(repo),
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    after = verify.git_bytes(repo, "ls-files", "--stage", "-z", "--", "conflict.txt").stdout
    assert after == before
    assert "<<<<<<< HEAD" in conflict.read_text()


def test_receipt_restore_transports_wide_pathsets_through_nul_stdin(project, monkeypatch):
    repo = project.project
    paths = [f"wide/{number:04d}-{'x' * 80}.txt" for number in range(300)]
    for rel in paths:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "wide baseline")
    old = verify.rev_parse_head(repo)
    for rel in paths:
        (repo / rel).write_text("new\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "wide integration")
    new = verify.rev_parse_head(repo)
    real_run_git = verify._run_git
    pathspec_calls = []

    def observe_pathspec_stdin(cmd, repo_path, **kwargs):
        if kwargs.get("input_data") is not None:
            pathspec_calls.append((list(cmd), kwargs["input_data"]))
        return real_run_git(cmd, repo_path, **kwargs)

    monkeypatch.setattr(verify, "_run_git", observe_pathspec_stdin)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
    )

    assert len(pathspec_calls) == 1
    argv, payload = pathspec_calls[0]
    assert "--pathspec-from-file=-" in argv and "--pathspec-file-nul" in argv
    assert all(rel not in argv for rel in paths)
    assert payload.count(b"\0") == len(paths)
    assert all((repo / rel).read_text() == "old\n" for rel in paths)


def test_collision_cleanup_rechecks_identity_at_each_mutation(project):
    repo = project.project
    collision = repo / "collision.txt"
    collision.write_bytes(b"captured")
    plan = verify.IncomingCollisionPlan(
        cleaned=("collision.txt",), tolerated=(), untracked=("collision.txt",)
    )

    def concurrent_writer(_path):
        collision.write_bytes(b"new operator bytes")
        return False

    with pytest.raises(verify.IntegrationCleanupChangedError):
        verify.apply_incoming_collision_plan(repo, plan, before_mutate=concurrent_writer)

    assert collision.read_bytes() == b"new operator bytes"


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "nul\0byte",
        "C:drive-relative",
        "NUL",
        "line\nfeed",
        "back\\slash",
        "nested/bad?.txt",
    ],
)
@pytest.mark.parametrize("win32_names", [False, True], ids=["posix", "win32"])
def test_receipt_schema_refuses_nonportable_paths_before_restore(
    project, tmp_path, monkeypatch, path, win32_names
):
    """Containment — an absolute path, a `..` segment, a NUL — is refused on
    every host. The Win32 name rules — reserved characters, device aliases, a
    drive prefix, a backslash separator — held on every host too, and on POSIX
    git permits `:`, `?`, `*`, `\\` and control characters in a name, the
    NUL-delimited plumbing round-trips them, and every modern bundle touching
    such a file paused before integration as malformed (Codex, #796 review).
    Those rules now apply on a Windows host alone (`WIN32_PATH_NAMES`).

    Ablation: apply the Win32 rules unconditionally and the `posix` rows for
    the Win32-only names red on the raise."""
    monkeypatch.setattr(verify, "WIN32_PATH_NAMES", win32_names)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    before = verify.rev_parse_head(project.project)
    everywhere = path in {"../escape", "/absolute", "nul\0byte"}

    if everywhere or win32_names:
        with pytest.raises(verify.IntegrationEvidenceError, match="path is malformed"):
            verify.validate_integration_state_schema(
                run_dir,
                [{"path": path, "state": "absent", "tracked": False}],
                [],
            )
    else:
        validated, _submodules = verify.validate_integration_state_schema(
            run_dir,
            [
                {
                    "path": path,
                    "state": "absent",
                    "tracked": False,
                    "index": {"entries": [], "intent_to_add": False},
                    "absent_parents": [],
                }
            ],
            [],
        )
        assert [entry["path"] for entry in validated] == [path]

    assert verify.rev_parse_head(project.project) == before


@pytest.mark.skipif(sys.platform == "win32", reason="the names are not Win32 names")
@pytest.mark.parametrize(
    "rel",
    ["a:/file", "dir/b:c/d:/file", "back\\slash/nested\\dir/file", "c:/deep/er/file"],
    ids=["drive-like", "colons", "backslashes", "drive-like-deep"],
)
def test_receipt_schema_reads_absent_parents_by_gits_slash_hierarchy(project, tmp_path, rel):
    """`absent_parents` is captured by git's slash hierarchy (`Path.parent`
    relative to the repository, `as_posix`), and the schema compared it under
    `PureWindowsPath`, whose `parent` of `a:/file` is the drive root `a:\\`
    while `a:` alone is drive-relative — so on POSIX, where `a:` is a plain
    directory name `_portable_integration_path` admits, the receipt the
    capture had just written was refused as malformed at the replay or
    restore that needed it (Codex, #796 review). A name holding a backslash
    is one segment to git and several to the Windows reading, the same way.
    The structural check now reads the same slash hierarchy the capture wrote.

    Ablation: compare under `PureWindowsPath` again and the `drive-like`,
    `backslashes` and `drive-like-deep` rows red on the raise (`colons`
    holds: a colon past the first segment is no drive to either reading;
    it pins the shape that already round-tripped)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "b" * 32
    snapshots, submodules = verify.capture_integration_state(
        project.project, run_dir, operation, (rel,)
    )
    [entry] = snapshots
    parents = rel.split("/")[:-1]
    assert entry["absent_parents"] == [
        "/".join(parents[:depth]) for depth in range(len(parents), 0, -1)
    ]

    validated, _submodules = verify.validate_integration_state_schema(
        run_dir, snapshots, submodules, operation
    )

    assert [item["path"] for item in validated] == [rel]
    assert validated[0]["absent_parents"] == entry["absent_parents"]


def _add_test_submodule(repo, tmp_path):
    origin = tmp_path / "sub-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "submodule old\n", "submodule baseline")
    old = verify.rev_parse_head(origin)
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "module")
    git(repo, "commit", "-q", "-m", "add populated submodule")
    return origin, repo / "module", old


def test_receipt_restores_populated_submodule_checkout(project, tmp_path):
    repo = project.project
    origin, checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ())
    assert submodules == [
        {"path": "module", "head": old_submodule, "gitlink": old_submodule, "flags": "0"}
    ]
    old = verify.rev_parse_head(repo)
    commit(origin, "payload.txt", "submodule new\n", "advance submodule")
    new_submodule = verify.rev_parse_head(origin)
    git(checkout, "fetch", "-q", "origin")
    git(checkout, "checkout", "-q", "--detach", new_submodule)
    git(repo, "add", "--", "module")
    git(repo, "commit", "-q", "-m", "integrated submodule")
    new = verify.rev_parse_head(repo)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
    )

    assert verify.rev_parse_head(repo) == old
    assert verify.rev_parse_head(checkout) == old_submodule
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "checked-path"])
@pytest.mark.parametrize(
    "shape",
    [
        "file-to-directory",
        "file-to-deep-directory",
        "symlink-to-directory",
        "dangling-symlink-to-directory",
        "resolving-symlink-to-directory",
        "escaping-symlink-to-directory",
        "directory-to-file",
    ],
)
def test_receipt_captures_and_restores_a_tracked_entry_type_change(
    project, tmp_path, monkeypatch, shape, anchored
):
    """`branch_incoming_paths` names both sides of a tracked file/directory
    transition (`a` deleted, `a/b` added), and capturing `a/b` while `a` is
    still a file `lstat`s through a file: `NotADirectoryError`, which the
    capture treated as a fault rather than as absence, so every receipt-backed
    integration of that valid transition paused before the merge and paused
    again on every resume (Codex, #796 review). A leaf beneath a file is
    absent — and the restore, which opens every snapshot's parent as a
    directory before writing (and `mkdir`s it on the checked-path host), must
    leave such an entry alone once `git restore` has put the file back: it is
    absent by topology, nothing to open, nothing to remove. The reverse
    transition (`d/x` deleted, `d` now a file) captures `d` as a tracked
    directory (no snapshot of its own) and `d/x` as the reversible leaf. A
    leaf deeper down (`a/b/c`) records `a/b` as a proved-absent parent, and
    once the file is back that parent is absent by topology too: the
    anchored restore's parent removal opened `a` as a directory and crashed
    on `NotADirectoryError`. A symlink in the file's place (`a -> src.txt`,
    then `a/b`) is captured as `symlink` and put back the same way — and the
    restore's redirection probe, which refuses any symlink on the way to a
    snapshot, must know that this one is the receipt's own restored shape. A
    DANGLING symlink there (`a -> missing`) is a valid tracked entry git
    replaces the same way, yet capturing `a/b` resolved the ancestor
    `strict=True` and refused "unavailable parent" before the merge (Codex,
    #796 review): nothing can be reached through a dangling link, so the leaf
    beneath it is absent by topology and the link's own parent is what
    confines it. A link that RESOLVES to a directory already holding the
    leaf's name (`a -> dir`, `dir/b` tracked, incoming `a/b`) is the same
    transition, yet the capture dereferenced it — `a/b` read as the existing
    `dir/b` — and the snapshot stream refused `a` as a redirected parent
    (Codex, #796 review): git tracks no path through a symlink, so a leaf
    beneath a link the incoming set replaces is absent by topology wherever
    the link points, and stays absent by topology once the link is back —
    the completeness reading must not reach through it either. Wherever it
    points includes outside the repository: the confinement reading used to
    follow a resolving link and refuse the leaf as "escaped the repository",
    yet nothing is read or written through the link — it is captured and
    put back as bytes under its own path — so the link's own parent is what
    confines the leaf beneath it.

    Ablation: catch `FileNotFoundError` alone in the capture and the
    `file-to-directory` row reds on the raise; drop the topology skip from
    the restore and it reds on the restore's parent opening; skip the
    topology test in the absent-parents removal and the anchored deep row
    reds on `NotADirectoryError`; ignore `captured_links` in
    `_absent_beneath_a_file` and the symlink rows red on "redirected"."""
    repo = project.project
    if not anchored:
        monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    leaf = "a/b/c" if shape == "file-to-deep-directory" else "a/b"
    outside = tmp_path / "outside"
    link_targets = {
        "symlink-to-directory": "src.txt",
        "dangling-symlink-to-directory": "missing",
        "resolving-symlink-to-directory": "dir",
        "escaping-symlink-to-directory": str(outside),
    }
    if shape in link_targets:
        if shape == "resolving-symlink-to-directory":
            (repo / "dir").mkdir()
            (repo / "dir" / "b").write_text("reachable through the link\n")
            git(repo, "add", "--", "dir/b")
        elif shape == "escaping-symlink-to-directory":
            outside.mkdir()
            (outside / "b").write_text("outside the repository\n")
        os.symlink(link_targets[shape], repo / "a")
        git(repo, "add", "--", "a")
        git(repo, "commit", "-q", "-m", "a is a symlink")
        incoming = ("a", leaf)
    elif shape != "directory-to-file":
        (repo / "a").write_text("a file\n")
        git(repo, "add", "--", "a")
        git(repo, "commit", "-q", "-m", "a is a file")
        incoming = ("a", leaf)
    else:
        (repo / "d").mkdir()
        (repo / "d" / "x").write_text("a directory\n")
        git(repo, "add", "--", "d/x")
        git(repo, "commit", "-q", "-m", "d is a directory")
        incoming = ("d/x", "d")
    old = verify.rev_parse_head(repo)

    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, incoming)

    by_path = {entry["path"]: entry for entry in snapshots}
    if shape != "directory-to-file":
        assert by_path["a"]["state"] == (
            "symlink" if shape.endswith("symlink-to-directory") else "regular"
        )
        assert by_path[leaf]["state"] == "absent"
        assert by_path[leaf]["absent_parents"] == (
            ["a/b"] if shape == "file-to-deep-directory" else []
        )
        git(repo, "rm", "-q", "--", "a")
        (repo / leaf).parent.mkdir(parents=True)
        (repo / leaf).write_text("a directory\n")
        git(repo, "add", "--", leaf)
        git(repo, "commit", "-q", "-m", "integrated: a becomes a directory")
    else:
        assert set(by_path) == {"d/x"} and by_path["d/x"]["state"] == "regular"
        git(repo, "rm", "-q", "--", "d/x")
        (repo / "d").write_text("a file\n")
        git(repo, "add", "--", "d")
        git(repo, "commit", "-q", "-m", "integrated: d becomes a file")
    new = verify.rev_parse_head(repo)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )

    assert verify.rev_parse_head(repo) == old
    assert git(repo, "status", "--porcelain", "-uall") == ""
    if shape in link_targets:
        # The link's own bytes, back under its own path. On Windows CPython
        # creates an absolute-target link under the extended-length `\\?\`
        # prefix and `os.readlink` returns that substitute name (3.8+), so the
        # escaping row's target reads back prefixed there — the same bytes git
        # captured and the restore put back; the prefix is not the restore's.
        assert os.readlink(repo / "a").removeprefix("\\\\?\\") == link_targets[shape]
        if shape == "resolving-symlink-to-directory":
            assert (repo / "dir" / "b").read_text() == "reachable through the link\n"
        elif shape == "escaping-symlink-to-directory":
            assert (outside / "b").read_text() == "outside the repository\n"
    elif shape != "directory-to-file":
        assert (repo / "a").read_text() == "a file\n"
    else:
        assert (repo / "d" / "x").read_text() == "a directory\n"
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )


def test_receipt_refuses_a_leaf_beneath_a_symlink_the_integration_keeps(project, tmp_path):
    """A leaf beneath a symlink is absent by topology only when the same
    incoming set names the link: the commit that adds `a/b` replaces `a`, and
    git refuses `a/b` against a tracked `a` it keeps. A snapshot set naming
    `a/b` beneath a link it does not name is not the merge's shape, and the
    capture refuses it ahead of any mutation rather than reading through the
    link (Codex, #796 review).

    Ablation: drop the refusal and the capture records the leaf absent."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "dir").mkdir()
    (repo / "dir" / "b").write_text("reachable through the link\n")
    os.symlink("dir", repo / "a")
    git(repo, "add", "--", "dir/b", "a")
    git(repo, "commit", "-q", "-m", "a is a symlink")

    with pytest.raises(verify.IntegrationEvidenceError, match="beneath a symlink"):
        verify.capture_integration_state(repo, run_dir, "c" * 32, ("a/b",))


def _uninitialized_submodule(repo, tmp_path):
    """A populated submodule deinitialised: gitlink in the index, `.gitmodules`
    in place, and an empty directory with no `.git` at the path — the shape a
    clone without `--recurse-submodules` leaves every gitlink in."""
    origin, checkout, old = _add_test_submodule(repo, tmp_path)
    git(repo, "submodule", "deinit", "-q", "-f", "--", "module")
    assert checkout.is_dir() and not any(checkout.iterdir())
    return origin, checkout, old


def test_receipt_captures_an_uninitialized_submodule_as_unpopulated(project, tmp_path):
    """A target cloned without `--recurse-submodules` holds every gitlink as an
    empty directory with no `.git` of its own. The capture probed each one for
    its superproject, git discovered the enclosing repository instead (whose
    superproject is nothing), and every modern bundle integration into such a
    target refused with "changed ownership" over a submodule the unit never
    touched (Codex, #796 review). An empty directory at an indexed gitlink is
    an unpopulated checkout, and the receipt records it as such (`head`
    None) rather than skipping it: skipped, the fact that nothing stood there
    was lost, and a checkout a target hook made there survived a refusal's
    restore to become the next capture's baseline (Codex, #796 review). A
    populated directory that is not this repository's checkout still is not
    a submodule.

    Ablation: skip the empty directory and this reds on the entry."""
    repo = project.project
    _origin, checkout, old = _uninitialized_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ())

    assert snapshots == []
    assert submodules == [{"path": "module", "head": None, "gitlink": old, "flags": "0"}]
    (checkout / "stray.txt").write_text("not a checkout\n")
    with pytest.raises(verify.IntegrationEvidenceError, match="changed ownership"):
        verify.capture_integration_state(repo, run_dir, "d" * 32, ())


@pytest.mark.parametrize("dirty", [False, True])
def test_receipt_removes_a_checkout_a_hook_made_at_an_unpopulated_gitlink(project, tmp_path, dirty):
    """The unit updates an unpopulated gitlink and a target hook runs
    `submodule update --init`: the checkout it makes is attempt-era in full —
    the receipt proved the directory empty — and a refusal's restore removes
    it whole, dirty or not, leaving git's own empty directory; `git restore`
    moves only the superproject's gitlink and, under `ignore = all`, the
    completeness diff would never have seen the leftover (Codex, #796
    review). The pre-restore reading calls the populated directory changed
    receipt-owned state, which is what routes the refusal.

    Ablation: skip unpopulated entries in the restore and both rows red on the
    checkout still standing."""
    repo = project.project
    origin, checkout, old_submodule = _uninitialized_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ("module",))
    old = verify.rev_parse_head(repo)
    commit(origin, "payload.txt", "submodule new\n", "advance submodule")
    new_submodule = verify.rev_parse_head(origin)
    git(repo, "update-index", "--cacheinfo", f"160000,{new_submodule},module")
    git(repo, "commit", "-q", "-m", "integrated: update module gitlink")
    new = verify.rev_parse_head(repo)
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "-q",
        "--",
        "module",
    )
    assert verify.rev_parse_head(checkout) == new_submodule
    if dirty:
        (checkout / "hook.txt").write_text("target hook output\n")
    assert not verify.integration_nonref_state_unchanged(
        repo, run_dir, snapshots, submodules, operation_identity="c" * 32
    )

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )

    assert verify.rev_parse_head(repo) == old
    assert checkout.is_dir() and not any(checkout.iterdir())
    assert git(repo, "ls-files", "--stage", "--", "module").startswith(f"160000 {old_submodule}")
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )


def test_receipt_never_removes_a_foreign_directory_at_an_unpopulated_gitlink(project, tmp_path):
    """Ownership is the removal authority here as at a proved-absent path: a
    fresh repository of its own at the unpopulated gitlink is not this
    repository's checkout, and the restore refuses before mutating anything."""
    repo = project.project
    _origin, checkout, _old_submodule = _uninitialized_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ("module",))
    old = verify.rev_parse_head(repo)
    commit(repo, "other.txt", "integrated\n", "integrated: unrelated")
    new = verify.rev_parse_head(repo)
    git(checkout, "init", "-q")
    git(checkout, "config", "user.email", "test@example.com")
    git(checkout, "config", "user.name", "Test")
    commit(checkout, "payload.txt", "foreign\n", "hook-made repository")

    with pytest.raises(verify.IntegrationEvidenceError, match="changed ownership"):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=old,
            new_revision=new,
            run_dir=run_dir,
            snapshots=snapshots,
            submodules=submodules,
            operation_identity="c" * 32,
        )

    assert verify.rev_parse_head(repo) == new
    assert (checkout / "payload.txt").read_text() == "foreign\n"


@pytest.mark.parametrize("state", ["clean", "ignored-file", "moved-head"])
def test_integrated_unpopulated_gitlink_a_hook_populated_reads_as_introduced(
    project, tmp_path, state
):
    """An incoming gitlink the receipt recorded unpopulated stands where the
    receipt proved an empty directory, so a checkout there after the hooks is
    attempt-era in full and is read as an introduced one: owned, at the
    gitlink, clean with ignored entries counted."""
    repo = project.project
    origin, checkout, _old_submodule = _uninitialized_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ("module",))
    commit(origin, "payload.txt", "submodule new\n", "advance submodule")
    new_submodule = verify.rev_parse_head(origin)
    git(repo, "update-index", "--cacheinfo", f"160000,{new_submodule},module")
    git(repo, "commit", "-q", "-m", "integrated: update module gitlink")
    integrated = verify.rev_parse_head(repo)
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "-q",
        "--",
        "module",
    )
    if state == "ignored-file":
        exclude = Path(git(checkout, "rev-parse", "--git-path", "info/exclude"))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.tmp\n")
        (checkout / "hook.tmp").write_text("target hook output\n")
        assert git(checkout, "status", "--porcelain", "-uall") == ""
    elif state == "moved-head":
        commit(origin, "payload.txt", "submodule newer\n", "advance again")
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "--detach", verify.rev_parse_head(origin))

    if state == "clean":
        assert (
            verify.validate_integrated_submodule_state(
                repo, submodules, prospective_paths=("module",), revision=integrated
            )
            == ()
        )
    else:
        with pytest.raises(verify.IntegrationEvidenceError, match="submodule checkout"):
            verify.validate_integrated_submodule_state(
                repo, submodules, prospective_paths=("module",), revision=integrated
            )


@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "checked-path"])
@pytest.mark.parametrize("residue", ["none", "ignored-file", "ignored-directory"])
def test_integrated_directory_replacing_an_unpopulated_gitlink_is_walked(
    project, tmp_path, monkeypatch, residue, anchored
):
    """The incoming commit replaces an unpopulated gitlink with a tracked
    directory: the submodule reading accepts the directory as the commit's
    own (no `.git` inside), the superproject's diff and status readings never
    list an ignored entry, and the walk had no root — the empty directory
    existed at capture, so the new descendants record no absent parent, and
    the submodule capture snapshots nothing at a gitlink (Codex, #796
    review). The receipt recorded the gitlink unpopulated, though, so a
    captured-unpopulated gitlink the commit holds only as a prefix is a root
    of the walk like any directory the commit created, and a hook's
    gitignored write under it is refused by path. The restore of a clean
    replacement puts the gitlink and its empty directory back, `git restore`
    having taken the commit's own files; the pre-restore reading, which
    called that plain directory a checkout of changed ownership, lets it
    through for exactly that. Residue left in it is the proved-absent
    directory's doctrine: nothing the restore cannot attribute is removed,
    and it refuses with the path named, the residue in place.

    Ablation: leave the unpopulated entries out of the roots and the residue
    rows red on the drift reading; remove the unowned-state test from the
    restore and they red on the residue removed."""
    repo = project.project
    if not anchored:
        monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    _origin, checkout, old_submodule = _uninitialized_submodule(repo, tmp_path)
    (repo / ".gitignore").write_text("*.tmp\ncache/\n")
    git(repo, "add", "--", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore hook output")
    old = verify.rev_parse_head(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("module", "module/x")
    )
    assert submodules[0]["head"] is None
    assert [entry["path"] for entry in snapshots] == ["module/x"]
    git(repo, "rm", "-q", "--cached", "--", "module")
    git(repo, "config", "-f", ".gitmodules", "--remove-section", "submodule.module")
    (checkout / "x").write_text("the commit's own\n")
    git(repo, "add", "--", ".gitmodules", "module/x")
    git(repo, "commit", "-q", "-m", "integrated: module becomes a directory")
    integrated = verify.rev_parse_head(repo)
    if residue == "ignored-file":
        (checkout / "cache.tmp").write_text("target hook output\n")
        expected = ("module/cache.tmp",)
    elif residue == "ignored-directory":
        (checkout / "cache").mkdir()
        (checkout / "cache" / "y").write_text("target hook output\n")
        expected = ("module/cache",)
    else:
        expected = ()
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert (
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=("module", "module/x"), revision=integrated
        )
        == ()
    )
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("module", "module/x")) == ()

    assert (
        verify.integrated_introduced_directories_drift(
            repo,
            integrated,
            run_dir,
            snapshots,
            submodules=submodules,
            operation_identity="e" * 32,
        )
        == expected
    )

    restore = functools.partial(
        verify.restore_integration_ref,
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )
    if residue != "none":
        with pytest.raises(verify.IntegrationRestoreError, match="unowned state: module"):
            restore()
        assert verify.rev_parse_head(repo) == integrated
        assert sorted(entry.name for entry in checkout.iterdir()) == [expected[0].split("/")[1]]
        return
    restore()

    assert verify.rev_parse_head(repo) == old
    assert checkout.is_dir() and not any(checkout.iterdir())
    assert git(repo, "ls-files", "--stage", "--", "module").startswith(f"160000 {old_submodule}")
    assert git(repo, "status", "--porcelain", "-uall", "--ignored") == ""
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )


def test_receipt_detects_index_only_submodule_gitlink_drift(project, tmp_path):
    repo = project.project
    origin, checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "c" * 32
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, operation, ())
    commit(origin, "payload.txt", "new gitlink\n", "advance gitlink")
    new_submodule = verify.rev_parse_head(origin)
    git(repo, "update-index", "--cacheinfo", "160000", new_submodule, "module")

    assert verify.rev_parse_head(checkout) == old_submodule
    assert not verify.integration_nonref_state_unchanged(
        repo,
        run_dir,
        snapshots,
        submodules,
        operation_identity=operation,
    )


@pytest.mark.parametrize("replacement", [False, True])
def test_receipt_restores_deleted_or_replaced_old_submodule_from_old_revision(
    project, tmp_path, replacement
):
    repo = project.project
    _origin, checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "5" * 32
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, operation, ("module",))
    old = verify.rev_parse_head(repo)
    git(repo, "rm", "-q", "-f", "--", "module")
    if replacement:
        (repo / "module").write_text("replacement file\n")
        git(repo, "add", "--", "module")
    git(repo, "commit", "-q", "-m", "delete or replace submodule")
    new = verify.rev_parse_head(repo)

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert checkout.is_dir()
    assert verify.rev_parse_head(checkout) == old_submodule
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("dirty", [False, True])
def test_receipt_removes_newly_introduced_submodule_checkout(project, tmp_path, dirty):
    """The receipt proved `new-module` absent, so a submodule checkout of
    this repository standing there after the attempt is attempt-era in
    full — a target hook's write into it included (`dirty`; Codex, #796
    review) — and the restore removes it. Ablation: require the checkout
    clean again and the `dirty` row reds on "contains unowned state"."""
    repo = project.project
    origin = tmp_path / "new-sub-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "new checkout\n", "new submodule")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "4" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("new-module",)
    )
    old = verify.rev_parse_head(repo)
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(origin),
        "new-module",
    )
    git(repo, "commit", "-q", "-m", "introduce submodule")
    new = verify.rev_parse_head(repo)
    if dirty:
        (repo / "new-module" / "hook.txt").write_text("target hook output\n")

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert not (repo / "new-module").exists()
    assert git(repo, "ls-files", "--", "new-module") == ""


def test_receipt_never_removes_a_foreign_directory_at_an_absent_path(project, tmp_path):
    """Ownership, not emptiness, is the removal authority: a directory at a
    receipt-proved-absent path that is no submodule checkout of this
    repository — a plain directory with files, or a fresh repository of its
    own — may be fresh operator state and refuses the restore."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "4" * 32
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, operation, ("fresh",))
    old = verify.rev_parse_head(repo)
    commit(repo, "src.txt", "integrated\n", "integrated")
    new = verify.rev_parse_head(repo)
    fresh = repo / "fresh"
    fresh.mkdir()
    (fresh / "operator.txt").write_text("operator state\n")

    with pytest.raises(verify.IntegrationRestoreError, match="unowned state"):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=old,
            new_revision=new,
            run_dir=run_dir,
            snapshots=snapshots,
            submodules=submodules,
            operation_identity=operation,
        )

    assert (fresh / "operator.txt").read_text() == "operator state\n"


def test_receipt_refuses_redirected_submodule_before_external_mutation(project, tmp_path):
    repo = project.project
    origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "d" * 32, ())
    old = verify.rev_parse_head(repo)
    commit(repo, "feature.txt", "integrated\n", "integrated target")
    new = verify.rev_parse_head(repo)
    sibling = repo / "sibling-checkout"
    git(repo, "-c", "protocol.file.allow=always", "clone", "-q", str(origin), str(sibling))
    parked = repo / "module-parked"
    checkout.rename(parked)
    checkout.symlink_to(sibling, target_is_directory=True)
    external_head = verify.rev_parse_head(sibling)

    with pytest.raises(verify.IntegrationEvidenceError, match="indexed location"):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=old,
            new_revision=new,
            run_dir=run_dir,
            snapshots=snapshots,
            submodules=submodules,
        )

    assert verify.rev_parse_head(repo) == new
    assert verify.rev_parse_head(sibling) == external_head
    checkout.unlink()
    shutil.move(parked, checkout)


@pytest.mark.skipif(sys.platform == "win32", reason="dirfd restoration is POSIX-only")
def test_receipt_restore_parent_redirect_never_writes_outside_or_moves_ref(
    project, tmp_path, monkeypatch
):
    """A parent swapped for a symlink out of the repository AFTER the anchored
    restore's ancestry preflight: the descriptor-relative writes land in the
    directory that was walked, never through the link, and the reading that
    follows refuses before the ref can be reset. That reading is the
    completeness reading, which never reaches through a symlink on the way
    to an entry — the confinement reading used to follow the link and refuse
    the operand as "escaped the repository", which was a dereference (Codex,
    #796 review); now it confines by the link's parent and the completeness
    reading answers "not restored" without reading a byte beyond it."""
    repo = project.project
    owned = repo / "nested" / "owned.bin"
    owned.parent.mkdir()
    owned.write_bytes(b"operator bytes")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "redirect baseline")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "3" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("nested/owned.bin",)
    )
    old = verify.rev_parse_head(repo)
    (repo / "feature.txt").write_text("integrated\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "redirect integration")
    new = verify.rev_parse_head(repo)
    owned.write_bytes(b"hook rewrite")
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "owned.bin"
    outside.write_bytes(b"outside sentinel")
    real_copy = verify._copy_sidecar_to_target
    swapped = []

    def redirect_after_preflight(*args, **kwargs):
        if not swapped:
            parked = repo / "nested-parked"
            (repo / "nested").rename(parked)
            (repo / "nested").symlink_to(external, target_is_directory=True)
            swapped.append(parked)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(verify, "_copy_sidecar_to_target", redirect_after_preflight)

    with pytest.raises(
        verify.IntegrationRestoreError, match="changed before the target ref could be restored"
    ):
        verify.restore_integration_ref(
            repo,
            "refs/heads/main",
            old_revision=old,
            new_revision=new,
            run_dir=run_dir,
            snapshots=snapshots,
            submodules=submodules,
            operation_identity=operation,
        )

    assert verify.rev_parse_head(repo) == new
    assert outside.read_bytes() == b"outside sentinel"
    (repo / "nested").unlink()
    swapped[0].rename(repo / "nested")


def test_checked_path_restore_refuses_a_parent_swapped_for_a_symlink(
    project, tmp_path, monkeypatch
):
    """The checked-path restore (no descriptor-relative syscalls: Windows) writes
    by path, so a parent swapped for a symlink between capture and restore
    would have every write land through the link. The confinement reading
    never follows a link — it confines the operand by the link's parent
    (Codex, #796 review) — so the restore's own ancestry preflight is the
    reading that sees one, and it refuses before the first mutation: nothing
    behind the link is written, and a leaf the receipt captured absent
    beneath its own captured link is left to topology as before.

    Ablation: drop the preflight and the restore writes the sidecar bytes
    through the link into the external directory."""
    monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    repo = project.project
    owned = repo / "nested" / "owned.bin"
    owned.parent.mkdir()
    owned.write_bytes(b"operator bytes")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "redirect baseline")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "4" * 32, ("nested/owned.bin",)
    )
    revision = verify.rev_parse_head(repo)
    external = tmp_path / "external"
    external.mkdir()
    (external / "owned.bin").write_bytes(b"outside sentinel")
    parked = repo / "nested-parked"
    (repo / "nested").rename(parked)
    (repo / "nested").symlink_to(external, target_is_directory=True)

    with pytest.raises(verify.IntegrationRestoreError, match="redirected"):
        verify.restore_integration_nonref_state(
            repo,
            "refs/heads/main",
            revision=revision,
            run_dir=run_dir,
            snapshots=snapshots,
            submodules=submodules,
            operation_identity="4" * 32,
        )

    assert (external / "owned.bin").read_bytes() == b"outside sentinel"
    assert sorted(path.name for path in external.iterdir()) == ["owned.bin"]
    (repo / "nested").unlink()
    parked.rename(repo / "nested")


@pytest.mark.parametrize("tracked", [False, True])
def test_receipt_snapshots_and_restores_tolerated_symlink_identity(project, tmp_path, tracked):
    repo = project.project
    link = repo / "operator-link"
    link.symlink_to("baseline-target")
    if tracked:
        git(repo, "add", "--", "operator-link")
        git(repo, "commit", "-q", "-m", "track operator symlink")
        link.unlink()
        link.symlink_to("operator-target")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    operation = "e" * 32
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, operation, ("operator-link",)
    )
    link.unlink()
    link.symlink_to("hook-target")

    verify.restore_integration_nonref_state(
        repo,
        "refs/heads/main",
        revision=verify.rev_parse_head(repo),
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity=operation,
    )

    assert link.is_symlink()
    assert os.readlink(link) == ("operator-target" if tracked else "baseline-target")
    assert verify.path_tracked(repo, "operator-link") is tracked


def test_receipt_capture_rejects_redirected_roots_without_external_writes(project, tmp_path):
    run_dir = tmp_path / "run"
    external = tmp_path / "external"
    run_dir.mkdir()
    external.mkdir()
    (run_dir / "integration-snapshots").symlink_to(external, target_is_directory=True)

    with pytest.raises(verify.IntegrationEvidenceError, match="redirected"):
        verify.capture_integration_state(project.project, run_dir, "f" * 32, ())

    assert not any(external.iterdir())
    (run_dir / "integration-snapshots").unlink()
    parent = run_dir / "integration-snapshots"
    parent.mkdir()
    (parent / ("f" * 32)).symlink_to(external, target_is_directory=True)
    with pytest.raises(verify.IntegrationEvidenceError, match="already exists"):
        verify.capture_integration_state(project.project, run_dir, "f" * 32, ())
    assert not any(external.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="dirfd capture is POSIX-only")
def test_receipt_capture_parent_redirect_never_writes_outside(project, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    external = tmp_path / "external"
    run_dir.mkdir()
    external.mkdir()
    (project.project / "captured.bin").write_bytes(b"captured")
    operation = "2" * 32
    real_replace = verify.os.replace
    swapped = []

    def redirect_before_publish(source, destination, *args, **kwargs):
        if str(source).startswith(".capture-") and not swapped:
            root = run_dir / "integration-snapshots" / operation
            parked = root.with_name(operation + "-parked")
            root.rename(parked)
            root.symlink_to(external, target_is_directory=True)
            swapped.append(parked)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(verify.os, "replace", redirect_before_publish)

    with pytest.raises(verify.IntegrationEvidenceError, match="changed during capture"):
        verify.capture_integration_state(project.project, run_dir, operation, ("captured.bin",))

    assert not any(external.iterdir())


def test_receipt_discard_refuses_redirected_snapshot_parent(tmp_path):
    run_dir = tmp_path / "run"
    redirected = run_dir / "redirected" / "integration-snapshots"
    operation = "f" * 32
    (redirected / operation).mkdir(parents=True)
    marker = redirected / operation / "keep.bin"
    marker.write_bytes(b"keep")
    (run_dir / "integration-snapshots").symlink_to(
        redirected.relative_to(run_dir), target_is_directory=True
    )

    verify.discard_integration_state(run_dir, {"operation_identity": operation})

    assert marker.read_bytes() == b"keep"


def test_receipt_capture_removes_partial_operation_directory(project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (project.project / "captured.txt").write_text("captured\n")
    (project.project / "not-a-file").mkdir()
    operation = "1" * 32

    with pytest.raises(verify.IntegrationEvidenceError, match="not a file"):
        verify.capture_integration_state(
            project.project,
            run_dir,
            operation,
            ("captured.txt", "not-a-file"),
        )

    assert not (run_dir / "integration-snapshots" / operation).exists()


def test_receipt_schema_binds_sidecar_to_operation_and_path(project, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (project.project / "one.txt").write_text("one\n")
    (project.project / "two.txt").write_text("two\n")
    operation = "2" * 32
    snapshots, submodules = verify.capture_integration_state(
        project.project, run_dir, operation, ("one.txt", "two.txt")
    )

    with pytest.raises(verify.IntegrationEvidenceError, match="another operation"):
        verify.validate_integration_state_schema(run_dir, snapshots, submodules, "3" * 32)

    swapped = [dict(entry) for entry in snapshots]
    swapped[0]["sidecar"], swapped[1]["sidecar"] = (
        swapped[1]["sidecar"],
        swapped[0]["sidecar"],
    )
    with pytest.raises(verify.IntegrationEvidenceError, match="another operation"):
        verify.validate_integration_state_schema(run_dir, swapped, submodules, operation)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only git file names")
@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "checked-path"])
def test_receipt_captures_and_restores_a_posix_only_git_name(
    project, tmp_path, monkeypatch, anchored
):
    """A tracked file whose name git permits on POSIX alone (`odd:name?*.txt`,
    with a backslash and a tab in it) is captured, refused and restored like
    any other: the sidecar is named by the path's digest, every git reading is
    NUL-delimited and literal, and the receipt's own containment rules are
    all that the name is held to on this host (Codex, #796 review)."""
    if not anchored:
        monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    name = "odd:na\\me?*\t.txt"
    (repo / name).write_bytes(b"operator bytes")
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", "posix-only name")
    old = verify.rev_parse_head(repo)

    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "b" * 32, (name,))

    [entry] = snapshots
    assert entry["path"] == name and entry["state"] == "regular"
    (repo / name).write_bytes(b"integrated bytes")
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", "integrated")
    new = verify.rev_parse_head(repo)
    (repo / name).write_bytes(b"hook rewrite")

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="b" * 32,
    )

    assert verify.rev_parse_head(repo) == old
    assert (repo / name).read_bytes() == b"operator bytes"
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="b" * 32,
    )


def test_receipt_schema_refuses_git_administration_operands(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(verify.IntegrationEvidenceError, match="path is malformed"):
        verify.validate_integration_state_schema(
            run_dir,
            [{"path": ".git/config", "state": "absent", "tracked": False}],
            [],
        )


@pytest.mark.skipif(
    not hasattr(os, "O_DIRECTORY"),
    reason="directory fsync is POSIX-only: `_fsync_directory` is a no-op without "
    "os.O_DIRECTORY, and the non-dirfd sidecar writers never call it",
)
def test_receipt_capture_fsyncs_sidecar_directory(project, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (project.project / "durable.txt").write_text("durable\n")
    fsynced = []
    real_fsync = verify._fsync_directory

    def record(directory):
        fsynced.append(directory)
        real_fsync(directory)

    monkeypatch.setattr(verify, "_fsync_directory", record)
    operation = "4" * 32
    verify.capture_integration_state(project.project, run_dir, operation, ("durable.txt",))

    assert run_dir / "integration-snapshots" / operation in fsynced


def test_duplicate_operation_tagged_reflog_updates_are_ambiguous(project):
    repo = project.project
    operation = "duplicate-operation"
    first_old = verify.rev_parse_head(repo)
    (repo / "first.txt").write_text("first\n")
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    first_new = git(repo, "commit-tree", tree, "-p", first_old, "-m", "first")
    action = f"bmad-loop-integrate:{operation}"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "update-ref",
            "-m",
            action,
            "refs/heads/main",
            first_new,
            first_old,
        ],
        check=True,
    )
    (repo / "second.txt").write_text("second\n")
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    second_new = git(repo, "commit-tree", tree, "-p", first_new, "-m", "second")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "update-ref",
            "-m",
            action,
            "refs/heads/main",
            second_new,
            first_new,
        ],
        check=True,
    )

    with pytest.raises(verify.IntegrationEvidenceError, match="ambiguous"):
        verify.integration_ref_update(repo, "refs/heads/main", operation)


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
    assert verify._merge_in_progress(repo) == (False, None)  # nothing to abort was ever started


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
    assert verify._merge_in_progress(repo) == (False, None)  # and nothing is mid-flight


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
    assert verify._merge_in_progress(repo) == (False, None)
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
    assert verify._merge_in_progress(repo) == (True, None)  # the tree really is unrestored


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
    """DATA-SAFETY PIN. The original #619 guard replaced a restore fired on an
    ABSOLUTE post-squash dirtiness reading, which read a checkout that was already
    dirty as "the squash acted" — so a merge git refused without touching a byte
    still triggered a repo-wide `reset --hard HEAD` and destroyed an unstaged edit
    to a file no branch involved ever mentions. The guard is per PATH now (a
    before/after delta intersected with the incoming set) and the restore is
    path-scoped, but what this row pins is unchanged: a refusal over a dirty tree
    writes nothing over the operator's edit.

    Both topologies are covered because the refusal renders differently when the
    merge would have been a fast-forward, and neither rendering may restore.

    Ablation (measured): drop the `- pre_untracked` subtraction and both rows
    fail on the raised class — the stray `leak.cs`, sitting on an incoming path,
    is read as materialized residue, which is the one shape the intersection
    cannot shield. The operator's `src.txt` edit itself now survives even that
    ablation (outside the incoming set), so the destruction pin has become a
    class pin — the destruction axes have their own rows among the concurrent
    tests below."""
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
    discriminator and exactly six rows fail — these four, the `ff` sibling below,
    and the names-only row, whose expected class collapses with the arm — every
    other verify-layer row staying green (measured). Dropping the DELTA instead
    is a different ablation with a different witness set — see the row below."""
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
    assert verify._merge_in_progress(repo) == (False, None)


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

    Unlike the untracked axis, this one IS restorable, and a path-scoped
    `git checkout HEAD --` over exactly the attributed paths is what restores it
    — never a repo-wide reset, whose blast radius is the seventh shape's rows
    below. So the row asserts both halves: the classification is
    `MergeHalfAppliedError` (the CAUSE is a stopped checkout, not a target-state
    clash, and the remedies differ), and the tree is genuinely put back.

    All three strategies, because all three check out. `ff` is the row that
    matters most: its leg carried an explicit "--ff-only never starts a merge"
    premise and did no residue detection at all, so a fast-forward killed
    mid-checkout left the target rewritten with nothing to restore it.

    Ablation (repair): sever the `_restore_rewritten_paths` call from all three
    legs and exactly nine rows fail (measured) — these three on the file
    contents (the half a classification-only fix would have missed), the three
    concurrent-edit compound rows below, the restore-failure row, AND two rows
    that predate the axis: `test_merge_squash_conflict_restores` and the dead-
    index-probe squash row. That last pair is worth keeping in the record:
    `rewritten` is the same attributed value that gates #619's pre-existing
    squash-conflict restore, so the two behaviours share one predicate and a
    change to it moves both. The untracked rows above stay green, having no
    tracked residue to see."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path, tracked=True)
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy, message="m")

    assert not isinstance(ei.value, verify.MergePreflightError)
    assert "refused before starting" not in str(ei.value)
    assert ei.value.paths == ()  # nothing untracked was left, so nothing to hand over
    assert ei.value.rewritten == ("aaa.txt",)  # the tracked rewrite, named
    assert ei.value.restored  # ...and rolled back
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
    """The residue is reported as a before/after DELTA intersected with the
    incoming set, so the operator's own pre-existing strays are never handed to
    them as git's doing.

    An absolute, unintersected reading of `ls-files --others` would name every
    untracked file in the checkout — and the message tells the operator to clear
    what it names, over a checkout the guard deliberately tolerates strays in
    (#460). Naming one is how a correct fix to the classification would have
    become a worse bug than the one it replaced.

    For a stray OUTSIDE the incoming set — this row's `operator-notes.txt` — the
    two proofs deliberately OVERLAP: measured, dropping the `- pre_untracked`
    subtraction alone leaves this row green (the intersection shields it) and so
    does dropping the intersection alone (the delta shields it). Neither ablation
    is inert, their witnesses are just DISJOINT: the subtraction alone holds the
    strays the intersection cannot shield (a stray already sitting on an incoming
    path — the untracked-overwrite pre-flight rows and both topologies of the
    dirty-tree data-safety pin redden, measured), and the intersection alone
    holds the writes the delta cannot (everything landing mid-window — the
    concurrent rows below). What reddens THIS row alone is the predicate: drop
    the `materialized` arm from the discriminator and the class this equality
    sits behind collapses."""
    repo = project.project
    (repo / "operator-notes.txt").write_text("mine\n")  # untracked, predates the merge
    _branch_whose_checkout_dies_partway(repo, tmp_path)

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    assert ei.value.paths == ("aaa.txt",)
    assert "operator-notes.txt" not in str(ei.value)
    assert (repo / "operator-notes.txt").read_text() == "mine\n"  # and left alone


def _operator_who_writes_mid_merge(monkeypatch, repo, rel="src.txt", content=None):
    """Land an operator's write inside the MERGE WINDOW: after `_residue_snapshot`'s
    pre-merge reading, before the failure is classified. Staged by delegating
    through the `_git` seam and writing just as the merge argv itself reaches git —
    the one moment both samples of the before/after pair have to disagree about.
    Argv-matched to the three merge invocations so the `merge` leg's own
    `merge --abort` never re-fires it."""
    real = verify._git

    def racing(r, *args):
        if args and args[0] == "merge" and args[1] in ("--ff-only", "--no-ff", "--squash"):
            (repo / rel).write_text(content if content is not None else "operator mid-merge\n")
        return real(r, *args)

    monkeypatch.setattr(verify, "_git", racing)


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_concurrent_edit_during_a_refused_merge_is_neither_attributed_nor_destroyed(
    project, tmp_path, strategy, monkeypatch
):
    """The SEVENTH mislabeled git state: a concurrent operator edit landing during
    the merge window, attributed to git by a repo-WIDE dirtiness reading.

    The tracked half of the residue answer used to be one boolean — "the tree was
    clean before and is dirty now" — so an edit to ANY tracked file between the
    two readings made it True. Here the base state is a genuine untracked-overwrite
    pre-flight refusal (git touched nothing), and the mid-window edit lands on
    `src.txt`, a file the incoming branch never mentions: the old classifier called
    that "failed part-way through checkout" (fiction) and its restore — a repo-wide
    `reset --hard HEAD` — DESTROYED the edit, on all three legs alike (measured).

    Attribution is now per PATH: the dirty-tracked set is sampled before and after
    and differenced, and only the part of that delta lying INSIDE the branch's
    incoming set — the only paths the merge can write — is git's. A bystander edit
    is outside it by construction, so the class stays pre-flight and no restore
    fires over the operator's bytes.

    Ablation (attribution axis): drop the `& incoming` intersection in
    `_merge_residue` and all three rows fail twice over — the class collapses to
    `MergeHalfAppliedError` and the edit is gone from disk. Ablation (delta axis):
    drop the `- pre_dirty_paths` subtraction instead and these rows stay green
    (the edit lands inside the window, so the delta never excluded it) — the
    staged-on-incoming pre-flight rows are what hold that axis (measured: exactly
    those two redden, a pre-existing staged edit on an incoming path being the
    one tracked dirt the intersection cannot shield)."""
    repo = project.project
    _preflight_untracked_overwrite(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    _operator_who_writes_mid_merge(monkeypatch, repo, "src.txt", "operator mid-merge\n")

    with pytest.raises(verify.MergePreflightError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy, message="m")

    msg = str(ei.value)
    assert "refused before starting" in msg
    assert "failed part-way" not in msg
    assert "src.txt" not in msg  # the bystander is not named as git's residue
    assert (repo / "src.txt").read_text() == "operator mid-merge\n"  # and survives
    assert git(repo, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_concurrent_edit_during_a_partway_checkout_is_parted_from_gits_residue(
    project, tmp_path, strategy, monkeypatch
):
    """The same race compounded with a GENUINE part-way checkout: git really did
    rewrite an incoming tracked path (`aaa.txt`) before dying, and the operator's
    bystander edit (`src.txt`) lands in the same window.

    Both halves of the claim are asserted per path: the class holds (this IS
    half-applied), git's own rewrite is restored — by `git checkout HEAD --` over
    exactly the attributed paths, never a repo-wide reset — and the operator's
    edit is neither restored away nor named in the message. The old repo-wide
    boolean could not say WHICH paths were git's, so its restore was all-or-nothing
    and this scene lost the edit.

    Ablation (restore-scope axis): put the repo-wide `reset --hard HEAD` back as
    the half-applied restore and these three rows fail on the operator's bytes —
    the class and `aaa.txt` both stay correct, which is why the scope needs its
    own rows. Ablation (attribution axis): dropping `& incoming` reddens these on
    the message naming `src.txt` and on its bytes."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path, tracked=True)
    head_before = git(repo, "rev-parse", "HEAD")
    _operator_who_writes_mid_merge(monkeypatch, repo, "src.txt", "operator mid-merge\n")

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy, message="m")

    assert ei.value.rewritten == ("aaa.txt",)  # git's rewrite, attributed by path
    assert ei.value.restored is True
    assert "src.txt" not in str(ei.value)
    assert (repo / "aaa.txt").read_text() == "original aaa\n"  # git's half: restored
    assert (repo / "src.txt").read_text() == "operator mid-merge\n"  # theirs: kept
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_concurrent_untracked_file_during_a_refused_merge_is_not_reported(
    project, tmp_path, monkeypatch
):
    """The untracked axis of the same window: an operator dropping a scratch file
    mid-merge used to flip a genuine pre-flight refusal into "failed part-way
    through checkout" and hand them their own file with an instruction to clear
    it — the delta proves the path is NEW, not that git wrote it. The incoming
    set is what proves that, so the materialized reading is intersected with it
    exactly as the tracked one is.

    Ablation: intersect only the tracked half and this row fails alone on the
    class and the named path, the tracked rows above staying green."""
    repo = project.project
    _preflight_untracked_overwrite(repo, tmp_path)
    _operator_who_writes_mid_merge(monkeypatch, repo, "scratch.txt", "operator notes\n")

    with pytest.raises(verify.MergePreflightError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    assert "scratch.txt" not in str(ei.value)
    assert (repo / "scratch.txt").read_text() == "operator notes\n"  # left alone


def test_partway_checkout_with_a_dead_incoming_probe_is_reported_unverified(
    project, tmp_path, monkeypatch
):
    """The incoming-set reading is a post-merge probe like its three siblings, so
    a failure there must degrade to the same unread marker: without the incoming
    set the delta cannot be attributed in either direction, and claiming
    pre-flight ("git touched nothing") or half-applied (with a restore riding on
    it) would both stand on a reading that died. It is also read LAZILY — only a
    non-empty delta needs attributing — so a refusal over an unresolvable ref
    still classifies as the pre-flight refusal it is instead of dying on a probe
    the clean scene never needed.

    Ablation (wrap axis): re-raise in `_merge_residue` and this fails on the
    raised type. Ablation (lazy axis): read the incoming set unconditionally and
    the row below fails instead — the clean-delta scene starts consulting a ref
    that cannot answer."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path, tracked=True)

    def dead_incoming(r, branch):
        raise verify.GitError(f"git diff --name-only HEAD {branch} failed in {r}: incoming boom")

    monkeypatch.setattr(verify, "_incoming_paths", dead_incoming)

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash")

    msg = str(ei.value)
    assert "checkout state unverified" in msg
    assert "AND the residue probe failed" in msg and "incoming boom" in msg
    assert "failed part-way" not in msg
    # nothing was restored on an unattributable delta: the residue survives, unread
    assert (repo / "aaa.txt").read_text() == "incoming aaa\n"


def test_refusal_with_a_clean_delta_never_reads_the_incoming_set(project, tmp_path, monkeypatch):
    """The lazy half of the incoming probe's contract, pinned from the scene that
    motivated it: a pre-flight refusal that left NO new dirt needs no attribution,
    so the incoming set is never read — and a monkeypatched probe that would die
    proves it was not consulted. This is what keeps refusals over an unresolvable
    ref (`branch_exists` raced away, unrelated histories) classifying as the
    pre-flight refusals they are rather than as unread."""
    repo = project.project
    _preflight_untracked_overwrite(repo, tmp_path)

    def dead_incoming(r, branch):
        raise verify.GitError("incoming probe consulted on a clean delta")

    monkeypatch.setattr(verify, "_incoming_paths", dead_incoming)

    with pytest.raises(verify.MergePreflightError):
        verify.merge_branch(repo, "feat", strategy="merge")


def test_half_applied_restore_failure_reports_the_rewritten_paths_unrestored(
    project, tmp_path, monkeypatch
):
    """The path-scoped restore is a repair write like the reset it replaced, so
    its failure must be carried, not implied away: `restored` flips False, the
    note names the failure, and `rewritten` still hands the caller the exact
    paths — which is what lets the escalation prescribe a path-scoped recovery
    instead of the repo-wide `reset --hard` whose blast radius this fix removed.

    Failed through the `_git` seam by argv, like the reset sibling above: there
    is no portable way to make a real `git checkout HEAD --` fail on demand.

    Ablation: hardcode `restored=True` past the failed restore and this row
    fails on the flag and the note while the restoring sibling stays green."""
    repo = project.project
    _branch_whose_checkout_dies_partway(repo, tmp_path, tracked=True)
    real_git = verify._git

    def failing_checkout(r, *args):
        if args[:2] == ("checkout", "HEAD"):
            return 1, "fatal: could not restore"
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", failing_checkout)

    with pytest.raises(verify.MergeHalfAppliedError) as ei:
        verify.merge_branch(repo, "feat", strategy="ff")

    assert ei.value.restored is False
    assert ei.value.rewritten == ("aaa.txt",)
    assert "tracked residue not restored" in str(ei.value)
    # the tree really is unrestored: the incoming rewrite is still in place
    assert (repo / "aaa.txt").read_text() == "incoming aaa\n"


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
    assert verify._merge_in_progress(repo) == (False, None)
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
    assert verify._merge_in_progress(repo) == (False, None)  # the abort still ran
    assert not (repo / "feature.txt").exists()
    assert git(repo, "rev-parse", "HEAD") == head_before


def _index_probe_that_dies(monkeypatch):
    """Kill `_index_unmerged`'s underlying read (`ls-files -u`) through the
    `_git_out` seam, argv-matched so every other caller keeps working — the
    raise exercises the catch INSIDE the probe, the same axis `_run_git`'s
    timeout/spawn/decode faults arrive on. No count is needed: unlike the
    residue pair this probe has no pre-merge reading to spare."""
    real = verify._git_out

    def dying(repo, *args, env=None):
        if args == ("ls-files", "-u"):
            raise verify.GitError(f"git ls-files timed out after 1s in {repo}: index probe boom")
        return real(repo, *args, env=env)

    monkeypatch.setattr(verify, "_git_out", dying)


def _merge_state_probe_that_dies(monkeypatch):
    """Kill `_merge_in_progress`'s underlying read (`rev-parse --verify
    MERGE_HEAD`) through the `_git` seam, argv-matched. A `GitSpawnError` on
    purpose: the catch must hold for the taxonomy's subclasses, not just the
    root."""
    real = verify._git

    def dying(repo, *args):
        if args == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            raise verify.GitSpawnError(f"git rev-parse failed to spawn in {repo}: state probe boom")
        return real(repo, *args)

    monkeypatch.setattr(verify, "_git", dying)


@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_conflict_with_a_dead_index_probe_is_reported_unverified(
    project, tmp_path, strategy, monkeypatch
):
    """The index reading picks the CLASS between conflict and every sibling, so
    with it dead no class may stand on "did not collide" — the honest answer is
    unverified, naming the reading that died. The scene is a genuine conflict,
    which is what makes the old silent False a mislabel and not a rounding: it
    dressed this exact state as commit-refused (`merge`: MERGE_HEAD is set) or
    half-applied (`squash`: the markers dirty a pre-clean tree).

    The cleanup is NOT skipped with the classification: the abort stays gated
    on the still-live merge-state reading and the squash restore on the proven
    per-path attribution, so both run here exactly as they would for the
    classified conflict.

    Ablation (wrap axis): re-raise instead of catch in `_index_unmerged` and
    both rows fail on the raised type — the probe's own `GitError` escapes,
    and on the `merge` row MERGE_HEAD survives the escape. Ablation (claim
    axis): drop `index_unread is None` from the `merge` leg's commit-refused
    claim and ITS row fails on `MergeCommitRefusedError`; drop it from the
    half-applied gates and the `squash` row fails on `MergeHalfAppliedError` —
    each mislabel lands in a different sibling, which is why one scene grades
    both legs."""
    repo = project.project
    _branch_with(repo, tmp_path, modifies={"src.txt": "branch\n"})
    commit(repo, "src.txt", "main change\n", "main edits src")
    head_before = git(repo, "rev-parse", "HEAD")
    _index_probe_that_dies(monkeypatch)

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy=strategy)

    msg = str(ei.value)
    assert "checkout state unverified" in msg
    assert "AND the index probe failed" in msg and "index probe boom" in msg
    assert "(conflict)" not in msg  # unread — the conflict is not claimed either
    assert "refused the commit" not in msg
    assert git(repo, "rev-parse", "HEAD") == head_before
    # the cleanup still ran: the merge leg aborted the started merge, the squash
    # leg restored the attributed dirt — read through the test's own git, since
    # the module's index probe is dead by construction here.
    assert verify._merge_in_progress(repo) == (False, None)
    assert git(repo, "ls-files", "-u") == ""
    assert (repo / "src.txt").read_text() == "main change\n"


def test_commit_refusal_with_a_dead_index_probe_still_aborts_the_merge(
    project, tmp_path, monkeypatch
):
    """The claim half of the same wrap, on the state the dead reading cannot
    part from a conflict: MERGE_HEAD alone says a merge started, not whether
    its content collided — a `--no-ff` conflict sits mid-merge too — so
    `started` may not claim commit-refused over a dead index reading, and the
    class degrades to unverified while the abort, gated on the still-live
    merge-state reading, runs anyway.

    Ablation (claim axis): drop `index_unread is None` from the commit-refused
    claim and this row fails on the raised type (`MergeCommitRefusedError`),
    while the dead-RESIDUE sibling above stays green — its index reading is
    live, which is what parts the two rows. Ablation (wrap axis): re-raise in
    `_index_unmerged` and it fails twice over — the type collapses to the
    probe's `GitError` and MERGE_HEAD survives the escape."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")
    _index_probe_that_dies(monkeypatch)

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    msg = str(ei.value)
    assert "checkout state unverified" in msg
    assert "AND the index probe failed" in msg and "index probe boom" in msg
    assert "refused the commit" not in msg
    assert verify._merge_in_progress(repo) == (False, None)  # the abort still ran
    assert not (repo / "feature.txt").exists()
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_commit_refusal_with_a_dead_merge_state_probe_skips_the_abort_and_says_so(
    project, tmp_path, monkeypatch
):
    """The one probe whose reading gates a REPAIR, so its unread half is the
    inverse of its neighbours': uncertainty must not authorize a repair write —
    the standing rule for `reset --hard`, applied to `merge --abort` — so the
    unread case attempts NO abort, raises unverified, and says both. The repo
    really is left mid-merge; the message hands the operator the reading the
    run could not take instead of a restore claim it cannot back.

    Degraded silently instead (False with no marker), this scene half-applies:
    "neither collided nor started" stands unmeasured, the class collapses to
    `MergeHalfAppliedError`, and the half-applied arm's restore fires over a
    mid-merge checkout it was never meant to touch.

    Ablation (wrap axis): re-raise in `_merge_in_progress` and this fails on
    the raised type — the probe's own `GitSpawnError` escapes. Ablation
    (silent-degrade axis): return `(False, None)` from its except arm and it
    fails on the type as above, plus the reset erases the merge state the
    no-abort assertion pins."""
    repo = project.project
    _cleanly_mergeable_branch(repo, tmp_path)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "bmad-loop-no-such-signer")
    _merge_state_probe_that_dies(monkeypatch)

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy="merge")

    msg = str(ei.value)
    assert "checkout state unverified" in msg
    assert "no `git merge --abort` was attempted" in msg and "state probe boom" in msg
    assert "refused the commit" not in msg
    # no abort was attempted on an unread gate: the merge state is still there
    # for the operator's own `git status` — read through the test's own git,
    # since the module's merge-state probe is dead by construction here.
    assert git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") != ""


def test_probe_helpers_read_an_environment_fault_as_unread(tmp_path):
    """The rc axis of the same honesty: each probe spends return codes outside
    its answer set on an unread marker — or, for the replay reading, a RAISE
    its one caller catches — never on a silent answer. `git -C` a non-repo is
    the portable rc-128 environment fault; `rev-parse -q --verify` keeps rc 1
    as the legitimate no (measured: 1 for a missing MERGE_HEAD, 128 for this),
    which the in-repo rows everywhere above pin as `(False, None)`, and
    `diff --cached --quiet` keeps rc 1 as "there are differences". The marker
    vs raise split is position, not importance: the first two are read between
    a failed merge and its cleanup, the replay reading after a succeeded one,
    where nothing below it needs to run.

    Ablation (rc axis): read only rc 0 vs everything-else in any helper and
    its stanza fails here — no other row exercises a probe whose git RAN and
    failed, the monkeypatched rows all arriving on the raise axis."""
    unmerged, unread = verify._index_unmerged(tmp_path)
    assert unmerged is False and unread is not None
    assert "git ls-files -u failed" in str(unread)

    started, unread = verify._merge_in_progress(tmp_path)
    assert started is False and unread is not None
    assert "git rev-parse --verify MERGE_HEAD failed" in str(unread)

    with pytest.raises(verify.GitError) as ei:
        verify._index_dirty_vs_head(tmp_path)
    assert "git diff --cached HEAD failed" in str(ei.value)


def test_squash_replay_ignores_preexisting_unstaged_dirt(project, tmp_path):
    """`allow_empty_squash` recognises a replay by "the squash staged nothing" — the
    target already carries the merged tree. Asking that of the WORKING TREE let a
    pre-existing unstaged edit answer for the squash: the clean early return was
    skipped, `git commit` found nothing staged, and a host-loss recovery was reported
    as a failed merge. The index is the honest question (#619).

    Ablation: gate the early return on a worktree-dirtiness reading
    (`git diff --quiet HEAD`) again and this fails with a GitError naming
    "no changes added to commit"."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"f.txt": "branch\n"})
    verify.merge_branch(repo, "feat", strategy="squash", message="squash feat")  # the lost commit
    (repo / "src.txt").write_text("operator edit\n")  # unstaged, tracked, outside `feat`
    head_before = git(repo, "rev-parse", "HEAD")

    verify.merge_branch(repo, "feat", strategy="squash", allow_empty_squash=True)  # must not raise

    assert git(repo, "rev-parse", "HEAD") == head_before  # no empty commit manufactured
    assert (repo / "src.txt").read_text() == "operator edit\n"


def test_squash_replay_with_a_dead_index_reading_is_unverified_not_commit_refused(
    project, tmp_path, monkeypatch
):
    """`--exit-code` spends rc 1 on exactly "there are differences", so any
    other nonzero from the replay's staged-result reading is a probe failure,
    not an answer. Read as "dirty" it skipped the no-op return, and the doomed
    `git commit` that followed dressed the failure as a hook/signing refusal —
    with `_reset_hard_head`'s rollback riding on the fiction over a tree the
    probe never measured. The honest class is unverified: nothing committed,
    nothing reset, the dead reading named.

    Ablation (rc axis): read `rc != 0` as dirty in `_index_dirty_vs_head`
    again and this fails on the raised type — `MergeCommitRefusedError`, the
    manufactured refusal — with the reset spy recording the rollback that rode
    on it. Ablation (wiring axis): change the call-site catch to `except ()`
    and it fails on the probe's bare `GitError` escaping unclassified."""
    repo = project.project
    _branch_with(repo, tmp_path, adds={"f.txt": "branch\n"})
    verify.merge_branch(repo, "feat", strategy="squash", message="squash feat")  # already landed
    head_before = git(repo, "rev-parse", "HEAD")
    real = verify._git

    def faulted(repo_arg, *args):
        if args == ("diff", "--cached", "--quiet", "HEAD"):
            return 128, "fatal: unable to read index: replay probe boom"
        return real(repo_arg, *args)

    monkeypatch.setattr(verify, "_git", faulted)
    resets = []
    real_reset = verify._reset_hard_head
    monkeypatch.setattr(verify, "_reset_hard_head", lambda r: (resets.append(r), real_reset(r))[1])

    with pytest.raises(verify.MergeResidueUnreadError) as ei:
        verify.merge_branch(repo, "feat", strategy="squash", allow_empty_squash=True)

    msg = str(ei.value)
    assert "index state unverified" in msg
    assert "replay probe boom" in msg
    assert "refused the commit" not in msg
    assert git(repo, "rev-parse", "HEAD") == head_before  # no empty commit manufactured
    assert resets == []  # uncertainty authorized no rollback


def test_no_ff_conflict_with_preexisting_dirt_aborts_and_keeps_it(project, tmp_path):
    """The `merge` leg's restore is `git merge --abort`, which — like the
    path-scoped restore the squash leg uses now, and unlike the repo-wide
    `reset --hard` it used to — leaves an unstaged edit to an untouched tracked
    file alone. So a genuine conflict still aborts even with the checkout dirty,
    and the operator keeps both their edit and the conflict to resolve (#619).

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
    assert verify._merge_in_progress(repo) == (False, None)  # the abort ran
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


def test_branch_incoming_paths_names_both_sides_of_a_rename(project, tmp_path):
    """`git diff --name-only` runs rename detection by default and reports a
    rename as its destination alone, so a bundle moving `old` to `new` had
    `old` outside the incoming set: never snapshotted, never cleaned or
    tolerated by the guard, and — once the receipt digested the index outside
    that set — deleted by the merge into a digest mismatch that named no
    entry and refused every renaming bundle (Codex, #796 review). Both sides
    are incoming, as the restore's own inventory already reads them.

    Ablation: drop `--no-renames` and this reds on `old` missing."""
    repo = project.project
    (repo / "old.txt").write_text("content worth renaming\n" * 20)
    git(repo, "add", "--", "old.txt")
    git(repo, "commit", "-q", "-m", "old.txt")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    git(wt, "mv", "--", "old.txt", "new.txt")
    git(wt, "commit", "-q", "-m", "rename")
    verify.worktree_remove(repo, wt, force=True)
    assert git(repo, "diff", "--name-only", "main", "feat") == "new.txt"

    assert verify.branch_incoming_paths(repo, "main", "feat") == {"old.txt", "new.txt"}


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


@pytest.mark.parametrize("recreated_as", ["file", "symlink", "ignored"])
def test_integrated_paths_drift_sees_a_deleted_incoming_path_recreated_untracked(
    project, recreated_as
):
    """`integrated_paths_drift` reads two whole-tree `git diff` listings against
    the integrated commit, and `git diff` reports only index-tracked paths — so
    an incoming path the integrated commit DELETES, which a TARGET hook then
    recreates without staging, was invisible to both readings: `status` shows
    `?? path`, both diffs are empty, the run recorded `unit-merged` and retired
    the rollback receipt over hook-made content the commit does not hold
    (Codex, #796 review). For a deleted incoming path the integrated commit's
    authority is "absent from the checkout", so that leg is a filesystem probe
    — any entry at the path, plain, symlink, or gitignored, is drift.

    Ablation: drop the absent-path probe and every row reds on the empty
    drift tuple."""
    repo = project.project
    if recreated_as == "ignored":
        (repo / ".gitignore").write_text("gone.bin\n")
        git(repo, "add", "--", ".gitignore")
    (repo / "gone.bin").write_text("incoming deletes me\n")
    git(repo, "add", "-f", "--", "gone.bin")
    git(repo, "commit", "-q", "-m", "gone.bin present")
    git(repo, "rm", "-q", "--", "gone.bin")
    git(repo, "commit", "-q", "-m", "integrated: delete gone.bin")
    integrated = verify.rev_parse_head(repo)
    assert verify.integrated_paths_drift(repo, integrated, ("gone.bin",)) == ()

    if recreated_as == "symlink":
        os.symlink("src.txt", repo / "gone.bin")
    else:
        (repo / "gone.bin").write_text("hook recreated me\n")

    assert git(repo, "diff", "--name-only", integrated) == ""
    assert git(repo, "diff", "--cached", "--name-only", integrated) == ""
    assert verify.integrated_paths_drift(repo, integrated, ("gone.bin",)) == ("gone.bin",)


def test_integrated_paths_drift_accepts_an_absent_deleted_incoming_path(project):
    """The absent-path probe is one-directional: a deleted incoming path that
    IS absent from the checkout is the integrated commit's own state, not
    drift — and a path the commit still holds keeps the diff readings as its
    authority, so an unchanged one is not reported either."""
    repo = project.project
    (repo / "gone.bin").write_text("incoming deletes me\n")
    git(repo, "add", "--", "gone.bin")
    git(repo, "commit", "-q", "-m", "gone.bin present")
    git(repo, "rm", "-q", "--", "gone.bin")
    git(repo, "commit", "-q", "-m", "integrated: delete gone.bin")
    integrated = verify.rev_parse_head(repo)

    assert verify.integrated_paths_drift(repo, integrated, ("gone.bin", "src.txt")) == ()


def test_integrated_paths_drift_reads_a_deleted_leaf_beneath_the_commit_s_symlink_as_absent(
    project,
):
    """The reverse of the symlink-to-directory transition: the commit deletes
    `a/b` and holds `a` as a symlink to a directory that has a `b` of its own
    (`a -> dir`, `dir/b` tracked). The absent-path probe `exists()`ed `a/b`
    through the new link, read `dir/b` as a recreated `a/b`, and refused a
    merge git applied cleanly (Codex, #796 review). Git tracks no path
    through a symlink, so the leaf beneath one the commit holds is absent by
    topology; the link itself is an incoming path the diff readings hold to
    the commit. A link the commit does NOT hold there is a hook's, and the
    leaf beneath it stays drift.

    Ablation: probe `exists()` again without the topology reading and the
    clean row reds on `("a/b",)`; accept any link and the hook row reds on
    the empty tuple."""
    repo = project.project
    (repo / "dir").mkdir()
    (repo / "dir" / "b").write_text("dir's own b\n")
    (repo / "a").mkdir()
    (repo / "a" / "b").write_text("incoming deletes me\n")
    git(repo, "add", "--", "dir/b", "a/b")
    git(repo, "commit", "-q", "-m", "a is a directory")
    git(repo, "rm", "-q", "--", "a/b")
    os.symlink("dir", repo / "a")
    git(repo, "add", "--", "a")
    git(repo, "commit", "-q", "-m", "integrated: a becomes a symlink")
    integrated = verify.rev_parse_head(repo)
    assert (repo / "a" / "b").exists()

    assert verify.integrated_paths_drift(repo, integrated, ("a", "a/b")) == ()

    # the same leaf beneath a link the commit does not hold: a hook's
    git(repo, "rm", "-q", "--", "a")
    git(repo, "commit", "-q", "-m", "integrated: a is gone")
    integrated = verify.rev_parse_head(repo)
    assert verify.integrated_paths_drift(repo, integrated, ("a", "a/b")) == ()
    os.symlink("dir", repo / "a")

    assert verify.integrated_paths_drift(repo, integrated, ("a", "a/b")) == ("a", "a/b")


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("path", ["src.txt", "newdir/tracked"], ids=["modified", "added"])
def test_integrated_index_flags_drift_reports_a_hook_s_flag_on_an_incoming_path(
    project, tmp_path, flag, path
):
    """A target hook running `update-index --assume-unchanged` (or
    `--skip-worktree`) on an incoming path changes no blob, so both diff
    readings stay empty while `ls-files --debug` reports `flags: 8000` (or
    `40004000`) — an index that hides later edits from git, retired with
    `unit-merged` over it (Codex, #796 review). The post-hook index flag
    word of every incoming path is read against what a fresh entry may
    carry — none; skip-worktree only on a sparse target, where git itself
    strips it from an in-pattern path — or the word the receipt captured
    for the path.

    Ablation: return `()` and every row reds."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("src.txt", "newdir/tracked")
    )
    (repo / "newdir").mkdir()
    (repo / "newdir" / "tracked").write_text("incoming\n")
    (repo / "src.txt").write_text("incoming\n")
    git(repo, "add", "--", "newdir/tracked", "src.txt")
    git(repo, "commit", "-q", "-m", "integrated")
    integrated = verify.rev_parse_head(repo)
    git(repo, "update-index", f"--{flag}", "--", path)
    assert verify.integrated_paths_drift(repo, integrated, ("src.txt", "newdir/tracked")) == ()
    assert (
        verify.integrated_stray_paths(repo, tolerated=(), incoming=("src.txt", "newdir/tracked"))
        == ()
    )

    assert verify.integrated_index_flags_drift(
        repo, run_dir, snapshots, ("src.txt", "newdir/tracked"), operation_identity="e" * 32
    ) == (path,)


@pytest.mark.parametrize(
    "flip", ["set-assume-unchanged", "set-skip-worktree", "clear-assume-unchanged"]
)
def test_integrated_index_flags_outside_drift_names_a_hook_s_flip(project, tmp_path, flip):
    """A target hook running `update-index --assume-unchanged` on a clean
    tracked file OUTSIDE the incoming set changes no blob and leaves
    `status --porcelain` empty while `ls-files --debug` reports `8000`; the
    incoming-path reading is scoped to its paths and the stray reading takes
    status, so the run recorded `unit-merged` over the mutation (Codex, #796
    review). The receipt now carries `index_flags`: a digest over the flag
    word of every stage-0 file entry outside the snapshot set, and the map
    of those carrying a word no fresh entry may — enough to prove the rest
    of the index unchanged after the hooks and to name a flipped path
    without persisting the whole index. Set or cleared, the flip is named.

    Ablation: return `()` from the reading and every row reds."""
    repo = project.project
    (repo / "notes.txt").write_text("clean and outside the incoming set\n")
    git(repo, "add", "--", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes.txt")
    if flip == "clear-assume-unchanged":
        git(repo, "update-index", "--assume-unchanged", "--", "notes.txt")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, _submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ("src.txt",))
    evidence = verify.capture_index_flags(repo, exclude=[entry["path"] for entry in snapshots])
    assert evidence["marked"] == ({"notes.txt": "8000"} if flip == "clear-assume-unchanged" else {})
    (repo / "src.txt").write_text("incoming\n")
    git(repo, "add", "--", "src.txt")
    git(repo, "commit", "-q", "-m", "integrated")
    exclude = [entry["path"] for entry in snapshots]
    assert verify.integrated_index_flags_outside_drift(repo, evidence, exclude=exclude) == ()
    if flip == "set-assume-unchanged":
        git(repo, "update-index", "--assume-unchanged", "--", "notes.txt")
    elif flip == "set-skip-worktree":
        git(repo, "update-index", "--skip-worktree", "--", "notes.txt")
    else:
        git(repo, "update-index", "--no-assume-unchanged", "--", "notes.txt")
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("src.txt",)) == ()

    assert verify.integrated_index_flags_outside_drift(repo, evidence, exclude=exclude) == (
        "notes.txt",
    )


def test_integrated_index_flags_outside_drift_accepts_a_sparse_target(project, tmp_path):
    """On a sparse target every out-of-cone entry carries skip-worktree, git's
    own word: the digest proves them unchanged, the map holds none of them,
    and the evidence validates as a receipt field."""
    repo = project.project
    (repo / "keep").mkdir()
    (repo / "keep" / "k.txt").write_text("in the cone\n")
    (repo / "other").mkdir()
    (repo / "other" / "o.txt").write_text("out of the cone\n")
    git(repo, "add", "--", "keep/k.txt", "other/o.txt")
    git(repo, "commit", "-q", "-m", "two dirs")
    git(repo, "sparse-checkout", "set", "--cone", "keep")
    assert git(repo, "ls-files", "-t", "--", "other/o.txt") == "S other/o.txt"
    evidence = verify.capture_index_flags(repo, exclude=["src.txt"])
    assert evidence["marked"] == {}
    assert verify.validate_index_flags_evidence(evidence) == evidence
    (repo / "src.txt").write_text("incoming\n")
    git(repo, "add", "--", "src.txt")
    git(repo, "commit", "-q", "-m", "integrated")

    assert verify.integrated_index_flags_outside_drift(repo, evidence, exclude=["src.txt"]) == ()
    with pytest.raises(verify.IntegrationEvidenceError, match="malformed"):
        verify.validate_index_flags_evidence({**evidence, "marked": {"x": "zz"}})
    git(repo, "sparse-checkout", "disable")


@pytest.mark.parametrize("shape", ["captured-word-rewritten", "sparse-out-of-cone"])
def test_integrated_index_flags_drift_accepts_git_s_own_words(project, tmp_path, shape):
    """The integration writes every incoming entry anew — a file's
    assume-unchanged bit does not survive even a fast-forward (git 2.55) —
    so an incoming path the receipt captured with `8000` reads clean at a
    fresh `0`; and on a sparse target it may carry skip-worktree outside
    the cone, which git sets on every such entry it writes."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if shape == "captured-word-rewritten":
        git(repo, "update-index", "--assume-unchanged", "--", "src.txt")
        snapshots, _submodules = verify.capture_integration_state(
            repo, run_dir, "e" * 32, ("src.txt",)
        )
        [entry] = snapshots
        assert entry["index"]["entries"][0]["flags"] == "8000"
        git(repo, "update-index", "--no-assume-unchanged", "--", "src.txt")
        (repo / "src.txt").write_text("integrated\n")
        git(repo, "add", "--", "src.txt")
        git(repo, "commit", "-q", "-m", "integrated")
        assert git(repo, "ls-files", "-v", "--", "src.txt") == "H src.txt"
        incoming = ("src.txt",)
    else:
        (repo / "keep").mkdir()
        (repo / "keep" / "k.txt").write_text("in the cone\n")
        git(repo, "add", "--", "keep/k.txt")
        git(repo, "commit", "-q", "-m", "keep")
        _branch_with(repo, tmp_path, adds={"other/o.txt": "out of the cone\n"})
        git(repo, "sparse-checkout", "set", "--cone", "keep")
        snapshots, _submodules = verify.capture_integration_state(
            repo, run_dir, "e" * 32, ("other/o.txt",)
        )
        git(repo, "merge", "-q", "--ff-only", "feat")
        assert git(repo, "ls-files", "-t", "--", "other/o.txt") == "S other/o.txt"
        assert not (repo / "other").exists()
        incoming = ("other/o.txt",)

    assert (
        verify.integrated_index_flags_drift(
            repo, run_dir, snapshots, incoming, operation_identity="e" * 32
        )
        == ()
    )
    if shape == "sparse-out-of-cone":
        git(repo, "sparse-checkout", "disable")


@pytest.mark.parametrize("shape", ["file-to-directory", "directory-to-file"])
def test_integrated_paths_drift_accepts_an_incoming_entry_type_change(project, shape):
    """The absent-path probe reads the integrated commit's inventory as
    `ls-tree -r`, which lists blobs and gitlinks, never the trees above them —
    so an incoming path the commit turned INTO a directory (`a` deleted,
    `a/b` added) was "absent" from that inventory while a directory
    legitimately stood there, and the probe refused the integration as hook
    drift (found off the Codex #796 review of the probe, 10be932b). A path
    that is a tree prefix of something the commit holds IS held; the reverse
    change (`d/x` deleted, `d` now a file) needs no such reading, a file at
    `d` making `d/x` unreachable on disk.

    Ablation: drop the prefix expansion and the `file-to-directory` row reds
    with `('a',)` reported as drift."""
    repo = project.project
    if shape == "file-to-directory":
        (repo / "a").write_text("a file\n")
        git(repo, "add", "--", "a")
        git(repo, "commit", "-q", "-m", "a is a file")
        git(repo, "rm", "-q", "--", "a")
        (repo / "a").mkdir()
        (repo / "a" / "b").write_text("a directory\n")
        git(repo, "add", "--", "a/b")
        git(repo, "commit", "-q", "-m", "integrated: a becomes a directory")
        incoming = ("a", "a/b")
    else:
        (repo / "d").mkdir()
        (repo / "d" / "x").write_text("a directory\n")
        git(repo, "add", "--", "d/x")
        git(repo, "commit", "-q", "-m", "d is a directory")
        git(repo, "rm", "-q", "--", "d/x")
        (repo / "d").write_text("a file\n")
        git(repo, "add", "--", "d")
        git(repo, "commit", "-q", "-m", "integrated: d becomes a file")
        incoming = ("d/x", "d")
    integrated = verify.rev_parse_head(repo)
    assert git(repo, "status", "--porcelain") == ""

    assert verify.integrated_paths_drift(repo, integrated, incoming) == ()


def test_ignored_entries_receipt_names_an_entry_added_after_the_hooks(project, tmp_path):
    """A target hook's gitignored write into a directory the target already
    held populated (`dir/keep` tracked, `dir/added` incoming, the hook writes
    `dir/cache.tmp`) is listed by no reading: the diff readings cover tracked
    paths, the stray reading takes `status` without `--ignored`, and the
    introduced-directory walk has no root at a populated parent (Codex, #796
    review). The receipt now seals the whole tree's ignored entries into a
    sidecar when it is armed, and after the hooks every ignored entry not on
    that listing is named — beside the incoming path, inside a directory
    that was already wholly ignored, anywhere — and, each entry sealed with
    its `lstat` identity, one it did record that a hook then overwrote,
    truncated or touched in place, which leaves the path set unchanged and
    `status` and `diff` silent (Codex, #796 review). What it does not name:
    an ignored entry that left the listing (the receipt never held its
    bytes), an ignored path the commit now tracks, a tolerated stray an
    incoming `.gitignore` change turned ignored, and the run's own records
    under the automator directory, this receipt's sidecars among them.

    Ablation: return `()` from `integrated_ignored_additions` and the
    additions assertion reds; compare paths alone and the rewritten,
    truncated and touched entries go unnamed; drop the tolerated exclusion
    and the turned-ignored stray is named; drop the record exclusion and the
    receipt's own sidecar is named."""
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (repo / ".gitignore").write_text("*.tmp\nbuild/\n.bmad-loop/runs/\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "keep").write_text("already here\n")
    (repo / "dir" / "stale.tmp").write_text("ignored before\n")
    (repo / "dir" / "becomes-tracked.tmp").write_text("ignored, then committed\n")
    (repo / "dir" / "rewritten.tmp").write_text("ignored before\n")
    (repo / "dir" / "truncated.tmp").write_text("ignored before\n")
    (repo / "dir" / "touched.tmp").write_text("ignored before\n")
    (repo / "build").mkdir()
    (repo / "build" / "old.o").write_text("ignored before\n")
    git(repo, "add", "--", ".gitignore", "dir/keep")
    git(repo, "commit", "-q", "-m", "populated dir; ignored entries")
    (repo / "stray.log").write_text("untracked, tolerated by the guard\n")
    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("dir/added", "dir/becomes-tracked.tmp")
    )
    assert {entry["path"] for entry in snapshots} == {"dir/added", "dir/becomes-tracked.tmp"}

    evidence = verify.capture_ignored_entries(repo, run_dir, "d" * 32)

    assert set(evidence) == {"sidecar", "size", "sha256"}
    sidecar = run_dir / str(evidence["sidecar"])
    assert sidecar.parent == run_dir / "integration-snapshots" / ("d" * 32)
    recorded = sidecar.read_bytes().split(b"\0")
    assert recorded[::2] == [
        b"build/old.o",
        b"dir/becomes-tracked.tmp",
        b"dir/rewritten.tmp",
        b"dir/stale.tmp",
        b"dir/touched.tmp",
        b"dir/truncated.tmp",
    ]
    stat = (repo / "dir" / "stale.tmp").lstat()
    assert (
        recorded[7]
        == ":".join(
            str(value)
            for value in (
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_ino,
                stat.st_dev,
                stat.st_mode,
            )
        ).encode()
    )
    assert verify.integrated_ignored_additions(repo, run_dir, evidence) == ()

    # the integrated shape: the commit tracks one ignored path and adds
    # beside the kept file; its `.gitignore` turns the tolerated stray ignored
    (repo / "dir" / "added").write_text("incoming\n")
    (repo / ".gitignore").write_text("*.tmp\nbuild/\n.bmad-loop/runs/\n*.log\n")
    git(repo, "add", "-f", "--", "dir/added", "dir/becomes-tracked.tmp", ".gitignore")
    git(repo, "commit", "-q", "-m", "integrated")
    (repo / "dir" / "stale.tmp").unlink()  # a removal is not read
    assert (
        verify.integrated_ignored_additions(repo, run_dir, evidence, tolerated=("stray.log",)) == ()
    )
    assert verify.integrated_ignored_additions(repo, run_dir, evidence) == ("stray.log",)

    (repo / "dir" / "cache.tmp").write_text("target hook output\n")
    (repo / "build" / "new.o").write_text("target hook output\n")
    (repo / "elsewhere.tmp").write_text("target hook output\n")
    (repo / "dir" / "rewritten.tmp").write_text("target hook output\n")
    (repo / "dir" / "truncated.tmp").write_bytes(b"")
    os.utime(repo / "dir" / "touched.tmp")
    assert git(repo, "status", "--porcelain", "-uall") == ""

    assert verify.integrated_ignored_additions(
        repo, run_dir, evidence, tolerated=("stray.log",)
    ) == (
        "build/new.o",
        "dir/cache.tmp",
        "dir/rewritten.tmp",
        "dir/touched.tmp",
        "dir/truncated.tmp",
        "elsewhere.tmp",
    )

    # the sealed listing is read back against its digest, and its shape
    sidecar.write_bytes(b"build/old.o")
    with pytest.raises(verify.IntegrationEvidenceError, match="changed"):
        verify.integrated_ignored_additions(repo, run_dir, evidence)
    odd = {
        "sidecar": evidence["sidecar"],
        "size": 11,
        "sha256": hashlib.sha256(b"build/old.o").hexdigest(),
    }
    with pytest.raises(verify.IntegrationEvidenceError, match="malformed"):
        verify.integrated_ignored_additions(repo, run_dir, odd)
    with pytest.raises(verify.IntegrationEvidenceError, match="malformed"):
        verify.integrated_ignored_additions(repo, run_dir, {"sidecar": "x", "size": 1})


def _integrate_new_directory(repo, run_dir):
    """Arm a receipt over `newdir/tracked` while `newdir` is absent, then commit
    the integrated shape: the directory created by the commit, holding exactly
    the file it tracks. Returns the receipt's snapshots and the integrated
    revision."""
    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("newdir/tracked", "src.txt")
    )
    [entry] = [entry for entry in snapshots if entry["path"] == "newdir/tracked"]
    assert entry["state"] == "absent" and entry["absent_parents"] == ["newdir"]
    (repo / "newdir").mkdir()
    (repo / "newdir" / "tracked").write_text("incoming\n")
    git(repo, "add", "--", "newdir/tracked")
    git(repo, "commit", "-q", "-m", "integrated: newdir/tracked")
    return snapshots, verify.rev_parse_head(repo)


@pytest.mark.parametrize("residue", ["ignored-file", "ignored-directory", "nested-repo"])
def test_integrated_introduced_directory_refuses_an_entry_status_never_lists(
    project, tmp_path, residue
):
    """A directory the incoming commit creates stands where the receipt proved
    nothing was (`absent_parents`), so everything in it is attempt-era — yet
    the readings after the hooks see only what git lists: the diff readings
    cover tracked paths, and the whole-tree stray reading takes `status`
    without `--ignored`, which also never names a `.git`. A target hook
    writing a gitignored file (or directory) into the new directory, or
    initialising a repository inside it, left the run recording
    `unit-merged` and retiring the receipt over it (Codex, #796 review). The
    new directory is now walked on disk against the integrated commit's
    inventory: every file must be a path the commit holds, every directory a
    prefix it holds (or a gitlink, whose checkout is the submodule
    reading's), and anything else is drift by path.

    Ablation: return `()` and every row reds."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / ".gitignore").write_text("*.tmp\ncache/\n")
    git(repo, "add", "--", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore hook output")
    snapshots, integrated = _integrate_new_directory(repo, run_dir)
    if residue == "ignored-file":
        (repo / "newdir" / "cache.tmp").write_text("target hook output\n")
        expected = ("newdir/cache.tmp",)
    elif residue == "ignored-directory":
        (repo / "newdir" / "cache").mkdir()
        (repo / "newdir" / "cache" / "x").write_text("target hook output\n")
        expected = ("newdir/cache",)
    else:
        git(repo / "newdir", "init", "-q")
        expected = ("newdir/.git",)
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integrated_paths_drift(repo, integrated, ("newdir/tracked", "src.txt")) == ()
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("newdir/tracked",)) == ()

    assert (
        verify.integrated_introduced_directories_drift(
            repo, integrated, run_dir, snapshots, operation_identity="e" * 32
        )
        == expected
    )


@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "checked-path"])
@pytest.mark.parametrize("residue", ["ignored-file", "nested-repo"])
@pytest.mark.parametrize("shape", ["file", "symlink", "file-deep"])
def test_integrated_directory_replacing_a_tracked_entry_is_walked(
    project, tmp_path, monkeypatch, shape, residue, anchored
):
    """A directory the incoming commit puts where a tracked file (or symlink)
    stood is one it created just as much as one where nothing stood — but
    `absent_parents` stops at the first existing ancestor, and the file
    exists, so the receipt named no proved-absent directory and the walk had
    no root: a target hook's gitignored write or nested `.git` under the new
    directory went unseen and the run recorded `unit-merged` over it (Codex,
    #796 review). The receipt already proves what stood there — the entry
    is captured under its own path as `regular` or `symlink` — so a path the
    receipt proved a non-directory and the commit now holds only as a prefix
    is a root of the walk. A leaf deeper down (`a/b/c`) records `a/b`
    absent; the walk starts at `a` all the same. On that refusal the restore
    puts the entry back through its own path, git taking the directory —
    the hook's residue with it, the receipt having proved the path a file —
    and the restoration reads complete, on the descriptor-anchored restore
    and the checked-path one alike.

    Ablation: derive roots from `absent_parents` alone and every row reds on
    the drift reading."""
    repo = project.project
    if not anchored:
        monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / ".gitignore").write_text("*.tmp\n")
    git(repo, "add", "--", ".gitignore")
    if shape == "symlink":
        os.symlink("src.txt", repo / "a")
    else:
        (repo / "a").write_text("a file\n")
    git(repo, "add", "--", "a")
    git(repo, "commit", "-q", "-m", "a is an entry")
    old = verify.rev_parse_head(repo)
    leaf = "a/b/c" if shape == "file-deep" else "a/b"
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ("a", leaf))
    by_path = {entry["path"]: entry for entry in snapshots}
    assert by_path["a"]["state"] == ("symlink" if shape == "symlink" else "regular")
    assert by_path[leaf]["absent_parents"] == (["a/b"] if shape == "file-deep" else [])
    git(repo, "rm", "-q", "--", "a")
    (repo / leaf).parent.mkdir(parents=True)
    (repo / leaf).write_text("a directory\n")
    git(repo, "add", "--", leaf)
    git(repo, "commit", "-q", "-m", "integrated: a becomes a directory")
    integrated = verify.rev_parse_head(repo)
    if residue == "ignored-file":
        (repo / "a" / "cache.tmp").write_text("target hook output\n")
        expected = ("a/cache.tmp",)
    else:
        git(repo / "a", "init", "-q")
        expected = ("a/.git",)
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integrated_paths_drift(repo, integrated, ("a", leaf)) == ()
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("a", leaf)) == ()

    assert (
        verify.integrated_introduced_directories_drift(
            repo, integrated, run_dir, snapshots, operation_identity="e" * 32
        )
        == expected
    )

    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )

    assert verify.rev_parse_head(repo) == old
    if shape == "symlink":
        assert os.readlink(repo / "a") == "src.txt"
    else:
        assert (repo / "a").read_text() == "a file\n"
    assert git(repo, "status", "--porcelain", "-uall", "--ignored") == ""
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )


def test_integrated_directory_replacing_a_tracked_entry_accepts_its_own_contents(project, tmp_path):
    """The walk over a directory that replaced a tracked file accepts exactly
    what the commit holds under it; and a receipt-captured file the commit
    still holds as a file — or deletes outright — is no root at all."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "a").write_text("a file\n")
    (repo / "kept").write_text("kept\n")
    (repo / "gone").write_text("gone\n")
    git(repo, "add", "--", "a", "kept", "gone")
    git(repo, "commit", "-q", "-m", "three files")
    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("a", "a/b", "a/deep/leaf", "kept", "gone")
    )
    git(repo, "rm", "-q", "--", "a", "gone")
    (repo / "a" / "deep").mkdir(parents=True)
    (repo / "a" / "b").write_text("a directory\n")
    (repo / "a" / "deep" / "leaf").write_text("a directory\n")
    (repo / "kept").write_text("kept, edited\n")
    git(repo, "add", "--", "a", "kept")
    git(repo, "commit", "-q", "-m", "integrated: a becomes a directory")
    integrated = verify.rev_parse_head(repo)

    assert (
        verify.integrated_introduced_directories_drift(
            repo, integrated, run_dir, snapshots, operation_identity="e" * 32
        )
        == ()
    )


@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "checked-path"])
@pytest.mark.parametrize("residue", ["none", "ignored-file", "nested-repo"])
@pytest.mark.parametrize("depth", ["direct", "deep"])
def test_integrated_directory_that_was_empty_at_capture_is_walked(
    project, tmp_path, monkeypatch, depth, residue, anchored
):
    """Git never tracks an empty directory, so an untracked empty `newdir/`
    already standing where the commit adds `newdir/tracked` (an IDE's, an
    earlier hook's) is invisible to every git reading — and `absent_parents`
    stopped at it, the parent existing, so the walk had no root and a
    hook's gitignored write or nested `.git` under it retired the receipt
    unverified (Codex, #796 review). The receipt now proves the first
    existing ancestor empty (`empty_parents`), which makes everything under
    it after the hooks attempt-era, exactly as under a proved-absent
    directory: walked against the commit, and on a refusal the restore
    leaves the directory absent or empty again — residue is the
    proved-absent doctrine's: nothing unattributable is removed, and the
    restore's own reading refuses before the ref moves, the run pausing as
    not safely restorable with the residue in place.

    Ablation: record no `empty_parents` and the residue rows red on the
    drift reading; drop them from the completeness reading and they red on
    the restore going through."""
    repo = project.project
    if not anchored:
        monkeypatch.setattr(verify, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / ".gitignore").write_text("*.tmp\n")
    git(repo, "add", "--", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore hook output")
    old = verify.rev_parse_head(repo)
    (repo / "newdir").mkdir()
    assert git(repo, "status", "--porcelain", "-uall", "--ignored") == ""
    leaf = "newdir/deep/tracked" if depth == "deep" else "newdir/tracked"
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, (leaf,))
    [entry] = snapshots
    assert entry["absent_parents"] == (["newdir/deep"] if depth == "deep" else [])
    assert entry["empty_parents"] == ["newdir"]
    (repo / leaf).parent.mkdir(parents=True, exist_ok=True)
    (repo / leaf).write_text("incoming\n")
    git(repo, "add", "--", leaf)
    git(repo, "commit", "-q", "-m", "integrated")
    integrated = verify.rev_parse_head(repo)
    if residue == "ignored-file":
        (repo / "newdir" / "cache.tmp").write_text("target hook output\n")
        expected = ("newdir/cache.tmp",)
    elif residue == "nested-repo":
        git(repo / "newdir", "init", "-q")
        expected = ("newdir/.git",)
    else:
        expected = ()
    assert git(repo, "status", "--porcelain", "-uall") == ""
    assert verify.integrated_paths_drift(repo, integrated, (leaf,)) == ()
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=(leaf,)) == ()

    assert (
        verify.integrated_introduced_directories_drift(
            repo, integrated, run_dir, snapshots, operation_identity="e" * 32
        )
        == expected
    )

    restore = functools.partial(
        verify.restore_integration_ref,
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )
    if residue != "none":
        with pytest.raises(verify.IntegrationRestoreError, match="before the target ref"):
            restore()
        assert verify.rev_parse_head(repo) == integrated
        assert (repo / expected[0]).exists()
        return
    restore()

    assert verify.rev_parse_head(repo) == old
    assert not (repo / leaf).exists()
    assert not (repo / "newdir").exists() or not any((repo / "newdir").iterdir())
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=integrated,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="e" * 32,
    )


def test_receipt_records_no_empty_parent_for_other_ancestors(project, tmp_path):
    """Only an empty directory is proved: a populated one holds what the
    receipt never read; a file ancestor is the entry-type transition's; an
    unpopulated gitlink is the submodule reading's; the repository root is
    nobody's; and a receipt written before the key reads as it did."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "held").mkdir()
    (repo / "held" / "present.txt").write_text("already here\n")
    (repo / "a").write_text("a file\n")
    git(repo, "add", "--", "a")
    git(repo, "commit", "-q", "-m", "a is a file")
    _origin, _checkout, _old = _uninitialized_submodule(repo, tmp_path)

    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("held/new.txt", "a/b", "module/x", "root.txt", "src.txt")
    )

    by_path = {entry["path"]: entry for entry in snapshots}
    assert all(entry["empty_parents"] == [] for entry in by_path.values())
    legacy = [{k: v for k, v in by_path["src.txt"].items() if k != "empty_parents"}]
    assert verify.integration_nonref_state_unchanged(
        repo, run_dir, legacy, [], operation_identity="e" * 32
    )
    with pytest.raises(verify.IntegrationEvidenceError, match="malformed"):
        verify.validate_integration_state_schema(
            run_dir, [{**by_path["src.txt"], "empty_parents": ["held"]}], [], "e" * 32
        )


def test_integrated_introduced_directory_accepts_the_commit_s_own_contents(project, tmp_path):
    """The walk accepts exactly what the integrated commit holds under the new
    directory — files, nested directories, a symlink — and leaves an
    introduced gitlink's populated checkout to the submodule reading, which
    reads it with `--ignored`; an absent directory (nothing snapshotted was
    ever written) has nothing to walk."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    origin = tmp_path / "sub-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "submodule\n", "submodule baseline")
    snapshots, _submodules = verify.capture_integration_state(
        repo, run_dir, "e" * 32, ("newdir/tracked", "newdir/deep/leaf", "newdir/link", "newdir/sub")
    )
    (repo / "newdir" / "deep").mkdir(parents=True)
    (repo / "newdir" / "tracked").write_text("incoming\n")
    (repo / "newdir" / "deep" / "leaf").write_text("incoming\n")
    os.symlink("tracked", repo / "newdir" / "link")
    git(repo, "add", "--", "newdir")
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(origin),
        "newdir/sub",
    )
    git(repo, "commit", "-q", "-m", "integrated: newdir")
    integrated = verify.rev_parse_head(repo)
    (repo / "newdir" / "sub" / "hook.txt").write_text("the submodule reading's\n")

    assert (
        verify.integrated_introduced_directories_drift(
            repo, integrated, run_dir, snapshots, operation_identity="e" * 32
        )
        == ()
    )


def _integrate_submodule_deletion(repo, *, leftover):
    """Commit the integrated shape git leaves when a merge deletes a populated
    submodule: gitlink and `.gitmodules` entry gone from HEAD and index, and —
    `warning: unable to rmdir 'module': Directory not empty` — the populated
    checkout still on disk as `?? module/` unless git could remove it."""
    git(repo, "rm", "-q", "--cached", "--", "module")
    git(repo, "config", "-f", ".gitmodules", "--remove-section", "submodule.module")
    git(repo, "add", "--", ".gitmodules")
    git(repo, "commit", "-q", "-m", "integrated: delete module")
    if not leftover:
        shutil.rmtree(repo / "module")
    return verify.rev_parse_head(repo)


def test_integrated_submodule_deletion_accepts_the_leftover_checkout(project, tmp_path):
    """A merge that deletes a populated submodule succeeds and leaves the
    checkout on disk (`warning: unable to rmdir`, then `?? module/`). The
    integrated-submodule reading demanded a gitlink row from the integrated
    commit for every captured submodule in the incoming set, so that
    deletion — a correct integrated commit necessarily has no row — raised
    "evidence is unavailable", and every populated-submodule deletion was
    refused and restored; the absent-path probe would then have called the
    leftover hook drift (Codex, #796 review). The commit's authority for a
    deleted gitlink is "no gitlink": the leftover is accepted only as the
    exact captured checkout — owned by this repository, clean, at the
    captured HEAD — and reported back so the absent-path probe leaves it to
    this reading; a checkout git did remove is simply absent.

    Ablation: drop the deleted-gitlink arm and the leftover row reds on the
    raise; drop `retained_checkouts` from the probe and it reds on `module`
    reported as drift."""
    repo = project.project
    _origin, checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", ".gitmodules")
    )
    assert submodules == [
        {"path": "module", "head": old_submodule, "gitlink": old_submodule, "flags": "0"}
    ]
    integrated = _integrate_submodule_deletion(repo, leftover=True)
    assert git(repo, "status", "--porcelain") == "?? module/"
    incoming = ("module", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ("module",)
    assert checkout.is_dir()
    assert (
        verify.integrated_paths_drift(repo, integrated, incoming, retained_checkouts=retained) == ()
    )
    assert verify.integrated_paths_drift(repo, integrated, incoming) == ("module",)


def test_integrated_submodule_deletion_accepts_a_removed_checkout(project, tmp_path):
    repo = project.project
    _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", ".gitmodules")
    )
    integrated = _integrate_submodule_deletion(repo, leftover=False)
    incoming = ("module", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ()
    assert (
        verify.integrated_paths_drift(repo, integrated, incoming, retained_checkouts=retained) == ()
    )


@pytest.mark.parametrize("drift", ["nested-file", "moved-head", "not-a-checkout", "foreign-repo"])
def test_integrated_submodule_deletion_refuses_a_changed_leftover(project, tmp_path, drift):
    """The leftover is accepted as the exact captured checkout and nothing
    else: a hook writing into it, moving its HEAD, replacing it with an
    ordinary directory of the same name, or with a fresh repository of its
    own (git dir at `module/.git`, not under this repository's
    `.git/modules`, where a submodule's lives) is drift on an incoming path."""
    repo = project.project
    origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", ".gitmodules")
    )
    integrated = _integrate_submodule_deletion(repo, leftover=True)
    if drift == "nested-file":
        (checkout / "hook.txt").write_text("target hook output\n")
    elif drift == "moved-head":
        commit(origin, "payload.txt", "submodule new\n", "advance submodule")
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "--detach", verify.rev_parse_head(origin))
    elif drift == "not-a-checkout":
        shutil.rmtree(checkout)
        checkout.mkdir()
        (checkout / "hook.txt").write_text("target hook output\n")
    else:
        shutil.rmtree(checkout)
        checkout.mkdir()
        git(checkout, "init", "-q")
        git(checkout, "config", "user.email", "test@example.com")
        git(checkout, "config", "user.name", "Test")
        commit(checkout, "payload.txt", "submodule old\n", "hook-made repository")

    with pytest.raises(verify.IntegrationEvidenceError, match="submodule checkout"):
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=("module", ".gitmodules"), revision=integrated
        )


def test_integrated_submodule_deletion_leaves_a_file_at_the_path_to_the_probe(project, tmp_path):
    """A hook that replaces the leftover with a plain file is not a checkout
    at all: the reading retains nothing, and the absent-path probe reports
    the path."""
    repo = project.project
    _origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", ".gitmodules")
    )
    integrated = _integrate_submodule_deletion(repo, leftover=True)
    shutil.rmtree(checkout)
    checkout.write_text("target hook output\n")
    incoming = ("module", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ()
    assert verify.integrated_paths_drift(
        repo, integrated, incoming, retained_checkouts=retained
    ) == ("module",)


def test_integrated_submodule_replaced_by_a_file_is_held_by_the_diff_readings(project, tmp_path):
    """A captured submodule the integrated commit replaced with a regular
    file was refused as "malformed" evidence; the commit holds a blob there,
    so the index and checkout readings against the commit are its authority
    and the submodule reading has nothing to adjudicate."""
    repo = project.project
    _origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", ".gitmodules")
    )
    git(repo, "rm", "-q", "--cached", "--", "module")
    git(repo, "config", "-f", ".gitmodules", "--remove-section", "submodule.module")
    shutil.rmtree(checkout)
    checkout.write_text("now a file\n")
    git(repo, "add", "--", ".gitmodules", "module")
    git(repo, "commit", "-q", "-m", "integrated: module becomes a file")
    integrated = verify.rev_parse_head(repo)
    incoming = ("module", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ()
    assert (
        verify.integrated_paths_drift(repo, integrated, incoming, retained_checkouts=retained) == ()
    )


def _integrate_new_submodule(repo, tmp_path, *, ignore_all):
    """Commit the integrated shape of an incoming commit that ADDS a submodule
    — gitlink plus `.gitmodules` entry — with the checkout populated at the
    gitlink, as a target hook's `submodule update --init` would leave it."""
    origin = tmp_path / "new-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "new submodule\n", "new submodule baseline")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "newmod")
    if ignore_all:
        git(repo, "config", "-f", ".gitmodules", "submodule.newmod.ignore", "all")
        git(repo, "add", "--", ".gitmodules")
    git(repo, "commit", "-q", "-m", "integrated: add newmod")
    return origin, repo / "newmod", verify.rev_parse_head(origin)


@pytest.mark.parametrize("drift", ["nested-file", "moved-head", "gitlink"])
def test_integrated_new_submodule_checkout_is_validated(project, tmp_path, drift):
    """The integrated-submodule reading iterated the receipt's captured
    submodules only, so a gitlink the incoming commit ADDS got no checkout
    validation at all — and with the incoming `.gitmodules` setting
    `submodule.<name>.ignore = all`, both diff readings of
    `integrated_paths_drift` omit everything inside that checkout: a target
    hook could `submodule update --init` the new submodule, write nested
    files or move its HEAD, and the run recorded `unit-merged` and retired
    its receipt over it (Codex, #796 review). Every incoming path the
    integrated commit holds as a gitlink is now read the same way, captured
    or new: the post-hook index carries exactly that gitlink, and a
    populated checkout is this repository's, clean, and at the gitlink.

    Ablation: drop the new-gitlink iteration and the `nested-file` and
    `moved-head` rows red on the missing raise; the `gitlink` row reds
    too, the index rewrite unseen under `ignore = all`."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ())
    assert submodules == []
    origin, checkout, new_head = _integrate_new_submodule(repo, tmp_path, ignore_all=True)
    integrated = verify.rev_parse_head(repo)
    incoming = ("newmod", ".gitmodules")
    if drift == "nested-file":
        (checkout / "hook.txt").write_text("target hook output\n")
    elif drift == "moved-head":
        commit(origin, "payload.txt", "moved\n", "advance")
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "--detach", verify.rev_parse_head(origin))
    else:
        git(repo, "update-index", "--cacheinfo", f"160000,{'1' * 40},newmod")
    # the blind spot: under `ignore = all` the diff readings see none of it
    assert verify.integrated_paths_drift(repo, integrated, incoming) == ()

    with pytest.raises(verify.IntegrationEvidenceError, match="integrated submodule"):
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=incoming, revision=integrated
        )


@pytest.mark.parametrize("populated", [True, False])
def test_integrated_new_submodule_at_the_gitlink_is_accepted(project, tmp_path, populated):
    """A new gitlink's checkout populated exactly at the gitlink and clean is
    what the integrated commit says; an unpopulated one (the ordinary merge
    result — git does not check submodules out) has nothing to read."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ())
    _origin, checkout, _new_head = _integrate_new_submodule(repo, tmp_path, ignore_all=False)
    if not populated:
        shutil.rmtree(checkout)
        checkout.mkdir()  # git leaves the empty directory for an unpopulated gitlink
    integrated = verify.rev_parse_head(repo)
    incoming = ("newmod", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ()
    assert verify.integrated_paths_drift(repo, integrated, incoming) == ()


def test_integrated_new_submodule_refuses_an_ignored_file_written_into_it(project, tmp_path):
    """A new gitlink's checkout is read with `status -uall`, which does not
    list ignored entries — so a target hook that initializes the new
    submodule and writes a file its own `.gitignore` covers left the
    reading empty, and the run recorded `unit-merged` over hook output the
    receipt would then never restore (Codex, #796 review). The receipt
    proved the path absent, so a checkout there is attempt-era in full and
    ignored entries count; a captured checkout keeps its documented
    reading, its ignored files never read. Ablation: drop `--ignored` from
    the new-checkout reading and this reds on the missing raise."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ())
    origin = tmp_path / "new-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    (origin / ".gitignore").write_text("*.log\n")
    commit(origin, "payload.txt", "new submodule\n", "new submodule with an ignore rule")
    assert git(origin, "ls-files", "--", ".gitignore") == ".gitignore"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "newmod")
    git(repo, "commit", "-q", "-m", "integrated: add newmod")
    integrated = verify.rev_parse_head(repo)
    incoming = ("newmod", ".gitmodules")
    (repo / "newmod" / "hook.log").write_text("target hook output\n")
    assert git(repo / "newmod", "status", "--porcelain", "-uall") == ""

    with pytest.raises(verify.IntegrationEvidenceError, match="submodule checkout"):
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=incoming, revision=integrated
        )


def test_integrated_gitlink_outside_a_sparse_cone_is_accepted(project, tmp_path):
    """A target on a cone-mode sparse checkout holds a gitlink outside the
    cone as a stage-0 `160000` entry with the skip-worktree flag
    (`40004000`) and no checkout. The reading hard-coded the entry's flags
    to `0`, so every integration adding or moving such a gitlink was
    refused as hook drift (Codex, #796 review). The flag hides nothing from
    this reading — the checkout is read from disk, not through the index —
    so skip-worktree is accepted alongside `0`; any other flag is not.
    Ablation: require `0` again and this reds on the raise."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ())
    origin = tmp_path / "new-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "new submodule\n", "new submodule")
    gitlink = verify.rev_parse_head(origin)
    (repo / "keep").mkdir()
    (repo / "keep" / "k.txt").write_text("in the cone\n")
    git(repo, "add", "--", "keep/k.txt")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},other/mod")
    git(repo, "commit", "-q", "-m", "integrated: gitlink outside the cone")
    integrated = verify.rev_parse_head(repo)
    git(repo, "sparse-checkout", "set", "--cone", "keep")
    assert git(repo, "ls-files", "-t", "--", "other/mod") == "S other/mod"
    assert not (repo / "other").exists()

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=("other/mod",), revision=integrated
    )

    assert retained == ()
    git(repo, "sparse-checkout", "disable")


@pytest.mark.parametrize("touched", ["unrelated", "updated"], ids=["unrelated", "updated"])
def test_receipt_preserves_an_operator_s_assume_unchanged_gitlink(project, tmp_path, touched):
    """`git update-index --assume-unchanged` on a submodule is released index
    configuration an operator sets before the run (`flags: 8000`), and it
    hides nothing from readings taken from disk — yet the gitlink predicate
    accepted only `0` and skip-worktree, so a target holding such a
    submodule anywhere, even one the unit never touches, paused every modern
    integration on "no longer the captured indexed gitlink" (Codex, #796
    review). The receipt now records the gitlink's flag word and every
    untouched gitlink's reading compares it exactly. A gitlink the commit
    rewrote may carry its captured word or a fresh one — `git merge` writes
    the entry anew and clears the bit, where a fast-forward or squash keeps
    it (git 2.55) — and `git restore` clears it too, so a refusal's restore
    of a gitlink the unit updated puts the word back and completeness reads
    it exactly; an unrelated gitlink is never restored through and keeps it.

    Ablation: drop `8000` from the captured words and both rows red on the
    capture; match the captured word alone in the integrated reading and the
    updated row reds on "submodule gitlink"; skip
    `_restore_gitlink_index_flags` and it reds on completeness."""
    repo = project.project
    origin, checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    git(repo, "update-index", "--assume-unchanged", "--", "module")
    assert git(repo, "ls-files", "-v", "--", "module") == "h module"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prospective = ("module",) if touched == "updated" else ("src.txt",)
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, prospective)
    assert submodules == [
        {"path": "module", "head": old_submodule, "gitlink": old_submodule, "flags": "8000"}
    ]
    old = verify.rev_parse_head(repo)
    if touched == "updated":
        commit(origin, "payload.txt", "submodule new\n", "advance submodule")
        new_submodule = verify.rev_parse_head(origin)
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "--detach", new_submodule)
        git(repo, "update-index", "--cacheinfo", f"160000,{new_submodule},module")
    else:
        (repo / "src.txt").write_text("integrated\n")
        git(repo, "add", "--", "src.txt")
    git(repo, "commit", "-q", "-m", "integrated")
    new = verify.rev_parse_head(repo)
    # the rewritten entry lost the bit, as under `git merge`; the untouched one keeps it
    assert git(repo, "ls-files", "-v", "--", "module") == (
        "H module" if touched == "updated" else "h module"
    )

    assert verify.integration_nonref_state_unchanged(
        repo, run_dir, snapshots, submodules, exclude_paths=prospective, operation_identity="c" * 32
    )
    assert (
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=prospective, revision=new
        )
        == ()
    )
    verify.restore_integration_ref(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )

    assert verify.rev_parse_head(repo) == old
    assert verify.rev_parse_head(checkout) == old_submodule
    assert git(repo, "ls-files", "-v", "--", "module") == "h module"
    assert verify.integration_restoration_complete(
        repo,
        "refs/heads/main",
        old_revision=old,
        new_revision=new,
        run_dir=run_dir,
        snapshots=snapshots,
        submodules=submodules,
        operation_identity="c" * 32,
    )


@pytest.mark.parametrize("reading", ["nonref-unchanged", "integrated"])
def test_receipt_refuses_a_hook_s_flip_of_a_captured_gitlink_s_flags(project, tmp_path, reading):
    """The captured word is matched exactly: a hook setting assume-unchanged
    on a gitlink the receipt captured without it — or clearing one it
    captured with it — is drift in both directions.

    Ablation: accept any captured word in `_gitlink_index_matches` and every
    row reds."""
    repo = project.project
    _origin, _checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ("module",))
    assert submodules[0]["flags"] == "0"
    revision = verify.rev_parse_head(repo)
    git(repo, "update-index", "--assume-unchanged", "--", "module")

    if reading == "nonref-unchanged":
        assert not verify.integration_nonref_state_unchanged(
            repo, run_dir, _snapshots, submodules, operation_identity="c" * 32
        )
    else:
        with pytest.raises(verify.IntegrationEvidenceError, match="submodule gitlink"):
            verify.validate_integrated_submodule_state(
                repo, submodules, prospective_paths=("module",), revision=revision
            )


def test_receipt_reads_a_submodule_entry_without_a_flag_word(project, tmp_path):
    """A receipt written before the flag word was recorded carries no
    `flags`: the schema accepts it, and the readings take the fresh-gitlink
    words for it — `0` accepted, assume-unchanged drift as before."""
    repo = project.project
    _origin, _checkout, old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshots, submodules = verify.capture_integration_state(repo, run_dir, "c" * 32, ())
    legacy = [{"path": "module", "head": old_submodule, "gitlink": old_submodule}]

    assert verify.integration_nonref_state_unchanged(
        repo, run_dir, snapshots, legacy, operation_identity="c" * 32
    )
    git(repo, "update-index", "--assume-unchanged", "--", "module")
    assert not verify.integration_nonref_state_unchanged(
        repo, run_dir, snapshots, legacy, operation_identity="c" * 32
    )
    with pytest.raises(verify.IntegrationEvidenceError, match="malformed"):
        verify.validate_integration_state_schema(
            run_dir, snapshots, [{**legacy[0], "flags": "20004000"}], "c" * 32
        )


def test_integrated_gitlink_with_a_foreign_index_flag_is_refused(project, tmp_path):
    """Skip-worktree is the one flag word a gitlink may carry besides none;
    any other bit a hook sets on the entry is still drift."""
    repo = project.project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(repo, run_dir, "e" * 32, ())
    _integrate_new_submodule(repo, tmp_path, ignore_all=False)
    integrated = verify.rev_parse_head(repo)
    git(repo, "update-index", "--assume-unchanged", "--", "newmod")

    with pytest.raises(verify.IntegrationEvidenceError, match="submodule gitlink"):
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=("newmod",), revision=integrated
        )


def test_integrated_submodule_deletion_in_a_linked_worktree_target(project, tmp_path):
    """A target that is itself a linked worktree keeps its submodules' git
    dirs under its own per-worktree git dir (`.git/worktrees/<id>/modules/`),
    and its `.git` is a file. The orphan ownership proof looked for the git
    dir under `<root>/.git/modules` — a main-checkout assumption — so a valid
    submodule deletion on such a target read as a foreign directory and was
    refused (Codex, #796 review). The modules directory is now derived from
    the git dir git reports for the target. Ablation: assume `root/.git`
    again and this reds on the raise."""
    linked = tmp_path / "linked"
    git(project.project, "worktree", "add", "-q", str(linked), "-b", "linked")
    assert (linked / ".git").is_file()
    origin = tmp_path / "sub-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    commit(origin, "payload.txt", "submodule old\n", "submodule baseline")
    old_submodule = verify.rev_parse_head(origin)
    git(linked, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "module")
    git(linked, "commit", "-q", "-m", "add populated submodule")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        linked, run_dir, "f" * 32, ("module", ".gitmodules")
    )
    assert submodules == [
        {"path": "module", "head": old_submodule, "gitlink": old_submodule, "flags": "0"}
    ]
    integrated = _integrate_submodule_deletion(linked, leftover=True)
    assert git(linked, "status", "--porcelain") == "?? module/"
    incoming = ("module", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        linked, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ("module",)
    assert (
        verify.integrated_paths_drift(linked, integrated, incoming, retained_checkouts=retained)
        == ()
    )


def _integrate_submodule_replacement(repo, checkout):
    """Commit the integrated shape git leaves when a merge replaces a populated
    submodule with an ordinary tracked directory: gitlink and `.gitmodules`
    entry gone, `module/file.txt` held by the commit and written INTO the
    old checkout (`warning: unable to rmdir`), so `.git` and the old payload
    sit beside the new tracked file."""
    git(repo, "rm", "-q", "--cached", "--", "module")
    git(repo, "config", "-f", ".gitmodules", "--remove-section", "submodule.module")
    (checkout / "file.txt").write_text("now a tracked directory\n")
    # staged the way a merge stages it — `git add` skips a path under the old
    # checkout's `.git`, the merge writes the index entry directly
    blob = git(repo, "hash-object", "-w", "--", "module/file.txt")
    git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},module/file.txt")
    git(repo, "add", "--", ".gitmodules")
    git(repo, "commit", "-q", "-m", "integrated: module becomes a directory")
    return verify.rev_parse_head(repo)


def test_integrated_submodule_replaced_by_a_directory_retains_the_leftover_checkout(
    project, tmp_path
):
    """An incoming commit that replaces a captured submodule with an ordinary
    tracked directory has no `ls-tree -r` row for the old gitlink path, only
    for its descendants (`module/file.txt`). The deleted-gitlink arm first
    took the directory for a leftover checkout and refused a valid
    replacement over the tracked file it found "untracked" from the
    submodule's view; the prefix reading that fixed it (523b248c) then left
    the leftover entirely to the diff readings — which cover only what the
    superproject's index tracks, so a hook's untracked write into the old
    checkout went unseen and the run recorded `unit-merged` over it (Codex,
    #796 review). The leftover git left INSIDE the replaced directory is
    adjudicated like the one it leaves at a deleted gitlink: owned by this
    repository, at the captured HEAD, and holding nothing but what the
    integrated tree holds under the path — the merge's own writes into it,
    which the diff readings own — and it is retained so the whole-tree stray
    reading leaves it to this one. Ablation: drop the replaced-directory arm
    and this reds on `retained == ()`."""
    repo = project.project
    _origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", "module/file.txt", ".gitmodules")
    )
    integrated = _integrate_submodule_replacement(repo, checkout)
    assert (checkout / ".git").exists() and (checkout / "payload.txt").exists()
    assert (
        git(repo, "ls-tree", "-r", "--name-only", integrated, "--", "module") == "module/file.txt"
    )
    assert git(repo, "status", "--porcelain", "-uall") == "?? module/payload.txt"
    assert git(checkout, "status", "--porcelain", "-uall") == "?? file.txt"
    incoming = ("module", "module/file.txt", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ("module",)
    assert (
        verify.integrated_paths_drift(repo, integrated, incoming, retained_checkouts=retained) == ()
    )
    assert (
        verify.integrated_stray_paths(
            repo, tolerated=(), incoming=incoming, retained_checkouts=retained
        )
        == ()
    )
    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=incoming) == (
        "module/payload.txt",
    )


@pytest.mark.parametrize("drift", ["nested-file", "deleted-payload", "moved-head", "foreign-repo"])
def test_integrated_submodule_replaced_by_a_directory_refuses_a_changed_leftover(
    project, tmp_path, drift
):
    """The leftover inside the replaced directory is accepted as the captured
    checkout plus the integrated tree's own writes and nothing else: a hook
    writing an untracked file into it, deleting the captured payload the
    integrated tree does not hold, moving its HEAD, or re-initialising it as
    a repository of its own is drift on an incoming path."""
    repo = project.project
    origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", "module/file.txt", ".gitmodules")
    )
    integrated = _integrate_submodule_replacement(repo, checkout)
    if drift == "nested-file":
        (checkout / "hook.txt").write_text("target hook output\n")
    elif drift == "deleted-payload":
        (checkout / "payload.txt").unlink()
    elif drift == "moved-head":
        commit(origin, "payload.txt", "submodule new\n", "advance submodule")
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "--detach", verify.rev_parse_head(origin))
    else:
        (checkout / ".git").unlink()
        git(checkout, "init", "-q")
    incoming = ("module", "module/file.txt", ".gitmodules")

    with pytest.raises(verify.IntegrationEvidenceError, match="submodule checkout"):
        verify.validate_integrated_submodule_state(
            repo, submodules, prospective_paths=incoming, revision=integrated
        )


def test_integrated_submodule_replaced_by_a_directory_without_a_leftover(project, tmp_path):
    """A replaced directory git could write cleanly — no `.git` inside it —
    holds no checkout to adjudicate: nothing is retained, the directory's
    contents are the diff readings' and the stray reading's business."""
    repo = project.project
    _origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _snapshots, submodules = verify.capture_integration_state(
        repo, run_dir, "d" * 32, ("module", "module/file.txt", ".gitmodules")
    )
    integrated = _integrate_submodule_replacement(repo, checkout)
    (checkout / ".git").unlink()
    (checkout / "payload.txt").unlink()
    assert git(repo, "status", "--porcelain", "-uall") == ""
    incoming = ("module", "module/file.txt", ".gitmodules")

    retained = verify.validate_integrated_submodule_state(
        repo, submodules, prospective_paths=incoming, revision=integrated
    )

    assert retained == ()
    (checkout / "hook.txt").write_text("target hook output\n")
    assert verify.integrated_stray_paths(
        repo, tolerated=(), incoming=incoming, retained_checkouts=retained
    ) == ("module/hook.txt",)


@pytest.mark.parametrize("shape", ["edited", "staged", "deleted", "untracked", "renamed"])
def test_integrated_stray_paths_reports_a_change_outside_every_receipt_set(project, shape):
    """The receipt snapshots the incoming set, the paths that were dirty
    before the merge, and the declared artifacts — a clean tracked file
    outside all of them has no baseline, and no post-hook reading looked at
    it: a target hook editing, staging, deleting, or renaming it, or writing a
    new file beside it, left the run recording `unit-merged` over unverified
    hook output (Codex, #796 review). The whole-tree `status` reading takes
    the pre-merge tolerated strays as the only dirt the target may hold after
    the hooks — the merge committed, the incoming set is the diff readings' —
    and reports every other entry by path.

    Ablation: return `()` and every row reds."""
    repo = project.project
    (repo / "notes.txt").write_text("clean before the merge\n")
    git(repo, "add", "--", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes.txt is clean")
    if shape == "edited":
        (repo / "notes.txt").write_text("target hook output\n")
        expected = ("notes.txt",)
    elif shape == "staged":
        (repo / "notes.txt").write_text("target hook output\n")
        git(repo, "add", "--", "notes.txt")
        expected = ("notes.txt",)
    elif shape == "deleted":
        (repo / "notes.txt").unlink()
        expected = ("notes.txt",)
    elif shape == "untracked":
        (repo / "hook.log").write_text("target hook output\n")
        expected = ("hook.log",)
    else:
        git(repo, "mv", "--", "notes.txt", "moved.txt")
        expected = ("moved.txt",)

    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("src.txt",)) == expected


@pytest.mark.parametrize(
    "shape", ["edited-hook-script", "staged-hook-script", "new-policy", "new-profile"]
)
def test_integrated_stray_paths_reads_the_automator_directory(project, shape):
    """`dirty_paths` excludes the whole `.bmad-loop/` subtree — the
    orchestrator's own working directory — so a target hook editing or
    staging a clean tracked file there (the hook relay script, a committed
    `policy.toml`, a profile overlay), or writing a new one, was outside the
    stray reading and the run recorded `unit-merged` over it (Codex, #796
    review). The stray reading now takes the automator directory too,
    leaving out only the run's own records (`runs/`, `archive/`, `cache/`,
    `decisions.json`, `operator/`, `operator-actions.json`).

    Ablation: drop `automator_dirty_paths` from the reading and every row
    reds."""
    repo = project.project
    (repo / ".bmad-loop").mkdir(exist_ok=True)
    (repo / ".bmad-loop" / "bmad_loop_hook.py").write_text("# relay\n")
    git(repo, "add", "--", ".bmad-loop/bmad_loop_hook.py")
    git(repo, "commit", "-q", "-m", "hook relay script")
    if shape == "edited-hook-script":
        (repo / ".bmad-loop" / "bmad_loop_hook.py").write_text("# rewritten by a hook\n")
        expected = (".bmad-loop/bmad_loop_hook.py",)
    elif shape == "staged-hook-script":
        (repo / ".bmad-loop" / "bmad_loop_hook.py").write_text("# rewritten by a hook\n")
        git(repo, "add", "--", ".bmad-loop/bmad_loop_hook.py")
        expected = (".bmad-loop/bmad_loop_hook.py",)
    elif shape == "new-policy":
        (repo / ".bmad-loop" / "policy.toml").write_text("[engine]\n")
        expected = (".bmad-loop/policy.toml",)
    else:
        (repo / ".bmad-loop" / "profiles").mkdir()
        (repo / ".bmad-loop" / "profiles" / "claude.toml").write_text("adapter = 'x'\n")
        expected = (".bmad-loop/profiles/claude.toml",)
    assert verify.dirty_paths(repo) == {}

    assert verify.integrated_stray_paths(repo, tolerated=(), incoming=("src.txt",)) == expected


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_incoming_collision_guard_reads_the_automator_directory(project, tmp_path, staged):
    """The pre-merge guard read the same `.bmad-loop`-less dirt, so an
    operator's uncommitted edit to a tracked file there was neither
    tolerated (journaled, and the receipt's to leave alone after the hooks)
    nor — staged — blocking, though a fast-forwardable squash folds a staged
    stray into the unit's commit wherever it lives (#618). It reads the
    automator directory now: unstaged is tolerated, staged blocks."""
    repo = project.project
    (repo / ".bmad-loop").mkdir(exist_ok=True)
    (repo / ".bmad-loop" / "bmad_loop_hook.py").write_text("# relay\n")
    git(repo, "add", "--", ".bmad-loop/bmad_loop_hook.py")
    git(repo, "commit", "-q", "-m", "hook relay script")
    _branch_with(repo, tmp_path, adds={"feature.txt": "branch\n"})
    (repo / ".bmad-loop" / "bmad_loop_hook.py").write_text("# operator's edit\n")
    if staged:
        git(repo, "add", "--", ".bmad-loop/bmad_loop_hook.py")
        with pytest.raises(verify.GitError, match="staged changes.*bmad_loop_hook.py"):
            verify.plan_incoming_collisions(repo, "main", "feat")
        return
    calls: list[list[str]] = []

    plan = verify.plan_incoming_collisions(repo, "main", "feat", on_tolerated=calls.append)

    assert plan.tolerated == (".bmad-loop/bmad_loop_hook.py",)
    assert calls == [[".bmad-loop/bmad_loop_hook.py"]]
    assert (
        verify.integrated_stray_paths(repo, tolerated=plan.tolerated, incoming=("feature.txt",))
        == ()
    )


def test_integrated_stray_paths_leaves_the_receipt_sets_to_their_own_readings(project, tmp_path):
    """Tolerated strays are the snapshot's (proved unchanged there), the
    incoming set is the diff readings', a retained leftover checkout — and
    everything under it — is the submodule reading's, and the run's own
    records under `.bmad-loop/` are its own; none of them is a stray."""
    repo = project.project
    _origin, checkout, _old_submodule = _add_test_submodule(repo, tmp_path)
    (repo / "stray.txt").write_text("operator dirt tolerated before the merge\n")
    (repo / "src.txt").write_text("incoming, owned by the diff readings\n")
    for record in ("runs/r1/state.json", "archive/r0/state.json", "cache/x", "operator/a.json"):
        (repo / ".bmad-loop" / record).parent.mkdir(parents=True, exist_ok=True)
        (repo / ".bmad-loop" / record).write_text("{}\n")
    (repo / ".bmad-loop" / "decisions.json").write_text("{}\n")
    (repo / ".bmad-loop" / "operator-actions.json").write_text("{}\n")
    _integrate_submodule_deletion(repo, leftover=True)
    assert sorted(verify.dirty_paths(repo)) == ["module/", "src.txt", "stray.txt"]

    assert (
        verify.integrated_stray_paths(
            repo, tolerated=("stray.txt",), incoming=("src.txt",), retained_checkouts=("module",)
        )
        == ()
    )
    assert verify.integrated_stray_paths(
        repo, tolerated=(), incoming=(), retained_checkouts=()
    ) == ("module/", "src.txt", "stray.txt")
