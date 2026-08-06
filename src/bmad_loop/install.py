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

import errno
import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, NamedTuple

from .adapters.profile import ALIASES, CLIProfile, ProfileError, load_profiles
from .checks import Finding
from .platform_util import atomic_write_bytes, file_lock
from .policy import POLICY_TEMPLATE
from .process_host import get_process_host
from .verify import GitError, git_bytes, path_tracked

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
# `bmad-loop init` lays down. The inner dev primitive (`bmad-build-auto`, formerly
# `bmad-dev-auto`) is upstream (not bundled here): the orchestrator drives it as an
# already-installed skill.
MODULE_SKILLS = (
    "bmad-loop-resolve",
    "bmad-loop-sweep",
    "bmad-loop-setup",
)

# The inner dev primitive, in both upstream eras. BMAD-METHOD PR #2651 (shipped in
# bmad-method 6.10.1-next.33) renamed `bmad-dev-auto` -> `bmad-build-auto` and left a
# forwarding SHIM behind under the old name: a lone SKILL.md whose customization
# migration gate is INTERACTIVE, so an unattended session that dispatches to it can
# HALT having written nothing to disk — no spec, no result artifact, nothing the
# post-session verification can read. The orchestrator therefore never accepts the
# shim: it resolves the primitive on disk (:func:`resolve_dev_primitive`) and fails
# the preflight when only the shim is installed.
#
# The shim carries no step files and no customize.toml, so DEV_PRIMITIVE_MARKERS —
# which already pinned "a real, complete install" — doubles as the shim detector.
# Markers pin BOTH a step file (catches a truncated copy) AND customize.toml, the
# layer/handoff config step-04 resolves review_layers from (BMAD-METHOD
# #2535/#2550): a pre-July bmm install predating it would let every dev run's
# step-04 fail.
DEV_PRIMITIVE_NEW = "bmad-build-auto"
DEV_PRIMITIVE_LEGACY = "bmad-dev-auto"
DEV_PRIMITIVE_MARKERS = ("step-04-review.md", "customize.toml")

# The adapter roles whose skill tree is asked for the dev primitive and for the
# review layers that primitive invokes inline — i.e. every question this module's
# skill checks ask. Triage is deliberately absent: its only prompt is
# `/bmad-loop-sweep`, which is in MODULE_SKILLS and is laid into that tree by
# `bmad-loop init`, so a triage-only CLI never needs one byte of the bmm module.
# Gating it anyway makes `[adapter.triage] name = "gemini"` under a claude dev/review
# pair demand the whole module in `.agents/skills` — a hard preflight FAIL over a
# tree no session ever dispatches one of these skills into.
#
# This must stay the same set `WorktreeFlow.worktree_profiles` provisions, which
# reads this constant for exactly that reason. A tree gated here that no worktree
# carries refuses runs over a skill no session will ever read; a tree provisioned but
# not gated ships a session into the `Unknown command` stall the preflight exists to
# catch. Neither has a defensible reading, so the two move together or not at all.
DEV_PRIMITIVE_ROLES: tuple[str, ...] = ("dev", "review")

# Where project-level customization of an upstream skill lives. This main-native
# Path remains the single source; renderer strings derive their `_bmad` spelling
# from it so the install and validation surfaces cannot drift.
CUSTOMIZE_DIR = Path("_bmad") / "custom"

# BMAD's project-local configuration and tooling root. Renderer paths derive from
# this one spelling so the validate-side checks in this phase and the worktree seed
# added later cannot drift.
BMAD_DIR = CUSTOMIZE_DIR.parent.as_posix()

# Since BMAD-METHOD PR #2601 a skill's SKILL.md can be a renderer stub that shells
# out to this project-local script. Its absence makes the session HALT before the
# dev workflow can write a spec or result, so a content-confirmed stub makes this a
# preflight problem rather than a best-effort advisory.
RENDERER_SCRIPT_REL = f"{BMAD_DIR}/scripts/render_skill.py"
RENDERER_SCRIPT_MARKER = "render_skill.py"

# The renderer currently imports this sibling helper at module scope. Require it
# only when the installed script still names that import surface; a false positive
# would refuse every run and has no --force escape hatch.
RENDERER_CONFIG_UTILS_REL = f"{BMAD_DIR}/scripts/config_utils.py"
RENDERER_CONFIG_UTILS_MARKER = "config_utils"
RENDERER_SCRIPT_UNIT_REL: tuple[str, ...] = (
    RENDERER_SCRIPT_REL,
    RENDERER_CONFIG_UTILS_REL,
)

# The renderer's only required project-global config layer. This is deliberately
# not `_bmad/bmm/config.yaml`, which configures bmad-loop's artifact paths.
CENTRAL_CONFIG_REL = f"{BMAD_DIR}/config.toml"

# The renderer surface a worktree must carry, in the exact strings provisioning
# reports when it comes up short. Worktree seeding reports these two from dedicated
# completeness checks, and escalates only when a renderer stub is what resolved.
BMAD_SCRIPTS_SEED_REL = f"{BMAD_DIR}/scripts"
RENDERER_SEED_SENTINELS: tuple[str, ...] = (BMAD_SCRIPTS_SEED_REL, CENTRAL_CONFIG_REL)

# The skill-relative render entry plus snapshot-reference grammar. The regex is a
# byte-for-byte mirror of BMAD-METHOD main's render_skill.py `_SNAPSHOT_TOKEN` at
# 57e70562e3776b47bce1a54710f1edb1f0bd3618. Loosening it invents failures the
# renderer ignores; tightening it misses a real HALT.
RENDERER_ENTRY_REL = "workflow.md"
SNAPSHOT_TOKEN_RE = re.compile(r"\[\[bmad-snapshot:([A-Za-z0-9_./-]+\.md)\]\]")

# Renderer output is regenerated with checkout-absolute paths and must never be
# copied into a worktree or committed. BMAD_SEED_EXCLUDES skips it while seeding
# `_bmad/`; RENDER_DIR_REL is the one spelling init gitignores and validate checks.
RENDER_DIR_NAME = "render"
BMAD_SEED_EXCLUDES = (RENDER_DIR_NAME,)
RENDER_DIR_REL = f"{BMAD_DIR}/{RENDER_DIR_NAME}"

# Upstream skills the orchestrator invokes but does NOT bundle in the wheel — the
# BMad Method (bmm) module installs them. Each must exist in every ``trees`` entry —
# callers pass the DEV_PRIMITIVE_ROLES trees — and carry its marker files (a
# half-installed or pre-automation skill is caught by the `bmad-loop validate`
# preflight). `{skill: (marker-rel-path, ...)}`.
#   - the dev primitive — always required, and never substitutable. Its entry is
#     keyed on the LEGACY name because this map doubles as the "lay down a pre-rename
#     install" catalog; missing_base_skills does NOT walk it for the primitive, it
#     resolves the installed name per tree first (resolve_dev_primitive).
#   - the two review hunters v6.10.0 ships. These are only the FALLBACK review
#     requirement, used when the installed skill's shape can't be read: normally the
#     reviewers are derived per tree from the dev primitive itself
#     (resolve_review_layers), because which skills the review step invokes is a
#     property of that skill version, not of a catalog pinned in here (#260).
# bmad-review-verification-gap is not in the fallback set: no tagged BMAD-METHOD
# release ships it standalone (on current sources it is a thin forwarder to the
# merged bmad-review), so demanding it of every project made `validate`
# unsatisfiable on real installs. A project whose review layers DO name it still has
# it required — by derivation from its own config, not by this list.
DEV_BASE_SKILLS = {
    DEV_PRIMITIVE_LEGACY: DEV_PRIMITIVE_MARKERS,
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
# BOTH primitive eras are listed: a worktree must carry whichever one the main
# checkout has, and copy-if-present makes naming both free. Adding the new name here
# is what keeps isolation working across the rename — provisioning unions this
# catalog with the resolved review layers, and the layers never name the primitive.
BASE_SKILLS = {
    DEV_PRIMITIVE_NEW: DEV_PRIMITIVE_MARKERS,
    **DEV_BASE_SKILLS,
    "bmad-review-verification-gap": (),
    MERGED_REVIEW_SKILL: (),
}

# How the dev primitive names a skill it hands a review off to, in both shapes:
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


def _customize_overrides(skill: str) -> tuple[Path, ...]:
    """Project overrides of ``skill``'s shipped customize.toml, in precedence order
    (later wins), per customize.toml's own header. The `.user.toml` layer is personal
    and gitignored by the upstream installer.

    Takes the RESOLVED primitive name and reads that pair ONLY — never both eras.
    Upstream's own resolver keys on the skill directory, so on a renamed project the
    legacy pair is simply not read at run time; merging it in here would make the
    preflight resolve layers the session never applies, which is the exact
    disagreement :func:`resolve_review_layers` exists to prevent. The orphaned file
    is surfaced instead, as the ``skills.customize-legacy`` warning in
    :func:`dev_primitive_warnings`.
    """
    return (
        CUSTOMIZE_DIR / f"{skill}.toml",
        CUSTOMIZE_DIR / f"{skill}.user.toml",
    )


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
    if _is_file(project / tree / DEV_PRIMITIVE_NEW / "SKILL.md"):
        return DEV_PRIMITIVE_NEW
    legacy = project / tree / DEV_PRIMITIVE_LEGACY
    if _is_file(legacy / "SKILL.md") and all(
        _is_file(legacy / marker) for marker in DEV_PRIMITIVE_MARKERS
    ):
        return DEV_PRIMITIVE_LEGACY
    return None


def _is_dev_primitive_shim(project: Path, tree: str) -> bool:
    """True when ``tree`` holds a legacy-named skill that is only a forwarding shim
    (SKILL.md present, at least one marker absent). Selects the failure *message* in
    :func:`missing_base_skills`; it is never a resolution input."""
    legacy = project / tree / DEV_PRIMITIVE_LEGACY
    if not (legacy / "SKILL.md").is_file():
        return False
    return any(not (legacy / marker).is_file() for marker in DEV_PRIMITIVE_MARKERS)


def _is_renderer_stub(skill_dir: Path) -> bool:
    """Whether ``skill_dir`` delegates to the project renderer.

    Content, not the skill's era/name, is the discriminator. An unreadable or
    non-UTF-8 SKILL.md cannot prove the renderer is involved, so this fails open;
    the ordinary marker checks still report the damaged skill tree.
    """
    try:
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return RENDERER_SCRIPT_MARKER in skill_md


def _renderer_unit_required(project: Path, rel: str) -> bool:
    """Whether the installed renderer requires project-relative unit member ``rel``.

    The entry point is unconditional. The sibling helper is content-keyed because
    it is required only while ``render_skill.py`` imports it; this preflight has no
    force override, so the installed script is the honest compatibility boundary.
    An absent/unreadable script already earns the entry-point finding and does not
    also fabricate a helper diagnosis.
    """
    if rel != RENDERER_CONFIG_UTILS_REL:
        return True
    try:
        script = (project / RENDERER_SCRIPT_REL).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return RENDERER_CONFIG_UTILS_MARKER in script


def _absent_renderer_sources(skill_dir: Path) -> list[str]:
    """Required renderer sources absent from ``skill_dir``, as POSIX rels.

    This validates the two statically decidable source HALTs: the hard-coded
    ``workflow.md`` entry and any ``[[bmad-snapshot:...]]`` target declared by a
    Markdown source. Missing config-token values are deliberately outside this
    presence-only check (#407).

    The era guard lives here: an inline pre-renderer SKILL.md legitimately has no
    workflow entry, so asking it renderer questions would refuse a healthy install.

    The ``rglob`` walk deliberately mirrors upstream
    ``render_skill.py::_load_sources`` and must not be unified with
    :func:`_copy_traversable`'s ``iterdir`` recursion. ``rglob`` does not descend a
    symlinked sub-directory; the copier does. Using the copier's enumeration here
    would invent sources the renderer never loads and turn a guaranteed HALT green.

    Upstream keys nested sources by POSIX relative path and excludes every basename
    ``SKILL.md``. :func:`_is_file` enforces filesystem totality, including the FIFO
    guard, before any source is read. Remaining read/decode faults fail open because
    they have different remediation from an absent declared source.
    """
    if not _is_renderer_stub(skill_dir):
        return []
    sources = {
        path.relative_to(skill_dir).as_posix(): path
        for path in skill_dir.rglob("*.md")
        if path.name != "SKILL.md" and _is_file(path)
    }
    absent = [] if RENDERER_ENTRY_REL in sources else [RENDERER_ENTRY_REL]
    for path in sources.values():
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        absent.extend(target for target in SNAPSHOT_TOKEN_RE.findall(body) if target not in sources)
    return sorted(set(absent))


def renderer_stub_resolved(project: Path, trees: Sequence[str]) -> bool:
    """Whether any active tree resolves to a renderer-backed dev primitive.

    Resolution is per tree and deduplicated exactly like :func:`missing_base_skills`.
    Public because the engine's seed-escalation decision reuses this exact preflight
    discriminator rather than re-deriving one (`worktree_flow`).
    """
    for tree in dict.fromkeys(trees):
        resolved = resolve_dev_primitive(project, tree)
        if resolved is not None and _is_renderer_stub(project / tree / resolved):
            return True
    return False


def dev_primitive_or_default(project: Path, tree: str | None) -> str:
    """Total form of :func:`resolve_dev_primitive` for callers that need a name.

    A prompt string — and a path this module has to probe *somewhere* to report on —
    always has to name something, and the preflight has already refused the
    unresolvable cases before any session is spawned. So an unresolvable tree (and a
    None tree, which is what an adapter with no profile reports) falls back to the
    legacy name rather than raising into prompt construction."""
    if tree is None:
        return DEV_PRIMITIVE_LEGACY
    return resolve_dev_primitive(project, tree) or DEV_PRIMITIVE_LEGACY


class ReviewResolution(NamedTuple):
    """Which skills the installed dev primitive's review step actually invokes.

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
    project: Path, tree: str, skill: str
) -> tuple[list[Any], tuple[str, ...]] | None:
    """The skill's shipped review layers with project overrides applied.

    ``skill`` is the resolved dev-primitive name — the shipped config and its
    overrides are read from the SAME era, because that is what the run does.

    Returns ``(layers, unreadable_override_paths)``, or None when the skill's OWN
    customize.toml is absent or unparseable — the one case that genuinely means
    "shape unknown", so the caller falls back to the static catalog.

    An unparseable OVERRIDE is not that case: BMAD's resolver warns and treats it
    as empty, still resolving every other layer. Matching that keeps the preflight
    agreeing with the run; the broken file is reported separately as a warning.
    """
    data = _read_toml(project / tree / skill / "customize.toml")
    if data is None:
        return None
    layers = _layers_of(data)
    unreadable: list[str] = []
    for rel in _customize_overrides(skill):
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

    The primitive's installed NAME is resolved from disk here rather than taken as
    an argument (:func:`dev_primitive_or_default`): both call sites — the preflight
    and worktree provisioning — hold exactly ``(project, tree)`` and would otherwise
    each compute the same value to hand back. On a renamed project the legacy paths
    are simply absent, so without this the resolution silently returns None and the
    caller degrades to the static catalog — quietly seeding a worktree with the
    wrong reviewers instead of the ones this project configured.

    Post-consolidation the primitive is layer-driven: each
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
    # `primitive`, not `skill`: the layer loop below binds `skill` to each INVOKED
    # review skill, and the step-04 fallback after it reads this name as a directory.
    # Sharing one name is safe only while the layer branch returns first — a
    # shadowing this function should not be one edit away from.
    primitive = dev_primitive_or_default(project, tree)
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


def missing_stories_support(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Problems for stories mode's stricter dev-primitive requirement.

    Sprint mode drives any dev primitive; stories mode needs the folder+id
    dispatch flow, which older skill versions lack. For every ``trees`` entry —
    callers pass the :data:`DEV_PRIMITIVE_ROLES` trees, since only a dev or review
    session ever dispatches one of these — confirm
    ``<resolved-primitive>/step-01-clarify-and-route.md`` exists and carries the
    dispatch-protocol marker. Returns one problem :class:`Finding` per tree lacking
    it (empty = OK). Callers gate this on stories mode only — sprint-mode runs must
    not require the newer skill.

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
    the review layers its step-04 invokes inline are installed by the BMad Method
    module, not by `bmad-loop init`. Each must exist in every ``trees`` entry —
    callers pass the :data:`DEV_PRIMITIVE_ROLES` trees, since only a dev or review
    session ever dispatches one of these — and carry its marker files. Returns one
    :class:`Finding` per missing/incomplete skill; empty list means OK. Run as a
    preflight so a missing skill fails loudly with remediation instead of stalling
    as an `Unknown command` until the run times out.

    Not every finding is fatal: review layers that are conditional, ambiguously
    phrased, or configured by an unparseable override come back as ``warning``
    (see :func:`_review_findings`). **Callers must branch on severity** — treating
    a non-empty return as failure turns every advisory into a blocked run.

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

    The review skills are read from the installed primitive itself
    (:func:`resolve_review_layers`) so the preflight requires what this project
    will really invoke: a tree whose configured layers call the merged
    ``bmad-review`` needs that skill and not the standalone hunters, and a tree on
    the pre-consolidation step-04 needs the two hunters it names. When the shape
    can't be read we fall back to the static catalog, with a present
    ``bmad-review`` satisfying the hunters. Everything is per tree, since a project
    can have a post-consolidation `.claude` tree and a pre-merge `.agents` one side
    by side — and, across the rename, a different primitive era in each.

    ``skills.base-incomplete`` carries ``missing_markers`` as a list — the message
    joins it with ", " for the human line, which a consumer would otherwise have to
    split back apart on a separator the message is free to change.

    A resolved renderer stub is also checked at the project-global and
    skill-relative surfaces whose absence deterministically HALTs before the
    workflow can write its contract:

    - ``skills.dev-renderer``: a required project-relative renderer script-unit
      member is absent (``missing_scripts`` detail, per tree);
    - ``skills.dev-renderer-config``: the project-global required config is absent
      (emitted once even when multiple trees resolve a stub);
    - ``skills.dev-renderer-sources``: the skill-relative workflow entry or a
      declared snapshot source is absent (``missing_sources`` detail, per tree).

    These are independent of the ordinary marker check: a damaged primitive may
    need both remediations. All are content-gated on the installed SKILL.md, so an
    inline pre-renderer install sees byte-stable behavior.
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
                    f"{tree}/{DEV_PRIMITIVE_NEW} not found — the orchestrator drives this "
                    f"upstream dev primitive directly; it ships with the bmm module "
                    f"(BMAD-METHOD >= 6.10.0); install or update bmm in this project "
                    f"(older installs name it {DEV_PRIMITIVE_LEGACY})",
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
                        f"{tree}/{resolved} is incomplete (missing "
                        f"{', '.join(absent)}) — reinstall it from the bmm module",
                        {"tree": tree, "skill": resolved, "missing_markers": absent},
                    )
                )
            if _is_renderer_stub(skill_dir):
                resolved_stub = True
                absent_scripts = [
                    rel
                    for rel in RENDERER_SCRIPT_UNIT_REL
                    if _renderer_unit_required(project, rel) and not (project / rel).is_file()
                ]
                if absent_scripts:
                    problems.append(
                        Finding(
                            "skills.dev-renderer",
                            "problem",
                            f"{tree}/{resolved}/SKILL.md renders via "
                            f"{RENDERER_SCRIPT_MARKER} but the renderer script unit is "
                            f"incomplete (missing {', '.join(absent_scripts)}) — the "
                            "session would HALT without writing a spec; reinstall the "
                            "BMad Method (bmm) module",
                            {
                                "tree": tree,
                                "skill": resolved,
                                "missing_scripts": absent_scripts,
                            },
                        )
                    )
            absent_sources = _absent_renderer_sources(skill_dir)
            if absent_sources:
                problems.append(
                    Finding(
                        "skills.dev-renderer-sources",
                        "problem",
                        f"{tree}/{resolved} renders via {RENDERER_SCRIPT_MARKER} but its "
                        f"render sources are incomplete (unresolved: "
                        f"{', '.join(absent_sources)}) — the session would HALT without "
                        "writing a spec; reinstall the BMad Method (bmm) module",
                        {
                            "tree": tree,
                            "skill": resolved,
                            "missing_sources": absent_sources,
                        },
                    )
                )
        problems.extend(_review_findings(project, tree))
    if resolved_stub and not (project / CENTRAL_CONFIG_REL).is_file():
        problems.append(
            Finding(
                "skills.dev-renderer-config",
                "problem",
                f"the dev primitive renders via {RENDERER_SCRIPT_MARKER} but "
                f"{CENTRAL_CONFIG_REL} is missing — the renderer requires that layer "
                "and would HALT without writing a spec; reinstall the BMad Method "
                "(bmm) module",
                {"config": CENTRAL_CONFIG_REL},
            )
        )
    return problems


def dev_primitive_warnings(project: Path, trees: Sequence[str]) -> list[Finding]:
    """Advisory findings about a resolved dev primitive — validate-only, never a gate.

    One condition, and it is genuinely survivable — which is what keeps it out of
    :func:`missing_base_skills`:

    - ``skills.customize-legacy``: at least one tree resolved to the NEW name while a
      customization override still sits under the OLD one with no counterpart, i.e.
      the rename silently orphaned it. Emitted once per project (the override files
      are project-global, not per tree). The session still runs; it just runs
      unstyled, so naming it is an operator heads-up rather than a gate.

    ⚠️ The remediation is COPY, not rename, whenever another active tree still
    resolves to the legacy primitive. A project can sit mid-upgrade with a different
    era in each tree (`.claude/skills` on the new name, `.agents/skills` still on a
    marker-complete old one), and each tree resolves its overrides under its OWN
    era — so the legacy file is orphaned for the new tree and LIVE for the legacy
    one. Telling that operator to rename it moves the customization from one tree to
    the other instead of fixing anything. Suppressing the finding instead would be
    the opposite error: the new tree really is running unstyled, which is precisely
    the silent degradation this warning exists to surface.

    Deliberately a warning and not a problem. :func:`missing_base_skills` feeds a
    gate with no severity filter and no ``--force``: a false FAIL there pauses every
    run behind a remediation nobody can apply, so on these checks a false green is
    the safe direction. The orphaned file's layers really are inert for a new-era
    tree — upstream's resolver keys on the skill dir — so the honest response is to
    say so and name the rename, not to block.

    Returns [] when nothing resolves — :func:`missing_base_skills` owns that story.
    """
    findings: list[Finding] = []
    resolved = {tree: resolve_dev_primitive(project, tree) for tree in dict.fromkeys(trees)}
    new_trees = [tree for tree, name in resolved.items() if name == DEV_PRIMITIVE_NEW]
    legacy_trees = [tree for tree, name in resolved.items() if name == DEV_PRIMITIVE_LEGACY]
    if new_trees:
        orphaned = [
            (CUSTOMIZE_DIR / f"{DEV_PRIMITIVE_LEGACY}{suffix}").as_posix()
            for suffix in (".toml", ".user.toml")
            if (project / CUSTOMIZE_DIR / f"{DEV_PRIMITIVE_LEGACY}{suffix}").is_file()
            and not (project / CUSTOMIZE_DIR / f"{DEV_PRIMITIVE_NEW}{suffix}").is_file()
        ]
        if orphaned and legacy_trees:
            # Mixed-era project: the file is live for the legacy tree, so name the
            # tree it still styles and say copy. `legacy_trees` rides in `detail`
            # only on this branch, keeping the all-new detail dict unchanged.
            findings.append(
                Finding(
                    "skills.customize-legacy",
                    "warning",
                    f"{', '.join(orphaned)} does not apply in "
                    f"{', '.join(new_trees)} (resolved {DEV_PRIMITIVE_NEW}) but still "
                    f"applies in {', '.join(legacy_trees)} — COPY the override file(s) "
                    f"to the {DEV_PRIMITIVE_NEW} name; renaming would drop the "
                    f"customization from {', '.join(legacy_trees)}",
                    {
                        "files": orphaned,
                        "skill": DEV_PRIMITIVE_NEW,
                        "legacy_trees": legacy_trees,
                    },
                )
            )
        elif orphaned:
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


def _where_clause(layer_ids: Sequence[str]) -> str:
    """How a finding's message names the layers that reach for a skill."""
    if len(layer_ids) > 1:
        return f"review layers {', '.join(layer_ids)} invoke"
    if layer_ids:
        return f"review layer {layer_ids[0]} invokes"
    return "review step invokes"


def _review_findings(project: Path, tree: str) -> list[Finding]:
    """Findings for the review skills this tree's dev primitive invokes.

    Problems block; warnings never do. A skill is only a problem when the
    installed config says this run WILL invoke it — anything conditional or
    ambiguous warns instead, because a false FAIL here is #260 all over again.
    Callers must therefore honour severity rather than treating any finding as
    fatal.

    The primitive's name is resolved here for the MESSAGES — which tell an operator
    which skill dir and which override file to go and fix, so a hardcoded era would
    send them to a path that does not exist on a renamed project. The static-catalog
    fallback below has no :class:`ReviewResolution` to carry the name, so this
    resolves independently rather than widening that tuple.
    """
    primitive = dev_primitive_or_default(project, tree)
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


def _walk_traversable_files(
    src,
    rel: str = "",
    _seen: frozenset[str] = frozenset(),
    _should_descend=None,
    _visit_dir=None,
    _suppress_errors: bool = True,
) -> Iterator[tuple[str, Traversable]]:
    """Yield the leaves of a Traversable tree as deterministic POSIX rels.

    ``iterdir`` recursion deliberately descends symlinked source directories, unlike
    the renderer's ``rglob`` enumeration in :func:`_absent_renderer_sources`. A
    branch-local real-path set terminates cycles without dropping a second sibling
    that points at the same shared tree.

    A directory the filesystem refuses to list is yielded as a leaf. That lets a
    copier decline content it cannot confirm while a result-side completeness check
    can still name the unreadable rel. Wheel Traversables have no real-path cycle leg.

    ``_should_descend`` and ``_visit_dir`` are private copier plumbing. The first may
    prune before ``iterdir`` (preserving no-clobber when a destination file stands
    where the source has a directory); the second sees every successfully enumerated
    directory, including empty ones. Keeping materialization after enumeration means
    an unreadable source directory never leaves an empty destination behind.
    """
    if _is_dir(src):
        real = str(src.resolve()) if isinstance(src, Path) else None
        if real is not None and real in _seen:
            return
        if _should_descend is not None and not _should_descend(rel, src):
            return
        try:
            children = sorted(src.iterdir(), key=lambda entry: entry.name)
        except OSError:
            if not _suppress_errors:
                raise
            yield rel, src
            return
        if _visit_dir is not None and not _visit_dir(rel, src):
            return
        inner = _seen if real is None else _seen | {real}
        for child in children:
            child_rel = f"{rel}/{child.name}" if rel else child.name
            yield from _walk_traversable_files(
                child,
                child_rel,
                inner,
                _should_descend,
                _visit_dir,
                _suppress_errors,
            )
        return
    yield rel, src


def _is_file(path) -> bool:
    """Answer whether ``path`` is a usable file, total over filesystem faults.

    A refused probe and an absent/non-file path all mean the same thing to a reader
    or copier: there are no bytes it can promise to consume. Python <=3.13 raises on
    an entry below an unsearchable parent while 3.14 answers false; both fold to false
    here. The directory reading is deliberately different — see :func:`_is_dir`.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path) -> bool:
    """Answer whether a walk may have unseen content below ``path``.

    Refusal is true, not false: an unreadable source must reach the walk as a named
    leaf instead of collapsing into absence. Python 3.14 made ``Path.is_dir`` return
    false for faults older interpreters raise, so a false is re-asked with ``stat``.
    """
    try:
        if path.is_dir():
            return True
    except OSError:
        return True
    return _probe_refused(path)


# pathlib's version-dependent private ignored-error sets, copied here so absence and
# refusal retain the same meaning on every supported interpreter and platform.
_ABSENCE_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)
_ABSENCE_WINERRORS = (
    21,  # ERROR_NOT_READY: drive exists but is not accessible
    123,  # ERROR_INVALID_NAME
    1921,  # ERROR_CANT_RESOLVE_FILENAME: broken self-referential symlink
)


def _probe_refused(path) -> bool:
    """True when a false pathlib convenience probe actually means I/O refusal."""
    if not isinstance(path, Path):
        # A wheel Traversable has no stat() method and no permission mode to recover.
        return False
    try:
        path.stat()
    except OSError as exc:
        return (
            exc.errno not in _ABSENCE_ERRNOS
            and getattr(exc, "winerror", None) not in _ABSENCE_WINERRORS
        )
    except ValueError:
        # Embedded-null and otherwise invalid paths are absent, not refused.
        return False
    return False


def _occupied(path: Path) -> bool:
    """Whether a destination slot is taken, dangling symlinks included."""
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        # A slot that cannot even be inspected is not safe to write through.
        return True


def _copy_traversable(
    src,
    dst: Path,
    *,
    skip_existing: bool = False,
    worktree: Path | None = None,
    repo_root: Path | None = None,
) -> bool:
    """Recursively copy a Traversable tree, optionally confined to a worktree.

    ``skip_existing`` remains the install helper's opt-in per-file no-clobber mode.
    Supplying ``worktree`` makes no-clobber mandatory and adds destination
    containment plus per-entry OSError degradation: provisioning never escapes the
    worktree through a link or crashes the run because one entry cannot be read.
    ``repo_root`` additionally refuses real source entries resolving outside the main
    checkout. Wheel Traversables have no source containment leg.

    The shared walk classifies every source leaf through :func:`_is_file`; FIFOs,
    dangling links, and refused entries therefore have no copy/read path. Directory
    visits preserve main's existing empty-directory behavior and its boolean result:
    true means at least one directory or file actually landed, false means total no-op.
    """
    copied = False
    no_clobber = skip_existing or worktree is not None

    def source_contained(entry) -> bool:
        if repo_root is None or not isinstance(entry, Path):
            return True
        try:
            return entry.resolve().is_relative_to(repo_root)
        except (OSError, RuntimeError):
            return False

    def target_for(rel: str) -> Path:
        return dst.joinpath(*rel.split("/"))

    def target_contained(target: Path) -> bool:
        if worktree is None:
            return True
        try:
            return target.resolve().is_relative_to(worktree)
        except (OSError, RuntimeError):
            return False

    def should_descend(rel: str, entry) -> bool:
        target = target_for(rel)
        if not source_contained(entry) or not target_contained(target):
            return False
        if no_clobber and _occupied(target) and not _is_dir(target):
            # A file or dangling link sits where the source has a directory. It wins;
            # mkdir must never replace it or write through its target.
            return False
        return True

    def visit_dir(rel: str, entry) -> bool:
        nonlocal copied
        # Recheck after enumeration: iterdir may block while a source or destination
        # component is replaced, so the pre-walk containment result can go stale.
        if not should_descend(rel, entry):
            return False
        target = target_for(rel)
        existed = _occupied(target)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            if worktree is None:
                raise
            return False
        if not existed:
            copied = True
        return True

    for rel, child in _walk_traversable_files(
        src,
        _should_descend=should_descend,
        _visit_dir=visit_dir,
        _suppress_errors=worktree is not None,
    ):
        if not _is_file(child) or not source_contained(child):
            continue
        target = target_for(rel)
        if not target_contained(target) or (no_clobber and _occupied(target)):
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(child, Path):
                # Preserve modes for filesystem sources (notably vendor/bin/*).
                shutil.copy2(child, target)
            else:
                # Zip-imported Traversables expose no stat: content only.
                target.write_bytes(child.read_bytes())
        except OSError:
            if worktree is None:
                raise
            continue
        copied = True
    return copied


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
# `--includes` note below is, so a later reader does not re-propose it as a
# simplification of the whole block.
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

    Refusing an unparseable answer is the point rather than a fallback: the only
    caller uses this to decide whether to make a PERMANENT repo-format change, so
    the failure it must not have is the optimistic one — a git that will not say
    what it is does not get that change made on its behalf.
    """
    match = re.match(r"git version (\d+)\.(\d+)", reported.strip())
    return match is not None and (int(match[1]), int(match[2])) >= want


def _shield_shared_config(worktree: Path, shared: str, key: str, *opts: str) -> bytes | None:
    """`key`'s raw value in the SHARED config, or None when it is definitely ABSENT.

    Raises `GitError` when git did not answer. **rc 1 is the only non-zero rc that
    means "no such key"** (#384) — reading any other non-zero rc as an absence
    degrades the `core.bare` / `core.worktree` gates OPEN, and what they guard is
    irreversible: enabling `extensions.worktreeConfig` drops the exception that
    confined those keys to the main worktree, after which a genuine `core.worktree`
    starts applying to every linked worktree.

    The rc mapping, stable from the 2.20 floor to current git:

        absent key ......................... 1     malformed config file ..... 128
        missing file ....................... 1     --type=bool on a non-bool . 128
        `--file` naming a directory ........ 1     key present ................ 0
        unreadable file (EACCES, non-root) . 1     empty or valueless key ..... 0

    git-config(1) prefaces its own list with "Some exit codes are:", i.e. the list is
    not closed, and it documents a malformed file as ret=3 while git actually answers
    128 — so a `{0,1}`-closed reading is wrong on git's documentation AND on its
    behavior. Hence a funnel rather than a per-rc taxonomy.

    THE RESIDUAL THIS CANNOT CLOSE: a momentarily missing or unreadable `.git/config`
    answers **rc 1**, byte-identical to "no such key" — all four rc-1 rows above are
    the same answer from here. git exposes no way to separate them, so this is a
    bound on the interface rather than a defect. Deliberately NOT narrowed with a
    pre-`--get` stat: that is TOCTOU, which shrinks the window instead of closing it,
    adds a syscall to every probe, and trades a very rare silent-wrong for a less
    rare noisy-degrade. Bounded in practice because git rewrites config atomically
    (lock + rename), so only a non-git tool truncating in place can produce it.

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

    For a filemode git masks the value with 0666, so private is `& 0066 == 0` — NOT
    "no execute bits" and NOT "equals 0600". `0711` IS private, because the mask
    discards the execute bits; `0755` is NOT, because its group/other READ bits
    survive it. Those two cases are why this is a mask test and not a literal set.

    `(mode & 0600) != 0600` is git's own `die()` condition, so such a value reaches
    us only in a repository git already refuses to operate on. It stays off the
    accept side with every other value git rejects.

    THE PATTERN IS DELIBERATELY STRICTER THAN `strtol`, in the refusing direction.
    git accepts leading whitespace and a leading `+` (`+0600` is private to git)
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
    # The legacy 1 (PERM_GROUP) and 2 (PERM_EVERYBODY) need no special case here,
    # though git special-cases them ahead of its filemode branch: neither satisfies
    # `& 0600`, so both land on the refusal this line already returns. A
    # `mode in (1, 2)` arm would be indistinguishable from dead code (#384).
    return mode & 0o600 == 0o600 and mode & 0o066 == 0


def _shield_shared_repository(worktree: Path, common_dir: Path) -> str | None:
    """Refuse a repository configured to be SHARED BETWEEN OS USERS, else None.

    A repository shared between OS users is not a supported configuration
    (maintainer decision, #384): the shield creates files of its own inside `.git`,
    and making those work across OS users is out of scope. Refusing up front fails
    LOUDLY and identically for every user — journaled and notified — rather than
    behaving differently depending on who provisioned first.

    The gate runs ABOVE the shield's lock, so nothing is created in a shared
    repository at all. That is safe here and would not be for
    `extensions.worktreeConfig`: this key is static configuration the tool never
    writes, so there is no shared mutable state to serialize. It adds no version
    floor either — a plain `--get` with no `--type=`, so it does not reintroduce the
    2.18 trap that keeps the version gate above the probes in
    `_shield_enable_worktree_config`.

    AN ALLOWLIST, NOT AN ENUMERATION OF SHARED SHAPES: git accepts keywords,
    booleans, the legacy 0/1/2 and bare octal modes, so enumerating the shared ones
    leaves every value git adds later — and every value git rejects — silently
    opening the gate. Values git rejects really do reach here, since `rev-parse
    --absolute-git-dir` answers rc 0 for every one of them, `banana` included; the
    "it would have fatalled earlier" reasoning that fits other probes in this file
    does not hold. They fall off the allowlist deliberately: such a repository is
    already unusable (`git status` exits 128), so refusing costs nothing while
    guessing at a malformed value would.

    AN EMPTY VALUE AND A VALUELESS `sharedRepository` LINE ARE INDISTINGUISHABLE AND
    MEAN OPPOSITE THINGS: explicitly empty is PERM_UMASK (private), a valueless line
    is PERM_GROUP (shared), and `--get` answers both rc 0 with a lone b"\\n". Nothing
    separates them, so the empty answer REFUSES — a false refusal is a reported skip,
    a false accept is the bug this gate prevents.

    `removesuffix("\\n")`, NEVER `.strip()`: `--get` returns edge whitespace VERBATIM
    (a quoted `" umask"` comes back as b" umask\\n"), so stripping would widen a value
    git itself rejects straight into the accept set below. The case handling is
    asymmetric because git's is — keywords compared with strcmp, booleans with
    strcasecmp — so `FALSE` and `Off` are accepted while `UMASK` and `GROUP` are
    values git REJECTS.

    SCOPE, NARROWER THAN GIT'S OWN RESOLUTION: the read names the repository's
    SHARED CONFIG. git honors `core.sharedRepository` from a GLOBAL config too, so
    an operator's global setting is not consulted here and such a repository is
    still shielded — deliberately, since a global preference is not a statement that
    THIS repository is shared. What is refused is a repository CONFIGURED as shared,
    the shape `git init --shared` writes (`--shared=group` records the legacy
    `sharedrepository = 1`).

    WORKTREE SCOPE is unread too, and git DOES honor `core.sharedRepository` from a
    linked worktree's own `config.worktree`, which the `--file` read below answers
    rc 1 for. What makes that unreachable is the provisioning flow rather than
    anything here: the shield only ever runs on a worktree this process just created
    (`workspace.open_unit_workspace` -> `verify.worktree_add` ->
    `worktree_flow.provision_worktree`), `git worktree add` creates the admin dir
    with NO `config.worktree`, a re-mount prunes `.git/worktrees/<id>` whole, the
    MAIN worktree's `.git/config.worktree` does not reach a linked one, and the
    tree's only worktree-scoped write is the shield's own `core.excludesFile`. So
    the value has no writer and no window. If a re-provision path onto an EXISTING
    worktree is ever added, or anything upstream starts writing the unit worktree's
    config, this read must grow a second `--file <git_dir>/config.worktree` probe in
    the same change.
    """
    raw = _shield_shared_config(worktree, str(common_dir / "config"), "core.sharedRepository")
    if raw is None:
        return None
    value = os.fsdecode(raw).removesuffix("\n")
    if _shared_repository_is_private(value):
        return None
    # {value!r}: operator-authored text going into a journaled reason, and repr
    # neutralizes a newline in it — as the version gate above does.
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

    Answering is split from writing because enabling the extension is a PERMANENT
    repo-format change: performed here it precedes the seed, the write and the
    activation, so any degrade below it left the repo marked for a shield that never
    applied. The caller enables it immediately before the activation and rolls the
    flag back when the shield then fails to hold — including when the ENABLE ITSELF
    fails in a way that leaves the outcome unknown. The flag outlives a failed
    shield only where the rollback was DECLINED because a sibling worktree depends
    on it, or could not be made at all; the reason says which.

    `extensions.worktreeConfig` is what makes a per-worktree config file exist at
    all; without it `git config --worktree` either refuses outright (a repo with
    linked worktrees) or silently writes the SHARED config.

    Enabling it is refused in the two shapes git-worktree(1) (CONFIGURATION FILE)
    calls out: with `core.bare = true` or `core.worktree` in the shared config,
    enabling drops the exception that confines those keys to the main worktree, so
    they would start applying to every worktree. Git's remedy is a repo-layout edit
    the installer will not make behind an operator's back, so the shield degrades
    instead.

    The version gate and both refusal probes STAY HERE, first. `git config
    --worktree` does not exist before 2.20, so on an older git the enable buys a
    permanent format change with nothing to show for it, and git-worktree(1) says
    older git refuses a repository carrying the extension. `--type=` is itself git
    2.18, so on an older git the two `--type=bool` probes below cannot answer —
    `_shield_shared_config`'s raise on any rc but 0 or 1 is defence in depth BEHIND
    this gate, not licence to relax the floor. The caller's `--type=path`
    excludesFile read rests on it too.
    """
    # Polarity note: this gate reads `returncode != 0` as REFUSE, while the funnel
    # below reads it as "git did not answer" and raises. Both fail closed, by
    # opposite-looking means, and unifying them would reverse one — an unanswerable
    # `git version` must refuse here, not degrade into a question about a key. Nor is
    # it a repo-config check: `git version` does no repository setup, so it exits 0
    # where `rev-parse` fatals 128 on a malformed `.git/config`.
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
    # EVERY read below names the SHARED config as a file rather than going by scope
    # (`--local` resolves to it today; naming it keeps the checks honest if that
    # stops being true). Load-bearing for the first of them: `extensions.*` is
    # repo-format state git honors only from the repo's own config, so a
    # `worktreeConfig = true` in `~/.gitconfig` answering an unscoped read would be a
    # false "already enabled" — the write below skipped, the activation left to fatal
    # for want of the extension.
    #
    # And NO `--includes` on any of the three, which is the asymmetry a later reader
    # will want to "fix" — `--file` without it does miss a value an `include.path`
    # supplies. Adding it breaks both gates, because git's worktree SETUP reads
    # `core.bare` / `core.worktree` from the common config LITERALLY: it goes through
    # `git_config_from_file`, which does not respect includes (config.h).
    #
    # - The REFUSAL probes would start refusing repos git is perfectly happy to
    #   shield, leaving the provisioned tool files unshielded: include-supplied
    #   values of either key do nothing, while literal ones move a linked worktree's
    #   toplevel to the main checkout or fatal every command. (`core.bare` only
    #   because `is_bare_repository()` also requires no work tree, which every
    #   worktree here has — its inertness is a conjunction, not an absence.)
    # - The ENABLED probe is worse: an include-supplied `extensions.worktreeConfig`
    #   is not honored either (`git config --worktree` still fatals), because the
    #   `config.worktree` read is gated on the flag harvested by that same
    #   include-blind setup read — so `--includes` there is the same false "already
    #   enabled", and nothing is shielded.
    #
    # All of that is undocumented IMPLEMENTATION DETAIL, not a compatibility
    # contract: git has converted reads to respect includes before (2.39, protected
    # config). Flag availability was never the question: `--includes` dates to git
    # 1.7.10, far below the 2.20 floor the gate above enforces.
    shared = str(common_dir / "config")
    carried = _shield_shared_config(worktree, shared, "extensions.worktreeConfig", "--type=bool")
    if carried is not None and os.fsdecode(carried).strip() == "true":
        return None, False  # already carried: nothing for the caller to write
    bare = _shield_shared_config(worktree, shared, "core.bare", "--type=bool")
    if bare is not None and os.fsdecode(bare).strip() == "true":
        refused = "core.bare = true"
    elif _shield_shared_config(worktree, shared, "core.worktree") is not None:
        # `is not None`, NEVER truthiness: any value refuses, including an empty one.
        # `core.worktree = ""` and a valueless `worktree` line both answer rc 0 with a
        # lone b"\n", so truthiness holds today — but one `.strip()` makes it b"",
        # which is falsy, re-opening this gate for a key that IS set. The value is
        # never decoded or inspected: this key is a path and its mere PRESENCE refuses,
        # unlike `core.bare`, where only a true value disqualifies.
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
    clause naming the fault otherwise — never raises: every caller is already
    reporting a degrade, and this says what it could not undo.

    Callers MUST gate this on having enabled the flag themselves, or on not having
    been able to FIND OUT whether they did — which is what the enable's own raise
    means. Unsetting one the repository already carried would stop git reading the
    `config.worktree` files other worktrees may depend on, so the rollback is
    conditional, never unconditional cleanup.

    That caller-side gate is necessary and NOT sufficient (#384): `needs_enable`
    records what the PROBE saw, and the enable is an idempotent rc-0 no-op against a
    flag already `true`, so two concurrent provisionings that both probed an absent
    flag both believe they own it, and whichever one's activation fails would unset a
    flag the other's LIVE shield depends on — leaving its tool files stageable
    mid-run. The caller serializes the transaction under a repository lock, but a lock
    binds only its holders: an operator, an older bmad-loop, or `git sparse-checkout`
    can set the same flag outside it.

    EXCLUDING OUR OWN `git_dir` IS LOAD-BEARING: a `config.worktree` there is the
    half-written product of the very activation whose failure prompted this rollback
    (a timeout can leave one behind), so counting it as a dependent would suppress
    the rollback.

    WHAT IT DOES NOT COVER: a non-lock-taking party between its own enable and its
    own activation owns the flag while having written nothing yet, so the scan cannot
    see it and this rollback unsets it. Bounded — its `git config --worktree` then
    fatals for want of the extension (rc 128 — a sibling exists by construction here;
    in a single-worktree repo `--worktree` instead exits 0 and writes the SHARED
    config), so its shield degrades with a REPORTED reason rather than going silently
    inert. The opposite error is accepted too: a stale admin dir git has not pruned yet
    counts as a dependent, costing a flag left enabled and nothing else.
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
        # leaves a reported flag this call did not earn, while unsetting one that IS
        # depended on silently un-shields a live worktree. So an unreadable scan
        # counts as a dependent.
        #
        # The tuple mirrors the caller's tail minus `GitError` (nothing here spawns
        # git), and the members beyond `OSError` are what keeps the "never raises"
        # promise true: on the 3.11/3.12 floor `Path.resolve()` raises `RuntimeError`
        # for a symlink loop rather than `OSError`, and `UnicodeError` covers the
        # `fsdecode` of a worktree admin-dir name on Windows.
        dependent = f"the scan for sibling worktrees failed ({e})"
    if dependent is not None:
        return (
            "; extensions.worktreeConfig was deliberately LEFT enabled — "
            f"{dependent} exists, so another worktree's shield depends "
            "on the flag and unsetting it would stop git reading that file"
        )
    try:
        # `--unset-all`, and rc 5 counts as SUCCESS: an uncertain ENABLE routed here
        # makes an absent flag reachable, and `--unset` of an absent key exits 5 —
        # reporting that as a failed rollback would be false.
        #
        # `--unset` alone could not simply treat 5 as success, because git-config(1)
        # gives that code TWO meanings — the key did not exist, or MULTIPLE LINES
        # matched — so a doubled key that SURVIVED would report a clean rollback.
        # Stable from the 2.20 floor to current git: `--unset` against a doubled key
        # exits 5 and removes NOTHING, while `--unset-all` exits 0 and removes both
        # lines, collapsing rc 5 to the single meaning "no line matched".
        undone = git_bytes(worktree, "config", "--unset-all", "extensions.worktreeConfig")
        if undone.returncode in (0, 5):
            return ""
        detail = os.fsdecode(undone.stderr).strip() or f"git exited {undone.returncode}"
    except GitError as e:
        # the rollback's OWN git can time out or fail to spawn: a read-only `.git` or
        # a dead git fails this unset for the same reason it failed the activation.
        detail = str(e)
    # Both clauses HEDGE whether this shield set the flag, and must: reached from the
    # enable's own raise, a spawn failure can kill the enable and this unset alike,
    # having written nothing at all. No precision is lost — `needs_enable` is
    # probe-derived and cannot tell "we enabled it" from "we both thought we did".
    return (
        "; extensions.worktreeConfig could NOT be "
        f"rolled back ({detail}) — if this shield set the flag, the repository keeps "
        "a permanent format change that shields nothing"
    )


def _shield_home_git_ignore(worktree: Path) -> Path:
    """`$HOME/.config/git/ignore` — git's XDG fallback — asked of GIT, not of Python.

    `Path.home()` is the obvious spelling and it is WRONG on Windows, the one
    platform where git's home and Python's are derived differently. Git locates this
    fallback with `getenv("HOME")` everywhere (`path.c::xdg_config_home`), but on
    Windows `compat/mingw.c::setup_windows_environment()` DERIVES `HOME` inside the
    git process first, preferring `HOMEDRIVE`+`HOMEPATH` (only when that names a real
    directory) over `USERPROFILE`; Python's `ntpath.expanduser` has the OPPOSITE
    precedence and never consults `HOME` at all. Git Bash and MSYS2 set `HOME`; a
    domain-joined machine's `HOMEDRIVE`+`HOMEPATH` may be a network home share.
    Either makes the two disagree.

    The miss is SILENT, and it is #384's own harm through the platform split: the
    wrong path is simply not a file, the seed comes back empty, and the caller's
    activation then SHADOWS the global ignore file git really reads.

    So ask git. `--type=path` interpolates a leading `~/` through the same
    `getenv("HOME")` git uses for the fallback itself, so the answer is correct by
    construction on every platform and names no environment variable. `-c` is COMMAND
    scope, the most specific git has, so the probe's value cannot be outranked by the
    operator's config. The answer comes back NUL-terminated at rc 0; with `HOME`
    unset git exits 128 and applies no fallback at all.

    KNOWN GAP: this answers "where is git's `$HOME`", which is the whole of the
    fallback UPSTREAM but not on **Git for Windows >= 2.46**, whose fork patches
    `xdg_config_home_for` to prefer `%APPDATA%/Git/<file>` whenever that file EXISTS.
    An operator there keeping global ignores at `%APPDATA%\\Git\\ignore` still gets
    the wrong file seeded. Not fixed here: a downstream fork's behavior, gated well
    above this shield's floor.

    Raises `GitError` on any non-zero rc, INCLUDING the `HOME`-unset one. Proceeding
    would be a guess, and a guess here is silent: the caller seeds nothing and
    activates over whatever git does read. git's message is deliberately NOT matched
    to tell "no HOME" apart from a transient fault, because that wording is
    version-dependent. A `HOME`-less environment therefore skips the shield with a
    reported reason; only a definite absent may be silent in this caller.
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

    A worktree-scoped `core.excludesFile` SHADOWS the operator's own: git reads the
    key from the most specific scope that sets it, never concatenating scopes. Not
    carrying their patterns over would un-ignore, inside the worktree, everything
    they ignore globally — and `git add -A` would commit it. The caller seeds once,
    at creation, so the copy never fights later edits to the private file.

    BYTES, never decoded text: exclude entries are path patterns, POSIX paths are
    arbitrary bytes, and one non-UTF-8 byte collapsed a `read_text` seed to empty —
    after which the activation SHADOWED what it failed to copy. Copying verbatim
    also keeps every pattern intact for the caller's dedupe; git reads the result
    either way, stripping a trailing `\\r` from an exclude line and skipping a
    UTF-8 BOM.

    FOUR outcomes (#384). The first two are silent; the other two RAISE, and the
    caller's tail turns that into the degrade reason that skips the activation.

    - ABSENT is silent, empty, and the ONLY outcome that falls back: `--get` of an
      unset key exits 1, and `is_file()` false says the fallback file is not there.
      Reaching that fallback can itself fail — `_shield_home_git_ignore` raises, and
      an unresolvable home is UNKNOWN, not absent.
    - DISABLED is silent and empty too, and is NOT absent. An explicitly EMPTY
      `core.excludesFile` means "no excludes file at all", and git honors that
      literally: `-z --get` answers rc 0 with a lone NUL, git loads no patterns, and
      does NOT reach for XDG (`dir.c` guards that fallback on a NULL `excludes_file`;
      an empty value is not NULL — unchanged from the v2.20 floor to master).
      Reading it as unset instead seeds and activates the XDG file's patterns —
      OVER-ignoring, so files the operator deliberately stopped ignoring go silently
      missing from `git add -A`.
    - PRESENT BUT UNREADABLE propagates: a read `OSError` (EACCES, EIO) reaches the
      caller's degrade arm — activating over patterns that could not be copied
      shadows them exactly as an empty seed did.
    - UNKNOWN propagates too, in TWO shapes: a raised `GitError`, and any returncode
      that is neither 0 nor 1. A fault means we did not FIND OUT; swallowing it maps
      UNKNOWN onto ABSENT and activates a key shadowing the file it failed to read.
    """
    # -z, not a bare read plus `.strip()`. `--type=path --get` returns leading and
    # trailing whitespace VERBATIM, because git writes such a value quoted and it
    # round-trips. So an operator whose excludesFile ended in a space had the strip
    # mangle the path, `is_file()` read absent, the seed come back empty, and the
    # activation shadow the file it never copied. `-z` terminates the value with NUL
    # instead.
    #
    # The answers below hold at the 2.20 floor (the version refusal in
    # `_shield_enable_worktree_config` has already run, so 2.20 is the oldest git that
    # reaches this line) and at 2.55: `-z` expands `~`, gives an unset key rc 1 +
    # empty stdout, an empty value a lone NUL at rc 0, a multi-valued key the LAST
    # value + NUL at rc 0, and a relative value verbatim.
    answer = git_bytes(worktree, "config", "-z", "--type=path", "--get", "core.excludesFile")
    raw = answer.stdout.split(b"\0", 1)[0]
    if answer.returncode == 0:
        if not raw:
            # DISABLED: an explicit "no excludes file", so there is nothing to copy
            # and NO fallback to reach for — see the docstring. The arm is inert on
            # its own (`Path(os.fsdecode(b""))` is `.`, which reads `is_file()` false),
            # so what separates DISABLED from ABSENT is the rc-FIRST condition above,
            # and restoring `and raw` on it is the only faithful ablation.
            return b""
        source = Path(os.fsdecode(raw))
        if not source.is_absolute():
            # `--type=path` expands `~` and stops there: a RELATIVE value comes back
            # verbatim. Git resolves such a value against the worktree's TOP LEVEL —
            # observed rather than specified (it falls out of git's setup chdir), and
            # it holds here because every git call goes through `_run_git`'s
            # `git -C <worktree>`. Python would resolve it against this process's cwd,
            # wherever the orchestrator was launched — the wrong file or, far more
            # often, none, and `is_file()` false is SILENT: the operator's patterns
            # would not be carried and the activation below would shadow them anyway.
            source = worktree / source
    elif answer.returncode != 1:
        # UNKNOWN, through git's OTHER failure shape. rc 1 is the ONLY non-zero rc
        # that means "no such key"; every other one is git saying it could not answer,
        # and routing those to the fallback below is the same UNKNOWN-as-ABSENT
        # mistake the raised shape is caught for.
        #
        # No STATIC config value reaches this branch: a `core.excludesFile` of
        # `~nosuchuser/ignore` does make THIS read exit 128, but git expands that same
        # value while parsing core config for any command that sets the repository up,
        # so `rev-parse --absolute-git-dir` in the caller fatals identically and the
        # shield SILENTLY SKIPS above this line. What occupies this branch is a fault
        # arriving BETWEEN that rev-parse and this read, and the caller sets that
        # window's width: the rev-parse runs before the repo-scoped lock, this read
        # after it, and POSIX `flock` blocks INDEFINITELY, so a run waiting on a
        # sibling worktree's shield sits in it for the sibling's whole git
        # transaction. Such a fault can CLEAR while an activation performed over it
        # lasts as long as the worktree.
        #
        # A FUNNEL rather than a per-rc taxonomy: git-config(1) allocates several
        # failure codes, and anything that is not "answered" or "no such key" is not
        # an answer.
        detail = os.fsdecode(answer.stderr).strip() or f"git exited {answer.returncode}"
        raise GitError(f"git could not resolve core.excludesFile: {detail}")
    else:
        # ABSENT (rc 1, an unset key): git's documented fallback (gitignore(5)), and
        # the ONLY outcome that gets one.
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
            # THE SAME DEFECT AS THE RELATIVE `core.excludesFile` ABOVE: this branch
            # reproduces git's fallback, so it must reproduce git's RESOLUTION too. A
            # relative XDG_CONFIG_HOME is invalid per the XDG base-directory spec, but
            # git does not ignore it: it HONORS the value and resolves it against the
            # worktree's TOP LEVEL, while Python would resolve it against the
            # orchestrator's launch cwd. The miss is SILENT — `is_file()` false, an
            # empty seed, and then the activation shadows the file git really reads.
            source = worktree / source
    # `is_file()` is the ABSENT test and swallows its own OSError; `read_bytes`
    # on a file that exists but cannot be read raises, deliberately (docstring).
    return source.read_bytes() if source.is_file() else b""


def _shield_verify_activation(worktree: Path, exclude: Path) -> str | None:
    """Why the just-written `core.excludesFile` is NOT the one git reads, or None.

    A successful `git config --worktree core.excludesFile` proves the value was
    WRITTEN, not that it is in FORCE. git resolves the key from the most specific
    scope that carries it, and `command` — a scope fed by the ENVIRONMENT the
    orchestrator was launched with — outranks `worktree`. So an operator carrying an
    ambient `core.excludesFile` gets a shield that reports success and whose private
    file is never read: the provisioned tool files stay stageable by the unit's
    `git add -A`, with no degrade reason to journal.

    So the post-condition is asked of git. Detecting the ORIGIN instead —
    `GIT_CONFIG_COUNT` and friends in `os.environ` — is the enumeration this function
    exists to avoid: `GIT_CONFIG_COUNT` does not exist at this shield's own 2.20
    floor, `GIT_CONFIG_PARAMETERS` does but in two mutually incompatible encodings
    across the supported range, and a `git -c` on a session's own command line never
    appears in our environment at all. Asking git what it RESOLVED costs one call and
    covers all of them, and whatever git adds next.

    `--show-scope` would name the winning scope, and is deliberately not used: it is
    git 2.26, above the 2.20 floor, and a probe flag with its own version floor turns
    every rc-branch below it into a silent default.

    The read shape is the seed read's: `-z` because a legal POSIX path may carry edge
    whitespace, `--type=path` because that is how git itself resolves the key. Any
    non-zero rc is a fault here, not an ABSENT answer — this call asks about a key we
    have just written, so "there is no such key" is not good news about it. That is a
    DIFFERENT taxonomy from `_shield_inherited_excludes`, which reads a key the
    operator may never have set; the two must not be unified.

    The comparison is byte-exact and stays that way: `git config` round-trips a path
    verbatim through every hazard this shield has been burned by — edge whitespace,
    an embedded newline, a non-UTF-8 byte, an interior `~` — so loosening it would
    buy nothing and could only mask a real mismatch.
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
    private exclude scoped to THIS worktree.

    SCOPE — the patterns land in `<worktree gitdir>/info/exclude`, activated by
    `git config --worktree core.excludesFile`. Git's only per-repo exclude is
    `$GIT_COMMON_DIR/info/exclude`, so the private file is inert without that key,
    which applies it to this worktree alone — main checkout and siblings untouched.

    LIFETIME — `git worktree remove`/`prune` deletes the per-worktree gitdir, taking
    the exclude and the `config.worktree` pointing at it: the shield expires with
    what it shields.

    The repository-wide `.git/info/exclude` is NEVER written again on any path
    (#384): a refusal SKIPS the shield rather than falling back to it.

    PRICE — `extensions.worktreeConfig` is repo-wide format state a successful shield
    leaves permanently enabled, and git older than 2.20 refuses to access a repository
    carrying it (git-worktree(1)). It is enabled at the LAST possible moment,
    immediately before the activation. The ACTIVATION rolls it back on a non-zero rc,
    on a raise, and on a write that succeeded without taking effect
    (`_shield_verify_activation`); the ENABLE only on the raise, since an rc means git
    declined to write and an unset would remove a concurrent writer's flag. Both are
    gated on this call having set the flag and no sibling worktree depending on it
    (`_shield_undo_extension`).

    Every git call goes through `verify.git_bytes`: the returncode is an ANSWER, not
    a raise, and stdout arrives as BYTES.

    Best-effort in two arms, separate because a fault means the opposite in each:

    - git unqueryable BEFORE GIT HAS ANSWERED AT ALL (not a repo, git missing, a
      timeout or spawn failure on the FIRST `rev-parse` — both `GitError`) is an
      EXPECTED skip: returns None silently. That scope is the whole of it, and
      nothing is DECODED in this arm — git's stdout stays bytes until the tail
      below, because a decode fault is a degrade, not this arm's silent skip (#374).
    - anything AFTER git answered degrades to a returned reason string; only a
      definite ABSENT answer stays silent. Silence is not cosmetic: without the
      exclude the unit's `git add -A` commits the tool files just provisioned.

    The exclude PAYLOAD is bytes end-to-end, so the tail's `UnicodeError` has two
    sources: `os.fsdecode` of git's stdout, Windows-only (#374/#377), and
    `os.fsencode` of a pattern carrying a surrogate POSIX surrogateescape never
    produced.
    """
    # Callers pass POSIX-slash patterns (glob rels via as_posix; config strings as
    # authored); git's exclude is POSIX-slash on every platform, so nothing to fix here.
    try:
        # ONE path per call, never both in one answer. `rev-parse` separates its
        # answers with a newline, a legal BYTE IN A POSIX PATH: asking for both at once
        # mis-split a repo directory carrying one, degrading the shield away *after*
        # provisioning had copied the very files it exists to hide. No NUL-delimited
        # mode to reach for instead — `rev-parse` has no `-z`. A path ENDING in a
        # newline is beyond this parse either way.
        answered = git_bytes(worktree, "rev-parse", "--absolute-git-dir")
        if answered.returncode != 0:
            # Not a repo (rc 128), the expected skip — NOT "a git too old for
            # `--absolute-git-dir`": rev-parse ECHOES an option it does not know and
            # exits 0, so a git predating the flag lands in the git 2.20 refusal in
            # `_shield_enable_worktree_config` — a degrade, not this silent skip.
            return None
    except GitError:
        # GitError alone is the whole taxonomy here: this `try` wraps ONE git call, and
        # the chokepoint raises a timeout as GitError and a spawn failure as its
        # GitSpawnError subclass — the translated form of the `OSError` this arm once
        # caught.
        return None
    try:
        # The COMMON-DIR probe belongs in THIS try, not the silent one above: once
        # `--absolute-git-dir` has answered, git has identified the repository, so every
        # later fault degrades with a reason instead of skipping the shield silently. No
        # `except` of its own: a non-zero rc returns the reason below, a raise lands in
        # this try's tail — which also catches the `UnicodeError` the `fsdecode` of
        # git's stderr can raise on Windows, hence that read sits inside the guard.
        # #394 records that same decode escaping at a sibling block that lacks it.
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
        # fsdecode, so a non-UTF-8 repo path round-trips back to the filesystem instead
        # of faulting; it can still raise on Windows (utf-8/surrogatepass rejects a lone
        # invalid byte), which is why it sits inside this try.
        #
        # removesuffix("\n"), NOT strip() — rev-parse terminates its answer with one
        # newline and every other byte belongs to the path. A BARE repo's common dir IS
        # the repository directory, so one at `…/common ` comes back with its trailing
        # space (`--absolute-git-dir` stays safe: git sanitizes the admin id). Stripping
        # pointed every later step at `…/common`, which does not exist, where `--file
        # <that>/config` reads rc 1, i.e. ABSENT — so the `core.bare = true` refusal was
        # MISSED and the shield made its permanent format change on a bare repository.
        #
        # CRLF is deliberately not absorbed: git writes LF here on every platform, and a
        # path legitimately ending in `\r` is indistinguishable from a CRLF terminator.
        raw_git_dir = os.fsdecode(answered.stdout).removesuffix("\n")
        raw_common = os.fsdecode(shared_answer.stdout).removesuffix("\n")
        if not raw_git_dir or not raw_common:
            # Defensive, and deliberately a REASON rather than a silent skip: git
            # answered. Unreachable with real git — an rc 0 from `rev-parse` always
            # prints a path.
            return (
                f"could not read this worktree's git dirs ({worktree}): "
                f"{raw_git_dir!r}, {raw_common!r}"
            )
        git_dir = Path(raw_git_dir)
        common_dir = Path(raw_common)
        if not common_dir.is_absolute():
            # a PLAIN checkout answers with a relative ".git"; a linked worktree
            # answers with the main repo's absolute .git.
            common_dir = worktree / common_dir
        # resolve BOTH before comparing: --absolute-git-dir is absolute but not
        # canonical, so a symlinked repo path would otherwise read as two
        # different dirs and a main checkout would pass for a linked worktree.
        git_dir, common_dir = git_dir.resolve(), common_dir.resolve()
        if git_dir == common_dir:
            # The main checkout itself: its gitdir IS the common dir, so the only
            # exclude file to write would be the shared one — the #384 bug.
            return (
                f"skipped the git-add shield ({worktree}): not a linked worktree, and the "
                "shield never writes the repository-wide exclude (#384)"
            )
        # ABOVE THE LOCK DELIBERATELY: a repository shared between OS users is refused
        # before anything is created in it, so every user gets the same reason rather
        # than the second one meeting a lock file the first left behind. Safe there
        # because `core.sharedRepository` is static config the tool never writes — no
        # shared mutable state to serialize. A `GitError` from the read hits this tail.
        shared_repository = _shield_shared_repository(worktree, common_dir)
        if shared_repository is not None:
            return shared_repository
        # EVERYTHING BELOW IS ONE TRANSACTION, serialized per repository.
        # `_shield_enable_worktree_config` only PROBES the flag, so two concurrent
        # provisionings both read it absent, both believe they enabled it, and whichever
        # one's activation fails rolls the flag back out from under the other's LIVE
        # shield — its `core.excludesFile` goes inert and its shielded tool files become
        # stageable again mid-run. That flag is the one piece of state two runs share;
        # every other per-run name is keyed on the run id. Concurrent runs in one repo
        # are permitted by design — `cmd_run` has no liveness check and the TUI offers
        # "launch anyway".
        #
        # The span cannot be narrowed to those writes: the enable must stay immediately
        # above the activation, the private exclude must exist before it, and the
        # version + `core.bare`/`core.worktree` gates inside the probe must stay above
        # the seed read, which shares the same `--type=` floor. So the file write sits
        # inside the locked span.
        #
        # The acquisition's own `OSError` is caught HERE only so the operator is told
        # which step failed: POSIX `flock` blocks indefinitely, but `msvcrt.locking`
        # bounds the wait at ~10 s and then raises, so Windows contention is a real,
        # reportable outcome.
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
            # Zero bytes is an INTERRUPTED creation (a kill or ENOSPC between the
            # `touch()` and the fill below), not an initialized exclude: read as
            # authoritative it skips the seed below and then points a shadowing
            # `core.excludesFile` at a file missing the operator's own excludes. Sized
            # rather than unlinked on the way out, because no handler runs on a KILL.
            existed = exclude.is_file() and exclude.stat().st_size > 0
            # On creation the operator's own excludes are copied in, because the
            # activation below shadows them (see _shield_inherited_excludes); on a
            # re-provision a non-empty file is authoritative, so the two are idempotent.
            #
            # A COPY FAULT SKIPS THE ACTIVATION: `_shield_inherited_excludes` raises on
            # anything but a definite ANSWER — an unreadable file, or a git that will
            # not say which file applies — and the raise lands in this function's tail,
            # which returns a reason. It raises rather than returning empty
            # because an empty seed plus the activation below is not a lost seed but
            # the operator's global ignores SHADOWED inside this worktree, with `git
            # add -A` staging what they told git to ignore. The raise is above
            # `exclude.touch()`, so no placeholder is left behind.
            #
            # BYTES, never decoded text: an exclude holds path patterns, POSIX paths
            # are arbitrary bytes, and an operator's own file may be in any legacy
            # 8-bit encoding. `str.splitlines()` breaks on \x0b, \x0c, \x1c, \x1d,
            # \x1e and \x85, none of which git treats as a line boundary, so a
            # legitimate pattern carrying one fragments into wrong dedupe keys.
            # `bytes.splitlines()` is CLOSE to git's split but not identical: it
            # still breaks on a lone \r, which git does not (measured, 2.55 — see
            # legacy_exclude_pollution, which must tokenize exactly and therefore
            # splits on \n alone). Acceptable HERE because a fragmented dedupe key
            # can only make a wanted pattern read as absent-or-unsettled, and the
            # consequence of that is an appended duplicate: last match still wins.
            # (The mirror risk — a fragment matching a wanted pattern and SKIPPING
            # the append — needs the fragment to sit after the last negation; the
            # settled-set rule below keeps that window, so prefer \n-splitting if
            # this is ever touched.)
            existing = exclude.read_bytes() if existed else _shield_inherited_excludes(worktree)
            lines = existing.splitlines()
            # PRESENT IS NOT THE SAME AS EFFECTIVE (#384). gitignore is LAST MATCH
            # WINS, so a pattern this file already contains can be cancelled by a `!`
            # line below it, and a plain set-membership dedupe then declined to append
            # the shield's own pattern — leaving the provisioned tool files stageable
            # with NO degrade reason at all, because nothing failed. So a pattern may
            # be skipped only where it is GUARANTEED effective: it sits after the LAST
            # negation. No later line can negate it, so the last pattern matching any
            # path it covers is either this one or a positive below it, and both
            # ignore. Anything earlier is appended, which is always safe — the appended
            # copy is last, so it decides.
            #
            # Deliberately CONSERVATIVE rather than exact: the only negation that
            # actually defeats the shield re-includes the pattern's own directory
            # (`!.claude/skills` does it too — it need not repeat the positive's
            # spelling), while `!*.md` below it does not, since git never descends into
            # an excluded directory, and neither does `!/.claude`, since re-including a
            # parent leaves the child matched. Telling those apart is git's matcher, so
            # this appends a duplicate in the harmless cases instead of guessing.
            #
            # `startswith(b"!")` with NO lstrip is git-correct twice over: git does not
            # strip leading whitespace from a pattern, and `\!` escapes a literal
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
                # atomic_write_bytes, never write_bytes onto `exclude` directly (#375):
                # "wb" TRUNCATES before writing, and this is a read-modify-REWRITE
                # carrying the operator's own excludes in `prefix`, so a short write
                # (ENOSPC) left the file truncated mid-content — losing shield lines —
                # while the degrade reason still said "could not update". A truncation
                # landing on a bare "/" is inert: git strips it to a zero-length
                # pattern that matches nothing, so only the LOST lines matter. The helper
                # rather than a hand-rolled tmp+replace: it fsyncs before the replace
                # and carries the target's mode over, which a bare replace resets.
                # #381's interleaving race is moot here — the file is one worktree's
                # private gitdir, so no two runs share the target — but the atomic
                # write stays for #375, whose fault needs no second writer.
                if not existed:
                    # Create it first: O_CREAT under the umask gives the helper
                    # a mode to carry. `atomic_write_bytes` preserves a mode only
                    # when the target already EXISTS (a fresh one gets mkstemp's
                    # private 0600), so dropping this line would narrow every
                    # private exclude's mode — and git treats an exclude it
                    # cannot read as one that is not there: it warns, exits 0,
                    # and the shield silently protects nothing, with no degrade
                    # reason because the write SUCCEEDED. A fault right after
                    # this leaves an empty exclude, which git treats as absent.
                    exclude.touch()
                atomic_write_bytes(exclude, prefix + b"".join(p + b"\n" for p in new))
            # The PERMANENT repo-format change, deliberately the last-but-one thing
            # this function does. `_shield_enable_worktree_config` probed the gates and
            # reported the write still owed; paying it here is what keeps every degrade
            # above from charging the operator's repo a format change it carries
            # forever for a shield that never applied. Two writes can still set the
            # flag without shielding anything — this enable's own raise and the
            # activation's failure — and each rolls it back. It cannot move BELOW the
            # activation either: `git config --worktree` is what the extension unlocks.
            if needs_enable:
                # BOTH of the chokepoint's failure shapes classified into one value
                # before anything is decided: handling only the rc left a raise (a
                # timeout, a spawn failure) to the tail, which cannot roll anything
                # back, and `git config` can be killed AFTER renaming the new config
                # into place — flag set, no shield ever activated.
                #
                # THE ROLLBACK FIRES ON THE RAISE ONLY, and that asymmetry is
                # deliberate: an rc IS an answer — git refused, the lock file was never
                # renamed into place, there is nothing to undo, and the likeliest cause
                # is `.git/config.lock` contention with the very concurrent writer whose
                # flag an `--unset` would then remove. A raise is NOT an answer, and
                # only the uncertain case may trigger repair; do not "finish" this by
                # rolling back the rc too. `needs_enable` is True on this branch, so
                # `_shield_undo_extension`'s caller-side gate holds by construction.
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
            # but a config pointing at a file a fault above left unwritten would be a
            # shield that silently excludes nothing.
            #
            # ONE rollback covering EVERY way this step can fail to leave a working
            # shield. `git_bytes` fails two ways — a non-zero rc (git refused) and a
            # RAISE (a timeout, or a spawn failure as its GitSpawnError subclass) — and
            # a third outcome is not a failure at all: a write that succeeds without
            # taking effect (`_shield_verify_activation`, below). So the `try` spans the
            # write AND the verification, every shape converges on one `fault` string
            # before anything is decided, and rc 0 is no longer an exit from this block.
            # The rule: confirm the POST-CONDITION rather than enumerate the ways the
            # call in front of you can go wrong.
            #
            # The rollback exists because the enable one line above is a PERMANENT
            # repo-format change, and the guarantee this code owes (docstring,
            # CHANGELOG, docs/FEATURES.md) is that the flag outlives a failed shield
            # only where a rollback was DECLINED or could not be made, and the reason
            # says which. `needs_enable` gates it — a safety property, not a
            # micro-optimization, and see `_shield_undo_extension` for the second gate
            # the callee adds: `needs_enable` records only what the PROBE saw, so it
            # cannot tell "we enabled it" from "we and a concurrent run both thought
            # we did".
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
                    # An rc 0 means the value was WRITTEN, not that it is in
                    # FORCE — `command` scope outranks `worktree` and is fed by
                    # the environment we were launched with. Verified INSIDE this
                    # same `try` and above the one decision below, so the
                    # verification's own failure shapes reach the existing
                    # rollback instead of growing a second one.
                    fault = _shield_verify_activation(worktree, exclude)
            except GitError as e:
                # Deliberately NOT left to the tail, which cannot roll the flag back: a
                # GitError from EITHER call in this block means what a refusal means —
                # the shield is not in force. Putting this except inside the `try` is
                # what extends that to the verification's own fault. The tail's GitError
                # guard stays load-bearing for the seed read above, so this narrows what
                # reaches it rather than bypassing it.
                fault = str(e)
            if fault is not None:
                if needs_enable:
                    fault += _shield_undo_extension(worktree, git_dir, common_dir)
                return f"could not activate the worktree git exclude ({worktree}): {fault}"
    # GitError is every fault a chokepoint git call can raise here — a timeout, or a
    # spawn failure as its GitSpawnError subclass. OSError and RuntimeError cover the
    # mkdir and read/write faults plus `.resolve()`'s pre-3.13 symlink loop, which
    # raises RuntimeError rather than OSError.
    #
    # UnicodeError, not UnicodeDecodeError, and NOT for the exclude file — that payload
    # is bytes end-to-end. What is left is the `os.fsdecode` of git's stdout
    # (Windows-only) and the `os.fsencode` of a pattern carrying a surrogate POSIX
    # surrogateescape never produced: ENCODE or DECODE depending on which, hence the
    # shared base class, and still far short of ValueError-broad.
    #
    # TypeError is deliberately absent: a str/bytes mixup on the payload path is a
    # programming error, and letting it crash beats reporting it as an operator's
    # degraded shield once per run.
    except (GitError, OSError, RuntimeError, UnicodeError) as e:
        return f"could not update the worktree-local git exclude ({worktree}): {e}"
    return None


class LegacyExcludePollution(NamedTuple):
    """Where the repository-wide exclude is, and which shield patterns it carries.

    Returned only when `lines` is non-empty, so a caller that has one of these has
    something to report and a file to name in the report. `lines` is every hit;
    `hiding_new_files`, `maybe_neutralized` and `no_tracked_content` partition it
    and always reunite to it.

    The split grades IMPACT, never AUTHORSHIP. No line here can be attributed —
    see the detector's docstring — so every bucket is for the operator to review
    before deleting; they differ only in what the repo's own index and the file's
    own line order prove about what the line is currently doing. A hit lands in
    `hiding_new_files` only when that claim is GUARANTEED: tracked content under
    the path AND the hit sits after the file's last `!` line, so no negation can
    re-include it (gitignore is last-match-wins). A tracked hit at-or-before a
    negation goes to `maybe_neutralized` instead — deciding whether that negation
    really re-includes it would mean modeling gitignore matching, which this
    check does not do.
    """

    path: Path
    lines: list[str]
    hiding_new_files: list[str]
    maybe_neutralized: list[str]
    no_tracked_content: list[str]


def legacy_exclude_pollution(
    project: Path, profiles: Sequence[CLIProfile], seed_rels: Sequence[str]
) -> LegacyExcludePollution | None:
    """Git-add shield patterns an OLDER bmad-loop left in `.git/info/exclude`.

    Read-only detection for the residue of #384. Until that fix, the worktree
    shield appended its patterns to the repository-wide exclude — shared with the
    main checkout, permanent, and unversioned. `_worktree_local_exclude` no longer
    writes that file at all, but nothing removes what it already wrote, and the
    patterns name paths projects legitimately TRACK (`.claude/skills`,
    `.claude/settings.json`). So in a repo provisioned by an older version the
    tracked files keep diffing normally while every NEW file under those paths
    stays invisible to `git status` and `git add -A` — the reporter lost 51 files
    and three whole skills that way, across two commits, with nothing in either
    diff able to reveal the cause.

    Detection only, never repair, and that is a decision rather than an omission:
    `.git/info/exclude` is a hand-editable file whose lines carry no authorship. A
    project that genuinely wants `/.claude/skills` ignored writes the identical
    line, and there is no way to tell the two apart after the fact — so removing
    one is a change only the operator can be sure is right. `validate` warns and
    names the lines; deleting them is theirs.

    EXACT matches only, for the same reason. A line counts only if it is
    byte-identical to a candidate. No prefix, substring or path-containment
    matching: `/.claude/skills-of-my-own` and a bare `.claude/skills` are the
    operator's lines, not ours, and a preflight that flagged them would be telling
    someone to delete their own gitignore rules.

    The candidate set reconstructs the HISTORICAL shared-exclude writers, which is a
    FIXED target — 0.8.x through v0.9.1 and pre-#384 `main`, every version that
    resolved `--git-common-dir`. It is deliberately NOT kept in step with today's
    `provision_worktree`: patterns that writer gained after #384 (the `seeded_bmad`
    per-file rels, the ledger seed) only ever reached the private per-worktree
    exclude, so a repo cannot carry them here.

    Limits — it UNDER-reports and never over-reports. Unreconstructable: a `_bmad/`
    child, an overlay profile or a `worktree_seed` entry REMOVED since the polluting
    run, and a `seed_globs` match that no longer exists on disk. Each leaves a line
    with no candidate, so it goes unreported rather than misreported.

    `--git-path info/exclude` names git's ONE shared exclude from either vantage:
    a main checkout answers relative (`.git/info/exclude`), a linked worktree
    answers with the MAIN repo's absolute path (verified, git 2.55) — which is the
    same file, and the file #384 is about. Nothing here reads the private
    per-worktree exclude the current shield writes; that one is supposed to carry
    these patterns.

    Silent on every fault — not a repo, git missing or too old, an unreadable
    exclude — because this is preflight observation, and a repo whose exclude
    cannot be read is not thereby a repo with a problem to report. The file's
    ENCODING is never a fault: it is read as bytes and matched byte-for-byte,
    so a legacy 8-bit exclude keeps its ASCII lines matchable.
    """
    candidates = {f"/{p.skill_tree}" for p in profiles}
    candidates |= {f"/{p.hooks.config_path}" for p in profiles if not p.hookless}
    candidates |= {f"/{rel}" for rel in seed_rels}
    # Unconditional, unlike the writers' `if customize_seeded` / `is_dir()` gates:
    # whether some past run seeded these is not knowable from here, and every one of
    # them is a pattern only the shield writes.
    #
    # The `_bmad` family is v0.9.1's, not 0.9.0's. That release still resolved
    # `--git-common-dir` (v0.9.1 install.py:1303), so it wrote this same shared file,
    # and `main` does not descend from it — a 0.9.1 user upgrading is the population
    # this detector exists for. Its own comment measured the damage: "a /_bmad line
    # hides an untracked _bmad/ from `git status` in the MAIN checkout, for the life
    # of the repo." The trailing slash on the render pattern is load-bearing; it is
    # what that writer emitted, and the match is byte-identical.
    candidates.add(f"/{CUSTOMIZE_DIR.as_posix()}")
    candidates.add(f"/{BMAD_DIR}")
    candidates.add(f"/{RENDER_DIR_REL}/")
    # `_seed_bmad_tree` returns [BMAD_DIR] when the checkout had no `_bmad/` — the
    # root line above — but one rel PER SEEDED FILE when it already had one. Mirror
    # that walk over the tree as it stands now. A child deleted since the polluting
    # run is unreconstructable and simply goes unreported (see Limits).
    bmad_root = project / BMAD_DIR
    if bmad_root.is_dir():
        try:
            for top in sorted(bmad_root.iterdir()):
                if top.name in BMAD_SEED_EXCLUDES or not top.is_dir():
                    continue
                for rel, _src in _walk_traversable_files(top, top.name):
                    candidates.add(f"/{BMAD_DIR}/{rel}")
        except OSError:
            pass  # observation degrades; an unreadable tree is not a finding
    # A bare "/" is never a shield pattern, and it is the candidate that could do
    # harm: it is a real (if odd) line for an operator to have written, and this
    # function's output tells them to delete what it names.
    #
    # No validated caller can produce it any more — every seed source is checked
    # at its own boundary (`skill_tree` and profile `seed_files` in
    # adapters/profile.py, plugin seeds in plugins/manifest.py, and
    # `scm.worktree_seed` in policy.py, which was the hole), and the writer skips
    # hookless config_paths for this same reason. This stays as the helper's own
    # contract: it takes `seed_rels` from its caller, and a detector that tells
    # someone to delete a line should not depend on validation happening upstream.
    candidates.discard("/")
    try:
        # fsencode is the writer's own encoding for these same patterns
        # (_worktree_local_exclude), so a hit is byte-identical to what a shield
        # wrote. Inside the try for the surrogate edge fsencode can refuse —
        # this function is contracted silent on every fault.
        wanted = {os.fsencode(c): c for c in candidates}
        answered = git_bytes(project, "rev-parse", "--git-path", "info/exclude")
        if answered.returncode != 0:
            return None  # not a repo, or a git too old to answer
        # fsdecode for the same reason _worktree_local_exclude uses it: a non-UTF-8
        # repo path must round-trip back to the filesystem rather than fault.
        exclude = Path(os.fsdecode(answered.stdout).strip())
        if not exclude.is_absolute():
            exclude = project / exclude
        raw = exclude.read_bytes()
    # GitError for the chokepoint's two raised faults (a timeout, and a spawn
    # failure as GitSpawnError) — this is a `validate` warning, and a git that
    # cannot answer is not a finding. OSError stays for the read.
    except (GitError, OSError, UnicodeError):
        return None
    # BYTES, tokenized the way GIT tokenizes this file: split on b"\n", trim one
    # trailing b"\r". Two defects lived in the decoded-text version of this read.
    # `str.splitlines()` breaks on \x0b \x0c \x1c \x1d \x1e \x85 — none of which
    # git treats as a line boundary — so an operator's line carrying one
    # FRAGMENTED, its first piece matched a candidate byte-for-byte, and validate
    # named a line that does not exist in the file: the one thing the Limits
    # paragraph above promises never happens. And `read_text("utf-8")` raised on
    # a legacy 8-bit exclude, which the except swallowed into "no pollution",
    # dropping every matchable ASCII line in the file.
    #
    # NOT `bytes.splitlines()` either — it still splits on a lone \r, which git
    # does not (measured, git 2.55: `/hidden\rjunk` leaves a file named `hidden`
    # unignored). That is the writer's dedupe splitter, where an over-split risks
    # only a redundant append; here it is a false deletion prompt. The single
    # trailing \r IS trimmed because git trims exactly one (measured: `/crlf\r\n`
    # ignores `crlf`, and `/two\r\r\n` ignores a file literally named `two\r`) —
    # a CRLF-saved exclude line really does hide the candidate path's new files,
    # and this check grades effect.
    tokens = [line.removesuffix(b"\r") for line in raw.split(b"\n")]
    present = set(tokens)
    # Set intersection IS the exact-match rule — no normalization, no strip(): a
    # leading space is a meaningful part of a gitignore pattern, so a line that
    # carries one is not a line this code wrote. Only the line TERMINATOR git
    # defines (\n, one optional preceding \r) is outside the pattern bytes.
    hit_pairs = sorted((wanted[line], line) for line in present & wanted.keys())
    hits = [line for line, _ in hit_pairs]
    if not hits:
        return None
    # PRESENT IS NOT THE SAME AS EFFECTIVE, on the read side this time: gitignore
    # is LAST MATCH WINS, so a `!` line BELOW a hit can re-include the path and
    # leave the hit doing nothing (measured, git 2.55: `/kept` then `!/kept` +
    # `!/kept/**` puts kept's new files back in `git status`). Same conservative
    # rule as the writer's dedupe (`_worktree_local_exclude` above): a hit is
    # GUARANTEED effective only where its last occurrence sits after the file's
    # last negation — the last pattern matching any path it covers is then this
    # line or a positive below it, and both ignore. Anything at-or-before a
    # negation is only MAYBE effective, and deciding for real would mean
    # modeling gitignore matching. Positional, never semantic: a negation ABOVE
    # the hit cannot defeat it and must not degrade the claim.
    last_negation = -1
    last_at: dict[bytes, int] = {}
    for i, token in enumerate(tokens):
        if token.startswith(b"!"):
            last_negation = i
        last_at[token] = i
    # Widening the candidate set widened the chance of naming a line the operator
    # wrote, and this function's output prompts a deletion. Grade what each line is
    # DOING, on evidence from the repo's own index — not who wrote it, which nothing
    # here can establish, and not a hand-curated list of "ours" paths, which would
    # rot against every new profile and plugin:
    #
    #   TRACKED content under the path -> the rule cannot be suppressing THAT
    #   content (an exclude never untracks), so what it is doing right now is hiding
    #   the path's NEW files, beside files the repo keeps. That is #384's harm
    #   mechanism, and #384 proposed this same probe as its own option 3.
    #
    #   nothing tracked -> the rule may be ignoring the whole path deliberately, and
    #   this cannot tell that from residue. Still reported: a shield line over an
    #   untracked path is the ordinary case.
    #
    # ⚠️ Tracked content is NOT evidence of authorship, and an earlier revision of
    # this comment claimed it was ("the project demonstrably does NOT ignore it").
    # A project can ignore a path it already tracks content under — that is the
    # ordinary way an ignore rule is adopted for NEW files only, and `git add -f`
    # reaches the same state deliberately. `path_tracked`'s own docstring says a
    # force-added file under an ignore rule reads tracked here. So every bucket stays
    # review-before-deleting; only the described effect differs.
    #
    # A git that cannot answer degrades the hit into `no_tracked_content` — the
    # bucket that claims less — rather than dropping it, since the line is present
    # either way.
    hiding_new_files: list[str] = []
    maybe_neutralized: list[str] = []
    no_tracked_content: list[str] = []
    for line, line_bytes in hit_pairs:
        rel = line.strip("/")
        try:
            tracked = bool(rel) and path_tracked(project, rel)
        except GitError:
            tracked = False
        if not tracked:
            no_tracked_content.append(line)
        elif last_at[line_bytes] > last_negation:
            hiding_new_files.append(line)
        else:
            maybe_neutralized.append(line)
    return LegacyExcludePollution(
        exclude, hits, hiding_new_files, maybe_neutralized, no_tracked_content
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
        for line in (
            ".bmad-loop/runs/",
            ".bmad-loop/cache/",
            ".bmad-loop/policy.toml",
            f"{RENDER_DIR_REL}/",
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
