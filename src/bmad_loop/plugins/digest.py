"""Content digest of a plugin directory — the identity a grant pins.

A name does not identify the code it trusts (#496/#497), so a project plugin's
grant pins a sha256 over its WHOLE directory: manifest, scripts and modules
alike. Pinning ``plugin.toml`` alone while the files beside it stay free would be
direnv's transitive-trust gap one for one.

`read_tree` is the only reader. It is deliberately strict, because the dir is
agent-writable and its answer is what later executes:

  * the walk never follows a link, and refuses one — a POSIX symlink or a Windows
    junction — anywhere below the root (the root itself may be a link: the
    operator chose where it points);
  * anything that is neither a regular file nor a dir (a FIFO, a socket, a
    device) is refused, and each file's shape is checked on the OPEN fd, not the
    name, so a swap between listing and open cannot feed a FIFO into the read;
  * ``__pycache__`` at any depth is skipped (``exclude``) or refused (``refuse``,
    the snapshot check) — a bytecode cache is not source and is never trusted;
  * entry count (files, dirs and skipped caches alike, counted as they are
    listed), file count, per-file and total size are capped, so a tampered tree
    costs a refusal rather than the orchestrator's memory.

Every fault is a `PluginError` and no partial tree is ever returned: a digest
over the readable subset would be a digest of something nobody granted.

`platform_util.walk_files_unlinked` is NOT reused here: it prunes links and
swallows errors, which is right for a copy and wrong for an identity.

The digest frames each file as ``[relpath, sha256, executable]`` inside one JSON
document, sorted by the relpath's filesystem bytes. Relpaths are POSIX and the
bytes are raw — no CRLF normalisation and no case folding (``normcase`` is only
the loader's overlay key), so the same tree digests the same on every platform
whose checkout produced the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..platform_util import is_link_like
from .model import PluginError

MAX_FILES = 2048
MAX_ENTRIES = 2 * MAX_FILES
MAX_FILE_BYTES = 16 << 20
MAX_TOTAL_BYTES = 64 << 20

DIGEST_VERSION = 1
PYCACHE_DIR = "__pycache__"
EXEC_SUFFIX = ":x"

PycacheMode = Literal["exclude", "refuse"]

# win32 has no O_NOFOLLOW/O_NONBLOCK; O_BINARY stops CRLF translation on the raw fd.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


@dataclass(frozen=True)
class TreeFile:
    """One regular file of a verified plugin tree, with the bytes that were hashed."""

    relpath: str
    sha256: str
    executable: bool
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class VerifiedTree:
    """A plugin dir read whole by `read_tree`: its files and their digest."""

    root: Path
    files: tuple[TreeFile, ...]
    digest: str

    def file_map(self) -> dict[str, str]:
        """``relpath -> "<sha>"``, or ``"<sha>:x"`` for an executable file."""
        return {f.relpath: file_entry(f.sha256, f.executable) for f in self.files}


def file_entry(sha256: str, executable: bool) -> str:
    """The `VerifiedTree.file_map` value for one file."""
    return sha256 + EXEC_SUFFIX if executable else sha256


def _fsorted(relpaths: Iterable[str]) -> list[str]:
    return sorted(relpaths, key=os.fsencode)


def _digest_entries(entries: Iterable[tuple[str, str, bool]]) -> str:
    rows = sorted(entries, key=lambda e: os.fsencode(e[0]))
    payload = {"v": DIGEST_VERSION, "files": [[rel, sha, x] for rel, sha, x in rows]}
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def tree_digest(files: Iterable[TreeFile]) -> str:
    """The sha256 identity of a set of tree files (order-independent)."""
    return _digest_entries((f.relpath, f.sha256, f.executable) for f in files)


def map_digest(file_map: Mapping[str, str]) -> str:
    """`tree_digest` recomputed from a `VerifiedTree.file_map`-shaped mapping,
    so a stored grant record can be checked for internal consistency."""
    entries: list[tuple[str, str, bool]] = []
    for rel, value in file_map.items():
        executable = value.endswith(EXEC_SUFFIX)
        sha = value[: -len(EXEC_SUFFIX)] if executable else value
        entries.append((rel, sha, executable))
    return _digest_entries(entries)


def diff_files(
    old: Mapping[str, str], new: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(added, removed, changed)`` relpaths between two file maps, each sorted.

    ``changed`` covers a content change and an exec-bit flip alike — both move
    the digest, so both are what the operator re-accepts."""
    added = tuple(_fsorted(new.keys() - old.keys()))
    removed = tuple(_fsorted(old.keys() - new.keys()))
    changed = tuple(_fsorted(r for r in old.keys() & new.keys() if old[r] != new[r]))
    return added, removed, changed


def _read_file(path: str, rel: str, budget: int) -> tuple[bytes, bool]:
    """The bytes and exec bit of one regular file, or PluginError.

    The shape check is on the fd: the name was listed as a regular file, but it
    may have been swapped since, and O_NOFOLLOW + O_NONBLOCK make that swap cost
    an error rather than a followed link or a blocked read."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as e:
        raise PluginError(f"plugin file {rel}: cannot open: {type(e).__name__}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PluginError(f"plugin file {rel}: not a regular file")
        cap = min(MAX_FILE_BYTES, budget)
        chunks: list[bytes] = []
        got = 0
        while got <= cap:
            chunk = os.read(fd, cap + 1 - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    except OSError as e:
        raise PluginError(f"plugin file {rel}: cannot read: {type(e).__name__}: {e}") from e
    finally:
        os.close(fd)
    if got > MAX_FILE_BYTES:
        raise PluginError(f"plugin file {rel}: larger than {MAX_FILE_BYTES} bytes")
    if got > budget:
        raise PluginError(f"plugin dir: larger than {MAX_TOTAL_BYTES} bytes in total")
    executable = os.name != "nt" and bool(st.st_mode & 0o111)
    return b"".join(chunks), executable


def read_tree(root: Path, *, pycache: PycacheMode = "exclude") -> VerifiedTree:
    """Read a plugin dir whole and digest it, or raise PluginError (see module doc)."""
    files: list[TreeFile] = []
    total = 0
    seen = 0
    pending: list[tuple[str, str]] = [(os.fspath(root), "")]
    while pending:
        dir_path, dir_rel = pending.pop()
        where = dir_rel or "."
        try:
            with os.scandir(dir_path) as it:
                # counted while listing, before the sort: a dir of dirs (or one
                # huge listing) costs a refusal, not an unbounded walk or sort
                listed: list[os.DirEntry[str]] = []
                for entry in it:
                    seen += 1
                    if seen > MAX_ENTRIES:
                        raise PluginError(f"plugin dir {root}: more than {MAX_ENTRIES} entries")
                    listed.append(entry)
            entries = sorted(listed, key=lambda e: os.fsencode(e.name))
        except OSError as e:
            raise PluginError(
                f"plugin dir {root}: cannot list {where}: {type(e).__name__}: {e}"
            ) from e
        for entry in entries:
            rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
            try:
                if entry.name == PYCACHE_DIR:
                    if pycache == "refuse":
                        raise PluginError(f"plugin dir {root}: {rel}: bytecode cache present")
                    continue
                if entry.is_symlink() or is_link_like(Path(entry.path)):
                    raise PluginError(f"plugin dir {root}: {rel}: is a link")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((entry.path, rel))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise PluginError(f"plugin dir {root}: {rel}: not a regular file")
            except OSError as e:
                raise PluginError(
                    f"plugin dir {root}: {rel}: cannot stat: {type(e).__name__}: {e}"
                ) from e
            if len(files) >= MAX_FILES:
                raise PluginError(f"plugin dir {root}: more than {MAX_FILES} files")
            data, executable = _read_file(entry.path, rel, MAX_TOTAL_BYTES - total)
            total += len(data)
            files.append(TreeFile(rel, hashlib.sha256(data).hexdigest(), executable, data))
    ordered = tuple(sorted(files, key=lambda f: os.fsencode(f.relpath)))
    return VerifiedTree(root=root, files=ordered, digest=tree_digest(ordered))
