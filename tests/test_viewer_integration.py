"""Integration tests for the tmux-based viewer window creation.

These tests run against a REAL tmux server, isolated from the developer's
own sessions by pointing ``TMUX_TMPDIR`` at a short per-test directory —
``ensure_viewer_window`` shells out to plain ``tmux``, which resolves its
socket through that env var, so both the code under test and the test's
assertions talk to the same throwaway server.

Covers:

1. A long-running viewer command produces a visible named window whose
   content is capturable through the multiplexer seam (``capture_pane``).
2. An immediately-exiting viewer is reported as failed: empty ViewerConfig,
   ``viewer-error.log`` with stderr + exit status, no lingering window.

Skipped when ``tmux`` is not available on PATH.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from bmad_loop.adapters.multiplexer import get_multiplexer
from bmad_loop.adapters.viewer_launch import ensure_viewer_window


def _has_tmux() -> bool:
    try:
        return subprocess.run(["tmux", "-V"], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.fixture(autouse=True)
def skip_no_tmux():
    if not _has_tmux():
        pytest.skip("tmux not available on PATH — skipping integration test")


@pytest.fixture
def isolated_tmux(monkeypatch):
    """Route every tmux invocation (the code under test's and ours) to a
    throwaway server via TMUX_TMPDIR, and kill that server afterwards.

    The socket directory must stay SHORT (unix socket paths cap at ~104
    chars), so it lives directly under the system temp dir rather than
    pytest's deeply nested tmp_path.
    """
    sock_dir = Path(tempfile.mkdtemp(prefix="blvt-"))
    monkeypatch.setenv("TMUX_TMPDIR", str(sock_dir))
    monkeypatch.delenv("TMUX", raising=False)
    get_multiplexer.cache_clear()
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)
    get_multiplexer.cache_clear()


def _fresh_run_id(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:6]}"


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)


def _long_running_viewer(tmp_path: Path) -> Path:
    """A fake `opencode`-style binary: ignores its args, prints a marker,
    and keeps running like a real attached TUI."""
    script = tmp_path / "fake-viewer.sh"
    script.write_text("#!/bin/sh\necho VIEWER-UP\nsleep 300\n", encoding="utf-8")
    script.chmod(0o755)
    return script


# ====================================================================== tests


class TestViewerWindowCreation:
    """A long-running viewer command produces a visible named tmux window."""

    def test_named_window_appears_and_is_capturable(self, isolated_tmux, tmp_path):
        run_id = _fresh_run_id("live")
        session_name = f"bmad-loop-{run_id}"
        viewer_name = f"bmad-loop-{run_id}-viewer"

        result = ensure_viewer_window(
            run_id=run_id,
            server_url="http://127.0.0.1:9999",
            session_id="test-sess-1",
            working_directory=tmp_path,
            attach_binary=str(_long_running_viewer(tmp_path)),
        )

        assert (
            result.viewer_window == f"={session_name}:{viewer_name}"
        ), f"ensure_viewer_window should return the window target; got {result}"
        assert result.viewer_url == "http://127.0.0.1:9999"
        assert result.viewer_session == "test-sess-1"

        # The window shows up in tmux list-windows.
        proc = _tmux("list-windows", "-t", f"={session_name}", "-F", "#{window_name}")
        assert proc.returncode == 0
        assert viewer_name in proc.stdout.splitlines(), proc.stdout

        # The persisted target is capturable THROUGH THE SEAM — the same
        # call the dashboard makes — and shows the viewer's output.
        captured = get_multiplexer().capture_pane(result.viewer_window)
        assert "VIEWER-UP" in captured

        # No failure artifacts for a healthy start.
        assert not (tmp_path / "viewer-error.log").exists()

    def test_existing_viewer_is_reused(self, isolated_tmux, tmp_path):
        run_id = _fresh_run_id("reuse")
        viewer = _long_running_viewer(tmp_path)

        first = ensure_viewer_window(
            run_id=run_id,
            server_url="http://127.0.0.1:9999",
            session_id="test-sess-r",
            working_directory=tmp_path,
            attach_binary=str(viewer),
        )
        second = ensure_viewer_window(
            run_id=run_id,
            server_url="http://127.0.0.1:9999",
            session_id="test-sess-r",
            working_directory=tmp_path,
            attach_binary=str(viewer),
        )
        assert first.viewer_window == second.viewer_window

        # Still exactly one viewer window.
        proc = _tmux("list-windows", "-t", f"=bmad-loop-{run_id}", "-F", "#{window_name}")
        names = proc.stdout.splitlines()
        assert names.count(f"bmad-loop-{run_id}-viewer") == 1, names


class TestImmediateExit:
    """A viewer command that exits immediately is reported as a failure —
    never persisted as a live viewer."""

    def test_immediate_exit_reports_failure(self, isolated_tmux, tmp_path):
        exit_script = tmp_path / "exit7.sh"
        exit_script.write_text("#!/bin/sh\necho 'viewer crash' >&2\nexit 7\n", encoding="utf-8")
        exit_script.chmod(0o755)

        run_id = _fresh_run_id("dead")
        result = ensure_viewer_window(
            run_id=run_id,
            server_url="http://127.0.0.1:9999",
            session_id="test-sess-2",
            working_directory=tmp_path,
            attach_binary=str(exit_script),
        )

        # Creation is reported as failed: no successful-looking metadata.
        assert (
            result.viewer_window is None
        ), f"Expected empty viewer config for exiting command; got {result}"
        assert result.viewer_url == ""
        assert result.viewer_session == ""

        # stderr and exit code are recorded.
        error_log = tmp_path / "viewer-error.log"
        assert error_log.exists(), "Expected viewer-error.log to be written"
        content = error_log.read_text(encoding="utf-8")
        assert "viewer-create-failed" in content
        assert "exit status 7" in content
        assert "viewer crash" in content

        # The dead window was cleaned up, not left as a live-looking target.
        proc = _tmux("list-windows", "-t", f"=bmad-loop-{run_id}", "-F", "#{window_name}")
        assert f"bmad-loop-{run_id}-viewer" not in proc.stdout.splitlines()

    def test_error_log_lands_in_run_dir_when_given(self, isolated_tmux, tmp_path):
        exit_script = tmp_path / "exit3.sh"
        exit_script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        exit_script.chmod(0o755)
        run_dir = tmp_path / "run-dir"
        run_dir.mkdir()

        result = ensure_viewer_window(
            run_id=_fresh_run_id("rundir"),
            server_url="http://127.0.0.1:9999",
            session_id="test-sess-3",
            working_directory=tmp_path,
            attach_binary=str(exit_script),
            run_dir=run_dir,
        )
        assert result.viewer_window is None
        assert (run_dir / "viewer-error.log").exists()
        assert not (tmp_path / "viewer-error.log").exists()
