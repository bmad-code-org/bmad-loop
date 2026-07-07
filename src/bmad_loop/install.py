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
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from .adapters.profile import ALIASES, CLIProfile, ProfileError, load_profiles
from .policy import POLICY_TEMPLATE
from .process_host import get_process_host

HOOK_SCRIPT_REL = ".bmad-loop/bmad_loop_hook.py"
# Dedup marker: matches any bmad-loop-managed hook command — both the signal
# relay (bmad_loop_hook.py) and the probe-adapter capture hook
# (bmad_loop_probe_hook.py) — so merge_hooks stays idempotent for either.
HOOK_MARKER = "bmad_loop"
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
#   - bmad-dev-auto: the inner dev primitive — always required.
#   - the two review hunters bmad-dev-auto's step-04 invokes inline on EVERY dev
#     run (and on each follow-up review re-invocation) — also always required, no
#     longer gated on a separate review session.
DEV_BASE_SKILLS = {
    "bmad-dev-auto": ("step-04-review.md",),
    "bmad-review-adversarial-general": (),
    "bmad-review-edge-case-hunter": (),
}
# Every non-bundled skill that might need copying into an isolated worktree.
BASE_SKILLS = dict(DEV_BASE_SKILLS)

# Stories mode (folder+id dispatch, BMAD-METHOD #2549) needs a *newer* bmad-dev-auto
# than sprint mode: one whose step-01 routes a spec-folder + story-id invocation.
# File existence (missing_base_skills) can't tell the two skill versions apart, so
# a content probe confirms the merged dispatch protocol is present. This literal is
# stable prose in the merged step-01 ("this is a **folder+id dispatch**").
STORIES_PROBE_SKILL = "bmad-dev-auto"
STORIES_PROBE_FILE = "step-01-clarify-and-route.md"
STORIES_PROBE_TEXT = "folder+id dispatch"


def missing_stories_support(project: Path, trees: Sequence[str]) -> list[str]:
    """Problems for stories mode's stricter bmad-dev-auto requirement.

    Sprint mode drives any bmad-dev-auto; stories mode needs the folder+id
    dispatch flow, which older skill versions lack. For each active CLI skill
    tree, confirm ``bmad-dev-auto/step-01-clarify-and-route.md`` exists and
    carries the dispatch-protocol marker. Returns one human-readable problem per
    tree lacking it (empty = OK). Callers gate this on stories mode only —
    sprint-mode runs must not require the newer skill."""
    problems: list[str] = []
    for tree in dict.fromkeys(trees):
        probe = project / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE
        try:
            text = probe.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # OSError = the probe file is absent/unreadable; UnicodeDecodeError = it
            # exists but is binary/non-UTF-8 (a corrupted skill tree). Either way the
            # dispatch-protocol marker can't be confirmed, so report a problem rather
            # than letting the decode error escape and crash the whole preflight.
            problems.append(
                f"{tree}/{STORIES_PROBE_SKILL}/{STORIES_PROBE_FILE} not found — stories "
                f"mode needs folder+id dispatch; update the BMad Method (bmm) module"
            )
            continue
        if STORIES_PROBE_TEXT not in text:
            problems.append(
                f"{tree}/{STORIES_PROBE_SKILL} lacks folder+id dispatch (no "
                f"{STORIES_PROBE_TEXT!r} in {STORIES_PROBE_FILE}) — stories mode needs a "
                f"newer bmad-dev-auto; update the bmm module"
            )
    return problems


def missing_base_skills(project: Path, trees: Sequence[str]) -> list[str]:
    """Problems for the upstream skills the orchestrator drives but doesn't bundle.

    The dev primitive (bmad-dev-auto) and the two adversarial review hunters it
    invokes inline are installed by the BMad Method module, not by `bmad-loop
    init`. Each must exist in every active CLI skill tree and carry its marker
    files. Returns one human-readable problem string per missing/incomplete skill;
    empty list means OK. Run as a preflight so a missing skill fails loudly with
    remediation instead of stalling as an `Unknown command` until the run times out.
    """
    required = dict(DEV_BASE_SKILLS)
    problems: list[str] = []
    for tree in dict.fromkeys(trees):
        for skill, markers in required.items():
            skill_dir = project / tree / skill
            if not (skill_dir / "SKILL.md").is_file():
                problems.append(
                    f"{tree}/{skill} not found — install the BMad Method (bmm) module "
                    f"(the orchestrator drives this upstream skill directly)"
                )
                continue
            absent = [m for m in markers if not (skill_dir / m).is_file()]
            if absent:
                problems.append(
                    f"{tree}/{skill} is incomplete (missing {', '.join(absent)}) — "
                    f"reinstall it from the bmm module"
                )
    return problems


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
            already = any(
                isinstance(cmd := handler.get("command"), str) and HOOK_MARKER in cmd
                for entry in handlers
                if isinstance(entry, dict)
                for handler in (entry, *entry.get("hooks", []))
                if isinstance(handler, dict)
            )
            if not already:
                handlers.append(_hook_entry(dialect, command))
                changed = True
        return config, changed
    if dialect == "copilot-settings-json":
        config.setdefault("version", 1)  # Copilot hook configs are versioned
    hooks = config.setdefault("hooks", {})
    for native_event, command in registrations.items():
        matchers = hooks.setdefault(native_event, [])
        # claude/codex/gemini nest handlers under "hooks"; copilot stores the
        # handler dict directly in the event list — check both shapes so a re-run
        # stays idempotent for every dialect.
        already = any(
            isinstance(cmd := handler.get("command"), str) and HOOK_MARKER in cmd
            for entry in matchers
            if isinstance(entry, dict)
            for handler in (entry, *entry.get("hooks", []))
            if isinstance(handler, dict)
        )
        if not already:
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
    else:
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
) -> None:
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
    """
    if not profiles and not seed_files and not seed_globs:
        return
    worktree = worktree.resolve()
    repo_root = repo_root.resolve()
    relay = repo_root / HOOK_SCRIPT_REL
    skills_root = resources.files("bmad_loop.data").joinpath("skills")

    # project gitignored MCP/CLI configs: copy from the main repo when absent.
    # Resolve-and-contain guards against an `..`/absolute entry escaping either tree.
    seeded: list[str] = []
    for rel in seed_files:
        src = (repo_root / rel).resolve()
        dst = (worktree / rel).resolve()
        if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
            continue
        if not src.exists() or dst.exists():
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
        # an isolated worktree can still resolve /bmad-dev-auto and the review
        # hunters. Skip silently when the main repo lacks them — the run-start
        # preflight reports it.
        for skill in BASE_SKILLS:
            dst = tree_dir / skill
            if dst.exists():
                continue
            src = (repo_root / tree / skill).resolve()
            if not src.is_relative_to(repo_root) or not src.is_dir():
                continue
            _copy_traversable(src, dst)

    # per-CLI signal-hook registration, baked to the main repo's relay (absolute)
    for profile in profiles:
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
    patterns |= {f"/{p.hooks.config_path}" for p in profiles}
    patterns |= {f"/{rel}" for rel in seeded}
    _worktree_local_exclude(worktree, sorted(patterns))


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

    # 5. gitignore generated dirs: per-run state (.bmad-loop/runs/) and the
    # game-engine plugins' rebuildable caches, e.g. the per-worktree Unity Library
    # (.bmad-loop/cache/). Both are large/ephemeral and must never be committed.
    gitignore = project / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    have = set(existing.splitlines())
    to_add = [line for line in (".bmad-loop/runs/", ".bmad-loop/cache/") if line not in have]
    if to_add:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(to_add) + "\n")
        for line in to_add:
            print(f"  gitignored: {line}")

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
