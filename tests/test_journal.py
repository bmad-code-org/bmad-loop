"""State persistence: the atomic write must survive the transient Windows
sharing violation (WinError 5) a concurrent TUI reader triggers. The retry
lives in platform_util.atomic_replace (unit-tested there); this proves
save_state still rides it end to end."""

from __future__ import annotations

import os
import stat

import pytest

from bmad_loop import journal as journal_mod
from bmad_loop import platform_util
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import RunState


def test_save_state_retries_transient_sharing_violation(tmp_path, monkeypatch):
    """On win32, os.replace denied by a concurrent reader is retried, not fatal."""
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)  # no real backoff

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
