"""Per-unit worktree isolation + integration flow.

Extracted from :class:`bmad_loop.engine.Engine` (issue #244, findings F-3/F-9a):
``Engine`` was a god-class whose worktree/integration cluster is an independent
state machine. It lives here as a collaborator built from narrow dependencies
(repo paths, the policy, run state, journal, plugin registry, loaded adapters)
plus a handful of engine callbacks (emit a plugin hook, save state, run the
per-unit ready gate, carry isolated ledger writes, escalate-pause, and get/set
the engine's active workspace).
The collaborator never receives the whole ``Engine`` — it cannot reach engine
internals beyond those callables.

``Engine`` keeps same-name private methods that delegate here, so its tests and
the ``SweepEngine``/``StoriesEngine`` subclasses see an unchanged surface.

``provision_worktree`` (previously in ``install.py``) is rehomed here too so the
runtime control loop no longer imports the installer; ``install.py`` re-exports
it lazily for its own tests.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NoReturn

from . import artifact_publication, gates, verify
from .adapters.profile import ProfileError
from .install import (
    _REVIEW_LAYER_SKILLS,
    BASE_SKILLS,
    BMAD_DIR,
    BMAD_SCRIPTS_SEED_REL,
    BMAD_SEED_EXCLUDES,
    CENTRAL_CONFIG_REL,
    DEV_PRIMITIVE_MARKERS,
    DEV_PRIMITIVE_ROLES,
    MERGED_REVIEW_SKILL,
    MODULE_SKILLS,
    RENDER_DIR_REL,
    RENDERER_SCRIPT_MARKER,
    RENDERER_SCRIPT_UNIT_REL,
    RENDERER_SEED_SENTINELS,
    _absent_renderer_sources,
    _copy_traversable,
    _hook_command,
    _is_dir,
    _is_file,
    _occupied,
    _renderer_unit_required,
    _walk_traversable_files,
    _worktree_local_exclude,
    dev_primitive_or_default,
    merge_hooks,
    missing_stories_support,
    renderer_stub_resolved,
    resolve_review_layers,
    strip_relay_hooks,
)
from .model import Phase
from .platform_util import atomic_write_text
from .workspace import (
    UnitWorkspace,
    Workspace,
    close_unit_workspace,
    discard_worktree,
    unit_worktrees_dir,
)

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from .adapters.base import CodingCLIAdapter
    from .adapters.profile import CLIProfile
    from .bmadconfig import ProjectPaths
    from .journal import Journal
    from .model import RunState, StoryTask
    from .plugins import PluginRegistry
    from .policy import Policy


# CLI profile name -> the agent id the Unity-MCP CLI's `setup-mcp` expects (see
# `unity-mcp-cli setup-mcp --list`). All but claude differ only by claude's
# "-code" suffix; codex/gemini/cursor and any custom profile pass through as-is.
_SETUP_MCP_AGENT_IDS = {"claude": "claude-code"}


def _setup_mcp_agent_id(profile_name: str) -> str:
    """Map a CLI profile name to its Unity-MCP `setup-mcp` agent id."""
    return _SETUP_MCP_AGENT_IDS.get(profile_name, profile_name)


def _crlf_normalized(data: bytes) -> bytes:
    """``data`` with every CRLF read as LF — the one translation a git checkout
    under ``core.autocrlf`` applies on its own (see `_warn_accepted_spec_superseded`)."""
    return data.replace(b"\r\n", b"\n")


def _worktree_skill_copy_candidates(repo_root: Path, tree: str) -> tuple[str, ...]:
    """Every upstream skill worth best-effort copying into ``tree``."""
    resolved = resolve_review_layers(repo_root, tree)
    return tuple(dict.fromkeys((*BASE_SKILLS, *(resolved.skills() if resolved else ()))))


def _required_worktree_skills(repo_root: Path, tree: str) -> tuple[str, ...]:
    """Upstream skills whose absence deterministically stalls this tree's run.

    Match :func:`install.missing_base_skills`: gate the selected dev primitive and
    resolved required review skills only. If the review shape is unknown, prefer a
    present merged reviewer; otherwise require the standalone fallback reviewers.
    Catalog-only and advisory skills remain copy candidates but never arm the fatal
    pre-dispatch completeness gate.
    """
    resolved = resolve_review_layers(repo_root, tree)
    if resolved is not None:
        review_skills = tuple(resolved.required)
    elif (repo_root / tree / MERGED_REVIEW_SKILL / "SKILL.md").is_file():
        review_skills = (MERGED_REVIEW_SKILL,)
    else:
        review_skills = tuple(sorted(_REVIEW_LAYER_SKILLS))
    return tuple(dict.fromkeys((dev_primitive_or_default(repo_root, tree), *review_skills)))


def _escape_exclude_pattern(pattern: str) -> str:
    """Render one shield pattern so it names the literal path it spells (#476).

    Patterns are built as ``f"/{rel}"`` from real on-disk rels, but git reads them
    as gitignore(5) patterns, so a rel carrying pattern syntax names something
    else entirely — and it goes wrong in both directions at once. Measured on git
    2.55.0, identical at 2.20.4 (the shield's floor):

    * ``/cfg[env]`` leaves ``cfg[env]/conf.json`` STAGEABLE. The path we seeded is
      never shielded, which is the one job the shield has (#476).
    * The same line hides ``cfge/`` and ``cfgn/``, because ``[env]`` is a class
      over ``e``/``n``/``v``. A broken pattern silently hides an UNRELATED file
      that the unit meant to commit (#401, consolidated into #476).

    An unescaped trailing space does both in one line: git drops it, so ``/kept``
    plus a space shields ``kept/`` and leaves ``kept /`` visible.

    gitignore(5)'s own escape rule is the fix — a backslash before a wildmatch
    special (``*``, ``?``, ``[``, and the backslash itself) or before a trailing
    space makes git match that character literally. ``]`` is deliberately left
    alone: it is not special without an opening ``[``, which this escapes. ``!``
    and ``#`` matter only at line start, and every pattern starts with ``/``.

    One trailing ``/`` is split off and re-appended unescaped: it is the MUSTBEDIR
    marker (``RENDER_DIR_REL``), not part of the name.

    Ordinary rels come back byte-identical, which is what makes this inert for
    every path the shield has ever written.
    """
    body, suffix = (pattern[:-1], "/") if pattern.endswith("/") else (pattern, "")
    for special in ("\\", "*", "?", "["):
        body = body.replace(special, "\\" + special)
    stripped = body.rstrip(" ")
    return stripped + "\\ " * (len(body) - len(stripped)) + suffix


def _fits_one_exclude_line(pattern: str) -> bool:
    """True when ``pattern`` survives a round trip through the exclude file.

    The exclude is a line-oriented format with NO escape for its own line
    boundary, so two characters in a real filename cannot be written as a pattern
    at all — unlike the wildmatch specials, which :func:`_escape_exclude_pattern`
    can quote. `_worktree_local_exclude` writes each pattern `\n`-terminated
    (`install.py`), and git reads lines back the way #472 measured them at 2.55.0:
    `\n` boundaries with exactly ONE trailing `\r` trimmed.

    * An embedded ``\n`` SPLITS the pattern into two. Neither half names the file,
      so it is not shielded — and the orphaned second half is a live, unanchored
      pattern that can hide an UNRELATED file, which is #401's harm direction
      arriving through the one character #476's escaping cannot quote.
    * A TRAILING ``\r`` is eaten as the line terminator's other half, so the
      pattern names the path WITHOUT it — #476's harm direction, same cause.

    An embedded ``\r`` is content to git (`/hidden\rjunk` ignores nothing but a
    file spelled that way), so it needs no exclusion here.
    """
    return "\n" not in pattern and not pattern.endswith("\r")


def _reconcile_tracked_patterns(
    worktree: Path, patterns: set[str], written: Collection[str]
) -> tuple[set[str], str | None]:
    """Reshape the shield patterns that name a TRACKED path; keep the rest as written.

    Git consults ignore rules only for UNTRACKED paths, so what a pattern is worth
    depends on what it names — and the two tracked shapes need opposite answers.

    A pattern naming a tracked REGULAR FILE shields nothing and costs something
    (#392, reported from production by an external user whose repo-hygiene gate then
    blocked the story's commit). Measured on git 2.55.0 with the shield's own
    private-exclude + worktree-scoped `core.excludesFile` shape, a tracked
    `.codex/hooks.json`, and its pattern present:

    * `git add -A` STAGES a modification to it anyway — ignore rules are consulted only
      for untracked paths, so the pattern never did the job it is here for;
    * `git ls-files -ci --exclude-standard` inside the worktree REPORTS it, which is the
      tracked-and-ignored state hygiene gates reject.

    So the pattern is pure cost and it is DROPPED. This is the second half of #384
    — its reporter proposed it as their option 3 ("skip any pattern whose path already
    contains tracked files … costs nothing and removes the surprising case entirely");
    PR #385 landed option 1 (scope + lifetime) and this half was dropped rather than
    rejected, which is how a second reporter hit it.

    A pattern naming a tracked DIRECTORY is that reporter's shape one step out (#484),
    and its answer is SUBSTITUTION. The measurement that used to justify keeping it
    still stands and is kept here: a dir pattern really does hide new children, and no
    pattern SHAPE both hides them and keeps the report clean — `dir/*`, `dir/**` and
    `dir` plus a trailing negation all measured identical to `dir`, because gitignore
    cannot re-include anything under an excluded parent, while the one shape that did
    clear the report (`dir/*` with per-child negations) leaked a new file into the
    commit. What reverses is the VERDICT, on the #392 physics: over a tracked tree the
    pattern's protection is already mostly inert — every modification to a tracked
    child stages regardless — so it was buying new-child coverage alone at the price of
    a false tracked-and-ignored report for the whole tree.

    The fix is therefore a different set of PATHS rather than a different shape: drop
    the dir pattern and substitute one pattern per file THIS provisioning run actually
    wrote below it (``written``). Those files are untracked by construction —
    copy-when-absent cannot land on top of a tracked child — which is why they are
    exactly the case an ignore rule works for, and why none of them is re-probed here:
    the spawn count stays one per pattern the caller built. A tracked directory that
    received nothing at all drops to no pattern, which is the same answer for the same
    reason: there is nothing of ours below it to shield.

    ACCEPTED RESIDUAL (maintainer decision on #484, 2026-08-08, which is this
    function's design authority): a file the SESSION creates under a tracked tool
    directory can be staged, where the dir pattern would have hidden it. That is the
    trade, deliberately taken — it matches the project's own decision to TRACK that
    tree, and everything the orchestrator put there keeps a pattern of its own. It is
    not a defect to fix by widening back to a dir pattern.

    APPEND-ONLY RESIDUE: `_worktree_local_exclude` never removes a line, by design —
    operator lines ride in its rewrite prefix and its last-match-wins reasoning depends
    on nothing being deleted, with no marker separating our stale line from theirs. So
    a `/dir` line an OLDER provisioning of the SAME worktree wrote survives a
    re-provision that would no longer write it. Accepted rather than fixed: the residue
    can only be too wide, never too narrow, and the private exclude dies with the
    worktree.

    Patterns ending in `/` are directory-shaped by construction (`RENDER_DIR_REL`) and
    are never probed.

    A substituted rel that cannot be spelled as ONE exclude line
    (:func:`_fits_one_exclude_line`) sends the WHOLE directory back to its dir
    pattern, journaled. That is the same trade as the degrade below and for the same
    reason: no per-file pattern exists for such a name, so substituting would leave
    the file stageable and — for a newline — write an orphan half-pattern that hides
    unrelated files.

    Degrades by KEEPING every pattern it could not resolve, in its ORIGINAL shape —
    the dir shape included, never the substitution. A shield staying too wide is
    cosmetic, while dropping or narrowing a pattern on a guess can leak the
    orchestrator's own seeded files into a story commit. Returns the surviving patterns
    and a reason for the caller to journal, or None when nothing went wrong.

    NOT-A-REPO IS SILENT, and it is the ordinary case rather than an edge one: many
    callers provision plain non-repo directories, where there is no index, no shield and
    no `git add -A` to be wrong about. The same `rev-parse --absolute-git-dir` gate
    `_worktree_local_exclude` skips on is asked FIRST, so this cannot emit a degrade per
    call and train operators past the one that matters. Only a repo that answers that
    probe and then fails the per-path one is reported."""
    try:
        if verify.git_bytes(worktree, "rev-parse", "--absolute-git-dir").returncode != 0:
            return patterns, None
    except (verify.GitError, OSError):
        return patterns, None
    kept: set[str] = set()
    unprobed: list[str] = []
    unwritable: list[str] = []
    for pattern in patterns:
        rel = pattern.lstrip("/")
        if not rel or pattern.endswith("/"):
            kept.add(pattern)
            continue
        try:
            kind = verify.path_tracked_kind(worktree, rel)
        except (verify.GitError, OSError) as e:
            kept.add(pattern)
            unprobed.append(f"{rel} ({e})")
            continue
        if kind == "untracked":
            kept.add(pattern)
        elif kind == "dir":
            subs = {f"/{w}" for w in written if w.startswith(rel + "/")}
            unrepresentable = sorted(s for s in subs if not _fits_one_exclude_line(s))
            if unrepresentable:
                # Substituting here would write a pattern the exclude cannot carry,
                # leaving that file stageable AND (for a `\n`) loosing an unanchored
                # orphan pattern on unrelated files. The dir pattern is the only shape
                # that still covers it, so this degrades the way every other
                # unanswerable case does: KEEP the original, journal the reason. Too
                # wide is cosmetic — this tree keeps its tracked-and-ignored report —
                # while dropping or half-substituting leaks provisioned work into the
                # unit's commit.
                kept.add(pattern)
                unwritable.extend(unrepresentable)
            else:
                kept |= subs
        # "file": dropped. The pattern is measurably inert over a tracked file and
        # only feeds it to repo-hygiene gates as tracked-and-ignored (#392).
    reasons: list[str] = []
    if unprobed:
        reasons.append(
            "worktree git-add shield could not check whether these paths are tracked, "
            "so their patterns were kept as built; a tracked path among them will read "
            f"as ignored to repo-hygiene checks (#392, #484): {'; '.join(sorted(unprobed))}"
        )
    if unwritable:
        reasons.append(
            "worktree git-add shield kept a tracked directory's whole-dir pattern "
            "because a file provisioning wrote below it cannot be spelled as one "
            "exclude line (a newline, or a trailing carriage return); that directory "
            "will read as ignored to repo-hygiene checks (#484): "
            f"{'; '.join(sorted(unwritable))}"
        )
    return kept, (" ".join(reasons) if reasons else None)


def _pin_tracked_config_rewrite(worktree: Path, rel: str) -> str | None:
    """Keep a rewritten TRACKED hook config out of the unit's story commits.

    The worktree-local exclude cannot: git consults ignore rules only for
    untracked paths (#392). When a project tracks its hook config, the relay
    rewrite — a machine-specific absolute command — would ride every
    `git add -A` (the skill's own commits and finalize_commit alike) into the
    story commit and merge back to the target branch, handing every other
    checkout a relay path that does not exist there. The worktree's own index
    carries a skip-worktree bit that `add -A`, `status` and checkout all honor
    (it is the sparse-checkout mechanism) and that dies with the worktree, so
    the rewrite stays session-local.

    While the pin holds, the config is orchestrator-owned: a story's own edit to
    the pinned file stays session-local and is discarded with the worktree. That
    is deliberate — before this pin the tracked case stalled outright (#352), so
    there is no prior working behavior to preserve, and any file-level hiding
    that keeps OUR rewrite out of `add -A` hides a story's edit with it.

    NOT-A-REPO IS SILENT for the same reason the shield's tracked-probe is:
    provisioning a plain directory is ordinary, and there is no index and no
    `git add -A` to be wrong about. An untracked config needs nothing — the
    exclude shield owns it. A tracked-probe that cannot answer is returned for
    the caller to journal (observation degrades; the rewrite stands, since a
    stalled session is the worse outcome, #352). But a failed update-index on a
    KNOWN-tracked config raises: the pin is a repair write, and continuing
    without it knowingly leaves `git add -A` free to commit the machine-specific
    command and merge it back.
    """
    try:
        if verify.git_bytes(worktree, "rev-parse", "--absolute-git-dir").returncode != 0:
            return None
    except (verify.GitError, OSError):
        return None
    try:
        if not verify.path_tracked_file(worktree, rel):
            return None
    except (verify.GitError, OSError) as e:
        return (
            f"could not check whether the rewritten hook config {rel} is tracked "
            f"({e}); if the project tracks it, the worktree's machine-specific relay "
            "command may be committed and merged back (#352)"
        )
    pinned = verify.git_bytes(worktree, "update-index", "--skip-worktree", "--", rel)
    if pinned.returncode != 0:
        raise verify.GitError(
            f"git update-index --skip-worktree {rel} failed in the worktree; the hook "
            "config is tracked, so without the pin its machine-specific relay rewrite "
            "would reach story commits and merge back (#352)"
        )
    return None


def _seed_bmad_tree(worktree: Path, repo_root: Path) -> tuple[list[str], list[str]]:
    """Merge the repo's project-local BMAD surface into an isolated worktree.

    Renderer-backed skills receive the worktree as their project root and do not
    walk upward for ``_bmad``. Copy every usable file except generated render output,
    per-file and without clobbering checkout content. The shared Traversable walk is
    intentional: unlike ``rglob``, it descends a symlinked child directory, allowing
    the result-side completeness predicates to see every file the copier considered.

    Returns ``(shield_rels, written)``, two readings of the same copy:

    * ``shield_rels`` — what the caller turns into git-add shield patterns: ``[]``
      when nothing landed, ``[BMAD_DIR]`` when the root was absent before seeding
      (one pattern covers a tree that is wholly ours), otherwise each file that
      actually landed. A fresh worktree checkout materializes every tracked path, so
      "root absent AND tracked" cannot arise and this collapse is already correct for
      the tracked-directory rule (#484): the collapsed shape only ever names an
      untracked root.
    * ``written`` — always the per-file landed rels, never the collapse. This is the
      substitution ledger the shield reads when a tool directory turns out to be
      TRACKED (#484): the dir pattern is dropped and these files get patterns of
      their own, so the answer has to stay per-file even when ``shield_rels``
      collapsed.
    """
    src_root = repo_root / BMAD_DIR
    if not _is_dir(src_root):
        return [], []
    dst_root = worktree / BMAD_DIR
    had_bmad = _is_dir(dst_root)
    try:
        tops = sorted(src_root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return [], []

    seeded: list[str] = []
    for top in tops:
        if top.name in BMAD_SEED_EXCLUDES:
            continue
        for rel, src in _walk_traversable_files(top, top.name):
            if not _is_file(src):
                continue
            dst = dst_root.joinpath(*rel.split("/"))
            if _copy_traversable(
                src,
                dst,
                skip_existing=True,
                worktree=worktree,
                repo_root=repo_root,
            ):
                seeded.append(f"{BMAD_DIR}/{rel}")
    if not seeded:
        return [], []
    return ([BMAD_DIR] if not had_bmad else seeded), seeded


def _record_seeded(
    seeded_from: dict[Path, Path],
    landed: Sequence[Path],
    src: Path,
    dst: Path,
) -> None:
    """Record where each path a seed entry just wrote was copied FROM.

    `_copy_traversable` reproduces the source tree's shape under ``dst``, so a landed
    path's rel against ``dst`` is also its source's rel against ``src``; the root of a
    file entry gives ``Path(".")``, which pathlib drops, mapping ``dst`` to ``src``.

    ``src`` is the RESOLVED source, so a seed entry reached through a symlink names
    the file that really holds the bytes rather than the link the operator listed.
    First writer wins: nothing overwrites an earlier entry, because a path is only
    ever written once — copy-when-absent makes the second entry naming it a skip, and
    a skip lands nothing to record (#592).
    """
    for path in landed:
        seeded_from.setdefault(path, src / path.relative_to(dst))


def _written_rels(worktree: Path, landed: Sequence[Path]) -> list[str]:
    """Worktree-relative rels of the FILES a copy call just wrote.

    Feeds the shield's tracked-directory substitution (#484): over a tracked tool
    dir the whole-dir pattern is replaced by one pattern per file provisioning
    actually landed there, so this is the ledger of what there is to shield.

    The :func:`_is_file` filter is load-bearing rather than defensive.
    ``_copy_traversable``'s ``copied_paths`` records every destination that landed —
    each file written AND each directory the call created (``install.py:1427-1436``).
    A directory rel reaching the substitution would render as another whole-dir
    pattern, reintroducing the exact shape #484 removes, one level down.

    ``as_posix`` so a substituted pattern anchors on Windows too; ``os.sep`` would
    not, and the exclude is read by git, not by the platform.
    """
    return [p.relative_to(worktree).as_posix() for p in landed if _is_file(p)]


def _bmad_scripts_seed_incomplete(worktree: Path, repo_root: Path) -> bool:
    """Whether a required repo renderer unit member missed the worktree.

    Match the renderer preflight's content-keyed required-file predicate. Arbitrary
    sibling scripts are still merge-seeded, but their absence cannot prove the
    renderer will HALT and therefore must not arm the CRITICAL escalation gate.
    """
    return any(
        _renderer_unit_required(repo_root, rel)
        and _is_file(repo_root / rel)
        and not _is_file(worktree / rel)
        for rel in RENDERER_SCRIPT_UNIT_REL
    )


def _central_config_seed_incomplete(worktree: Path, repo_root: Path) -> bool:
    """Whether the repo's required renderer config failed to reach the worktree."""
    return _is_file(repo_root / CENTRAL_CONFIG_REL) and not _is_file(worktree / CENTRAL_CONFIG_REL)


def base_skills_seed_incomplete(worktree: Path, repo_root: Path, trees: Sequence[str]) -> list[str]:
    """Required upstream skill rels present in the repo but absent in the worktree.

    ``BASE_SKILLS`` is a copy-if-present catalog, not a requirement set. Gate only
    the selected primitive and required review skills; advisory/conditional and
    inactive catalog entries are still provisioned best-effort, but their absence
    cannot prove the dev/review session will stall. Unknown review shapes use the
    same merged-or-standalone fallback as the run-start preflight.

    Within those active skills, mirror the deterministic run-start contract rather
    than treating every descendant as fatal: reviewers require ``SKILL.md``; the dev
    primitive additionally requires its defined markers and any renderer snapshot
    sources its worktree copy resolves. Other descendants still copy best-effort,
    but their absence does not prove the outer session will write no artifact.

    A missing ``SKILL.md`` reports the coarse skill rel. A repo directory without
    ``SKILL.md`` remains the run-start preflight's concern and cannot produce a false
    CRITICAL escalation here. Stories mode adds its content-keyed dispatch probe at
    the caller, where the run mode is available.
    """
    missing: list[str] = []
    for tree in dict.fromkeys(trees):
        primitive = dev_primitive_or_default(repo_root, tree)
        for skill in _required_worktree_skills(repo_root, tree):
            repo_skill = repo_root / tree / skill
            worktree_skill = worktree / tree / skill
            if not _is_file(repo_skill / "SKILL.md"):
                # Distinguish an unreadable directory from a skill the repo simply
                # does not carry. The walk yields only the former as a directory leaf.
                if any(_is_dir(src) for _, src in _walk_traversable_files(repo_skill)):
                    missing.append(f"{tree}/{skill}")
                continue
            if not _is_file(worktree_skill / "SKILL.md"):
                missing.append(f"{tree}/{skill}")
                continue
            if skill != primitive:
                continue
            missing.extend(
                f"{tree}/{skill}/{rel}"
                for rel in DEV_PRIMITIVE_MARKERS
                if _is_file(repo_skill / rel) and not _is_file(worktree_skill / rel)
            )
            missing.extend(
                f"{tree}/{skill}/{rel}"
                for rel in _absent_renderer_sources(worktree_skill)
                if _is_file(repo_skill.joinpath(*rel.split("/")))
            )
    return missing


def worktree_seed_undelivered(
    worktree: Path,
    repo_root: Path,
    seed_files: Sequence[str] = (),
    seed_globs: Sequence[str] = (),
    config_paths: Sequence[str] = (),
) -> list[str]:
    """Seed rels the repo carries that never reached the worktree.

    Source containment is deliberately *not* an eligibility requirement: a source
    symlink resolving outside the repo is the canonical entry the seed loop refuses
    and this result check must report. Delivery does require every usable source
    entry to remain within the repo, matching the copier. Destination containment is
    likewise required so a path outside the worktree cannot masquerade as delivery.

    Hook configs need a different result question because provisioning writes the
    hook registration itself after seeding. For those rels, existence proves nothing;
    source escape, a symlinked destination, or destination escape proves the seed was
    refused. This report is informational and is never an escalation gate.
    """
    unresolved_repo_root = repo_root
    try:
        worktree = worktree.resolve()
        repo_root = repo_root.resolve()
    except (OSError, RuntimeError, ValueError):
        # Observation only: root uncertainty cannot prove delivery, but it must
        # not turn an informational journal probe into a run-wide failure.
        rels = [str(rel) for rel in seed_files]
        for pattern in seed_globs:
            try:
                matches = sorted(unresolved_repo_root.glob(pattern))
            except (OSError, RuntimeError, ValueError):
                continue
            rels.extend(match.relative_to(unresolved_repo_root).as_posix() for match in matches)
        return list(dict.fromkeys(rels))
    rels = [str(rel) for rel in seed_files]
    for pattern in seed_globs:
        try:
            matches = sorted(repo_root.glob(pattern))
        except (OSError, RuntimeError, ValueError):
            continue
        rels.extend(match.relative_to(repo_root).as_posix() for match in matches)
    hook_configs = {Path(rel) for rel in config_paths}

    def contained(path: Path, root: Path) -> bool:
        try:
            return path.resolve().is_relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return False

    def delivered(src: Path, dst: Path) -> bool:
        """Whether every usable source descendant has a matching destination."""
        if not contained(src, repo_root) or not contained(dst, worktree):
            return False
        if _is_file(src):
            return _is_file(dst)
        if not _is_dir(src) or not _is_dir(dst):
            return False

        complete = True

        def visit_dir(rel: str, source) -> bool:
            nonlocal complete
            if not isinstance(source, Path) or not contained(source, repo_root):
                complete = False
                return False
            target = dst.joinpath(*rel.split("/")) if rel else dst
            if not contained(target, worktree) or not _is_dir(target):
                complete = False
            return True

        for child_rel, child in _walk_traversable_files(src, _visit_dir=visit_dir):
            target = dst.joinpath(*child_rel.split("/")) if child_rel else dst
            if _is_file(child):
                if (
                    not isinstance(child, Path)
                    or not contained(child, repo_root)
                    or not contained(target, worktree)
                    or not _is_file(target)
                ):
                    complete = False
            elif _is_dir(child):
                # Directories are yielded only when enumeration was refused, so
                # their unknown descendants cannot be claimed as delivered.
                complete = False
        return complete

    undelivered: list[str] = []
    for rel in dict.fromkeys(rels):
        src = repo_root / rel
        if not (_is_file(src) or _is_dir(src)):
            continue
        dst = worktree / rel
        if Path(rel) in hook_configs:
            try:
                destination_is_link = dst.is_symlink()
            except OSError:
                destination_is_link = True
            if not contained(src, repo_root) or not contained(dst, worktree) or destination_is_link:
                undelivered.append(rel)
            continue
        if delivered(src, dst):
            continue
        undelivered.append(rel)
    return undelivered


def module_skills_seed_undelivered(
    worktree: Path,
    trees: Sequence[str],
    skills_root: Traversable | None = None,
) -> list[str]:
    """Wheel-bundled ``MODULE_SKILLS`` whose content never reached the worktree.

    Re-probes DISK, never the copier's bookkeeping: a user-authored
    ``scm.worktree_seed`` entry that happens to spell a skill rel can therefore
    neither forge nor mask a report. The source is the wheel's own skills tree, which
    may be a zip Traversable rather than a real directory, so enumeration goes through
    the shared :func:`install._walk_traversable_files` walk instead of ``rglob``.

    Presence is the whole contract; content is NEVER compared. Seeding is per-FILE
    no-clobber, so a checkout carrying its own divergent fork of a bundled skill keeps
    those bytes while its absent siblings are filled in — comparing content would
    report that healthy shape as undelivered. A skill the wheel itself lacks is
    skipped: a broken wheel is the module-skills sync test's concern, not a run's.

    Informational, and NEVER an escalation gate. No ``MODULE_SKILLS`` entry has a
    worktree-resident consumer — sweep triage dispatches ``/bmad-loop-sweep`` at the
    MAIN checkout, ``bmad-loop-resolve`` runs at the main checkout too, and
    ``bmad-loop-setup`` has no session consumer at all — so an absence here cannot
    prove a stall, and a CRITICAL gate would refuse healthy runs. Extension point: if
    a consumer ever becomes worktree-resident (e.g. sweep triage moving into unit
    worktrees), arm the gate by routing this predicate's result into
    :meth:`WorktreeFlow.escalate_unit` for that consumer's required subset.

    Returns coarse ``"{tree}/{skill}"`` posix rels in iteration order.
    """
    if skills_root is None:
        skills_root = resources.files("bmad_loop.data").joinpath("skills")
    try:
        worktree = worktree.resolve()
    except (OSError, RuntimeError, ValueError):
        # This is a journal-only observation. Root uncertainty means every
        # bundled skill the wheel actually carries is coarsely undelivered.
        return [
            f"{tree}/{skill}"
            for tree in dict.fromkeys(trees)
            for skill in MODULE_SKILLS
            if _is_file(skills_root.joinpath(skill)) or _is_dir(skills_root.joinpath(skill))
        ]

    def contained(target: Path) -> bool:
        try:
            return target.resolve().is_relative_to(worktree)
        except (OSError, RuntimeError, ValueError):
            return False

    def delivered(src: Traversable, dst: Path) -> bool:
        """Whether every usable wheel entry has a matching worktree destination."""
        for rel, entry in _walk_traversable_files(src):
            target = dst.joinpath(*rel.split("/")) if rel else dst
            if _is_file(entry):
                if contained(target) and _is_file(target):
                    continue
                return False
            if _is_dir(entry):
                # Directories are yielded only when enumeration was refused, so
                # their unknown descendants cannot be claimed as delivered.
                return False
            # Neither a readable file nor a directory: the copier has no path for
            # such an entry either, so its absence is not a delivery failure.
        return True

    undelivered: list[str] = []
    for tree in dict.fromkeys(trees):
        for skill in MODULE_SKILLS:
            src = skills_root.joinpath(skill)
            if not (_is_file(src) or _is_dir(src)):
                continue
            if not delivered(src, worktree / tree / skill):
                undelivered.append(f"{tree}/{skill}")
    return undelivered


def provision_worktree(
    worktree: Path,
    profiles: Sequence[CLIProfile],
    repo_root: Path,
    seed_files: Sequence[str] = (),
    seed_globs: Sequence[str] = (),
    *,
    on_degraded: Callable[[str], None] | None = None,
) -> list[str]:
    """Make a freshly-created git worktree a self-sufficient bmad-loop project.

    A worktree checks out tracked files only, but the skill trees (.claude/skills,
    .agents/skills), the hook config, and the project's gitignored MCP/CLI configs
    are absent from the checkout. Without them the bundled bmad-loop-* skills are missing,
    the Stop-signal hook never fires, and isolated sessions can't reach their MCP
    server. Lay the bundled skills + signal hook into the worktree for the active
    CLI profiles, and copy the `seed_files` configs in from the main repo. The
    upstream skills the orchestrator drives (BASE_SKILLS: bmad-build-auto + the review
    hunters, plus whatever review layers this project's own config names) are not
    bundled in the wheel, so they are copied from the MAIN REPO's installed tree
    instead — together with `_bmad/custom/`, the customization those layers resolve
    through, so the isolated run resolves the same layer set the preflight
    validated. Quiet (no stdout) — unlike `install_into` this runs inside the
    engine loop under a TUI. No-op when there's nothing to do.

    seed_globs are project-relative glob patterns (e.g. ".claude/skills/*") expanded
    against the main repo; every match is copied into the worktree under the same
    relative path, copy-when-absent like seed_files. A game-engine plugin uses these
    to pull its MCP-generated skill tree (gitignored, so absent from the checkout)
    into a per_worktree Editor's checkout.

    A `seed_files` entry naming a DIRECTORY whose destination already exists is
    seeded child by child: the children the checkout lacks are copied in, the ones
    it carries are left untouched. A worktree checks out tracked files, so such a
    dir always exists and the entry would otherwise be a total no-op (issue #230).

    Kept safe against the unit's eventual `git add -A` commit:
    - skills + seed files are copied only when ABSENT — at FILE granularity, so a
      project that commits its own skill tree (e.g. .agents/) or config keeps it
      untouched (no diff merged back);
    - the hook points at the MAIN repo's already-installed relay via an absolute
      path (the relay locates its events directory from $BMAD_LOOP_EVENTS_DIR,
      falling back to $BMAD_LOOP_RUN_DIR/events — never from its own location),
      so nothing is written into the worktree's .bmad-loop/. Since #494 the
      primary channel is out of the project tree entirely, so a worktree cannot
      carry a run's control plane at all — but the fallback still resolves under
      the MAIN run dir, so the guarantee holds for an older installed relay too;
    - everything we wrote is excluded from git, in a file private to THIS worktree
      (`.git/worktrees/<id>/info/exclude`, activated per-worktree) that dies with it
      when the worktree is removed. It is never the repository-wide
      `.git/info/exclude`: that file is shared with the operator's own checkout and
      permanent, so shielding through it hid every new file under a tool dir from
      their `git add -A` forever (#384). That write is best-effort: when git can't
      be queried at all it is skipped silently, but any fault after that — including
      a refusal to scope the shield, and a shield that was written but which git does
      not resolve to — is reported to `on_degraded` (once, with the reason) rather
      than swallowed, and the shield is skipped rather than widened back to the
      shared file. An unshielded worktree lets `git add -A` stage the tool files, so
      it must not fail invisibly.
    Skill trees, the per-CLI hook config, and the seeded configs all live in dirs
    projects gitignore — but the exclude shields them even when a project doesn't.

    seed_files are copied BEFORE the hook step so a seeded settings file that is
    also a hook config_path (.claude/settings.json, .gemini/settings.json) keeps its
    real content rather than being created empty. Its relay entry is replaced, not
    kept: a seeded copy can carry a legacy workspace relay or stale installed
    command, so the hook step replaces it with this installation's absolute
    command. A config that is already there but
    cannot be parsed refuses provisioning outright — `verify.GitError`, which the
    caller escalates as CRITICAL and pauses the run — rather than being replaced by
    a hooks-only file: an unparseable config is evidence of an earlier fault, and the
    operator's bytes are left intact for inspection. The refusal sends the repair to
    whichever source supplied those bytes — read from the per-path record of what
    seeding actually wrote, never inferred from the seed entry that covers the path
    (#592).

    The repo's `_bmad/` surface is also merge-seeded, excluding generated render
    output. Renderer and upstream-skill completeness failures share the return
    channel with ordinary no-op seeds so they are journaled, but the caller re-probes
    the skill result and content-gates the renderer sentinels before escalating.

    Returns the `seed_files` entries that copied NOTHING because everything they
    name was already present, plus reserved completeness reports. A directory entry
    that seeded even one child is not a no-op and is not reported.
    """
    if not profiles and not seed_files and not seed_globs and not _is_dir(repo_root / BMAD_DIR):
        return []
    unresolved_worktree = worktree
    unresolved_repo_root = repo_root
    try:
        worktree = worktree.resolve()
        repo_root = repo_root.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise verify.GitError(
            "cannot resolve worktree provisioning roots safely "
            f"(worktree={unresolved_worktree}, repo_root={unresolved_repo_root}): {e}"
        ) from e
    skills_root = resources.files("bmad_loop.data").joinpath("skills")

    # project gitignored MCP/CLI configs: copy from the main repo when absent.
    # Resolve-and-contain guards against an `..`/absolute entry escaping either tree.
    seeded: list[str] = []
    # Every FILE provisioning actually wrote, worktree-relative. `seeded` answers per
    # ENTRY and collapses a directory to its root; this stays per-file, because over a
    # TRACKED tool directory the shield drops the dir pattern and substitutes one
    # pattern per file we landed under it (#484). A path is here only if this run
    # wrote it, which is what makes the substituted patterns untracked by construction
    # — copy-when-absent cannot land on top of a tracked child.
    written: set[str] = set()
    # Which source supplied each path seeding actually WROTE, keyed by where it
    # landed. `seeded` answers per ENTRY and cannot be read per FILE: a directory
    # entry records only its own rel below, and copy-when-absent skips occupied
    # children one at a time, so a dir rel here means "at least one child landed",
    # never "this child did". The hook step reads this map to name the real source of
    # an unparseable config, where guessing sends the operator to repair the wrong
    # file (#592). `_copy_traversable` mirrors the source layout under `dst`, so each
    # landed path's rel is its source's rel too.
    seeded_from: dict[Path, Path] = {}
    # Entries that named a real source but copied nothing, because every path they
    # name already exists. Reported to the caller (this function is quiet by
    # contract — it runs under a TUI) because the no-op is otherwise silent: an
    # entry that reads as applied configuration is not. Per-CHILD skips inside a
    # directory entry are deliberately not reported — the checkout is expected to
    # carry its tracked children, so that is routine rather than a
    # misconfiguration, exactly like the glob-expanded matches below.
    skipped: list[str] = []
    for rel in seed_files:
        raw = worktree / rel
        try:
            src = (repo_root / rel).resolve()
            dst = raw.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
            continue
        if not (_is_file(src) or _is_dir(src)):
            continue
        if _occupied(dst):
            # A live symlink remains the ordinary existing-destination no-op. This
            # arm must precede the raw-vs-resolved refusal below so it stays named in
            # `skipped` rather than turning into a silent drop.
            if dst != raw:
                skipped.append(str(rel))
                continue
            # File entries keep the classic copy-when-absent skip. A destination
            # that is not a directory while the source is (the checkout carries a
            # FILE where the seed names a dir) is a type mismatch: recursing would
            # try to mkdir over the file, so the entry is skipped whole instead.
            if not _is_dir(src) or not _is_dir(raw):
                skipped.append(str(rel))
                continue
            # A DIRECTORY whose destination exists is the case #230 reported: the
            # checkout carries some tracked child, so the whole entry used to be a
            # no-op — including the gitignored children that are absent and would
            # clobber nothing. Recurse instead, copying only what is missing.
            landed: list[Path] = []
            if not _copy_traversable(
                src,
                raw,
                skip_existing=True,
                worktree=worktree,
                repo_root=repo_root,
                copied_paths=landed,
            ):
                # every child was already present: still a total no-op, still
                # reported. Only a PARTIAL seed stops being reported.
                skipped.append(str(rel))
                continue
            # Partially seeded, so the entry must still reach `patterns` below:
            # the children we just wrote have to stay out of the unit's
            # `git add -A`. Excluding the whole dir is safe — an exclude does not
            # untrack the tracked children that were already there.
            seeded.append(rel)
            _record_seeded(seeded_from, landed, src, raw)
            written.update(_written_rels(worktree, landed))
            continue
        # `resolve()` is non-strict, so a dangling leaf or parent link answers for
        # its target. Never mkdir/copy through it. Existing live links were handled
        # by the skip arm above, preserving copy-when-absent reporting.
        if dst != raw:
            continue
        landed = []
        if _copy_traversable(
            src,
            raw,
            skip_existing=True,
            worktree=worktree,
            repo_root=repo_root,
            copied_paths=landed,
        ):
            seeded.append(rel)
            _record_seeded(seeded_from, landed, src, raw)
            written.update(_written_rels(worktree, landed))

    # glob-seeded trees (e.g. an engine plugin's MCP skill dirs): expand each
    # pattern against the main repo and copy matches in, same contain guard +
    # copy-when-absent semantics. rel is taken from the unresolved match so the
    # worktree path mirrors the repo layout; resolve only guards containment.
    for pattern in seed_globs:
        try:
            matches = sorted(repo_root.glob(pattern))
        except (OSError, RuntimeError, ValueError):
            continue
        for match in matches:
            rel = match.relative_to(repo_root)
            raw = worktree / rel
            try:
                src = match.resolve()
                dst = raw.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
                continue
            if not (_is_file(src) or _is_dir(src)) or _occupied(dst):
                continue
            if dst != raw:
                continue
            landed = []
            if not _copy_traversable(
                src,
                raw,
                skip_existing=True,
                worktree=worktree,
                repo_root=repo_root,
                copied_paths=landed,
            ):
                continue
            # as_posix so the exclude pattern anchors on Windows too (os.sep would not)
            seeded.append(rel.as_posix())
            _record_seeded(seeded_from, landed, src, raw)
            written.update(_written_rels(worktree, landed))

    # Renderer-backed skills are handed the worktree as their project root. Merge
    # the repo's project-local BMAD surface after explicit seeds (operator intent wins
    # on collisions) and reserve the two renderer sentinels for result-side checks.
    seeded_bmad, bmad_written = _seed_bmad_tree(worktree, repo_root)
    written.update(bmad_written)
    skipped = [rel for rel in skipped if rel not in RENDERER_SEED_SENTINELS]
    if _bmad_scripts_seed_incomplete(worktree, repo_root):
        skipped.append(BMAD_SCRIPTS_SEED_REL)
    if _central_config_seed_incomplete(worktree, repo_root):
        skipped.append(CENTRAL_CONFIG_REL)

    # bundled skills into each CLI's skill tree (deduped: codex+gemini share one);
    # never clobber a skill the checkout already carries (tracked or pre-existing).
    for tree in dict.fromkeys(p.skill_tree for p in profiles):
        tree_dir = worktree / tree
        for skill in MODULE_SKILLS:
            dst = tree_dir / skill
            # `copied_paths` is read only by the shield: when this tree turns out to
            # be TRACKED, these files are what gets a pattern in place of the dropped
            # dir pattern (#484). Completeness is still re-probed on disk below —
            # copy bookkeeping never arms a gate.
            landed = []
            _copy_traversable(
                skills_root.joinpath(skill),
                dst,
                skip_existing=True,
                worktree=worktree,
                copied_paths=landed,
            )
            written.update(_written_rels(worktree, landed))
        # Wheel MODULE_SKILLS get the same result-side completeness re-probe as every
        # other seeded surface (`module_skills_seed_undelivered`, journaled by the
        # caller as `worktree-module-skills-dropped`) — but journal-only, never the
        # CRITICAL gate below: no MODULE_SKILLS entry has a worktree-resident consumer
        # (sweep triage and bmad-loop-resolve dispatch at the MAIN checkout;
        # bmad-loop-setup has no session consumer), so a partial wheel copy cannot
        # prove a stall the way a missing upstream dev/review skill does. A future
        # worktree-resident consumer arms the gate via escalate_unit over that
        # predicate's result.
        # The orchestrator-driven upstream skills are not in the wheel; copy them
        # from the MAIN REPO's installed tree (same tree path) so an isolated
        # worktree can still resolve the dev primitive and the review layers. Skip
        # silently when the main repo lacks them — the run-start preflight reports
        # it.
        #
        # BASE_SKILLS names BOTH primitive eras, which is what carries the skill
        # across the rename: the resolution below returns REVIEW skills only, so a
        # primitive the catalog did not name would be silently left behind (the
        # is_dir guard swallows the miss) and every isolated session would stall on
        # an Unknown command.
        #
        # BASE_SKILLS is only the floor. The review layers this project actually
        # invokes are read from the installed primitive — resolved on disk, so a
        # renamed project resolves its own layers rather than degrading to the
        # static catalog — exactly as the preflight reads them, so a reviewer named
        # by a project override (a custom or renamed skill) is provisioned too.
        # Validating a skill here and then not copying it is how preflight passes in
        # the main checkout while the isolated review fails on a skill that was
        # never there.
        for skill in _worktree_skill_copy_candidates(repo_root, tree):
            dst = tree_dir / skill
            try:
                src = (repo_root / tree / skill).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if not src.is_relative_to(repo_root) or not _is_dir(src):
                continue
            landed = []
            _copy_traversable(
                src,
                dst,
                skip_existing=True,
                worktree=worktree,
                repo_root=repo_root,
                copied_paths=landed,
            )
            written.update(_written_rels(worktree, landed))

    # Re-ask the result rather than trusting copy bookkeeping. A user-authored seed
    # can happen to spell a skill rel, so only this disk predicate may arm the gate.
    skipped.extend(
        base_skills_seed_incomplete(
            worktree, repo_root, [profile.skill_tree for profile in profiles]
        )
    )

    # per-CLI signal-hook registration, baked to the main repo's relay (absolute).
    # Hookless profiles (HTTP/SSE transport) have no config to merge.
    stripped_paths: set[Path] = set()
    for profile in profiles:
        if profile.hookless:
            continue
        raw_config_path = worktree / profile.hooks.config_path
        # Refuse before mkdir, read, or write: hook commands are worktree-specific
        # and must never mutate a shared dotfile through a live or dangling link.
        # Inspect every component: a non-strict resolve either leaves a symlink cycle
        # unresolved — and so textually equal to the raw path — or raises RuntimeError,
        # which the except below takes; which of the two happens is interpreter-version
        # dependent. The resolved comparison additionally refuses ``..`` and absolute
        # profiles.
        refused = not raw_config_path.is_relative_to(worktree)
        cursor = raw_config_path
        try:
            while not refused and cursor != worktree:
                if cursor.is_symlink():
                    refused = True
                    break
                cursor = cursor.parent
            config_path = raw_config_path.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if refused or config_path != raw_config_path or not config_path.is_relative_to(worktree):
            continue
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config: dict = {}
        if _is_file(config_path):
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                # Refuse, exactly as `_register_hooks` does at init
                # (install.py:1244-1248): same file, same merge, one policy (#592).
                # JSON has no partial read, so a config that will not parse is
                # evidence of an earlier fault rather than a blank slate — and
                # swallowing it to `{}` is not a degrade but a destructive write:
                # `baseline_config` below deep-copies that `{}`, so the change gate
                # always fires and publishes a hooks-only file over the operator's
                # allowlist, env and MCP entries, erasing the very evidence.
                # UnicodeDecodeError rides along because `read_text` raises it on
                # invalid UTF-8 — the same "operator file unreadable as content"
                # family, and uncaught it crashes the engine instead of escalating.
                # Raising mid-loop, after earlier profiles were provisioned, is
                # safe: the escalation pauses the run, and provisioning re-runs on
                # resume without duplicating its work (pinned by
                # test_shield_reprovision_does_not_duplicate_patterns).
                #
                # Send the repair to whatever SUPPLIES these bytes, which is never
                # this file: the worktree is disposable, an escalated story re-enters
                # the run only through a re-arm, and that discards the worktree
                # (`engine._finish_inflight` -> `discard_worktree`) and provisions a
                # fresh one. `seeded_from` answers which source that is POSITIVELY —
                # keyed on THIS path, and populated only where a copy actually landed.
                # Both halves are load-bearing. Existence of a main-checkout
                # counterpart proves nothing: seeding is copy-when-absent, so a config
                # the project TRACKS is skipped as an occupied destination and arrives
                # with the branch checkout instead (the call site says so at the
                # `config_path` seed), and repairing the counterpart would not take —
                # the fresh worktree checks the committed version out again and the
                # refusal recurs, so that lane is told to commit. Nor can provenance
                # come from the seed ENTRY that covers this path: a directory entry is
                # recorded once, for the parent, while its children are skipped
                # individually, so a config landing under a seeded `.claude/` and one
                # merely sitting there beside a seeded sibling are indistinguishable
                # at that granularity — and misreading the second as seeded advises
                # committing a gitignored file that may carry credentials.
                # ESCALATED is terminal with no transition out (`statemachine.py`),
                # and `resolve` takes a required `run_id` (`cli.py:4232`), so the
                # remedy names the re-arm in a form that actually runs.
                seed_rel = profile.hooks.config_path
                seeded_source = seeded_from.get(config_path)
                if seeded_source is not None:
                    src_note = (
                        f"this copy was seeded from {seeded_source}, which a "
                        "re-arm seeds in again, so"
                    )
                    remedy = f"repair or remove {seeded_source} in the main checkout"
                else:
                    src_note = (
                        "nothing seeded this copy — it arrived with the branch "
                        "checkout, which a re-arm checks out again, so"
                    )
                    remedy = f"commit a repaired {seed_rel} on the target branch"
                raise verify.GitError(
                    f"hook config {config_path} cannot be parsed ({e}); an "
                    "unparseable config is evidence of an earlier fault, not a blank "
                    "slate — provisioning refuses rather than replace the operator's "
                    "allowlist, env, and MCP settings with a hooks-only file. "
                    f"{src_note} {remedy}, then re-arm this escalation with "
                    "`bmad-loop resolve <run-id> --no-interactive`: ESCALATED is "
                    "terminal, so repairing the file alone does not put the story "
                    "back in the run (#592)"
                ) from e
        try:
            registrations = {
                native: _hook_command(repo_root, profile, canonical)
                for native, canonical in profile.hooks.events.items()
            }
        except ProfileError as e:
            raise verify.GitError(f"cannot register worktree relay: {e}") from e
        # A seeded config_path (.claude/settings.json is both a seeded file and the
        # hook config) may carry a legacy workspace relay or a stale installed
        # command. Strip it first and register this installation's entry point.
        # Strip only on
        # FIRST encounter per config file: profiles can share a config_path
        # (user-overlay aliases of one CLI), and a later profile's pass must not
        # tear out the relay events an earlier one just registered — merge_hooks
        # unions its events in, the first registration winning a shared event.
        baseline_config = copy.deepcopy(config)
        if config_path not in stripped_paths:
            strip_relay_hooks(config, profile.hooks.dialect)
            stripped_paths.add(config_path)
        config, _ = merge_hooks(config, registrations, profile.hooks.dialect)
        # Write — and pin — only when the strip+merge actually changed the parsed
        # config. A tracked config may already carry exactly the installed command,
        # so strip-then-merge nets to
        # zero, and a pin would claim orchestrator ownership of a file this run
        # never modified, hiding a story's own edit to it for no benefit.
        if config != baseline_config:
            # atomic_write_text, never write_text (#379). `_register_hooks` states
            # the rule at length; the stakes are higher here. This config is the
            # SEEDED copy of the operator's own — allowlist, env, MCP entries and
            # their hooks all round-trip through the parse above — and a truncating
            # `"w"` publishes a prefix of it on a short write. Nothing downstream
            # re-reads it to complain, either: the session starts against a settings
            # file whose JSON no longer parses, so the CLI falls back to its defaults
            # and the Stop hook never registers. That is #363's stall with no
            # diagnostic. follow_symlinks stays at the default, matching the
            # `write_text` it replaces — and the symlink question is already settled
            # above this line, by the component-wise refusal walk that skips the
            # profile entirely when any component of the path is a link — a walk
            # stricter than the confined writers' (it refuses a link anywhere on the
            # path, in the worktree as well as out), so a confined write would
            # relax this site rather than harden it. The #597 flag is the whole
            # change here: `os.replace` needs write permission on the parent
            # DIRECTORY, never on the entry it replaces, so a seeded config the
            # operator had marked read-only was rewritten anyway and came back
            # reading `0444`. This is the operator's own settings file, round-tripped
            # through the parse above, so a read-only one earns the `PermissionError`
            # the `write_text` this replaced raised.
            atomic_write_text(
                config_path,
                json.dumps(config, indent=2) + "\n",
                require_writable_target=True,
            )
            pin_degrade = _pin_tracked_config_rewrite(worktree, profile.hooks.config_path)
            if pin_degrade is not None and on_degraded is not None:
                on_degraded(pin_degrade)

    # Shield exactly the paths we wrote (skill trees + hook configs + seeded
    # configs) from the unit's `git add -A`, in case a project doesn't gitignore
    # its tool dirs. Scoped to this worktree and expiring with it — these are
    # paths projects legitimately TRACK, so a repo-wide exclude here went on
    # hiding their new files from the operator's own checkout (#384).
    #
    # Built here at ENTRY granularity, which is only provisional: the reconcile step
    # below re-asks git what each one names, drops a pattern over a tracked file as
    # inert (#392), and over a tracked DIRECTORY swaps the entry for one pattern per
    # file `written` recorded under it (#484). `written` is therefore not an
    # alternative source of patterns but the substitution ledger that step reads.
    patterns = {f"/{p.skill_tree}" for p in profiles}
    # hookless profiles have no config_path, so there is nothing to shield: their
    # empty string would render as a bare "/", which git strips to a zero-length
    # pattern (the trailing slash becomes MUSTBEDIR) that matches nothing — inert,
    # unlike "/*" or "*", which do blanket. A junk line in a generated file, then,
    # not a worktree-wide exclusion.
    patterns |= {f"/{p.hooks.config_path}" for p in profiles if not p.hookless}
    patterns |= {f"/{rel}" for rel in seeded}
    patterns |= {f"/{rel}" for rel in seeded_bmad}
    if f"/{BMAD_DIR}" not in patterns:
        # The renderer may create or rewrite this generated directory during the
        # session, after provisioning has finished. Give it a dedicated transient
        # shield unless the blanket root shield already subsumes it: `/_bmad` prunes
        # the directory before git descends, so `/_bmad/render/` would provably never
        # be consulted. Avoiding that inert sibling keeps the worktree-local exclude
        # precise; the file and its lines disappear with this worktree.
        patterns.add(f"/{RENDER_DIR_REL}/")
    patterns, tracked_degrade = _reconcile_tracked_patterns(worktree, patterns, written)
    if tracked_degrade is not None and on_degraded is not None:
        on_degraded(tracked_degrade)
    # Escaping is the LAST transform: `_reconcile_tracked_patterns` strips the leading
    # "/" and probes git with the LITERAL rel, and the per-file patterns it substitutes
    # come back RAW too, so it has to keep seeing and emitting unescaped patterns —
    # escaping must stay downstream of the tracked-pattern transform.
    reason = _worktree_local_exclude(worktree, sorted(_escape_exclude_pattern(p) for p in patterns))
    if reason is not None and on_degraded is not None:
        on_degraded(reason)
    return skipped


@dataclass(frozen=True)
class _AcceptedSpecEnds:
    """What :meth:`WorktreeFlow._accepted_spec_pair` actually resolved.

    Replaces the ``tuple | None`` the locator returned until DW-104, whose ``None``
    collapsed four distinct outcomes into one silence: not applicable, a source that
    would not resolve, a source resolving outside the project, and a destination
    escaping the mount. Telling those apart is the whole point — two of them are
    losses a unit dispatches straight through, and two of them are spellings this
    path has no claim on at all.

    Encoding, from least to most resolved:

    - not applicable (empty/absolute ``spec_file``) or the source resolve raised:
      every field ``None``. ``faulted`` is what separates the two — the locator
      could not RESOLVE the spec at all (a swallowed filesystem fault, or a
      spelling that no longer resolves because the path is gone) versus a spelling
      that was never this path's business. It is deliberately not narrower than
      that: one ``except`` covers the whole source resolve, and the two causes are
      indistinguishable from inside it.
    - source resolved but outside the project: ``source`` set, ``relative`` None.
      An out-of-tree artifacts dir the mount reads directly; no claim, no record.
    - destination escapes the mount (or the mount root will not resolve):
      ``source`` and ``relative`` set, ``destination`` None. The containment
      refusal — the rel IS known, which is what lets it be nominated as a seed.
    - usable: every field set.

    Existence of neither end is asserted here; that stays the caller's arm (the
    seed copies only into an ABSENT destination, the supersession warning compares
    only against a PRESENT one).
    """

    source: Path | None = None
    relative: str | None = None
    destination: Path | None = None
    faulted: bool = False


class WorktreeFlow:
    """Provision, drive, integrate and reclaim per-unit git worktrees.

    Built once per engine from narrow deps + engine callbacks (see module
    docstring). Behavior is identical to the cluster it was carved out of; the
    only structural changes are that engine-owned effects go through injected
    callables: ``emit`` fires a plugin hook (late-bound so a monkeypatched
    ``Engine._emit`` still wins), ``save`` persists run state, ``gate_unit`` runs
    the per-unit ready gate, ``carry_isolated_ledger_writes`` applies Engine-owned
    ledger bookkeeping after a successful merge, ``workspace_get``/``workspace_set``
    read and swap the engine's active workspace, and ``escalation_pause`` raises the
    engine's ``RunPaused`` (injected so this module need not import ``engine`` — that
    would reintroduce a runtime<->engine import cycle)."""

    def __init__(
        self,
        *,
        paths: ProjectPaths,
        policy: Policy,
        state: RunState,
        journal: Journal,
        run_dir: Path,
        registry: PluginRegistry,
        adapters_get: Callable[[], dict[str, CodingCLIAdapter]],
        open_unit_workspace: Callable[..., UnitWorkspace],
        emit: Callable[..., object],
        save: Callable[[], None],
        gate_unit: Callable[[StoryTask], bool],
        carry_isolated_ledger_writes: Callable[[StoryTask], None],
        escalation_pause: Callable[..., NoReturn],
        workspace_get: Callable[[], Workspace],
        workspace_set: Callable[[Workspace], None],
    ) -> None:
        self.paths = paths
        self.policy = policy
        self.state = state
        self.journal = journal
        self.run_dir = run_dir
        self._registry = registry
        # Read live (a getter, not a captured dict) so a test that rebinds
        # `engine.adapters` after construction is still seen here.
        self._adapters_get = adapters_get
        # Injected late-bound so a test patching the `engine.open_unit_workspace`
        # module global still wins (worktree_flow's own binding wouldn't).
        self._open_unit_workspace = open_unit_workspace
        self._emit = emit
        self._save = save
        self._gate_unit = gate_unit
        self._carry_isolated_ledger_writes = carry_isolated_ledger_writes
        self._pause = escalation_pause
        self._workspace_get = workspace_get
        self._workspace_set = workspace_set
        verify.reconcile_integration_state_roots(
            run_dir, (task.integration_attempt for task in state.tasks.values())
        )

    @property
    def isolated(self) -> bool:
        return self.policy.scm.isolation == "worktree"

    def ensure_target_branch(self) -> None:
        """Resolve (once, at run start) the branch every unit merges back into.

        No-op unless isolation=worktree. Default target is the branch checked out
        now; a configured target is created if missing and checked out in the
        main repo (merges land on whatever the main repo has checked out, and a
        unit worktree must never check out the target itself). Pinned in state so
        resume keeps targeting the same branch."""
        if not self.isolated or self.state.target_branch:
            return
        if self.policy.scm.failed_diff_unlimited:
            # the safety cap is off; make sure the operator knows a failed unit
            # could write a very large forensic patch.
            self.journal.append(
                "scm-failed-diff-unlimited",
                note="failed-unit diff capture is uncapped (scm.failed_diff_unlimited); "
                "changes.patch may be very large",
            )
        repo = self.paths.repo_root
        configured = self.policy.scm.target_branch.strip()
        if configured:
            if not verify.branch_exists(repo, configured):
                try:
                    verify.create_branch(repo, configured, "HEAD")
                except verify.GitError as e:
                    # e.g. an unborn repo (no commit to base a branch on).
                    self._pause(f"cannot create target branch {configured!r}: {e}", cause=e)
                self.journal.append("target-branch-created", branch=configured)
            if verify.current_branch(repo) != configured:
                verify.checkout_branch(repo, configured)
                self.journal.append("target-branch-checkout", branch=configured)
            self.state.target_branch = configured
        else:
            current = verify.current_branch(repo)
            if current == "HEAD":
                # detached HEAD has no branch to merge into; merges would land on
                # an unreferenced commit. Require a real branch (or a configured
                # target) before isolating work into worktrees.
                self._pause(
                    "isolation=worktree on a detached HEAD: check out a branch or "
                    "set scm.target_branch before running"
                )
            self.state.target_branch = current
        self.journal.append("target-branch", branch=self.state.target_branch)
        self._save()

    def worktree_profiles(self) -> list[CLIProfile]:
        """The distinct CLI profiles of the dev + review adapters, for provisioning
        their skills/hooks into a worktree. Adapters without a `profile` (e.g. test
        fakes) contribute nothing, so provisioning is a no-op for them.

        The role set is :data:`install.DEV_PRIMITIVE_ROLES` — the same constant
        `cli._skill_trees` gates on — rather than a local pair, so the provisioned
        set and the gated set cannot drift apart. A tree gated but not provisioned
        ships a session into the `Unknown command` stall the preflight exists to
        catch; a tree provisioned but not gated refuses runs over a skill no session
        reads."""
        seen: dict[str, CLIProfile] = {}
        adapters = self._adapters_get()
        for adapter in (adapters[role] for role in DEV_PRIMITIVE_ROLES):
            profile = getattr(adapter, "profile", None)
            if profile is not None and profile.name not in seen:
                seen[profile.name] = profile
        return list(seen.values())

    def engine_agent_ids(self) -> list[str]:
        """The Unity-MCP `setup-mcp` agent ids for every CLI that runs in a
        worktree (dev + review). A worktree can host more than one agent — e.g.
        dev=claude, review=codex — and each reads its own MCP config file, so the
        per_worktree setup must point every one of them at the worktree's Editor,
        not just the dev agent. Deduped, order-preserving; empty for test fakes."""
        ids: list[str] = []
        for profile in self.worktree_profiles():
            agent = _setup_mcp_agent_id(profile.name)
            if agent not in ids:
                ids.append(agent)
        return ids

    def _ledger_seed(self, worktree: Path) -> tuple[str, ...]:
        """The deferred-work ledger, when a worktree checkout cannot deliver it.

        `git worktree add` checks out TRACKED files only, so a project that
        gitignores its ledger — the default shape — gets a unit worktree with
        none. The orchestrator is the ledger's single writer under the generic
        dev path and writes through ``self.workspace.paths``, i.e. that missing
        copy: ``mark_done`` returns False on an absent file,
        ``verify_review_bundle`` reads the same absent file and never sees the
        ids `done`, so the bundle defers on a fixable retry and `open_ids`
        re-bundles the same work for ever (#426). No DONE-leg carry can rescue
        that — the unit never reaches DONE. Seeding moves the failure onto a leg
        that has one, where ``SweepEngine._carry_isolated_ledger_writes`` applies
        the close to the main checkout.

        The copy is not what delivers the close: every seeded rel is shielded
        from the unit's ``git add -A``, so the worktree's flip never rides the
        merge. The seeded ledger exists so the GATE can read the orchestrator's
        own write; the carry stays the delivery path, hence
        ``sweep-bundle-close-carry-uncommitted`` on a run that lands.

        Excluded: a ledger already in the checkout (without the exclusion the copy
        would be a no-op the seed loop reports as ``worktree-seed-skipped`` on every
        tracked-ledger project); one absent from the main checkout (dropped
        silently, so the entry would be invisible rather than merely inert — the
        common case, since the first harvest is what CREATES the file); and one
        resolving outside the project tree, which for an out-of-tree artifacts DIR
        ``ProjectPaths.rebased`` leaves unmoved, so the worktree already reads it.
        Presence is asked of the WORKTREE, not of git: that is the predicate the
        seed loop itself decides on, and unlike ``verify.path_tracked`` it costs no
        subprocess and cannot raise.

        A ledger that is itself a symlink keeps the dir in-tree, so ``rebased``
        moves it and the worktree path does not exist: the exclusion is still right
        for an out-of-repo target (``provision_worktree`` refuses that source
        whatever rel it is handed), but an in-repo target is seeded to the WRONG
        path and still hits #426 (#462). Resolving also names the target of a
        TRACKED ledger symlink whose target is untracked — the only path the seed
        loop will write, since it refuses to copy through a link. ``relative_to``
        decides PLACEMENT; containment is re-checked against both roots at the copy
        site.

        Deduped against ``scm.worktree_seed`` by the caller.
        """
        ledger = self.paths.deferred_work
        repo = self.paths.repo_root
        try:
            rel = ledger.resolve().relative_to(repo.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return ()
        if not _is_file(ledger) or _is_file(worktree / rel):
            return ()
        return (rel,)

    def _board_seed(self, worktree: Path) -> tuple[str, ...]:
        """The sprint board, when a worktree checkout cannot deliver it (#350).

        The structural sibling of :meth:`_ledger_seed`, for the other
        orchestrator-owned artifact in the BMAD artifacts dir, and the same shape
        for the same reason: `git worktree add` checks out TRACKED files only, so a
        project that gitignores its board gets a unit worktree with none, while the
        orchestrator writes the board through ``self.workspace.paths`` — i.e. that
        missing copy.

        What the ledger's absence costs is a livelock; the board's is a CRASH.
        ``Engine._post_dev_state_sync``'s ``advance`` returns None on a file that is
        not there (a silent no-op), and ``verify_dev`` then reads the SAME absent
        file through ``story_status``, where ``sprintstatus.load`` raises
        ``SprintStatusError`` — a class cli, operatoractions and the TUI all catch
        and neither engine.py nor verify.py does, so the story dies and takes the
        run with it. Seeding removes that structurally: the gate reads the
        orchestrator's own write instead of a hole.

        Per the maintainer decision on #350 the worktree copy is CANONICAL for the
        duration of the story. As with the ledger, the copy is not what delivers the
        advance back: every seeded rel is shielded from the unit's ``git add -A``,
        so the worktree's flip never rides the merge, and a post-merge carry is the
        delivery path.

        Excluded, arm for arm as the ledger's: a board already in the checkout
        (tracked, hence delivered — seeding it would copy nothing and journal
        ``worktree-seed-skipped`` on every isolated unit of every project that
        tracks its board, which is the common shape for this file); one absent from
        the main checkout, which the seed loop drops SILENTLY — neither
        ``worktree-seed-skipped`` nor ``worktree-seed-dropped`` — so naming it would
        be invisible rather than merely inert (a story run cannot reach here without
        a board, since ``_pick_next`` reads it first, but a sweep or stories run
        needs no board at all); and one resolving outside the project tree, which
        for an out-of-tree artifacts dir ``ProjectPaths.rebased`` leaves unmoved, so
        the worktree already reads this very file. Presence is asked of the
        WORKTREE, not of git, for the ledger's reason: it is the predicate the seed
        loop itself decides on, it costs no subprocess and it cannot raise.

        A board that is itself a symlink inherits ``_ledger_seed``'s caveat verbatim
        (#462); it is derived there and not repeated here.

        INHERITED LIMITATION — parity, not a regression, and NOT fixed here: a
        non-fixable rollback does not restore a seeded board. Rollback resets
        TRACKED paths and removes untracked NON-IGNORED ones
        (``verify.untracked_files``); an ignored board is in neither set, so nothing
        puts back the pre-attempt status. An attempt that advanced the worktree
        board to ``done`` therefore PINS it there — ``advance`` never regresses — and
        a later attempt that parks instead cannot satisfy ``verify_dev``'s
        ``awaiting-operator`` expectation. That is exactly what an in-place
        gitignored board already does under ``isolation = "none"``; only a TRACKED
        board escapes, via the rollback's ``reset --hard``. Seeding neither
        introduces the trap nor widens it.

        Deduped against ``scm.worktree_seed`` by the caller.
        """
        board = self.paths.sprint_status
        repo = self.paths.repo_root
        try:
            rel = board.resolve().relative_to(repo.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return ()
        if not _is_file(board) or _is_file(worktree / rel):
            return ()
        return (rel,)

    def _accepted_spec_seed(
        self,
        task: StoryTask,
        worktree: Path,
        *,
        project_relative_only: bool = False,
    ) -> tuple[str, ...]:
        """Copy an accepted project-local spec a tracked checkout did not deliver.

        The isolation-flip normalizer gives a main-checkout absolute spec a
        project-relative spelling before this point. If the accepted file is
        ignored or untracked, ``git worktree add`` cannot carry it, so binding the
        new attempt would silently fall back to the bare story key. Seed only a
        canonical regular file inside the project, only when the mounted checkout
        lacks its corresponding path. Absolute/external spellings pass through;
        prior-attempt binding fields remain authoritative until fresh binding
        replaces them after the mount is provisioned.

        Both existence arms probe through ``_is_file``, total over ``OSError``,
        rather than ``Path.is_file``: on Python <=3.13 the raw probe RAISES on an
        entry below an unsearchable parent (3.14 answers false, as
        :func:`install._is_file` records), and this call site sits outside every
        ``except`` in ``run_isolated``, so such a fault killed the run instead of
        allowing dispatch to continue.

        Which arm a given fault can actually reach differs, because the locator
        resolves the two ends differently. A parent that is merely unsearchable
        does NOT reach the source arm on any interpreter: ``_accepted_spec_pair``
        resolves the source ``strict=True``, which raises first and folds the pair
        to ``None``. The source arm is reachable only by TOCTOU between that
        resolve and this stat, or by a non-EACCES ``OSError``. The DESTINATION is
        resolved ``strict=False``, which can leave an inaccessible suffix unresolved;
        other resolution failures are still caught by the locator. A source probe
        fault omits the seed; a destination probe fault treats that end as absent
        and leaves delivery to the seed loop. This uses the same total probe as
        :meth:`_ledger_seed`, :meth:`_board_seed` and
        :meth:`_warn_accepted_spec_superseded` (DW-103).

        A rel whose DESTINATION escapes the mount is nominated anyway (DW-104).
        Until then the locator's containment refusal returned ``()``, so the rel
        reached neither ``seed_files`` nor ``skipped_seeds`` nor
        ``undelivered_seeds`` and no journal named it. Returning it is safe only
        because the copier judges containment for itself: ``provision_worktree``
        re-derives ``dst`` and ``continue``s on ``not dst.is_relative_to(worktree)``
        before any copy, so nothing is written outside the mount, and
        :func:`worktree_seed_undelivered` then reports the same rel because its own
        ``contained(dst, worktree)`` arm fails. This return is a NOMINATION, not a
        permission — never rely on this method's judgement to keep a copy inside
        the mount.
        """
        ends = self._accepted_spec_pair(task, worktree, project_relative_only=project_relative_only)
        relative, source = ends.relative, ends.source
        # One narrowing, not a refusal: the locator sets `relative` only alongside
        # `source`, so this pair is either both present or there is nothing to seed.
        if relative is None or source is None:
            return ()
        # ONE existence arm for both branches below. A source that is not a regular
        # file is nominated by neither: `provision_worktree` would recurse a DIRECTORY
        # into the mount, and a containment refusal that came from an unresolvable
        # mount root rather than a real escape can still leave the copier a contained
        # `dst` to copy that tree onto.
        if not _is_file(source):
            return ()
        if ends.destination is None:
            return (relative,)
        if _is_file(ends.destination):
            return ()
        return (relative,)

    def _accepted_spec_pair(
        self,
        task: StoryTask,
        worktree: Path,
        *,
        project_relative_only: bool = False,
    ) -> _AcceptedSpecEnds:
        """Locate the accepted project-local spec's (rel, main-checkout, mount) ends.

        The ONE locator :meth:`_accepted_spec_seed`,
        :meth:`_warn_accepted_spec_superseded` and
        :meth:`_warn_accepted_spec_undelivered` all decide on. Extracted rather than
        restated so a record can never report a loss against a pair the seed was
        not looking at: two copies of this derivation drift the moment one of them
        learns something about spec spellings the other does not, and the record
        would then name a file whose delivery its own gate never measured.

        Returns :class:`_AcceptedSpecEnds` rather than the ``tuple | None`` it
        answered until DW-104 — see that class for the encoding. What changed is
        only how much of the outcome is REPORTED: the refusals are the same
        refusals, and every caller that wanted "usable or nothing" still gets it by
        requiring the fields it needs. The two states the widening exists for are
        ``destination is None`` with a known ``relative`` (the containment refusal,
        which can now be nominated as a seed and named in `worktree-seed-dropped`)
        and ``faulted`` — the source would not resolve at all, whether because a
        filesystem fault was swallowed or because the spelling no longer resolves.
        Either way it is not the same silence as a spelling this path has no claim
        on.

        Not a canonical project-local accepted artifact, and all-None with
        ``faulted=False``: an empty or absolute ``spec_file`` (an external spelling
        passes through untouched). Deliberately asserts the existence of NEITHER end
        — that is the caller's arm, and the callers want opposite answers to it (the
        seed copies only into an ABSENT destination; the supersession warning
        compares only against a PRESENT one).
        """
        raw = task.spec_file
        if not raw or Path(raw).is_absolute():
            return _AcceptedSpecEnds()
        try:
            project = self.paths.project.resolve(strict=True)
            source = (
                self.paths.project / raw
                if project_relative_only
                else verify.resolve_spec_path(raw, self.paths)
            ).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            # The whole source resolve, so `faulted` means "could not resolve it",
            # not "a filesystem fault occurred": a `strict=True` resolve of a path
            # that is simply gone lands here too, and nothing inside can tell the
            # two apart. The record it feeds says exactly that much and no more.
            return _AcceptedSpecEnds(faulted=True)
        try:
            relative = source.relative_to(project)
        except ValueError:
            # Resolves outside the project: an out-of-tree artifacts dir the mount
            # already reads directly. A spelling with no claim, not a fault.
            return _AcceptedSpecEnds(source=source)
        rel = relative.as_posix()
        try:
            destination = (worktree / relative).resolve(strict=False)
            mounted_root = worktree.resolve(strict=True)
            destination.relative_to(mounted_root)
        except (OSError, RuntimeError, ValueError):
            # The containment refusal (and an unresolvable mount root, which cannot
            # prove containment either). The REL is known, which is what lets
            # `_accepted_spec_seed` nominate it and `worktree_seed_undelivered`
            # name it.
            return _AcceptedSpecEnds(source=source, relative=rel)
        return _AcceptedSpecEnds(source=source, relative=rel, destination=destination)

    def _warn_accepted_spec_superseded(
        self,
        task: StoryTask,
        worktree: Path,
        *,
        project_relative_only: bool = False,
    ) -> None:
        """Journal when a fresh mount's copy of the accepted spec is not the
        operator's (DW-101).

        The ``pause_after_spec`` approval gate hands the operator a spec that is
        UNCOMMITTED by construction. For a TRACKED artifacts dir a re-drive's
        ``git worktree add`` then delivers the COMMITTED bytes into the mount,
        :meth:`_accepted_spec_seed` skips (its destination already exists) and the
        ``accepted_delivered`` probe in :meth:`run_isolated` passes on existence and
        containment alone — so the corrections the operator just made are silently
        superseded by the pre-approval text. The escalation path already answers this
        exact loss with ``rearm-spec-write-unreachable``; this is the approval path's
        equivalent.

        ADVISORY ONLY, and deliberately so on both counts: this method refuses,
        pauses and rewrites NOTHING. It does not overwrite the mount's copy — a dirty
        TRACKED file inside the mount is not covered by the worktree-local
        ``info/exclude`` fold, so ``finalize_commit``'s ``git add -A`` would merge the
        operator's in-progress edits into the story commit. And it does not refuse the
        mount, which would hard-fail every isolated unit in a project that tracks its
        artifacts dir. What happens to the unit afterwards is not this method's claim
        to make (the ready gate may still veto the dispatch); the remedy is the
        operator's, and it is the record's whole payload: commit the corrected spec on
        the named branch.

        Field shapes are routing decisions, not taste. ``spec_file`` carries the
        MAIN-CHECKOUT absolute path — the file the operator has to commit, not the
        mount's copy of it — and rides ``diagnostics._JOURNAL_ALIAS_FIELDS``' ``spec``
        namespace. The branch is spelled ``target_branch`` rather than a fresh name
        because that scrub routes by field NAME and ``target_branch`` is already
        aliased to the ``branch`` namespace, while any new spelling falls through to
        ``scrub_json``, which waves an identifier-shaped branch name through verbatim.
        The discriminator is a bare boolean ``compared`` rather than a ``reason``/
        ``error`` string for the mirror-image reason: both of those names are in
        ``_JOURNAL_DROP_FIELDS`` and would ship as a presence marker instead of the
        distinction the record exists to draw.

        Silent for the two legs that are not this loss: a destination the mount never
        delivered is the seed's own case (and a hard delivery fault has already
        escalated above this call), and identical bytes are no loss at all. A read
        that raises answers ``compared: false`` — the probe cannot PROVE the mount
        reads the operator's bytes, and an unprovable delivery is exactly what this
        record is for. Nothing here may raise out: the locator swallows its own
        faults, ``_is_file`` is total over them, and the comparison is wrapped.

        "Identical" is read with line endings normalized (CRLF ≡ LF). The mount is
        a git CHECKOUT, so under Git-for-Windows' system ``core.autocrlf=true`` an
        LF-authored spec comes back CRLF there while the main checkout keeps the
        LF bytes the spec writer laid down — a difference git itself folds away on
        the next commit, and one this record must not report as a lost correction.
        Only that translation is folded: a lone CR, a missing final newline, or any
        other byte still counts, since git would carry those into the commit.
        """
        ends = self._accepted_spec_pair(task, worktree, project_relative_only=project_relative_only)
        source, destination = ends.source, ends.destination
        # A FULLY USABLE pair or nothing. Every refusal the locator now reports in
        # more detail (DW-104) is still a refusal here: there is nothing to compare
        # against a destination that escapes the mount, and comparing against a
        # source outside the project would name a file this path has no claim on.
        if source is None or destination is None or ends.relative is None:
            return
        if not _is_file(source) or not _is_file(destination):
            return
        try:
            identical = _crlf_normalized(source.read_bytes()) == _crlf_normalized(
                destination.read_bytes()
            )
        except (OSError, RuntimeError, ValueError):
            compared = False
        else:
            if identical:
                return
            compared = True
        self.journal.append(
            "accepted-spec-write-unreachable",
            story_key=task.story_key,
            spec_file=str(source),
            target_branch=self.state.target_branch,
            compared=compared,
        )

    def _warn_accepted_spec_undelivered(
        self,
        task: StoryTask,
        worktree: Path,
        *,
        project_relative_only: bool = False,
    ) -> None:
        """Journal when the mount cannot be shown to carry the accepted spec at all
        (DW-104 + DW-115).

        The silence this record ends had two mouths and one shape. Before DW-104
        :meth:`_accepted_spec_pair`'s containment arm refused a spec whose mounted
        parent escapes the worktree, and :meth:`_accepted_spec_seed` returned ``()``
        for it: the rel reached neither ``seed_files`` nor ``skipped_seeds`` nor
        ``undelivered_seeds``. DW-115 is the second mouth: the locator's own
        ``except`` swallows everything that stops the source RESOLVING — a real
        filesystem fault, or a spelling that no longer resolves because the path is
        gone — and produces the SAME ``()``. For
        a spec already spelled project-relative — ``accepted_spec_relocated`` false,
        which is the spelling a resume persists — the escalating ``accepted_delivered``
        probe in :meth:`run_isolated` does not run either, so the unit dispatched
        against a mount lacking the operator's spec, fell back to the bare story key,
        and nothing in the journal named why. The seed widening restores the first
        mouth's visibility through ``worktree-seed-dropped``; this record covers both,
        and is the only thing that covers the swallowed fault.

        ADVISORY ONLY. It refuses nothing, pauses nothing, writes nothing into the
        mount and is read by no gate. The RELOCATED leg is deliberately not its
        business: that leg already escalates on the same loss a few lines above the
        call site, so a RECORD here as well would be a second advisory naming a unit
        that never reaches a session. (The relocated leg does still emit a
        ``worktree-seed-dropped`` entry for a containment refusal — that is the seed
        report, not this one.) The human decision dated 2026-09-04 on DW-104 is record,
        do not escalate — the escalating probe's ``if accepted_spec_relocated:`` gate
        does not move.

        Silent for the spellings this path has no claim on, which is why the locator's
        detail is required rather than a bare "not delivered": an empty or absolute
        ``spec_file`` and one resolving OUTSIDE the project (a shared out-of-tree
        artifacts dir the mount reads directly) have no ``relative`` and are not
        ``faulted``; an external source can still be set. They return before any probe. The two entry conditions are a project-local
        rel the locator RESOLVED, and ``faulted`` — the source would not resolve at
        all, so the rel is unknown and ``task.spec_file`` is the best spelling
        available.

        Field shapes are routing decisions, mirroring
        :meth:`_warn_accepted_spec_superseded` for the reasons stated there:
        ``spec_file`` carries the MAIN-CHECKOUT path (the locator's ``source`` when it
        resolved, else ``self.paths.project / task.spec_file`` — both reduce to the
        same basename alias in ``diagnostics``) and rides the ``spec`` namespace, and
        the branch is spelled ``target_branch`` because that name is already aliased
        to the ``branch`` namespace while a fresh spelling falls through to
        ``scrub_json`` and ships an identifier-shaped branch name verbatim. The
        discriminator is a bare boolean ``located`` — whether the locator resolved a
        project-local rel at all, i.e. which of the two mouths this is: ``false``
        says the spec could not be resolved (faulted or gone), ``true`` says it was
        resolved and the MOUNT could not be shown to carry it — rather than a
        ``reason``/``error`` string, because both of those names are in
        ``diagnostics._JOURNAL_DROP_FIELDS`` and would ship as a presence marker
        instead of the distinction the record exists to draw.

        Nothing here may raise out: the locator swallows its own faults, ``_is_file``
        is total over them, and the containment resolve is wrapped. A probe that
        cannot resolve has not PROVEN delivery, which is exactly what the record is
        for, so every fault answers "not delivered" rather than propagating.
        """
        ends = self._accepted_spec_pair(task, worktree, project_relative_only=project_relative_only)
        if ends.relative is None and not ends.faulted:
            return
        probe = worktree / (ends.relative or str(task.spec_file))
        # File-ness alone is not delivery, for the reason the escalating probe states:
        # a parent that is a real directory in the main checkout but a committed
        # OUTWARD symlink in the mounted commit lands the probe on an unrelated
        # external artifact, which answers true. Require containment as well.
        try:
            delivered = _is_file(probe) and probe.resolve(strict=False).is_relative_to(
                worktree.resolve()
            )
        except (OSError, RuntimeError, ValueError):
            delivered = False
        if delivered:
            return
        source = (
            ends.source if ends.source is not None else self.paths.project / str(task.spec_file)
        )
        self.journal.append(
            "accepted-spec-delivery-unreachable",
            story_key=task.story_key,
            spec_file=str(source),
            target_branch=self.state.target_branch,
            located=ends.relative is not None,
        )

    def run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        """Run one unit's `drive` body in a fresh per-unit worktree, then merge
        it back into the target branch. `drive` either returns (DONE/DEFERRED →
        integrate) or raises RunPaused (spec-approval gate / escalation → leave
        the worktree mounted for resume/inspection, integration skipped)."""
        # A sprint run can accept an absolute spec in the main checkout before
        # isolation is enabled. Convert only that canonical project-local accepted
        # artifact to the portable spelling the new mount can own. This must happen
        # before worktree creation and must not disturb an existing attempt binding,
        # whose path/snapshot authority belongs to its prior workspace.
        accepted_spec_before = task.spec_file
        task.relativize_project_local_accepted_spec(self.paths.project)
        accepted_spec_relocated = task.spec_file != accepted_spec_before
        try:
            unit = self._open_unit_workspace(
                self.paths.repo_root,
                self.paths,
                self.state.run_id,
                task.story_key,
                self.state.target_branch,
                self.policy.scm.branch_per,
                self.run_dir,
                # The accepted spec the reclaim must not destroy. `_accepted_spec_seed`
                # lays a gitignored spec into the mount and the shield then ignores it
                # there, so the orphan holds the only copy; the isolation flip kept
                # `spec_file` at its mount-relative spelling for exactly this reason
                # (`release_spec_paths_from_mount`), which is the spelling the reclaim's
                # snapshot resolves against the mount.
                spec_file=task.spec_file,
                # An orphan an isolation flip left standing at this unit's mount
                # path is reclaimed by the open; its uncommitted state is parked
                # first and named here so the recovery ref is discoverable from the
                # journal (`isolation-flip-orphaned-worktree` recorded the orphan).
                on_orphan_preserved=lambda worktree, ref: self.journal.append(
                    "isolation-flip-orphan-preserved",
                    story_key=task.story_key,
                    worktree=worktree,
                    ref=ref,
                ),
            )
        except verify.GitSpawnError as e:
            # a spawn fault is machine-wide, not this unit's: deferring would
            # march the whole queue into DEFERRED one notification at a time
            # and end the run "finished" over a broken environment (#194/#343).
            self._pause(
                f"cannot spawn git while opening a worktree for {task.story_key}: {e}",
                task.story_key,
                cause=e,
            )
        except verify.GitError as e:
            # could not mount a worktree (e.g. branch_per=run with a kept-failed
            # unit still holding the shared branch). Defer this unit rather than
            # crash the whole run; the operator can free the branch and re-run.
            task.defer_reason = f"could not open worktree: {e}"
            task.phase = Phase.DEFERRED  # deliberate: no legal move from PENDING
            self.journal.append("worktree-open-failed", story_key=task.story_key, error=str(e))
            gates.notify(
                self.policy, self.run_dir, f"worktree open failed: {task.story_key}", str(e)
            )
            self._save()
            return
        task.worktree_path = str(unit.path)
        self.journal.append(
            "worktree-opened", story_key=task.story_key, branch=unit.branch, path=str(unit.path)
        )
        task.branch = unit.branch
        if task.dw_ids:
            try:
                artifact_publication.capture(task, self.paths)
            except (artifact_publication.PublicationError, OSError, ValueError) as exc:
                self._save()
                self._pause(f"artifact baseline capture failed: {exc}", task.story_key, cause=exc)
        # A worktree checks out tracked files only, but the bmad-loop-* skill
        # trees + signal-hook config are typically gitignored, so they are absent
        # from the fresh checkout. Re-lay them into the worktree so the bundled
        # bmad-loop-* skills are present and the Stop-signal hook fires. Also seed the loaded
        # adapters' gitignored MCP/CLI configs so isolated sessions can reach their
        # MCP server (seed_adapter_defaults) plus any extra project-listed paths.
        profiles = self.worktree_profiles()
        scm = self.policy.scm
        seeds: list[str] = []
        if scm.seed_adapter_defaults:
            for profile in profiles:
                seeds.extend(profile.seed_files)
                # The hook config is seeded from the SAME list that shields it. It was
                # only ever in the shield set (`provision_worktree`'s `patterns`), never
                # here, so whether it got seeded depended on a profile happening to name
                # it twice: claude's `seed_files` carries `.claude/settings.json`, which
                # is also its `config_path`, and codex's does not carry
                # `.codex/hooks.json` (#471).
                #
                # What seeding fixes, stated only as far as it is measured: the hook
                # step in `provision_worktree` creates and writes that config whether
                # or not it was seeded (`merge_hooks` on an absent file returns
                # changed=True), so bmad-loop's OWN Stop hook registers either way.
                # What an unseeded worktree loses is the PROJECT's hook configuration —
                # the session runs against a file holding the relay registrations alone.
                # #471's reported stall is consistent with the CLI declining hooks from
                # a config it has not trusted (codex.toml's `first_run_note`), but that
                # mechanism is UNCONFIRMED and nothing here rests on it; the issue's own
                # stated mechanism — the file being absent — is false at this line.
                #
                # Deriving it from `config_path` rather than restating it per profile is
                # what keeps a future profile from regressing the same way. Hookless
                # profiles have no config to seed. The seed loop skips an occupied
                # destination, so a project that TRACKS this path keeps its checked-out
                # copy untouched.
                if not profile.hookless and profile.hooks.config_path:
                    seeds.append(profile.hooks.config_path)
        seeds.extend(scm.worktree_seed)
        # the two orchestrator-owned artifacts a tracked-only checkout can leave
        # behind — each decides its own exclusions; see the methods.
        seeds.extend(self._ledger_seed(unit.path))
        seeds.extend(self._board_seed(unit.path))
        seeds.extend(
            self._accepted_spec_seed(
                task,
                unit.path,
                project_relative_only=accepted_spec_relocated,
            )
        )
        # plugins (e.g. the Unity engine) may prime an isolated checkout with
        # gitignored paths they need — e.g. an MCP-generated skill tree + client
        # config so the worktree's Editor MCP is reachable. Aggregate every loaded
        # plugin's declared seeds.
        seeds.extend(self._registry.seed_files())
        seed_files = list(dict.fromkeys(seeds))  # dedupe, preserve order
        seed_globs = self._registry.seed_globs()
        try:
            skipped_seeds = provision_worktree(
                unit.path,
                profiles,
                self.paths.repo_root,
                seed_files=seed_files,
                seed_globs=seed_globs,
                on_degraded=lambda msg: self._exclude_degraded(task.story_key, msg),
            )
        except verify.GitError as e:
            # Every provisioning refusal carries its own cause — an unresolvable
            # root, a config pin that could not be recorded, an unparseable seeded
            # hook config (#592) — so the wrapper names the unit and defers the
            # "why" to the inner message rather than asserting one of the three.
            reason = f"cannot safely provision the worktree for {task.story_key}: {e}"
            self.escalate_unit(task, reason)  # always raises RunPaused
        if skipped_seeds:
            # A seed entry whose destination already exists is a no-op. Harmless for
            # a file the checkout legitimately carries, but a directory entry is
            # skipped WHOLE the moment any child is tracked — so a `worktree_seed`
            # that looks applied can be copying nothing. Journal it; provision is
            # quiet by contract (it runs under the TUI).
            self.journal.append(
                "worktree-seed-skipped", story_key=task.story_key, entries=skipped_seeds
            )

        # A dropped arbitrary seed is observable but not fatal. Unlike the two gates
        # below, bmad-loop cannot know that a user/plugin config is required by the
        # session, and its usual trigger is a healthy shared/dotfile-managed config.
        # Hook config destinations cannot prove delivery because provisioning writes
        # the Stop registration itself after the seed step.
        undelivered_seeds = worktree_seed_undelivered(
            unit.path,
            self.paths.repo_root,
            seed_files=seed_files,
            seed_globs=seed_globs,
            config_paths=[p.hooks.config_path for p in profiles if not p.hookless],
        )
        if undelivered_seeds:
            self.journal.append(
                "worktree-seed-dropped", story_key=task.story_key, entries=undelivered_seeds
            )

        # The last guard standing over a relocated accepted spec, and the only one
        # that STOPS a bind to an outside file. `_accepted_spec_seed` refuses on its
        # own containment arm — since DW-104 not silently: the refused rel is
        # nominated into `seed_files` anyway, `provision_worktree` re-derives `dst`
        # and copies nothing because it escapes, and `worktree-seed-dropped` above
        # names it. That report is informational, though, and this probe is what
        # keeps the unit from dispatching against someone else's bytes.
        # File-ness alone is not enough to call the spec delivered.
        # `_is_file` follows symlinks and asks only "are there bytes here", so a
        # parent that is a real directory in the main checkout but a committed
        # OUTWARD symlink in the commit this mount was cut from lands the probe on an
        # unrelated external artifact, which answers true and dispatches the unit
        # reading someone else's bytes under the accepted spec's name. Require
        # containment as well: resolve the probe and keep it inside the mount. A
        # legitimate INWARD link whose target is under the mount still passes, and an
        # unresolvable probe cannot prove delivery, so every filesystem fault
        # escalates rather than binding.
        if accepted_spec_relocated:
            accepted_probe = unit.path / str(task.spec_file)
            try:
                accepted_delivered = _is_file(accepted_probe) and accepted_probe.resolve(
                    strict=False
                ).is_relative_to(unit.path.resolve())
            except (OSError, RuntimeError, ValueError):
                accepted_delivered = False
            if not accepted_delivered:
                self.escalate_unit(
                    task,
                    f"accepted spec for {task.story_key} disappeared before it could be "
                    "delivered to the replacement worktree",
                )

        trees = [p.skill_tree for p in profiles]
        # The wheel's own bundled skills, journal-only like worktree-seed-dropped but
        # under their own kind so a user seed that spells a skill rel can neither forge
        # nor mask an entry. No MODULE_SKILLS entry has a worktree-resident consumer,
        # so an absence here cannot prove a stall and must never escalate.
        undelivered_module_skills = module_skills_seed_undelivered(unit.path, trees)
        if undelivered_module_skills:
            self.journal.append(
                "worktree-module-skills-dropped",
                story_key=task.story_key,
                entries=undelivered_module_skills,
            )

        # Missing upstream skill content is a determinate stall for both inline and
        # renderer-era primitives. Re-probe disk rather than trusting skipped_seeds,
        # which a user-authored seed rel could otherwise forge. Check this first so a
        # wholly absent primitive is not misdiagnosed as a renderer-surface problem.
        absent_skills = base_skills_seed_incomplete(unit.path, self.paths.repo_root, trees)
        if absent_skills:
            reason = (
                "the worktree is missing required upstream skill contract files the repo has "
                f"({', '.join(absent_skills)}) — the session would stall having "
                "written nothing: on `Unknown command` when the whole skill is "
                "absent, or at a required primitive marker or renderer source. "
                "The usual cause is a required skill directory or file symlinked "
                "to a shared BMad install outside the repo, which worktree seeding "
                "cannot follow"
            )
            self.escalate_unit(task, reason)  # always raises RunPaused

        # Stories mode has a stricter, content-keyed primitive contract than sprint
        # mode. Re-run that exact preflight against the mounted worktree: a step-01
        # through-link can pass in the main checkout yet be refused during copying,
        # while a tracked stale worktree copy proves that existence alone is not
        # enough. Either shape would HALT a folder+id dispatch before writing a spec.
        stories_support = (
            missing_stories_support(unit.path, trees) if self.state.source == "stories" else []
        )
        if stories_support:
            short_dispatch = []
            for finding in stories_support:
                detail = finding.detail or {}
                rel = f"{detail['tree']}/{detail['skill']}/{detail['file']}"
                marker = detail.get("marker")
                short_dispatch.append(f"{rel} (missing {marker!r})" if marker else rel)
            reason = (
                "the worktree's dev primitive does not satisfy stories-mode dispatch "
                f"support ({', '.join(short_dispatch)}) — the folder+id session would "
                "HALT without writing a spec. The usual cause is a required router "
                "file symlinked to a shared BMad install outside the repo, which "
                "worktree seeding cannot follow"
            )
            self.escalate_unit(task, reason)  # always raises RunPaused

        # Provisioning owns these exact sentinel strings, but only a content-confirmed
        # renderer stub consumes the surface. The conjunct is load-bearing: inline
        # pre-#2601 SKILL.md projects may carry the same repo paths and must proceed.
        short_surface = [rel for rel in RENDERER_SEED_SENTINELS if rel in skipped_seeds]
        if short_surface and renderer_stub_resolved(self.paths.project, trees):
            reason = (
                f"the dev primitive renders via {RENDERER_SCRIPT_MARKER} but the "
                "worktree's renderer surface came up short of the repo's "
                f"({', '.join(short_surface)}) — the session would HALT without "
                "writing a spec. The usual cause is a symlinked _bmad/ pointing "
                "outside the repo, which worktree seeding cannot follow"
            )
            self.escalate_unit(task, reason)  # always raises RunPaused

        # The residue the delivery probe above cannot see, stated as a warning rather
        # than a write: a spec the mount DID deliver, whose bytes are the committed
        # ones rather than the operator's uncommitted corrections (DW-101). Ungated by
        # `accepted_spec_relocated` on purpose — a hard delivery fault has already
        # escalated above, and a spec the task already spelled project-relative
        # reaches exactly the same loss without ever passing through the normalizer.
        #
        # LAST, below every gate that escalates: each of the three above always raises,
        # so a call placed among them would record "the mount superseded your spec" for
        # a unit that then never got near a session. Here the only things left are the
        # ready gate's veto and drive() itself.
        self._warn_accepted_spec_superseded(
            task, unit.path, project_relative_only=accepted_spec_relocated
        )
        # The residue neither the seed journals nor the probe above can see, for the
        # ONE leg that has no escalating guard at all: a spec the task already spelled
        # project-relative whose delivery the mount cannot PROVE (DW-104 + DW-115) —
        # the locator refused on containment, or swallowed a filesystem fault, and
        # either way the unit dispatches against a mount lacking the operator's spec.
        # Gated on `not accepted_spec_relocated` because the relocated leg already
        # escalated a few lines up, so a RECORD there too would be a second advisory
        # naming a unit that never reaches a session — the `worktree-seed-dropped`
        # entry that leg still emits is the seed's report, not this one.
        # LAST, beside the DW-101 warning and for its stated
        # reason: below every gate that raises, so the record cannot name a unit that
        # never dispatched.
        if not accepted_spec_relocated:
            # `False` literally, not `accepted_spec_relocated`: inside this gate the
            # flag cannot be anything else, and spelling it as a variable would read
            # as if it varied the way it does at the call two lines above.
            self._warn_accepted_spec_undelivered(task, unit.path, project_relative_only=False)
        self._save()
        prev = self._workspace_get()
        self._workspace_set(unit.workspace)
        try:
            # A plugin (e.g. the Unity engine) may launch the unit's managed Editor
            # at pre_worktree_setup + wait for its MCP at pre_ready_gate before
            # driving. A veto (defer) at either stage leaves the task DEFERRED and
            # skips drive(); both fall through to _integrate_unit, which tears the
            # (empty) worktree down via the DEFERRED path.
            if self._gate_unit(task):
                self._emit("post_worktree_setup", task)
                drive(task)
        finally:
            # always run teardown — on success, on a deferral, and on a RunPaused
            # (spec gate / escalation) propagating through — before the workspace is
            # restored, so a managed Editor never outlives its worktree. Teardown
            # stages are observe-only (a veto here cannot un-tear-down).
            self._emit("pre_worktree_teardown", task)
            self._emit("post_worktree_teardown", task)
            self._workspace_set(prev)
        # reached only on a normal return (DONE or DEFERRED); a RunPaused from the
        # spec gate or an escalation propagates past here, leaving the worktree up.
        self.integrate_unit(task, unit)

    def _exclude_degraded(self, story_key: str, msg: str) -> None:
        """The git-add shield was owed for this unit's worktree and did not happen.

        Journaled AND notified, the way `worktree-open-failed` is above. The notify
        is the half that was missing: the shield's whole degrade policy is to SKIP
        rather than widen — activating over patterns it could not copy would shadow
        the operator's own excludes — and skipping is only defensible if the operator
        finds out. A run that ends "finished" with a journal line nobody reads is how
        the provisioned tool files reach a story's merge unnoticed.

        `gates.notify` is best-effort and never raises, and is inert unless
        `notify.file`/`notify.desktop` is configured, so this cannot break a run —
        which matters, because it is called from inside provisioning.
        """
        self.journal.append("worktree-exclude-degraded", story_key=story_key, error=msg)
        gates.notify(self.policy, self.run_dir, f"worktree exclude degraded: {story_key}", msg)

    def failed_diff_max_bytes(self) -> int | None:
        """Per-untracked-file size cap for a failed unit's forensic patch, in
        bytes — or None when the operator lifted the cap (scm.failed_diff_unlimited)."""
        scm = self.policy.scm
        if scm.failed_diff_unlimited:
            return None
        return scm.failed_diff_max_mb * 1_048_576

    def integrate_unit(self, task: StoryTask, unit: UnitWorkspace) -> None:
        self._emit("pre_integrate", task)
        scm = self.policy.scm
        # AWAITING_OPERATOR merges beside DONE: a parked story CARRIES A COMMIT
        # (that is what separates it from DEFERRED/ESCALATED), and stranding that
        # commit on a torn-down unit branch would lose finished work over an
        # obligation that lives outside the repo entirely. The human's remaining
        # actions are recorded in the registry, not in this worktree.
        if task.phase in (Phase.DONE, Phase.AWAITING_OPERATOR):
            # Merge the unit branch into the target branch locally. We open PRs
            # ourselves by hand once the branch has landed; the orchestrator only
            # commits the worktree onto the selected target.
            self.merge_local(task, unit)
            # Engine-authored ledger writes normally ride the unit commit, but a
            # gitignored ledger is omitted by finalize_commit's `git add -A` and
            # would otherwise disappear with successful teardown. Carry only after
            # merge: a tracked ledger reaches the target through the merge first,
            # where Engine's all-status provenance scan can deduplicate it.
            self._carry_isolated_ledger_writes(task)
            # Phase is already terminal and persisted before integration, so make
            # the carry completion durable here. Engine replays an unlatched carry
            # after a crash in the merge-to-carry window.
            task.isolated_ledger_carried = True
            self._save()
        else:  # DEFERRED — capture the diff, keep or drop per keep_failed
            patch = close_unit_workspace(
                unit,
                success=False,
                keep_failed=scm.keep_failed,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=scm.delete_branch,
                detach_kept=scm.branch_per == "run",
                diff_max_file_bytes=self.failed_diff_max_bytes(),
                on_teardown_degraded=lambda msg: self.journal.append(
                    "worktree-teardown-degraded", story_key=task.story_key, error=msg
                ),
            )
            self.journal.append(
                "unit-closed",
                story_key=task.story_key,
                branch=unit.branch,
                kept=scm.keep_failed,
                patch=str(patch) if patch else None,
            )

    def _carried_artifact_rels(self, repo: Path) -> tuple[str, ...]:
        """The repo-relative posix paths the RUN commits for itself after the merge —
        ``clean_incoming_collisions``' ``protected`` operand (#618).

        The sprint board and the deferred-work ledger, because those are the two
        files the three post-merge carries name: ``_carry_harvested_deferrals``
        and ``_carry_story_deferred_closes`` pass ``paths.deferred_work`` and
        ``_carry_board_advance`` passes
        ``paths.sprint_status``, all three to ``verify.commit_paths`` against this same
        ``repo``. That call stages by PATHSPEC — `git add -- :(literal)<path>` — so
        whatever the working tree holds at that path is committed no matter who wrote
        it, and a merge that walked past an operator's edit there hands the run its
        own bytes to commit under a `chore(...)` message. The blast radius is strictly
        same-path (`git commit -- <pathspec>` is implicitly `--only`), which is why
        this is an exact path set and not a policy.

        ``self.paths``, not ``self.workspace.paths``: the carries read the MAIN
        checkout's copies (their docstrings say so explicitly), and this is the
        checkout the merge lands in.

        Relativized exactly as ``commit_paths`` relativizes its own operands
        (``resolve().relative_to(repo.resolve()).as_posix()``) — that is what makes
        each entry the same string the carry will later hand ``git add``, and the same
        key shape ``verify.dirty_paths`` returns, so the guard's membership test is an
        equality it cannot get subtly wrong. Resolving is load-bearing rather than
        defensive: through a symlinked artifacts dir the unresolved rel names the LINK
        while both git and ``commit_paths`` name the target.

        A path that cannot be expressed relative to ``repo`` is dropped, not raised
        on: it cannot be dirty in this checkout, and ``commit_paths`` filters the same
        ``ValueError`` and so would never commit it either. ``_ledger_seed``'s
        three-way catch for the same reason — an unresolvable path (the WSL UNC
        provider fault, a symlink loop) omits only itself.

        TRACKED ONLY, and that is the whole boundary of the hazard rather than a
        precaution. What makes the carry dangerous is committing a DIVERGENCE from a
        baseline somebody else authored: on a tracked board, an operator's local
        reopen of a story row rides out under `chore(sprint-status): carry ...` with
        the tree left clean and nothing to read it back from. An UNTRACKED artifact
        has no such baseline — git reports the whole file as dirt because git has
        never seen it, the orchestrator has been reading that exact file as its own
        all along, and committing it is how a non-ignored board first reaches git at
        all (#350's carry). Protecting it would refuse the merge on EVERY isolated run
        of any project that has yet to commit its board — measured: an untracked,
        non-ignored board with no operator dirt anywhere ends the run
        `done=0 paused=True escalated=1` — which is the unattended-halt class #460 and
        #618 exist to remove, in exchange for a "hazard" that loses nothing (the bytes
        are committed, not overwritten). A gitignored artifact never reaches the
        question: ``dirty_paths`` does not report ignored files and ``git add`` refuses
        an ignored pathspec, so the carry degrades instead of committing.

        A trackedness probe that cannot answer keeps the path, the direction
        ``path_tracked``'s own callers degrade in: uncertainty must not be what
        authorizes writing an operator's bytes into the run's commit. The cost of
        being wrong that way is a refusal the operator can act on; the other way it is
        silent.
        """
        rels: list[str] = []
        for artifact in (self.paths.sprint_status, self.paths.deferred_work):
            try:
                rel = artifact.resolve().relative_to(repo.resolve()).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                tracked = verify.path_tracked(repo, rel)
            except verify.GitError:
                tracked = True
            if tracked:
                rels.append(rel)
        return tuple(rels)

    def _validated_integration_attempt(self, task: StoryTask) -> dict[str, Any] | None:
        raw = task.integration_attempt
        if raw is None:
            return None
        required = (
            "version",
            "target_ref",
            "strategy",
            "source_revision",
            "operation_identity",
            "pre_target_revision",
            "snapshots",
            "submodules",
            "phase",
            "cleanup_plan",
        )
        if not isinstance(raw, dict) or any(key not in raw for key in required):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if raw["version"] != 3 or any(
            not isinstance(raw.get(key), str) or not raw.get(key)
            for key in required
            if key not in {"version", "snapshots", "submodules", "cleanup_plan"}
        ):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if raw["strategy"] not in {"merge", "squash", "ff"}:
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        target_ref = raw["target_ref"]
        if (
            not target_ref.startswith("refs/heads/")
            or ".." in target_ref
            or "\\" in target_ref
            or any(ord(char) < 32 or ord(char) == 127 for char in target_ref)
        ):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        operation = raw["operation_identity"]
        revisions = (raw["source_revision"], raw["pre_target_revision"])
        if (
            len(operation) != 32
            or any(char not in "0123456789abcdef" for char in operation)
            or any(
                len(value) not in (40, 64) or any(char not in "0123456789abcdef" for char in value)
                for value in revisions
            )
        ):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        old = raw.get("old_revision")
        new = raw.get("new_revision")
        if (old is None) != (new is None) or any(
            value is not None and (not isinstance(value, str) or not value) for value in (old, new)
        ):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if any(
            value is not None
            and (
                len(value) not in (40, 64) or any(char not in "0123456789abcdef" for char in value)
            )
            for value in (old, new)
        ):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        outcome = raw.get("outcome")
        if outcome is not None and outcome != "refused-restored":
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        phase = raw.get("phase")
        cleanup_plan = raw.get("cleanup_plan")
        if phase not in {"armed", "cleanup-pending", "cleanup-applied", "integrated"}:
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if cleanup_plan is not None:
            if (
                not isinstance(cleanup_plan, dict)
                or set(cleanup_plan) != {"cleaned", "tolerated", "untracked"}
                or any(
                    not isinstance(cleanup_plan.get(key), list)
                    or any(not isinstance(path, str) for path in cleanup_plan[key])
                    for key in ("cleaned", "tolerated", "untracked")
                )
            ):
                raise verify.IntegrationEvidenceError(
                    "persisted target integration receipt is missing or malformed"
                )
            for key in ("cleaned", "tolerated", "untracked"):
                verify.preflight_integration_paths(cleanup_plan[key])
        if phase == "armed" and cleanup_plan is not None:
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if phase != "armed" and cleanup_plan is None:
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        # `index_flags`, `ignored`: optional — a receipt armed before either
        # was recorded reads without that reading
        allowed = set(required) | {
            "old_revision",
            "new_revision",
            "outcome",
            "index_flags",
            "ignored",
        }
        if set(raw) - allowed or (outcome is not None and old is None):
            raise verify.IntegrationEvidenceError(
                "persisted target integration receipt is missing or malformed"
            )
        if "index_flags" in raw:
            verify.validate_index_flags_evidence(raw["index_flags"])
        if "ignored" in raw:
            verify.validate_ignored_entries_evidence(raw["ignored"])
        verify.validate_integration_state_schema(
            self.run_dir,
            raw["snapshots"],
            raw["submodules"],
            operation,
            payload_max_bytes=self.policy.limits.artifact_payload_max_mb * 1_048_576,
        )
        return raw

    def _arm_integration_attempt(
        self,
        task: StoryTask,
        *,
        target_ref: str,
        strategy: str,
        source: str,
        snapshot_paths: Collection[str],
    ) -> dict[str, Any]:
        previous = task.integration_attempt
        verify.require_ref_reflog(self.paths.repo_root, target_ref)
        operation_identity = secrets.token_hex(16)
        pre_target_revision = verify.ref_revision(self.paths.repo_root, target_ref)
        snapshots, submodules = verify.capture_integration_state(
            self.paths.repo_root,
            self.run_dir,
            operation_identity,
            snapshot_paths,
            payload_max_bytes=self.policy.limits.artifact_payload_max_mb * 1_048_576,
        )
        # The flag words of the index OUTSIDE the snapshot set — a hook's
        # `update-index --assume-unchanged` on a clean tracked file there is
        # invisible to every other reading (#796 review); captured after the
        # snapshots, inside the same ref-revision bracket
        # and the whole tree's ignored entries — a hook's gitignored write
        # beside an incoming path in a directory the target already held
        # populated is listed by no other reading (#796 review)
        try:
            index_flags = verify.capture_index_flags(
                self.paths.repo_root, exclude=[str(entry["path"]) for entry in snapshots]
            )
            ignored = verify.capture_ignored_entries(
                self.paths.repo_root, self.run_dir, operation_identity
            )
        except BaseException:
            verify.discard_integration_state(
                self.run_dir, {"operation_identity": operation_identity}
            )
            raise
        if verify.ref_revision(self.paths.repo_root, target_ref) != pre_target_revision:
            verify.discard_integration_state(
                self.run_dir, {"operation_identity": operation_identity}
            )
            raise verify.IntegrationEvidenceError(
                "target changed during integration snapshot capture"
            )
        attempt = {
            "version": 3,
            "target_ref": target_ref,
            "strategy": strategy,
            "source_revision": source,
            "operation_identity": operation_identity,
            "pre_target_revision": pre_target_revision,
            "snapshots": snapshots,
            "submodules": submodules,
            "index_flags": index_flags,
            "ignored": ignored,
            "phase": "armed",
            "cleanup_plan": None,
        }
        task.integration_attempt = attempt
        try:
            self._save()
        except BaseException:
            task.integration_attempt = previous
            verify.discard_integration_state(
                self.run_dir, {"operation_identity": operation_identity}
            )
            raise
        verify.discard_integration_state(self.run_dir, previous)
        return attempt

    def _refuse_refused_residue(self, attempt: dict[str, Any], *, revision: str) -> None:
        """Refuse a re-arm while the refused attempt's named residue stands.

        The refusal restored the receipt's own paths and left a hook's write
        outside them in place, named; `integration_restoration_complete` reads
        only the former, so a resume after the hook was disabled — but before
        the output it left was cleared — re-armed over the write and the retry
        recorded ``unit-merged`` with it still there (#796 review). The refused
        receipt's readings are taken again (`verify.refused_integration_residue`)
        and keep their authority until nothing is named.
        """
        residue = verify.refused_integration_residue(
            self.paths.repo_root, self.run_dir, attempt, revision=revision
        )
        if residue:
            raise verify.IntegrationEvidenceError(
                "the refused integration's residue is still in the target — the paths "
                "its pause named as left in place, or work of yours since (the run "
                "cannot tell them apart); clear it, or commit or stash what is yours, "
                "then resume: " + ", ".join(residue)
            )

    def _integration_artifact_paths(self, task: StoryTask) -> tuple[str, ...]:
        """The accepted artifact paths on the target, for the receipt's snapshot and
        its restore. Raises rather than degrading: an ignored deliverable's
        destination is never in the merge's own delta, so this entry is the only
        thing that lets a target hook's write to it be seen and restored. A
        resolver refusal (the target's artifact dir swapped for a symlink after
        acceptance, or replayed authority the path validator rejects) therefore
        has to pause BEFORE the receipt is armed and anything on the target moves
        — `merge_local` resolves once, ahead of the snapshot, and threads the
        result to the restore — never snapshot short and let the later
        `validate_integrated` refusal claim an exact restore over it (#796 review)."""
        if not task.dw_ids:
            return ()
        return artifact_publication.integrated_artifact_repo_paths(task, self.paths)

    def _retire_unmoved_integration_attempt(self, task: StoryTask) -> None:
        """Restore receipt cleanup, then retire a proven no-ref typed refusal."""
        if not task.dw_ids or task.integration_attempt is None:
            return
        try:
            attempt = self._validated_integration_attempt(task)
            assert attempt is not None
            update = verify.integration_ref_update(
                self.paths.repo_root,
                str(attempt["target_ref"]),
                str(attempt["operation_identity"]),
            )
            pre = str(attempt["pre_target_revision"])
            if (
                update is not None
                or verify.ref_revision(self.paths.repo_root, str(attempt["target_ref"])) != pre
            ):
                raise verify.IntegrationEvidenceError(
                    "typed Git refusal did not prove the target ref remained unchanged"
                )
            verify.restore_integration_nonref_state(
                self.paths.repo_root,
                str(attempt["target_ref"]),
                revision=pre,
                run_dir=self.run_dir,
                snapshots=attempt["snapshots"],
                submodules=attempt["submodules"],
                operation_identity=str(attempt["operation_identity"]),
            )
        except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
            # Preserve the caller's typed merge-failure classification and remedy;
            # this companion record explains why receipt authority was retained.
            self.journal.append(
                "artifact-publication-refused",
                story_key=task.story_key,
                error=f"typed Git refusal target restoration failed: {exc}",
            )
            self._save()
            return
        retired = task.integration_attempt
        task.integration_attempt = None
        self._save()
        verify.discard_integration_state(self.run_dir, retired)

    def _pause_integration_evidence(
        self, task: StoryTask, exc: BaseException, *, prefix: str
    ) -> NoReturn:
        self.journal.append(
            "artifact-publication-refused", story_key=task.story_key, error=str(exc)
        )
        self._save()
        self._pause(
            f"{prefix}: {exc}; source retained at {task.worktree_path}",
            task.story_key,
            cause=exc,
        )

    def _refuse_integrated_artifacts(
        self,
        task: StoryTask,
        attempt: dict[str, str],
        update: verify.IntegrationRefUpdate | None,
        exc: BaseException,
        *,
        artifact_paths: tuple[str, ...],
    ) -> NoReturn:
        try:
            if update is not None:
                if update.old_revision != attempt["pre_target_revision"]:
                    raise verify.IntegrationRestoreError(
                        "integration ref epoch differs from the snapshotted target; "
                        "no non-ref restoration was attempted"
                    )
                verify.restore_integration_ref(
                    self.paths.repo_root,
                    attempt["target_ref"],
                    old_revision=update.old_revision,
                    new_revision=update.new_revision,
                    extra_paths=artifact_paths,
                    run_dir=self.run_dir,
                    snapshots=attempt["snapshots"],
                    submodules=attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                )
            else:
                pre = attempt["pre_target_revision"]
                if verify.ref_revision(self.paths.repo_root, attempt["target_ref"]) != pre:
                    raise verify.IntegrationRestoreError(
                        "target changed without readable integration ownership evidence; "
                        "no restoration was attempted"
                    )
                verify.restore_integration_nonref_state(
                    self.paths.repo_root,
                    attempt["target_ref"],
                    revision=pre,
                    run_dir=self.run_dir,
                    snapshots=attempt["snapshots"],
                    submodules=attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                )
                if not verify.integration_restoration_complete(
                    self.paths.repo_root,
                    attempt["target_ref"],
                    old_revision=pre,
                    new_revision=pre,
                    extra_paths=artifact_paths,
                    run_dir=self.run_dir,
                    snapshots=attempt["snapshots"],
                    submodules=attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                ):
                    raise verify.IntegrationRestoreError(
                        "target non-ref state changed without a ref update; no restoration "
                        "authority was inferred"
                    )
        except (verify.GitError, OSError, RuntimeError, ValueError) as restore_exc:
            self.journal.append(
                "artifact-publication-refused",
                story_key=task.story_key,
                error=f"{exc}; target restoration failed: {restore_exc}",
            )
            self._save()
            self._pause(
                "artifact target integration validation failed and the target could not "
                f"be restored safely: {restore_exc}; source retained at {task.worktree_path}",
                task.story_key,
                cause=restore_exc,
            )
        if update is None:
            # No ref update to own (an artifact-only bundle's squash stages
            # nothing; a fast-forward of a source the target already holds):
            # the transition is pre -> pre, and the receipt says so. An outcome
            # without its revisions is the one shape `_validated_integration_attempt`
            # refuses, and it used to be written here — every resume then read
            # "receipt is missing or malformed" with no re-arm (#796 review).
            attempt["old_revision"] = attempt["pre_target_revision"]
            attempt["new_revision"] = attempt["pre_target_revision"]
        attempt["outcome"] = "refused-restored"
        task.integration_attempt = attempt
        self.journal.append(
            "artifact-publication-refused", story_key=task.story_key, error=str(exc)
        )
        self._save()
        self._pause(
            "artifact target integration validation failed and the exact pre-attempt "
            f"target was restored: {exc}; source retained at {task.worktree_path}",
            task.story_key,
            cause=exc,
        )

    def merge_local(
        self,
        task: StoryTask,
        unit: UnitWorkspace,
        *,
        replay: bool = False,
        replay_strategy: str | None = None,
        first_integration: bool = False,
    ) -> None:
        """Merge a DONE unit's branch into the target branch from the main repo."""
        if first_integration:
            self._emit("pre_integrate", task)
        if task.dw_ids:
            self.prepare_publication(task, unit.workspace.paths)
        receipt_required = False
        if task.dw_ids:
            try:
                receipt_required = artifact_publication.requires_target_integration_receipt(task)
            except artifact_publication.PublicationError as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration evidence is unsafe"
                )
        if not replay or first_integration:
            self._emit("pre_merge", task)
        scm = self.policy.scm
        merge_strategy = scm.merge_strategy if replay_strategy is None else replay_strategy
        repo = self.paths.repo_root
        target = self.state.target_branch
        if task.dw_ids and not target:
            # Compatibility for persisted runs created before target_branch was
            # stamped into state: integration historically used the branch checked
            # out in the main repository.  Resolve and persist that same target
            # before constructing receipt authority.
            target = verify.current_branch(repo)
            if target == "HEAD":
                self._pause_integration_evidence(
                    task,
                    verify.IntegrationEvidenceError(
                        "target branch is unavailable for integration receipt"
                    ),
                    prefix="target integration evidence is unsafe",
                )
            self.state.target_branch = target
            self._save()
        source = task.commit_sha or verify.rev_parse_head(unit.path)
        # The completed task's recorded commit is the only source revision this
        # run accepted.  A pre_merge plugin (or another process) may advance the
        # unit branch after final verification, so never let the movable branch
        # name choose bytes for either the collision probe or the merge itself.
        merge_ref = source
        if replay:
            current_source = verify.rev_parse_head(unit.path)
            if current_source != source:
                reason = (
                    f"merge replay of {unit.branch} into {target} blocked: the unit "
                    f"branch advanced from recorded source {source} to {current_source}; "
                    "the later commits were not produced or verified by the completed "
                    "session and the branch was preserved for manual recovery"
                )
                self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
                return
            # ``merge_ref`` stays pinned to the write-ahead SHA even after this
            # diagnostic check; the branch can move again before Git runs.
        # A per_worktree Unity Editor can leak asset writes into the *main*
        # checkout (see the unity plugin's worktree setup), dirtying the target with the very
        # files this branch already committed. Reconcile that first: clean only
        # the leaked copies of incoming files; nothing outside this branch's path set is
        # ever touched. Outside it two questions decide whether the merge proceeds. What
        # the MERGE can commit is what git has staged (#618), so an unstaged stray is
        # inert and is tolerated and journaled while a staged one escalates. What the RUN
        # can commit is the second question, and `protected` is what asks it: the
        # post-merge carry stages the board and the ledger BY PATHSPEC, so any dirt on
        # them — staged or not, whoever wrote it — would ride the run's own bookkeeping
        # commit. Inert-under-merge and safe-to-proceed are not the same predicate.
        target_ref = f"refs/heads/{target}" if receipt_required else ""
        landed = False
        update: verify.IntegrationRefUpdate | None = None
        attempt: dict[str, Any] | None = None
        if receipt_required and task.integration_attempt is not None:
            try:
                attempt = self._validated_integration_attempt(task)
                assert attempt is not None
                if (
                    attempt["target_ref"] != target_ref
                    or attempt["strategy"] != merge_strategy
                    or attempt["source_revision"] != source
                ):
                    raise verify.IntegrationEvidenceError(
                        "persisted target integration receipt does not match the replay"
                    )
                update = verify.integration_ref_update(
                    repo, target_ref, attempt["operation_identity"]
                )
                if update is None and attempt["phase"] in {
                    "cleanup-pending",
                    "cleanup-applied",
                }:
                    pre = str(attempt["pre_target_revision"])
                    if verify.ref_revision(repo, target_ref) != pre:
                        raise verify.IntegrationEvidenceError(
                            "cleanup-phase receipt no longer owns the target epoch"
                        )
                    if not verify.integration_cleanup_state_recoverable(
                        repo,
                        self.run_dir,
                        attempt["snapshots"],
                        cleaned=attempt["cleanup_plan"]["cleaned"],
                        untracked=attempt["cleanup_plan"]["untracked"],
                        revision=pre,
                        operation_identity=str(attempt["operation_identity"]),
                    ):
                        raise verify.IntegrationEvidenceError(
                            "receipt-owned collision changed outside the cleanup transaction"
                        )
                    verify.restore_integration_nonref_state(
                        repo,
                        target_ref,
                        revision=pre,
                        run_dir=self.run_dir,
                        snapshots=attempt["snapshots"],
                        submodules=attempt["submodules"],
                        operation_identity=str(attempt["operation_identity"]),
                        include_paths=attempt["cleanup_plan"]["cleaned"],
                    )
                    retired = task.integration_attempt
                    task.integration_attempt = None
                    self._save()
                    verify.discard_integration_state(self.run_dir, retired)
                    attempt = None
                if update is not None:
                    assert attempt is not None
                    if update.old_revision != attempt["pre_target_revision"]:
                        raise verify.IntegrationEvidenceError(
                            "integration ref epoch differs from the snapshotted target"
                        )
                    persisted_old = attempt.get("old_revision")
                    persisted_new = attempt.get("new_revision")
                    if persisted_old is not None and (
                        persisted_old != update.old_revision or persisted_new != update.new_revision
                    ):
                        raise verify.IntegrationEvidenceError(
                            "persisted target transition disagrees with reflog evidence"
                        )
                    if persisted_old is None:
                        attempt["old_revision"] = update.old_revision
                        attempt["new_revision"] = update.new_revision
                        attempt["phase"] = "integrated"
                        task.integration_attempt = attempt
                        self._save()
                    landed = verify.ref_revision(repo, target_ref) == update.new_revision
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration evidence is unsafe"
                )
        tolerated: list[str] = []
        planned_target_revision = (
            verify.ref_revision(repo, target_ref)
            if attempt is not None and attempt.get("outcome") == "refused-restored"
            else (
                str(attempt["pre_target_revision"])
                if attempt is not None
                else verify.ref_revision(repo, target_ref) if receipt_required else target
            )
        )

        def note_tolerated(paths: list[str]) -> None:
            """Journal the guard's decision AND keep the paths for the arms below.

            The event stays here, before the merge, because it records what the
            GUARD decided; emitting it only on success would lose the trace in
            exactly the run worth debugging. But "tolerated" is a claim about the
            path SET, and a stray outside that set by path can still clash with it
            structurally — a file where the merge needs a directory, or the reverse
            — so git may refuse the merge over a path this event just called
            harmless. Holding the list lets the pre-flight arm correct the record
            instead of leaving it asserting the run merged past a path that in fact
            stopped it (#623).
            """
            tolerated.extend(paths)
            self.journal.append(
                "merge-target-tolerated",
                story_key=task.story_key,
                branch=unit.branch,
                paths=paths,
            )

        try:
            if landed:
                collision_plan = verify.IncomingCollisionPlan((), (), ())
                cleaned_without_receipt: list[str] | None = []
            elif receipt_required:
                collision_plan = verify.plan_incoming_collisions(
                    repo,
                    planned_target_revision,
                    merge_ref,
                    protected=self._carried_artifact_rels(repo),
                    on_tolerated=note_tolerated,
                )
                cleaned_without_receipt = None
            else:
                cleaned_without_receipt = verify.clean_incoming_collisions(
                    repo,
                    target,
                    merge_ref,
                    protected=self._carried_artifact_rels(repo),
                    on_tolerated=note_tolerated,
                )
                collision_plan = verify.IncomingCollisionPlan((), (), ())
        except (verify.GitError, OSError, RuntimeError) as e:
            # OSError/RuntimeError join GitError because clean_incoming_collisions
            # mutates the checkout directly (resolve/unlink/iterdir/rmdir) — non-spawn
            # FS faults the #343 chokepoint cannot translate. Crashing here would
            # strand a DONE unit mid-merge; keep-branch escalation is this boundary.
            if isinstance(e, (verify.GitSpawnError, OSError, RuntimeError)):
                # environment fault (spawn failure or direct-FS error) — there may
                # be no stray files at all, so no "clean them" guidance: the inner
                # error is the diagnosis.
                reason = (
                    f"merge of {unit.branch} into {target} blocked: could not "
                    f"reconcile the target checkout ({e}) — fix the underlying "
                    f"fault, then `bmad-loop resume {self.state.run_id}`"
                )
            else:
                # The outer sentence names the HAZARD, never the mechanism: since #618
                # there are two, and only the inner clause knows which applies to which
                # path. Saying "a merge or squash would fold them" out here attributed
                # every refusal to the merge, including one raised because the run's own
                # post-merge carry would sweep a path it commits for itself.
                reason = (
                    f"merge of {unit.branch} into {target} blocked: the target checkout has "
                    f"uncommitted changes to tracked files outside this branch that this run "
                    f"could commit under the story's name — the clause below names the paths, "
                    f"the mechanism, and what each one needs. Commit, stash or revert them, "
                    f"then `bmad-loop resume {self.state.run_id}`. One cause is a "
                    f"per_worktree engine Editor writing into the main checkout; another is "
                    f"ordinary local work. {e}"
                )
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return
        prospective_paths: tuple[str, ...] = ()
        artifact_paths: tuple[str, ...] = ()
        if receipt_required:
            try:
                prospective_paths = verify.preflight_integration_paths(
                    verify.branch_incoming_paths(repo, planned_target_revision, merge_ref)
                )
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration path preflight failed"
                )
            # Resolved once, here, and threaded to the restore: a refusal pauses
            # before the receipt is armed and before cleanup or git touch the
            # target (see `_integration_artifact_paths`).
            try:
                artifact_paths = self._integration_artifact_paths(task)
            except (
                artifact_publication.PublicationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration evidence is unsafe"
                )
        snapshot_paths = tuple(
            dict.fromkeys(
                [
                    *prospective_paths,
                    *collision_plan.cleaned,
                    *collision_plan.tolerated,
                    *artifact_paths,
                ]
            )
        )
        if receipt_required:
            try:
                attempt = self._validated_integration_attempt(task)
                if attempt is not None:
                    if (
                        attempt["target_ref"] != target_ref
                        or attempt["strategy"] != merge_strategy
                        or attempt["source_revision"] != source
                    ):
                        raise verify.IntegrationEvidenceError(
                            "persisted target integration receipt does not match the replay"
                        )
                    update = verify.integration_ref_update(
                        repo, target_ref, attempt["operation_identity"]
                    )
                    current = verify.ref_revision(repo, target_ref)
                    if update is not None:
                        if update.old_revision != attempt["pre_target_revision"]:
                            raise verify.IntegrationEvidenceError(
                                "integration ref epoch differs from the snapshotted target"
                            )
                        persisted_old = attempt.get("old_revision")
                        persisted_new = attempt.get("new_revision")
                        if persisted_old is not None and (
                            persisted_old != update.old_revision
                            or persisted_new != update.new_revision
                        ):
                            raise verify.IntegrationEvidenceError(
                                "persisted target transition disagrees with reflog evidence"
                            )
                        if persisted_old is None:
                            attempt["old_revision"] = update.old_revision
                            attempt["new_revision"] = update.new_revision
                            attempt["phase"] = "integrated"
                            task.integration_attempt = attempt
                            self._save()
                        if current == update.new_revision:
                            landed = True
                        elif current == update.old_revision:
                            restored = verify.integration_restoration_complete(
                                repo,
                                target_ref,
                                old_revision=update.old_revision,
                                new_revision=update.new_revision,
                                extra_paths=artifact_paths,
                                run_dir=self.run_dir,
                                snapshots=attempt["snapshots"],
                                submodules=attempt["submodules"],
                                operation_identity=attempt["operation_identity"],
                            )
                            if restored:
                                # Complete for the receipt's own paths; what
                                # the refusal named and left in place is
                                # outside them, and re-arming over it made
                                # it the retry's baseline (#796 review).
                                self._refuse_refused_residue(attempt, revision=current)
                                attempt = self._arm_integration_attempt(
                                    task,
                                    target_ref=target_ref,
                                    strategy=merge_strategy,
                                    source=source,
                                    snapshot_paths=snapshot_paths,
                                )
                                update = None
                            else:
                                raise verify.IntegrationEvidenceError(
                                    "restored target receipt state is incomplete"
                                )
                        elif attempt.get("outcome") == "refused-restored":
                            # The durable outcome is written only after guarded
                            # restoration succeeded. A later target commit is an
                            # operator/concurrent advance, not the refused result;
                            # preserve it and make it the next attempt's baseline
                            # — the refused residue, still outside every set the
                            # commit could have taken up, is not (#796 review).
                            self._refuse_refused_residue(attempt, revision=current)
                            attempt = self._arm_integration_attempt(
                                task,
                                target_ref=target_ref,
                                strategy=merge_strategy,
                                source=source,
                                snapshot_paths=snapshot_paths,
                            )
                            update = None
                        else:
                            raise verify.IntegrationEvidenceError(
                                "target no longer matches the receipt-owned integration result"
                            )
                    elif current != attempt["pre_target_revision"]:
                        raise verify.IntegrationEvidenceError(
                            "target changed without the receipt-owned ref-update evidence"
                        )
                    else:
                        existing_snapshot_paths = [
                            str(entry["path"])
                            for entry in attempt["snapshots"]
                            if isinstance(entry, dict) and "path" in entry
                        ]
                        covered_cleanup = set(collision_plan.cleaned) & set(existing_snapshot_paths)
                        if covered_cleanup and not verify.integration_nonref_state_unchanged(
                            repo,
                            self.run_dir,
                            attempt["snapshots"],
                            attempt["submodules"],
                            exclude_paths=set(existing_snapshot_paths) - covered_cleanup,
                            operation_identity=attempt["operation_identity"],
                        ):
                            raise verify.IntegrationEvidenceError(
                                "receipt-owned collision state changed before cleanup"
                            )
                        missing_coverage = set(snapshot_paths) - set(existing_snapshot_paths)
                        if missing_coverage:
                            if not verify.integration_nonref_state_unchanged(
                                repo,
                                self.run_dir,
                                attempt["snapshots"],
                                attempt["submodules"],
                                operation_identity=attempt["operation_identity"],
                            ):
                                raise verify.IntegrationEvidenceError(
                                    "persisted target receipt changed before cleanup coverage "
                                    "could be extended"
                                )
                            attempt = self._arm_integration_attempt(
                                task,
                                target_ref=target_ref,
                                strategy=merge_strategy,
                                source=source,
                                snapshot_paths=tuple(
                                    dict.fromkeys([*existing_snapshot_paths, *snapshot_paths])
                                ),
                            )
                else:
                    attempt = self._arm_integration_attempt(
                        task,
                        target_ref=target_ref,
                        strategy=merge_strategy,
                        source=source,
                        snapshot_paths=snapshot_paths,
                    )
                if attempt["pre_target_revision"] != planned_target_revision:
                    raise verify.IntegrationEvidenceError(
                        "target changed while collision planning was being captured; "
                        "integration was not attempted"
                    )
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration evidence is unsafe"
                )
        if receipt_required and not landed:
            assert attempt is not None
            plan_payload = {
                "cleaned": list(collision_plan.cleaned),
                "tolerated": list(collision_plan.tolerated),
                "untracked": list(collision_plan.untracked),
            }
            try:
                if attempt["phase"] != "armed":
                    raise verify.IntegrationEvidenceError(
                        "target cleanup receipt phase is not re-armable"
                    )
                if not verify.integration_nonref_state_unchanged(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                ):
                    raise verify.IntegrationEvidenceError(
                        "target changed after collision planning and before cleanup"
                    )
                attempt["cleanup_plan"] = plan_payload
                attempt["phase"] = "cleanup-pending"
                task.integration_attempt = attempt
                self._save()
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target collision cleanup could not be armed"
                )
        # What the cleanup has TOUCHED, for the restore in the except arm: the
        # paths it finished plus the one in flight when it failed. Never the
        # whole plan — a path still ahead may hold fresh operator state by then,
        # and the snapshot would flatten it (#796 review). The identity error's
        # own `cleaned` is this inventory minus the in-flight path, which that
        # error proved untouched. Bound before the `try` so a probe fault ahead
        # of the cleanup restores nothing rather than naming an unbound list.
        progress: list[str] = []
        try:
            if receipt_required and not landed:
                assert attempt is not None
                if not verify.integration_nonref_state_unchanged(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                ):
                    self._pause_integration_evidence(
                        task,
                        verify.IntegrationEvidenceError(
                            "target collision identity changed immediately before cleanup"
                        ),
                        prefix="target collision cleanup evidence is unsafe",
                    )

            def cleanup_identity_unchanged(path: str) -> bool:
                if attempt is None:
                    return True
                return verify.integration_nonref_state_unchanged(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    attempt["submodules"],
                    exclude_paths={
                        str(entry["path"])
                        for entry in attempt["snapshots"]
                        if str(entry["path"]) != path
                    },
                    operation_identity=attempt["operation_identity"],
                )

            cleaned = (
                verify.apply_incoming_collision_plan(
                    repo,
                    collision_plan,
                    before_mutate=cleanup_identity_unchanged,
                    progress=progress,
                )
                if cleaned_without_receipt is None
                else cleaned_without_receipt
            )
        except (verify.GitError, OSError, RuntimeError) as e:
            if receipt_required and attempt is not None:
                try:
                    verify.restore_integration_nonref_state(
                        repo,
                        target_ref,
                        revision=str(attempt["pre_target_revision"]),
                        run_dir=self.run_dir,
                        snapshots=attempt["snapshots"],
                        submodules=attempt["submodules"],
                        operation_identity=str(attempt["operation_identity"]),
                        include_paths=(
                            e.cleaned
                            if isinstance(e, verify.IntegrationCleanupChangedError)
                            else tuple(progress)
                        ),
                    )
                except (verify.GitError, OSError, RuntimeError, ValueError) as restore_exc:
                    e = verify.IntegrationRestoreError(
                        f"{e}; partial collision cleanup restoration failed: {restore_exc}"
                    )
            self._pause_integration_evidence(
                task, e, prefix="target collision cleanup failed after receipt capture"
            )
        if receipt_required and not landed:
            assert attempt is not None
            attempt["phase"] = "cleanup-applied"
            task.integration_attempt = attempt
            self._save()
        if cleaned:
            self.journal.append(
                "merge-target-cleaned",
                story_key=task.story_key,
                branch=unit.branch,
                paths=cleaned,
            )
        if receipt_required and not landed:
            assert attempt is not None
            # state.json is the durable authority; this record is a diagnostic
            # mirror only and may be the torn final journal line after a crash.
            self.journal.append(
                "unit-merge-started",
                story_key=task.story_key,
                branch=unit.branch,
                target=target,
                strategy=merge_strategy,
                source=source,
                operation_id=attempt["operation_identity"],
                pre_target_revision=attempt["pre_target_revision"],
            )
        elif not receipt_required and (not replay or first_integration):
            # Released ordinary-story behavior: journal intent and replay the
            # exact strategy idempotently without target-reflog dependency.
            self.journal.append(
                "unit-merge-started",
                story_key=task.story_key,
                branch=unit.branch,
                target=target,
                strategy=merge_strategy,
                source=source,
            )
        if receipt_required and not landed:
            assert attempt is not None
            try:
                if verify.ref_revision(repo, target_ref) != attempt["pre_target_revision"]:
                    raise verify.IntegrationEvidenceError(
                        "target changed after integration snapshot capture"
                    )
                if not verify.integration_nonref_state_unchanged(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    attempt["submodules"],
                    exclude_paths=collision_plan.cleaned,
                    operation_identity=attempt["operation_identity"],
                ):
                    raise verify.IntegrationEvidenceError(
                        "target non-ref state changed after integration snapshot capture"
                    )
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._pause_integration_evidence(
                    task, exc, prefix="target integration evidence is unsafe"
                )
        # The tree the squash leg staged before its own commit sealed it; the
        # receipt block below proves the sealed commit against it. None on the
        # other legs and on a replay that found the result already landed.
        squash_staged_tree: str | None = None
        try:
            if not landed:
                squash_staged_tree = verify.merge_branch(
                    repo,
                    merge_ref,
                    strategy=merge_strategy,
                    message=self.merge_message(task),
                    allow_empty_squash=(
                        replay or (bool(task.dw_ids) and source == task.baseline_commit)
                    ),
                    reflog_action=(
                        f"bmad-loop-integrate:{attempt['operation_identity']}"
                        if attempt is not None
                        else None
                    ),
                )
        except verify.MergePreflightError as e:
            # Subclass arm, so it must precede the GitError one below. git declined
            # before the merge began: nothing was merged and the target checkout is
            # exactly as it was. Describe that STATE rather than prescribing one
            # remedy — the same refusal covers an untracked file the merge would
            # overwrite, a staged change on an incoming path, a file/directory shape
            # clash, and a target that cannot fast-forward — and let the appended raw
            # git error name the cause and the paths (#619).
            reason = (
                f"merge of {unit.branch} into {target} was refused by git before it "
                f"started: nothing was merged, the target checkout is unchanged, and "
                f"there is no conflict to resolve. The target's state clashes with the "
                f"incoming commit; git's own message below names the cause and the "
                f"paths. Clear that clash, then `bmad-loop resume {self.state.run_id}`. "
                f"{e}"
            )
            if tolerated:
                # Corrective, not duplicative: only this arm knows the merge died at
                # git's pre-flight, and only the callback above knows which paths the
                # guard waved through. One of them may be the cause — git's text names
                # it — so pair the two rather than making either infer the other. Rides
                # phase 1's typed error: one discriminator, two consumers (#623).
                self.journal.append(
                    "merge-preflight-refused",
                    story_key=task.story_key,
                    branch=unit.branch,
                    tolerated=tolerated,
                    error=str(e),
                )
            self._retire_unmoved_integration_attempt(task)
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        except verify.MergeHalfAppliedError as e:
            # Sibling of the pre-flight arm above, not a subclass of it, so it is a
            # separate arm rather than a branch inside one — and like its neighbours it
            # must precede the bare `GitError` arm. git died PART-WAY through the
            # checkout: the pre-flight sentence above is false in its load-bearing
            # clause ("the target checkout is unchanged"), because the incoming files
            # git had already written are still there, untracked. Naming them is the
            # whole point — they are what makes the next attempt fail, as an
            # untracked-overwrite refusal over paths no earlier message mentioned.
            #
            # No `merge-preflight-refused` companion here even when `tolerated` is set:
            # that event is the #623 corrective for a guard that called a path harmless
            # and then watched git refuse over that same path. This failure is not about
            # the tolerated paths at all — it is incoming content failing to check out —
            # so pairing it with the guard's decision would assert a link that is not there.
            # Two residue axes, two different asks, and the operator needs whichever
            # ones actually apply — so the middle of this message is composed rather
            # than picked from a fixed set. An incoming path the target did not track
            # lands untracked and no restore reaches it, so it is theirs to clear; one
            # it DID track was modified in place and `merge_branch` has already
            # restored it path-scoped, unless that restore failed too.
            # The restore clause LEADS when both apply: it says "before anything
            # else" and means it — a resume dies on the tracked residue first —
            # so the untracked clause defers to it ("then") rather than both
            # claiming first place in one message.
            # The prescription is path-scoped for the reason the restore itself is:
            # only the named paths are proven git's, and a repo-wide
            # `git reset --hard HEAD` would flatten the operator's own uncommitted
            # work alongside them — the destruction the per-path attribution exists
            # to prevent.
            steps: list[str] = []
            if not e.restored:
                rewritten = (
                    " ".join(e.rewritten) if e.rewritten else "<the paths git's message names>"
                )
                steps.append(
                    f"The tracked files git had already rewritten could NOT be rolled "
                    f"back — that failure is in the message below too — so {target} is "
                    f"still holding incoming content on those paths. Restore exactly "
                    f"those paths (`git checkout HEAD -- {rewritten}` in {target}, "
                    f"never a repo-wide `git reset --hard`, which would flatten your "
                    f"own uncommitted work too) before anything else."
                )
            if e.paths:
                steps.append(
                    f"Some incoming files are left UNTRACKED in the checkout and no "
                    f"restore removes them — not `git merge --abort`, not "
                    f"`git reset --hard`: {', '.join(e.paths)}. "
                    + ("Clear those first, " if e.restored else "Then clear those, ")
                    + "checking the contents before you delete: the run can prove git "
                    "wrote each path, not that the bytes now there are git's."
                )
            if e.restored and not e.paths:
                steps.append(
                    "The tracked files git had already rewritten have been rolled "
                    "back, so the checkout itself needs nothing from you."
                )
            reason = (
                f"merge of {unit.branch} into {target} failed PART-WAY THROUGH: git got "
                f"far enough to start writing the incoming files into {target}'s "
                f"checkout before it stopped, so nothing was committed and this is not a "
                f"clash between the target and the incoming commit. "
                + " ".join(steps)
                + f" Whatever residue is left refuses the NEXT attempt over those same "
                f"paths rather than with the error below, which is why a resume before "
                f"clearing it fails on the tree instead of on the cause. Then fix what "
                f"stopped the checkout — git's own message below names it, and a "
                f"required clean/smudge filter that cannot run is the measured cause — "
                f"and `bmad-loop resume {self.state.run_id}`. {e}"
            )
            self._retire_unmoved_integration_attempt(task)
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        except verify.MergeResidueUnreadError as e:
            # The terminal arm's caller-side half, and another sibling that must
            # precede the bare `GitError` arm. Neither neighbour's sentence can be
            # borrowed: the pre-flight arm's "the target checkout is unchanged" is
            # exactly the claim the dead probe can no longer back, and the
            # half-applied arm names residue this run never read. Say what IS known
            # — the merge failed, git's text below names why — and send the operator
            # to the one reading the run could not take, which their own `git
            # status` still can.
            reason = (
                f"merge of {unit.branch} into {target} failed, AND the after-the-fact "
                f"probe that verifies the checkout failed too, so whether {target}'s "
                f"checkout still holds incoming residue is UNVERIFIED. Run `git "
                f"status` in {target}: clear any residue it names that is not yours "
                f"(files from {unit.branch} left untracked or rewritten), fix what "
                f"stopped the merge — git's message below names it — then "
                f"`bmad-loop resume {self.state.run_id}`. {e}"
            )
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        except verify.MergeCommitRefusedError as e:
            # Sibling of the arm above and equally a GitError subclass, so it too must
            # precede the last arm. Neither neighbour's remedy applies here: the merge
            # RAN and resolved, so there is no target-state clash to clear, and it
            # resolved cleanly, so there is no conflict to resolve either. `merge_branch`
            # rolled it back — `merge --abort` on the `--no-ff` leg, `reset --hard` on
            # the squash leg's own commit — so the checkout is back where it started and
            # the operator is sent to the policy that declined the commit (#619). The
            # unrestored wording branches on WHERE the checkout stands, because the two
            # legs strand differently and the first step differs with them.
            if e.restored:
                reason = (
                    f"merge of {unit.branch} into {target} resolved cleanly, but git "
                    f"refused to COMMIT it, so the merge was rolled back and the target "
                    f"checkout is back as it was. There is no conflict to resolve and "
                    f"nothing to clear from the tree: a `pre-merge-commit` or "
                    f"`commit-msg` hook, or commit signing, declined it — git's own "
                    f"message below names which. Fix that, then "
                    f"`bmad-loop resume {self.state.run_id}`. {e}"
                )
            elif e.staged:
                # The squash leg's strand: no MERGE_HEAD exists, so "recover the
                # merge" would be fiction — the squash result is sitting STAGED,
                # either because the rollback failed or because the checkout also
                # carries the operator's own uncommitted work, which the rollback
                # refuses to flatten. The exception's text says which.
                reason = (
                    f"merge of {unit.branch} into {target} resolved cleanly, git "
                    f"refused to COMMIT it, and the squash result is left STAGED in "
                    f"{target}'s checkout — the message below says why it was not "
                    f"rolled back for you. Stash or commit anything in {target} that "
                    f"is yours, then clear the staged result "
                    f"(`git reset --hard HEAD` in {target}). Only then fix whatever "
                    f"declined the commit: a `pre-merge-commit` or `commit-msg` hook, "
                    f"or commit signing. Then `bmad-loop resume {self.state.run_id}`. "
                    f"{e}"
                )
            else:
                # The abort failed too, so the restored sentence would be a lie about
                # the one thing the operator has to act on FIRST: a resume attempted
                # over a mid-merge checkout dies on the merge state, not on the
                # policy, and keeps doing so however well they fix the hook.
                reason = (
                    f"merge of {unit.branch} into {target} resolved cleanly, git refused "
                    f"to COMMIT it, and the abort meant to undo that failed as well — so "
                    f"the target checkout is left MID-MERGE and has to be recovered "
                    f"first (`git merge --abort`, or reset it to the pre-merge commit). "
                    f"Only then fix whatever declined the commit: a `pre-merge-commit` "
                    f"or `commit-msg` hook, or commit signing. git's own message below "
                    f"names both failures. Then `bmad-loop resume {self.state.run_id}`. "
                    f"{e}"
                )
            self._retire_unmoved_integration_attempt(task)
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        except verify.MergeConflictError as e:
            # genuine content conflict against the target, measured (unmerged index
            # stages): keep the branch for manual merge. The unit committed cleanly
            # (phase is already DONE, which has no legal transition), so escalate
            # directly.
            reason = (
                f"merge of {unit.branch} into {target} failed "
                f"(content conflict against the target): resolve it by hand, then "
                f"`bmad-loop resume {self.state.run_id}`. {e}"
            )
            self._retire_unmoved_integration_attempt(task)
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        except verify.GitError as e:
            # Every state the classification MEASURED has a typed arm above, so a
            # bare GitError is a state nothing measured. This arm used to claim the
            # most specific diagnosis — "content conflict, resolve it by hand" — for
            # exactly the failures it knew least about, which is how six mislabeled
            # git states in a row reached the operator wearing a fictional remedy
            # (#619). It now claims only what it knows: the merge failed, the run
            # cannot say what state the checkout is in, and git's text names the
            # cause. An unforeseen shape lands here as a vague-but-true message
            # rather than a precise fiction.
            reason = (
                f"merge of {unit.branch} into {target} failed, and the failure was "
                f"not classified: the run cannot say what state {target}'s checkout "
                f"is in. Run `git status` in {target} and put the checkout right — "
                f"git's own message below names the cause — then "
                f"`bmad-loop resume {self.state.run_id}`. {e}"
            )
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        if receipt_required:
            assert attempt is not None
            try:
                if update is None:
                    update = verify.integration_ref_update(
                        repo, target_ref, attempt["operation_identity"]
                    )
                if update is None:
                    current = verify.ref_revision(repo, target_ref)
                    if current != attempt["pre_target_revision"]:
                        raise verify.IntegrationEvidenceError(
                            "integration changed the target without readable ref-update evidence"
                        )
                    expected_revision = current
                else:
                    if update.old_revision != attempt["pre_target_revision"]:
                        raise verify.IntegrationEvidenceError(
                            "integration ref epoch differs from the snapshotted target"
                        )
                    attempt["old_revision"] = update.old_revision
                    attempt["new_revision"] = update.new_revision
                    attempt["phase"] = "integrated"
                    task.integration_attempt = attempt
                    self._save()
                    current = verify.ref_revision(repo, target_ref)
                    if current != update.new_revision:
                        raise verify.IntegrationEvidenceError(
                            "target moved after the receipt-owned integration"
                        )
                    expected_revision = update.new_revision
                artifact_publication.validate_integrated(task, self.paths, expected_revision)
                # Three readings, one per place a TARGET hook can put its output.
                # Outside the incoming set the snapshot is the authority (below);
                # on it the integrated commit is — the merge changed those paths
                # by design — so the post-hook index and checkout must hold each
                # one as that commit has it; and the squash leg's commit, which
                # re-reads the index after `pre-commit`, must have sealed the tree
                # git resolved, since a rewrite landed THERE matches index and
                # checkout alike (#796 review). Path-only evidence throughout.
                if squash_staged_tree is not None:
                    if verify.revision_tree_oid(repo, expected_revision) != squash_staged_tree:
                        raise verify.IntegrationEvidenceError(
                            "target commit hook changed the squash result after the merge "
                            "resolved it"
                        )
                # The submodule reading goes first: a populated checkout git
                # left behind when the commit deleted its gitlink is git's, not
                # a hook's, and only that reading can say so to the probe.
                retained_checkouts = verify.validate_integrated_submodule_state(
                    repo,
                    attempt["submodules"],
                    prospective_paths=prospective_paths,
                    revision=expected_revision,
                )
                drifted = verify.integrated_paths_drift(
                    repo,
                    expected_revision,
                    prospective_paths,
                    retained_checkouts=retained_checkouts,
                )
                if drifted:
                    raise verify.IntegrationEvidenceError(
                        "target hook changed incoming paths after integration: "
                        + ", ".join(sorted(drifted))
                    )
                # The diff readings compare blobs; an index flag a hook set on an
                # incoming path (`update-index --assume-unchanged`, which hides
                # later edits from git) changes none, so the post-hook flag word
                # of every incoming entry is read against what a fresh entry may
                # carry or what the receipt captured — and an entry git trusts
                # unread under an accepted word is read from disk against the
                # integrated commit, the diff reading above having trusted the
                # bit (#796 review).
                flagged = verify.integrated_index_flags_drift(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    prospective_paths,
                    revision=expected_revision,
                    operation_identity=attempt["operation_identity"],
                )
                if flagged:
                    raise verify.IntegrationEvidenceError(
                        "target hook changed index flags on incoming paths after "
                        "integration: " + ", ".join(flagged)
                    )
                if not verify.integration_nonref_state_unchanged(
                    repo,
                    self.run_dir,
                    attempt["snapshots"],
                    attempt["submodules"],
                    exclude_paths=prospective_paths,
                    operation_identity=attempt["operation_identity"],
                ):
                    raise verify.IntegrationEvidenceError(
                        "target hook changed receipt-owned index, worktree, ignored, "
                        "or submodule state"
                    )
                # The fourth place: a clean tracked file outside every set
                # above has no baseline in the receipt, so the whole-tree
                # reading closes it — after the hooks the target may hold
                # exactly the strays the guard tolerated before the merge,
                # plus an ignored file that was already there and that an
                # incoming `.gitignore` change uncovered (the receipt's
                # ignored listing holds it at its identity), and nothing
                # else (#796 review). The plan is read from the receipt,
                # not the local variable: a replay that finds the ref
                # already moved plans no collisions of its own.
                cleanup_plan = attempt.get("cleanup_plan") or {}
                strays = verify.integrated_stray_paths(
                    repo,
                    tolerated=cleanup_plan.get("tolerated", ()),
                    incoming=prospective_paths,
                    retained_checkouts=retained_checkouts,
                    run_dir=self.run_dir,
                    ignored=attempt.get("ignored"),
                )
                if strays:
                    raise verify.IntegrationEvidenceError(
                        "target hook changed paths outside the incoming set after "
                        "integration (staged changes restored; unstaged and untracked "
                        "entries left in place): " + ", ".join(strays)
                    )
                # What status cannot list: an index flag word a hook flipped on
                # a clean tracked file outside the incoming set, proved unchanged
                # by the receipt's digest and named from its map — and the file
                # behind an entry the index already trusted unread (assume-
                # unchanged, skip-worktree), which a hook can overwrite with no
                # word, blob, or status reading moving, named from the receipt's
                # `lstat` identity of it (#796 review). After the stray reading,
                # which owns an entry added or removed. Left in place by the
                # restore, like unstaged dirt, and named.
                if attempt.get("index_flags") is not None:
                    flipped = verify.integrated_index_flags_outside_drift(
                        repo,
                        attempt["index_flags"],
                        exclude=[str(entry["path"]) for entry in attempt["snapshots"]],
                    )
                    if flipped:
                        raise verify.IntegrationEvidenceError(
                            "target hook changed index flags, or files the index trusts "
                            "unread, outside the incoming set after integration (left in "
                            "place): " + ", ".join(flipped)
                        )
                # And the one place none of those list: a directory the commit
                # created where the receipt proved nothing was — or proved a
                # file, a symlink, or an unpopulated gitlink — walked on disk
                # — a hook's gitignored write or nested `.git` there is
                # attempt-era with everything else in it (#796 review).
                residue = verify.integrated_introduced_directories_drift(
                    repo,
                    expected_revision,
                    self.run_dir,
                    attempt["snapshots"],
                    submodules=attempt["submodules"],
                    operation_identity=attempt["operation_identity"],
                )
                if residue:
                    raise verify.IntegrationEvidenceError(
                        "target hook wrote into a directory the integration created: "
                        + ", ".join(residue)
                    )
                # And wherever else: an ignored entry the receipt's whole-tree
                # listing did not record, recorded under another identity, or
                # recorded and now gone — a hook's write beside an incoming
                # path in a directory the target already held populated, over
                # an ignored file that was already there, or its deletion of
                # one, which no reading above lists — and a nested `.git`
                # anywhere, which git lists in no reading at all, a checkout
                # the submodule reading accepted at a gitlink the commit
                # introduced excepted, and a write inside a nested repository
                # git tracks nothing under (the tolerated `vendor` among
                # them), which git never descends into; the incoming set is
                # the commit's own, an ignored entry git clobbered on its way
                # in included, and the leftover of a deleted gitlink is the
                # captured checkout's reading's below (#796 review). Left as
                # found by the restore, like unstaged dirt, and named.
                if attempt.get("ignored") is not None:
                    added = verify.integrated_ignored_additions(
                        repo,
                        self.run_dir,
                        attempt["ignored"],
                        tolerated=cleanup_plan.get("tolerated", ()),
                        incoming=prospective_paths,
                        introduced_checkouts=verify.integrated_introduced_gitlinks(
                            repo,
                            self.run_dir,
                            attempt["submodules"],
                            revision=expected_revision,
                        ),
                        retained_checkouts=retained_checkouts,
                    )
                    if added:
                        raise verify.IntegrationEvidenceError(
                            "target hook wrote, changed or removed ignored entries after "
                            "integration (left as found): " + ", ".join(added)
                        )
                # And inside a captured submodule checkout, which that listing
                # never descends into and whose own reading takes `status`
                # without `--ignored`: against the listing the receipt sealed
                # beside its HEAD (#796 review). Left as found the same way.
                added = verify.integrated_submodule_ignored_additions(
                    repo,
                    self.run_dir,
                    attempt["submodules"],
                    revision=expected_revision,
                )
                if added:
                    raise verify.IntegrationEvidenceError(
                        "target hook wrote, changed or removed ignored entries in a captured "
                        "submodule checkout after integration (left as found): " + ", ".join(added)
                    )
                if verify.ref_revision(repo, target_ref) != expected_revision:
                    raise verify.IntegrationEvidenceError(
                        "target moved during artifact integration validation"
                    )
            except artifact_publication.PublicationError as exc:
                self._refuse_integrated_artifacts(
                    task, attempt, update, exc, artifact_paths=artifact_paths
                )
            except (verify.GitError, OSError, RuntimeError, ValueError) as exc:
                self._refuse_integrated_artifacts(
                    task, attempt, update, exc, artifact_paths=artifact_paths
                )
            self.journal.append(
                "unit-merged",
                story_key=task.story_key,
                branch=unit.branch,
                target=self.state.target_branch,
                strategy=merge_strategy,
                source=source,
                operation_id=attempt["operation_identity"],
            )
            # The validated completion record is durable before rollback authority is
            # retired. A crash between these writes replays from unit-merged and may
            # safely clear the leftover receipt without touching Git.
            completed_attempt = task.integration_attempt
            task.integration_attempt = None
            self._save()
            verify.discard_integration_state(self.run_dir, completed_attempt)
        else:
            self.journal.append(
                "unit-merged",
                story_key=task.story_key,
                branch=unit.branch,
                target=self.state.target_branch,
                strategy=merge_strategy,
                source=source,
            )
        self._emit("post_merge", task)
        self.finish_publication(task, unit)

    def prepare_publication(self, task: StoryTask, source: ProjectPaths) -> None:
        """Persist accepted bytes before merge can consume the unit."""
        try:
            limits = self.policy.limits
            artifact_publication.prepare(
                task,
                self.paths,
                source,
                file_max_bytes=limits.artifact_file_max_mb * 1_048_576,
                payload_max_bytes=limits.artifact_payload_max_mb * 1_048_576,
            )
            self._save()
        except artifact_publication.PublicationSizeError as exc:
            self.journal.append(
                "artifact-publication-refused",
                story_key=task.story_key,
                error=str(exc),
                publication_cause=exc.cause,
                measured_bytes=exc.measured_bytes,
                limit_bytes=exc.limit_bytes,
                measurement_is_lower_bound=exc.measurement_is_lower_bound,
            )
            self._save()
            self._pause(
                f"artifact publication preparation failed: {exc}", task.story_key, cause=exc
            )
        except (artifact_publication.PublicationError, verify.GitError, OSError, ValueError) as exc:
            self.journal.append(
                "artifact-publication-refused", story_key=task.story_key, error=str(exc)
            )
            self._save()
            self._pause(
                f"artifact publication preparation failed: {exc}", task.story_key, cause=exc
            )

    def bind_publication(
        self, task: StoryTask, source: ProjectPaths, acceptance_identity: str
    ) -> None:
        """Persist final-verification source authority for one accepted result.

        Arm and save the append-only session identity before reading source
        bytes. If the process dies during the read, replay sees the same identity
        with no digest map and refuses rather than blessing whatever bytes are
        present after restart.
        """
        if task.artifact_acceptance_identity == acceptance_identity:
            if task.artifact_source_digests is None or task.artifact_tracked_source_oids is None:
                exc = artifact_publication.PublicationError(
                    "accepted artifact source binding is unavailable for replay"
                )
                self.journal.append(
                    "artifact-publication-refused", story_key=task.story_key, error=str(exc)
                )
                self._save()
                self._pause(
                    f"artifact publication binding failed: {exc}",
                    task.story_key,
                    cause=exc,
                )
            return
        try:
            artifact_publication.arm_binding(task, acceptance_identity)
            self._save()
            limits = self.policy.limits
            artifact_publication.bind_armed(
                task,
                source,
                file_max_bytes=limits.artifact_file_max_mb * 1_048_576,
                payload_max_bytes=limits.artifact_payload_max_mb * 1_048_576,
            )
            self._save()
        except artifact_publication.PublicationSizeError as exc:
            self.journal.append(
                "artifact-publication-refused",
                story_key=task.story_key,
                error=str(exc),
                publication_cause=exc.cause,
                measured_bytes=exc.measured_bytes,
                limit_bytes=exc.limit_bytes,
                measurement_is_lower_bound=exc.measurement_is_lower_bound,
            )
            self._save()
            self._pause(f"artifact publication binding failed: {exc}", task.story_key, cause=exc)
        except (artifact_publication.PublicationError, verify.GitError, OSError, ValueError) as exc:
            self.journal.append(
                "artifact-publication-refused", story_key=task.story_key, error=str(exc)
            )
            self._save()
            self._pause(f"artifact publication binding failed: {exc}", task.story_key, cause=exc)

    def validate_staged_publication(self, task: StoryTask, source: ProjectPaths) -> dict[str, str]:
        """Validate final staged Git deliverables or retain the unit mount."""
        try:
            return artifact_publication.validate_staged(task, source)
        except (artifact_publication.PublicationError, verify.GitError, OSError, ValueError) as exc:
            self.journal.append(
                "artifact-publication-refused", story_key=task.story_key, error=str(exc)
            )
            self._save()
            self._pause(
                f"artifact publication staging failed: {exc}",
                task.story_key,
                cause=exc,
            )

    def validate_committed_publication(
        self,
        task: StoryTask,
        source: ProjectPaths,
        revision: str,
        staged_snapshot: object,
    ) -> None:
        """Validate the committed tree or roll back while retaining the mount."""
        try:
            if not isinstance(staged_snapshot, dict) or not all(
                isinstance(rel, str) and isinstance(identity, str)
                for rel, identity in staged_snapshot.items()
            ):
                raise artifact_publication.PublicationError(
                    "validated staged artifact snapshot is missing or malformed"
                )
            artifact_publication.validate_committed(task, source, revision, staged_snapshot)
        except (artifact_publication.PublicationError, verify.GitError, OSError, ValueError) as exc:
            self.journal.append(
                "artifact-publication-refused", story_key=task.story_key, error=str(exc)
            )
            self._save()
            self._pause(
                f"artifact publication commit validation failed: {exc}",
                task.story_key,
                cause=exc,
            )

    def finish_publication(self, task: StoryTask, unit: UnitWorkspace | None) -> None:
        """Publish and latch before successful teardown, including merge replay."""
        if task.dw_ids:
            try:
                artifact_publication.publish(task, self.paths)
                self._save()
            except (
                artifact_publication.PublicationError,
                verify.GitError,
                OSError,
                ValueError,
            ) as exc:
                self.journal.append(
                    "artifact-publication-refused", story_key=task.story_key, error=str(exc)
                )
                self._save()
                self._pause(
                    f"artifact publication failed: {exc}; source retained at {task.worktree_path}",
                    task.story_key,
                    cause=exc,
                )
        if unit is None:
            return  # journal-proven merge can publish from durable bytes alone
        scm = self.policy.scm
        close_unit_workspace(
            unit,
            success=True,
            keep_failed=scm.keep_failed,
            run_dir=self.run_dir,
            unit_key=task.story_key,
            delete_branch=scm.delete_branch,
            on_teardown_degraded=lambda msg: self.journal.append(
                "worktree-teardown-degraded", story_key=task.story_key, error=msg
            ),
        )

    def keep_branch_and_escalate(self, task: StoryTask, unit: UnitWorkspace, reason: str) -> None:
        """Preserve a DONE unit's branch (no delete, kept for manual merge) and
        escalate. Shared by every merge-back failure path: a target dirtied with
        stray work, a merge git refused at pre-flight, a merge that died part-way
        through its checkout, a merge whose COMMIT git refused, a genuine content
        conflict, and a failure nothing classified."""
        close_unit_workspace(
            unit,
            success=False,
            keep_failed=True,
            run_dir=self.run_dir,
            unit_key=task.story_key,
            delete_branch=False,
            diff_max_file_bytes=self.failed_diff_max_bytes(),
        )
        self.escalate_unit(task, reason)  # always raises RunPaused

    def escalate_unit(self, task: StoryTask, reason: str) -> None:
        """Mark a unit ESCALATED, notify, and pause the run.

        Callers escalate outside a legal transition, before dispatch or after a
        completed merge attempt, so the phase is set directly rather than advanced.
        """
        task.phase = Phase.ESCALATED
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        self._pause(reason, task.story_key)

    def merge_message(self, task: StoryTask) -> str:
        return f"Merge {task.branch} into {self.state.target_branch} (bmad-loop)"

    def gc_run_worktrees(self) -> None:
        """Reclaim this run's worktree scaffolding once it finishes cleanly.

        DONE and AWAITING_OPERATOR units drop their worktree at merge time; this
        is a safety net for a worktree leaked by a crash between merge and
        teardown, plus it prunes stale git admin entries and removes the now-empty
        run worktree dir.
        Worktrees deliberately kept for inspection (a kept-failed/escalated unit)
        are left in place and journaled so the operator can find them."""
        if not self.isolated:
            return
        repo = self.paths.repo_root
        for task in self.state.tasks.values():
            if task.phase in (Phase.DONE, Phase.AWAITING_OPERATOR) and task.worktree_path:
                wt = Path(task.worktree_path)
                if wt.is_dir():
                    if task.dw_ids and not task.artifact_publication_complete:
                        self._pause(
                            f"artifact publication incomplete; source retained at {task.worktree_path}",
                            task.story_key,
                        )
                    discard_worktree(repo, task.worktree_path, task.branch, run_dir=self.run_dir)
            elif task.terminal and task.worktree_path and Path(task.worktree_path).is_dir():
                # kept on purpose (keep_failed): leave it, but surface where.
                self.journal.append(
                    "worktree-kept", story_key=task.story_key, path=task.worktree_path
                )
        verify.worktree_prune(repo)
        worktrees_parent = unit_worktrees_dir(self.run_dir)
        if worktrees_parent.is_dir() and not any(worktrees_parent.iterdir()):
            worktrees_parent.rmdir()

    def reopen_unit(self, task: StoryTask) -> UnitWorkspace:
        """Reconstruct the UnitWorkspace for an in-flight unit on resume, from
        the worktree path + branch persisted on the task. The worktree must still
        be mounted — if it was pruned out from under us we cannot safely reuse it,
        so escalate rather than run a session in a missing directory."""
        wt = Path(task.worktree_path)
        try:
            mounted = wt.is_dir() and verify.worktree_is_registered(self.paths.repo_root, wt)
        except verify.GitError as exc:
            self.escalate_unit(
                task,
                f"cannot verify recorded worktree for {task.story_key} ({wt}): {exc}",
            )
        if not mounted:
            self.escalate_unit(
                task,
                f"worktree for {task.story_key} is gone or unopenable ({wt}); "
                "cannot resume in place",
            )
        try:
            mounted_branch = verify.current_branch(wt)
        except verify.GitError as exc:
            self.escalate_unit(
                task,
                f"cannot verify recorded worktree branch for {task.story_key} ({wt}): {exc}",
            )
        if mounted_branch != task.branch:
            self.escalate_unit(
                task,
                f"worktree for {task.story_key} is on {mounted_branch!r}, not recorded "
                f"branch {task.branch!r}; cannot resume in place",
            )
        # Spec paths are persisted relative to the worktree (model.to_dict) so
        # state stays portable; re-absolutize both accepted/result ownership and
        # the current/last attempt's dispatch ownership against the reopened tree.
        # Absolute outside-worktree paths pass through unchanged. The rule itself
        # lives on the class that creates the relative spelling, so this and
        # `Engine._finish_inflight`'s pre-discard re-anchor cannot drift apart.
        task.rebase_spec_paths_on(wt)
        return UnitWorkspace(
            workspace=Workspace(root=wt, paths=self.paths.rebased(wt)),
            repo_root=self.paths.repo_root,
            branch=task.branch,
            path=wt,
            baseline=task.baseline_commit or "",
        )
