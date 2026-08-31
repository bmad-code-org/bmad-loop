"""Native-Windows psmux backend for the terminal-multiplexer seam.

psmux (a Rust/ConPTY tmux re-implementation) speaks the tmux CLI through its
own distinctly-named ``psmux`` binary, so this leaf points the base's spawn
seam at that name, keeps every argv construction in :mod:`.tmux_base`, and
swaps only the shell dialect (PowerShell instead of POSIX sh) via the base's
hooks, plus the handful of behaviors where psmux diverges from tmux: an
attaching ``new-session`` is refused by a nesting guard when run from
inside a psmux pane, and a quoted command string does not survive psmux's
outer re-parse (so shell source travels as ``pwsh -EncodedCommand`` — the
log sink included, since the base's ``cat >>`` assumes a POSIX host
shell). Window ids are minted per server (one
server per session), so every id this backend hands back —
``new_window``, ``list_window_ids``, ``new_parked_window``, the
``window_id`` columns of ``list_windows``, and ``current_window_id`` — is
session-qualified to ``session:@N`` (degrading to the bare id only where
the grammar cannot carry the session name — see ``_qualified_window_id``)
and routes to the owning server from any caller (psmux/psmux#483). Every
seam verb takes that form, ``select-window`` included (psmux/psmux#497).
Per-window user options do not exist at all, so
the window-option verbs, the ``@``-prefixed columns of ``list_windows``
and the parked trailer route through a substitute channel — see the
``per-window option channel (#310)`` block below for the model and its
rules. Session-scoped options need no such substitute — one server per
session means that server's single map is the session's — but a value
still has to read back verbatim through the same listing parse, so
``set_session_option`` gates it on the same transportability rule (#320).
``detach-client`` and ``switch-client`` report dispatch, not effect: a
nonzero exit is a real failure, but a zero one says only that the verb was
sent, so neither seam boolean can be read off the exit code alone. The
session's attached-client count supplies what the rc cannot — as a drop
across ``detach-client`` and ``switch-client -l``, and as a gate on the
targeted ``switch-client -t``, whose same-session move no drop can see
(#659); see the ``client verbs: observed effect (#317)`` block. The seam's
``False`` is the joint claim that no switch happened *and* a client is still
here, so ``switch_client`` answers ``None`` wherever it cannot carry the
second half — for two different reasons. A timed-out verb and an rc-0 whose
gate count is unreadable are moves the server may have completed; a session
with nothing attached is a move it cannot have made, but there is nobody here
to keep prompting either. ``False`` is left to the exits that do carry the
whole claim, both of which dispatch nothing: no session to measure, and a
spawn fault with no fallback. psmux resolves every target
through a *registry* — the ``PSMUX_DATA_DIR`` directory of per-session
``.port``/``.key`` files — and bmad-loop points it at a per-project root
(``runs.mux_registry_root``), so this backend owns the two rules that follow
from that: it refuses to spawn under a root psmux would panic on, and it can
mint an instance bound to psmux's own default registry so cleanup can still
address sessions created before the move (``legacy_registries``). Both live in
the ``_run`` override, the one place psmux is spawned.
``available()`` additionally gates on the
reported version —
see ``_LAST_UNSUPPORTED`` for what the floor buys and why it moves.
``has_session``
is inherited unchanged, but one server per session gives it a residual the
tmux path does not have: a ``-t`` read resolves through the named port and key
files, so a wrong ``True`` is reachable whenever that PAIR addresses some OTHER
live server. Both halves are needed — the server rejects a key mismatch before
running anything — so a merely recycled port does not reach it; a duplicated
registry entry or a same-named session on a foreign server does. A server that
is simply *gone* fails closed: measured on 3.3.8, both a removed port file and
a stale one whose process was killed answer rc 1, ``no server running on
session``. For the lost-session probe
(#489) that is the safe direction — a wrong ``True`` drops the diagnosis
rather than inventing one — and the collision it needs is an independently
created session sharing a ``bmad-loop-<run-id>`` name: an operator's, or
another run's on a colliding id (see #531). The psmux
behaviors cited in this module were read from the psmux source at tag
``v3.3.8`` — the oldest build ``available()`` admits — and the safe
observable subset is probed in ``tests/test_psmux_live.py``.
See :mod:`.multiplexer` for the contract.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .tmux_base import PARKED_RETURN_DETACH, BaseTmuxBackend, TmuxError

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# psmux's registry-root variable, as psmux spells it. `runs.PSMUX_DATA_DIR` holds
# the same name for the export side; the two are deliberately separate constants
# rather than one import, because the dependency only runs one way — `runs`
# imports the adapter seam, never the reverse — and a transport name belongs in
# the transport leaf.
_DATA_DIR = "PSMUX_DATA_DIR"


def _pwsh_quote(value: str) -> str:
    # A single-quoted PowerShell literal: no interpolation, and the only escape
    # is doubling the quote itself.
    return "'" + value.replace("'", "''") + "'"


# One warning per process about `PSMUX_BARE_ENV`, not per verb — see
# `PsmuxMultiplexer._warn_if_bare_env`.
_BARE_ENV_WARNED = False


def _bare_env_on(value: str | None) -> bool:
    """psmux's own predicate for `PSMUX_BARE_ENV` (`src/pane.rs:890-892`,
    source-read at v3.3.8): on iff the value is "1" or case-insensitively
    "true"."""
    return value is not None and (value == "1" or value.lower() == "true")


# The `PSMUX_DATA_DIR` that was in force before this process derived its own and
# overwrote it (`runs.export_psmux_registry_root`), or None when nothing was
# displaced. Process-local, and the *only* record of it: the variable itself is
# gone by the time anything asks. See `note_displaced_registry`.
_DISPLACED_ROOT: str | None = None


def note_displaced_registry(value: str | None) -> None:
    """Record the registry root this process took over from, so the migration
    sweep can still reach the sessions living in it.

    Before #537 the backend simply inherited whatever ``PSMUX_DATA_DIR`` the
    environment carried, so an operator who exported an absolute root of their
    own has *their* pre-upgrade bmad-loop sessions in THAT registry — not in
    psmux's default. This process now overrides the variable, which makes those
    sessions unaddressable by every later verb: `stop`, `attach` and `cleanup`
    would each report nothing while the coding processes ran on. The default
    registry cannot stand in for it; the two are different directories, and
    which one a machine used is a fact only the displaced value carries.

    First non-empty value wins, and a later call is ignored. The caller runs
    once per process, ahead of dispatch (``cli._configure_mux``), so the first
    value is the one the operator's environment actually had; a second call
    would be handing back a root *this* process exported. Ceiling, named: a
    single process that configured two different projects in turn would record
    the first project's root as the second's displaced one. ``main()`` does not
    do that, and the consequence if something ever did is a no-op sweep rather
    than a hazard — the legacy pass demands this project's tag, which the other
    project's sessions do not carry.
    """
    global _DISPLACED_ROOT
    if _DISPLACED_ROOT is None and value:
        _DISPLACED_ROOT = value


class PsmuxMultiplexer(BaseTmuxBackend):
    """psmux backend — tmux-family argv from the base, PowerShell dialect and
    the documented psmux divergences here.

    Registered by :func:`~.multiplexer._load_builtin_backends` for ``win32``,
    mirroring :class:`~.tmux_backend.TmuxMultiplexer`.
    """

    # psmux ships psmux/pmux/tmux binaries built from the same source; spawning
    # the distinct psmux name never collides with another tmux-family install
    # (e.g. a tmux-windows port owning ``tmux`` on the same PATH).
    _BINARY = "psmux"
    # psmux emits UTF-8; decoding with the console codepage (cp1252) garbles
    # format-string output, and a stray byte must degrade visibly, not raise.
    _ENCODING = "utf-8"
    _ERRORS = "backslashreplace"

    def __init__(self, *, default_registry: bool = False, registry_root: str | None = None) -> None:
        """``default_registry=True`` binds this instance to psmux's OWN registry
        root — the one it computes when ``PSMUX_DATA_DIR`` is unset — regardless
        of what this process exports. That is the legacy registry every psmux
        session bmad-loop created before the per-project root existed still lives
        in, and the only reason to build such an instance is to sweep it (see
        :meth:`legacy_registries`).

        ``registry_root`` binds the instance to one NAMED root instead, for the
        other pre-upgrade world: a machine whose operator exported an absolute
        ``PSMUX_DATA_DIR`` before the upgrade kept its bmad-loop sessions there,
        not in psmux's default (see :func:`note_displaced_registry`). Same
        purpose, same single caller, and the same rule below about not touching
        the process environment to do it.

        The two are mutually exclusive and ``default_registry`` wins if both are
        passed — a programming error either way, since a registry is one place.

        Bound per instance rather than swapped into ``os.environ`` around a call:
        the sweep runs on a TUI worker thread beside other threads issuing
        ordinary verbs, and a global swap would aim one of *those* at the wrong
        registry for as long as it was in place.

        Unbinding is done by REMOVING the variable, never by re-deriving psmux's
        default here: that default is ``<home>\\.psmux``, where the home is
        ``USERPROFILE`` when it is set and non-empty, then the profile API
        (``GetUserProfileDirectoryW``), then ``HOMEDRIVE``+``HOMEPATH``, then
        ``HOME`` (``src/paths.rs`` ``home_dir``, source-read at v3.3.8) — and a
        second spelling of that cascade in Python is a second thing to keep in
        sync. A named ``registry_root`` is the opposite case and needs no
        cascade: the value is the root, verbatim as the operator spelled it.
        """
        self._default_registry = default_registry
        self._registry_root = None if default_registry else registry_root

    def _run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The base's one spawn point, plus this backend's registry-root rules.

        Two things happen here and nowhere else, because this is the only place
        psmux is spawned:

        1. **Registry binding.** A ``default_registry`` instance spawns with
           ``PSMUX_DATA_DIR`` removed, so psmux computes its own root. The parent
           env is *copied* first — a Windows child needs ``SystemRoot`` and
           friends — and an explicit per-call ``env`` is honoured as the base, so
           ``new_session``'s scrubbed env keeps its scrubbing.
        2. **The absoluteness gate.** psmux ``assert!``s ``PSMUX_DATA_DIR``
           absolute and non-empty (``src/paths.rs``, source-read at v3.3.8) and
           **panics** otherwise: a Rust panic message on stderr and a nonzero
           exit, which ``has_session`` and friends read as an ordinary "no" —
           a live session reading as gone, for a reason nothing in the output
           names. Refusing here turns that into one bmad-named error naming the
           variable and its value. Checked on the *effective* env, so a per-call
           ``env`` carrying a bad value is caught too, not just the inherited one.

        The unbound, well-formed case adds one dict lookup and no copy.
        """
        effective = env if env is not None else os.environ
        self._warn_if_bare_env(effective)
        if self._default_registry:
            env = {k: v for k, v in effective.items() if k != _DATA_DIR}
        else:
            if self._registry_root is not None:
                # Bound instance: the root is set in the child's env, never in
                # this process's — same threading rule as `default_registry`.
                env = {**effective, _DATA_DIR: self._registry_root}
            value = env.get(_DATA_DIR) if env is not None else effective.get(_DATA_DIR)
            if value is not None and not (value and os.path.isabs(value)):
                raise TmuxError(
                    f"{_DATA_DIR}={value!r} is not an absolute path; psmux panics on a "
                    "relative or empty registry root, so no verb can run under it — "
                    f"unset {_DATA_DIR} to use psmux's default registry, or set it to "
                    "an absolute directory"
                )
        return super()._run(argv, check=check, env=env)

    @staticmethod
    def _warn_if_bare_env(effective: Mapping[str, str]) -> None:
        """Say once per process when ``PSMUX_BARE_ENV`` is on: bmad-loop does
        not support that mode, and what it breaks is quiet.

        Under it psmux ``env_clear``s every pane child and repopulates from a
        14-name allowlist (``src/pane.rs:889-908``, source-read at v3.3.8;
        measured in a real pane) that drops ``BMAD_LOOP_STATE_DIR`` *and* the
        ``LOCALAPPDATA`` its default cascade falls back to. Coding-CLI windows
        still get the state root — their env rides the in-source
        ``-EncodedCommand`` prelude, which runs in the pane after the clear
        (see ``_window_launch``) — but a session's window-0 shell and the TUI's
        parked engine windows rely on inheritance, so a bmad-loop run in one of
        those re-derives the state root from what survived. That diverges, and
        the run then reads this very session as gone, in exactly two cases:
        ``BMAD_LOOP_STATE_DIR`` was in force (the clear drops it, and the
        default cascade answers somewhere else), or ``LOCALAPPDATA`` names
        something other than ``%USERPROFILE%\\AppData\\Local`` (a redirected
        or roaming profile). ``USERPROFILE`` *is* on the allowlist, so on a
        default profile the fallback arm lands on the same root and nothing
        diverges — the mode is unsupported because the failure is silent when
        it does happen, not because it always does. Supporting it means an env
        transport on the session and parked-window verbs, which is its own
        seam change — tracked
        as a follow-up issue, deliberately outside #537.

        Warned, not refused: the variable is psmux's (an operator may run their
        own sessions under it), and most commands never open a window. Ceiling:
        psmux reads the switch in the *server* process at pane spawn; this
        process's effective env is a proxy for it, so a server already running
        with the mode on under a clean client is not detected here.
        """
        global _BARE_ENV_WARNED
        if _BARE_ENV_WARNED or not _bare_env_on(effective.get("PSMUX_BARE_ENV")):
            return
        _BARE_ENV_WARNED = True
        print(
            "warning: PSMUX_BARE_ENV is on, which bmad-loop does not support — "
            "session and parked-window shells lose BMAD_LOOP_STATE_DIR and derive "
            "their own state root and registry, so a run can read as gone; unset "
            "PSMUX_BARE_ENV for bmad-loop's sessions",
            file=sys.stderr,
        )

    def registry_root(self) -> str | None:
        """The registry root a verb from this instance inherits — the process
        environment's, which is what ``_run``'s ``env=None`` default passes on.

        A per-call ``env`` can still name another one; nothing reads this to
        decide where a verb goes, only to disclose where they normally go.

        A ``default_registry`` instance answers ``None`` — it spawns with the
        variable *removed*, and the root psmux then computes for itself is
        deliberately not respelled here (see :meth:`__init__`). ``None`` from
        the ordinary instance likewise means psmux is on its own default
        registry; :meth:`has_registry_namespace` is what separates either from
        tmux's "no namespace exists", and a reader deciding ownership must use
        it — the default registry is shared with every project and with the
        operator, which is precisely what a ``None`` here must not be read as
        disclaiming.

        An instance bound to a named ``registry_root`` answers that root: it is
        where its verbs actually go, and the environment's value is not.
        """
        if self._default_registry:
            return None
        return self._registry_root or os.environ.get(_DATA_DIR)

    def has_registry_namespace(self) -> bool:
        # A property of the transport, not of the instance binding: even a
        # `default_registry` instance addresses A registry — psmux's own.
        return True

    def session_name_key(self, name: str) -> str:
        """psmux resolves a session by opening ``<data dir>\\<name>.port`` by
        name (``src/paths.rs:113``, source-read at v3.3.8), and NTFS opens
        names case-insensitively — measured: with ``bmad-loop-ctl-x`` live,
        target ``bmad-loop-CTL-x`` answers ``has-session``, is refused as a
        duplicate by ``new-session``, and a kill through it takes the
        lowercase session down. So on this transport two names differing only
        by ASCII case denote one session, and comparisons must fold."""
        return name.lower()

    def legacy_registries(self) -> list["PsmuxMultiplexer"]:
        """The registries a pre-upgrade session of ours may still be living in:
        psmux's default, and the root this process displaced.

        Sessions bmad-loop created before the per-project root existed are still
        there, and after the move nothing else can address them: cleanup would
        report a clean sweep while their servers ran on. An extra pass over each
        is the whole migration, and an already-migrated machine just finds
        nothing in either.

        **Two of them, because there were two pre-upgrade worlds.** The old
        backend inherited whatever ``PSMUX_DATA_DIR`` the process carried, so a
        machine that never set it kept its sessions in psmux's *default* root,
        while one whose operator exported an absolute root of their own kept
        them THERE. Returning only the default assumed the first machine and
        left the second's sessions — and their live coding processes —
        unreachable by every verb, with cleanup reporting success. The displaced
        root is remembered by :func:`note_displaced_registry`, because the
        variable no longer holds it.

        Neither pass gets extra reach from being here: ``prune_sessions`` runs
        every legacy registry with ``require_tag=True``, so a session is claimed
        only on its own ownership tag and never on run-directory evidence, which
        proves nothing in a registry shared with other projects and with the
        operator's own psmux sessions. That is what keeps this off a by-name
        kill in somebody else's registry.

        The default-registry pass is skipped when ``PSMUX_DATA_DIR`` is unset
        (this process IS on the default registry — the primary pass already
        covers it) and when it is set to a value psmux would panic on, where the
        primary pass is not running either and a sweep would be the only thing
        that appeared to work. The displaced pass is skipped when nothing was
        displaced, when the displaced value is one psmux would panic on, and
        when it is the root in force (nothing moved). A displaced value that
        happens to spell psmux's own default is swept twice, harmlessly:
        ``prune_sessions`` unions its passes by run id, so one session cannot
        become two kills.
        """
        value = os.environ.get(_DATA_DIR)
        registries: list[PsmuxMultiplexer] = []
        if value and os.path.isabs(value):
            registries.append(type(self)(default_registry=True))
        displaced = _DISPLACED_ROOT
        if displaced and os.path.isabs(displaced) and displaced != value:
            registries.append(type(self)(registry_root=displaced))
        return registries

    # ------------------------------------------- shell dialect (PowerShell)

    # A command pwsh could not even start (not recognized) still runs the rest of
    # the source but leaves $LASTEXITCODE unset — coalesce with a plain `if`
    # (works on any PowerShell version) rather than the PS7-only `??` syntax.
    _EXIT_CAPTURE = "$ec = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }"
    _ECHO = "Write-Host"
    _PARK = "Read-Host"

    def _join_argv(self, argv: list[str]) -> str:
        # The call operator runs a quoted executable with quoted args verbatim.
        # A bare `& ` is a pwsh parse error, so refuse an empty argv here rather
        # than shipping a window that dies on launch.
        if not argv:
            raise TmuxError("empty command")
        return "& " + " ".join(_pwsh_quote(arg) for arg in argv)

    def _source_prefix(self) -> str:
        # psmux windows inherit the claude environment of whichever process
        # cold-started the psmux server (teammate mode, session ids, SSE ports).
        # Clear it so a CLI launched here starts fresh instead of impersonating
        # that session.
        return (
            "Get-ChildItem Env: | Where-Object { $_.Name -like 'CLAUDE_CODE_*' "
            "-or $_.Name -like 'CLAUDECODE*' -or $_.Name -eq 'PSMUX_CLAUDE_TEAMMATE_MODE' } "
            "| ForEach-Object { Remove-Item ('Env:' + $_.Name) }; "
        )

    def _shell_wrap(self, source: str) -> list[str]:
        # psmux joins the trailing argv and re-parses it through an outer shell,
        # which strips embedded quoting; -EncodedCommand (base64 of UTF-16LE) is
        # the lossless transport for arbitrary shell source.
        encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
        return ["pwsh", "-NoProfile", "-EncodedCommand", encoded]

    def _parked_trailer(self, return_opt: str) -> str:
        # The base's trailer re-expressed in pwsh — the tmux verbs are protocol-
        # identical across the family. Errors go to $null: a client or pane that
        # is already gone means the window just parks as-is.
        #
        # The base reads the return target with `show-options -wqv`, which is
        # dead on psmux (#310), so this reads the session-scoped id-keyed option
        # instead. Unlike every other caller of that channel, the trailer cannot
        # be handed its own window id — it is built before the window exists —
        # so it probes for it in-pane. `-t $env:TMUX_PANE` pins the probe to the
        # pane's own window (psmux sets TMUX_PANE in every pane, and a bare `%N`
        # target resolves globally via DisplayMessageById); a target-less probe
        # would resolve the server's *active* window, which is another window's
        # key the moment focus moves after Enter. No `-t <session>`: running
        # inside the pane, $TMUX already routes to this window's own server.
        #
        # Both captures go through "$(...)".Trim(): a bare capture yields an
        # array when psmux emits more than one line, and `-eq` on an array
        # filters instead of comparing — silently taking neither branch.
        mux = self._BINARY
        probe = (
            '"$(' + mux + " display-message -p -t $env:TMUX_PANE '#{window_id}' 2>$null)\".Trim()"
        )
        read_key = '"$(' + mux + ' show-options -qv $key 2>$null)".Trim()'
        return (
            f"$wid = {probe}; "
            # A failed probe skips the return AND the key free; the orphan
            # sweep at a later parked-window launch reclaims the key once the
            # window is gone.
            # $wid arrives as `@N` — it supplies _SCOPE_MARKER's trailing `@`.
            f"if ($wid) {{ $key = {_pwsh_quote(return_opt + self._SCOPE_MARKER[:-1])} + $wid; "
            f"$ret = {read_key}; "
            f"if ($ret -eq '{PARKED_RETURN_DETACH}') {{ {mux} detach-client 2>$null }} "
            # The switch leg's target routes on the supported build — the
            # server parses it rather than splitting it raw (psmux/psmux#483) —
            # so this is no longer the dead branch it was; whether a real client
            # actually lands there is an attended-console question, not one any
            # unattended gate here can answer.
            f"elseif ($ret) {{ {mux} switch-client -t $ret 2>$null; "
            f"if ($LASTEXITCODE -ne 0) {{ {mux} switch-client -l 2>$null }} }} "
            # Free the key on the way out: the ctl session outlives every run,
            # so a key left behind is one this server carries for its whole life.
            f"{mux} set-option -u $key 2>$null }}"
        )

    def _window_launch(self, env: dict[str, str], command: str) -> list[str]:
        # 3.3.8 delivers `new-window -e`, but the env keeps riding an in-source
        # prelude: this leaf's command already travels as `-EncodedCommand`
        # source that must run the CLAUDE_* scrub (_source_prefix) in-pane
        # anyway, so the prelude is the transport already present — `-e` flags
        # would be a second one whose values the scrub then has to be ordered
        # against. The prelude also survives `PSMUX_BARE_ENV=1` (measured both
        # ways on 3.3.8: an in-source assignment runs in the pane after the
        # allowlist clear), which is why a coding-CLI window keeps its
        # `BMAD_LOOP_STATE_DIR` pin even under that mode — see the bare-env
        # warning in `_run` for what does not. `command` arrives POSIX-quoted
        # (callers shlex-quote each arg), so split it here and re-quote for pwsh.
        for key in env:
            if not _ENV_NAME.fullmatch(key):
                raise TmuxError(f"invalid environment variable name: {key!r}")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise TmuxError(f"unparseable command: {exc}") from exc
        prelude = "".join(f"$env:{key} = {_pwsh_quote(value)}; " for key, value in env.items())
        source = self._source_prefix() + prelude + self._join_argv(argv)
        return self._shell_wrap(source)

    # ------------------------------------------------- psmux divergences

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # psmux's nesting guard refuses new-session from inside a psmux pane
        # (current builds only for an attaching one, older builds no-op'd `-d`
        # too — exit 0, nothing created); the documented bypass is one env var
        # on the create call, kept as a cheap belt. The create env copies the
        # parent env rather than building from scratch — Windows children need
        # SystemRoot etc. — but scrubs the claude session vars (the same names
        # _source_prefix clears per window): this call may cold-start the psmux
        # server, whose env every window then inherits.
        env = {
            k: v
            for k, v in os.environ.items()
            if not (
                k.upper().startswith(("CLAUDE_CODE_", "CLAUDECODE"))
                or k.upper() == "PSMUX_CLAUDE_TEAMMATE_MODE"
            )
        }
        env["PSMUX_ALLOW_NESTING"] = "1"
        geometry = ["-x", str(cols), "-y", str(lines)] if cols and lines else []
        try:
            proc = self._run(
                ["new-session", "-d", "-s", name, "-c", str(cwd), *geometry],
                check=False,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"{self._BINARY} new-session failed: {exc}") from exc
        if proc.returncode != 0:
            raise TmuxError(f"{self._BINARY} new-session failed: {proc.stderr.strip()}")
        # Belt for the nesting guard's historical no-op mode (exit 0, nothing
        # created): verify the session exists so the failure blames session
        # creation, not the next verb's "can't find session".
        if not self.has_session(name):
            raise TmuxError(
                f"{self._BINARY} new-session exited 0 but session {name!r} was not "
                "created (nesting guard no-op?)"
            )

    @staticmethod
    def _qualified_window_id(session: str, window_id: str) -> str:
        # psmux mints window ids per server (one server per session), so a bare
        # `@N` replayed as a `-t` target routes by the caller's $TMUX — from a
        # ctl pane that is the wrong server entirely (#254). A `session:@N`
        # target routes by the session's port file instead (psmux/psmux#483:
        # qualified targets are at full parity; bare ids are a permanent model
        # boundary). Degrade to the bare id when the session name is empty or
        # contains `:` (the grammar cannot carry it: `":@N"` parses sessionless
        # and `a:b:@N` splits at the wrong colon) — the #221 rule — or when the
        # id is falsy (a failure sentinel must pass through unchanged).
        #
        # Composed here rather than via self.target() — which would emit
        # `=session:@N`, and does parse (psmux strips the `=`) — because the
        # seam requires target() to stay a stable *by-name* token and `@N` is
        # an id. Do not "unify" the two grammars: current_return_target's
        # `=session:%N` is a target(), this is not.
        if not window_id or not session or ":" in session:
            return window_id
        return f"{session}:{window_id}"

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        # Session-qualify the minted id (see _qualified_window_id) so every
        # downstream `-t` consumer inherits an unambiguous target. Post-process
        # the base's return rather than reformatting `-F` so the argv stays
        # byte-identical to the base's.
        window_id = super().new_window(session, name, cwd, env, command)
        return self._qualified_window_id(session, window_id)

    def list_window_ids(self, session: str) -> list[str]:
        # psmux's list-windows emits bare `@N` lines; qualify them identically
        # to new_window or window_alive's membership check (native_id in
        # list_window_ids) would read every window as dead.
        #
        # No `_SESSION_GONE_STDERR` override, and that is a measurement rather
        # than an omission (#525). psmux 3.3.8 words a vanished session as
        # `psmux: no server running on session '<name>'` — the base's fragment
        # matches it — and its client-side variant carries `can't find session`
        # too. The failures that must NOT read as gone are worded well clear of
        # both: a live session whose key was rejected answers `psmux: Invalid
        # session key`, and one whose server is unreachable answers `psmux:
        # connection timed out`. Both were rc 1 with the windows demonstrably
        # alive — this backend is where the bug was actually reachable, because
        # a per-session TCP server has failure modes tmux's socket does not.
        return [
            self._qualified_window_id(session, window_id)
            for window_id in super().list_window_ids(session)
        ]

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # Same per-server ambiguity as new_window, worse context: the launcher
        # usually runs OUTSIDE any pane, so a bare `@N` replayed as a `-t` target
        # falls through to the most-recent-session fallback rather than the
        # session that just minted it.
        window_id = super().new_parked_window(session, name, cwd, argv, return_opt)
        # Launch time is the reconcile point for keys whose window is gone —
        # Enter-dismissing a parked window closes it without kill_window ever
        # running, so its keys would otherwise outlive it (#310).
        self._sweep_orphan_keys(session)
        return self._qualified_window_id(session, window_id)

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        # Two corrections on the columns the prune reads. `window_id` is
        # qualified — the prune replays those values as kill-window targets, and
        # a bare one can hit another server's identically-numbered window. An
        # `@` field never reaches psmux: `#{@name}` expands from the one
        # per-server map, so every row would carry the same value. That is a
        # WINDOW-row problem only — one server holds many windows but exactly
        # one session, so the same expansion in session_options' list-sessions
        # is correct by construction (see set_session_option). Probe the
        # window id in its place and fill from the id-keyed options, fetched as
        # ONE full listing — a flat extra call per list_windows, regardless of
        # row or column count (#310).
        opt_columns = {i: field for i, field in enumerate(fields) if field.startswith("@")}
        probe_fields = [
            "window_id" if i in opt_columns else field for i, field in enumerate(fields)
        ]
        rows = super().list_windows(session, probe_fields)
        id_columns = {i for i, field in enumerate(fields) if field == "window_id"}
        if not opt_columns and not id_columns:
            return rows
        if not rows:
            # No rows to fill, so the option listing below has nothing to fill
            # them WITH — a pure short-circuit, byte-identical output. It is
            # load-bearing anyway (#525): the base answers [] both for a session
            # it proved gone and for a spawn that never landed, silently in each
            # case, and pressing on would spend a second probe and then warn that
            # the option listing failed — about a session that is legitimately
            # gone, or on a box with no multiplexer at all. The base's honest
            # silences must not be re-broken by the wrapper that reads them.
            return rows
        # The #221 degrade: an empty or `:`-bearing session cannot be routed
        # with `-t`, and an unrouted read would answer from whichever server
        # the fallback picks — fill "" without issuing reads at all.
        degraded = not session or ":" in session
        options = self._scoped_options(session) if opt_columns and not degraded else None
        if opt_columns and not degraded and options is None:
            # Say it: every column degrades to "unset" at once, so a prune reads
            # as if nothing were ever tagged. Visible on the CLI prune (cli.py,
            # incl. the --dry-run this most affects); NOT under the TUI, which
            # captures stderr for the app's whole run (tui/app.py's run_tui
            # note) — a deliberate ceiling, as with every warning in this module.
            print(
                f"warning: show-options listing failed on {session}; option columns read as unset",
                file=sys.stderr,
            )
        out: list[tuple[str, ...]] = []
        for row in rows:
            values = list(row)
            for i, option in opt_columns.items():
                # values[i] is the bare `@N` probed in the option column's place.
                digits = self._id_digits(values[i])
                if degraded or not digits or options is None:
                    # Unreadable degrades to "" ("unset") rather than to a
                    # "read failed" sentinel: the caller's untagged fallback
                    # proves ownership on its own (the ctl prune claims an
                    # untagged window only when the run dir exists under THIS
                    # project, and run ids are unique — runs.new_run_id), so a
                    # third state in the column would buy only the skip of our
                    # own dead window.
                    values[i] = ""
                else:
                    values[i] = options.get(self._scoped_option_key(option, digits), "")
            for i in id_columns:
                values[i] = self._qualified_window_id(session, values[i])
            out.append(tuple(values))
        return out

    # ------------------------------------ per-window option channel (#310)
    #
    # Per-window user options do not exist on psmux: there is one scope per
    # server, and every `-w` read of an `@` name returns '' before the map is
    # consulted (a 14-name builtin allowlist gates it). Deliberate, per
    # psmux/psmux#321 — a boundary to route around, not a bug to wait out.
    #
    # Substitute: a SESSION-scoped option whose key carries the window id
    # (`@bmad_project__blw@3` for `@3`). Two rules keep it correct —
    #   - `-t <session>` on every out-of-pane write and read (the parked
    #     trailer runs in-pane and rides `$TMUX` instead — see
    #     `_parked_trailer`). psmux picks the server from the target, and
    #     without an explicit session it falls back to $TMUX / most-recent —
    #     i.e. some other server. The session comes from the qualified ids
    #     this backend already mints, which is why #310 lands after #291.
    #   - the key carries the seam-owned `__blw@` marker plus the full id
    #     (`__blw@3`, never bare digits): the ctl server loads the user's
    #     psmux config, so the map is shared, and the generic cleanup sweeps
    #     delete every key matching their suffix. No real-world naming
    #     convention collides with the marker — a user option only matches by
    #     deliberately imitating the seam (`@theme__blw@3` would; the old
    #     hazard shapes `@theme_@3` / `@color_3` cannot) — and it keeps caller
    #     names out of the backend (a bmad-owned prefix guard would hardcode
    #     them). The session stays out of the key; routing already carries it.
    #
    # Builtin window options keep the base's `-w` argv — psmux accepts those
    # allowlisted names (only `automatic-rename` has true per-window storage;
    # the rest read the global value), and rerouting them into this channel
    # would break reads that work today.

    # A window target as the seam composes it: `session:@N`, `=session:@N`, or
    # `=session:<window-name>`. Deliberately looser than _QUALIFIED_ID, which
    # only admits the id form — the seam's option/kill verbs accept name
    # tokens even though today's launch.py callers all pass ids.
    _SESSION_WINDOW = re.compile(r"^(?P<session>[^:]+):(?P<window>.+)$")

    @staticmethod
    def _id_digits(window_id: str) -> str:
        """`@3` -> `3`; `""` for anything that is not a bare window id."""
        return window_id[1:] if PsmuxMultiplexer._BARE_ID.fullmatch(window_id) else ""

    def _option_scope(self, target: str) -> tuple[str, str] | None:
        """``(session, id-digits)`` for a window target, or None when it carries
        no session and so cannot be routed to a specific server.

        A sessionless target reaches here only where `_qualified_window_id`
        degraded. Guessing a server for it is the misrouting qualification
        exists to prevent, so it returns None and the caller declines to act.
        """
        match = self._SESSION_WINDOW.fullmatch(target.removeprefix("="))
        if match is None:
            return None
        session, window = match["session"], match["window"]
        digits = self._id_digits(window)
        if not digits:
            # A name token costs one listing round-trip, and psmux can rename or
            # renumber between it and the verb using the answer. Best-effort
            # callers, one round-trip wide, so no lock.
            digits = self._id_digits(self._window_id_for_name(session, window) or "")
        return (session, digits) if digits else None

    def _window_id_for_name(self, session: str, name: str) -> str | None:
        """Bare window id of the window called ``name`` in ``session``, or None."""
        # super(): the base emits unqualified ids, the form the key needs.
        for win_id, win_name in super().list_windows(session, ["window_id", "window_name"]):
            if win_name == name:
                return win_id
        return None

    # The seam-owned key marker ("bmad-loop window"): every key this channel
    # mints ends in `<marker><digits>`, and both cleanup sweeps match keys by
    # it — keep the mint (_scoped_option_key, _parked_trailer) and the matchers
    # (_KEY_SUFFIX, kill_window) derived from this one literal (within this
    # class: _KEY_SUFFIX binds at class-body time and _scoped_option_key is
    # static, so a subclass rebinding the marker would split mint from match).
    #
    # No transition rule for the pre-marker `_@<digits>` keys, deliberately
    # (#313 floated one): a sweep matching the old shape would delete the very
    # `@theme_@3` the marker exists to protect. They just read as foreign. The
    # channel is unreleased, so only a dev build holds any, and the cost falls
    # on windows parked BEFORE the upgrade: their trailer is baked in at mint
    # (tmux_base.new_parked_window) and still reads the old key, so their tag
    # reads unset (the prune falls back to the run dir) and their return move
    # stops firing. Restarting the ctl server clears the map.
    _SCOPE_MARKER = "__blw@"

    @staticmethod
    def _scoped_option_key(option: str, digits: str) -> str:
        return f"{option}{PsmuxMultiplexer._SCOPE_MARKER}{digits}"

    # The channel's key suffix. Anchored: `@bmad_project__blw@13` must not read
    # as a key of window `@3`, and a key without the marker — `@color_3`, a
    # hand-written `@theme_@3` — never matches (see the namespace note in the
    # channel comment above).
    _KEY_SUFFIX = re.compile(re.escape(_SCOPE_MARKER) + r"(\d+)$")

    def _read_scoped(self, session: str, option: str, digits: str) -> str | None:
        # No `-w`: the session-scoped read is the one that reaches the map.
        # None = transport failure, "" = unset. The ABC read admits only "", so
        # the caller collapses them — the distinction survives as a warning.
        try:
            proc = self._run(
                ["show-options", "-qv", "-t", session, self._scoped_option_key(option, digits)],
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def _warn_unroutable(self, verb: str, target: str, option: str) -> None:
        # Silence would leave a write that looks like it landed but did not.
        print(
            f"warning: {verb} {option} skipped — {target} does not resolve to a "
            "window id on a routable session (unqualified target, unknown window "
            "name, or the name-resolve listing failed), so the per-window option "
            "channel is unavailable",
            file=sys.stderr,
        )

    @staticmethod
    def _transportable(value: str) -> bool:
        # A value that cannot make the round trip verbatim is refused loudly
        # instead of stored corrupted — a tag that reads back different from
        # what the prune compares against makes the window silently unprunable.
        #
        # 3.3.8 carries the WIRE half whole: the client's quoting, the one-shot
        # chain splitter and the server tokenizer no longer eat `\\`, a trailing
        # `\`, `;`/`\;` tokens, a bare `'` or non-ASCII whitespace
        # (psmux/psmux#547, #499, #536), so ordinary Windows paths — spaced,
        # UNC, apostrophed, trailing-separator — all pass now.
        #
        # What still fails is the READ half, which is ours, not psmux's:
        # `_scoped_options` iterates `splitlines()` and strips ONE surrounding
        # `"` pair, and both reads `.strip()`/`.Trim()`. So a `"`, any line
        # break, and leading/trailing whitespace stay refused however cleanly
        # the wire now carries them — a value that reads back different is the
        # same silently-unprunable window whichever hop mangled it. An empty
        # value is a silent server-side no-op (`-u` is the verb for that), and a
        # `-`-leading one is still dropped as a flag server-side, or flips the
        # key to unset at rc 0 (psmux/psmux#583, open upstream).
        if not value or value.startswith("-"):
            return False
        if '"' in value or value != value.strip():
            return False
        return value.splitlines() == [value]

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # Session scope itself needs no substitute channel: psmux serves one
        # session per server, so that server's single option map IS the
        # session's map — the same model that makes per-window options unusable
        # makes session options correct by construction (probed: two sessions
        # on two servers read back their own values). What it does share with
        # the window channel is a value that must survive the write AND this
        # backend's own read, and this write was ungated (#320). A corrupted
        # tag is non-empty and never equals the caller's tag again, so the
        # prune skips that session forever.
        #
        # Refusing leaves the option unset. Project ownership now uses a hex
        # digest that clears this gate by construction (#419), but the gate stays
        # as the general contract for every `@` session option.
        #
        # The refusal frees the key rather than just returning. A session this
        # backend just created is NOT a blank map — the server loads the user's
        # psmux config (same shared-map basis as the option-channel block
        # below), so this name can arrive pre-seeded. Leaving a foreign value
        # in place would read back as a real non-matching tag and strand the
        # session forever, which is exactly the failure the gate exists to stop.
        if option.startswith("@") and not self._transportable(value):
            print(
                f"warning: set-option {option} skipped on session {name} — value does "
                "not survive psmux's control-line transport verbatim; the key is freed "
                "and the option reads as unset",
                file=sys.stderr,
            )
            self._write_scoped(["set-option", "-u", "-t", name, option], option)
            return
        super().set_session_option(name, option, value)

    def _write_scoped(self, verb: list[str], key: str) -> None:
        # Both window-channel mutating verbs, one body — plus the session-tag
        # refusal's free above, which wants the same never-raise contract for
        # the same reason. A write that silently failed re-opens the mis-scoped
        # prune this channel exists to close, and a silently failed `-u` leaves
        # a live return key that replays the return move when the window's
        # command exits (or, at session scope, a pre-seeded foreign tag that
        # strands the session) — so failures are said out loud either way (the
        # verbs stay best-effort: warn, never raise).
        label = " ".join(verb[: verb.index("-t")])  # `set-option` / `set-option -u`
        try:
            proc = self._run(verb, check=False)
            if proc.returncode != 0:
                print(f"warning: {label} {key} failed: {proc.stderr.strip()}", file=sys.stderr)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"warning: {label} {key} failed: {exc}", file=sys.stderr)

    def set_window_option(self, target: str, option: str, value: str) -> None:
        if not option.startswith("@"):
            super().set_window_option(target, option, value)
            return
        # Scope first, transport second: an unroutable target has nothing to
        # write AND nothing to free, and resolving here keeps the refusal path
        # from re-entering unset_window_option and warning twice about a verb
        # the caller never issued.
        scope = self._option_scope(target)
        if scope is None:
            self._warn_unroutable("set-option", target, option)
            return
        session, digits = scope
        key = self._scoped_option_key(option, digits)
        if not self._transportable(value):
            print(
                f"warning: set-option {option} skipped — value does not survive "
                "psmux's control-line transport verbatim; the key is freed and "
                "the option reads as unset",
                file=sys.stderr,
            )
            # Free any prior value: a refused REwrite must not leave the stale
            # one to be replayed later (e.g. a parked return target).
            self._write_scoped(["set-option", "-u", "-t", session, key], key)
            return
        self._write_scoped(["set-option", "-t", session, key, value], key)

    def unset_window_option(self, target: str, option: str) -> None:
        if not option.startswith("@"):
            super().unset_window_option(target, option)
            return
        scope = self._option_scope(target)
        if scope is None:
            self._warn_unroutable("set-option -u", target, option)
            return
        session, digits = scope
        # `-u` genuinely frees the key: the server's SetOptionUnset handler
        # removes `@`-prefixed names from the map (verified at v3.3.8).
        key = self._scoped_option_key(option, digits)
        self._write_scoped(["set-option", "-u", "-t", session, key], key)

    def show_window_option(self, target: str, option: str) -> str:
        if not option.startswith("@"):
            return super().show_window_option(target, option)
        scope = self._option_scope(target)
        if scope is None:
            # An unroutable target warns for reads the same as for writes —
            # no verb is sent; "" already means "unset" to every caller.
            self._warn_unroutable("show-options", target, option)
            return ""
        value = self._read_scoped(scope[0], option, scope[1])
        if value is None:
            # Transport failure, not a miss. The return value still degrades
            # to "" ("unset") — that is all the ABC read admits — but say so.
            print(
                f"warning: show-options {option} failed on {target}; treating as unset",
                file=sys.stderr,
            )
            return ""
        return value

    def _scoped_options(self, session: str) -> dict[str, str] | None:
        """All `@`-prefixed options on ``session`` as ``{key: value}``, or None
        on any failure (distinct from {} — an empty map is a real answer).
        Live-verified on 3.3.8: `show-options -q -t <session>` lists user
        options as `@key "value"` lines. This parse is what bounds
        _transportable: it strips ONE surrounding `"` pair and iterates
        `splitlines()`, so a `"` or a line break is refused at the WRITE and the
        strip stays lossless for every value this backend stored. Known hole:
        one server request handler (the ``ShowOptions`` arm of
        ``drain_plugin_req`` in server/mod.rs) answers this listing
        empty-with-success while keys exist, so a
        surprising {} is possible and is not proof that no keys are set."""
        try:
            proc = self._run(["show-options", "-q", "-t", session], check=False)
        except (subprocess.SubprocessError, OSError, UnicodeError):
            # UnicodeError as in the base's listings (#525): this is the SECOND
            # probe of a two-probe read, so a strict-codec leaf that decoded the
            # window listing cleanly can still fault here — and the caller above
            # is a best-effort metadata op that must degrade to "unset", never
            # raise a decode error out of it.
            return None
        if proc.returncode != 0:
            return None
        options: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            name, _, rest = line.partition(" ")
            if not name.startswith("@"):
                continue
            if len(rest) >= 2 and rest.startswith('"') and rest.endswith('"'):
                rest = rest[1:-1]
            options[name] = rest
        return options

    def _free_scoped_key(self, session: str, name: str) -> None:
        # The one unset path both sweeps share, routed through the same warn-
        # never-raise body as every other write. rc-0 is not proof the key is
        # gone on psmux's write side, but a nonzero rc IS proof it is not —
        # silence here would leak a key for the server's life with no signal.
        # Delegating also contains a dead round-trip to its own key: the outer
        # guard in either sweep would otherwise catch it and abandon the keys
        # after it in the batch.
        self._write_scoped(["set-option", "-u", "-t", session, name], f"{name} on {session}")

    def _sweep_orphan_keys(self, session: str) -> None:
        # Free seam-minted (`__blw@N`) keys whose window no longer exists
        # (Enter-dismissed parked windows never pass through kill_window). The
        # marker keeps every foreign config option out of the sweep; window ids
        # are never recycled within a server, so a swept key cannot belong to a
        # future window.
        #
        # Order matters: keys are snapshotted BEFORE the live-window listing, so
        # a window minted-and-tagged between the two calls has its key outside
        # the snapshot and cannot be swept as a false orphan.
        try:
            options = self._scoped_options(session)
            if options is None:
                # Transport failure, not "no keys": a sweep that silently fails
                # every launch leaks keys with no signal anywhere.
                print(
                    f"warning: orphan-key sweep on {session} could not list "
                    "options; orphaned keys unswept until the next launch",
                    file=sys.stderr,
                )
                return
            live = set(super().list_window_ids(session))  # base = bare ids
            if not live:
                # A session being swept just minted a window, so an empty live
                # list is a failed probe, not an empty session — treating it as
                # truth would sweep every key, live windows included. Warned for
                # the same reason the listing failure above is. Since #525 the
                # only way to reach here is a listing that PROVED the session
                # gone — every other failure raises and lands in the arm below —
                # which under a just-minted window means the server died between
                # the mint and the sweep. Its keys died with it, so the warning
                # is the whole remaining duty.
                print(
                    f"warning: orphan-key sweep on {session} could not list live "
                    "windows; orphaned keys unswept until the next launch",
                    file=sys.stderr,
                )
                return
            for name in options:
                match = self._KEY_SUFFIX.search(name)
                if match and f"@{match.group(1)}" not in live:
                    self._free_scoped_key(session, name)
        except (subprocess.SubprocessError, OSError, TmuxError) as exc:
            # Reconcile is opportunistic; the mint must never fail on it. But a
            # sweep that fails every launch leaks keys for the server's whole
            # life, so the failure is at least visible.
            print(f"warning: orphan-key sweep failed on {session}: {exc}", file=sys.stderr)

    def kill_window(self, target: str) -> None:
        # Kill first, clean only once the window is verifiably gone: the ctl
        # session outlives every run, so a leaked key lives as long as the
        # server — but a kill that FAILS must leave the live window its keys
        # (the project tag scopes the prune retry; the return key keeps both
        # return legs armed, see _parked_trailer). Scope resolves before the kill because a name
        # token cannot be resolved once the window is dead. An empty liveness
        # listing means the session is gone (#525 narrowed it to that; anything
        # unproven raises into the arm below), and its keys went with the
        # server — but this path cannot tell that from the pre-#525 reading, so
        # it still degrades toward retaining them; the launch-time orphan sweep
        # reclaims whatever survived once the window is provably gone. Discovery is generic by the seam's marker — the backend must
        # not know which option names callers use. Best-effort throughout:
        # cleanup failure warns (the sweep precedent) but never blocks or
        # fails the kill.
        # Cost, accepted: two listing round-trips per kill, three for a name
        # target (name-resolve, liveness, then keys; agent-window kills pay it
        # too, for nothing — and prune_ctl_windows fans it out once per stale
        # window), plus one more from the base's survivor probe whenever the
        # kill exits non-zero — on psmux that includes ordinary window-death
        # teardown; skip-by-session-name if that ever measures.
        scope = self._option_scope(target)
        super().kill_window(target)
        if scope is None:
            return
        session, digits = scope
        try:
            live = super().list_window_ids(session)  # base = bare ids
            if not live or f"@{digits}" in live:
                return
            options = self._scoped_options(session)
            if options is None:
                # Transport failure, not "no keys" — the keys of a verified-
                # dead window are now stranded until the orphan sweep.
                print(
                    f"warning: kill-window key cleanup on {session} could not "
                    "list options; stranded keys await the orphan sweep",
                    file=sys.stderr,
                )
                return
            suffix = f"{self._SCOPE_MARKER}{digits}"
            for name in options:
                if name.endswith(suffix):
                    self._free_scoped_key(session, name)
        except (subprocess.SubprocessError, OSError, TmuxError) as exc:
            print(f"warning: kill-window key cleanup failed on {session}: {exc}", file=sys.stderr)

    def _display_message(self, fmt: str) -> str | None:
        # The base's target-less probe resolves the server's *active* window on
        # psmux, so every inherited probe (current_window_id / current_pane_id /
        # current_session / current_return_target) answers for a foreign window
        # whenever this process's window is not the focused one (gh-669): the
        # ctl prune's own-window exclusion lands on the wrong window, and the
        # attach return records a pane the human never came from. Pin the probe
        # to the calling pane, exactly as _parked_trailer already does in pwsh:
        # psmux sets TMUX_PANE in every pane, and a bare `%N` target resolves
        # globally via DisplayMessageById. Still a probe rather than a
        # short-circuit to the env value — the round trip also verifies the
        # pane is live (a dead id exits nonzero, hence None). TMUX guard first,
        # as in the base; with TMUX set but TMUX_PANE unset the probe is
        # unpinnable, and an unpinnable probe must not answer for a foreign
        # window — None without spawning.
        if not os.environ.get("TMUX"):
            return None
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return None
        # Measured on 3.3.8: an `@N`-shaped or live-session-name value in the
        # target slot resolves rc=0 to the ACTIVE window — the foreign answer
        # this override exists to eliminate — while plain garbage fails closed
        # at rc!=0. Only a pane-shaped value may reach `-t`.
        if not re.fullmatch(r"%\d+", pane):
            return None
        try:
            proc = self._run(["display-message", "-p", "-t", pane, fmt], check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    # What _qualified_window_id composes: `<session>:@<n>`. The session part
    # excludes `:` because that is exactly when qualification degrades to a bare
    # id; requiring `@<digits>` keeps a `=session:window-name` token out — which
    # is what makes it the shape current_window_id must probe back into, so the
    # prune's own-window compare lines up against list_windows' rows.
    _QUALIFIED_ID = re.compile(r"^(?P<session>[^:]+):(?P<window_id>@\d+)$")
    _BARE_ID = re.compile(r"^@\d+$")

    def current_window_id(self) -> str | None:
        # Must match list_windows' window_id form: the ctl prune skips its own
        # window by comparing them, so a mismatch makes a prune run from inside a
        # ctl window kill that window. Hence ONE probe rather than a window id
        # plus a separate session name — a split probe can resolve the id and
        # fail the session, and rows are qualified from the session list_windows
        # was *passed*, so neither a bare id nor None could ever equal one.
        probed = self._display_message("#{session_name}:#{window_id}")
        if not probed:
            return None
        if self._QUALIFIED_ID.fullmatch(probed):
            return probed
        # A `:`-bearing (or empty) session name can't carry the grammar — the
        # #221 degrade, which list_windows applies to its rows identically. A
        # malformed id is no target at all; half-parsing one would aim a kill.
        _, _, window_id = probed.rpartition(":")
        return window_id if self._BARE_ID.fullmatch(window_id) else None

    def current_return_target(self) -> str | None:
        # psmux runs one server per session, so a bare pane id recorded on a
        # control-session window is session-local at replay: at best
        # unresolvable, at worst colliding with a real control-session pane and
        # landing the client on the wrong one with exit 0 (psmux/psmux#483).
        # Qualify with the session: psmux's parse_target accepts a pane id in
        # the window slot of `=session:%N` and (on releases carrying the #483
        # fix) forwards it to the owning server, where the id resolves in
        # exactly the right per-server id space. tmux must NOT receive this
        # form — its window resolver has no pane-id handling, so the qualified
        # target errors and the return degrades to `switch-client -l` — which
        # is why composition is backend-owned rather than seam-uniform.
        pane = self.current_pane_id()
        if not pane:
            return None
        session = self.current_session()
        # Degrade to the bare id (the base default, pre-#483 hazard and all)
        # rather than None when the session probe fails, answers empty, or
        # answers a name the `=session:%N` grammar cannot carry: a resolvable
        # own pane means we ARE inside the multiplexer, so recording "detach"
        # would strand the client.
        if not session or ":" in session:
            return pane
        return self.target(session, pane)

    # ------------------------------- client verbs: observed effect (#317)
    #
    # psmux's client verbs report *dispatch*, not effect, and the seam's contract
    # is effect: an unobservable move must never answer a vacuous True. WHAT it
    # answers instead is per verb — False from detach_client, None from
    # switch_client, whose False is the stronger joint claim that a client is
    # still HERE (see TerminalMultiplexer.switch_client).
    # The exit code is trustworthy in ONE direction for every verb — a nonzero
    # one is a real failure (an unreachable session server, a target that would
    # not parse) — so the verdict source is per verb, and there are two of them.
    #
    # ``switch-client -t`` reads the exit code, gated on this session having had
    # a client to move; an rc-0 the gate cannot vouch for answers None, never
    # False — the move may well have happened. The gate is what a bare rc
    # cannot supply: the verb still
    # exits 0 with nothing attached (pinned by
    # test_premise_client_verbs_exit_zero_with_no_client_to_move), which is the
    # rc-0 no-op (#228) reached through Python. With a client present the rc is
    # the whole answer, because the target either resolved server-side and moved
    # it (psmux/psmux#483) or failed loudly — pinned in the failure direction by
    # test_adopted_switch_client_rejects_an_unresolvable_target, and measured with
    # a real attached client only by hand (no CI box has one).
    #
    # Two costs are taken deliberately here, since both are the kind that go
    # unnoticed. The gate IS an absolute count, which the delta rule below
    # forbids for good reason: a misrouted read (#315) can hand back a FOREIGN
    # session's nonzero count, and a switch that exits 0 for its own reasons
    # would then read as vouched-for. _attached_clients now refuses the answer
    # that does not NAME the session it was asked about, so what is left is a
    # foreign server whose session carries the same name — the #531 collision,
    # which no read of a name can separate. It is admitted only because the
    # alternative — the delta — is blind to the same-session move this verb's
    # main caller performs, and the damage directions are not equal: a wrong
    # True here costs one unverified hand-back claim, where the delta cost a
    # relocated human. And the rc rule
    # assumes the admitted floor: psmux/psmux#483 lands in 3.3.8, so on a build
    # forced past ``available()`` (an explicit backend override) a ``-t`` that
    # moves nobody still exits 0 and is answered True.
    #
    # ``switch-client -l`` and ``detach-client`` read the attached-client DELTA:
    # count the clients on this session before and after, and answer on the drop.
    # Neither can use rc — ``-l`` has no target form and carries no server reply
    # at all, and ``detach-client`` exits 0 with zero clients attached (a
    # flag-less detach is promoted server-side to detach-all). Never an absolute
    # count: a misrouted read answers for a session nobody asked about, so "zero
    # attached" on its own proves nothing (#315) — and the identity compare in
    # _attached_clients narrows that to a same-name foreign server rather than
    # closing it. Against a DIFFERENTLY-named server the delta is additionally
    # safe: manufacturing a drop needs two successful reads, and the second one
    # is refused by name. Against a same-NAME one it is not — that server's own
    # client detaching between the two reads is a real drop, of a real client,
    # in the wrong session. #531 is the only thing that closes it.
    #
    # The delta is why ``-t`` could not stay on it (#659): a switch whose target
    # lives in THIS session moves the client between windows without changing the
    # session's count, so a real move read as no effect. That was not merely a
    # conservative False. With ``last_fallback`` set — which is exactly how
    # ``tui.launch.return_attached_client`` calls it — the failed verdict fired
    # ``-l``, which dragged the client out to an unrelated session and produced
    # the delta the correct move never could, so the call returned True and the
    # caller cleared its return option. Measured on a live attended client:
    # right move, undone, reported as success. Which is why the fallback in
    # switch_client hangs on the rc and not on the verdict — see the comment
    # there; a verdict-gated fallback keeps that drag alive for every rc-0
    # switch the gate merely cannot vouch for.

    def _attached_clients(self, session: str) -> int | None:
        """Clients attached to ``session``, or None when psmux cannot say.

        Self-detecting on purpose, in two directions. An unsupported format
        field cannot answer a plausible integer, so a build that does not carry
        ``#{session_attached}`` degrades to None instead of a wrong count. And
        the read reports the session it answered FOR: ``-t`` resolves through
        the named port AND key files, so when that pair addresses some OTHER
        live server the answer is that server's — rc 0, plausible count, wrong
        session (measured on 3.3.8: port and key copied from a live session
        answered ``0|alive`` for ``-t forged``, naming itself). Both files are
        required — a key mismatch is rejected before the command runs — so this
        is a duplicated registry entry, not a merely recycled port. Comparing
        the answered name against the requested one is what refuses it. The seam's
        ``={session}`` token cannot: psmux strips the ``=`` before it routes
        anything (``parse_target`` → ``strip_exact_match_prefix``), so both
        spellings were byte-identical in every probed state. What survives the
        compare is a genuine same-NAME session on a foreign server, which no
        name can separate — see the module docstring and #531.

        The count comes FIRST in the format so the split is unambiguous: it is
        all digits and so cannot contain the separator, which leaves the name
        as the whole remainder even when the name contains one.
        """
        # The #221 rule the sibling call sites already apply (see
        # current_return_target): `a:b` parses as session `a`, window `b`, so a
        # `:`-bearing name addresses something else entirely. Refuse before
        # spawning rather than trusting what comes back.
        if not session or ":" in session:
            return None
        try:
            proc = self._run(
                ["display-message", "-p", "-t", session, "#{session_attached}|#{session_name}"],
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        # `session` reaches here from `current_session`, which is
        # `_display_message` and therefore ALREADY `.strip()`ped, so the name we
        # ask with is the normalized one. That bounds what this compare can and
        # cannot do, in both directions:
        #
        #   - A session whose REAL name carries surrounding whitespace is
        #     unreachable through the normalized name anyway — the registry is
        #     one file per name, so `-t ctl` finds no `ctl.port` for a session
        #     named `" ctl"` and fails closed at rc 1. There is no own-count case
        #     to protect here, which is why the answer is stripped the same way
        #     rather than preserved.
        #   - The converse is the hazard: with a real `"ctl"` also present,
        #     `-t ctl` reaches THAT session and its count passes the compare.
        #     Measured on 3.3.8 — `" ctl"` and `"ctl"` coexist as ` ctl.port` and
        #     `ctl.port`, and the read answers `0|ctl`. A name cannot separate
        #     names the seam has already merged; that is the #531 family, the
        #     same ceiling the same-name foreign server sits under.
        #
        # Control characters need no handling: psmux escapes them in format
        # output, so a session whose name ends in a real carriage return answers
        # with the literal characters `\` and `r` and fails the compare like any
        # other mismatch. Measured on 3.3.8 through the hardest route available —
        # duplicate a live session's port and key under a second name, then
        # rename the original to a CR-bearing name so the surviving alias reaches
        # a server whose in-memory name differs only by the control character.
        count, _, answered = proc.stdout.strip().partition("|")
        if answered != session or not count.isdigit():
            return None
        return int(count)

    def _client_left(self, verb: list[str]) -> bool | None:
        """Run a client verb and answer whether a client left this session —
        None both when that cannot be established and when it can but the
        session had nobody on it to begin with. See
        TerminalMultiplexer.switch_client for why those share an answer: the
        seam's False is the joint claim that a client is still HERE, which an
        empty session refutes rather than supports."""
        session = self.current_session()
        if not session:
            # Not inside a pane (or the probe failed): there is no "this
            # session" to measure against, and no client of ours to move. No
            # verb is dispatched, so nothing moved and whoever was here still
            # is — the joint claim, hence a real False.
            return False
        before = self._attached_clients(session)
        try:
            self._run(verb, check=False)
        except subprocess.TimeoutExpired:
            # The verb may have landed after our wait ran out, and an `after`
            # read now measures a session the client may still be leaving. No
            # drop can be established in either direction.
            return None
        except (subprocess.SubprocessError, OSError):
            # A spawn-level fault is proof the verb never ran — but "nothing
            # moved" is only the first half of the seam's False, and the count
            # read before the verb still governs the second. An empty or
            # unreadable session leaves it unvouched here exactly as it does
            # past the verb, which is what this function's own docstring
            # promises; probing again now would only measure a broken transport.
            if before is None or before == 0:
                return None
            return False
        after = self._attached_clients(session)
        if before is None or after is None:
            return None
        if before == 0:
            # Nothing was attached, so nothing left — but there is nobody here
            # to keep prompting at either, which is not what False claims.
            return None
        return after < before

    def detach_client(self) -> bool:
        # This leg stays a bool: return_attached_client already routes a failed
        # detach to UNREACHABLE, so the state a None would add is one the
        # caller has no separate answer for.
        return self._client_left(["detach-client"]) is True

    def switch_client(self, target: str, last_fallback: bool = False) -> bool | None:
        # Two questions, deliberately separate: did the verb SUCCEED (rc), and
        # was there a client here to succeed on (the gate count, read BEFORE the
        # verb — a successful move can take the client off this session, so a
        # read taken after would answer 0 for the very case it has to admit).
        #
        # The fallback hangs on the rc alone, never on the verdict. `-l` has no
        # target form: it relocates whichever client the server last had, so
        # firing it after a `-t` that already succeeded is the #659 drag itself —
        # the correct move undone, and the delta the drag produces then read as
        # success. An rc-0 switch we merely cannot VOUCH for (count unreadable,
        # or zero clients here) is still a switch that happened or a no-op that
        # moved nobody; either way there is nothing to fall back to. This is the
        # `$LASTEXITCODE -ne 0` rule the pwsh trailer (_parked_trailer) has
        # always used, and the same shape as the tmux leaf's switch_client.
        #
        # What the gate cannot VOUCH for answers None rather than False. The
        # seam's False is the joint claim "no switch happened AND the client is
        # still here", and every unvouched case below is one where the client
        # may already be gone; collapsing them into False sends the return path
        # back to prompting a window nobody is viewing, which a --repeat sweep
        # then blocks on forever (the parked trailer's retry is no rescue — it
        # sits behind that same blocking read).
        session = self.current_session()
        if not session:
            # Not inside a pane (or the probe failed): there is no "this
            # session" to measure against, and no client of ours to move. No
            # verb is dispatched, so nothing moved — and when it is the probe
            # that failed rather than the pane that is absent, a wedged server
            # is a frozen terminal someone may still be sitting at. The second
            # half of the claim is a policy call here, not a measurement, and
            # False is the half that keeps talking to them.
            return False
        before = self._attached_clients(session)
        try:
            sent = self._run(["switch-client", "-t", target], check=False).returncode == 0
        except subprocess.TimeoutExpired:
            # A timeout is not a failure — it is the absence of an answer. The
            # server may well have moved the client before our wait ran out, so
            # this is the one exit that must neither claim the move nor fall
            # back: `-l` on a client that already went where it was asked is the
            # #659 drag, and the drag manufactures the delta that would report it
            # as a success. None leaves the caller its return option (cleared
            # only on a True) AND stops it prompting a window the client may
            # have left.
            return None
        except (subprocess.SubprocessError, OSError):
            # Spawn-level faults, by contrast, are proof the verb never ran:
            # nothing moved, so the fallback is as available as after a nonzero rc.
            sent = False
        if sent:
            # "Cannot say" and "nobody here" are different facts — an old build
            # with no #{session_attached} versus a session the client has
            # already left — and both refuse the True. They share a verdict
            # only because neither can vouch that a human is still watching.
            if before is None or before == 0:
                return None
            return True
        if last_fallback:
            return self._client_left(["switch-client", "-l"])
        # The refusal carries the joint claim only if a client was measurably
        # here to refuse on. Same gate as the rc-0 arm, for the same reason: an
        # unreadable count and an empty session each leave the "still here" half
        # unvouched, and which way the rc went does not touch that half. tmux
        # reaches this by probing AFTER a failed verb; the pre-verb read is what
        # lets this leaf answer it on the spawn-fault exit too, where a probe
        # taken now would be measuring an already-broken transport.
        if before is None or before == 0:
            return None
        return False

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # The base's POSIX `cat >>` sink assumes a POSIX host shell, so the sink
        # is pwsh source shipped through the same `-EncodedCommand` transport as
        # every other window command (psmux 3.3.8 passes dash-flag tokens and
        # quoting through pipe-pane intact — psmux/psmux#482, psmux/psmux#563).
        # Space-joining the wrapped argv needs no quoting of its own: base64 is
        # `[A-Za-z0-9+/=]`, and the log path rides inside the encoded source via
        # _pwsh_quote, so a spaced, `$`-bearing or backticked path is carried
        # verbatim rather than re-parsed. The sink is byte-exact like `cat >>`
        # (raw stream copy: no console decode of the pane bytes, no re-encode, no
        # CRLF normalization) and flushes per chunk: the run log is live-tailed
        # for activity detection, and a buffered copy never surfaces bytes — pipe
        # EOF is unreliable on psmux. Known ceiling: a spawn race that exits 0
        # still yields a silent empty log — the warning below covers surfaced
        # failures only.
        sink = (
            "$in = [System.Console]::OpenStandardInput()\n"
            f"$out = [System.IO.File]::Open({_pwsh_quote(str(log_file))}, "
            "'Append', 'Write', 'Read')\n"
            "$buf = New-Object byte[] 4096\n"
            "while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) "
            "{ $out.Write($buf, 0, $n); $out.Flush() }\n"
            "$out.Dispose()\n"
        )
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", " ".join(self._shell_wrap(sink)))
        except (TmuxError, UnicodeEncodeError) as exc:
            # Best-effort, as the base: a window that died on launch (or psmux's
            # first-pipe-after-new-window spawn race, noted in psmux/psmux#482)
            # is not a setup failure — but say so, or an empty run log is
            # unexplainable. UnicodeEncodeError joins it because the sink source
            # (log path included) rides a UTF-16LE encode that _tmux never sees;
            # a timeout / missing binary already arrives as TmuxError from there.
            print(
                f"warning: pipe-pane log capture failed for {window_id}: {exc}",
                file=sys.stderr,
            )

    # Releases up to this version are refused. 3.3.6 and older force-kill a
    # recycled PID during pane teardown and let orphaned servers accumulate —
    # engine-fatal on its own. 3.3.7 is excluded for a second reason: 3.3.8 is
    # the build this backend is written against, and several verbs here now
    # assume its fixes rather than routing around the defects (a direct
    # `pipe-pane -o` flag transport, a `select-window` id target, the `=name`
    # kill-session form, and the control-line shapes `_transportable` admits).
    # On 3.3.7 those would fail silently, so the floor forbids it outright.
    _LAST_UNSUPPORTED = (3, 3, 7)
    # Class-level default; instances shadow it on first probe. Never assign on
    # the class outside tests — that would poison every future instance.
    _version_ok: bool | None = None

    def available(self) -> bool:
        # Every window launch needs pwsh alongside the psmux binary itself.
        # The version gate fails closed: psmux prints `tmux X.Y.Z` (the tmux
        # prefix is kept deliberately for tmux-version parsers), and an old or
        # unidentifiable install reads as unusable. A forced backend name (env
        # var or policy) still bypasses this probe, with a warning at the
        # launch gates (see multiplexer.mux_usable). The gate verdict is cached
        # on the instance so repeated availability polls don't each spawn a
        # version query; the lru-cached selected instance re-probes a swapped
        # install only on restart (detect_multiplexers' fresh instances
        # re-probe every call).
        if not all(shutil.which(exe) for exe in (self._BINARY, "pwsh")):
            return False
        if self._version_ok is None:
            # A missing patch segment reads as 0 — psmux hardwires three-part
            # Cargo semver today, but real tmux versions are two-part and the
            # compat prefix invites upstream to mirror that format someday.
            reported = re.match(r"tmux (\d+)\.(\d+)(?:\.(\d+))?", self.version() or "")
            self._version_ok = bool(reported) and (
                tuple(int(part or 0) for part in reported.groups()) > self._LAST_UNSUPPORTED
            )
        return self._version_ok
