"""`tickets_source`: the pure `decide_pick` table and the `TicketsWorkSource`
wrapper over a `TicketTreeLike`.

Real-spawn budget: none. Every view is built in memory (`conftest.ticket_row`,
`fake_next_view`, `fake_status_view`) and the source reads a `FakeTicketTree`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    FakeTicketTree,
    fake_find,
    fake_next_view,
    fake_status_view,
    ticket_row,
)

from bmad_loop import tickets_source as ts
from bmad_loop.model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_TICKET_HITL,
    PAUSE_TICKET_REFINE,
    PAUSE_TICKET_TREE,
    Phase,
    RunState,
    StoryTask,
)
from bmad_loop.platform_util import safe_segment
from bmad_loop.ticketmodel import EpicRow, NextView, StatusView, TicketRow
from bmad_loop.tickettree import TicketsCrashed, TicketsPinMismatch
from bmad_loop.worksource import Halt as WorkHalt

CART, CHECKOUT = "epic-cart", "epic-checkout"


def _epic(slug: str, eid: str, status: str = "in-progress", **kw) -> EpicRow:
    return EpicRow(
        slug=slug,
        id=eid,
        status=status,
        after=(),
        gated_by=kw.get("gated_by", ()),
        blocks=(),
        next_id=3,
        folder=slug,
        refined=True,
    )


EPICS = (_epic(CART, "1"), _epic(CHECKOUT, "2", gated_by=(CART,)))
# Declared in the initiative, no folder yet (`folder: null`): no file, no tickets.
UNSLICED = EpicRow(
    slug="epic-payment",
    id="3",
    status="",
    after=(),
    gated_by=(),
    blocks=(),
    next_id=None,
    folder=None,
    refined=False,
)


def _views(
    *,
    refine: tuple[TicketRow, ...] = (),
    start: tuple[TicketRow, ...] = (),
    in_progress: tuple[TicketRow, ...] = (),
    blocked: tuple[TicketRow, ...] = (),
    others: tuple[TicketRow, ...] = (),
    order: tuple[TicketRow, ...] | None = None,
    epics: tuple[EpicRow, ...] = EPICS,
) -> tuple[NextView, StatusView]:
    """`next` with the given groups; `status` lists ``order`` (default: every row
    sorted by ref, which is build order for these fixtures)."""
    every = (*refine, *start, *in_progress, *blocked, *others)
    rows = order if order is not None else tuple(sorted(every, key=lambda r: r.ref or ""))
    return (
        fake_next_view(
            ready_to_refine=refine, ready_to_start=start, in_progress=in_progress, blocked=blocked
        ),
        fake_status_view(rows, epics=epics),
    )


def _done(ref: str, epic: str = CART) -> TicketRow:
    return ticket_row(ref, epic=epic, status="done")


def _waits(ref: str, *on: str, epic: str = CART, **kw) -> TicketRow:
    return ticket_row(ref, epic=epic, waiting_on=on, **kw)


# (id, views, phases, closures, approvals, expected). `expected` is
# ("dispatch", ref) | ("skip", reason, held, waiting) | ("halt", cause, key, stage).
CASES = [
    pytest.param(
        _views(start=(ticket_row("1.1"), ticket_row("1.2"))),
        {},
        {},
        (),
        ("dispatch", "1.1"),
        id="first-ready-dispatches",
    ),
    pytest.param(
        _views(
            start=(ticket_row("1.2"), ticket_row("1.1")),
            order=(ticket_row("1.1"), ticket_row("1.2")),
        ),
        {},
        {},
        (),
        ("dispatch", "1.1"),
        id="build-order-is-status-order-not-next-order",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", status="ready-for-dev"), ticket_row("1.2"))),
        {"1.1": Phase.DEFERRED},
        {},
        (),
        ("dispatch", "1.2"),
        id="touched-this-run-is-skipped-not-redispatched",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1"),)),
        {"1.1": Phase.ESCALATED},
        {},
        (),
        ("dispatch", "1.1"),
        id="pick-time-wedge-rereads-the-tree",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", hitl=True), ticket_row("1.2"))),
        {},
        {},
        (),
        ("halt", ts.HALT_HITL, "1.1", PAUSE_TICKET_HITL),
        id="hitl",
    ),
    pytest.param(
        _views(refine=(ticket_row("1.1", type="bug", refine=True),)),
        {},
        {},
        (),
        ("halt", ts.HALT_REFINE, "1.1", PAUSE_TICKET_REFINE),
        id="refine",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", refine=True, refined=True),)),
        {},
        {},
        (),
        ("dispatch", "1.1"),
        id="refined-bug-dispatches",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", refined=True, plan_checkpoint=True),)),
        {},
        {},
        (),
        ("halt", ts.HALT_PLAN_CHECKPOINT, "1.1", PAUSE_PLAN_CHECKPOINT),
        id="refined-plan-checkpoint-unapproved",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", refined=True, plan_checkpoint=True),)),
        {},
        {},
        ("1.1",),
        ("dispatch", "1.1"),
        id="refined-plan-checkpoint-approved",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", plan_checkpoint=True),)),
        {},
        {},
        (),
        ("dispatch", "1.1"),
        id="unrefined-plan-checkpoint-dispatches-its-halt-leg",
    ),
    pytest.param(
        _views(in_progress=(ticket_row("1.1", status="in-progress"),)),
        {},
        {},
        (),
        ("halt", ts.HALT_FOREIGN_ACTIVE, "1.1", PAUSE_TICKET_TREE),
        id="foreign-in-progress-blocks",
    ),
    pytest.param(
        _views(in_progress=(ticket_row("1.1", status="built"),), blocked=(_waits("1.2", "1"),)),
        {},
        {},
        (),
        ("halt", ts.HALT_FOREIGN_ACTIVE, "1.1", PAUSE_TICKET_TREE),
        id="foreign-built-blocks",
    ),
    pytest.param(
        _views(in_progress=(ticket_row("1.1", status="built"),), start=(ticket_row("1.2"),)),
        {},
        {},
        (),
        ("dispatch", "1.2"),
        id="foreign-built-is-passed-over-never-dispatched",
    ),
    pytest.param(
        _views(in_progress=(ticket_row("1.1", status="built"),), epics=(_epic(CART, "1"),)),
        {"1.1": Phase.DONE},
        {},
        (),
        ("skip", ts.SKIP_EMPTY, (), ()),
        id="own-built-finishes",
    ),
    pytest.param(
        _views(others=(_done("1.1"),), epics=(_epic(CART, "1", status="done"), EPICS[1])),
        {},
        {},
        (),
        ("halt", ts.HALT_STALLED, CHECKOUT, PAUSE_TICKET_TREE),
        id="open-epic-without-tickets-halts",
    ),
    pytest.param(
        _views(
            blocked=(
                ticket_row("1.1", status="blocked", blocked_reason="awaiting operator: x"),
                _waits("1.2", "1"),
            )
        ),
        {"1.1": Phase.AWAITING_OPERATOR},
        {},
        (),
        ("skip", ts.SKIP_PARKED, ("1.1",), ("1.2",)),
        id="blocked-behind-own-parked",
    ),
    pytest.param(
        _views(start=(ticket_row("1.1", status="ready-for-dev"),), blocked=(_waits("1.2", "1"),)),
        {"1.1": Phase.DEFERRED},
        {},
        (),
        ("skip", ts.SKIP_PARKED, ("1.1",), ("1.2",)),
        id="blocked-behind-own-deferred",
    ),
    pytest.param(
        _views(
            start=(ticket_row("1.1", status="ready-for-dev"),),
            blocked=(_waits("1.2", "1"), _waits("1.3", "2"), _waits("2.1", CART, epic=CHECKOUT)),
            epics=(_epic(CART, "1"), _epic(CHECKOUT, "2")),
        ),
        {"1.1": Phase.DEFERRED},
        {},
        (),
        ("skip", ts.SKIP_PARKED, ("1.1",), ("1.2", "1.3", "2.1")),
        id="blocked-transitively-behind-own-deferred",
    ),
    pytest.param(
        _views(
            start=(ticket_row("1.1", status="ready-for-dev"),),
            blocked=(_waits("1.2", "1"), _waits("1.3", "9")),
        ),
        {"1.1": Phase.DEFERRED},
        {},
        (),
        ("halt", ts.HALT_STALLED, "1.3", PAUSE_TICKET_TREE),
        id="unrelated-wait-stalls-despite-own-deferred",
    ),
    pytest.param(
        _views(
            start=(ticket_row("1.1", status="ready-for-dev"),),
            blocked=(_waits("1.2", "1", "9"),),
        ),
        {"1.1": Phase.DEFERRED},
        {},
        (),
        ("halt", ts.HALT_STALLED, "1.2", PAUSE_TICKET_TREE),
        id="partly-unexplained-wait-stalls",
    ),
    pytest.param(
        _views(blocked=(ticket_row("1.1", status="blocked", blocked_reason="tests red"),)),
        {},
        {},
        (),
        ("halt", ts.HALT_BLOCKED_PLAN, "1.1", PAUSE_ESCALATION),
        id="foreign-blocked-plan-escalates",
    ),
    pytest.param(
        _views(
            blocked=(ticket_row("1.1", status="blocked"),),
            start=(ticket_row("2.1", epic=CHECKOUT),),
            epics=(_epic(CART, "1"), _epic(CHECKOUT, "2")),
        ),
        {},
        {},
        (),
        ("dispatch", "2.1"),
        id="blocked-plan-does-not-hold-independent-work",
    ),
    pytest.param(
        _views(blocked=(ticket_row("1.1", unknown="which payment provider?"),)),
        {},
        {},
        (),
        ("halt", ts.HALT_REFINE, "1.1", PAUSE_TICKET_REFINE),
        id="open-unknown-needs-refining",
    ),
    pytest.param(
        _views(
            blocked=(_waits("2.1", CART, epic=CHECKOUT, gated_by=(CART,)),),
            others=(_done("1.1"), _done("1.2")),
        ),
        {},
        {},
        (),
        ("halt", ts.HALT_AWAITING_CLOSURE, CART, PAUSE_EPIC_BOUNDARY),
        id="gated-by-awaiting-closure",
    ),
    pytest.param(
        _views(others=(_done("1.1"), _done("1.2"))),
        {},
        {},
        (),
        ("halt", ts.HALT_AWAITING_CLOSURE, CART, PAUSE_EPIC_BOUNDARY),
        id="empty-with-open-epic-halts",
    ),
    pytest.param(
        _views(
            start=(ticket_row("2.1", epic=CHECKOUT),),
            others=(_done("1.1"),),
            epics=(_epic(CART, "1"), _epic(CHECKOUT, "2")),
        ),
        {},
        {CART: ts.CLOSURE_REJECTED},
        (),
        ("halt", ts.HALT_EPIC_REJECTED, CART, PAUSE_EPIC_BOUNDARY),
        id="rejected-epic-holds-the-next-epic",
    ),
    pytest.param(
        _views(start=(ticket_row("1.3"),), others=(_done("1.1"),)),
        {},
        {CART: ts.CLOSURE_REJECTED},
        (),
        ("dispatch", "1.3"),
        id="rejected-epic-runs-its-own-fixups",
    ),
    pytest.param(
        _views(
            start=(ticket_row("2.1", epic=CHECKOUT),),
            epics=(_epic(CART, "1", status="done"), _epic(CHECKOUT, "2")),
        ),
        {},
        {CART: ts.CLOSURE_REJECTED},
        (),
        ("dispatch", "2.1"),
        id="closed-rejected-epic-holds-nothing",
    ),
    pytest.param(
        _views(blocked=(_waits("1.2", "9"),)),
        {},
        {},
        (),
        ("halt", ts.HALT_STALLED, "1.2", PAUSE_TICKET_TREE),
        id="unexplained-wait-stalls",
    ),
    pytest.param(
        _views(
            start=(replace(ticket_row("1.1", file="story-orphan.md"), ref=None), ticket_row("1.2")),
            order=(ticket_row("1.2"),),
        ),
        {},
        {},
        (),
        ("dispatch", "1.2"),
        id="row-without-ref-is-skipped",
    ),
    pytest.param(
        _views(others=(_done("1.1"),), epics=(_epic(CART, "1", status="done"),)),
        {},
        {},
        (),
        ("skip", ts.SKIP_EMPTY, (), ()),
        id="empty-finishes",
    ),
]


# `run --epic` / `--story` (a `PickScope`): the same table, with a selector. Each
# row's unscoped answer differs, which the `*-unscoped` comments name.
SCOPE_CASES = [
    pytest.param(
        _views(start=(ticket_row("1.1"), ticket_row("2.1", epic=CHECKOUT))),
        {},
        {},
        (),
        ("dispatch", "2.1"),
        ts.PickScope(epic=CHECKOUT),
        id="epic-filter-passes-earlier-epics",  # unscoped: dispatch 1.1
    ),
    pytest.param(
        _views(
            in_progress=(ticket_row("1.2", status="built"),),
            others=(_done("2.1", CHECKOUT),),
            epics=(_epic(CART, "1"), _epic(CHECKOUT, "2", status="done")),
        ),
        {},
        {},
        (),
        ("skip", ts.SKIP_EMPTY, (), ()),
        ts.PickScope(epic=CHECKOUT),
        id="epic-filter-ignores-foreign-rows-outside-it",  # unscoped: foreign-active
    ),
    pytest.param(
        _views(others=(_done("1.1"), _done("2.1", CHECKOUT))),
        {},
        {},
        (),
        ("halt", ts.HALT_AWAITING_CLOSURE, CHECKOUT, PAUSE_EPIC_BOUNDARY),
        ts.PickScope(epic=CHECKOUT),
        id="epic-filter-scopes-awaiting-closure",  # unscoped: awaiting epic-cart
    ),
    pytest.param(
        _views(
            start=(ticket_row("2.1", epic=CHECKOUT),),
            others=(_done("1.1"),),
            epics=(_epic(CART, "1"), _epic(CHECKOUT, "2")),
        ),
        {},
        {CART: ts.CLOSURE_REJECTED},
        (),
        ("halt", ts.HALT_EPIC_REJECTED, CART, PAUSE_EPIC_BOUNDARY),
        ts.PickScope(epic=CHECKOUT),
        id="epic-filter-still-held-by-a-rejected-epic",  # same unscoped
    ),
    pytest.param(
        _views(start=(ticket_row("1.1"), ticket_row("1.2"))),
        {},
        {},
        (),
        ("dispatch", "1.2"),
        ts.PickScope(ref="1.2"),
        id="ref-filter-dispatches-only-its-ticket",  # unscoped: dispatch 1.1
    ),
    pytest.param(
        _views(others=(_done("1.1"),), epics=(_epic(CART, "1"),)),
        {"1.1": Phase.DONE},
        {},
        (),
        ("skip", ts.SKIP_EMPTY, (), ()),
        ts.PickScope(ref="1.1"),
        id="ref-filter-finishes-without-epic-halts",  # unscoped: awaiting epic-cart
    ),
]


@pytest.mark.parametrize(
    ("views", "phases", "closures", "approvals", "expected", "scope"),
    [pytest.param(*case.values, ts.EVERYTHING, id=case.id) for case in CASES] + SCOPE_CASES,
)
def test_decide_pick_table(views, phases, closures, approvals, expected, scope):
    """R17 and critique #5 as one table. Ablation (performed), each turning its
    rows red: route touched rows to dispatch (touched-* rows); drop the foreign
    halt (foreign-*-blocks); drop the parked arm (blocked-behind-own-*); drop the
    gated-wait arm (gated-by-awaiting-closure); drop the trailing open-epic arm
    (empty-with-open-epic-halts, which then finishes); drop the approvals test
    (refined-plan-checkpoint-approved); drop the `refined` test from the plan
    checkpoint arm (unrefined-plan-checkpoint-dispatches-its-halt-leg); make
    `PickScope.admits` admit every row (epic-filter-passes-*,
    epic-filter-ignores-*, ref-filter-dispatches-*); make `admits_epic` admit
    every epic (epic-filter-scopes-*, ref-filter-finishes-*)."""
    next_view, status_view = views
    decision = ts.decide_pick(next_view, status_view, phases, closures, approvals, scope)

    if expected[0] == "dispatch":
        assert isinstance(decision, ts.Dispatch), decision
        assert decision.ref == expected[1]
    elif expected[0] == "skip":
        assert isinstance(decision, ts.Skip), decision
        assert (decision.reason, decision.held, decision.waiting) == expected[1:]
    else:
        assert isinstance(decision, ts.Halt), decision
        assert (decision.cause, decision.key, decision.stage) == expected[1:]


def test_decide_pick_skips_an_acked_epic_only_at_the_end_of_the_tree():
    """An epic whose closure pause the operator resumed past (``acked``) no longer
    halts the empty tree: the run finishes. A ticket gated by that epic still
    halts ``awaiting-closure``, since tickets.py holds it until the close.
    Ablation (performed): drop the ``acked`` filter and the empty tree halts
    again — this FAILS."""
    empty = _views(others=(_done("1.1"), _done("1.2")), epics=(_epic(CART, "1"),))
    decision = ts.decide_pick(*empty, {}, {}, (), acked=frozenset({CART}))
    assert isinstance(decision, ts.Skip) and decision.reason == ts.SKIP_EMPTY, decision

    gated = _views(
        blocked=(_waits("2.1", CART, epic=CHECKOUT, gated_by=(CART,)),),
        others=(_done("1.1"), _done("1.2")),
    )
    decision = ts.decide_pick(*gated, {}, {}, (), acked=frozenset({CART}))
    assert isinstance(decision, ts.Halt), decision
    assert (decision.cause, decision.key) == (ts.HALT_AWAITING_CLOSURE, CART)


def test_decide_pick_notes_what_it_passed_over():
    """A dispatch carries the rows it passed over — a ticket this run touched and a
    foreign built one — for the engine to journal; neither is dispatched."""
    next_view, status_view = _views(
        start=(ticket_row("1.1", status="ready-for-dev"), ticket_row("1.3")),
        in_progress=(ticket_row("1.2", status="built"),),
    )

    decision = ts.decide_pick(next_view, status_view, {"1.1": Phase.DEFERRED}, {}, ())

    assert isinstance(decision, ts.Dispatch) and decision.ref == "1.3"
    assert decision.notes == (
        ts.PickNote(ts.NOTE_FOREIGN_ACTIVE, "1.2", "built"),
        ts.PickNote(ts.NOTE_SKIPPED, "1.1", "touched", "deferred"),
    )


def test_backlog_is_never_picked():
    """Only the initiative's own view is a candidate source: a ready backlog row
    with nothing else open finishes the run. Ablation (performed): add the
    backlog's `ready_to_start` to the candidates and this FAILS."""
    backlog = fake_next_view(folder="backlog", ready_to_start=(ticket_row("story-x.md"),))
    next_view, status_view = _views(epics=(_epic(CART, "1", status="done"),))
    next_view = NextView(**{**next_view.__dict__, "backlog": backlog})

    decision = ts.decide_pick(next_view, status_view, {}, {}, ())

    assert decision == ts.Skip(ts.SKIP_EMPTY)


@pytest.mark.parametrize(
    ("source", "stage", "key", "registered", "approved"),
    [
        ("tickets", PAUSE_PLAN_CHECKPOINT, "1.1", False, "1.1"),
        ("tickets", PAUSE_PLAN_CHECKPOINT, "1.1", True, None),
        ("tickets", PAUSE_TICKET_HITL, "1.1", False, None),
        ("tickets", PAUSE_PLAN_CHECKPOINT, None, False, None),
        ("stories", PAUSE_PLAN_CHECKPOINT, "1", False, None),
    ],
    ids=["refined-pick-pause", "unit-pause", "other-stage", "no-key", "stories-run"],
)
def test_approve_paused_story_file(source, stage, key, registered, approved):
    """Only a tickets run's unregistered `plan-checkpoint` pause is a story-file
    approval; every other paused state approves nothing, and approving twice
    records the ref once. Ablation (performed): drop the source test and the
    stories row approves its key — this FAILS."""
    state = RunState(run_id="r", project="/p", started_at="now", source=source)
    state.paused_stage, state.paused_story_key = stage, key
    if registered and key:
        state.tasks[key] = StoryTask(story_key=key, epic=0)

    assert ts.approve_paused_story_file(state) == approved
    assert ts.approve_paused_story_file(state) is None
    assert state.ticket_story_approvals == ([approved] if approved else [])


def test_halt_stages_cover_every_cause():
    """Every halt cause maps to one of R4's pause stages."""
    causes = {v for k, v in vars(ts).items() if k.startswith("HALT_") and isinstance(v, str)}
    assert set(ts.HALT_STAGES) == causes
    assert set(ts.HALT_STAGES.values()) == {
        PAUSE_TICKET_HITL,
        PAUSE_TICKET_REFINE,
        PAUSE_TICKET_TREE,
        PAUSE_PLAN_CHECKPOINT,
        PAUSE_EPIC_BOUNDARY,
        PAUSE_ESCALATION,
    }


@pytest.mark.parametrize(
    ("fields", "halt_leg"),
    [
        ({"plan_checkpoint": True}, True),
        ({"plan_checkpoint": True, "status": "draft"}, True),
        ({"plan_checkpoint": True, "status": "ready-for-dev"}, False),
        ({"plan_checkpoint": True, "status": "in-progress"}, False),
        ({"plan_checkpoint": True, "refined": True}, False),
        ({}, False),
    ],
)
def test_is_plan_halt_leg(fields, halt_leg):
    """Leg 1 of an unrefined plan checkpoint is the dispatch before the plan is
    planned; a refined ticket's checkpoint is its story file's approval instead."""
    assert ts.is_plan_halt_leg(ticket_row("1.1", **fields)) is halt_leg


def test_unit_key_is_the_ref_and_a_legal_segment():
    """The unit key is the `E.M` ref, unchanged by `safe_segment`, so run-dir paths
    and branch names keyed by it stay byte-identical to the ref."""
    for ref in ("1.1", "12.3", "1.6a"):
        assert safe_segment(ref) == ref


def test_ticket_binding_from_find(tmp_path: Path):
    """`find` binds the ref, the entry id, the epic slug and its id (the ref's
    epic part), the plan path and the entry flags."""
    row = ticket_row("2.1", epic=CHECKOUT, refined=True, plan_checkpoint=True, risk="high")
    found = fake_find(row, tmp_path / CHECKOUT)

    binding = ts.ticket_binding(found)

    assert (binding.ref, binding.id, binding.epic_slug, binding.epic_id) == (
        "2.1",
        "1",
        CHECKOUT,
        "2",
    )
    assert binding.plan == found.plan
    assert (binding.refined, binding.plan_checkpoint, binding.risk) == (True, True, "high")


# --------------------------------------------------------------- the source


def _source(tree: FakeTicketTree, approvals=(), closures=None) -> ts.TicketsWorkSource:
    return ts.TicketsWorkSource(lambda: tree, lambda: list(approvals), lambda: closures or {})


def test_source_pick_maps_the_decision(tmp_path: Path):
    """`pick` (the WorkSource view) dispatches a `TicketRef` keyed by the ref at
    epic 0, and carries a halt's full decision as the payload."""
    tree = FakeTicketTree.from_rows(tmp_path, ready_to_start=(ticket_row("1.1"),))
    pick = _source(tree).pick({}, None)
    assert pick.ref is not None and (pick.ref.key, pick.ref.epic) == ("1.1", 0)

    tree = FakeTicketTree.from_rows(tmp_path, ready_to_start=(ticket_row("1.1", hitl=True),))
    pick = _source(tree).pick({}, None)
    assert isinstance(pick.halt, WorkHalt) and pick.halt.cause == ts.HALT_HITL
    assert isinstance(pick.halt.payload, ts.Halt)


def test_source_reads_approvals_live(tmp_path: Path):
    """The source reads the run's approvals on every pick, never a snapshot."""
    row = ticket_row("1.1", refined=True, plan_checkpoint=True)
    tree = FakeTicketTree.from_rows(tmp_path, ready_to_start=(row,))
    approvals: list[str] = []
    source = ts.TicketsWorkSource(lambda: tree, lambda: approvals, dict)

    assert isinstance(source.decide({}), ts.Halt)
    approvals.append("1.1")
    assert isinstance(source.decide({}), ts.Dispatch)


@pytest.mark.parametrize(
    ("fault", "cause"),
    [
        (TicketsPinMismatch("shop", "other"), ts.HALT_PIN_MISMATCH),
        (TicketsCrashed("tickets.py died"), ts.HALT_TREE_FAULT),
    ],
)
def test_source_tree_faults_halt_ticket_tree(tmp_path: Path, fault, cause):
    """A tree that cannot be read halts at `ticket-tree` with the error's kind in
    the detail, never an exhausted pick that would finish the run."""
    tree = FakeTicketTree.from_rows(tmp_path, ready_to_start=(ticket_row("1.1"),))
    tree.faults["next"] = fault

    decision = _source(tree).decide({})

    assert isinstance(decision, ts.Halt)
    assert (decision.cause, decision.stage, decision.key) == (cause, PAUSE_TICKET_TREE, "")
    assert fault.kind in decision.detail


def test_source_remaining_estimate(tmp_path: Path):
    """Open tickets this run has not touched and that are not already in review."""
    tree = FakeTicketTree.from_rows(
        tmp_path,
        ready_to_start=(ticket_row("1.1"), ticket_row("1.2")),
        in_progress=(ticket_row("1.3", status="built"),),
        others=(ticket_row("1.4", status="done"),),
    )
    assert _source(tree).remaining_estimate({"1.1": Phase.DEFERRED}) == 1


def test_source_remaining_estimate_counts_only_the_scope(tmp_path: Path):
    """A `--epic` or `--story` run estimates only the tickets its scope admits:
    the rest of the initiative is never picked, so it is not left for a resume.

    Ablation (performed): drop the `scope.admits` filter and both scoped
    estimates count every open ticket — this FAILS."""
    tree = FakeTicketTree.from_rows(
        tmp_path,
        ready_to_start=(
            ticket_row("1.1"),
            ticket_row("1.2"),
            ticket_row("2.1", epic=CHECKOUT),
        ),
    )

    def scoped(scope: ts.PickScope) -> ts.TicketsWorkSource:
        return ts.TicketsWorkSource(lambda: tree, list, dict, lambda: scope)

    assert scoped(ts.EVERYTHING).remaining_estimate({}) == 3
    assert scoped(ts.PickScope(epic=CHECKOUT)).remaining_estimate({}) == 1
    assert scoped(ts.PickScope(ref="1.2")).remaining_estimate({}) == 1
    assert scoped(ts.PickScope(ref="1.2")).remaining_estimate({"1.2": Phase.DEFERRED}) == 0


def _boundary(views, closures=None, acked=(), committed=(CART,), resumed=()):
    _, status_view = views
    return [
        (b.action, b.slug, b.verdict)
        for b in ts.epic_boundary(
            status_view,
            closures or {},
            frozenset(acked),
            frozenset(committed),
            resumed=frozenset(resumed),
        )
    ]


@pytest.mark.parametrize(
    ("views", "closures", "acked", "committed", "resumed", "expected"),
    [
        pytest.param(
            _views(others=(_done("1.1"), ticket_row("1.2", status="built"))),
            None,
            (),
            (CART,),
            (),
            [("retro", CART, "")],
            id="finished-and-committed-owes-a-retro",
        ),
        pytest.param(
            _views(others=(_done("1.1"),)),
            None,
            (),
            (),
            (),
            [],
            id="finished-by-someone-else-owes-nothing",
        ),
        pytest.param(
            _views(others=(_done("1.1"), ticket_row("1.2", status="in-review"))),
            None,
            (),
            (CART,),
            (),
            [],
            id="unfinished-owes-nothing",
        ),
        pytest.param(
            _views(others=(_done("1.1"),), epics=(_epic(CART, "1", status="done"),)),
            {CART: "rejected"},
            (),
            (CART,),
            (),
            [],
            id="closed-epic-owes-nothing",
        ),
        pytest.param(
            _views(others=(_done("1.1"),)),
            {CART: "rejected"},
            (),
            (CART,),
            (),
            [("rejected", CART, "rejected")],
            id="rejected-open-and-finished-pauses-again",
        ),
        pytest.param(
            _views(start=(ticket_row("1.3"),), others=(_done("1.1"),)),
            {CART: "rejected"},
            (),
            (CART,),
            (),
            [],
            id="rejected-with-a-fix-up-ticket-builds-it-first",
        ),
        pytest.param(
            _views(others=(_done("1.1"), _done("2.1", epic=CHECKOUT))),
            {CART: "accepted"},
            (),
            (CART, CHECKOUT),
            (CART,),
            [("ack", CART, "accepted"), ("retro", CHECKOUT, "")],
            id="resumed-closure-then-the-next-epic-in-build-order",
        ),
        pytest.param(
            _views(others=(_done("1.1"), _done("2.1", epic=CHECKOUT))),
            {CART: "accepted"},
            (),
            (CART, CHECKOUT),
            (),
            [("pause", CART, "accepted"), ("retro", CHECKOUT, "")],
            id="closure-no-resume-answered-pauses-for-it",
        ),
        pytest.param(
            _views(others=(_done("1.1"),)),
            {CART: "unknown"},
            (CART,),
            (CART,),
            (),
            [],
            id="acked-closure-owes-nothing",
        ),
        pytest.param(
            _views(others=(_done("1.1"),), epics=(_epic(CART, "1"), UNSLICED)),
            {"epic-payment": "accepted"},
            (),
            (CART, "epic-payment"),
            (),
            [("retro", CART, "")],
            id="unsliced-epic-owes-nothing-even-with-a-closure",
        ),
    ],
)
def test_epic_boundary(views, closures, acked, committed, resumed, expected):
    """The epic-boundary actions, in build order, over the open epics: a retro
    for a finished epic this run committed to, a re-pause for a rejected one
    still finished, an acknowledgement for any other unacknowledged closure a
    resume answered, and a pause for one no resume answered (the run stopped
    between recording it and pausing); an epic not sliced yet owes nothing.
    Ablation (performed): emit the acknowledgement whatever `resumed` holds and
    the no-resume row FAILS; drop the `folder is None` skip and the unsliced row
    FAILS."""
    assert _boundary(views, closures, acked, committed, resumed) == expected


@pytest.mark.parametrize(
    ("source", "stage", "key", "closures", "recorded"),
    [
        ("tickets", PAUSE_EPIC_BOUNDARY, CART, {CART: "accepted"}, CART),
        ("tickets", PAUSE_EPIC_BOUNDARY, CART, {CART: "rejected"}, None),
        ("tickets", PAUSE_EPIC_BOUNDARY, CART, {}, None),
        ("tickets", PAUSE_TICKET_HITL, CART, {CART: "accepted"}, None),
        ("tickets", PAUSE_EPIC_BOUNDARY, None, {CART: "accepted"}, None),
        ("stories", PAUSE_EPIC_BOUNDARY, CART, {CART: "accepted"}, None),
    ],
    ids=["closure-pause", "rejected", "refusal-pause", "other-stage", "no-key", "stories-run"],
)
def test_resume_epic_closure(source, stage, key, closures, recorded):
    """Only a tickets run's `epic-boundary` pause on an epic with a recorded
    non-rejected closure is answered by a resume; a refused retro's pause (no
    closure yet) and a rejected verdict (it pauses again anyway) record
    nothing, and recording twice records the slug once. Ablation (performed):
    drop the closure test and the refusal-pause row records — this FAILS."""
    state = RunState(run_id="r", project="/p", started_at="now", source=source)
    state.paused_stage, state.paused_story_key = stage, key
    state.ticket_epic_closures = dict(closures)

    assert ts.resume_epic_closure(state) == recorded
    assert ts.resume_epic_closure(state) is None
    assert state.ticket_epic_closure_resumed == ([recorded] if recorded else [])


def test_source_decide_gates_on_the_pick_status_view(tmp_path: Path):
    """`decide`'s gate sees the very `status` view the pick reads, before the
    pick, and a gate that raises stops the pick."""
    tree = FakeTicketTree.from_rows(tmp_path, ready_to_start=(ticket_row("1.1"),))
    seen = []

    def gate(status_view):
        seen.append(status_view)
        raise RuntimeError("paused")

    with pytest.raises(RuntimeError):
        _source(tree).decide({}, gate=gate)

    assert seen == [tree.status_view]
    assert [c[0] for c in tree.calls] == ["next", "status"]
