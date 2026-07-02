"""Tests for the cross-platform process-lifecycle seam."""

from __future__ import annotations

import os
import sys

import pytest

from automator import process_host
from automator.process_host import (
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


def test_default_host_matches_platform(monkeypatch):
    monkeypatch.delenv("BMAD_AUTO_PROCESS_HOST", raising=False)
    get_process_host.cache_clear()
    expected = WindowsProcessHost if sys.platform == "win32" else PosixProcessHost
    assert isinstance(get_process_host(), expected)


def test_env_override_selects_by_name(monkeypatch):
    # The registry selects by name without monkeypatching sys.platform — the hook
    # PR #19's WindowsProcessHost registration relies on.
    monkeypatch.setenv("BMAD_AUTO_PROCESS_HOST", "posix")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), PosixProcessHost)

    monkeypatch.setenv("BMAD_AUTO_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    assert isinstance(get_process_host(), WindowsProcessHost)


def test_unknown_forced_name_raises(monkeypatch):
    # An explicit but unregistered override is a misconfiguration: fail loudly rather
    # than silently fall back to POSIX (on win32 os.kill(pid, 0) is destructive).
    monkeypatch.setenv("BMAD_AUTO_PROCESS_HOST", "bogus")
    get_process_host.cache_clear()
    with pytest.raises(ProcessHostError, match="bogus"):
        get_process_host()


def test_register_invalidates_cached_selection(monkeypatch):
    # register_process_host() must clear the singleton cache so a host registered
    # after a prior get_process_host() call is honored without a manual cache_clear.
    monkeypatch.delenv("BMAD_AUTO_PROCESS_HOST", raising=False)
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


def test_shell_quote_windows_uses_list2cmdline():
    # Windows quoting double-quotes a path with spaces (subprocess.list2cmdline),
    # never single-quoting — POSIX single-quotes would mangle a Windows command.
    quoted = WindowsProcessHost().shell_quote(r"C:\a b\c.py")
    assert quoted == '"C:\\a b\\c.py"'


def test_hook_interpreter_is_python3_on_posix(host):
    # Hook registrations (install/probe) interpolate this prefix; POSIX keeps the
    # historical `python3` byte-for-byte so existing configs stay valid.
    assert PosixProcessHost().hook_interpreter() == "python3"


def test_hook_interpreter_windows_resolves_without_project_venv():
    # Windows has no `python3` launcher — `uv run` resolves an interpreter, and
    # `--no-project` keeps it from activating a project venv for a detached hook.
    assert WindowsProcessHost().hook_interpreter() == "uv run --no-project python"


def test_hook_interpreter_routed_through_selected_host(monkeypatch):
    # The env override drives the prefix end-to-end, so a Windows host changes the
    # registered hook command with no `sys.platform` branch at the call site.
    monkeypatch.setenv("BMAD_AUTO_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    assert get_process_host().hook_interpreter() == "uv run --no-project python"
