import json
import sys

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


def test_review_on_timeout_default_and_parse():
    assert policy.loads("").review.on_timeout == "retry"
    for mode in ("salvage-if-done", "defer"):
        assert policy.loads(f'[review]\non_timeout = "{mode}"\n').review.on_timeout == mode


def test_review_on_timeout_invalid():
    with pytest.raises(policy.PolicyError, match="review.on_timeout"):
        policy.loads('[review]\non_timeout = "salvage"\n')


def test_review_on_status_contradiction_default_parse_and_template():
    import tomllib

    # default is the new behavior: the released retry-until-budget loop is the
    # defect (#334), "retry" is the compatibility opt-out.
    assert policy.loads("").review.on_status_contradiction == "escalate"
    for mode in sorted(policy.REVIEW_ON_STATUS_CONTRADICTION_MODES):
        loaded = policy.loads(f'[review]\non_status_contradiction = "{mode}"\n')
        assert loaded.review.on_status_contradiction == mode
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["review"]["on_status_contradiction"] == policy.ReviewPolicy.on_status_contradiction


def test_review_on_status_contradiction_invalid():
    with pytest.raises(policy.PolicyError, match=r"review\.on_status_contradiction"):
        policy.loads('[review]\non_status_contradiction = "defer"\n')


def test_stories_defaults():
    pol = policy.loads("")
    assert pol.stories.source == "sprint-status"
    assert pol.stories.spec_folder == ""


def test_stories_parse_and_folder():
    pol = policy.loads('[stories]\nsource = "stories"\nspec_folder = "_bmad-output/epic-1"\n')
    assert pol.stories.source == "stories"
    assert pol.stories.spec_folder == "_bmad-output/epic-1"


def test_stories_source_invalid():
    with pytest.raises(policy.PolicyError, match="stories.source"):
        policy.loads('[stories]\nsource = "manifest"\n')


def test_stories_mode_requires_spec_folder():
    with pytest.raises(policy.PolicyError, match="requires stories.spec_folder"):
        policy.loads('[stories]\nsource = "stories"\n')


def test_stories_spec_folder_under_sprint_mode_is_tolerated():
    # a leftover spec_folder while source stays sprint-status is not an error —
    # it's ignored at run time, so flipping source back and forth keeps the path.
    pol = policy.loads('[stories]\nspec_folder = "_bmad-output/epic-1"\n')
    assert pol.stories.source == "sprint-status"
    assert pol.stories.spec_folder == "_bmad-output/epic-1"


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


def _roundtrip_snapshot(pol):
    # RunState.policy_snapshot is the json-round-tripped asdict(Policy).
    return json.loads(json.dumps(pol.to_dict()))


@pytest.mark.parametrize(
    "body",
    [
        # (a) base-only config
        '[adapter]\nname = "claude"\nmodel = "opus"\nextra_args = ["--permission-mode", "plan"]\n',
        # (b) a stage model override (same client keeps the base model inheritable)
        '[adapter]\nname = "claude"\nmodel = "opus"\n[adapter.dev]\nmodel = "haiku"\n',
        # (c) a stage name override — the client switch resets model to ""
        '[adapter]\nname = "claude"\nmodel = "opus"\n'
        'extra_args = ["--permission-mode", "plan"]\n[adapter.review]\nname = "codex"\n',
    ],
)
def test_adapter_policy_from_snapshot_roundtrips_resolved(body):
    # a snapshot rebuild resolves identically to the live policy for every role,
    # so downstream display paths can reuse AdapterPolicy.resolved() verbatim.
    pol = policy.loads(body)
    rebuilt = policy.adapter_policy_from_snapshot(_roundtrip_snapshot(pol))
    assert rebuilt is not None
    for role in ("dev", "review", "triage"):
        assert rebuilt.resolved(role) == pol.adapter.resolved(role)


def test_adapter_policy_from_snapshot_extra_args_back_to_tuple():
    # asdict turns extra_args into a list; the rebuild must restore the tuple so
    # the reconstruction compares equal to a freshly-parsed policy (#189 trap).
    pol = policy.loads('[adapter]\nname = "claude"\nextra_args = ["--foo", "--bar"]\n')
    rebuilt = policy.adapter_policy_from_snapshot(_roundtrip_snapshot(pol))
    assert rebuilt is not None
    assert isinstance(rebuilt.extra_args, tuple)
    assert rebuilt.extra_args == ("--foo", "--bar")


@pytest.mark.parametrize(
    "snapshot",
    [
        None,  # no snapshot at all
        {},  # snapshot without an adapter table
        {"adapter": "garbage"},  # adapter present but not a table
        {"adapter": {}},  # adapter table with no name -> would falsely display "claude"
    ],
)
def test_adapter_policy_from_snapshot_returns_none(snapshot):
    assert policy.adapter_policy_from_snapshot(snapshot) is None


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


def test_non_utf8_file_raises_policy_error(tmp_path):
    """An undecodable policy.toml is as much a PolicyError as a malformed one.
    `read_text` raises UnicodeDecodeError, which is a ValueError and NOT an OSError,
    so raw it slips past every `except (PolicyError, OSError)` degrade handler —
    `cli._configure_mux` (which runs before argument dispatch on every command),
    `tui/app.py`, and the dashboard constructor — and kills the process instead of
    falling back to defaults. Asserting the type is the point: UnicodeDecodeError is
    an exception too."""
    p = tmp_path / "policy.toml"
    p.write_bytes(b'[gates]\nmode = "\xff\xfe"\n')
    with pytest.raises(policy.PolicyError, match="not valid UTF-8"):
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


def test_git_timeout_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.git_timeout_s == 120
    assert policy.loads("[limits]\ngit_timeout_s = 600\n").limits.git_timeout_s == 600
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["git_timeout_s"] == 120


@pytest.mark.parametrize("bad", [0, -5])
def test_git_timeout_must_be_positive(bad):
    with pytest.raises(policy.PolicyError, match=r"limits\.git_timeout_s"):
        policy.loads(f"[limits]\ngit_timeout_s = {bad}\n")


def test_teardown_grace_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.teardown_grace_s == 20
    assert policy.loads("[limits]\nteardown_grace_s = 45\n").limits.teardown_grace_s == 45
    # 0 is legal: the rollback lever back to the single unverified best-effort kill
    assert policy.loads("[limits]\nteardown_grace_s = 0\n").limits.teardown_grace_s == 0
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["teardown_grace_s"] == policy.LimitsPolicy.teardown_grace_s


def test_teardown_grace_must_be_nonnegative():
    with pytest.raises(policy.PolicyError, match=r"limits\.teardown_grace_s"):
        policy.loads("[limits]\nteardown_grace_s = -1\n")


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


def test_dev_stall_nudges_cap_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.dev_stall_nudges_cap == 6
    loaded = policy.loads("[limits]\ndev_stall_nudges_cap = 0\n")
    assert loaded.limits.dev_stall_nudges_cap == 0
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["dev_stall_nudges_cap"] == policy.LimitsPolicy.dev_stall_nudges_cap
    with pytest.raises(policy.PolicyError, match="dev_stall_nudges_cap"):
        policy.loads("[limits]\ndev_stall_nudges_cap = -1\n")


def test_dev_contract_nudge_default_parse_and_template():
    import tomllib

    # defaults on; overridable to false (no range validation — it is a bool)
    assert policy.loads("").limits.dev_contract_nudge is True
    loaded = policy.loads("[limits]\ndev_contract_nudge = false\n")
    assert loaded.limits.dev_contract_nudge is False
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["dev_contract_nudge"] == policy.LimitsPolicy.dev_contract_nudge


def test_session_budget_mode_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.session_budget_mode == "warn"
    for mode in sorted(policy.SESSION_BUDGET_MODES):
        loaded = policy.loads(f'[limits]\nsession_budget_mode = "{mode}"\n')
        assert loaded.limits.session_budget_mode == mode
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["session_budget_mode"] == policy.LimitsPolicy.session_budget_mode


def test_invalid_session_budget_mode():
    with pytest.raises(policy.PolicyError, match=r"limits\.session_budget_mode"):
        policy.loads('[limits]\nsession_budget_mode = "sometimes"\n')


def test_max_tokens_per_story_default_and_parse():
    assert policy.loads("").limits.max_tokens_per_story == 2_000_000
    assert policy.loads("[limits]\nmax_tokens_per_story = 500\n").limits.max_tokens_per_story == 500


@pytest.mark.parametrize("bad", [0, -1])
def test_max_tokens_per_story_must_be_positive(bad):
    """Documented as int >= 1 (core.toml `minimum = 1`), but the parser used a
    bare int() and took 0 — which now warns on every story at its first session
    boundary rather than once post-done."""
    with pytest.raises(policy.PolicyError, match=r"limits\.max_tokens_per_story"):
        policy.loads(f"[limits]\nmax_tokens_per_story = {bad}\n")


@pytest.mark.parametrize("bad", ["true", "2.5", '"2M"'])
def test_max_tokens_per_story_rejects_non_integers(bad):
    """Same rule as the per-session cap: `true` coerced to a 1-token story cap."""
    with pytest.raises(policy.PolicyError, match=r"limits\.max_tokens_per_story"):
        policy.loads(f"[limits]\nmax_tokens_per_story = {bad}\n")


def test_max_tokens_per_session_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.max_tokens_per_session == 4_000_000
    loaded = policy.loads("[limits]\nmax_tokens_per_session = 123\n")
    assert loaded.limits.max_tokens_per_session == 123
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["max_tokens_per_session"] == policy.LimitsPolicy.max_tokens_per_session


@pytest.mark.parametrize("bad", [0, -5])
def test_max_tokens_per_session_must_be_positive(bad):
    with pytest.raises(policy.PolicyError, match=r"limits\.max_tokens_per_session"):
        policy.loads(f"[limits]\nmax_tokens_per_session = {bad}\n")


@pytest.mark.parametrize("bad", ["true", "2.5", '"4M"'])
def test_max_tokens_per_session_rejects_non_integers(bad):
    """Bools/floats/strings raise PolicyError, never coerce (true would become
    1 token and terminate every enforce-mode session at its first sample)."""
    with pytest.raises(policy.PolicyError, match=r"limits\.max_tokens_per_session"):
        policy.loads(f"[limits]\nmax_tokens_per_session = {bad}\n")


@pytest.mark.parametrize("bad", ["true", "2.5", '"30s"'])
def test_session_budget_grace_rejects_non_integers(bad):
    with pytest.raises(policy.PolicyError, match=r"limits\.session_budget_grace_s"):
        policy.loads(f"[limits]\nsession_budget_grace_s = {bad}\n")


def test_session_budget_grace_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.session_budget_grace_s == 240
    loaded = policy.loads("[limits]\nsession_budget_grace_s = 30\n")
    assert loaded.limits.session_budget_grace_s == 30
    # 0 is legal: terminate at trip, no wrap-up nudge
    assert policy.loads("[limits]\nsession_budget_grace_s = 0\n").limits.session_budget_grace_s == 0
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["session_budget_grace_s"] == policy.LimitsPolicy.session_budget_grace_s


def test_session_budget_grace_must_be_nonnegative():
    with pytest.raises(policy.PolicyError, match=r"limits\.session_budget_grace_s"):
        policy.loads("[limits]\nsession_budget_grace_s = -1\n")


def test_max_followup_reviews_default_parse_and_template():
    import tomllib

    assert policy.loads("").limits.max_followup_reviews == 1  # default: honor one follow-up
    assert policy.loads("[limits]\nmax_followup_reviews = 0\n").limits.max_followup_reviews == 0
    assert policy.loads("[limits]\nmax_followup_reviews = 3\n").limits.max_followup_reviews == 3
    # the emitted template documents the knob at its dataclass default
    doc = tomllib.loads(policy.POLICY_TEMPLATE)
    assert doc["limits"]["max_followup_reviews"] == policy.LimitsPolicy.max_followup_reviews
    # >= 0 validation is a separate check from the neighbors' >= 1 requirement
    with pytest.raises(policy.PolicyError, match="max_followup_reviews"):
        policy.loads("[limits]\nmax_followup_reviews = -1\n")


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
    assert pol.scm.preserve_keep == 20
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


@pytest.mark.parametrize(
    "entry",
    [
        "",  # the harmful one — see below
        ".",  # …and the SAME harm, one spelling later
        "./",
        "./.",
        ".\\",  # a root ref only Windows parsing normalizes away
        "/etc/passwd",  # POSIX-absolute
        "C:\\secrets",  # Windows drive-absolute (rejected on POSIX too)
        "../../etc",  # climbs out without being absolute
    ],
)
def test_scm_worktree_seed_rejects_non_project_relative_entries(tmp_path, entry):
    """`worktree_seed` was the only one of the three seed sources feeding
    provision_worktree that arrived unvalidated — profiles and plugin manifests
    both already apply this rule to their own entries.

    A ROOT-NAMING entry is the one that does damage rather than merely no-op: it
    makes the seed loop resolve src to the repo ROOT and dst to the worktree, both
    of which pass its containment checks, so the whole repo is copied in — and since
    a worktree mounts under the repo, that copy recurses into itself until the path
    length fails. The "/" it renders is inert (git strips a bare slash to a pattern
    matching nothing), so none of the surplus is even shielded from `git add -A`.

    `""` is only ONE spelling of the root, which is why the guard is
    `names_tree_root` and not an emptiness check. Measured: `""` and `"."` produce a
    byte-identical (src, raw, dst) triple in that loop, and a real run seeded with
    `["."]` copied an untracked `secret.env` into the worktree and self-recursed 127
    levels — with `provision_worktree` reporting no skipped entry at all.

    Ablation: restore `not seed` in place of `names_tree_root(seed)` and the four
    dot spellings fail while `""` keeps passing — which is what makes them worth
    parametrizing separately."""
    p = tmp_path / "policy.toml"
    p.write_text(f"[scm]\nworktree_seed = [{entry!r}]\n".replace("'", '"'))

    with pytest.raises(policy.PolicyError, match="worktree_seed entries must be project-relative"):
        policy.load(p)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ('""', "must be a list of paths"),  # iterates to an EMPTY tuple: silently inert
        ('"foo"', "must be a list of paths"),  # iterates to ('f','o','o')
        ("5", "must be a list of paths"),  # raised a bare TypeError out of loads()
        ("[1]", "entries must be strings"),  # str()'d into the path "1"
    ],
)
def test_scm_worktree_seed_rejects_value_shapes_that_are_not_a_list_of_paths(
    tmp_path, value, match
):
    """The per-entry guard runs over whatever `tuple(str(s) for s in raw)` produced,
    and that coercion accepts things a list of paths never is. A scalar string is
    the trap: TOML permits it, it iterates CHARACTER-WISE, and every character then
    passes the per-entry rule — so `worktree_seed = "foo"` seeded three
    one-character paths while reading as applied configuration. A scalar int did not
    even reach a PolicyError; it raised TypeError out of `loads`, untyped, where
    every other malformed value in this file escalates as PolicyError.

    Ablation: drop the shape check and all four cases pass (three silently, the int
    as a TypeError rather than the PolicyError this asserts)."""
    p = tmp_path / "policy.toml"
    p.write_text(f"[scm]\nworktree_seed = {value}\n")

    with pytest.raises(policy.PolicyError, match=match):
        policy.load(p)


def test_scm_worktree_seed_rejects_a_bad_entry_beside_good_ones(tmp_path):
    """Every entry is checked, not just the first: a valid leading entry must not
    let a later empty one through."""
    p = tmp_path / "policy.toml"
    p.write_text('[scm]\nworktree_seed = [".mcp.json", "", ".envrc"]\n')

    with pytest.raises(policy.PolicyError, match="got ''"):
        policy.load(p)


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


def test_scm_preserve_keep_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[scm]\npreserve_keep = 5\n")
    assert policy.load(p).scm.preserve_keep == 5
    # 0 = never prune (maximum safety) — valid
    p.write_text("[scm]\npreserve_keep = 0\n")
    assert policy.load(p).scm.preserve_keep == 0
    p.write_text("[scm]\npreserve_keep = -1\n")
    with pytest.raises(policy.PolicyError, match="scm.preserve_keep"):
        policy.load(p)
    # strict typing: bool/float/string must not coerce into a smaller budget
    for bad in ("true", "1.9", '"5"'):
        p.write_text(f"[scm]\npreserve_keep = {bad}\n")
        with pytest.raises(policy.PolicyError, match="scm.preserve_keep must be an integer"):
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


def test_verify_stream_capture_kb(tmp_path):
    """The retain cap parses, defaults, and admits 0 as "capture nothing" —
    unlike scm.failed_diff_max_mb, whose 0 is rejected. The opt-out is the whole
    point of the knob, so the floor is 0 and only a negative is refused."""
    p = tmp_path / "policy.toml"
    p.write_text("[verify]\nstream_capture_kb = 8\n")
    assert policy.load(p).verify.stream_capture_kb == 8
    p.write_text('[verify]\ncommands = ["pytest -q"]\n')
    assert policy.load(p).verify.stream_capture_kb == 256  # default survives a partial table
    p.write_text("[verify]\nstream_capture_kb = 0\n")
    assert policy.load(p).verify.stream_capture_kb == 0  # opting out is legal
    p.write_text("[verify]\nstream_capture_kb = -1\n")
    with pytest.raises(policy.PolicyError, match="verify.stream_capture_kb"):
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
    assert snapshot["limits"]["max_followup_reviews"] == 1


# ---------------------------------------------------------------------------
# [operator] — awaiting-operator parks (issue #335)


def test_operator_park_defaults_on():
    """Default-on because without it a story owing a human action has no honest
    outcome: `done` hides the outstanding work, `blocked` halts the run over work
    the loop was never going to do."""
    assert policy.loads("").operator.enabled is True


def test_operator_park_can_be_turned_off():
    assert policy.loads("[operator]\nenabled = false\n").operator.enabled is False


def test_operator_scalar_section_rejected():
    with pytest.raises(policy.PolicyError, match=r"\[operator\] must be a table"):
        policy.loads('operator = "on"\n')


def test_template_operator_block_parses_to_the_default():
    # unlike [mux]'s commented anchor, this key ships uncommented — the template
    # must therefore agree with the dataclass, not merely parse
    assert policy.loads(policy.POLICY_TEMPLATE).operator.enabled is True


# ---------------------------------------------------------------------------
# [mux] — machine-scoped terminal-multiplexer backend choice (issue #87)


def test_mux_defaults_to_auto():
    pol = policy.loads("")
    assert pol.mux.backend == ""


def test_mux_backend_parses_and_strips():
    pol = policy.loads('[mux]\nbackend = " psmux "\n')
    assert pol.mux.backend == "psmux"


def test_mux_backend_rejects_junk():
    with pytest.raises(policy.PolicyError, match="mux.backend"):
        policy.loads('[mux]\nbackend = "not a name!"\n')


def test_mux_scalar_section_rejected():
    with pytest.raises(policy.PolicyError, match=r"\[mux\] must be a table"):
        policy.loads('mux = "tmux"\n')


def test_template_mux_block_parses_to_defaults():
    pol = policy.loads(policy.POLICY_TEMPLATE)
    assert pol.mux.backend == ""  # the anchor line ships commented out


def test_write_mux_backend_uncomments_template_anchor(tmp_path):
    p = tmp_path / "policy.toml"
    policy.write_mux_backend(p, "psmux")
    text = p.read_text(encoding="utf-8")
    assert 'backend = "psmux"' in text
    assert policy.load(p).mux.backend == "psmux"
    # created from the template: full documentation retained
    assert "[gates]" in text and "[scm]" in text


def test_write_mux_backend_replaces_existing_value(tmp_path):
    p = tmp_path / "policy.toml"
    policy.write_mux_backend(p, "psmux")
    before = p.read_text(encoding="utf-8")
    policy.write_mux_backend(p, "tmux")
    after = p.read_text(encoding="utf-8")
    assert policy.load(p).mux.backend == "tmux"
    # a targeted line replace: everything but the anchor line is byte-identical
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert diff == [('backend = "psmux"', 'backend = "tmux"')]


def test_write_mux_backend_clear_recomments(tmp_path):
    p = tmp_path / "policy.toml"
    policy.write_mux_backend(p, "psmux")
    policy.write_mux_backend(p, None)
    assert policy.load(p).mux.backend == ""
    assert '# backend = "tmux"' in p.read_text(encoding="utf-8")


def test_write_mux_backend_appends_table_to_legacy_file(tmp_path):
    p = tmp_path / "policy.toml"
    legacy = '# my notes\n[gates]\nmode = "none"\n'
    p.write_text(legacy, encoding="utf-8")
    policy.write_mux_backend(p, "psmux")
    text = p.read_text(encoding="utf-8")
    assert text.startswith(legacy)  # untouched prefix, table appended at EOF
    pol = policy.load(p)
    assert pol.mux.backend == "psmux"
    assert pol.gates.mode == "none"


def test_write_mux_backend_reinserts_deleted_key_line(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[mux]\n# hand-trimmed file: no key line\n", encoding="utf-8")
    policy.write_mux_backend(p, "tmux")
    assert policy.load(p).mux.backend == "tmux"


def test_write_mux_backend_preserves_hand_edits(tmp_path):
    p = tmp_path / "policy.toml"
    hand = '[limits]\nmax_dev_attempts = 7  # keep my comment\n\n[mux]\nbackend = "old"\n'
    p.write_text(hand, encoding="utf-8")
    policy.write_mux_backend(p, "new")
    pol = policy.load(p)
    assert pol.mux.backend == "new"
    assert pol.limits.max_dev_attempts == 7
    assert "# keep my comment" in p.read_text(encoding="utf-8")


def test_write_mux_backend_preserves_trailing_comment_on_anchor_line(tmp_path):
    """A hand-added comment on the backend line itself survives a replace —
    'preserving every other byte' includes the anchor line's own comment."""
    p = tmp_path / "policy.toml"
    p.write_text('[mux]\nbackend = "old"  # pinned per teammate X\n', encoding="utf-8")
    policy.write_mux_backend(p, "new")
    text = p.read_text(encoding="utf-8")
    assert 'backend = "new"  # pinned per teammate X\n' in text
    assert policy.load(p).mux.backend == "new"


def test_write_mux_backend_clear_preserves_trailing_comment(tmp_path):
    """Clearing re-comments the line but keeps the hand-added trailing comment."""
    p = tmp_path / "policy.toml"
    p.write_text('[mux]\nbackend = "old"  # pinned per teammate X\n', encoding="utf-8")
    policy.write_mux_backend(p, None)
    text = p.read_text(encoding="utf-8")
    assert '# backend = "tmux"  # pinned per teammate X\n' in text
    assert policy.load(p).mux.backend == ""


def test_write_mux_backend_preserves_crlf_line_ending(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_bytes(b'[mux]\r\nbackend = "old"\r\n')
    policy.write_mux_backend(p, "new")
    assert b'backend = "new"\r\n' in p.read_bytes()


def test_write_mux_backend_hands_the_helper_bytes(tmp_path, monkeypatch):
    """#363. The CRLF pin above does NOT grade the bytes-vs-text choice on Linux:
    `atomic_write_text` passes `newline=None`, whose translation is a no-op wherever
    `os.linesep == "\\n"`, so swapping the text helper in reddens that row on WINDOWS
    ONLY and CI's Linux leg would call the swap green.

    This grades it platform-independently by inspecting the payload the helper is
    handed, upstream of any newline translation: bytes, carrying CRLF verbatim. The
    binding is WRAPPED rather than replaced so the real write still happens and the
    surrounding round-trip behaviour is unchanged.

    Ablation: swap `atomic_write_bytes` for `atomic_write_text` in
    `write_mux_backend` (dropping the `.encode`) and this reddens on every platform,
    on the `isinstance(..., bytes)` row. The exact-length assertion also pins
    "exactly one write", so a retry loop cannot creep in unnoticed."""
    seen: list[bytes] = []
    real = policy.atomic_write_bytes

    def record(path, data, *, follow_symlinks=True):
        seen.append(data)
        real(path, data, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(policy, "atomic_write_bytes", record)
    p = tmp_path / "policy.toml"
    p.write_bytes(b'[mux]\r\nbackend = "old"\r\n')

    policy.write_mux_backend(p, "new")

    assert len(seen) == 1
    assert isinstance(seen[0], bytes)
    assert b'backend = "new"\r\n' in seen[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_write_mux_backend_replaces_a_planted_symlink(tmp_path):
    """#363. The one row that grades this SITE's `follow_symlinks=False` argument
    rather than the helper's implementation of it — everything else about no-follow
    is pinned in test_platform_util.py, where the helper is called directly.

    It is behaviour-preserving first: `os.replace` never dereferenced this
    destination either, so passing the default True here would have CHANGED what
    the function does, not merely relaxed it. It is also the security choice —
    `runsetup` states a driven session can write `.bmad-loop/policy.toml`, so
    writing THROUGH a link planted at that name would hand the session a host-side
    write to any operator-writable path.

    Ablation: drop `follow_symlinks=False` at the `write_mux_backend` call and this
    reddens alone (the link survives and the planted target is rewritten). Without
    this row that mutation is a KNOWN GREEN ablation — no other consumer test
    plants a symlink at the paths these five sites write."""
    real = tmp_path / "someone-elses-file"
    real.write_bytes(b'[mux]\nbackend = "old"\n')
    link = tmp_path / "policy.toml"
    link.symlink_to(real)

    policy.write_mux_backend(link, "new")

    assert not link.is_symlink()  # the NAME was replaced
    assert policy.load(link).mux.backend == "new"
    assert real.read_bytes() == b'[mux]\nbackend = "old"\n'  # not written through


def test_write_mux_backend_write_failure_raises_and_keeps_the_file(tmp_path, monkeypatch):
    """#363. The hand-rolled temp this replaced was the fixed name
    `.bmad-loop/policy.toml.tmp`, which nothing gitignores — the init line is the
    anchored literal `.bmad-loop/policy.toml`, which does not cover a `.tmp` suffix —
    so a failed replace stranded an untracked file that held `worktree_clean` False.
    The helper removes its own temp on any raise, and the raise still propagates.

    Patched at policy's OWN binding: the helper writes through an `mkstemp` fd via
    `os.fdopen`, so a `Path.write_bytes` patch never fires and passes vacuously.

    Ablation A5: revert `write_mux_backend` to `tmp.write_bytes(...)` +
    `atomic_replace` and this reddens together with the bytes pin above — both as
    an AttributeError from `monkeypatch.setattr`, since the `atomic_write_bytes`
    binding they share disappears with the revert. The pre-existing CRLF row stays
    green, which is the point of the bytes pin: CRLF alone does not grade this."""
    p = tmp_path / "policy.toml"
    p.write_bytes(b'[mux]\nbackend = "old"\n')
    before = p.read_bytes()

    def boom(path, data, *, follow_symlinks=True):
        raise OSError("disk full")

    monkeypatch.setattr(policy, "atomic_write_bytes", boom)
    with pytest.raises(OSError, match="disk full"):
        policy.write_mux_backend(p, "new")

    assert p.read_bytes() == before
    assert b'backend = "new"' not in p.read_bytes()  # the mutation that must not land


def test_write_mux_backend_rejects_bad_name(tmp_path):
    p = tmp_path / "policy.toml"
    with pytest.raises(policy.PolicyError, match="mux.backend"):
        policy.write_mux_backend(p, "bad name!")
    assert not p.exists()  # rejected before any write


def test_write_mux_backend_refuses_broken_file(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[gates\nmode = ", encoding="utf-8")
    with pytest.raises(policy.PolicyError):
        policy.write_mux_backend(p, "tmux")
    assert p.read_text(encoding="utf-8") == "[gates\nmode = "  # never half-writes
