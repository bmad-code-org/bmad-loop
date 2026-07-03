import pytest

from bmad_loop.adapters.profile import (
    ProfileError,
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


def test_builtin_profiles_load():
    profiles = load_profiles()
    assert {"claude", "codex", "gemini"} <= set(profiles)
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
    # not collapsed to the final frame by the fullscreen alt-screen TUI; other
    # profiles add no such env override
    assert profiles["claude"].env.get("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN") == "1"
    for name in ("codex", "gemini", "copilot"):
        assert "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN" not in profiles[name].env


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


def test_skill_tree_defaults_when_unset():
    # MINIMAL_PROFILE omits skill_tree -> defaults to .claude/skills
    assert get_profile("claude").skill_tree == ".claude/skills"


def test_legacy_alias_resolves():
    assert get_profile("claude-code-tmux").name == "claude"


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
        (
            MINIMAL_PROFILE.replace("[hooks]", "usage_grace_s = -1\n[hooks]"),
            "usage_grace_s",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "stop_without_result_nudges = -2\n[hooks]"),
            "stop_without_result_nudges",
        ),
    ],
)
def test_invalid_profiles_rejected(tmp_path, mutation, match):
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bad.toml").write_text(mutation)
    with pytest.raises(ProfileError, match=match):
        load_profiles(tmp_path)
