"""Backend-registry selection proof.

The multiplexer seam selects its transport backend through a registry
(:func:`~bmad_loop.adapters.multiplexer.register_multiplexer`) rather than a
hardcoded constructor, so a new OS/backend is a registration — not a core edit.
These tests pin the selection precedence (env var > policy [mux] backend >
platform default > first available platform match > the historical fallback),
the safe tmux fallback, detect_multiplexers, and the lru_cache gotcha. Backends
register a sentinel ``object()`` factory where availability doesn't matter (a
missing ``available()`` reads as unavailable); availability-sensitive tests use
the tiny :class:`_Stub` instead.
"""

import shutil
import sys
from pathlib import Path

import pytest

from bmad_loop import adapters
from bmad_loop.adapters import multiplexer as m
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer
from bmad_loop.adapters.tmux_backend import TmuxMultiplexer


class _Stub:
    """Minimal backend double for selection tests: fixed availability/version.
    Selection only touches available()/version(), so the full ABC is overkill."""

    def __init__(self, avail=True, version=None, version_error=None):
        self._avail = avail
        self._version = version
        self._version_error = version_error

    def available(self):
        if isinstance(self._avail, Exception):
            raise self._avail
        return self._avail

    def version(self):
        if isinstance(self._version, Exception):
            raise self._version
        return self._version

    def version_error(self):
        if isinstance(self._version_error, Exception):
            raise self._version_error
        return self._version_error


def _platform_default_name():
    """This host's platform-default backend name (win32 differs), so the tests
    stay deterministic on both CI legs."""
    return m._PLATFORM_DEFAULTS.get(sys.platform, m._DEFAULT_BACKEND)


@pytest.fixture
def fresh_registry(monkeypatch):
    """Isolate the global registry + lru_cache + configured choice: snapshot,
    clear, restore. The env override is removed so a test opts in explicitly.
    Teardown restores the real tmux registry so unrelated tests see normal
    selection."""
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    saved_backends = list(m._BACKENDS)
    saved_loaded = m._BUILTINS_LOADED
    saved_configured = m._CONFIGURED
    saved_ext_loaded = m._EXTERNALS_LOADED
    saved_ext_errors = dict(m._EXTERNAL_ERRORS)
    m._BACKENDS.clear()
    m._BUILTINS_LOADED = False
    m._CONFIGURED = None
    # Externals stay OFF by default (the flag reads "already scanned"): these
    # tests pin builtin selection, and a real entry-point scan here would let
    # whatever adapters happen to be installed on the dev box leak in. The
    # discovery tests opt back in by resetting the flag themselves.
    m._EXTERNALS_LOADED = True
    m._EXTERNAL_ERRORS.clear()
    m.get_multiplexer.cache_clear()
    yield m
    m._BACKENDS[:] = saved_backends
    m._BUILTINS_LOADED = saved_loaded
    m._CONFIGURED = saved_configured
    m._EXTERNALS_LOADED = saved_ext_loaded
    m._EXTERNAL_ERRORS.clear()
    m._EXTERNAL_ERRORS.update(saved_ext_errors)
    m.get_multiplexer.cache_clear()


def test_default_matches_platform(fresh_registry, monkeypatch):
    """No override → the loop's platform match picks the right builtin (tmux
    registers ``p != 'win32'``, psmux ``p == 'win32'``) through the
    platform-default branch, not the bottom fallback (which returns the same
    class when the backend is unavailable, so the reason must be asserted).
    Both legs are pinned regardless of the host OS."""
    monkeypatch.setattr(PsmuxMultiplexer, "available", lambda self: True)
    monkeypatch.setattr(sys, "platform", "win32")
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, PsmuxMultiplexer)
    assert (name, reason) == ("psmux", "platform-default")

    monkeypatch.setattr(TmuxMultiplexer, "available", lambda self: True)
    monkeypatch.setattr(sys, "platform", "linux")
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, TmuxMultiplexer)
    assert (name, reason) == ("tmux", "platform-default")


def test_env_override_selects_named_backend(fresh_registry, monkeypatch):
    """``BMAD_LOOP_MUX_BACKEND`` resolves a backend by name without monkeypatching
    sys.platform. ``matches`` returns False here, so only the name path can pick it."""
    sentinel = object()
    fresh_registry.register_multiplexer("fake", lambda p: False, lambda: sentinel)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "fake")
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is sentinel


def test_env_override_tmux_returns_tmux(fresh_registry, monkeypatch):
    """Forcing the default by name still works (name match short-circuits)."""
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)


def test_unknown_forced_name_raises(fresh_registry, monkeypatch):
    """An explicit but unregistered forced name is a misconfiguration: it must fail
    loudly rather than silently fall back to tmux (wrong/unsafe on a non-POSIX host)."""
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "nope")
    fresh_registry.get_multiplexer.cache_clear()
    with pytest.raises(MultiplexerError, match="nope"):
        fresh_registry.get_multiplexer()


def test_match_based_selection_wins_by_order(fresh_registry):
    """Registration order breaks ties among *available* platform-matching
    backends that aren't the platform default: the first registered wins.
    Builtins are suppressed so no real binary probe can skew the outcome."""
    fresh_registry._BUILTINS_LOADED = True
    first = _Stub(avail=True)
    second = _Stub(avail=True)
    fresh_registry.register_multiplexer("first", lambda p: p == sys.platform, lambda: first)
    fresh_registry.register_multiplexer("second", lambda p: p == sys.platform, lambda: second)
    backend, name, reason = fresh_registry._select()
    assert backend is first
    assert (name, reason) == ("first", "first-match")


def test_get_multiplexer_is_cached(fresh_registry):
    """One process-wide instance: repeated calls return the same object."""
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is fresh_registry.get_multiplexer()


def test_register_invalidates_cached_selection(fresh_registry):
    """register_multiplexer() must clear the singleton cache so a backend registered
    *after* a prior get_multiplexer() call is honored — without the caller manually
    clearing the cache. Guards the "register at import time, any order" contract."""
    fresh_registry.get_multiplexer()  # populate the cache
    assert fresh_registry.get_multiplexer.cache_info().currsize == 1
    fresh_registry.register_multiplexer("fake", lambda p: False, lambda: object())
    # no manual cache_clear() here — registration is responsible for invalidating it
    assert fresh_registry.get_multiplexer.cache_info().currsize == 0


# ---------------------------------------------------------------------------
# Selection precedence (issue #87): env > policy > platform default >
# first available match > historical fallback


def test_policy_choice_selects_by_name_bypassing_match_and_availability(fresh_registry):
    """configure_multiplexer installs the [mux] backend choice: exact-name
    selection that, like the env override, ignores the platform predicate and
    available() — an explicit choice is trusted."""
    sentinel = object()  # no available() at all: forced selection must not probe it
    fresh_registry.register_multiplexer("fake", lambda p: False, lambda: sentinel)
    fresh_registry.configure_multiplexer("fake")
    assert fresh_registry.get_multiplexer() is sentinel


def test_env_override_beats_policy_choice(fresh_registry, monkeypatch):
    """A per-invocation env override outranks the persisted policy choice."""
    by_policy, by_env = object(), object()
    fresh_registry.register_multiplexer("pol", lambda p: False, lambda: by_policy)
    fresh_registry.register_multiplexer("env", lambda p: False, lambda: by_env)
    fresh_registry.configure_multiplexer("pol")
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "env")
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is by_env


def test_unknown_policy_name_raises_naming_the_policy_file(fresh_registry):
    """A persisted choice that matches no registered backend is a
    misconfiguration and must fail loudly, pointing at the file to edit."""
    fresh_registry.configure_multiplexer("ghost", origin=Path("/repo/.bmad-loop/policy.toml"))
    with pytest.raises(MultiplexerError, match=r"ghost.*policy\.toml"):
        fresh_registry.get_multiplexer()


def test_platform_default_outranks_registration_order(fresh_registry):
    """When the platform's default backend is registered and available it wins,
    even against an available backend registered earlier."""
    fresh_registry._BUILTINS_LOADED = True
    early = _Stub(avail=True)
    default = _Stub(avail=True)
    fresh_registry.register_multiplexer("early", lambda p: p == sys.platform, lambda: early)
    fresh_registry.register_multiplexer(
        _platform_default_name(), lambda p: p == sys.platform, lambda: default
    )
    backend, name, reason = fresh_registry._select()
    assert backend is default
    assert (name, reason) == (_platform_default_name(), "platform-default")


def test_unavailable_platform_default_falls_through_to_first_available(fresh_registry):
    """A registered-but-unavailable default doesn't block selection: the first
    available platform match is chosen instead."""
    fresh_registry._BUILTINS_LOADED = True
    other = _Stub(avail=True)
    fresh_registry.register_multiplexer(
        _platform_default_name(), lambda p: p == sys.platform, lambda: _Stub(avail=False)
    )
    fresh_registry.register_multiplexer("other", lambda p: p == sys.platform, lambda: other)
    backend, name, reason = fresh_registry._select()
    assert backend is other
    assert (name, reason) == ("other", "first-match")


def test_platform_default_requires_platform_match(fresh_registry):
    """A backend name-colliding with this platform's default but claiming a
    *different* platform must not be defaulted onto this one: the default step
    enforces matches() like every other step, and selection falls through to
    the first genuine platform match."""
    fresh_registry._BUILTINS_LOADED = True
    other = _Stub(avail=True)
    fresh_registry.register_multiplexer(
        _platform_default_name(), lambda p: False, lambda: _Stub(avail=True)
    )
    fresh_registry.register_multiplexer("other", lambda p: p == sys.platform, lambda: other)
    backend, name, reason = fresh_registry._select()
    assert backend is other
    assert (name, reason) == ("other", "first-match")


def test_all_unavailable_pins_historical_first_match_fallback(fresh_registry):
    """Nothing available → today's behavior is preserved: the first platform
    match is returned anyway (validate reports it unavailable later)."""
    fresh_registry._BUILTINS_LOADED = True
    first = _Stub(avail=False)
    fresh_registry.register_multiplexer("first", lambda p: p == sys.platform, lambda: first)
    fresh_registry.register_multiplexer(
        "second", lambda p: p == sys.platform, lambda: _Stub(avail=False)
    )
    backend, name, reason = fresh_registry._select()
    assert backend is first
    assert (name, reason) == ("first", "fallback")


def test_empty_registry_bottoms_out_at_tmux(fresh_registry):
    """No registered backend at all → the historical TmuxMultiplexer fallback."""
    fresh_registry._BUILTINS_LOADED = True  # suppress builtins; registry stays empty
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, TmuxMultiplexer)
    assert (name, reason) == ("tmux", "fallback")


def test_raising_available_probe_reads_as_unavailable(fresh_registry):
    """A backend whose available() blows up must not crash selection — it is
    skipped exactly like an unavailable one."""
    fresh_registry._BUILTINS_LOADED = True
    ok = _Stub(avail=True)
    fresh_registry.register_multiplexer(
        "broken", lambda p: p == sys.platform, lambda: _Stub(avail=RuntimeError("boom"))
    )
    fresh_registry.register_multiplexer("ok", lambda p: p == sys.platform, lambda: ok)
    backend, name, _ = fresh_registry._select()
    assert backend is ok and name == "ok"


def test_configure_multiplexer_clears_cache_only_on_change(fresh_registry):
    """Re-configuring the same value must keep the cached singleton identity;
    an actual change must invalidate it (mirrors register_multiplexer)."""
    sentinel = object()
    fresh_registry.register_multiplexer("fake", lambda p: False, lambda: sentinel)
    before = fresh_registry.get_multiplexer()  # auto-selected, cache populated
    fresh_registry.configure_multiplexer(None)  # same effective value (auto)
    assert fresh_registry.get_multiplexer() is before  # cache survived
    fresh_registry.configure_multiplexer("fake")  # real change
    assert fresh_registry.get_multiplexer.cache_info().currsize == 0
    assert fresh_registry.get_multiplexer() is sentinel
    fresh_registry.configure_multiplexer("fake")  # same value again
    assert fresh_registry.get_multiplexer.cache_info().currsize == 1


def test_empty_string_configuration_means_auto(fresh_registry, monkeypatch):
    """configure_multiplexer("") — an unset policy key — must behave exactly
    like None, not force an empty backend name. Pinned to a POSIX platform so the
    auto-default is tmux regardless of any installed win32-matching external."""
    monkeypatch.setattr(sys, "platform", "linux")
    fresh_registry.configure_multiplexer("")
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)


# ---------------------------------------------------------------------------
# detect_multiplexers — the registry enumeration behind `bmad-loop mux` and
# the validate preflight


def test_detect_multiplexers_rows_and_selection_mark(fresh_registry):
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "off-platform", lambda p: False, lambda: _Stub(avail=True, version="v9")
    )
    fresh_registry.register_multiplexer(
        "chosen", lambda p: p == sys.platform, lambda: _Stub(avail=True, version="chosen 1.0")
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert set(rows) == {"off-platform", "chosen"}
    assert rows["off-platform"].matches_platform is False
    assert rows["off-platform"].available is True
    assert rows["off-platform"].selected is False and rows["off-platform"].reason == ""
    assert rows["chosen"].selected is True
    assert rows["chosen"].reason == "first-match"
    assert rows["chosen"].version == "chosen 1.0"


def test_detect_multiplexers_survives_forced_unknown_name(fresh_registry, monkeypatch):
    """Diagnostics must work on a misconfigured host: a forced unknown backend
    yields rows with no selected mark instead of raising."""
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "ghost")
    rows = fresh_registry.detect_multiplexers()
    assert rows  # the tmux builtin row is still listed
    assert not any(r.selected for r in rows)


def test_detect_multiplexers_guards_broken_probes(fresh_registry):
    """A sentinel with no available()/version() and a probe that raises both
    read as unavailable rows, never an exception."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer("bare", lambda p: False, lambda: object())
    fresh_registry.register_multiplexer(
        "raiser", lambda p: p == sys.platform, lambda: _Stub(avail=RuntimeError("boom"))
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["bare"].available is False and rows["bare"].version is None
    assert rows["raiser"].available is False


def test_detect_multiplexers_version_crash_keeps_availability(fresh_registry):
    """version() is cosmetic: a backend whose availability probes True but
    whose version() raises must still read available (and selected, since
    _select never calls version()) — never a contradictory
    selected=True/available=False row."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "verless",
        lambda p: p == sys.platform,
        lambda: _Stub(avail=True, version=RuntimeError("boom")),
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["verless"].available is True
    assert rows["verless"].version is None
    assert rows["verless"].selected is True and rows["verless"].reason == "first-match"


def test_detect_multiplexers_carries_the_dropped_version_diagnostic(fresh_registry):
    """The row is what `bmad-loop mux` renders, so the diagnostic version() drops
    has to survive the trip out of the backend (#428) — a VERSION cell of `-`
    otherwise says the same thing for a crashed probe and a quiet binary."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "crasher",
        lambda p: p == sys.platform,
        lambda: _Stub(avail=True, version=None, version_error="tmux -V failed: killed"),
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["crasher"].version is None
    assert rows["crasher"].version_error == "tmux -V failed: killed"


def test_detect_multiplexers_carries_the_diagnostic_off_an_unavailable_backend(fresh_registry):
    """The flagship #428 shape. psmux's available() gates on its own version()
    (psmux_backend), so an AV-blocked or corrupt binary reads available=False —
    the row that most needs an explanation is the one where the availability
    verdict already failed. The read hangs off the factory `try`, not off the
    availability answer, and nothing else pins that."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "blocked",
        lambda p: p == sys.platform,
        lambda: _Stub(avail=False, version=None, version_error="psmux -V failed: [WinError 5]"),
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["blocked"].available is False
    assert rows["blocked"].version_error == "psmux -V failed: [WinError 5]"


def test_detect_multiplexers_reads_no_diagnostic_off_a_working_version(fresh_registry):
    """Only a None version has a failure to explain. Asking a backend that just
    answered would report a stale error from some earlier probe — the accessor
    describes the LAST call, and this one succeeded."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "fine",
        lambda p: p == sys.platform,
        lambda: _Stub(avail=True, version="fine 1.0", version_error="stale"),
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["fine"].version == "fine 1.0"
    assert rows["fine"].version_error is None


def test_detect_multiplexers_guards_a_raising_version_error(fresh_registry):
    """The never-raises contract covers the new probe too: a backend whose
    version_error() blows up must still produce a row."""
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer(
        "rude",
        lambda p: p == sys.platform,
        lambda: _Stub(avail=True, version=None, version_error=RuntimeError("boom")),
    )
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert rows["rude"].available is True and rows["rude"].version_error is None


# ------------------------------------------------ fold_version, directly (#321)
#
# The seam folds and then every inline consumer folds again defensively, so the
# helper's own edges have to hold on their own — and most of them are invisible
# through any single consumer's rendering (`r.version or "-"` collapses "" and
# None to the same cell, and the tmux-family probe strips its capture, so the
# whitespace-only input is only reachable from a duck-typed backend at all).


def test_fold_version_edges():
    assert m.fold_version(None) is None  # the seam's own default version()
    assert m.fold_version("") is None
    # Truthy, but no segment survives: the sentinel this seam documents is None,
    # not a version that happens to be empty.
    assert m.fold_version("  \n \t \n") is None
    assert m.fold_version("tmux 3.4") == "tmux 3.4"  # single line: byte-identical


def test_fold_version_is_idempotent():
    """Load-bearing, and nothing else asserts it: `version()` folds at the
    tmux-family seam and detect_multiplexers / platform_preflight / collect_env /
    mux_usable / make_adapters each fold that already-folded result again."""
    for raw in ("tmux 3.4", "tmux 3.3.7\npsmux 3.3.7", "tmux 3.4 " + "x" * 300):
        once = m.fold_version(raw)
        assert m.fold_version(once) == once


def test_fold_version_bounds_the_line_it_produces():
    """`mux` sizes every column off the widest cell, so an unbounded single line
    breaks the table exactly as an embedded newline did. The cut is at the tail,
    which the one parser of this string (the psmux gate) never reads."""
    folded = m.fold_version("tmux 3.4 " + "x" * 300)

    assert folded is not None
    assert len(folded) == m.VERSION_MAX_CHARS
    assert folded.startswith("tmux 3.4 ") and folded.endswith("…")


def test_forced_unusable_warning_folds_the_reported_version(fresh_registry, monkeypatch, capsys):
    """The warning renders with `{version!r}`, which already escapes a newline —
    so this fold buys readability, not safety. Assert the readable form, or the
    site is indistinguishable from dead code."""
    monkeypatch.setattr(fresh_registry, "_FORCED_UNUSABLE_WARNED", False)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "alpha")
    fresh_registry._BUILTINS_LOADED = True
    fresh_registry.register_multiplexer("alpha", lambda p: True, lambda: _Stub(avail=False))

    assert fresh_registry.mux_usable(_Stub(avail=False, version="alpha 1.2\nbuild 7")) is True

    assert "version: 'alpha 1.2; build 7'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Bundled-registry platform behavior. These exercise the REAL builtin load (they
# leave `_BUILTINS_LOADED` False) and control the outcome by monkeypatching
# `sys.platform` (deterministic on both CI legs) and the shared `shutil.which`.
# The herdr selection facts that used to live here moved with the backend to
# pbean/bmad-loop-adapter-herdr (tests/test_registration.py).


def _which_only(*available: str):
    """`shutil.which` stub: only the named binaries resolve, everything else is
    absent. Patches the shared stdlib module, so every backend's available()
    probe sees it."""
    names = set(available)
    return lambda name, *a, **k: f"/usr/bin/{name}" if name in names else None


def test_win32_bottoms_out_at_psmux_with_no_externals(fresh_registry, monkeypatch):
    """On native Windows the bundled psmux backend matches (`p == 'win32'`), so
    even with nothing available `_select` bottoms out at psmux — not the tmux
    bottom fallback. psmux is the win32 platform default, but that step needs
    availability; an unavailable psmux is instead reached through the historical
    fallback (first platform match regardless of availability) and reported
    unavailable by validate. tmux never matches win32, so it stays out of the
    running here."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _which_only())  # nothing available
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, PsmuxMultiplexer)
    assert (name, reason) == ("psmux", "fallback")
    # psmux is both the win32 platform default and the sole bundled win32 match
    assert fresh_registry._PLATFORM_DEFAULTS.get("win32") == "psmux"


# Builtin seeding inside `register_multiplexer` (#565). First-wins only protects a
# bundled name if the bundled entry is guaranteed to be registered first; these pin
# that guarantee and the flag position that makes the seeding safe to re-enter.


def test_builtins_win_over_an_external_registered_before_any_resolution(
    fresh_registry, monkeypatch
):
    """The shadowing hole that first-wins ALONE does not close.

    An out-of-tree backend registers as an import side effect, and that import is
    not always triggered by a mux resolution: a plugin's ``[python]`` module is
    exec'd in-process by ``plugins/registry.py``, with no ordering relationship to
    the first ``get_multiplexer()`` call. Arriving first under a bundled name, it
    would sit ahead of the bundled tmux entry in the ordered ``_BACKENDS`` list and
    every first-match consumer would pick it — the whole process silently driving a
    third-party transport. Only ``register_multiplexer`` seeding the builtins on
    its own side makes first-wins an invariant instead of an ordering coincidence.

    ABLATION: drop the ``_load_builtin_backends()`` call from
    ``register_multiplexer`` and this reddens — while
    ``tests/test_external_backends.py``'s ``test_externals_load_after_builtins``
    stays green, because ``_select`` seeds the builtins on that path before the
    scan's registration ever runs. That asymmetry is the finding: the existing test
    pins the scan path and cannot see this hole at all."""
    sentinel = object()
    # The plugin trigger: a direct registration under a bundled name, with no
    # entry-point scan involved (the fixture parks `_EXTERNALS_LOADED = True`).
    fresh_registry.register_multiplexer("tmux", lambda p: True, lambda: sentinel)

    # (a) the bundled backend still wins the name. Forcing by name bypasses both
    # the platform predicate and available(), so this is deterministic on the
    # Linux and Windows CI legs alike.
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)

    # (b) the ordering that makes (a) true: the bundled entry is first and the
    # external is still present behind it — an append-only list, not a dedup'ing
    # dict, so a shadowed name legitimately appears twice.
    names = [name for name, _, _ in fresh_registry._BACKENDS]
    assert names[0] == "tmux" and names.count("tmux") == 2
    # Names alone do not discriminate: the shadowing external registers under
    # "tmux" too, so both assertions above still hold under the ABLATION. The
    # first entry being the *bundled* factory is the half that carries the
    # guarantee, so pin it explicitly rather than inferring it from the name.
    first_tmux = next(factory for name, _, factory in fresh_registry._BACKENDS if name == "tmux")
    assert first_tmux is TmuxMultiplexer


def test_builtin_seeding_is_reentrant_and_registers_each_builtin_once(fresh_registry):
    """One ordinary registration seeds the builtins exactly once and terminates.

    ``_load_builtin_backends`` registers its two backends by calling
    ``register_multiplexer``, which now calls ``_load_builtin_backends`` — so the
    seeding re-enters itself, and only the flag's position stops it.

    ABLATION: move ``_BUILTINS_LOADED = True`` back below the two
    ``register_multiplexer(...)`` calls and this reddens with a RecursionError."""
    # Without the flag move, the `register_multiplexer` calls inside
    # `_load_builtin_backends` re-enter it with the flag still False, and it
    # recurses without end.
    fresh_registry.register_multiplexer("extra", lambda p: False, lambda: object())
    names = [name for name, _, _ in fresh_registry._BACKENDS]
    # Seeded once, in bundled order, with the caller's own entry appended last.
    assert names == ["tmux", "psmux", "extra"]


def test_a_failed_builtin_import_leaves_the_seeding_retryable(fresh_registry, monkeypatch):
    """A transient import failure must not permanently poison the registry.

    ABLATION: move ``_BUILTINS_LOADED = True`` to the very top of
    ``_load_builtin_backends``, above the two imports, and this reddens — the flag
    reads True after the failed import and the retry early-outs on a registry that
    is permanently missing both bundled backends."""
    # The flag sits BELOW the two backend imports precisely so a transient import
    # failure retries; the function's docstring and comment both claim exactly
    # that property. The adapter twin sets its flag at the very top only because
    # its builtins are lazy thunks with nothing to import first.
    key = "bmad_loop.adapters.psmux_backend"
    # Evicting the entry alone leaks: the retry below re-imports the module for
    # real, which rebinds `psmux_backend` on the *parent package object* to the
    # new module, and restoring sys.modules does not undo that rebinding. Pin the
    # attribute through monkeypatch so the original comes back with it. Without
    # it the two import spellings disagree for the rest of the worker --
    # `from bmad_loop.adapters import psmux_backend` is a getattr on the package
    # and answers the new module, while `from bmad_loop.adapters.psmux_backend
    # import x` resolves through sys.modules and answers the original -- so a
    # later test asserts on one module's globals while the code under test writes
    # the other's. (Same hazard, same fix, as the `bmad_loop.tui` eviction in
    # tests/test_tui_app.py.)
    monkeypatch.setattr(adapters, "psmux_backend", sys.modules[key])
    # A None value in sys.modules makes `from ... import ...` raise
    # ModuleNotFoundError (an ImportError subclass) without touching the disk.
    monkeypatch.setitem(sys.modules, key, None)
    with pytest.raises(ImportError):
        fresh_registry._load_builtin_backends()
    assert fresh_registry._BACKENDS == []
    assert fresh_registry._BUILTINS_LOADED is False

    # Let the import succeed again and confirm the retry seeds both builtins.
    monkeypatch.delitem(sys.modules, key)
    fresh_registry._load_builtin_backends()
    assert [name for name, _, _ in fresh_registry._BACKENDS] == ["tmux", "psmux"]
