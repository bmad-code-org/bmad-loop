"""`TicketsEngine`: the tickets-mode pick and its pick-time pauses.

`compose_run` still refuses tickets runs, so every test builds the engine
directly (`make_engine`) on the `project_v7` sandbox with the mock adapter and an
injected `FakeTicketTree`. Real-spawn budget: none.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeTicketTree, V7Layout, ticket_row

from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import load_paths
from bmad_loop.engine import Engine, RunPaused
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_TICKET_HITL,
    PAUSE_TICKET_REFINE,
    PAUSE_TICKET_TREE,
    Phase,
    RunState,
    StoryTask,
)
from bmad_loop.policy import GatesPolicy, NotifyPolicy, Policy, ReviewPolicy
from bmad_loop.tickets_engine import TicketsEngine
from bmad_loop.tickets_source import TicketRef
from bmad_loop.tickettree import TicketsCrashed

QUIET = NotifyPolicy(desktop=False, file=True)


def _policy() -> Policy:
    return Policy(gates=GatesPolicy(mode="none"), notify=QUIET, review=ReviewPolicy(enabled=False))


def make_engine(layout: V7Layout, tree: FakeTicketTree, **state_fields):
    """A TicketsEngine over ``layout`` reading ``tree``, pinned to the layout's
    initiative as `runsetup.build_run_state` pins a tickets run."""
    paths = load_paths(layout.project, want_tickets=True)
    run_dir = layout.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter([])
    state = RunState(
        run_id="test-run",
        project=str(layout.project),
        started_at="now",
        initiative=layout.initiative,
        initiative_folder=str(layout.initiative_folder),
        **state_fields,
    )
    built: list[tuple[Path, str | None]] = []

    def factory(root: Path, pin: str | None) -> FakeTicketTree:
        built.append((root, pin))
        return tree

    engine = TicketsEngine(
        paths=paths,
        policy=_policy(),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        ticket_tree_factory=factory,
    )
    engine.tree_builds = built  # type: ignore[attr-defined]
    return engine, adapter


def _kinds(engine: TicketsEngine, kind: str) -> list[dict]:
    return [e for e in engine.journal.entries() if e.get("kind") == kind]


def _tree(layout: V7Layout, **groups) -> FakeTicketTree:
    return FakeTicketTree.from_rows(layout.project, initiative=layout.initiative, **groups)


def test_engine_pins_source_and_builds_the_tree_from_the_pin(project_v7):
    """The engine records `source = "tickets"` and builds its tree once, from the
    main checkout and the run's pinned initiative."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)

    engine._pick_next()
    engine._pick_next()

    assert engine.state.source == "tickets"
    assert engine.tree_builds == [(project_v7.project, project_v7.initiative)]


def test_hitl_ticket_pauses_without_registering(project_v7):
    """A hitl ticket at the head of the build order pauses at `ticket-hitl` before
    anything is recorded, so a resume re-reads the tree; no session runs and
    nothing is marked. Ablation (performed): register the task before the hitl
    pause raises and the `tasks == {}` assertion FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", hitl=True), ticket_row("1.2")))
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert (persisted.paused_stage, persisted.paused_story_key) == (PAUSE_TICKET_HITL, "1.1")
    assert persisted.tasks == {}
    assert [e["ref"] for e in _kinds(engine, "ticket-hitl-pause")] == ["1.1"]
    assert adapter.sessions == [] and tree.marks == []


def test_refine_ticket_pauses(project_v7):
    """A ticket that needs refining (a bug, here) pauses at `ticket-refine`,
    unregistered."""
    tree = _tree(project_v7, ready_to_refine=(ticket_row("1.1", type="bug", refine=True),))
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert (persisted.paused_stage, persisted.paused_story_key) == (PAUSE_TICKET_REFINE, "1.1")
    assert persisted.tasks == {}
    assert [e["ref"] for e in _kinds(engine, "ticket-refine-pause")] == ["1.1"]
    assert adapter.sessions == []


def test_refined_plan_checkpoint_pauses_before_dispatch(project_v7):
    """A refined `plan_checkpoint` ticket pauses at `plan-checkpoint` before any
    dispatch, its reason naming the story file to approve; once the ref is in the
    run's approvals the same pick dispatches it. Ablation (performed): drop the
    approvals test from `decide_pick` and the second pick FAILS."""
    row = ticket_row("1.1", refined=True, plan_checkpoint=True, file="story-cart-model.md")
    tree = _tree(project_v7, ready_to_start=(row,))
    story_file = tree.finds["1.1"].story_file
    assert story_file is not None
    engine, adapter = make_engine(project_v7, tree)

    with pytest.raises(RunPaused) as paused:
        engine._pick_next()

    assert paused.value.stage == PAUSE_PLAN_CHECKPOINT
    assert paused.value.story_key == "1.1"
    assert paused.value.reason.startswith(f"approve story file {story_file} ")
    assert engine.state.tasks == {} and adapter.sessions == []
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert (halt["ref"], halt["cause"], halt["stage"]) == (
        "1.1",
        "plan-checkpoint",
        "plan-checkpoint",
    )

    engine.state.ticket_story_approvals.append("1.1")
    picked = engine._pick_next()

    assert isinstance(picked, TicketRef) and picked.key == "1.1"


def test_blocked_plan_wedges_escalation(project_v7):
    """A blocked plan this run did not park records an ESCALATED task at attempt 0
    — carrying the plan as its spec and the ticket binding — and pauses at
    `escalation`, the shape `resolve` acts on. Fixed by hand, the next pick re-reads
    the tree and dispatches it: the re-pause guard holds only in-run escalations."""
    row = ticket_row("1.1", status="blocked", blocked_reason="tests red", blocked_at="2026-10-14")
    tree = _tree(project_v7, blocked=(row,))
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert (persisted.paused_stage, persisted.paused_story_key) == (PAUSE_ESCALATION, "1.1")
    task = persisted.tasks["1.1"]
    assert (task.phase, task.attempt, task.epic) == (Phase.ESCALATED, 0, 0)
    assert task.spec_file == tree.finds["1.1"].plan
    assert task.ticket is not None and task.ticket.plan == tree.finds["1.1"].plan
    [wedged] = _kinds(engine, "stories-wedged")
    assert (wedged["story_key"], wedged["state_kind"]) == ("1.1", "blocked-plan")
    assert adapter.sessions == [] and tree.marks == []

    # The operator re-arms the plan by hand: the pick re-reads it and dispatches.
    tree.next_view = _tree(project_v7, ready_to_start=(ticket_row("1.1"),)).next_view
    picked = engine._pick_next()
    assert isinstance(picked, TicketRef) and picked.key == "1.1"


def test_inrun_escalation_repauses_before_the_pick(project_v7):
    """An escalation a session raised (attempt > 0) re-pauses before the tree is
    read, never re-derived from it. Ablation (performed): drop the
    `_repause_inrun_escalation` call and the pick dispatches 1.1 instead."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = StoryTask("1.1", 0)
    task.phase = Phase.ESCALATED
    task.attempt = 1
    engine.state.tasks["1.1"] = task

    with pytest.raises(RunPaused) as paused:
        engine._pick_next()

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_ESCALATION, "1.1")
    assert tree.calls == []


def test_pin_mismatch_pauses_ticket_tree(project_v7):
    """The operator switched the active initiative under the run: every tree read
    refuses before spawning, and the run pauses at `ticket-tree` with no ticket
    recorded. Ablation (performed): map a pin mismatch to an exhausted pick and
    the run FINISHES instead."""
    tree = FakeTicketTree.from_rows(
        project_v7.project,
        initiative=project_v7.initiative,
        ready_to_start=(ticket_row("1.1"),),
        pinned_initiative=project_v7.initiative,
    )
    tree.live_initiative = "another-initiative"
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert persisted.paused_stage == PAUSE_TICKET_TREE
    assert persisted.paused_story_key is None
    assert "pin-mismatch" in (persisted.paused_reason or "")
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert (halt["cause"], halt["ref"]) == ("pin-mismatch", None)
    assert persisted.tasks == {} and adapter.sessions == [] and tree.calls == []


def test_find_fault_at_dispatch_pauses_unregistered(project_v7):
    """The picked ticket's binding comes from `find` before `_loop` registers it,
    so a `find` fault pauses at `ticket-tree` with nothing recorded."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    tree.faults["find"] = TicketsCrashed("tickets.py died")
    engine, _ = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert (persisted.paused_stage, persisted.paused_story_key) == (PAUSE_TICKET_TREE, "1.1")
    assert persisted.tasks == {}


def test_dispatch_binds_the_ticket_on_registration(project_v7, monkeypatch):
    """The binding `find` resolved at pick time lands on the task `_loop`
    registers, before any session, and survives the state round-trip."""
    row = ticket_row("2.1", epic="epic-checkout", risk="high", done_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    monkeypatch.setattr(Engine, "_run_story", lambda self, task: None)

    picked = engine._pick_next()
    assert isinstance(picked, TicketRef)
    task = StoryTask(picked.key, picked.epic)
    engine.state.tasks[picked.key] = task
    engine._run_story(task)

    binding = load_state(engine.run_dir).tasks["2.1"].ticket
    assert binding is not None
    assert (binding.ref, binding.epic_slug, binding.epic_id, binding.risk) == (
        "2.1",
        "epic-checkout",
        "2",
        "high",
    )
    assert binding.plan == tree.finds["2.1"].plan
    assert binding.done_checkpoint is True


def test_passed_over_rows_journal_once(project_v7):
    """A row the pick passes over is journaled once per process, however many
    picks pass it again: a foreign built ticket and one this run deferred."""
    tree = _tree(
        project_v7,
        ready_to_start=(ticket_row("1.1", status="ready-for-dev"), ticket_row("1.3")),
        in_progress=(ticket_row("1.2", status="built"),),
    )
    engine, _ = make_engine(project_v7, tree)
    deferred = StoryTask("1.1", 0)
    deferred.phase = Phase.DEFERRED
    engine.state.tasks["1.1"] = deferred

    for _ in range(3):
        assert engine._pick_next().key == "1.3"

    assert [(e["ref"], e["status"]) for e in _kinds(engine, "ticket-foreign-active")] == [
        ("1.2", "built")
    ]
    assert [(e["ref"], e["skip"], e["phase"]) for e in _kinds(engine, "ticket-skipped")] == [
        ("1.1", "touched", "deferred")
    ]


def test_run_behind_parked_tickets_finishes_with_a_summary(project_v7):
    """Open tickets that wait only behind a ticket this run parked end the run —
    finished, not paused — and each is journaled as skipped behind the park."""
    parked = ticket_row("1.1", status="blocked", blocked_reason="awaiting operator: buy a domain")
    tree = _tree(project_v7, blocked=(parked, ticket_row("1.2", waiting_on=("1",))))
    engine, adapter = make_engine(project_v7, tree)
    task = StoryTask("1.1", 0)
    task.phase = Phase.AWAITING_OPERATOR
    engine.state.tasks["1.1"] = task

    summary = engine.run()

    assert not summary.paused
    assert load_state(engine.run_dir).finished
    assert [(e["ref"], e["skip"]) for e in _kinds(engine, "ticket-skipped")] == [
        ("1.2", "behind-parked")
    ]
    assert adapter.sessions == []
