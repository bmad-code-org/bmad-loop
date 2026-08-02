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


def test_run_state_stories_fields_default_and_round_trip():
    default = _state()
    assert default.source == "sprint-status"
    assert default.spec_folder == ""
    stories = _state(source="stories", spec_folder="_bmad-output/epic-1")
    back = RunState.from_dict(stories.to_dict())
    assert back.source == "stories"
    assert back.spec_folder == "_bmad-output/epic-1"


def test_run_state_stories_fields_default_when_absent_from_dict():
    # a pre-stories state.json (no source/spec_folder keys) reads as sprint mode
    d = _state().to_dict()
    del d["source"]
    del d["spec_folder"]
    back = RunState.from_dict(d)
    assert back.source == "sprint-status" and back.spec_folder == ""


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


def test_session_record_adapter_identity_round_trips():
    record = SessionRecord(
        task_id="1-1-a-dev-1",
        role="dev",
        status="completed",
        adapter="claude",
        model="opus",
    )
    back = SessionRecord.from_dict(record.to_dict())
    assert back.adapter == "claude"
    assert back.model == "opus"


def test_session_record_adapter_identity_defaults_for_legacy_state():
    # a state.json from before #153 has no adapter/model keys — it must load with
    # "" defaults (adapter "" flags a record that predates identity stamping)
    doc = SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed").to_dict()
    del doc["adapter"]
    del doc["model"]
    back = SessionRecord.from_dict(doc)
    assert back.adapter == ""
    assert back.model == ""


def test_followup_review_recommended_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, followup_review_recommended=True)
    assert StoryTask.from_dict(task.to_dict()).followup_review_recommended is True


def test_followup_review_recommended_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["followup_review_recommended"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).followup_review_recommended is False


def test_followup_reviews_spent_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, followup_reviews_spent=2)
    assert StoryTask.from_dict(task.to_dict()).followup_reviews_spent == 2


def test_followup_reviews_spent_defaults_zero_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["followup_reviews_spent"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).followup_reviews_spent == 0


def test_resolved_redrive_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, resolved_redrive=True)
    assert StoryTask.from_dict(task.to_dict()).resolved_redrive is True


def test_resolved_redrive_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["resolved_redrive"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).resolved_redrive is False


def test_plan_checkpoint_pending_round_trips():
    task = StoryTask(story_key="1", epic=0, plan_checkpoint_pending=True)
    assert StoryTask.from_dict(task.to_dict()).plan_checkpoint_pending is True


def test_plan_checkpoint_pending_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1", epic=0).to_dict()
    del doc["plan_checkpoint_pending"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).plan_checkpoint_pending is False


def test_sentinel_kind_round_trips():
    task = StoryTask(story_key="1", epic=0, sentinel_kind="unresolved")
    assert StoryTask.from_dict(task.to_dict()).sentinel_kind == "unresolved"


def test_sentinel_kind_defaults_empty_for_legacy_state():
    doc = StoryTask(story_key="1", epic=0).to_dict()
    del doc["sentinel_kind"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).sentinel_kind == ""


def test_pre_harvest_ledger_round_trips():
    """Three text states, and the `""` one is load-bearing rather than decorative:
    the natural deserializer (`str(d.get(k, "")) or None`) collapses a persisted
    EMPTY snapshot — the ledger existed and was empty — into None, and None is the
    engine's instruction to UNLINK. That spelling would delete a file the snapshot
    says was there."""
    for value in ("# Deferred Work\n\n### DW-1: something\n", "", None):
        task = StoryTask(story_key="1-1-a", epic=1, pre_harvest_ledger=value)
        restored = StoryTask.from_dict(task.to_dict()).pre_harvest_ledger
        assert restored == value
        assert (restored is None) is (value is None)


def test_pre_harvest_ledger_captured_round_trips():
    """The companion flag is what separates "no ledger existed" from "no snapshot was
    taken"; a round-trip that lost the False would leave a disarmed task looking armed
    with a None text, i.e. armed to unlink."""
    for value in (True, False):
        task = StoryTask(story_key="1-1-a", epic=1, pre_harvest_ledger_captured=value)
        assert StoryTask.from_dict(task.to_dict()).pre_harvest_ledger_captured is value


def test_pre_harvest_ledger_defaults_disarmed_for_legacy_state():
    """Both keys absent ⇒ nothing armed, which is the hands-off default: a task
    persisted before the fields existed makes the replay's restore a no-op rather
    than an unlink of a ledger it knows nothing about. This is the plain
    `bool(d.get(k, False))` idiom, and here — unlike the presence bit this pair
    replaces — it is the correct one, because "missing" and "nothing captured" mean
    the same thing."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["pre_harvest_ledger"]  # state.json from before the fields existed
    del doc["pre_harvest_ledger_captured"]
    task = StoryTask.from_dict(doc)
    assert task.pre_harvest_ledger is None
    assert task.pre_harvest_ledger_captured is False


def test_harvest_wrote_ledger_round_trips():
    """Persistence is the whole point of this flag, not an incidental property of
    living on the task: a crash replay re-runs the harvest, which dedupes against the
    dead attempt's entries and reports filing nothing, so the only honest answer to
    "did the orchestrator write this ledger?" is the one that came off disk."""
    for value in (True, False):
        task = StoryTask(story_key="1-1-a", epic=1, harvest_wrote_ledger=value)
        assert StoryTask.from_dict(task.to_dict()).harvest_wrote_ledger is value


def test_harvest_wrote_ledger_defaults_false_for_legacy_state():
    """Absent ⇒ nothing of ours is in the diff, which is the conservative default: the
    gate then counts the ledger as the session's work, the pre-#405 behaviour."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["harvest_wrote_ledger"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).harvest_wrote_ledger is False


def test_ledger_changed_before_harvest_round_trips():
    """Persisted for the same reason its neighbour is, and it has to survive the
    round-trip to be read at all: the attempt that answers the question may be a
    fresh one whose `_dev_phase` call began before a host death."""
    for value in (True, False):
        task = StoryTask(story_key="1-1-a", epic=1, ledger_changed_before_harvest=value)
        assert StoryTask.from_dict(task.to_dict()).ledger_changed_before_harvest is value


def test_ledger_changed_before_harvest_defaults_false_for_legacy_state():
    """Absent ⇒ the exclusion applies whenever the harvest wrote, i.e. exactly the
    behaviour shipped before this field — the conservative direction."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["ledger_changed_before_harvest"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).ledger_changed_before_harvest is False


def test_harvested_deferrals_round_trip_json_native():
    """Persisted for the same reason the flag above is — a replayed harvest dedups
    against the dead attempt's entries and would re-assign an empty list — and it has
    to come back through JSON unchanged, because that is the form `_defer` reads to
    re-file into the main ledger.

    The `json.loads(json.dumps(...))` leg is the #189 guard: a tuple anywhere in
    these dicts survives `from_dict(to_dict(...))` intact but returns from *disk* as a
    list, so an in-memory comparison of the two reads as a spurious "changed"."""
    import json

    items = [
        {
            "origin": "spec-deferred abc123def456",
            "title": "Retry loop has no ceiling",
            "reason": "the backoff doubles forever: no cap",
            "location": "src/retry.py:88",
            "severity": "medium",
            "source_spec": "spec-1-1-a.md",
        },
        # the shape the harvest emits for a finding with neither field
        {
            "origin": "spec-deferred 0123456789ab",
            "title": "No location",
            "reason": "why",
            "location": None,
            "severity": None,
            "source_spec": "spec-1-1-a.md",
        },
    ]
    task = StoryTask(story_key="1-1-a", epic=1, harvested_deferrals=items)
    assert StoryTask.from_dict(task.to_dict()).harvested_deferrals == items
    assert StoryTask.from_dict(json.loads(json.dumps(task.to_dict()))).harvested_deferrals == items


def test_harvested_deferrals_defaults_empty_for_legacy_state():
    """Absent ⇒ nothing was recorded, so nothing carries — the pre-#405 behaviour
    exactly, and the conservative direction (a carry files entries into a ledger)."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["harvested_deferrals"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).harvested_deferrals == []


def test_harvested_deferrals_do_not_alias_the_persisted_doc():
    """`from_dict` copies each item rather than adopting the parsed doc's dicts: the
    engine mutates this list per attempt, and a task rehydrated from a shared
    `state.json` doc must not write back through it."""
    doc = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[{"origin": "spec-deferred x", "title": "t"}],
    ).to_dict()
    task = StoryTask.from_dict(doc)
    task.harvested_deferrals[0]["title"] = "mutated"
    assert doc["harvested_deferrals"][0]["title"] == "t"


def test_bundle_closes_intended_round_trips_json_native():
    """Persisted for the sharper version of its neighbour's reason: a replay re-runs
    the close, `mark_done` finds the entries already `done` and returns False, and a
    record derived fresh from that comes back empty while the flip is still confined
    to the unmerged worktree.

    The `json.loads(json.dumps(...))` leg is the #189 guard — a tuple survives
    `from_dict(to_dict(...))` intact but returns from *disk* as a list, so an
    in-memory comparison of the two reads as a spurious "changed"."""
    import json

    ids = ["DW-3", "DW-7"]
    task = StoryTask(story_key="sweep-1", epic=0, bundle_closes_intended=ids)
    assert StoryTask.from_dict(task.to_dict()).bundle_closes_intended == ids
    assert StoryTask.from_dict(json.loads(json.dumps(task.to_dict()))).bundle_closes_intended == ids


def test_bundle_closes_intended_defaults_empty_for_legacy_state():
    """Absent ⇒ no close was recorded, so none is re-applied — the pre-#405 shape,
    and the conservative direction (the carry WRITES `status: done` into a ledger)."""
    doc = StoryTask(story_key="sweep-1", epic=0).to_dict()
    del doc["bundle_closes_intended"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).bundle_closes_intended == []


def test_bundle_closes_intended_does_not_alias_the_persisted_doc():
    """A container field, so it gets the same aliasing guard `harvested_deferrals`
    has: the sweep re-assigns this list per bundle, and a task rehydrated from a
    shared `state.json` doc must not write back through it."""
    doc = StoryTask(story_key="sweep-1", epic=0, bundle_closes_intended=["DW-3"]).to_dict()
    task = StoryTask.from_dict(doc)
    task.bundle_closes_intended.append("DW-9")
    assert doc["bundle_closes_intended"] == ["DW-3"]


def test_isolated_ledger_carried_round_trips():
    """The latch governing `Engine._replay_unlatched_ledger_carries`. It has to come
    off DISK, because the crash it guards against is precisely the one that loses the
    in-memory copy: a run that carried and then died would replay its own carry on
    resume and — once anything closed the entry — file a duplicate under a fresh id."""
    task = StoryTask(story_key="1-1-a", epic=1, isolated_ledger_carried=True)
    assert StoryTask.from_dict(task.to_dict()).isolated_ledger_carried is True


def test_isolated_ledger_carried_defaults_false_for_legacy_state():
    """`False` here is provably safe rather than merely conservative: the two payloads
    the replay applies (`harvested_deferrals`, `bundle_closes_intended`) landed on the
    same unreleased branch, so a state.json old enough to lack this key has nothing
    recorded to replay either way."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["isolated_ledger_carried"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).isolated_ledger_carried is False


def test_baseline_ledger_digest_round_trips():
    """The digest is the reference `_harvest_gate_exclude` measures each attempt
    against, and a resumed `_dev_phase` call cannot re-capture it (that would move the
    reference onto the completed session's own tree). So it comes off disk or not at
    all."""
    task = StoryTask(story_key="1-1-a", epic=1, baseline_ledger_digest="a" * 64)
    assert StoryTask.from_dict(task.to_dict()).baseline_ledger_digest == "a" * 64


def test_baseline_ledger_digest_defaults_none_for_legacy_state():
    """`None` is a real state — "no reference was captured" — and it skips the
    per-attempt compute rather than guessing a comparison against nothing."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["baseline_ledger_digest"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).baseline_ledger_digest is None


def test_restore_patch_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, restore_patch="artifacts/attempt.patch")
    assert StoryTask.from_dict(task.to_dict()).restore_patch == "artifacts/attempt.patch"


def test_restore_patch_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["restore_patch"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).restore_patch is None


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
