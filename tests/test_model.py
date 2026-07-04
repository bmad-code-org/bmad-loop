"""RunState serialization + lifecycle-flag tests."""

import pytest

from bmad_loop.model import RunState, SessionRecord, StoryTask, TokenUsage


def _state(**kw) -> RunState:
    return RunState(run_id="r1", project="/p", started_at="now", **kw)


def _task_with_session(usage: TokenUsage | None = None) -> StoryTask:
    task = StoryTask(story_key="1-1-a", epic=1)
    task.record_session(
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed", usage=usage)
    )
    return task


def test_attach_session_usage_folds_usage_into_record_and_totals():
    task = _task_with_session()
    task.attach_session_usage("1-1-a-dev-1", TokenUsage(input_tokens=10, output_tokens=5))
    assert task.sessions[0].usage is not None
    assert task.sessions[0].usage.total == 15
    assert task.tokens.total == 15


def test_attach_session_usage_raises_on_unknown_task_id():
    task = _task_with_session()
    with pytest.raises(KeyError):
        task.attach_session_usage("nope", TokenUsage(input_tokens=1))


def test_attach_session_usage_is_noop_on_none():
    task = _task_with_session()
    task.attach_session_usage("1-1-a-dev-1", None)
    assert task.sessions[0].usage is None
    assert task.tokens.total == 0


def test_attach_session_usage_does_not_double_count_existing_usage():
    task = _task_with_session(usage=TokenUsage(input_tokens=10, output_tokens=5))
    task.attach_session_usage("1-1-a-dev-1", TokenUsage(input_tokens=100))
    assert task.sessions[0].usage.total == 15  # original usage kept
    assert task.tokens.total == 15


def test_session_record_result_json_round_trips():
    record = SessionRecord(
        task_id="1-1-a-dev-1",
        role="dev",
        status="completed",
        result_json={"workflow": "auto-dev", "status": "done"},
    )
    back = SessionRecord.from_dict(record.to_dict())
    assert back.result_json == {"workflow": "auto-dev", "status": "done"}


def test_session_record_result_json_defaults_none_for_legacy_state():
    doc = SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed").to_dict()
    del doc["result_json"]  # state.json from before the field existed
    assert SessionRecord.from_dict(doc).result_json is None


def test_followup_review_recommended_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, followup_review_recommended=True)
    assert StoryTask.from_dict(task.to_dict()).followup_review_recommended is True


def test_followup_review_recommended_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["followup_review_recommended"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).followup_review_recommended is False


def test_resolved_redrive_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, resolved_redrive=True)
    assert StoryTask.from_dict(task.to_dict()).resolved_redrive is True


def test_resolved_redrive_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["resolved_redrive"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).resolved_redrive is False


def test_stopped_round_trips():
    state = _state(stopped=True)
    assert RunState.from_dict(state.to_dict()).stopped is True


def test_stopped_defaults_false_for_legacy_state():
    doc = _state().to_dict()
    del doc["stopped"]  # a state.json written before the field existed
    assert RunState.from_dict(doc).stopped is False


def test_run_filters_round_trip():
    state = _state(epic_filter=9, story_filter="9-0", max_stories=3)
    back = RunState.from_dict(state.to_dict())
    assert (back.epic_filter, back.story_filter, back.max_stories) == (9, "9-0", 3)


def test_run_filters_default_none_for_legacy_state():
    doc = _state().to_dict()
    for key in ("epic_filter", "story_filter", "max_stories"):
        del doc[key]  # a state.json written before the fields existed
    back = RunState.from_dict(doc)
    assert back.epic_filter is None and back.story_filter is None and back.max_stories is None


def test_clear_pause_also_clears_stopped():
    state = _state(stopped=True, paused_reason="escalation", paused_stage="x")
    state.clear_pause()
    assert state.stopped is False
    assert state.paused is False


def test_crashed_round_trips():
    state = _state(crashed=True, crash_error="RuntimeError: boom")
    loaded = RunState.from_dict(state.to_dict())
    assert loaded.crashed is True
    assert loaded.crash_error == "RuntimeError: boom"


def test_crashed_defaults_for_legacy_state():
    doc = _state().to_dict()
    del doc["crashed"]  # a state.json written before the fields existed
    del doc["crash_error"]
    loaded = RunState.from_dict(doc)
    assert loaded.crashed is False
    assert loaded.crash_error is None


def test_clear_pause_also_clears_crashed():
    state = _state(crashed=True, crash_error="RuntimeError: boom", paused_reason="crash")
    state.clear_pause()
    assert state.crashed is False
    assert state.crash_error is None
    assert state.paused is False


def test_cache_read_weight_from_snapshot():
    state = _state(policy_snapshot={"limits": {"cache_read_weight": 0.5}})
    assert state.cache_read_weight() == 0.5


def test_cache_read_weight_defaults_when_snapshot_absent():
    assert _state().cache_read_weight() == 0.1  # empty snapshot


def test_cache_read_weight_defaults_when_limits_missing():
    state = _state(policy_snapshot={"gates": {}})  # no limits section
    assert state.cache_read_weight() == 0.1


def test_cache_read_weight_defaults_when_limits_not_a_dict():
    state = _state(policy_snapshot={"limits": "oops"})
    assert state.cache_read_weight() == 0.1


def test_cache_read_weight_defaults_when_value_not_a_number():
    state = _state(policy_snapshot={"limits": {"cache_read_weight": "high"}})
    assert state.cache_read_weight() == 0.1
