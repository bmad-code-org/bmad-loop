"""Run-directory helper tests."""

import os
import re
import subprocess
import sys
import tarfile

import pytest

from bmad_loop import runs
from bmad_loop.adapters import tmux_base
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import RunState
from bmad_loop.process_host import ProcessHost


def _make_run(project, run_id, with_state=True):
    run_dir = project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True)
    if with_state:
        (run_dir / "state.json").write_text("{}")
    return run_dir


def _make_state_run(project, run_id, **state_kwargs):
    run_dir = project / ".bmad-loop" / "runs" / run_id
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project),
            started_at="2026-06-11T10:00:00",
            **state_kwargs,
        ),
    )
    return run_dir


def _dead_pid() -> int:
    # A process that exits immediately, cross-platform (POSIX `true` isn't on
    # Windows). The interpreter is always present and on every host.
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class _FakeHost(ProcessHost):
    """A ProcessHost for driving stop_run's escalation deterministically without
    spawning real processes. ``alive`` / ``identity`` may be a value or a zero-arg
    callable (so they can change between the stop-time read and the post-grace
    check). A real subclass on purpose: ``alive_and_ours`` and ``liveness_of``
    are inherited, so these tests exercise the production decision table instead
    of a hand-copied mirror that could silently drift."""

    def __init__(self, *, alive, identity=1.0, on_terminate=None):
        self._alive = alive
        self._identity = identity
        self.on_terminate = on_terminate
        self.terminated: list[int] = []
        self.force_killed: list[int] = []

    def terminate(self, pid):
        self.terminated.append(pid)
        if self.on_terminate is not None:
            self.on_terminate(pid)

    def force_kill(self, pid):
        self.force_killed.append(pid)

    def is_alive(self, pid):
        return self._alive() if callable(self._alive) else self._alive

    def identity(self, pid):
        return self._identity() if callable(self._identity) else self._identity

    def hook_interpreter(self):
        return "python3"


def test_list_run_dirs_sorted_and_filtered(tmp_path):
    _make_run(tmp_path, "20260611-120000-bbbb")
    _make_run(tmp_path, "20260610-090000-aaaa")
    _make_run(tmp_path, "20260612-080000-cccc", with_state=False)  # no state.json
    listed = runs.list_run_dirs(tmp_path)
    assert [d.name for d in listed] == ["20260610-090000-aaaa", "20260611-120000-bbbb"]


def test_list_run_dirs_missing(tmp_path):
    assert runs.list_run_dirs(tmp_path) == []
    assert runs.latest_run_dir(tmp_path) is None


def test_latest_run_dir(tmp_path):
    _make_run(tmp_path, "20260610-090000-aaaa")
    newest = _make_run(tmp_path, "20260611-120000-bbbb")
    assert runs.latest_run_dir(tmp_path) == newest


def test_new_run_id_format():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", runs.new_run_id())


def test_write_pid(tmp_path):
    runs.write_pid(tmp_path)
    tokens = (tmp_path / "engine.pid").read_text().split()
    assert tokens[0] == str(os.getpid())
    # identity is persisted as an optional second token so a reused pid can later be
    # told from our engine; Linux always provides one (via /proc starttime).
    if sys.platform.startswith("linux"):
        assert len(tokens) == 2 and float(tokens[1]) > 0
    elif len(tokens) > 1:
        assert float(tokens[1]) > 0


def test_attach_argv_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert runs.attach_argv("r1") == ["tmux", "attach", "-t", "=bmad-loop-r1"]


def test_attach_argv_inside_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert runs.attach_argv("r1") == ["tmux", "switch-client", "-t", "=bmad-loop-r1"]


# --------------------------------------------------------- resolution / liveness


def test_run_dir_for_and_is_run(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.run_dir_for(tmp_path, "r1") == run_dir
    assert runs.is_run(run_dir)
    assert not runs.is_run(tmp_path / ".bmad-loop" / "runs" / "nope")


def test_short_ref():
    assert runs.short_ref("20260620-143025-a1b2") == "a1b2"


def test_resolve_run_dir_exact_and_partial(tmp_path):
    target = _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260619-101010-c3d4")
    # exact full id
    assert runs.resolve_run_dir(tmp_path, "20260620-143025-a1b2") == target
    # full trailing segment
    assert runs.resolve_run_dir(tmp_path, "a1b2") == target
    # prefix of the trailing segment
    assert runs.resolve_run_dir(tmp_path, "a1") == target
    # a longer tail of the id (endswith)
    assert runs.resolve_run_dir(tmp_path, "025-a1b2") == target


def test_resolve_run_dir_no_match(tmp_path):
    _make_run(tmp_path, "20260620-143025-a1b2")
    with pytest.raises(runs.RunRefError, match="no such run: zzzz"):
        runs.resolve_run_dir(tmp_path, "zzzz")


def test_resolve_run_dir_ambiguous(tmp_path):
    _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260619-101010-a1c9")
    with pytest.raises(runs.RunRefError, match="ambiguous run ref 'a1' matches 2 runs"):
        runs.resolve_run_dir(tmp_path, "a1")


def test_resolve_run_dir_exact_wins_over_ambiguity(tmp_path):
    # An exact id resolves even when another run's id ends with it (which would
    # otherwise be an ambiguous partial match).
    exact = _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260101-000000-20260620-143025-a1b2")  # ends with the exact id
    assert runs.resolve_run_dir(tmp_path, "20260620-143025-a1b2") == exact


def test_read_pid_missing_and_garbage(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.read_pid(run_dir) is None
    (run_dir / "engine.pid").write_text("not-a-pid")
    assert runs.read_pid(run_dir) is None
    (run_dir / "engine.pid").write_text("4242")
    assert runs.read_pid(run_dir) == 4242


def test_engine_alive(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.engine_alive(run_dir) is False  # no pid file
    runs.write_pid(run_dir)  # this test process: alive
    assert runs.engine_alive(run_dir) is True
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.engine_alive(run_dir) is False


def test_read_pid_identity_forms(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.read_pid_identity(run_dir) == (None, None)  # missing
    (run_dir / "engine.pid").write_text("4242")  # legacy: pid only
    assert runs.read_pid_identity(run_dir) == (4242, None)
    (run_dir / "engine.pid").write_text("4242 678.5")  # pid + identity
    assert runs.read_pid_identity(run_dir) == (4242, 678.5)
    (run_dir / "engine.pid").write_text("not-a-pid 1.0")  # unparseable pid
    assert runs.read_pid_identity(run_dir) == (None, None)


def test_engine_liveness(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.engine_liveness(run_dir) == "dead"  # no pid file → nothing to gate on

    (run_dir / "engine.pid").write_text("4242 100.0")

    def use(host):
        monkeypatch.setattr(runs, "get_process_host", lambda: host)

    use(_FakeHost(alive=True, identity=100.0))
    assert runs.engine_liveness(run_dir) == "alive"  # identity matches

    use(_FakeHost(alive=True, identity=999.0))
    assert runs.engine_liveness(run_dir) == "dead"  # reused pid: identity differs

    # live pid whose identity is unreadable (win32 ERROR_ACCESS_DENIED) → unknown, not dead
    use(_FakeHost(alive=True, identity=None))
    assert runs.engine_liveness(run_dir) == "unknown"

    class _Boom:  # an unexpected probe failure degrades to unknown, never a false dead
        def liveness_of(self, pid, identity):
            raise RuntimeError("probe blew up")

    use(_Boom())
    assert runs.engine_liveness(run_dir) == "unknown"

    # A misconfigured host (get_process_host itself raising) is a hard error, not a
    # flaky per-pid probe — it must propagate, never mask as 'unknown'.
    from bmad_loop.process_host import ProcessHostError

    def _boom_host():
        raise ProcessHostError("BMAD_LOOP_PROCESS_HOST matches no registered host")

    monkeypatch.setattr(runs, "get_process_host", _boom_host)
    with pytest.raises(ProcessHostError):
        runs.engine_liveness(run_dir)


@pytest.mark.parametrize("identity_token", ["garbage", "nan", "inf", "-inf"])
def test_engine_alive_malformed_identity_fails_closed(tmp_path, monkeypatch, identity_token):
    # Two tokens means "identity was intended"; if token 2 is corrupt, do not
    # degrade to legacy bare-existence liveness and report a reused pid as alive.
    run_dir = _make_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text(f"4242 {identity_token}")
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=123.0))
    assert runs.engine_alive(run_dir) is False


def test_engine_alive_reused_pid_reads_dead(tmp_path, monkeypatch):
    # A stranger inherited the recorded pid: identity no longer matches → dead.
    run_dir = _make_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=999.0))
    assert runs.engine_alive(run_dir) is False
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=123.0))
    assert runs.engine_alive(run_dir) is True


def test_engine_alive_legacy_pid_degrades_to_existence(tmp_path, monkeypatch):
    # A legacy pid file (no identity token) can only fall back to bare existence.
    run_dir = _make_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242")
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True))
    assert runs.engine_alive(run_dir) is True
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=False))
    assert runs.engine_alive(run_dir) is False


# ---------------------------------------------------------------- stop / delete


def test_stop_run_already_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1", finished=True)
    assert runs.stop_run(run_dir) is False
    assert load_state(run_dir).stopped is False


def test_stop_run_no_pid_falls_back_to_mark(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")  # no engine.pid -> legacy/dead
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True
    assert killed == ["r1"]
    journal = (run_dir / "journal.jsonl").read_text()
    assert "run-stop" in journal and '"fallback": true' in journal


def test_stop_run_dead_pid_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True


def test_stop_run_signals_live_process(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    proc = subprocess.Popen(["sleep", "30"])
    (run_dir / "engine.pid").write_text(str(proc.pid))
    assert runs.stop_run(run_dir) is True
    # the process received SIGTERM and is gone
    assert proc.poll() is not None or proc.wait(timeout=5) is not None
    assert load_state(run_dir).stopped is True


def test_stop_run_respects_engine_written_stopped(tmp_path, monkeypatch):
    """When a live engine exits having already marked the run stopped, stop_run
    trusts it and does not re-journal a fallback entry."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242")

    def _mark_stopped(_pid):
        # emulate the engine handler marking stopped, then dying on SIGTERM
        st = load_state(run_dir)
        st.stopped = True
        save_state(run_dir, st)

    host = _FakeHost(alive=False, on_terminate=_mark_stopped)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True
    assert host.force_killed == []  # exited gracefully — no escalation
    # trusted the engine: no fallback journal entry written
    journal = run_dir / "journal.jsonl"
    assert not journal.exists() or "fallback" not in journal.read_text()


def test_stop_run_force_kills_wedged_engine(tmp_path, monkeypatch):
    """An engine that ignores SIGTERM past the grace window is force-killed, then
    marked stopped — as long as its pid identity still matches what we recorded."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")  # persisted identity

    host = _FakeHost(alive=True, identity=123.0)  # never exits, identity stable
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert host.force_killed == [4242]
    assert load_state(run_dir).stopped is True


def test_stop_run_force_kills_wedged_legacy_engine(tmp_path, monkeypatch):
    """A legacy pid file (no persisted identity) can still force-kill a wedged
    engine: the forced path falls back to a stop-time identity sample (today's
    behavior) rather than refusing outright — no capability regression for
    pre-upgrade runs."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242")  # legacy: pid only, no identity token

    host = _FakeHost(alive=True, identity=555.0)  # never exits, identity stable
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert host.force_killed == [4242]
    assert load_state(run_dir).stopped is True


def test_stop_run_refuses_force_kill_on_identity_mismatch(tmp_path, monkeypatch):
    """If the pid is still 'alive' but its identity changed during the grace window
    (possible pid reuse), refuse to force-kill and raise StopRunError instead."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")  # persisted identity at run start

    # matches the persisted identity at stop entry, then changes before the
    # post-grace force-kill check (pid reused mid-grace).
    identities = iter([123.0, 999.0])
    host = _FakeHost(alive=True, identity=lambda: next(identities))
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    with pytest.raises(runs.StopRunError):
        runs.stop_run(run_dir)
    assert host.force_killed == []


def test_stop_run_refuses_force_kill_without_identity(tmp_path, monkeypatch):
    """On a platform that can't provide an identity (None), a wedged engine can't
    be safely force-killed — raise StopRunError rather than risk a reused pid."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242")

    host = _FakeHost(alive=True, identity=None)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    with pytest.raises(runs.StopRunError):
        runs.stop_run(run_dir)
    assert host.force_killed == []


def test_stop_run_clean_stop_on_pre_stop_pid_reuse(tmp_path, monkeypatch):
    """If the recorded pid was reused by an unrelated process before stop_run
    ran, don't signal the stranger — fall back to a clean mark-stopped, with no
    StopRunError and no terminate/force-kill."""
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")  # recorded identity 123.0

    host = _FakeHost(alive=True, identity=999.0)  # alive, but identity differs → reused
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert host.terminated == [] and host.force_killed == []  # stranger never signalled
    assert load_state(run_dir).stopped is True
    assert killed == ["r1"]
    assert '"fallback": true' in (run_dir / "journal.jsonl").read_text()


# ---------------------------------------------------------------- prune sessions


def test_tmux_sessions_no_tmux(monkeypatch):
    # tmux_sessions now delegates to the multiplexer backend; patch its seam.
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: None)
    assert runs.tmux_sessions() == []


def test_tmux_sessions_no_server(monkeypatch):
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server"),
    )
    assert runs.tmux_sessions() == []


def test_prunable_sessions_partitions(tmp_path, monkeypatch):
    mine = runs.project_tag(tmp_path)
    # live run: real run dir with this process's pid, tagged ours
    live = _make_state_run(tmp_path, "live-1")
    runs.write_pid(live)
    # finished run: run dir exists but dead pid, tagged ours
    finished = _make_state_run(tmp_path, "fin-1")
    (finished / "engine.pid").write_text(str(_dead_pid()))
    # orphan tagged ours: session's run dir is gone -> still prunable
    # untagged finished run: ownership proven by the run dir under this project
    untag_fin = _make_state_run(tmp_path, "untag-fin")
    (untag_fin / "engine.pid").write_text(str(_dead_pid()))

    sessions = [
        "bmad-loop-live-1",
        "bmad-loop-fin-1",
        "bmad-loop-orphan-1",
        "bmad-loop-other-1",  # another project's live run
        "bmad-loop-untag-fin",  # pre-upgrade session, no tag
        "bmad-loop-untag-orphan",  # pre-upgrade, no tag, no run dir here
        "bmad-loop-ctl",  # control session: never a candidate
        "unrelated",  # not ours
    ]
    monkeypatch.setattr(runs, "tmux_sessions", lambda: sessions)
    monkeypatch.setattr(
        runs,
        "session_project_tags",
        lambda: {
            "bmad-loop-live-1": mine,
            "bmad-loop-fin-1": mine,
            "bmad-loop-orphan-1": mine,
            "bmad-loop-other-1": "/some/other/project",
            # untag-* and unrelated intentionally absent (no tag)
        },
    )
    prunable, alive, unknown = runs.prunable_sessions(tmp_path)
    # other-1 (foreign tag) and untag-orphan (unprovable) are skipped entirely
    assert sorted(prunable) == ["fin-1", "orphan-1", "untag-fin"]
    assert alive == ["live-1"]
    assert unknown == set()


def test_prunable_sessions_flags_unknown(tmp_path, monkeypatch):
    # live pid, unreadable identity (win32 ERROR_ACCESS_DENIED) → prunable anyway
    # (unknown never blocks cleanup) but flagged so frontends can warn.
    mine = runs.project_tag(tmp_path)
    odd = _make_state_run(tmp_path, "odd-1")
    (odd / "engine.pid").write_text("4242 123.0")
    monkeypatch.setattr(runs, "tmux_sessions", lambda: ["bmad-loop-odd-1"])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {"bmad-loop-odd-1": mine})
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=None))
    prunable, live, unknown = runs.prunable_sessions(tmp_path)
    assert prunable == ["odd-1"]
    assert live == []
    assert unknown == {"odd-1"}


def test_prune_sessions_dry_run_kills_nothing(tmp_path, monkeypatch):
    finished = _make_state_run(tmp_path, "fin-1")
    (finished / "engine.pid").write_text(str(_dead_pid()))
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    monkeypatch.setattr(runs, "tmux_sessions", lambda: ["bmad-loop-fin-1"])
    monkeypatch.setattr(
        runs, "session_project_tags", lambda: {"bmad-loop-fin-1": runs.project_tag(tmp_path)}
    )
    assert runs.prune_sessions(tmp_path, dry_run=True) == (["fin-1"], [], set())
    assert killed == []
    assert runs.prune_sessions(tmp_path) == (["fin-1"], [], set())
    assert killed == ["fin-1"]


def test_prune_sessions_returns_unknown_from_same_sample(tmp_path, monkeypatch):
    # the unknown subset must come from the partition prune_sessions itself
    # killed, so a frontend warning built from it never names an unpruned session
    mine = runs.project_tag(tmp_path)
    odd = _make_state_run(tmp_path, "odd-1")
    (odd / "engine.pid").write_text("4242 123.0")
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    monkeypatch.setattr(runs, "tmux_sessions", lambda: ["bmad-loop-odd-1"])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {"bmad-loop-odd-1": mine})
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=None))
    assert runs.prune_sessions(tmp_path) == (["odd-1"], [], {"odd-1"})
    assert killed == ["odd-1"]


def test_delete_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "r1")
    runs.delete_run(run_dir)
    assert not run_dir.exists()


def _escalated_run(tmp_path, spec_text, *, restore_patch_stale=None):
    from bmad_loop.model import PAUSE_ESCALATION, Phase, StoryTask

    spec = tmp_path / "spec.md"
    spec.write_text(spec_text, encoding="utf-8")
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.ESCALATED,
        attempt=2,
        spec_file=str(spec),
        restore_patch=restore_patch_stale,
    )
    run_dir = _make_state_run(
        tmp_path,
        "r1",
        paused_reason="CRITICAL escalation",
        paused_stage=PAUSE_ESCALATION,
        paused_story_key="1-1-a",
        tasks={"1-1-a": task},
    )
    return run_dir, spec


_SPEC_WITH_ARR = (
    "---\ntitle: t\nstatus: blocked\n---\n\n## Intent\n\nbody\n"
    "\n## Auto Run Result\n\n- Status: blocked\n\nboom\n"
)


def test_rearm_restore_mode_sets_in_review_strips_arr_and_latches(tmp_path):
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.PENDING and task.attempt == 0
    assert task.restore_patch == "artifacts/attempt.patch"
    text = spec.read_text()
    assert "status: in-review" in text  # in-review routes step-01 -> step-04
    assert "## Auto Run Result" not in text  # stale terminal section stripped
    entry = [e for e in Journal(run_dir).entries() if e["kind"] == "story-escalation-resolved"][-1]
    assert entry["restore"] is True


def test_rearm_plain_mode_sets_ready_for_dev_and_clears_stale_latch(tmp_path):
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    # a stale latch from a prior restore attempt the human then chose to redo fresh
    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR, restore_patch_stale="old.patch")
    runs.rearm_escalation(run_dir)  # no restore_patch => from-scratch

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.PENDING
    assert task.restore_patch is None  # stale latch cleared
    assert "status: ready-for-dev" in spec.read_text()
    entry = [e for e in Journal(run_dir).entries() if e["kind"] == "story-escalation-resolved"][-1]
    assert entry["restore"] is False


def test_archive_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "journal.jsonl").write_text('{"kind":"x"}\n')
    dest = runs.archive_run(tmp_path, run_dir)

    assert dest == tmp_path / ".bmad-loop" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert dest.is_file()
    assert not run_dir.exists()  # original removed
    assert not dest.with_suffix(".tar.gz.tmp").exists()  # temp cleaned via replace
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert "20260611-100000-aaaa/state.json" in names
    assert "20260611-100000-aaaa/journal.jsonl" in names
