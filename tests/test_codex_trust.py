"""Codex trust is checked at the same executable and directory as a session."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import install_bmad_config, windows_relay_builder, write_script_launcher

from bmad_loop import cli, codex_trust, probe
from bmad_loop.adapters.profile import ProfileError, get_profile
from bmad_loop.install import _hook_command, merge_hooks


def _config(root: Path, commands: dict[str, str] | None = None) -> dict:
    profile = get_profile("codex")
    if commands is None:
        commands = {
            event: _hook_command(root, profile, event) for event in ("SessionStart", "Stop")
        }
    data, _ = merge_hooks({}, commands, profile.hooks.dialect)
    path = root / profile.hooks.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _rpc(root: Path, data: dict, status: str = "trusted") -> dict:
    hooks = [
        {
            "sourcePath": str((root / ".codex/hooks.json").resolve()),
            "eventName": event[0].lower() + event[1:],
            "handlerType": "command",
            "command": data["hooks"][event][0]["hooks"][0]["command"],
            "enabled": True,
            "trustStatus": status,
        }
        for event in ("SessionStart", "Stop")
    ]
    return {"data": [{"cwd": str(root.resolve()), "errors": [], "warnings": [], "hooks": hooks}]}


@pytest.mark.parametrize("event", ["SessionStart", "Stop"])
def test_trust_rejects_old_script_command_for_each_required_event(tmp_path, monkeypatch, event):
    data = _config(tmp_path)
    data["hooks"][event][0]["hooks"][0][
        "command"
    ] = f"python3 {tmp_path / '.bmad-loop/bmad_loop_hook.py'} {event}"
    (tmp_path / ".codex/hooks.json").write_text(json.dumps(data))
    monkeypatch.setattr(
        codex_trust,
        "_hooks_list",
        lambda *_: pytest.fail("an old command must not be queried as current trust"),
    )
    result = codex_trust.project_hook_trust(tmp_path, get_profile("codex"))
    assert result.status == "untrusted" and "relay" in result.reason


def test_trust_ignores_unrelated_command_containing_bmad_loop(tmp_path, monkeypatch):
    data = _config(tmp_path)
    data["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "echo /tools/bmad-loop/notice"}
    )
    (tmp_path / ".codex/hooks.json").write_text(json.dumps(data))
    monkeypatch.setattr(codex_trust, "resolved_codex_binary", lambda *_: "codex-stub")
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: _rpc(tmp_path, data))
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "trusted"


def test_trust_rejects_installed_relay_at_wrong_path(tmp_path, monkeypatch):
    data = _config(tmp_path)
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = "/old/bin/bmad-loop relay Stop"
    (tmp_path / ".codex/hooks.json").write_text(json.dumps(data))
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: pytest.fail("wrong relay path"))
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "untrusted"


def test_trust_reads_backslash_windows_relay_as_stale_and_forward_slash_as_current(
    tmp_path, monkeypatch
):
    """After #773 the expected Windows relay names its executable with forward
    slashes. A pre-#773 backslash registration (broken under Git Bash anyway) is
    still recognized as the relay but differs from the command init writes now:
    it reads untrusted — re-run init — without querying Codex or raising."""
    windows_exe = r"C:\Users\me\.local\bin\bmad-loop.exe"
    build = windows_relay_builder(monkeypatch, tmp_path, windows_exe)
    monkeypatch.setattr(codex_trust, "_hook_command", build)
    old = _config(tmp_path, {e: rf"{windows_exe} relay {e}" for e in ("SessionStart", "Stop")})
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: pytest.fail("stale relay"))
    result = codex_trust.project_hook_trust(tmp_path, get_profile("codex"))
    assert result.status == "untrusted" and "not usable" in result.reason

    profile = get_profile("codex")
    current = _config(tmp_path, {e: build(tmp_path, profile, e) for e in ("SessionStart", "Stop")})
    assert current != old
    assert current["hooks"]["Stop"][0]["hooks"][0]["command"] == (
        "C:/Users/me/.local/bin/bmad-loop.exe relay Stop"
    )
    monkeypatch.setattr(codex_trust, "resolved_codex_binary", lambda *_: "codex-stub")
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: _rpc(tmp_path, current))
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "trusted"


def test_trust_degrades_when_installed_relay_disappears(tmp_path, monkeypatch):
    _config(tmp_path)

    def missing(*_args):
        raise ProfileError("installed bmad-loop command is unavailable")

    monkeypatch.setattr(codex_trust, "_hook_command", missing)
    result = codex_trust.project_hook_trust(tmp_path, get_profile("codex"))
    assert result.status == "unverifiable"
    assert "installed relay unavailable" in result.reason


def test_scripted_app_server_executes_request_sequence_and_reads_environment(tmp_path):
    """An actual zero-token child parses initialize and hooks/list, not a mocked RPC."""
    data = _config(tmp_path)
    reply = _rpc(tmp_path, data)
    log = tmp_path / "requests.json"
    script = write_script_launcher(
        tmp_path,
        "codex-stub",
        "import json, os, sys\n"
        "requests = []\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line); requests.append(msg)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    if msg.get('method') == 'hooks/list':\n"
        "        assert os.environ['CODEX_HOME'] == 'test-home'\n"
        f"        open({str(log)!r}, 'w').write(json.dumps(requests))\n"
        f"        print(json.dumps({{'id': 2, 'result': {reply!r}}}), flush=True)\n",
    )
    profile = replace(get_profile("codex"), binary=str(script), env={"CODEX_HOME": "test-home"})
    result = codex_trust.project_hook_trust(tmp_path, profile)
    assert result.status == "trusted", result.reason
    requests = json.loads(log.read_text(encoding="utf-8"))
    assert [item["method"] for item in requests] == ["initialize", "initialized", "hooks/list"]
    assert requests[-1]["params"]["cwds"] == [str(tmp_path.resolve())]


def test_trust_resolves_codex_cmd_shim_before_spawning(tmp_path, monkeypatch):
    data = _config(tmp_path)
    resolved = r"C:\Program Files\nodejs\codex.cmd"
    profile = replace(get_profile("codex"), env={"PATH": r"C:\Program Files\nodejs"})

    def which(binary, *, path=None):
        assert binary == "codex"
        assert path == profile.env["PATH"]
        return resolved

    monkeypatch.setattr(codex_trust.shutil, "which", which)

    def hooks_list(binary, cwd, env):
        assert binary == resolved
        assert cwd == tmp_path
        return _rpc(tmp_path, data)

    monkeypatch.setattr(codex_trust, "_hooks_list", hooks_list)
    assert codex_trust.project_hook_trust(tmp_path, profile).status == "trusted"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("start-modified", "untrusted"),
        ("start-untrusted", "untrusted"),
        ("start-omitted", "untrusted"),
        ("modified", "untrusted"),
        ("untrusted", "untrusted"),
        ("disabled", "untrusted"),
        ("omitted", "untrusted"),
        ("wrong-command", "untrusted"),
        ("malformed-status", "unverifiable"),
        ("wrong-source", "untrusted"),
    ],
)
def test_trust_refuses_stale_or_unmatched_relay(tmp_path, monkeypatch, mutation, expected):
    data = _config(tmp_path)
    monkeypatch.setattr(codex_trust, "resolved_codex_binary", lambda *_: "codex-stub")
    result = _rpc(tmp_path, data)
    hooks = result["data"][0]["hooks"]
    if mutation == "start-modified":
        hooks[0]["trustStatus"] = "modified"
    elif mutation == "start-untrusted":
        hooks[0]["trustStatus"] = "untrusted"
    elif mutation == "start-omitted":
        hooks.pop(0)
    elif mutation == "modified":
        hooks[1]["trustStatus"] = "modified"
    elif mutation == "untrusted":
        hooks[1]["trustStatus"] = "untrusted"
    elif mutation == "disabled":
        hooks[1]["enabled"] = False
    elif mutation == "omitted":
        hooks.pop()
    elif mutation == "wrong-command":
        hooks[1]["command"] = "echo unrelated"
    elif mutation == "malformed-status":
        hooks[1]["trustStatus"] = []
    elif mutation == "wrong-source":
        hooks[1]["sourcePath"] = "/tmp/other/hooks.json"
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: result)
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == expected


def test_trust_refuses_relay_for_old_checkout_and_startup_excluding_matcher(tmp_path, monkeypatch):
    data = _config(tmp_path)
    monkeypatch.setattr(codex_trust, "resolved_codex_binary", lambda *_: "codex-stub")
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: _rpc(tmp_path, data))
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "trusted"

    data["hooks"]["SessionStart"][0]["matcher"] = "^resume$"
    (tmp_path / ".codex/hooks.json").write_text(json.dumps(data), encoding="utf-8")
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "untrusted"

    data["hooks"]["SessionStart"][0].pop("matcher")
    data["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ] = f"python3 {tmp_path / 'old-checkout/.bmad-loop/bmad_loop_hook.py'} Stop"
    (tmp_path / ".codex/hooks.json").write_text(json.dumps(data), encoding="utf-8")
    assert codex_trust.project_hook_trust(tmp_path, get_profile("codex")).status == "untrusted"


def test_missing_profile_stop_and_unsupported_launch_args_fail_closed(tmp_path, monkeypatch):
    data = _config(tmp_path)
    monkeypatch.setattr(codex_trust, "resolved_codex_binary", lambda *_: "codex-stub")
    monkeypatch.setattr(codex_trust, "_hooks_list", lambda *_: _rpc(tmp_path, data))
    profile = get_profile("codex")
    assert (
        codex_trust.project_hook_trust(tmp_path, replace(profile, launch_args=("-c", "x=1"))).status
        == "unverifiable"
    )
    assert (
        codex_trust.project_hook_trust(
            tmp_path, replace(profile, bypass_args=("-C", "other"))
        ).status
        == "unverifiable"
    )
    assert (
        codex_trust.project_hook_trust(
            tmp_path, replace(profile, env={"CODEX_HOME": "another"})
        ).status
        == "trusted"
    )
    config_path = tmp_path / profile.hooks.config_path
    config_path.write_text(json.dumps({"hooks": {"SessionStart": data["hooks"]["SessionStart"]}}))
    assert codex_trust.project_hook_trust(tmp_path, profile).status == "untrusted"
    config_path.write_text(json.dumps(data))
    assert (
        codex_trust.project_hook_trust(
            tmp_path,
            replace(profile, hooks=replace(profile.hooks, events={"SessionStart": "SessionStart"})),
        ).status
        == "untrusted"
    )


def test_validate_names_untrusted_hook_and_refuses_worktree_inference(project, monkeypatch, capsys):
    from bmad_loop.install import install_into

    install_bmad_config(project)
    install_into(project.project, clis=("codex",))
    capsys.readouterr()
    policy = project.project / ".bmad-loop/policy.toml"
    policy.write_text('[adapter]\nname = "codex"\n', encoding="utf-8")
    monkeypatch.setattr(
        codex_trust,
        "project_hook_trust",
        lambda *_args, **_kwargs: codex_trust.TrustResult("untrusted", "hook trust stale"),
    )
    cli.main(["validate", "--project", str(project.project), "--json"])
    out, err = capsys.readouterr()
    assert out, err
    doc = json.loads(out)
    findings = [f for f in doc["findings"] if f["check"] == "hooks.trust"]
    assert len(findings) == 1 and findings[0]["severity"] == "problem"
    assert "hook trust stale" in findings[0]["message"]

    policy.write_text(
        '[adapter]\nname = "codex"\n[scm]\nisolation = "worktree"\n', encoding="utf-8"
    )
    cli.main(["validate", "--project", str(project.project), "--json"])
    doc = json.loads(capsys.readouterr().out)
    findings = [f for f in doc["findings"] if f["check"] == "hooks.trust"]
    assert len(findings) == 1 and "worktree" in findings[0]["message"]


def test_validate_trust_query_uses_selected_project(project, tmp_path, monkeypatch, capsys):
    from bmad_loop.install import install_into

    install_bmad_config(project)
    install_into(project.project, clis=("codex",))
    capsys.readouterr()
    (project.project / ".bmad-loop/policy.toml").write_text(
        '[adapter]\nname = "codex"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    queried = []

    def trust(path, _profile):
        queried.append(path)
        return codex_trust.TrustResult("trusted", "hook trust current")

    monkeypatch.setattr(codex_trust, "project_hook_trust", trust)
    cli.main(["validate", "--project", str(project.project), "--json"])
    finding = next(
        f for f in json.loads(capsys.readouterr().out)["findings"] if f["check"] == "hooks.trust"
    )
    assert finding["severity"] == "ok"
    assert queried == [project.project]


def test_validate_does_not_run_project_owned_codex_profile(project, tmp_path, capsys):
    from bmad_loop.install import install_into

    install_bmad_config(project)
    install_into(project.project, clis=("codex",))
    capsys.readouterr()
    policy = project.project / ".bmad-loop/policy.toml"
    policy.write_text('[adapter]\nname = "codex"\n', encoding="utf-8")
    sentinel = tmp_path / "executed"
    binary = write_script_launcher(
        project.project,
        "codex-stub",
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('yes')\n",
    )
    overlay = project.project / ".bmad-loop/profiles/codex.toml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        f'name = "codex"\nbinary = {json.dumps(str(binary))}\n'
        '[hooks]\ndialect = "codex-hooks-json"\nconfig_path = ".codex/hooks.json"\n'
        'events = { SessionStart = "SessionStart", Stop = "Stop" }\n',
        encoding="utf-8",
    )
    cli.main(["validate", "--project", str(project.project), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert not sentinel.exists()
    finding = next(f for f in doc["findings"] if f["check"] == "hooks.trust")
    assert finding["severity"] == "problem" and "project-owned" in finding["message"]


@pytest.mark.parametrize("role", ["dev", "review", "triage"])
def test_validate_refuses_codex_stage_extra_args_that_change_hook_root(
    project, monkeypatch, capsys, role
):
    from bmad_loop.install import install_into

    install_bmad_config(project)
    install_into(project.project, clis=("codex",))
    capsys.readouterr()
    policy = project.project / ".bmad-loop/policy.toml"
    policy.write_text(
        f'[adapter]\nname = "codex"\n[adapter.{role}]\n'
        'extra_args = ["-C", "/another/project"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_trust,
        "project_hook_trust",
        lambda *_a, **_kw: pytest.fail("a different launch root cannot certify hook trust"),
    )
    cli.main(["validate", "--project", str(project.project), "--json"])
    doc = json.loads(capsys.readouterr().out)
    trust = next(f for f in doc["findings"] if f["check"] == "hooks.trust")
    assert trust["severity"] == "problem"
    assert "adapter.extra_args" in trust["message"] and role in trust["message"]


def test_scan_and_live_probe_refuse_trust_at_their_own_directories(tmp_path, monkeypatch):
    profile = get_profile("codex")
    _config(tmp_path)
    calls = []

    def trust(path, _profile, *, binary=None, marker=None):
        calls.append((path, binary, marker))
        return codex_trust.TrustResult("untrusted", "hook trust stale")

    monkeypatch.setattr(codex_trust, "project_hook_trust", trust)
    monkeypatch.setattr(probe, "run_version_help", lambda binary: probe.FlagFinding(binary, True))
    scanned = probe.scan(
        cli="codex", profile=profile, project=tmp_path, hints=probe.Hints(binary="chosen")
    )
    assert scanned.hook_trust == "untrusted" and calls[-1][:2] == (tmp_path, "chosen")
    assert calls[-1][2] == "bmad-loop"

    class Mux:
        def available(self):
            return True

    class Launcher:
        def __init__(self, **_kwargs):
            pass

        def start(self, *_args):
            pytest.fail("untrusted temporary hook config must stop before launch")

        def kill(self):
            pass

    monkeypatch.setattr(probe, "get_multiplexer", Mux)
    monkeypatch.setattr(probe, "_ProbeLauncher", Launcher)
    monkeypatch.setattr(probe.shutil, "which", lambda _binary, **_kwargs: "/bin/true")
    live = probe.probe(
        cli="codex", profile=profile, project=tmp_path, hints=probe.Hints(binary="chosen")
    )
    assert live.hook_trust == "untrusted"
    assert calls[-1][0] != tmp_path and calls[-1][1:] == ("chosen", probe.PROBE_HOOK_NAME)
    assert "temporary probe workspace" in live.warnings[0]


def test_trusted_live_probe_checks_temp_config_then_starts_zero_token_launcher(
    tmp_path, monkeypatch
):
    profile = get_profile("codex")
    events = []

    def trust(path, _profile, *, binary=None, marker=None):
        assert (path / profile.hooks.config_path).is_file()
        events.append(("trust", path, binary, marker))
        return codex_trust.TrustResult("trusted", "hook trust current")

    class Mux:
        def available(self):
            return True

    class Launcher:
        def __init__(self, **_kwargs):
            pass

        def start(self, argv, _env, cwd, _log_file):
            events.append(("start", cwd, argv[0]))
            return "fake-window"

        def kill(self):
            events.append(("kill",))

    class Watcher:
        def __init__(self, _capture_dir):
            pass

        def wait_for(self, *_args, **_kwargs):
            return object()  # scripted Stop; no model turn

    monkeypatch.setattr(codex_trust, "project_hook_trust", trust)
    monkeypatch.setattr(probe, "get_multiplexer", Mux)
    monkeypatch.setattr(probe, "_ProbeLauncher", Launcher)
    monkeypatch.setattr(probe, "SignalWatcher", Watcher)
    monkeypatch.setattr(probe.shutil, "which", lambda _binary, **_kwargs: "/bin/true")
    monkeypatch.setattr(probe, "run_version_help", lambda binary: probe.FlagFinding(binary, True))
    monkeypatch.setattr(probe, "discover_transcript", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    finding = probe.probe(
        cli="codex", profile=profile, project=tmp_path, hints=probe.Hints(binary="chosen")
    )
    assert finding.hook_trust == "trusted"
    assert events[0][0] == "trust" and events[0][1] != tmp_path
    assert events[0][2:] == ("chosen", probe.PROBE_HOOK_NAME)
    assert events[1] == ("start", events[0][1], "/bin/true")
    assert events[-1] == ("kill",)


def test_probe_json_exits_nonzero_and_names_hook_trust(tmp_path, monkeypatch, capsys):
    _config(tmp_path)
    monkeypatch.setattr(
        codex_trust,
        "project_hook_trust",
        lambda *_args, **_kwargs: codex_trust.TrustResult("untrusted", "hook trust stale"),
    )
    monkeypatch.setattr(probe, "run_version_help", lambda binary: probe.FlagFinding(binary, True))
    monkeypatch.setattr(probe, "discover_transcript", lambda *_args, **_kwargs: None)
    rc = cli.main(
        ["probe-adapter", "codex", "--project", str(tmp_path), "--binary", "chosen", "--json"]
    )
    out, err = capsys.readouterr()
    assert rc == 1 and "FAIL" in err
    doc = json.loads(out)
    assert doc["hook_trust"] == "untrusted"
    assert "hook trust stale" in doc["warnings"][0]


def test_probe_scan_with_unregistered_codex_hooks_is_non_green(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "run_version_help", lambda binary: probe.FlagFinding(binary, True))
    monkeypatch.setattr(probe, "discover_transcript", lambda *_args, **_kwargs: None)
    rc = cli.main(["probe-adapter", "codex", "--project", str(tmp_path), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["hooks_registered"] is False
    assert doc["hook_trust"] != "trusted"
    assert any("hook trust" in warning for warning in doc["warnings"])


def test_probe_codex_with_invalid_profile_cannot_fall_back_to_green_scan(tmp_path, capsys):
    overlay = tmp_path / ".bmad-loop/profiles/codex.toml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("invalid = [", encoding="utf-8")
    rc = cli.main(
        ["probe-adapter", "codex", "--project", str(tmp_path), "--binary", "chosen", "--json"]
    )
    out, err = capsys.readouterr()
    doc = json.loads(out)
    assert rc == 1 and "FAIL" in err
    assert doc["hook_trust"] == "unverifiable"
    assert any("hook trust unverifiable" in warning for warning in doc["warnings"])


def test_continuous_unrelated_messages_cannot_extend_rpc_deadline(tmp_path, monkeypatch):
    script = write_script_launcher(
        tmp_path,
        "chatty-codex",
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    if message.get('method') == 'hooks/list':\n"
        "        deadline = time.monotonic() + 1\n"
        "        while time.monotonic() < deadline:\n"
        "            print(json.dumps({'method': 'unrelated'}), flush=True)\n",
    )
    monkeypatch.setattr(codex_trust, "_TIMEOUT_S", 0.1)
    with pytest.raises(TimeoutError):
        codex_trust._hooks_list(str(script), tmp_path, {})
