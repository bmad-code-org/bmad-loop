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

A picked ticket is dispatched as ``ticket <ref>`` on every leg
(:meth:`TicketsEngine._dev_prompt`, :meth:`TicketsEngine._review_prompt`), with
the dry-run's own invocation line (``thorough`` per ``[tickets]
thorough_review``, ``Halt after planning.`` on leg 1 of an unrefined plan
checkpoint). The read-back is pinned to the plan ``find`` named from attempt 1,
ends at ``built`` (dev) or ``built``/``done`` (review), and probes the
initiative folder for a no-plan fallback result. The dev and review gates are
``verify.verify_dev_tickets`` / ``verify.verify_review_tickets``: no sprint board,
and the orchestrator alone owns ``done``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from . import gates, verify
from .adapters.base import SpecSnapshot
from .checkpoints import CheckpointFlags, CheckpointFlowMixin
from .engine import Engine, RunPaused, SpecStatusWriteRefused, SpecWriteReason
from .frontmatter import frontmatter_fault, parse_frontmatter, status_of
from .model import (
    PAUSE_TICKET_TREE,
    Phase,
    StoryTask,
    TicketBinding,
    VerifyOutcome,
    result_mapping,
)
from .runs import rebase_recorded_project_path
from .statemachine import PlanWriteReason, advance
from .tickets_source import (
    HALT_BLOCKED_PLAN,
    HALT_HITL,
    HALT_PLAN_CHECKPOINT,
    HALT_REFINE,
    NOTE_FOREIGN_ACTIVE,
    RISK_HIGH,
    SKIP_PARKED,
    Dispatch,
    Halt,
    PickNote,
    Skip,
    TicketRef,
    TicketsWorkSource,
    dispatch_invocation,
    plan_planned,
    ticket_binding,
    wants_thorough,
)
from .tickettree import TicketsError, TicketTree, TicketTreeLike
from .worksource import SOURCE_TICKETS
from .worksource import Halt as WorkHalt
from .worksource import WorkRef, WorkSource

TicketTreeFactory = Callable[[Path, str | None], TicketTreeLike]
"""Builds the run's tree from ``(project_root, pinned_initiative)``."""

PLAN_WRITE_REASONS: Mapping[tuple[SpecWriteReason, str], PlanWriteReason] = {
    # a kept branch adopted as finished: the orchestrator's acceptance (built → done)
    ("adoption", verify.PLAN_DONE): "accept",
    # a repair leg re-opens the plan in place
    ("repair", "in-progress"): "repair",
    # a timed-out review's verified work lands at `built` ({in-progress, in-review}
    # → built); salvage never targets `done`, which stays the acceptance's
    ("salvage", verify.PLAN_BUILT): "salvage",
}
"""The engine status writes tickets mode makes, as ``(engine reason, target) →
plan-transition reason``; every other pair is refused
(:meth:`TicketsEngine._spec_status_write`)."""


@dataclass(frozen=True)
class _PlanUndo:
    """A plan's bytes before the commit window marked it (None: no file)."""

    plan: Path
    data: bytes | None


def _bytes_or_none(path: Path) -> bytes | None:
    """``path``'s bytes; None when it is absent or unreadable."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths name the same file once resolved; False when either
    cannot be resolved."""
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return False


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
        # story key -> plan status its last review launched on (`_verify_review`).
        self._review_entered: dict[str, str] = {}
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
        if task.ticket is None:
            task.ticket = self._picked.pop(task.story_key, None) or self._rebind(task)
            self._save()
        super()._run_story(task)

    def _rebind(self, task: StoryTask) -> TicketBinding:
        """Re-resolve an unbound task's binding from the tree. A host that died
        between `_loop` saving the new task and the attach above leaves it
        registered with no binding and no session; this re-reads what a fresh pick
        would. A ``find`` fault pauses at ``ticket-tree``, and the resume retries."""
        try:
            found = self.ticket_tree.find(task.story_key)
        except TicketsError as e:
            self._pause_ticket_pick(
                Halt("tree-fault", task.story_key, None, f"tickets.py {e.kind}: {e}")
            )
        self.journal.append("ticket-rebound", story_key=task.story_key)
        return ticket_binding(found)

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
        no story key. A ``find`` fault naming the plan checkpoint's story file
        pauses at ``ticket-tree`` with the fault instead."""
        if halt.cause == HALT_PLAN_CHECKPOINT:
            try:
                found = self.ticket_tree.find(halt.key)
            except TicketsError as e:
                self._pause_ticket_pick(
                    Halt("tree-fault", halt.key, halt.row, f"tickets.py {e.kind}: {e}")
                )
            reason = (
                f"approve story file {found.story_file or halt.key} — {halt.key} has a "
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

    def _pause_blocked_plan(self, halt: Halt) -> NoReturn:
        """A blocked plan this run did not park: record the ESCALATED wedge task
        resolve acts on (carrying the plan and the ticket binding), then pause. A
        ``find`` fault pauses at ``ticket-tree`` with no task recorded, as
        :meth:`_dispatch_ref` does: an unbound wedge task would re-drive unbound."""
        try:
            binding = ticket_binding(self.ticket_tree.find(halt.key))
        except TicketsError as e:
            self._pause_ticket_pick(
                Halt("tree-fault", halt.key, halt.row, f"tickets.py {e.kind}: {e}")
            )
        try:
            self._pause_wedged(
                halt.key,
                state_kind="blocked-plan",
                detail=halt.detail,
                spec_file=binding.plan,
            )
        except RunPaused:
            task = self.state.tasks.get(halt.key)
            if task is not None:
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
        """Whether the bound plan is on disk at ``ready-for-dev`` or later, read in
        the active workspace (:meth:`_bound_plan`): under isolation leg 1 wrote it
        into the unit worktree only. A missing plan is not produced. A plan that is
        there but cannot be read stops the dispatch (:meth:`_plan_unreadable`):
        answering False would send another ``Halt after planning.`` leg onto
        planning work whose status nobody could read. An unrefined
        ``plan_checkpoint`` plan whose frontmatter does not parse stops it the
        same way; any other unit dispatches no halt leg either way, so its build
        meets the malformed plan instead."""
        plan = self._bound_plan(task)
        if plan is None:
            return False
        try:
            text = plan.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError) as e:
            self._plan_unreadable(task, plan, e)
        ticket = task.ticket
        if ticket is not None and ticket.plan_checkpoint and not ticket.refined:
            fault = frontmatter_fault(text)
            if fault is not None:
                self._plan_unreadable(task, plan, fault)
        return plan_planned(status_of(parse_frontmatter(text)))

    def _plan_unreadable(self, task: StoryTask, plan: Path, error: Exception | str) -> NoReturn:
        """The bound plan is on disk but cannot be read, before any session for
        this dispatch launched. A PENDING task has run nothing: pause at
        ``ticket-tree`` with the fault, as a ``find`` fault does, and the resume
        re-reads the plan. A DEV_RUNNING task escalates the way a failed restore
        patch does, stepping to DEV_VERIFY first: ``_escalate`` cannot leave
        DEV_RUNNING directly."""
        detail = f"ticket plan {plan} cannot be read: {error}"
        if task.phase == Phase.PENDING:
            self._pause_ticket_pick(Halt("tree-fault", task.story_key, None, detail))
        if task.phase == Phase.DEV_RUNNING:
            advance(task, Phase.DEV_VERIFY)
        self._escalate(task, detail)
        raise AssertionError("_escalate always raises")  # pragma: no cover

    def _schedule_complete(self) -> bool:
        """Whether the next pick would end the run. A tree that cannot be read
        answers False, so a done_checkpoint still fires."""
        return isinstance(self._tickets_source.decide(self._phases()), Skip)

    # -------------------------------------------------------------- dispatch

    def _recorded_plan(self, ticket: TicketBinding) -> Path:
        """The binding's plan path, moved with a renamed project: ``find`` named
        it absolute under the launch-time project ``state.project`` keeps, so a
        resume after a project move reads it under the live project
        (:func:`~bmad_loop.runs.rebase_recorded_project_path`). A plan outside
        the recorded project is left as recorded."""
        return rebase_recorded_project_path(Path(ticket.plan), self.state, self.paths.project)

    def _bound_plan(self, task: StoryTask) -> Path | None:
        """The ticket's plan as ``find`` named it at pick time (moved with the
        project, :meth:`_recorded_plan`), re-anchored in the active workspace
        (the same path in place; the unit worktree's copy under isolation).
        A plan outside the project is left as recorded: under isolation the
        preflight refuses an initiative folder outside the project
        (``tickets.layout-unsupported``), since the mount re-anchors only what
        the project holds. None for a task with no binding."""
        ticket = task.ticket
        if ticket is None:
            return None
        plan = self._recorded_plan(ticket)
        try:
            rel = plan.relative_to(self.paths.project)
        except ValueError:
            return plan
        return self.workspace.paths.project / rel

    def _finish_inflight(self) -> None:
        """Move each in-place unit's recorded plan paths with a renamed project
        before recovery reads them, as :meth:`_recorded_plan` moves the binding.
        An in-place ``spec_file`` / ``dispatched_spec_file`` is persisted absolute
        under the launch-time project, so a resume at ``REVIEW_VERIFY`` after a
        move would gate the review on a plan that is gone and retry forever. An
        isolated unit's paths are persisted relative to its mount and re-anchored
        by the base; a path outside the recorded project is left as recorded."""
        for task in self.state.tasks.values():
            if task.terminal or task.worktree_path:
                continue
            for name in ("spec_file", "dispatched_spec_file"):
                recorded = getattr(task, name)
                if recorded:
                    live = rebase_recorded_project_path(
                        Path(recorded), self.state, self.paths.project
                    )
                    setattr(task, name, str(live))
        super()._finish_inflight()

    def _plan_halt_now(self, task: StoryTask) -> bool:
        """Leg 1 of an unrefined ``plan_checkpoint``, read from the binding and the
        plan on disk before the session writes it — the one predicate the prompt's
        ``Halt after planning.`` and the env's ``BMAD_LOOP_PLAN_HALT`` share."""
        return self._plan_halt_leg(task, self._entry_checkpoint_flags(task))

    def _dev_thorough(self, task: StoryTask) -> bool:
        """``[tickets] thorough_review`` for a dev leg: the find row's risk."""
        ticket = task.ticket
        risk = ticket.risk if ticket is not None else ""
        return wants_thorough(self.policy.tickets.thorough_review, risk)

    def _review_thorough(self, task: StoryTask) -> bool:
        """``[tickets] thorough_review`` for a review leg: the plan's own ``risk``,
        which the build settles at or above the ticket's. A plan that sets no
        risk falls back to the find row's. A plan that cannot be read, or whose
        frontmatter does not parse, may have raised it unseen: the review reads
        it as ``high`` and journals ``ticket-plan-risk-unread``."""
        ticket = task.ticket
        risk = ticket.risk if ticket is not None else ""
        plan = self._bound_plan(task)
        if plan is not None:
            try:
                text = plan.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as e:
                fault: str | None = f"{type(e).__name__}: {e}"
            else:
                fault = frontmatter_fault(text)
            if fault is not None:
                self.journal.append(
                    "ticket-plan-risk-unread",
                    story_key=task.story_key,
                    plan=str(plan),
                    detail=fault,
                )
                risk = RISK_HIGH
            else:
                planned = parse_frontmatter(text).get("risk")
                if isinstance(planned, str) and planned.strip():
                    risk = planned
        return wants_thorough(self.policy.tickets.thorough_review, risk)

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """``ticket <ref>`` on every leg: build-auto resolves the ref with
        ``tickets.py find`` and routes an existing plan by its status, before its
        clean-tree check, so the ref alone resumes a planned, in-review or
        restored plan.

        * fresh — :func:`~bmad_loop.tickets_source.dispatch_invocation` (the line
          the dry-run prints): ``thorough`` per ``[tickets] thorough_review``,
          ``Halt after planning.`` on leg 1 of an unrefined plan checkpoint, then
          the clauses and, on a retry with parked work, its notice paragraph;
        * restore — the restored change is reviewed against the amended plan;
        * repair — the plan is re-opened, and the feedback file stays the last
          backticked token.

        The clauses follow the base order (board, environment claim, park last)
        and carry no backticks."""
        clauses = [
            c
            for c in (
                self._sprint_board_instruction(),
                self._environment_claim_instruction(),
                self._operator_park_instruction(),
            )
            if c
        ]
        tail = " ".join(clauses)
        ticket = task.ticket
        ref = ticket.ref if ticket is not None else task.story_key
        skill = self._dev_skill()
        if feedback is not None:
            self._reset_spec_for_repair(task)
            plan = self._bound_plan(task)
            return (
                f"/{skill} ticket {ref} — resume the autonomous dev session on its "
                f"in-progress plan at `{plan or task.spec_file or ref}`. The previous "
                f"session's work failed deterministic verification; repair the working "
                f"tree so verification passes without changing the plan's frozen intent "
                f"contract. Verification evidence is in `{feedback}`."
            ) + (f" {tail}" if tail else "")
        if task.restore_patch and task.spec_file:
            return (
                f"/{skill} ticket {ref} — resume review of the in-review plan: the "
                f"attempted change was restored onto the working tree after an "
                f"intent-gap resolution; review it against the amended plan."
            ) + (f" {tail}" if tail else "")
        line = dispatch_invocation(
            skill, ref, thorough=self._dev_thorough(task), plan_halt=self._plan_halt_now(task)
        )
        if tail:
            line += (" " if line.endswith(".") else " — ") + tail
        preserved = self._retry_preserve_notice(task)
        if preserved:
            line += f"\n\n{preserved}"
        return line

    def _review_prompt(self, task: StoryTask) -> str:
        """The follow-up review re-dispatches the same ticket (upstream routes a
        ``built`` or ``done`` plan to a fresh review pass), ``thorough`` per the
        plan's risk, with the base ledger sentence and clauses. The base pins
        ``"append"`` out of the whole prompt; nothing here spells it."""
        park = self._operator_park_instruction() if self._review_demotion_parks() else ""
        clauses = [
            c
            for c in (
                self._sprint_board_instruction(),
                self._board_handback_redirect(),
                self._environment_claim_instruction(),
                park,
            )
            if c
        ]
        tail = " ".join(clauses)
        ticket = task.ticket
        ref = ticket.ref if ticket is not None else task.story_key
        head = f"/{self._dev_skill('review')} ticket {ref}"
        if self._review_thorough(task):
            head += " thorough"
        return (
            f"{head} — do NOT modify, re-open, or rewrite existing deferred-work ledger "
            f"entries; the orchestrator owns their status and resolution."
        ) + (f" {tail}" if tail else "")

    def _sprint_board_instruction(self) -> str:
        """Tickets mode has no sprint board; the ticket plan's status is what the
        orchestrator owns instead. Build-auto marks its own plan ``blocked`` when it
        HALTs, so that is the one ``mark`` a session may run. Bare sentence,
        backtick-free, never the word the review seam bans."""
        return (
            "The ticket plan's status belongs to the orchestrator: never mark the "
            "ticket done, and do not run tickets.py mark except through "
            "build-auto's own HALT. The orchestrator marks the ticket done after its "
            "own verification."
        )

    def _board_handback_redirect(self) -> str:
        """Where a review that cannot finish the ticket goes: build-auto's own
        blocked HALT, which records the reason on the plan through ``mark``."""
        return (
            "If the ticket cannot be finished without a human decision, HALT with "
            "status blocked and say why. That is the hand-back channel."
        )

    def _operator_park_instruction(self) -> str:
        """The base park contract, worded for the ticket plan. "" when parking is
        off; the callers keep it last."""
        if not self._operator_park_enabled():
            return ""
        return (
            "If this ticket's acceptance criteria include actions only a HUMAN "
            "can perform outside the repo (buy a domain, publish a DNS record, "
            "grant an API key, click through a vendor console): complete every "
            "part an agent CAN do, commit it, then finalize the plan frontmatter "
            "to status: awaiting-operator and enumerate what is owed under an "
            "operator_actions: key — a YAML list of strings, one imperative "
            "instruction each, non-empty. The final Auto Run Result must also "
            "report the matching status: awaiting-operator. Never use the blocked "
            "status for this: blocked halts the whole run, and this ticket is "
            "finished as far as you can take it."
        )

    def _extra_session_env(
        self, task: StoryTask, role: str, label: str | None = None
    ) -> dict[str, str]:
        """Only ``BMAD_LOOP_PLAN_HALT``, on a primary dev session's plan-halt leg,
        so the adapter reads a ``ready-for-dev`` plan as that leg's success. The
        plan itself reaches the adapter as ``expected_spec``."""
        if label is None and role == "dev" and self._plan_halt_now(task):
            return {"BMAD_LOOP_PLAN_HALT": "1"}
        return {}

    def _expected_spec_for(self, task: StoryTask, prompt: str, label: str | None) -> str | None:
        """The bound plan, from attempt 1: ``find`` named it before launch, so the
        read-back never scans the initiative folder for another ticket's plan. A
        labeled workflow session owes its completion marker, not the plan."""
        del prompt
        plan = self._bound_plan(task)
        if label is not None or plan is None or not self._generic_dev():
            return None
        return str(plan)

    def _session_success_statuses(self, role: str, label: str | None) -> tuple[str, ...]:
        """A dev leg finishes its plan at ``built``; a review leaves it at
        ``built``, or ``done`` when a human accepted it first. A labeled workflow
        session keeps the marker contract's ``done``."""
        if label is not None:
            return super()._session_success_statuses(role, label)
        if role == "review":
            return (verify.PLAN_BUILT, verify.PLAN_DONE)
        return (verify.PLAN_BUILT,)

    def _review_converged_statuses(self) -> frozenset[str]:
        """A review pass converges on the statuses that end a review session
        (``built``, or ``done`` on a plan a human accepted), so the two cannot
        drift: a review that finishes its plan at ``built`` is final, not a
        non-terminal pass that burns ``max_review_cycles`` and defers."""
        return frozenset(self._session_success_statuses("review", None))

    def _session_fallback_dirs(self, role: str, label: str | None) -> tuple[Path, ...]:
        """A build that cannot resolve its ticket HALTs into the initiative folder
        (``bmad-build-auto-result-*``), never into the plan: probe it there."""
        folder = self.workspace.paths.initiative_folder
        if label is not None or folder is None:
            return super()._session_fallback_dirs(role, label)
        return (folder,)

    def _dev_success_status(self) -> str:
        return verify.PLAN_BUILT

    def _dispatched_spec_for_attempt(self, task: StoryTask) -> str | None:
        """The bound plan when it is on disk at dispatch, as the canonical regular
        file — a fresh ticket has none yet and launches unbound."""
        plan = self._bound_plan(task)
        if plan is None:
            return None
        try:
            if plan.is_symlink():
                return None
            resolved = plan.resolve(strict=True)
            if not resolved.is_file() or not verify.spec_within_roots(
                resolved, self.workspace.paths
            ):
                return None
            return str(resolved)
        except (OSError, RuntimeError, ValueError):
            return None

    def _requires_dispatched_spec_snapshot(self, task: StoryTask, prompt: str) -> bool:
        """``ticket <ref>`` routes on an existing plan, so any plan on disk is
        input the session will mutate: it must launch with a recoverable snapshot.
        A plan whose presence cannot be read fails closed."""
        del prompt
        plan = self._bound_plan(task)
        if plan is None:
            return False
        try:
            return plan.exists() or plan.is_symlink()
        except OSError:
            return True

    def _record_dev_spec(self, task: StoryTask, result_json: dict | None) -> None:
        """Record the bound plan — known before launch — never the session's
        claim, and never a fallback result. No-op once set or while the plan is
        absent (a pre-plan HALT)."""
        del result_json
        if task.spec_file:
            return
        plan = self._bound_plan(task)
        if plan is not None and plan.is_file():
            task.spec_file = str(plan)

    def _harvest_spec_path(self, task: StoryTask, result_json: dict | None) -> Path | None:
        """The bound plan verification accepts, when the result names a file."""
        if not result_mapping(result_json).get("spec_file"):
            return None
        return self._bound_plan(task)

    # ---------------------------------------------------------------- verify

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        """:func:`~bmad_loop.verify.verify_dev_tickets` against the bound plan. The
        adapter's ``plan_halt`` marker is latched on the task, as stories mode
        does, and switches the gate to the ``ready-for-dev`` plan."""
        plan = self._bound_plan(task)
        ticket = task.ticket
        if plan is None or ticket is None:
            return VerifyOutcome.escalate(
                f"ticket unit {task.story_key} has no ticket binding to verify against"
            )
        plan_halt = bool(result_mapping(result_json).get("plan_halt"))
        task.plan_checkpoint_pending = plan_halt
        return verify.verify_dev_tickets(
            task,
            self.workspace.paths,
            result_json,
            plan=plan,
            # every dispatched row has an id: backlog rows (no id) never dispatch
            ticket_id=ticket.id or ticket.ref,
            plan_halt=plan_halt,
            operator_park=self._operator_park_enabled(),
            engine_written=self._harvest_gate_exclude(task),
        )

    def _run_verify_commands_after_dev(self, task: StoryTask, result_json: dict | None) -> bool:
        # a plan-halt leg produced only the plan: nothing to build or test yet.
        return not bool(result_mapping(result_json).get("plan_halt"))

    def _reset_spec_for_review(self, task: StoryTask) -> SpecSnapshot | None:
        """The base strip and snapshot, plus the plan status the review entered at
        (the snapshot's), which :meth:`_verify_review` holds the review to."""
        snapshot = super()._reset_spec_for_review(task)
        if snapshot is not None:
            self._review_entered[task.story_key] = snapshot.fm_status
        else:
            self._review_entered.pop(task.story_key, None)
        return snapshot

    def _verify_review(self, task: StoryTask):
        """:func:`~bmad_loop.verify.verify_review_tickets`, held to the plan status
        the review entered at. A replayed review (no launch this process) or an
        unreadable launch snapshot assumes ``built``, the status every dev leg
        leaves: a plan a human had already marked ``done`` then retries once,
        visibly, and the next launch records ``done``."""
        outcome = verify.verify_review_tickets(
            task,
            self.workspace.paths,
            self.policy,
            entered_status=self._review_entered.get(task.story_key, verify.PLAN_BUILT),
            operator_park=self._operator_park_enabled(),
            on_results=self._review_command_sink(task),
            on_probes=self._review_probe_sink(task),
        )
        if outcome.ok:
            self._accept_review_artifact_source(task)
        return outcome

    # ------------------------------------------------- plan status and board

    def _workspace_ticket_tree(self) -> TicketTreeLike:
        """The tree that owns the active workspace's plans: the run's own in
        place, one built for the unit worktree's project under isolation (its
        ``find`` names the worktree's copy of the plan)."""
        project = self.workspace.paths.project
        if project == self.paths.project:
            return self.ticket_tree
        return self._ticket_tree_factory(project, self.state.initiative or None)

    def _spec_status_write(
        self,
        task: StoryTask,
        spec: Path,
        target: str,
        reason: SpecWriteReason,
        *,
        confine_root: Path,
    ) -> bool:
        """Every engine status write lands on the bound plan through
        ``TicketTree.mark`` with its plan-transition reason
        (:data:`PLAN_WRITE_REASONS`), or is refused: nothing else writes a plan's
        ``status`` in tickets mode.

        Refused, with ``ticket-status-write-refused`` journaled and nothing
        written: a ``(reason, target)`` pair with no plan-transition reason — a
        salvage to ``done`` (salvage lands a plan at ``built`` only), a reconcile
        from the session's own prose, an adoption park (the park conversion owns
        ``awaiting-operator``) — and a write to anything but the bound plan. A mapped write that ``mark`` refuses
        or cannot make raises its ``TicketsError``, as the base writer's faults
        do. Returns whether the plan changed."""
        del confine_root
        plan_reason = PLAN_WRITE_REASONS.get((reason, target))
        plan = self._bound_plan(task)
        ticket = task.ticket
        if plan_reason is None:
            why = "no-plan-reason"
        elif plan is None or ticket is None:
            why = "unbound"
        elif not _same_file(spec, plan):
            why = "not-bound-plan"
        else:
            mark = self._workspace_ticket_tree().mark(
                ticket.ref,
                target,
                reason=plan_reason,
                expected_plan=plan,
                journal=self.journal,
            )
            return mark.outcome == "written"
        self.journal.append(
            "ticket-status-write-refused",
            story_key=task.story_key,
            site=reason,
            target=target,
            cause=why,
        )
        raise SpecStatusWriteRefused(reason, target, why)

    def _unit_branch_epic(self, task: StoryTask) -> str | None:
        """The ticket's epic slug, so a unit branch reads
        ``bmad-loop/<run>/<epic-slug>/<ref>``; None for an unbound task."""
        ticket = task.ticket
        if ticket is None or not ticket.epic_slug:
            return None
        return ticket.epic_slug

    def _terminal_plan_write(self, task: StoryTask) -> _PlanUndo | None:
        """The orchestrator's acceptance: ``mark <ref> done`` (reason ``accept``)
        on the bound plan, inside the commit window, under ``[tickets] mark_done =
        "auto"``. The store is tracked inside the code checkout (preflight refuses
        every other layout for now), so the marked plan rides the unit's own
        squash and reaches the target branch only with the code it describes —
        never before: ``next`` unblocks a done ticket's dependents.

        Nothing is written under ``"never"`` (the plan stays ``built`` for a
        person to accept), for an unbound task, or for a plan already ``done``
        (a no-op mark). A failed mark puts back whatever it may have written,
        then pauses at ``ticket-tree`` with the task still COMMITTING, so a
        resume re-drives the window and marks again. Returns the pre-image for
        :meth:`_undo_terminal_plan_write`."""
        plan = self._bound_plan(task)
        ticket = task.ticket
        if self.policy.tickets.mark_done != "auto" or plan is None or ticket is None:
            return None
        try:
            preimage = plan.read_bytes() if plan.is_file() else None
        except OSError as e:
            self._pause_mark_done(task, f"cannot read {plan}: {e}")
        undo = _PlanUndo(plan, preimage)
        try:
            mark = self._workspace_ticket_tree().mark(
                ticket.ref, "done", reason="accept", expected_plan=plan, journal=self.journal
            )
        except TicketsError as e:
            detail = f"tickets.py {e.kind}: {e}"
            if _bytes_or_none(plan) != preimage:
                if unrestored := self._undo_terminal_plan_write(task, undo):
                    detail = f"{detail}; {unrestored}"
            self._pause_mark_done(task, detail)
        return undo if mark.outcome == "written" else None

    def _undo_terminal_plan_write(self, task: StoryTask, undo: object | None) -> str | None:
        """Put the plan back to its pre-mark bytes through
        :meth:`~bmad_loop.tickettree.TicketTree.restore_preimage`, which journals
        ``ticket-plan-restored`` with its outcome either way. A failed restore is
        never a raise out of the commit window's failure arm: it is returned, so
        the escalation (or pause) names the plan left marked for a commit that
        does not exist instead of reading as a plain commit failure."""
        del task
        if not isinstance(undo, _PlanUndo):
            return None
        try:
            self._workspace_ticket_tree().restore_preimage(undo.plan, undo.data, self.journal)
        except (TicketsError, OSError) as e:
            return (
                f"{undo.plan} could not be put back to its pre-mark bytes ({e}) and may "
                "still read done; restore it before resuming"
            )
        return None

    def _pause_mark_done(self, task: StoryTask, detail: str) -> NoReturn:
        reason = (
            f"marking {task.story_key} done failed ({detail}) — the unit is verified "
            f"but not committed; fix the ticket tree, then {self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, f"mark done failed: {task.story_key}", reason)
        raise RunPaused(reason, PAUSE_TICKET_TREE, task.story_key)

    def _operator_park_enabled(self) -> bool:
        """False: a session-declared ``awaiting-operator`` plan has no ticket-tree
        meaning (``tickets.py`` reads it as a problems row), and its conversion to
        a ``blocked`` park through ``mark`` lands with the ticket lifecycle — so,
        like stories and sweep, no tickets prompt offers the park and both gates
        refuse the status."""
        return False

    def _reset_spec_for_repair(self, task: StoryTask) -> None:
        """Re-open the bound plan before a repair leg: ``mark <ref> in-progress``
        (reason ``repair``, through :meth:`_spec_status_write`), then strip the
        stale terminal section — the base's trusted-file checks and strip, on the
        plan ``find`` named. That plan is known before launch, so a leg whose
        verification failed before the spec was recorded is still re-opened:
        build-auto routes a ``built`` plan to review, not to a resumed build.
        No-op while the plan is absent (a pre-plan HALT)."""
        if not task.spec_file:
            plan = self._bound_plan(task)
            if plan is None or not plan.is_file():
                return
            task.spec_file = str(plan)
        super()._reset_spec_for_repair(task)

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """No-op: tickets mode has no sprint board to mirror the plan onto, so no
        ``board_advance_intended`` is ever recorded either."""

    def _carry_board_advance(self, task: StoryTask) -> None:
        """No-op: there is no sprint board to carry an advance to, whatever a
        restored task's ``board_advance_intended`` says."""

    def _review_demotion_parks(self) -> bool:
        """False: a review demotion parks through the sprint board in the base
        engine, and tickets mode has none. The ticket park path (blocked with an
        ``awaiting operator:`` reason) takes this over in Session 17 of the v7
        program, under ``[operator] on_review_demotion = "park"``."""
        return False
