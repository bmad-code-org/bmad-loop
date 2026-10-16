"""Where a BMAD v7 ticket store lives relative to the code repo, and what a
tickets run must do about it.

``tickets.py`` resolves the store as ``output_folder`` joined onto the project root
(an absolute value stays put), and every plan a build writes lands under the
active initiative there. Whether that store is a folder the code checkout tracks,
an ignored folder, a repository of its own or a folder outside the project decides
where bmad-loop may mark a plan ``done`` and what a worktree mount has to see.

Layouts (:data:`Layout`):

- **T1** — the initiative is tracked inside the code checkout. Each worktree has
  its own copy, and a plan marked in the commit window rides the unit's commit.
- **T2** — the store is an ignored plain folder inside the code checkout.
  Unversioned, and shared: a worktree mount has no copy of it.
- **T3** — the store is its own git repository nested inside the code checkout
  (ignored by it).
- **T4** — the store is its own git repository outside the code checkout.
- **T6** — the store is a plain folder outside the code checkout.
- **T5** (refused, deferred) — a workspace: the project root is not a repository
  and the code lives in sibling repositories.

A store no checkout carries (T2–T4, T6) is SHARED: under worktree isolation the
mount's highest config layer, ``_bmad/custom/config.user.toml``, is rewritten to
point ``[core] output_folder`` at the main checkout's store
(:func:`redirect_mount_store`), so ``tickets.py`` and the renderer inside the mount
resolve the one store every session reads and writes.

Two parts. The PURE part (pyright-strict) holds the data — :class:`StoreFacts`,
:class:`StoreTopology`, :class:`StoreRefusal` — the classifier
:func:`classify_store`, :func:`store_topology_conflict` (the sole producer of the
refusal text, the :func:`bmadconfig.worktree_isolation_conflict` pattern) and the
config rewrite :func:`redirected_user_config_text`. The PROBE part reads git and
the disk: :func:`probe_store_facts` (every git call through ``verify``'s
``_run_git`` chokepoint), :func:`assess_store` and :func:`redirect_mount_store`.

A store with its own repository (T3/T4) is committed by :class:`StoreCommitter`:
the marked plan (and the ledger or retro document the run wrote there) alone, by
pathspec, never the planning edits a human has in flight beside it."""

from __future__ import annotations

import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast, get_args

from . import bmadconfig, platform_util, verify
from .platform_util import atomic_write_bytes

Layout = Literal["T1", "T2", "T3", "T4", "T6"]
Placement = Literal["checkout", "shared-in-project", "shared-outside"]
Versioning = Literal["code-repo", "store-repo", "none"]
RefusalCode = Literal[
    "partial-tracking",
    "store-not-ignored",
    "tracked-outside-project",
    "tracked-outside-checkout",
    "user-config-tracked",
    "user-config-not-ignored",
    "workspace-unsupported",
    "probe-failed",
]

# The upstream config layer the redirect rewrites: the highest of the three
# `tickets.py` merges, personal and gitignored by `_bmad/custom/.gitignore`.
USER_CONFIG_REL = Path("_bmad") / "custom" / "config.user.toml"

_VERSIONING: dict[Layout, Versioning] = {
    "T1": "code-repo",
    "T2": "none",
    "T3": "store-repo",
    "T4": "store-repo",
    "T6": "none",
}
_TOPOLOGY_KEYS = frozenset({"layout", "placement", "versioning", "store_root", "store_repo"})

# Which validate check reports each refusal.
_REFUSAL_CHECKS: dict[RefusalCode, str] = {
    "partial-tracking": "tickets.tree-untracked",
    "store-not-ignored": "tickets.tree-untracked",
    "tracked-outside-project": "tickets.store-topology",
    "tracked-outside-checkout": "tickets.store-topology",
    "user-config-tracked": "tickets.own-repo-isolation",
    "user-config-not-ignored": "tickets.own-repo-isolation",
    "workspace-unsupported": "tickets.workspace-unsupported",
    "probe-failed": "tickets.store-topology",
}


class TicketStoreError(Exception):
    """The worktree redirect could not be written or did not take."""


class StoreCommitError(Exception):
    """A store-repository commit could not be made: git, the disk or the store
    lock failed. Bookkeeping, never a repair write — the caller journals it."""


@dataclass(frozen=True)
class StoreFacts:
    """What the probe saw. ``tracked`` pairs each tree marker that exists (the
    initiative ``tickets.toml``, each epic's ``tickets.toml`` and ``epic-*.md``)
    as a posix path relative to the code repo's toplevel with whether the code
    repo tracks it;
    empty when the store is not in the code repo. ``ignored`` is whether the code
    repo ignores the initiative folder — or, for a store repository nested in the
    code repo, that repository's root: git does not descend into an unignored
    nested repository, so it shows as untracked dirt whatever ignores the
    initiative inside it. The two ``user_config_*`` facts are about
    :data:`USER_CONFIG_REL` in the code repo, which the redirect rewrites."""

    project: Path
    repo_root: Path
    store_root: Path
    initiative: Path
    project_is_repo: bool
    repo_toplevel: Path | None
    store_toplevel: Path | None
    tracked: tuple[tuple[str, bool], ...]
    ignored: bool
    user_config_tracked: bool
    user_config_ignored: bool


@dataclass(frozen=True)
class StoreTopology:
    """A classified store. ``store_root`` is the store ``tickets.py`` resolves
    (``output_folder``); ``store_repo`` is the store's own repository (T3/T4)."""

    layout: Layout
    placement: Placement
    versioning: Versioning
    store_root: Path
    store_repo: Path | None = None

    def per_worktree(self) -> bool:
        """True when each checkout carries its own copy of the tree (T1)."""
        return self.placement == "checkout"

    def needs_redirect(self, isolation: str) -> bool:
        """True when a worktree mount must be pointed at the main store."""
        return isolation == "worktree" and not self.per_worktree()

    def to_dict(self) -> dict[str, str | None]:
        return {
            "layout": self.layout,
            "placement": self.placement,
            "versioning": self.versioning,
            "store_root": str(self.store_root),
            "store_repo": None if self.store_repo is None else str(self.store_repo),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> StoreTopology:
        """The inverse of :meth:`to_dict`; raises ValueError on any other shape."""
        if frozenset(data) != _TOPOLOGY_KEYS:
            raise ValueError(f"store topology keys {sorted(data)} != {sorted(_TOPOLOGY_KEYS)}")
        layout, placement = data["layout"], data["placement"]
        versioning, store_root, store_repo = (
            data["versioning"],
            data["store_root"],
            data["store_repo"],
        )
        if layout not in get_args(Layout) or not isinstance(layout, str):
            raise ValueError(f"unknown store layout {layout!r}")
        if placement not in get_args(Placement) or not isinstance(placement, str):
            raise ValueError(f"unknown store placement {placement!r}")
        if versioning != _VERSIONING[_as_layout(layout)]:
            raise ValueError(f"store versioning {versioning!r} does not match layout {layout}")
        if not isinstance(store_root, str) or not store_root:
            raise ValueError(f"store_root must be a non-empty string: {store_root!r}")
        if store_repo is not None and (not isinstance(store_repo, str) or not store_repo):
            raise ValueError(f"store_repo must be a non-empty string or null: {store_repo!r}")
        if (store_repo is not None) != (versioning == "store-repo"):
            raise ValueError(f"store_repo {store_repo!r} does not match layout {layout}")
        return cls(
            layout=_as_layout(layout),
            placement=_as_placement(placement),
            versioning=_VERSIONING[_as_layout(layout)],
            store_root=Path(store_root),
            store_repo=None if store_repo is None else Path(store_repo),
        )


@dataclass(frozen=True)
class StoreRefusal:
    """A store layout tickets runs refuse. ``path`` is what the text names."""

    code: RefusalCode
    path: Path
    detail: str = ""

    @property
    def check(self) -> str:
        """The validate check id that reports this refusal."""
        return _REFUSAL_CHECKS[self.code]


def _as_layout(value: str) -> Layout:
    for layout in get_args(Layout):
        if value == layout:
            return layout
    raise ValueError(f"unknown store layout {value!r}")


def _as_placement(value: str) -> Placement:
    for placement in get_args(Placement):
        if value == placement:
            return placement
    raise ValueError(f"unknown store placement {value!r}")


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def classify_store(facts: StoreFacts, isolation: str) -> StoreTopology | StoreRefusal:
    """The layout ``facts`` describe under ``[scm] isolation``, or why it is refused.

    - The project is not a repository: a workspace (T5), deferred.
    - The store is its own repository: T3 when nested in the code checkout (which
      must ignore it — an unignored nested repository is untracked dirt, ``dir/``
      in ``git status``, and build-auto halts on it), T4 outside it.
    - The store is in no repository: T6.
    - The store is in the code repository: every tree marker tracked is T1 (only
      inside ``repo_root``, the checkout bmad-loop commits in, and under worktree
      isolation only inside the project, where the mount carries it); none
      tracked and the initiative ignored is T2; anything else — some markers
      tracked, or none tracked and not ignored — is refused.

    A shared layout under worktree isolation needs the redirect, so it also needs
    :data:`USER_CONFIG_REL` untracked and gitignored: the rewrite must neither ride
    a commit nor dirty the mount."""
    initiative = facts.initiative
    if not facts.project_is_repo or facts.repo_toplevel is None:
        return StoreRefusal("workspace-unsupported", facts.project)
    in_checkout = _within(initiative, facts.repo_root)
    in_project = _within(facts.store_root, facts.project)
    shared: Placement = "shared-in-project" if in_project else "shared-outside"
    result: StoreTopology | StoreRefusal
    if facts.store_toplevel is not None and facts.store_toplevel != facts.repo_toplevel:
        if in_checkout and not facts.ignored:
            return StoreRefusal("store-not-ignored", facts.store_toplevel, "a nested repository")
        result = StoreTopology(
            "T3" if in_checkout else "T4",
            shared,
            "store-repo",
            facts.store_root,
            facts.store_toplevel,
        )
    elif facts.store_toplevel is None:
        result = StoreTopology("T6", shared, "none", facts.store_root)
    else:
        states = [tracked for _, tracked in facts.tracked]
        if states and all(states):
            if not in_checkout:
                return StoreRefusal("tracked-outside-checkout", initiative, str(facts.repo_root))
            if isolation == "worktree" and not _within(initiative, facts.project):
                return StoreRefusal("tracked-outside-project", initiative)
            return StoreTopology("T1", "checkout", "code-repo", facts.store_root)
        if any(states):
            untracked = ", ".join(rel for rel, tracked in facts.tracked if not tracked)
            return StoreRefusal("partial-tracking", initiative, untracked)
        if not facts.ignored:
            return StoreRefusal("store-not-ignored", initiative, "an untracked folder")
        result = StoreTopology("T2", shared, "none", facts.store_root)
    if result.needs_redirect(isolation):
        if facts.user_config_tracked:
            return StoreRefusal("user-config-tracked", facts.project / USER_CONFIG_REL)
        if not facts.user_config_ignored:
            return StoreRefusal("user-config-not-ignored", facts.project / USER_CONFIG_REL)
    return result


def store_topology_conflict(
    result: StoreTopology | StoreRefusal, *, recorded: Mapping[str, object] | None = None
) -> str | None:
    """The refusal message for a store tickets runs cannot drive, or None.

    Refused: every :class:`StoreRefusal`, and a topology that differs from the one
    a run ``recorded`` at its start (resume re-validates — a store that moved under
    a running ticket tree would mark plans the run never read). Every classified
    layout runs: T1 is marked inside the commit window, the shared layouts after the
    code lands.

    Sole producer of the text, shared by validate, the run/sweep/resume/resolve
    preflights, the dry-run banner, the auto-sweep factory and the TUI guards."""
    if isinstance(result, StoreRefusal):
        return _refusal_text(result)
    if recorded is not None and result.to_dict() != dict(recorded):
        was = recorded.get("layout")
        return (
            f"the ticket store changed since this run started: it was {was} "
            f"({recorded.get('store_root')}) and is now {result.layout} "
            f"({result.store_root}) — a run marks plans only in the store it started "
            "on. Restore the store layout, or start a new run."
        )
    return None


def _describe(topology: StoreTopology) -> str:
    where = {
        "checkout": "tracked inside the code checkout",
        "shared-in-project": "inside the project, untracked by the code repo",
        "shared-outside": "outside the code checkout",
    }[topology.placement]
    kept = {
        "code-repo": "versioned by the code repo",
        "store-repo": f"versioned in its own repository {topology.store_repo}",
        "none": "unversioned",
    }[topology.versioning]
    return f"{where}, {kept}"


def describe_topology(topology: StoreTopology) -> str:
    """One line naming a classified layout, for validate's ok line."""
    return f"{topology.layout}: {topology.store_root} ({_describe(topology)})"


def _refusal_text(refusal: StoreRefusal) -> str:
    path, code = refusal.path, refusal.code
    if code == "workspace-unsupported":
        return (
            f"the project {path} is not a git repository — a BMAD workspace (the "
            "install and store at a folder holding several code repositories) is "
            "not supported by tickets runs yet; run from a project that is its own "
            "code repository"
        )
    if code == "partial-tracking":
        return (
            f"the initiative folder {path} is only partly tracked by the code "
            f"repository (untracked: {refusal.detail}) — commit the whole initiative "
            "folder, or gitignore all of it"
        )
    if code == "store-not-ignored" and refusal.detail == "a nested repository":
        return (
            f"the store repository {path} is nested inside the code checkout, which "
            "does not ignore it — it shows as untracked dirt and bmad-build-auto "
            "halts on an unclean tree; gitignore the store repository's root folder"
        )
    if code == "store-not-ignored":
        return (
            f"the initiative folder {path} is {refusal.detail} inside the code "
            "checkout that the code repository neither tracks nor ignores — it shows "
            "as untracked dirt and bmad-build-auto halts on an unclean tree; commit "
            "it, or gitignore the store"
        )
    if code == "tracked-outside-project":
        return (
            f'isolation = "worktree" needs a tracked initiative folder inside the '
            f"project: {path} is tracked by the code repository but lies outside the "
            "project, so a unit worktree would mark the main checkout's copy, not "
            "its own; move the store under the project, or set "
            '`isolation = "none"` under [scm] in .bmad-loop/policy.toml'
        )
    if code == "tracked-outside-checkout":
        return (
            f"the initiative folder {path} is tracked by the code repository but lies "
            f"outside repo_root ({refusal.detail}), the checkout bmad-loop commits in — "
            "the commits it makes by path skip a plan marked there; point repo_root at "
            "the repository root, or move the store under repo_root"
        )
    if code in ("user-config-tracked", "user-config-not-ignored"):
        state = "tracked by" if code == "user-config-tracked" else "not gitignored in"
        return (
            f'isolation = "worktree" points each unit worktree at the shared ticket '
            f"store by rewriting {path}, which is {state} the code repository — the "
            "rewrite would show up as a change in the unit's commit. Untrack it and "
            "keep `*.user.toml` in _bmad/custom/.gitignore (as `bmad setup` writes "
            'it), or set `isolation = "none"`'
        )
    return f"cannot classify the ticket store at {path}: {refusal.detail}"


_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(?:#.*)?$")
_OUTPUT_FOLDER_RE = re.compile(r"^\s*output_folder\s*=")


def _toml_basic_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def redirected_user_config_text(text: str, main_store: Path) -> str:
    """``text`` (a ``config.user.toml``) with ``[core] output_folder`` set to
    ``main_store``, every other byte kept: any ``output_folder`` line in the
    ``[core]`` table is dropped and the new one written right under its header, or
    a ``[core]`` table is appended. A CRLF file stays CRLF.

    Re-parsed before it is returned: a shape this line edit cannot reach — an
    inline ``core = {…}`` table, dotted ``core.x`` keys — fails the parse or the
    value check and raises :class:`TicketStoreError`, never a silently wrong
    redirect."""
    newline = "\r\n" if "\r\n" in text else "\n"
    line = f"output_folder = {_toml_basic_string(str(main_store))}"
    out: list[str] = []
    section: str | None = None
    inserted = False
    for raw in text.splitlines(keepends=True):
        header = _SECTION_RE.match(raw)
        if header:
            section = header.group("name").strip()
            out.append(raw)
            if section == "core" and not inserted:
                if not raw.endswith(("\n", "\r")):
                    out[-1] = raw + newline
                out.append(line + newline)
                inserted = True
            continue
        if section == "core" and _OUTPUT_FOLDER_RE.match(raw):
            continue
        out.append(raw)
    if not inserted:
        if out and not out[-1].endswith(("\n", "\r")):
            out.append(newline)
        out.append(f"{newline if out else ''}[core]{newline}{line}{newline}")
    result = "".join(out)
    try:
        parsed = tomllib.loads(result)
    except tomllib.TOMLDecodeError as e:
        raise TicketStoreError(f"{USER_CONFIG_REL.as_posix()} cannot take the redirect: {e}") from e
    core: object = parsed.get("core")
    value = cast("dict[str, object]", core).get("output_folder") if isinstance(core, dict) else None
    if value != str(main_store):
        raise TicketStoreError(
            f"{USER_CONFIG_REL.as_posix()} cannot take the redirect: [core] output_folder "
            f"reads {value!r} after the rewrite"
        )
    return result


# --- probe part -------------------------------------------------------------


def _has_mode(path: Path, kind: int) -> bool:
    """Whether ``path`` exists with file type ``kind`` (``stat.S_IFDIR``,
    ``stat.S_IFREG``); False only when it is absent or of another type. Any other
    stat fault raises ``verify.GitError`` — ``Path.is_dir``/``is_file`` read it as
    False from Python 3.14, which would drop a marker from the tracking check."""
    try:
        return stat.S_IFMT(path.stat().st_mode) == kind
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        raise verify.GitError(f"cannot stat {path}: {e}") from e


def _markers(initiative: Path) -> list[Path]:
    """The tree files whose tracking decides T1: the initiative ``tickets.toml``
    and each epic folder's ``tickets.toml`` and ``epic-*.md``, those that exist.
    Raises ``verify.GitError`` when the initiative cannot be listed or a candidate
    cannot be statted: an incomplete list could read a partly tracked tree as T1."""
    found = [initiative / "tickets.toml"]
    try:
        children = sorted(initiative.iterdir())
    except OSError as e:
        raise verify.GitError(f"cannot list the initiative folder {initiative}: {e}") from e
    for child in children:
        if child.name.startswith("epic-") and _has_mode(child, stat.S_IFDIR):
            found += [child / "tickets.toml", child / f"{child.name}.md"]
    return [path for path in found if _has_mode(path, stat.S_IFREG)]


def probe_store_facts(paths: bmadconfig.ProjectPaths, initiative: Path) -> StoreFacts:
    """Read the facts :func:`classify_store` decides on. Raises ``verify.GitError``
    when git cannot answer or the initiative cannot be read."""
    project, repo_root = paths.project.resolve(), paths.repo_root.resolve()
    store_root, initiative = paths.output_folder.resolve(), initiative.resolve()
    repo_toplevel = verify.git_toplevel(repo_root)
    project_toplevel = verify.git_toplevel(project)
    store_toplevel = verify.git_toplevel(
        initiative if _has_mode(initiative, stat.S_IFDIR) else store_root
    )
    tracked: list[tuple[str, bool]] = []
    if repo_toplevel is not None and store_toplevel == repo_toplevel:
        # Against the toplevel, not repo_root: a nested repo_root need not hold the
        # store (classify_store refuses that once it knows the store is tracked).
        for marker in _markers(initiative):
            rel = marker.relative_to(repo_toplevel).as_posix()
            tracked.append((rel, verify.path_tracked(repo_toplevel, rel)))
    user_config = project / USER_CONFIG_REL
    try:
        user_rel = user_config.relative_to(repo_root).as_posix()
    except ValueError:
        user_rel = None
    in_repo = repo_toplevel is not None  # nothing below can ask a non-repository
    # A nested store repository is ignored only if its ROOT is: git lists an
    # unignored one as `dir/` without descending, so a rule over the initiative
    # inside it hides nothing (see StoreFacts.ignored).
    nested = store_toplevel is not None and store_toplevel != repo_toplevel
    ignore_probe = store_toplevel if nested and store_toplevel is not None else initiative
    return StoreFacts(
        project=project,
        repo_root=repo_root,
        store_root=store_root,
        initiative=initiative,
        project_is_repo=project_toplevel is not None,
        repo_toplevel=repo_toplevel,
        store_toplevel=store_toplevel,
        tracked=tuple(tracked),
        ignored=in_repo and verify.path_ignored(repo_root, ignore_probe),
        user_config_tracked=(
            in_repo and user_rel is not None and verify.path_tracked(repo_root, user_rel)
        ),
        user_config_ignored=in_repo and verify.path_ignored(repo_root, user_config),
    )


def assess_store(
    paths: bmadconfig.ProjectPaths, isolation: str
) -> StoreTopology | StoreRefusal | None:
    """Probe and classify the store of ``paths`` (loaded for tickets mode). None
    when there is no active initiative folder to classify — the tickets preflight
    reports that on its own (``tickets.initiative-unset`` / ``-missing``). A git
    fault — or an initiative folder that cannot be statted — is a ``probe-failed``
    refusal, never a guess."""
    initiative = paths.initiative_folder
    if paths.era != "v7" or initiative is None:
        return None
    try:
        if not _has_mode(initiative, stat.S_IFDIR):
            return None
        facts = probe_store_facts(paths, initiative)
    except verify.GitError as e:
        return StoreRefusal("probe-failed", initiative, str(e))
    return classify_store(facts, isolation)


def redirect_mount_store(mount_project: Path, main_store: Path) -> None:
    """Point the worktree mount at ``mount_project`` to the main checkout's store:
    rewrite its :data:`USER_CONFIG_REL` (created when absent) so ``[core]
    output_folder`` is ``main_store``, then read the merged config back the way
    ``tickets.py`` does. Raises :class:`TicketStoreError` when the file cannot be
    rewritten or the merged value is not ``main_store``."""
    path = mount_project / USER_CONFIG_REL
    try:
        try:  # only absence reads as empty: Path.is_file folds any stat fault to False
            text = path.read_bytes().decode("utf-8")
        except (FileNotFoundError, NotADirectoryError):
            text = ""
        data = redirected_user_config_text(text, main_store).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, data)
        merged = bmadconfig.read_core_value(mount_project, "output_folder")
    except (OSError, UnicodeDecodeError, bmadconfig.BmadConfigError) as e:
        raise TicketStoreError(f"cannot redirect {path} to {main_store}: {e}") from e
    if merged is None or merged[0] != str(main_store):
        raise TicketStoreError(
            f"redirected {path}, but the merged [core] output_folder reads "
            f"{None if merged is None else merged[0]!r}, not {main_store}"
        )


def sweep_store_source(paths: bmadconfig.ProjectPaths, stories_source: str) -> str:
    """The source whose store a sweep works, for the store-topology refusal. On v7
    the ledger lives in the initiative folder of the ticket store, whatever
    ``[stories] source`` (``stories_source``) says, so a v7 sweep is judged as a
    tickets run. A sweep persists no source of its own."""
    return "tickets" if paths.era == "v7" else stories_source


def store_conflict(
    project: Path, source: str, isolation: str, *, recorded: Mapping[str, object] | None = None
) -> str | None:
    """The store-topology refusal for a ``source`` run of ``project`` under
    ``isolation``, or None — the predicate every run-arming surface calls (and
    ``cli._reject_store_topology`` wraps). None outside tickets mode. Loads the
    paths itself with ``want_tickets``, so a caller holding v6-era paths of a mixed
    install still classifies the v7 store. ``recorded`` is the run's
    ``RunState.store_topology`` on resume (empty or None: nothing recorded)."""
    if source != "tickets":
        return None
    try:
        paths = bmadconfig.load_paths(project, want_tickets=True)
    except bmadconfig.BmadConfigError as e:
        return f"cannot read the BMAD config to classify the ticket store: {e}"
    result = assess_store(paths, isolation)
    if result is None:
        return None
    return store_topology_conflict(result, recorded=recorded or None)


def with_shared_store(
    paths: bmadconfig.ProjectPaths, recorded: Mapping[str, object]
) -> bmadconfig.ProjectPaths:
    """``paths`` with :attr:`~bmadconfig.ProjectPaths.shared_store` set from a run's
    recorded topology when no checkout carries the store, else ``paths`` as is.
    Raises ValueError on a recorded topology that does not parse."""
    if not recorded:
        return paths
    topology = StoreTopology.from_dict(recorded)
    if topology.per_worktree():
        return paths
    return replace(paths, shared_store=topology.store_root)


# --- store-repository commits ------------------------------------------------


@dataclass(frozen=True)
class StoreCommit:
    """What :meth:`StoreCommitter.commit` did. ``sha`` is the new store commit,
    None when nothing was committed: the paths held no change, or (``foreign``
    non-empty) the store's index holds a staged version of one of them that the
    commit would have destroyed — those repo-relative paths, and nothing was
    staged."""

    sha: str | None
    foreign: tuple[str, ...] = ()


def code_trailers(code_repo: Path, commit: str | None, baseline: str | None = None) -> list[str]:
    """The trailers tying a store commit to the code it describes:
    ``Bmad-Code-Repo`` always, ``Bmad-Code-Commit`` with a commit, and
    ``Bmad-Code-Range: <baseline>..<commit>`` with both — the range survives a
    later squash of the code repository."""
    trailers = [f"Bmad-Code-Repo: {code_repo.as_posix()}"]
    if commit:
        trailers.append(f"Bmad-Code-Commit: {commit}")
        if baseline:
            trailers.append(f"Bmad-Code-Range: {baseline}..{commit}")
    return trailers


class StoreCommitter:
    """Commits a tickets run's own writes in a store repository (T3/T4).

    ``store_repo`` is the store's own repository (``StoreTopology.store_repo``),
    ``code_repo`` the code checkout the run builds in."""

    def __init__(self, store_repo: Path, code_repo: Path) -> None:
        self.store_repo = store_repo
        self.code_repo = code_repo

    def commit(
        self,
        story_key: str,
        paths: list[Path],
        message: str,
        *,
        code_commit: str | None = None,
        code_baseline: str | None = None,
    ) -> StoreCommit:
        """Commit exactly ``paths`` in the store repository (``verify.commit_paths``:
        pathspec only, every other change in the store left as it is) under
        ``message`` plus the :func:`code_trailers`, holding the store's lock
        (``runs.lock_path_for(<store_repo>/.git)``) so two runs over one store
        never interleave a stage and a commit.

        Before anything is staged, each path's index entry is checked with
        ``verify.index_holds_no_foreign_content`` against the bytes on disk (or,
        for a path absent from disk, the deletion its stage would record): a
        staged version distinct from both HEAD and the file (a human's ``git add``
        in progress) would be overwritten by the stage, so the commit is skipped
        and those paths are returned in :attr:`StoreCommit.foreign`. Raises
        :class:`StoreCommitError` naming ``story_key`` on any git, disk or lock
        fault."""
        from . import runs  # lazy: runs imports most of the package

        repo = self.store_repo
        body = (
            message.rstrip()
            + "\n\n"
            + "\n".join(code_trailers(self.code_repo, code_commit, code_baseline))
        )
        try:
            with platform_util.file_lock(runs.lock_path_for(repo / ".git")):
                foreign = self._foreign(paths)
                if foreign:
                    return StoreCommit(None, foreign)
                return StoreCommit(verify.commit_paths(repo, body, paths))
        except (verify.GitError, OSError, RuntimeError, ValueError, runs.StateRootError) as e:
            raise StoreCommitError(f"store commit for {story_key} in {repo} failed: {e}") from e

    def _foreign(self, paths: list[Path]) -> tuple[str, ...]:
        root = self.store_repo.resolve()
        foreign: list[str] = []
        for path in paths:
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue  # outside the store: `commit_paths` ignores it too
            try:
                data: bytes | None = path.read_bytes()
            except FileNotFoundError:
                data = None  # a deletion to stage: a staged version would be lost too
            if not verify.index_holds_no_foreign_content(self.store_repo, rel, data):
                foreign.append(rel)
        return tuple(foreign)
