"""Verified, content-addressed snapshots of granted plugin dirs (#496/#497).

A grant pins a plugin dir's digest at launch, but the live dir stays
agent-writable for the whole run: a hook script edited after the check would run
unpinned. So a granted dir is copied out of the tree, into
``<snapshots root>/<digest>/`` (`runs.plugin_snapshots_root`), and hooks and
in-process modules run from the copy.

`materialize` never publishes a dir it has not verified. A copy is built under a
private temp name from the bytes that were hashed (`VerifiedTree`), re-read with
`digest.read_tree` (``pycache="refuse"``) and compared against the tree's digest
AND file map, and only then renamed into place. An existing snapshot is reused
only when it passes the same check; one that does not (tampered, a planted
``__pycache__``, a link where the dir should be) is moved aside and replaced.

Concurrency is by rename. Two launches materializing the same digest each build
their own temp dir; the first rename wins, and the loser uses the winner once it
verifies. When no rename can land — Windows refuses to rename a dir some process
holds open — the verified temp dir itself is the answer: it is a complete,
checked copy, merely under a longer name. Its leftovers, like a ``.stale-`` dir
an ``rmtree`` could not finish, cost disk, not trust.

Writes go through the confined writers rooted at `runs.state_root()`, so a link
planted at any component below the state root refuses the write rather than
redirecting it (#593). Every write fault is a `PluginError`.

What this does not close: a process at the orchestrator's own privilege can
rewrite a published snapshot after it verified (#571).
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from ..platform_util import atomic_write_bytes_confined, is_link_like, make_dirs_confined
from .digest import VerifiedTree, read_tree
from .model import PluginError

# Confined writers create files at 0600; an executable file needs its bit back,
# or the snapshot's file map (and digest) would not match the tree's.
_EXEC_MODE = 0o700
_FILE_MODE = 0o600


def _matches(path: Path, tree: VerifiedTree) -> bool:
    """Whether ``path`` is a verified copy of ``tree``: a real dir (not a link),
    holding no bytecode cache, with the same digest and file map."""
    if path.is_symlink() or is_link_like(path):
        return False
    try:
        got = read_tree(path, pycache="refuse")
    except PluginError:
        return False
    return got.digest == tree.digest and got.file_map() == tree.file_map()


def _discard(path: Path) -> None:
    """Best-effort removal of a temp or stale entry, never through a link."""
    if path.is_symlink() or is_link_like(path):
        try:
            path.unlink()
        except OSError:
            try:
                path.rmdir()
            except OSError:
                pass
        return
    shutil.rmtree(path, ignore_errors=True)


def _set_mode(path: Path, executable: bool) -> None:
    """Restore the tree's exec bit on a freshly written file (POSIX only).

    Through an ``O_NOFOLLOW`` descriptor, so a link swapped in at the name since
    the write changes nothing outside the snapshot — the verify that follows
    refuses the link itself. Windows has no exec bit (and no ``os.fchmod``
    before 3.13), and the digest never records one there."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fchmod(fd, _EXEC_MODE if executable else _FILE_MODE)
    finally:
        os.close(fd)


def _build(tree: VerifiedTree, temp: Path, confine_root: Path) -> None:
    make_dirs_confined(temp, confine_root=confine_root)
    for f in tree.files:
        dest = temp.joinpath(*f.relpath.split("/"))
        if dest.parent != temp:
            make_dirs_confined(dest.parent, confine_root=confine_root)
        atomic_write_bytes_confined(dest, f.data, confine_root=confine_root)
        _set_mode(dest, f.executable)


def _publish(temp: Path, target: Path, tree: VerifiedTree, token: str) -> Path:
    """Rename the verified ``temp`` into ``target`` (see module doc on races)."""
    try:
        os.rename(temp, target)
        return target
    except OSError:
        pass
    if _matches(target, tree):  # a concurrent launch won with the same contents
        _discard(temp)
        return target
    if not os.path.lexists(target):
        return temp  # nothing in the way, yet the rename failed: keep the verified copy
    stale = target.with_name(f"{target.name}.stale-{token}")
    try:
        os.rename(target, stale)
    except OSError:
        return temp
    try:
        os.rename(temp, target)
    except OSError:
        if _matches(target, tree):
            _discard(temp)
            return target
        return temp
    finally:
        _discard(stale)
    return target


def materialize(tree: VerifiedTree, snapshots_root: Path) -> Path:
    """The path of a verified copy of ``tree`` under ``snapshots_root``.

    Reuses ``<snapshots_root>/<digest>/`` when it verifies, otherwise builds and
    publishes a fresh copy (see module doc). Raises `PluginError` when no
    verified copy can be produced."""
    from .. import runs

    target = snapshots_root / tree.digest
    if _matches(target, tree):
        return target
    token = secrets.token_hex(4)
    temp = snapshots_root / f"{tree.digest}.tmp-{os.getpid()}-{token}"
    try:
        confine_root = runs.state_root()
        make_dirs_confined(snapshots_root, confine_root=confine_root)
        _build(tree, temp, confine_root)
    except (runs.StateRootError, OSError) as exc:
        _discard(temp)
        raise PluginError(
            f"plugin snapshot {temp}: cannot write: {type(exc).__name__}: {exc}"
        ) from exc
    if not _matches(temp, tree):
        _discard(temp)
        raise PluginError(f"plugin snapshot {temp}: the copy did not verify")
    return _publish(temp, target, tree, token)
