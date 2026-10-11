"""The out-of-tree grant store: load and save `GrantSet`s (#496/#497).

The file itself — its path, the hardened read and the confined write — belongs
to `runs` (`runs.plugin_grants_path` carries what keeping it out of the tree does
and does not buy). This module is the plugin-side half: it turns that text into
records through the strict codec in `grants`, and back.

`load_grants` is observation and never raises: absence is an empty set with no
fault, and an unreadable or malformed store is an empty set WITH a fault, which a
launch reports rather than folding into "no grants". `save_grants` is a repair
write and raises. `upsert` and `remove` are pure.

`runs` is imported inside the functions: it is a heavy module, and the plugin
package must stay importable from it without a cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .grants import GrantRecord, GrantSet, decode_grants, encode_grants


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


def save_grants(project: Path, records: Iterable[GrantRecord]) -> None:
    """Replace the project's grant store with ``records``; raises on any fault."""
    from .. import runs

    runs.write_plugin_grants_text(project, encode_grants(records))


def upsert(grants: GrantSet, record: GrantRecord) -> GrantSet:
    """``grants`` with ``record`` added, replacing any record of the same name."""
    kept = [r for r in grants.records if r.name != record.name]
    return replace(grants, records=tuple(sorted([*kept, record], key=lambda r: r.name)))


def remove(grants: GrantSet, name: str) -> GrantSet:
    """``grants`` without the record named ``name`` (unchanged when there is none)."""
    return replace(grants, records=tuple(r for r in grants.records if r.name != name))
