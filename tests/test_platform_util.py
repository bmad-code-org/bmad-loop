"""Tests for the back-compat shims over the ProcessHost seam.

The kill/liveness bodies (and their pid<=0 guards) now live in
``bmad_loop.process_host`` — see ``test_process_host.py``. These cover only that
the legacy ``platform_util`` entry points still delegate, plus the real
``detach_kwargs`` that stayed behind."""

from __future__ import annotations

import errno
import functools
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
from conftest import NUL_PATH_RESOLVE_FAULTS, refuse_to_resolve

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


@pytest.mark.parametrize(
    "value",
    [
        "NUL",  # the bare device, the one form every Windows build still special-cases
        "nul",  # the match is case-insensitive
        "NUL.txt",  # an extension does not defuse it (Win10 and earlier)
        "PRN  ",  # trailing spaces are trimmed before the name is compared
        "sub/NUL",  # non-final component — `_is_reserved_basename` alone answers False here
        "sub/NUL.txt",  # both narrowings at once
        "CONIN$",  # the console pair, easy to omit from a hand-written set
        "COM1",
        "COM0",  # COM0/LPT0 are reserved by the same rule as COM1..COM9
        "LPT9",  # the last member — the set stops here, `com10` is the tripwire below
        "AUX",
        "aux.json",  # lowercase *and* extension, the shape a config field actually takes
        "CON.",  # a bare trailing dot on a device name
        "sub\\NUL",  # backslash separator — judged by the components Win32 would see
    ],
)
def test_names_win32_alias_catches_reserved_device_names(value):
    # Rule 1, proven per-component and in both separator flavours. The `sub/...`
    # rows are the ones `_is_reserved_basename` cannot answer on its own: it splits
    # on the first dot of the *whole* string, so it reads `"sub/NUL"` as stem
    # `"sub/NUL"` and returns False. Splitting into components first is the whole
    # difference. Ablation A1: drop the `_is_reserved_basename(part)` term from
    # `names_win32_alias` and this test reddens while
    # `test_names_win32_alias_catches_the_trailing_trim` stays green — except the
    # `"PRN  "` and `"CON."` rows, which rule 2 catches on its own.
    assert platform_util.names_win32_alias(value) is True


@pytest.mark.parametrize(
    "value",
    [
        ".claude/skills.",  # the #480 item-2 spelling: git matches `skills.`, Win32 creates `skills`
        ".claude/skills ",  # PR #708 made the space case identical to the dot case
        "skills. ",  # both at once
        "sub./x",  # a non-final component — the trim is not a basename-only rule
        "a/b ",
    ],
)
def test_names_win32_alias_catches_the_trailing_trim(value):
    # Rule 2, deliberately holding no row rule 1 also catches, so the two rules
    # redden separately: every component here strips to something non-empty and is
    # not a reserved name. Dropping either of rule 2's carve-outs leaves this test
    # entirely green — they only subtract, widening rule 2 onto the sibling
    # predicates' territory, which is what the disjointness test below pins.
    assert platform_util.names_win32_alias(value) is True


@pytest.mark.parametrize(
    "value",
    ["sub/...", "sub/.. ", "sub/   ", "a/. ", " /a", "sub\\..."],
)
def test_names_win32_alias_catches_an_all_dot_or_space_component_beside_a_real_one(value):
    """The round-1 review gap in rule 2's original carve-out: `names_tree_root`
    demands EVERY component be root-naming, `has_parent_ref` wants a literal `..`,
    and the old `part.strip(" .") != ""` carve-out excluded an all-period/space
    component unconditionally — so `sub/...` passed all four family members while
    Win32's trim empties the component and the value addresses `sub` (or, for
    `.. `, climbs under the other reading of the trim-vs-`..` ordering — divergent
    from the literal POSIX directory either way, so the refusal rests on neither
    reading). Scoping the carve-out by the WHOLE value (`not root_naming`) is what
    closes this without taking the single-component spellings off
    `names_tree_root`'s hands. Ablation: restore the old carve-out — replace
    `part not in (".", "..") and not root_naming` with `part.strip(" .") != ""` —
    and every row here reddens while the three alias tests above and the sibling
    delegation test below stay green."""
    assert platform_util.names_win32_alias(value) is True


@pytest.mark.parametrize(
    "value",
    [
        ".claude/skills",  # the shipped default — this predicate must never touch it
        "normal/path",
        "com10",  # NOT reserved: the set stops at COM9
        "nulls",  # a device name as a *prefix* is an ordinary name
        "auxiliary",
        "a.b/c.d",  # interior dots are ordinary; only *trailing* ones alias
    ],
)
def test_names_win32_alias_accepts_ordinary_paths(value):
    # The over-refusal guard. `com10`/`nulls`/`auxiliary` are the false-positive
    # tripwires: each would redden if the reserved-name test were loosened from an
    # exact stem match to a prefix or substring one.
    assert platform_util.names_win32_alias(value) is False


@pytest.mark.parametrize("value", ["..", ".", "", "...", "   ", ".. ", "a/..", "a/./b"])
def test_names_win32_alias_leaves_the_root_and_parent_spellings_to_its_siblings(value):
    # The disjointness pin. Every row here is refused by `has_parent_ref` (the
    # `..` spellings), refused by `names_tree_root` (the value-wide dot/space
    # ones), or contains a no-op `.` component that names the SAME path on every
    # platform — and this predicate must leave them all alone so the four family
    # members reject disjoint spelling classes and each stays separately
    # ablatable. Two arms, disjoint red sets: A2 — drop the `not root_naming`
    # term and the value-wide rows (`...`, `   `, `.. `) redden alone; A3 — drop
    # the `part not in (".", "..")` carve-out and the `..`/`.`-component rows
    # (`..`, `a/..`, `a/./b`) redden alone. `""` and bare `.` can redden under
    # neither: one has no components at all, the other is root-naming whole.
    assert platform_util.names_win32_alias(value) is False


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


# --------------------------------------------------------------- retrying_rmtree


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / "logs").mkdir(parents=True)
    (root / "state.json").write_text("{}", encoding="utf-8")
    (root / "logs" / "held.log").write_text("x", encoding="utf-8")
    return root


def _flaky_unlink(monkeypatch, target_name: str, failures: int | None, exc: OSError):
    """Patch ``os.unlink`` (the one ``shutil.rmtree`` calls) to raise ``exc`` for
    the entry named ``target_name`` — ``failures`` times, or forever when ``None``.
    Matched by basename: the fd-based walk passes a bare name plus ``dir_fd``, the
    handler's retry the full path."""
    calls = {"n": 0}
    real_unlink = os.unlink

    def fake(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == target_name:
            calls["n"] += 1
            if failures is None or calls["n"] <= failures:
                raise exc
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(platform_util.os, "unlink", fake)
    return calls


def test_retrying_rmtree_retries_then_succeeds(tmp_path, monkeypatch):
    # A concurrent reader's handle denies the delete on Windows; the backoff clears it
    # and the walk carries on to remove the rest of the tree.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    calls = _flaky_unlink(monkeypatch, "held.log", 2, PermissionError(13, "in use"))

    platform_util.retrying_rmtree(root)

    assert not root.exists()
    # the walk's own failing call, one more failure inside the retry, then success
    assert calls["n"] == 3
    assert len(sleeps) == 1


def test_retrying_rmtree_raises_after_retries_run_out(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util, "_REPLACE_ATTEMPTS", 3)
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    calls = _flaky_unlink(monkeypatch, "held.log", None, PermissionError(32, "in use"))

    with pytest.raises(PermissionError):
        platform_util.retrying_rmtree(root)

    assert calls["n"] == 1 + 3  # the walk's call, then every retry attempt
    assert len(sleeps) == 2  # no sleep after the final attempt
    assert root.exists()  # partial removal, the final failure is not swallowed


def test_retrying_rmtree_no_retry_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "linux")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    denied = PermissionError(13, "Permission denied")
    calls = _flaky_unlink(monkeypatch, "held.log", None, denied)

    with pytest.raises(PermissionError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is denied  # the original error, as a bare rmtree raises it
    assert calls["n"] == 1
    assert sleeps == []


def test_retrying_rmtree_no_retry_on_a_non_sharing_error(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    eio = OSError(errno.EIO, "I/O error")
    calls = _flaky_unlink(monkeypatch, "held.log", None, eio)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is eio
    assert calls["n"] == 1
    assert sleeps == []


def test_retrying_rmtree_treats_a_vanished_path_as_removed(tmp_path, monkeypatch):
    # The holder deleted (or moved) the entry between the denial and the retry: the
    # goal is met, so the walk continues instead of failing on FileNotFoundError.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)
    root = _tree(tmp_path)
    real_unlink = os.unlink
    calls = {"n": 0}

    def fake(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "held.log":
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(32, "in use")
            real_unlink(path, *args, **kwargs)  # really remove it ...
            raise FileNotFoundError(errno.ENOENT, "gone", os.fspath(path))  # ... and say so
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(platform_util.os, "unlink", fake)

    platform_util.retrying_rmtree(root)

    assert calls["n"] == 2
    assert not root.exists()


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 13),
    reason="win32 rmtree before 3.13 probes via _rmtree_islink, which swallows the "
    "lstat error, so the denial never reaches the handler",
)
def test_retrying_rmtree_never_retries_a_probe(tmp_path, monkeypatch):
    # rmtree returns early once the handler returns for its top-level lstat probe,
    # so "retrying" the probe would report a removal that never happened. Only the
    # removal ops (unlink/rmdir) are retried; a probe's denial surfaces at once.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    real_lstat = os.lstat
    calls = {"n": 0}

    def flaky_lstat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(root) and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(32, "in use")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(platform_util.os, "lstat", flaky_lstat)

    with pytest.raises(PermissionError):
        platform_util.retrying_rmtree(root)

    assert calls["n"] == 1
    assert sleeps == []
    assert root.exists()


def _dir_not_empty() -> OSError:
    """Win32's ERROR_DIR_NOT_EMPTY, which CPython maps to ENOTEMPTY. On a real
    host a genuinely non-empty directory reports this same 145; only `.winerror`
    tells it from a POSIX-shaped ENOTEMPTY."""
    exc = OSError(errno.ENOTEMPTY, "directory not empty")
    exc.winerror = 145  # pyright: ignore[reportAttributeAccessIssue]
    return exc


def _flaky_rmdir(monkeypatch, target_name: str, failures: int | None, exc: OSError):
    """Patch ``os.rmdir`` (the one ``shutil.rmtree`` calls) to raise ``exc`` for
    the directory named ``target_name`` — ``failures`` times, or forever when
    ``None``. Matched by basename: the fd-based walk passes a bare name plus
    ``dir_fd``, the handler's retry the full path."""
    calls = {"n": 0}
    real_rmdir = os.rmdir

    def fake(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == target_name:
            calls["n"] += 1
            if failures is None or calls["n"] <= failures:
                raise exc
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(platform_util.os, "rmdir", fake)
    return calls


@pytest.mark.parametrize("failures", [1, 2])
def test_retrying_rmtree_retries_a_delete_pending_child(tmp_path, monkeypatch, failures):
    # A child unlinked under another process's FILE_SHARE_DELETE handle stays
    # delete-pending, so the parent rmdir sees WinError 145 until the handle closes.
    # failures=2 makes the 145 repeat INSIDE the backoff loop, which only clears
    # when the loop itself was handed the rmdir predicate.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    calls = _flaky_rmdir(monkeypatch, "logs", failures, _dir_not_empty())

    platform_util.retrying_rmtree(root)

    assert not root.exists()
    # the walk's own failing call, the in-loop failures, then success
    assert calls["n"] == failures + 1
    assert len(sleeps) == failures - 1  # a backoff only between in-loop attempts


def test_retrying_rmtree_raises_when_a_delete_pending_child_never_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util, "_REPLACE_ATTEMPTS", 3)
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    not_empty = _dir_not_empty()
    calls = _flaky_rmdir(monkeypatch, "logs", None, not_empty)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is not_empty
    assert calls["n"] == 1 + 3  # the walk's call, then every retry attempt
    assert len(sleeps) == 2  # no sleep after the final attempt
    assert root.exists()  # partial removal, the final failure is not swallowed


def test_retrying_rmtree_no_retry_on_an_errno_only_dir_not_empty(tmp_path, monkeypatch):
    # The rmdir predicate keys on .winerror, not errno: an ENOTEMPTY with no
    # winerror attached (a POSIX-shaped error) is not retried and surfaces at once.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    not_empty = OSError(errno.ENOTEMPTY, "directory not empty")
    calls = _flaky_rmdir(monkeypatch, "logs", None, not_empty)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is not_empty
    assert calls["n"] == 1
    assert sleeps == []


def test_retrying_rmtree_retries_a_sharing_violation_on_rmdir(tmp_path, monkeypatch):
    # rmdir runs under the rmdir predicate, which must still cover the sharing
    # violation, not only the 145.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    calls = _flaky_rmdir(monkeypatch, "logs", 2, PermissionError(32, "in use"))

    platform_util.retrying_rmtree(root)

    assert not root.exists()
    assert calls["n"] == 3
    assert len(sleeps) == 1


def test_retrying_rmtree_no_dir_not_empty_retry_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "linux")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    not_empty = _dir_not_empty()
    calls = _flaky_rmdir(monkeypatch, "logs", 1, not_empty)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is not_empty  # the original error, as a bare rmtree raises it
    assert calls["n"] == 1
    assert sleeps == []


def test_retrying_rmtree_no_dir_not_empty_retry_on_unlink(tmp_path, monkeypatch):
    # 145 is retried for the parent rmdir only; on an unlink it is not transient.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    root = _tree(tmp_path)
    not_empty = _dir_not_empty()
    calls = _flaky_unlink(monkeypatch, "held.log", None, not_empty)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_rmtree(root)

    assert caught.value is not_empty
    assert calls["n"] == 1
    assert sleeps == []


def test_dir_not_empty_does_not_widen_atomic_replace(tmp_path, monkeypatch):
    # The rmdir predicate is rmtree's alone: the shared sharing-violation gate that
    # atomic_replace runs under still raises a 145 at once.
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    not_empty = _dir_not_empty()
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        raise not_empty

    monkeypatch.setattr(platform_util.os, "replace", replace)

    with pytest.raises(OSError) as caught:
        platform_util.atomic_replace(tmp_path / "s", tmp_path / "d")

    assert caught.value is not_empty
    assert calls["n"] == 1
    assert sleeps == []


def test_dir_not_empty_does_not_widen_retrying_unlink(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))
    target = tmp_path / "held.log"
    target.write_text("x", encoding="utf-8")
    not_empty = _dir_not_empty()
    calls = _flaky_unlink(monkeypatch, "held.log", None, not_empty)

    with pytest.raises(OSError) as caught:
        platform_util.retrying_unlink(target)

    assert caught.value is not_empty
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows open-handle delete denial")
def test_retrying_rmtree_outlasts_a_real_open_handle(tmp_path):
    # Real-host evidence: Python's open() grants no FILE_SHARE_DELETE, so the unlink
    # is denied until the timer closes the handle; the real backoff outlasts it.
    root = _tree(tmp_path)
    handle = open(root / "logs" / "held.log", encoding="utf-8")  # noqa: SIM115
    timer = threading.Timer(0.2, handle.close)
    timer.start()
    try:
        platform_util.retrying_rmtree(root)
        assert not root.exists()
    finally:
        timer.cancel()
        handle.close()


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows delete-pending child")
def test_retrying_rmtree_outlasts_a_delete_pending_child(tmp_path, monkeypatch, request):
    """Real-host characterization of the WinError 145 retry. ``held.log`` is held
    open WITH ``FILE_SHARE_DELETE``, so its unlink succeeds; under legacy delete
    semantics the name lingers delete-pending and the parent rmdir fails with 145
    until the timer closes the handle, while the POSIX delete recent Windows uses
    on NTFS unlinks it at once. The test passes either way: it exists to show the
    removal completes on the real host, whichever behavior that host has. How many 145s
    the parent rmdir saw is recorded as the ``rmdir_winerror_145_count`` JUnit
    property, so the CI XML shows which delete semantics the runner has."""
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    share_all = 0x7  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    open_existing = 3
    file_attribute_normal = 0x80

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid_handle_value = wintypes.HANDLE(-1).value

    root = _tree(tmp_path)
    handle = kernel32.CreateFileW(
        str(root / "logs" / "held.log"),
        generic_read,
        share_all,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    assert handle not in (None, invalid_handle_value), ctypes.get_last_error()  # type: ignore[attr-defined]

    lock = threading.Lock()
    closed = False

    def close_once() -> None:
        nonlocal closed
        with lock:
            if not closed:
                closed = True
                kernel32.CloseHandle(handle)

    seen_145 = {"n": 0}
    real_rmdir = os.rmdir

    def spy_rmdir(*args, **kwargs):
        try:
            real_rmdir(*args, **kwargs)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 145:
                seen_145["n"] += 1
            raise

    monkeypatch.setattr(platform_util.os, "rmdir", spy_rmdir)

    timer = threading.Timer(0.2, close_once)
    timer.start()
    try:
        platform_util.retrying_rmtree(root)
        assert not root.exists()
    finally:
        timer.cancel()
        close_once()
        # user_properties directly: the record_property fixture warns under xunit2,
        # but junitxml writes these properties either way — recorded on failure too
        request.node.user_properties.append(("rmdir_winerror_145_count", seen_145["n"]))


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


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
def test_resolve_or_lexical_handles_value_error_family_with_safe_stderr(
    resolve_fault, monkeypatch, tmp_path
):
    """The fallback note must remain encodable when the path itself is not."""
    import io

    path = tmp_path / "caf\xe9-\ud800-path"
    refuse_to_resolve(monkeypatch, path, error=resolve_fault)
    stderr_bytes = io.BytesIO()
    stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stderr", stderr)

    assert platform_util.resolve_or_lexical(path) == path.absolute()

    stderr.flush()
    note = stderr_bytes.getvalue().decode("ascii")
    assert "cannot canonicalize" in note
    assert "caf\\xe9-\\ud800-path" in note
    assert str(resolve_fault) in note


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
# The rows below that need only what BOTH anchored arms provide — a confined
# directory handle, the ``*_at`` writers, the confined writers — run on Windows
# too, through ``win32_at``; ``DIR_FD`` stays on the rows that also need
# ``scandir(fd)``, mode bits, or a symlink the runner may not be allowed to plant.
ANCHORED = pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)


@ANCHORED
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


@ANCHORED
def test_open_dir_confined_refuses_a_target_outside_the_root(tmp_path):
    """Confinement is refused before a single directory is opened: a target that is
    not under the root has no chain to walk, however clean its own ancestry is."""
    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert platform_util.open_dir_confined(root, elsewhere) is None


@ANCHORED
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


# ------------------------------------------- open_dir_confined root pin (DW-338)
#
# `root_identity=` pins a root the orchestrator minted or validated inside the
# checkout to the `lstat` identity the caller accepted. The threat: a writer that
# can reach the checkout replaces such a root (`implementation_artifacts`, a run
# dir) with a link between the caller's predicate and the root open, and every
# later confined read or write follows it out of the repository.


def _swap_root_for_link(root: Path, outside: Path, sub: str) -> None:
    """Rename ``root`` aside and plant a link at its name to an outside tree
    carrying the same subpath, so the walk below the root still succeeds."""
    (outside / sub).mkdir(parents=True, exist_ok=True)
    root.rename(root.with_name(root.name + "-aside"))
    root.symlink_to(outside, target_is_directory=True)


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_open_dir_confined_pinned_root_refuses_a_root_swapped_for_a_link(tmp_path):
    """The DW-338 swap: identity taken by `lstat`, root then replaced by a link to
    an outside tree with the same subpath — the pinned walk refuses.

    Ablation: remove the identity compare in `open_dir_confined` and this fails —
    the pinned call returns a descriptor into `outside` exactly as the unpinned
    control below does."""
    root = tmp_path / "project" / "artifacts"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    identity = os.lstat(root)

    _swap_root_for_link(root, outside, "specs")

    assert platform_util.open_dir_confined(root, root / "specs", root_identity=identity) is None
    # Control: the swap really redirects an unpinned walk into `outside`.
    fd = platform_util.open_dir_confined(root, root / "specs")
    assert fd is not None
    try:
        assert os.fstat(fd).st_ino == (outside / "specs").stat().st_ino
    finally:
        os.close(fd)


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_open_dir_confined_pinned_root_refuses_an_ancestor_swapped_for_a_link(tmp_path):
    """The compare also catches an ANCESTOR of the root swapped between the
    caller's predicate and the open: root's parent replaced by a link to an
    outside tree holding the same subpath reaches a different directory, which
    `O_NOFOLLOW` on the root alone would never notice.

    Ablation: remove the identity compare in `open_dir_confined` and this fails —
    the walk returns a descriptor into `outside/artifacts/specs`."""
    project = tmp_path / "project"
    root = project / "artifacts"
    (root / "specs").mkdir(parents=True)
    identity = os.lstat(root)
    outside = tmp_path / "outside"
    (outside / "artifacts" / "specs").mkdir(parents=True)

    project.rename(tmp_path / "project-aside")
    project.symlink_to(outside, target_is_directory=True)

    assert platform_util.open_dir_confined(root, root / "specs", root_identity=identity) is None


@ANCHORED
def test_open_dir_confined_pin_refuses_a_non_directory_identity(tmp_path):
    """An identity carrying the root's real `(st_dev, st_ino)` but a regular-file
    mode is refused: only a DIRECTORY identity can pin a directory root.

    Ablation: drop the `S_ISDIR` half of `_same_dir_identity` and this fails —
    the matching device and inode alone would admit the walk."""
    root = tmp_path / "artifacts"
    (root / "specs").mkdir(parents=True)
    fields = list(os.lstat(root)[:10])
    fields[0] = stat.S_IFREG | 0o644  # st_mode
    not_a_dir = os.stat_result(fields)

    assert platform_util.open_dir_confined(root, root / "specs", root_identity=not_a_dir) is None


@ANCHORED
def test_open_dir_confined_pinned_root_unchanged_returns_the_descriptor(tmp_path):
    """Positive control for the pin: an untouched root whose identity was taken by
    `lstat` (or `pinned_root_identity`) walks exactly as an unpinned one does."""
    root = tmp_path / "project" / "artifacts"
    nested = root / "specs"
    nested.mkdir(parents=True)

    for identity in (os.lstat(root), platform_util.pinned_root_identity(root)):
        assert identity is not None
        fd = platform_util.open_dir_confined(root, nested, root_identity=identity)
        assert fd is not None
        try:
            assert os.fstat(fd).st_ino == nested.stat().st_ino
        finally:
            os.close(fd)


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_open_dir_confined_pin_refuses_a_link_lstat_identity(tmp_path):
    """An identity that is a link's own `lstat` never matches the directory the
    open reaches through it: the pin refuses rather than degrading to "unpinned".

    Ablation: drop the `S_ISDIR` requirement from `_same_dir_identity` and compare
    only `(st_dev, st_ino)` — still refused (a link's inode is its own); drop the
    compare altogether and this fails."""
    real = tmp_path / "real"
    (real / "specs").mkdir(parents=True)
    root = tmp_path / "artifacts"
    root.symlink_to(real, target_is_directory=True)

    assert (
        platform_util.open_dir_confined(root, root / "specs", root_identity=os.lstat(root)) is None
    )


@ANCHORED
def test_open_dir_confined_pin_refuses_a_zero_inode_identity(tmp_path):
    """A synthetic identity with `st_ino == 0` carries no identity at all; even
    with a matching `st_dev` and mode it refuses. And the same root with its real
    inode walks, so the refusal is the zero, not the synthetic stat itself.

    Ablation: drop the nonzero-inode requirement from `_same_dir_identity` and the
    `_same_dir_identity(zero, zero)` assert fails — two identity-less stats would
    otherwise "match" each other. (The seam assert alone would still pass on the
    inode mismatch, which is why the helper is asserted directly.)"""
    root = tmp_path / "artifacts"
    (root / "specs").mkdir(parents=True)
    real = os.lstat(root)
    fields = list(real[:10])
    fields[1] = 0  # st_ino
    zero = os.stat_result(fields)

    assert platform_util.open_dir_confined(root, root / "specs", root_identity=zero) is None
    assert not platform_util._same_dir_identity(zero, zero)
    assert platform_util._same_dir_identity(real, real)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("writer", ["text", "bytes", "exclusive"])
def test_confined_writer_fallback_refuses_a_swapped_pinned_root(tmp_path, monkeypatch, writer):
    """No-handle fallback (`HANDLE_ANCHORED_WRITES` False): `path_is_confined`
    checks only components BELOW the root, so a root swapped for a link to a tree
    with the same subpath passes it — the pin's `lstat` compare is what refuses.
    Check-then-write; the residual (DW-295) is unchanged otherwise.

    Ablation: delete the `_root_still_pinned` check in `_atomic_write_confined` /
    `create_exclusive_confined` and this fails `DID NOT RAISE`, with the file
    landing in `outside/specs/`."""
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project" / "artifacts"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    identity = platform_util.pinned_root_identity(root)
    assert identity is not None
    target = root / "specs" / "record.md"

    def write() -> None:
        if writer == "text":
            platform_util.atomic_write_text_confined(
                target, "x\n", confine_root=root, root_identity=identity
            )
        elif writer == "bytes":
            platform_util.atomic_write_bytes_confined(
                target, b"x\n", confine_root=root, root_identity=identity
            )
        else:
            os.close(
                platform_util.create_exclusive_confined(
                    target, confine_root=root, root_identity=identity
                )
            )

    _swap_root_for_link(root, outside, "specs")
    with pytest.raises(platform_util.UnconfinedWriteError):
        write()
    assert list((outside / "specs").iterdir()) == []

    # Positive control: the same fallback writes through the unswapped root.
    root.unlink()
    root.with_name(root.name + "-aside").rename(root)
    write()
    assert target.exists()


@DIR_FD
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("writer", ["text", "bytes", "exclusive"])
def test_confined_writer_anchored_refuses_a_swapped_pinned_root(tmp_path, writer):
    """The anchored arm forwards `root_identity` to `open_dir_confined`, so a root
    swapped for a link after the caller accepted it raises and nothing lands
    outside.

    Ablation: stop forwarding `root_identity` from the writer (or remove the
    compare in `open_dir_confined`) and this fails `DID NOT RAISE`."""
    root = tmp_path / "project" / "artifacts"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    identity = platform_util.pinned_root_identity(root)
    assert identity is not None
    target = root / "specs" / "record.md"

    _swap_root_for_link(root, outside, "specs")
    with pytest.raises(platform_util.UnconfinedWriteError):
        if writer == "text":
            platform_util.atomic_write_text_confined(
                target, "x\n", confine_root=root, root_identity=identity
            )
        elif writer == "bytes":
            platform_util.atomic_write_bytes_confined(
                target, b"x\n", confine_root=root, root_identity=identity
            )
        else:
            platform_util.create_exclusive_confined(
                target, confine_root=root, root_identity=identity
            )
    assert list((outside / "specs").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_pinned_root_identity_refuses_links_files_and_missing_roots(tmp_path):
    """A pinned caller treats None as a refusal, so every root it cannot vouch for
    must answer None: a symlinked root, a file, a missing path. A real directory
    answers its own `lstat`."""
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")

    assert platform_util.pinned_root_identity(linked) is None
    assert platform_util.pinned_root_identity(a_file) is None
    assert platform_util.pinned_root_identity(tmp_path / "absent") is None
    identity = platform_util.pinned_root_identity(real)
    assert identity is not None
    assert identity.st_ino == os.lstat(real).st_ino


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_root_identity_record_is_the_pinned_dev_and_ino_or_none(tmp_path, monkeypatch):
    """DW-446: the mint-time record is `pinned_root_identity`'s ``(st_dev, st_ino)``
    for a real directory, and None for anything a pin could not vouch for — a link,
    a file, a missing path, or a zero inode (no identity to hold a root to).

    Ablation: drop the ``st_ino == 0`` check and the zero-inode row answers
    ``(dev, 0)``."""
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")

    own = os.lstat(real)
    assert platform_util.root_identity_record(real) == (own.st_dev, own.st_ino)
    for unpinnable in (linked, a_file, tmp_path / "absent"):
        assert platform_util.root_identity_record(unpinnable) is None

    zero = tmp_path / "zero"
    zero.mkdir()
    real_lstat = os.lstat
    zero_stat = os.stat_result((stat.S_IFDIR, 0, own.st_dev, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **k: zero_stat if str(p) == str(zero) else real_lstat(p, *a, **k)
    )
    assert platform_util.root_identity_record(zero) is None


def test_recorded_root_identity_carries_the_record_in_stat_field_order(tmp_path):
    """DW-446: the synthetic identity built from a record is a DIRECTORY stat whose
    ``st_dev``/``st_ino`` are the record's (`os.stat_result` takes mode, ino, dev in
    that order), so it matches the recorded directory's own stat; a None record is
    the never-matching identity.

    Ablation: swap ``ino``/``dev`` in the tuple `recorded_root_identity` builds and
    the field and match rows redden."""
    real = tmp_path / "real"
    real.mkdir()
    own = os.lstat(real)
    identity = platform_util.recorded_root_identity((own.st_dev, own.st_ino))

    assert (identity.st_dev, identity.st_ino) == (own.st_dev, own.st_ino)
    assert stat.S_ISDIR(identity.st_mode)
    assert platform_util._same_dir_identity(identity, own)
    never = platform_util.recorded_root_identity(None)
    assert never is platform_util.NEVER_MATCHING_IDENTITY
    assert not platform_util._same_dir_identity(never, own)
    assert not platform_util._same_dir_identity(never, never)


def test_pinned_root_identity_refuses_a_reparse_tagged_dir(tmp_path, monkeypatch):
    """A win32 junction `lstat`s as a DIRECTORY with a nonzero inode, so only the
    reparse-tag check stands between it and a pinned identity; drive that
    Windows-only branch here (the tuple is substituted, as the neighbours do).

    Ablation: delete the `st_reparse_tag` check in `pinned_root_identity` and
    this fails — the junction's own directory stat is returned as the pin."""

    class _IdentifiedReparseStat(_ReparseStat):
        st_dev = 1
        st_ino = 4242

    junction = tmp_path / "junction"
    junction.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    real_lstat = os.lstat
    monkeypatch.setattr(
        platform_util.os,
        "lstat",
        lambda p, *a, **k: _IdentifiedReparseStat() if str(p) == str(junction) else real_lstat(p),
    )

    assert platform_util.pinned_root_identity(junction) is None
    identity = platform_util.pinned_root_identity(plain)
    assert identity is not None
    assert identity.st_ino == real_lstat(plain).st_ino


@DIR_FD
def test_open_dir_confined_readable_default_supports_scandir(tmp_path):
    root = tmp_path / "project"
    nested = root / "artifacts"
    nested.mkdir(parents=True)
    (nested / "owned.md").write_bytes(b"x")

    fd = platform_util.open_dir_confined(root, nested)

    assert fd is not None
    try:
        with os.scandir(fd) as entries:
            assert [entry.name for entry in entries] == ["owned.md"]
    finally:
        os.close(fd)


@DIR_FD
@pytest.mark.skipif(
    not (getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0)),
    reason="host has no search-only directory-open flag",
)
def test_open_dir_confined_search_only_applies_to_root_intermediate_and_final(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    final = root / "intermediate" / "artifacts"
    final.mkdir(parents=True)
    search_flag = getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0)
    real_open = os.open
    observed_flags: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(platform_util.os, "open", record_open)
    fd = platform_util.open_dir_confined(root, final, search_only=True)

    assert fd is not None
    try:
        assert os.path.samestat(os.fstat(fd), final.stat())
    finally:
        os.close(fd)
    assert len(observed_flags) == 3
    assert all(flags & search_flag for flags in observed_flags)
    assert all(flags & os.O_DIRECTORY for flags in observed_flags)


@DIR_FD
@pytest.mark.skipif(
    not (getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0)),
    reason="host has no search-only directory-open flag",
)
def test_open_dir_confined_search_only_accepts_execute_only_final_directory(tmp_path):
    root = tmp_path / "project"
    final = root / "artifacts"
    final.mkdir(parents=True)
    final.chmod(0o100)
    try:
        fd = platform_util.open_dir_confined(root, final, search_only=True)
        assert fd is not None
        os.close(fd)
    finally:
        final.chmod(0o700)


@DIR_FD
@pytest.mark.skipif(
    not (getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0)),
    reason="host has no search-only directory-open flag",
)
def test_open_dir_confined_search_only_crosses_execute_only_intermediate_directory(tmp_path):
    root = tmp_path / "project"
    intermediate = root / "execute-only"
    final = intermediate / "artifacts"
    final.mkdir(parents=True)
    intermediate.chmod(0o100)
    try:
        fd = platform_util.open_dir_confined(root, final, search_only=True)
        assert fd is not None
        try:
            assert os.path.samestat(os.fstat(fd), final.stat())
        finally:
            os.close(fd)
    finally:
        intermediate.chmod(0o700)


# -------------------------------------------------- anchored atomic writes (#593)


@contextmanager
def _dir_fd(directory: Path):
    """The descriptor the anchored helpers take, closed on the way out — the
    ``O_DIRECTORY`` open on POSIX, a directory handle through ``win32_at`` on
    Windows, exactly as `open_dir_confined` anchors on each arm."""
    if platform_util.DIR_FD_ANCHORED_WRITES:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    else:
        fd = platform_util.win32_at.open_directory(directory)
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


@ANCHORED
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


@ANCHORED
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


@ANCHORED
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


@ANCHORED
def test_atomic_write_bytes_at_writable_refusal_precedes_staging(tmp_path, monkeypatch):
    target = tmp_path / "owned.md"
    target.write_bytes(b"before")
    failure = PermissionError("read only")
    calls: list[str] = []

    def refuse(_dir_fd: int, _name: str) -> None:
        calls.append("probe")
        raise failure

    def unexpected_stage(*_args, **_kwargs):
        calls.append("stage")
        raise AssertionError("staged before writable refusal")

    monkeypatch.setattr(platform_util, "_refuse_unwritable_target_at", refuse)
    monkeypatch.setattr(platform_util, "_open_exclusive_at", unexpected_stage)
    with _dir_fd(tmp_path) as fd:
        with pytest.raises(PermissionError) as excinfo:
            platform_util.atomic_write_bytes_at(
                fd, target.name, b"after", _require_writable_target=True
            )

    assert excinfo.value is failure
    assert calls == ["probe"]
    assert target.read_bytes() == b"before"


@ANCHORED
def test_atomic_write_bytes_at_does_not_verify_after_prepublication_failure(tmp_path):
    target = tmp_path / "owned.md"
    target.write_bytes(b"before")
    failure = RuntimeError("target changed before replace")
    verified = False

    def fail_before_replace() -> None:
        raise failure

    def verify_after_replace(_published_fd: int) -> None:
        nonlocal verified
        verified = True

    with _dir_fd(tmp_path) as fd:
        with pytest.raises(RuntimeError) as excinfo:
            platform_util.atomic_write_bytes_at(
                fd,
                target.name,
                b"after",
                _before_replace=fail_before_replace,
                _after_replace=verify_after_replace,
            )

    assert excinfo.value is failure
    assert verified is False
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


def test_atomic_write_text_confined_lands_the_platform_line_ending(tmp_path):
    """The confined TEXT writer keeps `atomic_write_text`'s translating newline
    default on every arm — LF on POSIX, CRLF on Windows — so anchoring the write
    through `win32_at` changed no byte a ledger or decisions file lands there.
    `atomic_write_text_at` (records, POSIX-only callers) is the one that never
    translates; the confined writer is what the operator-edited cohort uses.

    Ablation: drop the `newline=None` the confined arm passes `_atomic_write_at`
    and this reddens on Windows with LF where the path writer landed CRLF."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)

    platform_util.atomic_write_text_confined(parent / "ledger.md", "a\nb\n", confine_root=root)

    assert (parent / "ledger.md").read_bytes() == f"a{os.linesep}b{os.linesep}".encode()


def test_atomic_write_confined_refuses_a_parent_ref_below_the_root(tmp_path):
    """`is_relative_to` is a lexical PREFIX test, so `root/specs/../../outside/f`
    passes it while naming a path outside the root — and `..` is a real directory
    entry, not a link, so the anchored walk would open it (`O_NOFOLLOW` has no
    opinion on dot-dot) and climb straight back OUT of the root; the win32 walk
    `lstat`s through it the same way. The refusal is the `has_parent_ref` gate in
    `_atomic_write_confined`, raised over the RELATIVE part before anything is
    walked or staged.

    This is the writer paying the debt `path_is_confined`'s docstring assigns to
    "a caller building `target` out of untrusted parts": adopters hand these
    writers spec paths read back from state a driven session can influence
    (`runs.rearm_escalation` re-stamps whatever spec path the run recorded), so
    the chokepoint owes the check rather than twenty call sites.

    Matched on ITS message ("climbs back out"), not merely the type, for the
    reason the out-of-root row gives: refusing for a different reason must not
    grade as refusing for this one.

    Ablation: delete the `has_parent_ref` gate and this fails `DID NOT RAISE` —
    the anchored walk opens `specs`, then `..` twice, and publishes the payload
    over `outside/victim.md`."""
    root = tmp_path / "project"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text("theirs", encoding="utf-8")
    dodgy = root / "specs" / ".." / ".." / "outside" / "victim.md"
    assert dodgy.is_relative_to(root)  # PRECONDITION: the prefix gate alone passes it

    with pytest.raises(OSError, match="climbs back out") as caught:
        platform_util.atomic_write_text_confined(dodgy, "mine", confine_root=root)

    assert isinstance(caught.value, platform_util.UnconfinedWriteError)
    assert victim.read_text(encoding="utf-8") == "theirs"  # not rewritten
    assert sorted(p.name for p in outside.iterdir()) == ["victim.md"]  # nor staged


def test_atomic_write_confined_refuses_a_parent_ref_on_the_fallback_arm(tmp_path, monkeypatch):
    """The same refusal with `DIR_FD_ANCHORED_WRITES` off: the gate sits ABOVE
    the two arms, so the win32 check-then-write degrade is covered by the same
    raise. It has to be — `path_is_confined` alone answers True for the `..`
    spelling, since every lexical cursor on it `lstat`s through real directories
    and none is a link.

    Ablation: delete the `has_parent_ref` gate and this fails `DID NOT RAISE`,
    with the payload landing over `outside/victim.md` through the plain no-follow
    write."""
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text("theirs", encoding="utf-8")
    dodgy = root / "specs" / ".." / ".." / "outside" / "victim.md"

    with pytest.raises(platform_util.UnconfinedWriteError, match="climbs back out"):
        platform_util.atomic_write_text_confined(dodgy, "mine", confine_root=root)

    assert victim.read_text(encoding="utf-8") == "theirs"  # not rewritten
    assert sorted(p.name for p in outside.iterdir()) == ["victim.md"]  # nor staged

    # Positive control: the same fallback branch still writes a clean in-tree
    # spelling, so the refusal above is a refusal rather than an arm that raises
    # on everything.
    platform_util.atomic_write_text_confined(root / "specs" / "ok.md", "clean", confine_root=root)
    assert (root / "specs" / "ok.md").read_text(encoding="utf-8") == "clean"


def test_create_exclusive_confined_creates_at_0600_and_arbitrates(tmp_path):
    """The positive control and the arbitration in one row: the first create
    hands back a writable fd at a private mode, the second raises
    `FileExistsError` — the single-atomic-step "is one pending? lodge mine"
    contract `_create_stop_request` is built on, which the temp-and-replace
    confined writers cannot express."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)
    target = parent / "stop-request.json"

    previous = os.umask(0o022)
    try:
        fd = platform_util.create_exclusive_confined(target, confine_root=root)
    finally:
        os.umask(previous)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("mine")

    assert target.read_text(encoding="utf-8") == "mine"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        platform_util.create_exclusive_confined(target, confine_root=root)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_create_exclusive_confined_refuses_a_planted_link_at_the_name(tmp_path):
    """`O_EXCL` never dereferences the final component, so a link planted at the
    name — dangling included — reads as "already pending" rather than being
    followed; the victim it points at is untouched. The parents are this
    function's own half, the rows below."""
    root = tmp_path / "project"
    parent = root / ".bmad-loop"
    parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("theirs", encoding="utf-8")
    (parent / "stop-request.json").symlink_to(victim)

    with pytest.raises(FileExistsError):
        platform_util.create_exclusive_confined(parent / "stop-request.json", confine_root=root)

    assert victim.read_text(encoding="utf-8") == "theirs"  # not followed, not truncated


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_create_exclusive_confined_refuses_a_symlinked_parent(tmp_path, monkeypatch):
    """The reason this helper exists over a bare `O_EXCL` open: exclusivity
    covers only the final component, and every directory above it was resolved
    by name. Both arms are graded here — the anchored walk, then the win32
    check-then-create degrade driven from POSIX — with an inline positive
    control on the fallback arm so its refusal grades as a refusal.

    Ablation: replace the helper's body with the bare `os.open(path, flags,
    0o600)` it hardens and both arms fail `DID NOT RAISE`, with the request
    landing in `outside/` — while the arbitration and planted-link rows above
    stay GREEN, which is what proves they grade `O_EXCL`, not confinement."""
    root = tmp_path / "project"
    (root / ".bmad-loop").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".bmad-loop" / "runs").symlink_to(outside, target_is_directory=True)
    target = root / ".bmad-loop" / "runs" / "stop-request.json"

    with pytest.raises(platform_util.UnconfinedWriteError, match="without a redirect"):
        platform_util.create_exclusive_confined(target, confine_root=root)
    assert list(outside.iterdir()) == []  # nothing landed outside

    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    with pytest.raises(platform_util.UnconfinedWriteError, match="without a redirect"):
        platform_util.create_exclusive_confined(target, confine_root=root)
    assert list(outside.iterdir()) == []

    # Positive control for the fallback arm: a clean chain still creates.
    fd = platform_util.create_exclusive_confined(root / ".bmad-loop" / "ok.json", confine_root=root)
    os.close(fd)
    assert (root / ".bmad-loop" / "ok.json").exists()


# ------------------------- confined mkdir / unlink below a root (DW-497)


def _both_confined_arms(monkeypatch):
    """Yield once per arm: the host's anchored arm, then the no-handle fallback."""
    yield "anchored"
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    yield "fallback"


def test_make_dirs_confined_creates_missing_parents_idempotently(tmp_path, monkeypatch):
    """The positive half on both arms: every missing component is created, an
    existing chain is accepted, and a pinned intact root binds nothing extra."""
    for arm in _both_confined_arms(monkeypatch):
        root = tmp_path / arm
        root.mkdir()
        target = root / ".bmad-loop" / "operator"
        platform_util.make_dirs_confined(target, confine_root=root, root_identity=os.lstat(root))
        platform_util.make_dirs_confined(target, confine_root=root)
        assert target.is_dir(), arm


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("link_at", [".bmad-loop", ".bmad-loop/operator"])
def test_make_dirs_confined_refuses_a_link_below_the_root(tmp_path, monkeypatch, link_at):
    """A link at any component below the root refuses on both arms before
    anything is created through it; the link's target stays empty.

    Ablation: replace the body with `target.mkdir(parents=True, exist_ok=True)` and
    the `.bmad-loop` row grows `outside/operator/` while neither row raises."""
    for arm in _both_confined_arms(monkeypatch):
        root = tmp_path / arm
        (root / link_at).parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / f"outside-{arm}"
        outside.mkdir()
        (root / link_at).symlink_to(outside, target_is_directory=True)

        with pytest.raises(platform_util.UnconfinedWriteError):
            platform_util.make_dirs_confined(root / ".bmad-loop" / "operator", confine_root=root)
        assert list(outside.iterdir()) == [], arm


def test_unlink_confined_prunes_only_an_emptied_parent(tmp_path, monkeypatch):
    """The positive half on both arms: the file goes, a parent still holding
    another entry stays, the last unlink prunes it, and ``missing_ok`` tolerates a
    missing file and a missing directory alike — without it, absence raises."""
    for arm in _both_confined_arms(monkeypatch):
        root = tmp_path / arm
        records = root / ".bmad-loop" / "operator"
        records.mkdir(parents=True)
        (records / "a.json").write_text("a", encoding="utf-8")
        (records / "b.json").write_text("b", encoding="utf-8")
        unlink = functools.partial(
            platform_util.unlink_confined,
            confine_root=root,
            root_identity=os.lstat(root),
            prune_empty_parent=True,
        )

        unlink(records / "a.json")
        assert [p.name for p in records.iterdir()] == ["b.json"], arm
        unlink(records / "b.json")
        assert not records.exists(), arm
        unlink(records / "b.json", missing_ok=True)  # the directory is gone
        (root / ".bmad-loop").rmdir()
        unlink(records / "b.json", missing_ok=True)  # and its parent too
        with pytest.raises(FileNotFoundError):
            unlink(records / "b.json")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("link_at", [".bmad-loop", ".bmad-loop/operator"])
def test_unlink_confined_refuses_a_link_below_the_root(tmp_path, monkeypatch, link_at):
    """A link below the root refuses on both arms — ``missing_ok`` included, since
    the walk meets the link before anything missing — and the same-named file at
    its target survives.

    Ablation: replace the body with `path.unlink(missing_ok=missing_ok)` and the
    outside file is deleted on both arms."""
    for arm in _both_confined_arms(monkeypatch):
        root = tmp_path / arm
        (root / link_at).parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / f"outside-{arm}"
        victim = outside / Path(".bmad-loop/operator/r.json").relative_to(link_at)
        victim.parent.mkdir(parents=True)
        victim.write_text("theirs", encoding="utf-8")
        (root / link_at).symlink_to(outside, target_is_directory=True)

        with pytest.raises(platform_util.UnconfinedWriteError):
            platform_util.unlink_confined(
                root / ".bmad-loop" / "operator" / "r.json",
                confine_root=root,
                missing_ok=True,
                prune_empty_parent=True,
            )
        assert victim.read_text(encoding="utf-8") == "theirs", arm


@pytest.mark.skipif(
    not platform_util.DIR_FD_ANCHORED_WRITES or sys.platform == "win32",
    reason="dir-fd anchoring and POSIX symlinks",
)
def test_unlink_confined_prunes_through_the_walked_descriptor(tmp_path, monkeypatch):
    """The POSIX prune is relative to the descriptor the walk produced, so an
    ancestor swapped for a link AFTER the walk — between the unlink and the
    prune — steers nothing: the real (renamed-aside) directory is pruned and an
    empty same-named directory at the link's target survives.

    Ablation: prune with `_rmdir_if_empty(parent)` (by path) instead of the
    `os.rmdir(..., dir_fd=...)` and the outside directory is removed."""
    root = tmp_path / "project"
    records = root / ".bmad-loop" / "operator"
    records.mkdir(parents=True)
    (records / "r.json").write_text("mine", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "operator").mkdir(parents=True)
    real_unlink_at = platform_util.unlink_at

    def unlink_then_swap(dir_fd, name):
        real_unlink_at(dir_fd, name)
        (root / ".bmad-loop").rename(root / ".bmad-loop-aside")
        (root / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(platform_util, "unlink_at", unlink_then_swap)

    platform_util.unlink_confined(records / "r.json", confine_root=root, prune_empty_parent=True)

    assert (outside / "operator").is_dir()
    assert list((root / ".bmad-loop-aside").iterdir()) == []


def test_create_exclusive_confined_refuses_out_of_root_and_parent_refs(tmp_path):
    """Message-matched on each gate's OWN words, per the confined writers' rows:
    the lexical prefix gate is redundant with the walk by construction, so a
    type-only assertion would grade a refusal for the wrong reason."""
    root = tmp_path / "project"
    (root / "specs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(platform_util.UnconfinedWriteError, match="is not under"):
        platform_util.create_exclusive_confined(outside / "f.json", confine_root=root)
    dodgy = root / "specs" / ".." / ".." / "outside" / "f.json"
    with pytest.raises(platform_util.UnconfinedWriteError, match="climbs back out"):
        platform_util.create_exclusive_confined(dodgy, confine_root=root)
    assert list(outside.iterdir()) == []  # neither refusal created anything


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


@ANCHORED
def test_atomic_write_bytes_confined_runs_callback_with_live_published_fd(tmp_path):
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    target = parent / "story.md"
    observed: list[tuple[int, bytes]] = []

    def verify_after_publish(published_fd: int | None) -> None:
        assert published_fd is not None
        assert stat.S_ISREG(os.fstat(published_fd).st_mode)
        os.lseek(published_fd, 0, os.SEEK_SET)
        observed.append((published_fd, os.read(published_fd, 100)))

    platform_util.atomic_write_bytes_confined(
        target, b"published", confine_root=root, _after_replace=verify_after_publish
    )

    assert observed and observed[0][1] == b"published"
    with pytest.raises(OSError) as closed:  # EBADF: "Bad file descriptor" on POSIX,
        os.fstat(observed[0][0])  # "Invalid handle" on Windows
    assert closed.value.errno == errno.EBADF


def test_atomic_write_bytes_confined_fallback_post_callback_receives_no_fd(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    target = parent / "story.md"
    observed: list[int | None] = []

    def verify_after_publish(dir_fd: int | None) -> None:
        observed.append(dir_fd)
        assert target.read_bytes() == b"published"

    platform_util.atomic_write_bytes_confined(
        target, b"published", confine_root=root, _after_replace=verify_after_publish
    )

    assert observed == [None]


def test_confined_fallback_callback_failure_does_not_unlink_reused_temp_name(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    target = parent / "story.md"
    reused = parent / "fixed.tmp"
    failure = RuntimeError("post-publication verification failed")

    def fixed_temp(_target: Path) -> tuple[int, str]:
        fd = os.open(reused, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return fd, str(reused)

    def fail_after_publish(dir_fd: int | None) -> None:
        assert dir_fd is None
        reused.write_bytes(b"another writer")
        raise failure

    monkeypatch.setattr(platform_util, "_mkstemp_beside", fixed_temp)
    with pytest.raises(RuntimeError) as excinfo:
        platform_util.atomic_write_bytes_confined(
            target, b"published", confine_root=root, _after_replace=fail_after_publish
        )

    assert excinfo.value is failure
    assert target.read_bytes() == b"published"
    assert reused.read_bytes() == b"another writer"


@ANCHORED
def test_confined_anchored_callback_failure_does_not_unlink_reused_temp_name(tmp_path, monkeypatch):
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    target = parent / "story.md"
    reused = parent / "fixed.tmp"
    failure = RuntimeError("post-publication verification failed")

    def fixed_temp(dir_fd: int, _prefix: str, _name: str) -> tuple[int, str]:
        fd = platform_util.open_at(dir_fd, reused.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return fd, reused.name

    published_fds: list[int] = []

    def fail_after_publish(published_fd: int | None) -> None:
        assert published_fd is not None
        os.fstat(published_fd)
        published_fds.append(published_fd)
        reused.write_bytes(b"another writer")
        raise failure

    monkeypatch.setattr(platform_util, "_open_exclusive_at", fixed_temp)
    with pytest.raises(RuntimeError) as excinfo:
        platform_util.atomic_write_bytes_confined(
            target, b"published", confine_root=root, _after_replace=fail_after_publish
        )

    assert excinfo.value is failure
    assert target.read_bytes() == b"published"
    assert reused.read_bytes() == b"another writer"
    with pytest.raises(OSError) as closed:  # EBADF: "Bad file descriptor" on POSIX,
        os.fstat(published_fds[0])  # "Invalid handle" on Windows
    assert closed.value.errno == errno.EBADF


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

    def swap_after_the_walk(confine_root: Path, target: Path, **kwargs):
        fd = real_open(confine_root, target, **kwargs)
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
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
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


def test_confined_fallback_rejects_before_writable_probe_and_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    root = tmp_path / "project"
    parent = root / "specs"
    parent.mkdir(parents=True)
    target = parent / "owned.md"
    target.write_bytes(b"before")
    failure = RuntimeError("pre-staging authority loss")
    calls: list[str] = []

    def refuse_before_staging() -> None:
        calls.append("predicate")
        raise failure

    def unexpected_probe(*_args, **_kwargs) -> None:
        calls.append("writable-probe")

    def unexpected_temp(_target: Path):
        calls.append("temp")
        raise AssertionError("staged after pre-staging refusal")

    monkeypatch.setattr(platform_util, "_refuse_unwritable_target", unexpected_probe)
    monkeypatch.setattr(platform_util, "_mkstemp_beside", unexpected_temp)

    with pytest.raises(RuntimeError) as excinfo:
        platform_util.atomic_write_bytes_confined(
            target,
            b"after",
            confine_root=root,
            require_writable_target=True,
            _before_staging=refuse_before_staging,
        )

    assert excinfo.value is failure
    assert calls == ["predicate"]
    assert target.read_bytes() == b"before"


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


def test_failed_publish_cleanup_clears_a_readonly_temp(tmp_path, monkeypatch):
    """The win32 row above also asserts the staged temp is cleaned up, and that
    claim needs the chmod-and-retry arm in `_atomic_write`'s cleanup: the follow
    path's `copymode` stamps the READONLY target's bit onto the temp before the
    publish is denied, and win32's `DeleteFile` refuses a READONLY file, so the
    bare `unlink` was denied too and `suppress(OSError)` turned the denial into
    a leaked temp. Driven from POSIX — where unlink consults the parent
    directory and the arm never fires on its own — by faulting the publish and
    teaching `os.unlink` DeleteFile's rule: refuse until a chmod grants
    owner-write.

    Ablation: drop the `except PermissionError` chmod-and-retry arm and this
    fails on the temp surviving beside the target."""
    target = tmp_path / "sprint-status.yaml"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o444)
    real_unlink = os.unlink

    def deny_publish(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        raise PermissionError(errno.EACCES, "Access is denied", str(dst))

    def win32_unlink(path):
        if not os.stat(path).st_mode & stat.S_IWRITE:
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        real_unlink(path)

    monkeypatch.setattr(platform_util.os, "replace", deny_publish)
    monkeypatch.setattr(platform_util.os, "unlink", win32_unlink)
    try:
        with pytest.raises(PermissionError):
            platform_util.atomic_write_text(target, "after")
    finally:
        target.chmod(0o644)

    assert target.read_text(encoding="utf-8") == "before"
    assert list(tmp_path.glob("*.tmp")) == []  # cleaned up, not leaked READONLY


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


# ------------------------------------------------------------ filesystem_name


@pytest.mark.skipif(sys.platform == "win32", reason="the non-win32 arm")
def test_filesystem_name_off_win32_is_unknown_with_the_platform(tmp_path):
    """DW-444: off win32 the label is `unknown (<platform>)` — no mount-table
    arm, because the only caller is the path-based fallback no POSIX host takes
    (every one has `O_DIRECTORY`). Never `""`.

    Ablation: return `""` (or restore a `/proc/self/mounts` arm) and this fails."""
    assert platform_util.filesystem_name(tmp_path) == f"unknown ({sys.platform})"


def test_filesystem_name_reports_a_ctypes_fault_as_unknown(tmp_path, monkeypatch):
    """Never `""` and never a raise: a fault inside the win32 volume query
    (here a ctypes failure, on any host) becomes an `unknown (<reason>)` label a
    reader can tell from a real answer.

    Ablation: drop the `except` in `filesystem_name` and this raises."""
    import ctypes

    def refuse(*_args, **_kwargs):
        raise OSError("simulated ctypes fault")

    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", refuse, raising=False)
    label = platform_util.filesystem_name(tmp_path)
    assert label.startswith("unknown (") and "simulated ctypes fault" in label
    assert platform_util.filesystem_type(label) == "unknown"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("NTFS at C:\\", "NTFS"),
        ("ReFS at D:\\mnt\\vol\\", "ReFS"),
        ("unknown (linux)", "unknown"),
        ("unknown (GetVolumeInformationW failed on C:\\: winerror 5)", "unknown"),
    ],
)
def test_filesystem_type_strips_the_volume_path(label, expected):
    """DW-444: the diagnostics-safe type of a label — the volume path (and any
    reason an `unknown` label quotes) never survives."""
    assert platform_util.filesystem_type(label) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="win32 volume information")
def test_filesystem_name_labels_a_win32_volume(tmp_path):
    """DW-444's host: `GetVolumePathNameW` + `GetVolumeInformationW` name the
    volume's filesystem (`NTFS at C:\\`, `ReFS at D:\\`, ...)."""
    label = platform_util.filesystem_name(tmp_path)
    assert not label.startswith("unknown"), label
    fs_name, sep, volume = label.partition(" at ")
    assert sep and fs_name and volume.endswith("\\")
    assert str(tmp_path).casefold().startswith(volume.casefold().rstrip("\\"))
