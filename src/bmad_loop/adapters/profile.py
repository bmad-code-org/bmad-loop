"""Declarative CLI profiles for the generic tmux adapter.

A profile captures everything that differs between coding CLIs that share the
tmux-injection + hook-signal transport: binary name, how the canonical
"/skill args" prompt is rendered, bypass flags, hook registration (a config
dialect + an event-name map), and which usage parser reads the transcript.

Built-in profiles ship as packaged TOML (bmad_loop/data/profiles/*.toml) and
project-local TOML files in <project>/.bmad-loop/profiles/*.toml overlay them
(same name overrides, new names extend) — adding a CLI that clones an
existing hook dialect needs no Python.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ..platform_util import has_parent_ref, is_absolute_path

USAGE_PARSERS = {"claude-jsonl", "codex-rollout", "gemini-chat", "copilot-events", "none"}
HOOK_DIALECTS = {
    "claude-settings-json",
    "codex-hooks-json",
    "gemini-settings-json",
    "copilot-settings-json",
}
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact"}
USER_PROFILES_REL = Path(".bmad-loop") / "profiles"

# legacy adapter names from older policy.toml files
ALIASES = {"claude-code-tmux": "claude"}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class HookSpec:
    dialect: str
    config_path: str  # project-relative, e.g. ".claude/settings.json"
    events: dict[str, str]  # native event name -> canonical event name


@dataclass(frozen=True)
class CLIProfile:
    name: str
    binary: str
    hooks: HookSpec
    # project-relative tree this CLI reads skills from, e.g. ".claude/skills"
    # (claude) or ".agents/skills" (codex/gemini); `bmad-loop init` installs the
    # bundled bmad-loop-* skills here.
    skill_tree: str = ".claude/skills"
    prompt_template: str = "{prompt}"
    launch_args: tuple[str, ...] = ()
    bypass_args: tuple[str, ...] = ()
    model_flag: str = "--model"
    env: dict[str, str] = field(default_factory=dict)
    usage_parser: str = "none"
    # seconds to keep polling the transcript for token usage after the session
    # ends. 0 = read once (the totals are already there). CLIs that flush their
    # token totals only on shutdown (Copilot writes modelMetrics in the trailing
    # session.shutdown line, ~1s after the turn-end hook) need a small grace so
    # read_usage doesn't sample the transcript before the totals land.
    usage_grace_s: float = 0.0
    # per-adapter floor for Stop-without-result nudges; None = use the global
    # limits.stop_without_result_nudges. CLIs that fire a turn-end hook PER
    # response turn (Copilot's agentStop) end a parallel-subagent phase across
    # several turns, so the global default of 1 declares them stalled too early.
    stop_without_result_nudges: int | None = None
    # Some CLIs (Copilot) fire the turn-end hook for EVERY subagent turn too, with
    # an empty transcriptPath and a tool-use session id (toolu_…) — not the main
    # session's turn-end. When true, a Stop carrying no transcript_path is treated
    # as a subagent stop and ignored, so the main session's real turn-end drives
    # completion (and supplies the transcript for usage tallying). Without this a
    # subagent's premature Stop reads as a result-less completion -> false stall.
    subagent_stop_without_transcript: bool = False
    first_run_note: str = ""
    # project-relative gitignored configs (MCP/CLI settings) this CLI needs but
    # that a `git worktree add` checkout omits; provision_worktree copies them in
    # from the main repo so isolated dev/review sessions can reach the MCP server.
    seed_files: tuple[str, ...] = ()

    def render_prompt(self, prompt: str) -> str:
        """Render the engine's canonical "/skill args" prompt for this CLI.

        Placeholders: {prompt} = the canonical string, {skill} = the leading
        slash-command name without "/", {args} = everything after it.
        """
        skill, args = "", prompt
        if prompt.startswith("/"):
            head, _, rest = prompt[1:].partition(" ")
            skill, args = head, rest.strip()
        return self.prompt_template.format(prompt=prompt, skill=skill, args=args)


def _parse_profile(doc: dict, source: str) -> CLIProfile:
    def fail(msg: str) -> ProfileError:
        return ProfileError(f"profile {source}: {msg}")

    name = str(doc.get("name", "")).strip()
    binary = str(doc.get("binary", "")).strip()
    if not name or not binary:
        raise fail("'name' and 'binary' are required")

    hooks_d = doc.get("hooks")
    if not isinstance(hooks_d, dict):
        raise fail("missing [hooks] table")
    dialect = str(hooks_d.get("dialect", ""))
    if dialect not in HOOK_DIALECTS:
        raise fail(f"hooks.dialect must be one of {sorted(HOOK_DIALECTS)}: got {dialect!r}")
    config_path = str(hooks_d.get("config_path", ""))
    if not config_path or is_absolute_path(config_path) or has_parent_ref(config_path):
        raise fail("hooks.config_path must be a project-relative path")
    events_d = hooks_d.get("events")
    if not isinstance(events_d, dict) or not events_d:
        raise fail("hooks.events must map native event names to canonical ones")
    events = {str(k): str(v) for k, v in events_d.items()}
    bad = sorted(set(events.values()) - CANONICAL_EVENTS)
    if bad:
        raise fail(f"hooks.events values must be canonical {sorted(CANONICAL_EVENTS)}: got {bad}")

    usage_parser = str(doc.get("usage_parser", "none"))
    if usage_parser not in USAGE_PARSERS:
        raise fail(f"usage_parser must be one of {sorted(USAGE_PARSERS)}: got {usage_parser!r}")

    usage_grace_s = float(doc.get("usage_grace_s", 0.0))
    if usage_grace_s < 0:
        raise fail(f"usage_grace_s must be >= 0: got {usage_grace_s}")

    raw_nudges = doc.get("stop_without_result_nudges")
    stop_nudges = None if raw_nudges is None else int(raw_nudges)
    if stop_nudges is not None and stop_nudges < 0:
        raise fail(f"stop_without_result_nudges must be >= 0: got {stop_nudges}")

    skill_tree = str(doc.get("skill_tree", ".claude/skills"))
    if not skill_tree or is_absolute_path(skill_tree) or has_parent_ref(skill_tree):
        raise fail("skill_tree must be a project-relative path")

    seed_files = tuple(str(s) for s in doc.get("seed_files", ()))
    for seed in seed_files:
        if not seed or is_absolute_path(seed) or has_parent_ref(seed):
            raise fail(f"seed_files entries must be project-relative paths: got {seed!r}")

    return CLIProfile(
        name=name,
        binary=binary,
        hooks=HookSpec(dialect=dialect, config_path=config_path, events=events),
        skill_tree=skill_tree,
        prompt_template=str(doc.get("prompt_template", "{prompt}")),
        launch_args=tuple(str(a) for a in doc.get("launch_args", ())),
        bypass_args=tuple(str(a) for a in doc.get("bypass_args", ())),
        model_flag=str(doc.get("model_flag", "--model")),
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        usage_parser=usage_parser,
        usage_grace_s=usage_grace_s,
        stop_without_result_nudges=stop_nudges,
        subagent_stop_without_transcript=bool(doc.get("subagent_stop_without_transcript", False)),
        first_run_note=str(doc.get("first_run_note", "")),
        seed_files=seed_files,
    )


def _load_toml(text: str, source: str) -> CLIProfile:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"profile {source}: invalid TOML: {e}") from e
    return _parse_profile(doc, source)


def load_profiles(project: Path | None = None) -> dict[str, CLIProfile]:
    """Packaged built-ins, overlaid by <project>/.bmad-loop/profiles/*.toml."""
    profiles: dict[str, CLIProfile] = {}
    packaged = resources.files("bmad_loop.data").joinpath("profiles")
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".toml"):
            profile = _load_toml(entry.read_text(encoding="utf-8"), entry.name)
            profiles[profile.name] = profile
    if project is not None:
        user_dir = project / USER_PROFILES_REL
        if user_dir.is_dir():
            for path in sorted(user_dir.glob("*.toml")):
                profile = _load_toml(path.read_text(encoding="utf-8"), str(path))
                profiles[profile.name] = profile
    return profiles


def get_profile(name: str, project: Path | None = None) -> CLIProfile:
    profiles = load_profiles(project)
    profile = profiles.get(ALIASES.get(name, name))
    if profile is None:
        raise ProfileError(f"unknown CLI profile: {name!r} (available: {sorted(profiles)})")
    return profile
