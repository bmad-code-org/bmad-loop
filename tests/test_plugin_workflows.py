"""Phase 4: plugin-provided workflows + the worked-example plugin.

Three layers, mirroring the hook-bus test split:

  * **registry** — ``workflows_for`` / ``workflow_stages`` and the active-plugin
    gate (a workflow from an un-enabled ``[python]`` plugin must not fire any more
    than its module runs);
  * **engine integration** — a provided workflow injects an extra agent session at
    post_dev_phase through the generic ``_run_session`` path; the prompt
    substitutes; a *blocking* workflow whose session fails defers the unit; a
    non-blocking one is advisory; a workflow-free run is byte-identical; the
    board-ownership prohibition rides every injected prompt post-gate (#437);
  * **the example plugin** — ``examples/plugins/guardrails`` loads, enables, and
    exercises its setting, observe hook, veto gate, commit mutation, and provided
    workflow end-to-end.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import committing_crash_state, dev_effect, git, review_effect, write_sprint

from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, TokenUsage
from bmad_loop.plugins import Plugin, PluginRegistry
from bmad_loop.plugins.model import (
    LoadedPlugin,
    PluginManifest,
    PythonSpec,
    WorkflowSpec,
)
from bmad_loop.policy import GatesPolicy, NotifyPolicy, PluginsPolicy, Policy, ScmPolicy

QUIET = NotifyPolicy(desktop=False, file=True)
EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "guardrails"


# --------------------------------------------------------------- harness


def wf_manifest(name: str = "wf", *, python: bool = False, **wf_kw) -> PluginManifest:
    spec = WorkflowSpec(
        name=wf_kw.pop("wf_name", "doc"),
        stage=wf_kw.pop("stage", "post_dev_phase"),
        role=wf_kw.pop("role", "review"),
        prompt=wf_kw.pop("prompt", "/doc {story_key}"),
        blocking=wf_kw.pop("blocking", False),
    )
    return PluginManifest(
        name=name,
        api_version=1,
        python=PythonSpec("x.py") if python else None,
        workflows=(spec,),
    )


def make_engine(project, script, registry=None, policy=None, **kw):
    run_dir = project.project / ".bmad-loop" / "runs" / "wf-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="wf-run", project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
        ),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        registry=registry,
        **kw,
    )
    return engine, adapter


def one_story(project, key="1-1-a"):
    write_sprint(project, {"epic-1": "backlog", key: "ready-for-dev"})
    return [dev_effect(project, key), review_effect(project, key, clean=True)]


def setup_story(project, key="1-1-a"):
    write_sprint(project, {"epic-1": "backlog", key: "ready-for-dev"})


def workflow_effect(captured: list, status: str = "completed"):
    """A scripted session standing in for an injected workflow session: record the
    spec (to assert prompt substitution + task_id) and return ``status``."""

    def effect(spec):
        captured.append(spec)
        return SessionResult(status=status, result_json={})

    return effect


# =============================================================== registry


def test_workflow_stages_and_lookup():
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest())])
    assert reg.workflow_stages() == frozenset({"post_dev_phase"})
    found = reg.workflows_for("post_dev_phase")
    assert [w.name for _, w in found] == ["doc"]
    assert reg.workflows_for("post_review_result") == []


def test_data_only_workflow_is_active():
    # no [python] -> declarative tier -> always active (like a declarative hook)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest())])
    assert reg.workflows_for("post_dev_phase")


def test_unenabled_python_workflow_is_inert():
    # a [python] plugin that wasn't enabled has instance=None: its module never
    # ran, so its workflow must not inject a session either.
    m = wf_manifest("gated", python=True)
    reg = PluginRegistry([LoadedPlugin(manifest=m, trusted=False)])
    assert reg.workflow_stages() == frozenset({"post_dev_phase"})  # declared...
    assert reg.workflows_for("post_dev_phase") == []  # ...but not active


def test_provided_workflows_names():
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("p"))])
    assert reg.provided_workflows() == {"p": ("doc",)}


# ----------------------------------------------------- settings overlay


def two_stage_manifest(name: str = "ts") -> PluginManifest:
    """One workflow on each injection stage, both advisory by default — for
    exercising the per-workflow settings overlay."""
    return PluginManifest(
        name=name,
        api_version=1,
        workflows=(
            WorkflowSpec(name="td", stage="post_dev_phase", role="dev", prompt="/td"),
            WorkflowSpec(name="nfr", stage="post_review_result", role="review", prompt="/nfr"),
        ),
    )


def test_absent_settings_preserve_manifest_values():
    # no settings declared -> byte-identical to the pre-overlay behaviour.
    m = wf_manifest("p", blocking=True)
    reg = PluginRegistry([LoadedPlugin(manifest=m)])  # settings defaults to {}
    found = reg.workflows_for("post_dev_phase")
    assert [(w.name, w.blocking) for _, w in found] == [("doc", True)]


def test_setting_disables_one_workflow():
    # td_enabled=False drops only that step; the other stage's workflow survives.
    reg = PluginRegistry(
        [LoadedPlugin(manifest=two_stage_manifest(), settings={"td_enabled": False})]
    )
    assert reg.workflows_for("post_dev_phase") == []
    assert [w.name for _, w in reg.workflows_for("post_review_result")] == ["nfr"]


def test_setting_flips_blocking_true_and_false():
    # _blocking overrides the manifest flag in both directions.
    on = PluginRegistry(
        [LoadedPlugin(manifest=wf_manifest("p", blocking=False), settings={"doc_blocking": True})]
    )
    assert on.workflows_for("post_dev_phase")[0][1].blocking is True

    off = PluginRegistry(
        [LoadedPlugin(manifest=wf_manifest("p", blocking=True), settings={"doc_blocking": False})]
    )
    assert off.workflows_for("post_dev_phase")[0][1].blocking is False


def test_workflow_stages_drops_fully_disabled_stage():
    # disabling the only step at a stage removes that stage from the O(1) guard;
    # the other stage stays.
    reg = PluginRegistry(
        [LoadedPlugin(manifest=two_stage_manifest(), settings={"td_enabled": False})]
    )
    assert reg.workflow_stages() == frozenset({"post_review_result"})

    # absent settings keep both stages declared.
    plain = PluginRegistry([LoadedPlugin(manifest=two_stage_manifest())])
    assert plain.workflow_stages() == frozenset({"post_dev_phase", "post_review_result"})


# ====================================================== engine integration


def test_workflow_injects_a_session_at_post_dev_phase(project):
    captured: list = []
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf"))])
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),
        review_effect(project, "1-1-a", clean=True),
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()
    assert summary.done == 1
    # exactly one workflow session ran, between dev and review
    assert len(captured) == 1
    spec = captured[0]
    assert spec.prompt.startswith("/doc 1-1-a")  # {story_key} substituted
    assert spec.task_id == "1-1-a-wf.doc-1"  # label = "<plugin>.<workflow>"
    # the framework appends the completion contract to every workflow prompt:
    # the absolute marker path plus the frontmatter shape to write
    assert "bmad-dev-auto-result-1-1-a-wf.doc-1.md" in spec.prompt
    assert "status: done" in spec.prompt
    # and bounds its stall nudges so a forgotten marker degrades instead of
    # livelocking until session timeout
    assert spec.stall_nudges_cap == engine.policy.limits.workflow_stall_nudges_cap
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "workflow-start" in kinds and "workflow-end" in kinds
    starts = [e for e in engine.journal.entries() if e["kind"] == "workflow-start"]
    assert starts[0]["plugin"] == "wf" and starts[0]["workflow"] == "doc"


def test_dev_and_review_sessions_carry_no_workflow_contract(project):
    """The completion contract is workflow-session-only: the main dev/review
    sessions (their skills own their result conventions) must stay
    byte-identical — no appended contract. Their stall nudges are bounded by
    the dev cap, not the workflow one: a session whose reply to the wake nudge
    is itself a result-less Stop would otherwise refill the budget until
    session timeout (#149)."""
    captured: list = []
    setup_story(project)

    def capture(effect):
        def inner(spec):
            captured.append(spec)
            return effect(spec)

        return inner

    script = [
        capture(dev_effect(project, "1-1-a")),
        capture(review_effect(project, "1-1-a", clean=True)),
    ]
    engine, _ = make_engine(project, script)
    summary = engine.run()
    assert summary.done == 1
    assert len(captured) == 2
    for spec in captured:
        assert spec.stall_nudges_cap == engine.policy.limits.dev_stall_nudges_cap
        assert "Completion signal" not in spec.prompt
        assert "bmad-dev-auto-result-" not in spec.prompt


def test_blocking_workflow_failure_defers_the_unit(project):
    captured: list = []
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", blocking=True))])
    # dev runs, the blocking workflow session errors -> unit deferred; review never runs
    script = [dev_effect(project, "1-1-a"), workflow_effect(captured, status="error")]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 0
    assert len(captured) == 1  # the workflow session ran; no review session followed
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" in kinds


def test_blocking_workflow_env_fault_escalates_instead_of_deferring(project):
    """A blocking workflow session classified an environment fault (#194) escalates
    the run (re-arm restores the budget) instead of deferring the story as if its
    code were broken; the workflow-end entry carries env_fault + evidence."""
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", blocking=True))])
    evidence = "API Error: Unable to connect (ECONNREFUSED)"
    script = [
        dev_effect(project, "1-1-a"),
        SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence),
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "environment fault: blocking workflow" in engine.state.paused_reason
    assert evidence in engine.state.paused_reason
    end = [e for e in engine.journal.entries() if e["kind"] == "workflow-end"][-1]
    assert end["env_fault"] is True
    assert end["env_fault_evidence"] == evidence
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" not in kinds  # escalated, not deferred


def test_blocking_workflow_lost_session_says_so_in_the_defer_reason(project):
    """#489 on the one path that DEFERS rather than retries: a blocking workflow
    whose mux session was destroyed must not be filed as "the workflow ran and
    failed". The defer reason an operator reads carries both which workflow died
    and that the session went missing under it."""
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", blocking=True))])
    script = [
        dev_effect(project, "1-1-a"),
        SessionResult(status="crashed", session_vanished=True),
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()

    assert summary.deferred == 1
    deferred = [e for e in engine.journal.entries() if e["kind"] == "story-deferred"][-1]
    assert "blocking workflow 'doc' (wf)" in deferred["reason"]
    assert "multiplexer no longer reports the session" in deferred["reason"]


def test_nonblocking_workflow_failure_is_advisory(project):
    captured: list = []
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", blocking=False))])
    # the workflow session errors but is non-blocking -> the story still completes
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured, status="error"),
        review_effect(project, "1-1-a", clean=True),
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()
    assert summary.done == 1
    ends = [e for e in engine.journal.entries() if e["kind"] == "workflow-end"]
    assert ends and ends[0]["status"] == "error"


def test_pre_commit_gate_workflow_injects_before_commit(project):
    """A workflow bound to pre_commit_gate runs just before the commit — on the
    review-skip path too (dev recommends no follow-up, so the review loop and
    its post_review_result stage never run)."""
    captured: list = []
    setup_story(project)
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", stage="pre_commit_gate"))])
    script = [
        dev_effect(project, "1-1-a", followup_review=False),
        workflow_effect(captured),  # no review session between dev and this
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()
    assert summary.done == 1
    # seq is task.review_cycle, still 0 on the skip path (no review ever ran)
    assert len(captured) == 1 and captured[0].task_id == "1-1-a-wf.doc-0"
    entries = engine.journal.entries()
    starts = [e for e in entries if e["kind"] == "workflow-start"]
    assert [e["stage"] for e in starts] == ["pre_commit_gate"]
    kinds = [e["kind"] for e in entries]
    # the gate session finished before the commit landed
    assert kinds.index("workflow-end") < kinds.index("story-done")


def test_blocking_pre_commit_gate_failure_defers_the_unit(project):
    """A blocking pre_commit_gate workflow whose session doesn't complete defers
    the unit cleanly: the stage fires before the task enters COMMITTING (which
    has no legal move to DEFERRED), so the defer is a legal transition."""
    setup_story(project)
    reg = PluginRegistry(
        [LoadedPlugin(manifest=wf_manifest("wf", stage="pre_commit_gate", blocking=True))]
    )
    script = [
        dev_effect(project, "1-1-a", followup_review=False),
        workflow_effect([], status="error"),
    ]
    engine, _ = make_engine(project, script, reg)
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 0
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" in kinds and "story-done" not in kinds


def test_pre_commit_gate_workflow_not_rerun_on_commit_resume(project):
    """#115: a task persisted at COMMITTING already ran its pre_commit_gate
    workflows — the phase save lands only after the gate loop returns clean.
    The resume re-drive must not re-charge them (and could not legally unwind
    a blocking failure anyway: COMMITTING has no move to DEFERRED).

    Ablation: restore `journal=engine.journal` in the inline resume constructor
    and the clean resume-row assertion fails on inherited log_task/log_pos;
    the persisted-history and active-log premises still pass.
    With the earlier metadata assertion bypassed, each actual resume-commit
    and story-done row also fails the emitted-row metadata assertion (checked
    independently for each kind).
    """
    reg = PluginRegistry(
        [LoadedPlugin(manifest=wf_manifest("wf", stage="pre_commit_gate", blocking=True))]
    )
    engine, _ = make_engine(project, [], reg)
    committing_crash_state(project, engine)
    log = engine.run_dir / "logs" / "1-1-a-dev-1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"pre-pause session output\n")
    engine.journal.set_active_log("1-1-a-dev-1")
    engine.journal.append("run-start", cycle=1)
    history = engine.journal.entries()
    assert history[-1]["log_task"] == "1-1-a-dev-1"
    assert history[-1]["log_pos"] == log.stat().st_size > 0

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=Journal(engine.run_dir),
        state=state,
        registry=reg,
    )
    reopened_history = resumed.journal.entries()
    assert reopened_history[: len(history)] == history
    resumed.journal.append("run-start", cycle=2)
    entries = resumed.journal.entries()
    assert entries[:-1] == reopened_history
    resume_row = entries[-1]
    assert resume_row["kind"] == "run-start" and resume_row["cycle"] == 2
    assert "log_task" not in resume_row and "log_pos" not in resume_row
    summary = resumed.run()

    assert summary.done == 1
    assert load_state(resumed.run_dir).tasks["1-1-a"].phase == Phase.DONE
    assert adapter.sessions == []  # the gate session was not re-charged
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "workflow-start" not in kinds
    assert "resume-commit" in kinds and "story-done" in kinds
    for kind in ("resume-commit", "story-done"):
        rows = [e for e in resumed.journal.entries() if e["kind"] == kind]
        assert rows
        for row in rows:
            assert "log_task" not in row and "log_pos" not in row


def test_no_workflow_no_extra_session(project):
    # a plugin with hooks but no workflow injects nothing; the guard is O(1).
    reg = PluginRegistry([])
    engine, adapter = make_engine(project, one_story(project), reg)
    summary = engine.run()
    assert summary.done == 1
    # only the dev + review sessions ran (no injected third session)
    assert len(adapter.sessions) == 2
    assert not any(e["kind"].startswith("workflow") for e in engine.journal.entries())


# ============================================= board ownership (#437, phase 3)
#
# A workflow session is dispatched in the window between the orchestrator's own
# sprint-status write (`_post_dev_state_sync`) and the story's single commit
# (`finalize_commit`), so it opens on an uncommitted, unattributed board change —
# the exact state a review session read as a defect in #437. `_run_session`
# appends the prohibition AFTER the session-gate hooks, so a plugin prompt rewrite
# cannot strip it.
#
# Every presence/ordering/junction constant below is a test-local LITERAL, never a
# call to the code under test: `"" in s` is True and `s.index("")` is 0, so an
# assertion built from `engine._sprint_board_instruction()` goes vacuous under
# exactly the ablation it exists to catch. The junction literals span the END of
# one section and the START of the next — the only shape that can detect a changed
# separator or a reordering.

BOARD_OWNED = (
    "sprint-status.yaml is owned by the orchestrator: never write it, and never "
    "revert a change to it. A row at done or awaiting-operator is the orchestrator's "
    "own bookkeeping — not a defect to fix, and not proof that the work is verified."
)
BOARD_HEADING = "## Sprint board (orchestrator-owned)"
# the review-only hand-back redirect, which a workflow prompt must NOT carry
BLOCKED_INVITE = "status: blocked and say why"
# junction: the plugin's own prompt -> the board section
PROMPT_BOARD_JOIN = "/doc 1-1-a\n\n## Sprint board (orchestrator-owned)\n\nsprint-status.yaml"
# junction: the board section -> the completion contract
BOARD_CONTRACT_JOIN = "not proof that the work is verified.\n\n## Completion signal (required)"
# the completion contract's last words — it stays the tail of every workflow prompt
CONTRACT_TAIL = "stalled and its work may be discarded."


def run_one_workflow(project, *, registry):
    """Drive one story with a post_dev_phase workflow and return the injected
    session's SessionSpec."""
    captured: list = []
    setup_story(project)
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),
        review_effect(project, "1-1-a", clean=True),
    ]
    engine, _ = make_engine(project, script, registry)
    summary = engine.run()
    assert summary.done == 1
    assert len(captured) == 1
    return captured[0]


def test_workflow_prompt_names_the_sprint_boards_owner(project):
    """The prohibition rides every injected workflow prompt as its own section,
    ahead of the completion contract — which stays the tail, because a session that
    ends its turn without writing the marker livelocks the orchestrator."""
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf"))])
    spec = run_one_workflow(project, registry=reg)

    assert BOARD_OWNED in spec.prompt
    assert BOARD_HEADING in spec.prompt
    # the plugin's own text still leads, and the two separators are exactly \n\n
    assert spec.prompt.startswith("/doc 1-1-a")
    assert PROMPT_BOARD_JOIN in spec.prompt
    assert BOARD_CONTRACT_JOIN in spec.prompt
    # board BEFORE contract, and the contract is the last thing said
    assert spec.prompt.index(BOARD_OWNED) < spec.prompt.index("## Completion signal")
    assert spec.prompt.endswith(CONTRACT_TAIL)


def test_workflow_board_clause_rides_every_injection_stage(project):
    """All three stages run inside the uncommitted-board window, so all three
    carry it — post_review_result and pre_commit_gate as much as post_dev_phase."""
    for stage in ("post_dev_phase", "post_review_result", "pre_commit_gate"):
        reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf", stage=stage))])
        captured: list = []
        setup_story(project)
        script = [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)]
        # post_dev_phase fires between dev and review; the other two after it
        script.insert(1 if stage == "post_dev_phase" else 2, workflow_effect(captured))
        engine, _ = make_engine(project, script, reg)
        assert engine.run().done == 1, stage
        assert len(captured) == 1, stage
        assert BOARD_OWNED in captured[0].prompt, stage


def test_workflow_board_clause_survives_a_plugin_prompt_rewrite(project):
    """The unstrippable property, and the reason the append sits after
    `_emit_session_gate`: a plugin that replaces `proposed_prompt` wholesale — the
    documented way to author a workflow's dispatch — still cannot drop the board
    prohibition or the completion contract."""

    class P(Plugin):
        def on_pre_workflow_session(self, c):
            c.proposed_prompt = "/rewritten-by-plugin"

    # the workflow's provider is declarative; the rewriting plugin has to be
    # in-process, since only an instance carries `on_*` hooks
    m = PluginManifest(name="rw", api_version=1)
    reg = PluginRegistry(
        [LoadedPlugin(manifest=wf_manifest("wf")), LoadedPlugin(manifest=m, instance=P(m, {}))]
    )
    spec = run_one_workflow(project, registry=reg)

    assert spec.prompt.startswith("/rewritten-by-plugin")  # the rewrite took effect
    assert "/doc 1-1-a" not in spec.prompt  # ...wholesale
    assert BOARD_OWNED in spec.prompt  # ...and could not strip this
    assert spec.prompt.endswith(CONTRACT_TAIL)


def test_workflow_prompt_withholds_the_blocked_handback(project):
    """The `blocked` hand-back redirect is review-only: it synthesizes a CRITICAL
    that halts the whole run, which is the wrong trade for a session that is not
    the sign-off authority. Asserted against the redirect's own wording, not the
    bare token — the completion contract legitimately carries `status: blocked` as
    its own non-completion signal, which the second assertion pins so the first
    cannot pass merely because the token vanished."""
    reg = PluginRegistry([LoadedPlugin(manifest=wf_manifest("wf"))])
    spec = run_one_workflow(project, registry=reg)

    assert BLOCKED_INVITE not in spec.prompt
    assert "`status: blocked`" in spec.prompt


def test_dev_and_review_prompts_carry_no_workflow_board_section(project):
    """The workflow section is workflow-session-only. The dev and review prompts
    already carry the same sentence from their own builders, so a leaked section
    would say it twice — and a `label`-blind append would put it on sessions whose
    prompt the orchestrator authors."""
    captured: list = []
    setup_story(project)

    def capture(effect):
        def inner(spec):
            captured.append(spec)
            return effect(spec)

        return inner

    script = [
        capture(dev_effect(project, "1-1-a")),
        capture(review_effect(project, "1-1-a", clean=True)),
    ]
    engine, _ = make_engine(project, script)
    assert engine.run().done == 1
    assert len(captured) == 2
    for spec in captured:
        assert BOARD_HEADING not in spec.prompt
        assert spec.prompt.count(BOARD_OWNED) == 1  # from its own builder, exactly once


# ========================================================= example plugin


def install_example(project) -> None:
    dest = project.project / ".bmad-loop" / "plugins" / "guardrails"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLE_DIR, dest)


def example_policy(**settings) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        plugins=PluginsPolicy(enabled=("guardrails",), settings={"guardrails": settings}),
    )


def test_example_plugin_full_cycle(project):
    install_example(project)
    setup_story(project)
    policy = example_policy(trailer="Automated-by: guardrails", forbid_epic=0)
    reg = PluginRegistry.build(project.project, policy)
    # the in-process module was trusted (enabled) and constructed
    assert reg.get("guardrails").instance is not None

    captured: list = []
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),  # the doc-sync workflow at post_dev_phase
        review_effect(project, "1-1-a", clean=True),
    ]
    engine, _ = make_engine(project, script, reg, policy)
    summary = engine.run()
    assert summary.done == 1

    # observe hook: the cross-stage shared dict persisted into RunState
    assert engine.state.plugin_shared.get("stories_seen") == 1
    # provided workflow injected its session
    assert len(captured) == 1 and captured[0].task_id == "1-1-a-guardrails.doc-sync-1"
    # commit-message mutation: the trailer was appended
    body = git(project.project, "log", "-1", "--format=%B")
    assert "Automated-by: guardrails" in body


def test_example_plugin_veto_gate_skips_parked_epic(project):
    install_example(project)
    setup_story(project)
    # park epic 1 -> the pre_dev_phase gate skips story 1-1-a before any session
    policy = example_policy(forbid_epic=1)
    reg = PluginRegistry.build(project.project, policy)
    engine, adapter = make_engine(project, [], reg, policy)  # empty script: no session launches
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 0
    assert not adapter.sessions  # vetoed before the dev session
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-skipped" in kinds


def test_example_plugin_inert_until_enabled(project):
    install_example(project)
    # discovered but NOT in [plugins] enabled -> [python] module never imported
    reg = PluginRegistry.build(
        project.project, Policy(gates=GatesPolicy(mode="none"), notify=QUIET)
    )
    gr = reg.get("guardrails")
    assert gr is not None and gr.instance is None and gr.trusted is False
    # its workflow is declared but inert (the plugin is not active)
    assert reg.workflows_for("post_dev_phase") == []
