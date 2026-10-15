"""`TicketsEngine`: the tickets-mode pick, its pick-time pauses, and the
dispatch, verify and review path of a picked ticket.

`compose_run` still refuses tickets runs, so every test builds the engine
directly (`make_engine`) on the `project_v7` sandbox with the mock adapter and an
injected `FakeTicketTree`. Real-spawn budget: none.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeTicketTree, V7Layout, git, ticket_row

from bmad_loop import tickets_source, verify
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import load_paths
from bmad_loop.engine import Engine, RunPaused
from bmad_loop.frontmatter import parse_frontmatter, status_of
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
from bmad_loop.policy import (
    GatesPolicy,
    NotifyPolicy,
    OperatorPolicy,
    Policy,
    ReviewPolicy,
    TicketsPolicy,
)
from bmad_loop.tickets_engine import TicketsEngine
from bmad_loop.tickets_source import TicketRef, ticket_binding
from bmad_loop.tickettree import TicketsCrashed

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
    dry-run's line plus the tickets clauses (park last, no backticks), and the
    repair leg keeps the feedback file as its last backticked token. Ablation
    (performed): fall back to the base `_dev_prompt` and the fresh prompt
    dispatches the bare key `1.1` with the sprint-board clause — this FAILS."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    engine, _ = make_engine(project_v7, tree)
    task = _bound(engine, tree)
    skill = engine._dev_skill()

    fresh = engine._dev_prompt(task, None)

    assert fresh == (
        f"/{skill} ticket 1.1 — {engine._sprint_board_instruction()} "
        f"{engine._operator_park_instruction()}"
    )
    assert "`" not in fresh and "sprint-status" not in fresh
    assert "tickets.py mark" in fresh and fresh.endswith(engine._operator_park_instruction())

    plan = Path(tree.finds["1.1"].plan)
    _write_plan(plan, "built")
    task.spec_file = str(plan)
    feedback = project_v7.project / "feedback.md"
    repair = engine._dev_prompt(task, feedback)

    assert repair.startswith(f"/{skill} ticket 1.1 — resume the autonomous dev session")
    assert repair.split("`")[-2] == str(feedback)


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
    assert engine._operator_park_instruction() not in prompt


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


def test_tickets_unit_reaches_commit_with_the_plan_at_built(project_v7):
    """A picked ticket runs end to end on the mock adapter: one dev session,
    pointed at the bound plan, builds it; verify accepts `built`; the unit commits
    with the plan at `built` in the commit — never `done`, which nothing marks
    yet — and the tree is never written."""
    tree = _tree(project_v7, ready_to_start=(ticket_row("1.1"),))
    plan = Path(tree.finds["1.1"].plan)

    def build(spec: SessionSpec) -> SessionResult:
        assert spec.expected_spec == str(plan)
        cwd = Path(spec.cwd)
        baseline = verify.rev_parse_head(cwd)
        (cwd / "src.txt").write_bytes(b"cart model\n")
        _write_plan(plan, "built", baseline=baseline)
        # the ticket now reads as finished in the tree, as `tickets.py` derives it
        tree.next_view = _tree(project_v7).next_view
        result = {"workflow": "auto-dev", "spec_file": str(plan), "status": "built"}
        return SessionResult(status="completed", result_json=result)

    engine, adapter = make_engine(project_v7, tree, script=[build], max_stories=1)

    engine.run()

    task = load_state(engine.run_dir).tasks["1.1"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert len(adapter.sessions) == 1
    assert adapter.sessions[0].prompt.startswith(f"/{engine._dev_skill()} ticket 1.1")
    rel = plan.relative_to(project_v7.project).as_posix()
    committed = git(project_v7.project, "show", f"{task.commit_sha}:{rel}")
    assert status_of(parse_frontmatter(committed)) == "built"
    assert tree.marks == []
