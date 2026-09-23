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
in :mod:`bmad_loop.adapters.multiplexer`.
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

from . import envvars

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
        """The command prefix that runs a bmad-loop python hook script on this
        host, interpolated into the hook registrations `install`/`probe` write
        (the script path + canonical event are appended by the caller). The
        prefix is an absolute interpreter path, quoted for the host shell."""

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

    def descendants(self, pid: int) -> dict[int, float | None]:
        """Transitive descendants of ``pid`` (children, grandchildren, …) as a
        pid → :meth:`identity` snapshot, for the teardown reap that must chase a
        session's whole process tree, not just the pane root — a detached straggler
        (setsid, a double-fork survivor) escapes the pane pgid the window kill
        signals. The identity is captured DURING enumeration — from the same
        ``/proc`` read (Linux) or the same enumerated psutil ``Process`` object —
        so a pid reaped-and-reused after enumeration can never be authenticated by
        a later lookup; a member whose identity can't be captured is OMITTED, never
        returned unstamped. ``None`` values are reserved for platforms that
        genuinely can't stamp one — consumers must treat ``None`` as unconfirmable
        (never signal it).

        NEVER raises: ``{}`` on an unsupported platform, a missing psutil, a gone
        pid, or any read error — this feeds the kill escalation, which must never be
        the thing that raises. The default enumerates via psutil (macOS/Windows);
        the Linux host overrides it with a psutil-free ``/proc`` scan so the core
        stays dep-free there."""
        if pid <= 0:
            return {}
        return _psutil_descendants(pid)

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

    def descendants(self, pid: int) -> dict[int, float | None]:
        if pid <= 0:
            return {}
        if sys.platform.startswith("linux"):
            return _linux_descendants(pid)  # psutil-free: keeps the Linux core dep-free
        return super().descendants(pid)  # macOS: psutil, guarded by the seam's never-raise

    def hook_interpreter(self) -> str:
        return self.shell_quote(str(Path(sys.executable).absolute()))


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
        return self.shell_quote(str(Path(sys.executable).absolute()))

    def shell_quote(self, arg: str) -> str:
        # Hook runners hand these commands to a shell, not CreateProcess: Claude
        # Code uses Git Bash on Windows (PowerShell without it), and Git Bash eats
        # an unquoted backslash — `C:\Users\me\bmad-loop.exe` runs as
        # `C:Usersmebmad-loop.exe` and every session stalls to timeout (#773). So
        # separators become forward slashes, which Windows and Python accept and
        # sh, PowerShell and cmd.exe pass through unchanged; list2cmdline then
        # double-quotes a path with spaces as before. Every caller passes a path
        # (interpreter, relay executable, hook script), so no backslash here is an
        # escape. Known gap: on the PowerShell fallback a double-quoted executable
        # followed by arguments is a parse error without `&` (which would break
        # sh), so paths WITH spaces stay unsupported there. Claude's exec-form
        # `args` would avoid shells entirely but is Claude-only and changes the
        # registered JSON shape older versions mis-run.
        return subprocess.list2cmdline([arg.replace("\\", "/")])


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


def _linux_descendants(pid: int) -> dict[int, float | None]:
    """Transitive descendants of ``pid``, with their pid-reuse identities, from a
    single pass over ``/proc/*/stat`` (Linux only, psutil-free). Each entry's ppid
    (field 4, index 1 after the comm token) AND starttime identity (field 22,
    index 19 — the very value :func:`_proc_starttime` reads) come from ONE read of
    the same ``stat`` line, split on the last ``)`` exactly as
    :func:`_proc_starttime` does — so ancestry and identity are captured atomically
    per process and a pid reused after the scan can never be authenticated by a
    later lookup. BFS out from ``pid``. Best-effort: a process that exits mid-scan
    simply drops out of the map, an unparsable line is omitted (never
    authenticated), and any error yields ``{}`` so the kill seam never raises. A
    ``seen`` set guards against a pid-reuse race fabricating a cycle."""
    try:
        children: dict[int, list[int]] = {}
        identities: dict[int, float] = {}
        for entry in Path("/proc").iterdir():  # portability: Linux-only, guarded by caller
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                after_comm = stat[stat.rindex(")") + 1 :].split()
                ppid = int(after_comm[1])  # field 4 = ppid
                starttime = float(after_comm[19])  # field 22 = starttime, the identity
            except (OSError, ValueError, IndexError):
                continue  # vanished mid-scan / unparsable line — just skip it
            child = int(entry.name)
            children.setdefault(ppid, []).append(child)
            identities[child] = starttime
    except OSError:
        return {}
    out: dict[int, float | None] = {}
    seen: set[int] = set()
    stack = list(children.get(pid, ()))
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        out[child] = identities[child]
        stack.extend(children.get(child, ()))
    return out


def _psutil_descendants(pid: int) -> dict[int, float | None]:
    """Transitive descendants via psutil (non-Linux POSIX + Windows), each stamped
    with the ``create_time()`` of the SAME enumerated ``Process`` object and then
    REVALIDATED against that object's construction-bound identity before it is
    recorded.

    The revalidation is load-bearing, not belt-and-braces — ``create_time()`` alone
    is NOT reuse-safe here. Per ``psutil.Process._get_ident`` (7.2.2), only the
    ``WINDOWS`` branch pre-populates the epoch cache (``self._create_time =
    create_time(fast_only=True)``); the ``LINUX or NETBSD or OSX`` branch binds a
    *monotonic* starttime and leaves ``self._create_time`` unset. So on macOS our
    ``create_time()`` is the FIRST call — a raw call-time kernel read, and
    ``create_time()`` does not consult ``_raise_if_pid_reused()``. A pid reaped and
    reused between ``children()`` and the stamp would therefore hand back the
    NEWCOMER's identity, which teardown would then authenticate successfully and
    signal (#184 review). ``is_running()`` closes that window: it compares the
    ident captured at construction against a freshly built ``Process``, so a
    generation change reads as not-running and the member is omitted.

    Order matters — read the identity, THEN validate, THEN record; validating first
    would reopen the same TOCTOU gap. A member whose identity can't be confirmed is
    OMITTED, never returned unstamped. Blanket-except → ``{}`` so a missing psutil
    or an already-gone pid degrades silently — the never-raise contract the kill
    escalation depends on."""
    out: dict[int, float | None] = {}
    try:
        for child in _psutil().Process(pid).children(recursive=True):
            try:
                identity = child.create_time()
                if not child.is_running():  # generation changed under us — don't stamp it
                    continue
                out[child.pid] = identity
            except Exception:  # nosec B112 - gone/reused mid-walk: omit it
                continue
    except Exception:  # the kill-path seam must never raise
        return {}
    return out


def _psutil():
    """Lazily import psutil — a core dep on Windows, the ``non-linux`` extra on
    macOS — used only for the non-destructive Windows/macOS liveness + identity
    probes. The dep-free core never imports it on Linux; raise a clear, actionable
    error if it's missing where it's needed."""
    try:
        import psutil  # intentional lazy import — keeps the core dep-free
    except ImportError as exc:  # pragma: no cover - exercised only off Linux
        raise ProcessHostError(
            f"process_host: pid operations on {sys.platform!r} need psutil; "
            "on Windows reinstall bmad-loop (psutil is a required dependency there), "
            "on macOS run `pip install 'bmad-loop[non-linux]'`"
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
    selection; ``name`` is the key for the ``BMAD_LOOP_PROCESS_HOST`` override.
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

    ``BMAD_LOOP_PROCESS_HOST`` forces a host by name (test / override hook);
    otherwise the first host whose ``matches(sys.platform)`` is true wins. POSIX is
    the default fallback, so behavior on Linux/macOS is unchanged. Cached — tests
    that flip the env var must call ``get_process_host.cache_clear()``."""
    forced = envvars.process_host()
    _load_builtin_hosts()
    for name, matches, factory in _HOSTS:
        if name == forced or (not forced and matches(sys.platform)):
            return factory()
    if forced:
        # An explicit override that matches nothing is a misconfiguration; never
        # silently fall back to POSIX (on win32 os.kill(pid, 0) is destructive).
        known = ", ".join(name for name, _, _ in _HOSTS) or "(none registered)"
        raise ProcessHostError(
            f"BMAD_LOOP_PROCESS_HOST={forced!r} matches no registered host; known: {known}"
        )
    return PosixProcessHost()  # default fallback
