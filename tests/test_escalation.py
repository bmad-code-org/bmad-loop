"""Unit tests for the dev/review retry-budget decisions — specifically the
resolved-escalation guard that re-escalates instead of silently deferring."""

import dataclasses
import os

import pytest

from bmad_loop import escalation
from bmad_loop.adapters.base import SessionResult
from bmad_loop.escalation import (
    CRITICAL_DISPLAY_MAX,
    Action,
    critical_escalations,
    critical_session_reason,
    decide_dev,
    decide_reverify,
    decide_review_session,
    display_critical_reason,
    display_pause_reason,
    pause_title,
    preference_escalations,
    review_retry_or_exhaust,
    session_failure_reason,
    unresolved_escalation_keys,
)
from bmad_loop.model import PAUSE_ESCALATION, PAUSE_STORY_GATE, Phase, RunState, StoryTask
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
    cannot silently flip. Guard pin too: a NON-env-fault timeout with budget left
    still RETRYs — the env-fault branch must not swallow ordinary transient
    failures."""
    plain = SessionResult(status="timeout")  # env_fault defaults False
    assert plain.produced_work is True
    retried = decide_dev(_task(attempt=1), plain, None, POLICY)
    assert retried.action == Action.RETRY
    assert decide_dev(_task(attempt=2), plain, None, POLICY).action == Action.DEFER
    assert "no work produced" not in retried.reason
    assert "environment fault" not in retried.reason


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
    # the review decider carries the same arm, and the same suffix
    review = decide_review_session(task, both, POLICY)
    assert review.action == Action.PAUSE
    assert review.reason.startswith("no work produced: review session crashed:")
    assert "multiplexer no longer reports the session" in review.reason


_PARKED_EVIDENCE = "Notification(permission_prompt) -> PermissionPrompt"


@pytest.mark.parametrize("attempt", [1, 2])  # budget left, then spent
def test_dev_parked_session_pauses_whatever_the_budget(attempt):
    """DW-348/DW-350: a session the adapter ended parked (the CLI was waiting on a
    human, so the stall nudge was withheld) PAUSEs instead of RETRYing into the
    same prompt — with budget left, and ahead of exhaustion.

    ABLATION: delete the `parked` arm in `decide_dev` and the budget-left row
    RETRYs, the spent row DEFERs."""
    parked = SessionResult(status="stalled", parked=True, parked_evidence=_PARKED_EVIDENCE)
    decision = decide_dev(_task(attempt=attempt), parked, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("parked: dev session stalled (")
    assert _PARKED_EVIDENCE in decision.reason
    assert "the stall nudge was withheld" in decision.reason


def test_dev_stop_failure_account_pause_names_the_error():
    """#865: a session the adapter parked on an account `StopFailure` (the
    classifier skipped its log tail) PAUSEs uncharged, and both the reason and
    the notification title name it as a parked stop, carrying the error."""
    evidence = "StopFailure(billing_error): Credit balance is too low"
    parked = SessionResult(
        status="stalled", parked=True, parked_evidence=evidence, stop_failure="billing_error"
    )
    decision = decide_dev(_task(attempt=1), parked, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("parked: dev session stalled (StopFailure(billing_error)")
    assert evidence in decision.reason
    assert pause_title(decision.reason, "1-1") == "Paused (parked prompt): 1-1"


def test_dev_env_fault_outranks_parked():
    both = SessionResult(
        status="stalled",
        env_fault=True,
        env_fault_evidence="API Error: ETIMEDOUT",
        parked=True,
        parked_evidence=_PARKED_EVIDENCE,
    )
    decision = decide_dev(_task(attempt=1), both, None, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("environment fault: dev session stalled")


def test_dev_parked_outranks_no_work():
    """A named prompt explains the silence better than the silence does: the
    parked arm is tested before the #727 `produced_work=False` arm."""
    both = SessionResult(
        status="stalled", parked=True, parked_evidence=_PARKED_EVIDENCE, produced_work=False
    )
    decision = decide_dev(_task(attempt=1), both, None, POLICY)
    assert decision.reason.startswith("parked: dev session stalled")
    assert "no work produced" not in decision.reason


def test_parked_reason_keeps_the_lost_session_suffix_and_has_a_fallback():
    """Composed over `session_failure_reason` (#489), and never an empty
    parenthetical when the adapter kept no evidence string."""
    result = SessionResult(status="stalled", parked=True)
    assert escalation.parked_pause_reason("fix", result) == (
        "parked: fix session stalled (parked-session signal; the CLI was waiting on "
        "a human — the stall nudge was withheld so it could not answer the prompt)"
    )
    vanished = SessionResult(status="crashed", parked=True, session_vanished=True)
    assert "multiplexer no longer reports the session" in escalation.parked_pause_reason(
        "dev", vanished
    )


def test_review_parked_session_pauses_instead_of_charging_a_cycle():
    """ABLATION: delete the `parked` arm in `decide_review_session` and this
    RETRYs a review cycle."""
    parked = SessionResult(status="stalled", parked=True, parked_evidence=_PARKED_EVIDENCE)
    decision = decide_review_session(_task(attempt=1), parked, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("parked: review session stalled")


def test_review_env_fault_outranks_parked():
    both = SessionResult(
        status="stalled", env_fault=True, parked=True, parked_evidence=_PARKED_EVIDENCE
    )
    decision = decide_review_session(_task(attempt=1), both, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("environment fault: review session stalled")


@pytest.mark.parametrize("on_timeout", ["retry", "defer", "salvage-if-done"])
def test_review_no_work_session_pauses_whatever_the_timeout_mode(on_timeout):
    """#727: a review session that never did anything pauses with budget left,
    ahead of the `review.on_timeout` routing — retrying, deferring or salvaging a
    review that sat at a prompt changes nothing.

    ABLATION: delete the `produced_work` arm in `decide_review_session` and this
    RETRYs, DEFERs or SALVAGEs per mode."""
    idle = SessionResult(status="timeout", produced_work=False)
    decision = decide_review_session(_task(review_cycle=1), idle, _policy(on_timeout))
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("no work produced: review session timeout (")
    assert not decision.budget_exhausted


def test_review_parked_outranks_no_work():
    both = SessionResult(
        status="stalled", parked=True, parked_evidence=_PARKED_EVIDENCE, produced_work=False
    )
    decision = decide_review_session(_task(attempt=1), both, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("parked: review session stalled")


def test_review_env_fault_outranks_parked_and_no_work():
    every = SessionResult(
        status="stalled",
        env_fault=True,
        parked=True,
        parked_evidence=_PARKED_EVIDENCE,
        produced_work=False,
    )
    decision = decide_review_session(_task(attempt=1), every, POLICY)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("environment fault: review session stalled")


@pytest.mark.parametrize(
    ("reason", "env_fault_site", "title"),
    [
        ("parked: dev session stalled (x)", None, "Paused (parked prompt): 1-1"),
        ("no work produced: fix session timeout (x)", None, "Paused (no work): 1-1"),
        ("usage limit: dev session stalled (x)", None, "Paused (usage limit): 1-1"),
        ("environment fault: dev session crashed (x)", None, "Paused (environment fault): 1-1"),
        # a verify-detected fault carries no prefix: the recorded site classifies it
        ("verify command exited 127", "verify:dev", "Paused (environment fault): 1-1"),
        ("CRITICAL escalation from dev session: x", None, "CRITICAL escalation: 1-1"),
        ("review session crashed", None, "CRITICAL escalation: 1-1"),
        # a prefix only counts at the start of the reason
        ("dev said: parked: nope", None, "CRITICAL escalation: 1-1"),
        # the prefix outranks the site: a parked pause keeps its own title
        ("parked: dev session stalled (x)", "verify:dev", "Paused (parked prompt): 1-1"),
    ],
)
def test_pause_title_classifies_by_reason_prefix(reason, env_fault_site, title):
    assert pause_title(reason, "1-1", env_fault_site=env_fault_site) == title


# The #610 credit-exhaustion pane line, split so this file carries no line the
# shipped claude usage patterns match (test_pane_capture_patterns_do_not_match_this_repo).
_USAGE_EVIDENCE = (
    "pane matched 'usage-pattern': You're out of usage credit"
    + "s. Run /usage-credits to keep using Fable 5.1 or /model to switch models."
)


def _usage_limited_results(status):
    """The two shapes a live usage-limit sighting reaches a decider in: the
    adapter's stalled + parked exit, and that result after `_classify_env_fault`
    stamped it (env_fault with the pane evidence)."""
    parked = SessionResult(
        status=status, parked=True, parked_evidence=_USAGE_EVIDENCE, usage_limited=_USAGE_EVIDENCE
    )
    return [parked, dataclasses.replace(parked, env_fault=True, env_fault_evidence=_USAGE_EVIDENCE)]


@pytest.mark.parametrize("attempt", [1, 2])  # budget left, then spent
@pytest.mark.parametrize("status", ["stalled", "timeout"])
def test_dev_usage_limited_pauses_with_usage_reason(attempt, status):
    """A dev session seen live behind a usage limit (#610) PAUSEs with the
    usage-limit reason — through either the env-fault or the parked arm, with
    or without budget left — naming the evidence and the operator's ways out.
    ABLATION: drop the `usage_limited` delegation from `env_fault_pause_reason`
    / `parked_pause_reason` and the reason reads `environment fault:` /
    `parked:` instead."""
    for result in _usage_limited_results(status):
        decision = decide_dev(_task(attempt=attempt), result, None, POLICY)
        assert decision.action == Action.PAUSE
        assert decision.reason.startswith(f"usage limit: dev session {status} (")
        assert _USAGE_EVIDENCE in decision.reason
        assert "bmad-loop resolve" in decision.reason


@pytest.mark.parametrize("status", ["stalled", "timeout"])
def test_review_usage_limited_pauses_with_usage_reason(status):
    """The review-side twin: no review cycle is charged for a usage limit."""
    for result in _usage_limited_results(status):
        decision = decide_review_session(_task(attempt=1), result, POLICY)
        assert decision.action == Action.PAUSE
        assert decision.reason.startswith(f"usage limit: review session {status} (")
        assert _USAGE_EVIDENCE in decision.reason


@pytest.mark.parametrize(
    "resets", ["2026-10-25T19:20:00+07:00", "7:20pm (Mars/Olympus_Mons)"], ids=["iso", "raw"]
)
def test_usage_reason_names_the_reset(resets):
    """#610: a parsed reset rides the reason as `; resets <value>` right after
    the evidence — ISO-8601, or the raw clause when it did not resolve, read as
    given (no clock here). Without one the reason is unchanged.
    ABLATION: drop the `resets` suffix from `usage_limit_pause_reason` and the
    first assertion fails."""
    for result in _usage_limited_results("stalled"):
        with_reset = dataclasses.replace(result, usage_limit_resets=resets)
        reason = decide_dev(_task(attempt=1), with_reset, None, POLICY).reason
        assert f"{_USAGE_EVIDENCE}; resets {resets}; the coding CLI" in reason
        plain = decide_dev(_task(attempt=1), result, None, POLICY).reason
        assert "resets" not in plain.replace(_USAGE_EVIDENCE, "")


def test_usage_limited_decisions_carry_the_pause_facts():
    """DW-591: each decider arm that gives the usage-limit reason carries the
    facts the engine records with the pause — the reset only when it resolved,
    the limit class always. ABLATION: drop `usage_limit=` from one arm and its
    assertion fails."""
    for result in _usage_limited_results("stalled"):
        resolved = dataclasses.replace(
            result, usage_limit_resets="2026-10-25T19:20:00+07:00", usage_limit_class="wait"
        )
        for decision in (
            decide_dev(_task(attempt=1), resolved, None, POLICY),
            decide_review_session(_task(attempt=1), resolved, POLICY),
        ):
            assert decision.usage_limit == escalation.UsageLimitPause(
                reset_at="2026-10-25T19:20:00+07:00", limit_class="wait"
            )
        raw = dataclasses.replace(
            result, usage_limit_resets="7:20pm (Mars/Olympus_Mons)", usage_limit_class="blocked"
        )
        decision = decide_dev(_task(attempt=1), raw, None, POLICY)
        assert decision.usage_limit == escalation.UsageLimitPause(None, "blocked")
        assert "resets 7:20pm (Mars/Olympus_Mons)" in decision.reason


def test_non_usage_pauses_carry_no_usage_facts():
    """A plain env-fault or parked pause carries no usage-limit facts."""
    env = SessionResult(status="timeout", env_fault=True, env_fault_evidence="ECONNRESET")
    parked = SessionResult(status="stalled", parked=True, parked_evidence="permission")
    for result in (env, parked):
        dev = decide_dev(_task(attempt=1), result, None, POLICY)
        review = decide_review_session(_task(attempt=1), result, POLICY)
        assert dev.action == review.action == Action.PAUSE
        assert dev.usage_limit is None and review.usage_limit is None


def test_usage_limit_reason_keeps_the_lost_session_suffix():
    """Composed over `session_failure_reason` (#489), like its siblings."""
    vanished = SessionResult(status="stalled", usage_limited="x", session_vanished=True)
    reason = escalation.usage_limit_pause_reason("dev", vanished)
    assert reason.startswith("usage limit: dev session stalled: the multiplexer no longer")


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


_OVERRULED_SUFFIX = "; a provider error in its log tail was overruled: the session consumed tokens"


def test_session_failure_reason_names_an_overruled_env_fault():
    """#764: a log-tail provider error that measured usage overruled is named in
    the reason, so the operator sees why it did not pause. The bare reason stays
    byte-identical when the field is unset."""
    overruled = SessionResult(status="timeout", env_fault_overruled="provider 529 (overloaded)")
    assert session_failure_reason("dev", overruled) == f"dev session timeout{_OVERRULED_SUFFIX}"
    assert session_failure_reason("dev", SessionResult(status="timeout")) == "dev session timeout"


def test_session_failure_reason_overruled_composes_after_lost_session():
    """The #764 note follows the #489 lost-session text rather than replacing it."""
    both = SessionResult(status="crashed", session_vanished=True, env_fault_overruled="x")
    reason = session_failure_reason("dev", both)
    assert reason.startswith("dev session crashed: the multiplexer no longer reports")
    assert reason.endswith(_OVERRULED_SUFFIX)


def test_dev_overruled_timeout_retries_with_budget_left():
    """An overruled session is a plain timeout to routing (#764): it is charged
    and retried while budget lasts, and the reason names the overrule."""
    overruled = SessionResult(status="timeout", env_fault_overruled="provider 529 (overloaded)")
    decision = decide_dev(_task(attempt=1), overruled, None, POLICY)
    assert decision.action == Action.RETRY
    assert decision.reason.endswith(_OVERRULED_SUFFIX)
    assert "environment fault" not in decision.reason


def test_dev_overruled_timeout_takes_the_exhausted_action_when_spent():
    """No pause special case on exhaustion (#764): a spent budget DEFERs, the
    same as a plain timeout."""
    overruled = SessionResult(status="timeout", env_fault_overruled="provider 529 (overloaded)")
    decision = decide_dev(_task(attempt=2), overruled, None, POLICY)
    plain = decide_dev(_task(attempt=2), SessionResult(status="timeout"), None, POLICY)
    assert decision.action == plain.action == Action.DEFER
    assert decision.budget_exhausted is True


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


def test_review_exhausted_reescalates_resolved_redrive():
    task = _task(review_cycle=2, resolved_redrive=True)
    crashed = SessionResult(status="crashed")
    decision = decide_review_session(task, crashed, POLICY)
    assert decision.action == Action.PAUSE
    assert "re-escalating instead of deferring" in decision.reason


def test_exhausted_decisions_carry_budget_exhausted():
    """Every action `_exhausted_action` drives is marked `budget_exhausted`
    (DW-523), so the engine's environment seam re-probes before it lands; a RETRY
    and a non-budget PAUSE stay unmarked. Ablate any one `budget_exhausted=True`
    and its row reddens."""
    crashed = SessionResult(status="crashed")
    exhausted = {
        "dev verify defer": decide_dev(_task(attempt=2), COMPLETED, FAILING, POLICY),
        "dev session defer": decide_dev(_task(attempt=2), crashed, None, POLICY),
        "dev resolved-redrive pause": decide_dev(
            _task(attempt=2, resolved_redrive=True), COMPLETED, FAILING, POLICY
        ),
        "review_exhausted": escalation.review_exhausted(_task(), "harvest unreadable"),
        "review budget spent": decide_review_session(_task(review_cycle=2), crashed, POLICY),
        "on_timeout=defer": decide_review_session(
            _task(review_cycle=1), SessionResult(status="timeout"), _policy("defer")
        ),
    }
    for name, decision in exhausted.items():
        assert decision.action in (Action.DEFER, Action.PAUSE), name
        assert decision.budget_exhausted is True, name
        assert decision.env_site is None, name  # only the engine seam sets it
    assert exhausted["dev resolved-redrive pause"].action == Action.PAUSE

    charged_retries = (
        decide_dev(_task(attempt=1), COMPLETED, FAILING, POLICY),
        decide_dev(_task(attempt=1), crashed, None, POLICY),
        decide_review_session(_task(review_cycle=1), crashed, POLICY),
    )
    for decision in charged_retries:
        assert decision.action == Action.RETRY
        assert decision.budget_exhausted is False
    critical = VerifyOutcome.escalate("rc=127", env_fault=True)
    assert decide_dev(_task(attempt=2), COMPLETED, critical, POLICY).budget_exhausted is False


def test_env_fault_claim_reader_is_total():
    """Only a mapping's non-blank ``env_fault_claim`` string is a claim —
    stripped and bounded; anything else (a non-mapping document, a missing,
    non-string or blank value) reads as no claim and never raises."""
    for document in (
        None,
        {},
        [],
        "Environment fault: db down",
        7,
        {"env_fault_claim": None},
        {"env_fault_claim": 7},
        {"env_fault_claim": ["db down"]},
        {"env_fault_claim": ""},
        {"env_fault_claim": " \n\t "},
    ):
        assert escalation.env_fault_claim(document) is None
    assert escalation.env_fault_claim({"env_fault_claim": "  db down \n"}) == "db down"
    bounded = escalation.env_fault_claim({"env_fault_claim": "x" * 2000})
    assert bounded == "x" * escalation.ENV_FAULT_CLAIM_LIMIT


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


# ------------------------------------------ pause reasons shaped at display (DW-491)

# A multi-line reason carrying an ESC byte, e.g. a `({error})` a RunPaused builder
# interpolated raw: displayed, its second line must not land as a loose line.
_MULTILINE_REASON = "ledger refused (fatal: x\x1b[31m\nhint: y)"
_SHAPED_REASON = "ledger refused (fatal: x\\x1b[31m ⏎ hint: y)"


def _paused_state(reason: str, stage: str, task: StoryTask | None = None) -> RunState:
    state = RunState(run_id="r1", project="/p", started_at="now")
    state.paused_reason = reason
    state.paused_stage = stage
    if task is not None:
        state.tasks[task.story_key] = task
        state.paused_story_key = task.story_key
    return state


@pytest.mark.parametrize("stage", [PAUSE_STORY_GATE, PAUSE_ESCALATION])
def test_display_pause_reason_shapes_an_esc_and_newline_reason(stage):
    """`display_pause_reason` folds a reason's line breaks into `` ⏎ `` segments and
    escapes its control characters, so `status` and the TUI print one line; the
    persisted reason stays raw (DW-491).

    Ablation: drop the `notice_line` around the reason and the raw ESC and line
    break come back."""
    state = _paused_state(_MULTILINE_REASON, stage)

    assert display_pause_reason(state) == _SHAPED_REASON
    assert state.paused_reason == _MULTILINE_REASON


def test_display_pause_reason_shapes_the_recovery_trail():
    """The escalation recovery trail is a path, shaped like the reason (DW-491).

    Ablation: drop the `notice_line` around the source and the raw line break
    lands in the trail."""
    task = StoryTask(story_key="1-1-a", epic=1, spec_file="/p/spec\nx\x1b.md")
    state = _paused_state("CRITICAL escalation from dev session: d", PAUSE_ESCALATION, task)

    # `task_spec_path` renders the trail with the platform separator.
    trail = f"{os.sep}p{os.sep}spec ⏎ x\\x1b.md"
    assert display_pause_reason(state) == (
        f"CRITICAL escalation from dev session: d [recovery trail: {trail}]"
    )


@pytest.mark.parametrize("stage", [PAUSE_STORY_GATE, PAUSE_ESCALATION])
def test_display_pause_reason_keeps_a_plain_reason_unchanged(stage):
    reason = "CRITICAL escalation from dev session: needs a human — resolve it"
    assert display_pause_reason(_paused_state(reason, stage)) == reason


# ---------------------------------------------------------- decide_reverify (DW-522)


def _reverify_task(origin: str = "deferred", *, resolved_redrive: bool = False) -> StoryTask:
    return StoryTask(
        story_key="1-1-a", epic=1, reverify_from=origin, resolved_redrive=resolved_redrive
    )


def test_decide_reverify_proceeds_on_ok():
    for origin in ("deferred", "escalated"):
        decision = decide_reverify(_reverify_task(origin), VerifyOutcome.passed())
        assert decision.action == Action.PROCEED
        assert decision.reason == ""


def test_decide_reverify_pauses_on_env_fault():
    """A still-broken environment re-pauses even a deferred origin — deferring it
    again would charge the story for the environment once more.

    Ablation, performed: drop the `outcome.env_fault` conjunct in `decide_reverify`
    and this reddens (the deferred origin DEFERs)."""
    outcome = VerifyOutcome.escalate("verify environment fault (rc=75): e2e", env_fault=True)
    # `escalate` carries a severity, which would PAUSE on its own; strip it so the
    # env-fault conjunct is the only thing that can pause here
    outcome = dataclasses.replace(outcome, severity="")
    decision = decide_reverify(_reverify_task("deferred"), outcome)
    assert decision.action == Action.PAUSE
    assert decision.reason.startswith("reverify failed: verify environment fault")


def test_decide_reverify_pauses_on_a_non_retryable_failure():
    outcome = VerifyOutcome.escalate("contract violated")
    decision = decide_reverify(_reverify_task("deferred"), outcome)
    assert decision.action == Action.PAUSE


def test_decide_reverify_defers_a_deferred_origin():
    decision = decide_reverify(
        _reverify_task("deferred"), VerifyOutcome.retry("e2e failed", fixable=True)
    )
    assert decision.action == Action.DEFER
    assert decision.reason == "reverify failed: e2e failed"
    assert decision.budget_exhausted is False  # nothing was charged


def test_decide_reverify_reescalates_an_escalated_origin():
    decision = decide_reverify(_reverify_task("escalated"), VerifyOutcome.retry("e2e failed"))
    assert decision.action == Action.PAUSE
    assert decision.reason == "reverify failed: e2e failed"


def test_decide_reverify_reescalates_a_resolved_redrive():
    """A resolved-escalation re-drive must never downgrade to deferred work, the
    `_exhausted_action` rule.

    Ablation, performed: drop the `not task.resolved_redrive` conjunct and this
    reddens."""
    decision = decide_reverify(
        _reverify_task("deferred", resolved_redrive=True), VerifyOutcome.retry("e2e failed")
    )
    assert decision.action == Action.PAUSE


def test_decide_reverify_never_retries():
    for origin in ("deferred", "escalated", "unknown-future-value"):
        for outcome in (
            VerifyOutcome.retry("x"),
            VerifyOutcome.retry("x", fixable=True),
            VerifyOutcome.escalate("x"),
        ):
            assert decide_reverify(_reverify_task(origin), outcome).action != Action.RETRY


# ----------------------------------------------------------- unresolved_escalation_keys


def _escalation_state(source: str, *tasks: tuple[str, Phase, int]) -> RunState:
    state = RunState(run_id="r1", project="/p", started_at="now", source=source)
    for key, phase, attempt in tasks:
        state.tasks[key] = StoryTask(story_key=key, epic=1, phase=phase, attempt=attempt)
    return state


def test_unresolved_escalation_keys_sprint_counts_every_escalated_task_in_order():
    """A sprint run's run-end re-pause (DW-386) counts any ESCALATED task, including
    one at attempt 0, and resolve's admission reads the keys in task order."""
    state = _escalation_state(
        "sprint-status",
        ("b", Phase.ESCALATED, 0),
        ("a", Phase.DONE, 1),
        ("c", Phase.ESCALATED, 2),
        ("d", Phase.DEFERRED, 1),
    )
    assert unresolved_escalation_keys(state) == ["b", "c"]


@pytest.mark.parametrize("source", ["stories", "tickets"])
def test_unresolved_escalation_keys_checkpoint_flow_skips_attempt0_wedges(source):
    """Stories and tickets runs re-classify a pick-time wedge (attempt 0) from disk
    every pick, so only an in-run escalation (attempt > 0) is unresolved.

    Ablation: drop the attempt narrowing and the wedge key comes back."""
    state = _escalation_state(
        source,
        ("w", Phase.ESCALATED, 0),
        ("x", Phase.ESCALATED, 1),
        ("y", Phase.PENDING, 1),
        ("z", Phase.ESCALATED, 3),
    )
    assert unresolved_escalation_keys(state) == ["x", "z"]


def test_unresolved_escalation_keys_empty_without_an_escalated_task():
    state = _escalation_state("sprint-status", ("a", Phase.DONE, 1), ("b", Phase.PENDING, 0))
    assert unresolved_escalation_keys(state) == []


# --- #959: opt-in uncharged usage relaunch -------------------------------------

RELAUNCH = dataclasses.replace(
    POLICY, limits=dataclasses.replace(POLICY.limits, usage_limit_action="relaunch")
)
_ACCOUNT_EVIDENCE = "StopFailure(authentication_failed): account suspended"


def _account_stop_failure() -> SessionResult:
    """A dead window after an account StopFailure, as S1 relabels it."""
    return SessionResult(
        status="crashed",
        parked=True,
        parked_evidence=_ACCOUNT_EVIDENCE,
        stop_failure="authentication_failed",
        stop_failure_action="account",
    )


@pytest.mark.parametrize("attempt", range(1, POLICY.limits.max_dev_attempts + 1))
@pytest.mark.parametrize("status", ["stalled", "timeout", "crashed"])
def test_dev_usage_limited_relaunches_uncharged_in_relaunch_mode(attempt, status):
    """Relaunch mode turns a usage-limited dev session into an uncharged RETRY,
    even on the last attempt, ahead of the env_fault and parked pause arms.
    ABLATION: drop `uncharged=True` from `usage_relaunch`'s Decision and the
    uncharged assertion fails."""
    for result in _usage_limited_results(status):
        decision = decide_dev(_task(attempt=attempt), result, None, RELAUNCH)
        assert decision.action == Action.RETRY
        assert decision.uncharged is True
        assert not decision.budget_exhausted
        assert decision.reason.startswith("usage relaunch (1/3): ")
        assert _USAGE_EVIDENCE in decision.reason


def test_dev_account_stop_failure_relaunches():
    """An account StopFailure qualifies with no usage-limit sighting at all; the
    reason names the StopFailure evidence."""
    result = _account_stop_failure()
    assert result.usage_limited is None
    decision = decide_dev(_task(attempt=2), result, None, RELAUNCH)
    assert decision.action == Action.RETRY
    assert decision.uncharged is True
    assert decision.reason == f"usage relaunch (1/3): {_ACCOUNT_EVIDENCE}"
    # a parked result with no account action and no usage sighting does not qualify
    permission = SessionResult(status="stalled", parked=True, parked_evidence="permission")
    assert escalation.usage_relaunch(_task(attempt=1), permission, RELAUNCH) is None


def test_relaunch_cap_falls_back_to_the_usage_pause():
    """At the per-story cap (dev and review refunds together) the result gets
    the same uncharged usage-limit PAUSE it gets in pause mode. ABLATION: skip
    the cap check in `usage_relaunch` and the PAUSE assertions fail."""
    at_cap = _task(attempt=3, review_cycle=1, dev_usage_relaunches=2, review_usage_relaunches=1)
    assert at_cap.usage_relaunches == RELAUNCH.limits.max_usage_relaunches
    for result in [*_usage_limited_results("stalled"), _account_stop_failure()]:
        expected = decide_dev(at_cap, result, None, POLICY)
        assert expected.action == Action.PAUSE
        decision = decide_dev(at_cap, result, None, RELAUNCH)
        assert decision == expected
        assert decision.uncharged is False
        review = decide_review_session(at_cap, result, RELAUNCH)
        assert review == decide_review_session(at_cap, result, POLICY)
        assert review.action == Action.PAUSE and review.uncharged is False
    # a cap of 0: relaunch mode never relaunches
    zero = dataclasses.replace(
        RELAUNCH, limits=dataclasses.replace(RELAUNCH.limits, max_usage_relaunches=0)
    )
    for result in _usage_limited_results("stalled"):
        decision = decide_dev(_task(attempt=1), result, None, zero)
        assert decision.action == Action.PAUSE
        assert decision.uncharged is False
        assert escalation.usage_relaunch(_task(attempt=1), result, zero) is None


def test_pause_mode_never_relaunches():
    """The default `usage_limit_action = "pause"` keeps today's pause."""
    assert POLICY.limits.usage_limit_action == "pause"
    for result in [*_usage_limited_results("stalled"), _account_stop_failure()]:
        assert escalation.usage_relaunch(_task(attempt=1), result, POLICY) is None
        decision = decide_dev(_task(attempt=1), result, None, POLICY)
        assert decision.action == Action.PAUSE
        assert decision.uncharged is False


def test_completed_session_never_relaunches():
    """A completed session is judged on its work, never relaunched, even when it
    carries a usage-limit sighting."""
    completed = dataclasses.replace(COMPLETED, usage_limited=_USAGE_EVIDENCE)
    assert escalation.usage_relaunch(_task(attempt=1), completed, RELAUNCH) is None
    account = dataclasses.replace(COMPLETED, stop_failure_action="account")
    assert escalation.usage_relaunch(_task(attempt=1), account, RELAUNCH) is None
    decision = decide_dev(_task(attempt=1), completed, FAILING, RELAUNCH)
    assert decision.action == Action.RETRY
    assert decision.uncharged is False


@pytest.mark.parametrize("review_cycle", [1, POLICY.limits.max_review_cycles])
def test_review_usage_limited_relaunches(review_cycle):
    """The review-side twin, ahead of the pause arms and the on_timeout routing,
    and even on the last review cycle."""
    for result in [*_usage_limited_results("timeout"), _account_stop_failure()]:
        decision = decide_review_session(_task(review_cycle=review_cycle), result, RELAUNCH)
        assert decision.action == Action.RETRY
        assert decision.uncharged is True
        assert decision.reason.startswith("usage relaunch (1/3): ")


def test_relaunch_reason_never_reads_as_a_usage_pause():
    """`pause_title` keys on the `usage limit: ` prefix; a relaunch reason must not
    carry it, and its count names the next relaunch out of the cap."""
    for result in [*_usage_limited_results("stalled"), _account_stop_failure()]:
        task = _task(attempt=1, dev_usage_relaunches=1, review_usage_relaunches=1)
        decision = decide_dev(task, result, None, RELAUNCH)
        assert decision.reason.startswith("usage relaunch (3/3): ")
        assert not decision.reason.startswith("usage limit: ")
        assert pause_title(decision.reason, "9-0-x") != "Paused (usage limit): 9-0-x"


def test_charged_attempts_subtract_refunds():
    """The budget checks count charged sessions only. ABLATION: revert
    `decide_dev`'s `budget_left` to `task.attempt` and the dev RETRY becomes a
    DEFER."""
    crashed = SessionResult(status="crashed")
    dev_task = _task(attempt=POLICY.limits.max_dev_attempts, dev_usage_relaunches=1)
    decision = decide_dev(dev_task, crashed, None, POLICY)
    assert decision.action == Action.RETRY
    assert decision.uncharged is False  # a plain crash is charged
    # without the refund the same attempt count exhausts the budget
    spent = decide_dev(_task(attempt=POLICY.limits.max_dev_attempts), crashed, None, POLICY)
    assert spent.action == Action.DEFER
    review_task = _task(review_cycle=POLICY.limits.max_review_cycles, review_usage_relaunches=1)
    assert review_retry_or_exhaust(review_task, POLICY, "r").action == Action.RETRY
    full = _task(review_cycle=POLICY.limits.max_review_cycles)
    assert review_retry_or_exhaust(full, POLICY, "r").action == Action.DEFER
