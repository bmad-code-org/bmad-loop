"""Which child of a BMAD v7 parent initiative a tickets chain builds next.

A parent initiative's ``tickets.toml`` lists ``[[initiative]]`` children, each a
folder beside the parent's; ``tickets.py next`` on the parent
(:func:`~bmad_loop.ticketmodel.parse_parent_next`) reports them in build order
with the ones it lists as ``ready`` — nothing left to wait on, and not done or
dropped. :func:`next_child` walks the ready children in that order and takes the
first one it can build. Pure: the view and the caller's probe are the only
inputs, and every child it does not take is reported, so the caller can journal
why.

* A ready child whose folder does not exist yet (declared, not sliced) is passed
  over as ``no-folder``; upstream counts it as ready, but there is no tree to
  build from.
* ``passes_over``, when given, is asked about each ready child that has a folder
  and returns a reason to pass it over (``"drained"`` for a child with nothing
  left to dispatch), or None to take it.
* Children that are not ready and not done or dropped are ``waiting`` on a
  prerequisite (a folder-less one included).

When no child is taken the pick says why the chain ends: ``waiting`` when some
child still waits on a prerequisite (a drained child whose initiative is not
marked done keeps its dependents here), ``passed-over`` when every child left
unbuilt was passed over, and ``all-done`` when every child is done or dropped
(or the parent lists none).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from .ticketmodel import ChildInitiative, ParentView

NO_FOLDER: Final = "no-folder"
"""The pass-over reason for a ready child whose folder does not exist yet."""

_CLOSED: Final[frozenset[str]] = frozenset(("done", "dropped"))

ChainEnd = Literal["waiting", "passed-over", "all-done"]


@dataclass(frozen=True)
class PassedOver:
    """A ready child :func:`next_child` did not take, and why."""

    slug: str
    why: str


@dataclass(frozen=True)
class ChildPick:
    """:func:`next_child`'s answer. ``child`` is the child to build, or None and
    ``end`` says why the chain ends. ``passed_over`` holds the ready children
    walked past before the pick (in build order); ``waiting`` the slugs of the
    children still waiting on a prerequisite, whether or not one was taken."""

    child: ChildInitiative | None
    passed_over: tuple[PassedOver, ...]
    waiting: tuple[str, ...]
    end: ChainEnd | None


def next_child(
    view: ParentView,
    passes_over: Callable[[ChildInitiative], str | None] | None = None,
) -> ChildPick:
    """The first ready child of ``view`` that has a folder and that
    ``passes_over`` does not pass over, in build order (see the module
    docstring)."""
    waiting = tuple(c.slug for c in view.initiatives if not c.ready and c.status not in _CLOSED)
    passed: list[PassedOver] = []
    for child in view.initiatives:
        if not child.ready:
            continue
        why = NO_FOLDER if child.folder is None else None
        if why is None and passes_over is not None:
            why = passes_over(child)
        if why is None:
            return ChildPick(child, tuple(passed), waiting, None)
        passed.append(PassedOver(child.slug, why))
    end: ChainEnd = "waiting" if waiting else "passed-over" if passed else "all-done"
    return ChildPick(None, tuple(passed), waiting, end)
