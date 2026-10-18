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

Before a picked ticket is opened, a checkout-placed store whose initiative folder
holds uncommitted changes the run does not own pauses at ``ticket-tree``
(:meth:`TicketsEngine._gate_dirty_initiative`): the unit's commit stages the
whole tree.

Checkpoints follow upstream's contract through the checkpoint mixin. A refined
``plan_checkpoint`` ticket's story-file approval is the resume of its pick-time
pause (:func:`~bmad_loop.tickets_source.approve_paused_story_file`); an unrefined
one runs the two-leg plan checkpoint (:meth:`TicketsEngine._drive_story`) with no
interim commit; ``done_checkpoint`` is the mixin's ``_after_story``, fed by the
binding's flags and the next pick.

A picked ticket is dispatched as ``ticket <ref>`` on every leg
(:meth:`TicketsEngine._dev_prompt`, :meth:`TicketsEngine._review_prompt`), with
the dry-run's own invocation line (``thorough`` per ``[tickets]
thorough_review``, ``Halt after planning.`` on leg 1 of an unrefined plan
checkpoint). The read-back is pinned to the plan ``find`` named from attempt 1,
ends at ``built`` (dev) or ``built``/``done`` (review), and probes the
initiative folder for a no-plan fallback result. The dev and review gates are
``verify.verify_dev_tickets`` / ``verify.verify_review_tickets``: no sprint board,
and the orchestrator alone owns ``done``. The unit's commit carries
``Bmad-Ticket:`` and ``Bmad-Plan:`` trailers.

Where the ticket's terminal mark lands depends on the run's store topology. A
store tracked in the code checkout (T1) is marked inside the commit window, so the
plan rides the unit's commit. A shared store (T2–T4, T6: ``ProjectPaths.shared_store``)
cannot ride it, so the mark is latched on the task in the COMMITTING save
(``ticket_done_pending`` / ``ticket_park_pending``; ``ticket_commit_pending``
when ``mark_done = "never"`` leaves only the ``built`` plan to commit in a store
repository) and made once the code has landed: after ``finalize_commit`` in
place, after the merge-back under worktree isolation, or replayed on resume
before any pick. A failed mark pauses at ``ticket-tree``, latch kept.

A session (or, under ``[operator] on_review_demotion = "park"``, a review) that
parks the ticket at ``awaiting-operator`` keeps the base park verification; the
commit window then marks the plan ``blocked`` with an ``awaiting operator: ``
reason instead of ``done``, and ``bmad-loop confirm`` later marks it through the
same chokepoint. A code rollback re-arms the bound plan (``ready-for-dev``, or a
frontmatter-only plan deleted), and the recovery flow restores and re-routes a
plan only through the ticket tree, confined to the store root.

The epic boundary is checked before every pick and at run end
(:meth:`TicketsEngine._ticket_epic_boundary_check`), keyed by slug: an open epic
whose tickets are all ``built``/``done``/``dropped`` and of which this run
committed one gets its retrospective (``gates.retrospective = "auto"``:
``/bmad-retrospective -H <slug>``, verified on disk and committed alone), its
closure recorded in ``RunState.ticket_epic_closures`` and a pause at
``epic-boundary`` for a person to close it with bmad-ticket — bmad-loop never
writes the epic file. A ``rejected`` verdict pauses every later pick while the
epic stays open; any other pauses once. A ``--story`` run admits no epic, so it
finishes past the epic its ticket finished.
"""

from __future__ import annotations

import hashlib
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from . import deferredwork, gates, ticketstore, verify
from .adapters.base import SessionResult, SpecSnapshot
from .checkpoints import CheckpointFlags, CheckpointFlowMixin
from .engine import (
    RETRO_SKILL,
    RETRO_VERDICT_UNKNOWN,
    RETRO_VERDICTS,
    Engine,
    RunPaused,
    RunStopped,
    SpecStatusWriteRefused,
    SpecWriteReason,
    _retro_auto_prompt,
)
from .frontmatter import frontmatter_fault, is_frontmatter_only, parse_frontmatter, status_of
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_SPEC_APPROVAL,
    PAUSE_TICKET_TREE,
    Phase,
    RunState,
    StoryTask,
    TicketBinding,
    VerifyOutcome,
    result_mapping,
)
from .recovery_flow import OwnedPlanRefused, OwnedPlanWriter
from .runs import graceful_stop_requested, live_store_topology, rebase_recorded_project_path
from .statemachine import OPERATOR_PARK_PREFIX, PlanWriteReason, advance
from .ticketmodel import StatusView, TicketRow
from .tickets_source import (
    BOUNDARY_ACK,
    BOUNDARY_PAUSE,
    BOUNDARY_REJECTED,
    BOUNDARY_RETRO,
    CLOSURE_REJECTED,
    HALT_BLOCKED_PLAN,
    HALT_DIRTY_INITIATIVE,
    HALT_EPIC_CLOSURE,
    HALT_EPIC_REJECTED,
    HALT_HITL,
    HALT_IGNORED_PLAN,
    HALT_PLAN_CHECKPOINT,
    HALT_REFINE,
    NOTE_FOREIGN_ACTIVE,
    RISK_HIGH,
    SKIP_PARKED,
    Dispatch,
    EpicBoundary,
    Halt,
    PickNote,
    PickScope,
    Skip,
    TicketRef,
    TicketsWorkSource,
    dispatch_invocation,
    epic_boundary,
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


def _pick_scope(state: RunState) -> PickScope:
    """The ``--epic``/``--story`` selector as ``run`` resolved it into state."""
    return PickScope(state.ticket_epic_filter, state.story_filter or "")


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


@dataclass(frozen=True)
class _PlanLeftMarked:
    """A plan that reads ``target``, not the ``before`` the COMMITTING save
    latched: marked since (e.g. by an interrupted commit window, an adoption, or a
    hand edit). No pre-image bytes survive to restore it."""

    plan: Path
    target: str
    before: str


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


def _overlaps(path: str, folder: str) -> bool:
    """Whether repo-relative ``path`` lies in ``folder`` (``"."`` is the whole
    checkout), or contains it: an untracked nested repository is one dirty
    entry for its whole directory."""
    if folder == ".":
        return True
    return path == folder or path.startswith(folder + "/") or folder.startswith(path + "/")


# A rolled-back attempt's plan re-routed to `ready-for-dev` (the recovery flow's
# plain route) or to a re-drive's resume status; nothing else moves it there.
_ROLLBACK_ROUTE_REASONS: Mapping[str, PlanWriteReason] = {
    "ready-for-dev": "rollback",
    "in-progress": "rearm",
    "in-review": "rearm",
}
# What a code rollback re-arms: a plan the attempt carried past `ready-for-dev`.
_REARMED_AFTER_ROLLBACK = frozenset({"in-progress", "in-review", "built"})


def _plan_state(plan: Path) -> tuple[str, str, str] | None:
    """``(status, blocked_reason, text)`` of the plan on disk, None when it is
    absent. Raises ``OSError``/``UnicodeDecodeError`` for one that cannot be
    read."""
    if not plan.exists():
        return None
    text = plan.read_text(encoding="utf-8-sig")
    fm = parse_frontmatter(text)
    reason = fm.get("blocked_reason")
    return status_of(fm), reason.strip() if isinstance(reason, str) else "", text


def operator_park_reason(actions: Iterable[str]) -> str:
    """The ``blocked_reason`` a ticket park carries: the operator-park prefix,
    then what the human owes, one action after another."""
    return OPERATOR_PARK_PREFIX + "; ".join(a.strip() for a in actions if a.strip())


_TRAILER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*: \S")


def _with_trailers(message: str, trailers: list[str]) -> str:
    """``message`` with ``trailers`` appended to its closing trailer block, or
    to a new one after a blank line when its last paragraph is prose. A
    one-paragraph message is its subject, never a trailer block."""
    body = message.rstrip()
    paragraphs = body.split("\n\n")
    last = paragraphs[-1].splitlines()
    if len(paragraphs) > 1 and last and all(_TRAILER_RE.match(line) for line in last):
        return body + "\n" + "\n".join(trailers)
    return body + "\n\n" + "\n".join(trailers)


def default_ticket_tree(project_root: Path, pinned_initiative: str | None) -> TicketTreeLike:
    """The project's installed ``tickets.py``, pinned to ``pinned_initiative``."""
    return TicketTree(project_root, pinned_initiative=pinned_initiative)


class _TicketPlanWriter:
    """The recovery flow's writer for a bound ticket plan
    (:class:`~bmad_loop.recovery_flow.OwnedPlanWriter`): the byte restore is
    ``TicketTree.restore_preimage`` and the lifecycle route is ``mark``, so a
    rolled-back plan never meets the recovery flow's own frontmatter writer."""

    def __init__(self, engine: TicketsEngine) -> None:
        self._engine = engine

    def owns(self, task: StoryTask, spec_path: Path) -> bool:
        plan = self._engine._bound_plan(task)
        return plan is not None and _same_file(spec_path, plan)

    def restore(self, task: StoryTask, spec_path: Path, data: bytes) -> None:
        del task
        engine = self._engine
        try:
            engine._workspace_ticket_tree().restore_preimage(spec_path, data, engine.journal)
        except (TicketsError, OSError) as e:
            raise OwnedPlanRefused(str(e)) from e

    def normalize(self, task: StoryTask, spec_path: Path, target_status: str) -> None:
        del spec_path
        self._engine._route_plan(task, target_status)


class TicketsEngine(CheckpointFlowMixin, Engine):
    def __init__(
        self, *args: Any, ticket_tree_factory: TicketTreeFactory | None = None, **kwargs: Any
    ):
        # `--epic`/`--story` select by epic slug and ticket ref here, not by the
        # sprint E-S grammar, so keep them out of the base's selector parse (the
        # source reads them from run state). Set before super().__init__, which
        # builds the source.
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
        # Whether this run has journaled `store-unversioned`; None until the
        # journal is first read for it (`_note_store_unversioned`).
        self._unversioned_noted: bool | None = None
        # A store no checkout carries is shared: every artifact dir under it stays
        # at the main checkout (`ProjectPaths.rebased`), and worktree mounts are
        # redirected to it. Read from the topology the run recorded at its start,
        # moved with a renamed project.
        state, paths = kwargs.get("state"), kwargs.get("paths")
        if state is not None and paths is not None and state.store_topology:
            kwargs["paths"] = ticketstore.with_shared_store(
                paths, live_store_topology(state, paths.project)
            )
        super().__init__(*args, **kwargs)
        # pin the mode so resume/resolve rebuild a TicketsEngine without policy.
        self.state.source = SOURCE_TICKETS

    # ------------------------------------------------------------------ tree

    @property
    def store_topology(self) -> ticketstore.StoreTopology | None:
        """The ticket store the run recorded at its start, located on the live
        project (:func:`~bmad_loop.runs.live_store_topology`); None when it recorded
        none (no initiative to classify, or a run older than the record)."""
        recorded = live_store_topology(self.state, self.paths.project)
        return ticketstore.StoreTopology.from_dict(recorded) if recorded else None

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
        first pick, and approvals, closures, closure acknowledgements and the
        ``--epic``/``--story`` selector (resolved by ``run``) are read from run
        state each time."""
        return TicketsWorkSource(
            lambda: self.ticket_tree,
            lambda: self.state.ticket_story_approvals,
            lambda: self.state.ticket_epic_closures,
            lambda: _pick_scope(self.state),
            acked_get=lambda: self.state.ticket_epic_closure_acked,
        )

    @property
    def _tickets_source(self) -> TicketsWorkSource:
        source = self.source
        assert isinstance(source, TicketsWorkSource)
        return source

    # --------------------------------------------------------------- picking

    def _pick_next(self) -> WorkRef | None:
        self._repause_inrun_escalation()  # raises when one is live
        decision = self._tickets_source.decide(
            self._phases(), gate=self._ticket_epic_boundary_check
        )
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
        ``find`` fault pauses with no task recorded and a resume re-picks. The
        dirty-initiative gate runs first, for the same reason."""
        self._gate_dirty_initiative(decision.ref, decision.row)
        try:
            found = self.ticket_tree.find(decision.ref)
        except TicketsError as e:
            self._pause_ticket_pick(
                Halt("tree-fault", decision.ref, decision.row, f"tickets.py {e.kind}: {e}")
            )
        self._gate_ignored_plan(decision.ref, decision.row, Path(found.plan))
        self._picked[decision.ref] = ticket_binding(found)
        return TicketRef(key=decision.ref, row=decision.row)

    def _gate_ignored_plan(self, ref: str, row: TicketRow | None, plan: Path) -> None:
        """Pause at ``ticket-tree``, before the picked ticket is opened, when a
        worktree unit's mark could not ride its commit: the store is tracked in
        the code checkout (T1, so the plan is marked in the mount inside the
        commit window) but the code repo ignores the plan ``find`` named.
        ``finalize_commit``'s ``add -A`` would leave the marked plan behind, the
        teardown would delete it with the mount, and the main checkout's ticket
        would stay unmarked — dispatched again, its dependents still blocked.

        Asked of the path ``find`` names, which a build may not have written yet:
        ``check-ignore`` answers for a path that does not exist. Not asked in
        place (the mark lands on the plan ``tickets.py`` reads) nor for a shared
        store (marked after the code lands, in the store). A probe fault pauses
        the same way: the gate fails closed."""
        if not self._isolated or self.paths.shared_store is not None:
            return
        try:
            ignored = verify.path_ignored(self.paths.repo_root, plan)
        except verify.GitError as e:
            detail = f"cannot read whether the code repository ignores {plan}: {e}"
        else:
            if not ignored:
                return
            detail = (
                f"the plan {plan} of {ref} is gitignored by the code repository, "
                "so under worktree isolation its mark could not ride the unit's commit "
                "and the ticket would stay unmarked; commit the plans with the "
                "initiative folder, or gitignore the whole store"
            )
        self._pause_ticket_pick(Halt(HALT_IGNORED_PLAN, ref, row, detail))

    def _gate_dirty_initiative(self, ref: str, row: TicketRow | None) -> None:
        """Pause at ``ticket-tree``, before the picked ticket is opened, when the
        checkout holds uncommitted edits under the initiative folder that this
        run does not own. The unit's commit stages the whole tree
        (``finalize_commit``'s ``add -A``), so a person's in-flight edit to a plan
        or ``tickets.toml`` would ride the unit's commit, and build-auto's own
        commit step refuses a dirty tree. The run's own writes there
        (:meth:`_run_owned_paths`) never trip it.

        Applies only to a store placed in the code checkout (the initiative
        folder under ``repo_root``); a store elsewhere is not staged by the
        unit's commit. A checkout whose status cannot be read pauses the same
        way: the gate fails closed."""
        folder = self.paths.initiative_folder
        repo = self.paths.repo_root
        if folder is None:
            return
        try:
            prefix = folder.resolve().relative_to(repo.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return
        try:
            dirty = verify.dirty_paths(repo)
        except verify.GitError as e:
            self._pause_ticket_pick(
                Halt(
                    HALT_DIRTY_INITIATIVE,
                    ref,
                    row,
                    f"cannot read the checkout's status before opening {ref}: {e}",
                )
            )
        owned = self._run_owned_paths()
        touched = sorted(
            path
            for path in dirty
            if _overlaps(path.rstrip("/"), prefix) and path.rstrip("/") not in owned
        )
        if not touched:
            return
        shown = ", ".join(touched[:5]) + (
            f" and {len(touched) - 5} more" if len(touched) > 5 else ""
        )
        self._pause_ticket_pick(
            Halt(
                HALT_DIRTY_INITIATIVE,
                ref,
                row,
                f"the initiative folder {prefix} has uncommitted changes this run does not "
                f"own ({shown}); commit or stash them before {ref} is built, "
                f"because the ticket's commit stages the whole tree",
            )
        )

    def _run_owned_paths(self) -> frozenset[str]:
        """Repo-relative paths under the initiative folder the run itself writes
        outside a unit's commit, which the dirty-initiative gate ignores: the
        deferred-work ledger, whose carry and defer paths can leave a tracked
        ledger's edits uncommitted (it lives in the initiative folder on v7)."""
        repo = self.paths.repo_root
        try:
            ledger = self.paths.deferred_work.resolve().relative_to(repo.resolve())
        except (OSError, RuntimeError, ValueError):
            return frozenset()
        return frozenset({ledger.as_posix()})

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
        would, through the same gates as :meth:`_dispatch_ref`, so a unit the pick
        would have refused is not opened on the resume. A ``find`` fault or a
        gate pauses at ``ticket-tree``, and the resume retries."""
        self._gate_dirty_initiative(task.story_key, None)
        try:
            found = self.ticket_tree.find(task.story_key)
        except TicketsError as e:
            self._pause_ticket_pick(
                Halt("tree-fault", task.story_key, None, f"tickets.py {e.kind}: {e}")
            )
        self._gate_ignored_plan(task.story_key, None, Path(found.plan))
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

    # ---------------------------------------------------------- epic boundary

    def _committed_epics(self) -> frozenset[str]:
        """Epic slugs this run committed at least one ticket of."""
        return frozenset(
            task.ticket.epic_slug
            for task in self.state.tasks.values()
            if task.ticket is not None
            and task.ticket.epic_slug
            and task.phase == Phase.DONE
            and task.commit_sha
        )

    def _ticket_epic_boundary_check(self, status_view: StatusView) -> None:
        """The epic boundary, run before every pick (on the pick's own ``status``
        view) and at run end, so it survives a pause and a resume
        (:func:`~bmad_loop.tickets_source.epic_boundary` decides, in build order):

        * a finished epic this run committed a ticket of, with no closure
          recorded: run the retrospective gate (:meth:`_ticket_epic_retro`),
          record its verdict in ``state.ticket_epic_closures``, journal
          ``ticket-epic-closure`` and pause at ``epic-boundary`` for a person to
          close the epic with bmad-ticket;
        * a recorded ``rejected`` verdict on an epic still open and finished:
          pause again — closing the epic is the human override;
        * any other recorded verdict the operator resumed past: acknowledge it
          (``ticket_epic_closure_acked``), journal ``epic-closure-not-recorded``
          (the epic is still open) and go on; the pick's end-of-tree
          ``awaiting-closure`` halt skips an acknowledged epic too;
        * a recorded verdict no resume answered (``ticket_epic_closure_resumed``,
          set by the resume prologue): the run stopped between recording the
          closure and pausing for it, so pause for it now.

        Only the epics the run's selector admits (:meth:`PickScope.admits_epic`)
        are checked: a ``--story`` run admits none, so the epic its ticket
        finished is left to a later run (:meth:`_run_end_retrospective` journals
        it). bmad-loop never writes the epic's file: its status is read, never
        set."""
        scope = _pick_scope(self.state)
        for step in self._epic_boundary_steps(status_view):
            if not scope.admits_epic(step.slug):
                continue
            if step.action == BOUNDARY_ACK:
                self._ack_epic_closure(step)
                continue
            if step.action == BOUNDARY_RETRO:
                verdict = self._ticket_epic_retro(step.slug)
                self.state.ticket_epic_closures[step.slug] = verdict
                self._save()
                self.journal.append("ticket-epic-closure", epic_ref=step.slug, verdict=verdict)
                self._pause_epic_closure(step.slug, verdict)
            if step.action == BOUNDARY_PAUSE:
                self._pause_epic_closure(step.slug, step.verdict)
            if step.action == BOUNDARY_REJECTED:
                self._pause_ticket_pick(
                    Halt(
                        HALT_EPIC_REJECTED,
                        step.slug,
                        None,
                        f"{step.slug}'s retrospective verdict is rejected; close {step.slug} "
                        f"with bmad-ticket before the run goes on",
                    )
                )

    def _epic_boundary_steps(self, status_view: StatusView) -> list[EpicBoundary]:
        return epic_boundary(
            status_view,
            self.state.ticket_epic_closures,
            frozenset(self.state.ticket_epic_closure_acked),
            self._committed_epics(),
            resumed=frozenset(self.state.ticket_epic_closure_resumed),
        )

    def _ack_epic_closure(self, step: EpicBoundary) -> None:
        """The operator resumed past an epic's one closure pause without closing
        it: acknowledge it so it never pauses again, and say so once."""
        self.state.ticket_epic_closure_acked.append(step.slug)
        self._save()
        self.journal.append("epic-closure-not-recorded", epic_ref=step.slug, verdict=step.verdict)

    def _pause_epic_closure(self, slug: str, verdict: str) -> NoReturn:
        detail = f"close {slug} with bmad-ticket (closure check; retrospective verdict: {verdict})"
        if verdict == CLOSURE_REJECTED:
            detail += f"; the rejected verdict holds the run until {slug} is closed"
        self._pause_ticket_pick(Halt(HALT_EPIC_CLOSURE, slug, None, detail))

    def _run_end_retrospective(self) -> None:
        """The epic boundary at run end (an exhausted tree or ``--max-stories``):
        the same check as the pick's, on a fresh ``status`` read, so the run's
        last finished epic gets its retrospective and closure pause too. A tree
        that cannot be read journals ``retro-run-end-skipped``
        (``board-unreadable``) and pauses at ``ticket-tree``, as the pick's own
        read fault does: the epics this run committed are known to this run only,
        so finishing would lose the boundary for good. An epic this run finished
        outside its selector (a ``--story`` run's) journals
        ``retro-run-end-skipped`` (``out-of-scope``) instead. Store commits a
        landed mark still owes are settled first
        (:meth:`_settle_owed_store_commits`)."""
        self._settle_owed_store_commits()
        try:
            status_view = self.ticket_tree.status()
        except TicketsError as e:
            self.journal.append(
                "retro-run-end-skipped", epic=0, reason="board-unreadable", error=str(e)
            )
            self._pause_ticket_pick(Halt("tree-fault", "", None, f"tickets.py {e.kind}: {e}"))
        scope = _pick_scope(self.state)
        for step in self._epic_boundary_steps(status_view):
            if step.action == BOUNDARY_RETRO and not scope.admits_epic(step.slug):
                self.journal.append(
                    "retro-run-end-skipped", epic=0, epic_ref=step.slug, reason="out-of-scope"
                )
        self._ticket_epic_boundary_check(status_view)

    def _retro_doc(self, slug: str) -> Path:
        """Where the retrospective skill writes epic ``slug``'s document:
        ``<epic folder>/<slug>-retrospective.md``, the epic folder named by its
        slug inside the initiative folder."""
        folder = self.paths.initiative_folder or self.paths.output_folder
        return folder / slug / f"{slug}-retrospective.md"

    def _read_retro_doc(self, slug: str) -> tuple[list[str], str, int]:
        """Read epic ``slug``'s retrospective document as ``(errors, verdict,
        mtime_ns)``: it must be a regular file (``lstat``, never followed) whose
        frontmatter names ``epic: <slug>`` and a ``verdict`` in
        :data:`~bmad_loop.engine.RETRO_VERDICTS`. ``verdict`` is "" unless every
        check passed; ``mtime_ns`` is -1 when the file cannot be stat'd."""
        doc = self._retro_doc(slug)
        name = doc.name
        try:
            st = doc.lstat()
        except FileNotFoundError:
            return [f"no retrospective document {name}"], "", -1
        except OSError as e:
            return [f"cannot stat {name}: {e}"], "", -1
        if not stat.S_ISREG(st.st_mode):
            return [f"{name} is not a regular file"], "", st.st_mtime_ns
        try:
            fm = verify.read_frontmatter(doc)
        except OSError as e:
            return [f"cannot read {name}: {e}"], "", st.st_mtime_ns
        errors: list[str] = []
        epic = fm.get("epic")
        if not isinstance(epic, str) or epic.strip() != slug:
            errors.append(f"{name} frontmatter epic is not {slug}")
        raw = fm.get("verdict")
        verdict = raw.strip().lower() if isinstance(raw, str) else ""
        if verdict not in RETRO_VERDICTS:
            errors.append(f"{name} frontmatter verdict is not one of {sorted(RETRO_VERDICTS)}")
        return errors, ("" if errors else verdict), st.st_mtime_ns

    def _verify_retro_ticket(
        self, slug: str, result: SessionResult | None, launched_ns: int
    ) -> tuple[list[str], str]:
        """Deterministic success checks for epic ``slug``'s auto-retro session,
        as ``(errors, verdict)``: a completed session whose ``result.json`` says
        ``done``, and a retrospective document (:meth:`_read_retro_doc`) written
        at or after the launch. Nothing the session said is taken on trust.
        ``result`` is None when the session raised."""
        errors: list[str] = []
        if result is not None:
            if result.status != "completed":
                errors.append(f"session status {result.status}, not completed")
            elif result_mapping(result.result_json).get("status") != "done":
                errors.append("result.json status is not done")
        doc_errors, verdict, mtime_ns = self._read_retro_doc(slug)
        errors.extend(doc_errors)
        if 0 <= mtime_ns < launched_ns:
            errors.append(f"{self._retro_doc(slug).name} predates the session")
        return errors, ("" if errors else verdict)

    def _retro_doc_uncommitted(self, slug: str) -> bool:
        """Whether epic ``slug``'s retrospective document is dirty in the code repo,
        or in a store with its own repository (T3/T4). A git fault, or a path that
        does not resolve, reads as dirty: :meth:`_settle_retro_tree` re-reads the
        tree and pauses on it. Only a document outside a repository skips it."""
        doc = self._retro_doc(slug)
        roots = [self.paths.repo_root]
        topology = self.store_topology
        if topology is not None and topology.store_repo is not None:
            roots.append(topology.store_repo)
        for root in roots:
            try:
                resolved, top = doc.resolve(), root.resolve()
            except (OSError, RuntimeError):
                return True
            try:
                rel = resolved.relative_to(top).as_posix()
            except ValueError:
                continue  # outside this repository
            try:
                if rel in verify.dirty_paths(root):
                    return True
            except verify.GitError:
                return True
        return False

    def _pause_retro_refused(self, slug: str, why: str) -> NoReturn:
        """Pause at ``epic-boundary`` before ``slug``'s auto retrospective, with no
        closure recorded, so the resume runs it."""
        raise RunPaused(
            f"{slug} boundary — its auto retrospective did not run: {why}; fix that, "
            f"then {self._resume_hint()}",
            PAUSE_EPIC_BOUNDARY,
            slug,
        )

    def _ticket_epic_retro(self, slug: str) -> str:
        """The retrospective gate for finished epic ``slug``; returns the closure
        to record: the document's verdict, or ``unknown`` when none was read.

        A valid document already on disk is the retro already done (journaled
        ``retro-auto-skipped`` ``already-done`` under ``auto``), whatever the mode.
        Under ``auto`` a valid document left uncommitted (an earlier leg that
        failed verification) is committed alone first, as a success would be.
        ``notify`` sends the nudge and ``never`` does nothing; both still pause
        for closure. ``auto`` runs ``/bmad-retrospective -H <slug>`` on the retro
        adapter, after the base pre-dispatch refusals. The passing ones record no
        closure, so the next pass re-enters here: ``stop-requested`` honors the
        stop, and ``dirty``/``git-error`` pause at ``epic-boundary``. Only
        ``skill-missing`` lasts and records ``unknown``. The session is verified
        (:meth:`_verify_retro_ticket`), and settles the tree: the document alone
        (``TicketsWorkSource.retro_commit_paths``) is committed, and anything else
        left dirty pauses before a closure is recorded, so a resume re-enters
        here. The ``retro-auto-*`` rows keep ``epic`` 0 and name the slug in
        ``epic_ref``."""
        mode = self.policy.gates.retrospective
        doc_errors, existing, _ = self._read_retro_doc(slug)
        if not doc_errors:
            if mode == "auto":
                self._skip_auto_retro(0, "already-done", epic_ref=slug)
                if self._retro_doc_uncommitted(slug):
                    self._settle_retro_tree(0, True, [self._retro_doc(slug)], epic_ref=slug)
            return existing
        if mode == "notify":
            gates.notify(
                self.policy,
                self.run_dir,
                f"{slug} tickets complete",
                f"retrospective suggested: run /bmad-retrospective -H {slug} before closing it",
            )
        if mode != "auto":
            return RETRO_VERDICT_UNKNOWN
        if graceful_stop_requested(self.run_dir):
            self._skip_auto_retro(0, "stop-requested", epic_ref=slug)
            self._check_stop_request()
            self._pause_retro_refused(slug, "a stop was requested")  # a racing take
        try:
            clean = verify.worktree_clean(self.workspace.root, project=self.workspace.paths.project)
        except verify.GitError as e:
            self._skip_auto_retro(0, "git-error", str(e), epic_ref=slug)
            self._pause_retro_refused(slug, f"git could not read the worktree ({e})")
        if not clean:
            self._skip_auto_retro(0, "dirty", epic_ref=slug)
            self._pause_retro_refused(slug, "the tree has uncommitted changes")
        adapter = self.adapters["retro"]
        tree = getattr(getattr(adapter, "profile", None), "skill_tree", None)
        if tree and not (self.workspace.paths.project / tree / RETRO_SKILL / "SKILL.md").is_file():
            self._skip_auto_retro(0, "skill-missing", epic_ref=slug)
            return RETRO_VERDICT_UNKNOWN

        task = StoryTask(story_key=f"{slug}-retrospective", epic=0)
        self.journal.append("retro-auto-start", epic=0, epic_ref=slug)
        # whole seconds: a filesystem with coarse timestamps still dates the doc
        # at or after the launch
        launched_ns = time.time_ns() // 1_000_000_000 * 1_000_000_000
        result: SessionResult | None = None
        raised: Exception | None = None
        try:
            result = self._run_session(task, role="retro", prompt=_retro_auto_prompt(slug), seq=1)
        except (RunStopped, RunPaused):
            raise
        except Exception as e:  # a failed retro, never a failed run
            raised = e
        errors, verdict = self._verify_retro_ticket(slug, result, launched_ns)
        if raised is not None:
            errors.insert(0, f"session raised {type(raised).__name__}")
        ok = not errors
        doc = self._retro_doc(slug)
        if ok:
            self.journal.append(
                "retro-auto-finished", epic=0, epic_ref=slug, docs=[doc.name], verdict=verdict
            )
            gates.notify(
                self.policy,
                self.run_dir,
                f"{slug} retrospective done",
                f"retrospective written: {doc.name}; verdict: {verdict}",
            )
        else:
            verdict = RETRO_VERDICT_UNKNOWN
            extra = {} if raised is None else {"error": str(raised)}
            self.journal.append("retro-auto-failed", epic=0, epic_ref=slug, errors=errors, **extra)
            gates.notify(
                self.policy,
                self.run_dir,
                "auto retrospective failed",
                f"{slug}: {'; '.join(errors)} — run /bmad-retrospective -H {slug} by hand",
            )
        self._settle_retro_tree(0, ok, [doc] if ok else [], epic_ref=slug)
        return verdict

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

    # ------------------------------------------------------- plan checkpoint

    def _drive_story(self, task: StoryTask, dev_resume: SessionResult | None = None) -> None:
        """The two-leg plan checkpoint of an unrefined ``plan_checkpoint`` ticket,
        as stories mode drives it through the checkpoint mixin.

        Leg 1 (``Halt after planning.``) latches ``plan_review_owed`` and
        journals ``plan-halt`` before its session runs; the verified
        ``ready-for-dev`` plan then pauses at ``plan-checkpoint``. The plan is
        left uncommitted: on resume, leg 2 re-dispatches ``ticket <ref>``, which
        build-auto routes on the plan's status before its clean-tree check, and
        the unit's squash carries the plan with the code. A plan review still
        owed when the code is written pauses before the commit.

        A refined ticket was approved before dispatch (the pick's
        ``plan-checkpoint`` pause), so it takes neither leg. ``dev_resume`` None
        is a fresh drive, not a replay of a session that already ran."""
        if dev_resume is None:
            # the dispatch gate first, so an `environment` pause writes neither the
            # latch nor its journal row (a passing probe is fresh for `_dev_phase`)
            self._gate_dispatch(task, "dev")
        if dev_resume is None and self._plan_halt_now(task):
            # Saved before the session, so a crash after it, a retry that moved the
            # plan on, or a session that overran the halt still owes the review.
            task.plan_review_owed = True
            self._save()
            self.journal.append(
                "plan-halt", story_key=task.story_key, spec_folder=self._initiative_rel()
            )
        if not self._dev_phase(task, resume_result=dev_resume):
            return
        if task.plan_checkpoint_pending:
            self._pause_plan_checkpoint(task)  # always raises RunPaused
        if task.plan_review_owed:
            self._pause_plan_review_owed(task)  # always raises RunPaused
        if gates.pause_after_spec(self.policy):
            gates.notify(
                self.policy,
                self.run_dir,
                f"spec ready for approval: {task.story_key}",
                f"review {self._operator_spec_path(task)}, then {self._resume_hint()}",
            )
            raise RunPaused(
                f"awaiting spec approval for {task.story_key}",
                PAUSE_SPEC_APPROVAL,
                task.story_key,
            )
        self._review_and_commit(task)

    def _initiative_rel(self) -> str:
        """The initiative folder relative to the project, as the ``plan-halt``
        row names its folder; "" when there is none."""
        folder = self.paths.initiative_folder
        if folder is None:
            return ""
        try:
            return folder.relative_to(self.paths.project).as_posix()
        except ValueError:
            return str(folder)

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
        (the same path in place; the unit worktree's copy under isolation, but
        the one shared plan when the store is shared). A plan outside the
        project is left as recorded: under isolation the preflight refuses a
        tracked initiative folder outside the project
        (``tickets.store-topology``), since the mount re-anchors only what the
        project holds. None for a task with no binding."""
        ticket = task.ticket
        if ticket is None:
            return None
        plan = self._recorded_plan(ticket)
        if self.paths.in_shared_store(plan):
            return plan
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
        session, and the epic retrospective, keep the base contract."""
        if label is not None or role not in ("dev", "review"):
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
        (``bmad-build-auto-result-*``), never into the plan: probe it there. The
        epic retrospective is not a build."""
        folder = self.workspace.paths.initiative_folder
        if label is not None or folder is None or role not in ("dev", "review"):
            return super()._session_fallback_dirs(role, label)
        return (folder,)

    def _dev_success_status(self) -> str:
        return verify.PLAN_BUILT

    def _harvest_success_statuses(self) -> frozenset[str]:
        """A build leaves its plan at ``built``; a follow-up pass on a plan a
        human already accepted finds it at ``done``. Both carry real findings."""
        return frozenset({verify.PLAN_BUILT, verify.PLAN_DONE})

    # ------------------------------------------------------------ ledger gates

    def _gate_binding(self, story_key: str) -> TicketBinding | None:
        """The binding a gate check reads the ticket's epic from: resolved at pick
        time (the gate runs before ``_loop`` registers the task), or the task's own
        on the restart arm — re-resolved there first when a crash left it unbound
        (:meth:`_rebind`), so its epic's tokens still gate it."""
        task = self.state.tasks.get(story_key)
        if task is not None and task.ticket is None and story_key not in self._picked:
            task.ticket = self._rebind(task)
            self._save()
        return self._picked.get(story_key) or (task.ticket if task is not None else None)

    def _gate_hits(self, entry: deferredwork.DWEntry, story_key: str) -> list[str]:
        """Ticket-grammar tokens that gate ``story_key``: its ref, its epic's id or
        its epic's folder (:func:`deferredwork.gates_ticket`). With no binding only
        the ref arm can match."""
        binding = self._gate_binding(story_key)
        epic_id = binding.epic_id if binding is not None else ""
        epic_slug = binding.epic_slug if binding is not None else ""
        return [
            token
            for token in deferredwork.gates(entry, grammar="ticket").tokens
            if deferredwork.gates_ticket(token, story_key, epic_id=epic_id, epic_slug=epic_slug)
        ]

    def _legacy_gate_tokens(self, entry: deferredwork.DWEntry) -> tuple[str, ...]:
        return deferredwork.legacy_story_tokens(entry)

    def _legacy_gate_clause(
        self, story_key: str, legacy: list[tuple[str, tuple[str, ...]]], ledger_name: str
    ) -> str:
        named = ", ".join(f"{dw_id} (gate: {', '.join(tokens)})" for dw_id, tokens in legacy)
        return (
            f"{named} in {ledger_name} declare v6 story-key gate tokens, which no ticket "
            f"ref can match, so {story_key} cannot be shown free of them — the v7 "
            f"migration keeps each story's old key as `v6_key` in its ticket file; "
            f"rewrite the tokens as ticket refs (`E.M`, an epic id or `epic-<slug>`) or "
            f"close the entry"
        )

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
        outcome = verify.verify_dev_tickets(
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
        # A plan-halt leg waives the proof-of-work gate (it wrote no code). The
        # waiver leaves one row per attempt once every other gate passed, as in
        # stories mode; `zero_diff` observes the skipped gate, never feeds it.
        if plan_halt and outcome.ok and not self._plan_halt_waiver_recorded(task):
            self.journal.append(
                "plan-halt-proof-of-work-skipped",
                story_key=task.story_key,
                attempt=task.attempt,
                generation=task.generation,
                zero_diff=outcome.plan_halt_zero_diff,
            )
        return outcome

    def _plan_halt_waiver_recorded(self, task: StoryTask) -> bool:
        """Whether this attempt's proof-of-work waiver is already journaled (a
        replayed verify of the same leg)."""
        return any(
            entry.get("kind") == "plan-halt-proof-of-work-skipped"
            and entry.get("story_key") == task.story_key
            and entry.get("attempt") == task.attempt
            and entry.get("generation", 0) == task.generation
            for entry in self.journal.entries()
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

    def _commit_message(self, task: StoryTask) -> str:
        """The base message plus the git trailers that tie the unit's commit to
        its ticket: ``Bmad-Ticket: <ref>`` and ``Bmad-Plan: <plan>``, the plan
        relative to the code checkout (omitted when it lies outside it). They
        join a trailer block the message already ends with (a template's
        ``Signed-off-by:``), else open one. An unbound task gets the base
        message."""
        message = super()._commit_message(task)
        ticket = task.ticket
        if ticket is None:
            return message
        trailers = [f"Bmad-Ticket: {ticket.ref}"]
        try:
            plan = self._recorded_plan(ticket).resolve().relative_to(self.paths.repo_root.resolve())
        except (OSError, RuntimeError, ValueError):
            plan = None
        if plan is not None:
            trailers.append(f"Bmad-Plan: {plan.as_posix()}")
        return _with_trailers(message, trailers)

    def _unit_branch_epic(self, task: StoryTask) -> str | None:
        """The ticket's epic slug, so a unit branch reads
        ``bmad-loop/<run>/<epic-slug>/<ref>``; None for an unbound task."""
        ticket = task.ticket
        if ticket is None or not ticket.epic_slug:
            return None
        return ticket.epic_slug

    def _terminal_plan_write(self, task: StoryTask) -> _PlanUndo | _PlanLeftMarked | None:
        """The ticket's terminal write on the bound plan, inside the commit
        window, for a store tracked inside the code checkout (T1): the marked plan
        rides the unit's own squash and reaches the target branch only with the
        code it describes — never before: ``next`` unblocks a done ticket's
        dependents. A shared store (T2–T4, T6) is written after the code lands
        instead (:meth:`_latch_post_landing_writes`), so nothing is written here.

        * A park (``task.operator_actions`` latched): the session's
          ``awaiting-operator`` plan becomes ``mark <ref> blocked --blocked
          "awaiting operator: <actions>"`` (reason ``park``), whatever
          ``mark_done`` says — never ``done``: the human still owes the actions.
        * Otherwise the orchestrator's acceptance, under ``[tickets] mark_done =
          "auto"``: ``mark <ref> done`` (reason ``accept``). Nothing is written
          under ``"never"`` (the plan stays ``built`` for a person to accept).

        Nothing is written for an unbound task, or for a plan already at the
        target (a no-op mark). A failed mark puts back whatever it may have
        written, then pauses at ``ticket-tree`` with the task still COMMITTING, so
        a resume re-drives the window and marks again.

        Returns the token :meth:`_undo_terminal_plan_write` takes: the in-memory
        pre-image (:class:`_PlanUndo`) when this window wrote the mark; a
        :class:`_PlanLeftMarked` when the mark is a no-op on a plan marked since
        the COMMITTING save (e.g. by an interrupted earlier window) — that save's
        latch (``ticket_premark_status`` / ``ticket_premark_digest``, from
        :meth:`_latch_post_landing_writes`) read another status, and the plan's
        bytes no longer match its digest — so a commit failure names the plan
        left marked instead of reading as a plain commit failure; else None (a
        plan at the target before the window, a legacy state with no latch)."""
        plan = self._bound_plan(task)
        ticket = task.ticket
        if plan is None or ticket is None or self.paths.shared_store is not None:
            return None
        parked = bool(task.operator_actions)
        if not parked and self.policy.tickets.mark_done != "auto":
            return None
        try:
            preimage = plan.read_bytes() if plan.is_file() else None
        except OSError as e:
            self._pause_mark_done(task, f"cannot read {plan}: {e}", parked=parked)
        undo = _PlanUndo(plan, preimage)
        tree = self._workspace_ticket_tree()
        try:
            if parked:
                mark = tree.mark(
                    ticket.ref,
                    "blocked",
                    reason="park",
                    expected_plan=plan,
                    blocked=operator_park_reason(task.operator_actions),
                    journal=self.journal,
                )
            else:
                mark = tree.mark(
                    ticket.ref, "done", reason="accept", expected_plan=plan, journal=self.journal
                )
        except TicketsError as e:
            detail = f"tickets.py {e.kind}: {e}"
            if _bytes_or_none(plan) != preimage:
                if unrestored := self._undo_terminal_plan_write(task, undo):
                    detail = f"{detail}; {unrestored}"
            self._pause_mark_done(task, detail, parked=parked)
        if mark.outcome == "written":
            return undo
        target = "blocked" if parked else verify.PLAN_DONE
        before = task.ticket_premark_status
        digest = task.ticket_premark_digest
        if (
            digest
            and before != target
            and preimage is not None
            and hashlib.sha256(preimage).hexdigest() != digest
        ):
            return _PlanLeftMarked(plan, target, before)
        return None

    def _undo_terminal_plan_write(self, task: StoryTask, undo: object | None) -> str | None:
        """Put the plan back to its pre-mark bytes through
        :meth:`~bmad_loop.tickettree.TicketTree.restore_preimage`, which journals
        ``ticket-plan-restored`` with its outcome either way. A failed restore is
        never a raise out of the commit window's failure arm: it is returned, so
        the escalation (or pause) names the plan left marked for a commit that
        does not exist instead of reading as a plain commit failure.

        A :class:`_PlanLeftMarked` token restores nothing — no pre-image bytes
        survived whatever marked the plan since the COMMITTING save — and returns
        the detail that names the plan and asks the operator to restore it by
        hand."""
        del task
        if isinstance(undo, _PlanLeftMarked):
            return (
                f"{undo.plan} reads {undo.target}, not the {undo.before!r} the COMMITTING "
                "save latched — marked since, e.g. by an interrupted commit window — and "
                f"is left {undo.target} for a commit that does not exist; restore it by "
                "hand before resuming"
            )
        if not isinstance(undo, _PlanUndo):
            return None
        try:
            self._workspace_ticket_tree().restore_preimage(undo.plan, undo.data, self.journal)
        except (TicketsError, OSError) as e:
            return (
                f"{undo.plan} could not be put back to its pre-mark bytes ({e}) and may "
                "still carry the mark; restore it before resuming"
            )
        return None

    def _pause_mark_done(self, task: StoryTask, detail: str, *, parked: bool = False) -> NoReturn:
        what = "parking" if parked else "marking"
        target = "blocked" if parked else "done"
        reason = (
            f"{what} {task.story_key} {target} failed ({detail}) — the unit is verified "
            f"but not committed; fix the ticket tree, then {self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, f"mark {target} failed: {task.story_key}", reason)
        raise RunPaused(reason, PAUSE_TICKET_TREE, task.story_key)

    # ------------------------------------------- shared store: after landing

    def _latch_post_landing_writes(self, task: StoryTask) -> None:
        """For a shared store (T2–T4, T6: no code checkout carries the plan, so the
        unit's commit cannot), latch the ticket's terminal mark on ``task`` in the
        COMMITTING save — the same choice :meth:`_terminal_plan_write` makes for
        T1: a park's ``blocked`` reason (``ticket_park_pending``), else ``done``
        under ``mark_done = "auto"`` (``ticket_done_pending``), else no mark. Under
        ``"never"`` a store with its own repository (T3/T4) still latches the
        commit of the ``built`` plan the session wrote there
        (``ticket_commit_pending``), which a T1 unit's own commit carries. All
        three fields are assigned every time, so a re-armed unit never carries a
        stale latch into its next commit. Journals ``ticket-mark-latched``, and
        ``store-unversioned`` (once per run) for a store without a repository
        (T2/T6) — here, not only when a mark lands, since under ``"never"`` it
        latches nothing and the session's ``built`` plan lives on disk alone.

        For a T1 store (tracked in the code checkout) the window marks the plan
        itself; what is latched instead is the bound plan's pre-window status and
        the sha256 of its bytes (``ticket_premark_status`` /
        ``ticket_premark_digest``), so a resumed window can tell a plan marked
        since that save (e.g. by an interrupted window) from one already at the
        target (:meth:`_terminal_plan_write`). Both are assigned every time too —
        cleared for a shared store, an unbound task, no mark owed (``"never"``,
        not parked), or a plan that cannot be read (the window's own read or mark
        pauses on that fault)."""
        task.ticket_premark_status = ""
        task.ticket_premark_digest = ""
        if self.paths.shared_store is None:
            self._latch_premark(task)
            return
        if task.ticket is None:
            return
        parked = bool(task.operator_actions)
        accept = not parked and self.policy.tickets.mark_done == "auto"
        topology = self.store_topology
        versioned = topology is not None and topology.store_repo is not None
        if topology is not None and not versioned:
            self._note_store_unversioned(topology)
        task.ticket_park_pending = operator_park_reason(task.operator_actions) if parked else ""
        task.ticket_done_pending = accept
        task.ticket_commit_pending = not parked and not accept and versioned
        self._journal_latch(task)

    def _latch_premark(self, task: StoryTask) -> None:
        """Set the T1 pre-window latch (:meth:`_latch_post_landing_writes`) when
        the window will mark the bound plan; leave it cleared otherwise."""
        plan = self._bound_plan(task)
        if plan is None or task.ticket is None:
            return
        if not task.operator_actions and self.policy.tickets.mark_done != "auto":
            return
        try:
            data = plan.read_bytes()
            status = status_of(parse_frontmatter(data.decode("utf-8-sig")))
        except (OSError, UnicodeDecodeError):
            return
        task.ticket_premark_status = status
        task.ticket_premark_digest = hashlib.sha256(data).hexdigest()

    def _journal_latch(self, task: StoryTask) -> None:
        target = self._post_landing_target(task)
        if target:
            self.journal.append("ticket-mark-latched", story_key=task.story_key, target=target)

    @staticmethod
    def _post_landing_target(task: StoryTask) -> str:
        if task.ticket_park_pending:
            return "blocked"
        if task.ticket_done_pending:
            return verify.PLAN_DONE
        return verify.PLAN_BUILT if task.ticket_commit_pending else ""

    def _land_post_landing_writes(self, task: StoryTask) -> None:
        """Make the latched mark now that the unit's code is on the target: on the
        run's own tree (the main checkout's view of the shared store) and the plan
        ``find`` named at pick time — ``mark <ref> blocked --blocked <reason>``
        (reason ``park``) or ``mark <ref> done`` (reason ``accept``), no mark for a
        commit-only latch (``built``) — then commit the plan in the store's own
        repository, clear the latch and save. A replay after a crash past the mark
        is the chokepoint's no-op. A mark that fails is a repair write: it raises
        ``RunPaused`` at ``ticket-tree`` with the latch kept, so the resume marks
        again (:meth:`_replay_post_landing_writes`)."""
        target = self._post_landing_target(task)
        if not target:
            return
        ticket = task.ticket
        if ticket is None:
            self._pause_post_landing_mark(task, target, "the unit has no ticket binding")
        plan = self._recorded_plan(ticket)
        try:
            if target == verify.PLAN_BUILT:
                pass  # commit-only: the session's own `built` write is the change
            elif task.ticket_park_pending:
                self.ticket_tree.mark(
                    ticket.ref,
                    "blocked",
                    reason="park",
                    expected_plan=plan,
                    blocked=task.ticket_park_pending,
                    journal=self.journal,
                )
            else:
                self.ticket_tree.mark(
                    ticket.ref, "done", reason="accept", expected_plan=plan, journal=self.journal
                )
        except TicketsError as e:
            self._pause_post_landing_mark(task, target, f"tickets.py {e.kind}: {e}")
        if not self._commit_marked_plan(task, target):
            return  # latch kept: a resume marks (a no-op) and commits again
        task.ticket_done_pending = False
        task.ticket_park_pending = ""
        task.ticket_commit_pending = False
        self._save()

    def _commit_marked_plan(self, task: StoryTask, target: str) -> bool:
        """Commit the plan just marked ``target`` in a store with its own
        repository (T3/T4) — with the deferred-work ledger when the task wrote to
        it — through :class:`~bmad_loop.ticketstore.StoreCommitter`, as ``chore(
        tickets): <ref> <target> [bmad-loop <run>]`` with the code trailers. An
        unversioned store (T2/T6) has nothing to commit and journals
        ``store-unversioned`` once per run.

        Bookkeeping, never a pause: the mark on disk is what ``next`` reads. A
        failed commit journals ``store-commit-failed``, a staged foreign version
        of a path ``store-commit-foreign-dirt``; both return False so the caller
        keeps the latch and a resume commits again."""
        topology = self.store_topology
        ticket = task.ticket
        if topology is None or topology.per_worktree() or ticket is None:
            return True
        if topology.store_repo is None:
            self._note_store_unversioned(topology)
            return True
        paths = [self._recorded_plan(ticket)]
        if task.harvested_deferrals or task.story_closes_intended:
            paths.append(self.paths.deferred_work)
        return self._store_commit(
            topology.store_repo,
            task.story_key,
            paths,
            f"chore(tickets): {ticket.ref} {target} [bmad-loop {self.state.run_id}]",
            code_commit=self._landed_code_commit(task),
            code_baseline=task.baseline_commit,
        )

    def _store_commit(
        self,
        store_repo: Path,
        key: str,
        paths: list[Path],
        message: str,
        *,
        code_commit: str | None,
        code_baseline: str | None = None,
    ) -> bool:
        """One :class:`~bmad_loop.ticketstore.StoreCommitter` commit, journaled
        under ``key`` (``store-committed``, ``store-commit-foreign-dirt`` or
        ``store-commit-failed``); True when nothing is left owed."""
        committer = ticketstore.StoreCommitter(store_repo, self.paths.repo_root)
        try:
            result = committer.commit(
                key, paths, message, code_commit=code_commit, code_baseline=code_baseline
            )
        except ticketstore.StoreCommitError as e:
            self.journal.append("store-commit-failed", story_key=key, error=str(e))
            return False
        if result.foreign:
            self.journal.append(
                "store-commit-foreign-dirt", story_key=key, count=len(result.foreign)
            )
            return False
        self.journal.append("store-committed", story_key=key, commit=result.sha)
        return True

    def _note_store_unversioned(self, topology: ticketstore.StoreTopology) -> None:
        """Journal ``store-unversioned`` once per run: an unversioned store's marks
        live on disk only, with no history to recover them from."""
        if self._unversioned_noted is None:
            self._unversioned_noted = any(
                e.get("kind") == "store-unversioned" for e in self.journal.entries()
            )
        if not self._unversioned_noted:
            self.journal.append("store-unversioned", layout=topology.layout)
            self._unversioned_noted = True

    def _settle_retro_tree(
        self, epic: int, ok: bool, docs: list[Path], *, epic_ref: str | None = None
    ) -> None:
        """A store with its own repository (T3/T4) commits the retro document
        there first (``chore(retro): <slug> retrospective``). The code checkout is
        then settled as the base does; the store repository's own dirt never pauses
        the run — humans edit planning beside it. Journaled under the retro
        session's key, ``<slug>-retrospective``.

        A store commit that fails (``store-commit-failed`` /
        ``store-commit-foreign-dirt``) pauses at ``epic-boundary`` before a closure
        is recorded: nothing else would retry it, so the resume re-enters the
        boundary and ``already-done`` commits the document again
        (:meth:`_retro_doc_uncommitted` reads the store repository too)."""
        topology = self.store_topology
        if ok and docs and topology is not None and topology.store_repo is not None:
            label = epic_ref or f"epic {epic}"
            try:
                head: str | None = verify.rev_parse_head(self.paths.repo_root)
            except verify.GitError:
                head = None  # the trailer is provenance; the commit still lands
            if not self._store_commit(
                topology.store_repo,
                f"{label}-retrospective",
                list(docs),
                f"chore(retro): {label} retrospective [bmad-loop {self.state.run_id}]",
                code_commit=head,
            ):
                reason = (
                    f"{label} boundary — its retrospective is not committed in "
                    f"{topology.store_repo} (see the journal's store-commit-failed / "
                    "store-commit-foreign-dirt rows); commit or unstage the store's own "
                    f"changes to it, then {self._resume_hint()}"
                )
                gates.notify(self.policy, self.run_dir, "retro store commit owed", reason)
                raise RunPaused(reason, PAUSE_EPIC_BOUNDARY, label)
        super()._settle_retro_tree(epic, ok, docs, epic_ref=epic_ref)

    def _replay_post_landing_writes(self) -> None:
        """On resume, make every latched mark whose code had landed when the host
        stopped: a DONE or AWAITING_OPERATOR unit, in place (its commit is on the
        target) or merged back (``isolated_ledger_carried``: the merge-back
        returned; a terminal unit whose merge-back was lost has just been merged by
        :meth:`~bmad_loop.engine.Engine._replay_unlatched_ledger_carries`, which
        marks it there). A unit still in flight marks after its own landing.
        Journals ``resume-ticket-mark`` per replayed mark."""
        for task in list(self.state.tasks.values()):
            if target := self._landed_mark_owed(task):
                self.journal.append("resume-ticket-mark", story_key=task.story_key, target=target)
                self._land_post_landing_writes(task)

    def _landed_mark_owed(self, task: StoryTask) -> str:
        """The latched mark ``task`` still owes although its code has landed — a
        DONE or AWAITING_OPERATOR unit, in place or merged back — or ""."""
        target = self._post_landing_target(task)
        if not target or task.phase not in (Phase.DONE, Phase.AWAITING_OPERATOR):
            return ""
        if task.worktree_path and not task.isolated_ledger_carried:
            return ""
        return target

    def _settle_owed_store_commits(self) -> None:
        """Retry, before the run can finish, every mark and store commit a landed
        unit still owes. A store commit that fails mid-run keeps its latch and the
        run goes on, but a run recorded ``finished`` refuses ``resume``, so nothing
        would retry it after. Journals ``run-end-ticket-mark`` per retried mark; one
        still owed pauses at ``ticket-tree`` (``store-commit-owed``), so the run stays
        resumable and the resume retries it (:meth:`_replay_post_landing_writes`);
        ``store-commit-owed`` is journaled per unit still owing."""
        for task in list(self.state.tasks.values()):
            if target := self._landed_mark_owed(task):
                self.journal.append("run-end-ticket-mark", story_key=task.story_key, target=target)
                self._land_post_landing_writes(task)
        owed = [t.story_key for t in self.state.tasks.values() if self._landed_mark_owed(t)]
        if not owed:
            return
        topology = self.store_topology
        repo = topology.store_repo if topology is not None else None
        for key in owed:
            self.journal.append("store-commit-owed", story_key=key)
        reason = (
            f"the store commit of {', '.join(owed)} is still owed in {repo} (see the "
            "journal's store-commit-failed / store-commit-foreign-dirt rows) — commit or "
            f"unstage the store's own changes to those paths, then {self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, "store commit owed", reason)
        raise RunPaused(reason, PAUSE_TICKET_TREE, owed[0])

    def _pause_post_landing_mark(self, task: StoryTask, target: str, detail: str) -> NoReturn:
        what = "parking" if target == "blocked" else "marking"
        reason = (
            f"{what} {task.story_key} {target} failed after its code landed ({detail}) — "
            f"the mark is still owed; fix the ticket tree, then {self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, f"mark {target} failed: {task.story_key}", reason)
        raise RunPaused(reason, PAUSE_TICKET_TREE, task.story_key)

    def _apply_adoption(self, task: StoryTask) -> None:
        """An adopted kept branch (``resolve --adopt-branch``) is the orchestrator's
        acceptance. For a shared store the plan is never written before the code
        lands, so the acceptance is latched (``ticket_done_pending``) and made after
        the merge-back like a commit's; an adoption that owes operator actions is
        refused as it is for T1 (the park conversion owns those). A per-checkout
        store keeps the base adoption, which marks the mount's plan in the window."""
        if self.paths.shared_store is None or task.ticket is None:
            super()._apply_adoption(task)
            return
        if task.operator_actions:
            self._escalate(
                task,
                f"cannot adopt {task.story_key}: it owes operator actions, and an "
                "adoption only accepts a ticket",
            )
            return
        task.ticket_park_pending = ""
        task.ticket_done_pending = True
        task.ticket_commit_pending = False
        self._journal_latch(task)
        self._save()

    def _park_record_fields(self, task: StoryTask) -> dict[str, str]:
        """A ticket park's record names its source and ref, so ``bmad-loop
        confirm`` marks the plan through the ticket chokepoint instead of reading a
        sprint board."""
        ticket = task.ticket
        return {"source": SOURCE_TICKETS, "ref": ticket.ref if ticket else task.story_key}

    def _park_review_demotion(self, task: StoryTask, result: SessionResult) -> VerifyOutcome:
        """A review that demoted the ticket to ``awaiting-operator`` (``[operator]
        on_review_demotion = "park"``): there is no sprint board to regress, so
        this is the base gate and latch alone — ``verify_review_tickets`` holds
        the park to its pair and a non-empty action list — and the latched
        actions turn the commit window's plan write into the same ``blocked``
        park a dev park gets (:meth:`_terminal_plan_write`)."""
        return self._gate_review_demotion(task, result)

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

    # ------------------------------------------------------------- rollback

    def _spec_confine_root(self, task: StoryTask) -> Path:
        """A bound ticket's plan is restored only inside the store root (the
        active workspace's ``output_folder``, where ``tickets.py`` keeps every
        initiative); an unbound task keeps the base root."""
        if task.ticket is None:
            return super()._spec_confine_root(task)
        return self.workspace.paths.output_folder

    def _owned_plan_writer(self) -> OwnedPlanWriter | None:
        return _TicketPlanWriter(self)

    def _route_plan(self, task: StoryTask, target: str) -> None:
        """Re-route a rolled-back plan to ``target`` through ``mark``: reason
        ``rollback`` to ``ready-for-dev``, ``rearm`` to a re-drive's
        ``in-progress``/``in-review``. A plan already there is left alone; any
        other target, an absent or unreadable plan, or a refused mark raises
        :class:`~bmad_loop.recovery_flow.OwnedPlanRefused`."""
        plan = self._bound_plan(task)
        ticket = task.ticket
        if plan is None or ticket is None:
            raise OwnedPlanRefused(f"{task.story_key} has no ticket binding")
        try:
            state = _plan_state(plan)
        except (OSError, UnicodeDecodeError) as e:
            raise OwnedPlanRefused(f"cannot read {plan}: {e}") from e
        if state is None:
            raise OwnedPlanRefused(f"{plan} is absent")
        status, blocked_reason, _ = state
        if status == target and not blocked_reason:
            return
        reason = _ROLLBACK_ROUTE_REASONS.get(target)
        if reason is None:
            raise OwnedPlanRefused(f"no plan-transition reason routes a plan to {target!r}")
        try:
            self._workspace_ticket_tree().mark(
                ticket.ref, target, reason=reason, expected_plan=plan, journal=self.journal
            )
        except TicketsError as e:
            raise OwnedPlanRefused(f"tickets.py {e.kind}: {e}") from e

    def _after_code_rollback(self, task: StoryTask) -> None:
        """Re-arm the bound plan once a rollback returned. The reset keeps the
        artifact folders' untracked files, so a plan the attempt created and
        carried to ``in-progress``, ``in-review`` or ``built`` survives it (a
        tracked plan goes back to its baseline bytes, or its pre-launch snapshot
        through the recovery flow), and would otherwise send the next dispatch to
        resume or review against code that is gone. A plan holding only
        frontmatter is deleted (``discard_unbuilt_plan``), as upstream's
        unattended-builds contract asks; any other is marked ``ready-for-dev``
        (reason ``rollback``). Either way the plan moves only through the ticket
        chokepoint — never by a reset, a stash or a preserve ref of its own.

        Nothing is done for an unbound task, an absent plan, a plan still at or
        before ``ready-for-dev`` (or parked, done, dropped), or a re-drive that
        restores its change (``restore_patch``: the recovery flow routes that
        plan to ``in-review`` itself). A plan that cannot be read or re-armed
        pauses at ``ticket-tree``; a resume rolls back and re-arms again."""
        plan = self._bound_plan(task)
        ticket = task.ticket
        if plan is None or ticket is None or task.restore_patch:
            return
        try:
            state = _plan_state(plan)
        except (OSError, UnicodeDecodeError) as e:
            self._pause_rearm(task, f"cannot read {plan}: {e}")
        if state is None or state[0] not in _REARMED_AFTER_ROLLBACK:
            return
        tree = self._workspace_ticket_tree()
        try:
            if is_frontmatter_only(state[2]):
                tree.discard_unbuilt_plan(ticket.ref, plan, self.journal)
            else:
                tree.mark(
                    ticket.ref,
                    "ready-for-dev",
                    reason="rollback",
                    expected_plan=plan,
                    journal=self.journal,
                )
        except TicketsError as e:
            self._pause_rearm(task, f"tickets.py {e.kind}: {e}")

    def _pause_rearm(self, task: StoryTask, detail: str) -> NoReturn:
        reason = (
            f"re-arming {task.story_key}'s plan after its rollback failed ({detail}) — "
            f"the code is back at the attempt baseline but the plan is not; fix the "
            f"ticket tree, then {self._resume_hint()}"
        )
        gates.notify(self.policy, self.run_dir, f"plan re-arm failed: {task.story_key}", reason)
        raise RunPaused(reason, PAUSE_TICKET_TREE, task.story_key)
