"""Resolve BMAD artifact paths from the central TOML config or _bmad/bmm/config.yaml.

Two sources, one answer per key (#769, #154). BMAD-METHOD v6.12.0's installer
(`tools/installer/core/manifest-generator.js` `writeCentralConfig`) writes the four
path keys' upstream homes into the central TOML — `output_folder` under ``[core]``,
`implementation_artifacts`/`planning_artifacts` under ``[modules.bmm]``
(`src/core-skills/module.yaml`, `src/bmm-skills/module.yaml`); `repo_root` is
bmad-loop's own key and has no upstream home, so it is found wherever an operator
puts it. The legacy per-module `_bmad/bmm/config.yaml` may or may not be beside it.

- **Layers**, lowest to highest: `_bmad/config.toml`, `_bmad/config.user.toml`,
  `_bmad/custom/config.toml`, `_bmad/custom/config.user.toml` — upstream
  `config_utils.load_central_config`. A missing layer is skipped; upstream requires
  the base layer, but that is the renderer's gate (`install.py` reports it), not a
  path-resolution one.
- **Merge**: a minimal faithful subset of upstream `config_utils.structural_merge` —
  tables merge recursively and anything else is replaced by the higher layer. The
  subset is exact for the four path keys because array semantics cannot reach
  them: the renderer's short-key lookup (`render_skill._find_config_values`) never
  descends into an array and never matches an array value, and upstream's array
  merge only ever yields an array where both sides were arrays — the same *type*
  this merge leaves at that spot. What the subset does not reproduce is upstream
  refusing a malformed keyed array (`code`/`id` not a non-empty string); that is a
  renderer diagnosis about a table no path key lives in.
- **Lookup**: each key resolves as `render_skill._resolve_short_config` resolves a
  ``{{.key}}`` token — every scalar entry of that name anywhere in the merged tree,
  and more than one is refused as ambiguous, naming each location and its layer.
  #154's "`[modules.bmm]` beats `[core]`" flattening is obsolete and not done.
- **Precedence**: a TOML value wins over the YAML for every key; the YAML fills a
  key only when the merged TOML has no entry of that name. Everything wrong with a
  present TOML source — an undecodable or malformed layer, a blank or non-string
  value, an ambiguous key — raises rather than falling back, because a fallback
  would silently run against paths the operator did not choose.
- With no TOML layer at all the YAML is read exactly as it always was.

`{project-root}` is substituted with the renderer's spelling (a literal replace
with the canonical root) by `_resolve`, for both sources."""

from __future__ import annotations

import errno
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .mountpaths import rebased_project
from .platform_util import resolve_or_lexical


class BmadConfigError(Exception):
    pass


def _diagnostic_text(text: str) -> str:
    """Render diagnostics using only ASCII, independent of stderr's codec."""
    return text.replace("\x00", "\\x00").encode("ascii", errors="backslashreplace").decode("ascii")


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
        checkout of `repo_root`, e.g. a git worktree).

        `repo_root` becomes `new_root`. The project becomes the MOUNT PROJECT —
        :func:`mountpaths.rebased_project`, the single definition: the project's
        offset inside `repo_root` re-joined onto `new_root` (``new_root`` itself in
        the default config, ``<new_root>/app`` for a project at ``<repo>/app``), or
        the project itself, unmoved, when it does not lie inside `repo_root`
        (a disjoint layout, which worktree isolation refuses — see
        :func:`worktree_isolation_conflict`). Artifact dirs move with the project:
        each is re-joined onto the new project at its offset inside the old one, and
        one configured outside the project tree is shared, not per-checkout, so it
        doesn't move (DW-379)."""
        new_root = new_root.resolve()
        new_project = rebased_project(self.project, self.repo_root, new_root)
        return ProjectPaths(
            project=new_project,
            implementation_artifacts=self.artifact_at(self.implementation_artifacts, new_project),
            planning_artifacts=self.artifact_at(self.planning_artifacts, new_project),
            output_folder=self.artifact_at(self.output_folder, new_project),
            repo_root=new_root,
        )

    def artifact_at(self, artifact: Path, mount_project: Path) -> Path:
        """Where `artifact` (one of this config's dirs) sits for the project mounted
        at `mount_project`: re-joined there at its offset inside `project`, or
        unmoved when configured outside the project tree, which is shared rather
        than per-checkout (DW-379). The rule :meth:`rebased` applies to every
        artifact dir, exposed for callers that hold the mount PROJECT rather than
        the checkout root — the session cwd (DW-484)."""
        try:
            rel = artifact.relative_to(self.project)
        except ValueError:
            return artifact
        return (mount_project / rel).resolve()


def worktree_isolation_conflict(paths: ProjectPaths, isolation: str) -> str | None:
    """The refusal message for ``isolation = "worktree"`` when the project directory
    does not lie inside `repo_root` (a DISJOINT layout), or None when the combination
    is supported (#414, narrowed by DW-379).

    A unit worktree is a checkout of `repo_root`, so it can carry the project-local
    surfaces provisioning seeds — `_bmad/` and the `_bmad/custom/` overrides inside
    it, the upstream and bundled skill trees, the hook configs, each
    `seed_files`/`seed_globs` entry — only when they live inside that checkout.
    `load_paths` *requires* its config under `project/_bmad/`, so those surfaces are
    under `project` by definition. A NESTED layout (the default `repo_root ==
    project` included, or a monorepo whose BMAD project is `<repo>/app`) is
    supported: the mount mirrors the main checkout and provisioning lands every
    surface at the project's offset inside it (`worktree_flow.provision_worktree`'s
    ``project=``). A disjoint layout — a sibling project, or `repo_root` nested inside
    the project such as ``project/moved-code`` — has no mirror to build: the
    checkout carries no copy of the project at all, so an isolated session would
    dispatch into a worktree with no dev primitive and no renderer and stop with
    nothing journaled naming the cause (the #414 silent stall). That layout is
    refused by design, not pending a fix.

    The message names three remediations, because which one is right is the
    operator's call: move the project inside `repo_root` (or point `repo_root` at
    an ancestor of it), drop the `repo_root` override, or give up per-unit worktrees.

    Sole producer of the text, shared by `cmd_validate`, the run/sweep/resume/resolve
    preflights, the dry-run honesty banner, the auto-sweep factory and the TUI's
    guards, so they cannot drift. Compares canonical paths: `load_paths` resolves
    both sides, but a hand-built :class:`ProjectPaths` (tests) need not have."""
    if isolation != "worktree":
        return None
    # The default config — no `repo_root` key, so `__post_init__` makes the two the
    # same object — is settled here, before anything asks the OS. That is not just an
    # optimization: the two calls below are independent, so a *transient*
    # canonicalization failure between them (the guard catches every OSError, not only
    # a persistent WinError 64) could have one side degrade to lexical while the other
    # succeeds and canonicalizes, making one path unequal to itself and refusing an
    # ordinary isolated run with the #414 text. Comparing raw first means the common
    # shape cannot reach that window at all. (Only equality is settled raw: a lexical
    # containment test would accept `repo/../elsewhere`, which canonicalizes outside.)
    if paths.repo_root == paths.project:
        return None
    # Degrades rather than raises (#552): this gate runs in `cmd_validate` *before*
    # the platform preflight, so a raise here is the #332 finding going unreachable
    # again for anyone on `isolation = "worktree"`. A ProjectPaths built by
    # `load_paths` arrives with both sides canonical (it raises otherwise), so the
    # degrade below covers only hand-built instances and a share flapping between
    # the load and this gate. Both sides take the same treatment, so a host that
    # cannot canonicalize compares lexical to lexical; the cost, stated rather than
    # hidden: two spellings that only canonicalization relates (`p/../p` vs `p`)
    # would be refused with a wrong message, where the alternative is no message
    # and no command at all.
    project = resolve_or_lexical(paths.project)
    if project.is_relative_to(resolve_or_lexical(paths.repo_root)):
        return None
    return (
        'isolation = "worktree" needs the project directory to be inside repo_root: '
        f"the project ({paths.project}) is not inside repo_root ({paths.repo_root}), "
        "and a unit worktree is a checkout of repo_root, so it can only carry the "
        "project-local surfaces (_bmad/, skills, hooks) found inside it — an isolated "
        "session would get none of the skills the preflight just approved. Move the "
        "project inside repo_root (or point `repo_root` at an ancestor of the project), "
        "remove the `repo_root` key from the BMAD config (_bmad/bmm/config.yaml or a "
        '_bmad/ TOML layer), or set `isolation = "none"` under [scm] in '
        ".bmad-loop/policy.toml."
    )


ARTIFACT_DIR_KEYS = ("implementation_artifacts", "planning_artifacts", "output_folder")


def shared_artifact_dirs(paths: ProjectPaths, isolation: str) -> list[tuple[str, Path]]:
    """The configured artifact dirs a worktree-isolated run shares with the main
    checkout, as ``(key, dir)`` in :data:`ARTIFACT_DIR_KEYS` order (DW-485).

    :meth:`ProjectPaths.rebased` moves only a dir inside the PROJECT into the unit
    worktree. One inside `repo_root` but outside the project — reachable only in a
    nested layout, the project at ``<repo>/app`` — stays pointed at the main
    checkout, so every isolated session reads and writes that one copy, though
    the unit worktree carries its own checkout of the same path. `rebased` is right
    to keep it (the operator configured that exact dir); this names it so the
    sharing is not a surprise. A dir inside the project is per-worktree, and one
    outside `repo_root` entirely is outside every checkout, so neither is listed;
    nor is anything under ``isolation = "none"``, where there is only one checkout.

    Compares the members as :func:`load_paths` canonicalized them — the same
    spelling `rebased`'s ``relative_to`` reads."""
    if isolation != "worktree":
        return []
    dirs = (paths.implementation_artifacts, paths.planning_artifacts, paths.output_folder)
    return [
        (key, path)
        for key, path in zip(ARTIFACT_DIR_KEYS, dirs, strict=True)
        if path.is_relative_to(paths.repo_root) and not path.is_relative_to(paths.project)
    ]


def _canonical(expanded: Path, label: str) -> Path:
    """Canonicalize-or-raise, the shared boundary for every ProjectPaths member.
    `label` names what refused in the operator's terms — a configured string is
    reported with its raw spelling, the default output folder as the default —
    so the message never calls a path "configured" that nobody configured."""
    try:
        return expanded.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        message = (
            f"cannot canonicalize the {label} ({expanded}): {e} — whether it lies "
            "inside or outside the project tree cannot be determined, so no run can "
            "safely proceed. Run `bmad-loop validate` for what this host is doing."
        )
        raise BmadConfigError(_diagnostic_text(message)) from e


def _resolve(raw: str, project: Path, origin: str) -> Path:
    """Expand `{project-root}` and canonicalize, or raise typed. `origin` names the
    key and file the string came from, for the refusal. A config string can
    name a UNC share of its own, independent of `--project`, so it refuses on the same
    terms as the root in `load_paths` (#552). Degrading to the lexical spelling was
    tried and retired: a spelling the OS cannot canonicalize has an *unknowable*
    location — it can sit lexically inside the project while an in-tree symlink or
    junction carries it to a dead share outside — so any in-tree/external answer
    `rebased`'s `relative_to` reads off the spelling is a guess, and a wrong guess
    sends a worktree-isolated run's artifact writes into a worktree-local directory
    instead of the configured destination. No member enters a snapshot unresolved."""
    expanded = Path(raw.replace("{project-root}", str(project)))
    return _canonical(expanded, f"configured path {raw!r} ({origin})")


LEGACY_CONFIG_REL = Path("_bmad") / "bmm" / "config.yaml"
# Lowest to highest — upstream `config_utils.load_central_config` (v6.12.0).
CENTRAL_LAYERS_REL: tuple[Path, ...] = (
    Path("_bmad") / "config.toml",
    Path("_bmad") / "config.user.toml",
    Path("_bmad") / "custom" / "config.toml",
    Path("_bmad") / "custom" / "config.user.toml",
)
_REQUIRED_KEYS = ("implementation_artifacts", "planning_artifacts")
_PATH_KEYS = (*_REQUIRED_KEYS, "repo_root", "output_folder")


@dataclass(frozen=True)
class _Leaf:
    """A non-table value in the merged central config, with the layer that set it."""

    value: object
    layer: Path


def _load_layer(path: Path) -> dict[str, object] | None:
    """One central TOML layer, None when absent. Present-but-unusable raises: the
    caller must never read an unparseable layer as "no TOML" and fall back. A link
    counts as present even when `exists()` (which follows it) says otherwise: a
    dangling or looping symlink is an entry the operator put there, not an absence.
    The probes are raw `lstat`/`stat`, not `exists()`: only a missing entry is
    absent, and any other failure (an unreadable directory, a dead share) is this
    layer's error — `exists()` raises it untyped through 3.13 and reads it as absent
    on 3.14+, which would let the YAML fill the key."""
    try:
        entry = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise BmadConfigError(_diagnostic_text(f"cannot read {path}: {e}")) from e
    try:
        target = path.stat()
    except OSError as e:
        # Dangling (ENOENT/ENOTDIR) or looping (ELOOP; WinError 1921) — anything else
        # (EACCES on the target's directory) is a read failure, not a missing target.
        unresolved = e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
        if stat.S_ISLNK(entry.st_mode) and (unresolved or getattr(e, "winerror", None) == 1921):
            raise BmadConfigError(
                _diagnostic_text(f"BMAD config layer is a symlink that resolves to no file: {path}")
            ) from e
        raise BmadConfigError(_diagnostic_text(f"cannot read {path}: {e}")) from e
    if not stat.S_ISREG(target.st_mode):
        raise BmadConfigError(_diagnostic_text(f"BMAD config layer is not a file: {path}"))
    try:
        # tomllib decodes as UTF-8 itself; a bad byte surfaces as UnicodeDecodeError
        # (a ValueError), not TOMLDecodeError — both are this layer's fault
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except UnicodeDecodeError as e:
        raise BmadConfigError(_diagnostic_text(f"{path} is not valid UTF-8: {e}")) from e
    except tomllib.TOMLDecodeError as e:
        raise BmadConfigError(_diagnostic_text(f"invalid TOML in {path}: {e}")) from e
    except OSError as e:
        raise BmadConfigError(_diagnostic_text(f"cannot read {path}: {e}")) from e


def _legacy_is_file(path: Path) -> bool:
    """`path.is_file()` for the legacy YAML, with its probe failures typed. A missing
    entry (or a dangling/looping link) is not a file, as `is_file()` always said;
    any other failure — an unreadable `_bmad/bmm`, a dead share — raises, where
    `is_file()` raises it untyped through 3.13 and reads it as absent on 3.14+,
    misreporting an unreadable fallback as a missing key."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return False
        raise BmadConfigError(_diagnostic_text(f"cannot read {path}: {e}")) from e


class _Table(dict[str, object]):
    """A table in the merged central config, with every layer that contributed to it
    — a table has no single source the way a leaf does."""

    def __init__(self, entries: dict[str, object], layers: tuple[Path, ...]) -> None:
        super().__init__(entries)
        self.layers = layers


def _overlay(base: dict[str, object], layer: dict[str, object], source: Path) -> dict[str, object]:
    """`structural_merge` for everything a path key can observe (module docstring):
    tables merge, any other value replaces, and each node remembers its layers."""
    merged = dict(base)
    for key, value in layer.items():
        below = merged.get(key)
        if isinstance(value, dict):
            if isinstance(below, _Table):
                merged[key] = _Table(_overlay(below, value, source), (*below.layers, source))
            else:
                merged[key] = _Table(_overlay({}, value, source), (source,))
        else:
            merged[key] = _Leaf(value, source)
    return merged


def _load_central(project: Path) -> dict[str, object] | None:
    """The merged four-layer central config, or None when no layer exists."""
    merged: dict[str, object] | None = None
    for rel in CENTRAL_LAYERS_REL:
        layer = _load_layer(project / rel)
        if layer is not None:
            merged = _overlay(merged or {}, layer, project / rel)
    return merged


def _find(tree: dict[str, object], key: str, prefix: str = "") -> list[tuple[str, object]]:
    """Every entry named `key`, depth-first in document order, as (dotted path, node).
    Arrays are opaque, as in `render_skill._find_config_values`."""
    found: list[tuple[str, object]] = []
    for name, node in tree.items():
        dotted = f"{prefix}.{name}" if prefix else name
        if name == key:
            found.append((dotted, node))
        if isinstance(node, dict):
            found.extend(_find(node, key, dotted))
    return found


def _central_value(central: dict[str, object], key: str) -> tuple[str, str] | None:
    """(value, origin) for `key` the way the renderer resolves a short config token,
    or None when the merged config has no entry of that name. Scalars are the
    renderer's candidates; an array or table under the name is not one, but it is a
    present non-string value, so it refuses rather than letting the YAML fill in."""
    found = _find(central, key)
    scalars = [
        (dotted, node)
        for dotted, node in found
        if isinstance(node, _Leaf) and not isinstance(node.value, list)
    ]
    if len(scalars) > 1:
        where = ", ".join(f"{dotted} ({leaf.layer})" for dotted, leaf in scalars)
        raise BmadConfigError(
            _diagnostic_text(
                f"ambiguous config value `{key}` found at: {where} — the BMAD renderer "
                "refuses the same config; keep exactly one entry of that name across "
                "the _bmad/ TOML layers"
            )
        )
    if scalars:
        dotted, leaf = scalars[0]
        assert isinstance(leaf, _Leaf)
        origin = f"`{dotted}` in {leaf.layer}"
        value = leaf.value
        if not isinstance(value, str):
            raise BmadConfigError(
                _diagnostic_text(f"{origin} must be a string, got {type(value).__name__}")
            )
        if not value.strip():
            raise BmadConfigError(_diagnostic_text(f"{origin} must not be empty"))
        return value, origin
    if found:
        dotted, node = found[0]
        if isinstance(node, _Leaf):
            origin = f"`{dotted}` in {node.layer}"
            kind = type(node.value).__name__
        else:
            assert isinstance(node, _Table)
            origin = f"`{dotted}` in {', '.join(str(layer) for layer in node.layers)}"
            kind = "table"
        raise BmadConfigError(_diagnostic_text(f"{origin} must be a string, got {kind}"))
    return None


def _load_legacy(config_path: Path) -> dict[object, object]:
    """The legacy `_bmad/bmm/config.yaml` mapping, raising typed on any fault."""
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
    return doc


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
    except (OSError, RuntimeError, ValueError) as e:
        message = (
            f"cannot canonicalize the project root {project}: {e} — artifact paths "
            "are derived from the canonical root, so no run can safely proceed. "
            "Run `bmad-loop validate` for what this host is doing."
        )
        raise BmadConfigError(_diagnostic_text(message)) from e
    config_path = project / LEGACY_CONFIG_REL
    central = _load_central(project)
    if central is None:
        if not _legacy_is_file(config_path):
            layers = ", ".join(str(project / rel) for rel in CENTRAL_LAYERS_REL)
            raise BmadConfigError(
                f"BMAD config not found: neither the central TOML ({layers}) nor "
                f"{config_path} exists (is BMAD installed here?)"
            )
        return _paths_from_legacy(project, config_path, _load_legacy(config_path))

    # Mixed or TOML-only: TOML wins per key; the YAML is read only when some key is
    # absent from the merged TOML, and only if it exists.
    legacy: dict[object, object] | None = None

    def lookup(key: str) -> tuple[str, str] | None:
        nonlocal legacy
        hit = _central_value(central, key)
        if hit is not None:
            return hit
        if legacy is None:
            legacy = _load_legacy(config_path) if _legacy_is_file(config_path) else {}
        raw = legacy.get(key)
        return (str(raw), f"`{key}` in {config_path}") if raw else None

    found = {key: lookup(key) for key in _PATH_KEYS}
    for key in _REQUIRED_KEYS:
        if found[key] is None:
            layers = ", ".join(str(project / rel) for rel in CENTRAL_LAYERS_REL)
            raise BmadConfigError(
                _diagnostic_text(
                    f"missing `{key}`: not in the central TOML ({layers}) nor in {config_path}"
                )
            )
    return _assemble(project, found)


def _paths_from_legacy(project: Path, config_path: Path, doc: dict[object, object]) -> ProjectPaths:
    """Today's YAML-only behavior, unchanged: a falsy value is an absent key."""
    impl = doc.get("implementation_artifacts")
    plan = doc.get("planning_artifacts")
    if not impl or not plan:
        raise BmadConfigError(
            f"{config_path} missing implementation_artifacts/planning_artifacts keys"
        )
    found: dict[str, tuple[str, str] | None] = {}
    for key in _PATH_KEYS:
        raw = doc.get(key)
        found[key] = (str(raw), f"`{key}` in {config_path}") if raw else None
    return _assemble(project, found)


def _assemble(project: Path, found: dict[str, tuple[str, str] | None]) -> ProjectPaths:
    """Canonicalize every member of the snapshot; `found` maps each key to its
    (raw value, origin) or None, and the two required keys are never None here."""
    impl, plan = found["implementation_artifacts"], found["planning_artifacts"]
    assert impl is not None and plan is not None
    repo_root_hit, out_hit = found["repo_root"], found["output_folder"]
    repo_root = _resolve(repo_root_hit[0], project, repo_root_hit[1]) if repo_root_hit else project
    output_folder = (
        _resolve(out_hit[0], project, out_hit[1])
        if out_hit
        # the default branch is a bare join off the (canonical) root and takes the
        # same canonicalize-or-raise treatment as a configured string: an in-tree
        # junction under the default name misclassifies exactly like a configured one.
        else _canonical(project / "_bmad-output", "default output folder")
    )
    return ProjectPaths(
        project=project,
        implementation_artifacts=_resolve(impl[0], project, impl[1]),
        planning_artifacts=_resolve(plan[0], project, plan[1]),
        output_folder=output_folder,
        repo_root=repo_root,
    )
