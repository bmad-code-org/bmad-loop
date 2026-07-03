"""The orchestration hook bus: observe / veto / mutate at lifecycle stages.

Two layers:

  * **bus unit tests** drive ``HookBus.emit`` directly over a hand-built
    registry — the no-op fast path, mutation pipelining, most-conservative veto
    resolution, failure isolation, and the declarative (subprocess) transport
    with an injected runner;
  * **engine integration tests** wire a plugin into a real ``Engine`` run and
    prove a prompt mutation reaches the session, a commit-message mutation
    reaches git, a veto routes onto the existing defer/pause control flow, a
    session veto retries-then-defers, a plugin exception is isolated, and a
    zero-plugin run stays byte-identical (no ``plugin*`` journal entries).
"""

from __future__ import annotations

import sys

import pytest
from conftest import dev_effect, review_effect, write_sprint

from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.journal import Journal
from bmad_loop.model import RunState, TokenUsage
from bmad_loop.plugins import (
    HookBus,
    HookContext,
    Plugin,
    PluginManifest,
    PluginRegistry,
)
from bmad_loop.plugins.bus import _HookError, _run_subprocess
from bmad_loop.plugins.model import HookSpec, LoadedPlugin
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy

QUIET = NotifyPolicy(desktop=False, file=True)


# --------------------------------------------------------------- harness


def manifest(name: str = "t", **kw) -> PluginManifest:
    return PluginManifest(name=name, api_version=1, **kw)


def registry_of(*loaded: LoadedPlugin) -> PluginRegistry:
    return PluginRegistry(list(loaded))


def py_plugin(cls, name: str = "t", *, priority: int = 0) -> LoadedPlugin:
    m = manifest(name, priority=priority)
    return LoadedPlugin(manifest=m, instance=cls(m, {}))


def ctx(stage: str = "pre_story", **kw) -> HookContext:
    return HookContext(stage, run_id="r", story_key=kw.pop("story_key", "1-1-a"), **kw)


# ============================================================ bus unit tests


def test_zero_plugin_fast_path():
    bus = HookBus(registry_of())
    assert not bus.any_active()
    assert not bus.active("pre_story")
    # emit on an inactive stage is a no-op that returns the context untouched
    c = ctx()
    assert bus.emit("pre_story", c) is c and not c.vetoed


def test_active_only_for_bound_stages():
    class P(Plugin):
        def on_pre_commit(self, c):  # noqa: ANN001
            pass

    bus = HookBus(registry_of(py_plugin(P)))
    assert bus.active("pre_commit") and not bus.active("pre_story")
    assert bus.active_plugins() == ["t"]


def test_observe_sees_readonly_context():
    seen = {}

    class P(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            seen["story"] = c.story_key
            seen["stage"] = c.stage

    HookBus(registry_of(py_plugin(P))).emit("pre_story", ctx())
    assert seen == {"story": "1-1-a", "stage": "pre_story"}


def test_mutations_pipeline_last_writer_wins():
    # lower priority runs first; the later plugin sees the earlier edit and wins
    class First(Plugin):
        def on_pre_commit(self, c):  # noqa: ANN001
            c.proposed_commit_message = "first"

    class Second(Plugin):
        def on_pre_commit(self, c):  # noqa: ANN001
            assert c.proposed_commit_message == "first"  # sees the earlier edit
            c.proposed_commit_message = "second"

    bus = HookBus(
        registry_of(py_plugin(First, "a", priority=0), py_plugin(Second, "b", priority=5))
    )
    c = HookContext("pre_commit", proposed_commit_message="orig")
    bus.emit("pre_commit", c)
    assert c.proposed_commit_message == "second"


def test_veto_resolves_most_conservative():
    class Skip(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            c.veto("skip", "skip me")

    class Pause(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            c.veto("pause", "stop everything")

    # registered skip-first; resolution must still pick pause (no short-circuit)
    bus = HookBus(registry_of(py_plugin(Skip, "a"), py_plugin(Pause, "b")))
    c = ctx()
    bus.emit("pre_story", c)
    resolved = c.resolved_veto()
    assert resolved.action == "pause" and resolved.plugin_id == "b"
    assert {v.action for v in c.vetoes} == {"skip", "pause"}


def test_python_exception_is_isolated_and_disables_instance():
    calls = {"n": 0}

    class Boom(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            calls["n"] += 1
            raise RuntimeError("kaboom")

    journal = _FakeJournal()
    bus = HookBus(registry_of(py_plugin(Boom)), journal)
    bus.emit("pre_story", ctx())  # caught, not raised
    bus.emit("pre_story", ctx())  # instance disabled -> not called again
    assert calls["n"] == 1
    assert "plugin-error" in journal.kinds()


def test_baseexception_propagates():
    class Sig(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            raise KeyboardInterrupt("sigint-like")

    with pytest.raises(KeyboardInterrupt):
        HookBus(registry_of(py_plugin(Sig))).emit("pre_story", ctx())


def test_fail_closed_python_vetoes_on_raise():
    class Strict(Plugin):
        fail_closed = True

        def on_pre_story(self, c):  # noqa: ANN001
            raise RuntimeError("nope")

    c = ctx()
    HookBus(registry_of(py_plugin(Strict))).emit("pre_story", c)
    assert c.resolved_veto().action == "defer"


# -------------------------------------------------- declarative (subprocess)


def declarative(stage: str, *, blocking=False, fail_closed=False, name="d") -> LoadedPlugin:
    m = manifest(
        name, hooks=(HookSpec(stage=stage, cmd="X", blocking=blocking, fail_closed=fail_closed),)
    )
    return LoadedPlugin(manifest=m)


def test_declarative_nonzero_exit_vetoes_blocking():
    runs = {}

    def runner(cmd, *, cwd, env, timeout):  # noqa: ANN001
        runs["env_stage"] = env["BMAD_LOOP_STAGE"]
        return 3, "build failed"

    c = ctx()
    HookBus(registry_of(declarative("pre_story", blocking=True)), runner=runner).emit(
        "pre_story", c
    )
    assert c.resolved_veto().action == "defer" and "exited 3" in c.resolved_veto().reason
    assert runs["env_stage"] == "pre_story"


def test_declarative_nonblocking_never_vetoes():
    c = ctx()
    HookBus(
        registry_of(declarative("pre_story", blocking=False)),
        runner=lambda *a, **k: (1, "advisory only"),
    ).emit("pre_story", c)
    assert not c.vetoed


def test_declarative_stdout_json_mutates_and_shares():
    payload = '{"shared": {"flag": 1}, "mutate": {"proposed_commit_message": "via-hook"}}'
    c = HookContext("pre_commit", shared={})
    HookBus(
        registry_of(declarative("pre_commit", blocking=False)),
        runner=lambda *a, **k: (0, "log line\n" + payload),
    ).emit("pre_commit", c)
    assert c.shared == {"flag": 1}
    assert c.proposed_commit_message == "via-hook"


def test_declarative_explicit_veto_overrides_exit_code():
    # a blocking hook that exits 0 but asks to pause via JSON still vetoes
    c = ctx()
    HookBus(
        registry_of(declarative("pre_story", blocking=True)),
        runner=lambda *a, **k: (0, '{"veto": {"action": "pause", "reason": "halt"}}'),
    ).emit("pre_story", c)
    assert c.resolved_veto().action == "pause"


def test_declarative_error_fail_open_vs_closed():
    def boom(*a, **k):
        raise _HookError("timed out after 1s")

    open_ctx = ctx()
    HookBus(
        registry_of(declarative("pre_story", blocking=True, fail_closed=False)), runner=boom
    ).emit("pre_story", open_ctx)
    assert not open_ctx.vetoed  # fail-open: the run survives a hook error

    closed_ctx = ctx()
    HookBus(
        registry_of(declarative("pre_story", blocking=True, fail_closed=True)), runner=boom
    ).emit("pre_story", closed_ctx)
    assert closed_ctx.resolved_veto().action == "defer"


def test_real_subprocess_runner_reports_exit_code(tmp_path):
    # _run_subprocess runs via the host shell (cmd on Windows, sh on POSIX); use a
    # command that prints "hi" and exits 7 on each.
    cmd = "echo hi& exit 7" if sys.platform == "win32" else "printf hi; exit 7"
    rc, out = _run_subprocess(cmd, cwd=str(tmp_path), env={}, timeout=10)
    assert rc == 7 and "hi" in out


def test_shared_persists_across_stages():
    class P(Plugin):
        def on_pre_dev_phase(self, c):  # noqa: ANN001
            c.shared["count"] = c.shared.get("count", 0) + 1

    bus = HookBus(registry_of(py_plugin(P)))
    shared: dict = {}
    for _ in range(3):
        bus.emit("pre_dev_phase", HookContext("pre_dev_phase", shared=shared))
    assert shared == {"count": 3}


# ====================================================== engine integration


class _FakeJournal:
    def __init__(self):
        self.entries: list[dict] = []

    def append(self, kind, **fields):  # noqa: ANN001
        self.entries.append({"kind": kind, **fields})

    def kinds(self):
        return [e["kind"] for e in self.entries]


def make_engine(project, script, registry=None, policy=None, **kw):
    run_dir = project.project / ".bmad-loop" / "runs" / "hb-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="hb-run", project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy or Policy(gates=GatesPolicy(mode="none"), notify=QUIET),
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


def test_zero_plugin_run_is_byte_identical(project):
    """No registry passed -> the real registry loads only the data-only `example`
    builtin -> no stage is active -> the journal carries zero plugin entries."""
    engine, _ = make_engine(project, one_story(project))
    summary = engine.run()
    assert summary.done == 1
    # no plugins-active / plugin-veto / plugin-error / plugin-hook entries
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert not any(k.startswith("plugin") for k in kinds)
    assert engine.state.plugin_shared == {}


def test_prompt_mutation_reaches_the_session(project):
    class P(Plugin):
        def on_pre_session(self, c):  # noqa: ANN001
            if c.role == "dev":
                c.proposed_prompt = "/custom-dev-prompt"

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "promptmut")))
    engine.run()
    starts = [
        e for e in engine.journal.entries() if e["kind"] == "session-start" and e["role"] == "dev"
    ]
    assert starts and all(e["prompt"] == "/custom-dev-prompt" for e in starts)


def test_commit_message_mutation_reaches_git(project):
    from conftest import git

    class P(Plugin):
        def on_pre_commit(self, c):  # noqa: ANN001
            c.proposed_commit_message = f"plugin-authored: {c.story_key}"

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "msgmut")))
    summary = engine.run()
    assert summary.done == 1
    assert git(project.project, "log", "-1", "--format=%s") == "plugin-authored: 1-1-a"


def test_veto_defer_routes_to_defer(project):
    class P(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            c.veto("defer", "not now")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "vd")))
    summary = engine.run()
    assert summary.done == 0 and summary.deferred == 1
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-deferred" in kinds


def test_veto_pause_routes_to_escalation(project):
    class P(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            c.veto("pause", "halt the line")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "vp")))
    summary = engine.run()
    assert summary.paused and summary.escalated == 1


def test_session_veto_retries_then_defers(project):
    # a vetoed dev session synthesizes status="vetoed"; decide_dev retries within
    # budget, then defers — never silently proceeds.
    class P(Plugin):
        def on_pre_dev_session(self, c):  # noqa: ANN001
            c.veto("defer", "dev not allowed")

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),  # exercise retry/defer continuation
    )
    # no adapter calls happen (every session is vetoed before launch)
    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "sv")), policy)
    summary = engine.run()
    assert summary.deferred == 1
    vetoes = [e for e in engine.journal.entries() if e["kind"] == "plugin-veto"]
    assert len(vetoes) == 2  # one per dev attempt within budget


def test_plugin_exception_does_not_crash_the_run(project):
    class P(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            raise RuntimeError("plugin bug")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "buggy")))
    summary = engine.run()
    assert summary.done == 1  # the story still completed
    assert "plugin-error" in [e["kind"] for e in engine.journal.entries()]


def test_shared_state_persists_into_run_state(project):
    class P(Plugin):
        def on_pre_story(self, c):  # noqa: ANN001
            c.shared["seen_story"] = c.story_key

        def on_post_commit(self, c):  # noqa: ANN001, E301
            c.shared["committed"] = True

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "sh")))
    engine.run()
    assert engine.state.plugin_shared == {"seen_story": "1-1-a", "committed": True}
