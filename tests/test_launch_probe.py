"""`probe.launch_screen_probe`: the zero-token launch-and-capture probe (DW-596).

Unit layer: a fake multiplexer scripts the screens and the window's liveness, so
every verdict is reached without a terminal. E2E layer: a real private tmux
server runs a scripted fake CLI that paints a screen and sleeps — zero tokens,
no real coding CLI is ever started.
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
import sys
from pathlib import Path

import pytest
from conftest import real_mux_e2e

from bmad_loop import probe
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.panematch import MatchTimeout
from bmad_loop.adapters.profile import CLIProfile, HookSpec, get_profile

FOOTER = "Enter to confirm · Esc to cancel"
TRUST_PROMPT_LOG = Path(__file__).parent / "fixtures" / "claude-trust-prompt-parked.pipe-pane.log"


def _profile(**overrides) -> CLIProfile:
    fields = {
        "name": "fake",
        # Any file `shutil.which` resolves; the fake mux never runs it.
        "binary": sys.executable,
        "hooks": HookSpec("claude-settings-json", ".claude/settings.json", {}),
        "launch_args": ("-i",),
        "bypass_args": ("--yolo", "--allow-all"),
        "env": {"PROFILE_VAR": "1"},
        "parked_prompt_patterns": ("Enter to confirm · Esc t[o] cancel",),
    }
    fields.update(overrides)
    return CLIProfile(**fields)


class _Mux:
    """Scripted backend. ``screens`` are returned by successive captures (the
    last repeats; a ``MultiplexerError`` instance is raised instead), ``alive``
    likewise answers successive liveness reads."""

    def __init__(self, screens=("",), alive=(True,), available=True, log_text=""):
        self.screens = list(screens)
        self.alive = list(alive)
        self.is_available = available
        self.log_text = log_text
        self.new_window_fault: MultiplexerError | None = None
        self.sessions: list[str] = []
        self.killed: list[str] = []
        self.window: dict | None = None
        self.captures = 0
        self.typed: list[tuple] = []

    def available(self):
        return self.is_available

    def new_session(self, name, cwd, cols=None, lines=None):
        self.sessions.append(name)

    def new_window(self, session, name, cwd, env, command):
        if self.new_window_fault is not None:
            raise self.new_window_fault
        self.window = {"session": session, "cwd": cwd, "env": env, "command": command}
        return "@1"

    def pipe_pane(self, window_id, log_file):
        Path(log_file).write_text(self.log_text, encoding="utf-8")

    @staticmethod
    def _next(script):
        value = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def capture_pane(self, window_id):
        self.captures += 1
        return self._next(self.screens)

    def window_alive(self, session, window_id):
        return self._next(self.alive)

    def kill_session(self, name):
        self.killed.append(name)

    # Anything that types into the pane is recorded; the probe must call none.
    def send_text(self, *args):
        self.typed.append(("send_text", *args))

    def send_keys(self, *args):
        self.typed.append(("send_keys", *args))


@pytest.fixture
def fake_mux(monkeypatch):
    def install(**kwargs) -> _Mux:
        mux = _Mux(**kwargs)
        monkeypatch.setattr(probe, "get_multiplexer", lambda: mux)
        return mux

    return install


def _run(tmp_path, profile=None, **kwargs):
    profile = profile or _profile()
    kwargs.setdefault("timeout_s", 0.0)
    kwargs.setdefault("poll_s", 0.0)
    return probe.launch_screen_probe(profile, binary=profile.binary, project=tmp_path, **kwargs)


def _assert_torn_down(mux: _Mux):
    assert mux.killed == mux.sessions and len(mux.sessions) == 1
    assert mux.sessions[0].startswith("bmad-loop-launchprobe-")
    assert not Path(mux.window["env"]["BMAD_LOOP_RUN_DIR"]).exists()
    assert mux.typed == []


# ------------------------------------------------------------------ verdicts


def test_a_parked_prompt_on_the_screen_is_parked(tmp_path, fake_mux):
    mux = fake_mux(screens=["Booting…", f"  ❯ Yes\n  {FOOTER}\n"])

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_PARKED
    assert result.line == f"  {FOOTER}"
    assert "Esc t[o] cancel" in result.evidence and FOOTER in result.evidence
    assert mux.captures == 2  # the first, blank screen did not end the watch
    _assert_torn_down(mux)


def test_a_dialog_wrapped_across_rows_is_still_parked(tmp_path, fake_mux):
    """The adapter's wrap-tolerant matcher, not a per-row search."""
    mux = fake_mux(screens=["Enter to confirm · Esc\nto cancel\n"])

    assert _run(tmp_path).verdict == probe.LAUNCH_PARKED
    _assert_torn_down(mux)


def test_no_match_until_the_deadline_is_clean(tmp_path, fake_mux):
    mux = fake_mux(screens=["Welcome to fake CLI\n> "])

    result = _run(tmp_path, timeout_s=0.05, poll_s=0.01)

    assert result.verdict == probe.LAUNCH_CLEAN
    assert mux.captures > 1  # it kept looking until the deadline
    _assert_torn_down(mux)


def test_a_window_that_exits_is_died_with_the_scrubbed_log_tail(tmp_path, fake_mux):
    mux = fake_mux(
        screens=[MultiplexerError("can't find window: @1")],
        alive=[False],
        log_text="error: unknown option '--yolo'\n",
    )

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_DIED
    assert result.log_tail == "error: unknown option '--yolo'"
    _assert_torn_down(mux)


def test_a_window_dead_after_a_clean_look_is_died(tmp_path, fake_mux):
    mux = fake_mux(screens=["bye"], alive=[False])

    assert _run(tmp_path, timeout_s=60.0).verdict == probe.LAUNCH_DIED
    _assert_torn_down(mux)


def test_a_dialog_painted_by_a_window_that_then_exited_is_parked(tmp_path, fake_mux):
    """The screen is matched before liveness is read (a `remain-on-exit` pane
    stays capturable). ABLATION: read `window_alive` before matching and this
    reads `died`."""
    mux = fake_mux(screens=[FOOTER], alive=[False])

    assert _run(tmp_path, timeout_s=60.0).verdict == probe.LAUNCH_PARKED
    _assert_torn_down(mux)


def test_a_capture_fault_on_a_live_window_is_capture_failed(tmp_path, fake_mux):
    """Never folded into `clean`: the look at the pane failed."""
    mux = fake_mux(screens=[MultiplexerError("server hung")], alive=[True])

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_CAPTURE_FAILED
    assert "server hung" in result.detail
    _assert_torn_down(mux)


def test_unknowable_liveness_is_capture_failed(tmp_path, fake_mux):
    mux = fake_mux(screens=["Welcome"], alive=[MultiplexerError("list-panes timed out")])

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_CAPTURE_FAILED
    assert "list-panes timed out" in result.detail
    _assert_torn_down(mux)


def test_a_capture_fault_with_unknowable_liveness_is_capture_failed(tmp_path, fake_mux):
    mux = fake_mux(screens=[MultiplexerError("gone?")], alive=[MultiplexerError("hung")])

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_CAPTURE_FAILED
    assert "gone?" in result.detail and "hung" in result.detail
    _assert_torn_down(mux)


def test_a_match_timeout_is_capture_failed(tmp_path, fake_mux, monkeypatch):
    seen = {}

    def timed_out(rows, patterns, *, timeout, skip=frozenset()):
        seen["timeout"] = timeout
        return MatchTimeout("p", "TimeoutError: regex timed out")

    monkeypatch.setattr(probe, "match_rows", timed_out)
    mux = fake_mux(screens=["anything"])

    result = _run(tmp_path, timeout_s=60.0)

    assert result.verdict == probe.LAUNCH_CAPTURE_FAILED
    assert "regex timed out" in result.detail
    assert seen["timeout"] == probe.PARKED_PROMPT_MATCH_TIMEOUT_S
    _assert_torn_down(mux)


def test_empty_patterns_are_skipped_without_a_launch(tmp_path, monkeypatch):
    def no_mux():
        raise AssertionError("a skipped probe must not reach the multiplexer")

    monkeypatch.setattr(probe, "get_multiplexer", no_mux)

    result = _run(tmp_path, _profile(parked_prompt_patterns=()))

    assert result.verdict == probe.LAUNCH_SKIPPED


def test_an_unavailable_backend_is_unavailable(tmp_path, fake_mux):
    mux = fake_mux(available=False)

    assert _run(tmp_path).verdict == probe.LAUNCH_UNAVAILABLE
    assert mux.sessions == []


def test_a_missing_binary_is_unavailable(tmp_path, fake_mux):
    mux = fake_mux()

    result = _run(tmp_path, _profile(binary="no-such-cli-dw596"))

    assert result.verdict == probe.LAUNCH_UNAVAILABLE
    assert "no-such-cli-dw596" in result.detail
    assert mux.sessions == []


def test_a_window_that_cannot_open_is_launch_failed(tmp_path, fake_mux):
    mux = fake_mux()
    mux.new_window_fault = MultiplexerError("create window failed: no space for new pane")

    result = _run(tmp_path)

    assert result.verdict == probe.LAUNCH_FAILED
    assert "no space for new pane" in result.detail
    assert mux.killed == mux.sessions and len(mux.sessions) == 1


def test_an_unexpected_fault_still_tears_down(tmp_path, fake_mux):
    mux = fake_mux(screens=[RuntimeError("boom")])

    with pytest.raises(RuntimeError, match="boom"):
        _run(tmp_path)
    _assert_torn_down(mux)


# --------------------------------------------------------------- the launch


def test_the_argv_carries_launch_and_bypass_args_and_no_prompt(tmp_path, fake_mux):
    """The launched argv is a real session's minus the prompt, with the launch
    args LAST: gemini/copilot/agy launch with `-i <prompt>`, so a launch arg
    followed by anything would take that thing as a prompt. ABLATION: put the
    prompt back (or `launch_args` before `bypass_args`) and this fails."""
    mux = fake_mux(screens=[FOOTER])

    _run(tmp_path, model="m-1")

    argv = shlex.split(mux.window["command"])
    assert argv == [sys.executable, "--yolo", "--allow-all", "--model", "m-1", "-i"]
    assert probe.PROBE_PROMPT not in argv


def test_the_window_runs_in_the_project_with_a_throwaway_relay_env(tmp_path, fake_mux):
    """The relay's run dir, task id AND events dir all point into the temp dir,
    so a startup hook cannot write into a live run's channel even when the
    caller's environment names one."""
    mux = fake_mux(screens=[FOOTER])

    _run(tmp_path)

    env = mux.window["env"]
    run_dir = Path(env["BMAD_LOOP_RUN_DIR"])
    assert mux.window["cwd"] == tmp_path
    assert run_dir.name in mux.sessions[0]
    assert env["BMAD_LOOP_TASK_ID"] == probe.LAUNCH_PROBE_TASK_ID
    assert Path(env["BMAD_LOOP_EVENTS_DIR"]) == run_dir / "events"
    assert env["PROFILE_VAR"] == "1"


# ----------------------------------------------------------- real tmux (E2E)

HAVE_TMUX = sys.platform == "linux" and shutil.which("tmux") is not None


@real_mux_e2e
@pytest.mark.skipif(not HAVE_TMUX, reason="requires Linux with tmux on PATH")
@pytest.mark.parametrize(
    ("painted", "verdict"),
    [("trust-prompt", probe.LAUNCH_PARKED), ("banner", probe.LAUNCH_CLEAN)],
)
def test_e2e_launch_screen_on_a_real_tmux_server(
    tmp_path, force_tmux_backend, private_tmux_server, painted, verdict
):
    """The packaged claude profile with its binary swapped for a script that
    paints a screen and sleeps: DW-339's captured trust prompt (raw pipe-pane
    bytes, rendered by tmux itself) reads parked; a plain banner reads clean.
    Zero tokens: no real CLI runs."""
    screen = TRUST_PROMPT_LOG if painted == "trust-prompt" else tmp_path / "banner.txt"
    if painted == "banner":
        screen.write_text("Fake CLI v0.0.0\n> ready\n", encoding="utf-8")
    script = tmp_path / "fake-cli"
    script.write_text(f"#!/bin/sh\ncat {shlex.quote(str(screen))}\nexec sleep 60\n")
    script.chmod(0o755)
    profile = dataclasses.replace(get_profile("claude"), binary=str(script))
    project = tmp_path / "proj"
    project.mkdir()

    result = probe.launch_screen_probe(
        profile, binary=str(script), project=project, timeout_s=2.0, poll_s=0.2
    )

    assert result.verdict == verdict, result
    if verdict == probe.LAUNCH_PARKED:
        assert result.line is not None and FOOTER in result.line
