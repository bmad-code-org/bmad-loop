"""POSIX tmux backend for the terminal-multiplexer seam.

The tmux/POSIX-shell quarantine spans this file and its base
(:mod:`.tmux_base`) — together they are the **only** place in the codebase
allowed to shell out to ``tmux``, so a future non-POSIX backend (an eventual
native-Windows "psmux") can replace them wholesale. All argv construction and
the single spawn primitive live in :class:`~.tmux_base.BaseTmuxBackend`; this
leaf is the POSIX implementation and inherits the full contract unchanged. See
:mod:`.multiplexer` for the contract.

``subprocess`` and ``shutil`` are imported (and re-exported) here so existing
callers and tests can still reach the spawn seam via ``tmux_backend.subprocess``
/ ``tmux_backend.shutil``; the live calls run through ``tmux_base``.
"""

from __future__ import annotations

import shutil  # noqa: F401 — re-exported for callers/tests reaching the spawn seam
import subprocess  # noqa: F401 — re-exported for callers/tests reaching the spawn seam

from .tmux_base import PARKED_RETURN_DETACH  # noqa: F401 — re-exported for back-compat
from .tmux_base import TMUX_TIMEOUT_S  # noqa: F401 — re-exported for back-compat
from .tmux_base import TmuxError  # noqa: F401 — re-exported for back-compat
from .tmux_base import (
    BaseTmuxBackend,
)


class TmuxMultiplexer(BaseTmuxBackend):
    """POSIX tmux backend — inherits the full contract from BaseTmuxBackend.

    Registered by :func:`~.multiplexer._load_builtin_backends` (the bundled loader),
    not at import time, so the registry can be cleared and re-loaded deterministically
    in tests — mirroring how ``process_host._load_builtin_hosts`` registers its hosts.
    """
