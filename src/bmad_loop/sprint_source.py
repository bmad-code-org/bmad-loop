"""The sprint-board work source: :class:`SprintWorkSource` reads
``sprint-status.yaml`` for the base :class:`~bmad_loop.engine.Engine`.

This module holds the board-reading halves of the engine's pick, remaining
estimate and retrospective gate, lifted unchanged from the engine. It answers
questions and does nothing else. Every journal line, notice and pause stays in the
Engine function that always wrote it: the ``sprint-status-unknown-keys`` journal
stays in ``Engine._pick_next``, which reads ``Pick.unknown_keys``.

The board is read through a ``paths_get`` getter, never a snapshot, so every read
sees the engine's live ``paths`` (the main checkout). Construction does no I/O,
because ``SweepEngine`` inherits ``Engine.__init__`` and never reads the board.
It does parse the ``--epic``/``--story`` selector eagerly, so a bad ``--story``
still raises ``SprintStatusError`` during ``Engine.__init__``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from .bmadconfig import ProjectPaths
from .model import Phase
from .sprintstatus import ACTIONABLE_STATUSES, SprintStatus, SprintStatusError
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable, parse_selector
from .worksource import SOURCE_SPRINT, EpicProgress, Pick, SourceUnreadable

if TYPE_CHECKING:
    from .worksource import WorkSource


class SprintWorkSource:
    """The ``sprint-status`` source: units are board stories, grouped by epic."""

    name = SOURCE_SPRINT
    has_epics = True

    def __init__(
        self,
        paths_get: Callable[[], ProjectPaths],
        epic_filter: int | None,
        story_filter: str | None,
    ):
        self._paths_get = paths_get
        # widen --story interpretation: full key, short ref (3-1/3.1), bare
        # number (+ --epic), or slug fragment. See sprintstatus.StorySelector.
        self._selector = parse_selector(epic_filter, story_filter)

    def _board(self) -> SprintStatus:
        """The board, or :class:`SourceUnreadable` carrying the read error's text."""
        try:
            return load_sprint_status(self._paths_get().sprint_status)
        except (SprintStatusError, OSError, UnicodeDecodeError) as e:
            raise SourceUnreadable(str(e)) from e

    def pick(self, phases: Mapping[str, Phase], current_epic: int | None) -> Pick:
        """The next actionable story the selector admits, preferring
        ``current_epic``. A board read error propagates unwrapped, as it always has
        out of ``Engine._pick_next``."""
        ss = load_sprint_status(self._paths_get().sprint_status)
        base_skip = set(phases)  # anything this run already touched

        def _first(epic: int | None):
            # local skip copy so selector-rejections in this pass don't leak into
            # the next one (a story rejected here may still match the fallback).
            skip = set(base_skip)
            while True:
                story = next_actionable(ss, skip, epic=epic)
                if story is None:
                    return None
                if not self._selector.matches(story):
                    skip.add(story.key)
                    continue
                return story

        # Exhaust the current epic before advancing. Selection is otherwise
        # strict file order, and epics need not be file-ordered by number (an
        # epic can be appended out of place); without this, a still-open earlier-
        # in-file epic would "steal" the pick and fire a spurious epic boundary.
        story = None
        if current_epic is not None:
            story = _first(current_epic)
        if story is None:
            story = _first(None)
        return Pick(ref=story, unknown_keys=tuple(ss.unknown_keys))

    def remaining_estimate(self, phases: Mapping[str, Phase]) -> int:
        """Actionable board stories this run has not already picked up (mirrors
        ``cmd_status``'s sprint backlog count, minus anything in ``phases``). An
        unreadable board raises; the engine reads that as no estimate."""
        ss = load_sprint_status(self._paths_get().sprint_status)
        return sum(1 for s in ss.stories if s.status in ACTIONABLE_STATUSES and s.key not in phases)

    def epic_progress(self, epic: int) -> EpicProgress:
        """``epic`` is complete when the board has at least one story of it and
        every one is ``done``; its retro is done when ``epic-N-retrospective``
        reads ``done``."""
        board = self._board()
        stories = [s for s in board.stories if s.epic == epic]
        return EpicProgress(
            complete=bool(stories) and all(s.status == "done" for s in stories),
            retro_done=board.retros.get(epic) == "done",
        )

    def retro_done(self, epic: int) -> bool:
        return self._board().retros.get(epic) == "done"

    def retro_artifact_errors(self, epic: int) -> tuple[list[str], list[Path]]:
        """The board and doc checks of a finished retro session: the board reads
        ``epic-N-retrospective: done`` and at least one retro doc is on disk."""
        errors: list[str] = []
        try:
            board = load_sprint_status(self._paths_get().sprint_status).retros.get(epic)
        except (SprintStatusError, OSError, UnicodeDecodeError):
            errors.append("sprint-status unreadable")
        else:
            if board != "done":
                errors.append(f"sprint-status epic-{epic}-retrospective is {board or 'absent'}")
        try:
            docs = self.retro_docs(epic)
        except OSError:
            docs = []
            errors.append("implementation-artifacts unreadable")
        else:
            if not docs:
                errors.append(f"no epic-{epic}-retro-*.md retrospective doc")
        return errors, docs

    def retro_docs(self, epic: int) -> list[Path]:
        """The regular files matching ``epic-{epic}-retro-*.md`` in the
        implementation-artifacts dir, sorted. ``lstat``-checked, so a symlink or a
        directory of that name is not a retro doc. Raises ``OSError`` when the
        directory cannot be listed; a single entry that vanishes or cannot be
        stat'd is simply not counted."""
        docs: list[Path] = []
        for candidate in sorted(
            self._paths_get().implementation_artifacts.glob(f"epic-{epic}-retro-*.md")
        ):
            try:
                if S_ISREG(candidate.lstat().st_mode):
                    docs.append(candidate)
            except OSError:
                continue
        return docs

    def retro_commit_paths(self, docs: Sequence[Path]) -> set[Path]:
        """The board plus the retro docs: the skill writes both."""
        return {self._paths_get().sprint_status, *docs}

    def retro_target(self, epic: int) -> str:
        return str(epic)

    def epic_label(self, epic: int) -> str:
        return f"epic-{epic}"


if TYPE_CHECKING:
    _conforms: type[WorkSource] = SprintWorkSource
