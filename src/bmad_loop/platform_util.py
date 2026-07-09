"""Cross-platform process primitives.

The pid kill/liveness primitives now live behind the :class:`~bmad_loop.process_host.ProcessHost`
seam; ``terminate_pid``/``pid_alive`` remain here as thin back-compat shims that
delegate to it. ``detach_kwargs`` stays a real implementation — it is spawn
configuration, not a process-lifecycle primitive, so it does not belong on the
host. On Linux/macOS — and WSL, which *is* Linux — these preserve today's exact
behavior. The file-replace and segment helpers below (``atomic_replace``,
``safe_segment``, ``safe_ref_segment``) are exercised by the platform tests; the pid
kill/liveness Windows branch degrades gracefully and is not yet exercised.

``safe_segment`` and ``safe_ref_segment`` share a contract but not a rule set: the
first coerces a Windows *filename* segment, the second a *git ref* component, and
neither alphabet contains the other (``CON`` is a legal ref and an illegal filename;
``a..b`` is the reverse). Consumers that derive both a directory and a branch from
the same key must run both.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from .process_host import get_process_host

# Windows-only: os.replace (MoveFileExW) fails with ERROR_ACCESS_DENIED (5) or
# ERROR_SHARING_VIOLATION (32) when a concurrent reader holds a handle on the
# target — Python's open() grants no FILE_SHARE_DELETE, so renaming over the open
# file is denied. Readers hold their handle briefly, so a jittered backoff clears
# it; an anti-virus / indexer touch can hold longer, hence the ~5 s worst case.
# POSIX rename-over-open never raises this, so the retry stays win32-gated.
_REPLACE_ATTEMPTS = 12
_REPLACE_BASE_S = 0.02
_REPLACE_CAP_S = 0.7

# Reserved on Windows regardless of extension: CON.txt is as illegal as CON. The
# COM0/LPT0 and superscript (COM¹/COM²/COM³) forms are reserved by the same rule,
# as are the console device names CONIN$/CONOUT$.
_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
    | {f"COM{s}" for s in "¹²³"}
    | {f"LPT{s}" for s in "¹²³"}
)
_ILLEGAL_SEGMENT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_SEGMENT = 120  # keep segment (incl. any collision suffix) well under the 255 limit

# git-check-ref-format(1) rejects these anywhere in a ref component: ASCII control
# chars and space (\x00-\x20), DEL, and `~ ^ : ? * [ \`. `/` is added because it
# would split one component into two. `]`, `-`, `<`, `>`, `"` and `|` are all legal
# in a ref and deliberately absent — this is not _ILLEGAL_SEGMENT_CHARS.
_ILLEGAL_REF_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\/]")


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


def _retry_on_sharing_violation(op: Callable[[], None]) -> None:
    """Run ``op``, retrying the transient Windows sharing violation a concurrent
    handle on the file triggers (WinError 5/32). Gated to win32 so a real POSIX
    EACCES/EPERM surfaces immediately instead of after a pointless backoff.
    Worst-case total wait is ~5 s of jittered exponential backoff before the final
    failure propagates."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            op()
            return
        except OSError as exc:
            last = attempt == _REPLACE_ATTEMPTS - 1
            # a retryable open-handle denial, not a genuine permission fault
            winerror = getattr(exc, "winerror", None)
            retryable = isinstance(exc, PermissionError) or winerror in (5, 32)
            # portability: only Windows denies a rename/delete over an open handle;
            # elsewhere a permission error is real and must surface at once.
            if sys.platform != "win32" or last or not retryable:
                raise
            delay = min(_REPLACE_CAP_S, _REPLACE_BASE_S * 2**attempt)
            time.sleep(delay + random.uniform(0, _REPLACE_BASE_S))  # nosec B311 - retry jitter


def atomic_replace(tmp: Path, target: Path) -> None:
    """``os.replace(tmp, target)``, retried on the transient Windows sharing
    violation a concurrent reader of ``target`` triggers."""
    _retry_on_sharing_violation(lambda: os.replace(tmp, target))


def retrying_unlink(path: Path) -> None:
    """``path.unlink()`` with the same win32 retry as :func:`atomic_replace`.

    Windows denies a *delete* against an open handle exactly as it denies a
    rename-over, so the second half of a staged move is no safer than the first:
    an AV/indexer scanning the just-written source file fails the unlink. Pair the
    two whenever a move must not half-apply."""
    _retry_on_sharing_violation(path.unlink)


def _is_reserved_basename(seg: str) -> bool:
    """True if ``seg``'s basename (before the first dot, trailing spaces trimmed —
    ``CON .txt`` counts) is a Windows reserved device name."""
    stem = seg.split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED_BASENAMES


def _digest_suffix(name: str) -> str:
    """The ``-<hex8>`` collision suffix both sanitizers append to changed input."""
    digest = hashlib.sha1(
        name.encode("utf-8", "surrogatepass"),
        usedforsecurity=False,  # collision-resistance suffix, not a credential
    ).hexdigest()
    return "-" + digest[:8]


def safe_segment(name: str) -> str:
    """Coerce ``name`` into a single Windows-legal path segment, returning legal
    input unchanged (identity for clean keys — the common case, e.g. a story key
    like ``3-2-digest-delivery``).

    Replaces the reserved characters ``<>:"/\\|?*`` and control chars with ``_``,
    strips trailing dots and spaces (Windows silently drops them), caps the length,
    and defuses the reserved device basenames (CON, PRN, AUX, NUL, COM0-9, LPT0-9
    and their superscript ¹²³ forms — case-insensitive, with or without an
    extension). Whenever anything is changed a short digest of the raw input is
    appended, giving practical (probabilistic, not absolute) collision resistance
    between distinct raw names: clean-key identity is the stronger contract, so a
    clean name that happens to look like a sanitized-plus-digest name passes
    through verbatim, and case-insensitive NTFS collisions between clean names
    remain the caller's concern. Never raises."""
    cleaned = _ILLEGAL_SEGMENT_CHARS.sub("_", name).rstrip(". ")[:_MAX_SEGMENT]
    if _is_reserved_basename(cleaned):
        cleaned = "_" + cleaned
    if not cleaned:
        cleaned = "_"
    if cleaned == name:
        return name  # already a legal segment — keep it byte-identical
    suffix = _digest_suffix(name)
    return cleaned[: _MAX_SEGMENT - len(suffix)] + suffix


def _is_clean_ref_segment(seg: str) -> bool:
    """True if ``seg`` already satisfies git's rules for one ref component.

    Mirrors ``git check-ref-format``'s per-component checks. The length cap is
    ours, not git's: it keeps a branch segment in lockstep with the ``safe_segment``
    directory built from the same key."""
    return (
        bool(seg)
        and len(seg) <= _MAX_SEGMENT
        and not _ILLEGAL_REF_CHARS.search(seg)
        and ".." not in seg
        and "@{" not in seg
        and seg != "@"
        and not seg.startswith(".")
        and not seg.endswith((".", ".lock"))
    )


def safe_ref_segment(name: str) -> str:
    """Coerce ``name`` into a single git-ref-legal component, returning legal input
    unchanged (identity for clean keys — the common case, e.g. a story key like
    ``3-2-digest-delivery`` or an auto-generated run id).

    Same contract as :func:`safe_segment` — identity for clean input, a short digest
    of the raw name appended whenever anything changed, never raises — but git's
    alphabet, not Windows': replaces control chars, space, DEL and ``~^:?*[\\/`` with
    ``_``, rewrites ``..`` → ``__`` and ``@{`` → ``_{``, escapes a leading ``.``, and
    caps the length. Trailing ``.`` and trailing ``.lock`` are ref-illegal but need no
    rewrite: they only reach the coercion path, and the ``-<hex8>`` suffix appended
    there is itself the fix. A lone ``@`` is coerced to ``_`` even though git only
    forbids it as a whole ref name, so the contract holds for any caller.

    A leading ``-`` is deliberately preserved: it is legal in a ref component, and
    the git porcelain's separate "branch name must not start with ``-``" check reads
    the whole name, which callers always prefix (``bmad-loop/<run_id>/<segment>``).

    Digest collision resistance is probabilistic, and clean-key identity is the
    stronger contract — so a clean name that happens to look sanitized-plus-digest
    passes through verbatim."""
    if _is_clean_ref_segment(name):
        return name  # already a legal ref component — keep it byte-identical
    cleaned = _ILLEGAL_REF_CHARS.sub("_", name).replace("..", "__").replace("@{", "_{")
    if cleaned.startswith("."):
        cleaned = "_" + cleaned[1:]
    if not cleaned or cleaned == "@":
        cleaned = "_"
    suffix = _digest_suffix(name)
    return cleaned[: _MAX_SEGMENT - len(suffix)] + suffix
