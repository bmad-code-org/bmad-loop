"""Resolve BMAD artifact paths from _bmad/bmm/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class BmadConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectPaths:
    project: Path
    implementation_artifacts: Path
    planning_artifacts: Path
    # the BMAD output root (parent of the artifact dirs, holds project-context,
    # test-artifacts, story-bmad_loop, …). Protected wholesale on rollback so a
    # failed attempt never deletes generated BMAD output. Defaults to
    # {project-root}/_bmad-output when the config omits `output_folder`.
    output_folder: Path = field(default=None)  # type: ignore[assignment]
    # the git root code/git work happens against; defaults to `project`. Phase 1
    # foundation for worktree isolation — see ProjectPaths.rebased and Workspace.
    repo_root: Path = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.output_folder is None:
            object.__setattr__(self, "output_folder", (self.project / "_bmad-output").resolve())
        if self.repo_root is None:
            object.__setattr__(self, "repo_root", self.project)

    @property
    def sprint_status(self) -> Path:
        return self.implementation_artifacts / "sprint-status.yaml"

    @property
    def deferred_work(self) -> Path:
        return self.implementation_artifacts / "deferred-work.md"

    def rebased(self, new_root: Path) -> ProjectPaths:
        """Re-resolve the project and its artifact dirs onto `new_root` (a full
        checkout, e.g. a git worktree). Artifact dirs configured outside the
        project tree are shared, not per-checkout, so they don't move. The new
        ProjectPaths is rooted at `new_root` for both `project` and `repo_root`."""
        new_root = new_root.resolve()

        def rebase(p: Path) -> Path:
            try:
                rel = p.relative_to(self.project)
            except ValueError:
                return p  # configured outside the project tree; doesn't move
            return (new_root / rel).resolve()

        return ProjectPaths(
            project=new_root,
            implementation_artifacts=rebase(self.implementation_artifacts),
            planning_artifacts=rebase(self.planning_artifacts),
            output_folder=rebase(self.output_folder),
            repo_root=new_root,
        )


def _resolve(raw: str, project: Path) -> Path:
    return Path(raw.replace("{project-root}", str(project))).resolve()


def load_paths(project: Path) -> ProjectPaths:
    project = project.resolve()
    config_path = project / "_bmad" / "bmm" / "config.yaml"
    if not config_path.is_file():
        raise BmadConfigError(f"BMAD config not found: {config_path} (is BMAD installed here?)")
    try:
        doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise BmadConfigError(f"invalid YAML in {config_path}: {e}") from e

    impl = doc.get("implementation_artifacts")
    plan = doc.get("planning_artifacts")
    if not impl or not plan:
        raise BmadConfigError(
            f"{config_path} missing implementation_artifacts/planning_artifacts keys"
        )
    repo_root_raw = doc.get("repo_root")
    repo_root = _resolve(str(repo_root_raw), project) if repo_root_raw else project
    out_raw = doc.get("output_folder")
    output_folder = _resolve(str(out_raw), project) if out_raw else (project / "_bmad-output")
    return ProjectPaths(
        project=project,
        implementation_artifacts=_resolve(str(impl), project),
        planning_artifacts=_resolve(str(plan), project),
        output_folder=output_folder.resolve(),
        repo_root=repo_root,
    )
