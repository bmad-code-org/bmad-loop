"""`TicketsEngine`: the tickets-mode pick, its pick-time pauses, and the
dispatch, verify and review path of a picked ticket.

Every test builds the engine directly (`make_engine`) on the `project_v7`
sandbox with the mock adapter and an injected `FakeTicketTree`
(`compose_run`'s own wiring is pinned in tests/test_runsetup.py). Real-spawn
budget: none, except the stale-binding move test, which runs the vendored
`tickets.py` under a real tree (its section comment counts the spawns).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TREE,
    EntrySpec,
    EpicSpec,
    FakeTicketTree,
    V7Layout,
    git,
    run_tickets,
    ticket_row,
    write_ticket_tree,
)

from bmad_loop import runs, tickets_engine, tickets_source, verify
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import load_paths
from bmad_loop.engine import Engine, RunPaused, SpecStatusWriteRefused
from bmad_loop.frontmatter import parse_frontmatter, status_of
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_STORY_CHECKPOINT,
    PAUSE_STORY_GATE,
    PAUSE_TICKET_HITL,
    PAUSE_TICKET_REFINE,
    PAUSE_TICKET_TREE,
    Phase,
    RunState,
    StoryTask,
)
from bmad_loop.policy import (
    GatesPolicy,
    NotifyPolicy,
    OperatorPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    TicketsPolicy,
)
from bmad_loop.ticketmodel import EpicRow
from bmad_loop.tickets_engine import TicketsEngine
from bmad_loop.tickets_source import TicketRef, ticket_binding
from bmad_loop.tickettree import (
    TicketsCrashed,
    TicketsLockBusy,
    TicketsParentInitiative,
    TicketsPlanIOError,
    TicketTree,
    V6KeyClaim,
)
from bmad_loop.workspace import Workspace

QUIET = NotifyPolicy(desktop=False, file=True)


def _policy() -> Policy:
    return Policy(gates=GatesPolicy(mode="none"), notify=QUIET, review=ReviewPolicy(enabled=False))


def make_engine(
    layout: V7Layout,
    tree: FakeTicketTree,
    *,
    policy: Policy | None = None,
    script: list | None = None,
    **state_fields,
):
    """A TicketsEngine over ``layout`` reading ``tree``, pinned to the layout's
    initiative as `runsetup.build_run_state` pins a tickets run."""
    paths = load_paths(layout.project, want_tickets=True)
    run_dir = layout.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(list(script or []))
    state = RunState(
        run_id="test-run",
        project=str(layout.project),
        started_at="now",
        initiative=layout.initiative,
        initiative_folder=str(layout.initiative_folder),
        **state_fields,
    )
    built: list[tuple[Path, str | None]] = []

    def factory(root: Path, pin: str | None, *, folder: Path | None = None) -> FakeTicketTree:
        built.append((root, pin))
        return tree

    engine = TicketsEngine(
        paths=paths,
        policy=policy or _policy(),
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


@pytest.mark.parametrize("chained", [False, True])
def test_chained_trees_pin_the_parent_in_place_and_the_child_in_a_mount(
    project_v7, tmp_path, chained
):
    """A chained run (one child initiative of the parent the live config names)
    builds its main tree pinned to the parent and routed through the child's
    folder, and a unit mount's tree pinned to the child with no folder — the
    mount's config names the child. An unchained run pins its own initiative in
    both, with no folder. Ablation (performed): pin the main tree to
    `state.initiative` in `tree_pin` and the chained row FAILS."""
    parent, child = project_v7.initiative, "initiative-child"
    paths = load_paths(project_v7.project, want_tickets=True)
    if chained:
        paths = paths.with_initiative(child)
    run_dir = project_v7.project / ".bmad-loop" / "runs" / "test-run"
    state = RunState(
        run_id="test-run",
        project=str(project_v7.project),
        started_at="now",
        initiative=child if chained else parent,
        initiative_folder=str(paths.initiative_folder),
        parent_initiative=parent if chained else None,
    )
    tree = _tree(project_v7)
    built: list[tuple[Path, str | None, Path | None]] = []

    def factory(root, pin, folder=None):
        built.append((root, pin, folder))
        return tree

    engine = TicketsEngine(
        paths=paths,
        policy=_policy(),
        adapter=MockAdapter([]),
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        ticket_tree_factory=factory,
    )
    assert engine.ticket_tree is tree
    mount = (tmp_path / "mount").resolve()
    engine.workspace = Workspace(root=mount, paths=engine.paths.rebased(mount))
    engine._workspace_ticket_tree()

    if chained:
        assert built == [
            (project_v7.project, parent, paths.initiative_folder),
            (mount, child, None),
        ]
        assert paths.initiative_folder == project_v7.initiative_folder.parent / child
    else:
        assert built == [(project_v7.project, parent, None), (mount, parent, None)]


def test_chained_engine_refuses_parent_rooted_paths(project_v7):
    """A chained run's engine built on the parent's own paths (`load_paths`
    output a caller forgot to re-root with `runs.paths_for_state`) refuses at
    construction — loudly, before any ledger or tree I/O — instead of reading
    and marking the parent's folder as the child's. Ablation (performed): drop
    the `tree_pin` call in `TicketsEngine.__init__` and this FAILS."""
    run_dir = project_v7.project / ".bmad-loop" / "runs" / "test-run"
    state = RunState(
        run_id="test-run",
        project=str(project_v7.project),
        started_at="now",
        initiative="initiative-child",
        parent_initiative=project_v7.initiative,
    )
    with pytest.raises(ValueError, match="re-root them with paths_for_state"):
        TicketsEngine(
            paths=load_paths(project_v7.project, want_tickets=True),
            policy=_policy(),
            adapter=MockAdapter([]),
            run_dir=run_dir,
            journal=Journal(run_dir),
            state=state,
            ticket_tree_factory=lambda *_a, **_k: pytest.fail("built a tree"),
        )


@pytest.mark.parametrize(("layout", "shared"), [("T1", False), ("T3", True)])
def test_engine_shares_the_store_the_run_recorded(project_v7, layout, shared):
    """A run that recorded a shared store (here T3, its own nested repository)
    builds its engine with `ProjectPaths.shared_store` set, so worktree mounts are
    redirected and every store-derived path stays put; a T1 store is per-checkout
    and sets nothing. Ablation (performed): drop the `with_shared_store` call in
    `TicketsEngine.__init__` and the T3 row FAILS."""
    store = str(project_v7.output_folder.resolve())
    topology = {
        "T1": {"placement": "checkout", "versioning": "code-repo", "store_repo": None},
        "T3": {"placement": "shared-in-project", "versioning": "store-repo", "store_repo": store},
    }[layout]
    tree = _tree(project_v7)
    engine, _ = make_engine(
        project_v7, tree, store_topology={"layout": layout, "store_root": store, **topology}
    )
    assert engine.paths.shared_store == (Path(store) if shared else None)
    assert engine.workspace.paths.shared_store == engine.paths.shared_store


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


def test_find_fault_on_refined_plan_checkpoint_pauses_at_ticket_tree(project_v7):
    """A `find` fault naming a refined plan checkpoint's story file pauses at
    `ticket-tree` naming the fault, not at `plan-checkpoint` with the ref standing
    in for a story file the operator cannot approve. Ablation (performed): fall
    back to the ref on the fault and this FAILS."""
    row = ticket_row("1.1", refined=True, plan_checkpoint=True, file="story-cart-model.md")
    tree = _tree(project_v7, ready_to_start=(row,))
    tree.faults["find"] = TicketsCrashed("tickets.py died")
    engine, adapter = make_engine(project_v7, tree)

    with pytest.raises(RunPaused) as paused:
        engine._pick_next()

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_TICKET_TREE, "1.1")
    assert "tickets.py died" in paused.value.reason
    assert "approve story file" not in paused.value.reason
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert (halt["cause"], halt["stage"]) == ("tree-fault", PAUSE_TICKET_TREE)
    assert engine.state.tasks == {} and adapter.sessions == []


def test_parent_initiative_pauses_with_its_children_named(project_v7):
    """An active initiative that is a parent answers `next`/`status` with its
    children (`TicketsParentInitiative`): the pick pauses at `ticket-tree` with
    the message naming them verbatim, and nothing is built."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    parent = TicketsParentInitiative(
        "next", "initiative-platform", ("initiative-auth", "initiative-billing")
    )
    tree.faults["next"] = tree.faults["status"] = parent
    engine, adapter = make_engine(project_v7, tree)

    with pytest.raises(RunPaused) as paused:
        engine._pick_next()

    assert paused.value.stage == PAUSE_TICKET_TREE
    assert paused.value.reason.startswith(
        "tickets.py parent-initiative: active initiative initiative-platform is a parent "
        "initiative (children: initiative-auth, initiative-billing); set "
        "`[core] active_initiative` to one child"
    )
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert (halt["cause"], halt["stage"]) == ("tree-fault", PAUSE_TICKET_TREE)
    assert engine.state.tasks == {} and adapter.sessions == []


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


def test_find_fault_on_blocked_plan_pauses_unregistered(project_v7):
    """A `find` fault while binding a blocked plan pauses at `ticket-tree` naming
    the fault, with no wedge task recorded: an unbound ESCALATED task would
    re-drive with no binding after resolve. Ablation (performed): degrade the
    fault to an unbound `_pause_wedged` and this FAILS."""
    row = ticket_row("1.1", status="blocked", blocked_reason="tests red", blocked_at="2026-10-14")
    tree = _tree(project_v7, blocked=(row,))
    tree.faults["find"] = TicketsCrashed("tickets.py died")
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    persisted = load_state(engine.run_dir)
    assert (persisted.paused_stage, persisted.paused_story_key) == (PAUSE_TICKET_TREE, "1.1")
    assert "tickets.py died" in (persisted.paused_reason or "")
    assert persisted.tasks == {}
    assert _kinds(engine, "stories-wedged") == []
    assert adapter.sessions == []


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


def test_unbound_registered_task_rebinds_from_the_tree(project_v7, monkeypatch):
    """A host that died between `_loop` saving the new task and the attach leaves
    a registered task with no binding and an empty `_picked`: `_run_story`
    re-resolves it from the tree before any session."""
    row = ticket_row("2.1", epic="epic-checkout", done_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, adapter = make_engine(project_v7, tree)
    monkeypatch.setattr(Engine, "_run_story", lambda self, task: None)
    task = StoryTask("2.1", 0)
    engine.state.tasks["2.1"] = task

    engine._run_story(task)

    binding = load_state(engine.run_dir).tasks["2.1"].ticket
    assert binding is not None
    assert (binding.ref, binding.epic_slug, binding.plan) == (
        "2.1",
        "epic-checkout",
        tree.finds["2.1"].plan,
    )
    assert binding.done_checkpoint is True
    assert [e["story_key"] for e in _kinds(engine, "ticket-rebound")] == ["2.1"]
    assert adapter.sessions == []


def test_find_fault_on_rebind_pauses_at_ticket_tree(project_v7, monkeypatch):
    """A `find` fault re-resolving an unbound task pauses at `ticket-tree`
    instead of driving it unbound; the task stays unbound for the resume."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    tree.faults["find"] = TicketsCrashed("tickets.py died")
    engine, adapter = make_engine(project_v7, tree)
    driven: list[str] = []
    monkeypatch.setattr(Engine, "_run_story", lambda self, task: driven.append(task.story_key))
    task = StoryTask("1.1", 0)
    engine.state.tasks["1.1"] = task

    with pytest.raises(RunPaused) as paused:
        engine._run_story(task)

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_TICKET_TREE, "1.1")
    assert task.ticket is None and driven == [] and adapter.sessions == []


def _gate_ledger(engine: TicketsEngine, layout: V7Layout, *gate_lines: str) -> None:
    """One open ledger entry, DW-1, in the run's v7 ledger, carrying ``gate_lines``;
    committed so the checkout stays clean."""
    ledger = engine.paths.deferred_work
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "# Deferred Work\n\n### DW-1: item\n\norigin: test, 2026-10-01\n"
        "location: src.txt:1\nreason: test entry.\nstatus: open\n"
        + "".join(f"{line}\n" for line in gate_lines),
        encoding="utf-8",
    )
    git(layout.project, "add", "-A")
    git(layout.project, "commit", "-q", "-m", "ledger")


@pytest.mark.parametrize("token", ["2.1", "02.1", "2", "epic-checkout"])
def test_tickets_gate_refuses_ref(project_v7, token):
    """A `gate:` token in the ticket grammar refuses the ticket it names, by ref,
    by its epic's id, or by its epic's folder: the run pauses at `story-gate`
    before the ticket is registered or any session runs. Ablation (performed):
    route `TicketsEngine._gate_hits` back to the base story grammar and every row
    FAILS (`2.1` and `epic-checkout` are no story key)."""
    row = ticket_row("2.1", epic="epic-checkout")
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, adapter = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, f"gate: {token}")

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert (saved.paused_stage, saved.paused_story_key) == (PAUSE_STORY_GATE, "2.1")
    assert saved.tasks == {} and adapter.sessions == [] and tree.marks == []
    assert f"DW-1 (gate: {token})" in saved.paused_reason
    assert [e["dw_ids"] for e in _kinds(engine, "story-gated")] == [["DW-1"]]


@pytest.mark.parametrize("token", ["2", "epic-checkout"])
def test_tickets_gate_rebinds_an_unbound_task_on_the_restart_arm(project_v7, token):
    """The restart arm gates a registered task before `_run_story`; one a crash
    left unbound is re-resolved first, so an epic token still gates it."""
    row = ticket_row("2.1", epic="epic-checkout")
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, f"gate: {token}")
    task = StoryTask("2.1", 0)
    engine.state.tasks["2.1"] = task

    with pytest.raises(RunPaused) as paused:
        engine._refuse_gated_story("2.1")

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_STORY_GATE, "2.1")
    assert task.ticket is not None and task.ticket.epic_slug == "epic-checkout"
    assert [e["story_key"] for e in _kinds(engine, "ticket-rebound")] == ["2.1"]


@pytest.mark.parametrize("token", ["2.2", "1", "epic-cart", "2.1a", "story-2-1.md"])
def test_tickets_gate_passes_a_ticket_no_token_names(project_v7, token):
    """A ticket-grammar token for another ticket or epic gates nothing here — no
    prefix or split arm reaches `2.1` — so the gate check returns."""
    row = ticket_row("2.1", epic="epic-checkout")
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, f"gate: {token}")

    picked = engine._pick_next()
    assert picked is not None
    engine._refuse_gated_story(picked.key)  # must not raise
    assert _kinds(engine, "story-gated") == []


def test_tickets_gate_pauses_on_v6_token(project_v7):
    """A migrated ledger keeps its v6 `gate: 3-2` verbatim, and no ticket carries a
    story key, so read under the ticket grammar alone it would gate nothing in
    silence. The run pauses at `story-gate` instead, naming the entry, the token
    and the `v6_key` the migration left in the ticket file. Ablation (performed):
    return `()` from `TicketsEngine._legacy_gate_tokens` and the run dispatches
    1.1 — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, adapter = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2-invite-link, 1.9")

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert (saved.paused_stage, saved.paused_story_key) == (PAUSE_STORY_GATE, "1.1")
    assert saved.tasks == {} and adapter.sessions == []
    assert "DW-1 (gate: 3-2-invite-link)" in saved.paused_reason
    assert "`v6_key`" in saved.paused_reason
    assert saved.paused_reason.endswith(f"`bmad-loop resume {engine.state.run_id}`")
    assert [e["dw_ids"] for e in _kinds(engine, "story-gated")] == [["DW-1"]]


def _v6_keys(layout: V7Layout, **keys: str) -> None:
    """Rewrite the on-disk `DEFAULT_TREE` with ``keys`` (``e1_1="3-2-x"`` sets
    entry 1.1's `v6_key`) as the v6→v7 migration leaves them; committed so the
    checkout stays clean."""
    epics = tuple(
        dataclasses.replace(
            epic,
            entries=tuple(
                dataclasses.replace(entry, v6_key=keys.get(f"e{epic.id}_{entry.id}"))
                for entry in epic.entries
            ),
        )
        for epic in DEFAULT_TREE
    )
    write_ticket_tree(layout, epics)
    git(layout.project, "add", "-A")
    git(layout.project, "commit", "-q", "-m", "v6 keys")


@pytest.mark.parametrize("token", ["3-2-invite-link", "3-2"])
def test_tickets_gate_resolves_a_v6_token_to_its_ticket(project_v7, token):
    """A v6 token that exactly one ticket's `v6_key` claims (the key itself, or
    its `N-M` prefix) gates that ticket as its ref would: the run pauses on 1.1
    with the ordinary gated clause, naming the token as written, and not the
    legacy clause. Ablation (performed): return every token unresolved from
    `deferredwork.partition_legacy_tokens` and the legacy clause comes back —
    this FAILS."""
    _v6_keys(project_v7, e1_1="3-2-invite-link")
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, adapter = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, f"gate: {token}")

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert (saved.paused_stage, saved.paused_story_key) == (PAUSE_STORY_GATE, "1.1")
    assert saved.tasks == {} and adapter.sessions == []
    assert (
        f"1.1 is gated by unlanded deferred work: DW-1 (gate: {token} (`v6_key` of 1.1))"
        in saved.paused_reason
    )
    assert "no single ticket" not in saved.paused_reason
    assert [e["dw_ids"] for e in _kinds(engine, "story-gated")] == [["DW-1"]]


def test_tickets_gate_v6_token_resolved_elsewhere_passes(project_v7):
    """A v6 token resolved to 1.2 gates 1.2 only: a pick of 1.1 dispatches with no
    `story-gate` pause, while 1.2 itself is refused. Ablation (performed): return
    every token unresolved from `partition_legacy_tokens` and 1.1 pauses on the
    legacy clause — this FAILS."""
    _v6_keys(project_v7, e1_2="3-2-invite-link")
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")

    picked = engine._pick_next()
    assert picked is not None and picked.key == "1.1"
    engine._refuse_gated_story(picked.key)  # must not raise
    assert _kinds(engine, "story-gated") == []
    with pytest.raises(RunPaused) as paused:
        engine._refuse_gated_story("1.2")
    assert "DW-1 (gate: 3-2 (`v6_key` of 1.2))" in paused.value.reason


def test_tickets_gate_v6_token_follows_the_picked_ticket_across_a_renumber(project_v7):
    """The binding is the picked ticket's identity (epic folder + entry id), and a
    resume keeps it; an epic renumbered since (`epic-cart` 1 → 3) gives the same
    ticket the ref 3.1 in the claims snapshot. The token still gates the picked
    1.1, through the ticket it names rather than that ref, while the binding-free
    ref compare cannot see it. Ablation (performed): compare the resolved ref with
    `story_key` instead of the claim's identity and 1.1 passes — this FAILS."""
    _v6_keys(project_v7, e1_1="3-2-invite-link")
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")
    picked = engine._pick_next()
    assert picked is not None and picked.key == "1.1"
    epics = tuple(
        dataclasses.replace(
            epic,
            id=3 if epic.slug == "epic-cart" else epic.id,
            entries=tuple(
                dataclasses.replace(
                    entry,
                    v6_key="3-2-invite-link" if (epic.id, entry.id) == (1, 1) else None,
                )
                for entry in epic.entries
            ),
        )
        for epic in DEFAULT_TREE
    )
    write_ticket_tree(project_v7, epics)

    with pytest.raises(RunPaused) as paused:
        engine._refuse_gated_story("1.1")

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_STORY_GATE, "1.1")
    assert "DW-1 (gate: 3-2 (`v6_key` of 3.1))" in paused.value.reason
    # With no binding only the ref can be compared, and 3.1 is not 1.1.
    engine._picked.clear()
    engine._refuse_gated_story("1.1")  # must not raise


@pytest.mark.parametrize(
    "keys",
    [
        # ambiguous: two tickets claim it (both halves of a split)
        {"e1_1": "3-2a-x", "e1_2": "3-2b-y"},
        # unclaimed: `3-20-x` is a different story, so nothing claims `3-2`
        {"e1_1": "3-20-x"},
    ],
)
def test_tickets_gate_ambiguous_v6_token_pauses_as_legacy(project_v7, keys):
    """A token no single ticket claims — two do, or none — stays a legacy token
    and pauses every pick. Ablation (performed): resolve to the first claimant
    regardless of count in `partition_legacy_tokens` and the ambiguous row
    FAILS."""
    _v6_keys(project_v7, **keys)
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")

    with pytest.raises(RunPaused) as paused:
        engine._refuse_gated_story("1.1")

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_STORY_GATE, "1.1")
    assert "DW-1 (gate: 3-2)" in paused.value.reason
    assert "no single ticket's `v6_key` claims" in paused.value.reason


def test_tickets_gate_unreadable_ticket_file_pauses(project_v7):
    """A ticket file that cannot be read for its `v6_key` claims pauses at
    `story-gate` before anything is registered, naming the file, and journals
    `story-gate-unreadable`; it never resolves or passes the token."""
    toml = project_v7.initiative_folder / "tickets" / "epic-cart" / "tickets" / "tickets.toml"
    toml.write_text("[[entry]\n", encoding="utf-8")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "broken")
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, adapter = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert (saved.paused_stage, saved.paused_story_key) == (PAUSE_STORY_GATE, "1.1")
    assert saved.tasks == {} and adapter.sessions == []
    assert str(toml) in saved.paused_reason
    (record,) = _kinds(engine, "story-gate-unreadable")
    assert record["story_key"] == "1.1" and str(toml) in record["error"]
    assert _kinds(engine, "story-gated") == []


def test_tickets_gate_claims_fault_pauses_before_the_rebind(project_v7):
    """On the restart arm a crash-unbound task is rebound inside the gate check;
    an entry whose claims cannot be read pauses before that entry's rebind, so
    the task stays unbound and no `ticket-rebound` is journaled."""
    toml = project_v7.initiative_folder / "tickets" / "epic-cart" / "tickets" / "tickets.toml"
    toml.write_text("[[entry]\n", encoding="utf-8")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "broken")
    row = ticket_row("2.1", epic="epic-checkout")
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")
    task = StoryTask("2.1", 0)
    engine.state.tasks["2.1"] = task

    with pytest.raises(RunPaused) as paused:
        engine._refuse_gated_story("2.1")

    assert (paused.value.stage, paused.value.story_key) == (PAUSE_STORY_GATE, "2.1")
    assert task.ticket is None
    assert _kinds(engine, "ticket-rebound") == []
    assert [e["story_key"] for e in _kinds(engine, "story-gate-unreadable")] == ["2.1"]


def test_tickets_gate_reads_claims_once_and_only_for_v6_tokens(project_v7, monkeypatch):
    """A ledger with no v6 token does no tree I/O, and a check over several v6
    tokens reads the claims once."""
    calls: list[object] = []

    def counting(folder):
        calls.append(folder)
        return (V6KeyClaim("3-2-invite-link", "1.2", "epic-cart", "2"),)

    monkeypatch.setattr(tickets_engine, "read_v6_key_entries", counting)
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 1.9, epic-checkout")
    engine._refuse_gated_story("1.1")
    assert calls == []

    _gate_ledger(engine, project_v7, "gate: 3-2, 3-2-invite-link")
    engine._refuse_gated_story("1.1")
    assert calls == [engine.paths.initiative_folder]
    engine._refuse_gated_story("1.1")  # a fresh check reads the tree again
    assert len(calls) == 2


def test_tickets_gate_closed_v6_entry_gates_nothing(project_v7):
    """A `done` entry retires its gate, v6 tokens included: the legacy pause holds
    only while the entry is unlanded."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    _gate_ledger(engine, project_v7, "gate: 3-2")
    ledger = engine.paths.deferred_work
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace("status: open", "status: done 2026-10-02"),
        encoding="utf-8",
    )

    engine._refuse_gated_story("1.1")  # must not raise
    assert _kinds(engine, "story-gated") == []


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


# --------------------------------------------------- dispatch, verify, review


def _bound(engine: TicketsEngine, tree: FakeTicketTree, ref: str = "1.1") -> StoryTask:
    """A registered unit for ``ref`` carrying the binding `find` resolves, as
    `_run_story` attaches it."""
    task = StoryTask(ref, 0)
    task.ticket = ticket_binding(tree.finds[ref])
    engine.state.tasks[ref] = task
    return task


def _write_plan(
    plan: Path, status: str, *, ticket: str = "1", baseline: str = "", risk: str = ""
) -> None:
    """A v7 build plan as build-auto leaves it. Bytes, so Windows writes LF."""
    lines = ["---", f"ticket: {ticket}", f"status: {status}"]
    if baseline:
        lines.append(f"baseline_revision: '{baseline}'")
    if risk:
        lines.append(f"risk: {risk}")
    lines += ["---", "", "# plan", ""]
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes("\n".join(lines).encode("utf-8"))


def _probe_session(engine: TicketsEngine, adapter: MockAdapter, task: StoryTask, **kw):
    """The `SessionSpec` `_run_session` hands the adapter for ``task``."""
    adapter.script.append(SessionResult(status="crashed"))
    engine._run_session(task, prompt="probe", seq=1, **kw)
    spec = adapter.sessions[-1]
    assert isinstance(spec, SessionSpec)
    return spec


def test_dev_prompt_uses_ticket_ref(project_v7):
    """Every dev leg dispatches `ticket <ref>`: the fresh leg is exactly the
    dry-run's line plus the tickets clauses (no backticks; the park clause last,
    and absent under `[operator] enabled = false`), and the repair leg keeps the
    feedback file as its last backticked token.
    Ablation (performed): fall back to the base `_dev_prompt` and the fresh
    prompt dispatches the bare key `1.1` with the sprint-board clause — this
    FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    off = dataclasses.replace(_policy(), operator=OperatorPolicy(enabled=False))
    engine, _ = make_engine(project_v7, tree, policy=off)
    task = _bound(engine, tree)
    skill = engine._dev_skill()

    fresh = engine._dev_prompt(task, None)

    assert engine._operator_park_instruction() == ""
    assert fresh == f"/{skill} ticket 1.1 — {engine._sprint_board_instruction()}"
    assert "`" not in fresh and "sprint-status" not in fresh
    assert "tickets.py mark" in fresh and "awaiting-operator" not in fresh
    parking, _ = make_engine(project_v7, tree)
    park = parking._operator_park_instruction()
    assert park and "`" not in park and "awaiting-operator" in park
    assert parking._dev_prompt(task, None) == f"{fresh} {park}"

    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "built")
    # the tree reads the plan the dev leg built, as `tickets.py find` would
    tree.finds["1.1"] = dataclasses.replace(
        tree.finds["1.1"], row=ticket_row("1.1", status="built")
    )
    task.spec_file = str(plan)
    feedback = project_v7.project / "feedback.md"
    repair = engine._dev_prompt(task, feedback)

    assert repair.startswith(f"/{skill} ticket 1.1 — resume the autonomous dev session")
    assert repair.split("`")[-2] == str(feedback)
    assert [(m.target, m.reason) for m in tree.marks] == [("in-progress", "repair")]


@pytest.mark.parametrize(
    ("policy", "risk", "thorough"),
    [
        ("high-risk", "high", True),
        ("high-risk", "low", False),
        ("high-risk", "", False),
        ("always", "low", True),
    ],
)
def test_dev_prompt_thorough_on_high_risk(project_v7, policy, risk, thorough):
    """`[tickets] thorough_review`: the dev leg reads the find row's risk; the
    review leg reads the plan's own `risk` (here raised to high by the build).
    Ablation (performed): drop the `thorough` token from `dispatch_invocation`
    and the high-risk rows FAIL."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", risk=risk),))
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        tickets=TicketsPolicy(thorough_review=policy),
    )
    engine, _ = make_engine(project_v7, tree, policy=pol)
    task = _bound(engine, tree)
    skill = engine._dev_skill()

    head = engine._dev_prompt(task, None).split(" — ")[0]

    assert head == f"/{skill} ticket 1.1" + (" thorough" if thorough else "")

    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "built", risk="high")
    task.spec_file = str(plan)
    review_head = engine._review_prompt(task).split(" — ")[0]
    assert review_head.endswith("ticket 1.1 thorough")


@pytest.mark.parametrize("fault", ["unreadable", "malformed"])
def test_review_thorough_when_plan_risk_cannot_be_read(project_v7, fault):
    """A plan whose risk cannot be read may have been raised above the find row's
    `low`: the review leg reads it as high and journals the fault, instead of
    falling back to the row's risk unseen. `never` still wins. Ablation
    (performed): restore the silent `{}` fallback and both rows FAIL."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", risk="low"),))

    def engine_for(thorough_review: str):
        pol = Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
            tickets=TicketsPolicy(thorough_review=thorough_review),
        )
        engine, _ = make_engine(project_v7, tree, policy=pol)
        return engine, _bound(engine, tree)

    engine, task = engine_for("high-risk")
    plan = Path(tree.finds["1.1"].plan)
    if fault == "unreadable":
        plan.mkdir(parents=True)  # read_text raises an OSError on every platform
    else:
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_bytes(b"---\nstatus: built\nrisk: [high\n---\n\n# plan\n")
    task.spec_file = str(plan)

    assert engine._review_prompt(task).split(" — ")[0].endswith("ticket 1.1 thorough")
    [entry] = _kinds(engine, "ticket-plan-risk-unread")
    assert entry["story_key"] == "1.1"
    assert entry["plan"] == str(plan)
    assert entry["detail"]

    never, never_task = engine_for("never")
    never_task.spec_file = str(plan)
    assert "thorough" not in never._review_prompt(never_task)


def test_dev_prompt_never_thorough_when_policy_never(project_v7):
    """`thorough_review = "never"` keeps both legs at build-auto's own review
    selection, whatever the ticket's or the plan's risk."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", risk="high"),))
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        tickets=TicketsPolicy(thorough_review="never"),
    )
    engine, _ = make_engine(project_v7, tree, policy=pol)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "built", risk="high")
    task.spec_file = str(plan)

    assert "thorough" not in engine._dev_prompt(task, None)
    assert "thorough" not in engine._review_prompt(task)


def test_plan_halt_leg_prompt_and_env_in_lockstep(project_v7):
    """Leg 1 of an unrefined plan checkpoint: the prompt carries `Halt after
    planning.` (the dry-run's exact line) exactly when the dev session's env
    carries `BMAD_LOOP_PLAN_HALT`, and both drop together once the plan is on disk
    at `ready-for-dev`. No other session gets the env. Ablation (performed): drop
    the env from `_extra_session_env` and the leg-1 assertion FAILS."""
    row = ticket_row("1.1", plan_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, adapter = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    skill = engine._dev_skill()
    dry_run_line = tickets_source.dispatch_invocation(
        skill, "1.1", thorough=False, plan_halt=tickets_source.is_plan_halt_leg(row)
    )

    leg_one = engine._dev_prompt(task, None)

    assert dry_run_line == f"/{skill} ticket 1.1 Halt after planning."
    assert leg_one.startswith(dry_run_line + " ")
    assert _probe_session(engine, adapter, task, role="dev").env["BMAD_LOOP_PLAN_HALT"] == "1"
    assert "BMAD_LOOP_PLAN_HALT" not in _probe_session(engine, adapter, task, role="review").env
    assert "BMAD_LOOP_PLAN_HALT" not in (
        _probe_session(engine, adapter, task, role="dev", label="tea").env
    )

    _write_plan(Path(tree.finds["1.1"].plan), "ready-for-dev")

    assert "Halt after planning." not in engine._dev_prompt(task, None)
    assert "BMAD_LOOP_PLAN_HALT" not in _probe_session(engine, adapter, task, role="dev").env


def test_plan_produced_reads_the_active_workspace(project_v7, tmp_path):
    """Under worktree isolation leg 1 writes the plan into the retained unit
    worktree only, so the plan-produced check reads the workspace's copy: the
    resume after a plan checkpoint dispatches the implement leg, not another
    `Halt after planning.` leg. Ablation (performed): read `ticket.plan` (the main
    checkout's path) in `_plan_produced` and the halt leg repeats — this FAILS."""
    row = ticket_row("1.1", plan_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    mount = tmp_path / "mount"
    engine.workspace = Workspace(root=mount, paths=engine.paths.rebased(mount))
    mounted = mount / plan.relative_to(project_v7.project)
    assert engine._plan_halt_now(task)

    _write_plan(mounted, "ready-for-dev")

    assert not plan.exists()  # only the worktree's copy is planned
    assert engine._bound_plan(task) == mounted
    assert not engine._plan_halt_now(task)
    assert "Halt after planning." not in engine._dev_prompt(task, None)


def test_unreadable_checkpoint_plan_stops_the_dispatch(project_v7):
    """A plan-checkpoint plan that is on disk but cannot be read is not an absent
    plan: the run launches no session and pauses naming the fault, instead of
    sending another `Halt after planning.` leg onto it. Before dispatch (PENDING)
    it pauses at `ticket-tree`; mid-dispatch (DEV_RUNNING) it escalates.
    Ablation (performed): fold the read fault back into `return False` in
    `_plan_produced` and the session launches — this FAILS."""
    row = ticket_row("1.1", plan_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    plan = Path(tree.finds["1.1"].plan)
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes(b"---\nticket: 1\nstatus: ready-for-dev\xff\n---\n")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "plan")
    engine, adapter = make_engine(project_v7, tree)

    summary = engine.run()

    assert summary.paused
    assert adapter.sessions == []
    (paused,) = _kinds(engine, "run-paused")
    assert "cannot be read" in paused["reason"]

    pending = _bound(engine, tree)
    with pytest.raises(RunPaused) as before:
        engine._plan_halt_now(pending)
    assert before.value.stage == PAUSE_TICKET_TREE
    assert pending.phase == Phase.PENDING

    running = _bound(engine, tree)
    running.phase = Phase.DEV_RUNNING
    with pytest.raises(RunPaused) as during:
        engine._plan_halt_now(running)
    assert during.value.stage == PAUSE_ESCALATION
    assert running.phase == Phase.ESCALATED


@pytest.mark.parametrize(
    ("body", "fault"),
    [
        (b"# plan with no frontmatter\n", "no frontmatter block"),
        (b"---\nticket: 1\nstatus: [ready-for-dev\n---\n", "unparseable frontmatter YAML"),
        (b"---\n- ready-for-dev\n---\n", "frontmatter is not a mapping"),
    ],
)
def test_malformed_checkpoint_plan_stops_the_dispatch(project_v7, body, fault):
    """`parse_frontmatter` folds a plan with no block, unparseable YAML or a
    non-mapping document into `{}`, which raises nothing. An unrefined
    plan-checkpoint plan in that shape still pauses at `ticket-tree` naming the
    fault, instead of reading as unplanned and sending another `Halt after
    planning.` leg onto it. A unit that sends no halt leg dispatches as before.
    Ablation (performed): drop the `frontmatter_fault` check in `_plan_produced`
    and the halt leg dispatches — this FAILS."""
    row = ticket_row("1.1", plan_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    plan = Path(tree.finds["1.1"].plan)
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes(body)
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)

    with pytest.raises(RunPaused) as paused:
        engine._plan_halt_now(task)
    assert paused.value.stage == PAUSE_TICKET_TREE
    assert fault in str(paused.value)
    assert task.phase == Phase.PENDING

    assert task.ticket is not None
    task.ticket = dataclasses.replace(task.ticket, plan_checkpoint=False)
    assert not engine._plan_halt_now(task)


def test_bound_plan_moves_with_a_renamed_project(project_v7, tmp_path):
    """`find` names the plan absolute under the launch-time project, which
    `state.project` keeps. A resume after the project moved reads, re-arms and
    marks the plan under the live project, not under a directory that is gone.
    A plan outside the recorded project is left as recorded. Ablation
    (performed): bind `Path(ticket.plan)` in `_bound_plan` and the moved plan
    reads as the old path — this FAILS."""
    row = ticket_row("1.1", plan_checkpoint=True)
    tree = _tree(project_v7, ready_to_start=(row,))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    live = Path(tree.finds["1.1"].plan)
    launched = tmp_path / "launched-here"
    engine.state.project = str(launched)
    assert task.ticket is not None
    task.ticket = dataclasses.replace(
        task.ticket, plan=str(launched / live.relative_to(project_v7.project))
    )
    _write_plan(live, "ready-for-dev")

    assert engine._bound_plan(task) == live
    assert not engine._plan_halt_now(task)

    outside = tmp_path / "elsewhere" / "plan-1.1.md"
    task.ticket = dataclasses.replace(task.ticket, plan=str(outside))
    assert engine._bound_plan(task) == outside


def test_resume_moves_in_place_spec_paths_with_a_renamed_project(project_v7, tmp_path, monkeypatch):
    """An in-place unit persists its plan paths absolute under the launch-time
    project. A resume at `REVIEW_VERIFY` after the project moved re-anchors them
    under the live project before recovery reads them, so the review gate
    accepts the live plan instead of retrying on a path that is gone. A path
    outside the recorded project is left as recorded. Ablation (performed): drop
    `TicketsEngine._finish_inflight` and recovery sees the launch-time paths and
    the gate retries — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    live = Path(tree.finds["1.1"].plan)
    _write_plan(live, "built")
    launched = tmp_path / "launched-here"
    engine.state.project = str(launched)
    task.phase = Phase.REVIEW_VERIFY
    task.spec_file = str(launched / live.relative_to(project_v7.project))
    outside = tmp_path / "elsewhere" / "plan-1.1.md"
    task.dispatched_spec_file = str(outside)
    seen: list[tuple[str | None, str | None]] = []

    def recover(self: Engine) -> None:
        seen.append((task.spec_file, task.dispatched_spec_file))

    monkeypatch.setattr(Engine, "_finish_inflight", recover)

    engine._finish_inflight()

    assert seen == [(str(live), str(outside))]
    assert engine._verify_review(task).ok


def test_review_prompt_has_no_append_and_no_board_text(project_v7):
    """The follow-up review re-dispatches the ticket with the ledger sentence and
    the tickets clauses: never the banned word, never the sprint board, no
    backticks — and the park clause last only under `on_review_demotion =
    "park"`, the one setting a tickets review demotion parks under."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        operator=OperatorPolicy(on_review_demotion="park"),
    )
    engine, _ = make_engine(project_v7, tree, policy=pol)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "built")
    task.spec_file = str(plan)

    prompt = engine._review_prompt(task)

    assert prompt.startswith(
        f"/{engine._dev_skill('review')} ticket 1.1 — do NOT modify, re-open, or rewrite "
        f"existing deferred-work ledger entries"
    )
    assert "append" not in prompt.lower()
    assert "board" not in prompt.lower() and "sprint-status" not in prompt
    assert "`" not in prompt and str(plan) not in prompt
    assert prompt.endswith(
        f"{engine._board_handback_redirect()} {engine._operator_park_instruction()}"
    )
    escalating, _ = make_engine(project_v7, tree)
    plain = escalating._review_prompt(task)
    assert plain.endswith(escalating._board_handback_redirect())
    assert "awaiting-operator" not in plain


def test_expected_spec_pinned_on_attempt_one(project_v7):
    """The read-back is pinned to the bound plan from the first dev attempt —
    before the plan exists, with no recorded spec — and to the same plan on the
    review leg; a ticket session ends at `built` (dev) or `built`/`done` (review)
    and probes the initiative folder for a fallback result. A labeled workflow
    session keeps the v6 contract. Ablation (performed): fall back to the base
    `_expected_spec_for` and the attempt-1 pin is None — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, adapter = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = tree.finds["1.1"].plan
    assert task.spec_file is None and not Path(plan).exists()

    dev = _probe_session(engine, adapter, task, role="dev")
    review = _probe_session(engine, adapter, task, role="review")
    workflow = _probe_session(engine, adapter, task, role="dev", label="tea")

    folder = engine.workspace.paths.initiative_folder
    assert (dev.expected_spec, dev.success_statuses, dev.fallback_result_dirs) == (
        plan,
        ("built",),
        (folder,),
    )
    assert (review.expected_spec, review.success_statuses) == (plan, ("built", "done"))
    assert (workflow.expected_spec, workflow.success_statuses, workflow.fallback_result_dirs) == (
        None,
        ("done",),
        (),
    )


def test_verify_rejects_done_from_dev(project_v7):
    """A dev session that marks its own plan `done` is refused: the orchestrator
    owns `done`, so the gate retries; the same work at `built` passes and records
    the bound plan as the task's spec. Ablation (performed): route the tickets
    `_verify_dev_artifacts` to the base sprint gate and the `built` plan is
    refused instead."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    task.baseline_commit = verify.rev_parse_head(project_v7.project)
    plan = Path(tree.finds["1.1"].plan)
    (project_v7.project / "src.txt").write_bytes(b"changed\n")
    result = {"workflow": "auto-dev", "spec_file": str(plan)}

    _write_plan(plan, "done", baseline=task.baseline_commit)
    refused = engine._verify_dev_artifacts(task, result)

    assert not refused.ok and "built" in refused.reason
    assert task.spec_file is None

    _write_plan(plan, "built", baseline=task.baseline_commit)
    accepted = engine._verify_dev_artifacts(task, result)

    assert accepted.ok, accepted.reason
    assert task.spec_file == str(plan)


def _builds(
    layout: V7Layout,
    tree: FakeTicketTree,
    plan: Path,
    *,
    ticket: str = "1",
    seen: list | None = None,
):
    """A dev session that builds the ticket: code in the tree and the plan at
    `built`, the fake's row following as `tickets.py` would derive it. ``seen``
    collects the plan's status at each launch."""

    def build(spec: SessionSpec) -> SessionResult:
        cwd = Path(spec.cwd)
        # the workspace's copy: the plan itself in place, the mount's under isolation
        here = cwd / plan.relative_to(layout.project)
        assert spec.expected_spec == str(here)
        if seen is not None:
            seen.append(
                status_of(parse_frontmatter(here.read_text(encoding="utf-8")))
                if here.is_file()
                else ""
            )
        baseline = verify.rev_parse_head(cwd)
        (cwd / "src.txt").write_bytes(b"cart model\n")
        _write_plan(here, "built", ticket=ticket, baseline=baseline)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="built")
        )
        # the ticket now reads as finished in the tree, as `tickets.py` derives it
        tree.next_view = _tree(layout).next_view
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "built"}
        return SessionResult(status="completed", result_json=result)

    return build


def _committed_status(layout: V7Layout, sha: str, plan: Path) -> str:
    rel = plan.relative_to(layout.project).as_posix()
    return status_of(parse_frontmatter(git(layout.project, "show", f"{sha}:{rel}")))


def _disk_restores(tree: FakeTicketTree, monkeypatch) -> None:
    """Make the fake's `restore_preimage` put the bytes back on disk, as the real
    chokepoint does, while still recording the call."""
    real = tree.restore_preimage

    def restore(plan, data, journal=None):
        real(plan, data, journal)
        if data is None:
            Path(plan).unlink(missing_ok=True)
        else:
            Path(plan).write_bytes(data)

    monkeypatch.setattr(tree, "restore_preimage", restore)


def _reject_commits(layout: V7Layout) -> None:
    """A native `pre-commit` hook that rejects every commit: `finalize_commit`
    fails the way a lint or secret hook makes it fail."""
    hook = layout.project / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def test_mark_done_rides_squash_commit(project_v7, monkeypatch):
    """Under `mark_done = "auto"` the orchestrator accepts the verified ticket
    inside the commit window: `mark 1.1 done` (reason `accept`, the bound plan)
    runs with the code in the tree and HEAD still at the baseline, and the
    squashed unit commit carries the plan at `done` together with that code.
    Ablation (performed): drop the `_terminal_plan_write` call from
    `_finalize_commit_phase` and the commit carries `built` with no mark — this
    FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    at_mark: list[tuple[str, bool]] = []
    marking = tree.mark

    def mark(ref, target, **kw):
        at_mark.append(
            (verify.rev_parse_head(project_v7.project), (project_v7.project / "src.txt").is_file())
        )
        return marking(ref, target, **kw)

    monkeypatch.setattr(tree, "mark", mark)
    engine, adapter = make_engine(project_v7, tree, script=[_builds(project_v7, tree, plan)])
    engine.state.max_stories = 1

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert len(adapter.sessions) == 1
    assert adapter.sessions[0].prompt.startswith(f"/{engine._dev_skill()} ticket 1.1")
    assert [(m.ref, m.target, m.reason, m.expected_plan, m.outcome) for m in tree.marks] == [
        ("1.1", "done", "accept", plan, "written")
    ]
    assert at_mark == [(task.baseline_commit, True)]
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"
    assert git(project_v7.project, "show", f"{task.commit_sha}:src.txt").strip() == "cart model"
    assert git(project_v7.project, "status", "--porcelain") == ""


def test_mark_done_never_policy_leaves_built(project_v7):
    """`mark_done = "never"`: the unit commits with the plan at `built` — a
    person accepts it — and the tree is never written. Ablation (performed):
    drop the `mark_done` check in `_terminal_plan_write` and the plan is marked
    `done` — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    pol = dataclasses.replace(_policy(), tickets=TicketsPolicy(mark_done="never"))
    engine, adapter = make_engine(
        project_v7, tree, policy=pol, script=[_builds(project_v7, tree, plan)], max_stories=1
    )

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert len(adapter.sessions) == 1
    assert _committed_status(project_v7, task.commit_sha, plan) == "built"
    assert tree.marks == [] and tree.restores == []


def _reviews(layout: V7Layout, plan: Path):
    """A follow-up review pass that leaves the plan where the build left it, at
    `built`, and recommends no further pass."""

    def review(spec: SessionSpec) -> SessionResult:
        here = Path(spec.cwd) / plan.relative_to(layout.project)
        assert status_of(parse_frontmatter(here.read_text(encoding="utf-8"))) == "built"
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "built"}
        result["followup_review_recommended"] = False
        return SessionResult(status="completed", result_json=result)

    return review


def test_review_converges_on_a_built_plan(project_v7, monkeypatch):
    """A tickets review finishes its plan at `built`, and that converges: one
    review pass, then the verify gate, the mark-done and the unit commit — no
    second cycle and no defer. The converged set is exactly the review session's
    success statuses (`built`, or `done` on a plan a human accepted). Ablation
    (performed): drop `TicketsEngine._review_converged_statuses` and the base
    `{"done"}` reads `built` as non-terminal — the loop burns `max_review_cycles`
    and defers, and this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    pol = dataclasses.replace(_policy(), review=ReviewPolicy(enabled=True, trigger="always"))
    script = [_builds(project_v7, tree, plan)] + [_reviews(project_v7, plan)] * 3
    engine, adapter = make_engine(project_v7, tree, policy=pol, script=script, max_stories=1)

    assert engine._review_converged_statuses() == frozenset(
        engine._session_success_statuses("review", None)
    )
    assert engine._review_converged_statuses() == {"built", "done"}

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha and not task.defer_reason
    assert len(adapter.sessions) == 2  # the build and one review pass
    assert adapter.sessions[1].prompt.startswith(f"/{engine._dev_skill('review')} ticket 1.1")
    assert [(e["cycle"], e["status"]) for e in _kinds(engine, "review-result")] == [(1, "built")]
    assert _kinds(engine, "review-retry") == []
    assert [(m.ref, m.target, m.reason) for m in tree.marks] == [("1.1", "done", "accept")]
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"
    assert git(project_v7.project, "status", "--porcelain") == ""


def test_tickets_review_walked_back_plan_escalates(project_v7, monkeypatch):
    """A review that enters at `done` (a human accepted the plan once dev
    verified) and converges with the plan finalized at `built` pauses the run
    with a contradiction naming both statuses: no second review cycle, no defer,
    and no done mark. The result is the one the adapter synthesizes from that
    plan (frontmatter `built`): a walk-back to a pre-`built` status is a
    non-terminal pass that never reaches this gate. Ablation (performed): make
    `_is_plan_regression` return False and the gate retries instead — the loop
    burns its review cycles and this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    pol = dataclasses.replace(_policy(), review=ReviewPolicy(enabled=True, trigger="always"))

    def finalizes_built(spec: SessionSpec) -> SessionResult:
        here = Path(spec.cwd) / plan.relative_to(project_v7.project)
        _write_plan(here, "built", baseline=verify.rev_parse_head(Path(spec.cwd)))
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "built"}
        result["followup_review_recommended"] = False
        return SessionResult(status="completed", result_json=result)

    script = [_builds(project_v7, tree, plan)] + [finalizes_built] * 3
    engine, adapter = make_engine(project_v7, tree, policy=pol, script=script, max_stories=1)
    real_reset = engine._reset_spec_for_review

    def accepted_then_reset(task):
        _write_plan(plan, "done", baseline=task.baseline_commit)
        return real_reset(task)

    monkeypatch.setattr(engine, "_reset_spec_for_review", accepted_then_reset)

    summary = engine.run()

    assert summary.paused
    task = engine.state.tasks["1.1"]
    assert task.phase == Phase.ESCALATED and not task.defer_reason
    assert len(adapter.sessions) == 2  # the build and one review pass
    [escalated] = _kinds(engine, "story-escalated")
    assert "back from 'done' to 'built'" in escalated["reason"]
    assert tree.marks == []


def test_mark_done_undone_on_commit_failure(project_v7, monkeypatch):
    """The mark is made just BEFORE `finalize_commit` so it rides the unit's
    commit — but that commit can still fail. A plan left `done` for a commit that
    does not exist would let `next` unblock dependents of absent code, so the
    `GitError` arm puts the plan's exact pre-mark bytes back through
    `restore_preimage` before escalating. Ablation (performed): delete the
    `_undo_terminal_plan_write` calls in `_finalize_commit_phase` and the plan
    stays `done` — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    built: list[bytes] = []
    marking = tree.mark

    def mark(ref, target, **kw):
        built.append(plan.read_bytes())
        return marking(ref, target, **kw)

    monkeypatch.setattr(tree, "mark", mark)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    _reject_commits(project_v7)

    summary = engine.run()

    assert summary.paused  # the commit really did fail
    assert engine.state.tasks["1.1"].phase == Phase.ESCALATED
    assert [m.target for m in tree.marks] == ["done"]
    assert tree.restores == [(plan, built[0])]
    assert plan.read_bytes() == built[0]  # byte-for-byte
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "built"


def test_failed_mark_done_undo_is_named_in_the_escalation(project_v7, monkeypatch):
    """A commit failure whose put-back of the pre-mark plan also fails (a held
    lock, an FS fault) leaves the plan reading `done` for a commit that does not
    exist. The undo still never raises out of the failure arm, but the
    escalation names the plan instead of reading as a plain commit failure.
    Ablation (performed): return None from `_undo_terminal_plan_write`'s
    failure arm and the reason no longer names the plan — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)

    def restore(plan, data, journal=None):
        raise TicketsPlanIOError(f"cannot restore {plan}: disk full")

    monkeypatch.setattr(tree, "restore_preimage", restore)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    _reject_commits(project_v7)

    summary = engine.run()

    assert summary.paused
    assert engine.state.tasks["1.1"].phase == Phase.ESCALATED
    [escalated] = _kinds(engine, "story-escalated")
    assert escalated["reason"].startswith("commit failed: ")
    assert f"{plan} could not be put back" in escalated["reason"]
    assert "disk full" in escalated["reason"]


def _host_dies_past_the_mark(engine: TicketsEngine, monkeypatch) -> list:
    """Kill the host inside the commit window right after the in-window mark:
    `finalize_commit` raises `_HostDied`, the window's `BaseException` arm cannot
    put the plan back (the process is gone), and no save after the COMMITTING
    one reaches the disk. Returns the `(status, digest)` latch each COMMITTING
    save carried."""
    latched: list[tuple[str, str]] = []
    dead: list[bool] = []
    real_save = engine._save

    def save():
        if dead:
            return  # the host is gone: no later save reaches the disk
        task = engine.state.tasks.get("1.1")
        if task is not None and task.phase == Phase.COMMITTING:
            latched.append((task.ticket_premark_status, task.ticket_premark_digest))
        real_save()

    def host_dies(*a, **kw):
        dead.append(True)
        raise _HostDied

    monkeypatch.setattr(engine, "_save", save)
    monkeypatch.setattr(verify, "finalize_commit", host_dies)
    monkeypatch.setattr(engine, "_undo_terminal_plan_write", host_dies)
    return latched


@pytest.mark.parametrize("case", ["commit-fails", "commit-lands", "legacy-state"])
def test_resumed_window_names_a_plan_an_interrupted_window_marked(project_v7, monkeypatch, case):
    """A host lost after the in-window `done` mark and before the squash leaves
    the plan `done` on disk and the unit COMMITTING, with its pre-window latch
    (`built` + the digest of the built bytes) in the COMMITTING save. The
    resumed window's mark is a no-op, so no pre-image is in memory; if its
    commit then fails, the escalation names the plan left `done` for a commit
    that does not exist — nothing is restored (no bytes were kept) and the plan
    stays `done` for the operator. A resumed commit that succeeds lands the plan
    at `done` as usual. A legacy state file (no latch) cannot tell, so its
    failed commit stays the plain `commit failed:` escalation. Ablation
    (performed): return None from `_terminal_plan_write`'s no-op arm and the
    escalation reads as a plain commit failure — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    built: list[bytes] = []
    marking = tree.mark

    def mark(ref, target, **kw):
        built.append(plan.read_bytes())
        return marking(ref, target, **kw)

    monkeypatch.setattr(tree, "mark", mark)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_finalize = verify.finalize_commit
    latched = _host_dies_past_the_mark(engine, monkeypatch)

    with pytest.raises(_HostDied):
        engine.run()

    digest = hashlib.sha256(built[0]).hexdigest()
    assert latched == [("built", digest)]
    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.COMMITTING and not task.commit_sha
    assert (task.ticket_premark_status, task.ticket_premark_digest) == ("built", digest)
    assert _plan_status(plan) == "done"
    assert tree.restores == []

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    commit_fails = case != "commit-lands"
    if commit_fails:
        _reject_commits(project_v7)
    if case == "legacy-state":
        state = load_state(engine.run_dir)
        state.tasks["1.1"].ticket_premark_status = ""
        state.tasks["1.1"].ticket_premark_digest = ""
        save_state(engine.run_dir, state)
    resumed = _resume(engine, tree)
    summary = resumed.run()

    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "written"), ("done", "noop")]
    assert tree.restores == []
    assert _plan_status(plan) == "done"
    task = load_state(engine.run_dir).tasks["1.1"]
    if commit_fails:
        assert summary.paused and task.phase == Phase.ESCALATED and not task.commit_sha
        [escalated] = _kinds(resumed, "story-escalated")
        reason = escalated["reason"]
        assert reason.startswith("commit failed: ")
        if case == "legacy-state":
            assert "interrupted commit window" not in reason
        else:
            assert f"{plan} reads done, not the 'built' the COMMITTING save latched" in reason
            assert "interrupted commit window" in reason
            assert "'built'" in reason and "does not exist" in reason and "by hand" in reason
    else:
        assert task.phase == Phase.DONE and task.commit_sha
        assert _committed_status(project_v7, task.commit_sha, plan) == "done"
        assert _kinds(resumed, "story-escalated") == []


def test_resumed_window_after_the_squash_landed_keeps_a_plain_commit_failure(
    project_v7, monkeypatch
):
    """A host lost after `finalize_commit` made the squash and before the DONE
    save leaves the unit COMMITTING with its pre-window latch, and the `done`
    plan both on disk and in HEAD. The resumed window's mark is a no-op on a plan
    whose bytes no longer match the latch, but HEAD already carries them: a
    replayed commit that fails keeps that HEAD, so the escalation is the plain
    `commit failed:` one and never tells the operator to restore a plan the
    landed commit holds. Ablation (performed): drop the `_head_carries` check
    from `_terminal_plan_write`'s no-op arm and the reason says the commit does
    not exist — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_finalize = verify.finalize_commit
    dead: list[bool] = []
    real_save = engine._save

    def save():
        if not dead:
            real_save()

    def lands_then_host_dies(*a, **kw):
        landed.append(real_finalize(*a, **kw))
        dead.append(True)
        raise _HostDied

    def host_is_gone(*a, **kw):
        raise _HostDied

    landed: list[str | None] = []
    monkeypatch.setattr(engine, "_save", save)
    monkeypatch.setattr(verify, "finalize_commit", lands_then_host_dies)
    monkeypatch.setattr(engine, "_undo_terminal_plan_write", host_is_gone)

    with pytest.raises(_HostDied):
        engine.run()

    [sha] = landed
    assert sha and verify.rev_parse_head(project_v7.project) == sha
    assert _committed_status(project_v7, sha, plan) == "done"
    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.COMMITTING and task.ticket_premark_status == "built"

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    _reject_commits(project_v7)
    resumed = _resume(engine, tree)
    summary = resumed.run()

    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "written"), ("done", "noop")]
    assert tree.restores == []
    assert verify.rev_parse_head(project_v7.project) == sha
    assert summary.paused
    [escalated] = _kinds(resumed, "story-escalated")
    reason = escalated["reason"]
    assert reason.startswith("commit failed: ")
    assert "does not exist" not in reason and "by hand" not in reason


def test_plan_at_target_before_the_window_keeps_a_plain_commit_failure(project_v7, monkeypatch):
    """A plan already `done` before the window (a human accepted it once the
    unit verified) latches `done` with a digest its bytes still match: the
    window's no-op mark yields no token, so a failed commit is the plain `commit
    failed:` escalation and nothing is restored. Ablation (performed): drop the
    digest/status check in `_terminal_plan_write`'s no-op arm and the reason
    names the plan as marked by an interrupted window — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_latch = engine._latch_post_landing_writes

    def accepted_then_latch(task):
        _write_plan(plan, "done", baseline=task.baseline_commit)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="done")
        )
        real_latch(task)

    monkeypatch.setattr(engine, "_latch_post_landing_writes", accepted_then_latch)
    _reject_commits(project_v7)

    summary = engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert summary.paused and task.phase == Phase.ESCALATED
    assert task.ticket_premark_status == "done"
    assert task.ticket_premark_digest == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "noop")]
    assert tree.restores == []
    [escalated] = _kinds(engine, "story-escalated")
    assert escalated["reason"].startswith("commit failed: ")
    assert "interrupted commit window" not in escalated["reason"]


@pytest.mark.parametrize("mark_done", ["auto", "never"])
def test_resumed_window_names_a_park_an_interrupted_window_marked(
    project_v7, monkeypatch, mark_done
):
    """The park arm of the same crash: a parked unit's window marks its
    `awaiting-operator` plan `blocked` whatever `mark_done` says, so the
    COMMITTING save latches `awaiting-operator` under `"never"` too. A host lost
    past that mark, then a resumed commit that fails, escalates naming the plan
    as reading `blocked`, with nothing restored. Ablations (performed): hardcode
    the no-op arm's target to `done` and the reason names `done` — this FAILS;
    drop the `operator_actions` clause from `_latch_premark`'s guard and the
    `"never"` row latches nothing — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    parked: list[bytes] = []
    marking = tree.mark

    def mark(ref, target, **kw):
        parked.append(plan.read_bytes())
        return marking(ref, target, **kw)

    monkeypatch.setattr(tree, "mark", mark)
    pol = dataclasses.replace(_policy(), tickets=TicketsPolicy(mark_done=mark_done))
    engine, _ = make_engine(
        project_v7, tree, policy=pol, script=[_parks(project_v7, tree, plan)], max_stories=1
    )
    real_finalize = verify.finalize_commit
    latched = _host_dies_past_the_mark(engine, monkeypatch)

    with pytest.raises(_HostDied):
        engine.run()

    assert latched == [("awaiting-operator", hashlib.sha256(parked[0]).hexdigest())]
    assert _plan_status(plan) == "blocked"

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    _reject_commits(project_v7)
    resumed = _resume(engine, tree)
    summary = resumed.run()

    assert [(m.target, m.outcome) for m in tree.marks] == [
        ("blocked", "written"),
        ("blocked", "noop"),
    ]
    assert tree.restores == []
    assert _plan_status(plan) == "blocked"
    task = load_state(engine.run_dir).tasks["1.1"]
    assert summary.paused and task.phase == Phase.ESCALATED and not task.commit_sha
    [escalated] = _kinds(resumed, "story-escalated")
    reason = escalated["reason"]
    assert reason.startswith("commit failed: ")
    assert (
        f"{plan} reads blocked, not the 'awaiting-operator' the COMMITTING save latched" in reason
    )
    assert "is left blocked for a commit that does not exist" in reason


def test_plan_rewritten_at_target_after_the_window_keeps_a_plain_commit_failure(
    project_v7, monkeypatch
):
    """A plan already `done` before the window latches `done`; if its bytes are
    then rewritten still at `done` (a body edit between a crash and the resume),
    the digest no longer matches but nothing marked it since — the latched
    status is already the target — so the resumed commit's failure stays the
    plain `commit failed:` escalation. Ablation (performed): delete `and before
    != target` from `_terminal_plan_write`'s no-op arm and the reason names the
    plan as left marked — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_latch = engine._latch_post_landing_writes

    def accepted_then_latch(task):
        _write_plan(plan, "done", baseline=task.baseline_commit)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="done")
        )
        real_latch(task)

    monkeypatch.setattr(engine, "_latch_post_landing_writes", accepted_then_latch)
    real_finalize = verify.finalize_commit
    latched = _host_dies_past_the_mark(engine, monkeypatch)

    with pytest.raises(_HostDied):
        engine.run()

    assert [status for status, _ in latched] == ["done"]
    task = load_state(engine.run_dir).tasks["1.1"]
    _write_plan(plan, "done", baseline=task.baseline_commit, risk="low")
    assert hashlib.sha256(plan.read_bytes()).hexdigest() != latched[0][1]

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    _reject_commits(project_v7)
    resumed = _resume(engine, tree)
    summary = resumed.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert summary.paused and task.phase == Phase.ESCALATED
    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "noop"), ("done", "noop")]
    assert tree.restores == []
    [escalated] = _kinds(resumed, "story-escalated")
    assert escalated["reason"].startswith("commit failed: ")
    assert "COMMITTING save latched" not in escalated["reason"]
    assert "interrupted commit window" not in escalated["reason"]


def test_t1_premark_latch_cleared_when_no_mark_is_owed(project_v7, monkeypatch):
    """Under `mark_done = "never"` (not parked) the window marks nothing, so the
    COMMITTING save carries no T1 latch — and a stale one is cleared, since the
    fields are assigned on every latch. Ablation (performed): return before
    clearing the T1 fields in `_latch_post_landing_writes` and the stale latch
    rides the save — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    pol = dataclasses.replace(_policy(), tickets=TicketsPolicy(mark_done="never"))
    engine, _ = make_engine(
        project_v7, tree, policy=pol, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_latch = engine._latch_post_landing_writes

    def stale_latch(task):
        task.ticket_premark_status = "in-progress"
        task.ticket_premark_digest = "0" * 64
        real_latch(task)

    monkeypatch.setattr(engine, "_latch_post_landing_writes", stale_latch)

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert (task.ticket_premark_status, task.ticket_premark_digest) == ("", "")
    assert tree.marks == []


def test_t1_premark_latch_cleared_when_the_plan_cannot_be_read(project_v7, monkeypatch):
    """A plan the latch cannot read leaves the T1 latch cleared — never a stale
    one, and never a digest of nothing — so a later no-op window reads as a
    legacy state rather than as one an interrupted window marked. Ablation
    (performed): latch the stale fields through the read fault (skip the
    clearing) and the COMMITTING save carries them — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    real_latch = engine._latch_post_landing_writes
    real_read = Path.read_bytes
    latching: list[bool] = []
    latched: list[tuple[str, str]] = []

    def read_bytes(self):
        if latching and self == plan:
            raise OSError("plan read fault")
        return real_read(self)

    def faulted_latch(task):
        task.ticket_premark_status = "in-progress"
        task.ticket_premark_digest = "0" * 64
        latching.append(True)
        try:
            real_latch(task)
        finally:
            latching.clear()
        latched.append((task.ticket_premark_status, task.ticket_premark_digest))

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(engine, "_latch_post_landing_writes", faulted_latch)

    engine.run()

    assert latched == [("", "")]
    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert [m.target for m in tree.marks] == ["done"]


def test_mark_done_undone_when_the_window_is_interrupted(project_v7, monkeypatch):
    """The window's other failure arm: anything else raised out of
    `finalize_commit` (an FS fault here, or a stop signal) also puts the plan
    back, then travels on untouched. Ablation (performed): delete the
    `_undo_terminal_plan_write` call in the `BaseException` arm — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )

    def faulted(*a, **kw):
        raise OSError("disk fault outside _run_git")

    monkeypatch.setattr(verify, "finalize_commit", faulted)

    engine.run()

    assert _kinds(engine, "run-crash")  # the fault travelled on, untouched

    assert [m.target for m in tree.marks] == ["done"] and len(tree.restores) == 1
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "built"


def test_mark_done_failure_pauses_and_resume_marks_again(project_v7, monkeypatch):
    """A mark the chokepoint cannot make (here a held plan lock) pauses at
    `ticket-tree` with the unit verified but uncommitted, still COMMITTING; a
    resume re-drives the window and the mark rides the commit then. A fault
    after a partial write is put back first. Ablation (performed): swallow the
    `TicketsError` in `_terminal_plan_write` and the unit commits at `built` with
    no pause — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    _disk_restores(tree, monkeypatch)
    tree.faults["mark"] = TicketsLockBusy("the lock on the plan is held elsewhere")
    engine, _ = make_engine(
        project_v7, tree, script=[_builds(project_v7, tree, plan)], max_stories=1
    )

    summary = engine.run()

    state = load_state(engine.run_dir)
    task = state.tasks["1.1"]
    assert summary.paused and state.paused_stage == PAUSE_TICKET_TREE
    assert state.paused_story_key == "1.1" and "marking 1.1 done failed" in state.paused_reason
    assert task.phase == Phase.COMMITTING and not task.commit_sha
    assert verify.rev_parse_head(project_v7.project) == task.baseline_commit
    assert tree.marks == [] and tree.restores == []  # nothing was written to undo

    del tree.faults["mark"]
    state.clear_pause()
    resumed = TicketsEngine(
        paths=engine.paths,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=state,
        ticket_tree_factory=lambda root, pin, folder=None: tree,
    )
    resumed.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert [(m.target, m.reason) for m in tree.marks] == [("done", "accept")]
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"


class _MountTree:
    """The fake as a unit mount's own tree, as `tickets.py --project-root
    <mount>` reads it: `find` names the mount's copy of the plan, and a written
    mark lands on that copy; the rows stay the shared fake's."""

    def __init__(self, tree: FakeTicketTree, main: Path, root: Path) -> None:
        self.tree, self.main, self.root = tree, main, root
        self.project_root, self.pinned_initiative = root, tree.pinned_initiative

    def _here(self, plan) -> Path:
        return self.root / Path(plan).relative_to(self.main)

    def find(self, ref):
        found = self.tree.find(ref)
        return dataclasses.replace(found, plan=str(self._here(found.plan)))

    def mark(self, ref, target, *, expected_plan, **kw):
        main_plan = self.main / Path(expected_plan).relative_to(self.root)
        result = self.tree.mark(ref, target, expected_plan=main_plan, **kw)
        if result.outcome == "written":
            _write_plan(Path(expected_plan), target)
        return result

    def restore_preimage(self, plan, data, journal=None):
        self.tree.restore_preimage(plan, data, journal)


def test_worktree_unit_marks_the_mounts_plan_on_an_epic_branch(project_v7):
    """Under worktree isolation the unit branch is `bmad-loop/<run>/<epic-slug>/<ref>`,
    the mark is made by the mount's own tree on the mount's copy of the plan —
    so `done` rides the unit commit — and the merge-back carries it to the
    target with the code. Ablation (performed): mark through the run's main tree
    (`self.ticket_tree` in `_terminal_plan_write`) and the cross-check refuses
    the mount's plan — the unit pauses and this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    main = project_v7.project
    pol = dataclasses.replace(_policy(), scm=ScmPolicy(isolation="worktree", delete_branch=False))
    engine, _ = make_engine(
        project_v7, tree, policy=pol, script=[_builds(project_v7, tree, plan)], max_stories=1
    )
    engine._ticket_tree_factory = lambda root, pin, folder=None: (  # type: ignore[method-assign]
        tree if root == main else _MountTree(tree, main, root)
    )

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.branch == f"bmad-loop/test-run/{task.ticket.epic_slug}/1.1"
    assert task.ticket.epic_slug and verify.branch_exists(main, task.branch)
    assert [(m.target, m.reason, m.expected_plan) for m in tree.marks] == [("done", "accept", plan)]
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "done"
    # text, not bytes: the merge-back checks the file out under autocrlf on Windows
    assert (main / "src.txt").read_text(encoding="utf-8") == "cart model\n"


def test_never_mark_before_code_lands(project_v7, monkeypatch):
    """A unit that never reaches its commit is never marked `done`: here every
    dev leg builds the plan but names another ticket, so verification retries
    and then escalates with the code uncommitted. The only marks are the repair
    leg's re-open; `done` would let `next` unblock dependents of code that is
    not in any commit. Ablation (performed): call `_terminal_plan_write` from
    `_record_dev_spec` (after every dev session, before verify) and a `done`
    mark appears — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    wrong = _builds(project_v7, tree, plan, ticket="9")
    engine, adapter = make_engine(project_v7, tree, script=[wrong, wrong], max_stories=1)

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert len(adapter.sessions) == 2 and not task.commit_sha
    assert task.phase in (Phase.ESCALATED, Phase.DEFERRED)
    assert "done" not in [m.target for m in tree.marks]
    assert [(m.target, m.reason) for m in tree.marks] == [("in-progress", "repair")]


def test_repair_leg_regresses_via_chokepoint_reason_repair(project_v7, monkeypatch):
    """A repair leg re-opens the built plan through the chokepoint — `mark 1.1
    in-progress`, reason `repair` — before it launches, so the session resumes
    an `in-progress` plan with the feedback file as its last backticked token;
    the repaired unit is then accepted (`done`, reason `accept`). The failed leg
    never recorded its spec, so the re-open keys on the bound plan. Ablations
    (performed): drop the tickets `_reset_spec_for_repair` override and the plan
    is never re-opened; drop the `_spec_status_write` override and the re-open
    is a raw status write with no mark — each FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    seen: list[str] = []
    wrong = _builds(project_v7, tree, plan, ticket="9", seen=seen)
    right = _builds(project_v7, tree, plan, seen=seen)
    engine, adapter = make_engine(project_v7, tree, script=[wrong, right], max_stories=1)

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and len(adapter.sessions) == 2
    assert [(m.target, m.reason) for m in tree.marks] == [
        ("in-progress", "repair"),
        ("done", "accept"),
    ]
    assert seen == ["", "in-progress"]
    repair = adapter.sessions[1].prompt
    assert repair.startswith(f"/{engine._dev_skill()} ticket 1.1 — resume")
    assert repair.rsplit("`", 2)[-2].endswith(".md") and "Verification evidence" in repair
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"


# ------------------------------------------------- plan-status writers (R19)
#
# Every engine status write goes through `Engine._spec_status_write`; tickets mode
# maps it onto `TicketTree.mark` with a plan-transition reason or refuses it with
# nothing written (`ticket-status-write-refused`). One row per engine writer site.


def _marking_tree(tree: FakeTicketTree, plan: Path, monkeypatch) -> None:
    """Make the fake's written marks land on the plan file too, as the real
    `tickets.py mark` does, so a read-back after the mark sees them."""
    real = tree.mark

    def mark(ref, target, **kw):
        result = real(ref, target, **kw)
        if result.outcome == "written":
            _write_plan(plan, target)
        return result

    monkeypatch.setattr(tree, "mark", mark)


def _refusals(engine: TicketsEngine) -> list[tuple[str, str, str]]:
    return [
        (e["site"], e["target"], e["cause"]) for e in _kinds(engine, "ticket-status-write-refused")
    ]


def test_salvage_review_timeout_marks_built_not_done(project_v7, monkeypatch):
    """`review.on_timeout = "salvage-if-done"` converges a tickets review through
    the chokepoint. A plan the dying review left `in-review` is marked `built` with
    reason `salvage` against the bound plan, then gated; a plan already at `built`
    salvages with no write; and salvage never writes `done` — that pair has no
    plan-transition reason, so it is a typed, journaled refusal with nothing
    written. Ablations (performed): drop the `("salvage", "built")` mapping and
    the in-review arm is refused instead of marked — the marks assertion FAILS;
    map `("salvage", "done")` too and the fake's transition table raises instead
    of the journaled refusal — the `done` row FAILS; drop the tickets
    `_spec_status_write` override and the plan is reset on disk with no `mark` —
    the marks assertion FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="in-review"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    task.spec_file = str(plan)
    gated: list[str] = []

    def gate(task, **kw):
        gated.append(status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))))
        return verify.VerifyOutcome.retry("stub gate")

    monkeypatch.setattr(engine, "_review_verify_gate", gate)
    _marking_tree(tree, plan, monkeypatch)
    timed_out = SessionResult(status="timeout")

    _write_plan(plan, "in-review")
    assert engine._salvage_review_timeout(task, timed_out) is False  # the stub gate
    assert [(m.ref, m.target, m.reason, m.expected_plan, m.outcome) for m in tree.marks] == [
        ("1.1", "built", "salvage", plan, "written")
    ]
    assert gated == ["built"] and _refusals(engine) == []

    _write_plan(plan, "built")
    assert engine._salvage_review_timeout(task, timed_out) is False
    assert gated == ["built", "built"]
    assert len(tree.marks) == 1 and _refusals(engine) == []

    with pytest.raises(SpecStatusWriteRefused):
        engine._spec_status_write(
            task, plan, "done", "salvage", confine_root=engine.workspace.paths.project
        )
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "built"
    assert _refusals(engine) == [("salvage", "done", "no-plan-reason")]
    assert len(tree.marks) == 1


def test_reconcile_generic_frontmatter_refused_in_tickets_mode(project_v7):
    """The generic reconcile promotes a spec from its own `## Auto Run Result`
    prose. A plan's status moves only through `mark`, and no plan-transition reason
    lets a session's prose write `built`, so tickets mode refuses it: the plan stays
    `in-progress` for verification to judge. Ablation (performed): drop the tickets
    override and the plan is rewritten to `built` — this FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="in-progress"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "in-progress")
    plan.write_bytes(plan.read_bytes() + b"\n## Auto Run Result\n\nStatus: built\n")
    result = {"spec_file": str(plan), "status": "in-progress"}

    fm = parse_frontmatter(plan.read_text(encoding="utf-8"))
    status = engine._reconcile_generic_terminal_frontmatter(task, result, plan, fm)

    assert status == "in-progress" and result["status"] == "in-progress"
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "in-progress"
    assert _refusals(engine) == [("reconcile", "built", "no-plan-reason")]
    assert _kinds(engine, "spec-status-reconciled") == [] and tree.marks == []


def test_adoption_routes_through_chokepoint(project_v7, monkeypatch):
    """Adopting a kept unit as finished is the orchestrator's acceptance: `mark
    <ref> done` with reason `accept`, against the bound plan, read back from disk.
    Ablation (performed): drop the tickets override and the plan is set `done`
    with no `mark` — `tree.marks` is empty and this FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="built"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    task.spec_file = str(plan)
    task.phase = Phase.COMMITTING
    _write_plan(plan, "built")
    _marking_tree(tree, plan, monkeypatch)

    engine._apply_adoption(task)

    assert [(m.ref, m.target, m.reason, m.expected_plan) for m in tree.marks] == [
        ("1.1", "done", "accept", plan)
    ]
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "done"
    assert task.phase == Phase.COMMITTING and _refusals(engine) == []


def test_adoption_park_refused_and_escalates(project_v7):
    """An adopted unit that owes operator actions would be set `awaiting-operator`,
    which is a session status, not one `mark` writes: refused, nothing written, and
    the adoption escalates as any unwritable spec does. Ablation (performed): map
    `("adoption", "awaiting-operator")` in `PLAN_WRITE_REASONS` and the fake's
    transition table raises a different error — the refusal row FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="built"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    task.spec_file = str(plan)
    task.phase = Phase.COMMITTING
    task.operator_actions = ["publish the DNS record"]
    _write_plan(plan, "built")

    with pytest.raises(RunPaused):
        engine._apply_adoption(task)

    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "built"
    assert _refusals(engine) == [("adoption", "awaiting-operator", "no-plan-reason")]
    assert tree.marks == [] and task.phase == Phase.ESCALATED


def test_repair_reset_marks_in_progress_through_chokepoint(project_v7):
    """A repair leg re-opens the bound plan with `mark <ref> in-progress`, reason
    `repair`, then strips the stale terminal section. Ablation (performed): drop the
    tickets override and the reset is a raw `reset_spec_status` — no mark."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="built"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    task.spec_file = str(plan)
    _write_plan(plan, "built")
    plan.write_bytes(plan.read_bytes() + b"\n## Auto Run Result\n\nStatus: built\n")

    engine._reset_spec_for_repair(task)

    assert [(m.ref, m.target, m.reason) for m in tree.marks] == [("1.1", "in-progress", "repair")]
    assert "## Auto Run Result" not in plan.read_text(encoding="utf-8")


def test_status_write_to_another_file_is_refused(project_v7):
    """A mapped write aimed at anything but the bound plan is refused unwritten:
    the chokepoint marks tickets, never arbitrary files. Ablation (performed): drop
    the bound-plan check and the mark lands on the ticket instead — this FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="built"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    other = project_v7.initiative_folder / "other-plan.md"
    _write_plan(other, "built")

    with pytest.raises(SpecStatusWriteRefused):
        engine._spec_status_write(
            task, other, "in-progress", "repair", confine_root=project_v7.project
        )

    assert _refusals(engine) == [("repair", "in-progress", "not-bound-plan")]
    assert tree.marks == []


def _stray_board(engine: TicketsEngine, key: str) -> tuple[Path, bytes]:
    """A sprint board at the configured path that nothing in tickets mode owns."""
    board = engine.paths.sprint_status
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_bytes(f"development_status:\n  {key}: ready-for-dev\n".encode())
    return board, board.read_bytes()


def test_tickets_unit_never_touches_sprint_status(project_v7):
    """Sprint bookkeeping is neutral in tickets mode: a parked plan does not mirror
    onto a board (`_post_dev_state_sync`), and a restored task's
    `board_advance_intended` is never carried (`_carry_board_advance`) — a stray
    board keeps its bytes and no `board-advance-*` row is journaled. Ablation
    (performed): drop either override and the board's row is advanced — the bytes
    assertion FAILS."""
    tree = _tree(project_v7, in_progress=(ticket_row("1.1", status="in-progress"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, verify.AWAITING_OPERATOR)
    board, before = _stray_board(engine, "1.1")

    engine._post_dev_state_sync(task, {"spec_file": str(plan)})
    assert task.board_advance_intended in (None, "")

    task.board_advance_intended = "done"
    engine._carry_board_advance(task)

    assert board.read_bytes() == before
    assert [e for e in engine.journal.entries() if e["kind"].startswith("board-advance")] == []


# ------------------------------------------------ checkpoints (R12) and trailers
#
# Upstream's contract: a refined `plan_checkpoint` entry has its story file
# approved before dispatch; an unrefined one dispatches `Halt after planning.`,
# pauses for plan approval, then dispatches again — with no interim commit, because
# build-auto routes on the uncommitted plan before its clean-tree check; a
# `done_checkpoint` pauses after the ticket.


def _resumed(engine: TicketsEngine, tree: FakeTicketTree, script: list):
    """The engine `bmad-loop resume` builds from the persisted state: the resume
    path's story-file approval and epic-closure answer, then the pause cleared."""
    state = load_state(engine.run_dir)
    tickets_source.approve_paused_story_file(state)
    tickets_source.resume_epic_closure(state)
    state.clear_pause()
    adapter = MockAdapter(list(script))
    resumed = TicketsEngine(
        paths=engine.paths,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=state,
        ticket_tree_factory=lambda root, pin, folder=None: tree,
    )
    return resumed, adapter


def _plans_then_builds(layout: V7Layout, tree: FakeTicketTree, plan: Path, *, seen=None):
    """A build-auto that honors `Halt after planning.`: on the plan-halt leg
    (`BMAD_LOOP_PLAN_HALT`) it writes the plan at `ready-for-dev`, no code, and
    marks the result `plan_halt`, as the adapter synthesizes it; any other leg
    builds (`_builds`)."""
    build = _builds(layout, tree, plan, seen=seen)

    def effect(spec: SessionSpec) -> SessionResult:
        if not spec.env.get("BMAD_LOOP_PLAN_HALT"):
            return build(spec)
        cwd = Path(spec.cwd)
        here = cwd / plan.relative_to(layout.project)
        if seen is not None:
            seen.append("")
        _write_plan(here, "ready-for-dev", baseline=verify.rev_parse_head(cwd))
        result = {
            "workflow": "auto-dev",
            "spec_file": str(here),
            "status": "ready-for-dev",
            "plan_halt": True,
        }
        return SessionResult(status="completed", result_json=result)

    return effect


def test_refined_plan_checkpoint_waits_for_approval_then_dispatches(project_v7):
    """A refined `plan_checkpoint` ticket pauses before any session for its story
    file's approval, unregistered; the operator's resume records the approval and
    the resumed run dispatches the ticket once, as a plain `ticket <ref>` (no plan
    halt: the approved story file is the checkpoint), and commits it. Ablation
    (performed): drop the append in `tickets_source.approve_paused_story_file` and
    the resumed run pauses at `plan-checkpoint` again — this FAILS."""
    row = ticket_row("1.1", refined=True, plan_checkpoint=True, file="story-cart-model.md")
    tree = _tree(project_v7, ready_to_start=(row,))
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(project_v7, tree)

    assert engine.run().paused
    paused = load_state(engine.run_dir)
    assert (paused.paused_stage, paused.paused_story_key) == (PAUSE_PLAN_CHECKPOINT, "1.1")
    assert paused.tasks == {} and adapter.sessions == []

    resumed, adapter = _resumed(engine, tree, [_builds(project_v7, tree, plan)])
    resumed.run()

    state = load_state(engine.run_dir)
    assert state.ticket_story_approvals == ["1.1"]
    assert state.paused_stage is None and state.tasks["1.1"].phase == Phase.DONE
    [session] = adapter.sessions
    assert session.prompt.startswith(f"/{engine._dev_skill()} ticket 1.1 — ")
    assert "Halt after planning." not in session.prompt
    assert "BMAD_LOOP_PLAN_HALT" not in session.env
    assert len(_kinds(resumed, "ticket-pick-halt")) == 1


def test_unrefined_plan_checkpoint_two_leg(project_v7):
    """An unrefined `plan_checkpoint` ticket runs two legs: leg 1 dispatches
    `ticket 1.1 Halt after planning.` (with `BMAD_LOOP_PLAN_HALT`), its
    `ready-for-dev` plan is verified, and the run pauses at `plan-checkpoint` with
    the unit registered at DEV_VERIFY; the resume re-dispatches a plain `ticket
    1.1`, which builds and commits. The checkpoint-family rows keep the mixin's
    shape (`plan-halt`, `checkpoint-pause`, `checkpoint-resume`), and the resume
    approves no story file. Ablation (performed): drop the tickets `_drive_story`
    override and leg 1 commits without pausing — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", plan_checkpoint=True),))
    plan = Path(tree.finds["1.1"].plan)
    effect = _plans_then_builds(project_v7, tree, plan)
    engine, adapter = make_engine(project_v7, tree, script=[effect])
    skill = engine._dev_skill()

    assert engine.run().paused

    paused = load_state(engine.run_dir)
    task = paused.tasks["1.1"]
    assert (paused.paused_stage, paused.paused_story_key) == (PAUSE_PLAN_CHECKPOINT, "1.1")
    assert task.phase == Phase.DEV_VERIFY and task.plan_checkpoint_pending
    assert not task.plan_review_owed and not task.commit_sha
    [leg_one] = adapter.sessions
    assert leg_one.prompt.startswith(f"/{skill} ticket 1.1 Halt after planning. ")
    assert leg_one.env["BMAD_LOOP_PLAN_HALT"] == "1"
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "ready-for-dev"
    assert [e["story_key"] for e in _kinds(engine, "plan-halt")] == ["1.1"]
    [pause] = _kinds(engine, "checkpoint-pause")
    assert (pause["story_key"], pause["checkpoint"]) == ("1.1", "plan")
    assert [e["story_key"] for e in _kinds(engine, "plan-halt-proof-of-work-skipped")] == ["1.1"]

    resumed, adapter = _resumed(engine, tree, [effect])
    resumed.run()

    state = load_state(engine.run_dir)
    assert state.tasks["1.1"].phase == Phase.DONE and state.tasks["1.1"].commit_sha
    assert state.ticket_story_approvals == []
    [leg_two] = adapter.sessions
    assert leg_two.prompt.startswith(f"/{skill} ticket 1.1 — ")
    assert "Halt after planning." not in leg_two.prompt
    assert "BMAD_LOOP_PLAN_HALT" not in leg_two.env
    assert [e["checkpoint"] for e in _kinds(resumed, "checkpoint-resume")] == ["plan"]
    assert len(_kinds(resumed, "plan-halt")) == 1


def test_plan_checkpoint_leg2_dispatches_on_uncommitted_ready_for_dev_plan(project_v7, monkeypatch):
    """No interim commit (R12): between the legs the `ready-for-dev` plan is an
    uncommitted change on the baseline, leg 2 launches on it as it stands, and
    the unit's single squash commit carries the plan — accepted `done` — with
    the code. Ablation (performed): commit the plan in `_pause_plan_checkpoint`
    (an interim commit) and the HEAD-at-baseline assertion FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", plan_checkpoint=True),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    seen: list[str] = []
    effect = _plans_then_builds(project_v7, tree, plan, seen=seen)
    engine, _ = make_engine(project_v7, tree, script=[effect])
    baseline = verify.rev_parse_head(project_v7.project)
    rel = plan.relative_to(project_v7.project).as_posix()

    assert engine.run().paused

    assert load_state(engine.run_dir).paused_stage == PAUSE_PLAN_CHECKPOINT
    assert verify.rev_parse_head(project_v7.project) == baseline
    assert git(project_v7.project, "status", "--porcelain", "--", rel).strip().endswith(rel)

    resumed, _ = _resumed(engine, tree, [effect])
    resumed.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert seen == ["", "ready-for-dev"]
    assert task.phase == Phase.DONE and task.baseline_commit == baseline
    assert git(project_v7.project, "rev-list", "--count", f"{baseline}..HEAD").strip() == "1"
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"
    assert git(project_v7.project, "show", f"{task.commit_sha}:src.txt").strip() == "cart model"
    assert git(project_v7.project, "status", "--porcelain") == ""


def _builds_then_more(layout: V7Layout, tree: FakeTicketTree, plan: Path, *more: str):
    """`_builds`, after which the tree still offers ``more`` tickets."""
    build = _builds(layout, tree, plan)

    def effect(spec: SessionSpec) -> SessionResult:
        result = build(spec)
        tree.next_view = _tree(
            layout, ready_to_start=tuple(ticket_row(ref) for ref in more)
        ).next_view
        return result

    return effect


def test_done_checkpoint_pauses_after_commit(project_v7):
    """A `done_checkpoint` ticket pauses at `story-checkpoint` once its unit is
    committed, while another ticket is still to dispatch; the pause names the
    commit and no second session starts. Ablation (performed): answer
    `done_checkpoint=False` from `_entry_checkpoint_flags` and the run dispatches
    1.2 instead — this FAILS."""
    rows = (ticket_row("1.1", done_checkpoint=True),)
    tree = _tree(project_v7, ready_to_start=rows, others=(ticket_row("1.2"),))
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7, tree, script=[_builds_then_more(project_v7, tree, plan, "1.2")]
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    task = state.tasks["1.1"]
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_STORY_CHECKPOINT, "1.1")
    assert task.phase == Phase.DONE and task.commit_sha
    assert len(adapter.sessions) == 1 and set(state.tasks) == {"1.1"}
    [pause] = _kinds(engine, "checkpoint-pause")
    assert (pause["checkpoint"], pause["commit"]) == ("story", task.commit_sha)


def test_done_checkpoint_skipped_if_last(project_v7):
    """A `done_checkpoint` on the run's last ticket does not pause: nothing is
    left to dispatch, so the run finishes and journals `checkpoint-skip-last`.
    Ablation (performed): answer False from `_schedule_complete` and the run
    pauses at `story-checkpoint` — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", done_checkpoint=True),))
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(project_v7, tree, script=[_builds(project_v7, tree, plan)])

    summary = engine.run()

    state = load_state(engine.run_dir)
    assert not summary.paused and state.finished
    assert state.tasks["1.1"].phase == Phase.DONE
    assert [e["story_key"] for e in _kinds(engine, "checkpoint-skip-last")] == ["1.1"]
    assert _kinds(engine, "checkpoint-pause") == []


@pytest.mark.parametrize(
    "template",
    ["", "feat: {story_key}\n\nBuilt by the loop.\n\nSigned-off-by: Loop <loop@example.com>"],
    ids=["default", "template-with-trailer"],
)
def test_unit_commit_has_ticket_trailers(project_v7, template):
    """The unit's commit carries `Bmad-Ticket: <ref>` and `Bmad-Plan: <plan>`
    (repo-relative) as git trailers — joined to a template's own trailer block,
    which git still parses as one. Ablation (performed): drop the tickets
    `_commit_message` override and no trailer is parsed — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    pol = dataclasses.replace(_policy(), scm=ScmPolicy(commit_message_template=template))
    engine, _ = make_engine(project_v7, tree, policy=pol, script=[_builds(project_v7, tree, plan)])

    engine.run()

    sha = load_state(engine.run_dir).tasks["1.1"].commit_sha
    trailers = git(project_v7.project, "log", "-1", "--format=%(trailers:only,unfold)", sha)
    rel = plan.relative_to(project_v7.project).as_posix()
    want = ["Bmad-Ticket: 1.1", f"Bmad-Plan: {rel}"]
    if template:
        want.insert(0, "Signed-off-by: Loop <loop@example.com>")
    assert trailers.strip().splitlines() == want
    subject = git(project_v7.project, "log", "-1", "--format=%s", sha).strip()
    assert subject == ("feat: 1.1" if template else "story 1.1: implemented via bmad-loop")


def test_ticket_trailer_plan_moves_with_a_renamed_project(project_v7, tmp_path):
    """`Bmad-Plan` names the plan under the live project after a project move,
    not dropped because the binding's launch-time path left the checkout.
    Ablation (performed): relativize `Path(ticket.plan)` and the trailer is
    omitted — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    live = Path(tree.finds["1.1"].plan)
    rel = live.relative_to(project_v7.project)
    launched = tmp_path / "launched-here"
    engine.state.project = str(launched)
    assert task.ticket is not None
    task.ticket = dataclasses.replace(task.ticket, plan=str(launched / rel))

    assert f"Bmad-Plan: {rel.as_posix()}" in engine._commit_message(task)


def test_dirty_initiative_status_unreadable_pauses(project_v7, monkeypatch):
    """The dirty-initiative gate fails closed: a checkout whose status cannot be
    read pauses at `ticket-tree` before the ticket is registered. Ablation
    (performed): return early on `GitError` and a session runs — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, adapter = make_engine(project_v7, tree)

    def refuse(_repo):
        raise verify.GitError("git status failed")

    monkeypatch.setattr(verify, "dirty_paths", refuse)

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_TICKET_TREE, "1.1")
    assert state.tasks == {} and adapter.sessions == []
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert halt["cause"] == tickets_source.HALT_DIRTY_INITIATIVE


# ---- parking: a session-declared `awaiting-operator` plan becomes a `blocked` park

PARK_ACTIONS = ["buy example.com at the registrar", "publish the _acme TXT record"]


def _write_parked_plan(
    plan: Path,
    status: str,
    *,
    actions=PARK_ACTIONS,
    blocked_reason: str = "",
    baseline: str = "",
) -> None:
    """A plan as a parking build leaves it: the park status and its owed
    `operator_actions` (and, once marked, the operator-park `blocked_reason`)."""
    lines = ["---", "ticket: 1", f"status: {status}"]
    if baseline:
        lines.append(f"baseline_revision: '{baseline}'")
    if blocked_reason:
        lines.append(f"blocked_reason: '{blocked_reason}'")
    lines.append("operator_actions:")
    lines += [f"  - '{a}'" for a in actions]
    lines += ["---", "", "# plan", "", "the agent-doable half is built", ""]
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes("\n".join(lines).encode("utf-8"))


def _parks(layout: V7Layout, tree: FakeTicketTree, plan: Path, *, followup: bool | None = None):
    """A dev session that builds what it can and parks the ticket: code in the
    tree, the plan at `awaiting-operator` with its actions, and the fake's row
    reading the session's own token as `tickets.py` would hand it back.
    ``followup``, when given, is the result's `followup_review_recommended`."""

    def park(spec: SessionSpec) -> SessionResult:
        cwd = Path(spec.cwd)
        here = cwd / plan.relative_to(layout.project)
        baseline = verify.rev_parse_head(cwd)
        (cwd / "src.txt").write_bytes(b"dns client\n")
        _write_parked_plan(here, "awaiting-operator", baseline=baseline)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="awaiting-operator")
        )
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "awaiting-operator"}
        if followup is not None:
            result["followup_review_recommended"] = followup
        return SessionResult(status="completed", result_json=result)

    return park


def _park_marking_tree(tree: FakeTicketTree, plan: Path, monkeypatch) -> None:
    """Make the fake's written marks land on the plan as `tickets.py mark` does:
    status and `blocked_reason` rewritten, the plan's other keys (the owed
    actions) kept."""
    real = tree.mark

    def mark(ref, target, **kw):
        result = real(ref, target, **kw)
        if result.outcome == "written":
            _write_parked_plan(plan, target, blocked_reason=kw.get("blocked") or "")
        return result

    monkeypatch.setattr(tree, "mark", mark)


def _park_record(layout: V7Layout, key: str = "1.1") -> dict:
    from bmad_loop import operatoractions

    return operatoractions.load(layout.project)[key]


def test_park_converted_to_blocked_at_commit(project_v7, monkeypatch):
    """A session that parks the ticket at `awaiting-operator` keeps the v6 park
    verification (both gates hold it to its action list), and the commit window
    converts it: `mark 1.1 blocked --blocked "awaiting operator: <actions>"`
    (reason `park`) on the bound plan, riding the unit's squash with the code and
    the park record — which names its source and ref, for `confirm`. The unit
    ends AWAITING_OPERATOR with a clean tree, so the next pick's dirty-initiative
    gate does not trip. Ablation (performed): drop the park branch from
    `_terminal_plan_write` and the plan is marked `done` from `awaiting-operator`,
    which the transition table refuses and the run pauses — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    engine, adapter = make_engine(project_v7, tree, script=[_parks(project_v7, tree, plan)])
    engine.state.max_stories = 1

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert task.operator_actions == PARK_ACTIONS
    reason = "awaiting operator: " + "; ".join(PARK_ACTIONS)
    assert [(m.ref, m.target, m.reason, m.expected_plan, m.blocked) for m in tree.marks] == [
        ("1.1", "blocked", "park", plan, reason)
    ]
    committed = parse_frontmatter(
        git(
            project_v7.project,
            "show",
            f"{task.commit_sha}:{plan.relative_to(project_v7.project).as_posix()}",
        )
    )
    assert status_of(committed) == "blocked"
    assert committed["blocked_reason"] == reason
    assert committed["operator_actions"] == PARK_ACTIONS
    record = _park_record(project_v7)
    assert (record["source"], record["ref"]) == ("tickets", "1.1")
    assert git(project_v7.project, "show", f"{task.commit_sha}:src.txt").strip() == "dns client"
    assert git(project_v7.project, "status", "--porcelain") == ""
    assert [e["kind"] for e in engine.journal.entries()].count(
        "review-skipped-awaiting-operator"
    ) == 1


def test_ticket_park_records_the_followup_recommendation(project_v7, monkeypatch):
    """A ticket park skips review like a sprint park, so a follow-up review the
    build recommended is carried on the park record beside the source and ref
    `confirm` needs: the operator runs it after confirming the plan.

    Ablation: drop the field from `_write_park_record`'s `record_park` call and
    this fails on the KeyError."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    engine, adapter = make_engine(
        project_v7, tree, script=[_parks(project_v7, tree, plan, followup=True)]
    )
    engine.state.max_stories = 1

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert not any(s.role == "review" for s in adapter.sessions)
    record = _park_record(project_v7)
    assert (record["source"], record["ref"]) == ("tickets", "1.1")
    assert record["followup_review_recommended"] is True
    parked_entry = next(
        e for e in engine.journal.entries() if e["kind"] == "story-awaiting-operator"
    )
    assert parked_entry["followup_review_recommended"] is True


@pytest.mark.parametrize("mark_done", ["auto", "never"])
def test_park_not_marked_done(project_v7, monkeypatch, mark_done):
    """A parked ticket is never accepted: whatever `[tickets] mark_done` says, the
    one mark the commit window makes is the `blocked` park — `done` would unblock
    dependents of a ticket a human still owes work on, and `"never"` must not
    leave the session's `awaiting-operator` (a problems row upstream) behind.
    Ablation (performed): gate the park branch on `mark_done == "auto"` and the
    `never` row writes nothing, committing the plan at `awaiting-operator` — this
    FAILS; drop the branch and the `auto` row tries `done` — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    pol = dataclasses.replace(_policy(), tickets=TicketsPolicy(mark_done=mark_done))
    engine, _ = make_engine(
        project_v7, tree, policy=pol, script=[_parks(project_v7, tree, plan)], max_stories=1
    )

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.AWAITING_OPERATOR
    assert [(m.target, m.reason) for m in tree.marks] == [("blocked", "park")]
    rel = plan.relative_to(project_v7.project).as_posix()
    assert (
        status_of(parse_frontmatter(git(project_v7.project, "show", f"{task.commit_sha}:{rel}")))
        == "blocked"
    )


def _demotes(layout: V7Layout, tree: FakeTicketTree, plan: Path):
    """A review pass that concludes the built ticket still owes human-only
    actions: the plan finalized at `awaiting-operator` with its actions."""

    def review(spec: SessionSpec) -> SessionResult:
        here = Path(spec.cwd) / plan.relative_to(layout.project)
        _write_parked_plan(here, "awaiting-operator")
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status="awaiting-operator")
        )
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "awaiting-operator"}
        return SessionResult(status="completed", result_json=result)

    return review


def _builds_asking_review(layout: V7Layout, tree: FakeTicketTree, plan: Path):
    """`_builds`, with the dev result recommending the follow-up review pass."""
    build = _builds(layout, tree, plan)

    def effect(spec: SessionSpec) -> SessionResult:
        result = build(spec)
        rj = {**(result.result_json or {}), "followup_review_recommended": True}
        return dataclasses.replace(result, result_json=rj)

    return effect


def test_review_demotion_parks_to_blocked(project_v7, monkeypatch):
    """`[operator] on_review_demotion = "park"`: a review that demotes the built
    ticket to `awaiting-operator` is gated (actions owed) and committed as the
    same `blocked` park a dev park gets — no sprint board is touched or recorded
    as intended — and the review prompt offered the park. Ablation (performed):
    drop the `_park_review_demotion` override and the base regresses the sprint
    board and records `board_advance_intended` — this FAILS; restore the False
    `_review_demotion_parks` and the demotion is not taken (no park mark) — this
    FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True),
        operator=OperatorPolicy(on_review_demotion="park"),
    )
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=pol,
        script=[_builds_asking_review(project_v7, tree, plan), _demotes(project_v7, tree, plan)],
        max_stories=1,
    )
    # committed, so the dirty-initiative gate (the board path is in the initiative
    # folder on v7) lets the ticket through
    board, before = _stray_board(engine, "1.1")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "stray board")

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert len(adapter.sessions) == 2
    assert "awaiting-operator" in adapter.sessions[1].prompt
    reason = "awaiting operator: " + "; ".join(PARK_ACTIONS)
    assert [(m.target, m.reason, m.blocked) for m in tree.marks] == [("blocked", "park", reason)]
    rel = plan.relative_to(project_v7.project).as_posix()
    assert (
        status_of(parse_frontmatter(git(project_v7.project, "show", f"{task.commit_sha}:{rel}")))
        == "blocked"
    )
    assert board.read_bytes() == before
    assert task.board_advance_intended in (None, "")


def test_rollback_restores_plan_preimage_through_chokepoint(project_v7, monkeypatch):
    """A plan that was on disk at dispatch is attempt-owned input: when the
    attempt is rolled back, its pre-launch bytes go back through
    `TicketTree.restore_preimage` — the recovery flow's own anchored writer never
    touches a ticket plan — and the plan is confined to the store root
    (`output_folder`). Ablation (performed): return None from
    `TicketsEngine._owned_plan_writer` and the anchored writer restores it
    instead, with no `restore_preimage` call — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1", status="ready-for-dev"),))
    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "ready-for-dev")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "plan")
    _disk_restores(tree, monkeypatch)
    pol = dataclasses.replace(_policy(), scm=ScmPolicy(rollback_on_failure=False))
    engine, _ = make_engine(project_v7, tree, policy=pol)
    task = _bound(engine, tree)
    task.baseline_commit = verify.rev_parse_head(project_v7.project)
    task.baseline_untracked = []
    operator = plan.read_bytes().replace(b"# plan", b"# plan, amended before launch")
    task.dispatched_spec_file = str(plan.resolve())
    task.dispatched_spec_snapshot = operator
    # the failed child put the plan back at HEAD, discarding the pre-launch edit

    engine._rollback_or_pause(task)

    assert tree.restores == [(plan.resolve(), operator)]
    assert plan.read_bytes() == operator
    assert engine._spec_confine_root(task) == engine.workspace.paths.output_folder


# ------------------------------------------------------------- epic boundary
#
# A finished epic this run committed a ticket of gets its retrospective (under
# `gates.retrospective = "auto"`), its closure recorded and a pause for a person to
# close it with bmad-agent-po. The fake's `status` view is the epic table: each test
# lists the epics and moves the built ticket to `built` there, as tickets.py
# derives it.


def _epic(slug: str = "epic-cart", status: str = "in-progress", epic_id: str = "1") -> EpicRow:
    return EpicRow(
        slug=slug,
        id=epic_id,
        status=status,
        after=(),
        gated_by=(),
        blocks=(),
        next_id=1,
        folder=slug,
        refined=True,
    )


def _unsliced(slug: str = "epic-payment", epic_id: str = "3") -> EpicRow:
    """An epic the initiative declares that has no folder yet (`folder: null`)."""
    return EpicRow(
        slug=slug,
        id=epic_id,
        status="",
        after=(),
        gated_by=(),
        blocks=(),
        next_id=None,
        folder=None,
        refined=False,
    )


def _retro_policy(mode: str = "auto", **kw) -> Policy:
    return dataclasses.replace(_policy(), gates=GatesPolicy(mode="none", retrospective=mode), **kw)


def _epic_tree(
    layout: V7Layout,
    *,
    ready: tuple = (),
    others: tuple = (),
    epics: tuple[EpicRow, ...] | None = None,
) -> FakeTicketTree:
    """1.1 in epic-cart, ready, plus ``ready``/``others``, with the epic table."""
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"), *ready), others=others)
    tree.status_view = dataclasses.replace(tree.status_view, epics=epics or (_epic(),))
    return tree


def _close_epic(tree: FakeTicketTree, slug: str, status: str = "done") -> None:
    """A person closes the epic with bmad-agent-po: its file's status in `status`."""
    epics = tuple(
        dataclasses.replace(e, status=status) if e.slug == slug else e
        for e in tree.status_view.epics
    )
    tree.status_view = dataclasses.replace(tree.status_view, epics=epics)


def _builds_finishing(
    layout: V7Layout, tree: FakeTicketTree, plan: Path, *more: str, done: bool = False
):
    """`_builds`, after which 1.1 reads `built` in `status` (its epic finished),
    or `done` with ``done`` (as `[tickets] mark_done = "auto"` lands it), and the
    tree offers ``more`` tickets."""
    build = _builds(layout, tree, plan)
    status, state = ("done", "done") if done else ("built", "review")

    def effect(spec: SessionSpec) -> SessionResult:
        result = build(spec)
        tickets = tuple(
            dataclasses.replace(t, status=status, state=state) if t.ref == "1.1" else t
            for t in tree.status_view.tickets
        )
        tree.status_view = dataclasses.replace(tree.status_view, tickets=tickets)
        tree.next_view = _tree(
            layout, ready_to_start=tuple(ticket_row(ref, epic="epic-checkout") for ref in more)
        ).next_view
        return result

    return effect


def _retro_doc(layout: V7Layout, slug: str = "epic-cart") -> Path:
    """The epic's retrospective, at the top of its folder in the `tickets/` layout."""
    return layout.initiative_folder / "tickets" / slug / f"{slug}-retrospective.md"


def _write_retro_doc(layout: V7Layout, verdict: str, *, epic: str = "epic-cart") -> Path:
    """The retrospective document as bmad-retrospective finalizes it."""
    doc = _retro_doc(layout)
    lines = [
        "---",
        f"epic: {epic}",
        "date: 2026-10-15",
        f"verdict: {verdict}",
        "criteria: profiled",
        "headless: true",
        "---",
        "",
        "# Retrospective",
        "",
    ]
    doc.write_bytes("\n".join(lines).encode("utf-8"))
    return doc


def test_retro_doc_reads_the_tickets_folder_layout(project_v7):
    """The retrospective sits at the top of the epic folder, which lives in the
    initiative's `tickets/` folder, and `_read_retro_doc` reads it there (the
    flat twin is tests/test_ticketlayout.py's). Ablation (performed): spell
    `<initiative>/<slug>/` in `_retro_doc` and the flat path comes back — this
    FAILS."""
    engine, _ = make_engine(project_v7, _epic_tree(project_v7), policy=_retro_policy())
    initiative = engine.paths.initiative_folder
    assert initiative is not None
    epic = initiative / "tickets" / "epic-cart"
    doc = epic / "epic-cart-retrospective.md"
    doc.write_text("---\nepic: epic-cart\nverdict: accepted\n---\n", encoding="utf-8")
    assert engine._retro_doc("epic-cart") == doc
    errors, verdict, mtime_ns = engine._read_retro_doc("epic-cart")
    assert (errors, verdict) == ([], "accepted") and mtime_ns >= 0


def test_retro_doc_refused_layout_probe_is_an_error_not_a_crash(project_v7, monkeypatch):
    """A layout probe that cannot stat the initiative's `tickets/` folder leaves
    no document to verify: `_read_retro_doc` reports the fault as an error (the
    retro fails and pauses visibly), and the dirty check reads it as dirty —
    neither lets a bare `OSError` out of the epic boundary. Ablation (performed):
    drop the `OSError` catch in `_read_retro_doc` and the probe fault escapes —
    this FAILS."""
    engine, _ = make_engine(project_v7, _epic_tree(project_v7), policy=_retro_policy())
    initiative = engine.paths.initiative_folder
    assert initiative is not None
    target = initiative / "tickets"
    real = os.stat

    def refuse(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refuse)
    errors, verdict, mtime_ns = engine._read_retro_doc("epic-cart")
    assert verdict == "" and mtime_ns == -1
    assert len(errors) == 1 and "Permission denied" in errors[0]
    assert engine._retro_doc_uncommitted("epic-cart")


def _retro_writes(layout: V7Layout, verdict: str = "accepted", *, epic: str = "epic-cart"):
    """A `/bmad-retrospective -H` session: it writes the document and reports
    done through result.json."""

    def effect(spec: SessionSpec) -> SessionResult:
        _write_retro_doc(layout, verdict, epic=epic)
        result = {"workflow": "bmad-retrospective", "status": "done"}
        return SessionResult(status="completed", result_json=result)

    return effect


def _files_of(layout: V7Layout, subject: str) -> list[str]:
    log = git(layout.project, "log", "--format=%H %s").splitlines()
    shas = [line.split(" ", 1)[0] for line in log if line.split(" ", 1)[1] == subject]
    assert len(shas) == 1, (subject, log)
    return git(layout.project, "show", "--name-only", "--format=", shas[0]).split()


def test_ticket_retro_happy_path_commits_doc_only(project_v7):
    """The run's last ticket finishes epic-cart: under `auto` one retro session
    runs `/bmad-retrospective -H epic-cart`, its document lands alone in a
    `chore(retro)` commit, the verdict is recorded as the epic's closure, and the
    run pauses at `epic-boundary` naming bmad-agent-po. The `retro-auto-*` rows
    keep `epic` 0 and carry the slug in `epic_ref`. Ablation (performed): drop
    the `_settle_retro_tree` call in `_ticket_epic_retro` and the document is
    left uncommitted — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan), _retro_writes(project_v7)],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert "close epic-cart with bmad-agent-po" in (state.paused_reason or "")
    assert state.ticket_epic_closures == {"epic-cart": "accepted"}
    assert state.tasks["1.1"].phase == Phase.DONE and "epic-cart-retrospective" not in state.tasks
    retro = adapter.sessions[1]
    assert retro.role == "retro"
    assert retro.prompt.startswith("/bmad-retrospective -H epic-cart\n")
    doc = _retro_doc(project_v7).relative_to(project_v7.project).as_posix()
    assert _files_of(project_v7, "chore(retro): epic-cart retrospective") == [doc]
    assert git(project_v7.project, "status", "--porcelain") == ""
    [start] = _kinds(engine, "retro-auto-start")
    [finished] = _kinds(engine, "retro-auto-finished")
    assert (start["epic"], start["epic_ref"]) == (0, "epic-cart")
    assert (finished["epic"], finished["epic_ref"], finished["verdict"]) == (
        0,
        "epic-cart",
        "accepted",
    )
    assert finished["docs"] == ["epic-cart-retrospective.md"]
    [closure] = _kinds(engine, "ticket-epic-closure")
    assert (closure["epic_ref"], closure["verdict"]) == ("epic-cart", "accepted")
    [halt] = _kinds(engine, "ticket-pick-halt")
    assert (halt["ref"], halt["cause"], halt["stage"]) == (
        "epic-cart",
        "epic-closure",
        PAUSE_EPIC_BOUNDARY,
    )


def test_ticket_retro_verdict_rejected_hard_gates_until_epic_done(project_v7):
    """A `rejected` verdict pauses again on every later pick while epic-cart stays
    open — with nothing else queued, and with epic-checkout's 2.1 ready — and
    only closing the epic (bmad-agent-po sets its file to `done`) lets 2.1 start.
    Ablation (performed): drop the `BOUNDARY_REJECTED` arm in
    `tickets_source.epic_boundary` and the first resumed run finishes instead of
    pausing — this FAILS; drop the closure record
    (`state.ticket_epic_closures[slug] = verdict`) and the resumed runs never
    re-pause — this FAILS."""
    tree = _epic_tree(
        project_v7,
        others=(ticket_row("2.1", epic="epic-checkout"),),
        epics=(_epic(), _epic("epic-checkout", epic_id="2")),
    )
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan), _retro_writes(project_v7, "rejected")],
    )

    assert engine.run().paused
    first = load_state(engine.run_dir)
    assert first.ticket_epic_closures == {"epic-cart": "rejected"}
    assert "holds the run until epic-cart is closed" in (first.paused_reason or "")

    # nothing else queued: the rejected epic itself holds the run
    resumed, adapter = _resumed(engine, tree, [])
    assert resumed.run().paused
    again = load_state(engine.run_dir)
    assert (again.paused_stage, again.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert adapter.sessions == []

    # the next epic is ready: still held
    tree.next_view = _tree(
        project_v7, ready_to_start=(ticket_row("2.1", epic="epic-checkout"),)
    ).next_view
    resumed, adapter = _resumed(engine, tree, [])
    assert resumed.run().paused
    assert adapter.sessions == [] and "2.1" not in load_state(engine.run_dir).tasks
    causes = [h["cause"] for h in _kinds(resumed, "ticket-pick-halt")]
    assert causes == ["epic-closure", "epic-rejected", "epic-rejected"]

    _close_epic(tree, "epic-cart")
    resumed, _ = _resumed(engine, tree, [])
    picked = resumed._pick_next()
    assert isinstance(picked, TicketRef) and picked.key == "2.1"


@pytest.mark.parametrize("done", [False, True], ids=["built", "done"])
def test_ticket_retro_accepted_one_shot_closure_pause(project_v7, done):
    """A non-rejected verdict pauses once: the resumed run acknowledges it,
    journals `epic-closure-not-recorded` (the epic is still open) and finishes
    without pausing again — with the ticket `built`, and with it `done`, where
    the pick's end-of-tree `awaiting-closure` halt also skips the acknowledged
    epic. Ablation (performed): pause on the `BOUNDARY_ACK` step instead of
    acknowledging it and the resumed run pauses again — this FAILS; drop the
    `acked` filter in `decide_pick` and the `done` row re-pauses
    `awaiting-closure` — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[
            _builds_finishing(project_v7, tree, plan, done=done),
            _retro_writes(project_v7, "accepted-with-open-items"),
        ],
    )
    assert engine.run().paused

    resumed, adapter = _resumed(engine, tree, [])
    summary = resumed.run()

    state = load_state(engine.run_dir)
    assert not summary.paused and state.finished
    assert state.ticket_epic_closures == {"epic-cart": "accepted-with-open-items"}
    assert state.ticket_epic_closure_acked == ["epic-cart"]
    [note] = _kinds(resumed, "epic-closure-not-recorded")
    assert (note["epic_ref"], note["verdict"]) == ("epic-cart", "accepted-with-open-items")
    assert len(_kinds(resumed, "ticket-pick-halt")) == 1 and adapter.sessions == []


def test_ticket_closure_recorded_without_its_pause_pauses_again(project_v7):
    """A closure recorded while the pause never landed (the run was killed
    between the two: state carries the verdict, no pause) is not acknowledged on
    restart: the run pauses for it, without a second retrospective, and only a
    resume of that pause lets it go on. Ablation (performed): acknowledge every
    recorded closure regardless of `ticket_epic_closure_resumed` and the restart
    finishes past the epic — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[
            _builds_finishing(project_v7, tree, plan, done=False),
            _retro_writes(project_v7, "accepted-with-open-items"),
        ],
    )
    assert engine.run().paused
    killed = load_state(engine.run_dir)
    killed.clear_pause()
    save_state(engine.run_dir, killed)

    restarted, adapter = _resumed(engine, tree, [])
    assert restarted.run().paused
    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == ("epic-boundary", "epic-cart")
    assert state.ticket_epic_closure_acked == [] and adapter.sessions == []
    assert [h["cause"] for h in _kinds(restarted, "ticket-pick-halt")] == ["epic-closure"] * 2
    assert len(_kinds(restarted, "ticket-epic-closure")) == 1

    resumed, adapter = _resumed(engine, tree, [])
    assert not resumed.run().paused
    assert load_state(engine.run_dir).ticket_epic_closure_acked == ["epic-cart"]
    assert adapter.sessions == []


def test_ticket_retro_existing_doc_already_done(project_v7):
    """A valid retrospective document already in the epic folder is the retro
    already done: no retro session runs, `retro-auto-skipped` says
    `already-done`, and its verdict is the closure the run pauses on. Ablation
    (performed): skip the existing-document read in `_ticket_epic_retro` and a
    second session is dispatched — this FAILS."""
    _write_retro_doc(project_v7, "accepted-with-open-items")
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "retro by hand")
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan)],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_EPIC_BOUNDARY
    assert state.ticket_epic_closures == {"epic-cart": "accepted-with-open-items"}
    assert [s.role for s in adapter.sessions] == ["dev"]
    [skipped] = _kinds(engine, "retro-auto-skipped")
    assert (skipped["epic"], skipped["epic_ref"], skipped["reason"]) == (
        0,
        "epic-cart",
        "already-done",
    )


def test_ticket_epic_boundary_fires_at_pick_after_resume(project_v7):
    """The epic finishes behind a pause (1.1's done_checkpoint, with 2.1 ready):
    the boundary is read from the tree at the resumed run's first pick, so the
    retro runs and the run pauses for closure before 2.1 is dispatched.
    Ablation (performed): drop the `gate=` from `_pick_next`'s decide and the
    resumed run dispatches 2.1 — this FAILS."""
    tree = _epic_tree(
        project_v7,
        others=(ticket_row("2.1", epic="epic-checkout"),),
        epics=(_epic(), _epic("epic-checkout", epic_id="2")),
    )
    tree.next_view = _tree(
        project_v7, ready_to_start=(ticket_row("1.1", done_checkpoint=True),)
    ).next_view
    tree.finds["1.1"] = dataclasses.replace(
        tree.finds["1.1"], row=ticket_row("1.1", done_checkpoint=True)
    )
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan, "2.1")],
    )
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_STORY_CHECKPOINT
    assert _kinds(engine, "retro-auto-start") == []

    resumed, adapter = _resumed(engine, tree, [_retro_writes(project_v7)])
    assert resumed.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert [s.role for s in adapter.sessions] == ["retro"]
    assert "2.1" not in state.tasks
    assert state.ticket_epic_closures == {"epic-cart": "accepted"}


@pytest.mark.parametrize(
    ("scope", "paused"),
    [({"story_filter": "1.1"}, False), ({"ticket_epic_filter": "epic-cart"}, True)],
    ids=["story", "epic"],
)
def test_story_scope_run_finishes_past_the_epic_its_ticket_finished(project_v7, scope, paused):
    """A `--story` run admits no epic-level cause: the ticket finishing its epic
    commits, no retro runs, `retro-run-end-skipped` names the epic
    `out-of-scope` and the run finishes; an `--epic` run on that epic still
    pauses for its closure. Ablation (performed): drop the `admits_epic` filter
    in `_ticket_epic_boundary_check` and the `--story` run pauses at
    `epic-boundary` — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=_retro_policy("never"),
        script=[_builds_finishing(project_v7, tree, plan)],
        **scope,
    )

    assert engine.run().paused is paused

    state = load_state(engine.run_dir)
    assert state.tasks["1.1"].phase == Phase.DONE
    assert [s.role for s in adapter.sessions] == ["dev"]
    if paused:
        assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
        assert _kinds(engine, "retro-run-end-skipped") == []
        return
    assert state.finished and state.ticket_epic_closures == {}
    [skipped] = _kinds(engine, "retro-run-end-skipped")
    assert (skipped["epic_ref"], skipped["reason"]) == ("epic-cart", "out-of-scope")


def test_unsliced_epic_never_triggers_a_retrospective_or_closure(project_v7):
    """An epic the initiative declares but nobody sliced has no tickets: once the
    run's work is built it gets no retrospective, no closure and no closure pause,
    but the run is not finished either: it stalls naming the epic as not created
    yet (not as one to close). Ablation (performed): word every ticketless epic
    as open, as before, and this FAILS."""
    rest = ticket_row("1.2", after=("1",))
    tree = _epic_tree(project_v7, others=(rest,), epics=(_epic(), _unsliced()))
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan)],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert state.paused_story_key == "epic-payment"
    assert "epic-payment is declared in the initiative but has no folder yet" in (
        state.paused_reason or ""
    )
    assert state.ticket_epic_closures == {}
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert _kinds(engine, "retro-auto-start") == []
    assert _kinds(engine, "ticket-epic-closure") == []


@pytest.mark.parametrize("mode", ["never", "notify"])
def test_retro_never_still_pauses_for_closure(project_v7, mode):
    """`gates.retrospective` `never` or `notify` runs no retro session, but the
    finished epic still pauses for its closure, recorded as `unknown`. Ablation
    (performed): return from `_ticket_epic_boundary_check` when the gate is not
    `auto` and the run finishes — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, adapter = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(mode),
        script=[_builds_finishing(project_v7, tree, plan)],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert state.ticket_epic_closures == {"epic-cart": "unknown"}
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert _kinds(engine, "retro-auto-start") == []


def test_ticket_retro_wrong_epic_fails_and_pauses_on_its_output(project_v7):
    """A document whose frontmatter names another epic is no retrospective of
    this one: `retro-auto-failed`, nothing committed, and the uncommitted
    output pauses at `epic-boundary` before a closure is recorded, so a resume
    re-enters the boundary. Ablation (performed): drop the `epic` check in
    `_read_retro_doc` and the retro succeeds — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[
            _builds_finishing(project_v7, tree, plan),
            _retro_writes(project_v7, epic="epic-checkout"),
        ],
    )

    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_EPIC_BOUNDARY
    assert state.ticket_epic_closures == {}
    [failed] = _kinds(engine, "retro-auto-failed")
    assert failed["epic_ref"] == "epic-cart"
    assert any("frontmatter epic is not epic-cart" in e for e in failed["errors"])
    [dirty] = _kinds(engine, "retro-auto-dirty")
    assert dirty["epic_ref"] == "epic-cart"


def test_ticket_retro_commits_a_valid_doc_a_failed_leg_left(project_v7):
    """A retro session that writes a valid document but does not report done
    fails verification and pauses on the uncommitted document. Resumed with it
    still uncommitted, the boundary takes the document as the retro already done
    and commits it alone, as a success would, before the closure pause.
    Ablation (performed): drop the `_settle_retro_tree` call on the
    `already-done` arm and the document stays uncommitted — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    writes = _retro_writes(project_v7)

    def undone(spec: SessionSpec) -> SessionResult:
        writes(spec)
        return SessionResult(status="completed", result_json={"status": "failed"})

    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan), undone],
    )
    assert engine.run().paused
    assert load_state(engine.run_dir).ticket_epic_closures == {}
    assert _kinds(engine, "retro-auto-dirty")

    resumed, adapter = _resumed(engine, tree, [])
    assert resumed.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")
    assert state.ticket_epic_closures == {"epic-cart": "accepted"}
    assert adapter.sessions == []
    doc = _retro_doc(project_v7).relative_to(project_v7.project).as_posix()
    assert _files_of(project_v7, "chore(retro): epic-cart retrospective") == [doc]
    assert git(project_v7.project, "status", "--porcelain") == ""


@pytest.mark.parametrize("refusal", ["dirty", "git-error", "stop-requested"])
def test_ticket_retro_passing_refusal_records_no_closure(project_v7, monkeypatch, refusal):
    """A refusal that passes (`dirty`, `git-error`, `stop-requested`) records no
    closure, so the next pass runs the retrospective: dirt and a git fault pause
    at `epic-boundary`, and a stop request stops the run. Ablation (performed):
    return `unknown` from those arms, as before, and the closure is recorded and
    the retro never runs on resume — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    build = _builds_finishing(project_v7, tree, plan)
    real_clean = verify.worktree_clean

    def refuse(*a, **k):
        if refusal == "git-error":
            raise verify.GitError("index.lock exists")
        return False

    def stop_requested(run_dir):
        runs._write_stop_request(run_dir, "graceful")
        return True

    def builds_then_refuses(spec: SessionSpec) -> SessionResult:
        result = build(spec)
        if refusal == "stop-requested":
            monkeypatch.setattr(tickets_engine, "graceful_stop_requested", stop_requested)
        else:
            monkeypatch.setattr(verify, "worktree_clean", refuse)
        return result

    engine, _ = make_engine(project_v7, tree, policy=_retro_policy(), script=[builds_then_refuses])
    summary = engine.run()

    state = load_state(engine.run_dir)
    assert state.ticket_epic_closures == {}
    [skipped] = _kinds(engine, "retro-auto-skipped")
    assert (skipped["reason"], skipped["epic_ref"]) == (refusal, "epic-cart")
    if refusal == "stop-requested":
        assert not summary.paused and not state.finished
    else:
        assert summary.paused
        assert (state.paused_stage, state.paused_story_key) == (
            PAUSE_EPIC_BOUNDARY,
            "epic-cart",
        )
    monkeypatch.setattr(verify, "worktree_clean", real_clean)
    monkeypatch.setattr(tickets_engine, "graceful_stop_requested", lambda _d: False)

    resumed, adapter = _resumed(engine, tree, [_retro_writes(project_v7)])
    assert resumed.run().paused
    assert [s.role for s in adapter.sessions] == ["retro"]
    assert load_state(engine.run_dir).ticket_epic_closures == {"epic-cart": "accepted"}


def test_ticket_run_end_status_fault_pauses_before_the_boundary(project_v7):
    """`--max-stories` stops after the ticket that finishes epic-cart; a run-end
    `status` read that faults pauses at `ticket-tree` rather than finishing, so
    the resume still reaches the boundary. Ablation (performed): return after
    the `retro-run-end-skipped` row, as before, and the run finishes with no
    retro — this FAILS."""
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    build = _builds_finishing(project_v7, tree, plan)

    def builds_then_faults(spec: SessionSpec) -> SessionResult:
        result = build(spec)
        tree.faults["status"] = TicketsCrashed("tickets.py died")
        return result

    engine, _ = make_engine(project_v7, tree, policy=_retro_policy(), script=[builds_then_faults])
    engine.max_stories = 1
    assert engine.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_TICKET_TREE, None)
    assert not state.finished
    [skipped] = _kinds(engine, "retro-run-end-skipped")
    assert skipped["reason"] == "board-unreadable"

    del tree.faults["status"]
    resumed, adapter = _resumed(engine, tree, [_retro_writes(project_v7)])
    assert resumed.run().paused
    assert [s.role for s in adapter.sessions] == ["retro"]
    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_EPIC_BOUNDARY, "epic-cart")


def test_epic_file_never_written(project_v7):
    """bmad-loop never writes the epic's own file: across the retro, the closure
    pause, a rejected re-pause and the human's close, `epic-cart.md` keeps its
    bytes and its one commit, and no mark targets anything but the ticket."""
    epic_file = project_v7.initiative_folder / "tickets" / "epic-cart" / "epic-cart.md"
    before = epic_file.read_bytes()
    tree = _epic_tree(project_v7)
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = make_engine(
        project_v7,
        tree,
        policy=_retro_policy(),
        script=[_builds_finishing(project_v7, tree, plan), _retro_writes(project_v7, "rejected")],
    )
    assert engine.run().paused
    resumed, _ = _resumed(engine, tree, [])
    assert resumed.run().paused
    _close_epic(tree, "epic-cart")
    resumed, _ = _resumed(engine, tree, [])
    assert not resumed.run().paused

    rel = epic_file.relative_to(project_v7.project).as_posix()
    assert len(git(project_v7.project, "log", "--format=%H", "--", rel).split()) == 1
    assert epic_file.read_bytes() == before
    assert {m.ref for m in tree.marks} == {"1.1"}


# ---------------------------------------------- shared store: after landing


def _shared_engine(layout: V7Layout, tree: FakeTicketTree, *, isolation: str = "none", **kw):
    """`make_engine` over a shared store, the run's topology recorded from the
    real probe as `runsetup` stamps it (here T3: the nested, ignored store
    repository of `project_v7_store_repo`)."""
    from bmad_loop import ticketstore

    topology = ticketstore.assess_store(load_paths(layout.project, want_tickets=True), isolation)
    assert isinstance(topology, ticketstore.StoreTopology) and topology.layout == "T3"
    pol = kw.pop("policy", None) or _policy()
    if isolation != "none":
        pol = dataclasses.replace(pol, scm=ScmPolicy(isolation=isolation, delete_branch=False))
    return make_engine(layout, tree, policy=pol, store_topology=topology.to_dict(), **kw)


def _builds_shared(layout: V7Layout, tree: FakeTicketTree, plan: Path, *, park: bool = False):
    """A dev session over a shared store: the code in the session's own checkout,
    the plan at `built` (or parked at `awaiting-operator`) in the ONE store every
    checkout shares, the fake's row following."""

    def build(spec: SessionSpec) -> SessionResult:
        cwd = Path(spec.cwd)
        assert spec.expected_spec == str(plan)
        baseline = verify.rev_parse_head(cwd)
        (cwd / "src.txt").write_bytes(b"cart model\n")
        status = "awaiting-operator" if park else "built"
        if park:
            _write_parked_plan(plan, status, baseline=baseline)
        else:
            _write_plan(plan, status, baseline=baseline)
        tree.finds["1.1"] = dataclasses.replace(
            tree.finds["1.1"], row=ticket_row("1.1", status=status)
        )
        tree.next_view = _tree(layout).next_view
        result = {"workflow": "auto-dev", "spec_file": str(plan), "status": status}
        return SessionResult(status="completed", result_json=result)

    return build


def _has_unit_code(checkout: Path) -> bool:
    """Whether the unit's change (`_builds_shared`) is in ``checkout``'s tree; the
    template commits its own `src.txt`. Text, not bytes: the merge-back checks the
    file out under autocrlf on Windows."""
    return (checkout / "src.txt").read_text(encoding="utf-8") == "cart model\n"


def _landed_marks(tree: FakeTicketTree, main: Path, monkeypatch) -> list[tuple[str, bool]]:
    """Record, at every mark, the main checkout's HEAD and whether the unit's
    code is in it."""
    seen: list[tuple[str, bool]] = []
    marking = tree.mark

    def mark(ref, target, **kw):
        seen.append((verify.rev_parse_head(main), _has_unit_code(main)))
        return marking(ref, target, **kw)

    monkeypatch.setattr(tree, "mark", mark)
    return seen


def _resume(engine: TicketsEngine, tree: FakeTicketTree) -> TicketsEngine:
    """A fresh process over the run as its state file left it."""
    state = load_state(engine.run_dir)
    state.clear_pause()
    return TicketsEngine(
        paths=load_paths(engine.paths.project, want_tickets=True),
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=state,
        ticket_tree_factory=lambda root, pin, folder=None: tree,
    )


def _plan_status(plan: Path) -> str:
    return status_of(parse_frontmatter(plan.read_text(encoding="utf-8")))


def test_shared_store_marks_done_only_after_merge(project_v7_store_repo, monkeypatch):
    """A shared store (T3) under worktree isolation: the unit's commit cannot carry
    the plan, so the acceptance is latched in the COMMITTING save and made only
    once the merge-back has put the code on the target — `mark 1.1 done` (reason
    `accept`) on the main store's plan, the main checkout already holding the
    code — then the latch is cleared. Ablation (performed): let
    `_terminal_plan_write` mark a shared store in the window and the mark runs in
    the mount before the merge, the main checkout without the code — this
    FAILS."""
    layout = project_v7_store_repo
    main = layout.project
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    at_mark = _landed_marks(tree, main, monkeypatch)
    engine, _ = _shared_engine(
        layout,
        tree,
        isolation="worktree",
        script=[_builds_shared(layout, tree, plan)],
        max_stories=1,
    )

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha and task.worktree_path
    assert [(m.target, m.reason, m.expected_plan, m.outcome) for m in tree.marks] == [
        ("done", "accept", plan, "written")
    ]
    assert at_mark == [(verify.rev_parse_head(main), True)]
    assert (task.ticket_done_pending, task.ticket_park_pending) == (False, "")
    assert (task.ticket_premark_status, task.ticket_premark_digest) == ("", "")  # T1 only
    assert [e["target"] for e in _kinds(engine, "ticket-mark-latched")] == ["done"]
    assert _plan_status(plan) == "done"
    assert git(main, "status", "--porcelain") == ""


def test_shared_store_park_after_landing(project_v7_store_repo, monkeypatch):
    """A park in a shared store follows the same placement: the commit window
    writes nothing, the park's `blocked` reason is latched, and `mark 1.1 blocked
    --blocked "awaiting operator: …"` (reason `park`) runs once the unit's commit
    is on the target — never `done`. Ablation (performed): latch the park as
    `ticket_done_pending` and the post-landing mark is an `accept` the table
    refuses from `awaiting-operator` — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _park_marking_tree(tree, plan, monkeypatch)
    at_mark = _landed_marks(tree, layout.project, monkeypatch)
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan, park=True)], max_stories=1
    )

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    reason = "awaiting operator: " + "; ".join(PARK_ACTIONS)
    assert [(m.target, m.reason, m.blocked) for m in tree.marks] == [("blocked", "park", reason)]
    assert at_mark == [(task.commit_sha, True)]
    assert (task.ticket_done_pending, task.ticket_park_pending) == (False, "")
    assert _plan_status(plan) == "blocked"


class _HostDied(BaseException):
    """The host process vanishing: nothing after it runs, nothing after it is saved."""


def test_crash_before_committing_save_has_no_latch(project_v7_store_repo, monkeypatch):
    """The latch rides the COMMITTING save itself: it is set before that save is
    made, so a host lost at the save leaves the unit pre-COMMITTING on disk with
    no latch and no mark — nothing claims a mark for code that never committed.
    Ablation (performed): latch after `_commit`'s save instead of before it and
    the COMMITTING save carries no latch — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )
    latched_at_save: list[bool] = []
    real_save = engine._save

    def save():
        if latched_at_save:
            return  # the host is gone: no later save reaches the disk
        task = engine.state.tasks.get("1.1")
        if task is not None and task.phase == Phase.COMMITTING:
            latched_at_save.append(task.ticket_done_pending)
            raise _HostDied
        real_save()

    monkeypatch.setattr(engine, "_save", save)

    with pytest.raises(_HostDied):
        engine.run()

    assert latched_at_save == [True]
    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase in (Phase.DEV_VERIFY, Phase.REVIEW_VERIFY) and not task.commit_sha
    assert (task.ticket_done_pending, task.ticket_park_pending) == (False, "")
    assert tree.marks == []
    assert _plan_status(plan) == "built"


def test_crash_after_landing_replays_done_on_resume(project_v7_store_repo, monkeypatch):
    """A host lost after the unit's commit landed but before the mark leaves a
    DONE unit with its latch on disk; the resume makes the mark before any pick
    (`resume-ticket-mark`) and clears the latch. Ablation (performed): drop the
    `_replay_post_landing_writes` call from `run()` and the resumed run finishes
    with no mark — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )
    marking = tree.mark

    def host_dies(*a, **kw):
        raise _HostDied

    monkeypatch.setattr(tree, "mark", host_dies)
    with pytest.raises(_HostDied):
        engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert verify.rev_parse_head(layout.project) == task.commit_sha
    assert task.ticket_done_pending is True
    assert tree.marks == [] and _plan_status(plan) == "built"

    monkeypatch.setattr(tree, "mark", marking)
    resumed = _resume(engine, tree)
    resumed.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert [(m.target, m.reason, m.outcome) for m in tree.marks] == [("done", "accept", "written")]
    assert [e["target"] for e in _kinds(resumed, "resume-ticket-mark")] == ["done"]
    assert task.ticket_done_pending is False
    assert _plan_status(plan) == "done"


def test_crash_after_mark_before_clear_is_idempotent(project_v7_store_repo, monkeypatch):
    """A host lost after the mark was written but before the latch was cleared
    replays the mark on resume, which the chokepoint answers as a no-op (`done`
    is already there), and then clears the latch: one write, never a refusal.
    Ablation (performed): drop the `done → done` no-op from
    `check_plan_transition` and the replay is refused (`done` is final) — the
    resume pauses and this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )
    marking = tree.mark

    def mark_then_host_dies(ref, target, **kw):
        marking(ref, target, **kw)
        raise _HostDied

    monkeypatch.setattr(tree, "mark", mark_then_host_dies)
    with pytest.raises(_HostDied):
        engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.ticket_done_pending is True
    assert _plan_status(plan) == "done"

    monkeypatch.setattr(tree, "mark", marking)
    resumed = _resume(engine, tree)
    summary = resumed.run()

    assert not summary.paused
    assert [m.outcome for m in tree.marks] == ["written", "noop"]
    assert load_state(engine.run_dir).tasks["1.1"].ticket_done_pending is False
    assert _plan_status(plan) == "done"


def test_failed_post_landing_mark_pauses_ticket_tree(project_v7_store_repo, monkeypatch):
    """A post-landing mark the chokepoint cannot make is a repair write, so it
    raises rather than degrades: the run pauses at `ticket-tree` with the unit
    committed, the latch kept on disk and the reason naming the owed mark; a
    resume once the tree is fixed marks and clears it. Ablation (performed):
    swallow the `TicketsError` in `_land_post_landing_writes` and the run
    finishes with the plan unmarked — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    tree.faults["mark"] = TicketsLockBusy("the lock on the plan is held elsewhere")
    engine, _ = _shared_engine(
        layout, tree, script=[_builds_shared(layout, tree, plan)], max_stories=1
    )

    summary = engine.run()

    state = load_state(engine.run_dir)
    task = state.tasks["1.1"]
    assert summary.paused and state.paused_stage == PAUSE_TICKET_TREE
    assert state.paused_story_key == "1.1"
    assert "marking 1.1 done failed after its code landed" in state.paused_reason
    assert task.phase == Phase.DONE and task.commit_sha and task.ticket_done_pending
    assert tree.marks == [] and _plan_status(plan) == "built"

    del tree.faults["mark"]
    _resume(engine, tree).run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "written")]
    assert task.ticket_done_pending is False and _plan_status(plan) == "done"


def test_post_landing_mark_moves_with_a_renamed_project(
    project_v7_store_repo, monkeypatch, tmp_path
):
    """A nested store (T3) moves with the project: a latched mark replayed after
    the project moved marks, and commits, the plan under the live project, not
    the binding's launch-time path. Ablation (performed): mark and commit
    `Path(ticket.plan)` and the mark names the old path — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1", status="built"),))
    live = Path(tree.finds["1.1"].plan)
    engine, _ = _shared_engine(layout, tree)
    task = _bound(engine, tree)
    launched = tmp_path / "launched-here"
    engine.state.project = str(launched)
    assert task.ticket is not None
    task.ticket = dataclasses.replace(
        task.ticket, plan=str(launched / live.relative_to(layout.project))
    )
    task.ticket_done_pending = True
    marked: list[Path] = []
    committed: list[list[Path]] = []
    monkeypatch.setattr(
        engine.ticket_tree, "mark", lambda *_a, expected_plan, **_k: marked.append(expected_plan)
    )
    monkeypatch.setattr(
        engine,
        "_store_commit",
        lambda _repo, _key, paths, *_a, **_k: committed.append(paths) or True,
    )

    engine._land_post_landing_writes(task)

    assert marked == [live]
    assert committed and committed[0][0] == live
    assert task.ticket_done_pending is False


def test_shared_store_record_moves_with_a_renamed_project(project_v7_store_repo, tmp_path):
    """A nested store (T3) recorded under the launch-time project is located on
    the live one: the engine's shared store and the store repository it commits
    in are the moved paths, not the recorded ones. Ablation (performed): read the
    raw `state.store_topology` in `TicketsEngine.__init__` / `store_topology` and
    both name the old path — this FAILS."""
    from bmad_loop import ticketstore

    layout = project_v7_store_repo
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    paths = load_paths(layout.project, want_tickets=True)
    live = ticketstore.assess_store(paths, "none")
    assert isinstance(live, ticketstore.StoreTopology) and live.store_repo is not None
    launched = tmp_path / "launched-here"
    moved = dataclasses.replace(
        live,
        store_root=launched / live.store_root.relative_to(layout.project),
        store_repo=launched / live.store_repo.relative_to(layout.project),
    )
    engine, _ = make_engine(layout, tree, policy=_policy(), store_topology=moved.to_dict())
    engine.state.project = str(launched)
    resumed = TicketsEngine(
        paths=paths,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=engine.state,
        ticket_tree_factory=lambda root, pin, folder=None: tree,
    )

    assert resumed.paths.shared_store == live.store_root
    assert resumed.store_topology == live


def test_crash_before_merge_back_replays_merge_then_marks(project_v7_store_repo, monkeypatch):
    """Under worktree isolation a host lost after the unit committed in its mount
    but before the merge-back leaves a DONE unit whose code is NOT on the target:
    no mark may run until it is. The resume replays the merge-back first (the
    latch is a write owed after landing, like a ledger carry), then marks.
    Ablation (performed): drop the latch from `_replay_unlatched_ledger_carries`'
    payload test and the unit is never merged or marked — this FAILS."""
    layout = project_v7_store_repo
    main = layout.project
    tree = _tree(layout, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)
    _marking_tree(tree, plan, monkeypatch)
    at_mark = _landed_marks(tree, main, monkeypatch)
    engine, _ = _shared_engine(
        layout,
        tree,
        isolation="worktree",
        script=[_builds_shared(layout, tree, plan)],
        max_stories=1,
    )
    flow = engine._worktree_flow

    def host_dies(*a, **kw):
        raise _HostDied

    monkeypatch.setattr(flow, "merge_local", host_dies)
    with pytest.raises(_HostDied):
        engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.ticket_done_pending is True
    assert not _has_unit_code(main) and tree.marks == []

    monkeypatch.undo()
    _marking_tree(tree, plan, monkeypatch)
    at_mark = _landed_marks(tree, main, monkeypatch)
    _resume(engine, tree).run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert [(m.target, m.outcome) for m in tree.marks] == [("done", "written")]
    assert at_mark == [(verify.rev_parse_head(main), True)]
    assert task.ticket_done_pending is False and _plan_status(plan) == "done"


def test_shared_store_adoption_latches_instead_of_marking(project_v7_store_repo, monkeypatch):
    """An adopted kept branch in a shared store is accepted after its merge-back,
    like any commit: `_apply_adoption` latches `ticket_done_pending` and writes
    nothing, and an adoption owing operator actions escalates unwritten. Ablation
    (performed): drop the shared-store arm of the tickets `_apply_adoption` and
    the base adoption marks `done` before the code lands — this FAILS."""
    layout = project_v7_store_repo
    tree = _tree(layout, in_progress=(ticket_row("1.1", status="built"),))
    engine, _ = _shared_engine(layout, tree)
    task = _bound(engine, tree)
    plan = Path(tree.finds["1.1"].plan)
    task.spec_file = str(plan)
    task.phase = Phase.COMMITTING
    _write_plan(plan, "built")
    _marking_tree(tree, plan, monkeypatch)

    engine._apply_adoption(task)

    assert tree.marks == [] and _plan_status(plan) == "built"
    assert load_state(engine.run_dir).tasks["1.1"].ticket_done_pending is True

    task.operator_actions = ["publish the DNS record"]
    with pytest.raises(RunPaused):
        engine._apply_adoption(task)
    assert tree.marks == [] and task.phase == Phase.ESCALATED


def test_tickets_harvest_accepts_built_and_done_plans(project_v7):
    """A build leaves its plan at ``built``; a follow-up pass may find it already
    ``done``. Both harvest, and ``source_spec:`` names the plan by its path in
    the initiative folder."""
    engine, _ = make_engine(project_v7, _tree(project_v7))
    plan = project_v7.initiative_folder / "epic-auth" / "story-login-plan.md"

    assert engine._harvest_success_statuses() == frozenset({"built", "done"})
    assert engine._harvest_source_name(plan) == "epic-auth/story-login-plan.md"


# ------------------------------------------- resume: a stale binding (DW-575)
# Real-spawn budget: the move test runs the vendored `tickets.py` under a real
# tree — 13 spawns (the first run's pick, the `move`, the gate's `find`, `status`
# and `find` on the paused resume, and the last resume's gate and commit-window
# mark); every other test here injects a FakeTicketTree.

_MOVE_TREE = (
    EpicSpec(1, "epic-cart", "Cart", (EntrySpec(1, "Cart model", plan_checkpoint=True),)),
    EpicSpec(2, "epic-extras", "Extras", (EntrySpec(1, "Extras note"),)),
)


def _real_legs(plan_of):
    """A build-auto over a real tree: the plan-halt leg writes the plan the
    session expects at `ready-for-dev`; any other leg builds it to `built`."""

    def leg(spec: SessionSpec) -> SessionResult:
        here = Path(spec.expected_spec or plan_of())
        cwd = Path(spec.cwd)
        baseline = verify.rev_parse_head(cwd)
        if spec.env.get("BMAD_LOOP_PLAN_HALT"):
            _write_plan(here, "ready-for-dev", baseline=baseline)
            result = {
                "workflow": "auto-dev",
                "spec_file": str(here),
                "status": "ready-for-dev",
                "plan_halt": True,
            }
            return SessionResult(status="completed", result_json=result)
        (cwd / "src.txt").write_bytes(b"cart model\n")
        _write_plan(here, "built", baseline=baseline)
        result = {"workflow": "auto-dev", "spec_file": str(here), "status": "built"}
        return SessionResult(status="completed", result_json=result)

    return leg


def test_resume_pauses_when_the_bound_ticket_was_moved(project_v7):
    """`tickets.py move` while a unit is paused at its plan checkpoint renumbers
    the entry (1.1 → 2.2) and moves its plan into the other epic. The resume
    pauses at `ticket-tree` (`binding-moved`) before it drives the unit, naming
    both refs and both plans, with no session, no rebind and no mark; once the
    move is undone the next resume drives leg 2 and commits. Ablation
    (performed): drop the `_gate_stale_binding` call in `_finish_inflight` and
    the resume re-dispatches `ticket 1.1 Halt after planning.` for a ref the tree
    no longer holds, writes a stray plan back at the old path and pauses at
    `plan-checkpoint` again — this FAILS."""
    shutil.rmtree(project_v7.initiative_folder / "tickets")
    write_ticket_tree(project_v7, _MOVE_TREE)
    git(project_v7.project, "add", "-A")
    git(project_v7.project, "commit", "-q", "-m", "two epics")
    tree = TicketTree(project_v7.project, pinned_initiative=project_v7.initiative)
    plan = Path(tree.find("1.1").plan)
    leg = _real_legs(lambda: plan)
    engine, _ = make_engine(project_v7, tree, script=[leg])  # type: ignore[arg-type]
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_PLAN_CHECKPOINT

    moved = run_tickets(project_v7, "move", "epic-cart", "1.1", "epic-extras")
    assert moved["ref"] == "2.2"
    new_plan = plan.parents[2] / "epic-extras" / "tickets" / plan.name
    resumed, adapter = _resumed(engine, tree, [leg])  # type: ignore[arg-type]

    assert resumed.run().paused

    state = load_state(engine.run_dir)
    task = state.tasks["1.1"]
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_TICKET_TREE, "1.1")
    [halt] = _kinds(resumed, "ticket-pick-halt")
    assert (halt["cause"], halt["stage"]) == (tickets_source.HALT_BINDING_MOVED, PAUSE_TICKET_TREE)
    assert "1.1 → 2.2" in state.paused_reason
    assert f"plan {plan} → {new_plan};" in state.paused_reason
    assert adapter.sessions == [] and not plan.exists()
    assert task.phase == Phase.DEV_VERIFY and task.ticket is not None
    assert (task.ticket.ref, task.ticket.plan) == ("1.1", str(plan))
    assert status_of(parse_frontmatter(new_plan.read_text(encoding="utf-8"))) == "ready-for-dev"

    git(project_v7.project, "checkout", "--", str(project_v7.initiative_folder))
    shutil.move(str(new_plan), str(plan))
    _write_plan(plan, "ready-for-dev")
    resumed, adapter = _resumed(engine, tree, [leg])  # type: ignore[arg-type]
    resumed.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    [leg_two] = adapter.sessions
    assert leg_two.prompt.startswith(f"/{engine._dev_skill()} ticket 1.1 — ")
    assert task.phase == Phase.DONE and task.commit_sha
    assert _committed_status(project_v7, task.commit_sha, plan) == "done"


def _paused_at_plan_checkpoint(layout: V7Layout):
    """A FakeTicketTree run paused at 1.1's plan checkpoint, its `ready-for-dev`
    plan on disk and the unit at DEV_VERIFY."""
    tree = _tree(layout, ready_to_start=(ticket_row("1.1", plan_checkpoint=True),))
    plan = Path(tree.finds["1.1"].plan)
    effect = _plans_then_builds(layout, tree, plan)
    engine, _ = make_engine(layout, tree, script=[effect])
    assert engine.run().paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_PLAN_CHECKPOINT
    return engine, tree, plan, effect


@pytest.mark.parametrize("gone", ["plan", "ticket"])
def test_resume_pauses_when_the_bound_plan_is_missing(project_v7, gone):
    """A bound unit whose verified plan is gone pauses the resume at
    `ticket-tree` (`binding-moved`) naming the recorded plan as missing: the
    plan file deleted under a ticket the tree still holds, or the ticket gone
    from the tree with no row of its title left to have moved to. No session
    runs and nothing is marked. Ablation (performed): drop the
    `_written_plan_fault` arm and the `plan` row re-dispatches leg 1 — this
    FAILS."""
    engine, tree, plan, effect = _paused_at_plan_checkpoint(project_v7)
    plan.unlink()
    if gone == "ticket":
        del tree.finds["1.1"]
    resumed, adapter = _resumed(engine, tree, [effect])

    assert resumed.run().paused

    state = load_state(engine.run_dir)
    assert (state.paused_stage, state.paused_story_key) == (PAUSE_TICKET_TREE, "1.1")
    [halt] = _kinds(resumed, "ticket-pick-halt")
    assert halt["cause"] == tickets_source.HALT_BINDING_MOVED
    assert f"the bound plan of ticket 1.1 (Ticket 1.1) is missing: {plan} (" in state.paused_reason
    why = "the file is gone" if gone == "plan" else "tickets.py find 1.1: no ticket matches"
    assert why in state.paused_reason
    assert adapter.sessions == [] and tree.marks == [] and not plan.exists()
    assert state.tasks["1.1"].phase == Phase.DEV_VERIFY


def test_resume_without_a_move_drives_the_bound_unit(project_v7):
    """An unchanged binding passes the gate: the resume asks `find` of the bound
    ref once and drives leg 2 as before. Ablation (performed): answer every
    resume with the missing-plan halt and this FAILS."""
    engine, tree, plan, effect = _paused_at_plan_checkpoint(project_v7)
    resumed, adapter = _resumed(engine, tree, [effect])

    resumed.run()

    state = load_state(engine.run_dir)
    assert state.paused_stage is None and state.tasks["1.1"].phase == Phase.DONE
    assert len(adapter.sessions) == 1 and _kinds(resumed, "ticket-pick-halt") == []
    assert ("find", "1.1") in tree.calls


def test_stale_binding_gate_follows_a_renamed_project(project_v7, tmp_path):
    """A renamed project is not a ticket move: the binding's plan, recorded under
    the launch-time project, is rebased onto the live one before it is compared
    with `find`, so the gate passes. Ablation (performed): compare the raw
    `ticket.plan` instead of `_recorded_plan` and this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    live = Path(tree.finds["1.1"].plan)
    _write_plan(live, "built")
    launched = tmp_path / "launched-here"
    engine.state.project = str(launched)
    task.ticket = dataclasses.replace(
        task.ticket, plan=str(launched / live.relative_to(project_v7.project))
    )
    task.phase, task.spec_file = Phase.REVIEW_VERIFY, task.ticket.plan

    engine._gate_stale_binding(task)  # must not raise
