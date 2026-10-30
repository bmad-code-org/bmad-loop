"""Where a BMAD v7 ticket tree keeps each container's breakdown.

Upstream BMAD-METHOD (``tools/ticket-tree-rules.md``, ``tickets.py``
``tickets_dir``) keeps each container — an initiative or an epic — in two parts.
Its own ``<name>.md``, its ``.memlog.md`` and an epic's retrospective sit at the
top of its folder; its breakdown (``tickets.toml``, the child ``epic-*`` folders,
the leaf files and plans) sits in a ``tickets/`` folder inside it. Earlier
"7 preview" installs keep the breakdown flat, beside the container's own file,
and ``backlog/`` is always flat.

bmad-loop reads both, deciding per container by the rule upstream applies: a
``<container>/tickets/`` directory holds the breakdown, else the container folder
itself does. The installed ``tickets.py`` is the arbiter of which layout is legal (the
current script refuses the flat one and names ``bmad migrate``); bmad-loop never
refuses a layout itself.

Pure path rules over one ``stat``: imports nothing from ``bmad_loop``, so the
tree readers, the store probe, the tickets engine and the TUI share it without an
import cycle. A probe fault other than "absent" raises ``OSError`` for the caller
to wrap in its own typed error — never a silent "flat layout".
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

TICKETS = "tickets"
"""A container's breakdown sub-folder (upstream ``49fd27e4``)."""

BREAKDOWN = "tickets.toml"
"""A container's breakdown file: ``[[epic]]`` rows for an initiative,
``[[entry]]`` rows for an epic."""


def breakdown_dir(container: Path) -> Path:
    """Where ``container``'s ``tickets.toml``, child epic folders, leaf files and
    plans live: ``<container>/tickets/`` when that is a directory, else the
    container itself (the flat layout, and ``backlog/``). A missing container, or
    a ``tickets`` that is not a directory, reads as flat. Any other stat fault
    raises ``OSError`` (``Path.is_dir`` would read it as flat)."""
    try:
        st = os.stat(container / TICKETS)
    except (FileNotFoundError, NotADirectoryError):
        return container
    return container / TICKETS if stat.S_ISDIR(st.st_mode) else container


def epic_folder(initiative: Path, slug: str) -> Path:
    """Epic ``slug``'s folder: ``breakdown_dir(initiative) / slug``. Raises
    ``OSError`` as :func:`breakdown_dir` does."""
    return breakdown_dir(initiative) / slug


def epic_file(initiative: Path, slug: str) -> Path:
    """Epic ``slug``'s own file, ``<slug>.md`` at the top of its epic folder.
    Raises ``OSError`` as :func:`breakdown_dir` does."""
    return epic_folder(initiative, slug) / f"{slug}.md"


def retrospective_doc(initiative: Path, slug: str) -> Path:
    """Epic ``slug``'s retrospective document, ``<slug>-retrospective.md`` at the
    top of its epic folder — beside the epic file, above the epic's own
    ``tickets/`` folder (upstream ``tools/ticket-tree-rules.md``). Raises
    ``OSError`` as :func:`breakdown_dir` does."""
    return epic_folder(initiative, slug) / f"{slug}-retrospective.md"
