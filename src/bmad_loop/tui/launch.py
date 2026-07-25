"""Detached launching of bmad-loop commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated tmux control session (bmad-loop-ctl) so they survive
TUI exit, and the dashboard observes them through run-dir artifacts exactly
like runs started from a plain shell. Fast read-only commands (validate,
--dry-run) are captured instead, for display in a modal.

No textual imports here — everything drives the multiplexer seam (or a plain
subprocess for the captured read-only commands) and is unit-testable.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .. import runs
from ..adapters.multiplexer import MultiplexerError, get_multiplexer, mux_usable
from ..journal import Journal

CTL_SESSION = "bmad-loop-ctl"

# control-session windows are named <kind>-<run_id> (see start_detached)
_CTL_WINDOW_RE = re.compile(r"^(?:run|sweep|resume|resolve)-(.+)$")


def _find_view_window(run_id: str) -> str | None:
    """Find a viewer window for this run's session (set by opencode-http
    adapters when show_attached_ui is enabled).  Returns the mux target
    token, or None."""
    if not mux_available():
        return None
    session_name = runs.session_name(run_id)
    viewer_name = f"bmad-loop-{run_id}-viewer"
    mux = get_multiplexer()
    try:
        rows = mux.list_windows(session_name, ["window_name"])
    except MultiplexerError:
        return None
    for (wn,) in rows:
        if wn == viewer_name:
            return mux.target(session_name, wn)
    return None


class LaunchError(Exception):
    pass


def mux_available() -> bool:
    # Forced-aware (mux_usable, not raw available()): a pinned backend must look
    # the same to observers (attach, ctl-window lookup, prune) as it does to the
    # launch preflight, or a launched run becomes invisible to the rest of the TUI.
    return mux_usable(get_multiplexer())


def session_exists(session: str) -> bool:
    return get_multiplexer().has_session(session)


def ctl_window(run_id: str) -> str | None:
    """Name of the control-session window hosting this run's orchestrator
    process (start_detached names windows <kind>-<run_id>), or None when the
    run was not launched from the TUI or the session is gone."""
    if not mux_available():
        return None
    for (name,) in get_multiplexer().list_windows(CTL_SESSION, ["window_name"]):
        if name.endswith(f"-{run_id}"):
            return name
    return None


def ctl_target(window: str | None = None) -> str:
    """Seam-canonical target token for the control session (optionally one of
    its windows, by name); see :meth:`TerminalMultiplexer.target`."""
    return get_multiplexer().target(CTL_SESSION, window)


def select_ctl_window(window: str) -> None:
    """Make `window` the control session's current window, so a plain attach
    to the session lands on it (attach-session itself takes no window)."""
    get_multiplexer().select_window(ctl_target(window))


def select_ctl_window_id(window_id: str) -> None:
    """Like select_ctl_window but by stable tmux window id (@N). Immune to the
    by-name first-match ambiguity in ctl_window and to tmux auto-rename."""
    get_multiplexer().select_window(window_id)


# Per-window tmux user option recording what an interactive attach should do
# with the client once the window's command exits (consumed by the multiplexer's
# parked-window return trailer; see start_detached and the tmux backend). Set by
# set_return_pane at attach time. Value is either a backend-composed pane
# target — replayed opaquely, so each backend records the form its own
# switch-client resolves: a bare pane id (%N) on tmux, =session:%N on psmux,
# whose one-server-per-session model cannot resolve a bare id from the control
# session (psmux/psmux#483) — used when the TUI runs inside the multiplexer and
# switched its own client over; or RETURN_DETACH, used when the TUI runs
# outside and a throwaway client was attached that must detach so the
# suspended TUI resumes.
RETURN_OPTION = "@bmad_return_pane"
RETURN_DETACH = "detach"  # pane targets are %N / =sess:%N, never "detach"


def current_return_target() -> str | None:
    """Backend-composed target of the pane this process runs in — the place an
    attach should return the client to — or None when not inside the
    multiplexer / it is unavailable. The value is opaque to callers: record it
    with set_return_pane, replay it via switch_client / the parked trailer.
    See TerminalMultiplexer.current_return_target for the composition
    contract."""
    return get_multiplexer().current_return_target()


def set_return_pane(window_target: str, target: str) -> None:
    """Record `target` (a current_return_target value or RETURN_DETACH) as the
    return move on a control-session window, so its trailing shell sends the
    client back there when the window's command exits. `window_target` is any
    tmux window spec (e.g. `=bmad-loop-ctl:run-…` or a stable `@N` id)."""
    get_multiplexer().set_window_option(window_target, RETURN_OPTION, target)


def current_session() -> str | None:
    """Name of the tmux session this process is running inside, or None when
    not in tmux / tmux is unavailable."""
    return get_multiplexer().current_session()


def in_ctl_session() -> bool:
    """True when we are running inside a control-session window (i.e. launched
    detached by the TUI), as opposed to a user's own shell. Backend-honest:
    current_session() is None whenever this process is not inside the selected
    multiplexer, so no direct TMUX/HERDR_* env sniffing happens here."""
    return current_session() == CTL_SESSION


def detach_client() -> None:
    """Detach the tmux client viewing the current session, handing the terminal
    back to the user. Processes in the session keep running."""
    get_multiplexer().detach_client()


def return_attached_client() -> bool:
    """Hand an attached client back to its origin *now*, mid-process — the
    parked-window return move (see start_detached) executed while the window's
    command keeps running in the background, instead of after it exits.

    Reads the RETURN_OPTION recorded on the current window by set_return_pane:
      - a pane target (backend-composed: bare %N on tmux, =session:%N on
        psmux): switch that client back there (`-l` fallback if it's gone);
      - RETURN_DETACH: detach the client so a blocking `tmux attach` returns;
      - unset/empty: nobody attached with a return target — do nothing.
    The option is then cleared so the post-exit return trailer doesn't fire a
    second time. Returns True iff a client was actually returned."""
    mux = get_multiplexer()
    if not mux_usable(mux):
        return False
    win = mux.current_window_id()
    if win is None:
        return False
    ret = mux.show_window_option(win, RETURN_OPTION)
    if not ret:
        return False
    if ret == RETURN_DETACH:
        mux.detach_client()
    else:
        mux.switch_client(ret, last_fallback=True)
    mux.unset_window_option(win, RETURN_OPTION)
    return True


def decision_pending(run_dir: Path) -> bool:
    """True when the run's sweep is currently blocked on an interactive decision
    — its journal's last entry is a decision-pending announcement (the prompter
    blocks on input right after writing it, so any later entry means it moved
    on). Mirrors tui.data.pending_decision; kept here so the CLI can decide an
    attach target without importing the textual-laden data module."""
    entries = Journal(run_dir).entries()
    return bool(entries) and entries[-1].get("kind") == "decision-pending"


def attach_plan(project: Path, run_id: str) -> tuple[list[str], str | None] | None:
    """Pick where an interactive attach should land for this run and which window
    (if any) to record a return target on. Shared by the CLI `attach` command and
    mirroring the TUI's action_attach logic: prefer the viewer window (when one
    exists for an HTTP adapter), then the orchestrator's ctl window when a sweep
    is blocked on a decision or no agent session is live, else the live agent
    session. Returns (tmux argv, return_window) or None when there is nothing
    to attach to."""
    session = runs.session_name(run_id)
    viewer_window = _find_view_window(run_id)
    if viewer_window:
        return runs.attach_target_argv(viewer_window), None
    window = ctl_window(run_id)
    agent_live = session_exists(session)
    if window is not None and (
        decision_pending(runs.run_dir_for(project, run_id)) or not agent_live
    ):
        select_ctl_window(window)
        return runs.attach_target_argv(ctl_target()), ctl_target(window)
    if agent_live:
        return runs.attach_target_argv(runs.session_target(run_id)), None
    return None


def kill_ctl_window(run_id: str) -> None:
    """Kill the control-session window hosting this run's orchestrator process,
    if any. A no-op when the run was not launched from the TUI or tmux is gone."""
    window = ctl_window(run_id)
    if window is not None:
        get_multiplexer().kill_window(ctl_target(window))


def _ctl_window_candidates(project: Path) -> list[tuple[str, str]]:
    """(window_id, window_name) for parked control-session run windows whose run
    is no longer live — the kill candidates for a prune.

    A `<kind>-<run_id>` window parks on a `read` prompt that never closes on its
    own; it is a candidate once its run has finished/stopped/crashed (or its run
    dir is gone). The current window is excluded so a prune triggered from inside
    the ctl session never targets itself; live runs and the session's own shell
    window are excluded too.

    The control session is shared across projects, so pruning is scoped to
    `project` via the per-window PROJECT_OPTION tag (mirrors runs.prunable_sessions):
    a window tagged for another project is left alone; an untagged (pre-upgrade)
    window is only a candidate when its run dir exists under this project.
    """
    mux = get_multiplexer()
    if not mux_usable(mux) or not session_exists(CTL_SESSION):
        return []
    current = mux.current_window_id()
    rows = mux.list_windows(CTL_SESSION, ["window_id", "window_name", runs.PROJECT_OPTION])
    mine = runs.project_tag(project)
    candidates: list[tuple[str, str]] = []
    for win_id, name, tag in rows:
        if not win_id or win_id == current:
            continue
        m = _CTL_WINDOW_RE.match(name)
        if m is None:
            continue  # not a run window (e.g. the session's initial shell)
        if not runs.is_valid_run_id(m.group(1)):
            continue  # a foreign/mangled window name must not steer a run-dir path
        run_dir = runs.run_dir_for(project, m.group(1))
        if tag:
            if tag != mine:
                continue  # another project's window
        elif not runs.is_run(run_dir):
            continue  # untagged and no run dir here — ownership unprovable
        # boolean gate on purpose: an 'unknown' engine stays a candidate (unknown
        # never blocks cleanup) with no per-window warning — the session-level
        # unknown warning from prunable_sessions covers the operator surface.
        if runs.engine_alive(run_dir):
            continue
        candidates.append((win_id, name))
    return candidates


def prunable_ctl_windows(project: Path) -> list[str]:
    """Names of the control-session windows a prune would close (dry-run view)."""
    return [name for _, name in _ctl_window_candidates(project)]


def prune_ctl_windows(project: Path) -> list[str]:
    """Close parked control-session windows whose run is no longer live; returns
    the names of the windows that were closed (see _ctl_window_candidates)."""
    mux = get_multiplexer()
    killed: list[str] = []
    for win_id, name in _ctl_window_candidates(project):
        mux.kill_window(win_id)
        killed.append(name)
    return killed


def _ensure_ctl_session(project: Path) -> None:
    mux = get_multiplexer()
    # has_session is raiser-side (a server-backed backend can fail the probe after
    # the availability pre-gate). Keep it inside the try so a transport failure
    # converts to LaunchError, which the TUI launch/resume/resolve handlers already
    # catch — otherwise the raw MultiplexerError slips past them and crashes the app.
    try:
        if mux.has_session(CTL_SESSION):
            return
        mux.new_session(CTL_SESSION, project)
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer ctl-session setup failed: {e}") from e


def cli_argv(*tail: str) -> list[str]:
    """`sys.executable -m bmad_loop.cli ...` — immune to PATH/venv drift
    inside tmux windows."""
    return [sys.executable, "-m", "bmad_loop.cli", *tail]


def start_detached(project: Path, argv_tail: list[str], run_id: str, kind: str) -> str | None:
    """Run a bmad-loop command in a new window of the control session.

    The window parks after the command exits (keeping the exit status
    inspectable) and then returns an attached client to its origin pane — both
    handled by the multiplexer's parked-window primitive, keyed by the
    RETURN_OPTION recorded on the window by set_return_pane.

    Returns the new window's stable tmux id (@N) so callers can target it
    unambiguously (window names collide when several kinds share a run_id).
    """
    mux = get_multiplexer()
    if not mux_usable(mux):
        raise LaunchError(
            "multiplexer backend unavailable (binary missing, version unsupported, "
            "or a required helper absent)"
        )
    _ensure_ctl_session(project)
    try:
        win_id = (
            mux.new_parked_window(
                CTL_SESSION,
                f"{kind}-{run_id}",
                project,
                cli_argv(*argv_tail),
                RETURN_OPTION,
            )
            or None
        )
    except MultiplexerError as e:
        raise LaunchError(f"multiplexer new-window failed: {e}") from e
    if win_id:
        # Tag the window with its project so a cleanup in another project never
        # closes it (the ctl session is shared across projects).
        mux.set_window_option(win_id, runs.PROJECT_OPTION, runs.project_tag(project))
    return win_id


def start_run_detached(
    project: Path,
    run_id: str,
    *,
    spec: str | None = None,
    epic: int | None = None,
    story: str | None = None,
    max_stories: int | None = None,
) -> None:
    tail = ["run", "--project", str(project), "--run-id", run_id]
    if spec:
        tail += ["--spec", spec]  # forces stories mode (folder+id dispatch)
    if epic is not None:
        tail += ["--epic", str(epic)]
    if story:
        tail += ["--story", story]
    if max_stories is not None:
        tail += ["--max-stories", str(max_stories)]
    start_detached(project, tail, run_id, "run")


def start_sweep_detached(
    project: Path,
    run_id: str,
    *,
    no_prompt: bool = False,
    decisions_only: bool = False,
    max_bundles: int | None = None,
) -> None:
    tail = ["sweep", "--project", str(project), "--run-id", run_id]
    if no_prompt:
        tail.append("--no-prompt")
    if decisions_only:
        tail.append("--decisions-only")
    if max_bundles is not None:
        tail += ["--max-bundles", str(max_bundles)]
    start_detached(project, tail, run_id, "sweep")


def resume_detached(project: Path, run_id: str) -> None:
    start_detached(project, ["resume", "--project", str(project), run_id], run_id, "resume")


def start_resolve_detached(project: Path, run_id: str) -> str | None:
    """Run `bmad-loop resolve <run_id>` in a ctl-session window. The caller
    attaches to it: the resolve agent is interactive, and the post-session
    confirm + resume happen in that same window. Returns the window id so the
    caller attaches to exactly this window, not a stale same-run_id window."""
    return start_detached(
        project, ["resolve", "--project", str(project), run_id], run_id, "resolve"
    )


def run_captured_streams(argv_tail: list[str]) -> tuple[int, str, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its output
    with the two streams kept **apart**.

    Separation is the whole point of this seam. Anything parsing stdout as a
    whole document — a ``--json`` command, whose contract in :mod:`bmad_loop.machine`
    is that stdout is one JSON object and nothing else — cannot use the merged
    form: :func:`run_captured` appends stderr *after* stdout, so a single
    ``DeprecationWarning`` written to the child's stderr by any dependency turns
    ``json.loads`` into ``Extra data:``. That failure is environment-dependent —
    it needs the right interpreter, the right installed versions, the right
    warning filters — so it would pass everywhere it was tested and silently
    degrade the JSON path to the text one on a user's machine. A caller that
    genuinely wants one blob merges them itself; a caller that parses must never
    have been handed the option.

    Decoding is pinned to UTF-8 with ``errors="replace"`` rather than
    ``text=True``, which decodes with the *locale* encoding at ``errors="strict"``
    — the #200 family of failure already fixed CLI-side in :mod:`bmad_loop.machine`.
    A console in a non-UTF-8 code page must not turn a perfectly good document
    into a ``UnicodeDecodeError`` on the way in.
    """
    proc = subprocess.run(
        cli_argv(*argv_tail), capture_output=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_captured(argv_tail: list[str]) -> tuple[int, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its
    combined output for display.

    For text display only. Anything that parses the output must call
    :func:`run_captured_streams` — see its docstring on why the merge is
    unparseable.
    """
    rc, out, err = run_captured_streams(argv_tail)
    if err:
        if out and not out.endswith("\n"):
            out += "\n"
        out += err
    return rc, out
