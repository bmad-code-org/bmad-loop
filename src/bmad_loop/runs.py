"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import tarfile
import time
from pathlib import Path

from . import devcontract, verify
from .adapters.multiplexer import get_multiplexer
from .journal import STATE_FILE, Journal, load_state, save_state
from .model import PAUSE_ESCALATION, Phase, RunState, StoryTask
from .platform_util import (
    MAX_SEGMENT,
    atomic_replace,
    has_parent_ref,
    is_absolute_path,
    safe_segment,
)
from .process_host import get_process_host

RUNS_DIR = Path(".bmad-loop") / "runs"
ARCHIVE_DIR = Path(".bmad-loop") / "archive"
PID_FILE = "engine.pid"
_INVALID_PID_IDENTITY = -1.0  # impossible process start/create time; forces "not ours"


class StopRunError(Exception):
    """A live run could not be stopped — the engine ignored SIGTERM and its pid's
    identity can no longer be verified, so force-killing would risk an unrelated
    (reused) pid. The caller surfaces this rather than silently marking stopped."""


# How long stop_run waits for a signalled engine to exit before falling back to
# marking the run stopped itself.
_STOP_WAIT_S = 10.0
_STOP_POLL_S = 0.1


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


# A run id is a lookup key with exactly one legitimate producer (new_run_id), and it
# lands in three positions at once: a directory name under RUNS_DIR, a multiplexer
# session name (bmad-loop-<id>), and a git ref component (bmad-loop/<id>/<unit>).
# So an id supplied from outside is *rejected*, never sanitized — coercing it would
# break the id<->path<->session bijection the CLI relies on to find a run again.
#
# The charset is a superset of every new_run_id() output and excludes, by
# construction: path separators and `..` (traversal), `<>:"|?*` plus trailing dots
# and spaces (Windows), `.` and `:` (multiplexer session-name mangling), and all
# whitespace/control characters. It is also identity under safe_ref_segment, so the
# unit branch a run produces reads back verbatim — hence no ref check below.
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def is_valid_run_id(value: str) -> bool:
    """True when ``value`` is a run id we would have produced ourselves — the guard
    every externally-supplied ``--run-id`` and every id recomposed from the outside
    world (a foreign multiplexer session name) must pass before it touches a path.

    The length cap is ``platform_util.MAX_SEGMENT``: a run id is a directory name.
    The ``safe_segment`` identity check adds the one rule ``RUN_ID_RE`` cannot
    express — the reserved Windows device basenames (``CON``, ``NUL``, ``COM1``…),
    which are legal-looking ids that no filesystem will accept as a directory."""
    return (
        bool(RUN_ID_RE.fullmatch(value))
        and len(value) <= MAX_SEGMENT
        and safe_segment(value) == value
    )


def list_run_dirs(project: Path) -> list[Path]:
    """All run dirs containing a state.json, oldest first (run ids sort
    chronologically)."""
    runs = project / RUNS_DIR
    if not runs.is_dir():
        return []
    return sorted(d for d in runs.iterdir() if (d / "state.json").is_file())


def latest_run_dir(project: Path) -> Path | None:
    candidates = list_run_dirs(project)
    return candidates[-1] if candidates else None


def write_named_pid(pidfile: Path, pid: int) -> None:
    """Record ``pid`` plus its identity to ``pidfile``, so a later liveness read can
    tell our process from a stranger that inherited a reused pid (immediate on
    Windows). One whitespace-delimited line: ``"<pid>"`` (legacy) or
    ``"<pid> <identity>"``; the identity token is omitted when the platform can't
    provide one. The parameterized form :func:`write_pid` builds on — reused for the
    Unity dialog probe's own ``unity-dialog-probe.pid`` handle."""
    identity = get_process_host().identity(pid)
    line = f"{pid} {identity}" if identity is not None else str(pid)
    pidfile.write_text(line, encoding="utf-8")


def write_pid(run_dir: Path) -> None:
    """Record the engine pid plus its identity, so a later liveness read can tell
    our engine from a stranger that inherited a reused pid (immediate on Windows).
    Never deleted: a stale pid that reads as gone is the signal a run was
    interrupted."""
    write_named_pid(run_dir / PID_FILE, os.getpid())


def session_name(run_id: str) -> str:
    return f"bmad-loop-{run_id}"


def attach_target_argv(target: str) -> list[str]:
    """Multiplexer command to reach a target session/window (see
    :meth:`TerminalMultiplexer.attach_target_argv`)."""
    return get_multiplexer().attach_target_argv(target)


def session_target(run_id: str) -> str:
    """Seam-canonical target token for the run's agent session (see
    :meth:`TerminalMultiplexer.target`)."""
    return get_multiplexer().target(session_name(run_id))


def attach_argv(run_id: str) -> list[str]:
    return attach_target_argv(session_target(run_id))


# ---------------------------------------------------- run resolution / liveness


def run_dir_for(project: Path, run_id: str) -> Path:
    return project / RUNS_DIR / run_id


def is_run(run_dir: Path) -> bool:
    """A directory is a run iff it holds a state.json."""
    return (run_dir / STATE_FILE).is_file()


class RunRefError(Exception):
    """A run ref matched no run, or was ambiguous."""


def short_ref(run_id: str) -> str:
    """The trailing hex segment — the minimal handle users type."""
    return run_id.rsplit("-", 1)[-1]


def _is_path_escape(ref: str) -> bool:
    """True when ``ref`` would steer ``run_dir_for``'s recomposition outside the
    runs dir — it is absolute/drive-qualified, climbs with ``..``, or carries a
    path separator of either flavour. Sub-check of the run-id charset rather than
    `is_valid_run_id` itself: a run dir created by an older version (or by hand)
    may bear a name we would no longer mint, and must stay addressable."""
    return is_absolute_path(ref) or has_parent_ref(ref) or "/" in ref or "\\" in ref


def resolve_run_dir(project: Path, ref: str) -> Path:
    """Full or partial run id -> its run dir. An exact id wins outright;
    otherwise a partial matches when the trailing segment starts with `ref` or
    the full id ends with `ref` (run ids are date-prefixed, so the tail is what
    distinguishes them). Raises RunRefError on no match / ambiguity.

    The exact branch recomposes a path from the raw ref, so it is skipped for any
    ref that could escape the runs dir (`bmad-loop delete ../../x` would otherwise
    rmtree an outside directory that happens to hold a state.json). Such a ref
    falls through to partial matching, which can only ever yield a name
    `list_run_dirs` enumerated — and so cannot escape."""
    if not _is_path_escape(ref):
        exact = run_dir_for(project, ref)
        if is_run(exact):
            return exact
    matches = [
        d
        for d in list_run_dirs(project)
        if short_ref(d.name).startswith(ref) or d.name.endswith(ref)
    ]
    if not matches:
        raise RunRefError(f"no such run: {ref}")
    if len(matches) > 1:
        listing = "\n".join(f"  {d.name}" for d in matches)
        raise RunRefError(f"ambiguous run ref {ref!r} matches {len(matches)} runs:\n{listing}")
    return matches[0]


def read_pid(run_dir: Path) -> int | None:
    """The recorded engine pid, or None when missing/unparseable. Reads the first
    whitespace token, tolerating both the legacy pid-only file and the
    ``"<pid> <identity>"`` form (see :func:`read_pid_identity`)."""
    return read_pid_identity(run_dir)[0]


def read_pid_identity(run_dir: Path) -> tuple[int | None, float | None]:
    """The recorded engine pid and its persisted identity, from ``<run_dir>/engine.pid``.
    Thin wrapper over :func:`read_named_pid_identity` (which other pid files — the
    Unity dialog probe's — reuse)."""
    return read_named_pid_identity(run_dir / PID_FILE)


def read_named_pid_identity(pidfile: Path) -> tuple[int | None, float | None]:
    """The pid and its persisted identity recorded in ``pidfile``. ``(None, None)``
    when the file is missing or the pid is unparseable; identity ``None`` for a legacy
    pid-only file (callers then degrade to a bare existence check). A malformed
    second token is not legacy: it returns an impossible identity so reuse guards
    fail closed. First token is the pid, an optional second token the identity float."""
    try:
        tokens = pidfile.read_text(encoding="utf-8").split()
    except OSError:
        return None, None
    if not tokens:
        return None, None
    try:
        pid = int(tokens[0])
    except ValueError:
        return None, None
    identity: float | None = None
    if len(tokens) > 1:
        try:
            parsed = float(tokens[1])
        except ValueError:
            parsed = _INVALID_PID_IDENTITY
        # Only a true one-token legacy file degrades to bare existence. If an
        # identity token is present but corrupt/non-finite, fail closed as not-ours.
        identity = parsed if math.isfinite(parsed) else _INVALID_PID_IDENTITY
    return pid, identity


def engine_alive(run_dir: Path) -> bool:
    """True only when a local engine pid is provably alive **and still our engine**
    (identity-checked, so a reused pid reads as dead). Mirrors tui.data.liveness
    minus the tmux fallback — callers here want a definite 'is something running'
    answer, and 'unknown' must not block stop/delete."""
    pid, identity = read_pid_identity(run_dir)
    if pid is None:
        return False
    return get_process_host().alive_and_ours(pid, identity)


def engine_liveness(run_dir: Path) -> str:
    """Tri-state read of the local engine: ``'alive'`` | ``'dead'`` | ``'unknown'``.
    Wraps :meth:`ProcessHost.liveness_of` so a live-but-unreadable pid (win32
    ``ERROR_ACCESS_DENIED``) reads ``'unknown'``, not a false ``'dead'``. No pid →
    ``'dead'`` (the session fallback lives in the TUI layer)."""
    pid, identity = read_pid_identity(run_dir)
    if pid is None:
        return "dead"
    return probe_liveness(pid, identity)


def probe_liveness(pid: int, identity: float | None) -> str:
    """Tri-state probe of an already-read ``(pid, identity)`` — the shared body of
    :func:`engine_liveness` and ``tui.data.liveness``, so both read the pid file once.
    A probe failure degrades to ``'unknown'``, never a false ``'dead'``."""
    host = get_process_host()  # ProcessHostError (misconfig) propagates, not masked as unknown
    try:
        return host.liveness_of(pid, identity)
    except Exception:
        return "unknown"


# ----------------------------------------------------------- stop / delete / archive


def kill_session(run_id: str) -> None:
    """Kill a run's agent session (bmad-loop-<id>); a no-op when it is already
    gone or the multiplexer is unavailable."""
    get_multiplexer().kill_session(session_name(run_id))


CTL_SESSION = "bmad-loop-ctl"
_SESSION_PREFIX = "bmad-loop-"

# tmux user option stamping a session/window with the project it belongs to, so
# a prune in one project never touches another project's live runs. See
# prunable_sessions and tui.launch.
PROJECT_OPTION = "@bmad_project"


def project_tag(project: Path) -> str:
    """Canonical project identity stored in PROJECT_OPTION. The single source of
    normalization: both the tagging (at session/window creation) and the prune
    comparison must route through this so symlinks/relative paths can't make a
    project look foreign to its own sessions."""
    return str(project.resolve())


def mux_sessions() -> list[str]:
    """All live session names, or [] when the multiplexer is missing, no server
    is running, or the query fails."""
    return get_multiplexer().list_sessions()


def session_project_tags() -> dict[str, str]:
    """Map each live session name to its PROJECT_OPTION value ("" when unset).
    Same missing-multiplexer/no-server guards as mux_sessions()."""
    return get_multiplexer().session_options(PROJECT_OPTION)


def prunable_sessions(project: Path) -> tuple[list[str], list[str], set[str]]:
    """Partition the bmad-loop-<id> agent sessions into (prunable, live) run ids,
    plus the subset of prunable ids whose engine liveness read 'unknown'
    (unverifiable pid). Unknown never blocks cleanup — those sessions stay
    prunable — but frontends surface a warning for them.

    The control session (bmad-loop-ctl) is never a candidate. Pruning is scoped
    to `project` via the PROJECT_OPTION tag set at session creation:

    - tag == this project: ours — prunable unless a provably-alive engine pid is
      running (covers finished/stopped/crashed *and* orphans whose run dir was
      deleted, since engine_liveness reads 'dead' with no pid).
    - tag is another project: skipped — never touched.
    - tag empty (pre-upgrade, untagged session): can't prove ownership, so fall
      back to the run dir — prunable only when the dir exists under this project
      and is dead; skipped when the dir is absent.
    """
    tags = session_project_tags()
    mine = project_tag(project)
    prunable: list[str] = []
    live: list[str] = []
    unknown: set[str] = set()
    for name in mux_sessions():
        if name == CTL_SESSION or not name.startswith(_SESSION_PREFIX):
            continue
        run_id = name[len(_SESSION_PREFIX) :]
        if not is_valid_run_id(run_id):
            continue  # a foreign/mangled session name must not steer a run-dir path
        run_dir = run_dir_for(project, run_id)
        tag = tags.get(name, "")
        if tag:
            if tag != mine:
                continue  # another project's session
        elif not is_run(run_dir):
            continue  # untagged and no run dir here — ownership unprovable
        liveness = engine_liveness(run_dir)
        if liveness == "alive":
            live.append(run_id)
            continue
        prunable.append(run_id)
        if liveness == "unknown":
            unknown.add(run_id)
    return prunable, live, unknown


def prune_sessions(
    project: Path, *, dry_run: bool = False
) -> tuple[list[str], list[str], set[str]]:
    """Kill every prunable bmad-loop-<id> session (see prunable_sessions);
    returns (killed, live, unknown): the run ids that were (or, with dry_run,
    would be) killed, the live ids skipped, and the killed subset whose engine
    liveness read 'unknown'. All three come from the same partition sample, so
    frontend messaging built from them always describes the performed actions."""
    prunable, live, unknown = prunable_sessions(project)
    if not dry_run:
        for run_id in prunable:
            kill_session(run_id)
    return prunable, live, unknown


def stop_run(run_dir: Path) -> bool:
    """Stop a live run. Returns False if it was already finished.

    Prefers the engine's own SIGTERM handler so the engine stays the single
    writer of `stopped` (it marks the run, kills its in-flight agent window, and
    exits). Falls back to an external kill + mark when there is no live engine
    pid, it is a legacy run, or it does not exit in time. A wedged engine that
    ignores SIGTERM past the grace window is force-killed — but only while we can
    still prove the pid is the same process we signalled (a pid-reuse guard);
    otherwise we raise StopRunError rather than risk killing an unrelated process.
    """
    state = load_state(run_dir)
    if state.finished:
        return False

    host = get_process_host()
    pid, identity = read_pid_identity(run_dir)  # identity recorded at run start, not sampled now
    if pid is not None and identity is not None and not host.alive_and_ours(pid, identity):
        # the pid we recorded is already gone, or was reused by an unrelated
        # process before stop_run ran — never signal a stranger; mark stopped below.
        pid = None
    if pid is not None:
        try:
            host.terminate(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pid = None  # already gone / not ours — go straight to fallback
    if pid is not None:
        deadline = time.monotonic() + _STOP_WAIT_S
        while time.monotonic() < deadline:
            if not host.is_alive(pid):
                break  # exited
            time.sleep(_STOP_POLL_S)
        if host.is_alive(pid):
            # still wedged past the grace window — escalate to a force-kill, but
            # only if this is provably the same process we signalled (never SIGKILL
            # a pid the kernel may have recycled to an unrelated process). For a
            # legacy pid file (no persisted identity) fall back to a stop-time
            # sample so a pre-upgrade run can still be force-killed — today's
            # behavior, carrying the same late-sample reuse window it always had.
            guard = identity if identity is not None else host.identity(pid)
            if guard is not None and host.identity(pid) == guard:
                try:
                    host.force_kill(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass  # raced us to exit — that's the outcome we wanted
            else:
                raise StopRunError(
                    f"run {run_dir.name}: engine pid {pid} ignored SIGTERM and its "
                    "identity can no longer be verified; refusing to force-kill a "
                    "possibly-reused pid"
                )
        # the engine clears its agent window itself, but kill the session as a
        # backstop in case it died before tearing it down
        kill_session(run_dir.name)
        if load_state(run_dir).stopped:
            return True

    # Fallback: no live engine (or it never confirmed). Mark it stopped here.
    kill_session(run_dir.name)
    state = load_state(run_dir)
    state.stopped = True
    save_state(run_dir, state)
    Journal(run_dir).append("run-stop", pid=pid, fallback=True)
    return True


def delete_run(run_dir: Path) -> None:
    """Permanently remove a run directory. Callers enforce the live guard."""
    shutil.rmtree(run_dir)


def archive_run(project: Path, run_dir: Path) -> Path:
    """Compress a run dir into .bmad-loop/archive/<id>.tar.gz and remove the
    original. The tarball is written to a temp path then atomically replaced into
    place so a partial archive never appears. Callers enforce the live guard."""
    archive_dir = project / ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_dir.name}.tar.gz"
    tmp = dest.with_suffix(".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(run_dir, arcname=run_dir.name)
    atomic_replace(tmp, dest)
    shutil.rmtree(run_dir)
    return dest


# ------------------------------------------------------- reclaim / retention

# Heavy per-run scaffolding trimmed from a concluded run dir while the
# TUI-visible core (state.json, journal.jsonl, logs/, ATTENTION) is preserved,
# so the run still lists and renders in the dashboard. The value mirrors
# workspace.WORKTREE_DIRNAME; kept literal here to avoid an import cycle
# (workspace imports nothing from runs, but runs stays leaf-light on purpose).
_HEAVY_RUN_ENTRIES = ("worktrees",)


def _state_or_none(run_dir: Path):
    """Parsed run state, or None when it cannot be read — never classify (and so
    never reclaim) what you cannot positively read."""
    try:
        return load_state(run_dir)
    except Exception:  # noqa: BLE001 - unreadable/corrupt state ⇒ leave it alone
        return None


def is_finished(run_dir: Path) -> bool:
    """A finished, no-longer-live run. `resume` refuses these (cli checks
    state.finished), so tearing down their worktrees can never strand a resume —
    the safe predicate for the *automatic* reconcile paths."""
    if engine_alive(run_dir):
        return False
    state = _state_or_none(run_dir)
    return bool(state and state.finished)


def reclaimable(run_dir: Path) -> bool:
    """A terminal run (finished or stopped) with no live engine — eligible for
    the *explicit* `clean` command. A stopped run is technically resumable, so
    reclaiming its worktree ends that; `clean` is an opt-in reclaim (guarded by
    --keep / --dry-run). Paused, interrupted (crashed) and running/unknown-host
    runs are never reclaimed: paused/interrupted are actively resumable, and a
    missing pid could mean a foreign-host run, so we require positive local
    termination evidence (finished or stopped)."""
    if engine_alive(run_dir):
        return False
    state = _state_or_none(run_dir)
    return bool(state and (state.finished or state.stopped))


def reconcile_orphan_worktrees(repo: Path, run_dir: Path, *, dry_run: bool = False) -> list[Path]:
    """Force-remove every git worktree whose path lies under ``run_dir``, then
    prune git's admin entries. Reconciles from ``git worktree list`` (on-disk
    truth), NOT from policy — orphans created under a previous isolation=worktree
    config persist after a switch back to isolation=none. Returns the worktree
    paths handled (or that would be, under dry_run). Callers gate on
    ``reclaimable``; the main checkout is never under a run dir, so it is safe."""
    run_res = run_dir.resolve()
    try:
        worktrees = verify.worktree_list(repo)
    except verify.GitError:
        return []
    handled: list[Path] = []
    for wt in worktrees:
        try:
            wt.resolve().relative_to(run_res)
        except (ValueError, OSError):
            continue  # not this run's worktree (incl. the main checkout)
        handled.append(wt)
        if not dry_run:
            try:
                verify.worktree_remove(repo, wt, force=True)
            except verify.GitError:
                shutil.rmtree(wt, ignore_errors=True)
    if handled and not dry_run:
        verify.worktree_prune(repo)
    return handled


def reconcile_stale_worktrees(repo: Path, project: Path, *, dry_run: bool = False) -> list[Path]:
    """Safety net for the automatic paths (run/sweep start): tear down worktrees
    left behind by a *finished* run whose clean-finish GC didn't complete (e.g. a
    crash between merge and teardown). Deliberately finished-ONLY — a stopped run
    is still resumable, so its worktree is left for `resume`/`clean` to handle and
    never stranded out from under the operator."""
    handled: list[Path] = []
    for run_dir in list_run_dirs(project):
        if not is_finished(run_dir):
            continue
        handled += reconcile_orphan_worktrees(repo, run_dir, dry_run=dry_run)
    return handled


def trim_run_dir(run_dir: Path, *, dry_run: bool = False) -> list[Path]:
    """Delete heavy scaffolding (the ``worktrees/`` tree) from a concluded run
    dir, preserving its TUI-visible core so the run still appears in the
    dashboard with full status/journal/logs. Returns the paths removed."""
    removed: list[Path] = []
    for name in _HEAVY_RUN_ENTRIES:
        p = run_dir / name
        if p.exists() or p.is_symlink():
            removed.append(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
    return removed


def _run_started_epoch(run_dir: Path) -> float | None:
    """Unix time parsed from the run id's ``YYYYMMDD-HHMMSS`` prefix, or None
    when the name does not carry one (legacy/foreign id)."""
    try:
        return time.mktime(time.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def runs_past_retention(
    run_dirs: list[Path], *, keep_n: int, keep_days: int = 0, now: float | None = None
) -> list[Path]:
    """The subset of ``run_dirs`` (oldest-first) beyond the retention window:
    not among the newest ``keep_n``, and — when ``keep_days`` is set — also older
    than ``keep_days`` days. ``keep_n <= 0`` retains nothing by count; an
    unparseable run id is treated as old enough to prune once past ``keep_n``."""
    ordered = list(run_dirs)
    candidates = (
        ordered[:-keep_n]
        if keep_n > 0 and len(ordered) > keep_n
        else ([] if keep_n > 0 else list(ordered))
    )
    if keep_days and keep_days > 0:
        cutoff = (time.time() if now is None else now) - keep_days * 86400
        return [rd for rd in candidates if (_run_started_epoch(rd) or 0.0) < cutoff]
    return candidates


# ----------------------------------------------------------- escalation resolution


class RearmError(Exception):
    """The run/story is not in a re-armable escalation state."""


def validate_restore_latch(
    state: RunState, task: StoryTask, story_key: str, *, worktree_isolation: bool = False
) -> str | None:
    """Every precondition an intent-gap patch-restore latch (BMAD-METHOD #2564) must
    satisfy, in one place. Returns an operator-facing error string, or None to latch.

    The single seam for both entry points: `rearm_escalation` (which performs the
    latch, and is also reachable programmatically — a TUI restore, a future caller)
    and `cli._resolve_restore_patch` (which fails fast *before* the interactive
    resolve session, so an unhonorable restore doesn't cost an agent conversation).
    Splitting these let a non-CLI caller bypass the worktree half; keeping them here
    means a caller cannot latch a patch the engine could never honor.

    The CLI knows one thing this cannot: the *live* policy's isolation mode, which
    may have been edited between escalation and resolve. It passes that as
    `worktree_isolation`; the recorded `task.worktree_path` (how the unit actually
    executed) is checked here either way, so both entry points reject a
    worktree-isolation restore and the CLI additionally catches a policy flip.

    Path resolution and trusted-roots containment stay CLI-side: they need
    `--project` and the loaded bmad config, neither of which run state carries.
    """
    # A sentinel-wedged story escalated BEFORE planning — there is no attempted
    # implementation to restore, and its re-arm re-dispatches a planning leg.
    # Keyed on the recorded detection verdict (task.sentinel_kind), not the on-disk
    # basename, mirroring rearm_escalation's sentinel-clear branch.
    if state.source == "stories" and task.sentinel_kind:
        return (
            f"story {story_key} is wedged on a pre-planning {task.sentinel_kind} sentinel — "
            "there is no attempted implementation to restore, and the re-drive starts "
            "at planning. Re-run resolve without a restore patch for a clean re-plan."
        )
    # Same seam, broader shape: a restore only works through the spec's in-review
    # flip, so an escalation with NO recorded spec (an ambiguous two-file wedge, an
    # unknown --story selector, a session that died before naming one) has no
    # routing target — the latch would stick, the flip would be skipped, and the
    # engine would lay the patch onto the tree before a planning leg.
    if not task.spec_file:
        return (
            f"story {story_key} has no recorded spec file, so a restored patch has no "
            "review to resume (the re-drive starts at planning). Re-run resolve "
            "without a restore patch for a from-scratch re-drive."
        )
    # Restore is an in-place-only recovery: a worktree-isolation re-drive discards
    # the unit's worktree (engine._finish_inflight — taking a patch saved inside it
    # along) and re-mounts a fresh one, so the re-apply could only fail on a
    # destroyed patch file. Reject up front instead of latching a patch that can
    # never restore.
    if worktree_isolation or task.worktree_path:
        return (
            "restore patch is unsupported for worktree-isolation runs (the re-drive "
            "discards and re-mounts the unit's worktree, so an in-place restore has "
            "nothing durable to land on) — re-arm from scratch instead: drop "
            "--restore-patch, or if the resolve agent recorded the restore in "
            "resolution.json, re-run with --no-interactive (which ignores that "
            "marker) instead of repeating the agent session"
        )
    return None


def rearm_escalation(
    run_dir: Path, story_key: str | None = None, *, restore_patch: str | None = None
) -> str:
    """Re-arm an escalation-paused story so the next resume re-drives it.

    Flips the escalated task out of its terminal ESCALATED phase back to
    PENDING — which makes `_finish_inflight` reset the tree to the story's
    baseline and re-run it (clean rebuild) against the now-corrected frozen
    spec. The baseline itself is advanced to the project's current HEAD (and
    the untracked snapshot refreshed) so commits and files the resolve session
    produced count as the rebuild's starting point, not as attempt debris to
    roll back. Strips the escalated attempt's stale `## Auto Run Result`
    section so the re-drive cannot read as terminal from its first save, and
    sets the spec's frontmatter status so step-01 routes to the right stage.
    Does NOT clear the pause; the caller resumes the run separately.

    Two re-drive modes, selected by `restore_patch`:

    - **from-scratch** (default, ``restore_patch=None``): status → ``ready-for-dev``
      so the dev session re-implements from a clean baseline. Assigning None also
      clears any stale latch from a prior restore attempt the human abandoned.
    - **patch-restore** (BMAD-METHOD #2564, ``restore_patch`` set): the human
      confirmed the escalated attempt's reading was correct. Status → ``in-review``
      so step-01 routes straight to step-04, and the path is latched onto the task
      (`task.restore_patch`) so the engine re-applies the saved patch onto the
      baseline before dispatching — the re-driven session resumes review on the
      restored diff instead of re-implementing. The status is set here
      deterministically; the resolve agent must NOT set it. Because the baseline
      advances (above) while the patch was diffed from the OLD baseline, a resolve
      session that committed changes to the patched files makes the re-drive's
      apply fail — the engine then escalates loudly instead of dispatching on a
      half-restored tree (see verify.apply_patch).

    Stories mode: when the escalated spec is a fixed-slug sentinel
    (`<id>-unresolved.md` / `<id>-ambiguous.md`, written by a pre-planning HALT),
    it cannot be re-opened by a status flip — its very presence wedges the id.
    Instead preserve a copy under `{run_dir}/sentinels/`, journal `sentinel-cleared`
    with the blocking condition, and delete it, so the re-dispatch resolves to a
    clean PENDING and re-plans from scratch (leg 1 again for a spec_checkpoint id).

    Returns the re-armed story key. Raises RearmError when the run is not paused at
    the escalation stage, the target story is not escalated, or a supplied
    `restore_patch` fails `validate_restore_latch` (the shared precondition set —
    sentinel wedge, spec-less escalation, worktree isolation).
    """
    state = load_state(run_dir)
    if state.paused_stage != PAUSE_ESCALATION:
        raise RearmError(
            f"run {run_dir.name} is not paused at an escalation "
            f"(stage: {state.paused_stage or 'none'})"
        )
    key = story_key or state.paused_story_key
    if key is None:
        raise RearmError(f"run {run_dir.name} has no escalated story to resolve")
    task = state.tasks.get(key)
    if task is None:
        raise RearmError(f"run {run_dir.name} has no task for story {key}")
    if task.phase != Phase.ESCALATED:
        raise RearmError(f"story {key} is not escalated (phase: {task.phase})")
    # Patch-restore preconditions (T1 guard + spec-less wedge + worktree isolation),
    # rejected here before any task mutation so the escalation stays armed for a
    # corrected resolve. `cli._resolve_restore_patch` runs the same validator ahead
    # of the interactive session; this call is what makes a programmatic caller
    # (TUI restore parity, scripts) unable to bypass it.
    if restore_patch:
        err = validate_restore_latch(state, task, key)
        if err is not None:
            raise RearmError(err)

    journal = Journal(run_dir)
    # Read before the unconditional overwrite below: they describe the restore
    # attempt this re-arm is abandoning, and the residue block needs both.
    old_latch = task.restore_patch
    old_baseline = task.baseline_commit
    # deliberate reset, not a normal state-machine transition (mirrors
    # engine._finish_inflight): a clean re-attempt against the corrected spec.
    task.phase = Phase.PENDING
    task.attempt = 0
    task.review_cycle = 0
    task.followup_reviews_spent = 0  # human-resolved re-drive gets a fresh damping budget
    task.defer_reason = None
    # Both re-drive routes re-establish an exact spec/orchestrator baseline match
    # — ready-for-dev re-runs step-03, which stamps the advanced sha itself, and
    # the patch-restore route re-stamps it below — so a latch left by a kept
    # attempt of the PREVIOUS, escalated cycle must not survive into this one and
    # keep the ancestor relaxation armed for work it never covered.
    task.baseline_advanced = False
    task.rearmed = True  # resume-time recovery notice describes a clean rebuild,
    # not a failed attempt (engine._finish_inflight clears it once the rebuild runs)
    # Always (re)assign the latch: a None restore_patch clears a stale one left by
    # a prior restore attempt the human then chose to redo from scratch.
    task.restore_patch = restore_patch

    if task.spec_file:
        spec_path = Path(task.spec_file)
        # Stories mode only: a fixed-slug pre-planning-halt sentinel
        # (`<id>-unresolved.md` / `<id>-ambiguous.md`) is cleared by deletion, not a
        # status flip. Clear it ONLY when the run recorded this task AS a sentinel at
        # detection time (`task.sentinel_kind`, stamped by StoriesEngine's pick-time
        # wedge / post-dev read-back) — never by re-deriving from the basename. That
        # keeps a real story spec that merely happens to be named `<key>-unresolved.md`,
        # or a *non-sentinel* escalation whose spec matches the convention, on the
        # status-flip path so it is kept, not deleted. Gate on the run source too (the
        # convention exists only in stories mode) and defensively re-confirm the
        # on-disk name still matches the recorded slug before deleting.
        sentinel_kind = task.sentinel_kind if state.source == "stories" else ""
        if sentinel_kind and _sentinel_condition(spec_path, key) == sentinel_kind:
            # a sentinel is cleared by deletion, not a status flip; drop the stale
            # spec_file so the re-dispatch starts from PENDING (clean re-plan).
            _clear_sentinel(run_dir, journal, spec_path, key, sentinel_kind)
            task.spec_file = None
            task.sentinel_kind = ""  # verdict discharged; the re-dispatch is clean
        else:
            try:
                # Route /bmad-dev-auto via the spec's frontmatter status (decision
                # table): patch-restore -> in-review -> step-04 (resume review on
                # the restored diff); from-scratch -> ready-for-dev -> step-03
                # (re-implement). Independent of the resolve agent having set it.
                target_status = "in-review" if restore_patch else "ready-for-dev"
                verify.set_frontmatter_status(spec_path, target_status)
                # drop the stale `## Auto Run Result` section along with the status flip
                # (mirrors engine._reset_spec_for_repair): find_result_artifact keys on
                # that heading, so leaving it would let the re-driven session's first
                # save of the spec parse as the prior attempt's terminal outcome.
                devcontract.strip_auto_run_result(spec_path)
            except (OSError, UnicodeDecodeError) as e:
                # Both helpers re-read the spec as UTF-8; an undecodable PRESENT
                # spec is a first-class escalation state (resolve_story_spec
                # degrades it to a wedge), so it can reach this flip. Without the
                # flip the re-drive would just re-wedge — abort BEFORE any state
                # is persisted (save_state runs below) with an actionable error
                # instead of a traceback; the escalation stays armed for a retry.
                raise RearmError(
                    f"cannot re-open story spec {spec_path} for the re-drive "
                    f"({e.__class__.__name__}: {e}) — fix or replace the file "
                    f"(it must be readable UTF-8), then re-run resolve"
                ) from e

    # A previous restore latch is being replaced (or re-latched onto the same
    # patch): the abandoned attempt applied that patch, so its NEW files sit
    # untracked in the tree right now. The refresh below would capture them as
    # "pre-existing" — after which every rollback preserves them and
    # finalize_commit's `add -A` sweeps the abandoned attempt into the corrected
    # story's commit. Subtract them instead (issue #90).
    #
    # Runs after the spec block for the same reason the refresh does (a cleared
    # sentinel must not be snapshotted), and before it because it feeds it.
    # Nothing is deleted here: the re-drive's reset (verify.safe_rollback) removes
    # whatever the refreshed snapshot no longer blesses, at the right moment.
    stale_residue = _stale_restore_residue(
        Path(state.project), journal, key, old_latch, old_baseline
    )

    # Advance the attempt baseline to the project's current HEAD and refresh the
    # untracked snapshot: whatever the human-driven resolve session left on the
    # branch (a committed fixture, a corrected ledger, ...) is authorized input
    # for the re-drive, not failed-attempt debris. Without this, the re-drive's
    # reset-to-baseline in engine._rollback_or_pause parks the resolution
    # commits on an attempt-preserve ref and rebuilds against a tree that
    # contradicts the corrected spec — the re-driven dev session then hits the
    # very gap the human just resolved. Best-effort: on a git failure the old
    # baseline stands (the redrive rollback path tolerates a stale baseline; it
    # just loses this protection).
    # Runs AFTER the spec block so a just-cleared stories sentinel (an untracked
    # file removed above) is not captured into baseline_untracked as a phantom
    # pre-existing untracked file. The two locals are computed before either task
    # field is assigned, so a failure on either git call can't advance
    # baseline_commit while baseline_untracked stays stale, or vice versa.
    try:
        repo = Path(state.project)
        head = verify.rev_parse_head(repo)
        untracked = sorted(verify.untracked_files(repo) - stale_residue)
        task.baseline_commit = head
        task.baseline_untracked = untracked
    except Exception:  # noqa: BLE001  # nosec B110 - best-effort git read, must not fail re-arm
        pass

    # Patch-restore only: re-stamp the spec's own baseline to the advanced one.
    # The in-review route skips step-03 — the only step that stamps
    # `baseline_revision` — so without this the re-driven step-04 would build its
    # review diff (and, on an intent-gap/bad-spec re-triage, revert) "since" the
    # ORIGINAL pre-attempt sha, clawing back the very resolve-session commits the
    # advance above just blessed as the re-drive's starting point. Loud on
    # failure: a silently stale spec baseline is exactly the hazard being closed
    # (the spec block above already proved the file readable, so this is remote).
    if restore_patch and task.spec_file and task.baseline_commit:
        try:
            verify.set_frontmatter_field(
                Path(task.spec_file), "baseline_revision", task.baseline_commit
            )
        except (OSError, UnicodeDecodeError) as e:
            raise RearmError(
                f"cannot re-stamp baseline_revision on {task.spec_file} "
                f"({e.__class__.__name__}: {e}) — fix the file, then re-run resolve"
            ) from e

    save_state(run_dir, state)
    journal.append(
        "story-escalation-resolved",
        story_key=key,
        baseline=task.baseline_commit or "",
        restore=bool(restore_patch),
    )
    return key


def _stale_restore_residue(
    repo: Path,
    journal: Journal,
    story_key: str,
    old_latch: str | None,
    old_baseline: str | None,
) -> set[str]:
    """The untracked files an abandoned patch-restore attempt left in the tree —
    to be subtracted from the re-arm's refreshed `baseline_untracked` (issue #90).

    Empty when no restore was latched. Deliberately *not* a `git apply -R`: the
    re-drive's own reset already reverts the patch's tracked hunks, an `apply -R`
    fails outright on any drift the resolve session introduced, and it misbehaves
    on the committed variant below. Only the patch's new files are durable
    contamination, and naming them is enough — `verify.safe_rollback` deletes
    whatever the refreshed snapshot stops blessing.

    Also journals (warn-only) the commits sitting between the OLD baseline and the
    new one: a commit the escalated re-drive session made now becomes the next
    re-drive's permanent starting point, and no reset revisits it. It is not
    mechanically reversible — the resolve session's own blessed commits live in the
    same range and reverting those would claw back the human's resolution — so the
    human is the classifier. `bmad-loop resolve` echoes these to stderr.

    Best-effort throughout: a deleted or unreadable patch, a non-repo project, a
    bad old baseline — none may wedge a resolve. Every failure degrades to the
    pre-#90 behavior and says so in the journal.
    """
    if not old_latch:
        return set()
    patch_path = verify.resolve_restore_path(old_latch, repo)

    residue: set[str] = set()
    try:
        residue = verify.patch_new_files(patch_path)
    except (OSError, UnicodeDecodeError) as e:
        # degrade to the pre-#90 snapshot rather than wedge the resolve
        journal.append(
            "stale-restore-unparseable",
            story_key=story_key,
            patch=str(patch_path),
            error=f"{e.__class__.__name__}: {e}",
        )
    else:
        if residue:
            journal.append(
                "stale-restore-excluded",
                story_key=story_key,
                patch=str(patch_path),
                files=sorted(residue),
            )

    # Independent of the parse above — an unreadable patch must not also cost the
    # human the only notice they get about the committed variant.
    if old_baseline:
        try:
            shas = verify.commits_above(repo, old_baseline)
        except Exception:  # noqa: BLE001  # nosec B110 - warn-only, must not fail re-arm
            shas = []
        if shas:
            journal.append(
                "stale-restore-commits",
                story_key=story_key,
                old_baseline=old_baseline,
                commits=shas,
            )
    return residue


def _sentinel_condition(spec_path: Path, story_key: str) -> str | None:
    """The blocking condition (``unresolved`` / ``ambiguous``) iff ``spec_path`` is
    a fixed-slug pre-planning-halt sentinel for ``story_key``, else None."""
    from .stories import SENTINEL_SLUGS

    for slug in SENTINEL_SLUGS:
        if spec_path.name == f"{story_key}-{slug}.md":
            return slug
    return None


def _clear_sentinel(
    run_dir: Path, journal: Journal, spec_path: Path, story_key: str, sentinel_kind: str
) -> None:
    """Preserve a copy of the sentinel under ``{run_dir}/sentinels/`` (a write-only
    breadcrumb of what blocked planning), journal ``sentinel-cleared`` — carrying
    both the fixed slug (``sentinel_kind``) and the *recorded blocking condition*
    parsed from the sentinel's ``## Auto Run Result`` (the reason planning halted) —
    then delete the sentinel so the next dispatch is clean."""
    from .stories import recorded_blocking_condition

    dest_dir = run_dir / "sentinels"
    dest_dir.mkdir(parents=True, exist_ok=True)
    condition = ""
    if spec_path.is_file():
        try:
            condition = recorded_blocking_condition(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            # An unreadable/binary sentinel still gets preserved+deleted so re-arm
            # completes; we just journal an empty blocking condition.
            condition = ""
        shutil.copy2(spec_path, dest_dir / spec_path.name)
        spec_path.unlink()
    journal.append(
        "sentinel-cleared",
        story_key=story_key,
        sentinel_kind=sentinel_kind,
        condition=condition,
        sentinel=spec_path.name,
    )
