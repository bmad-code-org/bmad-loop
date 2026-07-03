"""Story lifecycle transition table — the single source of truth for legal moves."""

from __future__ import annotations

from .model import Phase, StoryTask


class IllegalTransition(Exception):
    pass


TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    # TRIAGE_RUNNING: a sweep run's triage task — also reused by the sweep's
    # legacy-ledger migration task (same lifecycle, its own task key);
    # story tasks go to DEV_RUNNING
    Phase.PENDING: frozenset({Phase.DEV_RUNNING, Phase.TRIAGE_RUNNING}),
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
    Phase.COMMITTING: frozenset({Phase.DONE, Phase.ESCALATED}),
    Phase.TRIAGE_RUNNING: frozenset({Phase.TRIAGE_VERIFY}),
    # TRIAGE_RUNNING: invalid triage output retries with feedback, like DEV_VERIFY
    Phase.TRIAGE_VERIFY: frozenset({Phase.TRIAGE_RUNNING, Phase.DONE, Phase.ESCALATED}),
    Phase.DONE: frozenset(),
    Phase.DEFERRED: frozenset(),
    Phase.ESCALATED: frozenset(),
}


def advance(task: StoryTask, to: Phase) -> None:
    allowed = TRANSITIONS[task.phase]
    if to not in allowed:
        raise IllegalTransition(
            f"{task.story_key}: {task.phase} -> {to} (allowed: {sorted(allowed)})"
        )
    task.phase = to
