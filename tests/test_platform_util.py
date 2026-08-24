"""Tests for the back-compat shims over the ProcessHost seam.

The kill/liveness bodies (and their pid<=0 guards) now live in
``bmad_loop.process_host`` — see ``test_process_host.py``. These cover only that
the legacy ``platform_util`` entry points still delegate, plus the real
``detach_kwargs`` that stayed behind."""

from __future__ import annotations

import errno
import ntpath
import os
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

import pytest

from bmad_loop import platform_util


def test_pid_alive_shim_true_for_self():
    assert platform_util.pid_alive(os.getpid()) is True


def test_pid_alive_shim_false_for_non_positive():
    assert platform_util.pid_alive(0) is False
    assert platform_util.pid_alive(-1) is False


def test_terminate_pid_shim_noop_for_non_positive():
    # delegates to the host, whose pid<=0 guard short-circuits before any signal
    platform_util.terminate_pid(0)  # no raise, no signal
    platform_util.terminate_pid(-42)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detach branch")
def test_detach_kwargs_posix():
    assert platform_util.detach_kwargs() == {"start_new_session": True}


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",  # POSIX-absolute — rejected even when running on Windows
        "C:\\Windows\\system32",  # Windows-absolute — rejected even on POSIX
        "C:/Windows",
        "\\\\server\\share",  # UNC root
        "C:foo",  # Windows drive-*relative* — still drive-qualified, intentionally rejected
    ],
)
def test_is_absolute_path_rejects_both_flavors(value):
    assert platform_util.is_absolute_path(value) is True


@pytest.mark.parametrize("value", [".claude/skills", "a/b/c.json", "file.txt", "."])
def test_is_absolute_path_accepts_relative(value):
    assert platform_util.is_absolute_path(value) is False


@pytest.mark.parametrize(
    "value",
    ["../etc", "../../secrets", "a/../../b", "a\\..\\b", "..", "nested/dir/../x"],
)
def test_has_parent_ref_detects_escapes(value):
    assert platform_util.has_parent_ref(value) is True


@pytest.mark.parametrize("value", [".claude/skills", "a/b/c", "..hidden", "a..b/c"])
def test_has_parent_ref_ignores_non_segments(value):
    # `..hidden` / `a..b` contain the substring but not a `..` path segment.
    assert platform_util.has_parent_ref(value) is False


@pytest.mark.parametrize("value", ["", ".", "./", ".//", "./.", ".\\"])
def test_names_tree_root_catches_every_spelling_of_the_root(value):
    # `""` is the spelling an emptiness check catches; the rest are why this exists.
    # `.\` is the Windows-only one — POSIX parsing keeps it as a one-segment name,
    # the same asymmetry `is_absolute_path` checks both flavors for.
    assert platform_util.names_tree_root(value) is True


@pytest.mark.parametrize(
    "value",
    [
        ". ",  # Win32 trims the trailing space -> names the containing dir
        ".  ",
        ".. ",  # the space stops it matching `..`, so Win32 trims rather than climbs
        "...",
        "....",
        "   ",  # no period at all, still trimmed to empty
        ". .",
        " . ",
        "./ ",
        ".\\ ",  # separator + a component that is nothing but a space
    ],
)
def test_names_tree_root_catches_the_win32_trim_aliases(value):
    # Win32 strips every trailing period and space from a path's final component,
    # so each of these names the tree root there. Both pure pathlib flavours keep
    # them as ordinary one-segment names, which is exactly why the lexical guard
    # has to know the rule — `resolve()` would, but these are checked at load,
    # long before any path is resolved. Parametrized apart from the `.`/`./` cases
    # so restoring the pure-equality-only guard reddens these and only these.
    assert platform_util.names_tree_root(value) is True


# --------------------------------------------------------------- is_wsl_unc_path


@pytest.mark.parametrize(
    "value",
    [
        "\\\\wsl.localhost\\Ubuntu-24.04\\home\\u\\p",
        "\\\\wsl$\\Ubuntu\\home\\u\\p",  # legacy prefix, still minted by older Windows
        "\\\\WSL.LOCALHOST\\Ubuntu\\home\\u",  # UNC hosts are case-insensitive
        "\\\\wsl$\\Ubuntu",  # distro root, no further path
        "\\\\wsl.localhost\\Ubuntu\\",  # trailing separator, no path component
        "//wsl.localhost/Ubuntu/home/u/p",  # Windows accepts either separator
        "\\\\wsl.localhost/Ubuntu\\home",  # mixed separators
        Path("\\\\wsl.localhost\\Ubuntu\\home\\u\\p"),  # a Path, not just a str
        "\\\\?\\UNC\\wsl.localhost\\Ubuntu\\home\\u\\p",  # extended-length UNC spelling
        "\\\\?\\unc\\wsl$\\Ubuntu\\home\\u",  # extended-length, legacy host, lowercase
        # not a shape Win32 accepts (a forward-slash device path addresses a host
        # named `?`) — the separator fold over-matches it, which can only add the
        # warning, never suppress it
        "//?/UNC/wsl.localhost/Ubuntu/home/u",
    ],
)
def test_is_wsl_unc_path_matches_the_interop_bridge(value):
    assert platform_util.is_wsl_unc_path(value) is True


@pytest.mark.parametrize(
    "value",
    [
        ".claude/skills",
        "a",
        "a/.",
        "./a",
        ".hidden",
        "..",
        "a/b",
        "foo. ",  # strips to `foo` — names a CHILD, not the root
        "foo ",
        "a/. ",  # the trailing component is trimmed away, leaving `a`
        ".claude/skills ",
        "..hidden",
        "a..b",
        "../..",  # every component is dots, but these climb — has_parent_ref's job
        "a/..",
    ],
)
def test_names_tree_root_accepts_anything_naming_a_child(value):
    # `a/.` and `./a` normalize to a real child, so they name something inside the
    # tree. `..` names the PARENT, which is `has_parent_ref`'s job, not this one —
    # the guards are paired at every call site. The trailing-space entries are the
    # boundary of the trim rule: a component only stops naming something once it is
    # *nothing but* periods and spaces.
    assert platform_util.names_tree_root(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "C:\\projects\\p",  # a real Windows drive path — psmux is right for it
        "C:/projects/p",
        "/home/u/p",  # the same project seen from inside the distro
        "\\\\fileserver\\share\\p",  # UNC, but not WSL: must not warn
        "\\\\wslfoo\\share\\p",  # host merely *starts* with wsl
        "\\\\wsl.localhost",  # no distro component at all
        "wsl.localhost\\Ubuntu\\home",  # not a UNC path
        "\\\\?\\UNC\\fileserver\\share\\p",  # extended-length, but not a WSL host
        "\\\\?\\C:\\p",  # extended-length drive path, not UNC at all
        "",
    ],
)
def test_is_wsl_unc_path_ignores_everything_else(value):
    assert platform_util.is_wsl_unc_path(value) is False


def test_is_wsl_unc_path_is_platform_blind(monkeypatch):
    """The predicate answers "is this path inside a distro", never "am I on Windows"
    — the sys.platform half lives at the runsetup call site. Pinned so the two halves
    stay separable and this truth table needs no platform monkeypatching."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert platform_util.is_wsl_unc_path("\\\\wsl.localhost\\Ubuntu\\home\\u") is True


@pytest.mark.skipif(sys.platform != "win32", reason="UNC resolution is a Windows behavior")
@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("\\\\wsl.localhost\\{d}\\home", "\\\\wsl.localhost\\{d}\\home"),
        ("\\\\wsl$\\{d}\\home", "\\\\wsl$\\{d}\\home"),
        ("//wsl.localhost/{d}/home", "\\\\wsl.localhost\\{d}\\home"),
        # resolve() must keep, not strip, an extended prefix the input carried —
        # the premise the predicate's fold rests on; if a future CPython starts
        # stripping it, the fold goes dead and only this row notices
        ("\\\\?\\UNC\\wsl.localhost\\{d}\\home", "\\\\?\\UNC\\wsl.localhost\\{d}\\home"),
    ],
)
def test_is_wsl_unc_path_survives_the_resolve_the_caller_applies(spelling, expected, monkeypatch):
    """The one shape production actually sees. `cli._project` hands the preflight a
    `Path(...).resolve()`, and every other test here passes an unresolved literal — so
    a `Path.resolve()` semantics change (it has moved across 3.6/3.8/3.13) could
    normalize the bridge prefix away and silently disable the whole #332 check with
    this file still green.

    This row pins `realpath`'s *lexical* walk — the branch taken when the syscall
    cannot answer — so the syscall wrapper is stubbed rather than aimed at a live
    provider. It raises ERROR_BAD_NET_NAME (67), which is on CPython's non-strict
    allow-list, so `realpath` degrades instead of failing. That is what the earlier
    "the distro need not exist" premise assumed it always got, and #529 is what
    happens when it does not: a registered-but-not-serving `wsl$` provider answers
    ERROR_NETNAME_DELETED (64), which is *off* that list, so `resolve()` raises and a
    semantics guard flakes on network state. The other branch — what a *serving*
    distro makes `realpath` do — is pinned by the sibling test below; keep both, they
    reach the same string by different routes."""
    calls: list[str] = []

    def unreachable_provider(path):
        calls.append(path)
        # Only the 4th arg (winerror) matters: an OSError escaping this test means 67
        # left ntpath's non-strict allow-list and the premise below needs re-measuring.
        raise OSError(0, "stubbed: 67 must stay on ntpath's non-strict allow-list", None, 67)

    def no_symlink(_path):
        raise OSError(0, "stubbed: nothing to dereference", None, 67)

    monkeypatch.setattr(ntpath, "_getfinalpathname", unreachable_provider)
    # the lexical walk also tries to read the path as a symlink; stub that too so the
    # row touches no provider at all, on a runner with WSL or without
    monkeypatch.setattr(ntpath, "_nt_readlink", no_symlink)

    resolved = Path(spelling.format(d="Ubuntu-24.04")).resolve()
    # Ablation guard: a future pathlib that stops routing resolve() through ntpath would
    # leave the stub unused and everything below would pass while pinning nothing. The
    # second half is what proves the *lexical walk* ran and not just the opening call:
    # measured 3.11-3.14, it asks 3 times and the last ask is the parent, having climbed.
    assert len(calls) > 1 and calls[-1] != calls[0], "resolve() no longer walks the path in ntpath"
    assert str(resolved) == expected.format(d="Ubuntu-24.04")
    assert platform_util.is_wsl_unc_path(resolved) is True


@pytest.mark.skipif(sys.platform != "win32", reason="UNC resolution is a Windows behavior")
@pytest.mark.parametrize("host", ["wsl.localhost", "wsl$"])
def test_is_wsl_unc_path_survives_the_resolve_a_serving_distro_takes(host, monkeypatch):
    """The other branch of the same `resolve()`, and the one production runs on the
    hosts #332 exists for. Measured on Win11/WSL2 with the distro serving: the syscall
    *succeeds* and hands `realpath` the extended form, which `realpath` then strips
    back down because it added that prefix itself — an add/strip decision the sibling
    test's lexical walk never reaches. Stubbing the syscall's *return value* covers it
    with no provider; a live row would only reinstate the #529 flake."""
    plain = f"\\\\{host}\\Ubuntu-24.04\\home"
    calls: list[str] = []

    def serving_provider(path):
        calls.append(path)
        return f"\\\\?\\UNC\\{host}\\Ubuntu-24.04\\home"

    monkeypatch.setattr(ntpath, "_getfinalpathname", serving_provider)

    resolved = Path(plain).resolve()
    # Same ablation guard, and it is what makes this row non-vacuous: on a host with a
    # live distro an unstubbed resolve() returns `plain` too, so without proof the stub
    # answered, both asserts below would pass on the environment rather than the code.
    # Two asks, measured 3.11-3.14: the input, then the prefix-stripped candidate
    # realpath re-verifies before returning it — that verify *is* the strip decision.
    assert calls == [plain, plain], "realpath no longer verifies the prefix it strips"
    assert str(resolved) == plain
    assert platform_util.is_wsl_unc_path(resolved) is True


# ---------------------------------------------------------------- atomic_replace


def _flaky_replace(fail_times: int, real=os.replace):
    """os.replace that raises a sharing violation the first ``fail_times`` calls."""
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    return replace, calls


def test_atomic_replace_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    replace, calls = _flaky_replace(2)
    monkeypatch.setattr(platform_util.os, "replace", replace)

    src = tmp_path / "s.tmp"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "d.json"
    platform_util.atomic_replace(src, dst)

    assert calls["n"] == 3
    assert len(sleeps) == 2  # one backoff before each retry
    assert dst.read_text(encoding="utf-8") == "x"


def test_atomic_replace_permanent_failure_reraises(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(platform_util.os, "replace", always_denied)

    with pytest.raises(PermissionError):
        platform_util.atomic_replace(tmp_path / "s", tmp_path / "d")


def test_atomic_replace_no_retry_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "linux")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    def denied(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(platform_util.os, "replace", denied)

    with pytest.raises(PermissionError):
        platform_util.atomic_replace(tmp_path / "s", tmp_path / "d")
    assert sleeps == []  # zero backoff — a real POSIX error surfaces at once


# -------------------------------------------------------- neutralize_surrogates


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain text", "plain text"),
        ("", ""),
        ("\ud800", "\ufffd"),  # the lone surrogate json.loads revives (#329)
        ("a\ud800b", "a\ufffdb"),
        ("\udfff", "\ufffd"),  # the far end of the range
        ("x\udfffy", "x\ufffdy"),
        ("\ud800\ud801\udfff", "\ufffd\ufffd\ufffd"),  # a run: one per code point
        ("\U0001d11e", "\U0001d11e"),  # astral, ONE code point — never a pair here
        ("\u00e9\U0001d11e\u6f22", "\u00e9\U0001d11e\u6f22"),
        ("\ud7ff\ue000", "\ud7ff\ue000"),  # the code points either side of the range
    ],
    ids=[
        "clean",
        "empty",
        "lone-d800",
        "surrounded",
        "lone-dfff",
        "surrounded-dfff",
        "run-per-code-point",
        "astral-untouched",
        "mixed-non-ascii",
        "range-boundaries",
    ],
)
def test_neutralize_surrogates_replaces_only_lone_surrogates(value, expected):
    """A surrogate has no UTF-8 encoding; everything else must survive intact.
    The astral row is the one that would break under a naive UTF-16 mental model:
    Python holds U+1D11E as a single code point, not a D834/DD1E pair, so it is
    outside the range and must come back byte-identical."""
    result = platform_util.neutralize_surrogates(value)

    assert result == expected
    result.encode("utf-8")  # the whole point: the strict encode now succeeds


def test_neutralize_surrogates_returns_clean_text_untouched():
    """The fast path hands back the identical object, so a clean ledger write
    stays byte-identical to one taken before the guard existed."""
    value = "origin: review of spec-foo.md"

    assert platform_util.neutralize_surrogates(value) is value


def test_neutralize_surrogates_makes_atomic_write_text_survive_a_surrogate(tmp_path):
    """The pairing this helper exists for: the same value crashes the strict
    encode without it and round-trips through a strict read with it."""
    target = tmp_path / "ledger.md"

    with pytest.raises(UnicodeEncodeError):
        platform_util.atomic_write_text(target, "note: \ud800")

    platform_util.atomic_write_text(target, platform_util.neutralize_surrogates("note: \ud800"))
    assert target.read_text(encoding="utf-8") == "note: �"


# ------------------------------------------------------------- atomic_write_text


def test_atomic_write_text_replaces_contents(tmp_path):
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")

    platform_util.atomic_write_text(target, "after")

    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_text_preserves_the_target_mode(tmp_path):
    """`os.replace` swaps in a NEW inode, so a naive tmp-write-and-replace resets
    the file's permissions to the umask default — silently widening a 0600 ledger
    to world-readable, or dropping group-write on a shared artifact dir.

    0o640, NOT 0o600: `mkstemp` creates its temp at 0600 already, so asserting that
    value passed with `copymode` deleted — the pin held for the wrong reason. Any
    mode the staging temp does not arrive with makes the ablation bite."""
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)

    platform_util.atomic_write_text(target, "after")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_text_writes_through_a_symlink(tmp_path):
    """Replacing the LINK would turn it into a regular file and orphan the real
    ledger, so the link is resolved first and the target is what gets rewritten."""
    real = tmp_path / "real-ledger.md"
    real.write_text("before", encoding="utf-8")
    link = tmp_path / "ledger.md"
    link.symlink_to(real)

    platform_util.atomic_write_text(link, "after")

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "after"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_text_no_follow_replaces_the_link(tmp_path):
    """The inverse contract, for a machine-minted file somewhere a less-trusted
    writer can reach: honouring a planted link would aim the write at a path of
    that writer's choosing, so the *name* is what gets replaced.

    No preflight check is what makes it safe — `os.replace` does not dereference
    its destination, so a link planted at any moment, including after a check
    would have run, is clobbered rather than written through."""
    real = tmp_path / "someone-elses-file"
    real.write_text("before", encoding="utf-8")
    link = tmp_path / "record"
    link.symlink_to(real)

    platform_util.atomic_write_text(link, "after", follow_symlinks=False)

    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "after"
    assert real.read_text(encoding="utf-8") == "before"  # untouched


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_text_no_follow_does_not_inherit_a_link_targets_mode(tmp_path):
    """A name being replaced rather than updated carries nothing of whatever it
    used to point at — inheriting the target's mode would let a planted link
    choose the new record's permissions."""
    real = tmp_path / "someone-elses-file"
    real.write_text("before", encoding="utf-8")
    real.chmod(0o666)
    link = tmp_path / "record"
    link.symlink_to(real)

    platform_util.atomic_write_text(link, "after", follow_symlinks=False)

    assert stat.S_IMODE(link.stat().st_mode) == 0o600  # mkstemp's private default


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_text_no_follow_does_not_inherit_a_plain_files_mode(tmp_path):
    """No-follow inherits nothing, and takes no probe to decide it — the sibling
    above covers the link; this covers the plain file, which is the case a probe
    would have said yes to.

    Inheriting here needs a shape check and then a `copymode`, and `copymode`
    re-resolves: a writer who plants a link in that gap chooses the new record's
    permissions. The probe is what makes that gap exist, so there is none. 0o640,
    for the reason the follow-mode pins give — `mkstemp` already arrives at 0600,
    so only a mode it does NOT arrive with can tell inheritance from its absence.

    The pairing is the ablation: restore the probe-and-copy and this reddens
    while the link sibling stays green, so it bites on inheritance itself rather
    than on anything the no-follow path does incidentally."""
    target = tmp_path / "record"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)

    platform_util.atomic_write_text(target, "after", follow_symlinks=False)

    assert target.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_text_preserves_extended_attributes(tmp_path):
    """`os.replace` swaps a fresh inode into place, so anything carried by the old
    inode rather than by its name is silently reset — xattrs included, which on a
    ledger is where an SELinux label or a backup tool's marker lives. Deleting
    `_copy_xattrs` left every other test green (#284 follow-up review, finding 8).

    Skipped where the platform or filesystem has no user xattrs (Windows, macOS's
    different API, tmpfs mounted `nouser_xattr`) — the helper is best-effort by
    design and must stay silent there, which the write below also proves."""
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")
    setxattr = getattr(os, "setxattr", None)
    if setxattr is None:
        pytest.skip("no os.setxattr on this platform")
    try:
        setxattr(target, "user.bmad-loop-test", b"kept")
    except OSError as e:
        pytest.skip(f"filesystem does not support user xattrs: {e}")

    platform_util.atomic_write_text(target, "after")

    assert target.read_text(encoding="utf-8") == "after"
    assert os.getxattr(target, "user.bmad-loop-test") == b"kept"


def test_atomic_write_text_fsyncs_before_it_publishes(tmp_path, monkeypatch):
    """`os.replace` is atomic against concurrent readers, but that says nothing
    about a machine losing power: closing the temp only hands its bytes to the
    page cache, so the rename can be durable while the data is not, and the new
    name comes back pointing at a zero-length file. An empty ledger *parses* — as
    no entries — so the failure reads as every hand-written entry having vanished
    rather than as corruption."""
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        platform_util.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        platform_util.os, "replace", lambda s, d: (order.append("replace"), real_replace(s, d))[1]
    )
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")

    platform_util.atomic_write_text(target, "after")

    assert order == ["fsync", "replace"]  # the sync must precede the publish
    assert target.read_text(encoding="utf-8") == "after"


def test_atomic_write_text_leaves_no_temp_behind(tmp_path):
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")

    platform_util.atomic_write_text(target, "after")

    assert [p.name for p in tmp_path.iterdir()] == ["ledger.md"]


def test_atomic_write_text_cleans_up_and_keeps_the_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(platform_util.os, "replace", boom)

    with pytest.raises(OSError):
        platform_util.atomic_write_text(target, "after")

    assert target.read_text(encoding="utf-8") == "before"
    assert [p.name for p in tmp_path.iterdir()] == ["ledger.md"]  # no orphaned temp


def test_atomic_write_text_temp_name_is_unique_per_call(tmp_path, monkeypatch):
    """A fixed `<name>.tmp` sibling is a collision between two writers of the same
    file — the second clobbers the first's staged content and one replace lands
    a half-written mix."""
    target = tmp_path / "ledger.md"
    target.write_text("before", encoding="utf-8")
    seen: list[str] = []
    real_replace = os.replace

    def record(src, dst):
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)

    platform_util.atomic_write_text(target, "a")
    platform_util.atomic_write_text(target, "b")

    assert len(set(seen)) == 2


# ------------------------------------------------------------ atomic_write_bytes
#
# Mirrored from the eight text cases above rather than parametrized over both, and
# deliberately: each property is a separate PIN the git-add shield now depends on
# (install.py's `_worktree_local_exclude` writes through this variant, not the text
# one), and the two helpers share a private tail that a later edit could split
# without either name changing. A parametrized suite would also lose the
# per-property rationale the text docstrings carry, which is where the reasons live.
# What is NOT mirrored is anything about encoding — the last case below is the whole
# difference between the two.


def test_atomic_write_bytes_replaces_contents(tmp_path):
    target = tmp_path / "exclude"
    target.write_bytes(b"before")

    platform_util.atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"after"


def test_atomic_write_bytes_writes_bytes_no_codec_can_decode(tmp_path):
    """The reason this variant exists (#384): the payload is an operator's git
    exclude file, whose patterns are POSIX paths and therefore arbitrary bytes.
    `atomic_write_text` would have to encode, and a strict UTF-8 encode of a
    legacy-encoded file's content raises."""
    target = tmp_path / "exclude"
    target.write_bytes(b"before\n")

    platform_util.atomic_write_bytes(target, b"secret-\xff\n/probe\n")

    assert target.read_bytes() == b"secret-\xff\n/probe\n"


def test_atomic_write_bytes_does_not_translate_newlines(tmp_path):
    """The one behavioral difference from the text sibling, and it is load-bearing on
    Windows. `atomic_write_text` opens in text mode with the translating `newline`
    default, so an LF payload lands as CRLF there — correct for a ledger a human
    edits, wrong for a file being copied byte-for-byte from somewhere else. Binary
    mode does no translation on any platform, which is what makes a verbatim copy
    verbatim."""
    target = tmp_path / "exclude"
    target.write_bytes(b"before\n")

    platform_util.atomic_write_bytes(target, b"a\nb\n")

    assert target.read_bytes() == b"a\nb\n"  # never b"a\r\nb\r\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_bytes_preserves_the_target_mode(tmp_path):
    """Same pin as the text sibling — and the one the shield leans on hardest: an
    exclude file git cannot READ is one git silently IGNORES, so a mode reset here
    stages the very files the exclude was written to hide.

    0o640 rather than 0o600 for the reason the text sibling gives: `mkstemp`'s temp
    is already 0600, so that value cannot tell a preserved mode from a fresh one."""
    target = tmp_path / "exclude"
    target.write_bytes(b"before")
    target.chmod(0o640)

    platform_util.atomic_write_bytes(target, b"after")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_bytes_writes_through_a_symlink(tmp_path):
    real = tmp_path / "real-exclude"
    real.write_bytes(b"before")
    link = tmp_path / "exclude"
    link.symlink_to(real)

    platform_util.atomic_write_bytes(link, b"after")

    assert link.is_symlink()
    assert real.read_bytes() == b"after"


# The no-follow trio for the BYTES helper (#363), mirroring the text trio above.
# The bytes sibling grew `follow_symlinks` when `policy.write_mux_backend` moved onto
# it: that site reads and writes bytes to preserve a CRLF policy.toml's endings, and
# needs no-follow because a driven session can write `.bmad-loop/policy.toml`.
#
# ABLATION A1: drop the `follow_symlinks=follow_symlinks` forward in
# `atomic_write_bytes` (leave the parameter, so callers still typecheck) and these
# three redden together while the TEXT trio and the True-default pins above stay
# green — the disjointness is what shows the forward, not the shared `_atomic_write`
# body, is what these grade.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_bytes_no_follow_replaces_the_link(tmp_path):
    """The inverse of the sibling above: the *name* is replaced, whatever it points
    at. Honouring a planted link would aim a machine-minted write at a path of the
    planter's choosing, and no preflight check is what makes this safe — `os.replace`
    does not dereference its destination, so a link planted after any check would
    have run is clobbered rather than written through."""
    real = tmp_path / "someone-elses-file"
    real.write_bytes(b"before")
    link = tmp_path / "record"
    link.symlink_to(real)

    platform_util.atomic_write_bytes(link, b"after", follow_symlinks=False)

    assert not link.is_symlink()
    assert link.read_bytes() == b"after"
    assert real.read_bytes() == b"before"  # untouched


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_bytes_no_follow_does_not_inherit_a_link_targets_mode(tmp_path):
    """A name being replaced rather than updated carries nothing of whatever it used
    to point at — inheriting the target's mode would let a planted link choose the
    new record's permissions."""
    real = tmp_path / "someone-elses-file"
    real.write_bytes(b"before")
    real.chmod(0o666)
    link = tmp_path / "record"
    link.symlink_to(real)

    platform_util.atomic_write_bytes(link, b"after", follow_symlinks=False)

    assert stat.S_IMODE(link.stat().st_mode) == 0o600  # mkstemp's private default


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_bytes_no_follow_does_not_inherit_a_plain_files_mode(tmp_path):
    """No-follow inherits nothing and takes no probe to decide it — the sibling above
    covers the link, this covers the plain file, which is the case a probe would have
    said yes to. 0o640 for the reason every mode pin here gives: `mkstemp` already
    ARRIVES at 0600, so only a mode it does not arrive with can tell inheritance from
    its absence.

    Ablation A2: change `_atomic_write`'s `if follow_symlinks and target.exists():`
    to `if target.exists():` and this reddens together with the three other
    "does_not_inherit" rows (both helpers), while both "replaces_the_link" rows stay
    green — so it bites on inheritance itself, not on anything else no-follow does."""
    target = tmp_path / "record"
    target.write_bytes(b"before")
    target.chmod(0o640)

    platform_util.atomic_write_bytes(target, b"after", follow_symlinks=False)

    assert target.read_bytes() == b"after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_bytes_preserves_extended_attributes(tmp_path):
    """Skipped where the platform or filesystem has no user xattrs, exactly as the
    text sibling is — the helper is best-effort there by design."""
    target = tmp_path / "exclude"
    target.write_bytes(b"before")
    setxattr = getattr(os, "setxattr", None)
    if setxattr is None:
        pytest.skip("no os.setxattr on this platform")
    try:
        setxattr(target, "user.bmad-loop-test", b"kept")
    except OSError as e:
        pytest.skip(f"filesystem does not support user xattrs: {e}")

    platform_util.atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"after"
    assert os.getxattr(target, "user.bmad-loop-test") == b"kept"


def test_atomic_write_bytes_fsyncs_before_it_publishes(tmp_path, monkeypatch):
    """Same ordering pin as the text sibling: a durable rename over data still in the
    page cache comes back as a zero-length file, which for an exclude reads as "no
    patterns" — a shield that silently excludes nothing."""
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        platform_util.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        platform_util.os, "replace", lambda s, d: (order.append("replace"), real_replace(s, d))[1]
    )
    target = tmp_path / "exclude"
    target.write_bytes(b"before")

    platform_util.atomic_write_bytes(target, b"after")

    assert order == ["fsync", "replace"]  # the sync must precede the publish
    assert target.read_bytes() == b"after"


def test_atomic_write_bytes_leaves_no_temp_behind(tmp_path):
    target = tmp_path / "exclude"
    target.write_bytes(b"before")

    platform_util.atomic_write_bytes(target, b"after")

    assert [p.name for p in tmp_path.iterdir()] == ["exclude"]


def test_atomic_write_bytes_cleans_up_and_keeps_the_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "exclude"
    target.write_bytes(b"before")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(platform_util.os, "replace", boom)

    with pytest.raises(OSError):
        platform_util.atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"before"
    assert [p.name for p in tmp_path.iterdir()] == ["exclude"]  # no orphaned temp


def test_atomic_write_bytes_temp_name_is_unique_per_call(tmp_path, monkeypatch):
    target = tmp_path / "exclude"
    target.write_bytes(b"before")
    seen: list[str] = []
    real_replace = os.replace

    def record(src, dst):
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)

    platform_util.atomic_write_bytes(target, b"a")
    platform_util.atomic_write_bytes(target, b"b")

    assert len(set(seen)) == 2


def test_atomic_write_bytes_stages_in_the_targets_own_directory(tmp_path, monkeypatch):
    """The temp has to be a SIBLING of the target, and this pins the shared tail for
    both variants. `os.replace` cannot cross a filesystem, and the default temp dir
    is a different mount on plenty of boxes (always, on a runner with a tmpfs
    `/tmp`) — so a temp staged there fails the publish with EXDEV *after* the
    content is written, turning an atomic write into a guaranteed one.

    Recorded from `os.replace`'s source argument, because the pre-existing
    "leaves no temp behind" assertion cannot see this: it lists the TARGET's
    directory, which a temp staged in `/tmp` is trivially absent from — with
    `dir=` dropped, that assertion still passes."""
    target = tmp_path / "sub" / "exclude"
    target.parent.mkdir()
    target.write_bytes(b"before")
    staged: list[str] = []
    real_replace = os.replace

    def record(src, dst):
        staged.append(os.path.dirname(str(src)))
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)

    platform_util.atomic_write_bytes(target, b"after")

    assert staged == [str(target.parent)]
    assert target.read_bytes() == b"after"


def test_atomic_write_bytes_creates_a_missing_target_at_the_private_mode(tmp_path):
    """A target that does not exist yet is created at `mkstemp`'s 0600, not the umask
    default — the shared contract's deliberate choice, and the reason
    `_worktree_local_exclude` `touch()`es the exclude before calling this: it needs
    the file to already exist so a readable mode is what gets carried over."""
    target = tmp_path / "exclude"

    platform_util.atomic_write_bytes(target, b"fresh")

    assert target.read_bytes() == b"fresh"
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pathconf(PC_NAME_MAX)")
def test_atomic_write_bytes_stages_a_basename_that_fills_name_max(tmp_path, monkeypatch):
    """A target whose basename is the longest the filesystem allows still writes
    (#595). `mkstemp` inserts 8 random chars between prefix and suffix, so the temp
    runs `len(basename) + 13` — meaning a target that is itself perfectly legal
    produced an ILLEGAL temp name and the write died with ENAMETOOLONG.

    A regression, not a pre-existing limit: the direct `write_bytes` that
    `set_frontmatter_status`/`set_frontmatter_field` used before this branch
    accepted the same name, and specs are named by BMAD's planning skills, not by
    anything here that bounds them (`safe_segment`'s MAX_SEGMENT caps story keys
    and run-dir segments, not spec basenames).

    Sized from the filesystem's own `PC_NAME_MAX` rather than a hardcoded 255,
    which is per-filesystem — on a box where it is smaller a fixed 252 would fail
    while CREATING the fixture, reddening this row for a reason that is not the
    property.

    The staged name is recorded and asserted legal rather than compared to the
    digest: what has to hold is that the temp fits, not which scheme produced it.

    Ablation: delete the `except OSError` fallback in `_mkstemp_beside` and this
    fails alone, on `OSError: [Errno 36] File name too long` raised before any
    assertion. `..._temp_name_is_unique_per_call` and
    `..._stages_in_the_targets_own_directory` stay green under it — both use short
    names, which never reach the fallback."""
    name_max = os.pathconf(str(tmp_path), "PC_NAME_MAX")
    target = tmp_path / ("s" * (name_max - len(".md")) + ".md")
    assert len(os.fsencode(target.name)) == name_max  # PRECONDITION: the limit itself
    staged: list[str] = []
    real_replace = os.replace

    def record(src, dst):
        staged.append(os.path.basename(str(src)))
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", record)

    platform_util.atomic_write_bytes(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert len(staged) == 1
    assert len(os.fsencode(staged[0])) <= name_max
    assert staged[0].endswith(".tmp")  # devcontract's *.md scans must skip it
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pathconf(PC_NAME_MAX)")
def test_confined_write_stages_a_basename_that_fills_name_max(tmp_path, monkeypatch):
    """#595's guarantee, carried onto the anchored arm. The anchored staging name
    appends `.<pid hex>.<8 hex>.tmp` to the full target name and originally
    retried nothing but `FileExistsError`, so a basename the target itself is
    perfectly legal at raised `ENAMETOOLONG` through the confined writer — while
    the plain no-follow writer those callers were moved OFF OF staged the same
    spec through `_mkstemp_beside`'s digest fallback. Both arms now walk the one
    `_stage_shortening` ladder, and this row is the anchored twin of the
    path-based row above.

    Reachable exactly where it hurts: specs are named by BMAD's planning skills,
    nothing bounds that name, and an in-checkout spec routes to this arm.

    The staged name is recorded and asserted legal rather than compared to the
    digest, for the sibling row's reason: what has to hold is that the temp fits,
    not which rung produced it.

    Ablation: bypass the ladder — have `_atomic_write_at` call
    `_open_exclusive_at(dir_fd, name + ".", name)` directly — and this reddens
    alone, on `OSError: [Errno 36] File name too long` raised before any
    assertion. The path-based row above stays green under it, which is the split
    that proves the two arms stage independently."""
    root = tmp_path / "checkout"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    name_max = os.pathconf(str(artifacts), "PC_NAME_MAX")
    target = artifacts / ("s" * (name_max - len(".md")) + ".md")
    assert len(os.fsencode(target.name)) == name_max  # PRECONDITION: the limit itself
    staged: list[str] = []
    real_replace = os.replace

    def record(src, dst, **kwargs):
        staged.append(os.path.basename(str(src)))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(platform_util.os, "replace", record)

    platform_util.atomic_write_bytes_confined(target, b"payload", confine_root=root)

    assert target.read_bytes() == b"payload"
    assert len(staged) == 1
    assert len(os.fsencode(staged[0])) <= name_max
    assert staged[0].endswith(".tmp")  # devcontract's *.md scans must skip it
    assert list(artifacts.glob("*.tmp")) == []


def _exceed_range() -> OSError:
    """Win32's "the name does not fit", which arrives as ENOENT and is told apart
    from a missing directory only by `.winerror` — see `_is_name_too_long`."""
    exceeded = OSError(errno.ENOENT, "The filename or extension is too long")
    exceeded.winerror = 206  # pyright: ignore[reportAttributeAccessIssue]
    return exceeded


def _too_long() -> OSError:
    """POSIX's "the name does not fit" — the errno arm of `_is_name_too_long`."""
    return OSError(errno.ENAMETOOLONG, "File name too long")


def _failing_mkstemp(prefixes: list[str], fail_first: int, error=_exceed_range):
    """A `mkstemp` that records each prefix it is handed and refuses the first
    `fail_first` attempts with a too-long error, then delegates.

    `error` defaults to win32's spelling, which is what the retry rows above need;
    pass `_too_long` for the POSIX one (see the #596 floor row)."""
    real_mkstemp = tempfile.mkstemp

    def fake_mkstemp(*, dir, prefix, suffix):
        prefixes.append(prefix)
        if len(prefixes) <= fail_first:
            raise error()
        return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    return fake_mkstemp


def test_atomic_write_bytes_stages_via_digest_when_win32_reports_exceed_range(
    tmp_path, monkeypatch
):
    """The #595 fallback also fires for win32's spelling of "the name does not
    fit", which is NOT ENAMETOOLONG.

    CPython's `PC/errmap.h` maps `ERROR_FILENAME_EXCED_RANGE` (206) to ENOENT, and
    does not map `ERROR_BUFFER_OVERFLOW` (111) at all — it falls to the EINVAL
    default. So on Windows nothing raises ENAMETOOLONG and only `.winerror`
    carries the distinction: keying the retry on the errno alone left the whole
    fallback DEAD on the one platform where, per `MAX_PATH`, the staged name is
    easiest to overflow.

    Driven through an injected error rather than a real long name because this
    runs on POSIX too, where no path produces winerror 206 — and a
    `skipif(win32)` row would leave the branch unexercised on every CI leg but
    two. The injection is the mechanism the code actually reads
    (`_is_name_too_long`), so it is the predicate under test, not a stand-in.

    The basename is deliberately LONGER than a 16-character digest, which is what
    puts the digest rung on the ladder at all — see the row below for the short
    basename that skips it.

    Asserts the retry STRICTLY SHORTENED rather than matching the digest: what
    has to hold is that the next attempt is narrower than the one that failed,
    not which scheme produced it.

    Ablation: drop the `.winerror` arm of `_is_name_too_long` and all THREE
    win32-injected rows redden together, on the injected `OSError` propagating out
    of `atomic_write_bytes` before any assertion — they share that predicate, so
    no one of them can fail alone under it. The disjointness proof is the row that
    stays GREEN: POSIX `..._fills_name_max` arrives on the errno arm, which this
    ablation leaves intact, so the two arms genuinely cover different conditions
    rather than one masking the other."""
    target = tmp_path / ("s" * 40 + ".md")
    prefixes: list[str] = []
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", _failing_mkstemp(prefixes, 1))

    platform_util.atomic_write_bytes(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert len(prefixes) == 2  # the retry happened at all
    assert prefixes[0] == target.name + "."
    assert prefixes[1] != ""  # the DIGEST rung, not the bare one
    assert len(prefixes[1]) < len(prefixes[0])  # strictly shorter than what failed
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_bytes_skips_the_digest_rung_when_it_would_not_shorten(tmp_path, monkeypatch):
    """A digest is 16 characters, so against a basename that short or shorter it
    is not a fallback at all — it stages a name no NARROWER than the one that
    just failed, and a retry at the same width cannot succeed.

    Unreachable where the binding limit is per-component: a POSIX `NAME_MAX` rung
    is only reached past a 242-character basename, far above the digest. Reachable
    where the limit is the whole path, which is the win32 `MAX_PATH` case — a
    short basename in a deep directory overflows on the staging suffix alone, and
    a 29-character digest temp is then LONGER than the readable one it replaced.

    So the ladder drops that rung and goes straight to a bare `mkstemp`, the
    shortest name this function can produce.

    Ablation: append the digest rung unconditionally and this reddens alone, on
    `prefixes[1] == ""` — the digest is attempted for a 7-character basename it
    cannot help. The long-basename row above stays green, because there the
    digest genuinely shortens and the rung belongs on the ladder."""
    target = tmp_path / "spec.md"  # 7 chars — a 16-char digest is no improvement
    prefixes: list[str] = []
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", _failing_mkstemp(prefixes, 1))

    platform_util.atomic_write_bytes(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert prefixes[0] == "spec.md."
    assert len(prefixes) == 2  # no wasted attempt at a name that cannot fit
    assert prefixes[1] == ""  # straight to the shortest name available
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_bytes_ends_the_ladder_at_a_bare_temp(tmp_path, monkeypatch):
    """When the digest rung ALSO cannot fit, the ladder still has one rung left.

    This is the case that makes the docstring's closing claim true. Keying the
    fallback on a single digest retry meant a second failure was reported as "the
    directory is too long, which no prefix can fix" while a shorter prefix — the
    empty one — would in fact have fit. Only once the last rung carries no prefix
    at all is a failure there genuinely about the directory.

    Ablation: delete the trailing bare rung so the ladder ends at the digest and
    TWO rows redden — this one, on the second injected `OSError` propagating, and
    the skip row above. Measured, not assumed: the bare rung is the fallback for
    both of them, since a basename too short for the digest has no other rung to
    fall to. They stay separate rows because they reach it for different reasons —
    one because the digest was skipped, one because the digest was tried and also
    failed — and only this one pins that a THIRD attempt exists at all."""
    target = tmp_path / ("s" * 40 + ".md")
    prefixes: list[str] = []
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", _failing_mkstemp(prefixes, 2))

    platform_util.atomic_write_bytes(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert len(prefixes) == 3
    assert prefixes[2] == ""  # the shortest name the function can stage
    # every rung strictly narrower than the one it replaced
    assert [len(p) for p in prefixes] == sorted((len(p) for p in prefixes), reverse=True)
    assert len(set(prefixes)) == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_mkstemp_beside_floor_failure_propagates_loudly(tmp_path, monkeypatch):
    """#596: a DESIGNED limit, pinned so a future "fix" has to re-argue the trade
    rather than quietly take it.

    `mkstemp` will not generate a name below 12 characters — 8 random plus the
    `.tmp` this helper needs to stay out of `devcontract`'s `*.md` scans — so once
    the ladder reaches its bare rung, what does not fit is the directory PLUS those
    12. A target with a shorter basename can still clear a direct write there, which
    is why the refusal is narrower than "the directory is too long". Going below the
    floor means abandoning `mkstemp`, and with it the entropy that keeps a staged
    name unguessable where a less-trusted writer can reach it (#591 is the cost of a
    guessable temp name). The recorded decision is to keep the entropy and let the
    last rung raise.

    So this row asserts the LOUDNESS, not a repair: the error propagates verbatim,
    nothing is staged, and the target keeps its old bytes. Any change that swallows
    or works around the floor reddens it.

    All three rungs fail with the POSIX spelling here — the win32 arm is already
    covered by the rows above, and #596's floor is stated in characters, which is
    the POSIX per-component limit's currency."""
    target = tmp_path / ("s" * 40 + ".md")
    target.write_bytes(b"original")
    prefixes: list[str] = []
    monkeypatch.setattr(
        platform_util.tempfile, "mkstemp", _failing_mkstemp(prefixes, 3, error=_too_long)
    )

    with pytest.raises(OSError) as caught:
        platform_util.atomic_write_bytes(target, b"payload")

    assert caught.value.errno == errno.ENAMETOOLONG  # the real error, not a substitute
    assert len(prefixes) == 3  # the whole ladder was walked before giving up
    assert prefixes[-1] == ""  # and it gave up at the bare rung, the shortest there is
    assert target.read_bytes() == b"original"  # the target is untouched by a refusal
    assert list(tmp_path.glob("*.tmp")) == []  # nothing staged survives it


def test_atomic_write_bytes_propagates_a_staging_error_that_is_not_a_long_name(
    tmp_path, monkeypatch
):
    """The retry stays NARROW: an `OSError` that is not "the name does not fit"
    propagates on the first attempt instead of being retried under a digest.

    The negative control for the row above. Widening the predicate to a bare
    `except OSError` — or folding in `ERROR_INVALID_NAME` (123), which also fires
    for characters win32 forbids outright and no shorter prefix can rescue — would
    turn an unrelated staging failure into a second doomed `mkstemp` and surface
    the RETRY's exception rather than the real one. `install.py` already assigns
    123 the opposite meaning (`_ABSENCE_WINERRORS`), so admitting it here would
    make one winerror mean two things in one codebase.

    Ablation: relax the guard so nothing re-raises and this reddens alone, on
    `len(calls) == 1` reading 2 — the second, doomed `mkstemp` runs under the next
    rung, which for this 7-character basename is the BARE one (a 16-character
    digest cannot shorten it). Note which assertion does NOT catch it:
    `caught.value.errno ==
    EACCES` still passes, because the retry fails the same way the first attempt
    did. The call count is the load-bearing assertion here; an errno check alone
    would pass through the widened guard and pin nothing."""
    target = tmp_path / "spec.md"
    calls: list[str] = []

    def fake_mkstemp(*, dir, prefix, suffix):
        calls.append(prefix)
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(platform_util.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(OSError) as caught:
        platform_util.atomic_write_bytes(target, b"payload")

    assert caught.value.errno == errno.EACCES  # the REAL error, not the retry's
    assert len(calls) == 1  # never retried
    assert not target.exists()


# --------------------------------------------------------------- retrying_unlink


def test_retrying_unlink_retries_then_succeeds(tmp_path, monkeypatch):
    # Windows denies a delete against an open handle exactly as it denies a
    # rename-over, so the second half of a staged move needs the same backoff.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    victim = tmp_path / "spec.md"
    victim.write_text("x", encoding="utf-8")
    calls = {"n": 0}
    real_unlink = os.unlink

    def flaky_unlink(path, **_kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(32, "The process cannot access the file")
        real_unlink(path)

    monkeypatch.setattr(platform_util.os, "unlink", flaky_unlink)
    platform_util.retrying_unlink(victim)

    assert calls["n"] == 3
    assert len(sleeps) == 2
    assert not victim.exists()


def test_retrying_unlink_no_retry_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "linux")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    def denied(_path, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(platform_util.os, "unlink", denied)
    victim = tmp_path / "spec.md"
    victim.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError):
        platform_util.retrying_unlink(victim)
    assert sleeps == []  # a real POSIX error surfaces at once


def test_retrying_unlink_propagates_missing_file(tmp_path):
    # not a sharing violation — no retry, no swallow
    with pytest.raises(FileNotFoundError):
        platform_util.retrying_unlink(tmp_path / "gone.md")


# --------------------------------------------------------------------- file_lock


def test_file_lock_excludes_second_acquirer(tmp_path):
    """While held, a second (non-blocking) acquisition on the same path fails —
    the deterministic exclusion probe, no sleep-based negative assertion. Runs
    the fcntl branch on POSIX and the msvcrt branch on the Windows CI leg."""
    lock = tmp_path / "state.json.lock"
    with platform_util.file_lock(lock):
        with pytest.raises(OSError):
            with platform_util.file_lock(lock, blocking=False):
                pass  # pragma: no cover — must not be reached
    # Released on exit: the probe now succeeds.
    with platform_util.file_lock(lock, blocking=False):
        pass


def test_file_lock_creates_parent_and_lock_file(tmp_path):
    lock = tmp_path / "deep" / "nested" / "s.lock"
    with platform_util.file_lock(lock):
        assert lock.exists()


def test_file_lock_reentry_after_exception(tmp_path):
    """An exception inside the critical section still releases the lock."""
    lock = tmp_path / "s.lock"
    with pytest.raises(RuntimeError):
        with platform_util.file_lock(lock):
            raise RuntimeError("boom")
    with platform_util.file_lock(lock, blocking=False):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes; Windows has no umask")
def test_file_lock_is_created_owner_only(tmp_path):
    """The lock's mode is stated by the code, not inherited from whoever ran first.

    A repository shared between OS users is refused before the shield ever takes
    this lock (`install._shield_shared_repository`, #384), so owner-only is the
    whole policy — but leaving `os.open` mode-less would still make the mode a
    property of the creator's umask rather than a decision: measured, 022 yields
    0o755 and 077 yields 0o700.

    `os.umask(0o022)` is the point of the fixture, not hygiene. At this box's own
    0o077 the mode-less code produces 0o600 by accident and the ablation does not
    bite — the same trap as
    `test_install.py::test_worktree_local_exclude_created_exclude_stays_readable`.

    Ablation (run): drop the `0o600` argument from `file_lock`'s `os.open` and this
    fails reporting 0o755."""
    lock = tmp_path / "s.lock"
    previous = os.umask(0o022)
    try:
        with platform_util.file_lock(lock):
            pass
    finally:
        os.umask(previous)

    assert stat.S_IMODE(lock.stat().st_mode) == 0o600, oct(stat.S_IMODE(lock.stat().st_mode))


# ------------------------------------------------------------------ safe_segment


def _is_legal_segment(seg: str) -> bool:
    return (
        bool(seg)
        and len(seg) <= platform_util.MAX_SEGMENT
        and not platform_util._ILLEGAL_SEGMENT_CHARS.search(seg)
        and not seg.endswith((" ", "."))
        and not platform_util._is_reserved_basename(seg)
    )


@pytest.mark.parametrize(
    "value", ["3-2-digest-delivery", "epic1_story2", "a.b.c", "plain", "console"]
)
def test_safe_segment_identity_for_clean_input(value):
    # a legal segment (incl. the non-reserved 'console') is returned byte-identical
    assert platform_util.safe_segment(value) == value


@pytest.mark.parametrize(
    "value, base",
    [
        ('a<b>c:"d/e\\f|g?h*i', "a_b_c__d_e_f_g_h_i"),  # every illegal char -> _ (`:"` = two)
        ("with\ttab", "with_tab"),  # control char
        ("x.", "x"),  # trailing dot stripped
        ("y ", "y"),  # trailing space stripped
        ("CON", "_CON"),  # reserved basename
        ("nul", "_nul"),  # case-insensitive
        ("COM1.txt", "_COM1.txt"),  # reserved even with extension
        ("LPT9", "_LPT9"),
        ("COM0", "_COM0"),  # COM0/LPT0 are reserved too
        ("CON .txt", "_CON .txt"),  # reserved stem with a trailing space before the extension
        ("CONIN$", "_CONIN$"),  # console device names are reserved ($ is otherwise legal)
        ("conout$.log", "_conout$.log"),  # case-insensitive, with extension
    ],
)
def test_safe_segment_coerces_and_suffixes_changed_input(value, base):
    out = platform_util.safe_segment(value)
    assert out != value
    assert out.startswith(base + "-")  # sanitized base + collision-suffix digest
    assert _is_legal_segment(out)


def test_safe_segment_distinct_dirty_keys_never_collide():
    # same sanitized base but different raw input must not share a segment (would
    # otherwise cross-wire two stories' task dirs / logs / feedback files)
    a = platform_util.safe_segment("a:b")
    b = platform_util.safe_segment("a?b")
    assert a.startswith("a_b-") and b.startswith("a_b-")
    assert a != b


def test_safe_segment_caps_length():
    out = platform_util.safe_segment("x" * 500)
    assert len(out) <= platform_util.MAX_SEGMENT
    assert _is_legal_segment(out)


def test_dirty_story_key_segment_is_creatable(tmp_path):
    # the sanitized segment a consumer builds a dir from must be creatable on this OS
    from bmad_loop import resolve

    d = resolve._story_dir(tmp_path, 'a<b>:c."')
    d.mkdir(parents=True)
    assert d.is_dir()


# -------------------------------------------------------------- safe_ref_segment

# Raw keys spanning every rule class, shared by the property tests and the git
# oracle. Only the sanitizer's *output* is ever handed to git, so NUL/DEL/tab in
# here never reach a subprocess argv.
_REF_CORPUS = [
    # clean — must survive the oracle byte-identical
    "3-2-digest-delivery",
    "epic1_story2",
    "a.b.c",
    "plain",
    "CON",
    "-leading-dash",
    "a<b>c",
    'a"b|c',
    "a]b",
    "@@",
    "é-ünïcødé",
    # one per coercion rule
    "a:b",
    "a b",
    "a~b",
    "a^b",
    "a?b",
    "a*b",
    "a[b",
    "a\\b",
    "a/b",
    "with\ttab",
    "a\x7fb",
    "a\x00b",
    "a..b",
    "a@{b",
    ".hidden",
    "x.",
    "a.lock",
    "@",
    "",
    "x" * 500,
    # adversarial combinations
    "...",
    "....",
    ".lock",
    "..lock",
    "a.lock.lock",
    "@{u}",
    "refs/heads/x",
    "/lead",
    "trail/",
    "a//b",
    "  ",
    "story/1:2..3@{now}.lock",
]


@pytest.mark.parametrize(
    "value",
    ["3-2-digest-delivery", "epic1_story2", "a.b.c", "plain", "CON", "-leading-dash", "a<b>c"],
)
def test_safe_ref_segment_identity_for_clean_input(value):
    # git's alphabet is not Windows': `CON` and `a<b>c` are ref-legal (safe_segment
    # rewrites both), and a leading `-` is legal inside the always-prefixed branch.
    assert platform_util.safe_ref_segment(value) == value


@pytest.mark.parametrize(
    "value, base",
    [
        ("a:b", "a_b"),  # colon
        ("a b", "a_b"),  # space
        ("a~b", "a_b"),
        ("a^b", "a_b"),
        ("a?b", "a_b"),
        ("a*b", "a_b"),
        ("a[b", "a_b"),
        ("a\\b", "a_b"),
        ("a/b", "a_b"),  # would split one component into two
        ("with\ttab", "with_tab"),  # control char
        ("a\x7fb", "a_b"),  # DEL
        ("a..b", "a__b"),  # ref-illegal, filename-legal
        ("a@{b", "a_{b"),
        (".hidden", "_hidden"),  # leading dot
        ("x.", "x."),  # trailing dot: no rewrite, the digest suffix is the fix
        ("a.lock", "a.lock"),  # trailing .lock: ditto
        ("@", "_"),  # lone @
        ("", "_"),
    ],
)
def test_safe_ref_segment_coerces_and_suffixes_changed_input(value, base):
    out = platform_util.safe_ref_segment(value)
    assert out != value
    assert out.startswith(base + "-")  # sanitized base + collision-suffix digest


def test_safe_ref_segment_distinct_dirty_keys_never_collide():
    # `a..b` and `a//b` sanitize to the same base — the digest keeps their unit
    # branches (and so their merge targets) distinct
    a = platform_util.safe_ref_segment("a..b")
    b = platform_util.safe_ref_segment("a//b")
    assert a.startswith("a__b-") and b.startswith("a__b-")
    assert a != b


def test_safe_ref_segment_caps_length():
    assert len(platform_util.safe_ref_segment("x" * 500)) <= platform_util.MAX_SEGMENT


@pytest.mark.parametrize("value", _REF_CORPUS)
@pytest.mark.parametrize(
    "template",
    [
        "bmad-loop/rid/{}",  # unit_key, branch_per=story
        "bmad-loop/{}/1-1-a",  # run_id, branch_per=story
        "bmad-loop/{}",  # run_id, branch_per=run
    ],
    ids=["unit_key", "run_id", "run_id_shared"],
)
def test_safe_ref_segment_output_passes_git_check_ref_format(value, template):
    """Oracle: git itself validates every sanitized segment, in each position
    `workspace.unit_branch_name` actually places it. Pure-Python sanitization is
    only as good as its agreement with `git check-ref-format`."""
    branch = template.format(platform_util.safe_ref_segment(value))
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{value!r} -> {branch!r}: {proc.stderr.strip()}"


# ----------------------------------------------------- resolve_or_lexical (#552)

# One string, asserted on, so a row proves the *cause* reached stderr rather than
# just that some note did.
_REFUSAL = "stubbed: the provider is registered but not serving"


@pytest.fixture
def unnoted(monkeypatch):
    """A clean note-dedupe set. The real one is module state that lives as long as the
    process, so without this the second test to degrade the same path in a session
    would assert on a note the first one already consumed."""
    monkeypatch.setattr(platform_util, "_LEXICAL_FALLBACK_NOTED", set())


def _refusing_resolve(exc):
    def stub(self, strict=False):
        raise exc

    return stub


@pytest.mark.parametrize(
    "exc",
    [
        # What a registered-but-not-serving WSL UNC provider answers. 64 is *off*
        # ntpath's non-strict allow-list, so resolve() raises rather than falling
        # back to its own lexical walk — the whole reason this helper exists.
        OSError(0, _REFUSAL, None, 64),
        # resolve() raises this, not an OSError, for a symlink loop on the 3.11/3.12
        # floor. A guard that caught OSError alone would be a floor-only hole.
        RuntimeError(_REFUSAL),
    ],
    ids=["oserror-winerror-64", "runtimeerror-symlink-loop"],
)
def test_resolve_or_lexical_degrades_when_the_os_refuses(exc, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(platform_util, "_LEXICAL_FALLBACK_NOTED", set())
    monkeypatch.setattr(Path, "resolve", _refusing_resolve(exc))

    got = platform_util.resolve_or_lexical(tmp_path / "a" / ".." / "b")

    # `..` is *kept*, not collapsed: see the rejected-normpath note on the helper —
    # folding it lexically names a different directory across a symlink, and this
    # value is persisted as state.project and reused as a repo root and a cwd.
    assert got == tmp_path / "a" / ".." / "b"
    assert got.is_absolute()
    captured = capsys.readouterr()
    assert captured.out == ""  # `<cmd> --json` is a one-object-on-stdout contract
    assert _REFUSAL in captured.err, "the note must carry the cause, not just its own text"
    assert "cannot canonicalize" in captured.err


def test_resolve_or_lexical_keeps_a_relative_path_relative_to_the_cwd(monkeypatch, capsys, unnoted):
    """`--project` defaults to `"."`, so the degraded path is the common case, not an
    edge one. `absolute()` is what supplies the root that `resolve()` would have."""
    monkeypatch.setattr(Path, "resolve", _refusing_resolve(OSError(0, _REFUSAL, None, 64)))
    assert platform_util.resolve_or_lexical(".") == Path.cwd()


def test_resolve_or_lexical_notes_once_per_process(monkeypatch, capsys, tmp_path, unnoted):
    """One condition, one line. A single invocation canonicalizes the project root at
    least three times — `main()`'s pre-dispatch `_configure_mux`, the handler's own
    `_project`, then `load_paths` — and three copies of one note reads as three
    faults."""
    monkeypatch.setattr(Path, "resolve", _refusing_resolve(OSError(0, _REFUSAL, None, 64)))

    for _ in range(3):
        platform_util.resolve_or_lexical(tmp_path)

    assert capsys.readouterr().err.count("cannot canonicalize") == 1


def test_resolve_or_lexical_notes_each_distinct_path(monkeypatch, capsys, tmp_path, unnoted):
    """The dedupe is per path, not a one-shot latch: a second, different path that also
    degrades is a second thing the operator has not been told about."""
    monkeypatch.setattr(Path, "resolve", _refusing_resolve(OSError(0, _REFUSAL, None, 64)))

    platform_util.resolve_or_lexical(tmp_path / "a")
    platform_util.resolve_or_lexical(tmp_path / "b")

    assert capsys.readouterr().err.count("cannot canonicalize") == 2


def test_resolve_or_lexical_prefers_the_real_resolve(tmp_path, capsys, unnoted):
    """The fallback is a fallback. On a working OS this is `Path.resolve()` — symlink
    dereference included — and it says nothing. Without the second assertion the row
    would still pass if the helper had degraded, since both answers are absolute."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as e:  # Windows without SeCreateSymbolicLink / developer mode
        pytest.skip(f"cannot create a symlink here: {e}")

    got = platform_util.resolve_or_lexical(link)

    assert got == real.resolve()
    assert got != link.absolute(), "took the lexical branch on a host that can resolve"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "spelling",
    [
        "\\\\wsl.localhost\\Ubuntu-24.04\\home",
        "\\\\wsl$\\Ubuntu-24.04\\home",
        "//wsl.localhost/Ubuntu-24.04/home",
        "\\\\?\\UNC\\wsl.localhost\\Ubuntu-24.04\\home",
    ],
)
def test_the_lexical_fallback_keeps_every_bridge_spelling_matchable(spelling):
    """The premise the whole degrade rests on, pinned platform-blind so a Linux run
    catches a regression too. `absolute()` returns an already-absolute path untouched,
    so `is_wsl_unc_path` — the #332 predicate, and the reason these commands must live
    long enough to run — still matches what the fallback hands it. Uses the pure
    Windows flavour because the real `absolute()` needs a Windows host; the flavour is
    what decides `is_absolute` and the separator fold, which is the whole claim."""
    pure = PureWindowsPath(spelling)
    assert pure.is_absolute(), "absolute() would prepend a POSIX cwd and destroy the prefix"
    assert platform_util.is_wsl_unc_path(pure) is True


class _ReparseStat:
    """Stand-in for the os.lstat() result of a Windows junction: a DIRECTORY
    mode (which is why Path.is_symlink() answers False) carrying a reparse tag."""

    st_mode = stat.S_IFDIR | 0o755
    st_reparse_tag = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


def test_is_link_like_refuses_a_reparse_tagged_dir(tmp_path, monkeypatch):
    """A Windows directory junction redirects but is NOT a symlink.

    `Path.is_symlink()` is False for a junction while `mkdir`/`os.open` follow
    it, and `mklink /J` needs no elevation at all — unlike a directory symlink,
    which needs SeCreateSymbolicLinkPrivilege or Developer Mode. So the junction
    is the CHEAPER attack and the one an is_symlink() check misses. The refusal
    keys on the reparse tag instead.

    That branch is reachable only on Windows; drive its logic here so it does not
    ship unexercised (the `stat.IO_REPARSE_TAG_*` constants do not exist on
    POSIX, hence the substituted tuple).

    Ablation guard: dropping the `st_reparse_tag` arm of `is_link_like` makes the
    last assertion fail — verified.
    """
    plain = tmp_path / "verify"
    plain.mkdir()
    assert platform_util.is_link_like(plain) is False  # positive control

    real_lstat = os.lstat
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p, *a, **k: _ReparseStat() if str(p) == str(plain) else real_lstat(p),
    )
    assert platform_util.is_link_like(plain) is True


def test_walk_files_unlinked_prunes_a_link_like_subdirectory(tmp_path, monkeypatch):
    """A nested redirect is pruned even where ``os.walk`` would descend into it.

    ``os.walk`` prunes a symlinked subdirectory by itself, so on POSIX this guard
    looks redundant — which is exactly the trap. It prunes via ``os.path.islink``,
    and a Windows DIRECTORY JUNCTION is not a symlink, so the arm that actually
    needs pruning is the one ``os.walk`` misses, and it is unreachable from a
    POSIX runner. The junction is therefore simulated by making ``is_link_like``
    answer True for an ordinary directory: that disagreement between the two
    predicates IS the win32 behaviour under test, and a real symlink would grade
    ``os.walk`` instead of this function.

    Ablation: delete the ``dirs[:]`` pruning line and `theirs.bin` joins the
    result — 9000 bytes from a tree the caller never meant to walk. Verified.
    """
    root = tmp_path / "run"
    (root / "keep").mkdir(parents=True)
    (root / "keep" / "mine.bin").write_bytes(b"m" * 10)
    junction = root / "verify"
    junction.mkdir()
    (junction / "theirs.bin").write_bytes(b"t" * 9000)

    monkeypatch.setattr(platform_util, "is_link_like", lambda q: Path(q) == junction)

    assert sorted(q.name for q in platform_util.walk_files_unlinked(root)) == ["mine.bin"]


def test_walk_files_unlinked_refuses_a_link_like_top(tmp_path, monkeypatch):
    """The other half: ``os.walk`` always follows the top path it is handed, so
    declining to descend into links says nothing about the root itself. Same
    simulation, and the two halves fail independently — pruning children cannot
    save a caller who was pointed at the redirect to begin with."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "theirs.bin").write_bytes(b"t" * 9000)

    monkeypatch.setattr(platform_util, "is_link_like", lambda q: Path(q) == outside)

    assert list(platform_util.walk_files_unlinked(outside)) == []


# ------------------------------------------------------ open_dir_confined (#593)
#
# First direct coverage of this helper and of `atomic_write_text_at`: both shipped
# for `tui/launch.py` and were graded only through that consumer, so a property
# neither consumer happens to exercise had nothing pinning it. #593 adopts them
# across a dozen more sites, which is a bad time to be inferring the contract.

DIR_FD = pytest.mark.skipif(
    not platform_util.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only"
)


@DIR_FD
def test_open_dir_confined_returns_a_descriptor_for_a_clean_chain(tmp_path):
    """The positive control the refusals below need: an unplanted chain hands back
    a descriptor for the directory that was asked for, not merely a non-None int.

    Identified by inode rather than by name, because a name is the one thing this
    helper deliberately stops consulting."""
    root = tmp_path / "project"
    nested = root / ".bmad-loop" / "runs"
    nested.mkdir(parents=True)

    fd = platform_util.open_dir_confined(root, nested)

    assert fd is not None
    try:
        assert os.fstat(fd).st_ino == nested.stat().st_ino
    finally:
        os.close(fd)


@DIR_FD
def test_open_dir_confined_refuses_a_symlinked_component(tmp_path):
    """A link at ANY component below the root fails the walk, not just at the last
    one. That is the whole of #593: `follow_symlinks=False` refuses a link at the
    final name while `mkstemp(dir=...)` still resolves every directory above it by
    name, so a link planted at `.bmad-loop/` redirects the write and the no-follow
    buys nothing."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "runs").mkdir(parents=True)
    (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    assert platform_util.open_dir_confined(root, root / ".bmad-loop" / "runs") is None


@DIR_FD
def test_open_dir_confined_refuses_a_target_outside_the_root(tmp_path):
    """Confinement is refused before a single directory is opened: a target that is
    not under the root has no chain to walk, however clean its own ancestry is."""
    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert platform_util.open_dir_confined(root, elsewhere) is None


@DIR_FD
def test_open_dir_confined_refuses_a_missing_component(tmp_path):
    """An absent directory is refused rather than created. The confined writers
    lean on this: they require the parent to EXIST, because a walk cannot vouch
    for a component that is not there yet, and a helper that made one would be
    deciding the mode and the ancestry of a directory nobody checked."""
    root = tmp_path / "project"
    root.mkdir()

    assert platform_util.open_dir_confined(root, root / "absent" / "deeper") is None


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_open_dir_confined_accepts_a_root_behind_a_link(tmp_path):
    """The root itself is opened WITHOUT `O_NOFOLLOW`, and that asymmetry is the
    design: the operator chooses where the project lives and may keep it behind a
    link (a checkout under a symlinked home, a `/var` that is really `/private/var`
    on macOS), while everything below it is session-writable.

    Ablation: add `O_NOFOLLOW` to the root open and this fails — the walk refuses a
    perfectly ordinary layout, which would take the confined writers with it."""
    real = tmp_path / "real-project"
    (real / ".bmad-loop").mkdir(parents=True)
    root = tmp_path / "project"
    root.symlink_to(real, target_is_directory=True)

    fd = platform_util.open_dir_confined(root, root / ".bmad-loop")

    assert fd is not None
    try:
        assert os.fstat(fd).st_ino == (real / ".bmad-loop").stat().st_ino
    finally:
        os.close(fd)


# -------------------------------------------------- anchored atomic writes (#593)


@contextmanager
def _dir_fd(directory: Path):
    """The descriptor the anchored helpers take, closed on the way out."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_text_at_lands_a_private_mode(tmp_path):
    """The anchored write states its mode rather than inheriting the umask's.

    These records live under a session-writable root, so a umask-derived 0644 is
    the difference between a private record and one the session that plants links
    at it can also read. `os.umask(0o022)` is the point of the bracket, not
    hygiene: under a 0o077 umask a mode-less create yields 0o600 by accident and
    the ablation goes inert (the trap `test_file_lock_is_created_owner_only`
    documents — and note that docstring's "this box is 0o077" claim is not
    universal, which is exactly why the value is forced here).

    Ablation: drop the `0o600` argument from the `os.open` in `_open_exclusive_at`
    and this fails reporting 0o644."""
    previous = os.umask(0o022)
    try:
        with _dir_fd(tmp_path) as fd:
            platform_util.atomic_write_text_at(fd, "record.json", '{"ok": true}')
    finally:
        os.umask(previous)

    landed = tmp_path / "record.json"
    assert landed.read_text(encoding="utf-8") == '{"ok": true}'
    assert stat.S_IMODE(landed.stat().st_mode) == 0o600, oct(landed.stat().st_mode)


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_bytes_at_lands_a_private_mode(tmp_path):
    """Mirrored from the text case rather than parametrized, for the reason the
    `atomic_write_bytes` banner gives: the two arms share a private tail that a
    later edit could split without either public name changing."""
    previous = os.umask(0o022)
    try:
        with _dir_fd(tmp_path) as fd:
            platform_util.atomic_write_bytes_at(fd, "policy.toml", b"backend = 'tmux'\n")
    finally:
        os.umask(previous)

    landed = tmp_path / "policy.toml"
    assert landed.read_bytes() == b"backend = 'tmux'\n"
    assert stat.S_IMODE(landed.stat().st_mode) == 0o600, oct(landed.stat().st_mode)


@DIR_FD
def test_atomic_write_bytes_at_preserves_crlf_verbatim(tmp_path):
    """The reason a bytes-anchored variant had to exist at all (#593).

    `atomic_write_text_at` is UTF-8 and text-mode; three of the sites adopting
    confinement — `policy.write_mux_backend` and the two frontmatter writers —
    read BYTES on purpose, so a CRLF file keeps its line endings. Routing them
    through the text helper would have rewritten every line ending in the file as
    a side effect of a hardening change.

    The payload carries a byte that is not valid UTF-8 in any position as well as
    CRLF, and deliberately: the CRLF half is only observable on win32 (POSIX text
    mode translates nothing on write), so on a POSIX runner an implementation that
    routed bytes through the text helper would pass a CRLF-only assertion. The
    invalid byte is what makes the ablation bite on both platforms.

    Ablation: reimplement this as `atomic_write_text_at(fd, name, data.decode())`
    and it fails `UnicodeDecodeError`."""
    payload = b"---\r\nstatus: caf\xe9\r\n---\r\n"  # latin-1 e-acute: not valid UTF-8
    with _dir_fd(tmp_path) as fd:
        platform_util.atomic_write_bytes_at(fd, "spec.md", payload)

    assert (tmp_path / "spec.md").read_bytes() == payload


@DIR_FD
def test_atomic_write_text_at_removes_its_temp_when_the_write_fails(tmp_path, monkeypatch):
    """A failed anchored write leaves the directory as it found it.

    The temp is created `O_EXCL` under an unguessable name, so a stranded one is
    not just litter: nothing ever reclaims it, and these directories are scanned.
    The cleanup unlinks dir_fd-relative, like everything else here."""
    target = tmp_path / "record.json"
    target.write_text("before", encoding="utf-8")

    def boom(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(platform_util.os, "fsync", boom)
    with _dir_fd(tmp_path) as fd:
        with pytest.raises(OSError, match="No space left"):
            platform_util.atomic_write_text_at(fd, "record.json", "after")

    assert target.read_text(encoding="utf-8") == "before"  # the original survived
    assert list(tmp_path.glob("*.tmp")) == []  # and nothing was stranded beside it


@DIR_FD
def test_atomic_write_bytes_at_removes_its_temp_when_the_write_fails(tmp_path, monkeypatch):
    """The bytes arm's own cleanup pin — mirrored, per the banner's reasoning."""
    target = tmp_path / "policy.toml"
    target.write_bytes(b"before")

    def boom(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(platform_util.os, "fsync", boom)
    with _dir_fd(tmp_path) as fd:
        with pytest.raises(OSError, match="No space left"):
            platform_util.atomic_write_bytes_at(fd, "policy.toml", b"after")

    assert target.read_bytes() == b"before"
    assert list(tmp_path.glob("*.tmp")) == []


# ------------------------------------------------------- path_is_confined (#593)


def test_path_is_confined_accepts_a_clean_chain(tmp_path):
    """The positive control every refusal below needs: without it they all pass
    for a predicate that answers False unconditionally."""
    root = tmp_path / "project"
    nested = root / ".bmad-loop" / "runs"
    nested.mkdir(parents=True)

    assert platform_util.path_is_confined(root, nested) is True
    assert platform_util.path_is_confined(root, root) is True  # the root is confined in itself


def test_path_is_confined_refuses_a_target_outside_the_root(tmp_path):
    """The lexical gate, before any component is probed."""
    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert platform_util.path_is_confined(root, elsewhere) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_path_is_confined_refuses_a_symlinked_component(tmp_path):
    """The win32 fallback's half of #593, exercised from POSIX so it is not left
    to the Windows legs alone: it still has to refuse an ancestor link, just with
    the weaker (racy, and documented as such) check-then-write guarantee."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    assert platform_util.path_is_confined(root, root / ".bmad-loop") is False


def test_path_is_confined_refuses_a_reparse_tagged_component(tmp_path, monkeypatch):
    """A Windows DIRECTORY JUNCTION redirects exactly as a symlink does, is False
    for `Path.is_symlink()`, and is the cheaper plant — `mklink /J` needs neither
    elevation nor Developer Mode. A guard written as `is_symlink()` would leave
    that half open with no race to win.

    Reachable only on Windows; the logic is driven here so it does not ship
    unexercised (the `stat.IO_REPARSE_TAG_*` constants do not exist on POSIX,
    hence the substituted tuple — the same idiom as
    `test_is_link_like_refuses_a_reparse_tagged_dir`).

    Ablation: delete the `st_reparse_tag` arm of `path_is_confined` and this fails
    while the symlink sibling above stays green, so it bites on the junction arm
    specifically."""
    root = tmp_path / "project"
    junction = root / ".bmad-loop"
    junction.mkdir(parents=True)  # a real directory: is_symlink() is False, as for a junction
    assert platform_util.path_is_confined(root, junction) is True  # positive control

    # Patch the TAG TUPLE, which the predicate reads from module globals on every
    # call, plus lstat itself — a junction's lstat reports a DIRECTORY mode.
    real_lstat = os.lstat
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p, *a, **k: _ReparseStat() if str(p) == str(junction) else real_lstat(p),
    )

    assert platform_util.path_is_confined(root, junction) is False


def test_path_is_confined_refuses_an_unprobeable_component(tmp_path, monkeypatch):
    """A component that cannot be probed is one this cannot vouch for, so the
    refusal is fail-CLOSED. `Path.is_symlink()` swallows that `OSError` and answers
    "not a link", which walks PAST the component — the opposite of what a
    confinement check owes its caller, and the quiet gap `launch.py` names.

    Ablation: drop the `except OSError` arm and this fails with the PermissionError
    escaping instead of being answered."""
    root = tmp_path / "project"
    nested = root / ".bmad-loop" / "runs"
    nested.mkdir(parents=True)
    unreadable = root / ".bmad-loop"

    real_lstat = os.lstat

    def refusing_lstat(p, *a, **k):
        if str(p) == str(unreadable):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_lstat(p)

    monkeypatch.setattr(os, "lstat", refusing_lstat)

    assert platform_util.path_is_confined(root, nested) is False


# ------------------------------------------------ confined atomic writes (#593)


def test_unconfined_write_error_is_an_oserror(tmp_path):
    """The refusal type is an `OSError` on purpose, and the adopters depend on it:
    every one of them already degrades on `OSError` from this very call
    (`runs.stop_run` swallows a failed stop-request write, the engine journals a
    failed park rollback, the settings screen reports a failed save). A refusal
    therefore lands in handling those sites already have.

    Ablation: rebase the class on `Exception` and this fails — as, downstream,
    would every adopter's degrade path, silently."""
    assert issubclass(platform_util.UnconfinedWriteError, OSError)

    root = tmp_path / "project"
    root.mkdir()
    stray = tmp_path / "stray.toml"

    # The lexical gate, which needs no links and so runs on both platforms. Matched
    # on ITS message, not merely on the type: the confinement walk below refuses an
    # out-of-root path too, so without the match this row passes with the gate
    # deleted — and the gate is what states the contract (`path` is under
    # `confine_root` by the caller's own spelling) rather than inferring it.
    with pytest.raises(OSError, match="is not under") as caught:
        platform_util.atomic_write_text_confined(stray, "x = 1\n", confine_root=root)

    assert isinstance(caught.value, platform_util.UnconfinedWriteError)
    assert not stray.exists()


def test_atomic_write_text_confined_writes_a_clean_tree(tmp_path):
    """The positive control for the refusals below — without it they pass for a
    writer that refuses everything."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)

    platform_util.atomic_write_text_confined(parent / "policy.toml", "x = 1\n", confine_root=root)

    assert (parent / "policy.toml").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_atomic_write_text_confined_lands_a_private_mode(tmp_path):
    """0600, the same mode `follow_symlinks=False` already gives this cohort — so
    adopting confinement changes no file's permissions. Bracketed umask for the
    reason the anchored case gives."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)

    previous = os.umask(0o022)
    try:
        platform_util.atomic_write_text_confined(
            parent / "policy.toml", "x = 1\n", confine_root=root
        )
    finally:
        os.umask(previous)

    landed = parent / "policy.toml"
    assert stat.S_IMODE(landed.stat().st_mode) == 0o600, oct(landed.stat().st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_text_confined_refuses_a_symlinked_parent(tmp_path):
    """The escape #593 names, closed. `follow_symlinks=False` refuses a link at
    `policy.toml`; it does not refuse one at `.bmad-loop/`, and
    `mkdir(parents=True, exist_ok=True)` ACCEPTS a symlink-to-a-directory, so the
    planted parent survives the setup step the callers run first.

    The assertion that pins the fix is the second one: refusing loudly is worth
    nothing if the write already landed somewhere else.

    Ablation: delete the `open_dir_confined` arm's `None` check and this fails
    `DID NOT RAISE`, with `policy.toml` sitting in `outside/`."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        platform_util.atomic_write_text_confined(
            root / ".bmad-loop" / "policy.toml", "x = 1\n", confine_root=root
        )

    assert list(outside.iterdir()) == []  # nothing escaped the project


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_bytes_confined_refuses_a_symlinked_parent(tmp_path):
    """The bytes arm's own refusal pin. Mirrored rather than parametrized: the two
    reach the shared body through different public names, and #593's byte-verbatim
    adopters are the ones a text-only helper would have quietly corrupted."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        platform_util.atomic_write_bytes_confined(
            root / ".bmad-loop" / "policy.toml", b"x = 1\n", confine_root=root
        )

    assert list(outside.iterdir()) == []


def test_atomic_write_bytes_confined_preserves_crlf_verbatim(tmp_path):
    """Byte-for-byte on both arms — the property the adopting frontmatter and
    policy writers read bytes to keep. Same payload reasoning as the anchored
    sibling: the non-UTF-8 byte is what makes this observable on POSIX."""
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    payload = b"---\r\nstatus: caf\xe9\r\n---\r\n"

    platform_util.atomic_write_bytes_confined(parent / "story.md", payload, confine_root=root)

    assert (parent / "story.md").read_bytes() == payload


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_confined_is_anchored_against_an_ancestor_swap(tmp_path, monkeypatch):
    """The race a path check cannot close: the session re-plants the parent as a
    link AFTER confinement is established. A preflight check is answered about a
    path and is stale the moment it returns, so the write follows the new link;
    the descriptor `open_dir_confined` hands back is bound to the directory it
    actually walked, so the swap renames something the write no longer consults.

    Forced rather than raced with threads — hooking the helper is the exact
    interleaving an attacker who wins the window achieves, and it is
    deterministic. The positive control is the second assertion: the write must
    actually LAND (in the real, now-renamed-aside directory), so this cannot pass
    by the write having simply failed."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = platform_util.open_dir_confined

    def swap_after_the_walk(confine_root: Path, target: Path):
        fd = real_open(confine_root, target)
        # attacker wins: the name now points outside, the fd still points home
        target.rename(tmp_path / "moved-aside")
        target.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(platform_util, "open_dir_confined", swap_after_the_walk)
    platform_util.atomic_write_text_confined(parent / "policy.toml", "x = 1\n", confine_root=root)

    assert not (outside / "policy.toml").exists()  # nothing escaped
    landed = tmp_path / "moved-aside" / "policy.toml"
    assert landed.read_text(encoding="utf-8") == "x = 1\n"  # and the write did happen
    assert parent.is_symlink()  # the swap really was in place for the write


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_text_confined_falls_back_without_dir_fd(tmp_path, monkeypatch):
    """win32 has no `*at()` family to anchor against, so it keeps check-then-write.
    Exercised here from POSIX so the fallback is not left to the Windows legs
    alone: it still has to refuse an ancestor link, just with the weaker guarantee.

    Ablation: delete the `path_is_confined` check and this fails `DID NOT RAISE`,
    with the file landing in `outside/` exactly as the unguarded POSIX path did."""
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        platform_util.atomic_write_text_confined(
            root / ".bmad-loop" / "policy.toml", "x = 1\n", confine_root=root
        )

    assert list(outside.iterdir()) == []

    # Positive control: the same fallback branch really does write when the tree is
    # clean, so the refusal is a refusal rather than a write that never got as far
    # as trying. Without this the test passes for a fallback wired to raise always.
    (root / ".bmad-loop").unlink()
    (root / ".bmad-loop").mkdir()
    platform_util.atomic_write_text_confined(
        root / ".bmad-loop" / "policy.toml", "x = 1\n", confine_root=root
    )
    assert (root / ".bmad-loop" / "policy.toml").read_text(encoding="utf-8") == "x = 1\n"


# ------------------------------------------------- require_writable_target (#597)
#
# `os.replace` needs write permission on the DIRECTORY, not on the entry it
# replaces, so a temp-and-replace write silently overwrites a file an operator
# marked read-only — and where mode is inherited, restores the 0444 afterwards, so
# nothing in the permission bits records that it changed. These rows pin the
# opt-in refusal that gives the pre-#590 behaviour back, and the compat row pins
# that the DEFAULT still does not refuse.
#
# 0o444 sets the READONLY attribute on win32 too, where O_WRONLY then fails with
# ERROR_ACCESS_DENIED -> PermissionError, so the refusal rows run unskipped on both
# platforms. Every chmod is on a file created in this test's own tmp_path and is
# restored in a `finally` — never on the session `project` template (copytree
# preserves modes), and Windows rmtree fails on a READONLY file left behind.


def _mkstemp_spy(calls: list[str]):
    """A `mkstemp` that records and refuses, to pin that a refusal STAGES NOTHING."""

    def fake_mkstemp(*, dir, prefix, suffix):
        calls.append(prefix)
        raise AssertionError("staged a temp before refusing the write")

    return fake_mkstemp


def test_atomic_write_text_refuses_a_readonly_target(tmp_path, monkeypatch):
    """The refusal #597 asks for: the operator marked this file read-only, and the
    writer honours that instead of routing around it.

    The `mkstemp` spy is the ORDERING pin, and it is the point of the test: the
    probe runs between the resolve and the staging, so a refusal leaves no temp to
    explain. Move `_refuse_unwritable_target` after `_mkstemp_beside` and this
    fails on the spy rather than on the contents.

    Ablation: delete the `require_writable_target` branch in `_atomic_write` and
    this fails `DID NOT RAISE`, with `target` reading "after"."""
    target = tmp_path / "sprint-status.yaml"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o444)
    calls: list[str] = []
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", _mkstemp_spy(calls))
    try:
        with pytest.raises(PermissionError):
            platform_util.atomic_write_text(target, "after", require_writable_target=True)
    finally:
        target.chmod(0o644)  # win32 rmtree refuses a READONLY file

    assert target.read_text(encoding="utf-8") == "before"
    assert calls == []  # nothing was staged


def test_atomic_write_bytes_refuses_a_readonly_target(tmp_path, monkeypatch):
    """The bytes arm's own row — mirrored, per the `atomic_write_bytes` banner."""
    target = tmp_path / "policy.toml"
    target.write_bytes(b"before")
    target.chmod(0o444)
    calls: list[str] = []
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", _mkstemp_spy(calls))
    try:
        with pytest.raises(PermissionError):
            platform_util.atomic_write_bytes(target, b"after", require_writable_target=True)
    finally:
        target.chmod(0o644)

    assert target.read_bytes() == b"before"
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="win32's own replace denies it — row below")
def test_default_still_replaces_a_readonly_target(tmp_path):
    """The compatibility pin, and the reason the flag is opt-in at all.

    Roughly twenty call sites write through these helpers, and the owner of a
    0444 file can legitimately replace it today. Turning the refusal on for
    everyone would convert a semantic loosening into a hard refusal of writes that
    currently succeed — the wrong direction for a contract this many callers rely
    on. So the default must keep replacing, and this is what says so.

    POSIX-only because the premise is: `os.replace` consults the parent
    directory's permission there, so the 0444 target is replaced. Win32's
    `MoveFileExW` denies a rename over a READONLY destination outright
    (WinError 5), so the silent-overwrite hole #597 answers never existed on
    win32 — the row below pins that platform's shape of the same default.

    Ablation: flip `require_writable_target`'s default to True and this fails,
    which is the whole warning it exists to give."""
    target = tmp_path / "sprint-status.yaml"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o444)
    try:
        platform_util.atomic_write_text(target, "after")
        landed = target.read_text(encoding="utf-8")
    finally:
        target.chmod(0o644)

    assert landed == "after"


@pytest.mark.skipif(sys.platform != "win32", reason="win32 READONLY semantics")
def test_default_readonly_denial_on_win32_comes_from_the_replace(tmp_path, monkeypatch):
    """The same compatibility pin, in the shape win32 allows it to take. There is
    no silent overwrite to preserve on this platform: `MoveFileExW` denies a
    rename over a READONLY destination with ERROR_ACCESS_DENIED, so the atomic
    write always raised `PermissionError` here — at the replace, where the direct
    `write_text` it succeeded raised at the open. What the default must preserve
    is that the denial is the OS's own, not the opt-in probe's: the write still
    STAGES — the mkstemp spy is that discriminator, since the raised type is
    `PermissionError` either way — fails at the publish after
    `_retry_on_sharing_violation`'s bounded backoff, and cleans its temp up.

    Ablation: flip `require_writable_target`'s default to True and this reddens
    on `calls` staying empty — `_refuse_unwritable_target` raises before
    `_mkstemp_beside` runs, so nothing stages."""
    target = tmp_path / "sprint-status.yaml"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o444)
    calls: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*, dir, prefix, suffix):
        calls.append(prefix)
        return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    monkeypatch.setattr(platform_util.tempfile, "mkstemp", spy)
    try:
        with pytest.raises(PermissionError):
            platform_util.atomic_write_text(target, "after")
        landed = target.read_text(encoding="utf-8")
    finally:
        target.chmod(0o644)

    assert calls == ["sprint-status.yaml."]  # it STAGED — the denial came at the replace
    assert landed == "before"  # the READONLY file is intact
    assert list(tmp_path.glob("*.tmp")) == []  # and the staged temp was cleaned up


def test_require_writable_target_allows_a_missing_target(tmp_path):
    """A file that does not exist yet is not a refusal — there is nothing to
    refuse, and creating it is what every flagged caller does on first run.

    Ablation: treat any failed probe as a refusal (drop the `except OSError`
    return) and this fails with `FileNotFoundError`, which would break first-run
    creation at every adopting site."""
    target = tmp_path / "sprint-status.yaml"

    platform_util.atomic_write_text(target, "first", require_writable_target=True)

    assert target.read_text(encoding="utf-8") == "first"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_require_writable_target_no_follow_ignores_a_planted_link_mode(tmp_path):
    """`follow_symlinks=False` replaces the NAME whatever it points at, so the mode
    of a plantable link's target is not the operator's answer about this file — it
    is the planter's, exactly as `_atomic_write` argues for mode inheritance. The
    probe opens `O_NOFOLLOW`, gets `ELOOP`, and allows the write.

    The second assertion is the one that matters: the victim is untouched. A
    no-follow write that read the link's mode as a refusal would be usable as a
    denial primitive — plant a link at a record, point it at any 0444 file, and
    every write to that record fails.

    Ablation: drop `O_NOFOLLOW` from the probe and this fails `PermissionError`,
    because the probe then reads the victim's mode through the link."""
    victim = tmp_path / "someone-elses-file"
    victim.write_text("victim", encoding="utf-8")
    victim.chmod(0o444)
    record = tmp_path / "record.json"
    record.symlink_to(victim)
    try:
        platform_util.atomic_write_text(
            record, "after", follow_symlinks=False, require_writable_target=True
        )
    finally:
        victim.chmod(0o644)

    assert record.read_text(encoding="utf-8") == "after"
    assert not record.is_symlink()  # the NAME was replaced, as no-follow promises
    assert victim.read_text(encoding="utf-8") == "victim"  # and the link's target was not


def test_atomic_write_text_confined_refuses_a_readonly_target(tmp_path):
    """The two guards compose: confinement anchors the parent, and the flag still
    refuses a read-only target underneath it.

    On the anchored arm the probe is asked dir_fd-relative
    (`os.open(name, ..., dir_fd=fd)`), never by path — a probe that re-named the
    path would reopen the window the descriptor exists to close.

    Ablation: drop the `require_writable_target` branch from
    `_atomic_write_confined` and this fails `DID NOT RAISE`."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)
    target = parent / "policy.toml"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            platform_util.atomic_write_text_confined(
                target, "after", confine_root=root, require_writable_target=True
            )
    finally:
        target.chmod(0o644)

    assert target.read_text(encoding="utf-8") == "before"
    assert list(parent.glob("*.tmp")) == []  # a refusal stages nothing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_writable_target_probe_answers_a_planted_fifo_without_blocking(tmp_path):
    """`O_WRONLY` against a reader-less FIFO does not fail — it WAITS for a reader
    that a planted FIFO will never have, wedging the probe (and the control loop
    driving it) forever. `O_NOFOLLOW` is no help: a FIFO is not a symlink, so the
    #591 threat model reaches this with one `mkfifo` at a name a driven session
    may write. The probe opens `O_NONBLOCK`, the kernel answers `ENXIO` at once,
    the probe's any-other-`OSError` arm returns, and the write replaces the
    FIFO's NAME the same way it replaces any other name it does not follow.

    Threaded with a bounded join so the ablated failure mode is a red assertion
    within the timeout, not a test run hung until the runner kills it.

    Ablation: drop `| non_block` from `_refuse_unwritable_target`'s open and this
    reddens alone, on `t.is_alive()` — the writer thread is still parked inside
    `os.open` waiting for a reader. The read-only rows above stay green: a
    regular file's permission check never blocks."""
    target = tmp_path / "policy.toml"
    os.mkfifo(target)
    failures: list[BaseException] = []

    def write() -> None:
        try:
            platform_util.atomic_write_bytes(
                target, b"payload", follow_symlinks=False, require_writable_target=True
            )
        except BaseException as e:  # pragma: no cover — reported by the asserts below
            failures.append(e)

    t = threading.Thread(target=write, daemon=True)
    t.start()
    t.join(timeout=10)

    assert not t.is_alive()  # the probe answered instead of waiting for a reader
    assert failures == []
    assert stat.S_ISREG(target.lstat().st_mode)  # the FIFO name was replaced
    assert target.read_bytes() == b"payload"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_confined_writable_target_probe_answers_a_planted_fifo_without_blocking(tmp_path):
    """The anchored probe shares the wedge and the fix: `_refuse_unwritable_target_at`
    opens the entry `O_WRONLY` dir_fd-relative, and a FIFO planted there parks it
    just as surely — the descriptor anchoring changes WHERE the name is opened,
    not what opening a reader-less FIFO for writing does.

    Ablation: drop `| os.O_NONBLOCK` from `_refuse_unwritable_target_at`'s open
    and this reddens alone, the same way as the path-based row above — the two
    probes are separate `os.open` calls, so neither row covers the other."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)
    target = parent / "policy.toml"
    os.mkfifo(target)
    failures: list[BaseException] = []

    def write() -> None:
        try:
            platform_util.atomic_write_bytes_confined(
                target, b"payload", confine_root=root, require_writable_target=True
            )
        except BaseException as e:  # pragma: no cover — reported by the asserts below
            failures.append(e)

    t = threading.Thread(target=write, daemon=True)
    t.start()
    t.join(timeout=10)

    assert not t.is_alive()  # the probe answered instead of waiting for a reader
    assert failures == []
    assert stat.S_ISREG(target.lstat().st_mode)  # the FIFO name was replaced
    assert target.read_bytes() == b"payload"
