import pytest

from bmad_loop.model import TERMINAL_PHASES, Phase, StoryTask
from bmad_loop.sprintstatus import STATUS_ORDER
from bmad_loop.statemachine import (
    BOARD_REGRESSIONS,
    PLAN_AWAITING_OPERATOR,
    PLAN_STATUS_ORDER,
    PLAN_TRANSITIONS,
    TRANSITIONS,
    IllegalTransition,
    PlanTransitionRefused,
    advance,
    check_board_regression,
    check_plan_transition,
)
from bmad_loop.ticketmodel import PLAN_STATUSES


def test_table_covers_every_phase():
    assert set(TRANSITIONS) == set(Phase)


def test_terminal_phases_are_exactly_the_dead_ends():
    """`TERMINAL_PHASES` and the transition table are two spellings of the same
    fact, in two modules, with nothing linking them: a new terminal phase added
    to the table with no outgoing edges but forgotten in the frozenset would be
    driven as if it were still live (`StoryTask.terminal` is False), and every
    other test would still pass. Pin them to each other."""
    dead_ends = {phase for phase, targets in TRANSITIONS.items() if not targets}
    assert dead_ends == set(TERMINAL_PHASES)


@pytest.mark.parametrize("source", list(Phase))
@pytest.mark.parametrize("target", list(Phase))
def test_exhaustive_transitions(source, target):
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    if target in TRANSITIONS[source]:
        advance(task, target)
        assert task.phase == target
    else:
        with pytest.raises(IllegalTransition):
            advance(task, target)
        assert task.phase == source


def test_happy_path_sequence():
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_park_path_sequence():
    """A story owing human-only external actions rides the NORMAL commit path and
    parks at the end of it: COMMITTING is the only phase it is reachable from, so
    a park can never skip the gates and the commit a DONE story clears."""
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.AWAITING_OPERATOR,
    ):
        advance(task, phase)
    assert task.terminal


@pytest.mark.parametrize(
    "source",
    [p for p in Phase if p is not Phase.COMMITTING],
)
def test_awaiting_operator_is_reachable_only_from_committing(source):
    """The explicit inverse of the happy path above. `test_exhaustive_transitions`
    covers this pairwise too, but only as one cell of an N-squared grid whose
    expectation is read out of the table under test — this states the rule
    independently, so widening the table cannot silently widen the test."""
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    with pytest.raises(IllegalTransition):
        advance(task, Phase.AWAITING_OPERATOR)
    assert task.phase == source


def test_triage_path_sequence():
    task = StoryTask(story_key="sweep-triage", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.TRIAGE_RUNNING,  # invalid triage output retries
        Phase.TRIAGE_VERIFY,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_migration_triage_commit_path_sequence():
    task = StoryTask(story_key="sweep-migrate", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_pending_migration_with_an_empty_manifest_completes_as_done():
    """DW-440: a sweep migrate task whose input holds no legacy entries has nothing
    to convert, so `_ensure_migration` retires it straight from PENDING to DONE
    through `advance()` rather than dispatching a session. DONE is terminal, so
    `migration_resume` never re-enters it."""
    task = StoryTask(story_key="sweep-migrate", epic=0)
    advance(task, Phase.DONE)
    assert task.phase == Phase.DONE
    assert task.terminal
    with pytest.raises(IllegalTransition):
        advance(task, Phase.TRIAGE_RUNNING)


# ---------------------------------------------------------------------------
# sprint-board regression allowlist (DW-383)


def test_board_regression_allowlist_is_exactly_the_review_demotion():
    """One pair and no more: widening this set silently licenses the sole board
    writer to walk a story backward."""
    assert BOARD_REGRESSIONS == frozenset({("done", "awaiting-operator")})


def test_the_allowlisted_board_regression_passes():
    check_board_regression("1-1-x", "done", "awaiting-operator")  # no raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for i, current in enumerate(STATUS_ORDER)
        for target in STATUS_ORDER[:i]
        if (current, target) != ("done", "awaiting-operator")
    ],
)
def test_every_other_board_regression_raises(current, target):
    """Ablation: make `check_board_regression` a no-op (or widen the set) and every
    case here reddens."""
    with pytest.raises(IllegalTransition, match=f"{current} -> {target}"):
        check_board_regression("1-1-x", current, target)


# ---- BMAD v7 plan transitions -------------------------------------------------

PARK = "awaiting operator: sign the release"
OTHER_PARK = "awaiting operator: rotate the key"
ESCALATION = "build halted: spec contradicts the epic"
# Each current state the table can meet: a status, plus the reason a blocked plan
# carries — an operator park and a build-auto escalation decide differently.
PLAN_STATES = {
    "": ("", ""),
    **{s: (s, "") for s in PLAN_STATUSES if s != "blocked"},
    "blocked(escalation)": ("blocked", ESCALATION),
    "blocked(park)": ("blocked", PARK),
    # a session-declared park, outside upstream's statuses until `park` converts it
    "awaiting-operator": ("awaiting-operator", ""),
}
_BUILT_SET = ("draft", "ready-for-dev", "in-progress", "in-review", "built")
_STARTED = ("ready-for-dev", "in-progress", "in-review", "built")
_BLOCKED = ("blocked(escalation)", "blocked(park)")
# The R1 table spelled out by hand, independent of `PLAN_TRANSITIONS`:
# reason -> {(current state, target): "write" | "noop"}; every other pair refuses.
EXPECTED: dict[str, dict[tuple[str, str], str]] = {
    "accept": {("built", "done"): "write", ("done", "done"): "noop"},
    "park": {
        **{(c, "blocked"): "write" for c in _BUILT_SET},
        ("awaiting-operator", "blocked"): "write",
        ("blocked(park)", "blocked"): "noop",
    },
    "confirm": {
        ("blocked(park)", "done"): "write",
        ("blocked(park)", "built"): "write",
        ("done", "done"): "noop",
        ("built", "built"): "noop",
    },
    "repair": {
        ("in-progress", "in-progress"): "noop",
        ("in-review", "in-progress"): "write",
        ("built", "in-progress"): "write",
    },
    "rearm": {
        **{
            (c, t): ("noop" if c == t else "write")
            for c in ("in-progress", "in-review", "built", *_BLOCKED)
            for t in ("ready-for-dev", "in-progress", "in-review")
        },
        # a replayed re-arm: the plan already sits where it would be written
        ("ready-for-dev", "ready-for-dev"): "noop",
    },
    "rollback": {
        (c, "ready-for-dev"): ("noop" if c == "ready-for-dev" else "write") for c in _STARTED
    },
    "replan": {
        **{(c, "draft"): "write" for c in (*_STARTED, *_BLOCKED)},
        ("draft", "draft"): "noop",
    },
    "salvage": {
        ("in-progress", "built"): "write",
        ("in-review", "built"): "write",
        ("built", "built"): "noop",
    },
}


def _decide(reason: str, state: str, target: str, blocked: str | None = None) -> str:
    current, blocked_reason = PLAN_STATES[state]
    if blocked is None and target == "blocked":
        blocked = PARK
    try:
        write = check_plan_transition(
            "1.1",
            current,
            target,
            reason,  # type: ignore[arg-type]
            has_plan=current != "",
            blocked_reason=blocked_reason,
            blocked=blocked,
        )
    except PlanTransitionRefused:
        return "refuse"
    return "write" if write else "noop"


def test_plan_status_order_is_the_lifecycle_and_ticketmodel_reads_it():
    assert PLAN_STATUS_ORDER == (
        "",
        "draft",
        "ready-for-dev",
        "in-progress",
        "in-review",
        "built",
        "done",
    )
    assert PLAN_STATUSES == (*PLAN_STATUS_ORDER[1:], "blocked", "dropped")
    assert set(PLAN_TRANSITIONS) == set(EXPECTED)


def test_plan_transition_forward_allowed():
    """The accept path is strictly forward: built -> done, nothing else."""
    assert _decide("accept", "built", "done") == "write"
    assert _decide("accept", "done", "done") == "noop"
    for state in ("", "draft", "ready-for-dev", "in-progress", "in-review", *_BLOCKED):
        assert _decide("accept", state, "done") == "refuse", state


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (c, t)
        for i, c in enumerate(PLAN_STATUS_ORDER)
        for t in PLAN_STATUS_ORDER[1:i]
        if c != "done"
    ],
)
def test_plan_regression_refused_without_reason(current, target):
    """A backward move under `accept` (the reason that carries no licence to
    regress) is refused — the regressions belong to the named reasons alone.

    Ablation: drop the `target not in rule.targets` refusal from
    `check_plan_transition` and this reddens (a regression then falls through to
    the no-op/source checks and some is written). The `rule.sources` gate is
    graded by `test_plan_transition_table`: answer True for every current status
    and that table reddens."""
    with pytest.raises(PlanTransitionRefused, match="not a legal 'accept' write"):
        check_plan_transition("1.1", current, target, "accept", has_plan=True, blocked_reason="")


@pytest.mark.parametrize("reason", sorted(EXPECTED))
@pytest.mark.parametrize("sink", ["done", "dropped"])
def test_done_and_dropped_are_sinks(reason, sink):
    """Nothing leaves done or dropped, under any reason; the only answer from a
    sink other than a refusal is the no-op of writing done onto done.

    Two gates hold this, so one ablation alone stays green by design: delete the
    final-state refusal in `check_plan_transition` AND add `done` to a reason's
    sources (say `rearm`'s) and this reddens; either edit alone is caught by the
    other gate."""
    for target in PLAN_STATUSES:
        got = _decide(reason, sink, target)
        if (sink, target) == ("done", "done") and reason in ("accept", "confirm"):
            assert got == "noop"
        else:
            assert got == "refuse", (reason, sink, target)


def test_dropped_is_never_written():
    for reason in EXPECTED:
        for state in PLAN_STATES:
            assert _decide(reason, state, "dropped") == "refuse"


@pytest.mark.parametrize(
    ("reason", "state", "target"),
    [(r, c, t) for r in sorted(EXPECTED) for c in PLAN_STATES for t in PLAN_STATUSES],
)
def test_plan_transition_table(reason, state, target):
    """Every (reason, current, target) against the hand-written R1 table."""
    assert _decide(reason, state, target) == EXPECTED[reason].get((state, target), "refuse")


def test_no_plan_refuses_every_reason():
    """bmad-loop never creates a plan: `has_plan=False` refuses even a pair the
    table would otherwise allow."""
    for reason in EXPECTED:
        with pytest.raises(PlanTransitionRefused, match="no plan"):
            check_plan_transition("1.1", "built", "done", reason, has_plan=False, blocked_reason="")


def test_park_reason_must_be_an_operator_park():
    """A park's reason starts `awaiting operator: `; a blocked reason goes only
    with status blocked; a parked plan re-parked with another reason is a write.

    Ablation: drop the prefix check in `check_plan_transition` and the first
    assertion reddens."""
    assert _decide("park", "built", "blocked", blocked=ESCALATION) == "refuse"
    assert _decide("park", "built", "blocked", blocked=None) == "write"  # PARK by default
    assert _decide("rearm", "built", "in-progress", blocked=PARK) == "refuse"
    assert _decide("park", "blocked(park)", "blocked", blocked=OTHER_PARK) == "write"
    assert _decide("confirm", "blocked(escalation)", "done") == "refuse"


def test_leftover_blocked_reason_is_not_a_noop():
    """A plan whose status already equals the target but still carries a
    blocked reason gets written, so the reason is cleared."""
    assert (
        check_plan_transition(
            "1.1", "in-progress", "in-progress", "rearm", has_plan=True, blocked_reason=PARK
        )
        is True
    )


def test_session_declared_park_converts_to_blocked():
    """A build session parks by writing `awaiting-operator` itself; the
    commit-time conversion to an operator-park `blocked` is a legal `park` write,
    and no other reason may move an `awaiting-operator` plan.

    Ablation: drop `PLAN_AWAITING_OPERATOR` from the park rule's sources and the
    first assertion reddens (the park is refused)."""
    assert PLAN_AWAITING_OPERATOR == "awaiting-operator"
    assert _decide("park", "awaiting-operator", "blocked") == "write"
    assert _decide("park", "awaiting-operator", "blocked", blocked=ESCALATION) == "refuse"
    for reason in sorted(set(EXPECTED) - {"park"}):
        for target in PLAN_STATUSES:
            assert _decide(reason, "awaiting-operator", target) == "refuse", (reason, target)


def test_salvage_writes_built_and_never_done():
    """A timed-out review's salvage lands the plan at `built` from in-progress or
    in-review; it never writes `done` and never starts from a plan the build has
    not started.

    Ablation: widen the salvage rule's targets to include `done` and the `done`
    refusal reddens; widen its sources to every build status and the
    draft/ready-for-dev refusals redden; remove the rule and the writes redden."""
    assert _decide("salvage", "in-review", "built") == "write"
    assert _decide("salvage", "in-progress", "built") == "write"
    assert _decide("salvage", "built", "built") == "noop"
    for state in ("in-progress", "in-review", "built"):
        assert _decide("salvage", state, "done") == "refuse", state
    for state in ("", "draft", "ready-for-dev", *_BLOCKED, "awaiting-operator"):
        assert _decide("salvage", state, "built") == "refuse", state


def test_plan_transition_refused_is_an_illegal_transition():
    with pytest.raises(IllegalTransition) as caught:
        check_plan_transition("2.3", "draft", "done", "accept", has_plan=True, blocked_reason="")
    err = caught.value
    assert isinstance(err, PlanTransitionRefused)
    assert (err.ref, err.current, err.target, err.reason) == ("2.3", "draft", "done", "accept")
    assert str(err).startswith("2.3: plan draft -> done is not a legal 'accept' write")


def test_unknown_reason_is_refused():
    with pytest.raises(PlanTransitionRefused, match="unknown reason"):
        check_plan_transition(
            "1.1", "built", "done", "promote", has_plan=True, blocked_reason=""  # type: ignore[arg-type]
        )
