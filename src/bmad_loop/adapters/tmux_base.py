"""Shared tmux-family backend base for the terminal-multiplexer seam.

This module is the **quarantine** for tmux/POSIX-shell knowledge: every tmux
invocation and POSIX-shell trailer lives here (and in its POSIX leaf
:mod:`.tmux_backend`). The point of the split is that a tmux-*family* backend —
the native-Windows :mod:`.psmux_backend` leaf — can subclass :class:`BaseTmuxBackend` and
swap only class attributes (:attr:`BaseTmuxBackend._BINARY` for the spawned
binary, :attr:`BaseTmuxBackend._ENCODING` / :attr:`BaseTmuxBackend._ERRORS`
for output decoding, :attr:`BaseTmuxBackend._SESSION_GONE_STDERR` for the stderr
wordings that prove a session is gone rather than a listing merely failed, #525
— a scrubbed
per-call ``env`` is a ``_run`` parameter, and an :meth:`BaseTmuxBackend._run`
override is left for timeout tweaks) plus the shell-dialect hooks (``_shell_wrap``, ``_join_argv``,
``_parked_trailer``, ``_source_prefix``, ``_window_launch`` and the
``_EXIT_CAPTURE``/``_ECHO``/``_PARK`` fragments), **without editing**
:mod:`.tmux_backend`. For :meth:`~BaseTmuxBackend.new_window` /
:meth:`~BaseTmuxBackend.new_parked_window` the hooks replace method-body
overrides entirely; :meth:`~BaseTmuxBackend.pipe_pane` still hands the
multiplexer a POSIX ``cat >>`` redirection, so a non-POSIX leaf overrides it
directly, alongside whatever divergences its multiplexer forces on it.

Every method that talks to tmux funnels through :meth:`BaseTmuxBackend._run`, the
one place a subprocess is spawned. See :mod:`.multiplexer` for the contract.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .multiplexer import MultiplexerError, TerminalMultiplexer, fold_version

TMUX_TIMEOUT_S = 30
# Per-window option value (vs a pane target) telling the parked trailer to detach
# the client rather than switch it. Recorded targets are backend-composed pane
# targets (bare %N on tmux, =session:%N on psmux — see
# TerminalMultiplexer.current_return_target), so this never collides with one.
PARKED_RETURN_DETACH = "detach"


class TmuxError(MultiplexerError):
    pass


class BaseTmuxBackend(TerminalMultiplexer):
    """tmux-family backend: all argv construction and every contract method, with
    one overridable subprocess primitive (:meth:`_run`) every call funnels through.
    The seam-canonical target grammar (``=session[:window]``, see
    :meth:`TerminalMultiplexer.target`) coincides with tmux's exact-match target
    syntax, so targets pass straight through to tmux — never parsed here."""

    #: The binary every spawn, PATH probe, and in-source client verb targets.
    #: A tmux-family leaf whose binary is not literally named ``tmux`` overrides
    #: this one name instead of any method body.
    _BINARY = "tmux"
    #: Output decoding for captured tmux text. ``None`` (POSIX) = locale default,
    #: byte-identical to a bare ``text=True``; a Windows leaf sets ``"utf-8"``.
    _ENCODING: str | None = None
    #: Decode error handling to pair with :attr:`_ENCODING`. ``"backslashreplace"``
    #: on every platform (#380): a stray byte the codec cannot decode degrades
    #: visibly to a ``\xNN`` escape in the captured text instead of raising
    #: mid-capture. Honored even where :attr:`_ENCODING` is None, so POSIX keeps
    #: the locale codec and only stops being strict. A leaf may still override.
    _ERRORS: str | None = "backslashreplace"
    #: stderr fragments (matched case-insensitively, as substrings) that PROVE
    #: the target session is gone, as opposed to a listing that merely failed.
    #: This is the whole discrimination rule behind :meth:`_session_proved_gone`
    #: — see it for why the answer has to be stderr and why the tuple is narrow.
    #: A tmux-family leaf whose multiplexer words this differently overrides the
    #: tuple; psmux deliberately does not (its own wordings are covered — see
    #: the note in :mod:`.psmux_backend`).
    _SESSION_GONE_STDERR: tuple[str, ...] = ("no server running", "can't find session")
    #: Diagnostic from the last :meth:`version` probe (see
    #: :meth:`TerminalMultiplexer.version_error`). A class-level default so an
    #: instance that never probed answers None instead of AttributeError.
    #: Per-instance and unsynchronized: only a caller that OWNS the instance may
    #: read it back (``detect_multiplexers`` builds one per row). The
    #: ``get_multiplexer()`` singleton is shared across the TUI's worker threads,
    #: and ``mux_usable`` probes ``version()`` on it — a reader there can be
    #: handed another thread's failure.
    _version_error: str | None = None

    def _run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The ONE place tmux is spawned. ``argv`` are the args after the binary.

        With ``check=True`` a non-zero exit raises :class:`TmuxError` (the strict
        form behind ``_tmux``); with ``check=False`` the completed process is
        returned as-is so callers can apply their own tolerant return-code handling.
        A timeout / missing binary always propagates (``TimeoutExpired`` / ``OSError``)
        so callers' existing ``try/except`` still fires.

        ``env`` (keyword-only, default ``None`` → inherit the parent env) lets one
        caller spawn with a scrubbed env without mutating this process's; decoding is
        the :attr:`_ENCODING` class attr. A leaf sets those rather than overriding here.
        Build a scrubbed env by copying the parent env and *removing* the offending
        vars — not from scratch (on Windows the child needs ``SystemRoot`` etc.).
        """
        proc = subprocess.run(
            [self._BINARY, *argv],
            capture_output=True,
            text=True,
            encoding=self._ENCODING,
            errors=self._ERRORS,
            env=env,
            timeout=TMUX_TIMEOUT_S,
        )
        if check and proc.returncode != 0:
            raise TmuxError(f"{self._BINARY} {' '.join(argv[:2])} failed: {proc.stderr.strip()}")
        return proc

    def _session_proved_gone(self, proc: subprocess.CompletedProcess[str]) -> bool:
        """Whether a non-zero listing exit PROVES the session no longer exists.

        The discrimination the seam's ``[]`` rests on (#525). A non-zero exit
        covers two unrelated answers: the session is gone (an ordinary,
        knowable fact) and the listing could not be taken at all (a server that
        errored while its windows are alive, a rejected auth, an unreachable
        port). Folding both to ``[]`` reports the second as "this session has
        no windows" — a death the engine acts on and a kill the prune reports
        as verified.

        Only stderr can tell them apart: the exit CODE cannot (both multiplexers
        exit 1 for everything), and a confirming ``has_session`` round trip
        cannot either — that predicate folds every non-zero exit to False by its
        own documented contract, so it answers "gone" for exactly the auth and
        timeout faults this discrimination exists to catch.

        The tuple is an ALLOWLIST, deliberately: an unrecognized wording is
        unknowable, never proof. The failure mode of a too-narrow tuple (a
        vanished session read as unverifiable) is loud and self-clearing; a
        too-wide one silently restores the bug. Measured rather than assumed,
        on tmux 3.4 and psmux 3.3.8 — see the ``_SESSION_GONE_STDERR`` rows in
        tests/test_multiplexer.py, which carry the transcript. Neither binary
        localizes these strings, so a case-insensitive substring match is enough.

        Both operands are folded, not just the captured text: the case-folding is
        a promise to the OVERRIDING leaf, and folding one side only would honor it
        for the base's own (already lower-case) tuple while silently breaking a
        leaf that spelled its fragment the way its binary prints it. A BLANK
        fragment is dropped rather than matched: ``"" in err`` is true for every
        error there is, and ``" " in err`` for very nearly as many — any message
        with a space in it. Either is #525 restored in full and in silence, from
        an authoring slip (a trailing comma, a fragment built from config) that
        no reviewer would see, so the two slips that could reintroduce the bug
        cannot. The fragment is dropped, not stripped: a leaf that meant a
        leading space meant it, and silently re-anchoring its fragment would
        substitute a different rule for the one it declared.
        """
        err = proc.stderr.lower()
        return any(f.lower() in err for f in self._SESSION_GONE_STDERR if f.strip())

    def _warn_unproven_listing(
        self, verb: str, proc: subprocess.CompletedProcess[str] | BaseException
    ) -> None:
        """Say out loud that a METADATA listing failed for a reason other than the
        session being gone (#525).

        The same lens as :meth:`list_window_ids`, the opposite conclusion: these
        callers read tags and window rows, not liveness, and their sentinel
        degrades toward doing NOTHING — an empty candidate list prunes nothing,
        an unread tag reads as untagged and is left alone — so the documented
        ``[]`` / ``{}`` stays. What was missing is the signal: a server erroring
        on every call made the tool behave as if the sessions it manages had
        simply stopped existing, with nothing on stderr to say why. Silent for a
        genuinely gone session, which is an answer, not a fault.

        Takes the completed process OR the transport exception that replaced one:
        a timeout and a spawn that died prove no more about the session than an
        unrecognized non-zero exit does, and the caller's sentinel is the same,
        so the signal must be too.

        The one failure left deliberately silent is a multiplexer that is not
        installed at all: a box without one has no sessions to report on, so the
        absence is an answer rather than a fault (the standing reading, see
        :meth:`list_sessions`). That check lives HERE, gating only the
        diagnostic, and must not be hoisted into the callers as an early return.
        A ``shutil.which`` short-circuit ahead of :meth:`_run` decides the
        RETURN VALUE from the ambient PATH, which makes the seam unreachable
        through the one spawn primitive — every caller and every test that
        injects a transport gets the sentinel no matter what it injected, and
        the divergence hides on whichever platform happens to have the binary.
        Gating the warning costs one failed exec on a binary-less box and keeps
        the answer where the contract says it comes from.

        Warn-only by construction, like every other diagnostic in this module:
        under the TUI stderr is captured for the app's whole run (see
        ``tui/app.py``), so this is a CLI-visible signal.
        """
        if not shutil.which(self._BINARY):
            return
        if isinstance(proc, BaseException):
            outcome, detail = "failed", f"{type(proc).__name__}: {proc}"
        else:
            if self._session_proved_gone(proc):
                return
            outcome = f"exited {proc.returncode}"
            detail = proc.stderr.strip() or "(no stderr)"
        print(
            f"warning: {self._BINARY} {verb} {outcome} without proving "
            f"the session gone; reading it as empty: {detail}",
            file=sys.stderr,
        )

    def _tmux(self, *args: str) -> str:
        # The strict form: a non-zero exit already raises TmuxError inside _run.
        # A timeout / missing binary escapes _run raw, so trap it here once and
        # re-raise as the seam type — this covers every _tmux caller (new_session,
        # set_session_option, new_window, new_parked_window, send_text) and, via
        # its own `except TmuxError`, pipe_pane too.
        try:
            return self._run(list(args), check=True).stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"{self._BINARY} {args[0] if args else ''} failed: {exc}") from exc

    # ----------------------------------------------------------- sessions

    def has_session(self, name: str) -> bool:
        # has-session returns nonzero for an absent session (a normal answer, not an
        # error), so this can't use check=True. But a timeout or a missing binary
        # is a real backend failure: raise the seam type so callers catch it via
        # MultiplexerError instead of a raw subprocess error escaping.
        #
        # Strength of a False: EVERY nonzero exit maps to it — "no such session",
        # "no server running", and a target the grammar could not parse alike. That
        # is exactly right for the create-if-missing callers this predicate was
        # written for, where a wrong False self-corrects on the next line. It is
        # weaker than it looks for a caller that reports the answer as evidence
        # (#489), which is why that one words its output as what the negative
        # withdraws rather than what it proves. Deliberately NOT tightened here:
        # `list_window_ids` raises on transport failure because it backs a liveness
        # probe, and this predicate has no such duty to its existing callers.
        try:
            probe = self._run(["has-session", "-t", f"={name}"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"{self._BINARY} has-session failed: {exc}") from exc
        return probe.returncode == 0

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # Window 0 is a plain shell so the session survives task windows closing.
        # Geometry is pinned only when both dimensions are given (detached agent
        # sessions); the control session omits it and takes tmux's default size.
        geometry = ["-x", str(cols), "-y", str(lines)] if cols and lines else []
        self._tmux("new-session", "-d", "-s", name, "-c", str(cwd), *geometry)

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # set-option has no '=' exact-match form; callers pass a unique full
        # session name so plain-name targeting resolves it unambiguously.
        self._tmux("set-option", "-t", name, option, value)

    def kill_session(self, name: str) -> None:
        # Tolerant of the binary being absent / the session already gone: a
        # best-effort teardown backstop, never a hard failure.
        if not shutil.which(self._BINARY):
            return
        try:
            self._run(["kill-session", "-t", f"={name}"], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def list_sessions(self) -> list[str]:
        # [] when the binary is missing, no server is running, or the query fails
        # — the absence of sessions and the absence of the multiplexer are
        # indistinguishable here and callers treat both as "nothing live".
        if not shutil.which(self._BINARY):
            return []
        try:
            proc = self._run(["list-sessions", "-F", "#{session_name}"], check=False)
        except (subprocess.SubprocessError, OSError):
            return []
        if proc.returncode != 0:  # no server / no sessions
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def session_options(self, option: str) -> dict[str, str]:
        # Map session name -> value of ``option`` ("" when unset). Same missing
        # binary / no-server tolerance as list_sessions().
        if not shutil.which(self._BINARY):
            return {}
        try:
            proc = self._run(
                ["list-sessions", "-F", f"#{{session_name}}\t#{{{option}}}"], check=False
            )
        except (subprocess.SubprocessError, OSError, UnicodeError) as exc:
            # UnicodeError for the reason list_window_ids names it: a leaf that
            # overrides _ERRORS back to a strict handler raises a ValueError-family
            # decode error neither other arm covers, and this method's contract is
            # warn-and-sentinel, never a raise.
            self._warn_unproven_listing("list-sessions", exc)
            return {}
        if proc.returncode != 0:  # no server / no sessions
            self._warn_unproven_listing("list-sessions", proc)
            return {}
        options: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            name, _, value = line.partition("\t")
            if name:
                options[name] = value
        return options

    # ------------------------------------------------- shell dialect seam

    # new_window / new_parked_window own the tmux argv construction and the
    # parked-window protocol; everything shell-*dialect* about them routes
    # through the hooks below so a non-POSIX leaf overrides string fragments,
    # never a contract method body. Defaults are POSIX sh, so a non-POSIX leaf
    # must override every hook whose default emits sh syntax: the three
    # fragments, _join_argv, _parked_trailer, and _shell_wrap.

    #: Fragments of the parked recipe. The banner line reads ``$ec`` verbatim
    #: and stays dialect-neutral only because every dialect of the family
    #: interpolates ``$ec`` inside its double-quoted strings — so an
    #: _EXIT_CAPTURE override MUST bind the variable ``ec``.
    _EXIT_CAPTURE = "ec=$?"
    _ECHO = "echo"
    _PARK = "read -r"

    def _join_argv(self, argv: list[str]) -> str:
        """Render ``argv`` as one shell command line in this dialect."""
        return shlex.join(argv)

    def _source_prefix(self) -> str:
        """Dialect prelude prepended to a parked window's shell source.

        The recipe adds no separator, so an override must return ``""`` or a
        self-terminating statement ending in ``"; "``.
        """
        return ""

    def _shell_wrap(self, source: str) -> list[str]:
        # Explicit `sh -c` (the user's login shell may be fish) — the one place
        # a window's shell source is turned into a spawnable argv.
        return ["sh", "-c", source]

    def _parked_trailer(self, return_opt: str) -> str:
        # After the park, switch an attached client back to its origin pane.
        # `return_opt` names the per-window option; on its recorded value:
        #   - a pane target (backend-composed by current_return_target, replayed
        #     opaquely here — bare %N on tmux, =session:%N on psmux): switch
        #     that client back there (`switch-client -l` is a best-effort
        #     fallback when it is gone);
        #   - PARKED_RETURN_DETACH: detach the client so a blocking
        #     `tmux attach` returns and a suspended TUI resumes;
        #   - unset/empty: nobody attached interactively -> park as-is.
        # The tmux verbs are protocol-identical across the family; only the
        # surrounding control-flow syntax is dialect-specific.
        mux = self._BINARY
        return (
            f"ret=$({mux} show-options -wqv {shlex.quote(return_opt)} 2>/dev/null); "
            f'if [ "$ret" = "{PARKED_RETURN_DETACH}" ]; then {mux} detach-client 2>/dev/null; '
            'elif [ -n "$ret" ]; then '
            f'{mux} switch-client -t "$ret" 2>/dev/null || {mux} switch-client -l 2>/dev/null; '
            "fi"
        )

    def _window_launch(self, env: dict[str, str], command: str) -> list[str]:
        """Trailing ``new-window`` args: env injection plus the command itself.

        Part of the dialect seam because the env-injection *strategy* is
        dialect-coupled: bare ``-e`` flags plus the raw command here, an
        in-source prelude for a leaf whose shell wraps the command.
        """
        env_args: list[str] = []
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        return [*env_args, command]

    # ------------------------------------------------------------ windows

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        return self._tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "-P",
            "-F",
            "#{window_id}",
            *self._window_launch(env, command),
        )

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # Run argv, then park on a blocking read so the exit status stays
        # inspectable instead of tmux closing the window the moment the process
        # exits; the trailer (see _parked_trailer) then returns an attached
        # client to where it came from.
        source = self._source_prefix() + (
            f"{self._join_argv(argv)}; {self._EXIT_CAPTURE}; "
            f'{self._ECHO} "[bmad-loop exited $ec — press enter]"; '
            f"{self._PARK}; {self._parked_trailer(return_opt)}"
        )
        return self._tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            *self._shell_wrap(source),
        )

    def list_window_ids(self, session: str) -> list[str]:
        # display-message -t <dead-window> exits 0 with empty output, so list the
        # session's window ids and check membership instead.
        #
        # A failed listing must RAISE, not return []. window_alive() is the engine's
        # liveness probe; a sentinel [] would falsely read as "window dead -> session
        # crashed". The honest answer to "is it alive?" is "unknowable" ->
        # MultiplexerError. That covers the transport failures below (timeout /
        # missing binary / decode) AND a non-zero exit that does not prove the
        # session is gone (#525) — see _session_proved_gone for the discrimination
        # and why an exit code alone cannot make it. A session that really vanished
        # still returns [] (no exception): the prune must be able to report its
        # kills as removed rather than invent a phantom survivor.
        #
        # UnicodeError is a transport failure too. _run no longer decodes strictly
        # on any platform (_ERRORS is backslashreplace, #380), so this arm is now
        # defence for a leaf that overrides _ERRORS back to a strict handler: such
        # a decode raises a ValueError-family error that neither exception arm
        # above names. It stays because the seam-honesty contract above is what
        # callers rely on — prune_ctl_windows takes its post-kill verdict from this
        # probe and only a MultiplexerError lands the candidates in `unverifiable`
        # rather than aborting cleanup mid-receipt (#435).
        try:
            probe = self._run(
                ["list-windows", "-t", f"={session}", "-F", "#{window_id}"], check=False
            )
        except (subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
            raise TmuxError(f"{self._BINARY} list-windows failed: {exc}") from exc
        if probe.returncode != 0:
            if self._session_proved_gone(probe):
                return []
            raise TmuxError(
                f"{self._BINARY} list-windows on {session} exited {probe.returncode} "
                f"without proving the session gone: {probe.stderr.strip() or '(no stderr)'}"
            )
        return probe.stdout.split()

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # A CLI that crashes on launch (bad args, instant auth failure) can take
        # its window down before pipe-pane attaches, which races as "can't find
        # window". That is not a setup failure, so tolerate it instead of raising
        # — but say so, or an empty run log is unexplainable.
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", f"cat >> {shlex.quote(str(log_file))}")
        except TmuxError as exc:
            print(
                f"warning: pipe-pane log capture failed for {window_id}: {exc}",
                file=sys.stderr,
            )

    def send_text(self, window_id: str, text: str) -> None:
        self._tmux("send-keys", "-t", window_id, "-l", text)
        time.sleep(0.3)  # let the TUI ingest the paste before submitting
        self._tmux("send-keys", "-t", window_id, "Enter")

    def kill_window(self, target: str) -> None:
        # Best-effort teardown: a hang / missing binary is no worse than the window
        # already being gone, so swallow to the documented no-op sentinel. A
        # non-zero exit still says so out loud — the return value stays None and
        # nothing raises — but only when the window the target names is still
        # there to leak.
        #
        # An ALREADY-GONE window exits non-zero too, and that is ordinary
        # teardown, not a fault — the DOMINANT case, not an edge one:
        # CodingCLIAdapter.run kills in a `finally` on every session, and a
        # session that completed by window death has nothing left to kill. The
        # return code cannot separate the two, so the survivor is what decides.
        # Replaying the failed target cannot decide it: a target the kill could
        # not resolve is a target a probe cannot resolve either, so a
        # same-target probe reads "gone" for the very failures it exists to
        # catch. For a session-qualified id target the session's OWN window
        # list answers instead — it resolves independently of the failed
        # target, and a leaked window is by definition still in it. So the
        # warning covers exactly the detectable leak class: the kill failed and
        # the window it named is still listed. A target that names no window at
        # all leaks nothing and stays silent. The probe is paid only on a
        # non-zero exit — which on psmux includes ordinary window-death
        # teardown, one listing alongside the ones the psmux override already
        # pays. An unreadable probe stays silent: this is a diagnostic, and
        # guessing would put the noise back on the path the probe exists to
        # clear.
        try:
            proc = self._run(["kill-window", "-t", target], check=False)
        except (subprocess.SubprocessError, OSError):
            return
        if proc.returncode == 0 or not self._window_survived_kill(target):
            return
        detail = proc.stderr.strip()
        print(
            f"warning: kill-window {target} exited {proc.returncode} and the window "
            f"is still alive{f': {detail}' if detail else ''}",
            file=sys.stderr,
        )

    def _window_survived_kill(self, target: str) -> bool:
        """Whether the window ``target`` names outlived a failed kill.

        False for both "provably gone" and "cannot tell" — the caller only warns,
        so an unanswerable probe must not manufacture a warning.
        """
        session, sep, window = target.partition(":")
        if sep and window.startswith("@"):
            # Membership is checked against both id shapes list_window_ids can
            # answer with: bare `@N` (this base) and session-qualified (the
            # psmux override qualifies to match its native_id form). Both are
            # rebuilt from the `=`-stripped session the listing was actually
            # asked for, never compared against the raw target: an exact-match
            # `=session:@N` target — the shape _option_scope also normalizes —
            # matches neither listed shape verbatim, and would read a survivor
            # as gone.
            session = session.removeprefix("=")
            try:
                live = self.list_window_ids(session)
            except TmuxError:
                return False
            return f"{session}:{window}" in live or window in live
        # An unqualified or name-token target carries no session to list, so
        # only the same-resolution probe remains: blind to a wrong-target leak,
        # but it still catches a kill that failed while the target resolves.
        # UnicodeError for the same reason list_window_ids names it: a leaf
        # overriding _ERRORS back to a strict codec raises a ValueError-family
        # decode error neither other arm covers, and this helper never raises.
        try:
            probe = self._run(["list-panes", "-t", target], check=False)
        except (subprocess.SubprocessError, OSError, UnicodeError):
            return False
        return probe.returncode == 0

    def window_pane_pids(self, target: str) -> list[int]:
        # Capability method (see the seam default): a transport failure, a dead
        # window, or unparsable output all degrade to the documented "unknown"
        # sentinel [] — this feeds the kill escalation, which must never be the
        # thing that raises.
        try:
            probe = self._run(["list-panes", "-t", target, "-F", "#{pane_pid}"], check=False)
            if probe.returncode != 0:
                return []
            return [int(line) for line in probe.stdout.split()]
        except (subprocess.SubprocessError, OSError, ValueError):
            return []

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        # No missing-binary pre-gate here, deliberately, unlike list_sessions /
        # session_options: those two answer "what exists" and may decide that from
        # the ambient PATH, but this one is reached with a session already in hand
        # and its answer must come from _run — the one spawn primitive the seam
        # documents. A `shutil.which` short-circuit ahead of it returns the
        # sentinel without consulting the transport at all, so an injected _run is
        # never asked, and the behavior silently diverges by whether the binary
        # happens to be on THIS box's PATH. The no-multiplexer silence this was
        # reaching for is gated inside _warn_unproven_listing instead, where it
        # costs a failed exec and nothing else.
        fmt = "\t".join(f"#{{{field}}}" for field in fields)
        try:
            probe = self._run(["list-windows", "-t", f"={session}", "-F", fmt], check=False)
        except (subprocess.SubprocessError, OSError, UnicodeError) as exc:
            # UnicodeError as in session_options: a strict-codec leaf must get the
            # documented sentinel, not a raw decode error out of a best-effort op.
            self._warn_unproven_listing("list-windows", exc)
            return []
        if probe.returncode != 0:
            self._warn_unproven_listing("list-windows", probe)
            return []
        rows: list[tuple[str, ...]] = []
        for line in probe.stdout.splitlines():
            # Bounded split, so the LAST field may itself contain tabs. An
            # unbounded split turns a row whose last field carries arbitrary
            # text into extra parts that the slice below then truncates,
            # silently corrupting the value. Callers requesting a free-text
            # field must therefore ask for it last; every current caller does.
            # This is the seam's standing contract, not a fix for one caller:
            # no field requested today can hold a tab — PROJECT_OPTION is a
            # digest and window names carry a shape-validated run id — so the
            # bound is here for the next free-text field rather than these.
            # (A newline in a value still splits the row, which no parse here
            # can undo; runs.project_tag's digest is what keeps the tag clear
            # of one.)
            parts = line.split("\t", len(fields) - 1)
            parts += [""] * (len(fields) - len(parts))  # tolerate unset trailing fields
            rows.append(tuple(parts[: len(fields)]))
        return rows

    def window_alive(self, session: str, window_id: str) -> bool:
        return window_id in self.list_window_ids(session)

    def select_window(self, target: str) -> None:
        # Best-effort focus change: swallow a transport failure to the no-op sentinel.
        try:
            self._run(["select-window", "-t", target], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def set_window_option(self, target: str, option: str, value: str) -> None:
        try:
            self._run(["set-option", "-w", "-t", target, option, value], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def unset_window_option(self, target: str, option: str) -> None:
        try:
            self._run(["set-option", "-wu", "-t", target, option], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def show_window_option(self, target: str, option: str) -> str:
        # "" reads as "option unset" — fine as the failure sentinel for a hang too.
        try:
            proc = self._run(["show-options", "-wqv", "-t", target, option], check=False)
        except (subprocess.SubprocessError, OSError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # ----------------------------------------------------- client / attach

    def attach_target_argv(self, target: str) -> list[str]:
        # Inside tmux, nesting an attach is refused, so switch this client
        # instead (a `switch-client -l` brings it back).
        if os.environ.get("TMUX"):
            return [self._BINARY, "switch-client", "-t", target]
        return [self._BINARY, "attach", "-t", target]

    def current_pane_id(self) -> str | None:
        return self._display_message("#{pane_id}")

    def current_window_id(self) -> str | None:
        return self._display_message("#{window_id}")

    def current_session(self) -> str | None:
        return self._display_message("#{session_name}")

    def _display_message(self, fmt: str) -> str | None:
        """Resolve a tmux format string against this process's client, or None
        when not inside tmux / tmux is unavailable. The TMUX guard is what makes
        "not inside" honest: against a live server, display-message would answer
        for some OTHER client's session and misreport a plain shell as being
        inside tmux — callers (in_ctl_session, the attach return-pane recording)
        branch on exactly that distinction."""
        if not os.environ.get("TMUX"):
            return None
        try:
            proc = self._run(["display-message", "-p", fmt], check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def detach_client(self) -> bool:
        # Returns True iff a client was detached; a transport failure didn't
        # detach anything, so False is the honest answer.
        try:
            proc = self._run(["detach-client"], check=False)
        except (subprocess.SubprocessError, OSError):
            return False
        return proc.returncode == 0

    def _attached_here(self) -> int | None:
        """Clients attached to this process's session, or None when tmux cannot
        say. Only :meth:`switch_client`'s failure path needs it, so it is read
        after the verb — nothing moved on that path, and the success path pays
        no probe at all."""
        text = self._display_message("#{session_attached}")
        return int(text) if text is not None and text.isdigit() else None

    def switch_client(self, target: str, last_fallback: bool = False) -> bool | None:
        # rc 0 is a real move and needs no gate: tmux runs one server, and it
        # refuses rather than no-ops when there is nobody to move.
        #
        # A nonzero rc is where the exit code stops being the whole answer. It
        # is TWO facts wearing one code — a target this client cannot reach, and
        # no client here at all — and only the first is the seam's False.
        # Measured on tmux 3.7c from inside a pane whose server had no attached
        # client: `-t <live session>`, `-t <other session>`, `-l` and `-t
        # <nonexistent>` ALL exit 1 with "no current client". So a bare rc would
        # answer False — "no switch, and the client is still here" — for the one
        # state where there is no client here at all, which is #659's hazard on
        # the default backend. The attached count is what separates them, the
        # same gate the psmux leaf applies to its own rc.
        #
        # A TIMEOUT is neither: the server may have completed the switch before
        # the wait expired, so False would report a human still watching a
        # window the client has already left. A spawn-level fault IS the joint
        # claim — proof the verb never ran, so nothing moved.
        try:
            proc = self._run(["switch-client", "-t", target], check=False)
            if proc.returncode == 0:
                return True
            if last_fallback and self._run(["switch-client", "-l"], check=False).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            return None
        except (subprocess.SubprocessError, OSError):
            return False
        attached = self._attached_here()
        if attached is None or attached == 0:
            # Unreadable and zero part company as facts and meet as verdicts:
            # neither can vouch that a human is still in front of this window.
            return None
        return False

    def available(self) -> bool:
        return shutil.which(self._BINARY) is not None

    def version(self) -> str | None:
        # Every exit path rewrites the diagnostic, so it always describes THIS
        # call (the seam's read-it-after-version rule) — a probe that recovers
        # must not leave the old failure standing for `mux` to warn about.
        self._version_error = None
        if not shutil.which(self._BINARY):
            return None
        try:
            raw = self._tmux("-V")
        # UnicodeError is in the list for a leaf that overrides _ERRORS back to a
        # strict handler. _run still decodes with the LOCALE codec on POSIX
        # (_ENCODING is None there), but no longer strictly on any platform
        # (_ERRORS is backslashreplace, #380), so an undecodable byte — a corrupt
        # install, or a binary emitting text in another encoding, exactly what
        # this diagnostic exists for — now degrades to a \xNN escape rather than
        # raising. Where a leaf does restore strictness it is a ValueError,
        # outside the SubprocessError/OSError family, so it would escape as a raw
        # crash for every caller above to guard; this arm keeps that closed.
        except (MultiplexerError, subprocess.SubprocessError, OSError, UnicodeError) as exc:
            # None stays the seam's answer, but the identity of the failure is
            # what separates a crashing binary from one that reports no version
            # (#428). On the nonzero-exit arm _run has already folded the probe's
            # stderr into the TmuxError text, so str(exc) carries it; the other
            # arms carry only the failure itself, which is all there is to carry.
            self._version_error = str(exc)
            return None
        # The seam promises one line (TerminalMultiplexer.version). `-V` is one
        # line on tmux, two on psmux (a `tmux X.Y.Z` compat line then its own),
        # so fold rather than truncate — the tail is what names psmux as the
        # answering binary. Order is load-bearing: PsmuxMultiplexer.available()
        # parses the compat segment with an anchored match, so the first
        # segment must stay first.
        return fold_version(raw)

    def version_error(self) -> str | None:
        return self._version_error
