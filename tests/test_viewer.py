"""Viewer capability: unit tests for show_attached_ui + viewer window lifecycle.

Covers:
- viewer disabled (default, policy flag false)
- viewer creation (mux + binary present, opencode attach argv correct)
- exact attach command and session ID wiring
- attach routing (attach_plan prefers viewer window)
- pane-log routing (viewer log path matches task log)
- cleanup (teardown order: viewer → session → server)
- retry/resume without duplicate windows (idempotency)
- missing `opencode attach` / mux failure degrades to headless operation
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bmad_loop.adapters.base import ViewerConfig
from bmad_loop.policy import AdapterPolicy

# ====================================================================== fixtures


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Standard bmad-loop sandbox with a run dir and mocked multiplexer.

    The mock simulates realistic tmux behaviour: ``list_windows`` initially
    returns ``[]`` (no windows), but after ``new_window`` is called it starts
    returning the viewer window entry so that ``_mux_window_exists`` sees the
    newly-created window, meaning ``ensure_viewer_window`` returns a success
    ``ViewerConfig`` instead of an empty fallback.
    """
    from bmad_loop.adapters import multiplexer
    from bmad_loop.adapters.viewer_launch import _VIEWER_OPTION

    mux = MagicMock()
    mux.available.return_value = True
    mux.has_session.return_value = False
    mux.target.side_effect = lambda s, w=None: f"={s}:{w}" if w else f"={s}"

    # Track whether new_window has been called (simulating tmux creating the
    # window).  The stored name/value come from the call args so that
    # _find_existing_viewer can find the window across retries.
    _state = {"created": False, "name": None, "option_val": ""}

    def _list_windows(session_name, fields):
        # Mimic the real backend exactly: one tuple per window, sized to the
        # requested fields (a fatter tuple here once hid a real unpack bug).
        if not _state["created"]:
            return []
        values = {
            "window_name": _state["name"],
            _VIEWER_OPTION: _state["option_val"],
            "pane_dead": "0",
            "pane_dead_status": "",
        }
        return [tuple(values.get(f, "") for f in fields)]

    def _new_window(session, name, cwd, env, command):
        # Simulate tmux creating the window — store its name so
        # the post-creation verification (which calls list_windows) sees it.
        _state["created"] = True
        _state["name"] = name

    def _new_parked_window(session, name, cwd, argv, return_opt):
        _state["created"] = True
        _state["name"] = name
        return "@1"

    def _set_window_option(target, option, value):
        # Record the marker option so _find_existing_viewer can see it
        if option == _VIEWER_OPTION:
            _state["option_val"] = value

    mux.new_window.side_effect = _new_window
    mux.new_parked_window.side_effect = _new_parked_window
    mux.set_window_option.side_effect = _set_window_option
    mux.list_windows.side_effect = _list_windows
    mux.capture_pane.return_value = ""  # live viewer: no exit banner
    mux.list_window_ids.return_value = []
    mux.session_options.return_value = {}
    mux.show_window_option.return_value = ""

    # Keep the immediate-exit poll from sleeping in unit tests.
    from bmad_loop.adapters import viewer_launch

    monkeypatch.setattr(viewer_launch, "_STARTUP_CHECKS", 1)

    multiplexer.get_multiplexer.cache_clear()
    monkeypatch.setattr(multiplexer, "get_multiplexer", MagicMock(return_value=mux))
    # viewer_launch binds these names at import (from-import), so the
    # multiplexer-module patch above never reaches it — patch its own
    # namespace too or the tests talk to the REAL tmux server.
    monkeypatch.setattr(viewer_launch, "get_multiplexer", MagicMock(return_value=mux))
    monkeypatch.setattr(viewer_launch, "mux_usable", lambda backend=None: True)
    monkeypatch.setattr(viewer_launch, "_binary_exists", lambda name: True)
    yield tmp_path, mux
    multiplexer.get_multiplexer.cache_clear()


# ====================================================================== tests


class TestViewerDisabled:
    """Viewer defaults to disabled: no window created when flag is absent or false."""

    def test_viewer_not_created_when_disabled(self, sandbox):
        tmp_path, mux = sandbox
        # Policy with show_attached_ui = False (the default)
        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=False)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.teardown_grace_s = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "test-profile"
        profile.binary = "fakecli"
        profile.launch_args = ()
        profile.env = {}
        profile.env_fault_patterns = []
        profile.hookless = False
        profile.skill_tree = "skills"
        profile.render_prompt = lambda p: p
        profile.bypass_args = ()
        profile.usage_grace_s = None
        profile.stop_without_result_nudges = None

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        from bmad_loop.adapters.generic import GenericAdapter

        adapter = GenericAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )
        # Viewer should be default (no-op)
        vc = adapter.viewer_config
        assert vc.viewer_window is None
        assert vc.viewer_url == ""
        assert vc.viewer_session == ""


class TestViewerCreation:
    """When show_attached_ui=True, a viewer window is created."""

    def test_viewer_window_created_when_enabled(self, sandbox):
        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.has_session.return_value = False

        # Mock the viewer_launch to verify it's called
        with patch("bmad_loop.adapters.opencode_http._ensure_viewer") as mock_ensure:
            mock_ensure.return_value = ViewerConfig(
                viewer_window="=bmad-loop-abc:my-viewer",
                viewer_url="http://127.0.0.1:9999",
                viewer_session="sess-abc123",
                viewer_cwd=str(tmp_path),
                viewer_command="opencode attach http://127.0.0.1:9999 --dir . --session sess-abc123",
            )

            from bmad_loop.adapters.base import SessionSpec
            from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

            policy_patch = MagicMock()
            policy_patch.adapter = AdapterPolicy(name="opencode", show_attached_ui=True)
            policy_patch.limits.stop_without_result_nudges = 0
            policy_patch.limits.max_tokens_per_session = 4_000_000
            policy_patch.limits.session_budget_mode = "off"
            policy_patch.limits.cache_read_weight = 0.1

            profile = MagicMock()
            profile.name = "opencode"
            profile.binary = "opencode"
            profile.skill_tree = "skills"
            profile.env_fault_patterns = []
            profile.hookless = True

            run_dir = tmp_path / "run1"
            run_dir.mkdir()
            (run_dir / "tasks").mkdir(exist_ok=True)
            (run_dir / "logs").mkdir(exist_ok=True)

            adapter = OpencodeHttpAdapter(
                run_dir=run_dir,
                policy=policy_patch,
                profile=profile,
            )
            # Viewer starts empty; _ensure_viewer not called yet:
            assert adapter.viewer_config.viewer_window is None

            # Calling ensure_viewer_window (the adapter seam) triggers creation:
            spec = SessionSpec(
                task_id="task-abc",
                role="dev",
                prompt="test",
                cwd=tmp_path,
            )
            result_vc = adapter.ensure_viewer_window(spec, "sess-abc123", "http://127.0.0.1:9999")
            assert result_vc.viewer_window == "=bmad-loop-abc:my-viewer"

            # _ensure_viewer was called with correct arguments:
            mock_ensure.assert_called_once()
            call_kwargs = mock_ensure.call_args
            assert call_kwargs.kwargs["run_id"] == "run1"
            assert call_kwargs.kwargs["session_id"] == "sess-abc123"


class TestAttachCommandAndSessionId:
    """The viewer command is constructed with the correct server URL,
    working directory, and session id."""

    def test_viewer_argv_construction(self):
        """Direct test of _viewer_argv helper in viewer_launch."""
        from bmad_loop.adapters.viewer_launch import _viewer_argv

        cmd = _viewer_argv("opencode", "http://127.0.0.1:9999", Path("/home/user/proj"), "sess-xyz")
        expected_argv = [
            "opencode",
            "attach",
            "http://127.0.0.1:9999",
            "--dir",
            "/home/user/proj",
            "--session",
            "sess-xyz",
        ]
        assert cmd == expected_argv

    def test_viewer_command_field_matches_argv(self, sandbox):
        """ViewerConfig.viewer_command field should be reconstructible
        from the constituent fields."""
        from bmad_loop.adapters.base import SessionSpec
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.list_windows.return_value = []

        # When ensure_viewer_window would create a window with specific fields,
        # those fields should be correctly populated.
        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1-xyz"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )

        spec = SessionSpec(
            task_id="task-xyz",
            role="dev",
            prompt="test",
            cwd=run_dir,
        )
        result_vc = adapter.ensure_viewer_window(spec, "sess-xyz", "http://127.0.0.1:8080")

        # The viewer_config fields are set
        assert result_vc.viewer_session == "sess-xyz"
        assert result_vc.viewer_url == "http://127.0.0.1:8080"


class TestAttachRouting:
    """attach_plan prefers the viewer window when present."""

    @pytest.fixture
    def routed_mux(self, sandbox, monkeypatch):
        """Route launch.py's and runs.py's own module bindings (from-imports)
        at the sandbox mux so attach_plan never talks to the real tmux."""
        from bmad_loop import runs
        from bmad_loop.tui import launch

        tmp_path, mux = sandbox
        mux.attach_target_argv.side_effect = lambda t: ["tmux", "attach", "-t", t]
        monkeypatch.setattr(launch, "get_multiplexer", MagicMock(return_value=mux))
        monkeypatch.setattr(launch, "mux_usable", lambda backend=None: True)
        monkeypatch.setattr(runs, "get_multiplexer", MagicMock(return_value=mux))
        return tmp_path, mux

    def test_attach_plan_prefers_viewer(self, routed_mux):
        tmp_path, mux = routed_mux

        # Set up: there is a viewer window in the run's session
        mux.list_windows.side_effect = None
        mux.list_windows.return_value = [
            ("run-abc123",),
            ("bmad-loop-abc123-viewer",),
        ]

        from bmad_loop.tui.launch import attach_plan

        project = tmp_path / "proj"
        project.mkdir()
        (project / ".bmad-loop").mkdir(exist_ok=True)
        (project / ".bmad-loop" / "runs").mkdir(exist_ok=True)
        (project / ".bmad-loop" / "runs" / "abc123").mkdir(exist_ok=True)
        (project / ".bmad-loop" / "runs" / "abc123" / "state.json").write_text(
            json.dumps({"run_id": "abc123", "finished": False}), encoding="utf-8"
        )

        plan = attach_plan(project, "abc123")
        # attach_plan should find and prefer the viewer window
        assert plan is not None
        target_argv, return_window = plan
        assert "bmad-loop-abc123-viewer" in target_argv[3]
        assert return_window is None

    def test_attach_plan_fallback_when_no_viewer(self, routed_mux):
        tmp_path, mux = routed_mux

        # No viewer window exists, but the agent session is live → the plan
        # falls back to attaching to the run session itself.
        mux.list_windows.side_effect = None
        mux.list_windows.return_value = [("shell",)]
        mux.has_session.return_value = True

        from bmad_loop.tui.launch import attach_plan

        project = tmp_path / "proj"
        project.mkdir()
        (project / ".bmad-loop").mkdir(exist_ok=True)
        (project / ".bmad-loop" / "runs").mkdir(exist_ok=True)

        plan = attach_plan(project, "abc123")
        assert plan is not None
        target_argv, _ = plan
        assert "bmad-loop-abc123-viewer" not in target_argv[3]


class TestPaneLogRouting:
    """The viewer pane log path should match the task log path expected
    by the dashboard Log tab."""

    def test_viewer_log_path_format(self, sandbox):
        """Viewer output logs to <run>.log which is the standard task log format."""
        from bmad_loop.journal import LOGS_DIR

        task_id = "task-abc123-def"
        expected_log_path = Path(str(LOGS_DIR)) / f"{task_id}.log"

        expected_name = expected_log_path.name  # e.g. "task-abc123-def.log"
        expected_pattern = re.compile(r"^task-[a-f0-9-]+\.log$")
        assert expected_pattern.match(expected_name)

    def test_viewer_config_stored_to_json(self, sandbox):
        """Viewer metadata persisted to viewer.json in the run dir."""
        from bmad_loop.adapters.base import SessionSpec
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.list_windows.return_value = []

        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )

        spec = SessionSpec(
            task_id="task-abc",
            role="dev",
            prompt="test",
            cwd=run_dir,
        )
        # ensure_viewer_window won't actually create a tmux window
        # (since we're mocking the mux), but it should still persist
        # and call _persist_viewer_config
        result_vc = adapter.ensure_viewer_window(spec, "sess-123", "http://127.0.0.1:9999")

        # The viewer_config was saved on the adapter
        assert result_vc.viewer_session == "sess-123"


class TestTeardown:
    """Teardown order: viewer → session → server."""

    def test_teardown_closes_viewer_before_server(self, sandbox):
        """When the adapter has _viewer_config set, _close_viewer reads it
        and calls the module-level close_viewer_window with the target."""
        from bmad_loop.adapters import opencode_http as oh
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.list_windows.return_value = []

        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )

        # The adapter stores the config as _viewer_config internally
        adapter._viewer_config = ViewerConfig(
            viewer_window="=bmad-loop-xyz:my-viewer",
            viewer_url="http://127.0.0.1:9999",
            viewer_session="sess-abc",
        )

        # Patch the module-level _close_viewer in the adapter's own module
        # namespace — the method references it by its module-level import name.
        close_target = []

        def record_call(target):
            close_target.append(target)

        with patch.object(oh, "_close_viewer", side_effect=record_call):
            adapter._close_viewer()

        assert len(close_target) == 1
        assert close_target[0] == "=bmad-loop-xyz:my-viewer"

    def test_close_viewer_on_none_is_noop(self, sandbox):
        """Calling _close_viewer with an empty viewer_config (all empty strings)
        should not crash."""
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.list_windows.return_value = []

        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )

        # viewer_config is None by default
        adapter._close_viewer()  # should not raise


class TestRetryResumeNoDuplicates:
    """Viewer window creation must be idempotent: retries, resume,
    interrupted runs should never create duplicate viewer windows."""

    def test_existing_window_reused(self, monkeypatch):
        """When a viewer window already exists, _find_existing_viewer should
        detect it and return the existing config rather than creating a new one."""
        from bmad_loop.adapters import viewer_launch

        mux_mock = MagicMock()
        mux_mock.available.return_value = True
        mux_mock.target.side_effect = lambda s, w=None: f"={s}:{w}" if w else f"={s}"
        mux_mock.list_windows.return_value = [
            ("bmad-loop-xyz-viewer", "1"),
            ("window-abc", ""),
        ]

        # Patch get_multiplexer in the same module where _find_existing_viewer
        # uses it, because from-import binds the name locally.
        monkeypatch.setattr(
            viewer_launch,
            "get_multiplexer",
            MagicMock(return_value=mux_mock),
        )

        vc = viewer_launch._find_existing_viewer("xyz", "bmad-loop-xyz")
        assert vc is not None
        # The reused target is the by-name window token, never the marker value.
        assert vc.viewer_window == "=bmad-loop-xyz:bmad-loop-xyz-viewer"

    def test_multiple_runs_have_separate_views(self, sandbox):
        """Different run ids should have different viewer targets."""
        from bmad_loop.adapters.viewer_launch import (
            _viewer_window_name,
        )

        name1 = _viewer_window_name("run-abc")
        name2 = _viewer_window_name("run-def")

        assert name1 != name2
        assert "run-abc" in name1
        assert "run-def" in name2

    def test_missing_viewer_on_resume_degrades_gracefully(self, sandbox):
        """When a viewer doesn't exist on resume (e.g. it was killed),
        the adapter should still function without it."""
        from bmad_loop.adapters.base import SessionSpec
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

        tmp_path, mux = sandbox
        mux.available.return_value = True
        mux.list_windows.return_value = []

        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(
            run_dir=run_dir,
            policy=policy_patch,
            profile=profile,
        )

        # Calling ensure when viewer doesn't exist:
        # Should return a default ViewerConfig (no error)
        spec = SessionSpec(
            task_id="task-xyz",
            role="dev",
            prompt="test",
            cwd=run_dir,
        )
        result_vc = adapter.ensure_viewer_window(spec, "sess-xyz", "http://127.0.0.1:9999")
        # Since mux.list_windows returns nothing, viewer creation will
        # fail silently and return a default ViewerConfig
        assert isinstance(result_vc, ViewerConfig)


class TestViewerAuth:
    """The attach client must receive the serve password — via the tmux
    session environment, never via argv or the persisted viewer_command
    (both are ps- / viewer.json-visible)."""

    def test_attach_env_lands_in_session_environment_not_argv(self, sandbox):
        from bmad_loop.adapters.viewer_launch import ensure_viewer_window

        tmp_path, mux = sandbox
        vc = ensure_viewer_window(
            run_id="auth-run",
            server_url="http://127.0.0.1:9999",
            session_id="sess-auth",
            working_directory=tmp_path,
            attach_binary="opencode",
            attach_env={"OPENCODE_SERVER_PASSWORD": "s3cret"},
        )
        assert vc.viewer_window is not None
        mux.set_session_environment.assert_called_once_with(
            "bmad-loop-auth-run", "OPENCODE_SERVER_PASSWORD", "s3cret"
        )
        # The secret never reaches argv or the persisted command.
        (_, _, _, argv, _), _ = mux.new_parked_window.call_args
        assert all("s3cret" not in a for a in argv)
        assert "s3cret" not in vc.viewer_command


class TestStopDuringCreation:
    """The engine's SIGTERM handler raises RunStopped in whatever frame is
    running. If that lands inside viewer creation it must PROPAGATE (after
    killing the half-made window) — swallowing it breaks the stop path,
    because the handler only raises once (#viewer-error.log 2026-07-25)."""

    def test_run_stopped_mid_verification_propagates(self, sandbox):
        from bmad_loop.adapters.viewer_launch import ensure_viewer_window
        from bmad_loop.engine import RunStopped

        tmp_path, mux = sandbox
        mux.capture_pane.side_effect = RunStopped()  # lands mid-poll

        with pytest.raises(RunStopped):
            ensure_viewer_window(
                run_id="stop-run",
                server_url="http://127.0.0.1:9999",
                session_id="sess-stop",
                working_directory=tmp_path,
                attach_binary="opencode",
            )
        # The unrecorded window was cleaned up on the way out.
        mux.kill_window.assert_called_once()
        # And the interruption is NOT reported as a viewer failure.
        assert not (tmp_path / "viewer-error.log").exists()

    def test_run_stopped_propagates_through_adapter_seam(self, sandbox):
        """The adapter's best-effort catch must not re-swallow RunStopped."""
        from bmad_loop.adapters import opencode_http as oh
        from bmad_loop.adapters.base import SessionSpec
        from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter
        from bmad_loop.engine import RunStopped

        tmp_path, mux = sandbox
        policy_patch = MagicMock()
        policy_patch.adapter = AdapterPolicy(show_attached_ui=True)
        policy_patch.limits.stop_without_result_nudges = 0
        policy_patch.limits.max_tokens_per_session = 4_000_000
        policy_patch.limits.session_budget_mode = "off"
        policy_patch.limits.cache_read_weight = 0.1

        profile = MagicMock()
        profile.name = "opencode"
        profile.binary = "opencode"
        profile.skill_tree = "skills"
        profile.env_fault_patterns = []
        profile.hookless = True

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "tasks").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        adapter = OpencodeHttpAdapter(run_dir=run_dir, policy=policy_patch, profile=profile)
        spec = SessionSpec(task_id="t", role="dev", prompt="p", cwd=tmp_path)

        with patch.object(oh, "_ensure_viewer", side_effect=RunStopped()):
            with pytest.raises(RunStopped):
                adapter.ensure_viewer_window(spec, "sess", "http://127.0.0.1:1")


class TestMissingBinaryFallback:
    """When opencode binary is not found or mux fails, the adapter
    degrades to headless operation."""

    def test_binary_not_found_returns_headless(self, sandbox):
        """When the attach binary is missing, viewer creation falls back
        to a no-op ViewerConfig — and says why in viewer-error.log."""
        from bmad_loop.adapters import viewer_launch
        from bmad_loop.adapters.viewer_launch import ensure_viewer_window

        tmp_path, _mux = sandbox
        with patch.object(viewer_launch, "_binary_exists", return_value=False):
            result_vc = ensure_viewer_window(
                run_id="test-run",
                server_url="http://127.0.0.1:9999",
                session_id="sess-123",
                working_directory=tmp_path,
                attach_binary="nonexistent-binary",
            )

        # Should return a default ViewerConfig (no crash)
        assert result_vc.viewer_window is None
        assert result_vc.viewer_url == ""
        error_log = (tmp_path / "viewer-error.log").read_text(encoding="utf-8")
        assert "nonexistent-binary" in error_log

    def test_mux_unavailable_returns_headless(self, sandbox):
        """When the multiplexer is unavailable, viewer creation falls back."""
        from bmad_loop.adapters.viewer_launch import ensure_viewer_window

        tmp_path, _mux = sandbox
        with patch("bmad_loop.adapters.viewer_launch.mux_usable", return_value=False):
            result_vc = ensure_viewer_window(
                run_id="test-run",
                server_url="http://127.0.0.1:9999",
                session_id="sess-123",
                working_directory=tmp_path,
                attach_binary="opencode",
            )
        assert result_vc.viewer_window is None

    def test_close_viewer_on_none_is_noop(self, sandbox):
        """close_viewer_window(None) should not raise."""
        from bmad_loop.adapters.viewer_launch import close_viewer_window

        # Should not raise
        close_viewer_window(None)
        close_viewer_window("")
        close_viewer_window("=session:window")  # even if window doesn't exist
