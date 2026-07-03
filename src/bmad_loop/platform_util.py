"""Cross-platform process primitives.

The pid kill/liveness primitives now live behind the :class:`~bmad_loop.process_host.ProcessHost`
seam; ``terminate_pid``/``pid_alive`` remain here as thin back-compat shims that
delegate to it. ``detach_kwargs`` stays a real implementation — it is spawn
configuration, not a process-lifecycle primitive, so it does not belong on the
host. On Linux/macOS — and WSL, which *is* Linux — these preserve today's exact
behavior; the Windows branch degrades gracefully and is not yet exercised.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from .process_host import get_process_host


def terminate_pid(pid: int) -> None:
    """Politely terminate ``pid``. Back-compat shim over
    :meth:`ProcessHost.terminate` — prefer ``get_process_host().terminate(pid)``
    in new code."""
    get_process_host().terminate(pid)


def pid_alive(pid: int) -> bool:
    """Read-only liveness check for ``pid``. Back-compat shim over
    :meth:`ProcessHost.is_alive` — prefer ``get_process_host().is_alive(pid)`` in
    new code."""
    return get_process_host().is_alive(pid)


def detach_kwargs() -> dict[str, object]:
    """``Popen`` kwargs that detach a child so it outlives its launcher.

    POSIX uses ``start_new_session``; Windows uses a new process group via
    ``creationflags`` (not exercised yet)."""
    if sys.platform == "win32":
        # portability: start_new_session is POSIX-only; CREATE_NEW_PROCESS_GROUP
        # is the Windows analogue.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}  # portability: POSIX detach kwarg; Windows branch above


def is_absolute_path(value: str | Path) -> bool:
    """True if ``value`` is rooted or drive-qualified in *either* POSIX or Windows
    terms — i.e. not safe as a path *inside* the project.

    Purpose-built for the "must be project-relative" guards (profile/manifest):
    ``Path.is_absolute()`` is platform-dependent, so on Windows a POSIX-absolute
    ``/etc/passwd`` reads as *not* absolute and slips a guard built on it. This
    rejects, on every platform: a POSIX root (``/etc/passwd``), a Windows root or
    drive-absolute path (``\\x``, ``C:\\x``), *and* a Windows drive-*relative* path
    (``C:foo`` — technically relative, but still drive-qualified and never a valid
    in-project path). Strictly broader than "absolute"; the extra rejection of
    ``C:foo`` is intentional for these guards. Pair with :func:`has_parent_ref` to
    also reject ``..`` escapes."""
    text = str(value)
    win = PureWindowsPath(text)
    return PurePosixPath(text).is_absolute() or bool(win.drive or win.root)


def has_parent_ref(value: str | Path) -> bool:
    """True if ``value`` contains a ``..`` segment in *either* POSIX or Windows
    terms. ``is_absolute_path`` rejects absolute escapes but not relative ones:
    ``../../etc`` is not absolute yet still climbs out of the project tree. Pair
    the two for a complete "must stay inside the project" guard."""
    text = str(value)
    return ".." in PurePosixPath(text).parts or ".." in PureWindowsPath(text).parts
