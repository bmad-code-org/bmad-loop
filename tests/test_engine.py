"""Engine scenario tests against the mock adapter — no tmux, no LLM."""

import dataclasses
import hashlib
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest
from conftest import (
    _file_exists_cmd,
    _spec_baseline,
    committing_crash_state,
    dev_effect,
    fault_read_text,
    generic_dev_effect,
    git,
    review_effect,
    set_sprint,
    spec_path,
    write_ledger,
    write_spec,
    write_sprint,
)

from bmad_loop import deferredwork, platform_util, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine, RunPaused, RunStopped, _run_depth
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
    ReviewPolicy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    VerifyPolicy,
)
from bmad_loop.runs import STOP_REQUEST_FILE, graceful_stop_requested, rearm_escalation
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


def _notify_engine(project):
    return make_engine(
        project,
        [],
        policy=Policy(gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=True, file=True)),
    )[0]


def test_warn_desktop_notifier_inert_journals_and_prints(project, monkeypatch, capsys):
    """#231: at run start, notify.desktop requested + no platform notifier ->
    one journal event and a stderr warning, so an unattended launch that skipped
    `validate` still learns the desktop channel is dead."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" in kinds
    assert "warning: notify.desktop is set" in capsys.readouterr().err


def test_warn_desktop_notifier_inert_noop_when_available(project, monkeypatch, capsys):
    """A resolvable notifier means notify.desktop works here — no warning, no event."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: "osascript")

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


def test_warn_desktop_notifier_inert_noop_when_desktop_off(project, monkeypatch, capsys):
    """notify.desktop off → nothing to warn about, even with no platform notifier."""
    engine = make_engine(
        project,
        [],
        policy=Policy(
            gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=False, file=True)
        ),
    )[0]
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


def test_warn_desktop_notifier_inert_no_file_channel_guidance(project, monkeypatch, capsys):
    """desktop requested + no notifier + notify.file off → the warning says no alert
    channel is configured rather than pointing at an ATTENTION file never written."""
    engine = make_engine(
        project,
        [],
        policy=Policy(
            gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=True, file=False)
        ),
    )[0]
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    err = capsys.readouterr().err
    assert "no alert channel is configured" in err
    assert "ATTENTION file" not in err


def _stub_run_side_effects(engine, monkeypatch):
    """No-op the git/loop/session side effects of Engine.run() so a test can drive
    the pre_run -> warn -> post_run path in isolation."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    for name in ("_loop", "_ensure_target_branch", "_prune_preserve_refs", "_gc_run_worktrees"):
        monkeypatch.setattr(engine, name, lambda *a, **k: None)


def test_run_warns_when_toplevel_owns_no_signals(project, monkeypatch, capsys):
    """#231/finding-4: a top-level run that owns no signals (e.g. off the main
    thread, where signal handlers cannot install) is still depth-0 / non-nested, so
    the inert-notifier warning fires. Guards the fix that keys the gate on _run_depth
    rather than signal ownership (which was wrongly silencing off-main-thread runs)."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)
    _stub_run_side_effects(engine, monkeypatch)
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)  # owns no signals

    engine.run()

    assert engine._owns_signals is False
    assert engine._is_nested is False
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" in kinds
    assert "warning: notify.desktop is set" in capsys.readouterr().err


def test_run_suppresses_warning_when_nested(project, monkeypatch, capsys):
    """A nested run (call-stack depth > 0) suppresses the once-per-run warning even
    with no notifier — it belongs to the owning top-level run, not the child sweep.
    Nesting is depth-based, so it holds even when the child owns no signals."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)
    _stub_run_side_effects(engine, monkeypatch)
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)

    token = _run_depth.set(1)  # simulate an outer engine's run() frame
    try:
        engine.run()
    finally:
        _run_depth.reset(token)

    assert engine._is_nested is True
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


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
    # the session still ends in the journal, with its real status — only the
    # usage total is lost with the failed read
    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "completed"
    assert ends[0]["tokens"] is None
    # null, never 0: a zeroed TokenUsage weighs 0, and untracked != free. This
    # is also the path where `usage` is unbound in the finally, so a weighted
    # figure derived from it there would raise NameError instead of journaling.
    assert ends[0]["tokens_weighted"] is None


def test_run_session_journals_exactly_one_session_end(project):
    """The finally-fallback must not double-journal a session that already
    ended on the happy path."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "completed"
    assert "fired_at" not in ends[0]  # no timeout → no forensics fields


def test_adapter_crash_journals_aborted_session_end(project, monkeypatch):
    """adapter.run raising must not leave the session open forever in the
    journal: run-crash records the run, the aborted session-end the session."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)

    def explode(_spec):
        raise RuntimeError("transport died")

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [explode])

    summary = engine.run()

    assert summary.crashed
    entries = engine.journal.entries()
    crashes = [e for e in entries if e["kind"] == "run-crash"]
    assert crashes and crashes[0]["error"] == "RuntimeError"
    ends = [e for e in entries if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert "1-1-a" in ends[0]["task_id"]
    assert ends[0]["status"] == "aborted"
    assert ends[0]["error"] == "RuntimeError"
    # No usage read ever happened, so neither token field appears at all.
    # The invariant is `tokens_weighted` present iff `tokens` present — a lone
    # null weighted here would imply a read that came back empty.
    assert "tokens" not in ends[0]
    assert "tokens_weighted" not in ends[0]


def test_timeout_session_end_carries_fire_forensics(project):
    """A timed-out session's end entry records when the timeout fired, the
    fire→journal teardown gap, and which clock had expired."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    fired = time.time() - 5.0
    engine, _ = make_engine(
        project,
        [
            SessionResult(
                status="timeout",
                timeout_fired_at=fired,
                timeout_expired_clock="monotonic",
            )
        ],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "timeout"
    assert ends[0]["fired_at"] == fired
    assert ends[0]["teardown_s"] >= 0
    assert ends[0]["expired_clock"] == "monotonic"


def test_session_timeout_s_env_override(monkeypatch):
    """BMAD_LOOP_SESSION_TIMEOUT_S overrides limits.session_timeout_min*60 — the
    deterministic-E2E seam for the #157 timeout path, whose 1-minute policy floor
    is too coarse to exercise in a fast real-binary run. Only a positive, parseable
    value wins; anything else falls back so a fat-fingered env can never silently
    shorten a real run's budget."""
    monkeypatch.delenv("BMAD_LOOP_SESSION_TIMEOUT_S", raising=False)
    assert Engine._session_timeout_s(5400.0) == 5400.0  # unset -> policy default
    monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", "2.5")
    assert Engine._session_timeout_s(5400.0) == 2.5  # positive -> override wins
    for bad in ("0", "-1", "nonsense", ""):
        monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", bad)
        assert Engine._session_timeout_s(5400.0) == 5400.0  # ignored -> fall back


def test_session_timeout_env_override_flows_into_spec(project, monkeypatch):
    """The override reaches the SessionSpec the adapter actually receives, not
    just the helper — so a sub-minute E2E can drive the real timeout path."""
    monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", "3")
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(
                status="timeout", timeout_fired_at=time.time(), timeout_expired_clock="both"
            )
        ],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    assert adapter.sessions[0].timeout_s == 3.0  # not the 90-min policy default


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
    entries = engine.journal.entries()
    stops = [i for i, e in enumerate(entries) if e["kind"] == "run-stop"]
    assert stops and entries[stops[0]]["reason"] == "KeyboardInterrupt"
    # the interrupted session is closed out in the journal before the stop
    ends = [i for i, e in enumerate(entries) if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert entries[ends[0]]["status"] == "aborted"
    assert entries[ends[0]]["error"] == "KeyboardInterrupt"
    assert ends[0] < stops[0]


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
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    finally:
        _run_depth.reset(token)
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


def _final_review_cycle_policy() -> Policy:
    # max_review_cycles=1 → the first (and only) review cycle IS the final one,
    # so a crash in its post-session window lands the resume with
    # review_cycle already == the budget ceiling.
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_review_cycles=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )


def test_resume_final_review_cycle_replays_clean_result(project):
    """A host death in the post-session window of the *last* allowed review
    cycle must still consume the durably-recorded clean pass: the resume
    continuation enters the loop even though review_cycle already == the budget,
    so the story reaches DONE instead of dropping a recorded clean review to
    defer. Regression guard for the final-cycle replay edge from #62."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=_final_review_cycle_policy(),
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # the (final) review session's post_session
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_RUNNING
    assert crashed.review_cycle == 1  # already at the budget ceiling
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # the replay did not burn an extra cycle
    assert adapter.sessions == []  # nothing re-run — the recorded pass was replayed
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds


def test_resume_final_review_cycle_dirty_replay_defers_without_extra_budget(project):
    """The same final-cycle replay for a non-convergent review consumes the
    recorded pass, then the loop exits on the normal budget guard — no fresh
    session, no extra cycle — and the story defers. Proves the relaxed guard
    burns no extra budget once the replayed result is consumed."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, finalized=False),
        ],
        policy=_final_review_cycle_policy(),
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].review_cycle == 1

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DEFERRED
    assert final.review_cycle == 1  # replay consumed; no extra budget burned
    assert adapter.sessions == []  # loop exited without a fresh session
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds


def _dev_verify_crash_state(project, engine, spec_status: str) -> tuple[str, Path]:
    """Persist the exact state.json shape from issue #100: a task at DEV_VERIFY
    with no verified spec (verify had not passed when the host died), whose
    completed dev session record — result on disk, commits above baseline — is
    durable. Returns (baseline, spec_path)."""
    baseline = rev_parse_head(project.project)
    # the attempt committed its work above baseline, as the reporter's session
    # did (only the work — sweeping the still-untracked sprint board into the
    # commit would make a later baseline reset delete it)
    src = project.project / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(project.project, "add", "src.txt")
    git(project.project, "commit", "-q", "-m", "attempt work for 1-1-a")
    sp = spec_path(project, "1-1-a")
    write_spec(sp, spec_status, baseline)

    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY, attempt=1)
    task.baseline_commit = baseline
    task.baseline_untracked = []
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    return baseline, sp


def test_resume_dev_verify_replays_recorded_dev_result_to_done(project):
    """#100: the host died after persisting DEV_VERIFY but before the decision's
    action completed — spec_file empty, completed/done dev record on disk,
    commits above baseline. Resume must replay that record through the normal
    verify/decide pipeline instead of demanding a manual rollback (`git reset
    --hard <baseline>`) of finished, possibly already-pushed work."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # the issue's environment: the production default, where the old
        # resume-restart arm paused with the destructive reset instruction
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    baseline, _sp = _dev_verify_crash_state(project, engine, "done")
    src = project.project / "src.txt"

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 1  # the replay burned no attempt budget
    assert final.commit_sha  # the field the issue found null — now stamped
    assert adapter.sessions == []  # nothing re-run — the record was replayed
    # the attempt's committed work survived (squashed into the story commit)
    assert "change for 1-1-a" in src.read_text()
    assert rev_parse_head(project.project) != baseline
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_dev_verify_replay_verify_still_failing_retries_normally(project):
    """When the replayed record's verify failure reproduces (the spec is still
    short of done), the replay re-enters the normal retry path — commits parked
    on a recovery ref, reset, then a fresh budgeted attempt — instead of
    resume-restart's evidence-blind discard."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])  # helper default: rollback ON
    _dev_verify_crash_state(project, engine, "in-progress")

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 2  # the replay burned no budget; the fresh attempt did
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert "attempt-commits-preserved" in kinds  # committed work parked, not orphaned


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="stalled"),
        # completed but without a recorded result (legacy state.json shape)
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed"),
    ],
)
def test_resume_dev_verify_record_incomplete_still_restarts(project, record):
    """DEV_VERIFY without a verified spec joins the replay matcher only for a
    completed record WITH a recorded result — anything less keeps today's
    resume-restart (no artifact re-scan, no loosening of completion authority)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY, attempt=1)
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


def test_resume_review_verify_replays_recorded_review(project):
    """A host death in the post-review-verify decision window (REVIEW_VERIFY
    persisted by the save right after the review session, decision not yet
    acted on) replays the recorded review pass instead of resume-restart's
    rollback — the same #100 window one phase later."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_review_result":
            raise RuntimeError("host died in the review decision window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_VERIFY
    assert crashed.review_cycle == 1
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # the replay burned no review-budget slot
    assert adapter.sessions == []  # neither dev nor review re-run
    entries = resumed.journal.entries()
    verifies = [e for e in entries if e["kind"] == "resume-verify"]
    assert verifies and verifies[-1]["role"] == "review"
    kinds = [e["kind"] for e in entries]
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_committing_finishes_commit_to_done(project):
    """#115: the host died after _commit persisted COMMITTING but before the
    DONE save stamped commit_sha. That phase matched no resume arm and fell
    through to resume-restart, rolling back (or pausing over) fully-verified
    work. Resume must finish the commit in place — without re-charging the
    pre_commit_gate workflows (the persisted phase is durable proof they
    passed) and without any fresh session."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # the issue's environment: the production default, where the old
        # resume-restart arm paused with the manual-recovery notice
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    baseline = committing_crash_state(project, engine)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 1 and final.review_cycle == 0  # no budget burned
    assert final.commit_sha == rev_parse_head(project.project) != baseline
    assert adapter.sessions == []  # no session re-run — gates included
    assert len(final.sessions) == 1  # only the pre-crash dev record
    # the whole attempt squashed into exactly one story commit above baseline
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert len(log.splitlines()) == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert worktree_clean(project.project)  # sprint board swept into the squash
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds and "story-done" in kinds
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_committing_post_squash_still_one_commit(project):
    """The other #115 crash state: finalize_commit completed just before the
    death (squashed commit at HEAD, clean tree) but the DONE save never landed.
    The re-drive must converge on exactly ONE commit above baseline — not stack
    a second squash — and stamp commit_sha at HEAD."""
    engine, _ = make_engine(project, [])
    baseline = committing_crash_state(project, engine, post_squash=True)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.commit_sha == rev_parse_head(project.project)
    assert adapter.sessions == []
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert len(log.splitlines()) == 1  # re-squash, not a stacked second commit
    assert worktree_clean(project.project)
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds
    assert "resume-restart" not in kinds


def test_resume_committing_git_error_escalates(project):
    """A commit failure during the re-drive escalates (COMMITTING→ESCALATED is
    the legal failure move) with the attempt chain intact at HEAD — never a
    silent rollback through resume-restart."""
    engine, _ = make_engine(project, [])
    committing_crash_state(project, engine)
    head_before = rev_parse_head(project.project)
    # a rejecting pre-commit hook makes finalize_commit's commit step fail
    hook = project.project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    resumed, _ = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.paused and summary.escalated == 1
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.ESCALATED
    # finalize's HEAD-restore preserved the attempt chain on the branch
    assert rev_parse_head(project.project) == head_before
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds
    assert "resume-restart" not in kinds


def test_reconcile_early_return_heals_stale_resumed_dict(project):
    """On the idempotent early-return path (frontmatter already at the success
    status), the reconcile still syncs a stale *resumed* result dict from the
    frontmatter — a resumed record is the pre-reconcile snapshot, so without the
    heal its `followup_review_recommended` gate would read the template default
    and silently skip the follow-up review."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        "---\ntitle: 'test'\ntype: 'feature'\nstatus: 'done'\n"
        "followup_review_recommended: true\nbaseline_commit: 'abc'\n---\n\n## Intent\n\ntest\n"
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    # the pre-reconcile snapshot persisted before the original run mutated its
    # in-memory dict: frontmatter template default status, no followup key.
    stale = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    engine._reconcile_generic_terminal_status(task, stale)

    assert stale["status"] == "done"  # synced from the finalized frontmatter
    assert stale["followup_review_recommended"] is True  # folded from the frontmatter
    # the idempotent path never rewrites the spec, so nothing is journaled
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]


def test_run_session_record_result_json_isolated_from_later_mutation(project):
    """The durable SessionRecord holds a defensive copy of result_json, so the
    in-place mutation `_reconcile_generic_terminal_status` performs on the live
    result after the session is recorded cannot retroactively rewrite the
    persisted snapshot."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, [SessionResult(status="completed", result_json={"workflow": "auto-dev"})]
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    result = engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)
    # reconcile-style in-place mutation of the live result AFTER it was recorded
    result.result_json["status"] = "done"
    result.result_json["followup_review_recommended"] = True

    assert task.sessions[-1].result_json == {"workflow": "auto-dev"}  # in-memory record
    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.sessions[-1].result_json == {"workflow": "auto-dev"}  # on-disk snapshot


def test_run_session_persists_result_json_only_for_resumable_roles(project):
    """Only dev/review sessions (never a label) are consumed on resume, so only
    they persist result_json; triage/sweep and labeled plugin-workflow records
    store None — the payload would be pure state.json bloat otherwise."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="completed", result_json={"role": "dev"}),
            SessionResult(status="completed", result_json={"role": "review"}),
            SessionResult(status="completed", result_json={"role": "triage"}),
            SessionResult(status="completed", result_json={"role": "labeled"}),
        ],
    )
    engine.adapters["triage"] = adapter  # SweepEngine registers this; wire it here
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1)
    engine._run_session(task, role="review", prompt="p", seq=1)
    engine._run_session(task, role="triage", prompt="p", seq=1)
    engine._run_session(task, role="dev", prompt="p", seq=1, label="tea-trace")

    by_id = {r.task_id: r.result_json for r in task.sessions}
    assert by_id["1-1-a-dev-1"] == {"role": "dev"}  # resumable → persisted
    assert by_id["1-1-a-review-1"] == {"role": "review"}  # resumable → persisted
    assert by_id["1-1-a-triage-1"] is None  # role not resumable → None
    assert by_id["1-1-a-tea-trace-1"] is None  # labeled → None


def test_run_session_labeled_task_id_capped_as_a_whole(project):
    """Two individually legal parts (story_key, plugin label) can compose past
    the Windows filename segment cap; the task_id is sanitized as one segment."""
    long_key = "k" * 110
    write_sprint(project, {long_key: "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key=long_key, epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1, label="l" * 110)

    (record,) = task.sessions
    assert len(record.task_id) <= platform_util.MAX_SEGMENT


@pytest.mark.parametrize("key", ["6-4:cli?list", "k" * 130])
def test_resumable_session_matches_sanitized_task_id(project, key):
    """Resume matching must compose the task_id byte-identically to what
    _run_session stored. A story key that sanitization actually changes (dirty
    chars, or a clean key whose composed id overflows the segment cap) would
    otherwise never match, and _finish_inflight would fall through to the
    destructive resume-restart instead of consuming the recorded result."""
    write_sprint(project, {key: "ready-for-dev"})
    engine, _ = make_engine(
        project, [SessionResult(status="completed", result_json={"status": "done"})]
    )
    task = StoryTask(story_key=key, epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1)
    # the post-save crash window: session recorded, phase still mid-dev
    task.phase = Phase.DEV_RUNNING
    task.attempt = 1

    resumable = engine._resumable_session(task)
    assert resumable is not None
    role, result = resumable
    assert role == "dev"
    assert result.result_json == {"status": "done"}


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
    # Both units ride the summary (#129). total_tokens stays raw; weighted is
    # the same figure the budget just declined to trip on, 7.75x smaller here.
    # Per task, not per session: 30k in + 10k out + round(1.2M * 0.1).
    assert summary.total_tokens == 2 * 620_000
    assert summary.weighted_tokens == 160_000
    journal_text = (run_dir / "journal.jsonl").read_text()
    assert "token-budget-exceeded" not in journal_text


def _cache_heavy_engine(project, *, snapshot_weight, live_weight, usage):
    """A one-story run whose live policy weight deliberately differs from the
    weight in its persisted policy snapshot, so tests can prove which one a
    display surface read."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(cache_read_weight=live_weight),
    )
    state = RunState(
        run_id="test-run",
        project=str(project.project),
        started_at="now",
        policy_snapshot={"limits": {"cache_read_weight": snapshot_weight}},
    )
    return Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
    )


def test_summary_weighted_reads_the_run_snapshot_not_live_policy(project):
    """Displays must be reproducible from state.json alone, because that is all
    the TUI, `bmad-loop status` and `diagnose` can see — sourcing the summary
    from live policy would make them disagree for the same run.

    The engine keeps the two in agreement by stamping the snapshot at every
    start (#189); this test forces them apart to prove which one is read, so it
    must keep constructing the divergence by hand rather than via resume.

    The two weights differ here precisely so the number identifies the source:
    0.5 (snapshot) -> 1,320; 1.0 (live policy) -> 2,320, i.e. raw.
    """
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=1.0,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    summary = engine.run()

    assert summary.total_tokens == 2 * 1160
    assert summary.weighted_tokens == 1320  # 200 + 100 + 20 + round(2000 * 0.5)

    # ...and the number a TUI observer computes from the persisted state, by the
    # same per-task route tui/widgets.py takes, is identical.
    reloaded = load_state(engine.run_dir)
    weight = reloaded.cache_read_weight()
    assert weight == 0.5
    assert sum(t.tokens.weighted_total(weight) for t in reloaded.tasks.values()) == 1320


def test_summary_weights_per_task_not_over_the_aggregate(project):
    """weighted_total rounds internally, so summing per task is NOT the same as
    weighting one aggregated TokenUsage — and the TUI sums per task (its header
    is the sum of the rows it shows). Weighting the aggregate instead would make
    the CLI and the TUI disagree, which is the bug class #129 exists to remove.

    Three tasks at cache_read=5, weight 0.1: per task round(0.5) = 0 under
    banker's rounding, so 30. Aggregated first: round(15 * 0.1) = 2, so 32.
    """
    engine = _cache_heavy_engine(project, snapshot_weight=0.1, live_weight=0.1, usage=TokenUsage())
    for key in ("1-1-a", "1-1-b", "1-1-c"):
        task = StoryTask(story_key=key, epic=1)
        task.tokens = TokenUsage(input_tokens=10, cache_read_tokens=5)
        engine.state.tasks[key] = task

    summary = engine.summary()

    assert summary.total_tokens == 45  # 3 x (10 + 5)
    assert summary.weighted_tokens == 30  # NOT 32


def test_run_summary_render_labels_both_units(project):
    """render() feeds stdout, the ATTENTION file and the desktop notification
    from one place, so this covers all three."""
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=0.5,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    rendered = engine.run().render()

    assert "1,320 weighted tokens (2,320 raw incl. cache reads)" in rendered
    # the bare, unlabeled figure the issue reported must be gone
    assert "2,320 tokens" not in rendered


def test_run_summary_render_untracked_usage_stays_one_plain_zero(project):
    """usage_parser = "none" profiles never report usage. Splitting that into
    "0 weighted tokens (0 raw incl. cache reads)" asserts free work twice."""
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    rendered = engine.run().render()

    assert "0 tokens" in rendered
    assert "weighted" not in rendered
    assert "raw" not in rendered


def test_session_end_journals_weighted_beside_raw(project):
    """Per-session weighted spend must be recoverable from the journal after
    the fact — `tokens` alone is a scalar the weight cannot be backed out of."""
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=0.5,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    engine.run()

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 2
    for end in ends:
        assert end["tokens"] == 1160
        assert end["tokens_weighted"] == 660  # 100 + 50 + 10 + round(1000 * 0.5)


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


# ------------------------------ mid-session token-budget guard (#158)


def _with_budget_weighted(effect, weighted):
    """Wrap a scripted effect so its result carries a tripped budget sample —
    the adapter sets budget_weighted on every post-trip exit, completed ones
    (a warn-mode trip, a wrap-up inside the grace) included."""

    def wrapper(spec):
        return dataclasses.replace(effect(spec), budget_weighted=weighted)

    return wrapper


def test_dev_over_budget_retries_then_defers(project):
    """over_budget rides the ordinary non-completed dev arm — RETRY while
    attempts remain, plateau-DEFER once exhausted — with zero escalation.py
    changes (#158)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            SessionResult(status="over_budget", budget_weighted=6_000_000),
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [d["action"] for d in decisions] == ["retry", "defer"]
    assert all("dev session over_budget" in d["reason"] for d in decisions)


def test_review_over_budget_retries_then_defers(project):
    """over_budget from a review session rides the same non-completed arm as
    stalled/crashed: review-retry while cycles remain, DEFER once the review
    budget is spent — zero escalation.py changes (#158)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),  # finalizes spec to done, recommends follow-up
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            SessionResult(status="over_budget", budget_weighted=5_500_000),
            SessionResult(status="over_budget", budget_weighted=6_000_000),  # budget spent
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "review session over_budget" in task.defer_reason
    retries = [e for e in engine.journal.entries() if e["kind"] == "review-retry"]
    assert len(retries) == 2
    assert all("review session over_budget" in r["reason"] for r in retries)


def test_session_end_journals_budget_extras_when_tripped(project):
    """A tripped session's session-end entry carries budget_weighted plus the
    cap and mode it was judged against (policy defaults: 4M / warn);
    untripped sessions carry none."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            _with_budget_weighted(dev_effect(project, "1-1-a"), 4_100_000),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert [e["status"] for e in ends] == ["over_budget", "completed", "completed"]
    assert ends[0]["budget_weighted"] == 5_000_000
    assert ends[0]["budget"] == 4_000_000
    assert ends[0]["budget_mode"] == "warn"
    # a tripped-but-completed session (warn mode / wrap-up in grace) carries
    # the extras too
    assert ends[1]["budget_weighted"] == 4_100_000
    # the untripped review session carries none
    assert "budget_weighted" not in ends[2]
    # ...but the ordinary usage fields are unconditional, which is the whole
    # distinction: budget_weighted = the guard's sample at trip time (only when
    # tripped); tokens_weighted = the end-of-session total (always, when usage
    # was read). They can legitimately differ on the same entry.
    assert "tokens_weighted" in ends[2]


def test_engine_threads_budget_policy_into_session_spec(project):
    """Every session the engine drives gets the [limits] budget knobs on its
    SessionSpec — the stall_nudges_cap threading pattern."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(
            session_budget_mode="warn",
            max_tokens_per_session=123_456,
            session_budget_grace_s=7,
            cache_read_weight=0.25,
        ),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()

    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    for spec in adapter.sessions:
        assert spec.token_budget == 123_456
        assert spec.token_budget_mode == "warn"
        assert spec.token_budget_grace_s == 7.0
        assert spec.cache_read_weight == 0.25


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
    # No cache reads in the default fixture usage, so weighting is a no-op —
    # pins that the weighted path doesn't distort the ordinary case.
    assert summary.weighted_tokens == 30
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    assert adapter.sessions[0].env["BMAD_LOOP_MODE"] == "1"
    assert adapter.sessions[1].prompt.startswith("/bmad-dev-auto ")


def test_post_kill_rescued_result_flows_and_journals(project):
    """A result rescued by the adapter's post-kill reconcile (#61) reaches the
    engine as an ordinary completed result — it must flow the completed path
    (reconcile/verify/commit) unchanged, with the extra breadcrumb key riding
    along harmlessly — plus one forensic journal entry, since the rescue is
    otherwise indistinguishable from a live completion."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    inner = dev_effect(project, "1-1-a", followup_review=False)

    def rescued_dev(spec):
        result = inner(spec)
        # what GenericDevAdapter._post_kill_reconcile returns: a synthesized
        # result (status included) stamped with the rescue breadcrumb
        result.result_json["status"] = "done"
        result.result_json["post_kill_reconciled"] = True
        return result

    engine, _ = make_engine(project, [rescued_dev])
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "session-rescued-post-kill" in kinds


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


def test_reconcile_skips_and_journals_on_unreadable_spec(project, monkeypatch):
    """Reconcile is a bookkeeping *observation* pass over a spec the dev skill may
    still be writing. An OSError there used to crash the whole run; it now skips the
    pass and journals `spec-read-failed`. Skipping is safe: the deterministic verify
    gate re-reads the spec straight after and supplies the retry ladder."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "---\ntitle: 'x'\nstatus: 'in-progress'\n---\n\n## Auto Run Result\n\n- Status: done\n"
    )
    sp.write_text(original, encoding="utf-8")
    before = sp.read_bytes()  # snapshot: text-mode write newline-translates on Windows
    task = StoryTask(story_key="1-1-a", epic=1)
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    fault_read_text(monkeypatch, sp)

    engine._reconcile_generic_terminal_status(task, rj)

    assert sp.read_bytes() == before  # never written (repair skipped)
    assert rj["status"] == "in-progress"  # result dict untouched
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1
    assert events[0]["site"] == "reconcile"
    assert events[0]["story_key"] == "1-1-a" and events[0]["spec"] == str(sp)
    assert "PermissionError" in events[0]["error"]
    assert "spec-status-reconciled" not in [e["kind"] for e in engine.journal.entries()]


def test_reconcile_folds_followup_without_reread(project, monkeypatch):
    """`reset_spec_status` rewrites only the frontmatter status line, so a re-read
    after it could only return the followup flag the first read already carried —
    at the cost of a second racy read that can now fail. Exactly one frontmatter
    read, and the flag still folds into the live result dict."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        "---\ntitle: 'x'\nstatus: 'in-progress'\nfollowup_review_recommended: true\n---\n\n"
        "## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    calls, real = [], verify.read_frontmatter

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(verify, "read_frontmatter", counting)
    task = StoryTask(story_key="1-1-a", epic=1)
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    engine._reconcile_generic_terminal_status(task, rj)

    assert len(calls) == 1  # the deleted re-read stays deleted
    assert rj["status"] == "done"
    assert rj["followup_review_recommended"] is True  # folded from the single read
    assert verify.status_of(real(sp)) == "done"  # the repair write still happened


def test_post_dev_state_sync_skips_on_unreadable_spec(project, monkeypatch):
    """Same degrade for the sprint-board sync: an unreadable spec must not advance
    the board, and must not crash the run."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123")
    before = project.sprint_status.read_bytes()
    fault_read_text(monkeypatch, sp)

    engine._post_dev_state_sync(StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)})

    assert project.sprint_status.read_bytes() == before  # board not advanced
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "post-dev-sync"
    assert events[0]["story_key"] == "1-1-a"


# ------------------------------------------- closes_deferred auto-resolve (#234)


def _ledger_entries(project) -> dict:
    return {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }


def _vetoing_emit(engine, stage: str, action: str):
    """Wrap `engine._emit` so `stage` resolves to a plugin veto of `action`,
    without a real plugin on disk — `_emit` returns None on the zero-plugin fast
    path, so the context is built the same way the bus would."""
    from bmad_loop.plugins.context import Veto

    original = engine._emit

    def emit(s, task=None, **fields):
        ctx = original(s, task, **fields)
        if s != stage:
            return ctx
        ctx = ctx or engine._make_context(s, task, **fields)
        ctx.add_veto(Veto(action, "test veto", "test-plugin"))
        return ctx

    return emit


def _closes_deferred_run(project, dw_ids, *, ledger=None, **dev_kwargs):
    """A whole sprint-mode story run whose dev session declares `closes_deferred:
    dw_ids`, over a committed ledger. `followup_review=False` so the story takes
    the skip-review path straight to commit — these assert the *lifecycle*
    placement of the close, not the review loop.

    Everything is committed up front and the run is driven end to end, because
    the defect this covers (#234 review) only appears downstream of the dev
    session: the close used to land at dev-sync time, before verification, review
    and commit could still reject the story."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, ledger or {"DW-1": "open"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, closes_deferred=dw_ids, **dev_kwargs)],
    )
    return engine


def test_closes_deferred_marks_declared_entries_done(project):
    """A story spec declaring `closes_deferred:` flips each referenced ledger
    entry to `status: done <date>` + a `resolution:` note when the story commits —
    the write the loop never made, forcing retros to reconstruct closure by hand
    (#234). Closure is declared, never inferred from the diff."""
    engine = _closes_deferred_run(project, ["DW-1"])

    summary = engine.run()

    assert summary.done == 1
    entry = _ledger_entries(project)["DW-1"]
    assert entry.status.startswith("done") and not entry.open
    assert "resolution: resolved by story 1-1-a" in entry.body
    closed = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-closed"]
    assert len(closed) == 1
    assert closed[0]["dw_ids"] == ["DW-1"] and closed[0]["story_key"] == "1-1-a"


def test_closes_deferred_annotation_rides_the_story_commit(project):
    """The annotation must be IN the story's own commit, not left dirty behind it:
    the loop has to end each story on a clean tree (story N+1's step-01 HALTs on a
    dirty one), which is why closure happens just before `finalize_commit`'s
    `git add -A` rather than after the commit."""
    engine = _closes_deferred_run(project, ["DW-1"])

    engine.run()

    committed = git(project.project, "show", "HEAD", "--", str(project.deferred_work))
    assert "+status: done" in committed
    assert "+resolution: resolved by story 1-1-a" in committed
    assert worktree_clean(project.project)


def test_closes_deferred_stays_open_when_verify_defers_the_story(project):
    """The reviewer's repro (#234 review, finding 1). The dev session finalizes
    its spec — declaration and all — but the story then fails deterministic
    verification and defers. Closing at dev-sync time made that permanent: the
    in-place defer path snapshots the ledger AFTER the mutation and restores it
    over the rollback, so a rejected story left the ledger claiming its work
    resolved. At the commit boundary there is nothing to undo."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    # write_src=False: no diff since baseline, so the proof-of-work gate fails and
    # every attempt defers — the spec still reaches `done` with its declaration.
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", write_src=False, closes_deferred=["DW-1"])] * 3,
    )

    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert _ledger_entries(project)["DW-1"].open  # never claimed resolved
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "story-deferred-closed" not in kinds
    assert "story-deferred" in kinds  # the story really did defer


def test_closes_deferred_stays_open_when_the_story_escalates(project):
    """`_escalate` rolls nothing back — it advances to ESCALATED and pauses. A
    close written before that point survives into the operator's checkout, where
    the NEXT story's `git add -A` sweeps it into an unrelated commit."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(
        project, [dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])]
    )
    engine._emit = _vetoing_emit(engine, "pre_commit", "pause")

    summary = engine.run()

    assert summary.paused and summary.done == 0
    assert _ledger_entries(project)["DW-1"].open
    assert "story-deferred-closed" not in {e["kind"] for e in engine.journal.entries()}


def test_closes_deferred_idempotent_on_resume(project):
    """A host death in the commit window re-drives `_finalize_commit_phase` on
    resume, so the close runs twice. The second pass must write nothing and stay
    *silent*: an id that is present-but-already-done is a satisfied declaration,
    not an unmatched one."""
    engine = _closes_deferred_run(project, ["DW-1"])
    engine.run()
    task = load_state(engine.run_dir).tasks["1-1-a"]

    # re-drive the exact commit-phase step a resume performs
    engine._close_declared_deferred(task)

    body = _ledger_entries(project)["DW-1"].body
    assert body.count("resolution: resolved by story 1-1-a") == 1  # not doubled
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert kinds.count("story-deferred-closed") == 1  # only the first pass wrote
    assert "deferred-close-unmatched" not in kinds  # already-done is NOT unknown


def test_closes_deferred_warns_on_unknown_id(project):
    """An id absent from the ledger (a typo, or an entry since reworded) is
    journaled and dropped — never a story failure, and never a ledger write."""
    engine = _closes_deferred_run(project, ["DW-99"])
    before = project.deferred_work.read_bytes()

    summary = engine.run()

    assert summary.done == 1  # the annotation is traceability, not a gate
    assert project.deferred_work.read_bytes() == before  # ledger untouched
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-unmatched"]
    assert len(events) == 1
    assert events[0]["dw_ids"] == ["DW-99"] and events[0]["story_key"] == "1-1-a"
    assert "story-deferred-closed" not in [e["kind"] for e in engine.journal.entries()]


def test_closes_deferred_reports_a_malformed_ledger_entry(project):
    """An entry that exists but carries neither an `open` nor a `done` status
    cannot be marked. Reporting only present-vs-absent left that case in silence,
    which reads to the operator exactly like a successful close (#234 review)."""
    engine = _closes_deferred_run(project, ["DW-1"], ledger={"DW-1": "in-progress"})

    engine.run()

    assert not _ledger_entries(project)["DW-1"].status.startswith("done")
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-malformed"]
    assert len(events) == 1 and events[0]["dw_ids"] == ["DW-1"]


def test_closes_deferred_reports_a_wrong_container_declaration(project):
    """`closes_deferred: DW-1` (a scalar, not a list) is a real declaration of
    intent. Reading it as an empty list closed nothing and said nothing, while
    the same mistake in `stories.yaml` was a hard schema error — the two channels
    now agree that it is a mistake worth surfacing."""
    engine = _closes_deferred_run(project, "DW-1")  # bare scalar
    before = project.deferred_work.read_bytes()

    summary = engine.run()

    assert summary.done == 1  # still never a gate
    assert project.deferred_work.read_bytes() == before
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-malformed"]
    assert len(events) == 1 and "must be a list" in events[0]["error"]


def test_closes_deferred_noop_when_field_absent(project):
    """The default spec declares nothing: no ledger write and no journal noise on
    the close path every ordinary story takes."""
    engine = _closes_deferred_run(project, None)
    before = project.deferred_work.read_bytes()

    engine.run()

    assert project.deferred_work.read_bytes() == before
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert not kinds & {
        "story-deferred-closed",
        "deferred-close-unmatched",
        "deferred-close-malformed",
    }


def test_closes_deferred_refuses_an_out_of_tree_spec(project, tmp_path):
    """The sprint-mode spec path comes from the session's own result.json. A
    stale or hostile absolute path carrying `status: done` + `closes_deferred`
    must not be able to steer a ledger write — the same root-containment rule the
    frontmatter-status reconcile already applies to its one session-supplied
    path (#234 review, finding 4)."""
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(project, [])
    outside = tmp_path / "elsewhere" / "spec.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    write_spec(outside, "done", "abc123", closes_deferred=["DW-1"])
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(outside)

    engine._close_declared_deferred(task)

    assert _ledger_entries(project)["DW-1"].open
    events = [
        e for e in engine.journal.entries() if e["kind"] == "deferred-close-skipped-out-of-tree"
    ]
    assert len(events) == 1 and events[0]["story_key"] == "1-1-a"


def test_transient_spec_read_fault_does_not_crash_run(project, monkeypatch):
    """Integration capstone for #97. A single transient OSError on the spec — a
    TOCTOU truncation while the dev skill rewrites the file the orchestrator is
    reading back — used to escape to `engine.run()`'s `except Exception` and mark
    the WHOLE RUN crashed, abandoning every remaining story.

    The run now absorbs it: the first read (the reconcile bookkeeping pass) skips
    and journals, every later read succeeds against the real spec, and the story
    lands DONE. One fault, one journal event, no crash."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [generic_dev_effect(project, "1-1-a", followup_review=False)])
    sp = spec_path(project, "1-1-a")
    real, fired = Path.read_text, []

    def raise_once_then_delegate(self, *a, **kw):
        if self == sp and not fired:
            fired.append(self)
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", raise_once_then_delegate)
    summary = engine.run()

    assert fired  # the fault really fired (a green run proves nothing otherwise)
    assert not summary.crashed and summary.done == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "reconcile"


def _crash_replay_setup(project):
    """A host death that leaves the replay-fold's exact preconditions on disk.

    The dev session runs in the reconcile scenario (prose done, frontmatter
    lagging), so its durable record is the pre-reconcile snapshot
    `devcontract.synthesize_result` produces there: status "in-progress" and NO
    `followup_review_recommended` key (only written on a done synth). The host
    dies in the post-session window — phase persists as DEV_RUNNING — but only
    AFTER the original run's reconcile repaired the spec on disk (that write
    lands before the next state save), so the resumed reconcile enters the
    already-finalized branch whose re-fold is the sole carrier of the followup
    flag back onto the replay."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", final_status="in-progress", prose_status="done")

    def snapshot_effect(spec):
        result = inner(spec)
        result.result_json["status"] = "in-progress"
        del result.result_json["followup_review_recommended"]
        return result

    engine, _ = make_engine(project, [snapshot_effect])
    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.phase == Phase.DEV_RUNNING
    assert "followup_review_recommended" not in saved.sessions[0].result_json

    sp = spec_path(project, "1-1-a")
    sp.write_text(
        "---\ntitle: 'test'\ntype: 'feature'\nstatus: 'done'\n"
        f"baseline_revision: '{rev_parse_head(project.project)}'\n"
        "followup_review_recommended: true\n---\n\n## Intent\n\ntest spec\n"
        "\n## Auto Run Result\n\n- Status: done\n\nSummary: test.\n",
        encoding="utf-8",
    )
    return engine, sp


def test_resume_replay_fault_still_routes_recommended_review(project, monkeypatch):
    """The resume counterpart of the capstone above. A replayed dev result is a
    pre-reconcile snapshot with no followup key, and the reconcile re-fold is
    what restores it — a transient read fault used to drop that fold silently.
    The verify gate re-supplies only *status*, so the story committed with its
    recommended follow-up review skipped. Routing now re-derives from the
    finalized spec at consumption (`_followup_from_spec`): the fault costs one
    journal event, not the review."""
    engine, sp = _crash_replay_setup(project)
    real, fired = Path.read_text, []

    def raise_once_then_delegate(self, *a, **kw):
        if self == sp and not fired:
            fired.append(self)
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])
    monkeypatch.setattr(Path, "read_text", raise_once_then_delegate)
    summary = resumed.run()

    assert fired  # the reconcile read really faulted
    assert not summary.crashed and summary.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]  # routed, dev not re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "review-not-recommended" not in kinds
    events = [e for e in resumed.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "reconcile"


def test_resume_replay_persistent_fault_degrades_and_defers(project, monkeypatch):
    """When the fault outlives the routing fallback too, the degrade stays the
    decided one: routing falls back to False (journaled at site
    `followup-routing`), the verify gate's own faulted read turns each attempt
    into a retry, and the attempt budget lands the story in DEFERRED — never a
    crash, never a phantom review."""
    engine, sp = _crash_replay_setup(project)
    resumed, adapter = resume_engine(project, engine, [dev_effect(project, "1-1-a")])
    fault_read_text(monkeypatch, sp)
    summary = resumed.run()

    assert not summary.crashed and summary.done == 0
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DEFERRED
    assert [s.role for s in adapter.sessions] == ["dev"]  # the one budgeted retry
    sites = [e["site"] for e in resumed.journal.entries() if e["kind"] == "spec-read-failed"]
    assert "reconcile" in sites and "followup-routing" in sites


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


def test_review_launch_snapshot_threaded_into_session_spec(project):
    """The engine captures a launch-state SpecSnapshot right after the review
    marker strip and threads it onto the review SessionSpec; the dev session that
    precedes it carries none (#276 M1)."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    captured: dict[str, str] = {}

    def capturing_review(spec):
        # The engine captured the snapshot just before launching this session; the
        # spec on disk is still the dev pass's bytes until this effect rewrites it,
        # so hashing it here recomputes exactly what the snapshot recorded.
        sp = spec_path(project, "1-1-a")
        captured["digest"] = hashlib.sha256(sp.read_bytes()).hexdigest()
        return review_effect(project, "1-1-a", clean=True)(spec)

    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a"), capturing_review])
    summary = engine.run()

    assert summary.done == 1
    dev_spec, review_spec = adapter.sessions
    assert dev_spec.role == "dev" and dev_spec.spec_snapshot is None
    snap = review_spec.spec_snapshot
    assert snap is not None
    assert snap.path == str(spec_path(project, "1-1-a"))
    assert snap.fm_status == "done"
    assert snap.sha256 == captured["digest"]


def test_review_launch_snapshot_degrades_on_unreadable_spec(project):
    """A spec path that cannot be read degrades the snapshot capture to None and
    journals `spec-read-failed` at site `review-launch-snapshot`. A directory where
    a file is expected is the trigger: it slips past the strip's `is_file()` guard
    (the strip's raise-on-unreadable doctrine is untouched), then `read_bytes` raises
    IsADirectoryError, which the capture catches."""
    engine, _ = make_engine(project, [])
    bad = project.implementation_artifacts / "spec-1-1-a.md"
    bad.mkdir(parents=True, exist_ok=True)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(bad))

    snap = engine._reset_spec_for_review(task)

    assert snap is None
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert events and events[-1]["site"] == "review-launch-snapshot"
    assert events[-1]["story_key"] == "1-1-a"


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
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2
    # round 1 (clean=False, still recommends) spent one damping grant; round 2
    # converged on its own (clean=True), so damping never fired — this is normal
    # early convergence, not a damped force-converge.
    assert task.followup_reviews_spent == 1
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_review_strips_stale_auto_run_result_before_each_launch(project):
    """The review leg strips the prior pass's `## Auto Run Result` before every
    launch (issue #160). The dev pass leaves a real terminal marker on the done
    spec; left in place, the review's own entry write lifts it past the adapter's
    launch-mtime floor and the first result-less Stop reads it as this session's
    result — killing the review mid-flight. Each review effect asserts the on-disk
    spec carries no marker at ENTRY; both passes finalize with their own marker, so
    the cycle-2 entry assertion proves the strip runs per launch (not just once)
    while the final pass's marker legitimately survives on the committed spec (no
    later launch strips it)."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def review_cycle1(spec):
        sp = spec_path(project, "1-1-a")
        # the dev pass's marker must be gone by the time this session runs
        assert not devcontract.parse_auto_run_result(sp.read_text()).present
        baseline = _spec_baseline(sp)
        # finalize like review_effect, but leave our OWN terminal marker behind so
        # cycle 2 can prove it too is stripped before the next launch
        write_spec(sp, "done", baseline, prose_status="done")
        set_sprint(project, "1-1-a", "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": True,  # non-clean -> a cycle 2 runs
                "escalations": [],
            },
        )

    def review_cycle2(spec):
        sp = spec_path(project, "1-1-a")
        # cycle 1's own marker must be stripped too — proves per-launch stripping
        assert not devcontract.parse_auto_run_result(sp.read_text()).present
        baseline = _spec_baseline(sp)
        # this converging pass finalizes with its OWN marker, exactly as a real
        # bmad-dev-auto finalize does — nothing strips it after the last launch
        write_spec(sp, "done", baseline, prose_status="done")
        set_sprint(project, "1-1-a", "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": False,  # converges
                "escalations": [],
            },
        )

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", prose_status="done"), review_cycle1, review_cycle2],
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2
    # the FINAL pass's own marker legitimately survives — nothing launches after it,
    # so no strip runs; the strip is a before-launch guard, not a scrub-on-commit
    assert devcontract.parse_auto_run_result(spec_path(project, "1-1-a").read_text()).present


def test_budget_exhausted_finalized_work_commits(project):
    """A finalized story (status: done, sprint done, verify green) whose review
    pass keeps recommending an independent follow-up is COMMITTED when the review
    budget is exhausted — not rolled back. The lingering recommendation is
    re-filed as a fresh open deferred-work entry, and the run records the event."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # pin the damping cap high so this test exercises the max_review_cycles
    # exhaustion path (the damped force-converge has its own tests below).
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
        ),
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


def test_budget_exhausted_defer_reason_names_last_status(project):
    """The exhaustion defer reason reflects the last completed pass's real status
    (issue #160). Every review pass leaves the spec non-terminal (in-progress), so
    it never finalizes: the reason must name that status, not the fixed
    'still recommending a follow-up pass' text (which is only true of a finalized
    pass that keeps recommending one)."""
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

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "in-progress" in task.defer_reason
    assert "recommending a follow-up" not in task.defer_reason


def test_budget_exhausted_refileable_followup_keeps_followup_wording(project):
    """Exhaustion with a refileable follow-up whose rescue verify FAILS still uses
    the 'still recommending a follow-up pass' wording (issue #160). Every pass
    finalizes done + recommends a follow-up (refileable_followup), but the last
    pass's tree breaks the verify gate, so the exhaustion rescue's _verify_review
    fails and the commit-instead-of-rollback is skipped → defer. Because the last
    completed pass really did leave a lingering recommendation, the reason keeps the
    follow-up wording (the in-loop verify never runs for a followup-recommending
    pass, so the broken gate only surfaces in the rescue)."""
    marker = project.project / "review-budget.marker"
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_final_review(spec):
        marker.unlink()  # the last pass's tree no longer passes the verify gate
        return review_effect(project, "1-1-a", clean=False, patched=1)(spec)

    engine, _ = make_engine(
        project,
        [
            dev_with_marker,
            review_effect(project, "1-1-a", clean=False, patched=1),
            review_effect(project, "1-1-a", clean=False, patched=1),
            breaking_final_review,
        ],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
            verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        ),
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "still recommending a follow-up pass" in task.defer_reason


def test_budget_exhausted_finalized_but_verify_failed_wording(project):
    """Exhaustion where the last pass finalized (status: done, no follow-up) but its
    verify gate fails names the finalized-but-verification-failed mode (issue #160).
    The single review pass converges (done, no follow-up), so the in-loop verify
    runs and fails; with max_review_cycles == 1 there is no cycle left to run a fix
    session, so the loop exits and the exhaustion reason reflects last_status 'done'
    with no follow-up claim (refileable_followup is False here)."""
    marker = project.project / "review-verify.marker"
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def converged_but_broken_review(spec):
        marker.unlink()  # converges done, but the tree fails the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    engine, _ = make_engine(
        project,
        [dev_with_marker, converged_but_broken_review],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_review_cycles=1),
            verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        ),
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "finalized but its verification failed" in task.defer_reason
    assert "recommending a follow-up" not in task.defer_reason


def test_budget_exhausted_unreconciled_status_reads_as_unknown(project):
    """A completed pass whose status could not be resolved defers as 'unknown', not
    'no review pass completed' (issue #160). When a review result.json carries no
    `spec_file`, `_reconcile_generic_terminal_status` bails and leaves `rj` with no
    `status`, so `last_status` parses as "" — an empty-but-not-None value that means
    'a pass ran, its status was unreadable'. The defer reason must render that
    honestly (`''` != None), distinguishing it from the no-pass-ran case."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def statusless_review(spec):
        sp = spec_path(project, "1-1-a")
        # deliberately NO "status" and NO "spec_file": the reconcile returns early,
        # so `rj` never gains a status and the loop parses "" (a completed pass with
        # an unreadable/unreconciled status), not None (no pass ran)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "baseline_commit": _spec_baseline(sp),
                "escalations": [],
            },
        )

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), statusless_review, statusless_review, statusless_review],
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "'unknown'" in task.defer_reason
    assert "no review pass completed" not in task.defer_reason


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


def _damp_policy(max_followup_reviews: int) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_followup_reviews=max_followup_reviews),
    )


def test_followup_damping_converges_at_cap(project):
    """Default damping cap (1): a finalized story whose review keeps recommending
    an independent follow-up converges after honoring exactly ONE self-recommended
    follow-up. Round 1 spends the grant; round 2 (still recommending) is damped →
    verify, refile, commit. The 3rd scripted review never runs, and — being the
    expected steady state — the damped converge stays quiet (no ATTENTION)."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2  # dev + 2 review rounds; the 3rd scripted review unused
    assert task.followup_reviews_spent == 1  # exactly one grant honored
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" in kinds
    assert "review-budget-committed" not in kinds  # not the exhaustion path
    assert "story-deferred" not in kinds
    # the lingering follow-up is preserved as exactly one open DW entry
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1
    # damped convergence is the steady state — no review-budget ATTENTION notice
    # (the always-on run-finished notice is the only thing in the file).
    attention = engine.run_dir / "ATTENTION"
    assert not attention.exists() or "review budget reached" not in attention.read_text()
    # only dev + 2 review sessions ran (3rd scripted review never consumed)
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review"]


def test_followup_damping_cap_zero_converges_immediately(project):
    """Cap 0: the orchestrator never honors a pass's own follow-up. The first
    finalized round that still recommends one is damped immediately — verify,
    refile, commit — after a single review round, with nothing spent."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=False)],
        policy=_damp_policy(0),
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 1  # damped on the very first review round
    assert task.followup_reviews_spent == 0  # cap 0 grants nothing to spend
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" in kinds
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1


def test_nonterminal_rounds_do_not_spend_damping_cap(project):
    """A review round that does NOT finalize the spec (status stays non-terminal)
    consumes a review cycle but never spends a damping grant — only a finalized
    round that still recommends its own follow-up does. A later clean round then
    converges normally with the cap untouched."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, finalized=False),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2  # the non-terminal round still consumed a cycle
    assert task.followup_reviews_spent == 0  # but spent no damping grant
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_verify_fix_rounds_do_not_spend_damping_cap(project):
    """A clean review whose patch breaks the verify gate routes to a dev fix
    session and a fresh review cycle. Those verify-repair cycles are dev work, not
    honored follow-ups — they never spend the damping cap. Convergence lands with
    followup_reviews_spent == 0."""
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
        [dev_with_marker, breaking_review, fix, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2 and task.attempt == 2
    assert task.followup_reviews_spent == 0  # verify-repair never spends the cap
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_followup_damping_resume_replay_does_not_double_count(project):
    """A host death in the post-session window of the grant-spending review round
    must not double-count the damping spend on resume. The recorded round-1 result
    replays (re-deriving the spend), then round 2 damps and converges: the story
    reaches DONE with followup_reviews_spent == 1 (not 2) and exactly one refiled
    entry — append_entry's open-dedupe keeps a replayed refile from duplicating.
    Modeled on test_resume_final_review_cycle_replays_clean_result."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # cap 1, max_review_cycles 2: round 1 spends the grant, round 2 is damped.
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_review_cycles=2, max_followup_reviews=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False),  # round 1: spends the grant
            review_effect(project, "1-1-a", clean=False),  # round 2 (runs on resume): damped
        ],
        policy=policy,
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # crash in round 1's review post-session window
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_RUNNING
    # the round-1 spend had not persisted yet (increment is after the workflow gate,
    # saved only by the next cycle) — so the resume must re-derive it exactly once.
    assert crashed.followup_reviews_spent == 0
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=False)]
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 2
    assert final.followup_reviews_spent == 1  # re-derived once, never double-counted
    assert len(adapter.sessions) == 1  # only round 2 re-ran; round 1 was replayed
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1  # exactly one, even across the crash/replay


def _tail_death_review_effect(paths, story_key, *, followup: bool):
    """A review session that dies between writing terminal prose (## Auto Run
    Result: done) and flipping the frontmatter off the transient ``in-review``
    marker. The spec is left at ``in-review`` with the followup flag written but
    the prose already finalized; the synthesized result the orchestrator sees for
    such a non-done spec reports the (unflipped) frontmatter status and drops the
    followup key. Exercises the review-leg terminal-status reconcile."""

    def effect(spec):
        sp = spec_path(paths, story_key)
        baseline = _spec_baseline(sp)
        flag = "true" if followup else "false"
        sp.write_text(
            f"---\ntitle: 'test'\ntype: 'feature'\nstatus: 'in-review'\n"
            f"baseline_revision: '{baseline}'\nfollowup_review_recommended: {flag}\n---\n\n"
            "## Intent\n\ntest spec\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "in-review",  # frontmatter not yet flipped; synth reports it
                # NO followup_review_recommended key: synth drops it for a non-done spec
                "escalations": [],
            },
        )

    return effect


def test_review_leg_reconciles_finalize_tail_death_followup_false(project):
    """A review session that dies in its finalize tail — terminal prose says done
    but the frontmatter is stuck at the transient ``in-review`` marker — is repaired
    by the review leg's terminal-status reconcile (mirroring the dev leg at
    engine.py:1541). Without it the stale ``in-review`` frontmatter would fail the
    review-verify gate and burn a review cycle re-reviewing already-finished work.
    Here the finalized spec no longer recommends a follow-up (frontmatter followup
    false), so the reconciled ``done`` converges the loop on that first review
    round: one cycle, spec repaired to done on disk, one spec-status-reconciled
    (in-review -> done), nothing damped."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), _tail_death_review_effect(project, "1-1-a", followup=False)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 1  # converged on the reconciled first review round
    assert task.followup_reviews_spent == 0  # followup false -> no grant honored
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"
    # the spec on disk was repaired to done
    assert verify.status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    assert "review-followup-damped" not in [e["kind"] for e in engine.journal.entries()]


def test_review_leg_reconciles_finalize_tail_death_followup_true(project):
    """Finalize-tail death on a review pass that still recommends a follow-up: the
    reconcile advances in-review -> done AND re-attaches the frontmatter's followup
    flag (folded because the key is present), so the pass is treated as a
    finalized-but-still-recommending round rather than being re-reviewed from a
    stale ``in-review``. Under the default damping cap (1) that round spends the
    grant and loops; a second, clean review then converges normally. Three sessions
    total, exactly one spec-status-reconciled (only the tail-death round needed
    repair), and — converging on a clean pass — nothing is damped or refiled."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            _tail_death_review_effect(project, "1-1-a", followup=True),  # spends the grant
            review_effect(project, "1-1-a", clean=True),  # round 2 converges
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2
    assert task.followup_reviews_spent == 1  # the reconciled followup-true round spent it
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review"]
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"
    kinds = [e["kind"] for e in engine.journal.entries()]
    # normal early convergence on round 2 — not a damped/exhausted force-converge
    assert "review-followup-damped" not in kinds and "review-budget-committed" not in kinds
    ledger = project.deferred_work.read_text() if project.deferred_work.exists() else ""
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(ledger)
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert not open_refiled  # a clean convergence refiles nothing


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


def test_rollback_gate_git_fault_pauses_instead_of_crashing(project, monkeypatch):
    """#156: the dirty check timing out (now a GitError) must degrade to
    assume-dirty, so with rollback OFF the run pauses on the manual-recovery
    notice with the tree untouched — it must never crash the run."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    head = rev_parse_head(repo)

    def timing_out(*args, **kwargs):
        raise GitError(f"git diff timed out after 120s in {repo}")

    monkeypatch.setattr(verify, "attempt_dirty", timing_out)

    with pytest.raises(RunPaused):
        engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == head  # tree untouched
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-dirty-check-failed" in kinds
    assert "rollback-skipped-clean" not in kinds


def test_rollback_gate_git_fault_still_auto_recovers_when_on(project, monkeypatch):
    """rollback ON: an un-determinable dirty check still takes the auto-recover
    branch (preserve + reset) — the degrade must not turn the pause-free path
    into a pause."""
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
    (repo / "impl.txt").write_text("committed implementation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")

    def timing_out(*args, **kwargs):
        raise GitError(f"git diff timed out after 120s in {repo}")

    monkeypatch.setattr(verify, "attempt_dirty", timing_out)

    engine._rollback_or_pause(task)  # must not raise

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-dirty-check-failed" in kinds
    assert "rollback-auto" in kinds


def test_engine_applies_git_timeout_from_policy(project):
    """Engine construction pushes limits.git_timeout_s into the verify module so
    every git helper honors the operator's bound."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(git_timeout_s=7),
    )
    try:
        make_engine(project, [], policy=policy)
        assert verify._git_timeout_s == 7
    finally:
        verify.configure_git_timeout(verify.GIT_TIMEOUT_S)


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


def test_manual_recovery_notice_names_committed_work(project):
    """#100: rollback OFF + an attempt that COMMITTED above baseline. The pause
    notice must lead with saving/checking the commits — which may already be
    pushed — never with a bare `git reset --hard` that would discard them."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)
    task.baseline_untracked = []
    src = project.project / "src.txt"
    src.write_text(src.read_text() + "committed attempt work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "attempt commit")
    attempt_head = rev_parse_head(project.project)

    with pytest.raises(RunPaused) as paused:
        engine._rollback_or_pause(task)

    assert rev_parse_head(project.project) == attempt_head  # tree untouched
    reason = paused.value.reason
    assert "failed" not in reason  # a stopped attempt is not described as "failed"
    assert f"{task.baseline_commit[:12]}..HEAD" in reason
    assert "intact" in reason
    assert "pushed" in reason
    # save-the-commits comes before any reset instruction
    assert reason.index("branch my-rescue") < reason.index("reset --hard")
    manual = [e for e in engine.journal.entries() if e["kind"] == "rollback-manual-required"]
    assert manual and manual[-1]["commits"] == 1


def test_manual_recovery_notice_probe_failure_falls_back(project, monkeypatch):
    """The commits probe is advisory: a git fault must neither block the pause
    nor change the classic no-commits notice."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    baseline = rev_parse_head(project.project)

    def boom(repo, base):
        raise GitError("probe failed")

    monkeypatch.setattr(verify, "commits_above", boom)
    with pytest.raises(RunPaused) as paused:
        engine._pause_for_manual_recovery(task, baseline)

    assert "attempt was stopped" in paused.value.reason
    assert "manual rollback" in paused.value.reason.lower()


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


def test_rollback_emits_pre_and_post_around_reset(project):
    """A plugin (the Unity engine) hooks pre_rollback / post_rollback so it can
    quiesce a live Editor before the hard reset rewrites tracked files under it, and
    refresh after. pre_rollback must fire while the attempt tree is still checked out
    (HEAD == attempt); post_rollback only after the reset landed (HEAD == baseline)."""
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
    (repo / "impl.txt").write_text("committed implementation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    seen: list[tuple[str, str]] = []
    original_emit = engine._emit

    def spying_emit(stage, *args, **kwargs):
        if stage in ("pre_rollback", "post_rollback"):
            seen.append((stage, rev_parse_head(repo)))
        return original_emit(stage, *args, **kwargs)

    engine._emit = spying_emit
    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit
    # pre fires before the reset (attempt tree still open), post after it landed
    assert seen == [
        ("pre_rollback", attempt_head),
        ("post_rollback", task.baseline_commit),
    ]


def test_rollback_emits_are_observe_only(project):
    """The rollback emits are observe-only, like pre_worktree_teardown: the returned
    ctx is never routed through ``_vetoed``, so a failed Editor quiesce can never
    block or pause the rollback."""
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
    (repo / "src.txt").write_text("uncommitted tracked edit\n")

    routed: list = []
    original_vetoed = engine._vetoed

    def spying_vetoed(ctx, t):
        routed.append(ctx)
        return original_vetoed(ctx, t)

    engine._vetoed = spying_vetoed
    engine._rollback_or_pause(task)  # must not raise / pause

    assert rev_parse_head(repo) == task.baseline_commit  # reset still happened
    assert routed == []  # the rollback emits were never handed to the veto router


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


def test_rollback_preserve_ref_slug_survives_a_ref_illegal_run_id(project):
    """A `--run-id` carrying ref-illegal sequences must not drop the recovery ref.
    Characterization for the safe_ref_segment swap — the old inline alnum/`_-` slug
    also kept the ref legal; what this pins is the invariant (real git accepts the
    slugged ref and the snapshot stays reachable) plus the new digest-suffixed shape."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    engine.state.run_id = "story/1:2..3@{now}.lock"
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted edit\n")

    engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved")
    ref = entry["ref"]
    assert ref.startswith("refs/attempt-preserve-dirty/story_1_2__3_{now}.lock-")
    assert git(repo, "show", f"{ref}:src.txt") == "uncommitted edit"  # real git resolves it


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


# ------------------------------------------------------ deferred-artifact stash


def test_stash_deferred_artifacts_moves_spec_into_run_dir(project):
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("first attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    engine._stash_deferred_artifacts(task)

    dest = engine.run_dir / "deferred" / "1-1-a"
    assert (dest / sp.name).read_text(encoding="utf-8") == "first attempt\n"
    assert not sp.exists()
    stashed = [e for e in engine.journal.entries() if e["kind"] == "deferred-artifacts-stashed"]
    assert len(stashed) == 1 and stashed[0]["stashed_to"] == str(dest / sp.name)


def test_stash_deferred_artifacts_overwrites_a_prior_stash(project):
    """A story that defers a second time re-stashes the same filename: the newest
    spec wins, leaving no staging residue. Characterization — `shutil.move` also
    overwrote here (via its copy2 fallback); the tests below pin what changed."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("second attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    dest = engine.run_dir / "deferred" / "1-1-a"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / sp.name).write_text("first attempt\n", encoding="utf-8")

    engine._stash_deferred_artifacts(task)

    assert (dest / sp.name).read_text(encoding="utf-8") == "second attempt\n"
    assert not sp.exists()
    assert list(dest.iterdir()) == [dest / sp.name]  # the staging copy is gone


def test_stash_deferred_artifacts_survives_a_win32_sharing_violation(project, monkeypatch):
    """The real #101 hazard: an AV/indexer handle on the destination denies the
    rename. `shutil.move` caught that OSError and fell back to `copy2`, which opens
    that same locked target and fails too — crashing the defer flow. Routing through
    `atomic_replace` retries the replace until the handle clears."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("second attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    dest = engine.run_dir / "deferred" / "1-1-a"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / sp.name).write_text("first attempt\n", encoding="utf-8")

    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)
    calls, real_replace = {"n": 0}, os.replace

    def sharing_violation_once(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", sharing_violation_once)
    engine._stash_deferred_artifacts(task)

    assert calls["n"] == 2  # denied once, retried, landed
    assert (dest / sp.name).read_text(encoding="utf-8") == "second attempt\n"
    assert not sp.exists()
    assert list(dest.iterdir()) == [dest / sp.name]


def test_stash_deferred_artifacts_retries_a_locked_source_spec(project, monkeypatch):
    """Second half of the staged move. Windows denies the source *delete* against an
    AV/indexer handle just as it denies the rename-over, and `_defer` calls this
    before the rollback and the `story-deferred` journal append — so an unretried
    unlink would abort the whole deferral after the stash had already landed."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("work\n", encoding="utf-8")
    task.spec_file = str(sp)

    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)
    calls, real_unlink = {"n": 0}, os.unlink

    def locked_once(path, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        real_unlink(path)

    monkeypatch.setattr(platform_util.os, "unlink", locked_once)
    engine._stash_deferred_artifacts(task)

    assert calls["n"] == 2  # denied once, retried, removed
    assert not sp.exists()
    dest = engine.run_dir / "deferred" / "1-1-a"
    assert (dest / sp.name).read_text(encoding="utf-8") == "work\n"


def test_stash_deferred_artifacts_keeps_source_and_cleans_tmp_on_replace_failure(
    project, monkeypatch
):
    """The replace is the only step that can fail after staging: the source spec
    must survive it (the stash is forensic — losing the work is worse than a crash)
    and the staging copy must not linger."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("work\n", encoding="utf-8")
    task.spec_file = str(sp)

    def boom(_tmp, _target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("bmad_loop.engine.atomic_replace", boom)
    with pytest.raises(PermissionError):
        engine._stash_deferred_artifacts(task)

    assert sp.read_text(encoding="utf-8") == "work\n"
    assert list((engine.run_dir / "deferred" / "1-1-a").iterdir()) == []
    assert "deferred-artifacts-stashed" not in [e["kind"] for e in engine.journal.entries()]


# -------------------------------------------------- intent-gap patch-restore (#2564)


def _escalate_with_patch(project, story_key, patch_path):
    """A dev effect that halts on an intent gap the way bmad-dev-auto #2564 does:
    it saves the attempted change as a patch under the protected artifacts, reverts
    the tree, and escalates blocked."""

    def effect(spec):
        repo = project.project
        baseline = rev_parse_head(repo)
        src = repo / "src.txt"
        src.write_text("original\nattempted reading\n")  # the attempted change
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")  # save it
        src.write_text("original\n")  # revert before halting (tree back at baseline)
        sp = spec_path(project, story_key)
        write_spec(sp, "blocked", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "escalations": [
                    {
                        "type": "blocked",
                        "severity": "CRITICAL",
                        "detail": f"intent gap; saved patch: {patch_path}",
                    }
                ],
            },
        )

    return effect


def _restoring_dev_effect(project, story_key, seen):
    """A re-driven dev effect that records what the tree looked like when it ran (so a
    test can assert the restored diff was present) and leaves `src.txt` exactly as the
    restore laid it down — the applied patch IS this session's proof of work."""
    return dev_effect(project, story_key, followup_review=False, seen=seen, write_src=False)


def test_restore_patch_applies_onto_baseline(project):
    """_restore_patch re-lays the saved diff onto the baseline tree and journals
    attempt-restored, leaving the phase untouched on success."""
    engine, _ = make_engine(project, [])
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    src = repo / "src.txt"
    src.write_text("original\nattempted\n")
    patch = project.implementation_artifacts / "attempt.patch"
    patch.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")
    src.write_text("original\n")  # tree at baseline
    task.restore_patch = str(patch)
    task.phase = Phase.DEV_RUNNING

    engine._restore_patch(task)

    assert src.read_text() == "original\nattempted\n"
    assert task.phase == Phase.DEV_RUNNING  # success does not advance the phase
    assert "attempt-restored" in [e["kind"] for e in engine.journal.entries()]


def test_restore_patch_failure_escalates_without_dispatch(project):
    """A patch that will not apply escalates (never dispatches onto a half-restored
    tree): the task ends ESCALATED, attempt-restore-failed is journaled, and the
    success marker is not."""
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)
    patch = project.implementation_artifacts / "bad.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("--- a/src.txt\n+++ b/src.txt\n@@ -1 +1 @@\n-nope\n+x\n", encoding="utf-8")
    task.restore_patch = str(patch)
    task.phase = Phase.DEV_RUNNING

    with pytest.raises(RunPaused):
        engine._restore_patch(task)

    assert task.phase == Phase.ESCALATED
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "attempt-restore-failed" in kinds
    assert "attempt-restored" not in kinds


def test_intent_gap_restore_redrive_applies_patch_and_lands_done(project):
    """End-to-end: a resolved escalation with a restore patch re-applies the
    attempted change onto the baseline before the re-driven session runs, so the
    session resumes on the restored diff and the story lands done; the latch clears
    on commit."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    rearm_escalation(engine.run_dir, restore_patch=str(patch))  # human confirmed the reading
    sp = spec_path(project, "1-1-a")
    assert read_frontmatter(sp)["status"] == "in-review"  # routes step-01 -> step-04
    assert load_state(engine.run_dir).tasks["1-1-a"].restore_patch == str(patch)

    seen: list[str] = []
    resumed, _ = resume_engine(project, engine, [_restoring_dev_effect(project, "1-1-a", seen)])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert seen == ["original\nattempted reading\n"]  # the session saw the RESTORED code
    assert "attempt-restored" in [e["kind"] for e in resumed.journal.entries()]
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.restore_patch is None  # latch cleared on commit


def test_restore_redrive_prompt_points_at_the_spec(project):
    """The sprint-mode restore re-drive must dispatch an explicit spec-file
    pointer: only step-01's spec-pointer intent check EARLY EXITs on the
    `in-review` status (to step-04) BEFORE its version-control sanity check — a
    bare story key takes the freeform/epic path, whose dirty-tree check HALTs
    `blocked` on the very diff _restore_patch just laid onto the tree."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    rearm_escalation(engine.run_dir, restore_patch=str(patch))
    seen: list[str] = []
    resumed, adapter = resume_engine(
        project, engine, [_restoring_dev_effect(project, "1-1-a", seen)]
    )
    assert resumed.run().done == 1

    prompt = adapter.sessions[0].prompt
    spec = load_state(resumed.run_dir).tasks["1-1-a"].spec_file
    assert spec and f"`{spec}`" in prompt  # explicit pointer -> step-01 EARLY EXIT
    assert prompt != "/bmad-dev-auto 1-1-a"  # never the bare key on a restore


def test_intent_gap_restore_reapplies_after_mid_redrive_rollback(project):
    """A non-fixable retry inside the restore re-drive resets to baseline (clearing
    the restored code), so the patch is re-applied before the next dispatch; the
    saved patch file under the protected artifacts survives the reset."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1
    rearm_escalation(engine.run_dir, restore_patch=str(patch))

    seen: list[str] = []
    resumed, _ = resume_engine(
        project,
        engine,
        [SessionResult(status="stalled"), _restoring_dev_effect(project, "1-1-a", seen)],
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert seen == ["original\nattempted reading\n"]  # the surviving retry ran on the restored tree
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert kinds.count("attempt-restored") == 2  # applied, rolled back on stall, re-applied
    assert patch.is_file()  # protected patch file survived the reset


def test_intent_gap_restore_escalates_when_resolution_commits_overlap(project):
    """T2 (patch-restore x #78 baseline advance): re-arm adopts the resolve
    session's commits as the re-drive's baseline, but the saved patch was diffed
    from the ORIGINAL baseline — so a resolution commit that rewrote the patched
    lines makes the restore's `git apply` fail. The engine must escalate loudly
    with no session dispatched (never silently merge the human's resolution with
    the stale attempt), and the resolution commit must survive untouched."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    # the resolve session commits its own fix REWRITING the line the patch's
    # context expects, then the human latches the restore anyway
    repo = project.project
    (repo / "src.txt").write_text("corrected by resolution\n")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-q", "-m", "resolution: overlapping fix")
    rearm_escalation(engine.run_dir, restore_patch=str(patch))

    seen: list[str] = []
    resumed, _ = resume_engine(project, engine, [_restoring_dev_effect(project, "1-1-a", seen)])
    summary = resumed.run()

    assert summary.paused
    assert seen == []  # no session ever dispatched onto a half-restored tree
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "attempt-restore-failed" in kinds
    assert "attempt-restored" not in kinds
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED  # re-escalated: the human re-resolves
    # git apply is all-or-nothing: the overlapping resolution commit is untouched
    assert (repo / "src.txt").read_text() == "corrected by resolution\n"


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


# --------------------------------------------------- verify env faults (issue #126)


def test_verify_env_fault_pauses_dev_without_burning_budget(project):
    """rc 127 at the dev gate is the run environment's fault, not the story's:
    the run pauses at the first story instead of spending max_dev_attempts on
    repair sessions that cannot fix the environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=("exit 127",)),
    )
    # only one dev session scripted: a repair session must never be requested
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=policy)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.attempt == 1  # budget untouched beyond the one real session
    assert engine.state.paused_stage == PAUSE_ESCALATION
    assert "rc=127" in engine.state.paused_reason
    decision = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"][-1]
    assert decision["env_fault"] is True


def _self_disarming_script(project, name="check.sh"):
    """A verify script that passes once, then strips its own exec bit — the
    next invocation dies rc=126 (the issue's seeded-worktree fault, exactly)."""
    script = project.project / name
    script.write_text(f'#!/bin/sh\nchmod 644 "{script}"\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="sh env-fault exit codes (cmd reports 9009)")
def test_review_verify_env_fault_escalates_instead_of_fix_session(project):
    """An env fault at the review gate pauses the run — no fix session is
    dispatched and no review cycles are burned re-verifying a broken environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    script = _self_disarming_script(project)
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(f'"{script}"',)),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review"]  # no fix session
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "rc=126" in engine.state.paused_reason
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"][-1]
    assert failed["env_fault"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="sh env-fault exit codes (cmd reports 9009)")
def test_skip_review_env_fault_escalates_not_defers(project):
    """review.enabled = false: an env fault at the commit gates pauses the run
    instead of deferring the story as if its code were broken."""
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    script = _self_disarming_script(project)
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        verify=VerifyPolicy(commands=(f'"{script}"',)),
    )
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=policy)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]  # no fix session either
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "rc=126" in engine.state.paused_reason
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"][-1]
    assert failed["env_fault"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="sh env-fault exit codes (cmd reports 9009)")
def test_fix_phase_env_fault_escalates_instead_of_looping(project):
    """An env fault surfacing mid-fix-loop stops the loop — the remaining dev
    budget is not spent on repair sessions against an unfixable environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"
    script = project.project / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # ordinary fixable failure: routes to a fix session
        return review_effect(project, "1-1-a", clean=True)(spec)

    def env_breaking_fix(spec):
        marker.write_text("ok\n")
        script.chmod(0o644)  # the re-verify now dies rc=126 — env fault
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), f'"{script}"')),
        limits=LimitsPolicy(max_dev_attempts=3),  # budget left — must not be spent
    )
    engine, adapter = make_engine(
        project, [dev_with_marker, breaking_review, env_breaking_fix], policy=policy
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev"]  # one fix, then stop
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "rc=126" in engine.state.paused_reason
    fix = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"][-1]
    assert fix["env_fault"] is True


# ---------------------------- session-transport environment faults (#194) ----
#
# A dev/review/fix session whose coding CLI lost its API connection is classified
# env_fault by the adapter (part 1) and stamped on the SessionResult. These pin
# the story-pipeline consumption (part 2): the run PAUSEs — evidence journaled,
# worktree preserved — instead of charging the attempt, and re-arm restores the
# budget. Guard pins confirm plain (non-classified) failures still retry/defer.


def test_session_env_fault_pauses_dev_without_burning_budget(project):
    """A dev session classified an environment fault (#194) pauses the run at the
    first story rather than charging the attempt; the decision + session-end carry
    the evidence, and re-arm restores the budget (attempt -> 0)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    evidence = "API Error: Unable to connect (ECONNREFUSED)"
    engine, adapter = make_engine(
        project,
        [SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence)],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]  # no retry session burned
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.attempt == 1  # the one real session, not a spent budget
    assert engine.state.paused_stage == PAUSE_ESCALATION
    assert "environment fault: dev session timeout" in engine.state.paused_reason
    assert evidence in engine.state.paused_reason

    dec = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"][-1]
    assert dec["action"] == "pause"
    assert dec["env_fault"] is True
    end = [e for e in engine.journal.entries() if e["kind"] == "session-end"][-1]
    assert end["env_fault"] is True
    assert end["env_fault_evidence"] == evidence

    # the resolve workflow's re-arm step restores the attempt budget
    rearm_escalation(engine.run_dir)
    assert load_state(engine.run_dir).tasks["1-1-a"].attempt == 0


def test_two_plain_timeouts_still_defer(project):
    """Guard: NON-env-fault timeouts keep today's flow — two of them exhaust the
    dev budget and defer the story (the env-fault pause must not intercept them)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project,
        [SessionResult(status="timeout"), SessionResult(status="timeout")],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.escalated == 0 and not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]  # both attempts spent
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    dec = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert all(d["env_fault"] is False for d in dec)


def test_fix_phase_session_env_fault_escalates(project):
    """A fix session whose CLI lost its API connection (#194) escalates instead of
    burning the remaining dev budget on repair sessions that never ran."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"
    script = project.project / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # ordinary fixable failure -> routes to a fix session
        return review_effect(project, "1-1-a", clean=True)(spec)

    evidence = "API Error: Connection reset by peer"
    env_fault_fix = SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), f'"{script}"')),
        limits=LimitsPolicy(max_dev_attempts=3),  # budget left -> must not be spent
    )
    engine, adapter = make_engine(
        project, [dev_with_marker, breaking_review, env_fault_fix], policy=policy
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev"]  # one fix, then stop
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "environment fault: fix session timeout" in engine.state.paused_reason
    assert evidence in engine.state.paused_reason
    fix = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"][-1]
    assert fix["env_fault"] is True


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


def test_max_stories_survives_a_pause_resume(project):
    """A5 regression on the SPRINT path: the --max-stories dispatch gate consults
    the durable _dispatched_count(), which replaced a _loop-local counter that
    reset to 0 on every resume (the stories-mode fix rewired the shared base
    gate). With cap=2 and one story committed before an epic-boundary pause, a
    resume must dispatch exactly ONE more story — never re-fill the whole cap."""
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
            "2-2-c": "ready-for-dev",
        },
    )
    gated = Policy(gates=GatesPolicy(mode="per-epic"), notify=QUIET)
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=gated,
        max_stories=2,
    )
    engine.state.max_stories = 2  # cli.py persists this on RunState (helper gap: issue #84)
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
    final = load_state(resumed.run_dir)
    assert set(final.tasks) == {"1-1-a", "2-1-b"}  # 2-2-c never dispatched — cap durable


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


def test_sessions_stamp_resolved_adapter_identity(project):
    """#153 phase 1: every session's session-start journal entry and persisted
    SessionRecord carry the resolved adapter profile + model. A review-stage name
    override switches the profile and (per AdapterPolicy.resolved) resets the
    model to the CLI default "" because model is client-specific."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            review=StageAdapterPolicy(name="gemini"),
        ),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()

    starts = {e["role"]: e for e in engine.journal.entries() if e["kind"] == "session-start"}
    assert set(starts) == {"dev", "review"}
    expected_dev = policy.adapter.resolved("dev")
    expected_review = policy.adapter.resolved("review")
    # base model rides the dev stage; the review name switch resets it to ""
    assert (expected_dev.name, expected_dev.model) == ("claude", "opus")
    assert (expected_review.name, expected_review.model) == ("gemini", "")
    for role, expected in (("dev", expected_dev), ("review", expected_review)):
        entry = starts[role]
        assert entry["adapter"] == expected.name
        assert entry["model"] == expected.model
        assert entry["story_key"] == "1-1-a"

    saved = load_state(engine.run_dir)
    records = {r.role: r for r in saved.tasks["1-1-a"].sessions}
    assert (records["dev"].adapter, records["dev"].model) == ("claude", "opus")
    assert (records["review"].adapter, records["review"].model) == ("gemini", "")


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
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(RunStopped):
            engine.run()
    finally:
        _run_depth.reset(token)
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
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(RuntimeError):
            engine.run()
    finally:
        _run_depth.reset(token)
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


# --------------------------------------------------------------- graceful stop
#
# A graceful stop is delivered out-of-band via the stop-request.json control file
# (Phase 1's runs helpers): the engine consumes it at an item boundary and unwinds
# into a clean-finalization arm, leaving the run `stopped` + resumable. These tests
# lodge the control file directly (an existence read is all the engine checks) and
# drive it through the mock adapter — no signals, no live requester process.


def _lodge_stop_request(run_dir: Path) -> None:
    """Drop the graceful-stop control file the way a CLI/TUI requester would — the
    engine only checks its existence, so a minimal body suffices."""
    (run_dir / STOP_REQUEST_FILE).write_text(
        '{"requested_at": "2026-07-20T00:00:00", "mode": "graceful"}', encoding="utf-8"
    )


def _lodge_after(inner, run_dir: Path):
    """Wrap a scripted effect so the graceful-stop request lands mid-story (right
    as ``inner`` returns), proving the in-flight item still runs to completion —
    the boundary check only fires at the next loop head."""

    def effect(spec):
        result = inner(spec)
        _lodge_stop_request(run_dir)
        return result

    return effect


def test_graceful_stop_finishes_current_story_then_stops(project, monkeypatch):
    """The in-flight story runs to DONE (dev→review→commit), the next story is never
    dispatched, and the run ends `stopped` + resumable — not `finished`."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.DONE
    assert "1-2-b" not in saved.tasks  # story 2 never dispatched
    assert saved.stopped is True and saved.finished is False
    assert not graceful_stop_requested(run_dir)  # control file consumed at the boundary
    assert summary.done == 1 and not summary.paused
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-complete" not in kinds
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    assert stops[-1]["remaining"] == 1  # 1-2-b still actionable, never picked


def test_graceful_stop_runs_clean_finalization_and_notifies(project, monkeypatch):
    """Unlike a hard stop, the graceful arm runs worktree GC + the post_run hook +
    the policy-gated session teardown, and the trailing notify is worded for a
    graceful stop with a resume hint."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    notes = []
    monkeypatch.setattr(
        "bmad_loop.gates.notify",
        lambda policy, rd, title, message: notes.append((title, message)),
    )
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )

    gc_calls = []
    original_gc = engine._gc_run_worktrees
    monkeypatch.setattr(
        engine, "_gc_run_worktrees", lambda: (gc_calls.append(True), original_gc())[1]
    )
    post_run_stages = []
    original_emit = engine._emit

    def spy_emit(stage, *args, **kwargs):
        if stage == "post_run":
            post_run_stages.append(stage)
        return original_emit(stage, *args, **kwargs)

    engine._emit = spy_emit

    engine.run()

    assert gc_calls == [True]  # worktree GC ran on the graceful path
    assert post_run_stages == ["post_run"]  # post_run hook fired
    assert killed == ["test-run"]  # session torn down (owns_signals + cleanup_on_finish)
    assert notes and notes[-1][0] == "bmad-loop run stopped gracefully"
    assert "bmad-loop resume test-run" in notes[-1][1]
    assert "1 story remaining" in notes[-1][1]


def test_epic_boundary_auto_sweep_suppressed_by_graceful_stop(project, monkeypatch):
    """A graceful stop lodged during epic 1 ends the run at the next loop head,
    before the epic-2 pick — so the per-epic child sweep never fires and epic 2 is
    never dispatched."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
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
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
        sweep_factory=calls.append,
    )
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped and "2-1-b" not in saved.tasks
    assert calls == []  # no child sweep started
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-auto-trigger" not in kinds


def test_maybe_auto_sweep_suppressed_when_graceful_stop_pending(project):
    """The run-end race: a request landing after the loop-head check reaches
    _maybe_auto_sweep, which suppresses the sweep (return, not raise) and — because
    the guard precedes the sweeps_triggered append — leaves the trigger unrecorded
    so a later resume can still fire it."""
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [], policy=policy, sweep_factory=calls.append)
    _lodge_stop_request(run_dir)

    engine._maybe_auto_sweep("run-end", "run-end")

    assert calls == []  # no child sweep started
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-auto-suppressed" in kinds
    assert "sweep-auto-trigger" not in kinds
    assert "run-end" not in engine.state.sweeps_triggered  # unrecorded → resumable


def test_pause_wins_over_pending_graceful_stop(project, monkeypatch):
    """A pause raised while a stop is pending wins; the finally discards the stale
    control file and journals stop-request-discarded (no run-stop)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    def pausing_loop():
        _lodge_stop_request(run_dir)
        raise RunPaused("epic gate", PAUSE_EPIC_BOUNDARY, None)

    monkeypatch.setattr(engine, "_loop", pausing_loop)
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.paused and not saved.stopped
    assert not graceful_stop_requested(run_dir)  # discarded in finally
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "stop-request-discarded" in kinds
    assert "run-stop" not in kinds


def test_hard_stop_wins_over_pending_graceful_stop(project, monkeypatch):
    """A hard RunStopped (SIGTERM) supersedes a pending graceful request: the run
    records run-stop WITHOUT the graceful flag, tears the session down
    unconditionally, and the finally discards the stale file. Contrast the graceful
    arm: the hard path does not emit post_run."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    post_run_stages = []
    original_emit = engine._emit

    def spy_emit(stage, *args, **kwargs):
        if stage == "post_run":
            post_run_stages.append(stage)
        return original_emit(stage, *args, **kwargs)

    engine._emit = spy_emit

    def stopping_loop():
        _lodge_stop_request(run_dir)
        raise RunStopped()  # hard (graceful=False), as the signal handler raises

    monkeypatch.setattr(engine, "_loop", stopping_loop)
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped
    assert not graceful_stop_requested(run_dir)  # finally discarded it
    assert killed == ["test-run"]  # hard stop's unconditional teardown
    assert post_run_stages == []  # hard path skips the clean-finish subset
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and "graceful" not in stops[-1]
    assert "stop-request-discarded" in [e["kind"] for e in engine.journal.entries()]


def test_crash_wins_over_pending_graceful_stop(project, monkeypatch):
    """An unexpected crash while a stop is pending wins; the crash arm records and
    the finally discards the stale control file (no run-stop)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    def crashing_loop():
        _lodge_stop_request(run_dir)
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", crashing_loop)
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.crashed and not saved.stopped
    assert not graceful_stop_requested(run_dir)  # discarded in finally
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-crash" in kinds and "stop-request-discarded" in kinds
    assert "run-stop" not in kinds


def test_graceful_stop_finalize_error_still_records_stop(project, monkeypatch):
    """A post_run hook that raises during graceful finalization is caught inline
    (an except-arm raise would escape run() uncaught): the run still records
    run-stop + run-stop-finalize-error and never crashes."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    original_emit = engine._emit

    def boom_on_post_run(stage, *args, **kwargs):
        if stage == "post_run":
            raise RuntimeError("post_run plugin exploded")
        return original_emit(stage, *args, **kwargs)

    engine._emit = boom_on_post_run

    summary = engine.run()  # must NOT raise

    assert not summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.stopped and not saved.finished and not saved.crashed
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-stop-finalize-error" in kinds
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True


def test_resume_after_graceful_stop_completes_remaining(project, monkeypatch):
    """A graceful-stopped run is resumable with zero special handling: the resume
    dispatches the story that never ran and finishes."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    engine.run()
    assert load_state(engine.run_dir).stopped
    assert not graceful_stop_requested(run_dir)

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-2-b"), review_effect(project, "1-2-b", clean=True)],
    )
    summary2 = resumed.run()

    assert summary2.done == 2 and not summary2.paused
    final = load_state(resumed.run_dir)
    assert set(final.tasks) == {"1-1-a", "1-2-b"}
    assert final.finished and not final.stopped


def test_graceful_stop_on_resume_finishes_inflight_then_stops(project, monkeypatch):
    """A request pending when a resume starts is honored AFTER _finish_inflight: the
    in-flight item completes, then the first loop-head check stops the run before
    any new story is picked."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")])
    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.DEV_RUNNING

    # a graceful stop is lodged before the resume even starts
    _lodge_stop_request(run_dir)
    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])
    resumed.run()

    final = load_state(resumed.run_dir)
    assert final.tasks["1-1-a"].phase == Phase.DONE  # in-flight item finished
    assert "1-2-b" not in final.tasks  # no NEW pick after the boundary
    assert final.stopped and not final.finished
    assert not graceful_stop_requested(run_dir)  # consumed at the first loop head
    assert [s.role for s in adapter.sessions] == ["review"]  # only the in-flight review ran
    stops = [e for e in resumed.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True


# ------------------------------- review.on_timeout = "salvage-if-done" (#271)


def _salvage_policy(**limits_kw) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(**limits_kw),
        review=ReviewPolicy(on_timeout="salvage-if-done"),
    )


def test_review_timeout_salvage_commits_and_refiles(project):
    """A review timeout over an already-finalized, verify-green dev product
    converges: the work commits, the outstanding follow-up refiles to deferred
    work, and no further review cycle burns."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), SessionResult(status="timeout")],
        policy=_salvage_policy(),
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    task = engine.state.tasks["1-1-a"]
    (salvage,) = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]
    assert salvage["session_status"] == "timeout"
    assert salvage["cycle"] == 1
    assert salvage["reset_from"] is None  # the review never touched the done spec
    assert salvage["refiled"] and salvage["refiled"].startswith("DW-")
    ledger = project.deferred_work.read_text(encoding="utf-8")
    assert "origin: review-timeout-salvage" in ledger
    assert "1-1-a" in ledger
    # the timeout neither re-arms the recommendation nor spends a damping grant
    assert task.followup_review_recommended is False
    assert task.followup_reviews_spent == 0
    assert not [e for e in engine.journal.entries() if e["kind"] == "review-retry"]


def test_review_timeout_salvage_resets_in_review_spec(project):
    """The mid-review interrupt: the dying pass flipped the transient in-review
    marker. Salvage resets it forward to done and commits."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def timeout_mid_review(spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-review", _spec_baseline(sp))
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_mid_review],
        policy=_salvage_policy(),
    )
    summary = engine.run()

    assert summary.done == 1
    (salvage,) = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]
    assert salvage["reset_from"] == "in-review"
    fm = read_frontmatter(spec_path(project, "1-1-a"))
    assert str(fm.get("status")) == "done"


def test_review_timeout_salvage_verify_fail_falls_back(project):
    """A failing verify gate refuses the salvage (journaled) and falls back to
    the default retry/exhaust routing — never ships unverified work."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # green at dev-verify time; the dying review breaks it, so only the salvage
    # gate (not the dev gate) sees the failure
    marker = project.project / "verify-marker.txt"
    marker.write_text("ok", encoding="utf-8")
    policy = dataclasses.replace(
        _salvage_policy(max_review_cycles=1),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )

    def timeout_and_break_verify(spec):
        marker.unlink()
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_and_break_verify],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    (failed,) = [
        e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage-failed"
    ]
    assert failed["cycle"] == 1
    task = engine.state.tasks["1-1-a"]
    assert "salvage not applicable" in task.defer_reason
    assert not [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]


def test_review_timeout_salvage_nonterminal_spec_falls_back(project):
    """A spec at a non-terminal status (unfinished dev work) is never salvaged
    over — no salvage event at all, straight to the fallback routing."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def timeout_mid_work(spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-progress", _spec_baseline(sp))
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_mid_work],
        policy=_salvage_policy(max_review_cycles=1),
    )
    summary = engine.run()

    assert summary.deferred == 1
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-timeout-salvage" not in kinds
    assert "review-timeout-salvage-failed" not in kinds
    assert "salvage not applicable" in engine.state.tasks["1-1-a"].defer_reason


def test_review_timeout_salvage_skipped_under_isolation(project):
    """Worktree isolation: a defer already preserves the unit's worktree + diff,
    and committing into the main repo would be wrong — salvage never applies."""
    policy = dataclasses.replace(
        _salvage_policy(), scm=ScmPolicy(rollback_on_failure=True, isolation="worktree")
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec_path(project, "1-1-a")))
    assert engine._salvage_review_timeout(task, SessionResult(status="timeout")) is False


def test_review_timeout_salvage_requires_spec_file(project):
    engine, _ = make_engine(project, [], policy=_salvage_policy())
    task = StoryTask(story_key="1-1-a", epic=1)  # no spec recorded yet
    assert engine._salvage_review_timeout(task, SessionResult(status="timeout")) is False


def test_session_synthesized_from_frontmatter_journaled(project):
    """The adapter's missing-marker synthesis flag (#224) leaves the same style
    of forensic breadcrumb as a post-kill rescue."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a")

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        return result

    engine, _ = make_engine(
        project,
        [synthesized_dev, review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()
    assert summary.done == 1
    (crumb,) = [
        e for e in engine.journal.entries() if e["kind"] == "session-synthesized-from-frontmatter"
    ]
    assert crumb["role"] == "dev"


def test_synthesized_result_repairs_spec_marker(project):
    """#276 M3: when the fallback synthesizes a dev result the engine appends the
    marker the skill owed onto the on-disk spec — with provenance — and journals
    `spec-marker-repaired`. The story still completes; verify/commit are unaffected
    (the marker is prose; the frontmatter status the gates read is unchanged)."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # No follow-up review, so nothing rewrites the spec after the repair lands.
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"  # what synthesize_result would carry
        return result

    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    text = spec_path(project, "1-1-a").read_text()
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert devcontract.ORCHESTRATOR_SYNTH_NOTE in text
    (repaired,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert repaired["status"] == "done"
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]


def test_marker_repair_failure_is_best_effort(project, monkeypatch):
    """An OSError out of the append is swallowed (journaled `spec-marker-repair-
    failed`): the result was already synthesized, so the story still completes —
    only the on-disk spec is left non-compliant."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("bmad_loop.devcontract.append_auto_run_result", boom)
    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    (failed,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-failed"]
    assert "OSError" in failed["error"]


def test_synthesized_review_result_repairs_marker_and_story_completes(project):
    """#276 M3, review path: when the fallback synthesizes a REVIEW result the engine
    appends the marker onto the on-disk spec (with provenance) and the story still
    completes. Prior engine coverage only exercised a synthesized DEV result with no
    follow-up review — this covers the primary review-repair path."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    dev = dev_effect(project, "1-1-a", followup_review=True)
    review = review_effect(project, "1-1-a", clean=True)

    def synthesized_review(spec):
        result = review(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    engine, _ = make_engine(project, [dev, synthesized_review])
    summary = engine.run()

    assert summary.done == 1
    text = spec_path(project, "1-1-a").read_text()
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert devcontract.ORCHESTRATOR_SYNTH_NOTE in text
    (repaired,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert repaired["status"] == "done"
    synth = [
        e for e in engine.journal.entries() if e["kind"] == "session-synthesized-from-frontmatter"
    ]
    assert [e["role"] for e in synth] == ["review"]  # the REVIEW session synthesized


def test_synthesized_review_repair_survives_followup_rewrite(project):
    """The finding's core case: a synthesized review cycle repairs the marker, then a
    subsequent clean review cycle rewrites the whole spec (dropping the marker). The
    story still converges to done, the run does not livelock, and exactly one repair
    fired (the synthesized cycle) — the repair never fights the follow-up rewrite."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    dev = dev_effect(project, "1-1-a", followup_review=True)
    review1 = review_effect(project, "1-1-a", clean=False)  # recommends another review

    def synthesized_review1(spec):
        result = review1(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    review2 = review_effect(project, "1-1-a", clean=True)  # converges; rewrites the spec
    engine, _ = make_engine(project, [dev, synthesized_review1, review2])
    summary = engine.run()

    assert summary.done == 1
    repaired = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert len(repaired) == 1  # only the synthesized cycle repaired


def test_marker_repair_undecodable_spec_is_best_effort(project, monkeypatch):
    """`append_auto_run_result` reads raw bytes and raises `UnicodeDecodeError`
    (a `ValueError`, not an `OSError`) on an undecodable spec — a spec torn mid-
    write through a multi-byte UTF-8 sequence between the frontmatter read and the
    append. The best-effort repair must swallow it too (journaled
    `spec-marker-repair-failed`) so the run still completes rather than crashing."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    def boom(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("bmad_loop.devcontract.append_auto_run_result", boom)
    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    (failed,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-failed"]
    assert "UnicodeDecodeError" in failed["error"]


def test_marker_repair_skips_out_of_tree_spec(project, tmp_path):
    """A session-reported spec_file outside the orchestrator-owned roots is never
    written — the repair skips with reason `out-of-tree`, like the reconcile."""
    engine, _ = make_engine(project, [])
    outside = tmp_path / "outside-spec.md"  # sibling of the sandbox root, not under it
    outside.write_text("---\nstatus: done\n---\n\nbody\n", encoding="utf-8")
    task = StoryTask(story_key="1-1-a", epic=1)

    engine._repair_spec_marker(task, {"spec_file": str(outside), "status": "done"})

    (skipped,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]
    assert skipped["reason"] == "out-of-tree"
    assert "## Auto Run Result" not in outside.read_text()


def test_marker_repair_skips_on_fm_mismatch(project):
    """A fresh frontmatter read that no longer agrees with the synthesized status
    (here still `in-progress` while `rj` claims `done`) is refused with reason
    `fm-mismatch` and no marker is appended — never author an inconsistent one."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-progress", "abc123")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(sp))

    engine._repair_spec_marker(task, {"spec_file": str(sp), "status": "done"})

    (skipped,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]
    assert skipped["reason"] == "fm-mismatch"
    assert "## Auto Run Result" not in sp.read_text()
