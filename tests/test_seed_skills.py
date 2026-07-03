"""Unit tests for scripts/seed_skills.py — the reseed that keeps the gitignored
dev-workspace skill forks (.claude/skills, .agents/skills) byte-identical to the
canonical src/bmad_loop/data/skills/ source after a version bump.

The module's paths and skill list are module-level globals, so each test points
them at a throwaway tmp workspace via monkeypatch rather than touching the real
repo trees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import seed_skills  # noqa: E402


def _build_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trees: tuple[str, ...]):
    """Lay out canonical + fork trees under tmp_path and repoint the module at
    them. Returns (canonical_skill_dir, {tree: fork_skill_dir})."""
    canonical = tmp_path / "src" / "bmad_loop" / "data" / "skills" / "demo-skill"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("canonical body\n")
    (canonical / "assets").mkdir()
    (canonical / "assets" / "module.yaml").write_text("module_version: 9.9.9\n")

    forks = {}
    for tree in trees:
        fork = tmp_path / tree / "demo-skill"
        fork.mkdir(parents=True)
        (fork / "SKILL.md").write_text("stale body\n")  # drifted on purpose
        forks[tree] = fork

    monkeypatch.setattr(seed_skills, "ROOT", tmp_path)
    monkeypatch.setattr(seed_skills, "SKILLS_SRC", canonical.parent)
    monkeypatch.setattr(seed_skills, "FORK_TREES", trees)
    monkeypatch.setattr(seed_skills, "MODULE_SKILLS", ("demo-skill",))
    return canonical, forks


def test_check_detects_drift(monkeypatch, tmp_path, capsys):
    _build_workspace(monkeypatch, tmp_path, (".claude/skills",))
    assert seed_skills.run(check=True) == 1
    assert "drift detected" in capsys.readouterr().err


def test_reseed_fixes_drift(monkeypatch, tmp_path):
    canonical, forks = _build_workspace(monkeypatch, tmp_path, (".claude/skills", ".agents/skills"))
    assert seed_skills.run(check=False) == 0
    # Both forks now byte-match canonical, including the nested asset.
    for fork in forks.values():
        assert not seed_skills.drift(canonical, fork)
        assert (fork / "assets" / "module.yaml").read_text() == "module_version: 9.9.9\n"
    # And a follow-up --check is clean.
    assert seed_skills.run(check=True) == 0


def test_reseed_prunes_extra_fork_files(monkeypatch, tmp_path):
    canonical, forks = _build_workspace(monkeypatch, tmp_path, (".claude/skills",))
    stray = forks[".claude/skills"] / "leftover.md"
    stray.write_text("should be removed\n")
    seed_skills.run(check=False)
    assert not stray.exists()


def test_missing_fork_tree_is_skipped(monkeypatch, tmp_path, capsys):
    # No .claude/.agents trees present at all (the CI shape).
    canonical = tmp_path / "src" / "bmad_loop" / "data" / "skills" / "demo-skill"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("body\n")
    monkeypatch.setattr(seed_skills, "ROOT", tmp_path)
    monkeypatch.setattr(seed_skills, "SKILLS_SRC", canonical.parent)
    monkeypatch.setattr(seed_skills, "FORK_TREES", (".claude/skills", ".agents/skills"))
    monkeypatch.setattr(seed_skills, "MODULE_SKILLS", ("demo-skill",))
    assert seed_skills.run(check=False) == 0
    assert "nothing to reseed" in capsys.readouterr().out


def test_missing_canonical_skill_is_fatal(monkeypatch, tmp_path):
    _build_workspace(monkeypatch, tmp_path, (".claude/skills",))
    monkeypatch.setattr(seed_skills, "MODULE_SKILLS", ("does-not-exist",))
    with pytest.raises(SystemExit) as exc:
        seed_skills.run(check=False)
    assert "canonical skill missing" in str(exc.value)


def test_main_rejects_bad_args(monkeypatch, tmp_path):
    _build_workspace(monkeypatch, tmp_path, (".claude/skills",))
    with pytest.raises(SystemExit):
        seed_skills.main(["--bogus"])
