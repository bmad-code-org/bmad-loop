"""Contract guards for the shipped `bmad-loop-sweep` skill."""

import pytest

SKILL_DIR = "bmad-loop-sweep"


@pytest.fixture(scope="module")
def skill_root():
    from importlib import resources

    return resources.files("bmad_loop.data").joinpath("skills").joinpath(SKILL_DIR)


def test_sweep_skill_states_bundle_name_contract(skill_root):
    """ABLATION A3: remove the primary skill constraint and this fails its first assertion."""
    skill_md = " ".join(skill_root.joinpath("SKILL.md").read_text(encoding="utf-8").split())
    automation_md = " ".join(
        skill_root.joinpath("automation-mode.md").read_text(encoding="utf-8").split()
    )

    assert "`name` matches `^[a-z0-9][a-z0-9-]{1,39}\\Z`" in skill_md
    assert "at most 40 characters" in skill_md
    assert (
        "otherwise-valid overlong bundle name or decision option `bundle_name` is truncated "
        "to 40 characters and journaled before validation"
    ) in automation_md
    assert "post-truncation name collisions still fail validation" in automation_md


def test_sweep_automation_mode_scopes_validation_to_the_triage_universe(skill_root):
    """ABLATION (#824): restore the old unconditional `open_ids` bullet and this fails
    its first assertion."""
    automation_md = " ".join(
        skill_root.joinpath("automation-mode.md").read_text(encoding="utf-8").split()
    )
    rules = automation_md.split("Validation rules the orchestrator enforces", 1)[1].split(
        "Write `already_resolved[].evidence`", 1
    )[0]

    assert "`open_ids` must list exactly this session's triage universe" in rules
    assert "when the invocation carries `--only DW-1,DW-2,...`, exactly those named ids" in rules
    assert "applies the same selection, and compares" in rules
    assert "Every id in the triage universe appears in exactly ONE of" in rules
    assert "an open entry outside the `--only` selection counts as invented" in rules
    assert "`open_ids` must list exactly the ledger's `status: open` entries" not in automation_md
    assert "Every open id appears in exactly ONE of" not in automation_md
