"""Cross-platform process-lifecycle primitives behind a single seam.

The orchestrator needs four operations on a pid it launched: politely stop it,
force-kill it when it ignores that, check whether it is still alive, and — to
guard against pid reuse before a force-kill — read a stable per-process identity.
On POSIX these are ``os.kill`` calls; on Windows they are ``taskkill`` / psutil.
Quarantining them behind :class:`ProcessHost` lets a native-Windows backend slot
in as a new subclass + one registration line, with no edits to the POSIX bodies
or to the callers (`runs.stop_run`, the TUI liveness column).

On Linux/macOS — and WSL, which *is* Linux — these preserve today's exact
behavior; the Windows branch degrades gracefully and is not yet exercised (no
Windows backend ships in this pass). Selection mirrors the multiplexer registry
in :mod:`automator.adapters.multiplexer`.
"""

from __future__ import annotations

import functools
import os
import shlex
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

# SIGKILL is absent on Windows; fall back to SIGTERM so attribute access never
# raises. The POSIX host references this rather than ``signal.SIGKILL`` directly.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)  # portability: SIGKILL absent on Windows


class ProcessHostError(Exception):
    """A process-lifecycle operation could not be carried out on this platform."""


class ProcessHost(ABC):
    """The four pid operations the orchestrator needs, abstracted over the OS."""

    @abstractmethod
    def terminate(self, pid: int) -> None:
        """Politely ask ``pid`` to stop (POSIX SIGTERM / Windows ``taskkill``).
        Raises the ``OSError`` family (``ProcessLookupError``/``PermissionError``)
        so callers keep their "already gone / not ours" handling."""

    @abstractmethod
    def force_kill(self, pid: int) -> None:
        """Forcibly kill ``pid`` (POSIX SIGKILL / Windows ``taskkill /F /T``). The
        escalation when ``terminate`` is ignored; only call once identity is
        confirmed, never on a possibly-reused pid."""

    @abstractmethod
    def is_alive(self, pid: int) -> bool:
        """Read-only liveness check for ``pid`` (no signal sent)."""

    @abstractmethod
    def identity(self, pid: int) -> float | None:
        """A value that stays constant for the life of ``pid`` but changes if the
        pid is reused — a PID-reuse guard for the force-kill escalation. ``None``
        where the platform can't provide one (callers must refuse to force-kill
        rather than risk an unrelated process)."""

    @abstractmethod
    def hook_interpreter(self) -> str:
        """The command prefix that runs a bmad-auto python hook script on this
        host, interpolated into the hook registrations `install`/`probe` write
        (the script path + canonical event are appended by the caller). POSIX runs
        the ``python3`` on PATH; a Windows host overrides it (no ``python3`` there)
        so hook registration never branches on ``sys.platform`` at the call site."""

    def alive_and_ours(self, pid: int, identity: float | None) -> bool:
        """Identity-aware liveness: True only when ``pid`` is alive **and** still the
        same process whose ``identity`` we recorded. A reused pid (immediate on
        Windows) fails the identity match and reads as gone — the reuse guard the
        bare :meth:`is_alive` existence probe lacks.

        ``identity is None`` (a legacy pid file with no persisted identity, or a
        platform that can't provide one) degrades to :meth:`is_alive` — today's
        bare-existence behavior, with the documented residual reuse risk. Kept
        distinct from :meth:`is_alive` so existence and ownership are never
        conflated again.

        Destructive paths use this strict check; non-destructive TUI reads use
        :meth:`liveness_of` to preserve an ``'unknown'`` state. Derived from
        :meth:`liveness_of` — one decision table, two projections — so the binary
        and tri-state probes can never drift: gone, reused, or unreadable
        (``'unknown'``) all read not-ours here."""
        return self.liveness_of(pid, identity) == "alive"

    def liveness_of(self, pid: int, identity: float | None) -> str:
        """Non-destructive tri-state read of *our* engine: ``'alive'`` |
        ``'dead'`` | ``'unknown'``. A pid that still exists but whose identity is
        unreadable reads ``'unknown'``, never ``'dead'``."""
        if pid <= 0:
            return "dead"
        if identity is None:
            return "alive" if self.is_alive(pid) else "dead"
        current = self.identity(pid)
        if current == identity:
            return "alive"
        if current is None and self.is_alive(pid):
            return "unknown"
        return "dead"

    def shell_quote(self, arg: str) -> str:
        """Quote ``arg`` for the shell that runs this host's hook commands, so the
        argument-quoting axis sits behind the same seam as ``hook_interpreter``. Not
        abstract: the default is POSIX ``shlex.quote``; a Windows host overrides it
        (POSIX quoting mangles ``C:\\Program Files\\...`` paths)."""
        return shlex.quote(arg)


class PosixProcessHost(ProcessHost):
    """Linux/macOS/WSL: ``os.kill`` for signalling and the read-only existence
    probe; ``/proc`` start-time (Linux) or psutil create-time for identity."""

    def terminate(self, pid: int) -> None:
        if pid <= 0:
            # 0/negative target a process group (0 is the caller's own group), never
            # a specific process — refuse so a corrupt pid file can't signal us.
            return
        os.kill(pid, signal.SIGTERM)

    def force_kill(self, pid: int) -> None:
        if pid <= 0:
            return
        os.kill(pid, SIGKILL)

    def is_alive(self, pid: int) -> bool:
        if pid <= 0:
            # 0/negative target a process group, not a specific process — a corrupt
            # pid file must read as "not alive", never as the caller's own group.
            return False
        try:
            os.kill(pid, 0)  # portability: read-only existence probe (POSIX); win32 uses psutil
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, just not ours to signal
        return True

    def identity(self, pid: int) -> float | None:
        if pid <= 0:
            return None
        if sys.platform.startswith("linux"):
            return _proc_starttime(pid)
        # macOS (and any non-Linux POSIX): no /proc — fall back to psutil if the
        # optional extra is present, else give up (None → callers won't force-kill).
        try:
            return _psutil().Process(pid).create_time()
        except Exception:
            return None

    def hook_interpreter(self) -> str:
        return "python3"


class WindowsProcessHost(ProcessHost):
    """Native Windows: ``taskkill`` for signalling, psutil for the non-destructive
    liveness probe and create-time identity. Not exercised in this pass — kept so a
    psmux-class backend can register it without editing the POSIX bodies above."""

    def terminate(self, pid: int) -> None:
        if pid <= 0:
            return
        # portability: no os.kill(SIGTERM) on Windows — taskkill is the analogue.
        subprocess.run([_taskkill(), "/PID", str(pid)], check=False, capture_output=True)

    def force_kill(self, pid: int) -> None:
        if pid <= 0:
            return
        # portability: SIGKILL has no Windows analogue — taskkill /F /T force-kills
        # the process and its child tree.
        subprocess.run(
            [_taskkill(), "/F", "/T", "/PID", str(pid)], check=False, capture_output=True
        )

    def is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        return _psutil().pid_exists(pid)

    def identity(self, pid: int) -> float | None:
        if pid <= 0:
            return None
        try:
            return _psutil().Process(pid).create_time()
        except Exception:
            return None

    def hook_interpreter(self) -> str:
        # Windows ships no `python3` launcher; `uv run --no-project python` resolves
        # an interpreter without activating a project venv (hooks fire detached).
        return "uv run --no-project python"

    def shell_quote(self, arg: str) -> str:
        # POSIX single-quoting breaks Windows paths; list2cmdline is the stdlib's
        # Windows argument quoter (the inverse of how CreateProcess parses argv).
        return subprocess.list2cmdline([arg])


def _proc_starttime(pid: int) -> float | None:
    """The process's start time (clock ticks since boot) from ``/proc/<pid>/stat``
    field 22 — stable for the life of the pid, so it doubles as a reuse guard. The
    comm field (2) is wrapped in parens and may itself contain spaces/parens, so we
    split on the last ``)`` before tokenizing the rest. ``None`` if the process is
    gone or unreadable."""
    try:
        proc = Path("/proc")  # portability: Linux-only, guarded by identity()'s platform check
        stat = proc.joinpath(str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after_comm = stat[stat.rindex(")") + 1 :].split()
        return float(after_comm[19])  # field 22 = index 19 after the comm token
    except (ValueError, IndexError):
        return None


def _psutil():
    """Lazily import psutil (the optional ``non-linux`` extra), used only for the
    non-destructive Windows/macOS liveness + identity probes. The dep-free core
    never imports it on Linux; raise a clear, actionable error if it's missing where
    it's needed."""
    try:
        import psutil  # noqa: PLC0415  (intentional lazy import — keeps the core dep-free)
    except ImportError as exc:  # pragma: no cover - exercised only off Linux
        raise ProcessHostError(
            f"process_host: pid operations on {sys.platform!r} need psutil; "
            "install the optional extra (pip install 'bmad-auto[non-linux]') or run "
            "under Linux/WSL"
        ) from exc
    return psutil


def _taskkill() -> str:
    """Absolute path to the Windows ``taskkill`` binary. Resolving it from
    ``%SystemRoot%\\System32`` rather than invoking ``taskkill`` by name keeps the
    Windows process-search order from picking up a same-named executable planted on
    PATH or in the working directory."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")


# (name, matches(platform) -> bool, factory() -> ProcessHost)
_HOSTS: list[tuple[str, Callable[[str], bool], Callable[[], ProcessHost]]] = []
_BUILTINS_LOADED = False


def register_process_host(
    name: str,
    matches: Callable[[str], bool],
    factory: Callable[[], ProcessHost],
) -> None:
    """Register a process host. ``matches(sys.platform)`` decides automatic
    selection; ``name`` is the key for the ``BMAD_AUTO_PROCESS_HOST`` override.
    Bundled hosts register from :func:`_load_builtin_hosts`; an out-of-tree host
    calls this at import time — no core edit required."""
    _HOSTS.append((name, matches, factory))
    get_process_host.cache_clear()  # a later registration must not be shadowed by a cached pick


def _load_builtin_hosts() -> None:
    """Register the bundled hosts. Idempotent and lazy (called from
    :func:`get_process_host`) to match the multiplexer registry's shape; both
    builtins live in this module, so there is nothing to import."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    register_process_host("posix", lambda platform: platform != "win32", PosixProcessHost)
    register_process_host("windows", lambda platform: platform == "win32", WindowsProcessHost)


@functools.lru_cache(maxsize=1)
def get_process_host() -> ProcessHost:
    """Return the process-wide process host, selected by registry.

    ``BMAD_AUTO_PROCESS_HOST`` forces a host by name (test / override hook);
    otherwise the first host whose ``matches(sys.platform)`` is true wins. POSIX is
    the default fallback, so behavior on Linux/macOS is unchanged. Cached — tests
    that flip the env var must call ``get_process_host.cache_clear()``."""
    forced = os.environ.get("BMAD_AUTO_PROCESS_HOST")
    _load_builtin_hosts()
    for name, matches, factory in _HOSTS:
        if name == forced or (not forced and matches(sys.platform)):
            return factory()
    if forced:
        # An explicit override that matches nothing is a misconfiguration; never
        # silently fall back to POSIX (on win32 os.kill(pid, 0) is destructive).
        known = ", ".join(name for name, _, _ in _HOSTS) or "(none registered)"
        raise ProcessHostError(
            f"BMAD_AUTO_PROCESS_HOST={forced!r} matches no registered host; known: {known}"
        )
    return PosixProcessHost()  # default fallback
