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
  prerequisite (a folder-less one included). ``waiting_on`` names, per waiting
  child, each prerequisite it still waits on and that prerequisite's status
  (:func:`prerequisite_status`).

:func:`drained` says whether a child is open with every ticket closed — the child
whose initiative a chain marks ``done`` before it picks again.

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

from .statemachine import TICKET_CLOSED_STATES
from .ticketmodel import ChildInitiative, ParentView

NO_FOLDER: Final = "no-folder"
"""The pass-over reason for a ready child whose folder does not exist yet."""

DRAINED: Final = "drained"
"""The pass-over reason for a ready child with nothing left to dispatch."""

OPEN: Final = "open"
"""A prerequisite's status when its folder exists and its file sets none."""

UNLISTED: Final = "?"
"""A prerequisite's status when the parent lists no such child."""

_CLOSED: Final[frozenset[str]] = frozenset(("done", "dropped"))

ChainEnd = Literal["waiting", "passed-over", "all-done"]


@dataclass(frozen=True)
class PassedOver:
    """A ready child :func:`next_child` did not take, and why."""

    slug: str
    why: str


@dataclass(frozen=True)
class WaitingOn:
    """One prerequisite a waiting child still waits on: the child, the
    prerequisite, and the prerequisite's status (:func:`prerequisite_status`)."""

    slug: str
    prerequisite: str
    status: str


@dataclass(frozen=True)
class ChildPick:
    """:func:`next_child`'s answer. ``child`` is the child to build, or None and
    ``end`` says why the chain ends. ``passed_over`` holds the ready children
    walked past before the pick (in build order); ``waiting`` the slugs of the
    children still waiting on a prerequisite, whether or not one was taken, and
    ``waiting_on`` one row per prerequisite each of them waits on, in the same
    order."""

    child: ChildInitiative | None
    passed_over: tuple[PassedOver, ...]
    waiting: tuple[str, ...]
    end: ChainEnd | None
    waiting_on: tuple[WaitingOn, ...] = ()


def drained(child: ChildInitiative) -> bool:
    """Whether ``child`` is open with every ticket closed: it has a folder, is not
    done or dropped, and counts at least one ticket, all done or dropped — the
    child ``statemachine.check_initiative_close`` writes ``done``."""
    if child.folder is None or child.status in _CLOSED:
        return False
    total = child.counts.get("total", 0)
    closed = sum(child.counts.get(state, 0) for state in TICKET_CLOSED_STATES)
    return total > 0 and closed == total


def prerequisite_status(view: ParentView, slug: str) -> str:
    """The status of the child ``slug`` of ``view`` as a prerequisite: its file's
    status (``in-progress``, ``dropped``, ``done``), :data:`OPEN` when its folder
    exists and the file sets none, :data:`NO_FOLDER` when it is declared but not
    created yet, or :data:`UNLISTED` when the parent lists no such child."""
    child = view.child(slug)
    if child is None:
        return UNLISTED
    if child.folder is None:
        return NO_FOLDER
    return child.status or OPEN


def next_child(
    view: ParentView,
    passes_over: Callable[[ChildInitiative], str | None] | None = None,
) -> ChildPick:
    """The first ready child of ``view`` that has a folder and that
    ``passes_over`` does not pass over, in build order (see the module
    docstring)."""
    blocked = [c for c in view.initiatives if not c.ready and c.status not in _CLOSED]
    waiting = tuple(c.slug for c in blocked)
    waiting_on = tuple(
        WaitingOn(c.slug, p, prerequisite_status(view, p)) for c in blocked for p in c.waiting_on
    )
    passed: list[PassedOver] = []
    for child in view.initiatives:
        if not child.ready:
            continue
        why = NO_FOLDER if child.folder is None else None
        if why is None and passes_over is not None:
            why = passes_over(child)
        if why is None:
            return ChildPick(child, tuple(passed), waiting, None, waiting_on)
        passed.append(PassedOver(child.slug, why))
    end: ChainEnd = "waiting" if waiting else "passed-over" if passed else "all-done"
    return ChildPick(None, tuple(passed), waiting, end, waiting_on)
