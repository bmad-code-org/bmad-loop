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


# --------------- toml_artifact_parity (renderer-era desync warning) -----------


def _parity_paths(root: Path) -> ProjectPaths:
    return ProjectPaths(
        project=root.resolve(),
        implementation_artifacts=(root / "_bmad-output" / "implementation-artifacts").resolve(),
        planning_artifacts=(root / "_bmad-output" / "planning-artifacts").resolve(),
    )


def _write_toml(root: Path, rel: str, impl: str) -> None:
    path = root / "_bmad" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[modules.bmm]\nimplementation_artifacts = "{impl}"\n', encoding="utf-8")


def test_toml_parity_none_when_toml_surface_absent(tmp_path: Path) -> None:
    """No _bmad/config.toml → the renderer preflight owns that report, not us."""
    assert bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path)) is None


def test_toml_parity_none_when_agreeing(tmp_path: Path) -> None:
    _write_toml(tmp_path, "config.toml", "{project-root}/_bmad-output/implementation-artifacts")
    assert bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path)) is None


def test_toml_parity_warns_on_override_desync(tmp_path: Path) -> None:
    """A hand-added custom/config*.toml override moves the renderer's artifact
    dir while the legacy yaml (which the orchestrator reads) stays put — the
    exact desync the warning exists for."""
    _write_toml(tmp_path, "config.toml", "{project-root}/_bmad-output/implementation-artifacts")
    _write_toml(tmp_path, "custom/config.user.toml", "{project-root}/elsewhere")
    warning = bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path))
    assert warning is not None
    assert "elsewhere" in warning and "deferred-work ledger" in warning


def test_toml_parity_bmm_beats_core_like_renderer(tmp_path: Path) -> None:
    """render.py flattens [core] then [modules.bmm] (module wins on collision) —
    the parity check must mirror that precedence, even across layers."""
    base = tmp_path / "_bmad" / "config.toml"
    base.parent.mkdir(parents=True)
    base.write_text(
        "[modules.bmm]\n"
        'implementation_artifacts = "{project-root}/_bmad-output/implementation-artifacts"\n',
        encoding="utf-8",
    )
    # a LATER layer sets only [core]; bmm from the earlier layer still wins
    user = tmp_path / "_bmad" / "config.user.toml"
    user.write_text('[core]\nimplementation_artifacts = "{project-root}/core-dir"\n')
    assert bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path)) is None


def test_toml_parity_none_when_unparseable(tmp_path: Path) -> None:
    """An unparseable layer is the renderer preflight/probe's report (render.py
    HALTs on it since #2588) — parity stays quiet rather than double-reporting."""
    _write_toml(tmp_path, "config.toml", "{project-root}/elsewhere")
    (tmp_path / "_bmad" / "config.user.toml").write_text("not = [valid", encoding="utf-8")
    assert bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path)) is None


def test_toml_parity_none_when_key_never_set(tmp_path: Path) -> None:
    (tmp_path / "_bmad").mkdir()
    (tmp_path / "_bmad" / "config.toml").write_text("[core]\n", encoding="utf-8")
    assert bmadconfig.toml_artifact_parity(tmp_path, _parity_paths(tmp_path)) is None
