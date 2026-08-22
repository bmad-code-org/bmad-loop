from importlib import resources
from pathlib import Path

import pytest
from conftest import fault_read_text

from bmad_loop.adapters import profile as profile_mod
from bmad_loop.adapters.profile import (
    CLIProfile,
    HookSpec,
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
    # transport/provider fault classification (#194): only the two profiles with
    # captured real-world error output seed patterns — claude (three
    # capture-anchored patterns, reproducing only COMPLETE Claude Code error
    # sentences: the "Unable to connect to API" connection failure and the
    # provider 5xx pair; still no quota cause, since no captured Claude Code
    # usage-limit line exists) and opencode-http (the serve process's
    # `error.error="AI_APICallError: …"` field). The other four stay inert on
    # purpose: patterns for them could only be written from strings scraped off
    # public issue trackers, and an unverified pattern that fires on a healthy
    # session pauses the whole run. Precision is asserted in
    # tests/test_env_fault_patterns.py; here we only pin which profiles are seeded.
    for name in ("claude", "opencode-http"):
        assert profiles[name].env_fault_patterns, f"{name} ships no env_fault_patterns"
    for name in ("codex", "gemini", "copilot", "antigravity"):
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


# --------------------------------------------------------------------------- #
# The `adapter` field (which adapter CLASS drives the profile)


def test_adapter_field_defaults_to_generic_and_parses():
    """The adapter kind is read at parse time: unset defaults to the bundled tmux
    generic; opencode-http declares its HTTP adapter kind. Asserted across ALL
    built-ins, not two spot checks — a profile silently defaulting to `generic`
    would dispatch to the tmux adapter, which cannot host it."""
    profiles = load_profiles()
    assert profiles["opencode-http"].adapter == "opencode-http"
    assert {name for name, p in profiles.items() if p.adapter == "generic"} == (
        set(profiles) - {"opencode-http"}
    )


def test_absent_adapter_on_a_hookless_profile_keeps_the_pre_registry_dispatch(tmp_path):
    """Back-compat for the TOML files this field did not exist in.

    Before the registry, `hooks.dialect = "none"` WAS the class selector — every
    hookless profile went to the opencode HTTP adapters. Copying the packaged
    opencode profile into `.bmad-loop/profiles/` to tweak binary/env/model is the
    documented customization, and that copy carries no `adapter` key. Taking the
    dataclass default would move it onto the tmux generic adapter, where it waits
    out `session_timeout_min` for a `Stop` hook a hookless profile never registers
    — and every `validate` check that would catch it keys on `hookless` too, so the
    preflight stays green. Non-hookless profiles keep defaulting to generic.

    ABLATION: restore `doc.get("adapter", "generic")` and the hookless row reddens
    (it resolves to `generic`) while the dialect row stays green."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    # The pre-field bytes: hookless, and no `adapter` key anywhere.
    (profiles_dir / "legacy.toml").write_text(HOOKLESS_PROFILE)
    (profiles_dir / "hooked.toml").write_text(MINIMAL_PROFILE)

    profiles = load_profiles(tmp_path)
    assert profiles["mycli-http"].hookless
    assert profiles["mycli-http"].adapter == "opencode-http"
    assert profiles["mycli"].adapter == "generic"


def test_explicit_adapter_beats_the_hookless_back_compat_default(tmp_path):
    """The back-compat default fires ONLY on the absent key. An explicit `adapter`
    is always honored — including hookless-driven-by-something-else, which is the
    decoupling the registry exists to allow and which the old dispatch could not
    express."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "decoupled.toml").write_text(
        HOOKLESS_PROFILE.replace("[hooks]", 'adapter = "some-other-http-kind"\n[hooks]')
    )
    profile = load_profiles(tmp_path)["mycli-http"]
    assert profile.hookless and profile.adapter == "some-other-http-kind"


def test_hookless_profile_cannot_select_the_generic_adapter(tmp_path):
    """The one coherence rule between the two axes, written out longhand.

    `generic` is the bundled tmux adapter and completes on a Stop hook (or the
    window dying); `dialect = "none"` means nothing ever registers one. The pair
    therefore describes a session that can only wait out `session_timeout_min`
    against an interactive CLI that never exits — the exact failure
    `_legacy_adapter_default` steers the ABSENT-key file away from, which an
    explicit file could still spell out.

    ABLATION: drop the `profile.hookless and ... == GENERIC` guard from
    `_validate_profile` and this reddens (the profile loads happily)."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "hangy.toml").write_text(
        HOOKLESS_PROFILE.replace("[hooks]", 'adapter = "generic"\n[hooks]')
    )
    with pytest.raises(ProfileError, match="cannot select the 'generic' adapter"):
        load_profiles(tmp_path)


def test_entry_point_profile_at_the_default_adapter_while_hookless_is_refused(profile_scan):
    """The same pair by the route `_legacy_adapter_default` cannot reach — and the
    reason the rule lives in `_validate_profile` rather than in the TOML parser.

    A provider that builds a hookless `HookSpec` and never sets `adapter` takes the
    dataclass default `generic`. Content a TOML file expresses by OMITTING the key,
    which the parser steers to the HTTP kind; the Python route has no absent-key to
    detect, so without this rule the two routes disagree about the same profile and
    the provider's silently hangs at run time. Dropped with a reason instead.

    ABLATION: drop the guard and `acme` loads with `adapter == "generic"`."""
    profile_scan(
        _FakeEntryPoint(
            "acme",
            lambda: [CLIProfile(name="acme", binary="acme", hooks=HookSpec("none", "", {}))],
        )
    )
    assert "acme" not in load_profiles()
    assert "generic" in profile_mod.external_profile_errors()["acme"]


def test_adapter_kind_membership_is_not_checked_at_parse_time(tmp_path):
    """A profile naming an unregistered adapter kind still PARSES — validity is
    enforced later against the live registry (at construction / by `validate`),
    never a set literal here that every new adapter would have to edit."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "future.toml").write_text(
        MINIMAL_PROFILE.replace("[hooks]", 'adapter = "not-a-real-kind-yet"\n[hooks]')
    )
    assert load_profiles(tmp_path)["mycli"].adapter == "not-a-real-kind-yet"


@pytest.mark.parametrize("value", ["5", "[1]", "{ k = 1 }", "true", '""'])
def test_malformed_adapter_value_funnels_into_profile_error(tmp_path, value):
    """#384: a malformed value funnels into ProfileError at the boundary rather
    than being coerced. `adapter` is the one selector field with no parse-time
    membership test to land in afterwards, so `str(["x"])` would carry the literal
    `"['x']"` all the way to `get_adapter_kind` and name that as the unknown kind.

    ABLATION: replace the isinstance check with `str(doc.get("adapter", ...))` and
    every row but `""` stops raising."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bad.toml").write_text(
        MINIMAL_PROFILE.replace("[hooks]", f"adapter = {value}\n[hooks]")
    )
    with pytest.raises(ProfileError, match="adapter"):
        load_profiles(tmp_path)


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
        # TOML-legal values of the wrong TYPE hit raw conversions (`float()`,
        # `int()`, `.items()`) — the `_load_toml` funnel turns those bare
        # ValueError/TypeError/AttributeError escapes into ProfileError, which
        # is what every consumer's fault handling keys on. Ablation: drop the
        # funnel arm and these four raise the bare exception instead.
        (
            MINIMAL_PROFILE.replace("[hooks]", 'usage_grace_s = "invalid"\n[hooks]'),
            "malformed field value",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", "usage_grace_s = [1]\n[hooks]"),
            "malformed field value",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'stop_without_result_nudges = "x"\n[hooks]'),
            "malformed field value",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'env = "invalid"\n[hooks]'),
            "malformed field value",
        ),
        # `inf` and an oversized int are legal TOML and raise OverflowError, a
        # sibling of neither ValueError nor TypeError — the rows that show why
        # the funnel has to be the CLOSED set for the tomllib domain rather than
        # the types seen so far. Ablation: drop OverflowError from
        # CONVERSION_FAULTS and exactly these two raise the bare exception.
        (
            MINIMAL_PROFILE.replace("[hooks]", "stop_without_result_nudges = inf\n[hooks]"),
            "malformed field value",
        ),
        (
            MINIMAL_PROFILE.replace("[hooks]", f"usage_grace_s = {'9' * 400}\n[hooks]"),
            "malformed field value",
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


# every type `tomllib` can yield, plus the numeric spellings that are legal TOML
# and hostile to a raw coercion
TOML_VALUE_DOMAIN = [
    '"x"',
    "1",
    "1.5",
    "true",
    "1979-05-27T07:32:00Z",
    "1979-05-27",
    "07:32:00",
    "[1]",
    "{ k = 1 }",
    "inf",
    "-inf",
    "nan",
    "9" * 400,  # tomllib keeps arbitrary precision; float() of this overflows
]


@pytest.mark.parametrize("value", TOML_VALUE_DOMAIN)
@pytest.mark.parametrize("key", ["usage_grace_s", "stop_without_result_nudges"])
def test_every_toml_value_type_parses_or_raises_profile_error(tmp_path, key, value):
    """The closure pin behind `CONVERSION_FAULTS`: a profile field can hold any of
    the nine types `tomllib` yields, and the contract is that each either parses or
    raises the DOMAIN error — never a bare conversion fault out of a `float()`,
    `int()` or `.items()`.

    Both a float knob and an int knob, because they fault differently: `inf` is a
    fine float and an OverflowError for `int()`, while a 400-digit integer is a
    fine int and an OverflowError for `float()`. A per-type exception list cannot
    promise this; two review rounds each added one type after a bot found a
    spelling nobody had tried."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "probe.toml").write_text(
        MINIMAL_PROFILE.replace("[hooks]", f"{key} = {value}\n[hooks]")
    )
    try:
        load_profiles(tmp_path)
    except ProfileError:
        pass


def test_non_utf8_overlay_raises_profile_error(tmp_path):
    """#473: the READ, not a value coercion. `CONVERSION_FAULTS` cannot reach this
    one — the funnel wraps `_parse_profile`, and the decode happens in `_load_toml`'s
    argument expression, before the funnel is entered — so a non-UTF-8 overlay
    escaped as a raw `UnicodeDecodeError`. Asserting the TYPE is the point: every
    consumer keys its fault handling on ProfileError, and a `ValueError` escape took
    `validate` down before it printed any document.

    ABLATION: route the overlay read back through
    `_load_toml(path.read_text(encoding="utf-8"), str(path))` and this raises
    UnicodeDecodeError instead."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    bad = profiles_dir / "bad.toml"
    bad.write_bytes(b'name = "b\xffad"\n')
    # Self-verify the fixture before trusting what it proves: a file that decoded
    # fine would make the assertion below pass for the wrong reason.
    with pytest.raises(UnicodeDecodeError):
        bad.read_text(encoding="utf-8")
    with pytest.raises(ProfileError, match="not valid UTF-8") as excinfo:
        load_profiles(tmp_path)
    assert str(bad) in str(excinfo.value)  # the fault names the file at fault


def test_unreadable_overlay_raises_profile_error(tmp_path, monkeypatch):
    """The OSError arm of the same guard: a profile that is present but cannot be
    read — permissions, an I/O error, a dead mount. `user_dir.is_dir()` and the glob
    rule out ABSENCE and nothing else, so this escaped as a bare OSError.

    `fault_read_text` rather than chmod for the reason its docstring gives, and its
    targeting matters twice over here: a blanket `Path.read_text` patch is answered
    by the PACKAGED loop first, which would redden with this call site untouched.

    ABLATION: route the overlay read back through `path.read_text(...)` and this
    raises PermissionError instead."""
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    bad = profiles_dir / "bad.toml"
    bad.write_text(MINIMAL_PROFILE)
    # Precondition: readable, this overlay loads — so the raise below is the fault
    # being converted, not a profile that was malformed all along.
    assert load_profiles(tmp_path)["mycli"].binary == "mycli"
    fault_read_text(monkeypatch, bad)
    with pytest.raises(ProfileError, match="unreadable") as excinfo:
        load_profiles(tmp_path)
    assert str(bad) in str(excinfo.value)


def test_unreadable_packaged_profile_raises_profile_error(monkeypatch):
    """The packaged built-ins are read through the same guard. They are trusted
    content, so this is not #473's user-authored fault class — but a corrupt or
    unreadable install is a PACKAGING bug, and the loader owes its callers the typed
    error that says so instead of a traceback: `validate`, `install` and
    `_require_base_skills` all key on ProfileError and none of them catches OSError.

    Here because the two read sites are separate wiring axes, not one. Measured:
    reverting the PACKAGED read to `entry.read_text(...)` leaves all three overlay
    tests green — that site had no oracle at all, and this is the only test that
    reddens for it. (Reverting the OVERLAY read reddens the other three and leaves
    this one green: disjoint, which is the proof that neither stands in for the
    other.)"""
    packaged = resources.files("bmad_loop.data").joinpath("profiles")
    names = sorted(e.name for e in packaged.iterdir() if e.name.endswith(".toml"))
    # Real path, not a zip member: `fault_read_text` targets `Path.read_text`, so a
    # zipimported install would leave the fault unarmed. Asserted, not assumed.
    victim = Path(str(packaged.joinpath(names[0])))
    assert victim.is_file()
    fault_read_text(monkeypatch, victim)
    with pytest.raises(ProfileError, match="unreadable") as excinfo:
        load_profiles()
    assert victim.name in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Out-of-tree profile providers (the bmad_loop.profiles entry-point scan)


@pytest.fixture
def profile_scan(monkeypatch):
    """Isolate + re-arm the profile entry-point scan: snapshot/clear the module's
    external-scan state, then hand back a hook to install fake entry points.

    Setup parks the scan as ALREADY-LOADED over an empty map rather than leaving
    whatever a previous test wrote — a test that takes this fixture without
    calling `arm` would otherwise assert against leftovers and pass for the wrong
    reason. Parked, not re-armed: arming without a fake `entry_points` in place
    would run the REAL scan and leak whichever profile packages the dev box
    happens to have installed into the assertions. `arm` re-opens it, exactly as
    `fresh_adapter_registry` does for the adapter half."""
    saved_loaded = profile_mod._EXTERNALS_LOADED
    saved_profiles = dict(profile_mod._EXTERNAL_PROFILES)
    saved_errors = dict(profile_mod._PROFILE_LOAD_ERRORS)
    profile_mod._EXTERNALS_LOADED = True
    profile_mod._EXTERNAL_PROFILES.clear()
    profile_mod._PROFILE_LOAD_ERRORS.clear()

    def arm(*eps, scan_error=None):
        def fake_entry_points(*, group):
            assert group == profile_mod.PROFILES_GROUP
            if scan_error is not None:
                raise scan_error
            return list(eps)

        monkeypatch.setattr(profile_mod.importlib.metadata, "entry_points", fake_entry_points)
        profile_mod._EXTERNALS_LOADED = False
        profile_mod._EXTERNAL_PROFILES.clear()
        profile_mod._PROFILE_LOAD_ERRORS.clear()

    yield arm

    profile_mod._EXTERNALS_LOADED = saved_loaded
    profile_mod._EXTERNAL_PROFILES.clear()
    profile_mod._EXTERNAL_PROFILES.update(saved_profiles)
    profile_mod._PROFILE_LOAD_ERRORS.clear()
    profile_mod._PROFILE_LOAD_ERRORS.update(saved_errors)


class _FakeDist:
    """Stands in for ``EntryPoint.dist``; the scan orders on its ``.name``."""

    def __init__(self, name):
        self.name = name


class _FakeEntryPoint:
    """Duck-typed EntryPoint. ``dist`` is the scan's ordering tiebreak (see
    `_load_external_profiles`) and defaults to a distinct-per-name stand-in, so
    only a test that sets it can decide a same-name collision."""

    def __init__(self, name, load, dist=None):
        self.name = name
        self.dist = _FakeDist(dist if dist is not None else f"{name}-dist")
        self._load = load

    def load(self):
        return self._load()


def _plugin_profile(name="acme", adapter="acme", **over):
    fields = {
        "name": name,
        "binary": name,
        "adapter": adapter,
        "hooks": HookSpec("none", "", {}),
        **over,
    }
    return CLIProfile(**fields)


def test_entry_point_profile_is_discovered(profile_scan):
    """A pip-installed profile provider (a callable returning CLIProfiles) makes
    its profile resolvable with no project TOML — the zero-config selection path."""
    profile_scan(_FakeEntryPoint("acme", lambda: (lambda: [_plugin_profile()])))
    prof = get_profile("acme")
    assert prof.name == "acme" and prof.adapter == "acme"
    assert profile_mod.external_profile_errors() == {}


def test_entry_point_profile_provider_may_be_iterable(profile_scan):
    """The provider may be an iterable directly, not only a callable returning
    one — both shapes are accepted."""
    profile_scan(_FakeEntryPoint("acme", lambda: [_plugin_profile()]))
    assert "acme" in load_profiles()


def test_project_profile_overrides_entry_point(profile_scan, tmp_path):
    """Precedence packaged < entry-point < project: a project-local TOML of the
    same name wins over an entry-point profile."""
    profile_scan(_FakeEntryPoint("acme", lambda: [_plugin_profile(adapter="acme")]))
    profiles_dir = tmp_path / ".bmad-loop" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "acme.toml").write_text(
        MINIMAL_PROFILE.replace('name = "mycli"', 'name = "acme"')
    )
    prof = load_profiles(tmp_path)["acme"]
    assert prof.binary == "mycli"  # the project TOML, not the entry-point profile


def test_entry_point_profile_can_override_packaged(profile_scan):
    """Entry-point profiles overlay the packaged built-ins (packaged <
    entry-point), so a plugin may re-point a bundled name."""
    profile_scan(_FakeEntryPoint("acme", lambda: [_plugin_profile(name="claude", adapter="acme")]))
    assert load_profiles()["claude"].adapter == "acme"


def test_broken_profile_provider_degrades_and_is_recorded(profile_scan):
    """A provider that blows up must not break profile loading: the built-ins
    still load, and the failure is recorded for diagnostics."""

    def boom():
        raise RuntimeError("half-installed plugin")

    profile_scan(_FakeEntryPoint("broken", boom))
    profiles = load_profiles()
    assert "claude" in profiles  # built-ins unaffected
    assert list(profile_mod.external_profile_errors()) == ["broken"]
    assert "half-installed" in profile_mod.external_profile_errors()["broken"]


def test_one_broken_profile_package_does_not_hide_the_rest(profile_scan):
    """Per-entry isolation: a good provider still registers alongside a broken one."""

    def boom():
        raise RuntimeError("broke")

    profile_scan(
        _FakeEntryPoint("broken", boom),
        _FakeEntryPoint("acme", lambda: [_plugin_profile()]),
    )
    profiles = load_profiles()
    assert "acme" in profiles
    assert list(profile_mod.external_profile_errors()) == ["broken"]


def test_profile_provider_returning_junk_is_rejected(profile_scan):
    """A provider that yields a non-CLIProfile is the package's bug — recorded,
    never trusted into the profile map."""
    profile_scan(_FakeEntryPoint("acme", lambda: [object()]))
    profiles = load_profiles()
    assert "acme" not in profiles
    assert "not CLIProfile" in profile_mod.external_profile_errors()["acme"]


def test_profile_provider_returning_a_non_iterable_is_rejected(profile_scan):
    """`list(produced)` is the shape check: a provider handing back a scalar is
    reported rather than raising a bare TypeError out of load_profiles."""
    profile_scan(_FakeEntryPoint("acme", lambda: 5))
    assert "acme" not in load_profiles()
    assert "iterable of CLIProfile" in profile_mod.external_profile_errors()["acme"]


def test_same_named_broken_distributions_both_record_a_reason(profile_scan):
    """Two distributions may advertise the SAME entry-point name in this group, and
    both may be broken. A name-keyed assignment let the second overwrite the first:
    the operator fixed the package they were shown and met the other one on the next
    run, with nothing saying it had ever been there. Both reasons are kept now, each
    labelled with its distribution — the entry-point name is not the name you
    `pip uninstall`, and two providers failing identically would otherwise render as
    the same sentence twice.

    The fixture trap #566 itself flags: asserting only "two reasons are present"
    passes for the wrong reason if the two entry points accidentally got DIFFERENT
    names. Pinning the single shared key forecloses that, and is the shape
    decision's own assertion besides — see the comment below.

    Ablation: restore the single-key write
    (`_PROFILE_LOAD_ERRORS[ep.name] = f"{type(exc).__name__}: {exc}"`) in
    `_load_external_profiles` and this test fails on the missing `alpha-profiles`
    half, while the adapter and mux twins stay green."""

    def boom(msg):
        def load():
            raise RuntimeError(msg)

        return load

    profile_scan(
        _FakeEntryPoint("acme", boom("alpha half-installed"), dist="alpha-profiles"),
        _FakeEntryPoint("acme", boom("zeta half-installed"), dist="zeta-profiles"),
    )
    assert "claude" in load_profiles()  # built-ins unaffected
    reason = profile_mod.external_profile_errors()["acme"]
    assert "alpha-profiles" in reason and "zeta-profiles" in reason
    assert "alpha half-installed" in reason and "zeta half-installed" in reason
    # Still ONE key — the shape decision. The key set is what reaches
    # `detail["entry_point"]` in `validate --json`, so it deliberately does not grow;
    # only the human-facing reason string widens.
    assert list(profile_mod.external_profile_errors()) == ["acme"]


def test_profile_scan_failure_degrades(profile_scan):
    """The enumeration itself blowing up leaves built-in loading working, with the
    scan failure recorded."""
    profile_scan(scan_error=RuntimeError("metadata index corrupt"))
    assert "claude" in load_profiles()
    assert "<entry-point scan>" in profile_mod.external_profile_errors()


# --------------------------------------------------------------------------- #
# Entry-point profiles obey the SAME invariants a TOML profile does


@pytest.mark.parametrize(
    ("over", "match"),
    [
        # the finding's own example: unchecked, this compile error moves from LOAD
        # time to MATCH time, inside a session's env-fault classification, where
        # the caller degrades rather than raises and the pattern never fires
        ({"env_fault_patterns": ("API Error(unbalanced",)}, "not a valid regex"),
        # a dialect the hook writer has no branch for
        ({"hooks": HookSpec("mycli-json", ".mycli/s.json", {"Stop": "Stop"})}, "dialect"),
        # a non-canonical event name silently never maps to a completion signal
        (
            {"hooks": HookSpec("claude-settings-json", ".m/s.json", {"Stop": "TurnDone"})},
            "canonical",
        ),
        # path containment — the three fields provision_worktree/install resolve
        ({"skill_tree": "/abs/skills"}, "skill_tree"),
        ({"skill_tree": "."}, "skill_tree"),
        ({"seed_files": ("/etc/passwd",)}, "seed_files"),
        ({"seed_files": (".",)}, "seed_files"),
        ({"hooks": HookSpec("claude-settings-json", "..", {"Stop": "Stop"})}, "relative"),
        # a real dialect with nothing to write to
        ({"hooks": HookSpec("claude-settings-json", "", {"Stop": "Stop"})}, "config_path"),
        # hookless carrying hook plumbing is a contradiction either way in
        ({"hooks": HookSpec("none", ".m/s.json", {})}, "hookless"),
        # the remaining value-level knobs
        ({"usage_parser": "magic"}, "usage_parser"),
        ({"usage_grace_s": -1.0}, "usage_grace_s"),
        ({"stop_without_result_nudges": -2}, "stop_without_result_nudges"),
        ({"adapter": ""}, "adapter"),
        # whitespace-only, not just empty: the TOML route strips before validating,
        # so testing the raw value would refuse `adapter = "  "` from a file while
        # admitting it from a provider — the divergence this whole test denies
        ({"adapter": "   "}, "adapter"),
        ({"binary": "  "}, "required"),
        # ...and the other half of that same divergence: NON-canonical, not empty.
        # `_parse_profile` strips exactly these three, so an unstripped value is
        # content the TOML route cannot produce. Validating a stripped copy while
        # installing the frozen original would file the profile under a key no
        # `--cli` finds / a binary no `which` resolves / a kind no registry has,
        # with the provider recorded as fine.
        ({"adapter": " acme "}, "whitespace"),
        ({"name": " acme "}, "whitespace"),
        ({"binary": " acme "}, "whitespace"),
    ],
)
def test_entry_point_profile_must_pass_the_parser_invariants(profile_scan, over, match):
    """The trust-boundary fix: an entry point hands over an already-CONSTRUCTED
    CLIProfile, so it is the one route into the profile map with no parser in
    front of it. Every value-level invariant `_parse_profile` enforces has to
    apply here too, or a Python package can install a state a TOML author would
    have been refused.

    Rejection is degrade-and-record (the entry-point contract), so the proof is
    that the profile never lands in the map AND the reason names the invariant.

    ABLATION: delete the `_validate_profile` call from `_coerce_profiles` and
    every row here goes green-with-a-bad-profile-installed — `"acme" in profiles`
    becomes true. Deleting it from `_parse_profile` instead reddens the TOML rows
    in `test_invalid_profiles_rejected`, which is the other half of the pair."""
    profile_scan(_FakeEntryPoint("acme", lambda: [_plugin_profile(**over)]))
    profiles = load_profiles()
    assert "acme" not in profiles, "an invalid profile must never reach the map"
    assert match in profile_mod.external_profile_errors()["acme"]


def test_entry_point_batch_is_rejected_whole(profile_scan):
    """One invalid profile drops the provider's whole batch rather than
    half-installing it: a provider is one package's declaration, and an operator
    reading the recorded reason would otherwise be looking at a profile set the
    error message does not account for."""
    profile_scan(
        _FakeEntryPoint(
            "acme",
            lambda: [
                _plugin_profile(name="good"),
                _plugin_profile(name="bad", env_fault_patterns=("(unbalanced",)),
            ],
        )
    )
    profiles = load_profiles()
    assert "good" not in profiles and "bad" not in profiles
    assert "not a valid regex" in profile_mod.external_profile_errors()["acme"]


def test_a_valid_entry_point_profile_still_lands(profile_scan):
    """The control for the rejection rows above: a provider whose profiles DO
    satisfy the invariants is installed unchanged, so those tests are failing on
    the invariant rather than on the plumbing."""
    profile_scan(
        _FakeEntryPoint(
            "acme",
            lambda: [_plugin_profile(env_fault_patterns=("API Error.*Connection refused",))],
        )
    )
    assert load_profiles()["acme"].env_fault_patterns == ("API Error.*Connection refused",)
    assert profile_mod.external_profile_errors() == {}
