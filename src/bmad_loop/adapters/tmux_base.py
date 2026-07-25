"""Shared tmux-family backend base for the terminal-multiplexer seam.

This module is the **quarantine** for tmux/POSIX-shell knowledge: every tmux
invocation and POSIX-shell trailer lives here (and in its POSIX leaf
:mod:`.tmux_backend`). The point of the split is that a tmux-*family* backend —
the native-Windows :mod:`.psmux_backend` leaf — can subclass :class:`BaseTmuxBackend` and
swap only class attributes (:attr:`BaseTmuxBackend._BINARY` for the spawned
binary, :attr:`BaseTmuxBackend._ENCODING` / :attr:`BaseTmuxBackend._ERRORS`
for output decoding — a scrubbed
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

from .multiplexer import PARKED_EXIT_BANNER, MultiplexerError, TerminalMultiplexer

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
    #: Decode error handling to pair with :attr:`_ENCODING`. ``None`` (POSIX) =
    #: the default strict handler; a Windows leaf sets ``"backslashreplace"`` so
    #: a stray non-UTF-8 byte degrades visibly instead of raising mid-capture.
    _ERRORS: str | None = None

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

    def set_session_environment(self, name: str, var: str, value: str) -> None:
        # Best-effort (see the seam contract): a failure degrades the viewer
        # to unauthenticated, it must never break session creation.
        try:
            self._tmux("set-environment", "-t", f"={name}", var, value)
        except TmuxError:
            pass

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
        except (subprocess.SubprocessError, OSError):
            return {}
        if proc.returncode != 0:  # no server / no sessions
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
    # `read -r` NEEDS the variable name: bash defaults to REPLY without one,
    # but POSIX requires it and dash (Debian/Ubuntu /bin/sh) errors out —
    # which un-parks the window the instant the command exits.
    _PARK = "read -r _park"

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
            f'{self._ECHO} "{PARKED_EXIT_BANNER} $ec — press enter]"; '
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
        # A transport failure (timeout / missing binary) must RAISE, not return [].
        # window_alive() is the engine's liveness probe; a sentinel [] would falsely
        # read as "window dead -> session crashed" on a mere tmux hang. The honest
        # answer to "is it alive?" is "unknowable" -> MultiplexerError. A real dead
        # window still returns [] via the returncode != 0 path below (no exception).
        try:
            probe = self._run(
                ["list-windows", "-t", f"={session}", "-F", "#{window_id}"], check=False
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"{self._BINARY} list-windows failed: {exc}") from exc
        if probe.returncode != 0:
            return []
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
        # already being gone, so swallow to the documented no-op sentinel.
        try:
            self._run(["kill-window", "-t", target], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

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

    def window_exists(self, session: str, window_name: str) -> bool:
        """Verify a window with the given name is live in ``session``."""
        try:
            rows = self.list_windows(session, ["window_name"])
        except MultiplexerError:
            return False
        return any(row and row[0] == window_name for row in rows)

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        fmt = "\t".join(f"#{{{field}}}" for field in fields)
        try:
            probe = self._run(["list-windows", "-t", f"={session}", "-F", fmt], check=False)
        except (subprocess.SubprocessError, OSError):
            return []
        if probe.returncode != 0:
            return []
        rows: list[tuple[str, ...]] = []
        for line in probe.stdout.splitlines():
            parts = line.split("\t")
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

    def resize_window(self, target: str, cols: int, lines: int) -> None:
        # Best-effort (see the seam contract): a failure leaves the window at
        # its creation geometry, which still renders — just letterboxed.
        try:
            self._run(
                ["resize-window", "-t", target, "-x", str(cols), "-y", str(lines)],
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    def set_viewer_option(self, window_id: str, value: str) -> None:
        try:
            self.set_window_option(window_id, value, "1")
        except TmuxError:
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

    def capture_pane(self, target: str, start_line: int = -100, end_line: int | None = None) -> str:
        """Return the pane contents as a text string via ``tmux capture-pane``.

        Captures with -e (encoding/ANSI), -J (jump wrapping), and -S/-E
        for the requested line range. Empty string when the pane is gone
        or the binary is missing."""
        try:
            args = [
                "capture-pane",
                "-p",  # print to stdout
                "-e",  # encoding (preserve ANSI)
                "-J",  # jump wrapping (don't truncate long lines)
                "-S",
                str(start_line),
                "-t",
                target,
            ]
            if end_line is not None:
                args += ["-E", str(end_line)]
            proc = self._run(args, check=False)
            if proc.returncode != 0:
                return ""
            return proc.stdout
        except (subprocess.SubprocessError, OSError):
            return ""

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

    def detach_client(self) -> None:
        try:
            self._run(["detach-client"], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        # Returns True iff a switch happened; a transport failure didn't switch, so
        # the documented False sentinel is the honest answer.
        try:
            proc = self._run(["switch-client", "-t", target], check=False)
            if proc.returncode == 0:
                return True
            if last_fallback:
                fb = self._run(["switch-client", "-l"], check=False)
                return fb.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False
        return False

    def available(self) -> bool:
        return shutil.which(self._BINARY) is not None

    def version(self) -> str | None:
        if not shutil.which(self._BINARY):
            return None
        try:
            return self._tmux("-V")
        except (MultiplexerError, subprocess.SubprocessError, OSError):
            return None
