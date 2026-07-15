"""Resolve BMAD artifact paths from _bmad/bmm/config.yaml."""

from __future__ import annotations

import tomllib
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


# The four-layer central TOML config (BMAD-METHOD #2285), lowest priority first.
_TOML_LAYERS = (
    ("config.toml",),
    ("config.user.toml",),
    ("custom", "config.toml"),
    ("custom", "config.user.toml"),
)


def _toml_merge(base: object, override: object) -> object:
    """Dict-aware deep merge, matching the renderer's central-config semantics:
    tables deep-merge, everything else the override wins."""
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            result[key] = _toml_merge(result[key], value) if key in result else value
        return result
    return override


def toml_artifact_parity(project: Path, paths: ProjectPaths) -> str | None:
    """Warn when the central TOML and the legacy yaml disagree on the artifact dir.

    The renderer-era bmad-dev-auto bakes `implementation_artifacts` from the
    four-layer `_bmad/config.toml` merge, while `load_paths` (and so the whole
    orchestrator) reads the legacy `_bmad/bmm/config.yaml`. The installer
    regenerates the yaml from the TOML, but a hand-added `custom/config*.toml`
    override desyncs the two until the next install — the skill then writes
    specs and the deferred-work ledger where the orchestrator is not looking.

    Mirrors the renderer's precedence: layers deep-merge lowest-to-highest,
    then `[modules.bmm]` beats `[core]` on collision. Returns a warning string
    on mismatch; None when they agree, when the TOML surface is absent or
    unparseable (the renderer preflight owns reporting that), or when the TOML
    never sets the key. Follow-up: make load_paths read the TOML as primary
    (issue #154) — this parity check retires with it.
    """
    project = project.resolve()
    bmad_dir = project / "_bmad"
    if not (bmad_dir / "config.toml").is_file():
        return None
    merged: object = {}
    for parts in _TOML_LAYERS:
        layer = bmad_dir.joinpath(*parts)
        if not layer.is_file():
            continue
        try:
            with layer.open("rb") as fh:
                doc = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError):
            return None
        merged = _toml_merge(merged, doc)
    if not isinstance(merged, dict):
        return None
    modules = merged.get("modules")
    bmm = modules.get("bmm") if isinstance(modules, dict) else None
    raw: str | None = None
    for section in (merged.get("core"), bmm):  # bmm wins on collision, like render.py
        if isinstance(section, dict) and isinstance(section.get("implementation_artifacts"), str):
            raw = section["implementation_artifacts"]
    if raw is None:
        return None
    toml_resolved = _resolve(raw, project)
    if toml_resolved == paths.implementation_artifacts:
        return None
    return (
        f"central TOML config resolves implementation_artifacts to {toml_resolved}, "
        f"but the legacy _bmad/bmm/config.yaml (which bmad-loop reads) resolves it "
        f"to {paths.implementation_artifacts} — the renderer-era bmad-dev-auto "
        f"follows the TOML, so specs and the deferred-work ledger would land where "
        f"the orchestrator is not looking; re-run the BMAD installer to regenerate "
        f"the yaml (bmad-loop will move to the TOML as primary — issue #154)"
    )


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
