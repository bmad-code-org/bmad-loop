"""State persistence: the atomic write must survive the transient Windows
sharing violation (WinError 5) a concurrent TUI reader triggers. The retry
lives in platform_util.atomic_replace (unit-tested there); this proves
save_state still rides it end to end."""

from __future__ import annotations

import os
import stat
import threading
from contextlib import contextmanager

import pytest

from bmad_loop import journal as journal_mod
from bmad_loop import platform_util, runs
from bmad_loop.journal import Journal, load_state, save_state, state_lock
from bmad_loop.model import RunState


def test_save_state_retries_transient_sharing_violation(tmp_path, monkeypatch):
    """On win32, os.replace denied by a concurrent reader is retried, not fatal."""
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


def test_two_concurrent_saves_never_share_the_fixed_temp_file(tmp_path, monkeypatch):
    """Ablation: remove save_state's state_lock and the second replace enters while
    the first is paused, so both calls race on state.json.tmp and one loses it."""
    real_replace = journal_mod.atomic_replace
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

    def controlled_replace(src, dst):
        replace_threads.append(threading.current_thread().name)
        if len(replace_threads) == 1:
            first_entered.set()
            assert release_first.wait(2)
        real_replace(src, dst)

    monkeypatch.setattr(journal_mod, "atomic_replace", controlled_replace)
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
        journal.write_verify_stream("v.stdout.log", "verifier output")

    # the assertion that actually pins the fix: nothing escaped the run dir
    assert list(elsewhere.iterdir()) == []


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
        journal.write_verify_stream("v.stdout.log", "verifier output")

    assert list(elsewhere.iterdir()) == []


def test_write_verify_stream_writes_an_ordinary_verify_directory(tmp_path):
    """The positive control: an unplanted run dir still retains its streams.

    Without this, both refusal tests above pass for a `write_verify_stream` that
    refuses everything unconditionally — a negative assertion is green for every
    reason a file could be absent.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = Journal(run_dir)

    pointer = journal.write_verify_stream("v.stdout.log", "verifier output")

    assert pointer == "verify/v.stdout.log"
    assert (run_dir / pointer).read_text(encoding="utf-8") == "verifier output"


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
        Journal(run_dir).write_verify_stream("v.stdout.log", "verifier output")
