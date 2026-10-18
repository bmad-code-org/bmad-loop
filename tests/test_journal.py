"""State persistence: the atomic write must survive the transient Windows
sharing violation (WinError 5) a concurrent TUI reader triggers. The retry
lives in platform_util.atomic_replace (unit-tested there) and in
platform_util.replace_at's win32 branch; the tests below prove save_state rides
each of them end to end, on the arm that reaches it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
from contextlib import contextmanager

import pytest

from bmad_loop import journal as journal_mod
from bmad_loop import platform_util, runs
from bmad_loop.journal import Journal, load_state, save_state, state_lock
from bmad_loop.model import RunState


def _force_path_fallback(monkeypatch):
    """Take save_state's path-based fallback: no handle-anchored arm on this host."""
    monkeypatch.setattr(journal_mod, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)


def test_save_state_retries_transient_sharing_violation(tmp_path, monkeypatch):
    """On win32, os.replace denied by a concurrent reader is retried, not fatal —
    on the path-based fallback arm, through `atomic_replace`."""
    _force_path_fallback(monkeypatch)
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)  # no real backoff
    monkeypatch.setattr(
        journal_mod, "file_lock", contextmanager(lambda _path, **_kw: iter((None,)))
    )

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:  # first two collide, third lands
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", flaky_replace)

    save_state(tmp_path, RunState(run_id="r1", project="p", started_at="2026-07-06T21:00:00"))

    assert calls["n"] == 3
    assert load_state(tmp_path).run_id == "r1"


@pytest.mark.skipif(not platform_util.HANDLE_ANCHORED_WRITES, reason="handle-anchored writes")
def test_save_state_anchored_win32_arm_retries_through_replace_at(tmp_path, monkeypatch):
    """The anchored arm's replace is `platform_util.replace_at`, whose win32 branch
    retries the same sharing violation around `win32_at.replace_at`. The branch is
    taken for the replace alone (`DIR_FD_ANCHORED_WRITES` False inside it), so the
    descriptor walk before it stays this host's own; on Windows that is a no-op.

    Ablation: call `win32_at.replace_at` bare in `replace_at` (drop the
    `_retry_on_sharing_violation` wrapper) and this fails on the first
    `PermissionError`."""
    real_dir_fd = platform_util.DIR_FD_ANCHORED_WRITES
    real_win32_replace_at = platform_util.win32_at.replace_at
    real_replace_at = platform_util.replace_at
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)  # no real backoff
    calls = {"n": 0}

    def flaky_replace_at(src_dir_fd, src, dst_dir_fd, dst):
        calls["n"] += 1
        if calls["n"] < 3:  # first two collide, third lands
            raise PermissionError(5, "Access is denied")
        if real_dir_fd:
            os.replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        else:
            real_win32_replace_at(src_dir_fd, src, dst_dir_fd, dst)

    def win32_branch_replace_at(*args):
        with monkeypatch.context() as m:
            m.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
            m.setattr(platform_util.sys, "platform", "win32")
            real_replace_at(*args)

    monkeypatch.setattr(platform_util.win32_at, "replace_at", flaky_replace_at)
    monkeypatch.setattr(platform_util, "replace_at", win32_branch_replace_at)

    save_state(tmp_path, _state(_minted(tmp_path)))

    assert calls["n"] == 3
    assert load_state(tmp_path).run_id == "r1"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_state_lock_holds_the_canonical_run_sidecar(tmp_path):
    run_dir = tmp_path / "run"
    lock_path = runs.lock_path_for(run_dir / journal_mod.STATE_FILE, follow_final_symlink=False)

    with state_lock(run_dir):
        with pytest.raises(OSError):
            with platform_util.file_lock(lock_path, blocking=False):
                pytest.fail("a rival acquired the held run-state sidecar")


def test_state_lock_same_run_nesting_acquires_os_lock_once(tmp_path, monkeypatch):
    acquired: list[object] = []

    @contextmanager
    def recording_lock(path, **_kw):
        acquired.append(path)
        yield

    monkeypatch.setattr(journal_mod, "file_lock", recording_lock)

    with state_lock(tmp_path):
        with state_lock(tmp_path / "."):
            save_state(
                tmp_path,
                RunState(run_id="r1", project="p", started_at="2026-09-01T00:00:00"),
            )

    assert acquired == [
        runs.lock_path_for(tmp_path / journal_mod.STATE_FILE, follow_final_symlink=False)
    ]


def test_state_lock_same_run_symlink_spellings_acquire_os_lock_once(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    alias = tmp_path / "run-alias"
    try:
        alias.symlink_to(run_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as e:
        pytest.skip(f"directory symlinks unavailable: {e}")
    acquired: list[object] = []

    @contextmanager
    def recording_lock(path, **_kw):
        acquired.append(path)
        yield

    monkeypatch.setattr(journal_mod, "file_lock", recording_lock)

    with state_lock(run_dir):
        with state_lock(alias):
            pass

    assert acquired == [
        runs.lock_path_for(run_dir / journal_mod.STATE_FILE, follow_final_symlink=False)
    ]


def test_state_lock_identity_survives_replacing_a_final_state_symlink(tmp_path):
    """Ablation: follow the final state.json symlink in state_lock and nested
    save_state changes sidecars when atomic_replace replaces the link."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    state_path = run_dir / journal_mod.STATE_FILE
    state_path.symlink_to(elsewhere)
    logical_lock = runs.lock_path_for(state_path, follow_final_symlink=False)

    # The default remains referent-based for ledgers and every other caller.
    assert runs.lock_path_for(state_path) == runs.lock_path_for(elsewhere)
    assert runs.lock_path_for(state_path) != logical_lock

    with state_lock(run_dir):
        save_state(
            run_dir,
            RunState(run_id="r1", project="p", started_at="2026-09-01T00:00:00"),
        )
        assert not state_path.is_symlink()
        with state_lock(run_dir / "."):
            with pytest.raises(OSError):
                with platform_util.file_lock(logical_lock, blocking=False):
                    pytest.fail("a rival acquired the original logical sidecar")

    assert elsewhere.read_text(encoding="utf-8") == "{}"
    assert load_state(run_dir).run_id == "r1"


def test_state_lock_refuses_different_run_nesting_before_second_acquire(tmp_path, monkeypatch):
    acquired: list[object] = []

    @contextmanager
    def recording_lock(path, **_kw):
        acquired.append(path)
        yield

    monkeypatch.setattr(journal_mod, "file_lock", recording_lock)

    with state_lock(tmp_path / "one"):
        with pytest.raises(RuntimeError, match="different runs"):
            with state_lock(tmp_path / "two"):
                pytest.fail("cross-run nesting was allowed")

    assert len(acquired) == 1


def test_state_lock_failure_clears_thread_guard(tmp_path, monkeypatch):
    acquired: list[object] = []

    @contextmanager
    def recording_lock(path, **_kw):
        acquired.append(path)
        yield

    monkeypatch.setattr(journal_mod, "file_lock", recording_lock)

    with pytest.raises(ValueError, match="boom"):
        with state_lock(tmp_path / "one"):
            raise ValueError("boom")
    with state_lock(tmp_path / "two"):
        pass

    assert len(acquired) == 2


def test_save_state_acquisition_error_writes_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"

    @contextmanager
    def refusing_lock(_path, **_kw):
        raise OSError("lock unavailable")
        yield

    monkeypatch.setattr(journal_mod, "file_lock", refusing_lock)

    with pytest.raises(OSError, match="lock unavailable"):
        save_state(
            run_dir,
            RunState(run_id="r1", project="p", started_at="2026-09-01T00:00:00"),
        )

    assert not run_dir.exists()


def test_save_state_root_error_writes_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"

    def no_state_root(_path, **_kwargs):
        raise runs.StateRootError("no state root")

    monkeypatch.setattr(runs, "lock_path_for", no_state_root)

    with pytest.raises(runs.StateRootError, match="no state root"):
        save_state(
            run_dir,
            RunState(run_id="r1", project="p", started_at="2026-09-01T00:00:00"),
        )

    assert not run_dir.exists()


def test_two_concurrent_saves_are_serialised_by_the_state_lock(tmp_path, monkeypatch):
    """state_lock serialises two saves; both land whole. The second save waits on
    the lock while the first is paused inside its replace, and neither raises.

    Ablation: remove save_state's state_lock and the second replace enters while
    the first is paused, so `replace_threads` reads `["first", "second"]` at the
    checkpoint."""
    real_replace_at = platform_util.replace_at
    real_file_lock = journal_mod.file_lock
    first_entered = threading.Event()
    second_attempted = threading.Event()
    release_first = threading.Event()
    replace_threads: list[str] = []

    @contextmanager
    def observed_file_lock(path, **_kw):
        if threading.current_thread().name == "second":
            second_attempted.set()
        with real_file_lock(path, **_kw):
            yield

    def controlled_replace(src_dir_fd, src, dst_dir_fd, dst):
        replace_threads.append(threading.current_thread().name)
        if len(replace_threads) == 1:
            first_entered.set()
            assert release_first.wait(2)
        real_replace_at(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(platform_util, "replace_at", controlled_replace)
    monkeypatch.setattr(journal_mod, "file_lock", observed_file_lock)
    errors: list[BaseException] = []

    def writer(run_id: str) -> None:
        try:
            save_state(
                tmp_path,
                RunState(run_id=run_id, project="p", started_at="2026-09-01T00:00:00"),
            )
        except BaseException as e:
            errors.append(e)

    first = threading.Thread(target=writer, args=("first",), name="first")
    second = threading.Thread(target=writer, args=("second",), name="second")
    first.start()
    assert first_entered.wait(2)
    second.start()
    assert second_attempted.wait(2)
    assert replace_threads == ["first"]
    release_first.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert sorted(replace_threads) == ["first", "second"]
    assert load_state(tmp_path).run_id in {"first", "second"}


def _minted(run_dir):
    """The run dir's mint-time identity record, as the composers persist it (DW-446)."""
    return platform_util.root_identity_record(run_dir)


def _planted_verify_symlink(tmp_path):
    """A run dir whose `verify/` a session has already replaced with a link out."""
    run_dir, elsewhere = tmp_path / "run", tmp_path / "elsewhere"
    run_dir.mkdir()
    elsewhere.mkdir()
    (run_dir / "verify").symlink_to(elsewhere, target_is_directory=True)
    return Journal(run_dir), elsewhere


@pytest.mark.skipif(not journal_mod.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_write_verify_stream_refuses_a_symlinked_verify_directory(tmp_path):
    """A session that plants `verify/` as a link cannot redirect verifier output.

    Sessions are handed the run directory (`BMAD_LOOP_RUN_DIR`) and write their
    own result.json into it, so this is a writer that really can plant the link.
    `mkdir(parents=True, exist_ok=True)` ACCEPTS a symlink-to-directory — it
    re-raises only when `is_dir()` is false, and that follows links — and
    `follow_symlinks=False` covers the final component, never its parent. Without
    the confinement walk the write lands in `elsewhere/`, outside the run dir.

    The refusal is an OSError because that is the caller's existing degrade path:
    the journal record still lands, with a null pointer and `capture_error`.

    Ablation, measured, and the two guards OVERLAP — which is the part worth
    writing down. Dropping the `open_dir_confined` arm alone reddens this test on
    the *message* only, because the win32 `is_symlink()` fallback below still
    refuses; so that ablation proves the arm is reached, not that it prevents the
    escape. Removing BOTH guards is what proves the harm: each test then fails
    `DID NOT RAISE`, and the same planted link writes `v.stdout.log` into
    `elsewhere/` while `write_verify_stream` returns the pointer
    `verify/v.stdout.log` — the file is outside the run dir and the record claims
    it is inside.
    """
    journal, elsewhere = _planted_verify_symlink(tmp_path)

    with pytest.raises(OSError, match=r"unconfined verify directory"):
        journal.write_verify_stream(
            "v.stdout.log", "verifier output", run_dir_identity=_minted(journal.run_dir)
        )

    # the assertion that actually pins the fix: nothing escaped the run dir
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(not journal_mod.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_write_verify_stream_refuses_a_run_dir_swapped_after_the_pin(tmp_path, monkeypatch):
    """DW-338: the run dir itself is engine-minted, so the anchored write pins it.
    A run dir replaced by a link (to a tree that also carries `verify/`) after
    its identity was recorded, but before the root open, is refused — the
    confinement walk below the run dir alone would pass that swap.

    Ablation: drop `root_identity=` from the `open_dir_confined` call in
    `write_verify_stream` and this fails `DID NOT RAISE`, with `v.stdout.log`
    written into `elsewhere/verify/`."""
    run_dir, elsewhere = tmp_path / "run", tmp_path / "elsewhere"
    (run_dir / "verify").mkdir(parents=True)
    (elsewhere / "verify").mkdir(parents=True)
    journal = Journal(run_dir)
    record = _minted(run_dir)
    opener = journal_mod.open_dir_confined

    def swap_then_open(root, target, **kwargs):
        run_dir.rename(tmp_path / "run-aside")
        run_dir.symlink_to(elsewhere, target_is_directory=True)
        return opener(root, target, **kwargs)

    monkeypatch.setattr(journal_mod, "open_dir_confined", swap_then_open)
    with pytest.raises(OSError, match=r"unconfined verify directory"):
        journal.write_verify_stream("v.stdout.log", "verifier output", run_dir_identity=record)

    assert list((elsewhere / "verify").iterdir()) == []
    assert list((tmp_path / "run-aside" / "verify").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.skipif(not journal_mod.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_write_verify_stream_refuses_a_linked_run_dir(tmp_path):
    """A run dir that is already a link has no identity to record: the engine mints
    run dirs, so a linked one is refused by contract rather than walked.

    Ablation: fall back to an unpinned open when the record is None and this fails
    `DID NOT RAISE`, the stream landing in `real/verify/`."""
    real = tmp_path / "real"
    real.mkdir()
    run_dir = tmp_path / "run"
    run_dir.symlink_to(real, target_is_directory=True)

    assert _minted(run_dir) is None  # a link records nothing
    with pytest.raises(OSError, match=r"unconfined verify directory"):
        Journal(run_dir).write_verify_stream(
            "v.stdout.log", "verifier output", run_dir_identity=_minted(run_dir)
        )

    assert not (real / "verify").exists()  # not even the directory


@pytest.mark.skipif(not journal_mod.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_write_verify_stream_refuses_a_symlinked_verify_directory_on_the_win32_path(
    tmp_path, monkeypatch
):
    """win32 has no *at() family, so it keeps a check-then-write — which must
    still refuse the planted link rather than fall through to the write.

    Ablation: delete the `is_link_like(verify_dir)` guard and this fails
    `DID NOT RAISE`, with the file landing in `elsewhere/` exactly as the
    unguarded POSIX path did.
    """
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    journal, elsewhere = _planted_verify_symlink(tmp_path)

    with pytest.raises(OSError, match=r"redirected verify directory"):
        journal.write_verify_stream(
            "v.stdout.log", "verifier output", run_dir_identity=_minted(journal.run_dir)
        )

    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("guard", ["record-pin", "link-check"])
def test_write_verify_stream_refuses_a_linked_run_dir_on_the_win32_path(
    tmp_path, monkeypatch, guard
):
    """DW-338 on the check-then-write arm: a linked run dir is refused there too,
    even when handed the identity of the directory the link leads to. Each guard
    is exercised on its own: ``record-pin`` is the DW-446 `require_root_pinned`
    compare (a link's `lstat` is not the recorded directory); ``link-check``
    neutralizes it so the `is_link_like(self.run_dir)` check alone must refuse.

    Ablation: drop the `require_root_pinned` calls and the ``record-pin`` row
    reddens (the refusal it gets is the link check's, not the pin's); drop the
    `is_link_like(self.run_dir)` checks and the ``link-check`` row fails `DID NOT
    RAISE`, the stream landing in `real/verify/`."""
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    real = tmp_path / "real"
    real.mkdir()
    run_dir = tmp_path / "run"
    run_dir.symlink_to(real, target_is_directory=True)
    if guard == "link-check":
        monkeypatch.setattr(journal_mod, "require_root_pinned", lambda root, identity: None)
    expected = r"no longer the directory" if guard == "record-pin" else r"redirected verify"

    with pytest.raises(OSError, match=expected):
        Journal(run_dir).write_verify_stream(
            "v.stdout.log", "verifier output", run_dir_identity=_minted(real)
        )

    assert not (real / "verify").exists()  # not even the directory


def test_write_verify_stream_refuses_a_zero_inode_run_dir_on_the_win32_path(tmp_path, monkeypatch):
    """DW-446, accepted 2026-09-27: a run dir whose `lstat` reports `st_ino` 0 (a
    win32 filesystem with no inode identity, DW-444's pattern) records no mint-time
    identity, and the win32 check-then-write arm now REFUSES it — before DW-446 it
    wrote there. A refusal, not a degrade: nothing is created, and the engine
    journals it as the verify record's `capture_error`.

    Ablation: revert the win32 arm's pin (drop its `require_root_pinned` calls, the
    pre-DW-446 fresh-`lstat`-only arm) and this fails `DID NOT RAISE`, the stream
    landing in `run/verify/`."""
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    real_lstat = os.lstat

    class _ZeroInode:
        def __init__(self, real):
            self._real = real
            self.st_ino = 0

        def __getattr__(self, name):
            return getattr(self._real, name)

    def lstat(path, *args, **kwargs):
        real = real_lstat(path, *args, **kwargs)
        return _ZeroInode(real) if str(path) == str(run_dir) else real

    monkeypatch.setattr(os, "lstat", lstat)
    record = platform_util.root_identity_record(run_dir)
    assert record is None  # the mint records nothing for a zero inode

    with pytest.raises(OSError, match=r"no longer the directory"):
        Journal(run_dir).write_verify_stream("v.stdout.log", "out", run_dir_identity=record)

    assert not (run_dir / "verify").exists()


def test_write_verify_stream_writes_an_ordinary_verify_directory(tmp_path):
    """The positive control: an unplanted run dir still retains its streams.

    Without this, both refusal tests above pass for a `write_verify_stream` that
    refuses everything unconditionally — a negative assertion is green for every
    reason a file could be absent.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = Journal(run_dir)

    pointer = journal.write_verify_stream(
        "v.stdout.log", "verifier output", run_dir_identity=_minted(run_dir)
    )

    assert pointer == "verify/v.stdout.log"
    assert (run_dir / pointer).read_text(encoding="utf-8") == "verifier output"
    # and again over the now-existing `verify/` (the mkdir's FileExistsError arm)
    journal.write_verify_stream("v.stderr.log", "more", run_dir_identity=_minted(run_dir))
    assert (run_dir / "verify" / "v.stderr.log").read_text(encoding="utf-8") == "more"


def test_write_verify_stream_writes_an_ordinary_verify_directory_on_the_win32_path(
    tmp_path, monkeypatch
):
    """The positive control for the check-then-write arm: an intact run dir whose
    record matches still retains its streams."""
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    pointer = Journal(run_dir).write_verify_stream(
        "v.stdout.log", "verifier output", run_dir_identity=_minted(run_dir)
    )

    assert (run_dir / pointer).read_text(encoding="utf-8") == "verifier output"


@pytest.mark.parametrize("arm", ["dir-fd", "win32-path"])
def test_write_verify_stream_refuses_a_missing_run_dir_record(tmp_path, monkeypatch, arm):
    """DW-446: a run dir with no mint-time record (a pre-upgrade state not yet
    backfilled) refuses — it never degrades to an unpinned open or a fresh `lstat`.

    Ablation: map a None record to `pinned_root_identity(self.run_dir)` (the
    pre-DW-446 per-write pin) and both arms fail `DID NOT RAISE`."""
    if arm == "dir-fd" and not journal_mod.DIR_FD_ANCHORED_WRITES:
        pytest.skip("dir-fd anchoring is POSIX-only")
    if arm == "win32-path":
        monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(OSError):
        Journal(run_dir).write_verify_stream("v.stdout.log", "out", run_dir_identity=None)

    assert not (run_dir / "verify").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("arm", ["dir-fd", "win32-path"])
def test_write_verify_stream_refuses_a_runs_dir_swapped_for_a_link_holding_a_real_run(
    tmp_path, monkeypatch, arm
):
    """DW-446: `runs/` swapped, after the mint, for a link to a tree holding a REAL
    `<id>/` refuses on both arms, and nothing is created or written outside.

    A fresh `lstat` refuses a link only at the final component: here the run dir
    itself is a real directory reached THROUGH the link, so the pre-DW-446 per-write
    pin accepted it and the stream landed in `outside/<id>/verify/`. The pin is now
    the identity recorded when the run dir was minted.

    Ablation: pin with `pinned_root_identity(self.run_dir)` instead of the record
    (the pre-DW-446 behaviour) and both arms fail `DID NOT RAISE`, with
    `outside/r1/verify/v.stdout.log` written."""
    if arm == "dir-fd" and not journal_mod.DIR_FD_ANCHORED_WRITES:
        pytest.skip("dir-fd anchoring is POSIX-only")
    if arm == "win32-path":
        monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    runs_dir = tmp_path / "project" / ".bmad-loop" / "runs"
    run_dir = runs_dir / "r1"
    run_dir.mkdir(parents=True)
    record = _minted(run_dir)
    journal = Journal(run_dir)
    outside = tmp_path / "outside"
    (outside / "r1").mkdir(parents=True)
    runs_dir.rename(runs_dir.with_name("runs-aside"))
    runs_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        journal.write_verify_stream("v.stdout.log", "verifier output", run_dir_identity=record)

    assert list((outside / "r1").iterdir()) == []  # not even `verify/`


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_write_verify_stream_win32_path_repins_the_run_dir_before_the_write(tmp_path, monkeypatch):
    """DW-446 on the check-then-write arm: `runs/` swapped for a link to a tree
    holding a real `<id>/verify/` AFTER the first pin (on the first `is_link_like`
    call, before the `mkdir`) is caught by the SECOND `require_root_pinned`, just
    before the write; nothing lands in the outside `verify/`. The run dir and
    `verify/` are real directories reached through the link, so the `is_link_like`
    checks alone pass the swap.

    Ablation: drop the second `require_root_pinned(self.run_dir, root_identity)`
    (between `verify_dir.mkdir` and the write) and this fails `DID NOT RAISE`, with
    `outside/r1/verify/v.stdout.log` written."""
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    runs_dir = tmp_path / "project" / ".bmad-loop" / "runs"
    run_dir = runs_dir / "r1"
    run_dir.mkdir(parents=True)
    record = _minted(run_dir)
    journal = Journal(run_dir)
    outside = tmp_path / "outside"
    (outside / "r1" / "verify").mkdir(parents=True)
    real_is_link_like = journal_mod.is_link_like
    swapped = False

    def swap_then_check(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            runs_dir.rename(runs_dir.with_name("runs-aside"))
            runs_dir.symlink_to(outside, target_is_directory=True)
        return real_is_link_like(path)

    monkeypatch.setattr(journal_mod, "is_link_like", swap_then_check)

    with pytest.raises(OSError, match=r"no longer the directory"):
        journal.write_verify_stream("v.stdout.log", "verifier output", run_dir_identity=record)

    assert swapped  # the premise: the swap happened after the first pin
    assert list((outside / "r1" / "verify").iterdir()) == []


class _ReparseStat:
    """os.lstat() of a Windows junction: a DIRECTORY mode — which is why
    Path.is_symlink() answers False — carrying a reparse tag."""

    st_mode = stat.S_IFDIR | 0o755
    st_reparse_tag = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


def test_write_verify_stream_refuses_a_junctioned_verify_directory(tmp_path, monkeypatch):
    """The win32 fallback must refuse a DIRECTORY JUNCTION, not just a symlink.

    `mklink /J` needs no elevation, while a directory symlink needs
    SeCreateSymbolicLinkPrivilege or Developer Mode — so on Windows the junction
    is the unprivileged half of the same escape, and `Path.is_symlink()` reports
    False for it. A guard written as `is_symlink()` would leave that half open
    with no race to win. Windows-only in reality; the logic is driven here so it
    does not ship unexercised.

    Ablation: point the guard back at `verify_dir.is_symlink()` and this fails
    `DID NOT RAISE` — verified.
    """
    monkeypatch.setattr(journal_mod, "DIR_FD_ANCHORED_WRITES", False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verify_dir = run_dir / "verify"
    verify_dir.mkdir()  # a real directory: is_symlink() is False, as for a junction

    # Patch the TAG TUPLE in platform_util, not `is_link_like` itself: journal.py
    # bound the function by value at import, so replacing the name there would not
    # reach this call — but the predicate reads `_LINK_REPARSE_TAGS` from its own
    # module globals on every call, so this does.
    real_lstat = os.lstat
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p, *a, **k: _ReparseStat() if str(p) == str(verify_dir) else real_lstat(p),
    )

    with pytest.raises(OSError, match=r"redirected verify directory"):
        Journal(run_dir).write_verify_stream(
            "v.stdout.log", "verifier output", run_dir_identity=_minted(run_dir)
        )


# ------------------------------------------------- append tail heal (DW-97)


def _journal_path(run_dir):
    return run_dir / "journal.jsonl"


def test_append_leaves_a_terminated_tail_alone(tmp_path):
    """The common case pays nothing: a journal ending in a newline gains exactly
    one line and no blank one.

    Ablation: heal unconditionally (drop the `_tail_is_terminated` guard) and this
    reddens on the blank line between the two records."""
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.append("session-start", task_id="t0")

    raw = _journal_path(tmp_path).read_text(encoding="utf-8")
    assert "\n\n" not in raw
    assert [e["kind"] for e in journal.entries()] == ["run-start", "session-start"]


def test_append_to_an_absent_or_empty_journal_writes_no_leading_newline(tmp_path):
    """A missing file and a zero-length one are both "terminated": there is no
    fragment to close, so neither may gain a leading blank line."""
    journal = Journal(tmp_path)
    assert not _journal_path(tmp_path).exists()
    journal.append("run-start")
    assert _journal_path(tmp_path).read_text(encoding="utf-8").startswith('{"ts"')

    other = tmp_path / "other"
    other.mkdir()
    _journal_path(other).write_text("", encoding="utf-8")
    Journal(other).append("run-start")
    assert _journal_path(other).read_text(encoding="utf-8").startswith('{"ts"')


def test_append_heals_an_unterminated_tail(tmp_path):
    """A partially flushed record ends the file mid-line. The next append must
    terminate that fragment on its OWN line rather than concatenating onto it, so
    the new record stays parseable."""
    _journal_path(tmp_path).write_text('{"ts": 1, "kind": "unit-merge-star', encoding="utf-8")
    journal = Journal(tmp_path)
    journal.append("unit-merged", unit="u1")

    lines = _journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"ts": 1, "kind": "unit-merge-star'
    assert json.loads(lines[1])["kind"] == "unit-merged"
    kinds = [e["kind"] for e in journal.entries()]
    assert kinds == [journal_mod.UNREADABLE_LINE_KIND, "unit-merged"]


def test_one_partial_flush_costs_one_record_not_two(tmp_path):
    """The regression this defect is about: WITHOUT the heal the first append
    concatenates onto the fragment and both are dropped as one unparseable line, so
    a single fault costs TWO records — and a swallowed `unit-merged` re-drives
    already-merged work in `engine._replay_unlatched_ledger_carries`.

    Ablation: delete the `if not self._tail_is_terminated()` prepend in
    `Journal.append` and this reddens — `unit-merged` is missing from the kinds and
    only two entries come back (verified)."""
    _journal_path(tmp_path).write_text('{"ts": 1, "kind": "unit-merge-star', encoding="utf-8")
    journal = Journal(tmp_path)
    journal.append("unit-merged", unit="u1")
    journal.append("run-complete")

    entries = journal.entries()
    assert [e["kind"] for e in entries] == [
        journal_mod.UNREADABLE_LINE_KIND,
        "unit-merged",
        "run-complete",
    ]
    assert entries[1]["unit"] == "u1"


def test_append_heals_when_the_tail_probe_fails(tmp_path, monkeypatch):
    """An unknown tail fails TOWARD the heal. The probe's seek can fail on a file
    that exists — a transient EIO, a sharing violation, an emulator that refuses the
    seek — and answering "already terminated" there would skip the heal over a real
    fragment and reproduce the two-record loss on exactly the unlucky path this heal
    exists to close. Costs at worst one blank line, which both readers skip.

    The empty-file arm answers True from ``fstat`` alone, and its own test above
    (`test_append_to_an_absent_or_empty_journal_writes_no_leading_newline`) is what
    keeps that arm honest — otherwise every fresh journal would open with a blank
    line.

    Ablation: answer `True` from `tail_is_terminated_fd`'s `except OSError` and this
    reddens — `unit-merged` is swallowed by the fragment."""
    _journal_path(tmp_path).write_text('{"ts": 1, "kind": "unit-merge-star', encoding="utf-8")
    journal = Journal(tmp_path)

    real_lseek = platform_util.os.lseek
    seen: list[int] = []

    def deny_the_probe(fd, pos, how):
        # Fail ONLY the first seek (the tail probe's); the write itself never seeks.
        if not seen:
            seen.append(fd)
            raise PermissionError(13, "denied")
        return real_lseek(fd, pos, how)

    monkeypatch.setattr(platform_util.os, "lseek", deny_the_probe)
    journal.append("unit-merged", unit="u1")
    monkeypatch.undo()

    assert seen, "the probe never ran; the test would be vacuous"
    lines = _journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"ts": 1, "kind": "unit-merge-star'
    assert json.loads(lines[1])["kind"] == "unit-merged"
    assert [e["kind"] for e in journal.entries()] == [
        journal_mod.UNREADABLE_LINE_KIND,
        "unit-merged",
    ]


def test_append_heals_only_once_per_fragment(tmp_path):
    """Two appends after one fragment leave one blank-free join: the second append
    sees a terminated tail and adds nothing.

    The blank-line count alone would stay green if the second append wrote NOTHING,
    so the kinds are asserted too — the claim is "adds no blank line", not "adds
    nothing"."""
    _journal_path(tmp_path).write_text("frag", encoding="utf-8")
    journal = Journal(tmp_path)
    journal.append("a")
    journal.append("b")
    assert _journal_path(tmp_path).read_text(encoding="utf-8").count("\n\n") == 0
    assert [e["kind"] for e in journal.entries()] == [
        journal_mod.UNREADABLE_LINE_KIND,
        "a",
        "b",
    ]


def test_append_preserves_a_complete_record_left_without_a_final_newline(tmp_path):
    """An unterminated tail is not always a TORN record: a whole record whose final
    newline never landed is complete JSON, and the heal must give it its own line so
    it still parses. Without the prepend the next record concatenates onto it and BOTH
    are lost — the same two-record fault, with the first record intact on disk.

    Ablation: drop the `if not self._tail_is_terminated()` prepend and this reddens —
    only the marker comes back (verified)."""
    _journal_path(tmp_path).write_text('{"ts": 1, "kind": "unit-merged"}', encoding="utf-8")
    journal = Journal(tmp_path)
    journal.append("run-complete")

    assert [e["kind"] for e in journal.entries()] == ["unit-merged", "run-complete"]


def test_append_leaves_an_existing_crlf_tail_alone(tmp_path):
    r"""A journal written through Windows text mode ends `\r\n`, whose LAST byte is
    still `\n` — so the probe reads it as terminated and no blank line is added, and
    the CRLF record still parses (`entries()` strips the `\r`).

    This is the CRLF half of a two-file pin; `test_append_leaves_a_terminated_tail_alone`
    is the LF half. The mutation only THIS half catches is a probe that reads a `\r\n`
    tail as a foreign writer's torn record and heals it —
    `tail.endswith(b"\n") and not tail.endswith(b"\r\n")` — which leaves the LF sibling
    green and reddens the blank-line assertion here (verified)."""
    _journal_path(tmp_path).write_bytes(b'{"ts": 1, "kind": "run-start"}\r\n')
    journal = Journal(tmp_path)
    journal.append("session-start", task_id="t0")

    # read_text normalizes `\r\n` to `\n`, so this catches a wrongly-healed blank line
    # in either spelling — including the `\r\n` one Windows text mode would write it as.
    assert _journal_path(tmp_path).read_text(encoding="utf-8").count("\n\n") == 0
    assert [e["kind"] for e in journal.entries()] == ["run-start", "session-start"]


def test_rearm_journal_subclass_inherits_the_heal(tmp_path):
    """`runs._RearmJournal.append` forwards to `super().append`, so the heal is not
    something a subclass has to remember."""
    _journal_path(tmp_path).write_text('{"kind": "frag', encoding="utf-8")
    runs._RearmJournal(tmp_path).append("rearm-ok", story_key="1-1")
    assert [e["kind"] for e in Journal(tmp_path).entries()] == [
        journal_mod.UNREADABLE_LINE_KIND,
        "rearm-ok",
    ]


# ------------------------------------------- append confinement and the pin


def _state(record=None):
    return RunState(
        run_id="r1", project="p", started_at="2026-10-17T00:00:00", run_dir_identity=record
    )


def _kinds(run_dir):
    return [e["kind"] for e in Journal(run_dir).entries()]


def test_append_bare_journal_writes_no_notice(tmp_path):
    """A journal built with no `pin` takes the unpinned arm silently: the shape is
    read-only or test-only outside the writers, so a notice there would be noise."""
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.append("session-start", task_id="t0")

    assert _kinds(tmp_path) == ["run-start", "session-start"]


def test_append_pinned_without_a_record_writes_one_notice_before_the_first_row(tmp_path):
    """A pinned state with no `run_dir_identity` (a pre-upgrade run never resumed, a
    zero-inode filesystem) still writes, confined but unpinned, and says so once —
    BEFORE the row that needed it. The notice carries only `reason` plus the
    self-minted fields, `log_*` among them only while a log is active."""
    Journal(tmp_path, pin=_state()).append("run-start")
    logged = Journal(tmp_path, pin=_state())
    logged.set_active_log("t0")
    logged.append("session-start", task_id="t0")

    entries = Journal(tmp_path).entries()
    assert [e["kind"] for e in entries] == [
        journal_mod.UNPINNED_NOTICE_KIND,
        "run-start",
        journal_mod.UNPINNED_NOTICE_KIND,
        "session-start",
    ]
    assert entries[0]["reason"] == "no-record"
    assert set(entries[0]) == {"ts", "kind", "reason"}
    assert set(entries[2]) == {"ts", "kind", "reason", "log_task", "log_pos"}


def test_append_notice_is_once_per_journal_object(tmp_path):
    """One notice per writer object, not per row: three appends through one object
    write one; a second object writes its own.

    Ablation: drop `self._unpinned_noticed = True` from `_notice_once` and this
    reddens (the notice recurses into itself until RecursionError)."""
    state = _state()
    journal = Journal(tmp_path, pin=state)
    for kind in ("a", "b", "c"):
        journal.append(kind)
    assert _kinds(tmp_path) == [journal_mod.UNPINNED_NOTICE_KIND, "a", "b", "c"]

    Journal(tmp_path, pin=state).append("d")
    assert _kinds(tmp_path).count(journal_mod.UNPINNED_NOTICE_KIND) == 2


def test_append_pinned_with_a_record_writes_no_notice(tmp_path):
    journal = Journal(tmp_path, pin=_state(_minted(tmp_path)))
    journal.append("run-start")
    journal.append("run-complete")

    assert _kinds(tmp_path) == ["run-start", "run-complete"]


def test_append_reads_the_pin_live(tmp_path):
    """The record is read from the shared `RunState` on every append, never
    snapshotted at construction: resume and re-arm backfill it after the journal is
    built. A record that lands later pins the next append — proven by a record for a
    different inode then refusing."""
    state = _state()
    journal = Journal(tmp_path, pin=state)
    journal.append("run-resume")

    state.run_dir_identity = _minted(tmp_path)
    journal.append("root-identity-recorded")
    assert _kinds(tmp_path) == [
        journal_mod.UNPINNED_NOTICE_KIND,
        "run-resume",
        "root-identity-recorded",
    ]

    dev, ino = _minted(tmp_path)
    state.run_dir_identity = (dev, ino + 1)
    with pytest.raises(platform_util.UnconfinedWriteError):
        journal.append("session-start")
    assert "session-start" not in _kinds(tmp_path)


@pytest.mark.skipif(not platform_util.HANDLE_ANCHORED_WRITES, reason="handle-anchored writes")
@pytest.mark.parametrize("swap", ["link", "directory"])
def test_append_pinned_refuses_a_run_dir_swapped_after_the_pin(tmp_path, monkeypatch, swap):
    """A run dir replaced after its identity was recorded, but before the root open,
    is refused with the typed error and nothing lands in the replacement — neither
    swap is a renumbered device, so there is no degrade. The `link` case is refused
    by the `O_NOFOLLOW` open on either arm; the `directory` case only by the pin.

    Ablation: pass `root_identity=None` on `_write`'s pinned call and the `directory`
    case fails `DID NOT RAISE`, with the row written into the replacement dir."""
    if swap == "link" and sys.platform == "win32":
        pytest.skip("POSIX symlinks")
    run_dir, elsewhere = tmp_path / "run", tmp_path / "elsewhere"
    run_dir.mkdir()
    elsewhere.mkdir()
    journal = Journal(run_dir, pin=_state(_minted(run_dir)))
    opener = platform_util.open_dir_confined

    def swap_then_open(root, target, **kwargs):
        if not (tmp_path / "run-aside").exists():
            run_dir.rename(tmp_path / "run-aside")
            if swap == "link":
                run_dir.symlink_to(elsewhere, target_is_directory=True)
            else:
                run_dir.mkdir()
        return opener(root, target, **kwargs)

    monkeypatch.setattr(platform_util, "open_dir_confined", swap_then_open)
    with pytest.raises(platform_util.UnconfinedWriteError):
        journal.append("unit-merged", unit="u1")

    assert list(elsewhere.iterdir()) == []
    assert list(run_dir.iterdir()) == []
    assert list((tmp_path / "run-aside").iterdir()) == []


def test_append_stale_device_degrades_with_a_notice(tmp_path):
    """A record that misses the intact run dir ONLY by `st_dev` (a reboot renumbered
    the device) degrades to the unpinned write plus a `stale-device` notice rather
    than refusing — the residual `reconcile_root_identities` already accepts."""
    dev, ino = _minted(tmp_path)
    journal = Journal(tmp_path, pin=_state((dev + 1, ino)))
    journal.append("stop-requested")
    journal.append("run-stopped")

    entries = Journal(tmp_path).entries()
    assert [e["kind"] for e in entries] == [
        journal_mod.UNPINNED_NOTICE_KIND,
        "stop-requested",
        "run-stopped",
    ]
    assert entries[0]["reason"] == "stale-device"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.skipif(not platform_util.HANDLE_ANCHORED_WRITES, reason="handle-anchored writes")
def test_append_refuses_a_linked_run_dir_even_unpinned(tmp_path):
    """Unpinned still means confined: the run dir is opened `O_NOFOLLOW` as the last
    component of the walk, so a link AT the run dir refuses."""
    run_dir, elsewhere = tmp_path / "run", tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_dir.symlink_to(elsewhere, target_is_directory=True)
    journal = Journal(run_dir)

    with pytest.raises(platform_util.UnconfinedWriteError):
        journal.append("run-start")
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_append_unpinned_follows_a_linked_parent(tmp_path):
    """The control for the refusal above: a link ABOVE the run dir is followed on the
    unpinned arm — only the run dir itself is opened `O_NOFOLLOW`."""
    real_parent = tmp_path / "real"
    (real_parent / "run").mkdir(parents=True)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    Journal(linked_parent / "run").append("run-start")

    assert _kinds(real_parent / "run") == ["run-start"]


def test_append_on_a_deleted_run_dir_raises_without_recreating_it(tmp_path):
    """`append` never mkdirs: a run dir removed under a live journal raises rather
    than being resurrected, which `runsetup._unwind_composition` relies on.

    Ablation: add `self.run_dir.mkdir(parents=True, exist_ok=True)` at the top of
    `append` and this fails `DID NOT RAISE`."""
    run_dir = tmp_path / "run"
    journal = Journal(run_dir)
    shutil.rmtree(run_dir)

    with pytest.raises(OSError):
        journal.append("composition-unwind-failed")
    assert not run_dir.exists()


def test_append_writes_lf_only_on_every_platform(tmp_path):
    """Bytes, not text mode: no `\\r` on Windows either."""
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.append("run-complete")

    raw = _journal_path(tmp_path).read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")


def test_two_journal_objects_interleave_whole_records(tmp_path):
    """Two writers on one run dir (the engine and an out-of-band `stop`) alternate
    whole records: every line parses and every record is kept."""
    first, second = Journal(tmp_path), Journal(tmp_path)
    for i in range(10):
        (first if i % 2 == 0 else second).append("tick", i=i)

    lines = _journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["i"] for line in lines] == list(range(10))


def test_rearm_journal_classifies_the_unpinned_notice_once(tmp_path):
    """The notice goes through the PUBLIC `append`, so `_RearmJournal` sees it like
    any other row — and its classifiers render nothing for it and hold nothing."""
    journal = runs._RearmJournal(tmp_path, pin=_state())
    journal.append("rearm-ok", story_key="1-1")

    assert journal.notices == []
    assert journal.hold_resume is False
    assert _kinds(tmp_path) == [journal_mod.UNPINNED_NOTICE_KIND, "rearm-ok"]


# ------------------------------------------- entries() unreadable-line marker


def test_entries_reports_an_unreadable_line_in_its_stream_position(tmp_path):
    """The marker takes the lost record's SLOT, so its position still carries the
    ordering information the entry itself would have.

    The torn line is deliberately PADDED with leading and trailing whitespace, so the
    `bytes` count can distinguish the raw line (12) from the stripped spelling the
    parse was attempted on (8). With an unpadded fixture both spellings give the same
    number and the choice is untestable.

    Ablation: restore `except json.JSONDecodeError: continue` and this reddens with
    the marker absent; count `len(line.encode(...))` (the stripped spelling) instead
    of the raw line and the `bytes` assertion reddens 8 != 12."""
    torn = "  not json  "
    _journal_path(tmp_path).write_text(
        f'{torn}\n{{"ts": 1, "kind": "run-start"}}\n', encoding="utf-8"
    )
    entries = Journal(tmp_path).entries()
    assert entries == [
        {"kind": journal_mod.UNREADABLE_LINE_KIND, "bytes": 12},
        {"ts": 1, "kind": "run-start"},
    ]
    assert len(torn) == 12 and len(torn.strip()) == 8  # the two spellings differ


def test_entries_marker_carries_no_ts_and_no_line_content(tmp_path):
    """`diagnostics.summarize_journal` derives first_ts/last_ts/duration_s from
    entry timestamps, so a fabricated `ts` would corrupt them; and a journal line
    can carry session text, so only a byte count is reported."""
    secret = '{"kind": "dev-decision", "note": "swordfish"'
    _journal_path(tmp_path).write_text(secret + "\n", encoding="utf-8")
    (marker,) = Journal(tmp_path).entries()
    assert marker == {"kind": journal_mod.UNREADABLE_LINE_KIND, "bytes": len(secret)}
    assert "swordfish" not in json.dumps(marker)


def test_entries_still_skips_blank_lines(tmp_path):
    """A blank line lost no record — and the heal itself can introduce one when a
    rival appender terminated the tail first — so blanks stay silent."""
    _journal_path(tmp_path).write_text('{"kind": "a"}\n\n\n{"kind": "b"}\n   \n', encoding="utf-8")
    assert [e["kind"] for e in Journal(tmp_path).entries()] == ["a", "b"]


def test_entries_still_passes_through_a_non_mapping_line(tmp_path):
    """A bare `3` PARSES, so it is not an unreadable line: it survives unchanged, as
    today, and `runs.journal_entries_or_none` is where non-dicts are filtered."""
    _journal_path(tmp_path).write_text('3\n{"kind": "a"}\n', encoding="utf-8")
    assert Journal(tmp_path).entries() == [3, {"kind": "a"}]


def test_entries_still_propagates_invalid_utf8(tmp_path):
    """Only `JSONDecodeError` becomes a marker. `runs.journal_entries_or_none`
    catches `UnicodeDecodeError` deliberately to return None (a journal it cannot
    read) rather than an empty list, and widening the marker to cover it would take
    that distinction away.

    Ablation: catch `UnicodeDecodeError` in `entries()` too and this fails
    `DID NOT RAISE`."""
    _journal_path(tmp_path).write_bytes(b'{"kind": "a"}\n\xff\xfe\n')
    with pytest.raises(UnicodeDecodeError):
        Journal(tmp_path).entries()
    assert runs.journal_entries_or_none(tmp_path) is None


def test_marker_survives_the_journal_entries_or_none_dict_filter(tmp_path):
    """The marker must be a plain dict: `journal_entries_or_none` drops non-dicts,
    and both its callers DIFF two reads on `len(before)` — a marker that vanished
    from one read and not the other would move the re-arm watermark."""
    _journal_path(tmp_path).write_text('bad\n{"kind": "a"}\n', encoding="utf-8")
    before = runs.journal_entries_or_none(tmp_path)
    after = runs.journal_entries_or_none(tmp_path)
    assert before is not None and after is not None
    assert len(before) == 2 and before == after
    assert before[0]["kind"] == journal_mod.UNREADABLE_LINE_KIND


# ------------------------------------------------- save_state confinement (DW-559)


def _save_arm(monkeypatch, arm):
    """Drive save_state on `arm`: the host's handle-anchored arm or the path fallback."""
    if arm == "anchored":
        if not platform_util.HANDLE_ANCHORED_WRITES:
            pytest.skip("handle-anchored writes")
    else:
        _force_path_fallback(monkeypatch)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("arm", ["anchored", "fallback"])
def test_save_state_pinned_refuses_a_swapped_run_dir(tmp_path, monkeypatch, arm):
    """`runs/` swapped, after the mint, for a link to a tree holding a REAL `<id>/`:
    the run dir path now reaches a real directory that is not the recorded one, so
    the pinned save refuses with the typed error and nothing lands on either side.

    Ablation: open the anchored arm's run dir unpinned (`open_dir_confined(
    run_dir.parent, run_dir)` for a record too) and the `anchored` row fails `DID
    NOT RAISE`, with `outside/r1/state.json` written; drop the fallback's
    `require_root_pinned` calls and the `fallback` row fails the same way."""
    _save_arm(monkeypatch, arm)
    runs_dir = tmp_path / "project" / ".bmad-loop" / "runs"
    run_dir = runs_dir / "r1"
    run_dir.mkdir(parents=True)
    state = _state(_minted(run_dir))
    outside = tmp_path / "outside"
    (outside / "r1").mkdir(parents=True)
    runs_dir.rename(runs_dir.with_name("runs-aside"))
    runs_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        save_state(run_dir, state)

    assert list((outside / "r1").iterdir()) == []
    assert list((runs_dir.with_name("runs-aside") / "r1").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("arm", ["anchored", "fallback"])
def test_save_state_unpinned_creates_the_run_dir_and_refuses_a_link_at_it(
    tmp_path, monkeypatch, arm
):
    """A bare `RunState` (no record) still creates a missing run dir, as every
    `save_state` caller that mints a dir through it relies on. Unpinned still means
    confined: a link AT the run dir refuses, because `mkdir(exist_ok=True)` accepts
    a symlink-to-directory and only the `O_NOFOLLOW` open (or, on the fallback, the
    `is_link_like` check) stands between the write and the link's target.

    Ablation: open the anchored arm's unpinned run dir as its own root
    (`open_dir_confined(run_dir, run_dir)`, which follows a link at the root) and the
    `anchored` row fails `DID NOT RAISE`; drop the fallback's `is_link_like(run_dir)`
    check and the `fallback` row does."""
    _save_arm(monkeypatch, arm)
    run_dir = tmp_path / "runs" / "r1"
    save_state(run_dir, _state())
    assert load_state(run_dir).run_id == "r1"

    linked, elsewhere = tmp_path / "linked", tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(platform_util.UnconfinedWriteError):
        save_state(linked, _state())
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("arm", ["anchored", "fallback"])
def test_save_state_stale_device_record_degrades_to_the_unpinned_write(tmp_path, monkeypatch, arm):
    """A record that misses the intact run dir ONLY by `st_dev` (a reboot renumbered
    the device) writes through the unpinned open instead of refusing — the residual
    `reconcile_root_identities` already accepts. No row: save_state has no journal.

    Ablation: drop the `stale_device_record` degrade on either arm and that row
    raises `UnconfinedWriteError`."""
    _save_arm(monkeypatch, arm)
    dev, ino = _minted(tmp_path)

    save_state(tmp_path, _state((dev + 1, ino)))

    assert load_state(tmp_path).run_dir_identity == (dev + 1, ino)


@pytest.mark.parametrize("arm", ["anchored", "fallback"])
def test_save_state_pinned_refuses_a_recorded_run_dir_that_is_gone(tmp_path, monkeypatch, arm):
    """A pinned save never recreates its run dir: only the unpinned arm mkdirs, so a
    recorded run dir removed underneath raises rather than being resurrected."""
    _save_arm(monkeypatch, arm)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(_minted(run_dir))
    shutil.rmtree(run_dir)

    with pytest.raises(platform_util.UnconfinedWriteError):
        save_state(run_dir, state)
    assert not run_dir.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.skipif(not platform_util.HANDLE_ANCHORED_WRITES, reason="handle-anchored writes")
@pytest.mark.parametrize("pinned", [True, False])
def test_save_state_lands_a_private_mode(tmp_path, pinned):
    """state.json lands `0600` whatever the umask, and keeps it over a prior file of
    a wider mode: `atomic_write_text_at` states its mode and inherits nothing. The
    `0o022` bracket is the point (see `test_atomic_write_text_at_lands_a_private_mode`).

    Ablation: revert save_state to the `write_text` + `atomic_replace` pair and both
    rows fail reporting 0o644."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / journal_mod.STATE_FILE).write_text("{}", encoding="utf-8")
    os.chmod(run_dir / journal_mod.STATE_FILE, 0o644)
    previous = os.umask(0o022)
    try:
        save_state(run_dir, _state(_minted(run_dir) if pinned else None))
    finally:
        os.umask(previous)

    assert stat.S_IMODE(os.stat(run_dir / journal_mod.STATE_FILE).st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_save_state_fallback_repins_before_the_replace(tmp_path, monkeypatch):
    """`runs/` swapped for a link to a tree holding a real `<id>/` AFTER the first
    pin (on the `is_link_like` call, before the temp is written) is caught by the
    SECOND `require_root_pinned`, just before the replace. The run dir is a real
    directory reached through the link, so the link check alone passes the swap.
    The temp the write staged there is removed with the refusal.

    Ablation: drop the second `require_root_pinned(run_dir, identity)` and this
    fails `DID NOT RAISE`, with `outside/r1/state.json` written."""
    _force_path_fallback(monkeypatch)
    runs_dir = tmp_path / "project" / ".bmad-loop" / "runs"
    run_dir = runs_dir / "r1"
    run_dir.mkdir(parents=True)
    state = _state(_minted(run_dir))
    outside = tmp_path / "outside"
    (outside / "r1").mkdir(parents=True)
    real_is_link_like = journal_mod.is_link_like
    swapped = False

    def swap_then_check(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            runs_dir.rename(runs_dir.with_name("runs-aside"))
            runs_dir.symlink_to(outside, target_is_directory=True)
        return real_is_link_like(path)

    monkeypatch.setattr(journal_mod, "is_link_like", swap_then_check)

    with pytest.raises(platform_util.UnconfinedWriteError, match=r"no longer the directory"):
        save_state(run_dir, state)

    assert swapped  # the premise: the swap happened after the first pin
    assert list((outside / "r1").iterdir()) == []
    assert list((runs_dir.with_name("runs-aside") / "r1").iterdir()) == []


@pytest.mark.parametrize("arm", ["anchored", "fallback"])
@pytest.mark.parametrize("pinned", [True, False])
def test_save_state_round_trips_through_load_state(tmp_path, monkeypatch, arm, pinned):
    """The positive control for every refusal above: an intact run dir saves and
    loads back the same state on both arms, pinned or not, and leaves no temp."""
    _save_arm(monkeypatch, arm)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = _state(_minted(run_dir) if pinned else None)
    state.finished = True

    save_state(run_dir, state)
    save_state(run_dir, state)  # over an existing state.json

    assert load_state(run_dir).to_dict() == state.to_dict()
    assert sorted(p.name for p in run_dir.iterdir()) == [journal_mod.STATE_FILE]
