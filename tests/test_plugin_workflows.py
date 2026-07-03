"""Phase 4: plugin-provided workflows + the worked-example plugin.

Three layers, mirroring the hook-bus test split:

  * **registry** — ``workflows_for`` / ``workflow_stages`` and the active-plugin
    gate (a workflow from an un-enabled ``[python]`` plugin must not fire any more
    than its module runs);
  * **engine integration** — a provided workflow injects an extra agent session at
    post_dev_phase through the generic ``_run_session`` path; the prompt
    substitutes; a *blocking* workflow whose session fails defers the unit; a
    non-blocking one is advisory; a workflow-free run is byte-identical;
  * **the example plugin** — ``examples/plugins/guardrails`` loads, enables, and
    exercises its setting, observe hook, veto gate, commit mutation, and provided
    workflow end-to-end.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import dev_effect, git, review_effect, write_sprint

from automator.adapters.base import SessionResult
from automator.adapters.mock import MockAdapter
from automator.engine import Engine
from automator.journal import Journal
from automator.model import RunState, TokenUsage
from automator.plugins import PluginRegistry
from automator.plugins.model import (
    LoadedPlugin,
    PluginManifest,
    PythonSpec,
    WorkflowSpec,
)
from automator.policy import GatesPolicy, NotifyPolicy, PluginsPolicy, Policy, ScmPolicy

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
    run_dir = project.project / ".automator" / "runs" / "wf-run"
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

    def effect(spec):  # noqa: ANN001
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
    """The completion contract + stall-nudge cap are workflow-session-only: the
    main dev/review sessions (their skills own their result conventions, and a
    slow background wait may legitimately need unbounded re-nudging) must stay
    byte-identical — no appended contract, no cap."""
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
        assert spec.stall_nudges_cap is None
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


def test_no_workflow_no_extra_session(project):
    # a plugin with hooks but no workflow injects nothing; the guard is O(1).
    reg = PluginRegistry([])
    engine, adapter = make_engine(project, one_story(project), reg)
    summary = engine.run()
    assert summary.done == 1
    # only the dev + review sessions ran (no injected third session)
    assert len(adapter.sessions) == 2
    assert not any(e["kind"].startswith("workflow") for e in engine.journal.entries())


# ========================================================= example plugin


def install_example(project) -> None:
    dest = project.project / ".automator" / "plugins" / "guardrails"
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
