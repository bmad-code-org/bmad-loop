#!/usr/bin/env python3
"""Seed the Unity scene auto-save guard into a bmad-loop-driven project.

Unity-MCP GameObject tools mark a scene dirty but never save it, so a project
driven by the shared editor accumulates a chronically dirty scene. That dirty
state is what raises the two run-stalling modal dialogs ("scene changed on disk —
reload?" when git/an agent rewrites the open .unity file, and "save changes before
quitting?" on editor exit). A modal freezes ``EditorApplication.update`` — which
the MCP plugin uses to dispatch tool calls — so every MCP call then times out.

This helper copies an editor-only auto-save guard (``SceneAutoSaveGuard.cs`` + its
asmdef, with pre-generated ``.meta`` files carrying fixed GUIDs) into the project's
``Assets`` tree so the editor's very first import already sees it. It is invoked by
the Unity plugin *before* ``unity_setup.py`` launches the per_worktree Editor, and
at the readiness gate in shared mode (where ``unity_setup.py`` never runs).

The install is idempotent and version-aware: it copies the payload only when the
target ``.cs`` is absent or its ``bmad-loop-scene-guard-version`` header is older
than the payload's. It never deletes or rewrites any file it did not ship, and a
missing asset tree is a graceful skip (not every worktree is a Unity project yet).

The seeded guard is committed into the consumer project by story-finalize's
``git add -A`` — that is intended: the guard travels with the repo so any editor
that opens the project is protected. Because seeding happens pre-baseline (before
the unit's untracked-file baseline is snapshotted), ``verify.safe_rollback`` never
treats it as a created-this-unit file and never deletes it.

Env (injected by the Unity plugin):
  BMAD_LOOP_WORKTREE                    project root (Assets = <worktree>/Assets)
  BMAD_LOOP_UNITY_INSTALL_SCENE_GUARD   "1" (default) enables; "0"/false skips
  BMAD_LOOP_UNITY_SCENE_GUARD_DIR       install dir  (default Assets/BmadLoop/Editor)

Exit 0 = seeded, already-current, or a benign skip (disabled / no asset tree);
non-zero = a real error (no worktree, unreadable payload, a failed copy).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

# The guard's version header line, e.g. "// bmad-loop-scene-guard-version: 1.0.0".
# The payload's value is the source of truth; a target with an older (or missing)
# value is reinstalled, a newer/equal one is left alone.
_VERSION_RE = re.compile(r"bmad-loop-scene-guard-version:\s*([0-9]+(?:\.[0-9]+)*)")
# The guard's canonical source file — its header carries the version, and its
# presence/absence in the target decides a fresh install.
_GUARD_CS = "SceneAutoSaveGuard.cs"
# Payload subdir holding the parent-folder ``.meta`` files, keyed by folder name;
# separated from the content files so "every file in the payload root" is exactly
# the set copied into the install dir.
_FOLDERS_SUBDIR = "_folders"
_DEFAULT_GUARD_DIR = "Assets/BmadLoop/Editor"


def _is_absolute(value: str) -> bool:
    """True if ``value`` is rooted or drive-qualified in *either* POSIX or Windows
    terms (mirrors ``bmad_loop.platform_util.is_absolute_path`` — this deployed
    script is stdlib-only and cannot import core)."""
    win = PureWindowsPath(value)
    return PurePosixPath(value).is_absolute() or bool(win.drive or win.root)


def _has_parent_ref(value: str) -> bool:
    """True if ``value`` contains a ``..`` segment in *either* POSIX or Windows
    terms (mirrors ``bmad_loop.platform_util.has_parent_ref``)."""
    return ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts


def _names_tree_root(value: str) -> bool:
    """True if ``value`` names the worktree rather than anything inside it
    (mirrors ``bmad_loop.platform_util.names_tree_root``).

    The third member of the family, and the one this script was missing. Win32
    strips every trailing period and space from a path's final component, so
    ``"..."`` names the worktree root there while both pure flavours read it as an
    ordinary one-segment name. That mattered here: the asset-root probe below
    would find the worktree itself for such a value, so the payload landed in the
    worktree root instead of under ``Assets/``. (The caller once ``.strip()``-ed
    the env var before validating, which collapsed the *space* spellings into
    ``"."``; validation now sees the authored value, so every spelling lands
    here.)"""
    if PurePosixPath(value) == PurePosixPath(".") or PureWindowsPath(value) == PureWindowsPath("."):
        return True
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    return bool(parts) and all(part.strip(" .") == "" and part != ".." for part in parts)


# Reserved on Windows regardless of extension: CON.txt is as illegal as CON. Mirrors
# ``bmad_loop.platform_util._RESERVED_BASENAMES`` member for member, including its
# deliberate over-refusals: Microsoft's published list names only COM1-COM9 /
# LPT1-LPT9 and omits the console pair, while Wine's ``RtlIsDosDeviceName_U`` matches
# CONIN$/CONOUT$ but rejects the 0 forms, so COM0/LPT0 are backed by neither. Mirror
# the set, not any claim about it.
_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
    | {f"COM{s}" for s in "¹²³"}
    | {f"LPT{s}" for s in "¹²³"}
)


def _is_reserved_basename(seg: str) -> bool:
    """True if ``seg``'s basename (before the first dot, trailing spaces trimmed —
    ``CON .txt`` counts) is a Windows reserved device name (mirrors
    ``bmad_loop.platform_util._is_reserved_basename``)."""
    stem = seg.split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED_BASENAMES


def _names_win32_alias(value: str) -> bool:
    """True if any component of ``value`` names something other than itself on Win32 —
    a reserved device name, or a name whose trailing periods and spaces Win32 trims
    away before the path ever reaches the filesystem (mirrors
    ``bmad_loop.platform_util.names_win32_alias`` — this deployed script is stdlib-only
    and cannot import core).

    The fourth member of the family, and the only one about *determinism* rather than
    containment: ``"Assets/NUL"`` and ``"Assets/BmadLoop/Editor."`` are both inside the
    worktree by every measure the other three apply, and both name a different thing on
    Windows than they spell here. The ``not root_naming`` term hands a value made
    entirely of period/space components back to :func:`_names_tree_root` — while still
    catching one such component embedded beside a real one (``"Assets/..."``), which is
    nobody's root and nobody's parent — and the ``part not in (".", "..")`` carve-out
    hands plain ``".."`` back to :func:`_has_parent_ref`, so all four refuse disjoint
    spelling classes. Core's docstring carries the two rules, their sources, and the
    Windows 11 narrowing this deliberately does not track."""
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    root_naming = _names_tree_root(value)
    return any(
        _is_reserved_basename(part)
        or (part != part.rstrip(" .") and part not in (".", "..") and not root_naming)
        for part in parts
    )


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _worktree() -> Path | None:
    wt = os.environ.get("BMAD_LOOP_WORKTREE")
    return Path(wt) if wt else None


def _payload_dir() -> Path:
    """The bundled ``unity_assets/`` payload, resolved relative to this script so it
    travels with a project-local copy of the plugin too."""
    return Path(__file__).resolve().parent / "unity_assets"


def _parse_version(text: str) -> tuple[int, ...] | None:
    """The ``bmad-loop-scene-guard-version`` tuple from a guard source, or None if
    the header is absent (an absent/unrecognized header sorts as oldest)."""
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _read_version(cs_path: Path) -> tuple[int, ...] | None:
    try:
        return _parse_version(cs_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _content_files(payload: Path) -> list[Path]:
    """The payload files copied verbatim into the install dir: the guard source,
    the asmdef, and their ``.meta`` companions (everything directly in the payload
    root — the ``_folders`` subdir is handled separately)."""
    return sorted(p for p in payload.iterdir() if p.is_file())


def _ensure_dir_with_meta(directory: Path, payload: Path) -> None:
    """Create ``directory`` if absent and, when the payload ships a matching
    folder ``.meta`` (keyed by the folder's name), drop it beside the folder with
    its fixed GUID. Never clobbers an existing folder meta — its GUID may already
    be referenced by the project."""
    directory.mkdir(parents=True, exist_ok=True)
    folder_meta = payload / _FOLDERS_SUBDIR / f"{directory.name}.meta"
    target_meta = directory.parent / f"{directory.name}.meta"
    if folder_meta.is_file() and not target_meta.exists():
        shutil.copy2(folder_meta, target_meta)


def _install(worktree: Path, target_dir: Path, payload: Path) -> int:
    """Copy the payload into ``target_dir``, creating each parent folder (with its
    fixed-GUID meta) along the way. Returns 0 on success, non-zero on a real I/O
    error."""
    rel = target_dir.relative_to(worktree)
    try:
        # Create every path segment from the worktree down to the install dir,
        # laying a fixed-GUID folder meta beside each that the payload ships one for
        # (Assets itself has no payload meta — Unity owns it — so it is skipped).
        current = worktree
        for segment in rel.parts:
            current = current / segment
            _ensure_dir_with_meta(current, payload)
        for src in _content_files(payload):
            shutil.copy2(src, target_dir / src.name)
    except OSError as exc:
        print(f"unity_seed_assets: install failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"unity_seed_assets: seeded scene auto-save guard into {rel}",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    if not _truthy(os.environ.get("BMAD_LOOP_UNITY_INSTALL_SCENE_GUARD"), True):
        print("unity_seed_assets: scene guard disabled; skipping", file=sys.stderr)
        return 0

    worktree = _worktree()
    if worktree is None:
        print("unity_seed_assets: BMAD_LOOP_WORKTREE is not set", file=sys.stderr)
        return 2

    payload = _payload_dir()
    guard_src = payload / _GUARD_CS
    if not guard_src.is_file():
        print(f"unity_seed_assets: payload guard missing at {guard_src}", file=sys.stderr)
        return 2
    payload_version = _read_version(guard_src)

    # `.strip()` decides only whether the env var is SET — the authored value is
    # what gets validated. Stripping before validation silently trimmed the exact
    # spelling the guard below promises to refuse ("Assets/BmadLoop/Editor " was
    # trimmed and installed into Editor), and made this the one site of seven whose
    # config value was normalized before the family saw it.
    raw = os.environ.get("BMAD_LOOP_UNITY_SCENE_GUARD_DIR", "")
    guard_dir = raw if raw.strip() else _DEFAULT_GUARD_DIR
    rel = Path(guard_dir)
    # The install dir must stay inside the worktree AND name something in it: an
    # absolute/drive-qualified path would make _install's relative_to() raise, a
    # ".." segment would let the copy escape the project tree, and a root-naming
    # spelling would scatter the payload across the worktree root itself. The fourth
    # term is about determinism rather than containment: Win32 trims a component's
    # trailing periods and spaces, so "Assets/BmadLoop/Editor." installs into "Editor"
    # while the configured string still spells "Editor.", and a component naming a
    # reserved device writes to the device instead of the tree.
    if (
        not rel.parts
        or _names_tree_root(guard_dir)
        or _is_absolute(guard_dir)
        or _has_parent_ref(guard_dir)
        or _names_win32_alias(guard_dir)
    ):
        print(
            f"unity_seed_assets: invalid scene guard dir {guard_dir!r}: it must name a "
            "path inside the worktree and must not name a Windows device or end a "
            "component in a period or space",
            file=sys.stderr,
        )
        return 2
    target_dir = worktree / guard_dir

    # Only seed into a project whose asset root is actually checked out. A worktree
    # without it is not (yet) a Unity project, and scattering an Assets/ tree into
    # it would be wrong — a benign skip, never an error, never destructive.
    asset_root = worktree / rel.parts[0]
    if not asset_root.is_dir():
        print(
            f"unity_seed_assets: {rel.parts[0]}/ not present under the worktree; "
            "nothing to seed",
            file=sys.stderr,
        )
        return 0

    target_cs = target_dir / _GUARD_CS
    if target_cs.is_file():
        target_version = _read_version(target_cs)
        # An unreadable/absent header sorts as oldest, so a foreign or stale guard
        # is refreshed; an equal-or-newer guard is left untouched (idempotent).
        current = target_version or ()
        incoming = payload_version or ()
        if current >= incoming:
            print(
                "unity_seed_assets: scene guard already current "
                f"({'.'.join(map(str, current)) or 'unversioned'}); nothing to do",
                file=sys.stderr,
            )
            return 0

    return _install(worktree, target_dir, payload)


if __name__ == "__main__":
    raise SystemExit(main())
