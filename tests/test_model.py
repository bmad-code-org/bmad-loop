"""RunState serialization + lifecycle-flag tests."""

import binascii
import errno
import json
from pathlib import Path

import pytest
from conftest import refuse_to_resolve

from bmad_loop.model import (
    SWEEP_REFUSED_DIRTY,
    SWEEP_REFUSED_NOT_STARTED,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
    TokenUsage,
    VerifyOutcome,
    result_mapping,
)


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


def test_run_state_sweep_options_version_round_trips_and_defaults_legacy():
    state = _state(run_type="sweep", sweep_options_version=2, sweep_options_digest="a" * 64)
    round_tripped = RunState.from_dict(state.to_dict())
    assert round_tripped.sweep_options_version == 2
    assert round_tripped.sweep_options_digest == "a" * 64
    legacy = state.to_dict()
    del legacy["sweep_options_version"]
    del legacy["sweep_options_digest"]
    assert RunState.from_dict(legacy).sweep_options_version == 0
    assert RunState.from_dict(legacy).sweep_options_digest == ""


def test_run_state_code_root_restamp_pending_round_trips_and_defaults_false():
    """The intent marker `runs.restamp_code_root` sets between the moved root and its
    journal record survives the state round trip, and a state.json from before the
    field existed reads back False — a pre-upgrade run owes no record."""
    state = _state(repo_root="/code")
    assert state.code_root_restamp_pending is False
    state.code_root_restamp_pending = True
    back = RunState.from_dict(state.to_dict())
    assert back.code_root_restamp_pending is True
    d = state.to_dict()
    del d["code_root_restamp_pending"]
    assert RunState.from_dict(d).code_root_restamp_pending is False


def test_run_state_repo_root_round_trips_and_backs_code_root():
    """The git root a run's code work happens in, persisted because
    `runs.rearm_escalation` runs OUT OF PROCESS from the engine and had only
    `project` to reach for."""
    state = _state(repo_root="/code")
    back = RunState.from_dict(state.to_dict())
    assert back.repo_root == "/code"
    assert back.code_root == Path("/code")


def test_run_state_code_root_falls_back_to_project_for_legacy_state():
    """A state.json written before the field existed reads back empty, and
    `code_root` then answers `project` — exactly the pre-upgrade behavior, and the
    correct answer for every run without a `repo_root:` override."""
    d = _state().to_dict()
    del d["repo_root"]  # state.json from before the field existed
    back = RunState.from_dict(d)
    assert back.repo_root == ""
    assert back.code_root == Path("/p")


def test_run_state_stories_fields_default_when_absent_from_dict():
    # a pre-stories state.json (no source/spec_folder keys) reads as sprint mode
    d = _state().to_dict()
    del d["source"]
    del d["spec_folder"]
    back = RunState.from_dict(d)
    assert back.source == "sprint-status" and back.spec_folder == ""


def test_sweeps_refused_round_trips():
    """#501's visibility record: trigger -> a closed SWEEP_REFUSED_* slug.

    Kept apart from `sweeps_triggered` deliberately — that list is the re-fire
    latch, and a refusal must not spend it. The two are independent here."""
    state = _state()
    state.sweeps_refused["run-end"] = SWEEP_REFUSED_DIRTY
    state.sweeps_refused["epic-1"] = SWEEP_REFUSED_NOT_STARTED
    back = RunState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back.sweeps_refused == {"run-end": "dirty", "epic-1": "not-started"}
    assert back.sweeps_triggered == []


def test_sweeps_refused_defaults_when_absent_from_dict():
    """A state.json written before #501 carries no `sweeps_refused` key at all.

    Ablation: change from_dict's `d.get("sweeps_refused", {})` to
    `d["sweeps_refused"]`. This test fails with KeyError; the round-trip above
    stays green, because to_dict always writes the key. The two tests cover
    disjoint halves — neither substitutes for the other."""
    d = _state().to_dict()
    del d["sweeps_refused"]
    assert RunState.from_dict(d).sweeps_refused == {}


def test_sweep_decision_quarantines_round_trip():
    """DW-124. The two dispositions a sweep run reaches about a decision — skipped
    unattended, and answer dropped — are the run's own, so they ride `state.json`
    and a pause/resume of the SAME run does not re-announce them. `list[str]` and
    not `set[str]` because `save_state` serializes through `json.dumps`, which
    cannot encode a set; `sweeps_triggered` beside them already has that shape.

    `sweep_unlanded_decisions` (DW-200) rides the same way for a different reason,
    and is deliberately not folded into either: it records a VERDICT — this `build`
    answer's `decision:` line never landed — observed in the decision phase and
    consumed in materialization, so an interruption between those two phases must
    not lose it. The drop that announces it clears it, so the two lists never both
    hold an id.

    The dumps/loads here is the point: a set would raise on the way out."""
    state = _state()
    assert state.sweep_skipped_decisions == [] and state.sweep_dropped_decisions == []
    assert state.sweep_unlanded_decisions == []
    state.sweep_skipped_decisions.append("DW-1")
    state.sweep_dropped_decisions.extend(["DW-2", "DW-3"])
    state.sweep_unlanded_decisions.append("DW-4")
    back = RunState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back.sweep_skipped_decisions == ["DW-1"]
    assert back.sweep_dropped_decisions == ["DW-2", "DW-3"]
    assert back.sweep_unlanded_decisions == ["DW-4"]


def test_sweep_decision_quarantines_default_when_absent_from_dict():
    """A `state.json` written before DW-124 carries neither key, and must resume
    exactly as it does today: both quarantines empty, every decision re-evaluated.

    A `state.json` written before DW-200 is the same case for the third list, and
    the default matters more there than for a quarantine: reading it absent as
    empty means an old paused run resumes with no unlanded verdicts, which is
    exactly what it had.

    Ablation: change from_dict's `d.get("sweep_dropped_decisions", [])` to
    `d["sweep_dropped_decisions"]` (or the same for `sweep_unlanded_decisions`)
    and this fails with KeyError while the round-trip above stays green — to_dict
    always writes all three keys, so the two tests cover disjoint halves."""
    d = _state().to_dict()
    del d["sweep_skipped_decisions"]
    del d["sweep_dropped_decisions"]
    del d["sweep_unlanded_decisions"]
    back = RunState.from_dict(d)
    assert back.sweep_skipped_decisions == [] and back.sweep_dropped_decisions == []
    assert back.sweep_unlanded_decisions == []


def test_sweep_decision_quarantines_coerce_their_elements():
    """Coerced with `str()` like `sweeps_triggered`'s elements: a hand-edited or
    foreign state file is reachable, and every consumer membership-tests these
    lists against a DW id string.

    Ablation: drop any `str()` in from_dict and the matching half fails."""
    d = _state().to_dict()
    d["sweep_skipped_decisions"] = [1]
    d["sweep_dropped_decisions"] = [2]
    d["sweep_unlanded_decisions"] = [3]
    back = RunState.from_dict(d)
    assert back.sweep_skipped_decisions == ["1"] and back.sweep_dropped_decisions == ["2"]
    assert back.sweep_unlanded_decisions == ["3"]


def test_sweep_ledger_in_doubt_round_trips():
    """DW-218/219. The sweep's ledger-publication doubt is CYCLE-scoped on the
    engine instance (`_ledger_in_doubt`, `_close_ledger_in_doubt`) and so is lost
    the moment the process ends — which is precisely what a stop request observed
    in the withheld branch, or a crash between the arming site and the dispatch
    gate's report, does. The RUN's copy of the verdict rides `state.json` so the
    resume withholds instead of dispatching.

    A plain bool, so `json.dumps` encodes it directly; the dumps/loads here is the
    same round-trip discipline the three lists above carry.

    Ablation: delete `"sweep_ledger_in_doubt"` from `to_dict` and this fails while
    the absent-key row below stays green — to_dict always writes the key, so the
    two rows cover disjoint halves."""
    state = _state()
    assert state.sweep_ledger_in_doubt is False
    state.sweep_ledger_in_doubt = True
    back = RunState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back.sweep_ledger_in_doubt is True


def test_sweep_ledger_in_doubt_defaults_when_absent_from_dict():
    """A `state.json` written before DW-218/219 carries no such key, and reading it
    absent as False is what makes an old paused run resume with no doubt — which is
    exactly what it had. The default is the SAFE direction only because no such run
    ever armed the latch; a True default would withhold every bundle of every
    resumed sweep in the archive.

    Ablation: change from_dict's `d.get("sweep_ledger_in_doubt", False)` to
    `d["sweep_ledger_in_doubt"]` and this fails with KeyError."""
    d = _state().to_dict()
    del d["sweep_ledger_in_doubt"]
    assert RunState.from_dict(d).sweep_ledger_in_doubt is False


def test_sweep_ledger_in_doubt_coerces_a_hand_edited_value():
    """Coerced with `bool()` the way `finished`/`stopped`/`crashed` are, so every
    reader of `_ledger_unfit_to_publish()` sees a bool rather than whatever a
    hand-edited or foreign state file put there. The gate is an `or` chain, so a
    truthy non-bool would work by accident today and stop working the moment a
    reader asserts identity.

    Ablation: drop the `bool()` in from_dict and the `is True` / `is False` below
    fail on the coerced values."""
    d = _state().to_dict()
    d["sweep_ledger_in_doubt"] = 1
    assert RunState.from_dict(d).sweep_ledger_in_doubt is True
    d["sweep_ledger_in_doubt"] = ""
    assert RunState.from_dict(d).sweep_ledger_in_doubt is False


def test_sweeps_refused_coerces_both_halves():
    """Both halves are coerced with str(). The value is the JSON-reachable one —
    a number survives a dumps/loads round trip as a number — and the key is
    reachable from a hand-edited or foreign state file. Coercion here is the
    precondition for diagnostics' `looks_like_identifier` filter, which is typed
    over strings on both sides.

    Ablation: drop either `str()` in from_dict and the matching half fails."""
    d = _state().to_dict()
    d["sweeps_refused"] = {1: 2}
    assert RunState.from_dict(d).sweeps_refused == {"1": "2"}


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


@pytest.mark.parametrize("pending", [False, True])
def test_salvage_refile_pending_round_trips(pending):
    task = StoryTask(story_key="1-1-a", epic=1, salvage_refile_pending=pending)
    assert StoryTask.from_dict(task.to_dict()).salvage_refile_pending is pending


def test_salvage_refile_pending_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["salvage_refile_pending"]
    assert StoryTask.from_dict(doc).salvage_refile_pending is False


def test_legacy_park_eligible_state_loads_but_is_not_persisted():
    """Retired authorization state is tolerated but cannot influence new runs."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    doc["park_eligible"] = True

    loaded = StoryTask.from_dict(doc)

    assert not hasattr(loaded, "park_eligible")
    assert "park_eligible" not in loaded.to_dict()


def test_verify_outcome_park_fields_are_absent_by_default():
    """Both park fields are opt-in on the one leg that waives proof-of-work, and
    every other outcome must leave them at the inert pair — `park_proof_skipped`
    is what `Engine._verify_dev_artifacts` journals on, so a default of True
    anywhere would file every ordinary story as a waived gate.

    They are asserted TOGETHER because the whole point of splitting them is that
    `park_zero_diff is None` no longer means "no waiver": on a waived leg whose
    probe faulted it means "unknown", and only `park_proof_skipped` separates the
    two."""
    assert VerifyOutcome.passed().park_proof_skipped is False
    assert VerifyOutcome.passed().park_zero_diff is None
    assert VerifyOutcome.retry("nope").park_proof_skipped is False
    assert VerifyOutcome.retry("nope").park_zero_diff is None
    assert VerifyOutcome.escalate("boom").park_proof_skipped is False
    assert VerifyOutcome.escalate("boom").park_zero_diff is None

    # settable, and independently: the waived-but-unanswerable pair is a real
    # state, not an unreachable combination
    waived = VerifyOutcome.passed(park_proof_skipped=True, park_zero_diff=True)
    assert waived.park_proof_skipped is True and waived.park_zero_diff is True
    unknown = VerifyOutcome.passed(park_proof_skipped=True)
    assert unknown.park_proof_skipped is True and unknown.park_zero_diff is None


def test_followup_reviews_spent_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, followup_reviews_spent=2)
    assert StoryTask.from_dict(task.to_dict()).followup_reviews_spent == 2


def test_followup_reviews_spent_defaults_zero_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["followup_reviews_spent"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).followup_reviews_spent == 0


def test_generation_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, generation=2)
    assert StoryTask.from_dict(task.to_dict()).generation == 2


def test_generation_defaults_zero_for_legacy_state():
    """A run in flight across the upgrade must resume at generation 0, which is the
    value `engine._session_task_id` renders as no suffix at all — so every task id
    already on disk still matches and its `tasks/` directory is still found (#705)."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["generation"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).generation == 0


def test_escalations_resolved_upto_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, escalations_resolved_upto=3)
    assert StoryTask.from_dict(task.to_dict()).escalations_resolved_upto == 3


def test_escalations_resolved_upto_defaults_zero_for_legacy_state():
    """A `state.json` written before DW-11 must resume UNFILTERED. 0 is the value
    `resolve._gather_escalations` reads as "nothing answered yet", so every escalation
    the run recorded is still shown and nothing is reported withheld — byte-for-byte
    today's behavior. Any other default would hide entries the human never saw, on a
    run that was mid-escalation across the upgrade."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["escalations_resolved_upto"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).escalations_resolved_upto == 0


def test_resolved_redrive_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, resolved_redrive=True)
    assert StoryTask.from_dict(task.to_dict()).resolved_redrive is True


def test_resolved_redrive_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["resolved_redrive"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).resolved_redrive is False


def test_dispatched_spec_file_round_trips():
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        dispatched_spec_file="_bmad-output/implementation-artifacts/spec-1-1-a.md",
    )
    restored = StoryTask.from_dict(json.loads(json.dumps(task.to_dict())))
    assert restored.dispatched_spec_file == task.dispatched_spec_file


def test_dispatched_spec_file_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["dispatched_spec_file"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).dispatched_spec_file is None


def test_rebase_spec_paths_on_reanchors_both_ownership_fields(tmp_path):
    """The read-side inverse of `_serialized_worktree_path`, on both fields at once.

    `to_dict` relativizes `spec_file` and `dispatched_spec_file` together, so a
    re-anchor that moved only one would leave a task naming two trees. Absolute
    values are already anchored (a spec outside the mount persists verbatim) and
    must pass through, which is also what makes the call idempotent.
    """
    mount = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1-1-a"
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file="_out/accepted.md",
        dispatched_spec_file="_out/dispatched.md",
    )

    task.rebase_spec_paths_on(mount)

    assert task.spec_file == str(mount / "_out/accepted.md")
    assert task.dispatched_spec_file == str(mount / "_out/dispatched.md")

    # idempotent: a second pass finds both absolute and leaves them alone
    task.rebase_spec_paths_on(mount)
    assert task.spec_file == str(mount / "_out/accepted.md")
    assert task.dispatched_spec_file == str(mount / "_out/dispatched.md")


def test_rebase_spec_paths_on_leaves_absolute_and_empty_values_untouched(tmp_path):
    """An out-of-mount spec and an unbound field are both already correct.

    `_serialized_worktree_path` keeps a path verbatim exactly when
    `relative_to(worktree_path)` raises, so an absolute value beside a set
    `worktree_path` is the out-of-mount shape — joining it onto the mount would
    invent a path no tree contains. `None` must survive as `None` rather than
    becoming the mount root: `Path("")` is `.`, so a bare join would answer the
    tree root, which is a write target, not a spec.
    """
    outside = str(tmp_path / "outside" / "spec.md")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=outside)

    task.rebase_spec_paths_on(tmp_path / "alternate-root" / "wt")

    assert task.spec_file == outside
    assert task.dispatched_spec_file is None


def test_project_local_absolute_accepted_spec_becomes_canonical_relative(tmp_path):
    project = tmp_path / "project"
    spec = project / "artifacts" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("spec\n", encoding="utf-8")
    (project / "hop").mkdir()
    raw = str(project / "hop" / ".." / "artifacts" / "spec.md")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(project)

    assert task.spec_file == "artifacts/spec.md"


def test_prior_attempt_binding_survives_accepted_spec_relocation(tmp_path):
    """Only accepted-spec portability changes before fresh attempt binding."""
    project = tmp_path / "project"
    spec = project / "spec.md"
    project.mkdir()
    spec.write_text("spec\n", encoding="utf-8")
    old_dispatch = str(tmp_path / "old-worktree" / "spec.md")
    old_snapshot = b"prior attempt bytes\x00"
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(spec),
        dispatched_spec_file=old_dispatch,
        dispatched_spec_snapshot=old_snapshot,
    )

    task.relativize_project_local_accepted_spec(project)

    assert task.spec_file == "spec.md"
    assert task.dispatched_spec_file == old_dispatch
    assert task.dispatched_spec_snapshot == old_snapshot


def test_external_and_symlink_external_accepted_specs_keep_their_spelling(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    spec = external / "spec.md"
    spec.write_text("spec\n", encoding="utf-8")
    link = project / "linked"
    link.symlink_to(external, target_is_directory=True)

    for raw in (str(spec), str(link / "spec.md")):
        task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)
        task.relativize_project_local_accepted_spec(project)
        assert task.spec_file == raw


def test_relative_accepted_spec_keeps_its_exact_spelling():
    """A relative value already carries the intended dispatch authority.

    Ablation: delete the absolute-path guard and ``./pyproject.toml`` is normalized
    to ``pyproject.toml``.
    """
    raw = "./pyproject.toml"
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(Path.cwd())

    assert task.spec_file == raw


def test_missing_absolute_accepted_spec_is_unchanged(tmp_path):
    """A missing target has no canonical containment fact to transfer.

    INVERSE ablation: resolve the target non-strictly; the missing in-project
    spelling is rewritten despite having no accepted artifact.
    """
    project = tmp_path / "project"
    project.mkdir()
    raw = str(project / "missing.md")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(project)

    assert task.spec_file == raw


def test_non_file_absolute_accepted_spec_is_unchanged(tmp_path):
    """A contained directory cannot gain relative fallback authority.

    Ablation: remove the regular-file guard and the accepted directory is
    rewritten to a relative spelling that can probe an unrelated artifact root.
    """
    project = tmp_path / "project"
    directory = project / "artifacts" / "spec.md"
    directory.mkdir(parents=True)
    raw = str(directory)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(project)

    assert task.spec_file == raw


def test_resolution_fault_accepted_spec_is_unchanged(tmp_path, monkeypatch):
    """Uncertain canonical containment fails safe with the exact original spelling.

    INVERSE ablation: fall back to lexical containment after the resolution error;
    the faulted in-project absolute value is rewritten.
    """
    project = tmp_path / "project"
    project.mkdir()
    faulted = project / "faulted.md"
    refuse_to_resolve(monkeypatch, faulted)
    raw = str(faulted)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(project)

    assert task.spec_file == raw


def test_probe_fault_accepted_spec_is_unchanged_and_does_not_raise(tmp_path, monkeypatch):
    """The regular-file probe may not raise out of the relativizer (DW-116).

    `worktree_flow.run_isolated` calls this method BEFORE its first `try`, so an
    `OSError` here kills the whole run rather than leaving the spelling alone.

    The injected errno is deliberately NOT EACCES. An unsearchable parent cannot
    reach this probe at all: the `strict=True` resolve two lines above raises EACCES
    first, and the `except` already covers that. What CAN reach the probe is a TOCTOU
    between those two syscalls, or a non-EACCES `OSError` from the stat itself — so
    EIO is what this row injects. An EACCES injection would grade a shape that never
    occurs here.

    Ablation: move the probe back BELOW the `except` block and this row reddens with
    the `OSError` escaping the call.
    """
    project = tmp_path / "project"
    spec = project / "artifacts" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("spec\n", encoding="utf-8")
    raw = str(spec)
    resolved = spec.resolve()
    real_is_file = Path.is_file
    faulted: list[Path] = []

    def fake(self, *a, **kw):
        if Path(self) == resolved:
            faulted.append(Path(self))
            raise OSError(errno.EIO, "Input/output error")
        return real_is_file(self, *a, **kw)

    monkeypatch.setattr(Path, "is_file", fake)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=raw)

    task.relativize_project_local_accepted_spec(project)

    # the fault really fired on the arm this row grades — a path-identity predicate
    # would otherwise go green while probing nothing
    assert faulted == [resolved]
    assert task.spec_file == raw


def test_dispatched_spec_snapshot_round_trips_byte_exactly():
    snapshot = b"---\r\nstatus: ready-for-dev\r\n---\r\n\xffoperator intent\r\n"
    task = StoryTask(story_key="1-1-a", epic=1, dispatched_spec_snapshot=snapshot)

    restored = StoryTask.from_dict(json.loads(json.dumps(task.to_dict())))

    assert restored.dispatched_spec_snapshot == snapshot


def test_dispatched_spec_snapshot_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["dispatched_spec_snapshot"]
    assert StoryTask.from_dict(doc).dispatched_spec_snapshot is None


@pytest.mark.parametrize(
    ("encoded", "cause_type"),
    [("%%%", binascii.Error), ("é", UnicodeEncodeError)],
    ids=["malformed-base64", "non-ascii"],
)
def test_dispatched_spec_snapshot_decode_error_names_story_and_field(encoded, cause_type):
    """Corrupt persisted authority fails with stable task and field context.

    Ablation: restore the inline unguarded decode and both rows leak their
    low-level exception type and message instead of this contextual ValueError.
    """
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    doc["dispatched_spec_snapshot"] = encoded

    with pytest.raises(
        ValueError,
        match=r"story '1-1-a': dispatched_spec_snapshot is not valid base64",
    ) as caught:
        StoryTask.from_dict(doc)

    assert type(caught.value) is ValueError
    assert isinstance(caught.value.__cause__, cause_type)


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


def test_board_advance_intended_round_trips_through_json():
    """#350's carry payload. The JSON leg is the point: this crosses state.json to
    reach the post-merge carry, and `_replay_unlatched_ledger_carries` reads it from
    a RELOADED state after a host loss, never from the object that recorded it."""
    task = StoryTask(story_key="1-1-a", epic=1, board_advance_intended="awaiting-operator")
    restored = StoryTask.from_dict(json.loads(json.dumps(task.to_dict())))
    assert restored.board_advance_intended == "awaiting-operator"


def test_board_advance_intended_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["board_advance_intended"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).board_advance_intended is None


def test_board_advance_intended_keeps_none_distinct_from_a_status():
    """None means the sync never ran (a sweep bundle, stories mode, the legacy
    path) and is the whole of the carry's guard — collapsing it to "" would still
    read falsy, but a str() over None would coin the string "None" and hand the
    carry a target `advance` would refuse in silence."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    assert doc["board_advance_intended"] is None
    assert StoryTask.from_dict(doc).board_advance_intended is None


_DEFERRED_STATE_KEYS = (
    "baseline_ledger_digest",
    "pre_harvest_ledger",
    "pre_harvest_ledger_captured",
    "harvest_wrote_ledger",
    "ledger_changed_before_harvest",
    "harvested_deferrals",
    "bundle_closes_intended",
    "story_closes_intended",
    "accepted_dev_session_index",
    "harvest_carry_commit_pending",
    "isolated_ledger_carried",
)


def test_deferred_work_state_fields_round_trip_through_json():
    """All eleven fields are hand-enumerated in both serializers. Non-default
    values make a missing line on either side observable, while the JSON leg pins
    the on-disk container shape rather than only an in-memory dataclass copy."""
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        baseline_ledger_digest="a" * 64,
        pre_harvest_ledger="",
        pre_harvest_ledger_captured=True,
        harvest_wrote_ledger=True,
        ledger_changed_before_harvest=True,
        harvested_deferrals=[{"origin": "spec-deferred abc", "title": "finding"}],
        bundle_closes_intended=["DW-3", "DW-7"],
        story_closes_intended=["DW-4"],
        accepted_dev_session_index=3,
        harvest_carry_commit_pending=True,
        isolated_ledger_carried=True,
    )
    restored = StoryTask.from_dict(json.loads(json.dumps(task.to_dict())))

    assert restored.baseline_ledger_digest == "a" * 64
    assert restored.pre_harvest_ledger == ""
    assert restored.pre_harvest_ledger is not None
    assert restored.pre_harvest_ledger_captured is True
    assert restored.harvest_wrote_ledger is True
    assert restored.ledger_changed_before_harvest is True
    assert restored.harvested_deferrals == [{"origin": "spec-deferred abc", "title": "finding"}]
    assert restored.bundle_closes_intended == ["DW-3", "DW-7"]
    assert restored.story_closes_intended == ["DW-4"]
    assert restored.accepted_dev_session_index == 3
    assert restored.harvest_carry_commit_pending is True
    assert restored.isolated_ledger_carried is True


def test_deferred_work_state_fields_default_for_one_old_state_dict():
    """A state.json written before this package has none of the eleven keys.
    Every load must use ``d.get`` so resume reaches the old behavior instead of
    raising KeyError; one shared old document prevents testing only a subset."""
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    for key in _DEFERRED_STATE_KEYS:
        del doc[key]

    restored = StoryTask.from_dict(doc)
    assert restored.baseline_ledger_digest is None
    assert restored.pre_harvest_ledger is None
    assert restored.pre_harvest_ledger_captured is False
    assert restored.harvest_wrote_ledger is False
    assert restored.ledger_changed_before_harvest is False
    assert restored.harvested_deferrals == []
    assert restored.bundle_closes_intended == []
    assert restored.story_closes_intended == []
    assert restored.accepted_dev_session_index is None
    assert restored.harvest_carry_commit_pending is False
    assert restored.isolated_ledger_carried is False


def test_pre_harvest_ledger_preserves_absent_empty_and_text_states():
    """Empty text means an existing empty ledger; None means no file. Collapsing
    them would turn the common empty-ledger restore into an unlink."""
    for value in (None, "", "# Deferred Work\n"):
        restored = StoryTask.from_dict(
            StoryTask(story_key="1-1-a", epic=1, pre_harvest_ledger=value).to_dict()
        ).pre_harvest_ledger
        assert restored == value
        assert (restored is None) is (value is None)


def test_deferred_work_state_containers_do_not_alias_the_persisted_doc():
    doc = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[
            {"title": "original", "metadata": {"labels": ["review"]}},
        ],
        bundle_closes_intended=["DW-1"],
    ).to_dict()
    restored = StoryTask.from_dict(doc)
    restored.harvested_deferrals[0]["title"] = "mutated"
    restored.harvested_deferrals[0]["metadata"]["labels"].append("follow-up")
    restored.bundle_closes_intended.append("DW-2")
    assert doc["harvested_deferrals"] == [
        {"title": "original", "metadata": {"labels": ["review"]}},
    ]
    assert doc["bundle_closes_intended"] == ["DW-1"]


def test_deferred_work_state_container_defaults_are_not_shared():
    one = StoryTask(story_key="1-1-a", epic=1)
    other = StoryTask(story_key="1-2-b", epic=1)
    one.harvested_deferrals.append({"title": "one"})
    one.bundle_closes_intended.append("DW-1")
    assert other.harvested_deferrals == []
    assert other.bundle_closes_intended == []


def test_restore_patch_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, restore_patch="artifacts/attempt.patch")
    assert StoryTask.from_dict(task.to_dict()).restore_patch == "artifacts/attempt.patch"


def test_restore_patch_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["restore_patch"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).restore_patch is None


def test_preserve_ref_round_trips():
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        preserve_ref="attempt-preserve/run-1-abcd1234",
        preserve_partial=True,
    )
    restored = StoryTask.from_dict(task.to_dict())
    assert restored.preserve_ref == "attempt-preserve/run-1-abcd1234"
    # the partial marker must survive resume too, or a resumed run's defer notice
    # silently re-acquires the whole-attempt promise for a commits-only ref
    assert restored.preserve_partial is True


def test_preserve_ref_defaults_none_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["preserve_ref"]  # state.json from before the field existed
    del doc["preserve_partial"]
    restored = StoryTask.from_dict(doc)
    assert restored.preserve_ref is None and restored.preserve_partial is False


def test_operator_actions_round_trip():
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.AWAITING_OPERATOR,
        operator_actions=["buy the domain", "publish the DKIM TXT record"],
    )
    restored = StoryTask.from_dict(task.to_dict())
    # the phase travels as its plain token, so a parked run resumes parked
    assert restored.phase is Phase.AWAITING_OPERATOR
    assert task.to_dict()["phase"] == "awaiting-operator"
    # order is meaningful — the human works the list top-down
    assert restored.operator_actions == ["buy the domain", "publish the DKIM TXT record"]


def test_operator_actions_defaults_empty_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["operator_actions"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).operator_actions == []


def test_operator_actions_are_not_shared_between_tasks():
    """`field(default_factory=list)` rather than a shared `[]` default: two parked
    stories in one run must not accumulate each other's actions."""
    one = StoryTask(story_key="1-1-a", epic=1)
    other = StoryTask(story_key="1-2-b", epic=1)
    one.operator_actions.append("buy the domain")
    assert other.operator_actions == []


def test_token_budget_warned_round_trips():
    task = StoryTask(story_key="1-1-a", epic=1, token_budget_warned=True)
    assert StoryTask.from_dict(task.to_dict()).token_budget_warned is True


def test_token_budget_warned_defaults_false_for_legacy_state():
    doc = StoryTask(story_key="1-1-a", epic=1).to_dict()
    del doc["token_budget_warned"]  # state.json from before the field existed
    assert StoryTask.from_dict(doc).token_budget_warned is False


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


def test_release_spec_paths_from_mount_relativizes_the_accepted_spec():
    """The accepted spec goes back to the spelling the REPLACEMENT mount re-resolves.

    `_discard_unit_for_restart` deletes the mount and the next attempt mounts a fresh
    one carrying the same story's spec at the same relative place. An absolute path
    into the deleted tree is what `verify.resolve_spec_path` passes through untouched,
    so `_dispatched_spec_for_attempt` resolves it `strict=True` and the fresh attempt
    starts unbound; the relative spelling is re-probed against the live workspace and
    binds. `spec_file` outlives the attempt, so it is relativized rather than cleared.

    Ablation: drop the `_serialized_worktree_path` call from
    `release_spec_paths_from_mount` and this reddens on the absolute spelling.
    """
    task = StoryTask("1-1-a", 1)
    task.worktree_path = "/runs/r1/worktrees/1"
    task.spec_file = "/runs/r1/worktrees/1/_bmad-output/spec.md"

    task.release_spec_paths_from_mount()

    assert task.spec_file == "_bmad-output/spec.md"


def test_release_spec_paths_from_mount_clears_the_attempt_binding():
    """The attempt-owned pair died with its tree, and both halves go together.

    `dispatched_spec_file`/`dispatched_spec_snapshot` are the authority pair
    `recovery_flow` restores bytes through. A path without its snapshot is a shape
    `_bind_dispatched_spec_for_attempt` never persists, so clearing one and not the
    other would invent it.

    Ablation: drop either `= None` and this reddens on that half.
    """
    task = StoryTask("1-1-a", 1)
    task.worktree_path = "/runs/r1/worktrees/1"
    task.dispatched_spec_file = "/runs/r1/worktrees/1/_bmad-output/spec.md"
    task.dispatched_spec_snapshot = b"frozen bytes"

    task.release_spec_paths_from_mount()

    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_release_spec_paths_from_mount_keeps_an_out_of_mount_spec_verbatim():
    """A spec outside the mount was never the mount's to give up.

    `_serialized_worktree_path` keeps such a path verbatim exactly when
    `relative_to` raises — the shared-artifact-dir shape that survives the re-drive.
    Relativizing it would be meaningless, and reusing that one helper is what makes
    the discarded-mount spelling and the persisted one agree by construction.

    Ablation: replace the helper call with an unconditional `relative_to`/join and
    this reddens (or raises) while the in-mount row above stays green.
    """
    task = StoryTask("1-1-a", 1)
    task.worktree_path = "/runs/r1/worktrees/1"
    task.spec_file = "/shared-artifacts/spec.md"

    task.release_spec_paths_from_mount()

    assert task.spec_file == "/shared-artifacts/spec.md"


# ------------------------------------------------------- result_mapping


@pytest.mark.parametrize(
    "document",
    [
        None,
        # truthy non-mappings: the shapes that RAISED out of `.get` before
        # DW-206, because `(doc or {})` substituted only on a falsy value
        ["nope"],
        "escalations",
        7,
        3.5,
        ("nope",),
        {"a", "b"},
        object(),
        # falsy non-mappings: the shapes the old idiom already absorbed
        [],
        "",
        0,
        False,
        (),
    ],
)
def test_result_mapping_answers_empty_for_every_non_mapping(document):
    """The one shape predicate every read of a session result document goes
    through. A result document is parsed JSON, so its top level can be any JSON
    value, while every consumer reads it with `.get` — so each truthy row here
    raised `AttributeError` at whichever frame touched it first (DW-206:
    `Engine._run_session`, one frame upstream of DW-181's own guards). All of
    them now refuse through the empty-document channel the absent-document row
    already takes.

    Ablation: replace the body with `result_json or {}` and every truthy
    non-mapping row reddens (it is returned as itself), while `None` and the
    falsy rows stay green — which is exactly why the falsy rows are here."""
    assert result_mapping(document) == {}


def test_result_mapping_returns_a_mapping_by_identity_never_a_copy():
    """Returning the caller's OWN object is load-bearing, not incidental:
    `Engine._reconcile_generic_terminal_status` mutates the document in place
    under its own `isinstance` guard, and `_dev_phase` / the review loop bind
    the result and pass it on. A copy would silently strand every such write.

    Ablation: return `dict(result_json)` instead and the `is` assertions redden
    while an `==` -only test would not notice."""
    document = {"spec_file": "x.md"}

    assert result_mapping(document) is document

    empty: dict = {}
    assert result_mapping(empty) is empty


def test_result_mapping_falsy_mapping_answers_identically_to_the_old_substitute():
    """`{}` is the one input the replaced `(doc or {})` idiom substituted for
    while the input was already the right shape. `isinstance` lets it fall
    through to `.get`, which answers the same thing the substitute would have —
    the equivalence that preserves field-read behavior at the converted sites."""
    assert result_mapping({}).get("spec_file") is None
    assert result_mapping(None).get("spec_file") is None


def test_result_mapping_absorbs_the_unchecked_session_record_rehydration():
    """`SessionRecord.from_dict` takes `result_json` off state.json with no shape
    check, so a hand-edited or corrupted run state is one of the two named
    producers of a non-mapping document. Reading that record through the
    predicate is total; reading it with `.get` is not."""
    record = SessionRecord.from_dict(
        {
            "task_id": "1-1-a-dev-1",
            "role": "dev",
            "status": "completed",
            "result_json": ["nope"],
        }
    )

    assert record.result_json == ["nope"]  # rehydrated verbatim, unchecked
    assert result_mapping(record.result_json) == {}


def test_artifact_publication_state_round_trips_and_releases_with_mount():
    task = StoryTask(story_key="dw-fix", epic=0)
    old = StoryTask.from_dict({"story_key": "dw-fix", "epic": 0, "phase": "pending"})
    assert old.artifact_baseline is None and old.artifact_payload is None
    assert old.artifact_source_digests is None
    assert old.artifact_tracked_source_oids is None
    assert old.artifact_acceptance_identity is None
    assert old.artifact_publication_complete is False
    assert old.integration_attempt is None
    task.artifact_baseline = {"report.bin": "before"}
    task.artifact_destination = "/project/artifacts"
    task.artifact_source_digests = {"report.bin": "accepted"}
    task.artifact_tracked_source_oids = {"tracked.bin": "blob-id"}
    task.artifact_acceptance_identity = "review:2"
    task.artifact_payload = {"report.bin": "AP8="}
    task.artifact_publication_complete = True
    task.integration_attempt = {
        "target_ref": "refs/heads/main",
        "strategy": "merge",
        "source_revision": "source",
        "operation_identity": "operation",
        "pre_target_revision": "before",
        "old_revision": "actual-before",
        "new_revision": "integrated",
    }
    loaded = StoryTask.from_dict(task.to_dict())
    assert loaded.artifact_baseline == task.artifact_baseline
    assert loaded.artifact_payload == task.artifact_payload
    assert loaded.artifact_destination == task.artifact_destination
    assert loaded.artifact_source_digests == task.artifact_source_digests
    assert loaded.artifact_tracked_source_oids == task.artifact_tracked_source_oids
    assert loaded.artifact_acceptance_identity == "review:2"
    assert loaded.artifact_publication_complete
    assert loaded.integration_attempt == task.integration_attempt
    loaded.release_mount_owned_state()
    assert loaded.artifact_baseline is None and loaded.artifact_payload is None
    assert loaded.artifact_source_digests is None
    assert loaded.artifact_tracked_source_oids is None
    assert loaded.artifact_acceptance_identity is None
    assert loaded.artifact_destination is None and not loaded.artifact_publication_complete
    assert loaded.integration_attempt is None


# ------------------------------------ the artifact-only receipt's snapshot (DW-273)


def test_story_task_baseline_artifacts_round_trips_and_defaults_none():
    """`baseline_artifacts` — path -> `[mtime_ns, size]` or `None` for an entry
    listed but unmeasurable at attempt start — survives `to_dict`/`from_dict`
    byte-for-byte, and a pre-upgrade record with no key reads as `None` (no
    snapshot), on which the receipt refuses rather than guesses."""
    task = StoryTask(story_key="dw-bundle", epic=0, dw_ids=["DW-1"])
    assert task.baseline_artifacts is None
    task.baseline_artifacts = {
        "_bmad-output/impl/spec.md": [1_700_000_000_123_456_789, 42],
        "_bmad-output/impl/gone.md": None,
    }

    rehydrated = StoryTask.from_dict(json.loads(json.dumps(task.to_dict())))

    assert rehydrated.baseline_artifacts == task.baseline_artifacts
    legacy = task.to_dict()
    del legacy["baseline_artifacts"]
    assert StoryTask.from_dict(legacy).baseline_artifacts is None


@pytest.mark.parametrize(
    "mangled",
    [
        "not a mapping",
        ["_bmad-output/impl/spec.md"],
        {"_bmad-output/impl/spec.md": [1]},
        {"_bmad-output/impl/spec.md": "1700000000:42"},
        {"_bmad-output/impl/spec.md": [1, 2, 3]},
        {"_bmad-output/impl/spec.md": ["bad", 42]},
        {"_bmad-output/impl/spec.md": [None, 42]},
        {"_bmad-output/impl/spec.md": [1.5, 42]},
        {"_bmad-output/impl/spec.md": [True, 42]},
    ],
)
def test_story_task_baseline_artifacts_mangled_shape_reads_as_no_snapshot(mangled):
    """A hand-edited state.json whose snapshot is not path -> 2-int list (or None)
    is not partially trusted: the whole field reads as `None`, so the receipt
    refuses for want of a snapshot instead of crediting entries against a
    baseline half of which was dropped — and a non-integer element never RAISES
    out of `from_dict`, which would keep the whole run state (and `bmad-loop
    resume`) from loading over one mangled fingerprint.

    Ablation: convert with `int(value[0])` and the `"bad"`/`None` rows raise."""
    d = StoryTask(story_key="dw-bundle", epic=0).to_dict()
    d["baseline_artifacts"] = mangled

    assert StoryTask.from_dict(d).baseline_artifacts is None
