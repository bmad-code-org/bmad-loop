"""`bmad_loop.win32_at` — the handle-relative ``*at()`` family on Windows.

Every row here but the last is Windows-only: the module builds its bindings
under ``sys.platform == "win32"`` alone, and there is no faking ``NtCreateFile``.
They run on the Windows CI legs, which is the only place the arm that replaced
the DW-309/DW-310 fail-closed pause is exercised for real; the recovery rows in
tests/test_recovery_flow.py ride on these primitives and pin the flow, not the
syscalls. Junctions stand in for directory symlinks throughout — ``mklink /J``
needs no elevation, a directory symlink needs SeCreateSymbolicLinkPrivilege or
Developer Mode, and the junction is the redirect an unprivileged session can
actually plant, so it is the one the walk must refuse.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from bmad_loop import win32_at
from bmad_loop.platform_util import is_link_like

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-relative opens")


def _junction(link: Path, target: Path) -> None:
    import _winapi  # Windows-only stdlib module; CPython's own tests use it the same way

    _winapi.CreateJunction(str(target), str(link))


@contextmanager
def _directory(path: Path):
    fd = win32_at.open_directory(path)
    try:
        yield fd
    finally:
        os.close(fd)


# ---------------------------------------------------------------- open_directory


@WINDOWS
def test_open_directory_hands_back_a_descriptor_on_that_directory(tmp_path):
    with _directory(tmp_path) as fd:
        observed = os.fstat(fd)
    assert stat.S_ISDIR(observed.st_mode)
    assert os.path.samestat(observed, tmp_path.stat())


@WINDOWS
def test_open_directory_refuses_a_file_and_a_missing_path(tmp_path):
    (tmp_path / "file").write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        win32_at.open_directory(tmp_path / "file")
    with pytest.raises(FileNotFoundError):
        win32_at.open_directory(tmp_path / "absent")


@WINDOWS
def test_open_directory_follows_a_junction_unless_told_not_to(tmp_path):
    """The root of a confined walk is opened WITH following, as the POSIX walk's
    root is: the operator chooses where the project lives. ``follow=False`` is
    the ``O_NOFOLLOW`` answer for the same entry, ``ELOOP``."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _junction(link, real)

    with _directory(link) as fd:
        assert os.path.samestat(os.fstat(fd), real.stat())
    with pytest.raises(OSError) as refused:
        win32_at.open_directory(link, follow=False)
    assert refused.value.errno == errno.ELOOP


# ----------------------------------------------------------------------- open_at


@WINDOWS
def test_open_at_walks_components_relative_to_the_handle_above(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    with _directory(tmp_path) as root:
        a = win32_at.open_at(root, "a", os.O_RDONLY | win32_at.AT_DIRECTORY | win32_at.AT_NOFOLLOW)
        try:
            b = win32_at.open_at(a, "b", os.O_RDONLY | win32_at.AT_DIRECTORY | win32_at.AT_NOFOLLOW)
        finally:
            os.close(a)
        try:
            assert os.path.samestat(os.fstat(b), nested.stat())
        finally:
            os.close(b)


@WINDOWS
def test_open_at_refuses_anything_but_a_single_component():
    for name in ("", ".", "..", "a\\b", "a/b", "a\x00b"):
        with pytest.raises(ValueError, match="single path component"):
            win32_at.open_at(0, name, os.O_RDONLY)


@WINDOWS
def test_open_at_refuses_the_flags_it_cannot_honour(tmp_path):
    """``O_TRUNC``/``O_APPEND`` have no handle-relative caller and no exact NT
    spelling here; a loud refusal beats a silent approximation."""
    with _directory(tmp_path) as root:
        for flag in (os.O_TRUNC, os.O_APPEND):
            with pytest.raises(ValueError, match="O_TRUNC/O_APPEND"):
                win32_at.open_at(root, "x", os.O_WRONLY | os.O_CREAT | flag)
    assert list(tmp_path.iterdir()) == []


@WINDOWS
def test_open_at_creates_exclusively_and_the_descriptor_is_an_ordinary_crt_fd(tmp_path):
    """The whole point of wrapping the handle: the anchored writer keeps calling
    ``os.write``/``os.fsync``/``os.fdopen``/``os.fstat``/``os.close`` on it."""
    with _directory(tmp_path) as root:
        fd = win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "r+b") as fh:
            fh.write(b"payload\r\n")
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            assert fh.read() == b"payload\r\n"  # binary: no newline translation
            assert stat.S_ISREG(os.fstat(fh.fileno()).st_mode)
        with pytest.raises(FileExistsError):
            win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        with pytest.raises(FileNotFoundError):
            win32_at.open_at(root, "absent", os.O_RDONLY)
    assert (tmp_path / "spec.tmp").read_bytes() == b"payload\r\n"


@WINDOWS
def test_open_at_reads_a_directory_but_refuses_to_write_one(tmp_path):
    """As ``open(2)``: a read-only open of a directory succeeds so the caller's
    ``S_ISREG`` check sees it; a write open is ``EISDIR``."""
    (tmp_path / "dir").mkdir()
    with _directory(tmp_path) as root:
        fd = win32_at.open_at(root, "dir", os.O_RDONLY | win32_at.AT_NOFOLLOW)
        try:
            assert stat.S_ISDIR(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        with pytest.raises(IsADirectoryError):
            win32_at.open_at(root, "dir", os.O_WRONLY | win32_at.AT_NOFOLLOW)


@WINDOWS
def test_open_at_directory_flag_refuses_a_file(tmp_path):
    (tmp_path / "file").write_bytes(b"x")
    with _directory(tmp_path) as root:
        with pytest.raises(NotADirectoryError):
            win32_at.open_at(root, "file", os.O_RDONLY | win32_at.AT_DIRECTORY)


@WINDOWS
def test_open_at_nofollow_refuses_a_junction_with_eloop(tmp_path):
    """The refusal the confined walk is built on: a junction planted at a
    component below the root fails the walk exactly as ``O_NOFOLLOW`` fails a
    symlink there — and nothing under the junction's target was touched."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "project").mkdir()
    _junction(tmp_path / "project" / "artifacts", outside)
    with _directory(tmp_path / "project") as project:
        with pytest.raises(OSError) as refused:
            win32_at.open_at(
                project, "artifacts", os.O_RDONLY | win32_at.AT_DIRECTORY | win32_at.AT_NOFOLLOW
            )
        assert refused.value.errno == errno.ELOOP
        # Without the flag the junction is followed, as `os.open` would follow it.
        fd = win32_at.open_at(project, "artifacts", os.O_RDONLY | win32_at.AT_DIRECTORY)
        try:
            assert os.path.samestat(os.fstat(fd), outside.stat())
        finally:
            os.close(fd)


@WINDOWS
def test_open_at_write_probe_refuses_a_read_only_file(tmp_path):
    """`_refuse_unwritable_target_at`'s contract: the operator's read-only
    attribute answers ``PermissionError`` from the handle-relative open too."""
    target = tmp_path / "spec.md"
    target.write_bytes(b"x")
    target.chmod(stat.S_IREAD)
    try:
        with _directory(tmp_path) as root:
            with pytest.raises(PermissionError):
                win32_at.open_at(root, "spec.md", os.O_WRONLY | win32_at.AT_NOFOLLOW)
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)


# ----------------------------------------------------------------------- stat_at


@WINDOWS
def test_stat_at_describes_the_entry_itself(tmp_path):
    (tmp_path / "file").write_bytes(b"hello")
    (tmp_path / "dir").mkdir()
    with _directory(tmp_path) as root:
        file_stat = win32_at.stat_at(root, "file")
        assert stat.S_ISREG(file_stat.st_mode)
        assert file_stat.st_size == 5
        assert os.path.samestat(file_stat, (tmp_path / "file").stat())
        assert stat.S_ISDIR(win32_at.stat_at(root, "dir").st_mode)
        with pytest.raises(FileNotFoundError):
            win32_at.stat_at(root, "absent")


@WINDOWS
def test_stat_at_reports_a_junction_as_a_link_not_a_directory(tmp_path):
    """``lstat`` semantics: the recovery flow's ``S_ISREG``/``S_ISDIR`` checks must
    answer False for a planted redirect, which ``fstat`` of the opened reparse
    point would otherwise describe as the directory it stands in for."""
    real = tmp_path / "real"
    real.mkdir()
    _junction(tmp_path / "link", real)
    with _directory(tmp_path) as root:
        observed = win32_at.stat_at(root, "link")
    assert stat.S_ISLNK(observed.st_mode)
    assert not stat.S_ISDIR(observed.st_mode) and not stat.S_ISREG(observed.st_mode)
    assert observed.st_reparse_tag == os.lstat(tmp_path / "link").st_reparse_tag
    assert is_link_like(tmp_path / "link")


# -------------------------------------------------------------------- replace_at


@WINDOWS
def test_replace_at_publishes_the_staged_inode_over_the_target(tmp_path):
    """The anchored writer's publication step: the temp is renamed over the
    target while the writer's own handle stays open on it, and that handle now
    IS the published file — the inode the recovery flow verifies afterwards."""
    target = tmp_path / "spec.md"
    target.write_bytes(b"old")
    with _directory(tmp_path) as root:
        fd = win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, b"new")
            staged = os.fstat(fd)
            win32_at.replace_at(root, "spec.tmp", root, "spec.md")
            assert os.path.samestat(os.fstat(fd), staged)
            assert os.path.samestat(win32_at.stat_at(root, "spec.md"), staged)
        finally:
            os.close(fd)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spec.md"]
    assert target.read_bytes() == b"new"


@WINDOWS
def test_replace_at_moves_across_directories_relative_to_both_handles(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "a").write_bytes(b"a")
    (dst_dir / "b").write_bytes(b"b")
    with _directory(src_dir) as src, _directory(dst_dir) as dst:
        win32_at.replace_at(src, "a", dst, "b")
    assert list(src_dir.iterdir()) == []
    assert (dst_dir / "b").read_bytes() == b"a"


@WINDOWS
def test_replace_at_missing_source_is_file_not_found(tmp_path):
    with _directory(tmp_path) as root:
        with pytest.raises(FileNotFoundError):
            win32_at.replace_at(root, "absent", root, "spec.md")


@WINDOWS
def test_replace_at_replaces_a_target_another_share_delete_handle_holds_open(tmp_path):
    """POSIX rename semantics (``FileRenameInformationEx``): a reader holding the
    OLD target open keeps reading the old bytes, while the name already serves
    the new file — what ``rename(2)`` does, and what the fallback class cannot."""
    target = tmp_path / "spec.md"
    target.write_bytes(b"old")
    with _directory(tmp_path) as root:
        reader = win32_at.open_at(root, "spec.md", os.O_RDONLY)
        try:
            fd = win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
            os.write(fd, b"new")
            os.close(fd)
            win32_at.replace_at(root, "spec.tmp", root, "spec.md")
            assert os.read(reader, 10) == b"old"
        finally:
            os.close(reader)
    assert target.read_bytes() == b"new"


@WINDOWS
def test_replace_at_is_a_sharing_violation_while_the_target_is_open_without_share_delete(
    tmp_path,
):
    """The retry `platform_util.replace_at` wraps this in exists for exactly this:
    a handle opened the ordinary CRT way (no ``FILE_SHARE_DELETE``) blocks the
    replace until it closes, as it blocks ``os.replace`` — and with the same
    WinError 5/32 pair `_retry_on_sharing_violation` treats as transient."""
    target = tmp_path / "spec.md"
    target.write_bytes(b"old")
    (tmp_path / "spec.tmp").write_bytes(b"new")
    with _directory(tmp_path) as root:
        with target.open("rb"):
            with pytest.raises(PermissionError) as refused:
                win32_at.replace_at(root, "spec.tmp", root, "spec.md")
        assert refused.value.winerror in (5, 32)  # ACCESS_DENIED / SHARING_VIOLATION
        win32_at.replace_at(root, "spec.tmp", root, "spec.md")  # ...and clears with it
    assert target.read_bytes() == b"new"


# --------------------------------------------------------------------- unlink_at


@WINDOWS
def test_unlink_at_removes_a_file_and_refuses_a_directory(tmp_path):
    (tmp_path / "file").write_bytes(b"x")
    (tmp_path / "dir").mkdir()
    with _directory(tmp_path) as root:
        win32_at.unlink_at(root, "file")
        with pytest.raises(IsADirectoryError):
            win32_at.unlink_at(root, "dir")
        with pytest.raises(FileNotFoundError):
            win32_at.unlink_at(root, "file")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dir"]


@WINDOWS
def test_unlink_at_removes_the_name_while_the_writers_own_handle_is_open(tmp_path):
    """The anchored writer's failure cleanup unlinks its temp while its own
    handle is still open; with POSIX delete semantics the NAME is gone at once
    (a fresh ``O_EXCL`` create of it succeeds) and the handle stays readable."""
    with _directory(tmp_path) as root:
        fd = win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, b"staged")
            win32_at.unlink_at(root, "spec.tmp")
            assert not (tmp_path / "spec.tmp").exists()
            again = win32_at.open_at(root, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
            os.close(again)
            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, 10) == b"staged"
        finally:
            os.close(fd)


# ----------------------------------------------- the anchor under a moved parent


@WINDOWS
def test_a_directory_with_a_handle_open_beneath_it_cannot_be_renamed(tmp_path):
    """The Windows half of the parent-swap race the POSIX rows stage in
    tests/test_recovery_flow.py (`posix_parent_swap_under_writer`): while the
    anchored writer holds its temp open, the OS refuses to rename any directory
    above it, so the swap cannot happen at all — the platform closes the race
    before the descriptor has to. If this row ever reddens, those rows can run
    here too."""
    parent = tmp_path / "artifacts"
    parent.mkdir()
    with _directory(parent) as anchor:
        fd = win32_at.open_at(anchor, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        try:
            with pytest.raises(PermissionError):
                parent.rename(tmp_path / "moved")
        finally:
            os.close(fd)
    assert parent.is_dir() and not (tmp_path / "moved").exists()


@WINDOWS
def test_handle_relative_operations_stay_with_a_renamed_directory(tmp_path):
    """A directory whose only open handle is the anchor itself CAN be renamed
    (the handle shares DELETE). Everything relative to that handle then lands in
    the directory wherever it now lives — the property that makes the anchor an
    anchor — and a junction planted at the old name is never consulted."""
    parent = tmp_path / "artifacts"
    parent.mkdir()
    (parent / "spec.md").write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved"
    with _directory(parent) as anchor:
        parent.rename(moved)
        _junction(parent, outside)
        assert os.path.samestat(win32_at.stat_at(anchor, "spec.md"), (moved / "spec.md").stat())
        fd = win32_at.open_at(anchor, "spec.tmp", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, b"new")
            win32_at.replace_at(anchor, "spec.tmp", anchor, "spec.md")
        finally:
            os.close(fd)
    assert (moved / "spec.md").read_bytes() == b"new"
    assert list(outside.iterdir()) == []


# --------------------------------------------------------------- off-Windows arm


@pytest.mark.skipif(sys.platform == "win32", reason="the stub arm")
def test_off_windows_every_call_is_enosys():
    """Importable everywhere, usable nowhere but Windows: the POSIX callers go
    through ``os`` with ``dir_fd`` and never reach these."""
    assert not win32_at.AVAILABLE
    calls = [
        lambda: win32_at.open_directory(Path(".")),
        lambda: win32_at.open_at(0, "x", os.O_RDONLY),
        lambda: win32_at.stat_at(0, "x"),
        lambda: win32_at.replace_at(0, "x", 0, "y"),
        lambda: win32_at.unlink_at(0, "x"),
    ]
    for call in calls:
        with pytest.raises(OSError) as refused:
            call()
        assert refused.value.errno == errno.ENOSYS
    # The flag vocabulary IS the os.O_* one on POSIX, so a caller composing
    # `os.O_RDONLY | AT_NOFOLLOW` hands `os.open(dir_fd=...)` the real bit.
    assert win32_at.AT_NOFOLLOW == os.O_NOFOLLOW
    assert win32_at.AT_DIRECTORY == os.O_DIRECTORY
