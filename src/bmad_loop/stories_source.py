"""The stories.yaml work source: :class:`StoriesWorkSource` reads a typed
``stories.yaml`` manifest for :class:`~bmad_loop.stories_engine.StoriesEngine`.

This module holds the manifest-reading halves of the stories engine's pick,
done_checkpoint skip-if-last scan and remaining estimate, lifted unchanged from the
engine. It answers questions and does nothing else. Every journal line, notice and
pause stays in the StoriesEngine function that always wrote it: the
``stories-validated`` journal stays in ``StoriesEngine._journal_validated``, which
reads ``Pick.loaded_count``, and a halted pick becomes a pause in
``StoriesEngine._halt_pick``.

The spec folder is read through two getters, never snapshots. ``workspace_get``
yields the engine's live workspace, so a relative folder anchors on the project at
pick time and on the unit's mount project during a driven story (DW-379/484).
``spec_folder_get`` yields the engine's project-relative folder string, which the
engine computes after ``Engine.__init__`` has built this source. Construction does
no I/O.

A flat list has no epics: :attr:`StoriesWorkSource.has_epics` is false, so the
engine never crosses an epic boundary or fires a retrospective here, and the
epic-shaped protocol methods answer "no epic" without reading anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import stories
from .model import Phase
from .worksource import SOURCE_STORIES, EpicProgress, Halt, Pick, UnitRef

if TYPE_CHECKING:
    from .worksource import WorkSource
    from .workspace import Workspace

HALT_UNKNOWN_SELECTOR = "unknown-selector"
"""``Halt.cause``: the ``--story`` id is not in ``stories.yaml``. ``Halt.key`` is the id."""

HALT_WEDGED = "wedged"
"""``Halt.cause``: a blocked / sentinel / ambiguous story stopped the scan.
``Halt.payload`` is the :class:`stories.Schedule` naming it."""


class StoriesModeError(Exception):
    """Raised when stories mode cannot proceed for a structural reason the run
    should surface loudly (unreadable/invalid ``stories.yaml`` mid-run).

    Distinct from the contract-side :class:`stories.StoriesError` (the parser's
    error) so the two seams — parse vs engine drive — never get conflated when a
    parser error leaks through an engine catch."""


class _UnknownSelector(StoriesModeError):
    """The ``--story`` selector id is not (or no longer) in ``stories.yaml``.

    A subclass so :meth:`StoriesWorkSource.pick` can turn it into a halt that the
    engine pauses on for resolve (the id is recoverable — fix the typo or the
    manifest, then resume) rather than let it crash the run like a
    genuinely-unreadable manifest."""

    def __init__(self, selector: str):
        super().__init__(f"--story id {selector!r} is not in stories.yaml")
        self.selector = selector


def _retired(phases: Mapping[str, Phase]) -> set[str]:
    """Stories retired this run: DONE (completed) or DEFERRED (plateaued).

    Crucially NOT ESCALATED: schedule() consults the skip set *before* classifying
    disk state, so skipping a wedge would let a bare `bmad-loop resume` (no
    resolve) leapfrog it and dispatch a later story onto a tree missing the blocked
    story's work — breaking the linear "a blocked story cannot be leapfrogged"
    invariant. Left out of the skip set, an unresolved wedge re-classifies from disk
    (blocked/sentinel) and re-pauses on itself; once `resolve` re-arms it to PENDING
    it re-dispatches. (Only pick-time wedge tasks reach this scan while ESCALATED:
    an *in-run* escalation — whose spec status proves nothing about resolution — is
    re-paused before scheduling by StoriesEngine._repause_inrun_escalation.)"""
    return {key for key, phase in phases.items() if phase in (Phase.DONE, Phase.DEFERRED)}


class StoriesWorkSource:
    """The ``stories`` source: units are manifest entries, keyed by story id."""

    name = SOURCE_STORIES
    has_epics = False

    def __init__(
        self,
        workspace_get: Callable[[], Workspace],
        spec_folder_get: Callable[[], str],
        id_filter: str | None,
    ):
        self._workspace_get = workspace_get
        self._spec_folder_get = spec_folder_get
        self.id_filter = id_filter

    @staticmethod
    def relativize(project: Path, spec_folder: str) -> str:
        """Store the spec folder project-relative when possible (we always
        dispatch project-relative). Shared with the CLI dry-run via
        :func:`stories.relativize_spec_folder` so both render the identical
        folder string."""
        return stories.relativize_spec_folder(project, spec_folder)

    @property
    def spec_folder_rel(self) -> str:
        """The spec folder as the engine dispatches it (project-relative when possible)."""
        return self._spec_folder_get()

    def folder(self) -> Path:
        """The spec folder resolved against the current workspace's PROJECT (the
        project at pick time, the unit's mount project during a driven story).

        The folder is stored project-relative (:meth:`relativize`), so it joins on
        ``workspace.paths.project`` — not ``workspace.root``, the code root, which a
        ``repo_root:`` override moves away from the project (DW-379)."""
        rel = Path(self.spec_folder_rel)
        return rel if rel.is_absolute() else self._workspace_get().paths.project / rel

    def load(self) -> stories.Stories:
        return stories.load_stories(self.folder())

    def load_checked(self) -> stories.Stories:
        """:meth:`load`, with a parse failure raised as :class:`StoriesModeError`."""
        try:
            return self.load()
        except stories.StoriesError as e:
            raise StoriesModeError(f"stories.yaml no longer usable: {e}") from e

    def compute_schedule(
        self, story_set: stories.Stories, phases: Mapping[str, Phase]
    ) -> stories.Schedule:
        """Run the linear scheduler against fresh on-disk state, honoring the
        ``--story`` selector and the within-run skip set (stories already driven
        to a terminal *retirement* this run — mirrors the sprint engine's base_skip,
        see :func:`_retired`). ``story_set`` is a fresh :meth:`load_checked`
        (id-stability rule F: a between-runs re-derive is safe because pinned ids
        are stable and un-started ids reschedule by list order). Shared by
        :meth:`pick` and the done_checkpoint skip-if-last check."""
        # A --story id that vanished from the manifest mid-run (or slipped past
        # preflight) must not crash the run via find_entry: raise a recoverable
        # _UnknownSelector that pick turns into a halt for resolve.
        if self.id_filter is not None and story_set.get(self.id_filter) is None:
            raise _UnknownSelector(self.id_filter)
        folder = self.folder()
        states = {e.id: stories.resolve_story_spec(folder, e.id) for e in story_set.entries}
        return stories.schedule(story_set, states, selector=self.id_filter, skip=_retired(phases))

    def pick(self, phases: Mapping[str, Phase], current_epic: int | None) -> Pick:
        """The next story the linear schedule dispatches. A bad/removed ``--story``
        id halts with :data:`HALT_UNKNOWN_SELECTOR`, keyed on the selector id so the
        escalation viewer + rearm act on the same key; a wedge halts with
        :data:`HALT_WEDGED`. ``current_epic`` is unused: a flat list has no epics.
        An unusable manifest raises :class:`StoriesModeError`."""
        story_set = self.load_checked()
        loaded = len(story_set.entries)
        try:
            sched = self.compute_schedule(story_set, phases)
        except _UnknownSelector as e:
            return Pick(halt=Halt(key=e.selector, cause=HALT_UNKNOWN_SELECTOR), loaded_count=loaded)
        if sched.outcome == stories.SCHEDULE_NEXT and sched.entry is not None:
            return Pick(ref=UnitRef(key=sched.entry.id), loaded_count=loaded)
        if sched.outcome == stories.SCHEDULE_WEDGED and sched.entry is not None:
            return Pick(
                halt=Halt(key=sched.entry.id, cause=HALT_WEDGED, payload=sched),
                loaded_count=loaded,
            )
        return Pick(loaded_count=loaded)  # SCHEDULE_COMPLETE — every story is done

    def remaining_estimate(self, phases: Mapping[str, Phase]) -> int:
        """Stories a resume would still drive. Mirrors the scheduler that drives
        ``is_complete`` (:meth:`compute_schedule`): scan the manifest in list order,
        skip entries already retired this run (DONE/DEFERRED) or done on disk, count
        the actionable ones, and stop at the first wedge — a blocked story a plain
        resume cannot leapfrog, so later entries are unreachable until ``resolve``.
        Reuses the scheduler's own classifier so the count stays in lockstep with
        ``is_complete`` (both derive from :func:`stories._classify`). A manifest or
        spec read error raises; the engine reads that as no estimate."""
        story_set = self.load()
        folder = self.folder()
        skip = _retired(phases)
        entries = story_set.entries
        if self.id_filter is not None:
            entries = tuple(e for e in entries if e.id == self.id_filter)
        remaining = 0
        for entry in entries:
            if entry.id in skip:
                continue
            disposition = stories._classify(stories.resolve_story_spec(folder, entry.id))
            if disposition == "done":
                continue
            if disposition == "wedged":
                break  # a blocked story stops the scan; the rest is unreachable
            remaining += 1  # actionable
        return remaining

    # ----------------------------------------------- no epics: inert answers

    def epic_progress(self, epic: int) -> EpicProgress:
        """Never complete: a flat list has no epic to finish."""
        return EpicProgress(complete=False, retro_done=False)

    def retro_done(self, epic: int) -> bool:
        return False

    def retro_artifact_errors(self, epic: int) -> tuple[list[str], list[Path]]:
        return ["stories mode has no epic retrospective"], []

    def retro_docs(self, epic: int) -> list[Path]:
        return []

    def retro_commit_paths(self, docs: Sequence[Path]) -> set[Path]:
        return set(docs)

    def retro_target(self, epic: int) -> str:
        return str(epic)

    def epic_label(self, epic: int) -> str:
        return f"epic-{epic}"


if TYPE_CHECKING:
    _conforms: type[WorkSource] = StoriesWorkSource
