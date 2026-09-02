"""Run-directory helper tests."""

import contextlib
import errno
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import assert_run_state_lock_held, escalated_run, git, refuse_to_resolve

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


def test_all_run_dirs_includes_state_json_less_dirs(tmp_path):
    """The ungated counterpart sees the run `list_run_dirs` filters out — a run
    whose state.json is gone still owns its engine.pid, and a liveness guard
    built on the gated view would archive out from under it (#711 review)."""
    _make_run(tmp_path, "20260611-120000-bbbb")
    _make_run(tmp_path, "20260610-090000-aaaa")
    _make_run(tmp_path, "20260612-080000-cccc", with_state=False)
    listed = runs.all_run_dirs(tmp_path)
    assert listed is not None
    assert [d.name for d in listed] == [
        "20260610-090000-aaaa",
        "20260611-120000-bbbb",
        "20260612-080000-cccc",
    ]
    assert all(d.parent == tmp_path / runs.RUNS_DIR for d in listed)


def test_all_run_dirs_distinguishes_missing_from_unreadable(tmp_path):
    """A missing runs root is a real answer (no runs); an unreadable one is no
    answer at all. Callers that refuse on "cannot tell" need them apart, so the
    empty list and `None` must not collapse into each other."""
    assert runs.all_run_dirs(tmp_path) == []
    (tmp_path / runs.RUNS_DIR).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / runs.RUNS_DIR).write_text("not a directory", encoding="utf-8")
    assert runs.all_run_dirs(tmp_path) is None


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


@pytest.mark.parametrize("ref", ["", ".", "...", ".. ", " .."], ids=repr)
def test_resolve_run_dir_refuses_the_root_naming_refs(tmp_path, ref):
    """#480: `_is_path_escape` was the one member of the guard family omitting
    `names_tree_root`, so a ref naming the runs *root* still reached the exact
    branch. `""` and `"."` both join to that root exactly — measured here, both
    `runs / ""` and `runs / "."` *are* the runs dir — so a state.json lying there
    made `bmad-loop delete ""` hand the whole runs tree to `shutil.rmtree`. The
    trailing dot/space spellings are the Win32 half of the same rule, cited rather
    than measurable on POSIX: the trim of trailing periods and spaces leaves `..`
    or nothing, so they name `.bmad-loop/` or the runs dir there.

    The planted state.json is the load-bearing part of the fixture. Without it the
    exact branch is inert for every row and the test would pass for the wrong
    reason. Ablation: drop `names_tree_root` from `_is_path_escape` and the `"."`
    row reddens alone — the other three have no POSIX reach to lose, and `""` is
    refused upstream by `resolve_run_dir`'s empty-ref gate (whose own single-run
    grading lives in the test below)."""
    project = tmp_path / "proj"
    _make_run(project, "20260620-143025-a1b2")
    _make_run(project, "20260619-101010-a1c9")
    runs_root = project / ".bmad-loop" / "runs"
    (runs_root / "state.json").write_text("{}")  # exactly where "" and "." land

    with pytest.raises(runs.RunRefError):
        runs.resolve_run_dir(project, ref)
    assert (runs_root / "state.json").is_file()  # never consumed as a run


def test_resolve_run_dir_refuses_an_empty_ref_even_with_a_single_run(tmp_path):
    """Round-1 review: `""` is a prefix and a suffix of every name, so partial
    matching reads it as a wildcard — the two-run fixture above lands it in the
    ambiguity arm, but with exactly ONE run it resolved that run, handing
    `bmad-loop delete ""` a run the operator never named. The refusal sits above
    partial matching so it cannot depend on how many runs exist, and it costs no
    addressability: no directory can be named `""`, unlike the other escape
    spellings, which keep the partial fallback so a legacy dir named `"..."`
    stays matchable. Ablation: drop the `if not ref` gate from `resolve_run_dir`
    and this test reddens alone (the ref resolves) while the two-run test above
    stays green on its ambiguity arm."""
    project = tmp_path / "proj"
    run = _make_run(project, "20260620-143025-a1b2")
    with pytest.raises(runs.RunRefError, match="empty run ref"):
        runs.resolve_run_dir(project, "")
    assert run.is_dir()


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


def test_stop_run_refuses_when_the_lodge_failed_and_the_signal_was_refused(tmp_path, monkeypatch):
    """Neither channel delivered: nothing written, nothing signalled, nothing proved.

    The refusal above only fires from inside the `pid is not None` arm. A `terminate`
    we were *refused* clears `pid` and skips that arm entirely, so this combination
    used to reach the fallback and report success — writing `stopped=True` and
    stamping `fallback=True` over a run whose engine may still be mutating the
    project, with no request on disk for it to honor.

    Not a regression: on the merge-base every refused signal ended here, because
    `stop_run` cleared the request as its first statement. What does not survive is
    the *justification* for reporting success — that the request stays lodged, so the
    stop is still in flight. When the lodge failed, nothing is in flight.

    Ablation: delete the `engine_may_live and not lodged` branch -> this reddens on
    the `pytest.raises` itself ("DID NOT RAISE"), not on the asserts below it, which
    are never reached; `stop_run` falls through to the fallback and returns True. The
    twin below is the position axis, and stays green under this one."""
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 123.0")

    def _enospc(_run_dir, _mode):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "_write_stop_request", _enospc)
    host = _FakeHost(alive=True, identity=123.0, on_terminate=_raise(PermissionError()))
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    with pytest.raises(runs.StopRunError, match="no stop is pending"):
        runs.stop_run(run_dir)
    assert killed == ["r1"]  # the session backstop still runs, ahead of the refusal
    assert load_state(run_dir).stopped is False  # never claimed a stop it did not make
    journal = (run_dir / "journal.jsonl").read_text()
    assert "run-stop-undelivered" in journal  # the attempt is on the record
    assert '"fallback": true' not in journal  # and not as a completed stop


def test_stop_run_still_trusts_an_engine_written_stop_when_the_lodge_failed(tmp_path, monkeypatch):
    """The placement ablation for the refusal above: it must sit *after* the
    `state.stopped` return, not before it.

    Same failed lodge and same refused signal, but the engine already honored an
    earlier stop and recorded it. `stop` is then reporting a stop that genuinely
    happened, so it must return True — raising here would turn a settled run into a
    CLI failure on the operator's second `stop`.

    Ablation: move the refusal ahead of the `if state.stopped:` branch -> this test
    raises while the one above still passes. Both are needed: the pair pins the
    branch's presence *and* its position."""
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")
    st = load_state(run_dir)
    st.stopped = True  # an earlier stop the engine honored and recorded itself
    save_state(run_dir, st)
    (run_dir / "engine.pid").write_text("4242 123.0")

    def _enospc(_run_dir, _mode):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "_write_stop_request", _enospc)
    host = _FakeHost(alive=True, identity=123.0, on_terminate=_raise(PermissionError()))
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    assert runs.stop_run(run_dir) is True
    assert killed == ["r1"]


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


def test_stop_run_takes_state_lock_only_after_signal_and_wait(tmp_path, monkeypatch):
    """Ablation: move stop_run's state_lock above terminate and this reddens on order;
    the engine would be unable to save its own stopped state while stop waits."""
    order: list[str] = []
    alive = True
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    def on_terminate(_pid):
        nonlocal alive
        order.append("terminate")
        alive = False

    host = _FakeHost(alive=lambda: alive, identity=100.0, on_terminate=on_terminate)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    monkeypatch.setattr(runs, "kill_session", lambda _rid: order.append("kill-session"))

    @contextlib.contextmanager
    def recording_state_lock(_run_dir):
        order.append("state-lock")
        yield

    monkeypatch.setattr(runs, "state_lock", recording_state_lock)

    assert runs.stop_run(run_dir) is True
    assert order == ["terminate", "kill-session", "state-lock"]


def test_stop_run_fallback_save_retains_the_outer_state_lock(tmp_path, monkeypatch):
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    real_save = runs.save_state

    def checked_save(target, state):
        assert_run_state_lock_held(target)
        real_save(target, state)

    monkeypatch.setattr(runs, "save_state", checked_save)

    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True


def test_stop_run_does_not_fallback_over_engine_completion(tmp_path, monkeypatch):
    """The final locked snapshot wins when the engine finishes while stop delivers."""
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0", encoding="utf-8")
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)

    def finish(_pid):
        state = load_state(run_dir)
        state.finished = True
        save_state(run_dir, state)

    monkeypatch.setattr(
        runs,
        "get_process_host",
        lambda: _FakeHost(alive=False, identity=100.0, on_terminate=finish),
    )

    assert runs.stop_run(run_dir) is False
    persisted = load_state(run_dir)
    assert persisted.finished is True
    assert persisted.stopped is False
    journal = run_dir / "journal.jsonl"
    assert not journal.exists() or '"fallback": true' not in journal.read_text()


def test_stop_run_retries_when_resume_publishes_a_new_engine(tmp_path, monkeypatch):
    """A rival resume that wins during delivery is itself sent the hard stop."""
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0", encoding="utf-8")
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    alive = {4242: True, 5252: True}
    identities = {4242: 100.0, 5252: 200.0}

    class GenerationalHost(_FakeHost):
        def __init__(self):
            super().__init__(alive=False)

        def is_alive(self, pid):
            return alive.get(pid, False)

        def identity(self, pid):
            return identities.get(pid)

        def terminate(self, pid):
            self.terminated.append(pid)
            alive[pid] = False
            if pid == 4242:
                # Resume clears the old gesture, then publishes its new pid and
                # state atomically under the lock stop will next acquire.
                with runs.state_lock(run_dir):
                    runs.clear_graceful_stop(run_dir)
                    rival = load_state(run_dir)
                    rival.crashed = True
                    (run_dir / "engine.pid").write_text("5252 200.0", encoding="utf-8")
                    save_state(run_dir, rival)

    host = GenerationalHost()
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    assert runs.stop_run(run_dir) is True
    assert host.terminated == [4242, 5252]
    persisted = load_state(run_dir)
    assert persisted.stopped is True
    assert persisted.crashed is True
    assert runs.read_stop_request_mode(run_dir) is None


def test_stop_run_does_not_loop_on_an_engine_whose_identity_cannot_be_read(tmp_path, monkeypatch):
    """A recorded pid that exists but whose identity cannot be read (win32
    ERROR_ACCESS_DENIED is the measured shape) is `"unknown"`, not `"dead"`, and
    `alive_and_ours` declines it — so the local pid is cleared and nothing is
    signalled. The generation compare under the lock must then read the pid file
    AS RECORDED: compared against the cleared local pid, the unchanged file looked
    like a rival that had published a new engine, `_stop_run_once` answered "retry",
    and `stop_run` — an unbounded loop by design, so a real rival is always sent the
    stop — never returned.

    `_stop_run_once` is wrapped to turn that livelock into a failure, or the
    ablation would hang the suite rather than redden it.

    Ablation: compare `current_engine` against the post-clearing `(pid, identity)`
    again and this reddens on the call count."""
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0", encoding="utf-8")
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    host = _FakeHost(alive=True, identity=None)  # exists, identity unreadable
    assert host.liveness_of(4242, 100.0) == "unknown"  # MEASURED: the arm under test
    monkeypatch.setattr(runs, "get_process_host", lambda: host)
    real_once = runs._stop_run_once
    calls: list[int] = []

    def bounded_once(run_dir_):
        calls.append(1)
        if len(calls) > 3:
            raise AssertionError("stop_run is retrying an unchanged engine forever")
        return real_once(run_dir_)

    monkeypatch.setattr(runs, "_stop_run_once", bounded_once)

    assert runs.stop_run(run_dir) is True

    assert len(calls) == 1
    assert host.terminated == []  # never signals a pid it cannot prove is ours
    assert load_state(run_dir).stopped is True


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


def test_stop_run_does_not_stamp_fallback_on_an_already_stopped_run(tmp_path, monkeypatch):
    """`fallback=True` says this tool completed the stop from outside. An engine that
    honored a stop and exited must never collect that stamp on a later `stop`.

    The check that trusts an engine-written `stopped` used to sit *inside* the
    `pid is not None` arm, so every path that clears the pid early skipped it and
    fell straight through to the append: a pid that is no longer ours, a `terminate`
    that raced the exit into `ProcessLookupError`, or a refusal that could not verify
    it. This case needs no race at all — `stopped` is set and `finished` is not, so
    the guard at the top of `stop_run` does not fire — which is why the check now
    sits outside that arm, where every path reaches it.

    The session backstop must still run: an engine that honored the stop and died
    before tearing its window down leaks the session exactly like one we killed.

    Ablation: delete the hoisted `if state.stopped:` branch -> a second `run-stop`
    carrying `"fallback": true` is appended and the last assert fails."""
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")
    st = load_state(run_dir)
    st.stopped = True  # an earlier stop the engine honored and recorded itself
    save_state(run_dir, st)
    (run_dir / "engine.pid").write_text("4242 123.0")

    host = _FakeHost(alive=False)  # the engine is gone
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    assert runs.stop_run(run_dir) is True
    assert host.terminated == [] and host.force_killed == []  # nothing left to signal
    assert killed == ["r1"]  # the session backstop still runs
    journal = run_dir / "journal.jsonl"
    assert not journal.exists() or '"fallback": true' not in journal.read_text()


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

    Patched at `os.replace` rather than on the two `atomic_replace` namespaces:
    the confined writer's anchored arm publishes with a bare dir_fd-relative
    `os.replace` and never reaches `atomic_replace` (#593), so patching that name
    would fire on nothing and the test would pass having interleaved nobody. One
    seam still covers the ablation the old dual patch existed for, and covers it
    better — `atomic_replace` is itself a wrapper around `os.replace`, so
    reverting `_write_stop_request` to the hand-rolled `tmp + atomic_replace`
    routes through this same patch and must still redden this test.

    Filtered to the stop-request name so an unrelated replace during the test is
    not collateral."""
    run_dir = _make_state_run(tmp_path, "r1")
    real_replace = os.replace
    nested: list[str] = []

    def _interleave(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if not nested and str(dst).endswith(runs.STOP_REQUEST_FILE):
            nested.append("b")  # inside writer A's replace, run writer B end to end
            runs._write_stop_request(run_dir, "graceful")
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", _interleave)

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


def test_consume_stop_request_never_removes_a_request_it_did_not_read(tmp_path, monkeypatch):
    """The reader-side half of the arbitration. A `stop` escalating to hard while the
    engine is consuming must not be deleted unread: a read followed by an unlink
    removes whatever answers to the name *now*, which may not be the request whose
    mode the caller is about to route on.

    Only this direction can lose anything — the mode lattice is monotone (graceful
    refuses to overwrite, hard only ever writes hard), so a stale "hard" read is
    still true while a stale "graceful" may not be.

    ⚠️ Re-reading the mode just before the unlink does NOT fix this and measurably
    worsens it (164 -> 929 swallowed over 4000 injected races): the extra read
    widens the interval the escalation has to land in. Narrowing is not closing.

    Ablation, two axes, and axis 2 is what proves this is not the shape-2 guard
    wearing a new hat:
      1. Revert `consume_stop_request` to `read_stop_request_mode` + a
         `clear_graceful_stop` unlink -> the FIRST assert fails, returning "hard":
         with no take, the mode answered is whatever the name resolves to at read
         time, which the escalation has already changed, and the unlink then removes
         that one too. Read and consume disagree about which request was handled,
         which is the whole defect; the channel is left empty, so the second assert
         would fail as well were it reached.
      2. Revert `_create_stop_request` to an unconditional
         `_write_stop_request(run_dir, "graceful")`, undoing the writer-side fix,
         but keep the atomic take -> this test still PASSES. The two axes redden
         disjoint sets, which is the proof the two guards are independent."""
    runs._create_stop_request(tmp_path)  # operator: stop --graceful
    real = runs._stop_request_mode_of
    escalated: list[str] = []

    def _escalate_then_read(path):
        if not escalated:  # once — the take happens before this, which is the point
            escalated.append("hard")
            runs._write_stop_request(tmp_path, "hard")  # concurrent `bmad-loop stop`
        return real(path)

    monkeypatch.setattr(runs, "_stop_request_mode_of", _escalate_then_read)

    assert runs.consume_stop_request(tmp_path) == "graceful"  # the body we took
    assert escalated == ["hard"]  # the interleave really happened
    assert runs.read_stop_request_mode(tmp_path) == "hard"  # NOT swallowed


def test_create_stop_request_failed_write_never_deletes_a_concurrent_hard_request(
    tmp_path, monkeypatch
):
    """The writer's *rollback* path is the third way a hard request could be lost,
    and it is the one the monotone-lattice argument did not cover: that argument
    enumerates the readers and the writers' success paths, and concludes only a
    reader acting on a stale "graceful" can lose anything.

    `_create_stop_request` creates the file with `O_EXCL` and then writes the body
    into it, so a failed write once rolled back with `path.unlink()`. `unlink`
    resolves the *name*, not the inode this call created — so a `stop` escalating to
    `mode: "hard"` onto that name while the write was in flight was deleted by the
    cleanup of a graceful lodge that never completed. That is a `hard -> absent`
    drop, below both rungs of the lattice, and on native Windows it withdraws the
    only channel that can stop the engine while `stop_run` still reports the request
    lodged.

    Closed by subtraction: there is no rollback. A short or empty body reads as
    "graceful", which is the mode this call was asked to lodge anyway.

    ⚠️ Guarding the unlink instead does NOT fix this and measurably worsens it, the
    same trap `consume_stop_request` documents: the check moves the decision earlier
    and the destructive act later by its own cost, shifting the window rather than
    narrowing it (inode compare 1.39x, mode compare 2.30x worse over a
    rendezvous-synchronised escalation sweep). There is also no atomic
    "unlink only if still my inode" to reach for, and `st_ino` is 0 on several
    Windows filesystems, which would make the compare a false *equal*.

    Ablation: restore the `except BaseException: path.unlink(); raise` cleanup ->
    the last assert fails with the mode `None`, because the escalation this test
    injects is exactly what that unlink removes."""
    escalated: list[str] = []
    real_fdopen = os.fdopen

    def _escalate_then_fail(fd, *a, **kw):
        if escalated:  # nested use by the hard writer's own staged write
            return real_fdopen(fd, *a, **kw)
        escalated.append("hard")
        os.close(fd)  # the graceful file exists and is empty, as O_EXCL left it
        runs._write_stop_request(tmp_path, "hard")  # concurrent `bmad-loop stop`
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fdopen", _escalate_then_fail)

    with pytest.raises(OSError):
        runs._create_stop_request(tmp_path)
    assert escalated == ["hard"]  # the interleave really happened
    assert runs.read_stop_request_mode(tmp_path) == "hard"  # NOT swallowed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_create_stop_request_refuses_a_redirected_runs_dir(tmp_path):
    """#593's parent half at the graceful lodge. `O_EXCL` never dereferences the
    FINAL name, but `.bmad-loop/`, `runs/` and the run's own directory were still
    resolved by name, so a link planted at any of them aimed the request outside
    the project — the hole the confined `_write_stop_request` next door had
    already closed. The lodge now walks the parents through
    `create_exclusive_confined`, whose own rows in test_platform_util grade the
    walk; what this row grades is the adoption, at the site.

    Ablation: revert `_create_stop_request` to the bare `os.open` and this
    reddens twice over — no raise, and the request file lands in `outside/`."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    run_id = "20260611-100000-aaaa"
    (project / ".bmad-loop").mkdir(parents=True)
    (outside / run_id).mkdir(parents=True)
    (project / ".bmad-loop" / "runs").symlink_to(outside, target_is_directory=True)
    run_dir = project / ".bmad-loop" / "runs" / run_id

    with pytest.raises(platform_util.UnconfinedWriteError, match="without a redirect"):
        runs._create_stop_request(run_dir)

    assert list((outside / run_id).iterdir()) == []  # nothing landed outside


def test_create_stop_request_failed_write_leaves_a_graceful_request_standing(tmp_path, monkeypatch):
    """The other half of removing the rollback, stated as its own behavior rather
    than left implicit: a write that fails part-way leaves the request pending.

    That is the bounded direction. The one production caller is `stop --graceful`,
    which an operator drove, so the standing request is the one they asked for; a
    short body reads as "graceful"; a later hard stop supersedes it unconditionally
    and a later graceful ask answers "already-pending", so the channel is never
    wedged; and `--cancel-graceful` or `resume` withdraws it. The CLI says so
    instead of reporting a clean failure.

    Ablation: restore the cleanup -> the file is gone and the mode is `None`."""
    real_fdopen = os.fdopen

    def _fail(fd, *a, **kw):
        os.close(fd)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fdopen", _fail)
    with pytest.raises(OSError):
        runs._create_stop_request(tmp_path)
    monkeypatch.setattr(os, "fdopen", real_fdopen)

    assert (tmp_path / runs.STOP_REQUEST_FILE).is_file()
    assert runs.read_stop_request_mode(tmp_path) == "graceful"


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


# ------------------------------------------------- lock_path_for (#286, #469)
#
# The advisory-lock sidecar for a mutable data file. Two claims are load-bearing
# and graded below: WHERE it lives (under the state root, never beside the data
# file, because the ledger is tracked and the engine stages with `git add -A`)
# and WHAT it is keyed on (the resolved path, so every spelling of one file
# contends on one lock).


def test_lock_path_for_keys_on_the_resolved_path(tmp_path):
    """Two spellings of one ledger get one lock; two ledgers get two.

    A lock keyed on the spelling excludes nobody: the run reaching the ledger by
    its `.bmad-loop` relative path and the sweep reaching it through an absolute
    or dot-dot spelling would take different sidecars and interleave exactly as
    they do today, with the fix installed and inert.

    Also grades the placement, which is the deliberate deviation from #286's own
    proposal of a `deferred-work.md.lock` sibling: the ledger is tracked by
    design and `verify.commit_story`/`finalize_commit` stage with `git add -A`,
    so a sibling would ride into the engine's own commits.

    Ablation: digest `data_path` instead of `data_path.resolve()` and the
    dot-dot row fails — one file, two locks."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    ledger = artifacts / "deferred-work.md"
    ledger.write_text("# Deferred Work\n", encoding="utf-8")

    direct = runs.lock_path_for(ledger)
    # A dot-dot spelling: `str()` keeps it verbatim, so only resolution folds it
    dotted = runs.lock_path_for(artifacts / ".." / "artifacts" / "deferred-work.md")

    assert direct == dotted  # one file, one lock
    assert direct.parent == runs.state_root() / "locks"  # never beside the ledger
    assert tmp_path not in direct.parents  # ...and never inside the project
    assert direct.name.endswith("-deferred-work.md.lock")  # basename, for humans

    sibling = artifacts / "deferred-work-archive.md"
    sibling.write_text("", encoding="utf-8")
    assert runs.lock_path_for(sibling) != direct  # distinct files, distinct locks


def test_lock_path_for_is_pure_and_creates_nothing(tmp_path):
    """No mkdir here: `file_lock` mkdirs the lock's parent when it opens it.

    Worth pinning rather than assuming — a helper that provisions the state root
    as a side effect of being *asked a question* turns every read-only caller
    into a writer, and `lock_path_for` is called from `--json` read models."""
    ledger = tmp_path / "deferred-work.md"

    lock = runs.lock_path_for(ledger)

    assert not lock.exists()
    assert not lock.parent.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_lock_path_for_follows_a_symlinked_ledger_to_one_lock(tmp_path):
    """The symlink half of the spelling problem — the one no lexical comparison
    catches, since the two paths share no component. A project pointed at a
    shared external artifact dir through a link contends with the direct
    spelling, which is where the real contention is."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    ledger = artifacts / "deferred-work.md"
    ledger.write_text("# Deferred Work\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(artifacts, target_is_directory=True)

    assert runs.lock_path_for(link / "deferred-work.md") == runs.lock_path_for(ledger)


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
    """A writer that replaces the NAME, and the reason is that this record lives
    under a root whose path the driven session is handed — the engine exports the
    sibling events dir as `BMAD_LOOP_EVENTS_DIR`. Following a link planted at the
    digest's name would aim an orchestrator write at a path of the session's
    choosing. The site no longer spells that choice as `follow_symlinks=False`:
    since #593 it calls `atomic_write_text_confined`, which is no-follow by
    construction.

    ABLATION: swap the writer for `atomic_write_text(path, ...)` at its
    follow-the-link default and the target below is what gets written."""
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_write_trusted_config_digest_refuses_a_symlinked_state_dir_component(tmp_path, monkeypatch):
    """The escape #593 names, at this site. The row above grades
    `follow_symlinks=False`, which stopped at the FINAL component only: the two
    components BELOW the state root — `<project tag>/` and `<run id>/` — were still
    resolved by name at both the `mkstemp` and the `os.replace`, and the
    `mkdir(parents=True, exist_ok=True)` on the line before the write ACCEPTS a
    symlinked directory, so a link planted at either survived the setup step and
    aimed the stamp wherever it pointed.

    The confinement root is the STATE ROOT, not the digest's parent: the walk
    covers the components strictly below the root, so rooting this at
    `path.parent` would check neither of the two components a session could reach
    and this row would stay green with the escape open. It is also not the
    project — this record deliberately lives OUT of the tree the baseline exists
    to police (`test_config_digest_is_stamped_under_the_state_root_not_in_the_project`).

    The mkdir runs first and walks THROUGH the planted link, so an empty `r1/`
    legitimately appears outside; what must not appear is content. That is what the
    last assertion says.

    Ablation: revert the call to
    `atomic_write_text(path, digest + chr(10), follow_symlinks=False)` and this
    fails `DID NOT RAISE`, with the digest published out in `outside/`."""
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv(envvars.STATE_DIR, str(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / runs.project_tag(project)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        runs.write_trusted_config_digest(project, "r1", "abc123")

    # the mkdir walked through the link, so the run dir is out here; nothing else
    # may be, and in particular no digest and no staging temp
    assert [p.name for p in outside.iterdir()] == ["r1"]
    assert list((outside / "r1").iterdir()) == []


def test_write_trusted_config_digest_lands_under_a_clean_state_root(tmp_path, monkeypatch):
    """The positive control for the refusal above: with no link planted, the
    anchored walk opens both components and the stamp lands where the reader looks.

    Wrapped rather than stubbed, so the real write still happens, and the CONFINED
    binding is the one wrapped — `runs.atomic_write_text` no longer exists here, so
    a stale patch of that name would fail loudly rather than record nothing.

    `seen` records the ROOT, not merely that a write happened: `confine_root` is
    the one component the anchored walk starts from rather than checks, so a root
    naming the digest's own parent would be lexically confined and behaviourally
    inert. Both this row and the refusal above redden under that ablation, from
    opposite directions — this one on the recorded value, that one on the escape
    it lets through.

    Ablation: point `confine_root` at `path.parent` and this fails on `seen`."""
    project = tmp_path / "proj"
    project.mkdir()
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv(envvars.STATE_DIR, str(root))
    seen: list[Path] = []
    real = runs.atomic_write_text_confined

    def record(path, text, *, confine_root, require_writable_target=False):
        seen.append(Path(confine_root))
        real(
            path,
            text,
            confine_root=confine_root,
            require_writable_target=require_writable_target,
        )

    monkeypatch.setattr(runs, "atomic_write_text_confined", record)

    runs.write_trusted_config_digest(project, "r1", "abc123")

    assert seen == [root]  # the state root itself, not the run dir
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


def test_delete_run_refuses_a_live_engine_inside_the_lifecycle_hold(tmp_path, monkeypatch):
    """Resume-first ordering: the decisive probe runs after lock acquisition and
    leaves both the run and its control plane untouched.

    Ablation: remove the in-lock ``engine_liveness`` gate and the run disappears;
    move it above ``state_lock`` and the lock assertion fails. Verified.
    """
    run_dir = _make_state_run(tmp_path, "r1")
    state_dir = _seed_state_dir(tmp_path, "r1")

    def live(target):
        assert_run_state_lock_held(target)
        return "alive"

    monkeypatch.setattr(runs, "engine_liveness", live)
    monkeypatch.setattr(
        runs,
        "_refuse_live_session",
        lambda *_args: pytest.fail("session guard ran before authoritative engine probe"),
    )
    with pytest.raises(runs.LiveEngineError, match="refusing to delete"):
        runs.delete_run(tmp_path, run_dir)

    assert run_dir.is_dir()
    assert state_dir.is_dir()


def test_delete_run_holds_lifecycle_lock_through_removal_and_state_discard(tmp_path, monkeypatch):
    """The authoritative probe, directory removal, and control-plane tail are one
    uninterrupted transaction. Moving either mutation outside the hold reddens.
    """
    run_dir = _make_state_run(tmp_path, "r1")
    removed: list[str] = []
    real_rmtree = runs.shutil.rmtree

    def dead(target):
        assert_run_state_lock_held(target)
        return "dead"

    def checked_rmtree(target, *args, **kwargs):
        assert_run_state_lock_held(run_dir)
        removed.append("run")
        return real_rmtree(target, *args, **kwargs)

    def checked_discard(_project, _run_id):
        assert_run_state_lock_held(run_dir)
        removed.append("state")

    monkeypatch.setattr(runs, "engine_liveness", dead)
    monkeypatch.setattr(runs.shutil, "rmtree", checked_rmtree)
    monkeypatch.setattr(runs, "_discard_state_dir", checked_discard)

    runs.delete_run(tmp_path, run_dir)

    assert removed == ["run", "state"]


def test_failed_composition_pid_bypasses_only_its_own_live_engine(tmp_path, monkeypatch):
    """The narrow composer token keeps the independent session guard active."""
    run_dir = _make_state_run(tmp_path, "r1")
    composer_claim = run_dir.stat(follow_symlinks=False)
    runs.write_pid(run_dir)
    checked: list[str] = []

    def session_guard(_project, run_id, _action):
        checked.append(run_id)

    monkeypatch.setattr(runs, "_refuse_live_session", session_guard)
    runs.delete_run(
        tmp_path,
        run_dir,
        _expected_composer_pid=os.getpid(),
        _expected_composer_claim=composer_claim,
    )

    assert checked == ["r1"]
    assert not run_dir.exists()


@pytest.mark.parametrize("operation", [runs.delete_run, runs.archive_run])
def test_lifecycle_containment_is_checked_before_lock_acquisition(tmp_path, monkeypatch, operation):
    """A hostile path is refused without deriving or taking any state lock."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}", encoding="utf-8")

    def unexpected_lock(_run_dir):
        pytest.fail("containment must run before state-lock acquisition")

    monkeypatch.setattr(runs, "state_lock", unexpected_lock)
    with pytest.raises(platform_util.UnconfinedWriteError):
        operation(project, outside)

    assert outside.is_dir()


def test_delete_run_refuses_before_removal_when_state_lock_cannot_be_named(tmp_path, monkeypatch):
    """The lifecycle lock is mandatory: without a state root cleanup cannot
    rendezvous with resume, so failure leaves the run intact and surfaces."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "state_root", _raising(runs.StateRootError("no root")))

    with pytest.raises(runs.StateRootError, match="no root"):
        runs.delete_run(tmp_path, run_dir)

    assert run_dir.is_dir()


def test_archive_run_refuses_before_staging_when_state_lock_cannot_be_named(tmp_path, monkeypatch):
    """Archive cannot stage or remove anything when its mandatory lock fails."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "state_root", _raising(runs.StateRootError("no root")))

    with pytest.raises(runs.StateRootError, match="no root"):
        runs.archive_run(tmp_path, run_dir)

    assert run_dir.is_dir()
    assert not (tmp_path / ".bmad-loop" / "archive").exists()


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
        ("project_tag", OSError("cannot canonicalize")),
        ("project_tag", RuntimeError("Symlink loop from '/p'")),
    ],
    ids=["unresolvable-project", "symlink-loop-project"],
)
def test_delete_run_survives_a_counterpart_it_cannot_name(tmp_path, monkeypatch, attr, exc):
    """The counterpart removal is a never-raise tail (#139 teardown doctrine).

    Every row is a project the OS refuses to canonicalize (#552) only after the
    mandatory lifecycle lock was named. Removal failures are absorbed separately,
    by `ignore_errors`. A missing state root now refuses before removal because
    cleanup cannot safely rendezvous with resume without that lock.

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
    monkeypatch.setattr(runs, "get_multiplexer", lambda: _LivenessMux(["bmad-loop-r1"]))
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
    monkeypatch.setattr(
        runs,
        "get_multiplexer",
        lambda: _LivenessMux(
            ["bmad-loop-r1"],
            tags={"bmad-loop-r1": runs.project_tag(tmp_path / "someone-else")},
        ),
    )
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def test_delete_run_refuses_a_session_tagged_as_ours(tmp_path, monkeypatch):
    """The mirror of the test above, so the tag read cannot be mistaken for "any
    tag clears the guard". Our own tag proves nothing about whether the removal is
    safe — it only fails to prove the session foreign — so the refusal stands."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(
        runs,
        "get_multiplexer",
        lambda: _LivenessMux(["bmad-loop-r1"], tags={"bmad-loop-r1": runs.project_tag(tmp_path)}),
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
    already does for a dead server. (A stronger contract — refuse on the raise —
    was built on this branch and withdrawn: the guard's degrade is main's
    documented decision, and the measured cost is filed for its owner.)"""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(runs, "get_multiplexer", lambda: _LivenessMux([], unanswerable=True))
    runs.delete_run(tmp_path, run_dir)
    assert not run_dir.exists()


def test_delete_run_refuses_when_the_tag_read_raises(tmp_path, monkeypatch):
    """The tag read degrades the other way. By the time the tag is queried the
    probe has already proven a session live, and a tag that could not be read
    is not proof it is another project's — so it reads as untagged and the
    refusal stands. Asserted separately from the probe case: one `except`
    landing the wrong constant would otherwise hide behind the other."""
    run_dir = _make_state_run(tmp_path, "r1")

    class _TagsBroken(_LivenessMux):
        def session_options(self, option):
            raise MultiplexerError("option read failed")

    monkeypatch.setattr(runs, "get_multiplexer", lambda: _TagsBroken(["bmad-loop-r1"]))
    with pytest.raises(runs.LiveSessionError):
        runs.delete_run(tmp_path, run_dir)
    assert run_dir.exists()


def test_delete_run_matches_the_session_by_exact_run_id(tmp_path, monkeypatch):
    """The guard keys on `bmad-loop-<id>` exactly. A session for a *different* run —
    including one whose id merely extends ours — must not block this removal, or one
    live run would wedge cleanup for every id it prefixes."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(
        runs,
        "get_multiplexer",
        lambda: _LivenessMux(["bmad-loop-r1-2", "bmad-loop-ctl", "r1"]),
    )
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


@pytest.mark.parametrize(
    "kind",
    [
        "outside-the-project",
        "the-runs-root-itself",
        "a-nested-grandchild",
        "the-dot-dot-alias",
    ],
)
def test_delete_run_refuses_a_run_dir_outside_the_runs_dir(tmp_path, kind):
    """#480's containment half, and the reason it is a second guard rather than a
    tighter ref check: `delete_run` is module-public and takes a `run_dir` outright,
    so a path composed by any route other than `resolve_run_dir` never meets
    `_is_path_escape` at all.

    The `..` row is the round-1 review catch — the one spelling the rebuild
    equality is blind to: `.name` of `runs / ".."` is `".."` and the rebuild
    reproduces it verbatim, so the lexical comparison holds while `rmtree` would
    resolve it to `.bmad-loop` itself (its canary lives there). Ablation: drop
    the `run_dir.name in (".", "..")` clause and that row reddens alone.

    The canary — not the raise — is what grades the guard's PLACEMENT: a guard that
    raised *after* `shutil.rmtree` would satisfy `pytest.raises` and still have
    destroyed the directory. Ablation: delete the guard and the raise assertion
    reddens; move it below the `rmtree` and the canary assertion reddens alone."""
    project = tmp_path / "proj"
    _make_run(project, "20260620-143025-a1b2")
    runs_root = project / ".bmad-loop" / "runs"
    target = {
        "outside-the-project": tmp_path / "outside",
        "the-runs-root-itself": runs_root,
        "a-nested-grandchild": runs_root / "20260620-143025-a1b2" / "nested",
        "the-dot-dot-alias": runs_root / "..",
    }[kind]
    target.mkdir(parents=True, exist_ok=True)
    canary = target / "canary.txt"
    canary.write_text("survives")

    with pytest.raises(platform_util.UnconfinedWriteError, match="not a run directory under"):
        runs.delete_run(project, target)
    assert canary.is_file()

    # `force` is the operator accepting a leaked session, never a licence to
    # rmtree outside the runs dir — so containment sits above it, not under it.
    with pytest.raises(platform_util.UnconfinedWriteError):
        runs.delete_run(project, target, force=True)
    assert canary.is_file()


def _redirected_project(tmp_path, level, run_name):
    """A project whose ``level`` — ``.bmad-loop``, its ``runs`` dir, or the run
    itself — is a symlink into an external tree holding a real state.json-bearing
    run. The lexical rebuild in `_refuse_uncontained_run_dir` is identical for all
    three, which is exactly what the link walk exists to see through. Returns
    ``(project, run_dir, external_run, canary)`` — the canary lives in the
    redirect TARGET, because that is what a guard trusting the lexical spelling
    hands `rmtree`."""
    project = tmp_path / "proj"
    external = tmp_path / "external"
    if level == "the-run-dir":
        (project / ".bmad-loop" / "runs").mkdir(parents=True)
        ext_run = external / run_name
        link, target = project / ".bmad-loop" / "runs" / run_name, ext_run
    elif level == "the-runs-dir":
        (project / ".bmad-loop").mkdir(parents=True)
        ext_run = external / run_name
        link, target = project / ".bmad-loop" / "runs", external
    else:  # the-state-dir
        project.mkdir()
        ext_run = external / "runs" / run_name
        link, target = project / ".bmad-loop", external
    ext_run.mkdir(parents=True)
    (ext_run / "state.json").write_text("{}")
    canary = ext_run / "canary.txt"
    canary.write_text("survives")
    link.symlink_to(target)
    return project, project / ".bmad-loop" / "runs" / run_name, ext_run, canary


@pytest.mark.parametrize("level", ["the-state-dir", "the-runs-dir", "the-run-dir"])
def test_delete_run_refuses_a_redirected_run_dir(tmp_path, level):
    """Round-1 review (codex P1): with an orchestrator-owned level replaced by a
    symlink, `run_dir_for(project, run_dir.name)` is lexically identical to
    `run_dir`, so the rebuild equality holds while `rmtree` follows the redirect
    and removes a tree OUTSIDE the project — a planted redirect being this
    module's live threat class (see the #591 notes in `archive_run`). The guard
    walks `is_link_like` — not `is_symlink`, which reports False for the
    unelevated win32 junction — over every level below `project`, and stops
    short of `project` itself: a project addressed through a symlinked home is
    the operator's own business.

    Ablation (measured): drop the link walk from `_refuse_uncontained_run_dir`
    and every arm reddens on the raise expectation — the state-dir and runs-dir
    arms as DID NOT RAISE, because the delete *succeeds* and eats the external
    run (the canary assertion never even runs; it is what would catch a guard
    moved below the rmtree), and the run-dir arm on the raise TYPE, because
    `shutil.rmtree` refuses a symlink argument itself but with a plain OSError
    where the containment contract promised UnconfinedWriteError."""
    run_name = "20260620-143025-a1b2"
    project, run_dir, ext_run, canary = _redirected_project(tmp_path, level, run_name)

    with pytest.raises(platform_util.UnconfinedWriteError, match="symlink or junction"):
        runs.delete_run(project, run_dir)
    assert canary.is_file()
    assert ext_run.is_dir()

    # containment sits above `force`, exactly as in the lexical test above
    with pytest.raises(platform_util.UnconfinedWriteError):
        runs.delete_run(project, run_dir, force=True)
    assert canary.is_file()


def test_delete_run_never_consults_availability(tmp_path, monkeypatch):
    """A regression this branch once shipped and withdrew: an arm that read
    `mux_usable(False)` as session absence. Usability folds in helper binaries
    and version gates — psmux with `pwsh` off PATH probes unavailable while its
    server hosts this very session — so the guard must key on the listing
    alone: a listable live session refuses even when `available()` is False.

    Ablate by re-adding a `mux_usable` short-circuit ahead of the listing and
    this fails with the run dir gone under the live session."""
    run_dir = _make_state_run(tmp_path, "r1")
    monkeypatch.setattr(
        runs,
        "get_multiplexer",
        lambda: _LivenessMux(["bmad-loop-r1"], unavailable=True),
    )
    with pytest.raises(runs.LiveSessionError, match="still live"):
        runs.delete_run(tmp_path, run_dir)
    assert run_dir.exists()


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


# --------------------------------------------------- restamp_code_root


@pytest.mark.parametrize("recorded", ["moved", "unchanged", "legacy"])
def test_restamp_code_root_aims_the_mirror_the_rearm_reads(tmp_path, recorded):
    """`rearm_escalation` reads the CODE tree out of the run state (`RunState.code_root`)
    and has no `ProjectPaths` to consult, so the surfaces that re-arm BEFORE they resume
    have to aim that mirror first. Three rows, because the write and the warning answer
    different questions:

    - `moved` — a `repo_root:` edit while the run was paused. The mirror follows, and the
      operator is told, because every sha the run already recorded names an object in the
      previous tree and nothing here can move them.
    - `unchanged` — the ordinary re-arm. No message, and no write at all: a row that
      rewrote state.json on every re-arm would make the "durable before the engine
      starts" ordering above it meaningless to reason about.
    - `legacy` — a state.json written before the field existed reads back `""`. That is a
      MISSING value, not a divergent one: it migrates silently, and calling it a move
      would fire the warning once on every pre-upgrade run.

    The journal line is graded on the same three rows, because it is the DURABLE half
    and the return value is not: the caller prints that string to stderr or a TUI toast
    and it is gone. Worse, this re-stamp is what makes `cli._resume_paused_run`'s own
    `code_root_changed` record read `false` later in the same gesture — both sides read
    `bmadconfig.load_paths` on one project, so once the mirror is aimed the compare
    NECESSARILY agrees. Without this line the one gesture where the root actually moved
    is the one that leaves no trace, while plain `resume` still writes one.

    Ablation: drop the `if not moved: return None` arm and `legacy` reddens on the
    message; return the message without the `save_state` and `moved` reddens on the
    persisted root while the other two rows still pass; delete the `journal.append` and
    `moved` reddens on the record alone, with every message assertion still green.
    """
    from bmad_loop.journal import STATE_FILE, Journal

    run = escalated_run(tmp_path, "r1", story_key="s1")
    now = tmp_path / "code"
    now.mkdir()
    run.state.repo_root = {
        "moved": str(tmp_path / "was"),
        "unchanged": str(now),
        "legacy": "",
    }[recorded]
    save_state(run.run_dir, run.state)
    before = (run.run_dir / STATE_FILE).read_bytes()

    message = runs.restamp_code_root(run.run_dir, now)

    # whatever the row, the tree the re-arm will read is the one the caller is acting in
    assert load_state(run.run_dir).code_root == now
    rewritten = (run.run_dir / STATE_FILE).read_bytes() != before
    assert rewritten is (recorded != "unchanged")
    if recorded == "moved":
        assert message is not None
        assert "the code root in _bmad/bmm/config.yaml has changed" in message
        # names NEITHER tree, like resume's: the fact is that the run changed
        # repositories, and the paths are the half that puts arbitrary text on a terminal
        assert str(now) not in message
        assert str(tmp_path / "was") not in message
    else:
        assert message is None

    # ...and the move is RECORDED, on exactly the row that moved
    records = [
        e for e in Journal(run.run_dir).entries() if e["kind"] == "rearm-code-root-restamped"
    ]
    assert len(records) == (1 if recorded == "moved" else 0)
    if records:
        # `code_root_changed` is resume's own field name, so a reader correlates the two
        # surfaces without knowing which one wrote the line
        assert records[0]["code_root_changed"] is True
        assert records[0]["repo"] == str(now)


def test_restamp_code_root_reloads_after_a_rival_writer(tmp_path, monkeypatch):
    """Ablation: move restamp_code_root's load above state_lock and the rival's
    ``crashed`` update is overwritten by the stale snapshot."""
    run = escalated_run(tmp_path, "r1", story_key="s1")
    save_state(run.run_dir, run.state)

    @contextlib.contextmanager
    def rival_first(_run_dir):
        rival = load_state(run.run_dir)
        rival.crashed = True
        save_state(run.run_dir, rival)
        yield

    monkeypatch.setattr(runs, "state_lock", rival_first)

    runs.restamp_code_root(run.run_dir, tmp_path / "new-code")

    persisted = load_state(run.run_dir)
    assert persisted.crashed is True
    assert persisted.code_root == tmp_path / "new-code"


def test_restamp_code_root_retains_outer_lock_through_save(tmp_path, monkeypatch):
    run = escalated_run(tmp_path, "r1", story_key="s1")
    save_state(run.run_dir, run.state)
    real_save = runs.save_state

    def checked_save(target, state):
        assert_run_state_lock_held(target)
        real_save(target, state)

    monkeypatch.setattr(runs, "save_state", checked_save)

    runs.restamp_code_root(run.run_dir, tmp_path / "new-code")
    assert load_state(run.run_dir).code_root == tmp_path / "new-code"


_SPEC_WITH_ARR = (
    "---\ntitle: t\nstatus: blocked\noperator_actions:\n"
    "  - publish the TXT record\n---\n\n## Intent\n\nbody\n"
    "\n## Auto Run Result\n\n- Status: blocked\n\nboom\n"
)


def test_rearm_restore_mode_sets_in_review_strips_arr_and_latches(tmp_path):
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    runs.rearm_escalation(
        run_dir,
        restore_patch="artifacts/attempt.patch",
        isolated_redrive=False,
        resolution_recorded=True,
    )

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
    runs.rearm_escalation(
        run_dir, isolated_redrive=False, resolution_recorded=True
    )  # no restore_patch => from-scratch

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.PENDING
    assert task.restore_patch is None  # stale latch cleared
    text = spec.read_text()
    assert "status: ready-for-dev" in text
    assert verify.operator_actions_of(verify.read_frontmatter(spec)) == ("publish the TXT record",)
    assert "## Auto Run Result" not in text  # stale attempt authority stripped
    entry = [e for e in Journal(run_dir).entries() if e["kind"] == "story-escalation-resolved"][-1]
    assert entry["restore"] is False


def test_rearm_reloads_state_after_waiting_for_the_run_lock(tmp_path, monkeypatch):
    """Ablation: load state before rearm_escalation's state_lock and this stale
    gesture re-arms after the rival has already completed it."""
    from bmad_loop.model import Phase

    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    original = spec.read_bytes()

    @contextlib.contextmanager
    def rival_first(_run_dir):
        rival = load_state(run_dir)
        rival.tasks["1-1-a"].phase = Phase.PENDING
        save_state(run_dir, rival)
        yield

    monkeypatch.setattr(runs, "state_lock", rival_first)

    with pytest.raises(runs.RearmError, match="is not escalated"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == original


def test_rearm_lock_acquisition_failure_leaves_spec_and_state_unchanged(tmp_path, monkeypatch):
    from bmad_loop import journal as journal_mod

    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    spec_before = spec.read_bytes()
    state_before = (run_dir / journal_mod.STATE_FILE).read_bytes()

    @contextlib.contextmanager
    def refusing_lock(_path):
        raise OSError("state lock unavailable")
        yield

    monkeypatch.setattr(journal_mod, "file_lock", refusing_lock)

    with pytest.raises(OSError, match="state lock unavailable"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == spec_before
    assert (run_dir / journal_mod.STATE_FILE).read_bytes() == state_before


def test_rearm_locked_body_retains_outer_lock_through_state_save(tmp_path, monkeypatch):
    run_dir, _spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)
    real_save = runs.save_state

    def checked_save(target, state):
        assert_run_state_lock_held(target)
        real_save(target, state)

    monkeypatch.setattr(runs, "save_state", checked_save)

    runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)


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
        runs.rearm_escalation(
            run_dir,
            restore_patch="artifacts/attempt.patch",
            isolated_redrive=False,
            resolution_recorded=True,
        )

    assert spec.read_text(encoding="utf-8") == spec_text  # byte-identical
    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED and task.attempt == 2  # nothing persisted
    assert task.restore_patch is None  # the latch never landed either


def _renamed_project_pair(tmp_path):
    """A run whose `state.project` names the tree it LAUNCHED in, plus a live tree
    holding the same spec at the same relative position — the shape a project move
    leaves behind.

    Both copies exist deliberately, and that is what makes the rows below falsifiable:
    a real rename deletes the old tree, so a re-arm aimed at it would merely no-op and
    "the live spec is unflipped" could not tell a wrong target from no write at all.
    With both present, exactly one file changes and the assertion names which.
    """
    recorded_project = tmp_path / "project-before-rename"
    live_project = tmp_path / "project-after-rename"
    for root in (recorded_project, live_project):
        root.mkdir()
        (root / "spec.md").write_text(_SPEC_WITH_ARR, encoding="utf-8")
    recorded_spec = recorded_project / "spec.md"
    run = escalated_run(
        recorded_project,
        "r1",
        story_key="1-1-a",
        attempt=2,
        spec_file=str(recorded_spec),
    )
    return run.run_dir, recorded_spec, live_project / "spec.md", live_project


def test_rearm_flips_the_spec_in_the_live_project_after_a_rename(tmp_path):
    """`resolve.build_context` publishes a `spec_file` REBASED onto the live CLI
    project, because `state.project` is the launch-time spelling and nothing re-stamps
    it — `restamp_code_root` moves the CODE root, and there is no project counterpart.
    The re-arm resolved the same task through `task_spec_path` against that recorded
    project, so after a rename the agent edited one file and the re-arm flipped
    another. In the real shape the recorded tree is gone, so the flip silently no-ops
    (every writer answers an absent path with `False`), the baseline re-stamp is
    skipped, and the re-drive wedges on the escalated attempt's status with the
    escalation spent.

    Both halves are asserted, because a fix that wrote BOTH files would satisfy the
    first alone.

    Ablation: revert `live_spec_path` to a bare `task_spec_path` and this reddens on
    the live spec's unchanged status. `live_spec_root` is graded by the row below
    instead, and deliberately: ablating the ROOT alone is invisible here, because every
    writer answers an out-of-root path by silently dropping to the unconfined arm —
    the write still lands, it just loses #593's O_NOFOLLOW walk. That silent degrade is
    the hazard `task_spec_root`'s own docstring names, so the invariant has to be
    asserted directly rather than inferred from an outcome that cannot see it."""
    run_dir, recorded_spec, live_spec, live_project = _renamed_project_pair(tmp_path)

    runs.rearm_escalation(
        run_dir,
        "1-1-a",
        isolated_redrive=False,
        resolution_recorded=False,
        project_root=live_project,
    )

    assert verify.status_of(verify.read_frontmatter(live_spec)) == "ready-for-dev"
    assert verify.status_of(verify.read_frontmatter(recorded_spec)) != "ready-for-dev"
    assert "## Auto Run Result" not in live_spec.read_text(encoding="utf-8")
    assert "## Auto Run Result" in recorded_spec.read_text(encoding="utf-8")


def test_live_spec_root_still_confines_the_live_spec_path(tmp_path):
    """The pair moves together or not at all. `task_spec_root` is the confinement claim
    about the very path `task_spec_path` produces, so rebasing one alone hands the four
    writers of these bytes a path outside their own root — which none of them refuses.
    They drop to the plain write and #593's O_NOFOLLOW walk of the parent components is
    gone, with no signal anywhere. Nothing downstream can observe that, so the
    containment is asserted here, at the seam that has to preserve it.

    Ablation: revert either helper to its un-rebased original and this reddens — the
    root one on containment, the path one on the root's own identity."""
    recorded_project = tmp_path / "project-before-rename"
    live_project = tmp_path / "project-after-rename"
    for root in (recorded_project, live_project):
        root.mkdir()
        (root / "spec.md").write_text(_SPEC_WITH_ARR, encoding="utf-8")
    run = escalated_run(
        recorded_project,
        "r1",
        story_key="1-1-a",
        spec_file=str(recorded_project / "spec.md"),
    )
    task = run.state.tasks["1-1-a"]

    spec_path = runs.live_spec_path(task, run.state, live_project)
    spec_root = runs.live_spec_root(task, run.state, live_project)

    assert spec_path == live_project / "spec.md"
    assert spec_root == live_project
    assert spec_path.is_relative_to(spec_root)  # the confined arm stays reachable


def test_rearm_without_a_live_project_writes_the_tree_the_run_recorded(tmp_path):
    """The default is byte-for-byte today's behavior, and that is the whole argument
    for it being optional: `None` does not stand in for a fact only the caller holds
    (the way a defaulted `isolated_redrive` would), it names the same tree the caller
    would have passed on every run whose project has not moved.

    Ablation: make the parameter required, or default it to anything but
    `state.project`, and this reddens."""
    run_dir, recorded_spec, live_spec, _live_project = _renamed_project_pair(tmp_path)

    runs.rearm_escalation(run_dir, "1-1-a", isolated_redrive=False, resolution_recorded=False)

    assert verify.status_of(verify.read_frontmatter(recorded_spec)) == "ready-for-dev"
    assert verify.status_of(verify.read_frontmatter(live_spec)) != "ready-for-dev"


def test_rearm_leaves_a_spec_outside_the_recorded_project_alone(tmp_path):
    """`rebase_recorded_project_path` is spelling arithmetic over the recorded project
    PREFIX, so an artifact dir configured outside the project tree — the shared
    external layout `[stories] source` allows, and the one shape
    `_spec_is_shared_with_the_redrive` treats as reachable under isolation — is not
    project-owned and must pass through untouched. Rebasing it would relocate a file
    every checkout already shares onto a tree that does not contain it.

    Ablation: drop the `relative_to` guard's `except ValueError: return path` arm and
    this reddens on an unflipped external spec."""
    external = tmp_path / "shared-artifacts"
    external.mkdir()
    spec = external / "spec.md"
    spec.write_text(_SPEC_WITH_ARR, encoding="utf-8")
    recorded_project = tmp_path / "project-before-rename"
    recorded_project.mkdir()
    live_project = tmp_path / "project-after-rename"
    live_project.mkdir()
    run = escalated_run(recorded_project, "r1", story_key="1-1-a", attempt=2, spec_file=str(spec))

    runs.rearm_escalation(
        run.run_dir,
        "1-1-a",
        isolated_redrive=False,
        resolution_recorded=False,
        project_root=live_project,
    )

    assert verify.status_of(verify.read_frontmatter(spec)) == "ready-for-dev"


def test_rearm_leaves_a_spec_that_traverses_out_of_the_recorded_project_alone(tmp_path):
    """`Path.relative_to` is a PREFIX match: ``<recorded>/../shared/spec.md`` passes it
    with remainder ``../shared/spec.md``, so a persisted spelling that climbs OUT of
    the recorded project (into an external artifact root) used to be classified
    project-owned and, once the project moved to a different parent, rebased to
    ``<live>/../shared/spec.md`` — a different file. A ``..`` after the recorded
    prefix names a tree outside it, so `rebase_recorded_project_path` now returns
    the spelling unchanged and the re-arm flips the file the run actually recorded.
    (An in-project spelling still rebases:
    `test_rearm_flips_the_spec_in_the_live_project_after_a_rename`.)

    Both parents hold a `shared/spec.md` deliberately: the decoy under the NEW
    parent is the file the redirected spelling names, so "unflipped" on the recorded
    spec cannot be read as a no-op. `resolve.build_context` publishes the same
    rebased path with no confinement at all, so the agent would be handed the decoy.

    Ablation: drop the ``".." in relative.parts`` arm and the direct assertion
    reddens on ``<live>/../shared/spec.md``; with that assertion removed too, the
    re-arm dies in `UnconfinedWriteError` (the redirected spelling climbs out of the
    rebased confine root) rather than flipping the recorded spec."""
    old_parent = tmp_path / "old-parent"
    new_parent = tmp_path / "new-parent"
    recorded_project = old_parent / "project"
    live_project = new_parent / "project"
    for root in (recorded_project, live_project):
        root.mkdir(parents=True)
    recorded_spec = old_parent / "shared" / "spec.md"
    decoy = new_parent / "shared" / "spec.md"
    for spec in (recorded_spec, decoy):
        spec.parent.mkdir()
        spec.write_text(_SPEC_WITH_ARR, encoding="utf-8")
    traversing = recorded_project / ".." / "shared" / "spec.md"  # the persisted spelling
    run = escalated_run(
        recorded_project, "r1", story_key="1-1-a", attempt=2, spec_file=str(traversing)
    )
    state = load_state(run.run_dir)
    assert runs.rebase_recorded_project_path(traversing, state, live_project) == traversing

    runs.rearm_escalation(
        run.run_dir,
        "1-1-a",
        isolated_redrive=False,
        resolution_recorded=False,
        project_root=live_project,
    )

    assert verify.status_of(verify.read_frontmatter(recorded_spec)) == "ready-for-dev"
    assert verify.status_of(verify.read_frontmatter(decoy)) != "ready-for-dev"


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

    runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

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


def _rendered_rearm_notice(entry):
    rendered = runs.rearm_event_notice(entry)
    assert rendered is not None
    return runs.RearmNotice(*rendered)


def test_rearm_excludes_stale_restore_residue_from_baseline_snapshot(tmp_path):
    """The abandoned attempt's applied new files must NOT be blessed as
    pre-existing, or finalize_commit's `add -A` sweeps them into the corrected
    story's commit. The resolve session's own untracked file still is.

    Also the commits probe's ORDINARY answer: nothing was committed above the old
    baseline here, so `verify.commits_above` returns `[]` and BOTH commit records
    stay away. That silence is the one an operator is entitled to read as "clean",
    which is exactly why the failed probe now writes `rearm-commits-probe-failed`
    instead of reproducing it (DW-81).

    Ablation: relax the producer's `if shas:` gate to `if shas is not None:` and the
    `stale-restore-commits` assertion reddens; journal the probe failure outside its
    `except` arm and the `rearm-commits-probe-failed` one does.
    """
    run_dir, _spec, patch = _stale_restore_tree(tmp_path)

    outcome = runs.rearm_escalation(
        run_dir, isolated_redrive=False, resolution_recorded=True
    )  # from-scratch re-arm replaces the latch

    task = load_state(run_dir).tasks["1-1-a"]
    assert "human.txt" in task.baseline_untracked
    assert "newfile.txt" not in task.baseline_untracked
    assert (tmp_path / "newfile.txt").exists()  # rearm deletes nothing; the re-drive's reset does
    excluded = _kinds(run_dir, "stale-restore-excluded")
    assert len(excluded) == 1
    assert excluded[0]["files"] == ["newfile.txt"]
    assert excluded[0]["patch"] == str(patch)
    assert outcome.notices == (_rendered_rearm_notice(excluded[0]),)
    # the probe ran and answered "none" — neither commit record may appear
    assert not _kinds(run_dir, "stale-restore-commits")
    assert not _kinds(run_dir, "rearm-commits-probe-failed")


def test_rearm_re_latching_the_same_patch_still_excludes_its_residue(tmp_path):
    """Re-arming a restore onto the same patch: the first application's files are
    still residue (and `git apply` would otherwise fail with 'already exists')."""
    run_dir, _spec, _patch = _stale_restore_tree(tmp_path)

    runs.rearm_escalation(
        run_dir,
        restore_patch="artifacts/attempt.patch",
        isolated_redrive=False,
        resolution_recorded=True,
    )

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

    outcome = runs.rearm_escalation(
        run_dir, isolated_redrive=False, resolution_recorded=True
    )  # must not raise RearmError

    task = load_state(run_dir).tasks["1-1-a"]
    assert {"human.txt", "newfile.txt"} <= set(task.baseline_untracked)  # full snapshot
    unparseable = _kinds(run_dir, "stale-restore-unparseable")
    assert len(unparseable) == 1
    assert "FileNotFoundError" in unparseable[0]["error"]
    assert not _kinds(run_dir, "stale-restore-excluded")
    # the unreadable patch must not also cost the human the commits warning
    (commits,) = _kinds(run_dir, "stale-restore-commits")
    assert outcome.notices == (
        _rendered_rearm_notice(unparseable[0]),
        _rendered_rearm_notice(commits),
    )


def test_rearm_without_a_stale_latch_journals_no_stale_restore_events(tmp_path):
    run_dir, _spec = _escalated_run(tmp_path, _SPEC_WITH_ARR, git_project=True)
    (tmp_path / "human.txt").write_text("from the resolve session\n")

    runs.rearm_escalation(
        run_dir,
        restore_patch="artifacts/attempt.patch",
        isolated_redrive=False,
        resolution_recorded=True,
    )

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

    outcome = runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.baseline_commit != old_baseline  # baseline advanced past the commit
    warned = _kinds(run_dir, "stale-restore-commits")
    assert len(warned) == 1
    assert warned[0]["old_baseline"] == old_baseline
    assert warned[0]["commits"] == [git(tmp_path, "rev-parse", "HEAD")]
    (excluded,) = _kinds(run_dir, "stale-restore-excluded")
    assert outcome.notices == (
        _rendered_rearm_notice(excluded),
        _rendered_rearm_notice(warned[0]),
    )


def test_rearm_survives_a_git_fault_reading_commits_above_the_old_baseline(tmp_path):
    """A bad old baseline is warn-only, and the persisted reset proves re-arm
    reached its save rather than returning early — and it now leaves a RECORD.

    The probe failing used to be byte-identical to the probe finding nothing: both
    wrote no journal entry, so `assert not _kinds(run_dir, "stale-restore-commits")`
    below is true for two opposite reasons and cannot tell them apart. The
    `rearm-commits-probe-failed` assertions are what separate them (DW-81) — without
    them this test passes on a re-arm that silently swallowed the fault.

    Ablation: catch a type outside ``verify.GitError`` and the real rev-list
    failure escapes before any of these completion assertions can run. Delete the
    new ``journal.append("rearm-commits-probe-failed", ...)`` and the length
    assertion below reddens while every pre-existing assertion here stays green —
    which is the gap it was added to close.
    """
    from bmad_loop.model import Phase

    run_dir, _spec, _patch = _stale_restore_tree(tmp_path)
    state = load_state(run_dir)
    task = state.tasks["1-1-a"]
    initial_generation = task.generation
    task.baseline_commit = "0" * 39 + "1"  # sha-shaped, but names no object
    save_state(run_dir, state)

    outcome = runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.PENDING
    assert task.attempt == 0
    assert task.generation == initial_generation + 1
    assert task.restore_patch is None
    assert task.baseline_commit == git(tmp_path, "rev-parse", "HEAD")
    assert not _kinds(run_dir, "stale-restore-commits")  # the probe never answered...
    probe = _kinds(run_dir, "rearm-commits-probe-failed")  # ...and now says so
    assert len(probe) == 1
    assert probe[0]["old_baseline"] == "0" * 39 + "1"  # the baseline it could not read
    assert probe[0]["story_key"] == "1-1-a"
    # the typed error, spelled the way the sibling `rearm-baseline-advance-failed`
    # spells it — `GitError: ...`, not a bare repr
    assert probe[0]["error"].startswith("GitError: ")
    assert "rev-list" in probe[0]["error"]
    excluded = _kinds(run_dir, "stale-restore-excluded")
    assert len(excluded) == 1
    assert excluded[0]["files"] == ["newfile.txt"]
    assert outcome.notices == (
        _rendered_rearm_notice(excluded[0]),
        _rendered_rearm_notice(probe[0]),
    )


def test_rearm_survives_a_non_repo_code_tree_when_reading_commits(tmp_path):
    """A non-repository code tree reaches the same typed, warn-only degrade — and
    the same record, because the operator's exposure is identical either way.

    Ablation: catch a type outside ``verify.GitError`` and the pinned probe fault
    escapes, so the persisted generation and latch reset never appear. Delete the
    new ``journal.append("rearm-commits-probe-failed", ...)`` and only the record
    assertions redden.
    """
    from bmad_loop.model import Phase

    run_dir, _spec = _escalated_run(tmp_path, _SPEC_WITH_ARR, restore_patch_stale="old.patch")
    state = load_state(run_dir)
    task = state.tasks["1-1-a"]
    initial_generation = task.generation
    task.baseline_commit = "0" * 39 + "1"
    save_state(run_dir, state)
    with pytest.raises(verify.GitError):
        verify.commits_above(tmp_path, task.baseline_commit)

    runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.PENDING
    assert task.attempt == 0
    assert task.generation == initial_generation + 1
    assert task.restore_patch is None
    assert task.baseline_commit == "0" * 39 + "1"
    assert not _kinds(run_dir, "stale-restore-commits")
    probe = _kinds(run_dir, "rearm-commits-probe-failed")
    assert len(probe) == 1
    assert probe[0]["old_baseline"] == "0" * 39 + "1"
    assert probe[0]["error"].startswith("GitError: ")
    assert len(_kinds(run_dir, "stale-restore-unparseable")) == 1


def test_rearm_skips_the_commits_probe_entirely_without_a_recorded_baseline(tmp_path):
    """No recorded baseline means no range to ask about, so the probe never runs —
    and a probe that never ran must not journal that it FAILED.

    The `if old_baseline:` guard is what separates "there was nothing to measure
    against" from "the measurement broke", and `rearm-commits-probe-failed` claims
    the second. Telling an operator to go diff a range that was never established
    would be the mirror of the silence DW-81 closed: a warning with no referent,
    trained straight into the scroll-past habit the `restore` split exists to prevent.

    The sibling `stale-restore-unparseable` is asserted PRESENT on purpose: without
    it this test is green for the uninteresting reason that
    `_stale_restore_residue` returned early on a missing latch and journalled
    nothing at all. It proves the function ran and only the commits block was
    skipped.

    Ablation: delete the `if old_baseline:` guard and `commits_above` is handed a
    `None` baseline, git fails on the `None..HEAD` range, and the record this test
    denies appears.
    """
    run_dir, _spec = _escalated_run(tmp_path, _SPEC_WITH_ARR, restore_patch_stale="old.patch")
    assert load_state(run_dir).tasks["1-1-a"].baseline_commit is None  # pin the premise

    runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert not _kinds(run_dir, "rearm-commits-probe-failed")
    assert not _kinds(run_dir, "stale-restore-commits")
    # ...while the residue pass itself did run: the latched patch is missing
    assert len(_kinds(run_dir, "stale-restore-unparseable")) == 1


def test_rearm_does_not_swallow_a_non_git_fault_from_the_commits_probe(monkeypatch, tmp_path):
    """Only Git faults are warn-only; programming faults must escape — and the re-arm
    they escape from leaves NOTHING behind.

    The propagation half graded the narrowing and stopped there, which made it silent on
    the state the escape left: this probe runs after the status flip and the
    `## Auto Run Result` strip have both published and before `save_state`, so the fault
    used to exit with the spec re-armed on disk against a task the run still calls
    ESCALATED — the one edit nothing else records (DW-79/DW-83). The window is one
    transaction now, so the same fault also has to come back byte-identical.

    `generation` and `restore_patch` are read from the RELOADED task, not the object the
    call mutated: `rearm_escalation` bumps both in memory long before the guard, and
    `save_state` is the only thing that would have made them true. Asserting on the
    in-memory task would pass with the whole transaction deleted.

    Ablations: widen the catch back to ``Exception`` and this fails with
    ``DID NOT RAISE``, grading the narrowing; delete the `except BaseException` arm from
    `rearm_escalation` and the spec-bytes assertion reddens while the raise still passes.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()
    was = load_state(run_dir).tasks["1-1-a"]

    def boom(repo, baseline):
        raise MemoryError("not a git answer")

    monkeypatch.setattr(runs.verify, "commits_above", boom)
    with pytest.raises(MemoryError, match="not a git answer"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before  # flip AND strip both undone
    task = load_state(run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED  # nothing was persisted, so it is still armed
    assert task.generation == was.generation
    assert task.restore_patch == was.restore_patch
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"  # a published write really was put back
    assert "MemoryError" in aborted["error"]
    assert aborted["spec_file"] == str(spec)
    # ...and the degrade record is NOT one of the things it leaves behind: this fault
    # is not a git answer, so the warn-only arm never runs and the abort is the whole
    # story. Graded by the same narrowing as the raise above — widen the catch to
    # `Exception` and the fault is swallowed into a record instead of propagating.
    assert not _kinds(run_dir, "rearm-commits-probe-failed")


def test_rearm_rolls_back_when_save_state_itself_fails(monkeypatch, tmp_path):
    """`save_state` IS the commit point, so a fault raised BY it is the sharpest case
    the transaction exists for: every spec write has landed and the one thing that would
    make them true has not.

    It was also outside every undo the function used to carry — those sat in two `except`
    arms further up — so an ENOSPC here left the spec flipped and stripped while
    `state.json` still described an escalated story, with nothing on the record at all.

    The state file is compared BYTE-for-byte rather than by reloading and checking the
    phase: a phase check passes for every reason a write could be absent, while the bytes
    also grade the `generation` bump and the cleared `defer_reason` riding in the same
    object.

    Ablation: delete the `except BaseException` arm and the spec bytes and the abort
    record both redden; the OSError still propagates, which is why it alone is no oracle.
    """
    from bmad_loop.journal import STATE_FILE
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()
    state_before = (run_dir / STATE_FILE).read_bytes()

    def boom(run_dir_, state_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "save_state", boom)
    with pytest.raises(OSError, match="No space left on device"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before
    assert (run_dir / STATE_FILE).read_bytes() == state_before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"
    assert "OSError" in aborted["error"]


def test_rearm_rolls_back_when_the_window_is_interrupted(monkeypatch, tmp_path):
    """The guard catches `BaseException`, and the breadth is load-bearing rather than
    defensive — nothing else in the suite grades it.

    `KeyboardInterrupt` and `SystemExit` derive from `BaseException` alone, so narrowing
    the arm to `except Exception` passes every other test while reopening the exact
    DW-79/DW-83 state on the most ordinary operator gesture there is. The window is
    mostly blocking I/O: three git subprocesses (`rev_parse_head`, `untracked_files`,
    `commits_above`) and then `save_state`, all AFTER the status flip has published and
    BEFORE anything persists it. A Ctrl-C in there under a narrowed arm exits with the
    spec re-armed on disk against a task still recorded as ESCALATED.

    Raised from `save_state` because that is the last statement inside the guard, so the
    interrupt lands at the widest point of the exposure — every spec write behind it and
    the commit point not yet reached.

    Ablation (run): narrow the arm to `except Exception` and this reddens on the
    spec-bytes assertion, which is simply the first of the four to run — remove the three
    tree/state assertions and the abort record reddens behind them with
    `ValueError: not enough values to unpack`, because with the arm narrowed no
    `rearm-aborted` entry is written at all. The `KeyboardInterrupt` still propagates
    either way, which is exactly why the raise alone is no oracle.
    """
    from bmad_loop.journal import STATE_FILE
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()
    state_before = (run_dir / STATE_FILE).read_bytes()

    def interrupted(run_dir_, state_):
        raise KeyboardInterrupt

    monkeypatch.setattr(runs, "save_state", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before
    assert (run_dir / STATE_FILE).read_bytes() == state_before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"
    assert "KeyboardInterrupt" in aborted["error"]


def test_rearm_abort_on_the_sentinel_leg_claims_nothing_about_the_file(monkeypatch, tmp_path):
    """The sentinel clear is the one in-window tree change the transaction does NOT undo,
    so the abort record must not describe the tree as untouched.

    That leg deletes the sentinel rather than writing spec bytes, and re-creating it would
    fight a gesture that is already safe to repeat — `_clear_sentinel` preserves a copy
    under `{run_dir}/sentinels/` and a retried resolve re-clears it idempotently. What was
    wrong was never the deletion; it was the CLAIM. `spec_before` is `None` on this leg,
    and folding that into `unchanged` made the notice name a file this re-arm had just
    DELETED as proof nothing moved.

    So the outcome is `unknown`, and the rendering is graded here rather than only the
    field: the field is what the producer wrote, the sentence is what the operator reads.

    Ablation (run): make `_restore_rearmed_spec` answer `"unchanged"` for
    `original is None` and this reddens on the recorded `rollback` first; drop that one
    assertion and the RENDERED message reddens behind it, on a notice that now reads
    "(…1-1-a-unresolved.md) was left exactly as the re-arm found it" about a file this
    re-arm deleted. Both halves are asserted because the field and the sentence are
    different claims. The deletion assertions keep passing throughout, which is why they
    alone do not grade this.
    """
    from bmad_loop.journal import STATE_FILE
    from bmad_loop.model import Phase

    sentinel = tmp_path / "1-1-a-unresolved.md"
    sentinel.write_text("---\nstatus: blocked\n---\n\nplanning halted\n", encoding="utf-8")
    run = escalated_run(
        tmp_path,
        "r1",
        story_key="1-1-a",
        source="stories",
        sentinel_kind="unresolved",
        spec_file=str(sentinel),
        git_project=True,
    )
    run_dir = run.run_dir
    state_before = (run_dir / STATE_FILE).read_bytes()

    def boom(run_dir_, state_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "save_state", boom)
    with pytest.raises(OSError, match="No space left on device"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    # the deletion STANDS — it is deliberately outside the transaction — and the
    # preserved copy is why that is safe
    assert not sentinel.exists()
    assert (run_dir / "sentinels" / sentinel.name).is_file()
    # ...while everything the transaction DOES cover was rolled back
    assert (run_dir / STATE_FILE).read_bytes() == state_before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED

    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "unknown"
    _severity, message, _next_step = runs.rearm_event_notice(aborted)
    assert "the re-arm ABORTED" in message
    assert "still escalated" in message
    # the whole point: no claim about a file that is no longer there
    assert "left exactly as the re-arm found it" not in message


def test_rearm_rolls_back_when_a_mid_window_journal_append_fails(monkeypatch, tmp_path):
    """A `journal.append` inside the residue pass is an ordinary file write and can fail
    like one — and it is the fault source furthest from anything that looks like a spec
    write, which is exactly why no per-arm undo ever covered it.

    Only the residue kind is made to fail, so the abort record itself can still land: the
    point being graded is that a fault from a helper that writes no spec still rolls the
    spec back and still says so.

    Ablation: delete the `except BaseException` arm and the spec-bytes assertion reddens.
    """
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()
    real_append = Journal.append

    def flaky(self, kind, **fields):
        if kind == "stale-restore-excluded":
            raise OSError(5, "Input/output error")
        return real_append(self, kind, **fields)

    monkeypatch.setattr(Journal, "append", flaky)
    with pytest.raises(OSError, match="Input/output error"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"


def test_rearm_records_unchanged_when_the_sequenced_refusal_fires(tmp_path):
    """`rollback: "unchanged"` is a DIFFERENT fact from `"restored"`, and the surfaces
    render it differently, so the flip's read-back refusal has to produce it.

    That refusal is sequenced ahead of every write — `set_frontmatter_status` decides it
    cannot move a spec with no top-level `status:` before it writes anything, and the
    `## Auto Run Result` strip is deliberately ordered after the check — so the guard
    finds the spec exactly as `spec_before` captured it and rewrites nothing. Recording
    that as `restored` would tell an operator a write had landed and been undone on the
    one path where nothing was ever written.

    Ablation: make `_restore_rearmed_spec`'s "bytes equal to `original`" arm return True
    — the arm this path actually takes — and this reddens on the `rollback` value alone,
    while every other assertion still passes. Its `original is None` arm does NOT grade
    this row: the spec here is readable, so `spec_before` is set and that arm never runs.
    """
    from bmad_loop.model import Phase

    run_dir, spec = _escalated_run(
        tmp_path, "---\ntitle: t\n---\n\n## Intent\n\nbody\n\n## Auto Run Result\n\nx\n"
    )
    before = spec.read_bytes()

    with pytest.raises(runs.RearmError, match="no frontmatter `status:`"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "unchanged"
    assert "RearmError" in aborted["error"]


def test_rearm_reports_a_rollback_that_itself_failed_and_keeps_the_original_fault(
    monkeypatch, tmp_path
):
    """A restore that cannot write leaves a HALF-WRITTEN spec, which is the loudest thing
    this can be — so it raises through the guard rather than degrading, and the abort
    record says `failed` so both surfaces print the "restore it from git" remedy instead
    of "the spec was left as the re-arm found it".

    The chain is the other half of the claim. `_restore_rearmed_spec` raises WHILE the
    original fault is being handled, so that fault rides in `__context__` and the
    operator sees both causes rather than a `RearmError` that has erased the reason the
    re-arm aborted in the first place.

    Only the restore's writer is broken: the flip and the strip go through `verify` and
    `devcontract`, so this injection cannot pre-empt the writes it is meant to fail to
    undo.

    Ablation: move the abort record out of `_rollback_rearm`'s `finally` into its success
    path and the `failed` row disappears entirely — the raise still propagates, which is
    why the record and not the exception is what grades this.
    """
    run_dir, spec, _patch = _stale_restore_tree(tmp_path)

    def no_space(*_a, **_kw):
        raise OSError(28, "No space left on device")

    def probe_boom(repo, baseline):
        raise MemoryError("not a git answer")

    monkeypatch.setattr(runs.verify, "commits_above", probe_boom)
    monkeypatch.setattr(runs, "atomic_write_bytes_confined", no_space)
    with pytest.raises(runs.RearmError, match="cannot restore") as excinfo:
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert str(spec) in str(excinfo.value)
    chain = []
    exc: BaseException | None = excinfo.value
    while exc is not None:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    assert any(isinstance(e, MemoryError) for e in chain)  # the original fault survives
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "failed"
    # the record names the fault the re-arm ABORTED on, not the one the rollback hit —
    # the second is in the exception the operator already has
    assert "MemoryError" in aborted["error"]


@pytest.mark.parametrize("append_fault", [TypeError, OSError])
def test_rearm_keeps_the_original_fault_when_the_abort_record_cannot_be_written(
    monkeypatch, tmp_path, append_fault
):
    """Writing the abort record is an OBSERVATION, and an observation that cannot be made
    must not REPLACE the fault the operator is being told about.

    `_rollback_rearm` journals `rearm-aborted` from a `finally` that runs while the
    original fault is unwinding, so anything that append raises escapes in its place —
    and the whole re-raise invariant this transaction is built on (the two pinned
    `MemoryError` rows) dies quietly with it. The rollback itself has already completed
    by then, so nothing about DW-79/DW-83 is at stake in that suppression; only the
    breadcrumb is lost, and the operator still receives the fault that explains why.

    BOTH rows matter and they grade different halves. `OSError` is the obvious shape (an
    unwritable journal) and passes under either breadth. `TypeError` is the one that
    grades the WIDTH: `Journal.append` serializes caller-supplied values and opens a
    file, so `json.dumps` and the open can raise outside the filesystem taxonomy
    entirely.

    Ablation: narrow the catch back to `except OSError` and the `TypeError` row reddens
    with `TypeError` where the `MemoryError` should be, while the `OSError` row stays
    green — which is exactly why one row alone is no oracle.
    """
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()

    def probe_boom(repo, baseline):
        raise MemoryError("not a git answer")

    real_append = Journal.append

    def flaky(self, kind, **fields):
        if kind == "rearm-aborted":
            raise append_fault("the abort record could not be written")
        return real_append(self, kind, **fields)

    monkeypatch.setattr(runs.verify, "commits_above", probe_boom)
    monkeypatch.setattr(Journal, "append", flaky)
    with pytest.raises(MemoryError, match="not a git answer"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    # the rollback ran BEFORE the record was attempted, so the transaction still held
    assert spec.read_bytes() == before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    # ...and the suppressed append left no entry, which the acceptance criterion is
    # deliberately conditioned on rather than promising one here
    assert not _kinds(run_dir, "rearm-aborted")


def test_rearm_lets_an_interrupt_from_the_abort_append_leave(monkeypatch, tmp_path):
    """The abort record's append suppresses `Exception` and deliberately NOT
    `KeyboardInterrupt` — the ONE place in this transaction where the breadth is narrower
    than the guard's own `BaseException`, and the asymmetry has to be graded from the
    side the sibling rows cannot reach.

    It is sound only because of WHERE this `finally` runs: the rollback has already
    completed by the time the record is attempted, so the spec is back to the bytes the
    re-arm found and an interrupt escaping here cannot reproduce DW-79/DW-83. What IS at
    stake is the operator's Ctrl-C. Swallowing it to keep a breadcrumb would answer a
    stop they issued themselves with a `MemoryError` traceback, and would leave the
    process running past the point they asked it to stop.

    Ablation: broaden that catch to `except BaseException` and this reddens with the
    `MemoryError` arriving in the interrupt's place. Neither row of
    `test_rearm_keeps_the_original_fault_when_the_abort_record_cannot_be_written`
    reddens there, because `TypeError` and `OSError` are both already `Exception`.
    """
    from bmad_loop.journal import Journal
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()

    def probe_boom(repo, baseline):
        raise MemoryError("not a git answer")

    real_append = Journal.append

    def interrupted(self, kind, **fields):
        if kind == "rearm-aborted":
            raise KeyboardInterrupt
        return real_append(self, kind, **fields)

    monkeypatch.setattr(runs.verify, "commits_above", probe_boom)
    monkeypatch.setattr(Journal, "append", interrupted)
    # the INTERRUPT is what leaves, not the fault the re-arm aborted on
    with pytest.raises(KeyboardInterrupt):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    # ...and letting it through costs nothing: the rollback ran first, so the spec is as
    # the re-arm found it and the story is still armed for a retry
    assert spec.read_bytes() == before
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    assert not _kinds(run_dir, "rearm-aborted")


def test_rearm_records_unknown_when_the_rollback_cannot_read_the_spec(monkeypatch, tmp_path):
    """`unchanged` is earned ONLY by reading the file and proving it byte-equal, so an
    undo that could not even LOOK must answer `unknown`.

    This is the read-failure arm, and it is a different arm from the one the sentinel row
    grades: there `original` is `None` and `_restore_rearmed_spec` returns before touching
    the disk, so that row cannot reach this code at all. Here the preimage was captured
    normally and the file is gone by the time the undo runs — another actor removed it
    mid-window — so `read_bytes` raises, the undo declines to re-create a file it did not
    delete, and it says so.

    Folding that into `unchanged` asserted a byte-equality the producer never checked, and
    the surfaces then told the operator the spec "was left exactly as the re-arm found
    it" about a file that is not there.

    Ablation: make the `except FileNotFoundError` arm in `_restore_rearmed_spec` return
    `"unchanged"` and this reddens on the recorded `rollback` first; drop that assertion
    and the rendered message reddens behind it.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)

    def vanishes(repo, baseline):
        spec.unlink()  # a concurrent actor removes it AFTER the flip published
        raise MemoryError("not a git answer")

    monkeypatch.setattr(runs.verify, "commits_above", vanishes)
    with pytest.raises(MemoryError, match="not a git answer"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert not spec.exists()  # the undo does NOT fight the actor that removed it
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "unknown"
    _severity, message, _next_step = runs.rearm_event_notice(aborted)
    assert "could not confirm what it left on disk" in message
    assert "left exactly as the re-arm found it" not in message


def test_rearm_restores_the_spec_when_the_rollbacks_read_only_faults(monkeypatch, tmp_path):
    """A read that could not be PERFORMED is not evidence the spec is fine.

    The row above grades the one read fault that ANSWERS something: the file is gone, so
    nothing on disk carries the flip and there is nothing to put back. Every other fault
    — EIO, EMFILE, a transient EACCES — says nothing about what is on disk, and this read
    is only the "already identical, skip the write" shortcut. Answering `unknown` there
    abandoned the undo on exactly the runs that still needed it: `save_state` leaves the
    story ESCALATED while the spec keeps the re-arm's status flip and its stripped
    `## Auto Run Result`, which is the split state this whole transaction exists to
    prevent.

    The fault is armed only for the ROLLBACK's read. The preimage capture upstream goes
    through the same call, and faulting that instead degrades `original` to `None` and
    grades the sentinel arm — a different row entirely, which is why the arming flag is
    set from inside the fake that triggers the abort.

    Ablation: fold the two `except` arms back into one `except OSError: return "unknown"`
    and this reddens on the spec's bytes first, then on the recorded `rollback`.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    found = spec.read_bytes()
    real_read_bytes = Path.read_bytes
    armed: list[bool] = []

    def only_during_the_rollback(self):
        if armed and self == spec:
            raise OSError(errno.EIO, "Input/output error")
        return real_read_bytes(self)

    def boom(repo, baseline):
        armed.append(True)  # the flip and the preimage capture are already behind us
        raise MemoryError("not a git answer")

    monkeypatch.setattr(runs.verify, "commits_above", boom)
    monkeypatch.setattr(Path, "read_bytes", only_during_the_rollback)
    with pytest.raises(MemoryError, match="not a git answer"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)
    armed.clear()  # let the assertions read the file back

    assert spec.read_bytes() == found  # the flip WAS undone, not abandoned
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"


def test_rearm_abort_without_a_spec_records_an_empty_locator(monkeypatch, tmp_path):
    """A re-arm that never resolved a spec path writes `""` there, NOT the story key.

    `spec_file` is the SPEC's locator on all five `rearm-*` kinds and
    `diagnostics._JOURNAL_ALIAS_FIELDS` routes it by that field NAME into the `spec`
    namespace. Falling back to the story key would push an identifier through the wrong
    namespace — rendered as a spec that does not exist — and the notice would name it as
    a file. The empty string routes nowhere and renders as `(none)`.

    Ablation: replace the `""` fallback with `story_key` and both halves redden — the
    field carries the key, and the notice names it where `(none)` belongs.
    """
    from bmad_loop.model import Phase

    run = escalated_run(tmp_path, "r1", story_key="1-1-a", git_project=True)
    run_dir = run.run_dir

    def boom(run_dir_, state_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "save_state", boom)
    with pytest.raises(OSError, match="No space left on device"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["spec_file"] == ""
    assert aborted["rollback"] == "unknown"  # no spec path ⇒ no claim about any file
    _severity, message, _next_step = runs.rearm_event_notice(aborted)
    assert "(none)" in message
    assert "1-1-a" not in message


def test_rearm_records_failed_when_the_rollback_write_is_interrupted(monkeypatch, tmp_path):
    """An interrupt during the restore WRITE leaves the spec in exactly the state `failed`
    describes — flipped and not put back — so that is what the record must say.

    `_rollback_rearm`'s inner arm catches `BaseException` for this reason, and the breadth
    is as load-bearing as the guard's own: the restore is a file write, and a Ctrl-C
    landing in it is the ordinary way for one to be abandoned half-done. Under a narrowed
    `except Exception` the interrupt skips the arm, `rollback` keeps its `unknown` floor,
    and the `finally` then records "could not confirm what it left on disk" for a spec
    the run knows perfectly well it left part-written — the one outcome whose remedy is
    not moot.

    Ablation: narrow that inner arm to `except Exception` and this reddens on the
    recorded `rollback` (`unknown` for `failed`). The `KeyboardInterrupt` still
    propagates either way, which is why the raise alone is no oracle.
    """
    run_dir, spec, _patch = _stale_restore_tree(tmp_path)

    def probe_boom(repo, baseline):
        raise MemoryError("not a git answer")

    def interrupted(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(runs.verify, "commits_above", probe_boom)
    monkeypatch.setattr(runs, "atomic_write_bytes_confined", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "failed"
    # the record still names the fault the RE-ARM aborted on, not the interrupt
    assert "MemoryError" in aborted["error"]
    _severity, message, next_step = runs.rearm_event_notice(aborted)
    assert "may be left part-written" in message
    assert next_step.startswith("Restore the spec")


def test_rearm_does_not_roll_back_a_commit_that_already_landed(monkeypatch, tmp_path):
    """`save_state` commits by ATOMIC REPLACE, so a fault escaping the call does not prove
    the transaction failed — and rolling the spec back after a commit that DID land builds
    the mirror image of the defect this guard closes.

    The rename is a single instant; the call around it is not. An interrupt delivered
    between the replace and the return unwinds through the guard with `state.json` already
    describing a PENDING, re-armed task. Undoing the spec there leaves persisted state
    re-armed against a spec that is not, and reports it as "nothing was persisted, the
    story is still escalated" — a false sentence on both operator surfaces, and a re-drive
    that reads the escalated attempt's terminal status on its first save.

    Control flow cannot see this (there is no statement after `save_state` on that path),
    so the guard asks the DISK, the only witness of a rename. No `rearm-aborted` record is
    written on this leg either: every rendering of that kind asserts nothing was
    persisted, and there is no value of `rollback` that is true here.

    Ablation: delete the `_rearm_commit_landed` check from the guard and this reddens on
    the flipped-status assertion — the spec is rolled back underneath committed state —
    with the abort-record assertion reddening behind it.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    real_save_state = runs.save_state

    def commits_then_dies(run_dir_, state_):
        real_save_state(run_dir_, state_)  # the atomic replace LANDS...
        raise KeyboardInterrupt  # ...and the call is interrupted on its way out

    monkeypatch.setattr(runs, "save_state", commits_then_dies)
    with pytest.raises(KeyboardInterrupt):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    text = spec.read_text(encoding="utf-8")
    assert "status: ready-for-dev" in text  # the flip STANDS beside the commit
    assert "## Auto Run Result" not in text
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.PENDING
    assert not _kinds(run_dir, "rearm-aborted")


@pytest.mark.parametrize("probe_fault", [KeyboardInterrupt, OSError])
def test_rearm_rolls_back_when_the_commit_probe_itself_fails(monkeypatch, tmp_path, probe_fault):
    """`_rearm_commit_landed` is asked a question ON THE ERROR PATH, so a fault raised
    ANSWERING it must not cost the rollback.

    Its one call site sits inside the transaction guard's `except BaseException` arm and
    runs BEFORE `_rollback_rearm`, so anything escaping the probe escapes the guard too
    and the undo never happens — the spec left flipped to the re-drive's status and
    stripped of its `## Auto Run Result`, against a task the run still calls ESCALATED,
    with no `rearm-aborted` record. That is DW-79/DW-83 reached through the very code
    added to prevent its mirror image, which is why the probe degrades to "not committed"
    — roll back — on ANY fault rather than only on an `Exception`.

    The probe reads and PARSES a file, so `KeyboardInterrupt` there is an ordinary
    outcome, not a contrivance: `load_state` is a `read_text` plus a `json.loads` plus a
    `RunState.from_dict`, and an operator's Ctrl-C lands wherever it lands.

    Swallowing that interrupt costs nothing the operator asked for. The rollback is a
    repair write whose omission IS the defect, and the guard's `raise` still propagates
    the original `MemoryError` a spec-sized write later — which the last assertion pins.

    BOTH rows matter and they grade different halves. `OSError` (a corrupt or unreadable
    state file) passes under either breadth. `KeyboardInterrupt` is the one that grades
    the WIDTH.

    Ablation: narrow the probe's catch back to `except Exception` and the
    `KeyboardInterrupt` row reddens — the spec comes back flipped and no abort record
    exists — while the `OSError` row stays green, which is exactly why one row alone is
    no oracle.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    before = spec.read_bytes()

    def probe_boom(repo, baseline):
        raise MemoryError("not a git answer")

    real_load_state = runs.load_state
    calls = []

    def unreadable(run_dir_):
        # `rearm_escalation` opens with its OWN `load_state`; only the probe's call,
        # made from inside the guard arm, is the one under test
        calls.append(1)
        if len(calls) > 1:
            raise probe_fault("the commit probe could not read the state file")
        return real_load_state(run_dir_)

    monkeypatch.setattr(runs.verify, "commits_above", probe_boom)
    monkeypatch.setattr(runs, "load_state", unreadable)
    with pytest.raises(MemoryError, match="not a git answer"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert spec.read_bytes() == before  # the rollback ran despite the probe failing
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "restored"
    assert "MemoryError" in aborted["error"]


def test_rearm_refuses_a_spec_whose_bytes_it_could_not_capture(monkeypatch, tmp_path):
    """The preimage read is the transaction's own precondition, so a spec that IS a file
    and whose bytes could not be captured must FAIL BEFORE the first write.

    That read is one syscall among many against a file three later writers open
    independently, so a TRANSIENT fault (EIO on a network mount, a momentary EACCES,
    ENFILE under load) can be followed by writes that all succeed. `spec_before` is then
    `None`, the abort further down records `unknown` and puts nothing back, and the
    re-arm exits with the flip published against a task still ESCALATED — DW-79/DW-83
    reached through the guard's own preimage.

    The fake fails only the FIRST read of this spec, which is what makes that reachable:
    every later read succeeds, so without the refusal the re-arm runs to completion.

    A path that is NOT a file keeps degrading to `None` — a missing spec, a dangling link
    and a directory all answer `False` from every writer below, so there is genuinely
    nothing to undo, and those shapes stay warn-and-continue.

    Ablation: drop the `is_file()` refusal and this reddens with `DID NOT RAISE` — the
    re-arm completes, the spec ends up flipped and stripped, and nothing records it.
    """
    from bmad_loop.model import Phase

    run_dir, spec, _patch = _stale_restore_tree(tmp_path)
    real_read_bytes = Path.read_bytes
    before = real_read_bytes(spec)
    failed_once = []

    def flaky(self):
        if self == spec and not failed_once:
            failed_once.append(1)
            raise OSError(5, "Input/output error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    with pytest.raises(runs.RearmError, match="refuses to write a spec it could not capture"):
        runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert real_read_bytes(spec) == before  # nothing was written, so nothing to undo
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    (aborted,) = _kinds(run_dir, "rearm-aborted")
    assert aborted["rollback"] == "unknown"  # no preimage ⇒ no claim about the file


def test_ordinary_rearm_writes_no_abort_record(tmp_path):
    """The negative side of the transaction: a re-arm that reaches `save_state` must
    leave no trace of a rollback that never happened.

    An abort record on a successful re-arm would print "nothing was persisted, the story
    is still escalated" beside `re-armed <story>` on the very same stderr — the
    contradiction that trains an operator to stop reading the warnings.

    Ablation: move the `_rollback_rearm(...)` call out of the `except` arm into a
    `finally` and this reddens.
    """
    from bmad_loop.model import Phase

    run_dir, spec = _escalated_run(tmp_path, _SPEC_WITH_ARR)

    runs.rearm_escalation(run_dir, isolated_redrive=False, resolution_recorded=True)

    assert not _kinds(run_dir, "rearm-aborted")
    assert load_state(run_dir).tasks["1-1-a"].phase == Phase.PENDING
    assert "## Auto Run Result" not in spec.read_text(encoding="utf-8")
    assert "status: ready-for-dev" in spec.read_text(encoding="utf-8")


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


def test_archive_run_refuses_a_live_engine_before_staging(tmp_path, monkeypatch):
    """Resume-first ordering leaves source, destination, and control state whole."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    state_dir = _seed_state_dir(tmp_path, run_dir.name)

    def live(target):
        assert_run_state_lock_held(target)
        return "alive"

    monkeypatch.setattr(runs, "engine_liveness", live)
    monkeypatch.setattr(
        runs,
        "_refuse_live_session",
        lambda *_args: pytest.fail("session guard ran before authoritative engine probe"),
    )
    with pytest.raises(runs.LiveEngineError, match="refusing to archive"):
        runs.archive_run(tmp_path, run_dir)

    assert run_dir.is_dir()
    assert state_dir.is_dir()
    assert not (tmp_path / ".bmad-loop" / "archive").exists()


def test_archive_run_holds_one_lock_through_snapshot_publish_and_removal(tmp_path, monkeypatch):
    """Snapshot, durable publication, source removal, and control cleanup all
    remain inside the same lifecycle hold.

    Ablation: moving the liveness gate, tar add, replace, rmtree, or discard outside
    the hold fails at that seam. Verified.
    """
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "payload").write_text("data", encoding="utf-8")
    order: list[str] = []
    real_add = runs.tarfile.TarFile.add
    real_replace = runs.atomic_replace
    real_rmtree = runs.shutil.rmtree

    def dead(target):
        assert_run_state_lock_held(target)
        order.append("probe")
        return "dead"

    def checked_add(self, *args, **kwargs):
        assert_run_state_lock_held(run_dir)
        order.append("snapshot")
        return real_add(self, *args, **kwargs)

    def checked_replace(src, dest):
        assert_run_state_lock_held(run_dir)
        order.append("publish")
        return real_replace(src, dest)

    def checked_rmtree(target, *args, **kwargs):
        assert_run_state_lock_held(run_dir)
        order.append("remove")
        return real_rmtree(target, *args, **kwargs)

    def checked_discard(_project, _run_id):
        assert_run_state_lock_held(run_dir)
        order.append("discard")

    monkeypatch.setattr(runs, "engine_liveness", dead)
    monkeypatch.setattr(runs.tarfile.TarFile, "add", checked_add)
    monkeypatch.setattr(runs, "atomic_replace", checked_replace)
    monkeypatch.setattr(runs.shutil, "rmtree", checked_rmtree)
    monkeypatch.setattr(runs, "_discard_state_dir", checked_discard)

    runs.archive_run(tmp_path, run_dir)

    assert order[0] == "probe"
    assert order[-3:] == ["publish", "remove", "discard"]
    assert order[1:-3] and set(order[1:-3]) == {"snapshot"}


def test_archive_run_names_its_temp_after_the_destination(tmp_path, monkeypatch):
    """#363's filename half, and it needs its own test because NOTHING else grades
    it: on the happy path `atomic_replace` consumes the temp under either spelling,
    and the `except BaseException` guard unlinks it whatever it is named — so a
    wrongly named temp reddens no other row in this file. Recording the name handed
    to `os.replace` is the only place the spelling is observable.

    The historical bug: `dest.with_suffix(".tar.gz.tmp")` yielded
    `<id>.tar.tar.gz.tmp`, because `with_suffix` replaces only the LAST suffix and
    `<id>.tar.gz` has stem `<id>.tar`. The name now flows from
    `_mkstemp_beside(dest)`'s prefix, so the contract is prefix+suffix — the temp
    is recognisably this destination's staging file and ends in `.tmp` — with
    mkstemp's random token in between.

    `run_dir` is built BEFORE the patch on purpose: `save_state` writes through the
    same `os.replace`, and building it after would pollute `seen` with a call that
    has nothing to do with the archive.

    Ablation A9: stage beside a different name (`_mkstemp_beside(dest.parent /
    "x")`) and this reddens alone — `test_archive_run` and the guard test below
    stay GREEN, which is exactly why this row exists rather than being folded into
    either."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    seen: list[str] = []
    real = os.replace

    def record(src, dst):
        seen.append(os.path.basename(src))
        real(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)
    dest = runs.archive_run(tmp_path, run_dir)

    # exactly one replace — a retry loop or a second write creeping in would redden
    assert len(seen) == 1
    assert seen[0].startswith(dest.name + ".")
    assert seen[0].endswith(".tmp")


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
    sidecar = runs.lock_path_for(run_dir / "state.json", follow_final_symlink=False)
    with platform_util.file_lock(sidecar, blocking=False):
        pass  # the original archive error released lifecycle exclusion


def test_archive_run_temp_is_created_exclusively_at_0600(tmp_path, monkeypatch):
    """#591. The staging create must be exclusive — that is the whole license for
    the `except BaseException` unlink below it, which would otherwise remove a name
    this process never owned. The site delegates to `_mkstemp_beside`, whose own
    rows in test_platform_util grade the `O_EXCL` `0600` create and the NAME_MAX
    ladder, so what THIS row grades is the delegation: the spy is the only place
    the choice of writer is visible on the happy path, where `atomic_replace`
    consumes the temp whichever writer staged it. The spy mirrors
    `tempfile.mkstemp`'s exact keyword signature on purpose — it doubles as the
    call-shape pin.

    `os.umask(0o022)` is the point of the bracket, not hygiene — the same trap
    `test_file_lock_is_created_owner_only` documents. Under a 0o077 umask a
    mode-less create produces 0o600 by accident and the on-disk assertion goes
    inert, so the bracket is what makes this row's ablation bite on any box rather
    than only where the ambient umask happens to cooperate.

    Ablation: revert the staging to a fixed-name `os.open(tmp, O_CREAT | O_EXCL |
    O_WRONLY, 0o600)` and this reddens alone on the spy staying empty — the
    behavioural rows below redden on the denial, not the writer."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    seen: list[tuple[str, str, str]] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*, dir, prefix, suffix):
        seen.append((dir, prefix, suffix))
        return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    monkeypatch.setattr(platform_util.tempfile, "mkstemp", spy)
    previous = os.umask(0o022)
    try:
        dest = runs.archive_run(tmp_path, run_dir)
    finally:
        os.umask(previous)

    assert seen == [
        (str(dest.parent), "20260611-100000-aaaa.tar.gz.", ".tmp")
    ]  # exactly one staging create, minted beside the destination
    if sys.platform != "win32":
        published = dest.stat().st_mode & 0o777
        assert published == 0o600, oct(published)


def test_archive_run_survives_a_stale_temp_and_leaves_it(tmp_path):
    """#591's ownership pin, in the shape the fresh-name staging gives it. A FIXED
    `O_EXCL` name turned any survivor at `<id>.tar.gz.tmp` — a temp stranded by a
    kill between create and publish, or a file planted at the guessable spelling —
    into a permanent `FileExistsError` denial of every later archive attempt,
    where the pre-#591 truncate-and-reuse completed. Staging under a per-attempt
    mkstemp name makes the survivor inert: the archive completes beside it, and
    the cleanup can only ever unlink a name this process itself minted, so the
    foreign bytes survive byte-identical.

    Ablation: revert the staging to the fixed-name `os.open(tmp, O_CREAT | O_EXCL
    | O_WRONLY, 0o600)` create and this reddens on the `FileExistsError`."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    archive_dir = tmp_path / ".bmad-loop" / "archive"
    archive_dir.mkdir(parents=True)
    stale = archive_dir / "20260611-100000-aaaa.tar.gz.tmp"
    sentinel = b"a temp stranded by a killed archiver"
    stale.write_bytes(sentinel)

    dest = runs.archive_run(tmp_path, run_dir)

    assert dest.is_file()  # the stale temp no longer denies the archive
    assert stale.read_bytes() == sentinel  # never ours, never unlinked
    assert not run_dir.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_archive_run_never_writes_through_a_planted_symlink(tmp_path):
    """#591. Before the fix `tarfile.open(tmp, "w:gz")` followed a link planted at
    the predictable temp name and truncated the victim. Staging through mkstemp
    closes that two ways at once: the per-attempt name is not guessable to plant
    at, and the create is `O_EXCL` (plus `O_NOFOLLOW` where defined), which never
    opens a name something else already holds — so a link at the old predictable
    spelling is simply bypassed, untouched, while the archive completes.

    The victim is a plain file in the project root rather than anything bmad-loop
    reads, so the test grades only the follow, not a second effect.

    Ablation: revert the staging to the pre-#591 `tarfile.open(tmp, "w:gz")` by
    name and this reddens on the victim's bytes."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do not clobber me")
    archive_dir = tmp_path / ".bmad-loop" / "archive"
    archive_dir.mkdir(parents=True)
    planted = archive_dir / "20260611-100000-aaaa.tar.gz.tmp"
    planted.symlink_to(victim)

    dest = runs.archive_run(tmp_path, run_dir)

    assert victim.read_bytes() == b"do not clobber me"  # not followed, not truncated
    assert planted.is_symlink()  # not ours, so not unlinked either
    assert dest.is_file()


def test_archive_run_fsyncs_before_the_replace(tmp_path, monkeypatch):
    """#591. This is the one writer in the atomic-write family where a missing fsync
    is DATA LOSS rather than staleness: `shutil.rmtree(run_dir)` removes the only
    other copy of the run right after the publish, so a crash with the tarball still
    in page cache destroys the run outright.

    Ordering is the assertion, not the mere presence of an fsync — an fsync after the
    replace would protect nothing that a crash between them could still lose."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    order: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def record_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(runs.os, "fsync", record_fsync)
    monkeypatch.setattr(platform_util.os, "replace", record_replace)
    runs.archive_run(tmp_path, run_dir)

    assert order == ["fsync", "replace"]


def test_archive_run_refuses_while_the_agent_session_is_live(tmp_path, monkeypatch):
    """Same backstop as delete (#419), and it runs before the tarball is written —
    a refusal must not leave a half-archived run behind for the operator to find."""
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(
        runs, "get_multiplexer", lambda: _LivenessMux(["bmad-loop-20260611-100000-aaaa"])
    )
    with pytest.raises(runs.LiveSessionError, match="still live"):
        runs.archive_run(tmp_path, run_dir)
    assert run_dir.exists()
    assert not (tmp_path / ".bmad-loop" / "archive").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "outside-the-project",
        "the-runs-root-itself",
        "a-nested-grandchild",
        "the-dot-dot-alias",
    ],
)
def test_archive_run_refuses_a_run_dir_outside_the_runs_dir(tmp_path, kind):
    """Archive carries the same `shutil.rmtree` as delete and needs the same
    containment (#480). It is checked ahead of the tarball for the reason the
    session guard is: a refusal must leave no archive directory behind.

    Graded like the delete twin — the canary, not the raise, pins the guard above
    the `rmtree`; the `..` row grades the delete twin's round-1 name clause from
    this write path too."""
    project = tmp_path / "proj"
    _make_run(project, "20260611-100000-aaaa")
    runs_root = project / ".bmad-loop" / "runs"
    target = {
        "outside-the-project": tmp_path / "outside",
        "the-runs-root-itself": runs_root,
        "a-nested-grandchild": runs_root / "20260611-100000-aaaa" / "nested",
        "the-dot-dot-alias": runs_root / "..",
    }[kind]
    target.mkdir(parents=True, exist_ok=True)
    canary = target / "canary.txt"
    canary.write_text("survives")

    with pytest.raises(platform_util.UnconfinedWriteError, match="not a run directory under"):
        runs.archive_run(project, target)
    assert canary.is_file()
    assert not (project / ".bmad-loop" / "archive").exists()  # nothing staged


def test_archive_run_refuses_a_redirected_runs_dir(tmp_path):
    """The archive twin of `test_delete_run_refuses_a_redirected_run_dir`, on the
    representative middle arm: archive would first TAR the redirect target's
    content and then `rmtree` it, so a refusal must come before either. Ablation
    (measured): drop the link walk and this reddens as DID NOT RAISE — the
    external run is consumed into a tarball and removed."""
    run_name = "20260611-100000-aaaa"
    project, run_dir, ext_run, canary = _redirected_project(tmp_path, "the-runs-dir", run_name)

    with pytest.raises(platform_util.UnconfinedWriteError, match="symlink or junction"):
        runs.archive_run(project, run_dir)
    assert canary.is_file()
    assert not (project / ".bmad-loop" / "archive").exists()  # nothing staged


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


# ------------------------------- the stop-request channel's confined write (#593)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_stop_request_refuses_a_symlinked_bmad_loop(tmp_path):
    """The escape #593 names, at this site. Refusing a link at
    `stop-request.json` covered the final component only: `.bmad-loop/`, `runs/`
    and the run's own directory were all still looked up by name, so a link
    planted at any of them aimed both the temp and the published control file
    wherever it pointed — and this is a directory a driven session can reach.

    The run dir is arranged so the unconfined write would SUCCEED: `outside/`
    already holds the `runs/r1` chain the link resolves to, so the second
    assertion measures a write that had somewhere to land rather than one that
    failed for want of a parent.

    Ablation: revert `_write_stop_request` to
    `atomic_write_text(..., follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with `stop-request.json` sitting in `outside/runs/r1/`."""
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside"
    landing = outside / "runs" / "r1"
    landing.mkdir(parents=True)
    (proj / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        runs._write_stop_request(proj / ".bmad-loop" / "runs" / "r1", "hard")

    assert list(landing.iterdir()) == []  # nothing escaped the project


def test_the_stop_request_lands_on_a_clean_tree(tmp_path):
    """The positive control for the refusal above — without it that test passes
    for a `_write_stop_request` wired to refuse everything, which is every reason
    a file could be absent from `outside/`."""
    run_dir = _make_state_run(tmp_path, "r1")

    runs._write_stop_request(run_dir, "hard")

    assert json.loads((run_dir / runs.STOP_REQUEST_FILE).read_text(encoding="utf-8"))["mode"] == (
        "hard"
    )


def test_stop_run_still_signals_when_the_lodge_is_refused_as_unconfined(tmp_path, monkeypatch):
    """`UnconfinedWriteError` subclasses `OSError` FOR THIS CALLER, and this is the
    row that says so. `stop_run` lodges the hard request and then signals, and it
    degrades rather than aborts when the lodge fails — the stop is delivered two
    ways at once, so losing one redundant channel must not cost the other. A
    refusal that escaped as a fresh exception type would abort `stop_run` BEFORE
    it ever signalled, leaving a run alive that the signal path could have killed.

    The sibling `_enospc` rows grade the degrade for a disk error; this one grades
    it for the confinement refusal specifically, which is the failure the #593
    adoption newly introduced at this call site.

    Ablation: make `UnconfinedWriteError` inherit from `Exception` instead of
    `OSError` and this fails — the refusal escapes `stop_run`'s `except OSError`
    and no signal goes out."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text("4242 100.0")

    def _unconfined(_run_dir, _mode):
        raise platform_util.UnconfinedWriteError("cannot reach the run dir without a redirect")

    monkeypatch.setattr(runs, "_write_stop_request", _unconfined)
    host = _FakeHost(alive=False, identity=100.0)
    monkeypatch.setattr(runs, "get_process_host", lambda: host)

    assert runs.stop_run(run_dir) is True
    assert host.terminated == [4242]  # the signal went out despite the refused lodge
    assert load_state(run_dir).stopped is True


def test_project_of_a_run_dir_too_shallow_refuses_rather_than_indexing_off_the_end(tmp_path):
    """The confinement root is derived by counting `RUNS_DIR` levels up from the
    run directory, so a path too shallow to HAVE that ancestor would index off the
    end of `Path.parents`. `IndexError` is not an `OSError`: it would escape
    `stop_run`'s degrade and abort the stop before it signalled — the exact
    failure the row above exists to prevent. So the guard refuses in the currency
    every caller here already handles.

    Ablation: drop the `len(parents) <= depth` check in `_project_of_run_dir` and
    this fails with `IndexError` instead of `UnconfinedWriteError`."""
    shallow = Path(tmp_path.anchor) / "one"

    with pytest.raises(platform_util.UnconfinedWriteError):
        runs._project_of_run_dir(shallow)


def test_project_of_a_real_run_dir_is_the_project_root(tmp_path):
    """The positive control for the guard above: on a run dir this module actually
    built, the derivation returns the project root rather than refusing. Without
    it, `_project_of_run_dir` could refuse everything and the row above would
    still pass."""
    run_dir = _make_state_run(tmp_path, "r1")

    assert runs._project_of_run_dir(run_dir) == tmp_path


# ---- task_spec_root: the root must be able to CONFINE the path task_spec_path returns


def test_task_spec_root_yields_the_project_when_the_worktree_cannot_confine_the_spec(tmp_path):
    """An absolute `spec_file` beside a set `worktree_path` is the OUT-OF-MOUNT shape.

    `model._serialized_worktree_path` keeps a path verbatim exactly when
    `relative_to(worktree_path)` raises, so this pair means the spec is lexically
    outside the mount. `task_spec_path` passes an absolute path through untouched, so
    answering the worktree here names a root that can NEVER contain the anchored path:
    `devcontract._atomic_write_spec` gates on the same lexical `is_relative_to` and would
    silently take the plain no-follow arm, losing #593's O_NOFOLLOW walk — and so would
    `_restore_rearmed_spec`, the re-arm's undo, which now selects its writer the same
    lexical way rather than calling `atomic_write_bytes_confined` directly. All four
    degrade together, which is the point: the undo that once RAISED `UnconfinedWriteError`
    here, turning a recoverable re-arm abort into a lost one, is the asymmetry that
    parity removed.

    The project is not guaranteed to contain it either; where nothing does, the write
    lands on the arm it already took. That is not unconditional, and the exception is
    graded by `test_task_spec_root_refuses_a_spec_the_project_cannot_reach` rather than
    asserted here — a lexically-contained spec reached through a symlinked component
    moves from a succeeding plain write to a refused confined one.

    Ablation: revert the body to `Path(task.worktree_path or state.project)` and this
    reddens — the root is the worktree, which cannot confine the spec.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    spec = tmp_path / "_bmad-output" / "specs" / "6-4.md"  # in the project, not the mount
    run = escalated_run(tmp_path, "r1", spec_file=str(spec), worktree_path=str(wt))

    assert runs.task_spec_root(run.task, run.state) == tmp_path
    # the anchored path is confinable by the root, which is the whole point
    assert runs.task_spec_path(run.task, run.state).is_relative_to(
        runs.task_spec_root(run.task, run.state)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_task_spec_root_refuses_a_spec_the_project_cannot_reach(tmp_path):
    """The out-of-mount arm's one REGRESSION, pinned so it is graded rather than assumed.

    `task_spec_root`'s docstring used to claim this arm "only ever trades a skipped or
    refused confined write for a taken one". That is false for a spec which is lexically
    under the project but reached THROUGH a symlinked component: `_atomic_write_spec`
    selects its arm on the lexical `is_relative_to` — which passes — and the confined
    arm it selects then walks the components below the root and refuses the redirect. So
    a write that previously took the plain no-follow arm and SUCCEEDED now raises
    `UnconfinedWriteError`. Graded here through `devcontract.reset_spec_status`, one of
    the three `_atomic_write_spec` writers, so the arm SELECTION is what reaches the
    refusal rather than being assumed. `rearm_escalation` converting it to `RearmError`
    is its own arm and is graded by the re-arm rows, not by this one.

    Kept as behavior rather than fixed, because the fix is worse: gating the arm on
    `path_is_confined` makes the root depend on filesystem state (that predicate answers
    False for a component it cannot probe, so an absent parent directory would anchor on
    the worktree and the same spec would anchor on the project once it existed). A
    confine root that moves under a `mkdir` is not a definition. This row exists so the
    trade is visible and a future reader does not rediscover it as a surprise.

    Ablation: make `task_spec_root` return the worktree for the out-of-mount shape and
    this reddens on the root assertion — and the refusal disappears with it, because the
    writer would take the plain arm instead.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "_bmad-output").mkdir()
    link = tmp_path / "_bmad-output" / "specs"
    link.symlink_to(real, target_is_directory=True)  # a REDIRECT below the project root
    spec = link / "6-4.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run = escalated_run(tmp_path, "r1", spec_file=str(spec), worktree_path=str(wt))

    root = runs.task_spec_root(run.task, run.state)
    assert root == tmp_path  # lexically contained, so the confined arm is selected
    assert spec.is_relative_to(root)
    # Driven through `_atomic_write_spec`, NOT through the primitive: calling
    # `atomic_write_bytes_confined` directly proves only that the primitive refuses a
    # symlinked component, which was never in doubt. The claim is that the WRITER's
    # lexical arm selection reaches that refusal for this root — so the real writer has
    # to be the thing that raises.
    from bmad_loop import devcontract

    with pytest.raises(platform_util.UnconfinedWriteError):
        devcontract.reset_spec_status(spec, "draft", confine_root=root)
    # and the spec is untouched by the aborted write
    assert spec.read_text(encoding="utf-8") == "---\nstatus: blocked\n---\n"


def test_task_spec_path_refuses_an_empty_spec_file(tmp_path):
    """`Path("")` is `.`, so an empty `spec_file` would answer the ROOT DIRECTORY.

    The helper is public now, so the precondition is enforced instead of documented: a
    caller that skips the guard every current call site has gets an exception rather
    than a write target pointing at the tree root.

    Ablation: restore `raw = Path(task.spec_file or "")` and drop the raise — this
    reddens, and `task_spec_path` answers the worktree itself.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    run = escalated_run(tmp_path, "r1", spec_file="", worktree_path=str(wt))

    with pytest.raises(ValueError, match="non-empty"):
        runs.task_spec_path(run.task, run.state)
    # the ROOT still answers, because naming a tree needs no spec
    assert runs.task_spec_root(run.task, run.state) == wt


def test_spec_reaches_the_redrive_is_false_for_a_worktree_local_spec(tmp_path):
    """The verdict `build_context` publishes so the resolve agent is not lied to.

    A worktree-local spec is destroyed with the mount by `engine._finish_inflight`
    before the re-drive reads anything, so an edit to it succeeds and then vanishes.
    `rearm_escalation` already journals `rearm-spec-write-unreachable` on this same
    verdict; promoting it is what lets the context carry it too.

    The two no-flip rows only. `isolated_redrive` agrees with the recorded mount on
    both, which is what makes them the rows a `task.worktree_path` proxy also passed —
    the rows that grade the SOURCE are in
    `test_spec_reaches_the_redrive_reads_live_policy_not_the_recorded_mount`.

    Ablation: return a bare `True` from `spec_reaches_the_redrive` and this reddens on
    the isolated leg.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    isolated = escalated_run(tmp_path, "r1", spec_file="specs/6-4.md", worktree_path=str(wt))
    assert (
        runs.spec_reaches_the_redrive(isolated.task, isolated.state, isolated_redrive=True) is False
    )

    plain = escalated_run(tmp_path, "r2", spec_file=str(tmp_path / "specs" / "6-4.md"))
    assert runs.spec_reaches_the_redrive(plain.task, plain.state, isolated_redrive=False) is True


def test_spec_reaches_the_redrive_reads_live_policy_not_the_recorded_mount(tmp_path):
    """The two rows where the recorded mount and the live policy DISAGREE — which is
    the whole reason this takes a parameter instead of reading `task.worktree_path`.

    `scm.isolation` is re-read at every resume and a mid-run change is journalled,
    never refused (`engine._finish_inflight`), so an operator who edits policy.toml
    while a story sits escalated makes the recorded mount describe the attempt that
    RAN and nothing about the re-drive that WILL run. `bmad-loop resolve` builds
    context.json in a separate process BEFORE that resume, so no resume-time
    bookkeeping on the recorded mount can reach it: the fact has to arrive as an
    argument or not at all.

    - `worktree` -> `none`: the mount is still recorded and the writes still land in
      it (`task_spec_path` anchors there), but `_run_story` now re-runs the story in
      the main checkout, which never reads that tree. False.
    - `none` -> `worktree`: no mount was ever recorded, and the fresh one is cut from
      git — so the working-tree edit the agent was sent to make is not in it. False,
      and this is the row the `task.worktree_path` proxy answered TRUE for: the agent
      was told its edit was safe while it silently vanished.

    Ablation: restore `not task.worktree_path or _spec_is_shared_with_the_redrive(...)`
    as the body and the `none -> worktree` row reddens (True for a doomed edit). The
    `worktree -> none` row does NOT redden under that ablation — it agreed by accident,
    which is why `test_redrive_base_ref_reads_live_policy_not_the_recorded_mount`
    carries that direction.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"

    flipped_off = escalated_run(tmp_path, "r1", spec_file="specs/6-4.md", worktree_path=str(wt))
    assert (
        runs.spec_reaches_the_redrive(flipped_off.task, flipped_off.state, isolated_redrive=False)
        is False
    )

    flipped_on = escalated_run(tmp_path, "r2", spec_file="specs/6-4.md")
    assert not flipped_on.task.worktree_path  # the premise: nothing recorded to gate on
    assert (
        runs.spec_reaches_the_redrive(flipped_on.task, flipped_on.state, isolated_redrive=True)
        is False
    )


def test_spec_reaches_the_redrive_in_place_measures_the_mount_not_its_presence(tmp_path):
    """The in-place arm asks WHERE the edit lands, not WHETHER a mount was recorded.

    After a `worktree` -> `none` flip the recorded mount is still set on every row here,
    so `bool(task.worktree_path)` cannot tell them apart — but the re-drive reads the
    main checkout's working tree, and only a spec inside the mount is out of its reach:

    - relative: `_serialized_worktree_path` relativizes exactly when the spec sits under
      the mount, so a relative spelling IS inside it, by construction and with no probe.
    - absolute, under the mount: the same file spelled the other way. Also unreachable.
    - absolute, outside the mount but under the PROJECT: the main checkout's own copy —
      which is precisely what an in-place re-drive reads. Reachable, and the row that
      separates this from the isolated arm, where the same shape is unreachable because
      a fresh worktree measures it against worktree-local roots.

    Ablation: return `bool(task.worktree_path)` from `_spec_is_inside_the_mount` and the
    third row reddens — a spec the re-drive reads is reported as doomed.
    """
    mount = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    mount.mkdir(parents=True)

    relative = escalated_run(tmp_path, "r1", spec_file="specs/6-4.md", worktree_path=str(mount))
    assert (
        runs.spec_reaches_the_redrive(relative.task, relative.state, isolated_redrive=False)
        is False
    )

    inside = escalated_run(
        tmp_path, "r2", spec_file=str(mount / "specs" / "6-4.md"), worktree_path=str(mount)
    )
    assert runs.spec_reaches_the_redrive(inside.task, inside.state, isolated_redrive=False) is False

    outside = escalated_run(
        tmp_path, "r3", spec_file=str(tmp_path / "specs" / "6-4.md"), worktree_path=str(mount)
    )
    assert (
        runs.spec_reaches_the_redrive(outside.task, outside.state, isolated_redrive=False) is True
    )
    # ...and the SAME task under isolation is unreachable: the fresh mount measures the
    # main checkout's copy against worktree-local roots and rejects it
    assert (
        runs.spec_reaches_the_redrive(outside.task, outside.state, isolated_redrive=True) is False
    )


def test_spec_reaches_the_redrive_keeps_a_shared_external_spec_without_a_mount(tmp_path):
    """`_spec_is_shared_with_the_redrive` had to lose its `not task.worktree_path`
    early return, or generalizing the isolated arm would have traded one wrong answer
    for another.

    An artifact dir configured outside the project tree is SHARED across checkouts
    (`ProjectPaths.rebased` leaves it where it is), so a spec that lands there is one
    file every worktree sees — reachable whether or not a mount is recorded. Without
    the generalization the `none -> worktree` flip above would warn on every such run:
    wrong-but-loud rather than silent, but still a doom notice on a spec that is fine,
    and the operator trained to scroll past it is the failure the record's narrowing
    exists to avoid.

    Ablation: restore `if not task.worktree_path or not raw.is_absolute(): return False`
    and the no-mount leg reddens.
    """
    shared = tmp_path / "outside" / "artifacts" / "6-4.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    no_mount = escalated_run(project, "r1", spec_file=str(shared))
    assert (
        runs.spec_reaches_the_redrive(no_mount.task, no_mount.state, isolated_redrive=True) is True
    )

    wt = project / ".bmad-loop" / "runs" / "r2" / "worktrees" / "1"
    mounted = escalated_run(project, "r2", spec_file=str(shared), worktree_path=str(wt))
    assert runs.spec_reaches_the_redrive(mounted.task, mounted.state, isolated_redrive=True) is True


def test_redrive_base_ref_reads_live_policy_not_the_recorded_mount(tmp_path):
    """Where a correction has to be committed to be read — answered from the mode the
    re-drive will RUN in, not from the mount the escalated attempt left behind.

    The pinned `target_branch` is only the right answer when the re-drive mounts:
    `workspace.open_unit_workspace` cuts the replacement worktree from it. An in-place
    re-drive reads the main checkout's working ref instead, and naming a branch there
    sends the resolve session to commit where this run never looks — the reported
    defect, and unreachable by any resume-time fix because `bmad-loop resolve` computes
    this in another process first.

    Four rows: both no-flip rows, and both flips. `task` is gone from the signature, so
    the recorded mount cannot influence any of them — the flip rows are what prove it.

    Ablation: restore `if task.worktree_path and state.target_branch` (re-adding the
    parameter) and BOTH flip rows redden — `worktree -> none` answers the pinned branch
    for a re-drive reading `HEAD`, and `none -> worktree` answers `HEAD` for one that
    reads the branch.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    mounted = escalated_run(tmp_path, "r1", spec_file="specs/6-4.md", worktree_path=str(wt))
    mounted.state.target_branch = "feat/the-pinned-one"

    # no flip
    assert runs.redrive_base_ref(mounted.state, isolated_redrive=True) == "feat/the-pinned-one"
    # `worktree` -> `none`: the mount is still recorded, and it must not decide this
    assert runs.redrive_base_ref(mounted.state, isolated_redrive=False) == "HEAD"

    unmounted = escalated_run(tmp_path, "r2", spec_file="specs/6-4.md")
    unmounted.state.target_branch = "feat/the-pinned-one"
    assert not unmounted.task.worktree_path  # the premise: nothing recorded to gate on

    # no flip
    assert runs.redrive_base_ref(unmounted.state, isolated_redrive=False) == "HEAD"
    # `none` -> `worktree`: the re-drive mounts from the pin, with no mount on record
    assert runs.redrive_base_ref(unmounted.state, isolated_redrive=True) == "feat/the-pinned-one"


def test_redrive_base_ref_degrades_to_head_without_a_pinned_target(tmp_path):
    """The migration shape, kept from the version that read `task.worktree_path`:
    `ensure_target_branch` pins the field before any worktree mounts, so an empty
    `target_branch` beside an isolated re-drive is a state.json predating the field —
    a MISSING value, not a divergent one. It degrades to exactly the ref it read
    before, rather than to `""`, which would hold the resume on a per-configuration
    constant.

    Ablation: drop the `and state.target_branch` conjunct and this reddens with `""`.
    """
    run = escalated_run(tmp_path, "r1", spec_file="specs/6-4.md")
    assert run.state.target_branch == ""
    assert runs.redrive_base_ref(run.state, isolated_redrive=True) == "HEAD"


def test_task_spec_root_stays_on_the_worktree_for_specs_it_can_confine(tmp_path):
    """The guard against an over-broad fix: only the out-of-mount shape moves.

    Two shapes must keep answering the worktree — the RELATIVE spec (the common
    isolated case, which `task_spec_path` resolves against this very root), and an
    ABSOLUTE spec that does sit under the mount. A fix that returned the project
    whenever `worktree_path` was set would re-break the defect the anchor exists to
    fix, sending the isolated read and write back to the main checkout's twin.

    Ablation: drop the `raw.is_absolute() and` conjunct so the arm keys on containment
    alone, and the relative row reddens (`Path("_bmad-output/...")` is not relative to
    the mount); return `Path(state.project)` whenever a worktree is set and both redden.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"

    run = escalated_run(
        tmp_path, "r1", spec_file="_bmad-output/specs/6-4.md", worktree_path=str(wt)
    )
    assert runs.task_spec_root(run.task, run.state) == wt  # relative: the common case

    inside = wt / "_bmad-output" / "specs" / "6-4.md"
    run = escalated_run(tmp_path, "r2", spec_file=str(inside), worktree_path=str(wt))
    assert runs.task_spec_root(run.task, run.state) == wt  # absolute, but under the mount


def test_task_spec_root_without_a_worktree_is_the_project(tmp_path):
    """The no-worktree fallback is untouched by the confinement arm: an absolute spec
    that the project cannot confine still answers the project, because there is no
    second candidate to choose and the pre-existing behavior is the contract.

    Ablation: return `Path(state.project)` only when the project confines the spec and
    this reddens — an out-of-project spec has nowhere else to go."""
    run = escalated_run(tmp_path, "r1", spec_file="/elsewhere/6-4.md")
    assert runs.task_spec_root(run.task, run.state) == tmp_path


def test_task_stories_root_stays_on_the_mount_for_an_out_of_mount_spec(tmp_path):
    """The ONE shape where `task_stories_root` and `task_spec_root` disagree — which is
    the entire reason the second function exists.

    `task_spec_root` answers "which tree can CONFINE a write to `task.spec_file`", so its
    out-of-mount arm falls back to the project precisely so a `confine_root` can never
    fail to contain the anchored path. The stories FOLDER is a different question: it is
    located from the workspace root by `state.spec_folder`, and a task's spec being
    elsewhere says nothing about where its manifest lives. `stories_engine._stories_folder`
    answers the mount for this task, so borrowing the confinement answer made one surface
    describe two trees.

    Every other row builds the isolated shape with a RELATIVE `spec_file`, where the two
    resolvers agree by construction and cannot tell each other apart — which is why
    collapsing `task_stories_root` back into `task_spec_root` left the whole suite green.

    Ablation: make `task_stories_root` delegate to `task_spec_root` and this reddens on
    the first assertion; the second pins that the two genuinely diverge here, so a future
    change that made them agree could not satisfy both."""
    wt = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    # OS-absolute, not merely rooted. `task_spec_root`'s out-of-mount arm gates on
    # `Path.is_absolute()`, and on Windows "/elsewhere/6-4.md" is DRIVE-relative — so the
    # arm never fired there, the fallback returned the worktree, and the row graded the
    # divergence it exists to pin on POSIX only. What the arm needs is a spec outside the
    # MOUNT, not outside the project, so anchoring on `tmp_path` keeps the shape while
    # being absolute on every OS.
    outside = tmp_path / "elsewhere" / "6-4.md"
    # the mount has to EXIST: a live isolated unit's does, and `task_stories_root`
    # degrades to the project for one that is gone (see the row below), so a
    # never-created path would grade that fallback instead of this divergence.
    wt.mkdir(parents=True, exist_ok=True)
    run = escalated_run(tmp_path, "r1", spec_file=str(outside), worktree_path=str(wt))

    assert runs.task_stories_root(run.task, run.state) == wt
    assert runs.task_spec_root(run.task, run.state) == tmp_path  # deliberately different


def test_task_stories_root_without_a_worktree_is_the_project(tmp_path):
    """The no-worktree and no-task arms, which the two call sites rely on rather than
    re-spelling the fallback."""
    run = escalated_run(tmp_path, "r1", spec_file="epic-1/stories/6-4.md")
    assert runs.task_stories_root(run.task, run.state) == tmp_path
    assert runs.task_stories_root(None, run.state) == tmp_path


def test_task_stories_root_falls_back_when_the_mount_is_gone(tmp_path):
    """A terminal task keeps naming the worktree its own teardown removed.

    `worktree_path` is cleared at exactly ONE site in the engine — the restart
    discard — so successful integration retires a task with the field still set while
    the mount is deleted. The `done_checkpoint` pause is raised in that window and the
    TUI reads this root for the checkpoint card's title and description, so trusting
    the stale field looked for `stories.yaml` under a deleted directory and dropped
    the committed story's manifest, which by then is merged into the project.

    Ablation: drop the `is_dir()` guard from `task_stories_root` and this reddens with
    the deleted mount; the sibling row above (whose mount exists) stays green, so the
    guard cannot be satisfied by collapsing the function to the project.
    """
    gone = tmp_path / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"  # never created
    run = escalated_run(tmp_path, "r1", spec_file="epic-1/stories/6-4.md", worktree_path=str(gone))

    assert not gone.exists()
    assert runs.task_stories_root(run.task, run.state) == tmp_path


# ------------------------------------------------- psmux registry root (#537)


def test_mux_registry_root_lives_under_the_projects_state_subtree(tmp_path):
    root = runs.mux_registry_root(tmp_path)
    assert root == runs.project_state_root(tmp_path) / runs.MUX_REGISTRY_DIR
    assert root.parent.name == runs.project_tag(tmp_path)
    assert root.is_absolute()


def test_mux_registry_root_can_never_collide_with_a_run(tmp_path):
    """`--run-id` is caller-supplied, so a run whose id spelled the registry's
    directory name would key its state dir ONTO the registry — the run's control
    plane and every live server's addressing files in one directory, each side
    deleting the other's entries. The leading underscore is what makes that
    unreachable: RUN_ID_RE requires an alphanumeric first character. Ablate it
    (name the directory `mux`) and this fails."""
    assert not runs.is_valid_run_id(runs.MUX_REGISTRY_DIR)
    assert runs.mux_registry_root(tmp_path) != runs.state_dir_for(tmp_path, "mux")


def test_mux_registry_root_agrees_across_two_spellings_of_one_project(tmp_path):
    """The whole cross-process contract: two processes reaching one project by
    different paths must land on the SAME registry, or each reads the other's
    live sessions as gone. Guaranteed by project_tag resolving first, which is
    why the root reuses it rather than deriving a second identity."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    detoured = tmp_path / "a" / ".." / "a" / "b"
    assert runs.mux_registry_root(nested) == runs.mux_registry_root(detoured)


def test_mux_registry_root_separates_two_projects(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    assert runs.mux_registry_root(one) != runs.mux_registry_root(two)


def test_export_psmux_registry_root_sets_the_derived_root(tmp_path, monkeypatch):
    monkeypatch.delenv(runs.PSMUX_DATA_DIR, raising=False)
    expected = str(runs.mux_registry_root(tmp_path))
    assert runs.export_psmux_registry_root(tmp_path) == expected
    assert os.environ[runs.PSMUX_DATA_DIR] == expected


def test_export_psmux_registry_root_overrides_an_operators_own(tmp_path, monkeypatch):
    """The rule, and the absence of an exception to it is the point: the root is
    derived from (project, state root), full stop, so two bmad-loop processes
    given one project cannot land in different registries.

    Honouring an ambient value was tried and is the thing that was cut. It makes
    the registry a function of the launch *shell* — a TUI from the Start menu
    derives while a run from a dev shell whose profile exports a root honours it,
    two registries on one machine — and no rule can be right for both operators,
    because the process that finds a root in its environment cannot tell one
    typed in this shell alone from one the profile exports into every shell.

    Ablate the unconditional export (restore an if-unset guard) and this fails."""
    theirs = str(tmp_path / "theirs")
    derived = str(runs.mux_registry_root(tmp_path))
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, theirs)

    assert runs.export_psmux_registry_root(tmp_path) == derived
    assert os.environ[runs.PSMUX_DATA_DIR] == derived


@pytest.mark.parametrize("ambient", ["", "relative/root", ".", "/an/absolute/one"])
def test_export_psmux_registry_root_overrides_any_ambient_spelling(tmp_path, monkeypatch, ambient):
    """Including the ones psmux would panic on. An earlier rule left a relative or
    empty value untouched so as not to countermand something the operator typed —
    which, now that nothing ambient is honoured, only preserved a value that makes
    every verb fail. Replacing it is strictly better: the derived root works."""
    derived = str(runs.mux_registry_root(tmp_path))
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, ambient)

    assert runs.export_psmux_registry_root(tmp_path) == derived
    assert os.environ[runs.PSMUX_DATA_DIR] == derived


def test_export_psmux_registry_root_is_indifferent_to_being_inside_a_pane(tmp_path, monkeypatch):
    """A pane child derives exactly what a clean process derives — the convergence
    four rounds of inherited-token designs were trying to buy, and which having no
    token buys outright.

    psmux hands a pane child the server's whole environment (measured on 3.3.8),
    so `bmad-loop --project B` from a pane of project A's session arrives carrying
    A's root; it must still get B's. And it must get the same answer whether or
    not it is in a pane at all, since pane-ness says nothing about which registry
    a project's sessions belong in."""
    a_root = str(tmp_path / "registry-A")
    project_b = tmp_path / "B"
    project_b.mkdir()
    derived = str(runs.mux_registry_root(project_b))

    monkeypatch.setenv(runs.PSMUX_DATA_DIR, a_root)
    monkeypatch.setenv("TMUX", "/tmp/psmux-1000/default,123,0")  # inside a pane
    assert runs.export_psmux_registry_root(project_b) == derived

    monkeypatch.delenv("TMUX", raising=False)  # and outside one
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, a_root)
    assert runs.export_psmux_registry_root(project_b) == derived


def test_export_psmux_registry_root_converges_a_pane_child_that_moves_the_state_root(
    tmp_path, monkeypatch
):
    """The scenario every round of review found a way to break, in its final form:
    whatever a pane child concludes is what a clean process under the same
    conditions concludes — for a pinned root and a derived one alike, because
    there is no longer a difference between them.

    The registry lives under the state root, so a child running under a different
    one must re-derive; keeping the parent's would put it where nothing else
    looks."""
    pinned = str(tmp_path / "pinned")
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S1"))
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, pinned)
    under_s1 = runs.export_psmux_registry_root(tmp_path)

    # the pane child, carrying S1's settled root, now under S2
    monkeypatch.setenv("TMUX", "/tmp/psmux-1000/default,123,0")
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S2"))
    child = runs.export_psmux_registry_root(tmp_path)

    # a clean process under S2: no pane, no inherited root
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv(runs.PSMUX_DATA_DIR, raising=False)
    clean = runs.export_psmux_registry_root(tmp_path)

    # ...and one under S2 whose PROFILE exports the pin into every shell
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, pinned)
    clean_pinned = runs.export_psmux_registry_root(tmp_path)

    assert child != under_s1 and child != pinned
    assert child == clean == clean_pinned == str(runs.mux_registry_root(tmp_path))


def test_pinned_state_env_resolves_rather_than_forwards(tmp_path, monkeypatch):
    """What travels is the answer this process reached, not the override it was
    handed. Forwarding only when the operator set something leaves the common case
    — no override at all — with nothing to pass, and that is exactly the case
    `PSMUX_BARE_ENV=1` also breaks: its allowlist drops `LOCALAPPDATA` and
    `XDG_STATE_HOME` too, so a child there cannot recompute the default either.

    Ablate the resolve (return the raw environment value, or `{}` when unset) and
    the second half fails."""
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S"))
    assert runs.pinned_state_env() == {envvars.STATE_DIR: str(runs.state_root())}

    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    assert runs.pinned_state_env() == {envvars.STATE_DIR: str(runs.state_root())}


def test_pinned_state_env_degrades_on_an_underivable_state_root(monkeypatch):
    """`{}` rather than a raise: a child told nothing derives its own answer and
    fails on the same broken environment with its own message, which beats a
    launcher that cannot report anything at all.

    Ablate the `except StateRootError` and this raises."""

    def boom():
        raise runs.StateRootError("no state root")

    monkeypatch.setattr(runs, "state_root", boom)
    assert runs.pinned_state_env() == {}


class _NamespaceStub:
    """Duck-typed mux answering only the namespace question — all
    ctl_session_for consults."""

    def __init__(self, namespaced):
        self._namespaced = namespaced

    def has_registry_namespace(self):
        return self._namespaced


def test_ctl_session_for_is_fixed_without_a_registry_namespace(tmp_path):
    """tmux keeps the machine-shared `bmad-loop-ctl` byte-identically — the
    shared session is correct there and every pinned tmux argv depends on it.

    Ablate the `has_registry_namespace()` arm (suffix always) and this fails."""
    assert runs.ctl_session_for(tmp_path, _NamespaceStub(False)) == runs.CTL_SESSION


def test_ctl_session_for_carries_the_registry_identity(tmp_path, monkeypatch):
    """On a namespacing transport the name is per REGISTRY, because psmux's
    duplicate-server mutex is keyed on the session name alone, machine-wide
    (`Local\\psmux-session-{name}`, source-read at v3.3.8): a fixed name lets
    only one registry on the machine hold a control session, and the second
    project's create is rejected as a duplicate server (measured: rc 1). Both
    axes of the registry key must move the name — a project-only tag would
    recreate the collision for one project under two state roots.

    Ablate the suffix (return the fixed name always) and every assertion but
    the stability one fails; key the suffix on `project_tag` alone and the
    state-root case fails."""
    mux = _NamespaceStub(True)
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()

    name_a = runs.ctl_session_for(a, mux)
    name_b = runs.ctl_session_for(b, mux)
    assert name_a.startswith(runs.CTL_SESSION + "-") and name_b.startswith(runs.CTL_SESSION + "-")
    assert name_a != name_b  # two projects, two registries, two names
    assert runs.ctl_session_for(a, mux) == name_a  # stable per registry

    # ...and the OTHER axis of the registry key: same project, moved state root
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S2"))
    assert runs.ctl_session_for(a, mux) != name_a


def test_ctl_session_for_converges_across_spellings_of_one_root(tmp_path, monkeypatch):
    """Two spellings of one state root reach ONE physical registry (the OS
    resolves both to the same files; psmux keeps the spelling only while
    constructing those paths — src/paths.rs:79, source-read at v3.3.8), so
    they must mint ONE control-session name: an as-spelled digest gave the
    same registry two ctl sessions, each blind to the other's parked windows.

    Ablate the `.resolve()` in ctl_session_for and this fails."""
    mux = _NamespaceStub(True)
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "alias").mkdir()
    plain = str(tmp_path / "state")
    detour = str(tmp_path / "alias" / ".." / "state")
    assert plain != detour  # the premise: two spellings, not one

    monkeypatch.setenv(envvars.STATE_DIR, plain)
    name_plain = runs.ctl_session_for(project, mux)
    monkeypatch.setenv(envvars.STATE_DIR, detour)
    name_detour = runs.ctl_session_for(project, mux)

    assert name_plain.startswith(runs.CTL_SESSION + "-")
    assert name_plain == name_detour


def test_ctl_session_for_degrades_to_the_fixed_name(tmp_path, monkeypatch):
    """The underivable arm runs on the transport's shared default registry —
    the one place psmux is in the tmux-shaped world where a shared session
    scoped by window tags is correct, and where a pre-#537 legacy ctl session
    under the fixed name may exist to be reused rather than collided with."""
    monkeypatch.setenv(envvars.STATE_DIR, "relative-root")
    assert runs.ctl_session_for(tmp_path, _NamespaceStub(True)) == runs.CTL_SESSION


def test_is_ctl_session_name_shapes():
    """Exactly the shapes ctl_session_for can mint — the fixed name and a
    16-hex suffix. An arbitrary suffix is NOT a control session: it is the
    agent session of a run an older release accepted (`--run-id ctl-foo`),
    and reading it as a control session made it unreachable by stop and the
    prune both. Ablate the 16-hex narrowing (back to any `-` suffix) and the
    arbitrary-suffix refusals fail."""
    assert runs.is_ctl_session_name(runs.CTL_SESSION)
    assert runs.is_ctl_session_name(runs.CTL_SESSION + "-0123456789abcdef")
    assert not runs.is_ctl_session_name("bmad-loop-ctl2")  # no `-` boundary
    assert not runs.is_ctl_session_name("bmad-loop-20260825-000000-run1")
    assert not runs.is_ctl_session_name("")
    # historical agent-session shapes, not control sessions:
    assert not runs.is_ctl_session_name("bmad-loop-ctl-foo")
    assert not runs.is_ctl_session_name("bmad-loop-ctl-0123456789abcde")  # 15 hex
    assert not runs.is_ctl_session_name("bmad-loop-ctl-0123456789abcdeff")  # 17 hex
    assert not runs.is_ctl_session_name(
        "bmad-loop-ctl-0123456789ABCDEF"
    )  # case: exact-name predicate


def test_agent_run_id_never_reads_a_ctl_session_as_a_run():
    """`bmad-loop-ctl-<16hex>` strips to `ctl-<16hex>`, which RUN_ID_RE admits —
    so without the control-alias exclusion the prune partition would treat
    this project's own control session as an untagged agent session, making
    the prune a kill path into the control plane. Ablate the
    `run_id_aliases_control_session` check in `_agent_run_id` and this
    fails."""
    assert runs._agent_run_id(runs.CTL_SESSION) is None
    assert runs._agent_run_id(runs.CTL_SESSION + "-0123456789abcdef") is None
    assert runs._agent_run_id("bmad-loop-20260825-000000-run1") == "20260825-000000-run1"


def test_agent_run_id_reads_a_historical_ctl_prefixed_run():
    """The inverse boundary: `bmad-loop-ctl-foo` is NOT a control session — it
    is the agent session of a run an older release accepted as `--run-id
    ctl-foo` — and the parse must return its id or the sweep can never reach
    the session (the mint refuses the shape, so nothing new can collide).
    Ablate the parse back to `is_valid_run_id` (the mint's broad reservation)
    and this fails."""
    assert runs._agent_run_id("bmad-loop-ctl-foo") == "ctl-foo"
    assert not runs.is_valid_run_id("ctl-foo")  # ...while the mint still refuses it


@pytest.mark.parametrize(
    "bad",
    [
        "ctl",
        "ctl-0123456789abcdef",
        "ctl-x",
        "ctl-run-1",
        # case variants: psmux resolves session names through a case-folding
        # filesystem, so `bmad-loop-CTL-<digest>` addresses — and kills — the
        # lowercase control session (measured on 3.3.8)
        "CTL",
        "Ctl-0123456789abcdef",
        "cTl-x",
    ],
)
def test_run_id_of_the_ctl_shape_is_refused(bad):
    """The control-session namespace is reserved: `session_name("ctl")` IS the
    fixed control session, and `session_name("ctl-<16hex>")` can equal a
    per-registry one exactly — the adapter would adopt the live control
    session as the run's agent session and the run's teardown would kill it.
    Case-insensitively, because the adoption and the kill both go through the
    multiplexer's case-folding name resolution on Windows.

    Ablate the `is_reserved_run_id(value)` clause and every case fails;
    ablate only the `.lower()` inside `is_reserved_run_id` and the case
    variants fail."""
    assert not runs.is_valid_run_id(bad)
    # every refusal here is exactly the reservation — the overlap with the
    # control-session namespace under the platform's worst-case name folding
    assert runs.is_reserved_run_id(bad)


@pytest.mark.parametrize("near_miss", ["ctl2", "CTL2", "ctlfoo", "ctl_x", "controller-1"])
def test_run_id_reservation_stops_at_the_ctl_shape(near_miss):
    """The inverse sweep: ids that merely start with `ctl` stay valid — the
    reservation is the predicate's own boundary (`ctl`, `ctl-…`), not a
    prefix ban, and the case fold widens no further than the shape."""
    assert runs.is_valid_run_id(near_miss)


def test_ctl_session_for_folds_case_only_where_the_filesystem_does(tmp_path, monkeypatch):
    """Two case spellings of a NOT-YET-created state root: `resolve()` can
    return stored case only for a path that exists, and the registry root
    usually does not exist at name time — so the digest folds case itself,
    via `os.path.normcase`. On Windows both spellings land in ONE physical
    registry (the `.port` files open case-insensitively), so they must mint
    one name; on POSIX case is significant — two case spellings ARE two
    registries and must keep two names.

    Ablate the normcase and the win32 arm fails; replace it with an
    unconditional `str.lower` and the POSIX arm fails."""
    mux = _NamespaceStub(True)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "MiXeD-State"))  # never created
    name_mixed = runs.ctl_session_for(project, mux)
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "mixed-state"))  # never created
    name_lower = runs.ctl_session_for(project, mux)
    if sys.platform == "win32":
        assert name_mixed == name_lower
    else:
        assert name_mixed != name_lower


def test_kill_session_never_addresses_a_control_session_alias():
    """An id that aliases a control session (`ctl`, `ctl-<16 hex>`, folded)
    can only address the control plane, so its kill is skipped — reachable
    only through run dirs an older release persisted, replayed by `stop`,
    `delete` and resume's stale-session sweep. A historical `ctl-foo` id is
    NOT an alias: its `bmad-loop-ctl-foo` session is a genuine agent session,
    distinct and exactly addressable, and skipping it stranded the session
    (the prune already refuses the id, so nothing else could reach it).

    Ablate the guard and the alias cases fail; widen the alias test back to
    every ctl-* shape and the `ctl-foo` case fails."""
    killed = []

    class _Recorder:
        def kill_session(self, name):
            killed.append(name)

    runs.kill_session("ctl", _Recorder())
    runs.kill_session("ctl-0123456789abcdef", _Recorder())
    runs.kill_session("CTL-0123456789ABCDEF", _Recorder())
    assert killed == []
    runs.kill_session("ctl-foo", _Recorder())  # historical agent session: killable
    runs.kill_session("20260826-000000-run1", _Recorder())
    assert killed == ["bmad-loop-ctl-foo", "bmad-loop-20260826-000000-run1"]


class _LivenessMux:
    """Duck-typed mux for live_session_may_be_ours: transport-controlled name
    key (`fold`), a listing that raises when `unanswerable` (the out-of-tree
    seam shape the guard's degrade arm exists for), foldable tags.
    `unavailable` drives `available()` only — the guard must never consult
    availability, which is exactly what the degraded-backend test grades."""

    def __init__(self, sessions, tags=None, fold=False, unanswerable=False, unavailable=False):
        self._sessions = sessions
        self._tags = tags or {}
        self._fold = fold
        self._unanswerable = unanswerable
        self._unavailable = unavailable

    def available(self):
        return not self._unavailable

    def session_name_key(self, name):
        return name.lower() if self._fold else name

    def has_registry_namespace(self):
        return False  # tmux-shaped: ctl_session_for answers the fixed name

    def list_sessions(self):
        if self._unanswerable:
            raise MultiplexerError("simulated transport failure")
        return list(self._sessions)

    def session_options(self, option):
        if self._unanswerable:
            raise MultiplexerError("simulated transport failure")
        return dict(self._tags)


def test_live_session_may_be_ours_compares_names_the_transports_way(tmp_path, monkeypatch):
    """Two layers of the same rule. The discount answers the INSTANCE question
    — "is this name the control session this process addresses" — never the
    shape question (round-17: the shape discount destroyed a tmux run dir
    under a live `ctl-<16 hex>` agent). And every name comparison goes
    through the transport's `session_name_key`, never a constant fold: tmux
    is case-sensitive (measured on 3.4 — `bmad-loop-ctl` and `bmad-loop-CTL`
    coexist), so the unconditional `.lower()` discounted a persisted `CTL`
    run's genuinely live uppercase agent as "the control session" and its
    run dir was deleted.

    Ablate the discount entirely and the `ctl` case fails; restore the
    round-16 shape predicate and the tmux digest case fails; restore the
    round-17 constant fold (base `session_name_key` returning `lower()`)
    and the tmux `CTL` case fails."""
    sessions = [
        "bmad-loop-ctl",
        "bmad-loop-CTL",
        "bmad-loop-ctl-foo",
        "bmad-loop-ctl-0123456789abcdef",
        "bmad-loop-ctl-aaaabbbbccccdddd",
    ]

    # tmux shape: case-sensitive, the only control session is the fixed name
    monkeypatch.setattr(runs, "get_multiplexer", lambda: _LivenessMux(sessions, fold=False))
    monkeypatch.setattr(runs, "ctl_session_for", lambda project, mux=None: runs.CTL_SESSION)
    assert not runs.live_session_may_be_ours(tmp_path, "ctl")
    # a case-variant is a DIFFERENT, coexisting session on tmux — live evidence
    assert runs.live_session_may_be_ours(tmp_path, "CTL")
    # ...and a digest-shaped id is a genuine agent session there
    assert runs.live_session_may_be_ours(tmp_path, "ctl-0123456789abcdef")
    assert runs.live_session_may_be_ours(tmp_path, "ctl-foo")

    # psmux shape: the transport folds, so the case-variant IS the control session
    monkeypatch.setattr(runs, "get_multiplexer", lambda: _LivenessMux(sessions, fold=True))
    assert not runs.live_session_may_be_ours(tmp_path, "CTL")
    # this project's derived name is also the control session's
    monkeypatch.setattr(
        runs, "ctl_session_for", lambda project, mux=None: "bmad-loop-ctl-aaaabbbbccccdddd"
    )
    assert not runs.live_session_may_be_ours(tmp_path, "ctl-aaaabbbbccccdddd")
    # ...while an OTHER digest in this registry is still not the control session
    assert runs.live_session_may_be_ours(tmp_path, "ctl-0123456789abcdef")


def test_live_session_may_be_ours_degrades_an_unanswerable_listing_to_absent(tmp_path, monkeypatch):
    """Observation degrades — the guard's documented contract, restored over
    this branch's withdrawn raise-propagation: a listing that cannot answer
    reads as "no session", the same answer the bundled backend gives for a
    missing multiplexer or a dead server. The control-name discount still
    answers before any probe at all, so the recovery `bmad-loop delete ctl`
    needs no transport."""
    monkeypatch.setattr(
        runs, "get_multiplexer", lambda: _LivenessMux([], fold=False, unanswerable=True)
    )
    monkeypatch.setattr(runs, "ctl_session_for", lambda project, mux=None: runs.CTL_SESSION)
    assert not runs.live_session_may_be_ours(tmp_path, "20260826-000000-run1")
    assert not runs.live_session_may_be_ours(tmp_path, "ctl")


def test_live_session_may_be_ours_degrades_an_unselectable_backend_to_absent(tmp_path, monkeypatch):
    """Selection is part of the listing read, so it degrades the listing's way.

    `mux_sessions()` selects the backend *inside* the call the guard catches, so
    a persisted `[mux] backend` naming a backend that is no longer registered has
    always read as "no live session" — a misconfigured host still gets a working
    `delete`/`archive`/`clean`. Naming the backend outside the handler turns that
    degrade into an abort on every removal path, including `clean`, which has no
    `--force`.

    Ablation: hoist the selection back above the `try` and this fails with the
    `MultiplexerError` the misconfiguration raises."""

    def unselectable():
        raise MultiplexerError("[mux] backend = 'ghost' matches no registered backend")

    monkeypatch.setattr(runs, "get_multiplexer", unselectable)
    assert not runs.live_session_may_be_ours(tmp_path, "20260826-000000-run1")
    # ...and the control-name discount needs the transport too, so it degrades alike
    assert not runs.live_session_may_be_ours(tmp_path, "ctl")


def test_prune_sessions_claims_a_historical_ctl_prefixed_session(tmp_path, monkeypatch):
    """End to end through the sweep: a `bmad-loop-ctl-foo` session minted by an
    older release (`--run-id ctl-foo`), untagged, with this project's dead run
    dir as ownership proof, is prunable in the project's own registry — the
    round-16 leak was this exact session being unreachable by both `stop` and
    the prune. Ablate `_agent_run_id`'s wellformed-not-valid split and this
    fails (the id never enters the partition)."""
    (_make_state_run(tmp_path, "ctl-foo") / "engine.pid").write_text(str(_dead_pid()))
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, str(runs.mux_registry_root(tmp_path)))
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-ctl-foo"])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])

    assert runs.prune_sessions(tmp_path, dry_run=True) == (["ctl-foo"], [], set())


def test_no_valid_run_id_can_mint_the_control_session_name(tmp_path):
    """The reviewer's reproduction, pinned end to end: read the live per-registry
    control-session name, replay its suffix as a --run-id, and the id is
    refused before `session_name` can alias the two session types. Covers the
    fixed name too — `--run-id ctl` minted `bmad-loop-ctl` itself, on every
    transport."""
    project = tmp_path / "proj"
    project.mkdir()
    ctl = runs.ctl_session_for(project, _NamespaceStub(True))
    colliding_id = ctl[len("bmad-loop-") :]
    assert runs.session_name(colliding_id) == ctl  # the alias, were the id valid
    assert not runs.is_valid_run_id(colliding_id)
    # ...nor its case variants: a Windows multiplexer resolves the uppercase
    # target onto the lowercase control session (measured on psmux 3.3.8)
    assert not runs.is_valid_run_id(colliding_id.upper())
    assert runs.session_name("ctl") == runs.CTL_SESSION
    assert not runs.is_valid_run_id("ctl")


def test_pin_state_root_overwrites_a_colliding_entry(tmp_path, monkeypatch):
    """The chokepoint's derivable arm: a caller-supplied (profile `[env]`)
    `BMAD_LOOP_STATE_DIR` is forced to this process's resolved root; other
    keys pass through untouched.

    Ablate the assignment (return `dict(env)` unchanged) and this fails."""
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S1"))
    pinned = runs.pin_state_root({"CALLER": "1", envvars.STATE_DIR: str(tmp_path / "S2")})
    assert pinned == {"CALLER": "1", envvars.STATE_DIR: str(runs.state_root())}


def test_pin_state_root_strips_the_entry_when_no_root_derives(monkeypatch):
    """The chokepoint's underivable arm — the round-11 gap: with no pin key to
    spread, an ordering rule protects nothing, so the key is REMOVED instead.
    The child then inherits the parent's own broken value and fails as the
    parent fails, rather than being aimed at a state root — and so a
    per-project registry — its parent cannot see.

    Ablate the `pop` (leave the caller's entry standing) and this fails."""
    monkeypatch.setenv(envvars.STATE_DIR, "relative-root")  # underivable
    pinned = runs.pin_state_root({"CALLER": "1", envvars.STATE_DIR: r"C:\S2"})
    assert pinned == {"CALLER": "1"}


def test_export_psmux_registry_root_degrades_on_an_underivable_state_root(tmp_path, monkeypatch):
    """Runs ahead of every command, `diagnose` and `validate` included, so a
    broken environment must not take the diagnostics down with it."""

    def boom(_project):
        raise runs.StateRootError("no state root")

    monkeypatch.delenv(runs.PSMUX_DATA_DIR, raising=False)
    monkeypatch.setattr(runs, "project_state_root", boom)
    assert runs.export_psmux_registry_root(tmp_path) is None
    assert runs.PSMUX_DATA_DIR not in os.environ

    # And an ambient value is left exactly as found here — the one case there is
    # nothing better to put in its place. `bmad-loop mux` reports that the root in
    # force is not bmad-loop's, rather than calling it derived.
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, "/whatever/they/had")
    assert runs.export_psmux_registry_root(tmp_path) is None
    assert os.environ[runs.PSMUX_DATA_DIR] == "/whatever/they/had"


def test_orphan_state_sweep_never_reaps_the_registry(tmp_path):
    """The registry holds the .port/.key files every psmux verb resolves a
    session through; sweeping it while a server is up leaves that server alive,
    unreachable, and invisible to `psmux ls` in any registry. Ablate the guard
    (drop the MUX_REGISTRY_DIR arm) and this fails, since `mux` is never a live
    run dir name — the sibling below is the other half, proving the guard did not
    simply stop the sweep."""
    registry = runs.mux_registry_root(tmp_path)
    registry.mkdir(parents=True)
    port = registry / "bmad-loop-r1.port"
    port.write_text("54321\n")

    assert runs.reconcile_orphan_state_dirs(tmp_path) == []
    assert port.exists()


def test_orphan_state_sweep_still_reaps_a_real_orphan_beside_the_registry(tmp_path):
    """The ablation's other half: sparing `mux` must not spare the orphan run
    dirs the sweep exists for."""
    runs.mux_registry_root(tmp_path).mkdir(parents=True)
    orphan = runs.state_dir_for(tmp_path, "20260101-000000-dead")
    orphan.mkdir(parents=True)

    assert runs.reconcile_orphan_state_dirs(tmp_path) == [orphan]
    assert not orphan.exists()
    assert runs.mux_registry_root(tmp_path).exists()


class _RegistryMux:
    """A backend bound to one registry, standing in for the cleanup sweep's
    second pass. Only the verbs the partition, the kill and the remainder use.

    `root` is what `registry_root()` answers: `None` is psmux's own default
    registry (the seam deliberately never respells its home cascade), which the
    remainder labels `runs.DEFAULT_REGISTRY_LABEL`.

    `fold` is the transport's name comparison: the seam's identity default
    (tmux, exact) unless set, `name.lower()` when set (psmux, whose registry is
    a directory of per-session files NTFS opens case-insensitively)."""

    def __init__(self, sessions, tags, root=None, fold=False):
        self._sessions, self._tags = sessions, tags
        self._root = root
        self._fold = fold
        self.killed: list[str] = []

    def registry_root(self):
        return self._root

    def session_name_key(self, name):
        return name.lower() if self._fold else name

    def list_sessions(self):
        return list(self._sessions)

    def session_options(self, _option):
        return dict(self._tags)

    def kill_session(self, name):
        self.killed.append(name)


def test_prune_sessions_sweeps_a_legacy_registry(tmp_path, monkeypatch):
    """Sessions created before the per-project root existed are addressable only
    from a backend bound to the old registry; without the second pass cleanup
    reports a clean sweep while their servers run on."""
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "engine_liveness", lambda _d: "dead")
    legacy = _RegistryMux(["bmad-loop-old-1"], {"bmad-loop-old-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path) == (["old-1"], [], set())
    assert legacy.killed == ["bmad-loop-old-1"]


def test_prune_sessions_leaves_another_projects_session_in_the_legacy_registry(
    tmp_path, monkeypatch
):
    """The second pass buys no extra reach: ownership is judged by the same
    partition, so a neighbouring project's sessions — and the operator's own
    psmux sessions — are skipped there exactly as they are in the primary pass."""
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    legacy = _RegistryMux(["bmad-loop-old-1"], {"bmad-loop-old-1": "0123456789abcdef"})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path) == ([], [], set())
    assert legacy.killed == []


def test_prune_sessions_dry_run_kills_nothing_in_a_legacy_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "engine_liveness", lambda _d: "dead")
    legacy = _RegistryMux(["bmad-loop-old-1"], {"bmad-loop-old-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path, dry_run=True) == (["old-1"], [], set())
    assert legacy.killed == []


def test_prune_sessions_carries_an_unknown_pid_out_of_the_legacy_registry(tmp_path, monkeypatch):
    """The legacy pass reports an unverifiable engine pid like the primary one.

    `unknown` is the killed subset whose liveness could not be read (win32
    ERROR_ACCESS_DENIED), and every cleanup frontend turns it into the "may
    still be live" warning. A session swept out of a legacy registry is exactly
    as unverifiable as one swept here, and the union in `prune_sessions` is what
    carries it — an arm that stayed green for years because the sibling tests
    stubbed `engine_liveness` with a tuple, which compares equal to neither
    "alive" nor "unknown".

    Ablate `unknown |= extra_unknown` in `prune_sessions` and this fails."""
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "engine_liveness", lambda _d: "unknown")
    legacy = _RegistryMux(["bmad-loop-old-1"], {"bmad-loop-old-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    # still killed — unknown never blocks cleanup — but named as unverifiable
    assert runs.prune_sessions(tmp_path) == (["old-1"], [], {"old-1"})
    assert legacy.killed == ["bmad-loop-old-1"]


def test_prune_sessions_leaves_a_live_legacy_session_standing(tmp_path, monkeypatch):
    """The live arm of the same union: a legacy session whose engine is provably
    running is reported live and never killed.

    Ablate `live += [...]` in `prune_sessions` and the tuple goes empty; ablate
    the `liveness == "alive"` continue and the session is killed."""
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "engine_liveness", lambda _d: "alive")
    legacy = _RegistryMux(["bmad-loop-old-1"], {"bmad-loop-old-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path) == ([], ["old-1"], set())
    assert legacy.killed == []


def test_export_records_the_root_it_displaced_for_the_migration_sweep(tmp_path, monkeypatch):
    """The wiring, end to end: the value the export overwrites is the only record
    of where a pre-upgrade machine's sessions live, and it is handed to the
    backend at the one moment it is still readable.

    Before #537 the backend inherited `PSMUX_DATA_DIR` as found, so an operator
    who exported an absolute root of their own has THEIR bmad-loop sessions in
    THAT registry — not in psmux's default. Without this hand-off the override
    strands exactly the sessions it displaced, with `cleanup` reporting a clean
    machine while the coding processes run on.

    Ablate the `note_displaced_registry` call in `export_psmux_registry_root`
    and the sweep is back to psmux's default alone."""
    from bmad_loop.adapters import psmux_backend

    # Before the export, never after: `monkeypatch.setattr` records whatever it
    # finds as the value to restore, so a reset placed *below* a real write would
    # record that write and hand it back at teardown. The autouse
    # `_isolate_mux_registry` fixture registers the same reset first and so
    # restores last (undo is LIFO), which is what keeps that mistake from
    # actually leaking — but a test whose own hygiene depends on the ordering of
    # a fixture in another file is one edit away from being wrong.
    monkeypatch.setattr(psmux_backend, "_DISPLACED_ROOT", None)
    theirs = str(tmp_path / "their-own-registry")
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, theirs)

    root = runs.export_psmux_registry_root(tmp_path)
    assert root == str(runs.mux_registry_root(tmp_path)) != theirs
    assert psmux_backend._DISPLACED_ROOT == theirs


def test_export_records_nothing_when_it_displaced_nothing(tmp_path, monkeypatch):
    """The other half, split into its own test rather than reset mid-body: a pane
    child of this project's own session already carries the derived root, which is
    the ordinary way the variable is set, and recording it would hand the sweep
    this project's *current* registry as a legacy one.

    Ablate the `displaced != root` guard in `export_psmux_registry_root` and this
    fails."""
    from bmad_loop.adapters import psmux_backend

    monkeypatch.setattr(psmux_backend, "_DISPLACED_ROOT", None)
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, str(runs.mux_registry_root(tmp_path)))

    runs.export_psmux_registry_root(tmp_path)
    assert psmux_backend._DISPLACED_ROOT is None


def test_legacy_registries_degrades_when_no_backend_can_be_selected(monkeypatch):
    """A cleanup that already swept the primary registry must report that work
    rather than die on the migration pass."""

    def boom():
        raise MultiplexerError("no backend")

    monkeypatch.setattr(runs, "get_multiplexer", boom)
    assert runs._legacy_registries() == []


# --------------------------------- legacy registry: ownership and remainder


def test_prune_sessions_refuses_an_untagged_legacy_session_claimed_only_by_a_run_dir(
    tmp_path, monkeypatch
):
    """The legacy registry is shared by every project, so a matching run dir here
    is not evidence about a session over there: run ids are unique only within one
    project and `--run-id` is caller-supplied. This project holding a dead
    `shared-id` must not let it kill another project's live, untagged
    `bmad-loop-shared-id`.

    Ablate the `require_tag` term in prunable_sessions (or stop passing it from
    the legacy pass) and this fails with the session killed — the cross-project
    reap the per-project registry removed from the primary pass, reintroduced in
    the one registry where every project's sessions sit together."""
    ours = tmp_path / "ours"
    ours.mkdir()
    (_make_state_run(ours, "shared-id") / "engine.pid").write_text(str(_dead_pid()))
    monkeypatch.setattr(runs, "mux_sessions", lambda: [])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    legacy = _RegistryMux(["bmad-loop-shared-id"], {})  # untagged: someone else's
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(ours) == ([], [], set())
    assert legacy.killed == []


def test_prune_sessions_still_claims_an_untagged_session_in_the_primary_registry(
    tmp_path, monkeypatch
):
    """The other half of the ablation: `require_tag` must not leak into the
    primary pass. There the registry itself proves ownership, so the run-dir
    fallback keeps the reach it always had — a session whose tag write failed is
    still cleanable by its own project. The export is put in force first, as
    `cli._configure_mux` does ahead of every command: with nothing exported a
    namespacing backend is on its shared default registry, where the fallback is
    correctly refused (the round-8 gate) — the reach this test pins is
    conditional on the registry being ours, not unconditional."""
    (_make_state_run(tmp_path, "fin-1") / "engine.pid").write_text(str(_dead_pid()))
    monkeypatch.setenv(runs.PSMUX_DATA_DIR, str(runs.mux_registry_root(tmp_path)))
    monkeypatch.setattr(runs, "mux_sessions", lambda: ["bmad-loop-fin-1"])
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])

    assert runs.prune_sessions(tmp_path, dry_run=True) == (["fin-1"], [], set())


class _RootedMux(_RegistryMux):
    """A registry mux that also answers which registry root it addresses, and
    whether the transport namespaces at all. The default couples the two the
    way the bundled backends do — a root in force implies a namespace, no root
    implies none (tmux) — and `namespaced=True` with `root=None` is the psmux
    default-registry shape."""

    def __init__(self, sessions, tags, root, namespaced=None):
        super().__init__(sessions, tags)
        self._root = root
        self._namespaced = (root is not None) if namespaced is None else namespaced

    def registry_root(self):
        return self._root

    def has_registry_namespace(self):
        return self._namespaced


def test_prune_refuses_an_untagged_session_in_a_registry_it_does_not_own(tmp_path, monkeypatch):
    """The untagged run-dir fallback is evidence only where the registry has
    already restricted the listing to this project. When the derivation fails,
    `export_psmux_registry_root` leaves whatever ambient `PSMUX_DATA_DIR` it found
    in force and psmux honours any absolute value — so the primary pass addresses
    the OPERATOR'S registry while this project's run dirs go on looking like
    ownership, and a run id is unique within a project, not across a registry
    shared with someone else.

    This is round-1 finding 2 reopened by the derivation-failure arm: the cut made
    an ambient value a no-op on the success path and left it live here.

    Ablate the `require_tag=not _registry_proves_ownership(project)` term and the
    kill lands in their registry."""
    (_make_state_run(tmp_path, "shared-1") / "engine.pid").write_text(str(_dead_pid()))
    theirs = _RootedMux(["bmad-loop-shared-1"], {}, str(tmp_path / "their-registry"))
    monkeypatch.setattr(runs, "get_multiplexer", lambda: theirs)
    monkeypatch.setattr(runs, "mux_sessions", theirs.list_sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid, mux=None: killed.append(rid))

    assert runs.prune_sessions(tmp_path) == ([], [], set())
    assert killed == []


def test_prune_still_claims_an_untagged_session_in_the_registry_it_derived(tmp_path, monkeypatch):
    """The other half, and the one that proves the gate did not simply stop the
    sweep: in bmad-loop's own per-project registry the fallback is sound, because
    the registry itself is what restricts the listing to this project."""
    (_make_state_run(tmp_path, "mine-1") / "engine.pid").write_text(str(_dead_pid()))
    ours = _RootedMux(["bmad-loop-mine-1"], {}, str(runs.mux_registry_root(tmp_path)))
    monkeypatch.setattr(runs, "get_multiplexer", lambda: ours)
    monkeypatch.setattr(runs, "mux_sessions", ours.list_sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid, mux=None: killed.append(rid))

    assert runs.prune_sessions(tmp_path) == (["mine-1"], [], set())
    assert killed == ["mine-1"]


def test_prune_keeps_its_historical_reach_with_no_registry_namespace(tmp_path, monkeypatch):
    """A backend with NO registry namespace (tmux: one server for the machine)
    keeps the reach it always had — its `registry_root()` None means exactly
    "there is nothing to compare", and narrowing it would be a regression
    dressed as caution. An earlier revision of this test read every None this
    way, which pinned the unsafe kill its sibling below now refuses."""
    (_make_state_run(tmp_path, "tmux-1") / "engine.pid").write_text(str(_dead_pid()))
    plain = _RootedMux(["bmad-loop-tmux-1"], {}, None)
    monkeypatch.setattr(runs, "get_multiplexer", lambda: plain)
    monkeypatch.setattr(runs, "mux_sessions", plain.list_sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid, mux=None: killed.append(rid))

    assert runs.prune_sessions(tmp_path) == (["tmux-1"], [], set())


def test_prune_refuses_an_untagged_session_on_a_backends_default_registry(tmp_path, monkeypatch):
    """`registry_root()` None from a backend that DOES namespace is not tmux's
    None: psmux with no root in force runs on its own user-wide default registry
    — shared with every project and with the operator — and there a dead run dir
    here proves nothing about an untagged session over there. Reachable when the
    export degrades on an underivable state root and no ambient value is set.

    Ablate the `has_registry_namespace()` term in `_registry_proves_ownership`
    (read every None as "nothing to own") and the kill lands in the shared
    default registry."""
    (_make_state_run(tmp_path, "shared-2") / "engine.pid").write_text(str(_dead_pid()))
    default = _RootedMux(["bmad-loop-shared-2"], {}, None, namespaced=True)
    monkeypatch.setattr(runs, "get_multiplexer", lambda: default)
    monkeypatch.setattr(runs, "mux_sessions", default.list_sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid, mux=None: killed.append(rid))

    assert runs.prune_sessions(tmp_path) == ([], [], set())
    assert killed == []


def test_registry_ownership_demands_a_tag_when_it_cannot_be_asked(tmp_path, monkeypatch):
    """A backend that cannot be selected answers False, so the pass requires the
    tag. The safe direction is to leave a session standing rather than kill one on
    evidence that may not hold."""

    def boom():
        raise MultiplexerError("no backend")

    monkeypatch.setattr(runs, "get_multiplexer", boom)
    assert runs._registry_proves_ownership(tmp_path) is False


def test_legacy_registry_leftovers_names_an_untagged_session(tmp_path, monkeypatch):
    """A sweep that silently declines to migrate something is the same silence
    this change exists to remove: cleanup prints a removal count, and a count that
    excludes what it chose not to claim reads as "everything is clean"."""
    legacy = _RegistryMux(["bmad-loop-old-1"], {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])
    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-old-1"]
    }


def test_legacy_registry_leftovers_keys_each_session_to_its_own_registry(tmp_path, monkeypatch):
    """The grouping is the whole point of the shape: the operator's next action is
    to open the registry and look, and there are two of them now — psmux's own
    default, and any absolute `PSMUX_DATA_DIR` this process displaced.

    A flat list, or a grouping that keyed everything on the default, told an
    operator whose sessions are in their own exported root to go look in a
    registry those sessions are not in.

    `registry_root()` answers `None` for psmux's default — the seam deliberately
    never respells its home cascade — so that arm is labelled instead.

    Ablate `legacy.registry_root() or DEFAULT_REGISTRY_LABEL` down to the
    constant and both keys collapse into one."""
    theirs = r"D:	heir-own-registry"
    default_reg = _RegistryMux(["bmad-loop-ctl"], {})
    displaced = _RegistryMux(["bmad-loop-old-1"], {}, root=theirs)
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [default_reg, displaced])

    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-ctl"],
        theirs: ["bmad-loop-old-1"],
    }


def test_legacy_registry_leftovers_merges_two_registries_that_name_one_root(tmp_path, monkeypatch):
    """A displaced root that happens to spell psmux's own default is admitted
    twice, and the rows merge rather than the second overwriting the first.

    Ablate the `grouped.get(label, [])` merge and the first registry's sessions
    vanish from a message that claims to name what is standing."""
    both = _RegistryMux(["bmad-loop-a"], {}), _RegistryMux(["bmad-loop-b"], {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: list(both))

    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-a", "bmad-loop-b"]
    }


def test_legacy_registry_leftovers_names_a_surviving_control_session(tmp_path, monkeypatch):
    """The prune partition never touches CTL_SESSION and the ctl-window sweep runs
    against the current registry only, so a pre-upgrade control session survives
    the migration. Naming it is the whole remedy."""
    legacy = _RegistryMux([runs.CTL_SESSION], {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])
    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: [runs.CTL_SESSION]
    }


def test_legacy_leftovers_names_a_case_variant_ctl_where_the_transport_folds(tmp_path, monkeypatch):
    """psmux resolves a session by opening `<data dir>\\<name>.port`, and NTFS
    opens names case-insensitively, so in ITS registry `bmad-loop-CTL-<hex>` is
    the control session. Asking `is_ctl_session_name` about the name as spelled
    misses it, and it then falls through `_agent_run_id` — which refuses every
    ctl-aliasing id, case-folded — so the leftover goes unreported by both arms.

    Ablate `legacy.session_name_key(name)` back to `name` and this fails with
    `{}`: the survivor is standing in a registry nothing else addresses, unnamed."""
    upper = runs.CTL_SESSION.upper() + "-0123456789ABCDEF"
    legacy = _RegistryMux([upper], {}, fold=True)
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])
    assert runs.legacy_registry_leftovers(tmp_path) == {runs.DEFAULT_REGISTRY_LABEL: [upper]}


def test_legacy_leftovers_leaves_a_case_variant_alone_where_the_transport_is_exact(
    tmp_path, monkeypatch
):
    """The other direction, and the reason the fold cannot be a constant here.
    On an exact transport (tmux: `bmad-loop-ctl` and `bmad-loop-CTL` coexist as
    distinct sessions, measured on 3.4) that name is NOT the control session, and
    it is not a session of ours either — the mint refuses every ctl-aliasing id
    case-folded, so bmad-loop cannot have created it. Naming it would send the
    operator after somebody else's session.

    Ablate to the blanket `.lower()` the review proposed and this fails."""
    upper = runs.CTL_SESSION.upper() + "-0123456789ABCDEF"
    legacy = _RegistryMux([upper], {})  # identity key: the seam default
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])
    assert runs.legacy_registry_leftovers(tmp_path) == {}


def test_legacy_registry_leftovers_degrades_on_a_transport_fault(tmp_path, monkeypatch):
    """Observation degrades: the sweep's own report still stands, and a migration
    remainder nobody could read is not a reason to fail a cleanup that already
    killed sessions."""

    class _Broken(_RegistryMux):
        def list_sessions(self):
            raise MultiplexerError("no server")

    monkeypatch.setattr(runs, "_legacy_registries", lambda: [_Broken([], {})])
    assert runs.legacy_registry_leftovers(tmp_path) == {}


def test_legacy_registry_leftovers_is_empty_with_no_legacy_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [])
    assert runs.legacy_registry_leftovers(tmp_path) == {}


# ------------------ legacy remainder: our own stranded sessions (#537)


def test_legacy_registry_leftovers_names_our_own_live_session(tmp_path, monkeypatch):
    """Tagged is not the same as dealt with. The legacy partition correctly
    declines to kill a live run of ours, and that session then sits in a registry
    ordinary attach and cleanup no longer address — the stranding worth naming.
    Ablate the `live` arm and this fails while the sweep still reports nothing."""
    runs.write_pid(_make_state_run(tmp_path, "live-1"))
    legacy = _RegistryMux(["bmad-loop-live-1"], {"bmad-loop-live-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    # the sweep itself is right: not prunable, and not killed
    assert runs.prune_sessions(tmp_path) == ([], ["live-1"], set())
    assert legacy.killed == []
    # ...and the remainder says so
    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-live-1"]
    }


def test_legacy_registry_leftovers_stays_quiet_about_a_dead_session_the_sweep_takes(
    tmp_path, monkeypatch
):
    """The other half of the ablation: a tagged, dead session of ours is the
    sweep's to remove, and reporting it as a leftover would contradict the
    "removed" line printed beside it — in --dry-run too, where it is announced as
    a would-kill."""
    (_make_state_run(tmp_path, "fin-1") / "engine.pid").write_text(str(_dead_pid()))
    legacy = _RegistryMux(["bmad-loop-fin-1"], {"bmad-loop-fin-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    plan = runs.prune_sessions(tmp_path, dry_run=True)
    assert plan == (["fin-1"], [], set())
    assert runs.legacy_registry_leftovers(tmp_path, announced=plan[0]) == {}


def test_legacy_registry_leftovers_still_stays_quiet_about_another_projects_session(
    tmp_path, monkeypatch
):
    """Unchanged and load-bearing: another project's tagged session is not this
    operator's business, and the sweep skipping it is the correct outcome rather
    than a remainder."""
    legacy = _RegistryMux(
        ["bmad-loop-theirs-1", "not-a-bmad-session"],
        {"bmad-loop-theirs-1": "0123456789abcdef"},
    )
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])
    assert runs.legacy_registry_leftovers(tmp_path) == {}


# ---------------- legacy remainder: presence, not a resampled partition (#537)


class _VanishingMux(_RegistryMux):
    """A registry whose engine exits between the sweep and the read.

    The sweep sees the run alive and correctly leaves it; by the time the reader
    looks, the pid is gone. A reader that re-ran the partition would call the
    session `prunable` and drop it from every arm it checks — reported by nobody,
    with no kill ever attempted. Presence has no such window.
    """

    def __init__(self, sessions, tags, run_dir):
        super().__init__(sessions, tags)
        self._run_dir = run_dir
        self.reads = 0

    def session_options(self, option):
        self.reads += 1
        if self.reads > 1:  # the reader's look, after the sweep's
            (self._run_dir / "engine.pid").write_text(str(_dead_pid()))
        return super().session_options(option)


def test_legacy_leftovers_names_a_session_whose_engine_exited_mid_sweep(tmp_path, monkeypatch):
    """The race the presence rule exists for. Ablate it back to consuming a
    re-run partition's `live` arm and this fails with `[]` — a session standing in
    a registry nothing addresses, and no kill attempted to explain it."""
    run_dir = _make_state_run(tmp_path, "race-live")
    runs.write_pid(run_dir)
    legacy = _VanishingMux(
        ["bmad-loop-race-live"], {"bmad-loop-race-live": runs.project_tag(tmp_path)}, run_dir
    )
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    # the sweep: alive, so correctly left standing and never killed
    assert runs.prune_sessions(tmp_path) == ([], ["race-live"], set())
    assert legacy.killed == []
    # ...and the reader names it even though it now looks prunable
    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-race-live"]
    }


def test_legacy_leftovers_names_a_session_whose_kill_did_not_land(tmp_path, monkeypatch):
    """`kill_session` is best-effort and silent by contract, so `sessions.removed`
    has always been an *attempted* kill. Presence closes that for the legacy
    registry at no extra cost: the session is still listed, so it is still named."""

    class _DeafMux(_RegistryMux):
        def kill_session(self, name):
            self.killed.append(name)  # recorded, but the session survives

    (_make_state_run(tmp_path, "fin-1") / "engine.pid").write_text(str(_dead_pid()))
    legacy = _DeafMux(["bmad-loop-fin-1"], {"bmad-loop-fin-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path) == (["fin-1"], [], set())
    assert legacy.killed == ["bmad-loop-fin-1"]
    assert runs.legacy_registry_leftovers(tmp_path) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-fin-1"]
    }


def test_legacy_leftovers_is_quiet_once_the_sweep_actually_removed_the_session(
    tmp_path, monkeypatch
):
    """The other half of the presence ablation: a session the sweep really did
    remove is gone from the listing, so it must not be named — otherwise every
    successful migration would report itself as unfinished."""

    class _RealMux(_RegistryMux):
        def kill_session(self, name):
            self.killed.append(name)
            self._sessions = [n for n in self._sessions if n != name]

    (_make_state_run(tmp_path, "fin-1") / "engine.pid").write_text(str(_dead_pid()))
    legacy = _RealMux(["bmad-loop-fin-1"], {"bmad-loop-fin-1": runs.project_tag(tmp_path)})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    assert runs.prune_sessions(tmp_path) == (["fin-1"], [], set())
    assert runs.legacy_registry_leftovers(tmp_path) == {}


def test_legacy_leftovers_dry_run_excludes_what_the_preview_announced(tmp_path, monkeypatch):
    """A dry run kills nothing, so presence alone would name every would-kill
    session the preview just listed — the preview would contradict itself. Those
    ids are excluded from the same partition the preview used."""
    (_make_state_run(tmp_path, "fin-1") / "engine.pid").write_text(str(_dead_pid()))
    runs.write_pid(_make_state_run(tmp_path, "live-1"))
    tag = runs.project_tag(tmp_path)
    legacy = _RegistryMux(
        ["bmad-loop-fin-1", "bmad-loop-live-1"],
        {"bmad-loop-fin-1": tag, "bmad-loop-live-1": tag},
    )
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    plan = runs.prune_sessions(tmp_path, dry_run=True)
    assert plan == (["fin-1"], ["live-1"], set())
    assert runs.legacy_registry_leftovers(tmp_path, announced=plan[0]) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-live-1"]
    }


def test_legacy_leftovers_dry_run_never_drops_what_the_preview_did_not_announce(
    tmp_path, monkeypatch
):
    """The dry-run half of the same race the presence rule closed for real cleanup,
    and the reason the plan is passed in rather than rediscovered here.

    The preview sees this run alive, so it prints it as live and announces no
    would-kill. The engine then exits. A reader that re-ran the partition to
    rediscover the plan would find the session prunable *now*, treat it as
    announced, and drop it from the preview entirely — a standing session named by
    nobody. Consuming the plan the preview actually printed cannot disagree with
    it.

    Ablate by re-deriving `announced` inside the reader (a second
    `prunable_sessions(..., require_tag=True)` call) and this fails with `[]`."""
    run_dir = _make_state_run(tmp_path, "race-live")
    runs.write_pid(run_dir)
    legacy = _VanishingMux(
        ["bmad-loop-race-live"], {"bmad-loop-race-live": runs.project_tag(tmp_path)}, run_dir
    )
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    plan = runs.prune_sessions(tmp_path, dry_run=True)
    assert plan == ([], ["race-live"], set())  # nothing announced as a would-kill
    assert legacy.killed == []
    assert runs.legacy_registry_leftovers(tmp_path, announced=plan[0]) == {
        runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-race-live"]
    }


def test_legacy_leftovers_dry_run_keeps_what_the_legacy_pass_cannot_claim(tmp_path, monkeypatch):
    """`prune_sessions` unions the ids of every pass, so the flat plan says only
    "some registry would kill this id" — and applying it here as a global name set
    let a would-kill in the PRIMARY registry silence a same-named session in a
    legacy one that the legacy pass, running with `require_tag=True`, deliberately
    cannot claim. The preview then disagreed with the cleanup it previews.

    Both halves are asserted against the same two registries: the real sweep leaves
    and reports the untagged session, and the dry run must say the same thing.

    Ablate by hoisting the exclusion back above the tag arms and the dry-run half
    fails with `{}` while the real half still reports it — the disagreement itself.

    Legacy refusal-gate ablation: temporarily changed the legacy call's
    ``require_tag=True`` to ``False`` and ran this test; it failed as intended,
    with ``legacy.killed == ['bmad-loop-dup']`` (the untagged session was killed).
    The gate was restored."""
    (_make_state_run(tmp_path, "dup") / "engine.pid").write_text(str(_dead_pid()))
    ours = _RootedMux(["bmad-loop-dup"], {}, str(runs.mux_registry_root(tmp_path)))
    monkeypatch.setattr(runs, "get_multiplexer", lambda: ours)
    monkeypatch.setattr(runs, "mux_sessions", ours.list_sessions)
    monkeypatch.setattr(runs, "session_project_tags", lambda: {})
    # untagged over there: the run dir proves nothing in a shared registry
    legacy = _RegistryMux(["bmad-loop-dup"], {})
    monkeypatch.setattr(runs, "_legacy_registries", lambda: [legacy])

    plan = runs.prune_sessions(tmp_path, dry_run=True)
    assert plan == (["dup"], [], set())  # announced by the primary pass alone
    preview = runs.legacy_registry_leftovers(tmp_path, announced=plan[0])

    assert runs.prune_sessions(tmp_path) == (["dup"], [], set())
    assert legacy.killed == []  # the legacy pass declined it, as it must
    assert preview == runs.legacy_registry_leftovers(tmp_path)
    assert preview == {runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-dup"]}
