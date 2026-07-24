"""Per-unit worktree isolation + integration flow.

Extracted from :class:`bmad_loop.engine.Engine` (issue #244, findings F-3/F-9a):
``Engine`` was a god-class whose worktree/integration cluster is an independent
state machine. It lives here as a collaborator built from narrow dependencies
(repo paths, the policy, run state, journal, plugin registry, loaded adapters)
plus a handful of engine callbacks (emit a plugin hook, save state, run the
per-unit ready gate, escalate-pause, and get/set the engine's active workspace).
The collaborator never receives the whole ``Engine`` — it cannot reach engine
internals beyond those callables.

``Engine`` keeps same-name private methods that delegate here, so its tests and
the ``SweepEngine``/``StoriesEngine`` subclasses see an unchanged surface.

``provision_worktree`` (previously in ``install.py``) is rehomed here too so the
runtime control loop no longer imports the installer; ``install.py`` re-exports
it lazily for its own tests.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NoReturn

from . import gates, verify
from .install import (
    BASE_SKILLS,
    CUSTOMIZE_DIR,
    HOOK_SCRIPT_REL,
    MODULE_SKILLS,
    _copy_traversable,
    _worktree_local_exclude,
    merge_hooks,
    resolve_review_layers,
)
from .model import Phase
from .process_host import get_process_host
from .workspace import (
    UnitWorkspace,
    Workspace,
    close_unit_workspace,
    discard_worktree,
    unit_worktrees_dir,
)

if TYPE_CHECKING:
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
      path (the relay locates the run dir from $BMAD_LOOP_RUN_DIR, not its own
      location), so nothing is written into the worktree's .bmad-loop/;
    - everything we wrote is added to the worktree's local git exclude.
    Skill trees, the per-CLI hook config, and the seeded configs all live in dirs
    projects gitignore — but the exclude shields them even when a project doesn't.

    seed_files are copied BEFORE the hook step so a seeded settings file that is
    also a hook config_path (.claude/settings.json, .gemini/settings.json) keeps its
    real content and just gets the Stop hook merged in, rather than being created empty.

    Returns the `seed_files` entries that copied NOTHING because everything they
    name was already present — copy-when-absent turned them into no-ops. The caller
    journals them: a user-authored `worktree_seed` entry that silently copies
    nothing reads as applied configuration and is not. A directory entry that
    seeded even one child is not reported: it is applied configuration.
    """
    if not profiles and not seed_files and not seed_globs:
        return []
    worktree = worktree.resolve()
    repo_root = repo_root.resolve()
    relay = repo_root / HOOK_SCRIPT_REL
    skills_root = resources.files("bmad_loop.data").joinpath("skills")

    # project gitignored MCP/CLI configs: copy from the main repo when absent.
    # Resolve-and-contain guards against an `..`/absolute entry escaping either tree.
    seeded: list[str] = []
    # Entries that named a real source but copied nothing, because every path they
    # name already exists. Reported to the caller (this function is quiet by
    # contract — it runs under a TUI) because the no-op is otherwise silent: an
    # entry that reads as applied configuration is not. Per-CHILD skips inside a
    # directory entry are deliberately not reported — the checkout is expected to
    # carry its tracked children, so that is routine rather than a
    # misconfiguration, exactly like the glob-expanded matches below.
    skipped: list[str] = []
    for rel in seed_files:
        src = (repo_root / rel).resolve()
        dst = (worktree / rel).resolve()
        if not src.is_relative_to(repo_root) or not dst.is_relative_to(worktree):
            continue
        if not src.exists():
            continue
        if dst.exists():
            # File entries keep the classic copy-when-absent skip. A destination
            # that is not a directory while the source is (the checkout carries a
            # FILE where the seed names a dir) is a type mismatch: recursing would
            # try to mkdir over the file, so the entry is skipped whole instead.
            if not src.is_dir() or not dst.is_dir():
                skipped.append(str(rel))
                continue
            # A DIRECTORY whose destination exists is the case #230 reported: the
            # checkout carries some tracked child, so the whole entry used to be a
            # no-op — including the gitignored children that are absent and would
            # clobber nothing. Recurse instead, copying only what is missing.
            if not _copy_traversable(src, dst, skip_existing=True):
                # every child was already present: still a total no-op, still
                # reported. Only a PARTIAL seed stops being reported.
                skipped.append(str(rel))
                continue
            # Partially seeded, so the entry must still reach `patterns` below:
            # the children we just wrote have to stay out of the unit's
            # `git add -A`. Excluding the whole dir is safe — an exclude does not
            # untrack the tracked children that were already there.
            seeded.append(rel)
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
        # The orchestrator-driven upstream skills are not in the wheel; copy them
        # from the MAIN REPO's installed tree (same tree path) so an isolated
        # worktree can still resolve /bmad-dev-auto and the review layers. Skip
        # silently when the main repo lacks them — the run-start preflight reports
        # it.
        #
        # BASE_SKILLS is only the floor. The review layers this project actually
        # invokes are read from the installed bmad-dev-auto, exactly as the
        # preflight reads them, so a reviewer named by a project override (a
        # custom or renamed skill) is provisioned too. Validating a skill here and
        # then not copying it is how preflight passes in the main checkout while
        # the isolated review fails on a skill that was never there.
        resolved = resolve_review_layers(repo_root, tree)
        required = dict.fromkeys((*BASE_SKILLS, *(resolved.skills() if resolved else ())))
        for skill in required:
            dst = tree_dir / skill
            if dst.exists():
                continue
            src = (repo_root / tree / skill).resolve()
            if not src.is_relative_to(repo_root) or not src.is_dir():
                continue
            _copy_traversable(src, dst)

    # The project's customization of those upstream skills (_bmad/custom/*.toml).
    # The preflight resolves review layers through these files, but the run inside
    # the worktree resolves them again from ITS OWN project root — and they are
    # routinely absent from a fresh checkout: the upstream installer gitignores
    # `*.user.toml`, and plenty of projects gitignore `_bmad/` whole. Without this
    # the two disagree, and validate approves a layer set the isolated run never
    # runs. Copy-when-absent at file granularity, so a checkout that tracks its
    # own customization keeps every tracked file untouched.
    #
    # Deliberately NOT routed through seed_files: `skipped` reports user-authored
    # `worktree_seed` entries that silently no-op, and this internal seed is not
    # one of those — journaling it would read as a project misconfiguration.
    customize_seeded = False
    src_customize = (repo_root / CUSTOMIZE_DIR).resolve()
    dst_customize = (worktree / CUSTOMIZE_DIR).resolve()
    if (
        src_customize.is_dir()
        and src_customize.is_relative_to(repo_root)
        and dst_customize.is_relative_to(worktree)
    ):
        if not dst_customize.exists():
            dst_customize.parent.mkdir(parents=True, exist_ok=True)
            _copy_traversable(src_customize, dst_customize)
            customize_seeded = True
        elif dst_customize.is_dir():
            customize_seeded = _copy_traversable(src_customize, dst_customize, skip_existing=True)

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
    if customize_seeded:
        patterns.add(f"/{CUSTOMIZE_DIR.as_posix()}")
    _worktree_local_exclude(worktree, sorted(patterns))
    return skipped


class WorktreeFlow:
    """Provision, drive, integrate and reclaim per-unit git worktrees.

    Built once per engine from narrow deps + engine callbacks (see module
    docstring). Behavior is identical to the cluster it was carved out of; the
    only structural changes are that engine-owned effects go through injected
    callables: ``emit`` fires a plugin hook (late-bound so a monkeypatched
    ``Engine._emit`` still wins), ``save`` persists run state, ``gate_unit`` runs
    the per-unit ready gate, ``workspace_get``/``workspace_set`` read and swap the
    engine's active workspace, and ``escalation_pause`` raises the engine's
    ``RunPaused`` (injected so this module need not import ``engine`` — that would
    reintroduce a runtime<->engine import cycle)."""

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
        self._pause = escalation_pause
        self._workspace_get = workspace_get
        self._workspace_set = workspace_set

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
        fakes) contribute nothing, so provisioning is a no-op for them."""
        seen: dict[str, CLIProfile] = {}
        adapters = self._adapters_get()
        for adapter in (adapters["dev"], adapters["review"]):
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

    def run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        """Run one unit's `drive` body in a fresh per-unit worktree, then merge
        it back into the target branch. `drive` either returns (DONE/DEFERRED →
        integrate) or raises RunPaused (spec-approval gate / escalation → leave
        the worktree mounted for resume/inspection, integration skipped)."""
        try:
            unit = self._open_unit_workspace(
                self.paths.repo_root,
                self.paths,
                self.state.run_id,
                task.story_key,
                self.state.target_branch,
                self.policy.scm.branch_per,
                self.run_dir,
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
        task.branch = unit.branch
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
        seeds.extend(scm.worktree_seed)
        # plugins (e.g. the Unity engine) may prime an isolated checkout with
        # gitignored paths they need — e.g. an MCP-generated skill tree + client
        # config so the worktree's Editor MCP is reachable. Aggregate every loaded
        # plugin's declared seeds.
        seeds.extend(self._registry.seed_files())
        skipped_seeds = provision_worktree(
            unit.path,
            profiles,
            self.paths.repo_root,
            seed_files=list(dict.fromkeys(seeds)),  # dedupe, preserve order
            seed_globs=self._registry.seed_globs(),
        )
        if skipped_seeds:
            # A seed entry whose destination already exists is a no-op. Harmless for
            # a file the checkout legitimately carries, but a directory entry is
            # skipped WHOLE the moment any child is tracked — so a `worktree_seed`
            # that looks applied can be copying nothing. Journal it; provision is
            # quiet by contract (it runs under the TUI).
            self.journal.append(
                "worktree-seed-skipped", story_key=task.story_key, entries=skipped_seeds
            )
        self.journal.append(
            "worktree-opened", story_key=task.story_key, branch=unit.branch, path=str(unit.path)
        )
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
        if task.phase == Phase.DONE:
            # Merge the unit branch into the target branch locally. We open PRs
            # ourselves by hand once the branch has landed; the orchestrator only
            # commits the worktree onto the selected target.
            self.merge_local(task, unit)
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

    def merge_local(self, task: StoryTask, unit: UnitWorkspace) -> None:
        """Merge a DONE unit's branch into the target branch from the main repo."""
        self._emit("pre_merge", task)
        scm = self.policy.scm
        repo = self.paths.repo_root
        target = self.state.target_branch
        # A per_worktree Unity Editor can leak asset writes into the *main*
        # checkout (see the unity plugin's worktree setup), dirtying the target with the very
        # files this branch already committed. Reconcile that first: clean only
        # the leaked copies of incoming files; refuse (escalate) if anything dirty
        # falls outside this branch's path set — that may be real operator work.
        try:
            cleaned = verify.clean_incoming_collisions(repo, target, unit.branch)
        except verify.GitError as e:
            reason = (
                f"merge of {unit.branch} into {target} blocked: the target checkout has "
                f"uncommitted changes that are not part of this branch (likely a Unity "
                f"Editor wrote into the main project) — clean them, then "
                f"`bmad-loop resume {self.state.run_id}`. {e}"
            )
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return
        if cleaned:
            self.journal.append(
                "merge-target-cleaned",
                story_key=task.story_key,
                branch=unit.branch,
                paths=cleaned,
            )
        try:
            verify.merge_branch(
                repo,
                unit.branch,
                strategy=scm.merge_strategy,
                message=self.merge_message(task),
            )
        except verify.GitError as e:
            # genuine content conflict against the target: keep the branch for
            # manual merge. The unit committed cleanly (phase is already DONE,
            # which has no legal transition), so escalate directly.
            reason = (
                f"merge of {unit.branch} into {target} failed "
                f"(content conflict against the target): {e}"
            )
            self.keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        self.journal.append(
            "unit-merged",
            story_key=task.story_key,
            branch=unit.branch,
            target=self.state.target_branch,
        )
        self._emit("post_merge", task)
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
        escalate. Shared by the two merge-back failure paths: a target dirtied
        with stray work, and a genuine content conflict."""
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
        """Mark a DONE unit ESCALATED, notify, and pause the run. DONE has no
        legal transition, so the phase is set directly rather than via advance()."""
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

        DONE units drop their worktree at merge time; this is a safety net for a
        worktree leaked by a crash between merge and teardown, plus it prunes
        stale git admin entries and removes the now-empty run worktree dir.
        Worktrees deliberately kept for inspection (a kept-failed/escalated unit)
        are left in place and journaled so the operator can find them."""
        if not self.isolated:
            return
        repo = self.paths.repo_root
        for task in self.state.tasks.values():
            if task.phase == Phase.DONE and task.worktree_path:
                wt = Path(task.worktree_path)
                if wt.is_dir():
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
        if not wt.is_dir():
            self.escalate_unit(
                task,
                f"worktree for {task.story_key} is gone ({wt}); cannot resume in place",
            )
        # spec_file is persisted relative to the worktree (model.to_dict) so the
        # state stays portable; re-absolutize it against the reopened worktree.
        if task.spec_file and not Path(task.spec_file).is_absolute():
            task.spec_file = str(wt / task.spec_file)
        return UnitWorkspace(
            workspace=Workspace(root=wt, paths=self.paths.rebased(wt)),
            repo_root=self.paths.repo_root,
            branch=task.branch,
            path=wt,
            baseline=task.baseline_commit or "",
        )
