"""Unit tests for the dev/review retry-budget decisions — specifically the
resolved-escalation guard that re-escalates instead of silently deferring."""

from bmad_loop.adapters.base import SessionResult
from bmad_loop.escalation import (
    Action,
    critical_escalations,
    decide_dev,
    decide_review_session,
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
