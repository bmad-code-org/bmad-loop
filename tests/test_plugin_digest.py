"""Plugin-dir content digest: the identity a grant pins (#496/#497).

The digest must move on every change that could change what runs (content, a new
file, an exec-bit flip) and on nothing else (creation order). The reader must
refuse every tree shape it cannot hash honestly — a link, a FIFO, an oversized
tree — with a PluginError, never a partial answer and never a hang.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bmad_loop.plugins import PluginError, digest
from bmad_loop.plugins.digest import diff_files, read_tree


def write_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


BASE = {"plugin.toml": '[plugin]\nname = "p"\n', "hooks/run.py": "print('hi')\n"}


def test_digest_is_independent_of_creation_order(tmp_path):
    a = write_tree(tmp_path / "a", BASE)
    b = write_tree(tmp_path / "b", dict(reversed(list(BASE.items()))))
    ta, tb = read_tree(a), read_tree(b)
    assert ta.digest == tb.digest
    assert [f.relpath for f in ta.files] == ["hooks/run.py", "plugin.toml"]
    assert ta.file_map() == tb.file_map()


def test_digest_framing_distinguishes_split_contents(tmp_path):
    # the same concatenated bytes split differently across names must not collide
    a = write_tree(tmp_path / "a", {"f1": "ab", "f2": "c"})
    b = write_tree(tmp_path / "b", {"f1": "a", "f2": "bc"})
    c = write_tree(tmp_path / "c", {"ab": "c"})
    d = write_tree(tmp_path / "d", {"a": "bc"})
    digests = {read_tree(p).digest for p in (a, b, c, d)}
    assert len(digests) == 4


def test_content_change_moves_the_digest(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    before = read_tree(root).digest
    (root / "hooks/run.py").write_text("print('pwned')\n")
    assert read_tree(root).digest != before


def test_content_is_hashed_raw_without_crlf_normalisation(tmp_path):
    a = write_tree(tmp_path / "a", {"f": b"x\n"})
    b = write_tree(tmp_path / "b", {"f": b"x\r\n"})
    assert read_tree(a).digest != read_tree(b).digest


def test_added_file_moves_the_digest(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    before = read_tree(root).digest
    (root / ".hidden.sh").write_text("")
    after = read_tree(root)
    assert after.digest != before
    assert ".hidden.sh" in after.file_map()


@pytest.mark.skipif(os.name == "nt", reason="no POSIX exec bit")
def test_exec_bit_flip_moves_the_digest(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    before = read_tree(root)
    assert before.file_map()["hooks/run.py"].endswith(":x") is False
    (root / "hooks/run.py").chmod(0o755)
    after = read_tree(root)
    assert after.digest != before.digest
    assert after.file_map()["hooks/run.py"].endswith(":x")


def test_pycache_is_excluded_by_default(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    before = read_tree(root).digest
    write_tree(root, {"__pycache__/hooks.cpython-311.pyc": b"\x00", "hooks/__pycache__/x.pyc": b""})
    tree = read_tree(root)
    assert tree.digest == before
    assert not any("__pycache__" in f.relpath for f in tree.files)


def test_pycache_refused_in_refuse_mode(tmp_path):
    root = write_tree(tmp_path / "p", {**BASE, "hooks/__pycache__/x.pyc": b""})
    with pytest.raises(PluginError, match="bytecode cache"):
        read_tree(root, pycache="refuse")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_file_is_refused(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    outside = tmp_path / "outside.py"
    outside.write_text("print('evil')\n")
    (root / "hooks/evil.py").symlink_to(outside)
    with pytest.raises(PluginError, match="is a link"):
        read_tree(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_subdir_is_refused(tmp_path):
    root = write_tree(tmp_path / "p", BASE)
    outside = write_tree(tmp_path / "outside", {"evil.py": "print('evil')\n"})
    (root / "lib").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PluginError, match="is a link"):
        read_tree(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_root_is_followed(tmp_path):
    # the operator chose where the plugin root points; only links INSIDE are refused
    real = write_tree(tmp_path / "real", BASE)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert read_tree(link).digest == read_tree(real).digest


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_fifo_is_refused_without_hanging(tmp_path):
    """Guarded by SIGALRM because the failure mode is a hang: a reader that opened
    the FIFO blocking would never return (pattern: test_runs.py FIFO tests)."""
    import signal

    root = write_tree(tmp_path / "p", BASE)
    os.mkfifo(root / "hooks" / "pipe")

    def _blew_up(signum, frame):
        raise AssertionError("the read blocked on the FIFO instead of refusing it")

    previous = signal.signal(signal.SIGALRM, _blew_up)
    signal.alarm(20)
    try:
        with pytest.raises(PluginError, match="not a regular file"):
            read_tree(root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_file_swapped_for_a_fifo_is_refused_on_the_fd(tmp_path, monkeypatch):
    """The listing said "regular file"; the open fd says otherwise. The shape is
    re-checked on the fd, so a swap between scandir and open is refused."""
    import signal

    root = write_tree(tmp_path / "p", {"plugin.toml": ""})
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    real_open = os.open
    monkeypatch.setattr(digest.os, "open", lambda _p, flags: real_open(pipe, flags))

    def _blew_up(signum, frame):
        raise AssertionError("the read blocked on the FIFO instead of refusing it")

    previous = signal.signal(signal.SIGALRM, _blew_up)
    signal.alarm(20)
    try:
        with pytest.raises(PluginError, match="not a regular file"):
            digest._read_file(str(root / "plugin.toml"), "plugin.toml", digest.MAX_TOTAL_BYTES)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_file_count_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MAX_FILES", 3)
    ok = write_tree(tmp_path / "ok", {"a": "", "b": "", "c": ""})
    assert len(read_tree(ok).files) == 3
    over = write_tree(tmp_path / "over", {"a": "", "b": "", "sub/c": "", "sub/d": ""})
    with pytest.raises(PluginError, match="more than 3 files"):
        read_tree(over)


def test_entry_cap_counts_directories(tmp_path, monkeypatch):
    # a tree of empty dirs holds no files, so the file cap alone never fires
    monkeypatch.setattr(digest, "MAX_ENTRIES", 4)
    ok = tmp_path / "ok"
    (ok / "a" / "b").mkdir(parents=True)
    (ok / "c").mkdir()
    (ok / "plugin.toml").write_text("")
    assert len(read_tree(ok).files) == 1
    over = tmp_path / "over"
    for d in ("a", "b", "c", "d", "e"):
        (over / d).mkdir(parents=True)
    with pytest.raises(PluginError, match="more than 4 entries"):
        read_tree(over)


def test_file_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MAX_FILE_BYTES", 8)
    ok = write_tree(tmp_path / "ok", {"a": b"x" * 8})
    assert read_tree(ok).files[0].data == b"x" * 8
    over = write_tree(tmp_path / "over", {"a": b"x" * 9})
    with pytest.raises(PluginError, match="larger than 8 bytes"):
        read_tree(over)


def test_total_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MAX_TOTAL_BYTES", 10)
    ok = write_tree(tmp_path / "ok", {"a": b"x" * 5, "b": b"x" * 5})
    assert len(read_tree(ok).files) == 2
    over = write_tree(tmp_path / "over", {"a": b"x" * 5, "b": b"x" * 6})
    with pytest.raises(PluginError, match="in total"):
        read_tree(over)


def test_missing_root_is_a_plugin_error(tmp_path):
    with pytest.raises(PluginError, match="cannot list"):
        read_tree(tmp_path / "absent")


def test_diff_files_reports_added_removed_changed():
    old = {"keep": "1" * 64, "gone": "2" * 64, "edit": "3" * 64, "flip": "4" * 64}
    new = {"keep": "1" * 64, "edit": "5" * 64, "flip": "4" * 64 + ":x", "new": "6" * 64}
    assert diff_files(old, new) == (("new",), ("gone",), ("edit", "flip"))


def test_map_digest_matches_tree_digest(tmp_path):
    tree = read_tree(write_tree(tmp_path / "p", BASE))
    assert digest.map_digest(tree.file_map()) == tree.digest
