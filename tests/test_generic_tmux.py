"""GenericTmuxAdapter tests.

Unit tests need no tmux. The integration tests drive a REAL tmux session but
substitute a tiny shell script for the CLI binary: the script writes
result.json and emits hook-style event files itself (canonical event names,
exactly what each CLI's hook registration produces), exercising spawn / env
propagation / hook-signal waiting / kill end-to-end for any profile.
"""

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bmad_loop.adapters import generic, tmux_base
from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec
from bmad_loop.adapters.generic import GenericDevAdapter, GenericTmuxAdapter
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.profile import get_profile
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.model import TokenUsage
from bmad_loop.policy import LimitsPolicy, Policy
from bmad_loop.signals import HookEvent

HAVE_TMUX = sys.platform != "win32" and shutil.which("tmux") is not None

FAKE_CLI = """#!/bin/bash
# fake CLI: last positional arg is the prompt; env comes from tmux -e
prompt="${@: -1}"
ts=$(date +%s%N)
mkdir -p "$BMAD_LOOP_RUN_DIR/events" "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts" "$BMAD_LOOP_TASK_ID" > "$BMAD_LOOP_RUN_DIR/events/$ts-$BMAD_LOOP_TASK_ID-SessionStart.json"
echo "{\\"workflow\\": \\"auto-dev\\", \\"prompt\\": \\"$prompt\\"}" \\
    > "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID/result.json"
ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts2" "$BMAD_LOOP_TASK_ID" > "$BMAD_LOOP_RUN_DIR/events/$ts2-$BMAD_LOOP_TASK_ID-Stop.json"
sleep 60  # stay alive like an idle interactive session
"""


def make_adapter(
    tmp_path, profile_name="claude", binary=None, extra_args=None, **policy_kw
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
    )


def test_ensure_session_tags_project(tmp_path, monkeypatch):
    """A freshly created agent session is stamped with its project so a cleanup
    in another project never prunes this run. The set-option now flows through
    the tmux backend, so patch its subprocess seam."""
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


# ----------------------------------------------- GenericDevAdapter (B1/B7)
#
# Alex's generic bmad-dev-auto skill writes no result.json; this adapter
# synthesizes the legacy result dict from the spec it leaves on disk, on the
# Stop event, via devcontract. These exercise that override in isolation.


def make_dev_adapter(tmp_path, profile_name="claude"):
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
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile(profile_name),
        paths=paths,
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
    """cap=None (dev/review sessions) preserves the pre-cap behavior byte-
    identical: every fresh Stop restores the budget and nudging continues well
    past any workflow cap — a legitimately slow background wait (e.g. a Unity
    PlayMode run) may need every one of them, bounded only by spec.timeout_s."""
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
    lands afterwards: the result check runs before any stall verdict, so a
    marker that races in wins and the session completes."""
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
        [stop, None, stop, None],  # nudge -> answered result-less -> idle again
        on_call=script,
    )
    result = adapter.wait_for_completion(_dev_handle(), _capped_spec(tmp_path, cap=1))
    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert sent == [generic.STALL_NUDGE_TEXT]  # the cap was already exhausted


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


def _write_fake_cli(tmp_path):
    fake = tmp_path / "fake-cli"
    fake.write_text(FAKE_CLI)
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
        env={"BMAD_LOOP_RUN_DIR": str(adapter.run_dir), "BMAD_LOOP_TASK_ID": task_id},
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
