import pytest

from bmad_loop import policy


def test_defaults_when_file_missing(tmp_path):
    pol = policy.load(tmp_path / "nope.toml")
    assert pol.gates.mode == "per-epic"
    assert pol.limits.max_review_cycles == 3
    assert pol.adapter.name == "claude"
    assert pol.adapter.extra_args is None  # None = use the profile's bypass flags
    assert pol.dev.skill == "bmad-dev-auto"  # the sole supported dev skill


def test_dev_skill_select_and_validate():
    assert policy.loads('[dev]\nskill = "bmad-dev-auto"\n').dev.skill == "bmad-dev-auto"
    assert policy.loads("").dev.skill == "bmad-dev-auto"
    with pytest.raises(policy.PolicyError, match="dev.skill"):
        policy.loads('[dev]\nskill = "nope"\n')
    # the retired legacy fork is no longer an accepted value
    with pytest.raises(policy.PolicyError, match="dev.skill"):
        policy.loads('[dev]\nskill = "bmad-loop-dev"\n')


def test_review_enabled_default_and_parse():
    assert policy.loads("").review.enabled is True
    assert policy.loads("[review]\nenabled = false\n").review.enabled is False


def test_review_trigger_default_and_parse():
    assert policy.loads("").review.trigger == "recommended"
    assert policy.loads('[review]\ntrigger = "always"\n').review.trigger == "always"


def test_review_trigger_invalid():
    with pytest.raises(policy.PolicyError, match="review.trigger"):
        policy.loads('[review]\ntrigger = "sometimes"\n')


def test_cleanup_session_on_finish_default_and_override(tmp_path):
    assert policy.load(None).adapter.cleanup_session_on_finish is True
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
cleanup_session_on_finish = false
""")
    assert policy.load(p).adapter.cleanup_session_on_finish is False


def test_load_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[gates]
mode = "none"
[limits]
max_review_cycles = 5
[verify]
commands = ["pytest -q"]
[adapter]
model = "haiku"
extra_args = ["--permission-mode", "plan"]
""")
    pol = policy.load(p)
    assert pol.gates.mode == "none"
    assert pol.limits.max_review_cycles == 5
    assert pol.limits.max_dev_attempts == 2  # default survives partial table
    assert pol.verify.commands == ("pytest -q",)
    assert pol.adapter.model == "haiku"
    assert pol.adapter.extra_args == ("--permission-mode", "plan")
    # no stage tables: both roles resolve to the base
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter(
        "claude", "haiku", ("--permission-mode", "plan")
    )
    assert pol.adapter.resolved("review").model == "haiku"


def test_stage_overrides_and_inheritance(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
name = "claude"
model = "opus"
extra_args = ["--permission-mode", "plan"]
[adapter.review]
name = "codex"
model = "gpt-5-codex"
""")
    pol = policy.load(p)
    dev = pol.adapter.resolved("dev")
    assert dev == policy.ResolvedAdapter("claude", "opus", ("--permission-mode", "plan"))
    review = pol.adapter.resolved("review")
    assert review.name == "codex"
    assert review.model == "gpt-5-codex"
    # client switch: claude-specific extra_args must not leak into codex
    assert review.extra_args is None


def test_stage_client_switch_drops_base_model_and_extra_args(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
name = "claude"
model = "opus"
extra_args = ["--permission-mode", "plan"]
[adapter.review]
name = "codex"
""")
    review = policy.load(p).adapter.resolved("review")
    assert review == policy.ResolvedAdapter("codex", "", None)


def test_stage_same_client_inherits_and_overrides(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
model = "opus"
[adapter.dev]
model = ""
[adapter.review]
extra_args = ["--foo"]
""")
    pol = policy.load(p)
    # explicit empty model in the stage table means "CLI default", beating the base
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter("claude", "", None)
    assert pol.adapter.resolved("review") == policy.ResolvedAdapter("claude", "opus", ("--foo",))


def test_unknown_role_resolves_to_base(tmp_path):
    pol = policy.load(None)
    assert pol.adapter.resolved("retro") == policy.ResolvedAdapter("claude", "", None)


def test_adapter_timing_knobs_base_and_per_stage(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[adapter]
name = "copilot"
usage_grace_s = 3.5
[adapter.review]
stop_without_result_nudges = 7
""")
    pol = policy.load(p)
    assert pol.adapter.usage_grace_s == 3.5
    assert pol.adapter.stop_without_result_nudges is None
    # base usage_grace_s inherits into every stage; review adds a nudge override
    review = pol.adapter.resolved("review")
    assert review.usage_grace_s == 3.5
    assert review.stop_without_result_nudges == 7
    # dev inherits the base grace and leaves nudges unset (= fall back to profile/global)
    dev = pol.adapter.resolved("dev")
    assert dev.usage_grace_s == 3.5
    assert dev.stop_without_result_nudges is None


def test_adapter_timing_knobs_default_none(tmp_path):
    # unset = None on both base and stages, so the adapter falls back to the profile
    pol = policy.load(None)
    assert pol.adapter.usage_grace_s is None
    assert pol.adapter.stop_without_result_nudges is None
    assert pol.adapter.resolved("dev").usage_grace_s is None
    assert pol.adapter.resolved("dev").stop_without_result_nudges is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[adapter]\nusage_grace_s = -1\n", r"adapter\.usage_grace_s"),
        ("[adapter]\nstop_without_result_nudges = -1\n", r"adapter\.stop_without_result_nudges"),
        ("[adapter.review]\nusage_grace_s = -1\n", r"adapter\.review\.usage_grace_s"),
        (
            "[adapter.review]\nstop_without_result_nudges = -1\n",
            r"adapter\.review\.stop_without_result_nudges",
        ),
    ],
)
def test_adapter_timing_knobs_reject_negatives(tmp_path, body, match):
    p = tmp_path / "policy.toml"
    p.write_text(body)
    with pytest.raises(policy.PolicyError, match=match):
        policy.load(p)


def test_legacy_model_keys_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\nmodel_dev = "haiku"\n')
    with pytest.raises(policy.PolicyError, match=r"adapter\.model_dev"):
        policy.load(p)
    p.write_text('[adapter]\nmodel_review = "haiku"\n')
    with pytest.raises(policy.PolicyError, match=r"adapter\.model_review"):
        policy.load(p)


def test_stage_scalar_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\ndev = "opus"\n')
    with pytest.raises(policy.PolicyError, match=r"\[adapter\.dev\] must be a table"):
        policy.load(p)


def test_invalid_gate_mode(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[gates]\nmode = "sometimes"\n')
    with pytest.raises(policy.PolicyError, match="gates.mode"):
        policy.load(p)


def test_bad_toml(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[gates\nmode=")
    with pytest.raises(policy.PolicyError, match="invalid policy TOML"):
        policy.load(p)


def test_loads_defaults_and_text():
    assert policy.loads("").gates.mode == policy.GatesPolicy.mode
    assert policy.loads('[gates]\nmode = "none"\n').gates.mode == "none"


def test_loads_validates():
    with pytest.raises(policy.PolicyError, match="gates.mode"):
        policy.loads('[gates]\nmode = "sometimes"\n')


def test_load_prefixes_path_in_errors(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[gates]\nmode = "sometimes"\n')
    with pytest.raises(policy.PolicyError, match=r"policy\.toml.*gates\.mode"):
        policy.load(p)


def test_zero_budget_rejected(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[limits]\nmax_dev_attempts = 0\n")
    with pytest.raises(policy.PolicyError):
        policy.load(p)


def test_workflow_stall_nudges_cap_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.workflow_stall_nudges_cap == 3
    loaded = policy.loads("[limits]\nworkflow_stall_nudges_cap = 0\n")
    assert loaded.limits.workflow_stall_nudges_cap == 0
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert (
        doc["limits"]["workflow_stall_nudges_cap"] == policy.LimitsPolicy.workflow_stall_nudges_cap
    )
    with pytest.raises(policy.PolicyError, match="workflow_stall_nudges_cap"):
        policy.loads("[limits]\nworkflow_stall_nudges_cap = -1\n")


def test_cache_read_weight_default_and_override(tmp_path):
    assert policy.load(None).limits.cache_read_weight == 0.1
    p = tmp_path / "policy.toml"
    p.write_text("[limits]\ncache_read_weight = 1.0\n")
    assert policy.load(p).limits.cache_read_weight == 1.0
    p.write_text("[limits]\ncache_read_weight = 1.5\n")
    with pytest.raises(policy.PolicyError, match="cache_read_weight"):
        policy.load(p)


def test_sweep_defaults_and_override(tmp_path):
    pol = policy.load(None)
    assert pol.sweep.auto == "never"
    assert pol.sweep.max_bundles == 5
    assert pol.sweep.max_triage_attempts == 2
    assert pol.sweep.repeat is False
    assert pol.sweep.max_cycles == 5
    p = tmp_path / "policy.toml"
    p.write_text('[sweep]\nauto = "run-end"\nmax_bundles = 2\nrepeat = true\nmax_cycles = 3\n')
    pol = policy.load(p)
    assert pol.sweep.auto == "run-end"
    assert pol.sweep.max_bundles == 2
    assert pol.sweep.repeat is True
    assert pol.sweep.max_cycles == 3


def test_sweep_invalid_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[sweep]\nauto = "always"\n')
    with pytest.raises(policy.PolicyError, match="sweep.auto"):
        policy.load(p)
    p.write_text("[sweep]\nmax_bundles = 0\n")
    with pytest.raises(policy.PolicyError, match="max_bundles"):
        policy.load(p)
    p.write_text("[sweep]\nmax_cycles = 0\n")
    with pytest.raises(policy.PolicyError, match="max_cycles"):
        policy.load(p)


def test_triage_stage_adapter(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[adapter]\nmodel = "opus"\n[adapter.triage]\nmodel = "sonnet"\n')
    pol = policy.load(p)
    assert pol.adapter.resolved("triage").model == "sonnet"
    assert pol.adapter.resolved("dev").model == "opus"
    # without a stage table, triage inherits the base
    assert policy.load(None).adapter.resolved("triage") == policy.ResolvedAdapter(
        "claude", "", None
    )


def test_triage_client_switch_uses_profile_defaults(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[adapter]\nmodel = "opus"\nextra_args = ["--foo"]\n[adapter.triage]\nname = "gemini"\n'
    )
    pol = policy.load(p)
    # base model/extra_args are client-specific and must not follow a client switch
    assert pol.adapter.resolved("triage") == policy.ResolvedAdapter("gemini", "", None)
    assert pol.adapter.resolved("dev") == policy.ResolvedAdapter("claude", "opus", ("--foo",))


def test_review_enabled_default_and_override(tmp_path):
    assert policy.load(None).review.enabled is True
    p = tmp_path / "policy.toml"
    p.write_text("[review]\nenabled = false\n")
    assert policy.load(p).review.enabled is False


def test_scm_defaults_reproduce_today(tmp_path):
    pol = policy.load(None)
    assert pol.scm.isolation == "none"
    assert pol.scm.branch_per == "story"
    assert pol.scm.target_branch == ""
    assert pol.scm.merge_strategy == "merge"
    assert pol.scm.delete_branch is True
    assert pol.scm.keep_failed is True
    assert pol.scm.failed_diff_max_mb == 5
    assert pol.scm.failed_diff_unlimited is False
    assert pol.scm.commit_message_template == ""
    assert pol.scm.max_parallel == 1
    # worktree config-seeding is on by default with no extra paths
    assert pol.scm.seed_adapter_defaults is True
    assert pol.scm.worktree_seed == ()


def test_scm_worktree_seed_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        "[scm]\nseed_adapter_defaults = false\n" 'worktree_seed = [".mcp.json", ".envrc"]\n'
    )
    pol = policy.load(p)
    assert pol.scm.seed_adapter_defaults is False
    assert pol.scm.worktree_seed == (".mcp.json", ".envrc")


def test_scm_override(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[scm]\nisolation = "worktree"\nbranch_per = "story"\n'
        'target_branch = "integration"\nmerge_strategy = "squash"\n'
        "delete_branch = false\nkeep_failed = false\n"
        'commit_message_template = "feat: {story_key} ({run_id})"\n'
    )
    pol = policy.load(p)
    assert pol.scm.isolation == "worktree"
    assert pol.scm.branch_per == "story"
    assert pol.scm.target_branch == "integration"
    assert pol.scm.merge_strategy == "squash"
    assert pol.scm.delete_branch is False
    assert pol.scm.keep_failed is False
    assert pol.scm.commit_message_template == "feat: {story_key} ({run_id})"


def test_scm_branch_per_run_forces_delete_branch_off(tmp_path):
    # branch_per="run" shares one branch across the run; deleting it after each
    # merge would defeat that, so delete_branch is coerced off even if set true.
    p = tmp_path / "policy.toml"
    p.write_text('[scm]\nbranch_per = "run"\ndelete_branch = true\n')
    assert policy.load(p).scm.delete_branch is False


def test_scm_max_parallel_clamped_to_one(tmp_path):
    # Parallel fan-out (Phase 5) is unbuilt: the knob is accepted and validated
    # but any value > 1 is clamped to 1 so it stays inert.
    p = tmp_path / "policy.toml"
    p.write_text("[scm]\nmax_parallel = 4\n")
    assert policy.load(p).scm.max_parallel == 1
    p.write_text("[scm]\nmax_parallel = 0\n")
    with pytest.raises(policy.PolicyError, match="scm.max_parallel"):
        policy.load(p)


def test_scm_failed_diff_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[scm]\nfailed_diff_max_mb = 25\nfailed_diff_unlimited = true\n")
    pol = policy.load(p)
    assert pol.scm.failed_diff_max_mb == 25
    assert pol.scm.failed_diff_unlimited is True
    # the cap must be a positive size
    p.write_text("[scm]\nfailed_diff_max_mb = 0\n")
    with pytest.raises(policy.PolicyError, match="scm.failed_diff_max_mb"):
        policy.load(p)


def test_scm_invalid_values(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[scm]\nisolation = "vm"\n')
    with pytest.raises(policy.PolicyError, match="scm.isolation"):
        policy.load(p)
    p.write_text('[scm]\nbranch_per = "epic"\n')
    with pytest.raises(policy.PolicyError, match="scm.branch_per"):
        policy.load(p)
    p.write_text('[scm]\nmerge_strategy = "rebase"\n')
    with pytest.raises(policy.PolicyError, match="scm.merge_strategy"):
        policy.load(p)


# The game-engine layer is now the "unity" plugin. A legacy [engine] block still
# loads — with a deprecation warning — by folding onto [plugins] + [plugins.unity].
# The editor_mode↔scm.isolation coupling moved to the plugin (UnityPlugin.validate,
# exercised in test_engine_plugin.py); policy.loads no longer enforces it.


def test_no_engine_block_by_default():
    pol = policy.load(None)
    assert pol.plugins.enabled == ()
    assert pol.plugins.settings == {}


def test_deprecated_engine_folds_to_unity_plugin(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("""
[engine]
name = "unity"
editor_mode = "shared"
mcp = "coplaydev"
unity_path = "/opt/Unity/Editor/Unity"
ready_timeout_sec = 120
ready_grace_sec = 90
""")
    with pytest.warns(DeprecationWarning):
        pol = policy.load(p)
    assert "unity" in pol.plugins.enabled
    assert pol.plugin_setting("unity", "mcp") == "coplaydev"
    assert pol.plugin_setting("unity", "unity_path") == "/opt/Unity/Editor/Unity"
    assert pol.plugin_setting("unity", "ready_timeout_sec") == 120
    assert pol.plugin_setting("unity", "ready_grace_sec") == 90


def test_deprecated_engine_disabled_when_name_empty(tmp_path):
    # name = "" was the old "disabled" state: warn, but enable nothing.
    p = tmp_path / "policy.toml"
    p.write_text('[engine]\neditor_mode = "shared"\n[scm]\nisolation = "worktree"\n')
    with pytest.warns(DeprecationWarning):
        pol = policy.load(p)
    assert pol.plugins.enabled == ()


def test_explicit_plugin_settings_win_over_folded_engine(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(
        '[engine]\nname = "unity"\nmcp = "ivanmurzak"\n' '[plugins.unity]\nmcp = "coplaydev"\n'
    )
    with pytest.warns(DeprecationWarning):
        pol = policy.load(p)
    assert pol.plugin_setting("unity", "mcp") == "coplaydev"


def test_template_parses():
    import tomllib

    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["gates"]["mode"] == "per-epic"
    assert doc["review"]["enabled"] is True
    assert doc["scm"]["isolation"] == "none"
    assert "engine" not in doc  # the game-engine layer is now a plugin
    assert doc["plugins"]["enabled"] == []


def test_to_dict_roundtrips_for_snapshot():
    pol = policy.load(None)
    snapshot = pol.to_dict()
    assert snapshot["limits"]["max_review_cycles"] == 3
