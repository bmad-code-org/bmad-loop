"""Server-start config output stays off the agent's pane (DW-635), on a real server.

A user config `run`/`run-shell` that fails or prints at server start reports in
view-mode on the pane active when the job ends. When bmad-loop starts the server
that used to be the first task window (`new-window` made it current), and every
wake nudge and TUI answer for the session was then refused as "in a pane mode".
Task windows now open detached, so the session's `shell` window stays current
and catches the output.

Linux only, zero tokens: the task window runs ``sleep``. The server is the
``private_tmux_server`` fixture's, which redirects ``TMUX_TMPDIR``, drops ``TMUX``
(which a client would otherwise follow to the operator's own socket) and kills the
server by explicit socket on teardown. ``HOME`` and ``XDG_CONFIG_HOME`` point at
the test's own dir, so the operator's tmux config is never read.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest
from conftest import REAL_MUX_HANG_CEILING_S, real_mux_e2e

from bmad_loop.adapters.multiplexer import get_multiplexer

HAVE_TMUX = sys.platform == "linux" and shutil.which("tmux") is not None


@real_mux_e2e
@pytest.mark.skipif(not HAVE_TMUX, reason="requires Linux with tmux on PATH")
@pytest.mark.usefixtures("force_tmux_backend")
def test_e2e_server_start_config_output_lands_on_the_shell_window(
    tmp_path, monkeypatch, private_tmux_server
):
    # Ablation: drop `-d` from `BaseTmuxBackend.new_window` and the task window
    # is current when the job ends, so it is the one left in view-mode.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".tmux.conf").write_text("run -b 'sleep 0.3; echo boom; exit 127'\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))

    mux = get_multiplexer()
    session = "bmad-loop-dw635"
    mux.new_session(session, tmp_path, 120, 40)  # starts the server: the job begins
    mux.new_window(session, "task", tmp_path, {}, "sleep 30")

    probe = [
        "tmux",
        "-S",
        str(private_tmux_server),
        "list-panes",
        "-s",
        "-t",
        f"={session}",
        "-F",
        "#{window_name} #{window_active} #{pane_in_mode}",
    ]
    rows: list[list[str]] = []
    deadline = time.monotonic() + REAL_MUX_HANG_CEILING_S
    while time.monotonic() < deadline:
        out = subprocess.run(
            probe, capture_output=True, text=True, timeout=REAL_MUX_HANG_CEILING_S, check=True
        )
        rows = [line.split() for line in out.stdout.splitlines()]
        if any(row[2] == "1" for row in rows):
            break
        time.sleep(0.05)
    by_name = {row[0]: row[1:] for row in rows}
    assert set(by_name) == {"shell", "task"}, rows
    assert by_name["shell"] == ["1", "1"], rows  # current, and holding the output
    assert by_name["task"] == ["0", "0"], rows  # the agent's pane still takes input
