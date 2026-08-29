"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import contextlib
import contextvars
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import devcontract, envvars, verify
from .adapters.multiplexer import MultiplexerError, get_multiplexer, mux_usable
from .frontmatter import auto_dev_baseline_of, parse_frontmatter, status_of
from .journal import STATE_FILE, VERIFY_DIR, Journal, load_state, save_state
from .model import PAUSE_ESCALATION, Phase, RunState, StoryTask
from .platform_util import (
    MAX_SEGMENT,
    UnconfinedWriteError,
    _mkstemp_beside,
    atomic_replace,
    atomic_write_bytes_confined,
    atomic_write_text_confined,
    create_exclusive_confined,
    has_parent_ref,
    is_absolute_path,
    is_link_like,
    names_tree_root,
    retrying_unlink,
    safe_segment,
)
from .process_host import ProcessHostError, get_process_host

RUNS_DIR = Path(".bmad-loop") / "runs"
ARCHIVE_DIR = Path(".bmad-loop") / "archive"
PID_FILE = "engine.pid"
# Cross-process channel for a stop request: a control file the requester (CLI/TUI)
# writes and the engine reads. The body carries a `mode` — "graceful" or "hard".
#
#   graceful (`stop --graceful`): finish the in-flight item, then finalize and stop.
#     Honored at item boundaries only; resumable.
#   hard (`stop`): stop now. Lodged by stop_run *before* it signals, honored by the
#     engine at item boundaries and mid-session by the adapter wait loop.
#
# The file exists because signals are not a portable stop channel: there is no
# SIGUSR1 on Windows/psmux, and an inter-process SIGTERM is never delivered to a
# native-Windows engine at all, so the win32 "graceful" terminate is a no-op that
# only ever burned _STOP_WAIT_S into a force-kill (#319). SIGTERM remains the POSIX
# fast path — the file is what makes a stop work everywhere else. The engine stays
# the single writer of journal.jsonl, and the single *consumer* of this file;
# requesters only ever write it, adapters only ever read it.
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
    """A live run could not be stopped — the engine honored neither channel (the
    lodged stop request nor SIGTERM) and its pid's identity can no longer be
    verified, so force-killing would risk an unrelated (reused) pid. The caller
    surfaces this rather than silently marking stopped."""


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
# How long stop_run lets a force-kill settle before deciding it failed. A kill that
# returns cleanly is not proof of death — win32 shells `taskkill /F /T` with
# `check=False`, so a refused kill raises nothing — but the pid can also linger for a
# moment after a delivered SIGKILL, and `is_alive` is a bare existence probe that
# reads a not-yet-reaped process as alive. Long enough to outlast that, short enough
# that a genuinely surviving engine is still noticed while the operator waits.
_KILL_CONFIRM_S = 0.5


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


def all_run_dirs(project: Path) -> list[Path] | None:
    """Every run dir under the runs root — ``state.json`` or not — oldest first,
    or ``None`` when the listing could not be taken.

    The ungated counterpart to :func:`list_run_dirs`, and the one to ask when the
    question is "does a run still own its control plane" rather than "which runs
    can I read". A run whose state.json was removed or corrupted still holds a
    live ``engine.pid``, so the gated view walks straight past exactly the run an
    operator is mid-recovery on — the hazard :func:`_run_dir_names` documents,
    whose set this wraps rather than re-listing.

    ``None`` is an unreadable runs root and means *nothing was learned*, which is
    not the same answer as the empty list a missing root gives. Callers that act
    on "no live runs" have to tell those apart; see :func:`_run_dir_names`.
    """
    names = _run_dir_names(project)
    if names is None:
        return None
    root = project / RUNS_DIR
    return sorted(root / name for name in names)


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
    # Confined to the state root (#593): a machine-minted record under a root
    # whose path the driven session is handed (BMAD_LOOP_EVENTS_DIR names its
    # sibling), so a planted link here must be replaced, never written through to
    # whatever it aims at. Refusing a link at the FINAL component was not enough —
    # `mkstemp(dir=...)` and `os.replace`'s destination still resolved every
    # directory above by name, and the `mkdir` on the line above ACCEPTS a
    # symlinked directory, so a link planted at either session-reachable component
    # (`<project tag>/`, `<run id>/`) survived the setup step and redirected both
    # the temp and the published stamp. `state_root()` is the one component the
    # anchored walk starts from rather than checks, and it is a host fact this
    # process derives — not a path any session names. The trailing newline is for
    # the operator who cats the file.
    atomic_write_text_confined(path, digest + "\n", confine_root=state_root())


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
    runs dir — it is absolute/drive-qualified, climbs with ``..``, names the runs
    dir itself rather than anything inside it, or carries a path separator of
    either flavour. Sub-check of the run-id charset rather than `is_valid_run_id`
    itself: a run dir created by an older version (or by hand) may bear a name we
    would no longer mint, and must stay addressable.

    `names_tree_root` restores this site to the three-guard pairing every sibling
    already spells (`policy.py`, `adapters/profile.py`, `plugins/manifest.py`); it
    was the only member of the family omitting it (#480). It closes the spellings
    that recompose to the runs *root* instead of a run in it. ``""`` and ``"."``
    join to it exactly — measured here, both `runs / ""` and `runs / "."` *are*
    the runs dir — so a `state.json` lying at that root made the exact branch
    below hand `delete_run` the whole runs tree to `rmtree`. ``"..."``, ``".. "``
    and ``"   "`` are the Win32 half of the same rule (cited, not measurable on
    POSIX): the trim of trailing periods and spaces leaves ``..`` or nothing, so
    they name `.bmad-loop/` or the runs dir there while both pure pathlib flavours
    keep them as ordinary one-segment names.

    Addressability is unharmed: skipping the exact branch only defers to partial
    matching, and a legacy dir named ``"..."`` is still enumerated by
    `list_run_dirs` and still matched by its own spelling.

    `names_win32_alias`, the family's fourth member, is deliberately NOT applied
    here — it would make a legacy run dir named ``NUL`` or ``run. `` permanently
    unaddressable, which is the one thing this guard exists to prevent. Refusing
    to *mint* such a name is `is_valid_run_id`'s job, and it already does it with
    a `safe_segment` identity check."""
    return (
        is_absolute_path(ref)
        or has_parent_ref(ref)
        or names_tree_root(ref)
        or "/" in ref
        or "\\" in ref
    )


def resolve_run_dir(project: Path, ref: str) -> Path:
    """Full or partial run id -> its run dir. An exact id wins outright;
    otherwise a partial matches when the trailing segment starts with `ref` or
    the full id ends with `ref` (run ids are date-prefixed, so the tail is what
    distinguishes them). Raises RunRefError on no match / ambiguity.

    The exact branch recomposes a path from the raw ref, so it is skipped for any
    ref that could escape the runs dir (`bmad-loop delete ../../x` would otherwise
    rmtree an outside directory that happens to hold a state.json). Such a ref
    falls through to partial matching, which can only ever yield a name
    `list_run_dirs` enumerated — and so cannot escape.

    An EMPTY ref is refused outright rather than deferred: `""` is a prefix and a
    suffix of every name, so partial matching reads it as a wildcard — harmlessly
    ambiguous with two runs, but silently resolving the sole run of a one-run
    project, which handed `bmad-loop delete ""` that run. No addressability is
    lost (no directory can be named `""`); every other escape spelling keeps the
    partial fallback so a legacy dir named `"..."` stays matchable by its own
    spelling."""
    if not ref:
        raise RunRefError("empty run ref: it would match every run, never name one")
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
    (identity-checked, so a reused pid reads as dead). Mirrors :func:`liveness`
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
    :func:`engine_liveness` and :func:`liveness`, so both read the pid file once.
    A probe failure degrades to ``'unknown'``, never a false ``'dead'``."""
    host = get_process_host()  # ProcessHostError (misconfig) propagates, not masked as unknown
    try:
        return host.liveness_of(pid, identity)
    except Exception:
        return "unknown"


# ------------------------------------------------- run inventory / classification


# Run statuses reported by `bmad-loop list` and the dashboard.
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
STOPPED = "stopped"
CRASHED = "crashed"
INTERRUPTED = "interrupted"
UNKNOWN = "unknown"

_StatSig = tuple[int, int, int]


def _stat_sig(path: Path) -> _StatSig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    # st_ino joins (mtime_ns, size): the engine rewrites state.json atomically
    # (temp + os.replace), so every write lands on a fresh inode. That catches a
    # same-size rewrite within one coarse mtime tick (e.g. WSL2 drvfs, or any fast
    # rewrite on a low-resolution mtime) that (mtime_ns, size) alone would miss and
    # serve stale from cache.
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def liveness(run_dir: Path) -> str:
    """'alive' | 'dead' | 'unknown' for the engine that owns run_dir.

    engine.pid is authoritative (written at run/sweep/resume start, never
    deleted). Legacy runs without one fall back to the per-run agent session —
    but that session only exists while an agent session runs, so its absence
    proves nothing: 'unknown', never falsely dead. Pid checks are local-only;
    runs on other hosts always come back 'unknown'.
    """
    pid, identity = read_pid_identity(run_dir)
    if pid is None:
        return _session_liveness(run_dir.name)
    # Probe the pid we just read (shared body with engine_liveness) rather than
    # re-reading it, so a non-atomic pid rewrite can't split the two reads and flash
    # a false 'dead' between "pid present" here and a re-read seeing an empty file.
    try:
        return probe_liveness(pid, identity)
    except ProcessHostError:
        # A misconfigured host (bad BMAD_LOOP_PROCESS_HOST) stays a hard error on
        # CLI decision paths, but the display layer must degrade, not crash: the
        # dashboard poll worker has no except and would take the whole app down.
        return "unknown"


def _session_liveness(run_id: str) -> str:
    # An absent multiplexer / dead query proves nothing about a legacy run, so the
    # only positive signal is a live session; everything else is 'unknown'.
    mux = get_multiplexer()
    if not mux_usable(mux):  # forced-aware, like every other observer gate
        return "unknown"
    try:
        return "alive" if mux.has_session(session_name(run_id)) else "unknown"
    except (OSError, MultiplexerError):
        # The seam raises MultiplexerError (not OSError) on a backend failure; a
        # dead query proves nothing about a legacy run, so degrade to 'unknown'
        # rather than crashing the TUI poll.
        return "unknown"


def _classify(finished: bool, paused: bool, stopped: bool, crashed: bool, run_dir: Path) -> str:
    if finished:
        return FINISHED
    if paused:
        return PAUSED
    # a deliberate stop leaves a dead pid — check it before liveness so it does
    # not read as INTERRUPTED (a crash).
    if stopped:
        return STOPPED
    # a recorded crash leaves a dead pid too — surface it as a distinct CRASHED
    # before liveness, where it would otherwise read as a generic INTERRUPTED.
    if crashed:
        return CRASHED
    live = liveness(run_dir)
    if live == "alive":
        return RUNNING
    if live == "dead":
        return INTERRUPTED
    return UNKNOWN


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    run_dir: Path
    run_type: str
    started_at: str
    status: str
    paused_stage: str = ""  # RunState.paused_stage when PAUSED, else ""; drives the badge
    stopping: bool = False  # a graceful stop is pending (control file present) while RUNNING


# state.json path -> (stat sig, header fields tuple)
_HeaderFields = tuple[str, str, bool, bool, bool, bool, str]
_header_cache: dict[Path, tuple[_StatSig, _HeaderFields]] = {}


def discover_runs(project: Path) -> list[RunInfo]:
    """One RunInfo per run dir, oldest first; [] when the runs dir is missing.

    Parses only the state.json header fields (cached on stat); a state file
    that fails to parse yields status 'unknown' rather than crashing — it is
    transient, the engine writes atomically.
    """
    out: list[RunInfo] = []
    for run_dir in list_run_dirs(project):
        state_path = run_dir / STATE_FILE
        sig = _stat_sig(state_path)
        cached = _header_cache.get(state_path)
        if sig is not None and cached is not None and cached[0] == sig:
            run_type, started_at, finished, paused, stopped, crashed, paused_stage = cached[1]
        else:
            try:
                doc = json.loads(state_path.read_text(encoding="utf-8"))
                run_type = str(doc.get("run_type", "story"))
                started_at = str(doc.get("started_at", ""))
                finished = bool(doc.get("finished", False))
                paused = doc.get("paused_reason") is not None
                stopped = bool(doc.get("stopped", False))
                crashed = bool(doc.get("crashed", False))
                paused_stage = str(doc.get("paused_stage") or "")
            except (OSError, json.JSONDecodeError):
                out.append(RunInfo(run_dir.name, run_dir, "?", "", UNKNOWN))
                continue
            if sig is not None:
                _header_cache[state_path] = (
                    sig,
                    (run_type, started_at, finished, paused, stopped, crashed, paused_stage),
                )
        status = _classify(finished, paused, stopped, crashed, run_dir)
        # paused_stage is advisory: only meaningful while the run is actually PAUSED
        # (a resumed run keeps the last stage in state until it re-pauses/finishes).
        stage = paused_stage if status == PAUSED else ""
        # A pending graceful stop is the control file's presence, but only while an
        # engine is still around to honor it — RUNNING or UNKNOWN (an unverifiable
        # pid still consumes the file). The engine discards the file at the stop
        # boundary, so a lingering file on an already-concluded run is not "stopping":
        # STOPPED/FINISHED/CRASHED classify before liveness, so they never read UNKNOWN.
        stopping = status in (RUNNING, UNKNOWN) and (run_dir / STOP_REQUEST_FILE).is_file()
        out.append(RunInfo(run_dir.name, run_dir, run_type, started_at, status, stage, stopping))
    return out


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


def lock_path_for(data_path: Path) -> Path:
    """The advisory-lock sidecar for a mutable data file:
    ``<state root>/locks/<sha256(resolved path)[:16]>-<basename>.lock``.

    Out of the repository, deliberately, and NOT the ``<file>.lock`` sibling the
    obvious reading of #286 asks for. The deferred-work ledger is a *tracked*
    file by design, and both :func:`verify.commit_story` and
    :func:`verify.finalize_commit` stage with ``git add -A``: a lock beside it
    would be swept into the engine's own commits, and the git-add shield that
    would otherwise hide it covers linked worktrees only. Under the state root
    the sidecar is never git-visible at all, so no exclusion machinery has to be
    kept correct for it.

    Keyed on the **resolved** path so the identity of the lock is the identity of
    the file rather than of the spelling used to reach it: a symlinked and a
    direct path to one ledger rendezvous on one lock (without which the two
    spellings would exclude nobody), two worktrees' in-tree ledgers are different
    files and correctly get independent locks, and several projects pointed at a
    shared external artifact dir land on one lock, which is where the real
    contention is. The basename is appended for debuggability only — a human
    reading ``ls`` of the locks dir should see which file a sidecar guards — and
    carries no meaning for exclusion, which rides the digest.

    Pure: no directory is created here, because
    :func:`~bmad_loop.platform_util.file_lock` mkdirs the parent when it opens
    the lock. May raise :class:`StateRootError` when the environment names no
    usable state root (see :func:`state_root`); the caller fails rather than
    silently locking somewhere else.
    """
    resolved = data_path.resolve()
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:16]
    return state_root() / "locks" / f"{digest}-{resolved.name}.lock"


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


# The run dir of the OUTERMOST engine in this call stack (#319). A nested auto-sweep
# runs synchronously in its parent's thread but mints its own run id and dir, so its
# adapters would poll a control file no operator ever writes to: `bmad-loop stop
# <parent-id>` lodges in the parent's dir. This carries the owning run dir down to
# them. A ContextVar, mirroring `engine._run_depth`, because the nesting it tracks is
# same-thread by construction; set once by the outermost `Engine.run()` and reset by
# token, so a later top-level run in the same process is never poisoned.
_owner_run_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bmad_loop_owner_run_dir", default=None
)


def set_owner_run_dir(run_dir: Path) -> contextvars.Token[Path | None]:
    """Claim ``run_dir`` as the owning run for this call stack. Returns the token the
    caller must hand to :func:`reset_owner_run_dir` from a ``finally``."""
    return _owner_run_dir.set(run_dir)


def reset_owner_run_dir(token: contextvars.Token[Path | None]) -> None:
    """Release the claim made by :func:`set_owner_run_dir`."""
    _owner_run_dir.reset(token)


def owner_run_dir() -> Path | None:
    """The outermost engine's run dir, or None outside any run — which is what a
    standalone adapter (tests, probes) reads, so callers fall back to their own."""
    return _owner_run_dir.get()


def graceful_stop_requested(run_dir: Path) -> bool:
    """True when *some* stop request is pending for this run — either mode. A bare
    existence read of the control file, never raising and deliberately never parsing.

    Every consumer wants exactly that existence question, not the mode: the
    ``stopping`` projection and the TUI badge (a run with a hard request lodged is
    stopping too), the ``--graceful`` idempotency check (a lodged hard request means
    a *stronger* stop already stands — "already-pending" is the right answer), the
    stories done-checkpoint skip, and auto-sweep suppression. Only ``status``'s
    ``graceful_stop_pending`` field is mode-exact; it calls
    :func:`read_stop_request_mode` instead."""
    return (run_dir / STOP_REQUEST_FILE).is_file()


def read_stop_request_mode(run_dir: Path) -> str | None:
    """The mode of this run's pending stop request: ``"hard"``, ``"graceful"``, or
    ``None`` when none is pending.

    ``None`` means *absent*, and only absent — it is returned for
    ``FileNotFoundError`` alone. Everything else about a file that is *present*
    reads ``"graceful"``: a modeless body (every pre-#319 writer and test fixture
    wrote one — this is the back-compat pin), unparseable or non-object JSON, and a
    transient read failure such as the win32 sharing violation a concurrent
    ``atomic_replace`` raises mid-write.

    Leaning graceful on every ambiguity is load-bearing, not defensive habit. A
    misread graceful costs at most one more item before the run stops; a spurious
    ``"hard"`` would abort a live session — so a torn read must never be able to
    produce one."""
    return _stop_request_mode_of(run_dir / STOP_REQUEST_FILE)


def _stop_request_mode_of(path: Path) -> str | None:
    """The parse half of :func:`read_stop_request_mode`, split out so
    :func:`consume_stop_request` can answer for the file it *took* rather than for
    whatever currently answers to the channel name."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # present but unreadable this tick (sharing violation, undecodable bytes) —
        # answer for the file we know is there, never escalate on a failed read.
        return "graceful"
    try:
        body = json.loads(raw)
    except ValueError:
        return "graceful"
    if isinstance(body, dict) and body.get("mode") == "hard":
        return "hard"
    return "graceful"


def _project_of_run_dir(run_dir: Path) -> Path:
    """The project root a run directory hangs under, for confining writes into it.

    Derived rather than passed because the stop-request channel is addressed by
    run directory alone: `stop_run` resolves a run reference and never holds the
    project separately. :func:`run_dir_for` is the only builder of these paths
    and spells them ``project / RUNS_DIR / run_id``, so the root sits exactly
    ``len(RUNS_DIR.parts)`` levels above the run's own directory — the arithmetic
    tracks `RUNS_DIR` rather than hard-coding 2, so moving the runs tree moves
    this with it.

    A path too shallow to have that ancestor is not one this module built.
    Refusing with :class:`UnconfinedWriteError` rather than letting `parents`
    raise `IndexError` is the load-bearing part: `stop_run` degrades on `OSError`
    so that a failed lodge still signals the run, and an `IndexError` there would
    abort the stop before it ever signalled."""
    depth = len(RUNS_DIR.parts)
    parents = run_dir.parents
    if len(parents) <= depth:
        raise UnconfinedWriteError(f"{run_dir} is not shaped like a run directory")
    return parents[depth]


def _write_stop_request(run_dir: Path, mode: str) -> None:
    """Lodge a stop request of ``mode`` on the control-file channel, written
    atomically so a concurrent engine read never sees a partial body.

    The atomic replace *is* the supersede: writing ``"hard"`` over a pending
    ``"graceful"`` escalates the request in one step, with no window in which
    nothing is pending for the engine to find.

    That is the only direction this function arbitrates, and the only one it may:
    ``stop_run`` shares it and its escalation must stay unconditional. The channel is
    otherwise last-writer-wins, so the *reverse* — a graceful write landing on a
    pending hard request and downgrading it — is refused by a different writer
    entirely: :func:`_create_stop_request`, which lodges the graceful mode with
    ``O_CREAT | O_EXCL`` so "is one pending?" and "lodge mine" are a single atomic
    step. Splitting the two directions across two functions is what lets this one
    stay an unconditional replace.

    Goes through :func:`platform_util.atomic_write_text` rather than a hand-rolled
    ``tmp + atomic_replace``, for the reason ``operatoractions`` was migrated under
    #379: this is the one control file with genuinely *concurrent* writers — two
    ``stop`` invocations against the same run, in either mode — and a fixed ``.tmp``
    sibling is exactly what two writers of the same key collide on. Interleaved,
    both stage over one name and the loser's ``os.replace`` raises
    ``FileNotFoundError`` after the winner's consumed it; on the hard path that
    would abort ``stop_run`` *before* it ever signals. A ``mkstemp`` temp per writer
    removes the collision: the last replace wins and neither writer errors.

    Refusing a link at the control file preserved what the bare ``os.replace``
    did — it never dereferenced this destination — and matches what the file is:
    machine-minted control state under a run dir a driven session can reach. The
    write is now confined to the project root (#593), because that refusal
    covered only the final component: every directory above it was still looked
    up by name, so a link planted at ``.bmad-loop/`` — or at ``runs/``, or at the
    run's own directory — aimed both the temp and the publish wherever it
    pointed. The file still lands at ``mkstemp``'s ``0600`` instead of
    ``0644 & ~umask``, since no-follow never inherited a mode either; nothing
    reads it cross-user.

    No ``require_writable_target``: this is not an operator-curated file but a
    channel two ``stop`` invocations race on, and its whole contract above is
    that the stronger request always lands."""
    body = json.dumps({"requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode})
    atomic_write_text_confined(
        run_dir / STOP_REQUEST_FILE, body, confine_root=_project_of_run_dir(run_dir)
    )


def _create_stop_request(run_dir: Path) -> bool:
    """Lodge a *graceful* request only if none is pending; False when one already is.

    ``O_CREAT | O_EXCL`` is the arbitration. It makes "is a request pending?" and
    "lodge mine" one atomic step against the destination name, so a hard request
    landing at any instant either already exists — we refuse, leaving it standing —
    or replaces what we wrote, which is escalation, the direction
    :func:`_write_stop_request` owns. A re-read immediately before an unconditional
    replace could only ever *narrow* that window (~1.3ms on a journalling
    filesystem, where the fsync dominates); this closes it.

    Graceful-ONLY by construction, and that is what makes the non-atomic body safe.
    The bytes are written *into* the created file rather than replaced in, so a
    concurrent reader can catch it empty — and :func:`read_stop_request_mode`
    answers ``"graceful"`` for a present-but-unparseable body, which is the very
    mode being written. The invariant that matters is untouched: a torn read must
    never produce ``"hard"``, so a hard writer must keep the atomic replace.

    Refuses a planted symlink rather than following it — ``O_EXCL`` never
    dereferences — which is stricter than the ``follow_symlinks=False`` replace it
    replaces. That refusal covers only the FINAL component, though, so the create
    goes through :func:`platform_util.create_exclusive_confined` (#593): a link
    planted at ``.bmad-loop/``, ``runs/`` or the run's own directory was still
    resolved by name and aimed the request outside the project, exactly the hole
    the confined :func:`_write_stop_request` next door already closed. The
    anchored create keeps the exclusive arbitration this function is built on;
    an unreachable parent raises ``UnconfinedWriteError``.

    A failed write is deliberately NOT rolled back, and that is load-bearing rather
    than sloppy. ``unlink`` resolves a *name*, not the inode this call created, so a
    rollback here would delete whatever occupies the path at that moment — including
    a ``"hard"`` request a concurrent ``stop`` escalated onto it while this write was
    in flight. That is a ``hard -> absent`` drop, the one descent the mode lattice
    :func:`consume_stop_request` documents must never happen, and on native Windows
    it would silently withdraw the only channel that can stop the engine. Guarding it
    is not available: an "unlink only if still my inode" step does not exist as one
    atomic operation, and both check-then-unlink shapes measure *worse* than no guard
    at all — the check moves the decision earlier and the destructive act later by
    its own cost, shifting the window rather than narrowing it (inode compare 1.39x,
    mode compare 2.30x, over a rendezvous-synchronised escalation sweep on btrfs).

    What a failed write leaves behind is a short or empty body, which
    :func:`read_stop_request_mode` reads as ``"graceful"`` — exactly the mode this
    function was asked to lodge, for the one caller (``stop --graceful``) that an
    operator drove. It does not wedge the channel: a later graceful ask answers
    "already-pending", a later *hard* stop supersedes it unconditionally, and
    ``stop --cancel-graceful`` or ``resume`` withdraws it. Leaving a graceful request
    standing is the bounded direction this channel already leans on everywhere else."""
    body = json.dumps({"requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "graceful"})
    path = run_dir / STOP_REQUEST_FILE
    try:
        fd = create_exclusive_confined(path, confine_root=_project_of_run_dir(run_dir))
    except FileExistsError:
        return False  # a request is already pending — a planted link included
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return True


def clear_graceful_stop(run_dir: Path) -> bool:
    """Consume a pending stop request of *either* mode, returning True iff one was
    present and removed. Never raises: the engine calls this the moment it honors a
    request, a resume calls it to discard a stale one, and stop_run calls it on the
    paths where nothing is left alive to read what it lodged — a missing file
    (already consumed) or an unremovable one must not wedge any of them. Uses the
    same win32 sharing-violation retry the atomic write pairs with."""
    try:
        retrying_unlink(run_dir / STOP_REQUEST_FILE)
    except OSError:
        # FileNotFoundError (nothing pending) or a genuine removal failure — either
        # way nothing was discarded, and the caller must not see an exception.
        return False
    return True


def consume_stop_request(run_dir: Path) -> str | None:
    """Take the pending request off the channel and answer the mode of the very file
    removed, or ``None`` when none was pending.

    The reader-side counterpart of :func:`_create_stop_request`'s
    ``O_CREAT | O_EXCL``: the rename *is* the consume, so "what mode is pending?"
    and "take it" cannot disagree. A read followed by an unlink can, and the gap is
    not academic — a concurrent ``stop`` escalating to ``"hard"`` in between is
    deleted unread while the caller routes on the stale ``"graceful"`` it already
    holds.

    Only that direction can lose anything, because the mode lattice is monotone:
    the graceful writer refuses to overwrite an existing request and the hard writer
    only ever writes ``"hard"``, so ``absent < graceful < hard`` until consumed. A
    stale ``"hard"`` read is therefore always still true; a stale ``"graceful"`` may
    not be.

    Monotone requires that no writer *descends* either, which is why
    :func:`_create_stop_request` has no rollback on a failed write: an ``unlink``
    keyed on the path rather than the inode it created is a ``hard -> absent`` drop,
    and it would put a second way to lose a hard request in a *writer* — leaving the
    three read-then-unlink sites in ``engine.py`` that rely on this argument resting
    on something untrue.

    Re-reading the mode immediately before the unlink does NOT fix this, and is a
    trap worth naming: measured over 4000 injected races it made the loss *more*
    likely, not less (164 -> 929 swallowed), because the extra read lengthens the
    interval an escalation has to land in. Narrowing a window is not closing it —
    only one atomic step is.

    A hard request lodged *after* the take is a new request against a run already
    stopping. It stays at the canonical name for ``run()``'s finally to discard and
    journal as ``stop-request-discarded`` — a record, not a silent loss."""
    src = run_dir / STOP_REQUEST_FILE
    taken = run_dir / (STOP_REQUEST_FILE + ".consumed")
    try:
        atomic_replace(src, taken)
    except FileNotFoundError:
        return None
    except OSError:
        # Could not take it (read-only dir, a sharing violation past its retries).
        # Leave it on the channel and answer from the canonical name: the next
        # boundary re-asks, which is strictly better than losing the request.
        return read_stop_request_mode(run_dir)
    try:
        return _stop_request_mode_of(taken)
    finally:
        with contextlib.suppress(OSError):
            retrying_unlink(taken)


def request_graceful_stop(run_dir: Path) -> str:
    """Ask a live run to stop gracefully: finish the in-flight item (story ->
    dev/review/commit, or a sweep bundle through commit) cleanly, then finalize and
    stop — resumable, unlike the hard stop :func:`stop_run` delivers.

    Delivery is the :data:`STOP_REQUEST_FILE` control file, written atomically by
    :func:`_write_stop_request` so a concurrent engine read never sees a partial file.
    Never signals the process and never writes ``journal.jsonl`` (engine-owned
    single-writer). Returns a status token for the caller to message on:

    - ``"requested"`` — file written; a provably-live engine will honor it.
    - ``"already-pending"`` — a request was already on disk; left untouched so its
      original ``requested_at`` stands (idempotent — a second ask is a no-op). The
      token is mode-blind, and the pending request is not necessarily graceful: a
      *hard* one sits there at rest whenever a `stop` could not prove the engine
      dead, and one can also land while this call is in flight. A stronger stop
      stands either way and must not be downgraded — so callers message this token
      as a *stop request*, never as a graceful one (#319).
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
    # The write IS the check. The existence test at the top of this function is
    # separated from here by a pid-file read, a liveness probe and (formerly) a
    # mkstemp and an fsync — measured at ~1.3ms median on btrfs, wide enough for a
    # concurrent `stop` to lodge `"hard"` in between — and the channel is
    # last-writer-wins, so an unconditional replace here would silently *downgrade*
    # it and cost the abort the operator asked for. A re-read just before the replace
    # narrows that window; a create-if-absent removes it, because there is no longer
    # a gap between deciding and writing. "already-pending" is the same answer the
    # check at the top gives, and the right one either way: a lodged hard request is
    # a *stronger* stop already standing. Two concurrent *graceful* asks resolve the
    # same way, which is the documented idempotency — the first one's timestamp
    # stands. The escalation direction is untouched and stays unconditional.
    if not _create_stop_request(run_dir):
        return "already-pending"
    return "requested" if liveness == "alive" else "requested-unverifiable"


def stop_run(run_dir: Path) -> bool:
    """Stop a live run. Returns False if it was already finished.

    The request is delivered two ways at once, and the engine wins whichever race
    it can: a ``mode: hard`` :data:`STOP_REQUEST_FILE` is lodged *first*, then the
    engine is signalled. SIGTERM is the POSIX fast path — the handler stops the run
    within the tick. The file is what makes the stop work where the signal cannot
    land: a native-Windows engine never receives an inter-process SIGTERM, so before
    #319 every Windows stop burned the full grace window into a blind force-kill.
    Now the engine reads the file at its next item boundary, or mid-session in the
    adapter wait loop, and performs its own teardown either way.

    That ordering is the whole point: lodging before signalling means the engine can
    never exit the signal path having missed a request that was only written after.

    Either way the engine stays the single writer of `stopped` (it marks the run,
    kills its in-flight agent window, and exits). Falls back to an external kill +
    mark when there is no live engine pid, it is a legacy run, or it does not exit
    in time. A wedged engine that ignores both channels past the grace window is
    force-killed — but only while we can still prove the pid is the same process we
    signalled (a pid-reuse guard); otherwise we raise StopRunError rather than risk
    killing an unrelated process.

    The lodged file is consumed by whoever settles the run: the engine when it
    honors the request, or this function on the paths where nothing is left alive to
    read it. Both exceptions to that turn on the same question — did we ever *prove*
    the engine dead? Where we did not, the file stays lodged, because it is then the
    only channel that can still stop it: the StopRunError refusal below (we decline
    to force-kill an unverifiable pid), and the ``engine_may_live`` paths where the
    signal or the kill was refused outright rather than racing us to exit.
    """
    state = load_state(run_dir)
    if state.finished:
        return False

    # Lodge the hard request before signalling. The atomic replace also supersedes a
    # pending *graceful* request in the same step: the operator escalated past it, and
    # a stronger request must never leave a window where nothing at all is pending.
    #
    # Degrade rather than abort when the lodge fails (read-only run dir, ENOSPC — and
    # the run's own session logs tee into this very directory, so a run can fill the
    # disk that then blocks stopping it). The doctrine's unit is the *repair*, not the
    # syscall: this stop is "delivered two ways at once" per the docstring above, so
    # failing the whole thing because one of two redundant channels failed would leave
    # a run alive that the pre-#319 signal path could still have killed. Keep the
    # signal, and stay loud where it actually matters — see the refusal branch below.
    try:
        _write_stop_request(run_dir, "hard")
        lodged = True
    except OSError:
        lodged = False

    host = get_process_host()
    pid, identity = read_pid_identity(run_dir)  # identity recorded at run start, not sampled now
    if pid is not None and identity is not None and not host.alive_and_ours(pid, identity):
        # the pid we recorded is already gone, or was reused by an unrelated
        # process before stop_run ran — never signal a stranger; mark stopped below.
        pid = None
    # Whether this call ever proved the engine dead. Only a confirmed death licenses
    # the fallback below to discard the request we lodged: while the engine may still
    # be running, that file is the one channel left that can stop it (on native
    # Windows it is the *only* one), so retracting it would throw away the very
    # repair #319 exists to deliver.
    engine_may_live = False
    if pid is not None:
        try:
            host.terminate(pid)
        except ProcessLookupError:
            pid = None  # provably gone — the fallback's discard is correct
        except (PermissionError, OSError):
            # We could not signal it and it was `alive_and_ours` a moment ago, so it
            # may well still be running (an EPERM mismatch, or a win32 taskkill that
            # errored). Skip the wait — there is nothing to wait for — but keep the
            # request lodged so the engine can still stop itself off the file.
            engine_may_live = True
            pid = None
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
                except ProcessLookupError:
                    pass  # raced us to exit — that's the outcome we wanted
                except (PermissionError, OSError):
                    # Unlike ESRCH above, this is the opposite news: the process is
                    # there and we were refused. Keep the request lodged.
                    engine_may_live = True
                else:
                    # A kill that returned cleanly is not a death certificate — on
                    # win32 `force_kill` shells `taskkill /F /T` with `check=False`,
                    # so a refused kill raises nothing at all, and win32 is the
                    # platform this whole channel exists for. Confirm rather than
                    # infer, since the answer decides whether we discard the request.
                    # Let it settle first: a delivered SIGKILL is immediate but the
                    # pid can linger a moment before it is reaped, and reading that
                    # as "still alive" would strand the file on the ordinary
                    # wedged-engine path.
                    confirm_deadline = time.monotonic() + _KILL_CONFIRM_S
                    while host.is_alive(pid) and time.monotonic() < confirm_deadline:
                        time.sleep(_STOP_POLL_S)
                    engine_may_live = host.is_alive(pid)
            else:
                # Refusing to kill leaves the hard request lodged on purpose: if that
                # pid *is* still our engine, the file is the only channel left that
                # can stop it, and discarding it here would retract a request the
                # operator made while we decline to enforce it ourselves.
                #
                # That reasoning only holds while the lodge succeeded. If it did not,
                # nothing at all is pending and we are declining to force-kill on top
                # of that — the operator must be told, or they are left believing a
                # request is in flight that was never written.
                if lodged:
                    raise StopRunError(
                        f"run {run_dir.name}: engine pid {pid} honored neither the "
                        "lodged stop request nor SIGTERM, and its identity can no "
                        "longer be verified; refusing to force-kill a possibly-reused "
                        "pid"
                    )
                raise StopRunError(
                    f"run {run_dir.name}: the stop request could not be written to "
                    f"the run directory and engine pid {pid} did not honor SIGTERM; "
                    "its identity can no longer be verified, so it will not be "
                    "force-killed. No stop is pending — free space in the run "
                    "directory and retry, or stop the process yourself"
                )
    # the engine clears its agent window itself, but kill the session as a backstop
    # in case it died before tearing it down. Ahead of everything below, because both
    # exits from here need it — an engine that honored the stop and died before
    # tearing its window down leaks the session just as surely as one we killed.
    kill_session(run_dir.name)
    state = load_state(run_dir)
    if state.stopped:
        # The engine honored the stop and is gone, and its own `run-stop` already
        # stands in the journal. Stamping `fallback=True` on top would describe an
        # engine that did its own teardown as one that had to be stopped from
        # outside. This check deliberately sits out here rather than inside the
        # `pid is not None` arm it used to live in: every path that clears `pid`
        # early — a pid that is no longer ours, a `terminate` that raced the exit
        # and got `ProcessLookupError`, a refusal that could not verify it — skipped
        # it and fell straight through to the append. The plainest case needs no race
        # at all: `stop` on a run a previous `stop` already stopped (`stopped` is set,
        # `finished` is not, so the guard at the top does not fire).
        #
        # It normally consumes the file on the way out; clear it belt-and-braces so a
        # run that is later resumed can never find our request still lodged and
        # re-stop at its first item. Safe on the `engine_may_live` paths too: a
        # written `stopped` *is* the engine reporting it honored the request, so
        # there is no live consumer left to strand.
        clear_graceful_stop(run_dir)
        return True

    # Neither channel was delivered: nothing is lodged, and we never proved the engine
    # dead. This is the one outcome `stop` must not report as success — the operator is
    # left believing a request is in flight that was never written, while an engine we
    # could not signal keeps mutating the project. The pid-reuse guard above already
    # refuses for its own path; these are its siblings, and the only reason they stayed
    # quiet is that they clear `pid` and skip that block. Not a regression — on the
    # merge-base this was the state of *every* refused signal, because `stop_run` cleared
    # the request as its first statement — but the earlier decision to report success
    # rested on the request being retained, which is exactly what did not happen here.
    #
    # Placement is load-bearing, twice over. It sits *after* the session backstop
    # because refusing to report a stop is no reason to leak the window, and *after* the
    # `state.stopped` return because a run the engine already honored must not be
    # reported as a failure. Journal the attempt before raising: the `run-stop` append
    # below is skipped, and an unrecorded stop attempt is its own trap.
    if engine_may_live and not lodged:
        Journal(run_dir).append("run-stop-undelivered", pid=pid)
        raise StopRunError(
            f"run {run_dir.name}: the stop request could not be written to the run "
            "directory and the engine could not be proved dead, so no stop is pending. "
            "Its agent session was killed as a backstop. Free space in the run directory "
            "and retry, or stop the process yourself"
        )

    # Fallback: no live engine (or it never confirmed). Mark it stopped here. Discard
    # the request first — nothing is left alive to consume it, and a file outliving
    # the run it asked to stop is a trap for the next resume.
    #
    # Unless we never actually proved that. Where the engine may still be running,
    # the request stays lodged and the stop is genuinely still in flight: the engine
    # honors the file at its next poll and writes `stopped` itself. Discarding it here
    # would leave a live engine with no channel left while we report the run stopped —
    # the stale-request trap above is the lesser of the two, and it only bites a run
    # that is later resumed, which this one cannot be until that engine exits.
    if not engine_may_live:
        clear_graceful_stop(run_dir)
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


def _refuse_uncontained_run_dir(project: Path, run_dir: Path, action: str) -> None:
    """Refuse to remove anything but a direct child of ``project``'s runs dir.

    The containment half of #480, and deliberately independent of how the ref was
    spelled: :func:`_is_path_escape` gates the *string* an operator typed, this
    gates the *path* the two destructive writes are about to hand `shutil.rmtree`.
    Both are wanted. `delete_run` and `archive_run` are module-public and take a
    `run_dir` outright, so a caller that composed one by some route other than
    :func:`resolve_run_dir` — the TUI's selection, a record read back from disk, a
    call site not yet written — never passes the ref guard at all.

    ``run_dir_for`` is the sole builder of these paths, so recomposing one from
    the basename and comparing is exactly the "is a direct child" question: the
    runs root itself, a nested grandchild, and anything outside the project all
    differ from what it returns. Comparing against the rebuild rather than
    walking `parents` keeps this tracking `RUNS_DIR` the way
    :func:`_project_of_run_dir` does. The rebuild has one blind spot the name
    check closes: ``.name`` of ``runs / ".."`` is ``".."`` and the rebuild
    reproduces it verbatim, so the lexical equality holds while `rmtree` would
    resolve it to ``.bmad-loop`` itself. pathlib drops ``"."`` at parse so only
    the ``".."`` spelling survives to here; ``"."`` is refused anyway rather than
    reasoned about.

    The link walk below the equality check refuses a REDIRECTED spelling of a
    contained path: with ``.bmad-loop``, ``runs`` or the run dir itself replaced
    by a symlink (or, on Windows, an unelevated ``mklink /J`` junction — why this
    is :func:`is_link_like` and not ``is_symlink``), the rebuild is lexically
    identical while `rmtree` follows the redirect and removes a tree outside the
    project. A planted redirect is this module's live threat class (see the #591
    notes in :func:`archive_run`). The walk stops short of ``project`` — the
    operator's own argument, and a project addressed through a symlinked home is
    legitimate — and covers only the orchestrator-owned levels under it. It is
    check-then-act, not fd-anchored like `journal.py`'s writes: `resolve()` is
    banned here (it can raise on a WSL-UNC host — `tests/conftest.py`'s
    ``refuse_to_resolve``), `tarfile` cannot take a dir fd at all, and the racer
    that could re-plant between check and rmtree is a live session, which the
    guard below this one refuses anyway.

    Raises rather than degrading — observation may degrade, a repair write must
    not: there is no partial `rmtree` to fall back to, and declining quietly would
    report a removal that never happened. :class:`UnconfinedWriteError` is the
    shape-refusal this module already raises for the same class of mistake (see
    :func:`_project_of_run_dir`), and being an ``OSError`` it lands in the
    handling callers already have for a removal that failed."""
    if run_dir.name in (".", "..") or run_dir_for(project, run_dir.name) != run_dir:
        raise UnconfinedWriteError(
            f"refusing to {action} {run_dir}: not a run directory under {project / RUNS_DIR}"
        )
    node = run_dir
    while node != project:
        if is_link_like(node):
            raise UnconfinedWriteError(
                f"refusing to {action} {run_dir}: {node} is a symlink or junction"
            )
        parent = node.parent
        if parent == node:  # anchored: never walk past the filesystem root
            break
        node = parent


def delete_run(project: Path, run_dir: Path, *, force: bool = False) -> None:
    """Permanently remove a run directory. Callers enforce the engine-liveness
    guard; the session guard is enforced here (see :func:`_refuse_live_session`),
    which raises :class:`LiveSessionError` instead of removing.

    ``force`` is the operator's explicit override and skips that guard, accepting
    the leak on their own say-so. It deliberately does not kill the session
    instead — that would be unscoped, and this project cannot prove the session is
    its own (which is the whole defect). Trading a possible leak of our own session
    for a possible kill of someone else's is the wrong direction for an override.

    The containment guard runs first and is NOT under ``force``: an override is
    the operator accepting a leaked session, never a licence to rmtree a path
    outside the runs dir."""
    _refuse_uncontained_run_dir(project, run_dir, "delete")
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
    for later (state, journal, tasks, logs) is in the run dir and unaffected.

    Containment (see :func:`_refuse_uncontained_run_dir`) is checked ahead of both,
    for the reason the session guard runs early: a refusal must leave no archive
    directory and no tarball behind."""
    _refuse_uncontained_run_dir(project, run_dir, "archive")
    if not force:
        _refuse_live_session(project, run_dir.name, "archive")
    archive_dir = project / ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_dir.name}.tar.gz"
    # #363: the guard, not a helper — the path is handed to `tarfile.open`, so there
    # is no payload for `atomic_write_*` to take. Nothing gitignores this directory:
    # init writes `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml`
    # and `_bmad/render/`, and `archive/` matches none of them. So a stranded temp
    # here is an untracked file holding `worktree_clean` False until a human removes
    # it — the same exposure `decisions._write_store`, `policy.write_mux_backend` and
    # `tui.settings.PolicyDoc.save` had. (Not the sweep's two `decisions.json`
    # writes, which look like the same fix but write under the ignored run dir.)
    #
    # #591: staged through `_mkstemp_beside` — the atomic writers' own exclusive
    # `0600` create (binary-mode on win32), under a fresh unpredictable name per
    # attempt. A fixed name made a temp stranded by a kill, or planted at the
    # guessable spelling, deny every later attempt as `FileExistsError`; the
    # truncate-and-reuse it replaced followed a planted symlink instead. mkstemp's
    # exclusivity still never opens a name something else holds, and the name being
    # this process's own mint is what licenses the cleanup unlink below. It sits
    # outside the `try` on purpose: a create that fails has staged nothing to
    # clean up.
    fd, tmp_name = _mkstemp_beside(dest)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                tar.add(run_dir, arcname=run_dir.name)
            # Flushed and fsynced before the publish, and unlike the rest of this
            # family that is not about staleness but about data loss: `shutil.rmtree`
            # below removes the only other copy of the run, so a crash with the
            # tarball still in page cache destroys it outright. Ordered inside the
            # fdopen context so the gzip trailer `tar.close()` just wrote is included.
            raw.flush()
            os.fsync(raw.fileno())
        atomic_replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)  # provably ours: mkstemp minted the name
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


def _unlink_redirect(p: Path) -> None:
    """Remove a link-like entry itself, never what it points at.

    ``shutil.rmtree`` REFUSES a directory symlink by design (it would otherwise
    delete the target's contents), and under ``ignore_errors=True`` that refusal
    is swallowed — so trimming a planted redirect reported success while leaving
    the link on disk. Unlink covers a POSIX symlink and a win32 file symlink;
    ``rmdir`` is the win32 arm, where ``DeleteFileW`` rejects a directory symlink
    or junction and ``RemoveDirectoryW`` drops the reparse point without
    following it. Best-effort to match the ``rmtree`` beside it: a trim is
    reclamation, and a run dir we cannot fully reclaim is not a reason to abort
    the whole `clean`."""
    try:
        p.unlink()
    except OSError:
        with contextlib.suppress(OSError):
            p.rmdir()


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
        link = is_link_like(p)
        if not (p.exists() or link):
            continue
        removed.append(p)
        if dry_run:
            continue
        if link:
            _unlink_redirect(p)
        else:
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


def task_spec_path(task: StoryTask, state: RunState) -> Path:
    """The recorded spec path, re-anchored on the tree it was persisted relative to.

    `StoryTask._serialized_worktree_path` (`model.py`) persists a worktree-local spec
    RELATIVE to the mounted worktree root, and `from_dict` reads it back raw. Resolving
    that against the process cwd is not merely unreachable — it is actively wrong:
    `bmad-loop resolve` runs from the project root, where the MAIN CHECKOUT carries the
    same `_bmad-output/specs/...` layout, so a bare `Path(task.spec_file)` names the main
    checkout's copy of the story spec. `is_file()` then answers True, `confine_root`
    accepts it (it genuinely is under `project`), and the status flip and the baseline
    re-stamp both land on a file the run never used while the worktree's real spec is
    left on the escalated attempt's sha.

    Absolute paths pass through: a spec outside the worktree is persisted verbatim.

    Raises `ValueError` on an empty `task.spec_file` rather than documenting a
    precondition nothing enforces: `Path("")` is `.`, so `root / raw` would answer the
    ROOT DIRECTORY — a write target, not a spec. Every caller already guards; this is
    public now, so the next one gets an exception instead of a silent tree root.
    """
    if not task.spec_file:
        raise ValueError("task_spec_path requires a non-empty task.spec_file")
    raw = Path(task.spec_file)
    if raw.is_absolute():
        return raw
    return task_spec_root(task, state) / raw


def task_spec_root(task: StoryTask, state: RunState) -> Path:
    """The tree a `task.spec_file` is anchored on — and confined to.

    One definition backs both halves because they must not disagree: the root
    `task_spec_path` resolves against and the `confine_root` the writers validate the
    result against are the same claim about which tree owns this spec. Passing
    `state.project` while resolving against the worktree does not REFUSE the mismatch —
    `set_frontmatter_status`, `devcontract.strip_auto_run_result` and
    `verify.set_frontmatter_field` all answer an out-of-root path by silently dropping
    to the plain no-follow write, losing the confined arm's O_NOFOLLOW walk of the
    parent components (#593) with no signal at all.

    Worktrees normally resolve under `<project>/.bmad-loop/runs/...`, so the confined
    arm is taken by construction rather than by luck — no policy or env var can
    relocate them. The one escape is that `workspace.open_unit_workspace` stores a
    `.resolve()`d path: a symlinked `.bmad-loop`, `runs` or `worktrees` lands the spec
    outside `project`, and before this anchor moved that silently degraded all three
    writes.

    A worktree that CANNOT confine the anchored path yields the project instead. An
    absolute `spec_file` beside a set `worktree_path` is precisely the out-of-mount
    shape: `model._serialized_worktree_path` keeps a path verbatim exactly when
    `relative_to(worktree_path)` raises, so the two spellings did not share a prefix.
    Returning the worktree there would name a root that can never contain the path
    `task_spec_path` passes through — the three `_atomic_write_spec` writers would
    silently take the plain no-follow arm (losing #593's O_NOFOLLOW walk) and
    `_restore_rearmed_spec`, which calls the confined writer directly, would RAISE.

    The project can often confine it, and where nothing can, the write lands on the arm
    it already took. NOT unconditionally, though, and the exception is graded by
    `test_task_spec_root_refuses_a_spec_the_project_cannot_reach`: `_atomic_write_spec`
    picks its arm on a LEXICAL `is_relative_to`, but the confined arm it picks then
    walks the components below the root and refuses a redirect (`open_dir_confined` on
    POSIX, `path_is_confined` on win32). A spec that is lexically under the project but
    reached THROUGH a symlinked component — a symlinked `_bmad-output`, say — therefore
    moves from a succeeding plain no-follow write to `UnconfinedWriteError`, which
    `rearm_escalation` re-raises as `RearmError`. That is a re-arm which used to
    complete and now aborts, so this arm is not the pure improvement an earlier draft of
    this docstring claimed.

    It is kept anyway, because the alternative is worse. Predicting the walk here (gate
    the arm on `path_is_confined` and fall back to the worktree) makes the ROOT depend
    on filesystem state: `path_is_confined` answers False for a component it cannot
    probe, so a spec whose parent does not exist yet would anchor on the worktree and
    the same spec would anchor on the project once the directory appeared. A confine
    root that moves under a `mkdir` is not a definition. The refusal is also the correct
    posture on its own terms — #593 exists to refuse writes through a link on a path
    that came from a session-driven scan — so this trades a narrow, LOUD failure for a
    deterministic rule, and the failure names the path in its message.

    The test is the same lexical `is_relative_to` the writer gates on, so the root and
    the writer's ARM SELECTION agree by construction; only the walk below can still
    refuse. Deliberately not canonicalized: `_spec_is_shared_with_the_redrive` answers a
    DIFFERENT question (is this spec reachable by the re-drive) and canonicalizes for
    it, but matching that here would diverge from the gate this value is measured
    against and change writes that are correct today.
    """
    worktree = task.worktree_path
    if not worktree:
        return Path(state.project)
    raw = Path(task.spec_file or "")
    if raw.is_absolute() and not raw.is_relative_to(worktree):
        return Path(state.project)
    return Path(worktree)


def _spec_is_shared_with_the_redrive(state: RunState, task: StoryTask) -> bool:
    """True when an isolated unit's recorded spec lives outside BOTH checkouts, so the
    re-arm's status flip survives the worktree's disposal and the re-drive reads it.

    The case: artifact dirs configured outside the project tree. `ProjectPaths.rebased`
    leaves those exactly where they are ("configured outside the project tree; doesn't
    move") — they are SHARED across checkouts, not per-worktree — so the spec the dev
    session reported resolves to one file that every worktree sees. The re-drive reads it
    back through `verify.resolve_spec_path`, whose absolute branch passes the value
    through untouched, and `engine._dispatched_spec_for_attempt` then accepts it because
    the rebased `implementation_artifacts` is still that same external directory.

    Both roots are load-bearing, and neither implies the other:

    - INSIDE the worktree — the file the fresh mount destroys. Unreachable.
    - inside the PROJECT but outside the worktree — the main checkout's copy. The write
      lands, but the re-drive cannot use it: under isolation `workspace.paths` is rebased
      onto the fresh worktree, so `verify.spec_within_roots` measures the main
      checkout's path against worktree-local roots and rejects it. Unreachable, and this
      is the one shape the worktree test alone would wrongly exempt.
    - outside both — the shared artifact dir above. Reachable.

    (The two are not nested: worktrees normally sit under `<project>/.bmad-loop/runs/`,
    but `workspace.open_unit_workspace` stores a `.resolve()`d path, so a symlinked
    `.bmad-loop` puts the mount outside the project.)

    The recorded spelling opens the question but does not answer it.
    `StoryTask._serialized_worktree_path` persists a spec RELATIVE whenever it sits
    under the mounted worktree and verbatim (absolute) otherwise, so an absolute value
    on a task that HAS a worktree is the only shape that can be shared — but that
    relativize is a LEXICAL `relative_to` against the same `worktree_path` read here, so
    all an absolute value proves is that the two spellings did not share a prefix. A
    spec reported through a symlink or a `..` segment sits inside the worktree and is
    persisted absolute all the same, and answering "shared" for it would suppress the
    warning on a spec that really is destroyed with the worktree.

    So containment is decided on the CANONICAL paths, and a host that cannot canonicalize
    one of them answers "not shared". That degrade is the safe direction and the reason
    this does not use `resolve_or_lexical`: its fallback is `absolute()`, which does not
    fold `..`, so a spec spelled through either checkout would come back looking external
    and go silent — trading a wrong warning for no warning at all."""
    raw = Path(task.spec_file or "")
    if not task.worktree_path or not raw.is_absolute():
        return False
    try:
        # the house pair — `resolve()` raises RuntimeError, not OSError, for a symlink
        # loop on the 3.11/3.12 floor
        real = raw.resolve()
        return not real.is_relative_to(Path(task.worktree_path).resolve()) and not (
            real.is_relative_to(Path(state.project).resolve())
        )
    except (OSError, RuntimeError):
        return False


def _redrive_base_ref(state: RunState, task: StoryTask) -> str:
    """The ref whose committed tree the re-drive will actually read this unit's spec
    from: the run's PINNED `target_branch` for an isolated unit, ``HEAD`` otherwise.

    Not `HEAD` in both cases, because the isolated re-drive never reads the main
    checkout's working ref. `engine._finish_inflight` discards the escalated worktree
    and its branch and `_run_story` mounts a replacement, and
    `workspace.open_unit_workspace` cuts that fresh branch from the `base` it is handed
    — `worktree_flow.run_isolated` passes `state.target_branch`, pinned once at run
    start so resume keeps targeting the same branch. An operator who checks out another
    branch in the main checkout while the escalation is paused therefore moves `HEAD`
    off the tree the re-drive reads, in either direction: a correction committed on the
    now-current branch is invisible to the re-drive, and one committed on the target
    branch is invisible to `HEAD`.

    That mattered once `rearm-spec-write-unreachable` began holding the resume
    (`rearm_holds_the_resume`): reading the wrong ref does not merely mis-word a
    warning, it either resumes a re-drive that re-wedges on the target branch's
    terminal status, or holds a resume whose work is already committed where the
    re-drive will find it.

    The two guards are the same proxy the caller already uses. `task.worktree_path` is
    how this file recognizes an isolated unit at all (`task_spec_root`,
    `_spec_is_shared_with_the_redrive`) — every isolated escalation carries a mounted
    one. An empty `target_branch` beside it is a MISSING value, not a divergent one:
    `ensure_target_branch` pins the field before any worktree mounts, so only a
    state.json predating it can reach here, and that shape degrades to exactly the ref
    it read before — the same migration `restamp_code_root` gives an unrecorded root.
    Answering ``""`` instead would hold the resume on a per-configuration constant, the
    failure the record's narrowing exists to avoid.
    """
    if task.worktree_path and state.target_branch:
        return state.target_branch
    return "HEAD"


def spec_reaches_the_redrive(task: StoryTask, state: RunState) -> bool:
    """Whether an edit to this task's spec survives to the re-drive that reads it.

    The other half of `task_spec_path`'s answer. That one says WHICH file the run's own
    tooling writes; this says whether that file still exists by the time the re-drive
    reads it. They differ exactly under isolation: `engine._finish_inflight` discards
    the mount, so a worktree-local spec is destroyed with it, while a spec in an
    artifact dir configured outside the project tree is shared across checkouts and
    survives (`_spec_is_shared_with_the_redrive` carries that argument in full).

    Public because `resolve.build_context` needs it for the same reason
    `rearm_escalation` does: the context hands a human and an agent a `spec_file` to
    edit, and an edit to a doomed copy is worse than no edit — it looks like it landed.
    """
    return not task.worktree_path or _spec_is_shared_with_the_redrive(state, task)


def _restore_rearmed_spec(
    spec_path: Path, original: bytes | None, task: StoryTask, state: RunState
) -> None:
    """Put back the bytes a re-arm FOUND on the spec, for the aborts that can fire after
    a write has already landed.

    `rearm_escalation` holds an invariant its own refusals depend on: an aborted re-arm
    leaves the spec byte-identical, so the escalation stays armed and the human can fix
    the file and re-run resolve. TWO of its four refusals earn that by SEQUENCING alone —
    the flip's read-back check and the `FrontmatterWriteError` arm both raise before
    `devcontract.strip_auto_run_result` runs, which is why that strip is deliberately
    ordered after them, and `set_frontmatter_status` decides it cannot move a `status:`
    before it writes anything. The other two cannot be sequenced out of the hazard, and
    both call this:

    * The baseline re-stamp needs `task.baseline_commit` from the advance, and the
      advance must itself run after the spec block (a just-cleared stories sentinel would
      otherwise be captured into `baseline_untracked` as phantom pre-existing residue).
    * The `(OSError, UnicodeDecodeError)` arm spans BOTH spec helpers, and the strip is
      the later one — a fault raised inside it is raised after the flip published.

    By the time either can fail, the status flip has landed and `save_state` has not — so
    the abort would otherwise leave the run's task ESCALATED against a spec already
    flipped to the re-drive's status and (for the re-stamp) stripped of the terminal
    `## Auto Run Result` the next resolve session reads as its context. That is exactly
    the "one edit nothing else records" the sequencing exists to prevent.

    Writes only what it can prove it changed. `original` is `None` when the spec was
    unreadable before the first write (there is then nothing to restore, and nothing
    could have been written either), and a spec that is gone or unreadable NOW is not a
    state this undo can improve — recreating a file another process removed would fight
    a concurrent actor rather than restore this function's own edit. Bytes equal to
    `original` mean nothing landed, so nothing is rewritten and the mtime is left alone.

    Byte-verbatim and CONFINED, matching the writes it undoes: `atomic_write_text_confined`
    would re-encode and translate newlines, so a CRLF spec would come back subtly
    different from the file this re-arm found, and an unconfined write would drop the
    `O_NOFOLLOW` walk of the parent components (#593) that every other write to this path
    takes. A restore that itself fails RAISES rather than degrading — the spec is then
    half-written and only the operator can settle it, which is the loudest thing this can
    be. `UnconfinedWriteError` is an `OSError`, so the one arm covers both.
    """
    if original is None:
        return
    try:
        if spec_path.read_bytes() == original:
            return
    except OSError:
        return
    try:
        atomic_write_bytes_confined(spec_path, original, confine_root=task_spec_root(task, state))
    except OSError as e:
        raise RearmError(
            f"cannot restore {spec_path} after a failed re-arm "
            f"({e.__class__.__name__}: {e}) — the spec carries this re-arm's status flip "
            "and has lost its `## Auto Run Result` section, while the story is still "
            "escalated; restore the spec from git, then re-run resolve"
        ) from e


def _committed_spec_status(state: RunState, task: StoryTask) -> str:
    """The spec's status as COMMITTED in the tree the re-drive reads, or ``""`` when
    unprovable.

    Under isolation the re-drive reads the committed spec and never a working-tree
    write (see `rearm_escalation`'s note on `rearm-spec-write-unreachable`), so this is
    the value that decides whether the operator still has anything to do. Anchored on
    `state.code_root` — the same tree the baseline advance reads — at the ref
    `_redrive_base_ref` names, which is the run's pinned `target_branch` for an isolated
    unit rather than that tree's current `HEAD`.

    Degrades to ``""`` on every uncertainty: a spec recorded absolute (nothing names
    its position in the tree), an absent or non-blob path at that ref, a non-UTF-8 blob,
    or any `GitError` — which includes the project simply not being a repository, and a
    `target_branch` the code root no longer carries. ``""`` never equals a target
    status, so the caller's record still fires. Suppression therefore requires PROOF
    that the work is already done, and the non-repo case stays non-fatal, as the story's
    Boundaries require.

    The absolute arm is narrower than it looks: the caller has already answered the one
    absolute shape whose write the re-drive DOES read — the shared external spec — with
    `_spec_is_shared_with_the_redrive`. What still reaches here is an absolute spelling
    of a path inside one of the two checkouts, which is genuinely unreachable and
    genuinely unprovable, so degrading it to a warning is the right answer, not a gap.
    """
    raw = Path(task.spec_file or "")
    if not task.spec_file or raw.is_absolute():
        return ""
    try:
        blob = verify.file_bytes_at_revision(
            state.code_root, _redrive_base_ref(state, task), raw.as_posix()
        )
    except verify.GitError:
        return ""
    if blob is None:
        return ""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return status_of(parse_frontmatter(text))


def restamp_code_root(run_dir: Path, repo_root: Path) -> str | None:
    """Re-point a paused run's persisted code-root mirror at `repo_root` — the tree
    the caller is about to act in — and return the warning an operator must see when
    that MOVED a root the run had recorded (`None` when it already agreed, or when the
    run predates the field).

    Exists because `rearm_escalation` reads that mirror OUT OF PROCESS
    (`RunState.code_root`) and has no `ProjectPaths` to consult, while `repo_root:` is
    re-read from config.yaml by every process that arms an engine. `cli._resume_paused_run`
    folds the same re-stamp into the one `save_state` that also carries the policy
    snapshot and the config digest — this is the seam for the surfaces that re-arm
    BEFORE they resume (`cli.cmd_resolve`, `TuiApp._do_rearm`), where that write lands
    too late to aim the re-arm.

    The compare is exact and uncanonicalized, matching resume's: both sides are
    `str(paths.repo_root)` off `bmadconfig.load_paths`, which resolves every member or
    raises, so they are spelled the same way whenever they name the same tree. An empty
    recorded root is a MISSING value, not a divergent one — a state.json written before
    the field existed — so it is migrated silently and reported as no move.

    The message names neither tree, like resume's: what an operator needs is that the
    run has changed repositories, and the paths are the half that would put an
    attacker-controlled string on their terminal.
    """
    state = load_state(run_dir)
    new = str(repo_root)
    if state.repo_root == new:
        return None
    moved = bool(state.repo_root)
    state.repo_root = new
    save_state(run_dir, state)
    if not moved:
        return None
    return (
        f"run {run_dir.name}: the code root in _bmad/bmm/config.yaml has changed since "
        "this run started — the re-drive works in the tree configured now, while the "
        "baselines, preserve refs and branches this run already recorded name objects "
        "in the previous one. Restore the previous `repo_root:` value if you did not "
        "intend the move."
    )


def rearm_escalation(
    run_dir: Path, story_key: str | None = None, *, restore_patch: str | None = None
) -> str:
    """Re-arm an escalation-paused story so the next resume re-drives it.

    Flips the escalated task out of its terminal ESCALATED phase back to
    PENDING — which makes `_finish_inflight` reset the tree to the story's
    baseline and re-run it (clean rebuild) against the now-corrected frozen
    spec. The baseline itself is advanced to the CODE TREE's current HEAD
    (`state.code_root`, which is `paths.repo_root` — the tree the dev writer
    stamps from and the proof-of-work gate measures, and the same directory as
    `state.project` in every configuration without a `repo_root:` override) and
    the untracked snapshot refreshed, so commits and files the resolve session
    produced count as the rebuild's starting point, not as attempt debris to
    roll back. Strips the escalated attempt's stale `## Auto Run Result`
    section so the re-drive cannot read as terminal from its first save, and
    sets the spec's frontmatter status so step-01 routes to the right stage.
    Does NOT clear the pause; the caller resumes the run separately.

    Two consequences of the reset are load-bearing and easy to undo by accident:

    - `task.generation` is bumped, because `attempt` returning to 0 would
      otherwise let the re-drive re-mint a session id byte-equal to one the
      abandoned attempt already recorded (#705). `task.sessions` is deliberately
      NOT cleared — a second resolve cycle reads that run-dir audit trail — so
      the id is what has to change.
    - The spec's `baseline_revision` is re-stamped on BOTH legs, and only when the
      advance above actually RAN — `advanced` records that both git reads succeeded,
      not that HEAD changed, so a resolve session that committed nothing still
      re-stamps (with the same sha, harmlessly). What it will not do is re-stamp
      after a FAILED advance (see the block that does it for why each half of that
      is the way it is).

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
    # A new generation of this task. `attempt` going back to 0 (and the next
    # dispatch bumping it to 1) would otherwise re-mint a session task_id
    # byte-equal to one the abandoned attempt already recorded, and
    # `Engine._resumable_session` — matching that id over the append-only
    # `task.sessions`, which this function deliberately does NOT clear — would
    # replay the abandoned verdict for the fresh attempt (#705). Bumped BEFORE any
    # dispatch, so the id is unique from the re-drive's first session onward.
    task.generation += 1
    task.review_cycle = 0
    task.followup_reviews_spent = 0  # human-resolved re-drive gets a fresh damping budget
    task.defer_reason = None
    task.rearmed = True  # resume-time recovery notice describes a clean rebuild,
    # not a failed attempt (engine._finish_inflight clears it once the rebuild runs)
    # Always (re)assign the latch: a None restore_patch clears a stale one left by
    # a prior restore attempt the human then chose to redo from scratch.
    task.restore_patch = restore_patch

    # The bytes this re-arm found on the spec, for `_restore_rearmed_spec`. Declared out
    # here because the baseline re-stamp that consumes it sits in a SECOND
    # `if task.spec_file:` block, past the advance it depends on.
    spec_before: bytes | None = None
    if task.spec_file:
        spec_path = task_spec_path(task, state)
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
            # A WORKTREE-LOCAL spec's writes below land in the unit's worktree
            # (`task_spec_path`) — which the re-drive destroys before reading anything.
            # A re-armed task (phase PENDING, `defer_reason` cleared, and no resumable
            # session because `generation` was just bumped) falls to
            # `engine._finish_inflight`'s final arm, which calls `discard_worktree` and
            # lets `_run_story` mount a fresh one. The re-driven session then resolves
            # its spec through `verify.resolve_spec_path(task.spec_file,
            # workspace.paths)` (`engine._dispatched_spec_for_attempt`), and under
            # isolation `workspace.paths` is rebased onto that FRESH worktree, which
            # checks out TRACKED files only. So the re-drive reads the COMMITTED spec.
            #
            # No working-tree write reaches it — not this one, and not a write to the
            # main checkout either: the fresh worktree comes from git rather than from a
            # copy of that tree, and `seed_adapter_defaults` seeds adapter config files,
            # not the output folder. The channel that DOES work is the human committing
            # the corrected spec from the resolve session, which runs with `cwd=project`.
            # The writes below are kept (they are correct for the in-place case, and
            # harmless here), but the operator is told — a flip that cannot land is
            # exactly the silent re-wedge #640(b) exists to end.
            #
            # "Worktree-local" is the load-bearing qualifier, and isolation does not
            # imply it: an artifact dir configured OUTSIDE the project tree is shared
            # across checkouts by `ProjectPaths.rebased`, so a spec that landed there is
            # one file the fresh worktree reads through the very absolute path this
            # writes to. `_spec_is_shared_with_the_redrive` carves out that case, and only
            # that one: the main checkout's copy is outside the worktree too, and stays
            # unreachable because the re-drive measures it against worktree-local roots.
            # Route /bmad-build-auto via the spec's frontmatter status (decision
            # table): patch-restore -> in-review -> step-04 (resume review on
            # the restored diff); from-scratch -> ready-for-dev -> step-03
            # (re-implement). Independent of the resolve agent having set it.
            target_status = "in-review" if restore_patch else "ready-for-dev"
            # Whether the writes below are the copy the re-driven session actually
            # reads. Hoisted out of the record's condition because TWO decisions turn on
            # it, and only one of them used to: the warning below, and the flip's
            # REFUSAL one screen down, which was gated on `spec_path.is_file()` alone.
            # Under isolation that readable file is the doomed worktree copy, so the
            # refusal demanded a repair to the one file the re-drive destroys before
            # reading anything — and demanded it even when `_committed_spec_status` had
            # already proven the committed spec carries the status the re-drive routes
            # on. See `_spec_is_shared_with_the_redrive` for why an isolated unit's spec
            # is nevertheless reachable when it sits in an artifact dir configured
            # outside the project tree.
            write_reaches_the_redrive = spec_reaches_the_redrive(task, state)
            # Narrowed to the case an operator can ACT on. Every isolated escalation
            # carries a mounted `worktree_path` — `worktree_flow.escalate_unit` never
            # clears it, and `keep_branch_and_escalate` deliberately leaves the worktree
            # up — so gating on that alone fired this warning on 100% of re-arms under
            # `isolation = "worktree"`: a per-configuration constant, not an event, and
            # the same "trains the operator to scroll past the meaningful one" failure
            # that the `flipped` read-back below and the `overwritten != old_baseline`
            # guard were each narrowed to avoid. The remedy it prints ("commit the
            # corrected spec") is already a no-op once the committed spec carries the
            # target status, which is precisely when the re-drive reads what it needs.
            # Suppression requires PROOF: an unreadable blob, a non-repo project, or any
            # git fault leaves `""` and the record fires. The proof is read at
            # `_redrive_base_ref`, NOT at the code root's current `HEAD` — the two part
            # company as soon as the operator checks out another branch while the
            # escalation is paused, and this record now holds the resume.
            #
            # The branch rides along because the remedy needs it: on exactly the shape
            # the ref fix rescues, "commit the corrected spec" without a branch sends
            # the operator to commit again on the branch the re-drive does not read, and
            # the next re-arm prints the same sentence. Empty for the migrated shape
            # `_redrive_base_ref` degrades to `HEAD` for, and the notice drops the
            # clause rather than naming a ref it cannot source.
            #
            # Spelled `target_branch` and NOT `base`, because `diagnostics` routes the
            # scrub by field NAME: `target_branch` is already in `_JOURNAL_ALIAS_FIELDS`
            # under the `branch` namespace (with no journal producer until now), while
            # any new spelling falls through to `scrub_json`, which waves an
            # identifier-shaped branch name through verbatim. In a normal run
            # `ensure_target_branch` has already journalled the same string as `branch`,
            # so the egress backstop would repair it and disclose a `backstop_repairs`
            # routing gap; in a truncated journal missing that event nothing would catch
            # it and the branch would ship in a shareable bundle. `target` — the
            # spelling the merge kinds use — is NOT available: `board-advance-*` puts a
            # sprint STATUS in that same field, and routing is by name, so aliasing it
            # to `branch` would pseudonymize statuses as branches.
            if (
                not write_reaches_the_redrive
                and _committed_spec_status(state, task) != target_status
            ):
                journal.append(
                    "rearm-spec-write-unreachable",
                    story_key=key,
                    spec_file=str(spec_path),
                    status=target_status,
                    target_branch=state.target_branch,
                )
            # Captured immediately before the FIRST write, so an abort further down can
            # put the spec back exactly as found. Unreadable degrades to `None`: the
            # writes below answer such a path with `False` rather than an exception, so
            # there would be nothing to undo either.
            try:
                spec_before = spec_path.read_bytes()
            except OSError:
                spec_before = None
            try:
                flipped = verify.set_frontmatter_status(
                    spec_path, target_status, confine_root=task_spec_root(task, state)
                )
                # `set_frontmatter_status` answers "nothing to change" with `False`
                # for FOUR causes, not three — its own docstring lists them: no file,
                # no frontmatter block, no top-level `status:`, and ALREADY AT THE
                # TARGET (`_edit_frontmatter_block` returns None on
                # `original[key] == value`). Only the first three are failures. The
                # fourth is an ordinary, fully-successful re-arm: a second resolve
                # cycle on an already-flipped spec, or the documented
                # `resolve --no-interactive` flow where a human fixed the spec
                # themselves — the case the comment above calls "Independent of the
                # resolve agent having set it". Journalling it fired the operator
                # warning ("could not be re-opened … may re-wedge on it") on a spec
                # that was byte-identical and CORRECT, which is the "trains the
                # operator to scroll past the meaningful one" failure the re-stamp's
                # `overwritten != old_baseline` guard exists to prevent one screen
                # below. Read the status back to tell the two apart: `read_frontmatter`
                # degrades a missing/unreadable/unparseable spec to `{}` and `status_of`
                # then answers `""`, so all three real failures still record.
                if not flipped and verify.status_of(verify.read_frontmatter(spec_path)) != (
                    target_status
                ):
                    # Discarding that return is how the flip
                    # became a SILENT no-op: the re-drive is dispatched anyway, step-01
                    # reads the unchanged terminal status, routes the session to "ingest
                    # as context, do not resume", and the story re-wedges with nothing on
                    # the record. The `FrontmatterWriteError` arm below covers only the
                    # shapes that RAISE; this covers the ones that lie quietly.
                    # `refused` is written ON the record because ONE kind now covers
                    # two outcomes and the operator surfaces must tell them apart —
                    # they read the journal OUT OF PROCESS, with neither the task nor
                    # the tree to re-derive it from. Printing the refusal's remedy
                    # ("add a top-level `status:`") for a re-arm that COMPLETED sends
                    # the human to repair a file nothing will read.
                    refused = spec_path.is_file() and write_reaches_the_redrive
                    journal.append(
                        "rearm-spec-flip-skipped",
                        story_key=key,
                        spec_file=str(spec_path),
                        status=target_status,
                        refused=refused,
                    )
                    # ...and then ABORT — but only for a spec that IS a readable file
                    # here AND is the copy the re-drive reads. The first half is the same
                    # `is_file` split the baseline re-stamp below already draws, and for
                    # the same reason. On THAT shape the failure is
                    # a REPAIR that did not land on the very file the re-drive reads, so it
                    # aborts for the same reason the `FrontmatterWriteError` arm does:
                    # journalling alone left the two default surfaces telling the operator
                    # "re-armed <story>" and resuming in the same gesture, so the record's
                    # own imperative was already unactionable when it rendered — while
                    # step-01's contract for what reaches here is not a maybe. A spec with
                    # no `status:` HALTs blocked on `unrecognized status in existing story
                    # file`; one still carrying the escalated attempt's terminal status
                    # routes to "ingest as context, do not resume". Either way the re-drive
                    # re-wedges and the escalation is burned. Refusing keeps it armed: nothing
                    # is persisted yet (`save_state` runs below), the spec is byte-identical
                    # (the `## Auto Run Result` strip is deliberately sequenced AFTER this
                    # check so an abort leaves nothing half-done), and the human fixes the
                    # frontmatter and re-runs resolve.
                    #
                    # A spec that is NOT a file from here keeps warn-and-continue, because
                    # there the flip's failure says nothing about what the re-drive will
                    # read: `spec_file` is persisted RELATIVE to a worktree, an isolated
                    # task's worktree may already be gone, and the re-drive mounts a fresh
                    # one and reads the COMMITTED spec regardless. Aborting on it would
                    # refuse the re-arms that the `rearm-baseline-restamp-skipped` and
                    # `rearm-spec-write-unreachable` records exist to report rather than
                    # prevent — an unreadable path is an observation, and observations
                    # degrade.
                    #
                    # A worktree-local spec that IS readable takes that same lane, for a
                    # sharper version of the same reason: `task_spec_root` anchors this
                    # write on the mounted worktree, so the readable file is the copy the
                    # re-drive DISCARDS. The refusal's own remedy could not fix anything
                    # there — an operator who added a `status:` to that file and re-ran
                    # resolve would flip a spec that is deleted before it is read, while
                    # the committed spec, the one thing that decides routing, went
                    # untouched. Worse, the refusal fired even when the correction was
                    # already committed: `_committed_spec_status` had just PROVEN the
                    # re-drive routes correctly, and the re-arm was refused anyway over an
                    # obsolete copy. The real remedy on that shape is
                    # `rearm-spec-write-unreachable`'s ("commit the corrected spec"),
                    # which fires from the block above on exactly the legs that need it
                    # and now holds the resume rather than merely printing.
                    #
                    # The record is written on BOTH sides of that split: the abort message
                    # reaches stderr only, and the journal is the run's audit trail —
                    # `_echo_rearm_events` surfaces it from a `finally` on this path.
                    if refused:
                        raise RearmError(
                            f"cannot re-open story spec {spec_path} to `{target_status}` "
                            "for the re-drive: it has no frontmatter `status:` this re-arm "
                            "can set, so the re-driven session would wedge on the status "
                            "it reads — add a top-level `status:` to the spec's "
                            "frontmatter block, then re-run resolve"
                        )
                # drop the stale `## Auto Run Result` section along with the status flip
                # (mirrors engine._reset_spec_for_repair): find_result_artifact keys on
                # that heading, so leaving it would let the re-driven session's first
                # save of the spec parse as the prior attempt's terminal outcome.
                #
                # Sequenced AFTER the read-back check above, not with the flip it mirrors:
                # that check now raises, and an aborted re-arm must leave the spec exactly
                # as it found it — a stripped result section on a spec the re-arm then
                # refused would be the one edit nothing else records.
                devcontract.strip_auto_run_result(
                    spec_path, confine_root=task_spec_root(task, state)
                )
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
                #
                # ...and this arm is the SECOND refusal that can fire after a write has
                # landed, which the sequencing argument above does not cover. It guards
                # BOTH helpers, and `strip_auto_run_result` is the later one: by the
                # time its own read/decode or its atomic write faults (an
                # `atomic_write_bytes_confined` that cannot land — ENOSPC, EIO, a
                # component swapped for a link under the `O_NOFOLLOW` walk — or a spec
                # replaced under us between the two writes), the flip has already been
                # published and `save_state` has not. Ordering the strip after the
                # read-back check bought that check its byte-identical abort; it buys
                # this one nothing, because the fault is IN the strip. So the same undo
                # the re-stamp carries applies here, on the same terms.
                #
                # On the arm's other shape — the flip itself faulting on an
                # unreadable/undecodable spec — nothing was written, `spec_before` still
                # equals the bytes on disk, and `_restore_rearmed_spec` proves that and
                # returns without touching the file or its mtime.
                _restore_rearmed_spec(spec_path, spec_before, task, state)
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
    # The CODE tree, not `state.project`: every git read below (and every baseline
    # the proof-of-work gate later measures against) must name the repository the
    # dev writer stamps.
    #
    # That is `paths.repo_root` for every run this function can be reached from, but
    # NOT because `paths.repo_root == workspace.root` universally — it does not.
    # `Workspace.default` sets `root=paths.repo_root`, while the isolation constructor
    # mounts `root=<run_dir>/worktrees/<unit>` and rebases a fresh `ProjectPaths` onto
    # it, so under `isolation = "worktree"` the run-level `repo_root` is the main
    # checkout and the baseline is stamped in the worktree.
    #
    # `bmadconfig.worktree_isolation_conflict` refuses worktree isolation beside a
    # `repo_root:` OVERRIDE — a narrower fact than it looks. It forces
    # `repo_root == project`; it says nothing about `repo_root` vs `workspace.root`.
    # Under plain isolation with NO override those two still diverge and isolation is
    # ON, so "wherever the roots could diverge, isolation is off" is false, and a rule
    # built on it licenses treating `state.code_root` as the tree the dev writer
    # stamped — which under isolation it is not.
    #
    # What is true, and the only claim to carry forward: `repo_root == project` in
    # every reachable configuration, so reading HEAD here is right for the in-place
    # case; and under isolation this value is deliberately SUPERSEDED rather than
    # relied on — `engine._finish_inflight` discards the worktree and `_dev_phase`
    # re-stamps `task.baseline_commit` from the fresh worktree's HEAD before any gate
    # reads it. Do not carry an identity into new code; carry this argument.
    #
    # A pre-upgrade state.json with no recorded root degrades to `project` exactly as
    # before.
    repo = state.code_root
    stale_residue = _stale_restore_residue(repo, journal, key, old_latch, old_baseline)

    # Advance the attempt baseline to the CODE TREE's current HEAD (`repo`, above)
    # and refresh the untracked snapshot: whatever the human-driven resolve session left on the
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
    advanced = False
    try:
        head = verify.rev_parse_head(repo)
        untracked = sorted(verify.untracked_files(repo) - stale_residue)
    except verify.GitError as e:
        # `verify.GitError` is a TOTAL replacement for the `except Exception` that
        # stood here, not a narrowing that leaks: both calls go through `_run_git`,
        # which translates spawn (`GitSpawnError`), timeout (`GitTimeoutError`) and
        # decode faults into this one taxonomy, and a non-zero rc into a plain
        # `GitError`. Still swallowed rather than raised — a project that is not a
        # git repo must not fail re-arm — but no longer SILENT: the degrade is the
        # difference between "the re-drive starts from the resolution" and "it
        # rebuilds against the tree the human just corrected away", and the
        # re-stamp below now refuses to paper over it.
        journal.append(
            "rearm-baseline-advance-failed",
            story_key=key,
            repo=str(repo),
            baseline=old_baseline or "",
            error=f"{e.__class__.__name__}: {e}",
        )
    else:
        task.baseline_commit = head
        task.baseline_untracked = untracked
        advanced = True

    # Re-stamp the spec's own baseline to the advanced one, on BOTH re-drive legs.
    #
    # The patch-restore leg needs it because the in-review route skips step-03 —
    # the only step that stamps `baseline_revision` — so without it the re-driven
    # step-04 would build its review diff (and, on an intent-gap/bad-spec
    # re-triage, revert) "since" the ORIGINAL pre-attempt sha, clawing back the
    # very resolve-session commits the advance above just blessed as the re-drive's
    # starting point.
    #
    # The from-scratch leg gets it too (#640a). Its step-03 re-stamps the key
    # itself, so the write is redundant on the happy path — but only ON that path:
    # until step-03 runs, the spec carries the escalated attempt's sha, and every
    # gate that reads a claimed baseline before then reads a stale one. The cost is
    # recorded rather than hidden: re-stamping removes the gate's INDEPENDENT
    # signal on this leg (it then compares a value the orchestrator itself wrote),
    # so a claim that genuinely diverged is journalled on the way out instead of
    # being silently normalized.
    #
    # Gated on `advanced`, not on truthiness of `task.baseline_commit`: a failed
    # advance leaves the OLD sha in that field, which passes a truthiness test
    # identically to a freshly advanced one. Writing it would make spec and task
    # agree on a stale value — the one state in which nothing downstream can tell
    # that the advance never happened, and the re-drive rebuilds from the wrong
    # point with no error anywhere. Skipping keeps the failure legible (the degrade
    # is journalled above) and keeps re-arm non-fatal outside a repo.
    #
    # Loud on WRITE failure: a silently stale spec baseline is exactly the hazard
    # being closed.
    #
    # Guarded on `is_file` FIRST, because a spec this process cannot reach is not a
    # write failure here — it is a SILENT one. Both frontmatter writers answer such a
    # path with `False` rather than an exception (`verify.set_frontmatter_status`,
    # `verify.set_frontmatter_field`), so without a check the re-stamp no-ops with
    # nothing on the record and the spec keeps the escalated attempt's sha.
    #
    # `task_spec_path` re-anchors the recorded path before we get here, which is what
    # makes `is_file` mean what it says. Resolved raw it meant something else and worse:
    # `spec_file` is persisted RELATIVE to the worktree for an isolated task, and the
    # main checkout carries the same layout, so the check passed on the wrong file and
    # the write landed there. The restore leg cannot reach any of this (its precondition
    # rejects a truthy `task.worktree_path`); the from-scratch leg has no such guard,
    # which is exactly why that precondition has to exist.
    #
    # `is_file` is necessary but not sufficient: a spec that EXISTS with no frontmatter
    # block also returns `False` from both writers. That shape is caught by the flip's
    # `flipped` check above and, here, by `overwritten` staying empty.
    if task.spec_file:
        spec_path = task_spec_path(task, state)
        if not spec_path.is_file():
            # OUTSIDE the `advanced` gate on purpose. Nesting this record inside it
            # made the two #640 legs shadow each other: on a project that is not a
            # repo the advance fails, `advanced` is False, and an unreadable spec
            # then produced NO record at all — the journal blamed git while the
            # status flip above had silently no-opped for an entirely different
            # reason. The two degrades compose; they do not substitute.
            journal.append(
                "rearm-baseline-restamp-skipped",
                story_key=key,
                spec_file=str(spec_path),
                baseline=task.baseline_commit or "",
            )
        elif advanced and task.baseline_commit:
            try:
                # Read through the same reader both consumers of a claimed baseline use,
                # so what gets journalled as "overwritten" is the value the gate would
                # have judged — not whichever key happened to be inspected here (#716).
                #
                # INSIDE the try, with the write it describes. `read_frontmatter` opens
                # the file itself, so an OSError here would otherwise escape as a
                # traceback from the one block whose whole contract is to turn a spec
                # this re-arm cannot move into an actionable `RearmError`. What it does
                # NOT rescue: `read_frontmatter` DEGRADES an unparseable YAML block to
                # `{}` rather than raising, so on such a spec `overwritten` is `""`, the
                # guard below is falsy, and no divergence record is written even though
                # the insert lands. That is the reader's deliberate observe-degrade
                # contract, not something to defeat here — the value is unknowable, and
                # inventing one would be worse than the silence.
                overwritten = auto_dev_baseline_of(verify.read_frontmatter(spec_path))
                verify.set_frontmatter_field(
                    spec_path,
                    "baseline_revision",
                    task.baseline_commit,
                    confine_root=task_spec_root(task, state),
                )
            except (OSError, UnicodeDecodeError, verify.FrontmatterWriteError) as e:
                # FrontmatterWriteError joins the tuple rather than getting its own
                # arm: the remedy is the same sentence ("fix the file"), and the
                # exception already says which shape it could not move. What matters
                # is that it aborts here — the stale-baseline hazard this block exists
                # to close is exactly what a swallowed write would leave behind.
                #
                # ...and that the abort leaves the spec as this re-arm FOUND it. This is
                # the LAST of the two refusals that can fire after a write has landed —
                # the flip and the result strip are both behind us, `save_state` is not —
                # so it carries the undo the sequenced refusals get for free (the other
                # is the spec block's `(OSError, UnicodeDecodeError)` arm, which the
                # strip raises through after the flip has published). Without
                # it a spec with a movable `status:` beside an unmovable
                # `baseline_revision:` came back flipped to the re-drive's status and
                # stripped of the terminal result, while the run still called the story
                # escalated.
                _restore_rearmed_spec(spec_path, spec_before, task, state)
                raise RearmError(
                    f"cannot re-stamp baseline_revision on {spec_path} "
                    f"({e.__class__.__name__}: {e}) — fix the file, then re-run resolve"
                ) from e
            if overwritten and overwritten != old_baseline:
                # Compared against `old_baseline` — what the RUN recorded for the
                # escalated attempt — NOT against `task.baseline_commit`, which the
                # advance above has already moved to the new HEAD. Measuring against the
                # advanced value made this fire on every ordinary from-scratch re-arm
                # whose resolve session committed anything: the spec and the run agreed
                # exactly, and the operator was still told they diverged. A record that
                # fires on the routine case is the "trains the operator to scroll past
                # the meaningful one" failure the `restore` split exists to prevent.
                #
                # What survives is the real signal, on BOTH legs: the spec claimed a
                # baseline the run never recorded. That is the only trace left of a
                # divergence the gate can no longer report, because the re-stamp is
                # about to normalize it away.
                journal.append(
                    "rearm-baseline-restamped",
                    story_key=key,
                    spec_file=str(spec_path),
                    overwritten=overwritten,
                    baseline=task.baseline_commit,
                    restore=bool(restore_patch),
                )

    save_state(run_dir, state)
    journal.append(
        "story-escalation-resolved",
        story_key=key,
        baseline=task.baseline_commit or "",
        restore=bool(restore_patch),
    )
    return key


def journal_entries_or_none(run_dir: Path) -> list[dict[str, Any]] | None:
    """This run's journal entries, or ``None`` when the journal cannot be read.

    The re-arm surfaces read the journal TWICE to diff what a re-arm appended, and
    before that echo existed they read it not at all — so `Journal.entries()`' strict
    UTF-8 decode would turn a corrupt journal into a re-arm the operator can no longer
    perform, which is strictly worse than the missing echo and a regression against the
    gesture's own history. Shared by `cli.cmd_resolve` and `TuiApp._do_rearm` rather
    than living on one of them: the CLI's copy was left unguarded when the TUI's was
    hardened, and the CLI's echo now runs from a `finally`, where a raise would replace
    the `RearmError` the operator actually needs to see.

    ``None`` rather than ``[]`` because the two callers DIFF two reads. Degrading a
    failed FIRST read to ``[]`` sets the watermark to zero, and a second read that
    succeeds then replays every historical `rearm-*`/`stale-restore-*` entry as if this
    re-arm had just produced it. A caller that cannot establish both ends of the diff
    must skip the echo, not guess at it.
    """
    try:
        # Non-mapping lines are dropped HERE so the annotation is true for every
        # caller: `Journal.entries()` appends `json.loads(line)` with no shape filter,
        # so a bare `3` or `null` on its own line survives as a non-dict entry and its
        # `list[dict[str, Any]]` return type is a claim about first-party producers,
        # not a guarantee — pyright sees `Any` and is satisfied. Both reads apply the
        # same filter, so the `len(before)` watermark stays exact.
        return [e for e in Journal(run_dir).entries() if isinstance(e, dict)]
    except (OSError, UnicodeDecodeError):
        return None


def _journal_sequence(value: Any) -> tuple[Any, ...]:
    """A journal list field read back as a sequence, whatever the line actually held.

    Every read in `rearm_event_notice` runs inside both operator surfaces' `finally`,
    where a `TypeError` replaces the outcome the operator needs — on the TUI, whose
    `_do_rearm` runs on Textual's message loop with no `_handle_exception` override,
    it ends the app. `", ".join` and `len` are the two reads that raise on a shape the
    journal admits (`"files": 3`, `"files": null`, `[1, 2]`); every sibling read is
    already `str()`-wrapped or f-string-interpolated and cannot.

    A bare string is deliberately NOT iterated: `", ".join("abc")` renders `"a, b, c"`,
    which is worse than useless. It is wrapped as a single element instead, and `None`
    — which `.get(key, default)` returns whenever the key EXISTS holding null, so the
    default never applies — reads as empty.
    """
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return () if value is None else (value,)


def rearm_event_notice(
    entry: dict[str, Any],
) -> tuple[Literal["note", "warning"], str, str] | None:
    """`(severity, message, next_step)` for a re-arm record an operator must see.

    ONE table, two surfaces. `cli._echo_rearm_events` prints `message` followed by
    `next_step`; `TuiApp._do_rearm` shows `message` alone. That split is the whole
    reason this returns three fields instead of a formatted line: the TUI re-arms and
    RESUMES in a single gesture, so an instruction to check something "before
    resuming" is already unactionable by the time it renders — but the finding it
    reports is not, and dropping the record to avoid the dead imperative is what left
    the TUI silent on three kinds `resolve` echoed.

    Returns None for journal kinds no operator has to act on, so a caller can walk
    every new entry and let the table decide.

    Severity is `"note"` or `"warning"`; each surface maps those onto its own channel.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind", "")
    if kind == "stale-restore-excluded":
        files = ", ".join(str(f) for f in _journal_sequence(entry.get("files")))
        return (
            "note",
            f"excluded the abandoned restore's new files from the re-drive " f"baseline: {files}",
            "",
        )
    if kind == "stale-restore-unparseable":
        return (
            "warning",
            f"could not read the abandoned restore patch ({entry.get('patch', '?')}) "
            "— its new files may be swept into the next commit",
            "check `git status` before resuming",
        )
    if kind == "stale-restore-commits":
        n = len(_journal_sequence(entry.get("commits")))
        return (
            "warning",
            f"{n} commit(s) sit below the re-drive's new baseline "
            f"({str(entry.get('old_baseline', '?'))[:12]}..) — if any came from the "
            "abandoned attempt rather than your resolve, revert them now",
            "",
        )
    if kind == "rearm-baseline-advance-failed":
        return (
            "warning",
            f"could not advance the re-drive baseline ({entry.get('error', '?')}) — it "
            f"still names {str(entry.get('baseline', '') or '(none)')[:12]}, so the "
            "re-drive rebuilds against the tree as it stood before your resolve; the "
            "spec was deliberately NOT re-stamped",
            "Check the baseline before resuming",
        )
    if kind == "rearm-spec-write-unreachable":
        # The branch is the half an operator cannot infer: the re-drive cuts its fresh
        # worktree from the run's PINNED target branch, so a correction committed on
        # whatever the main checkout happens to have checked out is not the one it
        # reads. Named only when the record carries it — a run predating the field
        # leaves it empty, and a remedy that names no ref beats one that names a guess.
        base = str(entry.get("target_branch", "") or "")
        where = f" on `{base}`" if base else ""
        return (
            "warning",
            f"this story ran under worktree isolation, so the re-arm's spec writes "
            f"({entry.get('spec_file', '?')}) land in a worktree the re-drive discards "
            "— the re-driven session reads the COMMITTED spec, so commit the corrected "
            f"spec{where} or the story re-wedges on the escalated attempt's status",
            f"Commit the corrected spec{where} before resuming",
        )
    if kind == "rearm-spec-flip-skipped":
        # ONE kind, TWO outcomes, told apart by the flag the producer writes rather
        # than by anything readable from here: `rearm_escalation` raises `RearmError`
        # right after journalling this only when the flip failed on the very copy the
        # re-drive reads. It also journals it — and completes — when that copy is
        # unreadable from this process, or is a worktree-local file the re-drive
        # discards. This row used to claim the abort unconditionally, which told an
        # operator whose re-arm had SUCCEEDED that it "was REFUSED" and sent them to
        # add a `status:` to a file the re-drive never opens.
        spec = entry.get("spec_file", "?")
        status = entry.get("status", "?")
        if entry.get("refused"):
            # The message names the refusal rather than predicting a re-wedge, because
            # there is no re-drive left to wedge — and the next_step is the repair, not
            # an inspection, for the same reason.
            return (
                "warning",
                f"the recorded spec for this story ({spec}) could not be re-opened to "
                f"`{status}` — it carries no frontmatter `status:` to set, so the "
                "re-arm was REFUSED rather than re-driving a session that would wedge "
                "on the status it reads",
                "Add a top-level `status:` to the spec, then re-run resolve",
            )
        # No next_step, and deliberately: on this leg there is nothing to do to THIS
        # file. Whether anything is left to do at all is decided by the committed spec,
        # and `rearm-spec-write-unreachable` — journalled from the same block, on
        # exactly the legs where the committed spec is not already at the target —
        # carries that imperative, and holds the resume behind it.
        return (
            "warning",
            f"the recorded spec for this story ({spec}) could not be re-opened to "
            f"`{status}` — the re-arm was NOT refused, because that copy is not what "
            "the re-driven session reads: it mounts a fresh worktree and reads the "
            "COMMITTED spec",
            "",
        )
    if kind == "rearm-baseline-restamp-skipped":
        return (
            "warning",
            f"the recorded spec for this story ({entry.get('spec_file', '?')}) is not a "
            "readable file from here, so the baseline re-stamp was skipped — the spec "
            "still names the escalated attempt's baseline",
            "Check the recorded spec path before resuming",
        )
    if kind == "rearm-baseline-restamped":
        head = (
            f"re-stamped the spec baseline "
            f"{str(entry.get('overwritten', '?'))[:12]}.. -> "
            f"{str(entry.get('baseline', '?'))[:12]}.."
        )
        # NOT differentiated on the `restore` flag any more. That split predated the
        # record's condition moving to `overwritten != old_baseline` (compared against
        # what the RUN recorded, not against the just-advanced value): the record now
        # fires ONLY when the spec claimed a baseline the run never recorded, which is
        # equally exceptional on both legs. Keeping the split meant the patch-restore
        # leg's real divergence was the one downgraded to a note. The flag stays ON the
        # record because it says which leg produced it — not how routine it is.
        return (
            "warning",
            f"{head} — the spec claimed a DIFFERENT baseline than the run recorded, "
            "and this re-stamp is the only trace of it; the gate can no longer report "
            "that divergence",
            "",
        )
    return None


def rearm_holds_the_resume(entry: dict[str, Any]) -> bool:
    """True for a re-arm record whose remedy has to land BEFORE the re-drive reads the
    tree — so a surface that re-arms and resumes in ONE gesture must stop after the
    re-arm and leave `bmad-loop resume` to the operator.

    Exactly one kind qualifies, and the discriminator is PROOF, not urgency.
    `rearm-spec-write-unreachable` is written only once `_committed_spec_status` has
    established that the committed spec does NOT carry the status the re-drive routes
    on, and only for a spec the working-tree flip cannot reach. Resuming on it is not
    risky, it is futile: the re-drive discards the worktree, mounts a fresh one from
    git, and step-01 reads a status it cannot route — `unrecognized status in existing
    story file` halts it blocked, and the escalation is spent. The record's own
    next_step already said "commit the corrected spec before resuming"; both default
    surfaces then resumed in the same breath, which made the imperative unactionable at
    the moment it rendered. The interactive resolve agent cannot close that gap either
    — its skill forbids it from committing.

    The other warnings stay advisory and do NOT hold. `stale-restore-commits`,
    `stale-restore-unparseable` and `rearm-baseline-advance-failed` each report
    something an operator may need to act on, but none of them PROVES the re-drive
    cannot route, and holding on a maybe would turn the ordinary degrade path into a
    two-command gesture for an outcome nothing decided.

    Not folded into `rearm_event_notice`'s tuple, because they are different questions
    asked of the same entry: that table answers "what do I tell the operator", this
    answers "may this gesture still resume". Both surfaces ask both, in one walk.
    """
    return isinstance(entry, dict) and entry.get("kind") == "rearm-spec-write-unreachable"


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
