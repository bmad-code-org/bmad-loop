"""Engine variant for "tickets mode" — dispatch from a BMAD v7 initiative ticket
tree (``[stories] source = "tickets"``).

``TicketsEngine`` picks from the active initiative through the project's
``tickets.py`` (:class:`~bmad_loop.tickets_source.TicketsWorkSource`, whose pure
:func:`~bmad_loop.tickets_source.decide_pick` holds the selection rules). The tree
is reached only through a :class:`~bmad_loop.tickettree.TicketTreeLike`, which
``ticket_tree_factory`` builds from ``(project_root, pinned_initiative)`` — the
real :class:`~bmad_loop.tickettree.TicketTree` by default, an in-memory fake in
tests. The tree is pinned to ``RunState.initiative``, so a resume never builds
another initiative's ticket.

The unit key is the ticket ref (``E.M``); every unit's epic is 0 and the epic
lifecycle is keyed by slug here, not by the base engine's numbered epics.

Pick-time pauses (``_halt_pick``), all raised before a ticket is recorded in
``state.tasks`` unless noted, so a resume re-picks it and re-reads the tree:

* a hitl ticket → ``ticket-hitl``; a ticket that needs refining → ``ticket-refine``;
* a refined ``plan_checkpoint`` ticket whose story file no operator approved this
  run → ``plan-checkpoint``, its reason naming the story file;
* an epic awaiting closure, or held behind a rejected epic → ``epic-boundary``;
* a pin mismatch, a ``tickets.py`` fault, or only foreign / unexplained rows in
  the way → ``ticket-tree``;
* a blocked plan → an ESCALATED task at attempt 0 and ``escalation``
  (:meth:`~bmad_loop.checkpoints.CheckpointFlowMixin._pause_wedged`), the shape
  ``bmad-loop resolve`` acts on. Like a stories wedge it is re-derived from the
  tree every pick; an in-run escalation re-pauses before the pick instead.

Rows the pick passed over are journaled once per process (``ticket-skipped``,
``ticket-foreign-active``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NoReturn

from . import gates
from .checkpoints import CheckpointFlags, CheckpointFlowMixin
from .engine import Engine, RunPaused
from .frontmatter import parse_frontmatter, status_of
from .model import StoryTask, TicketBinding
from .tickets_source import (
    HALT_BLOCKED_PLAN,
    HALT_HITL,
    HALT_PLAN_CHECKPOINT,
    HALT_REFINE,
    NOTE_FOREIGN_ACTIVE,
    SKIP_PARKED,
    Dispatch,
    Halt,
    PickNote,
    Skip,
    TicketRef,
    TicketsWorkSource,
    plan_planned,
    ticket_binding,
)
from .tickettree import TicketsError, TicketTree, TicketTreeLike
from .worksource import SOURCE_TICKETS
from .worksource import Halt as WorkHalt
from .worksource import WorkRef, WorkSource

TicketTreeFactory = Callable[[Path, str | None], TicketTreeLike]
"""Builds the run's tree from ``(project_root, pinned_initiative)``."""


def default_ticket_tree(project_root: Path, pinned_initiative: str | None) -> TicketTreeLike:
    """The project's installed ``tickets.py``, pinned to ``pinned_initiative``."""
    return TicketTree(project_root, pinned_initiative=pinned_initiative)


class TicketsEngine(CheckpointFlowMixin, Engine):
    def __init__(
        self, *args: Any, ticket_tree_factory: TicketTreeFactory | None = None, **kwargs: Any
    ):
        # `--epic`/`--story` select by epic slug and ticket ref here, not by the
        # sprint E-S grammar, so keep them out of the base's selector parse. Set
        # before super().__init__, which builds the source.
        kwargs["story_filter"] = None
        kwargs["epic_filter"] = None
        self._ticket_tree_factory = ticket_tree_factory or default_ticket_tree
        self._ticket_tree: TicketTreeLike | None = None
        # (kind, ref, why) already journaled — a pass-over is journaled once per
        # process, not on every pick that passes it again.
        self._noted: set[tuple[str, str, str]] = set()
        # ref -> binding resolved at pick time, attached when the task registers.
        self._picked: dict[str, TicketBinding] = {}
        super().__init__(*args, **kwargs)
        # pin the mode so resume/resolve rebuild a TicketsEngine without policy.
        self.state.source = SOURCE_TICKETS

    # ------------------------------------------------------------------ tree

    @property
    def ticket_tree(self) -> TicketTreeLike:
        """The run's tree, built once from the main checkout and the pin."""
        if self._ticket_tree is None:
            self._ticket_tree = self._ticket_tree_factory(
                self.paths.project, self.state.initiative or None
            )
        return self._ticket_tree

    def _make_work_source(self) -> WorkSource:
        """The ticket-tree source. Every getter is live: the tree is built on the
        first pick, and approvals and closures are read from run state each time."""
        return TicketsWorkSource(
            lambda: self.ticket_tree,
            lambda: self.state.ticket_story_approvals,
            lambda: self.state.ticket_epic_closures,
        )

    @property
    def _tickets_source(self) -> TicketsWorkSource:
        source = self.source
        assert isinstance(source, TicketsWorkSource)
        return source

    # --------------------------------------------------------------- picking

    def _pick_next(self) -> WorkRef | None:
        self._repause_inrun_escalation()  # raises when one is live
        decision = self._tickets_source.decide(self._phases())
        self._journal_pick_notes(decision.notes)
        if isinstance(decision, Halt):
            self._halt_pick(WorkHalt(key=decision.key, cause=decision.cause, payload=decision))
        if isinstance(decision, Skip):
            if decision.reason == SKIP_PARKED:
                self._finish_parked(decision)
            return None
        return self._dispatch_ref(decision)

    def _dispatch_ref(self, decision: Dispatch) -> WorkRef:
        """Resolve the picked ticket's binding before anything registers it, so a
        ``find`` fault pauses with no task recorded and a resume re-picks."""
        try:
            found = self.ticket_tree.find(decision.ref)
        except TicketsError as e:
            self._pause_ticket_pick(
                Halt("tree-fault", decision.ref, decision.row, f"tickets.py {e.kind}: {e}")
            )
        self._picked[decision.ref] = ticket_binding(found)
        return TicketRef(key=decision.ref, row=decision.row)

    def _run_story(self, task: StoryTask) -> None:
        # Attach the binding resolved at pick time the moment `_loop` registers
        # the task; a resumed task already carries its own.
        if task.ticket is None and task.story_key in self._picked:
            task.ticket = self._picked.pop(task.story_key)
            self._save()
        super()._run_story(task)

    def _journal_pick_notes(self, notes: Iterable[PickNote]) -> None:
        for note in notes:
            seen = (note.kind, note.ref, f"{note.why}:{note.phase}")
            if seen in self._noted:
                continue
            self._noted.add(seen)
            if note.kind == NOTE_FOREIGN_ACTIVE:
                self.journal.append("ticket-foreign-active", ref=note.ref, status=note.why)
            else:
                self.journal.append("ticket-skipped", ref=note.ref, skip=note.why, phase=note.phase)

    def _finish_parked(self, decision: Skip) -> None:
        """Open tickets wait only behind tickets this run retired without
        finishing: the run ends, saying which, instead of finishing silently."""
        for ref in decision.waiting:
            seen = ("behind-parked", ref, "")
            if seen not in self._noted:
                self._noted.add(seen)
                self.journal.append("ticket-skipped", ref=ref, skip="behind-parked", phase="")
        gates.notify(
            self.policy,
            self.run_dir,
            "tickets run ended behind parked tickets",
            f"{', '.join(decision.waiting)} wait behind {', '.join(decision.held)}, which "
            f"this run did not finish — resolve those, then start a new run",
        )

    # ---------------------------------------------------------------- pauses

    def _halt_pick(self, halt: WorkHalt) -> NoReturn:
        decision = halt.payload
        if not isinstance(decision, Halt):
            super()._halt_pick(halt)
        if decision.cause == HALT_BLOCKED_PLAN:
            self._pause_blocked_plan(decision)
        if decision.cause == HALT_HITL:
            self._pause_ticket_hitl(decision)
        if decision.cause == HALT_REFINE:
            self._pause_ticket_refine(decision)
        self._pause_ticket_pick(decision)

    def _resume_hint(self) -> str:
        return f"`bmad-loop resume {self.state.run_id}`"

    def _pause_ticket_hitl(self, halt: Halt) -> NoReturn:
        """The next ticket is a human-in-the-loop one: a person builds it (or pairs
        on it). Not registered, so a resume re-reads the tree and moves on once
        the ticket is in review or done."""
        self.journal.append("ticket-hitl-pause", ref=halt.key)
        reason = (
            f"{halt.key} is a hitl ticket — build it with a person in the loop, then "
            f"{self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, f"hitl ticket: {halt.key}", reason)
        raise RunPaused(reason, halt.stage, halt.key)

    def _pause_ticket_refine(self, halt: Halt) -> NoReturn:
        """The next ticket needs refining (bmad-ticket) before a build can start."""
        self.journal.append("ticket-refine-pause", ref=halt.key)
        reason = f"{halt.detail} — refine it with bmad-ticket, then {self._resume_hint()}"
        gates.notify(self.policy, self.run_dir, f"ticket needs refining: {halt.key}", reason)
        raise RunPaused(reason, halt.stage, halt.key)

    def _pause_ticket_pick(self, halt: Halt) -> NoReturn:
        """Every other pick halt: a refined plan checkpoint (named story file), an
        epic boundary, or the tree itself. ``key`` "" (a tree fault) pauses with
        no story key."""
        if halt.cause == HALT_PLAN_CHECKPOINT:
            reason = (
                f"approve story file {self._story_file(halt.key)} — {halt.key} has a "
                f"plan checkpoint; then {self._resume_hint()}"
            )
        else:
            reason = f"{halt.detail} — then {self._resume_hint()}"
        self.journal.append(
            "ticket-pick-halt",
            ref=halt.key or None,
            cause=halt.cause,
            stage=halt.stage,
            detail=halt.detail,
        )
        gates.notify(self.policy, self.run_dir, f"tickets run paused: {halt.cause}", reason)
        raise RunPaused(reason, halt.stage, halt.key or None)

    def _story_file(self, ref: str) -> str:
        """The ticket's pulled story file as ``find`` names it, else its ref."""
        try:
            found = self.ticket_tree.find(ref)
        except TicketsError:
            return ref
        return found.story_file or ref

    def _pause_blocked_plan(self, halt: Halt) -> NoReturn:
        """A blocked plan this run did not park: record the ESCALATED wedge task
        resolve acts on (carrying the plan and the ticket binding), then pause."""
        binding: TicketBinding | None = None
        try:
            binding = ticket_binding(self.ticket_tree.find(halt.key))
        except TicketsError:
            binding = None
        try:
            self._pause_wedged(
                halt.key,
                state_kind="blocked-plan",
                detail=halt.detail,
                spec_file=binding.plan if binding is not None else None,
            )
        except RunPaused:
            task = self.state.tasks.get(halt.key)
            if task is not None and binding is not None:
                task.ticket = binding
                self._save()
            raise
        raise AssertionError("_pause_wedged always raises")  # pragma: no cover

    # ------------------------------------------------------ checkpoint seam

    def _entry_checkpoint_flags(self, task: StoryTask) -> CheckpointFlags | None:
        """The flags the ticket carried when it was picked; None for an unbound
        task."""
        ticket = task.ticket
        if ticket is None:
            return None
        return CheckpointFlags(
            plan_checkpoint=ticket.plan_checkpoint,
            done_checkpoint=ticket.done_checkpoint,
            refined=ticket.refined,
        )

    def _plan_produced(self, task: StoryTask) -> bool:
        """Whether the bound plan is on disk at ``ready-for-dev`` or later. A
        missing or unreadable plan is not produced."""
        ticket = task.ticket
        if ticket is None:
            return False
        try:
            text = Path(ticket.plan).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return False
        return plan_planned(status_of(parse_frontmatter(text)))

    def _schedule_complete(self) -> bool:
        """Whether the next pick would end the run. A tree that cannot be read
        answers False, so a done_checkpoint still fires."""
        return isinstance(self._tickets_source.decide(self._phases()), Skip)
