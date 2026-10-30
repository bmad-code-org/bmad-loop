"""The #731 reproduction as a regression gate, on a real tmux server.

A tmux server hands every pane the environment it started with. A server
cold-started under state root S1 and reused by a TUI under S2 used to run each
parked engine under S1, where the TUI never looks. The TUI now hands the engine
its root in argv (`--state-root`), so the engine resolves S2 whatever the
server holds.

Linux only, zero tokens: the parked engine is `bmad-loop run --dry-run`, which
prints the events channel a real run would use (the control plane, under the
state root) and spawns nothing. It also gates the park itself: on a host whose
`sh` is dash, a window that does not park is gone before it can be read. The
server is the `private_tmux_server` fixture's, which redirects `TMUX_TMPDIR`,
drops `TMUX` (which a client would otherwise follow to the operator's own socket)
and kills the server by explicit socket on teardown.
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
    project, tmp_path, monkeypatch, private_tmux_server
):
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

        # The window parks after its command exits (`BaseTmuxBackend._PARK`),
        # so the whole screen stays readable once the exit banner is up. A
        # window that did not park is gone by then, and capture-pane says so.
        deadline = time.monotonic() + 30
        captured = subprocess.CompletedProcess([], 1, "", "")
        while "[bmad-loop exited" not in captured.stdout and time.monotonic() < deadline:
            time.sleep(0.2)
            captured = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-t", win], capture_output=True, text=True
            )
        windows = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_id} #{window_name}"],
            capture_output=True,
            text=True,
        ).stdout
        assert "[bmad-loop exited" in captured.stdout, (captured.stderr, windows)
        # The banner prints just before the park's `read`, so a window seen
        # right after it may still be on its way out. The pane must still be
        # alive across a settle interval: only a blocked reader keeps it.
        for _ in range(2):
            time.sleep(0.5)
            pane = subprocess.run(
                ["tmux", "display-message", "-p", "-t", win, "#{pane_dead}"],
                capture_output=True,
                text=True,
            )
            assert (pane.returncode, pane.stdout.strip()) == (0, "0"), (
                "the window did not park after its command",
                pane.stderr,
            )
        screen = captured.stdout
        assert "BMAD_LOOP_EVENTS_DIR=" in screen, screen
        (line,) = [ln for ln in screen.splitlines() if "BMAD_LOOP_EVENTS_DIR=" in ln]
        events = Path(line.split("BMAD_LOOP_EVENTS_DIR=", 1)[1].strip())
        assert events.is_relative_to(s2), line
        assert events == runs.events_dir_for(root, "<run-id>")
    finally:
        multiplexer.get_multiplexer.cache_clear()


@real_mux_e2e
@pytest.mark.skipif(not HAVE_TMUX, reason="requires Linux with tmux on PATH")
def test_e2e_a_chain_claim_moves_the_control_window_to_the_next_run(
    project, tmp_path, monkeypatch, private_tmux_server
):
    """A parent-initiative chain builds its next child as run B in the window the
    TUI minted for run A; the claim renames that window to `run-B` and records it,
    so the lookup answers it for B and no longer for A, whose own record still
    names it. On a real tmux server, so the rename, the tag a rename must keep
    and the by-name re-prove are tmux's, not a fake's.

    The window is a real start_detached window for A (named and tagged as the
    TUI mints them), parked on `bmad-loop --version`. The claim is called from
    this process, not from inside the window: the in-window probes
    (`in_ctl_session`, `current_window_id`) are monkeypatched to answer that
    window, which is what a chained engine running in it would read."""
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "state"))
    multiplexer.get_multiplexer.cache_clear()
    root = project.project
    for run_id in ("A", "B"):
        run_dir = runs.run_dir_for(root, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text("{}", encoding="utf-8")
    try:
        win = launch.start_detached(root, ["--version"], "A", "run")
        assert win, "the control window was not minted"
        (runs.run_dir_for(root, "A") / launch._CTL_WINDOW_FILE).write_text(win, encoding="utf-8")
        assert launch.ctl_window_id(root, "A") == win

        monkeypatch.setattr(launch, "in_ctl_session", lambda: True)
        mux = multiplexer.get_multiplexer()
        monkeypatch.setattr(type(mux), "current_window_id", lambda self: win)
        assert launch.claim_renamed_ctl_window(root, "B") is None

        name = subprocess.run(
            ["tmux", "display-message", "-p", "-t", win, "#{window_name}"],
            capture_output=True,
            text=True,
        )
        assert name.stdout.strip() == "run-B", name.stderr
        assert launch.recorded_ctl_window(root, "B") == win
        assert launch.ctl_window_lookup(root, "B") == (win, 0)  # the tag survived
        assert launch.ctl_window_id(root, "A") is None
    finally:
        multiplexer.get_multiplexer.cache_clear()
