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
from conftest import (
    committing_crash_state,
    dev_effect,
    needs_strict_codec,
    review_effect,
    write_sprint,
)

from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.escalation import critical_escalations
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, TokenUsage
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
        def on_pre_commit(self, c):
            pass

    bus = HookBus(registry_of(py_plugin(P)))
    assert bus.active("pre_commit") and not bus.active("pre_story")
    assert bus.active_plugins() == ["t"]


def test_observe_sees_readonly_context():
    seen = {}

    class P(Plugin):
        def on_pre_story(self, c):
            seen["story"] = c.story_key
            seen["stage"] = c.stage

    HookBus(registry_of(py_plugin(P))).emit("pre_story", ctx())
    assert seen == {"story": "1-1-a", "stage": "pre_story"}


def test_command_results_are_readonly_observation_data():
    from bmad_loop.verify import CommandResult

    result = CommandResult("pytest -q", 0, "tail", "out", "err")
    c = ctx("post_dev_verify", command_results=[result])

    assert c.command_results == (result,)
    with pytest.raises(AttributeError):
        c.command_results = ()


def test_a_plugin_cannot_erase_a_critical_escalation_through_result_json():
    """The observe-only claim has to hold at the depth escalations actually live.

    ``HookContext`` copies ``result_json`` so a plugin cannot rewrite the session
    result — but ``dict()`` is shallow, so the nested ``escalations`` LIST stayed
    the engine's own object. Both verify legs emit ``post_dev_verify`` before
    reading ``critical_escalations(result.result_json)``, so an in-process plugin
    that cleared that list erased the CRITICAL before the audit ran, and a
    verify-green repair proceeded where the run owed a pause.

    Asserted through ``critical_escalations`` on the ENGINE's dict rather than by
    comparing copies: that call is the read the fix exists to protect, and a test
    that only checked ``c.result_json is not original`` passed before the fix.

    ABLATION: restore ``dict(result_json)`` in ``HookContext.__init__`` and the
    audit comes back empty — the assert names the escalation that vanished.
    Verified."""

    class Eraser(Plugin):
        def on_post_dev_verify(self, c):
            c.result_json["escalations"].clear()
            c.result_json["escalations"].append({"severity": "INFO", "detail": "all fine"})

    original = {"escalations": [{"severity": "CRITICAL", "detail": "prod credential committed"}]}
    c = HookContext("post_dev_verify", result_json=original)
    HookBus(registry_of(py_plugin(Eraser))).emit("post_dev_verify", c)

    crits = critical_escalations(original)
    assert [e["detail"] for e in crits] == ["prod credential committed"]


def test_mutations_pipeline_last_writer_wins():
    # lower priority runs first; the later plugin sees the earlier edit and wins
    class First(Plugin):
        def on_pre_commit(self, c):
            c.proposed_commit_message = "first"

    class Second(Plugin):
        def on_pre_commit(self, c):
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
        def on_pre_story(self, c):
            c.veto("skip", "skip me")

    class Pause(Plugin):
        def on_pre_story(self, c):
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
        def on_pre_story(self, c):
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
        def on_pre_story(self, c):
            raise KeyboardInterrupt("sigint-like")

    with pytest.raises(KeyboardInterrupt):
        HookBus(registry_of(py_plugin(Sig))).emit("pre_story", ctx())


def test_fail_closed_python_vetoes_on_raise():
    class Strict(Plugin):
        fail_closed = True

        def on_pre_story(self, c):
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

    def runner(cmd, *, cwd, env, timeout):
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


@needs_strict_codec
def test_real_subprocess_runner_replaces_undecodable_output(tmp_path):
    """A hook child emitting a byte the locale codec cannot decode is decoded
    with replacement, not strictly: the strict decode raised UnicodeDecodeError —
    a ValueError, named by neither `except` arm — which escaped `_HookError`, the
    bus's designed transport-failure channel, and crashed the run instead of
    being classified.

    The exit code is asserted because it pins the sharpest consequence: the hook
    had already run to completion, so a *passing* hook took the run down and its
    verdict was lost with it. U+FFFD is asserted rather than merely "did not
    raise" — under `needs_strict_codec` the codec provably cannot decode the
    byte, so `errors="replace"` is the only thing that could have produced that
    character. Its *count* stays unasserted (that is what varies by codec), and
    the ASCII on both sides pins that only the offending byte was replaced.

    Driven through a real child on purpose: a monkeypatched `subprocess.run`
    never runs the stdlib decode, so such a test passes with the bug restored.

    Ablation: remove `errors="replace"` from `_run_subprocess` and this fails
    with UnicodeDecodeError."""
    # Interpreter is `sys.executable`, never a bare `python`: the tests run under
    # uv, where no `python` need be on PATH. The double quotes are honored by
    # both sh and cmd, so the one string works on either host shell.
    script = tmp_path / "emit383.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'before \\xff after\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )

    rc, out = _run_subprocess(
        f'"{sys.executable}" "{script}"', cwd=str(tmp_path), env={}, timeout=10
    )

    assert rc == 7  # the hook verdict a strict decode used to lose entirely
    assert "�" in out  # the replacement, not a survivor
    assert "before" in out and "after" in out


def test_shared_persists_across_stages():
    class P(Plugin):
        def on_pre_dev_phase(self, c):
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

    def append(self, kind, **fields):
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
        def on_pre_session(self, c):
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
        def on_pre_commit(self, c):
            c.proposed_commit_message = f"plugin-authored: {c.story_key}"

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "msgmut")))
    summary = engine.run()
    assert summary.done == 1
    assert git(project.project, "log", "-1", "--format=%s") == "plugin-authored: 1-1-a"


def test_post_dev_verify_reaches_a_real_plugin_through_the_bus(project, monkeypatch):
    """The verifier results and their discriminators survive the REAL dispatch.

    The engine-side tests for this surface swap `engine._bus` for a capture
    double: that proves what the engine BUILDS, but skips everything the bus does
    with it — stage activation, plugin routing, and the read-only view an actual
    `Plugin` subclass receives. This one goes through `HookBus.emit` into a
    registered plugin, so the plumbing itself is covered end to end.

    Ablation: drop `command_results`, `verification_stage` or
    `verification_sequence` from the engine's `post_dev_verify` emit and the
    plugin observes that field's default (`()` / `None`) instead.
    """
    from bmad_loop import verify

    seen = []

    class P(Plugin):
        def on_post_dev_verify(self, c):
            seen.append((c.verification_stage, c.verification_sequence, c.command_results))

    result = verify.CommandResult("pytest -q", 0, "tail", "out", "err")
    monkeypatch.setattr(verify, "run_verify_commands", lambda policy, cwd: [result])

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "verifyobs")))
    summary = engine.run()

    assert summary.done == 1
    assert seen == [("dev", 1, (result,))]
    # and the keys the plugin was handed are the ones its journal record carries,
    # which is the correlation the whole surface exists for. Scoped to the dev
    # stage: the review gate journals its own pass now, and that one deliberately
    # reaches no plugin — the single `seen` entry above is the other half of that.
    (entry,) = [
        e
        for e in engine.journal.entries()
        if e["kind"] == "verify-command-result" and e["verification_stage"] == "dev"
    ]
    assert entry["verification_sequence"] == 1
    assert entry["story_key"] == "1-1-a" and entry["command"] == "pytest -q"


def _resume_committing(project, engine, registry):
    """Resume a run whose task was persisted at COMMITTING (#115 crash state)."""
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        registry=registry,
    )
    return resumed, adapter


def test_pre_commit_hook_fires_on_commit_resume(project):
    """#115: the commit re-drive skips the pre_commit_gate workflows but must
    still emit the pre_commit hook — the message is regenerated on resume, so
    a plugin's rewrite has to reach the squashed commit."""

    class P(Plugin):
        def on_pre_commit(self, c):
            c.proposed_commit_message = f"plugin-authored: {c.story_key}"

    reg = registry_of(py_plugin(P, "msgmut"))
    engine, _ = make_engine(project, [], reg)
    committing_crash_state(project, engine)

    resumed, adapter = _resume_committing(project, engine, reg)
    summary = resumed.run()

    assert summary.done == 1
    assert adapter.sessions == []
    from conftest import git

    assert git(project.project, "log", "-1", "--format=%s") == "plugin-authored: 1-1-a"


def test_pre_commit_pause_veto_on_commit_resume_escalates(project):
    """A pause veto during the commit re-drive escalates (COMMITTING→ESCALATED
    is the legal move) with the attempt's commits left intact above baseline."""

    class P(Plugin):
        def on_pre_commit(self, c):
            c.veto("pause", "halt")

    reg = registry_of(py_plugin(P, "vpcommit"))
    engine, _ = make_engine(project, [], reg)
    baseline = committing_crash_state(project, engine)

    resumed, _ = _resume_committing(project, engine, reg)
    summary = resumed.run()

    assert summary.paused and summary.escalated == 1
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.ESCALATED
    from bmad_loop.verify import rev_parse_head

    assert rev_parse_head(project.project) != baseline  # attempt commits intact


def test_veto_defer_routes_to_defer(project):
    class P(Plugin):
        def on_pre_story(self, c):
            c.veto("defer", "not now")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "vd")))
    summary = engine.run()
    assert summary.done == 0 and summary.deferred == 1
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-deferred" in kinds


def test_veto_pause_routes_to_escalation(project):
    class P(Plugin):
        def on_pre_story(self, c):
            c.veto("pause", "halt the line")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "vp")))
    summary = engine.run()
    assert summary.paused and summary.escalated == 1


def test_session_veto_retries_then_defers(project):
    # a vetoed dev session synthesizes status="vetoed"; decide_dev retries within
    # budget, then defers — never silently proceeds.
    class P(Plugin):
        def on_pre_dev_session(self, c):
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
        def on_pre_story(self, c):
            raise RuntimeError("plugin bug")

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "buggy")))
    summary = engine.run()
    assert summary.done == 1  # the story still completed
    assert "plugin-error" in [e["kind"] for e in engine.journal.entries()]


def test_shared_state_persists_into_run_state(project):
    class P(Plugin):
        def on_pre_story(self, c):
            c.shared["seen_story"] = c.story_key

        def on_post_commit(self, c):
            c.shared["committed"] = True

    engine, _ = make_engine(project, one_story(project), registry_of(py_plugin(P, "sh")))
    engine.run()
    assert engine.state.plugin_shared == {"seen_story": "1-1-a", "committed": True}
