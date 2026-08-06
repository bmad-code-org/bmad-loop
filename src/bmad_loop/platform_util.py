"""Cross-platform process primitives.

The pid kill/liveness primitives now live behind the :class:`~bmad_loop.process_host.ProcessHost`
seam; ``terminate_pid``/``pid_alive`` remain here as thin back-compat shims that
delegate to it. ``detach_kwargs`` stays a real implementation — it is spawn
configuration, not a process-lifecycle primitive, so it does not belong on the
host. On Linux/macOS — and WSL, which *is* Linux — these preserve today's exact
behavior. The file-replace and segment helpers below (``atomic_replace``,
``atomic_write_text``, ``atomic_write_bytes``, ``safe_segment``,
``safe_ref_segment``) are exercised by the platform tests; the pid kill/liveness
Windows branch degrades gracefully and is not yet exercised.

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
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterator

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
MAX_SEGMENT = 120  # keep segment (incl. any collision suffix) well under the 255 limit

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


def names_tree_root(value: str | Path) -> bool:
    """True if ``value`` names the tree it is relative to rather than anything
    *inside* it: ``""``, ``"."``, ``"./"``, ``"./."`` all normalize to the root.

    The third member of the "must be a path inside the project" family, and the
    one a `not value` emptiness check misses. It exists because these guards feed
    `provision_worktree`'s seed loop, where a root-naming entry resolves ``src``
    to the repo root and ``dst`` to the worktree, both of which pass the loop's
    ``is_relative_to`` containment checks — a path is relative to itself. ``""``
    and ``"."`` produce a byte-identical ``(src, raw, dst)`` triple there
    (``PurePosixPath("") == PurePosixPath(".")``), so a guard rejecting only the
    first is a guard against one spelling.

    Both flavours are checked for the same reason :func:`is_absolute_path` checks
    both: ``".\\"`` is a root ref Windows normalizes away and POSIX parsing keeps
    as an ordinary one-segment name. Pair with the other two for a complete
    "must stay inside the project, and must name something in it" guard."""
    text = str(value)
    return PurePosixPath(text) == PurePosixPath(".") or PureWindowsPath(text) == PureWindowsPath(
        "."
    )


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


def _copy_xattrs(src: Path, dst: Path) -> None:
    """Best-effort extended-attribute copy (Linux). Absent everywhere else, and
    unsupported by many filesystems even there, so every failure is ignored: an
    xattr we could not carry over is not worth failing a ledger write for."""
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:  # portability: xattr syscalls are Linux-only
        return
    try:
        names = listxattr(src)
    except OSError:
        return
    for name in names:
        try:
            os.setxattr(dst, name, os.getxattr(src, name))
        except OSError:
            continue


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` atomically, preserving what the
    replacement would otherwise silently discard.

    ``os.replace`` swaps a *new inode* into place, so a naive tmp-write-and-replace
    quietly resets everything carried by the old file rather than by its name. This
    restores the parts that matter:

    * **Symlinks are followed.** ``path.resolve()`` first, so a ledger symlinked
      into the repo keeps being a symlink and the real file is what gets rewritten
      — a replace against the link itself would turn it into a regular file and
      orphan the target.
    * **Permission bits survive.** A ``0600`` file stays ``0600`` instead of
      becoming ``0644 & ~umask``, which on a shared artifact dir is the difference
      between "the group can still write this" and a silent lockout (or a
      disclosure).
    * **Extended attributes survive** where the platform has them (best effort).

    Ownership is NOT preserved — an unprivileged process cannot chown — so a file
    written by another user changes hands. Callers writing genuinely shared,
    multi-user state need more than this helper. A target that does not exist yet
    is created with ``mkstemp``'s private ``0600``, not the umask default: there is
    no prior mode to carry over, and the restrictive choice is the safe one.

    The temp file is uniquely named in the target's own directory: same filesystem
    (``os.replace`` cannot cross one), and no fixed ``.tmp`` sibling for a
    concurrent writer of the same file to collide with. A failure anywhere leaves
    the original untouched and removes the temp.

    The contents are **fsynced before the replace publishes them**. Closing a file
    only hands the data to the page cache, so a machine that loses power just
    after the rename can come back with the new name pointing at blocks that were
    never written — a zero-length or torn ledger, which parses as *no entries* and
    so reads as the whole file's worth of hand-written work having vanished.
    Ordering the flush before the rename means a crash yields either the old file
    or the complete new one. The directory itself is deliberately not synced: that
    would make the *rename* durable, and losing the rename just leaves the old
    contents in place — stale, never corrupt.

    The bytes sibling is :func:`atomic_write_bytes`; the two share every property
    above and differ only in the ``os.fdopen`` mode. Text mode's *newline*
    default (translating) is deliberate here — it matches the ``Path.write_text``
    this replaced, so a ledger's line endings do not change under Windows."""
    _atomic_write(path, text, mode="w", encoding="utf-8")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path``'s contents with ``data`` atomically — the byte-exact
    sibling of :func:`atomic_write_text`, whose docstring carries the shared
    contract (symlinks followed, mode and xattrs preserved when the target already
    exists, a fresh target left at ``mkstemp``'s private ``0600``, fsync before the
    replace, temp removed on any failure).

    The one difference is the whole point: ``data`` lands byte-for-byte. No encode
    and no newline translation, so a payload carrying LF keeps LF on Windows and
    bytes that are not valid text in any codec survive the round trip. Callers
    handling filesystem-derived content want this variant — a POSIX filename is
    arbitrary bytes, and an operator's git exclude file may be in any legacy
    encoding at all."""
    _atomic_write(path, data, mode="wb", encoding=None)


def _atomic_write(path: Path, payload: str | bytes, *, mode: str, encoding: str | None) -> None:
    """The shared body of the two public helpers above — see
    :func:`atomic_write_text` for the contract every step here implements.

    Written through ``os.fdopen`` rather than a raw ``os.write`` loop on purpose:
    it routes to ``io.open``, the one seam a test can inject a short write at for
    both variants at once (tests/test_install.py's #375 case)."""
    target = path.resolve()
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, mode, encoding=encoding) as fh:
            fh.write(payload)
            fh.flush()  # userspace buffer -> kernel, so there is something to sync
            os.fsync(fh.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
            _copy_xattrs(target, tmp)
        atomic_replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def retrying_unlink(path: Path) -> None:
    """``path.unlink()`` with the same win32 retry as :func:`atomic_replace`.

    Windows denies a *delete* against an open handle exactly as it denies a
    rename-over, so the second half of a staged move is no safer than the first:
    an AV/indexer scanning the just-written source file fails the unlink. Pair the
    two whenever a move must not half-apply."""
    _retry_on_sharing_violation(path.unlink)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Exclusive OS advisory lock on ``path`` (created if missing), released on
    exit — and by the kernel when the holder dies, so a crashed process never
    wedges the lock (no stale-lockfile scheme to clean up). ``blocking=False``
    raises ``OSError`` at once when the lock is already held, giving tests a
    deterministic exclusion probe instead of a sleep-based negative assertion.

    Lock a dedicated sibling file, never data that is swapped via
    :func:`atomic_replace` — the lock rides the open fd's inode, and a replace
    would swap that inode out from under later acquirers. ``fcntl.flock`` on
    POSIX; ``msvcrt.locking`` on Windows, where the blocking mode's built-in
    ~10 s retry bounds the wait and surfaces contention as ``OSError``.

    THE WAIT IS PLATFORM-ASYMMETRIC, and a caller has to decide what that means
    for it: POSIX blocks indefinitely, Windows gives up after ~10 s and raises.
    This docstring used to add "holders only do brief file I/O" — true when the
    only consumers were tests, and falsified by the first production caller
    (``install._worktree_local_exclude``, #384), which holds it across a
    multi-step git transaction of roughly seven ``git`` spawns, each bounded by
    ``[limits] git_timeout_s``. So a Windows acquirer can genuinely time out
    under contention rather than only under a deadlock. Hold it for as short a
    span as correctness allows, and handle the ``OSError`` from acquisition.

    OWNER-ONLY, AND DELIBERATELY NOT MADE TO WORK ACROSS OS USERS. A repository
    shared between OS users is not a supported configuration (maintainer
    decision, #384); ``install._shield_shared_repository`` refuses one up front,
    above the shield's acquisition of this lock, so no lock file is created there
    at all. That gate is where the case is handled — not here. Three "fixes"
    belong to it rather than to this function, and each is worse than it looks:
    widening the mode (from the umask, from ``.git``'s own bits, from a
    ``core.sharedRepository`` read) hands a peer write access to a file this
    process is relying on; falling back to ``O_RDONLY`` when the open fails
    rescues only the modes that happen to grant group read; and unlinking a
    badly-moded lock to recreate it is the actively dangerous one — a lock
    another process currently HOLDS would be replaced by a new inode, ``flock``
    exclusion rides the inode, and both processes would then believe they hold
    it, rebuilding the concurrency bug this lock was added to prevent.

    The explicit ``0o600`` states that policy rather than leaving it to the
    caller's umask, which would make the mode a property of whoever provisioned
    first (measured: umask 022 gives 0o755, umask 077 gives 0o700 — both
    arbitrary). It is a ceiling, not a floor: ``os.open``'s mode is still masked,
    so an unusual umask can only narrow it further, never widen it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            # Locks 1 byte at the current position — 0 on a fresh fd.
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                # Unlock the same 1-byte region before close; POSIX flock is
                # released by the close itself.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


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
    cleaned = _ILLEGAL_SEGMENT_CHARS.sub("_", name).rstrip(". ")[:MAX_SEGMENT]
    if _is_reserved_basename(cleaned):
        cleaned = "_" + cleaned
    if not cleaned:
        cleaned = "_"
    if cleaned == name:
        return name  # already a legal segment — keep it byte-identical
    suffix = _digest_suffix(name)
    return cleaned[: MAX_SEGMENT - len(suffix)] + suffix


def _is_clean_ref_segment(seg: str) -> bool:
    """True if ``seg`` already satisfies git's rules for one ref component.

    Mirrors ``git check-ref-format``'s per-component checks. The length cap is
    ours, not git's: it keeps a branch segment in lockstep with the ``safe_segment``
    directory built from the same key."""
    return (
        bool(seg)
        and len(seg) <= MAX_SEGMENT
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
    return cleaned[: MAX_SEGMENT - len(suffix)] + suffix
