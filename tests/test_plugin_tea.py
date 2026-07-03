"""Phase 2: the bundled Test Architect Enterprise (TEA) plugin.

Four layers:

  * **manifest** — the shipped ``data/plugins/tea`` loads as a builtin, trust-gated
    ``[python]`` plugin with the six TEA workflows and the enable/blocking settings;
  * **readiness gate** — ``TeaPlugin.validate`` fails fast when TEA is not installed
    (``require_tea`` on), passes when it is, and is skipped when ``require_tea`` is off;
  * **injection (runs)** — enabling ``tea`` injects three ``post_dev_phase`` + three
    ``post_review_result`` sessions, in order, for a normal story run;
  * **injection (sweeps)** — a ``SweepEngine`` bundle drives the same six workflows
    through the inherited pipeline, with no sweep-specific code;
  * **enable setting** — ``automate_enabled = false`` suppresses just that workflow.

The injected sessions are scripted with the same ``workflow_effect`` stand-in used
by ``test_plugin_workflows`` (capture the spec, return a status) so we assert the
prompt label / task_id and the ``workflow-start``/``-end`` journal trail without a
real TEA install or CLI.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest
from conftest import (
    bundle_dev_effect,
    bundle_review_effect,
    dev_effect,
    review_effect,
    write_ledger,
    write_sprint,
)

from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.journal import Journal
from bmad_loop.model import RunState, TokenUsage
from bmad_loop.plugins import PluginError, PluginRegistry, get_plugin, load_plugins
from bmad_loop.plugins.model import PluginManifest
from bmad_loop.policy import GatesPolicy, NotifyPolicy, PluginsPolicy, Policy
from bmad_loop.sweep import DecisionPrompter, SweepEngine

QUIET = NotifyPolicy(desktop=False, file=True)

# the canonical TEA workflow firing order: three at post_dev_phase (role=dev),
# then three at post_review_result (role=review).
DEV_STEPS = ("td", "atdd", "automate")
REVIEW_STEPS = ("trace", "nfr", "review")


# ----------------------------------------------------------------- harness


def install_tea(project) -> None:
    """Stand in for a real ``npx bmad-method install`` (Test Architect): write the
    one file the readiness gate probes for."""
    cfg = project.project / "_bmad" / "tea"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        "test_artifacts: '{project-root}/_bmad-output/test-artifacts'\n", encoding="utf-8"
    )


def tea_policy(**settings) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        plugins=PluginsPolicy(enabled=("tea",), settings={"tea": settings}),
    )


def _tea_instance(**settings):
    """A ``TeaPlugin`` built straight from the bundled module, for unit-testing
    ``validate`` without standing up an engine."""
    path = os.path.join(get_plugin("tea").scripts_dir, "tea_plugin.py")
    spec = importlib.util.spec_from_file_location("tea_plugin_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    full = {"require_tea": True}
    full.update(settings)
    return mod.TeaPlugin(PluginManifest(name="tea", api_version=1), full)


def write_gate_artifact(project, gate: str, verdict: str) -> None:
    """Stand in for a real TEA gate run: write the gate's decision artifact under
    the configured ``test_artifacts`` dir, in the shape that gate's workflow emits.
    ``verdict`` is the canonical PASS/CONCERNS/FAIL/WAIVED outcome."""
    art = project.project / "_bmad-output" / "test-artifacts"
    art.mkdir(parents=True, exist_ok=True)
    if gate == "trace":
        (art / "gate-decision.json").write_text(
            json.dumps({"schema_version": "0.1.0", "gate_status": verdict}), encoding="utf-8"
        )
    elif gate == "nfr":
        (art / "nfr-assessment.md").write_text(
            f"# NFR Evidence Audit - Feature\n\n**Overall Status:** {verdict} ✅\n",
            encoding="utf-8",
        )
    elif gate == "review":
        recommendation = {
            "FAIL": "Block",
            "CONCERNS": "Request Changes",
            "PASS": "Approve with Comments",
            "WAIVED": "Approve",
        }[verdict]
        (art / "test-review.md").write_text(
            f"# Test Quality Review\n\n**Recommendation**: {recommendation}\n", encoding="utf-8"
        )
    else:  # pragma: no cover - guard against a typo in a test
        raise ValueError(f"unknown gate {gate!r}")


def pre_commit_ctx(project):
    """A minimal pre_commit HookContext pointing at the project tree (isolation =
    none: worktree == repo root), for unit-testing ``on_pre_commit`` directly."""
    from bmad_loop.plugins.context import HookContext

    return HookContext(
        "pre_commit",
        story_key="1-1-a",
        worktree=str(project.project),
        repo_root=str(project.project),
        shared={},
    )


def workflow_effect(captured: list, status: str = "completed"):
    """Scripted stand-in for an injected TEA session: record the spec, return a
    status (mirrors test_plugin_workflows)."""
    from bmad_loop.adapters.base import SessionResult

    def effect(spec):  # noqa: ANN001
        captured.append(spec)
        return SessionResult(status=status, result_json={})

    return effect


def make_engine(project, script, registry, policy):
    run_dir = project.project / ".bmad-loop" / "runs" / "tea-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="tea-run", project=str(project.project), started_at="now")
    return Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        registry=registry,
    )


def make_sweep(project, script, registry, policy):
    run_dir = project.project / ".bmad-loop" / "runs" / "tea-sweep"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="tea-sweep", project=str(project.project), started_at="now")
    prompter = DecisionPrompter(input_fn=lambda _: "", print_fn=lambda _line: None)
    return SweepEngine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        prompter=prompter,
        registry=registry,
    )


def workflow_trail(engine) -> tuple[list, list]:
    """(start, end) workflow names from the journal, in order."""
    entries = engine.journal.entries()
    starts = [(e["plugin"], e["workflow"]) for e in entries if e["kind"] == "workflow-start"]
    ends = [e["workflow"] for e in entries if e["kind"] == "workflow-end"]
    return starts, ends


# =============================================================== manifest


def test_builtin_tea_plugin_loads():
    plugins = load_plugins()
    assert "tea" in plugins
    tea = plugins["tea"]
    assert tea.python is not None and tea.python.cls == "TeaPlugin"
    # scripts_dir points at the bundled plugin dir (for {scripts} + module load)
    assert tea.scripts_dir.replace("\\", "/").endswith("data/plugins/tea")
    # TEA runtime + compiled skills are gitignored; seed them into worktrees.
    assert "_bmad/**" in tea.seed_globs


def test_builtin_tea_plugin_workflows_shape():
    tea = get_plugin("tea")
    by_name = {w.name: w for w in tea.workflows}
    assert set(by_name) == set(DEV_STEPS) | set(REVIEW_STEPS)
    for name in DEV_STEPS:
        assert by_name[name].stage == "post_dev_phase" and by_name[name].role == "dev"
    for name in REVIEW_STEPS:
        # gates bind to pre_commit_gate (fires on EVERY path into a commit),
        # not post_review_result (fires only when the review loop runs)
        assert by_name[name].stage == "pre_commit_gate" and by_name[name].role == "review"
    # ship advisory: nothing blocking in the manifest (operators flip *_blocking).
    assert not any(w.blocking for w in tea.workflows)
    # prompts name the explicit TEA workflow/skill so they degrade across CLIs.
    assert "bmad-testarch-automate" in by_name["automate"].prompt


def test_builtin_tea_plugin_settings_schema():
    tea = get_plugin("tea")
    by_key = {s.key: s for s in tea.settings}
    assert set(by_key) == {
        "require_tea",
        "td_enabled",
        "atdd_enabled",
        "automate_enabled",
        "trace_enabled",
        "nfr_enabled",
        "review_enabled",
        "trace_blocking",
        "nfr_blocking",
        "review_blocking",
    }
    assert by_key["require_tea"].type == "bool" and by_key["require_tea"].default is True
    # gate steps ship advisory (blocking default false); operators opt in.
    assert by_key["nfr_blocking"].default is False


def test_tea_is_trust_gated():
    """The [python] readiness gate is never built unless tea is in [plugins] enabled
    (and building the registry does not run validate, so no TEA install is needed)."""
    off = PluginRegistry.build(policy=Policy())
    assert off.get("tea").instance is None  # untrusted: not enabled

    on = PluginRegistry.build(policy=tea_policy())
    assert type(on.get("tea").instance).__name__ == "TeaPlugin"
    # not active -> its workflows stay inert when tea is not enabled
    assert off.workflows_for("post_dev_phase") == []


# ============================================================ readiness gate


def test_readiness_raises_when_tea_absent(project, monkeypatch):
    # no _bmad/tea/config.yaml in the project + require_tea on -> fail fast.
    monkeypatch.chdir(project.project)
    with pytest.raises(PluginError, match="not .*installed"):
        _tea_instance(require_tea=True).validate(Policy())


def test_readiness_passes_when_tea_present(project, monkeypatch):
    install_tea(project)
    monkeypatch.chdir(project.project)
    _tea_instance(require_tea=True).validate(Policy())  # no raise


def test_readiness_skipped_when_require_tea_false(project, monkeypatch):
    # TEA absent, but the gate is disabled -> validate is a no-op.
    monkeypatch.chdir(project.project)
    _tea_instance(require_tea=False).validate(Policy())  # no raise


def test_engine_construction_fails_fast_without_tea(project, monkeypatch):
    """The engine runs registry.validate() at startup; an enabled tea plugin with
    no TEA install fails construction before any story runs."""
    monkeypatch.chdir(project.project)  # no TEA installed
    policy = tea_policy()
    reg = PluginRegistry.build(project.project, policy)
    with pytest.raises(PluginError, match="not .*installed"):
        make_engine(project, [], reg, policy)


# ========================================================== injection (runs)


def test_runs_inject_six_tea_workflows_in_order(project, monkeypatch):
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = tea_policy()
    reg = PluginRegistry.build(project.project, policy)

    captured: list = []
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd
        workflow_effect(captured),  # automate
        review_effect(project, "1-1-a", clean=True),
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.done == 1
    # six injected sessions: dev steps before review steps, task_id = story.<plugin>.<wf>-seq
    assert [s.task_id for s in captured] == [
        "1-1-a-tea.td-1",
        "1-1-a-tea.atdd-1",
        "1-1-a-tea.automate-1",
        "1-1-a-tea.trace-1",
        "1-1-a-tea.nfr-1",
        "1-1-a-tea.review-1",
    ]
    starts, ends = workflow_trail(engine)
    assert starts == [("tea", n) for n in DEV_STEPS + REVIEW_STEPS]
    assert ends == list(DEV_STEPS + REVIEW_STEPS)


def test_gates_run_even_when_review_loop_is_skipped(project, monkeypatch):
    """The hey-dad regression: with review.trigger="recommended" (the default)
    and a dev session that recommends no follow-up, the orchestrator review loop
    never runs, so post_review_result never fires. The gates bind to
    pre_commit_gate, which fires on the skip path too — the three gate sessions
    must still run, at that stage, before the story commits."""
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = tea_policy()
    reg = PluginRegistry.build(project.project, policy)

    captured: list = []
    script = [
        dev_effect(project, "1-1-a", followup_review=False),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd
        workflow_effect(captured),  # automate
        # NO review session: the review loop is skipped entirely
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.done == 1
    # gate seq is task.review_cycle — still 0 here, since no review ever ran
    assert [s.task_id for s in captured] == [
        "1-1-a-tea.td-1",
        "1-1-a-tea.atdd-1",
        "1-1-a-tea.automate-1",
        "1-1-a-tea.trace-0",
        "1-1-a-tea.nfr-0",
        "1-1-a-tea.review-0",
    ]
    entries = engine.journal.entries()
    kinds = [e["kind"] for e in entries]
    assert "review-skipped" in kinds  # the review loop really was skipped
    gate_starts = [
        e for e in entries if e["kind"] == "workflow-start" and e["workflow"] in REVIEW_STEPS
    ]
    assert [e["workflow"] for e in gate_starts] == list(REVIEW_STEPS)
    assert all(e["stage"] == "pre_commit_gate" for e in gate_starts)
    # every gate finished before the commit landed
    assert max(i for i, k in enumerate(kinds) if k == "workflow-end") < kinds.index("story-done")


def test_blocking_gate_fail_escalates_even_when_review_skipped(project, monkeypatch):
    """End-to-end enforcement on the skip path (the half of the hey-dad failure
    that made *_blocking inert): a FAIL nfr artifact with nfr_blocking=true must
    escalate at commit — the story must NOT land — even though the review loop
    never ran."""
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = tea_policy(nfr_blocking=True)
    reg = PluginRegistry.build(project.project, policy)

    write_gate_artifact(project, "nfr", "FAIL")
    script = [
        dev_effect(project, "1-1-a", followup_review=False),
        workflow_effect([]),  # td
        workflow_effect([]),  # atdd
        workflow_effect([]),  # automate
        workflow_effect([]),  # trace
        workflow_effect([]),  # nfr — its FAIL verdict artifact is on disk
        workflow_effect([]),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.escalated == 1
    assert summary.done == 0
    assert summary.paused  # pre_commit pause-veto escalates + pauses the run
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-escalated" in kinds and "story-done" not in kinds


def test_automate_enabled_false_suppresses_only_that_step(project, monkeypatch):
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = tea_policy(automate_enabled=False)
    reg = PluginRegistry.build(project.project, policy)

    captured: list = []
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd  (no automate)
        review_effect(project, "1-1-a", clean=True),
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.done == 1
    starts, _ = workflow_trail(engine)
    fired = [w for _, w in starts]
    assert fired == ["td", "atdd", "trace", "nfr", "review"]
    assert "automate" not in fired


# ========================================================= injection (sweeps)


def test_sweep_bundle_drives_the_same_tea_workflows(project, monkeypatch):
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_ledger(project, {"DW-1": "open"})
    policy = tea_policy()
    reg = PluginRegistry.build(project.project, policy)

    from conftest import triage_effect

    plan = {
        "workflow": "deferred-sweep-triage",
        "open_ids": ["DW-1"],
        "already_resolved": [],
        "bundles": [{"name": "thing", "dw_ids": ["DW-1"], "intent": "fix it"}],
        "blocked": [],
        "skip": [],
        "decisions": [],
        "escalations": [],
    }
    captured: list = []
    script = [
        triage_effect(plan),
        bundle_dev_effect(project, "thing", ["DW-1"]),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd
        workflow_effect(captured),  # automate
        bundle_review_effect(project, "thing"),
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_sweep(project, script, reg, policy)
    summary = engine.run()

    assert not summary.paused
    # the bundle (story_key dw-thing) drove the same six TEA workflows in order
    assert [s.task_id for s in captured] == [
        "dw-thing-tea.td-1",
        "dw-thing-tea.atdd-1",
        "dw-thing-tea.automate-1",
        "dw-thing-tea.trace-1",
        "dw-thing-tea.nfr-1",
        "dw-thing-tea.review-1",
    ]
    starts, _ = workflow_trail(engine)
    assert starts == [("tea", n) for n in DEV_STEPS + REVIEW_STEPS]
    wf_starts = [e for e in engine.journal.entries() if e["kind"] == "workflow-start"]
    assert all(e["story_key"] == "dw-thing" for e in wf_starts)


# ===================================================== gate enforcement (unit)
# on_pre_commit parsing + veto matrix, driven directly against a HookContext so
# the artifact shapes + fail-open paths are pinned without standing up an engine.


@pytest.mark.parametrize(
    "verdict,blocks",
    [("FAIL", True), ("CONCERNS", True), ("PASS", False), ("WAIVED", False)],
)
def test_blocking_trace_gate_verdict_matrix(project, verdict, blocks):
    """A blocking trace gate escalates (pause) only on FAIL/CONCERNS; PASS and an
    explicit WAIVER land cleanly."""
    write_gate_artifact(project, "trace", verdict)
    ctx = pre_commit_ctx(project)
    _tea_instance(trace_blocking=True).on_pre_commit(ctx)
    if blocks:
        veto = ctx.resolved_veto()
        assert veto is not None and veto.action == "pause"
        assert verdict in veto.reason
    else:
        assert not ctx.vetoed
    # the parsed verdict is recorded as a breadcrumb either way.
    assert ctx.shared["tea_gates"]["trace"] == verdict


def test_blocking_nfr_markdown_gate_escalates(project):
    """The NFR gate has no JSON; a FAIL parsed from its markdown still escalates."""
    write_gate_artifact(project, "nfr", "FAIL")
    ctx = pre_commit_ctx(project)
    _tea_instance(nfr_blocking=True).on_pre_commit(ctx)
    veto = ctx.resolved_veto()
    assert veto is not None and veto.action == "pause" and "nfr=FAIL" in veto.reason


def test_blocking_review_recommendation_block_escalates(project):
    """The test-review gate uses Approve/Block vocabulary; Block maps to FAIL."""
    write_gate_artifact(project, "review", "FAIL")  # writes "Recommendation: Block"
    ctx = pre_commit_ctx(project)
    _tea_instance(review_blocking=True).on_pre_commit(ctx)
    veto = ctx.resolved_veto()
    assert veto is not None and veto.action == "pause" and "review=FAIL" in veto.reason


def test_advisory_gate_never_blocks(project):
    """Default (no *_blocking) is advisory: a FAIL artifact is ignored at commit."""
    write_gate_artifact(project, "trace", "FAIL")
    ctx = pre_commit_ctx(project)
    _tea_instance().on_pre_commit(ctx)  # trace_blocking defaults false
    assert not ctx.vetoed
    # advisory means the artifact is not even parsed -> no breadcrumb recorded.
    assert "tea_gates" not in ctx.shared


def test_missing_artifact_is_fail_open(project):
    """A blocking gate with no artifact on disk never blocks the commit."""
    ctx = pre_commit_ctx(project)
    _tea_instance(trace_blocking=True).on_pre_commit(ctx)
    assert not ctx.vetoed


def test_garbled_artifact_is_fail_open(project):
    """An unparseable gate artifact never wrongly stops a commit."""
    art = project.project / "_bmad-output" / "test-artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "gate-decision.json").write_text("{ this is not json", encoding="utf-8")
    ctx = pre_commit_ctx(project)
    _tea_instance(trace_blocking=True).on_pre_commit(ctx)
    assert not ctx.vetoed


def test_not_evaluated_verdict_is_fail_open(project):
    """A NOT_EVALUATED gate (collection not gate-eligible) is advisory, not blocking."""
    art = project.project / "_bmad-output" / "test-artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "gate-decision.json").write_text(
        json.dumps({"gate_status": "NOT_EVALUATED"}), encoding="utf-8"
    )
    ctx = pre_commit_ctx(project)
    _tea_instance(trace_blocking=True).on_pre_commit(ctx)
    assert not ctx.vetoed


def test_generation_steps_are_never_gate_enforced(project):
    """Generation steps (td/atdd/automate) expose no *_blocking flag; even if an
    operator sets one and a failing artifact exists, no gate is enforced."""
    write_gate_artifact(project, "trace", "FAIL")
    ctx = pre_commit_ctx(project)
    _tea_instance(td_blocking=True, atdd_blocking=True, automate_blocking=True).on_pre_commit(ctx)
    assert not ctx.vetoed


# ================================================== gate enforcement (engine)


def test_blocking_gate_escalates_at_commit(project, monkeypatch):
    """End-to-end: flipping trace_blocking=true against a FAIL gate escalates the
    story (pause) at commit instead of committing it."""
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_gate_artifact(project, "trace", "FAIL")
    policy = tea_policy(trace_blocking=True)
    reg = PluginRegistry.build(project.project, policy)

    captured: list = []
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd
        workflow_effect(captured),  # automate
        review_effect(project, "1-1-a", clean=True),
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert summary.done == 0


def test_advisory_default_commits_despite_failing_gate(project, monkeypatch):
    """End-to-end: with gates left advisory (default), a FAIL artifact does not
    stop the commit — the story lands."""
    install_tea(project)
    monkeypatch.chdir(project.project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_gate_artifact(project, "trace", "FAIL")
    policy = tea_policy()  # all gates advisory
    reg = PluginRegistry.build(project.project, policy)

    captured: list = []
    script = [
        dev_effect(project, "1-1-a"),
        workflow_effect(captured),  # td
        workflow_effect(captured),  # atdd
        workflow_effect(captured),  # automate
        review_effect(project, "1-1-a", clean=True),
        workflow_effect(captured),  # trace
        workflow_effect(captured),  # nfr
        workflow_effect(captured),  # review
    ]
    engine = make_engine(project, script, reg, policy)
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
