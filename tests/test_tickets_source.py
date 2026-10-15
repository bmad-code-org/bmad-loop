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
    )


EPICS = (_epic(CART, "1"), _epic(CHECKOUT, "2", gated_by=(CART,)))


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


@pytest.mark.parametrize(("views", "phases", "closures", "approvals", "expected"), CASES)
def test_decide_pick_table(views, phases, closures, approvals, expected):
    """R17 and critique #5 as one table. Ablation (performed), each turning its
    rows red: route touched rows to dispatch (touched-* rows); drop the foreign
    halt (foreign-*-blocks); drop the parked arm (blocked-behind-own-*); drop the
    gated-wait arm (gated-by-awaiting-closure); drop the trailing open-epic arm
    (empty-with-open-epic-halts, which then finishes); drop the approvals test
    (refined-plan-checkpoint-approved); drop the `refined` test from the plan
    checkpoint arm (unrefined-plan-checkpoint-dispatches-its-halt-leg)."""
    next_view, status_view = views
    decision = ts.decide_pick(next_view, status_view, phases, closures, approvals)

    if expected[0] == "dispatch":
        assert isinstance(decision, ts.Dispatch), decision
        assert decision.ref == expected[1]
    elif expected[0] == "skip":
        assert isinstance(decision, ts.Skip), decision
        assert (decision.reason, decision.held, decision.waiting) == expected[1:]
    else:
        assert isinstance(decision, ts.Halt), decision
        assert (decision.cause, decision.key, decision.stage) == expected[1:]


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
