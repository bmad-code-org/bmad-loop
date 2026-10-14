"""The stop-aware child runner (DW-353): the probe, the tree kill, and the
exception-safe teardown behind verify commands and declarative hooks.

The real-process rows drive a genuine process TREE — a shell (or cmd.exe) root
with a grandchild — because the defect this seam fixes is exactly what a
single-process fake cannot show: killing the root alone orphans the grandchild.
The fake-host rows pin the ordering doctrine (harvest before the first signal,
win32 force-kills the root before anything polite) that no single platform can
exercise both arms of.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import pid_gone, read_pid, wait_pid_gone, write_script_launcher

from bmad_loop import childrun, runs
from bmad_loop.process_host import get_process_host

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="drives a /bin/sh process tree")
WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="drives a cmd.exe process tree")

# A hard-stop-to-return ceiling for the real rows: one poll + four kill steps
# (two root waits, the survivor sub-harvest, the reap) + the drain is ~5.25 s by
# construction; runs._STOP_WAIT_S (10 s) is the budget the
# stop path actually has. Asserting under that budget, not the construction, keeps
# the row about the contract rather than about a slow CI host.
_RETURN_CEILING_S = runs._STOP_WAIT_S - 2.0


@contextlib.contextmanager
def stop_probe(probe: Callable[[], bool]) -> Iterator[None]:
    token = childrun.install_stop_probe(probe)
    try:
        yield
    finally:
        childrun.reset_stop_probe(token)


def _pid_written(pid_file: Path) -> Callable[[], bool]:
    """A probe that flips True once the grandchild has recorded its pid — the
    point at which the tree is known to be fully formed."""
    return lambda: read_pid(pid_file) is not None


def _sh_tree(pid_file: Path) -> str:
    """A /bin/sh root with a backgrounded grandchild that records its pid. Two
    statements plus `wait`, so sh cannot exec the command in place of itself."""
    return f"sleep 60 & echo $! > '{pid_file}'; wait"


# ---- the probe -------------------------------------------------------------------


def test_no_probe_means_never_pending():
    assert childrun.hard_stop_pending() is False


def test_probe_is_scoped_by_its_token():
    with stop_probe(lambda: True):
        assert childrun.hard_stop_pending() is True
    assert childrun.hard_stop_pending() is False


def test_completed_child_reports_its_output_and_rc(tmp_path):
    script = tmp_path / "streams.py"
    script.write_text(
        "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    with stop_probe(lambda: False):
        run = childrun.run_child(f'"{sys.executable}" "{script}"', cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(3, "out\n", "err\n")


def test_pending_hard_stop_spawns_nothing(tmp_path, monkeypatch):
    """A hard request already pending interrupts before spawn: nothing starts.

    Ablation: drop the pre-spawn probe read and the Popen tripwire fires."""

    def no_spawn(*_args, **_kwargs):
        raise AssertionError("a pending hard stop must spawn nothing")

    monkeypatch.setattr(childrun.subprocess, "Popen", no_spawn)
    with stop_probe(lambda: True):
        run = childrun.run_child("exit 0", cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(None, "", "", timed_out=False, interrupted=True)


def test_graceful_request_does_not_interrupt(tmp_path):
    """The engine's probe is mode-exact: a pending GRACEFUL request lets the
    child run to completion, exactly as before DW-353."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        json.dumps({"mode": "graceful"}), encoding="utf-8"
    )
    assert runs.read_stop_request_mode(run_dir) == "graceful"
    script = tmp_path / "slowish.py"
    script.write_text("import time\ntime.sleep(0.6)\nprint('finished')\n", encoding="utf-8")

    with stop_probe(lambda: runs.read_stop_request_mode(run_dir) == "hard"):
        run = childrun.run_child(f'"{sys.executable}" "{script}"', cwd=tmp_path, timeout=30)

    assert run.interrupted is False and run.timed_out is False
    assert run.returncode == 0 and run.stdout == "finished\n"


# ---- real POSIX trees ------------------------------------------------------------


@POSIX_ONLY
def test_hard_stop_kills_the_whole_sh_tree(tmp_path, reap_leftovers):
    """The defect verbatim: before DW-353 only the sh root died, orphaning the
    grandchild. The probe flips once the grandchild exists; the runner must
    return interrupted, promptly, with the grandchild dead.

    Ablation: skip `_reap_descendants` in `kill_tree` and the grandchild
    survives (sh dies on SIGTERM, `sleep` is reparented and keeps running)."""
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    with stop_probe(_pid_written(pid_file)):
        run = childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=120)
    elapsed = time.monotonic() - started
    grandchild = read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.interrupted is True and run.timed_out is False
    assert elapsed < _RETURN_CEILING_S
    assert wait_pid_gone(grandchild), f"grandchild {grandchild} survived the hard stop"


@POSIX_ONLY
def test_timeout_kills_the_whole_sh_tree(tmp_path, reap_leftovers):
    """The timeout leg tree-kills too; `subprocess.run` killed only the root."""
    pid_file = tmp_path / "grandchild.pid"
    with stop_probe(lambda: False):
        run = childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=1.0)
    grandchild = read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.timed_out is True and run.interrupted is False
    assert wait_pid_gone(grandchild), f"grandchild {grandchild} survived the timeout"


@POSIX_ONLY
@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_exception_unwinding_kills_the_tree_and_reraises(tmp_path, monkeypatch, exc_type):
    """Whatever unwinds through the poll loop — SIGTERM's `RunStopped`, a raw
    `KeyboardInterrupt` — kills the tree in `finally` and propagates unchanged.

    Raised from the probe, which is exactly where a signal handler's exception
    lands in practice (the loop spends its time between probe reads).

    Ablation: drop the `finally` kill and both the root and the grandchild
    survive the raise."""
    pid_file = tmp_path / "grandchild.pid"
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)

    def exploding_probe() -> bool:
        if read_pid(pid_file) is not None:
            raise exc_type("unwinding")
        return False

    with stop_probe(exploding_probe), pytest.raises(exc_type, match="unwinding"):
        childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=120)

    (root,) = spawned
    grandchild = read_pid(pid_file)
    assert grandchild is not None
    try:
        assert root.poll() is not None, "the root survived the unwinding exception"
        assert wait_pid_gone(grandchild), f"grandchild {grandchild} survived the unwind"
    finally:
        with contextlib.suppress(Exception):
            get_process_host().force_kill(grandchild)


@POSIX_ONLY
def test_timeout_reaches_the_job_of_an_exited_root(tmp_path, monkeypatch, reap_leftovers):
    """DW-477, exited root: the shell backgrounds a job, lives across a loop
    harvest, then exits, leaving the job holding the pipes. The timeout finds the
    root already reaped — nothing is enumerable from its pid any more — so only
    the tree the loop accumulated while it ran can reach the job.

    Ablation: drop the poll-loop harvest in `run_child` (or pass no `known` to
    `kill_tree`) and the job survives the timeout."""
    monkeypatch.setattr(childrun, "HARVEST_S", 0.1)
    pid_file = tmp_path / "job.pid"
    started = time.monotonic()
    with stop_probe(lambda: False):
        run = childrun.run_child(
            f"sleep 60 & echo $! > '{pid_file}'; sleep 0.6", cwd=tmp_path, timeout=1.5
        )
    elapsed = time.monotonic() - started
    job = read_pid(pid_file)
    assert job is not None
    reap_leftovers.append(job)

    assert run.timed_out is True and run.interrupted is False
    # anti-vacuity: rc 0 means the root exited on its own before the timeout; a
    # root still up there (-SIGTERM) lets the pre-signal harvest reach the job
    assert run.returncode == 0, "the root was still running at the timeout"
    assert wait_pid_gone(job), f"job {job} survived the timeout of its exited root"
    assert elapsed < 1.5 + 4 * childrun.KILL_WAIT_S + childrun.DRAIN_S + 2.0


@POSIX_ONLY
def test_unwinding_kill_reaches_the_job_of_an_exited_root(tmp_path, monkeypatch, reap_leftovers):
    """The common POSIX hard stop: the engine's SIGTERM handler raises
    `RunStopped` through the poll loop, so the kill is the `finally` one. It must
    get the known tree too — the root has already exited, so without it nothing
    reaches the job it left holding the pipes.

    Ablation: call `kill_tree(proc)` without `known` in `run_child`'s `finally`
    and the job survives."""
    monkeypatch.setattr(childrun, "HARVEST_S", 0.1)
    pid_file = tmp_path / "job.pid"
    started = time.monotonic()

    def late_raise() -> bool:
        if time.monotonic() - started >= 1.2:  # the root exited at ~0.6 s
            raise RuntimeError("unwinding")
        return False

    with stop_probe(late_raise), pytest.raises(RuntimeError, match="unwinding"):
        childrun.run_child(
            f"sleep 60 & echo $! > '{pid_file}'; sleep 0.6", cwd=tmp_path, timeout=120
        )
    job = read_pid(pid_file)
    assert job is not None
    reap_leftovers.append(job)

    assert wait_pid_gone(job), f"job {job} survived the unwinding kill of its exited root"


@POSIX_ONLY
def test_hard_stop_reaches_a_process_forked_in_the_roots_grace(tmp_path, reap_leftovers):
    """DW-477, fork during grace: the root traps TERM and, in the trap, forks a
    job and then exits. The job did not exist at the pre-signal harvest; only the
    re-harvest while the root waits out its grace sees it before the root's exit
    reparents it.

    Ablation: drop the re-harvest in `_grace_wait` and the job survives."""
    ready = tmp_path / "ready"
    pid_file = tmp_path / "job.pid"
    command = (
        f"trap 'sleep 60 & echo $! > \"{pid_file}\"; sleep 0.5; exit 0' TERM; "
        f"echo up > '{ready}'; while :; do sleep 0.05; done"
    )
    started = time.monotonic()
    with stop_probe(ready.exists):
        run = childrun.run_child(command, cwd=tmp_path, timeout=120)
    elapsed = time.monotonic() - started
    job = read_pid(pid_file)
    assert job is not None, "the TERM trap never ran"
    reap_leftovers.append(job)

    assert run.interrupted is True
    assert elapsed < _RETURN_CEILING_S
    assert wait_pid_gone(job), f"job {job} forked in the grace survived the hard stop"


# ---- real win32 tree --------------------------------------------------------------


@WIN32_ONLY
def test_hard_stop_kills_a_cmd_rooted_tree(tmp_path, reap_leftovers):
    """shell=True roots the tree at cmd.exe; a python child under it starts a
    python grandchild. The interrupt must leave no member alive."""
    pid_file = tmp_path / "grandchild.pid"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with stop_probe(_pid_written(pid_file)):
        run = childrun.run_child(
            f'"{sys.executable}" "{parent}" "{pid_file}"', cwd=tmp_path, timeout=120
        )
    elapsed = time.monotonic() - started
    grandchild = read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.interrupted is True
    assert elapsed < _RETURN_CEILING_S
    assert wait_pid_gone(grandchild), f"grandchild {grandchild} survived the hard stop"


@WIN32_ONLY
@pytest.mark.parametrize("cap", [None, 64], ids=["uncapped", "capped"])
def test_drain_gives_up_on_a_holder_nothing_can_reach(tmp_path, monkeypatch, cap):
    """DW-478: the root exits at once, leaving an orphan holder with the pipes, and
    the harvest never sees it (patched to `{}`), so no kill reaches it. CPython's
    win32 `communicate` reads each pipe in a thread blocked inside `read()` with
    the BufferedReader lock held; closing the stream then waits on that lock for
    the holder's whole life. The runner must return well before the holder dies.
    The capped row drives the same give-up through `_CappedCapture`: its readers
    block in `os.read` too, so `_StreamReader.close` must defer the close to the
    reader, which closes its own stream once the holder lets the read return.
    Either way the close is deferred, not dropped: once the holder is killed,
    both of the root's streams must end up closed rather than leaked.

    Ablation: close every stream in `_close_pipes` regardless of its reader
    thread and, where close() blocks, the return waits out the holder's 30 s.
    Ablation (capped): drop the `close_now` self-close in `_StreamReader._run`
    and the root's streams stay open after the holder dies."""
    pid_file = tmp_path / "holder.pid"
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)\n",
        encoding="utf-8",
    )
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(get_process_host(), "descendants", lambda pid: {})
    started = time.monotonic()
    holder_alive_after = False
    try:
        with stop_probe(lambda: False):
            run = childrun.run_child(
                f'"{sys.executable}" "{parent}" "{holder}" "{pid_file}"',
                cwd=tmp_path,
                timeout=1.0,
                max_stream_bytes=cap,
            )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 5.0
        while read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        early_pid = read_pid(pid_file)
        # read before the finally kills it
        holder_alive_after = early_pid is not None and not pid_gone(early_pid)
    finally:
        deadline = time.monotonic() + 5.0
        while read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        holder_pid = read_pid(pid_file)
        if holder_pid is not None:
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder_pid)

    assert holder_pid is not None, "the holder never started"
    # anti-vacuity: a slow runner whose cmd.exe was still up at the timeout lets
    # `taskkill /F /T` reach the holder, and the drain-timeout arm is never taken
    assert holder_alive_after, "the kill reached the holder; the unreachable-holder arm never ran"
    assert run.timed_out is True
    assert elapsed < 1.0 + 4 * childrun.KILL_WAIT_S + childrun.DRAIN_S + 2.0
    # the holder is dead, so each deferred reader's read has returned and the
    # reader closed its own stream
    # the runner's spawn is the first Popen; the patch is module-global, so the
    # timeout kill's `taskkill` runs are recorded after it
    root = spawned[0]
    streams = (root.stdout, root.stderr)
    deadline = time.monotonic() + 5.0
    while not all(s is not None and s.closed for s in streams):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert all(s is not None and s.closed for s in streams), "a deferred pipe close leaked"


# ---- run_argv: the executable-argv sibling ------------------------------------------


def _argv_echo(directory: Path) -> Path:
    """A script that prints its own argv (after the script path) as JSON, its
    stdin as read, and exits 3 — one child showing boundaries, stdin and rc."""
    script = directory / "echo argv.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
        "print(repr(sys.stdin.read()), file=sys.stderr)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture
def spaced_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "dir with spaces"
    directory.mkdir()
    return directory


def test_run_argv_keeps_argv_boundaries_through_a_spaced_path(spaced_dir):
    """Each element reaches the child as exactly one argument — spaces, a quote,
    an empty string — and the path itself contains spaces. rc and both streams
    come back as from `run_child`; stdin reads EOF at once (DEVNULL).

    Ablation: join the argv into one string in `run_argv` and the boundaries
    split (or the spaced path fails to launch)."""
    script = _argv_echo(spaced_dir)
    args = ["a b", 'say "hi"', "", "--version"]
    with stop_probe(lambda: False):
        run = childrun.run_argv([sys.executable, str(script), *args], cwd=spaced_dir, timeout=30)
    assert run.returncode == 3 and not run.timed_out and not run.interrupted
    assert json.loads(run.stdout) == args
    assert run.stderr == "''\n"


def test_run_argv_keeps_argv_boundaries_through_a_launcher(spaced_dir):
    """The host's script launcher (a real `.cmd` on win32 — cmd.exe-rooted even
    with shell=False — an exec'ing sh script on POSIX), in a spaced directory,
    passes argument boundaries through to the program behind it."""
    launcher = write_script_launcher(
        spaced_dir, "echo shim", _argv_echo(spaced_dir).read_text(encoding="utf-8")
    )
    args = ["a b", "--version"]
    run = childrun.run_argv([str(launcher), *args], cwd=None, timeout=30)
    assert run.returncode == 3 and not run.timed_out
    assert json.loads(run.stdout) == args


def test_run_argv_spawns_the_list_without_a_shell_and_with_devnull_stdin(tmp_path, monkeypatch):
    """The spawn contract no real child can show on every host: the argv goes to
    `Popen` as a list (never a joined string), `shell=False`, stdin DEVNULL — a
    prompting shim otherwise blocks on the caller's tty for the whole timeout —
    and the same text decoding as `run_child`.

    Ablation: drop `stdin=subprocess.DEVNULL` from `run_argv` and the stdin
    assertion reddens with a KeyError."""
    seen: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def recording_popen(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return real_popen(args, **kwargs)

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)
    argv = (sys.executable, "-c", "pass")
    run = childrun.run_argv(argv, cwd=tmp_path, timeout=30)

    assert run.returncode == 0
    assert seen["args"] == list(argv) and isinstance(seen["args"], list)
    kwargs = seen["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is True and kwargs["errors"] == "replace"
    assert kwargs["cwd"] == tmp_path


def test_run_argv_propagates_spawn_faults(tmp_path):
    """A path that is not there faults in `Popen` and propagates, as from
    `run_child`: translating it is each caller's business."""
    with pytest.raises(OSError):
        childrun.run_argv([str(tmp_path / "nope" / "missing")], cwd=None, timeout=30)


def test_run_argv_pending_hard_stop_spawns_nothing(monkeypatch):
    """Same pre-spawn probe read as `run_child`."""

    def no_spawn(*_args, **_kwargs):
        raise AssertionError("a pending hard stop must spawn nothing")

    monkeypatch.setattr(childrun.subprocess, "Popen", no_spawn)
    with stop_probe(lambda: True):
        run = childrun.run_argv(["anything"], cwd=None, timeout=30)
    assert run == childrun.ChildRun(None, "", "", timed_out=False, interrupted=True)


def _write_tree_parent(directory: Path) -> Path:
    """A native root (python) whose child sleeps 120 s holding the inherited
    pipes, after recording its pid; the root then sleeps 120 s too."""
    parent = directory / "tree parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return parent


def test_run_argv_timeout_kills_a_native_root_and_its_descendant(
    spaced_dir, monkeypatch, reap_leftovers
):
    """The timeout leg on a native executable root with a descendant holding the
    pipes: the runner returns within the kill/drain allowance, the root is
    reaped, the descendant is dead, and the kill was aimed at the very `Popen`
    this call spawned with the tree the loop accumulated (kill identity).

    Ablation: replace the `kill_tree` call in `_supervise` with `proc.kill()` and
    the kill-identity assertion reddens (the root-only kill leaves the descendant
    running)."""
    pid_file = spaced_dir / "grandchild.pid"
    parent = _write_tree_parent(spaced_dir)
    spawned: list[subprocess.Popen[str]] = []
    killed: list[tuple[subprocess.Popen[str], object]] = []
    real_popen, real_kill_tree = subprocess.Popen, childrun.kill_tree

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    def recording_kill_tree(proc, **kwargs):
        killed.append((proc, kwargs.get("known")))
        return real_kill_tree(proc, **kwargs)

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(childrun, "kill_tree", recording_kill_tree)
    timeout = 3.0
    argv = [sys.executable, str(parent), str(pid_file)]
    started = time.monotonic()
    try:
        run = childrun.run_argv(argv, cwd=spaced_dir, timeout=timeout)
        elapsed = time.monotonic() - started
    finally:
        for proc in spawned:
            reap_leftovers.append(proc.pid)
        grandchild = read_pid(pid_file)
        if grandchild is not None:
            reap_leftovers.append(grandchild)

    assert grandchild is not None, "the descendant never started inside the timeout"
    assert run.timed_out is True and run.interrupted is False
    assert elapsed < timeout + _RETURN_CEILING_S
    # The patch is process-global, so it also records the helpers the host's own
    # tree kill launches (on win32, its process-table queries); the root is the
    # one launch of this argv.
    (root,) = [proc for proc in spawned if proc.args == argv]
    assert [proc for proc, _ in killed] == [root]
    assert isinstance(killed[0][1], dict)
    assert root.poll() is not None, "the root survived the timeout"
    assert wait_pid_gone(grandchild), f"descendant {grandchild} survived the timeout"


def test_run_argv_drain_gives_up_on_a_holder_nothing_can_reach(spaced_dir, monkeypatch):
    """Bounded held-pipe drain: the root exits at once leaving an orphan holder
    with the pipes, and the harvest never sees it (patched to `{}`), so no kill
    reaches it. The runner must still return within the timeout plus the kill and
    drain bounds — never wait out the holder's 30 s — on every host.

    Ablation: drop the timeout from `_drain`'s `communicate` and the return waits
    for the holder."""
    pid_file = spaced_dir / "holder.pid"
    holder = spaced_dir / "holder.py"
    holder.write_text(
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = spaced_dir / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_process_host(), "descendants", lambda pid: {})
    started = time.monotonic()
    holder_alive_after = False
    try:
        run = childrun.run_argv(
            [sys.executable, str(parent), str(holder), str(pid_file)], cwd=None, timeout=1.0
        )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 5.0
        while read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        early_pid = read_pid(pid_file)
        holder_alive_after = early_pid is not None and not pid_gone(early_pid)
    finally:
        deadline = time.monotonic() + 5.0
        while read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        holder_pid = read_pid(pid_file)
        if holder_pid is not None:
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder_pid)

    assert holder_pid is not None, "the holder never started"
    assert holder_alive_after, "the kill reached the holder; the unreachable-holder arm never ran"
    assert run.timed_out is True
    assert run.returncode == 0, "the root was still running at the timeout"
    assert elapsed < 1.0 + 4 * childrun.KILL_WAIT_S + childrun.DRAIN_S + 2.0


# ---- kill ordering against a fake host ---------------------------------------------


class _FakeProc:
    """A Popen stand-in whose root dies on the first force_kill (win32) or
    terminate (POSIX) the fake host records against it."""

    pid = 4242

    def __init__(self, host: _FakeHost):
        self._host = host

    def poll(self) -> int | None:
        return 0 if self._host.root_dead else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._host.root_dead:
            time.sleep(timeout or 0)  # a real wait blocks; the grace loop must not spin
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return 0

    def kill(self) -> None:  # pragma: no cover - only the ProcessHostError arm
        raise AssertionError("the host seam must be used")

    terminate = kill


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.root_dead = False
        # 5001: a straggler that ignores terminate and dies only to force_kill.
        # 5002: unstamped identity — unconfirmable, so never signalled or polled.
        self.alive = {5001}
        # A root that ignores SIGTERM and dies only to force_kill.
        self.root_ignores_terminate = False
        # descendants() keyed by the pid asked about; a pid absent here has none.
        self.trees: dict[int, dict[int, float | None]] = {
            _FakeProc.pid: {5001: 1.0, 5002: None},
        }

    def descendants(self, pid: int) -> dict[int, float | None]:
        self.calls.append(("descendants", pid))
        return dict(self.trees.get(pid, {}))

    def terminate(self, pid: int) -> None:
        self.calls.append(("terminate", pid))
        if pid == _FakeProc.pid and sys.platform != "win32" and not self.root_ignores_terminate:
            self.root_dead = True

    def force_kill(self, pid: int) -> None:
        self.calls.append(("force_kill", pid))
        if pid == _FakeProc.pid:
            self.root_dead = True
        self.alive.discard(pid)

    def alive_and_ours(self, pid: int, identity: float | None) -> bool:
        assert identity is not None, "an unstamped descendant must never be polled"
        return pid in self.alive


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_kill_tree_ordering(monkeypatch, platform):
    """Harvest before the first signal; on win32 `force_kill(root)` (taskkill /F
    /T) comes before anything polite, because a polite taskkill can reap cmd.exe
    alone and strand the command where /T can no longer find it; on POSIX the
    root gets SIGTERM. Then the harvested straggler's own descendants are
    harvested (DW-477) and it is reaped (terminate, then force_kill when it
    ignores that), and the unstamped member is never touched. The root dies on
    its first signal, so its grace wait takes no re-harvest.

    Ablation: swap the win32 arm to `terminate` and the second-call assertion
    reddens; move the harvest after the root signal and the first does."""
    host = _FakeHost()
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", platform)

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    root = _FakeProc.pid
    assert host.calls[0] == ("descendants", root)
    first_signal = ("force_kill", root) if platform == "win32" else ("terminate", root)
    assert host.calls[1] == first_signal
    if platform == "win32":
        assert ("terminate", root) not in host.calls
    assert host.calls[2:] == [
        ("descendants", 5001),
        ("terminate", 5001),
        ("force_kill", 5001),
    ]
    assert not any(pid == 5002 for _, pid in host.calls)
    assert host.alive == set()


def test_kill_tree_force_kills_a_root_that_outlives_its_grace(monkeypatch):
    """A root that ignores SIGTERM outlives its grace wait: the wait keeps
    re-harvesting its descendants until the deadline, then the root is
    force-killed, and only then are the known stragglers reaped.

    Ablation: make `_grace_wait`'s deadline arm return True and `force_kill(root)`
    never happens."""
    host = _FakeHost()
    host.root_ignores_terminate = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    root = _FakeProc.pid
    calls = host.calls
    signalled = calls.index(("terminate", root))
    forced = calls.index(("force_kill", root))
    assert calls[0] == ("descendants", root) and signalled == 1
    assert ("descendants", root) in calls[signalled + 1 : forced], "no grace re-harvest"
    assert calls[forced + 1 :] == [
        ("descendants", 5001),
        ("terminate", 5001),
        ("force_kill", 5001),
    ]
    assert host.alive == set()


def test_kill_tree_reaps_a_survivors_own_descendants(monkeypatch):
    """A known survivor (a reparented job) has children the root harvest never
    listed — they were never under the root. They are harvested from the
    survivor, identity-rechecked, and reaped with it.

    Ablation: drop the survivor-descendant harvest in `_reap_descendants` and
    6001 is never signalled."""
    host = _FakeHost()
    host.trees[5001] = {6001: 2.0}
    host.alive = {5001, 6001}
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert ("terminate", 6001) in host.calls
    assert ("force_kill", 6001) in host.calls
    assert host.alive == set()


def test_kill_tree_drops_a_survivor_harvest_whose_pid_was_reused(monkeypatch):
    """The survivor sub-harvest is merged only when the survivor is still ours
    AFTER it was taken: a pid reused before the harvest lists a stranger's
    children, and none of those may be signalled.

    Ablation: merge the sub-harvest without the post-harvest `alive_and_ours`
    recheck and 6001 is signalled."""
    host = _FakeHost()
    host.trees[5001] = {6001: 2.0}
    host.alive = {5001, 6001}
    real_descendants = host.descendants

    def reusing_descendants(pid: int) -> dict[int, float | None]:
        out = real_descendants(pid)
        if pid == 5001:
            host.alive.discard(5001)  # 5001 died and its pid went to a stranger
        return out

    host.descendants = reusing_descendants  # type: ignore[method-assign]
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert not any(pid == 6001 for name, pid in host.calls if name != "descendants")
    assert host.alive == {6001}


def test_kill_tree_reaps_the_known_tree_of_an_exited_root(monkeypatch):
    """A root that already exited is reaped — its pid may belong to someone else
    — so it is never signalled or harvested. But the tree the poll loop
    accumulated while it ran is still reaped: the background job that outlived
    its shell (DW-477).

    Ablation: keep the unconditional early return on an exited root and 5001
    survives."""
    host = _FakeHost()
    host.root_dead = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0, 5002: None},
    )

    assert not any(pid == _FakeProc.pid for _, pid in host.calls)
    assert ("terminate", 5001) in host.calls and ("force_kill", 5001) in host.calls
    assert not any(pid == 5002 for _, pid in host.calls)
    assert host.alive == set()


def test_kill_tree_never_lets_an_unstamped_harvest_overwrite_a_stamp(monkeypatch):
    """An unstamped entry must not erase the identity an earlier harvest
    recorded: that would turn a member the reap can confirm into one it must
    never touch. The built-in hosts OMIT a member they cannot stamp, so this
    defends against a host that returns unstamped entries — an out-of-tree
    `register_process_host` host, or a platform that never stamps.

    Ablation: let `_merge` overwrite unconditionally and 5001 is never reaped."""
    host = _FakeHost()
    host.trees[_FakeProc.pid] = {5001: None}
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0},
    )

    assert ("force_kill", 5001) in host.calls
    assert host.alive == set()


def test_kill_tree_retry_after_an_unwound_kill_reaps_what_the_first_harvested(monkeypatch):
    """The common POSIX hard stop: SIGTERM's `RunStopped` can land INSIDE the first
    `kill_tree` — after its pre-signal harvest and root signal — and `run_child`'s
    `finally` retries with the same `known`. The root is reaped by then, so the
    retry harvests nothing; only what the first call merged into `known` in place
    is left to reach.

    Ablation: make `kill_tree` work on a copy of `known` (`dict(known)`) and the
    retry finds an empty tree, so 5001 survives."""

    class _UnwindingHost(_FakeHost):
        def terminate(self, pid: int) -> None:
            super().terminate(pid)
            if pid == _FakeProc.pid:
                raise RuntimeError("unwinding")

    host = _UnwindingHost()
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")
    proc = _FakeProc(host)
    known: childrun.KnownTree = {}

    with pytest.raises(RuntimeError, match="unwinding"):
        childrun.kill_tree(proc, wait_s=0.05, known=known)  # pyright: ignore[reportArgumentType]
    assert 5001 in known, "the first call's harvest was not merged into the caller's tree"

    host.calls.clear()
    childrun.kill_tree(proc, wait_s=0.05, known=known)  # pyright: ignore[reportArgumentType]

    assert not any(pid == _FakeProc.pid for _, pid in host.calls)
    assert ("force_kill", 5001) in host.calls
    assert host.alive == set()


def test_kill_tree_bounds_the_survivor_sub_harvest_by_wait_s(monkeypatch):
    """One scan per survivor, so the sub-harvest has its own `wait_s` budget: no
    scan starts past it, and every survivor is still reaped. That keeps the kill's
    fourth step inside the hard-stop-to-return bound on a slow host.

    Ablation: drop the `sub_deadline` break in `_reap_descendants` and every
    survivor is scanned."""
    host = _FakeHost()
    host.root_dead = True
    host.alive = {5001, 5003, 5004}
    real_descendants = host.descendants

    def slow_descendants(pid: int) -> dict[int, float | None]:
        time.sleep(0.1)  # one scan outruns the whole 0.05 s budget
        return real_descendants(pid)

    host.descendants = slow_descendants  # type: ignore[method-assign]
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0, 5003: 1.0, 5004: 1.0},
    )

    assert [pid for name, pid in host.calls if name == "descendants"] == [5001]
    assert {pid for name, pid in host.calls if name == "force_kill"} == {5001, 5003, 5004}
    assert host.alive == set()


def test_unresolvable_host_leaves_a_completing_command_alone(tmp_path, monkeypatch):
    """`run_child` resolves the host up front for the loop harvest. A host that
    cannot be resolved (a bogus `BMAD_LOOP_PROCESS_HOST`) only disables the
    harvest: a command that finishes on its own still returns its result, as it
    did when only the kill resolved the host.

    Ablation: drop the `ProcessHostError` suppression around the resolution in
    `run_child` and the call raises right after spawn."""
    from bmad_loop.process_host import ProcessHostError

    def bogus_host():
        raise ProcessHostError("bogus-host-name")

    monkeypatch.setattr(childrun, "get_process_host", bogus_host)
    with stop_probe(lambda: False):
        run = childrun.run_child("exit 0", cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(0, "", "")


def test_loop_harvest_prunes_what_is_gone_and_keeps_what_is_reparented():
    """The loop's known tree stays bounded over a long run: an entry a fresh
    harvest no longer lists is dropped once it is gone or reused, or when it was
    never stamped. One still ours stays — that is the reparented job the tree
    exists to keep in reach. A fresh stamp for a known pid replaces the old one.

    Ablation: drop the prune in `_harvest_and_prune` and 7001/7002 remain."""
    host = _FakeHost()
    host.trees[_FakeProc.pid] = {5001: 3.0, 5003: None}
    host.alive = {5001, 7003}
    known: dict[int, float | None] = {
        5001: 1.0,  # re-stamped by the fresh harvest (new generation)
        7001: 1.0,  # absent and gone: pruned
        7002: None,  # absent and unstamped: pruned
        7003: 1.0,  # absent but still ours (reparented): kept
    }

    harvest: Any = childrun._harvest_and_prune  # pyright: ignore[reportPrivateUsage]
    harvest(host, _FakeProc.pid, known)

    assert known == {5001: 3.0, 5003: None, 7003: 1.0}


def test_loop_harvest_stops_once_the_root_is_reaped_or_the_host_faults():
    """The loop harvests only from an unreaped root (the Popen pins its pid) and
    never lets a host fault escape: it stops harvesting instead, and the kill
    reports a broken host loudly.

    Ablation: drop the `poll()` gate and the reaped root's pid is harvested;
    drop the `ProcessHostError` catch and the fault escapes."""
    from bmad_loop.process_host import ProcessHostError

    host = _FakeHost()
    host.root_dead = True
    known: dict[int, float | None] = {}
    loop_harvest: Any = childrun._loop_harvest  # pyright: ignore[reportPrivateUsage]
    got = loop_harvest(host, _FakeProc(host), known)
    assert got is None and host.calls == [] and known == {}

    class _FaultingHost(_FakeHost):
        def alive_and_ours(self, pid: int, identity: float | None) -> bool:
            raise ProcessHostError("no psutil")

    faulting = _FaultingHost()
    known = {7001: 1.0}  # absent from the fresh harvest, so the prune reads liveness
    got = loop_harvest(faulting, _FakeProc(faulting), known)
    assert got is None


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_kill_tree_strikes_the_root_once_before_reraising_a_host_error(monkeypatch, platform):
    """An explicit-but-bogus process-host override raises `ProcessHostError` by
    doctrine, but the root must not be left alive behind the raise: exactly one
    legacy Popen strike (`kill` on win32, `terminate` on POSIX), then the error
    propagates (precedent: the opencode adapter's `_kill_process` row).

    Ablation: drop the strike in `kill_tree`'s `except ProcessHostError` and the
    strike counts read zero; swallow the error and `pytest.raises` fails."""
    from bmad_loop.process_host import ProcessHostError

    class _StrikeCountingPopen:
        pid = 4242

        def __init__(self) -> None:
            self.terminated = 0
            self.killed = 0

        def poll(self) -> int | None:
            return None  # alive: kill_tree must not early-return

        def terminate(self) -> None:
            self.terminated += 1

        def kill(self) -> None:
            self.killed += 1

    def bogus_host():
        raise ProcessHostError("bogus-host-name")

    monkeypatch.setattr(childrun, "get_process_host", bogus_host)
    monkeypatch.setattr(childrun.sys, "platform", platform)
    proc = _StrikeCountingPopen()

    with pytest.raises(ProcessHostError, match="bogus-host-name"):
        childrun.kill_tree(proc)  # pyright: ignore[reportArgumentType]

    if platform == "win32":
        assert (proc.killed, proc.terminated) == (1, 0)
    else:
        assert (proc.killed, proc.terminated) == (0, 1)


def test_kill_tree_leaves_an_exited_root_alone(monkeypatch):
    """A root already reaped with no known tree has nothing reachable left: no
    harvest, no signal (its pid may already belong to someone else). The
    no-`known` call shape stays backward compatible."""
    host = _FakeHost()
    host.root_dead = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert host.calls == []


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Reader:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _PipedProc:
    """stdout's reader thread is still blocked in read(); stderr's has finished."""

    def __init__(self) -> None:
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.stdout_thread = _Reader(alive=True)
        self.stderr_thread = _Reader(alive=False)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_close_pipes_skips_a_stream_its_win32_reader_still_holds(monkeypatch, platform):
    """DW-478: on win32 a stream whose reader thread is still alive is left for
    that thread to close — `close()` would block on the BufferedReader lock its
    `read()` holds. A stream whose reader finished is closed. POSIX has no reader
    threads to wait on, so it closes both whatever attribute is present.

    Ablation: drop the reader-thread check in `_close_pipes` and the win32 arm
    closes stdout; make it unconditional and the linux arm leaves stdout open."""
    monkeypatch.setattr(childrun.sys, "platform", platform)
    proc = _PipedProc()

    childrun._close_pipes(proc)  # pyright: ignore[reportArgumentType, reportPrivateUsage]

    assert proc.stderr.closed is True
    assert proc.stdout.closed is (platform != "win32")


def test_timeout_stream_normalises_bytes_like_text_mode():
    assert childrun.timeout_stream(None) == ""
    assert childrun.timeout_stream("already\r\ntext") == "already\r\ntext"
    assert childrun.timeout_stream(b"a\r\nb\rc\n") == "a\nb\nc\n"


# ---- the drain-timeout arm ---------------------------------------------------------
#
# The only production path into `timeout_stream` since DW-353: a timed-out or
# interrupted command's output normally comes from the completed post-kill
# `communicate`, already text-mode decoded. Only when a pipe-holder the tree kill
# cannot reach outlives `DRAIN_S` does the drain raise `TimeoutExpired`, handing
# over the raw POSIX chunks for `timeout_stream` to decode. The holder here is the
# documented `kill_tree` limit made concrete: a background job double-forked out of
# a subshell that exits at once, so it is reparented away from the root before any
# harvest (the loop's first one is `HARVEST_S` in) and keeps the pipes open.
#
# Driven inside an ASCII-locale child interpreter for the reason the #378 rows in
# tests/test_verify.py give: every CI leg is UTF-8, where the locale codec and a
# hardcoded UTF-8 decode agree, so the codec half of the normalisation can only be
# observed under `LC_ALL=C` + `PYTHONUTF8=0`.

_DRAIN_RAW = b"caf\xc3\xa9\r\nsecond\rthird\n"


@POSIX_ONLY
def test_drain_timeout_output_goes_through_timeout_stream(tmp_path):
    """A pipe-holder the kill cannot reach forces the drain-timeout arm; what the
    tree wrote must still read back exactly as a completed run's output does —
    locale codec, newlines collapsed — and the runner must return within the
    drain bound rather than wait on the holder.

    Ablation: return `exc.stdout` raw (or decode it as UTF-8, or skip the newline
    collapse) in `_drain`'s except arm and `drained_stdout` stops matching
    `completed_stdout`; the `timeout_stream_calls` anti-vacuity check fails if the
    arm is ever not reached."""
    emit = tmp_path / "emit.py"
    emit.write_text(
        "import sys\n" f"sys.stdout.buffer.write({_DRAIN_RAW!r})\n" "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    holder_pid = tmp_path / "holder.pid"
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import json, locale, sys, time\n"
        "from bmad_loop import childrun\n"
        "emit, holder_pid, cwd = sys.argv[1:4]\n"
        "calls = []\n"
        "real = childrun.timeout_stream\n"
        "def spy(value):\n"
        "    calls.append(type(value).__name__)\n"
        "    return real(value)\n"
        "childrun.timeout_stream = spy\n"
        "childrun.DRAIN_S = 0.3\n"
        'py = \'"%s" "%s"\' % (sys.executable, emit)\n'
        "done = childrun.run_child(py, cwd=cwd, timeout=30)\n"
        "# the subshell exits at once, so `sleep 30` is reparented away from the\n"
        "# root before any harvest and holds stdout/stderr open past the kill\n"
        "cmd = \"%s; (sleep 30 & echo $! > '%s'); sleep 60\" % (py, holder_pid)\n"
        "started = time.monotonic()\n"
        "hung = childrun.run_child(cmd, cwd=cwd, timeout=1.0)\n"
        "elapsed = time.monotonic() - started\n"
        "json.dump({'encoding': locale.getpreferredencoding(False),\n"
        "           'completed_stdout': done.stdout, 'drained_stdout': hung.stdout,\n"
        "           'drained_stderr': hung.stderr, 'timed_out': hung.timed_out,\n"
        "           'elapsed': elapsed, 'timeout_stream_calls': calls}, sys.stdout)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "LANG", "LC_CTYPE")}
    env["LC_ALL"] = "C"
    env["PYTHONUTF8"] = "0"  # without this the C locale would resolve to UTF-8 (PEP 540)

    try:
        proc = subprocess.run(
            [sys.executable, str(driver), str(emit), str(holder_pid), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
    finally:
        holder = read_pid(holder_pid)
        if holder is not None and not pid_gone(holder):
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder)

    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    decoded = _DRAIN_RAW.decode(observed["encoding"], errors="replace")
    # anti-vacuity: a UTF-8 codec or a payload without CRs would pass with the bug in
    assert decoded != _DRAIN_RAW.decode("utf-8", errors="replace")
    assert "\r" in decoded
    # the drain-timeout arm was actually taken, on the raw POSIX bytes
    assert "bytes" in observed["timeout_stream_calls"]

    assert observed["completed_stdout"] == decoded.replace("\r\n", "\n").replace("\r", "\n")
    assert observed["timed_out"] is True
    assert observed["drained_stdout"] == observed["completed_stdout"]
    assert observed["drained_stderr"] == ""
    # timeout + kill steps + the (patched) drain — never the holder's 30 s
    assert observed["elapsed"] < 1.0 + 3 * childrun.KILL_WAIT_S + 0.3 + 2.0


# ---- the capped capture (DW-549) ---------------------------------------------------
#
# `max_stream_bytes` bounds what the runner HOLDS of a stream while the child runs,
# not just what it returns. The pure rows grade `_TailBuffer` against `byte_tail` of
# the whole decoded stream — the definition the capped path must reproduce — and
# its held-bytes bound after every feed; the real rows drive `run_child` itself.

# Two-, three- and four-byte characters, CRLF pairs and lone CRs, so a sweep of
# chunk sizes splits every one of them across a read boundary somewhere.
_TAIL_RAW = ("abé\r\n€\rcd\U0001f600\r\n" * 7 + "tail\r").encode("utf-8")


def _text_mode(raw: bytes, encoding: str = "utf-8") -> str:
    """What the completed text-mode path makes of ``raw``: decode with replace,
    then universal newlines (``Popen._translate_newlines``)."""
    return raw.decode(encoding, errors="replace").replace("\r\n", "\n").replace("\r", "\n")


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 16, len(_TAIL_RAW)])
@pytest.mark.parametrize("cap", [0, 1, 3, 4, 9, 40, 10_000])
def test_tail_buffer_equals_byte_tail_of_the_whole_and_stays_bounded(chunk, cap):
    """Fed in `chunk`-byte reads, the buffer returns exactly `byte_tail` of the
    whole decoded stream — a multibyte character or `\\r\\n` split across reads
    decodes as if read whole — and after EVERY feed it holds at most the cap plus
    one coalesced piece: under `min(cap, _READ_CHUNK)` bytes plus the decoded read
    that closed it. A `chunk`-byte read of valid UTF-8 decodes to at most `chunk`
    bytes plus the up-to-3-byte partial carried from the read before.

    Ablation: drop the leading-piece trim in `_TailBuffer._append` and the
    held-bytes bound fails on every small-cap row; decode each chunk on its own
    (`data.decode(...)`, no incremental decoder) and the split-character rows
    gain U+FFFD where the whole decode has none."""
    expected_whole = _text_mode(_TAIL_RAW)
    buffer = childrun._TailBuffer(cap, "utf-8", "replace")  # pyright: ignore[reportPrivateUsage]
    coalesce = max(1, min(cap, childrun._READ_CHUNK))  # pyright: ignore[reportPrivateUsage]
    for start in range(0, len(_TAIL_RAW), chunk):
        buffer.feed(_TAIL_RAW[start : start + chunk])
        assert buffer.held_bytes <= max(cap, 0) + coalesce + chunk + 3
    buffer.finish()

    assert buffer.snapshot() == childrun.byte_tail(expected_whole, cap)
    # premise: the payload really is over the small caps and really splits pairs
    assert len(expected_whole.encode("utf-8")) > 40 and "\r" not in expected_whole


def test_tail_buffer_holds_one_cut_piece_and_ignores_feeds_after_finish():
    """A single piece far over the cap is cut on arrival rather than held whole,
    a dangling `\\r` is settled by `finish`, and a feed arriving after `finish` —
    a win32 reader returning from a read the drain gave up on — is ignored."""
    buffer = childrun._TailBuffer(8, "utf-8", "replace")  # pyright: ignore[reportPrivateUsage]
    buffer.feed(b"x" * 100_000 + b"\r")
    assert buffer.held_bytes <= 8
    buffer.finish()
    buffer.finish()  # idempotent
    buffer.feed(b"late")

    assert buffer.snapshot() == ("x" * 7 + "\n", 100_001)


def test_tail_buffer_coalesces_tiny_reads(monkeypatch):
    """A child flushing one byte at a time must not cost one held object per
    read: small decoded pieces coalesce into runs of `min(cap, _READ_CHUNK)`
    bytes, so the held piece count stays O(cap / _READ_CHUNK) — plus the open
    run's parts — however many reads arrive. `_READ_CHUNK` is lowered so a small
    cap still spans many coalesced pieces.

    Ablation: close every piece on arrival (coalesce threshold 1) and the count
    climbs to ~cap one-byte pieces, past the bound on the first few hundred feeds."""
    monkeypatch.setattr(childrun, "_READ_CHUNK", 64)
    cap = 1000
    raw = bytes(ord("a") + i % 26 for i in range(10_000))
    buffer = childrun._TailBuffer(cap, "utf-8", "replace")  # pyright: ignore[reportPrivateUsage]
    most = 0
    for byte in raw:
        buffer.feed(bytes([byte]))
        most = max(most, buffer.piece_count)
    buffer.finish()

    assert most <= cap // 64 + 2 + 64  # closed pieces + the open run's parts
    assert buffer.snapshot() == childrun.byte_tail(raw.decode("ascii"), cap)


def test_tail_buffer_decodes_with_the_resolved_codec():
    """The capped decode is the completed path's codec, not a hardcoded UTF-8:
    under an ASCII codec the two-byte `é` reads back as two U+FFFD, exactly as
    `communicate` would have returned it."""
    raw = b"caf\xc3\xa9\r\nsecond\rthird\n"
    buffer = childrun._TailBuffer(  # pyright: ignore[reportPrivateUsage]
        1 << 20, "ascii", "replace"
    )
    for byte in raw:
        buffer.feed(bytes([byte]))
    buffer.finish()

    expected = _text_mode(raw, "ascii")
    assert expected != _text_mode(raw)  # premise: the codec choice is observable
    assert buffer.snapshot() == (expected, len(expected.encode("utf-8")))


def _writer(directory: Path, body: str) -> str:
    script = directory / "writer.py"
    script.write_text("import sys, time\n" + body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_capped_run_keeps_the_tail_and_the_emitted_size(tmp_path, monkeypatch):
    """Over the cap: each stream comes back as `byte_tail` of what the child wrote,
    with its emitted size, across many pipe reads — and the cap holds WHILE the
    streams are read, not only on the result: after every feed the buffer holds
    at most the cap plus one coalesced piece (`min(cap, _READ_CHUNK)`) plus one
    decoded read (`_READ_CHUNK` + a 3-byte carried partial).

    Ablations: pass `max_stream_bytes` nowhere in `run_child` and the stream comes
    back whole with `stdout_bytes` None; make `_TailBuffer._append` keep every
    piece uncut and untrimmed (cutting only in `snapshot`) and the final values
    still match, but the recorded held maximum is the whole stream."""
    real_feed = childrun._TailBuffer.feed  # pyright: ignore[reportPrivateUsage]
    held: list[int] = []

    def recording_feed(self, data: bytes) -> None:
        real_feed(self, data)
        held.append(self.held_bytes)

    tail_buffer = childrun._TailBuffer  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(tail_buffer, "feed", recording_feed)
    command = _writer(
        tmp_path,
        "for i in range(10000):\n"
        "    sys.stdout.write('line %05d \\u00e9\\n' % i)\n"
        "sys.stderr.write('e' * 300)\n"
        "sys.exit(2)\n",
    )
    whole_out = "".join("line %05d é\n" % i for i in range(10000))
    with stop_probe(lambda: False):
        run = childrun.run_child(command, cwd=tmp_path, timeout=30, max_stream_bytes=101)

    assert run.returncode == 2 and not run.timed_out and not run.interrupted
    assert (run.stdout, run.stdout_bytes) == childrun.byte_tail(whole_out, 101)
    assert (run.stderr, run.stderr_bytes) == ("e" * 101, 300)
    chunk = childrun._READ_CHUNK  # pyright: ignore[reportPrivateUsage]
    assert run.stdout_bytes is not None
    assert run.stdout_bytes > chunk * 2  # premise: more than one read chunk
    assert held and max(held) <= 101 + min(101, chunk) + chunk + 3


def test_a_failing_reader_surfaces_from_run_child(tmp_path, monkeypatch):
    """A reader thread that fails mid-stream is loud: its exception propagates
    from `run_child`, never folded into a healthy-looking short stream. Injected
    at the reader's read loop (both platform arms), after its first chunk.

    Ablation: delete the `raise reader.error` loop in `_CappedCapture.result` and
    the call returns normally with a truncated stream."""

    def first_chunk_then_fail(self, fd: int) -> None:
        self.buffer.feed(os.read(fd, childrun._READ_CHUNK))  # pyright: ignore[reportPrivateUsage]
        raise OSError("reader broke")

    reader_cls = childrun._StreamReader  # pyright: ignore[reportPrivateUsage]
    for name in ("_read_selected", "_read_blocking"):
        monkeypatch.setattr(reader_cls, name, first_chunk_then_fail)
    command = _writer(tmp_path, "print('out')\n")
    with stop_probe(lambda: False), pytest.raises(OSError, match="reader broke"):
        childrun.run_child(command, cwd=tmp_path, timeout=30, max_stream_bytes=64)


def test_capped_run_under_the_cap_returns_the_streams_whole(tmp_path):
    command = _writer(tmp_path, "print('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n")
    with stop_probe(lambda: False):
        run = childrun.run_child(command, cwd=tmp_path, timeout=30, max_stream_bytes=1 << 20)

    assert run == childrun.ChildRun(3, "out\n", "err\n", stdout_bytes=4, stderr_bytes=4)


def test_capped_timeout_retains_the_tail_of_what_was_written(tmp_path):
    """A child still running at its timeout is killed and its flags set, and the
    tail of what it wrote before the kill survives with its emitted size."""
    command = _writer(
        tmp_path,
        "sys.stdout.write('x' * 5000 + 'END')\nsys.stdout.flush()\ntime.sleep(60)\n",
    )
    with stop_probe(lambda: False):
        run = childrun.run_child(command, cwd=tmp_path, timeout=1.0, max_stream_bytes=16)

    assert run.timed_out is True and run.interrupted is False
    assert (run.stdout, run.stdout_bytes) == ("x" * 13 + "END", 5003)
    assert (run.stderr, run.stderr_bytes) == ("", 0)


def test_uncapped_callers_apply_no_cap(tmp_path, monkeypatch):
    """`run_argv` (the probe) and a `run_child` call without the keyword (the
    plugin bus) stay on the `communicate` path: no cap reaches `_supervise`, and
    the result carries no emitted sizes.

    Ablation: default `max_stream_bytes` to a number in `run_child`, or pass one
    from `run_argv`, and the spy records it."""
    seen: list[int | None] = []
    real = childrun._supervise  # pyright: ignore[reportPrivateUsage]

    def spy(spawn, timeout, max_stream_bytes=None):
        seen.append(max_stream_bytes)
        return real(spawn, timeout, max_stream_bytes)

    monkeypatch.setattr(childrun, "_supervise", spy)
    with stop_probe(lambda: False):
        shell = childrun.run_child(_writer(tmp_path, "print('x')\n"), cwd=tmp_path, timeout=30)
        argv = childrun.run_argv([sys.executable, "-c", "print('y')"], cwd=tmp_path, timeout=30)

    assert seen == [None, None]
    assert shell.stdout == "x\n" and argv.stdout == "y\n"
    assert (shell.stdout_bytes, shell.stderr_bytes) == (None, None)
    assert (argv.stdout_bytes, argv.stderr_bytes) == (None, None)


@POSIX_ONLY
def test_capped_unwind_kills_the_tree_and_closes_the_pipes(tmp_path, monkeypatch):
    """An exception unwinding through the capped loop kills the tree like the
    uncapped one, and its teardown stops the readers before closing the pipes.

    Ablation: drop `capture.release()` from `_supervise`'s `finally` and the
    reader threads outlive the call with both pipes open."""
    pid_file = tmp_path / "grandchild.pid"
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)

    def exploding_probe() -> bool:
        if read_pid(pid_file) is not None:
            raise RuntimeError("unwinding")
        return False

    with stop_probe(exploding_probe), pytest.raises(RuntimeError, match="unwinding"):
        childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=120, max_stream_bytes=64)

    (root,) = spawned
    grandchild = read_pid(pid_file)
    assert grandchild is not None
    try:
        assert root.poll() is not None, "the root survived the unwinding exception"
        assert wait_pid_gone(grandchild), f"grandchild {grandchild} survived the unwind"
        assert root.stdout is not None and root.stdout.closed
        assert root.stderr is not None and root.stderr.closed
        assert not [t for t in threading.enumerate() if t.name.startswith("bmad-loop-child-")]
    finally:
        with contextlib.suppress(Exception):
            get_process_host().force_kill(grandchild)


@POSIX_ONLY
def test_capped_drain_timeout_decodes_like_the_completed_path(tmp_path):
    """The capped twin of `test_drain_timeout_output_goes_through_timeout_stream`:
    a pipe-holder the kill cannot reach forces the capped drain to give up. What
    the tree wrote must read back exactly as the completed (uncapped) run's output
    — locale codec, newlines collapsed — with its emitted size, the readers must
    be stopped and both pipes closed, and the runner must return within the same
    latency bound rather than wait on the holder.

    Ablation: build `_TailBuffer` with a hardcoded "utf-8" codec and `drained`
    stops matching `completed`; drop the reader stop in `_CappedCapture.release`
    and the join backstop times out, leaving the readers live and both pipes
    open. The `settles` anti-vacuity check fails if the drain ever settled, i.e.
    if the give-up arm was not reached."""
    emit = tmp_path / "emit.py"
    emit.write_text(
        "import sys\n" f"sys.stdout.buffer.write({_DRAIN_RAW!r})\n" "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    holder_pid = tmp_path / "holder.pid"
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import json, locale, subprocess, sys, threading, time\n"
        "from bmad_loop import childrun\n"
        "emit, holder_pid, cwd = sys.argv[1:4]\n"
        "settles = []\n"
        "real_settle = childrun._CappedCapture.settle\n"
        "def settle(self, timeout):\n"
        "    settled = real_settle(self, timeout)\n"
        "    settles.append(settled)\n"
        "    return settled\n"
        "childrun._CappedCapture.settle = settle\n"
        "procs = []\n"
        "real_popen = subprocess.Popen\n"
        "def popen(*a, **k):\n"
        "    procs.append(real_popen(*a, **k))\n"
        "    return procs[-1]\n"
        "childrun.subprocess.Popen = popen\n"
        "childrun.DRAIN_S = 0.3\n"
        'py = \'"%s" "%s"\' % (sys.executable, emit)\n'
        "done = childrun.run_child(py, cwd=cwd, timeout=30)\n"
        "cmd = \"%s; (sleep 30 & echo $! > '%s'); sleep 60\" % (py, holder_pid)\n"
        "settles.clear()\n"
        "started = time.monotonic()\n"
        "hung = childrun.run_child(cmd, cwd=cwd, timeout=1.0, max_stream_bytes=1 << 20)\n"
        "elapsed = time.monotonic() - started\n"
        "readers = [t.name for t in threading.enumerate()\n"
        "           if t.name.startswith('bmad-loop-child-')]\n"
        "json.dump({'encoding': locale.getpreferredencoding(False),\n"
        "           'completed': done.stdout, 'drained': hung.stdout,\n"
        "           'drained_bytes': hung.stdout_bytes, 'drained_stderr': hung.stderr,\n"
        "           'timed_out': hung.timed_out, 'elapsed': elapsed,\n"
        "           'settles': settles, 'live_readers': readers,\n"
        "           'closed': [procs[-1].stdout.closed, procs[-1].stderr.closed]},\n"
        "          sys.stdout)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "LANG", "LC_CTYPE")}
    env["LC_ALL"] = "C"
    env["PYTHONUTF8"] = "0"  # without this the C locale would resolve to UTF-8 (PEP 540)

    try:
        proc = subprocess.run(
            [sys.executable, str(driver), str(emit), str(holder_pid), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
    finally:
        holder = read_pid(holder_pid)
        if holder is not None and not pid_gone(holder):
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder)

    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    decoded = _DRAIN_RAW.decode(observed["encoding"], errors="replace")
    # anti-vacuity: a UTF-8 codec or a payload without CRs would pass with the bug in
    assert decoded != _DRAIN_RAW.decode("utf-8", errors="replace")
    assert "\r" in decoded
    # the give-up arm was taken: the drain's settle (the last one) never settled
    assert observed["settles"] and observed["settles"][-1] is False

    assert observed["completed"] == decoded.replace("\r\n", "\n").replace("\r", "\n")
    assert observed["timed_out"] is True
    assert observed["drained"] == observed["completed"]
    assert observed["drained_bytes"] == len(observed["completed"].encode("utf-8"))
    assert observed["drained_stderr"] == ""
    assert observed["live_readers"] == [] and observed["closed"] == [True, True]
    # timeout + kill steps + the (patched) drain + one reader stop — never the holder's 30 s
    assert observed["elapsed"] < 1.0 + 3 * childrun.KILL_WAIT_S + 0.3 + 2.0
