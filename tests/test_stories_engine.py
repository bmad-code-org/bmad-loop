"""StoriesEngine (folder+id dispatch) scenario + seam tests against the mock
adapter — no tmux, no LLM. Mirrors test_engine.py / test_sweep.py conventions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import git, write_spec

from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_STORY_CHECKPOINT,
    Phase,
    RunState,
    StoryTask,
    TokenUsage,
)
from bmad_loop.policy import (
    GatesPolicy,
    NotifyPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
)
from bmad_loop.stories_engine import StoriesEngine
from bmad_loop.verify import read_frontmatter, rev_parse_head, status_of

QUIET = NotifyPolicy(desktop=False, file=True)
SPEC_FOLDER = "_bmad-output/epic-1"  # under output_folder -> excluded from proof-of-work


def _stories_policy(**over) -> Policy:
    # review disabled keeps the happy path one session per story; gates none.
    base = dict(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    base.update(over)
    return Policy(**base)


def setup_stories(paths, entries: list[dict], *, spec_folder: str = SPEC_FOLDER) -> Path:
    """Lay down <spec_folder>/{SPEC.md, stories.yaml, stories/} and commit it."""
    folder = paths.project / spec_folder
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "SPEC.md").write_text("---\ntitle: Epic 1\n---\n# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    git(paths.project, "add", "-A")
    git(paths.project, "commit", "-q", "-m", "stories fixture")
    return folder


def stories_dev_effect(
    *, final_status: str = "done", followup_review: bool = False, prose_status: str | None = None
):
    """Simulate a bmad-dev-auto folder+id dispatch: read the story id + spec
    folder from the session env (as the real adapter does), write the id-keyed
    story spec, and touch real code so proof-of-work passes."""

    def effect(spec) -> SessionResult:
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        baseline = rev_parse_head(Path(spec.cwd))
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        sp = stories_dir / f"{story_id}-slug.md"
        src = Path(spec.cwd) / "src.txt"
        src.write_text(src.read_text() + f"work for {story_id}\n")
        write_spec(sp, final_status, baseline, prose_status=prose_status)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_id,
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


def stories_checkpoint_effect():
    """Simulate bmad-dev-auto honoring `Halt after planning.`: on a plan-halt leg
    (BMAD_LOOP_PLAN_HALT set by the engine) write the id-keyed spec at
    ready-for-dev with NO code change — the plan is just the spec — and mark the
    synthesized result `plan_halt`; otherwise implement to done + touch real code
    (the plain implement leg). One effect drives both legs of a spec_checkpoint
    story across a plan-checkpoint pause/resume."""

    def effect(spec) -> SessionResult:
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        baseline = rev_parse_head(Path(spec.cwd))
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        sp = stories_dir / f"{story_id}-slug.md"
        common = {
            "workflow": "auto-dev",
            "story_key": story_id,
            "spec_file": str(sp),
            "baseline_commit": baseline,
            "escalations": [],
        }
        if spec.env.get("BMAD_LOOP_PLAN_HALT"):
            write_spec(sp, "ready-for-dev", baseline)
            return SessionResult(
                status="completed",
                result_json={**common, "status": "ready-for-dev", "plan_halt": True},
            )
        src = Path(spec.cwd) / "src.txt"
        src.write_text(src.read_text() + f"work for {story_id}\n")
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={**common, "status": "done", "followup_review_recommended": False},
        )

    return effect


def make_engine(project, script, *, policy=None, spec_folder=SPEC_FOLDER, **kwargs):
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="test-run", project=str(project.project), started_at="now")
    engine = StoriesEngine(
        paths=project,
        policy=policy or _stories_policy(),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        spec_folder=spec_folder,
        **kwargs,
    )
    return engine, adapter


def resume_engine(project, engine, script):
    """Rebuild a StoriesEngine from persisted state, as cli._resume_paused_run
    does — the spec folder is restored from RunState, no flag."""
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(script)
    new_engine = StoriesEngine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        story_filter=state.story_filter,
        max_stories=state.max_stories,
        spec_folder=state.spec_folder,
    )
    return new_engine, adapter


def story_spec(paths, story_id: str, *, spec_folder: str = SPEC_FOLDER) -> Path:
    return paths.project / spec_folder / "stories" / f"{story_id}-slug.md"


def entry(story_id: str, **over) -> dict:
    d = {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}
    d.update(over)
    return d


# ------------------------------------------------------------- happy path


def test_two_story_happy_path(project):
    setup_stories(project, [entry("1"), entry("2")])
    engine, adapter = make_engine(project, [stories_dev_effect(), stories_dev_effect()])
    summary = engine.run()

    assert summary.done == 2
    assert not summary.paused
    # dispatched in strict list order
    dev_prompts = [s.prompt for s in adapter.sessions if s.role == "dev"]
    assert dev_prompts == [
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 1.",
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 2.",
    ]
    # both story specs are done on disk, both committed
    for sid in ("1", "2"):
        assert status_of(read_frontmatter(story_spec(project, sid))) == "done"
    assert engine.state.tasks["1"].phase == Phase.DONE
    assert engine.state.tasks["2"].phase == Phase.DONE


def test_run_state_pins_stories_mode(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [stories_dev_effect()])
    engine.run()
    persisted = load_state(engine.run_dir)
    assert persisted.source == "stories"
    assert persisted.spec_folder == SPEC_FOLDER


def test_session_env_carries_spec_folder(project):
    setup_stories(project, [entry("1")])
    engine, adapter = make_engine(project, [stories_dev_effect()])
    engine.run()
    dev = next(s for s in adapter.sessions if s.role == "dev")
    assert dev.env["BMAD_LOOP_SPEC_FOLDER"] == SPEC_FOLDER
    assert dev.env["BMAD_LOOP_STORY_KEY"] == "1"


def test_stories_validated_journaled_once(project):
    setup_stories(project, [entry("1"), entry("2")])
    engine, _ = make_engine(project, [stories_dev_effect(), stories_dev_effect()])
    engine.run()
    validated = [e for e in engine.journal.entries() if e.get("kind") == "stories-validated"]
    assert len(validated) == 1
    assert validated[0]["count"] == 2


# ------------------------------------------------------------- scheduling


def test_skips_done_on_disk_and_resumes_later(project):
    # story 1 already done on disk from a prior run (its spec present + committed);
    # a fresh run must skip it and dispatch story 2.
    folder = setup_stories(project, [entry("1"), entry("2")])
    sp1 = folder / "stories" / "1-slug.md"
    write_spec(sp1, "done", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 pre-done")

    engine, adapter = make_engine(project, [stories_dev_effect()])
    summary = engine.run()
    assert summary.done == 1  # only story 2 driven this run
    dev_prompts = [s.prompt for s in adapter.sessions if s.role == "dev"]
    assert dev_prompts == ["/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 2."]


def test_blocked_on_disk_pauses_for_resolve(project):
    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 blocked")

    engine, adapter = make_engine(project, [])  # no session should run
    summary = engine.run()
    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    assert persisted.paused_story_key == "1"
    assert not any(s.role == "dev" for s in adapter.sessions)  # never leapfrogged to story 2
    # an ESCALATED task is recorded (spec path attached) so resolve/rearm act on it
    wedged = persisted.tasks["1"]
    assert wedged.phase == Phase.ESCALATED
    assert wedged.spec_file == str(folder / "stories" / "1-slug.md")


def test_bare_resume_does_not_leapfrog_a_wedged_story(project):
    """MAJOR-A: a wedge (story 1 blocked on disk → ESCALATED task persisted) must
    not be leapfrogged by a plain `bmad-loop resume` that never resolved it. The
    within-run skip set excludes ESCALATED tasks, so resume re-classifies story 1
    from disk (still blocked) and re-pauses on it — story 2 never dispatches onto a
    tree missing story 1's work, honoring the linear no-leapfrog invariant."""
    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 blocked")

    engine, _ = make_engine(project, [])
    assert engine.run().paused
    assert load_state(engine.run_dir).tasks["1"].phase == Phase.ESCALATED

    # bare resume WITHOUT resolving — sessions are available but none must run
    resumed, radapter = resume_engine(project, engine, [stories_dev_effect(), stories_dev_effect()])
    rsummary = resumed.run()
    assert rsummary.paused and rsummary.done == 0
    persisted = load_state(resumed.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    assert persisted.paused_story_key == "1"  # re-paused on the SAME story, not story 2
    assert not any(s.role == "dev" for s in radapter.sessions)  # story 2 never dispatched


def test_sentinel_on_disk_pauses(project):
    folder = setup_stories(project, [entry("1")])
    # a pre-planning halt left a fixed-slug sentinel with status blocked
    write_spec(folder / "stories" / "1-unresolved.md", "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sentinel")
    engine, _ = make_engine(project, [])
    summary = engine.run()
    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    # the sentinel path is attached so rearm can preserve + delete it
    assert persisted.tasks["1"].spec_file == str(folder / "stories" / "1-unresolved.md")


def test_story_selector_filters_to_one_id(project):
    setup_stories(project, [entry("1"), entry("2"), entry("3")])
    engine, adapter = make_engine(project, [stories_dev_effect()], story_filter="2")
    summary = engine.run()
    assert summary.done == 1
    dev_prompts = [s.prompt for s in adapter.sessions if s.role == "dev"]
    assert dev_prompts == ["/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 2."]


# ----------------------------------------------------------- prompt seams


def test_dev_prompt_fresh_dispatch_shape(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert (
        engine._dev_prompt(task, None)
        == "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 1."
    )


def test_dev_prompt_appends_invoke_dev_with_verbatim(project):
    setup_stories(project, [entry("1", invoke_dev_with="Use Redis, not in-process memory.")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    prompt = engine._dev_prompt(task, None)
    assert prompt == (
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 1.\n"
        "Use Redis, not in-process memory."
    )


def test_dev_prompt_plan_halt_leg(project, monkeypatch):
    # the plan-halt branch is dormant in Phase 2 (returns False); force it on to
    # prove the seam emits the pinned `Halt after planning.` phrasing.
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_plan_halt_leg", lambda task, e: True)
    task = StoryTask(story_key="1", epic=0)
    assert engine._dev_prompt(task, None) == (
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 1. Halt after planning."
    )


def test_plan_halt_leg_true_for_fresh_spec_checkpoint(project):
    # leg 1: a spec_checkpoint story with no plan yet on disk halts after planning
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._plan_halt_leg(task, engine._entry_for(task)) is True
    assert "Halt after planning." in engine._dev_prompt(task, None)


def test_plan_halt_leg_false_once_plan_produced(project):
    # leg 2: once the plan exists at ready-for-dev, the dispatch is plain implement
    folder = setup_stories(project, [entry("1", spec_checkpoint=True)])
    write_spec(folder / "stories" / "1-slug.md", "ready-for-dev", rev_parse_head(project.project))
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._plan_halt_leg(task, engine._entry_for(task)) is False
    assert "Halt after planning" not in engine._dev_prompt(task, None)


def test_plan_halt_leg_false_without_spec_checkpoint(project):
    # a plain story never halts, even with no plan on disk
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._plan_halt_leg(task, engine._entry_for(task)) is False


def test_plan_halt_env_only_on_leg_one(project):
    # BMAD_LOOP_PLAN_HALT tracks _plan_halt_leg so the prompt + env never disagree
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._extra_session_env(task, "dev")["BMAD_LOOP_PLAN_HALT"] == "1"
    # review sessions never carry it
    assert "BMAD_LOOP_PLAN_HALT" not in engine._extra_session_env(task, "review")


def test_dev_prompt_repair_leg_is_explicit_spec_resume(project, tmp_path):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    task.spec_file = str(story_spec(project, "1"))
    # need the spec file present for _reset_spec_for_repair
    story_spec(project, "1").parent.mkdir(parents=True, exist_ok=True)
    write_spec(story_spec(project, "1"), "done", "abc")
    feedback = tmp_path / "fb.md"
    feedback.write_text("boom")
    prompt = engine._dev_prompt(task, feedback)
    assert prompt.startswith("/bmad-dev-auto Resume the autonomous dev session on the in-progress")
    assert "Story id:" not in prompt  # repair is an explicit-spec-file invocation


# ------------------------------------------------------------- other seams


def test_extra_session_env(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._extra_session_env(task, "dev") == {"BMAD_LOOP_SPEC_FOLDER": SPEC_FOLDER}


def test_post_dev_state_sync_is_noop(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    # no sprint board written, no exception
    engine._post_dev_state_sync(task, {"spec_file": "whatever"})
    assert not project.sprint_status.exists()


@pytest.mark.parametrize(
    "given,expected",
    [
        ("_bmad-output/epic-1", "_bmad-output/epic-1"),
        ("./_bmad-output/epic-1", "_bmad-output/epic-1"),
    ],
)
def test_relativize_keeps_relative(project, given, expected):
    engine, _ = make_engine(project, [], spec_folder=given)
    assert engine._spec_folder_rel == expected


def test_relativize_absolute_in_project_becomes_relative(project):
    abs_folder = str(project.project / "_bmad-output" / "epic-1")
    engine, _ = make_engine(project, [], spec_folder=abs_folder)
    assert engine._spec_folder_rel == "_bmad-output/epic-1"


# --------------------------------------------------------------- resume


def test_resume_rebuilds_stories_engine_from_persisted_state(project):
    """A crash mid-story persists source/spec_folder; resume rebuilds a
    StoriesEngine from run state (no flag) and drives the recorded dev result to
    DONE without re-running the dev session."""
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [stories_dev_effect()])
    original_emit = engine._emit

    def crashing_emit(stage, *a, **k):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *a, **k)

    engine._emit = crashing_emit
    assert engine.run().crashed

    saved = load_state(engine.run_dir)
    assert saved.source == "stories" and saved.spec_folder == SPEC_FOLDER
    assert saved.tasks["1"].phase == Phase.DEV_RUNNING

    resumed, adapter = resume_engine(project, engine, [])  # no new session needed
    summary = resumed.run()
    assert summary.done == 1 and not summary.crashed
    assert load_state(resumed.run_dir).tasks["1"].phase == Phase.DONE
    assert not any(s.role == "dev" for s in adapter.sessions)  # dev NOT re-run


# ----------------------------------------------------- HITL checkpoints (Phase 3)


def _kinds(journal, kind):
    return [e for e in journal.entries() if e.get("kind") == kind]


def test_plan_checkpoint_pause_then_resume_implements(project):
    """spec_checkpoint: leg 1 halts after planning (spec at ready-for-dev) and the
    run pauses at PAUSE_PLAN_CHECKPOINT; resume dispatches a plain implement leg
    that carries no plan-halt directive, drives the story to done, and commits."""
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    engine, adapter = make_engine(project, [stories_checkpoint_effect()])
    summary = engine.run()

    assert summary.paused and summary.done == 0
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_PLAN_CHECKPOINT
    assert persisted.paused_story_key == "1"
    task = persisted.tasks["1"]
    assert task.phase == Phase.DEV_VERIFY and task.plan_checkpoint_pending
    # leg 1 planned only — spec at ready-for-dev, no code, no commit
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "ready-for-dev"
    leg1 = next(s for s in adapter.sessions if s.role == "dev")
    assert leg1.prompt.endswith("Halt after planning.")
    assert leg1.env["BMAD_LOOP_PLAN_HALT"] == "1"
    assert _kinds(engine.journal, "plan-halt")
    assert _kinds(engine.journal, "checkpoint-pause")[-1]["checkpoint"] == "plan"

    resumed, radapter = resume_engine(project, engine, [stories_checkpoint_effect()])
    rsummary = resumed.run()
    assert rsummary.done == 1 and not rsummary.paused
    assert load_state(resumed.run_dir).tasks["1"].phase == Phase.DONE
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "done"
    # leg 2 is a plain implement dispatch — no halt directive, no plan-halt env
    leg2 = next(s for s in radapter.sessions if s.role == "dev")
    assert "Halt after planning" not in leg2.prompt
    assert "BMAD_LOOP_PLAN_HALT" not in leg2.env


def test_story_checkpoint_pause_after_commit(project):
    """done_checkpoint: the story commits, then the run pauses at
    PAUSE_STORY_CHECKPOINT because another story still remains to dispatch."""
    setup_stories(project, [entry("1", done_checkpoint=True), entry("2")])
    engine, _ = make_engine(project, [stories_dev_effect()])
    summary = engine.run()

    assert summary.paused and summary.done == 1
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_STORY_CHECKPOINT
    assert persisted.paused_story_key == "1"
    assert persisted.tasks["1"].phase == Phase.DONE  # committed before the pause
    assert "2" not in persisted.tasks  # story 2 not started yet
    pause = _kinds(engine.journal, "checkpoint-pause")
    assert pause and pause[-1]["checkpoint"] == "story"

    # resume drives story 2 to done (summary.done is cumulative over run state)
    resumed, _ = resume_engine(project, engine, [stories_dev_effect()])
    assert not resumed.run().paused
    assert load_state(resumed.run_dir).tasks["2"].phase == Phase.DONE


def test_story_checkpoint_skipped_when_last(project):
    """done_checkpoint on the final story does NOT pause — the run ends anyway, so
    there is nothing to come back to review before."""
    setup_stories(project, [entry("1"), entry("2", done_checkpoint=True)])
    engine, _ = make_engine(project, [stories_dev_effect(), stories_dev_effect()])
    summary = engine.run()

    assert summary.done == 2 and not summary.paused
    assert not load_state(engine.run_dir).paused
    assert _kinds(engine.journal, "checkpoint-skip-last")
    assert not _kinds(engine.journal, "checkpoint-pause")


def test_both_checkpoints_pause_twice(project):
    """A story with BOTH flags pauses at the plan checkpoint, then (after the
    resumed implement leg commits) again at the story checkpoint — two pauses for
    one story, because there is a later story still to dispatch."""
    setup_stories(project, [entry("1", spec_checkpoint=True, done_checkpoint=True), entry("2")])
    engine, _ = make_engine(project, [stories_checkpoint_effect()])
    # pause 1: plan checkpoint
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_PLAN_CHECKPOINT

    # pause 2: story checkpoint, after leg 2 implements + commits on resume
    resumed, _ = resume_engine(project, engine, [stories_checkpoint_effect()])
    s2 = resumed.run()
    assert s2.paused and s2.done == 1
    persisted = load_state(resumed.run_dir)
    assert persisted.paused_stage == PAUSE_STORY_CHECKPOINT
    assert persisted.tasks["1"].phase == Phase.DONE
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "done"

    # final resume drives story 2, no more pauses
    resumed2, _ = resume_engine(project, resumed, [stories_dev_effect()])
    assert not resumed2.run().paused
    assert load_state(resumed2.run_dir).tasks["2"].phase == Phase.DONE
