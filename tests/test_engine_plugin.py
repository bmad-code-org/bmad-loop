"""The Unity game-engine plugin: it is now a first-class bmad-loop plugin (the
proof the framework carries an engine layer), not a bespoke ``engines/`` subsystem.

Covered here:
  * the bundled unity ``plugin.toml`` manifest (shape, settings, scripts, seeds);
  * the ``UnityPlugin`` in-process hooks — readiness gate / per_worktree setup /
    teardown — veto on failure and inject the ``BMAD_LOOP_ENGINE_*`` env contract;
  * ``UnityPlugin.validate`` enforcing the editor_mode↔scm.isolation coupling;
  * the engine's MCP agent-routing helpers that feed ``ctx.agents``;
  * the unchanged unity helper-script unit tests (``unity_ready`` / ``unity_setup``
    / ``unity_teardown``), now loaded from ``get_plugin("unity").scripts_dir``.

The engine-integration gate flow (a plugin gating pre_worktree_setup/pre_ready_gate/
pre_worktree_teardown and routing a veto to defer) is exercised end-to-end in
test_engine_worktree.py with a generic declarative plugin.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

import pytest
from conftest import write_script_launcher

from bmad_loop.engine import Engine, _setup_mcp_agent_id
from bmad_loop.plugins import HookContext, PluginError, get_plugin, load_plugins
from bmad_loop.plugins.model import PluginManifest
from bmad_loop.policy import NotifyPolicy, PluginsPolicy, Policy, ScmPolicy

QUIET = NotifyPolicy(desktop=False, file=True)


# ------------------------------------------------------------- manifest / loader


def test_builtin_unity_plugin_loads():
    plugins = load_plugins()
    assert "unity" in plugins
    unity = plugins["unity"]
    assert unity.python is not None and unity.python.cls == "UnityPlugin"
    # MCP-generated skills are gitignored; seed them into per_worktree checkouts.
    assert ".claude/skills/*" in unity.seed_globs
    # scripts_dir points at the bundled plugin dir (for {scripts} substitution)
    assert unity.scripts_dir.replace("\\", "/").endswith("data/plugins/unity")


def test_builtin_unity_plugin_ships_its_scripts():
    scripts = os.listdir(get_plugin("unity").scripts_dir)
    for name in ("unity_plugin.py", "unity_ready.py", "unity_setup.py", "unity_teardown.py"):
        assert name in scripts, name


def test_builtin_unity_plugin_settings_schema():
    unity = get_plugin("unity")
    by_key = {s.key: s for s in unity.settings}
    assert set(by_key) == {
        "editor_mode",
        "mcp",
        "unity_path",
        "ready_timeout_sec",
        "ready_grace_sec",
    }
    assert by_key["editor_mode"].type == "select"
    assert by_key["editor_mode"].options == ("shared", "per_worktree")
    assert by_key["editor_mode"].default == "shared"
    assert by_key["ready_timeout_sec"].default == 600


def test_unity_is_trust_gated():
    """The [python] module is never built unless unity is in [plugins] enabled."""
    from bmad_loop.plugins import PluginRegistry

    off = PluginRegistry.build(policy=Policy())
    assert off.get("unity").instance is None  # untrusted: not enabled

    pol = Policy(plugins=PluginsPolicy(enabled=("unity",)))
    on = PluginRegistry.build(policy=pol)
    assert type(on.get("unity").instance).__name__ == "UnityPlugin"


# ------------------------------------------------------ UnityPlugin in-process hooks


def _unity_plugin_module():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_plugin.py")
    spec = importlib.util.spec_from_file_location("unity_plugin_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_unity(settings, scripts_dir):
    """Build a UnityPlugin instance whose {scripts} dir is a tmp dir we control."""
    cls = _unity_plugin_module().UnityPlugin
    manifest = PluginManifest(name="unity", api_version=1, scripts_dir=str(scripts_dir))
    full = {
        "editor_mode": "shared",
        "mcp": "ivanmurzak",
        "unity_path": "",
        "ready_timeout_sec": 600,
        "ready_grace_sec": -1,
    }
    full.update(settings)
    return cls(manifest, full)


def _fake_script(scripts_dir, name, rc):
    (scripts_dir / name).write_text(f"import sys\nsys.exit({rc})\n", encoding="utf-8")


def _ctx(stage, scripts_dir, *, agents=()):
    return HookContext(
        stage,
        run_id="r",
        story_key="1-1-a",
        repo_root=str(scripts_dir),
        run_dir=str(scripts_dir),
        worktree=str(scripts_dir),
        agents=tuple(agents),
    )


def test_ready_gate_passes_when_script_succeeds(tmp_path):
    _fake_script(tmp_path, "unity_ready.py", 0)
    inst = _make_unity({}, tmp_path)
    ctx = _ctx("pre_ready_gate", tmp_path)
    inst.on_pre_ready_gate(ctx)
    assert not ctx.vetoed


def test_ready_gate_vetoes_defer_when_script_fails(tmp_path):
    _fake_script(tmp_path, "unity_ready.py", 7)
    inst = _make_unity({}, tmp_path)
    ctx = _ctx("pre_ready_gate", tmp_path)
    inst.on_pre_ready_gate(ctx)
    veto = ctx.resolved_veto()
    assert veto is not None and veto.action == "defer"
    assert "not ready" in veto.reason and "rc=7" in veto.reason


def test_ready_gate_vetoes_when_script_missing(tmp_path):
    # no unity_ready.py on disk -> launch error -> defer (never crashes the run)
    inst = _make_unity({}, tmp_path)
    ctx = _ctx("pre_ready_gate", tmp_path)
    inst.on_pre_ready_gate(ctx)
    assert ctx.resolved_veto().action == "defer"


def test_worktree_setup_only_acts_in_per_worktree(tmp_path):
    _fake_script(tmp_path, "unity_setup.py", 5)
    # shared mode: setup is a no-op, no veto even though the script would fail
    shared = _make_unity({"editor_mode": "shared"}, tmp_path)
    ctx = _ctx("pre_worktree_setup", tmp_path)
    shared.on_pre_worktree_setup(ctx)
    assert not ctx.vetoed
    # per_worktree: setup runs and a failure vetoes
    pw = _make_unity({"editor_mode": "per_worktree"}, tmp_path)
    ctx2 = _ctx("pre_worktree_setup", tmp_path)
    pw.on_pre_worktree_setup(ctx2)
    assert ctx2.resolved_veto().action == "defer"


def test_worktree_teardown_is_best_effort(tmp_path):
    _fake_script(tmp_path, "unity_teardown.py", 9)
    pw = _make_unity({"editor_mode": "per_worktree"}, tmp_path)
    ctx = _ctx("pre_worktree_teardown", tmp_path)
    pw.on_pre_worktree_teardown(ctx)  # a failing teardown never vetoes
    assert not ctx.vetoed


# ------------------------------------------------------ UnityPlugin env contract


def test_engine_env_carries_settings_and_agents(tmp_path):
    inst = _make_unity(
        {
            "editor_mode": "per_worktree",
            "mcp": "coplaydev",
            "unity_path": "/opt/Unity/Editor",
            "ready_timeout_sec": 120,
            "ready_grace_sec": 30,
        },
        tmp_path,
    )
    ctx = _ctx("pre_ready_gate", tmp_path, agents=["claude-code", "codex"])
    env = inst.engine_env(ctx)
    assert env["BMAD_LOOP_ENGINE_MCP"] == "coplaydev"
    assert env["BMAD_LOOP_ENGINE_EDITOR_MODE"] == "per_worktree"
    assert env["BMAD_LOOP_ENGINE_READY_TIMEOUT"] == "120"
    assert env["BMAD_LOOP_ENGINE_READY_GRACE"] == "30"
    assert env["BMAD_LOOP_UNITY_PATH"] == "/opt/Unity/Editor"
    assert env["BMAD_LOOP_WORKTREE"] == str(tmp_path)
    assert env["BMAD_LOOP_STORY_KEY"] == "1-1-a"
    # MCP agent routing: dev + review CLIs, from ctx.agents
    assert env["BMAD_LOOP_ENGINE_AGENTS"] == "claude-code,codex"


def test_engine_env_omits_agents_when_none(tmp_path):
    inst = _make_unity({}, tmp_path)
    env = inst.engine_env(_ctx("pre_ready_gate", tmp_path, agents=()))
    assert "BMAD_LOOP_ENGINE_AGENTS" not in env


# --------------------------------------------------- editor_mode↔isolation coupling


def test_validate_accepts_matching_coupling(tmp_path):
    _make_unity({"editor_mode": "shared"}, tmp_path).validate(
        Policy(scm=ScmPolicy(isolation="none"))
    )
    _make_unity({"editor_mode": "per_worktree"}, tmp_path).validate(
        Policy(scm=ScmPolicy(isolation="worktree"))
    )


def test_validate_rejects_shared_with_worktree(tmp_path):
    inst = _make_unity({"editor_mode": "shared"}, tmp_path)
    with pytest.raises(PluginError, match="shared.*requires scm.isolation = 'none'"):
        inst.validate(Policy(scm=ScmPolicy(isolation="worktree")))


def test_validate_rejects_per_worktree_without_worktree(tmp_path):
    inst = _make_unity({"editor_mode": "per_worktree"}, tmp_path)
    with pytest.raises(PluginError, match="per_worktree.*requires scm.isolation"):
        inst.validate(Policy(scm=ScmPolicy(isolation="none")))


def test_validate_rejects_unknown_editor_mode(tmp_path):
    inst = _make_unity({"editor_mode": "live"}, tmp_path)
    with pytest.raises(PluginError, match="editor_mode must be one of"):
        inst.validate(Policy(scm=ScmPolicy(isolation="none")))


def test_engine_rejects_invalid_coupling_at_construction(project):
    """The engine runs registry.validate() at startup; a bad Unity coupling fails
    the run fast rather than mid-unit."""
    pol = Policy(
        notify=QUIET,
        plugins=PluginsPolicy(
            enabled=("unity",), settings={"unity": {"editor_mode": "per_worktree"}}
        ),
        scm=ScmPolicy(isolation="none"),  # violates per_worktree's coupling
    )
    from bmad_loop.adapters.mock import MockAdapter
    from bmad_loop.journal import Journal
    from bmad_loop.model import RunState, TokenUsage

    run_dir = project.project / ".bmad-loop" / "runs" / "r"
    with pytest.raises(PluginError, match="per_worktree.*requires scm.isolation"):
        Engine(
            paths=project,
            policy=pol,
            adapter=MockAdapter([], usage_per_session=TokenUsage(input_tokens=1, output_tokens=1)),
            run_dir=run_dir,
            journal=Journal(run_dir),
            state=RunState(run_id="r", project=str(project.project), started_at="now"),
        )


# ------------------------------------ per_worktree MCP agent routing (feeds ctx.agents)


def test_setup_mcp_agent_id_mapping():
    # only claude carries the "-code" suffix; everything else passes through
    assert _setup_mcp_agent_id("claude") == "claude-code"
    assert _setup_mcp_agent_id("codex") == "codex"
    assert _setup_mcp_agent_id("gemini") == "gemini"
    assert _setup_mcp_agent_id("cursor") == "cursor"
    assert _setup_mcp_agent_id("some-custom-profile") == "some-custom-profile"


class _FakeProfile:
    def __init__(self, name):
        self.name = name


class _FakeAdapter:
    def __init__(self, name):
        self.profile = _FakeProfile(name)


def _make_engine(project, policy):
    from bmad_loop.adapters.mock import MockAdapter
    from bmad_loop.journal import Journal
    from bmad_loop.model import RunState, TokenUsage

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter([], usage_per_session=TokenUsage(input_tokens=1, output_tokens=1))
    state = RunState(run_id="test-run", project=str(project.project), started_at="now")
    return Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
    )


def test_engine_agent_ids_dedups_dev_and_review(project):
    # a worktree can host two different CLIs (dev=claude, review=codex) — both must
    # be routed, deduped and order-preserved
    engine = _make_engine(project, Policy(notify=QUIET))
    engine.adapters = {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("codex")}
    assert engine._engine_agent_ids() == ["claude-code", "codex"]
    engine.adapters = {"dev": _FakeAdapter("codex"), "review": _FakeAdapter("codex")}
    assert engine._engine_agent_ids() == ["codex"]


def test_engine_agent_ids_empty_for_profileless_adapters(project):
    # MockAdapter has no .profile -> nothing to route (ctx.agents stays empty)
    engine = _make_engine(project, Policy(notify=QUIET))
    assert engine._engine_agent_ids() == []


# ------------------------------------------------ unity_ready cold-launch grace


def _load_unity_ready():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_ready.py")
    spec = importlib.util.spec_from_file_location("unity_ready_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unity_ready_grace_auto_per_mode(monkeypatch):
    mod = _load_unity_ready()
    monkeypatch.delenv("BMAD_LOOP_ENGINE_READY_GRACE", raising=False)
    # cold per_worktree Editor waits; warm shared Editor does not
    monkeypatch.setenv("BMAD_LOOP_ENGINE_EDITOR_MODE", "per_worktree")
    assert mod._grace() == 120.0
    monkeypatch.setenv("BMAD_LOOP_ENGINE_EDITOR_MODE", "shared")
    assert mod._grace() == 0.0
    # explicit -1 is the same auto path
    monkeypatch.setenv("BMAD_LOOP_ENGINE_READY_GRACE", "-1")
    monkeypatch.setenv("BMAD_LOOP_ENGINE_EDITOR_MODE", "per_worktree")
    assert mod._grace() == 120.0


def _load_unity_teardown():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_teardown.py")
    spec = importlib.util.spec_from_file_location("unity_teardown_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unity_teardown_lingering_scan_no_false_match(tmp_path):
    """The sweep is scoped to the worktree path + a 'unity'/'gamedev-mcp-server' exe
    basename, so a path no such process references yields nothing (never crashing)."""
    mod = _load_unity_teardown()
    assert mod._TARGET_BASENAMES == ("unity", "gamedev-mcp-server")
    # this test process references tmp_path but is python, not a target binary
    assert mod._lingering_pids(tmp_path) == []
    assert mod._force_kill_lingering(tmp_path) == 0


# A pid guaranteed to differ from the runner's own (the sweep excludes os.getpid(),
# so a hardcoded constant could silently match the runner and neuter these tests).
_FAKE_PID = os.getpid() + 1


class _RecordingHost:
    """A fake ProcessHost recording which pids it was asked to terminate/force-kill.
    ``alive`` drives whether a pid is treated as a survivor past the grace window;
    ``identity_recheck`` lets a test simulate pid reuse — the value the *second*
    identity() read of a pid returns, so the snapshot no longer matches."""

    def __init__(self, alive, identity=1.0, identity_recheck=None):
        self._alive = alive
        self._identity = identity
        self._identity_recheck = identity if identity_recheck is None else identity_recheck
        self._identity_seen: dict[int, int] = {}
        self.terminated: list[int] = []
        self.force_killed: list[int] = []

    def terminate(self, pid):
        self.terminated.append(pid)

    def force_kill(self, pid):
        self.force_killed.append(pid)

    def is_alive(self, pid):
        return self._alive

    def identity(self, pid):
        seen = self._identity_seen.get(pid, 0)
        self._identity_seen[pid] = seen + 1
        return self._identity if seen == 0 else self._identity_recheck


def test_unity_teardown_sweep_escalates_to_force_kill(tmp_path, monkeypatch):
    """After de-dup the sweep routes through get_process_host(): a stubborn survivor
    (is_alive stays True past the grace window) is SIGTERM'd then force-killed."""
    mod = _load_unity_teardown()
    host = _RecordingHost(alive=True)
    monkeypatch.setattr(mod, "get_process_host", lambda: host)
    monkeypatch.setattr(mod, "_lingering_pids", lambda wt: [_FAKE_PID])
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)  # don't really wait 10s

    assert mod._force_kill_lingering(tmp_path) == 1
    assert host.terminated == [_FAKE_PID]
    assert host.force_killed == [_FAKE_PID]


def test_unity_teardown_sweep_skips_force_kill_when_terminate_suffices(tmp_path, monkeypatch):
    """A pid that exits after the polite SIGTERM is never force-killed."""
    mod = _load_unity_teardown()
    host = _RecordingHost(alive=False)  # already gone by the first liveness poll
    monkeypatch.setattr(mod, "get_process_host", lambda: host)
    monkeypatch.setattr(mod, "_lingering_pids", lambda wt: [_FAKE_PID])
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    assert mod._force_kill_lingering(tmp_path) == 1
    assert host.terminated == [_FAKE_PID]
    assert host.force_killed == []


def test_unity_teardown_sweep_skips_force_kill_on_identity_mismatch(tmp_path, monkeypatch):
    """A survivor whose identity changed between snapshot and escalation (pid reused)
    is never force-killed — guarding against SIGKILL landing on an unrelated process."""
    mod = _load_unity_teardown()
    host = _RecordingHost(alive=True, identity=1.0, identity_recheck=2.0)
    monkeypatch.setattr(mod, "get_process_host", lambda: host)
    monkeypatch.setattr(mod, "_lingering_pids", lambda wt: [_FAKE_PID])
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    assert mod._force_kill_lingering(tmp_path) == 1
    assert host.terminated == [_FAKE_PID]
    assert host.force_killed == []  # identity no longer matches → refused


def test_unity_ready_grace_explicit_override(monkeypatch):
    mod = _load_unity_ready()
    monkeypatch.setenv("BMAD_LOOP_ENGINE_EDITOR_MODE", "per_worktree")
    monkeypatch.setenv("BMAD_LOOP_ENGINE_READY_GRACE", "30")
    assert mod._grace() == 30.0  # explicit value wins over the per-mode default
    monkeypatch.setenv("BMAD_LOOP_ENGINE_READY_GRACE", "0")
    assert mod._grace() == 0.0  # operator can force no grace even for per_worktree


# --------------------------------- unity_ready client-independent round-trip


def _fake_cli(tmp_path, *, wait_rc=0, tool_rc=0):
    """A fake unity-mcp-cli that logs argv and returns per-subcommand exit codes."""
    log = tmp_path / "calls.log"
    body = (
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        f"sys.exit({wait_rc} if cmd == 'wait-for-ready' "
        f"else ({tool_rc} if cmd == 'run-tool' else 0))\n"
    )
    return write_script_launcher(tmp_path, "fake-unity-mcp-cli", body), log


def test_unity_ready_default_is_wait_for_ready_only(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, log = _fake_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_WORKTREE", str(tmp_path))
    monkeypatch.delenv("BMAD_LOOP_UNITY_READY_TOOL", raising=False)
    # default: the Editor hosts its own server, so wait-for-ready alone gates it
    assert mod._ready_ivanmurzak(time.monotonic() + 10) == 0
    calls = log.read_text()
    assert "wait-for-ready" in calls
    assert "run-tool" not in calls  # round-trip is opt-in


def test_unity_ready_optional_tool_roundtrip(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, log = _fake_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_UNITY_READY_TOOL", "ping")  # opt in
    assert mod._ready_ivanmurzak(time.monotonic() + 10) == 0
    assert "run-tool ping" in log.read_text()


def test_unity_ready_tool_error_marker_means_not_ready(tmp_path, monkeypatch):
    """A run-tool that exits 0 but prints an error marker (the CLI returns 0 on a
    connection-refused) is treated as not-ready, not a false pass."""
    mod = _load_unity_ready()
    # exit 0 but stdout carries an error marker
    script = write_script_launcher(
        tmp_path,
        "fake-cli",
        "import sys\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "print('ERROR: Tool with Name not found' if cmd == 'run-tool' else 'ok')\n"
        "sys.exit(0)\n",
    )
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_UNITY_READY_TOOL", "ping")
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod._ready_ivanmurzak(time.monotonic() + 0.2) == 1


def test_unity_ready_not_ready_when_tool_never_answers(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, _ = _fake_cli(tmp_path, wait_rc=0, tool_rc=1)  # bridge up, tool failing
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_UNITY_READY_TOOL", "ping")  # opt in to exercise the loop
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)  # don't burn the retry pause
    assert mod._ready_ivanmurzak(time.monotonic() + 0.2) == 1


# ------------------------------------------ unity_setup custom/local-mode launch


def _load_unity_setup():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_setup.py")
    spec = importlib.util.spec_from_file_location("unity_setup_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SETUP_KNOBS = (
    "BMAD_LOOP_UNITY_MCP_LOCAL",
    "BMAD_LOOP_UNITY_MCP_URL",
    "BMAD_LOOP_UNITY_MCP_TOKEN",
    "BMAD_LOOP_UNITY_MCP_TRANSPORT",
    "BMAD_LOOP_UNITY_MCP_AUTH",
    "BMAD_LOOP_UNITY_MCP_START_SERVER",
    "BMAD_LOOP_UNITY_MCP_KEEP_CONNECTED",
    "BMAD_LOOP_UNITY_PATH",
    "BMAD_LOOP_UNITY_LIBRARY_SEED",
    "BMAD_LOOP_UNITY_LIBRARY_SEED_MODE",
    "BMAD_LOOP_UNITY_LIBRARY_CACHE",
    "BMAD_LOOP_REPO_ROOT",
)


def _clear_setup_knobs(monkeypatch):
    for knob in _SETUP_KNOBS:
        monkeypatch.delenv(knob, raising=False)


def test_unity_setup_local_url_from_mcp_json(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"ai-game-developer": {"type": "http", "url": "http://localhost:28536"}}}
        )
    )
    assert mod._local_url(tmp_path) == "http://localhost:28536"
    # explicit override wins over the file
    monkeypatch.setenv("BMAD_LOOP_UNITY_MCP_URL", "http://localhost:9999")
    assert mod._local_url(tmp_path) == "http://localhost:9999"


def test_unity_setup_local_url_missing_returns_none(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    assert mod._local_url(tmp_path) is None  # no .mcp.json, no override


def test_unity_setup_open_command_local_defaults(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    cmd = mod._open_command("ucli", tmp_path, "http://localhost:1234")
    assert cmd[:3] == ["ucli", "open", str(tmp_path)]
    assert cmd[cmd.index("--url") + 1] == "http://localhost:1234"
    assert cmd[cmd.index("--transport") + 1] == "streamableHttp"
    assert cmd[cmd.index("--auth") + 1] == "none"
    assert cmd[cmd.index("--start-server") + 1] == "true"
    assert "--keep-connected" in cmd
    assert "--token" not in cmd  # empty default token is omitted


def test_unity_setup_open_command_cloud_fallback(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    # no derivable URL → bare open (project's persisted/cloud config)
    assert mod._open_command("ucli", tmp_path, None) == ["ucli", "open", str(tmp_path)]
    # explicit opt-out even with a URL available
    monkeypatch.setenv("BMAD_LOOP_UNITY_MCP_LOCAL", "0")
    assert "--url" not in mod._open_command("ucli", tmp_path, "http://localhost:1")


def test_unity_setup_open_command_overrides(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    monkeypatch.setenv("BMAD_LOOP_UNITY_PATH", "/opt/Unity/Editor")
    monkeypatch.setenv("BMAD_LOOP_UNITY_MCP_TOKEN", "secret")
    monkeypatch.setenv("BMAD_LOOP_UNITY_MCP_START_SERVER", "false")
    monkeypatch.setenv("BMAD_LOOP_UNITY_MCP_KEEP_CONNECTED", "false")
    cmd = mod._open_command("ucli", tmp_path, "http://localhost:1")
    assert cmd[cmd.index("--start-server") + 1] == "false"
    assert "--keep-connected" not in cmd
    assert cmd[cmd.index("--token") + 1] == "secret"
    assert cmd[cmd.index("--editor-path") + 1] == "/opt/Unity/Editor"


def test_unity_setup_truthy(monkeypatch):
    mod = _load_unity_setup()
    assert mod._truthy(None, True) is True
    assert mod._truthy("", False) is False
    assert mod._truthy("0", True) is False
    assert mod._truthy("yes", False) is True
    assert mod._truthy("  True ", False) is True


# ------------------------------------------ unity_setup Library priming


def _warm_library(root):
    """A fake warm main Library: a couple of cache files plus the volatile cruft a
    primed copy must strip (per-Editor identity, a lock, a pid)."""
    lib = root / "Library"
    (lib / "Artifacts").mkdir(parents=True)
    (lib / "Artifacts" / "ab").write_text("artifact")
    (lib / "ArtifactDB").write_text("db")
    (lib / "EditorInstance.json").write_text('{"process_id":123}')
    (lib / "ArtifactDB-lock").write_text("lock")
    (lib / "ilpp.pid").write_text("123")
    return lib


def test_unity_setup_prime_reflink_from_warm_library(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    lib = wt / "Library"
    assert not lib.is_symlink()  # a real (primed) directory, not a symlink
    assert (lib / "ArtifactDB").read_text() == "db"  # warm cache copied in
    assert (lib / "Artifacts" / "ab").read_text() == "artifact"
    # volatile per-Editor identity / locks / pids stripped from the copy
    assert not (lib / "EditorInstance.json").exists()
    assert not (lib / "ArtifactDB-lock").exists()
    assert not (lib / "ilpp.pid").exists()


def test_unity_setup_prime_leaves_substantive_library(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    (wt / "Library" / "Artifacts").mkdir(parents=True)
    (wt / "Library" / "existing").write_text("keep")
    mod._prime_library(wt)
    # a genuine Library is never clobbered
    assert (wt / "Library" / "existing").read_text() == "keep"
    assert not (wt / "Library" / "ArtifactDB").exists()  # seed not copied over it


def test_unity_setup_prime_scriptassemblies_only_is_cold(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    (wt / "Library" / "ScriptAssemblies").mkdir(parents=True)  # cold leftover only
    mod._prime_library(wt)
    assert (wt / "Library" / "ArtifactDB").read_text() == "db"  # primed over leftover


_skip_win_symlink = pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink-based Unity Library priming is POSIX-only; native-Windows cache path is a tracked follow-up",
)


@_skip_win_symlink
def test_unity_setup_prime_symlink_fallback_when_no_seed(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", "")  # disable priming
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_CACHE", str(tmp_path / "cache"))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    assert (wt / "Library").is_symlink()  # fell back to the empty-cache symlink


@_skip_win_symlink
def test_unity_setup_prime_seed_mode_off_uses_symlink(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", str(seed))
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED_MODE", "off")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_CACHE", str(tmp_path / "cache"))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    assert (wt / "Library").is_symlink()  # priming disabled despite a warm seed


@_skip_win_symlink
def test_unity_setup_prime_drops_stale_symlink(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_LOOP_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "Library").symlink_to(tmp_path / "old-cache")  # stale symlink-mode link
    mod._prime_library(wt)
    assert not (wt / "Library").is_symlink()  # replaced with a primed real dir
    assert (wt / "Library" / "ArtifactDB").read_text() == "db"


# ------------------------------------ unity_setup engine-agent env + isolation


def test_unity_setup_engine_agent_ids_env_parsing(monkeypatch):
    mod = _load_unity_setup()
    monkeypatch.delenv("BMAD_LOOP_ENGINE_AGENTS", raising=False)
    monkeypatch.delenv("BMAD_LOOP_ENGINE_AGENT", raising=False)
    assert mod._engine_agent_ids() == ["claude-code"]  # default
    monkeypatch.setenv("BMAD_LOOP_ENGINE_AGENT", "codex")  # legacy singular fallback
    assert mod._engine_agent_ids() == ["codex"]
    monkeypatch.setenv("BMAD_LOOP_ENGINE_AGENTS", "claude-code, codex ,claude-code")
    assert mod._engine_agent_ids() == ["claude-code", "codex"]  # strip + dedup, plural wins


def test_verify_agent_isolation(tmp_path):
    mod = _load_unity_setup()
    url = "http://localhost:23723"
    cdir = tmp_path / ".codex"
    cdir.mkdir()
    # config points at the worktree's port -> isolated
    (cdir / "config.toml").write_text(
        '[mcp_servers.ai-game-developer]\nurl = "http://localhost:23723"\n'
    )
    assert mod._verify_agent_isolation("codex", tmp_path, url) is True
    # config still points at a leaked main-project port -> refuse
    (cdir / "config.toml").write_text(
        '[mcp_servers.ai-game-developer]\nurl = "http://localhost:23191"\n'
    )
    assert mod._verify_agent_isolation("codex", tmp_path, url) is False
    # config missing -> can't guarantee isolation -> refuse
    assert mod._verify_agent_isolation("gemini", tmp_path, url) is False
    # agent without a worktree-local config path -> not verifiable here -> pass through
    assert mod._verify_agent_isolation("claude-desktop", tmp_path, url) is True


def _fake_setup_cli(tmp_path):
    """Fake unity-mcp-cli for the setup hook: setup-mcp writes claude-code's
    .mcp.json (path-derived 23723) and codex's .codex/config.toml (honoring --url,
    or FAKE_CODEX_FORCE_URL to simulate a leaked port); other subcommands no-op 0."""
    log = tmp_path / "setup-calls.log"
    body = (
        "import sys, os, json\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "a = sys.argv[1:]\n"
        "if a[:1] == ['setup-mcp']:\n"
        "    agent, path = a[1], a[2]\n"
        "    url = a[a.index('--url') + 1] if '--url' in a else 'http://localhost:23723'\n"
        "    if agent == 'claude-code':\n"
        "        json.dump({'mcpServers': {'ai-game-developer': {'url': 'http://localhost:23723'}}},\n"
        "                  open(os.path.join(path, '.mcp.json'), 'w'))\n"
        "    elif agent == 'codex':\n"
        "        u = os.environ.get('FAKE_CODEX_FORCE_URL') or url\n"
        "        d = os.path.join(path, '.codex'); os.makedirs(d, exist_ok=True)\n"
        "        open(os.path.join(d, 'config.toml'), 'w').write(\n"
        "            '[mcp_servers.ai-game-developer]\\nurl = \"%s\"\\n' % u)\n"
        "sys.exit(0)\n"
    )
    return write_script_launcher(tmp_path, "fake-setup-cli", body), log


def test_unity_setup_configures_every_agent_at_worktree_port(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    script, log = _fake_setup_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_ENGINE_AGENTS", "claude-code,codex")
    monkeypatch.delenv("FAKE_CODEX_FORCE_URL", raising=False)
    assert mod._setup_ivanmurzak(tmp_path) == 0
    calls = log.read_text()
    assert calls.count("setup-mcp claude-code") == 1  # written once, no duplicate
    assert "setup-mcp codex" in calls
    assert "--url http://localhost:23723" in calls  # codex forced to the worktree url
    # codex's real config now points at the worktree Editor, not a leaked port
    assert "23723" in (tmp_path / ".codex" / "config.toml").read_text()


def test_unity_setup_fails_loud_when_agent_config_leaks(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    script, _ = _fake_setup_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_LOOP_ENGINE_AGENTS", "claude-code,codex")
    # simulate the bug: codex config still pinned to the main project's port
    monkeypatch.setenv("FAKE_CODEX_FORCE_URL", "http://localhost:23191")
    # isolation check rejects -> non-zero so the engine defers the unit (no work)
    assert mod._setup_ivanmurzak(tmp_path) == 1
