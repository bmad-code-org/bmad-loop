"""Phase 3: isolation="worktree" — each unit runs in its own git worktree and
merges back into the target branch locally. Sessions run inside the worktree
(spec.cwd), so the effects here write artifacts rebased onto that checkout.

Exercised end-to-end against the conftest `project` sandbox with the mock
adapter (no tmux, no LLM).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import (
    _OK,
    _exists_run,
    _file_exists_cmd,
    _seeded_then_touch,
    _spec_baseline,
    _touch_run,
    crash_at_merge_back,
    git,
    mark_ledger_done,
    set_sprint,
    write_spec,
    write_sprint,
)

from bmad_loop import verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.install import BMAD_SCRIPTS_SEED_REL, CENTRAL_CONFIG_REL, renderer_stub_resolved
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import (
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ScmPolicy,
    VerifyPolicy,
)
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


def wt_dev_effect(project, story_key, *, final_status="done", followup_review=True, deferred=None):
    """Dev session running inside the unit worktree (spec.cwd). Mirrors the
    bmad-dev-auto skill: self-finalizes the spec to done, never writes the sprint
    board (the orchestrator advances it via the B2 seam, inside the worktree).
    ``followup_review`` mirrors the skill's `followup_review_recommended` signal;
    defaults True so the review runs under the default trigger = "recommended".
    ``deferred`` is the post-#2640 frontmatter `deferred:` list the harvest reads;
    ``None`` omits the field, which is the common shape."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + f"change for {story_key}\n")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        write_spec(sp, final_status, baseline, deferred=deferred)
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


def wt_review_effect(project, story_key, clean: bool, patched: int = 0, deferred=None):
    """Follow-up review pass in a worktree — a bmad-dev-auto re-invocation on the
    done spec. ``clean=True`` converges; ``clean=False`` keeps recommending.
    ``deferred`` is the spec's `deferred:` list as this pass leaves it — the skill
    accumulates into it, so a pass that defers its own finding rewrites the list
    with the dev leg's items still in place; ``None`` omits the field."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline, deferred=deferred)
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
    # the DONE leg's ledger carry is a no-op on a story that harvested nothing —
    # it returns on the empty record before it can journal anything (#405)
    assert "harvest-carried" not in kinds


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
    # …and names the leg that actually fired, not the whole sentinel tuple: the
    # config landed, so sending the operator to look for it would be a lie
    assert BMAD_SCRIPTS_SEED_REL in escalated[0]["reason"]
    assert CENTRAL_CONFIG_REL not in escalated[0]["reason"]
    # the seed's own report still went out, so the escalation reads as an
    # ESCALATION of that report rather than replacing it
    assert "worktree-seed-skipped" in kinds
    # the worktree the operator has to inspect is still mounted, and it is the
    # renderer half that is short — config.toml made it in, so "seeding ran"
    assert (Path(task.worktree_path) / "_bmad" / "config.toml").is_file()
    assert not (Path(task.worktree_path) / "_bmad" / "scripts" / "render_skill.py").exists()


def _real_skill_dirs(project, *skills: str, tree: str = ".claude/skills") -> None:
    """Lay skills down as ORDINARY in-repo dirs — the half `_symlink_skill_tree` cannot
    express, and what the primitive-era gate needs to be visible at all.

    A tree symlinked out WHOLE drops every skill at once, so the resolved primitive is
    always among the casualties and the gate is right to pause. Only the MIXED shape
    separates the two questions: the skill this run dispatches seeds normally, and the
    one it never names does not."""
    from bmad_loop.install import DEV_PRIMITIVE_MARKERS

    for skill in skills:
        real = project.project / tree / skill
        real.mkdir(parents=True, exist_ok=True)
        (real / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in DEV_PRIMITIVE_MARKERS:
            (real / marker).write_text("x\n", encoding="utf-8")


def _symlink_skill_tree(
    project,
    shared: Path,
    tree: str = ".claude/skills",
    *,
    renderer_stub: bool = False,
    skills: Sequence[str] | None = None,
) -> None:
    """Point upstream skills in ``tree`` at a shared install OUTSIDE the repo — how a
    machine-wide BMad install is wired, and the one shape provisioning cannot follow.

    The tree must be GITIGNORED (and so untracked) for this to bite: a committed
    symlink is checked out into the worktree as a symlink and resolves there just
    fine. It is the untracked case that provisioning has to copy, and the copy is
    what the containment guard refuses.

    ``skills`` defaults to everything a session dispatches. Pass an explicit list to
    symlink out ONE skill beside real dirs (see :func:`_real_skill_dirs`); the default
    cannot express that, which is why the era over-breadth shipped green."""
    from conftest import RENDERER_STUB_SKILL_MD, RENDERER_WORKFLOW_MD

    from bmad_loop.install import DEV_PRIMITIVE_MARKERS, DEV_PRIMITIVE_NEW, REVIEW_HUNTER_SKILLS

    tree_dir = project.project / tree
    tree_dir.mkdir(parents=True, exist_ok=True)
    for skill in skills if skills is not None else (DEV_PRIMITIVE_NEW, *REVIEW_HUNTER_SKILLS):
        real = shared / skill
        real.mkdir(parents=True, exist_ok=True)
        stub = renderer_stub and skill == DEV_PRIMITIVE_NEW
        (real / "SKILL.md").write_text(
            RENDERER_STUB_SKILL_MD if stub else f"# {skill}\n", encoding="utf-8"
        )
        if stub:
            # same pairing as `install_build_auto_skill`: a #2601 stub without the
            # `workflow.md` it composes from is a BROKEN install, not a leaner one
            (real / "workflow.md").write_text(RENDERER_WORKFLOW_MD, encoding="utf-8")
        for marker in DEV_PRIMITIVE_MARKERS:
            (real / marker).write_text("x\n", encoding="utf-8")
        (tree_dir / skill).symlink_to(real, target_is_directory=True)


def _symlink_skill_file(
    project,
    shared: Path,
    skill: str,
    filename: str,
    tree: str = ".claude/skills",
) -> None:
    """Replace ONE CHILD FILE of an already-real skill dir with a symlink to a file
    outside the repo — the shape neither sibling helper can express.

    :func:`_real_skill_dirs` lays a skill down whole and :func:`_symlink_skill_tree`
    points the WHOLE dir out of the repo, so between them a skill is either seeded
    completely or dropped completely. Neither can produce the state in between: a
    worktree skill dir that is PRESENT and resolvable but SHORT of one file the repo
    carries, because the per-file containment guard drops exactly the symlinked child
    and copies every sibling. That is the shape a DIRECTORY-granular check calls
    complete — the dir exists, so nothing is reported — and then dispatches a session
    whose step-04 has no customization to read.

    ``filename`` is deliberately free rather than a marker: since the seed gate asks
    walk PARITY with the repo instead of a named required set, this helper has to be
    able to drop a NON-marker child too (a step file, the renderer's `workflow.md`) —
    the half the old surface could not see at all.

    The target file is created, so the link is LIVE: the repo-side preflight stats
    through it and passes, which is what makes the worktree the only short half.
    """
    target = shared / skill / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    link = project.project / tree / skill / filename
    link.unlink(missing_ok=True)
    link.symlink_to(target)


def _symlink_skill_subdir(
    project,
    shared: Path,
    skill: str,
    subdir: str,
    *files: str,
    tree: str = ".claude/skills",
) -> None:
    """Replace one SUB-DIRECTORY of an already-real skill dir with a symlink to a
    populated directory outside the repo.

    The shape that separates a shared walk from a mirrored one, and the only helper
    that can build it. ``iterdir()`` + ``is_dir()`` descends a symlinked sub-directory;
    ``Path.rglob``/``**`` does not. So the copier walks INTO this dir and drops every
    child on containment, while a parity check spelled as ``repo_skill.rglob("*")``
    enumerates nothing under it and calls the worktree complete.

    :func:`_symlink_skill_file` cannot stand in: a TOP-LEVEL symlinked file is
    enumerated fine by rglob, so it stays green under exactly that ablation.
    """
    target = shared / skill / subdir
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        (target / name).write_text("x\n", encoding="utf-8")
    link = project.project / tree / skill / subdir
    if link.is_dir() and not link.is_symlink():
        shutil.rmtree(link)
    link.symlink_to(target, target_is_directory=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_skill_seed_pauses_before_dispatch(project, tmp_path):
    """The skills half of the same fault the renderer sentinels catch, and the leg
    nothing covered: `provision_worktree` copies BASE_SKILLS from the main repo behind
    a containment guard, and a skill tree symlinked to a shared install outside the
    repo resolves out of `repo_root` and is dropped — silently, because that guard's
    only stated justification ("the run-start preflight reports it") is true of the
    skill-genuinely-absent leg and FALSE of this one. The preflight stats through the
    link and passes.

    So a project that passed preflight dispatched into a worktree holding none of its
    skills, and every session stalled on `Unknown command` having written nothing —
    the exact failure `missing_base_skills` exists to prevent, reached anyway. Same
    environment-fault shape as the renderer legs: the seed reads the same repo for
    every unit, so no repair session fixes it and no later story escapes it."""
    from conftest import attach_profile

    from bmad_loop import install

    # gitignored skill tree = the normal shape, and the one that bites (see helper)
    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    # A COMPLETE renderer surface, deliberately: it keeps the renderer legs silent, so
    # the escalation below can only have come from the skills probe — and it is what
    # lets the era-gate ablation discriminate this test from its inline sibling.
    bmad = project.project / "_bmad" / "scripts"
    bmad.mkdir(parents=True)
    (bmad / "render_skill.py").write_text("# render", encoding="utf-8")
    (bmad / "config_utils.py").write_text("# config", encoding="utf-8")
    (project.project / "_bmad" / "config.toml").write_text("[core]\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    _symlink_skill_tree(project, tmp_path / "shared-bmad-install", renderer_stub=True)
    # the repo-side preflight follows the symlinks and passes — which is exactly why
    # the run reaches provisioning at all
    assert install.missing_base_skills(project.project, [".claude/skills"]) == []
    assert install.resolve_dev_primitive(project.project, ".claude/skills") is not None

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree carrying no upstream skills")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the pause stopped the loop rather than walking the backlog into the same stall
    assert "1-1-b" not in engine.state.tasks
    entries = Journal(engine.run_dir).entries()
    kinds = [e["kind"] for e in entries]
    assert "story-escalated" in kinds
    escalated = [e for e in entries if e["kind"] == "story-escalated"]
    # names the skills that actually went missing, not the renderer surface: the two
    # have different remediations, and _bmad/ is intact here
    assert ".claude/skills/bmad-build-auto" in escalated[0]["reason"]
    assert BMAD_SCRIPTS_SEED_REL not in escalated[0]["reason"]
    assert CENTRAL_CONFIG_REL not in escalated[0]["reason"]
    # the seed's own report still went out, so the escalation reads as an ESCALATION
    # of that report rather than a replacement for it
    assert "worktree-seed-skipped" in kinds
    # the worktree stays mounted for inspection, and it really is empty of skills
    assert not (Path(task.worktree_path) / ".claude" / "skills" / "bmad-build-auto").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_skill_seed_pauses_an_inline_primitive_too(project, tmp_path):
    """The delta from the renderer legs, and the thing most likely to be got wrong by
    copying them: this gate carries NO era condition. `_bmad/` is absent entirely, so
    `renderer_stub_resolved` is False and the renderer branch cannot fire — yet an
    inline `SKILL.md` the worktree does not have stalls a session exactly as hard as a
    renderer stub it does not have. Gating this on the renderer, as the sentinel legs
    are, would let the whole pre-#2601 world dispatch into the stall."""
    from conftest import attach_profile

    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _symlink_skill_tree(project, tmp_path / "shared-bmad-install")
    assert not (project.project / "_bmad").exists()  # nothing renderer-era anywhere
    assert not renderer_stub_resolved(project.project, [".claude/skills"])

    def never(spec):
        raise AssertionError("dispatched into a worktree carrying no upstream skills")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    escalated = [e for e in Journal(engine.run_dir).entries() if e["kind"] == "story-escalated"]
    # names the PRIMITIVE, not just the tree: the three hunters are dropped here too, so
    # a bare `.claude/skills/` substring passes even when the primitive stopped being
    # checked at all — which is how this assert survived an ablation of that very leg
    assert escalated and ".claude/skills/bmad-build-auto" in escalated[0]["reason"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_unused_primitive_era_dropped_by_the_seed_does_not_pause(project, tmp_path):
    """The other boundary, and the one the gate overran when it shipped: BASE_SKILLS is
    a copy-if-PRESENT catalog listing both primitive eras, so reading it as a
    requirement set pauses a run over a skill nothing dispatches.

    Here every skill a session names — the resolved `bmad-build-auto`, the three inline
    hunters, the merged `bmad-review` — is an ordinary in-repo dir that seeds normally.
    Only the leftover `bmad-dev-auto` shim is symlinked to a shared install and dropped,
    and post-rename no prompt ever spells it (`Engine._dev_skill` resolves the name from
    disk). Nothing can stall, so nothing may pause: this project passed the preflight
    and is not broken.

    The two tests above cannot see this — `_symlink_skill_tree` drops the whole
    dispatched set at once, so the resolved primitive is always among the casualties."""
    from conftest import attach_profile

    from bmad_loop import install

    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    _real_skill_dirs(
        project, install.DEV_PRIMITIVE_NEW, *install.REVIEW_HUNTER_SKILLS, "bmad-review"
    )
    # the ONE skill outside the repo is the era this run will never invoke — and it is
    # marker-COMPLETE, so it is a skill that would resolve if anything asked for it
    _symlink_skill_tree(
        project, tmp_path / "shared-bmad-install", skills=[install.DEV_PRIMITIVE_LEGACY]
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    tree = ".claude/skills"
    # the name every prompt spells, resolved the way the engine resolves it
    assert install.dev_primitive_or_default(project.project, tree) == install.DEV_PRIMITIVE_NEW
    assert install.missing_base_skills(project.project, [tree]) == []

    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)
    summary = engine.run()

    assert not summary.paused and summary.escalated == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert "story-escalated" not in [e["kind"] for e in Journal(engine.run_dir).entries()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_tracked_symlinked_skill_tree_does_not_pause(project, tmp_path):
    """Control, and the boundary the gate must not overrun: the identical symlinks,
    COMMITTED. Git stores a symlink as a symlink, so the worktree checks it out and
    the skill resolves there — nothing was dropped and the run must proceed.

    Without this, a gate that simply refused every symlinked tree would pass the two
    tests above while breaking every project that commits one."""
    from conftest import attach_profile

    _symlink_skill_tree(project, tmp_path / "shared-bmad-install")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})  # tracks the symlinks

    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)
    summary = engine.run()

    assert not summary.paused and summary.escalated == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # nothing was reported short, which is the gate's own evidence that the worktree
    # resolved the skill through the checked-out symlink (a successful run tears its
    # worktree down, so the tree itself is gone by now — the journal is the record)
    kinds = [e["kind"] for e in Journal(engine.run_dir).entries()]
    assert "story-escalated" not in kinds
    assert "worktree-seed-skipped" not in kinds


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_short_skill_dir_pauses_before_dispatch(project, tmp_path):
    """The granularity leg: the worktree HAS the dev primitive's directory, and it is
    still unusable. Only `customize.toml` is symlinked to a shared install outside the
    repo, so the per-file containment guard drops that one child and copies SKILL.md
    and the step files normally.

    A directory-granular seed (skip the whole dir when the destination exists) and a
    directory-granular gate (ask only whether the dir is there) both call that state
    complete, so the session dispatches and stalls INSIDE the workflow rather than on
    `Unknown command` — having written nothing, on this story and, since the seed
    reads the same repo every time, on every story after it. Same environment-fault
    shape as the whole-skill case, one layer down.

    The repo passes its own preflight — it stats through the link — which is the whole
    reason the run reaches provisioning. And `_bmad/` is absent entirely, so no
    renderer leg can be what fired."""
    from conftest import attach_profile

    from bmad_loop import install
    from bmad_loop.install import base_skills_seed_incomplete

    # gitignored skill tree = the normal shape, and the only one provisioning copies:
    # a tracked tree is checked out and there is nothing for the guard to drop
    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    _real_skill_dirs(project, install.DEV_PRIMITIVE_NEW, *install.REVIEW_HUNTER_SKILLS)
    # ...then swap exactly ONE required file of the resolved primitive for a link out
    _symlink_skill_file(
        project, tmp_path / "shared-bmad-install", install.DEV_PRIMITIVE_NEW, "customize.toml"
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    # the repo-side preflight follows the link and is HAPPY — that is the point: the
    # repo passes and the worktree does not
    assert install.missing_base_skills(project.project, [".claude/skills"]) == []
    assert install.resolve_dev_primitive(project.project, ".claude/skills") is not None
    # nothing renderer-era anywhere, so this is unambiguously the skills gate
    assert not renderer_stub_resolved(project.project, [".claude/skills"])

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree whose primitive dir is short")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the pause stopped the loop rather than walking the backlog into the same stall
    assert "1-1-b" not in engine.state.tasks
    entries = Journal(engine.run_dir).entries()
    assert "story-escalated" in [e["kind"] for e in entries]
    escalated = [e for e in entries if e["kind"] == "story-escalated"]
    # the FULL rel, including the file. A bare `.claude/skills/bmad-build-auto` is the
    # COARSE rel this same gate emits for a wholly-absent skill, so asserting only that
    # would survive an ablation back to directory granularity — which is precisely the
    # bug this test exists for.
    assert ".claude/skills/bmad-build-auto/customize.toml" in escalated[0]["reason"]
    # re-probe the predicate against the still-mounted worktree with an exact ==, so
    # extra rels (a second dropped file, the coarse rel alongside the fine one) cannot
    # hide behind a substring match on the prose above
    assert base_skills_seed_incomplete(
        Path(task.worktree_path), project.project, [".claude/skills"]
    ) == [".claude/skills/bmad-build-auto/customize.toml"]
    # ...and the rest of the dir really did arrive: this is a SHORT skill dir, not an
    # absent one, which is the only thing that makes the granularity the subject
    primitive = Path(task.worktree_path) / ".claude" / "skills" / "bmad-build-auto"
    assert (primitive / "SKILL.md").is_file()
    assert (primitive / "step-04-review.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_non_marker_file_pauses_before_dispatch(project, tmp_path):
    """The ten-thirteenths the old required set could not see. `step-01-clarify-and-route
    .md` is not a :data:`DEV_PRIMITIVE_MARKER`, so a gate asking for `SKILL.md` plus the
    two markers finds this dir complete and dispatches — the sibling test above passes
    unchanged under that gate because `customize.toml` happens to be a marker.

    The dropped file is not inert. It is the primitive's routing step: under a #2601
    stub `workflow.md` names it as a `[[bmad-snapshot:…]]` source and `render_skill.py`
    prints `HALT:` for an undeclared one; under an inline `SKILL.md` the session just
    reads a step that is not there. Either way the story Stops having written nothing,
    and since the seed reads the same repo every time, so does every story after it.

    Walk parity with the copier covers all thirteen files and cannot fall behind an
    upstream that renames a step — which is the argument for the reversal, made
    executable."""
    from conftest import attach_profile, install_build_auto_skill

    from bmad_loop import install
    from bmad_loop.install import base_skills_seed_incomplete

    dropped = "step-01-clarify-and-route.md"
    # the whole premise: the old surface was SKILL.md + these two, and this is neither
    assert dropped not in install.DEV_PRIMITIVE_MARKERS

    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    # the realistic multi-file primitive (7 files), INLINE era, so no renderer leg fires
    install_build_auto_skill(project.project)
    _real_skill_dirs(project, *install.REVIEW_HUNTER_SKILLS)
    _symlink_skill_file(
        project, tmp_path / "shared-bmad-install", install.DEV_PRIMITIVE_NEW, dropped
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    # the repo passes its own preflight — it stats through the link
    assert install.missing_base_skills(project.project, [".claude/skills"]) == []
    assert not renderer_stub_resolved(project.project, [".claude/skills"])

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree missing the primitive's step-01")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert "1-1-b" not in engine.state.tasks
    escalated = [e for e in Journal(engine.run_dir).entries() if e["kind"] == "story-escalated"]
    assert escalated and f".claude/skills/bmad-build-auto/{dropped}" in escalated[0]["reason"]
    # exact ==, so the coarse rel or a second casualty cannot hide behind the substring
    assert base_skills_seed_incomplete(
        Path(task.worktree_path), project.project, [".claude/skills"]
    ) == [f".claude/skills/bmad-build-auto/{dropped}"]
    # a SHORT dir, not an absent one: every sibling — markers and non-markers alike —
    # landed, which is what makes the dropped NON-marker the subject
    primitive = Path(task.worktree_path) / ".claude" / "skills" / "bmad-build-auto"
    assert (primitive / "SKILL.md").is_file()
    assert (primitive / "step-04-review.md").is_file()
    assert (primitive / "step-02-plan.md").is_file()
    assert (primitive / "review-prompts" / "adversarial.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_out_skill_subdir_pauses_before_dispatch(project, tmp_path):
    """The discriminator between a SHARED walk and a mirrored one, and the reason the
    gate calls the copier's own generator instead of restating it as an `rglob`.

    `iterdir()` + `is_dir()` descends a symlinked sub-directory; `Path.rglob`/`**` does
    not, on any supported Python. So the copier walks INTO `review-prompts/`, drops
    every child on containment, and an rglob-spelled parity check enumerates nothing
    under it and reports a complete worktree — dispatching a step-04 whose review
    prompts were never delivered. A shared install wired as a symlinked sub-tree is the
    ordinary shape of this, not a contrived one.

    ⚠️ NO Windows analogue exists: a symlinked sub-directory cannot be built on Windows
    CI, so ablation A2 (walk → rglob) has no witness there. Stating the gap rather than
    leaving it implicit — `test_a_dropped_nested_file_is_reported_with_a_posix_rel` in
    test_install.py is the symlink-free Windows witness for the SEPARATOR ablation, and
    covers nothing about this one."""
    from conftest import attach_profile, install_build_auto_skill

    from bmad_loop import install
    from bmad_loop.install import base_skills_seed_incomplete

    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    install_build_auto_skill(project.project)
    _real_skill_dirs(project, *install.REVIEW_HUNTER_SKILLS)
    _symlink_skill_subdir(
        project,
        tmp_path / "shared-bmad-install",
        install.DEV_PRIMITIVE_NEW,
        "review-prompts",
        "adversarial.md",
        "edge-case.md",
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    assert install.missing_base_skills(project.project, [".claude/skills"]) == []

    def never(spec):
        raise AssertionError("dispatched into a worktree missing the primitive's review prompts")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    escalated = [e for e in Journal(engine.run_dir).entries() if e["kind"] == "story-escalated"]
    assert escalated
    # both children of the symlinked dir, each as its own nested rel
    assert base_skills_seed_incomplete(
        Path(task.worktree_path), project.project, [".claude/skills"]
    ) == [
        ".claude/skills/bmad-build-auto/review-prompts/adversarial.md",
        ".claude/skills/bmad-build-auto/review-prompts/edge-case.md",
    ]
    # the rest of the dir arrived: the sub-directory is the only casualty
    primitive = Path(task.worktree_path) / ".claude" / "skills" / "bmad-build-auto"
    assert (primitive / "SKILL.md").is_file()
    assert (primitive / "customize.toml").is_file()
    assert not (primitive / "review-prompts").exists()


def test_a_partially_tracked_skill_dir_is_repaired_not_escalated(project):
    """The clearing leg for the granularity fix, and the proof it needs BOTH layers:
    half of it — the per-file gate without the per-file merge — would pause a worktree
    that provisioning is perfectly able to repair.

    No symlinks anywhere (so this runs on Windows too). `.claude/` is deliberately NOT
    gitignored and `customize.toml` is deliberately NOT committed, which is the
    ordinary shape of a project that tracks its skill tree but keeps one generated or
    machine-local layer out of git. The worktree checks out every tracked file of
    `bmad-build-auto` and none of that one — a destination directory that EXISTS while
    being short, i.e. exactly what the old dir-level `if dst.exists(): continue` skipped
    whole. Per-FILE merge fills the hole, the gate finds nothing missing, and the run
    completes.

    Second half: the file provisioning just wrote is untracked and un-gitignored, so
    the unit's `git add -A` would sweep it into the story commit — the worktree's local
    git exclude is the only thing stopping a tool file the repo deliberately does not
    track from being committed back to the target branch."""
    from conftest import attach_profile, install_build_auto_skill

    from bmad_loop import install

    # `.claude/` NOT ignored: the skill dir has to be TRACKABLE for the checkout to
    # carry half of it, which is the whole premise
    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")
    skill = install_build_auto_skill(project.project, ".claude/skills")
    _real_skill_dirs(project, *install.REVIEW_HUNTER_SKILLS)
    (skill / "customize.toml").unlink()
    commit_sprint(project, {"1-1-a": "ready-for-dev"})  # tracks everything BUT that file
    # ...and back it comes: present in the repo, untracked, not gitignored
    (skill / "customize.toml").write_text(
        '[[review_layers]]\nid = "adversarial"\nprompt_file = "review-prompts/adversarial.md"\n',
        encoding="utf-8",
    )
    # the fixture's own premise, asserted rather than assumed: a tracked file here
    # would be checked out and there would be nothing to repair
    assert ".claude/skills/bmad-build-auto/customize.toml" in git(
        project.project, "status", "--porcelain"
    )
    # the repo passes its preflight, so a pause below could only be provisioning's
    assert install.missing_base_skills(project.project, [".claude/skills"]) == []

    head_before = rev_parse_head(project.project)
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)
    summary = engine.run()

    assert not summary.paused and summary.escalated == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert "story-escalated" not in [e["kind"] for e in Journal(engine.run_dir).entries()]
    # ...and the repair stayed in the worktree: every commit the run landed carries the
    # code change and not one path under the skill tree
    files = git(
        project.project, "log", "--pretty=format:", "--name-only", f"{head_before}..HEAD"
    ).splitlines()
    assert "src.txt" in files
    assert not [f for f in files if f.startswith(".claude/skills")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_central_config_seed_pauses_before_dispatch(project, tmp_path):
    """The other leg of the same gate, and the one nothing above can reach: here
    `_bmad/scripts/` is a real directory that seeds WHOLE, and only
    `_bmad/config.toml` is symlinked out of the repo. The scripts check is therefore
    silent, `_seed_bmad_tree` reports the whole tree as seeded, and the repo-side
    preflight follows the symlink and passes — so before this leg existed the run
    dispatched into a worktree whose every session HALTs in `load_central_config`.

    The reason must name the CONFIG and not the scripts dir: the two have different
    remediations, and sending an operator to `_bmad/scripts` when it is intact is
    exactly the drift the shared sentinel tuple exists to prevent."""
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=True)
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "ready-for-dev"})
    shared = tmp_path / "shared-bmad"
    shared.mkdir()
    (shared / "config.toml").write_text("[core]\n", encoding="utf-8")
    bmad = project.project / "_bmad"
    (bmad / "scripts").mkdir(parents=True)
    (bmad / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (bmad / "scripts" / "config_utils.py").write_text("# config", encoding="utf-8")
    (bmad / "config.toml").symlink_to(shared / "config.toml")
    # the repo-side probe follows the symlink, which is why the preflight passed
    assert (bmad / "config.toml").is_file()

    dispatched: list[str] = []

    def never(spec):
        dispatched.append(spec.cwd.name)
        raise AssertionError("dispatched into a worktree with no central config")

    engine, adapter = make_engine(project, [never, never])
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert dispatched == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert "1-1-b" not in engine.state.tasks
    entries = Journal(engine.run_dir).entries()
    escalated = [e for e in entries if e["kind"] == "story-escalated"]
    assert escalated
    assert CENTRAL_CONFIG_REL in escalated[0]["reason"]
    assert BMAD_SCRIPTS_SEED_REL not in escalated[0]["reason"]
    # the worktree stays mounted, and it is the CONFIG half that is short
    assert not (Path(task.worktree_path) / "_bmad" / "config.toml").exists()
    assert (Path(task.worktree_path) / "_bmad" / "scripts" / "render_skill.py").is_file()


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_seed_is_journalled_and_does_not_pause(project, tmp_path):
    """The OTHER half of the seed silence (#415): an entry the repo HAS that the
    worktree did not get. The repo carries `.mcp.json` as a symlink out of itself —
    dotfile-managed, an ordinary healthy setup — so the resolve-and-contain guard
    drops it and the session runs without it.

    Journaled, never escalated, and that is the deliberate difference from the two
    gates above: those name files the ORCHESTRATOR dispatches or the renderer HALTs
    on, so their absence stalls every story determinately. A seed entry is arbitrary
    user config bmad-loop cannot know the session needs, and the trigger is a setup
    that works — escalating would refuse every run of such a project over a guard
    doing its job.

    Its own kind, not `worktree-seed-skipped`: "already there, your entry is doing
    nothing" and "the repo has it and this worktree does not" want different fixes,
    and the skipped channel additionally carries reserved sentinels the renderer
    escalation reads by exact membership."""
    # gitignored, which is what makes it a seed entry at all: a worktree checks out
    # tracked files only, so the fresh checkout has none of it
    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.mcp.json\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    shared = tmp_path / "dotfiles" / "mcp.json"
    shared.parent.mkdir(parents=True)
    shared.write_text('{"mcpServers": {}}', encoding="utf-8")
    (project.project / ".mcp.json").symlink_to(shared)

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(worktree_seed=(".mcp.json",)),
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    entries = Journal(engine.run_dir).entries()
    kinds = [e["kind"] for e in entries]
    assert "story-escalated" not in kinds
    dropped = [e for e in entries if e["kind"] == "worktree-seed-dropped"]
    assert dropped and dropped[0]["entries"] == [".mcp.json"]
    # the no-op channel stayed quiet: this entry is not doing nothing, it is missing
    assert "worktree-seed-skipped" not in kinds


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_dropped_hook_config_is_journalled_and_does_not_pause(project, tmp_path):
    """The one seed rel whose destination cannot answer for it (#405), and this is the
    ENGINE's witness that the call site says so.

    `.claude/settings.json` is claude's `hooks.config_path` as well as one of its
    default seed entries, and here the repo carries it as a symlink to a dotfile
    checkout — the same ordinary healthy setup the sibling above uses for `.mcp.json`,
    and the trigger the gate's own docstring names. The seed loop's source-containment
    guard drops it; the hook-registration step below provisioning then writes that exact
    path with a hooks-only config, so the destination probe stats a real file and reads
    the rel as delivered. Nothing is short at a path anyone can see, and the operator's
    own `env` block never reaches the session.

    At the engine and not only in `test_install.py` because passing the rels is the
    CALL SITE's half: `config_paths` defaults to `()`, so a fix that never reaches this
    call is byte-identical to no fix and invisible to every unit test of the gate.

    `attach_profile` is what makes the fault expressible at all, and both halves of it
    are the profile's: with no `profile` on the adapter `_worktree_profiles()` is empty,
    so `.claude/settings.json` is not a seed entry AND the hook step never writes it.
    The near-miss shape to avoid is naming the rel in `worktree_seed` instead — a
    profile-less run then leaves the destination genuinely absent, the ordinary probe
    reports it, and the test passes on the unfixed build."""
    from conftest import attach_profile

    from bmad_loop import install

    # gitignored `.claude/` is the shape that bites, and the reason the settings file
    # is a seed entry at all: a worktree checks out tracked files only
    (project.project / ".gitignore").write_text(".bmad-loop/runs/\n.claude/\n", encoding="utf-8")
    # ordinary in-repo skills, so the skills gate stays silent and the escalation
    # assertion below can only be about this fix
    _real_skill_dirs(
        project, install.DEV_PRIMITIVE_NEW, *install.REVIEW_HUNTER_SKILLS, "bmad-review"
    )
    shared = tmp_path / "dotfiles" / "settings.json"
    shared.parent.mkdir(parents=True)
    shared.write_text('{"env": {"FROM_THE_REPO": "1"}}', encoding="utf-8")
    (project.project / ".claude" / "settings.json").symlink_to(shared)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)  # without this the hook step never runs — see the docstring
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    entries = Journal(engine.run_dir).entries()
    assert "story-escalated" not in [e["kind"] for e in entries]
    dropped = [e for e in entries if e["kind"] == "worktree-seed-dropped"]
    assert dropped and dropped[0]["entries"] == [".claude/settings.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_escalating_provision_still_journals_the_worktree_it_mounted(project, tmp_path):
    """Both provisioning escalations promise the half-provisioned worktree "stays
    mounted for the operator to inspect", and the journal was the one place that never
    said WHERE — `worktree-opened` used to be appended after every gate had passed, so
    the runs that most need the path were exactly the ones with no record of it.

    Appended beside the state it mirrors instead, as soon as `task.worktree_path` is
    set, so it pairs 1:1 with `worktree-open-failed` and every mount attempt leaves
    exactly one record either way. The renderer leg is the witness because it is the
    LAST thing below the append that can raise; the fixture is
    `test_incomplete_bmad_scripts_seed_pauses_before_dispatch`'s."""
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=True)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    shared = tmp_path / "shared-bmad-scripts"
    shared.mkdir()
    (shared / "render_skill.py").write_text("# render", encoding="utf-8")
    (shared / "config_utils.py").write_text("# config", encoding="utf-8")
    bmad = project.project / "_bmad"
    bmad.mkdir(parents=True)
    (bmad / "config.toml").write_text("[core]\n", encoding="utf-8")
    (bmad / "scripts").symlink_to(shared, target_is_directory=True)

    engine, adapter = make_engine(project, [])  # empty script: nothing may be dispatched
    attach_profile(adapter)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    entries = Journal(engine.run_dir).entries()
    kinds = [e["kind"] for e in entries]
    opened = [e for e in entries if e["kind"] == "worktree-opened"]
    assert opened and kinds.index("worktree-opened") < kinds.index("story-escalated")
    # …and it names the worktree the escalation leaves mounted, not just the attempt
    assert opened[0]["path"] == engine.state.tasks["1-1-a"].worktree_path
    assert Path(opened[0]["path"]).is_dir()


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


# ------------------- carrying a deferred isolated unit's harvest out (#405, PR #406)
#
# The harvest writes `workspace.paths.deferred_work`, which under isolation is the
# UNIT WORKTREE's ledger — and `_integrate_unit`'s DEFERRED arm never merges that
# worktree. Nothing is destroyed (keep_failed defaults True; `capture_diff` takes
# untracked files too), but no ledger a sweep READS would ever see the findings.
# `_carry_harvested_deferrals` re-files them into the main checkout and commits.

_CARRY_A = {
    "summary": "Retry loop has no ceiling",
    "evidence": "the backoff doubles forever: no cap",
    "location": "src/retry.py:88",
    "severity": "medium",
}
_CARRY_B = {
    "summary": "Timeout is not configurable",
    "evidence": "hardcoded 30s",
    "location": "src/net.py:12",
    "severity": "low",
}


def _carry_origin(finding: dict) -> str:
    """The `origin:` marker the harvest fingerprints a finding into. Derived here
    the way the harvest derives it (summary + location) rather than pasted as a
    literal, so a change to the fingerprint's inputs cannot leave this file
    asserting against a hash nothing produces any more."""
    from bmad_loop import devcontract
    from bmad_loop.engine import HARVEST_ORIGIN

    return (
        f"{HARVEST_ORIGIN} "
        f"{devcontract.harvest_fingerprint(finding['summary'], finding['location'])}"
    )


def _main_ledger(project):
    """Entries in the MAIN checkout's ledger — the one a sweep reads. Deliberately
    `project.deferred_work`, never a worktree-rebased path: that distinction is the
    whole subject of these tests."""
    from bmad_loop import deferredwork

    text = (
        project.deferred_work.read_text(encoding="utf-8") if project.deferred_work.is_file() else ""
    )
    return deferredwork.parse_ledger(text)


def _carry_defer_script(project, key, *, deferred):
    """Dev harvests `deferred`, then a review that never converges exhausts the
    budget and defers the unit. Consumers must pin `_NO_DAMP` — see `_defer_script`.

    The review passes write the spec with NO `deferred:` field, which is the common
    shape and, importantly, the one that makes them re-enter `_harvest_spec_deferrals`
    and return early: the dev leg's record must survive that."""
    return [wt_dev_effect(project, key, deferred=deferred)] + [
        wt_review_effect(project, key, clean=False, patched=1) for _ in range(3)
    ]


def _wt_baseline_liar(project, key, *, deferred=None):
    """A unit-worktree dev session that COMPLETES, does real work and finalizes its
    spec to `done` — so the harvest fires — but stamps a baseline that is not the
    orchestrator's, so `_verify_dev_artifacts` returns a NON-fixable retry. Under
    isolation `_rollback_or_pause` always auto-recovers (the worktree is
    disposable), which is what reverts the attempt's ledger edit."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + f"bad attempt for {key}\n")
        sp = wt.implementation_artifacts / f"spec-{key}.md"
        write_spec(sp, "done", "0" * 40, deferred=deferred)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": key,
                "spec_file": str(sp),
                "baseline_commit": "0" * 40,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )

    return effect


def _ledger_commits(project, head_before) -> list[str]:
    """Commits on the main checkout since `head_before` that touched the ledger."""
    log = git(
        project.project,
        "log",
        "--pretty=format:%H",
        "--name-only",
        f"{head_before}..HEAD",
    )
    out, sha = [], ""
    for line in log.splitlines():
        if not line.strip():
            continue
        if len(line) == 40 and " " not in line:
            sha = line
        elif line.endswith("deferred-work.md"):
            out.append(sha)
    return out


def test_deferred_isolated_unit_carries_its_harvest_into_the_main_ledger(project):
    """The gap this closes: the finding is filed in a worktree that is about to be
    dropped unmerged, so every sweep-side reader sees nothing. The carry re-files it
    into the MAIN checkout and COMMITS it — committing is not incidental, an
    uncommitted ledger edit here is dirt in the path of the next story's
    `_merge_local`, whose `clean_incoming_collisions` escalates on it."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        _carry_defer_script(project, "1-1-a", deferred=[_CARRY_A]),
        policy=wt_policy(limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    entries = _main_ledger(project)
    assert [e.title for e in entries] == ["Retry loop has no ceiling"]
    assert entries[0].open
    body = entries[0].body
    assert f"origin: {_carry_origin(_CARRY_A)}" in body
    assert "source_spec: `spec-1-1-a.md`" in body
    assert "location: src/retry.py:88" in body and "severity: medium" in body
    # committed on the target branch, and nothing left dirty behind it
    assert len(_ledger_commits(project, head_before)) == 1
    assert worktree_clean(project.project)
    # …and ONLY the ledger came out: the deferred unit's code is still in its worktree
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    carried = [e for e in engine.journal.entries() if e["kind"] == "harvest-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == [entries[0].id]


def test_the_carry_does_not_depend_on_the_worktree_surviving(project):
    """`keep_failed = False` drops the unit worktree and deletes its branch, so the
    ledger the harvest wrote is gone by the end of the run. The carry still lands —
    it re-files from the task's own persisted record, not by reading the doomed
    tree, which is what lets it run before teardown and stay correct regardless of
    where teardown sits."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        _carry_defer_script(project, "1-1-a", deferred=[_CARRY_A]),
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    # the worktree that held the harvested entry is gone …
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    # … and the finding is in the main ledger anyway
    assert [e.title for e in _main_ledger(project)] == ["Retry loop has no ceiling"]
    assert worktree_clean(project.project)


def test_only_the_final_attempts_harvest_carries(project):
    """Attempt 1 harvests A and is rolled back — the ledger edit reverted with the
    code it describes. Attempt 2 harvests B and exhausts the attempt budget. Only B
    may carry: A names work no attempt ever landed.

    Reddens on an append-instead-of-assign harvest record (A would ride along) and
    on dropping the per-attempt clear is NOT what this pins — see the sibling
    below, whose final attempt never reaches the harvest at all."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _wt_baseline_liar(project, "1-1-a", deferred=[_CARRY_A]),
            _wt_baseline_liar(project, "1-1-a", deferred=[_CARRY_B]),
        ],
        policy=wt_policy(limits=LimitsPolicy(max_dev_attempts=2)),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    assert "rollback-auto" in journal_kinds(engine)
    # both attempts really did harvest — without this the assertion below passes
    # on a build where the second harvest never fired either
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 2
    assert [e.title for e in _main_ledger(project)] == ["Timeout is not configurable"]


def test_a_final_attempt_that_never_harvested_carries_nothing(project):
    """The `20d3dc0` bug in a new place. Attempt 1 harvests A and is rolled back;
    attempt 2's session STALLS, so the harvest never runs and there is nothing this
    attempt intended to file. Carrying A here would re-file, into the main ledger,
    the very entry the rollback removed.

    The stall is the point, and why the per-attempt clear cannot live at the
    harvest's arm site: that site sits inside `if result.status == "completed"`, so
    a non-completing final attempt never reaches it and would defer still carrying
    attempt 1's record."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    def stalling(spec):
        return SessionResult(status="stalled")

    engine, _ = make_engine(
        project,
        [_wt_baseline_liar(project, "1-1-a", deferred=[_CARRY_A]), stalling],
        policy=wt_policy(limits=LimitsPolicy(max_dev_attempts=2)),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    kinds = journal_kinds(engine)
    # attempt 1's harvest DID fire and its record was persisted — so an empty main
    # ledger below is the clear working, not a run where nothing was ever recorded
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 1 and harvests[0]["dw_ids"] == ["DW-1"]
    assert "rollback-auto" in kinds and "story-deferred" in kinds
    assert _main_ledger(project) == []
    assert "harvest-carried" not in kinds


def test_a_fixable_retrys_kept_harvest_still_carries_when_the_next_attempt_stalls(
    project, tmp_path
):
    """The other side of the row above, and the reason the per-attempt clear is not
    unconditional. Attempt 1 harvests A and fails a FIXABLE gate, which keeps the tree
    AND the ledger entry on purpose. Attempt 2's session STALLS — so its harvest never
    runs, nothing re-files A, and nothing re-records it either. The budget is spent, the
    unit defers, and A is still sitting in the worktree that is about to go unmerged.

    A cleared record here is a silent loss with no journal line to show for it: the
    carry returns on an empty record before it ever reaches `append_entry`.

    The marker lives outside the project tree so attempt 1's failure is the verify
    command's and nothing else's — inside, it would be untracked proof of work in the
    worktree and change what the artifact gate answers."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = tmp_path / "never-created.marker"

    def stalling(spec):
        return SessionResult(status="stalled")

    pol = dataclasses.replace(
        wt_policy(limits=LimitsPolicy(max_dev_attempts=2)),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, deferred=[_CARRY_A]), stalling],
        policy=pol,
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    kinds = journal_kinds(engine)
    # attempt 1's retry really was the FIXABLE one — the non-fixable leg would have
    # reverted the entry, and then carrying nothing would be correct
    assert "rollback-auto" not in kinds and "story-deferred" in kinds
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 1 and harvests[0]["dw_ids"] == ["DW-1"]
    # …and the finding attempt 1 filed in the doomed worktree reached the main ledger
    assert [e.title for e in _main_ledger(project)] == ["Retry loop has no ceiling"]
    carried = [e for e in engine.journal.entries() if e["kind"] == "harvest-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == ["DW-1"]


def test_the_carry_never_duplicates_an_entry_already_open_in_the_main_ledger(project):
    """A previous run already filed this finding into the main ledger and it is
    still open. `append_entry` dedups on `origin:` + `source_spec:`, so the carry
    is free to be unconditional; the journal still records the attempt with an
    empty id list, which is how a full-dedup carry stays visible."""
    from bmad_loop import deferredwork

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    deferredwork.append_entry(
        project.deferred_work,
        title="Already filed by an earlier run",
        origin=_carry_origin(_CARRY_A),
        source_spec="spec-1-1-a.md",
        reason="same finding, same spec",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "seed deferred-work")
    head_before = rev_parse_head(project.project)

    engine, _ = make_engine(
        project,
        _carry_defer_script(project, "1-1-a", deferred=[_CARRY_A]),
        policy=wt_policy(limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1
    assert [e.title for e in _main_ledger(project)] == ["Already filed by an earlier run"]
    assert _ledger_commits(project, head_before) == []  # nothing to commit either
    carried = [e for e in engine.journal.entries() if e["kind"] == "harvest-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == []


def test_the_carry_records_what_the_harvest_intended_not_what_it_filed(project):
    """The dev leg files A. The review leg re-reads the same spec — whose
    `deferred:` list now holds A *and* its own B — and its harvest dedups A against
    the entry the dev leg already wrote, so it reports filing B alone. Both must
    still carry.

    That gap between "intended" and "filed" is the whole reason the record is taken
    from the harvest's full pending set. This is the cheap, same-run instance of it;
    the expensive one is a crash replay, where the replayed harvest dedups against
    the dead attempt's entries and reports filing *nothing at all*."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a", deferred=[_CARRY_A])] + [
        wt_review_effect(project, "1-1-a", clean=False, deferred=[_CARRY_A, _CARRY_B])
        for _ in range(3)
    ]
    engine, _ = make_engine(project, script, policy=wt_policy(limits=_NO_DAMP))
    summary = engine.run()

    assert summary.deferred == 1
    # the review harvest reported filing B only — A deduped against the dev leg's
    # entry — which is the state the record must NOT be derived from
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [h["dw_ids"] for h in harvests] == [["DW-1"], ["DW-2"], [], []]
    assert sorted(e.title for e in _main_ledger(project)) == [
        "Retry loop has no ceiling",
        "Timeout is not configurable",
    ]
    # …and the record itself is the union, not four re-appended rows. Behaviourally
    # an append carries the same entries — the per-attempt clear draws the boundary
    # and `append_entry` dedups the rest — so this row is what keeps `state.json`
    # from growing a copy of every dev-leg finding per review pass.
    assert len(engine.state.tasks["1-1-a"].harvested_deferrals) == 2


def test_a_ledger_git_refuses_to_commit_still_gets_the_finding(project):
    """A project that gitignores its ledger. `commit_paths` names the path
    explicitly and `git add -- <ignored path>` REFUSES with rc 1 — where every
    in-place path never notices, because `commit_story`'s `add -A` skips such a path
    in silence. Unguarded, that `GitError` would leave the run with no
    `_integrate_unit` at all: no forensic patch, no `unit-closed`, worktree still
    mounted — all to protect bookkeeping whose real job (getting the finding into a
    ledger a sweep reads) is already done.

    So the write raises and the commit does not. The miss is journalled, and the
    dirt it leaves is exactly what the next story's `clean_incoming_collisions`
    reports on its own.

    Gitignoring only the ledger file, not the artifacts dir, is deliberate: the
    sprint board and specs beside it must still commit, or the run fails for an
    unrelated reason and the assertion below proves nothing. (`_bmad-output/` — this
    repo's own shape — ignores the whole dir and is the realistic carrier.)"""
    (project.project / ".gitignore").write_text("deferred-work.md\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        _carry_defer_script(project, "1-1-a", deferred=[_CARRY_A]),
        policy=wt_policy(limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused  # no crash, no escalation
    assert [e.title for e in _main_ledger(project)] == ["Retry loop has no ceiling"]
    kinds = journal_kinds(engine)
    assert "harvest-carried" in kinds and "unit-closed" in kinds
    uncommitted = [e for e in engine.journal.entries() if e["kind"] == "harvest-carry-uncommitted"]
    assert len(uncommitted) == 1 and uncommitted[0]["dw_ids"] == ["DW-1"]
    assert "git add failed" in uncommitted[0]["error"]
    # the forensic patch the raise would have cost the run
    assert (engine.run_dir / "failed" / "1-1-a" / "changes.patch").is_file()


def test_a_gitignored_ledger_whose_name_globs_still_reports_the_commit_miss(project):
    """The row above under an artifacts dir holding `[` / `]`, where the journalled miss
    is precisely what a globbing pathspec loses.

    A glob operand and an explicit one disagree about ignored paths: `git add` REFUSES an
    explicitly-named ignored path — rc 1, the row above's whole premise — but SKIPS
    ignored paths it reached by GLOBBING. So the operand lands on the tracked sibling
    instead, `add` exits 0 having staged nothing, `status` finds no ledger change to
    commit, and the carry returns success having committed no ledger and journalled no
    miss. A silent loss exactly where the row above keeps a record (#423).

    (rc 0 needs a glob-reachable neighbour to exist. Clean, as here, and the add stages
    nothing; dirty, and it stages the operator's edit instead — the harm
    `test_commit_paths_does_not_stage_a_glob_neighbour` pins.)

    The ledger is ignored by FILENAME, like the row above and for the same reason: the
    sprint board lives in the artifacts dir too, and ignoring the dir wholesale would
    fail the run for an unrelated reason. The decoy is force-added past that same rule,
    which is an ordinary tracked-and-ignored path — `path_tracked` documents it."""
    root = project.project
    (root / ".gitignore").write_text("deferred-work.md\n", encoding="utf-8")
    paths = dataclasses.replace(project, implementation_artifacts=root / "_bmad-output" / "impl[1]")
    paths.implementation_artifacts.mkdir(parents=True)
    decoy = root / "_bmad-output" / "impl1" / "deferred-work.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("# a neighbour's ledger\n", encoding="utf-8")

    commit_sprint(paths, {"1-1-a": "ready-for-dev"})
    git(root, "add", "-f", "--", "_bmad-output/impl1/deferred-work.md")
    git(root, "commit", "-q", "-m", "tracked neighbour")

    engine, _ = make_engine(
        paths,
        _carry_defer_script(paths, "1-1-a", deferred=[_CARRY_A]),
        policy=wt_policy(limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    uncommitted = [e for e in engine.journal.entries() if e["kind"] == "harvest-carry-uncommitted"]
    assert len(uncommitted) == 1 and uncommitted[0]["dw_ids"] == ["DW-1"]
    assert "git add failed" in uncommitted[0]["error"]
    # the finding still reached the ledger a sweep reads — only the commit was missed
    assert [e.title for e in _main_ledger(paths)] == ["Retry loop has no ceiling"]
    assert decoy.read_text(encoding="utf-8") == "# a neighbour's ledger\n"


def test_a_done_isolated_unit_files_its_harvest_exactly_once(project):
    """The TRACKED-ledger half of the DONE leg. Here the entry really does ride the
    branch — `finalize_commit`'s `add -A` stages it — so the merge lands it and the
    carry that follows must add nothing: no second entry, and no commit of its own.

    The empty `dw_ids` is what pins the carry's PLACEMENT. Hoisted above
    `_merge_local` the carry meets a main ledger that does not hold the branch's
    copy yet, so it FILES instead of dedupping and commits what it filed — and the
    merge then lays the branch's own copy over it. Here the two copies are
    byte-identical (`append_entry` writes no timestamp and computes the same next
    id from the same base), so git resolves them and only the extra commit and the
    non-empty id list show; that equality is a coincidence of the simplest shape,
    not a property to lean on. Getting the merge in first makes the dedup do the
    work in every shape, which is the invariant, so this row asserts the dedup and
    not the downstream damage.

    Its sibling below is this same shape with a GITIGNORED ledger, where `add -A`
    skips the path and the merge has nothing to bring: that one is what the carry
    exists for, and this one is what keeps it from double-filing (#405)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=[_CARRY_A]),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert "unit-merged" in journal_kinds(engine)
    entries = _main_ledger(project)
    assert [e.title for e in entries] == ["Retry loop has no ceiling"]
    assert f"origin: {_carry_origin(_CARRY_A)}" in entries[0].body
    # the carry DID run and deduped to nothing — an empty id list is how a
    # full-dedup carry stays visible, and proves the row is not vacuous
    carried = [e for e in engine.journal.entries() if e["kind"] == "harvest-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == []
    # …and it added no commit: the only commit touching the ledger is the unit's own
    subjects = git(project.project, "log", "--format=%s", f"{head_before}..HEAD").splitlines()
    assert [s for s in subjects if s.startswith("chore(deferred-work)")] == []
    assert worktree_clean(project.project)


def test_a_done_isolated_unit_with_a_gitignored_ledger_still_carries(project):
    """P1, the leg the row above cannot reach. The unit LANDS and its branch merges,
    but `finalize_commit` stages with `git add -A`, which skips a GITIGNORED path in
    silence — so the harvest's entry never rode the branch and the merge has nothing
    to bring over. `close_unit_workspace(success=True)` then removes the worktree
    unconditionally, and the DONE leg (unlike DEFER) takes no `capture_diff`, so
    without the carry the finding has no surviving copy anywhere.

    Ignoring the ledger by FILENAME rather than the artifacts dir is deliberate and
    the same choice `test_a_ledger_git_refuses_to_commit_still_gets_the_finding`
    makes: the sprint board and the spec live in that dir too and must still commit,
    or the unit never reaches DONE and the assertions below prove nothing."""
    # keeping the template's run-dir ignore matters: the cleanliness assertion below
    # is about the carry's leftovers, not about the run dir going untracked
    (project.project / ".gitignore").write_text(
        ".bmad-loop/runs/\ndeferred-work.md\n", encoding="utf-8"
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=[_CARRY_A]),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds
    # the harvest fired and the worktree it wrote into is gone — so the main ledger
    # below holds the only copy, and it got there by the carry
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 1 and harvests[0]["dw_ids"] == ["DW-1"]
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    entries = _main_ledger(project)
    assert [e.title for e in entries] == ["Retry loop has no ceiling"]
    assert entries[0].open and f"origin: {_carry_origin(_CARRY_A)}" in entries[0].body
    carried = [e for e in engine.journal.entries() if e["kind"] == "harvest-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == ["DW-1"]
    # the commit is best effort and `git add -- <ignored path>` refuses (rc 1); the
    # miss is journalled, and the dirt it leaves is invisible to `status --porcelain`
    uncommitted = [e for e in engine.journal.entries() if e["kind"] == "harvest-carry-uncommitted"]
    assert len(uncommitted) == 1 and uncommitted[0]["dw_ids"] == ["DW-1"]
    assert "git add failed" in uncommitted[0]["error"]
    assert worktree_clean(project.project)
    # the unit's code really did land — this is the DONE leg, not a disguised defer
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def _gitignored_ledger_run(project, deferred=(_CARRY_A,)):
    """The row above's shape, hoisted: a gitignored ledger, a committed sprint, a dev
    leg that harvests `deferred` and a review that converges. Every row below either
    crashes this run in `_integrate_unit`'s DONE window or resumes over it."""
    (project.project / ".gitignore").write_text(
        ".bmad-loop/runs/\ndeferred-work.md\n", encoding="utf-8"
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    return make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=list(deferred) or None),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )


def _resume(project, engine, script=()):
    """A fresh Engine over the persisted state — the hand-rolled resume the worktree
    rows above use, hoisted because these rows resume twice. Empty script by default:
    the replay must not drive a session, and `ScriptExhausted` says so loudly."""
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(list(script))
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    return resumed, adapter


def test_a_carry_lost_to_a_crash_after_the_merge_replays_on_resume(project):
    """P2. `_finalize_commit_phase` persists Phase.DONE BEFORE `_integrate_unit` runs,
    and `_merge_local` ends by removing the worktree unconditionally — so a host death
    between the merge and the carry destroys the worktree's copy of the harvest and
    skips the carry that was going to rescue it. `_finish_inflight` opens with `if
    task.terminal: continue`, so nothing in the ordinary resume path looks at a DONE
    task again.

    The sibling DEFER terminus has no such window: `_defer` carries BEFORE its
    terminal `_save()`, so a death there leaves a non-terminal phase that resume
    re-drives normally. DONE is the only leg that persists terminal and then carries.

    Nothing is unrecoverable, which is what makes the fix a replay rather than a
    rescue: `harvested_deferrals` is persisted to state.json and survives the crash
    intact — only its application to the main ledger was missing (#405)."""
    engine, _ = _gitignored_ledger_run(project)
    crash_at_merge_back(engine)
    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    # the payload survived the crash — this is a lost WRITE, not lost data
    assert [d["title"] for d in crashed.harvested_deferrals] == ["Retry loop has no ceiling"]
    # …and the merge really landed, taking the worktree (the finding's only other
    # copy) with it. Without the replay below the entry is gone for good.
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert _main_ledger(project) == []

    resumed, adapter = _resume(project, engine)
    summary = resumed.run()

    assert not summary.crashed and not summary.paused
    assert adapter.sessions == []  # a replay, not a re-drive of committed work
    assert "resume-ledger-carry" in journal_kinds(resumed)
    entries = _main_ledger(project)
    assert [e.title for e in entries] == ["Retry loop has no ceiling"]
    assert entries[0].open and f"origin: {_carry_origin(_CARRY_A)}" in entries[0].body
    assert resumed.state.tasks["1-1-a"].isolated_ledger_carried


def test_a_replayed_carry_latches_so_a_second_resume_files_no_duplicate(project):
    """Why the replay needs a persisted LATCH and not just the writes' idempotency.

    `append_entry` dedups on `origin:` + `source_spec:` against OPEN entries only, so
    once anything closes the carried entry — a later sweep bundle, a human — a blind
    second replay no longer dedups and files a DUPLICATE under a fresh id. The close
    here stands in for that: it is the exact condition the latch exists for, and the
    reason `mark_done`'s full idempotency (False on an entry already done) is not
    enough on its own."""
    engine, _ = _gitignored_ledger_run(project)
    crash_at_merge_back(engine)
    assert engine.run().crashed

    first, _ = _resume(project, engine)
    first.run()
    assert [e.id for e in _main_ledger(project)] == ["DW-1"]

    mark_ledger_done(project, ["DW-1"])
    second, _ = _resume(project, engine)
    second.run()

    # one entry, still the closed one — no DW-2 filed behind its back
    entries = _main_ledger(project)
    assert [e.id for e in entries] == ["DW-1"] and not entries[0].open
    replays = [e for e in second.journal.entries() if e["kind"] == "resume-ledger-carry"]
    assert len(replays) == 1


def test_a_clean_landing_latches_its_carry_so_resume_never_refiles_it(project):
    """The latch's other half: the carry that DID run must say so durably, or every
    later resume of a finished run re-files a landed unit's harvest.

    Same closed-entry setup as the row above, for the same reason — an open entry
    would dedup a wrongful replay into invisibility and this row would pass on a build
    with no latch at all."""
    engine, _ = _gitignored_ledger_run(project)
    assert engine.run().done == 1
    assert engine.state.tasks["1-1-a"].isolated_ledger_carried
    assert [e.id for e in _main_ledger(project)] == ["DW-1"]

    mark_ledger_done(project, ["DW-1"])
    resumed, _ = _resume(project, engine)
    resumed.run()

    assert "resume-ledger-carry" not in journal_kinds(resumed)
    entries = _main_ledger(project)
    assert [e.id for e in entries] == ["DW-1"] and not entries[0].open


def test_a_crash_before_the_merge_leaves_a_mounted_worktree_uncarried(project):
    """The mounted-worktree guard, and it is a CORRECTNESS guard rather than an
    optimization. A worktree still on disk means the death landed at or before
    `close_unit_workspace`, and from the replay's vantage that is indistinguishable
    from "the branch never merged" — as here, where it did not. Carrying a unit whose
    branch never landed files an id the human's later merge would duplicate.

    It costs the narrower merged-but-not-torn-down sub-window, where the carry is lost
    exactly as it is today — never wrongly applied."""
    engine, _ = _gitignored_ledger_run(project)
    crash_at_merge_back(engine, after=False)
    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and crashed.harvested_deferrals
    # the branch never landed and its worktree is still mounted — the two facts the
    # guard reads as "this is not mine to carry"
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    assert len(worktree_list(project.project)) == 2

    resumed, adapter = _resume(project, engine)
    resumed.run()

    assert adapter.sessions == []
    assert "resume-ledger-carry" not in journal_kinds(resumed)
    assert _main_ledger(project) == []


def test_the_replay_pass_is_silent_when_the_crashed_unit_had_nothing_to_carry(project):
    """`resume-ledger-carry` is a RECOVERY signal an operator reads, so the pass must
    not emit it for a unit with an empty record. Most landing units have one: the
    payloads are only non-empty when a spec deferred a finding or a sweep bundle
    closed ids, and every other crashed-at-merge unit would otherwise announce a
    rescue that carried nothing.

    Same crash in the same window as the row above, with the dev leg's `deferred:`
    list omitted — the common shape — so the only difference is the empty record."""
    engine, _ = _gitignored_ledger_run(project, deferred=())
    crash_at_merge_back(engine)
    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    # the crash landed in the same window (DONE, un-latched, worktree torn down) —
    # every guard but the record's emptiness would let this one through
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert not crashed.harvested_deferrals and not crashed.bundle_closes_intended
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]

    resumed, _ = _resume(project, engine)
    resumed.run()

    assert "resume-ledger-carry" not in journal_kinds(resumed)
    assert _main_ledger(project) == []


def test_a_done_unit_whose_merge_escalates_does_not_carry(project):
    """The escalation legs keep the unit branch for a HUMAN to merge, and that merge
    brings the branch's own ledger state with it — so a copy carried here under a
    fresh id would duplicate it, and the conflict leg can leave the repo mid-merge
    besides. Both `_keep_branch_and_escalate` calls always raise `RunPaused`, which
    is what makes the carry unreachable; this row is what keeps that structural.

    Reddens on hoisting the carry into `_merge_local`'s prologue — the shape that
    looks equivalent because the DONE arm has exactly one caller."""
    (project.project / ".gitignore").write_text(
        ".bmad-loop/runs/\ndeferred-work.md\n", encoding="utf-8"
    )
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=[_CARRY_A]),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
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
    # the MERGE leg specifically, not the pre-merge collision leg — the run dir stays
    # ignored above so the target checkout is clean and only the ff-only merge fails
    assert "content conflict against the target" in (engine.state.paused_reason or "")
    kinds = journal_kinds(engine)
    # the harvest DID run, so an empty main ledger is the escalation withholding the
    # carry rather than a run where there was never anything to carry
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvests) == 1 and harvests[0]["dw_ids"] == ["DW-1"]
    assert "harvest-carried" not in kinds
    assert _main_ledger(project) == []
    # …and the branch is still there, holding the finding for the human's merge
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")

    # …and the crash-replay pass must not carry it either, on resume after resume.
    # Two guards say so independently: the phase is ESCALATED, not DONE, and
    # `_keep_branch_and_escalate` keeps the worktree mounted — which is exactly the
    # state that means "a human still owns this merge".
    resumed, _ = _resume(project, engine)
    resumed.run()
    assert "resume-ledger-carry" not in journal_kinds(resumed)
    assert _main_ledger(project) == []


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
