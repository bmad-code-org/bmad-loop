"""Tests for the cross-platform process-lifecycle seam."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath

import pytest

from bmad_loop import process_host
from bmad_loop.process_host import (
    PosixProcessHost,
    ProcessHostError,
    WindowsProcessHost,
    get_process_host,
    register_process_host,
)


@pytest.fixture(autouse=True)
def _clear_host_cache():
    # get_process_host is lru_cached and env-driven; isolate every case.
    get_process_host.cache_clear()
    yield
    get_process_host.cache_clear()


@pytest.fixture
def host():
    return PosixProcessHost()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="PosixProcessHost.is_alive uses os.kill(pid, 0); on Windows signal 0 is "
    "CTRL_C_EVENT, so the probe sends a real Ctrl+C to this process's console and "
    "aborts the run. Windows self-liveness is covered by the WindowsProcessHost test below.",
)
def test_is_alive_true_for_self(host):
    assert host.is_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="WindowsProcessHost is the win32 host")
def test_is_alive_true_for_self_windows():
    # The Windows host probes liveness via psutil (no os.kill), so checking self is
    # safe here — unlike the POSIX host's os.kill(pid, 0), which is CTRL_C on Windows.
    pytest.importorskip("psutil")
    assert WindowsProcessHost().is_alive(os.getpid()) is True


def test_is_alive_rejects_non_positive(host, monkeypatch):
    # 0/negative would target a process group via os.kill — the guard must short
    # out before os.kill is ever reached.
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    assert host.is_alive(0) is False
    assert host.is_alive(-1) is False


def test_terminate_rejects_non_positive(host, monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    host.terminate(0)  # no raise, no signal
    host.terminate(-42)


def test_force_kill_rejects_non_positive(host, monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(process_host.os, "kill", _boom)
    host.force_kill(0)  # no raise, no signal
    host.force_kill(-42)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="identity via /proc is Linux-only")
def test_identity_stable_and_present_for_self(host):
    first = host.identity(os.getpid())
    second = host.identity(os.getpid())
    assert isinstance(first, float)
    assert first == second  # stable for the life of the pid → a usable reuse guard


def test_identity_none_for_non_positive(host):
    assert host.identity(0) is None
    assert host.identity(-1) is None


def test_alive_and_ours_rejects_non_positive(host):
    assert host.alive_and_ours(0, 1.0) is False
    assert host.alive_and_ours(-1, None) is False


def test_alive_and_ours_none_identity_degrades_to_is_alive(host, monkeypatch):
    # A legacy pid file (no persisted identity) can only fall back to bare existence.
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == 4242)
    assert host.alive_and_ours(4242, None) is True
    assert host.alive_and_ours(9999, None) is False


def test_alive_and_ours_matches_only_same_identity(host, monkeypatch):
    # Same identity → our process; a different value (reused pid) or None (gone) → not.
    monkeypatch.setattr(host, "identity", lambda pid: 123.0)
    assert host.alive_and_ours(4242, 123.0) is True
    assert host.alive_and_ours(4242, 999.0) is False  # reused
    # Unreadable identity is not-ours whether the pid is gone or still running —
    # 'unknown' must never read as ours on the strict/destructive path. Stub
    # is_alive too: alive_and_ours (via liveness_of) probes it on this branch, and
    # the real PosixProcessHost.is_alive would os.kill on a native-Windows CI
    # runner (WinError 87).
    monkeypatch.setattr(host, "identity", lambda pid: None)
    monkeypatch.setattr(host, "is_alive", lambda pid: False)
    assert host.alive_and_ours(4242, 123.0) is False  # gone
    monkeypatch.setattr(host, "is_alive", lambda pid: True)
    assert host.alive_and_ours(4242, 123.0) is False  # live but unreadable → unknown


def test_liveness_of_reads_alive_dead_and_unknown(host, monkeypatch):
    monkeypatch.setattr(host, "identity", lambda pid: 123.0)
    assert host.liveness_of(4242, 123.0) == "alive"  # identity matches → ours
    assert host.liveness_of(4242, 999.0) == "dead"  # readable-but-different → reused

    monkeypatch.setattr(host, "identity", lambda pid: None)
    monkeypatch.setattr(host, "is_alive", lambda pid: True)
    assert host.liveness_of(4242, 123.0) == "unknown"
    monkeypatch.setattr(host, "is_alive", lambda pid: False)
    assert host.liveness_of(4242, 123.0) == "dead"


def test_liveness_of_legacy_identity_degrades_to_is_alive(host, monkeypatch):
    # A legacy pid file (no persisted identity) can only fall back to bare existence.
    monkeypatch.setattr(host, "is_alive", lambda pid: pid == 4242)
    assert host.liveness_of(4242, None) == "alive"
    assert host.liveness_of(9999, None) == "dead"
    assert host.liveness_of(0, None) == "dead"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="descendants /proc walk is exercised on Linux"
)
def test_descendants_enumerates_transitively(host, tmp_path):
    """A child-of-child (grandchild) is found: the /proc walk is transitive, not
    just direct children. The outer sh backgrounds an inner sh that itself
    backgrounds a sleep and records that grandchild's pid; descendants(outer) must
    contain it. The whole tree runs in its own session so the finally can nuke it
    with a single killpg even if the assertion fails."""
    gc_file = tmp_path / "gc.pid"
    # outer sh -> inner sh (stays a shell: compound body, not exec'd) -> grandchild sleep
    script = f"sh -c 'sleep 300 & echo $! > {gc_file}; wait' & sleep 300"
    proc = subprocess.Popen(["sh", "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not gc_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gc_file.is_file(), "grandchild pid was never recorded"
        gc_pid = int(gc_file.read_text(encoding="utf-8").strip())
        kids = host.descendants(proc.pid)
        assert gc_pid in kids, f"grandchild {gc_pid} not in transitive descendants {kids}"
        # The identity rides along from the SAME /proc read the ancestry came from
        # and must equal the canonical per-pid stamp (the /proc starttime).
        assert kids[gc_pid] == process_host._proc_starttime(gc_pid)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="descendants /proc walk is exercised on Linux"
)
def test_descendants_dead_or_unknown_pid_is_empty(host):
    """The seam contract: a reaped pid, a never-allocated high pid, and a
    non-positive pid all enumerate to {} rather than raising."""
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=10)  # reap it — its pid is now gone
    assert host.descendants(proc.pid) == {}
    assert host.descendants(2**31 - 1) == {}  # never allocated
    assert host.descendants(0) == {}
    assert host.descendants(-1) == {}


def test_psutil_descendants_maps_children_and_never_raises(monkeypatch):
    """The non-Linux/Windows descendant path maps ``Process.children(recursive=True)``
    to a pid → create_time identity snapshot stamped from the SAME enumerated
    ``Process`` objects, and any failure (a raising Process, or a missing-psutil
    ProcessHostError from ``_psutil()`` itself) degrades to {} — the never-raise
    seam contract that the Linux ``/proc`` walk cannot exercise on Linux CI."""

    class _FakeChild:
        def __init__(self, pid, created):
            self.pid = pid
            self._created = created

        def create_time(self):
            return self._created

        def is_running(self):
            return True  # same generation we enumerated — identity confirmed

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            assert recursive is True  # transitive tree, not just direct children
            return [_FakeChild(11, 111.0), _FakeChild(22, 222.0)]

    class _FakePsutil:
        Process = _FakeProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _FakePsutil)
    assert process_host._psutil_descendants(123) == {11: 111.0, 22: 222.0}

    class _BoomProc:
        def __init__(self, pid):
            raise RuntimeError("no such process")  # psutil.NoSuchProcess analogue

    class _BoomPsutil:
        Process = _BoomProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _BoomPsutil)
    assert process_host._psutil_descendants(123) == {}

    def _missing():
        raise process_host.ProcessHostError("psutil missing")

    monkeypatch.setattr(process_host, "_psutil", _missing)
    assert process_host._psutil_descendants(123) == {}


def test_psutil_descendants_omits_pid_reused_before_identity_capture(monkeypatch):
    """PID-generation rollover between enumeration and the identity stamp, in BOTH
    shapes psutil actually produces — the snapshot must OMIT any member it cannot
    authenticate, keep confirmable siblings, and never raise.

    ``_GoneChild`` is the easy shape: the pid vanished and was not reused, so
    ``create_time()`` raises a ``NoSuchProcess`` analogue.

    ``_ReusedChild`` is the shape that actually bites on macOS and the reason the
    ``is_running()`` revalidation exists (#184 review). Per
    ``psutil.Process._get_ident``, the ``LINUX or NETBSD or OSX`` branch binds a
    *monotonic* ident and leaves ``self._create_time`` unset, so ``create_time()``
    is a raw call-time read that happily returns the RECYCLED process's stamp — no
    exception at all. Stamping that would let teardown authenticate and signal an
    unrelated process. Only ``is_running()`` (construction-bound ident vs. a fresh
    ``Process``) reports the generation change. An exception-only fake would pass
    against the buggy code, so this case is what makes the test a regression test."""

    class _GoneChild:
        pid = 11

        def create_time(self):
            raise RuntimeError("process no longer exists")  # NoSuchProcess analogue

        def is_running(self):  # pragma: no cover - create_time raises first
            raise AssertionError("must not be reached: identity read comes first")

    class _ReusedChild:
        """Enumerated as generation A; the pid was recycled before the stamp, so
        ``create_time()`` returns generation B's identity WITHOUT raising."""

        pid = 33

        def create_time(self):
            return 999.0  # generation B's stamp — plausible, and utterly wrong

        def is_running(self):
            return False  # construction-bound ident no longer matches this pid

    class _OkChild:
        pid = 22

        def create_time(self):
            return 222.0

        def is_running(self):
            return True

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return [_GoneChild(), _ReusedChild(), _OkChild()]

    class _FakePsutil:
        Process = _FakeProc

    monkeypatch.setattr(process_host, "_psutil", lambda: _FakePsutil)
    snapshot = process_host._psutil_descendants(123)
    assert snapshot == {22: 222.0}
    # The recycled pid must be absent outright, not stamped with the newcomer's
    # identity — a stamped entry authenticates at teardown and gets signalled.
    assert 33 not in snapshot


def test_default_host_matches_platform(monkeypatch):
    monkeypatch.delenv("BMAD_LOOP_PROCESS_HOST", raising=False)
    get_process_host.cache_clear()
    expected = WindowsProcessHost if sys.platform == "win32" else PosixProcessHost
    assert isinstance(get_process_host(), expected)


def test_env_override_selects_by_name(monkeypatch):
    # The registry selects by name without monkeypatching sys.platform — the hook
    # PR #19's WindowsProcessHost registration relies on.
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "posix")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), PosixProcessHost)

    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), WindowsProcessHost)


def test_unknown_forced_name_raises(monkeypatch):
    # An explicit but unregistered override is a misconfiguration: fail loudly rather
    # than silently fall back to POSIX (on win32 os.kill(pid, 0) is destructive).
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "bogus")
    get_process_host.cache_clear()
    with pytest.raises(ProcessHostError, match="bogus"):
        get_process_host()


def test_register_invalidates_cached_selection(monkeypatch):
    # register_process_host() must clear the singleton cache so a host registered
    # after a prior get_process_host() call is honored without a manual cache_clear.
    monkeypatch.delenv("BMAD_LOOP_PROCESS_HOST", raising=False)
    saved_hosts = list(process_host._HOSTS)
    saved_loaded = process_host._BUILTINS_LOADED
    try:
        get_process_host()  # populate the cache (and load builtins)
        assert get_process_host.cache_info().currsize == 1
        register_process_host("fake", lambda p: False, lambda: PosixProcessHost())
        # no manual cache_clear() — registration is responsible for invalidating it
        assert get_process_host.cache_info().currsize == 0
    finally:
        process_host._HOSTS[:] = saved_hosts
        process_host._BUILTINS_LOADED = saved_loaded
        get_process_host.cache_clear()


def test_shell_quote_posix_uses_shlex():
    # POSIX quoting wraps a path with spaces in single quotes (shlex.quote).
    assert PosixProcessHost().shell_quote("/a b/c.py") == "'/a b/c.py'"


@pytest.mark.parametrize(
    "path,quoted",
    [
        (r"C:\Users\me\.local\bin\bmad-loop.exe", "C:/Users/me/.local/bin/bmad-loop.exe"),
        (r"C:\Program Files\x y\bmad-loop.exe", '"C:/Program Files/x y/bmad-loop.exe"'),
    ],
    ids=["no-spaces", "spaces"],
)
def test_shell_quote_windows_forward_slashes_then_list2cmdline(path, quoted):
    # Hook runners run these under Git Bash, which eats unquoted backslashes
    # (#773): separators become forward slashes, and a path with spaces is still
    # double-quoted (subprocess.list2cmdline), never POSIX single-quoted.
    assert WindowsProcessHost().shell_quote(path) == quoted


def test_hook_interpreter_is_absolute_on_posix(host):
    import sys

    assert PosixProcessHost().hook_interpreter() == PosixProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )


def test_hook_interpreter_windows_uses_absolute_path():
    import sys

    assert WindowsProcessHost().hook_interpreter() == WindowsProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )


def test_hook_interpreter_windows_uses_forward_slashes(monkeypatch):
    class _Absolute:
        def __init__(self, raw):
            self._raw = raw

        def absolute(self):
            return PureWindowsPath(self._raw)

    monkeypatch.setattr(process_host, "Path", _Absolute)
    monkeypatch.setattr(process_host.sys, "executable", r"C:\Program Files\Py 3\python.exe")
    assert WindowsProcessHost().hook_interpreter() == '"C:/Program Files/Py 3/python.exe"'
    monkeypatch.setattr(process_host.sys, "executable", r"C:\Py\python.exe")
    assert WindowsProcessHost().hook_interpreter() == "C:/Py/python.exe"


def _git_bash() -> str | None:
    """Git for Windows' bash — never System32's WSL launcher, which PATH may
    list first."""
    git = shutil.which("git")
    if git:
        bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    bash = shutil.which("bash")
    return bash if bash and "system32" not in bash.lower() else None


def _windows_relay_shaped_command() -> str:
    # A real .exe at a backslash path standing where the relay executable goes.
    if " " in sys.executable:
        pytest.skip("paths with spaces are unsupported on the PowerShell fallback")
    quoted = WindowsProcessHost().shell_quote(sys.executable)
    return f'{quoted} -c "import sys; print(sys.argv[1:])" relay Stop'


@pytest.mark.skipif(sys.platform != "win32", reason="executes a Windows hook command")
def test_windows_hook_command_runs_under_git_bash():
    bash = _git_bash()
    if bash is None:
        pytest.skip("Git Bash is not installed")
    command = _windows_relay_shaped_command()
    ran = subprocess.run([bash, "-c", command], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert "['relay', 'Stop']" in ran.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="executes a Windows hook command")
def test_windows_hook_command_runs_under_powershell():
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    command = _windows_relay_shaped_command()
    ran = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    assert "['relay', 'Stop']" in ran.stdout


def test_hook_interpreter_routed_through_selected_host(monkeypatch):
    # The env override drives the prefix end-to-end, so a Windows host changes the
    # registered hook command with no `sys.platform` branch at the call site.
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    import sys

    assert get_process_host().hook_interpreter() == WindowsProcessHost().shell_quote(
        str(Path(sys.executable).absolute())
    )
