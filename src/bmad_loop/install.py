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
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

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
# `bmad-loop init` lays down. The inner dev primitive `bmad-dev-auto` is upstream
# (not bundled here): the orchestrator drives it as an already-installed skill.
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

# Upstream skills the orchestrator invokes but does NOT bundle in the wheel — the
# BMad Method (bmm) module installs them. Each must exist in every active CLI skill
# tree and carry its marker files (a half-installed or pre-automation skill is
# caught by the `bmad-loop validate` preflight). `{skill: (marker-rel-path, ...)}`.
#   - bmad-dev-auto: the inner dev primitive — always required, and never
#     substitutable. Markers pin BOTH a step file (catches a truncated copy) AND
#     customize.toml, the layer/handoff config step-04 resolves review_layers from
#     (BMAD-METHOD #2535/#2550): a pre-July bmm install predating it would let
#     every dev run's step-04 fail.
#   - the two review hunters v6.10.0 ships. These are only the FALLBACK review
#     requirement, used when the installed skill's shape can't be read: normally the
#     reviewers are derived per tree from bmad-dev-auto itself
#     (resolve_review_layers), because which skills the review step invokes is a
#     property of that skill version, not of a catalog pinned in here (#260).
# bmad-review-verification-gap is not in the fallback set: no tagged BMAD-METHOD
# release ships it standalone (on current sources it is a thin forwarder to the
# merged bmad-review), so demanding it of every project made `validate`
# unsatisfiable on real installs. A project whose review layers DO name it still has
# it required — by derivation from its own config, not by this list.
DEV_BASE_SKILLS = {
    "bmad-dev-auto": ("step-04-review.md", "customize.toml"),
    "bmad-review-adversarial-general": (),
    "bmad-review-edge-case-hunter": (),
}
# The merged lens-based reviewer (BMAD-METHOD core-streamline). Current sources make
# step-04 layer-driven off customize.toml's [[workflow.review_layers]]: four layers
# (blind-hunter, edge-case-hunter, verification-gap — each invoking bmad-review with
# one lens — plus intent-alignment, an inline prompt invoking no skill at all), so
# bmad-review alone satisfies every layer that needs a skill. Named here for the
# fallback path only; the derived path sees it named in the layers themselves.
MERGED_REVIEW_SKILL = "bmad-review"
# The DEV_BASE_SKILLS entries MERGED_REVIEW_SKILL subsumes. bmad-dev-auto (the dev
# primitive) is NOT here — the merged reviewer never substitutes for it.
_REVIEW_LAYER_SKILLS = frozenset(
    {"bmad-review-adversarial-general", "bmad-review-edge-case-hunter"}
)
# Every non-bundled skill that might need copying into an isolated worktree: the
# preflight set above plus the merged reviewer and the pre-consolidation standalone
# verification-gap forwarder (carried so a hand-installed forwarder still resolves,
# never validated). provision_worktree skips skills the main repo lacks, so this
# copy-if-present superset is safe in both directions.
BASE_SKILLS = {**DEV_BASE_SKILLS, "bmad-review-verification-gap": (), MERGED_REVIEW_SKILL: ()}

DEV_PRIMITIVE_SKILL = "bmad-dev-auto"
# How bmad-dev-auto names a skill it hands a review off to, in both shapes:
# "Invoke the `bmad-review` skill with only the `adversarial` lens" (a
# customize.toml review layer) and "Invoke the `bmad-review-edge-case-hunter`
# skill on this diff" (pre-consolidation step-04). Deliberately narrow —
# backticked, `bmad-` prefixed — because a false match here is a false FAIL,
# the exact failure mode #260 was.
_INVOKE_SKILL_RE = re.compile(r"[Ii]nvoke the `(bmad-[a-z0-9-]+)` skill")
# Any backticked bmad-* token in a layer's instruction. What this matches and
# _INVOKE_SKILL_RE does not is a skill reference we cannot confirm is an
# invocation — upstream itself writes "use the `bmad-code-review` skill"
# elsewhere, so the narrow phrasing is a convention, not a contract. Such a
# reference is reported as a WARNING and never hard-required: an override may
# legitimately mention a skill it does not invoke, and blocking on that would
# rebuild #260's false FAIL. Lens names (`adversarial`) lack the prefix, so the
# shipped layers add nothing here.
_SKILL_REF_RE = re.compile(r"`(bmad-[a-z0-9-]+)`")
# Where project-level customization of an upstream skill lives. Both the
# preflight (which reads the overrides) and worktree provisioning (which seeds
# them, so an isolated run resolves the same layers) derive their paths from
# this one constant, or they drift.
CUSTOMIZE_DIR = Path("_bmad") / "custom"
# Overrides of the skill's shipped customize.toml, in precedence order (later
# wins), per customize.toml's own header. The `.user.toml` layer is personal and
# gitignored by the upstream installer.
_CUSTOMIZE_OVERRIDES = (
    CUSTOMIZE_DIR / f"{DEV_PRIMITIVE_SKILL}.toml",
    CUSTOMIZE_DIR / f"{DEV_PRIMITIVE_SKILL}.user.toml",
)


class ReviewResolution(NamedTuple):
    """Which skills the installed bmad-dev-auto's review step actually invokes.

    ``source`` is the file it was read from (for the finding's detail).
    ``required`` maps each invoked skill to the review-layer ids invoking it —
    empty ids for the pre-consolidation shape, which has no layers. ``advisory``
    is the same shape for references we cannot confirm will run (see
    :data:`_SKILL_REF_RE` and ``when``-gated layers): missing ones warn, never
    fail. ``active_layers`` names every layer with a non-empty ``instruction`` —
    all of them disabled is a run that HALTs. ``unreadable`` names override files
    that failed to parse.
    """

    source: str
    required: dict[str, tuple[str, ...]]
    advisory: dict[str, tuple[str, ...]]
    layer_driven: bool
    active_layers: tuple[str, ...]
    unreadable: tuple[str, ...]

    def skills(self) -> tuple[str, ...]:
        """Every skill this review step may invoke, required or advisory."""
        return tuple(dict.fromkeys((*self.required, *self.advisory)))


def _read_toml(path: Path) -> dict[str, Any] | None:
    """Parse a TOML file, or None if it is absent/unreadable/malformed."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _layers_of(data: Any) -> list[Any]:
    """The ``[[workflow.review_layers]]`` array, or [] for any other shape.

    Every hop is type-guarded independently: `workflow` is only a table by
    convention, and a syntactically valid `workflow = "..."` would otherwise
    raise AttributeError straight out of the preflight — crashing validate, run,
    resume and sweep on a file whose only sin is being misconfigured.
    """
    workflow = data.get("workflow") if isinstance(data, dict) else None
    layers = workflow.get("review_layers") if isinstance(workflow, dict) else None
    return list(layers) if isinstance(layers, list) else []


# BMAD's own merge keys for arrays of tables, in detection precedence order.
_KEYED_MERGE_FIELDS = ("code", "id")


def _keyed_merge_field(items: list[Any]) -> str | None:
    """The key an array of tables merges on, or None to append.

    Port of `_detect_keyed_merge_field` in BMAD-METHOD
    `src/scripts/resolve_customization.py`. EVERY item — base and override
    combined — must be a table carrying the same key, and `code` is checked
    before `id`; a mixed or key-less array falls through to append. Guessing
    differently here means the preflight requires a different skill set than the
    resolver the run actually uses.
    """
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for candidate in _KEYED_MERGE_FIELDS:
        if all(item.get(candidate) is not None for item in items):
            return candidate
    return None


def _merge_layer_arrays(base: list[Any], override: list[Any]) -> list[Any]:
    """Merge two arrays of tables the way BMAD's resolver does.

    Port of `_merge_arrays`/`_merge_by_key` in BMAD-METHOD
    `src/scripts/resolve_customization.py`: keyed merge when the combined array
    opts into one (matching keys replace in place, new keys append), plain
    concatenation otherwise.
    """
    key = _keyed_merge_field(base + override)
    if key is None:
        return base + override
    result: list[Any] = []
    index_by_key: dict[Any, int] = {}
    for item in base:
        if not isinstance(item, dict):
            continue
        if item.get(key) is not None:
            index_by_key[item[key]] = len(result)
        result.append(dict(item))
    for item in override:
        if not isinstance(item, dict):
            result.append(item)
            continue
        item_key = item.get(key)
        if item_key is not None and item_key in index_by_key:
            result[index_by_key[item_key]] = dict(item)
        else:
            if item_key is not None:
                index_by_key[item_key] = len(result)
            result.append(dict(item))
    return result


def _merged_review_layers(project: Path, tree: str) -> tuple[list[Any], tuple[str, ...]] | None:
    """The skill's shipped review layers with project overrides applied.

    Returns ``(layers, unreadable_override_paths)``, or None when the skill's OWN
    customize.toml is absent or unparseable — the one case that genuinely means
    "shape unknown", so the caller falls back to the static catalog.

    An unparseable OVERRIDE is not that case: BMAD's resolver warns and treats it
    as empty, still resolving every other layer. Matching that keeps the preflight
    agreeing with the run; the broken file is reported separately as a warning.
    """
    data = _read_toml(project / tree / DEV_PRIMITIVE_SKILL / "customize.toml")
    if data is None:
        return None
    layers = _layers_of(data)
    unreadable: list[str] = []
    for rel in _CUSTOMIZE_OVERRIDES:
        override = project / rel
        if not override.is_file():
            continue
        extra = _read_toml(override)
        if extra is None:
            unreadable.append(rel.as_posix())
            continue
        layers = _merge_layer_arrays(layers, _layers_of(extra))
    return layers, tuple(unreadable)


def _layer_id(layer: dict[str, Any], index: int) -> str:
    """A layer's id for reporting — positional when it declares none."""
    lid = layer.get("id")
    return lid if isinstance(lid, str) and lid else f"#{index + 1}"


def resolve_review_layers(project: Path, tree: str) -> ReviewResolution | None:
    """Read the review skills this project will really invoke, or None if unknown.

    Post-consolidation bmad-dev-auto is layer-driven: each
    ``[[workflow.review_layers]]`` entry carries its whole execution recipe, and a
    layer that runs a skill names it inline (an empty ``instruction`` disables the
    layer, and a layer may legitimately name no skill at all — `intent-alignment`
    is a self-contained prompt). Pre-consolidation, ``step-04-review.md`` names its
    reviewers directly instead.

    Reading whichever is installed keeps the preflight honest as upstream moves:
    a project whose configured layers invoke a skill it does not have is otherwise
    green here and broken on every dev run (#260). None means the shape could not
    be determined, so the caller falls back to the static catalog.

    A ``when``-gated layer contributes ADVISORY requirements only: step-04 skips
    every layer whose condition does not hold, and that condition is evaluated by
    the model in run context — undecidable here, so hard-requiring its skill would
    be a false FAIL.
    """
    merged = _merged_review_layers(project, tree)
    if merged is None:
        return None
    layers, unreadable = merged
    if layers:
        required: dict[str, list[str]] = {}
        advisory: dict[str, list[str]] = {}
        active: list[str] = []
        for index, layer in enumerate(layers):
            if not isinstance(layer, dict):
                continue
            instruction = layer.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                continue
            layer_id = _layer_id(layer, index)
            active.append(layer_id)
            when = layer.get("when")
            gated = isinstance(when, str) and bool(when.strip())
            invoked = _INVOKE_SKILL_RE.findall(instruction)
            # A gated layer's invocations are advisory; so is any bmad-* token we
            # can't tie to an invocation phrasing, in either kind of layer.
            hard = [] if gated else invoked
            soft = [s for s in _SKILL_REF_RE.findall(instruction) if gated or s not in invoked]
            for bucket, skills in ((required, hard), (advisory, soft)):
                for skill in skills:
                    ids = bucket.setdefault(skill, [])
                    if layer_id not in ids:
                        ids.append(layer_id)
        return ReviewResolution(
            "customize.toml",
            {s: tuple(i) for s, i in required.items()},
            # never warn about a skill another layer already hard-requires
            {s: tuple(i) for s, i in advisory.items() if s not in required},
            True,
            tuple(active),
            unreadable,
        )
    try:
        step04 = (project / tree / DEV_PRIMITIVE_SKILL / "step-04-review.md").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return None
    # step-04 is a whole prose document rather than a self-contained execution
    # recipe, so only the invocation phrasing counts here — a backticked mention
    # anywhere in it is not evidence of a handoff.
    named = dict.fromkeys(_INVOKE_SKILL_RE.findall(step04), ())
    if not named:
        return None
    return ReviewResolution("step-04-review.md", dict(named), {}, False, (), unreadable)


# Stories mode (folder+id dispatch, BMAD-METHOD #2549) needs a *newer* bmad-dev-auto
# than sprint mode: one whose step-01 routes a spec-folder + story-id invocation.
# File existence (missing_base_skills) can't tell the two skill versions apart, so
# a content probe confirms the merged dispatch protocol is present. This literal is
# stable prose in the merged step-01 ("this is a **folder+id dispatch**").
STORIES_PROBE_SKILL = "bmad-dev-auto"
STORIES_PROBE_FILE = "step-01-clarify-and-route.md"
STORIES_PROBE_TEXT = "folder+id dispatch"


def missing_stories_support(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for stories mode's stricter bmad-dev-auto requirement.

    Sprint mode drives any bmad-dev-auto; stories mode needs the folder+id
    dispatch flow, which older skill versions lack. For each active CLI skill
    tree, confirm ``bmad-dev-auto/step-01-clarify-and-route.md`` exists and
    carries the dispatch-protocol marker. Returns one problem :class:`Finding`
    per tree lacking it (empty = OK). Callers gate this on stories mode only —
    sprint-mode runs must not require the newer skill.

    The two failures are separate check ids because they are separate conditions
    with separate remediations: ``-missing`` is a half install (reinstall the
    module), ``-stale`` is an install that is simply too old (update it)."""
    problems: list[Finding] = []
    for tree in dict.fromkeys(trees):
        probe = project / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE
        detail = {"tree": tree, "skill": STORIES_PROBE_SKILL, "file": STORIES_PROBE_FILE}
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
                    f"{tree}/{STORIES_PROBE_SKILL}/{STORIES_PROBE_FILE} not found — stories "
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
                    f"{tree}/{STORIES_PROBE_SKILL} lacks folder+id dispatch (no "
                    f"{STORIES_PROBE_TEXT!r} in {STORIES_PROBE_FILE}) — stories mode needs a "
                    f"newer bmad-dev-auto; update the bmm module",
                    {**detail, "marker": STORIES_PROBE_TEXT},
                )
            )
    return problems


def missing_base_skills(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for the upstream skills the orchestrator drives but doesn't bundle.

    The dev primitive (bmad-dev-auto) and the review layers its step-04 invokes
    inline are installed by the BMad Method module, not by `bmad-loop init`. Each
    must exist in every active CLI skill tree and carry its marker files. Returns
    one :class:`Finding` per missing/incomplete skill; empty list means OK. Run as
    a preflight so a missing skill fails loudly with remediation instead of
    stalling as an `Unknown command` until the run times out.

    Not every finding is fatal: review layers that are conditional, ambiguously
    phrased, or configured by an unparseable override come back as ``warning``
    (see :func:`_review_findings`). **Callers must branch on severity** — treating
    a non-empty return as failure turns every advisory into a blocked run.

    The review skills are read from the installed bmad-dev-auto itself
    (:func:`resolve_review_layers`) so the preflight requires what this project
    will really invoke: a tree whose configured layers call the merged
    ``bmad-review`` needs that skill and not the standalone hunters, and a tree on
    the pre-consolidation step-04 needs the two hunters it names. When the shape
    can't be read we fall back to the static catalog, with a present
    ``bmad-review`` satisfying the hunters. Everything is per tree, since a project
    can have a post-consolidation `.claude` tree and a pre-merge `.agents` one side
    by side.

    ``skills.base-incomplete`` carries ``missing_markers`` as a list — the message
    joins it with ", " for the human line, which a consumer would otherwise have to
    split back apart on a separator the message is free to change.
    """
    problems: list[Finding] = []
    for tree in dict.fromkeys(trees):
        skill_dir = project / tree / DEV_PRIMITIVE_SKILL
        markers = DEV_BASE_SKILLS[DEV_PRIMITIVE_SKILL]
        if not (skill_dir / "SKILL.md").is_file():
            problems.append(
                Finding(
                    "skills.base-missing",
                    "problem",
                    f"{tree}/{DEV_PRIMITIVE_SKILL} not found — the orchestrator drives this "
                    f"upstream dev primitive directly; it ships with the bmm module "
                    f"(BMAD-METHOD >= 6.10.0); install or update bmm in this project",
                    {"tree": tree, "skill": DEV_PRIMITIVE_SKILL},
                )
            )
        else:
            absent = [m for m in markers if not (skill_dir / m).is_file()]
            if absent:
                problems.append(
                    Finding(
                        "skills.base-incomplete",
                        "problem",
                        f"{tree}/{DEV_PRIMITIVE_SKILL} is incomplete (missing "
                        f"{', '.join(absent)}) — reinstall it from the bmm module",
                        {"tree": tree, "skill": DEV_PRIMITIVE_SKILL, "missing_markers": absent},
                    )
                )
        problems.extend(_review_findings(project, tree))
    return problems


def _where_clause(layer_ids: Sequence[str]) -> str:
    """How a finding's message names the layers that reach for a skill."""
    if len(layer_ids) > 1:
        return f"review layers {', '.join(layer_ids)} invoke"
    if layer_ids:
        return f"review layer {layer_ids[0]} invokes"
    return "review step invokes"


def _review_findings(project: Path, tree: str) -> list[Finding]:
    """Findings for the review skills this tree's bmad-dev-auto invokes.

    Problems block; warnings never do. A skill is only a problem when the
    installed config says this run WILL invoke it — anything conditional or
    ambiguous warns instead, because a false FAIL here is #260 all over again.
    Callers must therefore honour severity rather than treating any finding as
    fatal.
    """
    resolved = resolve_review_layers(project, tree)
    if resolved is None:
        # Unknown shape: keep the long-standing static requirement, which both real
        # topologies satisfy — the two hunters, or the merged reviewer instead.
        if (project / tree / MERGED_REVIEW_SKILL / "SKILL.md").is_file():
            return []
        return [
            Finding(
                "skills.base-missing",
                "problem",
                f"{tree}/{skill} not found — this review layer ships with the bmm module "
                f"(BMAD-METHOD >= 6.10.0), as does the consolidated {MERGED_REVIEW_SKILL} "
                f"skill that supersedes it in newer releases; install or update bmm in "
                f"this project",
                {"tree": tree, "skill": skill},
            )
            for skill in sorted(_REVIEW_LAYER_SKILLS)
            if not (project / tree / skill / "SKILL.md").is_file()
        ]
    findings: list[Finding] = []
    for rel in resolved.unreadable:
        findings.append(
            Finding(
                "skills.customize-unreadable",
                "warning",
                f"{rel} could not be parsed as TOML — the run's resolver skips a broken "
                f"override layer, so this project's review layers resolve without it; "
                f"fix the file or remove it",
                {"tree": tree, "file": rel},
            )
        )
    if resolved.layer_driven and not resolved.active_layers:
        findings.append(
            Finding(
                "skills.review-layers-empty",
                "problem",
                f"{tree}/{DEV_PRIMITIVE_SKILL} has no enabled review layer (every "
                f"`instruction` is empty) — every dev run would HALT blocked with "
                f"'no active review layers'; re-enable a layer in "
                f"{_CUSTOMIZE_OVERRIDES[0].as_posix()}",
                {"tree": tree, "source": resolved.source},
            )
        )
    for skill, layer_ids in sorted(resolved.required.items()):
        if (project / tree / skill / "SKILL.md").is_file():
            continue
        findings.append(
            Finding(
                "skills.review-layer-missing",
                "problem",
                f"{tree}/{skill} not found — {tree}/{DEV_PRIMITIVE_SKILL}'s "
                f"{_where_clause(layer_ids)} it ({resolved.source}), so every dev run's "
                f"review would fail; install or update the bmm module so this project's "
                f"review layers resolve",
                {
                    "tree": tree,
                    "skill": skill,
                    "layers": list(layer_ids),
                    "source": resolved.source,
                },
            )
        )
    for skill, layer_ids in sorted(resolved.advisory.items()):
        if (project / tree / skill / "SKILL.md").is_file():
            continue
        findings.append(
            Finding(
                "skills.review-layer-unresolved",
                "warning",
                f"{tree}/{skill} not found — {tree}/{DEV_PRIMITIVE_SKILL}'s "
                f"{_where_clause(layer_ids)} it conditionally, or names it in prose this "
                f"check cannot confirm is a handoff ({resolved.source}); install it if "
                f"that layer is meant to run",
                {
                    "tree": tree,
                    "skill": skill,
                    "layers": list(layer_ids),
                    "source": resolved.source,
                },
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


def _copy_traversable(src, dst: Path, *, skip_existing: bool = False) -> bool:
    """Recursively copy a packaged resource tree to a filesystem path.

    Walks via the Traversable API (.iterdir/.read_bytes) rather than resolving a
    filesystem path, so it works even when the package is zip-imported.

    ``skip_existing`` makes the copy no-clobber at FILE granularity: an existing
    destination file is left untouched and its siblings are still copied — even
    when it stands where the source has a directory (that subtree is skipped
    whole rather than mkdir'd over the file). It is
    opt-in because the `--force-skills` path (`install_into`) rmtree's the
    destination precisely to overwrite it, and a guard baked into this helper
    would silently regress that. Only the worktree-seed caller passes it.

    Returns whether anything was actually written, so a caller seeding into an
    existing directory can tell a partial seed (something landed) from a total
    no-op (every child was already present). Every other call site ignores it.
    """
    if src.is_dir():
        if skip_existing and dst.exists() and not dst.is_dir():
            # a FILE sits where the source has a directory: mkdir would raise
            # FileExistsError. Under the no-clobber contract the file wins and
            # the whole subtree is skipped, like any existing destination.
            return False
        # creating a missing directory IS a write: an empty dir seeded into an
        # existing tree must count, or the entry is misreported as a total no-op.
        copied = not dst.exists()
        dst.mkdir(parents=True, exist_ok=True)
        # `any(...)` would short-circuit and skip the remaining children.
        for child in src.iterdir():
            if _copy_traversable(child, dst / child.name, skip_existing=skip_existing):
                copied = True
        return copied
    if skip_existing and dst.exists():
        return False
    if isinstance(src, Path):
        # real filesystem source (worktree seeds, non-zip package data): copy2
        # preserves the mode so a seeded vendor/bin/* keeps +x (issue #126)
        shutil.copy2(src, dst)
    else:
        # zip-imported Traversable exposes no stat: content-only copy
        dst.write_bytes(src.read_bytes())
    return True


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


def _warn_if_policy_tracked(project: Path) -> None:
    """One-time migration hint: a .gitignore entry does not untrack an
    already-committed policy.toml, so repos initialized before the file was
    gitignored keep sharing it (and this machine's [mux] backend choice) until
    the dev runs `git rm --cached` once. Best-effort — not a repo, or no git,
    means nothing to warn about."""
    try:
        tracked = (
            subprocess.run(  # fixed argv, no shell
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
        for line in (".bmad-loop/runs/", ".bmad-loop/cache/", ".bmad-loop/policy.toml")
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


def __getattr__(name: str):
    # `provision_worktree` now lives in `worktree_flow` (issue #244 F-9a): the
    # runtime control loop must not import the installer. It is re-exported here
    # lazily — a module-level `from .worktree_flow import provision_worktree` would
    # form an import cycle (worktree_flow imports installer helpers from this
    # module), so resolve it on first attribute access, once both modules exist.
    if name == "provision_worktree":
        from .worktree_flow import provision_worktree

        return provision_worktree
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
