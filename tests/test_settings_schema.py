"""Schema-driven settings: the TOML core schema stays in lockstep with the
policy dataclasses, every policy field is accounted for, the schema ships in an
installed context, and an enabled plugin's settings render + round-trip.

The literal->TOML swap is proven behavior-identical by the unchanged
test_tui_settings.py suite; this file guards the schema layer itself."""

from __future__ import annotations

import dataclasses
from importlib import resources

import pytest
from conftest import grant_plugin

from bmad_loop import policy as policy_mod
from bmad_loop.plugins import load_plugins
from bmad_loop.plugins.loader import PLUGIN_FILE, USER_PLUGINS_REL
from bmad_loop.policy import (
    BRANCH_PER_MODES,
    GATE_MODES,
    ISOLATION_MODES,
    MERGE_STRATEGIES,
    OPERATOR_ON_REVIEW_DEMOTION_MODES,
    PERMISSION_MODES,
    POLICY_TEMPLATE,
    RETRO_MODES,
    REVIEW_TRIGGER_MODES,
    SESSION_BUDGET_MODES,
    STAGES,
    STORIES_SOURCES,
    SWEEP_AUTO_MODES,
    TICKETS_MARK_DONE_MODES,
    TICKETS_THOROUGH_REVIEW_MODES,
    AdapterPolicy,
    CleanupPolicy,
    DevPolicy,
    EnvironmentPolicy,
    GatesPolicy,
    LimitsPolicy,
    MuxPolicy,
    NotifyPolicy,
    OperatorPolicy,
    PluginsPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    StageAdapterPolicy,
    StoriesPolicy,
    SweepPolicy,
    TicketsPolicy,
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
    "stories": StoriesPolicy,
    "tickets": TicketsPolicy,
    "limits": LimitsPolicy,
    "verify": VerifyPolicy,
    "environment": EnvironmentPolicy,
    "notify": NotifyPolicy,
    "adapter": AdapterPolicy,
    "adapter.dev": StageAdapterPolicy,
    "adapter.review": StageAdapterPolicy,
    "adapter.triage": StageAdapterPolicy,
    "adapter.retro": StageAdapterPolicy,
    "sweep": SweepPolicy,
    "scm": ScmPolicy,
    "cleanup": CleanupPolicy,
    "tui": TuiPolicy,
    "operator": OperatorPolicy,
    "mux": MuxPolicy,
    "dev": DevPolicy,
}

# Policy dataclasses that are deliberately NOT a settings section. Plugins render
# dynamically from their own manifests; Policy is the composed root. Every other
# *Policy dataclass must appear in SECTION_DC (see test_every_policy_dataclass_is_classified).
NON_SECTION_DC = {PluginsPolicy, Policy}

# select fields whose options are an enum set.
OPTIONS_ENUM = {
    ("gates", "mode"): GATE_MODES,
    ("gates", "retrospective"): RETRO_MODES,
    ("review", "trigger"): REVIEW_TRIGGER_MODES,
    ("operator", "on_review_demotion"): OPERATOR_ON_REVIEW_DEMOTION_MODES,
    ("stories", "source"): STORIES_SOURCES,
    ("tickets", "mark_done"): TICKETS_MARK_DONE_MODES,
    ("tickets", "thorough_review"): TICKETS_THOROUGH_REVIEW_MODES,
    ("limits", "session_budget_mode"): SESSION_BUDGET_MODES,
    ("sweep", "auto"): SWEEP_AUTO_MODES,
    ("scm", "isolation"): ISOLATION_MODES,
    ("scm", "branch_per"): BRANCH_PER_MODES,
    ("scm", "merge_strategy"): MERGE_STRATEGIES,
    ("adapter", "permission_mode"): PERMISSION_MODES,
    **{(f"adapter.{stage}", "permission_mode"): PERMISSION_MODES for stage in STAGES},
}

# Policy fields with no settings-screen control, by design. Each maps to a whole
# section (the adapter stage sub-tables) or is intentionally hidden.
HIDDEN = {
    ("gates", "on_escalation"),  # CRITICAL escalations always pause; field reserved
    ("review", "on_status_contradiction"),  # a revoked sign-off always escalates; one value
    ("sweep", "max_migration_attempts"),  # rare legacy-ledger path; no UI knob
    ("scm", "max_parallel"),  # inert until the parallel scheduler lands
    ("adapter", "dev"),  # rendered as the adapter.dev section
    ("adapter", "review"),  # rendered as the adapter.review section
    ("adapter", "triage"),  # rendered as the adapter.triage section
    ("adapter", "retro"),  # rendered as the adapter.retro section
    ("dev", "skill"),  # internal dev-skill seam; DEV_SKILLS has one legal value, no UI knob
    # Dashboard pane geometry — set by mouse-drag / the Ctrl+W resize mode, not a
    # form field, so no settings-screen control.
    ("tui", "left_width"),
    ("tui", "runs_height"),
    ("tui", "deferred_height"),
    ("tui", "tasks_height"),
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


def test_extra_args_fields_explain_the_bypass_replace_rule():
    """DW-349: `extra_args` REPLACES the profile's `bypass_args`, so every
    `extra_args` field (the base table and each stage) says so on the settings
    screen; the stage entry also names the client-match inheritance."""
    fields = {f.section: f for f in core_fields() if f.key == "extra_args"}
    assert set(fields) == {
        "adapter",
        "adapter.dev",
        "adapter.review",
        "adapter.triage",
        "adapter.retro",
    }
    for section, f in fields.items():
        assert "replaces" in f.description and "bypass_args" in f.description, section
    assert "only when the client matches" in fields["adapter.dev"].description


def test_select_options_match_enum_sets():
    for f in core_fields():
        if f.kind == "select" and (f.section, f.key) in OPTIONS_ENUM:
            expected = tuple(sorted(OPTIONS_ENUM[(f.section, f.key)]))
            assert f.options == expected, f"{f.section}.{f.key}"


def test_mux_backend_is_a_dynamic_select():
    """mux.backend is a dropdown populated at build time from the registered
    backends (not an enum set, so it stays out of OPTIONS_ENUM). tmux is always
    registered, and the default stays empty (== MuxPolicy.backend) so the blank
    auto-select choice is intact and the defaults-sync test still holds."""
    field = next(f for f in core_fields() if (f.section, f.key) == ("mux", "backend"))
    assert field.kind == "select"
    assert "tmux" in field.options
    assert field.default == MuxPolicy.backend == ""


def test_mux_backend_options_preserve_a_forced_value():
    """A backend forced in policy that isn't registered on this host is still
    offered, so an untouched save can't blank-and-delete it (a blank Select
    collects to None = delete the key)."""
    reg = build_registry(None, policy_mod.loads('[mux]\nbackend = "psmux"\n'))
    field = next(f for f in reg.fields() if (f.section, f.key) == ("mux", "backend"))
    assert "psmux" in field.options  # foreign forced value preserved
    assert "tmux" in field.options  # plus the registered backend


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


def test_every_policy_dataclass_is_classified():
    """No policy dataclass can silently escape the editor contract: every *Policy
    dataclass (and the composed root Policy) is either a settings section or
    explicitly not one, so adding a new policy table forces a conscious editor
    decision instead of leaving it unreachable and unchecked (as [dev] once was)."""
    policy_dcs = {
        obj
        for name, obj in vars(policy_mod).items()
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) and name.endswith("Policy")
    }
    classified = set(SECTION_DC.values()) | NON_SECTION_DC
    assert policy_dcs <= classified, f"unclassified policy dataclasses: {policy_dcs - classified}"


def test_core_widget_ids_stay_section_dash_key():
    """Core fields carry no explicit id: their widget ids stay ``<section>-<key>``
    with dots dashed — only plugin fields go through `plugin_field_wid`."""
    specs = [f for s in load_core_schema() for f in s.fields]
    assert all(f.id == "" for f in specs)
    ids = {f.widget_id for f in specs}
    assert {"limits-max_review_cycles", "adapter-review-model"} <= ids


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
    assert (example.origin, example.grant_required, example.grant_status) == (
        "builtin",
        False,
        "n/a",
    )
    assert [f.key for f in example.fields] == ["greeting"]
    greeting = example.fields[0]
    assert greeting.section == "plugins.example"
    assert greeting.widget_id == "plugins-example-greeting"  # safe name and key read as-is
    assert greeting.kind == "str" and greeting.default == "hello"
    # plugin_schemas cover every plugin contributing settings, so [plugins.<name>]
    # tables are typed on save even when the plugin is not enabled.
    assert "example" in reg.plugin_schemas


def _write_project_plugin(root, name, body):
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    (pdir / PLUGIN_FILE).write_text(f'[plugin]\nname = "{name}"\napi_version = 1\n{body}')
    return pdir


def test_project_plugin_with_hooks_needs_a_grant(tmp_path):
    """A project plugin with an executable surface carries a toggle — its grant —
    even with no [python] module, and reports its grant status: ungranted, then
    granted under a matching grant, then mismatch once its bytes move.

    ABLATION: compute `trust_needed` as `python is not None` for every origin and
    the declarative project plugin loses its toggle."""
    pdir = _write_project_plugin(tmp_path, "hooky", '[hooks.pre_run]\ncmd = "echo hi"\n')
    info = next(p for p in build_registry(tmp_path, Policy()).plugins if p.name == "hooky")
    assert (info.origin, info.trust_needed, info.grant_required) == ("project", True, True)
    assert info.grant_status == "ungranted" and not info.has_grant

    grants = grant_plugin(tmp_path, "hooky")
    reg = build_registry(tmp_path, Policy(), grants=grants)
    assert next(p for p in reg.plugins if p.name == "hooky").grant_status == "granted"
    (pdir / "extra.sh").write_text("echo new\n")
    reg = build_registry(tmp_path, Policy(), grants=grants)
    changed = next(p for p in reg.plugins if p.name == "hooky")
    assert changed.grant_status == "mismatch" and changed.has_grant


def test_settings_only_project_plugin_needs_no_grant(tmp_path):
    """A project plugin with nothing executable loads freely: no toggle, no grant."""
    _write_project_plugin(
        tmp_path, "cfgonly", '[[settings]]\nkey = "k"\ntype = "str"\ndefault = "v"\n'
    )
    info = next(p for p in build_registry(tmp_path, Policy()).plugins if p.name == "cfgonly")
    assert (info.origin, info.trust_needed, info.grant_required) == ("project", False, False)
    assert info.grant_status == "n/a"
    assert [f.key for f in info.fields] == ["k"]


def test_build_registry_discovers_from_root(tmp_path):
    """``root`` (the code root a run loads) is where plugins are discovered."""
    _write_project_plugin(tmp_path / "code", "hooky", '[hooks.pre_run]\ncmd = "echo hi"\n')
    names = {p.name for p in build_registry(tmp_path, Policy(), root=tmp_path / "code").plugins}
    assert "hooky" in names
    assert "hooky" not in {p.name for p in build_registry(tmp_path, Policy()).plugins}


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
    assert (unity.origin, unity.grant_required) == ("builtin", False)  # enabled, not granted
    assert unity.enabled is False
    keys = [f.key for f in unity.fields]
    assert keys == [
        "editor_mode",
        "mcp",
        "unity_path",
        "ready_timeout_sec",
        "ready_grace_sec",
        "install_scene_guard",
        "scene_guard_dir",
        "quiesce_on_rollback",
        "quiesce_timeout_sec",
        "dialog_probe",
        "dialog_probe_interval_sec",
        "dialog_probe_notify",
    ]
    editor_mode = next(f for f in unity.fields if f.key == "editor_mode")
    assert editor_mode.kind == "select"
    assert editor_mode.options == ("shared", "per_worktree")
    assert editor_mode.widget_id == "plugins-unity-editor_5fmode"  # plugin_field_wid-encoded


def test_named_engine_block_is_refused_with_the_unity_plugin_migration():
    """The unity settings live only under [plugins.unity]: a named [engine] block is
    refused with the migration, never read onto the plugin."""
    with pytest.raises(policy_mod.PolicyError, match=r"\[plugins\.unity\]"):
        policy_mod.loads('[engine]\nname = "unity"\neditor_mode = "per_worktree"\n')
