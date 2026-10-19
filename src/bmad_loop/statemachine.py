"""Story lifecycle transition table — the single source of truth for legal moves.

Three tables live here. :data:`TRANSITIONS` is the task-phase graph
:func:`advance` enforces. :data:`BOARD_REGRESSIONS` is the sprint-board side:
``sprintstatus.advance`` never moves a row backward through its lifecycle order,
and the pairs listed here are the only exceptions it may perform — each one only
when its caller opts in explicitly (:func:`check_board_regression`).
:data:`PLAN_TRANSITIONS` is the BMAD v7 ticket-plan side: every ``status`` write
``tickettree.TicketTree.mark`` makes names a :data:`PlanWriteReason`, and
:func:`check_plan_transition` decides it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from .model import Phase, StoryTask


class IllegalTransition(Exception):
    pass


TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    # TRIAGE_RUNNING: a sweep run's triage task — also reused by the sweep's
    # legacy-ledger migration task (same lifecycle, its own task key);
    # story tasks go to DEV_RUNNING.
    # DONE: the sweep migrate task's empty manifest (DW-440) — its input holds no
    # legacy entries, so the migration is a completed no-op and dispatches nothing
    Phase.PENDING: frozenset({Phase.DEV_RUNNING, Phase.TRIAGE_RUNNING, Phase.DONE}),
    Phase.DEV_RUNNING: frozenset({Phase.DEV_VERIFY}),
    # COMMITTING: review.enabled = false skips the review loop entirely, so a
    # verified dev pass commits straight from DEV_VERIFY
    Phase.DEV_VERIFY: frozenset(
        {Phase.DEV_RUNNING, Phase.REVIEW_RUNNING, Phase.COMMITTING, Phase.DEFERRED, Phase.ESCALATED}
    ),
    Phase.REVIEW_RUNNING: frozenset({Phase.REVIEW_VERIFY}),
    Phase.REVIEW_VERIFY: frozenset(
        # DEV_RUNNING: fix session after a clean review whose verify commands failed
        {
            Phase.REVIEW_RUNNING,
            Phase.DEV_RUNNING,
            Phase.COMMITTING,
            Phase.DEFERRED,
            Phase.ESCALATED,
        }
    ),
    # AWAITING_OPERATOR: a story owing human-only external actions parks on the
    # NORMAL commit path — the work commits, then the final phase is chosen by
    # whether the task carries operator_actions. Reachable only from COMMITTING
    # precisely so a park can never skip the gates/commit a DONE story clears.
    Phase.COMMITTING: frozenset({Phase.DONE, Phase.ESCALATED, Phase.AWAITING_OPERATOR}),
    Phase.TRIAGE_RUNNING: frozenset({Phase.TRIAGE_VERIFY}),
    # TRIAGE_RUNNING: invalid triage output retries with feedback, like DEV_VERIFY
    Phase.TRIAGE_VERIFY: frozenset(
        {Phase.TRIAGE_RUNNING, Phase.COMMITTING, Phase.DONE, Phase.ESCALATED}
    ),
    Phase.DONE: frozenset(),
    Phase.DEFERRED: frozenset(),
    Phase.ESCALATED: frozenset(),
    # terminal: `bmad-loop confirm` completes a parked story out of band, not by
    # transitioning the (by then finished) run's task.
    Phase.AWAITING_OPERATOR: frozenset(),
}


def advance(task: StoryTask, to: Phase) -> None:
    allowed = TRANSITIONS[task.phase]
    if to not in allowed:
        raise IllegalTransition(
            f"{task.story_key}: {task.phase} -> {to} (allowed: {sorted(allowed)})"
        )
    task.phase = to


# The sprint-board regressions `sprintstatus.advance` may perform on explicit
# opt-in (`allow_regression=True`); every other backward move stays illegal.
# ("done", "awaiting-operator"): a review pass that finds a `done` story owes
# human-only external actions demotes it to a park (`[operator]
# on_review_demotion = "park"`, DW-383). The board sign-off the dev leg wrote is
# `done`, and without this exception never-regress would leave no honest move.
# Kept as (current, target) status-token pairs so this module stays free of any
# sprintstatus import (sprintstatus imports this one).
BOARD_REGRESSIONS: frozenset[tuple[str, str]] = frozenset({("done", "awaiting-operator")})


def check_board_regression(story_key: str, current: str, target: str) -> None:
    """Refuse a board regression ``current -> target`` that is not allowlisted.

    Called by the sole board writer only for a move it has already classified as
    a regression; a no-op for a pair in :data:`BOARD_REGRESSIONS`, and
    :class:`IllegalTransition` for anything else."""
    if (current, target) not in BOARD_REGRESSIONS:
        raise IllegalTransition(
            f"{story_key}: sprint-status {current} -> {target} is a regression "
            f"(allowed: {sorted(BOARD_REGRESSIONS)})"
        )


# ---- BMAD v7 ticket plans ----------------------------------------------------
#
# A v7 ticket's truth is its plan's frontmatter `status`. The builds write
# draft..built themselves; bmad-loop writes only through `TicketTree.mark`, and
# every such write carries a reason this table decides. "Never regress" means the
# accept path is strictly forward (built -> done); every backward move needs an
# explicit reason, and those reasons exist so park/confirm, repair, re-arm,
# rollback and replan keep working in tickets mode. `salvage` is the one other
# forward write: a timed-out review's verified work lands at `built`, never
# `done`. `done` and `dropped` are final, and bmad-loop never writes `dropped`
# (bmad-agent-po owns it).

# The lifecycle order of a plan `status`; "" is a row with no plan. `blocked` and
# `dropped` sit outside it (`ticketmodel.PLAN_STATUSES` adds them).
PLAN_STATUS_ORDER: Final[tuple[str, ...]] = (
    "",
    "draft",
    "ready-for-dev",
    "in-progress",
    "in-review",
    "built",
    "done",
)
PLAN_BLOCKED: Final = "blocked"
PLAN_DROPPED: Final = "dropped"
# The `blocked_reason` prefix that marks a park: a ticket waiting on the operator,
# as opposed to one a build blocked on its own.
OPERATOR_PARK_PREFIX: Final = "awaiting operator: "
# The status a build session itself writes to declare a park. Upstream reads it
# as an unknown status (a `problems` row), so bmad-loop converts it to an
# operator-park `blocked` under the `park` reason.
PLAN_AWAITING_OPERATOR: Final = "awaiting-operator"

PlanWriteReason = Literal[
    "accept", "park", "confirm", "repair", "rearm", "rollback", "replan", "salvage"
]

_BUILD_STATUSES: Final = frozenset(PLAN_STATUS_ORDER[1:6])  # draft..built
_STARTED: Final = frozenset(PLAN_STATUS_ORDER[2:6])  # ready-for-dev..built


@dataclass(frozen=True)
class PlanRule:
    """What one reason may write: from ``sources`` to ``targets``. ``parked`` is
    a further source admitted only when the plan is ``blocked`` with an operator
    park reason (:data:`OPERATOR_PARK_PREFIX`)."""

    sources: frozenset[str]
    targets: frozenset[str]
    parked: bool = False


PLAN_TRANSITIONS: Final[dict[PlanWriteReason, PlanRule]] = {
    # The orchestrator's acceptance after its own verify and review gates.
    "accept": PlanRule(frozenset({"built"}), frozenset({"done"})),
    # A ticket owing the operator something, parked as `blocked` with a reason
    # starting OPERATOR_PARK_PREFIX; re-parking a parked plan updates its reason.
    # PLAN_AWAITING_OPERATOR is the session-declared park this write converts.
    "park": PlanRule(
        _BUILD_STATUSES | {PLAN_AWAITING_OPERATOR}, frozenset({PLAN_BLOCKED}), parked=True
    ),
    # `bmad-loop confirm` on a park: done under `mark_done = "auto"`, else built.
    "confirm": PlanRule(frozenset(), frozenset({"done", "built"}), parked=True),
    "repair": PlanRule(
        frozenset({"in-progress", "in-review", "built"}), frozenset({"in-progress"})
    ),
    "rearm": PlanRule(
        frozenset({PLAN_BLOCKED, "in-progress", "in-review", "built"}),
        frozenset({"ready-for-dev", "in-progress", "in-review"}),
    ),
    # A plan holding only frontmatter is deleted instead
    # (`TicketTree.discard_unbuilt_plan`).
    "rollback": PlanRule(_STARTED, frozenset({"ready-for-dev"})),
    "replan": PlanRule(_STARTED | {PLAN_BLOCKED}, frozenset({"draft"})),
    # A review that timed out after its work verified: the plan lands at `built`,
    # never `done` — acceptance stays with `accept`, after the orchestrator's gates.
    "salvage": PlanRule(frozenset({"in-progress", "in-review"}), frozenset({"built"})),
}


class PlanTransitionRefused(IllegalTransition):
    """A plan write :data:`PLAN_TRANSITIONS` does not allow; nothing was written."""

    def __init__(self, ref: str, current: str, target: str, reason: str, why: str) -> None:
        super().__init__(
            f"{ref}: plan {current or '(no status)'} -> {target} is not a legal {reason!r} "
            f"write: {why}"
        )
        self.ref = ref
        self.current = current
        self.target = target
        self.reason = reason
        self.why = why


def is_operator_park(status: str, blocked_reason: str) -> bool:
    """Whether a plan is parked on the operator (``blocked`` with a park reason)."""
    return status == PLAN_BLOCKED and blocked_reason.startswith(OPERATOR_PARK_PREFIX)


def check_plan_transition(
    ref: str,
    current: str,
    target: str,
    reason: PlanWriteReason,
    *,
    has_plan: bool,
    blocked_reason: str,
    blocked: str | None = None,
    blocked_at: str = "",
) -> bool:
    """Decide one plan write: True to write, False for a no-op, else raise
    :class:`PlanTransitionRefused`.

    ``current``, ``blocked_reason`` and ``blocked_at`` are the plan's own fields
    as read back (normalized ``status``; ``""`` when unset). ``blocked`` is the
    reason the write would set — a park's — and None otherwise. bmad-loop never
    creates a plan, so ``has_plan=False`` refuses every reason.

    A no-op is a write that would change nothing: the plan already has ``target``
    (and, for ``blocked``, the same reason; for anything else, no reason left
    over). It is answered before the sinks are checked, so a replayed accept of a
    ``done`` plan is a no-op rather than a refusal. A same-status write onto a
    stale ``blocked_at`` with no reason (a hand edit: the script's own writes set
    or clear both) is no no-op — the script reads any ``blocked_at`` as blocked,
    so the write is the cleanup that clears it. A ``done`` plan is never picked
    again, so there it stays a no-op."""
    rule = PLAN_TRANSITIONS.get(reason)

    def refuse(why: str) -> PlanTransitionRefused:
        return PlanTransitionRefused(ref, current, target, reason, why)

    if rule is None:
        raise refuse(f"unknown reason (known: {sorted(PLAN_TRANSITIONS)})")
    if not has_plan:
        raise refuse("the ticket has no plan, and bmad-loop never creates one")
    if target == PLAN_DROPPED:
        raise refuse("bmad-loop never writes dropped")
    if target not in rule.targets:
        raise refuse(f"{reason} writes only {sorted(rule.targets)}")
    if (blocked is not None) != (target == PLAN_BLOCKED):
        raise refuse("a blocked reason goes with status blocked, and only with it")
    if blocked is not None and (reason != "park" or not blocked.startswith(OPERATOR_PARK_PREFIX)):
        raise refuse(f"a park reason must start {OPERATOR_PARK_PREFIX!r}")
    if current == target and blocked_reason == (blocked or ""):
        return blocked is None and bool(blocked_at) and current != PLAN_STATUS_ORDER[-1]
    if current in (PLAN_STATUS_ORDER[-1], PLAN_DROPPED):
        raise refuse(f"{current} is final")
    if current in rule.sources:
        return True
    if rule.parked and is_operator_park(current, blocked_reason):
        return True
    allowed = sorted(rule.sources) + (["blocked (operator park)"] if rule.parked else [])
    raise refuse(f"{reason} writes only from {allowed}")
