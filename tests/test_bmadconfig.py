"""ProjectPaths.repo_root / rebased and load_paths(repo_root) — the Phase 1
Workspace-seam foundation. repo_root defaults to project (today's behavior);
rebased re-roots artifacts onto a worktree-style checkout."""

from __future__ import annotations

from pathlib import Path

from conftest import install_bmad_config

from bmad_loop import bmadconfig
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.workspace import Workspace


def test_repo_root_defaults_to_project(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
    )
    assert paths.repo_root == paths.project


def test_repo_root_explicit_is_kept(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
        repo_root=tmp_path / "repo",
    )
    assert paths.repo_root == tmp_path / "repo"


def test_load_paths_repo_root_defaults_to_project(project) -> None:
    install_bmad_config(project)
    loaded = bmadconfig.load_paths(project.project)
    assert loaded.repo_root == project.project.resolve()


def test_load_paths_reads_repo_root_key(project) -> None:
    install_bmad_config(project)
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(cfg.read_text() + "repo_root: '{project-root}/sub'\n")
    loaded = bmadconfig.load_paths(project.project)
    assert loaded.repo_root == (project.project / "sub").resolve()


def test_rebased_reroots_project_and_artifacts(tmp_path: Path) -> None:
    src = tmp_path / "main"
    paths = ProjectPaths(
        project=src,
        implementation_artifacts=src / "out" / "impl",
        planning_artifacts=src / "out" / "plan",
    )
    wt = tmp_path / "worktree"
    rebased = paths.rebased(wt)

    assert rebased.project == wt.resolve()
    assert rebased.repo_root == wt.resolve()
    assert rebased.implementation_artifacts == (wt / "out" / "impl").resolve()
    assert rebased.planning_artifacts == (wt / "out" / "plan").resolve()
    # derived artifact files follow the rebase
    assert rebased.sprint_status == (wt / "out" / "impl" / "sprint-status.yaml").resolve()
    assert rebased.deferred_work == (wt / "out" / "impl" / "deferred-work.md").resolve()


def test_rebased_leaves_external_artifacts_in_place(tmp_path: Path) -> None:
    src = tmp_path / "main"
    external = tmp_path / "shared" / "impl"
    paths = ProjectPaths(
        project=src,
        implementation_artifacts=external,
        planning_artifacts=src / "out" / "plan",
    )
    rebased = paths.rebased(tmp_path / "worktree")
    # configured outside the project tree → shared, not per-checkout
    assert rebased.implementation_artifacts == external
    assert rebased.planning_artifacts == (tmp_path / "worktree" / "out" / "plan").resolve()


def test_workspace_default_uses_repo_root(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
        repo_root=tmp_path / "repo",
    )
    ws = Workspace.default(paths)
    assert ws.root == tmp_path / "repo"
    assert ws.paths is paths
