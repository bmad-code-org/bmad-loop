"""Retry budgets and typed escalation.

CRITICAL escalations pause the run for a human; PREFERENCE escalations are
journaled and the run continues. Exhausted budgets plateau-defer: the story
is skipped and the run stays alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .adapters.base import SessionResult
from .model import StoryTask
from .policy import Policy
from .verify import VerifyOutcome

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_PREFERENCE = "PREFERENCE"


class Action(StrEnum):
    PROCEED = "proceed"
    RETRY = "retry"
    DEFER = "defer"
    PAUSE = "pause"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


def critical_escalations(result_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not result_json:
        return []
    return [
        e
        for e in result_json.get("escalations", [])
        if isinstance(e, dict) and str(e.get("severity", "")).upper() == SEVERITY_CRITICAL
    ]


def preference_escalations(result_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not result_json:
        return []
    return [
        e
        for e in result_json.get("escalations", [])
        if isinstance(e, dict) and str(e.get("severity", "")).upper() != SEVERITY_CRITICAL
    ]


def decide_dev(
    task: StoryTask,
    result: SessionResult,
    outcome: VerifyOutcome | None,
    policy: Policy,
) -> Decision:
    """After a dev session (and its verification, when the session completed)."""
    crits = critical_escalations(result.result_json)
    if crits:
        details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
        return Decision(Action.PAUSE, f"CRITICAL escalation from dev session: {details}")

    budget_left = task.attempt < policy.limits.max_dev_attempts
    exhausted = _exhausted_action(task)

    if result.status != "completed":
        reason = f"dev session {result.status}"
        if budget_left:
            return Decision(Action.RETRY, reason)
        return Decision(exhausted, _exhaust_reason(task, reason))

    assert outcome is not None
    if outcome.ok:
        return Decision(Action.PROCEED)
    if outcome.severity == SEVERITY_CRITICAL:
        return Decision(Action.PAUSE, outcome.reason)
    if budget_left:
        return Decision(Action.RETRY, outcome.reason)
    return Decision(exhausted, _exhaust_reason(task, outcome.reason))


def decide_review_session(task: StoryTask, result: SessionResult, policy: Policy) -> Decision:
    """After a review session returns, before interpreting its done/followup status."""
    crits = critical_escalations(result.result_json)
    if crits:
        details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
        return Decision(Action.PAUSE, f"CRITICAL escalation from review session: {details}")

    budget_left = task.review_cycle < policy.limits.max_review_cycles
    if result.status != "completed":
        reason = f"review session {result.status}"
        if budget_left:
            return Decision(Action.RETRY, reason)
        return Decision(_exhausted_action(task), _exhaust_reason(task, reason))
    return Decision(Action.PROCEED)


def _exhausted_action(task: StoryTask) -> Action:
    """What a budget-exhausted, non-CRITICAL failure resolves to. Normally a
    plateau-defer (skip the story, keep the run alive). But a story mid re-drive
    of a human-resolved CRITICAL escalation (``resolved_redrive`` latched, not
    yet re-committed) must NOT silently downgrade to a defer — that would file an
    unresolved escalation as deferred work and roll back the human's correction.
    Re-escalate so the human sees it again; ``_escalate`` preserves the tree."""
    return Action.PAUSE if task.resolved_redrive else Action.DEFER


def _exhaust_reason(task: StoryTask, reason: str) -> str:
    if task.resolved_redrive:
        return (
            "resolved-escalation re-drive did not converge — re-escalating "
            f"instead of deferring: {reason}"
        )
    return reason
