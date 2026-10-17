"""StoriesWorkSource: the stories.yaml manifest as a runtime WorkSource.

The source is a lift of the StoriesEngine's manifest reads, so these rows pin it
on its own (pick, halts, remaining estimate, the inert epic answers) and against
the StoriesEngine that composes it (the same estimate, the folder anchor that
follows the live workspace under worktree isolation). Source-only rows run on
``project_tree``: a source never consults a repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import git, nested_repo_root_paths

from bmad_loop import stories, stories_engine, stories_source
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.journal import Journal
from bmad_loop.model import Phase, RunState, StoryTask
from bmad_loop.policy import GatesPolicy, NotifyPolicy, Policy, ReviewPolicy, ScmPolicy
from bmad_loop.stories_engine import StoriesEngine
from bmad_loop.stories_source import (
    HALT_UNKNOWN_SELECTOR,
    HALT_WEDGED,
    StoriesModeError,
    StoriesWorkSource,
)
from bmad_loop.worksource import SOURCE_STORIES, EpicProgress, UnitRef
from bmad_loop.workspace import Workspace

SPEC_FOLDER = "_bmad-output/epic-1"


def _entry(story_id: str) -> dict:
    return {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}


def _manifest(paths: ProjectPaths, ids: list[str]) -> Path:
    folder = paths.project / SPEC_FOLDER
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "SPEC.md").write_text("---\ntitle: Epic 1\n---\n# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(
        yaml.safe_dump([_entry(i) for i in ids], sort_keys=False), encoding="utf-8"
    )
    return folder


def _status(paths: ProjectPaths, story_id: str, status: str) -> None:
    spec = paths.project / SPEC_FOLDER / "stories" / f"{story_id}-slug.md"
    spec.write_text(f"---\nstatus: {status}\n---\n", encoding="utf-8")


def _source(paths: ProjectPaths, id_filter: str | None = None) -> StoriesWorkSource:
    workspace = Workspace.default(paths)
    return StoriesWorkSource(lambda: workspace, lambda: SPEC_FOLDER, id_filter)


def _engine(project: ProjectPaths, policy: Policy | None = None, **kwargs) -> StoriesEngine:
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    return StoriesEngine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=NotifyPolicy(desktop=False),
            review=ReviewPolicy(enabled=False),
        ),
        adapter=MockAdapter([]),
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(
            run_id="test-run",
            project=str(project.project),
            started_at="now",
            story_filter=kwargs.get("story_filter"),
        ),
        spec_folder=SPEC_FOLDER,
        **kwargs,
    )


# ------------------------------------------------------------------ identity


def test_has_epics_false_and_name_is_stories(project_tree):
    source = _source(project_tree)
    assert source.name == SOURCE_STORIES
    assert source.has_epics is False


def test_stories_mode_error_keeps_its_stories_engine_import_path():
    """Tests and callers import `StoriesModeError` from `stories_engine`; the move
    to `stories_source` must leave that path naming the same class."""
    assert stories_engine.StoriesModeError is stories_source.StoriesModeError


def _unread_workspace() -> Workspace:
    raise AssertionError("read the workspace")


def _unread_spec_folder() -> str:
    raise AssertionError("read the spec folder")


def test_construction_does_no_io():
    """The getters are only stored: neither is called until a read."""
    source = StoriesWorkSource(_unread_workspace, _unread_spec_folder, "1")
    assert source.id_filter == "1"


# ---------------------------------------------------------------------- pick


def test_pick_next_entry_skips_done_and_retired(project_tree):
    """The first actionable entry in list order is a `UnitRef` with epic 0, past
    entries done on disk and those this run already retired (DONE / DEFERRED). A
    touched story in any other phase is not retired and is picked again."""
    _manifest(project_tree, ["1", "2", "3", "4"])
    _status(project_tree, "1", "done")
    source = _source(project_tree)

    pick = source.pick({"2": Phase.DEFERRED}, 0)
    assert pick.ref == UnitRef(key="3", epic=0)
    assert pick.halt is None and pick.loaded_count == 4

    assert source.pick({"2": Phase.DONE, "3": Phase.DEV_RUNNING}, 0).ref == UnitRef("3")


def test_pick_complete_is_no_ref_and_no_halt(project_tree):
    _manifest(project_tree, ["1", "2"])
    _status(project_tree, "1", "done")
    _status(project_tree, "2", "done")

    pick = _source(project_tree).pick({}, None)

    assert pick.ref is None and pick.halt is None and pick.loaded_count == 2


def test_pick_wedge_is_halt_with_schedule_payload(project_tree):
    """A blocked entry stops the scan: the pick halts on it with the schedule as
    payload, and a later actionable entry is never handed out."""
    _manifest(project_tree, ["1", "2", "3"])
    _status(project_tree, "1", "done")
    _status(project_tree, "2", "blocked")

    pick = _source(project_tree).pick({}, 0)

    assert pick.ref is None and pick.halt is not None
    assert (pick.halt.key, pick.halt.cause) == ("2", HALT_WEDGED)
    payload = pick.halt.payload
    assert isinstance(payload, stories.Schedule) and payload.is_wedged
    assert payload.entry is not None and payload.entry.id == "2"
    assert payload.state is not None and payload.state.status == "blocked"


def test_unknown_selector_is_halt_not_raise(project_tree):
    """A `--story` id the manifest does not hold halts keyed on the id; it never
    raises, so the engine can pause for resolve. The manifest still loaded."""
    _manifest(project_tree, ["1", "2"])

    pick = _source(project_tree, id_filter="9").pick({}, 0)

    assert pick.ref is None and pick.halt is not None
    assert (pick.halt.key, pick.halt.cause) == ("9", HALT_UNKNOWN_SELECTOR)
    assert pick.loaded_count == 2


@pytest.mark.parametrize("fault", ["missing", "malformed"])
def test_broken_manifest_raises_stories_mode_error(project_tree, fault):
    folder = _manifest(project_tree, ["1"])
    manifest = folder / "stories.yaml"
    if fault == "missing":
        manifest.unlink()
    else:
        manifest.write_text("- id: [unclosed\n", encoding="utf-8")

    with pytest.raises(StoriesModeError, match="stories.yaml no longer usable"):
        _source(project_tree).pick({}, 0)


# --------------------------------------------------------- remaining estimate


def test_remaining_estimate_raises_on_broken_manifest(project_tree):
    """The source raises; only the engine's guard turns that into None."""
    folder = _manifest(project_tree, ["1"])
    (folder / "stories.yaml").unlink()

    with pytest.raises(stories.StoriesError):
        _source(project_tree).remaining_estimate({})


def test_remaining_matches_engine_estimate(project):
    """The engine's estimate is the source's count over the run's task phases:
    retired and done-on-disk entries skipped, the scan stopped at the wedge."""
    _manifest(project, [str(i) for i in range(1, 7)])
    _status(project, "2", "done")
    _status(project, "5", "blocked")
    engine = _engine(project)
    engine.state.tasks["1"] = StoryTask(story_key="1", epic=0, phase=Phase.DONE)
    engine.state.tasks["3"] = StoryTask(story_key="3", epic=0, phase=Phase.DEV_RUNNING)

    expected = _source(project).remaining_estimate(engine._phases())

    assert expected == 2  # 3 and 4; 6 sits behind the wedge
    assert engine._remaining_estimate() == expected


def test_remaining_estimate_honors_id_filter(project_tree):
    _manifest(project_tree, ["1", "2"])
    _status(project_tree, "1", "blocked")

    assert _source(project_tree, id_filter="2").remaining_estimate({}) == 1


# ------------------------------------------------------- no epics: inert answers


def test_epic_answers_are_inert_and_read_nothing(tmp_path):
    """`has_epics` is false, so the engine never asks; if it did, nothing would
    read as a complete epic or a done retro, and nothing is read to say so."""
    source = StoriesWorkSource(_unread_workspace, _unread_spec_folder, None)
    docs = [tmp_path / "x.md"]

    assert source.epic_progress(0) == EpicProgress(complete=False, retro_done=False)
    assert source.retro_done(0) is False
    assert source.retro_docs(0) == []
    errors, found = source.retro_artifact_errors(0)
    assert errors and found == []
    assert source.retro_commit_paths(docs) == set(docs)


# --------------------------------------------------------------- composition


def test_engine_composes_a_stories_source(project):
    """StoriesEngine builds its own source inside `Engine.__init__`, before it
    has set `_spec_folder_rel`; the getter reads it after, so the source and the
    engine's delegators agree."""
    _manifest(project, ["1"])
    engine = _engine(project, story_filter="1")

    assert isinstance(engine.source, StoriesWorkSource)
    assert engine.source.spec_folder_rel == engine._spec_folder_rel == SPEC_FOLDER
    assert engine.source.id_filter == engine._story_id_filter == "1"
    assert engine._stories_folder() == engine.source.folder() == project.project / SPEC_FOLDER
    assert engine._load_stories().get("1") is not None


def test_folder_anchors_on_live_workspace_project(project):
    """Under worktree isolation with a nested `repo_root:`, a driven story's
    manifest folder is the unit mount's project (`<mount>/app/<folder>`), not the
    main checkout: the source reads `engine.workspace` live, which
    `run_isolated` swaps for each unit. Back in place it is the main project again.

    Ablation: snapshot the workspace in `StoriesEngine._make_work_source` (pass
    `lambda ws=self.workspace: ws`) and the drive sees the main checkout's folder."""
    paths = nested_repo_root_paths(project)
    _manifest(paths, ["1"])
    git(paths.project, "add", "-A")
    git(paths.project, "commit", "-q", "-m", "stories fixture")
    engine = _engine(
        paths,
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=NotifyPolicy(desktop=False),
            review=ReviewPolicy(enabled=False),
            scm=ScmPolicy(isolation="worktree", keep_failed=False),
        ),
    )
    engine.state.repo_root = str(paths.repo_root)
    engine.state.target_branch = "main"
    task = StoryTask("1", 0)
    engine.state.tasks[task.story_key] = task
    seen: list[tuple[Path, Path]] = []

    def drive(current):
        seen.append((engine.source.folder(), engine.workspace.paths.project))
        current.phase = Phase.DEFERRED
        current.defer_reason = "test complete"

    engine._run_isolated(task, drive)

    assert len(seen) == 1
    folder, mount_project = seen[0]
    assert mount_project.name == "app" and mount_project != paths.project, "premise: a mount"
    assert folder == mount_project / SPEC_FOLDER
    assert engine.source.folder() == paths.project / SPEC_FOLDER
