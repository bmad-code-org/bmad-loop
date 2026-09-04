"""Resolve BMAD artifact paths from _bmad/bmm/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .platform_util import resolve_or_lexical


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

    **This function exists to be deleted.** The real fix is #443 — plumb ``project``
    through provisioning for the non-git reads — and landing it removes this
    function, all five of its call sites, the `policy.isolation-repo-root` id and
    both doc sentences. It is a refusal rather than the fix because "which root
    wins" is a separate decision per seeded surface (the relay only exists under
    `project`; operator-configured `seed_files` may legitimately name a path outside
    it), and `ProjectPaths.rebased` encodes `project == repo_root` besides. So the
    message names only remediations that exist today. Both are named because either
    alone is sufficient and which one is right is the operator's call: the override
    buys a decoupled git root, the isolation mode buys per-unit worktrees, and until
    #443 lands the orchestrator cannot give both.

    Sole producer of the text, shared by `cmd_validate`, the run/sweep preflight,
    the dry-run honesty banner and the TUI's pre-launch guard, so the four cannot
    drift. Compares resolved paths: `load_paths` resolves both sides, but a
    hand-built :class:`ProjectPaths` (tests) need not have."""
    if isolation != "worktree":
        return None
    # The default config — no `repo_root` key, so `__post_init__` makes the two the
    # same object — is settled here, before anything asks the OS. That is not just an
    # optimization: the two calls below are independent, so a *transient*
    # canonicalization failure between them (the guard catches every OSError, not only
    # a persistent WinError 64) could have one side degrade to lexical while the other
    # succeeds and canonicalizes, making one path unequal to itself and refusing an
    # ordinary isolated run with the #414 text. Comparing raw first means the common
    # shape cannot reach that window at all.
    if paths.repo_root == paths.project:
        return None
    # Degrades rather than raises (#552): this gate runs in `cmd_validate` *before*
    # the platform preflight, so a raise here is the #332 finding going unreachable
    # again for anyone on `isolation = "worktree"`. A ProjectPaths built by
    # `load_paths` arrives with both sides canonical (it raises otherwise), so the
    # degrade below covers only hand-built instances and a share flapping between
    # the load and this gate. Both sides take the same treatment, so a host that
    # cannot canonicalize compares lexical to lexical; the cost, stated rather than
    # hidden: two spellings that only canonicalization folds together (`p/../p` vs
    # `p`) would be refused with a wrong message, where the alternative is no
    # message and no command at all.
    if resolve_or_lexical(paths.repo_root) == resolve_or_lexical(paths.project):
        return None
    return (
        'isolation = "worktree" is not supported when repo_root differs from the project '
        f"directory: worktree provisioning seeds from repo_root ({paths.repo_root}) while "
        f"init, validate and the run preflight read the project ({paths.project}), so an "
        "isolated session would get none of the skills the preflight just approved. "
        "Remove the `repo_root` key from _bmad/bmm/config.yaml, or set "
        '`isolation = "none"` under [scm] in .bmad-loop/policy.toml.'
    )


def _canonical(expanded: Path, label: str) -> Path:
    """Canonicalize-or-raise, the shared boundary for every ProjectPaths member.
    `label` names what refused in the operator's terms — a configured string is
    reported with its raw spelling, the default output folder as the default —
    so the message never calls a path "configured" that nobody configured."""
    try:
        return expanded.resolve()
    except (OSError, RuntimeError) as e:
        raise BmadConfigError(
            f"cannot canonicalize the {label} ({expanded}): {e} — "
            "whether it lies inside or outside the project tree cannot be determined, "
            "so no run can safely proceed. Run `bmad-loop validate` for what this "
            "host is doing."
        ) from e


def _resolve(raw: str, project: Path) -> Path:
    """Expand `{project-root}` and canonicalize, or raise typed. A config string can
    name a UNC share of its own, independent of `--project`, so it refuses on the same
    terms as the root in `load_paths` (#552). Degrading to the lexical spelling was
    tried and retired: a spelling the OS cannot canonicalize has an *unknowable*
    location — it can sit lexically inside the project while an in-tree symlink or
    junction carries it to a dead share outside — so any in-tree/external answer
    `rebased`'s `relative_to` reads off the spelling is a guess, and a wrong guess
    sends a worktree-isolated run's artifact writes into a worktree-local directory
    instead of the configured destination. No member enters a snapshot unresolved."""
    expanded = Path(raw.replace("{project-root}", str(project)))
    return _canonical(expanded, f"configured path {raw!r}")


def load_paths(project: Path) -> ProjectPaths:
    # The root must canonicalize or there is no consistent ProjectPaths to hand back
    # (#552). Every member is compared against `project` — `rebased` decides "is this
    # artifact dir inside the tree" with `relative_to` — so a lexical root next to a
    # canonically spelled member (a resolved child, or an absolute path written
    # canonically in config.yaml) sits on the far side of a symlink from it, files an
    # in-tree artifact dir as external, and a worktree-isolated run then writes into
    # the original checkout. Degrading here reopened that split once per review round;
    # a typed raise closes every route at once, and it costs the diagnostic commands
    # nothing: every caller already catches BmadConfigError — `cmd_validate` records
    # the failure and still reaches the platform preflight that names the host — and
    # `diagnose` never loads paths at all. Only `cli._project` still degrades: it runs
    # pre-dispatch, where there is no handler to catch anything.
    try:
        project = Path(project).resolve()
    except (OSError, RuntimeError) as e:
        raise BmadConfigError(
            f"cannot canonicalize the project root {project}: {e} — artifact paths "
            "are derived from the canonical root, so no run can safely proceed. "
            "Run `bmad-loop validate` for what this host is doing."
        ) from e
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
    if not isinstance(doc, dict):
        raise BmadConfigError(f"{config_path} must contain a top-level mapping")

    impl = doc.get("implementation_artifacts")
    plan = doc.get("planning_artifacts")
    if not impl or not plan:
        raise BmadConfigError(
            f"{config_path} missing implementation_artifacts/planning_artifacts keys"
        )
    repo_root_raw = doc.get("repo_root")
    repo_root = _resolve(str(repo_root_raw), project) if repo_root_raw else project
    out_raw = doc.get("output_folder")
    output_folder = (
        _resolve(str(out_raw), project)
        if out_raw
        # the default branch is a bare join off the (canonical) root and takes the
        # same canonicalize-or-raise treatment as a configured string: an in-tree
        # junction under the default name misclassifies exactly like a configured one.
        else _canonical(project / "_bmad-output", "default output folder")
    )
    return ProjectPaths(
        project=project,
        implementation_artifacts=_resolve(str(impl), project),
        planning_artifacts=_resolve(str(plan), project),
        output_folder=output_folder,
        repo_root=repo_root,
    )
