"""Verified, content-addressed plugin snapshots (#496/#497).

A granted plugin runs from a copy out of the tree, so a snapshot must be exactly
the tree that was hashed — never an unverified dir, never one carrying a planted
bytecode cache — and a tampered one must be rebuilt, not reused.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from bmad_loop import runs
from bmad_loop.plugins import snapshot
from bmad_loop.plugins.digest import VerifiedTree, read_tree

FILES = {
    "plugin.toml": '[plugin]\nname = "p"\n',
    "hooks/run.py": "print('hi')\n",
    "lib/deep/util.py": "X = 1\n",
}


def _tree(tmp_path: Path, files: dict[str, str] = FILES) -> VerifiedTree:
    root = tmp_path / "live" / "p"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return read_tree(root)


def _snapshots_root(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return runs.plugin_snapshots_root(project)


def _leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if ".tmp-" in p.name or ".stale-" in p.name)


def test_materialize_creates_a_verified_tree(tmp_path):
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)

    snap = snapshot.materialize(tree, root)

    assert snap == root / tree.digest
    got = read_tree(snap, pycache="refuse")
    assert got.digest == tree.digest
    assert got.file_map() == tree.file_map()
    assert (snap / "lib" / "deep" / "util.py").read_text() == "X = 1\n"
    assert _leftovers(root) == []


@pytest.mark.skipif(os.name == "nt", reason="no exec bit on Windows")
def test_materialize_preserves_the_exec_bit(tmp_path):
    """The confined writer creates 0600; a hook script granted executable must stay
    executable, or the snapshot's digest would not match the grant's.

    ABLATION: drop `_set_mode` and the copy never verifies (PluginError)."""
    tree_root = tmp_path / "live" / "p"
    tree_root.mkdir(parents=True)
    script = tree_root / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (tree_root / "plain.txt").write_text("x\n")
    tree = read_tree(tree_root)

    snap = snapshot.materialize(tree, _snapshots_root(tmp_path))

    assert os.stat(snap / "run.sh").st_mode & stat.S_IXUSR
    assert not os.stat(snap / "plain.txt").st_mode & 0o111
    assert read_tree(snap).file_map() == tree.file_map()


def test_materialize_reuses_a_matching_snapshot(tmp_path):
    """A verified existing snapshot is reused as-is: no rewrite, no new temp dir."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    snap = snapshot.materialize(tree, root)
    stamp = (snap / "hooks" / "run.py").stat().st_mtime_ns
    os.utime(snap / "hooks" / "run.py", ns=(stamp - 10**9, stamp - 10**9))
    before = (snap / "hooks" / "run.py").stat().st_mtime_ns

    again = snapshot.materialize(tree, root)

    assert again == snap
    assert (snap / "hooks" / "run.py").stat().st_mtime_ns == before
    assert sorted(p.name for p in root.iterdir()) == [tree.digest]


def test_materialize_rebuilds_a_tampered_snapshot(tmp_path):
    """A snapshot edited after it was published is never reused: it is moved aside
    and replaced with a verified copy.

    ABLATION: make the reuse check compare the dir name only (return True from
    `_matches` whenever the target exists) and the tampered bytes come back."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    snap = snapshot.materialize(tree, root)
    (snap / "hooks" / "run.py").write_text("print('pwned')\n")

    again = snapshot.materialize(tree, root)

    assert again == snap
    assert (snap / "hooks" / "run.py").read_text() == "print('hi')\n"
    assert read_tree(snap, pycache="refuse").digest == tree.digest
    assert _leftovers(root) == []


def test_materialize_rebuilds_when_pycache_is_planted(tmp_path):
    """A `__pycache__` dir is invisible to the default digest (`exclude`), so a
    snapshot carrying one would still digest-match — and Python would happily load
    the planted bytecode. The reuse check reads with `pycache="refuse"`.

    ABLATION: verify with the default `pycache="exclude"` in `_matches` and the
    planted cache survives."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    snap = snapshot.materialize(tree, root)
    cache = snap / "lib" / "deep" / "__pycache__"
    cache.mkdir()
    (cache / "util.cpython-312.pyc").write_bytes(b"evil")

    again = snapshot.materialize(tree, root)

    assert again == snap
    assert not cache.exists()
    assert read_tree(snap, pycache="refuse").digest == tree.digest


def test_repeated_materialize_after_mismatch_leaves_a_valid_tree(tmp_path):
    """Rebuild, then reuse: the second call after a mismatch finds the rebuilt copy
    valid and leaves no temp or stale dirs behind."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    snap = snapshot.materialize(tree, root)
    (snap / "extra.py").write_text("planted\n")

    for _ in range(3):
        assert snapshot.materialize(tree, root) == snap
        assert read_tree(snap, pycache="refuse").file_map() == tree.file_map()
    assert sorted(p.name for p in root.iterdir()) == [tree.digest]


def test_materialize_falls_back_to_the_verified_temp_dir_when_no_rename_lands(
    tmp_path, monkeypatch
):
    """Windows refuses to rename a dir some process holds open. Then the verified
    temp copy is the answer — complete and checked — rather than the stale target
    or a failure."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    snap = snapshot.materialize(tree, root)
    (snap / "hooks" / "run.py").write_text("print('pwned')\n")

    def _in_use(src, dst):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(snapshot.os, "rename", _in_use)
    got = snapshot.materialize(tree, root)

    assert got != snap and got.parent == root
    assert got.name.startswith(f"{tree.digest}.tmp-{os.getpid()}-")
    assert read_tree(got, pycache="refuse").file_map() == tree.file_map()


def test_materialize_uses_a_concurrent_winner(tmp_path, monkeypatch):
    """Two launches materializing the same digest: the loser's rename fails because
    the winner's copy is already in place, and the loser uses that copy once it
    verifies, discarding its own temp.

    ABLATION: drop the winner arm in `_publish` and the loser moves the winner's
    dir aside and publishes its own — the inode assert fails."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    real_rename = os.rename
    winner: list[Path] = []
    raced = False

    def _race(src, dst):
        nonlocal raced
        if not raced:  # the other launch publishes first
            raced = True
            winner.append(snapshot.materialize(tree, root))
            inode.append(winner[0].stat().st_ino)
        return real_rename(src, dst)

    inode: list[int] = []
    monkeypatch.setattr(snapshot.os, "rename", _race)
    got = snapshot.materialize(tree, root)

    assert got == root / tree.digest == winner[0]
    # the winner's copy is kept, not displaced by the loser's identical one —
    # a dir another launch may already be executing from stays put
    assert got.stat().st_ino == inode[0]
    assert read_tree(got, pycache="refuse").file_map() == tree.file_map()
    assert _leftovers(root) == []


def test_materialize_never_publishes_an_unverified_copy(tmp_path, monkeypatch):
    """A copy that does not verify is discarded and refused, never published.

    ABLATION: drop the temp-dir verify in `materialize` and the corrupt copy is
    published at the target."""
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)
    real_write = snapshot.atomic_write_bytes_confined

    def _corrupt(path, data, *, confine_root):
        real_write(path, data + b"#", confine_root=confine_root)

    monkeypatch.setattr(snapshot, "atomic_write_bytes_confined", _corrupt)
    with pytest.raises(snapshot.PluginError, match="did not verify"):
        snapshot.materialize(tree, root)

    assert not (root / tree.digest).exists()
    assert _leftovers(root) == []


def test_materialize_write_fault_is_a_plugin_error(tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    root = _snapshots_root(tmp_path)

    def _full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snapshot, "atomic_write_bytes_confined", _full)
    with pytest.raises(snapshot.PluginError, match="cannot write"):
        snapshot.materialize(tree, root)
    assert _leftovers(root) == []


def test_snapshots_land_under_the_isolated_state_root(tmp_path):
    """Snapshots live out of the project tree, under the (test-isolated) state root
    — confined writes there, never in the agent-writable checkout."""
    tree = _tree(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    snap = snapshot.materialize(tree, runs.plugin_snapshots_root(project))

    assert snap.is_relative_to(runs.state_root())
    assert not snap.is_relative_to(project)
    assert not snap.is_relative_to(tmp_path / "live")


def test_materialize_refuses_a_root_outside_the_state_root(tmp_path):
    """The confined writers are rooted at the state root, so a snapshots root
    anywhere else is refused rather than written."""
    tree = _tree(tmp_path)
    with pytest.raises(snapshot.PluginError, match="cannot write"):
        snapshot.materialize(tree, tmp_path / "elsewhere")
