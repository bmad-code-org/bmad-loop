"""Multiplexer-seam proof.

Drives a full ``GenericAdapter`` start/wait cycle against a stub
``TerminalMultiplexer`` with **no tmux on PATH** and the tmux backend's
subprocess seam booby-trapped, proving the adapter never shells out to tmux
directly — every transport op goes through ``self.mux``. Mirrors MockAdapter's
role for the transport axis.
"""

import json
import os
import subprocess

import pytest

from bmad_loop.adapters import tmux_base
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.generic import GenericAdapter
from bmad_loop.adapters.multiplexer import MultiplexerError, TerminalMultiplexer
from bmad_loop.adapters.profile import get_profile
from bmad_loop.adapters.tmux_backend import TmuxMultiplexer
from bmad_loop.policy import LimitsPolicy, Policy


class StubMux(TerminalMultiplexer):
    """Records the transport ops the adapter performs; never touches a real
    multiplexer. Unused ops raise, so the test also pins exactly which ops the
    adapter relies on."""

    def __init__(self):
        self.calls: list[str] = []
        self._sessions: set[str] = set()
        self._windows: dict[str, list[str]] = {}
        self._next = 0

    # ---- ops the adapter uses (record + minimal real behavior)
    def has_session(self, name):
        self.calls.append("has_session")
        return name in self._sessions

    def new_session(self, name, cwd, cols, lines):
        self.calls.append("new_session")
        self._sessions.add(name)
        self._windows[name] = []

    def set_session_option(self, name, option, value):
        self.calls.append("set_session_option")

    def new_window(self, session, name, cwd, env, command):
        self.calls.append("new_window")
        self._next += 1
        win = f"@stub{self._next}"
        self._windows.setdefault(session, []).append(win)
        return win

    def pipe_pane(self, window_id, log_file):
        self.calls.append("pipe_pane")

    def list_window_ids(self, session):
        self.calls.append("list_window_ids")
        return list(self._windows.get(session, []))

    def send_text(self, window_id, text):
        self.calls.append("send_text")

    def kill_window(self, target):
        self.calls.append("kill_window")
        for wins in self._windows.values():
            if target in wins:
                wins.remove(target)

    def available(self):
        return True

    # ---- ops the adapter must NOT touch
    def kill_session(self, name):
        raise AssertionError("adapter must not call kill_session")

    def list_sessions(self):
        raise AssertionError("adapter must not call list_sessions")

    def session_options(self, option):
        raise AssertionError("adapter must not call session_options")

    def new_parked_window(self, session, name, cwd, argv, return_opt):
        raise AssertionError("adapter must not call new_parked_window")

    def list_windows(self, session, fields):
        raise AssertionError("adapter must not call list_windows")

    def window_alive(self, session, window_id):
        raise AssertionError("adapter must not call window_alive")

    def select_window(self, target):
        raise AssertionError("adapter must not call select_window")

    def set_window_option(self, target, option, value):
        raise AssertionError("adapter must not call set_window_option")

    def unset_window_option(self, target, option):
        raise AssertionError("adapter must not call unset_window_option")

    def show_window_option(self, target, option):
        raise AssertionError("adapter must not call show_window_option")

    def attach_target_argv(self, target):
        raise AssertionError("adapter must not call attach_target_argv")

    def current_pane_id(self):
        raise AssertionError("adapter must not call current_pane_id")

    def current_window_id(self):
        raise AssertionError("adapter must not call current_window_id")

    def current_session(self):
        raise AssertionError("adapter must not call current_session")

    def detach_client(self):
        raise AssertionError("adapter must not call detach_client")

    def switch_client(self, target, last_fallback=False):
        raise AssertionError("adapter must not call switch_client")


@pytest.fixture
def no_tmux(monkeypatch):
    """tmux off PATH, and the backend's subprocess seam booby-trapped: any direct
    shell-out to tmux fails the test loudly."""
    monkeypatch.setenv("PATH", "")

    def boom(*a, **k):
        raise AssertionError("GenericAdapter shelled out to tmux directly")

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)


def _spec(tmp_path):
    task_id = "1-1-dev-1"
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(tmp_path / "run"), "BMAD_LOOP_TASK_ID": task_id},
        timeout_s=10.0,
    )


def test_generic_adapter_drives_only_the_mux(tmp_path, no_tmux):
    stub = StubMux()
    adapter = GenericAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        mux=stub,
    )
    spec = _spec(tmp_path)

    handle = adapter.start_session(spec)
    assert handle.native_id == "@stub1"
    # session bootstrap + window launch + log tee all went through the mux
    assert stub.calls == [
        "has_session",
        "new_session",
        "set_session_option",
        "new_window",
        "pipe_pane",
    ]

    # Seed a fresh Stop event + result.json (ts above the launch floor) so the
    # wait observes a normal completion without any real process.
    ts = handle.launched_ns + 1
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{spec.task_id}-Stop.json").write_text(
        json.dumps({"ts": ts, "event": "Stop", "task_id": spec.task_id, "session_id": "s1"}),
        encoding="utf-8",
    )
    (adapter.tasks_dir / spec.task_id / "result.json").write_text(
        json.dumps({"workflow": "auto-dev"}), encoding="utf-8"
    )

    result = adapter.wait_for_completion(handle, spec)
    assert result.status == "completed"
    assert result.result_json == {"workflow": "auto-dev"}
    assert result.session_id == "s1"

    adapter.kill(handle)
    assert "kill_window" in stub.calls


# --------------------------------------------------------------- seam honesty
#
# Phase 1: no tmux contract method may leak a raw subprocess.TimeoutExpired /
# OSError. The one place a subprocess is spawned (_run) deliberately propagates
# those raw; the guarantee is enforced one level up, in the inherited contract
# methods, so it holds even for a psmux that overrides only _run.


@pytest.fixture(params=[subprocess.TimeoutExpired(["tmux"], 30), FileNotFoundError("tmux")])
def boom_run(request, monkeypatch):
    """tmux 'present' on PATH, but the single subprocess spawn always raises a raw
    transport error (parametrized over a timeout and a missing binary)."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    def boom(*_a, **_k):
        raise request.param

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)


def test_seam_methods_never_leak_raw_subprocess_error(boom_run, tmp_path):
    mux = TmuxMultiplexer()

    # Raisers: liveness / mutating ops re-raise as the seam type. A raw timeout
    # would escape the MultiplexerError contract; a sentinel would mis-read as a
    # real (empty/absent) answer — so these MUST raise, and as MultiplexerError.
    raisers = [
        lambda: mux.list_window_ids("s"),
        lambda: mux.window_alive("s", "@1"),
        lambda: mux.new_session("s", tmp_path),
        lambda: mux.new_window("s", "n", tmp_path, {}, "cmd"),
        lambda: mux.set_session_option("s", "opt", "val"),
        lambda: mux.new_parked_window("s", "n", tmp_path, ["echo", "hi"], ""),
        lambda: mux.send_text("@1", "hi"),
        lambda: mux.has_session("s"),  # already-correct raiser — lock it in
    ]
    for call in raisers:
        with pytest.raises(MultiplexerError) as excinfo:
            call()
        # the seam type, never a raw subprocess / OS error leaking through
        assert not isinstance(excinfo.value, subprocess.SubprocessError)
        assert not isinstance(excinfo.value, OSError)

    # Sentinel returners: a transport failure degrades to the documented value
    # (never a raise, never a mis-typed answer).
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.show_window_option("@1", "opt") == ""
    assert mux.switch_client("s") is False
    assert mux.switch_client("s", last_fallback=True) is False
    assert mux.kill_window("@1") is None
    assert mux.select_window("@1") is None
    assert mux.set_window_option("@1", "opt", "val") is None
    assert mux.unset_window_option("@1", "opt") is None
    assert mux.detach_client() is None
    assert mux.pipe_pane("@1", tmp_path / "log") is None

    # Already-correct swallowers stay swallowing (lock-in).
    assert mux.kill_session("s") is None
    assert mux.list_sessions() == []
    assert mux.session_options("opt") == {}
    assert mux.version() is None
    assert mux.current_pane_id() is None


def test_seam_honesty_holds_for_psmux_style_run_override(monkeypatch):
    """The guarantee lives ABOVE _run, so a backend (like the eventual psmux) that
    overrides only _run and lets a raw TimeoutExpired escape it still gets seam
    honesty from the inherited contract methods."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    class PsmuxStyle(TmuxMultiplexer):
        def _run(self, argv, *, check=True, env=None):
            raise subprocess.TimeoutExpired(["tmux", *argv], 30)

    mux = PsmuxStyle()
    with pytest.raises(MultiplexerError):
        mux.list_window_ids("s")
    with pytest.raises(MultiplexerError):
        mux.window_alive("s", "@1")
    # sentinel methods still degrade rather than leak the raw timeout
    assert mux.list_windows("s", ["window_id"]) == []


# ---------------------------------------------- _run seam: encoding + env (#40)
#
# The spawn primitive carries the two knobs its docstring promises — output
# decoding (class attr _ENCODING) and a per-call env — both defaulting to today's
# POSIX behavior, so a native-Windows leaf overrides zero lines of spawn plumbing.


class _RecordRun:
    """Stand-in for subprocess.run that records the argv and kwargs of the one spawn."""

    def __init__(self):
        self.argv: list = []
        self.kwargs: dict = {}

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_run_posix_default_passes_no_encoding_and_no_env(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer()._run(["list-windows"])

    # byte-identical to today: locale-default decode (encoding=None ≡ bare text=True),
    # inherit the parent env (env=None).
    assert rec.kwargs["text"] is True
    assert rec.kwargs["encoding"] is None
    assert rec.kwargs["env"] is None


def test_run_subclass_encoding_reaches_subprocess(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    class Utf8Backend(TmuxMultiplexer):
        _ENCODING = "utf-8"  # a Windows leaf forces UTF-8 without touching _run

    Utf8Backend()._run(["list-windows"])
    assert rec.kwargs["encoding"] == "utf-8"


def test_run_custom_env_is_forwarded_without_leaking(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1234,0")  # a nesting-guard var to scrub
    before = dict(os.environ)

    # per the _run docstring: copy the parent env and REMOVE the offending var —
    # never rebuild from scratch (Windows children need SystemRoot etc.)
    scrubbed = dict(os.environ)
    del scrubbed["TMUX"]
    TmuxMultiplexer()._run(["new-session"], env=scrubbed)

    assert rec.kwargs["env"] == scrubbed
    assert "TMUX" not in rec.kwargs["env"]
    # the scrubbed env is confined to the child spawn — this process's env is untouched
    assert dict(os.environ) == before


# ------------------------------------------------------- shell-dialect seam
#
# new_window / new_parked_window keep the tmux argv construction and the
# parked-window protocol in the base; only shell-dialect fragments route
# through overridable hooks. Locked two ways: the POSIX output stays
# byte-identical to the pre-seam inline code, and a leaf that overrides only
# the hooks still gets the base's scaffolding without touching a method body.

# the exact sh source the POSIX backend produced before the hooks existed
_PARKED_SH_SOURCE = (
    'echo hi; ec=$?; echo "[bmad-loop exited $ec — press enter]"; read -r; '
    "ret=$(tmux show-options -wqv %3 2>/dev/null); "
    'if [ "$ret" = "detach" ]; then tmux detach-client 2>/dev/null; '
    'elif [ -n "$ret" ]; then '
    'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; fi'
)


def test_new_parked_window_posix_argv_byte_identical(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer().new_parked_window("s", "n", tmp_path, ["echo", "hi"], "%3")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "sh",
        "-c",
        _PARKED_SH_SOURCE,
    ]


def test_new_window_posix_argv_byte_identical(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer().new_window("s", "n", tmp_path, {"A": "1", "B": "2"}, "cmd")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        "-e",
        "A=1",
        "-e",
        "B=2",
        "cmd",
    ]


class _FakeDialect(TmuxMultiplexer):
    """A leaf that overrides ONLY the dialect hooks — no contract method bodies."""

    _EXIT_CAPTURE = "ec := EXITSTATUS"
    _ECHO = "say"
    _PARK = "pause"

    def _join_argv(self, argv):
        return "run " + " ".join(f"<{a}>" for a in argv)

    def _source_prefix(self):
        return "PRELUDE; "

    def _shell_wrap(self, source):
        return ["fakesh", "-enc", source]

    def _parked_trailer(self, return_opt):
        return f"TRAILER({return_opt})"

    def _window_launch(self, env, command):
        return [f"wrapped:{command}"]


def test_dialect_leaf_parked_window_composes_from_hooks(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    _FakeDialect().new_parked_window("s", "n", tmp_path, ["echo", "hi"], "%3")

    # the tmux scaffolding is the base's, unchanged
    assert rec.argv[:12] == [
        "tmux",
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
    ]
    # the shell source is composed prefix + inner + capture + banner + park + trailer
    assert rec.argv[12:] == [
        "fakesh",
        "-enc",
        "PRELUDE; run <echo> <hi>; ec := EXITSTATUS; "
        'say "[bmad-loop exited $ec — press enter]"; '
        "pause; TRAILER(%3)",
    ]


def test_dialect_leaf_new_window_routes_launch_through_hook(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    _FakeDialect().new_window("s", "n", tmp_path, {"A": "1"}, "cmd")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        "wrapped:cmd",
    ]
    assert "-e" not in rec.argv  # env strategy fully delegated to the hook
