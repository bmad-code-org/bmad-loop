"""The out-of-tree grant store (#496/#497): load/save round-trip through the
hardened `runs` pair, faults surfaced rather than raised, pure set edits."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bmad_loop import runs
from bmad_loop.plugins import digest, store
from bmad_loop.plugins.grants import GrantRecord, GrantSet


def _record(tmp_path: Path, name: str, body: str = "print('hi')\n") -> GrantRecord:
    root = tmp_path / "trees" / name
    (root / "hooks").mkdir(parents=True)
    (root / "plugin.toml").write_text(f'[plugin]\nname = "{name}"\n')
    (root / "hooks" / "run.py").write_text(body)
    tree = digest.read_tree(root)
    return GrantRecord.from_tree(name, f".bmad-loop/plugins/{name}", tree, "2026-10-10T00:00:00Z")


def test_absent_store_loads_empty_with_no_fault(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    got = store.load_grants(project)
    assert got.records == ()
    assert got.fault is None
    assert got.snapshot_root == runs.plugin_snapshots_root(project)


def test_grants_round_trip_through_the_store(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    b, a = _record(tmp_path, "beta"), _record(tmp_path, "alpha")

    store.save_grants(project, [b, a])
    got = store.load_grants(project)

    assert got.fault is None
    assert got.records == (a, b)  # sorted by name
    assert got.get("alpha") == a
    assert got.snapshot_root == runs.plugin_snapshots_root(project)
    assert runs.plugin_grants_path(project).is_file()
    assert not (project / ".bmad-loop").exists()  # never in the tree


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"version": 2, "plugins": {}}', '{"version": 1, "plugins": {"x": 1}}'],
    ids=["not-json", "not-an-object", "wrong-version", "bad-record"],
)
def test_corrupt_store_loads_empty_with_a_fault_and_never_raises(tmp_path, text):
    """A corrupt store is reported, never folded into a healthy-looking empty set
    and never raised into a launch.

    ABLATION: drop the ValueError arm in `load_grants` and every row raises;
    return `GrantSet()` from it and every row fails on `fault`."""
    project = tmp_path / "proj"
    project.mkdir()
    runs.write_plugin_grants_text(project, text)

    got = store.load_grants(project)

    assert got.records == ()
    assert got.fault is not None
    assert got.fault.startswith(f"{runs.plugin_grants_path(project)}: malformed: ")
    assert got.snapshot_root == runs.plugin_snapshots_root(project)


def test_unreadable_store_carries_the_read_fault(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = runs.plugin_grants_path(project)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    got = store.load_grants(project)
    assert got.records == ()
    assert got.fault == f"{path}: not UTF-8 text"


def test_upsert_and_remove_are_pure(tmp_path):
    a1, b = _record(tmp_path, "alpha"), _record(tmp_path, "beta")
    a2 = _record(tmp_path / "v2", "alpha", body="print('changed')\n")
    assert a1.digest != a2.digest
    base = GrantSet(records=(a1,), snapshot_root=tmp_path / "snaps")

    added = store.upsert(base, b)
    replaced = store.upsert(added, a2)
    dropped = store.remove(replaced, "alpha")

    assert base.records == (a1,)  # the input is never edited
    assert added.records == (a1, b)
    assert replaced.records == (a2, b)
    assert dropped.records == (b,)
    assert store.remove(dropped, "missing") == dropped
    assert {s.snapshot_root for s in (added, replaced, dropped)} == {tmp_path / "snaps"}
    with pytest.raises(FrozenInstanceError):
        base.records = ()  # type: ignore[misc]
