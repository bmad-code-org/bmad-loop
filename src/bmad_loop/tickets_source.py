"""The BMAD v7 ticket-tree work source: :class:`TicketsWorkSource` reads the
active initiative through its ``tickets.py`` for
:class:`~bmad_loop.tickets_engine.TicketsEngine`.

The pick is a pure function, :func:`decide_pick`, over two ``tickets.py`` views
(``next`` for the groups, ``status`` for build order and the epics), the phases
of the units this run already touched, the epic closures it recorded and the
refined ``plan_checkpoint`` approvals it holds. It answers :class:`Dispatch`,
:class:`Skip` (nothing to dispatch: the run finishes) or :class:`Halt` (the run
must pause; :data:`HALT_STAGES` names the stage). Like every source it never
journals: each answer carries :class:`PickNote` rows the engine journals.

**Candidates** are ``ready_to_refine`` and ``ready_to_start`` of the main view
(never the backlog's), in initiative build order: the epic table order, then
entry order, which ``status`` lists. The first candidate this run has not
touched decides the pick:

1. it needs refining (``refine`` and not ``refined``) → halt ``refine``;
2. it is a hitl ticket → halt ``hitl``;
3. it is refined with a ``plan_checkpoint`` whose story file no operator has
   approved this run → halt ``plan-checkpoint``;
4. otherwise it is dispatched.

A candidate in an epic that follows an epic whose recorded closure is
``rejected`` (and that is not closed yet) halts ``epic-rejected`` instead.

**A selector** (``run --epic`` / ``--story``, persisted as
``RunState.ticket_epic_filter`` / ``story_filter``) narrows every row the pick
reads — candidates, foreign active rows and blocked rows — to that epic's
tickets or that one ref. The epic-level causes below (awaiting closure, an open
epic with no tickets) then look at the selected epic only, and at none under a
ref selector, so a selected run finishes once its scope has nothing left.

**Rows this run touched** (every phase but ESCALATED) are routed by their task
state, never re-dispatched: a ticket that rollback re-armed to ``ready-for-dev``
shows up as a candidate again and is skipped. An ESCALATED task is a pick-time
wedge (an in-run escalation re-pauses before the pick), so its row is read from
the tree afresh, as stories mode does.

**Rows this run did not start** in ``next``'s ``in_progress`` group (an
in-progress, in-review or built plan) are never dispatched or marked; they are
noted ``foreign-active``.

**With no candidate** the first cause found, in this order, decides:

1. a blocked plan this run did not park → halt ``blocked-plan`` (an escalation);
2. an open ``unknown`` with nothing else to wait on → halt ``refine``;
3. a ``gated_by`` wait on an epic whose tickets are all done but which is not
   closed → halt ``awaiting-closure``;
4. foreign active rows → halt ``foreign-active``;
5. rows that wait only behind tickets this run retired without finishing
   (deferred, parked) — directly, through another such row, or through such a
   ticket's epic → :class:`Skip` ``parked``; any row with a wait none of those
   explains → halt ``stalled`` on it;
6. an epic still open with every ticket done (an empty ``next`` included) →
   halt ``awaiting-closure``, and an open epic with no tickets at all → halt
   ``stalled``, so the run cannot finish past an unclosed epic — except one whose
   closure pause the operator already resumed past (``acked``);
7. otherwise :class:`Skip` ``empty``: the run finishes.

**Epic boundaries** (:func:`epic_boundary`) are read before the pick, from the
same ``status`` view, so they survive a pause and a resume: an open epic whose
every ticket is ``built``, ``done`` or ``dropped``, and of which this run committed
at least one, owes its retrospective and closure pause; a recorded ``rejected``
closure re-pauses while that epic stays open and finished; any other recorded
closure pauses once and is acknowledged on the next pick.

The unit key is the ticket ref (``E.M``), which is a legal path segment and
branch component as it stands. A flat key space has no epic numbers:
:attr:`TicketsWorkSource.has_epics` is false and every unit's epic is 0; the
epic lifecycle is keyed by slug inside the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_TICKET_HITL,
    PAUSE_TICKET_REFINE,
    PAUSE_TICKET_TREE,
    Phase,
    RunState,
    TicketBinding,
)
from .statemachine import PLAN_BLOCKED, PLAN_STATUS_ORDER
from .ticketmodel import FindResult, NextView, StatusView, TicketRow
from .tickettree import TicketsError, TicketTreeLike
from .worksource import SOURCE_TICKETS, EpicProgress
from .worksource import Halt as WorkHalt
from .worksource import Pick

if TYPE_CHECKING:
    from .worksource import WorkSource

HALT_REFINE: Final = "refine"
HALT_HITL: Final = "hitl"
HALT_PLAN_CHECKPOINT: Final = "plan-checkpoint"
HALT_AWAITING_CLOSURE: Final = "awaiting-closure"
HALT_EPIC_CLOSURE: Final = "epic-closure"
"""Raised by the engine, not :func:`decide_pick`: an epic this run finished has
its closure recorded (a retrospective verdict) and waits for a person to close it
with bmad-ticket (``TicketsEngine._ticket_epic_boundary_check``)."""
HALT_EPIC_REJECTED: Final = "epic-rejected"
HALT_BLOCKED_PLAN: Final = "blocked-plan"
HALT_FOREIGN_ACTIVE: Final = "foreign-active"
HALT_STALLED: Final = "stalled"
HALT_TREE_FAULT: Final = "tree-fault"
HALT_PIN_MISMATCH: Final = "pin-mismatch"
HALT_DIRTY_INITIATIVE: Final = "dirty-initiative"
"""Raised by the engine, not :func:`decide_pick`: the checkout holds uncommitted
edits under the initiative folder that this run does not own, so the picked
ticket is not opened (``TicketsEngine._gate_dirty_initiative``)."""
HALT_IGNORED_PLAN: Final = "ignored-plan"
"""Raised by the engine, not :func:`decide_pick`: under worktree isolation the
picked ticket's plan lies in a tracked initiative folder (T1) but the code repo
ignores it, so its mark could not ride the unit's commit
(``TicketsEngine._gate_ignored_plan``)."""

HALT_STAGES: Final[Mapping[str, str]] = {
    HALT_REFINE: PAUSE_TICKET_REFINE,
    HALT_HITL: PAUSE_TICKET_HITL,
    HALT_PLAN_CHECKPOINT: PAUSE_PLAN_CHECKPOINT,
    HALT_AWAITING_CLOSURE: PAUSE_EPIC_BOUNDARY,
    HALT_EPIC_CLOSURE: PAUSE_EPIC_BOUNDARY,
    HALT_EPIC_REJECTED: PAUSE_EPIC_BOUNDARY,
    HALT_BLOCKED_PLAN: PAUSE_ESCALATION,
    HALT_FOREIGN_ACTIVE: PAUSE_TICKET_TREE,
    HALT_STALLED: PAUSE_TICKET_TREE,
    HALT_TREE_FAULT: PAUSE_TICKET_TREE,
    HALT_PIN_MISMATCH: PAUSE_TICKET_TREE,
    HALT_DIRTY_INITIATIVE: PAUSE_TICKET_TREE,
    HALT_IGNORED_PLAN: PAUSE_TICKET_TREE,
}
"""The pause stage each :class:`Halt` cause raises."""

SKIP_EMPTY: Final = "empty"
SKIP_PARKED: Final = "parked"

NOTE_SKIPPED: Final = "skipped"
NOTE_FOREIGN_ACTIVE: Final = "foreign-active"

# The closure verdict that holds every later epic until the epic is closed.
CLOSURE_REJECTED: Final = "rejected"
# An epic container status that no longer holds anything back.
EPIC_CLOSED: Final = frozenset({"done", "dropped"})
_TICKET_FINISHED: Final = frozenset({"done", "dropped"})

BOUNDARY_RETRO: Final = "retro"
BOUNDARY_REJECTED: Final = "rejected"
BOUNDARY_ACK: Final = "ack"
BOUNDARY_PAUSE: Final = "pause"


@dataclass(frozen=True)
class PickNote:
    """A row the pick passed over, for the engine to journal: ``kind`` is
    :data:`NOTE_SKIPPED` (``why`` is ``touched``, with the touching task's
    ``phase``, or ``no-ref``) or :data:`NOTE_FOREIGN_ACTIVE` (``why`` is the plan
    status)."""

    kind: str
    ref: str
    why: str
    phase: str = ""


@dataclass(frozen=True)
class Dispatch:
    """Dispatch ``row`` (its ``ref`` is never None)."""

    row: TicketRow
    notes: tuple[PickNote, ...] = ()

    @property
    def ref(self) -> str:
        assert self.row.ref is not None
        return self.row.ref


@dataclass(frozen=True)
class Skip:
    """Nothing to dispatch: the run finishes. ``reason`` is :data:`SKIP_EMPTY`,
    or :data:`SKIP_PARKED` when open tickets wait only behind ``held`` (refs this
    run retired without finishing); ``waiting`` lists those open tickets."""

    reason: str
    held: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    notes: tuple[PickNote, ...] = ()


@dataclass(frozen=True)
class Halt:
    """Pause the run: ``cause`` (a ``HALT_*`` token, staged by
    :data:`HALT_STAGES`) on ``key`` — a ticket ref, an epic slug for the epic
    causes, ``""`` for a tree fault — with the ``row`` it stopped at (None when no
    single row did) and an operator-facing ``detail``."""

    cause: str
    key: str
    row: TicketRow | None
    detail: str
    notes: tuple[PickNote, ...] = ()

    @property
    def stage(self) -> str:
        return HALT_STAGES[self.cause]


PickDecision = Dispatch | Skip | Halt


@dataclass(frozen=True)
class TicketRef:
    """A :class:`~bmad_loop.worksource.WorkRef` for a dispatched ticket."""

    key: str
    row: TicketRow
    epic: int = 0


def ticket_binding(found: FindResult) -> TicketBinding:
    """The :class:`~bmad_loop.model.TicketBinding` a unit persists for the ticket
    ``find`` resolved. ``epic_id`` is the epic part of an ``E.M`` ref (``""`` for
    a ticket whose epic the initiative does not list)."""
    row = found.row
    ref = row.ref or row_label(row)
    head, dot, tail = ref.rpartition(".")
    epic_id = head if dot and row.id is not None and tail == row.id else ""
    return TicketBinding(
        ref=ref,
        id=row.id or "",
        epic_slug=row.epic,
        epic_id=epic_id,
        title=row.title,
        plan=found.plan,
        risk=row.risk,
        refined=row.refined,
        plan_checkpoint=row.plan_checkpoint,
        done_checkpoint=row.done_checkpoint,
        hitl=row.hitl,
    )


@dataclass(frozen=True)
class EpicBoundary:
    """One epic-boundary action, in build order (:func:`epic_boundary`):

    * :data:`BOUNDARY_RETRO` — ``slug`` is finished, this run committed one of
      its tickets, and no closure is recorded: run the retrospective gate,
      record the closure, pause for the human's close;
    * :data:`BOUNDARY_REJECTED` — the recorded closure (``verdict``) is
      ``rejected`` and the epic is still open and finished: pause again;
    * :data:`BOUNDARY_ACK` — a non-rejected closure (``verdict``) whose one
      pause the operator resumed past: acknowledge it and go on;
    * :data:`BOUNDARY_PAUSE` — a non-rejected closure (``verdict``) no resume
      answered: the record landed but the run stopped before its pause did, so
      pause for it now."""

    action: str
    slug: str
    verdict: str = ""


def epic_finished(status_view: StatusView, slug: str) -> bool:
    """Whether epic ``slug`` has tickets and every one is ``built`` or reads
    ``done``/``dropped`` — the retrospective skill's own rule for an epic ready to
    retro (a ``built`` ticket awaits only the orchestrator's or a person's
    acceptance)."""
    rows = [t for t in status_view.tickets if t.epic == slug]
    return bool(rows) and all(t.status == "built" or t.state in _TICKET_FINISHED for t in rows)


def epic_boundary(
    status_view: StatusView,
    closures: Mapping[str, str],
    acked: Sequence[str] | frozenset[str],
    committed: frozenset[str],
    *,
    resumed: Sequence[str] | frozenset[str],
) -> list[EpicBoundary]:
    """The epic-boundary actions owed before the next pick, in build order, over
    the open epics only (a closed epic owes nothing: its closure is recorded in
    the tree). ``committed`` holds the epic slugs this run committed a ticket
    of; ``resumed`` the epics whose closure pause a resume answered
    (:func:`resume_epic_closure`). The caller stops at the first
    :data:`BOUNDARY_RETRO`, :data:`BOUNDARY_REJECTED` or :data:`BOUNDARY_PAUSE`;
    acknowledgements before it are carried out."""
    out: list[EpicBoundary] = []
    for epic in status_view.epics:
        if epic.status in EPIC_CLOSED:
            continue
        slug = epic.slug
        verdict = closures.get(slug)
        if verdict is None:
            if slug in committed and epic_finished(status_view, slug):
                out.append(EpicBoundary(BOUNDARY_RETRO, slug))
        elif verdict == CLOSURE_REJECTED:
            # Fix-up tickets added to the epic reopen it: they are built first
            # (`decide_pick` holds only the later epics), then it pauses again.
            if epic_finished(status_view, slug):
                out.append(EpicBoundary(BOUNDARY_REJECTED, slug, verdict))
        elif slug in acked:
            continue
        elif slug in resumed:
            out.append(EpicBoundary(BOUNDARY_ACK, slug, verdict))
        else:
            out.append(EpicBoundary(BOUNDARY_PAUSE, slug, verdict))
    return out


# ------------------------------------------------------------------- the pick


def row_label(row: TicketRow) -> str:
    """How an operator names ``row``: its ref, else its file, else ``epic/id``."""
    return row.ref or row.file or f"{row.epic}/{row.id}"


def needs_refine(row: TicketRow) -> bool:
    """Whether a build cannot start before ``row`` is refined."""
    return row.refine and not row.refined


def plan_planned(status: str) -> bool:
    """Whether a plan at ``status`` is planned (``ready-for-dev`` or later), so
    the next dispatch implements rather than plans."""
    return status in PLAN_STATUS_ORDER and PLAN_STATUS_ORDER.index(
        status
    ) >= PLAN_STATUS_ORDER.index("ready-for-dev")


def is_plan_halt_leg(row: TicketRow) -> bool:
    """Whether dispatching ``row`` is leg 1 of an unrefined ``plan_checkpoint``:
    ``ticket <ref> Halt after planning.``, then a pause for plan approval. A
    refined ticket's checkpoint is its story file's approval, before dispatch."""
    return row.plan_checkpoint and not row.refined and not plan_planned(row.status)


def approve_paused_story_file(state: RunState) -> str | None:
    """Record the operator's approval of a refined ticket's story file, which a
    resume of the pick's ``plan-checkpoint`` pause gives: the ticket's ref joins
    ``state.ticket_story_approvals``, so the next pick dispatches it. Called on
    the resume path before the pause is cleared.

    Only that pause qualifies: a tickets run paused at ``plan-checkpoint`` on a
    ref with no task. A registered unit's ``plan-checkpoint`` pause (an
    unrefined ticket's planned plan, or a plan review owed after
    implementation) is the two-leg flow's, resumed through its task. Returns
    the ref newly approved, else None."""
    ref = state.paused_story_key
    if (
        state.source != SOURCE_TICKETS
        or state.paused_stage != PAUSE_PLAN_CHECKPOINT
        or not ref
        or ref in state.tasks
        or ref in state.ticket_story_approvals
    ):
        return None
    state.ticket_story_approvals.append(ref)
    return ref


def resume_epic_closure(state: RunState) -> str | None:
    """Record that this resume answers a tickets run's epic closure pause: a run
    paused at ``epic-boundary`` on an epic with a recorded non-rejected closure.
    The slug joins ``state.ticket_epic_closure_resumed``, so the next boundary
    check acknowledges it instead of pausing for it again. Called on the resume
    path before the pause is cleared. A closure the run recorded but never paused
    for (the run was killed between the two) has no such pause to resume, so it
    pauses on the next pass. Returns the slug newly recorded, else None."""
    slug = state.paused_story_key
    if (
        state.source != SOURCE_TICKETS
        or state.paused_stage != PAUSE_EPIC_BOUNDARY
        or not slug
        or state.ticket_epic_closures.get(slug, CLOSURE_REJECTED) == CLOSURE_REJECTED
        or slug in state.ticket_epic_closure_resumed
    ):
        return None
    state.ticket_epic_closure_resumed.append(slug)
    return slug


THOROUGH_ALWAYS: Final = "always"
THOROUGH_HIGH_RISK: Final = "high-risk"
RISK_HIGH: Final = "high"


def wants_thorough(thorough_review: str, risk: str) -> bool:
    """Whether a session gets the thorough review under ``[tickets]
    thorough_review``: every ticket under ``always``, a ``high`` risk one under
    ``high-risk``, none under ``never``. ``risk`` is the find row's for a dev
    leg and the plan's for a review leg."""
    if thorough_review == THOROUGH_ALWAYS:
        return True
    return thorough_review == THOROUGH_HIGH_RISK and risk.strip().lower() == RISK_HIGH


def dispatch_invocation(skill: str, ref: str, *, thorough: bool, plan_halt: bool) -> str:
    """The invocation line a ticket's dev leg dispatches, shared with the
    dry-run so the two cannot drift: ``/<skill> ticket <ref>``, then ``thorough``
    (build-auto's review selection) and ``Halt after planning.`` on leg 1 of an
    unrefined ``plan_checkpoint``."""
    line = f"/{skill} ticket {ref}"
    if thorough:
        line += " thorough"
    if plan_halt:
        line += " Halt after planning."
    return line


def plan_blocked(row: TicketRow) -> bool:
    """Whether ``row``'s plan is blocked (status or a ``blocked_at`` stamp)."""
    return row.status == PLAN_BLOCKED or bool(row.blocked_at)


def build_order(status_view: StatusView) -> dict[str, int]:
    """Each ticket's position in initiative build order, by label: ``status``
    lists the epics in the initiative table's order and each epic's tickets in
    entry order."""
    return {row_label(row): i for i, row in enumerate(status_view.tickets)}


def _ordered(rows: Sequence[TicketRow], rank: Mapping[str, int]) -> list[TicketRow]:
    last = len(rank)
    keyed = sorted(enumerate(rows), key=lambda p: (rank.get(row_label(p[1]), last), p[0]))
    return [row for _, row in keyed]


def _awaiting_closure(status_view: StatusView) -> list[str]:
    """Open epics with tickets, every one done or dropped, in build order: the
    human closes them (bmad-ticket), never bmad-loop."""
    out: list[str] = []
    for epic in status_view.epics:
        if epic.status in EPIC_CLOSED:
            continue
        states = [t.state for t in status_view.tickets if t.epic == epic.slug]
        if states and all(s in _TICKET_FINISHED for s in states):
            out.append(epic.slug)
    return out


def _unbroken_epics(status_view: StatusView) -> list[str]:
    """Open epics with no tickets at all, in build order."""
    with_tickets = {t.epic for t in status_view.tickets}
    return [
        e.slug
        for e in status_view.epics
        if e.status not in EPIC_CLOSED and e.slug not in with_tickets
    ]


def _behind_held(
    rows: Sequence[TicketRow], every: Sequence[TicketRow], held: Sequence[str]
) -> set[str]:
    """The labels of ``rows`` whose every wait is ``held`` work: a held ticket, a
    row already behind one, or the epic of either (a ``gated_by`` slug). A
    sibling's wait is its bare id, resolved within the row's epic through
    ``every``; another epic's is its ``E.M`` ref."""
    by_id = {(r.epic, r.id): row_label(r) for r in every if r.id is not None}
    epic_of = {row_label(r): r.epic for r in every}
    behind: set[str] = set(held)
    grew = True
    while grew:
        grew = False
        epics = {epic_of[key] for key in behind if key in epic_of}
        for row in rows:
            label = row_label(row)
            if label in behind or not row.waiting_on:
                continue
            if all(
                by_id.get((row.epic, wait), wait) in behind or wait in epics
                for wait in row.waiting_on
            ):
                behind.add(label)
                grew = True
    return behind


def _rejected_gate(
    row: TicketRow, status_view: StatusView, epic_closures: Mapping[str, str]
) -> str | None:
    """The first open epic ahead of ``row``'s epic whose recorded closure is
    ``rejected``, or None. A ticket inside that epic (a fix-up) is not held; a
    ticket outside every listed epic (the initiative's own entries) has every
    epic ahead of it."""
    for epic in status_view.epics:
        if epic.slug == row.epic:
            return None
        if epic_closures.get(epic.slug) == CLOSURE_REJECTED and epic.status not in EPIC_CLOSED:
            return epic.slug
    return None


@dataclass(frozen=True)
class PickScope:
    """The run's selector: ``epic`` an epic slug (``epic-<x>``), ``ref`` one
    ticket ref; ``""`` selects everything."""

    epic: str = ""
    ref: str = ""

    def admits(self, row: TicketRow) -> bool:
        if self.epic and row.epic != self.epic:
            return False
        return not self.ref or row_label(row) == self.ref

    def admits_epic(self, slug: str) -> bool:
        """Whether an epic-level cause on ``slug`` belongs to this scope."""
        return not self.ref and (not self.epic or slug == self.epic)


EVERYTHING: Final = PickScope()


def decide_pick(
    next_view: NextView,
    status_view: StatusView,
    task_phases: Mapping[str, Phase],
    epic_closures: Mapping[str, str],
    approvals: Sequence[str] | frozenset[str],
    scope: PickScope = EVERYTHING,
    *,
    acked: Sequence[str] | frozenset[str] = (),
) -> PickDecision:
    """The next pick, per the module docstring. ``acked`` holds the epics whose
    one closure pause the operator resumed past (``ticket_epic_closure_acked``)."""
    touched = {key: phase for key, phase in task_phases.items() if phase != Phase.ESCALATED}
    rank = build_order(status_view)
    notes: list[PickNote] = []

    def scoped(*groups: Sequence[TicketRow]) -> list[TicketRow]:
        return _ordered([row for group in groups for row in group if scope.admits(row)], rank)

    foreign = [row for row in scoped(next_view.in_progress) if row_label(row) not in touched]
    notes.extend(PickNote(NOTE_FOREIGN_ACTIVE, row_label(row), row.status) for row in foreign)

    candidates = scoped(next_view.ready_to_refine, next_view.ready_to_start)
    for row in candidates:
        if row.ref is None:
            notes.append(PickNote(NOTE_SKIPPED, row_label(row), "no-ref"))
            continue
        phase = touched.get(row.ref)
        if phase is not None:
            notes.append(PickNote(NOTE_SKIPPED, row.ref, "touched", str(phase)))
            continue
        held = tuple(notes)
        gate = _rejected_gate(row, status_view, epic_closures)
        if gate is not None:
            return Halt(
                HALT_EPIC_REJECTED,
                gate,
                row,
                f"{gate}'s retrospective verdict is rejected; close {gate} before "
                f"{row.ref} can start",
                held,
            )
        if needs_refine(row):
            return Halt(HALT_REFINE, row.ref, row, f"{row.ref} needs refining", held)
        if row.hitl:
            return Halt(HALT_HITL, row.ref, row, f"{row.ref} is a hitl ticket", held)
        if row.refined and row.plan_checkpoint and row.ref not in approvals:
            return Halt(
                HALT_PLAN_CHECKPOINT,
                row.ref,
                row,
                f"{row.ref} has a plan checkpoint: approve its story file",
                held,
            )
        return Dispatch(row, held)

    held_notes = tuple(notes)
    blocked = scoped(next_view.blocked)
    for row in blocked:
        if plan_blocked(row) and row_label(row) not in touched:
            reason = row.blocked_reason or "no reason recorded"
            return Halt(
                HALT_BLOCKED_PLAN,
                row_label(row),
                row,
                f"{row_label(row)}'s plan is blocked: {reason}",
                held_notes,
            )
    for row in blocked:
        if not plan_blocked(row) and not row.waiting_on and row.unknown:
            return Halt(
                HALT_REFINE,
                row_label(row),
                row,
                f"{row_label(row)} has an open unknown: {row.unknown}",
                held_notes,
            )
    awaiting = [slug for slug in _awaiting_closure(status_view) if scope.admits_epic(slug)]
    for row in blocked:
        for wait in row.waiting_on:
            if wait in awaiting:
                return Halt(
                    HALT_AWAITING_CLOSURE,
                    wait,
                    row,
                    f"every ticket of {wait} is done; close {wait} so {row_label(row)} can start",
                    held_notes,
                )
    if foreign:
        named = ", ".join(f"{row_label(r)} ({r.status})" for r in foreign)
        return Halt(
            HALT_FOREIGN_ACTIVE,
            row_label(foreign[0]),
            foreign[0],
            f"only tickets this run did not start stand in the way: {named}",
            held_notes,
        )
    if blocked:
        waiting_rows = [r for r in blocked if row_label(r) not in touched]
        waiting = tuple(row_label(r) for r in waiting_rows)
        # This run's unfinished tickets — or, when every blocked row is one this
        # run touched, those rows themselves.
        own_held = [key for key, phase in touched.items() if phase != Phase.DONE] or [
            row_label(r) for r in blocked if not waiting_rows
        ]
        if own_held:
            every = (
                *status_view.tickets,
                *next_view.ready_to_refine,
                *next_view.ready_to_start,
                *next_view.in_progress,
                *blocked,
            )
            behind = _behind_held(waiting_rows, every, own_held)
            stalled = [r for r in waiting_rows if row_label(r) not in behind]
            if not stalled:
                last = len(rank)
                held = tuple(sorted(own_held, key=lambda k: (rank.get(k, last), k)))
                return Skip(SKIP_PARKED, held, waiting, held_notes)
            # a row also waiting on something no held ticket explains stalls the
            # tree whatever this run finishes
            waiting_rows = stalled
            waiting = tuple(row_label(r) for r in stalled)
        return Halt(
            HALT_STALLED,
            waiting[0],
            waiting_rows[0],
            f"{len(waiting)} ticket(s) wait on nothing this run can start: {', '.join(waiting)}",
            held_notes,
        )
    # An acknowledged closure paused once already; only a ticket that waits on
    # the epic (above) holds the run behind it again.
    awaiting = [slug for slug in awaiting if slug not in acked]
    if awaiting:
        return Halt(
            HALT_AWAITING_CLOSURE,
            awaiting[0],
            None,
            f"every ticket of {awaiting[0]} is done; close {awaiting[0]}",
            held_notes,
        )
    unbroken = [slug for slug in _unbroken_epics(status_view) if scope.admits_epic(slug)]
    if unbroken:
        return Halt(
            HALT_STALLED,
            unbroken[0],
            None,
            f"{unbroken[0]} is open but has no tickets: break it down or close it",
            held_notes,
        )
    return Skip(SKIP_EMPTY, notes=held_notes)


# ----------------------------------------------------------------- the source


class TicketsWorkSource:
    """The ``tickets`` source: units are the active initiative's tickets, keyed
    by ref. Every getter is live, so construction does no I/O and a pick always
    reads the run's current approvals, closures and closure
    acknowledgements."""

    name = SOURCE_TICKETS
    has_epics = False

    def __init__(
        self,
        tree_get: Callable[[], TicketTreeLike],
        approvals_get: Callable[[], Sequence[str]],
        closures_get: Callable[[], Mapping[str, str]],
        scope_get: Callable[[], PickScope] = lambda: EVERYTHING,
        *,
        acked_get: Callable[[], Sequence[str]] = lambda: (),
    ):
        self._tree_get = tree_get
        self._approvals_get = approvals_get
        self._closures_get = closures_get
        self._scope_get = scope_get
        self._acked_get = acked_get

    @property
    def tree(self) -> TicketTreeLike:
        return self._tree_get()

    def read(self) -> tuple[NextView, StatusView]:
        """``next`` and ``status`` of the active initiative; raises
        :class:`~bmad_loop.tickettree.TicketsError`."""
        tree = self._tree_get()
        return tree.next(), tree.status()

    def decide(
        self,
        phases: Mapping[str, Phase],
        gate: Callable[[StatusView], None] | None = None,
    ) -> PickDecision:
        """:func:`decide_pick` over a fresh read. A tree that cannot be read halts
        ``pin-mismatch`` or ``tree-fault``, the error's ``kind`` in the detail.
        ``gate`` sees the fresh ``status`` view before the pick (the engine's
        epic boundary, which may pause), so one read serves both."""
        try:
            next_view, status_view = self.read()
        except TicketsError as e:
            cause = HALT_PIN_MISMATCH if e.kind == "pin-mismatch" else HALT_TREE_FAULT
            return Halt(cause, "", None, f"tickets.py {e.kind}: {e}")
        if gate is not None:
            gate(status_view)
        return decide_pick(
            next_view,
            status_view,
            phases,
            self._closures_get(),
            frozenset(self._approvals_get()),
            self._scope_get(),
            acked=frozenset(self._acked_get()),
        )

    def pick(self, phases: Mapping[str, Phase], current_epic: int | None) -> Pick:
        """The protocol view of :meth:`decide`: a dispatch, a halt carrying the
        :class:`Halt` as its payload, or exhausted. ``current_epic`` is unused."""
        decision = self.decide(phases)
        if isinstance(decision, Dispatch):
            return Pick(ref=TicketRef(key=decision.ref, row=decision.row))
        if isinstance(decision, Halt):
            return Pick(halt=WorkHalt(key=decision.key, cause=decision.cause, payload=decision))
        return Pick()

    def remaining_estimate(self, phases: Mapping[str, Phase]) -> int:
        """Open tickets this run has not touched and that are not already in
        review. A read error raises; the engine reads that as no estimate."""
        status_view = self._tree_get().status()
        touched = {key for key, phase in phases.items() if phase != Phase.ESCALATED}
        return sum(
            1
            for row in status_view.tickets
            if row.state not in (*_TICKET_FINISHED, "review") and row_label(row) not in touched
        )

    # ----------------------------------------------- no epics: inert answers

    def epic_progress(self, epic: int) -> EpicProgress:
        """Never complete: the epic lifecycle is keyed by slug in the engine."""
        return EpicProgress(complete=False, retro_done=False)

    def retro_done(self, epic: int) -> bool:
        return False

    def retro_artifact_errors(self, epic: int) -> tuple[list[str], list[Path]]:
        return ["tickets mode has no numbered epic retrospective"], []

    def retro_docs(self, epic: int) -> list[Path]:
        return []

    def retro_commit_paths(self, docs: Sequence[Path]) -> set[Path]:
        return set(docs)

    def retro_target(self, epic: int) -> str:
        return str(epic)

    def epic_label(self, epic: int) -> str:
        return f"epic-{epic}"


if TYPE_CHECKING:
    _conforms: type[WorkSource] = TicketsWorkSource
