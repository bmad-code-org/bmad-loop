"""The one place that answers "where is skill X for this CLI?".

Every skill question — validate, the run preflight, dispatch, worktree seeding,
the render probe and the review-lens reads — resolves a skill through
:func:`locate` over a :class:`SkillScope`, so the copy a check reads is the copy
the session will load.

Doctrine:

- **Off means project-only.** With ``[skills] user_roots = false`` (the
  default) a scope resolves only from the project trees (the profile's
  ``skill_tree`` then its ``extra_skill_trees``), exactly as before user roots
  existed. :func:`shadow` and :func:`home_copy` still look at user roots with
  the switch off, but only to warn and hint, never to resolve.
- **Roots are what each CLI natively loads.** A profile declares, in
  ``[user_skills]``, the home-relative folders its CLI reads on its own and in
  which order, plus the vendor variables that turn a folder off
  (``disabled_by``). Nothing here invents a root a CLI would not read.
- **Validate must not pass what the session cannot load.** A root whose
  disabler is set, whose home is unknown, or which cannot be read is carried as
  *unavailable* and never resolves; precedence picks the copy the CLI itself
  would pick (``user``: personal beats project; ``project`` and ``unordered``:
  the project copy is checked, and ``unordered`` clashes are reported by
  :func:`shadow`).

Pure stdlib with no ``bmad_loop`` imports: the profile-side data types live
here so ``adapters/profile.py`` imports from this module, never the reverse.
The two seams :func:`user_home` and :func:`ambient_environ` are pinned by the
test suite's conftest so no test reads the real home directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

PRECEDENCES: frozenset[str] = frozenset({"project", "user", "unordered"})

Origin = Literal["project", "user"]


@dataclass(frozen=True)
class UserSkillRoot:
    """One declared user root: a ``~/``-prefixed folder and the environment
    variables whose presence stops the CLI reading it."""

    path: str
    disabled_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserSkillsSpec:
    """A profile's ``[user_skills]`` table: which copy wins on a name clash
    (one of :data:`PRECEDENCES`) and the roots in the order the CLI reads them."""

    precedence: str = "project"
    roots: tuple[UserSkillRoot, ...] = ()


@dataclass(frozen=True)
class UserRoot:
    """A declared root resolved against a home directory and an environment.

    ``unavailable`` is ``""`` when the root can be read, else why not:
    ``"env:<VAR>"`` (a disabler is set), ``"no-home"`` or ``"unreadable"``.
    ``path`` is ``None`` only for ``"no-home"``."""

    spelling: str
    path: Path | None
    unavailable: str = ""


@dataclass(frozen=True)
class SkillScope:
    """Everything needed to resolve a skill for one CLI. Frozen and hashable,
    so it serves as a dedupe or cache key."""

    tree: str
    extra_trees: tuple[str, ...] = ()
    user_roots: tuple[UserRoot, ...] = ()
    precedence: str = "project"
    resolve_user: bool = False

    def project_trees(self) -> tuple[str, ...]:
        return (self.tree, *self.extra_trees)


@dataclass(frozen=True)
class Located:
    """One present copy of a skill: ``path`` is its folder (holding
    ``SKILL.md``), ``root`` the display spelling of the tree or user root it
    sits in, and ``primary`` whether that is the profile's ``skill_tree``."""

    name: str
    path: Path
    origin: Origin
    root: str
    primary: bool

    @property
    def display(self) -> str:
        return f"{self.root}/{self.name}"


@dataclass(frozen=True)
class Shadow:
    """A name clash between the project copy and a user copy. ``checked`` is
    the copy a project-only check reads; ``other`` the copy that may run
    instead (``overrides``) or alongside it in no stated order (``competes``)."""

    name: str
    checked: Located
    other: Located
    kind: Literal["overrides", "competes"]


class SkillProfile(Protocol):
    """The profile fields a scope is built from (``CLIProfile`` satisfies it)."""

    @property
    def skill_tree(self) -> str: ...

    @property
    def extra_skill_trees(self) -> tuple[str, ...]: ...

    @property
    def user_skills(self) -> UserSkillsSpec | None: ...

    @property
    def env(self) -> Mapping[str, str]: ...


class _Default(Enum):
    SEAM = "seam"


_SEAM = _Default.SEAM


def user_home() -> Path | None:
    """The user's home directory, or ``None`` when it cannot be determined."""
    try:
        return Path.home()
    except RuntimeError:
        return None


def ambient_environ() -> Mapping[str, str]:
    """bmad-loop's own environment, read for root disablers."""
    return os.environ


def as_scope(tree: str | SkillScope) -> SkillScope:
    """A plain tree string is a project-only scope over that one tree."""
    return tree if isinstance(tree, SkillScope) else SkillScope(tree)


def _user_root(root: UserSkillRoot, home: Path | None, environ: Mapping[str, str]) -> UserRoot:
    if home is None:
        return UserRoot(root.path, None, "no-home")
    rest = root.path[2:]
    path = home.joinpath(*[part for part in rest.split("/") if part])
    for var in root.disabled_by:
        if var in environ:
            return UserRoot(root.path, path, f"env:{var}")
    try:
        path.is_dir()
    except OSError:
        return UserRoot(root.path, path, "unreadable")
    return UserRoot(root.path, path)


def build_scope(
    tree: str,
    *,
    extra_trees: Iterable[str] = (),
    user_skills: UserSkillsSpec | None = None,
    resolve_user: bool = False,
    home: Path | None | Literal[_Default.SEAM] = _SEAM,
    environ: Mapping[str, str] | None = None,
) -> SkillScope:
    """A scope over ``tree`` + ``extra_trees`` and the declared user roots.

    ``home`` defaults to :func:`user_home` and ``environ`` to
    :func:`ambient_environ`, both read at call time. A disabler counts as set
    when the variable is present at all, even empty: an unavailable root is the
    conservative answer, since validate must not pass a copy the CLI skips."""
    if home is _SEAM:
        home = user_home()
    env = ambient_environ() if environ is None else environ
    spec = user_skills or UserSkillsSpec()
    return SkillScope(
        tree=tree,
        extra_trees=tuple(extra_trees),
        user_roots=tuple(_user_root(r, home, env) for r in spec.roots),
        precedence=spec.precedence,
        resolve_user=resolve_user,
    )


def scope_for(
    profile: SkillProfile,
    *,
    resolve_user: bool,
    home: Path | None | Literal[_Default.SEAM] = _SEAM,
    environ: Mapping[str, str] | None = None,
    session_env: Mapping[str, str] | None = None,
) -> SkillScope:
    """The scope ``profile``'s sessions load skills from.

    The disabler environment is ``environ`` (default :func:`ambient_environ`)
    overlaid with the profile's own ``env``, which every session is launched
    with, then ``session_env`` — one session's own env after its plugin hooks,
    in the adapters' launch order. Without it the scope is the one every
    session of the profile shares, which a ``pre_session`` hook setting a
    disabler (an account-rotation ``CLAUDE_CONFIG_DIR``) can still narrow."""
    base = ambient_environ() if environ is None else environ
    return build_scope(
        profile.skill_tree,
        extra_trees=profile.extra_skill_trees,
        user_skills=profile.user_skills,
        resolve_user=resolve_user,
        home=home,
        environ={**base, **profile.env, **(session_env or {})},
    )


def _present(folder: Path) -> bool:
    try:
        return (folder / "SKILL.md").is_file()
    except OSError:
        return False


def _project_copies(project: Path, scope: SkillScope, name: str) -> list[Located]:
    found: list[Located] = []
    for i, tree in enumerate(scope.project_trees()):
        folder = project / tree / name
        if _present(folder):
            found.append(Located(name, folder, "project", tree, i == 0))
    return found


def _user_copies(scope: SkillScope, name: str) -> list[Located]:
    found: list[Located] = []
    for root in scope.user_roots:
        if root.unavailable or root.path is None:
            continue
        folder = root.path / name
        if _present(folder):
            found.append(Located(name, folder, "user", root.spelling, False))
    return found


def copies(project: Path, scope: SkillScope, name: str) -> list[Located]:
    """Every present copy of ``name``: the primary tree, the extra trees, then
    the available user roots in declared order. A copy whose folder cannot be
    read is absent."""
    return [*_project_copies(project, scope, name), *_user_copies(scope, name)]


def locate(project: Path, scope: SkillScope, name: str) -> Located | None:
    """The copy of ``name`` the CLI loads, or ``None``.

    Switch off: the first project copy. Switch on: under ``user`` precedence a
    user copy beats the project copy; under ``project`` and ``unordered`` the
    project copy is preferred and a user copy fills in only when it is absent."""
    project_found = _project_copies(project, scope, name)
    if not scope.resolve_user:
        return project_found[0] if project_found else None
    user_found = _user_copies(scope, name)
    first, second = (
        (user_found, project_found) if scope.precedence == "user" else (project_found, user_found)
    )
    if first:
        return first[0]
    return second[0] if second else None


def shadow(project: Path, scope: SkillScope, name: str) -> Shadow | None:
    """A clash between the project copy and a user copy, whatever the switch.

    ``user`` precedence with the switch off: the CLI runs the user copy over
    the project copy the checks read (``overrides``). ``unordered``: both load
    in no stated order (``competes``). Otherwise ``None``."""
    project_found = _project_copies(project, scope, name)
    if not project_found:
        return None
    user_found = _user_copies(scope, name)
    if not user_found:
        return None
    if scope.precedence == "user" and not scope.resolve_user:
        return Shadow(name, project_found[0], user_found[0], "overrides")
    if scope.precedence == "unordered":
        return Shadow(name, project_found[0], user_found[0], "competes")
    return None


def home_copy(scope: SkillScope, name: str) -> Located | None:
    """The first available user-root copy of ``name``, whatever the switch."""
    found = _user_copies(scope, name)
    return found[0] if found else None


def prompt_skill(prompt: str) -> tuple[str, str]:
    """Split a canonical ``"/skill args"`` prompt into ``(skill, args)``;
    a prompt without a leading slash is ``("", prompt)``."""
    if not prompt.startswith("/"):
        return "", prompt
    head, _, rest = prompt[1:].partition(" ")
    return head, rest.strip()


def prompt_skill_dir(project: Path, scope: SkillScope, prompt: str) -> str | None:
    """The ``{skill_dir}`` spelling for ``prompt``'s skill.

    ``None`` for no skill, a miss or a primary-tree hit, so the renderer keeps
    its relative default; ``"<extra_tree>/<skill>"`` for an extra-tree hit; the
    absolute native folder for a user-root hit."""
    skill, _ = prompt_skill(prompt)
    if not skill:
        return None
    found = locate(project, scope, skill)
    if found is None or found.primary:
        return None
    if found.origin == "project":
        return found.display
    return str(found.path)
