"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path

from . import devcontract, envvars, verify
from .adapters.multiplexer import MultiplexerError, get_multiplexer
from .journal import STATE_FILE, VERIFY_DIR, Journal, load_state, save_state
from .model import PAUSE_ESCALATION, Phase, RunState, StoryTask
from .platform_util import (
    MAX_SEGMENT,
    atomic_replace,
    atomic_write_text,
    has_parent_ref,
    is_absolute_path,
    retrying_unlink,
    safe_segment,
)
from .process_host import get_process_host

RUNS_DIR = Path(".bmad-loop") / "runs"
ARCHIVE_DIR = Path(".bmad-loop") / "archive"
PID_FILE = "engine.pid"
# Cross-process channel for a graceful-stop request: a control file the requester
# (CLI/TUI) writes and the engine polls at item boundaries. Distinct from the hard
# SIGTERM stop_run delivers — there is no SIGUSR1 on Windows/psmux and SIGTERM
# already means "hard stop". The engine stays the single writer of journal.jsonl;
# requesters only ever touch this file.
STOP_REQUEST_FILE = "stop-request.json"
# The host-exec config baseline's name inside a run's state dir (see
# `config_digest_path_for`). A bare hex digest, not JSON: one opaque token, and a
# format an operator can read with `cat`.
CONFIG_DIGEST_FILE = "config-digest"
# Read cap for the file above. A sha256 hex digest is 64 bytes; the slack is for
# a trailing newline and for saying "this is not the digest" out of a file that
# is merely wrong rather than hostile. The cap's real job is the hostile case —
# see `read_trusted_config_digest` on why a bound, not a bigger buffer.
_MAX_DIGEST_BYTES = 256
_INVALID_PID_IDENTITY = -1.0  # impossible process start/create time; forces "not ours"


class StopRunError(Exception):
    """A live run could not be stopped — the engine ignored SIGTERM and its pid's
    identity can no longer be verified, so force-killing would risk an unrelated
    (reused) pid. The caller surfaces this rather than silently marking stopped."""


class GracefulStopError(Exception):
    """A graceful-stop request could not be lodged (run already finished, or its
    engine is provably dead so the request would never be consumed). ``str()`` is
    the operator-facing message the CLI/TUI surface verbatim."""


class LiveSessionError(Exception):
    """A run directory was not removed because the run's agent session is still
    live (see :func:`live_session_may_be_ours`). ``str()`` is the operator-facing
    message the CLI/TUI surface verbatim."""


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


# ------------------------------------------------------- user-scoped state root


class StateRootError(Exception):
    """No user-scoped state root could be derived from this environment — every
    candidate base was unset, empty, relative, or named the filesystem root. The
    control plane has nowhere to live, and the caller must fail rather than guess
    (see :func:`state_root`)."""


def _state_base(value: str | None) -> Path | None:
    """``value`` as a usable base directory, or ``None`` when it cannot be one.

    The single rule every *derived* candidate below is held to, so the POSIX and
    win32 branches cannot drift into judging their inputs differently. A base is
    rejected when it is unset, empty, relative, or names the filesystem root
    itself. The last three are the answers a broken environment gives *instead* of
    raising, which is what makes them worth naming:

    - **empty**: ``os.path.expanduser("~")`` answers ``""`` on Windows for a
      set-but-empty ``USERPROFILE``, and ``Path("")`` is the current directory.
    - **relative**: including ``"~"`` itself, which is what ``expanduser`` returns
      when it cannot expand at all. The state root would then move with the
      launch cwd, and a run whose control plane it cannot find again is a run
      that stalls to ``session_timeout_min`` rather than one that fails.
    - **the root**: ``expanduser("~")`` answers ``"/"`` on POSIX for a set-but-empty
      ``HOME`` (``posixpath`` folds the empty prefix to the root), which would put
      ``/.local/state/bmad-loop`` on the filesystem root — a permission error for
      an ordinary user and, for a containerised root, a silent write to ``/``.
      ``base == base.parent`` is the root test on both flavours.

    ``os.path.isabs`` rather than :func:`platform_util.is_absolute_path`: the
    latter is purpose-built for "must stay inside the project" guards and is
    strictly broader — it calls the drive-*relative* ``C:foo`` absolute, which is
    exactly the value that must not become a state root. The question here is the
    platform's own, and each branch below only ever runs on its own platform.
    """
    if not value or not os.path.isabs(value):
        return None
    base = Path(value)
    return None if base == base.parent else base


def state_root() -> Path:
    """The bmad-loop state root for this user: the out-of-tree home of per-run
    control-plane state — the events channel (#494) and, later, the config digest
    (#498). Outside the project tree because a branch switch, a worktree mount or
    a rollback must not be able to take a live run's control plane away.

    Resolution, first answer wins:

    1. ``BMAD_LOOP_STATE_DIR``, used as the state root **itself** — no
       ``bmad-loop`` segment is appended, because the variable names our root
       rather than a base to build one under. It is honoured as spelled (see
       :func:`envvars.state_dir`) and is not passed through ``_state_base``:
       *skipping* a stated override would be a silent countermand, where skipping
       a derived base only moves on to the next guess.

       It must still be **absolute**, and a relative spelling raises rather than
       being resolved for the operator. Absoluteness is not a matter of taste
       here — the root is read by two processes with different working
       directories. The engine exports it to the session as
       ``BMAD_LOOP_EVENTS_DIR`` and the multiplexer launches that session at
       ``spec.cwd`` (a worktree under isolation), while the watcher polls it from
       the orchestrator's own cwd. A relative root therefore names two different
       directories at once: the relay writes its Stop where nothing is watching,
       and the run waits out ``session_timeout_min`` — the exact silent stall
       ``_state_base`` rejects relative *derived* bases to avoid, and the one
       this whole channel was moved out of the tree to prevent.

       Raising is not the countermand the paragraph above refuses: it names the
       variable and the fix, where absolutizing against whichever cwd this
       process happens to have would be the guess. The not-the-root half of
       ``_state_base``'s rule is deliberately *not* applied — that half exists to
       stop a broken environment's ``""`` from landing a guess at ``/``, and an
       override is not a guess.
    2. POSIX — ``$XDG_STATE_HOME/bmad-loop`` when that variable names an absolute
       path, else ``~/.local/state/bmad-loop``. A relative ``XDG_STATE_HOME`` is
       *ignored*, which the XDG base-directory spec requires of its consumers.
       (``install._shield_inherited_excludes`` resolves a relative
       ``XDG_CONFIG_HOME`` instead of ignoring it — the opposite call for the
       opposite reason: there we reproduce *git's* reading of the variable, here
       we are the spec's own consumer.)
    3. win32 — ``%LOCALAPPDATA%\\bmad-loop\\state``, else
       ``%USERPROFILE%\\AppData\\Local\\bmad-loop\\state``. ``LOCALAPPDATA`` names
       the per-user, per-machine, non-roaming store Windows intends for exactly
       this, and the second form is its documented default location.

    **Never** ``Path.home()`` on the win32 arm. It is ``ntpath.expanduser("~")``,
    which prefers ``USERPROFILE`` and then falls back to ``HOMEDRIVE`` +
    ``HOMEPATH`` — a pair that on a domain-joined machine may name a network home
    share. A control plane whose atomic renames and ``O_NOFOLLOW``-anchored writes
    live on an SMB share is not the local directory this needs, and the derivation
    also disagrees with the one git uses for its own ``$HOME``
    (``install._shield_home_git_ignore`` documents that split in full). Reading
    ``LOCALAPPDATA``/``USERPROFILE`` directly asks for the store by name instead of
    inferring it from a home.

    Raises :class:`StateRootError` when no candidate answers. This is a write
    path, so it raises rather than degrading to a plausible-looking default:
    ``platform_util.resolve_or_lexical`` states the doctrine (observation may
    degrade, repair writes must raise), and the degraded outcomes here are all
    silent — a control plane at the cwd, or at ``/``, that the *next* process to
    ask resolves somewhere else.
    """
    override = envvars.state_dir()
    if override:
        # `os.path.isabs` on the raw string, matching `_state_base` exactly rather
        # than `Path.is_absolute` — the rule and its reason are stated there.
        if not os.path.isabs(override):
            raise StateRootError(
                f"{envvars.STATE_DIR} must name an absolute directory: {override!r} is "
                "relative, and the state root is read by both this process and the "
                "session it launches — which run from different working directories, "
                "so a relative root names two different places and the run's "
                "completion signal is written where nothing is watching"
            )
        return Path(override)
    if sys.platform == "win32":
        local = _state_base(os.environ.get("LOCALAPPDATA"))
        if local:
            return local / "bmad-loop" / "state"
        profile = _state_base(os.environ.get("USERPROFILE"))
        if profile:
            return profile / "AppData" / "Local" / "bmad-loop" / "state"
    else:
        xdg = _state_base(os.environ.get("XDG_STATE_HOME"))
        if xdg:
            return xdg / "bmad-loop"
        home = _state_base(os.path.expanduser("~"))
        if home:
            return home / ".local" / "state" / "bmad-loop"
    raise StateRootError(
        "cannot locate a state directory for bmad-loop's run control plane: "
        + (
            "neither %LOCALAPPDATA% nor %USERPROFILE% names an absolute directory"
            if sys.platform == "win32"
            else "neither $XDG_STATE_HOME nor $HOME names an absolute directory"
        )
        + f" — set {envvars.STATE_DIR} to the directory it should live in"
    )


def project_state_root(project: Path) -> Path:
    """The subtree of :func:`state_root` holding every run of this project:
    ``<state root>/<project key>``. Split out from :func:`state_dir_for` because
    the GC reads it as a *directory to enumerate* rather than composing one run's
    path — see :func:`reconcile_orphan_state_dirs`, whose whole job is the entries
    under here that no longer have a run dir."""
    return state_root() / project_tag(project)


def state_dir_for(project: Path, run_id: str) -> Path:
    """This run's control-plane directory: ``<state root>/<project key>/<run id>``.

    The project key is :func:`project_tag`, reused verbatim rather than re-derived:
    it already resolves the project before digesting it, so the two spellings of
    one project a caller can arrive with — a symlinked path, a relative one — key
    to the same directory. They must, or a run started through one spelling would
    write its events where a poll through the other never looks, and the run would
    wait out ``session_timeout_min`` with the completion signal sitting on disk.
    Its ``resolve()`` raising on a project the OS cannot canonicalize is correct
    here for the same reason: an unknowable location cannot be keyed at all, and
    guessing one is the wrong-directory write the tag exists to prevent.

    ``run_id`` needs no sanitizing — the id contract (see :data:`RUN_ID_RE`) is
    already "a legal path segment on every platform", pinned by
    :func:`is_valid_run_id`, and an id from outside is rejected there rather than
    coerced here.
    """
    return project_state_root(project) / run_id


def events_dir_for(project: Path, run_id: str) -> Path:
    """The run's hook-event channel: the directory the relay writes a session's
    events into and ``SignalWatcher`` polls for them."""
    return state_dir_for(project, run_id) / "events"


def config_digest_path_for(project: Path, run_id: str) -> Path:
    """The run's host-exec config baseline: ``runsetup.config_digest`` as of the
    last time a human started or resumed this run (#498).

    Out here rather than in ``state.json`` because the baseline exists to police
    the agent-writable tree, and until this move it *lived* in it: a session that
    rewrote ``policy.toml`` could blank or re-stamp the field in the same breath
    and the warning `resume` owes the operator never fired. The same reasoning the
    events channel moved on (#494).

    **What moving it buys, stated exactly.** It closes the *incidental* path: the
    pin is no longer a project file, so nothing a session does in the ordinary
    course of rewriting the tree can collaterally blank it — which is the case the
    advisory was documented to catch. It is **not** a boundary against a
    deliberate one. Sessions run with permission bypass by default — every shipped
    profile's ``bypass_args``, which ``GenericAdapter.interactive_argv`` uses
    unless ``[adapter] extra_args`` overrides them; that is what an unattended loop
    is — and are handed ``BMAD_LOOP_EVENTS_DIR``, whose parent is this directory. A
    session that goes looking can *truncate* this file and the reader below answers
    ``""`` — a real "no baseline" — or delete it and blank the in-tree copy
    (``RunState.trusted_config_digest``, the secondary this falls back to) for the
    same silence. Either way the result is indistinguishable from a run that never
    had a baseline: any marker saying "this run *should* have one" would have to
    live somewhere the same session cannot reach, and no such place exists at equal
    privilege. Closing it needs privilege separation on the state root, not a better
    hiding place — tracked in #571."""
    return state_dir_for(project, run_id) / CONFIG_DIGEST_FILE


def read_trusted_config_digest(project: Path, run_id: str) -> str | None:
    """This run's persisted host-exec baseline, or ``None`` when the state root
    holds none for it.

    ``None`` is "ask the in-tree copy", not "no pin" — the two are different
    answers and the caller acts on the difference (see
    ``cli._resume_paused_run``). No file here means this run's baseline is
    reachable only through ``state.json``: it was paused before #498, or the
    project moved and keyed its state subtree somewhere new
    (:func:`project_state_root`). An *empty* file, by contrast, is a real answer
    of "no baseline" and comes back as ``""``.

    **Known limit: a file at this key can be stale (#572).** The key is the
    project's resolved path, so a project that moves away and later returns finds
    its old subtree still here — nothing can sweep it in between (FEATURES.md) —
    holding the baseline blessed before it left, while the blessing it picked up
    in between is the one in ``state.json``. Preferring the file means that older
    pin wins for one resume, which re-stamps this key and heals it. Preferring the
    fresher-looking in-tree copy is *not* the fix: it is session-writable, so it
    would hand any session the silencing #498 closed. Arbitrating by sequence
    number needs a counterpart the session cannot forge, and at equal privilege
    there is none — the same wall as #571, reached by re-keying instead of
    tampering.

    Pure observation, so it degrades rather than raising: a state root this host
    cannot name, or a file it cannot read, both answer ``None`` and hand the
    decision to the in-tree copy. The write half raises — see
    :func:`write_trusted_config_digest` — and the split is the standard one
    (``platform_util.resolve_or_lexical`` states the doctrine). Degrading here
    costs at most one advisory warning; a resume that *aborts* because an
    advisory could not be read would be the worse failure, and the resume is
    about to resolve the same state root for its events channel anyway, where
    the error is owned and reported.

    **Deliberately not ``read_text``**, and for the same reason the write is
    ``follow_symlinks=False``: this file sits in a directory the driven session
    can reach (its parent is the ``BMAD_LOOP_EVENTS_DIR`` the engine exports), so
    the *shape* of what is at the path has to be established before any bytes are
    consumed. Degrading on a hostile path is not enough when the read itself is
    the weapon:

    * ``O_NONBLOCK`` + an ``S_ISREG`` check **on the descriptor**. Opening a FIFO
      for reading otherwise blocks until someone writes — indefinitely — and
      ``resume`` is a foreground command a human is waiting on, so a planted FIFO
      wedges the terminal rather than costing a warning. The check is on the fd,
      not the path, so it cannot be raced: ``fstat`` describes the object actually
      opened.
    * ``O_NOFOLLOW``, so the name is read rather than wherever it points.
    * At most :data:`_MAX_DIGEST_BYTES`. A link to an endless source
      (``/dev/zero``) reads forever otherwise, and raises ``MemoryError`` — not
      the ``OSError`` this promises never to leak. The cap removes the condition
      instead of absorbing it.

    The POSIX-only flags degrade to 0 on win32, which has neither FIFOs at these
    paths nor ``O_NOFOLLOW``; the size cap and the regular-file check carry there
    on their own. This mirrors ``tui.launch._read_ctl_window`` deliberately — same
    hazard, same shape, one idiom. It does **not** collapse empty to ``None`` the
    way that twin does: here the two are different answers (above).

    None of this makes the baseline tamper-*proof* — a session can still delete
    the file, and #571 carries that. It stops a tampered path from hanging or
    exhausting the orchestrator, which is a different and fixable harm."""
    try:
        path = config_digest_path_for(project, run_id)
    except (StateRootError, OSError, RuntimeError):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)  # win32: no CRLF translation on the raw fd
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _MAX_DIGEST_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def write_trusted_config_digest(project: Path, run_id: str, digest: str) -> None:
    """Stamp ``digest`` as this run's host-exec baseline, creating the state dir.

    Raises rather than degrading — a repair write, and a silently skipped stamp
    is the outcome hardest to detect later: the next resume reads no file and
    decides on the in-tree copy alone, which is the tree this baseline exists to
    police. The caller is starting or resuming a run and is about to resolve the
    very same state root for its events channel, so a root that cannot be named
    or written fails that run regardless; failing here just fails it sooner,
    before the pid lands.

    **Call this only after the run dir exists.** Creating the state dir is what
    makes this the earliest writer into it, and :func:`reconcile_orphan_state_dirs`
    reads its entries *before* the live run-dir names on the strength of run dirs
    being created strictly first — a state dir minted ahead of its run dir would
    look like an orphan to a ``clean`` racing the launch."""
    path = config_digest_path_for(project, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # follow_symlinks=False: a machine-minted record under a root whose path the
    # driven session is handed (BMAD_LOOP_EVENTS_DIR names its sibling), so a
    # planted link here must be replaced, never written through to whatever it
    # aims at. The trailing newline is for the operator who cats the file.
    atomic_write_text(path, digest + "\n", follow_symlinks=False)


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
    """Canonical project identity used by both tag writers and prune readers. The
    single source of normalization: both sides must route through this so symlinks
    and relative paths can't make a project look foreign to its own sessions.

    Hashing the resolved path makes every value safe by construction, on both
    transports a tag has to cross. It clears psmux's control line (#419), whose
    gate refuses any value the CLI->server hop would mangle — a UNC share whose
    name holds a space is refused verbatim, and that refusal left the session
    untagged, which is weak ownership twice over. It equally clears the listing
    round trip (#518): a hex digest holds nothing `str.splitlines()` breaks on,
    no tab, and no byte outside ASCII, so it can neither split a row nor fail the
    backends' strict decode.

    That subsumes the conditional percent-encoding this function briefly applied.
    Encoding answered only the listing half, so a path the listing could carry but
    the control line could not — the spaced UNC above — still went untagged. The
    compatibility objection encoding was shaped around, that rewriting every tag
    strands the ones already stored on live sessions and windows, is answered on
    the read side instead, by `accepted_tags`.

    16 hex characters are ample for one machine's project population.
    """
    return hashlib.sha256(os.fsencode(str(project.resolve()))).hexdigest()[:16]


def accepted_tags(project: Path) -> frozenset[str]:
    """Current digest plus the legacy resolved-path tag accepted during pruning.

    The legacy member is read-only compatibility for sessions and ctl windows that
    survive an upgrade; remove it once no path-tagged multiplexer state can remain.
    Returns the whole set rather than answering per tag so a read site resolves the
    project once per prune instead of once per session.

    The two shapes cannot collide into false ownership: a legacy tag is an absolute
    path, so it always holds a separator, while a digest is bare 16-hex.

    Deliberately two members and not three — a tag spelled with the `%enc%` prefix,
    from the window when this module encoded rather than hashed, is not accepted.
    Only a path the listing could not carry was ever spelled that way (one holding
    a line separator, or a byte invalid in the filesystem encoding), and that
    spelling never reached a release. An unaccepted tag reads as foreign, which
    skips the session rather than pruning it, so the edge is fail-safe and clears
    itself on the next tag write.
    """
    return frozenset({project_tag(project), str(project.resolve())})


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

    - tag proves this project (see accepted_tags): ours — prunable unless a
      provably-alive engine pid is running (covers finished/stopped/crashed *and*
      orphans whose run dir was deleted, since engine_liveness reads 'dead' with
      no pid).
    - tag is another project: skipped — never touched.
    - tag empty (untagged session): can't prove ownership, so fall back to the run
      dir — prunable only when the dir exists under this project and is dead;
      skipped when the dir is absent. Reachable when the tag write failed, when
      the option read degrades (session_options reads unset as "no answer", never
      as proof nothing was written), or on a session predating a working tag
      write — e.g. psmux path tags refused before the digest.
    """
    tags = session_project_tags()
    mine = accepted_tags(project)
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
            if tag not in mine:
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


def graceful_stop_requested(run_dir: Path) -> bool:
    """True when a graceful-stop request is pending for this run (its control file
    is present). The single definition of "requested" the engine checks at item
    boundaries and the CLI/TUI surface — a bare existence read, never raising."""
    return (run_dir / STOP_REQUEST_FILE).is_file()


def clear_graceful_stop(run_dir: Path) -> bool:
    """Consume a pending graceful-stop request, returning True iff one was present
    and removed. Never raises: a hard stop and a resume both call this to cancel a
    superseded request, and a missing file (already consumed by the engine, or
    never written) or an unremovable one must not wedge those paths. Uses the same
    win32 sharing-violation retry the atomic write pairs with."""
    try:
        retrying_unlink(run_dir / STOP_REQUEST_FILE)
    except OSError:
        # FileNotFoundError (nothing pending) or a genuine removal failure — either
        # way nothing was discarded, and the caller must not see an exception.
        return False
    return True


def request_graceful_stop(run_dir: Path) -> str:
    """Ask a live run to stop gracefully: finish the in-flight item (story ->
    dev/review/commit, or a sweep bundle through commit) cleanly, then finalize and
    stop — resumable, unlike the hard SIGTERM :func:`stop_run` delivers.

    Delivery is the :data:`STOP_REQUEST_FILE` control file, written atomically (tmp
    + ``atomic_replace``) so a concurrent engine read never sees a partial file.
    Never signals the process and never writes ``journal.jsonl`` (engine-owned
    single-writer). Returns a status token for the caller to message on:

    - ``"requested"`` — file written; a provably-live engine will honor it.
    - ``"already-pending"`` — a request was already on disk; left untouched so its
      original ``requested_at`` stands (idempotent — a second ask is a no-op).
    - ``"requested-unverifiable"`` — file written, but engine liveness read
      ``'unknown'`` (e.g. a win32 access-denied pid): the request stands and fires
      if an engine is in fact running; the caller warns that it can't confirm.

    Raises :class:`GracefulStopError` when the run has already finished (nothing to
    stop) or its engine is provably dead (no consumer — ``resume`` is the tool).
    """
    state = load_state(run_dir)
    if state.finished:
        raise GracefulStopError(f"run {run_dir.name} has already finished — nothing to stop")
    if graceful_stop_requested(run_dir):
        return "already-pending"  # keep the original request's timestamp
    liveness = engine_liveness(run_dir)
    if liveness == "dead":
        raise GracefulStopError(
            f"run {run_dir.name} has no live engine — a graceful stop request would "
            f"never be consumed; use `bmad-loop resume {run_dir.name}` to continue it"
        )
    path = run_dir / STOP_REQUEST_FILE
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps({"requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "graceful"})
    tmp.write_text(body, encoding="utf-8")
    atomic_replace(tmp, path)
    return "requested" if liveness == "alive" else "requested-unverifiable"


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

    # A hard stop always supersedes a pending graceful request — cancel it so a
    # later resume doesn't re-honor a stop the operator escalated past (covers the
    # signalled, force-kill, and mark-stopped fallback paths below alike).
    clear_graceful_stop(run_dir)

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


def live_session_may_be_ours(project: Path, run_id: str) -> bool:
    """True when a live ``bmad-loop-<id>`` session exists that this project cannot
    prove belongs to another one — the precondition of the removal guard below.

    Ownership is read exactly as :func:`prunable_sessions` reads it. A tag outside
    :func:`accepted_tags` proves the session foreign, and a *tagged* session carries
    its own ownership proof, so it does not need this project's run dir at all:
    answering False there keeps the guard off a removal that provably strands
    nothing. Untagged, or tagged as ours, answers True — neither can be ruled out
    as depending on this run dir, and only the untagged case is load-bearing.

    An observation, so it degrades rather than raising, and each read degrades in
    its own direction. A listing that cannot answer reads as "no session": that is
    already what the bundled backend returns for a missing multiplexer, a dead
    server or a failed query, and a guard that varied by backend would be worse
    than no guard. A tag that cannot be read is *not* proof the session is foreign,
    so it reads as untagged and the refusal stands — by then the listing has
    already established that a session is live.

    Both reads are caught explicitly because the seam permits a raise: only
    `pipe_pane` and `kill_session` are contractually best-effort, so an
    out-of-tree backend raises :class:`MultiplexerError` here where the bundled
    one returns empty (docs/adapter-authoring-guide.md). The listing is checked
    first, so the tag query only runs on a name collision."""
    name = session_name(run_id)
    try:
        if name not in mux_sessions():
            return False
    except MultiplexerError:
        return False
    try:
        tag = session_project_tags().get(name, "")
    except MultiplexerError:
        tag = ""  # unread is not proof of foreign
    return not tag or tag in accepted_tags(project)


def _refuse_live_session(project: Path, run_id: str, verb: str) -> None:
    """Backstop for #419: refuse to remove a run dir out from under a live session.

    Every caller's live guard is keyed on *engine pid* liveness, so an orphan —
    engine dead, agent session still alive in the multiplexer — passes all of them.
    That is the one state where the run dir is load-bearing: for an untagged
    session it is the only ownership proof :func:`prunable_sessions` can read, so
    removing it leaks the session (and its server) for the life of the machine.
    Refusing is a repair-path write failing loudly, per the module doctrine.

    Scoped to what it can justify: a session this project can prove is another
    one's does not block anything (see :func:`live_session_may_be_ours`). Refusing
    there would strand nothing and wedge every removal path — including `clean`,
    which has no override — for as long as the other project's run lives.

    Never a kill from here: a session name carries no project, so killing
    `bmad-loop-<id>` by name would tear down another project's live run whenever the
    two share a run id (reachable — `--run-id` is caller-supplied).

    The message names `bmad-loop cleanup` as the remedy but does not call it sound.
    `prune_sessions` proves ownership from the tag when there is one and falls back
    to *this same run dir* when there is not — the weak proof this guard exists to
    protect, so on the untagged case it can prune another project's session on a
    shared run id (#419's second edge, pinned by
    `test_prunable_sessions_claims_an_untagged_session_on_a_run_id_collision`).
    Hence the message asks the operator to confirm first: nothing available here can
    prove the session ours, and minting a proof that outlives the run dir is #419
    direction (2), not this guard."""
    if live_session_may_be_ours(project, run_id):
        raise LiveSessionError(
            f"run {run_id}: refusing to {verb} its directory while its agent session is "
            f"still live — for an untagged session this directory is the only ownership "
            f"proof a later prune has. Clear the session with `bmad-loop cleanup` first, "
            f"having confirmed it is this project's (`bmad-loop attach {run_id}`): an "
            f"untagged session is proven ours by this same directory, so a run id shared "
            f"with another project would prune theirs"
        )


def _discard_state_dir(project: Path, run_id: str) -> None:
    """Remove the run's out-of-tree control-plane counterpart, best-effort.

    The events channel (#494) lives outside the project tree, so removing a run
    dir no longer removes everything the run owns: without this every
    delete/archive would leak ``<state root>/<project>/<run-id>/`` forever. It
    lives here rather than in the CLI so every caller inherits it — `delete`,
    `archive`, `clean`, the TUI's removal actions and the engine's own
    finish-time reclamation alike.

    A **never-raise tail**, per the teardown doctrine (#139): the run dir is
    already gone by the time this runs, and failing the operator's delete over an
    unreachable state root would report a removal that in fact happened. Every
    catchable outcome is "the counterpart could not even be named" —
    :class:`StateRootError` for an environment with no derivable root, and
    ``OSError``/``RuntimeError`` for a project path the OS cannot canonicalize
    (:func:`project_tag` resolves before digesting). ``RuntimeError`` is not
    optional there: below 3.13 ``Path.resolve`` reports a symlink loop that way
    rather than as ``OSError`` (measured — 3.11 and 3.12 raise, 3.13 and 3.14
    return the unresolved path), so on two supported interpreters an ``OSError``
    -only guard lets a loop escape and breaks the promise in this paragraph.
    Removal failures are absorbed by ``ignore_errors``. Either way the orphan
    sweep in :func:`reconcile_orphan_state_dirs` is the backstop.

    Deliberately not called by :func:`trim_run_dir`: a trimmed run is still live
    on disk and resumable, and its control plane must outlive the scaffolding.
    """
    try:
        target = state_dir_for(project, run_id)
    except (StateRootError, OSError, RuntimeError):
        return
    shutil.rmtree(target, ignore_errors=True)


def delete_run(project: Path, run_dir: Path, *, force: bool = False) -> None:
    """Permanently remove a run directory. Callers enforce the engine-liveness
    guard; the session guard is enforced here (see :func:`_refuse_live_session`),
    which raises :class:`LiveSessionError` instead of removing.

    ``force`` is the operator's explicit override and skips that guard, accepting
    the leak on their own say-so. It deliberately does not kill the session
    instead — that would be unscoped, and this project cannot prove the session is
    its own (which is the whole defect). Trading a possible leak of our own session
    for a possible kill of someone else's is the wrong direction for an override."""
    if not force:
        _refuse_live_session(project, run_dir.name, "delete")
    shutil.rmtree(run_dir)
    # after the run dir, never before: a raise above leaves the run whole, and a
    # whole run keeps its control plane (see _discard_state_dir).
    _discard_state_dir(project, run_dir.name)


def archive_run(project: Path, run_dir: Path, *, force: bool = False) -> Path:
    """Compress a run dir into .bmad-loop/archive/<id>.tar.gz and remove the
    original. The tarball is written to a temp path then atomically replaced into
    place so a partial archive never appears. Callers enforce the engine-liveness
    guard; the session guard is enforced here (see :func:`_refuse_live_session`,
    and :func:`delete_run` for ``force``) and runs before the tarball is written,
    so a refusal leaves nothing behind.

    The tarball holds the run dir only, so since #494 an archive no longer carries
    the run's ``events/``: the channel moved out of the tree, and its files are
    transient completion signals the watcher has already consumed — the recorded
    decision accepts losing them from the archive. Everything an archive is read
    for later (state, journal, tasks, logs) is in the run dir and unaffected."""
    if not force:
        _refuse_live_session(project, run_dir.name, "archive")
    archive_dir = project / ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_dir.name}.tar.gz"
    # `with_name`, not `with_suffix`: the latter replaces only the LAST suffix, so
    # on `<id>.tar.gz` (stem `<id>.tar`) it produced `<id>.tar.tar.gz.tmp` — not the
    # name this docstring implies, and not one any cleanup could be written against.
    tmp = dest.with_name(dest.name + ".tmp")
    # #363: the guard, not a helper — the path is handed to `tarfile.open`, so there
    # is no payload for `atomic_write_*` to take. Nothing gitignores this directory:
    # init writes `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml`
    # and `_bmad/render/`, and `archive/` matches none of them. So a stranded temp
    # here is an untracked file holding `worktree_clean` False until a human removes
    # it — the same exposure `decisions._write_store`, `policy.write_mux_backend` and
    # `tui.settings.PolicyDoc.save` had. (Not the sweep's two `decisions.json`
    # writes, which look like the same fix but write under the ignored run dir.)
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(run_dir, arcname=run_dir.name)
        atomic_replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    shutil.rmtree(run_dir)
    _discard_state_dir(project, run_dir.name)  # same tail as delete_run
    return dest


# ------------------------------------------------------- reclaim / retention

# Heavy per-run scaffolding trimmed from a concluded run dir while the
# TUI-visible core (state.json, journal.jsonl, logs/, ATTENTION) is preserved,
# so the run still lists and renders in the dashboard. "worktrees" mirrors
# workspace.WORKTREE_DIRNAME; kept literal here to avoid an import cycle
# (workspace imports nothing from runs, but runs stays leaf-light on purpose).
#
# VERIFY_DIR is the retained verifier stdout/stderr store. It qualifies as heavy
# on the same measure as a worktree checkout: `[verify] stream_capture_kb`
# defaults to 256 KiB per stream, so a run accumulates up to 512 KiB per verify
# command per attempt, and nothing else ever reclaims it. Its journal records
# survive the trim and keep naming the files (`stdout_path`/`stderr_path`), which
# is the same bargain `worktrees` already makes — a trimmed run is a run you can
# still see and resume, not one you can still re-read every artifact of. Imported
# from the writer rather than re-spelled, so the reclaim cannot drift from the
# directory `Journal.write_verify_stream` actually creates.
_HEAVY_RUN_ENTRIES = ("worktrees", VERIFY_DIR)


def heavy_run_entries(run_dir: Path) -> list[Path]:
    """The paths :func:`trim_run_dir` would remove from ``run_dir``.

    Exists so a caller sizing the reclaim measures exactly what the trim takes.
    `clean` sums these before mutating (its estimate has to hold under
    --dry-run); reading the tuple through this function is what keeps that sum
    from silently going stale the next time an entry is added to it."""
    return [run_dir / name for name in _HEAVY_RUN_ENTRIES]


def _state_or_none(run_dir: Path):
    """Parsed run state, or None when it cannot be read — never classify (and so
    never reclaim) what you cannot positively read."""
    try:
        return load_state(run_dir)
    except Exception:  # unreadable/corrupt state ⇒ leave it alone
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


def _run_dir_names(project: Path) -> set[str] | None:
    """Every *directory name* under the runs dir, or ``None`` when that listing
    could not be taken.

    Deliberately not :func:`list_run_dirs`, which is ``state.json``-gated: this
    answers "does a run dir by this name exist", and a run whose ``state.json`` is
    missing or corrupt still owns its control plane. Gating on state.json would
    sweep the counterpart out from under exactly the run an operator is trying to
    recover.

    The two failures are distinguished because they mean opposite things. A
    *missing* runs dir is a real answer — no runs, so nothing is live — while an
    unreadable one answers nothing at all, and a sweep run against "no live names"
    would remove every state dir this project has. ``None`` is that second case.
    """
    try:
        return {entry.name for entry in os.scandir(project / RUNS_DIR) if entry.is_dir()}
    except FileNotFoundError:
        return set()
    except OSError:
        return None


def reconcile_orphan_state_dirs(project: Path, *, dry_run: bool = False) -> list[Path]:
    """Remove this project's out-of-tree control-plane dirs whose run dir is gone.

    The GC backstop for the events channel (#494). :func:`_discard_state_dir`
    removes the counterpart on every ordinary delete/archive, so this catches what
    that path could not: a run dir removed by hand or by an `rm -rf .bmad-loop`,
    a delete that ran before this version existed, and any tail that failed
    quietly. Without it the state root accumulates one dead subtree per run
    forever, on a path outside the project that no operator thinks to look at.

    Shaped like :func:`reconcile_orphan_worktrees`: enumerate on-disk truth,
    containment-test each path, remove with failures tolerated. Returns what was
    removed (or, under ``dry_run``, what would be).

    Every path is built from an entry name this function itself enumerated —
    never from a caller-supplied ref, which is what :func:`_is_path_escape`
    refuses on the ref-resolution path. Entries that are not real directories are
    skipped, symlinks included: a link is not a state dir we created, and
    reporting one swept would be a false count even where ``rmtree`` refuses it.
    The containment test then covers what ``is_symlink`` cannot — a Windows
    *junction* reads as a plain directory while ``resolve()`` follows it, so
    without the test ``rmtree`` would empty a target sitting outside the root.
    That case is POSIX-invisible, and the tests say so rather than claim it.

    Degrades to no-op rather than raising, in either direction: an underivable
    state root, an unreadable root, or an unreadable runs dir all sweep nothing.
    This is reclamation, not repair — leaving disk behind is the cheap outcome,
    and removing a live run's control plane is not.

    Both guards hold ``RuntimeError`` alongside ``OSError`` for the same reason
    :func:`_discard_state_dir` does: every path here is resolved (the project by
    :func:`project_tag`, then the root, then each entry), and below 3.13
    ``Path.resolve`` reports a symlink loop as ``RuntimeError``. A loop planted
    among the entries would otherwise escape a sweep whose whole contract is to
    degrade, and take the operator's ``clean`` down with it after its real work
    was already done.

    **The two reads are ordered, and the order is the whole race guard.** State
    entries are enumerated *before* the live run-dir names, because a run creates
    its run dir strictly before its state dir — ``compose_run`` builds the
    ``Journal`` (which mkdirs the run dir) and only then stamps the config digest
    (:func:`write_trusted_config_digest`, the earliest writer into the state dir
    since #498) and calls ``make_adapters``, whose ``SignalWatcher`` mkdirs the
    events dir alongside it. Reading entries first makes
    that ordering carry the guarantee: anything in ``entries`` had its state dir
    on disk at the first read, so its run dir was on disk *before* that, so the
    later ``live`` read is certain to contain it. Read the other way round, a run
    starting in the gap is missing from ``live`` and present in ``entries``, and
    an operator's ``clean`` deletes the control plane of a run that is starting
    right now — whose watcher then polls a primary that no longer exists, or
    simply never sees the Stop. A run dir that disappears *between* the reads is
    the opposite case and correctly swept: it is a real orphan by then.
    """
    try:
        root = project_state_root(project)
        entries = sorted(root.iterdir())
        root_res = root.resolve()
    except (StateRootError, OSError, RuntimeError):
        return []
    live = _run_dir_names(project)
    if live is None:
        return []
    handled: list[Path] = []
    for entry in entries:
        if entry.name in live or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            entry.resolve().relative_to(root_res)
        except (OSError, RuntimeError, ValueError):
            continue
        handled.append(entry)
        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)
    return handled


def trim_run_dir(run_dir: Path, *, dry_run: bool = False) -> list[Path]:
    """Delete heavy scaffolding (the ``worktrees/`` tree and the retained
    verifier stream store) from a concluded run dir, preserving its TUI-visible
    core so the run still appears in the dashboard with full status/journal/logs.
    Returns the paths removed.

    The run's out-of-tree control plane is deliberately left alone (see
    :func:`_discard_state_dir`): a trimmed run still exists and is still
    resumable, so its state dir has to outlive its scaffolding."""
    removed: list[Path] = []
    for p in heavy_run_entries(run_dir):
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
                # Route /bmad-build-auto via the spec's frontmatter status (decision
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
            except verify.FrontmatterWriteError as e:
                # The spec reads fine but carries `status:` in a shape no line
                # edit can move (a block scalar, a flow mapping, a value continued
                # on the next line). This used to be a silent no-op on a bool
                # nobody read: the re-drive was dispatched anyway, step-01 saw the
                # unchanged terminal status and routed the session to "ingest as
                # context, do not resume", and the story re-wedged with nothing on
                # the record explaining why. Abort here for the same reason as
                # below, with the remedy this cause actually has.
                raise RearmError(
                    f"cannot re-open story spec {spec_path} for the re-drive: {e} "
                    f"— the re-drive would repeat the wedge it is meant to clear"
                ) from e
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
    except Exception:  # nosec B110 - best-effort git read, must not fail re-arm
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
        except (OSError, UnicodeDecodeError, verify.FrontmatterWriteError) as e:
            # FrontmatterWriteError joins the tuple rather than getting its own
            # arm: the remedy is the same sentence ("fix the file"), and the
            # exception already says which shape it could not move. What matters
            # is that it aborts here — the stale-baseline hazard this block exists
            # to close is exactly what a swallowed write would leave behind.
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
        except Exception:  # nosec B110 - warn-only, must not fail re-arm
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
