"""The skill locator (`bmad_loop.skill_locate`): pure resolution over a scope.

Every test passes `home=` / `environ=` (or monkeypatches the seams), because
conftest pins `user_home` to an empty dir and `ambient_environ` to `{}`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bmad_loop import skill_locate
from bmad_loop.skill_locate import (
    Located,
    SkillScope,
    UserRoot,
    UserSkillRoot,
    UserSkillsSpec,
    as_scope,
    build_scope,
    copies,
    home_copy,
    locate,
    prompt_skill,
    prompt_skill_dir,
    scope_for,
    shadow,
)

SKILL = "bmad-build-auto"
# Captured at import, before conftest's autouse fixture pins the seam.
_REAL_USER_HOME = skill_locate.user_home


def _skill(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    return folder


def _plant_directory_redirect(link: Path, target: Path) -> None:
    """A directory redirect: a symlink on POSIX, a junction on win32 (no
    privilege needed), as in tests/test_artifact_publication.py."""
    if sys.platform == "win32":
        import _winapi  # Windows-only stdlib module

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def layout(tmp_path):
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return project, home


def _spec(precedence: str, *roots: str | UserSkillRoot) -> UserSkillsSpec:
    return UserSkillsSpec(
        precedence=precedence,
        roots=tuple(r if isinstance(r, UserSkillRoot) else UserSkillRoot(r) for r in roots),
    )


def _scope(home, precedence="project", *, resolve_user, roots=("~/.agents/skills",), environ=None):
    return build_scope(
        ".agents/skills",
        user_skills=_spec(precedence, *roots),
        resolve_user=resolve_user,
        home=home,
        environ={} if environ is None else environ,
    )


# precedence x resolve_user x {project-only, user-only, both} -> expected origin
_MATRIX = [
    (precedence, resolve_user, placement, expected)
    for precedence in ("project", "user", "unordered")
    for resolve_user in (False, True)
    for placement, expected in (
        ("project", "project"),
        ("user", "user" if resolve_user else None),
        (
            "both",
            "user" if (resolve_user and precedence == "user") else "project",
        ),
    )
]


@pytest.mark.parametrize(("precedence", "resolve_user", "placement", "expected"), _MATRIX)
def test_locate_precedence_matrix(layout, precedence, resolve_user, placement, expected):
    """Off = project-only, whatever the precedence. On: `user` puts the user copy
    first; `project` and `unordered` keep the project copy and fall back to a user
    copy only when the project has none.

    Ablation: drop the `if not scope.resolve_user` early return in `locate` and the
    (False, "user") rows fail with a user hit, and (False, "both") under `user`
    precedence returns the user copy."""
    project, home = layout
    if placement in ("project", "both"):
        _skill(project / ".agents/skills" / SKILL)
    if placement in ("user", "both"):
        _skill(home / ".agents/skills" / SKILL)
    found = locate(project, _scope(home, precedence, resolve_user=resolve_user), SKILL)
    if expected is None:
        assert found is None
        return
    assert found is not None
    assert found.origin == expected
    if expected == "project":
        assert found == Located(
            SKILL, project / ".agents/skills" / SKILL, "project", ".agents/skills", True
        )
        assert found.display == f".agents/skills/{SKILL}"
    else:
        assert found == Located(
            SKILL, home / ".agents/skills" / SKILL, "user", "~/.agents/skills", False
        )
        assert found.display == f"~/.agents/skills/{SKILL}"


def test_locate_missing_everywhere_is_none(layout):
    project, home = layout
    assert locate(project, _scope(home, resolve_user=True), SKILL) is None
    assert copies(project, _scope(home, resolve_user=True), SKILL) == []


def test_a_folder_without_skill_md_is_not_a_copy(layout):
    project, home = layout
    (project / ".agents/skills" / SKILL).mkdir(parents=True)
    (home / ".agents/skills" / SKILL).mkdir(parents=True)
    assert locate(project, _scope(home, resolve_user=True), SKILL) is None


def test_copies_order_primary_extras_then_user_roots(layout):
    project, home = layout
    for tree in (".claude/skills", ".agents/skills", ".github/skills"):
        _skill(project / tree / SKILL)
    for root in (".config/opencode/skills", ".agents/skills"):
        _skill(home / root / SKILL)
    scope = build_scope(
        ".claude/skills",
        extra_trees=(".agents/skills", ".github/skills"),
        user_skills=_spec("project", "~/.config/opencode/skills", "~/.agents/skills"),
        resolve_user=True,
        home=home,
        environ={},
    )
    assert scope.project_trees() == (".claude/skills", ".agents/skills", ".github/skills")
    found = copies(project, scope, SKILL)
    assert [(c.origin, c.root, c.primary) for c in found] == [
        ("project", ".claude/skills", True),
        ("project", ".agents/skills", False),
        ("project", ".github/skills", False),
        ("user", "~/.config/opencode/skills", False),
        ("user", "~/.agents/skills", False),
    ]


def test_extra_tree_fills_in_when_primary_lacks_the_skill(layout):
    project, home = layout
    _skill(project / ".agents/skills" / SKILL)
    scope = build_scope(".claude/skills", extra_trees=(".agents/skills",), home=home, environ={})
    found = locate(project, scope, SKILL)
    assert found is not None
    assert (found.origin, found.root, found.primary) == ("project", ".agents/skills", False)


def test_user_roots_are_searched_in_declared_order(layout):
    project, home = layout
    _skill(home / ".gemini/skills" / SKILL)
    _skill(home / ".agents/skills" / SKILL)
    scope = _scope(home, resolve_user=True, roots=("~/.agents/skills", "~/.gemini/skills"))
    found = locate(project, scope, SKILL)
    assert found is not None and found.root == "~/.agents/skills"
    scope = _scope(home, resolve_user=True, roots=("~/.gemini/skills", "~/.agents/skills"))
    found = locate(project, scope, SKILL)
    assert found is not None and found.root == "~/.gemini/skills"


def test_disabled_root_never_resolves(layout):
    """A root whose disabler variable is set (even to "") is unavailable: never
    located, never a shadow, never a home copy.

    Ablation: drop the `disabled_by` loop in `_user_root` and every assertion
    below fails with the user copy resolving."""
    project, home = layout
    _skill(home / ".claude/skills" / SKILL)
    _skill(project / ".claude/skills" / "other")
    root = UserSkillRoot("~/.claude/skills", ("CLAUDE_CONFIG_DIR",))
    for value in ("/elsewhere", ""):
        scope = build_scope(
            ".claude/skills",
            user_skills=UserSkillsSpec("user", (root,)),
            resolve_user=True,
            home=home,
            environ={"CLAUDE_CONFIG_DIR": value},
        )
        assert scope.user_roots == (
            UserRoot("~/.claude/skills", home / ".claude/skills", "env:CLAUDE_CONFIG_DIR"),
        )
        assert locate(project, scope, SKILL) is None
        assert home_copy(scope, SKILL) is None
    # Unrelated variables leave it available.
    scope = build_scope(
        ".claude/skills",
        user_skills=UserSkillsSpec("user", (root,)),
        resolve_user=True,
        home=home,
        environ={"CODEX_HOME": "/x"},
    )
    assert scope.user_roots[0].unavailable == ""
    assert locate(project, scope, SKILL) is not None


@dataclass(frozen=True)
class _Profile:
    skill_tree: str = ".agents/skills"
    extra_skill_trees: tuple[str, ...] = ()
    user_skills: UserSkillsSpec | None = None
    env: dict[str, str] = field(default_factory=dict)


def test_scope_for_reads_the_disabler_from_the_profile_env(layout):
    project, home = layout
    _skill(home / ".codex/skills" / SKILL)
    spec = _spec("unordered", "~/.agents/skills", UserSkillRoot("~/.codex/skills", ("CODEX_HOME",)))
    plain = scope_for(_Profile(user_skills=spec), resolve_user=True, home=home)
    assert locate(project, plain, SKILL) is not None
    disabled = scope_for(
        _Profile(user_skills=spec, env={"CODEX_HOME": "/srv/codex"}), resolve_user=True, home=home
    )
    assert [r.unavailable for r in disabled.user_roots] == ["", "env:CODEX_HOME"]
    assert locate(project, disabled, SKILL) is None


def test_scope_for_reads_the_disabler_from_the_ambient_environ(layout, monkeypatch):
    project, home = layout
    _skill(home / ".codex/skills" / SKILL)
    spec = _spec("unordered", UserSkillRoot("~/.codex/skills", ("CODEX_HOME",)))
    monkeypatch.setattr(skill_locate, "ambient_environ", lambda: {"CODEX_HOME": "/srv/codex"})
    scope = scope_for(_Profile(user_skills=spec), resolve_user=True, home=home)
    assert scope.user_roots[0].unavailable == "env:CODEX_HOME"
    # An explicit environ replaces the ambient one; the profile env still overlays.
    scope = scope_for(_Profile(user_skills=spec), resolve_user=True, home=home, environ={})
    assert scope.user_roots[0].unavailable == ""


def test_scope_for_defaults_to_the_seams(layout, monkeypatch):
    project, home = layout
    _skill(home / ".agents/skills" / SKILL)
    monkeypatch.setattr(skill_locate, "user_home", lambda: home)
    scope = scope_for(_Profile(user_skills=_spec("project", "~/.agents/skills")), resolve_user=True)
    assert scope.user_roots == (UserRoot("~/.agents/skills", home / ".agents/skills"),)
    found = locate(project, scope, SKILL)
    assert found is not None and found.origin == "user"


def test_scope_for_a_packaged_profile(layout):
    """`CLIProfile` satisfies the profile protocol."""
    from bmad_loop.adapters.profile import get_profile

    project, home = layout
    scope = scope_for(get_profile("claude"), resolve_user=False, home=home, environ={})
    assert scope == SkillScope(
        ".claude/skills",
        user_roots=(UserRoot("~/.claude/skills", home / ".claude/skills"),),
        precedence="user",
    )


def test_no_user_skills_means_no_roots(layout):
    project, home = layout
    _skill(home / ".agents/skills" / SKILL)
    scope = scope_for(_Profile(), resolve_user=True, home=home)
    assert scope == SkillScope(".agents/skills", resolve_user=True)
    assert locate(project, scope, SKILL) is None


def test_no_home_marks_every_root_unavailable(layout):
    project, _home = layout
    scope = _scope(None, resolve_user=True, roots=("~/.agents/skills", "~/.codex/skills"))
    assert scope.user_roots == (
        UserRoot("~/.agents/skills", None, "no-home"),
        UserRoot("~/.codex/skills", None, "no-home"),
    )
    assert locate(project, scope, SKILL) is None
    assert home_copy(scope, SKILL) is None


def test_user_home_is_none_when_home_cannot_be_determined(monkeypatch):
    def boom():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(boom))
    assert _REAL_USER_HOME() is None


def test_unreadable_root_is_unavailable(layout, monkeypatch):
    project, home = layout
    _skill(home / ".agents/skills" / SKILL)
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == home / ".agents/skills":
            raise PermissionError(13, "Permission denied")
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    scope = _scope(home, resolve_user=True)
    assert scope.user_roots == (
        UserRoot("~/.agents/skills", home / ".agents/skills", "unreadable"),
    )
    assert locate(project, scope, SKILL) is None


def test_unreadable_copy_counts_as_absent(layout, monkeypatch):
    project, home = layout
    _skill(project / ".agents/skills" / SKILL)
    real_is_file = Path.is_file

    def is_file(self, *args, **kwargs):
        if self.name == "SKILL.md":
            raise PermissionError(13, "Permission denied")
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert locate(project, _scope(home, resolve_user=True), SKILL) is None


def test_a_missing_root_is_available_but_empty(layout):
    project, home = layout
    scope = _scope(home, resolve_user=True, roots=("~/.nowhere/skills",))
    assert scope.user_roots[0].unavailable == ""
    assert locate(project, scope, SKILL) is None


def test_redirected_skill_inside_a_user_root_resolves(layout, tmp_path):
    """The skills CLI links each global skill into ~/.agents/skills: a link (a
    junction on Windows) inside a user root is a copy at its native path."""
    project, home = layout
    real = _skill(tmp_path / "store" / SKILL)
    (home / ".agents/skills").mkdir(parents=True)
    _plant_directory_redirect(home / ".agents/skills" / SKILL, real)
    found = locate(project, _scope(home, resolve_user=True), SKILL)
    assert found is not None
    assert found.path == home / ".agents/skills" / SKILL
    assert prompt_skill_dir(project, _scope(home, resolve_user=True), f"/{SKILL} x") == str(
        home / ".agents/skills" / SKILL
    )


@pytest.mark.parametrize(
    ("precedence", "resolve_user", "kind"),
    [
        ("user", False, "overrides"),
        ("user", True, None),
        ("unordered", False, "competes"),
        ("unordered", True, "competes"),
        ("project", False, None),
        ("project", True, None),
    ],
)
def test_shadow(layout, precedence, resolve_user, kind):
    project, home = layout
    _skill(project / ".agents/skills" / SKILL)
    _skill(home / ".agents/skills" / SKILL)
    scope = _scope(home, precedence, resolve_user=resolve_user)
    found = shadow(project, scope, SKILL)
    if kind is None:
        assert found is None
        return
    assert found is not None
    assert found.kind == kind
    assert found.checked.origin == "project" and found.checked.primary
    assert found.other.display == f"~/.agents/skills/{SKILL}"


@pytest.mark.parametrize("placement", ["project", "user", "none"])
def test_no_shadow_without_both_copies(layout, placement):
    project, home = layout
    if placement == "project":
        _skill(project / ".agents/skills" / SKILL)
    if placement == "user":
        _skill(home / ".agents/skills" / SKILL)
    for precedence in ("user", "unordered"):
        assert shadow(project, _scope(home, precedence, resolve_user=False), SKILL) is None


def test_home_copy_ignores_the_switch(layout):
    project, home = layout
    _skill(home / ".agents/skills" / SKILL)
    found = home_copy(_scope(home, resolve_user=False), SKILL)
    assert found is not None and found.display == f"~/.agents/skills/{SKILL}"
    assert home_copy(_scope(home, resolve_user=False), "absent") is None


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("/bmad-build-auto story-1 --x", ("bmad-build-auto", "story-1 --x")),
        ("/bmad-build-auto", ("bmad-build-auto", "")),
        ("/bmad-build-auto   padded  ", ("bmad-build-auto", "padded")),
        ("plain text", ("", "plain text")),
        ("", ("", "")),
    ],
)
def test_prompt_skill(prompt, expected):
    assert prompt_skill(prompt) == expected


def test_prompt_skill_dir_spellings(layout):
    """None for a primary hit or a miss (the renderer keeps its relative
    default), the relative spelling for an extra tree, the absolute native folder
    for a user root."""
    project, home = layout
    _skill(project / ".claude/skills" / "primary")
    _skill(project / ".agents/skills" / "extra")
    _skill(home / ".agents/skills" / "personal")
    scope = build_scope(
        ".claude/skills",
        extra_trees=(".agents/skills",),
        user_skills=_spec("project", "~/.agents/skills"),
        resolve_user=True,
        home=home,
        environ={},
    )
    assert prompt_skill_dir(project, scope, "/primary a b") is None
    assert prompt_skill_dir(project, scope, "/missing a b") is None
    assert prompt_skill_dir(project, scope, "no slash") is None
    assert prompt_skill_dir(project, scope, "/extra a") == ".agents/skills/extra"
    expected = str(home / ".agents/skills" / "personal")
    assert prompt_skill_dir(project, scope, "/personal a") == expected
    assert Path(expected).is_absolute()


def test_as_scope():
    assert as_scope("x") == SkillScope("x")
    assert as_scope("x").project_trees() == ("x",)
    scope = SkillScope("x", extra_trees=("y",), resolve_user=True)
    assert as_scope(scope) is scope


def test_scope_is_hashable_and_dedupes():
    a = SkillScope(".agents/skills", user_roots=(UserRoot("~/.agents/skills", Path("/h")),))
    b = SkillScope(".agents/skills", user_roots=(UserRoot("~/.agents/skills", Path("/h")),))
    assert list(dict.fromkeys([a, b, SkillScope(".claude/skills")])) == [
        a,
        SkillScope(".claude/skills"),
    ]
