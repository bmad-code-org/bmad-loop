"""Run-directory helper tests."""

import contextlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import escalated_run, git, refuse_to_resolve

from bmad_loop import envvars, platform_util, runs, verify
from bmad_loop.adapters import tmux_base
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer
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

    def __init__(self, *, alive, identity=1.0, on_terminate=None, on_force_kill=None):
        self._alive = alive
        self._identity = identity
        self.on_terminate = on_terminate
        self.on_force_kill = on_force_kill
        self.terminated: list[int] = []
        self.force_killed: list[int] = []

    def terminate(self, pid):
        self.terminated.append(pid)
        if self.on_terminate is not None:
            self.on_terminate(pid)

    def force_kill(self, pid):
        self.force_killed.append(pid)
        if self.on_force_kill is not None:
            self.on_force_kill(pid)

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


def test_is_valid_run_id_is_identity_for_generated_ids():
    """The validator must accept everything our one legitimate producer emits —
    and those ids must survive both sanitizers byte-for-byte, since a run id is a
    directory name (safe_segment) and a git ref component (safe_ref_segment) at once."""
    for _ in range(20):
        run_id = runs.new_run_id()
        assert runs.is_valid_run_id(run_id)
        assert platform_util.safe_segment(run_id) == run_id
        assert platform_util.safe_ref_segment(run_id) == run_id


@pytest.mark.parametrize("value", ["r1", "RID", "a", "A_b-C9", "x" * platform_util.MAX_SEGMENT])
def test_is_valid_run_id_accepts(value):
    assert runs.is_valid_run_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "..",  # traversal
        "../x",
        "..\\x",
        "/etc/passwd",  # posix absolute
        "C:\\windows",  # windows drive-absolute
        "C:rel",  # windows drive-relative
        "a/b",  # posix separator
        "a\\b",  # windows separator
        "-lead",  # leading dash (git porcelain option-lookalike)
        "_lead",  # leading underscore: only [A-Za-z0-9] may start an id
        ".hidden",  # leading dot
        "a.b",  # dot: mangles a multiplexer session name
        "a:b",  # colon: mangles a multiplexer session name, illegal in a git ref
        "a b",  # whitespace
        "a\tb",
        "a\nb",
        "a\x00b",  # control char
        "trailing.",  # windows drops trailing dots
        "trailing ",  # ...and trailing spaces
        'a"b',
        "a<b",
        "a>b",
        "a|b",
        "a?b",
        "a*b",
        "a~b",
        "a^b",
        "a[b",
        "a@{b",
        "CON",  # reserved windows device basenames, any case, with or without ext
        "nul",
        "COM1",
        "x" * (platform_util.MAX_SEGMENT + 1),  # over the segment cap
    ],
)
def test_is_valid_run_id_rejects(value):
    assert not runs.is_valid_run_id(value)


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


@pytest.mark.usefixtures("force_tmux_backend")  # attach_argv goes through the seam
def test_attach_argv_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert runs.attach_argv("r1") == ["tmux", "attach", "-t", "=bmad-loop-r1"]


@pytest.mark.usefixtures("force_tmux_backend")  # attach_argv goes through the seam
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


@pytest.mark.parametrize("ref", ["../../outside", "../outside", "a/b", "a\\b"])
def test_resolve_run_dir_never_escapes_the_runs_dir(tmp_path, ref):
    """The exact branch recomposes `project / RUNS_DIR / ref` from the raw ref, so a
    ref carrying separators or `..` must never reach it — otherwise
    `bmad-loop delete ../../x` rmtree's any outside directory that happens to hold a
    state.json. Such refs fall through to partial matching, which can only yield a
    name `list_run_dirs` enumerated, i.e. an immediate child of the runs dir.

    Stated as containment rather than no-match because `a\\b` is one legal directory
    name on POSIX (inside the runs dir, so it legitimately resolves) and a nested
    path on Windows (where it must not)."""
    project = tmp_path / "proj"
    _make_run(project, "20260620-143025-a1b2")
    runs_dir = (project / ".bmad-loop" / "runs").resolve()
    # plant a state.json exactly where the un-gated exact branch would land
    planted = (project / ".bmad-loop" / "runs" / ref).resolve()
    planted.mkdir(parents=True, exist_ok=True)
    (planted / "state.json").write_text("{}")

    try:
        got = runs.resolve_run_dir(project, ref)
    except runs.RunRefError as e:
        assert "no such run" in str(e)
    else:
        assert got.resolve().parent == runs_dir  # an enumerated run dir, never an escape
    assert (planted / "state.json").is_file()  # never consumed as a run


def test_resolve_run_dir_absolute_ref_never_escapes(tmp_path):
    """`run_dir_for(project, "/abs")` is `Path("/abs")` — `/`-join discards the
    project prefix entirely, so an absolute ref escapes without needing a `..`."""
    project = tmp_path / "proj"
    _make_run(project, "20260620-143025-a1b2")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}")

    with pytest.raises(runs.RunRefError, match="no such run"):
        runs.resolve_run_dir(project, str(outside))
    assert (outside / "state.json").is_file()


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
    # Bound the grace window: this child is settled either way, and the default
    # 10s is pure dead time here. It is also burned on *every* platform, not just
    # win32 — the exited child stays an unreaped zombie while this test holds its
    # Popen handle, and a zombie answers the POSIX `os.kill(pid, 0)` liveness
    # probe as alive.
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 2.0)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.05)
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (run_dir / "engine.pid").write_text(str(proc.pid))
        assert runs.stop_run(run_dir) is True
        # the process is gone. On POSIX it took the SIGTERM; on win32 `taskkill`
        # without /F posts a WM_CLOSE a console child has no window to receive, so
        # there it is force-killed after the bounded wait instead. Either way
        # stop_run settles the run — which is the whole point of the file channel.
        assert proc.poll() is not None or proc.wait(timeout=5) is not None
        assert load_state(run_dir).stopped is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_stop_run_lodges_hard_request_before_signalling(tmp_path, monkeypatch):
    """The hard request is on disk *before* terminate() is called. That ordering is
    the guarantee: an engine that is signal-deaf, or that dies to the signal before
    reading anything, can never exit having missed a request written only after it
    was signalled. Read from inside the host at terminate time so the assertion
    cannot be satisfied by a write that lands later."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    seen: list[str | None] = []

    def _read_at_terminate(_pid):
        seen.append(runs.read_stop_request_mode(run_dir))
        st = load_state(run_dir)  # emulate the engine honoring it, then exiting
        st.stopped = True
        save_state(run_dir, st)

    host = _FakeHost(alive=False, identity=100.0, on_terminate=_read_at_terminate)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert seen == ["hard"]
    assert host.force_killed == []  # the engine settled it — no escalation


def test_stop_run_still_signals_when_the_lodge_fails(tmp_path, monkeypatch):
    """A run dir that rejects the write must not cost the signal path too.

    The lodge goes first (see the test above), so before it was guarded an OSError
    escaped `stop_run` ahead of `terminate` and left alive a POSIX run the pre-#319
    code would have killed — a stop that does nothing at all, replacing one that
    worked. Reachable without exotic setup: every session tees its pane into
    `run_dir/logs/`, so a long run can fill the very directory the request must be
    written to, and then `stop` is what fails.

    Degrading is right *here* specifically because the hard stop is delivered two
    ways at once. `request_graceful_stop` has only the file, so its write still
    raises — that asymmetry is the point, and `test_request_graceful_stop_*` holds
    the other side."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    def _enospc(_run_dir, _mode):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "_write_stop_request", _enospc)
    host = _FakeHost(alive=False, identity=100.0)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    assert runs.stop_run(run_dir) is True
    assert host.terminated == [4242]  # the signal went out despite the failed lodge
    assert load_state(run_dir).stopped is True  # and the run is settled


def test_stop_run_refusal_says_nothing_is_pending_when_the_lodge_failed(tmp_path, monkeypatch):
    """The force-kill refusal justifies itself by the file still being lodged — "the
    only channel left that can stop it". When the lodge failed that sentence is
    false: nothing is pending, and the operator is declining a force-kill on top of
    a request that was never written. The message has to say so, or they wait on a
    stop that can never arrive."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.0)  # expire the grace window at once
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    def _enospc(_run_dir, _mode):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "_write_stop_request", _enospc)
    # Alive throughout, and the identity goes unreadable right after the stop-time
    # read: the grace window expires and the pid-reuse guard then refuses to kill.
    identities = iter([100.0] + [None] * 50)
    host = _FakeHost(alive=True, identity=lambda: next(identities))
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    with pytest.raises(runs.StopRunError, match="could not be written"):
        runs.stop_run(run_dir)
    assert host.force_killed == []  # still refuses to kill an unverifiable pid


def test_stop_run_stops_sigterm_immune_child_via_stop_request_file(tmp_path, monkeypatch):
    """THE #319 acceptance test: a stand-in engine that cannot be reached by signal
    stops *itself* off the control file, and stop_run confirms rather than blindly
    force-killing it.

    The child ignores SIGTERM, modelling a native-Windows engine — which never
    receives an inter-process SIGTERM at all, and whose `taskkill` "graceful" step
    posts a WM_CLOSE that a console process has no window to receive. So the only
    channel that can reach it is the `mode: hard` request stop_run lodges before
    signalling. It polls for that file, marks the run stopped exactly as the
    engine's own handler does, and exits 0. Before #319 this was unreachable: every
    Windows stop burned the full grace window into a blind force-kill and `stopped`
    was written by the external fallback, so the engine-is-single-writer invariant
    held only by fallback.

    The reaper thread clears the exited child so the liveness probe stops reading it
    as alive: `os.kill(pid, 0)` answers True for an unreaped zombie, and this test
    holds the Popen handle. Production never has that problem — the engine is not
    the CLI's child — so without the reaper the assertions below would still pass
    but take the whole (shortened) wait window, hiding the speed this fixes.

    Ablation: with `_write_stop_request(run_dir, "hard")` deleted from stop_run, the
    child never sees a request, burns the wait, and is SIGKILLed — returncode -9
    instead of 0, plus a `fallback: true` journal entry. Run once against the
    ablated source, confirmed failing on both, then restored.
    """
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 5.0)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.02)
    run_dir = _make_state_run(tmp_path, "r1")
    request = run_dir / runs.STOP_REQUEST_FILE
    state_path = run_dir / "state.json"
    ready = run_dir / "child-ready"

    # A stand-in engine: deaf to SIGTERM, awake to the control file. Marks the run
    # stopped itself — the engine is the single writer of `stopped`, and this test
    # exists to prove that stays true when no signal can be delivered. The ready
    # file is published only after the handler is installed; see the wait below.
    child = (
        "import json, pathlib, signal, sys, time\n"
        "try:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "except (AttributeError, OSError, ValueError):\n"
        "    pass\n"  # a platform that refuses the handler still can't reach us
        "req, state = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
        "pathlib.Path(sys.argv[3]).write_text('ready')\n"
        "deadline = time.monotonic() + 60\n"
        "while time.monotonic() < deadline:\n"
        "    if req.exists():\n"
        "        d = json.loads(state.read_text())\n"
        "        d['stopped'] = True\n"
        "        state.write_text(json.dumps(d))\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.05)\n"
        "sys.exit(3)\n"  # never saw a request — the outcome the ablation produces
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(request), str(state_path), str(ready)]
    )

    def _reap():
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=90)

    reaper = threading.Thread(target=_reap, daemon=True)
    reaper.start()
    try:
        # Wait for SIG_IGN to be installed before stopping. Interpreter startup is
        # tens of milliseconds and stop_run signals immediately, so without this the
        # SIGTERM lands on a child still importing and kills it by default action —
        # rc -15, and the test would be measuring the race instead of the channel.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        assert ready.exists(), "stand-in engine never became SIGTERM-immune"

        (run_dir / "engine.pid").write_text(str(proc.pid))
        assert runs.stop_run(run_dir) is True
        reaper.join(timeout=30)
        # the child exited ITSELF: rc 0, not -15 (SIGTERM), -9 (SIGKILL) or a
        # taskkill /F status. This is the assertion the whole issue is about.
        assert proc.returncode == 0
        assert load_state(run_dir).stopped is True
        # ...and stop_run trusted it, rather than marking the run stopped behind it
        journal = run_dir / "journal.jsonl"
        assert not journal.exists() or "fallback" not in journal.read_text()
        assert runs.read_stop_request_mode(run_dir) is None  # request consumed
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_stop_run_fallback_clears_hard_request(tmp_path, monkeypatch):
    """On the mark-stopped fallback nothing is left alive to consume the request, so
    stop_run discards what it lodged. A file outliving the run it asked to stop is a
    trap: the next resume would find it and stop again at the first item."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")  # no engine.pid -> straight to fallback
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True
    assert runs.read_stop_request_mode(run_dir) is None
    assert '"fallback": true' in (run_dir / "journal.jsonl").read_text()


def test_stop_run_engine_confirmed_leaves_nothing_pending(tmp_path, monkeypatch):
    """When the engine confirms the stop itself the request is consumed too. The
    engine normally clears it on the way out; this is the belt-and-braces half, and
    it is what keeps a confirmed stop from stranding a request on disk."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    def _mark_stopped(_pid):
        st = load_state(run_dir)
        st.stopped = True
        save_state(run_dir, st)

    host = _FakeHost(alive=False, identity=100.0, on_terminate=_mark_stopped)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    assert runs.stop_run(run_dir) is True
    assert runs.read_stop_request_mode(run_dir) is None
    journal = run_dir / "journal.jsonl"
    assert not journal.exists() or "fallback" not in journal.read_text()


def test_stop_run_refusal_leaves_hard_request_lodged(tmp_path, monkeypatch):
    """StopRunError is the one path that leaves the request on disk. We refused to
    force-kill a pid whose identity we can no longer verify — but if it *is* still
    our engine, the file is now the only channel that can stop it. Clearing it here
    would retract the operator's request while simultaneously declining to enforce
    it, leaving a live run nobody asked to keep running."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    identities = iter([123.0, 999.0])  # identity changes mid-grace -> possible reuse
    host = _FakeHost(alive=True, identity=lambda: next(identities))
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    with pytest.raises(runs.StopRunError):
        runs.stop_run(run_dir)
    assert host.force_killed == []
    assert runs.read_stop_request_mode(run_dir) == "hard"


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


def _raise(exc):
    """A `_FakeHost` hook that refuses the kill instead of performing it."""

    def _hook(_pid):
        raise exc

    return _hook


def test_stop_run_keeps_the_hard_request_when_the_signal_is_refused(tmp_path, monkeypatch):
    """A `terminate` we were *refused* leaves the lodged request on disk.

    The pid was `alive_and_ours` a moment earlier and we could not signal it, so it
    may well still be running — and on native Windows the control file is then the
    only channel that can still stop it. Discarding it here would retract the repair
    #319 exists to deliver, while reporting the run stopped. Contrast the
    ProcessLookupError twin below: that one is proof of death, so the file goes."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    host = _FakeHost(alive=True, identity=123.0, on_terminate=_raise(PermissionError()))
    _use_host(monkeypatch, host)

    assert runs.stop_run(run_dir) is True
    assert runs.read_stop_request_mode(run_dir) == "hard"  # still lodged, still honorable
    assert host.force_killed == []  # unsignalable — never escalated to a kill
    assert load_state(run_dir).stopped is True


def test_stop_run_discards_the_hard_request_when_the_signal_proves_it_gone(tmp_path, monkeypatch):
    """The mode-exact twin, and the second ablation axis: `ProcessLookupError` from
    `terminate` says the process is *gone*, so nothing is left to consume the request
    and leaving it would trap the next resume. Collapsing the two excepts back into
    one reddens exactly one of this pair, whichever way it is collapsed."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    host = _FakeHost(alive=True, identity=123.0, on_terminate=_raise(ProcessLookupError()))
    _use_host(monkeypatch, host)

    assert runs.stop_run(run_dir) is True
    assert not runs.graceful_stop_requested(run_dir)  # provably dead — discarded
    assert load_state(run_dir).stopped is True


def test_stop_run_keeps_the_hard_request_when_the_force_kill_is_refused(tmp_path, monkeypatch):
    """A `force_kill` that raises `PermissionError` is the opposite of a race: the
    process is there and we were refused. The request stays lodged."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    host = _FakeHost(alive=True, identity=123.0, on_force_kill=_raise(PermissionError()))
    _use_host(monkeypatch, host)

    assert runs.stop_run(run_dir) is True
    assert host.force_killed == [4242]  # we did try
    assert runs.read_stop_request_mode(run_dir) == "hard"  # and kept the channel


def test_stop_run_keeps_the_hard_request_when_a_clean_force_kill_did_not_take(
    tmp_path, monkeypatch
):
    """A force-kill that returns cleanly is not a death certificate.

    `WindowsProcessHost.force_kill` shells `taskkill /F /T` with `check=False`, so a
    refused kill raises nothing at all — and win32 is the platform this channel
    exists for. The engine is re-probed after the kill settles, and a survivor keeps
    its request."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    monkeypatch.setattr(runs, "_KILL_CONFIRM_S", 0.05)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    # never dies: the silent-taskkill-failure shape
    host = _FakeHost(alive=True, identity=123.0)
    _use_host(monkeypatch, host)

    assert runs.stop_run(run_dir) is True
    assert host.force_killed == [4242]
    assert runs.read_stop_request_mode(run_dir) == "hard"


def test_stop_run_discards_the_hard_request_once_the_force_kill_confirms(tmp_path, monkeypatch):
    """The settle window's positive control: a pid that disappears once the kill
    lands reads as dead, so the request is discarded rather than stranded on the
    ordinary wedged-engine path. Without the settle loop an immediate sample of a
    not-yet-reaped pid would keep the file here and trap the next resume."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(runs, "_STOP_WAIT_S", 0.05)
    monkeypatch.setattr(runs, "_STOP_POLL_S", 0.01)
    monkeypatch.setattr(runs, "_KILL_CONFIRM_S", 1.0)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    lingering = {"ticks": 3}  # still in the pid table for a few probes after the kill

    def _alive():
        if killed["yes"] and lingering["ticks"] > 0:
            lingering["ticks"] -= 1
        return not killed["yes"] or lingering["ticks"] > 0

    killed = {"yes": False}

    def _on_force_kill(_pid):
        killed["yes"] = True

    host = _FakeHost(alive=_alive, identity=123.0, on_force_kill=_on_force_kill)
    _use_host(monkeypatch, host)

    assert runs.stop_run(run_dir) is True
    assert host.force_killed == [4242]
    assert not runs.graceful_stop_requested(run_dir)  # confirmed dead — discarded


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


# ---------------------------------------------------------------- graceful stop


def _use_host(monkeypatch, host):
    monkeypatch.setattr(runs, "get_process_host", lambda: host)


def test_request_graceful_stop_writes_file_when_alive(tmp_path, monkeypatch):
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")  # identity matches the live host
    _use_host(monkeypatch, _FakeHost(alive=True, identity=100.0))
    assert runs.request_graceful_stop(run_dir) == "requested"
    assert runs.graceful_stop_requested(run_dir)
    body = json.loads((run_dir / runs.STOP_REQUEST_FILE).read_text())
    assert body["mode"] == "graceful"
    assert body["requested_at"]  # an ISO timestamp is stamped
    # No sibling left behind. The graceful lodge is an O_CREAT|O_EXCL create written
    # in place, so it has no staging temp *by construction* — this asserts the
    # absence of stray debris, not the atomicity of a replace. The staging-temp
    # guarantee belongs to `_write_stop_request`, which still replaces, and is
    # asserted where it is exercised: see
    # test_write_stop_request_survives_an_interleaved_concurrent_writer.
    assert [p.name for p in run_dir.glob(runs.STOP_REQUEST_FILE + "*")] == [runs.STOP_REQUEST_FILE]


def test_write_stop_request_survives_an_interleaved_concurrent_writer(tmp_path, monkeypatch):
    """Two `stop` invocations against one run stage at the same time — the only
    control file with genuinely concurrent writers.

    With a fixed `<name>.tmp` sibling the second writer's staging file overwrote the
    first's, one `os.replace` consumed the single name, and the loser raised
    `FileNotFoundError`. On the hard path that aborts `stop_run` *before* it signals,
    so a collision between two operators cost the stop entirely. A per-writer
    `mkstemp` temp removes the collision: both calls return, the survivor is a
    complete body, and neither leaves a staging file behind.

    Patched on both namespaces so reverting `_write_stop_request` to the hand-rolled
    `tmp + atomic_replace` still routes through the interleave — that ablation must
    redden this test."""
    from bmad_loop import platform_util

    run_dir = _make_state_run(tmp_path, "r1")
    real_replace = platform_util.atomic_replace
    nested: list[str] = []

    def _interleave(tmp, target):
        if not nested:  # inside writer A's replace, run writer B end to end
            nested.append("b")
            runs._write_stop_request(run_dir, "graceful")
        real_replace(tmp, target)

    monkeypatch.setattr(platform_util, "atomic_replace", _interleave)
    monkeypatch.setattr(runs, "atomic_replace", _interleave)

    runs._write_stop_request(run_dir, "hard")  # writer A — must not raise

    assert nested == ["b"]  # the interleave really happened
    body = json.loads((run_dir / runs.STOP_REQUEST_FILE).read_text())
    assert body["mode"] == "hard"  # A replaced last, so A wins — never a torn body
    assert [p.name for p in run_dir.glob(runs.STOP_REQUEST_FILE + "*")] == [runs.STOP_REQUEST_FILE]


def test_request_graceful_stop_idempotent_keeps_timestamp(tmp_path, monkeypatch):
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")
    _use_host(monkeypatch, _FakeHost(alive=True, identity=100.0))
    # a request already on disk with a distinctive timestamp must be left untouched
    existing = json.dumps({"requested_at": "2000-01-01T00:00:00", "mode": "graceful"})
    (run_dir / runs.STOP_REQUEST_FILE).write_text(existing)
    assert runs.request_graceful_stop(run_dir) == "already-pending"
    assert (run_dir / runs.STOP_REQUEST_FILE).read_text() == existing  # original preserved


def test_request_graceful_stop_refuses_dead_engine(tmp_path):
    run_dir = _make_state_run(tmp_path, "r1")  # no engine.pid → liveness reads "dead"
    with pytest.raises(runs.GracefulStopError, match="no live engine"):
        runs.request_graceful_stop(run_dir)
    assert not runs.graceful_stop_requested(run_dir)  # nothing written on refusal


def test_request_graceful_stop_refuses_finished_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "r1", finished=True)
    with pytest.raises(runs.GracefulStopError, match="already finished"):
        runs.request_graceful_stop(run_dir)
    assert not runs.graceful_stop_requested(run_dir)


def test_request_graceful_stop_unknown_liveness_is_unverifiable(tmp_path, monkeypatch):
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")
    # a live pid whose identity is unreadable (win32 access-denied) reads "unknown":
    # the request is still written, but the caller can't confirm a consumer.
    _use_host(monkeypatch, _FakeHost(alive=True, identity=None))
    assert runs.request_graceful_stop(run_dir) == "requested-unverifiable"
    assert runs.graceful_stop_requested(run_dir)


def test_read_stop_request_mode_matrix(tmp_path):
    """The mode reader answers ``None`` for *absent*, and only absent. Every other
    state of a file that is present reads "graceful".

    The asymmetry is deliberate and load-bearing: "hard" aborts a live session
    mid-flight, so no torn, odd, or unreadable file may be able to produce it. A
    misread graceful costs at most one more item before the run stops."""
    run_dir = _make_run(tmp_path, "r1")
    path = run_dir / runs.STOP_REQUEST_FILE

    assert runs.read_stop_request_mode(run_dir) is None  # nothing pending

    path.write_text('{"requested_at": "now", "mode": "graceful"}', encoding="utf-8")
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    path.write_text('{"requested_at": "now", "mode": "hard"}', encoding="utf-8")
    assert runs.read_stop_request_mode(run_dir) == "hard"

    # Back-compat pin: every pre-#319 writer — and the fixtures still written by
    # hand across this suite — produced a body with no mode at all. It must keep
    # reading as the graceful request it was, not fall through to a hard abort.
    path.write_text("{}", encoding="utf-8")
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    path.write_text('{"mode": "har', encoding="utf-8")  # torn mid-write
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    path.write_text('["hard"]', encoding="utf-8")  # valid JSON, but not an object
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    path.write_text('"hard"', encoding="utf-8")  # a bare JSON scalar
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    path.write_bytes(b"\xff\xfe\x00 not utf-8")  # undecodable bytes
    assert runs.read_stop_request_mode(run_dir) == "graceful"

    # A read that fails outright, standing in for the win32 sharing violation a
    # concurrent atomic_replace raises: a directory in the file's place is an
    # OSError on every platform (IsADirectoryError on POSIX, PermissionError on
    # win32) and must not be mistaken for absence.
    path.unlink()
    path.mkdir()
    assert runs.read_stop_request_mode(run_dir) == "graceful"


def test_clear_graceful_stop_removes_or_noops(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.clear_graceful_stop(run_dir) is False  # nothing pending → no-op, never raises
    (run_dir / runs.STOP_REQUEST_FILE).write_text("{}")
    assert runs.clear_graceful_stop(run_dir) is True  # present → removed
    assert not runs.graceful_stop_requested(run_dir)
    assert runs.clear_graceful_stop(run_dir) is False  # already gone → no-op again


def test_stop_run_supersedes_pending_graceful_request(tmp_path, monkeypatch):
    """A hard stop supersedes a pending graceful request by *overwriting* it, not by
    clearing it. The atomic replace escalates the mode in one step, so there is no
    instant in which the operator has asked for a stop and nothing at all is pending
    for the engine to find. Nothing is left on disk once the run is settled, so a
    later resume can't re-honor the stop the operator escalated past."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        '{"requested_at": "old", "mode": "graceful"}', encoding="utf-8"
    )

    seen: list[str | None] = []

    def _read_at_terminate(_pid):
        seen.append(runs.read_stop_request_mode(run_dir))
        st = load_state(run_dir)
        st.stopped = True
        save_state(run_dir, st)

    host = _FakeHost(alive=False, identity=100.0, on_terminate=_read_at_terminate)
    _use_host(monkeypatch, host)
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True
    assert seen == ["hard"]  # escalated in place — never a gap with nothing pending
    assert not runs.graceful_stop_requested(run_dir)  # and nothing pending after


# ---------------------------------------------------------------- prune sessions


@pytest.mark.parametrize("lodge_at", ["engine_liveness", "just_before_the_create"])
def test_request_graceful_stop_cannot_downgrade_a_hard_request_at_any_instant(
    tmp_path, monkeypatch, lodge_at
):
    """A hard request landing anywhere inside the check -> write window is not
    downgraded, and the guarantee holds at the *last* instant, not just an early one.

    `request_graceful_stop` clears its existence check, then spends a pid-file read
    and a liveness probe before it lodges — measured at ~1.3ms median on btrfs back
    when an fsync sat in there too. The channel is last-writer-wins, so an
    unconditional write here silently supersedes the stronger stop and costs the
    operator the abort they asked for. `O_CREAT | O_EXCL` fuses the decision to the
    write so no interleaving can land between them.

    The two parameters are the point. `engine_liveness` lodges early — a re-read
    immediately before the write already catches that one. `just_before_the_create`
    lodges from the last statement that runs ahead of the `os.open`, which only real
    arbitration catches; a re-read narrows that window but cannot close it.

    Ablation, two axes, and axis 2 is what proves this is not merely re-testing the
    re-read it replaced:
      1. Make `_create_stop_request` an unconditional
         `_write_stop_request(run_dir, "graceful")` — BOTH parameters redden.
      2. Same, but restore a `read_stop_request_mode(...) == "hard"` guard ahead of
         it — `engine_liveness` goes GREEN while `just_before_the_create` stays RED.
         Both going green would mean this test measures the old guard, not the new
         arbitration."""
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")
    lodged: list[str] = []

    def _lodge_hard() -> None:
        if not lodged:  # once — the injection points are per-call, not per-test
            lodged.append("hard")
            runs._write_stop_request(run_dir, "hard")

    if lodge_at == "engine_liveness":

        def _alive(_run_dir):
            _lodge_hard()
            return "alive"

        monkeypatch.setattr(runs, "engine_liveness", _alive)
    else:
        # the last statement before the O_EXCL open, so the hard request lands with
        # nothing but the create left to run
        real_strftime = time.strftime

        def _strftime(fmt, *a):
            _lodge_hard()
            return real_strftime(fmt, *a)

        monkeypatch.setattr(runs.time, "strftime", _strftime)
        monkeypatch.setattr(runs, "engine_liveness", lambda _d: "alive")

    # the same answer a request found at entry gets: a stronger stop already stands
    assert runs.request_graceful_stop(run_dir) == "already-pending"
    assert lodged == ["hard"]  # the interleave really happened
    assert runs.read_stop_request_mode(run_dir) == "hard"  # not downgraded


def test_request_graceful_stop_keeps_escalation_unconditional(tmp_path, monkeypatch):
    """The mirror direction, which the refusal above must not have cost. A hard
    request landing *after* the graceful file exists still supersedes it — that is
    `_write_stop_request`'s unconditional replace, which `stop_run` depends on.

    Ablation: give `_write_stop_request` the same create-if-absent treatment and
    this reddens, reading "graceful" — the asymmetry between the two writers is the
    whole design."""
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")
    _use_host(monkeypatch, _FakeHost(alive=True, identity=100.0))

    assert runs.request_graceful_stop(run_dir) == "requested"
    runs._write_stop_request(run_dir, "hard")  # a later `stop`, escalating

    assert runs.read_stop_request_mode(run_dir) == "hard"


def test_mux_sessions_no_tmux(monkeypatch):
    # mux_sessions now delegates to the multiplexer backend; patch its seam.
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: None)
    assert runs.mux_sessions() == []


def test_mux_sessions_no_server(monkeypatch):
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server"),
    )
    assert runs.mux_sessions() == []


# Everything `str.splitlines()` breaks on. Spelled as escapes on purpose: the
# literals are invisible in a diff, and a raw U+2028/U+2029 in a source file
# makes every splitlines()-based tool disagree with Python's tokenizer about
# which line anything after it is on.
_LINE_SEPARATORS = [
    ("LF", "\n"),
    ("CR", "\r"),
    ("CRLF", "\r\n"),
    ("VT", "\v"),
    ("FF", "\f"),
    ("FS", "\x1c"),
    ("GS", "\x1d"),
    ("RS", "\x1e"),
    ("NEL", "\x85"),
    ("LS", "\u2028"),
    ("PS", "\u2029"),
]
_SEP_VALUES = [s for _, s in _LINE_SEPARATORS]
_SEP_IDS = [i for i, _ in _LINE_SEPARATORS]


@pytest.mark.skipif(sys.platform == "win32", reason="a separator in a name is a POSIX concern")
@pytest.mark.parametrize("separator", _SEP_VALUES, ids=_SEP_IDS)
def test_project_tag_carries_a_path_the_listing_cannot_carry(tmp_path, separator):
    """A listing splits on far more than LF, and every one of those is legal in a
    POSIX directory name (#518).

    The digest makes this true by construction instead of by encoding the few paths
    that needed it, but the property is the same one and still needs pinning: return
    a raw path from project_tag again and these ride the transport raw, arriving
    truncated at every comparison site — a truncated tag is non-empty, so it reads as
    *another* project's and the scan discards the project's own windows.

    Trailing is not a separate case here as it was for the old predicate: a digest
    has no separator anywhere, so there is no row-count blind spot left to probe.
    Two projects must also stay distinguishable — a tag collapsing them would let a
    prune cross the boundary the tag exists to hold."""
    tag = runs.project_tag(tmp_path / f"my{separator}proj")
    assert re.fullmatch("[0-9a-f]{16}", tag)
    assert tag.splitlines()[:1] == [tag]  # one row, and the whole of it
    assert tag != runs.project_tag(tmp_path / "theirproj")


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
    monkeypatch.setattr(runs, "mux_sessions", lambda: sessions)
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


def test_project_tag_is_transportable_whatever_the_path(tmp_path):
    """Tags have one safe shape even for paths psmux or UTF-8 cannot carry raw.

    Assert the shape, not just that the gate accepts it: an ordinary Windows path —
    spaced, UNC, apostrophed alike — clears the gate on its own now that psmux 3.3.8
    carries the wire verbatim, so only "hex whatever the input" fails when
    project_tag returns a raw path.
    """
    # The premise, on the half of the gate 3.3.8 did NOT retire: a `"` cannot come
    # back through _scoped_options' one-quote-pair strip, so a raw path carrying one
    # is refused and a raw-path tag would still be unstorable.
    assert not PsmuxMultiplexer._transportable(r'C:\a"b\proj')
    project = tmp_path / "share name" / "proj"
    project.mkdir(parents=True)
    tag = runs.project_tag(project)
    assert re.fullmatch("[0-9a-f]{16}", tag)
    assert PsmuxMultiplexer._transportable(tag)
    assert re.fullmatch("[0-9a-f]{16}", runs.project_tag(tmp_path / f"bad{chr(0xDC80)}"))
    assert len({tag, runs.project_tag(tmp_path / "other")}) == 2


# ------------------------------------------------- user-scoped state root (#494)
#
# Every row here clears the suite-wide `_isolate_state_root` override first: that
# fixture exists so no test writes into the real state directory, and it is the
# first thing `state_root` consults, so a cascade row that left it set would grade
# nothing. `sys.platform` is faked per branch (the house idiom — see
# test_journal.py), and the fake home is written to HOME *and* USERPROFILE because
# `expanduser` reads the first on POSIX and the second on Windows, so a row must
# set both to mean the same thing on either host (tests/test_diagnostics.py:630).


def _fake_home(monkeypatch, home) -> None:
    """Point `expanduser("~")` at `home` on whichever host is running, and clear
    everything else `state_root` would answer from first."""
    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_state_root_precedence_override_then_xdg_then_home(tmp_path, monkeypatch):
    """Three answers, ranked, and the ranking is what each assertion removes.

    The override outranks a *set* XDG_STATE_HOME and not merely an unset one —
    graded by leaving XDG set throughout — because it is the operator's stated
    answer and the suite's own isolation depends on it winning against whatever a
    host exports.

    The asymmetry in the middle is deliberate and pinned here rather than left to
    the reader: `XDG_STATE_HOME` is a *base* to build under, so `bmad-loop` is
    appended to it, while `BMAD_LOOP_STATE_DIR` names our root itself and is used
    as spelled. Appending to the override would silently move every path a
    phase-3 hook computes from that same variable."""
    monkeypatch.setattr(runs.sys, "platform", "linux")
    home = tmp_path / "home"
    _fake_home(monkeypatch, home)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "override"))

    assert runs.state_root() == tmp_path / "override"  # verbatim: no "bmad-loop" tail

    monkeypatch.delenv(envvars.STATE_DIR)
    assert runs.state_root() == tmp_path / "xdg" / "bmad-loop"

    monkeypatch.delenv("XDG_STATE_HOME")
    assert runs.state_root() == home / ".local" / "state" / "bmad-loop"


@pytest.mark.parametrize("value", ["state", "./state", "~/state"], ids=repr)
def test_state_root_refuses_a_relative_override(tmp_path, monkeypatch, value):
    """`BMAD_LOOP_STATE_DIR` is honoured as spelled, but a relative spelling is
    not a root — it is two roots. The engine exports this path to the session as
    `BMAD_LOOP_EVENTS_DIR` and the multiplexer launches that session at
    `spec.cwd` (a worktree, under isolation), while the watcher polls from the
    orchestrator's cwd. So the relay writes its Stop into one directory and
    nothing watches it, and the run waits out `session_timeout_min` — silent, and
    exactly the stall moving this channel out of the tree was meant to prevent.

    It RAISES rather than falling through to the cascade, which is the split from
    the sibling XDG test above: a derived base that fails its check is a guess we
    move on from, an override is a statement we cannot honour and must not
    silently replace. It equally does not absolutize — resolving against whichever
    cwd this process happens to have is the guess, and picking one of the two
    directories at random is how the stall gets harder to see rather than gone.

    `~/state` is here for the same reason it is in the XDG rows: nothing expands
    it, so it stays relative. The empty string is deliberately NOT a row — empty
    reads as *unset* and falls through to the cascade, which
    `test_state_root_precedence_override_then_xdg_then_home` already grades.

    Ablation target: drop the `os.path.isabs` guard from the override arm and all
    three rows fail — each returning a cwd-relative root instead of raising."""
    monkeypatch.setattr(runs.sys, "platform", "linux")
    _fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv(envvars.STATE_DIR, value)

    with pytest.raises(runs.StateRootError, match=envvars.STATE_DIR):
        runs.state_root()


@pytest.mark.parametrize("value", ["state", "./state", "~/state", ""], ids=repr)
def test_state_root_ignores_an_xdg_state_home_that_is_not_absolute(tmp_path, monkeypatch, value):
    """The XDG base-directory spec says a relative value "must be ignored", and
    ignoring it means falling through to the home default — not resolving it
    against the cwd, which is what `Path(value) / "bmad-loop"` would do.

    That distinction is the whole test: a cwd-relative control plane is not a
    failure anyone sees, it is a run whose events land somewhere the next process
    to ask does not look, and a run that finds no completion signal waits out
    `session_timeout_min`. `~/state` is here because expansion is not this
    reader's job either — nothing expands it, so it stays relative. The empty
    string is the same rule reached from the other side: set-but-empty is how an
    unset-looking export reads, and `Path("")` is the cwd.

    Ablation target: drop the `os.path.isabs` half of `_state_base` and the three
    relative rows fail together, each on a cwd-relative root. The empty row is
    held by BOTH halves — `os.path.isabs("")` is already False — so no single
    ablation reddens it here, and it is kept as the spelling an operator produces
    rather than as an independent gate. What the emptiness half holds alone is the
    *unset* variable, where `os.path.isabs(None)` raises; the refusal rows below
    are what grade it."""
    monkeypatch.setattr(runs.sys, "platform", "linux")
    home = tmp_path / "home"
    _fake_home(monkeypatch, home)
    monkeypatch.setenv("XDG_STATE_HOME", value)

    assert runs.state_root() == home / ".local" / "state" / "bmad-loop"


def test_state_root_on_win32_prefers_localappdata_over_the_user_profile(tmp_path, monkeypatch):
    """Windows keeps this class of per-user, per-machine state under
    `%LOCALAPPDATA%`, and `%USERPROFILE%\\AppData\\Local` is that variable's
    documented default location — the fallback, not a second opinion.

    XDG_STATE_HOME stays set across both assertions: it is a POSIX variable, and a
    branch that consulted it on Windows would answer from an operator's WSL or
    MSYS environment instead of the local store."""
    monkeypatch.setattr(runs.sys, "platform", "win32")
    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))

    assert runs.state_root() == tmp_path / "local" / "bmad-loop" / "state"

    monkeypatch.delenv("LOCALAPPDATA")
    expected = tmp_path / "profile" / "AppData" / "Local" / "bmad-loop" / "state"
    assert runs.state_root() == expected


def test_state_root_on_win32_refuses_a_home_derived_from_homedrive(tmp_path, monkeypatch):
    """With neither `%LOCALAPPDATA%` nor `%USERPROFILE%` set, refuse — do not fall
    back to a home directory.

    `Path.home()` is `ntpath.expanduser("~")`, which prefers `USERPROFILE` and then
    `HOMEDRIVE` + `HOMEPATH`; on a domain-joined machine that pair can name a
    network home share, and the control plane's `O_NOFOLLOW`-anchored writes and
    atomic renames are not something to move onto SMB by inference. So the
    variables are read by name and an absent store raises.

    Ablation target: replace the `USERPROFILE` read with `Path.home()` and this
    row fails — on Windows it derives `Z:\\users\\x`, and on a POSIX host running
    the faked branch `expanduser` falls back to the passwd entry. Both answer
    where the guard refuses to."""
    monkeypatch.setattr(runs.sys, "platform", "win32")
    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("HOMEDRIVE", "Z:")
    monkeypatch.setenv("HOMEPATH", "\\users\\x")

    with pytest.raises(runs.StateRootError, match=envvars.STATE_DIR):
        runs.state_root()


@pytest.mark.parametrize("home", ["", "relative-home", "~"], ids=repr)
def test_state_root_refuses_a_home_that_cannot_root_a_control_plane(monkeypatch, home):
    """A write path fails loud rather than picking a plausible-looking directory.

    Each row is an answer `expanduser` really gives: `""` for a set-but-empty
    `USERPROFILE` on Windows — and, on POSIX, `"/"`, since `posixpath` folds the
    empty prefix to the root; `"relative-home"` for a `HOME` that is not a path at
    all; and `"~"` for the input handed back when nothing can expand it. All three
    would otherwise mkdir the control plane somewhere silently wrong — the launch
    cwd, or `/.local/state`, which is a permission error for an ordinary user and
    a real write to `/` for a containerised root.

    The message names the override, because that is the one remedy an operator
    always has."""
    monkeypatch.setattr(runs.sys, "platform", "linux")
    _fake_home(monkeypatch, home)

    with pytest.raises(runs.StateRootError, match=envvars.STATE_DIR):
        runs.state_root()


def test_state_dir_for_is_keyed_on_project_identity_not_spelling(tmp_path, monkeypatch):
    """One project reached by two spellings must key to ONE control plane.

    It is the same requirement `project_tag` was written for, and it reaches here
    because the run and the hook that signals its completion can arrive with
    different spellings of the project: the engine holds a resolved path, a relay
    computes from what it was handed. Two keys would mean the poller watching one
    directory while the events land in the other — no completion signal, and a run
    that waits out `session_timeout_min` with the signal sitting on disk.

    Distinctness is asserted alongside identity: a key that collapsed every
    project would satisfy the first half by making all runs share one plane."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    absolute = runs.state_dir_for(project, "20260812-101500-ab12")
    relative = runs.state_dir_for(Path("proj"), "20260812-101500-ab12")
    assert absolute == relative
    assert absolute == runs.state_root() / runs.project_tag(project) / "20260812-101500-ab12"
    assert runs.events_dir_for(project, "20260812-101500-ab12") == absolute / "events"

    assert runs.state_dir_for(project, "20260812-101500-cd34") != absolute
    assert runs.state_dir_for(tmp_path / "other", "20260812-101500-ab12") != absolute


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_state_dir_for_follows_a_symlinked_project_to_one_key(tmp_path):
    """The symlink half of the spelling problem, and the one a lexical comparison
    of the two paths would never catch — they share no component."""
    project = tmp_path / "proj"
    project.mkdir()
    link = tmp_path / "link"
    link.symlink_to(project)

    assert runs.state_dir_for(link, "r1") == runs.state_dir_for(project, "r1")


def test_state_dir_for_raises_when_the_project_cannot_be_canonicalized(tmp_path, monkeypatch):
    """A project the OS refuses to canonicalize (#552: a registered-but-not-serving
    WSL UNC provider) has no knowable identity, so it gets no key.

    `project_tag`'s bare `resolve()` raising is the correct behaviour to inherit
    rather than soften. Degrading to the lexical spelling would hand two spellings
    of the one project two different control planes — the failure the test above
    exists to prevent — and this is a write path, where the doctrine is to raise
    (`platform_util.resolve_or_lexical`)."""
    project = tmp_path / "proj"
    project.mkdir()
    refuse_to_resolve(monkeypatch, project)

    with pytest.raises(OSError):
        runs.state_dir_for(project, "r1")


def test_config_digest_is_stamped_under_the_state_root_not_in_the_project(tmp_path):
    """#498's whole point: the baseline `resume` TRUSTS leaves the tree the driven
    sessions can write to.

    The negative half is the load-bearing one — asserting only that the state root
    holds the digest would still pass if this writer also dropped a copy in the
    project, and a file inside `.bmad-loop/` is exactly the thing a session edits
    to silence the warning.

    Scope, so the negative assert is not read as more than it is: it pins THIS
    function, which writes out of tree and nowhere else. The run as a whole does
    keep a second copy in `state.json` (`RunState.trusted_config_digest`, itself
    under `.bmad-loop/runs/`) — the travelling secondary a project move needs, and
    deliberately never preferred over this file. `test_cli` owns that precedence:
    `..._still_warns_when_a_session_rewrote_the_digest_in_state_json` proves the
    in-tree copy loses, `..._still_warns_after_the_project_is_renamed` proves it is
    consulted when this file is out of reach."""
    project = tmp_path / "proj"
    (project / ".bmad-loop").mkdir(parents=True)

    runs.write_trusted_config_digest(project, "r1", "abc123")

    path = runs.config_digest_path_for(project, "r1")
    assert path == runs.state_dir_for(project, "r1") / "config-digest"
    assert runs.read_trusted_config_digest(project, "r1") == "abc123"
    assert not any(p.is_file() for p in (project / ".bmad-loop").rglob("*"))


def test_read_trusted_config_digest_separates_an_absent_file_from_an_empty_one(tmp_path):
    """`None` and `""` are different answers and the resume acts on the
    difference: `None` means "this run predates #498, ask state.json", while `""`
    means "a baseline was stamped and it is empty" and must NOT reopen the
    agent-writable field. Collapsing them to `""` would retire the legacy runs'
    fallback; collapsing them to `None` would let a session that truncates the
    out-of-tree file fall back into the tree it controls.

    ABLATION: return `""` instead of `None` from the reader's except arm, or drop
    the `.strip()`-of-an-empty-file distinction, and one of these two fails."""
    project = tmp_path / "proj"
    project.mkdir()

    assert runs.read_trusted_config_digest(project, "r1") is None

    runs.write_trusted_config_digest(project, "r1", "")
    assert runs.read_trusted_config_digest(project, "r1") == ""


@pytest.mark.parametrize(
    "attr, exc",
    [
        ("state_root", runs.StateRootError("no root")),
        ("project_tag", OSError("cannot canonicalize")),
        ("project_tag", RuntimeError("Symlink loop from '/p'")),
    ],
    ids=["no-derivable-state-root", "unresolvable-project", "symlink-loop-project"],
)
def test_trusted_config_digest_read_degrades_where_the_write_raises(
    tmp_path, monkeypatch, attr, exc
):
    """The halves are deliberately asymmetric, and each row runs both.

    Reading is observation feeding an advisory warning, so an unnameable state
    root costs the warning and nothing else — and the resume is about to resolve
    the same root for its events channel, where the error is owned and reported.
    Writing is a repair write, and a silently skipped stamp is undetectable later:
    the next resume finds no file, falls back to a legacy field that is empty for
    any run this code started, and quietly declines to warn.

    The `RuntimeError` row is live below 3.13, where `Path.resolve` reports a
    symlink loop that way — same reason `_discard_state_dir` holds it.

    ABLATION: widen the write to swallow these and the second half passes."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(runs, attr, _raising(exc))

    assert runs.read_trusted_config_digest(project, "r1") is None
    with pytest.raises(type(exc)):
        runs.write_trusted_config_digest(project, "r1", "abc123")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_read_trusted_config_digest_refuses_a_planted_fifo_instead_of_hanging(tmp_path):
    """The read is on a path the driven session can reach, so its *shape* has to be
    established before any bytes are consumed. A FIFO opened for reading blocks
    until someone writes — indefinitely — and `resume` is a foreground command a
    human is waiting on, so this wedges the terminal rather than costing a warning.

    Guarded by `SIGALRM` because the failure mode under test IS a hang: an
    unguarded call would not fail, it would never return, and the suite would sit
    there until CI killed the job with no attributable test. The alarm converts
    "never returns" into a named assertion.

    Deliberately not a `multiprocessing.Process` with a join timeout, which was
    the first draft: the default start method on Linux is `fork`, and forking a
    process pytest may already have threaded earns a DeprecationWarning on 3.12+
    and risks a child deadlock — trading a hang under ablation for a possible hang
    in the ordinary run. The alarm stays in one process and needs no picklable
    target. POSIX-only, which this test already is.

    ABLATION: restore `Path.read_text` in the reader, or drop `O_NONBLOCK`, and
    the alarm fires. Dropping the `S_ISREG` check instead fails the assert rather
    than the alarm — with no writer the FIFO reads EOF, so the reader answers `""`
    where it owes `None`. Both are graded; the twin below covers the case where a
    writer makes those bytes attacker-chosen instead of empty."""
    import signal

    project = tmp_path / "proj"
    project.mkdir()
    path = runs.config_digest_path_for(project, "r1")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    def _blew_up(signum, frame):
        raise AssertionError("the read blocked on the FIFO instead of refusing it")

    previous = signal.signal(signal.SIGALRM, _blew_up)
    signal.alarm(20)
    try:
        assert runs.read_trusted_config_digest(project, "r1") is None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_read_trusted_config_digest_refuses_a_fed_fifo_instead_of_reading_it(tmp_path):
    """The twin of the FIFO test above, on the half that is the actual attack. That
    one plants an idle FIFO, so the harm is a hang and — with the `S_ISREG` check
    gone — the bytes read are merely empty. Here a writer is holding it open and
    feeding it, so a reader that got as far as `os.read` would come back with
    whatever the session piped in and treat it as this run's baseline: feed the
    digest of the config it just installed and `resume` is satisfied, feed noise
    and the operator is warned off a change nobody made. Neither is a hang, so the
    alarm above would never notice.

    Opened `O_RDWR` deliberately: a write-only open on a FIFO blocks until a reader
    arrives, which would wedge the test itself, and `O_RDWR` never blocks.

    ABLATION: drop the `S_ISREG` check and this returns the piped text instead of
    `None` — the assert names the value it got."""
    project = tmp_path / "proj"
    project.mkdir()
    path = runs.config_digest_path_for(project, "r1")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    holder = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(holder, b"ff" * 32 + b"\n")  # a plausible-looking sha256 hex digest
        assert runs.read_trusted_config_digest(project, "r1") is None
    finally:
        os.close(holder)


def test_read_trusted_config_digest_is_bounded(tmp_path):
    """A link to an endless source (`/dev/zero`) would otherwise read until
    `MemoryError` — a ValueError-family escape from a function that promises never
    to raise. The cap removes the condition rather than absorbing it.

    A large regular file stands in for the endless one: same read path, same
    bound, and it runs on every platform.

    What is asserted is the BOUND ON THE READ, which is not the same as the length
    of what comes back, and the difference is the whole point: a reader that
    slurped the file and only then truncated would return exactly
    `_MAX_DIGEST_BYTES` too, pass a returned-length assert, and still exhaust
    memory on /dev/zero — the condition this cap exists to remove rather than
    absorb. So the requested counts are captured at `os.read` and totalled. (The
    returned-length assert stays: it is what catches a cap applied to the read but
    not honoured afterwards.)

    ABLATION: two rows, and the first is the one a length-only assert misses.
    Slurp the whole file and truncate at the end — the total-bytes assert fails,
    the length assert does not. Drop `_MAX_DIGEST_BYTES` from the `os.read`
    outright and both fail."""
    project = tmp_path / "proj"
    project.mkdir()
    path = runs.config_digest_path_for(project, "r1")
    path.parent.mkdir(parents=True)
    path.write_text("a" * (1024 * 1024))

    requested: list[int] = []
    real_read = os.read

    def _spy(fd: int, n: int) -> bytes:
        requested.append(n)
        return real_read(fd, n)

    with mock.patch.object(runs.os, "read", _spy):
        got = runs.read_trusted_config_digest(project, "r1")

    assert got is not None
    assert len(got) == runs._MAX_DIGEST_BYTES
    # The read never asks for more than the cap, however many calls it makes.
    assert requested, "the spy saw no read at all — the assertion below would be vacuous"
    assert sum(requested) <= runs._MAX_DIGEST_BYTES


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_read_trusted_config_digest_does_not_follow_a_planted_symlink(tmp_path):
    """`O_NOFOLLOW`: the name is read, not wherever it points. Without it a session
    aims the orchestrator's read at any file the orchestrator can open, and the
    "digest" it comes back with is that file's contents.

    ABLATION: drop `O_NOFOLLOW` from the flags and the read returns the target's
    contents instead of `None`."""
    project = tmp_path / "proj"
    project.mkdir()
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("not-the-digest")
    path = runs.config_digest_path_for(project, "r1")
    path.parent.mkdir(parents=True)
    path.symlink_to(secret)

    assert runs.read_trusted_config_digest(project, "r1") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_write_trusted_config_digest_replaces_a_planted_symlink(tmp_path):
    """`follow_symlinks=False`, and the reason is that this record lives under a
    root whose path the driven session is handed — the engine exports the sibling
    events dir as `BMAD_LOOP_EVENTS_DIR`. Following a link planted at the digest's
    name would aim an orchestrator write at a path of the session's choosing.

    ABLATION: drop `follow_symlinks=False` and the target below is what gets
    written."""
    project = tmp_path / "proj"
    project.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("untouched")
    path = runs.config_digest_path_for(project, "r1")
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    runs.write_trusted_config_digest(project, "r1", "abc123")

    assert target.read_text() == "untouched"
    assert not path.is_symlink()
    assert runs.read_trusted_config_digest(project, "r1") == "abc123"


def test_the_state_dir_gc_reclaims_the_config_digest(tmp_path):
    """#498's GC is #494's GC — the digest is a file inside the run's state dir, so
    the lifecycle that already reclaims that subtree reclaims this too. Asserted
    rather than assumed: a digest stamped somewhere the sweep does not reach would
    leak one file per run, outside the project, for the life of the machine.

    Deliberately the ORPHAN SWEEP and not `delete_run`, which was the first draft
    and was fake green — it removes the run dir as well, so it passes whether the
    digest is out of tree or sitting in `.bmad-loop/runs/<id>/`, which is the one
    thing this needs to tell apart. `reconcile_orphan_state_dirs` reaches only
    out-of-tree state, so a digest that drifted back into the project survives it
    and this reddens."""
    runs.write_trusted_config_digest(tmp_path, "r1", "abc123")
    digest = runs.config_digest_path_for(tmp_path, "r1")
    assert digest.is_file()

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [runs.state_dir_for(tmp_path, "r1")]

    assert not digest.exists()


def test_prunable_sessions_accepts_legacy_path_tag(tmp_path, monkeypatch):
    """A pre-digest tag stays ours; another project's path or digest stays foreign."""
    legacy = str(tmp_path.resolve())
    fin = _make_state_run(tmp_path, "legacy-fin")
    (fin / "engine.pid").write_text(str(_dead_pid()))
    sessions = ["bmad-loop-legacy-fin", "bmad-loop-legacy-other", "bmad-loop-legacy-digest"]
    monkeypatch.setattr(runs, "mux_sessions", lambda: sessions)
    monkeypatch.setattr(
        runs,
        "session_project_tags",
        lambda: {
            "bmad-loop-legacy-fin": legacy,
            "bmad-loop-legacy-other": "/some/other/project",
            "bmad-loop-legacy-digest": runs.project_tag(tmp_path / "other"),
        },
    )
    prunable, live, unknown = runs.prunable_sessions(tmp_path)
    assert prunable == ["legacy-fin"]
    assert live == [] and unknown == set()


def test_prunable_sessions_claims_an_untagged_session_on_a_run_id_collision(tmp_path, monkeypatch):
    """Characterization (#419): for an untagged session, ownership is proven by run-id
    collision on the filesystem, not by identity — so a project that happens to hold a
    dead run dir with the same id classifies *another* project's session as prunable,
    reading its own pid file to make the call.

    Nothing in the fixture marks the session as foreign, because nothing can: that is
    the defect. Foreignness is constructed out of band — `theirs` created the session
    (untagged, because its tag write failed or it predates a working one) and is still
    running it, while `ours` only shares the id. Both views are asserted, so the test
    shows the two projects disagreeing about one live session rather than just echoing
    one side.

    The collision needs no luck: `--run-id` is accepted from the CLI and validated for
    shape only, so a script reusing one fixed id across two projects reproduces it.

    Pinned as the actual outcome, not the desired one. #523's digest keeps ordinary
    paths tagged, so this is reachable only for untagged state, and closing it needs a
    second ownership proof that outlives the run dir (#419 direction 2) — not a change
    here, and not the removal backstop (#526 residual A), which guards a different
    sequence. Flip this test deliberately if that proof ever lands."""
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    collided = "shared-id"
    # their run: engine alive, so the session is genuinely in use
    runs.write_pid(_make_state_run(theirs, collided))
    # our run: same id, dead engine — the only thing that makes us claim the session
    (_make_state_run(ours, collided) / "engine.pid").write_text(str(_dead_pid()))

    monkeypatch.setattr(runs, "mux_sessions", lambda: [runs.session_name(collided)])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})  # untagged everywhere

    assert runs.prunable_sessions(ours) == ([collided], [], set())  # ours would kill it
    assert runs.prunable_sessions(theirs) == ([], [collided], set())  # theirs is using it


def test_prunable_sessions_skips_invalid_run_ids(tmp_path, monkeypatch):
    """A session name is untrusted input (anyone can create one). Stripping the
    prefix off `bmad-loop-../../x` would hand `run_dir_for` a traversing id, and a
    tagged session would then steer engine_liveness — and prune_sessions' kill — at
    a path outside the runs dir. Reject before recomposing."""
    mine = runs.project_tag(tmp_path)
    good = _make_state_run(tmp_path, "fin-1")
    (good / "engine.pid").write_text(str(_dead_pid()))

    sessions = ["bmad-loop-fin-1", "bmad-loop-../../x", "bmad-loop-a.b", "bmad-loop-"]
    monkeypatch.setattr(runs, "mux_sessions", lambda: sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: dict.fromkeys(sessions, mine))

    prunable, live, unknown = runs.prunable_sessions(tmp_path)
    assert prunable == ["fin-1"]
    assert live == [] and unknown == set()


def test_prunable_sessions_flags_unknown(tmp_path, monkeypatch):
    # live pid, unreadable identity (win32 ERROR_ACCESS_DENIED) → prunable anyway
    # (unknown never blocks cleanup) but flagged so frontends can warn.
    mine = runs.project_tag(tmp_path)
    odd = _make_state_run(tmp_path, "odd-1")
    (odd / "engine.pid").write_text("4242 123.0")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-odd-1"])
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
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-fin-1"])
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
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-odd-1"])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {"bmad-loop-odd-1": mine})
    monkeypatch.setattr(runs, "get_process_host", lambda: _FakeHost(alive=True, identity=None))
    assert runs.prune_sessions(tmp_path) == (["odd-1"], [], {"odd-1"})
    assert killed == ["odd-1"]


def _seed_state_dir(project, run_id) -> Path:
    """The out-of-tree control plane a driven run leaves behind: its events dir
    holding one already-consumed completion signal."""
    events = runs.events_dir_for(project, run_id)
    events.mkdir(parents=True)
    (events / "1700000000-t1-Stop.json").write_text("{}")
    return runs.state_dir_for(project, run_id)


def _raising(exc: Exception):
    def _fail(*_args, **_kwargs):
        raise exc

    return _fail


def test_delete_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "r1")
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def test_delete_run_removes_the_out_of_tree_state_counterpart(tmp_path):
    """#494 moved the events channel out of the project tree, so removing the run
    dir stopped removing everything the run owns. Without this tail every delete
    leaks a subtree under the user-scoped state root — outside the project, where
    no operator thinks to look — one per run, for the life of the machine."""
    run_dir = _make_state_run(tmp_path, "r1")
    state_dir = _seed_state_dir(tmp_path, "r1")

    runs.delete_run(tmp_path, run_dir)

    assert not run_dir.exists()
    assert not state_dir.exists()


@pytest.mark.parametrize(
    "attr, exc",
    [
        ("state_root", runs.StateRootError("no root")),
        ("project_tag", OSError("cannot canonicalize")),
        ("project_tag", RuntimeError("Symlink loop from '/p'")),
    ],
    ids=["no-derivable-state-root", "unresolvable-project", "symlink-loop-project"],
)
def test_delete_run_survives_a_counterpart_it_cannot_name(tmp_path, monkeypatch, attr, exc):
    """The counterpart removal is a never-raise tail (#139 teardown doctrine).

    Every row is the counterpart being *unnameable*, which is the only failure
    that can escape: an environment with no derivable state root, and a project
    the OS refuses to canonicalize (#552). Removal failures are absorbed
    separately, by `ignore_errors`.

    The `RuntimeError` row is not a hypothetical type: `project_tag` resolves
    before digesting, and below 3.13 `Path.resolve` reports a symlink loop as
    `RuntimeError` rather than `OSError` — measured across the support matrix,
    3.11 and 3.12 raise it where 3.13 and 3.14 return the unresolved path. So on
    two supported interpreters this is the live arm, and it is injected here
    rather than built from real symlinks because the loop would have to sit on
    the *project* path, which the sandbox fixtures own.

    Raising would be worse than the leak it reports. The run dir is already gone
    by this point, so the exception would fail a delete that in fact happened and
    send the operator to retry a removal that can only fail the same way — while
    `reconcile_orphan_state_dirs` already backstops the leak."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, attr, _raising(exc))

    runs.delete_run(tmp_path, run_dir)

    assert not run_dir.exists()


def test_delete_run_refuses_while_the_agent_session_is_live(tmp_path, monkeypatch):
    """The #419 backstop: every caller's guard is keyed on engine pid liveness, so an
    orphan (engine dead, session alive) reaches here. For an untagged session the run
    dir is the only ownership proof a later prune can read, so the dir must outlive
    the session, not the other way round."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-r1"])
    with pytest.raises(runs.LiveSessionError, match="still live") as exc:
        runs.delete_run(tmp_path, run_dir)
    assert run_dir.exists()
    # The remedy it names is not sound on its own: `cleanup` proves an untagged
    # session ours by this same run dir, so it can prune another project's on a
    # shared id (the case the collision test above pins). The message must carry
    # the confirmation step, not just the command.
    assert "bmad-loop cleanup" in str(exc.value)
    assert "bmad-loop attach r1" in str(exc.value)


def test_delete_run_ignores_a_session_proven_to_be_another_project_s(tmp_path, monkeypatch):
    """The guard is scoped to what it can justify. A tag outside `accepted_tags`
    proves the session foreign, and a tagged session carries its own ownership
    proof — it does not need this run dir — so removing the dir strands nothing.

    Refusing here would be a pure false positive that wedges every removal path
    for as long as the other project's run lives, `clean` included, and `clean`
    has no override. Untagged still refuses: unread is not proof (see the
    degradation test above)."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-r1"])
    monkeypatch.setattr(
        runs,
        "session_project_tags",
        lambda: {"bmad-loop-r1": runs.project_tag(tmp_path / "someone-else")},
    )
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def test_delete_run_refuses_a_session_tagged_as_ours(tmp_path, monkeypatch):
    """The mirror of the test above, so the tag read cannot be mistaken for "any
    tag clears the guard". Our own tag proves nothing about whether the removal is
    safe — it only fails to prove the session foreign — so the refusal stands."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-r1"])
    monkeypatch.setattr(
        runs, "session_project_tags", lambda: {"bmad-loop-r1": runs.project_tag(tmp_path)}
    )
    with pytest.raises(runs.LiveSessionError):
        runs.delete_run(tmp_path, run_dir)
    assert run_dir.exists()


def test_delete_run_proceeds_when_the_session_listing_raises(tmp_path, monkeypatch):
    """The seam only promises `pipe_pane` and `kill_session` never raise, so an
    out-of-tree backend answers a failed listing with MultiplexerError where the
    bundled one answers `[]`. Both must reach the same place, or the guard would
    turn a transient transport error into a failed `delete`/`archive`/`clean` —
    and `clean` has no override. Degrading to "no session" matches what tmux
    already does for a dead server."""
    run_dir = _make_state_run(tmp_path, "r1")

    def boom():
        raise MultiplexerError("transport down")

    monkeypatch.setattr(runs, "mux_sessions", boom)
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def test_delete_run_refuses_when_the_tag_read_raises(tmp_path, monkeypatch):
    """The other read degrades the other way. By the time the tag is queried the
    listing has already proven a session live, and a tag that could not be read is
    not proof it is another project's — so it reads as untagged and the refusal
    stands. Asserted separately from the listing case: one `except` returning the
    wrong constant would otherwise hide behind the other."""
    run_dir = _make_state_run(tmp_path, "r1")

    def boom(*_args):
        raise MultiplexerError("option read failed")

    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-r1"])
    monkeypatch.setattr(runs, "session_project_tags", boom)
    with pytest.raises(runs.LiveSessionError):
        runs.delete_run(tmp_path, run_dir)
    assert run_dir.exists()


def test_delete_run_matches_the_session_by_exact_run_id(tmp_path, monkeypatch):
    """The guard keys on `bmad-loop-<id>` exactly. A session for a *different* run —
    including one whose id merely extends ours — must not block this removal, or one
    live run would wedge cleanup for every id it prefixes."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-r1-2", "bmad-loop-ctl", "r1"])
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


@pytest.mark.usefixtures("force_tmux_backend")  # the degradation is the seam's, not a stub's
def test_delete_run_proceeds_when_the_multiplexer_cannot_answer(tmp_path, monkeypatch):
    """Observation degrades: an absent multiplexer, a dead server, or a failed query
    all read as "no session" (mux_sessions returns []). A removal the operator asked
    for must not be blocked by an unanswerable question — the cost is only that the
    backstop is inert there, which is the pre-#419 behavior."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: None)  # no tmux at all
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def _escalated_run(tmp_path, spec_text, *, restore_patch_stale=None, git_project=False):
    """conftest's builder with this module's shape: the spec is written first (so
    `git_project=True` commits it), and only `(run_dir, spec)` comes back."""
    spec = tmp_path / "spec.md"
    spec.write_text(spec_text, encoding="utf-8")
    run = escalated_run(
        tmp_path,
        "r1",
        story_key="1-1-a",
        attempt=2,
        started_at="2026-06-11T10:00:00",
        spec_file=str(spec),
        restore_patch=restore_patch_stale,
        git_project=git_project,
    )
    return run.run_dir, spec


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


def test_rearm_aborts_when_the_spec_status_cannot_be_reopened(tmp_path):
    """The seam that proves the silent-`False` defect mattered. This spec reads as
    `status: blocked` — the reader resolves the block scalar fine — so it clears
    every gate ahead of the flip, and the flip is the one thing that cannot
    happen. Under the old line scanner that was a `False` nobody read: the re-drive
    was dispatched, step-01 saw the unchanged terminal status and routed the
    session to "ingest as context, do not resume", and the story re-wedged.

    Nothing may be persisted: `save_state` runs BELOW this point, so the task must
    still be ESCALATED at attempt 2 and the escalation still armed for a retry."""
    from bmad_loop.model import Phase

    spec_text = (
        "---\ntitle: t\nstatus: |\n  blocked\n---\n\n## Intent\n\nbody\n"
        "\n## Auto Run Result\n\n- Status: blocked\n\nboom\n"
    )
    run_dir, spec = _escalated_run(tmp_path, spec_text)
    assert verify.status_of(verify.read_frontmatter(spec)) == "blocked"  # the reader is fine

    with pytest.raises(runs.RearmError, match="re-open story spec"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    assert spec.read_text(encoding="utf-8") == spec_text  # byte-identical
    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED and task.attempt == 2  # nothing persisted
    assert task.restore_patch is None  # the latch never landed either


def test_rearm_resets_followup_reviews_spent(tmp_path):
    """A human-resolved re-drive gets a fresh damping budget: rearm_escalation
    zeroes followup_reviews_spent alongside review_cycle, so the clean rebuild
    against the corrected spec can honor a follow-up again."""
    run_dir, _ = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    # seed a spent damping budget from the escalated attempt
    state = load_state(run_dir)
    state.tasks["1-1-a"].followup_reviews_spent = 3
    state.tasks["1-1-a"].review_cycle = 2
    save_state(run_dir, state)

    runs.rearm_escalation(run_dir)

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.followup_reviews_spent == 0
    assert task.review_cycle == 0  # reset in lockstep with the counter


# --------------------------------------------- #90: abandoned restore-latch residue


def _stale_restore_tree(tmp_path, *, latch="artifacts/attempt.patch"):
    """An escalation whose latched restore already applied: `newfile.txt` is the
    patch's untracked creation, `human.txt` is the resolve session's own file."""
    run_dir, spec = _escalated_run(
        tmp_path, _SPEC_WITH_ARR, restore_patch_stale=latch, git_project=True
    )
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        "diff --git a/newfile.txt b/newfile.txt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/newfile.txt\n"
        "@@ -0,0 +1 @@\n"
        "+from the abandoned attempt\n",
        encoding="utf-8",
    )
    (tmp_path / "newfile.txt").write_text("from the abandoned attempt\n")  # the applied residue
    (tmp_path / "human.txt").write_text("from the resolve session\n")
    return run_dir, spec, patch


def _kinds(run_dir, prefix="stale-restore-"):
    from bmad_loop.journal import Journal

    return [e for e in Journal(run_dir).entries() if e["kind"].startswith(prefix)]


def test_rearm_excludes_stale_restore_residue_from_baseline_snapshot(tmp_path):
    """The abandoned attempt's applied new files must NOT be blessed as
    pre-existing, or finalize_commit's `add -A` sweeps them into the corrected
    story's commit. The resolve session's own untracked file still is."""
    run_dir, _spec, patch = _stale_restore_tree(tmp_path)

    runs.rearm_escalation(run_dir)  # from-scratch re-arm replaces the latch

    task = load_state(run_dir).tasks["1-1-a"]
    assert "human.txt" in task.baseline_untracked
    assert "newfile.txt" not in task.baseline_untracked
    assert (tmp_path / "newfile.txt").exists()  # rearm deletes nothing; the re-drive's reset does
    excluded = _kinds(run_dir, "stale-restore-excluded")
    assert len(excluded) == 1
    assert excluded[0]["files"] == ["newfile.txt"]
    assert excluded[0]["patch"] == str(patch)


def test_rearm_re_latching_the_same_patch_still_excludes_its_residue(tmp_path):
    """Re-arming a restore onto the same patch: the first application's files are
    still residue (and `git apply` would otherwise fail with 'already exists')."""
    run_dir, _spec, _patch = _stale_restore_tree(tmp_path)

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.restore_patch == "artifacts/attempt.patch"
    assert "human.txt" in task.baseline_untracked
    assert "newfile.txt" not in task.baseline_untracked
    assert _kinds(run_dir, "stale-restore-excluded")


def test_rearm_missing_stale_patch_degrades_loudly_without_raising(tmp_path):
    """A deleted patch file must never wedge resolve: journal the degrade and fall
    back to the pre-#90 snapshot (everything untracked counts as pre-existing)."""
    run_dir, _spec, patch = _stale_restore_tree(tmp_path)
    patch.unlink()
    (tmp_path / "committed.txt").write_text("from the escalated attempt\n")
    git(tmp_path, "add", "committed.txt")
    git(tmp_path, "commit", "-q", "-m", "attempt commit")

    runs.rearm_escalation(run_dir)  # must not raise RearmError

    task = load_state(run_dir).tasks["1-1-a"]
    assert {"human.txt", "newfile.txt"} <= set(task.baseline_untracked)  # full snapshot
    unparseable = _kinds(run_dir, "stale-restore-unparseable")
    assert len(unparseable) == 1
    assert "FileNotFoundError" in unparseable[0]["error"]
    assert not _kinds(run_dir, "stale-restore-excluded")
    # the unreadable patch must not also cost the human the commits warning
    assert _kinds(run_dir, "stale-restore-commits")


def test_rearm_without_a_stale_latch_journals_no_stale_restore_events(tmp_path):
    run_dir, _spec = _escalated_run(tmp_path, _SPEC_WITH_ARR, git_project=True)
    (tmp_path / "human.txt").write_text("from the resolve session\n")

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    assert "human.txt" in load_state(run_dir).tasks["1-1-a"].baseline_untracked
    assert _kinds(run_dir) == []


def test_rearm_warns_about_commits_below_the_refreshed_baseline(tmp_path):
    """The worse variant: commits made above the OLD baseline become the re-drive's
    permanent starting point. Warn-only — a mechanical revert would claw back the
    resolve session's own blessed commits, which live in the same range."""
    run_dir, _spec, _patch = _stale_restore_tree(tmp_path)
    (tmp_path / "committed.txt").write_text("from the escalated attempt\n")
    git(tmp_path, "add", "committed.txt")
    git(tmp_path, "commit", "-q", "-m", "attempt commit")
    old_baseline = load_state(run_dir).tasks["1-1-a"].baseline_commit

    runs.rearm_escalation(run_dir)

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.baseline_commit != old_baseline  # baseline advanced past the commit
    warned = _kinds(run_dir, "stale-restore-commits")
    assert len(warned) == 1
    assert warned[0]["old_baseline"] == old_baseline
    assert warned[0]["commits"] == [git(tmp_path, "rev-parse", "HEAD")]


def test_archive_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "journal.jsonl").write_text('{"kind":"x"}\n')
    dest = runs.archive_run(tmp_path, run_dir)

    assert dest == tmp_path / ".bmad-loop" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert dest.is_file()
    assert not run_dir.exists()  # original removed
    # An exhaustive listing, not `not dest.with_suffix(".tar.gz.tmp").exists()`: that
    # spelling was the SAME buggy expression as the source it graded (`with_suffix`
    # replaces only the last suffix, so on `<id>.tar.gz` it yields
    # `<id>.tar.tar.gz.tmp`), and right only by accident. After the #363 filename fix
    # it named a path existing under neither spelling and would have gone silently
    # vacuous. Listing the directory cannot: it names every leftover, whatever it is
    # called.
    assert [p.name for p in dest.parent.iterdir()] == ["20260611-100000-aaaa.tar.gz"]
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert "20260611-100000-aaaa/state.json" in names
    assert "20260611-100000-aaaa/journal.jsonl" in names


def test_archive_run_names_its_temp_after_the_destination(tmp_path, monkeypatch):
    """#363's filename half, and it needs its own test because NOTHING else grades
    it: on the happy path `atomic_replace` consumes the temp under either spelling,
    and the new `except BaseException` guard unlinks it whatever it is named — so
    reverting the fix reddens no other row in this file. Recording the name handed to
    `os.replace` is the only place the spelling is observable.

    `dest.with_suffix(".tar.gz.tmp")` yielded `<id>.tar.tar.gz.tmp`, because
    `with_suffix` replaces only the LAST suffix and `<id>.tar.gz` has stem
    `<id>.tar` — not the name the docstring implied, and not one any cleanup could
    have been written against.

    `run_dir` is built BEFORE the patch on purpose: `save_state` writes through the
    same `os.replace`, and building it after would pollute `seen` with a call that
    has nothing to do with the archive.

    Ablation A9: restore `dest.with_suffix(".tar.gz.tmp")` and this reddens alone —
    `test_archive_run` and the guard test below stay GREEN, which is exactly why
    this row exists rather than being folded into either."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    seen: list[str] = []
    real = os.replace

    def record(src, dst):
        seen.append(os.path.basename(src))
        real(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)
    dest = runs.archive_run(tmp_path, run_dir)

    # an exact list, so it also pins "exactly one replace" — a retry loop or a second
    # write creeping in here would redden rather than pass on a substring match
    assert seen == [dest.name + ".tmp"]


def test_archive_run_failed_replace_strands_no_temp(tmp_path, monkeypatch):
    """#363. `.bmad-loop/archive/` is gitignored by NOTHING — `bmad-loop init` writes
    `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml` and
    `_bmad/render/` — so a temp stranded here is an untracked file that holds
    `verify.worktree_clean` False until a human deletes it by hand.

    This site cannot use `atomic_write_*`: the path is handed to `tarfile.open`, so
    there is no payload for a helper to take. It gets the house guard instead, the
    one `operatoractions.record_park` uses.

    A PLAIN OSError, never PermissionError: `_retry_on_sharing_violation` treats
    PermissionError (and winerror 5/32) as the transient Windows sharing violation
    and would burn ~5 s of jittered backoff before propagating it.

    Ablation A8: delete the `except BaseException` guard and ONLY the third assertion
    reddens — the first two are pinned by statement ordering (`shutil.rmtree` runs
    after the replace), not by the guard, so the leftover-temp row is the one that
    grades it."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(platform_util.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        runs.archive_run(tmp_path, run_dir)

    assert (run_dir / "state.json").is_file()  # the run survives a failed archive
    assert list((tmp_path / ".bmad-loop" / "archive").iterdir()) == []  # no temp left


def test_archive_run_refuses_while_the_agent_session_is_live(tmp_path, monkeypatch):
    """Same backstop as delete (#419), and it runs before the tarball is written —
    a refusal must not leave a half-archived run behind for the operator to find."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-20260611-100000-aaaa"])
    with pytest.raises(runs.LiveSessionError, match="still live"):
        runs.archive_run(tmp_path, run_dir)
    assert run_dir.exists()
    assert not (tmp_path / ".bmad-loop" / "archive").exists()


def test_archive_run_removes_the_out_of_tree_state_counterpart(tmp_path):
    """Archive inherits delete's tail — it removes the run dir just the same, so
    it would leak the same subtree.

    The ordering is what the assertions pin: the tarball is complete before the
    counterpart goes. Since #494 that tarball no longer carries the run's
    `events/` — the channel is out of the tree and never enters the tar — which
    the recorded decision accepts: those files are transient completion signals
    the watcher consumed while the run was live, and everything an archive is
    read for later (state, journal, tasks, logs) is in the run dir."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    state_dir = _seed_state_dir(tmp_path, "20260611-100000-aaaa")

    dest = runs.archive_run(tmp_path, run_dir)

    with tarfile.open(dest) as tar:
        assert "20260611-100000-aaaa/state.json" in tar.getnames()
    assert not state_dir.exists()


# ------------------------------------------------- orphan state-dir sweep (#494)


def test_reconcile_orphan_state_dirs_removes_only_what_has_no_run_dir(tmp_path):
    """The GC backstop for everything `_discard_state_dir` cannot reach: a run dir
    removed by hand, an `rm -rf .bmad-loop`, a delete from before that tail
    existed. Distinctness is the load-bearing half — a sweep that took the live
    run's control plane too would strand a resumable run's completion channel."""
    _make_state_run(tmp_path, "live-1")
    kept = _seed_state_dir(tmp_path, "live-1")
    orphan = _seed_state_dir(tmp_path, "gone-1")

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [orphan]

    assert not orphan.exists()
    assert kept.is_dir()


def test_reconcile_orphan_state_dirs_keeps_a_run_dir_with_no_state_json(tmp_path):
    """Existence of the *directory* is the test, not `list_run_dirs`, which is
    state.json-gated.

    A run whose state.json is missing or corrupt is exactly the run an operator is
    trying to recover, and it still owns its control plane. Reading liveness from
    the gated listing would sweep the counterpart out from under it — deleting
    state on the strength of state being unreadable."""
    _make_run(tmp_path, "corrupt-1", with_state=False)
    kept = _seed_state_dir(tmp_path, "corrupt-1")

    assert runs.reconcile_orphan_state_dirs(tmp_path) == []

    assert kept.is_dir()


def test_reconcile_orphan_state_dirs_dry_run_reports_without_removing(tmp_path):
    """`clean --dry-run` promises a preview: the plan must name the work and leave
    the disk alone, so the count a caller pre-flights is the count they get."""
    orphan = _seed_state_dir(tmp_path, "gone-1")

    assert runs.reconcile_orphan_state_dirs(tmp_path, dry_run=True) == [orphan]

    assert orphan.is_dir()


def test_reconcile_orphan_state_dirs_sweeps_a_project_whose_runs_dir_is_gone(tmp_path):
    """`rm -rf .bmad-loop` is the leak this exists for, and it is the case a
    missing runs dir has to answer *as an answer*: no runs exist, so every state
    dir under this project's key is an orphan. Reading it as "cannot tell" would
    leave the whole subtree behind permanently — nothing will ever re-create the
    runs dir with those ids in it."""
    orphan = _seed_state_dir(tmp_path, "gone-1")
    assert not (tmp_path / ".bmad-loop" / "runs").exists()

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [orphan]

    assert not orphan.exists()


def test_reconcile_orphan_state_dirs_sweeps_nothing_when_the_runs_dir_cannot_be_read(
    tmp_path, monkeypatch
):
    """The mirror of the test above, and the reason the two failures are told
    apart. An unreadable runs dir answers nothing at all — treating it like the
    missing one would sweep every control plane this project has, live runs
    included, on the strength of a transient permission error.

    The fault is scoped to the runs dir on purpose. A blanket `os.scandir` raise
    also takes out the state-root enumeration below it (and `rmtree`), so the
    sweep returns `[]` whatever the arm under test does — measured: with the
    degradation ablated to `set()` the test still passed, which is the negative
    assertion holding for a reason that has nothing to do with the gate."""
    _make_state_run(tmp_path, "live-1")
    kept = _seed_state_dir(tmp_path, "live-1")
    runs_dir = tmp_path / ".bmad-loop" / "runs"
    real_scandir = runs.os.scandir

    def _refuse_only_the_runs_dir(path, *args, **kwargs):
        if Path(path) == runs_dir:
            raise PermissionError("nope")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(runs.os, "scandir", _refuse_only_the_runs_dir)

    assert runs.reconcile_orphan_state_dirs(tmp_path) == []

    assert kept.is_dir()


def test_reconcile_orphan_state_dirs_leaves_another_projects_subtree_alone(tmp_path):
    """One state root holds every project's control planes, keyed by project
    identity. The sweep enumerates its own key's subtree only — a sweep from the
    root would let one project's `clean` delete another project's live runs, and
    the two need not even be on the same disk."""
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    (mine / ".bmad-loop" / "runs").mkdir(parents=True)
    _make_state_run(theirs, "live-1")
    foreign = _seed_state_dir(theirs, "live-1")
    # my own orphan, so the sweep provably enumerates rather than finding nothing
    orphan = _seed_state_dir(mine, "gone-1")

    assert runs.reconcile_orphan_state_dirs(mine) == [orphan]

    assert not orphan.exists()
    assert foreign.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_reconcile_orphan_state_dirs_never_removes_through_a_symlink(tmp_path):
    """A symlink is not a state dir we created, so it is skipped rather than
    followed — and this row points *inside* the root, where the containment test
    below it cannot help.

    Reporting it would be a false count even where the removal fails harmlessly
    (`rmtree` refuses a symlink): `clean` would claim a sweep that never happened
    and go on claiming it every run. On Windows the containment test carries the
    case this one cannot: a **junction** reads as a plain directory
    (`is_symlink()` is False) but `resolve()` follows it, so without the
    containment test `rmtree` would delete the target's contents outside the
    root. That row is POSIX-invisible and is not graded here."""
    _make_state_run(tmp_path, "live-1")
    target = _seed_state_dir(tmp_path, "live-1")
    link = runs.project_state_root(tmp_path) / "ghost-1"
    link.symlink_to(target)

    assert runs.reconcile_orphan_state_dirs(tmp_path) == []

    assert link.is_symlink()  # not followed, not removed
    assert target.is_dir()


@pytest.mark.parametrize(
    "attr, exc",
    [
        ("state_root", runs.StateRootError("no root")),
        ("project_tag", OSError("cannot canonicalize")),
        ("project_tag", RuntimeError("Symlink loop from '/p'")),
    ],
    ids=["no-derivable-state-root", "unresolvable-project", "symlink-loop-project"],
)
def test_reconcile_orphan_state_dirs_degrades_when_the_root_cannot_be_named(
    tmp_path, monkeypatch, attr, exc
):
    """Reclamation, not repair: a sweep that cannot name its root sweeps nothing
    and says so, rather than failing the whole `clean` around it. Leaving disk
    behind is the cheap outcome here — the caller's real work (worktrees, trims,
    archives) has already been done by the time this runs.

    `RuntimeError` is the below-3.13 spelling of a symlink loop out of
    `Path.resolve`, which `project_tag` calls; see the sibling delete test for
    the measured version split."""
    monkeypatch.setattr(runs, attr, _raising(exc))

    assert runs.reconcile_orphan_state_dirs(tmp_path) == []


def test_reconcile_orphan_state_dirs_keeps_a_run_that_starts_mid_sweep(tmp_path, monkeypatch):
    """`clean` is an operator command with no lock against a run starting, and the
    two reads it makes are of different trees. A run creates its run dir strictly
    before its state dir (`compose_run` builds the `Journal`, which mkdirs the run
    dir, and only then calls `make_adapters`, whose `SignalWatcher` mkdirs the
    events dir) — so reading state entries FIRST is what makes the ordering carry
    the guarantee: an entry seen there had its run dir on disk even earlier, and
    the later `live` read cannot miss it.

    Read the other way round, a run that starts in the gap is absent from `live`
    and present in `entries`, and `clean` deletes the control plane of a run that
    is starting right now. The cost is not a lost directory — it is the run, which
    then polls a primary that no longer exists or never sees its Stop and waits
    out `session_timeout_min`.

    The gap is simulated where it actually lives, by starting a run *inside* the
    `live` read rather than by patching the sweep: whichever read runs second is
    the one that sees `racer`, which is exactly what a real interleaving does.

    Ablation guard: move the `live = _run_dir_names(project)` read back above the
    `entries` enumeration and this fails, sweeping `racer` mid-startup."""
    _make_state_run(tmp_path, "live-1")
    _seed_state_dir(tmp_path, "live-1")
    orphan = _seed_state_dir(tmp_path, "ghost-1")

    real_names = runs._run_dir_names
    racer: list[Path] = []

    def _names_then_a_new_run(project: Path):
        names = real_names(project)  # the snapshot, taken before `racer` exists
        _make_state_run(project, "racer")
        racer.append(_seed_state_dir(project, "racer"))
        return names

    monkeypatch.setattr(runs, "_run_dir_names", _names_then_a_new_run)

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [orphan]

    assert not orphan.exists()
    assert racer[0].is_dir(), "swept the control plane of a run that was starting"


def test_reconcile_orphan_state_dirs_skips_an_entry_it_cannot_resolve(tmp_path, monkeypatch):
    """The containment test resolves each candidate, so it inherits the same
    below-3.13 `RuntimeError` a symlink loop raises — and here it lands *per
    entry*, mid-sweep, after earlier entries have already been removed. An
    unguarded loop would abort `clean` half-done and report none of what it had
    just deleted.

    Skipping is the safe arm rather than sweeping: an entry that cannot be
    resolved cannot be proven inside the root, and that proof is the only thing
    standing between `rmtree` and a Windows junction's target.

    Ablation guard: drop `RuntimeError` from the containment guard and this
    raises instead of returning the resolvable orphan."""
    _make_state_run(tmp_path, "live-1")
    _seed_state_dir(tmp_path, "live-1")
    good = _seed_state_dir(tmp_path, "ghost-good")
    bad = _seed_state_dir(tmp_path, "ghost-loop")

    real_resolve = Path.resolve

    def _resolve(self: Path, *args, **kwargs):
        if self == bad:
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [good]
    assert not good.exists() and bad.is_dir()


# ---- run inventory (moved from tui/data.py, #650)
# discover_runs and its status/liveness closure are core now: `bmad-loop list`
# reads them and must not drag the [tui] extra in. The RunWatcher halves of the
# split tests stay in test_tui_data.py.


def test_discover_runs_missing_dir(tmp_path):
    assert runs.discover_runs(tmp_path) == []


def test_discover_runs_classification(tmp_path):
    _make_state_run(tmp_path, "20260611-100000-aaaa", finished=True)
    _make_state_run(tmp_path, "20260611-110000-bbbb", paused_reason="escalation")
    alive_dir = _make_state_run(tmp_path, "20260611-120000-cccc")
    runs.write_pid(alive_dir)  # test process pid: alive
    gone_dir = _make_state_run(tmp_path, "20260611-130000-dddd", run_type="sweep")
    (gone_dir / "engine.pid").write_text(str(_dead_pid()))

    infos = runs.discover_runs(tmp_path)
    assert [i.status for i in infos] == [
        runs.FINISHED,
        runs.PAUSED,
        runs.RUNNING,
        runs.INTERRUPTED,
    ]
    assert infos[0].started_at == "2026-06-11T10:00:00"
    assert [i.run_type for i in infos] == ["story", "story", "story", "sweep"]
    # statuses re-classify on a second (cached-header) pass
    assert [i.status for i in runs.discover_runs(tmp_path)] == [i.status for i in infos]


def test_live_pid_with_unreadable_identity_is_unknown_not_interrupted(tmp_path, monkeypatch):

    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0")

    class Host:
        def liveness_of(self, pid, identity):
            return "unknown"

    # liveness reads the host seam through probe_liveness; patch get_process_host
    # in this module to exercise the full delegation path.
    monkeypatch.setattr(runs, "get_process_host", lambda: Host())
    assert runs.liveness(run_dir) == "unknown"
    assert runs.discover_runs(tmp_path)[0].status == runs.UNKNOWN


def test_process_host_misconfig_degrades_to_unknown(tmp_path, monkeypatch):
    # A ProcessHostError from get_process_host (bad BMAD_LOOP_PROCESS_HOST) must not
    # escape the display layer: the dashboard poll worker has no except and would
    # take the whole app down. The status column degrades to 'unknown' instead.
    from bmad_loop.process_host import ProcessHostError

    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0")

    def boom():
        raise ProcessHostError("BMAD_LOOP_PROCESS_HOST matches no registered host")

    monkeypatch.setattr(runs, "get_process_host", boom)
    assert runs.liveness(run_dir) == "unknown"
    assert runs.discover_runs(tmp_path)[0].status == runs.UNKNOWN


def test_finished_beats_stopped(tmp_path):
    _make_state_run(tmp_path, "20260611-100000-aaaa", finished=True, stopped=True)
    assert runs.discover_runs(tmp_path)[0].status == runs.FINISHED


def test_discover_runs_marks_graceful_stop_pending_while_running(tmp_path):
    from bmad_loop.runs import STOP_REQUEST_FILE

    run_dir = _make_state_run(tmp_path, "20260611-120000-cccc")
    runs.write_pid(run_dir)  # test process pid: alive -> RUNNING
    assert runs.discover_runs(tmp_path)[0].stopping is False  # no request yet
    (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    info = runs.discover_runs(tmp_path)[0]
    assert info.status == runs.RUNNING
    assert info.stopping is True


def test_discover_runs_marks_graceful_stop_pending_while_unknown(tmp_path, monkeypatch):
    # An unverifiable ('unknown') pid still has an engine that can consume the control
    # file, so the "stopping" badge shows for an UNKNOWN-status run too — matching the
    # CLI's graceful_stop_pending, which projects the request on liveness != "dead".
    from bmad_loop.runs import STOP_REQUEST_FILE

    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0")

    class Host:
        def liveness_of(self, pid, identity):
            return "unknown"

    monkeypatch.setattr(runs, "get_process_host", lambda: Host())
    assert runs.discover_runs(tmp_path)[0].stopping is False  # no request yet
    (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    info = runs.discover_runs(tmp_path)[0]
    assert info.status == runs.UNKNOWN
    assert info.stopping is True


def test_stopping_ignored_on_a_non_running_run(tmp_path):
    # The engine consumes the control file at the stop boundary; a file lingering
    # on an already-stopped or finished run must not read as still-stopping.
    from bmad_loop.runs import STOP_REQUEST_FILE

    stopped = _make_state_run(tmp_path, "20260611-100000-aaaa", stopped=True)
    (stopped / "engine.pid").write_text(str(_dead_pid()))
    (stopped / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    finished = _make_state_run(tmp_path, "20260611-110000-bbbb", finished=True)
    (finished / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    infos = {i.run_id: i for i in runs.discover_runs(tmp_path)}
    assert infos["20260611-100000-aaaa"].status == runs.STOPPED
    assert infos["20260611-100000-aaaa"].stopping is False
    assert infos["20260611-110000-bbbb"].status == runs.FINISHED
    assert infos["20260611-110000-bbbb"].stopping is False


def test_discover_runs_legacy_no_pid_is_unknown(tmp_path, monkeypatch):
    _make_state_run(tmp_path, "20260611-100000-aaaa")
    # legacy liveness now flows through the multiplexer backend; patch its seam.
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _: None)
    assert runs.discover_runs(tmp_path)[0].status == runs.UNKNOWN


@pytest.mark.usefixtures("force_tmux_backend")  # asserts tmux liveness through the seam
def test_legacy_run_with_live_tmux_session_is_running(tmp_path, monkeypatch):
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _: "/usr/bin/tmux")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Proc:
            returncode = 0

        return Proc()

    monkeypatch.setattr(tmux_base.subprocess, "run", fake_run)
    assert runs.discover_runs(tmp_path)[0].status == runs.RUNNING
    assert calls[0][:3] == ["tmux", "has-session", "-t"]
    assert calls[0][3] == f"=bmad-loop-{run_dir.name}"


def test_legacy_run_liveness_unknown_when_backend_query_fails(tmp_path, monkeypatch):
    """A timed-out / failing has-session surfaces as a MultiplexerError at the seam,
    not a raw subprocess error: a dead query proves nothing about a legacy run, so it
    degrades to 'unknown' instead of escaping discover_runs() and crashing the TUI."""
    _make_state_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _: "/usr/bin/tmux")

    def boom(argv, **kwargs):
        raise tmux_base.subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    assert runs.discover_runs(tmp_path)[0].status == runs.UNKNOWN


def test_discover_runs_corrupt_state_is_unknown_not_crash(tmp_path):
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "state.json").write_text("{ not json")
    infos = runs.discover_runs(tmp_path)
    assert [i.status for i in infos] == [runs.UNKNOWN]
    assert infos[0].run_id == "20260611-100000-aaaa"


def test_discover_runs_reports_pause_stage(tmp_path):
    from bmad_loop.model import PAUSE_PLAN_CHECKPOINT

    _make_state_run(
        tmp_path,
        "20260101-000000-aaaa",
        paused_reason="plan checkpoint for 1",
        paused_stage=PAUSE_PLAN_CHECKPOINT,
    )
    info = runs.discover_runs(tmp_path)[0]
    assert info.status == runs.PAUSED
    assert info.paused_stage == PAUSE_PLAN_CHECKPOINT


def test_discover_runs_pause_stage_blank_when_not_paused(tmp_path):
    # a finished run keeps its last paused_stage in state; it must not badge.
    _make_state_run(tmp_path, "20260101-000000-aaaa", finished=True, paused_stage="plan-checkpoint")
    info = runs.discover_runs(tmp_path)[0]
    assert info.status == runs.FINISHED
    assert info.paused_stage == ""


def test_stat_sig_includes_inode_for_same_size_rewrite(tmp_path):
    # The engine rewrites state.json atomically (temp + os.replace), landing a
    # fresh inode. A same-size rewrite with an identical (forced) mtime must still
    # change the signature — otherwise a coarse-mtime filesystem (WSL2 drvfs) would
    # serve a stale parse from cache. st_ino is what catches it.
    target = tmp_path / "state.json"
    target.write_text("AAAA", encoding="utf-8")
    before = runs._stat_sig(target)
    original = target.stat()

    replacement = tmp_path / "state.json.tmp"
    replacement.write_text("BBBB", encoding="utf-8")  # same size, different content
    os.replace(replacement, target)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))  # pin mtime

    after = runs._stat_sig(target)
    same_size = before[1] == after[1]
    same_mtime = before[0] == after[0]
    assert same_size and same_mtime  # (mtime_ns, size) alone could not tell these apart
    assert before != after  # ...but the inode did


def test_stopped_run_classifies_as_stopped_not_interrupted(tmp_path):
    # a deliberate stop leaves a dead pid; it must read STOPPED, not INTERRUPTED
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa", stopped=True)
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.discover_runs(tmp_path)[0].status == runs.STOPPED


def test_classify_crashed(tmp_path):
    # a recorded crash classifies as CRASHED (distinct from a generic INTERRUPTED),
    # checked before liveness so the dead pid does not override it.
    assert (
        runs._classify(
            finished=False,
            paused=False,
            stopped=False,
            crashed=True,
            run_dir=tmp_path,
        )
        == runs.CRASHED
    )
    # a state.json carrying crashed=True surfaces through discover_runs
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa", crashed=True)
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.discover_runs(tmp_path)[0].status == runs.CRASHED


def test_classify_legacy_crash_stays_interrupted(tmp_path):
    # a pre-feature run has no crashed flag; a dead pid reads as INTERRUPTED, not
    # CRASHED — backward compatible.
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    doc = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    doc.pop("crashed", None)
    (run_dir / "state.json").write_text(json.dumps(doc), encoding="utf-8")
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.discover_runs(tmp_path)[0].status == runs.INTERRUPTED
