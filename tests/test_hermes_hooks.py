import io
import json

from bmad_loop import cli
from bmad_loop.hermes_hooks import (
    HERMES_RELAY_COMMAND,
    hermes_config_path,
    merge_hermes_stop_hook,
    relay_event,
    remove_hermes_stop_hook,
)


def test_hermes_config_path_honors_hermes_home(tmp_path):
    assert hermes_config_path({"HERMES_HOME": str(tmp_path)}) == tmp_path / "config.yaml"


def test_merge_hermes_stop_hook_preserves_unrelated_entries_and_is_idempotent():
    original = {"hooks": {"post_llm_call": [{"command": "other-hook", "timeout": 5}]}}

    merged, changed = merge_hermes_stop_hook(original)

    assert changed is True
    assert merged["hooks"]["post_llm_call"] == [
        {"command": "other-hook", "timeout": 5},
        {"command": HERMES_RELAY_COMMAND, "timeout": 10},
    ]
    repeated, changed = merge_hermes_stop_hook(merged)
    assert changed is False
    assert repeated == merged


def test_remove_hermes_stop_hook_keeps_unrelated_entries():
    config = {
        "hooks": {
            "post_llm_call": [
                {"command": "other-hook", "timeout": 5},
                {"command": HERMES_RELAY_COMMAND, "timeout": 10},
            ]
        }
    }

    updated, changed = remove_hermes_stop_hook(config)

    assert changed is True
    assert updated == {"hooks": {"post_llm_call": [{"command": "other-hook", "timeout": 5}]}}


def test_relay_noops_without_active_bmad_environment(tmp_path):
    assert relay_event("Stop", {"session_id": "s-1"}, {}) is False
    assert not (tmp_path / "events").exists()


def test_relay_writes_stop_event_for_active_bmad_environment(tmp_path):
    environ = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "todo-1-dev-1"}

    assert relay_event("Stop", {"session_id": "s-1", "cwd": "/project"}, environ) is True

    event_path = next((tmp_path / "events").glob("*-todo-1-dev-1-Stop.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["event"] == "Stop"
    assert event["task_id"] == "todo-1-dev-1"
    assert event["session_id"] == "s-1"
    assert event["transcript_path"] is None
    assert event["cwd"] == "/project"
    assert isinstance(event["ts"], int)


def test_relay_cli_reads_payload_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_TASK_ID", "todo-1-dev-1")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s-1", "cwd": "/project"}'))

    assert cli.main(["relay", "Stop"]) == 0

    event_path = next((tmp_path / "events").glob("*-todo-1-dev-1-Stop.json"))
    assert json.loads(event_path.read_text(encoding="utf-8"))["session_id"] == "s-1"
