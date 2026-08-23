"""GenericTmuxAdapter tests.

Unit tests need no tmux. The integration tests drive a REAL tmux session but
substitute a tiny shell script for the CLI binary: the script writes
result.json and emits hook-style event files itself (canonical event names,
exactly what each CLI's hook registration produces), exercising spawn / env
propagation / hook-signal waiting / kill end-to-end for any profile.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import regex

from bmad_loop import devcontract, runs
from bmad_loop.adapters import env_fault, generic, tmux_base
from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec, SpecSnapshot
from bmad_loop.adapters.generic import GenericDevAdapter, GenericTmuxAdapter
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.profile import get_profile
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.model import TokenUsage
from bmad_loop.policy import LimitsPolicy, NotifyPolicy, Policy
from bmad_loop.signals import HookEvent

HAVE_TMUX = sys.platform != "win32" and shutil.which("tmux") is not None

# The read-back decodes artifacts as UTF-8. A spec truncated mid-write (the CLI was
# killed) can end inside a multi-byte sequence; `read_text(encoding="utf-8")` then
# raises UnicodeDecodeError — a ValueError, NOT an OSError.
_BAD_UTF8 = b"\xff\xfe\x00\x01 not utf-8 \x80\x81"

# The line that decides where the fake relay writes its events. Swapped below to
# build the version-skew twin, so the two scripts differ in nothing else.
_EVENTS_LINE = 'ed="$BMAD_LOOP_EVENTS_DIR"'
_LEGACY_EVENTS_LINE = 'ed="$BMAD_LOOP_RUN_DIR/events"'

FAKE_CLI = """#!/bin/bash
# fake CLI: last positional arg is the prompt; env comes from tmux -e
prompt="${@: -1}"
ts=$(date +%s%N)
ed="$BMAD_LOOP_EVENTS_DIR"
mkdir -p "$ed" "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts" "$BMAD_LOOP_TASK_ID" > "$ed/$ts-$BMAD_LOOP_TASK_ID-SessionStart.json"
echo "{\\"workflow\\": \\"auto-dev\\", \\"prompt\\": \\"$prompt\\"}" \\
    > "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID/result.json"
ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts2" "$BMAD_LOOP_TASK_ID" > "$ed/$ts2-$BMAD_LOOP_TASK_ID-Stop.json"
sleep 60  # stay alive like an idle interactive session
"""

# What a relay installed before #494 knows: only the in-tree location. Pairing it
# with a current orchestrator is the version-skew case the dual poll covers, and
# it is the ordinary state of any project whose `.bmad-loop/bmad_loop_hook.py`
# copy predates the move.
LEGACY_EVENTS_FAKE_CLI = FAKE_CLI.replace(_EVENTS_LINE, _LEGACY_EVENTS_LINE)


def make_adapter(
    tmp_path, profile_name="claude", binary=None, extra_args=None, mux=None, **policy_kw
) -> GenericTmuxAdapter:
    # session_name derives from run_dir.name, and the live tests all share one
    # tmux server — a fixed "run" name races one test's kill-session teardown
    # against another's new-window under pytest-xdist. Production run dirs are
    # unique run ids, so unique-per-adapter matches reality.
    run_dir = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    policy = Policy(limits=LimitsPolicy(**policy_kw) if policy_kw else LimitsPolicy())
    profile = get_profile(profile_name)
    return GenericTmuxAdapter(
        run_dir=run_dir,
        policy=policy,
        profile=profile,
        binary=binary,
        extra_args=extra_args,
        mux=mux,
        # As `runsetup.make_adapters` does: the primary channel is out of the
        # project tree (#494), keyed by run. Under `tmp_path` rather than the real
        # state root only because these are unit tests; what matters is that it is
        # NOT `run_dir / "events"`, so every test here drives the production shape
        # (out-of-tree primary, in-tree legacy still under poll).
        events_dir=tmp_path / "state" / run_dir.name / "events",
    )


def test_ensure_session_tags_project(tmp_path, monkeypatch, force_tmux_backend):
    """A freshly created agent session is stamped with its project so a cleanup
    in another project never prunes this run. The set-option now flows through
    the tmux backend, so patch its subprocess seam. ``force_tmux_backend`` pins
    tmux against any installed win32-matching external backend (a no-op on a
    stock POSIX box) — the adapter's default ``mux`` is ``get_multiplexer()``."""
    from bmad_loop import runs

    project = tmp_path
    run_dir = project / ".bmad-loop" / "runs" / "RID"  # parents[2] == project
    adapter = GenericTmuxAdapter(
        run_dir=run_dir, policy=Policy(limits=LimitsPolicy()), profile=get_profile("claude")
    )

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        rc = 1 if argv[1] == "has-session" else 0  # session missing -> create it
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake_run)
    adapter._ensure_session(project)

    assert [c for c in calls if c[1] == "set-option"] == [
        [
            "tmux",
            "set-option",
            "-t",
            adapter.session_name,
            runs.PROJECT_OPTION,
            runs.project_tag(project),
        ]
    ]


def make_spec(tmp_path, task_id="1-1-a-dev-1", timeout_s=30.0, model="sonnet") -> SessionSpec:
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env={"BMAD_LOOP_MODE": "1", "BMAD_LOOP_TASK_ID": task_id},
        model=model,
        timeout_s=timeout_s,
    )


def test_build_command_claude(tmp_path):
    adapter = make_adapter(tmp_path)
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith("claude '/bmad-dev-auto 1-1-a' --permission-mode bypassPermissions")
    assert cmd.endswith("--model sonnet")


def test_build_command_codex_renders_skill_mention(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="codex")
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith(
        "codex 'Use the $bmad-dev-auto skill now, and use subagents as needed: 1-1-a'"
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd.endswith("--model sonnet")


def test_build_command_gemini_uses_interactive_flag(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="gemini")
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith("gemini -i '/bmad-dev-auto 1-1-a' --approval-mode=yolo")
    assert cmd.endswith("--model sonnet")


def test_extra_args_replace_profile_bypass(tmp_path):
    adapter = make_adapter(tmp_path, extra_args=("--custom-flag",))
    cmd = adapter.build_command(make_spec(tmp_path))
    assert "--custom-flag" in cmd
    assert "bypassPermissions" not in cmd


def test_read_result_variants(tmp_path):
    adapter = make_adapter(tmp_path)
    task_dir = adapter.tasks_dir / "t1"
    task_dir.mkdir(parents=True)
    assert adapter._read_result("t1") is None  # missing
    (task_dir / "result.json").write_text("{broken")
    assert adapter._read_result("t1") is None  # malformed
    (task_dir / "result.json").write_text('["not a dict"]')
    assert adapter._read_result("t1") is None  # wrong shape
    (task_dir / "result.json").write_text('{"clean": true}')
    assert adapter._read_result("t1") == {"clean": True}


def test_await_result_grace_expires_fast(tmp_path):
    adapter = make_adapter(tmp_path)
    (adapter.tasks_dir / "t1").mkdir(parents=True)
    start = time.monotonic()
    assert adapter._await_result("t1", grace_s=0.2) is None
    assert time.monotonic() - start < 5


# ------------------------------------------- verified kill escalation (#157)
#
# GenericAdapter.kill was a single best-effort kill_window with no verification
# the window died. These pin the new bounded escalation: verify within
# teardown_grace_s, then force-kill the pane pids and re-kill — degrading
# cleanly for a backend that doesn't offer window_pane_pids (herdr returns the
# seam default []).


class _TeardownMux:
    """Only the ops kill() drives — kill_window, list_window_ids (liveness),
    window_pane_pids — with scriptable survival: the window stays alive until
    ``survives_kills`` kill_window calls have landed."""

    def __init__(self, survives_kills=0, pids=()):
        self.survives_kills = survives_kills
        self.pids = list(pids)
        self.kill_windows = 0
        self.liveness_probes = 0
        self.pane_pid_reads = 0

    def kill_window(self, target):
        self.kill_windows += 1

    def list_window_ids(self, session):
        self.liveness_probes += 1
        return ["@w1"] if self.kill_windows <= self.survives_kills else []

    def window_pane_pids(self, target):
        self.pane_pid_reads += 1
        return list(self.pids)


class _RecordingHost:
    """A tiny process-tree model for the reap path. ``alive`` is the set of live
    pids; ``descendants_map`` gives each pid's transitive children (the pre-kill
    harvest reads it); ``ignore_terminate`` pids survive SIGTERM so a force_kill is
    required (the terminate-precedes-force-kill case); ``no_identity`` pids report
    identity ``None`` (the unconfirmable case). terminate/force_kill record the call
    and — unless ignored — drop the pid from ``alive``, so the reap's poll converges."""

    def __init__(
        self, alive=(), descendants_map=None, ignore_terminate=(), no_identity=(), reused=()
    ):
        self.alive = set(alive)
        self.descendants_map = dict(descendants_map or {})
        self.ignore_terminate = set(ignore_terminate)
        self.no_identity = set(no_identity)
        # pids recycled to a different process since harvest: still alive, but the
        # recorded identity no longer matches — alive_and_ours must read them not-ours.
        self.reused = set(reused)
        self.force_killed: list[int] = []
        self.terminated: list[int] = []

    def descendants(self, pid):
        # The real seam stamps identity during enumeration; the fake mirrors that,
        # still emitting None for no_identity pids so the adapter's unconfirmable
        # guard stays exercised (the contract allows None where no stamp exists).
        return {child: self.identity(child) for child in self.descendants_map.get(pid, ())}

    def identity(self, pid):
        return None if pid in self.no_identity else float(pid)

    def is_alive(self, pid):
        # Bare existence — a recycled pid still reads alive here (that's the point:
        # the reap must not treat this as licence to signal an unconfirmable pid).
        return pid in self.alive

    def alive_and_ours(self, pid, identity):
        if pid in self.reused or pid not in self.alive:
            return False  # recycled pid or gone → not ours
        return identity is None or identity == self.identity(pid)  # None → bare-liveness degrade

    def terminate(self, pid):
        self.terminated.append(pid)
        if pid not in self.ignore_terminate:
            self.alive.discard(pid)

    def force_kill(self, pid):
        self.force_killed.append(pid)
        self.alive.discard(pid)


def _kill_handle() -> SessionHandle:
    return SessionHandle(task_id="3-1-dev-1", native_id="@w1")


def _lifecycle_lines(adapter, task_id="3-1-dev-1"):
    path = adapter.tasks_dir / task_id / "session-lifecycle.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_kill_returns_on_first_dead_probe_without_escalating(tmp_path, monkeypatch):
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    mux = _TeardownMux(survives_kills=0)
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert mux.kill_windows == 1
    assert mux.liveness_probes == 1
    # #183: the clean end now reads the pane pids exactly ONCE (the pre-harvest
    # snapshot), yet — no straggler and the window dead on probe 1 — it still
    # performs no window re-kill and no escalation, leaving no breadcrumb.
    assert mux.pane_pid_reads == 1
    assert _lifecycle_lines(adapter) == []


def test_kill_reaps_clean_end_straggler(tmp_path, monkeypatch):
    """Clean end (window dead on probe 1) with a detached straggler harvested
    pre-kill: it is terminated, then force-killed (it ignored SIGTERM), all within
    the grace. terminate must precede force_kill so a mid-write process can flush."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    # pane root 100 dies with the window; the setsid child 200 (a harvested child of
    # 100, its own session at runtime) survives the pane-pgid kill and must be reaped.
    host = _RecordingHost(alive={200}, descendants_map={100: [200]}, ignore_terminate={200})
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=0, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert mux.pane_pid_reads == 1  # harvested once, pre-kill
    assert mux.kill_windows == 1  # window died on the first strike — no re-kill
    assert host.terminated == [200]
    assert host.force_killed == [200]  # ignored SIGTERM → force-killed within grace
    events = _lifecycle_lines(adapter)
    assert [e["event"] for e in events] == ["straggler-reap", "kill-outcome"]
    assert events[0]["pids"] == [200]
    assert events[1]["forced"] == [200]
    assert events[1]["unreaped"] == []  # reaped clean (distinct key from the wedged `alive`)


def test_kill_clean_end_no_stragglers_leaves_no_breadcrumb(tmp_path, monkeypatch):
    """Clean end, harvested tree already dead: the pane pids are read once and the
    window killed once, but the reap finds nothing — no terminate, no force_kill, no
    breadcrumb (the #157 clean path, now with a one-time pre-harvest read)."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    host = _RecordingHost(alive=set(), descendants_map={100: [200]})
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=0, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert mux.pane_pid_reads == 1
    assert mux.kill_windows == 1
    assert host.terminated == []
    assert host.force_killed == []
    assert _lifecycle_lines(adapter) == []


def test_kill_never_signals_identity_none_straggler(tmp_path, monkeypatch):
    """A harvested straggler whose identity could not be read (None) is
    unconfirmable — a possible pid reuse — so it is never signalled AT ALL: no
    terminate, no force-kill, and no poll burning the grace deadline (even a
    SIGTERM to a recycled pid kills an innocent process). It rides out the grace
    untouched, recorded honestly as unreaped; with nothing actually signalled
    there is no straggler-reap breadcrumb, only the kill-outcome."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    host = _RecordingHost(
        alive={200}, descendants_map={100: [200]}, ignore_terminate={200}, no_identity={200}
    )
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=0, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.terminated == []  # identity None → never signalled (possible reuse)
    assert host.force_killed == []
    events = _lifecycle_lines(adapter)
    assert [e["event"] for e in events] == ["kill-outcome"]
    assert events[0]["forced"] == []
    assert events[0]["unreaped"] == [200]  # unconfirmable → left alive, recorded honestly


def test_kill_reap_skips_reused_harvested_pid(tmp_path, monkeypatch):
    """A harvested pid recycled to an unrelated process since the pre-kill snapshot
    reads not-alive-and-ours at reap (identity mismatch) and is never signalled —
    only the genuinely-ours straggler is reaped."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    # 200 is still ours (reaped); 300 was reused by the OS for an unrelated process.
    host = _RecordingHost(
        alive={200, 300}, descendants_map={100: [200, 300]}, ignore_terminate={200}, reused={300}
    )
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=0, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.terminated == [200]  # 300 never touched — its identity no longer matches
    assert host.force_killed == [200]
    events = _lifecycle_lines(adapter)
    assert events[0]["event"] == "straggler-reap"
    assert events[0]["pids"] == [200]


def test_kill_wedged_escalation_also_force_kills_harvested_descendants(tmp_path, monkeypatch):
    """A wedged window (outlives grace) force-kills the re-read pane pids AND every
    harvested descendant still alive-and-ours — the setsid child the pane-pid
    escalation alone would miss. A descendant that no longer reads alive-and-ours (a
    reused/gone pid) is skipped."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    # pane root 100 (re-read at escalation) + harvested descendants 200 (still
    # alive-and-ours) and 300 (gone → not alive_and_ours, must be skipped).
    host = _RecordingHost(alive={100, 200}, descendants_map={100: [200, 300]})
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=99, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.force_killed == [100, 200]  # 300 skipped: not alive_and_ours
    assert mux.kill_windows == 2
    events = _lifecycle_lines(adapter)
    assert [e["event"] for e in events] == ["kill-escalated", "kill-outcome"]
    assert events[0]["pids"] == [100]
    assert events[1]["alive"] is True
    assert events[1]["escalated"] is True


def test_kill_wedged_escalation_never_force_kills_identity_none_descendant(tmp_path, monkeypatch):
    """A wedged window must NOT force-kill a harvested descendant whose recorded
    identity is None: alive_and_ours(pid, None) degrades to bare is_alive, so a
    reused pid would pass — the ProcessHost contract forbids force-killing it. Only
    the pane pid (pinned by the live window) and the identity-confirmed descendant
    are struck."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    # 200 is identity-confirmed (force-killed); 400 is alive but unconfirmable
    # (identity None) → must be left untouched even under the wedged escalation.
    host = _RecordingHost(
        alive={100, 200, 400}, descendants_map={100: [200, 400]}, no_identity={400}
    )
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=99, pids=[100])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.force_killed == [100, 200]  # 400 refused: None identity
    assert 400 not in host.force_killed


def test_kill_strikes_window_before_reraising_bad_host_override(tmp_path, monkeypatch):
    """The process-host lookup now precedes the first strike; an explicit-but-bogus
    BMAD_LOOP_PROCESS_HOST must still raise loudly (never silently mis-signal), but
    the window must not be left alive behind the raise — kill_window fires once, then
    ProcessHostError propagates and the harvest is never reached."""
    from bmad_loop.process_host import ProcessHostError, get_process_host

    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "bogus-host-name")
    get_process_host.cache_clear()
    mux = _TeardownMux(survives_kills=0)
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    try:
        with pytest.raises(ProcessHostError):
            adapter.kill(_kill_handle())
        assert mux.kill_windows == 1  # struck once before the raise
        assert mux.pane_pid_reads == 0  # never reached the harvest
    finally:
        get_process_host.cache_clear()


def test_kill_escalates_to_pane_pid_force_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    host = _RecordingHost()
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=99, pids=[4242])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.force_killed == [4242]
    assert mux.kill_windows == 2  # first strike + post-escalation re-kill
    events = _lifecycle_lines(adapter)
    assert [e["event"] for e in events] == ["kill-escalated", "kill-outcome"]
    assert events[0]["pids"] == [4242]
    assert events[1]["alive"] is True  # honest outcome: the window survived even the escalation
    assert events[1]["escalated"] is True


def test_kill_degrades_when_backend_offers_no_pids(tmp_path, monkeypatch):
    """A herdr-shaped backend inherits the seam default [] — the escalation
    degrades to the re-kill + breadcrumb, force-killing nothing."""
    monkeypatch.setattr(generic, "KILL_POLL_S", 0)
    host = _RecordingHost()
    monkeypatch.setattr(generic, "get_process_host", lambda: host)
    mux = _TeardownMux(survives_kills=99, pids=())
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0.05)
    adapter.kill(_kill_handle())
    assert host.force_killed == []
    assert mux.kill_windows == 2
    events = _lifecycle_lines(adapter)
    assert [e["event"] for e in events] == ["kill-escalated", "kill-outcome"]
    assert events[0]["pids"] == []


def test_kill_grace_zero_is_the_legacy_single_strike(tmp_path):
    mux = _TeardownMux(survives_kills=99, pids=[4242])
    adapter = make_adapter(tmp_path, mux=mux, teardown_grace_s=0)
    adapter.kill(_kill_handle())
    assert mux.kill_windows == 1
    assert mux.liveness_probes == 0
    assert mux.pane_pid_reads == 0
    assert _lifecycle_lines(adapter) == []


# ----------------------------------------------- GenericDevAdapter (B1/B7)
#
# Alex's generic bmad-dev-auto skill writes no result.json; this adapter
# synthesizes the legacy result dict from the spec it leaves on disk, on the
# Stop event, via devcontract. These exercise that override in isolation.


class _UnitMux:
    """Mux stand-in for the unit tests: the session is alive, nothing else exists.

    These tests stub `_window_alive`, so before #489 they never touched `self.mux`
    at all and the module docstring's "unit tests need no tmux" held by accident.
    The crash path now asks `has_session` for its diagnosis, which without this
    would reach the HOST multiplexer — a real subprocess (~130ms) answering False
    for a tmp_path session that never existed, scoring every crash test
    `session_vanished` and writing a breadcrumb. Answering True keeps each test on
    the side of the distinction it was written for: the CLI exited.
    """

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def has_session(self, name):
        return True

    def send_text(self, window_id, text):
        # The contract/stall nudges reach the mux too; recording them keeps that
        # off the host binary as well, which is the same promise as has_session.
        self.sent.append((window_id, text))


def make_dev_adapter(tmp_path, profile_name="claude", policy=None, mux=None):
    impl = tmp_path / "impl"
    impl.mkdir()
    # project root == tmp_path so rebased(spec.cwd=tmp_path) is a no-op: these
    # unit tests exercise _result_json in place, where cwd == the project root.
    paths = ProjectPaths(
        project=tmp_path,
        implementation_artifacts=impl,
        planning_artifacts=tmp_path / "plan",
    )
    adapter = GenericDevAdapter(
        run_dir=tmp_path / "run",
        policy=policy or Policy(limits=LimitsPolicy()),
        profile=get_profile(profile_name),
        paths=paths,
        mux=mux or _UnitMux(),
    )
    return adapter, impl


class _ScriptedWatcher:
    """SignalWatcher stand-in: yields a scripted HookEvent per wait_for call, then
    None. on_call(n) fires before the nth return so a test can flush an on-disk
    artifact between events (mirrors a session writing its spec mid-run)."""

    def __init__(self, events, on_call=None):
        self._events = list(events)
        self._on_call = on_call
        self.calls = 0

    def wait_for(self, task_id, kinds, timeout_s, since_ns=0):
        self.calls += 1
        if self._on_call:
            self._on_call(self.calls)
        return self._events.pop(0) if self._events else None


def _stop_event(task_id, session_id, transcript_path):
    return HookEvent(
        ts=1,
        event="Stop",
        task_id=task_id,
        session_id=session_id,
        transcript_path=transcript_path,
        path=Path("x"),
    )


def _dev_handle(launched_ns=0) -> SessionHandle:
    return SessionHandle(task_id="3-1-dev-1", native_id="@1", launched_ns=launched_ns)


def _dev_spec(tmp_path, story_key="3-1") -> SessionSpec:
    return SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": story_key},
    )


def test_generic_dev_synthesizes_done_spec(tmp_path):
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented the thing.\n"
    )
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["workflow"] == "auto-dev"
    assert rj["status"] == "done"
    assert rj["baseline_commit"] == "abc123"  # mapped from baseline_revision
    assert rj["story_key"] == "3-1"
    assert rj["escalations"] == []
    assert "dw_ids" not in rj  # a normal story exports no BMAD_LOOP_DW_IDS


def test_generic_dev_bundle_stamps_dw_ids_from_env(tmp_path):
    # The orchestrator exports the bundle's owned dw ids; the generic skill never
    # authors them. The adapter stamps them onto the synthesized result, tolerant
    # of whitespace in the env value (e.g. a hand-set or hook-rewritten "DW-1, DW-2").
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-dw-bundle.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nResolved the bundle.\n"
    )
    spec = SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto bundle",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "dw-bundle", "BMAD_LOOP_DW_IDS": "DW-1, DW-2"},
    )
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["dw_ids"] == ["DW-1", "DW-2"]


def test_generic_dev_dw_ids_none_env_does_not_crash(tmp_path):
    # A misbehaving plugin/hook could set BMAD_LOOP_DW_IDS to None instead of
    # deleting it; synthesis must not crash (it would false-stall a completed
    # session), and emits no dw ids.
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented the thing.\n"
    )
    spec = SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1", "BMAD_LOOP_DW_IDS": None},
    )
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["status"] == "done"
    assert "dw_ids" not in rj


def test_generic_dev_finds_spec_in_worktree(tmp_path):
    # Under worktree isolation the skill runs with cwd set to the worktree and
    # leaves its terminal spec in the worktree's rebased implementation-artifacts
    # dir, not the main checkout's. The adapter must search the cwd-rebased dir or
    # it false-stalls a story that actually completed (and rolls it back).
    impl = tmp_path / "_bmad-output" / "impl"
    impl.mkdir(parents=True)  # configured main-repo dir, left empty
    paths = ProjectPaths(
        project=tmp_path,
        implementation_artifacts=impl,
        planning_artifacts=tmp_path / "_bmad-output" / "plan",
    )
    adapter = GenericDevAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        paths=paths,
    )

    wt = tmp_path / "wt"
    wt_impl = wt / "_bmad-output" / "impl"
    wt_impl.mkdir(parents=True)
    (wt_impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented the thing.\n"
    )

    rj = adapter._result_json(_dev_handle(), _dev_spec(wt), wait=False)
    assert rj is not None and rj["status"] == "done"

    # Genuinely cwd-driven: pointed at the main checkout (empty dir), nothing is found.
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None


def test_generic_dev_blocked_spec_is_critical(tmp_path):
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nUnclear intent.\n"
    )
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "blocked"
    assert rj["escalations"][0]["severity"] == "CRITICAL"


def test_generic_dev_finds_no_spec_fallback(tmp_path):
    """The no-spec fallback has frontmatter status but no `## Auto Run Result`
    heading, so it is located by filename rather than content."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "bmad-dev-auto-result-unclear-1234.md").write_text(
        "---\nstatus: blocked\n---\n\n# BMad Dev Auto Result\n\n"
        "Status: blocked\nBlocking condition: unclear intent\n"
    )
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "blocked"
    assert rj["escalations"][0]["type"] == "blocked"


def test_generic_dev_fallback_done_marker_frontmatter_only(tmp_path):
    """The workflow completion contract instructs exactly this shape: a
    ``bmad-dev-auto-result-*.md`` with ``status: done`` frontmatter and no
    ``## Auto Run Result`` heading. It must be located by filename prefix and
    synthesize a done result."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "bmad-dev-auto-result-1-1-tea.automate-1.md").write_text(
        "---\nstatus: done\n---\n\nCompletion signal; artifacts live elsewhere.\n"
    )
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "done"


def test_scan_readback_non_utf8_spec_returns_none(tmp_path):
    """The scan-path twin of the stories read-back guard: a binary/truncated spec
    (or a torn glimpse of one still being written) degrades to a result-less
    read-back on the Stop path too, so the session nudges/stalls instead of
    crashing the run. find_result_artifact's `except OSError` never caught this."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_bytes(_BAD_UTF8)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None


def test_scan_readback_non_utf8_fallback_marker_returns_none(tmp_path):
    """The fallback marker is name-matched, so it reaches synthesize_result unread."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "bmad-dev-auto-result-3-1-dev-1.md").write_bytes(_BAD_UTF8)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None


def test_generic_dev_ignores_pre_launch_artifact(tmp_path, monkeypatch):
    """A spec left by a prior cycle (mtime below the launch floor) is not this
    session's output and must not be read as a stale completion."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)  # don't sit out the await grace
    spec = impl / "spec-old.md"
    spec.write_text("---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n")
    floor = spec.stat().st_mtime_ns + 1_000_000_000  # 1s after the file's mtime
    assert adapter._result_json(_dev_handle(floor), _dev_spec(tmp_path), wait=True) is None


def test_generic_dev_result_json_polls_until_artifact_flushed(tmp_path, monkeypatch):
    """wait=True must briefly await a spec that isn't flushed the instant the Stop
    event fires, rather than reading once and mis-reporting a live run as stalled."""
    adapter, impl = make_dev_adapter(tmp_path)
    spec_file = impl / "spec-3-1-foo.md"
    calls = {"n": 0}

    def delayed_find(artifacts, *, since_ns):
        calls["n"] += 1
        if calls["n"] < 3:
            return None  # not yet flushed to disk
        spec_file.write_text("---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n")
        return spec_file

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", delayed_find)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)  # spin without real sleeps
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj is not None and rj["status"] == "done"
    assert calls["n"] >= 3  # it polled rather than giving up on the first miss


# ------------------------------- GenericDevAdapter stories-mode read-back
#
# Under folder+id dispatch (BMAD_LOOP_SPEC_FOLDER set), the adapter resolves the
# story spec deterministically at <spec-folder>/stories/<id>-*.md instead of the
# mtime-floor scan.


def _stories_spec(tmp_path, story_key="1", spec_folder="epic") -> SessionSpec:
    return SessionSpec(
        task_id="1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto Spec folder: epic. Story id: 1.",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": story_key, "BMAD_LOOP_SPEC_FOLDER": spec_folder},
    )


def _write_story_spec(tmp_path, story_key, slug, body, spec_folder="epic") -> Path:
    d = tmp_path / spec_folder / "stories"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{story_key}-{slug}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_stories_readback_resolves_by_id_not_mtime_scan(tmp_path, monkeypatch):
    adapter, impl = make_dev_adapter(tmp_path)
    # a stray, NEWER artifact in the impl dir would win the mtime scan — the
    # stories path must ignore it entirely (never call find_result_artifact).
    (impl / "spec-stray.md").write_text(
        "---\nstatus: done\nbaseline_revision: straybase\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    _write_story_spec(
        tmp_path,
        "1",
        "foo",
        "---\nstatus: done\nbaseline_revision: story1base\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n",
    )

    def boom(*a, **k):
        raise AssertionError("stories mode must not call the mtime scan")

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", boom)
    rj = adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True)
    assert rj["status"] == "done"
    assert rj["story_key"] == "1"
    assert rj["baseline_commit"] == "story1base"  # the story spec, not the stray


def test_stories_readback_sentinel_is_blocked_escalation(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    _write_story_spec(
        tmp_path,
        "1",
        "unresolved",
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\n"
        "Status: blocked\nBlocking condition: story already blocked\n",
    )
    rj = adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True)
    assert rj is not None and rj["status"] == "blocked"
    crits = [e for e in rj["escalations"] if str(e.get("severity", "")).upper() == "CRITICAL"]
    assert crits, "a blocked sentinel must synthesize a CRITICAL escalation"


def test_stories_readback_stale_spec_below_launch_floor_returns_none(tmp_path):
    """A1: a terminal spec whose mtime predates the session launch is a stale prior
    artifact (the dev's `done` a follow-up review session re-opens), not this
    session's output — it must NOT read as completed. Mirrors the mtime-scan path's
    `since_ns` floor. Without the floor this returns `completed:done` for a review
    that produced nothing."""
    adapter, _ = make_dev_adapter(tmp_path)
    spec = _write_story_spec(
        tmp_path, "1", "foo", "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    # launch AFTER the spec was written → the spec is stale for this session
    launched = spec.stat().st_mtime_ns + 1
    handle = _dev_handle(launched_ns=launched)
    assert adapter._result_json(handle, _stories_spec(tmp_path), wait=False) is None
    # a re-write at/after the floor is this session's output → read normally
    spec.write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\nreviewed.\n",
        encoding="utf-8",
    )
    import os

    os.utime(spec, ns=(launched + 1_000, launched + 1_000))
    rj = adapter._result_json(handle, _stories_spec(tmp_path), wait=False)
    assert rj is not None and rj["status"] == "done"


def test_stories_readback_ambiguous_returns_none_without_waiting(tmp_path):
    """A2: >1 file matching `<id>-*.md` is an anomaly no wait can collapse. The
    read-back returns None promptly (rather than burning the full grace) — the
    engine's next _pick_next re-classifies AMBIGUOUS into an actionable wedge."""
    adapter, _ = make_dev_adapter(tmp_path)
    _write_story_spec(tmp_path, "1", "foo", "---\nstatus: done\n---\n\ndone\n")
    _write_story_spec(tmp_path, "1", "bar", "---\nstatus: done\n---\n\ndone\n")  # 2nd match
    start = time.monotonic()
    # wait=True would normally poll up to RESULT_GRACE_S; AMBIGUOUS must short-circuit
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True) is None
    assert time.monotonic() - start < generic.RESULT_GRACE_S / 2


def test_stories_readback_pending_returns_none(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    # no story spec on disk yet -> not terminal
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=False) is None


def test_stories_readback_non_terminal_returns_none(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    # a died-mid-flight ready-for-dev (no plan-halt) is not a terminal result
    _write_story_spec(tmp_path, "1", "foo", "---\nstatus: ready-for-dev\n---\n\nplanned only\n")
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=False) is None


def test_stories_readback_non_utf8_spec_returns_none(tmp_path):
    """synthesize_result re-reads the resolved spec as UTF-8; a binary/undecodable
    spec (or a torn glimpse of one still being written) must degrade to a
    result-less poll, never crash the read-back. resolve_story_spec classifies it
    PRESENT with status "" — so without the guard the poll dies on the very state
    the engine is designed to wedge-and-pause on at the next pick."""
    adapter, _ = make_dev_adapter(tmp_path)
    d = tmp_path / "epic" / "stories"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-slug.md").write_bytes(_BAD_UTF8)
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=False) is None


def test_stories_readback_plan_halt_is_successful_terminal(tmp_path):
    # BMAD_LOOP_PLAN_HALT flips the SAME ready-for-dev spec into a successful,
    # plan-marked terminal (the leg-1 plan is done, awaiting implementation).
    adapter, _ = make_dev_adapter(tmp_path)
    _write_story_spec(
        tmp_path,
        "1",
        "foo",
        "---\nstatus: ready-for-dev\nbaseline_revision: planbase\n---\n\nplan\n",
    )
    spec = _stories_spec(tmp_path)
    spec.env["BMAD_LOOP_PLAN_HALT"] = "1"
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj is not None
    assert rj["status"] == "ready-for-dev"
    assert rj["plan_halt"] is True
    assert rj["escalations"] == []
    assert rj["baseline_commit"] == "planbase"


def test_generic_dev_result_json_no_wait_reads_once(tmp_path, monkeypatch):
    """wait=False keeps the read-once behavior: no polling, immediate None."""
    adapter, _ = make_dev_adapter(tmp_path)
    calls = {"n": 0}

    def find(artifacts, *, since_ns):
        calls["n"] += 1
        return None

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", find)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None
    assert calls["n"] == 1


# ------------------------------- result-less-Stop diagnostics (#149)
#
# When a Stop's artifact read-back gives up empty, the adapter appends a
# {ts, verdict, detail} line to tasks/<task_id>/resultless-stops.jsonl so the
# WHY of a nudge/stall (issue #149's undiagnosable trigger) is readable
# straight from the run dir.


def _breadcrumbs(adapter, task_id="3-1-dev-1"):
    path = adapter.tasks_dir / task_id / "resultless-stops.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_resultless_stop_breadcrumb_scan_no_artifact(tmp_path, monkeypatch):
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "no-artifact"
    assert str(impl) in crumb["detail"]  # names the searched dirs


def test_resultless_stop_breadcrumb_stories_pending(tmp_path, monkeypatch):
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "pending"


def test_resultless_stop_breadcrumb_stories_ambiguous(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    _write_story_spec(tmp_path, "1", "foo", "---\nstatus: done\n---\n\ndone\n")
    _write_story_spec(tmp_path, "1", "bar", "---\nstatus: done\n---\n\ndone\n")
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "ambiguous"
    assert "2 specs" in crumb["detail"]


def test_resultless_stop_breadcrumb_stories_stale_mtime(tmp_path, monkeypatch):
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec = _write_story_spec(
        tmp_path, "1", "foo", "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    handle = _dev_handle(launched_ns=spec.stat().st_mtime_ns + 1)
    assert adapter._result_json(handle, _stories_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "stale-mtime"
    assert "predates session launch" in crumb["detail"]


def test_resultless_stop_breadcrumb_stories_not_terminal(tmp_path, monkeypatch):
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    _write_story_spec(tmp_path, "1", "foo", "---\nstatus: ready-for-dev\n---\n\nplanned only\n")
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "not-terminal"
    assert "'ready-for-dev'" in crumb["detail"]


def test_resultless_stop_breadcrumb_base_no_result_json(tmp_path):
    adapter = GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
    )
    assert adapter._await_result("3-1-dev-1", grace_s=0.0) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "no-result-json"
    assert "result.json" in crumb["detail"]


def test_resultless_stop_breadcrumb_only_on_stop_readback(tmp_path):
    """wait=False reads (the _final stall/crash re-checks) must not write
    breadcrumbs — only the Stop-event read-back diagnoses a result-less Stop."""
    adapter, _ = make_dev_adapter(tmp_path)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None
    assert adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=False) is None
    assert _breadcrumbs(adapter) == []


def test_resultless_stop_breadcrumb_write_failure_is_swallowed(tmp_path):
    """The breadcrumb is best-effort observability: an unwritable tasks dir
    must never break the completion loop."""
    adapter, _ = make_dev_adapter(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the tasks dir should be")
    adapter.tasks_dir = blocker / "tasks"  # any write under it raises OSError
    adapter._note_resultless_stop("3-1-dev-1", "pending", "detail")  # must not raise


def test_resultless_stop_breadcrumb_surrogate_detail_is_swallowed(tmp_path):
    """The other half of the same promise: a lone surrogate must not escape
    `_append_diag_jsonl` either, and it is NOT an OSError like the sibling above.

    `ensure_ascii=False` leaves the surrogate in the dumped str, so it reaches the
    UTF-8 encode inside `fh.write` as a UnicodeEncodeError — a ValueError, which
    the pre-#380 `except OSError` did not name. "/tmp/bad\\udcff.md" is exactly the
    shape a POSIX filename holding a non-UTF-8 byte takes once the filesystem API
    surrogate-escapes it, and six `_note_resultless_stop` call sites interpolate
    such a path straight into `detail`.

    NOT codec-conditional, and must never grow a locale skipif: the fault is an
    ENCODE to UTF-8, pinned by `encoding="utf-8"` in `path.open`, so a lone
    surrogate is unencodable on every host whatever that host's locale decodes.

    Ablation: narrow the guard back to `except OSError:` and this fails alone, on
    the UnicodeEncodeError escaping the writer.
    """
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._note_resultless_stop("3-1-dev-1", "pending", "/tmp/bad\udcff.md")  # must not raise

    # Dropped, not written — and that absence is the anti-vacuity half. "Did not
    # raise" alone would pass just as well if the encode had never failed; the
    # missing line is what proves the fault fired and the widened guard ate it.
    assert _breadcrumbs(adapter) == []

    # ...and the swallow left the writer usable for the next breadcrumb.
    adapter._note_resultless_stop("3-1-dev-1", "pending", "plain")
    assert [crumb["detail"] for crumb in _breadcrumbs(adapter)] == ["plain"]


def test_generic_dev_disables_nudges(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    assert adapter._stop_nudges == 0


def test_wait_for_completion_skips_transcriptless_subagent_stop(tmp_path):
    """Copilot (subagent_stop_without_transcript) fires agentStop for each subagent
    turn with an empty transcriptPath and a tool-use session id. The dev stage runs
    0 nudges, so without filtering that first subagent Stop would stall the run
    outright (the v0.7.0 Copilot regression). It must be ignored, and the main
    session's later turn-end must drive completion."""
    adapter, impl = make_dev_adapter(tmp_path, profile_name="copilot")
    assert adapter._stop_nudges == 0  # dev: a result-less *main* Stop is a real stall

    def flush_terminal_spec(call_n):
        # the spec lands only after the (ignored) subagent Stop — exactly as the main
        # session writes it on its own turn-end, not on the subagent's premature one
        if call_n == 2:
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [
            _stop_event("3-1-dev-1", "toolu_bdrk_subagent", None),  # subagent: ignored
            _stop_event("3-1-dev-1", "main-sess", "/run/events.jsonl"),  # main turn-end
        ],
        on_call=flush_terminal_spec,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "completed"
    assert result.transcript_path == "/run/events.jsonl"  # main's path, not empty
    assert result.session_id == "main-sess"  # the subagent's toolu_ id is never recorded


def test_wait_for_completion_transcriptless_stop_is_terminal_without_flag(tmp_path):
    """Gating: a profile without subagent_stop_without_transcript (claude) still
    treats every Stop as the main turn-end, so a result-less one stalls the dev
    stage (0 nudges) — the filter must not leak to other CLIs."""
    adapter, _ = make_dev_adapter(tmp_path, profile_name="claude")
    adapter._stall_grace_s = 0  # isolate the gating from the idle-grace path
    assert adapter.profile.subagent_stop_without_transcript is False
    adapter.watcher = _ScriptedWatcher([_stop_event("3-1-dev-1", "sess", None)])
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "stalled"


def test_dev_stall_grace_defaults_from_policy(tmp_path):
    # dev sessions tolerate a result-less Stop (a turn ended awaiting a background
    # process) for the policy grace; the base/non-dev adapter never does (grace 0).
    dev, _ = make_dev_adapter(tmp_path)
    assert dev._stall_grace_s == float(LimitsPolicy().dev_stall_grace_s)
    base = GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy(dev_stall_grace_s=600)),
        profile=get_profile("claude"),
    )
    assert base._stall_grace_s == 0.0


# -------------------- launch-stall + dead-window nudge parity (#470/#504)
#
# Two-way contract-parity link: tests/test_opencode_http.py carries identically
# named tests under its matching header (phase 2 adds that reciprocal link).
# Changes to the shared completion-loop behavior must update both transports or
# record the deliberate divergence.


def _frozen_stall_clock(monkeypatch):
    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # wall co-bound stays frozen
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)
    return clock


def test_dev_stall_arms_at_launch_without_stop(tmp_path, monkeypatch):
    """A silent dev/review turn is bounded even if no Stop hook ever arrives.

    Ablation target: restore launch initialization of ``stall_deadline`` and
    ``last_activity`` to ``None`` and this test times out with zero stall nudges.
    """
    mux = _UnitMux()
    adapter, _ = make_dev_adapter(tmp_path, mux=mux)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter._window_alive = lambda handle: True
    log_path = adapter.logs_dir / "3-1-dev-1.log"
    log_path.write_bytes(b"launch baseline\n")
    clock = _frozen_stall_clock(monkeypatch)

    heartbeats: list[dict] = []
    adapter._write_heartbeat = lambda task_id, payload: heartbeats.append(payload)

    def advance(call_n):
        clock["t"] += 11.0

    adapter.watcher = _ScriptedWatcher([], on_call=advance)
    spec = dataclasses.replace(_dev_spec(tmp_path), timeout_s=100.0)
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert (result.status, [text for _, text in mux.sent]) == (
        "stalled",
        [generic.STALL_NUDGE_TEXT] * 2,
    )
    assert heartbeats[0]["stall_armed"] is True


def test_dev_activity_rearms_launch_stall_grace(tmp_path, monkeypatch):
    """Two productive launch ticks re-arm grace; the first silent full grace stalls.

    Ablation target: restore a disarmed launch deadline and this no-Stop test
    reaches the wall timeout instead of tracking activity and stalling on call 3.
    """
    mux = _UnitMux()
    adapter, _ = make_dev_adapter(tmp_path, mux=mux)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0
    adapter._window_alive = lambda handle: True
    log_path = adapter.logs_dir / "3-1-dev-1.log"
    log_path.write_bytes(b"launch baseline\n")
    clock = _frozen_stall_clock(monkeypatch)

    def advance_and_stream(call_n):
        clock["t"] += 11.0
        if call_n <= 2:
            with log_path.open("ab") as stream:
                stream.write(b"productive tick\n")

    adapter.watcher = _ScriptedWatcher([], on_call=advance_and_stream)
    spec = dataclasses.replace(_dev_spec(tmp_path), timeout_s=100.0)
    result = adapter.wait_for_completion(_dev_handle(), spec)

    assert result.status == "stalled"
    assert adapter.watcher.calls == 3
    assert mux.sent == []


def test_dev_stall_nudge_send_failure_reaches_liveness_verdict(tmp_path, monkeypatch):
    """A failed launch-stall nudge defers the verdict to the next liveness probe.

    Ablation target: remove the stall-nudge ``MultiplexerError`` guard and this
    test fails on ``window gone`` before the ordinary crash path can run.
    """
    mux = _UnitMux()

    def fail_send(window_id, text):
        mux.sent.append((window_id, text))
        raise MultiplexerError("window gone")

    mux.send_text = fail_send
    adapter, _ = make_dev_adapter(tmp_path, mux=mux)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    result_reads: list[bool] = []
    real_result_json = adapter._result_json

    def spy_result_json(handle, spec, *, wait):
        result_reads.append(wait)
        return real_result_json(handle, spec, wait=wait)

    monkeypatch.setattr(adapter, "_result_json", spy_result_json)
    clock = _frozen_stall_clock(monkeypatch)
    alive = iter((True, False))
    adapter._window_alive = lambda handle: next(alive)

    def cross_first_grace(call_n):
        if call_n == 1:
            clock["t"] += 11.0

    adapter.watcher = _ScriptedWatcher([], on_call=cross_first_grace)
    result = adapter.wait_for_completion(
        _dev_handle(), dataclasses.replace(_dev_spec(tmp_path), timeout_s=100.0)
    )

    assert result.status == "crashed"
    assert mux.sent == [("@1", generic.STALL_NUDGE_TEXT)]
    assert adapter.watcher.calls == 2
    assert result_reads == [False]  # dead-window _final performed ordinary artifact read-back


def test_resultless_stop_nudge_send_failure_reaches_liveness_verdict(tmp_path, monkeypatch):
    """A failed result-less-Stop nudge also leaves liveness in charge of verdicts.

    Ablation target: remove the result-less-Stop ``MultiplexerError`` guard and
    this test fails on ``window gone`` instead of returning the crash verdict.
    """
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    mux = _UnitMux()

    def fail_send(window_id, text):
        mux.sent.append((window_id, text))
        raise MultiplexerError("window gone")

    mux.send_text = fail_send
    adapter = make_adapter(tmp_path, mux=mux)
    adapter._stop_nudges = 1
    result_reads: list[bool] = []

    def no_result(handle, spec, *, wait):
        result_reads.append(wait)
        return None

    monkeypatch.setattr(adapter, "_result_json", no_result)
    adapter._window_alive = lambda handle: False
    _frozen_stall_clock(monkeypatch)
    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl"), None]
    )

    result = adapter.wait_for_completion(
        _dev_handle(), dataclasses.replace(_dev_spec(tmp_path), timeout_s=100.0)
    )

    assert result.status == "crashed"
    assert mux.sent == [("@1", generic.NUDGE_TEXT)]
    assert adapter.watcher.calls == 2
    assert result_reads == [True, False]  # Stop await, then dead-window artifact read-back


def test_dev_result_less_stop_awaits_reinvocation_then_completes(tmp_path, monkeypatch):
    """A dev session that ends its turn awaiting a background process emits a
    result-less Stop, then a later Stop once the work lands. With grace > 0 the
    first Stop must NOT stall; the second (carrying the terminal spec) completes."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)  # don't sit out the per-Stop await
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    assert adapter._stall_grace_s > 0

    def flush_terminal_spec(call_n):
        # spec only finalizes on the second turn-end, after the background run
        if call_n == 2:
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # yielded to await bg run
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # re-invoked, finished
        ],
        on_call=flush_terminal_spec,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "completed"


def test_dev_idle_result_is_ignored_while_window_alive(tmp_path, monkeypatch):
    """A terminal artifact observed on an idle tick while the window is alive is
    advisory only — the agent may still be mid-turn (returning early would let
    run()'s finally-kill terminate it). Completion waits for the next Stop."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: True

    def flush_terminal_spec(call_n):
        if call_n == 2:  # idle tick after a result-less Stop, before final turn-end
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # arms the grace window
            None,  # idle tick: artifact on disk, window alive -> must keep waiting
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # authoritative turn-end
        ],
        on_call=flush_terminal_spec,
    )

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "completed"
    assert adapter.watcher.calls == 3  # completed on the Stop, not the idle tick


def test_dev_grace_result_does_not_complete_while_window_alive(tmp_path, monkeypatch):
    """Grace expiry under a live window must not upgrade to completed on artifact
    presence — the stall verdict stands until a Stop or window death vouches."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0
    adapter._window_alive = lambda handle: True

    clock = {"t": 1000.0}

    class _Clock:  # scoped shim so we don't mutate the real time module
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def flush_terminal_spec(call_n):
        if call_n == 2:  # artifact lands, then the grace window expires in silence
            clock["t"] += 11.0
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],
        on_call=flush_terminal_spec,
    )

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "stalled"
    assert result.result_json is None


def test_dev_window_death_with_artifact_completes(tmp_path, monkeypatch):
    """Window death is authoritative: a terminal artifact on disk when the window
    is gone upgrades the crash fallback to completed."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )

    adapter.watcher = _ScriptedWatcher([None])  # no hook event, window already gone

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "completed"
    assert result.result_json["status"] == "done"


def test_dev_stalls_when_grace_elapses_without_reinvocation(tmp_path, monkeypatch):
    """A result-less Stop with no re-invocation before the grace window elapses is
    a genuine stall — the grace must not hang until the session timeout."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0  # isolate the grace-expiry stall from the wake-nudge path
    adapter._window_alive = lambda handle: True  # window still up, just idle

    clock = {"t": 1000.0}

    class _Clock:  # scoped shim so we don't mutate the real time module
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def advance_past_grace(call_n):
        if call_n == 2:  # after the result-less Stop armed the window
            clock["t"] += 11.0

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],  # then None forever
        on_call=advance_past_grace,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "stalled"


def test_dev_grace_expiry_rechecks_liveness_and_honors_just_dead_window(tmp_path, monkeypatch):
    """A window that dies in the gap between the top-of-tick liveness probe and the
    grace-expiry stall return must flow through the crash path — window death is
    authoritative, so its just-flushed artifact is honored (completed), not
    discarded by the stall's accept_result=False."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0

    # alive at the top-of-tick probe (call 1), dead at the pre-stall re-probe (call 2)
    alive_calls = {"n": 0}

    def flaky_alive(handle):
        alive_calls["n"] += 1
        return alive_calls["n"] == 1

    adapter._window_alive = flaky_alive

    clock = {"t": 1000.0}

    class _Clock:  # scoped shim so we don't mutate the real time module
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def flush_terminal_spec(call_n):
        if call_n == 2:  # artifact lands, then the grace window expires in silence
            clock["t"] += 11.0
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],
        on_call=flush_terminal_spec,
    )

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert alive_calls["n"] == 2  # top-of-tick probe + pre-stall re-probe


def test_dev_grace_expiry_stall_recheck_transport_error_still_stalls(tmp_path, monkeypatch):
    """A transport error on the pre-stall liveness re-probe is not proof of death
    (as at the top of the tick): the verdict falls through to stalled rather than
    crashing on the hiccup."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0

    alive_calls = {"n": 0}

    def flaky_alive(handle):
        alive_calls["n"] += 1
        if alive_calls["n"] == 1:
            return True  # top-of-tick probe
        raise MultiplexerError("tmux hang")  # pre-stall re-probe

    adapter._window_alive = flaky_alive

    clock = {"t": 1000.0}

    class _Clock:  # scoped shim so we don't mutate the real time module
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def advance_past_grace(call_n):
        if call_n == 2:
            clock["t"] += 11.0

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],
        on_call=advance_past_grace,
    )

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "stalled"
    assert result.result_json is None
    assert alive_calls["n"] == 2  # probe raised on the re-check, fell through to stall


def test_dev_log_activity_keeps_grace_window_alive(tmp_path, monkeypatch):
    """A session still streaming to the tee'd pane log is working, not stalled:
    pane growth must re-arm the grace window even with no fresh Stop, so only
    genuine silence for the full grace trips a stall (the Mode-2 regression — a
    long productive turn building a diff / launching review subagents)."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0  # isolate the activity re-arm from the wake-nudge path
    adapter._window_alive = lambda handle: True

    log_path = adapter.logs_dir / "3-1-dev-1.log"
    log_path.write_bytes(b"start\n")  # baseline captured when the window arms

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def tick(call_n):
        # call 1 yields the result-less Stop that arms the window. Each later idle
        # tick advances the clock past the grace; calls 2-3 ALSO grow the pane log
        # (active -> must not stall), call 4+ stays silent (-> stall).
        if call_n >= 2:
            clock["t"] += 11.0
        if 2 <= call_n <= 3:
            with log_path.open("ab") as f:
                f.write(b"working\n")

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],  # then None forever
        on_call=tick,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    # Pre-fix this stalls at call 2; the activity re-arm carries it to the first
    # silent tick (call 4) before the genuine stall.
    assert result.status == "stalled"
    assert adapter.watcher.calls == 4


def test_dev_grace_expiry_nudges_awake_before_stalling(tmp_path, monkeypatch):
    """bmad-loop can't re-invoke a turn ended to await a background process, so an
    idle dev session is woken with up to dev_stall_nudges wake nudges on grace
    expiry before it is declared stalled (the Mode-1 fix)."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter._window_alive = lambda handle: True
    sent: list[str] = []
    adapter.send_text = lambda handle, text: sent.append(text)

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def advance(call_n):
        if call_n >= 2:  # every idle tick after the result-less Stop armed the window
            clock["t"] += 11.0

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],  # then None forever
        on_call=advance,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "stalled"
    # two wake nudges spent (silent through both grace windows), then the stall
    assert sent == [generic.STALL_NUDGE_TEXT, generic.STALL_NUDGE_TEXT]


def test_dev_stall_nudge_wakes_session_that_then_completes(tmp_path, monkeypatch):
    """A wake nudge that the session answers (a fresh Stop carrying the terminal
    spec) completes the session — the nudge served as the missing re-invocation."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter._window_alive = lambda handle: True
    sent: list[str] = []
    adapter.send_text = lambda handle, text: sent.append(text)

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def script(call_n):
        if call_n == 2:  # idle tick: push past the grace so the nudge fires
            clock["t"] += 11.0
        if call_n == 3:  # the session answered the nudge and landed its spec
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # ended turn to await bg run
            None,  # idle gap -> grace expires -> wake nudge
            _stop_event("3-1-dev-1", "sess", "/run/events.jsonl"),  # woke, finished
        ],
        on_call=script,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "completed"
    assert sent == [generic.STALL_NUDGE_TEXT]  # one nudge was enough to wake it


def _capped_spec(tmp_path, cap: int) -> SessionSpec:
    """A workflow-session spec: same shape as _dev_spec but with the monotonic
    stall-nudge cap the engine sets for injected plugin workflows."""
    return SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/tea-automate 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
        stall_nudges_cap=cap,
    )


def _stall_loop_adapter(tmp_path, monkeypatch):
    """Adapter + clock + sent-nudge recorder for driving the refill loop: a
    session that answers every wake nudge with a fresh result-less Stop."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter._window_alive = lambda handle: True
    sent: list[str] = []
    adapter.send_text = lambda handle, text: sent.append(text)

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)
    return adapter, impl, clock, sent


def test_workflow_cap_bounds_refilled_stall_nudges(tmp_path, monkeypatch):
    """The completion-signal livelock: a session that answers every wake nudge
    with a fresh result-less Stop gets its per-silence budget refilled each time
    and can ride the loop until session timeout. A capped spec (what the engine
    sets for injected workflow sessions) bounds the TOTAL nudges ever sent:
    exactly cap sends, then stalled."""
    adapter, _, clock, sent = _stall_loop_adapter(tmp_path, monkeypatch)

    def advance(call_n):
        if call_n >= 2:
            clock["t"] += 11.0

    stop = _stop_event("3-1-dev-1", "sess", "/run/events.jsonl")
    adapter.watcher = _ScriptedWatcher(
        # each None is an idle tick past the grace -> a nudge; each fresh Stop is
        # the session answering result-less -> the per-silence budget refills
        [stop, None, stop, None, stop, None],
        on_call=advance,
    )
    result = adapter.wait_for_completion(_dev_handle(), _capped_spec(tmp_path, cap=2))
    assert result.status == "stalled"
    assert sent == [generic.STALL_NUDGE_TEXT] * 2


def test_uncapped_spec_keeps_refilling_nudges_past_cap(tmp_path, monkeypatch):
    """cap=None (the raw SessionSpec default — the engine now caps every
    session it drives, dev/review included) preserves the uncapped adapter
    contract byte-identical: every fresh Stop restores the budget and nudging
    continues past any cap, bounded only by spec.timeout_s."""
    adapter, _, clock, sent = _stall_loop_adapter(tmp_path, monkeypatch)

    def advance(call_n):
        if call_n >= 2:
            clock["t"] += 11.0

    stop = _stop_event("3-1-dev-1", "sess", "/run/events.jsonl")
    adapter.watcher = _ScriptedWatcher(
        [stop, None, stop, None, stop, None],  # then None forever
        on_call=advance,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "stalled"
    # one nudge per refilled silence cycle, then the final budget (2) drains in
    # genuine silence: 4 total sends, strictly more than a cap of 2 would allow
    assert sent == [generic.STALL_NUDGE_TEXT] * 4


def test_capped_session_still_completes_when_marker_lands_late(tmp_path, monkeypatch):
    """Exhausting the cap must not discard a session whose completion marker
    lands afterwards: the marker plus its turn-end Stop still complete the
    session (a bare marker under a live window is advisory — only the Stop,
    the authoritative signal, seals it)."""
    adapter, impl, clock, sent = _stall_loop_adapter(tmp_path, monkeypatch)

    def script(call_n):
        if call_n >= 2:
            clock["t"] += 11.0
        if call_n == 4:  # after the cap was spent: the marker finally lands
            (impl / "bmad-dev-auto-result-3-1-tea.automate-1.md").write_text(
                "---\nstatus: done\n---\n"
            )

    stop = _stop_event("3-1-dev-1", "sess", "/run/events.jsonl")
    adapter.watcher = _ScriptedWatcher(
        [stop, None, stop, stop],  # nudge -> answered result-less -> final turn-end
        on_call=script,
    )
    result = adapter.wait_for_completion(_dev_handle(), _capped_spec(tmp_path, cap=1))
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert sent == [generic.STALL_NUDGE_TEXT]  # the cap was already exhausted


# ---------------------- timeout instrumentation + wall-clock co-bound (#157)
#
# The #157 timeout fired with zero record of when the adapter declared it, and
# a host suspend (macOS sleep) freezing time.monotonic() could silently extend
# the deadline by the nap's length. The fire moment now stamps the result and
# a session-lifecycle.jsonl line, a wall-clock co-bound fires through a frozen
# monotonic clock (but may never EXTEND the deadline), and each tick tops up a
# throttled heartbeat.json whose staleness diagnoses a frozen orchestrator.
#
# Contract parity: test_opencode_http.py holds identically named tests over the
# HTTP transport. A behavior change here must land in both or record the
# divergence.


def _timeout_clock_adapter(tmp_path, monkeypatch):
    """Adapter + independently steerable monotonic/wall clocks for driving the
    timeout-fire path. The window stays alive and no hook event ever arrives,
    so only a clock crossing its deadline can end the wait."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: True

    clock = {"mono": 1000.0, "wall": 5000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["mono"])
        time = staticmethod(lambda: clock["wall"])
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)
    return adapter, clock


def _short_spec(tmp_path, timeout_s=30.0) -> SessionSpec:
    return SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
        timeout_s=timeout_s,
    )


def test_timeout_monotonic_expiry_is_instrumented(tmp_path, monkeypatch):
    """A plain monotonic expiry records WHEN and BY WHICH CLOCK the deadline
    was declared elapsed: fields on the result plus exactly one timeout-fired
    line in session-lifecycle.jsonl."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)

    def advance(call_n):
        clock["mono"] += 11.0  # wall frozen: only the monotonic clock expires

    adapter.watcher = _ScriptedWatcher([], on_call=advance)
    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))
    assert result.status == "timeout"
    assert result.timeout_expired_clock == "monotonic"
    assert result.timeout_fired_at == 5000.0  # the fake wall clock at fire time
    fired = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "timeout-fired"]
    assert len(fired) == 1
    assert fired[0]["expired_clock"] == "monotonic"
    assert fired[0]["timeout_s"] == 30.0
    assert fired[0]["mono_remaining_s"] <= 0


def test_timeout_fires_on_wall_clock_when_monotonic_frozen(tmp_path, monkeypatch):
    """The #157 suspend signature: time.monotonic() stands still through a host
    suspend, so the monotonic deadline alone would stretch the session by the
    nap's length. The wall-clock co-bound fires anyway, and the wall-only
    expiry (monotonic time still to spare) is stamped as the evidence."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)

    def advance(call_n):
        clock["wall"] += 11.0  # suspended host: wall counts on, monotonic frozen

    adapter.watcher = _ScriptedWatcher([], on_call=advance)
    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))
    assert result.status == "timeout"
    assert result.timeout_expired_clock == "wall"
    (fired,) = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "timeout-fired"]
    assert fired["expired_clock"] == "wall"
    assert fired["mono_remaining_s"] == 30.0  # the frozen clock never advanced


def test_timeout_wall_clock_step_back_cannot_extend_deadline(tmp_path, monkeypatch):
    """The co-bound may only EXPIRE the deadline, never stretch it: a wall
    clock stepped backward (an NTP correction) leaves the monotonic expiry on
    its original schedule."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)

    def advance(call_n):
        clock["mono"] += 11.0
        clock["wall"] -= 3600.0  # NTP step-back: must change nothing

    adapter.watcher = _ScriptedWatcher([], on_call=advance)
    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))
    assert result.status == "timeout"
    assert result.timeout_expired_clock == "monotonic"
    assert adapter.watcher.calls == 3  # same tick count as an untouched wall clock


def test_heartbeat_written_and_throttled(tmp_path, monkeypatch):
    """Each tick tops up tasks/<id>/heartbeat.json with the loop's view of the
    session — but at most once per HEARTBEAT_INTERVAL_S: two ticks inside one
    interval produce one write."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    (adapter.tasks_dir / "3-1-dev-1").mkdir()  # start_session creates it in production

    writes: list[dict] = []
    real_write = adapter._write_heartbeat

    def spy(task_id, payload):
        writes.append(payload)
        real_write(task_id, payload)

    adapter._write_heartbeat = spy

    def advance(call_n):
        if call_n == 1:
            clock["mono"] += 1.0  # next tick lands inside the same interval
        elif call_n == 2:
            clock["mono"] += generic.HEARTBEAT_INTERVAL_S + 10.0  # crosses it
        else:
            clock["mono"] += 1000.0  # past spec.timeout_s: end the loop

    adapter.watcher = _ScriptedWatcher([], on_call=advance)
    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path, timeout_s=100.0))
    assert result.status == "timeout"
    assert writes[0] == {
        "ts": 5000.0,
        "remaining_s": 100.0,
        "stall_armed": True,
        "stall_nudges_sent": 0,
    }
    assert [w["remaining_s"] for w in writes] == [100.0, 59.0]  # tick 2 was throttled
    hb = json.loads((adapter.tasks_dir / "3-1-dev-1" / "heartbeat.json").read_text())
    assert hb == writes[-1]  # the on-disk file is the last overwrite


def test_lifecycle_and_heartbeat_write_failure_is_swallowed(tmp_path):
    """Like the resultless-stop breadcrumb: pure observability, so an
    unwritable tasks dir must never break the completion loop."""
    adapter, _ = make_dev_adapter(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the tasks dir should be")
    adapter.tasks_dir = blocker / "tasks"  # any write under it raises OSError
    adapter._note_lifecycle("3-1-dev-1", "timeout-fired", expired_clock="wall")  # must not raise
    adapter._write_heartbeat("3-1-dev-1", {"ts": 0.0})  # must not raise


# ------------------------------ in-session hard-stop poll (#319)
#
# `bmad-loop stop` lodges a mode-aware stop-request.json before it signals, so a
# stop reaches a session on platforms where the engine's SIGTERM never arrives.
# The wait loop reads that file once per iteration and returns the non-completion
# `aborted` verdict; the engine raises RunStopped off it. The adapter never
# unlinks the file — the engine consumes it, and must still see it to attribute
# the stop.
#
# Contract parity: test_opencode_http.py holds the same pair over the HTTP
# transport. A behavior change here must land in both or record the divergence.


def _lodge_stop_request(adapter, mode: str) -> Path:
    """Lodge a stop request of ``mode`` on this run's control-file channel, as
    ``bmad-loop stop`` does. Written directly rather than through
    ``runs._write_stop_request`` so the adapter's read stays pinned to the
    on-disk shape, not to the writer's guards."""
    adapter.run_dir.mkdir(parents=True, exist_ok=True)
    path = adapter.run_dir / runs.STOP_REQUEST_FILE
    path.write_text(
        json.dumps({"requested_at": "2026-08-22T00:00:00", "mode": mode}), encoding="utf-8"
    )
    return path


def test_wait_aborts_on_hard_stop_request(tmp_path, monkeypatch):
    """A hard stop pending on the channel ends the wait on its very next
    iteration with the non-completion ``aborted`` verdict — no artifact
    read-back (that rescue is `_post_kill_reconcile`'s job) and no timeout burn.

    The abort fires before the loop ever reaches its event source, so the
    steerable clock never advances: the pass is deterministic, not a race.

    Ablation: delete the `_hard_stop_requested()` arm from `wait_for_completion`
    and the clock runs the session to its scripted `timeout` verdict instead —
    proven red once, then restored.
    """
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    request = _lodge_stop_request(adapter, "hard")

    def advance(call_n):
        clock["mono"] += 11.0  # only reached if the abort arm is gone

    adapter.watcher = _ScriptedWatcher([], on_call=advance)

    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))

    assert result.status == "aborted"
    assert result.result_json is None  # an abort is never a completion path
    assert adapter.watcher.calls == 0  # aborted before the first event wait
    fired = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "stop-abort-fired"]
    assert len(fired) == 1
    # The engine consumes the request when it raises; an adapter that unlinked it
    # would leave the engine unable to attribute the stop.
    assert request.is_file()


def test_wait_polls_the_hard_stop_channel_after_the_event_wait_too(tmp_path, monkeypatch):
    """The arm at the top of the loop is not enough by itself. Between it and its
    next run sit the loop's own 5s wait *and* whichever dispatch leg the event
    selects — a `_window_alive` or `send_text` bounded only by TMUX_TIMEOUT_S (30s),
    or a `_result_json(wait=True)` that waits RESULT_GRACE_S (15s) for an artifact.
    That last one outlasts `stop_run`'s 10s grace window on a perfectly healthy box.
    Polling again straight after the wait leaves at most one leg between two checks.

    The request is lodged *during* the event wait, so it is absent at the
    top-of-loop check and present immediately after — the exact interval this
    second poll exists to cover.

    It does not make the interval unconditionally short, and the prose no longer
    claims it does: an in-flight subprocess cannot be interrupted from this thread,
    so a leg that outlasts the window still degrades to the force-kill backstop.

    Ablation: delete the second `_hard_stop_requested()` arm (the one just below
    `watcher.wait_for`) -> `_window_alive` is called, because the loop enters the
    `event is None` dispatch leg and only notices the request on its next
    iteration. The verdict stays `aborted` either way, which is exactly why the
    dispatch spy is the assertion carrying the proof and the status is not."""
    adapter, _clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    alive_calls: list[int] = []
    adapter._window_alive = lambda handle: (alive_calls.append(1), True)[1]

    lodged: list[str] = []

    def _lodge_during_the_wait(_call_n):
        if not lodged:
            lodged.append("hard")
            _lodge_stop_request(adapter, "hard")

    adapter.watcher = _ScriptedWatcher([], on_call=_lodge_during_the_wait)

    result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))

    assert result.status == "aborted"
    assert lodged == ["hard"]  # the interleave really happened
    assert adapter.watcher.calls == 1  # caught on the same iteration, not the next
    assert alive_calls == []  # never entered the dispatch leg below the wait


def _lodge_owner_stop_request(tmp_path, mode: str) -> Path:
    """Lodge a stop request in a *different* run dir and publish it as the owning
    run, the way `stop <parent-id>` reaches a nested auto-sweep child: the request
    lands in the parent's dir while the child's own stays empty."""
    owner = tmp_path / ".bmad-loop" / "runs" / "parent-run"
    owner.mkdir(parents=True, exist_ok=True)
    (owner / runs.STOP_REQUEST_FILE).write_text(
        json.dumps({"requested_at": "2026-08-22T00:00:00", "mode": mode}), encoding="utf-8"
    )
    return owner


def test_wait_aborts_on_owning_runs_hard_stop_request(tmp_path, monkeypatch):
    """A nested auto-sweep child aborts on the *parent's* hard request (#319).

    The child mints its own run dir, so `stop <parent-id>` writes a file this
    adapter would otherwise never read — and on native Windows, where the shared
    SIGTERM cannot land, that left the parent stop force-killing blind. The poll now
    reads the owning run's channel too.

    Ablation: drop the owner leg from `_hard_stop_requested` and the clock runs the
    session to its scripted `timeout` verdict instead."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    owner = _lodge_owner_stop_request(tmp_path, "hard")
    assert not (adapter.run_dir / runs.STOP_REQUEST_FILE).exists()  # child's own is empty

    def advance(call_n):
        clock["mono"] += 11.0  # only reached if the owner leg is gone

    adapter.watcher = _ScriptedWatcher([], on_call=advance)

    token = runs.set_owner_run_dir(owner)
    try:
        result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))
    finally:
        runs.reset_owner_run_dir(token)

    assert result.status == "aborted"
    assert result.result_json is None
    assert adapter.watcher.calls == 0
    # The child never consumes the parent's file — the parent's own hard arm must
    # still find it to record and attribute the stop.
    assert (owner / runs.STOP_REQUEST_FILE).is_file()


def test_wait_ignores_owning_runs_graceful_stop_request(tmp_path, monkeypatch):
    """The mode-exact twin of the owner leg: graceful already suppresses a child
    sweep from *starting*, and letting one already in flight finish is what graceful
    means — so a graceful request on the owning run must not abort this session.

    Ablation: widen the owner leg to `is not None` and this reddens alone."""
    adapter, clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    owner = _lodge_owner_stop_request(tmp_path, "graceful")

    def advance(call_n):
        clock["mono"] += 11.0

    adapter.watcher = _ScriptedWatcher([], on_call=advance)

    token = runs.set_owner_run_dir(owner)
    try:
        result = adapter.wait_for_completion(_dev_handle(), _short_spec(tmp_path))
    finally:
        runs.reset_owner_run_dir(token)

    assert result.status == "timeout"  # ran on, exactly as with no request at all


def test_hard_stop_requested_falls_back_to_own_dir_outside_any_run(tmp_path, monkeypatch):
    """With no owning run published — a standalone adapter, as in probes and most
    tests — the predicate is its own dir alone, and answers without raising."""
    adapter, _clock = _timeout_clock_adapter(tmp_path, monkeypatch)
    assert runs.owner_run_dir() is None
    assert adapter._hard_stop_requested() is False
    _lodge_stop_request(adapter, "hard")
    assert adapter._hard_stop_requested() is True


def test_wait_ignores_graceful_stop_request(tmp_path, monkeypatch):
    """Graceful means *finish the in-flight item*, so a graceful request pending
    on the same channel must not touch a running session — only ``hard`` aborts.
    Since every pre-#319 (modeless) body reads graceful, this is the back-compat
    pin for the in-session poll as well.

    Ablation: widen the adapter's check to any pending request (drop the
    ``== "hard"`` comparison in `_hard_stop_requested`) and this test reddens
    with an `aborted` verdict.
    """
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: True
    _lodge_stop_request(adapter, "graceful")
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    adapter.watcher = _ScriptedWatcher([_stop_event("3-1-dev-1", "sess", "/t.jsonl")])

    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert not _lifecycle_lines(adapter) or not [
        ln for ln in _lifecycle_lines(adapter) if ln["event"] == "stop-abort-fired"
    ]


# ------------------------------ mid-session token-budget guard (#158)
#
# The wait loop samples cumulative weighted usage on the heartbeat cadence and
# trips AT MOST ONCE per session on crossing spec.token_budget: warn =
# ATTENTION + lifecycle breadcrumb only; enforce = wrap-up nudge + a monotonic
# grace window, then an over_budget exit that never accepts an on-disk
# artifact under a live window. Driven with a scripted watcher, a steerable
# clock (ticks advance past HEARTBEAT_INTERVAL_S to cross the throttle), and a
# real claude-jsonl transcript file.
#
# Contract parity: test_opencode_http.py holds identically named tests over the
# HTTP transport. A behavior change here must land in both or record the
# divergence.


def _write_claude_transcript(path: Path, input_tokens: int) -> None:
    entry = {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        },
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _budget_adapter(tmp_path, monkeypatch, usage_parser="claude-jsonl"):
    """Base adapter (result.json contract) + steerable clock + recorded nudges.
    Desktop notifications are off so gates.notify only appends the ATTENTION
    file under the run dir."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    profile = dataclasses.replace(get_profile("claude"), usage_parser=usage_parser)
    adapter = GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy(), notify=NotifyPolicy(desktop=False, file=True)),
        profile=profile,
    )
    adapter._window_alive = lambda handle: True
    sent: list[str] = []
    adapter.send_text = lambda handle, text: sent.append(text)
    (adapter.tasks_dir / "b-1").mkdir()

    # wall starts frozen at 0 (the session's #157 co-bound never fires) but is
    # steerable so the budget-grace wall co-bound can be driven independently.
    clock = {"t": 1000.0, "wall": 0.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: clock["wall"])
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)
    return adapter, clock, sent


def _budget_handle() -> SessionHandle:
    return SessionHandle(task_id="b-1", native_id="@1", launched_ns=0)


def _budget_spec(tmp_path, mode="enforce", budget=1000, grace_s=50.0, timeout_s=100_000.0):
    return SessionSpec(
        task_id="b-1",
        role="dev",
        prompt="p",
        cwd=tmp_path,
        timeout_s=timeout_s,
        token_budget=budget,
        token_budget_mode=mode,
        token_budget_grace_s=grace_s,
        cache_read_weight=0.1,
    )


def _start_event(transcript_path):
    return HookEvent(
        ts=1,
        event="SessionStart",
        task_id="b-1",
        session_id="sess",
        transcript_path=str(transcript_path),
        path=Path("x"),
    )


def _advance_31(clock):
    """on_call hook: every tick after the SessionStart crosses the heartbeat
    throttle, so each watcher call is one sampling opportunity."""

    def advance(call_n):
        if call_n >= 2:
            clock["t"] += 31.0

    return advance


def test_budget_warn_trips_once_and_session_completes(tmp_path, monkeypatch):
    """Warn mode: one ATTENTION line + one budget-tripped breadcrumb, no nudge,
    no termination — the session runs to its natural end with budget_weighted
    on the result. The latch stops all further sampling."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    samples: list[str] = []
    real_tally = generic.tally_usage

    def spying_tally(parser, path):
        samples.append(str(path))
        return real_tally(parser, path)

    monkeypatch.setattr(generic, "tally_usage", spying_tally)

    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(_budget_handle(), _budget_spec(tmp_path, mode="warn"))

    assert result.status == "completed"
    assert result.result_json == {"ok": True}
    assert result.budget_weighted == 5000
    assert sent == []  # warn mode: no nudge
    assert samples == [str(transcript)]  # latched after the trip: no re-sampling
    attention = (adapter.run_dir / "ATTENTION").read_text(encoding="utf-8")
    assert len(attention.splitlines()) == 1
    tripped = [ln for ln in _lifecycle_lines(adapter, "b-1") if ln["event"] == "budget-tripped"]
    assert len(tripped) == 1
    assert tripped[0]["weighted"] == 5000
    assert tripped[0]["budget"] == 1000
    assert tripped[0]["mode"] == "warn"


def test_budget_enforce_nudges_then_terminates_over_budget(tmp_path, monkeypatch):
    """Enforce mode: BUDGET_NUDGE_TEXT at trip, then grace expiry under a live
    window ends over_budget WITHOUT accepting the on-disk artifact (#48/#53)."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)
    # a result on disk must NOT upgrade the over_budget exit: live-window distrust
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=50.0)
    )

    assert result.status == "over_budget"
    assert result.result_json is None
    assert result.budget_weighted == 5000
    assert sent == [generic.BUDGET_NUDGE_TEXT]
    attention = (adapter.run_dir / "ATTENTION").read_text(encoding="utf-8")
    assert len(attention.splitlines()) == 1
    # the verdict leaves a breadcrumb, like timeout-fired (#157 forensics)
    fired = [ln for ln in _lifecycle_lines(adapter, "b-1") if ln["event"] == "over-budget-fired"]
    assert len(fired) == 1
    assert fired[0]["weighted"] == 5000
    assert fired[0]["budget"] == 1000
    assert fired[0]["zero_grace"] is False


def test_budget_enforce_completion_within_grace_completes(tmp_path, monkeypatch):
    """A Stop with a result inside the grace window completes the session
    normally — budget_weighted still rides the completed result."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=100.0)
    )

    assert result.status == "completed"
    assert result.result_json == {"ok": True}
    assert result.budget_weighted == 5000
    assert sent == [generic.BUDGET_NUDGE_TEXT]


def test_budget_enforce_zero_grace_is_immediate_no_nudge(tmp_path, monkeypatch):
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)

    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=0.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5000
    assert sent == []  # zero grace: terminate at trip, no wrap-up nudge
    fired = [ln for ln in _lifecycle_lines(adapter, "b-1") if ln["event"] == "over-budget-fired"]
    assert len(fired) == 1
    assert fired[0]["zero_grace"] is True


def test_budget_grace_expiry_reprobes_liveness_dead_window_is_crashed(tmp_path, monkeypatch):
    """Window death at grace expiry is authoritative: the existing crashed path
    (artifact honored) wins over the over_budget verdict."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)

    alive_calls = {"n": 0}

    def flaky_alive(handle):
        alive_calls["n"] += 1
        return alive_calls["n"] <= 3  # alive through the grace, dead at the expiry re-probe

    adapter._window_alive = flaky_alive
    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=50.0)
    )

    assert result.status == "crashed"
    assert result.budget_weighted == 5000


def test_budget_timeout_after_trip_carries_weighted(tmp_path, monkeypatch):
    """budget_weighted rides every post-trip exit — here the session times out
    inside a still-open grace window."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)

    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(),
        _budget_spec(tmp_path, mode="enforce", grace_s=1_000_000.0, timeout_s=100.0),
    )

    assert result.status == "timeout"
    assert result.budget_weighted == 5000
    assert sent == [generic.BUDGET_NUDGE_TEXT]


def test_budget_parser_none_is_inert(tmp_path, monkeypatch):
    """No usage signal (usage_parser \"none\") leaves the guard inert whatever
    the mode: the session never trips and completes normally."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch, usage_parser="none")
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=10_000_000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(_budget_handle(), _budget_spec(tmp_path, mode="enforce"))

    assert result.status == "completed"
    assert result.budget_weighted is None
    assert sent == []
    assert not (adapter.run_dir / "ATTENTION").exists()


def test_budget_mode_off_never_samples(tmp_path, monkeypatch):
    """Mode off: zero sampling — the transcript is never read despite huge
    usage, and behavior is byte-identical to today."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=10_000_000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    samples: list[str] = []
    monkeypatch.setattr(generic, "tally_usage", lambda parser, path: samples.append(str(path)))

    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(_budget_handle(), _budget_spec(tmp_path, mode="off"))

    assert result.status == "completed"
    assert result.budget_weighted is None
    assert samples == []  # the guard never read the transcript
    assert sent == []
    assert not (adapter.run_dir / "ATTENTION").exists()


def test_budget_sampling_oserror_is_inert(tmp_path, monkeypatch):
    """A failing usage read must never break the wait loop: the sample reads as
    None and the guard skips the tick."""
    adapter, _, _ = _budget_adapter(tmp_path, monkeypatch)

    def boom(parser, path):
        raise OSError("unreadable transcript")

    monkeypatch.setattr(generic, "tally_usage", boom)
    assert adapter._sample_weighted_usage("/t.jsonl", _budget_spec(tmp_path)) is None


def test_budget_sampling_survives_torn_transcript(tmp_path, monkeypatch):
    """The transcript is a LIVE file being appended mid-turn: a flush boundary
    can split a multibyte UTF-8 character, and the torn read raises
    UnicodeDecodeError (a ValueError, NOT an OSError). The sample tick must go
    inert — never crash the wait loop — and the session completes normally."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    entry = json.dumps({"message": {"usage": {"input_tokens": 5000}}})
    # valid entry, then a truncated multibyte sequence at the flush boundary
    transcript.write_bytes(entry.encode("utf-8") + b"\n\xe2\x82")
    assert adapter._sample_weighted_usage(str(transcript), _budget_spec(tmp_path)) is None

    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')
    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(_budget_handle(), _budget_spec(tmp_path, mode="enforce"))

    assert result.status == "completed"
    assert result.budget_weighted is None  # every sample tick was inert
    assert sent == []
    assert not (adapter.run_dir / "ATTENTION").exists()


def test_budget_nudge_send_failure_still_arms_grace(tmp_path, monkeypatch):
    """A dead/hung window can reject the wrap-up nudge (tmux send-keys
    raises); the trip must survive it and the grace still arm — the verdict
    then follows the normal paths (here: grace expiry under a live window)."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)

    def boom(handle, text):
        raise MultiplexerError("window gone")

    adapter.send_text = boom
    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=50.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5000


def test_budget_notify_failure_does_not_break_trip(tmp_path, monkeypatch):
    """observe-degrade: an ATTENTION append failure (disk full, perms) degrades
    to a missing notification; the trip itself and the session proceed."""
    from bmad_loop import gates as gates_mod

    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gates_mod, "notify", boom)
    adapter.watcher = _ScriptedWatcher(
        [_start_event(transcript), None, _stop_event("b-1", "sess", str(transcript))],
        on_call=_advance_31(clock),
    )
    result = adapter.wait_for_completion(_budget_handle(), _budget_spec(tmp_path, mode="warn"))

    assert result.status == "completed"
    assert result.budget_weighted == 5000  # the trip proceeded past the failed notify
    tripped = [ln for ln in _lifecycle_lines(adapter, "b-1") if ln["event"] == "budget-tripped"]
    assert len(tripped) == 1


def test_budget_grace_fires_on_wall_clock_when_monotonic_frozen(tmp_path, monkeypatch):
    """The #157 suspend signature on the budget grace: a host suspend freezes
    time.monotonic(), silently stretching the 'bounded' wrap-up window. The
    wall-clock co-bound expires it anyway."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)

    def advance(call_n):
        if call_n in (2, 3):
            clock["t"] += 31.0  # reach the sampling heartbeat: the trip arms the grace
        elif call_n >= 4:
            clock["wall"] += 31.0  # suspended host: wall counts on, monotonic frozen

    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=advance)
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=50.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5000
    assert sent == [generic.BUDGET_NUDGE_TEXT]


def test_budget_zero_grace_dead_window_takes_crash_path(tmp_path, monkeypatch):
    """A trip coinciding with window death must not discard a landed artifact
    just because grace is 0: the zero-grace exit re-probes liveness and routes
    a dead window through the crash path, which honors the artifact."""
    adapter, clock, sent = _budget_adapter(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    _write_claude_transcript(transcript, input_tokens=5000)
    (adapter.tasks_dir / "b-1" / "result.json").write_text('{"ok": true}')

    alive_calls = {"n": 0}

    def flaky_alive(handle):
        alive_calls["n"] += 1
        return alive_calls["n"] == 1  # alive at the first idle-tick probe, dead at the trip

    adapter._window_alive = flaky_alive
    adapter.watcher = _ScriptedWatcher([_start_event(transcript)], on_call=_advance_31(clock))
    result = adapter.wait_for_completion(
        _budget_handle(), _budget_spec(tmp_path, mode="enforce", grace_s=0.0)
    )

    assert result.status == "completed"  # crash path honored the artifact
    assert result.result_json == {"ok": True}
    assert result.budget_weighted == 5000
    assert sent == []


# ----------------------------------------------- post-kill reconcile (#61)
#
# A session that finished its work but lost its final Stop ends "stalled"
# (nudge-unresponsive under a live window), or "timeout" when no hook event
# ever arrived (total hook loss never arms the stall grace). Both verdicts
# discard the on-disk result — correctly, at verdict time, because the window
# was alive to distrust. run()'s finally-kill settles that question:
# _post_kill_reconcile re-probes and, on a provably dead window, re-runs the
# read-back and rescues a self-consistent successful terminal. These drive the
# hook in isolation, plus through run() for the kill-before-scan ordering.

_DONE_SPEC = (
    "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
    "## Auto Run Result\n\nStatus: done\nImplemented.\n"
)


def _unvouched(status="stalled", **extra) -> SessionResult:
    return SessionResult(status=status, session_id="sess", transcript_path="/t.jsonl", **extra)


def test_post_kill_reconcile_rescues_consistent_done_artifact(tmp_path):
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched())
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True
    # the stall verdict's identity is preserved on the rescued result
    assert result.session_id == "sess"
    assert result.transcript_path == "/t.jsonl"


def test_post_kill_reconcile_rescues_timeout(tmp_path):
    """Total hook loss (misconfigured hooks, events-dir write failure) never arms
    the stall grace — the session exits `timeout` with no artifact check at all.
    The same post-kill rescue must cover it — upgrading the outcome, not the
    timing evidence: the fired-deadline stamps survive the rescue (#157)."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    original = _unvouched("timeout", timeout_fired_at=1234.5, timeout_expired_clock="wall")
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original)
    assert result.status == "completed"
    assert result.result_json["post_kill_reconciled"] is True
    assert result.timeout_fired_at == 1234.5
    assert result.timeout_expired_clock == "wall"


def test_post_kill_reconcile_rescues_over_budget(tmp_path):
    """over_budget joins the rescue set (#158): a terminal artifact the wrap-up
    nudge flushed at kill-time is honored once the window is provably dead —
    the tripped budget's sample survives the upgrade."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    original = _unvouched("over_budget", budget_weighted=5_000_000)
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original)
    assert result.status == "completed"
    assert result.result_json["post_kill_reconciled"] is True
    assert result.budget_weighted == 5_000_000


def test_post_kill_reconcile_leaves_other_statuses_alone(tmp_path):
    """completed and crashed already had their artifact read at verdict time;
    the hook must not touch them (nor re-scan for a completed result)."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    for status in ("completed", "crashed"):
        original = _unvouched(status)
        assert (
            adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original
        )
    # pins the base.py cross-claim that `session_vanished` rides only verdicts
    # this hook never rebuilds: if `crashed` ever joins the rescue set, this
    # rescuable artifact would produce a rebuilt result without the flag.
    vanished = _unvouched("crashed", session_vanished=True)
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), vanished) is vanished


def test_post_kill_reconcile_keeps_stall_when_window_alive_after_kill(tmp_path):
    """kill_window is best-effort; a window that survived it is still live, so the
    live-window invariant (#48/#53) still applies — no rescue."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: True
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    original = _unvouched()
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original)
    assert result is original
    assert result.status == "stalled"
    assert result.result_json is None


def test_post_kill_reconcile_probe_error_keeps_stall(tmp_path):
    """A transport failure on the post-kill probe means liveness is unknowable —
    and unknown is not dead (tri-state): never upgrade on a guess."""
    adapter, impl = make_dev_adapter(tmp_path)

    def boom(handle):
        raise MultiplexerError("tmux hang")

    adapter._window_alive = boom
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_inconsistent_status_keeps_stall(tmp_path):
    """Frontmatter and prose actively disagreeing is exactly the low-trust state
    the stricter-than-crash gate exists for: keep the stall verdict."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: in-progress\n"
    )
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_blocked_artifact_keeps_stall(tmp_path):
    """A blocked terminal carries no finished work to preserve, and blocked-plus-
    nudge-unresponsive is weak evidence — not rescued."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nStuck.\n"
    )
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_no_artifact_keeps_stall(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_rescues_aborted(tmp_path):
    """An operator's hard stop (#319) kills the window mid-wait, so a Stop event
    that had already landed is never read — the same lost-vouching problem
    `stalled` and `timeout` have, settled by the same trust model.

    The upgrade to `completed` does NOT resume the run: the engine re-reads the
    hard-stop file after saving the rescued session and stops there. So this
    rescue records the finished work *and* the stop is still honored — the pair
    is pinned engine-side by
    `test_engine.py::test_hard_stop_after_completed_session_stops_before_next_leg`.

    Ablation: drop `"aborted"` from the rescue tuple and the verdict stands
    unrescued.
    """
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched("aborted"))
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True
    # the abort verdict's identity is preserved on the rescued result
    assert result.session_id == "sess"
    assert result.transcript_path == "/t.jsonl"


def test_post_kill_reconcile_ignores_pre_launch_artifact(tmp_path):
    """The launch floor still applies: a terminal spec predating this session is a
    stale prior artifact, not evidence this session finished."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_DONE_SPEC)
    handle = _dev_handle(launched_ns=spec_file.stat().st_mtime_ns + 1)
    original = _unvouched()
    assert adapter._post_kill_reconcile(handle, _dev_spec(tmp_path), original) is original


# ---- corrupt / unreadable artifacts: the rescue must never make things worse.
#
# The hook is the one path guaranteed to read a file immediately after run()'s
# finally-kill — precisely when a spec the CLI was mid-write is truncated, quite
# possibly through a multi-byte UTF-8 sequence. An escaping exception is NOT
# contained per-task: it unwinds past adapter.run() to the engine's broad
# `except Exception`, which marks the whole RUN crashed and abandons every
# remaining story. So a read fault keeps the original verdict, like every other
# keep-verdict branch.


def test_post_kill_reconcile_synth_read_error_keeps_stall(tmp_path, monkeypatch):
    """The load-bearing guard, pinned independently of devcontract's internals:
    whatever the read-back raises, the hook returns the verdict it was given.
    OSError and UnicodeDecodeError share no base class below Exception."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    for exc in (OSError("I/O error"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):

        def raising(handle, spec, *, wait, dead_window=False, _exc=exc):
            raise _exc

        monkeypatch.setattr(adapter, "_synth_result", raising)
        original = _unvouched()
        assert (
            adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original
        )


def test_post_kill_reconcile_non_utf8_scan_artifact_keeps_stall(tmp_path):
    """A truncated/binary `spec-*.md` on the mtime-scan path: find_result_artifact
    reads it to check for a terminal section, and its `except OSError` never
    catches a decode error."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_bytes(_BAD_UTF8)
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_non_utf8_fallback_marker_keeps_stall(tmp_path):
    """The no-spec fallback marker is matched by NAME, so the finder hands it back
    without ever reading it — the decode fault lands in synthesize_result instead.
    This is the artifact an injected-workflow session writes, and a `timeout`
    verdict reaches this hook having never read anything at all."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "bmad-dev-auto-result-3-1-dev-1.md").write_bytes(_BAD_UTF8)
    original = _unvouched("timeout")
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_non_utf8_stories_spec_keeps_stall(tmp_path):
    """Stories mode resolves the spec by id, not by scan; the same fault must
    degrade to a kept verdict there too."""
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    d = tmp_path / "epic" / "stories"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1-slug.md").write_bytes(_BAD_UTF8)
    original = _unvouched()
    assert (
        adapter._post_kill_reconcile(_dev_handle(), _stories_spec(tmp_path), original) is original
    )


def test_stories_readback_oserror_spec_returns_none(tmp_path, monkeypatch):
    """The read-back *poll* (not the post-kill hook) is where the issue's headline
    crash lived: this path guards only UnicodeDecodeError, so an OSError escaped to
    engine.run()'s `except Exception` and marked the whole run crashed. It now reads
    like a spec that has not terminated yet — poll returns None, grace expires, the
    stall/timeout verdict routes through the designed ladder.

    `devcontract` binds `read_frontmatter` by ``from .verify import``, so patch the
    name on `devcontract`; patching `verify.read_frontmatter` would not rebind it.
    Faulting `Path.read_text` instead would also trip `stories.resolve_story_spec`,
    whose own guard would mask which read actually failed."""
    adapter, _ = make_dev_adapter(tmp_path)
    _write_story_spec(tmp_path, "1", "slug", _DONE_SPEC)

    def boom(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(generic.devcontract, "read_frontmatter", boom)
    assert adapter._stories_synth_result(_dev_handle(), _stories_spec(tmp_path), wait=False) is None


def test_post_kill_reconcile_blank_frontmatter_prose_done_rescues(tmp_path):
    """status_consistent is "no active disagreement": a blank frontmatter with prose
    `done` is exactly what a delivered Stop would have synthesized (the engine's
    reconcile repairs the lagging frontmatter downstream) — rescued."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text("## Auto Run Result\n\nStatus: done\nDone.\n")
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched())
    assert result.status == "completed"
    assert result.result_json["status"] == "done"


def test_post_kill_reconcile_present_but_blank_frontmatter_status_rescues(tmp_path):
    """Sibling of the test above at the shape that actually shipped broken: a
    frontmatter block IS present, with a blank `status:` line (YAML null) — the
    bmad-dev-auto template shape. The test above covers only fm={} (no block at
    all), and the two diverged: the no-block spec rescued while the template shape
    synthesized `status="none"`/`status_consistent=False` and was discarded as
    stalled, losing a session's finished work (#369)."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus:\nbaseline_revision: blankbase\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nDone.\n"
    )
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched())
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["baseline_commit"] == "blankbase"


def test_post_kill_reconcile_rescues_stories_spec(tmp_path):
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    _write_story_spec(
        tmp_path,
        "1",
        "foo",
        "---\nstatus: done\nbaseline_revision: story1base\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n",
    )
    result = adapter._post_kill_reconcile(_dev_handle(), _stories_spec(tmp_path), _unvouched())
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["baseline_commit"] == "story1base"


def test_post_kill_reconcile_rescues_stories_plan_halt_leg(tmp_path):
    """The plan-halt leg's `ready-for-dev` is a successful terminal (marked
    plan_halt, no escalation) — a lost Stop on that leg is rescued too. This
    deliberately widens #61's literal done-only wording."""
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    _write_story_spec(tmp_path, "1", "foo", "---\nstatus: ready-for-dev\n---\n\nplan\n")
    spec = _stories_spec(tmp_path)
    spec.env["BMAD_LOOP_PLAN_HALT"] = "1"
    result = adapter._post_kill_reconcile(_dev_handle(), spec, _unvouched())
    assert result.status == "completed"
    assert result.result_json["plan_halt"] is True
    assert result.result_json["post_kill_reconciled"] is True


def test_run_kills_before_the_post_kill_probe(tmp_path):
    """run() must tear the window down before the hook probes/scans — the rescue's
    trust rests on the kill having settled liveness."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    order = []
    adapter.start_session = lambda spec: _dev_handle()
    adapter.wait_for_completion = lambda handle, spec: _unvouched()
    adapter.kill = lambda handle: order.append("kill")
    adapter._window_alive = lambda handle: (order.append("probe"), False)[1]
    result = adapter.run(_dev_spec(tmp_path))
    assert order == ["kill", "probe"]
    assert result.status == "completed"


def test_run_exception_kills_without_reconcile(tmp_path):
    """A raising wait_for_completion (e.g. RunStopped) must still kill the window
    and propagate — the hook only runs on the normal return path."""
    adapter, _ = make_dev_adapter(tmp_path)
    calls = []
    adapter.start_session = lambda spec: _dev_handle()

    def raising_wait(handle, spec):
        raise RuntimeError("stop requested")

    adapter.wait_for_completion = raising_wait
    adapter.kill = lambda handle: calls.append("kill")
    adapter._post_kill_reconcile = lambda handle, spec, result: calls.append("hook")
    with pytest.raises(RuntimeError, match="stop requested"):
        adapter.run(_dev_spec(tmp_path))
    assert calls == ["kill"]


# ----------------------------------- env-fault classification (#194, part 1/3)
#
# A coding CLI that loses its API connection idles out the session clock and is
# stamped `timeout` (or `stalled`/`crashed`), indistinguishable from a real
# wall-clock timeout. _classify_env_fault reads the tee'd pane log's tail ONCE
# after the verdict and reconcile settle, matches the profile's env_fault_patterns
# against the ANSI-stripped lines, and stamps env_fault/env_fault_evidence so a
# later phase can PAUSE instead of charging a dev attempt. These pin the classifier
# in isolation plus its ordering through run(). Downstream consumption is phase 2+.

_ENV_FAULT_TASK = "1-1-a-dev-1"


def _write_task_log(adapter, data: bytes, task_id=_ENV_FAULT_TASK) -> None:
    (adapter.logs_dir / f"{task_id}.log").write_bytes(data)


def _classify(adapter, status, *, result_json=None, task_id=_ENV_FAULT_TASK) -> SessionResult:
    handle = SessionHandle(task_id=task_id, native_id="@1")
    result = SessionResult(status=status, result_json=result_json)
    spec = make_spec(adapter.run_dir, task_id=task_id)
    return adapter._classify_env_fault(handle, spec, result)


def test_classify_env_fault_flags_timeout_from_ansi_log(tmp_path):
    """The headline case: a timeout whose pane log holds an ANSI-colored
    `API Error … Unable to connect to API (ConnectionRefused)` line is stamped
    env_fault, the evidence is the ANSI-stripped line, and an
    `env-fault-classified` breadcrumb is written."""
    adapter = make_adapter(tmp_path)  # claude profile ships the seed pattern
    _write_task_log(
        adapter,
        b"building the diff...\n"
        b"\x1b[31mAPI Error: Unable to connect to API (ConnectionRefused)\x1b[0m\n"
        b"idle...\n",
    )
    result = _classify(adapter, "timeout")
    assert result.env_fault is True
    assert result.status == "timeout"  # the status string is unchanged
    assert result.env_fault_evidence == "API Error: Unable to connect to API (ConnectionRefused)"
    assert "\x1b" not in result.env_fault_evidence  # ANSI stripped
    events = _lifecycle_lines(adapter, _ENV_FAULT_TASK)
    assert [e["event"] for e in events] == ["env-fault-classified"]
    assert events[0]["status"] == "timeout"
    assert "ConnectionRefused" in events[0]["evidence"]


@pytest.mark.parametrize("status", ["stalled", "crashed"])
def test_classify_env_fault_flags_stalled_and_crashed(tmp_path, status):
    """stalled and crashed join timeout in the eligible set — all three can be a
    lost-connection session dressed up as a non-completed verdict."""
    adapter = make_adapter(tmp_path)
    _write_task_log(adapter, b"API Error: Connection closed mid-response\n")
    result = _classify(adapter, status)
    assert result.env_fault is True
    assert result.env_fault_evidence == "API Error: Connection closed mid-response"


def test_classify_env_fault_ignores_completed_and_over_budget(tmp_path):
    """completed never reaches the scan (it carries result_json), and over_budget is
    excluded outright — a budget crossing proves real API traffic. Both pass through
    unchanged (same object), with no breadcrumb."""
    adapter = make_adapter(tmp_path)
    _write_task_log(adapter, b"API Error: Connection closed mid-response\n")
    completed = _classify(adapter, "completed", result_json={"ok": True})
    assert completed.env_fault is False and completed.env_fault_evidence is None
    over = _classify(adapter, "over_budget")
    assert over.env_fault is False and over.env_fault_evidence is None
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_classify_env_fault_ignores_result_json_present(tmp_path):
    """The guard is `result_json is None`: an eligible status that somehow carries a
    result dict is trusted work, never re-classified."""
    adapter = make_adapter(tmp_path)
    _write_task_log(adapter, b"API Error: Connection closed mid-response\n")
    result = _classify(adapter, "timeout", result_json={"salvaged": True})
    assert result.env_fault is False
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_classify_env_fault_inert_without_patterns(tmp_path):
    """A profile with no env_fault_patterns never classifies, even with a matching
    line in the log — so mixing EnvFaultMixin into an adapter is always safe.

    Builds the empty profile explicitly rather than borrowing whichever shipped CLI
    happens to have none (it used to borrow codex). Four profiles are in fact
    unseeded — see test_unseeded_profiles_stay_inert — but which ones is a shipping
    decision that has changed once already and should not be able to silently
    invalidate this test.

    The profile is swapped BEFORE the first classification on purpose:
    _env_fault_patterns is a cached_property, so a swap afterwards would leave the
    old patterns compiled (documented on the property)."""
    adapter = make_adapter(tmp_path)
    adapter.profile = dataclasses.replace(adapter.profile, env_fault_patterns=())
    assert adapter._env_fault_patterns == ()
    _write_task_log(adapter, b"API Error: Connection closed mid-response\n")
    result = _classify(adapter, "timeout")
    assert result.env_fault is False
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_classify_env_fault_no_match_leaves_verdict(tmp_path):
    """A log with no transport-failure line (only benign output that mentions
    `API Error` in prose, the false-positive control) leaves the verdict alone."""
    adapter = make_adapter(tmp_path)
    _write_task_log(adapter, b"the story tests how we surface an API Error to users\n")
    result = _classify(adapter, "timeout")
    assert result.env_fault is False
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_classify_env_fault_missing_log_degrades_silently(tmp_path):
    """No pane log at all (an OSError on read) → no classification, no crash,
    no breadcrumb — the best-effort doctrine."""
    adapter = make_adapter(tmp_path)  # no log file written
    result = _classify(adapter, "timeout")
    assert result.env_fault is False
    assert result.env_fault_evidence is None
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_classify_env_fault_last_match_wins_and_truncates(tmp_path):
    """Multiple matching lines → the LAST one is the evidence (the most recent
    failure), and a long line is truncated to ENV_FAULT_EVIDENCE_MAX."""
    adapter = make_adapter(tmp_path)
    filler = "x" * 400
    _write_task_log(
        adapter,
        (
            f"API Error: Connection closed mid-response FIRST {filler}\n"
            f"unrelated line\n"
            f"API Error: Connection closed mid-response LAST {filler}\n"
        ).encode(),
    )
    result = _classify(adapter, "crashed")
    assert result.env_fault is True
    assert len(result.env_fault_evidence) == generic.ENV_FAULT_EVIDENCE_MAX
    assert "LAST" in result.env_fault_evidence  # last match won
    assert "FIRST" not in result.env_fault_evidence


def test_run_classifies_env_fault_after_reconcile(tmp_path):
    """Through run(): a non-rescued timeout (window still alive at the post-kill
    probe) whose log tail matches is stamped env_fault — classification runs on the
    reconcile-settled result."""
    adapter, _impl = make_dev_adapter(tmp_path)
    _write_task_log(
        adapter,
        b"\x1b[31mAPI Error: Unable to connect to API (ECONNREFUSED)\x1b[0m\n",
        task_id="3-1-dev-1",
    )
    adapter.start_session = lambda spec: _dev_handle()
    adapter.wait_for_completion = lambda handle, spec: _unvouched("timeout")
    adapter.kill = lambda handle: None
    adapter._window_alive = lambda handle: True  # alive → reconcile keeps the timeout
    result = adapter.run(_dev_spec(tmp_path))
    assert result.status == "timeout"
    assert result.env_fault is True
    assert "ECONNREFUSED" in result.env_fault_evidence


def test_run_reconcile_upgrade_is_not_reclassified(tmp_path):
    """The ordering invariant: a session reconcile upgrades to completed is NOT
    re-classified, even though the same log tail would match — env_fault runs after
    the reconcile and only ever inspects a non-completed, result-less verdict."""
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)  # a done artifact to rescue
    _write_task_log(
        adapter,
        # Padded past PROOF_OF_WORK_MIN_LOG_BYTES: this fixture stands in for a
        # session that implemented the story and then lost its Stop, and such a
        # session renders. A log holding ONLY the error line would be a session that
        # rendered ~50 bytes total, which the #261 gate correctly declines to rescue
        # — it would make this ordering test depend on a scenario it is not about.
        b"working...\n" * 32 + b"API Error: Unable to connect to API (ECONNREFUSED)\n",
        task_id="3-1-dev-1",
    )
    adapter.start_session = lambda spec: _dev_handle()
    adapter.wait_for_completion = lambda handle, spec: _unvouched("timeout")
    adapter.kill = lambda handle: None
    adapter._window_alive = lambda handle: False  # dead → reconcile rescues to completed
    result = adapter.run(_dev_spec(tmp_path))
    assert result.status == "completed"
    assert result.env_fault is False
    assert result.env_fault_evidence is None


class _StartSessionMux:
    """Minimal mux to drive the real start_session: new_window returns a stable id
    and pipe_pane records the log path it was handed (it writes nothing itself)."""

    def __init__(self):
        self.piped: list[tuple[str, Path]] = []

    def new_window(self, session_name, window_name, cwd, env, cmd):
        return "@1"

    def pipe_pane(self, window_id, log_file):
        self.piped.append((window_id, Path(log_file)))

    def has_session(self, name):
        # The crash-path diagnosis probe (#489) asks this; these tests are about a
        # window that died under a session that is still very much there.
        return True


def test_start_session_resets_reused_task_log(tmp_path):
    """A re-armed run reuses task_ids and both mux backends APPEND to
    logs/<task_id>.log, so a prior cycle's transport-failure line would linger in the
    64 KiB tail and mis-flag a later unrelated timeout. start_session drops the stale
    tee before re-piping (mirroring the result.json unlink), so the reused path holds
    only the current session's output.

    It then re-creates the file EMPTY rather than leaving it absent, so a window that
    dies before pipe_pane attaches still reports "rendered nothing" to the #261
    proof-of-work gate instead of "no pane signal here" (#298 review). The invariant
    this pins is that no stale BYTE survives — not that no file does."""
    mux = _StartSessionMux()
    adapter = make_adapter(tmp_path, mux=mux)
    adapter._ensure_session = lambda cwd: None  # skip the tmux server plumbing
    task_id = _ENV_FAULT_TASK
    _write_task_log(
        adapter, b"API Error: Unable to connect to API (ECONNREFUSED)\n", task_id=task_id
    )
    log_path = adapter.logs_dir / f"{task_id}.log"
    assert log_path.stat().st_size > 0  # the prior cycle's tee is present...

    adapter.start_session(make_spec(tmp_path, task_id=task_id))

    # ...and start_session dropped every stale byte before re-piping, leaving the
    # empty file the proof-of-work gate needs to read a dead-on-arrival window.
    assert log_path.read_bytes() == b""
    assert mux.piped == [("@1", log_path)]  # the fresh tee attaches to the same path
    # a re-driven session that times out with no NEW matching output is not misclassified
    assert _classify(adapter, "timeout", task_id=task_id).env_fault is False


def test_classify_env_fault_bounds_pathological_pattern(tmp_path, monkeypatch):
    """A pathological operator regex can't hang run() teardown: each match is bounded
    by ENV_FAULT_MATCH_TIMEOUT_S, and exceeding it aborts the WHOLE scan and declines
    to classify (best-effort, like an unreadable log) rather than backtracking forever
    on a long tail line.

    Two details keep this test honest, and it was vacuous without both:

    * The patch targets ``env_fault``, the module that DEFINES the constant and reads
      it at ``pat.search`` time. Patching ``generic`` — which only re-exports the name,
      copying the object binding — never reaches the classifier, so the scan silently
      ran at the 2.0s default.
    * The second, trivially-matching pattern is what makes "declined because a match
      timed out" distinguishable from "found nothing". With a lone non-matching
      backtracker, ``evidence is None`` holds for BOTH reasons, so the assertions
      passed with the timeout gate deleted outright. Here, any scan that is not cut
      short reaches ``!$``, matches, and reddens every assertion below — which is
      also what catches the patch being repointed at a non-authoritative module,
      since the 2.0s default lets the backtracker run to completion."""
    adapter = make_adapter(tmp_path)
    adapter._env_fault_patterns = (
        regex.compile(r"(a+)+$"),  # catastrophic backtracker, never matches
        regex.compile(r"!$"),  # trips instantly IF the scan is allowed to get here
    )
    monkeypatch.setattr(env_fault, "ENV_FAULT_MATCH_TIMEOUT_S", 0.1)
    _write_task_log(adapter, b"a" * 1000 + b"!\n")  # long non-matching line -> deep backtrack
    start = time.monotonic()
    result = _classify(adapter, "timeout")
    assert time.monotonic() - start < 5  # bounded; did not hang on the runaway match
    assert result.env_fault is False and result.env_fault_evidence is None
    assert _lifecycle_lines(adapter, _ENV_FAULT_TASK) == []


def test_wait_for_completion_tolerates_transient_liveness_probe_failure(tmp_path, monkeypatch):
    """A transient transport hang (the liveness probe raising MultiplexerError, e.g.
    a 30s tmux hang) must never be read as a dead window -> crash. The tick is
    skipped; once the probe recovers and the session's turn-end lands, the run
    completes normally (the 0.7.7 stall-hardening rule: don't roll back a
    possibly-working session)."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)

    probe_calls = {"n": 0}

    def flaky_alive(handle):
        probe_calls["n"] += 1
        if probe_calls["n"] == 1:
            raise MultiplexerError("transient tmux hang")  # transport hiccup, not death
        return True  # recovered

    adapter._window_alive = flaky_alive

    def flush_terminal_spec(call_n):
        if call_n == 3:  # the session's real turn-end lands its spec
            (impl / "spec-3-1-foo.md").write_text(
                "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
            )

    adapter.watcher = _ScriptedWatcher(
        [None, None, _stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],
        on_call=flush_terminal_spec,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "completed"  # never "crashed"
    assert probe_calls["n"] == 2  # probe failed once, then recovered


def test_wait_for_completion_persistent_probe_failure_times_out_not_crashes(tmp_path, monkeypatch):
    """A persistent transport failure (the probe always raising MultiplexerError)
    must degrade to an honest 'timeout' when it outlasts spec.timeout_s — never a
    spurious 'crashed' (death was never actually observed)."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)

    def always_hangs(handle):
        raise MultiplexerError("tmux server wedged")

    adapter._window_alive = always_hangs

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def advance(call_n):
        clock["t"] += 11.0  # each idle tick crawls toward spec.timeout_s

    adapter.watcher = _ScriptedWatcher([], on_call=advance)  # None forever
    spec = SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
        timeout_s=30.0,
    )
    result = adapter.wait_for_completion(_dev_handle(), spec)
    assert result.status == "timeout"  # bounded by spec.timeout_s, not crashed


def test_wait_for_completion_genuine_window_death_still_crashes(tmp_path, monkeypatch):
    """The transient-tolerance must not disable real crash detection: a probe that
    cleanly returns False (dead window -> list_window_ids returned [], no exception)
    is still a crash."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False  # genuinely dead

    adapter.watcher = _ScriptedWatcher([])  # None on the first idle tick
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "crashed"


class _SessionProbeMux:
    """Mux stand-in exposing only what `_session_vanished` asks: has_session."""

    def __init__(self, answer):
        self._answer = answer
        self.calls: list[str] = []

    def has_session(self, name):
        self.calls.append(name)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.mark.parametrize(
    ("has_session", "expect_vanished"),
    [
        (False, True),  # the session itself is gone: something destroyed it
        (True, False),  # session alive, window gone: the CLI exited
        (MultiplexerError("server wedged"), False),  # unknown is not vanished
    ],
)
def test_window_death_distinguishes_a_destroyed_session_from_an_exited_cli(
    tmp_path, monkeypatch, has_session, expect_vanished
):
    """#489: `list_window_ids` answers [] for a dead window AND for a session that
    no longer exists, so the crash verdict alone cannot say which happened. The
    verdict is `crashed` either way — only the diagnosis differs."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    adapter.mux = _SessionProbeMux(has_session)

    adapter.watcher = _ScriptedWatcher([])
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "crashed"
    assert result.session_vanished is expect_vanished
    assert adapter.mux.calls == [adapter.session_name]
    # The durable half of the diagnosis: CHANGELOG and FEATURES both promise this
    # crumb, and without an assertion deleting the write keeps the suite green.
    crumbs = _lifecycle_events(adapter, "session-vanished")
    assert len(crumbs) == (1 if expect_vanished else 0)
    if expect_vanished:
        assert crumbs[0]["session"] == adapter.session_name
        assert crumbs[0]["status"] == "crashed"


def test_session_probe_is_skipped_for_non_crash_verdicts(tmp_path, monkeypatch):
    """The probe answers "why did the window die" — a stall/timeout reached under a
    LIVE window never asks it, or an unrelated mux outage would be misattributed to
    a session the mux never touched."""
    adapter, _ = make_dev_adapter(tmp_path)
    adapter.mux = _SessionProbeMux(False)  # would say "vanished" if asked

    res = adapter._final(_dev_handle(), _dev_spec(tmp_path), "timeout", None, None)

    assert res.status == "timeout"
    assert res.session_vanished is False
    assert adapter.mux.calls == []


def test_read_back_upgrade_is_not_diagnosed_even_if_the_session_is_gone(tmp_path):
    """The deliberately-dropped case: a session reaped AFTER flushing its result
    still earns `completed`, and a completed session gets no vanished diagnosis —
    it produced something. Pinned separately because the skip test above only
    exercises a `timeout` fallback, so this branch could invert unnoticed."""
    adapter = make_adapter(tmp_path)
    adapter.mux = _SessionProbeMux(False)  # the session really is gone
    task_dir = adapter.tasks_dir / "1-1-a-dev-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text('{"status": "done", "workflow": "dev"}')

    res = adapter._final(
        SessionHandle(task_id="1-1-a-dev-1", native_id="@1"),
        make_spec(tmp_path),
        "crashed",
        None,
        None,
    )

    assert res.status == "completed"
    assert res.session_vanished is False
    assert adapter.mux.calls == []  # never even asked


def _usage_adapter(tmp_path, profile_name, **kw) -> GenericTmuxAdapter:
    return GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile(profile_name),
        **kw,
    )


def test_effective_timing_knobs_precedence(tmp_path):
    # copilot ships grace 8 / nudges 5; with no override the profile value wins
    cop = _usage_adapter(tmp_path, "copilot")
    assert cop._usage_grace_s == 8.0
    assert cop._stop_nudges == 5
    # claude ships neither -> grace 0, nudges from the global limits default (1)
    cla = _usage_adapter(tmp_path, "claude")
    assert cla._usage_grace_s == 0.0
    assert cla._stop_nudges == 1
    # an explicit [adapter]/[adapter.<stage>] override beats the profile default
    over = _usage_adapter(tmp_path, "copilot", usage_grace_s=2.0, stop_without_result_nudges=9)
    assert over._usage_grace_s == 2.0
    assert over._stop_nudges == 9


def test_effective_nudges_fall_back_to_global_limits(tmp_path):
    # claude carries no profile nudge value, so the global limits value flows through
    cla = GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy(stop_without_result_nudges=4)),
        profile=get_profile("claude"),
    )
    assert cla._stop_nudges == 4
    # the copilot profile floor still wins over a lower global default
    cop = GenericTmuxAdapter(
        run_dir=tmp_path / "run2",
        policy=Policy(limits=LimitsPolicy(stop_without_result_nudges=2)),
        profile=get_profile("copilot"),
    )
    assert cop._stop_nudges == 5


def test_read_usage_polls_for_late_metrics(tmp_path, monkeypatch):
    # copilot ships usage_grace_s = 8.0, so read_usage retries until metrics land
    adapter = _usage_adapter(tmp_path, "copilot")
    usage = TokenUsage(input_tokens=10)
    calls: list[str] = []

    def fake_tally(parser, path):
        calls.append(parser)
        return None if len(calls) < 3 else usage

    monkeypatch.setattr(generic, "tally_usage", fake_tally)
    monkeypatch.setattr(generic.time, "sleep", lambda *_: None)
    result = SessionResult(status="completed", transcript_path=str(tmp_path / "events.jsonl"))
    assert adapter.read_usage(result) is usage
    assert len(calls) == 3  # polled past the early None reads


def test_read_usage_single_read_when_no_grace(tmp_path, monkeypatch):
    # claude has usage_grace_s = 0.0 -> read exactly once, never sleeps
    adapter = _usage_adapter(tmp_path, "claude")
    calls: list[str] = []

    def fake_tally(parser, path):
        calls.append(parser)
        return None

    def no_sleep(*_):
        raise AssertionError("read_usage must not sleep when the grace is 0")

    monkeypatch.setattr(generic, "tally_usage", fake_tally)
    monkeypatch.setattr(generic.time, "sleep", no_sleep)
    result = SessionResult(status="completed", transcript_path=str(tmp_path / "x.jsonl"))
    assert adapter.read_usage(result) is None
    assert len(calls) == 1


def test_read_usage_none_without_transcript(tmp_path):
    adapter = _usage_adapter(tmp_path, "copilot")
    assert adapter.read_usage(SessionResult(status="completed")) is None


def _write_fake_cli(tmp_path, script: str = FAKE_CLI):
    fake = tmp_path / "fake-cli"
    fake.write_text(script)
    fake.chmod(0o755)
    return fake


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
@pytest.mark.parametrize("profile_name", ["claude", "codex", "gemini"])
def test_tmux_end_to_end_with_fake_cli(tmp_path, profile_name):
    """Spawn a real tmux window running a fake CLI that behaves like a
    hook-instrumented session: emits SessionStart + result.json + Stop."""
    fake = _write_fake_cli(tmp_path)
    # extra_args=() drops the bypass flags so the rendered prompt is the last argv
    # entry for every profile (claude/codex positional, gemini behind -i).
    adapter = make_adapter(tmp_path, profile_name=profile_name, binary=str(fake), extra_args=())
    spec_env = {
        "BMAD_LOOP_MODE": "1",
        "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
        "BMAD_LOOP_EVENTS_DIR": str(adapter.watcher.events_dir),
        "BMAD_LOOP_TASK_ID": "t-int-1",
    }
    spec = SessionSpec(
        task_id="t-int-1",
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env=spec_env,
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "auto-dev"
    # the fake echoes back the rendered prompt it received
    assert result.result_json["prompt"] == adapter.profile.render_prompt(spec.prompt)
    assert result.session_id == "fake-1"
    # canonical prompt recorded for debugging
    assert (adapter.tasks_dir / "t-int-1" / "prompt.txt").read_text().strip() == spec.prompt


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_reused_task_id_ignores_stale_artifacts(tmp_path):
    """A re-armed run reuses the task_id. A prior cycle's Stop event + result.json
    must NOT replay: start_session clears the stale result, and the launch-time
    floor makes wait_for skip the old Stop so only the fresh session counts."""
    fake = _write_fake_cli(tmp_path)
    adapter = make_adapter(tmp_path, binary=str(fake), extra_args=())
    task_id = "t-reused-1"
    # seed last cycle's leftovers, with an obviously old ts and a stale marker
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "result.json").write_text('{"workflow": "STALE"}', encoding="utf-8")
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"1-{task_id}-Stop.json").write_text(
        '{"ts": 1, "event": "Stop", "task_id": "' + task_id + '", "session_id": "old"}',
        encoding="utf-8",
    )
    spec = SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_EVENTS_DIR": str(adapter.watcher.events_dir),
            "BMAD_LOOP_TASK_ID": task_id,
        },
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "auto-dev"  # fresh, not "STALE"
    assert result.session_id == "fake-1"  # fresh session, not "old"


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_end_to_end_with_a_relay_that_only_knows_the_legacy_dir(tmp_path):
    """The version-skew guard, end to end through real tmux: a CURRENT
    orchestrator (it exports BMAD_LOOP_EVENTS_DIR and waits on the out-of-tree
    channel) driving a session whose relay is an OLD copy that writes only to
    `<run_dir>/events`. That pairing is not exotic — the relay is copied into the
    target project at init, so every project not re-inited after an upgrade is in
    it, and without the watcher's legacy poll EVERY such session stalls to
    `session_timeout_min` instead of completing.

    Ablation guard: drop `legacy_dir` from `SignalWatcher._dirs()` and this fails
    (as a 30s timeout, not an assertion — which is precisely the production
    symptom)."""
    assert "$BMAD_LOOP_EVENTS_DIR" not in LEGACY_EVENTS_FAKE_CLI, "the twin still reads the new var"
    assert LEGACY_EVENTS_FAKE_CLI != FAKE_CLI, "the swap did not take"

    fake = _write_fake_cli(tmp_path, LEGACY_EVENTS_FAKE_CLI)
    adapter = make_adapter(tmp_path, binary=str(fake), extra_args=())
    spec = SessionSpec(
        task_id="t-legacy-1",
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_EVENTS_DIR": str(adapter.watcher.events_dir),
            "BMAD_LOOP_TASK_ID": "t-legacy-1",
        },
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.session_id == "fake-1"
    # the premise, asserted rather than assumed: the events really did land in the
    # legacy location and nowhere else, so the completion came through the fallback
    assert list((adapter.run_dir / "events").glob("*.json"))
    assert not list(adapter.watcher.events_dir.glob("*.json"))


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_crash_detected(tmp_path):
    """A session that dies without writing result.json -> crashed. Also the
    SessionEnd-less path (codex profile) relies on this window-death check."""
    fake = tmp_path / "fake-cli"
    fake.write_text("#!/bin/bash\nexit 1\n")
    fake.chmod(0o755)

    adapter = make_adapter(
        tmp_path, profile_name="codex", binary=str(fake), stop_without_result_nudges=0
    )
    spec = SessionSpec(
        task_id="t-crash",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(adapter.run_dir), "BMAD_LOOP_TASK_ID": "t-crash"},
        timeout_s=20.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)
    assert result.status == "crashed"
    assert result.result_json is None


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_timeout_with_flushed_spec_rescued_post_kill(tmp_path):
    """End-to-end #61 (total hook loss): the session writes its terminal spec but
    never emits any hook event, so the wait loop idles to `timeout` — a path that
    never arms the stall grace and checks no artifact. run()'s real kill then
    settles liveness, and the post-kill reconcile rescues the finished work
    through a real tmux probe + scan.

    The fake renders to its pane before writing the spec. That is not decoration:
    the #261 proof-of-work gate refuses to upgrade a session that emitted NOTHING
    (no hook event AND no pane-log growth), and a CLI that genuinely implemented a
    story always renders — the run this test stands in for logged 1.4 MB. A silent
    fake would be byte-identical to the wedge #261 is about (0-byte log, zero
    events), which is precisely what must NOT be rescued; see the companion
    test_tmux_timeout_silent_session_not_rescued."""
    impl = tmp_path / "impl"
    impl.mkdir()
    fake = tmp_path / "fake-cli"
    fake.write_text(
        "#!/bin/bash\n"
        "# finished work, but hooks are 'misconfigured': no event files at all\n"
        # Emit OVER TIME, not in one burst at startup: pipe_pane attaches after the
        # window is created, so a burst can finish before the sink exists and leave
        # a 0-byte log on a fast runner (observed on CI py3.11/3.12 while 3.13/3.14
        # passed). Spread across ~2s, well inside the 6s session timeout below.
        'for i in $(seq 1 40); do echo "implementing story 3-1: step $i of 40 ..."; '
        "sleep 0.05; done\n"
        f"printf -- '---\\nstatus: done\\nbaseline_revision: abc123\\n---\\n\\n"
        f"## Auto Run Result\\n\\nStatus: done\\nImplemented.\\n' > {impl}/spec-3-1-foo.md\n"
        "sleep 60  # stay alive so the wait loop times out under a live window\n"
    )
    fake.chmod(0o755)
    adapter = GenericDevAdapter(
        run_dir=tmp_path / f"run-{uuid.uuid4().hex[:8]}",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        binary=str(fake),
        extra_args=(),
        paths=ProjectPaths(
            project=tmp_path,
            implementation_artifacts=impl,
            planning_artifacts=tmp_path / "plan",
        ),
    )
    spec = SessionSpec(
        task_id="t-rescue",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_TASK_ID": "t-rescue",
            "BMAD_LOOP_STORY_KEY": "3-1",
        },
        timeout_s=6.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_timeout_silent_session_not_rescued(tmp_path):
    """The #261 counterpart of the rescue above, same call path, one delta: the CLI
    wedges instantly and renders NOTHING. A qualifying spec still appears in the
    shared artifacts dir — here written by a concurrent process, exactly as a
    parallel run's merge-back did in the report — and it is newer than launch, so
    the mtime scan would adopt it and the post-kill reconcile would score a session
    that never ran as `completed:done`.

    Proof-of-work refuses it: no hook event ever arrived AND the pane log never
    grew (0 bytes — measured, the same signature as the report's wedged review
    windows). The verdict stays `timeout`, which is what the report's two control
    stories correctly received."""
    impl = tmp_path / "impl"
    impl.mkdir()
    fake = tmp_path / "fake-cli"
    # Wedges immediately: no output, no events, no writes of its own.
    fake.write_text("#!/bin/bash\nsleep 60\n")
    fake.chmod(0o755)
    adapter = GenericDevAdapter(
        run_dir=tmp_path / f"run-{uuid.uuid4().hex[:8]}",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        binary=str(fake),
        extra_args=(),
        paths=ProjectPaths(
            project=tmp_path,
            implementation_artifacts=impl,
            planning_artifacts=tmp_path / "plan",
        ),
    )
    spec = SessionSpec(
        task_id="t-silent",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_TASK_ID": "t-silent",
            "BMAD_LOOP_STORY_KEY": "3-1",
        },
        timeout_s=6.0,
    )
    # A FOREIGN story's finished spec lands MID-WINDOW, from a separate thread
    # standing in for the concurrent run's merge-back. It must be written after
    # `run()` stamps `launched_ns`, or its mtime sits below the `since_ns` floor,
    # `find_result_artifact` discards it on that alone, and the read-back never
    # produces a candidate for the gate to refuse — the test would then pass with
    # the proof-of-work gate entirely removed (verified: it did).
    #
    # So the writer waits on the ACTUAL launch rather than a fixed sleep: the gap
    # from `run()` to `launched_ns` covers `_ensure_session`, a real `tmux
    # new-session`, and a slow or loaded box can push that past any margin picked
    # in advance — silently restoring the vacuous pass (#298 review). Capturing the
    # handle also gives the assertion below the real floor to test against.
    foreign = impl / "spec-9-9-someone-elses-story.md"
    launched: list[SessionHandle] = []
    launch_stamped = threading.Event()
    real_start_session = adapter.start_session

    def spy_start_session(session_spec):
        handle = real_start_session(session_spec)
        launched.append(handle)
        launch_stamped.set()
        return handle

    adapter.start_session = spy_start_session

    def merge_back():
        if not launch_stamped.wait(timeout=10):
            return  # the run failed to launch; the assertions below will say so
        time.sleep(0.5)  # comfortably inside the 6s session window
        foreign.write_text(
            "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
            "## Auto Run Result\n\nStatus: done\nImplemented.\n"
        )

    writer = threading.Thread(target=merge_back, daemon=True)
    writer.start()
    try:
        result = adapter.run(spec)
    finally:
        writer.join(timeout=10)
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    # The foreign spec really is a candidate the read-back would otherwise adopt: it
    # exists, qualifies, and — tested against the launch floor this session actually
    # recorded, not a permissive 0 — post-dates launch. Only the gate rejects it.
    assert launched, "start_session never ran"
    assert devcontract.is_result_artifact(foreign, since_ns=launched[0].launched_ns)
    assert result.status == "timeout"
    assert result.result_json is None
    # Below the floor is the invariant; exactly zero is merely what it happens to be.
    assert (adapter.logs_dir / "t-silent.log").stat().st_size <= (
        generic.PROOF_OF_WORK_MIN_LOG_BYTES
    )


# ------------------------------- missing-marker fallback (#224)
#
# A session (in practice: the follow-up review leg) can finalize the spec's
# frontmatter to a terminal status while omitting the `## Auto Run Result`
# marker find_result_artifact keys on. The scan-path fallback synthesizes from
# the frontmatter once the fingerprint holds stable across FM_FALLBACK_MIN_OBS
# resultless Stops (live), or on a single sighting under a dead window
# (post-kill reconcile).

_MARKERLESS_DONE = "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nAll done.\n"
_MARKERLESS_BLOCKED = "---\nstatus: blocked\n---\n\n# Story\n\nStuck.\n"


def _snap(spec_file: Path) -> SpecSnapshot:
    """A review-launch snapshot of an on-disk spec, exactly as
    ``_reset_spec_for_review`` captures it: hash + mtime + normalized fm status."""
    raw = spec_file.read_bytes()
    return SpecSnapshot(
        path=str(spec_file),
        mtime_ns=spec_file.stat().st_mtime_ns,
        sha256=hashlib.sha256(raw).hexdigest(),
        fm_status="done",
    )


def _snapshotted_spec(tmp_path, spec_file: Path, story_key="3-1") -> SessionSpec:
    """A review SessionSpec carrying the launch snapshot of ``spec_file`` (#276 M1)."""
    return dataclasses.replace(
        _dev_spec(tmp_path, story_key), role="review", spec_snapshot=_snap(spec_file)
    )


def _snapshotted_stories_spec(tmp_path, story_spec: Path, story_key="1") -> SessionSpec:
    """A review-role stories SessionSpec carrying the launch snapshot of its story
    spec (#276 M1/M2), as the engine threads for a stories-mode review leg."""
    return dataclasses.replace(
        _stories_spec(tmp_path, story_key), role="review", spec_snapshot=_snap(story_spec)
    )


def test_stories_readback_refuses_unmodified_snapshot_no_transition(tmp_path, monkeypatch):
    """#276 M1 on the stories read-back: a `done` story spec byte-identical to its
    review-launch snapshot with no transition observed is the dead-window false
    positive (a review that only bumped the mtime). The gate REFUSES synthesis
    (`unmodified-since-launch`) instead of accepting on the mtime floor alone."""
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    story = _write_story_spec(tmp_path, "1", "foo", _MARKERLESS_DONE)
    spec = _snapshotted_stories_spec(tmp_path, story)  # snapshot == current bytes
    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    (crumb,) = _breadcrumbs(adapter)  # keyed by handle.task_id (3-1-dev-1)
    assert crumb["verdict"] == "unmodified-since-launch"


def test_stories_readback_transition_beats_snapshot(tmp_path, monkeypatch):
    """M2 outranks M1 on the stories path too: with a recorded transition the same
    byte-identical `done` spec synthesizes (the review provably ran)."""
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    story = _write_story_spec(tmp_path, "1", "foo", _MARKERLESS_DONE)
    spec = _snapshotted_stories_spec(tmp_path, story)
    adapter._fm_transition_obs["3-1-dev-1"] = "in-review"  # keyed by handle.task_id
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj is not None and rj["status"] == "done"
    assert _breadcrumbs(adapter) == []


def test_stories_readback_changed_bytes_synthesizes(tmp_path, monkeypatch):
    """Over-refusal guard: when the spec's bytes differ from the launch snapshot the
    gate is NEUTRAL and the read-back synthesizes as before."""
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    story = _write_story_spec(tmp_path, "1", "foo", _MARKERLESS_DONE)
    spec = _snapshotted_stories_spec(tmp_path, story)  # snapshot of the original bytes
    story.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nReviewed and done.\n",
        encoding="utf-8",
    )  # this session actually rewrote the spec
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj is not None and rj["status"] == "done"


def test_stories_readback_dev_leg_no_snapshot_accepts(tmp_path, monkeypatch):
    """Inert on a dev leg: no launch snapshot → NEUTRAL → the unchanged mtime-floor
    accept, even for a spec that would be byte-identical to some snapshot."""
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    _write_story_spec(tmp_path, "1", "foo", _MARKERLESS_DONE)
    rj = adapter._result_json(_dev_handle(), _stories_spec(tmp_path), wait=True)  # snap None
    assert rj is not None and rj["status"] == "done"


def test_stories_readback_snapshot_other_path_inert(tmp_path, monkeypatch):
    """The gate only bites when the resolved story spec IS the snapshotted file. A
    snapshot for a different path leaves the read-back on its normal accept."""
    adapter, _ = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    story = _write_story_spec(tmp_path, "1", "foo", _MARKERLESS_DONE)
    other = tmp_path / "epic" / "stories" / "9-other.md"
    snap = dataclasses.replace(_snap(story), path=str(other))
    spec = dataclasses.replace(_stories_spec(tmp_path), role="review", spec_snapshot=snap)
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj is not None and rj["status"] == "done"


def test_snapshot_verdict_truth_table(tmp_path):
    """Direct unit test of the shared M1/M2 decision — the anti-drift guard both the
    scan fallback and the stories read-back route through."""
    adapter, _ = make_dev_adapter(tmp_path)
    snap = SpecSnapshot(path="/x/spec.md", mtime_ns=1, sha256="deadbeef", fm_status="done")
    decide = adapter._snapshot_verdict
    V = generic._SnapVerdict
    # no snapshot / different file → NEUTRAL regardless of digest
    assert decide(same_file=False, snap=None, task_id="t", digest="deadbeef") is V.NEUTRAL
    assert decide(same_file=False, snap=snap, task_id="t", digest="deadbeef") is V.NEUTRAL
    # matching digest, no transition → REFUSE
    assert decide(same_file=True, snap=snap, task_id="t", digest="deadbeef") is V.REFUSE
    # transition observed → PROVEN, even with a matching digest
    adapter._fm_transition_obs["t"] = "in-review"
    assert decide(same_file=True, snap=snap, task_id="t", digest="deadbeef") is V.PROVEN
    # mismatched digest, no transition → NEUTRAL
    assert decide(same_file=True, snap=snap, task_id="u", digest="feed") is V.NEUTRAL
    # digest None (unreadable) → NEUTRAL
    assert decide(same_file=True, snap=snap, task_id="u", digest=None) is V.NEUTRAL


def test_frontmatter_fallback_synthesizes_on_second_stable_stop(tmp_path, monkeypatch):
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    # first resultless Stop: observation recorded, no harvest yet
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "terminal-frontmatter-pending"
    assert "spec-3-1-foo.md" in crumb["detail"]
    # second Stop over the identical (path, mtime, status) fingerprint: harvest
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "done"
    assert rj["workflow"] == "auto-dev"
    assert rj["baseline_commit"] == "abc123"
    assert rj["synthesized_from_frontmatter"] is True
    assert rj["escalations"] == []
    # the harvest pass writes no breadcrumb
    assert len(_breadcrumbs(adapter)) == 1


def test_frontmatter_fallback_stamps_story_key_and_dw_ids(tmp_path, monkeypatch):
    """The fallback shares _synthesize_from with the marker path, so bundle dev
    sessions get their exported dw ids stamped for verify_dev_bundle."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    spec = dataclasses.replace(
        _dev_spec(tmp_path),
        env={"BMAD_LOOP_STORY_KEY": "3-1", "BMAD_LOOP_DW_IDS": "DW-7, DW-9"},
    )
    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["story_key"] == "3-1"
    assert rj["dw_ids"] == ["DW-7", "DW-9"]


def test_frontmatter_fallback_mtime_bump_resets_counter(tmp_path, monkeypatch):
    """A spec still being written (the premature-harvest hazard: review launched
    on a done spec, first edit bumped mtime before the in-review flip) must not
    be harvested — any fingerprint change restarts the count."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    os.utime(spec_file, ns=(1_000_000_000, 1_000_000_000))
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    os.utime(spec_file, ns=(2_000_000_000, 2_000_000_000))  # the session wrote again
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    # now stable across two Stops -> harvest on the third call
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["synthesized_from_frontmatter"] is True
    verdicts = [c["verdict"] for c in _breadcrumbs(adapter)]
    assert verdicts == ["terminal-frontmatter-pending", "terminal-frontmatter-pending"]


def test_frontmatter_fallback_status_flip_clears_observations(tmp_path, monkeypatch):
    """done -> in-review (a review actually running) drops the candidate AND the
    recorded fingerprint, so a later terminal state starts the count over."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    os.utime(spec_file, ns=(1_000_000_000, 1_000_000_000))
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    spec_file.write_text("---\nstatus: in-review\n---\n\n# Story\n")
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    assert adapter._fm_fallback_obs == {}
    spec_file.write_text(_MARKERLESS_DONE)
    os.utime(spec_file, ns=(3_000_000_000, 3_000_000_000))
    # back at one observation: not harvested yet
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["synthesized_from_frontmatter"] is True


def test_frontmatter_fallback_blocked_synthesizes_critical(tmp_path, monkeypatch):
    """A marker-less blocked terminal synthesizes the same CRITICAL escalation
    stories mode produces, routing decide_dev/decide_review_session to PAUSE."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_BLOCKED)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "blocked"
    assert rj["synthesized_from_frontmatter"] is True
    (esc,) = rj["escalations"]
    assert esc["severity"] == "CRITICAL"


def test_frontmatter_fallback_ambiguous_never_harvests(tmp_path, monkeypatch):
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    (impl / "spec-3-2-bar.md").write_text(_MARKERLESS_DONE)
    for _ in range(3):
        assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    verdicts = {c["verdict"] for c in _breadcrumbs(adapter)}
    assert verdicts == {"ambiguous-frontmatter"}
    assert "2 terminal marker-less candidates" in _breadcrumbs(adapter)[0]["detail"]


def test_frontmatter_fallback_pre_launch_spec_is_no_artifact(tmp_path, monkeypatch):
    """A marker-less terminal spec older than the session launch is prior state,
    not this session's output — the plain no-artifact verdict stands."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    handle = _dev_handle(launched_ns=time.time_ns() + 10**12)
    assert adapter._result_json(handle, _dev_spec(tmp_path), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "no-artifact"


def test_frontmatter_fallback_wait_false_is_compare_only(tmp_path, monkeypatch):
    """The crash path's read-once (wait=False, live window) neither records
    observations nor writes breadcrumbs; it may only harvest a fingerprint the
    live loop already saw and that still matches."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    os.utime(spec_file, ns=(1_000_000_000, 1_000_000_000))
    # no prior observation: nothing harvested, nothing recorded, no crumb
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is None
    assert adapter._fm_fallback_obs == {}
    assert _breadcrumbs(adapter) == []
    # one live observation, then the crash read over the unchanged state harvests
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False)
    assert rj["synthesized_from_frontmatter"] is True


def test_post_kill_reconcile_rescues_markerless_done_spec(tmp_path):
    """The #224 backstop: dead window + terminal marker-less frontmatter rescues
    on a single sighting — no live observations required."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched())
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True
    assert result.result_json["synthesized_from_frontmatter"] is True


def test_post_kill_reconcile_markerless_blocked_keeps_verdict(tmp_path):
    """The post-kill done-only gate still refuses a blocked synthesis: blocked
    carries no finished work, marker or no marker."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_BLOCKED)
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_post_kill_reconcile_markerless_ambiguous_keeps_verdict(tmp_path):
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    (impl / "spec-3-2-bar.md").write_text(_MARKERLESS_DONE)
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), original) is original


def test_wait_loop_completes_markerless_spec_on_second_stop(tmp_path, monkeypatch):
    """End-to-end through wait_for_completion: two Stops over a stable
    marker-less done spec complete the session instead of arming the stall
    grace toward the #149 livelock."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, impl = make_dev_adapter(tmp_path)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    stop = _stop_event("3-1-dev-1", "sess-1", "/t.jsonl")
    adapter.watcher = _ScriptedWatcher([stop, stop])
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["synthesized_from_frontmatter"] is True


# ---------------------------- launch-state snapshot + content-hash gate (#276 M1)
#
# A review session inherits the dev pass's `done` spec. When the review is killed
# after an mtime-only bump but before it flips the frontmatter to `in-review`, the
# #224 fallback would score `done` without the review ever running. The engine now
# threads a SpecSnapshot (hash + mtime + fm status) captured at review launch; the
# fallback refuses to synthesize from a candidate still byte-identical to it — in
# every mode, live or dead-window.


def test_frontmatter_fallback_refuses_unmodified_snapshot_live(tmp_path, monkeypatch):
    """A byte-identical spec is provably untouched by this session: no wait=True
    pass ever harvests, every verdict is `unmodified-since-launch`, and the
    fingerprint dict stays empty (no observation recorded or popped)."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    spec = _snapshotted_spec(tmp_path, spec_file)
    for _ in range(3):
        assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    verdicts = {c["verdict"] for c in _breadcrumbs(adapter)}
    assert verdicts == {"unmodified-since-launch"}
    assert "byte-identical to review-launch snapshot" in _breadcrumbs(adapter)[0]["detail"]
    assert adapter._fm_fallback_obs == {}


def test_frontmatter_fallback_refuses_unmodified_snapshot_dead_window(tmp_path):
    """The kill shot: a dead window over a markerless `done` spec whose only change
    since launch is an mtime bump. The single-sighting post-kill rescue must refuse
    — returning the ORIGINAL result — and leave the dead-window lifecycle crumb."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    spec = _snapshotted_spec(tmp_path, spec_file)
    # mtime-only bump: bytes unchanged, so the hash still matches the snapshot.
    os.utime(spec_file, ns=(spec.spec_snapshot.mtime_ns + 10**9,) * 2)
    original = _unvouched()
    assert adapter._post_kill_reconcile(_dev_handle(), spec, original) is original
    events = [ln["event"] for ln in _lifecycle_lines(adapter)]
    assert "frontmatter-unmodified-refused" in events
    # a refusal is not a resultless-Stop breadcrumb (that channel is wait=True only)
    assert _breadcrumbs(adapter) == []


def test_frontmatter_fallback_hash_mismatch_still_two_obs(tmp_path, monkeypatch):
    """Content edited since the snapshot (the review actually wrote): the hash gate
    is inert and today's fingerprint behavior stands — pending on the first stable
    Stop, harvested on the second."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nLaunch.\n"
    )
    spec = _snapshotted_spec(tmp_path, spec_file)
    # the review rewrote the spec's body: bytes differ from the launch snapshot.
    spec_file.write_text(_MARKERLESS_DONE)
    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "terminal-frontmatter-pending"
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["synthesized_from_frontmatter"] is True


def test_frontmatter_fallback_snapshot_other_path_inert(tmp_path, monkeypatch):
    """The gate only fires when the candidate IS the snapshotted spec. A snapshot
    for a different path leaves the fallback on its normal fingerprint path even
    when the candidate happens to be byte-identical to that snapshot's content."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    snap = dataclasses.replace(_snap(spec_file), path=str(impl / "spec-9-9-other.md"))
    spec = dataclasses.replace(_dev_spec(tmp_path), role="review", spec_snapshot=snap)
    # not the snapshotted path -> no hash comparison -> ordinary 2-obs harvest
    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "terminal-frontmatter-pending"
    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["synthesized_from_frontmatter"] is True


def test_frontmatter_fallback_unmodified_wait_false_silent(tmp_path, monkeypatch):
    """The plain crash path (wait=False, live window) is compare-only over an
    unmodified spec: it refuses to synthesize and leaves zero crumbs of either
    kind."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    spec = _snapshotted_spec(tmp_path, spec_file)
    assert adapter._result_json(_dev_handle(), spec, wait=False) is None
    assert _breadcrumbs(adapter) == []
    assert _lifecycle_lines(adapter) == []
    assert adapter._fm_fallback_obs == {}


# ------------------------- mid-session status-transition observation (#276 M2)
#
# The engine threads a launch snapshot whose fm_status is the review's `done`
# (re-opened). On each heartbeat tick the dev adapter samples the snapshotted
# spec and records the FIRST non-terminal status it observes this session drive
# it to (in practice `in-review`). That single sighting makes a later terminal
# frontmatter deterministic proof THIS session wrote it, so the fallback
# harvests on one sighting — but the M1 hash gate still outranks it.


def _transitioned_spec(tmp_path, spec_file: Path, live_status: str = "in-review") -> SessionSpec:
    """A review spec whose launch snapshot carries `done` but whose on-disk file
    now sits at `live_status` — the state a mid-session tick observes."""
    spec_file.write_text(_MARKERLESS_DONE)  # launch state: terminal `done`
    spec = _snapshotted_spec(tmp_path, spec_file)
    spec_file.write_text(f"---\nstatus: {live_status}\n---\n\n# Story\n\nReviewing.\n")
    return spec


def _lifecycle_events(adapter, event, task_id="3-1-dev-1"):
    """Lifecycle breadcrumbs of one `event` kind — the shared filter behind the
    per-event helpers (`spec-status-transition-observed`, `contract-nudge-sent`)."""
    return [ln for ln in _lifecycle_lines(adapter, task_id) if ln["event"] == event]


def test_observe_tick_records_first_transition(tmp_path):
    """The first observed non-terminal, non-launch status is recorded once with a
    single `spec-status-transition-observed` crumb; a second tick — even a
    different non-terminal status — neither overwrites the record nor re-crumbs."""
    adapter, impl = make_dev_adapter(tmp_path)
    spec_file = impl / "spec-3-1-foo.md"
    spec = _transitioned_spec(tmp_path, spec_file)  # on disk: in-review

    adapter._observe_tick(_dev_handle(), spec)
    assert adapter._fm_transition_obs == {"3-1-dev-1": "in-review"}
    (crumb,) = _lifecycle_events(adapter, "spec-status-transition-observed")
    assert crumb["status"] == "in-review"
    assert crumb["spec"] == str(spec_file)

    # a later tick at a different non-terminal status must not overwrite or re-crumb
    spec_file.write_text("---\nstatus: in-progress\n---\n\n# Story\n\nStill.\n")
    adapter._observe_tick(_dev_handle(), spec)
    assert adapter._fm_transition_obs == {"3-1-dev-1": "in-review"}
    assert len(_lifecycle_events(adapter, "spec-status-transition-observed")) == 1


@pytest.mark.parametrize(
    "on_disk, launch_status",
    [
        ("done", "done"),  # terminal — and this review's own launch status
        ("blocked", "done"),  # terminal — belongs to the Stop harvest
        ("", "done"),  # blank/torn parse — not evidence of anything
        ("in-review", "in-review"),  # equals the launch status — not a transition
    ],
)
def test_observe_tick_ignores_terminal_blank_and_launch_status(tmp_path, on_disk, launch_status):
    """A tick records nothing when the on-disk status is terminal (`done`/
    `blocked`), a blank/torn parse, or unchanged from the snapshot's launch
    status — only a live, non-terminal transition off the launch state counts."""
    adapter, impl = make_dev_adapter(tmp_path)
    spec_file = impl / "spec-3-1-foo.md"
    if on_disk == "":
        spec_file.write_text("# Story\n\nno frontmatter\n")  # parses to status ""
    else:
        spec_file.write_text(f"---\nstatus: {on_disk}\n---\n\n# Story\n\nbody\n")
    snap = dataclasses.replace(_snap(spec_file), fm_status=launch_status)
    spec = dataclasses.replace(_dev_spec(tmp_path), role="review", spec_snapshot=snap)

    adapter._observe_tick(_dev_handle(), spec)

    assert adapter._fm_transition_obs == {}
    assert _lifecycle_events(adapter, "spec-status-transition-observed") == []


def test_observe_tick_ignores_bare_null_status(tmp_path):
    """A bare `status:` line parses to YAML null, and BOTH sides of the comparison
    read it through `status_of` — the tick here, and the launch snapshot
    `_reset_spec_for_review` captures (pinned by its sibling,
    `test_review_launch_snapshot_reads_bare_status_as_blank`). It normalizes to ""
    on both, so the `s != ""` guard drops it and no transition is fabricated.

    Written as its own case rather than a row on the parametrize above: expressing
    "a bare status line" through that helper's `f"status: {on_disk}"` template needs
    a sentinel, and a sentinel is exactly how the #358 parametrize silently
    collapsed its YAML-null row into the missing-key one."""
    adapter, impl = make_dev_adapter(tmp_path)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text("---\nstatus:\nbaseline_revision: abc123\n---\n\n# Story\n\nbody\n")
    # fm_status="" is what `_reset_spec_for_review` records for this spec.
    snap = dataclasses.replace(_snap(spec_file), fm_status="")
    spec = dataclasses.replace(_dev_spec(tmp_path), role="review", spec_snapshot=snap)

    adapter._observe_tick(_dev_handle(), spec)

    assert adapter._fm_transition_obs == {}
    assert _lifecycle_events(adapter, "spec-status-transition-observed") == []


def test_observe_tick_without_snapshot_or_unreadable_is_noop(tmp_path, monkeypatch):
    """No snapshot (every non-review session) is a pure no-op, and a torn/
    unreadable snapshot read (OSError) is a skipped sample — never a verdict —
    so neither records a transition or a crumb."""
    adapter, impl = make_dev_adapter(tmp_path)

    # (a) no snapshot at all: returns before reading anything
    adapter._observe_tick(_dev_handle(), _dev_spec(tmp_path))
    assert adapter._fm_transition_obs == {}

    # (b) a snapshot whose path raises on read: the sampling path swallows OSError
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text("---\nstatus: in-review\n---\n\n# Story\n\nx\n")
    spec = _snapshotted_spec(tmp_path, spec_file)

    def boom(_path):
        raise OSError("torn read")

    monkeypatch.setattr(generic, "read_frontmatter", boom)
    adapter._observe_tick(_dev_handle(), spec)
    assert adapter._fm_transition_obs == {}
    assert _lifecycle_lines(adapter) == []


def test_frontmatter_fallback_transition_single_sighting_harvest(tmp_path, monkeypatch):
    """A recorded transition + a terminal frontmatter whose bytes differ from the
    snapshot: the FIRST wait=True pass harvests (no 2-obs wait), the result is
    marked synthesized, the synth crumb carries transition=True, and no
    `terminal-frontmatter-pending` breadcrumb is left."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nLaunch.\n"
    )
    spec = _snapshotted_spec(tmp_path, spec_file)  # snapshot of the launch bytes
    # the review actually ran: terminal spec written, bytes differ from launch
    spec_file.write_text(_MARKERLESS_DONE)
    adapter._fm_transition_obs["3-1-dev-1"] = "in-review"

    rj = adapter._result_json(_dev_handle(), spec, wait=True)

    assert rj is not None and rj["synthesized_from_frontmatter"] is True
    assert _breadcrumbs(adapter) == []  # single-sighting: no pending crumb
    synth = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "frontmatter-synthesized"]
    assert len(synth) == 1 and synth[0]["transition"] is True


def test_frontmatter_fallback_missed_transition_stays_conservative(tmp_path, monkeypatch):
    """Snapshot present, bytes differ, but NO transition was recorded (the flip
    happened between ticks): the fallback keeps the conservative 2-observation
    fingerprint — pending on the first stable Stop, harvested on the second with
    transition=False."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nLaunch.\n"
    )
    spec = _snapshotted_spec(tmp_path, spec_file)
    spec_file.write_text(_MARKERLESS_DONE)  # bytes differ; transition unrecorded
    assert adapter._fm_transition_obs == {}

    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "terminal-frontmatter-pending"

    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["synthesized_from_frontmatter"] is True
    synth = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "frontmatter-synthesized"]
    assert len(synth) == 1 and synth[0]["transition"] is False


def test_transition_beats_hash_gate(tmp_path, monkeypatch):
    """A recorded transition (#276 M2) OUTRANKS the M1 hash gate: a clean review that
    round-tripped `done -> in-review -> done` back to byte-identical launch bytes
    while omitting its marker still harvests — the observed transition proves it ran.
    No `unmodified-since-launch` refusal; one `frontmatter-synthesized` crumb marked
    transition=True."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    spec = _snapshotted_spec(tmp_path, spec_file)  # snapshot == current bytes
    adapter._fm_transition_obs["3-1-dev-1"] = "in-review"  # a transition WAS seen

    rj = adapter._result_json(_dev_handle(), spec, wait=True)
    assert rj["synthesized_from_frontmatter"] is True
    assert _breadcrumbs(adapter) == []  # a harvest leaves no give-up breadcrumb
    synth = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "frontmatter-synthesized"]
    assert len(synth) == 1 and synth[0]["transition"] is True


def test_frontmatter_fallback_alias_path_recognized_as_same_spec(tmp_path, monkeypatch):
    """#5: snapshot/candidate identity is filesystem-based, not lexical. A snapshot
    recorded under a non-normalized alias of the candidate (a `..` detour to the same
    file) is still recognized as the same spec, so the M1 hash gate fires on a
    byte-identical unmodified spec instead of wrongly harvesting (a raw string compare
    would miss the alias)."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    alias = impl / ".." / impl.name / "spec-3-1-foo.md"  # same file, different spelling
    assert alias.resolve() == spec_file.resolve()
    snap = dataclasses.replace(_snap(spec_file), path=str(alias))
    spec = dataclasses.replace(_dev_spec(tmp_path), role="review", spec_snapshot=snap)

    assert adapter._result_json(_dev_handle(), spec, wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "unmodified-since-launch"


def test_wait_loop_heartbeat_drives_observe_tick(tmp_path, monkeypatch):
    """The wait loop invokes _observe_tick inside the heartbeat-throttled block:
    the first tick always fires (last_heartbeat is None) and each later tick a
    HEARTBEAT_INTERVAL_S apart fires again, always with the session's handle."""
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 100.0  # let several idle ticks pass before the stall
    adapter._stall_nudges = 0
    adapter._window_alive = lambda handle: True

    observed: list[str] = []
    monkeypatch.setattr(
        adapter, "_observe_tick", lambda handle, spec: observed.append(handle.task_id)
    )

    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)  # frozen wall clock: the co-bound never fires
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)

    def advance(call_n):
        if call_n >= 2:  # each idle tick crosses one HEARTBEAT_INTERVAL_S (30s)
            clock["t"] += 31.0

    adapter.watcher = _ScriptedWatcher(
        [_stop_event("3-1-dev-1", "sess", "/run/events.jsonl")],  # then None forever
        on_call=advance,
    )
    result = adapter.wait_for_completion(_dev_handle(), _dev_spec(tmp_path))

    assert result.status == "stalled"
    # the first tick plus at least one later heartbeat crossing fired the hook
    assert len(observed) >= 2 and all(tid == "3-1-dev-1" for tid in observed)


# ------------------------------ contract nudge (#276 M4)
#
# On the FIRST `terminal-frontmatter-pending` observation (a Stop that found the
# spec finalized to a terminal frontmatter status but missing its `## Auto Run
# Result` marker), the dev adapter sends ONE targeted nudge asking the skill to
# append the section it owed — repairing the omission at the source. Sent exactly
# once per session via a never-cleared set (marked before the send), gated by
# `limits.dev_contract_nudge`, and touching no stall counters. Frontmatter
# synthesis stays the backstop for a session that never complies.


def _record_sent(adapter):
    sent: list[str] = []
    adapter.send_text = lambda handle, text: sent.append(text)
    return sent


def test_contract_nudge_sent_on_first_pending_observation(tmp_path, monkeypatch):
    """The first pending observation sends exactly the formatted nudge, journals a
    `contract-nudge-sent` crumb, and still records the `terminal-frontmatter-
    pending` verdict — the nudge is additive, not a replacement for the fallback."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)

    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None

    assert sent == [generic.CONTRACT_NUDGE_TEXT.format(spec_path=spec_file, status="done")]
    (crumb,) = _lifecycle_events(adapter, "contract-nudge-sent")
    assert crumb["spec"] == str(spec_file) and crumb["status"] == "done"
    (verdict,) = _breadcrumbs(adapter)
    assert verdict["verdict"] == "terminal-frontmatter-pending"
    assert adapter._contract_nudge_sent == {"3-1-dev-1"}


def test_contract_nudge_exactly_once_across_mtime_reset(tmp_path, monkeypatch):
    """#149's refill hazard cannot apply: an mtime bump resets the observation
    counter to 1, but the nudge budget is the never-cleared set, so a second
    first-observation Stop over the same session sends nothing more."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    os.utime(spec_file, ns=(1_000_000_000, 1_000_000_000))

    # first pending observation -> one nudge
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    assert len(sent) == 1
    # the session wrote again: fingerprint changes, observation count resets to 1
    os.utime(spec_file, ns=(2_000_000_000, 2_000_000_000))
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None

    assert len(sent) == 1  # not re-nudged despite observations back at 1
    assert len(_lifecycle_events(adapter, "contract-nudge-sent")) == 1


def test_contract_nudge_not_sent_when_transition_proven(tmp_path, monkeypatch):
    """A recorded mid-session transition (#276 M2) harvests on the first sighting,
    so the record-obs branch — and its nudge — is never reached."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nLaunch.\n"
    )
    spec = _snapshotted_spec(tmp_path, spec_file)
    spec_file.write_text(_MARKERLESS_DONE)  # bytes differ from the launch snapshot
    adapter._fm_transition_obs["3-1-dev-1"] = "in-review"

    rj = adapter._result_json(_dev_handle(), spec, wait=True)

    assert rj is not None and rj["synthesized_from_frontmatter"] is True
    assert sent == []
    assert _lifecycle_events(adapter, "contract-nudge-sent") == []


def test_contract_nudge_not_sent_on_ambiguous(tmp_path, monkeypatch):
    """Several marker-less candidates refuse to guess before the fingerprint/nudge
    branch, so no nudge is sent."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)
    (impl / "spec-3-2-bar.md").write_text(_MARKERLESS_DONE)

    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None

    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "ambiguous-frontmatter"
    assert sent == []
    assert _lifecycle_events(adapter, "contract-nudge-sent") == []


def test_contract_nudge_not_sent_on_dead_window(tmp_path):
    """A dead-window post-kill reconcile synthesizes on a single sighting via the
    wait=False path, which never reaches the nudge (wait=True only)."""
    adapter, impl = make_dev_adapter(tmp_path)
    adapter._window_alive = lambda handle: False
    sent = _record_sent(adapter)
    (impl / "spec-3-1-foo.md").write_text(_MARKERLESS_DONE)

    result = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), _unvouched())

    assert result.status == "completed"  # synthesized on the dead window
    assert sent == []
    assert _lifecycle_events(adapter, "contract-nudge-sent") == []


def test_contract_nudge_not_sent_on_unmodified_refusal(tmp_path, monkeypatch):
    """The M1 hash gate refuses a byte-identical spec before the fingerprint/nudge
    branch: a spec provably untouched by this session is not this session's to
    repair, so no nudge fires."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)
    spec = _snapshotted_spec(tmp_path, spec_file)  # snapshot == current bytes

    assert adapter._result_json(_dev_handle(), spec, wait=True) is None

    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "unmodified-since-launch"
    assert sent == []
    assert _lifecycle_events(adapter, "contract-nudge-sent") == []


def test_contract_nudge_send_failure_marks_sent(tmp_path, monkeypatch):
    """A raising transport still satisfies exactly-once: the task is marked (and
    the crumb journaled) BEFORE the send, so a `MultiplexerError` is swallowed and
    the next Stop attempts no retry."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    calls = {"n": 0}

    def boom(handle, text):
        calls["n"] += 1
        raise generic.MultiplexerError("transport down")

    adapter.send_text = boom
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)

    # first observation: nudge attempted, raises, swallowed (no crash)
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    assert calls["n"] == 1
    assert adapter._contract_nudge_sent == {"3-1-dev-1"}
    assert len(_lifecycle_events(adapter, "contract-nudge-sent")) == 1  # marked before the send

    # second stable Stop: task already marked -> no retry, and it harvests
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["synthesized_from_frontmatter"] is True
    assert calls["n"] == 1  # never re-sent


def test_contract_nudge_disabled_by_policy(tmp_path, monkeypatch):
    """Knob off: no send, no `contract-nudge-sent` crumb, and the fallback behaves
    exactly as it did pre-M4 (pending crumb recorded, harvest on the second Stop)."""
    adapter, impl = make_dev_adapter(
        tmp_path, policy=Policy(limits=LimitsPolicy(dev_contract_nudge=False))
    )
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)

    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    assert sent == []
    assert _lifecycle_events(adapter, "contract-nudge-sent") == []
    assert adapter._contract_nudge_sent == set()
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "terminal-frontmatter-pending"
    # pre-M4 behavior intact: harvest on the second stable Stop
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["synthesized_from_frontmatter"] is True


def test_wait_loop_contract_nudge_then_skill_appends_marker(tmp_path, monkeypatch):
    """End to end: Stop 1 finds a marker-less terminal spec and nudges; the skill
    complies by appending its `## Auto Run Result` section before Stop 2; Stop 2
    then completes via the NORMAL marker scan — no synthesis, no
    `synthesized_from_frontmatter` flag, and no second nudge."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    sent = _record_sent(adapter)
    spec_file = impl / "spec-3-1-foo.md"
    spec_file.write_text(_MARKERLESS_DONE)

    # Stop 1: marker-less -> pending + one nudge
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True) is None
    assert sent == [generic.CONTRACT_NUDGE_TEXT.format(spec_path=spec_file, status="done")]

    # the skill heeds the nudge and appends its required marker section
    spec_file.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\nAll done.\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented the thing.\n"
    )

    # Stop 2: harvested by the ordinary scan, not the fallback
    rj = adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=True)
    assert rj["status"] == "done"
    assert "synthesized_from_frontmatter" not in rj
    assert len(sent) == 1  # no second nudge


# ------------------------------- authoritative-path read-back (#261)
#
# The read-back located "the artifact this session produced" by scanning the
# implementation-artifacts dir for the newest qualifying `*.md`. Under worktree
# isolation that search also covers the MAIN checkout's dir, which every
# concurrent run shares; with isolation="none" it IS that shared dir. So a
# foreign story's spec landing there after launch (a parallel run's merge-back,
# a human edit, a sweep) won on mtime and was adopted as this session's result:
# a review that produced nothing was scored `completed:done` and unreviewed code
# merged.
#
# Where the orchestrator already knows which spec the session owes — every review
# leg and every dev retry, via StoryTask.spec_file, which it literally hands the
# session in its own prompt — SessionSpec.expected_spec pins the read-back to
# that one file and the scan is never reached.

_FOREIGN_DONE = (
    "---\nstatus: done\nbaseline_revision: deadbeef\n---\n\n"
    "# Someone else's story\n\n## Auto Run Result\n\nStatus: done\nImplemented.\n"
)


def _expecting(tmp_path, spec_file: Path, story_key="3-1") -> SessionSpec:
    """A review SessionSpec pinned to the spec the orchestrator recorded (#261),
    carrying its launch snapshot too — exactly what the engine threads for a
    review leg whose story already has a spec_file."""
    return dataclasses.replace(
        _snapshotted_spec(tmp_path, spec_file, story_key), expected_spec=str(spec_file)
    )


def test_expected_spec_ignores_foreign_marker_spec(tmp_path, monkeypatch):
    """The #261 regression, marker path. Our own spec sits stripped of its marker
    (the #160 pre-review-launch strip) and the session died without rewriting it; a
    FOREIGN story's finished spec is newer. The scan would return the foreign spec
    and score it as this story's `done`."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n")
    foreign = impl / "spec-9-9-someone-elses-story.md"
    foreign.write_text(_FOREIGN_DONE)
    os.utime(foreign, ns=(2_000_000_000, 2_000_000_000))  # newest: the scan would win here

    spec = _expecting(tmp_path, ours)
    assert adapter._result_json(_dev_handle(), spec, wait=False) is None
    # ...and the unpinned spec is exactly what the bug looked like.
    assert adapter._result_json(_dev_handle(), _dev_spec(tmp_path), wait=False) is not None


def test_expected_spec_never_reaches_the_scan(tmp_path, monkeypatch):
    """Structural: with expected_spec set, the directory scan is not merely
    out-voted, it is never called."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)

    def boom(*a, **k):
        raise AssertionError("find_result_artifact must not run with expected_spec set")

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", boom)
    monkeypatch.setattr(generic.devcontract, "find_frontmatter_candidates", boom)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: done\n---\n\n# Story\n")
    (impl / "spec-9-9-someone-elses-story.md").write_text(_FOREIGN_DONE)
    assert adapter._result_json(_dev_handle(), _expecting(tmp_path, ours), wait=False) is None


def test_expected_spec_synthesizes_its_own_marker_spec(tmp_path, monkeypatch):
    """Over-refusal guard: when THIS session wrote the spec it owed, the pinned
    read-back synthesizes exactly as the scan did — and stamps the session's own
    story key, not the foreign spec's."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n")
    spec = _expecting(tmp_path, ours)  # snapshot taken of the stripped spec
    (impl / "spec-9-9-someone-elses-story.md").write_text(_FOREIGN_DONE)
    ours.write_text(  # the review then finishes and appends its marker
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n# Story\n\n"
        "## Auto Run Result\n\nStatus: done\nReviewed.\n"
    )
    rj = adapter._result_json(_dev_handle(), spec, wait=False)
    assert rj is not None and rj["status"] == "done"
    assert rj["story_key"] == "3-1"
    assert rj["spec_file"] == str(ours)


def test_expected_spec_ignores_foreign_markerless_spec(tmp_path, monkeypatch):
    """Same regression through the #224 missing-marker fallback, which #261 predates
    but which added a second identical mtime-only scan of the shared dir. A foreign
    marker-less `status: done` spec must not be a candidate at all — under a dead
    window it would otherwise synthesize on a single sighting."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: in-review\n---\n\n# Story\n")  # non-terminal: still working
    (impl / "spec-9-9-someone-elses-story.md").write_text(_MARKERLESS_DONE)

    spec = _expecting(tmp_path, ours)
    assert adapter._synth_result(_dev_handle(), spec, wait=False, dead_window=True) is None
    # Unpinned, the foreign spec is adopted on the dead window's single sighting.
    unpinned = dataclasses.replace(_dev_spec(tmp_path), role="review")
    assert adapter._synth_result(_dev_handle(), unpinned, wait=False, dead_window=True) is not None


def test_expected_spec_markerless_own_spec_still_synthesizes(tmp_path, monkeypatch):
    """Over-refusal guard for the fallback: our OWN marker-less terminal spec is
    still harvested under a dead window, and the M1/M2 gates still apply to it."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: in-review\n---\n\n# Story\n")
    spec = _expecting(tmp_path, ours)  # snapshot of the in-review bytes
    ours.write_text(_MARKERLESS_DONE)  # this session finalized it, marker omitted
    sr = adapter._synth_result(_dev_handle(), spec, wait=False, dead_window=True)
    assert sr is not None and sr.result_json["status"] == "done"
    assert sr.result_json["synthesized_from_frontmatter"] is True


def test_expected_spec_absent_file_is_no_result(tmp_path, monkeypatch):
    """A session that never wrote the spec it owed produced no result — and the
    pinned read-back deliberately does NOT fall back to the scan, so a foreign
    artifact cannot stand in for it."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-9-9-someone-elses-story.md").write_text(_FOREIGN_DONE)
    spec = dataclasses.replace(
        _dev_spec(tmp_path), role="review", expected_spec=str(impl / "spec-3-1-foo.md")
    )
    assert adapter._result_json(_dev_handle(), spec, wait=False) is None


def test_expected_spec_breadcrumb_names_the_pinned_path(tmp_path, monkeypatch):
    """The give-up crumb points at the spec that was owed, not at a directory list —
    the diagnostic that would have made #261 obvious in the journal."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text("---\nstatus: in-review\n---\n\n# Story\n")
    (impl / "spec-9-9-someone-elses-story.md").write_text(_FOREIGN_DONE)
    assert adapter._result_json(_dev_handle(), _expecting(tmp_path, ours), wait=True) is None
    (crumb,) = _breadcrumbs(adapter)
    assert crumb["verdict"] == "no-artifact"
    assert str(ours) in crumb["detail"]
    assert "someone-elses" not in crumb["detail"]


def test_dev_attempt_one_keeps_the_scan(tmp_path, monkeypatch):
    """Unchanged where it must be: a dev attempt 1 has no recorded spec yet (the
    skill creates it), so expected_spec is None and the mtime scan still runs."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    spec = _dev_spec(tmp_path)
    assert spec.expected_spec is None
    rj = adapter._result_json(_dev_handle(), spec, wait=False)
    assert rj is not None and rj["status"] == "done"


# ------------------------------- proof-of-work gate (#261)


def _pane_log(adapter, task_id: str, size: int) -> Path:
    adapter.logs_dir.mkdir(parents=True, exist_ok=True)
    log = adapter.logs_dir / f"{task_id}.log"
    log.write_bytes(b"x" * size)
    return log


def test_proof_of_work_refuses_silent_dead_session(tmp_path, monkeypatch):
    """A dead session with no hook event and a pane log that never grew produced
    nothing, so a read-back artifact is not its output. Keep the crash verdict."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    _pane_log(adapter, "3-1-dev-1", 0)
    res = adapter._final(_dev_handle(), _dev_spec(tmp_path), "crashed", None, None)
    assert res.status == "crashed"
    assert res.result_json is None


def test_proof_of_work_two_byte_log_is_not_work(tmp_path, monkeypatch):
    """The floor is not zero: the report's wedged windows left 0-byte AND 2-byte
    logs, so `size > 0` would have cleared one of them."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    _pane_log(adapter, "3-1-dev-1", 2)
    assert adapter._final(_dev_handle(), _dev_spec(tmp_path), "crashed", None, None).status == (
        "crashed"
    )


def test_proof_of_work_pane_log_growth_is_evidence(tmp_path, monkeypatch):
    """A session that rendered to its pane ran, so its artifact is honoured."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    _pane_log(adapter, "3-1-dev-1", generic.PROOF_OF_WORK_MIN_LOG_BYTES + 1)
    res = adapter._final(_dev_handle(), _dev_spec(tmp_path), "crashed", None, None)
    assert res.status == "completed"
    assert res.result_json["status"] == "done"


def test_proof_of_work_ended_turn_is_evidence_despite_empty_log(tmp_path, monkeypatch):
    """The two signals are ORed for a reason: on a misbound pane sink (#254/#217) a
    HEALTHY session logs zero bytes. A turn having ENDED carries it."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    _pane_log(adapter, "3-1-dev-1", 0)
    res = adapter._final(
        _dev_handle(), _dev_spec(tmp_path), "crashed", "sess-1", None, stop_seen=True
    )
    assert res.status == "completed"


def test_proof_of_work_session_id_alone_is_not_evidence(tmp_path, monkeypatch):
    """`session_id`/`transcript_path` are populated by SessionStart and SessionEnd,
    which a CLI that launched and wedged emits without doing anything. Reading them
    as proof left the gate satisfied in exactly the case it exists to catch (#298
    review): only `stop_seen` — a turn that ENDED — is the hook-side evidence."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-9-9-someone-elses-story.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    _pane_log(adapter, "3-1-dev-1", 0)
    res = adapter._final(
        _dev_handle(),
        _dev_spec(tmp_path),
        "crashed",
        "sess-1",  # SessionStart handed us a session id...
        "/t.jsonl",  # ...and a transcript path. Neither means work happened.
        stop_seen=False,
    )
    assert res.status == "crashed"
    assert res.result_json is None


def test_proof_of_work_no_pane_log_is_inert(tmp_path, monkeypatch):
    """Unknown never blocks: with no pane log at all there is no signal, and the
    gate preserves the previous behavior exactly. Reachable only for a handle this
    adapter never launched — `start_session` always leaves a log behind (see the
    dead-on-arrival test below), which is what keeps this state from swallowing the
    case the gate is for."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    assert not (adapter.logs_dir / "3-1-dev-1.log").exists()
    res = adapter._final(_dev_handle(), _dev_spec(tmp_path), "crashed", None, None)
    assert res.status == "completed"


def test_proof_of_work_dead_on_arrival_window_is_refused(tmp_path, monkeypatch):
    """A window that dies before `pipe_pane` attaches tees NOTHING, so before #298
    it left no log file at all — the inert state above — and the gate failed open on
    precisely the dead-on-arrival session it exists to refuse. `start_session` now
    creates the log empty up front, so "no tee ever attached" reports as `False`
    (rendered nothing) rather than `None` (no pane signal here).

    Driven through the REAL `start_session` with a mux whose `pipe_pane` writes
    nothing — the faithful stand-in for attaching a tee to a corpse."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    adapter._ensure_session = lambda cwd: None  # skip the tmux server plumbing
    adapter.mux = _StartSessionMux()

    handle = adapter.start_session(_dev_spec(tmp_path))
    assert (adapter.logs_dir / "3-1-dev-1.log").stat().st_size == 0

    # The foreign spec must land AFTER launch, or its mtime sits below the
    # `since_ns` floor, the scan discards it there, and the gate is never reached —
    # the test would pass with the gate removed (verified by ablation: it did).
    #
    # Writing it "after start_session returned" is not enough to establish that on
    # Windows: `launched_ns` comes from `time.time_ns()` (a precise clock) while an
    # NTFS mtime is stamped from the coarse system-time tick (~15.6 ms), so a file
    # written a millisecond later can carry an mtime BELOW the floor. Re-stamp until
    # the precondition actually holds rather than assuming the two clocks agree.
    foreign = impl / "spec-9-9-someone-elses-story.md"
    deadline = time.monotonic() + 5.0
    while True:
        foreign.write_text(
            "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
            "## Auto Run Result\n\nStatus: done\nImplemented.\n"
        )
        if devcontract.is_result_artifact(foreign, since_ns=handle.launched_ns):
            break
        assert time.monotonic() < deadline, "foreign spec never cleared the launch floor"
        time.sleep(0.02)

    res = adapter._final(handle, _dev_spec(tmp_path), "crashed", None, None)
    assert res.status == "crashed"
    assert res.result_json is None


def test_proof_of_work_leaves_task_scoped_result_json_alone(tmp_path):
    """The gate is scoped to the shared-directory read-back, not to `_final` at
    large (#298 review). A base adapter's `tasks/<task_id>/result.json` is unique to
    this task and unlinked at launch, so its presence already proves THIS session
    wrote it — no foreign writer can reach it. Gating it could only ever discard an
    authoritative completion, so `_ResultFileMixin` declines the gate outright.
    `_UnitMux` keeps the crash-verdict `_final` call off the host multiplexer —
    the read-back upgrade skips the probe today, but that must not be what this
    test leans on."""
    adapter = make_adapter(tmp_path, mux=_UnitMux())
    task_dir = adapter.tasks_dir / "1-1-a-dev-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text('{"status": "done", "workflow": "dev"}')
    _pane_log(adapter, "1-1-a-dev-1", 0)  # no rendering, and no Stop below

    handle = SessionHandle(task_id="1-1-a-dev-1", native_id="@1")
    res = adapter._final(handle, make_spec(tmp_path), "crashed", None, None)
    assert res.status == "completed"
    assert res.result_json["status"] == "done"


def test_proof_of_work_gates_post_kill_reconcile(tmp_path, monkeypatch):
    """The post-kill call path: the rescue is for a session that finished and
    lost its Stop, not for one that never ran."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    _pane_log(adapter, "3-1-dev-1", 0)
    stalled = SessionResult(status="stalled")
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), stalled) is stalled

    _pane_log(adapter, "3-1-dev-1", generic.PROOF_OF_WORK_MIN_LOG_BYTES + 1)
    rescued = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), stalled)
    assert rescued.status == "completed"
    assert rescued.result_json["post_kill_reconciled"] is True


def test_proof_of_work_gates_the_aborted_rescue(tmp_path, monkeypatch):
    """The #261 gate covers the abort leg (#319) exactly as it covers the others:
    a session a hard stop killed before it did anything produced nothing, so a
    qualifying artifact on disk is not its output and the `aborted` verdict
    stands. With proof-of-work the same artifact rescues it.

    Ablation: delete the `_produced_work` gate in `_post_kill_reconcile` and the
    first arm reddens with a `completed` rescue.
    """
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    adapter._window_alive = lambda handle: False
    (impl / "spec-3-1-foo.md").write_text(_DONE_SPEC)
    _pane_log(adapter, "3-1-dev-1", 0)
    aborted = SessionResult(status="aborted")
    assert adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), aborted) is aborted

    _pane_log(adapter, "3-1-dev-1", generic.PROOF_OF_WORK_MIN_LOG_BYTES + 1)
    rescued = adapter._post_kill_reconcile(_dev_handle(), _dev_spec(tmp_path), aborted)
    assert rescued.status == "completed"
    assert rescued.result_json["post_kill_reconciled"] is True


def test_proof_of_work_journals_the_refusal(tmp_path, monkeypatch):
    """The refusal is observable — a silent downgrade would be its own #261."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    (impl / "spec-3-1-foo.md").write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    )
    _pane_log(adapter, "3-1-dev-1", 0)
    adapter._final(_dev_handle(), _dev_spec(tmp_path), "crashed", None, None)
    events = (adapter.tasks_dir / "3-1-dev-1" / "session-lifecycle.jsonl").read_text()
    assert "readback-refused-no-proof-of-work" in events


def test_expected_spec_relative_path_is_rebased_on_cwd(tmp_path, monkeypatch):
    """A relative expected_spec resolves against the session cwd, not the process
    CWD. The engine always threads an absolute path, so this is a guard against a
    future caller quietly turning the #261 fix into a work-losing false refusal."""
    adapter, impl = make_dev_adapter(tmp_path)
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    ours = impl / "spec-3-1-foo.md"
    ours.write_text(
        "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
        "## Auto Run Result\n\nStatus: done\nImplemented.\n"
    )
    spec = dataclasses.replace(
        _dev_spec(tmp_path), role="review", expected_spec=str(ours.relative_to(tmp_path))
    )
    rj = adapter._result_json(_dev_handle(), spec, wait=False)
    assert rj is not None and rj["spec_file"] == str(ours)


def test_log_evidence_mro_is_not_shadowed_by_the_mixin():
    """`_log_evidence` is declared inert on `_ResultFileMixin` and overridden on
    `GenericAdapter`, which owns the pane log. Both mixins sit ahead of the concrete
    adapter in the MRO, and this file already documents that hazard for `send_text` —
    so pin the resolution: if a future base reshuffle let the inert stub win, the
    proof-of-work gate would silently go dead everywhere instead of failing loudly.
    OpencodeDevAdapter has no pane log and must keep the inert one."""
    from bmad_loop.adapters.generic import GenericAdapter, _ResultFileMixin
    from bmad_loop.adapters.opencode_http import OpencodeDevAdapter

    assert GenericDevAdapter._log_evidence is GenericAdapter._log_evidence
    assert OpencodeDevAdapter._log_evidence is _ResultFileMixin._log_evidence
    # Same hazard, same reason: the gate applies to the shared-directory read-back
    # the dev mixin owns and to nothing else. An MRO that let the base default win
    # for a dev adapter would silently disarm it; one that let the dev value leak
    # onto a base adapter would start discarding authoritative task-scoped results.
    assert GenericDevAdapter._READBACK_NEEDS_PROOF_OF_WORK is True
    assert OpencodeDevAdapter._READBACK_NEEDS_PROOF_OF_WORK is True
    assert GenericTmuxAdapter._READBACK_NEEDS_PROOF_OF_WORK is False


def test_classify_env_fault_marks_a_dropped_suffix(tmp_path):
    """A window that dropped a SUFFIX says so. Marking only the head made a
    truncated excerpt read as a complete line that simply ended there — the one
    thing a string an operator reads out of a pause reason must not do. Both
    markers are spent from ENV_FAULT_EVIDENCE_MAX, never added on top of it."""
    adapter = make_adapter(tmp_path)
    lead = "y" * 300  # push the match past the head so both ends are cut
    _write_task_log(
        adapter, f"{lead} API Error: Connection closed mid-response {'z' * 400}\n".encode()
    )
    result = _classify(adapter, "timeout")
    assert result.env_fault is True
    ev = result.env_fault_evidence
    assert ev.startswith("…") and ev.endswith("…")
    assert len(ev) <= generic.ENV_FAULT_EVIDENCE_MAX
    assert "API Error: Connection closed mid-response" in ev


def test_classify_env_fault_drops_the_partial_line_at_the_tail_seek(tmp_path):
    """The 64 KiB tail seek lands on an arbitrary byte, so whatever line straddles
    the window edge arrives as a fragment. Matching it would quote half a line as
    evidence — and because the cut can land mid-codepoint, its head may be a U+FFFD
    from the errors="replace" decode. The fragment is discarded."""
    adapter = make_adapter(tmp_path)
    # The byte layout is the whole test, so it is computed rather than guessed: the
    # seek must land INSIDE the matching line, and far enough into its leading junk
    # that the surviving fragment would STILL match. Otherwise the test passes for
    # the wrong reason — an earlier version padded so heavily that the matching line
    # fell entirely outside the 64 KiB window, so it went green with the
    # fragment-drop deleted and proved nothing.
    prefix = b"P" * 50
    straddler = b"J" * 200 + b"API Error: Connection closed mid-response\n"  # 242 bytes
    cut_into_line = 10
    filler = b"filler line\n"  # 12 bytes, matches nothing
    tail_bytes = generic.ENV_FAULT_TAIL_BYTES - len(straddler) + cut_into_line
    assert tail_bytes % len(filler) == 0  # exact fill; no accidental re-alignment
    _write_task_log(adapter, prefix + straddler + filler * (tail_bytes // len(filler)))

    log = adapter.logs_dir / f"{_ENV_FAULT_TASK}.log"
    size = log.stat().st_size
    seek = size - generic.ENV_FAULT_TAIL_BYTES
    assert size > generic.ENV_FAULT_TAIL_BYTES  # the tail read actually truncates
    assert len(prefix) < seek < len(prefix) + len(straddler)  # cut is inside the line
    # And the surviving fragment still carries the full pattern, so dropping it is
    # the ONLY reason this must not classify.
    assert b"API Error: Connection closed mid-response" in log.read_bytes()[seek:]

    result = _classify(adapter, "timeout")
    assert result.env_fault is False
    assert result.env_fault_evidence is None


def test_classify_env_fault_keeps_a_boundary_aligned_first_line(tmp_path):
    """Sibling of the test above, and its counterexample: when the 64 KiB seek
    happens to land on the first byte AFTER a newline, the first element is a
    COMPLETE line, not a straddling fragment. Discarding it on "the read
    truncated" alone loses a whole line — and if that line held the only
    provider-error match in the tail, the outage goes unclassified and the run
    burns a story attempt. The classifier looks at the byte before the window to
    tell the two cases apart."""
    adapter = make_adapter(tmp_path)
    # As with the straddler test, the byte layout IS the test, so it is computed
    # and then asserted: the seek must land exactly on len(prefix), i.e. one past
    # the prefix's terminating newline, and the matching line must be the only
    # match anywhere in the file.
    prefix = b"P" * 50 + b"\n"  # 51 bytes, falls outside the window
    match_line = b"J" * 10 + b"API Error: Connection closed mid-response\n"  # 52 bytes
    filler = b"filler line\n"  # 12 bytes, matches nothing
    fill = generic.ENV_FAULT_TAIL_BYTES - len(match_line)
    assert fill % len(filler) == 0  # exact fill; the seek lands where we computed
    _write_task_log(adapter, prefix + match_line + filler * (fill // len(filler)))

    log = adapter.logs_dir / f"{_ENV_FAULT_TASK}.log"
    body = log.read_bytes()
    seek = len(body) - generic.ENV_FAULT_TAIL_BYTES
    assert len(body) > generic.ENV_FAULT_TAIL_BYTES  # the tail read actually truncates
    assert seek == len(prefix)  # window opens exactly on a line boundary...
    assert body[seek - 1 : seek] == b"\n"  # ...one byte past the terminator
    assert body[seek : seek + len(match_line)] == match_line  # whole line, not a fragment
    assert body.count(b"API Error") == 1  # it is the ONLY match in the log

    result = _classify(adapter, "timeout")
    assert result.env_fault is True
    # Equality, not containment: the surviving line is the whole line, so this
    # also fails if the fix ever kept a fragment instead.
    assert result.env_fault_evidence == "J" * 10 + "API Error: Connection closed mid-response"


def test_classify_env_fault_scans_a_truncated_window_with_no_newline(tmp_path):
    """A >tail-window log containing NO newline is a single fragment. Dropping it
    as a straddler would leave nothing to scan — trading a cosmetic half-line for
    a missed outage, which is the wrong way round. The fragment is scanned; the
    leading ellipsis marks that it is a window, not a whole line."""
    adapter = make_adapter(tmp_path)
    hit = b"API Error: Connection closed mid-response"
    _write_task_log(adapter, b"x" * (generic.ENV_FAULT_TAIL_BYTES + 10) + hit)
    result = _classify(adapter, "timeout")
    assert result.env_fault is True
    assert result.env_fault_evidence.startswith("…")
    assert "API Error: Connection closed mid-response" in result.env_fault_evidence


@pytest.mark.parametrize("terminator", [b"\n", b"\r"], ids=["lf", "cr"])
def test_classify_env_fault_scans_a_single_oversized_terminated_line(tmp_path, terminator):
    """The same "leaves nothing to scan" case, one byte different — and the one
    the length test got wrong. A >tail-window log that IS one line but ends with a
    terminator splits to [fragment, ""], so a `len(lines) > 1` guard reads it as
    "there is more to scan", drops the fragment, and scans only "". The window has
    to be judged by whether anything SURVIVES the drop, not by how many pieces the
    split produced. \\r counts: pane captures are CR-terminated and \\r is
    normalized to \\n before the split."""
    adapter = make_adapter(tmp_path)
    hit = b"API Error: Connection closed mid-response"
    _write_task_log(adapter, b"x" * (generic.ENV_FAULT_TAIL_BYTES + 10) + hit + terminator)

    log = adapter.logs_dir / f"{_ENV_FAULT_TASK}.log"
    body = log.read_bytes()
    seek = len(body) - generic.ENV_FAULT_TAIL_BYTES
    assert seek > 0  # the tail read actually truncates
    window = body[seek:]
    # The window is ONE line plus its terminator, so the split yields exactly
    # [fragment, ""] — the layout that makes a length test the wrong question.
    assert window.count(b"\n") + window.count(b"\r") == 1
    assert window.endswith(terminator)
    assert hit in window  # the only match survives the seek, and only as the fragment

    result = _classify(adapter, "timeout")
    assert result.env_fault is True
    assert result.env_fault_evidence.startswith("…")
    assert "API Error: Connection closed mid-response" in result.env_fault_evidence
