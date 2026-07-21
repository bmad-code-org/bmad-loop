"""The shared relay-event primitive, the `bmad-loop relay` CLI, and their
schema parity with the standalone ``data/bmad_loop_hook.py`` script.

The primitive (:func:`bmad_loop.events.write_relay_event`) is the general,
in-tree counterpart to the standalone hook script for global/user-scoped-hook
adapters. The two share one event schema but cannot share code — the script is
installed as a lone stdlib-only file — so a parity test pins them together.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bmad_loop import cli
from bmad_loop.adapters.profile import CANONICAL_EVENTS
from bmad_loop.events import build_event, write_relay_event

HOOK_SCRIPT = Path(__file__).parent.parent / "src" / "bmad_loop" / "data" / "bmad_loop_hook.py"


def _sole_event(run_dir: Path) -> dict:
    files = list((run_dir / "events").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


# --- the primitive ---------------------------------------------------------


def test_write_noops_without_env(tmp_path):
    """No BMAD env vars -> None, and not even the events dir is created."""
    assert write_relay_event("Stop", {"session_id": "s-1"}, {}) is None
    assert not (tmp_path / "events").exists()


def test_write_noops_with_only_run_dir(tmp_path):
    """Both env vars are required; run_dir alone must not write."""
    environ = {"BMAD_LOOP_RUN_DIR": str(tmp_path)}
    assert write_relay_event("Stop", {"session_id": "s-1"}, environ) is None
    assert not (tmp_path / "events").exists()


def test_write_event_for_active_env(tmp_path):
    environ = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "1-1-a-dev-1"}
    payload = {
        "session_id": "abc-123",
        "transcript_path": "/home/u/.claude/projects/x/abc-123.jsonl",
        "cwd": "/proj",
    }

    written = write_relay_event("Stop", payload, environ)

    assert written is not None
    assert written.name.endswith("-1-1-a-dev-1-Stop.json")
    assert written.exists()
    # atomic tmp+rename leaves nothing behind
    assert not list((tmp_path / "events").glob("*.tmp"))

    event = _sole_event(tmp_path)
    assert event["event"] == "Stop"
    assert event["task_id"] == "1-1-a-dev-1"
    assert event["session_id"] == "abc-123"
    assert event["transcript_path"].endswith("abc-123.jsonl")
    assert event["cwd"] == "/proj"
    assert isinstance(event["ts"], int)


def test_write_camelcase_and_workspace_fallbacks(tmp_path):
    """agy-style protojson: conversationId + workspacePaths for cwd."""
    environ = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    payload = {
        "conversationId": "agy-3",
        "transcriptPath": "/ws/.gemini/antigravity-cli/transcript.jsonl",
        "workspacePaths": ["/ws"],
    }

    write_relay_event("Stop", payload, environ)

    event = _sole_event(tmp_path)
    assert event["session_id"] == "agy-3"
    assert event["transcript_path"].endswith("transcript.jsonl")
    assert event["cwd"] == "/ws"


def test_build_event_degrades_unusable_workspace_paths():
    """An empty/odd workspacePaths degrades to None, never IndexError."""
    event = build_event("Stop", {"workspacePaths": []}, ts=7, task_id="t1")
    assert event["cwd"] is None
    event = build_event("Stop", {"workspacePaths": [123]}, ts=7, task_id="t1")
    assert event["cwd"] is None


# --- the CLI path ----------------------------------------------------------


def test_cli_reads_payload_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_TASK_ID", "todo-1-dev-1")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s-1", "cwd": "/project"}'))

    assert cli.main(["relay", "Stop"]) == 0

    event = _sole_event(tmp_path)
    assert event["event"] == "Stop"
    assert event["task_id"] == "todo-1-dev-1"
    assert event["session_id"] == "s-1"
    assert event["cwd"] == "/project"


def test_cli_noops_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BMAD_LOOP_RUN_DIR", raising=False)
    monkeypatch.delenv("BMAD_LOOP_TASK_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s-1"}'))

    assert cli.main(["relay", "Stop"]) == 0
    assert not (tmp_path / "events").exists()


def test_cli_tolerates_garbage_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_TASK_ID", "t1")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))

    assert cli.main(["relay", "SessionEnd"]) == 0
    assert _sole_event(tmp_path)["session_id"] is None


def test_cli_rejects_non_canonical_event(monkeypatch):
    """`event` choices come from CANONICAL_EVENTS — an unknown name is a parse error."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["relay", "Bogus"])
    assert exc.value.code == 2


def test_cli_accepts_every_canonical_event(tmp_path, monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    for i, name in enumerate(sorted(CANONICAL_EVENTS)):
        monkeypatch.setenv("BMAD_LOOP_TASK_ID", f"t{i}")
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert cli.main(["relay", name]) == 0
    written = {json.loads(p.read_text())["event"] for p in (tmp_path / "events").glob("*.json")}
    assert written == CANONICAL_EVENTS


# --- schema parity with the standalone script ------------------------------


def _run_standalone(event: str, env: dict, payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), event],
        input=json.dumps(payload) if payload is not None else "",
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "abc", "transcript_path": "/t/abc.jsonl", "cwd": "/proj"},
        {"conversation_id": "conv-9"},
        {"conversationId": "agy", "transcriptPath": "/ws/t.jsonl", "workspacePaths": ["/ws"]},
        {"sessionId": "cop", "transcriptPath": "/c/events.jsonl"},
        {},
    ],
)
def test_standalone_and_primitive_share_one_schema(tmp_path, payload):
    """The lone stdlib script and the primitive must emit identical events.

    They cannot share code (the script is installed standalone), so this pins the
    inlined copy in bmad_loop_hook.py to bmad_loop.events on every payload dialect.
    """
    script_dir = tmp_path / "script"
    prim_dir = tmp_path / "prim"

    proc = _run_standalone(
        "Stop",
        {"BMAD_LOOP_RUN_DIR": str(script_dir), "BMAD_LOOP_TASK_ID": "t1"},
        payload,
    )
    assert proc.returncode == 0, proc.stderr

    write_relay_event(
        "Stop",
        payload,
        {"BMAD_LOOP_RUN_DIR": str(prim_dir), "BMAD_LOOP_TASK_ID": "t1"},
    )

    from_script = _sole_event(script_dir)
    from_prim = _sole_event(prim_dir)
    # ts is a wall-clock nanosecond stamp; the rest of the schema must match exactly.
    from_script.pop("ts")
    from_prim.pop("ts")
    assert from_script == from_prim
    assert set(from_prim) == {"event", "task_id", "session_id", "transcript_path", "cwd"}
