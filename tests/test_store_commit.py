"""`ticketstore.StoreCommitter` and the tickets engine's commits in a ticket store
that is its own git repository (T3 nested, T4 external).

The committer is exercised against a bare store repository; the engine tests run
`TicketsEngine` in place over `project_v7_store_repo` with the mock adapter and an
injected `FakeTicketTree` (the worktree-isolation rows are in
tests/test_engine_worktree.py). Real-spawn budget: none.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from conftest import _store_repo, git, ticket_row
from test_tickets_engine import (
    _builds_finishing,
    _builds_shared,
    _epic_tree,
    _kinds,
    _marking_tree,
    _plan_status,
    _policy,
    _resume,
    _retro_doc,
    _retro_policy,
    _retro_writes,
    _shared_engine,
    _tree,
)

from bmad_loop import verify
from bmad_loop.journal import load_state
from bmad_loop.model import PAUSE_EPIC_BOUNDARY, Phase
from bmad_loop.policy import TicketsPolicy
from bmad_loop.ticketstore import StoreCommit, StoreCommitError, StoreCommitter

PLAN = b"---\nstatus: built\n---\n"
DONE = b"---\nstatus: done\n---\n"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A store repository holding a committed plan and a human's notes."""
    root = tmp_path / "store"
    root.mkdir()
    (root / "plan.md").write_bytes(PLAN)
    (root / "notes.md").write_bytes(b"notes\n")
    _store_repo(root)
    return root


def _subjects(repo: Path) -> list[str]:
    return git(repo, "log", "--format=%s").splitlines()


def _head_files(repo: Path) -> list[str]:
    return git(repo, "show", "--name-only", "--format=", "HEAD").split()


def test_store_commit_pathspec_only(store, tmp_path):
    """Only the named path is committed: a human's unstaged edit and a staged new
    file beside it stay exactly as they were, and the message carries the code
    trailers. A second commit with nothing changed is a no-op. Ablation
    (performed): commit the whole store (`commit_paths(repo, body, [repo])`) and
    the other two paths ride the commit — this FAILS."""
    (store / "plan.md").write_bytes(DONE)
    (store / "notes.md").write_bytes(b"notes, edited by hand\n")
    (store / "draft.md").write_bytes(b"draft\n")
    git(store, "add", "draft.md")
    code = tmp_path / "code"
    committer = StoreCommitter(store, code)

    result = committer.commit(
        "1.1",
        [store / "plan.md"],
        "chore(tickets): 1.1 done",
        code_commit="c0de",
        code_baseline="ba5e",
    )

    assert result == StoreCommit(verify.rev_parse_head(store))
    assert _head_files(store) == ["plan.md"]
    body = git(store, "log", "-1", "--format=%B")
    assert body.splitlines()[0] == "chore(tickets): 1.1 done"
    assert f"Bmad-Code-Repo: {code.as_posix()}" in body
    assert "Bmad-Code-Commit: c0de" in body and "Bmad-Code-Range: ba5e..c0de" in body
    assert verify.dirty_paths(store) == {"notes.md": " M", "draft.md": "A "}
    assert committer.commit("1.1", [store / "plan.md"], "again") == StoreCommit(None)


def test_store_commit_refuses_foreign_staged_plan(store):
    """A human has staged their own version of the plan: staging the marked file
    over it would destroy that version, so nothing is staged or committed, the
    plan is reported foreign, and the index still holds the human's bytes.
    Ablation (performed): drop the foreign check in `StoreCommitter.commit` and
    the plan is committed over the staged version — this FAILS."""
    head = verify.rev_parse_head(store)
    staged = b"---\nstatus: built\n---\n\nhand-edited\n"
    (store / "plan.md").write_bytes(staged)
    git(store, "add", "plan.md")
    (store / "plan.md").write_bytes(DONE)

    result = StoreCommitter(store, store).commit("1.1", [store / "plan.md"], "chore")

    assert result == StoreCommit(None, ("plan.md",))
    assert verify.rev_parse_head(store) == head
    assert git(store, "show", ":plan.md") == staged.decode("utf-8").rstrip("\n")


def test_store_commit_refuses_foreign_staged_version_of_an_absent_path(store):
    """A path the commit names is gone from disk while a human's staged version
    of it sits in the index: staging the deletion would destroy that version, so
    the path is reported foreign and nothing is committed. A plain deletion (the
    index at HEAD's content) still commits. Ablation (performed): skip the foreign
    check for an absent path and the deletion is committed over the staged
    version — this FAILS."""
    head = verify.rev_parse_head(store)
    staged = b"---\nstatus: built\n---\n\nhand-edited\n"
    (store / "plan.md").write_bytes(staged)
    git(store, "add", "plan.md")
    (store / "plan.md").unlink()
    committer = StoreCommitter(store, store)

    result = committer.commit("1.1", [store / "plan.md"], "chore")

    assert result == StoreCommit(None, ("plan.md",))
    assert verify.rev_parse_head(store) == head
    assert git(store, "show", ":plan.md") == staged.decode("utf-8").rstrip("\n")

    git(store, "reset", "-q", "--", "plan.md")
    assert committer.commit("1.1", [store / "plan.md"], "chore").sha is not None
    assert "plan.md" not in git(store, "ls-files").split()


def test_store_commit_into_an_unborn_store_repository(tmp_path):
    """A store repository with no commit yet (unborn HEAD) is an empty baseline,
    not a git fault: the plan commits as the store's first commit, while a human's
    staged version distinct from the file is still foreign. Ablation (performed):
    let the HEAD read fail on an unborn repository and every store commit raises
    `StoreCommitError` — this FAILS."""
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "store@example.invalid")
    git(root, "config", "user.name", "store")
    (root / "plan.md").write_bytes(b"---\nstatus: draft\n---\n")
    git(root, "add", "plan.md")
    (root / "plan.md").write_bytes(PLAN)
    committer = StoreCommitter(root, root)

    assert committer.commit("1.1", [root / "plan.md"], "chore") == StoreCommit(None, ("plan.md",))
    assert verify.head_unborn(root)

    git(root, "reset", "-q")
    result = committer.commit("1.1", [root / "plan.md"], "chore")

    assert result.sha is not None and result.foreign == ()
    assert git(root, "show", "HEAD:plan.md") == PLAN.decode("utf-8").rstrip("\n")
    assert not verify.head_unborn(root)


def test_store_commit_broken_head_is_not_read_as_unborn(store):
    """HEAD's branch exists but names a missing object: that is a broken repository,
    not an empty one, so the HEAD read fault still raises (typed)."""
    (store / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n")

    assert not verify.head_unborn(store)
    with pytest.raises(StoreCommitError, match="1.1"):
        StoreCommitter(store, store).commit("1.1", [store / "plan.md"], "chore")


def test_store_commit_git_fault_raises_typed(store, monkeypatch):
    """A git fault is a `StoreCommitError` naming the unit, never a bare
    `GitError` a best-effort caller has no handler for."""

    def boom(repo, message, paths):
        raise verify.GitError("index.lock exists")

    monkeypatch.setattr(verify, "commit_paths", boom)
    with pytest.raises(StoreCommitError, match="1.1"):
        StoreCommitter(store, store).commit("1.1", [store / "plan.md"], "chore")


@pytest.mark.parametrize("fault", ["git", "foreign"])
def test_store_commit_failure_keeps_latch_and_journals(project_v7_store_repo, monkeypatch, fault):
    """The plan is marked `done` after the code lands, but its store commit fails
    (a git fault, or a human's staged version of the plan): the run goes on — the
    commit is bookkeeping — with `store-commit-failed` / `store-commit-foreign-dirt`
    journaled and the latch kept. At run end the commit is retried, fails again,
    and the run pauses at `ticket-tree` (`store-commit-owed`) instead of finishing,
    so the resume (the fault gone) marks again, a no-op, then commits the plan and
    clears the latch. Ablation (performed): clear the latch whatever the commit did
    and the resume has nothing to replay — this FAILS; drop the run-end settle and
    the run finishes, which `resume` refuses — this FAILS."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    real = verify.commit_paths
    if fault == "git":

        def commit_paths(repo, message, paths):
            if Path(repo).resolve() == store.resolve():
                raise verify.GitError("index.lock exists")
            return real(repo, message, paths)

        monkeypatch.setattr(verify, "commit_paths", commit_paths)
    building = _builds_shared(layout, tree, plan)

    def build_then_stage(spec):
        result = building(spec)
        plan.write_bytes(plan.read_bytes() + b"\nhand-edited\n")
        git(store, "add", plan.relative_to(store).as_posix())
        return result

    script = [build_then_stage if fault == "foreign" else building]
    engine, _ = _shared_engine(layout, tree, script=script, max_stories=1)
    store_head = verify.rev_parse_head(store)

    result = engine.run()

    assert result.paused
    state = load_state(engine.run_dir)
    assert not state.finished
    assert (state.paused_stage, state.paused_story_key) == ("ticket-tree", "1.1")
    task = state.tasks["1.1"]
    assert task.phase == Phase.DONE and task.ticket_done_pending
    assert _plan_status(plan) == "done"
    assert verify.rev_parse_head(store) == store_head
    kind = "store-commit-failed" if fault == "git" else "store-commit-foreign-dirt"
    assert [e["story_key"] for e in _kinds(engine, kind)] == ["1.1", "1.1"]
    assert [e["story_key"] for e in _kinds(engine, "store-commit-owed")] == ["1.1"]
    assert not _kinds(engine, "store-committed")

    monkeypatch.setattr(verify, "commit_paths", real)
    if fault == "foreign":
        git(store, "reset", "-q")
    resumed = _resume(engine, tree)
    resumed._replay_post_landing_writes()

    assert not load_state(engine.run_dir).tasks["1.1"].ticket_done_pending
    assert [(m.target, m.outcome) for m in tree.marks] == [
        ("done", "written"),
        ("done", "noop"),
        ("done", "noop"),
    ]
    assert _subjects(store)[0] == "chore(tickets): 1.1 done [bmad-loop test-run]"
    assert [e["story_key"] for e in _kinds(resumed, "store-committed")] == ["1.1"]


def test_store_commit_failed_mid_run_is_retried_before_finishing(
    project_v7_store_repo, monkeypatch
):
    """A store commit that fails once (a transient git fault) keeps its latch, and
    the run retries it before recording `finished` — a finished run refuses
    `resume`, so no later retry could reach it: the plan is committed, the latch
    cleared, and the run finishes. Ablation (performed): drop the
    `_settle_owed_store_commits` call in `_run_end_retrospective` and the run
    finishes with the latch set and the plan uncommitted — this FAILS."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    real = verify.commit_paths
    faults = [verify.GitError("index.lock exists")]

    def commit_paths(repo, message, paths):
        if faults and Path(repo).resolve() == store.resolve():
            raise faults.pop()
        return real(repo, message, paths)

    monkeypatch.setattr(verify, "commit_paths", commit_paths)
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )

    result = engine.run()

    assert not result.paused
    state = load_state(engine.run_dir)
    assert state.finished and not state.tasks["1.1"].ticket_done_pending
    assert [e["story_key"] for e in _kinds(engine, "store-commit-failed")] == ["1.1"]
    assert [e["story_key"] for e in _kinds(engine, "run-end-ticket-mark")] == ["1.1"]
    assert [e["story_key"] for e in _kinds(engine, "store-committed")] == ["1.1"]
    assert _subjects(store)[0] == "chore(tickets): 1.1 done [bmad-loop test-run]"


def test_mark_done_never_still_commits_the_built_plan(project_v7_store_repo, monkeypatch):
    """`mark_done = "never"` writes no mark, but the session's own `built` write
    sits in the store repository, which no unit commit carries (T1's does): the
    task latches a commit-only obligation, and once the code lands the plan is
    committed at `built` and the latch cleared. Ablation (performed): latch
    nothing under "never" and the run finishes with the plan uncommitted — this
    FAILS."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    pol = dataclasses.replace(_policy(), tickets=TicketsPolicy(mark_done="never"))
    engine, _ = _shared_engine(
        layout, tree, policy=pol, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )

    result = engine.run()

    assert not result.paused
    state = load_state(engine.run_dir)
    task = state.tasks["1.1"]
    assert state.finished and task.phase == Phase.DONE and not task.ticket_commit_pending
    assert not tree.marks
    assert _plan_status(plan) == "built"
    assert [e["target"] for e in _kinds(engine, "ticket-mark-latched")] == ["built"]
    assert _subjects(store)[0] == "chore(tickets): 1.1 built [bmad-loop test-run]"
    rel = plan.relative_to(store).as_posix()
    assert rel in _head_files(store) and rel not in verify.dirty_paths(store)


def test_retro_doc_committed_in_store_repo(project_v7_store_repo, monkeypatch):
    """The epic's retrospective document lands in the store repository — a
    `chore(retro)` commit holding the document alone — and the human's planning
    edit beside it in the store never pauses the run: it pauses only for the
    epic's closure, the edit left uncommitted. Ablation (performed): drop the
    store commit in `TicketsEngine._settle_retro_tree` and the document stays
    uncommitted in the store — this FAILS."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _epic_tree(layout)
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    human = layout.initiative_folder / "notes.md"
    human.write_bytes(b"planning in progress\n")
    engine, _ = _shared_engine(
        layout,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(layout, tree, plan), _retro_writes(layout)],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_EPIC_BOUNDARY
    assert "close epic-cart with bmad-agent-po" in (state.paused_reason or "")
    assert _subjects(store)[:2] == [
        "chore(retro): epic-cart retrospective [bmad-loop test-run]",
        "chore(tickets): 1.1 done [bmad-loop test-run]",
    ]
    assert _head_files(store) == [_retro_doc(layout).relative_to(store).as_posix()]
    assert verify.dirty_paths(store) == {human.relative_to(store).as_posix(): "??"}
    assert [e["story_key"] for e in _kinds(engine, "store-committed")] == [
        "1.1",
        "epic-cart-retrospective",
    ]
    assert not _kinds(engine, "retro-auto-dirty")
    assert git(layout.project, "status", "--porcelain") == ""


@pytest.mark.parametrize("fault", ["git", "foreign"])
def test_retro_store_commit_failure_pauses_and_resume_commits(
    project_v7_store_repo, monkeypatch, fault
):
    """A retrospective whose store commit fails (a git fault, or a human's staged
    draft of the document) pauses at `epic-boundary` with no closure recorded, so
    the resume re-enters the boundary: `already-done` finds the document still
    uncommitted in the store repository, commits it, and only then records the
    closure and pauses for it. Ablation (performed): drop the pause on a failed
    store commit in `TicketsEngine._settle_retro_tree` and the run records the
    closure with the document uncommitted — this FAILS; drop the store-repository
    read in `_retro_doc_uncommitted` and the resume records the closure without
    committing it — this FAILS too."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _epic_tree(layout)
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    doc = _retro_doc(layout)
    retro = _retro_writes(layout)
    if fault == "git":
        real = verify.commit_paths
        faults = [verify.GitError("index.lock exists")]

        def commit_paths(repo, message, paths):
            if faults and message.startswith("chore(retro)"):
                raise faults.pop()
            return real(repo, message, paths)

        monkeypatch.setattr(verify, "commit_paths", commit_paths)
    else:
        written = retro

        def retro(spec):
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("a human's draft\n", encoding="utf-8")
            git(store, "add", doc.relative_to(store).as_posix())
            return written(spec)

    engine, _ = _shared_engine(
        layout,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(layout, tree, plan), retro],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert "retrospective is not committed in" in (state.paused_reason or "")
    assert state.ticket_epic_closures == {}
    assert _subjects(store)[0] == "chore(tickets): 1.1 done [bmad-loop test-run]"
    kind = "store-commit-failed" if fault == "git" else "store-commit-foreign-dirt"
    assert [e["story_key"] for e in _kinds(engine, kind)] == ["epic-cart-retrospective"]

    if fault == "foreign":
        git(store, "reset", "-q")
    resumed = _resume(engine, tree)

    assert resumed.run().paused

    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_EPIC_BOUNDARY
    assert "close epic-cart with bmad-agent-po" in (state.paused_reason or "")
    assert state.ticket_epic_closures == {"epic-cart": "accepted"}
    assert _subjects(store)[0] == "chore(retro): epic-cart retrospective [bmad-loop test-run]"
    assert _head_files(store) == [doc.relative_to(store).as_posix()]
    assert [e["story_key"] for e in _kinds(resumed, "store-committed")][-1] == (
        "epic-cart-retrospective"
    )


def test_retro_resume_reads_an_unresolvable_doc_as_uncommitted(project_v7_store_repo, monkeypatch):
    """A resume after a failed retrospective store commit whose repository probe
    cannot resolve its paths reads the document as uncommitted, not as outside
    every repository, so `already-done` retries the store commit before recording the
    closure. Ablation (performed): fold the resolve fault into "outside" in
    `TicketsEngine._retro_doc_uncommitted` and the resume records the closure
    with the document uncommitted — this FAILS."""
    layout = project_v7_store_repo
    store = layout.output_folder
    tree = _epic_tree(layout)
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    doc = _retro_doc(layout)
    real_commit = verify.commit_paths
    faults = [verify.GitError("index.lock exists")]

    def commit_paths(repo, message, paths):
        if faults and message.startswith("chore(retro)"):
            raise faults.pop()
        return real_commit(repo, message, paths)

    monkeypatch.setattr(verify, "commit_paths", commit_paths)
    engine, _ = _shared_engine(
        layout,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(layout, tree, plan), _retro_writes(layout)],
    )
    assert engine.run().paused
    assert load_state(engine.run_dir).ticket_epic_closures == {}

    real_resolve = Path.resolve
    faulted: list[Path] = []

    def resolve(self, strict=False):
        if sys._getframe(1).f_code.co_name == "_retro_doc_uncommitted":
            faulted.append(self)
            raise OSError(13, "Permission denied")
        return real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    resumed = _resume(engine, tree)

    assert resumed.run().paused

    assert faulted
    state = load_state(engine.run_dir)
    assert state.ticket_epic_closures == {"epic-cart": "accepted"}
    assert _subjects(store)[0] == "chore(retro): epic-cart retrospective [bmad-loop test-run]"
    assert doc.relative_to(store).as_posix() not in verify.dirty_paths(store)
