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
from .model import PAUSE_ESCALATION, RunState, StoryTask, VerifyOutcome
from .policy import Policy

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_PREFERENCE = "PREFERENCE"
CRITICAL_DISPLAY_MAX = 2000
CRITICAL_FALLBACK_SOURCE = "journal.jsonl"
CRITICAL_SOURCE_DISPLAY_MAX = 400
_CRITICAL_TRUNCATION_MARKER = f" [… truncated; full detail in {CRITICAL_FALLBACK_SOURCE}]"


class Action(StrEnum):
    PROCEED = "proceed"
    RETRY = "retry"
    DEFER = "defer"
    PAUSE = "pause"
    # review.on_timeout = "salvage-if-done" only (#271): the engine attempts to
    # commit the already-finalized dev product instead of burning another review
    # cycle; when salvage is not applicable it falls back through
    # `review_retry_or_exhaust`. Produced only by `decide_review_session`.
    SALVAGE = "salvage"


# Timeout-like review verdicts review.on_timeout governs (#271): deliberately the
# same set `_post_kill_reconcile` treats as rescue-eligible. `crashed` is excluded
# — a hard window death is cheap to retry and already honors on-disk artifacts via
# the crash-path read-back — and env-fault (#194) short-circuits before this.
REVIEW_TIMEOUT_STATUSES = frozenset({"timeout", "stalled", "over_budget"})


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


def _escalation_list(result_json: dict[str, Any] | None) -> list[Any]:
    """The `escalations` list a result document contributes, or `[]`.

    Total on any input (DW-181): a non-mapping document -- a list, a string, a
    number -- answers `[]` instead of raising `AttributeError` out of `.get`.
    Like the DW-155/DW-170 guards in `sweep.validate_triage` /
    `validate_migration`, what that buys is totality over parseable JSON for
    this predicate's callers. When DW-181 wrote this guard that totality was
    unreachable in production: `Engine._run_session` dereferenced
    `result.result_json.get(...)` behind an `is not None` check alone and raised
    THERE, upstream of every caller here. DW-206 routed that frame through
    `model.result_mapping`, so a non-mapping document now survives to reach the
    callers that still pass one RAW -- the `critical_escalations` sites reading
    `result.result_json` directly (`sweep.py`'s triage and migration lanes,
    `engine.py`'s review leg, and the two in this module) -- and this guard is
    what answers it. Refused through the existing return channel -- no
    escalation contributes, no new raise or escalation path.

    Scope that reachability claim to those callers only. `preference_escalations`
    has a single call site (`engine.py`'s review leg) and it is now handed the
    already-normalized `rj`, so a non-mapping cannot reach this guard along that
    path. `resolve.py` likewise pre-checks: it raises
    `ValueError("artifact is not a JSON object")` on a non-dict artifact before
    it ever calls `critical_escalations`.

    Kept as its own `isinstance` rather than delegated to `result_mapping`, so
    the ablation still proves this predicate total on its own -- routing it
    through the shared helper would make that ablation vacuous.

    Kept as the single shared predicate so `critical_escalations` and
    `preference_escalations` cannot drift on what a non-list `escalations`
    VALUE contributes -- the question `resolve.py:273-284` relies on this
    owning.
    """
    if not isinstance(result_json, dict):
        # Subsumes the old `if not result_json`: `None` refuses here, and `{}`
        # falls through to `.get`, which returns `[]`.
        return []
    escalations = result_json.get("escalations", [])
    return escalations if isinstance(escalations, list) else []


def critical_escalations(result_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        e
        for e in _escalation_list(result_json)
        if isinstance(e, dict) and str(e.get("severity", "")).upper() == SEVERITY_CRITICAL
    ]


def critical_session_reason(role: str, result_json: dict[str, Any] | None) -> str | None:
    """Compose the lossless reason for a session's CRITICAL escalations.

    This is the one wording owner for every session role.  It deliberately does
    no display truncation: callers journal and persist this value before a
    human-facing boundary renders it through :func:`display_critical_reason`.
    """
    crits = critical_escalations(result_json)
    if not crits:
        return None
    details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
    return f"CRITICAL escalation from {role} session: {details}"


def display_critical_reason(reason: str, source: str | None = None) -> str:
    """Bound a CRITICAL reason and optional recovery trail for display.

    The run journal is the durable source of every full reason.  A validated
    story spec is useful for recovery, but is not claimed to contain arbitrary
    verify/plugin/recovery detail.  Short reasons without a spec are returned
    byte-for-byte; otherwise the reason and recovery hint share the existing
    2,000-character display budget.

    ``source`` is presentation metadata, not an authority grant.  Engine callers
    pass only the already-validated ``StoryTask.spec_file``; claimed paths in a
    raw session result therefore continue through the existing validation path.
    """
    recovery_hint = ""
    if isinstance(source, str) and source:
        shown_source = source
        if len(shown_source) > CRITICAL_SOURCE_DISPLAY_MAX:
            head = CRITICAL_SOURCE_DISPLAY_MAX // 3
            tail = CRITICAL_SOURCE_DISPLAY_MAX - head - 1
            shown_source = shown_source[:head] + "…" + shown_source[-tail:]
        recovery_hint = f" [recovery trail: {shown_source}]"

    if len(reason) + len(recovery_hint) <= CRITICAL_DISPLAY_MAX:
        return reason + recovery_hint
    suffix = _CRITICAL_TRUNCATION_MARKER + recovery_hint
    prefix = reason[: CRITICAL_DISPLAY_MAX - len(suffix)].rstrip()
    return prefix + suffix


def display_pause_reason(state: RunState) -> str:
    """Render a state's pause reason without mutating its lossless record.

    Missing task/source metadata is total and falls back to ``journal.jsonl``.
    A persisted worktree-local spec is relative by design, so anchor it through
    ``runs.task_spec_path`` before presenting it to an operator (#734).
    """
    raw_reason = state.paused_reason
    reason = (
        raw_reason if isinstance(raw_reason, str) else "" if raw_reason is None else str(raw_reason)
    )
    if state.paused_stage != PAUSE_ESCALATION:
        return reason
    story_key = state.paused_story_key
    task = state.tasks.get(story_key) if isinstance(story_key, str) else None
    source = None
    if task is not None and task.spec_file:
        # Local import avoids an escalation -> runs -> devcontract -> verify
        # module-initialization cycle. Display calls happen only after startup.
        from .runs import task_spec_path

        source = str(task_spec_path(task, state))
    return display_critical_reason(reason, source)


def preference_escalations(result_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        e
        for e in _escalation_list(result_json)
        if isinstance(e, dict) and str(e.get("severity", "")).upper() != SEVERITY_CRITICAL
    ]


def env_fault_detail(result: SessionResult) -> str:
    """The evidence excerpt for an environment-fault pause reason, or a generic
    fallback when the adapter classified a fault but kept no line (#194). Shared
    by every site that pauses a transport-failed session — dev/review here, plus
    fix/workflow/sweep in the engine — so the reason wording stays uniform:
    ``environment fault: <role> session <status> (<detail>)``."""
    return result.env_fault_evidence or "transport-failure pattern in session log"


def env_fault_pause_reason(role: str, result: SessionResult) -> str:
    """The uniform pause/escalation reason for a transport-failed session (#194).
    `role` is the descriptor placed before "session" — e.g. "dev", "review",
    "fix", "migration", "triage", or a richer "blocking workflow 'x' (y)". Keeps
    the wording identical across escalation/engine/sweep (see env_fault_detail).

    Composed over `session_failure_reason` so the two diagnoses cannot cancel
    each other out (#489): `crashed` is in `ENV_FAULT_STATUSES`, and both deciders
    test `env_fault` FIRST, so a session destroyed under the run whose pane-log
    tail also matches a transport pattern would otherwise pause blaming only the
    provider. Both facts hold and the operator needs both — a lost session is not
    evidence about the API, and a log pattern is not evidence the session
    survived."""
    return f"environment fault: {session_failure_reason(role, result)} ({env_fault_detail(result)})"


def no_work_pause_reason(role: str, result: SessionResult) -> str:
    """The pause reason for a non-completed session that never did anything (#727):
    ``no work produced: <role> session <status> (...)``.

    Composed over `session_failure_reason`, like `env_fault_pause_reason`, so the
    #489 lost-session suffix survives: a session the multiplexer destroyed before
    it painted a second frame carries both facts, and the operator needs both. The
    parenthetical names what the adapter measured — no completed turn or qualifying
    activity — and what that most often means, because the
    verdict alone (`crashed` / `stalled` / `timeout`) reads as an agent that ran and
    failed, when the CLI in fact sat at a prompt only a human can answer."""
    return (
        f"no work produced: {session_failure_reason(role, result)} (no completed turn "
        "or qualifying activity was observed — the CLI may be waiting on a "
        "human: a permission prompt, a login, a confirmation; the attempt is not charged)"
    )


def session_failure_reason(role: str, result: SessionResult) -> str:
    """The reason text for a non-completed session: ``<role> session <status>``,
    plus the lost-session diagnosis (#489).

    Without the suffix a session destroyed under the run reads exactly like a CLI
    that ran and produced nothing, and the operator debugs the agent instead of
    the host. Routing is unchanged either way — the verdict was already correct,
    only its explanation was missing.

    Only a ``crashed`` verdict can ever carry the suffix (``session_vanished`` is
    stamped nowhere else), so on the timeout/stall paths the suffix never appears.

    The wording states what the evidence *withdraws*, not what it proves. All the
    probe establishes is that a session lookup came back negative — see
    ``TerminalMultiplexer.has_session``, whose False is "the backend did not
    confirm it", not "the session provably no longer exists". That is enough to
    stop an operator reading window death as a CLI exit, and not enough to assert
    the session was destroyed."""
    reason = f"{role} session {result.status}"
    if result.session_vanished:
        return (
            f"{reason}: the multiplexer no longer reports the session, so the window's "
            "disappearance is not evidence the CLI exited"
        )
    return reason


def decide_dev(
    task: StoryTask,
    result: SessionResult,
    outcome: VerifyOutcome | None,
    policy: Policy,
) -> Decision:
    """After a dev session (and its verification, when the session completed)."""
    critical_reason = critical_session_reason("dev", result.result_json)
    if critical_reason is not None:
        return Decision(Action.PAUSE, critical_reason)

    budget_left = task.attempt < policy.limits.max_dev_attempts
    exhausted = _exhausted_action(task)

    if result.status != "completed":
        if result.env_fault:
            # A transport/API failure (the CLI never reached the API, #194): the
            # attempt did no real work, so pause for a human instead of charging
            # it — re-arm resets the budget, exactly like a verify env-fault (rc
            # 126/127). The crits check above already ran (env-fault results carry
            # result_json=None, so it found nothing).
            return Decision(
                Action.PAUSE,
                env_fault_pause_reason("dev", result),
            )
        if not result.produced_work:
            # The session never did anything (#727): no turn ended and the pane
            # never changed after its first frame — a CLI parked on a permission
            # dialog, a login, a dead-on-arrival window. A RETRY would launch a
            # fresh session into the identical wall and burn `max_dev_attempts`
            # without a line of work, so pause for a human instead, ahead of the
            # budget like the env-fault arm above: a spent budget must not file
            # it as deferred work. Re-arm resets the attempt. After `env_fault`
            # because a transport failure explains the silence better than the
            # silence explains itself. Default `True` keeps every adapter that
            # cannot measure this (opencode-http, unit fixtures) on today's path.
            return Decision(
                Action.PAUSE,
                no_work_pause_reason("dev", result),
            )
        reason = session_failure_reason("dev", result)
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
    critical_reason = critical_session_reason("review", result.result_json)
    if critical_reason is not None:
        return Decision(Action.PAUSE, critical_reason)

    if result.status != "completed":
        if result.env_fault:
            # transport/API failure (#194): pause rather than charge a review
            # cycle for a session that never reached the API (see decide_dev).
            return Decision(
                Action.PAUSE,
                env_fault_pause_reason("review", result),
            )
        reason = session_failure_reason("review", result)
        if result.status in REVIEW_TIMEOUT_STATUSES:
            mode = policy.review.on_timeout
            if mode == "defer":
                return Decision(
                    _exhausted_action(task),
                    _exhaust_reason(task, f"{reason} (review.on_timeout=defer)"),
                )
            if mode == "salvage-if-done":
                return Decision(Action.SALVAGE, reason)
        return review_retry_or_exhaust(task, policy, reason)
    return Decision(Action.PROCEED)


def review_retry_or_exhaust(task: StoryTask, policy: Policy, reason: str) -> Decision:
    """The default routing for a failed review session: RETRY while the outer
    cycle budget lasts, then plateau-defer (or re-escalate mid re-drive). Module-
    level so the engine can fall back through it when a SALVAGE attempt turns out
    not to be applicable (#271)."""
    if task.review_cycle < policy.limits.max_review_cycles:
        return Decision(Action.RETRY, reason)
    return review_exhausted(task, reason)


def review_exhausted(task: StoryTask, reason: str) -> Decision:
    """Terminal review-side failure routing without spending another session.

    Used when a deterministic post-review repair has exhausted its own local
    retry bound and launching another reviewer would be unsafe. It preserves the
    same resolved-CRITICAL re-drive rule as ordinary review-budget exhaustion.
    """
    return Decision(_exhausted_action(task), _exhaust_reason(task, reason))


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
