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


def worktree_isolation_conflict(paths: ProjectPaths, isolation: str) -> str | None:
    """The refusal message for ``isolation = "worktree"`` under a `repo_root`
    override, or None when the combination is supported (#414).

    Worktree provisioning reads ``repo_root`` for every surface it seeds *off disk*
    — the upstream skill trees, `_bmad/` and the `_bmad/custom/` overrides inside
    it, and each `seed_files`/`seed_globs` entry — and bakes the absolute hook-relay
    path from it into the worktree's hook config, while `init`, `validate` and the
    run preflight write and probe those same surfaces under ``project``. (The relay
    itself is pointed at, never copied. The `MODULE_SKILLS` this wheel bundles are
    seeded from package data and are unaffected by either root; nothing is seeded
    from ``project``, which `provision_worktree` is never even passed.)
    `load_paths` *requires* `project/_bmad/bmm/config.yaml`, so `_bmad/` is under
    `project` by definition and `repo_root/_bmad/` generally does not exist. When
    the two diverge the preflight therefore approves a surface the isolated run
    never receives, and the seed-completeness gates go inert rather than fire: an
    isolated session dispatches into a worktree with no dev primitive and no
    renderer, and stops with no result and nothing journaled naming the cause.

    Refusing is the whole fix here — the alternative (plumb `project` through
    provisioning for the non-git reads) is a behaviour change this patch release
    does not make, so the message names only remediations that exist today. Both
    are named because either alone is sufficient and which one is right is the
    operator's call: the override buys a decoupled git root, the isolation mode
    buys per-unit worktrees, and 0.9.x cannot give both.

    Sole producer of the text, shared by `cmd_validate`, the run/sweep preflight,
    the dry-run honesty banner and the TUI's pre-launch guard, so the four cannot
    drift. Compares resolved paths: `load_paths` resolves both sides, but a
    hand-built :class:`ProjectPaths` (tests) need not have."""
    if isolation != "worktree":
        return None
    if paths.repo_root.resolve() == paths.project.resolve():
        return None
    return (
        'isolation = "worktree" is not supported when repo_root differs from the project '
        f"directory: worktree provisioning seeds from repo_root ({paths.repo_root}) while "
        f"init, validate and the run preflight read the project ({paths.project}), so an "
        "isolated session would get none of the skills the preflight just approved. "
        "Remove the `repo_root` key from _bmad/bmm/config.yaml, or set "
        '`isolation = "none"` under [scm] in .bmad-loop/policy.toml.'
    )


def _resolve(raw: str, project: Path) -> Path:
    return Path(raw.replace("{project-root}", str(project))).resolve()


def load_paths(project: Path) -> ProjectPaths:
    project = project.resolve()
    config_path = project / "_bmad" / "bmm" / "config.yaml"
    if not config_path.is_file():
        raise BmadConfigError(f"BMAD config not found: {config_path} (is BMAD installed here?)")
    try:
        # UnicodeDecodeError is a ValueError, not an OSError, so an undecodable file
        # would otherwise escape every caller's `except BmadConfigError` and crash
        # them. Same reasoning as `policy.load`.
        raw = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise BmadConfigError(f"{config_path} is not valid UTF-8: {e}") from e
    try:
        doc = yaml.safe_load(raw) or {}
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
