import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.hermes import HermesStartupError
from bmad_loop.adapters.hermes import HermesAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.policy import LimitsPolicy, Policy


HAVE_TMUX = sys.platform != "win32" and shutil.which("tmux") is not None


class StubMux:
    def __init__(self, alive: bool = True):
        self.alive = alive
        self.sessions: set[str] = set()
        self.commands: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def has_session(self, name):
        return name in self.sessions

    def new_session(self, name, cwd, cols, lines):
        self.sessions.add(name)

    def set_session_option(self, name, option, value):
        pass

    def new_window(self, session, name, cwd, env, command):
        self.commands.append(command)
        return "@hermes"

    def pipe_pane(self, window_id, log_file):
        pass

    def list_window_ids(self, session):
        return ["@hermes"] if self.alive else []

    def send_text(self, window_id, text):
        self.sent.append((window_id, text))


def make_spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        task_id="todo-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto todo-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(tmp_path / "run"), "BMAD_LOOP_TASK_ID": "todo-1-dev-1"},
        model="test-model",
    )


def make_adapter(tmp_path: Path, mux: StubMux) -> HermesAdapter:
    return HermesAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("hermes"),
        mux=mux,
    )


def test_hermes_command_is_interactive_without_initial_prompt(tmp_path):
    adapter = make_adapter(tmp_path, StubMux())

    assert adapter.build_command(make_spec(tmp_path)) == "hermes --cli --accept-hooks --yolo --model test-model"


def test_hermes_start_injects_prompt_after_live_startup(tmp_path, monkeypatch):
    mux = StubMux(alive=True)
    adapter = make_adapter(tmp_path, mux)
    monkeypatch.setattr("bmad_loop.adapters.hermes.time.sleep", lambda _: None)

    handle = adapter.start_session(make_spec(tmp_path))

    assert mux.commands == ["hermes --cli --accept-hooks --yolo --model test-model"]
    assert mux.sent == [(handle.native_id, "Use the $bmad-dev-auto skill now: todo-1")]


def test_hermes_start_fails_when_pane_dies_during_startup(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, StubMux(alive=False))
    monkeypatch.setattr("bmad_loop.adapters.hermes.time.sleep", lambda _: None)

    with pytest.raises(HermesStartupError, match="exited during Hermes startup"):
        adapter.start_session(make_spec(tmp_path))


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_hermes_tmux_session_injects_prompt_and_relays_stop(tmp_path, monkeypatch):
    fake = tmp_path / "fake-hermes"
    fake.write_text(
        "#!/bin/bash\n"
        "IFS= read -r prompt\n"
        "mkdir -p \"$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID\"\n"
        "printf '{\"workflow\": \"fake-hermes\", \"prompt\": \"%s\"}\\n' \"$prompt\" "
        "> \"$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID/result.json\"\n"
        "printf '{\"session_id\": \"fake-hermes-session\"}\\n' | "
        f"{shlex.quote(sys.executable)} -c "
        "'from bmad_loop.cli import main; raise SystemExit(main([\"relay\", \"Stop\"]))'\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    run_dir = tmp_path / ".bmad-loop" / "runs" / f"hermes-{uuid.uuid4().hex[:8]}"
    adapter = HermesAdapter(
        run_dir=run_dir,
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("hermes"),
        binary=str(fake),
        extra_args=(),
    )
    monkeypatch.setattr(adapter, "STARTUP_GRACE_S", 0.05)
    spec = SessionSpec(
        task_id="hermes-tmux-e2e",
        role="dev",
        prompt="/bmad-dev-auto todo-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(run_dir), "BMAD_LOOP_TASK_ID": "hermes-tmux-e2e"},
        timeout_s=30,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.session_id == "fake-hermes-session"
    assert result.result_json == {
        "workflow": "fake-hermes",
        "prompt": "Use the $bmad-dev-auto skill now: todo-1",
    }
