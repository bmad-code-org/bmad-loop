"""Phase 3: isolation="worktree" — each unit runs in its own git worktree and
merges back into the target branch locally. Sessions run inside the worktree
(spec.cwd), so the effects here write artifacts rebased onto that checkout.

Exercised end-to-end against the conftest `project` sandbox with the mock
adapter (no tmux, no LLM).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import (
    _OK,
    _exists_run,
    _seeded_then_touch,
    _spec_baseline,
    _touch_run,
    git,
    set_sprint,
    write_spec,
    write_sprint,
)

from bmad_loop import verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.install import BMAD_SCRIPTS_SEED_REL, renderer_stub_resolved
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy
from bmad_loop.verify import (
    branch_exists,
    current_branch,
    rev_parse_head,
    worktree_clean,
    worktree_list,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def wt_policy(*, limits: LimitsPolicy | None = None, **scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree", **scm),
        limits=limits if limits is not None else LimitsPolicy(),
    )


def commit_sprint(project, statuses: dict[str, str]) -> None:
    """Worktrees are checkouts of a commit, so the sprint board (and artifact
    dirs) must be committed before the run, not left untracked."""
    write_sprint(project, statuses)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sprint")


def wt_dev_effect(project, story_key, *, final_status="done", followup_review=True):
    """Dev session running inside the unit worktree (spec.cwd). Mirrors the
    bmad-dev-auto skill: self-finalizes the spec to done, never writes the sprint
    board (the orchestrator advances it via the B2 seam, inside the worktree).
    ``followup_review`` mirrors the skill's `followup_review_recommended` signal;
    defaults True so the review runs under the default trigger = "recommended"."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + f"change for {story_key}\n")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        write_spec(sp, final_status, baseline)
        # NO set_sprint: the orchestrator is the single sprint-status writer
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "followup_review_recommended": followup_review,
            },
        )

    return effect


def wt_review_effect(project, story_key, clean: bool, patched: int = 0):
    """Follow-up review pass in a worktree — a bmad-dev-auto re-invocation on the
    done spec. ``clean=True`` converges; ``clean=False`` keeps recommending."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline)
        set_sprint(wt, story_key, "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect


def make_engine(project, script, policy=None, run_id="test-run", **kwargs):
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id=run_id, project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy or wt_policy(),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def journal_kinds(engine):
    return [e["kind"] for e in engine.journal.entries()]


# ----------------------------------------------------------------- happy path


def test_worktree_happy_path_merges_to_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    # the unit's work landed on the target branch (main, checked out in the repo)
    assert engine.state.target_branch == "main"
    assert rev_parse_head(project.project) != head_before
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # worktree cleaned up, branch deleted (delete_branch default), tree clean
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "worktree-opened" in kinds and "unit-merged" in kinds
    # a clean teardown degrades nothing (gh-139): no warning event is emitted
    assert "worktree-teardown-degraded" not in kinds


def test_worktree_run_dir_is_outside_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    opened = []
    orig = Journal.append

    def spy(self, kind, **kw):
        if kind == "worktree-opened":
            opened.append(kw["path"])
        return orig(self, kind, **kw)

    Journal.append = spy
    try:
        engine.run()
    finally:
        Journal.append = orig

    assert opened, "expected a worktree-opened event"
    wt = opened[0]
    # run state lives in the main repo, never inside the worktree
    assert str(engine.run_dir.resolve()).startswith(str(project.project.resolve()))
    assert not str(engine.run_dir.resolve()).startswith(str(wt))


def test_worktree_multiple_stories_serialize_onto_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
            wt_dev_effect(project, "1-2-b"),
            wt_review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 2
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "change for 1-2-b" in src
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


# ----------------------------------------------------------------- branch naming


def test_branch_per_story_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="story", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "bmad-loop/test-run/1-1-a"
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_branch_per_run_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="run", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "bmad-loop/test-run"
    assert branch_exists(project.project, "bmad-loop/test-run")


def test_dirty_unit_key_branch_is_created_by_real_git(project):
    """#102: a unit key carrying ref-illegal sequences reached `git branch` raw and
    blew up at worktree-mount time. `unit_branch_name` now ref-sanitizes both
    segments, so real git accepts the name — while the worktree dir (safe_segment)
    and the branch (safe_ref_segment) are each sanitized on their own alphabet."""
    from bmad_loop.workspace import open_unit_workspace, unit_branch_name

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    key = "story/1:2..3@{now}.lock"
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = open_unit_workspace(project.project, project, "test-run", key, "main", "story", run_dir)

    assert unit.branch == unit_branch_name("test-run", key, "story")
    assert unit.branch.startswith("bmad-loop/test-run/story_1_2__3_{now}.lock-")
    assert branch_exists(project.project, unit.branch)  # real git accepted the name
    assert unit.path.is_dir() and unit.path.name != key  # dir sanitized separately


# ----------------------------------------------------------------- merge strategies


def test_worktree_squash_merge_linear_history(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="squash"),
    )
    summary = engine.run()
    assert summary.done == 1
    assert git(project.project, "log", "--oneline", "--merges") == ""  # squash → linear


# ----------------------------------------------------------------- failure preservation


def _defer_script(project, key):
    """Dev succeeds, then review never converges → plateau defer. Consumers must
    pin ``limits=LimitsPolicy(max_followup_reviews=99)`` so the default damping cap
    (1) doesn't force-converge round 2 — this script tests the exhaustion/defer
    plateau, not damping."""
    return [wt_dev_effect(project, key)] + [
        wt_review_effect(project, key, clean=False, patched=1) for _ in range(3)
    ]


# damping pinned high so _defer_script's 3 non-clean rounds reach the exhaustion
# plateau instead of force-converging at the cap
_NO_DAMP = LimitsPolicy(max_followup_reviews=99)


def test_worktree_defer_keeps_failed_unit(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, _defer_script(project, "1-1-a"), policy=wt_policy(limits=_NO_DAMP)
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    # the failed unit's diff is preserved for forensics
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file()
    assert "change for 1-1-a" in patch.read_text()
    # keep_failed default → worktree + branch remain mounted for inspection
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    listed = [p.resolve() for p in worktree_list(project.project)]
    assert project.project.resolve() in listed and len(listed) == 2
    # the main repo is untouched by the failed unit
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    assert worktree_clean(project.project)


def test_worktree_defer_without_keep_drops_worktree_but_saves_patch(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        _defer_script(project, "1-1-a"),
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file() and "change for 1-1-a" in patch.read_text()
    # not kept → worktree removed, branch deleted
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_worktree_defer_then_next_story_succeeds(project):
    """A deferred (kept) unit must not block the next story's worktree/merge."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script, policy=wt_policy(limits=_NO_DAMP))
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 1
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()


def test_branch_per_run_kept_failure_detaches_so_next_unit_runs(project):
    """branch_per=run shares one branch; keeping a kept-failed unit's worktree
    checked out on it would block every later unit's mount and cascade the whole
    run into never-attempted deferrals. close_unit_workspace detaches the kept
    worktree's HEAD, freeing the shared branch so the next unit gets a genuine
    attempt instead of insta-deferring on a collision (issue #138)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script, policy=wt_policy(branch_per="run", limits=_NO_DAMP))
    summary = engine.run()

    # 1-1-a defers (kept), but 1-2-b actually runs and lands — no collision cascade
    assert summary.deferred == 1 and summary.done == 1 and not summary.paused
    assert "worktree-open-failed" not in journal_kinds(engine)
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE
    assert not engine.state.tasks["1-2-b"].defer_reason
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    # the kept 1-1-a worktree is detached (freeing the shared run branch), while
    # the branch ref itself survives for inspection
    assert branch_exists(project.project, "bmad-loop/test-run")
    kept = [p for p in worktree_list(project.project) if p.resolve() != project.project.resolve()]
    assert len(kept) == 1 and current_branch(kept[0]) == "HEAD"


def test_worktree_followup_damped_commits_and_integrates(project):
    """Damping fires the same in worktree isolation (default cap 1, no _isolated
    guard): a finalized unit whose review keeps recommending a follow-up converges
    after one honored round, the work MERGES into the main repo, and the refiled
    follow-up lands in the MAIN repo's ledger — not stranded inside the discarded
    unit worktree. Exempting isolation would leave isolated runs non-convergent AND
    deferred (strictly worse), which this locks out."""
    from bmad_loop import deferredwork

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a")] + [
        wt_review_effect(project, "1-1-a", clean=False) for _ in range(3)
    ]
    engine, _ = make_engine(project, script)  # default wt_policy() → cap 1
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2 and task.followup_reviews_spent == 1
    # the unit's work merged into the main repo (target branch checkout)
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = journal_kinds(engine)
    assert "review-followup-damped" in kinds and "unit-merged" in kinds
    assert "story-deferred" not in kinds
    # the refiled follow-up is in the MAIN repo ledger, integrated from the worktree
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1


# ----------------------------------------------------------------- configured target


def test_configured_target_branch_created_and_checked_out(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(target_branch="integration"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert engine.state.target_branch == "integration"
    assert current_branch(project.project) == "integration"
    assert branch_exists(project.project, "integration")
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_worktree_merge_conflict_escalates_and_keeps_branch(project):
    """A unit whose ff-only merge can't fast-forward (target diverged) escalates
    cleanly without an illegal DONE->ESCALATED transition, keeping its branch."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="ff"),
    )
    # diverge the target right after the worktree is cut so ff-only cannot apply
    import bmad_loop.engine as eng

    real_open = eng.open_unit_workspace

    def diverging_open(*a, **k):
        unit = real_open(*a, **k)
        (project.project / "diverge.txt").write_text("target moved\n")
        git(project.project, "add", "-A")
        git(project.project, "commit", "-q", "-m", "target diverges")
        return unit

    eng.open_unit_workspace = diverging_open
    try:
        summary = engine.run()
    finally:
        eng.open_unit_workspace = real_open

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the unit branch is kept for manual merge
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_branch_per_run_escalation_pauses_without_dispatching_next_unit(project):
    """Issue #138 scoping guard: the shared-branch collision cascade is a property
    of the DEFER path, which *returns* and lets the loop dispatch the next unit
    into the held branch. A merge-conflict escalation instead *pauses* the run
    (RunPaused), so under branch_per=run no sibling is ever dispatched while the
    kept worktree holds the shared branch — there is nothing to detach here, and
    on resume the re-armed unit's worktree is freed by the resume-restart discard
    (see test_worktree_crash_restart_discards_stale_worktree) before any mount."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="run", merge_strategy="ff"),
    )
    # diverge the target right after the (shared) worktree is cut so ff-only merge
    # of 1-1-a cannot fast-forward → escalate + pause
    import bmad_loop.engine as eng

    real_open = eng.open_unit_workspace

    def diverging_open(*a, **k):
        unit = real_open(*a, **k)
        if not (project.project / "diverge.txt").exists():
            (project.project / "diverge.txt").write_text("target moved\n")
            git(project.project, "add", "-A")
            git(project.project, "commit", "-q", "-m", "target diverges")
        return unit

    eng.open_unit_workspace = diverging_open
    try:
        summary = engine.run()
    finally:
        eng.open_unit_workspace = real_open

    assert summary.paused and summary.escalated == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    # the run halted at the escalation: 1-2-b was never dispatched, so the
    # shared-branch collision that cascades the DEFER path cannot arise here
    assert "1-2-b" not in engine.state.tasks
    assert "worktree-open-failed" not in journal_kinds(engine)


# ----------------------------------------------------------------- resume


def test_worktree_spec_approval_pause_resumes_in_same_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(
        gates=GatesPolicy(mode="per-story-spec-approval"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
    )
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    task = saved.tasks["1-1-a"]
    assert task.phase == Phase.DEV_VERIFY and task.worktree_path and task.branch
    # the worktree stays mounted across the pause so resume can review in it
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert len(worktree_list(project.project)) == 2

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([wt_review_effect(project, "1-1-a", clean=True)])
    resumed = Engine(
        paths=project,
        policy=gated,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary2 = resumed.run()

    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


def test_worktree_crash_restart_discards_stale_worktree(project):
    """A unit interrupted before the spec gate is restarted fresh: the stale
    worktree is discarded and a new one mounted, not stacked on top."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")])
    # simulate an interrupted unit left mid-flight (DEV_RUNNING, worktree mounted)
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1)
    engine.state.tasks["1-1-a"] = task
    task.phase = Phase.DEV_RUNNING
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    engine._save()

    # resume with a full dev+review script → restart should succeed
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_worktree_resume_committing_finishes_and_merges(project):
    """#115, isolated flavor: a unit persisted at COMMITTING (gate+advance save
    landed, DONE save did not) is finished inside its still-mounted worktree
    and merged back — not discarded as a stale worktree by resume-restart."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    # the attempt committed its work inside the unit (only the work file —
    # the sprint board is the orchestrator's dev-time write, still uncommitted)
    src = unit.path / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(unit.path, "add", "src.txt")
    git(unit.path, "commit", "-q", "-m", "attempt work for 1-1-a")
    wt = project.rebased(unit.path)
    sp = wt.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "done", unit.baseline)
    set_sprint(wt, "1-1-a", "done")

    task = StoryTask("1-1-a", 1, phase=Phase.COMMITTING, attempt=1)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    task.spec_file = str(sp)
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": unit.baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks["1-1-a"] = task
    engine._save()

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    assert adapter.sessions == []  # commit finished from persisted state alone
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)
    kinds = journal_kinds(resumed)
    assert "resume-commit" in kinds and "unit-merged" in kinds
    assert "resume-restart" not in kinds


# ----------------------------------------------------------------- regression guard


def test_isolation_none_leaves_no_worktrees(project):
    """The default (isolation=none) path must not create branches/worktrees."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=Policy(gates=GatesPolicy(mode="none"), notify=QUIET),  # isolation defaults to none
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.target_branch == ""  # never resolved in none mode
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert "worktree-opened" not in journal_kinds(engine)


def test_worktree_run_carries_bmad_surface_and_never_commits_it(project):
    """End-to-end: an UNTRACKED `_bmad/` in the main repo (what a project that
    gitignores it has — and a worktree checks out tracked files only, so the
    checkout has none) reaches the worktree the session actually runs in. The
    renderer is handed that worktree as its project root and hard-fails when the
    root has no `_bmad/`.

    Deliberately not gitignored: that keeps the second half load-bearing. The
    worktree's local git exclude is then the ONLY thing stopping the seed — and the
    render output the session writes on top of it — from being swept into the story
    commit by the unit's `git add -A`."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    bmad = project.project / "_bmad"
    (bmad / "scripts").mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (bmad / "scripts" / "config_utils.py").write_text("# config", encoding="utf-8")

    head_before = rev_parse_head(project.project)
    seen: list[list[str]] = []
    dev = wt_dev_effect(project, "1-1-a")

    def dev_and_probe(spec):
        wt = spec.cwd
        seen.append(
            [
                rel
                for rel in ("config.toml", "scripts/render_skill.py", "scripts/config_utils.py")
                if (wt / "_bmad" / rel).is_file()
            ]
        )
        # what the renderer writes mid-session, AFTER provisioning shielded the dir
        out = wt / "_bmad" / "render" / "bmad-build-auto" / "sandbox-abc" / "gen"
        out.mkdir(parents=True)
        (out / "workflow.md").write_text("rendered", encoding="utf-8")
        return dev(spec)

    engine, _ = make_engine(
        project, [dev_and_probe, wt_review_effect(project, "1-1-a", clean=True)]
    )
    summary = engine.run()

    assert summary.done == 1
    assert seen == [["config.toml", "scripts/render_skill.py", "scripts/config_utils.py"]]
    # every commit the run landed carries the code change and no _bmad/ path at all
    files = git(
        project.project, "log", "--pretty=format:", "--name-only", f"{head_before}..HEAD"
    ).splitlines()
    assert "src.txt" in files
    assert not [f for f in files if f.startswith("_bmad/")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_incomplete_bmad_scripts_seed_pauses_before_dispatch(project, tmp_path):
    """A shared BMad install wired as a symlink out of the repo: the seed's
    resolve-and-contain guard drops every file under `_bmad/scripts/`, so the
    worktree gets `config.toml` and no renderer at all.

    That is an ENVIRONMENT fault, not this story's: the seed reads the same repo for
    every unit, so dispatching would walk the whole backlog into result-less Stops
    one story at a time. The run pauses before `drive` is ever entered, and the
    half-seeded worktree stays mounted for inspection.

    The #2601 renderer stub is installed on purpose: the gate is conditional on the
    resolved primitive actually rendering, so without a stub on disk this environment
    costs the run nothing and must NOT pause — see the inline sibling below."""
    from conftest import attach_profile, install_build_auto_skill

    # before commit_sprint, so the tree is tracked and the worktree checks it out
    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=True)
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    shared = tmp_path / "shared-bmad-scripts"
    shared.mkdir()
    (shared / "render_skill.py").write_text("# render", encoding="utf-8")
    (shared / "config_utils.py").write_text("# config", encoding="utf-8")
    bmad = project.project / "_bmad"
    bmad.mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts").symlink_to(shared, target_is_directory=True)
    # the repo-side probe still sees the renderer through the symlink
    assert (bmad / "scripts" / "render_skill.py").is_file()

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree with no renderer")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)  # one call arms dev AND review: they share the adapter
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the sibling never ran: a pause stops the loop, it does not walk the backlog
    # one result-less Stop at a time (tasks are created on dispatch, so an absent
    # key IS the proof the loop stopped)
    assert "1-1-b" not in engine.state.tasks
    entries = Journal(engine.run_dir).entries()
    kinds = [e["kind"] for e in entries]
    assert "story-escalated" in kinds
    # the reason names the renderer, which is the evidence the gate now actually has
    escalated = [e for e in entries if e["kind"] == "story-escalated"]
    assert "render_skill.py" in escalated[0]["reason"]
    # the seed's own report still went out, so the escalation reads as an
    # ESCALATION of that report rather than replacing it
    assert "worktree-seed-skipped" in kinds
    # the worktree the operator has to inspect is still mounted, and it is the
    # renderer half that is short — config.toml made it in, so "seeding ran"
    assert (Path(task.worktree_path) / "_bmad" / "config.toml").is_file()
    assert not (Path(task.worktree_path) / "_bmad" / "scripts" / "render_skill.py").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_short_scripts_seed_does_not_pause_an_inline_primitive(project, tmp_path):
    """The ERA discriminator for the gate above. Same symlinked `_bmad/scripts/`,
    same dropped files, same `worktree-seed-skipped` report — the single delta is
    that the resolved dev primitive is a pre-#2601 INLINE SKILL.md.

    Nothing in such a project ever shells out to `render_skill.py`, so a short
    `_bmad/scripts/` seed costs the run nothing and the whole backlog must run to
    completion. `_bmad_scripts_seed_incomplete` cannot tell the two apart — it only
    ever sees the REPO, which carries a renderer-era `_bmad/scripts/` in both — so
    this is the leg that pins the discrimination onto the engine's gate.

    Fails three independent ways on the un-gated build (`if BMAD_SCRIPTS_SEED_REL in
    skipped_seeds` alone): the run pauses, story 1 escalates, and story 2 is never
    dispatched."""
    from conftest import attach_profile, install_build_auto_skill

    # the ONLY delta from the sibling above (which installs the same skill with
    # renderer_stub=True): an inline SKILL.md, no render_skill.py reference in it
    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=False)
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    shared = tmp_path / "shared-bmad-scripts"
    shared.mkdir()
    (shared / "render_skill.py").write_text("# render", encoding="utf-8")
    (shared / "config_utils.py").write_text("# config", encoding="utf-8")
    bmad = project.project / "_bmad"
    bmad.mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts").symlink_to(shared, target_is_directory=True)
    # the repo-side half of the trigger is armed exactly as in the sibling: the
    # probe sees the renderer through the symlink, and the seed cannot follow it
    assert (bmad / "scripts" / "render_skill.py").is_file()
    assert not renderer_stub_resolved(project.project, [".claude/skills"])

    # what the worktree the session is actually handed looked like — the run
    # completing is only meaningful if it completed over a SHORT seed
    seen: list[tuple[bool, bool]] = []
    dev_a = wt_dev_effect(project, "1-1-a")

    def dev_and_probe(spec):
        wt_bmad = spec.cwd / "_bmad"
        seen.append(
            (
                (wt_bmad / "config.toml").is_file(),
                (wt_bmad / "scripts" / "render_skill.py").is_file(),
            )
        )
        return dev_a(spec)

    engine, adapter = make_engine(
        project,
        [
            dev_and_probe,
            wt_review_effect(project, "1-1-a", clean=True),
            wt_dev_effect(project, "1-1-b"),
            wt_review_effect(project, "1-1-b", clean=True),
        ],
    )
    attach_profile(adapter)  # one call arms dev AND review: they share the adapter
    summary = engine.run()

    # not just story 1: the whole backlog, which is what the escalation would cost
    assert summary.done == 2 and not summary.paused
    entries = Journal(engine.run_dir).entries()
    kinds = [e["kind"] for e in entries]
    assert "story-escalated" not in kinds
    # ...and the report itself is byte-identical to the escalating sibling's.
    # provision_worktree stayed a pure reporter, so the sentinel really did ride
    # the channel — the engine's gate is what declined to act on it, which is the
    # only way this can be a discrimination rather than a silenced signal.
    skipped = [e for e in entries if e["kind"] == "worktree-seed-skipped"]
    assert skipped and any(BMAD_SCRIPTS_SEED_REL in e["entries"] for e in skipped)
    # seeding ran (config.toml made it in) and the renderer half really was short,
    # so the run completed over exactly the environment the sibling pauses on
    assert seen == [(True, False)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_stub_in_the_review_tree_alone_still_pauses(project, tmp_path):
    """A run can mix skill trees (dev=claude → .claude/skills, review=gemini →
    .agents/skills) and the two can sit on different upstream eras. The gate asks
    ANY over both, because either session kind reaching a renderer that is not
    there is one result-less Stop per story just the same.

    Here the DEV tree is inline and only the REVIEW tree is a #2601 stub. It is the
    one ablation that catches implementing the gate against `self._dev_skill()` or
    the dev adapter's tree alone: every other test in this family passes under that
    bug, because in every other one the dev tree answers for both."""
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=False)
    install_build_auto_skill(project.project, ".agents/skills", renderer_stub=True)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    shared = tmp_path / "shared-bmad-scripts"
    shared.mkdir()
    (shared / "render_skill.py").write_text("# render", encoding="utf-8")
    (shared / "config_utils.py").write_text("# config", encoding="utf-8")
    bmad = project.project / "_bmad"
    bmad.mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts").symlink_to(shared, target_is_directory=True)
    # the premise, asserted rather than assumed: two trees, two eras. Without this
    # the test would still pass with BOTH trees stubbed, proving nothing per-role.
    assert not renderer_stub_resolved(project.project, [".claude/skills"])
    assert renderer_stub_resolved(project.project, [".agents/skills"])

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree with no renderer")

    engine, dev = make_engine(
        project, [never], review_adapter=attach_profile(MockAdapter([never]), "gemini")
    )
    attach_profile(dev, "claude")
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "story-escalated" in journal_kinds(engine)


def test_a_benign_skipped_seed_does_not_pause(project):
    """The clearing leg for the gate above, and it has to arm a NON-EMPTY
    `skipped_seeds` to earn its keep: a `worktree_seed` dir the checkout already
    carries is reported skipped, which is the ordinary informational case that has
    always ridden this channel. Escalating on it would pause every run that has a
    no-op seed entry.

    Ablation this catches and a bare happy-path cannot: widening the gate from the
    `_bmad/scripts` sentinel to `if skipped_seeds:`."""
    bmad = project.project / "_bmad"
    (bmad / "scripts").mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (bmad / "scripts" / "config_utils.py").write_text("# config", encoding="utf-8")
    # tracked, so the worktree checks it out and copy-when-absent makes the seed a
    # no-op — provision_worktree reports it, and it must stay informational
    vendor = project.project / "vendor"
    vendor.mkdir()
    (vendor / "conf.txt").write_text("x\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(worktree_seed=("vendor",)),
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    kinds = journal_kinds(engine)
    assert "worktree-seed-skipped" in kinds  # the channel really did fire
    assert "story-escalated" not in kinds


def test_harvest_reverted_on_retry_under_isolation(project):
    """#405, workspace-scoped: the pre-harvest snapshot and its restore read the
    UNIT WORKTREE's ledger — the same tree `_rollback_or_pause` resets — so a
    harvested finding about work the rollback discarded never reaches the unit's
    merge. Isolation is not assumed to follow from the in-place case: the ledger
    path comes from `workspace.paths`, which differs between them."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    finding = {
        "summary": "Retry loop has no ceiling",
        "evidence": "the backoff doubles forever: no cap",
        "location": "src/retry.py:88",
        "severity": "medium",
    }

    def deferring_liar(spec):
        # completes and finalizes (the harvest fires) but claims a foreign
        # baseline — the non-fixable retry that routes through _rollback_or_pause
        cwd = spec.cwd
        wt = project.rebased(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + "bad attempt\n")
        sp = wt.implementation_artifacts / "spec-1-1-a.md"
        write_spec(sp, "done", "0" * 40, deferred=[finding])
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": "0" * 40,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )

    seen: list[bool] = []

    def probing_dev(spec):
        # attempt 2 opens on the reverted worktree: the harvest's ledger is gone
        seen.append(project.rebased(spec.cwd).deferred_work.exists())
        return wt_dev_effect(project, "1-1-a", followup_review=False)(spec)

    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        [deferring_liar, probing_dev],
        policy=wt_policy(limits=LimitsPolicy(max_dev_attempts=2)),
    )
    summary = engine.run()

    assert summary.done == 1
    kinds = journal_kinds(engine)
    assert "rollback-auto" in kinds
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 1 and harvests[0]["dw_ids"] == ["DW-1"]
    assert seen == [False]
    files = git(
        project.project, "log", "--pretty=format:", "--name-only", f"{head_before}..HEAD"
    ).split()
    assert "src.txt" in files
    assert not [f for f in files if f.endswith("deferred-work.md")]
    assert not project.deferred_work.exists()


# ----------------------------------------------------------------- new guards (review hardening)


def test_detached_head_pauses_instead_of_landing_on_unreferenced_commit(project):
    """isolation=worktree with no configured target on a detached HEAD has no
    branch to merge into; the run must pause rather than commit onto a nameless
    detached HEAD that the next checkout would orphan."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "checkout", "--detach")
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()
    assert summary.paused
    assert "detached HEAD" in (engine.state.paused_reason or "")
    # nothing was isolated into a worktree
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_commit_message_template_applied(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(commit_message_template="feat({story_key}): via {run_id}"),
    )
    summary = engine.run()
    assert summary.done == 1
    # the story's commit message (not the merge commit) used the template
    log = git(project.project, "log", "--format=%s")
    assert "feat(1-1-a): via test-run" in log
    assert "implemented" not in log  # built-in default was not used


# ------------------------------------------------ per_worktree engine plugin


def _write_stub_plugin(project, name, *, ready=_OK, setup=_OK, teardown=_OK, seed_globs=None):
    """A project-local *declarative* plugin whose lifecycle hooks are shell stubs
    (no real Unity) — proving a generic data-only plugin can gate the engine's
    per_worktree flow. A blocking hook's non-zero exit vetoes (defers) the unit.
    Commands are TOML literal strings, so they may embed double quotes but not
    single quotes. No [python], so it loads on folder-drop (no [plugins] enabled)."""
    plug_dir = project.project / ".bmad-loop" / "plugins" / name
    plug_dir.mkdir(parents=True)
    lines = ["[plugin]", f'name = "{name}"', "api_version = 1"]
    if seed_globs:
        globs = ", ".join(f'"{g}"' for g in seed_globs)
        lines.append(f"seed_globs = [{globs}]")
    lines += [
        "[hooks.pre_worktree_setup]",
        f"cmd = '{setup}'",
        "blocking = true",
        "[hooks.pre_ready_gate]",
        f"cmd = '{ready}'",
        "blocking = true",
        "[hooks.pre_worktree_teardown]",
        f"cmd = '{teardown}'",
    ]
    (plug_dir / "plugin.toml").write_text("\n".join(lines) + "\n")


def _pw_policy(**gates):
    return Policy(
        gates=GatesPolicy(mode=gates.get("mode", "none")),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
    )


def _hook_stages(engine):
    """The stages of every plugin-hook the bus journaled, in order."""
    return [e.get("stage") for e in engine.journal.entries() if e["kind"] == "plugin-hook"]


def test_per_worktree_setup_then_gate_then_teardown_and_seed(project):
    """Happy path: the worktree is seeded, the setup hook runs, the ready gate
    waits (and only passes because setup ran first), the agent runs, teardown
    fires. Ordering is proven by the gate depending on a setup marker."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # a gitignored MCP skill dir present in the main repo (untracked) to be seeded
    skill = project.project / ".claude" / "skills" / "gameobject-create"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("tool", encoding="utf-8")
    # setup asserts the seed reached its cwd (the worktree) before marking ready;
    # the gate fails unless that marker exists -> proves seed+setup precede the gate.
    _write_stub_plugin(
        project,
        "stub",
        setup=_seeded_then_touch(".claude/skills/gameobject-create/SKILL.md", "setup-done"),
        ready=_exists_run("setup-done"),
        teardown=_touch_run("teardown-done"),
        seed_globs=[".claude/skills/*"],
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.done == 1
    assert (engine.run_dir / "setup-done").is_file()
    assert (engine.run_dir / "teardown-done").is_file()
    # setup gated the ready gate gated teardown, in order, all via the bus
    stages = _hook_stages(engine)
    assert "pre_worktree_setup" in stages
    assert stages.index("pre_worktree_setup") < stages.index("pre_ready_gate")
    assert stages.index("pre_ready_gate") < stages.index("pre_worktree_teardown")
    # the dev + review sessions actually ran (gate let them through)
    assert [s.role for s in adapter.sessions] == ["dev", "review"]


def test_per_worktree_setup_failure_defers_and_skips_session(project):
    """A setup failure (Editor wouldn't launch) vetoes -> defers the unit, never
    starts a session, still tears down best-effort, and closes the (empty) worktree."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        setup="exit 3",
        teardown=_touch_run("teardown-done"),
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "pre_worktree_setup" in task.defer_reason  # the setup-stage veto deferred it
    assert adapter.sessions == []  # gate/setup ran before any dev session
    kinds = journal_kinds(engine)
    assert "plugin-veto" in kinds and "story-deferred" in kinds
    # the ready gate never ran (setup vetoed first)
    assert "pre_ready_gate" not in _hook_stages(engine)
    # teardown still ran; the deferred unit's worktree is kept (keep_failed default)
    # for inspection, exactly like any other deferral.
    assert (engine.run_dir / "teardown-done").is_file()
    assert len(worktree_list(project.project)) == 2


def test_per_worktree_ready_gate_failure_defers(project):
    """Setup succeeds but the Editor never reports ready -> defer + teardown."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        ready="exit 1",
        teardown=_touch_run("teardown-done"),
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "pre_ready_gate" in task.defer_reason  # the ready-stage veto deferred it
    assert adapter.sessions == []
    stages = _hook_stages(engine)
    assert "pre_worktree_setup" in stages and "pre_ready_gate" in stages
    assert (engine.run_dir / "teardown-done").is_file()


def test_per_worktree_teardown_runs_on_pause(project):
    """A spec-approval pause leaves the worktree mounted, but the teardown hook is
    still fired (teardown runs in the finally, even as RunPaused unwinds)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        teardown=_touch_run("teardown-done"),
    )
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy(mode="per-story-spec-approval"),
    )
    summary = engine.run()

    assert summary.paused
    # the worktree stays up for resume, but teardown fired
    assert len(worktree_list(project.project)) == 2
    assert (engine.run_dir / "teardown-done").is_file()
    assert "pre_worktree_teardown" in _hook_stages(engine)


def _leaking_dev_effect(project, story_key, *, leak_name, in_branch_set):
    """A dev effect that does the normal worktree work AND simulates a per_worktree
    Unity Editor leaking an asset write into the *main* checkout before merge.
    When in_branch_set the branch also commits `leak_name` (so the leaked main-tree
    copy collides with an incoming file — the recoverable case); otherwise the leak
    is stray work the merge does not introduce."""
    base = wt_dev_effect(project, story_key)

    def effect(spec):
        if in_branch_set:
            (spec.cwd / leak_name).write_text(f"branch content for {story_key}\n")
        result = base(spec)
        # the competing main-repo Editor writes the asset into the main checkout
        (project.project / leak_name).write_text("editor leaked\n")
        return result

    return effect


def test_merge_auto_recovers_editor_dirtied_target(project):
    """A unit whose own incoming file was leaked (untracked) into the main checkout
    by a per_worktree Editor merges successfully after auto-clean, journaling
    merge-target-cleaned."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _leaking_dev_effect(project, "1-1-a", leak_name="Leak.cs", in_branch_set=True),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # the branch's version of the leaked file landed on target
    assert (project.project / "Leak.cs").read_text() == "branch content for 1-1-a\n"
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "merge-target-cleaned" in kinds and "unit-merged" in kinds
    cleaned = next(e for e in engine.journal.entries() if e["kind"] == "merge-target-cleaned")
    assert cleaned["paths"] == ["Leak.cs"]


def test_merge_stray_dirt_escalates_with_clear_message(project):
    """Dirt in the main checkout that is NOT part of the branch's incoming files
    (possible real operator work) is never cleaned: the unit escalates with the
    Editor-leak message and keeps its branch."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _leaking_dev_effect(project, "1-1-a", leak_name="stray.txt", in_branch_set=False),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    assert "not part of this branch" in reason and "stray.txt" in reason
    # branch kept for manual merge; the stray file was left untouched
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert (project.project / "stray.txt").read_text() == "editor leaked\n"
    assert "merge-target-cleaned" not in journal_kinds(engine)


def test_spec_file_serialized_relative_to_worktree():
    """A worktree task persists spec_file relative to its worktree so a kept run's
    state stays portable (no dangling absolute path into a pruned worktree)."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/.bmad-loop/runs/run/worktrees/1-1-a"
    task.spec_file = "/repo/.bmad-loop/runs/run/worktrees/1-1-a/_out/spec.md"
    assert task.to_dict()["spec_file"] == "_out/spec.md"
    # a spec living outside the worktree stays absolute
    task.spec_file = "/elsewhere/spec.md"
    assert task.to_dict()["spec_file"] == "/elsewhere/spec.md"
    # in-place mode (no worktree) is unchanged
    task.worktree_path = ""
    task.spec_file = "/repo/_out/spec.md"
    assert task.to_dict()["spec_file"] == "/repo/_out/spec.md"


# ---------------------------------------------- gh-139 resilient teardown


def _open_unit(project, key="1-1-a", branch_per="story"):
    """Mount a real unit worktree (commits the sprint board first, like every
    direct-open test) and return (unit, run_dir)."""
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {key: "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = open_unit_workspace(
        project.project, project, "test-run", key, "main", branch_per, run_dir
    )
    return unit, run_dir


def _drop_admin_entry(project):
    """Delete git's worktree admin dir under the main repo, reproducing the
    gh-139 post-ENOTEMPTY state where both `git worktree remove` calls fail with
    'is not a working tree'. Exactly one linked worktree is open at call time."""
    admin = list((project.project / ".git" / "worktrees").iterdir())
    assert len(admin) == 1
    shutil.rmtree(admin[0])


def test_close_after_admin_entry_dropped_degrades_not_crashes(project):
    """gh-139 fingerprint: a process the just-ended session left running keeps
    `git worktree remove` from clearing the tree (ENOTEMPTY), and by then git has
    already dropped its admin entry — so the force=True retry fails with 'is not a
    working tree' and the second GitError used to crash the whole run after the
    merge already landed. Teardown now degrades: rmtree+prune reclaim the dir, the
    branch is still deleted, and the failure is reported, not raised."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )

    assert not unit.path.exists()  # rmtree reclaimed the stuck dir
    assert not branch_exists(project.project, unit.branch)  # prune freed it → deleted
    assert len(reports) == 1 and "is not a working tree" in reports[0]


def test_close_degrades_when_branch_delete_fails(project, monkeypatch):
    """The branch-delete tail is the second crash door: a `delete_branch` GitError
    is degraded to a report, not raised, so a merged unit's run still completes."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)

    def boom(*a, **k):
        raise verify.GitError("branch is checked out elsewhere")

    monkeypatch.setattr(verify, "delete_branch", boom)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()  # the worktree itself removed cleanly
    assert len(reports) == 1 and "branch delete failed" in reports[0]


def test_close_dirty_tree_force_retry_is_not_degraded(project):
    """A stray untracked file makes the plain `git worktree remove` refuse; the
    force=True retry clears it. That is the ordinary dirty-tree case, not a
    degradation — no report is emitted and behavior matches today."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    (unit.path / "stray.txt").write_text("dirty\n")  # untracked → plain remove refuses

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()  # force retry handled the dirty tree
    assert not branch_exists(project.project, unit.branch)
    assert reports == []  # NOT a degradation


def test_close_deferred_without_keep_degrades(project, monkeypatch):
    """The DEFERRED, no-keep teardown (success=False) runs the same fallback chain:
    the patch is already captured, so a dropped admin entry degrades to a report
    while the worktree is reclaimed via rmtree+prune — the run continues. (In the
    real gh-139 sequence capture runs before the remove drops the admin entry;
    dropping it up front here would break capture too and flip the close into the
    capture-failure preserve path, so pin capture to its real-life outcome.)"""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)
    monkeypatch.setattr(verify, "capture_diff", lambda *a, **k: "")

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()
    assert not branch_exists(project.project, unit.branch)
    assert len(reports) == 1 and "is not a working tree" in reports[0]


def test_close_capture_failure_preserves_worktree_and_branch(project, monkeypatch):
    """The teardown tail's premise is that a dropped unit's changes are already
    patch-captured — a failed capture (e.g. a #156 git timeout) breaks it, and
    tearing down anyway would destroy the only copy of the unit's work. The
    close must instead preserve the worktree + branch (as if keep_failed) and
    report the degradation."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)

    def boom(*a, **k):
        raise verify.GitError("git diff timed out")

    monkeypatch.setattr(verify, "capture_diff", boom)

    reports: list[str] = []
    patch = close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )

    assert patch is None
    assert unit.path.exists()  # preserved: the worktree holds the only copy
    assert branch_exists(project.project, unit.branch)
    assert len(reports) == 1 and "diff capture failed" in reports[0]


def test_close_capture_failure_frees_shared_branch(project, monkeypatch):
    """branch_per=run: a worktree preserved by a failed capture holds the shared
    run branch, which would collide with every later unit's mount (gh-138). The
    detach_kept handling must apply to this preserve path exactly as it does to
    keep_failed: HEAD detaches, so the branch is mountable elsewhere."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project, branch_per="run")

    def boom(*a, **k):
        raise verify.GitError("git diff timed out")

    monkeypatch.setattr(verify, "capture_diff", boom)

    close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        detach_kept=True,
        on_teardown_degraded=lambda _msg: None,
    )

    assert unit.path.exists()  # preserved for recovery
    # the shared branch is free again: a sibling worktree can mount it
    sibling = run_dir / "worktrees" / "1-1-b"
    verify.worktree_add(project.project, sibling, unit.branch, create=False)


def test_close_notes_leftover_path_when_rmtree_loses_race(project, monkeypatch):
    """If the writing process recreates files faster than rmtree(ignore_errors)
    can clear them, the dir survives the fallback. The degraded report then names
    the leftover path — the dir lives under the gitignored run dir and is reclaimed
    later by trim_run_dir / clean, so the run still continues."""
    from bmad_loop import workspace
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)  # force both worktree_remove calls to fail
    # rmtree loses the race: the dir survives the fallback (deterministic no-op)
    monkeypatch.setattr(workspace.shutil, "rmtree", lambda *a, **k: None)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert unit.path.exists()  # the no-op rmtree left it in place
    assert len(reports) == 1
    assert str(unit.path) in reports[0] and "still present" in reports[0]


def test_close_double_degradation_reports_both(project, monkeypatch):
    """Both teardown doors can fail in one close: a dropped admin entry degrades
    the worktree removal AND a raising delete_branch degrades the branch deletion.
    Both are reported, in order (worktree first, branch second), no raise."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    def boom(*a, **k):
        raise verify.GitError("branch is checked out elsewhere")

    monkeypatch.setattr(verify, "delete_branch", boom)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert len(reports) == 2
    assert "fell back to rmtree+prune" in reports[0]  # worktree-remove degradation first
    assert "branch delete failed" in reports[1]  # branch-delete degradation second


def test_discard_worktree_falls_back_to_rmtree_and_prunes(project):
    """Resume-restart discard: if `git worktree remove` can't clear a stale unit
    worktree (gh-139-style dropped admin entry), fall back to rmtree + prune so the
    same path is free to re-mount on resume, without raising."""
    from bmad_loop.workspace import discard_worktree

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    discard_worktree(project.project, str(unit.path), unit.branch, run_dir=run_dir)  # no raise

    assert not unit.path.exists()  # rmtree reclaimed the stuck dir
    assert not branch_exists(project.project, unit.branch)  # pruned → deletable


def test_discard_refuses_rmtree_outside_run_worktrees_dir(project, tmp_path):
    """`task.worktree_path` arrives from persisted state (state.json), which can
    be corrupt or hand-edited. git itself refuses to remove a dir that is not a
    worktree — but that very refusal used to hand the path to the rmtree
    fallback, which validates nothing. The fallback must decline any path that
    does not resolve under this run's worktrees dir."""
    from bmad_loop.workspace import discard_worktree

    victim = tmp_path / "not-a-worktree"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n")
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"

    discard_worktree(project.project, str(victim), "", run_dir=run_dir)  # no raise

    assert (victim / "precious.txt").exists()  # confinement guard refused the rmtree


def test_close_refuses_rmtree_outside_run_worktrees_dir(project, tmp_path):
    """close_unit_workspace's rmtree fallback can receive a persisted path too
    (_reopen_unit rebuilds the UnitWorkspace from task.worktree_path on resume).
    A path outside the run's worktrees dir is never rmtree'd, and the degraded
    report says the fallback was refused."""
    from bmad_loop.workspace import UnitWorkspace, Workspace, close_unit_workspace

    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n")
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = UnitWorkspace(
        workspace=Workspace(root=victim, paths=project.rebased(victim)),
        repo_root=project.project,
        branch="bmad-loop/test-run/1-1-a",
        path=victim,
        baseline="",
    )

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        delete_branch=False,
        on_teardown_degraded=reports.append,
    )

    assert (victim / "precious.txt").exists()  # confinement guard refused the rmtree
    assert len(reports) == 1 and "rmtree refused" in reports[0]


def test_engine_run_completes_when_worktree_remove_always_fails(project, monkeypatch):
    """gh-139 end-to-end: with `git worktree remove` failing on every call, a
    worktree-isolation run still merges the unit to the target and reaches
    run-complete — teardown degrades to a journaled warning instead of crashing
    the run after the work already landed."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)

    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    # the merge still landed on the target branch (main, checked out in the repo)
    assert rev_parse_head(project.project) != head_before
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # admin entry is INTACT here (only the remove call fails), so prune's
    # branch-freeing is load-bearing: after rmtree+prune the branch is deletable
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds and "run-complete" in kinds
    assert "worktree-teardown-degraded" in kinds


def test_engine_deferred_teardown_degrades_are_journaled(project, monkeypatch):
    """The DEFERRED (no-keep) close site must wire on_teardown_degraded too: with
    `git worktree remove` always failing, the deferral still finishes and its
    teardown degradation is journaled — so dropping the kwarg from the deferral
    call site would be caught here (only the success path is asserted E2E above)."""

    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    engine, _ = make_engine(
        project,
        _defer_script(project, "1-1-a"),
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    assert "worktree-teardown-degraded" in journal_kinds(engine)


def test_resume_remount_survives_discard_remove_failure(project, monkeypatch):
    """The discard fallback's prune is load-bearing: if `git worktree remove` can't
    clear a stale unit worktree on resume-restart (admin entry INTACT — the dir is
    stuck, not the entry), rmtree drops the dir but only the prune clears git's
    admin entry so `git worktree add` can re-mount at the same path. Without the
    prune the re-mount would collide and the unit would defer instead of finishing."""
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")])
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1)
    engine.state.tasks["1-1-a"] = task
    task.phase = Phase.DEV_RUNNING
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    engine._save()

    # `git worktree remove` always fails, admin entry left intact → only
    # worktree_prune can free the path for the resume re-mount
    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1  # re-mounted at the same path and finished
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert "worktree-open-failed" not in journal_kinds(resumed)


def test_spec_file_serialized_with_posix_separators():
    """The relative spec_file is persisted with forward slashes (as_posix) so a
    state.json written under one OS reads back identically under another — no
    backslashes leak into the cross-OS state contract."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/wt"
    task.spec_file = "/repo/wt/_out/sub/spec.md"
    serialized = task.to_dict()["spec_file"]
    assert serialized == "_out/sub/spec.md"
    assert "\\" not in serialized


# ------------------------------------------------- retry recovery (issue #161)


def test_dev_retry_in_worktree_auto_recovers_instead_of_pausing(project):
    """#161: a mid-drive dev retry inside a unit worktree must auto-recover the
    disposable worktree (parking the attempt's commits on a preserve ref) even
    with rollback_on_failure OFF — never pause with in-place manual-recovery
    instructions aimed at the operator's checkout, which the attempt never
    touched. rollback_on_failure gates isolation="none" recovery only."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    def bad_dev(spec):
        # commits work in the worktree, then claims a foreign baseline — the
        # non-fixable verify retry that routes through _rollback_or_pause
        cwd = spec.cwd
        wt = project.rebased(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + "bad attempt\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", "bad attempt work")
        sp = wt.implementation_artifacts / "spec-1-1-a.md"
        write_spec(sp, "in-review", "0" * 40)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "escalations": [],
            },
        )

    engine, _ = make_engine(
        project,
        [
            bad_dev,
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
        policy=wt_policy(rollback_on_failure=False),
    )
    summary = engine.run()

    assert not summary.paused  # the old behavior: paused for manual recovery
    assert summary.done == 1
    kinds = journal_kinds(engine)
    assert "rollback-manual-required" not in kinds
    assert "rollback-auto" in kinds
    # the bad attempt's commits were parked on a recovery ref, not lost
    preserved = [e for e in engine.journal.entries() if e["kind"] == "attempt-commits-preserved"]
    assert preserved and preserved[0]["ref"].startswith("attempt-preserve/")
    # only the successful attempt's work merged to the target branch
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "bad attempt" not in src
    assert worktree_clean(project.project)
