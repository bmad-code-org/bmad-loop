"""StoriesEngine (folder+id dispatch) scenario + seam tests against the mock
adapter — no tmux, no LLM. Mirrors test_engine.py / test_sweep.py conventions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from conftest import attach_profile, git, install_build_auto_skill, write_gated_ledger, write_spec

from bmad_loop import stories
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.install import (
    DEV_PRIMITIVE_NEW,
    STORIES_PROBE_FILE,
    STORIES_PROBE_TEXT,
    missing_stories_support,
)
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_CHECKPOINT,
    PAUSE_STORY_GATE,
    Phase,
    RunState,
    StoryTask,
    TokenUsage,
)
from bmad_loop.plugins import PluginRegistry
from bmad_loop.plugins.model import LoadedPlugin, PluginManifest, WorkflowSpec
from bmad_loop.policy import (
    GatesPolicy,
    NotifyPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
)
from bmad_loop.runs import STOP_REQUEST_FILE, graceful_stop_requested
from bmad_loop.stories_engine import StoriesEngine
from bmad_loop.verify import read_frontmatter, rev_parse_head, status_of, worktree_clean

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
    *,
    final_status: str = "done",
    followup_review: bool = False,
    prose_status: str | None = None,
    closes_deferred: object = None,
    write_src: bool = True,
    deferred=None,
):
    """Simulate a bmad-dev-auto folder+id dispatch: read the story id + spec
    folder from the session env (as the real adapter does), write the id-keyed
    story spec, and touch real code so proof-of-work passes.

    ``write_src=False`` skips the code change, so the proof-of-work gate fails and
    the story defers with its spec still finalized — the shape a story-declared
    ledger closure must survive without claiming anything resolved. ``deferred``
    writes the post-#2640 finding list into that spec."""

    def effect(spec) -> SessionResult:
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        baseline = rev_parse_head(Path(spec.cwd))
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        sp = stories_dir / f"{story_id}-slug.md"
        src = Path(spec.cwd) / "src.txt"
        if write_src:
            src.write_text(src.read_text() + f"work for {story_id}\n")
        write_spec(
            sp,
            final_status,
            baseline,
            prose_status=prose_status,
            closes_deferred=closes_deferred,
            deferred=deferred,
        )
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


def stories_checkpoint_effect(*, deferred=None):
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
            write_spec(sp, "ready-for-dev", baseline, deferred=deferred)
            return SessionResult(
                status="completed",
                result_json={**common, "status": "ready-for-dev", "plan_halt": True},
            )
        src = Path(spec.cwd) / "src.txt"
        src.write_text(src.read_text() + f"work for {story_id}\n")
        write_spec(sp, "done", baseline, deferred=deferred)
        return SessionResult(
            status="completed",
            result_json={**common, "status": "done", "followup_review_recommended": False},
        )

    return effect


def make_engine(project, script, *, policy=None, spec_folder=SPEC_FOLDER, **kwargs):
    """Mirrors `cli.cmd_run`: the launching scope (`--max-stories`, `--story`, `--epic`)
    is persisted on RunState as well as handed to the engine, because `resume` rebuilds
    the cap and filters from run state alone (cli._resume_paused_run). Seeding only the
    constructor made every resume test silently run uncapped/unfiltered (#84)."""
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(
        run_id="test-run",
        project=str(project.project),
        started_at="now",
        max_stories=kwargs.get("max_stories"),
        story_filter=kwargs.get("story_filter"),
        epic_filter=kwargs.get("epic_filter"),
    )
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


# ----------------------------------------------- dispatched spec ownership


def test_dispatched_spec_binding_uses_exact_present_id_keyed_file(project):
    folder = setup_stories(project, [entry("1")])
    present = story_spec(project, "1")
    write_spec(present, "ready-for-dev", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "present story spec")
    engine, _ = make_engine(project, [])

    assert engine._dispatched_spec_for_attempt(StoryTask("1", 0)) == str(present)
    assert present.parent == folder / "stories"


def test_bound_folder_id_dispatch_aborts_when_final_snapshot_fails(project, monkeypatch):
    """A Stories prompt stays fail-safe even though it names only folder + id.

    Ablation: require the final snapshot only for prompts containing ``spec_file``
    and this bound attempt launches despite its failed final observation.
    """
    setup_stories(project, [entry("1")])
    present = story_spec(project, "1")
    write_spec(present, "ready-for-dev", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "present story spec")
    engine, adapter = make_engine(project, [stories_dev_effect()])
    task = StoryTask("1", 0, spec_file=str(present))
    engine.state.tasks[task.story_key] = task
    real_refresh = engine._refresh_dispatched_spec_snapshot
    calls = 0

    def fail_final_snapshot(bound_task, **kwargs):
        nonlocal calls
        calls += 1
        refreshed = real_refresh(bound_task, **kwargs)
        if calls == 2:
            return False
        return refreshed

    monkeypatch.setattr(engine, "_refresh_dispatched_spec_snapshot", fail_final_snapshot)

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert calls == 2
    assert adapter.sessions == []


def test_present_folder_id_dispatch_aborts_when_initial_binding_fails(project, monkeypatch):
    """An existing Stories target cannot launch unbound after a read fault."""
    setup_stories(project, [entry("1")])
    present = story_spec(project, "1")
    write_spec(present, "ready-for-dev", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "present story spec")
    engine, adapter = make_engine(project, [stories_dev_effect()])
    task = StoryTask("1", 0, spec_file=str(present))
    engine.state.tasks[task.story_key] = task
    monkeypatch.setattr(engine, "_dispatched_spec_for_attempt", lambda _task: None)

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert task.phase == Phase.DEV_RUNNING
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    assert adapter.sessions == []
    persisted = load_state(engine.run_dir).tasks[task.story_key]
    assert persisted.phase == Phase.DEV_RUNNING
    assert persisted.attempt == task.attempt


def test_present_folder_id_dispatch_fails_closed_when_requirement_observation_faults(
    project, monkeypatch
):
    """Two failed resolver observations cannot turn an existing target unbound."""
    setup_stories(project, [entry("1")])
    present = story_spec(project, "1")
    write_spec(present, "ready-for-dev", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "present story spec")
    engine, adapter = make_engine(project, [stories_dev_effect()])
    task = StoryTask("1", 0, spec_file=str(present))
    engine.state.tasks[task.story_key] = task
    real_resolve = stories.resolve_story_spec
    calls = 0

    def fail_authority_observations(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            raise OSError("transient Stories resolver fault")
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(
        "bmad_loop.stories_engine.stories.resolve_story_spec", fail_authority_observations
    )

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert adapter.sessions == []
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    assert calls == 3
    persisted = load_state(engine.run_dir).tasks[task.story_key]
    assert persisted.phase == Phase.DEV_RUNNING
    assert persisted.attempt == task.attempt


def test_dispatched_spec_binding_refuses_pending_story(project):
    """Ablation: delete the StoriesEngine override and the inherited sprint seam
    binds the valid decoy ``task.spec_file``, failing this PENDING refusal alone."""
    folder = setup_stories(project, [entry("1")])
    task = StoryTask("1", 0, spec_file=str(folder / "SPEC.md"))
    engine, _ = make_engine(project, [])

    assert engine._dispatched_spec_for_attempt(task) is None


def test_dispatched_spec_binding_refuses_ambiguous_story(project):
    """Ablation: delete the StoriesEngine override and the inherited sprint seam
    binds the valid decoy ``task.spec_file``, failing this AMBIGUOUS refusal alone."""
    folder = setup_stories(project, [entry("1")])
    write_spec(story_spec(project, "1"), "ready-for-dev", rev_parse_head(project.project))
    write_spec(folder / "stories" / "1-other.md", "ready-for-dev", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "ambiguous story specs")
    task = StoryTask("1", 0, spec_file=str(folder / "SPEC.md"))
    engine, _ = make_engine(project, [])

    assert engine._dispatched_spec_for_attempt(task) is None


def test_dispatched_spec_binding_refuses_sentinel_story(project):
    """Ablation: delete the StoriesEngine override and the inherited sprint seam
    binds the valid decoy ``task.spec_file``, failing this SENTINEL refusal alone."""
    folder = setup_stories(project, [entry("1")])
    sentinel = folder / "stories" / "1-unresolved.md"
    write_spec(sentinel, "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sentinel story spec")
    task = StoryTask("1", 0, spec_file=str(folder / "SPEC.md"))
    engine, _ = make_engine(project, [])

    assert engine._dispatched_spec_for_attempt(task) is None


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


def test_bare_resume_repauses_inrun_escalation_with_resumable_spec(project):
    """A story that escalated AFTER a session ran (attempt > 0) can sit at a
    resumable spec status — e.g. a CRITICAL proof-of-work GitError fires only
    after the status gate already passed at in-review. A bare resume must
    re-pause on it (only `resolve` discharges an in-run escalation), never
    re-derive it from disk: the disk scan would re-dispatch a fresh StoryTask
    over the escalated one, destroying the escalation record and its
    resolved_redrive guard (a later exhaustion would then DEFER the
    human-resolved work)."""
    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "in-review", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 in-review")

    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    task.phase = Phase.ESCALATED
    task.attempt = 2
    task.resolved_redrive = True
    task.spec_file = str(folder / "stories" / "1-slug.md")
    engine.state.tasks["1"] = task
    engine._save()

    # bare resume WITHOUT resolving — sessions are available but none must run
    resumed, radapter = resume_engine(project, engine, [stories_dev_effect(), stories_dev_effect()])
    rsummary = resumed.run()
    assert rsummary.paused and rsummary.done == 0
    persisted = load_state(resumed.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    assert persisted.paused_story_key == "1"
    # the escalated task survives untouched — resolve still has its record
    survivor = persisted.tasks["1"]
    assert survivor.phase == Phase.ESCALATED
    assert survivor.attempt == 2
    assert survivor.resolved_redrive is True
    assert not any(s.role == "dev" for s in radapter.sessions)  # nothing re-dispatched


def test_bare_resume_does_not_leapfrog_inrun_escalation_at_done(project):
    """With review disabled a CRITICAL verify escalation can leave the spec at
    `done`; disk classification would count story 1 as complete and dispatch
    story 2 straight past the unresolved escalation. The in-run guard re-pauses
    on story 1 instead."""
    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "done", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 done on disk, escalation unresolved")

    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    task.phase = Phase.ESCALATED
    task.attempt = 1
    engine.state.tasks["1"] = task
    engine._save()

    resumed, radapter = resume_engine(project, engine, [stories_dev_effect()])
    rsummary = resumed.run()
    assert rsummary.paused and rsummary.done == 0
    persisted = load_state(resumed.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    assert persisted.paused_story_key == "1"  # re-paused on story 1, not story 2
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
    """Stories keeps its folder+id route instead of inheriting sprint routing.

    Ablation: route ``StoriesEngine._dev_prompt`` through its superclass and this
    exact assertion fails on the inherited bare sprint-key prompt.
    """
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


def test_review_prompt_carries_no_sprint_board_clause(project):
    """Stories mode has no sprint-status.yaml — `_post_dev_state_sync` is a no-op and
    `verify_review_stories` reads the id-keyed story spec alone — so the inherited
    review prompt's board-ownership clause would assert ownership of a file this mode
    never touches. One override empties it, and the `blocked` hand-back redirect goes
    with it, because the redirect gates itself on that clause.

    Asserted against the REVIEW prompt: `StoriesEngine` overrides `_dev_prompt`, so a
    dev-prompt assertion here would be dead. The base-class check is the ablation
    guard — without it every assertion below passes for free the moment the clause is
    emptied for ALL modes. And this is the live half of the review seam's
    `if tail else ""`: an unconditional separator leaves a trailing space here."""
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    spec = str(story_spec(project, "1"))
    task = StoryTask(story_key="1", epic=0, spec_file=spec)

    assert Engine._sprint_board_instruction(engine)  # the base clause is non-empty
    assert engine._sprint_board_instruction() == ""
    assert engine._board_handback_redirect() == ""

    prompt = engine._review_prompt(task)

    assert "sprint-status" not in prompt
    assert "status: blocked" not in prompt
    # byte-identical to the pre-#437 stories review prompt: the ledger sentence alone,
    # with no trailing space where the clauses would have joined
    assert prompt == (
        f"/bmad-dev-auto {spec} — do NOT modify, re-open, or rewrite existing "
        f"deferred-work ledger entries; the orchestrator owns their status and "
        f"resolution."
    )
    assert prompt.endswith("the orchestrator owns their status and resolution.")


def test_dev_prompt_repair_leg_is_explicit_spec_resume(project, tmp_path):
    """A Stories repair remains its pre-sprint-routing explicit-spec invocation.

    Green ablation: routing ``StoriesEngine._dev_prompt`` through its superclass
    leaves this test green because the two repair legs are intentionally
    byte-identical; ``test_dev_prompt_fresh_dispatch_shape`` grades the override.
    """
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
    assert prompt == (
        f"/bmad-dev-auto Resume the autonomous dev session on the in-progress spec at "
        f"`{task.spec_file}`. The previous session's work failed deterministic "
        f"verification; repair the working tree so verification passes without "
        f"changing the spec's frozen intent contract. Verification evidence is "
        f"in `{feedback}`."
    )


def test_dev_prompt_spells_the_post_rename_primitive(project, tmp_path, monkeypatch):
    """Every leg spells the primitive resolved from the dev adapter's skill tree,
    not a hardcoded name (upstream renamed it bmad-dev-auto -> bmad-build-auto,
    BMAD-METHOD #2651): fresh folder+id dispatch, its plan-halt tail, and the
    inherited repair leg.

    Both the installer and `attach_profile` are load-bearing. `project` installs no
    skills and `MockAdapter` carries no `profile`, so either one alone resolves the
    tree to None and falls back to the LEGACY name — which the tests above already
    pin, and which would make this one green for the wrong reason."""
    setup_stories(project, [entry("1")])
    install_build_auto_skill(project.project, ".claude/skills")
    engine, adapter = make_engine(project, [])
    attach_profile(adapter)  # claude -> .claude/skills
    task = StoryTask(story_key="1", epic=0)

    assert (
        engine._dev_prompt(task, None)
        == "/bmad-build-auto Spec folder: _bmad-output/epic-1. Story id: 1."
    )
    # the plan-halt tail rides the same f-string (third caller of the resolved name)
    monkeypatch.setattr(engine, "_plan_halt_leg", lambda task, e: True)
    assert engine._dev_prompt(task, None) == (
        "/bmad-build-auto Spec folder: _bmad-output/epic-1. Story id: 1. Halt after planning."
    )

    task.spec_file = str(story_spec(project, "1"))
    write_spec(story_spec(project, "1"), "done", "abc")  # _reset_spec_for_repair needs it
    feedback = tmp_path / "fb.md"
    feedback.write_text("boom")
    assert engine._dev_prompt(task, feedback).startswith(
        "/bmad-build-auto Resume the autonomous dev session on the in-progress"
    )


# ------------------------------------------------------------- other seams


def test_extra_session_env(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._extra_session_env(task, "dev") == {"BMAD_LOOP_SPEC_FOLDER": SPEC_FOLDER}


def test_extra_session_env_withheld_from_injected_workflow(project):
    # MAJOR-C: a labeled (injected plugin-workflow) session must NOT get the
    # story-spec env — only the primary dev/review session does.
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    assert engine._extra_session_env(task, "review", label="tea.gate") == {}
    assert engine._extra_session_env(task, "dev", label=None) == {
        "BMAD_LOOP_SPEC_FOLDER": SPEC_FOLDER
    }


def _workflow_capture(captured: list):
    def effect(spec) -> SessionResult:
        captured.append(spec)
        return SessionResult(status="completed", result_json={})

    return effect


def test_injected_workflow_session_does_not_leak_story_spec_env(project):
    """MAJOR-C: an injected pre_commit_gate workflow session in stories mode must
    not carry BMAD_LOOP_SPEC_FOLDER. Otherwise the generic adapter short-circuits
    to story-spec synthesis — at pre_commit_gate the spec is already `done`, so a
    gate session that did nothing would read `completed:done` and bypass the
    completion-marker + monotonic stall-nudge contract (the TEA-livelock fix). The
    primary dev session still gets the env for its id-keyed read-back."""
    setup_stories(project, [entry("1")])
    reg = PluginRegistry(
        [
            LoadedPlugin(
                manifest=PluginManifest(
                    name="tea",
                    api_version=1,
                    workflows=(
                        WorkflowSpec(
                            name="gate",
                            stage="pre_commit_gate",
                            role="review",
                            prompt="/gate {story_key}",
                            blocking=False,
                        ),
                    ),
                )
            )
        ]
    )
    captured: list = []
    engine, adapter = make_engine(
        project, [stories_dev_effect(), _workflow_capture(captured)], registry=reg
    )
    summary = engine.run()
    assert summary.done == 1

    # the gate session ran and did NOT get the story-spec short-circuit env
    assert len(captured) == 1
    gate = captured[0]
    assert "BMAD_LOOP_SPEC_FOLDER" not in gate.env
    assert "BMAD_LOOP_PLAN_HALT" not in gate.env
    # the primary dev session still carries it for id-keyed read-back
    dev = next(s for s in adapter.sessions if s.role == "dev" and "gate" not in s.task_id)
    assert dev.env["BMAD_LOOP_SPEC_FOLDER"] == SPEC_FOLDER


def test_injected_workflow_prompt_carries_no_board_section(project):
    """#437 phase 3: `_run_session` appends a `## Sprint board` section to every
    injected workflow prompt, gated on `_sprint_board_instruction`. This mode
    empties that clause (no board exists), so the section must disappear with it
    and the workflow prompt stays byte-identical to its pre-#437 shape: the
    plugin's own text joined straight to the completion contract.

    The base-class check is the ablation guard — without it every assertion here
    passes for free the moment the clause is emptied for ALL modes."""
    setup_stories(project, [entry("1")])
    reg = PluginRegistry(
        [
            LoadedPlugin(
                manifest=PluginManifest(
                    name="tea",
                    api_version=1,
                    workflows=(
                        WorkflowSpec(
                            name="gate",
                            stage="pre_commit_gate",
                            role="review",
                            prompt="/gate {story_key}",
                            blocking=False,
                        ),
                    ),
                )
            )
        ]
    )
    captured: list = []
    engine, _ = make_engine(
        project, [stories_dev_effect(), _workflow_capture(captured)], registry=reg
    )
    assert engine.run().done == 1
    assert len(captured) == 1
    prompt = captured[0].prompt

    assert Engine._sprint_board_instruction(engine)  # the base clause is non-empty
    assert engine._sprint_board_instruction() == ""
    assert "sprint-status" not in prompt
    assert "## Sprint board" not in prompt
    # junction literal: the plugin's text joins the completion contract directly,
    # with nothing (and no stray separator) between them
    assert prompt.startswith("/gate 1\n\n## Completion signal (required)")


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


def test_operator_spec_path_anchors_an_isolated_units_spec(project):
    """The pause notice is the FIRST surface an operator meets — before any dashboard.

    Every pause here hands a human a path and says "review it, then resume", and the
    `checkpoint-pause` journal records the same string. `task.spec_file` is persisted
    RELATIVE to the mounted worktree (`model._serialized_worktree_path`), so the raw
    value resolved against whatever directory the operator happened to be in — the main
    checkout, which carries the same `epic-1/stories/...` layout and answers with the
    wrong tree's copy. The TUI's `_paused_spec` carries a docstring about exactly this;
    the notification reaches the operator earlier and had none.

    NOT applied to the dev-session prompt at `spec_ref`: that session's cwd IS the
    mount, so the relative spelling is the correct one there. The anchor belongs to the
    consumer, not to the field.

    Ablation: change the helper's body to return `task.spec_file` and this reddens.
    NOTE this row grades the HELPER only — reverting a CALL SITE to a bare
    `task.spec_file` leaves it green, which is why
    `test_plan_checkpoint_pause_journals_the_mount_anchored_spec` below pins the
    journalled value that an actual pause emits.
    """
    engine, _adapter = make_engine(project, [])
    wt = project.project / ".bmad-loop" / "runs" / "test-run" / "worktrees" / "1"
    rel = "epic-1/stories/1-slug.md"
    task = StoryTask("1", 0, spec_file=rel)
    task.worktree_path = str(wt)

    assert engine._operator_spec_path(task) == str(wt / rel)
    # a spec-less task falls back to the story key rather than raising out of a notice
    assert engine._operator_spec_path(StoryTask("1", 0)) == "1"


def test_plan_checkpoint_pause_journals_the_mount_anchored_spec(project):
    """The CALL SITE, not the helper — the two can regress independently.

    `test_operator_spec_path_anchors_an_isolated_units_spec` calls the helper directly,
    so every one of the five notification/journal sites could be reverted to a bare
    `task.spec_file` with the whole suite still green: the stories engine builds its
    policy with `notify=QUIET`, so no row observes a notification body, and every other
    `checkpoint-pause` assertion reads `checkpoint` and never `spec`.

    This pins the value an actual pause emits, which is also the premise
    `diagnostics.py` now records for the `spec` alias field.

    Ablation: revert `_pause_plan_checkpoint`'s `spec=` to `task.spec_file` and this
    reddens on the bare relative spelling.
    """
    from bmad_loop.engine import RunPaused

    engine, _adapter = make_engine(project, [])
    wt = project.project / ".bmad-loop" / "runs" / "test-run" / "worktrees" / "1"
    rel = "epic-1/stories/1-slug.md"
    task = StoryTask("1", 0, spec_file=rel)
    task.worktree_path = str(wt)
    engine.state.tasks["1"] = task

    with pytest.raises(RunPaused):
        engine._pause_plan_checkpoint(task)

    record = _kinds(engine.journal, "checkpoint-pause")[-1]
    assert record["spec"] == str(wt / rel)
    assert record["checkpoint"] == "plan"

    # The NOTIFY body, not only the journal: they are separate call sites that regress
    # independently, and this one is the surface the operator actually reads. `QUIET` is
    # `NotifyPolicy(desktop=False, file=True)`, so the ATTENTION file is written. Before
    # this assertion no row in the repo observed ANY `gates.notify` body, so every
    # notification site could be reverted to a bare `task.spec_file` with the suite green.
    #
    # Ablation: revert `_pause_plan_checkpoint`'s notify to `task.spec_file` and this
    # reddens — the bare relpath appears and the anchored path does not.
    attention = (engine.run_dir / "ATTENTION").read_text(encoding="utf-8")
    assert str(wt / rel) in attention
    assert f"review {rel}," not in attention  # not the un-anchored spelling


# -------- MAJOR-B: a spec_checkpoint story can never commit without a plan review


def _write_story_spec_effect(status: str, *, touch_code: bool, result_over: dict | None = None):
    """A dev effect that writes the id-keyed story spec at ``status`` (optionally
    touching real code) and returns a completed result, with ``result_over`` merged
    into result.json. Used to script the three ways a plan review gets bypassed."""

    def effect(spec) -> SessionResult:
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        baseline = rev_parse_head(Path(spec.cwd))
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        sp = stories_dir / f"{story_id}-slug.md"
        if touch_code:
            src = Path(spec.cwd) / "src.txt"
            src.write_text(src.read_text() + f"work for {story_id}\n")
        write_spec(sp, status, baseline)
        result = {
            "workflow": "auto-dev",
            "story_key": story_id,
            "spec_file": str(sp),
            "baseline_commit": baseline,
            "escalations": [],
        }
        result.update(result_over or {})
        return SessionResult(status="completed", result_json=result)

    return effect


def test_plan_review_owed_survives_crash_before_durable_record(project):
    """MAJOR-B(a): the plan-halt leg wrote the plan (ready-for-dev) but the host
    died before the durable session record. plan_review_owed is latched + saved
    BEFORE the session runs, so it survives the crash; on resume the on-disk plan
    makes the re-drive an implement leg, but the run pauses for the owed plan review
    before committing instead of silently implementing past the checkpoint."""
    setup_stories(project, [entry("1", spec_checkpoint=True)])

    def plan_then_die(spec):
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        baseline = rev_parse_head(Path(spec.cwd))
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        write_spec(stories_dir / f"{story_id}-slug.md", "ready-for-dev", baseline)
        raise RuntimeError("host died after the plan was written, before the durable record")

    engine, _ = make_engine(project, [plan_then_die])
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1"]
    assert crashed.phase == Phase.DEV_RUNNING and crashed.plan_review_owed
    # the plan survives the crash (spec folder is under output_folder, rollback-kept)
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "ready-for-dev"

    # resume: the implement leg runs (session available) but must pause, not commit
    resumed, _ = resume_engine(project, engine, [stories_dev_effect()])
    rsummary = resumed.run()
    assert rsummary.paused and rsummary.done == 0
    persisted = load_state(resumed.run_dir)
    assert persisted.paused_stage == PAUSE_PLAN_CHECKPOINT
    task = persisted.tasks["1"]
    assert not task.plan_review_owed  # discharged at the pause
    assert task.commit_sha is None  # never committed un-reviewed


def test_resume_dev_verify_replay_stories_mode(project):
    """Stories-mode parity for the #100 resume arm: a story persisted at
    DEV_VERIFY without a verified spec (verify failed, then the host died mid
    retry-reset) replays its completed dev record instead of resume-restart.
    After the operator repaired the story spec, the replay verifies green and
    commits — no session re-run, no attempt budget burned."""
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [stories_dev_effect(final_status="in-progress")])

    with pytest.MonkeyPatch.context() as mp:

        def boom(*args, **kwargs):
            raise RuntimeError("host died during the retry reset")

        mp.setattr("bmad_loop.verify.safe_rollback", boom)
        assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1"]
    assert crashed.phase == Phase.DEV_VERIFY
    assert not crashed.spec_file  # verify had not passed — the #100 shape
    assert crashed.sessions[-1].result_json is not None

    # the operator repaired the story spec before resuming
    write_spec(story_spec(project, "1"), "done", crashed.baseline_commit)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1"]
    assert final.phase == Phase.DONE
    assert final.attempt == 1  # the replay burned no attempt budget
    assert adapter.sessions == []  # the dev session was not re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds


def test_plan_review_owed_after_non_fixable_retry_becomes_implement_leg(project):
    """MAJOR-B(b): leg 1 plans (ready-for-dev) but fails verify non-fixably (wrong
    workflow tag), so the tree resets and attempt 2 re-dispatches. The plan survived
    (rollback-kept), so attempt 2 is an implement leg — which must still pause for
    the owed plan review rather than drive straight to a commit."""
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    # attempt 1: a plan-halt leg that writes the plan but claims the wrong workflow →
    # verify_dev_stories retries (non-fixable); attempt 2: a clean implement leg.
    attempt1 = _write_story_spec_effect(
        "ready-for-dev", touch_code=False, result_over={"workflow": "quick-dev", "plan_halt": True}
    )
    attempt2 = _write_story_spec_effect("done", touch_code=True, result_over={"status": "done"})
    engine, adapter = make_engine(project, [attempt1, attempt2])
    summary = engine.run()

    assert summary.paused and summary.done == 0
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_PLAN_CHECKPOINT
    assert not persisted.tasks["1"].plan_review_owed  # discharged at the pause
    assert persisted.tasks["1"].commit_sha is None  # not committed un-reviewed
    # attempt 2 really was an implement leg (no halt directive), yet it still paused
    assert len([s for s in adapter.sessions if s.role == "dev"]) == 2
    assert "Halt after planning" not in adapter.sessions[-1].prompt


def test_plan_review_owed_when_skill_overruns_halt_to_done(project):
    """MAJOR-B(c): the skill ignores `Halt after planning.` and drives leg 1 all the
    way to done (result carries no plan_halt marker). The obligation latched at
    dispatch forces a pause before commit, so the story cannot commit without the
    human ever reviewing the plan."""
    setup_stories(project, [entry("1", spec_checkpoint=True)])
    overrun = _write_story_spec_effect("done", touch_code=True, result_over={"status": "done"})
    engine, _ = make_engine(project, [overrun])
    summary = engine.run()

    assert summary.paused and summary.done == 0
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_PLAN_CHECKPOINT
    task = persisted.tasks["1"]
    assert not task.plan_review_owed  # discharged at the pause
    assert task.commit_sha is None  # not committed un-reviewed
    # the pause is the distinct "owed after implement" variant, not the clean leg-1 halt
    owed = [e for e in engine.journal.entries() if e.get("owed_after_implement")]
    assert owed and owed[-1]["story_key"] == "1"


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


def test_story_checkpoint_still_fires_when_manifest_unreadable_after_commit(project):
    """A manifest that goes unreadable between the commit and the after-story
    check makes the done_checkpoint flag unknowable — the conservative default is
    to pause for review (mirroring _schedule_complete), never to silently drop a
    checkpoint the manifest may set. The run cannot proceed past the broken
    manifest anyway; only the human review could be lost by skipping."""
    setup_stories(project, [entry("1", done_checkpoint=True), entry("2")])

    def corrupting_effect(spec) -> SessionResult:
        result = stories_dev_effect()(spec)
        # the session's tree (== project: no isolation in this harness) ends up
        # with an undecodable stories.yaml right before commit/after-story
        (Path(spec.cwd) / SPEC_FOLDER / "stories.yaml").write_bytes(b"\xff\xfe not yaml \x80")
        return result

    engine, _ = make_engine(project, [corrupting_effect])
    summary = engine.run()
    assert summary.paused and summary.done == 1
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_STORY_CHECKPOINT
    assert persisted.paused_story_key == "1"
    unreadable = _kinds(engine.journal, "stories-manifest-unreadable")
    assert unreadable and unreadable[-1]["story_key"] == "1"


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


def test_done_checkpoint_skipped_when_graceful_stop_pending(project):
    """A pending graceful stop turns a done_checkpoint into a skip, not a pause: the
    loop-head check ends the run `stopped` on the next iteration, so pausing here
    would strand it `paused` instead. The skip is tagged reason=graceful-stop, and
    the still-pending story is never dispatched."""
    setup_stories(project, [entry("1", done_checkpoint=True), entry("2")])
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"

    def dev_then_request_stop(spec) -> SessionResult:
        result = stories_dev_effect()(spec)
        (run_dir / STOP_REQUEST_FILE).write_text(
            '{"requested_at": "2026-07-20T00:00:00", "mode": "graceful"}', encoding="utf-8"
        )
        return result

    engine, _ = make_engine(project, [dev_then_request_stop])
    summary = engine.run()

    persisted = load_state(engine.run_dir)
    assert summary.done == 1 and not summary.paused
    assert persisted.stopped and not persisted.paused  # stop wins over the checkpoint pause
    assert persisted.tasks["1"].phase == Phase.DONE
    assert "2" not in persisted.tasks  # story 2 never dispatched
    skips = _kinds(engine.journal, "checkpoint-skip-last")
    assert skips and skips[-1]["reason"] == "graceful-stop"
    assert not _kinds(engine.journal, "checkpoint-pause")
    assert not graceful_stop_requested(run_dir)  # consumed at the loop head
    stops = _kinds(engine.journal, "run-stop")
    assert stops and stops[-1]["graceful"] is True


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


# ------------------------------------ blocked → resolve → re-dispatch (E2E)


def test_blocked_resolve_rearm_then_redispatch_to_done(project):
    """Scenario 4: a blocked story stops the run; re-arm (as `resolve
    --no-interactive` does) flips it back to ready-for-dev + strips the stale
    Auto Run Result, and the resumed run re-dispatches it through to done — the
    end-to-end path the pause-only tests above leave un-stitched."""
    from bmad_loop import runs

    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 blocked")

    engine, adapter = make_engine(project, [])
    assert engine.run().paused
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION and persisted.paused_story_key == "1"
    assert not any(s.role == "dev" for s in adapter.sessions)  # story 2 not leapfrogged

    # human fixed the frozen spec → re-arm (must run while still escalation-paused)
    runs.rearm_escalation(engine.run_dir, "1")
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "ready-for-dev"

    # resume re-drives the re-armed story, then continues the schedule to story 2
    resumed, radapter = resume_engine(project, engine, [stories_dev_effect(), stories_dev_effect()])
    rsummary = resumed.run()
    assert rsummary.done == 2 and not rsummary.paused
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "done"
    assert status_of(read_frontmatter(story_spec(project, "2"))) == "done"
    dev_prompts = [s.prompt for s in radapter.sessions if s.role == "dev"]
    assert dev_prompts == [
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 1.",
        "/bmad-dev-auto Spec folder: _bmad-output/epic-1. Story id: 2.",
    ]


def test_resolved_wedge_is_still_gated_on_redispatch(project):
    """The state that makes a "has this story ever run?" test unbuildable, and so
    the reason `_finish_inflight`'s restart arm asks the gate unconditionally.

    `_pause_wedged` records an ESCALATED task *before any session runs this pick*:
    `attempt == 0`, no session records, and after `resolve` also `rearmed`. Every
    signal that would exempt a re-drive is therefore set on a story whose first
    dispatch has not happened — so exempting re-drives would wave a wedged story
    straight past a gate that landed while the run was down. Story 1's re-dispatch
    is a start like any other, and story 2 must not be leapfrogged either: the gate
    pauses the run rather than skipping the story, exactly as `validate` fails the
    whole preflight."""
    from bmad_loop import runs

    folder = setup_stories(project, [entry("1"), entry("2")])
    write_spec(folder / "stories" / "1-slug.md", "blocked", rev_parse_head(project.project))
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story 1 blocked")

    engine, _ = make_engine(project, [])
    assert engine.run().paused
    wedged = load_state(engine.run_dir).tasks["1"]
    assert wedged.phase == Phase.ESCALATED and wedged.attempt == 0 and not wedged.sessions

    runs.rearm_escalation(engine.run_dir, "1")  # human fixed the frozen spec
    assert load_state(engine.run_dir).tasks["1"].rearmed  # ...and the re-drive is armed
    # a gate on story 1 lands while the run is down
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1"])})

    resumed, radapter = resume_engine(project, engine, [stories_dev_effect(), stories_dev_effect()])
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert radapter.sessions == []
    assert load_state(resumed.run_dir).paused_stage == PAUSE_STORY_GATE


def test_sentinel_rearm_deletes_by_recorded_verdict_e2e(project):
    """C2 (E2E): a pick-time sentinel wedge records task.sentinel_kind on disk; a
    subsequent rearm clears the sentinel by that recorded verdict (not the basename)
    — preserving a copy, deleting the sentinel, and re-dispatching clean. Proves the
    detection→rearm path end-to-end through the engine, not just the isolated
    runs.rearm_escalation unit test."""
    from bmad_loop import runs

    folder = setup_stories(project, [entry("1"), entry("2")])
    sentinel = folder / "stories" / "1-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sentinel")

    engine, _ = make_engine(project, [])
    assert engine.run().paused
    assert load_state(engine.run_dir).tasks["1"].sentinel_kind == "unresolved"  # recorded

    runs.rearm_escalation(engine.run_dir, "1")
    assert not sentinel.exists()  # cleared by the recorded verdict
    assert (engine.run_dir / "sentinels" / "1-unresolved.md").is_file()  # copy preserved
    reloaded = load_state(engine.run_dir)
    assert reloaded.tasks["1"].spec_file is None  # re-dispatch resolves PENDING
    assert reloaded.tasks["1"].sentinel_kind == ""  # verdict discharged

    # resumed run re-plans story 1 from scratch, then continues to story 2
    resumed, _ = resume_engine(project, engine, [stories_dev_effect(), stories_dev_effect()])
    assert resumed.run().done == 2


# ----------------------------------------------- worktree isolation (E2E)


def test_worktree_isolation_two_stories(project):
    """Scenario 5: StoriesEngine inherits worktree isolation unchanged — each
    story runs in its own worktree (spec.cwd) and merges back to the target
    branch, leaving the main checkout clean with both story specs done."""
    from bmad_loop.verify import worktree_clean

    setup_stories(project, [entry("1"), entry("2")])
    engine, adapter = make_engine(
        project,
        [stories_dev_effect(), stories_dev_effect()],
        policy=_stories_policy(scm=ScmPolicy(isolation="worktree")),
    )
    summary = engine.run()

    assert summary.done == 2 and not summary.paused
    assert worktree_clean(project.project)  # unit worktrees merged back + torn down
    # sessions ran inside a worktree checkout, not the project root
    dev = [s for s in adapter.sessions if s.role == "dev"]
    assert dev and all(Path(s.cwd) != project.project for s in dev)
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "done"
    assert status_of(read_frontmatter(story_spec(project, "2"))) == "done"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("checkout_probe", ["missing", "stale"])
def test_worktree_reprobes_stories_dispatch_support_before_session(
    project, tmp_path, checkout_probe
):
    """The main checkout's valid through-link cannot alibi a short worktree router."""
    setup_stories(project, [entry("1")])
    tree = ".claude/skills"
    skills = install_build_auto_skill(project.project, tree, folder_id=True)
    probe = skills / DEV_PRIMITIVE_NEW / STORIES_PROBE_FILE
    if checkout_probe == "stale":
        probe.write_text("old router without the required protocol\n", encoding="utf-8")
        git(
            project.project,
            "add",
            "-f",
            str(probe.relative_to(project.project)),
        )
        git(project.project, "commit", "-q", "-m", "tracked stale stories router")
    probe.unlink()
    shared = tmp_path / STORIES_PROBE_FILE
    shared.write_text(f"This is a **{STORIES_PROBE_TEXT}** router.\n", encoding="utf-8")
    probe.symlink_to(shared)

    assert missing_stories_support(project.project, [tree]) == []
    engine, adapter = make_engine(
        project,
        [stories_dev_effect()],
        policy=_stories_policy(scm=ScmPolicy(isolation="worktree")),
    )
    attach_profile(adapter)

    summary = engine.run()

    assert summary.paused and adapter.sessions == []
    reason = engine.state.paused_reason or ""
    assert STORIES_PROBE_FILE in reason
    assert (STORIES_PROBE_TEXT in reason) is (checkout_probe == "stale")
    assert Path(engine.state.tasks["1"].worktree_path).is_dir()


# ---------------------- item 10: MINOR/NOTE batch (Session 3) ----------------


def test_gate_and_spec_checkpoint_pause_additively(project):
    """MINOR-4: a spec_checkpoint story under gates.mode=per-story-spec-approval
    pauses TWICE — first at the plan checkpoint (before code), then, after the
    resumed implement leg, at the run-global spec-approval gate. The per-story
    checkpoint does not substitute for the run-global gate; they stack."""
    setup_stories(project, [entry("1", spec_checkpoint=True), entry("2")])
    pol = _stories_policy(gates=GatesPolicy(mode="per-story-spec-approval"))
    engine, _ = make_engine(project, [stories_checkpoint_effect()], policy=pol)

    # pause 1: plan checkpoint (leg 1 halted after planning, no code yet)
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_PLAN_CHECKPOINT

    # pause 2: the run-global spec-approval gate, after the implement leg
    resumed, _ = resume_engine(project, engine, [stories_checkpoint_effect()])
    assert resumed.run().paused
    assert load_state(resumed.run_dir).paused_stage == PAUSE_SPEC_APPROVAL
    assert load_state(resumed.run_dir).tasks["1"].phase != Phase.DONE  # not committed yet

    # approve the spec gate → story 1 commits (story 2 then pauses at its own gate)
    resumed2, _ = resume_engine(project, resumed, [stories_dev_effect()])
    resumed2.run()
    assert load_state(resumed2.run_dir).tasks["1"].phase == Phase.DONE
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "done"


def test_unknown_story_selector_pauses_not_crashes(project):
    """MINOR-E: a --story id absent from the manifest pauses for resolve (fix the
    id/manifest, resume) instead of crashing the run in the scheduler."""
    setup_stories(project, [entry("1"), entry("2")])
    engine, adapter = make_engine(project, [], story_filter="99")
    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_ESCALATION
    assert persisted.paused_story_key == "99"
    assert persisted.tasks["99"].phase == Phase.ESCALATED
    assert not any(s.role == "dev" for s in adapter.sessions)  # nothing dispatched
    assert _kinds(engine.journal, "stories-selector-unknown")


def test_done_checkpoint_skipped_at_max_stories(project):
    """MINOR-F: with --max-stories=1 a done_checkpoint on the only dispatched story
    is SKIPPED (the bound ends the run here) — otherwise the pause+resume would
    reset the loop counter and leapfrog story 2 past the cap."""
    setup_stories(project, [entry("1", done_checkpoint=True), entry("2")])
    engine, _ = make_engine(project, [stories_dev_effect()], max_stories=1)
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert _kinds(engine.journal, "checkpoint-skip-last")
    assert not _kinds(engine.journal, "checkpoint-pause")
    assert "2" not in load_state(engine.run_dir).tasks  # capped, story 2 never dispatched


def test_max_stories_survives_a_checkpoint_pause_resume(project):
    """A5: with --max-stories=2 and a done_checkpoint on story 1 (below the cap), the
    run pauses at story 1's checkpoint, then a resume dispatches ONLY story 2 — never
    leapfrogs to story 3. The dispatch gate consults durable run state, not a
    _loop-local counter that resets to 0 on resume (which would let the cap be
    exceeded)."""
    setup_stories(project, [entry("1", done_checkpoint=True), entry("2"), entry("3")])
    engine, _ = make_engine(project, [stories_dev_effect()], max_stories=2)

    # run 1: story 1 dispatched + committed, then pauses at its done_checkpoint
    # (dispatched=1 < cap=2, more stories pending → not skip-if-last)
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_STORY_CHECKPOINT
    assert _kinds(engine.journal, "checkpoint-pause")

    # resume: dispatch story 2 (dispatched reaches the cap), then stop — story 3
    # must NOT be dispatched despite the resume resetting any local counter
    resumed, _ = resume_engine(project, engine, [stories_dev_effect()])
    summary = resumed.run()

    final = load_state(resumed.run_dir)
    assert not summary.paused
    assert set(final.tasks) == {"1", "2"}  # story 3 never dispatched — cap honored
    assert final.tasks["1"].phase == Phase.DONE and final.tasks["2"].phase == Phase.DONE
    assert _kinds(resumed.journal, "max-stories-reached")


def test_make_engine_persists_the_launching_scope_for_resume(project):
    """#84: the launching scope must survive the state round-trip with no per-test
    seeding — a `make_engine` that configured only the constructor let every resume
    test above silently run uncapped/unfiltered, masking exactly the durability
    regressions they exist to catch."""
    setup_stories(project, [entry("1"), entry("2")])
    engine, _ = make_engine(project, [], max_stories=2, story_filter="1", epic_filter=7)
    save_state(engine.run_dir, engine.state)  # the engine's own _save, ahead of a run

    reloaded = load_state(engine.run_dir)  # what `resume` actually reads back
    assert reloaded.max_stories == 2
    assert reloaded.story_filter == "1"
    assert reloaded.epic_filter == 7

    resumed, _ = resume_engine(project, engine, [])  # no manual seeding anywhere
    assert resumed.state.max_stories == 2
    # construction must not clobber the durable scope: StoriesEngine nulls the
    # story_filter/epic_filter *constructor kwargs* (a flat list has no E-S refs),
    # and the base Engine parks them on itself — never back onto RunState.
    assert resumed.state.story_filter == "1"
    assert resumed.state.epic_filter == 7
    # `--story` instead drives StoriesEngine's own id filter, scanned at pick time
    assert resumed._story_id_filter == "1"


def test_sentinel_detected_journaled_at_pick(project):
    """MINOR-6: a fixed-slug sentinel found by the pick-time read-back journals a
    distinct sentinel-detected event carrying its recorded blocking condition, not
    only the later stories-wedged / escalation trace."""
    folder = setup_stories(project, [entry("1")])
    (folder / "stories" / "1-unresolved.md").write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sentinel")
    engine, _ = make_engine(project, [])
    assert engine.run().paused

    detected = _kinds(engine.journal, "sentinel-detected")
    assert detected and detected[-1]["story_key"] == "1"
    assert detected[-1]["sentinel_kind"] == "unresolved"
    assert "intent too vague" in detected[-1]["condition"]
    # C2: the pick-time wedge records the sentinel verdict on the task so a later
    # rearm clears it by recorded kind, not by re-deriving from the basename.
    assert load_state(engine.run_dir).tasks["1"].sentinel_kind == "unresolved"


def test_sentinel_detected_tolerates_non_utf8_sentinel(project):
    """Bug class: a sentinel classified by NAME can still hold non-UTF-8 bytes, so
    _journal_sentinel_detected's blocking-condition read must tolerate a
    UnicodeDecodeError — the engine still pauses on the wedge and records the
    sentinel with an empty condition instead of crashing the pick."""
    folder = setup_stories(project, [entry("1")])
    (folder / "stories" / "1-unresolved.md").write_bytes(b"\xff\xfe\x00\x01 \x80\x81")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sentinel")
    engine, _ = make_engine(project, [])
    assert engine.run().paused  # must not raise on the undecodable sentinel

    detected = _kinds(engine.journal, "sentinel-detected")
    assert detected and detected[-1]["sentinel_kind"] == "unresolved"
    assert detected[-1]["condition"] == ""  # unreadable → empty, not a crash
    assert load_state(engine.run_dir).tasks["1"].sentinel_kind == "unresolved"


def test_sentinel_detected_journaled_at_readback(project):
    """MINOR-6: the just-run dev session HALTs pre-planning and writes a sentinel;
    the post-dev read-back journals sentinel-detected before the escalation."""
    setup_stories(project, [entry("1")])

    def sentinel_effect(spec) -> SessionResult:
        story_id = spec.env["BMAD_LOOP_STORY_KEY"]
        rel = spec.env["BMAD_LOOP_SPEC_FOLDER"]
        stories_dir = Path(spec.cwd) / rel / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        (stories_dir / f"{story_id}-unresolved.md").write_text(
            "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\ntoo vague\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_id,
                "status": "blocked",
                "escalations": [
                    {"type": "spec-gap", "severity": "CRITICAL", "detail": "too vague"}
                ],
            },
        )

    engine, _ = make_engine(project, [sentinel_effect])
    engine.run()

    detected = _kinds(engine.journal, "sentinel-detected")
    assert detected and detected[-1]["sentinel_kind"] == "unresolved"
    assert "too vague" in detected[-1]["condition"]
    # C2: the post-dev read-back also records the sentinel verdict on the task.
    assert load_state(engine.run_dir).tasks["1"].sentinel_kind == "unresolved"


def test_entry_for_unreadable_manifest_journals_warning_once(project):
    """NOTE-10: _entry_for swallows a hand-broken manifest (bare dispatch still
    runs) but now leaves a one-time stories-manifest-unreadable trace per story."""
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1", epic=0)
    (project.project / SPEC_FOLDER / "stories.yaml").write_text("[unclosed", encoding="utf-8")

    assert engine._entry_for(task) is None
    warned = _kinds(engine.journal, "stories-manifest-unreadable")
    assert warned and warned[-1]["story_key"] == "1"
    # a second call for the same story does not re-journal (dedup per story key)
    assert engine._entry_for(task) is None
    assert len(_kinds(engine.journal, "stories-manifest-unreadable")) == 1


# ------------------- closes_deferred auto-resolve in stories mode (#234) ------


def _entries(project) -> dict:
    from bmad_loop import deferredwork

    text = project.deferred_work.read_text(encoding="utf-8")
    return {e.id: e for e in deferredwork.parse_ledger(text)}


def _closing_run(project, *, manifest=None, spec=None, ledger=None, attempts=1, **effect_kwargs):
    """A whole stories-mode run of one story, declaring `manifest` ids on its
    stories.yaml entry and `spec` ids in the generated story spec, over a
    committed ledger. Driven end to end: the close lives at the commit boundary,
    so only a real run exercises where it actually lands. `attempts` scripts the
    same session repeatedly for a story expected to retry before deferring."""
    from conftest import write_ledger

    setup_stories(project, [entry("1", **({"closes_deferred": manifest} if manifest else {}))])
    write_ledger(project, ledger or {"DW-1": "open"})
    effect = stories_dev_effect(closes_deferred=spec, **effect_kwargs)
    return make_engine(project, [effect] * attempts)[0]


def test_stories_mode_closes_declared_deferred_entries(project):
    """#234: stories mode has no sprint board, but it shares the project's
    deferred-work ledger — so a story declaring `closes_deferred:` must still get
    those entries annotated. Without this the field is inert in exactly the mode
    whose declarations `validate` preflights."""
    engine = _closing_run(project, spec=["DW-1"])

    summary = engine.run()

    assert summary.done == 1
    dw1 = _entries(project)["DW-1"]
    assert dw1.status.startswith("done") and not dw1.open
    assert "resolution: resolved by story 1" in dw1.body
    closed = _kinds(engine.journal, "story-deferred-closed")
    assert len(closed) == 1 and closed[0]["dw_ids"] == ["DW-1"]


def test_stories_mode_annotation_rides_the_story_commit(project):
    """An in-repo ledger annotation is part of the story's own commit, so the run
    ends on the clean tree the next story's step-01 requires."""
    engine = _closing_run(project, spec=["DW-1"])

    engine.run()

    committed = git(project.project, "show", "HEAD", "--", str(project.deferred_work))
    assert "+resolution: resolved by story 1" in committed
    assert worktree_clean(project.project)


def test_stories_mode_closes_nothing_when_the_story_defers(project):
    """The story finalizes its spec — declaration and all — then fails the
    proof-of-work gate and defers. Closing at dev-sync time made that permanent
    (#234 review, finding 1); at the commit boundary the entry is never touched."""
    engine = _closing_run(project, manifest=["DW-1"], spec=["DW-1"], write_src=False, attempts=3)

    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert _entries(project)["DW-1"].open
    assert not _kinds(engine.journal, "story-deferred-closed")


def test_stories_mode_close_is_silent_without_declaration(project):
    """The ordinary story declares nothing: no ledger write, no journal noise —
    and the sprint board stays untouched in a mode that has none."""
    engine = _closing_run(project)
    before = project.deferred_work.read_bytes()

    engine.run()

    assert project.deferred_work.read_bytes() == before
    assert not _kinds(engine.journal, "story-deferred-closed")
    assert not _kinds(engine.journal, "deferred-close-unmatched")


def test_stories_mode_dev_sync_writes_nothing(project):
    """Stories mode keeps the contract's "the orchestrator writes nothing" at dev
    time: the post-dev sync is a pure no-op, and in particular does NOT close
    declared entries — that belongs after verification, at commit."""
    engine = _closing_run(project, manifest=["DW-1"], spec=["DW-1"])
    before = project.deferred_work.read_bytes()

    engine._post_dev_state_sync(StoryTask(story_key="1", epic=0), {})

    assert project.deferred_work.read_bytes() == before
    assert not _kinds(engine.journal, "story-deferred-closed")


def test_stories_mode_closes_ids_declared_in_the_manifest(project):
    """The channel that makes this work unattended (#234): `bmad-dev-auto` writes
    the story spec and knows nothing of the ledger, so a frontmatter-only
    declaration would have to be hand-edited into every generated spec. Declaring
    on the stories.yaml entry — authored while the ledger is in view — closes the
    loop with no upstream change."""
    engine = _closing_run(project, manifest=["DW-1"])  # spec declares nothing

    engine.run()

    dw1 = _entries(project)["DW-1"]
    assert dw1.status.startswith("done") and "resolution: resolved by story 1" in dw1.body
    assert _kinds(engine.journal, "story-deferred-closed")[0]["dw_ids"] == ["DW-1"]


def test_stories_mode_unreadable_manifest_at_commit_does_not_crash_the_run(project, monkeypatch):
    """`_manifest_closes_deferred` advertises a fallback — journal the unreadable
    manifest, carry on with the spec channel — but it catches only StoriesError,
    and `load_stories` let an OSError from the read escape raw. A commit-time
    permission fault therefore crashed the whole run instead of costing one
    channel (#284 round-5 review, finding 5).

    The fault is transient and scoped to the close, which is the shape that
    matters: a manifest unreadable for the whole run is a preflight failure, not
    this."""
    engine = _closing_run(project, spec=["DW-1"])
    manifest = project.project / SPEC_FOLDER / "stories.yaml"
    real_read = Path.read_text
    faulted = {"on": False}

    def maybe_fault(self, *a, **kw):
        if faulted["on"] and self == manifest:
            raise PermissionError(13, "Permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", maybe_fault)
    finalize = engine._finalize_commit_phase

    def unreadable_across_the_close(task):
        faulted["on"] = True
        try:
            return finalize(task)
        finally:
            faulted["on"] = False

    monkeypatch.setattr(engine, "_finalize_commit_phase", unreadable_across_the_close)

    summary = engine.run()

    assert summary.done == 1  # the run survives; only the manifest channel is lost
    assert not _entries(project)["DW-1"].open  # the spec channel still closed it
    lost = _kinds(engine.journal, "deferred-close-declaration-unreadable")
    assert lost and lost[-1]["source"] == "stories.yaml"


def test_stories_mode_unions_manifest_and_frontmatter_declarations(project):
    """Both channels are honored, and an id named in both is marked and reported
    once — the natural case once a planner declares it and the spec echoes it."""
    engine = _closing_run(
        project,
        manifest=["DW-1", "DW-2"],
        spec=["DW-2", "DW-3"],  # DW-2 in both
        ledger={"DW-1": "open", "DW-2": "open", "DW-3": "open"},
    )

    engine.run()

    entries = _entries(project)
    assert all(not entries[dw].open for dw in ("DW-1", "DW-2", "DW-3"))
    assert entries["DW-2"].body.count("resolution: resolved by story 1") == 1  # not doubled
    closed = _kinds(engine.journal, "story-deferred-closed")
    assert closed[0]["dw_ids"] == ["DW-1", "DW-2", "DW-3"]  # manifest first, deduped


def test_stories_mode_reports_a_manifest_unreadable_at_the_commit_boundary(project, monkeypatch):
    """The manifest half is re-read at the commit and, unlike the spec half, is
    persisted nowhere — so a parse failure there drops a declared closure with
    nothing to fall back on. Routing it through `_entry_for` reported that as a
    generic `stories-manifest-unreadable` warning that is emitted at most ONCE per
    story, so an earlier dispatch-time failure spent the one line and the lost
    closure passed in silence (#284 follow-up review, finding 4).

    The spec half is unaffected: one unreadable channel must not take the other
    down with it."""
    from bmad_loop import stories as stories_mod

    engine = _closing_run(
        project,
        manifest=["DW-1"],
        spec=["DW-2"],
        ledger={"DW-1": "open", "DW-2": "open"},
    )
    finalize = engine._finalize_commit_phase
    real_load = engine._load_stories

    def unreadable(*_a, **_kw):
        raise stories_mod.StoriesError("stories.yaml is not valid YAML")

    def unreadable_from_the_commit_phase_on(task):
        monkeypatch.setattr(engine, "_load_stories", unreadable)
        try:
            return finalize(task)
        finally:
            monkeypatch.setattr(engine, "_load_stories", real_load)

    monkeypatch.setattr(engine, "_finalize_commit_phase", unreadable_from_the_commit_phase_on)

    summary = engine.run()

    assert summary.done == 1  # never a gate
    entries = _entries(project)
    assert entries["DW-1"].open  # the manifest-declared id is lost...
    events = _kinds(engine.journal, "deferred-close-declaration-unreadable")
    assert len(events) == 1 and events[0]["source"] == "stories.yaml"  # ...but not silently
    assert not entries["DW-2"].open  # the spec half still closed


def test_stories_mode_unknown_id_is_journaled_not_fatal(project):
    """An id naming no ledger entry — a typo, or an entry renumbered since the
    breakdown was written — is journaled and dropped. The annotation is
    traceability, so a stale reference must never fail a story that succeeded."""
    engine = _closing_run(project, manifest=["DW-99"])
    before = project.deferred_work.read_bytes()

    summary = engine.run()

    assert summary.done == 1
    assert project.deferred_work.read_bytes() == before
    unmatched = _kinds(engine.journal, "deferred-close-unmatched")
    assert len(unmatched) == 1 and unmatched[0]["dw_ids"] == ["DW-99"]


# ---------------- frontmatter `deferred:` harvest parity (BMAD-METHOD #2640)

STORY_FINDING = {
    "summary": "The id-keyed spec loader rescans on every call",
    "evidence": "resolve_story_spec globs the folder each time",
    "location": "src/bmad_loop/stories.py:120",
    "severity": "low",
}


def test_stories_mode_harvests_spec_deferrals_into_the_ledger(project):
    from bmad_loop import deferredwork

    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [stories_dev_effect(deferred=[STORY_FINDING])])

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    entries = deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0].open
    assert entries[0].title == STORY_FINDING["summary"]
    assert "location: src/bmad_loop/stories.py:120" in entries[0].body
    assert "source_spec: `1-slug.md`" in entries[0].body
    assert _kinds(engine.journal, "spec-deferrals-harvested")[0]["dw_ids"] == ["DW-1"]


def test_stories_mode_harvests_id_resolved_spec_not_reported_sibling(project):
    """The harvest and verifier must consume the same story-id-keyed artifact.

    A session-reported sibling is untrusted in stories mode: harvesting it before
    ``verify_dev_stories`` resolves the real spec can file another story's finding
    even though only the id-keyed spec is subsequently accepted and committed.
    """
    from bmad_loop import deferredwork

    sibling_finding = {
        "summary": "A stale sibling must not drive the harvest",
        "evidence": "The session reported an unrelated spec path",
        "location": "src/unrelated.py:1",
        "severity": "high",
    }
    write_id_spec = stories_dev_effect(deferred=[STORY_FINDING])

    def report_sibling(spec):
        result = write_id_spec(spec)
        result_json = dict(result.result_json or {})
        sibling = Path(spec.cwd) / SPEC_FOLDER / "stories" / "stale-sibling.md"
        write_spec(
            sibling,
            "done",
            result_json["baseline_commit"],
            deferred=[sibling_finding],
        )
        result_json["spec_file"] = str(sibling)
        return SessionResult(status="completed", result_json=result_json)

    setup_stories(project, [entry("1")])
    engine, _ = make_engine(project, [report_sibling])

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    entries = deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    assert [item.title for item in entries] == [STORY_FINDING["summary"]]
    assert "source_spec: `1-slug.md`" in entries[0].body


def test_plan_halt_leg_does_not_harvest_then_implement_leg_does(project):
    from bmad_loop import deferredwork

    setup_stories(project, [entry("1", spec_checkpoint=True)])
    engine, _ = make_engine(project, [stories_checkpoint_effect(deferred=[STORY_FINDING])])

    summary = engine.run()

    assert summary.paused and summary.done == 0
    assert status_of(read_frontmatter(story_spec(project, "1"))) == "ready-for-dev"
    assert not project.deferred_work.exists()
    assert not _kinds(engine.journal, "spec-deferrals-harvested")

    resumed, _ = resume_engine(
        project, engine, [stories_checkpoint_effect(deferred=[STORY_FINDING])]
    )
    resumed_summary = resumed.run()

    assert resumed_summary.done == 1
    entries = deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0].open
    assert _kinds(resumed.journal, "spec-deferrals-harvested")[0]["dw_ids"] == ["DW-1"]


def test_stories_harvest_alone_is_not_proof_of_work(project):
    setup_stories(project, [entry("1")])
    engine, _ = make_engine(
        project,
        [
            stories_dev_effect(write_src=False, deferred=[STORY_FINDING]),
            stories_dev_effect(),
        ],
    )

    summary = engine.run()

    decisions = _kinds(engine.journal, "dev-decision")
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert "no changes" in decisions[0]["reason"]
    assert _kinds(engine.journal, "spec-deferrals-harvested")[0]["dw_ids"] == ["DW-1"]
    assert summary.done == 1
