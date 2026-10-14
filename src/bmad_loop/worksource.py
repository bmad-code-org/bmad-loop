"""Static per-source registry: the work sources a run can dispatch from, and what
each one supports.

A *work source* is where a run's queue comes from — ``"sprint-status"`` (the
sprint board written by bmad-sprint-planning) or ``"stories"`` (a stories.yaml
manifest beside SPEC.md). The CLI, runsetup and the read models have no Engine
instance to ask, so the per-source differences they branch on live here as plain
data: :class:`SourceTraits`, one entry per source in :data:`TRAITS`. A site that
used to test ``source == "stories"`` reads the trait that names *why* it
branches, and a site that dispatches whole functions per source keys a table on
:data:`SOURCES` (``tests/test_worksource.py`` holds every such table exhaustive).

``SOURCES`` must equal ``policy.STORIES_SOURCES`` (a sync test enforces it).
policy.py deliberately does not import this module: the settings sync test
resolves ``options_ref = "STORIES_SOURCES"`` inside policy.py, and the pure core
stays free of a dependency on this registry.

The second half is the *runtime* seam: the :class:`WorkSource` protocol an
Engine composes (``Engine.source``) to read its queue — the next unit to pick,
the remaining estimate, epic identity and progress, and the retrospective
artifacts. A source is a read model: it never journals, notifies or pauses. It
returns data (a :class:`Pick`, possibly carrying a :class:`Halt`), and the engine
keeps every ``journal.append``, ``gates.notify`` and ``RunPaused`` in its own
``(file, function)``, where ``tests/test_portability_guard.py`` keys them.

Pure data and lookups — no I/O, no journal writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias

from .model import Phase

SOURCE_SPRINT: Final = "sprint-status"
SOURCE_STORIES: Final = "stories"
SOURCES: Final[tuple[str, ...]] = (SOURCE_SPRINT, SOURCE_STORIES)

QueueMode = Literal["sprint", "stories"]
"""The ``mode`` values ``validate --json`` reports — one per source, through
:attr:`SourceTraits.validate_mode`."""


class UnknownSource(ValueError):
    """A source name no registry entry carries."""


@dataclass(frozen=True)
class SourceTraits:
    """What one work source supports.

    - ``label``: the start-run picker's display text.
    - ``validate_mode``: the ``mode`` value ``validate --json`` reports.
    - ``has_epics``: the queue is grouped into numbered epics, so a run crosses
      epic boundaries (retrospective, ``--epic``, the auto-retro ``retro`` role).
    - ``has_spec_folder``: the run pins a spec folder (``RunState.spec_folder``).
    - ``stories_primitive``: the dev skill must support folder+id dispatch
      (``install.missing_stories_support``).
    - ``sentinels``: a story's spec can carry a sentinel status the run pauses on.
    - ``operator_park``: review demotion can park a story at awaiting-operator,
      verified against the sprint board.
    - ``replay``: a standalone unit replay can read this source's queue.
    - ``runnable``: ``run`` can launch from it today.
    """

    name: str
    label: str
    validate_mode: QueueMode
    has_epics: bool
    has_spec_folder: bool
    stories_primitive: bool
    sentinels: bool
    operator_park: bool
    replay: bool
    runnable: bool


TRAITS: Final[Mapping[str, SourceTraits]] = {
    SOURCE_SPRINT: SourceTraits(
        name=SOURCE_SPRINT,
        label="sprint mode — sprint-status.yaml",
        validate_mode="sprint",
        has_epics=True,
        has_spec_folder=False,
        stories_primitive=False,
        sentinels=False,
        operator_park=True,
        replay=True,
        runnable=True,
    ),
    SOURCE_STORIES: SourceTraits(
        name=SOURCE_STORIES,
        label="stories mode — folder+id dispatch",
        validate_mode="stories",
        has_epics=False,
        has_spec_folder=True,
        stories_primitive=True,
        sentinels=True,
        operator_park=False,
        replay=False,
        runnable=True,
    ),
}


def traits_for(source: str) -> SourceTraits:
    """The traits of ``source``; raises :class:`UnknownSource` for a name the
    registry does not carry (policy load already refuses one at launch)."""
    try:
        return TRAITS[source]
    except KeyError:
        raise UnknownSource(f"unknown work source {source!r}") from None


def resumed_traits_for(source: str) -> SourceTraits:
    """The traits of a *persisted* run's ``source``, falling back to the sprint
    source for a name the registry does not carry.

    This is the legacy resume behavior, kept on purpose: a run state written with
    an unrecognised ``source`` has always been resumed, reported and replayed as a
    sprint run. Only sites reading ``RunState.source`` use it; a launch-time
    source goes through :func:`traits_for`."""
    return TRAITS.get(source, TRAITS[SOURCE_SPRINT])


def launch_roles(source: str, retrospective: str) -> tuple[str, ...]:
    """The session roles a story run from ``source`` launches: dev + review, plus
    the auto retrospective's ``retro`` when the source has epics and
    ``gates.retrospective`` is ``"auto"``. A source without epics never crosses an
    epic boundary, so it never launches a retro."""
    if traits_for(source).has_epics and retrospective == "auto":
        return ("dev", "review", "retro")
    return ("dev", "review")


def validate_mode(source: str) -> QueueMode:
    """The ``mode`` value ``validate --json`` reports for ``source``."""
    return traits_for(source).validate_mode


# ------------------------------------------------------------ runtime protocol

EpicId: TypeAlias = int
"""A unit's epic. An int for every source today; a source without epics uses 0."""


class WorkRef(Protocol):
    """A unit a source hands the engine to dispatch: its run-unique, resume-stable
    key and its epic. ``sprintstatus.Story`` and :class:`UnitRef` both match it
    structurally."""

    @property
    def key(self) -> str: ...

    @property
    def epic(self) -> EpicId: ...


@dataclass(frozen=True)
class UnitRef:
    """A plain :class:`WorkRef`, for a source whose queue has no richer row type."""

    key: str
    epic: EpicId = 0


@dataclass(frozen=True)
class Halt:
    """A pick that must stop the run instead of dispatching: the unit ``key`` it
    stopped on, a source-defined ``cause``, and a ``payload`` the engine's
    ``_halt_pick`` turns into the pause."""

    key: str
    cause: str
    payload: object = None


@dataclass(frozen=True)
class Pick:
    """One pick's answer. ``ref`` is the unit to dispatch, ``halt`` a reason to
    stop; both ``None`` means the queue is exhausted. ``unknown_keys`` are queue
    keys the source could not classify and ``loaded_count`` how many units it
    read — reported for the engine to journal, since a source never journals."""

    ref: WorkRef | None = None
    halt: Halt | None = None
    unknown_keys: tuple[str, ...] = ()
    loaded_count: int | None = None


@dataclass(frozen=True)
class EpicProgress:
    """Whether every unit of an epic is done (at least one unit, all done), and
    whether its retrospective is already done."""

    complete: bool
    retro_done: bool


class SourceUnreadable(Exception):
    """The source's queue could not be read. ``str()`` is the underlying error's
    text, unchanged, so a journal line built from it reads as it always has."""


class WorkSource(Protocol):
    """The runtime read model an Engine dispatches from (``Engine.source``).

    Constructing one does no I/O: ``SweepEngine`` inherits ``Engine.__init__``
    and never reads the queue. Every method re-reads the source on call."""

    @property
    def name(self) -> str:
        """The registry name (a :data:`SOURCES` member)."""
        ...

    @property
    def has_epics(self) -> bool:
        """The queue is grouped into epics, so a run crosses epic boundaries and
        fires the retrospective gate."""
        ...

    def pick(self, phases: Mapping[str, Phase], current_epic: EpicId | None) -> Pick:
        """The next unit to dispatch. ``phases`` maps every unit this run already
        touched to its phase; ``current_epic`` is the epic of the last one."""
        ...

    def remaining_estimate(self, phases: Mapping[str, Phase]) -> int:
        """How many units a resume would still have to run. A hint: it may raise,
        and the engine reads any exception as "no estimate"."""
        ...

    def epic_progress(self, epic: EpicId) -> EpicProgress:
        """``epic``'s completion and retro state; raises :class:`SourceUnreadable`."""
        ...

    def retro_done(self, epic: EpicId) -> bool:
        """Whether ``epic``'s retrospective is done; raises :class:`SourceUnreadable`."""
        ...

    def retro_artifact_errors(self, epic: EpicId) -> tuple[list[str], list[Path]]:
        """Every failed artifact check of a finished retro session as one error
        line, plus the retro docs found (:meth:`retro_docs`)."""
        ...

    def retro_docs(self, epic: EpicId) -> list[Path]:
        """``epic``'s retrospective docs on disk, sorted; raises ``OSError`` when
        their directory cannot be listed."""
        ...

    def retro_commit_paths(self, docs: Sequence[Path]) -> set[Path]:
        """The paths a succeeded retro session owns and the engine commits."""
        ...

    def retro_target(self, epic: EpicId) -> str:
        """The ``/bmad-retrospective -H <target>`` argument for ``epic``."""
        ...

    def epic_label(self, epic: EpicId) -> str:
        """``epic``'s label as a sweep trigger."""
        ...
