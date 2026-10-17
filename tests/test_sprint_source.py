"""SprintWorkSource: the sprint board as a runtime WorkSource.

The source is a lift of the Engine's board reads, so these rows pin it against
the Engine on the same board (pick order, remaining estimate), plus the parts the
Engine only reached through a session: the epic progress and retro checks, the
retro-doc glob, and the commit paths. Source-only rows run on ``project_tree``:
a source never consults a repository.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from conftest import write_sprint

from bmad_loop import sprintstatus
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.engine import Engine
from bmad_loop.journal import Journal
from bmad_loop.model import Phase, RunState, StoryTask
from bmad_loop.policy import GatesPolicy, NotifyPolicy, Policy
from bmad_loop.sprint_source import SprintWorkSource
from bmad_loop.worksource import SOURCE_SPRINT, EpicProgress, SourceUnreadable

# Out of file order on purpose: epic 5 sits above epic 9, which is the case the
# current-epic preference exists for.
BOARD = {
    "epic-5": "in-progress",
    "5-1-e5": "ready-for-dev",
    "5-2-e5b": "backlog",
    "epic-9": "in-progress",
    "9-0-x": "ready-for-dev",
    "9-1-y": "ready-for-dev",
    "9-2-z": "done",
    "epic-9-retrospective": "optional",
}


def _source(paths: ProjectPaths, epic: int | None = None, story: str | None = None):
    return SprintWorkSource(lambda: paths, epic, story)


def _engine(project: ProjectPaths, **kwargs) -> Engine:
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    return Engine(
        paths=project,
        policy=Policy(gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=False)),
        adapter=MockAdapter([]),
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
        **kwargs,
    )


def _drain(pick_key, touched: dict[str, Phase], current_epic: int | None) -> list[str]:
    """Pick until the queue is exhausted, retiring each pick, as `_loop` does."""
    order: list[str] = []
    while (key_epic := pick_key(touched, current_epic)) is not None:
        key, current_epic = key_epic
        order.append(key)
        touched[key] = Phase.DONE
    return order


# ------------------------------------------------------------------ construction


def test_construction_does_no_io(tmp_path):
    """Building the source never calls its paths getter, so it reads nothing —
    `SweepEngine` inherits `Engine.__init__` and never reads the board.

    Ablation: read the board in `__init__` and the getter count reddens."""
    calls: list[int] = []

    def paths_get() -> ProjectPaths:
        calls.append(1)
        raise AssertionError("constructing the source read its paths")

    source = SprintWorkSource(paths_get, 3, "3-1")
    assert calls == []
    assert source.name == SOURCE_SPRINT and source.has_epics


def test_bad_selector_raises_at_construction(project):
    """The selector is parsed eagerly, so a bad `--story` still fails the Engine
    build itself, as it did when the Engine parsed it."""
    with pytest.raises(sprintstatus.SprintStatusError, match="ambiguous story"):
        SprintWorkSource(lambda: project, None, "2")
    with pytest.raises(sprintstatus.SprintStatusError, match="ambiguous story"):
        _engine(project, story_filter="2")


def test_engine_composes_a_sprint_source_on_live_paths(project, tmp_path):
    """`Engine.source` is a SprintWorkSource reading `engine.paths` at call time,
    never a snapshot taken at construction.

    Ablation: bind the source to the construction-time `paths` and the second
    pick reddens."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    other = dataclasses.replace(project, implementation_artifacts=tmp_path / "other")
    other.implementation_artifacts.mkdir()
    write_sprint(other, {"7-1-q": "ready-for-dev"})
    engine = _engine(project)
    assert isinstance(engine.source, SprintWorkSource)
    assert engine._pick_next().key == "1-1-a"

    engine.paths = other
    assert engine._pick_next().key == "7-1-q"


# -------------------------------------------------------------------------- pick


def test_pick_order_matches_engine_on_the_same_board(project):
    """Draining the board through the source and through `Engine._pick_next`
    gives one order: the current epic first, then strict file order, skipping
    what the run touched."""
    write_sprint(project, BOARD)
    source = _source(project)

    def via_source(touched, current_epic):
        ref = source.pick(touched, current_epic).ref
        return None if ref is None else (ref.key, ref.epic)

    engine = _engine(project)

    def via_engine(touched, current_epic):
        engine.state.tasks = {
            k: StoryTask(story_key=k, epic=0, phase=p) for k, p in touched.items()
        }
        engine.state.current_epic = current_epic
        ref = engine._pick_next()
        return None if ref is None else (ref.key, ref.epic)

    expected = ["9-0-x", "9-1-y", "5-1-e5", "5-2-e5b"]
    assert _drain(via_source, {}, 9) == expected
    assert _drain(via_engine, {}, 9) == expected
    # no current epic: strict file order
    assert _drain(via_source, {}, None) == ["5-1-e5", "5-2-e5b", "9-0-x", "9-1-y"]
    assert _drain(via_engine, {}, None) == ["5-1-e5", "5-2-e5b", "9-0-x", "9-1-y"]


def test_pick_skips_touched_and_applies_selector(project_tree):
    write_sprint(project_tree, BOARD)
    assert _source(project_tree).pick({"5-1-e5": Phase.DONE}, None).ref.key == "5-2-e5b"
    # the selector's rejections never leak into the fallback pass
    scoped = _source(project_tree, epic=9)
    assert scoped.pick({}, 5).ref.key == "9-0-x"
    assert _source(project_tree, story="9-1").pick({}, None).ref.key == "9-1-y"
    assert scoped.pick({"9-0-x": Phase.DONE, "9-1-y": Phase.DONE}, 9).ref is None


def test_pick_reports_unknown_keys_and_never_halts(project_tree):
    """Unknown board keys ride the Pick for the Engine to journal; the source
    itself writes nothing."""
    write_sprint(project_tree, {"1-1-a": "ready-for-dev", "totally-weird": "huh"})
    pick = _source(project_tree).pick({}, None)
    assert pick.ref.key == "1-1-a"
    assert pick.unknown_keys == ("totally-weird",)
    assert pick.halt is None
    write_sprint(project_tree, {"1-1-a": "done"})
    assert _source(project_tree).pick({}, None) == type(pick)()


def test_pick_board_error_propagates_unwrapped(project_tree):
    """A pick on an unreadable board raises the board's own error, as
    `Engine._pick_next` always has — not SourceUnreadable."""
    project_tree.sprint_status.write_text("development_status: [\n", encoding="utf-8")
    with pytest.raises(sprintstatus.SprintStatusError):
        _source(project_tree).pick({}, None)


# ------------------------------------------------------------- remaining estimate


def test_remaining_estimate_matches_engine(project):
    write_sprint(project, BOARD)
    engine = _engine(project)
    engine.state.tasks = {"9-0-x": StoryTask(story_key="9-0-x", epic=9, phase=Phase.DONE)}
    # actionable (ready-for-dev / backlog) minus touched: 5-1, 5-2, 9-1
    assert _source(project).remaining_estimate({"9-0-x": Phase.DONE}) == 3
    assert engine._remaining_estimate() == 3


def test_remaining_estimate_raises_and_engine_reads_none(project):
    project.sprint_status.unlink(missing_ok=True)
    with pytest.raises(sprintstatus.SprintStatusError):
        _source(project).remaining_estimate({})
    assert _engine(project)._remaining_estimate() is None


# ---------------------------------------------------------- epic progress / retro


def test_epic_progress_complete_incomplete_empty(project_tree):
    write_sprint(
        project_tree,
        {
            "1-1-a": "done",
            "1-2-b": "done",
            "epic-1-retrospective": "done",
            "2-1-c": "done",
            "2-2-d": "review",
        },
    )
    source = _source(project_tree)
    assert source.epic_progress(1) == EpicProgress(complete=True, retro_done=True)
    assert source.epic_progress(2) == EpicProgress(complete=False, retro_done=False)
    assert source.epic_progress(3) == EpicProgress(complete=False, retro_done=False)
    assert source.retro_done(1) and not source.retro_done(2)


def test_unreadable_board_is_source_unreadable_with_the_same_text(project_tree):
    """`SourceUnreadable` carries the load error's text unchanged — the
    `board-unreadable` journal lines are built from it."""
    project_tree.sprint_status.write_text("development_status: [\n", encoding="utf-8")
    with pytest.raises(sprintstatus.SprintStatusError) as original:
        sprintstatus.load(project_tree.sprint_status)
    source = _source(project_tree)
    for read in (source.epic_progress, source.retro_done):
        with pytest.raises(SourceUnreadable) as e:
            read(1)
        assert str(e.value) == str(original.value)
        assert isinstance(e.value.__cause__, sprintstatus.SprintStatusError)


def test_retro_docs_glob_keeps_regular_files_of_the_epic_sorted(project_tree):
    art = project_tree.implementation_artifacts
    for name in ("epic-2-retro-b.md", "epic-2-retro-a.md", "epic-3-retro-a.md", "epic-2-x.md"):
        (art / name).write_text("x\n", encoding="utf-8")
    (art / "epic-2-retro-dir.md").mkdir()
    if hasattr(os, "symlink"):
        try:
            (art / "epic-2-retro-link.md").symlink_to(art / "epic-2-retro-a.md")
        except OSError:
            pass  # no symlink privilege (Windows): the regular-file rows still hold
    docs = _source(project_tree).retro_docs(2)
    assert [d.name for d in docs] == ["epic-2-retro-a.md", "epic-2-retro-b.md"]


def test_retro_artifact_errors_texts(project_tree):
    """The board and doc halves of `Engine._verify_retro`, texts unchanged."""
    source = _source(project_tree)
    write_sprint(project_tree, {"4-1-a": "done"})
    assert source.retro_artifact_errors(4) == (
        [
            "sprint-status epic-4-retrospective is absent",
            "no epic-4-retro-*.md retrospective doc",
        ],
        [],
    )
    write_sprint(project_tree, {"4-1-a": "done", "epic-4-retrospective": "done"})
    doc = project_tree.implementation_artifacts / "epic-4-retro-2026.md"
    doc.write_text("x\n", encoding="utf-8")
    assert source.retro_artifact_errors(4) == ([], [doc])
    project_tree.sprint_status.write_bytes(b"development_status:\n  x: \xff\xfe\n")
    assert source.retro_artifact_errors(4) == (["sprint-status unreadable"], [doc])


def test_retro_commit_paths_target_and_label(project_tree):
    source = _source(project_tree)
    doc = Path(project_tree.implementation_artifacts / "epic-4-retro-a.md")
    assert source.retro_commit_paths([doc]) == {project_tree.sprint_status, doc}
    assert source.retro_target(4) == "4"
    assert source.epic_label(4) == "epic-4"
