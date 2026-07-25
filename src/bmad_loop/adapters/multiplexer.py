"""Terminal-multiplexer seam.

The coding-CLI adapter (:class:`~.base.CodingCLIAdapter`) abstracts *which CLI*
to drive and how its prompts/hooks work. This module abstracts the orthogonal
**transport** axis: how sessions, windows, and panes are created, observed, and
torn down. The bundled backends are tmux
(:class:`~.tmux_backend.TmuxMultiplexer`) and the native-Windows psmux
(:class:`~.psmux_backend.PsmuxMultiplexer`); every other backend lives out-of-tree
(the reference is the herdr adapter, https://github.com/pbean/bmad-loop-adapter-herdr)
and slots in without the rest of the codebase shelling out to ``tmux`` directly.

``TerminalMultiplexer`` is the contract a backend author implements. Operation
names mirror today's call sites verbatim so the migration is mechanical. Backends
register themselves through :func:`register_multiplexer` (bundled ones from
:func:`_load_builtin_backends`; out-of-tree ones at import time, triggered by the
``bmad_loop.mux_backends`` entry-point scan in :func:`_load_external_backends` —
so a pip/uv co-installed adapter package is selectable with no config step); the
process-wide backend is selected by registry and returned by :func:`get_multiplexer`.

Selection precedence (issue #87): the ``BMAD_LOOP_MUX_BACKEND`` env var, then the
policy ``[mux] backend`` choice (installed once per CLI invocation via
:func:`configure_multiplexer`), then the platform default when registered and
available, then the first registered backend that matches the platform and is
available, then the historical fallback (first platform match regardless of
availability, bottoming out at tmux). :func:`detect_multiplexers` enumerates the
registry for ``bmad-loop mux`` and the ``validate`` preflight.
"""

from __future__ import annotations

import functools
import importlib.metadata
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import envvars


class MultiplexerError(Exception):
    """A transport-backend operation failed. Backends raise a subclass (e.g.
    :class:`~.tmux_backend.TmuxError`) so call sites can catch the seam-level type
    without importing a backend."""


# The banner a parked window prints when its command exits (followed by the
# exit status — see :meth:`TerminalMultiplexer.new_parked_window`). Part of the
# parked-window contract: observers (e.g. the viewer launcher) detect a died
# command by finding this prefix in a pane capture, so every backend's parked
# recipe must emit it verbatim.
PARKED_EXIT_BANNER = "[bmad-loop exited"


def parse_target(target: str) -> tuple[str, str | None] | None:
    """Decode a seam-canonical window target (see :meth:`TerminalMultiplexer.target`).

    Returns ``(session, window)`` for a canonical ``=session[:window]`` token —
    ``window`` is None when absent *or* empty, so ``"=s"`` and ``"=s:"`` both
    decode to ``("s", None)`` — or None when ``target`` does not start with
    ``=``: a backend-native id (``"@1"``, ``"%3"``, ``"w1:p1"``, ...) the caller
    resolves itself. The window part is everything after the *first* ``:``;
    that split is safe because bmad-loop mints window names
    (``<kind>-<run_id>``) that never contain ``:``. Provided so a backend whose
    native addressing differs decodes the grammar with one tested helper
    instead of re-deriving it (see the herdr backend's ``_parse_target``)."""
    if not target.startswith("="):
        return None
    session, _, window = target[1:].partition(":")
    return (session, window or None)


class TerminalMultiplexer(ABC):
    """Transport backend for agent sessions: sessions, windows, and clients.

    A backend must shell out to (or otherwise drive) exactly one multiplexer and
    nothing else — it is the single place POSIX-shell / tmux knowledge is allowed
    to live. The full surface below is the contract; Phase 1 wired only the subset
    the generic adapter needs, and Phase 2 fills in the rest as ``runs.py``,
    ``tui/launch.py``, ``probe.py``, and ``tui/data.py`` migrate onto it.
    """

    # ------------------------------------------------------------ targets

    def target(self, session: str, window: str | None = None) -> str:
        """Format the seam-canonical target token for ``session`` (optionally
        one of its windows, *by name*). The default grammar is
        ``=session[:window]`` — historically tmux's exact-match syntax, now
        owned by the seam: every target-taking method below accepts both this
        token and the backend's native ids, and :func:`parse_target` is the
        matching decoder. Backends MAY override to emit native ids, but the
        result must stay a stable *by-name* reference: callers format targets
        ahead of use (e.g. a parked window's return target), so eager
        resolution to a live native id can go stale — keeping the token
        symbolic and resolving lazily at use time is the recommended default."""
        return f"={session}:{window}" if window else f"={session}"

    # ----------------------------------------------------------- sessions

    @abstractmethod
    def has_session(self, name: str) -> bool:
        """True iff a session named exactly ``name`` exists."""

    @abstractmethod
    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        """Create a detached session with a single shell window rooted at ``cwd``.
        When ``cols``/``lines`` are given the session is pinned to that geometry
        (agent sessions are observed detached, so their pane size must be fixed);
        omit both for a session whose size is irrelevant (e.g. the control session,
        which is only ever attached, and an attaching client resizes it anyway)."""

    @abstractmethod
    def kill_session(self, name: str) -> None:
        """Kill the named session (tolerant of it already being gone)."""

    @abstractmethod
    def list_sessions(self) -> list[str]:
        """Names of all live sessions."""

    @abstractmethod
    def session_options(self, option: str) -> dict[str, str]:
        """Map of session name -> value of ``option`` across all sessions."""

    @abstractmethod
    def set_session_option(self, name: str, option: str, value: str) -> None:
        """Set a user option on the named session."""

    def set_session_environment(self, name: str, var: str, value: str) -> None:
        """Add ``var=value`` to the named session's environment, inherited by
        every process the session spawns *afterwards* (tmux ``set-environment``).
        Used to hand a viewer window credentials (e.g. a server password)
        without putting them in the window's argv, where ``ps`` would show
        them. Not abstract — best-effort: the default no-op means a backend
        without the capability degrades to an unauthenticated viewer, never a
        crash."""

    # ------------------------------------------------------------ windows

    @abstractmethod
    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        """Create a window running ``command`` (with ``env`` layered on) in
        ``session``, rooted at ``cwd``. Returns the backend-native window id.

        ``command`` is a POSIX shlex-joined argv string, not a shell line:
        shell-operator behavior (``&&``, ``|``, ...) is backend-defined —
        one backend may hand the string to a shell, another may shlex
        re-split it into literal argv — so callers must not rely on it."""

    @abstractmethod
    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        """Create a window that runs ``argv`` then *parks* — waiting on a key so
        the exit status stays inspectable instead of the window closing the moment
        the process exits — and finally returns an attached client to its origin
        (keyed by the per-window ``return_opt``). Returns the native window id."""

    @abstractmethod
    def list_window_ids(self, session: str) -> list[str]:
        """Native ids of every window in ``session`` (empty if it is gone).

        Raises :class:`MultiplexerError` if the transport itself fails (timeout /
        missing binary): an empty list means "no windows" and must not be
        conflated with "couldn't ask" — this op backs the engine's liveness
        probe (:meth:`window_alive`)."""

    @abstractmethod
    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        """One tuple per window in ``session``, each holding the requested
        backend fields in order. Best-effort: returns ``[]`` on a transport
        failure (unlike :meth:`list_window_ids`, this is metadata, not a liveness
        probe, so a sentinel is safe)."""

    @abstractmethod
    def window_alive(self, session: str, window_id: str) -> bool:
        """True iff ``window_id`` is still a window of ``session``.

        May raise :class:`MultiplexerError` when liveness is unknowable (a
        transport timeout / missing binary) — callers must treat that as "don't
        know", not "dead", and must not tear down a possibly-working session on
        it."""

    def window_exists(self, session: str, window_name: str) -> bool:
        """True iff ``window_name`` is an active window of ``session``.

        Best-effort: returns ``False`` when the multiplexer is unavailable.
        Used by viewer creation to verify a newly-created window actually
        appeared (the ``new-window`` return value is not enough)."""
        return False

    @abstractmethod
    def kill_window(self, target: str) -> None:
        """Kill the targeted window (tolerant of it already being gone, and a
        no-op on a transport failure). ``target`` is a :meth:`target` token or
        a backend-native window id."""

    @abstractmethod
    def select_window(self, target: str) -> None:
        """Make ``target`` the current window of its session (best-effort: a no-op
        on a transport failure). ``target`` is a :meth:`target` token or a
        backend-native window id."""

    def resize_window(self, target: str, cols: int, lines: int) -> None:
        """Resize the targeted window to ``cols`` x ``lines`` (tmux
        ``resize-window``). Used by the dashboard to keep a detached viewer
        window matched to the pane that displays its capture, so the attached
        TUI redraws at the size actually shown. Not abstract — best-effort:
        the default no-op leaves the window at its creation geometry."""

    def set_viewer_option(self, window_id: str, value: str) -> None:
        """Stash a unique marker on the viewer window so later lookups
        (reuse, teardown, resume) find it even when auto-rename is active.

        ``window_id`` is the backend-native id returned by
        :meth:`TerminalMultiplexer.new_window`.  Best-effort: no-op when the
        multiplexer is unavailable."""

    @abstractmethod
    def set_window_option(self, target: str, option: str, value: str) -> None:
        """Set a user option on the targeted window (best-effort: a no-op on a
        transport failure). ``target`` is a :meth:`target` token or a
        backend-native window id."""

    def capture_pane(self, target: str, start_line: int = -100, end_line: int | None = None) -> str:
        """Capture the contents of a pane as a text string.

        Equivalent to ``tmux capture-pane -p -e -J -S <start> -E <end> -t <target>``.

        Args:
            target: pane/window session target (e.g. ``=bmad-loop-abc:viewer``).
            start_line: first line to capture (negative = lines above the
                visible pane; ``-100`` ≈ 100 lines of history).
            end_line: last line to capture (``None`` = current line).

        Returns the captured text (including escape sequences) or empty
        string when the pane is missing or the backend is unavailable.
        Callers parse the text through pyte to produce a styled render."""
        raise NotImplementedError

    @abstractmethod
    def unset_window_option(self, target: str, option: str) -> None:
        """Remove a user option from the targeted window (so a later read sees it
        as unset, not as an empty value). Best-effort: a no-op on a transport
        failure. ``target`` is a :meth:`target` token or a backend-native
        window id."""

    @abstractmethod
    def show_window_option(self, target: str, option: str) -> str:
        """Value of a user option on the targeted window ('' if unset, and '' on a
        transport failure). ``target`` is a :meth:`target` token or a
        backend-native window id."""

    @abstractmethod
    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        """Tee the window's pane output to ``log_file`` (tolerant of the window
        having already died)."""

    @abstractmethod
    def send_text(self, window_id: str, text: str) -> None:
        """Send ``text`` literally to the window, then submit it (Enter)."""

    # ----------------------------------------------------- client / attach

    @abstractmethod
    def attach_target_argv(self, target: str) -> list[str]:
        """argv that attaches the caller's terminal to ``target`` (a
        :meth:`target` token — session-only or session+window — or a
        backend-native id)."""

    @abstractmethod
    def current_pane_id(self) -> str | None:
        """Native id of the pane this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_window_id(self) -> str | None:
        """Native id of the window this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_session(self) -> str | None:
        """Name of the session this process runs in, or None when not inside the
        multiplexer."""

    def current_return_target(self) -> str | None:
        """Target an interactive attach records so the parked-window return
        trailer / :meth:`switch_client` can send the client back to the pane
        this process runs in; None when not inside the multiplexer. The value
        is backend-composed and replayed opaquely, so each backend emits
        whatever its own ``switch-client`` resolves best. Default: the native
        pane id — globally unique on a one-server multiplexer (tmux) and the
        pass-through form for native-id backends. A backend whose ids do not
        resolve from another session's context (e.g. psmux, one server per
        session) overrides this to emit a qualified form."""
        return self.current_pane_id() or None

    @abstractmethod
    def detach_client(self) -> None:
        """Detach the client viewing the current session (best-effort: a no-op on
        a transport failure)."""

    @abstractmethod
    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        """Switch the current client to ``target`` (optionally falling back to
        the last client on failure). Returns True iff a switch happened — so a
        transport failure returns False. ``target`` is a :meth:`target` token
        or a backend-native id."""

    @abstractmethod
    def available(self) -> bool:
        """True iff this backend can run on the current host (e.g. its binary is
        on PATH)."""

    def version(self) -> str | None:
        """The backend binary's version string, or None when unavailable. Not
        abstract: backends that can't report one inherit this default. Used by
        the diagnostic dump; the implementation owns the binary invocation so it
        stays behind the seam."""
        return None

    def window_pane_pids(self, target: str) -> list[int]:
        """Best-effort OS pids of ``target``'s pane root processes, for the kill
        escalation. Not abstract: backends that can't (or don't) report pids
        inherit this default. ``[]`` means unknown or capability not offered —
        callers must degrade (skip the pid-level escalation) and never read
        ``[]`` as "no processes". Must not raise."""
        return []


# (name, matches(platform) -> bool, factory() -> TerminalMultiplexer)
_BACKENDS: list[tuple[str, Callable[[str], bool], Callable[[], TerminalMultiplexer]]] = []
_BUILTINS_LOADED = False
# The policy [mux] backend choice — (name, origin policy path) — installed once
# per CLI invocation by cli._configure_mux via configure_multiplexer. None = auto.
_CONFIGURED: tuple[str, Path | None] | None = None

# Per-platform default backend name, consulted only when that backend is both
# registered AND available on this host. psmux is a bundled builtin (registered
# below), so on a win32 host it applies whenever psmux reports available; if it
# isn't, selection falls through to the first platform match / fallback.
_PLATFORM_DEFAULTS: dict[str, str] = {"win32": "psmux"}
_DEFAULT_BACKEND = "tmux"  # every platform not listed above


def register_multiplexer(
    name: str,
    matches: Callable[[str], bool],
    factory: Callable[[], TerminalMultiplexer],
) -> None:
    """Register a transport backend. ``matches(sys.platform)`` decides automatic
    selection; ``name`` is the key for the ``BMAD_LOOP_MUX_BACKEND`` override.
    Bundled backends register from :func:`_load_builtin_backends`; an out-of-tree
    backend calls this at import time — no core edit required."""
    _BACKENDS.append((name, matches, factory))
    get_multiplexer.cache_clear()  # a later registration must not be shadowed by a cached pick


def _load_builtin_backends() -> None:
    """Register the bundled backends — tmux (POSIX) and psmux (native Windows);
    every other backend is out-of-tree and arrives via
    :func:`_load_external_backends` or a manual import. Idempotent and lazy
    (called from :func:`get_multiplexer`, not at
    module import) to stay cycle-safe. Registers inline rather than via
    tmux_backend's import side effect so the registry can be cleared and
    re-loaded deterministically (a re-import is a no-op once cached) —
    mirroring ``process_host._load_builtin_hosts``."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from .psmux_backend import PsmuxMultiplexer
    from .tmux_backend import TmuxMultiplexer

    # tmux is the default everywhere except native Windows (no tmux binary there);
    # get_multiplexer still falls back to tmux when no backend matches. Builtins
    # register before externals, so tmux keeps first-wins on any name collision.
    register_multiplexer("tmux", lambda platform: platform != "win32", TmuxMultiplexer)
    # psmux speaks the tmux CLI through its own distinctly-named binary, so
    # native Windows gets the tmux-family backend with a PowerShell dialect.
    register_multiplexer("psmux", lambda platform: platform == "win32", PsmuxMultiplexer)
    _BUILTINS_LOADED = True  # set only after a successful import so a transient failure retries


# The entry-point group an out-of-tree backend package advertises its module
# under; importing the module runs its register_multiplexer call. Loader state:
# scanned-once flag + per-entry-point failure reasons for mux/validate to show.
MUX_BACKENDS_GROUP = "bmad_loop.mux_backends"
_EXTERNALS_LOADED = False
_EXTERNAL_ERRORS: dict[str, str] = {}


def _load_external_backends() -> None:
    """Import every ``bmad_loop.mux_backends`` entry point; each module
    self-registers via :func:`register_multiplexer` at import time. Called after
    :func:`_load_builtin_backends`, so builtins keep first registration (tmux
    stays first-wins on a name collision) and selection precedence is unchanged.

    A broken third-party distribution must never break backend selection:
    failures are recorded in ``_EXTERNAL_ERRORS`` (surfaced by ``bmad-loop mux``
    and the ``validate`` preflight via :func:`external_backend_errors`), not
    raised. Unlike ``_BUILTINS_LOADED``, the loaded-flag is set up front: a
    third-party import failure is not transient, and retrying on every
    selection would re-import (and re-fail) each time."""
    global _EXTERNALS_LOADED
    if _EXTERNALS_LOADED:
        return
    _EXTERNALS_LOADED = True
    try:
        eps = importlib.metadata.entry_points(group=MUX_BACKENDS_GROUP)
    except Exception as exc:  # diagnostics path, never crash selection
        _EXTERNAL_ERRORS["<entry-point scan>"] = f"{type(exc).__name__}: {exc}"
        return
    for ep in eps:
        try:
            ep.load()  # module import runs register_multiplexer(...)
        except Exception as exc:  # one bad package must not hide the rest
            _EXTERNAL_ERRORS[ep.name] = f"{type(exc).__name__}: {exc}"


def external_backend_errors() -> dict[str, str]:
    """Entry-point name -> failure reason for every external backend that failed
    to load this process (empty when all loaded). For diagnostics surfaces."""
    return dict(_EXTERNAL_ERRORS)


def configure_multiplexer(name: str | None, *, origin: Path | None = None) -> None:
    """Install the policy ``[mux] backend`` choice (``None``/``""`` = auto).

    Called once per CLI invocation (``cli.main``, after parsing ``--project``)
    before any :func:`get_multiplexer` consumer runs, so probe/diagnose/attach —
    which never load policy themselves — select under the persisted choice too.
    Idempotent: the selection cache is cleared only when the effective value
    changes, so the process-wide singleton identity survives repeated
    same-value configuration."""
    global _CONFIGURED
    new = (name, origin) if name else None
    if new == _CONFIGURED:
        return
    _CONFIGURED = new
    get_multiplexer.cache_clear()


def _known() -> str:
    return ", ".join(name for name, _, _ in _BACKENDS) or "(none registered)"


def _factory_by_name(name: str) -> Callable[[], TerminalMultiplexer] | None:
    for reg_name, _, factory in _BACKENDS:
        if reg_name == name:  # duplicate registrations: first wins, as in the loop below
            return factory
    return None


def _usable(backend: TerminalMultiplexer) -> bool:
    """``available()`` read through a guard: selection must never crash on a
    backend's host probe, so a missing or raising probe reads as unavailable."""
    try:
        return bool(backend.available())
    except Exception:
        return False


def backend_forced() -> bool:
    """True when selection is pinned by the env var or the policy choice.

    A forced name bypasses ``available()`` throughout (an explicit choice is
    trusted; the backend fails loudly if it can't run), so launch preflights
    that refuse an unusable backend must stand down for it too — via
    :func:`mux_usable`, which stands down loudly."""
    return bool(envvars.mux_backend()) or _CONFIGURED is not None


_FORCED_UNUSABLE_WARNED = False


def mux_usable(backend: TerminalMultiplexer | None = None) -> bool:
    """The one usability gate for launch preflights and TUI observers
    (attach, liveness, prune): the backend probes available, or its selection
    is forced. Every gate must share this rule — if launch trusts a forced
    backend but observers don't, a launched run becomes invisible to the rest
    of the TUI with no error anywhere.

    A forced backend that probes unavailable is still trusted, but says so
    once per process on stderr: a missing binary fails loudly on first use
    anyway, while a version-gated binary works right up until the gated defect
    fires — proceeding must not be silent."""
    global _FORCED_UNUSABLE_WARNED
    if backend is None:
        backend = get_multiplexer()
    if _usable(backend):
        return True
    if not backend_forced():
        return False
    if not _FORCED_UNUSABLE_WARNED:
        _FORCED_UNUSABLE_WARNED = True
        try:
            version = backend.version()
        except Exception:  # a broken probe must not break the warning
            version = None
        print(
            f"warning: forced multiplexer backend {type(backend).__name__} reports "
            f"unavailable (version: {version!r}); proceeding because the choice is "
            "pinned — a version-gated backend can misbehave mid-run",
            file=sys.stderr,
        )
    return True


def _select() -> tuple[TerminalMultiplexer, str, str]:
    """Resolve the backend by precedence; returns ``(instance, name, reason)``.

    1. ``env`` — ``BMAD_LOOP_MUX_BACKEND`` forces a backend by name
    2. ``policy`` — the ``[mux] backend`` choice installed by
       :func:`configure_multiplexer`, same forced-by-name semantics
    3. ``platform-default`` — this platform's default, iff registered +
       platform match + available
    4. ``first-match`` — first registered backend matching the platform that is
       available (registration order breaks ties among available backends)
    5. ``fallback`` — the historical behavior, preserved so a POSIX host without
       tmux still returns TmuxMultiplexer and ``validate`` reports it
       unavailable: first platform match regardless of availability, then tmux

    A forced name (1-2) bypasses both the platform predicate and ``available()``
    — an explicit choice is trusted, and the backend itself fails loudly if it
    can't run. A forced name matching nothing is a misconfiguration; never
    silently fall back to tmux (wrong/unsafe on a non-POSIX host)."""
    _load_builtin_backends()
    _load_external_backends()
    forced = envvars.mux_backend()
    if forced:
        factory = _factory_by_name(forced)
        if factory is None:
            raise MultiplexerError(
                f"BMAD_LOOP_MUX_BACKEND={forced!r} matches no registered backend; known: {_known()}"
            )
        return factory(), forced, "env"
    if _CONFIGURED is not None:
        name, origin = _CONFIGURED
        factory = _factory_by_name(name)
        if factory is None:
            where = f"[mux] backend = {name!r}" + (f" in {origin}" if origin else "")
            raise MultiplexerError(f"{where} matches no registered backend; known: {_known()}")
        return factory(), name, "policy"

    # Construct each candidate at most once across the remaining steps.
    instances: dict[str, TerminalMultiplexer] = {}

    def _instance(name: str, factory: Callable[[], TerminalMultiplexer]) -> TerminalMultiplexer:
        if name not in instances:
            instances[name] = factory()
        return instances[name]

    default = _PLATFORM_DEFAULTS.get(sys.platform, _DEFAULT_BACKEND)
    for name, matches, factory in _BACKENDS:
        if name != default:
            continue
        # first registration with the default name wins, as everywhere else;
        # it must also claim this platform — a name-colliding backend for
        # another platform doesn't get defaulted onto this one.
        if matches(sys.platform):
            backend = _instance(name, factory)
            if _usable(backend):
                return backend, name, "platform-default"
        break
    for name, matches, factory in _BACKENDS:
        if matches(sys.platform) and _usable(_instance(name, factory)):
            return instances[name], name, "first-match"
    for name, matches, factory in _BACKENDS:
        if matches(sys.platform):
            return _instance(name, factory), name, "fallback"
    from .tmux_backend import TmuxMultiplexer  # bottom fallback, as before

    return TmuxMultiplexer(), "tmux", "fallback"


@functools.lru_cache(maxsize=1)
def get_multiplexer() -> TerminalMultiplexer:
    """Return the process-wide terminal multiplexer, selected by registry.

    Selection precedence lives in :func:`_select` (env var, policy choice,
    platform default, first available match, historical fallback). Cached —
    tests that flip the env var must call ``get_multiplexer.cache_clear()``;
    :func:`register_multiplexer` and :func:`configure_multiplexer` clear it
    themselves."""
    return _select()[0]


@dataclass(frozen=True)
class MuxBackendInfo:
    """One registered backend's detection row, for ``bmad-loop mux`` and the
    ``validate`` preflight."""

    name: str
    matches_platform: bool
    available: bool
    version: str | None
    selected: bool
    reason: str  # "" unless selected: env | policy | platform-default | first-match | fallback


def detect_multiplexers() -> list[MuxBackendInfo]:
    """Probe every registered backend: availability, version, platform match,
    and which one :func:`_select` would pick (with its reason).

    Never raises — this feeds diagnostics, which must work on a misconfigured
    host: a forced unknown name yields rows with no selected mark, and a
    backend whose factory or probes blow up reads as unavailable. Constructs
    every registered backend, so factories must stay cheap, side-effect-free
    constructors (true of the tmux family)."""
    _load_builtin_backends()
    _load_external_backends()
    try:
        _, selected_name, reason = _select()
    except MultiplexerError:
        selected_name, reason = None, ""
    rows: list[MuxBackendInfo] = []
    seen: set[str] = set()
    for name, matches, factory in _BACKENDS:
        if name in seen:  # duplicate registrations: only the selectable (first) one is shown
            continue
        seen.add(name)
        try:
            matches_platform = bool(matches(sys.platform))
        except Exception:
            matches_platform = False
        version: str | None = None
        try:
            backend = factory()
            available = _usable(backend)
        except Exception:
            available = False
        else:
            # version() is cosmetic: its failure must not overwrite the
            # already-computed availability (a selected backend would
            # otherwise show a contradictory available=False row).
            try:
                version = backend.version()
            except Exception:
                version = None
        selected = name == selected_name
        rows.append(
            MuxBackendInfo(
                name=name,
                matches_platform=matches_platform,
                available=available,
                version=version,
                selected=selected,
                reason=reason if selected else "",
            )
        )
    return rows
