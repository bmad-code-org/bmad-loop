"""Schema-driven settings: the TOML core schema stays in lockstep with the
policy dataclasses, every policy field is accounted for, the schema ships in an
installed context, and an enabled plugin's settings render + round-trip.

The literal->TOML swap is proven behavior-identical by the unchanged
test_tui_settings.py suite; this file guards the schema layer itself."""

from __future__ import annotations

import dataclasses
from importlib import resources

import pytest

from bmad_loop import policy as policy_mod
from bmad_loop.plugins import load_plugins
from bmad_loop.policy import (
    BRANCH_PER_MODES,
    GATE_MODES,
    ISOLATION_MODES,
    MERGE_STRATEGIES,
    POLICY_TEMPLATE,
    RETRO_MODES,
    REVIEW_TRIGGER_MODES,
    SWEEP_AUTO_MODES,
    AdapterPolicy,
    CleanupPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    TuiPolicy,
    VerifyPolicy,
)
from bmad_loop.settings_schema import build_registry, load_core_schema

# Which policy dataclass backs each core schema section. The game-engine layer is
# no longer a core section — it is the "unity" plugin, rendered from its own
# manifest schema (see test_unity_plugin_settings_render_when_enabled).
SECTION_DC = {
    "gates": GatesPolicy,
    "review": ReviewPolicy,
    "limits": LimitsPolicy,
    "verify": VerifyPolicy,
    "notify": NotifyPolicy,
    "adapter": AdapterPolicy,
    "adapter.dev": StageAdapterPolicy,
    "adapter.review": StageAdapterPolicy,
    "adapter.triage": StageAdapterPolicy,
    "sweep": SweepPolicy,
    "scm": ScmPolicy,
    "cleanup": CleanupPolicy,
    "tui": TuiPolicy,
}

# select fields whose options are an enum set.
OPTIONS_ENUM = {
    ("gates", "mode"): GATE_MODES,
    ("gates", "retrospective"): RETRO_MODES,
    ("review", "trigger"): REVIEW_TRIGGER_MODES,
    ("sweep", "auto"): SWEEP_AUTO_MODES,
    ("scm", "isolation"): ISOLATION_MODES,
    ("scm", "branch_per"): BRANCH_PER_MODES,
    ("scm", "merge_strategy"): MERGE_STRATEGIES,
}

# Policy fields with no settings-screen control, by design. Each maps to a whole
# section (the adapter stage sub-tables) or is intentionally hidden.
HIDDEN = {
    ("gates", "on_escalation"),  # CRITICAL escalations always pause; field reserved
    ("sweep", "max_migration_attempts"),  # rare legacy-ledger path; no UI knob
    ("scm", "max_parallel"),  # inert until the parallel scheduler lands
    ("adapter", "dev"),  # rendered as the adapter.dev section
    ("adapter", "review"),  # rendered as the adapter.review section
    ("adapter", "triage"),  # rendered as the adapter.triage section
}


def core_fields():
    return [f for f in build_registry().fields() if not f.section.startswith("plugins.")]


# ------------------------------------------------------------------ sync


def test_defaults_match_referenced_dataclass():
    """Every core field that carries a default resolves to the live dataclass
    default — the schema can never drift from policy.py's runtime model."""
    for f in core_fields():
        dc = SECTION_DC[f.section]
        if f.kind in ("switch", "select", "int", "float"):
            # widget kinds that render a baked default MUST carry the live one
            assert f.default == getattr(dc, f.key), f"{f.section}.{f.key}"
        elif f.default is not None:
            # str fields may omit a default (placeholder-only); if present, sync it
            assert f.default == getattr(dc, f.key), f"{f.section}.{f.key}"


def test_select_options_match_enum_sets():
    for f in core_fields():
        if f.kind == "select" and (f.section, f.key) in OPTIONS_ENUM:
            expected = tuple(sorted(OPTIONS_ENUM[(f.section, f.key)]))
            assert f.options == expected, f"{f.section}.{f.key}"


def test_every_core_spec_maps_to_a_real_policy_field():
    for f in core_fields():
        assert hasattr(SECTION_DC[f.section], f.key), f"{f.section}.{f.key} is not a policy field"


def test_every_policy_field_is_covered_by_exactly_one_spec():
    """Inverse coverage: no policy field is silently unreachable from the UI."""
    keys = [(f.section, f.key) for f in core_fields()]
    assert len(keys) == len(set(keys)), "duplicate spec keys"
    covered = set(keys)
    for section, dc in SECTION_DC.items():
        for fld in dataclasses.fields(dc):
            key = (section, fld.name)
            if key in HIDDEN:
                continue
            assert key in covered, f"uncovered policy field {section}.{fld.name}"


# --------------------------------------------------------------- packaging


def test_core_schema_ships_in_installed_context():
    packaged = resources.files("bmad_loop.data").joinpath("settings/core.toml")
    assert packaged.is_file()
    # and it parses into the same sections the loader produces
    assert [s.name for s in load_core_schema()][0] == "gates"


# ------------------------------------------------------------- plugin render


def test_every_discovered_plugin_is_listed_with_settings():
    """Every discovered plugin is surfaced (not just enabled ones) so the screen
    can offer an enable toggle. A data-only plugin's settings render regardless;
    its fields and schema are present whether or not it is enabled."""
    reg = build_registry(None, Policy())
    example = next((p for p in reg.plugins if p.name == "example"), None)
    assert example is not None
    assert example.enabled is False  # not in [plugins] enabled
    assert example.trust_needed is False  # data-only: no [python], no toggle
    assert [f.key for f in example.fields] == ["greeting"]
    greeting = example.fields[0]
    assert greeting.section == "plugins.example"
    assert greeting.widget_id == "plugins-example-greeting"  # dotted -> dashed, unchanged
    assert greeting.kind == "str" and greeting.default == "hello"
    # plugin_schemas cover every plugin contributing settings, so [plugins.<name>]
    # tables are typed on save even when the plugin is not enabled.
    assert "example" in reg.plugin_schemas


def test_enabled_state_reflects_policy():
    on = build_registry(None, policy_mod.loads('[plugins]\nenabled = ["example"]\n'))
    assert next(p for p in on.plugins if p.name == "example").enabled is True
    off = build_registry(None, Policy())
    assert next(p for p in off.plugins if p.name == "example").enabled is False


def test_plugins_are_separate_from_core_sections():
    reg = build_registry(None, Policy())
    names = [s.name for s in reg.sections]
    assert names[0] == "gates"
    assert not any(n.startswith("plugins.") for n in names)  # plugins live in .plugins
    assert {p.name for p in reg.plugins} >= {"example", "unity"}


# ----------------------------------------------------- policy storage / accessor


def test_plugin_settings_parse_and_accessor():
    pol = policy_mod.loads(
        '[plugins]\nenabled = ["example"]\n\n[plugins.example]\ngreeting = "hi"\n'
    )
    assert pol.plugins.settings == {"example": {"greeting": "hi"}}
    assert pol.plugin_setting("example", "greeting") == "hi"
    assert pol.plugin_setting("example", "missing", "fallback") == "fallback"
    assert pol.plugin_setting("absent", "greeting", "x") == "x"


def test_plugin_settings_table_does_not_disturb_enabled():
    pol = policy_mod.loads(
        '[plugins]\nenabled = ["example", "other"]\n\n[plugins.example]\ngreeting = "hi"\n'
    )
    assert pol.plugins.enabled == ("example", "other")


def test_plugin_schema_validation_rejects_unknown_key():
    schemas = {m: load_plugins()[m].settings for m in ["example"]}
    text = '[plugins]\nenabled = ["example"]\n\n[plugins.example]\nbogus = 1\n'
    with pytest.raises(policy_mod.PolicyError, match="unknown setting 'bogus'"):
        policy_mod.loads(text, plugin_schemas=schemas)


def test_plugin_schema_validation_rejects_wrong_type():
    schemas = {"example": load_plugins()["example"].settings}
    text = '[plugins]\nenabled = ["example"]\n\n[plugins.example]\ngreeting = 5\n'
    with pytest.raises(policy_mod.PolicyError, match="greeting must be a string"):
        policy_mod.loads(text, plugin_schemas=schemas)


def test_plugin_schema_validation_passes_valid_and_is_optional():
    schemas = {"example": load_plugins()["example"].settings}
    text = '[plugins]\nenabled = ["example"]\n\n[plugins.example]\ngreeting = "hey"\n'
    # with schema: validates and passes
    assert policy_mod.loads(text, plugin_schemas=schemas).plugin_setting("example", "greeting")
    # without schema: passes through untouched (plugin may not be loaded here)
    assert policy_mod.loads(text).plugin_setting("example", "greeting") == "hey"


def test_template_has_no_plugin_settings_tables():
    # POLICY_TEMPLATE must stay untouched: [plugins] carries only `enabled`.
    pol = policy_mod.loads(POLICY_TEMPLATE)
    assert pol.plugins.settings == {}


# ---------------------------------------------- engine-is-now-a-plugin (Unity)


def test_unity_plugin_is_trust_gated_and_renders_settings():
    """The game-engine layer's settings render from the unity plugin schema under
    [plugins.unity]. Unity declares a [python] module, so it is trust-gated
    (carries an enable toggle) and its settings are listed whether enabled or not."""
    reg = build_registry(None, Policy())
    unity = next((p for p in reg.plugins if p.name == "unity"), None)
    assert unity is not None
    assert unity.trust_needed is True  # has [python] -> enabling grants trust
    assert unity.enabled is False
    keys = [f.key for f in unity.fields]
    assert keys == ["editor_mode", "mcp", "unity_path", "ready_timeout_sec", "ready_grace_sec"]
    editor_mode = next(f for f in unity.fields if f.key == "editor_mode")
    assert editor_mode.kind == "select"
    assert editor_mode.options == ("shared", "per_worktree")
    assert editor_mode.widget_id == "plugins-unity-editor_mode"  # dotted -> dashed


def test_deprecated_engine_block_folds_into_unity_plugin():
    """A legacy [engine] block loads (with a deprecation warning) by folding onto
    [plugins] enabled + [plugins.unity]; explicit [plugins.unity] values win."""
    with pytest.warns(DeprecationWarning):
        pol = policy_mod.loads(
            '[engine]\nname = "unity"\neditor_mode = "per_worktree"\nmcp = "coplaydev"\n'
            '[scm]\nisolation = "worktree"\n'
        )
    assert "unity" in pol.plugins.enabled
    assert pol.plugin_setting("unity", "editor_mode") == "per_worktree"
    assert pol.plugin_setting("unity", "mcp") == "coplaydev"
