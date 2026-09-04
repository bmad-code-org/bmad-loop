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

import errno
import hashlib
import os
import random
import re
import shutil
import stat
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

# Windows-only: the "name does not fit" errno is not ENAMETOOLONG. CPython's
# PC/errmap.h maps ERROR_FILENAME_EXCED_RANGE (206) to ENOENT and does not map
# ERROR_BUFFER_OVERFLOW (111) at all — it falls to the EINVAL default — so
# ENAMETOOLONG is effectively unreachable there and only .winerror tells.
# ERROR_INVALID_NAME (123) is deliberately NOT here: it also fires for a name
# holding characters win32 forbids outright, which no shorter prefix fixes.
_WINERROR_FILENAME_EXCED_RANGE = 206

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
    ``is_relative_to`` containment checks — a path is relative to itself. Measured:
    ``""`` and ``"."`` produce a byte-identical ``(src, raw, dst)`` triple there,
    so a guard rejecting only the first is a guard against one spelling.

    Both flavours are checked for the same reason :func:`is_absolute_path` checks
    both: ``".\\"`` is a root ref Windows normalizes away and POSIX parsing keeps
    as an ordinary one-segment name. Pair with the other two for a complete
    "must stay inside the project, and must name something in it" guard.

    The dot/space spellings are the same asymmetry one layer down. Win32 path
    normalization strips *every* trailing period and space from a path's final
    component, so ``". "``, ``".. "``, ``"..."`` and even ``"   "`` all name the
    containing directory there, while both pure flavours keep them as ordinary
    one-segment names (pathlib never applies that trim — only ``resolve()``, by
    asking the OS, does). A component made solely of periods and spaces is
    therefore root-naming, and that is where *this* predicate's rule stops:
    ``"foo. "`` strips to ``"foo"``, names a child, and is accepted here. It is
    still refused, by :func:`names_win32_alias`, on the ground this predicate does
    not speak to: the trim leaves ``"foo. "`` inside the tree, so containment has
    nothing to object to, but it does not name the same path on Windows as it does
    on POSIX, and that determinism rule is the fourth member's.

    ``".. "`` lands here rather than in :func:`has_parent_ref` because the trailing
    space stops it matching the ``..`` relative component, so Win32 trims it to
    empty instead of climbing — it names the root, not the parent. That reading is
    Wine's conformance suite and Project Zero's write-up; Microsoft's own docs are
    ambiguous on the trim-vs-relative-component ordering. Nothing rests on
    resolving it: under the other reading ``".. "`` escapes the tree, and the
    call sites that pair the two guards reject it either way. Plain ``".."`` is
    unchanged and stays :func:`has_parent_ref`'s job."""
    text = str(value)
    if PurePosixPath(text) == PurePosixPath(".") or PureWindowsPath(text) == PureWindowsPath("."):
        return True
    # `/` separates on both platforms, `\` only on Windows — split on both so a
    # value is judged by the same components Win32 would see.
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    return bool(parts) and all(part.strip(" .") == "" and part != ".." for part in parts)


def names_win32_alias(value: str | Path) -> bool:
    """True if any component of ``value`` names something other than itself on
    Win32 — a reserved device name, or a name whose trailing periods and spaces
    Win32 trims away before the path ever reaches the filesystem.

    The fourth member of the "must be a path inside the project" family, and the
    only one about *determinism* rather than containment. The other three refuse a
    value that leaves the tree; this one refuses a value that stays inside it and
    still names a *different* path on Windows than it does on POSIX.
    ``skill_tree = "NUL"`` is project-relative by every measure the other three
    apply, and on Windows it is a device rather than a directory. Two rules, both
    applied per component — both separators are split for the same reason
    :func:`names_tree_root` splits both.

    **Rule 1 — reserved device basenames.** ``_RESERVED_BASENAMES`` holds ``CON``,
    ``PRN``, ``AUX``, ``NUL``, the console pair ``CONIN$``/``CONOUT$``, ``COM0``
    through ``COM9``, ``LPT0`` through ``LPT9``, and the ISO-8859-1 superscript
    ``COM¹``/``COM²``/``COM³`` and ``LPT¹``/``LPT²``/``LPT³`` forms.
    :func:`_is_reserved_basename` compares case-insensitively, with or without an
    extension, and trims trailing spaces before comparing — ``nul``, ``NUL.txt``
    and ``CON .txt`` all count, and the trim-first ordering is right because Win32
    strips the trailing run *before* it tests for a device (``aux.. ..`` resolves
    to AUX). It is a *segment* predicate: it splits on the first dot of the whole
    string, so ``_is_reserved_basename("sub/NUL")`` is False. Applying it per
    component is what puts ``"sub/NUL"`` in reach at all.

    That set is deliberately a superset of Microsoft's published list, which names
    only ``COM1``-``COM9`` and ``LPT1``-``LPT9`` and omits the console pair
    entirely — Wine's ``RtlIsDosDeviceName_U`` matches ``CONIN$``/``CONOUT$`` and
    rejects the ``0`` forms, so ``COM0``/``LPT0`` are refused here by neither
    authority. Over-refusing six spellings nobody wants as a directory name is the
    safe direction for a guard; do not read the set as a claim about Win32.

    Windows 11 narrowed the rule the set encodes. Microsoft states the change (in
    the .NET path-format documentation, not in the file-naming page, which still
    asserts the old model): before Windows 11 a path *beginning* with a legacy
    device name was always interpreted as that device, so ``CON.TXT`` meant
    ``\\\\.\\CON``; that no longer applies. Wine's conformance data encodes the
    same narrowing case by case — ``C:\\con\\con`` carries a Windows 11 alternate
    expectation of a literal path, and the extension forms are marked as failing
    there — while bare ``NUL`` is left unmarked at every position. Bare ``NUL`` at
    a leaf therefore stays a device on Windows 11, as though every existing
    directory holds a virtual ``NUL``; ``sub/CON`` and ``NUL.txt`` do not. The
    unnarrowed rule — hijack from any position, extension or not — holds on
    Windows 10 and earlier.

    We refuse the Windows 10 superset on every platform anyway, deliberately: a
    config value must not mean one thing on one OS build and something else on the
    next, and a guard that tracked the narrowing would turn a ``seed_files`` entry
    into a build-number question. It is the same reasoning that already has the
    family refusing ``C:\\secrets`` on POSIX.

    **Rule 2 — the trailing period/space trim.** Win32 removes every trailing
    period and space from a path component, so ``".claude/skills."`` creates and
    addresses ``.claude/skills`` while the configured string still spells
    ``skills.``. The divergence reaches past the filesystem: git's gitignore parser
    reads the authored spelling, so a shield pattern rendered from the config
    matches ``skills.`` and misses the directory Win32 actually made.

    Rule 2 reads the same trim :func:`names_tree_root` does, split by what the
    whole *value* amounts to rather than by component. That predicate owns a value
    made *entirely* of period/space components, where the trim leaves nothing at
    any level and the value names the tree root. This one owns everything else the
    trim touches: a component the trim shortens (``"skills. "`` names a sibling of
    what was written) and equally a component the trim *empties* when it sits
    beside a real one — ``"sub/..."`` is nobody's root and nobody's parent, so it
    is an alias and belongs here (it addresses ``sub`` on Windows and a literal
    ``...`` directory on POSIX; the first review round caught it slipping all four
    members). The ``not root_naming`` term draws that line, and the
    ``part not in (".", "..")`` carve-out beside it hands the two spellings that
    mean the *same* path on every platform back to their owners — ``"."`` is a
    no-op component everywhere, ``".."`` is :func:`has_parent_ref`'s climb. All
    four members therefore refuse disjoint spelling classes — which is what lets
    each be ablated on its own, and mirrors :func:`names_tree_root`'s own
    ``part != ".."`` carve-out one function up.

    The git half of rule 2 is measured, on this repo's own suite. **The Win32
    filesystem half is cited, not measured** — this is a Linux box and nothing here
    calls a Win32 API. The sources are Microsoft's "Naming Files, Paths, and
    Namespaces" for the reserved list and the ``NUL.txt`` equivalence; Microsoft's
    ".NET File path formats on Windows systems" for the trim rule and the Windows
    11 statement; Wine's ntdll path conformance tests (``test_RtlGetFullPathName_U``
    and ``test_RtlIsDosDeviceName_U``, against ``collapse_path`` and
    ``RtlIsDosDeviceName_U``) for the per-case narrowing and the ``NUL`` carve-out;
    and Project Zero's "The Definitive Guide on Win32 to NT Path Conversion" (2016,
    so pre-narrowing) for the mechanism. The ``NUL`` carve-out is stated by none of
    Microsoft's pages."""
    text = str(value)
    # Same both-separator split as `names_tree_root`, and for the same reason: a
    # value is judged by the components Win32 would see.
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    # A value made ENTIRELY of period/space components names the tree root and is
    # `names_tree_root`'s to refuse; scoping rule 2 by the WHOLE value rather than
    # per component is what keeps the two disjoint while still catching an
    # all-period/space component embedded beside a real one (`sub/...`), which is
    # nobody's root and nobody's parent.
    root_naming = names_tree_root(text)
    return any(
        _is_reserved_basename(part)
        or (
            part != part.rstrip(" .")
            # `.` and `..` spell the same path on every platform — the no-op
            # component, and the climb `has_parent_ref` owns — the same
            # carve-out, for the same reason, as `names_tree_root`'s `..`.
            and part not in (".", "..")
            and not root_naming
        )
        for part in parts
    )


def is_wsl_unc_path(value: str | Path) -> bool:
    """True if ``value`` addresses a WSL distro's filesystem through the Windows UNC
    bridge — ``\\\\wsl.localhost\\<distro>\\...`` or its legacy ``\\\\wsl$\\<distro>\\...``
    spelling, matched case-insensitively and with either separator, since Windows
    accepts ``//wsl$/...`` as readily as the backslash form.

    Used to spot a native-Windows interpreter working a distro path — the #332
    mis-pick, where WSL's appended Windows ``PATH`` lands a bash prompt on a ``win32``
    build that takes the win32 defaults and never sees the distro's tmux.

    The path is the signal because the obvious alternative does not survive: probed on
    a live interop launch (Windows 11, WSL2, Ubuntu-24.04, 2026-08), ``WSL_DISTRO_NAME``
    and ``WSL_INTEROP`` were absent from the child and ``PWD``, when present at all,
    carried the *Windows*-side parent's value rather than the distro cwd — WSL hands a
    Windows binary the *Windows* environment block. So an env marker is not merely
    missing, it can be present and wrong. Nothing pins that observation, so re-probe
    before adding an env-marker check rather than assuming one would work.

    Platform-blind by design (reads no ``sys.platform``): the "is this interpreter the
    wrong one" half stays at the call site.

    Callers pass a resolved path (``cli._project``), and resolution is what decides the
    coverage: measured on Windows 11 / CPython 3.13, ``Path.resolve()`` leaves both
    bridge spellings untouched, folds ``//wsl.localhost/...`` into the backslash form,
    and dereferences a mapped drive or ``subst`` alias back to the UNC spelling — so all
    of those match. An extended-length ``\\\\?\\UNC\\...`` prefix the input already
    carried (``ntpath.realpath`` only strips one it added itself) is folded down to the
    plain UNC form here, so that spelling matches too. A spelling this still misses
    degrades gracefully: the ``mux.selection`` line names the platform regardless.

    One caller now passes an *un*resolved path: when the OS refuses to canonicalize,
    :func:`resolve_or_lexical` degrades to ``absolute()``, which is the only way this
    check runs at all on the host it is for (#552). All four bridge spellings still
    match — they are already absolute — but the mapped-drive/``subst`` dereference is
    gone, so a substituted drive letter over a *dead* provider goes unflagged. That is
    the graceful-degradation case above, not a new one: the finding is a warning, and
    ``mux.selection`` still names the platform."""
    text = str(value).replace("/", "\\").lower()
    if text.startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[len("\\\\?\\unc\\") :]
    return text.startswith(("\\\\wsl.localhost\\", "\\\\wsl$\\"))


# One note per degraded spelling per process. A single invocation canonicalizes the
# project root at least twice — `main()` pre-dispatch, then the handler's own
# `_project` — and one condition must not print two lines.
_LEXICAL_FALLBACK_NOTED: set[str] = set()


def resolve_or_lexical(path: str | Path) -> Path:
    """``Path(path).resolve()``, degrading to a *lexical* absolutization — and one
    note on stderr — when the OS refuses to canonicalize the path (#552).

    The condition this exists for: on a Windows host whose WSL UNC provider is
    registered but not serving, resolving ``\\\\wsl$\\<distro>\\...`` raises
    ``ERROR_NETNAME_DELETED`` (WinError 64). CPython's non-strict
    ``ntpath._getfinalpathname_nonstrict`` re-raises any winerror outside its
    allow-list, and 64 is not on it, so ``resolve()`` fails outright rather than
    falling back to its own lexical walk. Reached from ``cli._project`` before
    dispatch, that killed *every* subcommand at ``main()``'s backstop — including
    ``diagnose`` and ``validate``, whose ``host.win32-on-wsl-path`` finding names
    that exact host. The warning was unreachable on the only hosts it is for.

    ``RuntimeError`` is caught alongside ``OSError`` because ``resolve()`` raises it,
    not an ``OSError``, for a symlink loop on the 3.11/3.12 floor — the asymmetry
    ``install._shield_undo_extension`` documents; the pair is this repo's house guard,
    applied at 17-odd sites already.

    **Degrade, not fail** — deliberately, and bounded. The fallback is exactly
    ``Path(path).absolute()``: absolute, nothing else. It is enough for the
    observation surface, because :func:`is_wsl_unc_path` is purely lexical and every
    bridge spelling is already absolute, so ``absolute()`` hands it back untouched
    (pathlib folds ``//wsl.localhost/...`` to the backslash form on the way). It is
    *not* canonical, so this helper stays at the observation surface —
    ``cli._project``, which runs pre-dispatch where there is no handler to catch
    anything, and ``bmadconfig.worktree_isolation_conflict``'s comparison, which must
    not kill ``validate`` ahead of the platform preflight. ``bmadconfig.load_paths``
    is the boundary and refuses instead — a typed ``BmadConfigError`` for the project
    root *and* every configured path: a spelling the OS cannot canonicalize has an
    unknowable location (it can sit lexically inside the project while an in-tree
    junction carries it to a dead share outside), and classifying it by its spelling
    is a guess that can redirect a worktree-isolated run's writes. The write paths
    that need a canonical answer keep their bare ``resolve()`` and still raise:
    ``runs.project_tag`` digests ``str(project.resolve())`` into a session-ownership
    tag, and two spellings of one project would strand live sessions. So a ``run`` on
    such a host fails loud at config load, while ``validate``/``diagnose`` still
    reach the finding that explains it — observation degrades, repair writes raise.

    **Rejected: ``normpath(absolute(path))``.** Collapsing ``..`` lexically is not a
    respelling, it names a *different directory* whenever the ``..`` crosses a
    symlink — measured, not reasoned: with ``/home/u/link -> /var/x``,
    ``/home/u/link/../proj`` opens ``/var/proj``, and normpath yields
    ``/home/u/proj``. The raw value reaching here is persisted as ``state.project``
    and reused as a git repo root and a session cwd (``runsetup.build_run_state``,
    ``runs``, ``resolve``), so that would be a silent wrong-directory write — a
    failure class ``resolve()`` never had, introduced by the guard meant to soften
    it. Plain ``absolute()`` keeps the ``..`` and lets the OS dereference it
    correctly at every use, which is the whole reason to prefer it over a
    prettier-looking string.

    A relative ``path`` with an unreadable cwd still raises out of ``absolute()``:
    there is no lexical answer to degrade to, and the backstop is the honest reply."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError) as e:
        lexical = Path(path).absolute()
        if str(lexical) not in _LEXICAL_FALLBACK_NOTED:
            _LEXICAL_FALLBACK_NOTED.add(str(lexical))
            # stderr, never stdout: `<cmd> --json` is a one-object-on-stdout contract.
            print(
                f"note: cannot canonicalize {path}: {e} — continuing with the lexical "
                f"path {lexical} (symlinks are not dereferenced). "
                "Run `bmad-loop validate` for what this host is doing.",
                file=sys.stderr,
            )
        return lexical


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


# Matched as a code-point range rather than by an encode round trip, the same way
# ``engine._TITLE_CONTROL_RE`` reaches these: a Python ``str`` holds code points,
# so an astral character like U+1D11E is one code point *outside* this range and
# is never touched. Only genuinely lone surrogates match.
_SURROGATES_RE = re.compile(r"[\ud800-\udfff]")


def neutralize_surrogates(text: str) -> str:
    """Replace every lone surrogate in ``text`` with U+FFFD (``�``).

    A surrogate is a legal ``str`` code point with **no UTF-8 encoding at all**,
    so any strict encode — :func:`atomic_write_text`'s included — raises
    ``UnicodeEncodeError`` on one. That is a ``ValueError`` subclass, which is
    how a single unpaired code point reaches a caller as a crash rather than as
    mangled text. They arrive from anywhere a decoder is allowed to mint them:
    ``json.loads`` reviving a ``\\ud800`` escape, a double-quoted YAML scalar, a
    ``surrogateescape`` decode of undecodable filesystem bytes.

    Replace, not strip and not refuse. U+FFFD keeps the value **visible** — the
    field still says *something unencodable was here* — where dropping the code
    point would let it vanish silently and refusing would only move the stoppage
    upstream. It is also why this is a substitution rather than the shorter
    ``text.encode("utf-8", "replace").decode("utf-8")``: that spelling yields
    ``"?"``, indistinguishable from a question mark the author actually typed.

    Text with no surrogate is returned untouched — the identical object, so a
    clean write stays byte-identical."""
    if not _SURROGATES_RE.search(text):
        return text
    return _SURROGATES_RE.sub("�", text)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    follow_symlinks: bool = True,
    require_writable_target: bool = False,
) -> None:
    """Replace ``path``'s contents with ``text`` atomically, preserving what the
    replacement would otherwise silently discard.

    ``os.replace`` swaps a *new inode* into place, so a naive tmp-write-and-replace
    quietly resets everything carried by the old file rather than by its name. This
    restores the parts that matter:

    * **Symlinks are followed.** ``path.resolve()`` first, so a ledger symlinked
      into the repo keeps being a symlink and the real file is what gets rewritten
      — a replace against the link itself would turn it into a regular file and
      orphan the target. Pass ``follow_symlinks=False`` to invert that: the name
      is replaced, whatever it points at. Right for a machine-minted file living
      somewhere a less-trusted writer can reach, where honouring a planted link
      would aim this write at a path of that writer's choosing; wrong for the
      operator-curated ledgers this helper was built for, hence the default.
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

    ``require_writable_target=True`` refuses the write when the target already
    exists and the kernel will not open it for writing — the property a
    temp-and-replace write otherwise loses, because ``os.replace`` needs write
    permission on the *directory* and never opens the entry it replaces (#597).
    Off by default: that is the behavior every caller has today, and turning it on
    for all of them would refuse writes that currently succeed. See
    :func:`_refuse_unwritable_target` for what the probe does and does not promise.

    The bytes sibling is :func:`atomic_write_bytes`; the two share every property
    above and differ only in the ``os.fdopen`` mode. Text mode's *newline*
    default (translating) is deliberate here — it matches the ``Path.write_text``
    this replaced, so a ledger's line endings do not change under Windows."""
    _atomic_write(
        path,
        text,
        mode="w",
        encoding="utf-8",
        follow_symlinks=follow_symlinks,
        require_writable_target=require_writable_target,
    )


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    follow_symlinks: bool = True,
    require_writable_target: bool = False,
) -> None:
    """Replace ``path``'s contents with ``data`` atomically — the byte-exact
    sibling of :func:`atomic_write_text`, whose docstring carries the shared
    contract (mode and xattrs preserved when the target already exists and
    symlinks are followed, a fresh target left at ``mkstemp``'s private ``0600``,
    fsync before the replace, temp removed on any failure).

    ``follow_symlinks`` behaves exactly as it does there. The default resolves
    ``path`` first, so a file symlinked into the repo keeps being a symlink and
    the real file is what gets rewritten — a replace against the link itself would
    turn it into a regular file and orphan the target. ``follow_symlinks=False``
    inverts that: the *name* is replaced, whatever it points at, and mode and
    xattrs are then not inherited at all. Right for a machine-minted file living
    somewhere a less-trusted writer can reach, where honouring a planted link
    would aim this write at a path of that writer's choosing; wrong for the
    operator-curated files the default was built for — including the private git
    exclude ``install._worktree_local_exclude`` writes, which pre-creates the
    target precisely so this helper has a umask mode to carry over.

    The one difference from the text sibling is the whole point: ``data`` lands
    byte-for-byte. No encode and no newline translation, so a payload carrying LF
    keeps LF on Windows and bytes that are not valid text in any codec survive the
    round trip. Callers handling filesystem-derived content want this variant — a
    POSIX filename is arbitrary bytes, and an operator's git exclude file may be
    in any legacy encoding at all — as do callers who read bytes to preserve a
    file's existing line endings (``policy.write_mux_backend``).

    ``require_writable_target`` behaves exactly as it does in the text sibling."""
    _atomic_write(
        path,
        data,
        mode="wb",
        encoding=None,
        follow_symlinks=follow_symlinks,
        require_writable_target=require_writable_target,
    )


def _is_name_too_long(exc: OSError) -> bool:
    """True if ``exc`` is the filesystem refusing a name for its LENGTH, under
    either platform's spelling of that condition.

    Two spellings because win32 does not use the POSIX one: CPython's
    ``PC/errmap.h`` maps ``ERROR_FILENAME_EXCED_RANGE`` to ``ENOENT``, so there
    the errno is indistinguishable from an absent directory and only
    ``.winerror`` tells them apart (see ``_WINERROR_FILENAME_EXCED_RANGE``)."""
    if exc.errno == errno.ENAMETOOLONG:
        return True
    return getattr(exc, "winerror", None) == _WINERROR_FILENAME_EXCED_RANGE


def _stage_shortening(name: str, attempt: Callable[[str], tuple[int, str]]) -> tuple[int, str]:
    """Walk ``attempt`` down the staging-prefix ladder :func:`_mkstemp_beside`
    documents: the target's readable name, then a fixed-width digest of it where
    that strictly shortens, then no prefix at all. A rung that raises the
    platform's "name too long" (:func:`_is_name_too_long`) falls to the next;
    anything else propagates, and only the bare last rung's failure escapes.

    Shared by both staging families — ``mkstemp`` beside a path and the ``O_EXCL``
    create relative to a descriptor — because the guarantee is one guarantee: a
    basename the target itself is legal at must stage, whichever writer the
    caller reached (#595). The confined adoption briefly split them, and a spec
    name near ``NAME_MAX`` wrote fine through the plain helper while the anchored
    one died appending its suffix to the full name."""
    digest = hashlib.blake2b(os.fsencode(name), digest_size=8).hexdigest()
    rungs = [name + "."]
    if len(digest) < len(name):
        rungs.append(digest + ".")
    rungs.append("")
    for prefix in rungs[:-1]:
        try:
            return attempt(prefix)
        except OSError as e:
            if not _is_name_too_long(e):
                raise
    return attempt(rungs[-1])


def _mkstemp_beside(target: Path) -> tuple[int, str]:
    """``mkstemp`` in ``target``'s own directory, prefixed with its name so the
    temp is recognisably that target's staging file — and so it ends in ``.tmp``
    rather than the target's extension, which `devcontract._atomic_write_spec`
    depends on to keep its temps out of the ``*.md`` artifact scans.

    ``mkstemp`` inserts 8 random characters between prefix and suffix, so the temp
    name runs ``len(target.name) + 13``. A basename within 13 bytes of the
    filesystem's ``NAME_MAX`` therefore makes the TEMP name illegal at a path the
    target itself is perfectly legal at. Measured on ext4 (``NAME_MAX`` 255): a
    242-byte basename stages fine, 243 raises ``ENAMETOOLONG`` while the direct
    write it replaced succeeded through 255 (#595).

    The fallback replaces the readable prefix with a digest of it rather than
    truncating it. Truncation is the obvious fix and it is wrong: ``NAME_MAX``
    counts BYTES while Python slices CHARACTERS, so a bounded character slice
    neither guarantees a legal name nor avoids splitting a UTF-8 sequence — and
    the limit is not portably knowable anyway (``os.pathconf`` is POSIX-only,
    NTFS counts UTF-16 code units, and win32 is usually bound by ``MAX_PATH`` on
    the whole path instead). Letting the OS answer needs none of that arithmetic:
    the common path keeps the readable name, and only a basename that cannot fit
    degrades to a fixed-width digest.

    ``os.fsencode``, not ``str.encode``: a POSIX filename is arbitrary bytes and
    may carry surrogates that a strict UTF-8 encode would raise on.

    The retry keys on TWO spellings of one condition, because win32 does not use
    the POSIX one: ``ERROR_FILENAME_EXCED_RANGE`` arrives as ``ENOENT`` and is
    distinguishable only by ``.winerror`` (see
    ``_WINERROR_FILENAME_EXCED_RANGE``). Keying on ``ENAMETOOLONG`` alone left
    this whole fallback dead on Windows — where, per the ``MAX_PATH`` note above,
    it is if anything easier to reach than on ext4.

    The rungs STRICTLY SHORTEN, and the last one carries no prefix at all. A
    digest is 16 characters, so on its own it is not a fallback — against a
    basename of 16 or fewer it stages a name no shorter than the one that just
    failed, and retrying at the same width cannot succeed. That is unreachable
    where the binding limit is per-component (a POSIX ``NAME_MAX`` rung is only
    reached past 242 characters, far above the digest) and reachable where it is
    the whole path, which is the win32 case: a short basename in a directory near
    ``MAX_PATH`` overflows on the staging suffix alone. So the digest rung is used
    only while it actually shortens, and a bare ``mkstemp`` — the shortest name
    this function can produce — always ends the ladder.

    Only a failure at that last rung propagates. No choice of *prefix* can fix
    that one — there is no prefix left — but that is a narrower statement than "the
    directory is too long", and the difference is #596: ``mkstemp`` will not
    generate a name below 12 characters (8 random, plus the ``.tmp`` this helper
    needs to stay out of ``devcontract``'s ``*.md`` scans). What does not fit at
    the last rung is therefore the directory PLUS those 12, which a target with a
    shorter basename can still clear on a direct write. Shrinking below the floor
    means abandoning ``mkstemp``, and with it the entropy that keeps the staged
    name unpredictable where a less-trusted writer can reach it — see #591 for the
    cost of a guessable temp name."""
    directory = str(target.parent)
    return _stage_shortening(
        target.name,
        lambda prefix: tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp"),
    )


def _refuse_unwritable_target(target: Path, *, follow_symlinks: bool) -> None:
    """Re-raise the kernel's own ``PermissionError`` when ``target`` exists and
    will not open for writing — the opt-in half of ``require_writable_target``.

    A temp-and-replace write never opens the file it replaces, and ``os.replace``
    needs write permission on the *directory*, not on the entry being replaced. So
    a file an operator marked ``0444`` is overwritten anyway — and where mode is
    inherited (``follow_symlinks=True``) it comes back reading ``0444``, with
    nothing in the permission bits recording that it changed. Every writer here
    that once spelled ``Path.write_text`` refused that write as a side effect of
    opening the file; going atomic dropped the refusal silently (#597). This asks
    for it back, per caller, over the files an operator actually curates.

    An actual ``os.open``, not ``os.access``: access(2) answers for the REAL uid
    and only approximates ACLs, while the open is the kernel's answer under the
    same credentials the write will use, and reproduces the exact error the direct
    write used to raise. On win32 a READONLY file denies ``O_WRONLY`` with
    ``ERROR_ACCESS_DENIED``, which arrives as ``PermissionError``, so this arm is
    real on both platforms rather than POSIX-only.

    Three cases are deliberately NOT refusals, and all three simply return:

    * **A missing target.** There is nothing to refuse — the write creates it, and
      creation is what the flagged callers do on first run.
    * **``ELOOP``**, i.e. a symlink at the name on the no-follow path. That write
      replaces the NAME whatever it points at, on purpose, and a plantable link's
      target mode is the planter's choice as much as anyone's (see
      :func:`_atomic_write`). Win32 has no ``O_NOFOLLOW`` to raise ``ELOOP`` with,
      so the same case is caught there by the :func:`is_link_like` pre-check below.
    * **Any other ``OSError``** — a directory at the name, a full disk, a
      disconnected share. Refusing here would replace the write's own, more
      accurate error with a permission story that is not what happened.

    ``O_NONBLOCK`` is what keeps the third case reachable for a FIFO. Opening a
    reader-less FIFO ``O_WRONLY`` does not fail — it WAITS for a reader that a
    planted FIFO will never have, wedging the probe (and the loop driving it)
    forever, and ``O_NOFOLLOW`` is no help because a FIFO is not a symlink.
    Non-blocking turns that wait into ``ENXIO``, which the third case returns on,
    and the write then replaces the FIFO's name like any other. For the regular
    files this probe exists for the flag changes nothing: POSIX gives it no
    effect on a regular-file open or on the permission check, and win32 — which
    has no ``O_NONBLOCK`` — has no path-visible FIFOs to block on either.

    This honours an operator's stated intent; it is NOT a boundary against a
    same-uid writer. The answer is stale the moment it returns — a ``chmod``
    between probe and replace still lands the write — and whoever can chmod the
    file back can defeat it outright. :func:`_atomic_write` runs it BEFORE
    ``_mkstemp_beside``, so a refusal stages nothing."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)  # POSIX-only; win32 cannot ask
    if not follow_symlinks and not no_follow and is_link_like(target):
        return  # win32: no O_NOFOLLOW, so the probe would read through the link
    non_block = getattr(os, "O_NONBLOCK", 0)  # POSIX-only; win32 has no FIFOs here
    try:
        fd = os.open(target, os.O_WRONLY | (0 if follow_symlinks else no_follow) | non_block)
    except PermissionError:
        raise  # the refusal this flag exists for
    except OSError:
        return  # missing, a link at the name, or something the write itself reports
    os.close(fd)


def _atomic_write(
    path: Path,
    payload: str | bytes,
    *,
    mode: str,
    encoding: str | None,
    follow_symlinks: bool = True,
    require_writable_target: bool = False,
) -> None:
    """The shared body of the two public helpers above — see
    :func:`atomic_write_text` for the contract every step here implements.

    Written through ``os.fdopen`` rather than a raw ``os.write`` loop on purpose:
    it routes to ``io.open``, the one seam a test can inject a short write at for
    both variants at once (tests/test_install.py's #375 case).

    ``follow_symlinks=False`` skips the resolve, so the *name* is what gets
    replaced. It needs no preflight ``is_symlink`` check to be safe, and that is
    the reason to prefer it over one: ``os.replace`` does not dereference its
    destination, so a link planted at any moment — including between a check and
    this call — is overwritten rather than written through.

    Mode and xattrs are then not inherited **at all**, and nothing is probed to
    decide that. A name being replaced rather than updated should carry nothing
    of whatever it used to point at, and in this mode there is no trustworthy
    prior to carry over anyway: the caller asked for no-follow precisely because
    a less-trusted writer can reach the name, so the mode found there is that
    writer's choice as much as anyone's. Probing first and copying after would
    also reopen by the back door the very window the paragraph above closes —
    ``shutil.copymode`` re-resolves the path it is handed, so a link planted
    between the probe and the copy hands the new record the mode of a file of
    the planter's choosing (the contents stay safe; ``os.replace`` still does not
    dereference). Taking no probe leaves no window to race, and ``mkstemp``'s
    private ``0600`` is the right mode for the machine-minted file this mode
    exists for.

    ``require_writable_target=True`` inserts :func:`_refuse_unwritable_target`
    between the resolve and the staging, in that order on purpose: a refusal then
    stages nothing, so a caller that declines to overwrite a read-only file also
    leaves no temp behind to explain."""
    target = path.resolve() if follow_symlinks else path
    if require_writable_target:
        _refuse_unwritable_target(target, follow_symlinks=follow_symlinks)
    fd, tmp_name = _mkstemp_beside(target)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, mode, encoding=encoding) as fh:
            fh.write(payload)
            fh.flush()  # userspace buffer -> kernel, so there is something to sync
            os.fsync(fh.fileno())
        if follow_symlinks and target.exists():
            shutil.copymode(target, tmp)
            _copy_xattrs(target, tmp)
        atomic_replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            try:
                tmp.unlink()
            except PermissionError:
                # win32 DeleteFile refuses a READONLY file, and the copymode
                # above stamps the target's READONLY bit onto the temp — so a
                # publish denied over a read-only destination would leak it.
                # POSIX never takes this arm: unlink consults the parent
                # directory's permission, never the entry's own mode.
                os.chmod(tmp, stat.S_IWRITE)
                tmp.unlink()
        raise


# Whether this platform has the `*at()` family the two helpers below need. The
# probe is `O_DIRECTORY` rather than `os.supports_dir_fd`: that set tracks only
# the literal `dir_fd` parameter, so `os.replace` — which spells it
# `src_dir_fd`/`dst_dir_fd` — is absent from it even on Linux, where renameat
# works. CPython gates the whole family on one configure pass, so the flag's
# presence answers for all of them: it is defined on Linux/macOS and absent on
# Windows, whose pyconfig has neither HAVE_RENAMEAT nor HAVE_OPENAT.
DIR_FD_ANCHORED_WRITES = hasattr(os, "O_DIRECTORY")


# Windows reparse tags that make a directory entry REDIRECT somewhere else,
# compared against os.lstat().st_reparse_tag (Windows, 3.8+). Deliberately not
# os.path.isjunction(), which is 3.12+ while this package's floor is 3.11.
# Deliberately not "any reparse tag" either: cloud placeholders (OneDrive) and
# dedup stubs are reparse points too, and refusing those would stall a
# legitimate run. Empty on POSIX.
_LINK_REPARSE_TAGS = tuple(
    tag
    for tag in (
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", None),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
    )
    if tag is not None
)


def is_link_like(path: Path) -> bool:
    """True when ``path`` redirects elsewhere: a POSIX symlink, or a Windows
    symlink OR DIRECTORY JUNCTION.

    ``Path.is_symlink()`` is False for a junction — junctions are a distinct
    reparse kind, which is why ``os.path.isjunction()`` exists at all. On Windows
    the junction is the arm that matters: ``mklink /J`` needs no elevation, while
    a directory symlink needs SeCreateSymbolicLinkPrivilege or Developer Mode, so
    the UNPRIVILEGED redirect is exactly the one an ``is_symlink()`` check misses.

    This is the win32 half of :func:`open_dir_confined`, which anchors the POSIX
    side at a descriptor instead. A path check is inherently check-then-write —
    answered about a name, and stale the moment it returns — so it narrows the
    window rather than closing it. That residual is the platform's, not this
    function's: win32 has no ``*at()`` family to anchor against.

    ``events.py`` and the standalone hook relay keep their own copies of this
    predicate on purpose: they run under the HOST's interpreter, not this
    package's, so they cannot import it from here.
    """
    if path.is_symlink():
        return True
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) in _LINK_REPARSE_TAGS
    except OSError:
        return False


class UnconfinedWriteError(OSError):
    """A confined write refused: the path is not under its root, or a component
    below that root is a link, is missing, or cannot be probed.

    An ``OSError`` subclass on purpose. Every site that adopts a confined writer
    already degrades on ``OSError`` from the same call — ``runs.stop_run``
    swallows a failed stop-request write, the engine journals a failed park
    rollback, the settings screen reports a failed save — so a refusal arrives in
    the handling those callers already have rather than escaping as a new
    exception type nobody catches. A caller that wants to tell a refusal apart
    from a disk error can still catch this name specifically.

    The read-only refusal (``require_writable_target``) is deliberately NOT this
    class: it re-raises the kernel's own ``PermissionError``, which is exactly
    what a bare ``Path.write_text`` raised before these writes went atomic."""


def path_is_confined(root: Path, target: Path) -> bool:
    """Whether ``target`` is reached from ``root`` without traversing a redirect
    at any component below it.

    The win32 half of :func:`open_dir_confined`, which anchors the POSIX side at
    a descriptor instead. A check, not a race-free open: it is answered about a
    NAME and is stale the moment it returns, so it removes the standing redirect
    — plant a link, wait for a write — while a writer who re-plants inside the
    window between check and write still wins. That residual is the platform's,
    not this function's: win32 has no ``*at()`` family to anchor against.

    Every component below ``root`` is checked and ``root`` itself is not: the
    operator chooses where the project lives and may well keep it behind a link,
    while everything under it is session-writable. That is the same split
    :func:`open_dir_confined` makes by opening ``root`` without ``O_NOFOLLOW``.

    ``lstat``-based throughout, so the walk never resolves through the thing it
    is testing for — and an ``lstat`` that RAISES answers False, because a
    component that cannot be probed is one this cannot vouch for. ``Path``'s own
    predicates swallow that error and answer "not a link", which walks PAST the
    component instead: the opposite of what a confinement check owes its caller.

    Both link kinds count. ``S_ISLNK`` is the whole answer on POSIX; on win32 a
    DIRECTORY JUNCTION redirects identically, is invisible to ``is_symlink()``,
    and is the cheaper plant (``mklink /J`` needs neither elevation nor Developer
    Mode). Recognised through :data:`_LINK_REPARSE_TAGS` rather than
    ``os.path.isjunction``, which is 3.12+ while this package's floor is 3.11.

    Narrower than ``tui/launch._run_dir_is_confined``, which refuses ANY reparse
    point. Over-refusing is the cheap direction there — the cost is one unwritten
    hint — but this backs writes an operator's own configuration depends on, so a
    OneDrive placeholder or a dedup stub in the ancestry must not turn every
    policy write into a failure.

    Confinement is answered about the LEXICAL spelling handed in, exactly as
    :func:`open_dir_confined` answers it; a caller building ``target`` out of
    untrusted parts owes its own ``..`` check first (:func:`has_parent_ref`)."""
    try:
        if not target.is_relative_to(root):
            return False
        cursor = target
        while cursor != root:
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                return False
            if getattr(info, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS:  # win32-only field
                return False
            cursor = cursor.parent
    except OSError:
        return False  # a component we cannot probe is one we cannot vouch for
    return True


def walk_files_unlinked(top: Path) -> Iterator[Path]:
    """Every non-directory entry under ``top``, never crossing a redirect out of it.

    **Non-directory, not regular file** — ``os.walk`` puts FIFOs, device nodes and
    symlinks in ``files`` alongside ordinary ones, and this yields what it is
    handed. A caller that only counts or ``lstat``s is fine; a caller that OPENS
    what it yields owes its own regular-file check, because opening a planted
    FIFO blocks forever. Swapping ``rglob`` for this helper silently drops the
    ``is_file()`` guard the old loop carried — that regression shipped once
    (``diagnostics.summarize_files``, whose ``logs`` arm reads to count lines).

    Two holes, closed together because a caller that measures or counts a tree
    gets both wrong in the same way:

    ``os.walk`` already declines to recurse into a symlinked subdirectory — but
    it decides that with ``os.path.islink``, which is False for a Windows
    DIRECTORY JUNCTION. That is the unprivileged redirect (see
    :func:`is_link_like`), so on win32 the pruning `os.walk` documents is exactly
    the arm an attacker would use. And ``os.walk`` always follows the top path it
    is handed, symlink or not, so refusing to descend into links says nothing
    about the root.

    Both matter to more than tidiness: a session is handed a writable run
    directory (`BMAD_LOOP_RUN_DIR`) and can plant a link at an entry that `clean`
    sizes and `diagnose` counts, which would bill a reclaim estimate — or a
    diagnostic dump — for an arbitrarily large tree outside the run that neither
    command touches. Yields paths; the caller chooses ``stat`` or ``lstat``.
    """
    if is_link_like(top):
        return
    for root, dirs, files in os.walk(top, onerror=lambda _e: None):
        # in-place, which is how os.walk documents pruning under topdown=True
        dirs[:] = [d for d in dirs if not is_link_like(Path(root) / d)]
        for name in files:
            yield Path(root) / name


def open_dir_confined(root: Path, target: Path) -> int | None:
    """An open descriptor for ``target``, reached from ``root`` without
    traversing a symlink at any component below it — or None when that cannot be
    established. The caller owns the descriptor and must ``os.close`` it.

    A *descriptor*, not a verdict, and that is the whole point. A boolean
    "is this path confined?" is answered about a path, and the answer is stale
    the instant it returns: whoever can write those directories can swap one for
    a symlink before the caller gets around to opening anything. The descriptor
    this hands back is bound to the directory that was actually walked, so a
    later swap of any name along the way renames a path this no longer consults.
    Pair it with :func:`atomic_write_text_at`, which never names a path again.

    Each component is opened ``O_NOFOLLOW | O_DIRECTORY`` relative to the one
    above it, so a link anywhere below ``root`` fails the open rather than being
    followed. ``root`` itself is opened without ``O_NOFOLLOW``: the operator
    chooses where the project lives and may keep it behind a link, while
    everything under it is session-writable.

    POSIX only — see :data:`DIR_FD_ANCHORED_WRITES`. Callers need a fallback for
    win32, which has no ``*at()`` family to anchor against."""
    if not DIR_FD_ANCHORED_WRITES:
        return None
    try:
        relative = target.relative_to(root)
    except ValueError:
        return None  # not under root at all
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    for part in relative.parts:
        try:
            nested = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            os.close(fd)
            return None  # a link, a missing component, or one we cannot probe
        os.close(fd)
        fd = nested
    return fd


# Draws before giving up on a unique temp name. O_EXCL makes a collision
# harmless, so this only bounds a pathological loop.
_TMP_NAME_ATTEMPTS = 100


def atomic_write_text_at(dir_fd: int, name: str, text: str) -> None:
    """:func:`atomic_write_text`, anchored at an open directory descriptor.

    Every syscall here is relative to ``dir_fd``, so nothing resolves a path a
    concurrent writer could have redirected — the directory is the one
    :func:`open_dir_confined` walked to, whatever its name points at now. That
    closes the window a preflight path check leaves open, rather than narrowing
    it. ``name`` must be a single component.

    Shares the shape of the path-based helper: unique temp in the same
    directory, contents fsynced *before* the replace publishes them, temp
    removed on any failure. It deliberately does NOT inherit mode or xattrs —
    this exists for machine-minted files under a session-writable root, where
    the prior file's mode is as untrusted as the rest of it, so the new record
    keeps the private ``0600`` it is created with. Text is written UTF-8 with
    no newline translation; the callers are records, not operator-edited files.

    No win32 sharing-violation retry, unlike :func:`atomic_replace`: there is no
    win32 here at all — the ``*at()`` family this is built on does not exist
    there, so a caller reaching this is on POSIX by construction."""
    _atomic_write_at(dir_fd, name, text, mode="w", encoding="utf-8")


def atomic_write_bytes_at(dir_fd: int, name: str, data: bytes) -> None:
    """:func:`atomic_write_text_at`'s byte-exact sibling, whose docstring carries
    the shared contract (a unique unguessable temp created ``O_EXCL`` at ``0600``,
    every syscall relative to ``dir_fd``, fsync before the replace, temp removed
    on any failure, no mode or xattrs inherited, POSIX by construction).

    The one difference is the whole point: ``data`` lands byte-for-byte. No
    encode and no newline translation, so a payload carrying CRLF keeps CRLF and
    bytes that are not valid text in any codec survive the round trip. The
    anchored cohort needs this variant for the same reason the path-based one
    does — ``policy.write_mux_backend`` and the two frontmatter writers read
    bytes precisely to preserve a file's existing line endings, and a text-only
    anchored helper would have rewritten them (#593)."""
    _atomic_write_at(dir_fd, name, data, mode="wb", encoding=None)


def _open_exclusive_at(dir_fd: int, prefix: str, name: str) -> tuple[int, str]:
    """One ``mkstemp``'s worth of anchored staging: ``O_EXCL``-create
    ``{prefix}<pid>.<random>.tmp`` relative to ``dir_fd`` and return the open fd
    with the name it landed at. The ``.tmp`` suffix keeps the temp out of
    ``devcontract``'s ``*.md`` artifact scans, exactly as `_mkstemp_beside`'s
    suffix does.

    ``os.urandom``, not ``random``: this name is created in a directory a
    less-trusted writer can reach, and a predictable one lets them pre-create it
    and fail every record write (``O_EXCL`` turns the collision into a refusal
    rather than a clobber, so the harm is a stuck hint rather than a redirect —
    but an unguessable name removes even that, #591)."""
    for _ in range(_TMP_NAME_ATTEMPTS):
        tmp = f"{prefix}{os.getpid():x}.{os.urandom(4).hex()}.tmp"
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd), tmp
        except FileExistsError:
            continue  # astronomically unlikely; costs one more draw
    raise OSError(f"no free temp name beside {name!r} after {_TMP_NAME_ATTEMPTS} tries")


def _atomic_write_at(
    dir_fd: int, name: str, payload: str | bytes, *, mode: str, encoding: str | None
) -> None:
    """The shared body of the two anchored helpers above — see
    :func:`atomic_write_text_at` for the contract every step here implements.

    ``encoding`` doubles as the text/bytes discriminator, as it does in
    :func:`_atomic_write`: the text arm is opened with it plus ``newline=""``,
    the bytes arm with neither, because ``os.fdopen`` refuses both in binary mode
    and a byte-verbatim payload has nothing to translate anyway.

    Staging walks :func:`_stage_shortening`'s ladder, the same one
    ``_mkstemp_beside`` walks, so a target basename near ``NAME_MAX`` stages here
    exactly where the path-based writer stages it (#595) — without the ladder the
    confined adoption reintroduced the long-basename failure for every spec it
    moved onto this arm."""
    fd, tmp = _stage_shortening(name, lambda prefix: _open_exclusive_at(dir_fd, prefix, name))
    try:
        staged = (
            os.fdopen(fd, mode)
            if encoding is None
            else os.fdopen(fd, mode, encoding=encoding, newline="")
        )
        with staged as fh:
            fh.write(payload)
            fh.flush()  # userspace buffer -> kernel, so there is something to sync
            os.fsync(fh.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _refuse_unwritable_target_at(dir_fd: int, name: str) -> None:
    """:func:`_refuse_unwritable_target`, asked relative to an open descriptor.

    The point of the anchored path is that it never names a path again, so the
    probe must not either: ``dir_fd=`` asks about the entry in the directory that
    was actually walked, not about a name a concurrent writer may since have
    redirected. ``O_NOFOLLOW`` always, because the write this guards replaces the
    NAME — the same reasoning as the no-follow arm there, ``ELOOP`` included.
    ``O_NONBLOCK`` for the reason the path-based probe gives: a reader-less FIFO
    planted at the name answers ``ENXIO`` instead of wedging the probe forever.
    POSIX by construction: only :data:`DIR_FD_ANCHORED_WRITES` reaches here."""
    try:
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except PermissionError:
        raise  # the refusal this flag exists for
    except OSError:
        return  # missing, a link at the name, or something the write itself reports
    os.close(fd)


def atomic_write_text_confined(
    path: Path, text: str, *, confine_root: Path, require_writable_target: bool = False
) -> None:
    """:func:`atomic_write_text`, refusing to write through a redirected PARENT.

    ``follow_symlinks=False`` stops a link planted at the file itself and nothing
    more: ``mkstemp(dir=...)`` and ``os.replace``'s destination are still ordinary
    path lookups, so a link planted at any DIRECTORY above the target lands both
    the temp and the published file wherever that link points, and the no-follow
    bought nothing (#593). ``mkdir(parents=True, exist_ok=True)`` accepts a
    symlink-to-a-directory, so a planted parent survives the setup step the
    callers run first. This closes that: the parent is established once, and the
    write is aimed at what was established rather than at the name again.

    A separate function rather than a ``confine_root=`` keyword on the plain
    writer. That keyword would admit a combination with no meaning — confined AND
    following symlinks — it would change ``_atomic_write`` for the dozen callers
    who did not ask for it, and separate names keep the adopted cohort greppable.

    ``path`` must be **lexically** under ``confine_root`` (``relative_to``, no
    resolve — the confined spelling is the caller's own construction, and
    resolving it would consult the very links this refuses) **and carry no ``..``
    below it**: ``is_relative_to`` is a prefix test that ``root/specs/../../x``
    passes while naming a path outside the root, and ``..`` is a real directory
    entry the anchored walk would otherwise climb straight back out through
    (``O_NOFOLLOW`` has no opinion on dot-dot). Either refusal raises before
    anything is walked or staged. ``path.parent`` must
    already EXIST: a confinement walk cannot vouch for a component that is not
    there, so every adopter mkdirs or gates first.

    POSIX walks the components with :func:`open_dir_confined` and writes through
    the descriptor that walk produced, which a later swap of any name along the
    way no longer reaches. Win32 has no ``*at()`` family, so it degrades to
    :func:`path_is_confined` plus a no-follow write — check-then-write, which
    removes the standing redirect but leaves the window between the check and the
    write open (the precedent, and the same documented residual, as
    ``tui/launch._record_ctl_window``). Either arm refuses by raising
    :class:`UnconfinedWriteError`, which is an ``OSError``.

    Mode and xattrs are NEVER inherited — the file lands at ``0600``, which is
    exactly what ``follow_symlinks=False`` already gives this cohort, so adopting
    this changes no file's permissions. The anchored arm writes UTF-8 with no
    newline translation (identity on POSIX, where the translating default writes
    ``\n`` unchanged) and the win32 arm keeps :func:`atomic_write_text`'s
    translating default, so on each platform the bytes that land are the ones
    that land today. A caller preserving a file's existing line endings wants
    :func:`atomic_write_bytes_confined`, as it wants the bytes writer today.

    ``require_writable_target`` behaves as it does in :func:`atomic_write_text`;
    on the anchored arm the probe is asked dir_fd-relative, never by path."""
    _atomic_write_confined(
        path,
        text,
        mode="w",
        encoding="utf-8",
        confine_root=confine_root,
        require_writable_target=require_writable_target,
    )


def atomic_write_bytes_confined(
    path: Path, data: bytes, *, confine_root: Path, require_writable_target: bool = False
) -> None:
    """:func:`atomic_write_text_confined`'s byte-exact sibling, whose docstring
    carries the shared contract (lexical ``confine_root`` gate, anchored parent on
    POSIX and the documented check-then-write degrade on win32,
    :class:`UnconfinedWriteError` on refusal, an existing parent required, ``0600``
    with no mode or xattrs inherited).

    ``data`` lands byte-for-byte on both arms: no encode, no newline translation.
    That is what the byte-verbatim writers in this cohort exist for — they read
    bytes precisely so a CRLF file keeps its line endings."""
    _atomic_write_confined(
        path,
        data,
        mode="wb",
        encoding=None,
        confine_root=confine_root,
        require_writable_target=require_writable_target,
    )


def _atomic_write_confined(
    path: Path,
    payload: str | bytes,
    *,
    mode: str,
    encoding: str | None,
    confine_root: Path,
    require_writable_target: bool,
) -> None:
    """The shared body of the two confined helpers above — see
    :func:`atomic_write_text_confined` for the contract every step implements.

    The ``has_parent_ref`` refusal is the debt :func:`path_is_confined`'s
    docstring assigns to "a caller building ``target`` out of untrusted parts":
    ``is_relative_to`` is a lexical PREFIX test, so ``root/specs/../../outside``
    passes it while naming a path outside the root. Neither arm below catches
    that on its own — ``..`` is a real directory entry, so the anchored walk
    opens it (``O_NOFOLLOW`` has no opinion on dot-dot) and climbs back OUT of
    the root, and the win32 walk ``lstat``s through it the same way. Checked over
    the RELATIVE part only: the operator chooses ``confine_root``'s own spelling,
    and the components below it are the session-reachable half."""
    if not path.is_relative_to(confine_root):
        raise UnconfinedWriteError(f"{path} is not under {confine_root}")
    if has_parent_ref(path.relative_to(confine_root)):
        raise UnconfinedWriteError(f"{path} climbs back out of {confine_root} through '..'")
    unconfined = f"cannot reach {path.parent} from {confine_root} without a redirect"
    if DIR_FD_ANCHORED_WRITES:
        dir_fd = open_dir_confined(confine_root, path.parent)
        if dir_fd is None:
            raise UnconfinedWriteError(unconfined)
        try:
            if require_writable_target:
                _refuse_unwritable_target_at(dir_fd, path.name)
            _atomic_write_at(dir_fd, path.name, payload, mode=mode, encoding=encoding)
        finally:
            os.close(dir_fd)
        return
    if not path_is_confined(confine_root, path.parent):
        raise UnconfinedWriteError(unconfined)
    _atomic_write(
        path,
        payload,
        mode=mode,
        encoding=encoding,
        follow_symlinks=False,
        require_writable_target=require_writable_target,
    )


def create_exclusive_confined(path: Path, *, confine_root: Path) -> int:
    """``os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)``, the parent reached
    the way the confined writers reach it (#593). Returns the open fd, which the
    caller owns; raises ``FileExistsError`` when the name is already taken — a
    planted link included, since ``O_EXCL`` never dereferences the final
    component — and ``UnconfinedWriteError`` when the parent cannot be vouched
    for (out of root, a ``..`` in the relative part, or a redirect at any
    component below ``confine_root``).

    Exists for exclusive-create ARBITRATION files (``runs._create_stop_request``),
    where "is one pending?" and "lodge mine" must stay a single atomic step
    against the destination name. The temp-and-replace confined writers cannot
    express that — a replace is unconditional by design — so this shares only
    their parent walk, not their staging. On POSIX the create is anchored at the
    walked descriptor; win32 has no ``*at()`` family and degrades to the same
    documented check-then-create as :func:`atomic_write_text_confined`'s
    fallback arm."""
    if not path.is_relative_to(confine_root):
        raise UnconfinedWriteError(f"{path} is not under {confine_root}")
    if has_parent_ref(path.relative_to(confine_root)):
        raise UnconfinedWriteError(f"{path} climbs back out of {confine_root} through '..'")
    unconfined = f"cannot reach {path.parent} from {confine_root} without a redirect"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if DIR_FD_ANCHORED_WRITES:
        dir_fd = open_dir_confined(confine_root, path.parent)
        if dir_fd is None:
            raise UnconfinedWriteError(unconfined)
        try:
            return os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    if not path_is_confined(confine_root, path.parent):
        raise UnconfinedWriteError(unconfined)
    return os.open(path, flags, 0o600)


def retrying_unlink(path: Path) -> None:
    """``path.unlink()`` with the same win32 retry as :func:`atomic_replace`.

    Windows denies a *delete* against an open handle exactly as it denies a
    rename-over, so the second half of a staged move is no safer than the first:
    an AV/indexer scanning the just-written source file fails the unlink. Pair the
    two whenever a move must not half-apply."""
    _retry_on_sharing_violation(path.unlink)


class LockUnavailableError(OSError):
    """:func:`file_lock` could not ACQUIRE the lock: another holder has it, or the
    acquisition call itself faulted (EINTR, ENOLCK, EBADF).

    An ``OSError`` subclass for the reason :class:`UnconfinedWriteError` is one —
    every site that already degrades on ``OSError`` from a lock acquisition keeps
    its behavior unchanged, so this narrows nothing that was previously caught.
    What the subclass buys is the arm a caller could not write before: telling
    "something else is using this" apart from every other ``OSError`` WITHOUT the
    bare ``except OSError`` that would also swallow an ``UnconfinedWriteError``
    and file a confinement refusal as a benign "busy" — the exact fold-to-benign
    those two classes exist to keep apart.

    Raised for the ACQUISITION ALONE. A fault raised by the locked body is never
    wrapped: reporting a body error as contention is the same fold in the other
    direction. The ``os.open`` that provisions the lock file is deliberately left
    unwrapped too — failing to create the sidecar is a provisioning fault, not a
    holder, and a caller that treats it as "someone is using this run" would
    retry forever against a broken path."""


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Exclusive OS advisory lock on ``path`` (created if missing), released on
    exit — and by the kernel when the holder dies, so a crashed process never
    wedges the lock (no stale-lockfile scheme to clean up). ``blocking=False``
    raises :class:`LockUnavailableError` at once when the lock is already held,
    giving tests a deterministic exclusion probe instead of a sleep-based negative
    assertion — and giving a bulk sweep a way to skip a busy item rather than
    stall on it (``cli.cmd_clean``).

    Lock a dedicated sibling file, never data that is swapped via
    :func:`atomic_replace` — the lock rides the open fd's inode, and a replace
    would swap that inode out from under later acquirers. ``fcntl.flock`` on
    POSIX; ``msvcrt.locking`` on Windows, where the blocking mode's built-in
    ~10 s retry bounds the wait and surfaces contention as
    :class:`LockUnavailableError`.

    THE WAIT IS PLATFORM-ASYMMETRIC, and a caller has to decide what that means
    for it: POSIX blocks indefinitely, Windows gives up after ~10 s and raises.
    This docstring used to add "holders only do brief file I/O" — true when the
    only consumers were tests, and falsified by the first production caller
    (``install._worktree_local_exclude``, #384), which holds it across a
    multi-step git transaction of roughly seven ``git`` spawns, each bounded by
    ``[limits] git_timeout_s``. So a Windows acquirer can genuinely time out
    under contention rather than only under a deadlock. Hold it for as short a
    span as correctness allows, and handle the :class:`LockUnavailableError` from
    acquisition.

    AND THE ASYMMETRY IS NOT REMOVED BY THE TYPED ERROR, only made catchable: a
    ``blocking=True`` POSIX acquirer still waits forever, because ``fcntl.flock``
    does not time out. :class:`LockUnavailableError` therefore reaches a blocking
    caller only on Windows' ~10 s bound and on genuinely exceptional POSIX errnos.
    A caller that must not stall — a bulk sweep over many items, where one busy
    item is data rather than a reason to stop — has to pass ``blocking=False`` and
    handle the refusal; the typed error alone does not buy it.

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
        try:
            if sys.platform == "win32":
                import msvcrt

                # Locks 1 byte at the current position — 0 on a fresh fd.
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as e:
            # Only this call is wrapped — see LockUnavailableError on why the
            # `os.open` above and the `yield` below are deliberately outside it.
            # errno/strerror/filename are carried through so a caller that reads
            # them, or just prints the exception, sees what the bare OSError said.
            raise LockUnavailableError(e.errno, e.strerror or str(e), str(path)) from e
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
