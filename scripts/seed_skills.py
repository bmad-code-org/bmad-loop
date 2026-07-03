#!/usr/bin/env python3
"""Reseed the dev-workspace skill forks from the canonical wheel source.

``src/bmad_loop/data/skills/<skill>`` is the single source of truth for the
``bmad-loop-*`` automation skills (bundled into the wheel; ``bmad-loop init``
installs them). Two dev-workspace trees hold byte-identical *forks* of those
skills so the local agents can run them out of this repo:

* ``.claude/skills/<skill>``  — read by Claude Code
* ``.agents/skills/<skill>``  — read by codex / gemini

``tests/test_module_skills_sync.py`` turns any drift between canonical and a
fork into a failure. The version bump in ``scripts/sync_version.py`` stamps the
canonical ``bmad-loop-setup/assets/module.yaml``, which immediately drifts both
forks — so every release had to be followed by a hand reseed before the local
suite went green again. This script is that reseed, and ``release.py prepare``
runs it automatically right after stamping.

Both fork trees are gitignored dev-only workspaces, so nothing here is committed
— a tree that is absent (as in CI) is simply skipped, never created.

Usage::

    uv run python scripts/seed_skills.py            # reseed every present fork
    uv run python scripts/seed_skills.py --check     # report drift, mutate nothing
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Import MODULE_SKILLS straight from the package so this list can never drift
# from the one the installer and the sync test use.
sys.path.insert(0, str(ROOT / "src"))
from bmad_loop.install import MODULE_SKILLS  # noqa: E402

SKILLS_SRC = ROOT / "src" / "bmad_loop" / "data" / "skills"
FORK_TREES = (".claude/skills", ".agents/skills")


def drift(canonical: Path, fork: Path) -> list[str]:
    """Recursively compare a canonical skill dir against its fork, returning a
    list of human-readable drift problems (empty when byte-identical). Mirrors
    the comparison in tests/test_module_skills_sync.py."""
    if not fork.exists():
        return [f"fork missing: {fork.relative_to(ROOT)}"]
    problems: list[str] = []
    stack = [filecmp.dircmp(canonical, fork)]
    while stack:
        node = stack.pop()
        rel = Path(node.left).relative_to(canonical)
        problems += [f"only in canonical: {rel / n}" for n in node.left_only]
        problems += [f"extra in fork: {rel / n}" for n in node.right_only]
        _, mismatch, errors = filecmp.cmpfiles(
            node.left, node.right, node.common_files, shallow=False
        )
        problems += [f"content differs: {rel / n}" for n in mismatch + errors]
        stack.extend(node.subdirs.values())
    return problems


def reseed(canonical: Path, fork: Path) -> None:
    """Replace ``fork`` with an exact copy of ``canonical``."""
    if fork.exists():
        shutil.rmtree(fork)
    fork.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(canonical, fork)


def run(check: bool) -> int:
    present = [tree for tree in FORK_TREES if (ROOT / tree).is_dir()]
    if not present:
        print("no dev-workspace skill forks present (.claude/.agents) — nothing to reseed")
        return 0

    drifted: list[tuple[str, list[str]]] = []
    reseeded: list[str] = []
    for tree in present:
        for skill in MODULE_SKILLS:
            canonical = SKILLS_SRC / skill
            if not canonical.is_dir():
                sys.exit(f"error: canonical skill missing: {canonical.relative_to(ROOT)}")
            fork = ROOT / tree / skill
            problems = drift(canonical, fork)
            if not problems:
                continue
            if check:
                drifted.append((f"{tree}/{skill}", problems))
            else:
                reseed(canonical, fork)
                reseeded.append(f"{tree}/{skill}")

    if check:
        if drifted:
            print("skill fork drift detected (run scripts/seed_skills.py to fix):", file=sys.stderr)
            for label, problems in drifted:
                for p in problems:
                    print(f"  - {label}: {p}", file=sys.stderr)
            return 1
        print("ok: every skill fork matches canonical")
        return 0

    if reseeded:
        print("reseeded skill forks from canonical:\n  " + "\n  ".join(reseeded))
    else:
        print("skill forks already match canonical — nothing to reseed")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 or (argv and argv[0] != "--check"):
        sys.exit("usage: seed_skills.py [--check]")
    return run(check=bool(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
