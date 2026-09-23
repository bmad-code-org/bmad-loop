"""The hook relay script runs as a real subprocess, like Claude Code runs it."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "src" / "bmad_loop" / "data" / "bmad_loop_hook.py"


def _relay_module():
    """Import the relay by path. It ships as package DATA, not an importable
    module (init copies it into the workspace), so the behavioral tests below
    drive it as a subprocess — but the reparse-tag branch of `_is_link_like` is
    reachable only on Windows, and shipping it unexercised is how a capability
    check silently becomes dead code."""
    spec = importlib.util.spec_from_file_location("_bmad_loop_hook_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(event: str, env: dict, payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), event],
        input=json.dumps(payload) if payload is not None else "",
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_noop_without_env(tmp_path):
    proc = run_hook("Stop", {}, {"session_id": "s1"})
    assert proc.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_writes_event_file(tmp_path):
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "1-1-a-dev-1"}
    payload = {
        "session_id": "abc-123",
        "transcript_path": "/home/u/.claude/projects/x/abc-123.jsonl",
        "cwd": "/proj",
    }
    proc = run_hook("Stop", env, payload)
    assert proc.returncode == 0

    files = list((tmp_path / "events").glob("*.json"))
    assert len(files) == 1
    assert "1-1-a-dev-1" in files[0].name and "Stop" in files[0].name
    event = json.loads(files[0].read_text())
    assert event["event"] == "Stop"
    assert event["task_id"] == "1-1-a-dev-1"
    assert event["session_id"] == "abc-123"
    assert event["transcript_path"].endswith("abc-123.jsonl")
    assert not list((tmp_path / "events").glob("*.tmp"))


def test_conversation_id_fallback(tmp_path):
    """Cursor-style payloads carry conversation_id instead of session_id."""
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("Stop", env, {"conversation_id": "conv-9"})
    assert proc.returncode == 0
    files = list((tmp_path / "events").glob("*.json"))
    assert json.loads(files[0].read_text())["session_id"] == "conv-9"


def test_antigravity_payload(tmp_path):
    """agy payloads are protojson: conversationId, and workspacePaths for cwd."""
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    payload = {
        "conversationId": "agy-3",
        "transcriptPath": "/ws/.gemini/antigravity-cli/transcript.jsonl",
        "workspacePaths": ["/ws"],
        "terminationReason": "model_stop",
        "fullyIdle": True,
    }
    proc = run_hook("Stop", env, payload)
    assert proc.returncode == 0
    event = json.loads(next((tmp_path / "events").glob("*.json")).read_text())
    assert event["session_id"] == "agy-3"
    assert event["transcript_path"].endswith("transcript.jsonl")
    assert event["cwd"] == "/ws"


def test_workspace_paths_ignored_when_unusable(tmp_path):
    """An empty/odd workspacePaths must degrade to None, never IndexError."""
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("Stop", env, {"conversationId": "agy-4", "workspacePaths": []})
    assert proc.returncode == 0
    assert json.loads(next((tmp_path / "events").glob("*.json")).read_text())["cwd"] is None


def test_camelcase_payload(tmp_path):
    """Copilot payloads carry camelCase sessionId / transcriptPath."""
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    payload = {
        "sessionId": "cop-7",
        "transcriptPath": "/home/u/.copilot/session-state/cop-7/events.jsonl",
        "stopReason": "end_turn",
    }
    proc = run_hook("Stop", env, payload)
    assert proc.returncode == 0
    event = json.loads(next((tmp_path / "events").glob("*.json")).read_text())
    assert event["session_id"] == "cop-7"
    assert event["transcript_path"].endswith("events.jsonl")


def test_tolerates_garbage_stdin(tmp_path):
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("SessionEnd", env, None)  # empty stdin
    assert proc.returncode == 0
    files = list((tmp_path / "events").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["session_id"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_events_dir_writes_nothing_and_exits_zero(tmp_path):
    """#461 (Low): a driven session can write inside the run dir, so it can plant
    `events/` as a symlink and redirect the orchestrator's control-plane event
    stream — swallowing the Stop signal and stalling the run to timeout. The relay
    must refuse the link and degrade to a no-op, never write through it."""
    target = tmp_path / "attacker"
    target.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events").symlink_to(target, target_is_directory=True)

    env = {"BMAD_LOOP_RUN_DIR": str(run_dir), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    assert list(target.iterdir()) == []
    assert list((run_dir / "events").iterdir()) == []


class _ReparseStat:
    """Stand-in for the os.lstat() result of a Windows junction: a DIRECTORY
    mode (which is why os.path.islink() answers False) carrying a reparse tag."""

    st_mode = stat.S_IFDIR | 0o755
    st_reparse_tag = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


def test_is_link_like_refuses_a_reparse_tagged_dir(tmp_path, monkeypatch):
    """A Windows directory junction is a reparse point but NOT a symlink, so
    `os.path.islink` is False for it while `os.makedirs`/`os.open` follow it —
    and `mklink /J` needs no elevation, unlike a directory symlink, so it is the
    cheaper attack. The refusal keys on the reparse tag instead. That branch is
    reachable only on Windows; drive its logic here so it is not shipped
    unexercised (the `stat.IO_REPARSE_TAG_*` constants do not exist on POSIX,
    hence the substituted tuple).

    Ablation guard: dropping the `st_reparse_tag` arm of `_is_link_like` makes
    the last assertion fail."""
    relay = _relay_module()
    plain = tmp_path / "events"
    plain.mkdir()
    assert relay._is_link_like(plain) is False

    real_lstat = os.lstat
    monkeypatch.setattr(relay, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p, *a, **k: _ReparseStat() if str(p) == str(plain) else real_lstat(p),
    )
    assert relay._is_link_like(plain) is True


def test_fallback_refuses_a_redirect_that_appears_mid_write(tmp_path, monkeypatch):
    """The Windows fallback has no dir_fd to anchor to, so it re-resolves
    `events_dir` by path and a swap between the check and the create lands the
    temp file in the attacker's directory. The post-write re-check catches a
    swap that is still in place. Driven here with the dir_fd branch disabled,
    because on POSIX that branch is always taken and the fallback would ship
    unexercised.

    Ablation guard: deleting the post-write `_is_link_like` block makes this
    fail — the event gets published instead of refused."""
    relay = _relay_module()
    events = tmp_path / "events"
    calls = []
    real = relay._is_link_like

    def swapped_after_the_check(path):
        calls.append(path)
        return len(calls) > 1 and real(path) is False  # clean at check, dirty after

    monkeypatch.setattr(os, "supports_dir_fd", frozenset())  # force the fallback
    monkeypatch.setattr(relay, "_is_link_like", swapped_after_the_check)

    with pytest.raises(OSError, match="redirected mid-write"):
        relay._write_event(str(events), "1-t1-Stop.json", {"event": "Stop", "task_id": "t1"})

    assert len(calls) == 2  # the check ran on both sides of the write
    assert list(events.iterdir()) == []  # nothing published, no .tmp left behind


@pytest.mark.skipif(os.name == "nt", reason="dir_fd is implemented with the POSIX *at() calls")
def test_the_anchored_branch_is_actually_taken_on_posix(tmp_path, monkeypatch):
    """The capability probe must resolve to True where the capability exists, or
    the TOCTOU-closing layer ships dead while every other test stays green — the
    `islink` refusal covers the same cases, so nothing else would redden.

    This is not hypothetical: `os.replace` is NOT in `os.supports_dir_fd` on
    Linux (only `os.rename` is) even though it accepts src_dir_fd/dst_dir_fd, so
    probing `os.replace` — the function the code used to call — made the branch
    unreachable on every platform. Observe the anchoring behaviorally rather
    than re-deriving the probe, so editing the probe reddens this.

    Ablation guard: change the probe to `os.replace` (or drop the branch) and
    this fails; no other test notices."""
    relay = _relay_module()
    real_open, real_supports = os.open, os.supports_dir_fd
    # The premise, asserted rather than assumed: were a future CPython to drop
    # rename from the set, the branch would go dead and this says so directly.
    assert {real_open, os.rename} <= real_supports
    anchored = []

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        anchored.append(dir_fd)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy)
    # The probe tests os.open by IDENTITY, so the spy has to be in the capability
    # set too or the probe answers False and this measures the fallback instead
    # of the branch it exists to pin — which is how it first went red.
    monkeypatch.setattr(os, "supports_dir_fd", real_supports | {spy})
    relay._write_event(str(tmp_path / "events"), "1-t1-Stop.json", {"event": "Stop"})
    monkeypatch.undo()

    assert any(fd is not None for fd in anchored), "the create was never anchored to a dir_fd"


@pytest.mark.parametrize("forced_fallback", [False, True])
def test_a_short_os_write_still_publishes_the_whole_payload(tmp_path, monkeypatch, forced_fallback):
    """`os.write` may write FEWER bytes than asked and just return the count. The
    buffered `open()` this replaced looped internally; the raw fd needed for
    O_NOFOLLOW/dir_fd does not. A truncated event file is not retried but LOST:
    `SignalWatcher.poll` adds a name to `_consumed` before parsing it, so
    malformed JSON is skipped and never re-read — the Stop signal is gone and the
    run waits out `session_timeout_min`.

    Both branches are driven: the loop is shared, but the fallback is the only
    path Windows takes and POSIX would otherwise never exercise it.

    Ablation guard: collapsing `_write_all` back to a single `os.write` makes
    this fail. Nothing else in the suite catches that — a real `os.write`
    returns the full count, so the normal-path tests pass either way."""
    relay = _relay_module()
    events = tmp_path / "events"
    real_write = os.write

    def a_byte_at_a_time(fd, data):
        return real_write(fd, bytes(data)[:1])  # a legal short write

    if forced_fallback:
        monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    event = {"event": "Stop", "task_id": "t1", "session_id": "s" * 300}
    monkeypatch.setattr(os, "write", a_byte_at_a_time)
    relay._write_event(str(events), "1-t1-Stop.json", event)
    monkeypatch.undo()

    published = list(events.glob("*.json"))
    assert len(published) == 1
    assert json.loads(published[0].read_text()) == event
    assert list(events.glob("*.tmp")) == []


def test_a_zero_length_write_raises_instead_of_spinning(tmp_path, monkeypatch):
    """`_write_all` loops on short writes, so a descriptor that always accepts 0
    bytes would spin forever. Refuse instead: the caller degrades to a no-op and
    the run takes the timeout path, which beats a hook process that never exits.

    Ablation guard: dropping the `written <= 0` arm hangs this test."""
    relay = _relay_module()
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="short write"):
        relay._write_event(str(tmp_path / "events"), "1-t1-Stop.json", {"event": "Stop"})


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junctioned_events_dir_writes_nothing_and_exits_zero(tmp_path):
    """The Windows half of the symlink test — Windows CI is its only oracle, a
    junction cannot be created on POSIX."""
    # If either tag constant were misnamed the tuple is empty and the refusal
    # silently never fires. Assert it directly rather than inferring from below.
    assert _relay_module()._LINK_REPARSE_TAGS

    target = tmp_path / "attacker"
    target.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run_dir / "events"), str(target)],
        check=True,
        capture_output=True,
    )
    # The premise, asserted rather than assumed: were a future CPython to start
    # reporting junctions as links, this says so directly instead of going green
    # for the wrong reason.
    assert os.path.islink(run_dir / "events") is False

    env = {"BMAD_LOOP_RUN_DIR": str(run_dir), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_event_file_mode_is_0600(tmp_path):
    """Event files carry the orchestrator's control plane; nothing but the
    operator running the loop needs to read them (narrowed from the umask-derived
    0644 a plain `open()` produced)."""
    env = {"BMAD_LOOP_RUN_DIR": str(tmp_path), "BMAD_LOOP_TASK_ID": "t1"}
    proc = run_hook("Stop", env, {"session_id": "s1"})
    assert proc.returncode == 0
    written = next((tmp_path / "events").glob("*.json"))
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


# --------------------------------------------- where the event is written (#494)


def test_the_events_dir_env_wins_over_the_run_dir(tmp_path):
    """#494: the channel moved out of the project tree, and this variable is how
    the orchestrator names it. Nothing may land in the legacy in-tree location
    when it is set — an event written there is only found by the orchestrator's
    compatibility poll, and the whole point is that a branch switch or a worktree
    mount cannot take the live channel away."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events = tmp_path / "state" / "runs" / "RID" / "events"
    env = {
        "BMAD_LOOP_RUN_DIR": str(run_dir),
        "BMAD_LOOP_EVENTS_DIR": str(events),
        "BMAD_LOOP_TASK_ID": "t1",
    }
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    files = list(events.glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["session_id"] == "s1"
    assert not (run_dir / "events").exists()


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_the_events_dir_falls_back_to_the_run_dir(tmp_path, value):
    """The version-skew half, from the producing side: an orchestrator that
    predates #494 sets no BMAD_LOOP_EVENTS_DIR, and its sessions must still write
    their events somewhere the orchestrator polls — the legacy location it knows.

    The empty case is not hypothetical: `export BMAD_LOOP_EVENTS_DIR=` is what an
    unset-looking export leaves behind, and an empty path names the launch cwd,
    which is not a control plane.

    Ablation guard: change the `or` to a presence test (`is not None`) and the
    empty case writes into the CLI's working directory instead — this fails."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"BMAD_LOOP_RUN_DIR": str(run_dir), "BMAD_LOOP_TASK_ID": "t1"}
    if value is not None:
        env["BMAD_LOOP_EVENTS_DIR"] = value
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    files = list((run_dir / "events").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["session_id"] == "s1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_env_directed_events_dir_writes_nothing_and_exits_zero(tmp_path):
    """The #493 hardening is about the DIRECTORY the relay is pointed at, so it
    has to hold for the one an env var names just as it did for the one derived
    from the run dir. Under isolation a driven session can write into the project
    but not into the state root — yet the variable itself is inherited env, and a
    session that can plant a link at the named path must still not redirect the
    control plane through it."""
    target = tmp_path / "attacker"
    target.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "events").symlink_to(target, target_is_directory=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    env = {
        "BMAD_LOOP_RUN_DIR": str(run_dir),
        "BMAD_LOOP_EVENTS_DIR": str(state / "events"),
        "BMAD_LOOP_TASK_ID": "t1",
    }
    proc = run_hook("Stop", env, {"session_id": "s1"})

    assert proc.returncode == 0
    assert list(target.iterdir()) == []
    assert list((state / "events").iterdir()) == []
    # and it did not silently fall back to the legacy location either
    assert not (run_dir / "events").exists()


def test_installed_command_writes_without_workspace_script(tmp_path):
    from bmad_loop.install import install_into

    assert install_into(tmp_path, skills=False) == 0
    assert not (tmp_path / ".bmad-loop" / "bmad_loop_hook.py").exists()
    config = json.loads((tmp_path / ".claude/settings.json").read_text())
    command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
    proc = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "BMAD_LOOP_RUN_DIR": str(tmp_path),
            "BMAD_LOOP_TASK_ID": "t1",
        },
        input=json.dumps({"session_id": "s1"}),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0 and proc.stdout == ""
    event = json.loads(next((tmp_path / "events").glob("*.json")).read_text())
    assert event["event"] == "Stop" and event["task_id"] == "t1"
