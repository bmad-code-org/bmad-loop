"""Terminal-multiplexer seam.

The coding-CLI adapter (:class:`~.base.CodingCLIAdapter`) abstracts *which CLI*
to drive and how its prompts/hooks work. This module abstracts the orthogonal
**transport** axis: how sessions, windows, and panes are created, observed, and
torn down. Today the only backend is tmux (:class:`~.tmux_backend.TmuxMultiplexer`);
the seam exists so a future non-POSIX backend (an eventual native-Windows "psmux")
can slot in without the rest of the codebase shelling out to ``tmux`` directly.

``TerminalMultiplexer`` is the contract a backend author implements. Operation
names mirror today's call sites verbatim so the migration is mechanical. Backends
register themselves through :func:`register_multiplexer` (bundled ones from
:func:`_load_builtin_backends`, out-of-tree ones at import time); the process-wide
backend is selected by registry — by platform, or forced by name through the
``BMAD_LOOP_MUX_BACKEND`` env var — and returned by :func:`get_multiplexer`.
"""

from __future__ import annotations

import functools
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class MultiplexerError(Exception):
    """A transport-backend operation failed. Backends raise a subclass (e.g.
    :class:`~.tmux_backend.TmuxError`) so call sites can catch the seam-level type
    without importing a backend."""


class TerminalMultiplexer(ABC):
    """Transport backend for agent sessions: sessions, windows, and clients.

    A backend must shell out to (or otherwise drive) exactly one multiplexer and
    nothing else — it is the single place POSIX-shell / tmux knowledge is allowed
    to live. The full surface below is the contract; Phase 1 wired only the subset
    the generic adapter needs, and Phase 2 fills in the rest as ``runs.py``,
    ``tui/launch.py``, ``probe.py``, and ``tui/data.py`` migrate onto it.
    """

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

    @abstractmethod
    def kill_window(self, target: str) -> None:
        """Kill the targeted window (tolerant of it already being gone, and a
        no-op on a transport failure)."""

    @abstractmethod
    def select_window(self, target: str) -> None:
        """Make ``target`` the current window of its session (best-effort: a no-op
        on a transport failure)."""

    @abstractmethod
    def set_window_option(self, target: str, option: str, value: str) -> None:
        """Set a user option on the targeted window (best-effort: a no-op on a
        transport failure)."""

    @abstractmethod
    def unset_window_option(self, target: str, option: str) -> None:
        """Remove a user option from the targeted window (so a later read sees it
        as unset, not as an empty value). Best-effort: a no-op on a transport
        failure."""

    @abstractmethod
    def show_window_option(self, target: str, option: str) -> str:
        """Value of a user option on the targeted window ('' if unset, and '' on a
        transport failure)."""

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
        """argv that attaches the caller's terminal to ``target``."""

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

    @abstractmethod
    def detach_client(self) -> None:
        """Detach the client viewing the current session (best-effort: a no-op on
        a transport failure)."""

    @abstractmethod
    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        """Switch the current client to ``target`` (optionally falling back to
        the last client on failure). Returns True iff a switch happened — so a
        transport failure returns False."""

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


# (name, matches(platform) -> bool, factory() -> TerminalMultiplexer)
_BACKENDS: list[tuple[str, Callable[[str], bool], Callable[[], TerminalMultiplexer]]] = []
_BUILTINS_LOADED = False


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
    """Register the bundled backends. Idempotent and lazy (called from
    :func:`get_multiplexer`, not at module import) to stay cycle-safe. Registers
    inline rather than via tmux_backend's import side effect so the registry can be
    cleared and re-loaded deterministically (a re-import is a no-op once cached) —
    mirroring ``process_host._load_builtin_hosts``."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from .tmux_backend import TmuxMultiplexer

    # tmux is the default everywhere except native Windows (no tmux binary there);
    # get_multiplexer still falls back to tmux when no backend matches.
    register_multiplexer("tmux", lambda platform: platform != "win32", TmuxMultiplexer)
    _BUILTINS_LOADED = True  # set only after a successful import so a transient failure retries


@functools.lru_cache(maxsize=1)
def get_multiplexer() -> TerminalMultiplexer:
    """Return the process-wide terminal multiplexer, selected by registry.

    ``BMAD_LOOP_MUX_BACKEND`` forces a backend by name (test / override hook);
    otherwise the first backend whose ``matches(sys.platform)`` is true wins. tmux
    is the default fallback, so POSIX behavior is unchanged. Cached — tests that
    flip the env var must call ``get_multiplexer.cache_clear()``."""
    forced = os.environ.get("BMAD_LOOP_MUX_BACKEND")
    _load_builtin_backends()
    for name, matches, factory in _BACKENDS:
        if name == forced or (not forced and matches(sys.platform)):
            return factory()
    if forced:
        # An explicit override that matches nothing is a misconfiguration; never
        # silently fall back to tmux (wrong/unsafe on a non-POSIX host).
        known = ", ".join(name for name, _, _ in _BACKENDS) or "(none registered)"
        raise MultiplexerError(
            f"BMAD_LOOP_MUX_BACKEND={forced!r} matches no registered backend; known: {known}"
        )
    from .tmux_backend import TmuxMultiplexer  # default fallback

    return TmuxMultiplexer()
