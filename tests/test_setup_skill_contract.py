"""Contract guards for the shipped `bmad-loop-setup` skill.

Two independent contracts meet in this one directory, and both are easy to break
silently:

1. **The BMAD installer's resolver contract.** bmad-loop is registered in
   BMAD-METHOD's `bmad-modules.yaml` as a `marketplace-plugin`, and the installer
   resolves it through `plugin-resolver.js` strategy 2 — "a skill whose directory
   name ends in `-setup`, carrying `assets/module.yaml` **and**
   `assets/module-help.csv`". If any of those three go missing, resolution falls
   through and `official-modules.js` *throws*: the module stops installing.
   Nothing in this repo reads those asset files at runtime, so only a test keeps
   them honest.

2. **No writes to the legacy BMAD config layout** (#258). Setup used to write
   `_bmad/config.yaml`, `_bmad/config.user.yaml` and a root
   `_bmad/module-help.csv` — files BMAD v6.10 never reads. The BMAD installer owns
   module registration; the skill's only `_bmad/` write is the per-module help CSV.
   These are prose assertions because the skill *is* prose — the instructions are
   the executable.
"""

import csv

import pytest

from bmad_loop.install import MODULE_SKILLS

SKILL_DIR = "bmad-loop-setup"


@pytest.fixture(scope="module")
def skill_root():
    from importlib import resources

    return resources.files("bmad_loop.data").joinpath("skills").joinpath(SKILL_DIR)


@pytest.fixture(scope="module")
def skill_md(skill_root):
    return skill_root.joinpath("SKILL.md").read_text(encoding="utf-8")


def test_setup_skill_is_bundled():
    # the installer's strategy-2 match keys off the trailing "-setup"
    assert SKILL_DIR in MODULE_SKILLS
    assert SKILL_DIR.endswith("-setup")


@pytest.mark.parametrize("asset", ["module.yaml", "module-help.csv"])
def test_installer_required_assets_present(skill_root, asset):
    # plugin-resolver.js strategy 2 needs BOTH or bmad-loop stops resolving
    assert skill_root.joinpath("assets").joinpath(asset).is_file()


def test_module_help_csv_shape(skill_root):
    """`mergeModuleHelpCatalogs` parses this positionally and keys rows off column 0."""
    raw = skill_root.joinpath("assets").joinpath("module-help.csv").read_text(encoding="utf-8")
    rows = list(csv.reader(raw.splitlines()))
    header, data = rows[0], rows[1:]
    assert header[0] == "module"
    assert len(header) == 13, f"help CSV column count changed: {header}"
    assert data, "module-help.csv must carry at least one skill row"
    for row in data:
        assert len(row) == len(header), f"ragged row: {row}"
        assert row[0] == "BMAD Loop Skills", f"unexpected module column: {row[0]!r}"
    skill_column = header.index("skill")
    csv_skills = {row[skill_column] for row in data}
    assert csv_skills == set(MODULE_SKILLS), (
        "module-help.csv must list exactly the module skills bmad-loop init "
        f"installs: csv={csv_skills} MODULE_SKILLS={set(MODULE_SKILLS)}"
    )


# The legacy layout (#258) and the pre-rename module code: both are gone, and the
# skill must not reintroduce either. Substring match on purpose — the failure mode
# is prose drifting back, not an exact string reappearing.
@pytest.mark.parametrize(
    "forbidden",
    [
        "config.yaml",
        "config.user.yaml",
        "merge-config.py",
        "merge-help-csv.py",
        "cleanup-legacy.py",
        "bauto",
        "bmad-auto",
        ".automator",
    ],
)
def test_skill_md_drops_legacy_layout(skill_md, forbidden):
    assert forbidden not in skill_md


def test_skill_md_registers_help_at_the_path_v610_reads(skill_md):
    # the per-module CSV is the only path the installer's catalog merge scans;
    # a root _bmad/module-help.csv is never read.
    assert "_bmad/bmad-loop/module-help.csv" in skill_md


def test_setup_skill_ships_no_scripts(skill_root):
    # all three PEP 723 scripts were retired with #258/#259; nothing left to invoke
    assert not skill_root.joinpath("scripts").is_dir()
