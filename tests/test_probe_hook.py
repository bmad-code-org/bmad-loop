"""The capture hook runs as a real subprocess, like the CLI runs it."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "src" / "bmad_loop" / "data" / "bmad_loop_probe_hook.py"


def run_hook(event: str, env: dict, payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), event],
        input=json.dumps(payload) if payload is not None else "",
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_noop_without_capture_dir(tmp_path):
    proc = run_hook("Stop", {}, {"session_id": "s1"})
    assert proc.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_writes_signal_and_payload(tmp_path):
    capture = tmp_path / "capture"
    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture), "BMAD_LOOP_TASK_ID": "probe"}
    payload = {
        "session_id": "abc-123",
        "transcript_path": "/home/u/.copilot/x/events.jsonl",
        "cwd": "/proj",
        "extra": {"nested": "field"},
    }
    proc = run_hook("Stop", env, payload)
    assert proc.returncode == 0

    signals = list(capture.glob("*.signal.json"))
    payloads = list(capture.glob("*.payload.json"))
    assert len(signals) == 1 and len(payloads) == 1
    assert "Stop" in signals[0].name and "Stop" in payloads[0].name

    signal = json.loads(signals[0].read_text())
    assert signal["event"] == "Stop"
    assert signal["task_id"] == "probe"
    assert signal["session_id"] == "abc-123"
    assert signal["transcript_path"].endswith("events.jsonl")

    captured = json.loads(payloads[0].read_text())
    # the ENTIRE raw payload survives (un-sanitized; the command scrubs later)
    assert captured["extra"] == {"nested": "field"}
    assert captured["argv_event"] == "Stop"  # native event name for pairing
    assert not list(capture.glob("*.tmp"))


def test_conversation_id_fallback(tmp_path):
    capture = tmp_path / "capture"
    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture)}
    proc = run_hook("Stop", env, {"conversation_id": "conv-9"})
    assert proc.returncode == 0
    signal = json.loads(next(capture.glob("*.signal.json")).read_text())
    assert signal["session_id"] == "conv-9"
    # task_id defaults when the env var is absent
    assert signal["task_id"] == "probe"


def test_tolerates_garbage_stdin(tmp_path):
    capture = tmp_path / "capture"
    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture)}
    proc = run_hook("SessionStart", env, None)  # empty stdin
    assert proc.returncode == 0
    assert len(list(capture.glob("*.signal.json"))) == 1
    captured = json.loads(next(capture.glob("*.payload.json")).read_text())
    assert captured == {"argv_event": "SessionStart"}


def test_installed_copy_matches_source(tmp_path):
    # packaged alongside the real relay; importlib.resources resolves it
    from importlib import resources

    packaged = resources.files("bmad_loop.data").joinpath("bmad_loop_probe_hook.py")
    assert packaged.read_text(encoding="utf-8") == SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_capture_dir_writes_nothing_and_exits_zero(tmp_path):
    """Parity with tests/test_hook_script.py's production-relay case: a driven
    session can plant the capture dir as a symlink and redirect or swallow the
    probe's capture -- the hook must refuse the link and degrade to a no-op
    instead of writing through it.

    Ablation guard: reverting _atomic_write to a plain
    open()+json.dump()+os.replace() makes this fail -- the plain writer follows
    the symlink and lands the payload in the attacker's directory."""
    target = tmp_path / "attacker"
    target.mkdir()
    capture = tmp_path / "capture"
    capture.symlink_to(target, target_is_directory=True)

    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture), "BMAD_LOOP_TASK_ID": "probe"}
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    assert list(target.iterdir()) == []
    assert list(capture.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_capture_files_mode_is_0600(tmp_path):
    """The probe's capture dir holds the FULL raw CLI payload -- more sensitive
    than the production relay's trimmed event -- so its files get the same
    0o600 narrowing (from the umask-derived mode a plain open() produces).

    Ablation guard: reverting _atomic_write to a plain open() makes this fail --
    a plain open() produces an umask-derived mode, not 0o600."""
    capture = tmp_path / "capture"
    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture), "BMAD_LOOP_TASK_ID": "probe"}
    proc = run_hook("Stop", env, {"session_id": "s1"})
    assert proc.returncode == 0

    written = list(capture.glob("*.signal.json")) + list(capture.glob("*.payload.json"))
    assert len(written) == 2
    for f in written:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
