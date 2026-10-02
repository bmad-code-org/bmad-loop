"""POSIX tmux backend for the terminal-multiplexer seam.

The tmux/POSIX-shell quarantine spans this file and its base
(:mod:`.tmux_base`) — together they are the **only** place in the codebase
allowed to shell out to ``tmux``, so a future non-POSIX backend (an eventual
native-Windows "psmux") can replace them wholesale. All argv construction and
the single spawn primitive live in :class:`~.tmux_base.BaseTmuxBackend`; this
leaf is the POSIX implementation and inherits the full contract, adding only
the POSIX launch-pid prelude to each coding-CLI window (DW-507) and the
``inherited_env`` query psmux must not inherit (#731). See
:mod:`.multiplexer` for the contract.

``subprocess`` and ``shutil`` are imported (and re-exported) here so existing
callers and tests can still reach the spawn seam via ``tmux_backend.subprocess``
/ ``tmux_backend.shutil``; the live calls run through ``tmux_base``.
"""

from __future__ import annotations

import shutil  # noqa: F401 — re-exported for callers/tests reaching the spawn seam
import subprocess
from collections.abc import Callable

from .multiplexer import UNSET, Unset
from .tmux_base import PARKED_RETURN_DETACH  # noqa: F401 — re-exported for back-compat
from .tmux_base import TMUX_TIMEOUT_S  # noqa: F401 — re-exported for back-compat
from .tmux_base import TmuxError  # noqa: F401 — re-exported for back-compat
from .tmux_base import (
    LAUNCH_PRELUDE,
    BaseTmuxBackend,
)


class TmuxMultiplexer(BaseTmuxBackend):
    """POSIX tmux backend — inherits the full contract from BaseTmuxBackend.

    Registered by :func:`~.multiplexer._load_builtin_backends` (the bundled loader),
    not at import time, so the registry can be cleared and re-loaded deterministically
    in tests — mirroring how ``process_host._load_builtin_hosts`` registers its hosts.
    """

    def _window_launch(self, env: dict[str, str], command: str) -> list[str]:
        """The base's ``-e`` flags, with the command behind the launch-pid prelude.

        The command runs behind a small ``/bin/sh -c`` prelude (DW-507) that exports
        :data:`~.tmux_base.LAUNCH_PID_ENV` as its own ``$$`` and then ``exec``s
        ``"${SHELL:-/bin/sh}" -c <command>``: the relays need the launched
        CLI's pid to tag hook lineage, and that pid is known only in-pane.
        tmux's ``default-shell`` semantics are preserved — tmux sets ``SHELL``
        to its ``default-shell`` in every pane (even over ``-e SHELL=``), so
        the command runs under that shell exactly as before, and whatever it
        sources for a ``-c`` command (fish's ``config.fish``, zsh's
        ``.zshenv``) still applies. The program is the absolute ``/bin/sh``,
        never a PATH lookup, so a profile's ``[env] PATH`` overlay cannot
        re-point it. The prelude's ``exec`` keeps ``$$``
        the pane's process: bash and zsh then exec a single ``-c`` command, so
        the recorded pid IS the CLI's; fish and dash (Debian/Ubuntu ``/bin/sh``,
        0.5.12) fork it, so the pid is the shell's and the relays' launch-chain
        rule skips the CLI under it.

        The prelude lives on this POSIX leaf, not the base: it is POSIX source
        built as a literal argv, and an out-of-tree tmux-family leaf that swaps
        only ``_shell_wrap`` for another dialect must keep inheriting the base's
        plain command (its hook lineage then reads ``unknown``, which
        attribution ignores).
        """
        *env_args, command = super()._window_launch(env, command)
        return [*env_args, "/bin/sh", "-c", LAUNCH_PRELUDE, "sh", command]

    def inherited_env(
        self,
        session: str,
        name: str,
        *,
        on_fault: Callable[[str], None] | None = None,
    ) -> str | Unset | None:
        """The seam query (#731), answered by ``show-environment``: a new pane's
        env is the global env overlaid with the session env, so the session
        scope is asked first and the global scope only on a session miss.

        Replies, measured on tmux 3.4: ``NAME=value`` is the value (``NAME=``
        is a set-empty ``""``), ``-NAME`` is tmux's removal marker (known-unset),
        and rc 1 with exactly ``unknown variable: NAME`` is a miss in that scope. Anything
        else — another error such as ``no such session``, a timeout, a missing
        binary, an unparseable reply — is a fault: ``None``, reported once
        through ``on_fault``.

        Here and not on :class:`~.tmux_base.BaseTmuxBackend`, which would hand
        it to psmux: psmux's ``show-environment`` ignores the variable name and
        hides inherited values, so its replies do not mean what this parse
        reads them as."""
        for scope in (["-t", f"={session}"], ["-g"]):
            argv = ["show-environment", *scope, name]
            try:
                proc = self._run(argv, check=False)
            except (subprocess.SubprocessError, OSError, UnicodeError) as exc:
                return self._env_fault(argv, str(exc), on_fault)
            reply = proc.stdout.removesuffix("\n")
            if proc.returncode == 0:
                if reply == f"-{name}":
                    return UNSET
                if reply.startswith(f"{name}=") and "\n" not in reply:
                    return reply[len(name) + 1 :]
                return self._env_fault(argv, f"unexpected reply {reply!r}", on_fault)
            # A miss is exactly tmux's own line for exactly this name; anything
            # merely containing the words (a socket path, say) is a fault.
            if proc.returncode != 1 or proc.stderr.strip() != f"unknown variable: {name}":
                detail = proc.stderr.strip() or f"exit {proc.returncode}"
                return self._env_fault(argv, detail, on_fault)
        return UNSET

    def _env_fault(
        self, argv: list[str], detail: str, on_fault: Callable[[str], None] | None
    ) -> None:
        if on_fault is not None:
            on_fault(f"{self._BINARY} {' '.join(argv)} failed: {detail}")
