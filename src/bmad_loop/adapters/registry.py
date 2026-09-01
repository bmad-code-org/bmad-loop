"""Coding-CLI adapter registry — the out-of-tree extension seam for the CLI axis.

The transport axis (:mod:`~.multiplexer`) has long been extensible out-of-tree:
a backend registers through ``register_multiplexer`` and a co-installed package
is discovered via the ``bmad_loop.mux_backends`` entry-point group. This module
is the same seam for the *other* axis — which adapter **class** drives a coding
CLI. A CLI that fits the tmux-injection + hook-signal transport still needs no
Python at all (drop a TOML :class:`~.profile.CLIProfile` and run ``bmad-loop
probe-adapter``); this registry is for a CLI that needs a whole new adapter
subclass (the shipped example is the HTTP/SSE ``opencode-http`` adapter, which no
tmux profile can host).

An adapter *kind* is selected by **data**: ``profile.adapter`` names the kind, and
:func:`get_adapter_kind` resolves it. Two registration-time dataclasses carry the
family:

- :class:`AdapterKind` — ``name`` + ``needs_mux`` (does the family drive a
  terminal multiplexer?) + ``load``, a lazy thunk returning the builder. The thunk
  is why registration, validation (``known_adapter_kinds``) and listing
  (``detect_adapters``) never import a heavy adapter module — nor an optional
  dependency like ``httpx``, which only the opencode family pulls in at
  construction.
- :class:`AdapterBuilder` — the ``plain`` class, the ``_DevSynthesisMixin``-composed
  ``dev`` class (both share the ``(*args, paths, **kwargs)`` dev ``__init__``), and
  the family's construction-failure exception type(s) (``()`` = none;
  ``(OpencodeServerError,)`` for the HTTP family, which fails loud when its server
  can't spawn). ``runsetup.make_adapters`` converts a raised ``construct_error``
  into a ``SystemExit``.

Bundled kinds register from :func:`_load_builtin_adapters` (:data:`GENERIC`,
:data:`OPENCODE_HTTP`); out-of-tree kinds arrive at import time, triggered by the
``bmad_loop.adapters`` entry-point scan in :func:`_load_external_adapters` — so a
pip/uv co-installed adapter package is selectable with no config step. Builtins
are seeded by :func:`register_adapter` itself, so an external can never shadow a
bundled name however early its import lands. A broken third-party distribution
degrades to a recorded, surfaced reason (:func:`external_adapter_errors`) and can
never break selection — one reason per failing distribution, kept even when two of
them advertise the same entry-point name (:mod:`~.entrypoints`).

**Two deliberate asymmetries versus the multiplexer seam** (this is not a
copy-paste omission):

- *No process-wide cache / no ``cache_clear``.* The multiplexer is a single
  process-wide singleton behind an ``lru_cache``; adapters are built **per run**
  in ``runsetup.make_adapters``, which keeps its own ``by_cfg`` cache keyed on
  ``(resolved-config, synthesizes)``. Selection here is a pure registry lookup, so
  there is nothing to cache and :func:`register_adapter` invalidates nothing.
- *No ``configure_*`` / ``matches(platform)`` / platform defaults.* The multiplexer
  is chosen by a policy knob and a ``sys.platform`` predicate; an adapter kind is
  chosen by the ``profile.adapter`` field alone. There is no host-dependent
  auto-selection and no persisted choice to install, so none of that machinery
  exists — ``get_adapter_kind(name)`` fails loud on an unknown name rather than
  falling back.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from dataclasses import dataclass

from .entrypoints import record_load_error

# The two bundled kind names, as constants rather than literals scattered across
# modules. `validate`'s httpx check keys on OPENCODE_HTTP because httpx is *that
# family's* optional extra — a fact about one bundled family, which is a different
# thing from the set of VALID kinds (that set is only ever `known_adapter_kinds()`,
# never a literal). GENERIC is the `profile.adapter` default.
GENERIC = "generic"
OPENCODE_HTTP = "opencode-http"
KILO_HTTP = "kilo-http"


class AdapterError(Exception):
    """An adapter kind could not be resolved (an unknown ``profile.adapter``).

    Construction failures a *known* family raises during ``__init__`` are its own
    types (e.g. ``OpencodeServerError``), carried by
    :attr:`AdapterBuilder.construct_error`, not this seam-level type."""


@dataclass(frozen=True)
class AdapterBuilder:
    """The classes and failure modes of one adapter family.

    ``plain`` and ``dev`` are the two variants ``runsetup.make_adapters`` picks
    between on the ``synthesizes`` axis (a ``bmad-build-auto`` dev/review session
    gets ``dev``, which takes an extra ``paths=`` kwarg; every other role gets
    ``plain``). ``construct_error`` is the tuple of exception types the family's
    constructor raises when the session cannot be built — empty for a family that
    cannot fail construction (``generic``); the caller wraps a match into a
    ``SystemExit`` so a run aborts with a clean message instead of a traceback."""

    plain: type
    dev: type
    construct_error: tuple[type[BaseException], ...] = ()


@dataclass(frozen=True)
class AdapterKind:
    """One registered adapter family, keyed by ``name`` (the ``profile.adapter``
    value). ``needs_mux`` gates whether ``runsetup.make_adapters`` resolves and
    usability-checks the shared terminal multiplexer for this family (a hookless
    HTTP/SSE family needs no transport). ``load`` is a lazy thunk returning the
    :class:`AdapterBuilder`; it is the *only* place the family's classes (and any
    optional dependency they pull in) are imported."""

    name: str
    needs_mux: bool
    load: Callable[[], AdapterBuilder]


# ---------------------------------------------------------------- builtins


def _generic_builder() -> AdapterBuilder:
    from .generic import GenericAdapter, GenericDevAdapter

    return AdapterBuilder(plain=GenericAdapter, dev=GenericDevAdapter, construct_error=())


def _opencode_http_builder() -> AdapterBuilder:
    from .opencode_http import (
        OpencodeDevAdapter,
        OpencodeHttpAdapter,
        OpencodeServerError,
    )

    return AdapterBuilder(
        plain=OpencodeHttpAdapter,
        dev=OpencodeDevAdapter,
        construct_error=(OpencodeServerError,),
    )


def _kilo_http_builder() -> AdapterBuilder:
    from .kilo_http import (
        KiloDevAdapter,
        KiloHttpAdapter,
        OpencodeServerError,
    )

    return AdapterBuilder(
        plain=KiloHttpAdapter,
        dev=KiloDevAdapter,
        construct_error=(OpencodeServerError,),
    )


# The bundled kinds, as (name, needs_mux, load-thunk). A module constant, not
# mutable registry state, so detect_adapters can label a row builtin-vs-external
# without the fixtures having to snapshot it. `generic` drives tmux + hooks and
# needs the multiplexer; `opencode-http`/`kilo-http` are hookless HTTP/SSE and
# do not.
_BUILTIN_ADAPTERS: tuple[tuple[str, bool, Callable[[], AdapterBuilder]], ...] = (
    (GENERIC, True, _generic_builder),
    (OPENCODE_HTTP, False, _opencode_http_builder),
    (KILO_HTTP, False, _kilo_http_builder),
)
_BUILTIN_NAMES = frozenset(name for name, _, _ in _BUILTIN_ADAPTERS)

# The live registry: name -> AdapterKind. Unlike the multiplexer's ordered list
# (registration order breaks selection ties), adapter selection is a pure by-name
# lookup, so a dict with first-registration-wins semantics is the whole story.
_ADAPTERS: dict[str, AdapterKind] = {}
_BUILTINS_LOADED = False


def register_adapter(name: str, needs_mux: bool, load: Callable[[], AdapterBuilder]) -> None:
    """Register an adapter kind. ``name`` is the ``profile.adapter`` key that
    selects it; ``needs_mux`` declares whether the family drives a terminal
    multiplexer; ``load`` is the lazy builder thunk. First registration of a name
    wins, and the builtins are seeded here rather than only by the resolution
    entry points, so an out-of-tree package can never shadow a bundled name. An
    out-of-tree kind calls this at import time — no core edit required. There is
    no selection cache to invalidate (see the module docstring).

    Seeding on *this* side is what makes first-wins an invariant instead of an
    ordering coincidence. An external module runs its ``register_adapter`` calls
    as an import side effect, and the import is not always triggered by an
    adapter resolution: the documented packaging layout puts both entry points in
    one module, so the ``bmad_loop.profiles`` scan in :mod:`~.profile` — which
    runs long before any kind is resolved — imports it too, as does any plugin
    that imports the package directly. Any of those arriving first would have
    ``setdefault`` keep the external under a bundled name and silently redirect
    every default profile to it."""
    _load_builtin_adapters()
    _ADAPTERS.setdefault(name, AdapterKind(name=name, needs_mux=needs_mux, load=load))


def _load_builtin_adapters() -> None:
    """Register the bundled adapter kinds. Idempotent and lazy (called from the
    resolution entry points and from :func:`register_adapter`, not at module
    import) to stay cycle-safe: the load thunks import ``generic`` /
    ``opencode_http``, which import back through the package. Builtins register
    before externals so a bundled name keeps first-wins on any collision.

    The flag is set BEFORE the loop because the loop re-enters through
    ``register_adapter``; setting it afterwards would recurse without end."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    for name, needs_mux, load in _BUILTIN_ADAPTERS:
        register_adapter(name, needs_mux, load)


# The entry-point group an out-of-tree adapter package advertises its module
# under; importing the module runs its register_adapter call. Loader state:
# scanned-once flag + per-entry-point failure reasons for adapters/validate.
ADAPTERS_GROUP = "bmad_loop.adapters"
_EXTERNALS_LOADED = False
_EXTERNAL_ERRORS: dict[str, str] = {}


def _load_external_adapters() -> None:
    """Import every ``bmad_loop.adapters`` entry point; each module self-registers
    via :func:`register_adapter` at import time. Called after
    :func:`_load_builtin_adapters`, so builtins keep first registration.

    A broken third-party distribution must never break adapter selection:
    failures are recorded in ``_EXTERNAL_ERRORS`` (surfaced by ``bmad-loop
    adapters`` and the ``validate`` preflight via :func:`external_adapter_errors`),
    not raised. The loaded-flag is set up front: a third-party import failure is
    not transient, and retrying on every resolution would re-import (and re-fail)
    each time — mirroring the multiplexer's external scan.

    A recorded failure does NOT mean the entry point registered nothing: a module
    that registers kind A and then raises while registering kind B leaves A
    registered and selectable. Deliberate — unwinding would mean tracking which
    names a half-run import claimed, and a kind that registered cleanly is usable
    whatever else its package got wrong. The recorded reason is a fact about the
    import, not a promise about the registry.

    Entry points are visited in (name, distribution) order. ``importlib.metadata``
    yields them in distribution-discovery order, which varies with ``sys.path``, so
    without an explicit sort two hosts carrying the same packages could resolve a
    collision differently — first-wins would be a fact about the install rather
    than about the packages.

    The distribution belongs in the key because the name alone is NOT a total
    order. ``entry_points(group=...)`` does not dedup across distributions, so two
    packages advertising the same entry-point name come back as two entries, and
    ``sorted`` is stable — a name-only key resolves that tie straight back into
    ``sys.path`` order. That tie is the whole case the sort exists for: a package
    conventionally names its entry point after the kind it registers, so packages
    that collide on a kind normally collide on the entry-point name too."""
    global _EXTERNALS_LOADED
    if _EXTERNALS_LOADED:
        return
    _EXTERNALS_LOADED = True
    try:
        eps = sorted(
            importlib.metadata.entry_points(group=ADAPTERS_GROUP),
            key=lambda e: (e.name, getattr(e.dist, "name", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics path, never crash selection
        _EXTERNAL_ERRORS["<entry-point scan>"] = f"{type(exc).__name__}: {exc}"
        return
    for ep in eps:
        try:
            ep.load()  # module import runs register_adapter(...)
        except Exception as exc:  # noqa: BLE001 — one bad package must not hide the rest
            record_load_error(_EXTERNAL_ERRORS, ep, exc)


def external_adapter_errors() -> dict[str, str]:
    """Entry-point name -> failure reason(s) for every external adapter that failed
    to load this process (empty when all loaded). For diagnostics surfaces.

    One value may carry MORE than one reason, ``"; "``-joined: two distributions
    may advertise the same entry-point name, and each of their failures is kept
    (see :func:`~.entrypoints.record_load_error`). Each reason is labelled with
    its distribution whenever one is resolvable.

    Performs the scan itself rather than relying on a neighbouring
    ``known_adapter_kinds`` / ``detect_adapters`` call having run first — an
    accessor whose emptiness depends on call order reads as "nothing failed"."""
    _load_builtin_adapters()
    _load_external_adapters()
    return dict(_EXTERNAL_ERRORS)


def _known() -> str:
    return ", ".join(sorted(_ADAPTERS)) or "(none registered)"


def get_adapter_kind(name: str) -> AdapterKind:
    """Resolve the adapter kind named ``name`` (a ``profile.adapter`` value),
    loading the builtins and scanning the entry-point group first.

    Fails loud on an unknown name, listing the registered kinds — an explicit but
    unregistered adapter is a misconfiguration (a typo, or a plugin package that
    isn't installed), never something to silently fall back from. The caller
    (``runsetup.make_adapters``) adds the offending profile's name to the
    message."""
    _load_builtin_adapters()
    _load_external_adapters()
    kind = _ADAPTERS.get(name)
    if kind is None:
        raise AdapterError(f"unknown adapter kind {name!r}; known: {_known()}")
    return kind


def known_adapter_kinds() -> list[str]:
    """Sorted names of every registered adapter kind (builtins + successfully
    loaded externals). The oracle for ``validate``'s ``adapter.kind`` finding —
    validity of ``profile.adapter`` is enforced against this registry, never a
    hardcoded set."""
    _load_builtin_adapters()
    _load_external_adapters()
    return sorted(_ADAPTERS)


@dataclass(frozen=True)
class AdapterKindInfo:
    """One registered adapter kind's detection row, for ``bmad-loop adapters`` and
    the ``validate`` preflight."""

    name: str
    needs_mux: bool
    builtin: bool


def detect_adapters() -> list[AdapterKindInfo]:
    """Enumerate every registered adapter kind (builtins + loaded externals),
    sorted by name, each labelled builtin-vs-external. Never raises — this feeds
    diagnostics, which must work on a misconfigured host. The ``load`` thunk is
    never invoked, so listing stays free of any heavy adapter import."""
    _load_builtin_adapters()
    _load_external_adapters()
    return [
        AdapterKindInfo(
            name=kind.name,
            needs_mux=kind.needs_mux,
            builtin=kind.name in _BUILTIN_NAMES,
        )
        for kind in sorted(_ADAPTERS.values(), key=lambda k: k.name)
    ]
