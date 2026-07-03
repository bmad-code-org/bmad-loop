"""Drift guard: src/bmad_loop/data/skills/ is the canonical source for the
bmad-loop skills (bundled into the wheel; `bmad-loop init` installs them).

The forked skills are plain copies (not symlinks) in .claude/skills/ (read by
Claude Code) and .agents/skills/ (read by codex/gemini). Edits must flow
src/bmad_loop/data/skills/<skill> -> both trees; this test turns drift into a
CI failure.
"""

import filecmp
from pathlib import Path

import pytest

from bmad_loop.install import MODULE_SKILLS

REPO = Path(__file__).resolve().parent.parent
SKILLS_SRC = REPO / "src" / "bmad_loop" / "data" / "skills"
SKILL_TREES = [".claude/skills", ".agents/skills"]


def _assert_identical(canonical: Path, installed: Path) -> None:
    cmp = filecmp.dircmp(canonical, installed)
    stack = [cmp]
    problems: list[str] = []
    while stack:
        node = stack.pop()
        rel = Path(node.left).relative_to(canonical)
        for name in node.left_only:
            problems.append(f"missing from installed copy: {rel / name}")
        for name in node.right_only:
            problems.append(f"extra in installed copy: {rel / name}")
        _, mismatch, errors = filecmp.cmpfiles(
            node.left, node.right, node.common_files, shallow=False
        )
        for name in mismatch + errors:
            problems.append(f"content differs: {rel / name}")
        stack.extend(node.subdirs.values())
    assert not problems, (
        f"{installed} has drifted from canonical {canonical}; "
        f"re-copy from src/bmad_loop/data/skills/ to fix:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("skill", MODULE_SKILLS)
@pytest.mark.parametrize("tree", SKILL_TREES)
def test_installed_skill_matches_module(skill: str, tree: str) -> None:
    canonical = SKILLS_SRC / skill
    installed = REPO / tree / skill
    assert canonical.is_dir(), f"canonical skill missing: {canonical}"
    # .claude/ and .agents/ are dev-workspace trees, untracked and absent in CI
    # (gitignored). When present locally this still guards drift; otherwise skip.
    if not (REPO / tree).is_dir():
        pytest.skip(f"{tree} not present (dev-workspace only)")
    assert installed.is_dir(), f"skill not installed in {tree}: {installed}"
    _assert_identical(canonical, installed)
