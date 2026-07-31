"""`bmad-loop init`: make a target project orchestratable.

- copies the hook relay script to <project>/.bmad-loop/bmad_loop_hook.py
- idempotently merges hook registrations into each selected CLI's hook config
  (dialect + native->canonical event map come from the CLI profile)
- installs the bundled bmad-loop-* skills into each selected CLI's skill tree
  (.claude/skills for claude, .agents/skills for codex/gemini/copilot)
- writes .bmad-loop/policy.toml from the template (if missing)
- gitignores generated dirs: .bmad-loop/runs/ (per-run state) and
  .bmad-loop/cache/ (engine plugins' rebuildable caches, e.g. the Unity Library)

Every dialect registers the same relay script under the CLI's native event
names while passing the canonical event name as the script argument, so the
orchestrator's signal watcher is CLI-agnostic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path, PurePosixPath

from .adapters.profile import ALIASES, CLIProfile, ProfileError, load_profiles
from .checks import Finding
from .policy import POLICY_TEMPLATE
from .process_host import get_process_host

HOOK_SCRIPT_REL = ".bmad-loop/bmad_loop_hook.py"
# Markers for bmad-loop-managed hook commands. RELAY_MARKER is shared by
# merge_hooks' dedup and validate/probe detection (via relay_registered) so init
# and the preflight can never disagree about whether the relay is installed. It
# matches the relay script name specifically: a hook command whose path merely
# contains "bmad_loop" can't read as a registration — or suppress one.
RELAY_MARKER = "bmad_loop_hook"
# The probe-adapter capture hook participates in merge_hooks' dedup only (a
# probe re-merge must stay idempotent) and never counts as a relay
# registration. Disjoint from RELAY_MARKER: "bmad_loop_probe_hook" does not
# contain the substring "bmad_loop_hook".
PROBE_MARKER = "bmad_loop_probe_hook"
# Pre-rename marker: the old relay/probe hooks lived under .automator/ and carried
# `bmad_auto` in their command. `bmad-loop init` strips them on upgrade so a project
# renamed from bmad-auto isn't left double-signalling. Underscore form, so it never
# matches the hyphenated upstream `bmad-dev-auto` skill.
LEGACY_HOOK_MARKER = "bmad_auto"
GEMINI_HOOK_TIMEOUT_MS = 60_000
COPILOT_HOOK_TIMEOUT_SEC = 60
ANTIGRAVITY_HOOK_TIMEOUT_SEC = 60  # agy hook timeouts are seconds (default 30)
# agy's .agents/hooks.json keys by hook NAME at the top level (not a "hooks"
# wrapper); bmad-loop registers all its handlers under this single group.
ANTIGRAVITY_HOOK_GROUP = "bmad-loop"

# The bmad-loop-* skills bundled in the wheel (bmad_loop/data/skills/) that
# `bmad-loop init` lays down. The inner dev primitive (`bmad-build-auto`, formerly
# `bmad-dev-auto`) is upstream (not bundled here): the orchestrator drives it as an
# already-installed skill.
MODULE_SKILLS = (
    "bmad-loop-resolve",
    "bmad-loop-sweep",
    "bmad-loop-setup",
)

# Pre-rename skill dirs (bmad-auto-*). `bmad-loop init` removes them from each CLI
# skill tree on upgrade so a renamed project isn't left with both the old and new
# forks side by side. Guarded on a SKILL.md inside, so only a real skill dir we own
# is ever deleted.
LEGACY_MODULE_SKILLS = (
    "bmad-auto-resolve",
    "bmad-auto-sweep",
    "bmad-auto-setup",
)

# The inner dev primitive, in both upstream eras. BMAD-METHOD PR #2651 (shipped in
# bmad-method 6.10.1-next.33) renamed `bmad-dev-auto` -> `bmad-build-auto` and left a
# forwarding SHIM behind under the old name: a lone SKILL.md whose customization
# migration gate is INTERACTIVE, so an unattended session that dispatches to it can
# HALT having written nothing to disk — no spec, no result artifact, nothing the
# post-session verification can read. The orchestrator therefore never accepts the
# shim: it resolves the primitive on disk (resolve_dev_primitive) and fails the
# preflight when only the shim is installed.
#
# The shim carries no step files and no customize.toml, so DEV_PRIMITIVE_MARKERS —
# which already pinned "a real, complete install" — doubles as the shim detector.
# Markers pin BOTH a step file (catches a truncated copy) AND customize.toml, the
# layer/handoff config step-04 resolves review_layers from (BMAD-METHOD #2535/#2550):
# a pre-July bmm install predating it would let every dev run's step-04 fail.
DEV_PRIMITIVE_NEW = "bmad-build-auto"
DEV_PRIMITIVE_LEGACY = "bmad-dev-auto"
DEV_PRIMITIVE_MARKERS = ("step-04-review.md", "customize.toml")

# BMAD's config/tool dir at the project root. Everything the renderer reads hangs
# off it, and the renderer takes the project root as an argument and hard-fails when
# `<project-root>/_bmad` is absent — there is no walk-up — so an isolated worktree
# must carry its own (see _seed_bmad_tree).
BMAD_DIR = "_bmad"

# Since BMAD-METHOD PR #2601 a skill's SKILL.md can be a renderer *stub* that shells
# out (via uv) to this project-local script to compose the real prompt. When the
# script is absent the session HALTs without writing anything, so this is a preflight
# FAIL: absence is SUFFICIENT for the HALT, which is the only question a gate asks.
# That the probe is not sufficient for success (uv on PATH is never checked, nor is
# the config's contents — see #407) argues against trusting a green, not against
# blocking on a red.
RENDERER_SCRIPT_REL = f"{BMAD_DIR}/scripts/render_skill.py"
RENDERER_SCRIPT_MARKER = "render_skill.py"

# The bottom layer of the renderer's four-layer central config, and the only
# REQUIRED one (`config_utils.load_central_config` passes required=True; the
# .user/custom layers above it are all optional). Absent, the renderer raises
# before it composes anything, so the stub's `uv run` exits with `HALT:` and the
# session Stops having written no spec — the same result-less failure
# RENDERER_SCRIPT_REL guards, one file further in, and blocking for the same
# reason. Project-global, not per tree.
CENTRAL_CONFIG_REL = f"{BMAD_DIR}/config.toml"

# The one `provision_worktree` skipped-seed entry that is NOT informational: it says
# the worktree's renderer support came up SHORT, not that a seed was a no-op. Shared
# with the engine, which escalates on it — a magic string on either side would let the
# two drift silently apart, and the failure mode of that drift is a run that dispatches
# into guaranteed result-less Stops. See _bmad_scripts_seed_incomplete.
BMAD_SCRIPTS_SEED_REL = f"{BMAD_DIR}/scripts"

# Top-level _bmad/ entries never seeded into a worktree. render/ is the renderer's
# published output: it is regenerated on skill entry, and every snapshot dir name is
# keyed on a hash of the project root's absolute path, so seeding the main
# checkout's copy would carry ITS paths into the worktree and make every parallel
# session race on one shared tree.
BMAD_SEED_EXCLUDES = ("render",)

# Upstream per-skill customization overrides live here, named after the skill. The
# rename does NOT migrate them, so a project upgraded to bmad-build-auto silently
# stops applying its bmad-dev-auto.toml — validate warns (v0.9.0's orchestrator has
# no customize read site of its own, so this is purely an operator heads-up).
CUSTOMIZE_DIR_REL = f"{BMAD_DIR}/custom"

# The three review hunters the dev primitive's step-04 invokes inline on EVERY dev
# run (and on each follow-up review re-invocation) — always required, no longer
# gated on a separate review session. bmad-review-verification-gap is the newest
# layer (BMAD-METHOD #2550): a target project missing it makes the verification-gap
# review layer fail on every run. No markers: existence is the whole check.
REVIEW_HUNTER_SKILLS: dict[str, tuple[str, ...]] = {
    "bmad-review-adversarial-general": (),
    "bmad-review-edge-case-hunter": (),
    "bmad-review-verification-gap": (),
}

# Upstream skills the orchestrator invokes but does NOT bundle in the wheel — the
# BMad Method (bmm) module installs them. Each must exist in every active CLI skill
# tree and carry its marker files (a half-installed or pre-automation skill is
# caught by the `bmad-loop validate` preflight). `{skill: (marker-rel-path, ...)}`.
# The dev-primitive entry is keyed on the LEGACY name because this map is also the
# "lay down a pre-rename install" catalog; missing_base_skills does not walk it for
# the primitive — it resolves the installed name per tree first.
DEV_BASE_SKILLS = {DEV_PRIMITIVE_LEGACY: DEV_PRIMITIVE_MARKERS, **REVIEW_HUNTER_SKILLS}
# Every non-bundled skill that might need copying into an isolated worktree.
# bmad-review is the merged lens-based reviewer (BMAD-METHOD core-streamline):
# on new bmm installs the three hunter IDs above are thin forwarders to it, so a
# worktree must carry the real skill for those forwards to resolve. It is NOT in
# DEV_BASE_SKILLS (preflight) so pre-merge bmm installs — which have the three
# real hunters and no bmad-review — keep validating; provision_worktree skips
# skills the main repo lacks, so copy-if-present is safe in both directions.
# Both primitive eras are listed: a worktree must carry whichever one the main
# checkout has, and copy-if-present makes naming both free. Adding the new name
# here is what keeps isolation working across the rename.
BASE_SKILLS = {DEV_PRIMITIVE_NEW: DEV_PRIMITIVE_MARKERS, **DEV_BASE_SKILLS, "bmad-review": ()}

# Stories mode (folder+id dispatch, BMAD-METHOD #2549) needs a *newer* dev primitive
# than sprint mode: one whose step-01 routes a spec-folder + story-id invocation.
# File existence (missing_base_skills) can't tell the two skill versions apart, so
# a content probe confirms the merged dispatch protocol is present. This literal is
# stable prose in the merged step-01 ("this is a **folder+id dispatch**").
# STORIES_PROBE_SKILL names the FALLBACK era only — the probe runs against the skill
# resolve_dev_primitive picked for that tree, so a bmad-build-auto install is probed
# under its own name.
STORIES_PROBE_SKILL = DEV_PRIMITIVE_LEGACY
STORIES_PROBE_FILE = "step-01-clarify-and-route.md"
STORIES_PROBE_TEXT = "folder+id dispatch"


def resolve_dev_primitive(project: Path, tree: str) -> str | None:
    """The dev-primitive skill name to drive in ``tree``, or None when none is usable.

    Prefers :data:`DEV_PRIMITIVE_NEW`; falls back to :data:`DEV_PRIMITIVE_LEGACY`
    only when that install is marker-complete, which is exactly what the post-rename
    forwarding shim is not (see the constants block). None means "fail the preflight"
    — never "drive the old name and hope".

    The new name needs only its SKILL.md to *resolve*: completeness is reported by
    :func:`missing_base_skills` against the resolved dir. Requiring markers here
    instead would make a truncated bmad-build-auto silently resolve to a legacy
    install (or to the shim's failure message), hiding the real problem.
    """
    if (project / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file():
        return DEV_PRIMITIVE_NEW
    legacy = project / tree / DEV_PRIMITIVE_LEGACY
    if (legacy / "SKILL.md").is_file() and all(
        (legacy / marker).is_file() for marker in DEV_PRIMITIVE_MARKERS
    ):
        return DEV_PRIMITIVE_LEGACY
    return None


def _is_dev_primitive_shim(project: Path, tree: str) -> bool:
    """True when ``tree`` holds a legacy-named skill that is only a forwarding shim
    (SKILL.md present, at least one marker absent). Selects the failure *message*
    in :func:`missing_base_skills`; it is never a resolution input."""
    legacy = project / tree / DEV_PRIMITIVE_LEGACY
    if not (legacy / "SKILL.md").is_file():
        return False
    return any(not (legacy / marker).is_file() for marker in DEV_PRIMITIVE_MARKERS)


def _is_renderer_stub(skill_dir: Path) -> bool:
    """True when ``skill_dir``'s SKILL.md is a renderer stub (BMAD-METHOD #2601) —
    i.e. it shells out to ``render_skill.py`` rather than carrying the prompt inline.

    Keyed on content, never on the installed skill name, so it answers the same for
    both eras. An unreadable or binary SKILL.md cannot be *shown* to be a stub, so it
    reads False: :func:`missing_base_skills`' marker checks have already spoken about
    that tree's health, and inventing a renderer FAIL out of a read fault would blame
    the wrong file."""
    try:
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return RENDERER_SCRIPT_MARKER in skill_md


def dev_primitive_or_default(project: Path, tree: str | None) -> str:
    """Total form of :func:`resolve_dev_primitive` for prompt builders.

    A prompt string always has to name *something*, and the preflight has already
    refused the unresolvable cases before any session is spawned — so an
    unresolvable tree (and a None tree, which is what an adapter with no profile
    reports) falls back to the legacy name rather than raising into prompt
    construction."""
    if tree is None:
        return DEV_PRIMITIVE_LEGACY
    return resolve_dev_primitive(project, tree) or DEV_PRIMITIVE_LEGACY


def missing_stories_support(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for stories mode's stricter dev-primitive requirement.

    Sprint mode drives any dev primitive; stories mode needs the folder+id
    dispatch flow, which older skill versions lack. For each active CLI skill
    tree, confirm ``<resolved-primitive>/step-01-clarify-and-route.md`` exists and
    carries the dispatch-protocol marker. Returns one problem :class:`Finding`
    per tree lacking it (empty = OK). Callers gate this on stories mode only —
    sprint-mode runs must not require the newer skill.

    The two failures are separate check ids because they are separate conditions
    with separate remediations: ``-missing`` is a half install (reinstall the
    module), ``-stale`` is an install that is simply too old (update it)."""
    problems: list[Finding] = []
    for tree in dict.fromkeys(trees):
        skill = dev_primitive_or_default(project, tree)
        probe = project / tree / skill / STORIES_PROBE_FILE
        detail = {"tree": tree, "skill": skill, "file": STORIES_PROBE_FILE}
        try:
            text = probe.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # OSError = the probe file is absent/unreadable; UnicodeDecodeError = it
            # exists but is binary/non-UTF-8 (a corrupted skill tree). Either way the
            # dispatch-protocol marker can't be confirmed, so report a problem rather
            # than letting the decode error escape and crash the whole preflight.
            problems.append(
                Finding(
                    "skills.stories-dispatch-missing",
                    "problem",
                    f"{tree}/{skill}/{STORIES_PROBE_FILE} not found — stories "
                    f"mode needs folder+id dispatch; update the BMad Method (bmm) module",
                    detail,
                )
            )
            continue
        if STORIES_PROBE_TEXT not in text:
            problems.append(
                Finding(
                    "skills.stories-dispatch-stale",
                    "problem",
                    f"{tree}/{skill} lacks folder+id dispatch (no "
                    f"{STORIES_PROBE_TEXT!r} in {STORIES_PROBE_FILE}) — stories mode needs a "
                    f"newer {skill}; update the bmm module",
                    {**detail, "marker": STORIES_PROBE_TEXT},
                )
            )
    return problems


def missing_base_skills(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for the upstream skills the orchestrator drives but doesn't bundle.

    The dev primitive (bmad-build-auto, or a complete pre-rename bmad-dev-auto) and
    the three review hunters it invokes inline — adversarial-general,
    edge-case-hunter, and verification-gap — are installed by the BMad Method
    module, not by `bmad-loop init`. Each must exist in every active CLI skill tree
    and carry its marker files. Returns one problem :class:`Finding` per
    missing/incomplete skill; empty list means OK. Run as a preflight so a missing
    skill fails loudly with remediation instead of stalling as an `Unknown command`
    until the run times out.

    The primitive is resolved per tree (:func:`resolve_dev_primitive`) before any
    marker check, so the markers are asserted against the skill this run would
    actually drive. That splits the failures three ways:

    - ``skills.base-incomplete`` — one resolved, but it is truncated.
    - ``skills.base-shim`` — nothing resolved, yet a legacy-named SKILL.md is there.
    - ``skills.base-missing`` — nothing at all under either name.

    A truncated *legacy* install is byte-for-byte the same shape as the shim (old
    SKILL.md, absent markers), so it lands on ``base-shim`` rather than
    ``base-incomplete``; nothing on disk can tell those two apart, so the message
    names both causes and the single remediation they share. What the ids DO
    separate is what a consumer can act on differently: resolved-but-truncated
    (reinstall that skill) vs nothing-usable-resolved (update the module).

    ``skills.base-incomplete`` carries ``missing_markers`` as a list — the message
    joins it with ", " for the human line, which a consumer would otherwise have to
    split back apart on a separator the message is free to change.

    A resolved *renderer stub* (BMAD-METHOD #2601) is probed two files further in,
    because a stub whose script or required config layer is absent is the same
    result-less HALT as the shim above and must fail the same gate:

    - ``skills.dev-renderer`` — the stub is there, ``_bmad/scripts/render_skill.py``
      is not. Per tree, since the primitive resolves per tree.
    - ``skills.dev-renderer-config`` — a stub resolved *somewhere* and the project has
      no ``_bmad/config.toml``. Once per project (the central config is
      project-global), and gated on a stub having resolved so the check stays
      era-agnostic: a pre-#2601 inline SKILL.md never reads the file, so its absence
      is not a finding to make about that project.

    Both are emitted independently of the marker check above them — different files,
    different remediations, and a wholly-absent ``_bmad/`` legitimately earns both
    lines beside a truncated skill.
    """
    problems: list[Finding] = []
    resolved_stub = False
    for tree in dict.fromkeys(trees):
        resolved = resolve_dev_primitive(project, tree)
        if resolved is None and _is_dev_primitive_shim(project, tree):
            legacy_dir = project / tree / DEV_PRIMITIVE_LEGACY
            absent = [m for m in DEV_PRIMITIVE_MARKERS if not (legacy_dir / m).is_file()]
            problems.append(
                Finding(
                    "skills.base-shim",
                    "problem",
                    f"{tree}/{DEV_PRIMITIVE_LEGACY} is unusable (missing "
                    f"{', '.join(absent)}) and {DEV_PRIMITIVE_NEW} is not installed — "
                    f"most likely the forwarding shim the BMad Method's rename left "
                    f"behind, otherwise a truncated install; update the bmm module. The "
                    f"shim's migration prompt is interactive and would HALT an unattended "
                    f"session without writing anything to disk",
                    {
                        "tree": tree,
                        "skill": DEV_PRIMITIVE_LEGACY,
                        "expected": DEV_PRIMITIVE_NEW,
                        "missing_markers": absent,
                    },
                )
            )
        elif resolved is None:
            problems.append(
                Finding(
                    "skills.base-missing",
                    "problem",
                    f"{tree}/{DEV_PRIMITIVE_NEW} not found — install the BMad Method (bmm) "
                    f"module (the orchestrator drives this upstream skill directly; older "
                    f"installs name it {DEV_PRIMITIVE_LEGACY})",
                    {"tree": tree, "skill": DEV_PRIMITIVE_NEW},
                )
            )
        else:
            skill_dir = project / tree / resolved
            absent = [m for m in DEV_PRIMITIVE_MARKERS if not (skill_dir / m).is_file()]
            if absent:
                problems.append(
                    Finding(
                        "skills.base-incomplete",
                        "problem",
                        f"{tree}/{resolved} is incomplete (missing {', '.join(absent)}) — "
                        f"reinstall it from the bmm module",
                        {"tree": tree, "skill": resolved, "missing_markers": absent},
                    )
                )
            if _is_renderer_stub(skill_dir):
                resolved_stub = True
                if not (project / RENDERER_SCRIPT_REL).is_file():
                    problems.append(
                        Finding(
                            "skills.dev-renderer",
                            "problem",
                            f"{tree}/{resolved}/SKILL.md renders via {RENDERER_SCRIPT_MARKER} "
                            f"but {RENDERER_SCRIPT_REL} is missing — the session would HALT "
                            f"without writing a spec; reinstall the BMad Method (bmm) module",
                            {"tree": tree, "skill": resolved, "script": RENDERER_SCRIPT_REL},
                        )
                    )
        for skill, markers in REVIEW_HUNTER_SKILLS.items():
            skill_dir = project / tree / skill
            if not (skill_dir / "SKILL.md").is_file():
                problems.append(
                    Finding(
                        "skills.base-missing",
                        "problem",
                        f"{tree}/{skill} not found — install the BMad Method (bmm) module "
                        f"(the orchestrator drives this upstream skill directly)",
                        {"tree": tree, "skill": skill},
                    )
                )
                continue
            absent = [m for m in markers if not (skill_dir / m).is_file()]
            if absent:
                problems.append(
                    Finding(
                        "skills.base-incomplete",
                        "problem",
                        f"{tree}/{skill} is incomplete (missing {', '.join(absent)}) — "
                        f"reinstall it from the bmm module",
                        {"tree": tree, "skill": skill, "missing_markers": absent},
                    )
                )
    if resolved_stub and not (project / CENTRAL_CONFIG_REL).is_file():
        problems.append(
            Finding(
                "skills.dev-renderer-config",
                "problem",
                f"the dev primitive renders via {RENDERER_SCRIPT_MARKER} but "
                f"{CENTRAL_CONFIG_REL} is missing — the renderer requires that layer and "
                f"would HALT without writing a spec; reinstall the BMad Method (bmm) module",
                {"config": CENTRAL_CONFIG_REL},
            )
        )
    return problems


def dev_primitive_warnings(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Advisory findings about a resolved dev primitive — validate-only, never a gate.

    One condition, and it is genuinely survivable — which is what keeps it out of
    :func:`missing_base_skills`:

    - ``skills.customize-legacy``: the tree resolved to the NEW name while a
      customization override still sits under the OLD one with no counterpart, i.e.
      the rename silently orphaned it. Emitted once per project (the override files
      are project-global, not per tree). The session still runs; it just runs
      unstyled, so naming it is an operator heads-up rather than a gate.

    The two renderer conditions this used to carry (``skills.dev-renderer``,
    ``skills.dev-renderer-config``) moved into :func:`missing_base_skills` as
    problems: each is a deterministic HALT-without-writing-a-spec, the same failure
    the shim check already blocks on, so previewing a run that cannot possibly
    produce one was the wrong service to offer.

    Returns [] when nothing resolves — :func:`missing_base_skills` owns that story.
    """
    findings: list[Finding] = []
    resolved_new = any(
        resolve_dev_primitive(project, tree) == DEV_PRIMITIVE_NEW for tree in dict.fromkeys(trees)
    )
    if resolved_new:
        orphaned = [
            f"{CUSTOMIZE_DIR_REL}/{DEV_PRIMITIVE_LEGACY}{suffix}"
            for suffix in (".toml", ".user.toml")
            if (project / CUSTOMIZE_DIR_REL / f"{DEV_PRIMITIVE_LEGACY}{suffix}").is_file()
            and not (project / CUSTOMIZE_DIR_REL / f"{DEV_PRIMITIVE_NEW}{suffix}").is_file()
        ]
        if orphaned:
            findings.append(
                Finding(
                    "skills.customize-legacy",
                    "warning",
                    f"{', '.join(orphaned)} no longer applies — the dev primitive is now "
                    f"{DEV_PRIMITIVE_NEW}; rename the override file(s) to match",
                    {"files": orphaned, "skill": DEV_PRIMITIVE_NEW},
                )
            )
    return findings


def _hook_command(project: Path, profile: CLIProfile, canonical_event: str) -> str:
    host = get_process_host()
    interp = host.hook_interpreter()
    if profile.hooks.dialect == "claude-settings-json":
        return f'{interp} "$CLAUDE_PROJECT_DIR"/{HOOK_SCRIPT_REL} {canonical_event}'
    # Codex/Gemini expose no $CLAUDE_PROJECT_DIR equivalent to hook commands;
    # bake the absolute path at init time.
    return f"{interp} {host.shell_quote(str(project / HOOK_SCRIPT_REL))} {canonical_event}"


def _hook_entry(dialect: str, command: str) -> dict:
    handler: dict = {"type": "command", "command": command}
    if dialect == "gemini-settings-json":
        handler["timeout"] = GEMINI_HOOK_TIMEOUT_MS  # Gemini timeouts are milliseconds
        return {"matcher": "", "hooks": [handler]}
    if dialect == "copilot-settings-json":
        handler["timeoutSec"] = COPILOT_HOOK_TIMEOUT_SEC  # Copilot timeouts are seconds
        return handler  # Copilot stores the handler directly in the event list
    if dialect == "antigravity-hooks-json":
        handler["timeout"] = ANTIGRAVITY_HOOK_TIMEOUT_SEC  # agy timeouts are seconds
        # agy's Stop event value is a flat list of handler objects — the handler
        # sits directly in the event list, with no matcher/hooks wrapper (unlike
        # gemini's grouped shape).
        return handler
    # claude-settings-json and codex-hooks-json share the schema
    return {"hooks": [handler]}


def hook_event_container(config: dict, dialect: str) -> dict:
    """The `native event -> handlers` map inside a parsed hook config.

    Most dialects nest it under "hooks". agy instead keys the file by hook GROUP
    name at the top level, so our relay lives under ANTIGRAVITY_HOOK_GROUP —
    reading "hooks" there yields {} and reports a correctly-installed relay as
    unregistered (issue #159). Every reader must go through this, or it drifts.
    """
    if dialect == "antigravity-hooks-json":
        container = config.get(ANTIGRAVITY_HOOK_GROUP, {})
    else:
        container = config.get("hooks", {})
    return container if isinstance(container, dict) else {}


def _relay_in_handlers(handlers) -> bool:
    """True if any handler in a native-event list carries the relay command."""
    return RELAY_MARKER in json.dumps(handlers)


def _managed_hook_in_handlers(handlers) -> bool:
    """merge_hooks' dedup: a relay OR probe-capture command is already present."""
    dumped = json.dumps(handlers)
    return RELAY_MARKER in dumped or PROBE_MARKER in dumped


def relay_registered(config: dict, dialect: str, events: Iterable[str]) -> bool:
    """True if the bmad-loop relay is registered for any of `events`."""
    container = hook_event_container(config, dialect)
    return any(_relay_in_handlers(container.get(event, [])) for event in events)


def merge_hooks(config: dict, registrations: dict[str, str], dialect: str) -> tuple[dict, bool]:
    """Add relay registrations (native event -> command) to a hook config dict."""
    changed = False
    if dialect == "antigravity-hooks-json":
        # agy keys .agents/hooks.json by hook NAME at the top level (no "hooks"
        # wrapper); register every handler under one ANTIGRAVITY_HOOK_GROUP group.
        # Other named groups (user/plugin hooks) sit alongside and are preserved.
        group = config.setdefault(ANTIGRAVITY_HOOK_GROUP, {})
        if not isinstance(group, dict):
            raise ProfileError(
                f"{ANTIGRAVITY_HOOK_GROUP!r} in the hooks file is not a table; "
                "fix or remove it before registering the Stop hook"
            )
        for native_event, command in registrations.items():
            handlers = group.setdefault(native_event, [])
            if not isinstance(handlers, list):
                raise ProfileError(
                    f"hook event {native_event!r} under {ANTIGRAVITY_HOOK_GROUP!r} "
                    "is not a list; fix the hooks file before re-running init"
                )
            if not _managed_hook_in_handlers(handlers):
                handlers.append(_hook_entry(dialect, command))
                changed = True
        return config, changed
    if dialect == "copilot-settings-json":
        config.setdefault("version", 1)  # Copilot hook configs are versioned
    hooks = config.setdefault("hooks", {})
    for native_event, command in registrations.items():
        matchers = hooks.setdefault(native_event, [])
        # claude/codex/gemini nest handlers under "hooks"; copilot stores the
        # handler dict directly in the event list — the serialized scan covers
        # both shapes so a re-run stays idempotent for every dialect.
        if not _managed_hook_in_handlers(matchers):
            matchers.append(_hook_entry(dialect, command))
            changed = True
    return config, changed


def strip_legacy_hooks(config: dict) -> tuple[dict, int]:
    """Remove hook handlers carrying the pre-rename `bmad_auto` marker.

    Mirrors merge_hooks' dialect shapes: copilot stores the handler dict directly
    in the event list; claude/codex/gemini nest handlers under ``entry["hooks"]``.
    Emptied matcher entries — and event lists left empty — are dropped so a
    re-registered `bmad_loop` hook doesn't share space with a dead `bmad_auto` one.
    Returns (config, removed_count). The hyphenated upstream `bmad-dev-auto` skill
    never matches the underscore marker.
    """
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return config, 0
    removed = 0
    for native_event in list(hooks):
        matchers = hooks.get(native_event)
        if not isinstance(matchers, list):
            continue
        kept: list = []
        for entry in matchers:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            # copilot: the handler dict is the entry itself. Guard the command
            # against a non-string (present-but-null) value so a malformed
            # hand-edited config can't crash the strip with a TypeError.
            if isinstance(cmd := entry.get("command"), str) and LEGACY_HOOK_MARKER in cmd:
                removed += 1
                continue
            # claude/codex/gemini: handlers nest under "hooks"
            nested = entry.get("hooks")
            if isinstance(nested, list):
                pruned = [
                    h
                    for h in nested
                    if not (
                        isinstance(h, dict)
                        and isinstance(cmd := h.get("command"), str)
                        and LEGACY_HOOK_MARKER in cmd
                    )
                ]
                if len(pruned) != len(nested):
                    removed += len(nested) - len(pruned)
                    if not pruned:
                        continue  # emptied matcher entry -> drop it
                    entry["hooks"] = pruned
            kept.append(entry)
        if kept:
            hooks[native_event] = kept
        else:
            del hooks[native_event]  # emptied event -> drop it
    return config, removed


def _register_hooks(project: Path, profile: CLIProfile) -> int:
    if profile.hookless:
        print(f"  no hooks needed ({profile.name}): HTTP/SSE transport")
        return 0
    config_path = project / profile.hooks.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"FAIL: {config_path} is not valid JSON; fix it and re-run init")
            return 1
    registrations = {
        native: _hook_command(project, profile, canonical)
        for native, canonical in profile.hooks.events.items()
    }
    config, removed = strip_legacy_hooks(config)
    config, changed = merge_hooks(config, registrations, profile.hooks.dialect)
    if removed:
        print(f"  removed {removed} legacy bmad-auto hook(s) ({profile.name})")
    if changed or removed:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        if changed:
            print(f"  hooks registered ({profile.name}): {config_path}")
    elif not removed:
        print(f"  hooks already registered ({profile.name})")
    return 0


def _copy_traversable(src, dst: Path) -> None:
    """Recursively copy a packaged resource tree to a filesystem path.

    Walks via the Traversable API (.iterdir/.read_bytes) rather than resolving a
    filesystem path, so it works even when the package is zip-imported.
    """
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_traversable(child, dst / child.name)
    elif isinstance(src, Path):
        # real filesystem source (worktree seeds, non-zip package data): copy2
        # preserves the mode so a seeded vendor/bin/* keeps +x (issue #126)
        shutil.copy2(src, dst)
    else:
        # zip-imported Traversable exposes no stat: content-only copy
        dst.write_bytes(src.read_bytes())


def _worktree_local_exclude(worktree: Path, patterns: Sequence[str]) -> None:
    """Add anchored ignore patterns to the worktree's local git exclude so the
    provisioned tool files are never staged by the unit's `git add -A`. Uses
    git's standard local-only exclude (never committed or pushed); it does not
    affect already-tracked files. Best-effort — skipped if git can't be queried.
    """
    # Callers pass POSIX-slash patterns (glob rels via as_posix; config strings as
    # authored); git's exclude is POSIX-slash on every platform, so nothing to fix here.
    try:
        common = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (worktree / common_dir).resolve()
    exclude = common_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    present = set(existing.splitlines())
    new = [p for p in patterns if p not in present]
    if not new:
        return
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    exclude.write_text(prefix + "\n".join(new) + "\n", encoding="utf-8")


def _is_under_bmad(rel: str) -> bool:
    """True when a user-authored seed rel names ``_bmad`` or something inside it.
    Normalized so ``_bmad``, ``_bmad/``, ``./_bmad/custom`` and a Windows-authored
    ``_bmad\\custom`` all answer the same."""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    return bool(parts) and parts[0] == BMAD_DIR


def _seed_bmad_tree(worktree: Path, repo_root: Path) -> list[str]:
    """Merge-copy the main repo's ``_bmad/`` config surface into a worktree.

    Sessions run with the worktree as their cwd, and the renderer-era dev
    primitive (BMAD-METHOD #2601) shells out to ``_bmad/scripts/render_skill.py``
    with a project root that must contain a ``_bmad/`` directory: the renderer
    hard-fails when it does not, and it does **not** walk up. Projects commonly
    gitignore ``_bmad/`` (this repo does) and a worktree checks out tracked files
    only, so under ``isolation = "worktree"`` the checkout has none — the stub
    then HALTs before the workflow's own HALT protocol is loaded and every story
    becomes a result-less Stop.

    Whole-directory per-FILE merge, never a curated file list: ``render_skill.py``
    bare-imports its sibling ``config_utils`` off ``sys.path[0]``, so
    ``_bmad/scripts/`` is one multi-file unit and a partial seed raises a bare
    ``ModuleNotFoundError`` above the renderer's own try/except — losing even the
    ``HALT: <error>`` contract line. The central config is a four-layer stack
    (``config.toml`` plus the usually-gitignored ``config.user.toml`` and
    ``custom/`` layers) for the same reason.

    Copy-when-absent, so a checkout that commits its ``_bmad/`` keeps every
    tracked file untouched and only the missing (gitignored) layers are filled in
    — nothing new is ever merged back. :data:`BMAD_SEED_EXCLUDES` entries are
    skipped *before* descending. The resolve-and-contain guard is the seed_files
    loop's, so neither a symlink nor a ``..`` component can read outside the repo
    or write outside the worktree.

    Returns the rels to shield from the unit's ``git add -A``: the single
    ``_bmad`` root when the worktree had none (everything under it is ours), or
    the individual seeded files when merging into a checkout that already had one
    — shield exactly what we wrote.
    """
    src_root = repo_root / BMAD_DIR
    if not src_root.is_dir():
        return []
    dst_root = worktree / BMAD_DIR
    had_bmad = dst_root.is_dir()
    seeded: list[str] = []
    for top in sorted(src_root.iterdir()):
        if top.name in BMAD_SEED_EXCLUDES:
            continue
        for src in [top] if top.is_file() else sorted(top.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if not src.resolve().is_relative_to(repo_root) or not dst.resolve().is_relative_to(
                worktree
            ):
                continue
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_traversable(src, dst)
            seeded.append((Path(BMAD_DIR) / rel).as_posix())
    if not seeded:
        return []
    return [BMAD_DIR] if not had_bmad else seeded


def _bmad_scripts_seed_incomplete(worktree: Path, repo_root: Path) -> bool:
    """True when the repo carries the renderer but the worktree's ``_bmad/scripts/``
    is missing at least one file the repo's has.

    That seed is the only thing between a renderer-era install and a result-less
    Stop, and it can come up short *without failing*. The realistic trigger is a
    **symlinked** ``_bmad/`` or ``_bmad/scripts/`` — how a shared BMAD install is
    wired. Measured: a symlinked dir IS walked when it is the rglob root (which it
    is in :func:`_seed_bmad_tree`), so every file under it reaches the
    resolve-and-contain guard and, resolving outside ``repo_root``, is dropped
    one by one. The destination lands empty while provisioning otherwise reports
    success — a partial ``_bmad/scripts/`` is worse than none, because the bare
    ``config_utils`` import fails above the renderer's try/except and even the
    ``HALT:`` line is lost.

    Reported through :func:`provision_worktree`'s existing skipped-seed return
    channel, as the :data:`BMAD_SCRIPTS_SEED_REL` entry, rather than raising —
    provisioning has no failure vocabulary of its own (every containment violation
    is a bare ``continue``), and the decision this drives is escalate-vs-defer plus
    notify and pause, which belongs to the caller that owns the state machine and
    the journal. The *engine* reads the entry back and escalates: this is not a
    degraded provision the run can carry, it is an environment fault identical for
    every story, so dispatching would burn the whole backlog on result-less Stops.
    """
    if not (repo_root / RENDERER_SCRIPT_REL).is_file():
        return False
    scripts = repo_root / BMAD_DIR / "scripts"
    dst_scripts = worktree / BMAD_DIR / "scripts"
    return any(
        src.is_file() and not (dst_scripts / src.relative_to(scripts)).is_file()
        for src in scripts.rglob("*")
    )


def _copy_skills(project: Path, trees: Sequence[str], force: bool) -> bool:
    """Install the bundled bmad-loop-* skills into each project skill tree.

    A skill directory that already exists is skipped unless ``force`` (so the
    BMAD installer's copy or local edits are never clobbered silently). Returns
    True if any skill was skipped because it already existed.
    """
    skills_root = resources.files("bmad_loop.data").joinpath("skills")
    skipped_any = False
    for tree in trees:
        tree_dir = project / tree
        installed: list[str] = []
        skipped: list[str] = []
        for skill in MODULE_SKILLS:
            dst = tree_dir / skill
            if dst.exists() and not force:
                skipped.append(skill)
                continue
            if dst.exists():
                shutil.rmtree(dst)
            _copy_traversable(skills_root.joinpath(skill), dst)
            installed.append(skill)
        parts: list[str] = []
        if installed:
            parts.append(f"installed {', '.join(installed)}")
        if skipped:
            parts.append(f"skipped {', '.join(skipped)} (exist)")
            skipped_any = True
        print(f"  skills -> {tree}/: {'; '.join(parts) if parts else 'nothing to do'}")
    return skipped_any


def _remove_legacy_skills(project: Path, trees: Sequence[str]) -> None:
    """Delete the pre-rename bmad-auto-* skill dirs from each project skill tree.

    Guarded on a SKILL.md inside, so an unrelated same-named folder is never touched.
    Idempotent (a missing dir is a no-op); prints one line per removal.
    """
    for tree in dict.fromkeys(trees):
        for skill in LEGACY_MODULE_SKILLS:
            dst = project / tree / skill
            if dst.is_dir() and (dst / "SKILL.md").is_file():
                shutil.rmtree(dst)
                print(f"  removed legacy skill: {tree}/{skill}")


def provision_worktree(
    worktree: Path,
    profiles: Sequence[CLIProfile],
    repo_root: Path,
    seed_files: Sequence[str] = (),
    seed_globs: Sequence[str] = (),
) -> list[str]:
    """Make a freshly-created git worktree a self-sufficient bmad-loop project.

    A worktree checks out tracked files only, but the skill trees (.claude/skills,
    .agents/skills), the hook config, and the project's gitignored MCP/CLI configs
    are absent from the checkout. Without them the bundled bmad-loop-* skills are missing,
    the Stop-signal hook never fires, and isolated sessions can't reach their MCP
    server. Lay the bundled skills + signal hook into the worktree for the active
    CLI profiles, and copy the `seed_files` configs in from the main repo. The
    upstream skills the orchestrator drives (BASE_SKILLS: bmad-dev-auto + the review
    hunters) are not bundled in the wheel, so they are copied from the MAIN REPO's
    installed tree instead. Quiet (no stdout) — unlike `install_into` this runs
    inside the engine loop under a TUI. No-op when there's nothing to do.

    seed_globs are project-relative glob patterns (e.g. ".claude/skills/*") expanded
    against the main repo; every match is copied into the worktree under the same
    relative path, copy-when-absent like seed_files. A game-engine plugin uses these
    to pull its MCP-generated skill tree (gitignored, so absent from the checkout)
    into a per_worktree Editor's checkout.

    Kept safe against the unit's eventual `git add -A` commit:
    - skills + seed files are copied only when ABSENT, so a project that commits its
      own skill tree (e.g. .agents/) or config keeps it untouched (no diff merged back);
    - the hook points at the MAIN repo's already-installed relay via an absolute
      path (the relay locates the run dir from $BMAD_LOOP_RUN_DIR, not its own
      location), so nothing is written into the worktree's .bmad-loop/;
    - everything we wrote is added to the worktree's local git exclude.
    Skill trees, the per-CLI hook config, and the seeded configs all live in dirs
    projects gitignore — but the exclude shields them even when a project doesn't.

    seed_files are copied BEFORE the hook step so a seeded settings file that is
    also a hook config_path (.claude/settings.json, .gemini/settings.json) keeps its
    real content and just gets the Stop hook merged in, rather than being created empty.

    The `_bmad/` config surface is merge-seeded from the MAIN REPO too (see
    _seed_bmad_tree): the renderer-era dev primitive is handed the worktree as its
    project root and hard-fails when that root has no _bmad/, so a project that
    gitignores it (most do) would HALT every session. `_bmad/render/` is never
    seeded and is additionally excluded whenever the worktree has a _bmad/ at all,
    so the renderer's in-session rewrite of it can't be swept into a story commit.

    Returns the `seed_files` entries that existed in the repo but were skipped
    because the destination already existed — copy-when-absent turned them into
    no-ops. The caller journals them: a user-authored `worktree_seed` entry that
    silently copies nothing reads as applied configuration and is not. The same
    channel also reports an INCOMPLETE `_bmad/scripts/` seed (see
    _bmad_scripts_seed_incomplete), which is likewise silent otherwise.
    """
    if not profiles and not seed_files and not seed_globs and not (repo_root / BMAD_DIR).is_dir():
        return []
    worktree = worktree.resolve()
    repo_root = repo_root.resolve()
    relay = repo_root / HOOK_SCRIPT_REL
    skills_root = resources.files("bmad_loop.data").joinpath("skills")

    # project gitignored MCP/CLI configs: copy from the main repo when absent.
    # Resolve-and-contain guards against an `..`/absolute entry escaping either tree.
    seeded: list[str] = []
    # Entries that named a real source but whose destination already exists, so
    # copy-when-absent made them a no-op. Reported to the caller (this function is
    # quiet by contract — it runs under a TUI) because for a DIRECTORY entry the
    # no-op is silent and total: a worktree checks out tracked files, so a seed dir
    # with any tracked child always exists and the whole entry is skipped, including
    # the children that are absent and would clobber nothing. Glob-expanded matches
    # are deliberately not reported: a plugin's glob is expected to hit paths the
    # checkout already carries, so that skip is routine rather than a misconfiguration.
    skipped: list[str] = []
    for rel in seed_files:
        src = (repo_root / rel).resolve()
        dst = (worktree / rel).resolve()
        if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
            continue
        if not src.exists():
            continue
        if dst.exists():
            skipped.append(str(rel))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_traversable(src, dst)
        seeded.append(rel)

    # glob-seeded trees (e.g. an engine plugin's MCP skill dirs): expand each
    # pattern against the main repo and copy matches in, same contain guard +
    # copy-when-absent semantics. rel is taken from the unresolved match so the
    # worktree path mirrors the repo layout; resolve only guards containment.
    for pattern in seed_globs:
        for match in sorted(repo_root.glob(pattern)):
            rel = match.relative_to(repo_root)
            src = match.resolve()
            dst = (worktree / rel).resolve()
            if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
                continue
            if not src.exists() or dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_traversable(src, dst)
            # as_posix so the exclude pattern anchors on Windows too (os.sep would not)
            seeded.append(rel.as_posix())

    # The _bmad/ config surface, merged in AFTER the explicit seed loops: a
    # user-authored seed_files entry is explicit intent and wins on any collision,
    # and `had_bmad` must be read once those loops have had their say.
    seeded_bmad = _seed_bmad_tree(worktree, repo_root)
    if seeded_bmad:
        # 0.9.0 documented `worktree_seed = ["_bmad"]` as the workaround for exactly
        # this gap. That entry is now a no-op *because the merge already covered it*,
        # so reporting it would journal worktree-seed-skipped for a seed that applied.
        skipped = [rel for rel in skipped if not _is_under_bmad(rel)]
    if _bmad_scripts_seed_incomplete(worktree, repo_root):
        skipped.append(BMAD_SCRIPTS_SEED_REL)

    # bundled skills into each CLI's skill tree (deduped: codex+gemini share one);
    # never clobber a skill the checkout already carries (tracked or pre-existing).
    for tree in dict.fromkeys(p.skill_tree for p in profiles):
        tree_dir = worktree / tree
        for skill in MODULE_SKILLS:
            dst = tree_dir / skill
            if dst.exists():
                continue
            _copy_traversable(skills_root.joinpath(skill), dst)
        # The orchestrator-driven upstream skills (BASE_SKILLS) are not in the
        # wheel; copy them from the MAIN REPO's installed tree (same tree path) so
        # an isolated worktree can still resolve the dev primitive (under EITHER
        # name — BASE_SKILLS lists both eras) and the review hunters. Skip silently
        # when the main repo lacks them — the run-start preflight reports it.
        for skill in BASE_SKILLS:
            dst = tree_dir / skill
            if dst.exists():
                continue
            src = (repo_root / tree / skill).resolve()
            if not src.is_relative_to(repo_root) or not src.is_dir():
                continue
            _copy_traversable(src, dst)

    # per-CLI signal-hook registration, baked to the main repo's relay (absolute).
    # Hookless profiles (HTTP/SSE transport) have no config to merge.
    for profile in profiles:
        if profile.hookless:
            continue
        config_path = worktree / profile.hooks.config_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config: dict = {}
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                config = {}
        host = get_process_host()
        interp = host.hook_interpreter()
        registrations = {
            native: f"{interp} {host.shell_quote(str(relay))} {canonical}"
            for native, canonical in profile.hooks.events.items()
        }
        config, changed = merge_hooks(config, registrations, profile.hooks.dialect)
        if changed:
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    # Shield exactly the paths we wrote (skill trees + hook configs + seeded
    # configs) from the unit's `git add -A`, in case a project doesn't gitignore
    # its tool dirs.
    patterns = {f"/{p.skill_tree}" for p in profiles}
    # hookless profiles have no config_path — and their empty string would render
    # as the pattern "/", git-excluding the entire worktree.
    patterns |= {f"/{p.hooks.config_path}" for p in profiles if not p.hookless}
    patterns |= {f"/{rel}" for rel in seeded}
    patterns |= {f"/{rel}" for rel in seeded_bmad}
    if (worktree / BMAD_DIR).is_dir():
        # Shielded whether we seeded it or not: the renderer rewrites this dir
        # DURING the session, long after provisioning. Gated on the worktree
        # actually having a _bmad/ because .git/info/exclude is the COMMON git dir,
        # shared with the main checkout — a line for a dir this worktree can never
        # grow is pollution in the operator's own repo.
        patterns.add(f"/{BMAD_DIR}/render/")
    _worktree_local_exclude(worktree, sorted(patterns))
    return skipped


def _warn_if_policy_tracked(project: Path) -> None:
    """One-time migration hint: a .gitignore entry does not untrack an
    already-committed policy.toml, so repos initialized before the file was
    gitignored keep sharing it (and this machine's [mux] backend choice) until
    the dev runs `git rm --cached` once. Best-effort — not a repo, or no git,
    means nothing to warn about."""
    try:
        tracked = (
            subprocess.run(  # noqa: S603, S607 — fixed argv, no shell
                ["git", "ls-files", "--error-unmatch", ".bmad-loop/policy.toml"],
                cwd=project,
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return
    if tracked:
        print(
            "  note: .bmad-loop/policy.toml is tracked by git; run "
            "`git rm --cached .bmad-loop/policy.toml` once to stop sharing it "
            "(your local copy is kept)"
        )


def install_into(
    project: Path,
    clis: Sequence[str] = ("claude",),
    *,
    skills: bool = True,
    force_skills: bool = False,
) -> int:
    project = project.resolve()
    try:
        available = load_profiles(project)
        profiles = []
        for name in clis:
            key = ALIASES.get(name, name)
            if key not in available:
                raise ProfileError(
                    f"unknown CLI profile: {name!r} (available: {sorted(available)})"
                )
            profiles.append(available[key])
    except ProfileError as e:
        print(f"FAIL: {e}")
        return 1

    bmad_loop_dir = project / ".bmad-loop"
    bmad_loop_dir.mkdir(parents=True, exist_ok=True)

    # 1. hook relay script (shared by all CLIs)
    script_target = project / HOOK_SCRIPT_REL
    script_source = resources.files("bmad_loop.data").joinpath("bmad_loop_hook.py")
    script_target.write_text(script_source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  hook script: {script_target}")

    # 2. per-CLI hook registration
    for profile in profiles:
        if _register_hooks(project, profile) != 0:
            return 1

    # 3. bundled skills into each CLI's skill tree (deduped: codex+gemini share
    #    .agents/skills)
    skills_skipped = False
    if skills:
        trees = list(dict.fromkeys(p.skill_tree for p in profiles))
        _remove_legacy_skills(project, trees)
        skills_skipped = _copy_skills(project, trees, force_skills)

    # 4. policy template — on an upgrade from bmad-auto, carry the old policy over
    #    (its contents are unchanged by the rename) rather than resetting to default.
    policy_path = bmad_loop_dir / "policy.toml"
    legacy_policy = project / ".automator" / "policy.toml"
    if policy_path.is_file():
        print("  policy exists, leaving untouched")
    elif legacy_policy.is_file():
        policy_path.write_text(legacy_policy.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  migrated policy: {legacy_policy} -> {policy_path}")
    else:
        policy_path.write_text(POLICY_TEMPLATE, encoding="utf-8")
        print(f"  policy written: {policy_path}")

    # 5. gitignore generated/machine-local state: per-run state (.bmad-loop/runs/),
    # the game-engine plugins' rebuildable caches, e.g. the per-worktree Unity
    # Library (.bmad-loop/cache/), and the policy file itself — policy.toml is
    # per-machine-per-repo (it carries this machine's [mux] backend choice, and
    # the TUI settings editor rewrites it), so it must never travel to teammates.
    gitignore = project / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    have = set(existing.splitlines())
    to_add = [
        line
        for line in (
            ".bmad-loop/runs/",
            ".bmad-loop/cache/",
            ".bmad-loop/policy.toml",
            # the renderer's published output: regenerated on every skill entry,
            # keyed on this machine's absolute project root. Under isolation =
            # "none" (the default) this line is the only thing keeping it out of
            # story commits — worktrees get a git exclude instead.
            f"{BMAD_DIR}/render/",
        )
        if line not in have
    ]
    if to_add:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(to_add) + "\n")
        for line in to_add:
            print(f"  gitignored: {line}")
    _warn_if_policy_tracked(project)

    if skills_skipped:
        print("  some skills already present; re-run with --force-skills to overwrite")

    # 6. legacy state left in place: init never deletes the old .automator/ tree
    #    (runs/archives/profiles/plugins) or its stale .automator/* gitignore lines.
    #    Policy was carried over above; everything else is yours to keep or remove.
    if (project / ".automator").is_dir():
        print(
            "  note: legacy .automator/ left in place (runs, archives, profiles, "
            "plugins, and any stale .automator/* gitignore lines). Delete it or "
            "hand-move state once you've confirmed the migration."
        )

    print(
        "init complete. One-time setup before `bmad-loop run` — spawned "
        "sessions cannot answer first-run dialogs, and a pending dialog reads "
        "as a session timeout:"
    )
    for profile in profiles:
        if profile.first_run_note:
            print(f"  {profile.name}: {profile.first_run_note}")
    return 0
