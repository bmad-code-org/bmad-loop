import pytest

from bmad_loop.adapters.profile import (
    ProfileError,
    discovered_profiles,
    get_profile,
    load_profiles,
)

MINIMAL_PROFILE = """
name = "mycli"
binary = "mycli"
bypass_args = ["--yes"]

[hooks]
dialect = "claude-settings-json"
config_path = ".mycli/settings.json"
events = { SessionStart = "SessionStart", Stop = "Stop" }
"""


HOOKLESS_PROFILE = """
name = "mycli-http"
binary = "mycli"

[hooks]
dialect = "none"
"""


def test_builtin_profiles_load():
    profiles = load_profiles()
    assert {"claude", "codex", "gemini", "opencode-http"} <= set(profiles)
    assert profiles["claude"].usage_parser == "claude-jsonl"
    assert profiles["codex"].hooks.dialect == "codex-hooks-json"
    assert "SessionEnd" not in profiles["codex"].hooks.events  # codex has no such hook
    assert profiles["gemini"].hooks.events["AfterAgent"] == "Stop"
    assert profiles["gemini"].launch_args == ("-i",)
    # claude reads .claude/skills; codex and gemini read .agents/skills
    assert profiles["claude"].skill_tree == ".claude/skills"
    assert profiles["codex"].skill_tree == ".agents/skills"
    assert profiles["gemini"].skill_tree == ".agents/skills"
    # each profile carries the gitignored configs a worktree checkout omits
    assert ".mcp.json" in profiles["claude"].seed_files
    assert ".claude/settings.json" in profiles["claude"].seed_files
    assert profiles["codex"].seed_files == (".codex/config.toml",)
    assert profiles["gemini"].seed_files == (".gemini/settings.json",)
    # copilot: turn-end is agentStop (Copilot 1.0.63 never fires PascalCase Stop),
    # no PreCompact equivalent, and its events.jsonl parser is wired up
    assert profiles["copilot"].hooks.events == {
        "agentStop": "Stop",
        "sessionStart": "SessionStart",
        "sessionEnd": "SessionEnd",
    }
    assert profiles["copilot"].usage_parser == "copilot-events"
    # copilot writes token totals only on shutdown (poll grace) and fires
    # agentStop per turn (multi-turn reviews need more nudges)
    assert profiles["copilot"].usage_grace_s == 8.0
    assert profiles["copilot"].stop_without_result_nudges == 5
    # copilot also fires agentStop for subagent turns (empty transcriptPath) — those
    # are ignored so the main session's turn-end drives completion
    assert profiles["copilot"].subagent_stop_without_transcript is True
    # other built-ins keep the defaults: read usage once, inherit the global nudge
    # limit, and treat every Stop as the main turn-end (no subagent filtering)
    for name in ("claude", "codex", "gemini"):
        assert profiles[name].usage_grace_s == 0.0
        assert profiles[name].stop_without_result_nudges is None
        assert profiles[name].subagent_stop_without_transcript is False
    # claude forces its classic (inline/scrollback) renderer so a pane capture is
    # not collapsed to the final frame by the fullscreen alt-screen TUI, and
    # disables background tasks so a dev session cannot background its
    # implementation sub-agent and strand it at turn end (#109); other profiles
    # add no such env overrides
    assert profiles["claude"].env.get("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN") == "1"
    assert profiles["claude"].env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "1"
    for name in sorted(set(profiles) - {"claude"}):
        assert "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN" not in profiles[name].env
        assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in profiles[name].env
    # transport-failure classification (#194): only claude seeds env_fault_patterns
    # (the "API Error … connection cause" signature); every other built-in ships
    # none, so classification stays inert until a project overlay adds patterns
    assert profiles["claude"].env_fault_patterns  # non-empty
    for name in ("codex", "gemini", "copilot", "antigravity", "opencode-http"):
        assert profiles[name].env_fault_patterns == ()
    # opencode-http is hookless (HTTP/SSE transport): no hook dialect surfaces,
    # skills read from the claude tree, usage comes over HTTP (no transcript parser)
    opencode = profiles["opencode-http"]
    assert opencode.hookless is True
    assert opencode.hooks.dialect == "none"
    assert opencode.hooks.config_path == ""
    assert opencode.hooks.events == {}
    assert opencode.skill_tree == ".claude/skills"
    assert opencode.usage_parser == "none"
    assert opencode.binary == "opencode"
    # every hook-driven built-in stays non-hookless
    for name in sorted(set(profiles) - {"opencode-http"}):
        assert profiles[name].hookless is False


def test_usage_grace_and_nudges_default_when_unset(tmp_path):
    # MINIMAL_PROFILE omits both -> 0.0 / None
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    prof = load_profiles(tmp_path)["mycli"]
    assert prof.usage_grace_s == 0.0
    assert prof.stop_without_result_nudges is None
    assert prof.subagent_stop_without_transcript is False


def test_seed_files_default_empty_when_unset(tmp_path):
    # MINIMAL_PROFILE omits seed_files -> defaults to ()
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    assert load_profiles(tmp_path)["mycli"].seed_files == ()


def test_env_fault_patterns_default_empty_when_unset(tmp_path):
    # MINIMAL_PROFILE omits env_fault_patterns -> defaults to () (classification inert)
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    assert load_profiles(tmp_path)["mycli"].env_fault_patterns == ()


def test_env_fault_patterns_parse_from_overlay(tmp_path):
    # a project overlay may add/extend the transport-failure patterns; they parse
    # into a tuple verbatim (compilation validated, see the invalid-regex case)
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(
        MINIMAL_PROFILE.replace(
            "[hooks]",
            'env_fault_patterns = ["API Error.*refused", "socket hang up"]\n[hooks]',
        )
    )
    prof = load_profiles(tmp_path)["mycli"]
    assert prof.env_fault_patterns == ("API Error.*refused", "socket hang up")


def test_skill_tree_defaults_when_unset():
    # MINIMAL_PROFILE omits skill_tree -> defaults to .claude/skills
    assert get_profile("claude").skill_tree == ".claude/skills"


def test_legacy_alias_resolves():
    assert get_profile("claude-code-tmux").name == "claude"


def test_opencode_alias_resolves():
    assert get_profile("opencode").name == "opencode-http"


def test_hookless_user_profile_parses(tmp_path):
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli-http.toml").write_text(HOOKLESS_PROFILE)
    prof = load_profiles(tmp_path)["mycli-http"]
    assert prof.hookless is True
    assert prof.hooks.dialect == "none"
    assert prof.hooks.config_path == ""
    assert prof.hooks.events == {}


def test_unknown_profile_raises():
    with pytest.raises(ProfileError, match="unknown CLI profile"):
        get_profile("acme-cli")


def test_render_prompt_passthrough_and_template():
    claude = get_profile("claude")
    assert claude.render_prompt("/bmad-dev-auto 1-1-a") == "/bmad-dev-auto 1-1-a"
    codex = get_profile("codex")
    assert codex.render_prompt("/bmad-dev-auto 1-1-a") == (
        "Use the $bmad-dev-auto skill now, and use subagents as needed: 1-1-a"
    )
    opencode = get_profile("opencode-http")
    assert opencode.render_prompt("/bmad-dev-auto 1-1-a") == (
        "Use the bmad-dev-auto skill now: 1-1-a"
    )
    # non-slash prompts pass through {prompt}; {skill}/{args} degrade gracefully
    assert claude.render_prompt("just do it") == "just do it"


def test_user_profile_overlay(tmp_path):
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    # override a built-in by reusing its name
    (profiles_dir / "claude-override.toml").write_text(
        MINIMAL_PROFILE.replace('name = "mycli"', 'name = "claude"')
    )
    profiles = load_profiles(tmp_path)
    assert "mycli" in profiles
    assert profiles["mycli"].bypass_args == ("--yes",)
    assert profiles["claude"].binary == "mycli"  # overridden
    assert "codex" in profiles  # built-ins still present


def test_discovered_profiles_union_keeps_both_sides_of_a_same_name_overlay(tmp_path):
    """A valid same-name overlay REPLACES the packaged profile in the effective
    map — correct for a run, wrong for `validate`'s historical reconstruction,
    where the packaged profile's paths may be exactly what an older run wrote
    into the shared exclude. The union carries both.

    `config_path` distinguishes the two sides; `skill_tree` cannot — it is
    shared across profiles (claude and opencode-http both read .claude/skills),
    so a probe keyed on it is void."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "claude-override.toml").write_text(
        MINIMAL_PROFILE.replace('name = "mycli"', 'name = "claude"')
    )

    union = discovered_profiles(tmp_path)

    assert {p.hooks.config_path for p in union if p.name == "claude"} == {
        ".claude/settings.json",  # the packaged profile, still a candidate source
        ".mycli/settings.json",  # the overlay that replaced it
    }
    # the contrast that makes the union necessary: the effective map collapses
    # the pair to the overlay, losing the packaged paths
    assert load_profiles(tmp_path)["claude"].hooks.config_path == ".mycli/settings.json"


def test_discovered_profiles_skips_only_the_malformed_file(tmp_path):
    """One bad overlay skips ITSELF — not the packaged profiles, and not its
    overlay siblings. `a-broken.toml` sorts FIRST within the overlay dir, so a
    guard that stops that source at the first fault (a `break`, or fail-fast)
    also loses `z-extra.toml` — which is what the "extra" assert catches."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "a-broken.toml").write_text("this is not = = toml\n")
    (profiles_dir / "z-extra.toml").write_text(
        MINIMAL_PROFILE.replace('name = "mycli"', 'name = "extra"')
    )
    # non-vacuity: the effective loader really refuses this overlay dir, so the
    # union below cannot be riding on a fixture that never exercised the guards
    with pytest.raises(ProfileError):
        load_profiles(tmp_path)

    names = {p.name for p in discovered_profiles(tmp_path)}

    assert {"claude", "codex", "gemini", "opencode-http"} <= names
    assert "extra" in names


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ('name = "mycli"\nbinary = "mycli"', "missing"),  # no [hooks]
        (
            MINIMAL_PROFILE.replace('dialect = "claude-settings-json"', 'dialect = "nope"'),
            "dialect",
        ),
        (MINIMAL_PROFILE.replace('Stop = "Stop"', 'Stop = "TurnDone"'), "canonical"),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'usage_parser = "magic"\n[hooks]'),
            "usage_parser",
        ),
        (
            MINIMAL_PROFILE.replace(
                'config_path = ".mycli/settings.json"',
                'config_path = "/abs/settings.json"',
            ),
            "relative",
        ),
        (
            MINIMAL_PROFILE.replace(
                "[hooks]",
                'skill_tree = "/abs/skills"\n[hooks]',
            ),
            "skill_tree",
        ),
        (
            MINIMAL_PROFILE.replace(
                "[hooks]",
                'seed_files = ["/etc/passwd"]\n[hooks]',
            ),
            "seed_files",
        ),
        # A root-naming entry is the harmful one, and `""` is only one spelling of
        # it: these feed provision_worktree's seed loop, where any of them resolves
        # src to the repo root and dst to the worktree — both pass its containment
        # checks, so the whole repo is copied in and the copy recurses into itself.
        (
            MINIMAL_PROFILE.replace("[hooks]", 'seed_files = ["."]\n[hooks]'),
            "seed_files",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'seed_files = ["./"]\n[hooks]'),
            "seed_files",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'skill_tree = "."\n[hooks]'),
            "skill_tree",
        ),
        (
            MINIMAL_PROFILE.replace(
                'config_path = ".mycli/settings.json"',
                'config_path = "."',
            ),
            "relative",
        ),
        # an env_fault_patterns entry that is not a valid regex fails fast at parse
        (
            MINIMAL_PROFILE.replace(
                "[hooks]",
                'env_fault_patterns = ["API Error(unbalanced"]\n[hooks]',
            ),
            "env_fault_patterns",
        ),
        # list fields must be a TOML array of strings: a bare string would iterate to
        # per-character entries (regexes, in env_fault_patterns' case) and a scalar
        # would leak a raw TypeError — both are rejected with a friendly ProfileError.
        (
            MINIMAL_PROFILE.replace("[hooks]", 'env_fault_patterns = "API"\n[hooks]'),
            "env_fault_patterns must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "env_fault_patterns = 5\n[hooks]"),
            "env_fault_patterns must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "env_fault_patterns = [1]\n[hooks]"),
            "env_fault_patterns must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "launch_args = [1]\n[hooks]"),
            "launch_args must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "seed_files = 3\n[hooks]"),
            "seed_files must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace('bypass_args = ["--yes"]', 'bypass_args = "x"'),
            "bypass_args must be a list of strings",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "usage_grace_s = -1\n[hooks]"),
            "usage_grace_s",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "stop_without_result_nudges = -2\n[hooks]"),
            "stop_without_result_nudges",
        ),
        # real dialects still hard-require config_path and events
        (
            MINIMAL_PROFILE.replace('config_path = ".mycli/settings.json"', ""),
            "config_path",
        ),
        (
            MINIMAL_PROFILE.replace(
                'events = { SessionStart = "SessionStart", Stop = "Stop" }', ""
            ),
            "events",
        ),
        # hookless must not carry hook plumbing (config_path/events)
        (
            MINIMAL_PROFILE.replace('dialect = "claude-settings-json"', 'dialect = "none"'),
            "hookless",
        ),
    ],
)
def test_invalid_profiles_rejected(tmp_path, mutation, match):
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bad.toml").write_text(mutation)
    with pytest.raises(ProfileError, match=match):
        load_profiles(tmp_path)
