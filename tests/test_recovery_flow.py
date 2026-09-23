"""Unit tests for the RecoveryFlow collaborator (issue #244 PR 2/2).

RecoveryFlow was carved out of Engine's rollback/preserve cluster. These
exercise it in isolation — built from narrow deps + stub engine callbacks, no
Engine instance — which is the point of the extraction. End-to-end behavior
under a real Engine stays covered by test_engine.py.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
from conftest import NUL_PATH_RESOLVE_FAULTS, git, refuse_to_resolve

from bmad_loop import platform_util, recovery_flow, verify
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.gates import ATTENTION_FILE
from bmad_loop.model import Phase, StoryTask
from bmad_loop.platform_util import UnconfinedWriteError, is_absolute_path
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy
from bmad_loop.recovery_flow import (
    PRESERVE_REF_PROBE_LIMIT,
    RecoveryFlow,
    _OwnedSpecAuthorityError,
)
from bmad_loop.verify import GitError, rev_parse_head
from bmad_loop.workspace import Workspace

QUIET = NotifyPolicy(desktop=False, file=True)
requires_descriptor_restoration = pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES,
    reason="automatic restore requires handle-anchored writes",
)
# Rows that rename a directory OUT FROM UNDER a writer holding a handle beneath it.
# Windows refuses that rename outright (ERROR_ACCESS_DENIED while any handle is
# open below the directory), so the swap these rows stage cannot be performed
# there — the OS closes the race before the anchored writer has to. The refusal
# itself is pinned on the win32 arm by tests/test_win32_at.py.
posix_parent_swap_under_writer = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses to rename a directory with a handle open beneath it",
)


def _plant_directory_redirect(link: Path, target: Path) -> None:
    """Plant a directory redirect at ``link`` the confined walk must refuse.

    A symlink on POSIX; on Windows a JUNCTION, which needs no elevation where a
    directory symlink needs SeCreateSymbolicLinkPrivilege — and is the redirect
    an unprivileged session can actually plant, so it is the one worth pinning
    against the ``win32_at`` walk."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _policy(**scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(**scm),
        limits=LimitsPolicy(),
    )


class _RecordingJournal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def append(self, event: str, **fields) -> None:
        self.entries.append((event, fields))

    def events(self) -> list[str]:
        return [e for e, _ in self.entries]

    def fields(self, event: str) -> dict:
        for e, f in self.entries:
            if e == event:
                return f
        raise KeyError(event)


class _Pause(Exception):
    """Stand-in for the engine's RunPaused, raised by the injected escalation_pause
    (and the escalate callback) so these tests need not import the engine."""

    def __init__(self, reason: str, story_key: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.story_key = story_key


def _fake_workspace(root: Path, *, output=None, impl=None, plan=None):
    """A duck-typed Workspace for the pure `protected_relpaths` predicate — root +
    the three artifact folders, no git required."""
    root = Path(root)
    paths = SimpleNamespace(
        output_folder=output if output is not None else root / "_bmad-output",
        implementation_artifacts=(
            impl if impl is not None else root / "_bmad-output" / "implementation-artifacts"
        ),
        planning_artifacts=(
            plan if plan is not None else root / "_bmad-output" / "planning-artifacts"
        ),
    )
    return SimpleNamespace(root=root, paths=paths)


@requires_descriptor_restoration
def test_owned_spec_restore_recreates_missing_canonical_parents(tmp_path):
    spec = tmp_path.resolve() / "new" / "deep" / "owned.md"
    snapshot = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"

    RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert spec.read_bytes() == snapshot


def test_owned_spec_restore_forced_fallback_refuses_before_path_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    spec = tmp_path.resolve() / "owned.md"
    original = b"operator bytes\n"
    spec.write_bytes(original)
    snapshot = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    path_writer_calls: list[Path] = []

    def path_writer_is_forbidden(path, *_args, **_kwargs):
        path_writer_calls.append(path)
        raise AssertionError("generic confined writer was called")

    monkeypatch.setattr(platform_util, "_atomic_write_confined", path_writer_is_forbidden)

    with pytest.raises(_OwnedSpecAuthorityError, match="restoration is unavailable") as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert excinfo.value.safe_restoration_unavailable is True
    assert path_writer_calls == []
    assert spec.read_bytes() == original


def test_owned_spec_restore_forced_fallback_does_not_create_missing_parents(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    first_missing_parent = tmp_path.resolve() / "new"
    spec = first_missing_parent / "deep" / "owned.md"

    def path_writer_is_forbidden(*_args, **_kwargs):
        raise AssertionError("generic confined writer was called")

    monkeypatch.setattr(platform_util, "_atomic_write_confined", path_writer_is_forbidden)

    with pytest.raises(_OwnedSpecAuthorityError, match="restoration is unavailable"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert not first_missing_parent.exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_owned_spec_normalization_forced_fallback_refuses_before_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    spec = tmp_path.resolve() / "owned.md"
    original = b"---\nstatus: in-progress\n---\n\noperator bytes\n"
    spec.write_bytes(original)
    writer_calls: list[tuple] = []
    monkeypatch.setattr(
        verify,
        "set_frontmatter_status",
        lambda *args, **kwargs: writer_calls.append((args, kwargs)),
    )

    with pytest.raises(_OwnedSpecAuthorityError, match="restoration is unavailable") as excinfo:
        RecoveryFlow._normalize_attempt_owned_spec(
            spec,
            "ready-for-dev",
            confine_root=tmp_path,
        )

    assert excinfo.value.safe_restoration_unavailable is True
    assert writer_calls == []
    assert spec.read_bytes() == original


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows coverage")
def test_owned_spec_restore_native_windows_anchors_at_a_handle(tmp_path, monkeypatch):
    """Windows restores through the handle-relative arm, not the path writer.

    DW-309/310 first made this host fail closed because CPython offers no
    `dir_fd` there; `platform_util.win32_at` now supplies the same anchor through
    NT handle-relative opens, so the row that pinned the refusal pins the
    restoration instead — and that the generic confined PATH writer is still
    never reached, which is what the refusal existed to guarantee."""
    assert not platform_util.DIR_FD_ANCHORED_WRITES
    assert recovery_flow.HANDLE_ANCHORED_WRITES
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes\n")
    snapshot = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    path_writer_calls: list[Path] = []

    def path_writer_is_forbidden(path, *_args, **_kwargs):
        path_writer_calls.append(path)
        raise AssertionError("generic confined writer was called")

    monkeypatch.setattr(platform_util, "_atomic_write_confined", path_writer_is_forbidden)
    real_write = platform_util.atomic_write_bytes_at
    anchored: list[str] = []

    def spy(dir_fd, name, data, **kwargs):
        anchored.append(name)
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", spy)

    RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert anchored == ["owned.md"]
    assert path_writer_calls == []
    assert spec.read_bytes() == snapshot
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
def test_owned_spec_restore_translates_value_error_family_before_write(
    tmp_path, monkeypatch, resolve_fault
):
    spec = tmp_path.resolve() / "owned.md"
    original = b"operator bytes\n"
    spec.write_bytes(original)
    refuse_to_resolve(monkeypatch, spec.parent, error=resolve_fault)

    with pytest.raises(_OwnedSpecAuthorityError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args
    assert spec.read_bytes() == original


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
def test_owned_spec_restore_translates_value_error_family_from_target_revalidation(
    tmp_path, monkeypatch, resolve_fault
):
    spec = tmp_path.resolve() / "owned.md"
    original = b"operator bytes\n"
    spec.write_bytes(original)
    refuse_to_resolve(monkeypatch, spec, error=resolve_fault)

    with pytest.raises(_OwnedSpecAuthorityError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args
    assert spec.read_bytes() == original


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
def test_owned_spec_restore_validates_full_missing_parent_before_creation(
    tmp_path, monkeypatch, resolve_fault
):
    parent = tmp_path.resolve() / "new" / "deep"
    spec = parent / "owned.md"
    refuse_to_resolve(monkeypatch, parent, error=resolve_fault)

    with pytest.raises(_OwnedSpecAuthorityError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args
    assert not parent.exists()


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
def test_owned_spec_restore_validates_missing_target_spelling_before_write(
    tmp_path, monkeypatch, resolve_fault
):
    spec = tmp_path.resolve() / "missing.md"
    writes: list[Path] = []
    refuse_to_resolve(monkeypatch, spec, error=resolve_fault)
    monkeypatch.setattr(
        recovery_flow,
        "atomic_write_bytes_confined",
        lambda path, *_args, **_kwargs: writes.append(path),
        raising=False,
    )
    monkeypatch.setattr(
        recovery_flow,
        "atomic_write_bytes_at",
        lambda _fd, path, *_args, **_kwargs: writes.append(Path(path)),
    )

    with pytest.raises(_OwnedSpecAuthorityError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args
    assert writes == []
    assert not spec.exists()


@pytest.mark.parametrize("failure", NUL_PATH_RESOLVE_FAULTS)
@requires_descriptor_restoration
def test_owned_spec_restore_does_not_translate_parent_mkdir_value_error(
    tmp_path, monkeypatch, failure
):
    parent = tmp_path.resolve() / "new"
    spec = parent / "owned.md"
    real_mkdir = Path.mkdir

    def fail_mkdir(path, *args, **kwargs):
        if path == parent:
            raise failure
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(type(failure)) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert excinfo.value is failure
    assert not parent.exists()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError("atomic repair write failed"), id="oserror"),
        pytest.param(RuntimeError("atomic repair write failed"), id="runtimeerror"),
        *NUL_PATH_RESOLVE_FAULTS,
    ],
)
@requires_descriptor_restoration
def test_owned_spec_restore_does_not_translate_atomic_repair_write_failure(
    tmp_path, monkeypatch, failure
):
    spec = tmp_path.resolve() / "owned.md"
    original = b"operator bytes\n"
    spec.write_bytes(original)

    def fail_write(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", fail_write)

    with pytest.raises(type(failure)) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert excinfo.value is failure
    assert spec.read_bytes() == original


@pytest.mark.parametrize("failure", NUL_PATH_RESOLVE_FAULTS)
@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_does_not_translate_content_read_value_error(
    tmp_path, monkeypatch, failure
):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes\n")

    def fail_readback(fd, size):
        raise failure

    monkeypatch.setattr(recovery_flow.os, "read", fail_readback)

    with pytest.raises(type(failure)) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes\n")

    assert excinfo.value is failure
    assert spec.read_bytes() == b"operator bytes\n"


@requires_descriptor_restoration
def test_owned_spec_restore_preserves_byte_hostile_snapshot(tmp_path):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"old")
    snapshot = b"---\r\nstatus: caf\xe9\r\n---\r\n\x00tail"

    RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert spec.read_bytes() == snapshot


@posix_parent_swap_under_writer
@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
@pytest.mark.parametrize("victim_matches", [False, True], ids=["different-victim", "equal-victim"])
def test_owned_spec_restore_parent_swap_before_publication_stays_anchored(
    tmp_path, monkeypatch, victim_matches
):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes\n"
    moved = tmp_path / "moved-artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / spec.name
    victim_before = snapshot if victim_matches else b"external victim"
    victim.write_bytes(victim_before)
    real_write = recovery_flow.atomic_write_bytes_at

    def swap_before_replace(dir_fd, name, data, **kwargs):
        def swap() -> None:
            parent.rename(moved)
            _plant_directory_redirect(parent, outside)

        kwargs["_before_replace"] = swap
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", swap_before_replace)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert victim.read_bytes() == victim_before
    assert (moved / spec.name).read_bytes() == snapshot
    assert platform_util.is_link_like(parent)


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_parent_swap_before_filesystem_root_walk(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    original = b"operator bytes"
    spec.write_bytes(original)
    moved = tmp_path / "moved-artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / spec.name
    victim.write_bytes(b"external victim")
    real_open = recovery_flow.open_dir_confined
    swapped = False

    def swap_before_walk(root: Path, target: Path, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(moved)
            _plant_directory_redirect(parent, outside)
        return real_open(root, target, **kwargs)

    monkeypatch.setattr(recovery_flow, "open_dir_confined", swap_before_walk)

    with pytest.raises(_OwnedSpecAuthorityError):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes")

    assert victim.read_bytes() == b"external victim"
    assert (moved / spec.name).read_bytes() == original


@posix_parent_swap_under_writer
@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_ancestor_swap_before_publication_stays_anchored(tmp_path, monkeypatch):
    ancestor = tmp_path.resolve() / "artifacts"
    parent = ancestor / "nested"
    parent.mkdir(parents=True)
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    moved = tmp_path / "moved-artifacts"
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    victim = outside / "nested" / spec.name
    victim.write_bytes(b"external victim")
    real_write = recovery_flow.atomic_write_bytes_at

    def swap_before_replace(dir_fd, name, data, **kwargs):
        def swap() -> None:
            ancestor.rename(moved)
            _plant_directory_redirect(ancestor, outside)

        kwargs["_before_replace"] = swap
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", swap_before_replace)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes")

    assert victim.read_bytes() == b"external victim"
    assert (moved / "nested" / spec.name).read_bytes() == b"snapshot bytes"


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_detects_parent_swap_after_first_callback_check(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    moved = tmp_path / "moved-artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / spec.name
    victim.write_bytes(b"external victim")
    real_open = recovery_flow.open_dir_confined
    probes = 0

    def swap_after_first_probe(root: Path, target: Path, **kwargs):
        nonlocal probes
        fd = real_open(root, target, **kwargs)
        probes += 1
        if probes == 1:
            parent.rename(moved)
            _plant_directory_redirect(parent, outside)
        return fd

    monkeypatch.setattr(recovery_flow, "open_dir_confined", swap_after_first_probe)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes")

    assert probes == 2
    assert victim.read_bytes() == b"external victim"
    assert (moved / spec.name).read_bytes() == b"snapshot bytes"


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
@pytest.mark.parametrize(
    "replacement", ["missing", "symlink", "junction", "fifo", "socket", "directory"]
)
def test_owned_spec_restore_types_final_entry_substitution_as_authority_loss(
    tmp_path, monkeypatch, replacement
):
    """Every shape the name can take after publication is authority loss, on
    both anchored arms. ``fifo``/``socket`` are POSIX entry types; ``symlink`` at
    a FILE name needs elevation on Windows, where ``junction`` is the redirect an
    unprivileged writer plants instead (a directory reparse point the win32 arm
    must refuse as it refuses a link) and has no POSIX counterpart."""
    if replacement in {"fifo", "socket"} and sys.platform == "win32":
        pytest.skip(f"{replacement} is a POSIX entry type")
    if replacement == "symlink" and sys.platform == "win32":
        pytest.skip("file symlink creation may need elevation")
    if replacement == "junction" and sys.platform != "win32":
        pytest.skip("junctions are a Windows reparse point")
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"external victim")
    published: list[bytes] = []
    listeners: list[socket.socket] = []
    real_write = recovery_flow.atomic_write_bytes_at

    def mutate_after_publish(dir_fd, name, data, **kwargs):
        verifier = kwargs["_after_replace"]

        def replace_then_verify(published_fd):
            published.append(spec.read_bytes())
            spec.unlink()
            if replacement == "symlink":
                spec.symlink_to(victim)
            elif replacement == "junction":
                _plant_directory_redirect(spec, outside)
            elif replacement == "fifo":
                os.mkfifo(spec)
            elif replacement == "socket":
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(str(spec))
                listeners.append(listener)
            elif replacement == "directory":
                spec.mkdir()
            verifier(published_fd)

        kwargs["_after_replace"] = replace_then_verify
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", mutate_after_publish)

    try:
        with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
            RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)
    finally:
        for listener in listeners:
            listener.close()

    assert published == [snapshot]
    assert victim.read_bytes() == b"external victim"
    if replacement == "symlink":
        assert spec.is_symlink()
    elif replacement == "junction":
        assert platform_util.is_link_like(spec)
    elif replacement == "fifo":
        assert stat.S_ISFIFO(spec.lstat().st_mode)
    elif replacement == "socket":
        assert stat.S_ISSOCK(spec.lstat().st_mode)
    elif replacement == "directory":
        assert spec.is_dir()
    else:
        assert not spec.exists()


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_anchored_snapshot_plus_suffix_is_a_genuine_mismatch(
    tmp_path, monkeypatch
):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    real_write = recovery_flow.atomic_write_bytes_at
    real_read = os.read
    requested: list[int] = []

    def write_suffix(dir_fd, name, data, **kwargs):
        return real_write(dir_fd, name, data + b"x", **kwargs)

    def bounded_read(fd: int, size: int) -> bytes:
        requested.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", write_suffix)
    monkeypatch.setattr(recovery_flow.os, "read", bounded_read)

    with pytest.raises(verify.FrontmatterWriteError):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert spec.read_bytes() == snapshot + b"x"
    assert requested[-1:] == [len(snapshot) + 1]


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_preserves_raw_anchored_os_read_failure(tmp_path, monkeypatch):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes")
    failure = OSError("ordinary read failed")

    def fail_read(_fd, _size):
        raise failure

    monkeypatch.setattr(recovery_flow.os, "read", fail_read)

    with pytest.raises(OSError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes")

    assert excinfo.value is failure
    assert spec.read_bytes() == b"operator bytes"


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_preserves_raw_postpublication_os_read_failure(tmp_path, monkeypatch):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    failure = OSError("ordinary postpublication read failed")
    real_read = os.read
    real_write = recovery_flow.atomic_write_bytes_at
    published = False

    def mark_published(dir_fd, name, data, **kwargs):
        verify_after = kwargs["_after_replace"]

        def mark_then_verify(published_fd: int) -> None:
            nonlocal published
            published = True
            verify_after(published_fd)

        kwargs["_after_replace"] = mark_then_verify
        return real_write(dir_fd, name, data, **kwargs)

    def fail_after_publish(fd: int, size: int) -> bytes:
        if published:
            raise failure
        return real_read(fd, size)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", mark_published)
    monkeypatch.setattr(recovery_flow.os, "read", fail_after_publish)

    with pytest.raises(OSError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert excinfo.value is failure
    assert spec.read_bytes() == snapshot


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_anchored_uses_filesystem_anchor_for_external_target(
    tmp_path, monkeypatch
):
    external = tmp_path.resolve() / "trusted-external-artifacts"
    external.mkdir()
    spec = external / "owned.md"
    snapshot = b"snapshot bytes"
    seen_roots: list[Path] = []
    real_open = recovery_flow.open_dir_confined

    def record_root(root, target, **kwargs):
        seen_roots.append(root)
        return real_open(root, target, **kwargs)

    monkeypatch.setattr(recovery_flow, "open_dir_confined", record_root)

    RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert seen_roots and set(seen_roots) == {Path(spec.anchor)}
    assert spec.read_bytes() == snapshot


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_preserves_anchored_writable_target_refusal(tmp_path, monkeypatch):
    spec = tmp_path.resolve() / "owned.md"
    original = b"operator bytes"
    spec.write_bytes(original)
    failure = PermissionError("target is read-only")

    def refuse(_dir_fd, _name):
        raise failure

    monkeypatch.setattr(platform_util, "_refuse_unwritable_target_at", refuse)

    with pytest.raises(PermissionError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot bytes")

    assert excinfo.value is failure
    assert spec.read_bytes() == original


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_anchored_refuses_missing_target_appearing_before_staging(
    tmp_path, monkeypatch
):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    appeared = b"new owner bytes"
    real_write = recovery_flow.atomic_write_bytes_at
    later_calls: list[str] = []

    def appear_before_writer(dir_fd, name, data, **kwargs):
        spec.write_bytes(appeared)
        return real_write(dir_fd, name, data, **kwargs)

    def unexpected_probe(*_args, **_kwargs):
        later_calls.append("writable-probe")
        raise AssertionError("writable probe ran after authority loss")

    def unexpected_stage(*_args, **_kwargs):
        later_calls.append("temp")
        raise AssertionError("temp creation ran after authority loss")

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", appear_before_writer)
    monkeypatch.setattr(platform_util, "_refuse_unwritable_target_at", unexpected_probe)
    monkeypatch.setattr(platform_util, "_open_exclusive_at", unexpected_stage)

    # This simultaneously pins recovery's initially-missing target authority
    # and `_before_staging` ordering ahead of the probe and temp creation.
    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert later_calls == []
    assert spec.read_bytes() == appeared


def _replace_target(spec: Path, data: bytes) -> None:
    """Swap the entry at `spec` for a NEW file holding `data`, distinguishable
    from the original by the identity the restore guard compares (`samestat`).

    Not `unlink()` then `write_bytes()`: the guard's pre-replace predicate is
    st_dev/st_ino, and ext4 hands a just-freed inode number straight back to the
    next creation in the same directory, so on the CI runners that sequence
    produced a "replacement" the guard could not tell from the original (it
    differed on the dev box only because that filesystem allocates differently). The
    replacement is created while the original still exists — two live entries
    cannot share an inode — and then renamed over it, which is also the only
    portable spelling: Windows refuses to unlink a file this process holds
    open, so "hold the original open across the swap" is not an option there.
    """
    replacement = spec.with_name(spec.name + ".replacement")
    replacement.write_bytes(data)
    os.replace(replacement, spec)


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_target_replacement_after_staging(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    real_write = recovery_flow.atomic_write_bytes_at

    def replace_before_publish(dir_fd, name, data, **kwargs):
        validate = kwargs["_before_replace"]

        def replace_then_validate() -> None:
            _replace_target(spec, b"replacement")
            validate()

        kwargs["_before_replace"] = replace_then_validate
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", replace_before_publish)

    # Ablation: removing recovery's pre-replace predicate publishes over the
    # replacement and this authority-loss assertion fails.
    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert spec.read_bytes() == b"replacement"
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_in_place_target_edit_after_staging(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    original_inode = spec.stat().st_ino
    competing = b"competing data"
    real_write = recovery_flow.atomic_write_bytes_at
    monkeypatch.setattr(recovery_flow, "_target_stat_version", lambda _observed: (0, 0, 0))

    def edit_before_publish(dir_fd, name, data, **kwargs):
        validate = kwargs["_before_replace"]

        def edit_then_validate() -> None:
            with spec.open("r+b") as fh:
                fh.write(competing)
                fh.truncate()
            assert spec.stat().st_ino == original_inode
            validate()

        kwargs["_before_replace"] = edit_then_validate
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", edit_before_publish)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert spec.read_bytes() == competing
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_in_place_edit_during_initial_content_read(
    tmp_path, monkeypatch
):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    original = b"operator bytes"
    competing = original + b"x"
    spec.write_bytes(original)
    real_read = os.read
    mutated = False

    def mutate_then_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            with spec.open("ab") as fh:
                fh.write(b"x")
        return real_read(fd, size)

    monkeypatch.setattr(recovery_flow.os, "read", mutate_then_read)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert mutated is True
    assert spec.read_bytes() == competing
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_short_initial_content_sample(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    original = b"operator bytes"
    spec.write_bytes(original)
    monkeypatch.setattr(recovery_flow.os, "read", lambda _fd, _size: b"")

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert spec.read_bytes() == original
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_compares_bounded_multichunk_content_after_staging(
    tmp_path, monkeypatch
):
    chunk_size = 1024 * 1024
    prefix = b"a" * chunk_size
    original = prefix + b"operator bytes"
    competing = prefix + b"competing data"
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(original)
    original_inode = spec.stat().st_ino
    real_read = os.read
    real_write = recovery_flow.atomic_write_bytes_at
    requests: list[int] = []
    monkeypatch.setattr(recovery_flow, "_target_stat_version", lambda _observed: (0, 0, 0))

    def bounded_read(fd: int, size: int) -> bytes:
        requests.append(size)
        return real_read(fd, size)

    def edit_suffix_before_publish(dir_fd, name, data, **kwargs):
        validate = kwargs["_before_replace"]

        def edit_then_validate() -> None:
            with spec.open("r+b") as fh:
                fh.seek(chunk_size)
                fh.write(b"competing data")
                fh.truncate()
            assert spec.stat().st_ino == original_inode
            validate()

        kwargs["_before_replace"] = edit_then_validate
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow.os, "read", bounded_read)
    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", edit_suffix_before_publish)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert requests == [chunk_size, 15, 1] * 3
    assert max(requests) == chunk_size
    assert spec.read_bytes() == competing
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_refuses_name_replacement_during_prepublication_read(
    tmp_path, monkeypatch
):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    original = b"operator bytes"
    competing = b"replacement"
    spec.write_bytes(original)
    real_write = recovery_flow.atomic_write_bytes_at
    real_read = os.read

    def replace_during_validation(dir_fd, name, data, **kwargs):
        validate = kwargs["_before_replace"]

        def replace_then_validate() -> None:
            replaced = False

            def replace_then_read(fd: int, size: int) -> bytes:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    spec.unlink()
                    spec.write_bytes(competing)
                return real_read(fd, size)

            monkeypatch.setattr(recovery_flow.os, "read", replace_then_read)
            validate()

        kwargs["_before_replace"] = replace_then_validate
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", replace_during_validation)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert spec.read_bytes() == competing
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_preserves_raw_content_comparison_failure(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    original = b"operator bytes"
    spec.write_bytes(original)
    failure = OSError("ordinary comparison read failed")
    real_write = recovery_flow.atomic_write_bytes_at

    def fail_before_publish(dir_fd, name, data, **kwargs):
        validate = kwargs["_before_replace"]

        def fail_then_validate() -> None:
            def fail_read(_fd: int, _size: int) -> bytes:
                raise failure

            monkeypatch.setattr(recovery_flow.os, "read", fail_read)
            validate()

        kwargs["_before_replace"] = fail_then_validate
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", fail_before_publish)

    with pytest.raises(OSError) as excinfo:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert excinfo.value is failure
    assert spec.read_bytes() == original
    assert list(parent.glob("*.tmp")) == []


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_rejects_equal_byte_final_entry_replacement(tmp_path, monkeypatch):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    real_write = recovery_flow.atomic_write_bytes_at

    def replace_after_publish(dir_fd, name, data, **kwargs):
        verify_after = kwargs["_after_replace"]

        def replace_then_verify(published_fd: int) -> None:
            spec.unlink()
            spec.write_bytes(snapshot)
            verify_after(published_fd)

        kwargs["_after_replace"] = replace_then_verify
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", replace_after_publish)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert spec.read_bytes() == snapshot


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_rejects_live_name_replacement_during_readback(tmp_path, monkeypatch):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    real_read = os.read
    real_write = recovery_flow.atomic_write_bytes_at
    replaced = False
    published = False

    def mark_published(dir_fd, name, data, **kwargs):
        verify_after = kwargs["_after_replace"]

        def mark_then_verify(published_fd: int) -> None:
            nonlocal published
            published = True
            verify_after(published_fd)

        kwargs["_after_replace"] = mark_then_verify
        return real_write(dir_fd, name, data, **kwargs)

    def replace_name_then_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and published:
            replaced = True
            spec.unlink()
            spec.write_bytes(snapshot)
        return real_read(fd, size)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", mark_published)
    monkeypatch.setattr(recovery_flow.os, "read", replace_name_then_read)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert replaced is True
    assert spec.read_bytes() == snapshot


@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_rejects_in_place_mutation_during_readback(tmp_path, monkeypatch):
    spec = tmp_path.resolve() / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    real_read = os.read
    mutated = False
    published = False
    real_write = recovery_flow.atomic_write_bytes_at

    def mark_published(dir_fd, name, data, **kwargs):
        verify_after = kwargs["_after_replace"]

        def mark_then_verify(published_fd: int) -> None:
            nonlocal published
            published = True
            verify_after(published_fd)

        kwargs["_after_replace"] = mark_then_verify
        return real_write(dir_fd, name, data, **kwargs)

    def mutate_then_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated and published:
            mutated = True
            with spec.open("ab") as fh:
                fh.write(b"x")
        return real_read(fd, size)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", mark_published)
    monkeypatch.setattr(recovery_flow.os, "read", mutate_then_read)

    # Ablation: removing the before/after metadata comparison turns this into a
    # stable-mismatch classification instead of authority loss.
    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    assert mutated is True
    assert spec.read_bytes() == snapshot + b"x"


@posix_parent_swap_under_writer
@pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
def test_owned_spec_restore_real_parent_replacement_stays_in_retained_directory(
    tmp_path, monkeypatch
):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    moved = tmp_path / "moved-artifacts"
    real_write = recovery_flow.atomic_write_bytes_at

    def replace_parent_before_publish(dir_fd, name, data, **kwargs):
        def replace_parent() -> None:
            parent.rename(moved)
            parent.mkdir()

        kwargs["_before_replace"] = replace_parent
        return real_write(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(recovery_flow, "atomic_write_bytes_at", replace_parent_before_publish)

    with pytest.raises(_OwnedSpecAuthorityError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, b"snapshot")

    assert (moved / spec.name).read_bytes() == b"snapshot"
    assert not (parent / spec.name).exists()


@pytest.mark.skipif(
    not platform_util.DIR_FD_ANCHORED_WRITES
    or not (getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0)),
    reason="host has no search-only directory-open flag",
)
def test_owned_spec_restore_supports_search_only_parent(tmp_path):
    parent = tmp_path.resolve() / "artifacts"
    parent.mkdir()
    spec = parent / "owned.md"
    spec.write_bytes(b"operator bytes")
    snapshot = b"snapshot bytes"
    parent.chmod(0o300)
    try:
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)
    finally:
        parent.chmod(0o700)

    assert spec.read_bytes() == snapshot


def test_attempt_owned_spec_refuses_a_posix_absolute_spec_path(tmp_path, monkeypatch):
    """#480 item 4: the one genuine REFUSAL guard in the tree built on stdlib
    `is_absolute()`, pinned so a later path-guard sweep does not "fix" it.

    It fails CLOSED on Windows, and `platform_util.is_absolute_path` would open
    it: the Windows flavour reads a POSIX-absolute spec path as NOT absolute, so
    the guard raises, while the family predicate answers True and would let it
    through. That divergence is asserted on the pure flavour, so a POSIX host
    measures the claim rather than skipping it.

    Ablation: deleting the whole refusal reddens the raise and the canary
    together. Deleting the `not spec_path.is_absolute()` term ALONE leaves this
    GREEN on POSIX -- measured, not assumed -- because the `resolve(strict=True)`
    fixed-point term below subsumes it here: a relative path never equals its own
    resolve. That is the finding rather than a hole in the test. The term is
    load-bearing on Windows only, so no POSIX test can protect it from deletion
    and the comment at the guard is what has to."""
    # The divergence the proposed swap would introduce, on the flavour that
    # decides it -- this is the whole of #480 item 4's mechanism, inverted.
    assert PureWindowsPath("/attempt/owned.md").is_absolute() is False
    assert is_absolute_path("/attempt/owned.md") is True

    monkeypatch.chdir(tmp_path)
    snapshot = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec = Path("attempt") / "owned.md"

    with pytest.raises(RuntimeError, match="became unsafe"):
        RecoveryFlow._restore_attempt_owned_spec_bytes(spec, snapshot)

    # Canary: the guard sits ABOVE the mkdir and the write, so neither ran. A
    # refusal raised after the restore would pass the assertion above alone.
    assert not spec.exists()
    assert not spec.parent.exists()


def _make_flow(
    *,
    workspace,
    paths=None,
    policy: Policy | None = None,
    state=None,
    journal: _RecordingJournal | None = None,
    run_dir: Path | None = None,
    dev_attempt_dispatched: bool = True,
):
    """Build a RecoveryFlow wired to recording stubs. The returned flow carries a
    ``.calls`` namespace tallying the injected callbacks for assertions. ``paths``
    defaults to the workspace root (so the flow reads as "main checkout"); pass a
    different ``repo_root`` to simulate a mounted unit worktree."""
    calls = SimpleNamespace(saves=0, emits=[], pauses=[], escalates=[])

    def _save() -> None:
        calls.saves += 1

    def _emit(stage, task=None, **fields):
        calls.emits.append(stage)
        return None

    def _escalate(task, reason) -> None:
        calls.escalates.append((task.story_key, reason))
        raise _Pause(reason, task.story_key)

    def _pause(reason, story_key="", *, cause=None):
        calls.pauses.append((reason, story_key, cause))
        raise _Pause(reason, story_key)

    flow = RecoveryFlow(
        paths=paths if paths is not None else SimpleNamespace(repo_root=workspace.root),
        policy=policy if policy is not None else _policy(),
        state=state if state is not None else SimpleNamespace(run_id="run-1"),
        journal=journal if journal is not None else _RecordingJournal(),
        run_dir=run_dir if run_dir is not None else workspace.root,
        workspace_get=lambda: workspace,
        emit=_emit,
        save=_save,
        escalate=_escalate,
        escalation_pause=_pause,
        dev_attempt_dispatched=lambda task: dev_attempt_dispatched,
    )
    flow.calls = calls
    return flow


def _task(repo: Path, story_key: str = "1-1-a") -> StoryTask:
    task = StoryTask(story_key=story_key, epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    return task


def _tracked_spec(
    project: ProjectPaths,
    *,
    name: str = "spec-1-1-a.md",
    status: str = "ready-for-dev",
    body: str = "baseline intent\n",
) -> Path:
    """Create and commit one ordinary spec, returning the attempt baseline file."""
    spec = project.implementation_artifacts / name
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(f"---\nstatus: {status}\n---\n\n{body}")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "tracked spec baseline")
    return spec


def _status(spec: Path) -> str:
    return verify.status_of(verify.read_frontmatter(spec))


def _assert_owned_spec_manual_adoption_pause(
    flow: RecoveryFlow,
    task: StoryTask,
    spec: Path,
    *,
    stage: str,
    expected_status: str | None = None,
) -> None:
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    assert flow.calls.saves == 1
    assert len(flow.calls.pauses) == 1
    assert "manual adoption is required" in flow.calls.pauses[0][0]
    assert flow.journal.events().count("rollback-owned-spec-manual-required") == 1
    status_guidance = (
        f"; the adopted spec must have lifecycle status {expected_status!r}"
        if expected_status is not None
        else ""
    )
    assert flow.journal.fields("rollback-owned-spec-manual-required") == {
        "story_key": task.story_key,
        "spec": str(spec.resolve()),
        "problem": (
            f"safe automatic restoration is unavailable {stage} because "
            "this platform lacks handle-anchored writes"
            f"{status_guidance}; manual adoption is required"
        ),
    }


# --------------------------------------------------------------- protected paths


def test_protected_relpaths_lists_bmad_folders(tmp_path):
    flow = _make_flow(workspace=_fake_workspace(tmp_path))
    assert flow.protected_relpaths() == (
        "_bmad-output",
        "_bmad-output/implementation-artifacts",
        "_bmad-output/planning-artifacts",
    )


def test_protected_relpaths_skips_folders_outside_repo(tmp_path):
    # a planning folder configured outside the repo raises ValueError on
    # relative_to and is dropped — nothing to protect there.
    outside = tmp_path.parent / "elsewhere" / "planning"
    ws = _fake_workspace(tmp_path, plan=outside)
    flow = _make_flow(workspace=ws)
    assert flow.protected_relpaths() == (
        "_bmad-output",
        "_bmad-output/implementation-artifacts",
    )


def test_protected_relpaths_drops_repo_root_prefix(tmp_path):
    # A folder == repo root would relativize to "." and, used as a preserve
    # prefix, keep the whole tree through a reset — it must be dropped.
    ws = _fake_workspace(tmp_path, output=tmp_path)
    flow = _make_flow(workspace=ws)
    assert "." not in flow.protected_relpaths()
    assert "_bmad-output/implementation-artifacts" in flow.protected_relpaths()


# --------------------------------------------------------------- rollback_or_pause


def test_rollback_skips_clean_tree(project):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=False))
    task = _task(project.project)  # HEAD == baseline, no untracked

    flow.rollback_or_pause(task)  # must NOT raise

    assert "rollback-skipped-clean" in flow.journal.events()
    assert flow.calls.pauses == []
    assert flow.calls.emits == []  # no pre/post_rollback on the clean short-circuit


def test_rollback_auto_resets_when_flag_on(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("committed attempt\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")

    flow.rollback_or_pause(task)  # must NOT raise

    assert rev_parse_head(repo) == task.baseline_commit  # reset to baseline
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]
    assert flow.calls.pauses == []
    assert "rollback-auto" in flow.journal.events()


def test_rollback_off_pauses_and_leaves_tree(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=False))
    task = _task(repo)
    (repo / "dirty.txt").write_text("uncommitted work\n")  # untracked → dirty

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert (repo / "dirty.txt").exists()  # tree untouched
    assert flow.calls.pauses  # escalation_pause fired
    assert "rollback-manual-required" in flow.journal.events()


def test_rollback_in_unit_worktree_auto_recovers_even_when_off(project, tmp_path):
    # workspace.root != paths.repo_root ⇒ a mounted unit worktree: rollback OFF
    # still auto-recovers (the flag gates in-place recovery only, #161).
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(
        workspace=ws,
        paths=SimpleNamespace(repo_root=tmp_path / "some-other-main-checkout"),
        policy=_policy(rollback_on_failure=False),
    )
    task = _task(repo)
    (repo / "src.txt").write_text("worktree attempt\n")

    flow.rollback_or_pause(task)  # must NOT pause despite rollback OFF

    assert flow.calls.pauses == []
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]
    assert "rollback-auto" in flow.journal.events()


def test_rollback_resolved_cause_auto_recovers_when_off(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=False))
    task = _task(repo)
    (repo / "src.txt").write_text("re-drive edit\n")

    flow.rollback_or_pause(task, cause="resolved")  # human-initiated → never pauses

    assert flow.calls.pauses == []
    assert "rollback-auto" in flow.journal.events()


def test_rollback_dirty_check_git_fault_degrades_to_dirty(project, monkeypatch):
    # #156: an un-determinable dirty check assumes dirty — OFF then pauses rather
    # than skip-clean, and never crashes.
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=False))
    task = _task(repo)

    def boom(*a, **k):
        raise GitError("git diff timed out")

    monkeypatch.setattr(verify, "attempt_dirty", boom)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert "rollback-dirty-check-failed" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()


def test_rollback_dirty_check_oserror_degrades_to_dirty(project, monkeypatch):
    # #343: spawn faults now arrive typed as GitSpawnError, but the guard keeps a
    # plain-OSError net for any untyped fault out of the probe. It must degrade
    # exactly like the GitError above — this is the first git call on the rollback
    # path, so an unguarded fault here crashes before any preserve step can run.
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=False))
    task = _task(repo)

    def boom(*a, **k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(verify, "attempt_dirty", boom)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert "rollback-dirty-check-failed" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()


@requires_descriptor_restoration
def test_bound_lifecycle_only_spec_is_normalized_and_reads_git_clean(project):
    """T8: the one-file attempt binding recognizes only its own lifecycle delta.

    Ablation: replace `owned_exclude` with `()` in the first dirty probe and this
    test fails by taking the rollback-off manual-pause path.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert _status(spec) == "ready-for-dev"
    assert git(repo, "status", "--porcelain") == ""
    assert flow.journal.events() == ["rollback-skipped-clean"]
    assert flow.calls.pauses == []
    assert flow.calls.emits == []


@pytest.mark.parametrize("status", ["draft", "in-progress", "in-review"])
def test_bound_unchanged_resumable_spec_is_never_normalized(project, status, monkeypatch):
    """A bound Stories spec is not itself proof that the attempt changed it.

    Ablation: delete the first real-checkout clean return in ``rollback_or_pause``
    and this test reaches the forbidden normalization spy below.
    """
    repo = project.project
    spec = _tracked_spec(project, status=status)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    def normalization_is_forbidden(*_args, **_kwargs):
        raise AssertionError("an unchanged bound spec must not be normalized")

    monkeypatch.setattr(flow, "_normalize_attempt_owned_spec", normalization_is_forbidden)

    flow.rollback_or_pause(task)

    assert _status(spec) == status
    assert git(repo, "status", "--porcelain") == ""
    assert flow.journal.events() == ["rollback-skipped-clean"]
    assert flow.calls.pauses == []
    assert flow.calls.emits == []


@pytest.mark.parametrize(
    ("baseline_status", "attempt_status"),
    [("draft", "in-progress"), ("in-progress", "in-review"), ("in-review", "done")],
)
@requires_descriptor_restoration
def test_plain_bound_lifecycle_change_restores_baseline_status(
    project, baseline_status, attempt_status
):
    """A plain lifecycle-only attempt returns to its exact baseline route.

    Ablation: replace the baseline-status oracle with the hard-coded
    ``ready-for-dev`` target and the non-ready rows fail by pausing on the dirty
    rewritten spec instead of converging cleanly.
    """
    repo = project.project
    spec = _tracked_spec(project, status=baseline_status)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, attempt_status, confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert _status(spec) == baseline_status
    assert git(repo, "status", "--porcelain") == ""
    assert flow.journal.events() == ["rollback-skipped-clean"]
    assert flow.calls.pauses == []


@requires_descriptor_restoration
def test_plain_bound_lifecycle_commit_is_parked_and_reset_before_retry(project):
    """A baseline-shaped checkout is not clean while attempt commits remain.

    Ablation: remove the normalized-commit-only auto-recovery arm and this test
    fails by demanding manual recovery instead of parking the lifecycle commit
    and resetting it automatically.
    """
    repo = project.project
    spec = _tracked_spec(project, status="draft")
    task = _task(repo)
    baseline = task.baseline_commit
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt lifecycle flip")
    attempt_head = rev_parse_head(repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert rev_parse_head(repo) == baseline
    assert _status(spec) == "draft"
    assert git(repo, "status", "--porcelain") == ""
    assert task.preserve_ref is not None
    assert git(repo, "rev-parse", task.preserve_ref) == attempt_head
    assert "attempt-commits-preserved" in flow.journal.events()
    assert "rollback-auto" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]
    assert flow.calls.pauses == []


def test_plain_owned_spec_with_substantive_residue_still_pauses(project):
    """T9: status normalization does not authorize a plain attempt's body edit.

    Ablation: delete the byte-for-byte restoration after the normalized checkout
    remains dirty and this test fails because recovery changes the spec before
    handing the untouched-tree manual-recovery policy back to the operator.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    spec.write_text("---\nstatus: in-progress\n---\n\nhuman substantive correction\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "human substantive correction" in spec.read_text()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.calls.emits == []


def test_plain_owned_spec_forced_fallback_pauses_while_undoing_lifecycle_repair(
    project, monkeypatch
):
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    child = b"---\nstatus: in-progress\n---\n\nfailed child body\n"
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="attempt-owned lifecycle status"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert "rollback-manual-required" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="while restoring the attempt-owned lifecycle status",
        expected_status="ready-for-dev",
    )


def test_bound_spec_exclusion_does_not_hide_sibling_source_residue(project):
    """T10/source: source debris keeps the ordinary rollback policy reachable.

    INVERSE ablation: replace the exact-file exclusion with `.` and this test
    fails because the source is initially hidden and the owned status is rewritten.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    (repo / "src.txt").write_text("attempt source residue\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"  # no normalization while sibling debris exists
    assert "rollback-skipped-clean" not in flow.journal.events()


def test_bound_spec_exclusion_does_not_hide_sibling_artifact_residue(project):
    """T10/artifact: a whole-folder exclusion must not return through this path.

    INVERSE ablation: pass `protected` to the first dirty probe and this test
    fails because the sibling artifact is initially hidden and the owned status
    is rewritten despite that residue.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    (project.implementation_artifacts / "sibling-result.md").write_text("attempt residue\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()


def test_bound_spec_exclusion_does_not_hide_run_created_untracked_residue(project):
    """T10/untracked: an unrelated run-created path remains attempt dirtiness.

    INVERSE ablation: add `run-created.tmp` beside the owned-spec exclusion and
    this test fails because normalization runs while unrelated residue exists.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    (repo / "run-created.tmp").write_text("attempt residue\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert (repo / "run-created.tmp").is_file()
    assert "rollback-skipped-clean" not in flow.journal.events()


def test_unbound_spec_flip_retains_existing_dirty_policy(project):
    """T11: late `spec_file` cannot substitute for attempt-scoped ownership.

    INVERSE ablations: add the implementation-artifacts folder to the first dirty
    probe's exclusions, or substitute late `task.spec_file` ownership; either makes
    this test fail by emitting `rollback-skipped-clean`.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.spec_file = str(spec)  # deliberately late/accepted ownership only
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


@requires_descriptor_restoration
def test_plain_tracked_snapshot_restores_operator_bytes_child_reverted_to_baseline(project):
    """Git-clean child output cannot erase dirty input present before launch."""
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    task = _task(repo)
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    spec.write_bytes(operator)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    # The failed child discards the pre-launch operator edit and puts the tracked
    # file back at HEAD, which is invisible to Git's ordinary dirtiness probe.
    spec.write_bytes(baseline)
    assert not verify.attempt_dirty(repo, task.baseline_commit, task.baseline_untracked)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == operator
    assert flow.journal.fields("rollback-owned-spec-restored") == {
        "story_key": task.story_key,
        "spec": str(spec.resolve()),
        "checkout_dirty": True,
    }
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert task.preserve_ref is None  # the child's exact bytes remain durable in HEAD
    assert flow.calls.pauses == []


def test_plain_tracked_snapshot_forced_fallback_pauses_before_write(project, monkeypatch):
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    spec.write_bytes(operator)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    spec.write_bytes(baseline)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="manual adoption is required"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == baseline
    assert "rollback-owned-spec-restored" not in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="while restoring the attempt-owned lifecycle status",
        expected_status="ready-for-dev",
    )


@requires_descriptor_restoration
def test_plain_auto_reset_restores_unchanged_prelaunch_operator_spec(project):
    """Sibling rollback cannot erase tracked operator input the child inherited.

    Ablation: gate the post-reset restore on ``owned_snapshot_changed`` and the
    spec falls back to its Git baseline even though the durable launch snapshot
    proves the operator bytes predated the failed child.
    """
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    spec.write_bytes(operator)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    source = repo / "src.txt"
    source.write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == operator
    assert source.read_text() == "original\n"
    assert task.preserve_ref is not None
    assert flow.journal.fields("rollback-owned-spec-restored") == {
        "story_key": task.story_key,
        "spec": str(spec.resolve()),
        "checkout_dirty": True,
    }


def test_plain_forced_fallback_pauses_after_completed_baseline_reset(project, monkeypatch):
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    spec.write_bytes(operator)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    source = repo / "src.txt"
    source.write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="after the baseline reset"):
        flow.rollback_or_pause(task)

    assert source.read_text() == "original\n"
    assert spec.read_bytes() == baseline
    assert "rollback-auto" in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback"]
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="after the baseline reset",
    )


@requires_descriptor_restoration
def test_plain_manual_pause_restores_operator_spec_child_put_at_baseline_with_sibling(project):
    """A sibling does not hide a provably baseline-shaped child spec deletion.

    Git-for-Windows materializes the tracked LF blob as CRLF under its system
    ``core.autocrlf=true`` default. The baseline oracle must compare the file to
    that filtered checkout form, not to raw object bytes. Ablation: switch the
    recovery read back to ``file_bytes_at_revision`` and this test reaches the
    generic manual pause without restoring the operator snapshot.
    """
    repo = project.project
    git(repo, "config", "core.autocrlf", "true")
    spec = _tracked_spec(project)
    spec_rel = spec.relative_to(repo).as_posix()
    spec.unlink()
    git(repo, "checkout", "--", spec_rel)
    baseline = spec.read_bytes()
    assert b"\r\n" in baseline
    baseline_blob = verify.file_bytes_at_revision(repo, verify.rev_parse_head(repo), spec_rel)
    assert baseline_blob is not None and b"\r\n" not in baseline_blob
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    # The failed child erased the operator edit and also left unrelated residue,
    # so the older spec-only restoration branch cannot run.
    spec.write_bytes(baseline)
    source = repo / "src.txt"
    source.write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause, match="restored the byte-exact pre-launch operator input"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == operator
    assert source.read_text() == "failed child sibling\n"
    assert "rollback-owned-spec-restored" in flow.journal.events()
    assert "rollback-manual-required" in flow.journal.events()
    assert task.preserve_ref is None


def test_plain_sibling_residue_forced_fallback_preempts_generic_manual_pause(project, monkeypatch):
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    operator = baseline.replace(b"baseline intent", b"operator input outside HEAD")
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = operator
    spec.write_bytes(baseline)
    source = repo / "src.txt"
    source.write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="before the ordinary manual-recovery pause"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == baseline
    assert source.read_text() == "failed child sibling\n"
    assert "rollback-owned-spec-restored" not in flow.journal.events()
    assert "rollback-manual-required" not in flow.journal.events()
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="before the ordinary manual-recovery pause",
    )


@requires_descriptor_restoration
def test_latched_redrive_reports_owned_corrected_spec_as_still_dirty(project):
    """T12: failed child body edits restore the pre-attempt human correction.

    Ablation: replace the snapshot restore with status-only normalization and
    this test fails because the failed child's body survives into the retry.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    child = b"---\nstatus: in-progress\n---\n\nfailed child body edit\n"
    task.dispatched_spec_snapshot = corrected
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == corrected
    assert b"failed child body edit" not in spec.read_bytes()
    assert task.preserve_ref is not None
    rel = spec.relative_to(repo).as_posix()
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")
    assert verify.attempt_dirty(repo, task.baseline_commit, task.baseline_untracked)
    assert flow.journal.fields("rollback-owned-spec-normalized") == {
        "story_key": task.story_key,
        "spec": str(spec.resolve()),
        "status": "ready-for-dev",
        "checkout_dirty": True,
    }
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.calls.pauses == []
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]


def test_latched_redrive_snapshot_equal_forced_fallback_pauses_for_retry_input(
    project, monkeypatch
):
    repo = project.project
    spec = _tracked_spec(project)
    baseline = spec.read_bytes()
    corrected = baseline.replace(b"baseline intent", b"human corrected intent")
    spec.write_bytes(corrected)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="pre-attempt retry input"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == corrected
    assert task.preserve_ref is None
    assert "rollback-owned-spec-normalized" not in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="while restoring the pre-attempt retry input",
        expected_status="ready-for-dev",
    )


@requires_descriptor_restoration
def test_latched_redrive_restores_preexisting_untracked_spec_by_snapshot(project):
    """Git's baseline-untracked name set cannot hide child edits to its contents.

    Ablation: remove the current-vs-snapshot byte comparison before the first
    clean return and recovery leaves the failed child body untouched.
    """
    repo = project.project
    spec = project.implementation_artifacts / "untracked-redrive.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected untracked intent\n"
    spec.write_bytes(corrected)
    task = _task(repo)
    rel = spec.relative_to(repo).as_posix()
    task.baseline_untracked = [rel]
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    spec.write_bytes(b"---\nstatus: done\n---\n\nfailed child untracked body\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == corrected
    assert b"failed child untracked body" not in spec.read_bytes()
    assert task.preserve_ref is not None
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == (
        b"---\nstatus: done\n---\n\nfailed child untracked body"
    )
    assert "attempt-worktree-preserved" in flow.journal.events()
    assert "rollback-owned-spec-restored" in flow.journal.events()
    assert flow.calls.pauses == []


def test_latched_redrive_forced_fallback_pauses_after_preservation(project, monkeypatch):
    repo = project.project
    spec = project.implementation_artifacts / "untracked-redrive-fallback.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected input\n"
    child = b"---\nstatus: done\n---\n\nfailed child input\n"
    spec.write_bytes(corrected)
    task = _task(repo)
    rel = spec.relative_to(repo).as_posix()
    task.baseline_untracked = [rel]
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="manual adoption is required"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert task.preserve_ref is not None
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")
    assert "attempt-worktree-preserved" in flow.journal.events()
    assert "rollback-owned-spec-restored" not in flow.journal.events()
    assert "post_rollback" not in flow.calls.emits
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="before the baseline reset",
        expected_status="ready-for-dev",
    )


def test_latched_redrive_forced_fallback_pauses_after_completed_baseline_reset(
    project, monkeypatch
):
    repo = project.project
    source = repo / "redrive-source.txt"
    source.write_text("baseline source\n")
    spec = _tracked_spec(project)
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    spec.write_bytes(corrected)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    source.write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="after the baseline reset"):
        flow.rollback_or_pause(task)

    assert source.read_text() == "baseline source\n"
    # The reset's `preserve` round-trips the artifact folder through
    # `git stash create` + `git checkout`, which under Git-for-Windows'
    # `core.autocrlf=true` re-materializes this LF content as CRLF. What is
    # asserted is the content the refused restoration left alone, so compare
    # newline-normalized rather than raw.
    assert spec.read_bytes().replace(b"\r\n", b"\n") == corrected
    assert "rollback-auto" in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback"]
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="after the baseline reset",
        expected_status="ready-for-dev",
    )


@requires_descriptor_restoration
def test_latched_redrive_restores_ignored_spec_as_untracked_after_child_commit(project):
    """A failed child cannot turn restored ignored input into a staged addition."""
    repo = project.project
    spec = project.implementation_artifacts / "ignored-committed-redrive.md"
    rel = spec.relative_to(repo).as_posix()
    (repo / ".gitignore").write_text(f"/{rel}\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore redrive spec")
    corrected = b"---\nstatus: ready-for-dev\n---\n\noperator ignored input\n"
    child = b"---\nstatus: done\n---\n\nfailed child committed input\n"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_bytes(corrected)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    spec.write_bytes(child)
    git(repo, "add", "-f", rel)
    git(repo, "commit", "-q", "-m", "failed child force-adds ignored spec")
    failed_head = rev_parse_head(repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == corrected
    assert not verify.path_tracked(repo, rel)
    assert task.preserve_ref is not None
    assert git(repo, "rev-parse", task.preserve_ref) == failed_head
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")


@pytest.mark.parametrize("git_invisible", ["baseline-untracked", "ignored"])
@pytest.mark.parametrize("child_index", ["staged", "committed"])
@requires_descriptor_restoration
def test_plain_reset_recreates_force_added_git_invisible_snapshot(
    project, git_invisible, child_index
):
    """A baseline reset may delete the path; recovery must recreate its input."""
    repo = project.project
    spec = project.implementation_artifacts / f"plain-{git_invisible}-{child_index}.md"
    rel = spec.relative_to(repo).as_posix()
    if git_invisible == "ignored":
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore plain owned spec")
    spec.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    child = b"---\nstatus: done\n---\n\nfailed child input\n"
    spec.write_bytes(original)
    task = _task(repo)
    task.baseline_untracked = [rel] if git_invisible == "baseline-untracked" else []
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = original
    spec.write_bytes(child)
    git(repo, "add", "-f", rel)
    if child_index == "committed":
        git(repo, "commit", "-q", "-m", "failed child force-adds owned spec")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == original
    assert not verify.path_tracked(repo, rel)
    assert not verify.index_path_changed_since(repo, task.baseline_commit, rel)
    assert task.preserve_ref is not None
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")
    assert "rollback-owned-spec-restored" in flow.journal.events()


@pytest.mark.parametrize("git_invisible", ["baseline-untracked", "ignored"])
def test_plain_git_invisible_snapshot_forced_fallback_pauses_before_reset(
    project, monkeypatch, git_invisible
):
    repo = project.project
    spec = project.implementation_artifacts / f"fallback-{git_invisible}.md"
    rel = spec.relative_to(repo).as_posix()
    if git_invisible == "ignored":
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore fallback owned spec")
    spec.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    child = b"---\nstatus: done\n---\n\nfailed child input\n"
    spec.write_bytes(original)
    task = _task(repo)
    task.baseline_untracked = [rel] if git_invisible == "baseline-untracked" else []
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = original
    spec.write_bytes(child)
    git(repo, "add", "-f", rel)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="manual adoption is required"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert verify.index_path_changed_since(repo, task.baseline_commit, rel)
    assert task.preserve_ref is not None
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")
    assert "rollback-owned-spec-restored" not in flow.journal.events()
    assert "post_rollback" not in flow.calls.emits
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="before the baseline reset",
    )


@pytest.mark.parametrize("baseline_present", [False, True], ids=["force-add", "cached-remove"])
@requires_descriptor_restoration
def test_latched_redrive_resets_index_only_owned_spec_mutation(project, baseline_present):
    """Snapshot-equal bytes cannot hide a child-authored index ownership change."""
    repo = project.project
    if baseline_present:
        spec = _tracked_spec(project, name="tracked-index-only.md")
    else:
        spec = project.implementation_artifacts / "ignored-index-only.md"
        rel = spec.relative_to(repo).as_posix()
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore index-only spec")
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("---\nstatus: ready-for-dev\n---\n\noperator input\n")
    rel = spec.relative_to(repo).as_posix()
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    snapshot = spec.read_bytes()
    task.dispatched_spec_snapshot = snapshot
    task.resolved_redrive = True
    if baseline_present:
        git(repo, "rm", "--cached", rel)
    else:
        git(repo, "add", "-f", rel)
    assert verify.index_path_changed_since(repo, task.baseline_commit, rel)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == snapshot
    assert verify.path_tracked(repo, rel) is baseline_present
    assert not verify.index_path_changed_since(repo, task.baseline_commit, rel)
    assert "rollback-auto" in flow.journal.events()
    assert "rollback-owned-spec-restored" in flow.journal.events()


@pytest.mark.parametrize("baseline_present", [False, True], ids=["force-add", "cached-remove"])
def test_latched_redrive_index_only_forced_fallback_pauses_after_preservation(
    project, monkeypatch, baseline_present
):
    repo = project.project
    if baseline_present:
        spec = _tracked_spec(project, name="tracked-index-only-fallback.md")
    else:
        spec = project.implementation_artifacts / "ignored-index-only-fallback.md"
        rel = spec.relative_to(repo).as_posix()
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore fallback index-only spec")
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("---\nstatus: ready-for-dev\n---\n\noperator input\n")
    rel = spec.relative_to(repo).as_posix()
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    snapshot = spec.read_bytes()
    task.dispatched_spec_snapshot = snapshot
    task.resolved_redrive = True
    if baseline_present:
        git(repo, "rm", "--cached", rel)
    else:
        git(repo, "add", "-f", rel)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="before the baseline reset"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == snapshot
    assert verify.index_path_changed_since(repo, task.baseline_commit, rel)
    if baseline_present:
        assert task.preserve_ref is None  # cached removal has no child bytes to park
        assert "attempt-worktree-preserved" not in flow.journal.events()
    else:
        assert task.preserve_ref is not None
        assert "attempt-worktree-preserved" in flow.journal.events()
    assert "rollback-owned-spec-restored" not in flow.journal.events()
    assert "post_rollback" not in flow.calls.emits
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="before the baseline reset",
        expected_status="ready-for-dev",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
def test_plain_reset_refuses_baseline_parent_retarget_before_mutation(project, tmp_path):
    """A reset cannot turn canonical snapshot authority into an external write."""
    repo = project.project
    parent = project.implementation_artifacts / "baseline-link"
    victim_parent = tmp_path / "external-victim"
    victim_parent.mkdir()
    victim = victim_parent / "owned.md"
    victim.write_bytes(b"external victim\n")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.symlink_to(victim_parent, target_is_directory=True)
    git(repo, "add", parent.relative_to(repo).as_posix())
    git(repo, "commit", "-q", "-m", "baseline artifact symlink")
    parent.unlink()
    parent.mkdir()
    spec = parent / "owned.md"
    operator = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec.write_bytes(operator)
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = operator
    (repo / "src.txt").write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause, match="baseline would replace"):
        flow.rollback_or_pause(task)

    assert not parent.is_symlink()
    assert spec.read_bytes() == operator
    assert victim.read_bytes() == b"external victim\n"
    assert "rollback-auto" not in flow.journal.events()


@pytest.mark.parametrize("baseline_shape", ["symlink", "tree"])
def test_plain_reset_refuses_unsafe_baseline_final_shape_before_mutation(
    project, tmp_path, baseline_shape
):
    """Reset cannot replace a canonical input with a symlink or directory."""
    if baseline_shape == "symlink" and sys.platform == "win32":
        pytest.skip("file symlinks may need elevation")
    repo = project.project
    spec = project.implementation_artifacts / "baseline-final-shape"
    spec.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "external-victim.md"
    victim.write_bytes(b"external victim\n")
    if baseline_shape == "symlink":
        spec.symlink_to(victim)
    else:
        spec.mkdir()
        (spec / "tracked.md").write_text("baseline tree\n")
    rel = spec.relative_to(repo).as_posix()
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", f"baseline final {baseline_shape}")
    if baseline_shape == "symlink":
        spec.unlink()
    else:
        (spec / "tracked.md").unlink()
        spec.rmdir()
    operator = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec.write_bytes(operator)
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = operator
    (repo / "src.txt").write_text("failed child sibling\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause, match="baseline would replace"):
        flow.rollback_or_pause(task)

    assert spec.is_file() and not spec.is_symlink()
    assert spec.read_bytes() == operator
    assert victim.read_bytes() == b"external victim\n"
    assert "rollback-auto" not in flow.journal.events()


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
def test_plain_normalization_restore_revalidates_parent_authority(project, tmp_path, monkeypatch):
    """A parent retarget during tentative normalization cannot redirect restore."""
    repo = project.project
    parent = project.implementation_artifacts / "normalize-retarget"
    spec = parent / "owned.md"
    parent.mkdir(parents=True, exist_ok=True)
    original = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec.write_bytes(original)
    git(repo, "add", spec.relative_to(repo).as_posix())
    git(repo, "commit", "-q", "-m", "tracked normalization target")
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = original
    spec.write_bytes(b"---\nstatus: done\n---\n\nfailed child body\n")
    victim_parent = tmp_path / "external-victim"
    victim_parent.mkdir()
    victim = victim_parent / "owned.md"
    victim.write_bytes(b"external victim\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    def retarget_after_normalize(path, target_status, *, confine_root):
        RecoveryFlow._normalize_attempt_owned_spec(path, target_status, confine_root=confine_root)
        path.unlink()
        parent.rmdir()
        parent.symlink_to(victim_parent, target_is_directory=True)

    monkeypatch.setattr(flow, "_normalize_attempt_owned_spec", retarget_after_normalize)

    with pytest.raises(_Pause, match="unsafe while undoing") as raised:
        flow.rollback_or_pause(task)

    assert parent.is_symlink()
    assert victim.read_bytes() == b"external victim\n"
    assert "rollback-auto" not in flow.journal.events()
    assert "now requires inspection" in str(raised.value)
    assert "left untouched" not in str(raised.value)


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
def test_post_reset_authority_failure_reports_completed_rollback(project, tmp_path, monkeypatch):
    """A post-reset TOCTOU pause never claims the checkout was untouched."""
    repo = project.project
    parent = project.implementation_artifacts / "post-reset-retarget"
    spec = parent / "owned.md"
    rel = spec.relative_to(repo).as_posix()
    (repo / ".gitignore").write_text(f"/{rel}\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore post-reset target")
    parent.mkdir(parents=True, exist_ok=True)
    original = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec.write_bytes(original)
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = original
    spec.write_bytes(b"---\nstatus: done\n---\n\nfailed child body\n")
    git(repo, "add", "-f", rel)
    victim_parent = tmp_path / "external-victim"
    victim_parent.mkdir()
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    real_safe_reset = flow.safe_reset

    def retarget_after_reset(reset_task, *, preserve=()):
        real_safe_reset(reset_task, preserve=preserve)
        if spec.exists():
            spec.unlink()
        if parent.exists():
            parent.rmdir()
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.symlink_to(victim_parent, target_is_directory=True)

    monkeypatch.setattr(flow, "safe_reset", retarget_after_reset)

    with pytest.raises(_Pause, match="unsafe after the baseline reset") as raised:
        flow.rollback_or_pause(task)

    assert parent.is_symlink()
    assert not (victim_parent / "owned.md").exists()
    assert "rollback-auto" in flow.journal.events()
    assert "any rollback already completed" in str(raised.value)
    assert "left untouched" not in str(raised.value)


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
def test_resolved_redrive_normalization_confines_to_the_project_not_workspace_root(
    project, tmp_path, monkeypatch
):
    """The supported `repo_root` override (isolation = "none") makes
    `workspace.root` the separate CODE repo while the attempt binding resolves
    under `workspace.paths.project`. Threading `workspace.root` as
    `confine_root` made an in-project spec fail the chokepoint's
    `is_relative_to` test and silently take the plain arm, whose parent
    directories are resolved by NAME — so a parent swapped after
    `_attempt_owned_spec` validated the binding sent the post-reset route
    repair outside the project. The recovery sites now thread
    `workspace.paths.project` (equal to `workspace.root` under worktree
    isolation via `ProjectPaths.rebased`, so only the override shape moves).
    This drives the bare-normalize site of the `resolved` unwind — the one arm
    where the confined walk is the ONLY parent authority: no byte restore runs
    there to re-validate the path.

    Ablation: thread `workspace.root` back at that site and this fails on
    `DID NOT RAISE UnconfinedWriteError` — measured with the gate ablated, the
    decoy behind the swapped parent is rewritten to `ready-for-dev` (content
    escaping the project through the parent link)."""
    code_repo = tmp_path / "code-repo"
    code_repo.mkdir()
    git(code_repo, "init")
    git(code_repo, "config", "user.email", "t@t")
    git(code_repo, "config", "user.name", "t")
    (code_repo / "src.txt").write_text("baseline\n")
    git(code_repo, "add", "-A")
    git(code_repo, "commit", "-q", "-m", "code baseline")

    parent = project.implementation_artifacts / "override-retarget"
    parent.mkdir(parents=True, exist_ok=True)
    spec = parent / "owned.md"
    spec.write_bytes(b"---\nstatus: done\n---\n\nescalated attempt\n")

    override = ProjectPaths(
        project=project.project,
        implementation_artifacts=project.implementation_artifacts,
        planning_artifacts=project.planning_artifacts,
        output_folder=project.output_folder,
        repo_root=code_repo,
    )
    workspace = Workspace.default(override)
    assert workspace.root == code_repo  # the override shape this row exists for
    flow = _make_flow(workspace=workspace, policy=_policy(rollback_on_failure=False))

    task = _task(code_repo)
    task.dispatched_spec_file = str(spec.resolve())
    (code_repo / "src.txt").write_text("failed attempt residue\n")  # real dirt to undo

    victim_parent = tmp_path / "external-victim"
    victim_parent.mkdir()
    decoy = b"---\nstatus: done\n---\n\nexternal victim\n"
    (victim_parent / "owned.md").write_bytes(decoy)
    real_safe_reset = flow.safe_reset

    def retarget_after_reset(reset_task, *, preserve=()):
        real_safe_reset(reset_task, preserve=preserve)
        spec.unlink()
        parent.rmdir()
        parent.symlink_to(victim_parent, target_is_directory=True)

    monkeypatch.setattr(flow, "safe_reset", retarget_after_reset)

    with pytest.raises(UnconfinedWriteError):
        flow.rollback_or_pause(task, cause="resolved")

    assert parent.is_symlink()
    assert (victim_parent / "owned.md").read_bytes() == decoy  # nothing escaped


def test_resolved_cause_forced_fallback_pauses_after_completed_reset(project, monkeypatch):
    repo = project.project
    spec = _tracked_spec(project)
    remaining = b"---\nstatus: done\n---\n\nhuman corrected intent\n"
    spec.write_bytes(remaining)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = b"stale bytes from the abandoned attempt"
    source = repo / "src.txt"
    source.write_text("failed attempt residue\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="after the baseline reset"):
        flow.rollback_or_pause(task, cause="resolved")

    assert source.read_text() == "original\n"
    # Same `git stash create` + `git checkout` preserve round trip as
    # `test_latched_redrive_forced_fallback_pauses_after_completed_baseline_reset`:
    # the refused restoration left the operator's `done` content in place, and
    # only its line endings may differ under `core.autocrlf=true`.
    assert spec.read_bytes().replace(b"\r\n", b"\n") == remaining
    assert "rollback-auto" in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback"]
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="after the baseline reset",
        expected_status="ready-for-dev",
    )


@pytest.mark.parametrize("git_invisible", ["baseline-untracked", "ignored"])
@requires_descriptor_restoration
def test_plain_attempt_restores_and_parks_git_invisible_owned_spec(project, git_invisible):
    """A plain child cannot hide body edits in Git's untracked blind spots.

    The child bytes are force-added only to the temporary recovery index, then
    the byte-exact pre-launch input is restored. Ablation: scope the snapshot
    comparison back to resolved redrives and both rows falsely report clean;
    omit ``force_include`` and the recovery ref contains no child spec.
    """
    repo = project.project
    spec = project.implementation_artifacts / f"{git_invisible}-owned.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    rel = spec.relative_to(repo).as_posix()
    if git_invisible == "ignored":
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore owned spec")
    original = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    child = b"---\nstatus: done\n---\n\nfailed child body\n"
    spec.write_bytes(original)
    task = _task(repo)
    task.baseline_untracked = [rel] if git_invisible == "baseline-untracked" else []
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = original
    spec.write_bytes(child)
    assert not verify.attempt_dirty(repo, task.baseline_commit, task.baseline_untracked)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == original
    assert task.preserve_ref is not None
    assert git(repo, "show", f"{task.preserve_ref}:{rel}").encode() == child.rstrip(b"\n")
    assert "attempt-worktree-preserved" in flow.journal.events()
    assert "rollback-owned-spec-restored" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.calls.pauses == []


def test_unchanged_ignored_owned_spec_with_snapshot_is_a_clean_noop(project):
    """Snapshot equality proves a Git-ignored binding was not child-modified."""
    repo = project.project
    spec = project.implementation_artifacts / "ignored-unchanged.md"
    rel = spec.relative_to(repo).as_posix()
    (repo / ".gitignore").write_text(f"/{rel}\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore unchanged spec")
    spec.parent.mkdir(parents=True, exist_ok=True)
    snapshot = b"---\nstatus: ready-for-dev\n---\n\noperator input\n"
    spec.write_bytes(snapshot)
    task = _task(repo)
    task.baseline_untracked = []
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = snapshot
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == snapshot
    assert flow.journal.events() == ["rollback-skipped-clean"]
    assert task.preserve_ref is None


@requires_descriptor_restoration
def test_latched_redrive_reset_normalizes_preserved_spec_after_sibling_residue(project):
    """A non-fixable retry re-establishes the route its next prompt declares.

    The reset removes the rejected implementation edit while preserving the
    human-corrected artifact tree. Ablation: delete the post-reset owned-spec
    normalization and this test fails with the retained ``done`` status.
    """
    repo = project.project
    source = repo / "redrive-source.txt"
    source.write_text("baseline source\n")
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    task.dispatched_spec_snapshot = corrected
    source.write_text("rejected implementation\n")
    spec.write_text("---\nstatus: done\n---\n\nfailed child body edit\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    flow.rollback_or_pause(task)

    assert source.read_text() == "baseline source\n"
    assert spec.read_bytes() == corrected
    assert b"failed child body edit" not in spec.read_bytes()
    assert flow.journal.fields("rollback-owned-spec-normalized") == {
        "story_key": task.story_key,
        "spec": str(spec.resolve()),
        "status": "ready-for-dev",
        "checkout_dirty": True,
    }
    assert "rollback-auto" in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]


def test_latched_redrive_without_snapshot_refuses_reset_of_sibling_residue(project):
    """Legacy state cannot guess which owned-spec bytes came from the child.

    Ablation: remove the pre-policy missing-snapshot guard and rollback-on-failure
    resets the sibling while retaining the child's arbitrary spec body.
    """
    repo = project.project
    source = repo / "redrive-source.txt"
    source.write_text("baseline source\n")
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    source.write_text("rejected implementation\n")
    spec.write_text("---\nstatus: done\n---\n\nunknown child-or-operator body\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert source.read_text() == "rejected implementation\n"
    assert b"unknown child-or-operator body" in spec.read_bytes()
    assert "rollback-owned-spec-snapshot-missing" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None

    # The notice tells the operator to restore/verify the approved spec. Because
    # the unusable pair was cleared before the pause, that remedy converges rather
    # than hitting the same legacy-snapshot guard forever on resume.
    corrected = b"---\nstatus: ready-for-dev\n---\n\noperator restored intent\n"
    spec.write_bytes(corrected)
    flow.rollback_or_pause(task)
    # The now-unbound whole-folder preserve runs through Git checkout filters;
    # content and lifecycle survive, while LF may correctly materialize as CRLF.
    assert spec.read_text() == corrected.decode()
    assert flow.journal.events().count("rollback-owned-spec-manual-required") == 1
    assert "rollback-auto" in flow.journal.events()


def test_latched_redrive_deleted_owned_spec_refuses_automatic_reset(project):
    """A child deletion cannot bypass snapshot-backed ownership recovery.

    Ablation: remove the unavailable-owned-spec guard and rollback-on-failure
    resets automatically without recreating the operator's corrected spec.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = spec.read_bytes()
    task.resolved_redrive = True
    spec.unlink()
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert not spec.exists()
    assert "rollback-owned-spec-unavailable" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_latched_redrive_unreadable_owned_spec_requires_manual_recovery(project, monkeypatch):
    """A recovery-time read fault cannot become permission to overwrite."""
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = spec.read_bytes()
    task.resolved_redrive = True
    child = b"---\nstatus: done\n---\n\nfailed child body\n"
    spec.write_bytes(child)
    real_read_bytes = Path.read_bytes

    def unreadable(path):
        if path == spec.resolve():
            raise PermissionError("simulated unreadable spec")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause, match="current bytes could not be read"):
        flow.rollback_or_pause(task)

    assert real_read_bytes(spec) == child
    assert "rollback-owned-spec-unreadable" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


@pytest.mark.skipif(sys.platform == "win32", reason="file symlink creation may need elevation")
def test_latched_redrive_symlink_replacement_cannot_retarget_snapshot(project):
    """A child cannot redirect operator bytes into another trusted file."""
    repo = project.project
    spec = _tracked_spec(project)
    victim = project.implementation_artifacts / "trusted-victim.txt"
    victim_bytes = b"unrelated trusted contents\n"
    victim.write_bytes(victim_bytes)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add trusted victim")
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = spec.read_bytes()
    task.resolved_redrive = True
    spec.unlink()
    spec.symlink_to(victim)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert spec.is_symlink()
    assert victim.read_bytes() == victim_bytes
    assert "rollback-owned-spec-unavailable" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()


def test_latched_redrive_refuses_to_overwrite_changed_external_spec(project, tmp_path):
    """Even a re-drive cannot replace external child bytes it cannot park."""
    repo = project.project
    external_impl = tmp_path / "external-artifacts"
    external_impl.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=external_impl,
        planning_artifacts=project.planning_artifacts,
        output_folder=project.output_folder,
        repo_root=repo,
    )
    spec = external_impl / "spec-1-1-a.md"
    corrected = b"---\nstatus: ready-for-dev\n---\n\nexternal operator intent\n"
    spec.write_bytes(corrected)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = corrected
    task.resolved_redrive = True
    child = b"---\nstatus: done\n---\n\nfailed child external body\n"
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace(root=repo, paths=paths),
        paths=paths,
        policy=_policy(rollback_on_failure=False),
    )

    with pytest.raises(_Pause, match="outside Git and cannot be parked"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert "rollback-owned-spec-unpreservable" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_plain_attempt_refuses_to_overwrite_changed_external_spec(project, tmp_path):
    """Rollback policy cannot replace external child bytes it cannot park."""
    repo = project.project
    external_impl = tmp_path / "external-artifacts"
    external_impl.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=external_impl,
        planning_artifacts=project.planning_artifacts,
        output_folder=project.output_folder,
        repo_root=repo,
    )
    spec = external_impl / "spec-1-1-a.md"
    original = b"---\nstatus: ready-for-dev\n---\n\nexternal input\n"
    child = b"---\nstatus: done\n---\n\nfailed child external body\n"
    spec.write_bytes(original)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.dispatched_spec_snapshot = original
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace(root=repo, paths=paths),
        paths=paths,
        policy=_policy(rollback_on_failure=True),
    )

    with pytest.raises(_Pause, match="outside Git and cannot be parked"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert "rollback-owned-spec-unpreservable" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


@requires_descriptor_restoration
def test_latched_redrive_parks_child_commit_and_restores_operator_snapshot(project):
    """Committed child body edits cannot hide behind the retained correction.

    Ablation: remove the pre-restore ``commits_above`` probe and recovery takes
    the owned-dirty early return, leaving the child commit at HEAD.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    baseline = task.baseline_commit
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    task.dispatched_spec_snapshot = corrected
    spec.write_text("---\nstatus: done\n---\n\nfailed child body edit\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "failed redrive child")
    failed_head = rev_parse_head(repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert rev_parse_head(repo) == baseline
    assert spec.read_bytes() == corrected
    assert b"failed child body edit" not in spec.read_bytes()
    assert task.preserve_ref is not None
    assert git(repo, "rev-parse", task.preserve_ref) == failed_head
    assert "rollback-auto" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()


def test_latched_redrive_refuses_restore_when_child_commit_cannot_be_parked(project, monkeypatch):
    """A failed commit ref cannot be bypassed by a clean forced worktree tree."""
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    child = b"---\nstatus: done\n---\n\nfailed committed child body\n"
    task.dispatched_spec_snapshot = corrected
    spec.write_bytes(child)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "failed child")
    failed_head = rev_parse_head(repo)
    monkeypatch.setattr(verify, "preserve_commits", lambda *args, **kwargs: None)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    with pytest.raises(_Pause, match="could not be auto-preserved"):
        flow.rollback_or_pause(task)

    assert rev_parse_head(repo) == failed_head
    assert spec.read_bytes() == child
    assert "attempt-preserve-failed" in flow.journal.events()
    assert "attempt-worktree-preserved" not in flow.journal.events()


@requires_descriptor_restoration
def test_latched_redrive_preserves_uncommitted_child_bytes_above_child_commit(project):
    """The dirty preserve ref retains the child's latest uncommitted spec body.

    Ablation: keep the normalized-commit worktree-snapshot skip for redrives and
    the final preserve ref contains committed body A instead of uncommitted body B.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    corrected = b"---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n"
    task.dispatched_spec_snapshot = corrected
    spec.write_text("---\nstatus: done\n---\n\nfailed child committed body A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "failed redrive child A")
    spec.write_text("---\nstatus: done\n---\n\nfailed child uncommitted body B\n")
    spec_rel = spec.relative_to(repo).as_posix()
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert spec.read_bytes() == corrected
    assert task.preserve_ref is not None
    preserved = git(repo, "show", f"{task.preserve_ref}:{spec_rel}")
    assert "failed child uncommitted body B" in preserved
    assert "failed child committed body A" not in preserved


@requires_descriptor_restoration
def test_post_normalization_probe_fault_cannot_authorize_owned_dirty(project, monkeypatch):
    """A failed pre-reset re-probe cannot bypass the resolved reset.

    Ablation: remove `dirty_probe_succeeded` from the owned-dirty event guard and
    this test fails because an unproven checkout bypasses the actual rollback.
    After that rollback, a fresh successful probe may report the retained spec.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.resolved_redrive = True
    task.dispatched_spec_snapshot = b"stale bytes from the abandoned attempt"
    spec.write_text("---\nstatus: ready-for-dev\n---\n\nhuman corrected intent\n")
    real_attempt_dirty = verify.attempt_dirty
    probes = 0

    def fault_post_normalization_probe(*args, **kwargs):
        nonlocal probes
        probes += 1
        if probes == 3:
            raise verify.GitError("post-normalization probe failed")
        return real_attempt_dirty(*args, **kwargs)

    monkeypatch.setattr(verify, "attempt_dirty", fault_post_normalization_probe)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task, cause="resolved")

    assert probes == 4
    assert "rollback-dirty-check-failed" in flow.journal.events()
    assert "rollback-auto" in flow.journal.events()
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.journal.fields("rollback-owned-spec-normalized")["checkout_dirty"] is True
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]
    assert verify.attempt_dirty(repo, task.baseline_commit, task.baseline_untracked)
    assert "human corrected intent" in spec.read_text()
    assert b"stale bytes" not in spec.read_bytes()


@requires_descriptor_restoration
def test_patch_restore_redrive_normalizes_owned_spec_to_in_review(project):
    """T13: the restore latch selects `in-review`, never from-scratch readiness.

    Ablation: replace the restore-latched target selection with unconditional
    `ready-for-dev` and this test fails on both the spec and journal status.
    """
    repo = project.project
    spec = _tracked_spec(project, status="in-review")
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.restore_patch = "intent-gap.patch"
    task.resolved_redrive = True
    task.dispatched_spec_snapshot = b"---\nstatus: in-review\n---\n\nrestored human correction\n"
    spec.write_text("---\nstatus: in-progress\n---\n\nrestored human correction\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    flow.rollback_or_pause(task)

    assert _status(spec) == "in-review"
    assert "restored human correction" in spec.read_text()
    event = flow.journal.fields("rollback-owned-spec-normalized")
    assert event["status"] == "in-review"
    assert event["checkout_dirty"] is True
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert flow.calls.emits == ["pre_rollback", "post_rollback"]


def test_patch_restore_redrive_forced_fallback_requires_in_review_adoption(project, monkeypatch):
    repo = project.project
    spec = _tracked_spec(project, status="in-review")
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    task.restore_patch = "intent-gap.patch"
    task.resolved_redrive = True
    restored = b"---\nstatus: in-review\n---\n\nrestored human correction\n"
    task.dispatched_spec_snapshot = restored
    spec.write_text("---\nstatus: in-progress\n---\n\nrestored human correction\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    monkeypatch.setattr(recovery_flow, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)

    with pytest.raises(_Pause, match="lifecycle status 'in-review'"):
        flow.rollback_or_pause(task)

    assert spec.read_text() == "---\nstatus: in-progress\n---\n\nrestored human correction\n"
    assert "rollback-owned-spec-normalized" not in flow.journal.events()
    assert "post_rollback" not in flow.calls.emits
    _assert_owned_spec_manual_adoption_pause(
        flow,
        task,
        spec,
        stage="before the baseline reset",
        expected_status="in-review",
    )


@requires_descriptor_restoration
def test_owned_spec_without_visible_status_fails_the_post_write_oracle(project):
    """T14/False: a writer no-op is not repair success without the target oracle.

    Ablation: delete the post-write `status_of(read_frontmatter(...))` comparison
    and this test fails by reaching ordinary rollback policy instead of the typed
    repair error.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    spec.write_text("---\ntitle: no status here\n---\n\nbaseline intent\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(verify.FrontmatterWriteError, match="could not normalize"):
        flow.rollback_or_pause(task)

    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()
    assert flow.calls.pauses == []


@requires_descriptor_restoration
def test_owned_spec_with_unsafe_status_shape_propagates_writer_error(project):
    """T14/write: repair-write refusal is never caught as failed observation.

    Ablation: catch `FrontmatterWriteError` around the normalization write and
    return from recovery; this test fails because the unsafe-shape error vanishes.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    spec.write_text("---\n{status: in-progress, keep: 1}\n---\nbaseline intent\n")
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(verify.FrontmatterWriteError, match="no in-place line edit"):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


def test_pre_repair_owned_spec_read_fault_pauses_without_mutation(project, monkeypatch):
    """An observational read fault degrades to convergent manual recovery.

    Ablation: remove the OSError guard around the pre-repair ``read_bytes`` and
    this test leaks the injected PermissionError instead of the typed pause.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    real_read_bytes = Path.read_bytes
    before = real_read_bytes(spec)
    canonical = spec.resolve()

    def fail_owned_read(path):
        if path == canonical:
            raise PermissionError("owned spec read denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_owned_read)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause, match="could not be read before lifecycle repair"):
        flow.rollback_or_pause(task)

    assert real_read_bytes(spec) == before
    assert "rollback-owned-spec-unreadable" in flow.journal.events()
    assert "rollback-owned-spec-manual-required" in flow.journal.events()
    assert "rollback-auto" not in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_owned_spec_outside_trusted_roots_is_not_excluded_or_mutated(project):
    """T15/outside: an in-repo path is not trusted merely because Git can name it.

    Ablation: delete the `spec_within_roots` refusal in `_attempt_owned_spec` and
    this test fails because the out-of-project file is normalized and called clean.
    """
    repo = project.project
    trusted = repo / "trusted-project"
    trusted_impl = trusted / "_bmad-output" / "implementation-artifacts"
    trusted_plan = trusted / "_bmad-output" / "planning-artifacts"
    trusted_impl.mkdir(parents=True)
    trusted_plan.mkdir(parents=True)
    paths = ProjectPaths(
        project=trusted,
        implementation_artifacts=trusted_impl,
        planning_artifacts=trusted_plan,
        repo_root=repo,
    )
    outside = repo / "outside-trusted-project.md"
    outside.write_text("---\nstatus: ready-for-dev\n---\nbody\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "outside binding baseline")
    task = _task(repo)
    task.dispatched_spec_file = str(outside)
    verify.set_frontmatter_status(outside, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace(root=repo, paths=paths),
        paths=paths,
        policy=_policy(rollback_on_failure=False),
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(outside) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


def test_missing_attempt_binding_does_not_fall_back_to_late_spec(project):
    """T15/missing: a stale attempt binding cannot borrow accepted ownership.

    INVERSE ablation: fall back to `task.spec_file` when the dispatched path is
    missing and this test fails because the late spec is normalized and called clean.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.spec_file = str(spec)
    task.dispatched_spec_file = str(project.implementation_artifacts / "missing.md")
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


def test_non_file_attempt_binding_is_refused(project):
    """T15/non-file: a directory can never become an exact owned-spec exclusion.

    INVERSE ablation: accept the resolved candidate without its `is_file` guard
    and this test fails on the attempted directory frontmatter repair.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(project.implementation_artifacts)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


def test_ambiguous_relative_attempt_binding_is_refused(project):
    """T15/ambiguous: two live interpretations cannot confer attempt ownership.

    Ablation: weaken the unique-candidate guard to accept the first live file and
    this test fails because the project-relative candidate is normalized as owned.
    """
    repo = project.project
    project_candidate = repo / "spec.md"
    artifact_candidate = project.implementation_artifacts / "spec.md"
    for candidate in (project_candidate, artifact_candidate):
        candidate.write_text("---\nstatus: ready-for-dev\n---\nbody\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ambiguous spec baseline")
    task = _task(repo)
    task.dispatched_spec_file = "spec.md"
    verify.set_frontmatter_status(project_candidate, "in-progress", confine_root=repo)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(project_candidate) == "in-progress"
    assert _status(artifact_candidate) == "ready-for-dev"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


@pytest.mark.parametrize(
    "resolve_fault",
    [
        pytest.param(OSError("binding resolve failed"), id="oserror"),
        pytest.param(RuntimeError("binding resolve failed"), id="runtimeerror"),
        *NUL_PATH_RESOLVE_FAULTS,
    ],
)
def test_binding_resolution_fault_is_fail_safe_dirty(project, monkeypatch, resolve_fault):
    """T15/unsafe: uncertain ownership cannot become a mutation/exclusion grant.

    Ablation: let `_attempt_owned_spec` continue with the unresolved candidate
    after `Path.resolve` raises and this test fails before the manual-pause policy.
    """
    repo = project.project
    spec = _tracked_spec(project)
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    verify.set_frontmatter_status(spec, "in-progress", confine_root=repo)
    refuse_to_resolve(monkeypatch, spec, error=resolve_fault)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert "rollback-skipped-clean" not in flow.journal.events()
    assert "rollback-owned-spec-normalized" not in flow.journal.events()


def test_legacy_external_owned_spec_without_snapshot_requires_manual_adoption(
    project, tmp_path, monkeypatch
):
    """A configured external artifact root is trusted but never Git-verifiable.

    Legacy state has no byte snapshot, so a Git-clean checkout cannot prove the
    external spec itself is unchanged. Recovery leaves it untouched, clears the
    unusable binding, and asks the operator to adopt the intended bytes once.
    """
    repo = project.project
    external_impl = tmp_path / "external-artifacts"
    external_impl.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=external_impl,
        planning_artifacts=project.planning_artifacts,
        output_folder=project.output_folder,
        repo_root=repo,
    )
    spec = external_impl / "spec-1-1-a.md"
    spec.write_text("---\nstatus: in-progress\n---\nexternal intent\n")
    task = _task(repo)
    task.dispatched_spec_file = str(spec)
    seen_excludes: list[tuple[str, ...]] = []
    real_attempt_dirty = verify.attempt_dirty

    def recording_attempt_dirty(*args, **kwargs):
        seen_excludes.append(kwargs.get("exclude", ()))
        return real_attempt_dirty(*args, **kwargs)

    monkeypatch.setattr(verify, "attempt_dirty", recording_attempt_dirty)
    flow = _make_flow(
        workspace=Workspace(root=repo, paths=paths),
        paths=paths,
        policy=_policy(rollback_on_failure=False),
    )

    with pytest.raises(_Pause, match="attempt-owned spec needs manual recovery"):
        flow.rollback_or_pause(task)

    assert _status(spec) == "in-progress"
    assert seen_excludes == [()]
    assert "rollback-owned-spec-snapshot-missing" in flow.journal.events()
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


# --------------------------------------------------------------- preserve refs


def test_preserve_attempt_commits_parks_committed_work(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")

    flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-commits-preserved" in flow.journal.events()
    ref = flow.journal.fields("attempt-commits-preserved")["ref"]
    assert ref.startswith("attempt-preserve/")
    git(repo, "rev-parse", "--verify", ref)  # the recovery branch exists
    assert task.preserve_ref == ref  # #333: the ref reaches run state, not just the journal


def test_preserve_attempt_commits_pins_the_observed_head(project, monkeypatch):
    """Range enumeration and ref creation use one HEAD observed before either."""
    repo = project.project
    flow = _make_flow(workspace=Workspace.default(project))
    task = _task(repo)
    baseline = task.baseline_commit
    assert baseline is not None
    git(repo, "checkout", "-q", "-b", "unrelated", baseline)
    git(repo, "commit", "--allow-empty", "-q", "-m", "unrelated commit")
    unrelated = rev_parse_head(repo)
    git(repo, "checkout", "-q", "-b", "attempt", baseline)
    git(repo, "commit", "--allow-empty", "-q", "-m", "attempt commit")
    intended = rev_parse_head(repo)
    real_commits_above = verify.commits_above

    def move_then_enumerate(repo, baseline, revision="HEAD"):
        assert revision == intended
        git(repo, "checkout", "-q", "unrelated")
        return real_commits_above(repo, baseline, revision)

    monkeypatch.setattr(verify, "commits_above", move_then_enumerate)

    flow.preserve_attempt_commits(task, allow_pause=True)

    assert rev_parse_head(repo) == unrelated
    assert task.preserve_ref is not None
    assert git(repo, "rev-parse", task.preserve_ref) == intended


def test_preserve_attempt_commits_noop_without_commits(project):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(project.project)  # HEAD == baseline, nothing committed above it

    flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-commits-preserved" not in flow.journal.events()
    assert flow.calls.pauses == []
    assert task.preserve_ref is None  # nothing parked → nothing to point the operator at


def test_preserve_attempt_commits_pauses_when_ref_fails_and_allowed(project, monkeypatch):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")

    def no_ref(*a, **k):
        raise GitError("branch creation failed")

    monkeypatch.setattr(verify, "preserve_commits", no_ref)

    with pytest.raises(_Pause):
        flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-preserve-failed" in flow.journal.events()
    # preserve_failed=True routes through the committed-work notice
    assert "could not be auto-preserved" in flow.calls.pauses[-1][0]
    # the ref never took — run state must not name one (#333)
    assert task.preserve_ref is None


def test_preserve_attempt_commits_no_pause_on_redrive(project, monkeypatch):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")

    monkeypatch.setattr(verify, "preserve_commits", lambda *a, **k: None)  # ref did not take

    flow.preserve_attempt_commits(task, allow_pause=False)  # re-drive: never pauses

    assert "attempt-preserve-failed" in flow.journal.events()
    assert flow.calls.pauses == []


def _commit_something(repo: Path) -> None:
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")


@pytest.mark.parametrize(
    "make_exc",
    # Factories, not instances: a parametrized exception *instance* is built once at
    # collection and shared by every case, and re-raising it mutates its
    # __traceback__ — which under pytest-randomly's shuffling made these cases
    # couple to each other and fail for the wrong reason.
    [lambda: GitError("git log timed out"), lambda: OSError(24, "Too many open files")],
    ids=["giterror", "oserror"],
)
def test_preserve_attempt_commits_pauses_when_range_unenumerable(project, monkeypatch, make_exc):
    # #343: `commits_above` carried no guard at all, so even a plain GitError (a
    # translated git timeout) crashed the rollback here. An un-determinable range
    # must refuse the reset, never fall through the `not commits` early return.
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    _commit_something(repo)

    def boom(*a, **k):
        raise make_exc()

    monkeypatch.setattr(verify, "commits_above", boom)

    with pytest.raises(_Pause):
        flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-preserve-enumerate-failed" in flow.journal.events()
    assert "could not be auto-preserved" in flow.calls.pauses[-1][0]
    assert task.preserve_ref is None  # nothing parked → nothing to point the operator at


def test_preserve_attempt_commits_pauses_when_head_read_fails(project, monkeypatch):
    # The pinned-tip read fails before range enumeration; uncertainty still means
    # there may be work above baseline that cannot safely be reset away.
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    _commit_something(repo)

    def boom(*a, **k):
        raise GitError("git rev-parse timed out")

    monkeypatch.setattr(verify, "rev_parse_head", boom)

    with pytest.raises(_Pause):
        flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-preserve-enumerate-failed" in flow.journal.events()
    assert task.preserve_ref is None


def test_preserve_attempt_commits_unenumerable_no_pause_on_redrive(project, monkeypatch):
    # The re-drive contract forbids pausing even here: a human directed the discard.
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    _commit_something(repo)

    def boom(*a, **k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(verify, "commits_above", boom)

    flow.preserve_attempt_commits(task, allow_pause=False)  # must not raise

    assert "attempt-preserve-enumerate-failed" in flow.journal.events()
    assert flow.calls.pauses == []


def test_preserve_attempt_commits_ref_oserror_treated_as_failure(project, monkeypatch):
    # Sibling of the GitError case above: the ref write can also fail untyped (#343).
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    _commit_something(repo)

    def boom(*a, **k):
        raise OSError(12, "Cannot allocate memory")

    monkeypatch.setattr(verify, "preserve_commits", boom)

    with pytest.raises(_Pause):
        flow.preserve_attempt_commits(task, allow_pause=True)

    assert "attempt-preserve-failed" in flow.journal.events()
    assert task.preserve_ref is None


def test_preserve_attempt_worktree_snapshots_dirty_tree(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    task.attempt = 2
    (repo / "src.txt").write_text("uncommitted edit\n")  # tracked file dirtied

    flow.preserve_attempt_worktree(task, allow_pause=False)

    assert "attempt-worktree-preserved" in flow.journal.events()
    ref = flow.journal.fields("attempt-worktree-preserved")["ref"]
    assert ref.startswith("refs/attempt-preserve-dirty/")
    git(repo, "rev-parse", "--verify", ref)  # the snapshot ref exists
    assert task.preserve_ref == ref  # #333


def test_dirty_preserve_ref_wins_and_subsumes_the_commits_branch(project):
    """Both families fire on one rollback: commits above baseline are parked on an
    `attempt-preserve/*` branch and the still-dirty tree on a dirty snapshot. The
    dirty ref is the one `preserve_ref` keeps, because it is committed parented at
    the attempt's HEAD and therefore already contains the branch — so the single
    `git merge --ff-only <preserve_ref>` the defer notice prints recovers the
    whole attempt, not half of it."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")
    (repo / "src.txt").write_text("committed then edited\n")  # uncommitted on top

    flow.rollback_or_pause(task)

    branch = flow.journal.fields("attempt-commits-preserved")["ref"]
    dirty = flow.journal.fields("attempt-worktree-preserved")["ref"]
    assert task.preserve_ref == dirty != branch
    # subsumption: the branch tip is an ancestor of the dirty snapshot
    git(repo, "merge-base", "--is-ancestor", branch, dirty)
    # Ablation target for the partial marker: hoist `preserve_partial = True` out of
    # preserve_attempt_worktree's `except` and this fails. A park that captured
    # everything must never be libelled as commits-only.
    assert task.preserve_partial is False


def _fail_snapshot(monkeypatch, exc=None):
    """Make verify.snapshot_worktree raise, the way a full disk or a git timeout
    inside `add -u`/`write-tree`/`update-ref` would."""

    def _fail(*_a, **_k):
        raise exc if exc is not None else verify.GitError("simulated commit-tree failure")

    monkeypatch.setattr(verify, "snapshot_worktree", _fail)


def test_changed_owned_snapshot_capture_failure_pauses_latched_redrive(project, monkeypatch):
    """Replacing child bytes stays forbidden until forced capture succeeds."""
    repo = project.project
    spec = _tracked_spec(project, name="forced-capture.md")
    task = _task(repo)
    task.dispatched_spec_file = str(spec.resolve())
    task.dispatched_spec_snapshot = spec.read_bytes()
    task.resolved_redrive = True
    child = b"---\nstatus: done\n---\n\nfailed child body\n"
    spec.write_bytes(child)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=False)
    )
    _fail_snapshot(monkeypatch)

    with pytest.raises(_Pause, match="could not be auto-preserved"):
        flow.rollback_or_pause(task)

    assert spec.read_bytes() == child
    assert "attempt-worktree-preserve-failed" in flow.journal.events()
    assert "post_rollback" not in flow.calls.emits


def test_snapshot_failure_leaves_a_commits_only_ref_flagged_partial(project, monkeypatch):
    """The snapshot raises *after* the commits branch was already parked, and on a
    re-drive the reset runs anyway. `preserve_ref` then names the commits branch
    alone, so the whole-attempt promise no longer holds — `preserve_partial` records
    that, and the defer notice downgrades its claim instead of telling the operator
    a `merge --ff-only` restores work the reset just destroyed.

    Since #340 a *plain* rollback refuses this reset, so the re-drive
    (`cause="resolved"`, contractually pause-free) is the surviving path where the
    partial park is reachable — which is exactly why #338's downgrade is narrowed
    rather than superseded. `rollback_on_failure` is left OFF so the re-drive is
    what carries the auto-recover arm here, not the policy flag.

    Ablation target: delete `task.preserve_partial = True` from
    preserve_attempt_worktree's `except verify.GitError` block and this fails."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")
    (repo / "src.txt").write_text("committed then edited\n")  # the half that will be lost

    _fail_snapshot(monkeypatch)

    flow.rollback_or_pause(task, cause="resolved")

    assert flow.calls.pauses == []  # a re-drive never pauses, even on a preserve failure
    assert "attempt-worktree-preserve-failed" in flow.journal.events()
    assert task.preserve_ref == flow.journal.fields("attempt-commits-preserved")["ref"]
    assert task.preserve_ref.startswith("attempt-preserve/")
    assert task.preserve_partial is True
    # why it matters: the reset ran regardless, so the tree is back at baseline and
    # the ref the notice names carries the committed half ONLY — the uncommitted
    # edit survives nowhere, which is exactly what the un-downgraded notice's
    # `merge --ff-only` would have implied it could restore
    assert "committed" not in (repo / "src.txt").read_text()
    assert git(repo, "show", f"{task.preserve_ref}:src.txt") == "committed"


def test_snapshot_failure_pauses_when_work_would_be_lost(project, monkeypatch):
    """#340: a failed dirty snapshot refuses the reset instead of destroying the
    uncommitted work it existed to capture. Unlike the commits path's orphaned
    objects, a tracked edit a `reset --hard` discards is unrecoverable, so the
    safety net becomes a gate.

    Ablation target: delete the `pause_for_manual_recovery(..., snapshot_failed=True)`
    call from preserve_attempt_worktree's `except` and this fails."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    (repo / "new.txt").write_text("run-created untracked\n")
    _fail_snapshot(monkeypatch)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert "attempt-worktree-preserve-failed" in flow.journal.events()
    # the whole point: the tree is untouched, so the work is still there to rescue
    assert (repo / "src.txt").read_text() == "uncommitted work\n"
    assert (repo / "new.txt").is_file()
    notice = flow.calls.pauses[0][0]
    assert "uncommitted work could not be auto-preserved" in notice
    assert "attempt-worktree-preserve-failed" in notice  # names the diagnosis breadcrumb
    # rescue first, discard second — and step 3 is what lets the pause terminate
    # (reset, resume, rollback-skipped-clean). Anchor on the command, not the bare
    # phrase: the prose above it also says `reset --hard`, so index() would match there.
    assert notice.index("Save what you want to keep") < notice.index(
        f'git -C "{repo}" reset --hard'
    )


def test_snapshot_failure_proceeds_when_nothing_would_be_lost(project, monkeypatch):
    """The refusal is gated on there being something left to lose. An attempt that
    committed everything has a tree clean vs HEAD, so the reset destroys nothing and
    a snapshot fault must not halt an unattended run over it (cf. #123's false-pause
    complaint).

    INVERSE ablation — `pauses == []` would also pass if the pause were simply
    unreachable. Make the pause unconditional (drop the `_reset_would_destroy`
    guard) and this must fail."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt commit")  # nothing left uncommitted
    _fail_snapshot(monkeypatch)

    flow.rollback_or_pause(task)

    assert flow.calls.pauses == []
    assert "attempt-worktree-preserve-failed" in flow.journal.events()
    assert rev_parse_head(repo) == task.baseline_commit  # the harmless reset ran
    # and the committed half is still recoverable by name
    assert git(repo, "show", f"{task.preserve_ref}:src.txt") == "committed"


def test_snapshot_failure_probe_fault_pauses(project, monkeypatch):
    """A git fault in the at-risk probe itself must read as work-at-risk, never as
    permission to reset — the same fail-safe direction as the dirty check (#156).

    Ablation target: flip `_reset_would_destroy`'s `except verify.GitError` to
    `return False` and this fails."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    _fail_snapshot(monkeypatch)

    def _fail_probe(*_a, **_k):
        raise verify.GitError("simulated rev-parse failure")

    monkeypatch.setattr(verify, "rev_parse_head", _fail_probe)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert (repo / "src.txt").read_text() == "uncommitted work\n"  # not reset


def test_probe_oserror_also_fails_safe(project, monkeypatch):
    """The probe runs immediately after a snapshot fault, against the same git
    binary, so the EMFILE/ENOMEM that broke the capture is likely to break the probe
    too. Catching only GitError here would undo the broadening one frame up and
    crash the rollback anyway — the asymmetry, not either half, is the defect.

    Ablation target: narrow `_reset_would_destroy`'s `except` back to
    `verify.GitError` and this fails with the raw OSError."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    _fail_snapshot(monkeypatch, OSError(24, "Too many open files"))

    def _fail_probe(*_a, **_k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(verify, "rev_parse_head", _fail_probe)

    with pytest.raises(_Pause):  # the typed pause, not the OSError
        flow.rollback_or_pause(task)

    assert (repo / "src.txt").read_text() == "uncommitted work\n"  # not reset


def test_snapshot_failure_never_pauses_on_redrive(project, monkeypatch):
    """The re-drive's pause-free contract outranks the #340 gate: the operator
    already directed this discard through the resolve workflow, so a failed capture
    journals and lets the reset run.

    Ablation target: delete `if not allow_pause: return` from the `except` and this
    fails."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    _fail_snapshot(monkeypatch)

    flow.rollback_or_pause(task, cause="resolved")

    assert flow.calls.pauses == []
    assert task.preserve_partial is True  # still latched — the notice must downgrade
    assert (repo / "src.txt").read_text() == "original\n"  # reset ran


def test_snapshot_oserror_degrades_into_the_typed_path(project, monkeypatch):
    """`snapshot_worktree` can raise a plain OSError outright — ENOSPC/EMFILE from
    its TemporaryDirectory — a filesystem fault the #343 spawn translation cannot
    cover. Preservation is observation, not a repair write, so it degrades into
    the same journal-and-decide path a GitError takes rather than crashing the
    run mid-rollback.

    Ablation target: narrow the `except` back to `verify.GitError` and this fails
    with the raw OSError."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    _fail_snapshot(monkeypatch, OSError(24, "Too many open files"))

    with pytest.raises(_Pause):  # the typed pause, not the OSError
        flow.rollback_or_pause(task)

    entry = flow.journal.fields("attempt-worktree-preserve-failed")
    assert "Too many open files" in entry["error"]  # errno detail kept as a breadcrumb
    assert (repo / "src.txt").read_text() == "uncommitted work\n"


def test_ref_probe_git_fault_degrades_like_a_failed_snapshot(project, monkeypatch):
    """The free-refname probe spawns git before the snapshot does, so it is the
    first place a spawn/timeout fault can surface. `ref_exists` deliberately does
    not swallow those (mistaking "git could not run" for "the name is free" would
    overwrite the very snapshot the probe exists to protect), so the probe has to
    sit inside the handler that turns a preservation fault into a pause.

    Ablation target: move the probe loop back above the `try` and this fails with
    the raw GitSpawnError instead of the typed pause."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")

    def _fail(*_a, **_k):
        raise verify.GitSpawnError("git: command not found")

    monkeypatch.setattr(verify, "ref_exists", _fail)

    with pytest.raises(_Pause):  # the typed pause, not the GitSpawnError
        flow.rollback_or_pause(task)

    entry = flow.journal.fields("attempt-worktree-preserve-failed")
    assert "command not found" in entry["error"]
    assert (repo / "src.txt").read_text() == "uncommitted work\n"  # work not reset away


def test_ref_probe_is_bounded_and_refuses_to_reuse_an_occupied_name(project, monkeypatch):
    """The probe terminates on its own — the ref set is finite and the serial only
    climbs — but terminating is not the same as being bounded: without a cap the
    iteration count is whatever the namespace happens to hold, one git spawn each,
    inside a crash-recovery path. `PRESERVE_REF_PROBE_LIMIT` bounds the scan, and
    exhausting it must RAISE rather than fall through to the last candidate;
    reusing an occupied name is the exact data loss the probe exists to prevent
    (#349). Exhaustion is a preservation fault like any other, so it degrades into
    the typed pause instead of crashing the rollback.

    `ref_exists` is answered True for a few calls PAST the limit, so removing the
    cap makes this fail on the missing pause rather than hanging the suite.

    Ablation target: delete the `serial > PRESERVE_REF_PROBE_LIMIT` raise and the
    probe walks past the bound to a free name, the snapshot succeeds, and the
    `pytest.raises(_Pause)` fails."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")

    probed: list[str] = []

    def _occupied(_repo, refname: str) -> bool:
        probed.append(refname)
        return len(probed) <= PRESERVE_REF_PROBE_LIMIT + 5

    snapshots: list[str] = []

    def _snapshot(_repo, refname, **_k):
        snapshots.append(refname)
        return refname

    monkeypatch.setattr(verify, "ref_exists", _occupied)
    monkeypatch.setattr(verify, "snapshot_worktree", _snapshot)

    with pytest.raises(_Pause):  # the typed pause, not a raw error and not a hang
        flow.rollback_or_pause(task)

    # The bound is the assertion: the base name plus -r2..-r<limit>, then stop.
    assert len(probed) == PRESERVE_REF_PROBE_LIMIT
    assert probed[-1].endswith(f"-r{PRESERVE_REF_PROBE_LIMIT}")
    assert not snapshots  # never wrote over any of the names it found occupied

    entry = flow.journal.fields("attempt-worktree-preserve-failed")
    assert "no free snapshot refname" in entry["error"]  # names the exhaustion
    assert "scm.preserve_keep" in entry["error"]  # and the operator's remedy
    assert (repo / "src.txt").read_text() == "uncommitted work\n"  # work not reset away


def test_notice_probe_oserror_does_not_swallow_the_pause(project, monkeypatch):
    """`pause_for_manual_recovery`'s advisory `commits_above` probe runs while the
    fault that broke the snapshot is still in force, so it is the likeliest place
    for a second EMFILE. Its own comment says a git fault there must not block the
    pause — an uncaught OSError did exactly that, losing a pause the caller had
    already decided to take.

    Ablation target: narrow that probe's `except` back to `verify.GitError` and this
    fails with the raw OSError instead of pausing."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("uncommitted work\n")
    _fail_snapshot(monkeypatch, OSError(24, "Too many open files"))

    # Only the *notice's* probe may fail: preserve_attempt_commits calls
    # commits_above first, and breaking that would abort the rollback earlier and
    # never reach the code under test.
    real_commits_above = verify.commits_above
    seen = {"n": 0}

    def _flaky(*a, **k):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_commits_above(*a, **k)
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(verify, "commits_above", _flaky)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    assert seen["n"] >= 2  # the advisory probe really was reached and really failed
    notice = flow.calls.pauses[0][0]
    assert "uncommitted work could not be auto-preserved" in notice  # shape (d) intact
    assert (repo / "src.txt").read_text() == "uncommitted work\n"  # not reset


def test_snapshot_failure_pause_names_the_unit_worktree(project, tmp_path, monkeypatch):
    """#161 compatibility: a preserve-failure pause can fire while a unit worktree is
    mounted, and every instruction must target that tree. Naming the main checkout
    there quotes a HEAD the attempt never moved and invites a destructive reset of a
    tree the operator never worked in.

    Ablation target: change `pause_for_manual_recovery`'s `root` back to
    `self.paths.repo_root` and this fails."""
    repo = project.project
    ws = Workspace.default(project)
    main_checkout = tmp_path / "some-other-main-checkout"
    flow = _make_flow(
        workspace=ws,
        paths=SimpleNamespace(repo_root=main_checkout),
        policy=_policy(rollback_on_failure=False),  # in-worktree recovery ignores it
    )
    task = _task(repo)
    (repo / "src.txt").write_text("worktree attempt\n")
    _fail_snapshot(monkeypatch)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task)

    notice = flow.calls.pauses[0][0]
    assert str(repo) in notice
    assert str(main_checkout) not in notice


def test_rollback_clears_a_previous_attempts_preserve_ref(project, monkeypatch):
    """Ablation target: delete the `task.preserve_ref = None` at the top of
    RecoveryFlow's auto-recover arm and this fails. A later rollback that parks
    nothing — here the ref simply fails to take on a pause-free re-drive — must not
    leave the *earlier* attempt's ref standing, or the defer notice sends the
    operator to work that is not the deferred attempt's."""
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(rollback_on_failure=True))
    task = _task(repo)
    (repo / "src.txt").write_text("attempt 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt 1")

    flow.rollback_or_pause(task)
    stale = task.preserve_ref
    assert stale and stale.startswith("attempt-preserve/")

    task.attempt = 1
    (repo / "src.txt").write_text("attempt 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt 2")
    monkeypatch.setattr(verify, "preserve_commits", lambda *a, **k: None)  # ref did not take

    flow.rollback_or_pause(task, cause="resolved")  # re-drive: never pauses

    assert "attempt-preserve-failed" in flow.journal.events()
    assert task.preserve_ref is None


# --------------------------------------------------------------- safe_reset


def test_safe_reset_reverts_tracked_and_keeps_baseline(project):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    (repo / "src.txt").write_text("committed then to be reset\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt")

    flow.safe_reset(task)

    assert rev_parse_head(repo) == task.baseline_commit


def test_safe_reset_preflight_failure_journals_and_pauses_redrive(project, monkeypatch):
    """A typed cleanup-preflight refusal is journaled and passed to the injected
    pause as its exact cause; the resolved re-drive stops before post-rollback or
    any destructive reset can run.

    Ablation target: delete the `except RollbackPreflightError` journal/pause block
    and this test fails on the uncaught typed error; delete only `_pause` and it
    fails because `post_rollback` continues and the expected pause is absent.
    """
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(repo)
    created = repo / "uncertain" / "created.txt"
    created.parent.mkdir()
    created.write_text("run-created\n")
    (repo / "src.txt").write_text("tracked attempt\n")
    refuse_to_resolve(monkeypatch, created)
    monkeypatch.setattr(flow, "preserve_attempt_commits", lambda *args, **kwargs: None)
    monkeypatch.setattr(flow, "preserve_attempt_worktree", lambda *args, **kwargs: None)

    with pytest.raises(_Pause):
        flow.rollback_or_pause(task, cause="resolved")

    failure = flow.journal.fields("rollback-reset-failed")
    assert "preflight rollback cleanup" in failure["error"]
    assert len(flow.calls.pauses) == 1
    reason, story_key, cause = flow.calls.pauses[0]
    assert story_key == task.story_key
    assert isinstance(cause, verify.RollbackPreflightError)
    assert cause is not None and cause.__cause__ is not None
    assert str(cause) in reason
    assert flow.calls.emits == ["pre_rollback"]  # no post-reset re-drive continuation
    assert (repo / "src.txt").read_text() == "tracked attempt\n"
    assert created.read_text() == "run-created\n"


# --------------------------------------------------------------- prune


def test_prune_preserve_refs_disabled_when_keep_zero(project):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(preserve_keep=0))
    flow.prune_preserve_refs()
    assert flow.journal.events() == []


def test_prune_preserve_refs_journals_deleted_per_family(project, monkeypatch):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(preserve_keep=3))
    monkeypatch.setattr(verify, "prune_preserve_refs", lambda repo, keep: ["a", "b"])
    monkeypatch.setattr(verify, "prune_preserve_dirty_refs", lambda repo, keep: [])

    flow.prune_preserve_refs()

    assert flow.journal.fields("attempt-preserve-pruned")["count"] == 2
    # empty deletion for the other family journals nothing
    assert "attempt-preserve-dirty-pruned" not in flow.journal.events()


def test_prune_preserve_refs_error_journaled_and_other_family_still_runs(project, monkeypatch):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, policy=_policy(preserve_keep=3))

    def stuck(repo, keep):
        exc = RuntimeError("update-ref stuck")
        exc.deleted = ["r1"]  # a partial prune already deleted this before stalling
        exc.failed = ["r2"]
        raise exc

    monkeypatch.setattr(verify, "prune_preserve_refs", stuck)
    monkeypatch.setattr(verify, "prune_preserve_dirty_refs", lambda repo, keep: ["d1"])

    flow.prune_preserve_refs()  # a failure in one family must never crash or skip the other

    events = flow.journal.events()
    assert "attempt-preserve-pruned" in events  # partial deletions stay auditable
    assert flow.journal.fields("attempt-preserve-prune-failed")["failed"] == ["r2"]
    assert "attempt-preserve-dirty-pruned" in events  # the second family still ran


# --------------------------------------------------------------- manual recovery


def test_pause_stopped_wording_no_commits(project, tmp_path):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, run_dir=tmp_path)
    task = _task(project.project)

    with pytest.raises(_Pause) as excinfo:
        flow.pause_for_manual_recovery(task, task.baseline_commit)

    reason = excinfo.value.reason
    assert "failed" not in reason
    assert "manual rollback needed" in reason
    assert flow.calls.saves == 1
    assert "rollback-manual-required" in flow.journal.events()
    # notify wrote a line to the run dir's attention file (QUIET file=True)
    assert "manual rollback for 1-1-a" in (tmp_path / ATTENTION_FILE).read_text()


def test_pause_committed_wording_names_at_risk_commits(project, tmp_path):
    repo = project.project
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, run_dir=tmp_path)
    task = _task(repo)
    (repo / "src.txt").write_text("committed work above baseline\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "finished but unfolded")

    with pytest.raises(_Pause) as excinfo:
        flow.pause_for_manual_recovery(task, task.baseline_commit)

    reason = excinfo.value.reason
    assert "committed work above its baseline" in reason
    assert "do NOT reset before checking" in reason


def test_pause_preserve_failed_wording(project, tmp_path):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws, run_dir=tmp_path)
    task = _task(project.project)

    with pytest.raises(_Pause) as excinfo:
        flow.pause_for_manual_recovery(task, task.baseline_commit, preserve_failed=True)

    assert "could not be auto-preserved" in excinfo.value.reason


# --------------------------------------------------------------- restore_patch


def test_restore_patch_noop_without_latch(project):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(project.project)  # restore_patch is None by default

    flow.restore_patch(task)

    assert flow.journal.events() == []
    assert flow.calls.escalates == []


def test_restore_patch_applies_and_journals(project, monkeypatch):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(project.project)
    task.restore_patch = "patch.diff"
    applied = []
    monkeypatch.setattr(verify, "resolve_restore_path", lambda raw, root: Path(root) / raw)
    monkeypatch.setattr(verify, "apply_patch", lambda repo, patch: applied.append(patch))

    flow.restore_patch(task)

    assert applied  # the patch was applied
    assert "attempt-restored" in flow.journal.events()
    assert flow.calls.escalates == []


def test_restore_patch_escalates_on_apply_failure(project, monkeypatch):
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(project.project)
    task.restore_patch = "patch.diff"
    task.phase = Phase.DEV_RUNNING  # the call-site invariant restore_patch relies on
    monkeypatch.setattr(verify, "resolve_restore_path", lambda raw, root: Path(root) / raw)

    def boom(repo, patch):
        raise GitError("does not apply")

    monkeypatch.setattr(verify, "apply_patch", boom)

    with pytest.raises(_Pause):
        flow.restore_patch(task)

    assert task.phase == Phase.DEV_VERIFY  # stepped to the escalatable phase first
    assert flow.calls.escalates  # routed through the engine's escalation
    assert "attempt-restore-failed" in flow.journal.events()
    assert "attempt-restored" not in flow.journal.events()  # never reached on failure


def test_restore_patch_escalates_on_apply_oserror(project, monkeypatch):
    # #343: the patch is read from disk, so an ENOENT/EACCES arrives untyped and
    # must still escalate rather than crash past the escalation.
    ws = Workspace.default(project)
    flow = _make_flow(workspace=ws)
    task = _task(project.project)
    task.restore_patch = "patch.diff"
    task.phase = Phase.DEV_RUNNING
    monkeypatch.setattr(verify, "resolve_restore_path", lambda raw, root: Path(root) / raw)

    def boom(repo, patch):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(verify, "apply_patch", boom)

    with pytest.raises(_Pause):
        flow.restore_patch(task)

    assert task.phase == Phase.DEV_VERIFY
    assert flow.calls.escalates
    assert "attempt-restore-failed" in flow.journal.events()


# ---------------------------------------------------------------------------
# retry dev prompt: the pointer at an earlier attempt's parked work (#777)


def _dirty_rollback(flow: RecoveryFlow, task: StoryTask, repo: Path, text: str) -> str:
    """Leave one tracked edit on the tree and roll it back; return the snapshot ref."""
    (repo / "src.txt").write_text(text)
    flow.rollback_or_pause(task)
    assert git(repo, "status", "--porcelain") == ""
    return flow.journal.entries[-1][1]["ref"]


def test_retry_preserve_notice_names_a_verified_worktree_snapshot(project):
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    ref = _dirty_rollback(flow, task, repo, "attempt 1 edit\n")

    assert ref.startswith("refs/attempt-preserve-dirty/run-1-") and task.preserve_ref == ref
    notice = flow.retry_preserve_notice(task)
    base = task.baseline_commit
    assert notice.startswith("An earlier attempt at this work was rolled back; its work is ")
    assert f"preserved at `{ref}`" in notice
    assert f"`git log --oneline {base}..{ref}`" in notice
    assert f"`git diff {base} {ref}`" in notice
    assert "unverified and has not been applied to this working tree" in notice
    assert "every gate must pass fresh on this attempt" in notice
    # informational only: never a replay instruction, never a provenance overclaim
    for word in ("cherry-pick", "merge", "previous attempt", "only its commits"):
        assert word not in notice
    # the offered command really shows the parked attempt against this baseline
    assert "attempt 1 edit" in git(repo, "diff", base, ref)


def test_retry_preserve_notice_absent_without_a_preserved_ref(project):
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    flow.rollback_or_pause(task)  # nothing to park: the attempt left no trace

    assert flow.journal.events() == ["rollback-skipped-clean"]
    assert task.preserve_ref is None
    assert flow.retry_preserve_notice(task) == ""


def test_retry_preserve_notice_labels_commits_only_preservation(project, monkeypatch):
    """The worktree snapshot failed, so `preserve_ref` names the commits branch
    alone (`preserve_partial`). The notice must narrow its claim to the commits.

    Ablation: drop the `preserve_partial` branch of the wording and this reddens
    on both the label and the absent whole-attempt claim."""
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    (repo / "src.txt").write_text("committed attempt work\n")
    git(repo, "commit", "-qam", "attempt commit")
    head = rev_parse_head(repo)

    def snapshot_fails(*args, **kwargs):
        raise GitError("commit-tree failed")

    monkeypatch.setattr(verify, "snapshot_worktree", snapshot_fails)
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )

    flow.rollback_or_pause(task)

    assert rev_parse_head(repo) == task.baseline_commit
    assert task.preserve_partial is True
    assert task.preserve_ref == recovery_flow.attempt_preserve_ref_name("run-1", head)
    notice = flow.retry_preserve_notice(task)
    qualified = f"refs/heads/{task.preserve_ref}"
    assert (
        f"only its commits were preserved, at `{qualified}` — its uncommitted "
        "changes were not captured there" in notice
    )
    assert "its work is preserved" not in notice
    assert f"`git diff {task.baseline_commit} {qualified}`" in notice


def test_retry_preserve_notice_clean_rollback_keeps_the_earlier_attempts_ref(project):
    """A later attempt that leaves a clean tree parks nothing, so its rollback
    keeps the older attempt's ref — the notice must call that work an EARLIER
    attempt's, never the previous one's."""
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    first = _dirty_rollback(flow, task, repo, "attempt 1 edit\n")

    task.attempt = 2
    flow.rollback_or_pause(task)  # attempt 2 left the tree clean

    assert flow.journal.events()[-1] == "rollback-skipped-clean"
    assert task.preserve_ref == first
    notice = flow.retry_preserve_notice(task)
    assert notice.startswith("An earlier attempt at this work was rolled back")
    assert f"`{first}`" in notice
    assert "previous attempt" not in notice


def test_retry_preserve_notice_follows_the_ref_a_later_rollback_parks(project):
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    first = _dirty_rollback(flow, task, repo, "attempt 1 edit\n")

    task.attempt = 2
    second = _dirty_rollback(flow, task, repo, "attempt 2 edit\n")

    assert second != first and task.preserve_ref == second
    notice = flow.retry_preserve_notice(task)
    assert f"`{second}`" in notice
    assert first not in notice.replace(second, "")  # the replaced ref is not offered


def test_retry_preserve_notice_withheld_when_no_dev_session_produced_the_ref(project):
    """A rollback with no dispatched dev session for the current attempt (a
    resolve re-drive's reset) still parks the tree — but nothing proves an
    attempt wrote it, so the notice must not claim one did. The rollback replaces
    the earlier, attributable ref's provenance along with the ref."""
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    _dirty_rollback(flow, task, repo, "attempt 1 edit\n")
    assert flow.retry_preserve_notice(task)

    unattributed = _make_flow(
        workspace=Workspace.default(project),
        policy=_policy(rollback_on_failure=True),
        dev_attempt_dispatched=False,
    )
    ref = _dirty_rollback(unattributed, task, repo, "re-drive residue\n")

    assert task.preserve_ref == ref and task.preserve_from_attempt is False
    assert unattributed.retry_preserve_notice(task) == ""
    assert git(repo, "rev-parse", "--verify", ref)  # suppressed, not deleted


def test_retry_preserve_notice_omitted_when_retention_pruned_the_ref(project):
    """Run-start retention (`scm.preserve_keep`) can delete the task's ref before
    a resume re-dispatches it. The stale name must yield no notice and no crash,
    and the task record keeps the name — nothing is cleared to hide guidance.

    Ablation: skip the resolve/ancestry probes and this reddens by offering a
    `git diff` against a ref that no longer exists."""
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project),
        policy=_policy(rollback_on_failure=True, preserve_keep=1),
    )
    ref = _dirty_rollback(flow, task, repo, "attempt 1 edit\n")
    # a newer snapshot (another story's rollback) outranks it under keep=1
    newer = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", "-p", "HEAD", "-m", "newer", "HEAD^{tree}"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "@4102444800 +0000"},
    ).stdout.strip()
    git(repo, "update-ref", "refs/attempt-preserve-dirty/run-1-other-1", newer)

    flow.prune_preserve_refs()

    assert "attempt-preserve-dirty-pruned" in flow.journal.events()
    assert not verify.ref_exists(repo, ref)
    assert flow.retry_preserve_notice(task) == ""
    assert task.preserve_ref == ref


@pytest.mark.parametrize("stale", ["baseline-moved", "foreign-run", "tip-moved"])
def test_retry_preserve_notice_refuses_a_ref_this_task_cannot_own(project, stale):
    """The ref must be this run's, on this task's baseline, and still where the
    rollback parked it; otherwise `git diff <baseline> <ref>` would show something
    other than this task's earlier attempt."""
    repo = project.project
    task = _task(repo)
    task.attempt = 1
    flow = _make_flow(
        workspace=Workspace.default(project), policy=_policy(rollback_on_failure=True)
    )
    if stale == "tip-moved":
        (repo / "src.txt").write_text("committed attempt work\n")
        git(repo, "commit", "-qam", "attempt commit")
        flow.rollback_or_pause(task)
        assert task.preserve_ref and task.preserve_ref.startswith("attempt-preserve/")
        assert flow.retry_preserve_notice(task)
        git(repo, "branch", "-f", task.preserve_ref, task.baseline_commit)
    else:
        _dirty_rollback(flow, task, repo, "attempt 1 edit\n")
        assert flow.retry_preserve_notice(task)
        if stale == "baseline-moved":
            # another unit merged first: the re-stamped baseline is past the work
            (repo / "other.txt").write_text("someone else's merge\n")
            git(repo, "add", "other.txt")
            git(repo, "commit", "-qm", "unrelated merge")
            task.baseline_commit = rev_parse_head(repo)
        else:
            flow.state = SimpleNamespace(run_id="run-2")

    assert flow.retry_preserve_notice(task) == ""
