"""Engine scenario tests against the mock adapter — no tmux, no LLM."""

import re
import signal
from pathlib import Path

import pytest
from conftest import (
    _file_exists_cmd,
    dev_effect,
    generic_dev_effect,
    git,
    review_effect,
    spec_path,
    write_spec,
    write_sprint,
)

from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine, RunPaused, RunStopped
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
    TokenUsage,
)
from bmad_loop.policy import (
    AdapterPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    VerifyPolicy,
)
from bmad_loop.runs import rearm_escalation
from bmad_loop.verify import (
    GitError,
    PrunePreserveError,
    read_frontmatter,
    rev_parse_head,
    worktree_clean,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def make_engine(project, script, policy=None, **kwargs) -> tuple[Engine, MockAdapter]:
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="test-run", project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            # in-place tests exercise the retry/defer continuation path, which
            # needs auto-rollback on; the OFF (pause) default is covered by its
            # own tests.
            scm=ScmPolicy(rollback_on_failure=True),
        ),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def resume_engine(project, engine, script, policy=None) -> tuple[Engine, MockAdapter]:
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(script)
    new_engine = Engine(
        paths=project,
        policy=policy or engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        # mirror cli._resume_paused_run: the run's scope + cap are restored from
        # persisted state so a resumed `--epic N` run keeps its selector.
        epic_filter=state.epic_filter,
        story_filter=state.story_filter,
        max_stories=state.max_stories,
    )
    return new_engine, adapter


def test_run_session_saves_completed_session_checkpoint(project):
    """The completed session must already be on disk when post_session fires:
    a host kill inside the hooks cannot lose it."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    original_emit = engine._emit
    on_disk_at_post_session = []

    def spying_emit(stage, *args, **kwargs):
        if stage == "post_session":
            on_disk = load_state(engine.run_dir)
            on_disk_at_post_session.append(bool(on_disk.tasks["1-1-a"].sessions))
        return original_emit(stage, *args, **kwargs)

    engine._emit = spying_emit
    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    saved = load_state(engine.run_dir)
    saved_task = saved.tasks["1-1-a"]
    assert len(saved_task.sessions) == 1
    assert saved_task.sessions[0].status == "completed"
    assert saved_task.sessions[0].usage is not None
    assert saved_task.sessions[0].usage.total == 15
    assert saved_task.tokens.total == 15
    assert on_disk_at_post_session == [True]


def test_run_session_persists_session_when_usage_read_raises(project):
    """A failed usage read propagates, but the completed session is already
    saved — usage is metadata, not a durability gate."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [SessionResult(status="completed", session_id="sess-1", transcript_path="events.jsonl")],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    def boom(result):
        raise RuntimeError("usage read failed")

    adapter.read_usage = boom
    with pytest.raises(RuntimeError):
        engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    saved = load_state(engine.run_dir)
    saved_task = saved.tasks["1-1-a"]
    assert len(saved_task.sessions) == 1
    assert saved_task.sessions[0].status == "completed"
    assert saved_task.sessions[0].session_id == "sess-1"
    assert saved_task.sessions[0].transcript_path == "events.jsonl"
    assert saved_task.sessions[0].usage is None


def test_keyboard_interrupt_records_stopped_run(project, monkeypatch):
    """A raw KeyboardInterrupt (Windows console-ctrl bypassing the signal
    handler) records a controlled stop, not a crash."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))

    def interrupt(_spec):
        raise KeyboardInterrupt()

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [interrupt])

    summary = engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped
    assert not summary.crashed
    assert not saved.crashed
    assert killed == ["test-run"]
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[0]["reason"] == "KeyboardInterrupt"


def test_nested_engine_reraises_keyboard_interrupt(project, monkeypatch):
    """A nested engine re-raises KeyboardInterrupt for the outer (owning)
    engine to record — it still tears down its own agent session."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise KeyboardInterrupt()

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # pretend an outer engine owns signals
    try:
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    finally:
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).stopped is False  # owner records it, not us
    assert killed == ["test-run"]


def test_resume_continues_from_completed_dev_session(project):
    """A host kill inside the post-session window of a completed dev session
    must not roll the work back: resume consumes the durably-recorded result
    and drives verify/decide as if the session had just returned."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")])

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    crashed_task = saved.tasks["1-1-a"]
    assert crashed_task.phase == Phase.DEV_RUNNING
    assert crashed_task.sessions[0].result_json is not None
    assert crashed_task.attempt == 1

    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    # the replay stays the attempt it was recorded under, against the persisted
    # baseline — re-capturing either would shift the rollback/squash reference
    # and desync the counter from the recorded session's task_id
    assert final.attempt == 1
    assert final.baseline_commit == crashed_task.baseline_commit
    assert [s.role for s in adapter.sessions] == ["review"]  # dev NOT re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert not any(k.startswith("rollback") for k in kinds)


def test_resume_continues_from_completed_review_session(project):
    """A host kill inside the post-session window of a completed review session
    resumes into the review decision path — the dev phase is not re-entered."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # the review session's post_session
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.REVIEW_RUNNING

    resumed, adapter = resume_engine(project, engine, [])
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # replay does not burn a review-budget slot
    assert adapter.sessions == []  # neither dev nor review re-run
    entries = resumed.journal.entries()
    verifies = [e for e in entries if e["kind"] == "resume-verify"]
    assert verifies and verifies[-1]["role"] == "review"
    kinds = [e["kind"] for e in entries]
    assert "resume-restart" not in kinds


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="stalled"),
        # completed but without a recorded result (legacy state.json shape)
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed"),
    ],
)
def test_resume_restart_when_session_record_incomplete(project, record):
    """A dev-running task whose current-attempt record is not a completed
    session with a recorded result still takes today's resume-restart."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.record_session(record)
    engine.state.tasks[task.story_key] = task
    engine._save()

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-restart" in kinds
    assert "resume-verify" not in kinds


def test_token_budget_discounts_cache_reads(project):
    """Raw totals dominated by cache reads must not trip the budget; the
    weighted total (cache reads at 0.1x) is what's checked."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # per session: raw = 620k (would bust 1.2M over 2 sessions), weighted = 80k
    usage = TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_tokens_per_story=1_200_000),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert summary.total_tokens == 2 * 620_000  # display stays raw
    journal_text = (run_dir / "journal.jsonl").read_text()
    assert "token-budget-exceeded" not in journal_text


def test_token_budget_exceeded_journals_weighted(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    usage = TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_tokens_per_story=100_000),  # < 2 x 80k weighted
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    engine.run()

    entries = [
        line
        for line in (run_dir / "journal.jsonl").read_text().splitlines()
        if "token-budget-exceeded" in line
    ]
    assert len(entries) == 1
    assert '"weighted": 160000' in entries[0]


def test_happy_path(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    assert worktree_clean(project.project)
    assert summary.total_tokens == 30  # 2 sessions x 15
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    assert adapter.sessions[0].env["BMAD_LOOP_MODE"] == "1"
    assert adapter.sessions[1].prompt.startswith("/bmad-dev-auto ")


def test_inplace_ready_gate_veto_defers_before_any_session(project):
    """A plugin gating pre_ready_gate in non-isolated (in-place) mode — e.g. a
    shared-mode Unity engine waiting on the live Editor — defers the unit via the
    bus veto path before any dev session runs. Proves the engine emits the ready
    gate + honors a veto outside the worktree path, with no engine-specific code."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # a declarative plugin whose blocking pre_ready_gate hook fails -> defer veto
    plug = project.project / ".bmad-loop" / "plugins" / "gate"
    plug.mkdir(parents=True)
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "gate"\napi_version = 1\n'
        "[hooks.pre_ready_gate]\ncmd = 'exit 1'\nblocking = true\n"
    )
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert adapter.sessions == []  # gate vetoed before the dev session
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-deferred" in kinds


def test_review_disabled_skips_review_session(project):
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
    )
    # only a dev session is scripted — no review_effect at all
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.review_cycle == 0
    # exactly one session, and it carries the skip-review signal
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert adapter.sessions[0].env["BMAD_LOOP_SKIP_REVIEW"] == "1"
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-skipped" in kinds
    msg = _head_commit_message(project.project)
    assert "implemented via bmad-loop" in msg and "reviewed" not in msg


def test_review_not_recommended_skips_review_session(project):
    """Default review.trigger = "recommended": when the dev session does NOT set
    followup_review_recommended, the orchestrator skips the separate review
    session, validates the deterministic gates, and commits."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # review.enabled stays True (default); only the trigger gate skips it. No
    # review_effect scripted — the dev session must not provoke a review.
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a", followup_review=False)])
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.followup_review_recommended is False
    assert task.review_cycle == 0
    assert [s.role for s in adapter.sessions] == ["dev"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" in kinds and "review-skipped" in kinds


def test_review_recommended_runs_review_session(project):
    """followup_review_recommended True under the default trigger runs the
    follow-up review pass (bmad-dev-auto re-invoked on the done spec)."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=True),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # dev recommended review → the follow-up pass ran; it converged (the latest
    # pass no longer recommends a further follow-up, so the flag is now False)
    assert engine.state.tasks["1-1-a"].followup_review_recommended is False
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" not in kinds


def test_review_trigger_always_runs_without_recommendation(project):
    """review.trigger = "always" runs the review even when the dev session did
    not recommend a follow-up (pre-#2505 behavior)."""
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        review=ReviewPolicy(enabled=True, trigger="always"),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" not in kinds


def test_generic_dev_path_orchestrator_advances_sprint(project):
    """On the generic bmad-dev-auto path the skill self-finalizes the spec but
    never writes the bmad_loop's sprint board; the orchestrator (B2 seam) is the
    single sprint-status writer and advances the story to match verify_dev."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(project, [generic_dev_effect(project, "1-1-a")], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # the orchestrator advanced sprint-status, not the skill
    assert story_status(project.sprint_status, "1-1-a") == "done"
    # the generic dev invocation form
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert adapter.sessions[0].prompt == "/bmad-dev-auto 1-1-a"


def test_generic_dev_path_no_sprint_advance_when_spec_unfinalized(project):
    """The sprint write is gated on the spec actually reaching the success
    status. A session that completes but leaves the spec short of done must not
    advance the sprint, and the story defers (verify_dev fails on spec status)."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="in-progress")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert story_status(project.sprint_status, "1-1-a") == "ready-for-dev"


def test_generic_reconcile_advances_stale_frontmatter_done(project):
    """bmad-dev-auto finalized in prose (## Auto Run Result: Status done) but left
    the frontmatter at the template default. The orchestrator reconciles the
    frontmatter before the sprint sync + verify, so completed, tested work reaches
    DONE instead of falsely deferring — and the repair is journaled loudly."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="draft", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert story_status(project.sprint_status, "1-1-a") == "done"
    # the frontmatter on disk was repaired to the success status
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    # and the repair is recorded loudly so the upstream skill quirk stays visible
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "draft" and recon[0]["to"] == "done"


def test_generic_reconcile_advances_in_review_frontmatter_done(project):
    """A session that dies in its step-04 Finalize tail leaves the frontmatter at
    the transient `in-review` marker while the prose `## Auto Run Result` already
    says done (the Lights-Out DW-153 symptom). On the sole generic path in-review is
    never a deliberate terminal — the legacy review-handoff fork is retired — so the
    orchestrator reconciles it to done before the gates, closing the false-defer +
    rollback re-sweep loop instead of discarding completed, tested work."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="in-review", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert story_status(project.sprint_status, "1-1-a") == "done"
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_in_review_preserves_followup_review_true(project):
    """The follow-up review pass MUST still run when a reconciled-from-in-review
    spec carries `followup_review_recommended: true` in its frontmatter. synth drops
    the flag for a non-done spec, so the frontmatter is the only source — reconcile
    re-reads it when advancing to done, so the recommended-trigger gate still sees it
    and re-invokes bmad-dev-auto on the done spec."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def dev(spec):
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        sp = spec_path(project, "1-1-a")
        # Finalize tail died: frontmatter stuck at the transient in-review marker,
        # but the skill wrote the followup flag + terminal prose done first.
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'in-review'\n"
            f"followup_review_recommended: true\nbaseline_commit: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="recommended"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project, [dev, review_effect(project, "1-1-a", clean=True)], policy=pol
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # reconcile advanced in-review → done AND re-attached the frontmatter flag, so
    # the follow-up review pass ran (dev then review session)
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-not-recommended" not in kinds
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_in_review_followup_false_skips_review(project):
    """The mirror case (DW-153's actual shape): a reconciled-from-in-review spec
    with `followup_review_recommended: false` in frontmatter skips the follow-up
    review and commits with the dev session only."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def dev(spec):
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        sp = spec_path(project, "1-1-a")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'in-review'\n"
            f"followup_review_recommended: false\nbaseline_commit: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="recommended"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    # No review_effect scripted: the recommended-trigger gate must skip the review.
    engine, adapter = make_engine(project, [dev], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].followup_review_recommended is False
    assert [s.role for s in adapter.sessions] == ["dev"]
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-not-recommended" in kinds
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_advances_bare_null_frontmatter_status(project):
    """The skill left a bare `status:` (YAML null) but finalized in prose with real
    code. status_of would read that as "none"; the reconcile normalizes null to ""
    so it still advances to done — and the filled line is valid YAML."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, rev_parse_head, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def effect(spec):
        baseline = rev_parse_head(project.project)
        # a real source change so the proof-of-work gate passes
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        # spec finalized in prose, but frontmatter left at a bare YAML-null status
        sp = spec_path(project, "1-1-a")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus:\nbaseline_revision: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [effect], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert story_status(project.sprint_status, "1-1-a") == "done"
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "" and recon[0]["to"] == "done"


def test_generic_reconcile_skips_out_of_tree_spec(project, tmp_path):
    """Reconcile refuses to mutate a spec the session reports outside the
    orchestrator-owned roots: the file is left untouched and the skip is journaled,
    so a surprising `spec_file` can never be silently rewritten."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy

    outside = tmp_path / "outside" / "spec.md"
    outside.parent.mkdir(parents=True)
    original = "---\ntitle: 'x'\nstatus:\n---\n\n## Auto Run Result\n\n- Status: done\n"
    outside.write_text(original, encoding="utf-8")

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [generic_dev_effect(project, "1-1-a")], policy=pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    engine._reconcile_generic_terminal_status(task, {"spec_file": str(outside)})

    assert outside.read_text() == original  # never written
    kinds = [e["kind"] for e in engine.journal.entries()]
    skipped = [
        e for e in engine.journal.entries() if e["kind"] == "spec-reconcile-skipped-out-of-tree"
    ]
    assert len(skipped) == 1 and skipped[0]["spec"] == str(outside)
    assert "spec-status-reconciled" not in kinds  # no reconcile happened


def test_generic_reconcile_idempotent_when_already_done(project):
    """When the skill DID advance the frontmatter to done, reconcile is a no-op:
    no second write, no `spec-status-reconciled` journal entry."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="done", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert recon == []


def test_reset_spec_for_repair_strips_stale_terminal_section(project):
    """Re-arming a self-finalized spec must remove the stale `## Auto Run Result`
    section along with the status flip — find_result_artifact keys on that
    heading, so leaving it would let the re-driven session's first save of the
    spec qualify as a terminal result mid-turn."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        "---\nstatus: done\n---\n\n## Intent\n\nbody\n\n"
        "## Auto Run Result\n\nStatus: done\nAll done.\n",
        encoding="utf-8",
    )
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec))

    engine._reset_spec_for_repair(task)

    text = spec.read_text(encoding="utf-8")
    assert "status: in-progress\n" in text  # re-opened
    assert "Auto Run Result" not in text  # stale terminal section gone
    assert "## Intent\n\nbody\n" in text  # frozen intent untouched


def test_generic_reconcile_skips_blocked_prose(project):
    """A blocked outcome (prose Status: blocked) is NEVER reconciled: the
    frontmatter stays non-terminal, no `spec-status-reconciled` is emitted, and the
    story does not falsely pass (it defers via the unfinalized-spec gate)."""
    from bmad_loop.policy import DevPolicy, LimitsPolicy, ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="draft", prose_status="blocked")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 0  # blocked prose never rides reconcile to a pass
    # reconcile never fired (no journal entry); the unfinalized spec defers as before
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert recon == []


def test_generic_reconcile_does_not_bypass_no_change_gate(project):
    """Reconcile repairs a bookkeeping field, never the proof-of-work gate. A
    session that finalizes in prose (Status: done) but produced NO real code change
    is reconciled to done on disk yet still DEFERS — has_changes_since backstops it,
    so empty work cannot ride the prose marker to PROCEED."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, LimitsPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    # Real projects do NOT gitignore the BMAD output tree (`bmad-loop init` only
    # ignores .bmad-loop/runs|cache), so the spec file the skill writes is tracked.
    # The proof-of-work gate excludes the orchestrator-owned artifact folders, so a
    # spec-only edit — including the reconcile rewrite — still reads as "no changes".
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def effect(spec):
        # finalize in prose only; touch NO source file
        baseline = rev_parse_head(project.project)
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "draft", baseline, prose_status="done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [effect], policy=pol)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    # reconcile DID fire (the spec was advanced to done; recorded before the
    # deferral relocates the spec to the archive) ...
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["to"] == "done"
    # ... but the deterministic diff gate still deferred the empty work
    assert "no changes" in (engine.state.tasks["1-1-a"].defer_reason or "")


def test_generic_repair_reopens_spec_before_reinvocation(project):
    """B6: bmad-dev-auto self-finalizes to `done`; its step-01 would route a done
    spec to "ingest as context, don't resume." So before a verify-failure repair
    re-invocation the orchestrator flips the spec back to `in-progress` — the
    repair session must SEE an open spec on entry."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy, VerifyPolicy
    from bmad_loop.verify import read_frontmatter, rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    sp = spec_path(project, "1-1-a")
    marker = project.project / "marker.txt"
    seen_status: list[str] = []
    calls = {"n": 0}

    def effect(spec):
        calls["n"] += 1
        if sp.is_file():  # status the repair session sees on entry
            seen_status.append(str(read_frontmatter(sp).get("status", "")).strip())
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + f"change {calls['n']}\n")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'done'\nbaseline_commit: '{baseline}'\n---\n\n## Intent\n",
            encoding="utf-8",
        )
        if calls["n"] >= 2:  # second pass satisfies the verify command
            marker.write_text("ok\n")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        verify=VerifyPolicy(commands=(_file_exists_cmd("marker.txt"),)),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(project, [effect, effect], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    # the repair session saw an in-progress spec, not the finalized `done`
    assert seen_status == ["in-progress"]
    # and it was driven by the freeform resume prompt, not /bmad-dev-auto <key>
    assert adapter.sessions[1].prompt.startswith("/bmad-dev-auto Resume the autonomous")


def _head_commit_message(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_finish_kills_session_when_enabled(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    assert engine.state.finished
    assert killed == ["test-run"]


def test_finish_keeps_session_when_disabled(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(cleanup_session_on_finish=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()
    assert engine.state.finished
    assert killed == []


def test_per_stage_adapter_and_model_dispatch(project):
    """Dev and review sessions go to their own adapters with per-stage models."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    dev_mock = MockAdapter([dev_effect(project, "1-1-a")])
    review_mock = MockAdapter([review_effect(project, "1-1-a", clean=True)])
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            review=StageAdapterPolicy(name="codex", model="gpt-5-codex"),
        ),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=dev_mock,
        review_adapter=review_mock,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert [s.role for s in dev_mock.sessions] == ["dev"]
    assert [s.role for s in review_mock.sessions] == ["review"]
    assert dev_mock.sessions[0].model == "opus"
    assert review_mock.sessions[0].model == "gpt-5-codex"


def test_review_loop_converges_within_budget(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, patched=2),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.tasks["1-1-a"].review_cycle == 2


def test_budget_exhausted_finalized_work_commits(project):
    """A finalized story (status: done, sprint done, verify green) whose review
    pass keeps recommending an independent follow-up is COMMITTED when the review
    budget is exhausted — not rolled back. The lingering recommendation is
    re-filed as a fresh open deferred-work entry, and the run records the event."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 3
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    # the finalized work is committed, not reverted
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-budget-committed" in kinds and "story-deferred" not in kinds
    # the lingering follow-up is preserved as a new open deferred-work entry
    open_entries = [
        e for e in deferredwork.parse_ledger(project.deferred_work.read_text()) if e.open
    ]
    assert any("origin: review-budget-followup" in e.body for e in open_entries)


def test_budget_exhausted_unfinalized_defers(project):
    """Genuine non-convergence: the review never finalizes the spec (status stays
    in-progress, so the post-budget verify gate fails). Budget exhaustion defers
    and rolls the tree back, exactly as before the commit-instead-of-rollback
    safeguard."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "did not converge" in task.defer_reason
    # repo rolled back for the next story
    assert (project.project / "src.txt").read_text() == "original\n"
    assert rev_parse_head(project.project) == task.baseline_commit
    # the in-review spec is stashed out of artifacts into the run dir so a
    # leftover can't confuse the next attempt — the work is kept for the human
    from conftest import spec_path

    assert not spec_path(project, "1-1-a").exists()
    stashed = engine.run_dir / "deferred" / "1-1-a" / "spec-1-1-a.md"
    assert stashed.is_file() and "status: 'in-progress'" in stashed.read_text()


def test_budget_exhausted_failed_review_sessions_defer_not_commit(project):
    """A *failed* final review session must never trigger the commit-instead-of-
    rollback rescue. Dev finalizes the story (status: done, recommends a follow-up),
    but every review session crashes/stalls. On the last cycle the budget is spent,
    so decide_review_session returns DEFER (not RETRY) and the loop rolls the tree
    back — it does not reach (or fire) the budget-exhaustion rescue commit. Locks in
    the invariant that makes a 'final-iteration RETRY commits un-reviewed work' path
    unreachable."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),  # finalizes spec to done, recommends follow-up
            SessionResult(status="crashed"),
            SessionResult(status="stalled"),
            SessionResult(status="crashed"),  # final cycle: budget spent -> DEFER
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "review session" in task.defer_reason  # decide_review_session's DEFER reason
    # rolled back, not committed — and the rescue commit never ran
    assert (project.project / "src.txt").read_text() == "original\n"
    assert rev_parse_head(project.project) == task.baseline_commit
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" in kinds and "review-budget-committed" not in kinds


def test_defer_preserves_deferred_work_additions(project):
    """Review sessions append real knowledge to deferred-work.md; a plateau
    defer's git reset must not erase it."""
    from conftest import git
    from conftest import review_effect as make_review

    project.deferred_work.write_text("# Deferred Work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "seed deferred-work")
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def reviewing_with_defer(spec):
        with project.deferred_work.open("a") as f:
            f.write("\n### DW-1: pre-existing flaky retry\n\nstatus: open\n")
        return make_review(project, "1-1-a", clean=False, patched=1, finalized=False)(spec)

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")] + [reviewing_with_defer for _ in range(3)],
    )
    summary = engine.run()
    assert summary.deferred == 1
    assert "DW-1: pre-existing flaky retry" in project.deferred_work.read_text()


def test_rollback_off_pauses_with_manual_notice(project):
    """Production default (rollback_on_failure=False): a would-be defer reset
    never touches the tree — it pauses with bold manual-recovery instructions."""

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
        policy=policy,
    )
    precious = project.project / "keep-me.txt"
    precious.write_text("precious\n")  # an untracked file the user wants kept
    summary = engine.run()

    assert summary.paused
    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_ESCALATION
    reason = state.paused_reason.lower()
    assert "manual rollback" in reason and "back up" in reason
    assert "failed" not in reason  # a stopped attempt is not described as "failed"
    # the orchestrator left the tree exactly as-is — no reset, nothing deleted
    assert not worktree_clean(project.project)
    assert precious.read_text() == "precious\n"
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-manual-required" in kinds


def test_rollback_on_preserves_preexisting_untracked(project):
    """With rollback_on_failure=True the auto-rollback reverts tracked changes
    but never deletes untracked files that predate the attempt."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
        policy=policy,
    )
    precious = project.project / "user-notes.txt"
    precious.write_text("keep me\n")  # untracked, present before baseline capture
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert rev_parse_head(project.project) == task.baseline_commit  # tracked reverted
    assert precious.read_text() == "keep me\n"  # pre-existing untracked survives


def test_rollback_or_pause_skips_clean_tree(project):
    """When the attempt left nothing in the tree (HEAD == baseline, no run-created
    untracked files), there is nothing to roll back: even with auto-rollback OFF
    the orchestrator neither pauses nor touches the tree — it just journals and
    returns, so resume can proceed."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)  # clean tree at baseline
    task.baseline_untracked = []

    engine._rollback_or_pause(task)  # must NOT raise RunPaused

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-skipped-clean" in kinds
    assert "rollback-manual-required" not in kinds


def test_manual_recovery_wording_stopped(project):
    """Only the stopped/abandoned path reaches the manual-recovery notice now (a
    resolved escalation auto-recovers instead). The notice never claims the story
    'failed' and names the real cause."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    baseline = rev_parse_head(project.project)

    with pytest.raises(RunPaused) as stopped:
        engine._pause_for_manual_recovery(task, baseline)

    assert "failed" not in stopped.value.reason
    assert "manual rollback" in stopped.value.reason.lower()
    assert "attempt was stopped" in stopped.value.reason


def test_rollback_preserves_committed_attempt_work(project):
    """rollback_on_failure ON + an attempt that committed its work: the hard reset
    parks those commits under a recovery ref instead of orphaning them."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed implementation\n")  # attempt commits its work
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-commits-preserved")
    assert git(repo, "rev-parse", entry["ref"]).strip() == attempt_head  # reachable by name


def test_rollback_preserves_uncommitted_attempt_worktree(project):
    """rollback_on_failure ON + an attempt that left work UNcommitted: before the
    hard reset (and its untracked cleanup) the engine parks the uncommitted diff —
    both the tracked edit and the run-created untracked file — under a recovery ref,
    so a re-drive never restarts from zero and nothing is silently destroyed."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted tracked edit\n")  # tracked, never committed
    (repo / "new_test.txt").write_text("uncommitted new file\n")  # run-created untracked

    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    assert (repo / "src.txt").read_text() == "original\n"  # tracked edit reverted...
    assert (repo / "new_test.txt").exists() is False  # ...untracked cleanup removed the new file
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved")
    ref = entry["ref"]  # ...but both are recoverable from the parked snapshot
    # (conftest `git` strips, so compare against the newline-free blob content)
    assert git(repo, "show", f"{ref}:src.txt") == "uncommitted tracked edit"
    assert git(repo, "show", f"{ref}:new_test.txt") == "uncommitted new file"


def test_rollback_preserves_distinct_refs_across_repeated_dirty_rollbacks(project):
    """Two dirty rollbacks against the SAME baseline_commit (mimicking the dev retry
    loop, where baseline_commit is fixed) must each park their uncommitted work under
    a DISTINCT recovery ref — keyed on task.attempt — so the 2nd rollback cannot
    orphan the 1st attempt's snapshot. Both parked snapshots stay recoverable by name
    with their own attempt's edit."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []

    # cycle 1 (attempt 0): dirty the tree, roll back
    task.attempt = 0
    (repo / "src.txt").write_text("attempt 0 edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    assert (repo / "src.txt").read_text() == "original\n"

    # cycle 2 (attempt 1): SAME baseline, dirty again, roll back
    task.attempt = 1
    (repo / "src.txt").write_text("attempt 1 edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit
    assert (repo / "src.txt").read_text() == "original\n"

    refs = [e["ref"] for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved"]
    assert len(refs) == 2
    assert len(set(refs)) == 2  # distinct — the 2nd rollback did not overwrite the 1st
    # both snapshots remain reachable and carry their own attempt's uncommitted edit
    assert git(repo, "show", f"{refs[0]}:src.txt") == "attempt 0 edit"
    assert git(repo, "show", f"{refs[1]}:src.txt") == "attempt 1 edit"


def test_run_start_prunes_excess_preserve_refs(project):
    """Run start with scm.preserve_keep set and more attempt-preserve/* refs than
    the budget: the tail is deleted before the loop, only preserve_keep refs
    survive, and the deletions are journalled."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    for i in range(3):
        (repo / "impl.txt").write_text(f"parked attempt {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"attempt {i}")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    remaining = git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/attempt-preserve/"
    ).splitlines()
    assert len(remaining) == 2  # tail pruned down to the budget
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-pruned")
    assert entry["count"] == 1
    assert set(entry["refs"]) | set(remaining) == {f"attempt-preserve/run-{i}" for i in range(3)}


def test_run_start_prune_failure_journals_and_run_proceeds(project, monkeypatch):
    """A failing prune at run start is journalled and never blocks the run —
    the recovery refs are a housekeeping concern, not run state."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _fail(*a, **k):
        raise GitError("simulated for-each-ref failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "simulated for-each-ref failure" in entry["error"]
    assert engine.state.finished  # the prune failure never blocked or crashed the run


def test_run_start_partial_prune_journals_deletions_and_failure(project, monkeypatch):
    """A partial prune (some refs deleted before one stuck) journals BOTH the
    structured deletions and the failure — the destructive half of a stuck
    prune must never be auditable only via the error string."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=1),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _partial(*a, **k):
        raise PrunePreserveError(
            "one ref stuck",
            deleted=["attempt-preserve/gone"],
            failed=["attempt-preserve/stuck (checked out)"],
        )

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _partial)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    pruned = next(e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-pruned")
    assert pruned["count"] == 1 and pruned["refs"] == ["attempt-preserve/gone"]
    failed = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "one ref stuck" in failed["error"]
    assert engine.state.finished


def _park_dirty_snapshots(repo, count):
    """Write `count` refs/attempt-preserve-dirty/* snapshot refs, each on its
    own commit so committer-date ordering is well defined."""
    for i in range(count):
        (repo / "impl.txt").write_text(f"snapshot {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"snapshot {i}")
        git(repo, "update-ref", f"refs/attempt-preserve-dirty/run-{i}", "HEAD")


def test_run_start_prunes_excess_dirty_snapshot_refs(project):
    """Run start with scm.preserve_keep set and more attempt-preserve-dirty
    snapshot refs than the budget: the tail is deleted before the loop, only
    preserve_keep refs survive, and the deletions are journalled under the
    family's own event kind."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    _park_dirty_snapshots(repo, 3)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    remaining = git(
        repo, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/"
    ).splitlines()
    assert len(remaining) == 2  # tail pruned down to the budget
    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-pruned"
    )
    assert entry["count"] == 1
    assert set(entry["refs"]) | set(remaining) == {
        f"refs/attempt-preserve-dirty/run-{i}" for i in range(3)
    }


def test_run_start_dirty_prune_failure_journals_and_run_proceeds(project, monkeypatch):
    """A failing dirty-snapshot prune at run start is journalled under its own
    event kind and never blocks the run."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _fail(*a, **k):
        raise GitError("simulated dirty for-each-ref failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_dirty_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-prune-failed"
    )
    assert "simulated dirty for-each-ref failure" in entry["error"]
    assert engine.state.finished  # the prune failure never blocked or crashed the run


def test_run_start_branch_prune_failure_does_not_skip_dirty_prune(project, monkeypatch):
    """A failing branch-family prune must not skip the dirty family: with excess
    dirty snapshot refs present, the branch failure is journalled AND the dirty
    tail is still pruned and journalled."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=1),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    _park_dirty_snapshots(repo, 2)

    def _fail(*a, **k):
        raise GitError("simulated branch prune failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    failed = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "simulated branch prune failure" in failed["error"]
    pruned = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-pruned"
    )
    remaining = git(
        repo, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/"
    ).splitlines()
    assert pruned["count"] == 1 and len(remaining) == 1  # pruned down to the budget
    assert set(pruned["refs"]) | set(remaining) == {
        f"refs/attempt-preserve-dirty/run-{i}" for i in range(2)
    }
    assert engine.state.finished


def test_rollback_worktree_preserve_failure_journals_git_error(project, monkeypatch):
    """When the uncommitted-work snapshot can't be captured, the best-effort path still
    resets (rollback ON, no commits above baseline -> no pause) but journals the underlying
    git error, so a post-mortem can see WHY preservation failed — not just that it did."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted edit\n")  # dirty but uncommitted; no commits

    def _fail(*a, **k):
        raise GitError("simulated commit-tree failure")

    monkeypatch.setattr("bmad_loop.verify.snapshot_worktree", _fail)

    engine._rollback_or_pause(task)  # best-effort: journals + proceeds, never raises

    assert rev_parse_head(repo) == task.baseline_commit  # reset still happened
    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserve-failed"
    )
    assert "simulated commit-tree failure" in entry["error"]  # underlying git detail preserved


def test_rollback_pauses_when_preserve_fails(project, monkeypatch):
    """Safety invariant: if the recovery ref can't be created while commits exist,
    the engine pauses for manual recovery rather than resetting past the work — and
    the notice names the at-risk commits instead of the misleading rollback-OFF
    'just reset --hard' wording."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    def _fail(*a, **k):
        raise GitError("simulated branch failure")

    monkeypatch.setattr("bmad_loop.verify.preserve_commits", _fail)

    with pytest.raises(RunPaused) as paused:
        engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == attempt_head  # NOT reset — work left intact
    reason = paused.value.reason.lower()
    assert "commit" in reason  # notice names the at-risk committed work
    assert "auto-rollback is off" not in reason  # never the misleading OFF wording (rollback is ON)


def test_resolved_redrive_never_pauses_when_preserve_fails(project, monkeypatch):
    """A resolved re-drive is contractually pause-free. Even if the recovery ref
    can't be created for the attempt's commits, it journals the failure and lets
    the reset proceed — unlike the general rollback path, which pauses."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # OFF: only the resolved path resets here
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("failed attempt work\n")  # committed, outside artifacts
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "failed attempt")

    def _fail(*args, **kwargs):
        raise GitError("simulated branch failure")

    monkeypatch.setattr("bmad_loop.verify.preserve_commits", _fail)

    engine._rollback_or_pause(task, cause="resolved")  # must NOT raise RunPaused

    assert rev_parse_head(repo) == task.baseline_commit  # reset proceeded, not paused
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "attempt-preserve-failed" in kinds
    assert "rollback-manual-required" not in kinds


def test_rollback_or_pause_resolved_auto_recovers(project):
    """A resolved escalation re-drive (human-initiated) auto-recovers even with
    rollback_on_failure OFF: it reverts the failed attempt's source change but
    preserves the corrected spec under the BMAD artifact folder, and never pauses
    for manual rollback."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # OFF: stopped attempts would pause
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []  # clean fixture at baseline

    (repo / "src.txt").write_text("partial dev work\n")  # failed attempt's source
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: corrected by resolve\n")  # spec under the artifact folder

    engine._rollback_or_pause(task, cause="resolved")  # must NOT raise RunPaused

    assert (repo / "src.txt").read_text() == "original\n"  # source reverted
    assert spec.read_text() == "frozen: corrected by resolve\n"  # spec preserved
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-auto" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_redrive_preserves_spec_on_later_rollback(project):
    """Regression: once a resolved re-drive latches `resolved_redrive`, a *later*
    mid-re-drive rollback (default cause="stopped", rollback_on_failure ON) must
    still preserve the corrected spec under the artifact folder — not just the
    first resume-time reset. Without the latch this reset ran with preserve=()
    and silently reverted the human correction, looping the re-drive."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),  # ON: a stopped attempt resets
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    task.resolved_redrive = True  # latched by _finish_inflight on the re-drive

    (repo / "src.txt").write_text("re-drive dev work\n")  # this attempt's source
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: corrected by resolve\n")  # the human correction

    engine._rollback_or_pause(task)  # default cause="stopped"; must NOT pause

    assert (repo / "src.txt").read_text() == "original\n"  # source reverted
    assert spec.read_text() == "frozen: corrected by resolve\n"  # correction kept
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-auto" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_escalation_resume_skips_clean_rollback(project):
    """End-to-end regression for the resume loop: a CRITICAL escalation that left
    a clean tree, once resolved (re-armed) and resumed, must NOT demand a manual
    rollback — it skips the no-op rollback and re-drives the corrected story."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "auto-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # production default
    )
    engine, _ = make_engine(project, [escalating], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.ESCALATED

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.paused  # re-drove, no manual-rollback loop
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "rollback-skipped-clean" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_escalation_resume_dirty_tree_auto_recovers(project):
    """End-to-end regression for the reported loop: a CRITICAL escalation that left
    the tree dirty (a partial source edit plus a spec under the artifact folder),
    once resolved (re-armed) and resumed with rollback_on_failure OFF, must
    auto-recover — NOT demand a manual rollback — and re-drive the story to done."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def escalate_dirty(spec):
        baseline = rev_parse_head(project.project)
        (project.project / "src.txt").write_text("partial dev work\n")  # source debris
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "blocked", baseline)  # spec under the artifact folder
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "escalations": [{"type": "blocked", "severity": "CRITICAL", "detail": "boom"}],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # production default
    )
    engine, _ = make_engine(project, [escalate_dirty], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.paused  # no manual-rollback loop
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "rollback-auto" in kinds  # auto-recovered despite OFF
    assert "rollback-manual-required" not in kinds


def test_dev_escalation_records_spec_for_rearm(project):
    """A dev session that HALTs with a `blocked` spec still records task.spec_file,
    so rearm_escalation can flip the spec to `ready-for-dev` for the re-drive.
    Without it (verify_dev only records the spec on success) the re-drive HALTs
    again on the stale `blocked` status — the loop seen in the live run."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    sp = spec_path(project, "1-1-a")

    def halt_blocked(spec):
        write_spec(sp, "blocked", rev_parse_head(project.project))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "escalations": [
                    {"type": "blocked", "severity": "CRITICAL", "detail": "blocked spec supplied"}
                ],
            },
        )

    engine, _ = make_engine(project, [halt_blocked])
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    task = load_state(engine.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.spec_file and Path(task.spec_file).name == sp.name  # recorded despite HALT

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step
    assert read_frontmatter(sp)["status"] == "ready-for-dev"  # re-drive will not HALT


def test_dev_stall_retries_then_succeeds(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="stalled"),
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.tasks["1-1-a"].attempt == 2


def test_dev_exhausted_defers_and_run_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="timeout"),
            SessionResult(status="crashed"),
            dev_effect(project, "1-2-b"),
            review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE


def test_critical_escalation_pauses_and_resume_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "auto-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    engine, _ = make_engine(project, [escalating])
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    saved = load_state(engine.run_dir)
    assert saved.paused_reason and "boom" in saved.paused_reason
    assert saved.tasks["1-1-a"].phase == Phase.ESCALATED

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-2-b"), review_effect(project, "1-2-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 1 and not summary2.paused
    assert resumed.state.finished


def test_epic_boundary_gate_pause_and_resume(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    gated = Policy(gates=GatesPolicy(mode="per-epic"), notify=QUIET)
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=gated,
    )
    summary = engine.run()
    assert summary.done == 1 and summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_EPIC_BOUNDARY

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 2 and not summary2.paused


def test_spec_approval_gate_pause_then_resume_reviews(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(gates=GatesPolicy(mode="per-story-spec-approval"), notify=QUIET)
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_SPEC_APPROVAL
    assert saved.tasks["1-1-a"].phase == Phase.DEV_VERIFY
    assert saved.tasks["1-1-a"].spec_file

    resumed, adapter = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=True)], policy=gated
    )
    summary2 = resumed.run()
    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]


def test_dev_verify_command_failure_routes_feedback_fix(project):
    """A broken build never reaches review: the dev-stage gate fails, the tree
    is kept, and the next dev session gets the failing output as feedback."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def fix(spec):
        marker.write_text("ok\n")
        sp = spec_path(project, "1-1-a")
        baseline = rev_parse_head(project.project)
        # the repair session re-finalizes the re-opened spec to done, as the real
        # bmad-dev-auto resume does (the orchestrator flipped it to in-progress)
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [{"command": _file_exists_cmd(marker), "ok": True}],
                "escalations": [],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.attempt == 2
    prompts = [s.prompt for s in adapter.sessions]
    # the repair re-invocation is the freeform resume prompt (no --feedback flag);
    # the feedback file is referenced as the last backtick-wrapped path.
    assert "Resume the autonomous" not in prompts[0] and "Resume the autonomous" in prompts[1]
    feedback = Path(re.findall(r"`([^`]*)`", prompts[1])[-1])
    assert _file_exists_cmd(marker) in feedback.read_text()
    # the first attempt's work survived: no reset between attempts
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_review_verify_failure_routes_fix_session_then_rereview(project):
    """Verify commands failing after a clean review route to a feedback-driven
    dev fix session and a fresh review cycle — not a blind re-review."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # the review's "patch" broke the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix(spec):
        marker.write_text("ok\n")
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2 and task.attempt == 2
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev", "review"]
    assert "Resume the autonomous" in adapter.sessions[2].prompt


def test_review_verify_failure_without_fix_budget_defers(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine, adapter = make_engine(project, [dev_with_marker, breaking_review], policy=policy)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert "kept failing" in engine.state.tasks["1-1-a"].defer_reason


def test_verify_commands_never_pass_defers_at_dev(project):
    """Unfixable verify failures exhaust the dev budget and defer before any
    review session is spent."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=("false",)),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), dev_effect(project, "1-1-a")],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]
    assert "Resume the autonomous" in adapter.sessions[1].prompt


def test_max_stories_limit(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        max_stories=1,
    )
    summary = engine.run()
    assert summary.done == 1
    assert "1-2-b" not in engine.state.tasks


def test_run_end_auto_sweep_fires_once(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=calls.append,
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert calls == ["run-end"]
    assert load_state(engine.run_dir).sweeps_triggered == ["run-end"]


def test_per_epic_auto_sweep_fires_at_boundary(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="per-epic")
    )
    calls = []
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
            dev_effect(project, "2-1-b"),
            review_effect(project, "2-1-b", clean=True),
        ],
        policy=policy,
        sweep_factory=calls.append,
    )
    summary = engine.run()
    assert summary.done == 2
    assert calls == ["epic-1"]  # boundary only; run-end mode not set


def test_auto_sweep_no_refire_on_resume(project):
    """The per-epic trigger is recorded before the gate pause, so resuming
    the run must not fire the same sweep again."""
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="per-epic"),
        notify=QUIET,
        sweep=SweepPolicy(auto="per-epic"),
    )
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=calls.append,
    )
    assert engine.run().paused
    assert calls == ["epic-1"]

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        sweep_factory=calls.append,
    )
    assert resumed.run().done == 2
    assert calls == ["epic-1"]  # not re-fired


def test_auto_sweep_failure_does_not_pause_parent(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def exploding(trigger):
        raise RuntimeError("child sweep blew up")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=exploding,
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert engine.state.finished
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-failed" in journal and "child sweep blew up" in journal


def test_no_auto_sweep_by_default(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        sweep_factory=calls.append,
    )
    engine.run()
    assert calls == []


def test_journal_records_decisions(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    kinds = [e["kind"] for e in engine.journal.entries()]
    for expected in (
        "story-start",
        "session-start",
        "dev-decision",
        "review-result",
        "story-done",
        "run-complete",
    ):
        assert expected in kinds


def test_journal_stamps_log_position(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.set_active_log("t-dev-1")
    journal.append("session-start", task_id="t-dev-1")  # log file not created yet
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-dev-1.log").write_bytes(b"x" * 37)
    journal.append("dev-decision", story_key="1-1-a")
    journal.append("custom", log_task="elsewhere", log_pos=5)  # caller fields win

    entries = journal.entries()
    assert "log_task" not in entries[0] and "log_pos" not in entries[0]
    assert entries[1]["log_task"] == "t-dev-1" and entries[1]["log_pos"] == 0
    assert entries[2]["log_task"] == "t-dev-1" and entries[2]["log_pos"] == 37
    assert entries[3]["log_task"] == "elsewhere" and entries[3]["log_pos"] == 5


def test_journal_log_position_covers_post_session_entries(project):
    """The active log is set at session-start and deliberately not cleared:
    post-session entries (decisions, story-done) point at the end of the log
    of the session they are about."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    entries = engine.journal.entries()
    starts = [e for e in entries if e["kind"] == "session-start"]
    assert len(starts) == 2  # dev + review
    assert all(e["log_task"] == e["task_id"] for e in starts)
    assert all(isinstance(e["log_pos"], int) for e in starts)
    story_start = next(e for e in entries if e["kind"] == "story-start")
    assert "log_task" not in story_start  # written before any session
    dev_decision = next(e for e in entries if e["kind"] == "dev-decision")
    assert dev_decision["log_task"] == starts[0]["task_id"]
    story_done = next(e for e in entries if e["kind"] == "story-done")
    assert story_done["log_task"] == starts[-1]["task_id"]


# ----------------------------------------------------------- stop / SIGTERM


@pytest.mark.parametrize(
    ("signal_name", "fallback_signum"),
    [("SIGINT", signal.SIGINT), ("SIGBREAK", 21)],
)
def test_windows_console_ctrl_signal_is_ignored(project, monkeypatch, signal_name, fallback_signum):
    import bmad_loop.engine as engine_mod

    signum = getattr(signal, signal_name, fallback_signum)
    if signal_name == "SIGBREAK":
        monkeypatch.setattr(signal, "SIGBREAK", signum, raising=False)

    installed = {}
    restored = {}
    previous = {}

    def fake_signal(sig, handler):
        previous.setdefault(sig, object())
        if callable(handler):
            installed[sig] = handler
        else:
            restored[sig] = handler
        return previous[sig]

    monkeypatch.setattr(engine_mod.sys, "platform", "win32")
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: None)

    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_loop", lambda: installed[signum](signum, None))

    summary = engine.run()

    assert summary is not None
    assert load_state(engine.run_dir).stopped is False
    assert "console-ctrl-ignored" in (engine.run_dir / "journal.jsonl").read_text()
    assert restored[signal.SIGTERM] is previous[signal.SIGTERM]
    assert restored[signal.SIGINT] is previous[signal.SIGINT]
    assert restored[signum] is previous[signum]
    assert Engine._stop_signals_owner is None


def test_non_windows_sigint_still_stops_run(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    installed = {}
    previous = {}

    def fake_signal(sig, handler):
        previous.setdefault(sig, object())
        if callable(handler):
            installed[sig] = handler
        return previous[sig]

    killed = []
    monkeypatch.setattr(engine_mod.sys, "platform", "linux")
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))

    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_loop", lambda: installed[signal.SIGINT](signal.SIGINT, None))

    engine.run()

    assert load_state(engine.run_dir).stopped is True
    assert killed == ["test-run"]
    assert "run-stop" in (engine.run_dir / "journal.jsonl").read_text()
    assert Engine._stop_signals_owner is None


def test_run_stopped_via_real_signal(project, monkeypatch):
    """SIGTERM unwinds the loop as RunStopped: the run is marked stopped, the
    agent session is torn down, and the prior signal handlers are restored."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])
    # raise_signal delivers an in-process, catchable SIGTERM via C raise() — the
    # portable "signal myself" primitive. os.kill(getpid(), SIGTERM) is POSIX-only
    # here: on Windows it maps to TerminateProcess (uncatchable, kills the runner).
    monkeypatch.setattr(engine, "_loop", lambda: signal.raise_signal(signal.SIGTERM))

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    summary = engine.run()

    assert summary is not None
    assert load_state(engine.run_dir).stopped is True
    assert killed == ["test-run"]
    assert "run-stop" in (engine.run_dir / "journal.jsonl").read_text()
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int
    assert Engine._stop_signals_owner is None


def test_nested_engine_reraises_runstopped(project, monkeypatch):
    """A nested auto-sweep engine does not own the handlers, so it re-raises
    RunStopped for the outer (owning) engine to record — it still tears down
    its own agent session."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RunStopped()

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # pretend an outer engine owns signals
    try:
        with pytest.raises(RunStopped):
            engine.run()
    finally:
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).stopped is False  # owner records it, not us
    assert killed == ["test-run"]


# ----------------------------------------------------------- crash safety-net


def test_run_crash_records_diagnostics(project, monkeypatch):
    """An unexpected exception out of the loop is recorded (state flag, journal,
    persisted traceback) instead of crashing the orchestrator: the orphaned
    agent session is torn down and a crashed summary is returned."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    summary = engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crashed is True
    assert state.crash_error.startswith("RuntimeError")
    assert state.finished is False
    assert killed == ["test-run"]
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    crash_txt = (engine.run_dir / "crash.txt").read_text()
    assert "Traceback" in crash_txt
    assert "boom" in crash_txt
    assert summary.crashed is True
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int
    assert Engine._stop_signals_owner is None


def test_nested_engine_reraises_crash(project, monkeypatch):
    """A nested auto-sweep engine does not own the handlers, so an unexpected
    exception re-raises for the outer engine to record — it still persists its
    own traceback and tears down its agent session, but records no run-crash."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # pretend an outer engine owns signals
    try:
        with pytest.raises(RuntimeError):
            engine.run()
    finally:
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).crashed is False  # owner records it, not us
    assert killed == ["test-run"]
    assert (engine.run_dir / "crash.txt").read_text()  # traceback still persisted
    journal = engine.run_dir / "journal.jsonl"
    assert not journal.exists() or "run-crash" not in journal.read_text()


def test_run_crash_after_finish_clears_finished(project, monkeypatch):
    """A post-loop step that throws after finished=True is recorded as a crash
    and the finished flag is cleared, so status classification reads CRASHED
    rather than FINISHED (which it checks first)."""
    from bmad_loop.tui import data

    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])  # loop completes → sets finished=True

    def boom():
        raise RuntimeError("post-run boom")

    monkeypatch.setattr(engine, "_gc_run_worktrees", boom)

    summary = engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crashed is True
    assert state.finished is False  # the masking flag was cleared
    assert "Traceback" in (engine.run_dir / "crash.txt").read_text()
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    assert summary.crashed is True
    # the real payoff: it classifies as CRASHED, not FINISHED
    assert (
        data._classify(state.finished, state.paused, state.stopped, state.crashed, engine.run_dir)
        == data.CRASHED
    )


def test_top_level_crash_without_signal_handlers_still_records(project, monkeypatch):
    """A top-level engine that could not install signal handlers (e.g. off the
    main thread) is not nested, so an unexpected exception is recorded rather
    than re-raised — the crash-gap stays closed in non-CLI usage paths."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])
    # simulate signal.signal failing: no handlers installed, no owner, not nested
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)

    summary = engine.run()  # must NOT raise even though _owns_signals is False

    state = load_state(engine.run_dir)
    assert engine._is_nested is False
    assert engine._owns_signals is False
    assert state.crashed is True
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    assert summary.crashed is True


def test_crash_message_fallback_when_str_raises(project, monkeypatch):
    """If the exception's own __str__ raises, the fallback uses the bare type
    name (not its repr) so crash_error reads 'BadStr: BadStr', not quoted."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])

    class BadStr(Exception):
        def __str__(self):
            raise ValueError("nope")

    monkeypatch.setattr(engine, "_loop", lambda: (_ for _ in ()).throw(BadStr()))

    engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crash_error == "BadStr: BadStr"
    assert "'" not in state.crash_error


def _escalate_blocked(project, story_key):
    """A dev session that HALTs `blocked` with a spec on disk (so rearm can flip
    it) — the environmental-block shape from the live Epic-9 run."""

    def effect(spec):
        sp = spec_path(project, story_key)
        write_spec(sp, "blocked", rev_parse_head(project.project))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "escalations": [
                    {"type": "blocked", "severity": "CRITICAL", "detail": "unity bridge wedged"}
                ],
            },
        )

    return effect


def test_resume_with_epic_filter_stays_in_scoped_epic(project):
    """Regression for the Epic-9 jump: a `--epic 9` run whose first story (9-0,
    story index 0) escalates, is resolved, and resumes must keep picking WITHIN
    epic 9 — not widen to every epic and bounce to an earlier-in-file epic. The
    fixture is document-ordered (epic 5 before epic 9), not numeric, exactly like
    the real sprint board."""
    write_sprint(
        project,
        {
            "epic-5": "backlog",
            "5-1-map": "ready-for-dev",
            "epic-9": "backlog",
            "9-0-test-infra": "ready-for-dev",  # story numbered 0, leads the epic
            "9-1-keystone": "ready-for-dev",
        },
    )
    engine, _ = make_engine(project, [_escalate_blocked(project, "9-0-test-infra")], epic_filter=9)
    engine.state.epic_filter = 9  # cmd_run persists the launch scope; mirror it here
    summary = engine.run()
    assert summary.paused and summary.escalated == 1
    assert engine.state.current_epic == 9

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step
    resumed, _ = resume_engine(
        project,
        engine,
        [
            dev_effect(project, "9-0-test-infra"),
            review_effect(project, "9-0-test-infra", clean=True),
            dev_effect(project, "9-1-keystone"),
            review_effect(project, "9-1-keystone", clean=True),
        ],
    )
    summary2 = resumed.run()

    # both epic-9 stories completed; epic 5 never touched; no false boundary
    assert summary2.done == 2 and not summary2.paused
    assert resumed.state.tasks["9-0-test-infra"].phase == Phase.DONE
    assert resumed.state.tasks["9-1-keystone"].phase == Phase.DONE
    assert "5-1-map" not in resumed.state.tasks
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "epic-boundary" not in kinds


def test_pick_next_prefers_current_epic_over_earlier_file_position(project):
    """Fix B (hardening): selection exhausts the current epic before advancing,
    even when an actionable story of another epic sits earlier in file order.
    Then, once the epic is exhausted, it falls back to file order — preserving
    document-order epic execution."""
    write_sprint(
        project,
        {
            "5-1-e5": "backlog",  # earlier in file, actionable, but NOT current epic
            "9-0-x": "ready-for-dev",
            "9-1-y": "backlog",
        },
    )
    engine, _ = make_engine(project, [])
    engine.state.current_epic = 9
    engine.state.tasks["9-0-x"] = StoryTask(story_key="9-0-x", epic=9, phase=Phase.DEFERRED)

    assert engine._pick_next().key == "9-1-y"  # stays in epic 9, not 5-1-e5

    # exhaust epic 9 → fallback returns the earlier-in-file epic (doc order kept)
    engine.state.tasks["9-1-y"] = StoryTask(story_key="9-1-y", epic=9, phase=Phase.DONE)
    assert engine._pick_next().key == "5-1-e5"


def test_resolved_redrive_reescalates_instead_of_deferring(project):
    """Fix C (Bug 1): a story from a human-resolved CRITICAL escalation whose
    re-drive still can't converge must RE-ESCALATE (pause for the human), not
    silently plateau-defer + roll back the work. The live run downgraded an
    environmental block to a deferral this way."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [_escalate_blocked(project, "1-1-a")], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    rearm_escalation(engine.run_dir)  # human resolved; re-drive re-armed
    # re-drive never reaches `done` (env still blocked): both attempts land at
    # in-progress with no escalation — the exact non-convergence that used to defer
    resumed, _ = resume_engine(
        project,
        engine,
        [
            dev_effect(project, "1-1-a", final_status="in-progress"),
            dev_effect(project, "1-1-a", final_status="in-progress"),
        ],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.paused and summary2.escalated == 1 and summary2.deferred == 0
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.defer_reason is None
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "story-deferred" not in kinds
    saved = load_state(resumed.run_dir)
    assert "re-escalating instead of deferring" in saved.paused_reason
