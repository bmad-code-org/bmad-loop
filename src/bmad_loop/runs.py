"""Run-directory discovery and helpers shared by the CLI and the TUI.

The public story-spec ownership and confinement seam consists of four helpers:

* :func:`task_spec_path` anchors a persisted ``StoryTask.spec_file`` on the tree
  that owned the task when it was recorded.
* :func:`task_spec_root` names the matching root used to confine writes to that
  anchored spec.
* :func:`task_stories_root` locates the workspace tree from which the run reads
  its stories folder; it is a read locator, not a spec-write confinement root.
* :func:`spec_reaches_the_redrive` reports whether an edit at the persisted-task
  anchor will survive to the workspace used by the next attempt.
"""

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import devcontract, envvars, verify
from .adapters.multiplexer import (
    MultiplexerError,
    TerminalMultiplexer,
    get_multiplexer,
    mux_usable,
)
from .frontmatter import auto_dev_baseline_of, parse_frontmatter, status_of
from .journal import STATE_FILE, VERIFY_DIR, Journal, load_state, save_state, state_lock
from .model import PAUSE_ESCALATION, Phase, RunState, StoryTask
from .platform_util import (
    MAX_SEGMENT,
    UnconfinedWriteError,
    _mkstemp_beside,
    atomic_replace,
    atomic_write_bytes,
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

# The multiplexer registry's directory name inside a project's state subtree (see
# `mux_registry_root`). It sits beside the run entries and must never BE one: the
# leading underscore is what makes that structural, since `RUN_ID_RE` requires an
# alphanumeric first character, so no `--run-id` can key its state dir onto the
# registry. That is also what lets the orphan-state sweep tell the two apart by
# name alone (see `reconcile_orphan_state_dirs`).
MUX_REGISTRY_DIR = "_mux"
# psmux's own registry-root variable. Named here, in transport-agnostic code, for
# the same reason `PROJECT_OPTION` is: the export has to happen ahead of backend
# selection, which probes a subprocess, so it cannot be routed through a backend
# instance. See `export_psmux_registry_root`.
PSMUX_DATA_DIR = "PSMUX_DATA_DIR"
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
    which are legal-looking ids that no filesystem will accept as a directory.

    The control-session shape (``ctl``, ``ctl-…``, any letter case) is reserved
    on the same principle, against the multiplexer namespace instead of the
    filesystem's — see :func:`is_reserved_run_id` for the shape and why case is
    folded. Refusing the id here is what makes the two session namespaces
    disjoint: every agent session is ``bmad-loop-<valid id>``, so none can
    reach the control session's name."""
    return _wellformed_run_id(value) and not is_reserved_run_id(value)


def _wellformed_run_id(value: str) -> bool:
    """The shape half of :func:`is_valid_run_id`: charset, length, and the
    reserved-device-basename identity check — everything except the
    control-session reservation. Split out because the *parse* side
    (:func:`_agent_run_id`) must accept ids the *mint* refuses: a run
    persisted by an older release under e.g. ``ctl-foo`` owns a genuine
    ``bmad-loop-ctl-foo`` agent session that the sweep has to be able to
    reach."""
    return (
        bool(RUN_ID_RE.fullmatch(value))
        and len(value) <= MAX_SEGMENT
        and safe_segment(value) == value
    )


def is_reserved_run_id(value: str) -> bool:
    """The MINT-side reservation: any id of the control-session shape (``ctl``
    or ``ctl-…``, any letter case) is refused at :func:`is_valid_run_id`.
    Deliberately broader than :func:`run_id_aliases_control_session` — a new id
    anywhere near the control namespace buys nothing but confusion, so none is
    admitted — while the read paths, which must handle ids an older release
    already persisted, use the narrow test. ``RUN_ID_RE`` is ASCII-only, so
    ``str.lower`` is the exact fold (see the narrow test for why case folds at
    all)."""
    v = value.lower()
    return v == "ctl" or v.startswith("ctl-")


def run_id_aliases_control_session(value: str) -> bool:
    """True when ``session_name(value)`` names a session that can BE a live
    control session: the fixed name (id ``ctl``) or a per-registry digest name
    (id ``ctl-<16 hex>`` — the only suffix :func:`ctl_session_for` can mint).
    The adapter's ensure-session would *adopt* that live session as the run's
    own, and the run's teardown would kill the whole control session, every
    parked window of every run in it — so the project-free READ paths key on
    this: :func:`kill_session` skips such an id, :func:`_agent_run_id`
    refuses to read such a session as a run, and ``cli``/the TUI refuse to
    resume/re-arm/replan such a run. This is the SHAPE question — "could
    this name be a control session's on some registry" — and it must stay
    out of any site asking the *instance* question ("is it the control
    session this process addresses"): :func:`live_session_may_be_ours`
    compares against the actual names (the fixed one plus this project's
    :func:`ctl_session_for`), because discounting the whole shape there
    destroyed run dirs under live `ctl-<other digest>` agents on tmux.

    Compared **case-insensitively**: psmux resolves a session by opening
    ``<data dir>\\<name>.port`` by name (``src/paths.rs:113``, source-read at
    v3.3.8), and NTFS opens names case-insensitively — measured: with
    ``bmad-loop-ctl-x`` live, target ``bmad-loop-CTL-x`` answers
    ``has-session``, is refused as a duplicate by ``new-session``, and a kill
    through it takes the lowercase session down.

    Deliberately narrower than :func:`is_reserved_run_id`: a historical
    ``ctl-foo`` run's session is a GENUINE agent session, distinct from every
    control session and addressable exactly and safely (tmux: measured, the
    exact full target removes only it; our seam sends ``=``-exact targets —
    ``tmux_base.py:141,166``, source-read. psmux: exact port files, case
    aside). Skipping those too made such runs unreachable by ``stop`` and
    ``cleanup`` both. Ceiling, named: an id of exactly the digest shape whose
    hex is NOT the current registry's digest is also skipped — undecidable
    without the project in hand, and the leak direction (one stale session
    left standing) is the safe one."""
    return is_ctl_session_name(session_name(value).lower())


def is_parsable_run_id(value: str) -> bool:
    """The PARSE-side counterpart of :func:`is_valid_run_id`: may an id
    recovered from an existing multiplexer name be acted on as a run?

    The two questions are different and must never share a predicate.
    :func:`is_valid_run_id` answers "may a NEW id be this", so it carries the
    mint's broad ctl reservation (:func:`is_reserved_run_id`) — and a reader
    that borrows it stops recognising every id an older release already
    persisted. A ``ctl-foo`` run minted before that reservation owns a real
    run dir and a real ``run-ctl-foo`` control-session window; asking the
    mint's question about them leaks both, unreachable by the sweep forever.

    So: the shape half (:func:`_wellformed_run_id` — charset, length, and the
    reserved-device-basename check, because the id still steers a run-dir
    path) minus only the narrow alias test
    (:func:`run_id_aliases_control_session`), which the read paths key on
    because reading one of THOSE as a run points a kill path at the control
    plane. Exactly :func:`_agent_run_id`'s guard, public so the other parse
    sites ask it instead of re-deriving it — the ctl-window sweep in
    ``tui.launch`` did borrow the mint's, and parked pre-upgrade windows
    leaked from ``cleanup`` because of it."""
    return _wellformed_run_id(value) and not run_id_aliases_control_session(value)


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


def mux_registry_root(project: Path) -> Path:
    """This project's terminal-multiplexer registry root:
    ``<state root>/<project key>/_mux`` (see :data:`MUX_REGISTRY_DIR`).

    A *registry* is the directory a multiplexer keeps its per-session addressing
    state in — psmux writes one ``.port``/``.key``/``.sid``/``.pid`` quartet per
    session under ``PSMUX_DATA_DIR`` (default ``%USERPROFILE%\\.psmux``), and
    every verb resolves a session by reading that quartet back. Two processes
    that disagree about the root therefore disagree about which sessions exist,
    which is why the root is *derived* — from the project, through the same
    :func:`project_tag` every ownership tag already uses — rather than minted per
    run, read from a file, or taken from whatever the launching shell exported.
    See :func:`export_psmux_registry_root` for the export and its rules.

    Keyed on the project rather than on bmad-loop as a whole so a prune bug in
    one project cannot address another project's servers at all: the partition
    becomes structural instead of a filter (the ``@bmad_project`` tag stays, as
    the tmux-side answer and the belt). The price is that one ``psmux ls`` no
    longer shows every bmad-loop session on the machine — stated for the operator
    in ``docs/multiplexer-backends.md`` and printed by ``bmad-loop mux``.

    Under :func:`state_root` and not in the project tree, deliberately: a branch
    switch or a rollback that deleted a ``.port`` file would leave the server
    alive, unreachable, and invisible to ``psmux ls`` in *any* registry — a
    manufactured orphan. Same doctrine :func:`state_root` itself exists for.
    """
    return project_state_root(project) / MUX_REGISTRY_DIR


def export_psmux_registry_root(project: Path) -> str | None:
    """Point this process — and everything it spawns — at ``project``'s registry
    by exporting ``PSMUX_DATA_DIR``. Returns the value in force afterwards, or
    ``None`` when no root could be derived.

    **The process environment, not a per-call argument.** The seam spawns every
    psmux verb through ``BaseTmuxBackend._run``, whose ``env=None`` default means
    *inherit this process's environment*, and a create-call-only injection is
    worse than none: the session's server would come up under a root every later
    ``has_session`` / ``list_window_ids`` cannot see, and those verbs report an
    unreadable registry as ``False`` / ``[]`` — a live run reading itself as gone.
    One export ahead of dispatch covers every verb in-process.

    **The root is always derived, and an ambient value never changes it.** That
    is the whole rule, and the absence of an exception is the point:
    :func:`mux_registry_root` is a pure function of (project, state root), so any
    two bmad-loop processes given the same project and the same state root agree
    — which is the entire property #537 exists to establish. A value already in
    the environment is *overridden*, and the caller says so
    (:func:`cli._configure_mux` reports it once on stderr; ``bmad-loop mux``
    discloses it).

    **Why an operator's own ``PSMUX_DATA_DIR`` is not honoured**, since honouring
    it is the obvious kindness and it was tried:

    - It would make the registry a function of the launch *shell*. A TUI started
      from the Start menu carries no profile environment and derives; a run
      started from a dev shell whose profile exports a root honours that root.
      Two registries on one machine, and a live session reading as gone in one of
      them — which is the failure this module exists to prevent, not a corner of
      it.
    - Whether honouring is even the right answer is unknowable from here. A
      process that finds a root in its environment cannot tell one the operator
      typed once in *this* shell — where a clean sibling process would derive —
      from one their profile exports into *every* shell, where a clean sibling
      honours it. The two produce byte-identical environments and want opposite
      answers, so no comparison settles it: the missing fact is the operator's
      intent, and it is not in the environment.
    - It contradicts the promise made beside it. ``BMAD_LOOP_STATE_DIR``'s
      documentation says there is deliberately no second variable naming the
      registry, because "two knobs that can disagree would put two processes on
      different registries, each blind to the other's live sessions". An ambient
      ``PSMUX_DATA_DIR`` is exactly that second knob.

    Overridden rather than *refused*, deliberately: ``PSMUX_DATA_DIR`` is psmux's
    variable, and an operator may have it set for their own sessions with no
    thought of bmad-loop at all. Erroring out of every command on such a machine
    would be bmad-loop claiming a name it does not own. The remedy runs the other
    way and ``bmad-loop mux`` prints it ready to paste: point *your* shell at
    bmad-loop's root, which is a function of the project rather than of whichever
    shell happened to launch something.

    **Overridden, but not abandoned.** A machine that had an absolute value
    exported before the upgrade kept its bmad-loop sessions in THAT registry,
    because the old backend simply inherited it — so the displaced root is
    handed to :func:`~.adapters.psmux_backend.note_displaced_registry` here,
    the last moment anything can still read it, and the migration sweep runs a
    tag-scoped pass over it alongside psmux's default
    (:meth:`~.adapters.psmux_backend.PsmuxMultiplexer.legacy_registries`).
    Without that the override would strand exactly the sessions it displaced,
    with cleanup reporting a clean machine.

    Wanting one registry to serve both is a real request and is deliberately not
    answered here. It needs a stated operator preference rather than a guess at
    one — and it must be a policy *whether*, never a *where*: ``policy.toml`` is
    written by the sessions this orchestrator drives, so a policy-sourced root
    would let a driven session choose which registry the cleanup path kills in.

    **No ``BMAD_LOOP_*`` knob for the root either.** It is derived state, not
    configuration; ``BMAD_LOOP_STATE_DIR`` already relocates it transitively —
    one knob, one cascade, instead of two that can disagree. And ``envvars.py``
    gains no entry for ``PSMUX_DATA_DIR`` itself: that module is scoped to
    ``BMAD_LOOP_*`` names and this is psmux's own, unregistered on the same
    precedent as ``PSMUX_ALLOW_NESTING``.

    **No root travels between processes.** Because every bmad-loop process
    derives its own root, nothing about a registry has to be transported at
    all. What does have to travel is the *state root*: coding-CLI windows are
    told it explicitly through their env dict (:func:`pinned_state_env`), and
    everything else — a session's window-0 shell, the TUI's parked engine
    windows — inherits it, as it always has. psmux's ``PSMUX_BARE_ENV=1`` mode
    breaks that inheritance and is **not supported**: the psmux backend warns
    once per process when it is on (see ``PsmuxMultiplexer._warn_if_bare_env``).

    Never raises. This runs ahead of *every* command, ``diagnose`` and
    ``validate`` included, and an underivable state root must not take the
    diagnostics down with it. ``None`` means "no root established": psmux keeps
    whatever it had, which is also the root cleanup sweeps as the legacy one.
    """
    try:
        root = str(mux_registry_root(project))
    except (StateRootError, OSError, RuntimeError):
        # OSError/RuntimeError: project_tag resolves the project, which raises on
        # a path the OS cannot canonicalize and, below 3.13, on a symlink loop.
        # The ambient value is left exactly as found — there is nothing better to
        # put there, and PsmuxMultiplexer._run still refuses to spawn under a
        # value psmux would panic on.
        return None
    displaced = os.environ.get(PSMUX_DATA_DIR)
    os.environ[PSMUX_DATA_DIR] = root
    if displaced is not None and displaced != root:
        # The variable is now gone, and it was the only record of where a
        # pre-upgrade machine's sessions live: before #537 the backend simply
        # inherited it. Hand it to the backend that has to sweep there, at the
        # one moment it is still knowable. Imported here rather than at module
        # scope because this is the psmux leaf, and this module talks to the
        # seam — the coupling is confined to the function already named for
        # psmux's own variable.
        from .adapters.psmux_backend import note_displaced_registry

        note_displaced_registry(displaced)
    return root


def pinned_state_env() -> dict[str, str]:
    """``{BMAD_LOOP_STATE_DIR: <this process's state root>}``, for a child that
    must land on the same one — or ``{}`` when no root can be derived.

    A convenience spelling of :func:`pin_state_root` over an empty dict, for
    composing env dicts (the engine's session env spreads it in). The final
    merge before a window launch goes through :func:`pin_state_root` itself —
    a spread of this dict is only an ordering guarantee, and ordering
    guarantees nothing when the dict is ``{}``.

    **Resolved, never forwarded.** Passing this only when the operator set it
    would leave exactly the default case broken, which is the common one. What
    travels is the answer this process reached, however it reached it.

    What follows the state root, and what does not, since the two are easy to
    swap: the run's control plane (:func:`state_dir_for`), its event channel
    (:func:`events_dir_for`) and the multiplexer registry
    (:func:`mux_registry_root`) all live under it, so a child computing a
    different root writes and reads where nothing else looks. The run *directory*
    does not — :func:`run_dir_for` is in-tree at ``<project>/.bmad-loop/runs``
    and moves with the project, not with this.

    ``{}`` rather than a raise: a child told nothing derives its own answer and
    fails on the same broken environment with its own message, which is better
    than a launcher that cannot report anything at all.
    """
    return pin_state_root({})


def pin_state_root(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with its ``BMAD_LOOP_STATE_DIR`` entry forced to this process's
    own answer: **set** to the resolved state root when one derives, **removed**
    when none does. Other keys pass through untouched.

    The chokepoint for every merge where a caller-supplied env (a profile's
    ``[env]`` table rides those dicts) meets the state-root pin — the engine's
    coding-CLI window, the probe window, and the attached resolve session. A
    "pin spreads last" ordering rule is not enough, because with an underivable
    state root there is no pin key to order: :func:`pinned_state_env` is ``{}``
    and a profile-declared absolute root would sail through, aiming the window
    at a state root — and so a per-project registry — its own parent cannot
    see. Removing the key instead makes the child inherit the parent's own
    (broken) value and fail exactly as the parent fails: whatever a child
    concludes is what a clean process under the same conditions concludes, in
    the error arm too. The strip governs only what bmad-loop *adds* to a
    child; a value already in the environment a child inherits is not
    scrubbed here.
    """
    pinned = dict(env)
    try:
        pinned[envvars.STATE_DIR] = str(state_root())
    except StateRootError:
        pinned.pop(envvars.STATE_DIR, None)
    return pinned


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


def kill_session(run_id: str, mux: TerminalMultiplexer | None = None) -> None:
    """Kill a run's agent session (bmad-loop-<id>); a no-op when it is already
    gone or the multiplexer is unavailable.

    Also a no-op for an id that **aliases a control session**
    (:func:`run_id_aliases_control_session` — ``ctl`` or ``ctl-<16 hex>``,
    case-folded): the only session such a name can address is the control
    plane, every parked window of every run in it. Unreachable through
    minting (validation refuses the shape) but reachable through what an
    **older release persisted**: a run dir named ``ctl`` that `stop`,
    `delete` or a resume's stale-session sweep replays as a kill target.
    This chokepoint keeps those read paths safe — and usable as the
    operator's way out of such a run — without each caller re-deriving the
    rule.

    The narrow test, not the mint's broad reservation, deliberately: a
    historical ``ctl-foo`` run DOES own an agent session of its own
    (``bmad-loop-ctl-foo``, distinct from every control session and killed
    exactly — the seam sends ``=``-exact tmux targets, and psmux resolves
    exact port files), and skipping its kill stranded it: the prune already
    could not reach it, so nothing could. Scope, stated: the kill addresses
    the registry THIS process addresses — a pre-upgrade session left in
    psmux's old default registry is not reachable from here (measured), and
    deliberately so: a by-name kill in a shared registry without tag proof
    could take another project's same-named session (run ids are unique per
    project only). The legacy sweep in :func:`prune_sessions`, which does
    demand the tag, is the path that reaches it."""
    if run_id_aliases_control_session(run_id):
        return
    (mux or get_multiplexer()).kill_session(session_name(run_id))


CTL_SESSION = "bmad-loop-ctl"
_SESSION_PREFIX = "bmad-loop-"


def ctl_session_for(project: Path, mux: TerminalMultiplexer | None = None) -> str:
    """The control-session name this project's launches and lookups share.

    On a transport with no registry namespace (tmux) it is the fixed
    :data:`CTL_SESSION`, machine-shared as it has always been. On a namespacing
    transport (psmux) the name carries the registry's identity — a 16-hex
    digest of the derived registry root — because the two scopes genuinely
    differ: the session lives *per registry*, but psmux's duplicate-server
    guard is a mutex keyed on the session name alone, across every registry
    in the **login session** (``Local\\psmux-session-{name}`` over
    ``port_file_base()`` — the ``Local\\`` kernel-object namespace is
    per-login-session, not machine-global; ``server/mod.rs:853`` /
    ``platform.rs:346`` / ``types.rs:1345``, source-read at v3.3.8 —
    ``PSMUX_DATA_DIR`` never enters it). A fixed name therefore admits ONE
    control session across every registry a desktop session can reach,
    and the second project's create is rejected as a duplicate server — its
    TUI launch fails instead of minting its own session (measured: a second
    registry answers ``new-session`` rc 1 for the fixed name while the first
    registry's server lives, and rc 0 for a per-registry name).

    The digest is over ``mux_registry_root(project)`` **resolved**: the name
    must be unique per *physical* registry, and the resolved path is that
    registry's identity — (project, state root), both axes; ``project_tag``
    alone would recreate the collision for one project under two state roots.
    Resolved rather than as spelled because two spellings of one state root
    (``C:\\work\\state`` vs ``C:\\work\\alias\\..\\state``) reach **one**
    registry — Windows resolves both to the same files, and psmux keeps the
    spelling only while constructing those paths (``src/paths.rs:79``,
    source-read at v3.3.8; convergence measured) — so an as-spelled digest
    minted two control sessions inside one registry, each blind to the other's
    parked windows: the split-control-plane failure again, one level up. Same
    rule ``project_tag`` already states: resolve *before* digesting.

    …and then ``os.path.normcase``, because ``resolve()`` can only return the
    filesystem's stored case for a path that **exists**, and the registry
    root usually does not yet exist at the moment the name is needed (psmux
    ``create_dir_all``\\s it at first spawn). Two case spellings of a
    not-yet-created state root resolve to two strings, digest to two names —
    and then land in ONE physical registry, because NTFS folds case when
    psmux opens the ``.port`` files (measured). ``normcase`` folds exactly
    where the filesystem does: it lowercases on Windows and is the identity
    on POSIX, where case is significant and two case spellings ARE two
    registries — folding there would merge genuinely distinct roots.
    Ceiling, named: ``normcase`` lowercases with ``str.lower``, which can
    disagree with NTFS's own fold table for a few non-ASCII case pairs; a
    state root spelled in two such casings of the same non-ASCII name stays
    split, as it is for every other digest of an operator-supplied path.

    The degrade arm (namespaced transport, underivable state root) answers
    the fixed name: that arm runs on the transport's shared default registry,
    where a shared session scoped by per-window project tags is the correct,
    tmux-shaped semantic — and where a pre-#537 legacy ctl session under the
    fixed name may exist to be reused rather than collided with.
    """
    mux = mux or get_multiplexer()
    if not mux.has_registry_namespace():
        return CTL_SESSION
    try:
        scope = os.path.normcase(str(mux_registry_root(project).resolve()))
    except (StateRootError, OSError, RuntimeError):
        return CTL_SESSION
    return f"{CTL_SESSION}-{hashlib.sha256(os.fsencode(scope)).hexdigest()[:16]}"


def is_ctl_session_name(name: str) -> bool:
    """Whether ``name`` is a control session's name — the fixed
    :data:`CTL_SESSION`, or ``bmad-loop-ctl-<16 hex>``, the ONE suffix shape
    :func:`ctl_session_for` can mint.

    The shape predicate exists because several readers ask "is this A control
    session" without a project in hand: the agent-session parser must exclude
    ctl sessions (``bmad-loop-ctl-<16hex>`` would otherwise parse as run id
    ``ctl-<16hex>``, which ``RUN_ID_RE`` admits), the legacy-leftovers reader
    names a surviving ctl session in a registry this process did not derive,
    and ``in_ctl_session`` classifies whatever session this process woke up
    inside.

    Exactly the mintable shapes, no wider: an arbitrary suffix
    (``bmad-loop-ctl-foo``) is NOT a control session — it is the agent
    session of a run an older release accepted as ``--run-id ctl-foo``, and
    reading it as a control session made it unreachable by ``stop`` and the
    prune both. No agent session of OURS can match this predicate:
    :func:`is_valid_run_id` refuses every ctl-shaped id at the mint (broad —
    :func:`is_reserved_run_id`), so a matching name is either genuinely a
    control session or hand-made to look like one — and the hand-made
    16-hex-suffixed case stays unprunable, the leak direction."""
    if name == CTL_SESSION:
        return True
    suffix = name.removeprefix(CTL_SESSION + "-")
    return suffix != name and len(suffix) == 16 and all(c in "0123456789abcdef" for c in suffix)


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


def lock_path_for(data_path: Path, *, follow_final_symlink: bool = True) -> Path:
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
    # Run-state publication atomically replaces ``state.json``.  Its transaction
    # lock therefore needs the identity of that *logical directory entry*, not the
    # current referent of a planted final-component symlink: following that link
    # would change the sidecar halfway through an outer transaction when
    # ``save_state`` replaces it.  Other mutable artifacts retain the historical
    # referent-based behavior by default (notably shared external ledgers).
    resolved = (
        data_path.resolve() if follow_final_symlink else data_path.parent.resolve() / data_path.name
    )
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


def _agent_run_id(session: str) -> str | None:
    """The run id behind a ``bmad-loop-<id>`` agent session name, or ``None`` when
    the name is not one: the control session, a foreign session, or a mangled name
    whose id could not be replayed as a path segment — never let one steer a
    run-dir path. Shared so the prune partition and
    :func:`legacy_registry_leftovers` cannot drift on what counts as ours.

    The id question is :func:`is_parsable_run_id`, deliberately NOT
    :func:`is_valid_run_id` — the parse side must accept ids the mint refuses.
    That predicate owns the reasoning, and the ctl-window sweep in
    ``tui.launch`` asks the same one."""
    if not session.startswith(_SESSION_PREFIX):
        return None
    run_id = session[len(_SESSION_PREFIX) :]
    return run_id if is_parsable_run_id(run_id) else None


def prunable_sessions(
    project: Path, mux: TerminalMultiplexer | None = None, *, require_tag: bool = False
) -> tuple[list[str], list[str], set[str]]:
    """Partition the bmad-loop-<id> agent sessions into (prunable, live) run ids,
    plus the subset of prunable ids whose engine liveness read 'unknown'
    (unverifiable pid). Unknown never blocks cleanup — those sessions stay
    prunable — but frontends surface a warning for them.

    A control session (:func:`is_ctl_session_name` — the fixed name or a
    per-registry one) is never a candidate. Pruning is scoped
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

    ``require_tag`` drops that last arm: an untagged session is skipped outright
    rather than falling back to the run dir. Set for a **shared** registry — the
    legacy psmux root every project's pre-upgrade sessions sit in together (see
    :func:`prune_sessions`). The fallback proves ownership from
    ``run_dir_for(project, run_id)``, and a run id is only unique *within* one
    project (``--run-id`` is caller-supplied), so in a shared registry a dead run
    dir here is not evidence about a session over there: project A holding a dead
    `shared-id` would claim project B's live, untagged `bmad-loop-shared-id` and
    kill it. In a per-project registry the same fallback is sound because the
    registry itself proves ownership, which is why the flag is off by default and
    the primary pass keeps the reach it always had. What the flag leaves behind is
    reported by :func:`legacy_registry_leftovers`.
    """
    # `mux` bypasses the module-level readers rather than widening them: those
    # two are the seam every other caller (and every test) reaches the process-wide
    # backend through, and a bound instance is this function's business alone.
    tags = mux.session_options(PROJECT_OPTION) if mux is not None else session_project_tags()
    mine = accepted_tags(project)
    prunable: list[str] = []
    live: list[str] = []
    unknown: set[str] = set()
    names = mux.list_sessions() if mux is not None else mux_sessions()
    for name in names:
        run_id = _agent_run_id(name)
        if run_id is None:
            continue
        run_dir = run_dir_for(project, run_id)
        tag = tags.get(name, "")
        if tag:
            if tag not in mine:
                continue  # another project's session
        elif require_tag or not is_run(run_dir):
            continue  # ownership unprovable: no tag, and no run dir here to stand in
        liveness = engine_liveness(run_dir)
        if liveness == "alive":
            live.append(run_id)
            continue
        prunable.append(run_id)
        if liveness == "unknown":
            unknown.add(run_id)
    return prunable, live, unknown


def _registry_proves_ownership(project: Path) -> bool:
    """True when the registry the *primary* prune pass addresses is one bmad-loop
    derived for this project — which is what makes
    :func:`prunable_sessions`' untagged run-dir fallback evidence rather than a
    guess.

    That fallback claims an untagged ``bmad-loop-<id>`` session when this project
    holds a dead run dir of the same id. Run ids are only unique *within* a
    project (``--run-id`` is caller-supplied), so the claim is sound exactly when
    the registry itself already restricts what can be listed to this project's
    sessions. In a registry shared with other projects — or with the operator —
    it is not, and the same reasoning that put ``require_tag=True`` on the legacy
    pass applies here.

    The primary registry is not always ours. :func:`export_psmux_registry_root`
    degrades to ``None`` on an underivable state root and leaves whatever ambient
    ``PSMUX_DATA_DIR`` it found in force, and psmux honours any absolute value
    (``src/paths.rs``, source-read at v3.3.8) — so on that arm every verb,
    including the kill, addresses the operator's own registry while this project's
    run dirs go on looking like ownership.

    ``registry_root()`` answering ``None`` covers two cases, and they get
    **opposite** answers — conflating them was a defect, not caution. A backend
    with no registry namespace at all (tmux: one server for the machine,
    ``has_registry_namespace()`` False) keeps the reach it had before
    per-project registries existed: the listing there is exactly what it always
    was, and narrowing it would be a regression dressed as caution. A backend
    that DOES namespace and has no root in force (psmux with ``PSMUX_DATA_DIR``
    unset — the export degraded on an underivable state root and there was no
    ambient value either) is running on its transport's own **default**
    registry — shared with every other project and with the operator
    (``<home>\\.psmux``, the home being ``USERPROFILE`` when set, else the
    profile API, else ``HOMEDRIVE``+``HOMEPATH``, else ``HOME`` —
    ``src/paths.rs`` ``home_dir``, source-read at v3.3.8) — which proves nothing about
    ownership, exactly as an absolute ambient value naming a foreign registry
    proves nothing. Both shared cases make the tag mandatory.

    A backend that cannot be asked answers ``False``: the safe direction is to
    demand the tag, which leaves a session standing rather than killing one on
    evidence that may not hold.
    """
    try:
        mux = get_multiplexer()
        root = mux.registry_root()
        if root is None:
            # No namespace (tmux): historical reach. A namespace with no root
            # in force is the transport's shared default registry: demand the tag.
            return not mux.has_registry_namespace()
    except MultiplexerError:
        return False
    try:
        return root == str(mux_registry_root(project))
    except (StateRootError, OSError, RuntimeError):
        return False


def prune_sessions(
    project: Path, *, dry_run: bool = False
) -> tuple[list[str], list[str], set[str]]:
    """Kill every prunable bmad-loop-<id> session (see prunable_sessions);
    returns (killed, live, unknown): the run ids that were (or, with dry_run,
    would be) killed, the live ids skipped, and the killed subset whose engine
    liveness read 'unknown'. All three come from the same partition sample, so
    frontend messaging built from them always describes the performed actions.

    Runs once per registry: the one this process is pointed at, then each legacy
    registry the backend still admits (:func:`_legacy_registries`). Sessions
    bmad-loop created before it took a per-project psmux root are addressable
    only from the second pass, and without it cleanup would report a clean sweep
    while their servers ran on. The passes are unioned rather than concatenated —
    a run id can only be in one registry, but a backend answering the same
    registry twice must not make one kill look like two.

    Ownership is judged per pass by the same :func:`prunable_sessions` partition,
    so a legacy registry buys no extra reach: another project's sessions and the
    operator's own psmux sessions are skipped there exactly as they are here.

    The legacy pass always runs with ``require_tag=True``, and the primary pass
    runs with it whenever the registry it addresses is not one bmad-loop derived
    for this project (:func:`_registry_proves_ownership`). Both are the same rule:
    :func:`prunable_sessions`' untagged run-dir fallback is evidence only where
    the registry has already restricted the listing to this project. A legacy
    registry is shared by every project by definition; the primary one is shared
    whenever the derivation failed — an ambient ``PSMUX_DATA_DIR`` left in
    force, or nothing in force at all, where a namespacing backend runs on its
    own shared default registry. What that strictness leaves standing in a
    legacy registry is reported
    by :func:`legacy_registry_leftovers`, which the cleanup frontends print: a
    sweep that silently declines to migrate something is the same silence this
    whole change exists to remove."""
    prunable, live, unknown = prunable_sessions(
        project, require_tag=not _registry_proves_ownership(project)
    )
    if not dry_run:
        for run_id in prunable:
            kill_session(run_id)
    for legacy in _legacy_registries():
        extra, extra_live, extra_unknown = prunable_sessions(project, legacy, require_tag=True)
        if not dry_run:
            for run_id in extra:
                kill_session(run_id, legacy)
        prunable += [i for i in extra if i not in prunable]
        live += [i for i in extra_live if i not in live]
        unknown |= extra_unknown
    return prunable, live, unknown


#: How a frontend names psmux's OWN default registry, the one root
#: :meth:`~.adapters.multiplexer.TerminalMultiplexer.registry_root` deliberately
#: answers ``None`` for (respelling its home cascade in Python is a second thing
#: to keep in sync). Lives here so both frontends say it the same way.
DEFAULT_REGISTRY_LABEL = "the multiplexer's own default registry"


def legacy_registry_leftovers(
    project: Path, *, announced: Iterable[str] = ()
) -> dict[str, list[str]]:
    """Session names a legacy registry **still holds** after :func:`prune_sessions`
    ran — the migration's honest remainder, for the cleanup frontends to print.
    ``{}`` when there is no legacy registry, when they hold nothing, or when
    every listing fails.

    **Grouped by registry, and that is load-bearing.** There is more than one
    legacy registry now (:meth:`~.adapters.psmux_backend.PsmuxMultiplexer.legacy_registries`
    — psmux's default, and the root this process displaced), so a flat list
    cannot say where to go look: a message built from one would either name a
    registry the leftovers are not in, or name every registry the sweep
    addressed including the ones that contributed nothing. The operator's next
    action is to open that registry, so the answer has to be per registry. Keys
    are :meth:`registry_root`'s answer, or :data:`DEFAULT_REGISTRY_LABEL` where
    that is ``None``; a registry holding nothing is absent rather than empty, so
    a caller can print the keys without checking.

    **Presence, not a second opinion.** Called after the sweep, this lists what is
    actually there; a session the sweep killed is simply gone from the listing.
    That is the whole judgement for anything tagged as ours, and it is deliberately
    *not* a re-run of the partition: re-judging liveness would open a race the
    reader cannot see the far side of. A run alive during the prune (correctly
    left, and reported ``live``) can exit before the reader looks; a resampled
    partition would then call it ``prunable``, and it would fall out of both the
    live arm and the untagged fallback — stranded and unreported, with no kill ever
    attempted. Presence has no such gap: the session is standing, so it is named.

    What that covers, in one rule:

    - **Untagged** ``bmad-loop-<id>`` sessions. The legacy pass runs with
      ``require_tag=True`` (:func:`prunable_sessions`), so an untagged session there
      is skipped rather than claimed by a run dir that proves nothing in a shared
      registry.
    - **Ours, still standing.** Tagged this project's, and the sweep did not remove
      it — because it was live, because it exited mid-sweep, or because
      ``kill_session`` (best-effort and silent by contract) did not land. All three
      leave the same fact behind: a session of ours in a registry ordinary attach
      and cleanup no longer address. Naming it needs no cause, which is why this
      also closes the failed-kill case ``cleanup --json``'s ``sessions.removed``
      documents as an *attempted* kill.
    - **A surviving control session.** The prune never touches a ctl-named
      session (:func:`is_ctl_session_name`), and its parked windows are not swept
      in a legacy registry either — the ctl-window scan runs against the primary
      backend only. The shape question is asked through *that registry's*
      :meth:`~.adapters.multiplexer.TerminalMultiplexer.session_name_key`, never a
      constant fold: on a case-folding store ``bmad-loop-CTL-<hex>`` IS the
      control session and goes unnamed without it, while on an exact one it is a
      distinct session bmad-loop cannot have minted — naming it there would send
      the operator after somebody else's.

    Another project's tagged sessions never appear: the sweep skipping them is the
    correct outcome, not a remainder.

    ``announced`` is the one thing presence alone cannot judge: on a **dry run**
    nothing was killed, so every session the preview just announced as a would-kill
    is still standing and would be named here as if the sweep had declined it. The
    caller passes the run ids it printed — :func:`prune_sessions`' own return — and
    they are excluded.

    **Excluded only where THIS registry's own pass could have announced it**, which
    is the tagged-ours arm and only it. :func:`prune_sessions` unions the ids of
    every pass, the *primary* registry's included, so the flat set says no more
    than "some registry would kill this id" — while a legacy pass runs with
    ``require_tag=True`` and therefore cannot claim an untagged session at all.
    Applied to the untagged arm the set hid exactly the remainder this listing
    exists for: a dead ``bmad-loop-X`` the primary pass plans to kill, an untagged
    ``bmad-loop-X`` over here that the real cleanup leaves and reports, and a
    preview of that same cleanup that does not mention it.

    Inside the tagged arm the flat set is exact, so no per-registry plan has to be
    threaded down here. Liveness is read from ``run_dir_for(project, run_id)`` —
    one directory per (project, id), whatever registry the session sits in — so an
    id the primary pass judged dead the legacy pass judges dead too: if the same
    id is standing here under a tag proving ours, this pass announced it as well
    and the union merely collapsed the two.

    Passed in rather than re-derived, and that is the whole point of the parameter.
    An earlier revision re-ran the partition here to rediscover the plan, which is
    a *second sample*: a tagged legacy run seen alive by the first (so printed as
    live, never announced) can exit before this call, land in the second sample's
    prunable arm, and be excluded from a listing it should have headed — a session
    dropped from the preview outright, not merely mentioned twice. Consuming what
    the preview actually printed cannot disagree with it.

    On a real cleanup the caller passes nothing: there, a killed session is gone
    from the listing by presence, and one whose kill did not land must be named.

    Deliberately its own listing rather than a fourth arm on
    :func:`prune_sessions`. That tuple is read by two frontends and projected into
    the schema-versioned ``cleanup --json`` document; widening it is a contract
    change and ~30 call sites, against one extra pair of psmux calls against a
    registry that answers "no server" instantly on any machine that never ran the
    pre-registry build.

    Names, not run ids: the ctl session has no run id, and the operator is going to
    paste these into a ``psmux`` target — under the registry this maps them to,
    which is the other half of what makes them pasteable.
    """
    grouped: dict[str, list[str]] = {}
    mine = accepted_tags(project)
    # Run ids, so names. `prune_sessions` unions its passes, so an id it reports
    # names at most one session anywhere — the same collapse that makes its own
    # "killed" count one per id.
    excluded = {session_name(run_id) for run_id in announced}
    for legacy in _legacy_registries():
        try:
            names = legacy.list_sessions()
            tags = legacy.session_options(PROJECT_OPTION) if names else {}
        except MultiplexerError:
            continue  # observation degrades; the sweep's own report still stands
        here: list[str] = []
        for name in names:
            if is_ctl_session_name(legacy.session_name_key(name)):
                # A legacy registry holds the pre-#537 fixed name; the shape
                # predicate also names any per-registry-named stray. Asked
                # through THIS registry's own comparison key, never a constant
                # fold: whether `bmad-loop-CTL-<hex>` denotes the control
                # session is the transport's answer to give.
                here.append(name)
                continue
            if _agent_run_id(name) is None:
                continue  # not a bmad-loop agent session at all
            tag = tags.get(name, "")
            if tag and tag not in mine:
                continue  # another project's session
            if tag and name in excluded:
                continue  # a would-kill of this registry's own pass (dry run)
            here.append(name)
        if here:
            # `registry_root()` is a diagnostic and never raises (seam contract).
            # Two admitted registries could in principle answer the same label —
            # a displaced root that spells the default is swept twice — so the
            # rows are merged rather than overwritten.
            label = legacy.registry_root() or DEFAULT_REGISTRY_LABEL
            grouped[label] = sorted(set(grouped.get(label, []) + here))
    return grouped


def _legacy_registries() -> list[TerminalMultiplexer]:
    """Backends bound to registries this project's sessions may predate, or []
    (see :meth:`~.multiplexer.TerminalMultiplexer.legacy_registries`, which owns
    the concept and every backend's answer).

    Degrades to [] rather than raising: a backend that cannot even be selected
    has no legacy registry to offer, and a cleanup that already swept the primary
    registry must report that work rather than die on the migration pass."""
    try:
        return list(get_multiplexer().legacy_registries())
    except MultiplexerError:
        return []


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
    """Stop the engine generation current at completion of the gesture."""
    while True:
        result = _stop_run_once(run_dir)
        if result is not None:
            return result


def _stop_run_once(run_dir: Path) -> bool | None:
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

    **Registry scope, stated because it is easy to read past.** The stop
    itself is registry-independent: both channels address the engine *process* —
    the request file lands in the run directory, the signal on the pid recorded
    there — and a run directory is per (project, run id), not per registry. So a
    pre-upgrade run living in a legacy psmux registry stops, and a still-live
    engine tears down its own window under the registry it was launched with.
    What is scoped is the backstop below: :func:`kill_session` addresses the
    registry THIS process exported, so an agent session an already-dead engine
    leaked in a legacy registry is not reached from here and the run is marked
    stopped with that session standing.

    Deliberately not widened, and for the reason ``kill_session``'s own docstring
    gives: a by-name kill in a registry shared with other projects, without tag
    proof, could take a neighbour's same-named session — run ids are unique per
    project only. Both legacy registries are shared in exactly that sense. The
    displaced one is no exception: it is the *ambient* ``PSMUX_DATA_DIR`` this
    process found (:func:`~.adapters.psmux_backend.note_displaced_registry`), so
    a profile that exports one exports it into every project's shell and every
    one of them kept its pre-upgrade sessions there. That is why the legacy pass
    of :func:`prune_sessions` demands the tag in both, and it is the path that
    reaches such a session — ``bmad-loop cleanup``, with
    :func:`legacy_registry_leftovers` naming whatever the tag rule leaves and the
    registry it is in.
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
    addressed_engine = (pid, identity)
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
    # This is the one registry-scoped step of the stop (see the docstring): it
    # addresses the registry this process exported, and `cleanup`'s legacy pass is
    # what reaches a session left in an older one.
    kill_session(run_dir.name)

    already_stopped = False
    finished_during_stop = False
    retry_new_engine = False
    clear_request = False
    with state_lock(run_dir):
        # Authoritative post-delivery snapshot.  A rival writer that completed while
        # stop was signalling is observed here, after exclusion, rather than being
        # overwritten by the stale state loaded at entry.
        state = load_state(run_dir)
        current_engine = read_pid_identity(run_dir)
        current_liveness = engine_liveness(run_dir)
        rival_published_engine = current_liveness != "dead" and current_engine != addressed_engine
        if state.finished:
            # The engine completed while the stop channels were in flight.  Its
            # terminal state is authoritative; do not rewrite it as a fallback stop.
            finished_during_stop = True
            clear_request = current_liveness == "dead"
        elif rival_published_engine:
            # Resume publishes pid + state under this same lock.  If that happened
            # while this attempt was signalling an older generation, release before
            # delivering to the new process and retry from its fresh identity.
            retry_new_engine = True
        elif state.stopped:
            already_stopped = True
            clear_request = True
        elif engine_may_live and not lodged:
            Journal(run_dir).append("run-stop-undelivered", pid=pid)
            raise StopRunError(
                f"run {run_dir.name}: the stop request could not be written to the run "
                "directory and the engine could not be proved dead, so no stop is pending. "
                "Its agent session was killed as a backstop. Free space in the run directory "
                "and retry, or stop the process yourself"
            )
        else:
            state.stopped = True
            save_state(run_dir, state)
            clear_request = not engine_may_live

    if clear_request:
        clear_graceful_stop(run_dir)
    if finished_during_stop:
        return False
    if retry_new_engine:
        return None

    if already_stopped:
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
        return True

    # The locked branch above performed the external fallback's final
    # read-modify-write.  The journal remains outside the state transaction: it is
    # append-only observation, not part of state publication.
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
    first, so the tag query only runs on a name collision.

    A stronger shape was built and withdrawn: a proof discipline (block unless
    the transport *proves* the session absent) fell to four consecutive reviews,
    each refuting its newest proof source — the transports genuinely offer none.
    psmux's registry is advisory and self-healing (its server re-creates a
    reaped port file on a 5 s tick, source-read at v3.3.8), a binary's PATH
    presence is per-process while the server is not, and the listing is
    load-sensitive; so a "proof of absence" either wedges every removal behind
    `--force` or quietly accepts a refutable proof. The degrade above is the
    guard's owner's documented trade, kept deliberately; the measured cost of
    the unobservable-multiplexer window is filed for that owner to revisit
    rather than overturned here.

    Two registry-root-era additions on that unchanged contract:

    **The control-alias discount.** An id whose session name is one of THE
    control session's own names — the fixed :data:`CTL_SESSION`, or this
    project's :func:`ctl_session_for` — answers False before any transport
    read: that session is the control plane's, never claimed through a run
    dir, so its liveness is not evidence about the run, and blocking removal
    on it wedged exactly the recovery (`bmad-loop delete ctl`) the resume
    refusal points an operator at, for as long as the machine had a control
    session at all. This is the *instance* question, deliberately not
    :func:`run_id_aliases_control_session`'s shape question: on tmux a
    `main`-created run `ctl-<16 hex>` owns a genuine agent session distinct
    from the fixed name (measured: killing it exactly leaves `bmad-loop-ctl`
    alive), and the shape discount destroyed its run dir without ever querying
    the mux. A namespace probe that cannot answer degrades to the fixed name
    alone — the *smaller* discount, which blocks more, the safe direction. A
    discount, not a proof source: it removes non-evidence, and never clears a
    removal on transport testimony.

    **Transport-owned name comparison.** Every comparison goes through
    :meth:`session_name_key`, never a constant fold: psmux resolves names
    through a case-folding store, tmux is case-sensitive (both measured), and
    a constant ``.lower()`` discounted a persisted `CTL` run's genuinely live
    uppercase agent on tmux as "the control session" and deleted its run dir.
    On tmux the key is identity, so the listing and tag reads keep their
    historical exact comparison. Selecting that backend is itself part of the
    listing read — :func:`mux_sessions` selects inside the caught call — so it
    degrades the listing's way: a transport that cannot even be chosen (a
    persisted `[mux] backend` naming a backend no longer registered) reports
    no live session rather than aborting every removal path."""
    try:
        mux = get_multiplexer()
    except MultiplexerError:
        return False
    key = mux.session_name_key
    name = session_name(run_id)
    control = {CTL_SESSION}
    try:
        control.add(ctl_session_for(project, mux))
    except MultiplexerError:
        pass  # namespace unanswerable: only the fixed name is knowable
    if key(name) in {key(c) for c in control}:
        return False
    try:
        if key(name) not in {key(s) for s in mux_sessions()}:
            return False
    except MultiplexerError:
        return False
    try:
        tags = session_project_tags()
    except MultiplexerError:
        tags = {}  # unread is not proof of foreign
    tag = next((v for s, v in tags.items() if key(s) == key(name)), "")
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
        if entry.name == MUX_REGISTRY_DIR:
            # Not a run entry at all (`mux_registry_root`), and the one entry here
            # whose deletion costs more than the disk it reclaims: it holds the
            # `.port`/`.key` files every psmux verb resolves a session through, so
            # sweeping it while a server is up leaves that server alive,
            # unreachable, and invisible to `psmux ls` in any registry — the
            # manufactured orphan the root was moved out of the project tree to
            # avoid. Never reaped rather than reaped-when-empty: proving it empty
            # means asking every server in it whether it is alive, and this sweep
            # has no seam to the multiplexer (nor may it acquire one — it must
            # degrade to a no-op, and a transport probe cannot promise that).
            # psmux removes its own quartet on session shutdown, so what is left
            # behind is a directory of small files, not growth.
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


def rebase_recorded_project_path(path: Path, state: RunState, project_root: Path) -> Path:
    """Move a project-owned persisted path with a renamed project, lexically.

    Run state intentionally keeps the LAUNCH-TIME project string — nothing re-stamps
    `state.project` the way `restamp_code_root` re-stamps the code root — while both
    surfaces that resolve an escalation run from the LIVE CLI project. Paths outside
    the recorded project are shared/external and remain untouched. This is spelling
    arithmetic only: do not introduce filesystem canonicalization here, at either
    caller's boundary.

    Lives in `runs` rather than in `resolve` because BOTH sides of one gesture need
    the identical answer, and `resolve` already imports this module (the reverse is a
    cycle). `resolve.build_context` hands the agent a `spec_file` to edit;
    `rearm_escalation` flips the status of the file it then re-drives from. Rebasing
    one and not the other is not a cosmetic split — after a project move the agent
    edits the live copy while the re-arm writes a path under a directory that no
    longer exists, so the flip silently no-ops and the re-drive wedges on the
    escalated attempt's status.
    """
    recorded_project = Path(state.project)
    if project_root == recorded_project:
        return path
    try:
        relative = path.relative_to(recorded_project)
    except ValueError:
        return path
    return project_root / relative


def task_spec_path(task: StoryTask, state: RunState) -> Path:
    """The persisted-task spec anchor, re-based on the tree it was recorded relative to.

    Use this when an operation must address the tree recorded by the task. A bare
    basename is joined directly to :func:`task_spec_root`; it is NOT probed against the
    project and has no implementation-artifacts fallback. To bind either a
    session-reported path or a persisted spelling inside the current active
    ``ProjectPaths``, use :func:`verify.resolve_spec_path`, which probes project-first
    and then falls back under the implementation-artifacts directory. Recovery uses
    ``recovery_flow.RecoveryFlow._attempt_owned_spec`` instead: for a bare basename it
    probes both locations and accepts the binding only when exactly one trusted regular
    file exists.

    `StoryTask._serialized_worktree_path` (`model.py`) persists a worktree-local spec
    RELATIVE to the mounted worktree root, and `from_dict` reads it back raw. Resolving
    that against the process cwd is not merely unreachable — it is actively wrong:
    `bmad-loop resolve` runs from the project root, where the MAIN CHECKOUT carries the
    same implementation-artifacts-relative path, so a bare `Path(task.spec_file)` names
    the main checkout's copy of the story spec. `is_file()` then answers True, `confine_root`
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
    `task_spec_path` passes through — all FOUR writers of these bytes would silently take
    the plain no-follow arm and lose #593's O_NOFOLLOW walk.

    The project can often confine it. Where nothing can, every writer of these bytes
    lands on the arm it already took — they all select LEXICALLY, so an out-of-root path
    simply takes the plain no-follow write as before. That parity is load-bearing and was
    once broken: `_restore_rearmed_spec` called `atomic_write_bytes_confined` DIRECTLY
    with no lexical arm, so for a spec outside both the mount and the project — the shared
    artifact dir `_spec_is_shared_with_the_redrive` treats as first-class and reachable —
    the flip, the strip and the re-stamp all LANDED while the re-arm's undo alone raised
    `UnconfinedWriteError`, losing the rollback on precisely the specs it could still
    break. A writer that refuses where its siblings write does not add safety here; it
    subtracts the transaction. Do not re-introduce the asymmetry by "hardening" one of
    the four in isolation.

    The arm is not unconditionally an improvement either, and that exception is graded by
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


def task_stories_root(task: StoryTask | None, state: RunState) -> Path:
    """The tree this run's STORIES FOLDER lives in — the workspace root, not a
    confinement root.

    Deliberately NOT `task_spec_root`, which the sentinel and stories-block readers
    used to borrow. That function answers "which tree can CONFINE a write to
    `task.spec_file`", and its out-of-mount arm falls back to the project precisely so
    a `confine_root` can never fail to contain the anchored path. Reusing that answer
    here imported a write-confinement decision into a READ of a different file: for an
    isolated run whose `spec_file` is absolute and lexically outside the mount — the
    shape `model._serialized_worktree_path` persists verbatim, reachable whenever a
    symlinked component makes a spec that physically lives in the mount look outside
    it, since `verify.resolve_spec_path` deliberately does not `.resolve()` — the
    stories folder would be looked up in the MAIN CHECKOUT while
    `stories_engine._stories_folder` answers the worktree for the same task. One
    surface would then describe two trees, which is the exact defect the spec anchor
    exists to close.

    So this mirrors `_stories_folder`'s own rule instead: the mount whenever the task
    holds one, the project otherwise. `spec_file` does not enter into it — the stories
    folder is located by `state.spec_folder` relative to the workspace root, and a
    task's spec being elsewhere says nothing about where its story manifest lives.

    A mount that is GONE degrades to the project. `worktree_path` is cleared at
    exactly one site in the engine — the restart discard — so a task that reached a
    terminal phase through successful integration keeps naming the unit worktree its
    teardown already removed. The `done_checkpoint` pause is raised in precisely that
    window, and the TUI reads this for the checkpoint card's title and description, so
    trusting the stale field lost the committed story's manifest to a deleted
    directory while the merged copy sat in the project checkout.

    Answering on filesystem state is right HERE and would be wrong in
    `task_spec_root`: that one is a write-confinement root, where a value that moves
    under a `mkdir` is not a definition. This is a READ locator, and observation
    degrades rather than raising — a probe that cannot answer falls back to the tree
    that always exists.

    Accepts `None` so the two call sites do not each re-spell the no-task fallback.
    """
    if task is None or not task.worktree_path:
        return Path(state.project)
    mount = Path(task.worktree_path)
    try:
        if not mount.is_dir():
            return Path(state.project)
    except OSError:
        return Path(state.project)
    return mount


def live_spec_path(task: StoryTask, state: RunState, project_root: Path) -> Path:
    """`task_spec_path` carried onto the tree the caller is acting in.

    The pair below is the WRITE side of `rearm_escalation`: the file it flips and
    re-stamps, and the root every writer confines that edit to. Public because the
    TUI's escalation modal is the READ side of that same gesture: it shows and
    validates the spec `_do_rearm` then flips, so it must anchor on the identical
    live path or the operator reviews one copy and re-arms another. They move together
    because `task_spec_root` is the confinement claim about the very path
    `task_spec_path` produces — rebasing one alone would hand the writers a path
    outside their own root, and all four of them answer that by silently dropping to
    the unconfined arm (see `task_spec_root` for why that matters).
    """
    return rebase_recorded_project_path(task_spec_path(task, state), state, project_root)


def live_spec_root(task: StoryTask, state: RunState, project_root: Path) -> Path:
    """`task_spec_root` carried onto the tree the caller is acting in — the confine
    root for the path `live_spec_path` names. See there."""
    return rebase_recorded_project_path(task_spec_root(task, state), state, project_root)


def live_stories_root(task: StoryTask | None, state: RunState, project_root: Path) -> Path:
    """`task_stories_root` carried onto the tree the caller is acting in — the root
    the stories folder is located from by the READ side of the re-arm gesture.

    The escalation modal's title, description and sentinel indicator are read from
    the stories folder, and `_do_rearm` clears that sentinel at `live_spec_path`. A
    locator answering the recorded `state.project` after a project move reads the
    manifest from a tree the re-arm no longer writes: absent once the old tree is
    gone, stale while it lingers. A mount is a path under the run dir, outside the
    recorded project, so `rebase_recorded_project_path` passes it through unchanged
    and the isolated arm keeps its answer; only the project fallback moves.
    """
    return rebase_recorded_project_path(task_stories_root(task, state), state, project_root)


def _spec_is_shared_with_the_redrive(state: RunState, task: StoryTask) -> bool:
    """True when the recorded spec lives outside BOTH checkouts, so the re-arm's status
    flip survives a mount's disposal and the ISOLATED re-drive reads it.

    Asked only of a re-drive that will mount (`spec_reaches_the_redrive`'s isolated
    arm), and deliberately not of a task that HAS a mount: those are two different
    questions, and a policy flip separates them. A run switched from `isolation = "none"`
    to `"worktree"` while an escalation is paused re-drives isolated with no mount
    recorded at all, and the recorded spec is then measured against the project alone —
    which is the whole point, since the fresh worktree is cut from git and reads no
    working tree.

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
    `StoryTask._serialized_worktree_path` persists a spec RELATIVE whenever it sits under
    the mounted worktree (and, with no mount, whenever the run recorded it relative to
    the project), and verbatim (absolute) otherwise — so an absolute value is the only
    shape that can be shared. But that relativize is a LEXICAL `relative_to` against the
    same `worktree_path` read here, so all an absolute value proves is that the two
    spellings did not share a prefix. A spec reported through a symlink or a `..` segment
    sits inside the worktree and is persisted absolute all the same, and answering
    "shared" for it would suppress the warning on a spec that really is destroyed with
    the worktree.

    So containment is decided on the CANONICAL paths, and a host that cannot canonicalize
    one of them answers "not shared". That degrade is the safe direction and the reason
    this does not use `resolve_or_lexical`: its fallback is `absolute()`, which does not
    fold `..`, so a spec spelled through either checkout would come back looking external
    and go silent — trading a wrong warning for no warning at all."""
    raw = Path(task.spec_file or "")
    if not raw.is_absolute():
        return False
    try:
        # the house pair — `resolve()` raises RuntimeError, not OSError, for a symlink
        # loop on the 3.11/3.12 floor
        real = raw.resolve()
        if real.is_relative_to(Path(state.project).resolve()):
            return False
        if task.worktree_path and real.is_relative_to(Path(task.worktree_path).resolve()):
            return False
        return True
    except (OSError, RuntimeError):
        return False


def _spec_is_inside_the_mount(task: StoryTask) -> bool:
    """True when the file `task_spec_path` names sits INSIDE the mount this task
    recorded — so a write to it cannot reach an IN-PLACE re-drive, which reads the main
    checkout.

    The mirror of `_spec_is_shared_with_the_redrive`, for the other arm of
    `spec_reaches_the_redrive`. Reachable only through a policy flip: a run switched
    from `isolation = "worktree"` to `"none"` while an escalation is paused still
    carries the escalated attempt's `worktree_path`, so `task_spec_path` re-anchors the
    edit on that mount while `engine._run_story` re-runs the story in the main checkout.
    `_finish_inflight` releases the mount-owned spelling at RESUME, which is after
    `bmad-loop resolve` has already written the context and re-armed — this is what the
    human and the agent are told in the meantime.

    Unlike the shared test, containment inside the PROJECT is not disqualifying: an
    in-place re-drive reads the main checkout's working tree, so a spec anywhere the
    project can see it reaches. Only the mount is out of reach.

    A relative spelling beside a recorded mount is inside it BY CONSTRUCTION —
    `_serialized_worktree_path` relativizes exactly when `relative_to(worktree_path)`
    succeeds — so it needs no filesystem probe and gets none. Absolute spellings are
    canonicalized for the same reason the shared test does it (a `..` segment or a
    symlinked component puts a physically-inside path outside lexically), and a host
    that cannot canonicalize degrades to "inside": the safe direction here is the one
    that WARNS, matching the shared test's own degrade.
    """
    if not task.worktree_path:
        return False
    raw = Path(task.spec_file or "")
    if not raw.is_absolute():
        return True
    try:
        return raw.resolve().is_relative_to(Path(task.worktree_path).resolve())
    except (OSError, RuntimeError):
        return True


def redrive_base_ref(state: RunState, *, isolated_redrive: bool) -> str:
    """The ref whose committed tree the re-drive will actually read this unit's spec
    from: the run's PINNED `target_branch` when the re-drive will MOUNT, ``HEAD``
    otherwise.

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

    `isolated_redrive` is the LIVE policy's isolation mode, injected by the caller, and
    the task drops out of the signature entirely. It used to be inferred from
    `task.worktree_path` — a recorded mount — and that is the retrospective fact, not
    this one. `engine._run_story` selects the mode from `self._isolated` alone, and an
    isolation change mid-run is journalled, never refused, so the recorded mount and the
    next re-drive part company in BOTH directions: a run flipped to `"none"` still
    carries the escalated attempt's mount and would name the pinned branch for an
    in-place re-drive that reads `HEAD`, and one flipped to `"worktree"` carries no
    mount at all and would name `HEAD` for a re-drive that mounts. Both send a
    correction to a tree the run does not read. The same injection is how
    `validate_restore_latch` already learns this fact.

    That the caller must supply it is the point: `bmad-loop resolve` computes this
    context in a SEPARATE process, before the resume ever runs, so no amount of
    resume-time bookkeeping on `task.worktree_path` could have reached it. The fact
    enters the pure core as a parameter and nothing here reads policy.

    An empty `target_branch` beside an isolated re-drive is a MISSING value, not a
    divergent one: `ensure_target_branch` pins the field before any worktree mounts, so
    only a state.json predating it can reach here, and that shape degrades to exactly
    the ref it read before — the same migration `restamp_code_root` gives an unrecorded
    root. Answering ``""`` instead would hold the resume on a per-configuration
    constant, the failure the record's narrowing exists to avoid.
    """
    if isolated_redrive and state.target_branch:
        return state.target_branch
    return "HEAD"


def spec_reaches_the_redrive(task: StoryTask, state: RunState, *, isolated_redrive: bool) -> bool:
    """Whether an edit to this task's spec survives to the re-drive that reads it.

    The other half of `task_spec_path`'s answer, and the two ask different questions of
    different sources. That one is RETROSPECTIVE — which tree owns the state this task
    already persisted — and reads the recorded mount, correctly. This one is
    PROSPECTIVE, so it reads `isolated_redrive`: the live policy's mode, injected by the
    caller exactly as `redrive_base_ref` and `validate_restore_latch` take it.

    Both arms are about the same gap between where the edit LANDS (`task_spec_path`) and
    where the re-drive READS:

    - the re-drive will MOUNT: it reads the COMMITTED tree of a fresh worktree, so only
      a spec outside both checkouts is one file they share
      (`_spec_is_shared_with_the_redrive` carries that argument in full). True whether
      or not a mount is recorded — a run flipped to `isolation = "worktree"` mid-pause
      has none, and its working-tree edit vanishes just as silently.
    - the re-drive runs IN PLACE: it reads the main checkout's working tree, so the edit
      reaches unless it landed inside a recorded mount (`_spec_is_inside_the_mount`) —
      the flip in the other direction.

    Public because `resolve.build_context` needs it for the same reason
    `rearm_escalation` does: the context hands a human and an agent a `spec_file` to
    edit, and an edit to a doomed copy is worse than no edit — it looks like it landed.
    """
    if isolated_redrive:
        return _spec_is_shared_with_the_redrive(state, task)
    return not _spec_is_inside_the_mount(task)


def _upstream_artifacts_folder(state: RunState) -> Path:
    """The folder holding the UPSTREAM stories artifacts a sentinel's correction goes
    into — anchored on the project, never on a mount.

    Deliberately NOT `task_stories_root`, which answers "which tree does this RUN read
    its manifest out of" and is the mount whenever the task holds one. This answers
    "which folder does the CORRECTION land in", and `resolve.run_session` settles that
    independently of the mount: the agent runs with `cwd=project` and the artifacts are
    named by a project-relative `state.spec_folder`, so the writes go to the main
    checkout even for a task that recorded a worktree. An absolute `spec_folder` — the
    external-artifact-dir layout `[stories] source` allows — is left where it is, which
    is what `resolve_spec_folder` already does and what makes it shared across
    checkouts.

    One locator for all three consumers (the gate, the proof, and the journal record)
    so a record can never name a folder its own gate did not measure.
    """
    from .stories import resolve_spec_folder

    return resolve_spec_folder(Path(state.project), state.spec_folder)


def stories_reach_the_redrive(task: StoryTask, state: RunState, *, isolated_redrive: bool) -> bool:
    """Whether an edit to this run's UPSTREAM stories artifacts survives to the re-drive.

    `spec_reaches_the_redrive` asked of `SPEC.md` / `stories.yaml` instead of the frozen
    spec, for the one wedge where the spec is not the artifact being corrected: a
    fixed-slug pre-planning-halt SENTINEL. A sentinel is cleared by DELETION, so the
    re-arm drops `task.spec_file` and there is no spec write whose reachability that
    helper could measure — which is why its arm is an `else` this path never entered,
    and why no hold ever fired for a sentinel. But the correction that stops the
    sentinel RECURRING is upstream, in the artifacts `bmad-loop-resolve/SKILL.md` sends
    the agent to instead of the sentinel, and it faces the identical gap: an isolated
    re-drive mounts fresh from `redrive_base_ref` and re-plans from a COMMITTED tree, so
    an uncommitted upstream edit is invisible and the re-plan mints the sentinel again.

    The two arms are NOT the spec question's, and the difference is where the write
    lands. `task_spec_path` re-anchors a spec write ON the recorded mount, so a policy
    flip separates writer from reader in BOTH directions. The upstream artifacts are
    named by a project-relative `state.spec_folder` and `resolve.run_session` runs the
    agent with `cwd=project`, so the correction lands in the MAIN CHECKOUT whichever way
    the flip went. That collapses one arm:

    - the re-drive runs IN PLACE: it reads the main checkout's working tree —
      `stories_engine._stories_folder` anchors a relative folder on the live workspace
      root, which is the project under `isolation = "none"`. Writer and reader are the
      same tree, so the edit reaches. The recorded mount does not enter into it; a run
      flipped `"worktree" -> "none"` mid-pause still carries one, and it is not where
      the correction went.
    - the re-drive will MOUNT: the fresh worktree is cut from git and checks out TRACKED
      files, so no working-tree write reaches it — with the single exception
      `_spec_is_shared_with_the_redrive` carries in full, an artifact dir configured
      OUTSIDE the project tree, which `ProjectPaths.rebased` leaves exactly where it is
      and every worktree therefore reads through the same absolute path. True whether or
      not a mount is recorded: a run flipped `"none" -> "worktree"` has none, and its
      working-tree edit vanishes just as silently.

    Both roots are tested on the mounting arm for the same reason that helper tests
    both: worktrees normally sit under `<project>/.bmad-loop/runs/`, but
    `workspace.open_unit_workspace` stores a `.resolve()`d path, so a symlinked
    `.bmad-loop` puts the mount outside the project and "outside the project" alone
    would not be "shared".

    Canonicalized because a `..` segment or a symlinked component puts a
    physically-inside path outside lexically, and a host that cannot canonicalize
    degrades to UNREACHABLE — the direction that warns, matching the degrade both spec
    helpers already chose.
    """
    if not isolated_redrive:
        return True
    try:
        real = _upstream_artifacts_folder(state).resolve()
        if real.is_relative_to(Path(state.project).resolve()):
            return False
        if task.worktree_path and real.is_relative_to(Path(task.worktree_path).resolve()):
            return False
        return True
    except (OSError, RuntimeError):
        return False


# The two upstream artifacts `bmad-loop-resolve/SKILL.md` names for a sentinel wedge:
# the epic spec and the story manifest the planner reads. Fixed names, discovered as
# siblings in the spec folder (`stories.STORIES_FILENAME`'s own docstring says so), so
# the proof below can name them without parsing anything.
_UPSTREAM_ARTIFACTS = ("SPEC.md", "stories.yaml")


def _redrive_reads_the_upstream_artifacts(state: RunState) -> bool:
    """PROOF that the tree the re-drive re-plans from already carries this checkout's
    upstream artifacts byte-for-byte. ``False`` on every uncertainty.

    `_redrive_spec_status`'s counterpart for the sentinel path, and it exists for the
    same reason: without it the record its caller writes is a per-configuration
    CONSTANT. Every isolated stories run resolves its spec folder inside the project,
    so `stories_reach_the_redrive` answers "unreachable" for 100% of sentinel re-arms
    under `isolation = "worktree"` — and that record now HOLDS THE RESUME
    (`rearm_holds_the_resume`), so an unnarrowed gate would not merely train the
    operator to scroll past a warning, it would turn every one of those re-arms into a
    two-command gesture for an outcome nothing decided. That is the exact failure the
    spec arm's own narrowing exists to avoid, and it is worse here.

    There is no status to read for a sentinel — it is cleared by deletion and the
    re-plan routes on nothing — so the proof is byte equality instead: if the ref the
    fresh worktree is cut from already holds what this checkout holds, the re-drive
    re-plans from exactly the tree the operator is looking at and there is nothing left
    to commit. If it does not, the operator has upstream work the re-drive will not read.

    Read at `redrive_base_ref` and NOT at the code root's `HEAD`, for the reason that
    function documents: an operator who checks out another branch while the escalation
    is paused moves `HEAD` off the tree the re-drive reads, in either direction. It is
    asked for the MOUNTING mode unconditionally, and takes no `isolated_redrive` to say
    so, because there is exactly one reachable caller and it has already established
    that: `stories_reach_the_redrive` answers "reaches" for every in-place re-drive, so
    the `and` short-circuits before this runs. Carrying a second in-place arm here would
    not be defence in depth — it would SHADOW that one, leaving the reachability arm
    ungraded by any test and a wrong answer there invisible.

    Every uncertainty answers ``False`` so the record fires and the resume holds: a
    folder outside the code root (which includes the external artifact dir, already
    exempted one gate earlier as SHARED), an unreadable working-tree file, an untracked
    or non-blob path at that ref (the read answers ``None``, which no byte string
    equals), or any `GitError` — including the project simply not being a repository. Suppression requires proof that the work is already done.

    The blob is materialized through `worktree_file_bytes_at_revision`, not read raw:
    that function exists for precisely this comparison — a live checkout file against
    its committed counterpart — because Git's smudge, EOL and working-tree-encoding
    filters mean a byte-exact LF blob is legitimately a CRLF file on disk under
    `core.autocrlf=true`. Comparing raw blob bytes would mismatch every artifact on a
    Windows checkout and re-create, on one platform, the constant this narrowing exists
    to prevent.
    """
    base = _upstream_artifacts_folder(state)
    code_root = state.code_root
    ref = redrive_base_ref(state, isolated_redrive=True)
    for name in _UPSTREAM_ARTIFACTS:
        live = base / name
        try:
            rel = live.relative_to(code_root).as_posix()
        except ValueError:
            return False
        try:
            committed = verify.worktree_file_bytes_at_revision(code_root, ref, rel)
        except verify.GitError:
            return False
        try:
            working = live.read_bytes()
        except OSError:
            return False
        if committed != working:
            return False
    return True


def _restore_rearmed_spec(
    spec_path: Path, original: bytes | None, task: StoryTask, state: RunState
) -> Literal["restored", "unchanged", "unknown"]:
    """Put back the bytes a re-arm FOUND on the spec, and say what is now on disk.

    `rearm_escalation` holds an invariant its own refusals depend on: an aborted re-arm
    leaves the spec byte-identical, so the escalation stays armed and the human can fix
    the file and re-run resolve. That used to be earned twice over — by SEQUENCING for
    the two refusals that raise before `devcontract.strip_auto_run_result` runs (which is
    why that strip is still deliberately ordered after the flip's read-back check), and
    by two hand-placed calls to this function for the two that could not be sequenced out
    of the hazard. Neither half covered the rest of the window: a `journal.append`
    OSError from the residue pass, a non-Git fault from the commits probe, or a failing
    `save_state` each escaped with the flip published and the task still ESCALATED.

    So there is now ONE caller, `_rollback_rearm`, invoked from the transaction guard
    that spans the whole window from the first spec write to `save_state`. The sequencing
    is not redundant — it is what keeps those two refusals from ever writing in the first
    place — but the undo no longer depends on someone remembering to place it.

    THREE outcomes rather than a bool, because the operator surfaces make a CLAIM about
    the file and only one of the non-restoring cases entitles them to it:

    * ``"restored"`` — a write had landed and was put back.
    * ``"unchanged"`` — the file was READ and PROVED byte-equal to `original`, so nothing
      landed, nothing is rewritten, and the mtime is left alone. This is the only answer
      that licenses "the spec was left exactly as the re-arm found it".
    * ``"unknown"`` — this undo cannot say. Two shapes reach it. `original` is `None`,
      meaning the spec was unreadable at capture time or the re-arm never entered the
      spec block at all (the sentinel-clear leg, which has already UNLINKED the file —
      so a reader told "unchanged" there would be told a deleted file proves the tree is
      untouched). Or the spec is gone or unreadable NOW, in which case the undo could not
      even look: recreating a file another process removed would fight a concurrent actor
      rather than restore this function's own edit, so it declines — but declining is not
      the same as proving nothing landed.

    Folding those into one "nothing had to be put back" answer is what made the notice
    overclaim, so the distinction lives in the return type rather than in a comment.

    Byte-verbatim, never the text writer: `atomic_write_text_confined` would re-encode and
    translate newlines, so a CRLF spec would come back subtly different from the file this
    re-arm found.

    And it picks its arm the SAME LEXICAL WAY the three writers it undoes do
    (`frontmatter.set_frontmatter_status` states the rule; `verify.set_frontmatter_field`
    and `devcontract._atomic_write_spec` restate it): under `confine_root`, through the
    component-walking confined helper (#593); outside it, the plain `follow_symlinks=False`
    write. Calling the confined helper unconditionally looked stricter and was strictly
    worse — an artifacts folder configured OUTSIDE both the mount and the project is
    supported configuration (`bmadconfig` resolves one, `verify.spec_within_roots` trusts
    it, `_spec_is_shared_with_the_redrive` treats it as first-class), and there the flip,
    the strip and the re-stamp all LAND while this undo alone raised
    `UnconfinedWriteError`. The undo then reported `failed` on exactly the specs it was
    able to break, which is the asymmetry `task_spec_root`'s docstring used to name as
    out of scope. A restore that refuses where the writes succeeded is not extra safety;
    it is the transaction's write set going unhonoured.

    A restore that itself fails RAISES rather than degrading — the spec is then
    half-written and only the operator can settle it, which is the loudest thing this can
    be. `UnconfinedWriteError` is an `OSError`, so the one arm still covers both.

    `unknown` is reserved for the two shapes that are ANSWERS rather than failures to
    look: nothing was captured, and the spec is gone. A read that merely could not be
    performed is neither, and must not short-circuit the undo — see the `except` arms.
    """
    if original is None:
        return "unknown"
    try:
        if spec_path.read_bytes() == original:
            return "unchanged"
    except FileNotFoundError:
        # The one read fault that is an ANSWER about the disk: the spec is gone, so there
        # are no bytes carrying this re-arm's flip and nothing for the undo to put back.
        return "unknown"
    except OSError:
        # Every other read fault (EIO, EMFILE, a transient EACCES) says nothing about
        # what is ON DISK — and this read is only the "already identical, skip the write"
        # shortcut. Answering `unknown` here abandoned the restore on exactly the runs
        # that still needed it: the spec keeps the flip and the stripped result section
        # while `save_state` leaves the story ESCALATED, which is the split state this
        # whole transaction exists to prevent. Fall through and attempt the write; it
        # raises `RearmError` if it cannot land, which is the loud outcome the docstring
        # above promises. The cost of being wrong here is one redundant identical write.
        pass
    confine_root = task_spec_root(task, state)
    try:
        if spec_path.is_relative_to(confine_root):
            atomic_write_bytes_confined(
                spec_path, original, confine_root=confine_root, require_writable_target=True
            )
        else:
            atomic_write_bytes(
                spec_path, original, follow_symlinks=False, require_writable_target=True
            )
    except OSError as e:
        raise RearmError(
            f"cannot restore {spec_path} after a failed re-arm "
            f"({e.__class__.__name__}: {e}) — the spec may carry this re-arm's status "
            "flip and may have lost its `## Auto Run Result` section, while the story is "
            "still escalated; restore the spec from git or from your own copy, then "
            "re-run resolve"
        ) from e
    return "restored"


def _rollback_rearm(
    journal: Journal,
    story_key: str,
    spec_path: Path | None,
    spec_before: bytes | None,
    task: StoryTask,
    state: RunState,
    error: BaseException,
) -> None:
    """Undo an aborted re-arm's spec writes and RECORD that the re-arm aborted.

    The error-path half of `rearm_escalation`'s transaction. Its caller re-raises the
    original fault immediately after, so nothing here may return a verdict or swallow
    one: this function's whole job is to leave the spec's bytes as the re-arm found them
    and put the fact on the run's audit trail.

    `rollback` goes ON the record, not left to be re-derived, because every reader is
    OUT of process: `rearm_event_notice` renders from a journal line alone, with neither
    the task nor the tree to consult, and the outcomes need different sentences.
    `restored` and `unchanged` are `_restore_rearmed_spec`'s own answers and both mean
    the spec on disk is what the re-arm found. `failed` means the restore itself could
    not write, the one outcome that can leave a HALF-WRITTEN spec — recorded from a
    `finally` for exactly that reason, since that arm re-raises. `unknown` means no such
    claim is available: either this re-arm never resolved a spec path at all, or the undo
    could not read the file to prove anything.

    The sentinel-clear leg lands in `unknown` and that is the point. It runs INSIDE the
    guarded window but writes no spec bytes — it UNLINKS the sentinel — and the
    transaction deliberately does not undo it (`_clear_sentinel` preserves a copy under
    `{run_dir}/sentinels/` and a retried resolve re-clears it idempotently). What the
    transaction covers is the spec's BYTES from the first spec write onward; the deletion
    is outside that, so the record must not claim the tree is as the re-arm found it.

    The record is journalled through a `finally` and every `Exception` from that append
    is suppressed. Recording an abort is an OBSERVATION, and an observation that cannot
    be made must not replace the fault the operator is being told about — while a restore
    failure is a repair write, and repair writes raise.

    `Exception` and not `OSError`, which is the ONE place in this transaction where the
    breadth is deliberately NARROWER than the guard's own `BaseException` and, at the
    same time, wider than a filesystem taxonomy. Wider, because `Journal.append`
    serializes caller-supplied values and opens a file: a `TypeError` or `ValueError` out
    of `json.dumps`, or anything else this append can raise, would otherwise REPLACE the
    fault the whole record exists to report — the `Always:` re-raise invariant the two
    pinned `MemoryError` tests depend on. Narrower, because `KeyboardInterrupt` and
    `SystemExit` must still leave: by the time this `finally` runs the rollback has
    already completed, so an interrupt here cannot reproduce DW-79/DW-83, and discarding
    the operator's Ctrl-C to keep a breadcrumb would be the worse trade.
    """
    # `unknown` is the floor, not `unchanged`: a re-arm that resolved no spec path made
    # no claim about any file, and the surfaces must not manufacture one for it.
    rollback = "unknown"
    try:
        if spec_path is not None:
            rollback = _restore_rearmed_spec(spec_path, spec_before, task, state)
    except BaseException:
        rollback = "failed"
        raise
    finally:
        try:
            journal.append(
                "rearm-aborted",
                story_key=story_key,
                # `""` rather than the key again when the re-arm never resolved a spec
                # path: the field is the SPEC's locator on every other `rearm-*` kind,
                # and a reader that finds a story key there would alias it into the wrong
                # namespace and render it as a spec that does not exist.
                spec_file=str(spec_path) if spec_path is not None else "",
                error=f"{error.__class__.__name__}: {error}",
                rollback=rollback,
            )
        except Exception:  # nosec B110 - the OBSERVATION must not replace the fault
            # See the docstring: the ORIGINAL re-arm fault wins over ANY ordinary
            # failure of this append, not just a filesystem one. `KeyboardInterrupt`
            # and `SystemExit` are not `Exception` and still propagate.
            pass


def _rearm_commit_landed(run_dir: Path, story_key: str, task: StoryTask) -> bool:
    """Did `save_state` already COMMIT this re-arm, despite the fault now unwinding?

    `journal.save_state` ends in `atomic_replace`, so the commit is a single rename that
    either happened or did not — but the CALL can still fail after it: a `KeyboardInterrupt`
    delivered between that rename and the return unwinds through the transaction guard
    with `state.json` already describing a PENDING, re-armed task. Rolling the spec back
    there does not restore the pre-re-arm world; it MANUFACTURES the mirror image of
    DW-79/DW-83 — persisted state re-armed against a spec that is not — and then reports
    it as "nothing was persisted, the story is still escalated", which is simply false.

    The guard cannot know this from control flow (no assignment after `save_state` runs
    on that path), so it ASKS THE DISK, which is the only witness of a rename. Both the
    bumped `generation` and the reset `phase` must match the object `save_state` was
    handed: `generation` alone would be satisfied by a state file this call never wrote
    only if some other writer had minted the same bump, and `phase` alone moves for
    reasons a re-arm does not own.

    Those two conjuncts are a sufficient identity because the entire re-arm — including
    this error-path probe — runs inside :func:`journal.state_lock`. Every state writer
    participates through the self-locking :func:`journal.save_state`, and every external
    read-modify-write gesture holds the same canonical run sidecar from its deciding read
    through publication. Therefore no rival can supply the observed generation/phase
    while this transaction is in flight: a waiter reloads only after this hold exits.

    The two operator call sites still repeat liveness under their outer transaction
    holds. That is a separate safety rule: serialization prevents stale publication,
    while liveness prevents deliberately taking a turn after an engine known to be live.
    The portability guard keeps both the writer/transaction inventory and the two re-arm
    surface gates executable rather than relying on this prose.

    Degrades to `False` — roll back, the pre-existing behavior — on ANY failure to read
    or parse the state file. This is observation feeding a repair decision, and the safe
    default is the one that leaves the spec as the re-arm found it: a re-arm that did
    NOT commit and is wrongly believed to have is the DW-79/DW-83 defect itself, while
    the converse leaves a rolled-back spec beside committed state that the next resume
    re-drives from a spec still carrying the escalated status — recoverable, and loud.

    ANY failure means `BaseException`, and that breadth is the whole reason this probe is
    safe to call where it is called. Its ONE call site sits inside the transaction guard's
    `except BaseException` arm and runs BEFORE `_rollback_rearm`, so a fault escaping this
    function escapes the guard too and the rollback never happens — leaving exactly the
    spec-flipped-against-an-ESCALATED-task state the guard exists to end, now reached by
    the code added to prevent its mirror image. `load_state` reads and parses a file, so a
    `KeyboardInterrupt` or `SystemExit` delivered anywhere in it is not hypothetical, and
    under `except Exception` it took precisely that path.

    Swallowing an interrupt here is therefore the correct trade, and it is not a lost
    Ctrl-C: the rollback is a REPAIR WRITE that must not be skipped, and the guard's own
    `raise` still propagates the original re-arm fault immediately afterwards, so the
    process still exits loudly — one spec-sized write later. This is the reverse of the
    trade `_rollback_rearm`'s abort-record append makes, and the two are consistent
    because the acts differ: recording is an observation and must never displace a fault,
    while repairing is a write whose omission IS the defect. The abort record's append
    remains the ONE place in this transaction whose breadth is narrower than the guard's.

    No `rearm-aborted` record is written on the committed path either (the caller skips
    the whole rollback). Every rendering of that kind asserts that nothing was persisted;
    there is no value of `rollback` that is true here, and inventing one would put a
    false sentence on both operator surfaces rather than leave the fault to speak.
    """
    try:
        persisted = load_state(run_dir).tasks.get(story_key)
    except BaseException:
        # See the docstring: a fault escaping this probe escapes the guard arm that
        # calls it and skips the rollback entirely, so an interrupt is absorbed here
        # and the original fault still propagates from the guard's `raise` below.
        return False
    return (
        persisted is not None
        and persisted.generation == task.generation
        and persisted.phase == task.phase
    )


def _redrive_spec_status(state: RunState, task: StoryTask, *, isolated_redrive: bool) -> str:
    """The spec's status AS THE RE-DRIVE WILL READ IT, or ``""`` when unprovable.

    The proof that decides whether the operator still has anything to do, so it has to
    read the same file the caller's remedy names — otherwise the record holds a resume
    over work that is already done, or clears on work that is not.

    Two sources, because the two re-drive modes read two different things:

    * MOUNTING: the fresh worktree is cut from git and checks out TRACKED files only, so
      it reads the COMMITTED spec and never a working-tree write. Anchored on
      `state.code_root` — the same tree the baseline advance reads — at the ref
      `redrive_base_ref` names, the run's pinned `target_branch` rather than that tree's
      current `HEAD`.
    * IN PLACE: the story re-runs in the main checkout, which reads its WORKING TREE. A
      commit is neither required nor sufficient there, so measuring the committed tree
      would hold the resume until the operator committed a correction the re-drive would
      have read uncommitted — and `rearm_event_notice`'s in-place remedy tells them to
      do exactly that (re-apply it in the main checkout, no commit), so a committed-only
      proof would make the record's own instruction unable to clear it.

    Reached only when the write does NOT reach the re-drive, so the in-place arm is
    always the isolation-flip shape: the flip's write landed in the mount the escalated
    attempt recorded while the re-drive reads `state.project`. That is the tree
    `task_spec_root` answers for a task with no mount, which is what the resume makes
    this task once `release_mount_owned_state` runs.

    Degrades to ``""`` on every uncertainty: a spec recorded absolute (nothing names
    its position in the tree), an absent or non-blob path at that ref, a non-UTF-8 blob,
    or any `GitError` — which includes the project simply not being a repository, and a
    `target_branch` the code root no longer carries. ``""`` never equals a target
    status, so the caller's record still fires. Suppression therefore requires PROOF
    that the work is already done, and the non-repo case stays non-fatal, as the story's
    Boundaries require.

    Degrades to ``""`` on every uncertainty in BOTH arms, including a spec recorded
    absolute. That arm is narrower than it looks: the caller has already answered the one
    absolute shape whose write the re-drive DOES read — the shared external spec — with
    `_spec_is_shared_with_the_redrive`. What still reaches here is an absolute spelling
    of a path inside one of the two checkouts, which is genuinely unreachable, and whose
    position in the re-drive's tree nothing here can name, so degrading it to a warning
    is the right answer rather than a gap.
    """
    raw = Path(task.spec_file or "")
    if not task.spec_file or raw.is_absolute():
        return ""
    if not isolated_redrive:
        try:
            text = (Path(state.project) / raw).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        return status_of(parse_frontmatter(text))
    try:
        blob = verify.file_bytes_at_revision(
            state.code_root,
            redrive_base_ref(state, isolated_redrive=isolated_redrive),
            raw.as_posix(),
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
    with state_lock(run_dir):
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


@dataclass(frozen=True)
class RearmNotice:
    """One operator-facing notice produced by a successful re-arm."""

    severity: Literal["note", "warning"]
    message: str
    next_step: str


@dataclass(frozen=True)
class RearmOutcome:
    """Authoritative result of a successfully persisted escalation re-arm."""

    story_key: str
    notices: tuple[RearmNotice, ...]
    hold_resume: bool


class _RearmJournal(Journal):
    """Journal writer that captures successful re-arm notices at append time."""

    def __init__(self, run_dir: Path):
        super().__init__(run_dir)
        self.notices: list[RearmNotice] = []
        self.hold_resume = False

    def append(self, kind: str, **fields: Any) -> None:
        # Capture only after the durable append succeeds. The synthetic entry contains
        # every producer-supplied field the shared classifiers consume; Journal's
        # self-minted timestamp/log fields are not part of either contract.
        super().append(kind, **fields)
        entry = {"kind": kind, **fields}
        self.hold_resume = rearm_holds_the_resume(entry) or self.hold_resume
        rendered = rearm_event_notice(entry)
        if rendered is not None:
            severity, message, next_step = rendered
            self.notices.append(RearmNotice(severity, message, next_step))


def rearm_escalation(
    run_dir: Path,
    story_key: str | None = None,
    *,
    restore_patch: str | None = None,
    isolated_redrive: bool,
    resolution_recorded: bool,
    project_root: Path | None = None,
) -> RearmOutcome:
    """Run the complete spec/git/state re-arm transaction under the run lock."""
    with state_lock(run_dir):
        return _rearm_escalation_locked(
            run_dir,
            story_key,
            restore_patch=restore_patch,
            isolated_redrive=isolated_redrive,
            resolution_recorded=resolution_recorded,
            project_root=project_root,
        )


def _rearm_escalation_locked(
    run_dir: Path,
    story_key: str | None = None,
    *,
    restore_patch: str | None = None,
    isolated_redrive: bool,
    resolution_recorded: bool,
    project_root: Path | None = None,
) -> RearmOutcome:
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
      the id is what has to change. That preserved trail is also what
      `resolution_recorded` watermarks: keeping it whole is what lets a later
      cycle tell the answered prefix from the unanswered tail, instead of
      choosing between re-presenting everything and losing the audit (DW-11).
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

    `isolated_redrive` is the LIVE policy's isolation mode (`scm.isolation ==
    "worktree"`), which run state cannot carry: the mode is re-read at every resume and
    a mid-run change is journalled, never refused, so the recorded `task.worktree_path`
    says how the escalated attempt RAN and only policy says how the re-drive WILL run.
    Keyword-only and required, because every consumer of it here is an answer a human
    acts on — which ref to commit the corrected spec on, whether the working-tree flip
    reaches the re-drive at all, whether a restore latch can be honored — and a
    defaulted mode would answer all three for the wrong tree in silence, which is the
    defect this parameter exists to close. Both callers (`cli.cmd_resolve`,
    `tui.TuiApp._do_rearm`) hold a loaded policy already.

    `resolution_recorded` says whether THIS gesture accepted a resolution, and it
    alone gates the `escalations_resolved_upto` watermark (DW-11): the next resolve
    cycle hides every escalation recorded below it, so advancing it over entries no
    human answered would bury them forever and report them as already answered — the
    inverse of the defect the watermark exists to fix. Keyword-only and REQUIRED for
    the same reason as `isolated_redrive`: a default would be wrong in silence on
    exactly the path that matters. It is a PARAMETER rather than a disk read because
    the fact is not on disk. `resolution.json` is unlinked at one site in `src/`
    (`resolve.run_session`, before it launches), which only `cli.cmd_resolve`'s
    interactive arm reaches, and nothing deletes the marker at or after a re-arm — so
    the marker survives the re-arm that consumed it, and `resolve --no-interactive` or
    the TUI's Re-arm button would read the PREVIOUS cycle's marker as its own. The
    caller already holds the answer: `cmd_resolve` binds it from `resolve.run_session`,
    and both non-interactive callers know by construction that no session ran. Do not
    unlink the marker here either — the TUI's Re-arm button is gated on its presence.

    `project_root` is the LIVE CLI project, and it exists so this function writes the
    file `resolve.build_context` told the agent to edit. `state.project` is the
    LAUNCH-TIME project and nothing re-stamps it (unlike `state.code_root`, which
    `restamp_code_root` re-points just before both callers reach here), so after a
    project move `task_spec_path` resolves under a directory that no longer exists:
    the status flip and the baseline re-stamp both silently no-op — every writer
    answers an absent path with `False` rather than an exception — while the agent's
    correction sits in the live tree the re-drive actually reads. The re-drive then
    wedges on the escalated attempt's status and the escalation is spent.
    `build_context` already rebases the `spec_file` it publishes, through the very
    helper used here, so the two sides now name one file.

    `None` means "the project this run recorded", which is byte-for-byte today's
    behavior and the correct answer for every run whose project has not moved — the
    default is safe in a way `isolated_redrive`'s would not be, because it does not
    stand in for a fact only the caller holds; it names the same tree the caller
    would pass. It is optional for that reason and to match `build_context`'s own
    signature, which takes the live roots the same way.

    Scope is the WRITE TARGET and its confinement root, not the reachability verdicts
    beside them. Those read `task.spec_file`, which is relative for every run that
    records one under the project, and answer without consulting `state.project` at
    all; the two that can consult it degrade toward WARNING on an unresolvable path,
    which is the safe direction and already their documented contract.

    The generation bump stays UNCONDITIONAL beside the gated stamp: it answers session-id
    reuse (#705), which an abandoned attempt needs exactly as much as a resolved one.

    Returns the authoritative re-arm outcome: the story key, the ordered notices
    whose journal appends succeeded during this call, and whether one of the appended
    records holds the combined re-arm/resume gesture. Raises RearmError when the run is not paused at
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
        err = validate_restore_latch(state, task, key, worktree_isolation=isolated_redrive)
        if err is not None:
            raise RearmError(err)

    # The tree this gesture is ACTING IN, for the paths below that WRITE. `state.project`
    # is where the run was launched and nothing re-stamps it, so after a project move it
    # names a directory that is no longer there. See the `project_root` note above.
    live_project = project_root if project_root is not None else Path(state.project)

    journal = _RearmJournal(run_dir)
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
    # DW-11. How much of the preserved audit trail this resolution covered, so the next
    # `resolve` shows the human only what they have not already answered. Gated on the
    # CALLER's answer, never on `resolution.json`: the marker survives the re-arm that
    # consumed it (only `resolve.run_session` unlinks it, and two of the three callers
    # never run one), so reading it here would let a later marker-less gesture stamp
    # over escalations nobody saw. A length, taken BEFORE the re-drive appends anything
    # — `record_session` is the sole mutation of this list — and left where it stands
    # when nothing was accepted, which reproduces the pre-DW-11 behavior for that
    # gesture: everything shown, nothing reported withheld.
    if resolution_recorded:
        task.escalations_resolved_upto = len(task.sessions)
    task.review_cycle = 0
    task.followup_reviews_spent = 0  # human-resolved re-drive gets a fresh damping budget
    task.defer_reason = None
    task.rearmed = True  # resume-time recovery notice describes a clean rebuild,
    # not a failed attempt (engine._finish_inflight clears it once the rebuild runs)
    # Always (re)assign the latch: a None restore_patch clears a stale one left by
    # a prior restore attempt the human then chose to redo from scratch.
    task.restore_patch = restore_patch

    # The spec this re-arm writes to and the bytes it FOUND there — the two inputs the
    # rollback below needs. Declared out here because their consumers sit past every
    # block that sets them: the baseline re-stamp's SECOND `if task.spec_file:` block,
    # and the transaction guard's `except` arm, which has to name them from outside all
    # of them.
    spec_before: bytes | None = None
    spec_path: Path | None = None

    # ONE transaction, from the first spec write to the commit point. `save_state` IS
    # that commit point: until it returns the run still calls this story ESCALATED, so
    # any fault escaping this window left a spec re-armed on disk against a task that is
    # not — the "one edit nothing else records" each sequenced refusal was written to
    # avoid, reached instead by a `journal.append` OSError from the residue pass, a
    # non-Git fault from the commits probe, or `save_state` itself failing. Only two of
    # the aborts in here ever undid their own writes; guarding the window replaces both
    # of those per-arm undos with one rule that covers every fault source in it.
    #
    # `except BaseException: ...; raise` rather than a `finally` with a flag, because
    # the rollback must run on the ERROR path only and a bare `raise` re-raises the
    # ORIGINAL fault untouched — the narrowed `verify.GitError` taxonomies inside stay
    # narrowed, and a non-git fault from either probe still escapes as itself.
    #
    # The guard opens one line above the FIRST write rather than at the `spec_before`
    # capture, because the residue pass, the advance and `save_state` all have to be
    # covered too and they live outside that block.
    #
    # STATE THE SCOPE PRECISELY, because one branch in here is the counterexample to the
    # loose reading: what this transaction restores is the SPEC's BYTES, from the first
    # spec write onward. It is not "the tree as the re-arm found it". The sentinel-clear
    # branch sits INSIDE the guard and UNLINKS a file, and that deletion is deliberately
    # NOT undone — `_clear_sentinel` preserves a copy under `{run_dir}/sentinels/` and a
    # retried resolve re-clears it idempotently, so re-creating it here would fight a
    # gesture that is already safe to repeat. `spec_before` is `None` on that leg, so
    # `_rollback_rearm` records `unknown` rather than `unchanged` and the operator
    # surfaces make no claim about the file. Recording it as `unchanged` is precisely
    # the bug that reading would produce: a notice naming a file this re-arm DELETED as
    # proof the tree is untouched.
    try:
        if task.spec_file:
            spec_path = live_spec_path(task, state, live_project)
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
                # Deleting the sentinel does not make the re-plan produce a different one:
                # the correction that does lives UPSTREAM, in the `SPEC.md` / `stories.yaml`
                # the resolve skill sends the agent to instead of this file. That correction
                # faces the same reachability gap the spec arm below measures, and faced NO
                # gate at all — this arm cleared `spec_file` and fell through, so
                # `write_reaches_the_redrive` was never computed and the resume was never
                # held for a sentinel. An isolated re-drive then mounts fresh from
                # `redrive_base_ref`, re-plans from a committed tree that never saw the
                # edit, mints the same sentinel again, and the escalation is spent.
                #
                # Narrowed by PROOF for the reason the spec record below is, and the need is
                # sharper here: `stories_reach_the_redrive` answers "unreachable" for EVERY
                # isolated stories run whose spec folder sits inside the project, which is
                # every one we author. Gating on it alone would fire — and hold the resume —
                # on 100% of isolated sentinel re-arms, a per-configuration constant rather
                # than an event. `_redrive_reads_the_upstream_artifacts` is what makes it an
                # event: it fires only while this checkout still holds upstream bytes the
                # ref the re-drive mounts from does not.
                #
                # No `redrive` discriminator, unlike the spec record: this one has a single
                # remedy because it has a single reachable shape. An in-place re-drive reads
                # the main checkout's working tree, which is exactly where `cwd=project` put
                # the correction, so `stories_reach_the_redrive` short-circuits that leg to
                # reachable and no record is written for it at all.
                if not stories_reach_the_redrive(
                    task, state, isolated_redrive=isolated_redrive
                ) and not _redrive_reads_the_upstream_artifacts(state):
                    journal.append(
                        "rearm-upstream-write-unreachable",
                        story_key=key,
                        # `task_stories_root` names the tree the RUN owns; the correction
                        # lands in the checkout the resolve session ran in. Both are the
                        # project on this leg unless a mount is recorded, and the operator
                        # needs the folder to act, so the record carries the folder the
                        # remedy is about rather than the run's read locator.
                        stories_root=str(_upstream_artifacts_folder(state)),
                        target_branch=state.target_branch,
                    )
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
                # reading anything — and demanded it even when `_redrive_spec_status` had
                # already proven the committed spec carries the status the re-drive routes
                # on. See `_spec_is_shared_with_the_redrive` for why an isolated unit's spec
                # is nevertheless reachable when it sits in an artifact dir configured
                # outside the project tree.
                write_reaches_the_redrive = spec_reaches_the_redrive(
                    task, state, isolated_redrive=isolated_redrive
                )
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
                # `redrive_base_ref`, NOT at the code root's current `HEAD` — the two part
                # company as soon as the operator checks out another branch while the
                # escalation is paused, and this record now holds the resume.
                #
                # The branch rides along because the remedy needs it: on exactly the shape
                # the ref fix rescues, "commit the corrected spec" without a branch sends
                # the operator to commit again on the branch the re-drive does not read, and
                # the next re-arm prints the same sentence. Empty for the migrated shape
                # `redrive_base_ref` degrades to `HEAD` for, and the notice drops the
                # clause rather than naming a ref it cannot source — and empty for an
                # IN-PLACE re-drive, which has no branch to name at all.
                #
                # `redrive` is that second shape's discriminator, and it goes ON the record
                # because the reader is out of process: `rearm_event_notice` renders from a
                # journal line alone and cannot re-read the policy that produced it. One
                # kind, two remedies. Isolated: the writes landed in a mount the re-drive
                # discards, so the correction must be COMMITTED on the named branch. In
                # place: the writes landed in the mount the escalated attempt recorded while
                # the re-drive now reads the main checkout, so the correction must be made
                # THERE — a commit is neither required nor sufficient. Telling the second
                # operator to commit sends them to the wrong tree, which is the same class
                # of silent loss this whole record exists to end.
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
                    and _redrive_spec_status(state, task, isolated_redrive=isolated_redrive)
                    != target_status
                ):
                    journal.append(
                        "rearm-spec-write-unreachable",
                        story_key=key,
                        spec_file=str(spec_path),
                        status=target_status,
                        target_branch=state.target_branch if isolated_redrive else "",
                        redrive="isolated" if isolated_redrive else "in-place",
                    )
                # Captured immediately before the FIRST write, so an abort further down can
                # put the spec back exactly as found.
                #
                # A path that is NOT a file here degrades to `None`, and that degrade is
                # sound for the reason it always was: every writer below answers such a
                # path with `False` rather than an exception, so there is nothing to undo.
                # A missing spec, a dangling link and a directory all land there.
                #
                # A path that IS a file whose bytes could not be read is the opposite
                # case, and it must FAIL BEFORE WRITING. The read below is one syscall
                # among many against a file three later writers open independently, so a
                # transient fault (EIO on a network mount, a momentary EACCES, ENFILE
                # under load) can be followed by writes that all succeed — and the abort
                # that follows would then find `spec_before is None`, record `unknown`,
                # and re-raise with the flip PUBLISHED and nothing put back. That is
                # DW-79/DW-83 reached through the transaction's own preimage. Refusing
                # keeps the escalation armed for a retry.
                #
                # Gated on the SAME PAIR as the flip's refusal one screen below
                # (`spec_path.is_file() and write_reaches_the_redrive`), because it is the
                # same abort-vs-warn decision about the same file and the two must not
                # disagree. `is_file` alone is not enough: under isolation the readable
                # file is the worktree copy the re-drive DESTROYS before reading anything,
                # so an abort there demands a repair to a file nothing opens — its remedy
                # cannot change what the re-drive reads, and it costs the operator the
                # interactive resolve session over a spec whose real reachability record
                # (`rearm-spec-write-unreachable`) has already been written above. On that
                # shape the unreadable preimage is an OBSERVATION, and observations
                # degrade: `spec_before` stays `None`, the writes below no-op or land on a
                # doomed copy, and any later abort records `unknown` rather than claiming
                # a file it never captured.
                try:
                    spec_before = spec_path.read_bytes()
                except OSError as e:
                    if spec_path.is_file() and write_reaches_the_redrive:
                        raise RearmError(
                            f"cannot read story spec {spec_path} before re-opening it for "
                            f"the re-drive ({e.__class__.__name__}: {e}) — the re-arm "
                            "refuses to write a spec it could not capture first, since a "
                            "later abort would have nothing to put back; fix or replace "
                            "the file, then re-run resolve"
                        ) from e
                    spec_before = None
                try:
                    flipped = verify.set_frontmatter_status(
                        spec_path,
                        target_status,
                        confine_root=live_spec_root(task, state, live_project),
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
                        # already committed: `_redrive_spec_status` had just PROVEN the
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
                        spec_path, confine_root=live_spec_root(task, state, live_project)
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
                    # Two shapes reach this arm and the transaction guard below covers both.
                    # On the flip's own read/decode fault nothing was written, so the rollback
                    # proves that and leaves the file and its mtime alone. A fault raised
                    # inside `strip_auto_run_result` is raised with the flip already PUBLISHED
                    # — an `atomic_write_bytes_confined` that cannot land (ENOSPC, EIO, a
                    # component swapped for a link under the `O_NOFOLLOW` walk), or a spec
                    # replaced under us between the two writes. Ordering the strip after the
                    # read-back check bought that check its byte-identical abort; it buys this
                    # one nothing, because the fault is IN the strip.
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
            spec_path = live_spec_path(task, state, live_project)
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
                        confine_root=live_spec_root(task, state, live_project),
                    )
                except (OSError, UnicodeDecodeError, verify.FrontmatterWriteError) as e:
                    # FrontmatterWriteError joins the tuple rather than getting its own
                    # arm: the remedy is the same sentence ("fix the file"), and the
                    # exception already says which shape it could not move. What matters
                    # is that it aborts here — the stale-baseline hazard this block exists
                    # to close is exactly what a swallowed write would leave behind.
                    #
                    # The abort still leaves the spec as this re-arm FOUND it, but no longer
                    # by an undo written here: the transaction guard around this whole window
                    # rolls the spec back on every fault that escapes it, so this arm only has
                    # to raise. Without that rollback a spec with a movable `status:` beside an
                    # unmovable `baseline_revision:` came back flipped to the re-drive's status
                    # and stripped of the terminal result, while the run still called the story
                    # escalated.
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
    except BaseException as e:
        # Roll the spec back to the bytes this re-arm found, record that it aborted, and
        # re-raise the ORIGINAL fault. A failed rollback raises out of here instead —
        # a part-written spec is the loudest thing this can be, and the original fault
        # rides along in that `RearmError`'s `__context__` because it is still being
        # handled at the moment the restore raises.
        #
        # `BaseException` and not `Exception`, and the breadth is load-bearing rather
        # than defensive: `KeyboardInterrupt` and `SystemExit` derive from `BaseException`
        # alone, and this window spends most of its time in blocking I/O an operator can
        # interrupt — three git subprocesses (`rev_parse_head`, `untracked_files`,
        # `commits_above`) plus `save_state`, all AFTER the status flip has published and
        # BEFORE anything persists it. A Ctrl-C there under `except Exception` would exit
        # by the one path that reproduces exactly the DW-79/DW-83 state this guard exists
        # to end: a spec re-armed on disk against a task still recorded as ESCALATED.
        # Narrowing this arm is a silent regression, so a test raises `KeyboardInterrupt`
        # through the window on purpose.
        #
        # That breadth is also what makes the commit point AMBIGUOUS on exactly one path,
        # and the check below is the price of it: `save_state` commits by `atomic_replace`
        # and can still be interrupted between that rename and its return, so a fault
        # arriving here does NOT prove the transaction failed. `_rearm_commit_landed` asks
        # the disk — the only witness of a rename — and a committed re-arm is left alone:
        # undoing the spec then would build the mirror image of the defect this guard
        # closes, persisted state re-armed against a spec that is not.
        if not _rearm_commit_landed(run_dir, key, task):
            _rollback_rearm(journal, key, spec_path, spec_before, task, state, e)
        raise
    journal.append(
        "story-escalation-resolved",
        story_key=key,
        baseline=task.baseline_commit or "",
        restore=bool(restore_patch),
    )
    return RearmOutcome(key, tuple(journal.notices), journal.hold_resume)


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
            f"excluded the abandoned restore's new files from the re-drive baseline: {files}",
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
    if kind == "rearm-commits-probe-failed":
        # The sibling row above is written only when the probe ANSWERED, so its
        # absence used to mean either "no commits from the abandoned attempt" or
        # "the probe could not tell" — and nothing downstream could separate them.
        # The range is named in BOTH halves on purpose: `resolve` prints the
        # next_step and the TUI drops it, so the message has to stand alone there.
        baseline = str(entry.get("old_baseline", "?"))
        # A first-party value is a full rev-parse hex result. Refuse to turn a
        # malformed persisted value into a copy/paste command, and collapse control
        # characters from git's detail before either operator surface renders it.
        base = baseline[:12] if re.fullmatch(r"[0-9a-fA-F]{12,}", baseline) else "unknown"
        detail = str(entry.get("error", "?"))
        if baseline:
            # `commits_above` repeats the complete baseline in its GitError. The
            # display label and command deliberately use the short form, so leaving
            # the full value in the detail defeated that truncation on both surfaces.
            detail = detail.replace(baseline, base)
        detail = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff]+", " ", detail)
        if len(detail) > 4096:
            detail = detail[:4093] + "..."
        return (
            "warning",
            f"could not list the commits above the abandoned attempt's baseline "
            f"({base}..) — {detail}; this silence proves nothing, so fix the Git "
            f"failure, then run `git log {base}..HEAD` yourself and revert anything "
            "that did not come from your resolve",
            f"Fix the Git failure, then check `git log {base}..HEAD` before resuming",
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
        # ONE kind, TWO remedies, told apart by the `redrive` field its producer writes
        # — the live isolation mode of the re-drive, which this reader runs too late and
        # in the wrong process to determine for itself. A record predating the field is
        # an ISOLATED one: that was the only shape the producer could journal before the
        # in-place arm existed, so the absent field is a known value, not an unknown.
        spec = entry.get("spec_file", "?")
        if str(entry.get("redrive", "isolated") or "isolated") == "in-place":
            # The mirror shape: `isolation` was edited to `"none"` while the escalation
            # was paused, so the writes went into the mount the escalated attempt
            # recorded and the re-drive reads the main checkout instead. Committing is
            # not the remedy here and naming a branch would be actively wrong — the
            # in-place re-drive reads a WORKING TREE, so the edit simply has to be made
            # in the checkout the run resumes into.
            return (
                "warning",
                f"this run's isolation policy changed to `none` while the story was "
                f"escalated, so the re-arm's spec writes ({spec}) landed in the "
                "escalated attempt's worktree while the re-drive now runs in the main "
                "checkout — re-apply the correction to the main checkout's copy of the "
                "spec or the story re-wedges on the escalated attempt's status",
                "Correct the spec in the main checkout before resuming",
            )
        # The branch is the half an operator cannot infer: the re-drive cuts its fresh
        # worktree from the run's PINNED target branch, so a correction committed on
        # whatever the main checkout happens to have checked out is not the one it
        # reads. Named only when the record carries it — a run predating the field
        # leaves it empty, and a remedy that names no ref beats one that names a guess.
        base = str(entry.get("target_branch", "") or "")
        where = f" on `{base}`" if base else ""
        return (
            "warning",
            f"the re-drive of this story will mount a fresh worktree, so the re-arm's "
            f"spec writes ({spec}) land in a tree it discards — the re-driven session "
            "reads the COMMITTED spec, so commit the corrected "
            f"spec{where} or the story re-wedges on the escalated attempt's status",
            f"Commit the corrected spec{where} before resuming",
        )
    if kind == "rearm-upstream-write-unreachable":
        # The sentinel counterpart, and ONE remedy rather than the two above: the
        # producer only reaches this record on the mounting leg, because an in-place
        # re-drive reads the very checkout `resolve.run_session` ran the agent in. So
        # there is no `redrive` discriminator to read and no in-place arm to get wrong.
        #
        # It names the FOLDER, not a file, because the correction is not one file: the
        # skill sends the agent to `SPEC.md` or to this story's entry in `stories.yaml`,
        # and which of the two moved is the agent's choice, not something a journal
        # reader can recover. Naming both and the folder they sit in is what makes the
        # remedy actionable without claiming more than the record proves.
        root = str(entry.get("stories_root", "?"))
        base = str(entry.get("target_branch", "") or "")
        where = f" on `{base}`" if base else ""
        return (
            "warning",
            f"the sentinel was cleared, but the re-drive of this story will mount a "
            f"fresh worktree and re-plan from the COMMITTED tree — the upstream "
            f"correction in {root} (`SPEC.md` / `stories.yaml`) is uncommitted there, "
            f"so the re-plan reads the same intent that wedged and mints the sentinel "
            "again",
            f"Commit the corrected SPEC.md / stories.yaml{where} before resuming",
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
    if kind == "rearm-aborted":
        # ONE kind, THREE renderings, told apart by `rollback` — a field the producer
        # writes because this reader runs out of process and cannot look at the spec to
        # see what is on disk. The split is by what the surface may CLAIM about the file,
        # not by how the re-arm failed:
        #
        # * `restored` / `unchanged` — `_restore_rearmed_spec` either put a landed write
        #   back or READ the file and proved it byte-equal. Both license "left exactly as
        #   the re-arm found it", so they share a message.
        # * `failed` — the restore could not write, so the spec may be half-written and
        #   no re-run of resolve can settle it.
        # * anything else — `unknown`, an absent field, or a value this table does not
        #   recognize. Says what is TRUE regardless (nothing persisted, still escalated)
        #   and claims nothing about the file. The default is deliberately the
        #   non-reassuring branch: an unknown outcome rendered as the benign one is how a
        #   sentinel-clear abort came to describe a DELETED file as untouched, and a
        #   record written by a future producer must not inherit a reassurance by
        #   accident.
        #
        # Position-independent wording, because the same string renders as a `resolve`
        # stderr line and as a TUI toast, and the TUI drops the `next_step`: the message
        # alone has to carry everything an operator must act on. That is why `failed`
        # names the restore-from-git remedy in the MESSAGE and keeps it in `next_step`
        # too — this is the one re-arm kind whose imperative is not moot on the TUI,
        # since an abort raises and that surface does not go on to resume.
        #
        # Neither surface may read this as a re-arm that half-succeeded — an abort raises,
        # so `rearm_holds_the_resume` is deliberately NOT extended to this kind. There is
        # no gesture left to hold.
        spec = entry.get("spec_file", "") or "(none)"
        error = entry.get("error", "?")
        rollback = str(entry.get("rollback", ""))
        if rollback == "failed":
            # No enumeration of WHICH writes landed. A fault raised inside
            # `strip_auto_run_result` reaches the guard with the flip published and the
            # `## Auto Run Result` section still present, so the old sentence ("carrying
            # this re-arm's status flip and missing its `## Auto Run Result` section")
            # described a state this record cannot know it is in.
            #
            # Nor does it promise GIT alone. The bytes the undo failed to write lived only
            # in this process and are gone with it, and a spec is not necessarily tracked:
            # an untracked artifact, or one in an artifacts folder configured outside the
            # checkout entirely (supported configuration — `bmadconfig` resolves one), has
            # no committed copy to check out. Naming git as THE remedy sent that operator
            # to a command with nothing to give them; naming it as ONE of two keeps the
            # common case one word away without asserting a recovery that may not exist.
            return (
                "warning",
                f"the re-arm ABORTED ({error}) and putting the spec back FAILED — {spec} "
                "may be left part-written, so restore it from git or from your own copy "
                "before re-running resolve; nothing was persisted and the story is still "
                "escalated",
                "Restore the spec from git or your own copy, then re-run resolve",
            )
        if rollback in ("restored", "unchanged"):
            return (
                "warning",
                f"the re-arm ABORTED ({error}) — nothing was persisted, the spec ({spec}) "
                "was left exactly as the re-arm found it, and the story is still escalated",
                "Fix the cause above, then re-run resolve",
            )
        return (
            "warning",
            f"the re-arm ABORTED ({error}) — nothing was persisted and the story is still "
            f"escalated, but the re-arm could not confirm what it left on disk ({spec})",
            "Check the recorded spec, then re-run resolve",
        )
    return None


def rearm_holds_the_resume(entry: dict[str, Any]) -> bool:
    """True for a re-arm record whose remedy has to land BEFORE the re-drive reads the
    tree — so a surface that re-arms and resumes in ONE gesture must stop after the
    re-arm and leave `bmad-loop resume` to the operator.

    TWO kinds qualify, and the discriminator is PROOF, not urgency.
    `rearm-spec-write-unreachable` is written only once `_redrive_spec_status` has
    established that the committed spec does NOT carry the status the re-drive routes
    on, and only for a spec the working-tree flip cannot reach. Resuming on it is not
    risky, it is futile: the re-drive discards the worktree, mounts a fresh one from
    git, and step-01 reads a status it cannot route — `unrecognized status in existing
    story file` halts it blocked, and the escalation is spent. The record's own
    next_step already said "commit the corrected spec before resuming"; both default
    surfaces then resumed in the same breath, which made the imperative unactionable at
    the moment it rendered. The interactive resolve agent cannot close that gap either
    — its skill forbids it from committing.

    `rearm-upstream-write-unreachable` earns it the same way on the sentinel path,
    where there is no spec write to measure at all: the sentinel is cleared by
    deletion, and the correction that stops it recurring sits upstream in `SPEC.md` /
    `stories.yaml`. Its proof is `_redrive_reads_the_upstream_artifacts`, which fires
    the record only while the ref the re-drive mounts from does NOT already hold this
    checkout's copy of those two files — so, exactly as above, resuming is not risky
    but futile: the re-drive re-plans from a tree that never saw the correction and
    mints the same sentinel again.

    The other warnings stay advisory and do NOT hold — `stale-restore-commits`,
    `stale-restore-unparseable`, `rearm-commits-probe-failed` and
    `rearm-baseline-advance-failed` among them: each reports something an operator may
    need to act on, but none of them PROVES the re-drive cannot route, and holding on a
    maybe would turn the ordinary degrade path into a two-command gesture for an
    outcome nothing decided. `rearm-commits-probe-failed` is the newest and the least
    tempting to promote: it says the commits probe could not answer, which is strictly
    LESS than the answer — it proves nothing about whether the re-drive can route, only
    that one advisory could not be computed.

    Not folded into `rearm_event_notice`'s tuple, because they are different questions
    asked of the same entry: that table answers "what do I tell the operator", this
    answers "may this gesture still resume". Both surfaces ask both, in one walk.
    """
    return isinstance(entry, dict) and entry.get("kind") in (
        "rearm-spec-write-unreachable",
        "rearm-upstream-write-unreachable",
    )


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
    bad old baseline — none may wedge a resolve. Both degrades journal a record of
    their own: a patch parse failure writes `stale-restore-unparseable`, and a
    commits-probe Git failure writes `rearm-commits-probe-failed` (DW-81). Neither
    is silent, because the sibling record's ABSENCE is what an operator reads as
    "clean" — and a probe that could not answer is not the same claim.
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
        except verify.GitError as e:
            # Follows rearm_escalation's baseline-advance taxonomy boundary, and the
            # catch stays EXACTLY `verify.GitError`: every fault `_run_git` can
            # translate (spawn, timeout, decode, non-zero rc) lands here, and a
            # non-git fault still escapes to the re-arm's transaction guard.
            #
            # Warn-only, but no longer silent. This arm used to set `shas = []` and
            # fall through to the `if shas:` gate below, which made a probe that
            # FAILED byte-identical — on every downstream surface — to a probe that
            # found no commits above the old baseline. The operator was told nothing
            # either way, and the absent record is the only warning they get that the
            # abandoned attempt's commits may still be sitting under the re-drive's
            # new baseline. The `else:` is what retires the sentinel rather than
            # leaving it inert: no `shas` exists on this leg to gate on.
            journal.append(
                "rearm-commits-probe-failed",
                story_key=story_key,
                old_baseline=old_baseline,
                error=f"{e.__class__.__name__}: {e}",
            )
        else:
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
