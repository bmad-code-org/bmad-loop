"""`TicketsEngine`: the tickets-mode pick, its pick-time pauses, and the
dispatch, verify and review path of a picked ticket.

Every test builds the engine directly (`make_engine`) on the `project_v7`
sandbox with the mock adapter and an injected `FakeTicketTree`
(`compose_run`'s own wiring is pinned in tests/test_runsetup.py). Real-spawn
budget: none.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import FakeTicketTree, V7Layout, git, ticket_row

from bmad_loop import tickets_source, verify
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import load_paths
from bmad_loop.engine import Engine, RunPaused, SpecStatusWriteRefused
from bmad_loop.frontmatter import parse_frontmatter, status_of
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_STORY_CHECKPOINT,
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
from bmad_loop.tickets_engine import TicketsEngine
from bmad_loop.tickets_source import TicketRef, ticket_binding
from bmad_loop.tickettree import TicketsCrashed, TicketsLockBusy, TicketsPlanIOError
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

    def factory(root: Path, pin: str | None) -> FakeTicketTree:
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
    dry-run's line plus the tickets clauses (no backticks; no park clause, which
    tickets mode does not offer yet — and which stays last when it is on), and
    the repair leg keeps the feedback file as its last backticked token.
    Ablation (performed): fall back to the base `_dev_prompt` and the fresh
    prompt dispatches the bare key `1.1` with the sprint-board clause — this
    FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    skill = engine._dev_skill()

    fresh = engine._dev_prompt(task, None)

    assert engine._operator_park_instruction() == ""
    assert fresh == f"/{skill} ticket 1.1 — {engine._sprint_board_instruction()}"
    assert "`" not in fresh and "sprint-status" not in fresh
    assert "tickets.py mark" in fresh and "awaiting-operator" not in fresh
    engine._operator_park_enabled = lambda: True  # type: ignore[method-assign]
    park = engine._operator_park_instruction()
    assert park and "`" not in park
    assert engine._dev_prompt(task, None) == f"{fresh} {park}"
    del engine._operator_park_enabled

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
    backticks — and no park clause, because a tickets review demotion does not
    park yet, even under `on_review_demotion = "park"`."""
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
    assert prompt.endswith(engine._board_handback_redirect())
    assert "awaiting-operator" not in prompt


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
        ticket_tree_factory=lambda root, pin: tree,
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
    engine._ticket_tree_factory = lambda root, pin: (  # type: ignore[method-assign]
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
    path's story-file approval, then the pause cleared."""
    state = load_state(engine.run_dir)
    tickets_source.approve_paused_story_file(state)
    state.clear_pause()
    adapter = MockAdapter(list(script))
    resumed = TicketsEngine(
        paths=engine.paths,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=state,
        ticket_tree_factory=lambda root, pin: tree,
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
