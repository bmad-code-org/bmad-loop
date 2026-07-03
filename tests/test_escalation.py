"""Unit tests for the dev/review retry-budget decisions — specifically the
resolved-escalation guard that re-escalates instead of silently deferring."""

from bmad_loop.adapters.base import SessionResult
from bmad_loop.escalation import Action, decide_dev, decide_review_session
from bmad_loop.model import StoryTask
from bmad_loop.policy import LimitsPolicy, NotifyPolicy, Policy
from bmad_loop.verify import VerifyOutcome

POLICY = Policy(
    limits=LimitsPolicy(max_dev_attempts=2, max_review_cycles=2),
    notify=NotifyPolicy(desktop=False, file=True),
)
COMPLETED = SessionResult(status="completed", result_json={"escalations": []})
FAILING = VerifyOutcome.retry("spec status is 'in-progress', expected 'done'")


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
