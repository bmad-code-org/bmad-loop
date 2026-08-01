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
import re
import shutil
import subprocess
from collections.abc import Iterable, Iterator, Sequence
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
#
# REPO-side surface only: the shim detector, and what missing_base_skills demands of an
# install. The worktree seed gate stopped sharing it — these two markers plus SKILL.md are
# 3 of the 13 files a real bmad-build-auto carries, so it asks walk parity with the repo instead
# (_absent_skill_files). A named tuple can be a floor for "is this install real"; it
# cannot be the answer to "did the copy deliver everything".
DEV_PRIMITIVE_NEW = "bmad-build-auto"
DEV_PRIMITIVE_LEGACY = "bmad-dev-auto"
DEV_PRIMITIVE_MARKERS = ("step-04-review.md", "customize.toml")

# The adapter roles whose skill tree is asked for the dev primitive, the review
# hunters that primitive invokes inline, and the renderer unit behind a stub SKILL.md
# — i.e. every question this module's skill checks ask. Triage is deliberately absent:
# its only prompt is `/bmad-loop-sweep`, which is in MODULE_SKILLS and is laid into
# that tree by `bmad-loop init`, so a triage-only CLI never needs one byte of the bmm
# module. Gating it anyway made `[adapter.triage] name = "gemini"` under a claude
# dev/review pair demand the whole module in `.agents/skills` — a hard preflight FAIL
# since the renderer checks became problems rather than warnings.
#
# This must stay the same set `Engine._worktree_profiles` provisions. A tree gated here
# that no worktree carries refuses runs over a skill no session will ever read; a tree
# provisioned but not gated ships a session into the `Unknown command` stall the
# preflight exists to catch. Neither has a defensible reading, so the two move together
# or not at all.
DEV_PRIMITIVE_ROLES: tuple[str, ...] = ("dev", "review")

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

# render_skill.py's sibling helper, and the second half of the same unit: upstream
# imports it at module scope off `sys.path[0]` with no try and no path fixup, so its
# absence is a bare ModuleNotFoundError raised ABOVE the renderer's own
# `except (ConfigError, RenderError, OSError, UnicodeError, ValueError)` guard.
# Measured: the real script alone in an empty dir exits non-zero with a traceback and
# NO `HALT: <error>` line. The stub's SKILL.md tells the agent to report the command
# output and halt either way, so what is actually lost is the structured diagnosis —
# the result-less Stop is identical, which is what makes this the same finding as an
# absent script rather than a new one. Required only when the INSTALLED script
# actually imports it (see _renderer_unit_required): both files are one commit old
# upstream (c2530ea5, BMAD-METHOD #2601), and this gate has no --force.
RENDERER_CONFIG_UTILS_REL = f"{BMAD_DIR}/scripts/config_utils.py"
RENDERER_CONFIG_UTILS_MARKER = "config_utils"
# The renderer's per-project script unit, in the order a finding should list it.
RENDERER_SCRIPT_UNIT_REL: tuple[str, ...] = (RENDERER_SCRIPT_REL, RENDERER_CONFIG_UTILS_REL)

# The bottom layer of the renderer's four-layer central config, and the only
# REQUIRED one (`config_utils.load_central_config` passes the required=True flag to
# its inner load_toml; the .user/custom layers above it are all optional). Absent,
# the renderer raises before it composes anything, so the stub's `uv run` exits with
# `HALT:` and the session Stops having written no spec — the same result-less failure
# RENDERER_SCRIPT_REL guards, one file further in, and blocking for the same
# reason. Project-global, not per tree.
CENTRAL_CONFIG_REL = f"{BMAD_DIR}/config.toml"

# The `provision_worktree` skipped-seed entry that reports the worktree's renderer
# support came up SHORT, rather than a seed that turned out to be a no-op. Shared with
# the engine because the entry is only half the story: whether a short seed matters at
# all depends on the resolved dev primitive being a renderer STUB, and provisioning is
# handed CLI profiles and a repo root — not the project root the primitive resolves
# against — so the engine owns the escalate-or-not decision and reads this sentinel back
# to make it. A magic string on either side would let the two drift silently apart, and
# the failure mode of that drift is a run that dispatches into guaranteed result-less
# Stops. See _bmad_scripts_seed_incomplete and Engine._run_isolated.
BMAD_SCRIPTS_SEED_REL = f"{BMAD_DIR}/scripts"

# Every sentinel above, i.e. the whole project-global surface the renderer needs to
# have REACHED THE WORKTREE. The engine intersects this rather than testing one
# constant, and names whichever member fired in the escalation reason — sending an
# operator to `_bmad/scripts` when the config is what went missing is exactly the
# drift the shared constants exist to prevent. The tuple is also the forgery filter
# in provision_worktree: these strings are reserved for the completeness checks, so a
# user `worktree_seed` entry that happens to spell one cannot pause a healthy run.
RENDERER_SEED_SENTINELS: tuple[str, ...] = (BMAD_SCRIPTS_SEED_REL, CENTRAL_CONFIG_REL)

# The SKILL-relative half of the renderer's surface — everything above is
# project-global. Since BMAD-METHOD #2601 `render_skill.py` composes the real prompt
# from `<skill>/workflow.md` and refuses to start without it (`render entry is
# missing`), then rewrites every `[[bmad-snapshot:<rel>]]` token into a path under the
# generation dir, raising `snapshot reference targets undeclared source` when the
# target is not one of that skill's OWN sources. Both exit `HALT:` — the same
# result-less Stop on every story that RENDERER_SCRIPT_REL and CENTRAL_CONFIG_REL
# guard, one layer further in. See _absent_renderer_sources.
RENDERER_ENTRY_REL = "workflow.md"
# A byte-for-byte mirror of the renderer's own `_SNAPSHOT_TOKEN`. Mirroring is the
# contract, not a convenience: a LOOSER pattern reports a token the renderer ignores
# outright — a false positive this gate cannot be talked out of, having no severity
# filter and no `--force` — and a STRICTER one misses a real HALT. Note the `.md`
# suffix is part of the pattern upstream, so a token naming anything else is not a
# snapshot reference at all and is left in the prompt as literal text.
SNAPSHOT_TOKEN_RE = re.compile(r"\[\[bmad-snapshot:([A-Za-z0-9_./-]+\.md)\]\]")

# Top-level _bmad/ entries never seeded into a worktree. render/ is the renderer's
# published output: it is regenerated on skill entry, and every snapshot dir name is
# keyed on a hash of the project root's absolute path, so seeding the main
# checkout's copy would carry ITS paths into the worktree and make every parallel
# session race on one shared tree.
RENDER_DIR_NAME = "render"
BMAD_SEED_EXCLUDES = (RENDER_DIR_NAME,)

# The one spelling of the renderer's output dir, shared by the two shields that keep
# it out of commits (`init`'s .gitignore line, the worktree exclude) and by
# `validate`'s tracked-output probe. They have to agree: a probe that looked
# somewhere the shields do not write would warn about a path nothing protects.
RENDER_DIR_REL = f"{BMAD_DIR}/{RENDER_DIR_NAME}"

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
# BMad Method (bmm) module installs them. Each must exist in every `trees` entry —
# callers pass the DEV_PRIMITIVE_ROLES trees, since only a dev or review session ever
# dispatches one of these — and carry its marker files (a half-installed or
# pre-automation skill is caught by the `bmad-loop validate` preflight). A tree only a
# triage adapter reads is never asked: nothing here is in its dispatch path.
# `{skill: (marker-rel-path, ...)}`.
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

    Every probe is the TOTAL :func:`_is_file` rather than ``Path.is_file()``, because
    this is not preflight-only: :func:`base_skills_seed_incomplete` resolves the era
    through :func:`dev_primitive_or_default` on every provision, so a raise here is a
    raise out of ``Engine._run_isolated`` — the one thing provisioning must never do
    (see :func:`_merge_traversable`). Measured: with a bare ``is_file()`` an unreadable
    ``bmad-build-auto`` dir in the repo took ``provision_worktree`` down with
    ``PermissionError`` through 3.13 while 3.14 answered ``False``. Falling through to
    the legacy name on a read fault is what 3.14 already did, and it is the right
    answer: a complete legacy install is dispatchable and the run drives it; when
    there is no usable legacy either, this returns None and the preflight refuses.
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


def _renderer_unit_required(project: Path, rel: str) -> bool:
    """True when the renderer INSTALLED in ``project`` actually needs unit member ``rel``.

    ``render_skill.py`` is the entry point and is unconditional. Its sibling is
    content-keyed on the script that would import it, the same way
    :func:`_is_renderer_stub` keys on SKILL.md: the import is a plain
    ``from config_utils import ...`` at module scope, so the script's own text is the
    only honest source of truth for whether the helper is load-bearing here.

    Why key it at all, when upstream's script does import it: this feeds a preflight
    with no severity filter and no ``--force`` — a false positive REFUSES every run
    with a remediation that cannot fix it. Both files are one commit old upstream, so
    if a later bmm inlines or renames the helper the import simply disappears and this
    gate goes quiet instead. Unreadable or absent script ⇒ False, fail-open for the
    same reason :func:`_is_renderer_stub` does: the script's own absence is already a
    finding, and blaming a sibling for a read fault names the wrong file.
    """
    if rel != RENDERER_CONFIG_UTILS_REL:
        return True
    try:
        script = (project / RENDERER_SCRIPT_REL).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return RENDERER_CONFIG_UTILS_MARKER in script


def _absent_renderer_sources(skill_dir: Path) -> list[str]:
    """Render sources ``skill_dir`` needs and does not carry, as POSIX rels.

    Two questions. They are not the whole set of ways ``render_skill.py`` can
    ``HALT:`` — a ``{{config.…}}`` naming a missing key is another (#407) — but both
    of these are answerable from the install alone, and each is a result-less Stop on
    EVERY story: the same environment fault ``skills.dev-renderer`` and
    ``skills.dev-renderer-config`` already block on, one layer further in:

    - :data:`RENDERER_ENTRY_REL` is a source (``render entry is missing``);
    - every ``[[bmad-snapshot:…]]`` target in any source is itself a source
      (``snapshot reference targets undeclared source``).

    The era conjunct is the first line of THIS function rather than a condition at
    the call site: a pre-#2601 inline ``SKILL.md`` legitimately has no ``workflow.md``
    and no step files, so asking it these questions refuses a healthy install. Kept
    inside so it is one deletable line no neighbouring leg can short-circuit.

    **The source set mirrors the RENDERER's enumeration, deliberately not the
    copier's** :func:`_walk_traversable_files`. The two disagree exactly where it
    matters: ``rglob`` does not descend a symlinked sub-directory and ``iterdir()``
    recursion does, so a ``review-prompts/`` pointed out of the repo holds files the
    copier would carry but the renderer will never see. This gate asks what
    ``render_skill.py`` will SEE, so borrowing the copier's walk here would invent
    sources the renderer does not have and turn a guaranteed HALT into a green.
    (:func:`_absent_skill_files` asks the other question — did the seed deliver what
    the repo has — and shares the copier's walk for the same reason.)

    Keyed by POSIX rel because upstream keys by ``relative_to(skill_dir).as_posix()``:
    a nested source is referenced as ``phases/plan.md``, never by its bare name, and a
    native-separator key would report every nested target as undeclared on Windows.
    ``SKILL.md`` is excluded at any depth, as upstream excludes it — a token naming it
    HALTs there and must be reported here.

    Deliberately not a fixed renderer-era file list — with ONE exception, named
    because it is one. The gate has no severity filter and no ``--force``, so a false
    positive refuses every run with a remediation that cannot fix it; asking only what
    the install DECLARES cannot refuse an upstream that renames or reorganizes its
    step files. :data:`RENDERER_ENTRY_REL` is the exception: upstream hardcodes that
    name too (``if "workflow.md" not in sources``), so mirroring it is exactly as
    stale-proof as upstream is — and no more.

    Every source is scanned, not just the entry, because upstream substitutes tokens
    across all of them. ``customize.toml`` is not, because upstream never loads it as
    a source and its ``instruction`` prose would otherwise be mined for tokens the
    renderer leaves as literal text — that is what the ``*.md`` filter is for. (Its
    other half, that a non-``.md`` file could never be a token TARGET, is a tautology
    of the token pattern rather than a guard.)

    Fidelity is bounded, and this is the whole boundary. Upstream additionally
    ``resolve(strict=True)``s each candidate and refuses one escaping the skill dir;
    this does not, so a source symlinked out of the skill directory reads as present
    here and ``HALT:``s there. That is a false GREEN — the safe direction for a gate
    with no ``--force`` — and it is the only one left. The ``is_file()`` conjunct is
    what keeps the rest out: without it a ``workflow.md`` that is a directory or a
    dangling symlink reads as a source (both a ``HALT:`` upstream), and a FIFO
    anywhere in the tree would block ``read_text`` forever — an unbounded hang in a
    preflight, which is neither a green nor a refusal. An unreadable source is
    likewise skipped rather than reported: also a HALT upstream, but a different one
    with a different remediation, and fail-open on a read fault is the doctrine
    :func:`_is_renderer_stub` and :func:`_renderer_unit_required` already follow.
    """
    if not _is_renderer_stub(skill_dir):
        return []
    sources = {
        path.relative_to(skill_dir).as_posix(): path
        for path in skill_dir.rglob("*.md")
        if path.name != "SKILL.md" and path.is_file()
    }
    absent = [] if RENDERER_ENTRY_REL in sources else [RENDERER_ENTRY_REL]
    for path in sources.values():
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        absent.extend(t for t in SNAPSHOT_TOKEN_RE.findall(body) if t not in sources)
    return sorted(set(absent))


def renderer_stub_resolved(project: Path, trees: Sequence[str]) -> bool:
    """True when the dev primitive ANY active tree resolves to is a renderer stub
    (BMAD-METHOD #2601) — i.e. some session this run spawns will compose its prompt by
    shelling out to ``_bmad/scripts/render_skill.py`` instead of reading it inline.

    The public form of the ``resolved_stub`` flag :func:`missing_base_skills` folds
    into ``skills.dev-renderer``/``skills.dev-renderer-config``, lifted out for the
    caller that needs the answer without the findings: ``Engine._run_isolated``, which
    asks of a WORKTREE's seeded ``_bmad/`` the question the preflight already asked of
    the main checkout. Sharing the predicate rather than restating the marker test is
    what keeps the run-time gate era-agnostic in the same way the preflight is — on a
    pre-#2601 inline SKILL.md nothing ever reads ``_bmad/scripts/``, so a missing
    renderer is not a finding to make about that project, at either layer.

    ANY over the trees, deduped exactly like :func:`missing_base_skills`: resolution is
    per tree (one run can mix ``.claude/skills`` and ``.agents/skills``, and an operator
    can upgrade the bmm module in one and not the other), and a single stubbed tree is
    enough for one of the run's sessions to need the renderer.

    False when nothing resolves. An unresolvable tree is :func:`missing_base_skills`'
    story (``skills.base-missing``/``skills.base-shim``) and has already refused the run
    at preflight, so no caller here invents a second answer for it; an empty ``trees``
    (an adapter with no profile) has no tree to shell out from at all."""
    for tree in dict.fromkeys(trees):
        resolved = resolve_dev_primitive(project, tree)
        if resolved is not None and _is_renderer_stub(project / tree / resolved):
            return True
    return False


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
    the three review hunters it invokes inline — adversarial-general,
    edge-case-hunter, and verification-gap — are installed by the BMad Method
    module, not by `bmad-loop init`. Each must exist in every ``trees`` entry —
    callers pass the :data:`DEV_PRIMITIVE_ROLES` trees, since only a dev or review
    session ever dispatches one of these — and carry its marker files. Returns one
    problem :class:`Finding` per missing/incomplete skill; empty list means OK. Run
    as a preflight so a missing skill fails loudly with remediation instead of
    stalling as an `Unknown command` until the run times out.

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

    - ``skills.dev-renderer`` — the stub is there, at least one member of the
      renderer's script unit (:data:`RENDERER_SCRIPT_UNIT_REL`) is not. Per tree,
      since the primitive resolves per tree. ONE id for both members: a missing entry
      point and a missing sibling are the same result-less Stop with the same
      remediation, and ``checks.py`` splits ids only where the two outcomes are
      genuinely different findings. Which ones are absent is the ``missing_scripts``
      detail — project-relative rels, hence not the skill-relative ``missing_markers``
      key above.
    - ``skills.dev-renderer-config`` — a stub resolved *somewhere* and the project has
      no ``_bmad/config.toml``. Once per project (the central config is
      project-global), and gated on a stub having resolved so the check stays
      era-agnostic: a pre-#2601 inline SKILL.md never reads the file, so its absence
      is not a finding to make about that project.
    - ``skills.dev-renderer-sources`` — a stub resolved and the SKILL's own render
      sources are short: no ``workflow.md`` entry, or a ``[[bmad-snapshot:…]]`` token
      naming something the renderer will not load as a source — usually an absent
      file, but ``SKILL.md`` and a directory named ``*.md`` count too
      (:func:`_absent_renderer_sources`). Per tree, and era-gated inside the helper.
      Without it the marker pair answered for thirteen files: a truncated repo-side
      install passed ``validate`` and then HALTed every session. ``missing_sources``
      carries the rels, SKILL-relative like ``missing_markers`` — not
      ``missing_scripts``, which is project-relative because its files are.

    All three are emitted independently of the marker check above them — different
    files, different remediations, and a wholly-absent ``_bmad/`` legitimately earns
    both project-global lines beside a truncated skill.
    """
    problems: list[Finding] = []
    # Same predicate as :func:`renderer_stub_resolved`, kept inline here rather than
    # delegated: this loop needs the per-tree `resolved`/`skill_dir` to attribute the
    # `skills.dev-renderer` finding to a tree, which a bare any() discards.
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
                            f"{tree}/{resolved}/SKILL.md renders via {RENDERER_SCRIPT_MARKER} "
                            f"but the renderer script unit is incomplete (missing "
                            f"{', '.join(absent_scripts)}) — the session would HALT without "
                            f"writing a spec; reinstall the BMad Method (bmm) module",
                            {"tree": tree, "skill": resolved, "missing_scripts": absent_scripts},
                        )
                    )
            # Unconditional: the era question lives inside the helper, so this leg
            # cannot be short-circuited by the one above it and a future edit to the
            # era test has one place to go rather than a condition here to keep in
            # step. (The `if _is_renderer_stub` above is that test's other reader —
            # they are two call sites of one predicate, not two predicates.)
            absent_sources = _absent_renderer_sources(skill_dir)
            if absent_sources:
                problems.append(
                    Finding(
                        "skills.dev-renderer-sources",
                        "problem",
                        f"{tree}/{resolved} renders via {RENDERER_SCRIPT_MARKER} but its "
                        f"render sources are incomplete (unresolved: "
                        f"{', '.join(absent_sources)}) — the session would HALT without "
                        f"writing a spec; reinstall the BMad Method (bmm) module",
                        {"tree": tree, "skill": resolved, "missing_sources": absent_sources},
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


def _walk_traversable_files(
    src, rel: str = "", _seen: frozenset[str] = frozenset()
) -> Iterator[tuple[str, object]]:
    """Yield ``(rel, traversable)`` for the leaves of ``src``: every entry this walk
    neither descends into nor has already covered under another rel.

    The one walk both halves of the skills seed share: :func:`_merge_traversable`
    copies what this yields and :func:`_absent_skill_files` asks the worktree for the
    same rels. Sharing is the whole point, not tidiness. ``Path.rglob``/``**`` does
    **not** descend a symlinked sub-directory while ``iterdir()`` + ``is_dir()``
    does, so a parity check that merely *mirrored* the copier with an rglob would
    walk straight past a ``review-prompts/`` symlinked to a shared install — the
    exact drop the gate exists to report. Any future edit to how the copier
    recurses is therefore an edit to what the gate asks for, by construction.

    Traversal only: every containment decision stays with the caller, so this needs
    no ``repo_root`` and answers the same for wheel package data as for a filesystem
    tree (``.iterdir()``/``.is_dir()`` are the Traversable API, so a zip-imported
    package walks too). Sorted per level, so a copy order and a report order are
    both deterministic.

    ``rel`` is built by concatenation and is POSIX on every platform — never
    ``relative_to``, whose separator is the host's, which would make the reported
    rels (and the exclude patterns keyed off them) diverge on Windows. Map back with
    ``joinpath(*rel.split("/"))``; a file source yields ``""``, and
    ``joinpath("")`` is a no-op, so it still names ``dst`` itself.

    An entry that is neither file nor directory — a dangling symlink, a FIFO — is
    yielded like any other leaf, so the caller's guards see exactly what the old
    recursion handed them. By the same rule an ABSENT ``src`` yields one ``("", src)``
    pair rather than nothing: "not a directory" is the only question asked, and a file
    source has to yield itself for :func:`_merge_traversable` to copy it. That pair
    used to need a guard in front of every caller to keep it from becoming a bogus
    ``""`` rel; it no longer does, because the consumers grew their own
    ``_is_file``/``_is_dir`` filters and an absent path answers False to both. Measured
    (A18): deleting the ``BASE_SKILLS`` loop's ``src_root`` dir-guard now reddens
    NOTHING — it bounds the walk and nothing more, which is the whole reason it is
    still safe to keep. ⚠️ Do not read the same of
    :func:`base_skills_seed_incomplete`'s ``SKILL.md`` precondition, which this walk is
    now called on BOTH sides of: deleting it reddens 16 tests that predate this phase,
    because it is the absent-from-the-repo branch itself and not a guard on the walk.

    An entry the walk cannot see INSIDE is yielded as a LEAF rather than raising, and
    that single choice is what makes an unreadable subtree reportable instead of fatal.
    Two faults reach it, and :func:`_is_dir` is what folds them into one case: a
    directory that cannot be listed (``iterdir`` raises), and an entry that cannot be
    CLASSIFIED at all, which is what a directory readable-but-not-searchable (mode
    ``0o444``) makes of every child — ``iterdir`` succeeds and then each child's own
    ``stat`` is refused. The consumers then split on one predicate: the copier takes
    ``_is_file`` alone (nothing it cannot confirm is deliverable content), the gates
    take ``_is_file or _is_dir``, and the only yielded entry that answers the second
    alone is one of those two — a descendable directory is recursed into, and a cycle
    repeat is dropped with no rel at all, its content having already been yielded under
    the ancestor that closed the loop. So the seed skips it and the gate names it.
    ⚠️ Raising instead takes an unreadable repo skill out through
    :meth:`Engine._run_isolated`, which has no ``try`` around provisioning, killing the
    whole RUN rather than pausing on one story's named escalation (measured:
    ``crash.txt`` + ``state.crashed``).

    ``_seen`` carries the resolved real path of every directory on the CURRENT branch
    so a symlink CYCLE terminates. ``rglob`` needs no such guard because it does not
    descend child symlinks; descending them is this walk's whole reason to exist, and
    it makes ``_bmad/scripts/loop -> ..`` unbounded. Measured before the guard, on the
    fixture the cycle test still carries: 42 files, every one passing containment
    because ``resolve()`` collapses the loop back inside the repo, terminating only by
    accident when the kernel's ``ELOOP`` made ``is_dir()`` answer False — at a depth
    that moves with the absolute path, which is what the limit actually bounds. Branch-local, not global: two sibling symlinks pointing at one shared
    install are not a cycle, and one set shared across the whole walk would silently
    drop the second one's rels from both the copy and the gate.  Real paths only for
    ``Path`` sources — a zip-imported Traversable has no ``resolve()`` and no way to
    make a cycle.

    Lazy across levels and it must stay that way, so a caller that stops early does not
    pay for the rest of the tree. (Within one level ``sorted()`` has always been eager;
    ``Path.iterdir()`` is a generator over ``os.listdir`` through 3.12, deferring the
    read to the first ``next()``, and performs an eager ``os.scandir`` from 3.13 —
    wrapping it in ``sorted()`` makes that difference invisible.)
    """
    if _is_dir(src):
        real = str(src.resolve()) if isinstance(src, Path) else None
        if real is not None and real in _seen:
            return
        try:
            children = sorted(src.iterdir(), key=lambda entry: entry.name)
        except OSError:
            yield rel, src
            return
        inner = _seen if real is None else _seen | {real}
        for child in children:
            yield from _walk_traversable_files(
                child, f"{rel}/{child.name}" if rel else child.name, inner
            )
        return
    yield rel, src


def _is_file(path) -> bool:
    """``Path.is_file()`` made TOTAL: False rather than an exception on an IO fault.

    ``Path.is_file()`` is not total, and not even consistently non-total across the
    interpreters this project supports. Measured on 3.11/3.12/3.13/3.14: probing a file
    whose PARENT directory is not searchable **raises** ``PermissionError`` through 3.13
    — ``pathlib``'s ``_IGNORED_ERRNOS`` is ``(ENOENT, ENOTDIR, EBADF, ELOOP)`` and
    ``EACCES`` is not in it — while 3.14 routes through ``os.path.isfile`` and answers
    ``False``. Both readings are wrong here in different ways, and a check that crashes
    on three of the four supported interpreters and goes quiet on the fourth is worse
    than either.

    Answers the caller's question, not the filesystem's: **nothing we cannot confirm is
    a usable file**. That is true whether the file is absent, is a directory, or sits
    behind a door we cannot open, and all three are the same fact for the session that
    has to read it — and for the copier, which has no bytes it can promise to deliver.
    Its sibling :func:`_is_dir` folds the same fault the other way for the same reason;
    together they are the rule that when a probe cannot answer, the seed stays cautious
    and the gate stays loud.

    Where the REPO side is probed this way the reading is deliberate rather than
    incidental, because there False can mean "the project legitimately lacks this" and
    must not swallow an unreadable install: :func:`resolve_dev_primitive` picks a name
    and a raise from it lands in a provision; :func:`_bmad_scripts_seed_incomplete` and
    :func:`_central_config_seed_incomplete` go quiet on a ``_bmad/`` that cannot be read
    only because the run-start preflight has already refused it (see
    :func:`_seed_bmad_tree`); and :func:`base_skills_seed_incomplete` splits the two
    apart explicitly rather than letting False stand for both.

    ⚠️ Only the ≤3.13 lanes exercise the ``except``. On 3.14 the wrapped call is already
    total, so ablating this wrapper reddens nothing there — measured, 6 reds on 3.13 and
    0 on 3.14, so the CI matrix and not the local suite is what covers both readings.
    That is safe HERE only because both readings answer False; :func:`_is_dir` needed
    more than a wrapper precisely because its two do not agree. Unannotated for the same
    reason
    :func:`_copy_traversable` is: the walk yields wheel Traversables as well as Paths.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path) -> bool:
    """``Path.is_dir()`` made TOTAL — and True, not False, when it cannot answer.

    The opposite fallback to :func:`_is_file`, because it answers the opposite
    question: not "can this be delivered" but **"is there something here the walk has
    not seen inside"**. An entry whose ``stat`` is refused might hold a whole subtree;
    calling it False would make it indistinguishable from an absent path, and that
    collapse is the exact silent failure the completeness gate exists to prevent (see
    :func:`base_skills_seed_incomplete`). Calling it True costs one ``iterdir`` that
    then fails into the walk's own ``except`` and yields the entry as a leaf — so every
    unclassifiable entry arrives at the consumers in the shape they already handle, and
    the gate names it.

    The fault is not exotic. A directory readable but NOT searchable — mode ``0o444``,
    or ``chmod -R a-x`` — lets ``iterdir()`` succeed and then refuses the ``stat`` on
    every child; measured, that took ``provision_worktree`` down with ``PermissionError``
    on 3.11/3.12/3.13 while 3.14 answered False and dropped the subtree silently. Mode
    ``0o555`` is unaffected, and the suite carries a witness for each so the two are not
    confused again.

    What the report can say degrades with what the filesystem allows, which is honest in
    both directions: a directory that cannot be LISTED is named as one rel, because
    nobody can say which files are short; one that can be listed but not stat'd through
    is named file by file, because ``iterdir`` did give up the names.

    ⚠️ Unlike :func:`_is_file` this CANNOT be a bare ``try``/``except``, because the two
    interpreter readings do not agree on which answer is the safe one: ≤3.13 raises
    where 3.14 returns ``False``, and here ``False`` IS the silent collapse rather than
    the cautious reading. Measured on the wrapper alone, both ``0o444`` witnesses passed
    on 3.11/3.12/3.13 and FAILED on 3.14 — the seed dropped the subtree and the gate
    reported nothing, which is the pre-fix 3.14 behaviour with the ``except`` merely
    made unreachable. :func:`_probe_refused` is what makes the four lanes agree.
    """
    try:
        if path.is_dir():
            return True
    except OSError:
        return True
    return _probe_refused(path)


# What ``pathlib`` itself reads as "this path is simply not there" when a probe fails.
# A local copy of its ``_IGNORED_ERRNOS``, which is private and moved in 3.13. Anything
# outside it — ``EACCES`` above all — is the filesystem REFUSING to answer, which is a
# different fact and the one :func:`_is_dir` must not swallow.
_ABSENCE_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def _probe_refused(path) -> bool:
    """True when a ``False`` out of ``is_dir()`` means REFUSED rather than ABSENT.

    3.14 made ``is_dir()``/``is_file()``/``exists()`` total by answering ``False`` for
    a fault the older interpreters raise on, which collapses "there is nothing here"
    into "we were not allowed to look" — and :func:`_is_dir` exists precisely to keep
    those two apart. ``stat()`` is the probe that still tells them apart on every
    supported version: measured on 3.11/3.12/3.13/3.14, ``Path.stat()`` raises
    ``PermissionError`` on an entry whose parent is unsearchable while 3.14's
    ``is_dir()`` answers ``False``, so re-asking a ``False`` of ``stat()`` recovers the
    distinction without special-casing the version.

    Only real ``Path`` sources are re-asked. The walk yields wheel Traversables too;
    a zip-imported tree has no ``stat`` and no permission fault to find (the same
    reason its cycle guard skips them — see :func:`_walk_traversable_files`).
    """
    if not isinstance(path, Path):
        return False
    try:
        path.stat()
    except OSError as exc:
        return exc.errno not in _ABSENCE_ERRNOS
    except ValueError:
        # An invalid path (embedded null): `is_dir()` swallows it on every lane, and so
        # does this. Not a refusal, and re-raising would take provisioning out by the
        # one door it must not use.
        return False
    return False


def _occupied(path: Path) -> bool:
    """True when ``path``'s slot is taken by ANYTHING — including a dangling symlink.

    ``Path.exists()`` follows symlinks, so a dangling one answers False while the name
    is very much taken. Writing anyway does one of two measured things, neither of them
    the intended copy: when the link resolves to a nonexistent path INSIDE the worktree
    ``shutil.copy2`` follows it and lands the bytes at the link's target instead of at
    the slot, leaving an untracked file for the unit's ``git add -A`` to commit while
    the completeness gate reads green through the now-live link; when the dangling link
    is an intermediate DIRECTORY component instead, ``mkdir(parents=True,
    exist_ok=True)`` raises ``FileExistsError`` (its recovery is ``if not exist_ok or
    not self.is_dir(): raise``, and ``is_dir()`` is False on a dangling link).

    Treating it as occupied keeps provisioning's one promise — never clobber what the
    checkout carries — for a committed symlink whose target does not exist on this
    machine, which is a thing git stores and checks out happily. The gates then name
    the rel, because ``is_file()`` is False through a dangling link too, and the
    escalation prose already reaches for that exact cause.

    What the ``try`` buys is TOTALITY, not the answer inside it: ``exists()`` raises on
    an unreadable parent through 3.13 (3.14 answers False — see :func:`_is_file`), and
    that raise would leave provisioning by the one door it must not use. Answering
    ``True`` there is the conservative reading — a slot we cannot even stat is not one
    to write through, and skipping something writable costs a reported rel where writing
    through something we misread costs a file — but it is not observably different from
    ``False`` through any caller: the write that would follow is into the very directory
    that could not be stat'd, so it fails into the caller's leaf ``except`` and the gate
    names the same rel either way. The witnesses assert that outcome rather than this
    return value, which is also why they hold on 3.14, where the ``except`` is dead.
    """
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


def _merge_traversable(src, dst: Path, worktree: Path, repo_root: Path | None = None) -> None:
    """Copy a resource tree into a worktree, copy-when-absent per FILE.

    The merge half of :func:`_copy_traversable`, and the same reason
    :func:`_seed_bmad_tree` merges rather than replaces: a skill dir is one
    multi-file unit, so a DIRECTORY-level skip is silent and total — a checkout
    carrying one file of that dir would receive zero bytes of the rest, pass a
    directory-granular completeness check, and dispatch a session whose step files
    are absent. Walks :func:`_walk_traversable_files` rather than ``rglob`` so a
    zip-imported wheel works, and creates parents lazily at the leaf so an empty
    source dir is not recreated.

    ``dst`` containment is always checked: a symlink in the CHECKOUT pointing out of
    the worktree must not be written *through*. ``repo_root`` is passed only for
    real-filesystem sources, where a child symlinked to a shared install outside the
    repo must not be read through — wheel package data has no such leg to check, and
    is not a ``Path`` to resolve in the first place.

    **Every reason one file cannot land is a per-file ``continue``, never a raise.**
    Provisioning has no failure vocabulary of its own; the honesty comes from
    :func:`base_skills_seed_incomplete` re-asking the RESULT afterwards, which the
    engine turns into a named CRITICAL escalation — ``Phase.ESCALATED`` plus notify
    plus ``RunPaused``, unconditionally, with every rel in the reason line. A raise
    bypasses all of it: measured, an ``OSError`` here escapes ``Engine._run_isolated``
    uncaught (the one ``try`` above the provision catches ``verify.GitError``), past
    the completeness gate that would have named the file, and ends the whole run with
    ``crash.txt`` and a half-provisioned worktree. The pause is resumable and says what
    to fix; the crash is neither. Three legs enforce that, and they fail at three
    different calls:

    - :func:`_is_file` — a source that is not a confirmed regular file has no bytes to
      deliver. A dangling symlink raises ``FileNotFoundError`` out of ``shutil.copy2``;
      a FIFO raises ``SpecialFileError`` (``copyfile`` stats both ends before opening
      either, so this is a crash, not the unbounded read a FIFO causes where something
      calls ``read_text`` on it).
    - :func:`_occupied` — the destination slot is taken, dangling symlinks included.
    - ``try/except OSError`` around the write — an unreadable source file, an
      unwritable destination, an intermediate dangling-symlink directory, a source
      that vanished mid-walk. This one is not redundant with the other two: measured,
      ``target.is_symlink()`` is False when it is the target's PARENT that dangles, so
      the ``mkdir`` still raises and only the ``except`` catches it.

    The asymmetry with the gate is deliberate and is exactly one predicate wide. This
    filters :func:`_is_file`; :func:`_absent_skill_files` filters ``_is_file or
    _is_dir``. The difference is a directory the walk could not see inside: not
    deliverable, so the copier skips it, and genuinely missing from the worktree, so
    the gate names it.

    ⚠️ Bounded: only the ``BASE_SKILLS`` caller has a completeness gate behind it. A
    per-file skip on the ``MODULE_SKILLS`` (wheel) leg is not reported by anything —
    pre-existing, and the reporting channel belongs to the phase that owns ``skipped``.
    """
    for rel, child in _walk_traversable_files(src):
        if not _is_file(child):
            continue
        target = dst.joinpath(*rel.split("/"))
        if repo_root is not None and not Path(str(child)).resolve().is_relative_to(repo_root):
            continue
        if not target.resolve().is_relative_to(worktree) or _occupied(target):
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_traversable(child, target)
        except OSError:
            continue


def _worktree_local_exclude(worktree: Path, patterns: Sequence[str]) -> None:
    """Add anchored ignore patterns to the git exclude that covers the worktree, so the
    provisioned tool files are never staged by the unit's `git add -A`. Uses git's
    standard local-only exclude (never committed or pushed); it does not affect
    already-tracked files. Best-effort — skipped if git can't be queried.

    ⚠️ "Local" is not "private". `info/exclude` lives under `--git-common-dir`, which a
    linked worktree SHARES with the main checkout (measured: `rev-parse
    --git-common-dir` from inside a linked worktree answers the main repo's `.git`), and
    nothing in this codebase ever prunes a line from it. So a pattern written for one
    story's worktree also hides that path from the operator's own `git status` — and
    keeps hiding it after the worktree is gone. That is only harmless because every
    pattern here names a path a bmad-loop project is expected to gitignore anyway
    (`.claude/`, `.agents/`, `.mcp.json`, `_bmad/`); for a project that deliberately
    tracks one of them, the shield is over-broad in the main checkout.

    Callers therefore keep lines OUT of this file wherever they can rather than writing
    every pattern that might apply — see the `_bmad/render/` gate in
    :func:`provision_worktree`. Making the shield genuinely per-worktree needs a private
    `<worktree-gitdir>/info/exclude` plus a worktree-scoped `core.excludesFile` behind
    `extensions.worktreeConfig`; that is a repo-format change with its own failure modes
    and is not something a patch release should introduce.
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

    Walks the skills copier's :func:`_walk_traversable_files` rather than ``rglob``, so
    ``_bmad/`` gets the seed semantics the skill trees already have. ``rglob`` does not
    descend a symlinked SUB-directory, which is how a shared BMAD install is commonly
    wired one level in (``_bmad/scripts/lib -> /opt/...``): measured, the whole subtree
    was copied as nothing while :func:`_bmad_scripts_seed_incomplete` — mirroring the
    same rglob — agreed that nothing was missing. The under-seed and its own detector
    were wrong together, which is why both moved to the shared walk in one change and
    not one at a time. A sub-directory symlinked to a real dir INSIDE the repo is now
    genuinely seeded; one pointing outside is still dropped file-by-file by the
    containment guard, but the gates can now SEE it — ⚠️ which is the whole reporting
    surface for ``_bmad/``, and it is two rels wide: :data:`RENDERER_SEED_SENTINELS` is
    ``scripts`` and ``config.toml``. A symlinked-out or unreadable ``_bmad/<anything
    else>/`` is walked and dropped exactly as before, and nothing names it.

    The per-level ``iterdir()`` here is the walker's, so an unreadable ``_bmad/`` root
    is the one ``iterdir`` left outside it — hence its own ``try``. Returning ``[]``
    rather than raising is the same doctrine as the leaf below, and in the renderer era
    no run reaches provisioning with a ``_bmad/`` it cannot read: the run-start
    preflight sees the same fault two files in, since ``render_skill.py`` is then not a
    readable file either. ⚠️ Measured, it says so in two different shapes — on 3.14
    ``missing_base_skills`` returns ``skills.dev-renderer`` + ``-config`` and refuses;
    through 3.13 its own bare ``is_file()`` probes raise ``PermissionError`` out of
    ``validate``/``run`` instead. Loud either way, so nothing proceeds, but the
    traceback lane is a finding wearing the wrong clothes; :func:`_is_file` is the fix
    and this phase applied it only where a raise would have escaped *provisioning*.
    ⚠️ Both preflight legs are era-gated on :func:`_is_renderer_stub`, so a pre-#2601
    INLINE primitive passes them and reaches provisioning with an unreadable ``_bmad/``
    — where this returns ``[]`` and both sentinel predicates go quiet. Harmless in that
    era, which never reads the tree, but it is why the safety here is the era's and not
    the function's.

    Ordering is by entry NAME at every level, the walker's key. The old code sorted
    ``Path`` objects at BOTH levels (``sorted(src_root.iterdir())`` over
    ``sorted(top.rglob("*"))``), so the two levels agreed with each other and disagreed
    with the rest of the seed: ``Path`` ordering is case-INSENSITIVE on Windows and a
    bare name sort is not, and a flat ``rglob`` sort interleaves depths besides. This
    now orders every level the way the skills walk does. Nothing in production reads
    the order — the sole call site's two consumers are a truthiness test and a set
    union that is re-``sorted`` before use — but a report should not be sorted two ways
    in one list.

    Returns the rels to shield from the unit's ``git add -A``: the single
    ``_bmad`` root when the worktree had none (everything under it is ours), or
    the individual seeded files when merging into a checkout that already had one
    — shield exactly what we wrote.
    """
    src_root = repo_root / BMAD_DIR
    if not _is_dir(src_root):
        return []
    dst_root = worktree / BMAD_DIR
    had_bmad = _is_dir(dst_root)
    seeded: list[str] = []
    try:
        tops = sorted(src_root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    for top in tops:
        if top.name in BMAD_SEED_EXCLUDES:
            continue
        for rel, src in _walk_traversable_files(top, top.name):
            if not _is_file(src):
                continue
            dst = dst_root.joinpath(*rel.split("/"))
            if not src.resolve().is_relative_to(repo_root) or not dst.resolve().is_relative_to(
                worktree
            ):
                continue
            if _occupied(dst):
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_traversable(src, dst)
            except OSError:
                continue
            seeded.append(f"{BMAD_DIR}/{rel}")
    if not seeded:
        return []
    return [BMAD_DIR] if not had_bmad else seeded


def _bmad_scripts_seed_incomplete(worktree: Path, repo_root: Path) -> bool:
    """True when the repo carries the renderer but the worktree's ``_bmad/scripts/``
    is missing at least one file the repo's has.

    That seed is the only thing between a renderer-era install and a result-less
    Stop, and it can come up short *without failing*. The realistic trigger is a
    **symlinked** ``_bmad/`` or ``_bmad/scripts/`` — how a shared BMAD install is
    wired. A symlinked ``scripts`` dir is walked when it is the walk's root, so every
    file under it reaches the resolve-and-contain guard and, resolving outside
    ``repo_root``, is dropped one by one. The destination lands empty while
    provisioning otherwise reports success — a partial ``_bmad/scripts/`` is worse than
    none, because the bare ``config_utils`` import fails above the renderer's
    try/except and even the ``HALT:`` line is lost.

    Asks :func:`_walk_traversable_files`, the same walk :func:`_seed_bmad_tree` now
    copies with, for the same reason :func:`_absent_skill_files` shares the skills
    copier's: a detector that merely *mirrors* the seed with its own traversal can be
    wrong in the seed's own direction and confirm it. Measured before the change, with
    both halves on ``rglob``: ``_bmad/scripts/lib`` symlinked to a shared install, or
    simply unreadable, was copied as nothing AND reported as complete. Two rglobs
    agreeing is not a check.

    Filters ``_is_file or _is_dir`` where the seed filters :func:`_is_file` alone — the
    walker's contract, and the one predicate of difference is a directory the walk
    could not see inside: the seed cannot deliver it and this must not stay quiet
    about it.

    Reported through :func:`provision_worktree`'s existing skipped-seed return
    channel, as the :data:`BMAD_SCRIPTS_SEED_REL` entry, rather than raising —
    provisioning has no failure vocabulary of its own (every containment violation
    is a bare ``continue``), and the decision this drives is escalate-and-pause plus
    notify, which belongs to the caller that owns the state machine and the journal.

    This answers only whether the seed came up SHORT — never whether that matters.
    It cannot tell: the repo-side ``render_skill.py`` probe fires for any project
    carrying a renderer-era ``_bmad/scripts/``, including one whose resolved dev
    primitive is a pre-#2601 INLINE ``SKILL.md`` that never reads the renderer at all.
    So the engine escalates only when :func:`renderer_stub_resolved` also holds; on an
    inline primitive the short seed is journaled as an ordinary
    ``worktree-seed-skipped`` and the run proceeds. Where it does hold, this is not a
    degraded provision the run can carry — it is an environment fault identical for
    every story, so dispatching would burn the whole backlog on result-less Stops.
    """
    if not _is_file(repo_root / RENDERER_SCRIPT_REL):
        return False
    scripts = repo_root / BMAD_DIR / "scripts"
    dst_scripts = worktree / BMAD_DIR / "scripts"
    return any(
        (_is_file(src) or _is_dir(src)) and not _is_file(dst_scripts.joinpath(*rel.split("/")))
        for rel, src in _walk_traversable_files(scripts)
    )


def _absent_skill_files(repo_skill: Path, worktree_skill: Path) -> list[str]:
    """Every file of one repo skill dir that did not reach the worktree, as POSIX rels.

    Parity with the COPIER, over the copier's own :func:`_walk_traversable_files` —
    the question is "did the seed deliver what the repo has", not "does the worktree
    carry a named list". Asking the shared walk rather than an rglob mirror of it is
    load-bearing, not stylistic: rglob does not descend a symlinked sub-directory, so
    a ``review-prompts/`` pointed at a shared install would be dropped by the copy and
    invisible to the check at once.

    **This reverses the rationale this function used to carry**, which rejected parity
    because it "would halt the whole backlog over a repo-only ``README.md`` no session
    reads, with no remedy available to the operator". That objection was retired by the
    per-FILE merge in the same commit that stated it: :func:`_merge_traversable` filters
    nothing by name, so a repo-only ``README.md`` is *delivered* like everything else
    and parity stays silent about it. What parity can flag is exactly the set of files
    the seed FAILED to deliver — a source symlinked out of the repo, a destination
    symlink escaping the worktree, a destination occupied by a directory — and every
    one of those has an operator remedy.

    The old named surface (``SKILL.md`` plus the :data:`BASE_SKILLS` markers) was 3 of
    the 13 files a real ``bmad-build-auto`` install carries, and the other ten are not
    inert: since BMAD-METHOD #2601 ``workflow.md`` is the renderer's entry point and
    names its step files as ``[[bmad-snapshot:…]]`` sources, so a dropped one makes
    ``render_skill.py`` print ``HALT:`` and the session Stop having written nothing —
    on every story, which is the environment-fault shape this gate reports. Keeping the
    surface in sync by hand-extending a marker tuple was the maintenance debt; the walk
    has no list to fall behind.

    :data:`DEV_PRIMITIVE_MARKERS` is untouched by the reversal — it is still the shim
    detector and still what :func:`missing_base_skills` demands of the repo. Only the
    worktree gate's question changed.

    The repo-has-it conjunct is now STRUCTURAL: the walk enumerates the repo, so a
    project whose install predates a file is never asked for something no copy could
    have delivered. ``is_file`` rather than ``exists`` on the destination: a slot
    occupied by a directory is not the file the skill needs, and a slot occupied by a
    DANGLING symlink is not either — ``is_file()`` is False through one, which is what
    lets :func:`_merge_traversable` decline to write through it and this name the rel.

    The source filter is ``_is_file or _is_dir`` — the copier's :func:`_is_file` plus
    exactly one more case, and the ONLY yielded entry that can answer the second alone
    is a directory :func:`_walk_traversable_files` could not see inside (a descendable
    one is recursed into; a cycle repeat is dropped with no rel). That one predicate is
    the whole difference between the two halves of the seed, and it is the right
    difference in both directions:

    - a dangling symlink or a FIFO in the repo is dropped by BOTH, so a broken link the
      copy could never have followed does not halt the backlog. What the repo's own
      CONTENT should be is :func:`missing_base_skills`' question at run start
      (``skills.base-missing``/``-incomplete``/``-shim``, ``skills.dev-renderer-sources``
      — deliberately un-counted here, since a new check id must not stale this line);
      this one asks only whether the copy delivered what the repo has.
    - a directory the walk could not see inside is dropped by the copier and named
      here, because the repo does have content there and the worktree did not get it.
      When it could not be LISTED the rel names the directory, which is all anyone
      honestly knows; when it could be listed but not stat'd through (mode ``0o444``)
      the rels name the individual children, because ``iterdir`` did give up the names.

    Every probe is total — :func:`_is_file`/:func:`_is_dir` rather than the raw calls —
    on both sides: the worktree slot can be unreadable, and so can the repo entry the
    walk just handed back (see :func:`_is_dir`).
    """
    return [
        rel
        for rel, src in _walk_traversable_files(repo_skill)
        if (_is_file(src) or _is_dir(src))
        and not _is_file(worktree_skill.joinpath(*rel.split("/")))
    ]


def base_skills_seed_incomplete(worktree: Path, repo_root: Path, trees: Sequence[str]) -> list[str]:
    """The rels the repo resolves but the worktree does not carry — a whole skill dir,
    or a required file inside one.

    The skills half of the same shape :func:`_bmad_scripts_seed_incomplete` reports for
    the renderer, and the one containment leg nothing covered. `provision_worktree`
    copies BASE_SKILLS from the main repo's tree behind a
    ``src.resolve().is_relative_to(repo_root)`` guard whose two legs are not alike:

    - the skill is genuinely absent from the repo — the run-start preflight
      (:func:`missing_base_skills`) already refused the run, so skipping is right;
    - the skill IS there but is a **symlink to a shared install outside the repo** —
      how a shared BMad install is wired, and the same trigger as the ``_bmad/`` case.
      Then the guard drops it while the preflight, which stats through the link,
      passes. Measured: a symlinked `.claude/skills/bmad-build-auto` leaves
      `missing_base_skills` empty and `resolve_dev_primitive` naming the primitive,
      while the worktree receives NOTHING and provisioning reports success.

    The session then dispatches a skill its worktree does not have — the `Unknown
    command` stall the preflight exists to prevent, reached anyway by a project that
    passed it. Unlike the renderer sentinels this needs no RENDERER-era gate: an absent
    dev primitive or review hunter stalls a session whether its SKILL.md renders or is
    inline, so the answer is the same question at both layers. (The PRIMITIVE-era gate
    below is a different axis — which of the two skill names this run dispatches.)

    Gated on the REPO having the skill so a project that legitimately lacks one is not
    reported twice, and on the WORKTREE lacking it so a tracked skill the checkout
    already carries (which `provision_worktree` deliberately never clobbers) is not
    reported at all. Returns the rels rather than a bool so the journal and the
    escalation reason can name which skill went missing — a run pointed at
    ``_bmad/scripts`` when a review hunter is what vanished is the drift the shared
    constants exist to prevent.

    Asked per FILE, not per directory (:func:`_absent_skill_files`): a worktree can
    hold a SHORT skill dir, and a directory-granular check calls that complete and
    dispatches a session whose step-04 has nothing to read. The per-file question is
    walk PARITY with the copier, not a named required set — the seed delivers every
    file the repo has, so the only thing parity can report is a file the seed dropped,
    and a named list would go stale against upstream every time a step file is renamed
    (see :func:`_absent_skill_files` for the rationale this reverses). The precondition
    is ``SKILL.md`` rather than ``is_dir()`` for the same reason in the other direction —
    a repo dir with no ``SKILL.md`` is not a dispatchable skill (nothing spells it,
    :func:`resolve_dev_primitive` returns None), so its absence downstream stalls
    nothing and pausing over it refuses a healthy run. Both legs still stat *through*
    a symlink, which is the whole reason the gate exists.

    That precondition is where an UNREADABLE repo skill would otherwise be lost, and
    losing it is worse than crashing on it: it reads as the benign leg — "this project
    does not have that skill" — and the run walks on into a worktree that has none of
    it either. So the two are separated explicitly, and the answer is the same on every
    supported interpreter, which a bare ``is_file()`` is not: measured, that call
    RAISES ``PermissionError`` through 3.13 and answers ``False`` on 3.14, so this gate
    would have crashed on every ≤3.13 lane and gone quiet on the two 3.14 lanes (see
    :func:`_is_file`). ⚠️ The split is reachable for the review HUNTERS; for the dev
    primitive an unreadable dir resolves the era away from itself one branch above
    (:func:`resolve_dev_primitive` → None → the legacy default), so the unused-era skip
    takes it first and this branch never sees it. That is the right answer where a
    legacy install is dispatchable, and where it is not the preflight refuses — but it
    is the era filter doing the work there, not this.

    The era resolution above carried the same hazard and is fixed at its source rather
    than here: :func:`dev_primitive_or_default` runs on every provision, so a bare probe
    inside :func:`resolve_dev_primitive` made an unreadable ``bmad-build-auto`` dir
    raise out of ``provision_worktree`` itself (measured, through 3.13). With that probe
    total, an unreadable NEW dir falls through to a complete LEGACY install — which is
    dispatchable, so its era is the used one and the unreadable dir is rightly never
    asked about — or, with no usable legacy, resolves to None and the preflight refuses.
    ⚠️ The rest of the run-start preflight keeps its bare probes and this phase did not
    change them. Measured, that is now narrower than it sounds: the dev-primitive lane
    answers ``skills.base-missing`` on 3.14 AND through 3.13, because the probe above is
    total. What still raises out of ``validate``/``run`` on ≤3.13 is an unreadable
    review-HUNTER dir, at :func:`missing_base_skills`' own bare ``SKILL.md`` probe —
    loud, so no run proceeds on a bad read, but with a traceback where a finding
    belongs. Nothing on that surface can reach a session; every path that could reach
    one is total, walk and probes alike (see :func:`_is_dir`).

    Rel granularity follows the cause: the coarse ``<tree>/<skill>`` when the worktree
    lacks ``SKILL.md``, because then everything under the dir is missing and three rels
    for one cause is noise; the fine ``<tree>/<skill>/<file>`` otherwise, because
    naming a directory the operator falsifies with one ``ls`` teaches them to stop
    reading the gate. Every consumer treats a rel as an opaque string (an exact-match
    sentinel filter, an ``_is_under_bmad`` prefix test, a join into a message), so the
    extra segment splits nothing.

    Third gate, and the one :data:`BASE_SKILLS` cannot supply: the primitive era this
    run will not invoke. That map is a copy-if-*present* catalog ("every non-bundled
    skill that MIGHT need copying") listing both era names, because naming both is free
    when the answer to each is only "copy it if it is there". Read as a requirement set
    it demands one skill too many: with `bmad-build-auto` installed, every prompt spells
    it (:func:`dev_primitive_or_default`, the same call `Engine._dev_skill` makes) and a
    leftover `bmad-dev-auto` shim is never dispatched — so dropping the shim is not a
    stall, and pausing the whole run over it refuses a project nothing is wrong with.
    Measured both directions: a repo whose `bmad-build-auto` is a real dir beside a
    symlinked-out `bmad-dev-auto` passes the preflight, seeds the worktree with every
    skill a session names, and was still escalated; so was a resolved *legacy* install
    beside a `SKILL.md`-less `bmad-build-auto` dir, which nothing can resolve and the
    preflight never stats. Resolving here rather than filtering the caller's list keeps
    the engine's re-probe a pure function of disk. (It resolves against ``repo_root``
    while the engine spells the prompt from ``paths.project``. Reading the two as one
    is sound on every shipped path: both call sites sit under `Engine._run_isolated`,
    so nothing reaches this function outside worktree isolation, and every CLI
    entrypoint that constructs an Engine refuses that mode when a project overrides
    `repo_root` (:func:`bmadconfig.worktree_isolation_conflict`). It holds by those
    guards rather than by anything checked here — an in-process caller building an
    Engine directly, as the tests do, is not covered. #414's fix-1, plumbing
    `project` through provisioning, is a main-line option this release does not take.)

    The other two non-preflight entries stay gated on purpose. ``bmad-review`` is absent
    from :data:`DEV_BASE_SKILLS` so a pre-merge bmm install keeps validating, but on a
    merged-lens install the three hunter ids are thin forwarders to it: a worktree that
    lacks it breaks those forwards, and the repo-has-it conjunct already keeps the
    pre-merge case silent."""
    missing: list[str] = []
    for tree in dict.fromkeys(trees):
        # Total form: an unresolvable tree yields the legacy name, which the preflight
        # has already refused the run over — never a second, quieter answer here.
        unused_era = {DEV_PRIMITIVE_NEW, DEV_PRIMITIVE_LEGACY} - {
            dev_primitive_or_default(repo_root, tree)
        }
        for skill in BASE_SKILLS:
            if skill in unused_era:
                continue
            repo_skill = repo_root / tree / skill
            worktree_skill = worktree / tree / skill
            if not _is_file(repo_skill / "SKILL.md"):
                # Absent from the repo, or there and unreadable — and the two must not
                # collapse. False here means "this project legitimately lacks the
                # skill", so answering it for a dir we merely cannot open would turn an
                # unreadable install into silence. Only the walk can tell them apart:
                # a directory it could not see inside is the one thing it yields as a
                # leaf, while an absent path yields one ("", src) pair too — but that
                # pair answers _is_dir False, so `any` is False and the skip stands.
                if any(_is_dir(src) for _, src in _walk_traversable_files(repo_skill)):
                    missing.append(f"{tree}/{skill}")
                continue
            if not _is_file(worktree_skill / "SKILL.md"):
                missing.append(f"{tree}/{skill}")
                continue
            missing += [
                f"{tree}/{skill}/{rel}" for rel in _absent_skill_files(repo_skill, worktree_skill)
            ]
    return missing


def _central_config_seed_incomplete(worktree: Path, repo_root: Path) -> bool:
    """True when the repo has ``_bmad/config.toml`` but the worktree did not get one.

    The other half of the surface :func:`_bmad_scripts_seed_incomplete` guards, and
    silent in exactly the same way: the seed's resolve-and-contain guard drops a file
    that resolves outside ``repo_root`` with a bare ``continue``, so a *symlinked*
    ``_bmad/config.toml`` — a real BMAD install shared across checkouts, with the
    scripts dir a real directory — is never copied, never reported, and the
    ``_seed_bmad_tree`` return still claims the whole tree was seeded. The repo-side
    preflight follows the symlink and passes. Measured: no journal line at all.

    ``load_central_config`` reads this layer with ``required=True``, so a worktree
    without it HALTs before composing anything, on every story.

    Deliberately NOT gated on the renderer era, unlike the scripts predicate: that
    one gates to bound an rglob and to assert "this is a renderer-era scripts dir",
    whereas the premise here is one named file's presence in the repo. Provisioning
    is a pure reporter; :func:`renderer_stub_resolved` in the engine decides era for
    both sentinels in one place.

    Both probes are the total :func:`_is_file`, and the repo-side one is why: an
    unreadable ``_bmad/`` root is exactly the fault :func:`_seed_bmad_tree` degrades
    over, and a bare ``is_file()`` here raised ``PermissionError`` straight back out of
    ``provision_worktree`` through 3.13 (measured) — the crash the whole cluster exists
    to prevent, reached two calls after the ``try`` that prevented it. Answering False
    matches the sibling scripts predicate, which goes quiet on the same fault because
    ``render_skill.py`` is not a readable file either, and neither silence hides a run:
    a project whose ``_bmad/`` cannot be read fails the run-start preflight (see
    :func:`_seed_bmad_tree`).
    """
    return _is_file(repo_root / CENTRAL_CONFIG_REL) and not _is_file(worktree / CENTRAL_CONFIG_REL)


def worktree_seed_undelivered(
    worktree: Path,
    repo_root: Path,
    seed_files: Sequence[str] = (),
    seed_globs: Sequence[str] = (),
    config_paths: Sequence[str] = (),
) -> list[str]:
    """The seed rels the repo carries that never reached the worktree (#415).

    The last silent leg of ``provision_worktree``'s copy-when-absent seeding. Its two
    explicit loops drop an entry with a bare ``continue`` when the resolve-and-contain
    guard refuses it, and from the caller's side that is indistinguishable from an entry
    that applied: ``skipped`` reports only the *destination already exists* case. The
    realistic trigger is the same one the ``_bmad/`` and skills gates were written for —
    a config the repo carries as a **symlink to something outside it** (a
    dotfile-managed ``.claude/settings.json``, a shared MCP config, a plugin's skill
    tree pointing at a shared checkout). The seed then delivers nothing and says nothing.

    Asked of the RESULT rather than accumulated inside the loops, the pattern the three
    sibling gates established (:func:`base_skills_seed_incomplete`,
    :func:`_bmad_scripts_seed_incomplete`, :func:`_central_config_seed_incomplete`): a
    drop by any future path is caught the same way, and a caller cannot forge one — the
    answer is a pure function of the two trees on disk.

    **The SOURCE side is deliberately not containment-checked, and that asymmetry is the
    whole point.** ``_is_file``/``_is_dir`` stat *through* a symlink, so a source
    resolving outside ``repo_root`` still reads as "the repo carries this" — which is
    exactly the entry the loop refused and nothing named. Re-applying the loop's
    ``src.is_relative_to(repo_root)`` guard here would make the gate agree with the loop
    and report nothing at all. The DESTINATION side IS containment-checked, for the
    opposite reason: when ``worktree/rel`` resolves out of the worktree (a ``..``
    component, or a checked-out symlink pointing away), the probe would otherwise stat
    the file the loop refused to write *through* and read it as delivered.

    ``_is_file(x) or _is_dir(x)`` on both sides, because a ``seed_files`` entry may name
    a directory (``vendor``) as readily as a file — unlike :func:`_absent_skill_files`,
    whose rels always come from a walk and are always files. What that filter excludes
    is the decision the copier already made: a dangling symlink is neither file nor
    directory, so both halves drop it in silence (see
    ``test_a_dangling_repo_symlink_is_dropped_by_the_copier_and_the_gate``) — a broken
    link the copy could never have followed is not a report. A FIFO is filtered out here
    too, but the copier does NOT drop it: the two loops gate on ``src.exists()``, which a
    FIFO passes, so ``shutil.copy2`` reaches it and raises — the loud case the warning
    below covers, not a silent one. An *unreadable* source is
    the other way: :func:`_is_dir` answers True for a refused probe, so it is carried,
    and it is named. Absent-from-the-repo and unreadable-in-the-repo must not collapse
    into one silence, the same split :func:`base_skills_seed_incomplete` makes.

    ⚠️ Bounded to the SILENT drops. A copy that *fails* still raises out of
    ``provision_worktree`` — the two seed loops have no ``try`` around
    :func:`_copy_traversable`, unlike :func:`_merge_traversable` and
    :func:`_seed_bmad_tree` — so this gate never runs for that fault rather than
    reporting it. Loud, not silent, and unchanged here.

    ⚠️ ``config_paths`` names the rels the per-CLI hook step writes ITSELF
    (``profile.hooks.config_path`` for every non-hookless profile), and for those the
    destination probe cannot be the question. That step runs after the seed loops and
    creates the path unconditionally, so a config the loop dropped is answered for by
    the hook's own bytes and reads as delivered. Every hook config is also a profile
    seed entry, and for gemini, copilot and antigravity it is the profile's SOLE
    default seed — so there the false green is the gate's ENTIRE answer. Asked instead
    of the two things the hook step cannot fake:

    - the SOURCE resolves out of ``repo_root`` — the seed loop's own refusal, and the
      canonical trigger (a dotfile-managed ``.claude/settings.json``);
    - the DESTINATION is a symlink, or resolves out of the worktree — the loop refused
      to write *through* it (see ``provision_worktree``) and the hook step then wrote
      through it anyway, materialising a dangling target or a file outside the tree.

    The containment half is KEPT rather than replaced: it is not subsumed by
    ``is_symlink()``. Measured — a worktree whose ``.claude/`` is a live symlink out of
    the tree leaves a leaf that is not a link and a source that is perfectly contained,
    and the ordinary probe below already names it. What the branch drops is the
    EXISTENCE half, the only half the hook write invalidates. The branch REPLACES the
    probe rather than adding to it, so a destination that escapes containment is not
    counted twice.

    ⚠️ A LIVE symlinked destination is therefore named here as well as in ``skipped``.
    Both facts are true at once — the entry no-op'd, and the repo's bytes are not at
    the named path — and this channel journals rather than pauses, so the cost of
    saying both is one line.

    ⚠️ This is NOT the ``skipped``/:data:`RENDERER_SEED_SENTINELS` channel and cannot
    reach the renderer escalation, which reads that list by exact membership. Reported
    separately because the two facts have different remediations: a *skip* means the
    checkout already carries the path and the entry is doing nothing, while a *drop*
    means the repo has something the worktree does not. Rels are echoed in the caller's
    own spelling — a user-authored ``seed_files`` string verbatim (a Windows-authored
    ``_bmad\\custom`` included), a glob match as a POSIX rel — matching what the two
    loops put in ``skipped`` and in the git exclude respectively.
    """
    worktree = worktree.resolve()
    repo_root = repo_root.resolve()
    rels: list[str] = [str(rel) for rel in seed_files]
    for pattern in seed_globs:
        # Re-expanded rather than passed in, so this stays a function of disk. `glob`
        # never descends a symlinked sub-directory, which is the same match set the
        # seed loop saw.
        rels += [m.relative_to(repo_root).as_posix() for m in sorted(repo_root.glob(pattern))]
    # `Path`, not the raw string: rels are echoed in the CALLER's spelling (a
    # Windows-authored `.claude\settings.json` included) while a profile's
    # `config_path` is always POSIX, and the two name the same file.
    hook_configs = {Path(rel) for rel in config_paths}
    undelivered: list[str] = []
    for rel in dict.fromkeys(rels):
        src = repo_root / rel
        if not (_is_file(src) or _is_dir(src)):
            continue
        dst = worktree / rel
        if Path(rel) in hook_configs:
            if (
                not src.resolve().is_relative_to(repo_root)
                or not dst.resolve().is_relative_to(worktree)
                or dst.is_symlink()
            ):
                undelivered.append(rel)
            continue
        if dst.resolve().is_relative_to(worktree) and (_is_file(dst) or _is_dir(dst)):
            continue
        undelivered.append(rel)
    return undelivered


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
      own skill tree (e.g. .agents/) or config keeps it untouched (no diff merged back).
      Skill trees are merged per FILE, not per directory: a checkout carrying part of
      a skill dir keeps every file it has and gets the rest filled in, rather than
      being skipped whole and dispatched short (see _merge_traversable);
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
    seeded, and it gets its own exclude line only when the blanket `/_bmad` one is
    not going in — either line alone keeps the renderer's in-session rewrite of it
    from being swept into a story commit.

    Returns the `seed_files` entries that existed in the repo but were skipped
    because the destination already existed — copy-when-absent turned them into
    no-ops. The caller journals them: a user-authored `worktree_seed` entry that
    silently copies nothing reads as applied configuration and is not. The same
    channel also reports a renderer surface that reached the worktree SHORT — an
    incomplete `_bmad/scripts/` or an absent `_bmad/config.toml` (see
    _bmad_scripts_seed_incomplete, _central_config_seed_incomplete) — which is
    likewise silent otherwise. Those two entries are RENDERER_SEED_SENTINELS and are
    reserved: a `seed_files` entry spelling one is dropped rather than passed through,
    so only the checks above can emit them. Whether either is fatal depends on the
    resolved primitive's era, which provisioning cannot see from a repo root and a
    list of CLI profiles — the caller decides (see RENDERER_SEED_SENTINELS).

    Entries the two seed loops DROPPED are NOT in this list: a skip and a drop are
    different facts with different remediations, so the drop report is its own
    predicate on its own channel (see worktree_seed_undelivered), which the caller
    asks of the result afterwards.
    """
    if not profiles and not seed_files and not seed_globs and not (repo_root / BMAD_DIR).is_dir():
        return []
    worktree = worktree.resolve()
    repo_root = repo_root.resolve()
    relay = repo_root / HOOK_SCRIPT_REL
    skills_root = resources.files("bmad_loop.data").joinpath("skills")

    # project gitignored MCP/CLI configs: copy from the main repo when absent.
    # Resolve-and-contain guards against an `..`/absolute entry escaping either tree.
    # Both loops drop a refused entry with a bare `continue`; what they dropped is named
    # afterwards by `worktree_seed_undelivered`, which re-asks the result rather than
    # accumulating a bit here — see its docstring for why the two guards below cannot be
    # the report (the source one is the very fault being reported).
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
        raw = worktree / rel
        dst = raw.resolve()
        if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
            continue
        if not src.exists():
            continue
        if dst.exists():
            skipped.append(str(rel))
            continue
        # Never write THROUGH a link (#405). `dst` is resolved, and non-strict
        # `resolve()` follows a dangling one, so the `exists()` above asked about the
        # TARGET and answered "free" — the mkdir and the copy then land at a path
        # nobody named, inside the worktree, which the exclude below does not cover
        # and `git add -A` would stage into the story branch. One comparison covers
        # both legs: a dangling leaf AND a dangling directory component, which
        # `_occupied(raw)` would miss (the leaf there is neither link nor file).
        # Sufficient, not necessary: an entry spelling an internal `..` (`a/../b`)
        # also differs, since PurePath does not normalize `..` and `resolve()` does.
        # That entry is refused here and NAMED afterwards by the gate, which is the
        # right answer for a rel that is not a name for the path it appears to be.
        #
        # Strictly AFTER the skip arm, not before: `dst != raw` is true of every
        # symlinked destination, live ones included, so hoisting it would turn the
        # ordinary copy-when-absent no-op into a silent drop and empty `skipped`.
        # Refused with a bare `continue` like the guards above, and named afterwards
        # by `worktree_seed_undelivered` — the link stays dangling, so the gate's
        # destination probe reports it.
        if dst != raw:
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
            raw = worktree / rel
            dst = raw.resolve()
            if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
                continue
            if not src.exists() or dst.exists():
                continue
            # same write-through refusal as the loop above, and it needs its own: this
            # loop builds its own `dst` from its own `rel` and shares no code with it
            if dst != raw:
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
        # 0.9.0 named `worktree_seed = ["_bmad"]` only to document that it COPIES
        # NOTHING once any child is tracked (#230) — it was the broken example, never
        # the workaround. That entry is now a no-op *because the merge already covered
        # it*, so reporting it would journal worktree-seed-skipped for a seed that
        # applied.
        skipped = [rel for rel in skipped if not _is_under_bmad(rel)]
    # The sentinels are ours to emit, never a user's to forge. A `worktree_seed` entry
    # spelling one exactly ("_bmad/scripts", "_bmad/config.toml" — both natural things
    # to write) lands in `skipped` the moment the checkout already carries it, and the
    # strip above is conditional on the merge having seeded SOMETHING, so a repo that
    # commits its whole `_bmad/` skips it. The engine reads these by exact membership,
    # so an exact-string reservation is exactly as strong as the read it protects:
    # what follows is then the only thing that can put one back.
    skipped = [rel for rel in skipped if rel not in RENDERER_SEED_SENTINELS]
    if _bmad_scripts_seed_incomplete(worktree, repo_root):
        skipped.append(BMAD_SCRIPTS_SEED_REL)
    if _central_config_seed_incomplete(worktree, repo_root):
        skipped.append(CENTRAL_CONFIG_REL)

    # bundled skills into each CLI's skill tree (deduped: codex+gemini share one);
    # never clobber a FILE the checkout already carries (tracked or pre-existing).
    for tree in dict.fromkeys(p.skill_tree for p in profiles):
        tree_dir = worktree / tree
        for skill in MODULE_SKILLS:
            _merge_traversable(skills_root.joinpath(skill), tree_dir / skill, worktree)
        # The orchestrator-driven upstream skills (BASE_SKILLS) are not in the
        # wheel; copy them from the MAIN REPO's installed tree (same tree path) so
        # an isolated worktree can still resolve the dev primitive (under EITHER
        # name — BASE_SKILLS lists both eras) and the review hunters.
        #
        # Three legs, and only one of them is safe to skip silently. `not is_dir()`
        # means the repo genuinely lacks the skill, which the run-start preflight
        # already refused. A containment failure does NOT: the skill is there, as a
        # symlink to a shared install outside the repo, and the preflight stats
        # through the link and passes. Nor does a destination that already holds SOME
        # of the dir — which is why this merges per FILE rather than skipping on the
        # directory: a checkout carrying one tracked child of a skill tree, or a
        # `worktree_seed` entry naming a file inside one, would otherwise leave the
        # rest at zero bytes. Both reported below via `base_skills_seed_incomplete`,
        # which re-asks the question of the result rather than trusting this loop's
        # bookkeeping — and asks it of every file the repo carries, walking exactly the
        # way this loop does, so a containment drop is reported whichever child it hits
        # rather than only the two marker files. The per-file skips are NOT reported: the
        # `seed_files` report exists because a USER-AUTHORED entry that copies nothing
        # reads as applied configuration, and because a directory entry's no-op is
        # total — the merge is precisely what removes that total-skip hazard, and
        # naming every already-present file would put hundreds of rels per story into
        # the channel the renderer sentinels share.
        for skill in BASE_SKILLS:
            src_root = repo_root / tree / skill
            if not src_root.resolve().is_relative_to(repo_root) or not _is_dir(src_root):
                continue
            _merge_traversable(src_root, tree_dir / skill, worktree, repo_root)

    # Report whatever the skills copy above could not deliver. Asked of the RESULT,
    # not accumulated inside the loop, so a skill dropped by any future path is caught
    # the same way — and so a user `worktree_seed` entry that happens to spell a skill
    # rel cannot forge one: the engine re-runs this predicate rather than reading these
    # strings back (the reason RENDERER_SEED_SENTINELS needs its filter above and this
    # does not).
    skipped += base_skills_seed_incomplete(worktree, repo_root, [p.skill_tree for p in profiles])

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
    if f"/{BMAD_DIR}" not in patterns and (worktree / BMAD_DIR).is_dir():
        # Shielded whether we seeded it or not: the renderer rewrites this dir
        # DURING the session, long after provisioning. Both conditions exist to keep
        # lines OUT of this file, and they are one policy rather than two:
        # `.git/info/exclude` is the COMMON git dir, shared with the main checkout and
        # never pruned by anything, so every line written here is permanent state in the
        # operator's own repo. Measured: a `/_bmad` line hides an untracked `_bmad/` from
        # `git status` in the MAIN checkout, for the life of the repo.
        #
        # So: nothing when the worktree can never grow a `_bmad/`, and nothing when the
        # blanket root line is already going in — `/_bmad` prunes the directory before
        # git descends, so `/_bmad/render/` beneath it is never consulted (measured with
        # `check-ignore -v`: both files attribute to the root line). Writing both was the
        # inconsistency: one sibling gated to avoid polluting a shared file while the
        # other added a provably inert line to it.
        #
        # Read off `patterns` rather than `seeded_bmad`, because the root line has two
        # producers: the `_bmad/` merge when the checkout had none, and a user
        # `worktree_seed = ["_bmad"]` entry that copied (0.9.0's documented broken
        # example, which really does copy when nothing under `_bmad/` is tracked).
        patterns.add(f"/{RENDER_DIR_REL}/")
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
