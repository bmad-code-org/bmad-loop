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
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

from .adapters.profile import ALIASES, CLIProfile, ProfileError, load_profiles
from .checks import Finding
from .platform_util import atomic_write_bytes, file_lock
from .policy import POLICY_TEMPLATE
from .process_host import get_process_host
from .verify import GitError, git_bytes

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
GEMINI_HOOK_TIMEOUT_MS = 60_000
COPILOT_HOOK_TIMEOUT_SEC = 60
ANTIGRAVITY_HOOK_TIMEOUT_SEC = 60  # agy hook timeouts are seconds (default 30)
# agy's .agents/hooks.json keys by hook NAME at the top level (not a "hooks"
# wrapper); bmad-loop registers all its handlers under this single group.
ANTIGRAVITY_HOOK_GROUP = "bmad-loop"

# The bmad-loop-* skills bundled in the wheel (bmad_loop/data/skills/) that
# `bmad-loop init` lays down. The inner dev primitive (see PRIMITIVE_SKILL below)
# is upstream (not bundled here): the orchestrator drives it as an
# already-installed skill.
MODULE_SKILLS = (
    "bmad-loop-resolve",
    "bmad-loop-sweep",
    "bmad-loop-setup",
)

# The inner dev primitive's name, across the BMAD-METHOD rename that renamed
# `bmad-dev-auto` to `bmad-build-auto`. Post-rename bmm ships `bmad-dev-auto` only
# as a permanent one-file shim that forwards to `bmad-build-auto` — so a project's
# real primitive (the one carrying step-04-review.md/customize.toml, the one every
# dev/review session actually runs) is whichever of the two is genuinely installed.
# `resolve_primitive_skill` (below) is the one place that decides; every other
# function in this module resolves through it rather than hardcoding a name, or
# validate would drift from what a dev run really invokes (see its docstring).
PRIMITIVE_SKILL = "bmad-build-auto"
LEGACY_PRIMITIVE_SKILL = "bmad-dev-auto"
# Markers pin BOTH a step file (catches a truncated copy) AND customize.toml, the
# layer/handoff config step-04 resolves review_layers from (BMAD-METHOD
# #2535/#2550): an install predating that would let every dev run's step-04 fail.
# Identical requirement for both PRIMITIVE_SKILL and LEGACY_PRIMITIVE_SKILL — the
# rename changed the skill's name, not its shape.
PRIMITIVE_MARKERS = ("step-04-review.md", "customize.toml")


def resolve_primitive_skill(project: Path, tree: str) -> str:
    """Which skill is this project's inner dev primitive, for one skill tree.

    Prefers ``bmad-build-auto`` whenever it is installed there at all (its
    SKILL.md exists) — that is what every current bmm module ships. Falls back
    to ``bmad-dev-auto`` only when ``bmad-build-auto`` is entirely absent: an
    install that predates the rename ships ``bmad-dev-auto`` as the real
    primitive, and bmm's post-rename forwarding shim (a single SKILL.md with no
    step-04-review.md/customize.toml of its own) never creates a build-auto
    directory, so it never masks a genuine dev-auto-only install.

    Existence, not completeness, decides: a build-auto directory present but
    missing a marker (a half-applied upgrade) still counts as "installed" and is
    still returned here — the marker gap itself is reported separately by
    :func:`missing_base_skills` as ``skills.base-incomplete`` against
    ``bmad-build-auto``. Requiring full markers here instead would misdiagnose
    that half-applied upgrade as "no primitive at all", pointing the remediation
    at the wrong skill name (``bmad-dev-auto``, which in this scenario may not
    exist either).

    Every consumer in this module — the base-skills marker check, review-layer
    resolution (which must read the primitive's OWN
    step-04-review.md/customize.toml, not the shim's), the stories-dispatch
    probe, customize overrides, and worktree provisioning's copy list — resolves
    through this one function, so they can never name different skills for the
    same project.

    When NEITHER is installed at all, this still falls back to ``bmad-dev-auto``
    rather than reporting the (also absent) ``bmad-build-auto``: that keeps every
    existing consumer's remediation message pointing at the name it has always
    pointed at for a from-scratch project.
    """
    if (project / tree / PRIMITIVE_SKILL / "SKILL.md").is_file():
        return PRIMITIVE_SKILL
    return LEGACY_PRIMITIVE_SKILL


# Upstream skills the orchestrator invokes but does NOT bundle in the wheel — the
# BMad Method (bmm) module installs them. Each must exist in every active CLI skill
# tree and carry its marker files (a half-installed or pre-automation skill is
# caught by the `bmad-loop validate` preflight). `{skill: (marker-rel-path, ...)}`.
# Kept here under the LEGACY name for back-compat with call sites (including
# tests) that build a full skill topology explicitly; the live preflight below
# does NOT read this dict for the primitive — it resolves the marker requirement
# from PRIMITIVE_MARKERS against whichever skill `resolve_primitive_skill` picks.
#   - bmad-dev-auto: the inner dev primitive — always required, and never
#     substitutable. See PRIMITIVE_MARKERS for why both markers are pinned.
#   - the two review hunters v6.10.0 ships. These are only the FALLBACK review
#     requirement, used when the installed skill's shape can't be read: normally the
#     reviewers are derived per tree from the resolved primitive itself
#     (resolve_review_layers), because which skills the review step invokes is a
#     property of that skill version, not of a catalog pinned in here (#260).
# bmad-review-verification-gap is not in the fallback set: no tagged BMAD-METHOD
# release ships it standalone (on current sources it is a thin forwarder to the
# merged bmad-review), so demanding it of every project made `validate`
# unsatisfiable on real installs. A project whose review layers DO name it still has
# it required — by derivation from its own config, not by this list.
DEV_BASE_SKILLS = {
    LEGACY_PRIMITIVE_SKILL: PRIMITIVE_MARKERS,
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
# The DEV_BASE_SKILLS entries MERGED_REVIEW_SKILL subsumes. The dev primitive is
# NOT here — the merged reviewer never substitutes for it.
_REVIEW_LAYER_SKILLS = frozenset(
    {"bmad-review-adversarial-general", "bmad-review-edge-case-hunter"}
)
# Every non-bundled skill that might need copying into an isolated worktree: the
# preflight set above plus the merged reviewer and the pre-consolidation standalone
# verification-gap forwarder (carried so a hand-installed forwarder still resolves,
# never validated). provision_worktree skips skills the main repo lacks, so this
# copy-if-present superset is safe in both directions. Retained under the LEGACY
# primitive name for back-compat with existing call sites and tests that treat it
# as a fixed catalog; `base_skills_for_tree` is the per-project/tree equivalent
# that resolves the primitive dynamically, and is what worktree provisioning uses.
BASE_SKILLS = {**DEV_BASE_SKILLS, "bmad-review-verification-gap": (), MERGED_REVIEW_SKILL: ()}


def base_skills_for_tree(project: Path, tree: str) -> dict[str, tuple[str, ...]]:
    """:data:`BASE_SKILLS`, but with the dev primitive resolved for THIS project/tree.

    Same composition — the primitive plus the fallback review hunters, the merged
    reviewer, and the pre-consolidation verification-gap forwarder — except the
    primitive entry names whichever skill :func:`resolve_primitive_skill` actually
    finds installed, instead of always the legacy name. Worktree provisioning uses
    this (not the static ``BASE_SKILLS``) so an isolated run copies the SAME
    primitive the main repo's preflight validated — a project on `bmad-build-auto`
    must not have its worktree seeded with a `bmad-dev-auto` that was never there.
    """
    primitive = resolve_primitive_skill(project, tree)
    return {
        primitive: PRIMITIVE_MARKERS,
        "bmad-review-adversarial-general": (),
        "bmad-review-edge-case-hunter": (),
        "bmad-review-verification-gap": (),
        MERGED_REVIEW_SKILL: (),
    }


# How the primitive names a skill it hands a review off to, in both shapes:
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


def _customize_overrides(primitive: str) -> tuple[Path, Path]:
    """Override files for `primitive`'s shipped customize.toml, precedence order.

    Per customize.toml's own header, later wins; the `.user.toml` layer is
    personal and gitignored by the upstream installer. Named after the RESOLVED
    primitive (see :func:`resolve_primitive_skill`), so a project on
    `bmad-build-auto` reads/writes `_bmad/custom/bmad-build-auto*.toml` and an
    older project still on `bmad-dev-auto` keeps using
    `_bmad/custom/bmad-dev-auto*.toml` — the deprecated override files are still
    honored, just under whichever name is this project's actual primitive.
    """
    return (
        CUSTOMIZE_DIR / f"{primitive}.toml",
        CUSTOMIZE_DIR / f"{primitive}.user.toml",
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


def _merged_review_layers(
    project: Path, tree: str, primitive: str
) -> tuple[list[Any], tuple[str, ...]] | None:
    """`primitive`'s shipped review layers with project overrides applied.

    Returns ``(layers, unreadable_override_paths)``, or None when the skill's OWN
    customize.toml is absent or unparseable — the one case that genuinely means
    "shape unknown", so the caller falls back to the static catalog.

    An unparseable OVERRIDE is not that case: BMAD's resolver warns and treats it
    as empty, still resolving every other layer. Matching that keeps the preflight
    agreeing with the run; the broken file is reported separately as a warning.
    """
    data = _read_toml(project / tree / primitive / "customize.toml")
    if data is None:
        return None
    layers = _layers_of(data)
    unreadable: list[str] = []
    for rel in _customize_overrides(primitive):
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


def resolve_review_layers(
    project: Path, tree: str, primitive: str | None = None
) -> ReviewResolution | None:
    """Read the review skills this project will really invoke, or None if unknown.

    ``primitive`` is the skill to read this from — defaults to whatever
    :func:`resolve_primitive_skill` picks for this project/tree, so most callers
    can omit it; a caller that already resolved the primitive (e.g.
    :func:`missing_base_skills`, which needs the same value for its own marker
    check) passes it through instead of resolving it twice.

    Post-consolidation, the primitive is layer-driven: each
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
    if primitive is None:
        primitive = resolve_primitive_skill(project, tree)
    merged = _merged_review_layers(project, tree, primitive)
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
        step04 = (project / tree / primitive / "step-04-review.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # step-04 is a whole prose document rather than a self-contained execution
    # recipe, so only the invocation phrasing counts here — a backticked mention
    # anywhere in it is not evidence of a handoff.
    named = dict.fromkeys(_INVOKE_SKILL_RE.findall(step04), ())
    if not named:
        return None
    return ReviewResolution("step-04-review.md", dict(named), {}, False, (), unreadable)


# Stories mode (folder+id dispatch, BMAD-METHOD #2549) needs a *newer* primitive
# than sprint mode: one whose step-01 routes a spec-folder + story-id invocation.
# File existence (missing_base_skills) can't tell the two skill versions apart, so
# a content probe confirms the merged dispatch protocol is present. This literal is
# stable prose in the merged step-01 ("this is a **folder+id dispatch**").
#
# STORIES_PROBE_SKILL is kept as an alias of LEGACY_PRIMITIVE_SKILL for back-compat
# with call sites (including tests) that build a probe path directly; the function
# below resolves the actual skill to probe per tree via resolve_primitive_skill.
STORIES_PROBE_SKILL = LEGACY_PRIMITIVE_SKILL
STORIES_PROBE_FILE = "step-01-clarify-and-route.md"
STORIES_PROBE_TEXT = "folder+id dispatch"


def missing_stories_support(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for stories mode's stricter dev-primitive requirement.

    Sprint mode drives any resolved primitive; stories mode needs the folder+id
    dispatch flow, which older skill versions lack. For each active CLI skill
    tree, resolve the primitive (:func:`resolve_primitive_skill`) and confirm its
    ``step-01-clarify-and-route.md`` exists and carries the dispatch-protocol
    marker. Returns one problem :class:`Finding` per tree lacking it (empty = OK).
    Callers gate this on stories mode only — sprint-mode runs must not require the
    newer skill.

    The two failures are separate check ids because they are separate conditions
    with separate remediations: ``-missing`` is a half install (reinstall the
    module), ``-stale`` is an install that is simply too old (update it)."""
    problems: list[Finding] = []
    for tree in dict.fromkeys(trees):
        primitive = resolve_primitive_skill(project, tree)
        probe = project / tree / primitive / STORIES_PROBE_FILE
        detail = {"tree": tree, "skill": primitive, "file": STORIES_PROBE_FILE}
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
                    f"{tree}/{primitive}/{STORIES_PROBE_FILE} not found — stories "
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
                    f"{tree}/{primitive} lacks folder+id dispatch (no "
                    f"{STORIES_PROBE_TEXT!r} in {STORIES_PROBE_FILE}) — stories mode needs a "
                    f"newer dev primitive; update the bmm module",
                    {**detail, "marker": STORIES_PROBE_TEXT},
                )
            )
    return problems


def missing_base_skills(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for the upstream skills the orchestrator drives but doesn't bundle.

    The dev primitive (resolved per tree by :func:`resolve_primitive_skill` —
    ``bmad-build-auto`` on current bmm installs, ``bmad-dev-auto`` on installs that
    predate the rename) and the review layers its step-04 invokes inline are
    installed by the BMad Method module, not by `bmad-loop init`. Each must exist
    in every active CLI skill tree and carry its marker files. Returns one
    :class:`Finding` per missing/incomplete skill; empty list means OK. Run as a
    preflight so a missing skill fails loudly with remediation instead of stalling
    as an `Unknown command` until the run times out.

    Not every finding is fatal: review layers that are conditional, ambiguously
    phrased, or configured by an unparseable override come back as ``warning``
    (see :func:`_review_findings`). **Callers must branch on severity** — treating
    a non-empty return as failure turns every advisory into a blocked run.

    The review skills are read from the installed primitive itself
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
        primitive = resolve_primitive_skill(project, tree)
        skill_dir = project / tree / primitive
        if not (skill_dir / "SKILL.md").is_file():
            problems.append(
                Finding(
                    "skills.base-missing",
                    "problem",
                    f"{tree}/{primitive} not found — the orchestrator drives this "
                    f"upstream dev primitive directly; it ships with the bmm module "
                    f"(BMAD-METHOD >= 6.10.0); install or update bmm in this project",
                    {"tree": tree, "skill": primitive},
                )
            )
        else:
            absent = [m for m in PRIMITIVE_MARKERS if not (skill_dir / m).is_file()]
            if absent:
                problems.append(
                    Finding(
                        "skills.base-incomplete",
                        "problem",
                        f"{tree}/{primitive} is incomplete (missing "
                        f"{', '.join(absent)}) — reinstall it from the bmm module",
                        {"tree": tree, "skill": primitive, "missing_markers": absent},
                    )
                )
        problems.extend(_review_findings(project, tree, primitive))
    return problems


def _where_clause(layer_ids: Sequence[str]) -> str:
    """How a finding's message names the layers that reach for a skill."""
    if len(layer_ids) > 1:
        return f"review layers {', '.join(layer_ids)} invoke"
    if layer_ids:
        return f"review layer {layer_ids[0]} invokes"
    return "review step invokes"


def _review_findings(project: Path, tree: str, primitive: str) -> list[Finding]:
    """Findings for the review skills this tree's resolved primitive invokes.

    ``primitive`` is the skill :func:`missing_base_skills` already resolved for
    this tree (:func:`resolve_primitive_skill`) — passed through rather than
    re-resolved, so a caller and this helper can never disagree on which skill's
    step-04-review.md/customize.toml is authoritative.

    Problems block; warnings never do. A skill is only a problem when the
    installed config says this run WILL invoke it — anything conditional or
    ambiguous warns instead, because a false FAIL here is #260 all over again.
    Callers must therefore honour severity rather than treating any finding as
    fatal.
    """
    resolved = resolve_review_layers(project, tree, primitive)
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
                f"{tree}/{primitive} has no enabled review layer (every "
                f"`instruction` is empty) — every dev run would HALT blocked with "
                f"'no active review layers'; re-enable a layer in "
                f"{_customize_overrides(primitive)[0].as_posix()}",
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
                f"{tree}/{skill} not found — {tree}/{primitive}'s "
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
                f"{tree}/{skill} not found — {tree}/{primitive}'s "
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
    config, changed = merge_hooks(config, registrations, profile.hooks.dialect)
    if changed:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"  hooks registered ({profile.name}): {config_path}")
    else:
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


# The git that introduced `extensions.worktreeConfig` and `git config --worktree`,
# both of which the worktree-scoped shield is built out of (git-worktree(1)).
_WORKTREE_CONFIG_GIT = (2, 20)

# The shield's mutual exclusion, held in the repository's COMMON dir so every
# worktree of a repo contends on one file — a per-worktree gitdir would give each
# concurrent run its own lock and exclude nothing. A dedicated file, never config
# or the exclude itself, per `file_lock`'s contract: the lock rides an open fd's
# inode, so anything written through `atomic_replace` swaps that inode out from
# under later acquirers. Zero bytes, and inside `.git` — never in the working tree,
# so it is not something the operator's `git add -A` can see.
_SHIELD_LOCK_NAME = "bmad-loop-shield.lock"

# THE ALTERNATIVE ARCHITECTURE, EVALUATED AND REJECTED — recorded here the way the
# `--includes` finding below is, so a later reader or bot round does not re-propose
# it as a simplification of the whole block.
#
# Every finding in this family (an empty seed plus an activation that shadows what
# the seed failed to copy) exists because the shield SHADOWS a config key. One
# design dissolves the class outright: drop `core.excludesFile` entirely and instead
# unstage the tool paths in the orchestrator's own commit path — `git reset --
# <paths>` after the `add -A` at `verify.py:1891` and `:1932`. No shadow, so no
# seed, no `_shield_inherited_excludes`, no permanent `extensions.worktreeConfig`,
# no 2.20 floor. It is MECHANICALLY REACHABLE: those adds are the orchestrator's
# own, not an LLM's (no skill in `data/skills/` runs git).
#
# Rejected on the merits, for two reasons that are about correctness rather than
# scope:
#
# - It converts AMBIENT protection into CALL-SITE protection. An ignore binds
#   whoever runs the add; a reset covers only the call sites we remember to patch.
#   These adds run around disposable coding-CLI sessions that have shell access and
#   can stage and commit on their own initiative, so the shield has to hold for a
#   `git add -A` bmad-loop never issued. That actor is the one we cannot constrain,
#   and the reset design is the one that stops protecting against it.
# - It leaves the tool files permanently UNTRACKED-VISIBLE in the worktree. Every
#   `git status` a session runs would list the skill trees, the per-CLI hook config
#   and the seeded configs as untracked — inviting exactly the commit the shield
#   exists to prevent, from exactly that actor.
#
# The shadow is also structural, not an artifact of this implementation, so there is
# no third way to look for: gitignore(5) documents four pattern sources and the
# per-repo one is `$GIT_COMMON_DIR/info/exclude` — the COMMON dir, shared by every
# worktree. There is no per-worktree exclude file, `core.excludesFile` is singular,
# and git never concatenates the key across scopes. Checked against current sources
# at git 2.55.0, the newest release.


def _git_version_at_least(reported: str, want: tuple[int, int]) -> bool:
    """Is this `git version …` line at least `want`? Anything unreadable is NO.

    Only `major.minor` is compared, and only from a line that actually starts
    `git version` — the tail is vendor soup (`2.44.0.windows.1`,
    `2.39.5 (Apple Git-154)`) and searching the whole string for the first two
    dotted numbers would happily read a version out of a build tag.

    Refusing an unparseable answer is the point rather than a fallback. The only
    caller uses this to decide whether to make a PERMANENT repo-format change, so
    the failure it must not have is the optimistic one; a git that will not say
    what it is does not get that change made on its behalf.
    """
    match = re.match(r"git version (\d+)\.(\d+)", reported.strip())
    return match is not None and (int(match[1]), int(match[2])) >= want


def _shield_shared_config(worktree: Path, shared: str, key: str, *opts: str) -> bytes | None:
    """`key`'s raw value in the SHARED config, or None when it is definitely ABSENT.

    Raises `GitError` when git did not answer. **rc 1 is the only non-zero rc that
    means "no such key"** — the funnel `_shield_inherited_excludes` already applies to
    its own read (round 9), applied here to the three probes that were still reading
    every non-zero rc as an absence (#384, Codex round 10). Routing those to ABSENT
    degrades the `core.bare` / `core.worktree` gates OPEN, and what they guard is
    irreversible: enabling `extensions.worktreeConfig` drops the exception that
    confined those keys to the main worktree, after which a genuine `core.worktree`
    starts applying to every linked worktree.

    Measured at git 2.20.4 AND 2.55.0, identical at both ends of the supported range:

        absent key ......................... 1     malformed config file ..... 128
        missing file ....................... 1     --type=bool on a non-bool . 128
        `--file` naming a directory ........ 1     key present ................ 0
        unreadable file (EACCES, non-root) . 1     empty or valueless key ..... 0

    git-config(1) prefaces its own list with "Some exit codes are:", i.e. the list is
    not closed, and it documents a malformed file as ret=3 while both versions
    actually answer 128 — so a `{0,1}`-closed reading is wrong on git's documentation
    AND on its behavior. Hence a funnel rather than a per-rc taxonomy (round 7).

    THE RESIDUAL THIS CANNOT CLOSE, stated rather than left for another round: a
    momentarily missing or unreadable `.git/config` answers **rc 1**, byte-identical
    to "no such key" — all four rc-1 rows above are the same answer from here. git
    exposes no way to separate them, so this is a bound on the interface rather than a
    defect. Deliberately NOT narrowed with a pre-`--get` stat: that is TOCTOU, which
    shrinks the window instead of closing it, adds a syscall to every probe, and
    trades a very rare silent-wrong for a less rare noisy-degrade. Bounded in practice
    because git rewrites config atomically (lock + rename), so only a non-git tool
    truncating in place can produce it.

    Returns the value RAW and undecoded — `core.worktree` is a path — so callers test
    presence with `is not None`, never truthiness. See the call sites for why.
    """
    answer = git_bytes(worktree, "config", "--file", shared, *opts, "--get", key)
    if answer.returncode == 0:
        return answer.stdout
    if answer.returncode == 1:
        return None
    detail = os.fsdecode(answer.stderr).strip() or f"git exited {answer.returncode}"
    raise GitError(f"git could not read {key} from the shared config: {detail}")


_SHARED_REPOSITORY_OCTAL = re.compile(r"[0-7]+")


def _shared_repository_is_private(value: str) -> bool:
    """Does `core.sharedRepository = value` leave the repository private to its owner?

    git's parse, mirrored (`setup.c::git_config_perm`): the keyword `umask` and the
    false booleans are private, `group`/`all`/`world`/`everybody` and the true
    booleans are not, and an octal value is a FILEMODE with the legacy 0/1/2
    special-cased ahead of it.

    Measured at git 2.20.4 AND 2.55.0, byte-identical at both ends of the supported
    range, by the mode of a loose object git writes under `umask 077` — git's OWN
    answer, not a reading of `setup.c`:

        0 / 00 / 0000 ....... r--------  PRIVATE     0400 / 0200 ... REJECTED (128)
        0600 / 0700 / 0711 .. r--------  PRIVATE     -0600 ......... REJECTED
        +0600 ............... r--------  PRIVATE     0o600 / 0_600 . REJECTED
        01 / 1 .............. r--r-----  group       0600x ......... REJECTED
        0640 / 0660 ......... r--r-----  group
        02 / 2 / 07777 ...... r--r--r--  everybody
        0666 / 0755 ......... r--r--r--  everybody
        0604 ................ r-----r--  other

    So the mask is `value & 0666` and private is `& 0066 == 0` — NOT "no execute
    bits" and NOT "equals 0600". `0711` IS private, because the mask discards the
    execute bits; `0755` is NOT, because its group/other READ bits survive it. Those
    two rows are why this is a mask test and not a literal set, and both were
    mispredicted before they were measured.

    `(mode & 0600) != 0600` is git's own `die()` condition, so such a value reaches
    us only in a repository git already refuses to operate on. It stays off the
    accept side with every other value git rejects.

    THE PATTERN IS DELIBERATELY STRICTER THAN `strtol`, in the refusing direction.
    git accepts leading whitespace and a leading `+` (measured: `+0600` is private)
    which `[0-7]+` refuses — a false refusal, which this gate is contracted to
    prefer. Strictness in the other direction is the load-bearing half: Python's
    `int(value, 8)` accepts `0o600` and `0_600`, which git REJECTS, so parsing with
    Python's own rules would accept a repository git cannot open.

    THE EMPTY STRING IS EXCLUDED ON PURPOSE, and the `+` quantifier is what excludes
    it. `strtol("")` converts nothing, leaves `*end == 0` and yields 0, so git reads
    an explicitly empty value as PERM_UMASK, i.e. private. It is refused anyway,
    because `--get` answers an empty value and a VALUELESS `sharedRepository` line
    (PERM_GROUP, shared) with the same rc 0 and the same lone newline. What is
    refused there is the ambiguity, not the value — see the caller.
    """
    if value == "umask" or value.lower() in ("false", "no", "off"):
        return True
    if not _SHARED_REPOSITORY_OCTAL.fullmatch(value):
        return False
    mode = int(value, 8)
    if mode == 0:  # PERM_UMASK
        return True
    # The legacy 1 (PERM_GROUP) and 2 (PERM_EVERYBODY) need NO special case here even
    # though git special-cases them ahead of its filemode branch: neither satisfies
    # `& 0600`, so both land on the same refusal this line already returns. Written as
    # one test rather than three because a `mode in (1, 2)` arm would be indistinguish-
    # able from dead code — no test could fail without it (#384, round 10's rule).
    return mode & 0o600 == 0o600 and mode & 0o066 == 0


def _shield_shared_repository(worktree: Path, common_dir: Path) -> str | None:
    """Refuse a repository configured to be SHARED BETWEEN OS USERS, else None.

    A repository shared between OS users is not a supported configuration
    (maintainer decision, #384): the shield creates files of its own inside `.git`,
    and making those work across OS users is out of scope. Refusing the
    configuration up front is what makes that decision visible — it fails LOUDLY and
    identically for every user, with a reason that is journaled and notified, rather
    than behaving differently depending on which user provisioned first and leaving
    state behind for the next run to meet.

    This gate runs ABOVE the shield's lock, which is the point of it: nothing is
    created in a shared repository at all. That is safe here and would not be for
    `extensions.worktreeConfig` — this key is static configuration the tool never
    writes, so there is no shared mutable state to serialize.

    It adds no version floor: a plain `--get` with no `--type=`, so it does not
    reintroduce the 2.18 trap that keeps the version gate above the probes in
    `_shield_enable_worktree_config`.

    AN ALLOWLIST, NOT AN ENUMERATION OF SHARED SHAPES (round 7's funnel). git accepts
    keywords, booleans, the legacy 0/1/2, and bare octal modes; enumerating the
    shared ones leaves every value git adds later, and every value git rejects,
    silently opening the gate. Measured at git 2.20.4 AND 2.55.0, byte-identical at
    both ends of the supported range. The verdict column is git's OWN answer — the
    mode of a loose object git writes under `umask 077` — not a reading of `config.c`:

        absent .................. rc 1              (no key)
        umask / false/no/off / 0  rc 0   r--------  PRIVATE
        FALSE, Off .............. rc 0   r--------  PRIVATE
        empty      (`= ""`) ..... rc 0   r--------  PRIVATE   <- stdout is b"\\n"
        valueless  (no `=`) ..... rc 0   r--r-----  group     <- stdout is b"\\n" TOO
        true/yes/on / group / 1 . rc 0   r--r-----  group
        all/world/everybody / 2 . rc 0   r--r--r--  everybody
        octal filemodes ......... rc 0   varies     `_shared_repository_is_private`

    THE MIDDLE TWO ROWS ARE INDISTINGUISHABLE AND MEAN OPPOSITE THINGS: an explicitly
    empty value is PERM_UMASK (private) and a VALUELESS `sharedRepository` line is
    PERM_GROUP (shared), while `--get` answers both rc 0 with a lone b"\\n". git
    exposes nothing that separates them, so the empty answer REFUSES — a false refusal
    is a reported skip, a false accept is the bug this gate exists to prevent.

    `removesuffix("\\n")`, NEVER `.strip()`, and that is live rather than defensive
    here: `--get` returns edge whitespace VERBATIM (a quoted `" umask"` comes back as
    b" umask\\n", measured at both versions), so stripping would widen a value git
    itself rejects straight into the accept set below. Round 15's lesson at a second
    site in this same function.

    The case handling is measured rather than inferred from the accept list's shape:
    `FALSE` and `Off` really are not shared at both versions and are accepted, while
    `UMASK` and `GROUP` are values git REJECTS — git comparing the keywords with
    strcmp and the booleans with strcasecmp. Values git rejects DO reach this gate:
    `rev-parse --absolute-git-dir` answers rc 0 for every one of them, `banana`
    included, so the "it would have fatalled earlier" reasoning that fits some other
    probes in this file does not hold here. They fall off the allowlist into a
    refusal deliberately — their repository is already unusable (`git status` exits
    128, `fatal: bad boolean config value ... for 'core.sharedrepository'`), so
    refusing costs nothing while guessing at a malformed value would.

    SCOPE, stated because this is NARROWER than git's own resolution: the read names
    the repository's SHARED CONFIG, as every probe in `_shield_enable_worktree_config`
    does. Measured, git honors `core.sharedRepository` from a GLOBAL config too (an
    object written under a global `= group` came back `r--r-----` with the repo config
    empty), so an operator's global setting is not consulted here and such a
    repository is still shielded — unchanged from the behavior before this gate
    existed, and deliberately so, since a global preference is not a statement that
    THIS repository is shared. What is refused is a repository CONFIGURED as shared,
    which is the shape `git init --shared` writes: measured, `--shared=group` records
    `sharedrepository = 1`, the legacy numeric form the table above lists as group.

    WORKTREE SCOPE is unread too, and unlike the global case that omission rests on a
    barrier two modules away rather than on anything this function does. git DOES honor
    `core.sharedRepository` from a linked worktree's own `config.worktree` — measured at
    2.20.4 and 2.55.0, an object written from such a worktree under `umask 077` came back
    `r--r-----` against a `r--------` control, while the `--file` read below answers rc 1
    for it. What makes that unreachable is the provisioning flow: the shield only ever
    runs on a worktree this process just created (`workspace.open_unit_workspace` ->
    `verify.worktree_add` -> `worktree_flow.provision_worktree`), `git worktree add`
    creates the admin dir with NO `config.worktree` at either version, and a re-mount
    prunes `.git/worktrees/<id>` whole. The MAIN worktree's `.git/config.worktree` does
    not reach a linked one either (measured, `r--------`), and the tree's only
    worktree-scoped write is the shield's own `core.excludesFile`, further down this
    file. So the value has no writer and no window. If a re-provision path onto an
    EXISTING worktree is ever added, or anything upstream of the shield starts writing
    the unit worktree's config, this read must grow a second
    `--file <git_dir>/config.worktree` probe in the same change.
    """
    raw = _shield_shared_config(worktree, str(common_dir / "config"), "core.sharedRepository")
    if raw is None:
        return None
    value = os.fsdecode(raw).removesuffix("\n")
    if _shared_repository_is_private(value):
        return None
    # {value!r}: operator-authored text going into a journaled reason, and repr
    # neutralizes a newline in it. The version gate above renders git's own answer
    # the same way.
    return (
        f"skipped the git-add shield ({worktree}): the repository's shared config sets "
        f"core.sharedRepository = {value!r}, and a repository shared between OS users is "
        "not a supported configuration — the provisioned tool files are not shielded "
        "from the unit's `git add -A`"
    )


def _shield_enable_worktree_config(worktree: Path, common_dir: Path) -> tuple[str | None, bool]:
    """May `git config --worktree` be made writable in this repo? `(reason, needs_enable)`.

    PROBE ONLY — it does not write. `needs_enable` is True when every gate passed
    and the caller must still set `extensions.worktreeConfig` itself; False when the
    repo already carries it (or when `reason` is set and nothing may be written).
    Splitting the answer from the write is round 5's fix: enabling the extension is
    a PERMANENT repo-format change, and performed here it landed BEFORE the seed,
    the mkdir, the write and the activation — so every degrade below it, including
    the `_shield_inherited_excludes` one, left the operator's repo permanently
    marked for a shield that never applied. The caller now enables it immediately
    before the activation, and rolls the flag back when the shield then fails to
    hold — including when the ENABLE ITSELF fails in a way that leaves the outcome
    unknown, which is round 10. The flag outlives a failed shield only where the
    rollback was DECLINED because a sibling worktree depends on it, or could not be
    made at all; both are named in the returned reason.

    `extensions.worktreeConfig` is what makes a per-worktree config file exist at
    all; without it `git config --worktree` either refuses outright (a repo with
    linked worktrees) or silently writes the SHARED config. Already-true is the
    common case after the first isolated run — reported as `needs_enable` False, so
    the caller never rewrites it.

    Enabling it is a PERMANENT, repo-wide format change this code never reverses,
    so it is refused in the two shapes git's own documentation calls out
    (git-worktree(1), CONFIGURATION FILE): with `core.bare = true` or
    `core.worktree` in the shared config, enabling the extension drops the
    exception that confined those keys to the main worktree, and they would start
    applying to every worktree. Git says to move them into the main worktree's
    `config.worktree` first — a repo-layout edit the installer has no business
    making behind an operator's back — so the shield degrades instead, and the
    run continues with the tool files unshielded rather than the repo rearranged.

    The version gate and both refusal probes STAY HERE, ahead of everything, and
    deferring them the way the write was deferred would be a bug. `git config
    --worktree` does not exist before 2.20, so on an older git the enable buys a
    permanent format change with nothing to show for it — and git-worktree(1) says
    older git refuses a repository carrying the extension. And `--type=` is itself
    git 2.18: on an older git the two probes below exit non-zero, which this
    function USED to read as "not enabled" and, worse, as "not bare" — silently
    opening the core.bare gate the paragraph above exists to hold shut. That is the
    safety-gate-degrading-open trap, and it had a second door: ANY unanswerable rc
    did the same thing, not just an old git's. `_shield_shared_config` now closes
    that one by raising on every rc but 0 and 1 (round 10).

    THAT IS DEFENCE IN DEPTH BEHIND THIS GATE, NOT A REPLACEMENT FOR IT, and the
    distinction is worth stating because the funnel makes the 2.18 pair fail closed
    on its own and a later round could read that as licence to relax the floor. It is
    not: this gate rests on two arguments the funnel does not touch — a permanent
    format change bought on a git that cannot use it, and git-worktree(1)'s older-git
    refusal — and it keeps the 2.18 pair UNREACHABLE rather than merely survivable.
    The caller's own `--type=path` excludesFile read depends on the same floor.
    So: the GATES run first and early, the WRITE runs last and late.
    """
    # Polarity note, deliberately recorded: this gate reads `returncode != 0` as
    # REFUSE, while the funnel below reads it as "git did not answer" and raises.
    # Both fail closed, by opposite-looking means, and unifying them would reverse
    # one of them — an unanswerable `git version` must refuse here, not degrade into
    # a question about a key. Nor can this gate catch a bad repo config on git's
    # behalf: `{ "version", cmd_version }` carries no flags at all in git.c, so it
    # never does repository setup, and measured at 2.20.4 and 2.55.0 `git version`
    # exits 0 under both a malformed `.git/config` and a non-bool `core.bare` while
    # `rev-parse` fatals 128 on each.
    version = git_bytes(worktree, "version")
    if version.returncode != 0 or not _git_version_at_least(
        os.fsdecode(version.stdout), _WORKTREE_CONFIG_GIT
    ):
        want = f"{_WORKTREE_CONFIG_GIT[0]}.{_WORKTREE_CONFIG_GIT[1]}"
        found = os.fsdecode(version.stdout).strip() or f"git exited {version.returncode}"
        return (
            f"skipped the git-add shield ({worktree}): needs git {want} for "
            f"extensions.worktreeConfig and `git config --worktree`, but git answered "
            f"{found!r} — enabling the extension on an older git is a permanent "
            "repo-format change that would shield nothing"
        ), False
    # EVERY read below names the SHARED config as a file rather than going by
    # scope. `--local` from a linked worktree already resolves to this same file,
    # but naming it keeps the checks honest if that ever stops being true — and,
    # load-bearing for the first of them, it stops a value the operator set
    # GLOBALLY from answering a question about this repository's format.
    #
    # `extensions.*` is repo-format state git honors only from the repo's own
    # config, so an unscoped `--get extensions.worktreeConfig` asks the wrong file:
    # a `worktreeConfig = true` in ~/.gitconfig answers it, this returns "already
    # enabled", the write below never happens, and the activation dies with
    # `fatal: --worktree cannot be used with multiple working trees unless the
    # config extension worktreeConfig is enabled` — the shield skipped over a repo
    # that was one write away from supporting it.
    #
    # And NO `--includes` on any of the three, which is the asymmetry a later
    # reader (or bot round) will want to "fix" — `--file` without it does miss a
    # value an `include.path` supplies. Adding it would break both gates:
    #
    # - The two REFUSAL probes ask whether enabling the extension would break the
    #   operator's worktrees, and git's worktree SETUP reads `core.bare` /
    #   `core.worktree` only from the common config literally. It reaches them
    #   through `git_config_from_file` (setup.c's `check_repo_format`), and include
    #   expansion is not a parser feature but a callback wrapper installed in
    #   `config_with_options()` alone — so `git_config_from_file` does not respect
    #   includes, in git's own header's words (config.h). An `include.path` arrives
    #   there as an ordinary unmatched key. Measured with the extension enabled at
    #   git 2.20.4 / 2.32.7 / 2.43.7 / 2.49.1 / 2.55.0, identical at all five: a
    #   LITERAL `core.worktree` moves a linked worktree's toplevel to the main
    #   checkout and a literal `core.bare = true` fatals every command, while
    #   include-supplied values of either do nothing at all. `--includes` here
    #   would therefore refuse repos git is perfectly happy to shield — leaving the
    #   provisioned tool files unshielded, the harm this shield exists to prevent.
    # - The ENABLED probe is worse: an include-supplied `extensions.worktreeConfig`
    #   is not honored either (`git config --worktree` still fatals at all five
    #   versions), because the `config.worktree` read is gated on the flag harvested
    #   by that same include-blind setup read. `--includes` there is a false
    #   "already enabled" → the write is skipped → the activation dies → nothing is
    #   shielded. That is commit c6d5960's bug re-entering through the include door.
    #
    # Two bounds on the claim, so a later reader knows what would invalidate it.
    # It is an undocumented IMPLEMENTATION DETAIL, not a compatibility contract:
    # git has deliberately converted reads to respect includes before (ecec57b3c973,
    # git 2.39, "config: respect includes in protected config"), and a future
    # `config_with_options()` conversion could flip it with no release note. And
    # `core.bare`'s inertness is a CONJUNCTION, not an absence — an include-supplied
    # `core.bare = true` does reach `repo->bare_cfg` on the normal
    # include-respecting path; what keeps it harmless is `is_bare_repository()`
    # requiring `bare_cfg && !repo_get_work_tree(repo)`, and every worktree this
    # shield touches has a work tree. `core.worktree` needs no such caveat: it is
    # read as a config key only in setup.c. Flag availability was never the
    # question either way — `--includes` dates to git 1.7.10, far below the 2.20
    # floor the gate above enforces.
    shared = str(common_dir / "config")
    carried = _shield_shared_config(worktree, shared, "extensions.worktreeConfig", "--type=bool")
    if carried is not None and os.fsdecode(carried).strip() == "true":
        return None, False  # already carried: nothing for the caller to write
    bare = _shield_shared_config(worktree, shared, "core.bare", "--type=bool")
    if bare is not None and os.fsdecode(bare).strip() == "true":
        refused = "core.bare = true"
    elif _shield_shared_config(worktree, shared, "core.worktree") is not None:
        # `is not None`, NEVER truthiness: any value refuses, including an empty one.
        # Measured at 2.20.4 and 2.55.0 — `core.worktree = ""` and a valueless
        # `worktree` line BOTH answer rc 0 with a lone b"\n", so raw truthiness
        # happens to hold today, but it is one `.strip()` away from b"", which is
        # falsy, and that would re-open this gate for a key that IS set. The rc is the
        # only thing separating present-but-empty from absent, and returning it as
        # None-or-value is exactly what the helper exists to do. Never decoded, and
        # the value never inspected: this key is a path, and its mere PRESENCE is the
        # refusal — unlike `core.bare`, where only a true value disqualifies.
        refused = "core.worktree"
    else:
        # Cleared, not enabled. The write itself is the caller's, deferred to the
        # last moment before the activation (see the docstring).
        return None, True
    return (
        f"skipped the git-add shield ({worktree}): the repository's shared config sets "
        f"{refused}, which git requires moving into the main worktree's config.worktree "
        "before extensions.worktreeConfig may be enabled"
    ), False


def _shield_undo_extension(worktree: Path, git_dir: Path, common_dir: Path) -> str:
    """Undo the `extensions.worktreeConfig` THIS provisioning just enabled.

    Returns `""` when the repository is back in the state it was found in, or a
    clause naming the fault when it is not — never raises, because every caller is
    already reporting a degrade and this exists to say what it could not undo.

    Callers MUST gate this on having enabled the flag themselves — or, since round
    10, on not having been able to FIND OUT whether they did, which is what the
    enable's own raise means. Unsetting one the repository already carried would stop
    git reading the `config.worktree` files other worktrees may depend on — the
    reason the shield's rollback is conditional rather than unconditional cleanup.

    That caller-side gate is necessary and NOT sufficient, which is round 8 (#384,
    Codex P2). `needs_enable` records what the probe saw, and the enable itself is an
    idempotent rc-0 no-op against a flag that is already `true` — so two concurrent
    provisionings that both probed an absent flag both believe they own it, and
    whichever one's activation fails then unsets a flag the other's LIVE shield
    depends on: git stops reading its `config.worktree`, its `core.excludesFile`
    goes inert, and its shielded tool paths become stageable again mid-run. The
    caller serializes the whole transaction under a repository lock, which closes
    that window for processes taking the lock. This scan is the fallback for the
    flag having been enabled OUTSIDE that discipline — an older bmad-loop, a
    concurrently running different version, an operator, or another tool entirely
    (`git sparse-checkout` enables this same extension) — so it is not redundant
    with the lock and must not be deleted as such. A lock binds only its holders.

    EXCLUDING OUR OWN `git_dir` IS LOAD-BEARING, not tidiness: a `config.worktree`
    in it is the half-written product of the very activation whose failure prompted
    this rollback (a timeout can leave one behind), and treating that as a dependent
    would suppress every rollback the round-6/7 fixes exist to make.

    WHAT THIS DOES NOT COVER, stated rather than left for another round to find (the
    round-6 lesson: a rationale of "now nothing can go wrong" has to enumerate its own
    remaining cases). The scan reads an EXISTING file, so a non-lock-taking party
    sitting between its own enable and its own activation owns the flag while having
    written nothing yet, and this rollback will unset it. The consequence is bounded
    and different in kind from the finding above: that party's `git config --worktree`
    then fatals for want of the extension, so its shield degrades with a REPORTED
    reason instead of silently going inert mid-run. That bound depends on a
    precondition this scenario supplies for free, and it is worth naming because the
    other branch is #384 itself: `git config --worktree` only REFUSES (rc 128) once a
    repository has more than one worktree, and in a single-worktree repo it exits 0
    and writes the SHARED config instead. Here a sibling exists by construction — it
    is the other party — so the refusal is the one git gives. The reverse asymmetry is also
    accepted — a stale admin dir git has not pruned yet counts as a dependent, which
    costs a flag left enabled and nothing else.

    Round 10 widened that window's APERTURE without changing its bound: this rollback
    now also runs from the enable's own raise, one call earlier, so there are two
    occasions on which a non-lock-taking party can be caught mid-transaction rather
    than one. Same class and same bounded consequence, twice as often.

    Measured, because "the key is gone" and "the extension is off" are two claims:
    the unset exits 0, removes the key AND the now-empty `[extensions]` section (the
    config file returns to its original bytes), and `git config --worktree` refuses
    again afterwards with git's own fatal — so this is a real rollback, not cosmetic.
    An ABSENT key exits 5, which used to be unreachable ("the gate above means we
    never do") and stopped being so in round 10: the enable's raise routes here
    without knowing whether anything was written. See the call below for why that
    makes rc 5 a success and why the spelling is `--unset-all`.
    """
    try:
        # The main worktree's own, then every SIBLING's — never ours (above).
        others = [common_dir / "config.worktree"]
        others += [
            d / "config.worktree"
            for d in (common_dir / "worktrees").iterdir()
            if d.resolve() != git_dir
        ]
        dependent = next((str(p) for p in others if p.exists()), None)
    except (OSError, RuntimeError, UnicodeError) as e:
        # Conservative on purpose, and the asymmetry is the argument: skipping
        # leaves a reported flag this call did not earn, while unsetting one that
        # IS depended on silently un-shields a live worktree. A cosmetic residue
        # beats silent file loss, so an unreadable scan counts as a dependent.
        #
        # The tuple mirrors the caller's tail minus `GitError` (nothing here spawns
        # git), and the two beyond `OSError` are what keeps this function's "never
        # raises" promise true rather than merely stated — round 9 (#384,
        # CodeRabbit), and measured, not reasoned: with `resolve()` made to fail the
        # way a symlink loop does, the bare `OSError` tuple let it escape. On the
        # Python 3.11/3.12 floor `Path.resolve()` raises `RuntimeError` for a
        # symlink loop rather than `OSError` (the caller's own tail already carries
        # it for exactly that), and `UnicodeError` covers the fsdecode of a
        # directory name on Windows. Escaping here is not a crash — the caller's
        # tail catches all three — but it replaces the activation fault AND the
        # retained-flag disclosure with a generic reason, losing precisely what
        # rounds 6 and 7 added.
        dependent = f"the scan for sibling worktrees failed ({e})"
    if dependent is not None:
        return (
            "; extensions.worktreeConfig was deliberately LEFT enabled — "
            f"{dependent} exists, so another worktree's shield depends "
            "on the flag and unsetting it would stop git reading that file"
        )
    try:
        # `--unset-all`, and rc 5 counts as SUCCESS. Both halves are round 10's, and
        # neither is cosmetic. Routing an uncertain ENABLE into this rollback makes an
        # absent flag reachable here for the first time, and `--unset` of an absent
        # key exits 5 — which rendered as "could NOT be rolled back (git exited 5)"
        # for a repository whose flag is in fact gone: a false disclosure.
        #
        # `--unset` alone could not simply treat 5 as success, because git-config(1)
        # gives that code TWO meanings — "unset an option which does not exist" and
        # "unset/set an option for which multiple lines match" — so a doubled key that
        # SURVIVED would report a clean rollback. Measured at 2.20.4 and 2.55.0,
        # identical: `--unset` against a doubled key exits 5 and removes NOTHING,
        # while `--unset-all` exits 0 and removes both lines. `--unset-all` therefore
        # collapses rc 5 to the single meaning "no line matched", which makes the
        # guarantee structural instead of resting on a four-step argument about which
        # doubled values can reach here. It is also what this function actually
        # promises: a statement about the KEY, not about one line.
        undone = git_bytes(worktree, "config", "--unset-all", "extensions.worktreeConfig")
        if undone.returncode in (0, 5):
            return ""
        detail = os.fsdecode(undone.stderr).strip() or f"git exited {undone.returncode}"
    except GitError as e:
        # the rollback's OWN git can time out or fail to spawn. Both writes failing
        # is the coherent case rather than a contrived one: a read-only `.git` or a
        # dead git fails the unset for the same reason it failed the activation.
        detail = str(e)
    # Both clauses HEDGE whether this shield set the flag, which round 10 made
    # necessary: reached from the enable's own raise, a spawn failure can kill the
    # enable and this unset for the same reason, having written nothing at all — and
    # the old wording ("was enabled for this shield and could NOT be rolled back")
    # then asserted a format change the repository does not carry. No real precision
    # is lost: `needs_enable` is probe-derived and already cannot tell "we enabled it"
    # from "we and a concurrent run both thought we did".
    return (
        "; extensions.worktreeConfig could NOT be "
        f"rolled back ({detail}) — if this shield set the flag, the repository keeps "
        "a permanent format change that shields nothing"
    )


def _shield_home_git_ignore(worktree: Path) -> Path:
    """`$HOME/.config/git/ignore` — git's XDG fallback — asked of GIT, not of Python.

    `Path.home()` is the obvious spelling here and it is WRONG on Windows, the one
    platform where git's home and Python's home are derived differently. Git locates
    this fallback with `getenv("HOME")` on every platform (`path.c::xdg_config_home`,
    semantics unchanged 2.20.0 → 2.55) — but on Windows
    `compat/mingw.c::setup_windows_environment()` DERIVES `HOME` inside the git
    process before that runs, preferring `HOMEDRIVE`+`HOMEPATH` (and only when that
    names a real directory) over `USERPROFILE`. Python's `ntpath.expanduser` has the
    OPPOSITE precedence — `USERPROFILE` first — and never consults `HOME` at all. Two
    ordinary setups therefore disagree:

    * anything that sets `HOME` (Git Bash and MSYS2 do), where git obeys it and
      Python cannot see it; and
    * a domain-joined machine whose `HOMEDRIVE`+`HOMEPATH` is a network home share,
      where git reads `H:\\…\\.config\\git\\ignore` and Python reads `C:\\Users\\…`.

    The miss is SILENT, and it is #384's own harm reached through the platform split:
    the wrong path is simply not a file, the seed comes back empty, and the caller's
    activation then SHADOWS the global ignore file git really reads — un-ignoring
    inside the worktree exactly what this branch exists to preserve.

    So ask git. `--type=path` interpolates a leading `~/` through the same
    `getenv("HOME")` git uses for the fallback itself, which makes the answer correct
    by construction on every platform and names no environment variable — round 18's
    lesson (ask what git RESOLVED, not how the environment might have told it) at the
    sibling site. `-c` is COMMAND scope, the most specific git has, so the probe's
    value cannot be outranked by the operator's config: the very precedence rule that
    caused round 18 is what makes this reliable.

    MEASURED at git 2.20.4 (docker `alpine:3.9`, non-root, `id -u` checked) and
    2.55.0, byte-identical: the probe returns `$HOME/.config/git/ignore` NUL-
    terminated at rc 0, and that is the file git really loads patterns from — proven
    through `git status` hiding a name the file lists, not inferred from the path.
    With `HOME` unset it exits 128 and git applies no fallback at all.

    KNOWN GAP, recorded so this is not read as complete: this answers "where is
    git's `$HOME`", which is the whole of the fallback UPSTREAM but not on **Git for
    Windows >= 2.46**, whose fork patches `xdg_config_home_for` to prefer
    `%APPDATA%/Git/<file>` whenever that file EXISTS — it even warns that the `$HOME`
    one "was ignored because" the APPDATA one is there. Verified by reading the
    fork's `path.c` and bounded by version: 1 occurrence of `APPDATA` at
    `v2.46.0.windows.1`, 0 at `v2.45.0.windows.1`, `v2.20.0.windows.1`, and 0
    upstream at master. So an operator on a current Git for Windows who keeps their
    global ignores at `%APPDATA%\\Git\\ignore` still gets the wrong file seeded here.
    Deliberately NOT fixed in this pass: it is a downstream fork's behavior, gated on
    a version well above this shield's floor, and there is no way to MEASURE it from
    a POSIX box — which is this branch's standing bar for a git claim.

    Raises `GitError` when git did not answer, INCLUDING that `HOME`-unset rc.
    Proceeding on a non-zero rc would be a guess, and a guess here is silent: the
    caller seeds nothing and activates over whatever git does read. git's message is
    deliberately NOT matched to tell "no HOME" apart from a transient fault, because
    that wording is version-dependent (it differs between 2.20.4 and 2.55.0 for other
    config faults on this same branch). The cost is that a genuinely `HOME`-less
    environment now skips the shield with a journaled reason instead of proceeding
    with an empty seed — and that direction is REPORTED, which is the whole taxonomy
    of the caller: only a definite absent may be silent.
    """
    key = "bmadloop.xdghomeprobe"
    probe = git_bytes(
        worktree, "-c", f"{key}=~/.config/git/ignore", "config", "-z", "--type=path", "--get", key
    )
    if probe.returncode != 0:
        detail = os.fsdecode(probe.stderr).strip() or f"git exited {probe.returncode}"
        raise GitError(f"git could not resolve its own home directory: {detail}")
    return Path(os.fsdecode(probe.stdout.split(b"\0", 1)[0]))


def _shield_inherited_excludes(worktree: Path) -> bytes:
    """The BYTES of whatever `core.excludesFile` this worktree resolves to now.

    A worktree-scoped `core.excludesFile` SHADOWS the operator's own: git reads
    the key from the most specific scope that sets it and does not concatenate
    across scopes (verified — with a worktree value set, a path listed in the
    global excludes file shows up as untracked inside the worktree and stays
    ignored in the main checkout). Activating the shield without carrying their
    patterns over would therefore un-ignore, inside the worktree, everything they
    ignore globally — and the unit's `git add -A` would commit it.

    Seeded once, at creation, so the copy never fights later edits to the private
    file.

    BYTES, never decoded text, and the return type is the fix: an exclude file is
    a list of path patterns, POSIX paths are arbitrary bytes, and a `read_text`
    here made one non-UTF-8 byte anywhere in the operator's file collapse the whole
    seed to empty — after which the activation SHADOWED the excludes it had failed
    to copy, and `git add -A` staged what they told git to ignore (measured
    end-to-end). Copying verbatim also keeps every pattern intact for the caller's
    dedupe, which a text round trip could not promise. Git reads the result either
    way: it strips a trailing `\\r` from an exclude line and skips a UTF-8 BOM.

    FOUR outcomes, and which answer means what is the whole subtlety. The first
    two are silent; this function RAISES for the other two, and the caller's tail
    turns that into the degrade reason that skips the activation. Only ABSENT has a
    fallback, and routing DISABLED into it was round 8's bug.

    - ABSENT is silent and empty, and the only outcome that falls back. The common
      case — no `core.excludesFile`, no XDG fallback file — and not a reason to skip
      the shield. It is an ANSWER: `--get` of an unset key exits 1, and `is_file()`
      false says the file is not there. One caveat added with the `$HOME` probe:
      REACHING the fallback path can itself fail, and `_shield_home_git_ignore`
      raises when it does. That is not this outcome — an unresolvable home is
      UNKNOWN, not absent, and gets the loud arm like every other unknown here.
    - DISABLED is silent and empty too, and it is NOT absent (#384, Codex round 8).
      An explicitly EMPTY `core.excludesFile` is the operator saying "no excludes
      file at all", and git honors that literally rather than treating it as unset:
      measured at 2.55.0, `-z --get` answers rc 0 with a lone NUL, after which git
      loads no patterns and does NOT reach for XDG (`git check-ignore` exits 1
      against a name the XDG file lists). The mechanism is in git's source, not
      inferred: `dir.c::setup_standard_excludes` guards the XDG fallback on
      `if (!excludes_file)` — a NULL POINTER — while `config.c` resolves an empty
      value through `interpolate_path("")`, which returns a non-NULL empty string.
      Byte-identical guard at v2.20.0, this shield's own floor, and at master.
      Reading that as unset (the pre-round-8 `and raw`) seeded the XDG file's
      patterns in and activated them, which is the MIRROR IMAGE of every other
      fault here: instead of shadowing patterns it failed to copy it OVER-ignores,
      so session-created files the operator deliberately stopped ignoring go
      silently missing from the unit's `git add -A` and from the story's commit.
      Same silent-file-loss class as #384 itself, in the other direction.
    - PRESENT BUT UNREADABLE propagates. A read `OSError` (EACCES, EIO) reaches the
      caller's degrade arm, which skips the activation — the only safe answer, since
      activating over patterns that could not be copied shadows them exactly as an
      empty seed did.
    - UNKNOWN propagates too, and it arrives in TWO shapes that this function
      deliberately funnels rather than enumerates — a raised `GitError`, and any
      returncode that is neither 0 nor 1.

      The raise is round 5's fix (#384, Codex P1). A `GitError` out of the read below
      used to be swallowed here on the premise that "git unqueryable ⇒ no key to
      resolve ⇒ nothing to copy". The premise is a false inference: a raised fault
      means we did not FIND OUT — so the code mapped UNKNOWN onto ABSENT and then
      activated a key that shadowed the file it had failed to read. Measured,
      and pinned by the ablation on
      `test_shield_degrades_when_git_will_not_say_what_the_users_excludes_are`:
      inject a fault on this one call and a file the operator ignores globally comes
      back `?? <name>` inside the worktree, ready for the unit's `git add -A`, with
      no reason returned at all. Nor is a permanently
      unqueryable git reachable here — by the time this runs `git_bytes` has
      answered at least four times, so such a git already left through the caller's
      silent `rev-parse` arm. What arrives is a TRANSIENT fault, and for those the
      premise is false by construction: the key is there, we just cannot see it.
      The accepted cost is that such a fault now loses the shield for that run,
      which the caller surfaces; the harm it buys off is unbounded and silent.

      The rc shape is round 9's (#384, CodeRabbit). Round 5's bullet used to justify
      itself by saying the swallow "holds for `rc != 0`, which is an answer" — and
      that generalization was too broad, which is the correction worth recording
      here rather than quietly dropping. Only **rc 1** means "no such key"; every
      other non-zero rc is git reporting that it could not answer, and routed to
      ABSENT it seeded and activated the XDG file over patterns it had not read.
      What can actually occupy that branch is narrower than it looks, and the
      comment at the call below carries the measurement: not a static config value
      — those fatal the `rev-parse` in the caller first, which skips the shield
      silently — but a fault arriving inside the window the caller holds its lock.
    """
    # -z, not a bare read plus `.strip()` — round 5, Codex P2. A `.strip()` here
    # DESTROYED a legal POSIX path: `--type=path --get` returns leading and
    # trailing whitespace VERBATIM, because git writes such a value quoted and it
    # round-trips. So an operator whose excludesFile ended in a space had the strip
    # mangle the path, `is_file()` read absent, the seed come back empty, and the
    # activation shadow the file it never copied — the same leak as the encoding bug
    # above, through a different door. `-z` terminates the value with NUL instead,
    # which git-config(1) recommends for exactly this ("allows for secure parsing of
    # the output without getting confused by values that contain line breaks").
    #
    # Everything below is measured at git 2.20.4 AND 2.55.0 — both ends of the
    # supported range, the flag checked AT the floor rather than inferred from
    # git-config(1), since the 2.20 refusal in `_shield_enable_worktree_config` has
    # already run by the time this does and 2.20 is therefore the oldest git that
    # can reach this line. `-z` still expands `~`, gives an unset key rc 1 + empty
    # stdout, an empty value a lone NUL at rc 0, a multi-valued key the LAST value +
    # NUL at rc 0, and a relative value verbatim.
    #
    # That empty-value branch is the one round 5 got WRONG, and how is worth
    # recording: this comment used to say the lone NUL "falls to XDG the way it
    # did" — a measurement of OUR READER's classification and of behavior
    # preservation, which never asked what GIT does with the same value. Preserving
    # a behavior is not the same as that behavior being correct. Git treats it as
    # "no excludes file", so the value is an ANSWER and gets its own branch below;
    # see the docstring's DISABLED outcome for git's own source.
    #
    # The sibling `rev-parse` strips below are deliberately NOT getting the same
    # treatment, and this is the note that stops a later round re-raising it:
    # `rev-parse` has no `-z` (it echoes one back as a literal argument), and it
    # cannot answer with edge whitespace anyway — git SANITIZES the worktree admin
    # id, so a worktree at `…/wt ` gets `.git/worktrees/wt-` (measured), and
    # `--git-common-dir` ends in `.git`.
    answer = git_bytes(worktree, "config", "-z", "--type=path", "--get", "core.excludesFile")
    raw = answer.stdout.split(b"\0", 1)[0]
    # rc FIRST, and the value only once rc says there IS one — the condition used to
    # be `returncode == 0 and raw`, which collapsed DISABLED onto ABSENT. Round 8's
    # fix is the branch below, not this reordering; the reordering is what makes the
    # two answers nameable apart.
    if answer.returncode == 0:
        if not raw:
            # DISABLED: an explicit "no excludes file", so there is nothing to copy
            # and NO fallback to reach for — see the docstring. Note for a later
            # reader (or ablation): deleting this arm does not restore the bug, it
            # only hides it, because `Path(os.fsdecode(b""))` is `.`, which resolves
            # to the worktree directory and reads `is_file()` false. The bug is the
            # ROUTING of this answer into the XDG branch, so the only faithful
            # ablation is restoring `and raw` on the condition above.
            return b""
        source = Path(os.fsdecode(raw))
        if not source.is_absolute():
            # `--type=path` expands `~` and stops there: a RELATIVE value comes
            # back verbatim. Git resolves such a value against the worktree's
            # top level (MEASURED — git 2.20.4, 2.49.1, 2.55.0 — and NOT
            # specified: relative values for this key were deliberately never
            # implemented, and the worktree-top result is an artifact of git's
            # setup chdir, so a consumer reached through RUN_SETUP alone with an
            # explicit git dir and an outside cwd resolves it against the
            # CALLER's cwd instead. bmad-loop is not exposed to that: every git
            # call here goes through `_run_git`'s `git -C <repo>`, and the
            # activation below writes an absolute path.) Python would resolve it
            # against this process's cwd, which is wherever the orchestrator was
            # launched. Left alone that reads the wrong file or, far more often,
            # none — and `is_file()` false is silent, so the operator's patterns
            # would simply not be carried and the activation below would shadow
            # them anyway. Un-ignoring inside the worktree exactly what this
            # function exists to preserve.
            source = worktree / source
    elif answer.returncode != 1:
        # UNKNOWN, through git's OTHER failure shape. rc 1 is the ONLY non-zero rc
        # that means "no such key"; every other one is git saying it could not answer,
        # and routing those to the fallback below is the same UNKNOWN-as-ABSENT
        # mistake round 5 fixed for the RAISED shape (#384, CodeRabbit round 9).
        #
        # WHAT CAN REACH HERE is narrower than the finding's own example, and the
        # measurement is worth keeping because the plausible version of it is wrong.
        # `core.excludesFile = ~nosuchuser/ignore` — an ordinary thing to inherit from
        # a `.gitconfig` synced between machines — does make THIS read exit 128
        # (`fatal: failed to expand user dir`). But git expands that same value while
        # parsing core config for any command that sets the repository up, so
        # `rev-parse --absolute-git-dir` in the caller fatals identically and the
        # shield SILENTLY SKIPS there, above this line. Measured at git 2.20.4 and
        # 2.55.0, both ends of the supported range. No static config value reaches
        # this branch: its occupants are faults that appear BETWEEN that rev-parse
        # and this read.
        #
        # That window is structural rather than theoretical, and the caller sets its
        # width: the rev-parse runs before the repo-scoped lock, this read runs after
        # it, and POSIX `flock` blocks INDEFINITELY — so a run waiting on a sibling
        # worktree's shield sits between the two calls for that sibling's whole git
        # transaction. A dotfile manager writing `~otheruser/ignore` in, or an
        # `~alice` whose NSS/LDAP lookup fails for a moment, lands here.
        #
        # "The repo is broken anyway, so the harm is bounded" does not survive that:
        # the fault can CLEAR (the sync finishes, LDAP answers) while an activation
        # performed over it lasts as long as the worktree — a `core.excludesFile`
        # shadowing the operator's real excludes with the XDG file's patterns. #384's
        # own harm, reached through a transient.
        #
        # Deliberately shaped as a FUNNEL rather than a per-rc taxonomy, which is
        # round 7's lesson: git-config(1) allocates several failure codes and
        # enumerating them one per review round is how the activation arm took three.
        # Anything that is not "answered" or "no such key" is not an answer.
        detail = os.fsdecode(answer.stderr).strip() or f"git exited {answer.returncode}"
        raise GitError(f"git could not resolve core.excludesFile: {detail}")
    else:
        # ABSENT (rc 1, an unset key): git's documented fallback (gitignore(5)), and
        # the ONLY outcome that gets one. Not reachable for an empty value any more.
        #
        # `XDG_CONFIG_HOME` this process may read for itself: git reads the same
        # variable from the same environment (`_run_git` passes `os.environ`
        # through), and set-but-empty counts as unset for BOTH — git's guard is
        # `if (config_home && *config_home)`, the falsy check below is the same
        # test. The `$HOME` limb is the one Python cannot answer; see
        # `_shield_home_git_ignore` for why it is asked of git instead.
        xdg = os.environ.get("XDG_CONFIG_HOME")
        source = Path(xdg) / "git" / "ignore" if xdg else _shield_home_git_ignore(worktree)
        if not source.is_absolute():
            # THE SAME DEFECT AS THE RELATIVE `core.excludesFile` ABOVE, at the
            # sibling site (#384, Codex round 12) — this branch reproduces git's
            # fallback, so it has to reproduce git's RESOLUTION too, and it did not.
            #
            # A relative XDG_CONFIG_HOME is invalid per the XDG base-directory spec
            # ("All paths ... must be absolute. If an implementation encounters a
            # relative path ... it should consider the path invalid and ignore it"),
            # but git does not ignore it: measured at 2.20.4 and 2.55.0, git HONORS
            # the value and resolves it against the worktree's TOP LEVEL. Measured
            # from a SUBDIR to tell top-level from cwd, the same way the branch above
            # was — `git -C <wt>/sub` with `XDG_CONFIG_HOME=rel` read
            # `<wt>/rel/git/ignore`, not `<wt>/sub/rel/git/ignore`.
            #
            # Python resolves it against the orchestrator's launch cwd instead, and
            # the miss is SILENT: `is_file()` false, an empty seed, and then the
            # activation shadows the file git really reads. Un-ignoring inside the
            # worktree exactly what this function exists to preserve — #384's harm
            # reached through a misconfigured environment rather than a fault.
            source = worktree / source
    # `is_file()` is the ABSENT test and swallows its own OSError; `read_bytes`
    # on a file that exists but cannot be read raises, deliberately (docstring).
    return source.read_bytes() if source.is_file() else b""


def _shield_verify_activation(worktree: Path, exclude: Path) -> str | None:
    """Why the just-written `core.excludesFile` is NOT the one git reads, or None.

    A successful `git config --worktree core.excludesFile` proves the value was
    WRITTEN, not that it is in FORCE. git resolves the key from the most specific
    scope that carries it, and `worktree` is not the most specific one — `command`
    outranks it, and that scope is fed by the ENVIRONMENT the orchestrator was
    launched with. So an operator carrying an ambient `core.excludesFile` gets a
    shield that reports success, writes the private file, and never has it read:
    the provisioned tool files stay stageable by the unit's `git add -A`, with no
    degrade reason to journal. Round 17 was "a pattern PRESENT in the file is not
    EFFECTIVE"; this is the same gap one level up — WRITTEN is not EFFECTIVE.

    So the post-condition is asked of git rather than assumed, and the shape is the
    point. The obvious fix is to detect the ORIGIN — look for `GIT_CONFIG_COUNT`
    and friends in `os.environ` — and it is the enumeration this function exists to
    avoid, because the channels are neither one nor stable. Measured end to end, on
    a real linked worktree with the shield fully installed, at BOTH ends of the
    supported range:

                                        git 2.20.4        git 2.55.0
        GIT_CONFIG_COUNT/KEY_n/VALUE_n  inert (2.31)      shield defeated
        GIT_CONFIG_PARAMETERS 'k=v'     shield defeated   shield defeated
        GIT_CONFIG_PARAMETERS 'k'='v'   fatal: bogus      shield defeated
        what `git -c` itself emits      'k=v'             'k'='v'

    Three names in two mutually incompatible encodings, and the one the finding
    that prompted this named does not exist at this shield's own floor. A `git -c`
    on a session's own command line is a fourth and is not visible in our
    environment at all. Asking git what it RESOLVED costs one call and covers every
    one of them, including whatever git adds next.

    `--show-scope` would name the winning scope and make the reason better, and is
    deliberately not used: it is git 2.26, above the 2.20 floor, and a probe flag
    with its own version floor turns every rc-branch below it into a silent default
    (the `--type=` 2.18 trap, one round-1 finding away from this one).

    The read shape is the seed read's, already measured at 2.20.4 and 2.55.0: `-z`
    because a legal POSIX path may carry edge whitespace, `--type=path` because
    that is how git itself resolves the key. Any non-zero rc is a fault here rather
    than an ABSENT answer — this call asks about a key we have just written, so
    "there is no such key" is not good news about it. That is a DIFFERENT taxonomy
    from `_shield_inherited_excludes`, which reads a key the operator may simply
    not have set; the two must not be unified.

    The comparison is byte-exact and stays that way. Round-tripping a path through
    `git config` and back was measured for every hazard this shield has already
    been burned by — a space, leading and trailing whitespace, an embedded newline,
    a non-UTF-8 byte, an interior `~` — all seven exact at both versions, so an
    inexact comparison would be buying nothing and could only mask a real
    mismatch. Windows CI is the oracle for the one platform not measured here: git
    escapes a backslash path it is handed as an ARGUMENT and reads it back intact,
    which is why the shield's other tests pass there, but that claim is about the
    config file rather than about `--type=path`.
    """
    resolved = git_bytes(worktree, "config", "-z", "--type=path", "--get", "core.excludesFile")
    if resolved.returncode != 0:
        detail = os.fsdecode(resolved.stderr).strip() or f"git exited {resolved.returncode}"
        return f"git would not confirm which excludes file now applies: {detail}"
    effective = resolved.stdout.split(b"\0", 1)[0]
    if effective == os.fsencode(str(exclude)):
        return None
    return (
        "the write succeeded but another configuration scope outranks it, so git "
        f"reads {os.fsdecode(effective)!r} instead and the shield's patterns never apply"
    )


def _worktree_local_exclude(worktree: Path, patterns: Sequence[str]) -> str | None:
    """Shield the just-provisioned tool files from the unit's `git add -A`, in a
    private exclude file scoped to THIS worktree alone.

    Two properties, both load-bearing, and both of them were wrong before #384:

    - SCOPE — the patterns land in `<worktree gitdir>/info/exclude`
      (`.git/worktrees/<id>/info/exclude`), activated by
      `git config --worktree core.excludesFile <that path>`. Git only ever reads
      `$GIT_COMMON_DIR/info/exclude`, so the private file does nothing on its own;
      the worktree-scoped config key is what makes it apply, and it applies to
      this worktree only. The main checkout and every sibling worktree are
      untouched. Writing that key is not the same as it being in FORCE — `command`
      scope outranks `worktree`, and it is fed by the environment the orchestrator
      was launched with — so the activation is VERIFIED against what git actually
      resolves rather than trusted; see `_shield_verify_activation`.
    - LIFETIME — `git worktree remove`/`prune` deletes the whole per-worktree
      gitdir, taking the private exclude AND the `config.worktree` that points at
      it. The shield expires exactly when the thing it shields does; there is no
      remover to keep in step with it.

    The repository-wide `.git/info/exclude` is NEVER written again, on any path.
    That file is what #384 reported: shared by every worktree, permanent, and
    unversioned, so patterns naming directories a project legitimately tracks
    (`.claude/skills`, `.claude/settings.json`) went on hiding every NEW file
    under them from `git add -A` in the operator's own checkout, long after the
    run that wrote them. The old docstring's defense — that an exclude "does not
    affect already-tracked files" — is precisely what made it invisible: the
    tracked files kept diffing normally while their new siblings vanished. So a
    refusal below SKIPS the shield and reports; falling back to the shared file
    would reintroduce the bug this function exists to fix.

    The price is a permanent one: `extensions.worktreeConfig` is repo-wide format
    state, enabled once and never removed here, and git versions older than 2.20
    refuse to access a repository carrying it (git-worktree(1)). See
    `_shield_enable_worktree_config` for the two shapes where enabling it is
    refused outright. It is paid at the LAST possible moment, so no degrade above it
    can charge it: that function probes, and the write itself happens here, one line
    above the activation. Enabling it up front — where it used to be — charged the operator's
    repo that permanent price on every path that then degraded away without
    shielding anything. Moving it down leaves exactly two writes able to set it
    without shielding anything — the enable itself and the activation — and each
    classifies both of the ways a chokepoint call can fail (a non-zero rc and a
    raise). The ACTIVATION rolls the flag back on either shape, and on a third
    outcome that is not a failure at all — a write that succeeds without taking
    effect, which a later round showed is reachable whenever the environment
    supplies the same key at `command` scope. The ENABLE rolls it
    back on the raise ONLY, because an rc from that write is git declining to make
    it: nothing was written, so there is nothing to undo, and undoing it anyway
    would unset a concurrent writer's flag. That asymmetry is load-bearing, not an
    unfinished enumeration. Getting here took five review rounds, because the early
    attempts enumerated failure shapes instead of funnelling them, the funnel was
    then applied to the activation but not to the enable one line above it, and even
    a complete funnel over the ways a CALL can fail said nothing about whether the
    call achieved what it was for. Rollback
    only when this call is what set the flag, and only when no other worktree's
    `config.worktree` depends on it; see `_shield_undo_extension`.

    CONCURRENCY — the probe→enable→activate→rollback sequence runs under an exclusive
    `file_lock` on `<common dir>/bmad-loop-shield.lock`, because that flag is the one
    piece of state two simultaneous runs in one repository share (round 8). Without
    it both probe an absent flag, both believe they own it, and a failed activation
    in one rolls the flag back out from under the other's live shield. The lock is
    per REPOSITORY, not per worktree, and it leaves a residue: a zero-length file in
    `.git/` — never in the working tree, so nothing the operator's `git add -A` can
    see, and nothing that needs a stale-lock scheme, since the kernel drops the lock
    when a holder dies. Its wait is platform-asymmetric by inheritance: POSIX `flock`
    blocks indefinitely (the transaction is ~7 git spawns, each bounded by
    `limits.git_timeout_s`), while `msvcrt.locking` gives up after ~10 s and raises
    `OSError`, which this function reports as a skipped shield naming the lock.

    Every git call goes through `verify.git_bytes`, the chokepoint accessor whose
    two properties this function is built on: the returncode comes back as an
    ANSWER rather than a raised fault (`config --get` of an unset key exits 1, and
    that reply is what tells the shield to enable the extension or fall through to
    the XDG default), and stdout arrives as BYTES, since POSIX filenames are bytes
    and a strict decode would raise `UnicodeDecodeError` out of the silent arm
    below, which promises never to propagate (#374). Standing inside it also buys
    the `LC_ALL=C` pin and the `limits.git_timeout_s` bound the engine configures.

    Best-effort in two arms that must stay separate, because a fault means the
    opposite thing in each:

    - git unqueryable BEFORE GIT HAS ANSWERED AT ALL (not a repo, git missing, a
      timeout or spawn failure on the first `rev-parse` — both `GitError`) is an
      EXPECTED skip — returns None silently. That scope is the whole of it, and the
      qualifier is load-bearing: read loosely as "any `GitError` is silent" it
      licensed the swallow round 5 deleted from `_shield_inherited_excludes`, which
      is BELOW this arm and therefore in the one below. Callers hand this
      plain temp dirs routinely. Nothing is DECODED in this arm: git's stdout is
      captured as bytes and decoded in the tail below, because a decode fault is a
      degrade (git answered; we could not read it), not the silent skip this arm
      means (#374). A git too old for a flag is NOT here: rev-parse echoes an
      option it does not know and exits 0, so that lands in the arm below, via
      the version refusal in `_shield_enable_worktree_config`.
    - anything AFTER git answered degrades to a returned reason string the caller
      can surface: the `--git-common-dir` probe failing once `--absolute-git-dir`
      has already answered (round 11 — it sat in the silent arm above from round 3,
      when one combined `rev-parse` became two calls, until this list was read as
      the specification it is), a main checkout passed in (nothing to scope to), a
      repository configured to be shared between OS users (refused above the lock —
      see `_shield_shared_repository`), a
      shield lock that could not be taken (contention on Windows, or an `os.open`
      fault in the common dir), a refused or
      failed extension enable, a failed activation, `.resolve()` (pre-3.13 a
      symlink loop raises RuntimeError, not OSError), `mkdir`, read/write
      `OSError` — including the operator's own excludes file existing but being
      unreadable, which must NOT be silent, because activating over patterns that
      could not be copied shadows them — a later git call timing out or failing to
      spawn (`GitError`), and a `UnicodeError` (below). Since round 5 that
      `GitError` case explicitly covers a git that will not say WHICH excludes file
      applies: not knowing whether there is anything to copy is the same standing as
      knowing there is and failing to read it, because the activation shadows it
      either way. Only a definite ABSENT answer stays silent. Silence here is not
      cosmetic: without the exclude the unit's `git add -A` commits the tool files
      this provisioning just wrote into the story's merge.

    The exclude PAYLOAD is bytes end-to-end — read, dedupe and write — so nothing
    on this path decodes or encodes an exclude file at all. That leaves two real
    sources for the `UnicodeError` in the tail's tuple, neither of them a
    file-content codec fault: the `os.fsdecode` of git's own stdout, which can raise
    on Windows only (#374/#377), and `os.fsencode` of a caller's pattern when that
    pattern carries a surrogate POSIX surrogateescape never produced — a
    hand-authored `"\\ud800"` in a config string rather than a real filename, which
    fsencode's surrogateescape rejects. The seed_globs case the tuple was originally
    written for is GONE: a pattern built from a genuinely non-UTF-8 filename arrives
    surrogate-escaped and `os.fsencode` returns its exact original bytes (POSIX);
    Windows' utf-8/surrogatepass encodes such a name to WTF-8 without raising.
    """
    # Callers pass POSIX-slash patterns (glob rels via as_posix; config strings as
    # authored); git's exclude is POSIX-slash on every platform, so nothing to fix here.
    try:
        # ONE path per call, never both in one answer. `rev-parse` separates its
        # answers with a newline, which is a legal BYTE IN A POSIX PATH — asking
        # for both at once made the split of a repo directory carrying one produce
        # four "paths" instead of two, and the length check below then degraded the
        # shield away *after* provisioning had copied the very files it exists to
        # hide (verified, git 2.55: the newlines come back raw, unquoted). No
        # NUL-delimited mode to reach for instead — `rev-parse` has no `-z`; it
        # echoes one back as a literal argument. A path that ENDS in a newline is
        # still beyond this parse, and beyond rev-parse's output format itself.
        answered = git_bytes(worktree, "rev-parse", "--absolute-git-dir")
        if answered.returncode != 0:
            # Not a repo (rc 128) — the expected skip the previous `check=True` raised
            # its way into. NOT "a git too old for `--absolute-git-dir`", which this
            # comment used to also claim: rev-parse ECHOES an option it does not know
            # and exits 0 (measured), so a git predating the flag (2.13) answers with
            # the flag's own name. That lands in the git 2.20 refusal in
            # `_shield_enable_worktree_config`, which returns above any write — a
            # degrade, not this silent skip.
            return None
    except GitError:
        # GitError alone, and it is not a narrowing: this `try` wraps ONE git call
        # and nothing else that can raise, and every fault the chokepoint raises out
        # of it is a GitError — a timeout directly, a spawn failure as
        # GitSpawnError, which subclasses it. The `OSError` that used to belong here
        # was the spawn fault's raw form and is now translated before it arrives.
        return None
    try:
        # The COMMON-DIR probe lives in THIS try, not the silent one above, and that
        # placement IS the fix (round 11). Once `--absolute-git-dir` has answered,
        # git has identified the repository, so every later fault is on the degrade
        # side of the two-arm taxonomy — which the silent arm's own docstring already
        # said ("a timeout or spawn failure on the FIRST rev-parse ... that scope is
        # the whole of it, and the qualifier is load-bearing"), and which
        # `worktree_flow`'s contract says too ("any fault after that ... is reported
        # to on_degraded rather than swallowed"). Round 3 split one combined
        # `rev-parse` into two calls to survive a newline in a repo path, and the
        # second call inherited the first's silent arm — leaving a timeout on it to
        # skip the shield with NO reason, after provisioning had already copied the
        # files the shield exists to hide.
        #
        # Both failure shapes are handled without an `except` of its own, per round
        # 7's funnel lesson: a non-zero rc returns the specific reason below, and a
        # raise is caught by this try's tail, which reports a reason too. The tail
        # also catches the `UnicodeError` this `fsdecode` of git's stderr can raise
        # on Windows — the same defect #394 records at a sibling block, so the stderr
        # read is deliberately INSIDE a guarded region rather than above one.
        shared_answer = git_bytes(worktree, "rev-parse", "--git-common-dir")
        if shared_answer.returncode != 0:
            detail = (
                os.fsdecode(shared_answer.stderr).strip()
                or f"git exited {shared_answer.returncode}"
            )
            return (
                f"skipped the git-add shield ({worktree}): git identified the "
                f"repository but could not name its common dir: {detail} — the "
                "provisioned tool files are not shielded from the unit's `git add -A`"
            )
        # fsdecode, so a non-UTF-8 repo path round-trips back to the filesystem
        # instead of faulting. It can still raise on Windows (utf-8/surrogatepass
        # rejects a lone invalid byte), which is why it sits inside this try —
        # defensive placement rather than a path under test: the regression test
        # is POSIX-only because git on Windows has no such path to hand back.
        #
        # removesuffix("\n"), NOT strip() — rev-parse terminates its answer with one
        # newline and every other byte belongs to the path (#384, Codex round 15).
        #
        # This comment used to argue strip() was harmless because "a final component
        # is `.git` or `worktrees/<id>`". That is true for a NON-bare repo and false
        # for a bare one, whose common dir IS the repository directory. Measured at
        # 2.55.0, a worktree of a bare repo at `…/common ` answers
        # `--git-common-dir` = `…/common ` — trailing space and all — while
        # `--absolute-git-dir` stays safe at `…/common /worktrees/<id>`, since git
        # sanitizes the admin id (round 5). So exactly one of the two answers was
        # exposed, and stripping it pointed every later step at `…/common`, which does
        # not exist: `--file <that>/config` reads rc 1, i.e. ABSENT (round 10), so the
        # `core.bare = true` refusal was MISSED and the shield went on to make the
        # permanent format change on a bare repository. The lock's own
        # `mkdir(parents=True)` then CREATED the stripped directory as a side effect.
        # A safety gate defeated by whitespace in a path, which is why this is the
        # parse and not a cosmetic.
        #
        # The dropped CRLF absorption is deliberate: git writes LF here on every
        # platform, and a path legitimately ending in `\r` is indistinguishable from a
        # CRLF terminator — so guessing would trade this bug for a rarer one. Windows
        # CI is the oracle: were git to emit CRLF there, every shield test would fail
        # on the malformed path rather than degrade quietly.
        raw_git_dir = os.fsdecode(answered.stdout).removesuffix("\n")
        raw_common = os.fsdecode(shared_answer.stdout).removesuffix("\n")
        if not raw_git_dir or not raw_common:
            # Defensive, and deliberately still a REASON rather than a silent skip:
            # git answered, so the two-arm taxonomy in the docstring puts an
            # unreadable answer on the degrade side. Unreachable with real git — an
            # rc of 0 from `rev-parse` always prints a path — which is the same
            # standing the fsdecode placement above has.
            return (
                f"could not read this worktree's git dirs ({worktree}): "
                f"{raw_git_dir!r}, {raw_common!r}"
            )
        git_dir = Path(raw_git_dir)
        common_dir = Path(raw_common)
        if not common_dir.is_absolute():
            # a PLAIN checkout answers with a relative ".git"; a linked worktree
            # answers with the main repo's absolute .git (verified, git 2.55).
            common_dir = worktree / common_dir
        # resolve BOTH before comparing: --absolute-git-dir is absolute but not
        # canonical, so a symlinked repo path would otherwise read as two
        # different dirs and a main checkout would pass for a linked worktree.
        git_dir, common_dir = git_dir.resolve(), common_dir.resolve()
        if git_dir == common_dir:
            # The main checkout itself: its gitdir IS the common dir, so the only
            # exclude file to write would be the shared one — the #384 bug.
            # Nothing here is transient enough to shield, so say so and stop.
            return (
                f"skipped the git-add shield ({worktree}): not a linked worktree, and the "
                "shield never writes the repository-wide exclude (#384)"
            )
        # ABOVE THE LOCK DELIBERATELY, and the placement is the whole value of this
        # gate: a repository shared between OS users is refused before anything is
        # created in it, so every user gets this same reason rather than the second
        # one meeting a lock file the first left behind. Safe above the lock because
        # `core.sharedRepository` is static config the tool never writes — unlike
        # `extensions.worktreeConfig` there is no shared mutable state to serialize.
        # It is one deletion point if shared repositories are ever supported for real.
        # A `GitError` from the read lands in this try's tail, which returns a reason.
        shared_repository = _shield_shared_repository(worktree, common_dir)
        if shared_repository is not None:
            return shared_repository
        # EVERYTHING BELOW IS ONE TRANSACTION, serialized per repository — round 8
        # (#384, Codex P2). `_shield_enable_worktree_config` PROBES the flag and the
        # enable further down is an idempotent no-op when it is already `true`, so two
        # concurrent provisionings in one repo both read an absent flag, both believe
        # they enabled it, and whichever one's activation then fails rolls the flag
        # back out from under the other's LIVE shield — git stops reading its
        # `config.worktree`, its `core.excludesFile` goes inert, and its shielded tool
        # files become stageable again mid-run. Nothing else collides first: every
        # other per-run name is keyed on the run id (worktree path, branch, tmux
        # session, run dir), so the flag is the one piece of state two runs share.
        # `cmd_run` has no liveness check and the TUI's warning modal offers "launch
        # anyway", so concurrent runs against one repo are permitted by design.
        #
        # The lock also removes a benign second consequence: `git config` writes take
        # `.git/config.lock`, so a concurrent writer got an rc≠0 the enable read as a
        # degrade and skipped the shield for that unit.
        #
        # It spans the probe→enable→activate→rollback sequence and cannot be narrowed
        # to just those: the enable must stay immediately above the activation (round
        # 5), the private exclude must exist before the activation, and the version +
        # `core.bare`/`core.worktree` gates inside the probe must stay above the seed
        # read, which depends on the same `--type=` floor. So the file write sits in
        # the middle of the locked span rather than outside it.
        #
        # The acquisition's own `OSError` is caught HERE rather than left to the tail,
        # only so the operator is told which step failed: POSIX `flock` blocks
        # indefinitely, but `msvcrt.locking` bounds the wait at ~10 s and then raises,
        # so on Windows contention is a real, reportable outcome. Behaviorally the
        # tail would also return a reason — this arm names the lock in it.
        held = ExitStack()
        try:
            held.enter_context(file_lock(common_dir / _SHIELD_LOCK_NAME))
        except OSError as e:
            return (
                f"skipped the git-add shield ({worktree}): could not take this "
                f"repository's shield lock ({e}) — another bmad-loop run may hold it"
            )
        with held:
            refusal, needs_enable = _shield_enable_worktree_config(worktree, common_dir)
            if refusal is not None:
                return refusal
            exclude = git_dir / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            # Zero bytes is an INTERRUPTED creation, not an initialized exclude. The
            # `touch()` below creates the target before `atomic_write_bytes` fills it, and
            # a fault in between (ENOSPC, or a kill) leaves that placeholder behind —
            # harmless on its own, since the degrade arm returns above the activation and
            # an unactivated private exclude does nothing. The harm would be deferred:
            # read as authoritative, it skips the seed below and then points a shadowing
            # `core.excludesFile` at a file missing the operator's own excludes,
            # un-ignoring inside the worktree everything they ignore globally. Size
            # rather than unlinking the placeholder on the way out, because no handler
            # runs when the process is KILLED in that window. Re-seeding costs nothing
            # either way: git treats an empty exclude and an absent one alike.
            existed = exclude.is_file() and exclude.stat().st_size > 0
            # On creation the operator's own excludes are copied in, because the
            # activation below shadows them (see _shield_inherited_excludes). On a
            # re-provision a non-empty file's content is authoritative and the copy is
            # not repeated, so the two stay idempotent together.
            #
            # A COPY FAULT SKIPS THE ACTIVATION. `_shield_inherited_excludes` raises on
            # anything but a definite "absent" — an unreadable file, and since round 5 a
            # git that will not say which file applies — and the raise lands in this
            # function's tail, which returns a reason. That is the whole point of it
            # raising rather than returning empty: an empty seed plus the activation
            # below is not "the seed was lost", it is the operator's global ignores
            # SHADOWED inside this worktree, and `git add -A` staging what they told git
            # to ignore. There is nothing to clean up on that path — the raise is above
            # `exclude.touch()`, so no placeholder is left for a later provisioning to
            # read as authoritative, and the `mkdir` above leaves an inert empty `info/`
            # that dies with the worktree.
            #
            # BYTES on both sides of that choice, and never decoded text: an exclude
            # file holds path patterns, POSIX paths are arbitrary bytes, and a legacy
            # 8-bit encoding is a perfectly ordinary thing for an operator's own file to
            # be in. `bytes.splitlines()` is also the git-CORRECT line split, not merely
            # the type-consistent one — `str.splitlines()` breaks on \x0b, \x0c, \x1c,
            # \x1d, \x1e and \x85, none of which git treats as a line boundary, so a
            # legitimate pattern containing one fragmented into wrong dedupe keys.
            existing = exclude.read_bytes() if existed else _shield_inherited_excludes(worktree)
            lines = existing.splitlines()
            # PRESENT IS NOT THE SAME AS EFFECTIVE (#384, Codex round 17). gitignore's
            # rule is LAST MATCH WINS, so a pattern this file already contains can be
            # cancelled by a `!` line below it — and a plain set-membership dedupe then
            # declined to append the shield's own pattern, leaving the provisioned tool
            # files stageable with NO degrade reason at all, because nothing failed.
            # Measured end-to-end through this function: seeded from an operator's
            # `core.excludesFile` holding `/.claude/skills` then `!/.claude/skills`,
            # `git status --porcelain -uall` in the worktree answers
            # `?? .claude/skills/tool.md` and `git add -A` stages it.
            #
            # So a pattern may be skipped only where it is already GUARANTEED effective:
            # it sits after the LAST negation in the file. That is airtight without
            # reimplementing git's matching — no later line can negate it, so the last
            # pattern matching any path it covers is either this one or a positive below
            # it, and both ignore. Anything earlier is appended instead, which is always
            # safe: the appended copy is last, so it is the one that decides.
            #
            # Deliberately CONSERVATIVE rather than exact. Measured, the only negation
            # that actually defeats the shield is one re-including the pattern's own
            # directory (`!.claude/skills` does it too — the negation need not repeat the
            # positive's spelling); a `!*.md` below it does NOT, since git never descends
            # into an excluded directory, and neither does `!/.claude`, since re-including
            # a parent leaves the child matched. Telling those apart is git's matcher, so
            # this appends a duplicate line in the harmless cases instead of guessing.
            #
            # `startswith(b"!")` with NO lstrip, which is git-correct twice over: git does
            # not strip leading whitespace from a pattern, and `\!` escapes a literal
            # leading `!` — both then read as positives here, as they do in git.
            last_negation = max(
                (i for i, line in enumerate(lines) if line.startswith(b"!")), default=-1
            )
            settled = set(lines[last_negation + 1 :])
            # fsencode, the inverse of the fsdecode above: a pattern derived from a real
            # filename round-trips to its exact original bytes. Both sides of the `in`
            # MUST be bytes — with `patterns` left as str every one of them reads as
            # absent and gets re-appended on every re-provision.
            wanted = [os.fsencode(p) for p in patterns]
            new = [p for p in wanted if p not in settled]
            # `not existed` keeps a re-provision that adds no pattern from leaving the
            # config below pointing at a file that was never created.
            if new or not existed:
                # b"\n", not "\n": `bytes.endswith(str)` is a TypeError, and TypeError is
                # not in the tail's except tuple — it would escape a function contracted
                # never to propagate.
                prefix = existing if not existing or existing.endswith(b"\n") else existing + b"\n"
                # atomic_write_bytes, never write_bytes onto `exclude` directly (#375).
                # write_bytes opens "wb", which TRUNCATES before writing, and this is a
                # read-modify-REWRITE carrying the operator's own excludes in
                # `prefix` — so a short write (ENOSPC) left the file truncated
                # mid-content while the degrade reason still said "could not update".
                # Worse, the surviving tail is a valid git pattern and a prefix of the
                # intended one: cut one char in and the last line is "/", which
                # excludes the entire worktree.
                #
                # The helper rather than a hand-rolled tmp+replace: it fsyncs before
                # the replace and carries the target's mode over, which a bare replace
                # resets.
                #
                # #381's interleaving race is mooted for NEW writes rather than fixed:
                # this file lives in one worktree's private gitdir, so the two runs
                # that used to race here — every linked worktree of a repo resolved to
                # the same shared common dir — no longer share a target at all. Only a
                # second provisioning of the SAME worktree can reach this file, which
                # the engine does serially. The atomic write stays for #375, whose
                # fault (a short write) needs no second writer.
                if not existed:
                    # Create it first, the way the write_text this replaced did:
                    # O_CREAT under the umask, giving the helper a mode to carry.
                    # BEHAVIOR PRESERVATION is the whole of the rationale (#375).
                    # `atomic_write_bytes` carries a mode over only when the target
                    # already EXISTS — its own docstring says a fresh one gets
                    # mkstemp's private 0600 — so dropping this line would narrow
                    # every private exclude's mode as an unannounced side effect of
                    # that refactor.
                    #
                    # The mode is load-bearing only where another OS user reads this
                    # gitdir, and that configuration no longer reaches here:
                    # `_shield_shared_repository` refuses a repository configured
                    # `core.sharedRepository` above the lock (#384). What keeps the
                    # line anyway is the SHAPE the mode's failure takes — measured,
                    # git treats an exclude it cannot read as one that is not there:
                    # it warns, exits 0, and `git add -A` stages the very files the
                    # exclude was written to shield, with no degrade reason at all
                    # because the write SUCCEEDED. Silence is the one outcome this
                    # function's docstring rules out, so a line that cannot produce
                    # it is worth its cost.
                    #
                    # Safe against the tail below: an empty exclude is equivalent to
                    # an absent one for git, so a fault after this leaves no more
                    # damage than the missing file it replaced.
                    exclude.touch()
                atomic_write_bytes(exclude, prefix + b"".join(p + b"\n" for p in new))
            # The PERMANENT repo-format change, deliberately the last-but-one thing
            # this function does. `_shield_enable_worktree_config` probed the gates
            # above and reported that the write is still owed; performing it there —
            # where it used to live — put a format change the operator's repo carries
            # forever AHEAD of the seed, the mkdir and the write, so every degrade
            # below it (including the `_shield_inherited_excludes` fault that round 5
            # turned from a swallow into a degrade) left the repo marked for a shield
            # that never applied. Nothing BETWEEN this write and the activation can
            # degrade — and that sentence used to stop right there, which is what
            # round 10 caught: "nothing between here and X" never covered the ENABLE
            # ITSELF, precisely as round 6 found it did not cover the activation
            # itself. This call has its own two failure shapes. So the paths that can
            # still set the flag without shielding anything are the enable's own raise
            # (handled immediately below) and the activation's failure (handled after
            # it), and each rolls the flag back.
            #
            # It cannot move BELOW the activation: `git config --worktree` is what the
            # extension unlocks, so the activation fatals without it.
            if needs_enable:
                # BOTH of the chokepoint's failure shapes, classified into one value
                # before anything is decided — round 7's funnel, applied to the one
                # call that never received it (#384, Codex round 10). Handling only
                # the rc left a raise (a timeout, a spawn failure) to the tail, which
                # cannot roll anything back: `git config` can be killed AFTER it has
                # renamed the new config into place, so the flag survived with no
                # shield ever activated.
                #
                # THE ROLLBACK FIRES ON THE RAISE ONLY, and that asymmetry is
                # deliberate — it is NOT the enumeration defect it resembles, so do
                # not "finish" it by rolling back the rc too. Round 5's taxonomy draws
                # the line: an rc IS an answer. git refused, which means the lock file
                # was never renamed into place and there is nothing to undo; the
                # likeliest cause is `.git/config.lock` contention with a concurrent
                # writer, and that writer is exactly the party whose flag an `--unset`
                # would then remove. A raise is NOT an answer — we did not find out —
                # and only the uncertain case may trigger repair. Both shapes are
                # still CLASSIFIED in one place, which is the part round 7 was about.
                #
                # `needs_enable` is True on this branch, so `_shield_undo_extension`'s
                # caller-side gate is satisfied by construction; the callee applies
                # its own sibling-ownership gate on top.
                rolled_back = ""
                try:
                    enabled = git_bytes(worktree, "config", "extensions.worktreeConfig", "true")
                    enable_fault = (
                        None
                        if enabled.returncode == 0
                        else os.fsdecode(enabled.stderr).strip()
                        or f"git exited {enabled.returncode}"
                    )
                except GitError as e:
                    enable_fault = str(e)
                    rolled_back = _shield_undo_extension(worktree, git_dir, common_dir)
                if enable_fault is not None:
                    return (
                        f"could not enable extensions.worktreeConfig ({worktree}): "
                        f"{enable_fault} — the provisioned tool files are not shielded "
                        f"from the unit's `git add -A`{rolled_back}"
                    )
            # LAST, after the file it names exists: git reads core.excludesFile lazily,
            # but a config pointing at a file that a fault above left unwritten would
            # be a shield that silently excludes nothing while reporting a reason.
            # ONE rollback covering EVERY way this step can fail to leave a working
            # shield, and the shape of this arm is the lesson rather than an incidental
            # style choice. `git_bytes` can fail two ways — a non-zero rc (git refused)
            # and a RAISE (a timeout, or a spawn failure as its GitSpawnError subclass)
            # — and two consecutive rounds each fixed ONE of them, because each fix
            # ENUMERATED the failure it had been shown. Enumeration is what kept being
            # incomplete. So every shape converges on one `fault` string BEFORE
            # anything is decided.
            #
            # A later round then supplied the shape that sentence could not have
            # anticipated, because it is not a FAILURE at all: the write SUCCEEDS and
            # the shield still does not apply, since `command` scope outranks
            # `worktree` and git resolves the key from the most specific scope that
            # carries it. So the `try` now spans the write AND the verification that
            # it took effect, and rc-0 is no longer an exit from this block — see
            # `_shield_verify_activation`. The rule that survived all of it: ask what
            # the POST-CONDITION is and confirm it, rather than enumerating the ways
            # the call in front of you can go wrong.
            #
            # What that first paragraph used to add — "and there is a single place
            # where the flag can be rolled back" — was true of the failure SHAPES and
            # false of the WRITES: round 10 added a second rollback point at the
            # enable above, because rollback sites track the number of writes that can
            # fail, not the number of ways each one fails.
            #
            # The rollback exists because the enable one line above is a PERMANENT
            # repo-format change, and the guarantee this code owes (docstring, CHANGELOG,
            # docs/FEATURES.md) is that the flag outlives a failed shield only where a
            # rollback was declined or could not be made, and the reason says which.
            # That wording is round 10's: it used to read "survives only a shield that
            # actually activated", which the enable's own raise falsified. When a
            # fix's value proposition is a GUARANTEE, the same edit has to reach every
            # copy of it — these three, plus this function's own docstring.
            # `needs_enable` gates it — see `_shield_undo_extension` for why
            # that condition is a safety property and not a micro-optimization, and for
            # the second gate it applies itself: `needs_enable` records what the PROBE
            # saw, so it cannot tell "we enabled it" from "we and a concurrent run both
            # thought we did", and the callee declines when a sibling worktree's
            # `config.worktree` would stop being read.
            try:
                activated = git_bytes(
                    worktree, "config", "--worktree", "core.excludesFile", str(exclude)
                )
                fault = (
                    None
                    if activated.returncode == 0
                    else os.fsdecode(activated.stderr).strip()
                    or f"git exited {activated.returncode}"
                )
                if fault is None:
                    # An rc 0 here means the value was WRITTEN, not that it is in
                    # FORCE — `command` scope outranks `worktree` and is fed by the
                    # environment we were launched with. Verified rather than
                    # assumed, INSIDE this same `try` and above the one decision
                    # below, so the verification's own two failure shapes reach the
                    # existing rollback instead of growing a second one. See
                    # `_shield_verify_activation` for why this asks git what it
                    # resolved instead of looking for the override's origin.
                    fault = _shield_verify_activation(worktree, exclude)
            except GitError as e:
                # Deliberately NOT left to the tail: the tail cannot roll the flag back,
                # and a GitError from EITHER call in this block means exactly what a
                # refusal means — the shield is not in force. That now covers the
                # verification's own fault as well as the write's, which is the point of
                # putting it inside this `try` rather than after it. The tail's own
                # GitError guard stays load-bearing for the seed read above, which round
                # 5 turned into a degrade, so this is a narrowing of what reaches it,
                # not a bypass.
                fault = str(e)
            if fault is not None:
                if needs_enable:
                    fault += _shield_undo_extension(worktree, git_dir, common_dir)
                return f"could not activate the worktree git exclude ({worktree}): {fault}"
    # GitError is every fault a chokepoint git call can raise here — a timeout, or
    # a spawn failure as its GitSpawnError subclass. It replaces the
    # `subprocess.SubprocessError` this tuple used to carry for exactly that pair,
    # which the chokepoint now translates before it ever reaches this frame.
    #
    # UnicodeError, not UnicodeDecodeError, and NOT for the exclude file any more:
    # the payload is bytes end-to-end, so neither its read nor its write can raise a
    # codec fault at all. What is left for it is stated in the docstring — the
    # `os.fsdecode` of git's stdout (Windows-only) and `os.fsencode` of a pattern
    # carrying a surrogate that POSIX surrogateescape never produced. Both are
    # ENCODE-or-DECODE depending on which, hence the shared base class. Still far
    # short of ValueError-broad, so a programming error still escapes.
    #
    # TypeError is deliberately absent: a str/bytes mixup on the payload path is a
    # programming error, and letting it crash beats reporting it as an operator's
    # degraded shield once per run.
    except (GitError, OSError, RuntimeError, UnicodeError) as e:
        return f"could not update the worktree-local git exclude ({worktree}): {e}"
    return None


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
        skills_skipped = _copy_skills(project, trees, force_skills)

    # 4. policy template
    policy_path = bmad_loop_dir / "policy.toml"
    if policy_path.is_file():
        print("  policy exists, leaving untouched")
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
