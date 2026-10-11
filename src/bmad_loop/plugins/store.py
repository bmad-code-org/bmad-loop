"""The out-of-tree grant store: load and save `GrantSet`s (#496/#497).

The file itself — its path, the hardened read and the confined write — belongs
to `runs` (`runs.plugin_grants_path` carries what keeping it out of the tree does
and does not buy). This module is the plugin-side half: it turns that text into
records through the strict codec in `grants`, and back.

`load_grants` is observation and never raises: absence is an empty set with no
fault, and an unreadable or malformed store is an empty set WITH a fault, which a
launch reports rather than folding into "no grants". `save_grants` is a repair
write and raises. `upsert` and `remove` are pure.

Every grant mutation is a read-modify-write, and two can overlap (`plugins
revoke A` beside `plugins enable B`, or a CLI verb beside a TUI save). So
`save_grants` is a compare-and-swap: under the store's lock it re-reads the file
and refuses with `GrantStoreChanged` unless it still holds the records the
caller started from. Without that the later writer silently erases the earlier
one, and an enable that loaded before a revoke would restore the revoked grant.
The lock spans only that re-read and the write — the plugin-tree hashing a grant
needs runs before it, outside the hold (`platform_util.file_lock` gives up after
~10 s on Windows).

`runs` is imported inside the functions: it is a heavy module, and the plugin
package must stay importable from it without a cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .grants import GrantRecord, GrantSet, decode_grants, encode_grants


class GrantStoreChanged(RuntimeError):
    """The grant store no longer holds what a mutation started from: another
    writer got there first. Nothing was written; re-run against the new store."""


def load_grants(project: Path) -> GrantSet:
    """The project's recorded grants, with ``snapshot_root`` set (see module doc)."""
    from .. import runs

    try:
        snapshot_root: Path | None = runs.plugin_snapshots_root(project)
    except (runs.StateRootError, OSError, RuntimeError):
        snapshot_root = None  # the read below names the same fault
    text, fault = runs.read_plugin_grants_text(project)
    if text is None:
        return GrantSet(snapshot_root=snapshot_root, fault=fault)
    try:
        records = decode_grants(text)
    except ValueError as exc:
        path = runs.plugin_grants_path(project)
        return GrantSet(snapshot_root=snapshot_root, fault=f"{path}: malformed: {exc}")
    return GrantSet(records=records, snapshot_root=snapshot_root)


def save_grants(
    project: Path, records: Iterable[GrantRecord], *, expected: GrantSet | None
) -> None:
    """Replace the project's grant store with ``records``; raises on any fault.

    ``expected`` is the store the caller read and derived ``records`` from: under
    the store's lock the file is re-read, and unless it still holds exactly those
    records (and still reads cleanly) :class:`GrantStoreChanged` is raised and
    nothing is written (see module doc). ``None`` writes unconditionally, for
    seeding a store no one else can be mutating."""
    from .. import runs
    from ..platform_util import file_lock

    text = encode_grants(records)
    with file_lock(runs.lock_path_for(runs.plugin_grants_path(project))):
        if expected is not None:
            current = load_grants(project)
            if current.fault is not None or current.records != expected.records:
                raise GrantStoreChanged(
                    f"plugin grants in {runs.plugin_grants_path(project)} changed while "
                    "this was being decided; nothing was written — re-run"
                )
        runs.write_plugin_grants_text(project, text)


def upsert(grants: GrantSet, record: GrantRecord) -> GrantSet:
    """``grants`` with ``record`` added, replacing any record of the same name."""
    kept = [r for r in grants.records if r.name != record.name]
    return replace(grants, records=tuple(sorted([*kept, record], key=lambda r: r.name)))


def remove(grants: GrantSet, name: str) -> GrantSet:
    """``grants`` without the record named ``name`` (unchanged when there is none)."""
    return replace(grants, records=tuple(r for r in grants.records if r.name != name))
