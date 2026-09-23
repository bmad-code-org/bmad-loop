"""Unit tests for the dev/review retry-budget decisions — specifically the
resolved-escalation guard that re-escalates instead of silently deferring."""

import pytest

from bmad_loop import escalation
from bmad_loop.adapters.base import SessionResult
from bmad_loop.escalation import (
    CRITICAL_DISPLAY_MAX,
    Action,
    critical_escalations,
    critical_session_reason,
    decide_dev,
    decide_review_session,
    display_critical_reason,
    preference_escalations,
    review_retry_or_exhaust,
)
from bmad_loop.model import StoryTask
from bmad_loop.policy import LimitsPolicy, NotifyPolicy, Policy, ReviewPolicy
from bmad_loop.verify import VerifyOutcome

POLICY = Policy(
    limits=LimitsPolicy(max_dev_attempts=2, max_review_cycles=2),
    notify=NotifyPolicy(desktop=False, file=True),
)
COMPLETED = SessionResult(status="completed", result_json={"escalations": []})
FAILING = VerifyOutcome.retry("spec status is 'in-progress', expected 'done'")


def test_escalation_selectors_preserve_valid_list_semantics():
    critical = {"severity": "critical", "detail": "stop"}
    preferences = [
        {},
        {"severity": "PREFERENCE", "detail": "explicit"},
        {"detail": "implicit"},
        {"severity": 1, "detail": "non-critical"},
    ]
    result = {"escalations": [None, "junk", critical, *preferences]}

    assert critical_escalations(result) == [critical]
    assert preference_escalations(result) == preferences


def test_escalation_selectors_reject_every_non_list_shape():
    for value in (None, 1, "escalation", {"severity": "CRITICAL"}, ("tuple",)):
        result = {"escalations": value}
        assert critical_escalations(result) == []
        assert preference_escalations(result) == []


def test_escalation_selectors_absorb_a_non_mapping_document():
    """The row above varies the `escalations` VALUE; this one varies the whole
    document through direct helper calls (DW-181). The sweep lanes call the
    critical selector before their validators, but the earlier engine
    dereference prevents malformed session documents from reaching them.

    ABLATION: restore `if not result_json` in `_escalation_list` and every
    truthy row here raises `AttributeError` instead of returning `[]` -- the
    empty list alone would pass for any reason a value could be absent, so the
    mapping control below pins that the widened guard still lets a real
    document through to its partition."""
    for document in (None, {}, [], "", 0, False, ["nope"], "escalations", 7):
        assert critical_escalations(document) == []
        assert preference_escalations(document) == []

    critical = {"severity": "CRITICAL", "detail": "stop"}
    preference = {"severity": "PREFERENCE", "detail": "note"}
    control = {"escalations": [critical, preference]}

    assert critical_escalations(control) == [critical]
    assert preference_escalations(control) == [preference]


@pytest.mark.parametrize("role", ["dev", "review", "fix", "migration", "triage"])
def test_every_critical_session_role_uses_the_shared_lossless_formatter(role):
    detail = "begin\n" + "x" * 2500 + "TAIL"
    reason = critical_session_reason(
        role,
        {"escalations": [{"severity": "CRITICAL", "detail": detail}]},
    )
    assert reason == f"CRITICAL escalation from {role} session: {detail}"
    assert reason.endswith("TAIL")


def test_critical_display_bound_marks_spec_or_journal_without_touching_short_text():
    exact = "x" * CRITICAL_DISPLAY_MAX
    assert display_critical_reason(exact) == exact
    assert "truncated" not in display_critical_reason(exact)

    with_spec = display_critical_reason(exact + "TAIL", "/tmp/spec.md")
    without_spec = display_critical_reason(exact + "TAIL")
    assert len(with_spec) <= CRITICAL_DISPLAY_MAX
    assert "[… truncated; full detail in journal.jsonl]" in with_spec
    assert "[recovery trail: /tmp/spec.md]" in with_spec
    assert len(without_spec) <= CRITICAL_DISPLAY_MAX
    assert "[… truncated; full detail in journal.jsonl]" in without_spec
    assert "TAIL" not in with_spec


def test_critical_display_preserves_source_spelling_and_compacts_an_overlong_source():
    spaced = "/tmp/spec.md "
    assert display_critical_reason("short", spaced) == f"short [recovery trail: {spaced}]"

    source = "/root/" + "middle/" * 300 + "spec.md"
    displayed = display_critical_reason("R" * 3000, source)
    assert len(displayed) <= CRITICAL_DISPLAY_MAX
    assert "R" * 1000 in displayed
    assert "full detail in journal.jsonl" in displayed
    assert "recovery trail: /root/" in displayed
    assert displayed.endswith("spec.md]")
    assert source not in displayed


def test_decide_review_session_calls_the_shared_critical_formatter(monkeypatch):
    calls = []

    def shared(role, result_json):
        calls.append((role, result_json))
        return "shared review reason"

    monkeypatch.setattr(escalation, "critical_session_reason", shared)
    result = SessionResult(status="completed", result_json={"escalations": []})
    decision = escalation.decide_review_session(_task(), result, POLICY)
    assert calls == [("review", result.result_json)]
    assert decision == escalation.Decision(Action.PAUSE, "shared review reason")


def _task(**kw) -> StoryTask:
    return StoryTask(story_key="9-0-x", epic=9, **kw)


def test_exhausted_budget_defers_normal_story():
    task = _task(attempt=2)  # 2 == max_dev_attempts -> budget spent
    assert decide_dev(task, COMPLETED, FAILING, POLICY).action == Action.DEFER


def test_exhausted_budget_reescalates_resolved_redrive():
    task = _task(attempt=2, resolved_redrive=True)
    decision = decide_dev(task, COMPLETED, FAILING, POLICY)
    assert decision.action == Action.PAUSE
    assert "re-escalating instead of deferring" in decision.reason


def test_budget_left_still_retries_even_when_resolved_redrive():
    task = _task(attempt=1, resolved_redrive=True)  # 1 < 2 -> budget remains
    decision = decide_dev(task, COMPLETED, FAILING, POLICY)
    assert decision.action == Action.RETRY
    # a plain retry must NOT carry the exhausted re-escalation wording — that reason
    # is journaled/fed back, and "re-escalating instead of deferring" is a lie here.
    assert "re-escalating" not in decision.reason


def test_noncompleted_session_reescalates_resolved_redrive():
    task = _task(attempt=2, resolved_redrive=True)
    crashed = SessionResult(status="crashed")
    assert decide_dev(task, crashed, None, POLICY).action == Action.PAUSE


def test_env_fault_outcome_pauses_even_with_budget_left():
    """An environment-fault verify outcome (rc 126/127 → CRITICAL escalate)
    pauses immediately — the attempt budget is never consulted for it."""
    task = _task(attempt=1)  # 1 < 2 -> budget remains
    env_fault = VerifyOutcome.escalate("verify environment fault (rc=127): pint", env_fault=True)
    decision = decide_dev(task, COMPLETED, env_fault, POLICY)
    assert decision.action == Action.PAUSE
    assert "rc=127" in decision.reason


def test_dev_env_fault_session_pauses_even_with_budget_left():
    """A dev session classified as a transport/API environment fault (#194) PAUSEs
    immediately — like the verify env-fault above, the attempt budget is never
    consulted, and the reason carries the evidence line."""
    task = _task(attempt=1)  # 1 < 2 -> budget remains
    env_fault = SessionResult(
        status="timeout", env_fault=True, env_fault_evidence="API Error: Connection refused"
    )
    decision = decide_dev(task, env_fault, None, POLICY)
    assert decision.action == Action.PAUSE
    assert "environment fault: dev session timeout" in decision.reason
    assert "API Error: Connection refused" in decision.reason


def test_dev_env_fault_session_pauses_even_when_budget_exhausted():
    """The env-fault pause outranks budget exhaustion too — a spent budget must not
    downgrade it to a defer (that would file a transport failure as deferred work)."""
    task = _task(attempt=2)  # 2 == max_dev_attempts -> budget spent
    env_fault = SessionResult(status="crashed", env_fault=True)
    decision = decide_dev(task, env_fault, None, POLICY)
    assert decision.action == Action.PAUSE
    assert "environment fault" in decision.reason


def test_dev_no_work_session_pauses_even_with_budget_left():
    """A dev session with no qualifying work evidence (#727), such as a CLI
    parked on a permission dialog,
    PAUSEs for a human instead of RETRYing into the identical wall. The reason
    names the measurement, not the verdict alone.

    ABLATION: delete the `produced_work` arm in `decide_dev` and this RETRYs."""
    task = _task(attempt=1)  # 1 < 2 -> budget remains
    parked = SessionResult(status="stalled", produced_work=False)
    decision = decide_dev(task, parked, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("no work produced: dev session stalled")
    assert "no completed turn or qualifying activity was observed" in decision.reason
    assert "permission prompt" in decision.reason
    assert "the attempt is not charged" in decision.reason


def test_dev_no_work_session_pauses_even_when_budget_exhausted():
    """The no-work pause outranks budget exhaustion like the env-fault pause does:
    a spent budget must not file a CLI waiting on a human as deferred work."""
    task = _task(attempt=2)  # 2 == max_dev_attempts -> budget spent
    parked = SessionResult(status="crashed", produced_work=False)
    decision = decide_dev(task, parked, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("no work produced: dev session crashed")


def test_dev_produced_work_default_keeps_todays_routing():
    """`produced_work` defaults True — "unknown never blocks" — so every positional
    construction (the engine's hand-built results, opencode-http, every fixture)
    keeps RETRY-with-budget / DEFER-without. Pinned on both arms so the default
    cannot silently flip."""
    plain = SessionResult(status="timeout")
    assert plain.produced_work is True
    assert decide_dev(_task(attempt=1), plain, None, POLICY).action == Action.RETRY
    assert decide_dev(_task(attempt=2), plain, None, POLICY).action == Action.DEFER
    assert "no work produced" not in decide_dev(_task(attempt=1), plain, None, POLICY).reason


def test_dev_env_fault_outranks_no_work():
    """Both flags set: the env-fault arm is checked first, so the reason blames
    the transport, not the silence — a lost API connection explains a still
    pane better than the still pane explains itself."""
    task = _task(attempt=1)
    both = SessionResult(
        status="timeout",
        env_fault=True,
        env_fault_evidence="API Error: ETIMEDOUT",
        produced_work=False,
    )
    decision = decide_dev(task, both, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("environment fault: dev session timeout")
    assert "no work produced" not in decision.reason


def test_no_work_reason_keeps_the_lost_session_suffix():
    """#727 x #489: the no-work reason is composed over `session_failure_reason`,
    so a session the multiplexer destroyed before it painted a second frame
    carries both facts instead of one cancelling the other."""
    task = _task(attempt=1)
    both = SessionResult(status="crashed", session_vanished=True, produced_work=False)
    decision = decide_dev(task, both, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("no work produced: dev session crashed:")
    assert "multiplexer no longer reports the session" in decision.reason
    # the review decider does not carry the arm (a named follow-up, not this wave)
    review = decide_review_session(task, both, POLICY)
    assert "no work produced" not in review.reason


def test_dev_plain_noncompleted_still_retries_with_budget():
    """Guard pin: a NON-env-fault timeout with budget left still RETRYs — the
    env-fault branch must not swallow ordinary transient failures."""
    task = _task(attempt=1)
    plain = SessionResult(status="timeout")  # env_fault defaults False
    decision = decide_dev(task, plain, None, POLICY)
    assert decision.action == Action.RETRY
    assert "environment fault" not in decision.reason


def test_vanished_session_says_so_without_changing_the_routing():
    """#489: a session the multiplexer destroyed and a CLI that exited both land
    `crashed`. The routing is the same (a retry re-creates the session), but the
    reason must not read as "the agent ran and produced nothing"."""
    task = _task(attempt=1)
    vanished = SessionResult(status="crashed", session_vanished=True)
    decision = decide_dev(task, vanished, None, POLICY)
    assert decision.action == Action.RETRY  # unchanged: diagnosis, not routing
    assert "multiplexer no longer reports the session" in decision.reason
    # ablation pin: the same verdict WITHOUT the flag stays bare, or the suffix
    # would be decoration rather than a discriminator. Pinned on a word the suffix
    # actually contains — an assertion keyed to wording the text no longer uses
    # passes for the wrong reason and stops guarding anything.
    plain = decide_dev(task, SessionResult(status="crashed"), None, POLICY)
    assert plain.reason == "dev session crashed"
    # and the review side reads the same signal
    review = decide_review_session(task, vanished, POLICY)
    assert "multiplexer no longer reports the session" in review.reason


def test_env_fault_and_lost_session_compose_instead_of_cancelling():
    """#489 x #194: `crashed` is in ENV_FAULT_STATUSES and both deciders test
    env_fault FIRST, so a session destroyed under the run whose log tail also
    matches a transport pattern would otherwise pause blaming only the provider.
    Both facts hold; the operator needs both."""
    task = _task(attempt=1)
    both = SessionResult(
        status="crashed",
        env_fault=True,
        env_fault_evidence="API Error: Connection refused",
        session_vanished=True,
    )
    decision = decide_dev(task, both, None, POLICY)
    assert decision.action == Action.PAUSE  # env-fault routing is untouched
    assert "environment fault: dev session crashed" in decision.reason
    assert "multiplexer no longer reports the session" in decision.reason
    assert "API Error: Connection refused" in decision.reason
    # ablation pin: an env fault WITHOUT a lost session stays exactly as before
    plain = SessionResult(status="crashed", env_fault=True)
    assert "multiplexer" not in decide_dev(task, plain, None, POLICY).reason


def test_review_env_fault_session_pauses():
    """The same classification pauses a review session (evidence in the reason),
    where a plain non-completed review would RETRY/DEFER."""
    task = _task(review_cycle=1)  # budget remains
    env_fault = SessionResult(
        status="stalled", env_fault=True, env_fault_evidence="API Error: ETIMEDOUT"
    )
    decision = decide_review_session(task, env_fault, POLICY)
    assert decision.action == Action.PAUSE
    assert "environment fault: review session stalled" in decision.reason
    assert "ETIMEDOUT" in decision.reason


def test_review_plain_noncompleted_still_retries_with_budget():
    task = _task(review_cycle=1)
    plain = SessionResult(status="crashed")
    assert decide_review_session(task, plain, POLICY).action == Action.RETRY


def test_review_exhausted_defers_normal_story():
    task = _task(review_cycle=2)  # 2 == max_review_cycles
    crashed = SessionResult(status="crashed")
    assert decide_review_session(task, crashed, POLICY).action == Action.DEFER


def test_review_exhausted_reescalates_resolved_redrive():
    task = _task(review_cycle=2, resolved_redrive=True)
    crashed = SessionResult(status="crashed")
    decision = decide_review_session(task, crashed, POLICY)
    assert decision.action == Action.PAUSE
    assert "re-escalating instead of deferring" in decision.reason


# ------------------------------- review.on_timeout routing (#271)


def _policy(on_timeout: str) -> Policy:
    return Policy(
        limits=LimitsPolicy(max_dev_attempts=2, max_review_cycles=2),
        notify=NotifyPolicy(desktop=False, file=True),
        review=ReviewPolicy(on_timeout=on_timeout),
    )


def test_review_timeout_default_retry_matches_legacy_decisions():
    """on_timeout="retry" (the default) is byte-compatible with the pre-knob
    routing for every timeout-like status."""
    for status in ("timeout", "stalled", "over_budget"):
        result = SessionResult(status=status)
        with_budget = decide_review_session(_task(review_cycle=1), result, _policy("retry"))
        assert with_budget.action == Action.RETRY
        assert with_budget.reason == f"review session {status}"
        spent = decide_review_session(_task(review_cycle=2), result, _policy("retry"))
        assert spent.action == Action.DEFER


def test_review_timeout_salvage_mode_routes_to_salvage():
    for status in ("timeout", "stalled", "over_budget"):
        result = SessionResult(status=status)
        # budget state is irrelevant: the engine owns the fallback routing
        for cycle in (1, 2):
            decision = decide_review_session(
                _task(review_cycle=cycle), result, _policy("salvage-if-done")
            )
            assert decision.action == Action.SALVAGE
            assert decision.reason == f"review session {status}"


def test_review_timeout_defer_mode_gives_up_immediately():
    decision = decide_review_session(
        _task(review_cycle=1), SessionResult(status="timeout"), _policy("defer")
    )
    assert decision.action == Action.DEFER
    assert "review.on_timeout=defer" in decision.reason


def test_review_timeout_defer_mode_reescalates_resolved_redrive():
    """The resolved_redrive latch outranks the defer mode — same contract as
    budget exhaustion (never silently downgrade a human's correction)."""
    decision = decide_review_session(
        _task(review_cycle=1, resolved_redrive=True),
        SessionResult(status="timeout"),
        _policy("defer"),
    )
    assert decision.action == Action.PAUSE
    assert "re-escalating instead of deferring" in decision.reason


def test_review_crashed_unaffected_by_on_timeout_modes():
    """crashed is not a timeout-like verdict: every mode keeps the default
    retry/exhaust routing for it."""
    crashed = SessionResult(status="crashed")
    for mode in ("retry", "salvage-if-done", "defer"):
        assert decide_review_session(_task(review_cycle=1), crashed, _policy(mode)).action == (
            Action.RETRY
        )
        assert decide_review_session(_task(review_cycle=2), crashed, _policy(mode)).action == (
            Action.DEFER
        )


def test_review_env_fault_pauses_under_every_on_timeout_mode():
    """env-fault (#194) short-circuits before the on_timeout branch."""
    env_fault = SessionResult(status="timeout", env_fault=True)
    for mode in ("retry", "salvage-if-done", "defer"):
        decision = decide_review_session(_task(review_cycle=1), env_fault, _policy(mode))
        assert decision.action == Action.PAUSE
        assert "environment fault" in decision.reason


def test_review_completed_proceeds_under_every_on_timeout_mode():
    for mode in ("retry", "salvage-if-done", "defer"):
        assert decide_review_session(_task(), COMPLETED, _policy(mode)).action == Action.PROCEED


def test_review_retry_or_exhaust_helper_matches_budget_semantics():
    assert review_retry_or_exhaust(_task(review_cycle=1), POLICY, "r").action == Action.RETRY
    assert review_retry_or_exhaust(_task(review_cycle=2), POLICY, "r").action == Action.DEFER
    latched = review_retry_or_exhaust(_task(review_cycle=2, resolved_redrive=True), POLICY, "r")
    assert latched.action == Action.PAUSE
