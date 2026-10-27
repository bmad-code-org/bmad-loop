"""Declarative CLI profiles for the generic tmux adapter.

A profile captures everything that differs between coding CLIs that share the
tmux-injection + hook-signal transport: binary name, how the canonical
"/skill args" prompt is rendered, bypass flags, hook registration (a config
dialect + an event-name map), and which usage parser reads the transcript.

Built-in profiles ship as packaged TOML (bmad_loop/data/profiles/*.toml) and
project-local TOML files in <project>/.bmad-loop/profiles/*.toml overlay them
(same name overrides, new names extend) — adding a CLI that clones an
existing hook dialect needs no Python.

An out-of-tree package advertises additional profiles under the
``bmad_loop.profiles`` entry-point group (:func:`load_profiles` scans it): the
companion to the ``bmad_loop.adapters`` registry (:mod:`~.registry`), so a
co-installed adapter package ships both its class and the profile that selects
it with zero project config. Precedence is packaged < entry-point < project (a
project TOML always wins). A broken entry point degrades to a recorded reason
(:func:`external_profile_errors`), never a crash — one per failing distribution,
kept even when two of them advertise the same name (:mod:`~.entrypoints`).

Which adapter *class* drives a profile is the ``adapter`` field, resolved against
the :mod:`~.registry` — it is read here but intentionally **not** checked against
the set of registered kinds at parse time (an unknown kind is caught at
construction and by ``validate``, against the live registry, never a hardcoded
set). Its *shape* is still enforced here, like every other field.

Both routes into the profile map — the TOML parser and the entry-point scan —
converge on :func:`_validate_profile`, so a Python package cannot install a
profile state a TOML author would have been refused.
"""

from __future__ import annotations

import importlib.metadata
import string
import sys
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import regex

from ..platform_util import (
    has_parent_ref,
    is_absolute_path,
    names_tree_root,
    names_win32_alias,
)
from ..policy import PERMISSION_MODES, PROFILE_ALIASES
from .entrypoints import record_load_error
from .multiplexer import ALLOWED_KEYS

USAGE_PARSERS = {"claude-jsonl", "codex-rollout", "gemini-chat", "copilot-events", "none"}
# The subagent transcript tree a usage parser's CLI writes beside its session
# transcript, for profiles that leave `subagent_transcript_dir` unset. Mirrors
# the subagent anchor tokens.tally already sums (`<stem>/subagents`, walked
# recursively); "{stem}" is the transcript's filename stem.
SUBAGENT_TRANSCRIPT_DIR_DEFAULTS = {"claude-jsonl": "{stem}/subagents"}
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
# Parked-session kinds (DW-348): the CLI is waiting on a human — a permission,
# idle or quota prompt. A parked event never completes a session; the generic
# adapter latches it and, only where the stall logic would otherwise type a wake
# nudge into the pane, withholds the nudge and ends the session as parked
# (`SessionResult.parked`). A profile reaches these either by mapping a native
# event straight to one, or through the `Notification` carrier plus
# `hooks.notification_types` (native subtype -> parked kind).
PARKED_EVENTS = frozenset({"PermissionPrompt", "IdlePrompt", "QuotaPrompt"})
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact", "Notification"} | set(
    PARKED_EVENTS
)
USER_PROFILES_REL = Path(".bmad-loop") / "profiles"

# Legacy adapter names from older policy.toml files, plus friendly short names.
# The table itself lives in `policy` (see `PROFILE_ALIASES` there for why) and
# is re-exported here under the name `get_profile` and `install` always used.
ALIASES = PROFILE_ALIASES


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
    # Subtypes of the canonical `Notification` carrier that mean "parked on a
    # human" (DW-348): the relay forwards the payload's `notification_type`, and
    # this table maps it onto a PARKED_EVENTS kind — e.g. claude's
    # `permission_prompt` -> `PermissionPrompt`. An unmapped subtype is ignored.
    # Non-empty requires some native event mapped to `Notification`. APPENDED
    # with a default so every positional HookSpec construction stays valid.
    notification_types: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceTrustSpec:
    """Where a CLI records the workspaces it trusts (DW-390).

    Some CLIs gate every workspace on an exact-path allowlist in a home-level
    settings file (agy: ``~/.gemini/antigravity-cli/settings.json``
    ``trustedWorkspaces``), so a freshly provisioned worktree blocks on an
    interactive trust dialog no spawned session can answer. Declaring the file
    and its top-level list key lets :mod:`bmad_loop.workspace_trust` extend the
    grant the operator already gave the project root to each unit worktree.

    ``settings_path`` must be ``~/``-anchored (home-relative, no ``..``, no
    Windows-alias component) and ``key`` names ONE top-level key — never a nested
    path. Both are validated in :func:`_validate_profile`, so both profile routes
    enforce the same shape."""

    settings_path: str  # "~/"-prefixed, e.g. "~/.gemini/antigravity-cli/settings.json"
    key: str  # a top-level key holding a list of path strings


@dataclass(frozen=True)
class ParkedAnswersSpec:
    """The keys that answer a parked permission prompt from the dashboard (#855).

    ``approve`` / ``deny`` are named keys (each one of
    :data:`~.multiplexer.ALLOWED_KEYS`) pressed in order with no implicit Enter.
    ``approve_guard`` / ``deny_guard`` are regexes that must ALL match a fresh
    capture of the visible pane before that choice's keys are sent: the same
    keys answer other dialogs differently (Enter on Claude Code's bypass startup
    dialog chooses "No, exit"), so a screen that is not plainly the prompt sends
    nothing. Both guard lists are non-empty and compile; validated in
    :func:`_validate_profile`."""

    approve: tuple[str, ...]
    deny: tuple[str, ...]
    approve_guard: tuple[str, ...]
    deny_guard: tuple[str, ...]


# The `sys.platform` values a `[permission_modes.<mode>]` table may name.
PERMISSION_MODE_PLATFORMS = frozenset({"linux", "darwin", "win32"})


@dataclass(frozen=True)
class PermissionModeSpec:
    """The launch args of one non-bypass ``adapter.permission_mode`` (#727).

    ``args`` stand in for ``bypass_args`` when the mode is selected
    (:meth:`CLIProfile.for_permission_mode`), so ``extra_args`` replaces them
    exactly as it replaces ``bypass_args``. ``platforms`` names the
    ``sys.platform`` values the mapping is known to work on; empty = every
    platform. Mode names, args and platforms are validated in
    :func:`_validate_profile`."""

    args: tuple[str, ...]
    platforms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CLIProfile:
    name: str
    binary: str
    hooks: HookSpec
    # Which adapter *class* drives this CLI — a key resolved against the adapter
    # registry (adapters/registry.py), not a hardcoded enum. "generic" = the
    # bundled tmux-injection + hook-signal adapter; "opencode-http" = the bundled
    # HTTP/SSE adapter; an out-of-tree package registers its own. Membership is NOT
    # checked at parse time: an unknown kind fails loud at construction and as a
    # `validate` finding, both against the live registry. A hookless HTTP profile
    # (hooks.dialect = "none") MUST set this to its HTTP adapter kind — the
    # transport (hookless) and the driving class are now decoupled axes.
    #
    # That MUST binds the two routes differently, which is why the default below
    # is not the whole story. A TOML profile that OMITS the key is read as
    # predating the field and keeps the old dialect-based dispatch
    # (`_legacy_adapter_default`), so a hookless file still lands on the HTTP
    # kind. A provider that constructs this dataclass directly takes the default
    # verbatim — nothing infers a kind for it — so an entry-point profile really
    # must set the field, and a hookless one that leaves it at "generic" resolves
    # to a mux-driving kind that will never see a Stop hook.
    adapter: str = "generic"
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
    # Patterns matched line-by-line against the ANSI-stripped tail of a
    # non-completed session's log to classify a provider/transport *environment
    # fault* (#194) — an "API Error … Connection refused" or a "usage limit
    # reached" the CLI printed while idling out the session clock. Which log is
    # scanned is per-adapter, named by EnvFaultMixin.ENV_FAULT_LOG_SUFFIX: the
    # tmux adapters' pane capture logs/<task_id>.log, or logs/<task_id>.server.out
    # for opencode-http (whose .log is a model-written transcript). Compiled and
    # validated at parse time (an invalid regex is a profile error); empty = inert.
    #
    # A pattern is only sound against a log the model cannot write to. A pane
    # capture is not one: it carries the model's own output, so a bare `quota`
    # or `429` matches a story that merely *implements* rate limiting, and an
    # error-shaped token joined to a cause on the SAME line is precisely what a
    # story writing ABOUT the error emits — the doctrine that used to live here,
    # withdrawn because it classified 44 of this repo's own tracked lines (#507).
    # So a pane-capture profile's pattern must reproduce one COMPLETE error
    # sentence its CLI was captured printing, and must not match its own line in
    # the profile file — hence the shipped single-character classes (`t[o]`,
    # `respons[e]`), which change what the source line matches and nothing else.
    # Both halves are pinned in tests/test_env_fault_patterns.py, the second
    # across every tracked file. Override/extend via a project profile in
    # .bmad-loop/profiles/.
    env_fault_patterns: tuple[str, ...] = ()
    # Patterns matched line-by-line, then across 2-3 wrapped rows
    # (`panematch.match_rows`), against the ANSI-stripped VISIBLE pane
    # (`mux.capture_pane`), only at the moment a stall wake nudge is about to be
    # typed into it (DW-350). A match means the CLI is parked on a prompt only a
    # human should answer — the nudge's trailing Enter could confirm it (#727:
    # Enter chose "No, exit" on Claude Code's Bypass Permissions dialog) — so the
    # nudge is withheld and the session ends as parked instead. Same evidentiary
    # bar as env_fault_patterns: seed only lines captured from the CLI's own
    # screen, with single-character classes so the profile line does not match
    # itself. Compiled and validated at parse time; empty = inert.
    parked_prompt_patterns: tuple[str, ...] = ()
    # Did this profile ship INSIDE the package (bmad_loop/data/profiles/*.toml)?
    # Provenance, not configuration: it is stamped by `load_profiles` at the one
    # place that knows which directory a file came from, and no TOML key sets it —
    # a project overlay declaring `packaged = true` is read as an unknown key, not
    # as a promotion. Default False, so the untrusted answer is the one you get by
    # forgetting: an entry-point profile constructing this dataclass directly is
    # not packaged either, because "installed alongside us" is not the same claim
    # as "shipped by us".
    #
    # The one consumer is `validate`'s liveness probe (#294), which EXECUTES the
    # binary. `binary` is project-controlled end to end — policy.toml picks the
    # profile and .bmad-loop/profiles/*.toml supplies its fields, both arriving
    # with a clone — so the probe needs a trust boundary that no spelling of
    # `binary` can talk its way through: gating on the string (rejecting "./tool")
    # still runs a bare "pwn" whenever a checkout-local directory is on PATH.
    # Provenance is that boundary; it answers "who wrote this", which is the
    # question actually being asked.
    packaged: bool = False
    # Home-level workspace-trust allowlist this CLI gates sessions on (DW-390);
    # None = the CLI has none, and provisioning never reads or writes under `~`.
    # APPENDED with a default so every positional CLIProfile construction stays
    # valid.
    workspace_trust: WorkspaceTrustSpec | None = None
    # The CLI's launch flag for a caller-chosen session id (DW-505), e.g. claude's
    # "--session-id". When set, the generic adapter mints a UUID4 per launch,
    # appends `[session_id_flag, uuid]` to the launched argv and pins hook-event
    # attribution to it (signals.SessionAttribution), so a nested child's
    # SessionEnd is foreign even when the child never announced a SessionStart
    # (DW-508). "" = off: attribution learns the id from the first SessionStart.
    # An argv flag, so it is part of runsetup.config_digest's launch surface.
    # APPENDED after workspace_trust for the same positional-compatibility reason.
    session_id_flag: str = ""
    # Template, relative to the transcript's directory, of the subagent transcript
    # tree the CLI writes beside the session transcript ("{stem}" = the
    # transcript's filename stem). The idle notice stat-walks it recursively and
    # counts its writes as session activity — never read, never usage. None =
    # derive from usage_parser (SUBAGENT_TRANSCRIPT_DIR_DEFAULTS); "" = off.
    # APPENDED after session_id_flag for the same positional-compatibility reason.
    subagent_transcript_dir: str | None = None
    # Patterns naming a subscription/usage limit the CLI is stopped behind (#610),
    # matched line-by-line (then across 2-3 wrapped rows) against the
    # ANSI-stripped VISIBLE pane at the same live
    # decision points as parked_prompt_patterns (stall-grace expiry, and once when
    # the session timeout fires) and, as ordinary env-fault families, against the
    # post-mortem log tail. Two lists, disjoint by contract — "usage-limited" means
    # either one matched:
    #   usage_limit_patterns: the CLI is BLOCKED and will not continue by itself
    #     (credit exhaustion); pause now, an operator must act.
    #   usage_wait_patterns: the CLI is blocked but WAITING for a reset it then
    #     continues from on its own (a session-window limit).
    # A LIVE pane sighting exempts the verdict from the #764 token veto (the line is
    # on screen now, so it is not a stale match from earlier in the session); a
    # tail-only match is still subject to it. Same evidentiary bar as
    # env_fault_patterns: seed only lines captured from the CLI's own screen, with
    # the run cited, and single-character classes so the profile line does not
    # match itself. Compiled and validated at parse time; empty = inert.
    # APPENDED after subagent_transcript_dir for the same positional-compatibility
    # reason.
    usage_limit_patterns: tuple[str, ...] = ()
    usage_wait_patterns: tuple[str, ...] = ()
    # The CLI's auto-deny countdown on a permission prompt it will answer "no"
    # to by itself. Matched (then across 2-3 wrapped rows) against the visible
    # pane once when a PermissionPrompt latches (and once more on the next tick
    # when nothing matched), never on a timer; each pattern names groups `min`
    # and `sec`, read into the `auto_deny_at` of the `session-parked` journal
    # event. Same evidentiary bar as usage_limit_patterns. Compiled and
    # validated at parse time; empty = no deadline is ever read. APPENDED after
    # usage_wait_patterns for the same positional-compatibility reason.
    auto_deny_patterns: tuple[str, ...] = ()
    # The keys that answer a parked permission prompt from the TUI (#855); None =
    # the CLI offers no answer keys and the operator attaches to answer. APPENDED
    # after auto_deny_patterns for the same positional-compatibility reason.
    parked_answers: ParkedAnswersSpec | None = None
    # Non-bypass `adapter.permission_mode` mappings, keyed by mode name (#727);
    # a mode absent here is refused for this CLI. APPENDED after parked_answers
    # for the same positional-compatibility reason.
    permission_modes: dict[str, PermissionModeSpec] = field(default_factory=dict)

    @property
    def hookless(self) -> bool:
        """True for profiles whose adapter observes completion itself (HTTP/SSE)
        instead of via hook scripts — no hook config exists to register, merge,
        validate, or git-exclude."""
        return self.hooks.dialect == "none"

    @property
    def subagent_transcript_root(self) -> str:
        """The effective subagent-tree template: the declared
        `subagent_transcript_dir`, else the usage parser's default, else "" (off)."""
        if self.subagent_transcript_dir is not None:
            return self.subagent_transcript_dir
        return SUBAGENT_TRANSCRIPT_DIR_DEFAULTS.get(self.usage_parser, "")

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

    def for_permission_mode(self, mode: str, platform: str = sys.platform) -> CLIProfile:
        """This profile with ``bypass_args`` set to ``mode``'s launch args.

        ``"bypass"`` returns the profile unchanged. Any other mode swaps in its
        ``[permission_modes.<mode>]`` args, so every reader of ``bypass_args``
        (the launched argv, the config digest, :meth:`missing_bypass_tokens`)
        sees the effective permission args with no second code path. Raises
        :class:`ProfileError` when the profile has no mapping for ``mode`` or
        the mapping excludes ``platform``."""
        if mode == "bypass":
            return self
        spec = self.permission_modes.get(mode)
        if spec is None:
            raise ProfileError(
                f"profile {self.name!r} has no permission_mode {mode!r} mapping "
                f'(no [permission_modes.{mode}] table); use permission_mode = "bypass" '
                "or another profile"
            )
        if spec.platforms and platform not in spec.platforms:
            raise ProfileError(
                f"profile {self.name!r} maps permission_mode {mode!r} only on "
                f"{', '.join(spec.platforms)}, not {platform}"
            )
        return replace(self, bypass_args=spec.args)

    def missing_bypass_tokens(self, extra_args: tuple[str, ...] | None) -> tuple[str, ...]:
        """The permission args (``bypass_args``) a policy ``extra_args`` override
        drops (DW-349).

        The replace rule (``GenericAdapter.interactive_argv``): a non-None
        ``extra_args`` REPLACES ``bypass_args`` rather than extending it, so any
        permission token that is not an exact element of ``launch_args + extra_args``
        is absent from the launched argv. On a profile resolved by
        :meth:`for_permission_mode` these are that mode's args. ``None`` means
        "inherit the profile's permission args" and drops nothing; ``()`` is an explicit override and drops
        them all. Order kept, duplicates collapsed."""
        if extra_args is None:
            return ()
        present = {*self.launch_args, *extra_args}
        return tuple(t for t in dict.fromkeys(self.bypass_args) if t not in present)


def _validate_profile(profile: CLIProfile, source: str) -> None:
    """Enforce every value-level invariant a ``CLIProfile`` must satisfy, whatever
    built it.

    Two routes reach the profile map and only one has a parser in front of it:
    :func:`_parse_profile` coerces a TOML document, while a ``bmad_loop.profiles``
    entry point hands over an already-constructed instance. Holding the invariants
    in one function is what stops those routes drifting — the failure it closes is
    a Python package installing a state the TOML parser would have refused, and
    the sharpest instance is an ``env_fault_patterns`` entry that is not a valid
    regex: unchecked, it trades a compile error at LOAD time for one at MATCH
    time, inside a session's env-fault classification, where the caller degrades
    rather than raises and the pattern silently never fires.

    Scope is semantic, not type-level. A package that constructs a ``CLIProfile``
    with the wrong runtime type in a field (a list where a ``str`` belongs) is a
    bug this deliberately does not chase: reaching here already ran that package's
    code in-process, so this is not a security boundary and hardening it as one
    would invite it being trusted as one. What it does catch is the well-typed and
    wrong profile — exactly what a TOML author is told about at parse time."""

    def fail(msg: str) -> ProfileError:
        return ProfileError(f"profile {source}: {msg}")

    if not profile.name.strip() or not profile.binary.strip():
        raise fail("'name' and 'binary' are required")

    hooks = profile.hooks
    if hooks.dialect not in HOOK_DIALECTS:
        raise fail(f"hooks.dialect must be one of {sorted(HOOK_DIALECTS)}: got {hooks.dialect!r}")
    if hooks.dialect == "none":
        # hookless: nothing is ever registered, so a config_path or events map
        # is a contradiction — reject rather than silently ignore.
        if hooks.config_path or hooks.events or hooks.notification_types:
            raise fail(
                'hookless profiles (dialect = "none") must not set '
                "hooks.config_path/events/notification_types"
            )
    else:
        if (
            names_tree_root(hooks.config_path)
            or is_absolute_path(hooks.config_path)
            or has_parent_ref(hooks.config_path)
        ):
            # `names_tree_root("")` is True, so this arm also carries the
            # "a real dialect must name a config_path at all" case.
            raise fail("hooks.config_path must be a project-relative path")
        if names_win32_alias(hooks.config_path):
            raise fail(
                "hooks.config_path must not name a Windows device or end a component "
                "in a period or space"
            )
        if not hooks.events:
            raise fail("hooks.events must map native event names to canonical ones")
        bad = sorted(set(hooks.events.values()) - CANONICAL_EVENTS)
        if bad:
            raise fail(
                f"hooks.events values must be canonical {sorted(CANONICAL_EVENTS)}: got {bad}"
            )
        if hooks.notification_types:
            bad_kinds = sorted(set(hooks.notification_types.values()) - PARKED_EVENTS)
            if bad_kinds:
                raise fail(
                    "hooks.notification_types values must be parked kinds "
                    f"{sorted(PARKED_EVENTS)}: got {bad_kinds}"
                )
            if "Notification" not in hooks.events.values():
                raise fail(
                    "hooks.notification_types needs a native event mapped to the "
                    "canonical 'Notification' carrier in hooks.events"
                )

    # Shape only — membership against the registered kinds is deliberately NOT
    # checked here (see the module docstring): that set is open-ended and lives in
    # adapters/registry.py, which importing from here would make a cycle.
    # `.strip()` because the TOML route strips before it gets here: testing the raw
    # value would refuse `adapter = "  "` from a file while admitting it from an
    # entry-point provider, which is exactly the two-routes divergence this
    # function exists to prevent.
    if not profile.adapter.strip():
        raise fail("adapter must be a non-empty string naming an adapter kind")

    # The emptiness tests above use `.strip()` because the TOML route CANONICALIZES
    # before it gets here — `_parse_profile` strips exactly these three fields — so
    # testing the raw value would refuse `adapter = "  "` from a file while
    # admitting it from a provider. But validating a stripped COPY while the frozen
    # original is what gets installed leaves the other half of that same divergence
    # open: `" acme "` is content `_parse_profile` cannot produce, and every
    # consumer keys on the exact string. The profile lands under a map key `--cli
    # acme` never finds, a `binary` `shutil.which` never resolves, and an `adapter`
    # `get_adapter_kind` reports as an unknown kind — while the provider that
    # shipped it is recorded as perfectly fine.
    #
    # Refused, not normalized: this function validates and does not rewrite, and a
    # frozen dataclass rebuilt here would leave the caller holding the original
    # anyway. Refusing is also the louder half — the provider is dropped WITH a
    # reason naming the field, which is what the recorded-degrade contract owes an
    # operator. Ordered after the emptiness tests so `"   "` still reads as empty
    # rather than as non-canonical.
    for label, value in (
        ("name", profile.name),
        ("binary", profile.binary),
        ("adapter", profile.adapter),
    ):
        if value != value.strip():
            raise fail(
                f"{label} must not carry leading/trailing whitespace: {value!r} "
                "(the TOML route strips it; a provider must hand over the "
                "canonical value so both routes install the same profile)"
            )

    # The one coherence rule between the two axes, and the only place both routes
    # pass through. `generic` is the bundled tmux adapter: it injects into a window
    # and completes on a Stop hook (or the window dying), so pairing it with
    # dialect = "none" — which means nothing ever registers that hook — describes a
    # session that can only wait out `session_timeout_min` against an interactive
    # CLI that never exits. Both routes could reach it: a TOML file naming the pair
    # outright, and an entry-point provider that builds a hookless `HookSpec` while
    # leaving `adapter` at its dataclass default. (The absent-key TOML case cannot:
    # `_legacy_adapter_default` sends a hookless file to the HTTP kind.)
    #
    # This is the membership check's opposite, not an instance of it: naming ONE
    # bundled kind is a fact about that adapter's completion contract, which this
    # package owns, and the same latitude `validate`'s httpx check takes. Hookless
    # on any OTHER kind stays legal — that decoupling is what the registry is for,
    # and an out-of-tree kind's completion contract is its own to state.
    from .registry import GENERIC

    if profile.hookless and profile.adapter.strip() == GENERIC:
        raise fail(
            f'hookless profiles (dialect = "none") cannot select the {GENERIC!r} adapter: '
            "it completes on a Stop hook a hookless profile never registers, so the "
            "session would wait out session_timeout_min. Name the adapter kind that "
            "drives this CLI over its own transport."
        )

    # "" = off. Otherwise ONE option token: the adapter appends it and the minted
    # id as two argv elements, so whitespace (which could never be one flag) or a
    # non-option word (which a CLI reads as a positional — the prompt slot) is a
    # profile error, not a launch surprise. A hookless profile has no hook events
    # to attribute, so a pin there is a contradiction.
    flag = profile.session_id_flag
    if flag:
        # No `=`: the id is passed as the NEXT argv element, so `--session-id=`
        # would launch as the two tokens `--session-id=` `<uuid>`.
        if (
            not flag.startswith("-")
            or flag.strip("-") == ""
            or "=" in flag
            or any(c.isspace() for c in flag)
        ):
            raise fail(
                "session_id_flag must be one option token starting with '-', with no "
                "whitespace and no '=' (the id is passed as the next argument, e.g. "
                f'"--session-id"), or "" for none: got {flag!r}'
            )
        if profile.hookless:
            raise fail(
                'hookless profiles (dialect = "none") must not set session_id_flag: '
                "it pins hook-event attribution, and a hookless profile has no hook events"
            )

    # Only an explicit non-empty template is checked: None derives from a known
    # usage_parser, "" is off. "{stem}" is required — a tree shared by sibling
    # sessions would let another session's writes mask this one's idleness — and
    # the rendered path must stay under the transcript's directory.
    subdir = profile.subagent_transcript_dir
    if subdir:
        # TOML accepts a "\u0000" escape; the rendered path would make the
        # tree walk raise ValueError, not the OSError it degrades.
        if "\0" in subdir:
            raise fail(f"subagent_transcript_dir must not contain a NUL character: got {subdir!r}")
        try:
            parts = list(string.Formatter().parse(subdir))
        except ValueError as e:
            raise fail(f"subagent_transcript_dir is not a valid template: {e}: got {subdir!r}")
        fields = [(name, spec, conv) for _, name, spec, conv in parts if name is not None]
        if not fields or any(name != "stem" or spec or conv for name, spec, conv in fields):
            raise fail(
                'subagent_transcript_dir must use exactly the placeholder "{stem}" (the '
                "transcript's filename stem), with no other field, format spec or "
                f"conversion: got {subdir!r}"
            )
        if is_absolute_path(subdir) or has_parent_ref(subdir):
            raise fail(
                "subagent_transcript_dir must be a relative path under the transcript's "
                f"directory, with no '..': got {subdir!r}"
            )
        if profile.hookless:
            raise fail(
                'hookless profiles (dialect = "none") must not set subagent_transcript_dir: '
                "it extends the transcript idle notice, and a hookless profile has no "
                "transcript path"
            )

    if profile.usage_parser not in USAGE_PARSERS:
        raise fail(
            f"usage_parser must be one of {sorted(USAGE_PARSERS)}: got {profile.usage_parser!r}"
        )

    if profile.usage_grace_s < 0:
        raise fail(f"usage_grace_s must be >= 0: got {profile.usage_grace_s}")

    nudges = profile.stop_without_result_nudges
    if nudges is not None and nudges < 0:
        raise fail(f"stop_without_result_nudges must be >= 0: got {nudges}")

    if (
        names_tree_root(profile.skill_tree)
        or is_absolute_path(profile.skill_tree)
        or has_parent_ref(profile.skill_tree)
    ):
        raise fail("skill_tree must be a project-relative path")

    if names_win32_alias(profile.skill_tree):
        raise fail(
            "skill_tree must not name a Windows device or end a component in a period or space"
        )

    # `names_tree_root` subsumes the emptiness check it replaced. These entries feed
    # provision_worktree's seed loop, where any spelling of the root ("", ".", "./",
    # ".\") resolves src to the repo root and dst to the worktree — both pass the
    # loop's containment checks, so the whole repo is copied in.
    for seed in profile.seed_files:
        if names_tree_root(seed) or is_absolute_path(seed) or has_parent_ref(seed):
            raise fail(f"seed_files entries must be project-relative paths: got {seed!r}")
        if names_win32_alias(seed):
            raise fail(
                "seed_files entries must not name a Windows device or end a component "
                f"in a period or space: got {seed!r}"
            )

    trust = profile.workspace_trust
    if trust is not None:
        settings_path = trust.settings_path
        rest = settings_path[2:]
        # Home-anchored and nothing else: the file lives outside the project, so
        # the confinement is "under ~", and the remainder must name something
        # inside home — not home itself, not an absolute path smuggled after the
        # prefix (`~//etc/passwd` joins to `/etc/passwd`), not a `..` climb out.
        if (
            not settings_path.startswith("~/")
            or names_tree_root(rest)
            or is_absolute_path(rest)
            or has_parent_ref(rest)
        ):
            raise fail(
                "workspace_trust.settings_path must be a '~/'-prefixed path inside the "
                f"home directory: got {settings_path!r}"
            )
        if names_win32_alias(rest):
            raise fail(
                "workspace_trust.settings_path must not name a Windows device or end a "
                f"component in a period or space: got {settings_path!r}"
            )
        if not trust.key.strip() or trust.key != trust.key.strip():
            raise fail(
                "workspace_trust.key must name one top-level key (non-empty, no "
                f"surrounding whitespace): got {trust.key!r}"
            )

    for pattern in profile.env_fault_patterns:
        try:
            regex.compile(pattern)  # same engine the adapter matches with (timeout-guarded)
        except (regex.error, RecursionError) as e:
            raise fail(f"env_fault_patterns entry is not a valid regex: {pattern!r} ({e})") from e

    for pattern in profile.parked_prompt_patterns:
        try:
            regex.compile(pattern)  # same engine the stall gate matches with (timeout-guarded)
        except (regex.error, RecursionError) as e:
            raise fail(
                f"parked_prompt_patterns entry is not a valid regex: {pattern!r} ({e})"
            ) from e

    for field_name in ("usage_limit_patterns", "usage_wait_patterns"):
        for pattern in getattr(profile, field_name):
            try:
                regex.compile(pattern)  # same engine the usage gate matches with (timeout-guarded)
            except (regex.error, RecursionError) as e:
                raise fail(f"{field_name} entry is not a valid regex: {pattern!r} ({e})") from e

    for pattern in profile.auto_deny_patterns:
        try:
            compiled = regex.compile(pattern)  # same engine the parked notice matches with
        except (regex.error, RecursionError) as e:
            raise fail(f"auto_deny_patterns entry is not a valid regex: {pattern!r} ({e})") from e
        missing = sorted({"min", "sec"} - compiled.groupindex.keys())
        if missing:
            raise fail(
                f"auto_deny_patterns entry must name groups 'min' and 'sec': {pattern!r} "
                f"(missing {', '.join(missing)})"
            )

    for mode, spec in profile.permission_modes.items():
        if mode == "bypass" or mode not in PERMISSION_MODES:
            raise fail(
                f"permission_modes.{mode}: unknown mode (one of "
                f"{sorted(PERMISSION_MODES - {'bypass'})}; bypass is bypass_args)"
            )
        if not spec.args or not all(isinstance(a, str) and a for a in spec.args):
            raise fail(f"permission_modes.{mode}.args must list at least one non-empty string")
        bad = [p for p in spec.platforms if p not in PERMISSION_MODE_PLATFORMS]
        if bad:
            raise fail(
                f"permission_modes.{mode}.platforms must name values from "
                f"{sorted(PERMISSION_MODE_PLATFORMS)}: got {bad!r}"
            )

    answers = profile.parked_answers
    if answers is not None:
        for field_name in ("approve", "deny"):
            keys = getattr(answers, field_name)
            bad = [key for key in keys if key not in ALLOWED_KEYS]
            if not keys or bad:
                raise fail(
                    f"parked_answers.{field_name} must list named keys from "
                    f"{sorted(ALLOWED_KEYS)}: got {list(keys)!r}"
                )
        for field_name in ("approve_guard", "deny_guard"):
            guards = getattr(answers, field_name)
            # Empty would let the keys out on any screen, the one thing a guard
            # exists to stop.
            if not guards:
                raise fail(f"parked_answers.{field_name} must list at least one pattern")
            for pattern in guards:
                try:
                    regex.compile(pattern)  # same engine the answer guard matches with
                except (regex.error, RecursionError) as e:
                    raise fail(
                        f"parked_answers.{field_name} entry is not a valid regex: {pattern!r} ({e})"
                    ) from e


def _legacy_adapter_default(dialect: str) -> str:
    """The adapter kind a TOML profile that predates the ``adapter`` field meant.

    Before the registry, ``hooks.dialect`` WAS the class selector: ``make_adapters``
    sent every hookless profile to the opencode HTTP adapters and everything else to
    the generic ones. Project overlays are a documented customization point
    (``<project>/.bmad-loop/profiles/*.toml``, same name overrides), and the way to
    tweak the opencode profile's ``binary``/``env``/``model`` was to copy the
    packaged one — which carried no ``adapter`` key, because the key did not exist.
    Taking the dataclass default for those files would silently move a working
    hookless run onto tmux, where it launches the CLI in a window and waits out
    ``session_timeout_min`` for a ``Stop`` hook a hookless profile never registers —
    and ``validate`` stays green, because every check it would trip keys on
    ``hookless`` too. So reproduce the old dispatch instead of defaulting.

    Only the absent key takes this path; an explicit ``adapter`` is always honored,
    including the now-legal hookless-but-not-``opencode-http`` combination the axes
    were decoupled to allow. Naming the two bundled kinds here is a fact about what
    the *old* dispatch did, not a valid-kinds set — that set is only ever
    ``registry.known_adapter_kinds()``. Imported inside the function so this module
    keeps no import-time dependency on the registry."""
    from .registry import GENERIC, OPENCODE_HTTP

    return OPENCODE_HTTP if dialect == "none" else GENERIC


def _parse_profile(doc: dict, source: str) -> CLIProfile:
    """Coerce a TOML document into a :class:`CLIProfile`.

    SHAPE only: the container and element types a TOML document can get wrong and
    a constructed dataclass cannot. Every value-level invariant lives in
    :func:`_validate_profile`, called on the result — so the entry-point route,
    which has no document to coerce, enforces exactly the same set."""

    def fail(msg: str) -> ProfileError:
        return ProfileError(f"profile {source}: {msg}")

    def str_list(key: str) -> tuple[str, ...]:
        # TOML arrays parse as list; reject a bare string (which would iterate to
        # per-character entries) or a scalar (a raw TypeError) with a friendly error.
        raw = doc.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise fail(f"{key} must be a list of strings")
        return tuple(raw)

    hooks_d = doc.get("hooks")
    if not isinstance(hooks_d, dict):
        raise fail("missing [hooks] table")
    events_d = hooks_d.get("events", {})
    if not isinstance(events_d, dict):
        raise fail("hooks.events must map native event names to canonical ones")
    notification_d = hooks_d.get("notification_types", {})
    if not isinstance(notification_d, dict) or not all(
        isinstance(v, str) for v in notification_d.values()
    ):
        raise fail("hooks.notification_types must map notification subtypes to parked kinds")

    # Optional table; SHAPE here (a table of two strings), values in
    # `_validate_profile`. A bare `str()` coercion would turn an array into the
    # literal "['x']" and carry it to the filesystem as a path.
    trust_d = doc.get("workspace_trust")
    workspace_trust: WorkspaceTrustSpec | None = None
    if trust_d is not None:
        if not isinstance(trust_d, dict):
            raise fail("workspace_trust must be a table")
        unknown = sorted(set(trust_d) - {"settings_path", "key"})
        if unknown:
            raise fail(f"workspace_trust has unknown keys: {unknown}")
        settings_path, key = trust_d.get("settings_path"), trust_d.get("key")
        if not isinstance(settings_path, str) or not isinstance(key, str):
            raise fail("workspace_trust.settings_path and workspace_trust.key must be strings")
        workspace_trust = WorkspaceTrustSpec(settings_path=settings_path, key=key)

    # Same shape-only rule as workspace_trust: four string lists, values in
    # `_validate_profile`.
    answers_d = doc.get("parked_answers")
    parked_answers: ParkedAnswersSpec | None = None
    if answers_d is not None:
        if not isinstance(answers_d, dict):
            raise fail("parked_answers must be a table")
        answer_keys = ("approve", "deny", "approve_guard", "deny_guard")
        unknown = sorted(set(answers_d) - set(answer_keys))
        if unknown:
            raise fail(f"parked_answers has unknown keys: {unknown}")
        lists: dict[str, tuple[str, ...]] = {}
        for key in answer_keys:
            raw = answers_d.get(key, [])
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise fail(f"parked_answers.{key} must be a list of strings")
            lists[key] = tuple(raw)
        parked_answers = ParkedAnswersSpec(**lists)

    # `[permission_modes.<mode>]` tables of two string lists; mode names and
    # values are checked in `_validate_profile`.
    modes_d = doc.get("permission_modes", {})
    if not isinstance(modes_d, dict):
        raise fail("permission_modes must be a table of [permission_modes.<mode>] tables")
    permission_modes: dict[str, PermissionModeSpec] = {}
    for mode, spec_d in modes_d.items():
        if not isinstance(spec_d, dict):
            raise fail(f"permission_modes.{mode} must be a table")
        unknown = sorted(set(spec_d) - {"args", "platforms"})
        if unknown:
            raise fail(f"permission_modes.{mode} has unknown keys: {unknown}")
        mode_lists: dict[str, tuple[str, ...]] = {}
        for key in ("args", "platforms"):
            raw = spec_d.get(key, [])
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise fail(f"permission_modes.{mode}.{key} must be a list of strings")
            mode_lists[key] = tuple(raw)
        permission_modes[mode] = PermissionModeSpec(**mode_lists)

    # A dedicated shape check rather than the `str()` coercion the neighbouring
    # scalars get, because `adapter` has no parse-time membership test to land in
    # afterwards: `str(["x"])` would coerce a TOML array to the literal `"['x']"`
    # and carry it all the way to `get_adapter_kind`, which would then name that
    # nonsense as the unknown kind. #384's rule — a malformed value funnels into
    # ProfileError at the boundary, never a silent coercion.
    raw_adapter = doc.get("adapter")
    if raw_adapter is None:
        raw_adapter = _legacy_adapter_default(str(hooks_d.get("dialect", "")))
    if not isinstance(raw_adapter, str):
        raise fail(f"adapter must be a string: got {type(raw_adapter).__name__}")

    # Same rule as `adapter`: an argv token, so a TOML array or number must not be
    # `str()`-coerced into a flag the CLI is then launched with.
    raw_session_id_flag = doc.get("session_id_flag", "")
    if not isinstance(raw_session_id_flag, str):
        raise fail(f"session_id_flag must be a string: got {type(raw_session_id_flag).__name__}")

    # None (absent) is meaningful — it derives the default from usage_parser — so
    # it is kept, and anything present must already be a string.
    raw_subagent_dir = doc.get("subagent_transcript_dir")
    if raw_subagent_dir is not None and not isinstance(raw_subagent_dir, str):
        raise fail(
            f"subagent_transcript_dir must be a string: got {type(raw_subagent_dir).__name__}"
        )

    profile = CLIProfile(
        name=str(doc.get("name", "")).strip(),
        binary=str(doc.get("binary", "")).strip(),
        hooks=HookSpec(
            dialect=str(hooks_d.get("dialect", "")),
            config_path=str(hooks_d.get("config_path", "")),
            events={str(k): str(v) for k, v in events_d.items()},
            notification_types={str(k): v for k, v in notification_d.items()},
        ),
        adapter=raw_adapter.strip(),
        skill_tree=str(doc.get("skill_tree", ".claude/skills")),
        prompt_template=str(doc.get("prompt_template", "{prompt}")),
        launch_args=str_list("launch_args"),
        bypass_args=str_list("bypass_args"),
        model_flag=str(doc.get("model_flag", "--model")),
        session_id_flag=raw_session_id_flag,
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        usage_parser=str(doc.get("usage_parser", "none")),
        # `float()`/`int()` are the raw coercions `_load_toml`'s CONVERSION_FAULTS
        # funnel exists for: they answer OverflowError for `inf` and for an integer
        # too large to be a float, both of which are legal TOML.
        usage_grace_s=float(doc.get("usage_grace_s", 0.0)),
        stop_without_result_nudges=(
            None if (raw := doc.get("stop_without_result_nudges")) is None else int(raw)
        ),
        subagent_stop_without_transcript=bool(doc.get("subagent_stop_without_transcript", False)),
        first_run_note=str(doc.get("first_run_note", "")),
        seed_files=str_list("seed_files"),
        env_fault_patterns=str_list("env_fault_patterns"),
        parked_prompt_patterns=str_list("parked_prompt_patterns"),
        usage_limit_patterns=str_list("usage_limit_patterns"),
        usage_wait_patterns=str_list("usage_wait_patterns"),
        auto_deny_patterns=str_list("auto_deny_patterns"),
        workspace_trust=workspace_trust,
        subagent_transcript_dir=raw_subagent_dir,
        parked_answers=parked_answers,
        permission_modes=permission_modes,
    )
    _validate_profile(profile, source)
    return profile


def _read_profile_text(entry: Traversable | Path, source: str) -> str:
    """Read a profile TOML as text, converting a read fault into ProfileError.

    Not part of `_load_toml`'s CONVERSION_FAULTS funnel and not reachable by
    widening it: that funnel wraps `_parse_profile`, and the decode happens in
    the *argument expression* at both of `load_profiles`' call sites, before
    `_load_toml` is entered. So a non-UTF-8 overlay escaped as a raw
    `UnicodeDecodeError` (a ValueError) while every consumer keys its fault
    handling on ProfileError — `validate`'s role loop crashed before printing
    its `--json` document instead of reporting an `adapter.profile` failure
    naming the file, and `get_profile` backs every adapter resolution, so the
    same escape reached run/sweep preflight (#473). Both-arms precedent:
    `stories.py`'s manifest read; the decode-only twin is `policy.load`.

    Takes the packaged built-ins too. They are trusted, but a corrupt package
    is a packaging bug and should say so with a typed error rather than a
    traceback. One helper covers both call shapes: an `importlib.resources`
    Traversable and a `Path` each expose ``read_text(encoding=...)``.
    """
    try:
        return entry.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ProfileError(f"profile {source}: not valid UTF-8: {e}") from e
    except OSError as e:
        # A profile that is present but cannot be read — permissions, an I/O
        # error, a dead mount. The overlay's `is_dir()` + glob rule out ABSENCE
        # and nothing else, so this escaped as a bare OSError.
        raise ProfileError(f"profile {source}: unreadable: {e}") from e


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
        # fault handling on ProfileError — `validate`'s role loop reports an
        # `adapter.profile` failure, `_require_base_skills` skips the one
        # profile, `install` prints FAIL. A bare escape crashed `validate`
        # before any document was printed.
        raise ProfileError(f"profile {source}: malformed field value: {e}") from e


# The entry-point group an out-of-tree package advertises extra profiles under —
# the companion to adapters/registry.py's `bmad_loop.adapters` group. Each entry
# point loads to a provider: a callable returning an iterable of CLIProfile (or an
# iterable directly), e.g. one built from the package's own bundled TOML. Scanned
# once per process (a third-party import failure is not transient); the resulting
# profiles are process-global (project-independent), so only the project overlay
# is re-read per load_profiles call. A broken entry point is recorded, not raised.
PROFILES_GROUP = "bmad_loop.profiles"
_EXTERNALS_LOADED = False
_EXTERNAL_PROFILES: dict[str, CLIProfile] = {}
_PROFILE_LOAD_ERRORS: dict[str, str] = {}


def _coerce_profiles(produced: object, ep_name: str) -> list[CLIProfile]:
    """A provider may return a callable's result or an iterable directly; either
    way it must yield CLIProfile instances that satisfy the same invariants a TOML
    profile does. Anything else is the package's bug — reported (per
    :func:`external_profile_errors`), never trusted into the map.

    The :func:`_validate_profile` call is the point of this function: without it a
    Python provider is the one route into the profile map with no parser in front
    of it, and it could install a state ``_parse_profile`` would refuse."""
    try:
        items = list(produced)  # pyright: ignore[reportArgumentType] — TypeError is the check
    except TypeError as exc:
        raise ProfileError(
            f"{ep_name}: profile provider must return an iterable of CLIProfile"
        ) from exc
    for item in items:
        if not isinstance(item, CLIProfile):
            raise ProfileError(
                f"{ep_name}: profile provider yielded {type(item).__name__}, not CLIProfile"
            )
        _validate_profile(item, f"entry point {ep_name}")
    return items


def _load_external_profiles() -> dict[str, CLIProfile]:
    """Import every ``bmad_loop.profiles`` entry point and collect the profiles it
    provides, first-registration-wins on a name collision. Scan-once; failures are
    recorded in ``_PROFILE_LOAD_ERRORS`` (surfaced via
    :func:`external_profile_errors`), never raised — a broken adapter package must
    not break profile loading for everything else.

    A provider is rejected WHOLE: one invalid profile in the returned batch drops
    the batch, because ``_coerce_profiles`` raises before any of them is recorded.
    Deliberate — a provider is one package's declaration, and half-installing it
    would leave an operator with a profile set no error message accounts for.

    Entry points are visited in (name, distribution) order, so which provider wins
    a name collision is a property of the packages rather than of ``sys.path``
    ordering. The distribution is part of the key because the name alone is not a
    total order — ``entry_points(group=...)`` does not dedup across distributions,
    and ``sorted`` being stable would resolve a same-name tie back into discovery
    order (the adapter scan sorts on the same key, for the same reason)."""
    global _EXTERNALS_LOADED
    if _EXTERNALS_LOADED:
        return _EXTERNAL_PROFILES
    _EXTERNALS_LOADED = True
    try:
        eps = sorted(
            importlib.metadata.entry_points(group=PROFILES_GROUP),
            key=lambda e: (e.name, getattr(e.dist, "name", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics path, never crash loading
        _PROFILE_LOAD_ERRORS["<entry-point scan>"] = f"{type(exc).__name__}: {exc}"
        return _EXTERNAL_PROFILES
    for ep in eps:
        try:
            provider = ep.load()
            produced = provider() if callable(provider) else provider
            for profile in _coerce_profiles(produced, ep.name):
                _EXTERNAL_PROFILES.setdefault(profile.name, profile)
        except Exception as exc:  # noqa: BLE001 — one bad package must not hide the rest
            record_load_error(_PROFILE_LOAD_ERRORS, ep, exc)
    return _EXTERNAL_PROFILES


def external_profile_errors() -> dict[str, str]:
    """Entry-point name -> failure reason(s) for every external profile provider
    that failed to load this process (empty when all loaded). For diagnostics
    surfaces.

    One value may carry MORE than one reason, ``"; "``-joined: two distributions
    may advertise the same entry-point name, and each of their failures is kept
    (see :func:`~.entrypoints.record_load_error`). Each reason is labelled with
    its distribution whenever one is resolvable.

    Performs the scan rather than assuming a neighbouring call already did. The
    only other trigger is :func:`load_profiles`, which ``validate`` reaches through
    ``get_profile`` — inside the block that a ``PolicyError`` aborts. Reading a map
    nothing had populated would report a broken profile package as absent for a
    reason having nothing to do with that package, exactly when an operator is
    already looking at a broken config. Scan-once still holds."""
    _load_external_profiles()
    return dict(_PROFILE_LOAD_ERRORS)


def load_profiles(project: Path | None = None) -> dict[str, CLIProfile]:
    """Packaged built-ins, overlaid by ``bmad_loop.profiles`` entry-point
    profiles, overlaid by <project>/.bmad-loop/profiles/*.toml.

    Precedence is packaged < entry-point < project: a co-installed adapter package
    extends (or overrides) the bundled set, and a project TOML always wins."""
    profiles: dict[str, CLIProfile] = {}
    packaged = resources.files("bmad_loop.data").joinpath("profiles")
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".toml"):
            profile = _load_toml(_read_profile_text(entry, entry.name), entry.name)
            # Stamped HERE and nowhere else: this loop is the only code that knows
            # a profile came out of the package rather than off a project's disk.
            profiles[profile.name] = replace(profile, packaged=True)
    profiles.update(_load_external_profiles())
    if project is not None:
        user_dir = project / USER_PROFILES_REL
        if user_dir.is_dir():
            for path in sorted(user_dir.glob("*.toml")):
                profile = _load_toml(_read_profile_text(path, str(path)), str(path))
                profiles[profile.name] = profile
    return profiles


def get_profile(name: str, project: Path | None = None) -> CLIProfile:
    profiles = load_profiles(project)
    profile = profiles.get(ALIASES.get(name, name))
    if profile is None:
        raise ProfileError(f"unknown CLI profile: {name!r} (available: {sorted(profiles)})")
    return profile
