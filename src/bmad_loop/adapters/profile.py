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
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from importlib import resources
from pathlib import Path

import regex

from ..platform_util import has_parent_ref, is_absolute_path, names_tree_root

USAGE_PARSERS = {"claude-jsonl", "codex-rollout", "gemini-chat", "copilot-events", "none"}
HOOK_DIALECTS = {
    "claude-settings-json",
    "codex-hooks-json",
    "gemini-settings-json",
    "copilot-settings-json",
    "antigravity-hooks-json",
    # hookless: the adapter observes completion itself (HTTP/SSE transport) —
    # no hook config is ever written, so config_path/events must stay empty.
    "none",
}
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact"}
USER_PROFILES_REL = Path(".bmad-loop") / "profiles"

# legacy adapter names from older policy.toml files, plus friendly short names
ALIASES = {"claude-code-tmux": "claude", "opencode": "opencode-http"}


class ProfileError(Exception):
    pass


# Every fault a raw coercion over a `tomllib` value can raise — the set is CLOSED,
# not a list of the ones seen so far, which is what makes funnelling on it a funnel
# rather than another round of enumeration. `tomllib` yields exactly nine types
# (str, int, float, bool, datetime, date, time, list, dict) and all nine were run
# through this module's coercions: `int()`/`float()` answer TypeError for the five
# non-numerics, ValueError for a non-numeric str and for `nan`, and **OverflowError**
# for `inf`/`-inf` (int) and for an int too large to be a float — `tomllib` accepts
# arbitrary-precision integers, so that second one is reachable from a config file.
# Iteration and `.items()` add TypeError and AttributeError and nothing new.
# `plugins/manifest.py` funnels on the same set for the same reason.
CONVERSION_FAULTS = (AttributeError, OverflowError, TypeError, ValueError)


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
    # Python `re` patterns matched line-by-line against the ANSI-stripped tail of
    # a non-completed session's pane log to classify a transport/API *environment
    # fault* (#194) — e.g. an "API Error … Connection refused" the CLI printed
    # while idling out the session clock. Compiled and validated at parse time
    # (an invalid regex is a profile error). Seeded only for `claude`; empty =
    # inert. Override/extend via a project profile in .bmad-loop/profiles/.
    env_fault_patterns: tuple[str, ...] = ()

    @property
    def hookless(self) -> bool:
        """True for profiles whose adapter observes completion itself (HTTP/SSE)
        instead of via hook scripts — no hook config exists to register, merge,
        validate, or git-exclude."""
        return self.hooks.dialect == "none"

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

    def str_list(key: str) -> tuple[str, ...]:
        # TOML arrays parse as list; reject a bare string (which would iterate to
        # per-character entries) or a scalar (a raw TypeError) with a friendly error.
        raw = doc.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise fail(f"{key} must be a list of strings")
        return tuple(raw)

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
    if dialect == "none":
        # hookless: nothing is ever registered, so a config_path or events map
        # is a contradiction — reject rather than silently ignore.
        if hooks_d.get("config_path") or hooks_d.get("events"):
            raise fail('hookless profiles (dialect = "none") must not set hooks.config_path/events')
        config_path = ""
        events: dict[str, str] = {}
    else:
        config_path = str(hooks_d.get("config_path", ""))
        if (
            names_tree_root(config_path)
            or is_absolute_path(config_path)
            or has_parent_ref(config_path)
        ):
            raise fail("hooks.config_path must be a project-relative path")
        events_d = hooks_d.get("events")
        if not isinstance(events_d, dict) or not events_d:
            raise fail("hooks.events must map native event names to canonical ones")
        events = {str(k): str(v) for k, v in events_d.items()}
        bad = sorted(set(events.values()) - CANONICAL_EVENTS)
        if bad:
            raise fail(
                f"hooks.events values must be canonical {sorted(CANONICAL_EVENTS)}: got {bad}"
            )

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
    if names_tree_root(skill_tree) or is_absolute_path(skill_tree) or has_parent_ref(skill_tree):
        raise fail("skill_tree must be a project-relative path")

    seed_files = str_list("seed_files")
    # `names_tree_root` subsumes the emptiness check it replaced. These entries feed
    # provision_worktree's seed loop, where any spelling of the root ("", ".", "./",
    # ".\") resolves src to the repo root and dst to the worktree — both pass the
    # loop's containment checks, so the whole repo is copied in.
    for seed in seed_files:
        if names_tree_root(seed) or is_absolute_path(seed) or has_parent_ref(seed):
            raise fail(f"seed_files entries must be project-relative paths: got {seed!r}")

    env_fault_patterns = str_list("env_fault_patterns")
    for pattern in env_fault_patterns:
        try:
            regex.compile(pattern)  # same engine the adapter matches with (timeout-guarded)
        except regex.error as e:
            raise fail(f"env_fault_patterns entry is not a valid regex: {pattern!r} ({e})") from e

    return CLIProfile(
        name=name,
        binary=binary,
        hooks=HookSpec(dialect=dialect, config_path=config_path, events=events),
        skill_tree=skill_tree,
        prompt_template=str(doc.get("prompt_template", "{prompt}")),
        launch_args=str_list("launch_args"),
        bypass_args=str_list("bypass_args"),
        model_flag=str(doc.get("model_flag", "--model")),
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        usage_parser=usage_parser,
        usage_grace_s=usage_grace_s,
        stop_without_result_nudges=stop_nudges,
        subagent_stop_without_transcript=bool(doc.get("subagent_stop_without_transcript", False)),
        first_run_note=str(doc.get("first_run_note", "")),
        seed_files=seed_files,
        env_fault_patterns=env_fault_patterns,
    )


def _load_toml(text: str, source: str) -> CLIProfile:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"profile {source}: invalid TOML: {e}") from e
    try:
        return _parse_profile(doc, source)
    except ProfileError:
        raise  # intent: a domain error is never re-wrapped (it is not a CONVERSION_FAULT)
    except CONVERSION_FAULTS as e:
        # A funnel, not per-field guards: `_parse_profile`'s raw conversions
        # (`float()`, `int()`, `.items()`) raise bare conversion errors on
        # TOML-legal values of the wrong type, and every consumer keys its
        # fault handling on ProfileError — the validate role loop reports it,
        # `discovered_profiles` skips the one file. A bare escape crashed
        # `validate` before any document was printed.
        raise ProfileError(f"profile {source}: malformed field value: {e}") from e


def _packaged_profile_files() -> list[tuple[str, Callable[[], str]]]:
    """(source label, text reader) for every packaged profile TOML. Enumeration
    only — nothing is read or parsed here, so callers choose their own fault
    granularity."""
    packaged = resources.files("bmad_loop.data").joinpath("profiles")
    return [
        (entry.name, partial(entry.read_text, encoding="utf-8"))
        for entry in sorted(packaged.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".toml")
    ]


def _overlay_profile_files(project: Path | None) -> list[tuple[str, Callable[[], str]]]:
    """(source label, text reader) for every <project>/.bmad-loop/profiles TOML."""
    if project is None:
        return []
    user_dir = project / USER_PROFILES_REL
    if not user_dir.is_dir():
        return []
    return [
        (str(path), partial(path.read_text, encoding="utf-8"))
        for path in sorted(user_dir.glob("*.toml"))
    ]


def load_profiles(project: Path | None = None) -> dict[str, CLIProfile]:
    """Packaged built-ins, overlaid by <project>/.bmad-loop/profiles/*.toml.

    This is the EFFECTIVE map: it fails fast on the first unreadable or
    malformed file, and a later same-name profile replaces an earlier one.
    Both are what a run wants and both are wrong for reconstructing history —
    see :func:`discovered_profiles`.
    """
    profiles: dict[str, CLIProfile] = {}
    for source, read in [*_packaged_profile_files(), *_overlay_profile_files(project)]:
        profile = _load_toml(read(), source)
        profiles[profile.name] = profile
    return profiles


def discovered_profiles(project: Path | None = None) -> list[CLIProfile]:
    """Every parseable profile from every discovered source — a UNION, never
    the effective map, so a packaged profile and a same-name overlay BOTH
    appear.

    Exists for historical reconstruction (`validate`'s #384 shared-exclude
    check), where :func:`load_profiles`'s semantics are each wrong in a
    different way: fail-fast lets one malformed overlay zero the packaged
    built-ins it had already collected, and last-write-wins lets an ordinary
    valid override silently remove the packaged profile's paths from the set —
    yet the repo may have been polluted while that packaged profile, or a
    since-edited overlay, was the one running. Consumers only ever ADD
    candidates, so over-inclusion is safe by construction.

    Never raises. Enumeration is guarded per SOURCE (losing the overlay dir
    cannot lose the packaged one) and read+parse per ITEM (one bad file skips
    only itself). Faults are silent here because the effective loaders are the
    ones that report them: any file this skips either fails `get_profile` in
    the same run or is not part of today's config at all. A file today's
    schema cannot parse contributes nothing — an under-report the #384
    detector's docstring already records as its safe direction.
    """
    union: list[CLIProfile] = []
    for enumerate_source in (_packaged_profile_files, partial(_overlay_profile_files, project)):
        try:
            files = enumerate_source()
        except OSError:
            continue
        for source, read in files:
            try:
                union.append(_load_toml(read(), source))
            except (ProfileError, OSError, UnicodeError):
                continue
    return union


def get_profile(name: str, project: Path | None = None) -> CLIProfile:
    profiles = load_profiles(project)
    profile = profiles.get(ALIASES.get(name, name))
    if profile is None:
        raise ProfileError(f"unknown CLI profile: {name!r} (available: {sorted(profiles)})")
    return profile
