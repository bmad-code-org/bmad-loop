"""The #731 reproduction as a regression gate, on a real tmux server.

A tmux server hands every pane the environment it started with. A server
cold-started under state root S1 and reused by a TUI under S2 used to run each
parked engine under S1, where the TUI never looks. The TUI now hands the engine
its root in argv (`--state-root`), so the engine resolves S2 whatever the
server holds.

Linux only, zero tokens: the parked engine is `bmad-loop run --dry-run`, which
prints the events channel a real run would use (the control plane, under the
state root) and spawns nothing. The server runs on a private `TMUX_TMPDIR`, and
the autouse `_isolate_mux_registry` fixture removes `TMUX`, which a client would
otherwise follow to the operator's own socket.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import install_bmad_config, real_mux_e2e, write_sprint

from bmad_loop import envvars, runs
from bmad_loop.adapters import multiplexer
from bmad_loop.tui import launch

HAVE_TMUX = sys.platform == "linux" and shutil.which("tmux") is not None


@real_mux_e2e
@pytest.mark.skipif(not HAVE_TMUX, reason="requires Linux with tmux on PATH")
def test_e2e_parked_engine_resolves_the_launchers_root_on_a_stale_server(
    project, tmp_path, monkeypatch
):
    socket_dir = tmp_path / "tmux"
    socket_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    multiplexer.get_multiplexer.cache_clear()
    s1, s2 = tmp_path / "s1", tmp_path / "s2"
    install_bmad_config(project)
    write_sprint(project, {"1-1-first": "ready-for-dev"})
    root = project.project

    # Cold-start the server under S1, as an earlier bmad-loop would have.
    stale = {**os.environ, envvars.STATE_DIR: str(s1)}
    subprocess.run(["tmux", "new-session", "-d", "-s", "keepalive"], env=stale, check=True)
    try:
        monkeypatch.setenv(envvars.STATE_DIR, str(s2))
        win = launch.start_detached(
            root, ["run", "--project", str(root), "--dry-run"], "RID", "run"
        )
        assert win, "the parked window was not minted"

        deadline = time.monotonic() + 30
        screen = ""
        while "BMAD_LOOP_EVENTS_DIR=" not in screen and time.monotonic() < deadline:
            time.sleep(0.2)
            screen = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-t", win],
                capture_output=True,
                text=True,
            ).stdout
        assert "BMAD_LOOP_EVENTS_DIR=" in screen, screen
        (line,) = [ln for ln in screen.splitlines() if "BMAD_LOOP_EVENTS_DIR=" in ln]
        events = Path(line.split("BMAD_LOOP_EVENTS_DIR=", 1)[1].strip())
        assert events.is_relative_to(s2), line
        assert events == runs.events_dir_for(root, "<run-id>")
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
        multiplexer.get_multiplexer.cache_clear()
