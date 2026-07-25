"""Generic multiplexer-based viewer window launch.

Create, reuse, and teardown detached tmux windows that run an attached
viewer (e.g. ``opencode attach``).  The caller (an adapter) provides the
argv to launch the viewer; the mux backend creates a new or reuses an
existing window inside the run's tmux session.

Idempotency: a viewer already exists for the given session name + window
name (``<session_name>-viewer``, ``bmad-loop-<run_id>-viewer``), the
existing window is returned — no duplicate windows across retries,
review sessions, interrupted runs, or resume.

Teardown priority: stop/kill viewer window first, then close the
underlying server/session.
"""

from __future__ import annotations

import re
import shlex
import shutil
import time
from pathlib import Path

from .base import ViewerConfig
from .multiplexer import PARKED_EXIT_BANNER, MultiplexerError, get_multiplexer, mux_usable

# ------------------------------------------------------------------ config
# The tmux user option stamped on the viewer window so we can find it
# again (retries, resume, attach routing).  Never empty — the parser
# rejects it, so callers use this sentinel to distinguish "unset" from
# an actual option value.
_VIEWER_OPTION = "@bmad_viewer"

# Per-window option the parked-window trailer consults for a return client
# target. Never set for a viewer (nobody attach-routes *from* it), so the
# window simply parks after the viewer exits — keeping its final screen and
# exit banner capturable instead of the window closing.
_VIEWER_RETURN_OPTION = "@bmad_viewer_return"

# Pulls the exit status out of the parked banner ("[bmad-loop exited 7 — ...").
_EXIT_BANNER_RE = re.compile(re.escape(PARKED_EXIT_BANNER) + r"\s+(\d+)")

# Immediate-exit detection: after new-window, the pane is re-checked this
# many times at this interval. A viewer that dies inside the grace window
# (bad URL, refused attach, missing session) is reported as failed instead
# of being handed to the dashboard as a live target.
_STARTUP_CHECKS = 4
_STARTUP_CHECK_INTERVAL_S = 0.25

# ---------------------------------------------------------------------- core


def _viewer_window_name(run_id: str) -> str:
    """Seam-canonical name for the viewer window inside the control session."""
    return f"bmad-loop-{run_id}-viewer"


def _find_existing_viewer(run_id: str, session_name: str) -> ViewerConfig | None:
    """Check whether a viewer window already exists for this run.

    Reads the marker option as a best-effort existence probe: if the
    window is present and carries the value stamped at creation time,
    we assume it is live and reuse it.
    """
    mux = get_multiplexer()
    if not mux_usable(mux):
        return None
    viewer_name = _viewer_window_name(run_id)
    # A window's user option is the authoritative existence proof;
    # a bare window name match is fragile under auto-rename.
    try:
        rows = mux.list_windows(session_name, ["window_name", _VIEWER_OPTION])
    except MultiplexerError:
        return None
    for row in rows:
        if row and row[0] == viewer_name and len(row) > 1 and row[1]:
            # Window carries our marker — it may be parked or still
            # streaming; treat as live. The persisted target is the
            # by-name token, never the marker value.
            return ViewerConfig(viewer_window=mux.target(session_name, viewer_name))
    return None


def ensure_viewer_window(
    run_id: str,
    server_url: str,
    session_id: str,
    working_directory: Path,
    attach_binary: str,
    run_dir: Path | None = None,
    attach_env: dict[str, str] | None = None,
) -> ViewerConfig:
    """Create (or reuse) a detached viewer window.

    ``server_url`` — the headless HTTPS or HTTP URL the viewer should
    ``attach`` to (e.g. ``http://127.0.0.1:port``).

    ``session_id`` — the session the viewer will connect to (e.g. an
    OpenCode session id).

    ``working_directory`` — ``--dir`` for the attached TUI.

    ``attach_binary`` — the binary that runs the attached CLI
    (e.g. ``opencode``; argv will be ``{binary} attach <url> --dir <cwd>``).

    ``run_dir`` — where ``viewer-error.log`` and the ``viewer-create-failed``
    journal entry land on failure; falls back to ``working_directory``.

    ``attach_env`` — credentials/config the attached TUI needs (e.g.
    ``OPENCODE_SERVER_PASSWORD``), delivered via the tmux *session*
    environment so they never appear in the window's argv (``ps``-visible)
    or in the persisted ``viewer_command``.

    Returns a ``ViewerConfig`` with the mux-native window target so
    ``kill_session`` can tear it down.  Returns a ``ViewerConfig()``
    (all empty) when tmux is unavailable, the viewer binary is not on
    PATH, or the viewer window could not be verified live — the caller
    degrades to headless.  Never persists a live-looking config for a
    window that does not exist.
    """
    mux = get_multiplexer()
    report_dir = run_dir or working_directory

    session_name = f"bmad-loop-{run_id}"
    viewer_name = _viewer_window_name(run_id)
    viewer_argv = _viewer_argv(attach_binary, server_url, working_directory, session_id)
    viewer_cmd = " ".join(shlex.quote(a) for a in viewer_argv)
    viewer_target = mux.target(session_name, viewer_name)

    if not mux_usable(mux):
        _report_viewer_failure(
            report_dir, viewer_cmd, viewer_target, "terminal multiplexer unavailable"
        )
        return ViewerConfig()

    def _success() -> ViewerConfig:
        return ViewerConfig(
            viewer_window=viewer_target,
            viewer_url=server_url,
            viewer_session=session_id,
            viewer_cwd=str(working_directory),
            viewer_command=viewer_cmd,
        )

    # Idempotency: check for existing viewer window BEFORE trying to create.
    if _find_existing_viewer(run_id, session_name) is not None:
        return _success()

    # Check that the attach binary is available before creating the window.
    if not _binary_exists(attach_binary):
        _report_viewer_failure(
            report_dir,
            viewer_cmd,
            viewer_target,
            f"attach binary not found on PATH: {attach_binary!r}",
        )
        return ViewerConfig()

    try:
        if not mux.has_session(session_name):
            # Pin the same geometry the agent panes use: the session is only
            # ever observed detached (capture-pane -> pyte at this size), and
            # tmux's default 80x24 would cramp the attached TUI's layout.
            from .generic import PANE_COLUMNS, PANE_LINES

            mux.new_session(session_name, working_directory, PANE_COLUMNS, PANE_LINES)
        for var, value in (attach_env or {}).items():
            mux.set_session_environment(session_name, var, value)
        # Parked window: runs the viewer, and if (when) the viewer exits the
        # pane prints the exit banner and parks instead of the window closing
        # — so an immediately-dying viewer stays inspectable, race-free, and
        # a mid-run crash leaves its final screen visible in the dashboard.
        mux.new_parked_window(
            session_name,
            viewer_name,
            working_directory,
            viewer_argv,
            _VIEWER_RETURN_OPTION,
        )
    except MultiplexerError as exc:
        _report_viewer_failure(report_dir, viewer_cmd, viewer_target, str(exc))
        return ViewerConfig()

    # Anything that goes wrong past this point must not leak an orphaned
    # window that no ViewerConfig records — kill it and report instead.
    try:
        # Stamp a marker so _find_existing_viewer can locate us across retries.
        mux.set_viewer_option(viewer_target, _VIEWER_OPTION)

        # Verify the window is actually visible and the viewer stays up —
        # a new-window return value is not enough (a racing kill can lose
        # the window, and a viewer that can't attach exits within moments,
        # which the parked recipe surfaces as the exit banner).
        for check in range(_STARTUP_CHECKS):
            if not _mux_window_exists(mux, session_name, viewer_name):
                _report_viewer_failure(
                    report_dir,
                    viewer_cmd,
                    viewer_target,
                    "window not found after new-window",
                )
                return ViewerConfig()
            try:
                output = mux.capture_pane(viewer_target)
            except (MultiplexerError, NotImplementedError, OSError):
                output = ""  # a capture-less backend still gets a viewer
            if PARKED_EXIT_BANNER in output:
                match = _EXIT_BANNER_RE.search(output)
                status = match.group(1) if match else "unknown"
                _report_viewer_failure(
                    report_dir,
                    viewer_cmd,
                    viewer_target,
                    f"viewer exited immediately (exit status {status})",
                    output=output,
                )
                try:
                    mux.kill_window(viewer_target)
                except Exception:
                    pass
                return ViewerConfig()
            if check < _STARTUP_CHECKS - 1:
                time.sleep(_STARTUP_CHECK_INTERVAL_S)
    except BaseException as exc:  # never hand back (or orphan) an unverified window
        try:
            mux.kill_window(viewer_target)
        except Exception:
            pass
        if not isinstance(exc, Exception) or _is_control_flow(exc):
            # KeyboardInterrupt/SystemExit or the engine's RunStopped/RunPaused
            # landing mid-poll (the SIGTERM handler raises in whatever frame is
            # running): clean up and LET IT PROPAGATE — swallowing it here
            # permanently breaks the engine's stop path (the handler only
            # raises once).
            raise
        _report_viewer_failure(
            report_dir,
            viewer_cmd,
            viewer_target,
            f"viewer verification failed: {type(exc).__name__}: {exc}",
        )
        return ViewerConfig()

    return _success()


def _is_control_flow(exc: Exception) -> bool:
    """Whether ``exc`` is one of the engine's loop-unwinding exceptions.

    Imported lazily: the engine imports the adapters package at module load,
    so a top-level import here would be a cycle."""
    try:
        from ..engine import RunPaused, RunStopped
    except Exception:  # circular-import corner or stripped install: assume not
        return False
    return isinstance(exc, (RunPaused, RunStopped))


def _mux_window_exists(mux, session_name: str, window_name: str) -> bool:
    """Return True if ``window_name`` is live in ``session_name``."""
    try:
        rows = mux.list_windows(session_name, ["window_name"])
    except MultiplexerError:
        return False
    return any(row and row[0] == window_name for row in rows)


def close_viewer_window(target: str | None) -> None:
    """Kill the viewer window by its mux-specific target token.

    Idempotent: ``kill_window`` already tolerates the window being gone.
    """
    if not target:
        return
    mux = get_multiplexer()
    try:
        mux.kill_window(target)
    except MultiplexerError:
        pass


def viewer_window_exists(run_id: str, session_name: str) -> bool:
    """Whether a viewer window is currently attached to the run's session."""
    mux = get_multiplexer()
    if not mux_usable(mux):
        return False
    viewer_name = _viewer_window_name(run_id)
    try:
        rows = mux.list_windows(session_name, ["window_name"])
    except MultiplexerError:
        return False
    return any(row and row[0] == viewer_name for row in rows)


# ------------------------------------------------------------------ helpers


def _viewer_argv(
    binary: str,
    server_url: str,
    cwd: Path,
    session_id: str,
) -> list[str]:
    """Build the ``opencode attach``-style argv for a viewer window."""
    return [
        binary,
        "attach",
        server_url,
        "--dir",
        str(cwd),
        "--session",
        session_id,
    ]


def _binary_exists(name: str) -> bool:
    """Best-effort check whether ``name`` is on PATH."""
    return shutil.which(name) is not None


def _report_viewer_failure(
    report_dir: Path, command: str, target: str, reason: str, output: str = ""
) -> None:
    """Append to ``viewer-error.log`` in ``report_dir`` and journal a
    ``viewer-create-failed`` event (best-effort either way).

    The journal entry is only written when ``report_dir`` already carries a
    ``journal.jsonl`` (i.e. it is a run dir) — reporting into a plain
    working directory must not seed a stray journal there."""
    try:
        log_path = report_dir / "viewer-error.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"viewer-create-failed: {reason}\n")
            fh.write(f"command: {command}\n")
            fh.write(f"target: {target}\n")
            fh.write(f"timestamp: {time.time()}\n")
            if output:
                fh.write("output:\n")
                fh.write(output.rstrip("\n") + "\n")
            fh.write("---\n")
    except OSError:
        pass

    try:
        from ..journal import JOURNAL_FILE, Journal

        if (report_dir / JOURNAL_FILE).is_file():
            Journal(report_dir).append(
                "viewer-create-failed",
                reason=reason,
                command=command,
                target=target,
            )
    except Exception:  # journaling must never crash the viewer path
        pass
