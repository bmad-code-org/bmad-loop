"""The deterministic control loop.

Per story: dev session -> artifact verification -> bounded review loop
-> deterministic verify commands -> orchestrator commit. The engine never
edits sprint-status.yaml or spec files; it re-reads them to decide and
verify. All creative work happens inside disposable adapter sessions.
"""

from __future__ import annotations

import contextlib
import functools
import shutil
import signal
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import deferredwork, devcontract, gates, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec
from .bmadconfig import ProjectPaths
from .escalation import (
    Action,
    critical_escalations,
    decide_dev,
    decide_review_session,
    preference_escalations,
)
from .install import provision_worktree
from .journal import Journal, save_state
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
)
from .plugins import HookBus, HookContext, PluginRegistry
from .policy import Policy
from .runs import kill_session
from .sprintstatus import advance as sprint_advance
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable, parse_selector
from .statemachine import advance
from .workspace import (
    UnitWorkspace,
    Workspace,
    close_unit_workspace,
    discard_worktree,
    open_unit_workspace,
    unit_worktrees_dir,
)


class RunPaused(Exception):
    def __init__(self, reason: str, stage: str, story_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.story_key = story_key


class RunStopped(Exception):
    """Raised from the SIGTERM/SIGINT handler to unwind the loop cleanly so the
    engine can mark the run `stopped` (a deliberate stop, distinct from a
    crash) and tear down its in-flight agent session."""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    done: int
    deferred: int
    escalated: int
    paused: bool
    paused_reason: str
    total_tokens: int
    crashed: bool = False
    crash_error: str | None = None

    def render(self) -> str:
        lines = [
            f"run {self.run_id}: {self.done} done, {self.deferred} deferred, "
            f"{self.escalated} escalated, {self.total_tokens:,} tokens"
        ]
        if self.crashed:
            lines.append(f"CRASHED: {self.crash_error}")
        if self.paused:
            lines.append(f"PAUSED: {self.paused_reason}")
        return "\n".join(lines)


# CLI profile name -> the agent id the Unity-MCP CLI's `setup-mcp` expects (see
# `unity-mcp-cli setup-mcp --list`). All but claude differ only by claude's
# "-code" suffix; codex/gemini/cursor and any custom profile pass through as-is.
_SETUP_MCP_AGENT_IDS = {"claude": "claude-code"}

# Appended to every injected plugin-workflow session prompt. The dev/review
# skills carry their own result conventions, but a workflow prompt is arbitrary
# text from a plugin manifest — without an explicit protocol the session has to
# *infer* the completion-marker convention, and one that finishes its work but
# never writes the marker leaves the orchestrator waiting (a completion-signal
# livelock, bounded only by session_timeout_min). The orchestrator's adapter
# discovers the marker by its `bmad-dev-auto-result-` filename prefix and
# mtime, not by exact name.
WORKFLOW_COMPLETION_CONTRACT = """

## Completion signal (required)

When you have finished this workflow — fully done OR blocked and unable to
proceed — you MUST create the file:

    {marker_path}

containing YAML frontmatter that declares the outcome, then end your turn:

    ---
    status: done
    ---

Use `status: blocked` (plus a short explanation in the body) if you could not
finish. This marker is the orchestrator's only completion signal for this
session; it is required in addition to any artifacts the workflow itself
produces. If you end your turn without it, the session is eventually declared
stalled and its work may be discarded."""


def _setup_mcp_agent_id(profile_name: str) -> str:
    """Map a CLI profile name to its Unity-MCP `setup-mcp` agent id."""
    return _SETUP_MCP_AGENT_IDS.get(profile_name, profile_name)


class Engine:
    # The engine that installed the process-wide stop handlers; nested
    # auto-sweep runs (same process) see it set and let RunStopped propagate up.
    _stop_signals_owner: "Engine | None" = None

    def __init__(
        self,
        paths: ProjectPaths,
        policy: Policy,
        adapter: CodingCLIAdapter,
        run_dir: Path,
        journal: Journal,
        state: RunState,
        max_stories: int | None = None,
        epic_filter: int | None = None,
        story_filter: str | None = None,
        review_adapter: CodingCLIAdapter | None = None,
        sweep_factory: Callable[[str], None] | None = None,
        registry: PluginRegistry | None = None,
    ):
        self.paths = paths
        # where code+git work + artifact reads happen. isolation="none" (today's
        # only mode) → the repo root in place; Phase 3 swaps in per-unit worktrees.
        self.workspace = Workspace.default(paths)
        self.policy = policy
        self.adapters = {
            "dev": adapter,
            "review": review_adapter if review_adapter is not None else adapter,
        }
        self.run_dir = run_dir
        self.journal = journal
        self.state = state
        self.max_stories = max_stories
        self.epic_filter = epic_filter
        self.story_filter = story_filter
        # widen --story interpretation: full key, short ref (3-1/3.1), bare
        # number (+ --epic), or slug fragment. See sprintstatus.StorySelector.
        self._selector = parse_selector(epic_filter, story_filter)
        # spawns a child deferred-work sweep run (injected by the CLI to
        # avoid an engine -> sweep import cycle); see _maybe_auto_sweep
        self.sweep_factory = sweep_factory
        # plugin hook bus. Built silently (no journal handed to the registry) so a
        # zero-plugin run — the only builtin is the data-only `example` — adds
        # nothing to the journal and stays byte-identical to today. The bus
        # journals actual hook activity itself; a single "plugins-active" line
        # records the live plugins only when at least one binds a stage. The
        # game-engine layer (Unity) is now itself a plugin: enabling it in
        # [plugins] gives it lifecycle hooks that gate/manage the Editor.
        self._registry = (
            registry if registry is not None else PluginRegistry.build(self.paths.repo_root, policy)
        )
        # let every in-process plugin reject an incompatible config at startup
        # (e.g. the Unity plugin's editor_mode↔scm.isolation coupling) so the run
        # fails fast rather than mid-unit.
        self._registry.validate(policy)
        self._bus = HookBus(self._registry, journal)
        # stages at which some active plugin injects a provided workflow session
        # (Phase 4). Precomputed once for an O(1) guard so a run whose plugins
        # provide no workflows stays byte-identical (no extra sessions, no journal).
        self._workflow_stages = self._registry.workflow_stages()
        if self._bus.any_active():
            self.journal.append("plugins-active", plugins=self._bus.active_plugins())
        # stop-signal bookkeeping (see run())
        self._owns_signals = False
        # True iff an outer engine already owned the stop handlers when this one
        # started — i.e. this is a nested auto-sweep run. Distinct from
        # _owns_signals, which is also False for a top-level engine that simply
        # could not install handlers (e.g. off the main thread).
        self._is_nested = False
        self._stopping = False
        self._prev_handlers: dict[int, object] = {}

    # ------------------------------------------------------------- top level

    def run(self) -> RunSummary:
        self._install_stop_signals()
        try:
            try:
                # target-branch setup can raise RunPaused (detached HEAD, unborn
                # repo), so it must sit inside the pause handler, not before it.
                self._emit_run_boundary("pre_run")
                self._ensure_target_branch()
                self._prune_preserve_refs()
                self._loop()
                self.state.finished = True
                self._gc_run_worktrees()
                self._emit("post_run")
                self.journal.append("run-complete")
                # tear down the run's agent session now that it finished. Only
                # the outermost engine owns this (nested auto-sweep never sets
                # _owns_signals); stop already kills it, and pause/interrupt
                # leave it for resume to reuse.
                if self._owns_signals and self.policy.adapter.cleanup_session_on_finish:
                    kill_session(self.state.run_id)
            except RunPaused as pause:
                self.state.paused_reason = pause.reason
                self.state.paused_stage = pause.stage
                self.state.paused_story_key = pause.story_key
                self.journal.append(
                    "run-paused",
                    reason=pause.reason,
                    stage=pause.stage,
                    story_key=pause.story_key,
                )
            except RunStopped:
                # the loop was interrupted inside adapter.run(), so the agent
                # window is still live — tear the whole run session down.
                kill_session(self.state.run_id)
                if self._is_nested:
                    raise  # nested auto-sweep: let the owner record the stop
                self.state.stopped = True
                self.journal.append("run-stop")
            except KeyboardInterrupt:
                # Some Windows console/control events can still surface as a raw
                # KeyboardInterrupt without routing through the installed signal
                # handler. Persist a controlled stop rather than letting the
                # engine disappear with stale state.
                self._stopping = True  # swallow stop signals landing mid-teardown
                try:
                    kill_session(self.state.run_id)
                except (
                    BaseException
                ):  # noqa: BLE001  # nosec B110 - best-effort teardown; the stop must still record
                    pass
                if self._is_nested:
                    raise
                self.state.stopped = True
                self.journal.append("run-stop", reason="KeyboardInterrupt")
            except Exception as exc:
                # an unexpected exception escaped the loop (e.g. a transport
                # hang that leaked past the seam). Don't let it die to the lossy
                # parked control pane: persist the traceback, tear down the
                # orphaned agent session, and fall through to a crashed summary.
                tb = traceback.format_exc()
                # a crash is never also "finished": the loop may have set
                # finished=True (line above) before a post-run step threw, and
                # status classification checks finished first — so a recorded
                # crash would otherwise read as FINISHED. Reset before the nested
                # re-raise so the trailing _save() persists it on both paths.
                self.state.finished = False
                try:
                    (self.run_dir / "crash.txt").write_text(tb, encoding="utf-8")
                except OSError:
                    pass
                try:
                    kill_session(self.state.run_id)
                except (
                    Exception
                ):  # noqa: BLE001  # nosec B110 - best-effort teardown; a crashing run must still record
                    pass
                if self._is_nested:
                    raise  # nested auto-sweep: let the owner record the failure
                try:
                    message = str(exc)
                except Exception:
                    message = type(exc).__name__
                self.state.crashed = True
                self.state.crash_error = f"{type(exc).__name__}: {message}"
                try:
                    self.journal.append(
                        "run-crash",
                        error=type(exc).__name__,
                        message=message,
                        epic=self.state.current_epic,
                    )
                except (
                    Exception
                ):  # noqa: BLE001  # nosec B110 - journal write is best-effort; crash.txt + state flag already persisted
                    pass
            finally:
                self._save()
        finally:
            self._restore_stop_signals()
        summary = self.summary()
        gates.notify(self.policy, self.run_dir, "bmad-loop run finished", summary.render())
        return summary

    # ---------------------------------------------------------- stop signals

    def _install_stop_signals(self) -> None:
        """Make SIGTERM/SIGINT unwind the loop as a RunStopped. Only the
        outermost engine in the process owns the handlers (nested auto-sweep
        runs let the exception propagate up to it); install is best-effort and
        silently skipped off the main thread (signal.signal raises there)."""
        # capture nesting before the early return: a non-None owner here means an
        # outer engine already installed the handlers, so we are nested.
        self._is_nested = Engine._stop_signals_owner is not None
        if Engine._stop_signals_owner is not None:
            return

        windows_ctrl_signals = {signal.SIGINT}
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            windows_ctrl_signals.add(sigbreak)

        def handler(signum, frame):  # noqa: ANN001 - stdlib signal signature
            if sys.platform == "win32" and signum in windows_ctrl_signals:
                # best-effort: a journal error must never escape a signal handler.
                with contextlib.suppress(Exception):
                    self.journal.append("console-ctrl-ignored", signum=signum)
                return
            if self._stopping:
                return  # already unwinding; don't re-raise during teardown
            self._stopping = True
            raise RunStopped()

        try:
            signals = [signal.SIGTERM, signal.SIGINT]
            if sys.platform == "win32" and sigbreak is not None:
                signals.append(sigbreak)
            for sig in dict.fromkeys(signals):
                self._prev_handlers[sig] = signal.signal(sig, handler)
        except ValueError:
            # not on the main thread — cannot install; degrade to no handler
            self._restore_stop_signals()
            return
        self._owns_signals = True
        Engine._stop_signals_owner = self

    def _restore_stop_signals(self) -> None:
        for sig, prev in self._prev_handlers.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, TypeError):
                pass
        self._prev_handlers.clear()
        if Engine._stop_signals_owner is self:
            Engine._stop_signals_owner = None
        self._owns_signals = False

    # ----------------------------------------------------- worktree isolation

    @property
    def _isolated(self) -> bool:
        return self.policy.scm.isolation == "worktree"

    def _ensure_target_branch(self) -> None:
        """Resolve (once, at run start) the branch every unit merges back into.

        No-op unless isolation=worktree. Default target is the branch checked out
        now; a configured target is created if missing and checked out in the
        main repo (merges land on whatever the main repo has checked out, and a
        unit worktree must never check out the target itself). Pinned in state so
        resume keeps targeting the same branch."""
        if not self._isolated or self.state.target_branch:
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
                    raise RunPaused(
                        f"cannot create target branch {configured!r}: {e}",
                        PAUSE_ESCALATION,
                        "",
                    ) from e
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
                raise RunPaused(
                    "isolation=worktree on a detached HEAD: check out a branch or "
                    "set scm.target_branch before running",
                    PAUSE_ESCALATION,
                    "",
                )
            self.state.target_branch = current
        self.journal.append("target-branch", branch=self.state.target_branch)
        self._save()

    def _worktree_profiles(self):
        """The distinct CLI profiles of the dev + review adapters, for provisioning
        their skills/hooks into a worktree. Adapters without a `profile` (e.g. test
        fakes) contribute nothing, so provisioning is a no-op for them."""
        seen: dict[str, object] = {}
        for adapter in (self.adapters["dev"], self.adapters["review"]):
            profile = getattr(adapter, "profile", None)
            if profile is not None and profile.name not in seen:
                seen[profile.name] = profile
        return list(seen.values())

    def _engine_agent_ids(self) -> list[str]:
        """The Unity-MCP `setup-mcp` agent ids for every CLI that runs in a
        worktree (dev + review). A worktree can host more than one agent — e.g.
        dev=claude, review=codex — and each reads its own MCP config file, so the
        per_worktree setup must point every one of them at the worktree's Editor,
        not just the dev agent. Deduped, order-preserving; empty for test fakes."""
        ids: list[str] = []
        for profile in self._worktree_profiles():
            agent = _setup_mcp_agent_id(profile.name)
            if agent not in ids:
                ids.append(agent)
        return ids

    def _run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        """Run one unit's `drive` body in a fresh per-unit worktree, then merge
        it back into the target branch. `drive` either returns (DONE/DEFERRED →
        integrate) or raises RunPaused (spec-approval gate / escalation → leave
        the worktree mounted for resume/inspection, integration skipped)."""
        try:
            unit = open_unit_workspace(
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
        profiles = self._worktree_profiles()
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
        provision_worktree(
            unit.path,
            profiles,
            self.paths.repo_root,
            seed_files=list(dict.fromkeys(seeds)),  # dedupe, preserve order
            seed_globs=self._registry.seed_globs(),
        )
        self.journal.append(
            "worktree-opened", story_key=task.story_key, branch=unit.branch, path=str(unit.path)
        )
        self._save()
        prev = self.workspace
        self.workspace = unit.workspace
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
            self.workspace = prev
        # reached only on a normal return (DONE or DEFERRED); a RunPaused from the
        # spec gate or an escalation propagates past here, leaving the worktree up.
        self._integrate_unit(task, unit)

    def _failed_diff_max_bytes(self) -> int | None:
        """Per-untracked-file size cap for a failed unit's forensic patch, in
        bytes — or None when the operator lifted the cap (scm.failed_diff_unlimited)."""
        scm = self.policy.scm
        if scm.failed_diff_unlimited:
            return None
        return scm.failed_diff_max_mb * 1_048_576

    def _integrate_unit(self, task: StoryTask, unit: UnitWorkspace) -> None:
        self._emit("pre_integrate", task)
        scm = self.policy.scm
        if task.phase == Phase.DONE:
            # Merge the unit branch into the target branch locally. We open PRs
            # ourselves by hand once the branch has landed; the orchestrator only
            # commits the worktree onto the selected target.
            self._merge_local(task, unit)
        else:  # DEFERRED — capture the diff, keep or drop per keep_failed
            patch = close_unit_workspace(
                unit,
                success=False,
                keep_failed=scm.keep_failed,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=scm.delete_branch,
                diff_max_file_bytes=self._failed_diff_max_bytes(),
            )
            self.journal.append(
                "unit-closed",
                story_key=task.story_key,
                branch=unit.branch,
                kept=scm.keep_failed,
                patch=str(patch) if patch else None,
            )

    def _merge_local(self, task: StoryTask, unit: UnitWorkspace) -> None:
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
            self._keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
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
                message=self._merge_message(task),
            )
        except verify.GitError as e:
            # genuine content conflict against the target: keep the branch for
            # manual merge. The unit committed cleanly (phase is already DONE,
            # which has no legal transition), so escalate directly.
            reason = (
                f"merge of {unit.branch} into {target} failed "
                f"(content conflict against the target): {e}"
            )
            self._keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
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
        )

    def _keep_branch_and_escalate(self, task: StoryTask, unit: UnitWorkspace, reason: str) -> None:
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
            diff_max_file_bytes=self._failed_diff_max_bytes(),
        )
        self._escalate_unit(task, reason)  # always raises RunPaused

    def _escalate_unit(self, task: StoryTask, reason: str) -> None:
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
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _merge_message(self, task: StoryTask) -> str:
        return f"Merge {task.branch} into {self.state.target_branch} (bmad-loop)"

    def _gc_run_worktrees(self) -> None:
        """Reclaim this run's worktree scaffolding once it finishes cleanly.

        DONE units drop their worktree at merge time; this is a safety net for a
        worktree leaked by a crash between merge and teardown, plus it prunes
        stale git admin entries and removes the now-empty run worktree dir.
        Worktrees deliberately kept for inspection (a kept-failed/escalated unit)
        are left in place and journaled so the operator can find them."""
        if not self._isolated:
            return
        repo = self.paths.repo_root
        for task in self.state.tasks.values():
            if task.phase == Phase.DONE and task.worktree_path:
                wt = Path(task.worktree_path)
                if wt.is_dir():
                    discard_worktree(repo, task.worktree_path, task.branch)
            elif task.terminal and task.worktree_path and Path(task.worktree_path).is_dir():
                # kept on purpose (keep_failed): leave it, but surface where.
                self.journal.append(
                    "worktree-kept", story_key=task.story_key, path=task.worktree_path
                )
        verify.worktree_prune(repo)
        worktrees_parent = unit_worktrees_dir(self.run_dir)
        if worktrees_parent.is_dir() and not any(worktrees_parent.iterdir()):
            worktrees_parent.rmdir()

    def _reopen_unit(self, task: StoryTask) -> UnitWorkspace:
        """Reconstruct the UnitWorkspace for an in-flight unit on resume, from
        the worktree path + branch persisted on the task. The worktree must still
        be mounted — if it was pruned out from under us we cannot safely reuse it,
        so escalate rather than run a session in a missing directory."""
        wt = Path(task.worktree_path)
        if not wt.is_dir():
            self._escalate_unit(
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

    def summary(self) -> RunSummary:
        tasks = self.state.tasks.values()
        return RunSummary(
            run_id=self.state.run_id,
            done=sum(1 for t in tasks if t.phase == Phase.DONE),
            deferred=sum(1 for t in tasks if t.phase == Phase.DEFERRED),
            escalated=sum(1 for t in tasks if t.phase == Phase.ESCALATED),
            paused=self.state.paused,
            paused_reason=self.state.paused_reason or "",
            total_tokens=sum(t.tokens.total for t in tasks),
            crashed=self.state.crashed,
            crash_error=self.state.crash_error,
        )

    def _loop(self) -> None:
        self._finish_inflight()
        started = 0
        while True:
            if self.max_stories is not None and started >= self.max_stories:
                self.journal.append("max-stories-reached", count=started)
                return
            self._emit("pre_pick_next")
            story = self._pick_next()
            self._emit("post_pick_next", story_key=(story.key if story is not None else None))
            if story is None:
                self._maybe_auto_sweep("run-end", "run-end")
                return
            if self.state.current_epic is not None and story.epic != self.state.current_epic:
                self._epic_boundary(self.state.current_epic, story.epic)
            self.state.current_epic = story.epic
            task = StoryTask(story_key=story.key, epic=story.epic)
            self.state.tasks[story.key] = task
            self.journal.append("story-start", story_key=story.key)
            self._save()
            started += 1
            self._run_story(task)

    def _pick_next(self):
        ss = load_sprint_status(self.paths.sprint_status)
        if ss.unknown_keys:
            self.journal.append("sprint-status-unknown-keys", keys=list(ss.unknown_keys))
        base_skip = set(self.state.tasks)  # anything this run already touched

        def _first(epic: int | None):
            # local skip copy so selector-rejections in this pass don't leak into
            # the next one (a story rejected here may still match the fallback).
            skip = set(base_skip)
            while True:
                story = next_actionable(ss, skip, epic=epic)
                if story is None:
                    return None
                if not self._selector.matches(story):
                    skip.add(story.key)
                    continue
                return story

        # Exhaust the current epic before advancing. Selection is otherwise
        # strict file order, and epics need not be file-ordered by number (an
        # epic can be appended out of place); without this, a still-open earlier-
        # in-file epic would "steal" the pick and fire a spurious epic boundary.
        if self.state.current_epic is not None:
            story = _first(self.state.current_epic)
            if story is not None:
                return story
        return _first(None)

    def _protected_relpaths(self) -> tuple[str, ...]:
        """Repo-relative posix paths of the BMAD artifact folders. These are
        orchestrator-owned: never counted as a dev attempt's dirtiness (the
        resolve workflow corrects the frozen spec here) and preserved through
        rollback. Folders configured outside the repo are skipped — nothing to
        protect there."""
        out: list[str] = []
        for protected in (
            self.workspace.paths.output_folder,
            self.workspace.paths.implementation_artifacts,
            self.workspace.paths.planning_artifacts,
        ):
            try:
                rel = protected.relative_to(self.workspace.root).as_posix()
            except ValueError:
                continue  # configured outside the repo; nothing to protect here
            # "." (folder == repo root) as an exclude/keep prefix would cover the
            # whole tree — drop it so a misconfig can't disable the dirty check.
            if rel and rel != ".":
                out.append(rel)
        return tuple(out)

    def _rollback_or_pause(self, task: StoryTask, *, cause: str = "stopped") -> None:
        """Recover from an in-place attempt that won't proceed.

        No-op when the tree is already at the attempt's baseline (nothing this
        attempt touched, ignoring orchestrator-owned artifact folders): neither a
        reset nor a pause is needed. This is also what lets the manual-recovery
        instructions terminate — after the operator resets and resumes, the
        now-clean tree skips straight through instead of re-pausing on the
        still-set ``baseline_commit``.

        A ``cause="resolved"`` re-drive is human-initiated (the operator ran the
        resolve workflow and re-armed the story), so it always auto-recovers and
        never pauses, regardless of ``scm.rollback_on_failure``. For the entire
        re-drive (``task.resolved_redrive``, latched at resume and cleared once the
        correction is committed) the BMAD artifact folders are treated as
        orchestrator-owned: excluded from the dirty check (the corrected spec must
        not read as a failed attempt) and preserved through every reset — so a
        later mid-re-drive retry/defer reset can't silently revert the correction.

        Otherwise (a stopped/abandoned attempt) the flag governs: OFF (default)
        leaves the working tree untouched and emits a bold manual-recovery notice
        that pauses the run (stop-and-wait); ON does a clean reset to baseline.
        Either way pre-existing untracked files are preserved; there is no blanket
        ``git clean``."""
        resolved = cause == "resolved"
        # preserve the corrected spec for the whole re-drive, not just the first
        # reset; the auto-recover (pause-vs-reset) decision below is unaffected.
        redrive = resolved or task.resolved_redrive
        protected = self._protected_relpaths() if redrive else ()
        if task.baseline_commit and not verify.attempt_dirty(
            self.workspace.root, task.baseline_commit, task.baseline_untracked, exclude=protected
        ):
            self.journal.append("rollback-skipped-clean", story_key=task.story_key)
            return
        if resolved or self.policy.scm.rollback_on_failure:
            self.journal.append(
                "rollback-auto",
                story_key=task.story_key,
                baseline=task.baseline_commit or "",
                note="reverting tracked changes + run-created untracked files",
            )
            # A re-drive (resolved / mid-re-drive) is contractually pause-free, so it
            # preserves best-effort but never blocks; a plain rollback pauses rather
            # than reset past work it could not park.
            self._preserve_attempt_commits(task, allow_pause=not redrive)
            # Park the attempt's uncommitted diff too, so the reset below (and its
            # untracked cleanup) can't silently destroy in-progress work. Runs only
            # if _preserve_attempt_commits did not pause (plain-rollback preserve
            # failure); best-effort, never blocks.
            self._preserve_attempt_worktree(task)
            self._safe_reset(task, preserve=protected)
            return
        self._pause_for_manual_recovery(task, task.baseline_commit or "")
        return  # unreachable: _pause_for_manual_recovery always raises

    def _safe_reset(self, task: StoryTask, *, preserve: tuple[str, ...] = ()) -> None:
        """Revert tracked changes to the task baseline and remove only the
        untracked files this run created — never a blanket `git clean`. Used by
        the gated/resolved rollback and by internal ledger recovery (sweep
        migration), which restores the orchestrator's own state and must not
        pause. The BMAD artifact folders are always kept from untracked deletion;
        ``preserve`` (set only on a resolved re-drive) additionally keeps their
        *tracked* content alive through the reset, so a just-corrected spec is not
        reverted. Sweep passes no ``preserve`` — it wants the broken ledger gone."""
        verify.safe_rollback(
            self.workspace.root,
            task.baseline_commit or "",
            baseline_untracked=task.baseline_untracked,
            keep=(".bmad-loop", *self._protected_relpaths()),
            preserve=preserve,
        )

    def _prune_preserve_refs(self) -> None:
        """Bounded retention for both recovery-ref families at run start — the
        attempt-preserve/* branches and the refs/attempt-preserve-dirty/*
        worktree snapshots: keep the newest scm.preserve_keep of each by
        committer date, delete the tail (mirrors the runs/cleanup retention
        knobs — without it the refs grow unbounded on a long-lived project).
        Best-effort: a git failure is journalled per family and never blocks or
        pauses the run — the refs are a safety net, not run state — and a
        failure in one family never skips the other. preserve_keep = 0 disables
        pruning entirely."""
        keep = self.policy.scm.preserve_keep
        if keep <= 0:
            return
        for family, prune in (
            ("attempt-preserve", verify.prune_preserve_refs),
            ("attempt-preserve-dirty", verify.prune_preserve_dirty_refs),
        ):
            try:
                deleted = prune(self.workspace.root, keep)
            except Exception as exc:  # noqa: BLE001 - housekeeping must never crash the
                # run: a git timeout/OSError here would otherwise escape to the crash
                # handler, so anything beyond the expected GitError is journalled too
                # A partial prune (PrunePreserveError) already deleted refs before one
                # stuck — that destructive half must stay structurally auditable, not
                # buried in the error string.
                partial = getattr(exc, "deleted", [])
                if partial:
                    self.journal.append(f"{family}-pruned", count=len(partial), refs=partial)
                failed = getattr(exc, "failed", [])
                if failed:
                    self.journal.append(f"{family}-prune-failed", error=str(exc), failed=failed)
                else:
                    self.journal.append(f"{family}-prune-failed", error=str(exc))
                continue
            if deleted:
                self.journal.append(f"{family}-pruned", count=len(deleted), refs=deleted)

    def _preserve_attempt_commits(self, task: StoryTask, *, allow_pause: bool) -> None:
        """Before an auto-rollback's hard reset, park any commits the attempt made
        above its baseline under a named recovery ref, so `reset --hard baseline`
        can't silently orphan committed work (it survives `git gc` and is
        recoverable by name, not just the reflog). No-op when the attempt added no
        commits — an uncommitted-only revert is the intended, non-destructive case.

        If commits exist but the ref cannot be created: with ``allow_pause`` (a
        plain rollback) refuse to reset — pause for manual recovery rather than
        destroy the work. On a re-drive (``allow_pause=False``) the caller's
        contract forbids pausing, so journal the failure and let the reset proceed
        (the re-drive is a human-directed discard of the failed attempt)."""
        baseline = task.baseline_commit
        if not baseline:
            return
        commits = verify.commits_above(self.workspace.root, baseline)
        if not commits:
            return
        head = verify.rev_parse_head(self.workspace.root)  # the tip the recovery ref parks at
        # run_id can be an arbitrary user `--run-id`; keep the ref component git-safe
        # and length-bounded so an exotic/overlong id can't blow the ref-name limit,
        # fail `git branch`, and drop the recovery ref (which on a re-drive would then
        # reset past the work anyway).
        slug = "".join(c if (c.isalnum() or c in "_-") else "-" for c in self.state.run_id)[:64]
        try:
            ref = verify.preserve_commits(
                self.workspace.root,
                baseline,
                f"attempt-preserve/{slug}-{head[:8]}",
                commits=commits,
            )
        except verify.GitError:
            ref = None  # branch creation failed — treat as a preservation failure
        if ref is None:
            # commits exist (just enumerated) but the ref did not take.
            self.journal.append("attempt-preserve-failed", story_key=task.story_key, head=head)
            if allow_pause:
                # the commits at HEAD could not be parked — the notice must NOT tell
                # the operator to blindly `reset --hard` (that would discard them).
                self._pause_for_manual_recovery(task, baseline, preserve_failed=True)
            return  # re-drive: never pause — proceed to the (human-directed) reset
        self.journal.append(
            "attempt-commits-preserved", story_key=task.story_key, ref=ref, count=len(commits)
        )

    def _preserve_attempt_worktree(self, task: StoryTask) -> None:
        """Before an auto-rollback's hard reset, park the attempt's *uncommitted*
        working-tree changes (tracked edits + run-created untracked files) under a
        named recovery ref, so `reset --hard baseline` and its untracked cleanup
        can't silently destroy in-progress work. Complements
        `_preserve_attempt_commits` (which parks *committed* work above baseline);
        together they cover the whole attempt. No-op when the tree is clean vs HEAD
        — the intended non-destructive uncommitted-revert case. Best-effort: a
        capture failure is journaled but never blocks the (human-directed re-drive
        or policy-gated) reset — the recovery ref is a safety net, not a gate."""
        baseline = task.baseline_commit
        if not baseline:
            return
        # Same git-safe, length-bounded slug as _preserve_attempt_commits so an
        # exotic/overlong --run-id can't blow the ref-name limit and drop the ref.
        slug = "".join(c if (c.isalnum() or c in "_-") else "-" for c in self.state.run_id)[:64]
        # ``baseline_commit`` is fixed across the whole dev retry loop, so keying the
        # ref on the baseline alone would make a 2nd dirty rollback reuse the name and
        # orphan the 1st attempt's snapshot. ``task.attempt`` only ever increments
        # (never resets), so it uniquely discriminates each retry's recovery ref.
        ref = f"refs/attempt-preserve-dirty/{slug}-{baseline[:8]}-{task.attempt}"
        try:
            parked = verify.snapshot_worktree(
                self.workspace.root, ref, baseline_untracked=task.baseline_untracked
            )
        except verify.GitError as exc:
            # Keep the git failure detail (commit-tree/update-ref stderr): if the
            # following reset destroys work, this is the only breadcrumb explaining
            # why the safety-net snapshot couldn't be captured.
            self.journal.append(
                "attempt-worktree-preserve-failed", story_key=task.story_key, error=str(exc)
            )
            return
        if parked:
            self.journal.append("attempt-worktree-preserved", story_key=task.story_key, ref=parked)

    def _pause_for_manual_recovery(
        self, task: StoryTask, baseline: str, *, preserve_failed: bool = False
    ) -> None:
        """Leave the tree untouched, surface bold manual-recovery instructions, and
        pause the run. Always raises RunPaused. Reached either (a, default) the OFF
        path for a stopped/abandoned in-place attempt, or (b, ``preserve_failed``)
        rollback is ON/resolved but the attempt's commits above baseline could not be
        parked on a recovery ref, so an automatic ``reset --hard`` would silently
        discard them — a distinct notice that names the at-risk commits and never
        tells the operator to blindly reset. A *resolved* escalation never reaches
        here — `_rollback_or_pause` auto-recovers that human-initiated re-drive
        regardless of `scm.rollback_on_failure`."""
        short = baseline[:12] or "<baseline_commit>"
        if preserve_failed:
            notice = (
                "**ACTION REQUIRED — commits could not be auto-preserved**\n"
                f"Story **{task.story_key}**'s attempt committed work above its "
                "baseline, but a recovery ref for those commits could not be created, "
                "so the automatic rollback was refused rather than `reset --hard` "
                "past (and discard) them. **Your commits are intact at the current "
                "HEAD.**\n"
                "  1. **Save them first** — e.g. `git branch my-rescue HEAD` (the "
                f"commits are `{short}..HEAD`).\n"
                "  2. Only once they are safe, discard the attempt if you want to: "
                f"`git reset --hard {short}`, then review/remove leftover untracked "
                "files.\n"
                f"Then run `bmad-loop resume {self.state.run_id}`."
            )
        else:
            why = (
                f"Story **{task.story_key}**'s attempt was stopped and auto-rollback "
                "is OFF, so the working tree was left exactly as-is for you to "
                "inspect.\n"
            )
            notice = (
                "**ACTION REQUIRED — manual rollback needed**\n"
                f"{why}"
                "To discard this attempt yourself:\n"
                "  1. **BACK UP any untracked files you want to keep** — the reset "
                "below deletes uncommitted work.\n"
                f"  2. `git reset --hard {short}` then review/remove leftover "
                "untracked files.\n"
                "  3. **Restore the files you backed up in step 1.**\n"
                f"Then run `bmad-loop resume {self.state.run_id}`. To let the "
                "orchestrator do a safe automatic rollback next time, enable "
                "`[scm] rollback_on_failure` (it discards the attempt's uncommitted "
                "work but never deletes pre-existing untracked files)."
            )
        self.journal.append("rollback-manual-required", story_key=task.story_key, baseline=baseline)
        gates.notify(
            self.policy,
            self.run_dir,
            f"ACTION REQUIRED: manual rollback for {task.story_key}",
            notice,
        )
        self._save()
        raise RunPaused(notice, PAUSE_ESCALATION, task.story_key)

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            isolated = self._isolated and task.worktree_path
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                # paused at the spec-approval gate: dev verified, review pending
                self.journal.append("resume-review", story_key=task.story_key)
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._review_and_commit(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._review_and_commit(task)
            elif (resumable := self._resumable_session(task)) is not None:
                # the host died inside the post-session window: the session
                # itself completed and its recorded result is on disk, so
                # continue into the normal verify/decide pipeline instead of
                # rolling the finished work back through resume-restart.
                role, result = resumable
                self.journal.append("resume-verify", story_key=task.story_key, role=role)
                if role == "dev":
                    # deliberate reset like the restart arm: _dev_phase re-enters
                    # its loop and consumes the recorded result instead of
                    # running a session
                    task.phase = Phase.PENDING
                    continuation = functools.partial(self._drive_story, task, dev_resume=result)
                else:
                    # deliberate reset to the legal pre-review phase
                    task.phase = Phase.DEV_VERIFY
                    continuation = functools.partial(
                        self._review_and_commit, task, resume_result=result
                    )
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        continuation()
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    continuation()
            else:
                self.journal.append(
                    "resume-restart", story_key=task.story_key, phase=str(task.phase)
                )
                if isolated:
                    # drop the half-built worktree; _run_story mounts a fresh one
                    discard_worktree(self.paths.repo_root, task.worktree_path, task.branch)
                    task.worktree_path = ""
                    task.branch = ""
                elif task.baseline_commit:
                    # latch resolved_redrive so the corrected spec stays protected
                    # through every reset of this re-drive, not just this first one
                    task.resolved_redrive = task.resolved_redrive or task.rearmed
                    self._rollback_or_pause(task, cause="resolved" if task.rearmed else "stopped")
                task.rearmed = False  # past rollback (only reached when not paused)
                task.phase = Phase.PENDING  # deliberate reset, not a normal transition
                self._save()
                self._run_story(task)

    def _resumable_session(self, task: StoryTask) -> tuple[str, SessionResult] | None:
        """The in-flight session's durably-recorded result, when complete enough
        to act on: the task died mid-phase but its current attempt/cycle record
        is ``completed`` and carries the parsed result. Consumes only evidence
        the adapter vouched for at session end — no artifact re-scan, no
        loosening of completion authority. Anything less returns None and the
        caller falls through to resume-restart."""
        if task.phase == Phase.DEV_RUNNING:
            role, seq = "dev", task.attempt
        elif task.phase == Phase.REVIEW_RUNNING:
            role, seq = "review", task.review_cycle
        else:
            return None
        task_id = f"{task.story_key}-{role}-{seq}"
        for record in reversed(task.sessions):
            if record.task_id != task_id:
                continue
            if record.status != "completed" or record.result_json is None:
                return None
            return role, SessionResult(
                status="completed",
                result_json=record.result_json,
                session_id=record.session_id,
                transcript_path=record.transcript_path,
            )
        return None

    # ------------------------------------------------------------- per story

    def _gate_unit(self, task: StoryTask) -> bool:
        """per_worktree gate: emit ``pre_worktree_setup`` then ``pre_ready_gate``
        so a plugin (e.g. the Unity engine) can launch + wait for the unit's
        managed Editor. Returns True to proceed; a veto at either stage routes the
        unit to DEFERRED/PAUSE via ``_vetoed`` (which raises on pause) and returns
        False. A zero-plugin run takes the O(1) fast path and proceeds."""
        ctx = self._emit("pre_worktree_setup", task)
        if self._vetoed(ctx, task):
            return False
        ctx = self._emit("pre_ready_gate", task)
        if self._vetoed(ctx, task):
            return False
        self._emit("post_ready_gate", task)
        return True

    # --------------------------------------------------------- plugin hook bus

    def _emit(self, stage: str, task: StoryTask | None = None, **fields) -> HookContext | None:
        """Fire plugin hooks for ``stage``, or return None on the O(1) no-op fast
        path (no plugin binds the stage → a zero-plugin run does no work). Builds
        a HookContext from the task + extra fields, dispatches it through the bus,
        and returns it so the caller can read whitelisted mutations / resolve a
        veto. ``ctx.shared`` aliases ``state.plugin_shared`` so cross-stage
        mutations persist automatically."""
        if not self._bus.active(stage):
            return None
        ctx = self._make_context(stage, task, **fields)
        self._bus.emit(stage, ctx)
        return ctx

    def _make_context(self, stage: str, task: StoryTask | None, **fields) -> HookContext:
        base: dict = {
            "run_id": self.state.run_id,
            "repo_root": str(self.paths.repo_root),
            "run_dir": str(self.run_dir),
            "shared": self.state.plugin_shared,
            # the dev + review CLI agent ids in this unit's worktree, for a plugin
            # that routes per-agent config (the Unity engine's MCP routing).
            "agents": tuple(self._engine_agent_ids()),
        }
        if task is not None:
            base.update(
                story_key=task.story_key,
                epic=task.epic,
                phase=str(task.phase),
                attempt=task.attempt,
                worktree=task.worktree_path or str(self.workspace.root),
                branch=task.branch or None,
            )
        base.update(fields)
        return HookContext(stage, **base)

    def _vetoed(self, ctx: HookContext | None, task: StoryTask) -> bool:
        """Route a per-unit veto onto the engine's existing control flow. Returns
        True if the unit was vetoed (the caller should stop driving it).

        The phase is set *directly* (not via ``advance``) because a veto can fire
        from a stage with no legal transition to a terminal phase (e.g. PENDING) —
        the same deliberate move the engine's own gate-failure / DONE-unit paths
        make. ``skip`` quietly retires the unit (DEFERRED, no notify) so the loop
        continues and resume sees a terminal task; ``defer`` notifies; ``pause``
        escalates and raises RunPaused."""
        if ctx is None:
            return False
        veto = ctx.resolved_veto()
        if veto is None:
            return False
        msg = f"plugin {veto.plugin_id!r} vetoed {ctx.stage}: {veto.reason}".rstrip(": ")
        self.journal.append(
            "plugin-veto",
            stage=ctx.stage,
            action=veto.action,
            plugin=veto.plugin_id,
            reason=veto.reason,
            story_key=task.story_key,
        )
        if veto.action == "pause":
            task.phase = Phase.ESCALATED  # deliberate: veto stage may have no legal advance
            self.journal.append("story-escalated", story_key=task.story_key, reason=msg)
            gates.notify(
                self.policy,
                self.run_dir,
                f"CRITICAL escalation: {task.story_key}",
                f"{msg} — resolve, then `bmad-loop resume {self.state.run_id}`",
            )
            self._save()
            raise RunPaused(msg, PAUSE_ESCALATION, task.story_key)
        task.defer_reason = msg
        task.phase = Phase.DEFERRED  # deliberate set; the veto stage may have no legal advance
        if veto.action == "defer":
            self.journal.append("story-deferred", story_key=task.story_key, reason=msg)
            gates.notify(self.policy, self.run_dir, f"story deferred: {task.story_key}", msg)
        else:  # skip: retire quietly, no human notification
            self.journal.append("story-skipped", story_key=task.story_key, reason=msg)
        self._save()
        return True

    def _emit_run_boundary(self, stage: str) -> None:
        """Fire a run-level stage (no task). A ``pause`` veto raises RunPaused so
        the run records as paused; ``defer``/``skip`` have no per-unit target here
        and are advisory (the bus already journalled them)."""
        ctx = self._emit(stage)
        if ctx is None:
            return
        veto = ctx.resolved_veto()
        if veto is not None and veto.action == "pause":
            raise RunPaused(
                f"plugin {veto.plugin_id!r} vetoed {stage}: {veto.reason}".rstrip(": "),
                PAUSE_ESCALATION,
                None,
            )

    def _emit_session_gate(
        self, task: StoryTask, role: str, prompt: str, env: dict[str, str], session_stage: str
    ) -> tuple[str, dict[str, str], HookContext | None]:
        """Fire the role-specific then generic session hooks before a session
        launches, sharing one context so the generic ``pre_session`` sees the
        role hook's mutations. Returns the (possibly rewritten) prompt + env and
        the context (None on the fast path). A veto is left on the context for
        the caller to turn into a synthesized ``vetoed`` SessionResult."""
        if not (self._bus.active(session_stage) or self._bus.active("pre_session")):
            return prompt, env, None
        ctx = self._make_context(
            "pre_session", task, role=role, proposed_prompt=prompt, proposed_env=dict(env)
        )
        # role-specific stage first (its mutations are visible to pre_session)
        ctx._stage = session_stage
        self._bus.emit(session_stage, ctx)
        ctx._stage = "pre_session"
        self._bus.emit("pre_session", ctx)
        if ctx.proposed_prompt is not None:
            prompt = ctx.proposed_prompt
        if ctx.proposed_env:
            env = dict(ctx.proposed_env)
        return prompt, env, ctx

    def _run_workflows(self, stage: str, task: StoryTask, seq: int) -> bool:
        """Run every plugin-provided workflow bound to ``stage`` as an extra agent
        session through the generic ``_run_session`` path — the conservative form
        of custom orchestration (no new pipeline stage; an injected session in the
        unit's live worktree). Returns True iff a *blocking* workflow's session
        did not complete and the unit was therefore deferred (the caller must stop
        driving it). O(1) no-op when no active plugin provides a workflow here, so
        a workflow-free run stays byte-identical.

        A workflow session is just another session: it fires ``pre_workflow_session``
        + ``pre_session`` + ``post_session`` and is recorded on the task like any
        other, so token budgets and the transcript trail account for it."""
        if stage not in self._workflow_stages:
            return False
        for lp, wf in self._registry.workflows_for(stage):
            prompt = (
                lp.manifest.render(wf.prompt)
                .replace("{story_key}", task.story_key)
                .replace("{run_id}", self.state.run_id)
            )
            self.journal.append(
                "workflow-start",
                plugin=lp.name,
                workflow=wf.name,
                stage=stage,
                role=wf.role,
                story_key=task.story_key,
            )
            result = self._run_session(
                task,
                role=wf.role,
                prompt=prompt,
                seq=seq,
                session_stage="pre_workflow_session",
                label=f"{lp.name}.{wf.name}",
            )
            self.journal.append(
                "workflow-end",
                plugin=lp.name,
                workflow=wf.name,
                status=result.status,
                story_key=task.story_key,
            )
            if wf.blocking and result.status != "completed":
                self._defer(
                    task,
                    f"blocking workflow {wf.name!r} ({lp.name}) did not complete: {result.status}",
                )
                return True
        return False

    def _run_story(self, task: StoryTask) -> None:
        ctx = self._emit("pre_story", task)
        if self._vetoed(ctx, task):
            return
        if self._isolated:
            self._run_isolated(task, self._drive_story)
        else:
            # in-place (non-isolated) ready gate: a plugin (e.g. a shared-mode
            # Unity engine) needs the live Editor up before any session starts.
            # The per_worktree gate runs inside _run_isolated, after that
            # worktree's own Editor has launched.
            ctx = self._emit("pre_ready_gate", task)
            if self._vetoed(ctx, task):
                return
            self._emit("post_ready_gate", task)
            self._drive_story(task)
        self._emit("post_story", task)

    def _drive_story(self, task: StoryTask, dev_resume: SessionResult | None = None) -> None:
        if not self._dev_phase(task, resume_result=dev_resume):
            return
        if gates.pause_after_spec(self.policy):
            gates.notify(
                self.policy,
                self.run_dir,
                f"spec ready for approval: {task.story_key}",
                f"review {task.spec_file}, then `bmad-loop resume {self.state.run_id}`",
            )
            raise RunPaused(
                f"awaiting spec approval for {task.story_key}",
                PAUSE_SPEC_APPROVAL,
                task.story_key,
            )
        self._review_and_commit(task)

    def _dev_phase(self, task: StoryTask, resume_result: SessionResult | None = None) -> bool:
        if self._vetoed(self._emit("pre_dev_phase", task), task):
            return False
        if resume_result is None:
            task.baseline_commit = verify.rev_parse_head(self.workspace.root)
            # snapshot untracked files now so a later rollback removes only what
            # THIS attempt creates, never files the user already had on disk.
            # A resumed result keeps the persisted baseline: re-capturing here
            # would shift the rollback/squash reference onto the completed
            # session's own tree.
            task.baseline_untracked = sorted(verify.untracked_files(self.workspace.root))
        feedback: Path | None = None
        while True:
            if resume_result is None:
                # a resumed result replays the attempt it was recorded under, so
                # the counter (and the session task_id derived from it) must not
                # advance; a second host death then still finds the record and
                # re-enters this continuation instead of falling back to restart.
                task.attempt += 1
            advance(task, Phase.DEV_RUNNING)
            self._save()
            if resume_result is not None:
                # the session already ran before the host died; its recorded
                # result re-enters the verify/decide pipeline. Consumed exactly
                # once — later iterations run sessions normally.
                result = resume_result
                resume_result = None
            else:
                result = self._run_session(
                    task,
                    role="dev",
                    prompt=self._dev_prompt(task, feedback),
                    seq=task.attempt,
                )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            if result.status == "completed":
                # bmad-dev-auto sometimes finalizes the spec in prose (## Auto Run
                # Result: Status done) but leaves the frontmatter status at the
                # template default. Repair it BEFORE any frontmatter reader runs —
                # the sync below, verify_dev, and the review-verify gate all key
                # off the on-disk frontmatter status.
                self._reconcile_generic_terminal_status(task, result.result_json)
                # generic-path single-writer for the bookkeeping the decoupled
                # skill never touches (sprint-status for stories, the deferred-work
                # ledger for sweep bundles), before verify reads that state.
                self._post_dev_state_sync(task, result.result_json)
                # carry the skill's follow-up-review recommendation (PR #2505)
                # onto the task so _review_and_commit can gate the review loop.
                task.followup_review_recommended = bool(
                    (result.result_json or {}).get("followup_review_recommended", False)
                )
                outcome = self._verify_dev_artifacts(task, result.result_json)
                if outcome.ok:
                    # deterministic gates run here too: a broken build must not
                    # reach the (far more expensive) review loop
                    outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
            self._emit(
                "post_dev_verify",
                task,
                session_status=result.status,
                result_json=result.result_json,
                verify_reason=(outcome.reason if outcome is not None else None),
            )
            decision = decide_dev(task, result, outcome, self.policy)
            self.journal.append(
                "dev-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                action=str(decision.action),
                reason=decision.reason,
            )
            self._save()
            if decision.action == Action.PROCEED:
                self._emit("post_dev_phase", task)
                if self._run_workflows("post_dev_phase", task, task.attempt):
                    return False
                return True
            if decision.action == Action.RETRY:
                if outcome is not None and outcome.fixable:
                    # work exists and the failure is concrete: keep the tree,
                    # hand the failing output to a repair session
                    feedback = self._write_feedback(task, decision.reason)
                else:
                    feedback = None
                    self._rollback_or_pause(task)
                continue
            if decision.action == Action.DEFER:
                self._record_dev_spec(task, result.result_json)
                self._defer(task, decision.reason)
                return False
            self._record_dev_spec(task, result.result_json)
            self._escalate(task, decision.reason)

    def _record_dev_spec(self, task: StoryTask, result_json: dict | None) -> None:
        """Capture the spec the dev session produced when the session escalates or
        defers. ``verify_dev`` only records ``task.spec_file`` on full success, so
        a blocked/escalated spec (the common escalation case) would otherwise leave
        it unset — and then escalation resolution (``runs.rearm_escalation`` flips
        the spec's frontmatter status to ``ready-for-dev``) and deferral stashing
        have no spec path to act on, so the re-drive HALTs on the stale ``blocked``
        status. The synthesized result names the spec even on a HALT
        (``devcontract.synthesize_result``). No-op once set or when the claimed
        spec is absent."""
        if task.spec_file:
            return
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if spec_path.is_file():
            task.spec_file = str(spec_path)

    def _review_and_commit(
        self, task: StoryTask, resume_result: SessionResult | None = None
    ) -> None:
        if not self.policy.review.enabled:
            # review.enabled = false: the bmad-dev-auto session's own inline
            # review is the only review; verify the deterministic gates + commit.
            self._skip_review_and_commit(task)
            return
        # review.enabled = true (default): run a follow-up review session by
        # re-invoking bmad-dev-auto on the done spec (BMAD-METHOD #2508 routes a
        # `done` spec to a fresh step-04 review pass). The dev session self-
        # finalizes the spec to done (no in-review handoff) and the orchestrator
        # advances sprint-status at dev time (_post_dev_state_sync), so this runs
        # as an independent second-opinion pass on a done spec before commit.
        #
        # review.trigger = "recommended" (default) gates that loop per-story on the
        # bmad-dev-auto session's `followup_review_recommended` signal (PR #2505):
        # the skill already self-reviews inline every story and only recommends an
        # independent pass when its review-driven changes were significant. When it
        # didn't, skip the separate session and let the deterministic gates +
        # commit run (_skip_review_and_commit still validates them). "always"
        # keeps the pre-#2505 behavior of reviewing every story. Either way the
        # loop below is bounded by limits.max_review_cycles — the oscillation guard
        # for orchestrator-applied follow-up review.
        if self.policy.review.trigger == "recommended" and not task.followup_review_recommended:
            self.journal.append("review-not-recommended", story_key=task.story_key)
            self._skip_review_and_commit(task)
            return
        if self._vetoed(self._emit("pre_review_phase", task), task):
            return
        clean = False
        # Tracks whether the last *completed* review pass left the story finalized
        # (status: done) while still recommending an independent follow-up — the
        # only state the budget-exhaustion rescue below is allowed to commit.
        refileable_followup = False
        while task.review_cycle < self.policy.limits.max_review_cycles:
            if resume_result is None:
                # a resumed result replays the cycle it was recorded under: the
                # counter must not advance, or the replay burns a review-budget
                # slot and mislabels its journal/session ids.
                task.review_cycle += 1
            refileable_followup = False  # only a completed pass this cycle can set it
            advance(task, Phase.REVIEW_RUNNING)
            self._save()
            if resume_result is not None:
                # the session already ran before the host died; its recorded
                # result re-enters the decision pipeline. Consumed exactly
                # once — later cycles run sessions normally.
                result = resume_result
                resume_result = None
            else:
                result = self._run_session(
                    task,
                    role="review",
                    prompt=self._review_prompt(task),
                    seq=task.review_cycle,
                )
            advance(task, Phase.REVIEW_VERIFY)
            self._save()
            self._emit(
                "post_review_session",
                task,
                role="review",
                session_status=result.status,
                result_json=result.result_json,
            )
            decision = decide_review_session(task, result, self.policy)
            if decision.action == Action.PAUSE:
                self._escalate(task, decision.reason)
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return
            if decision.action == Action.RETRY:
                self.journal.append(
                    "review-retry", story_key=task.story_key, reason=decision.reason
                )
                continue

            rj = result.result_json or {}
            for pref in preference_escalations(rj):
                self.journal.append("preference-escalation", story_key=task.story_key, **pref)
            # A review pass is itself a bmad-dev-auto run: it produces a spec
            # (status done/blocked + a refreshed followup_review_recommended),
            # not a result.json with `clean`. devcontract synthesizes that for us.
            # Convergence = the pass finished `done` and no longer recommends an
            # independent follow-up. A blocked pass is already handled above
            # (decide_review_session PAUSEs on its synthesized CRITICAL).
            status = str(rj.get("status", "")).strip()
            followup = bool(rj.get("followup_review_recommended", False))
            task.followup_review_recommended = followup  # latest pass wins
            refileable_followup = status == "done" and followup
            self.journal.append(
                "review-result",
                story_key=task.story_key,
                cycle=task.review_cycle,
                status=status,
                followup_review_recommended=followup,
            )
            self._emit("post_review_result", task, role="review", result_json=rj)
            if self._run_workflows("post_review_result", task, task.review_cycle):
                return
            if status == "done" and not followup:
                outcome = self._verify_review(task)
                if outcome.ok:
                    clean = True
                    break
                self.journal.append(
                    "review-verify-failed",
                    story_key=task.story_key,
                    reason=outcome.reason,
                )
                if outcome.fixable and task.review_cycle < self.policy.limits.max_review_cycles:
                    # failing verify commands are dev work, not review work: a
                    # re-review of the same tree cannot make them pass. Repair
                    # with the failing output as feedback, then re-review.
                    if not self._fix_phase(task, outcome.reason):
                        self._defer(task, "verify commands kept failing after clean review")
                        return
                continue
            # still recommends a follow-up (or a non-terminal status): loop runs a
            # fresh review pass on the newly-patched tree, bounded by max_review_cycles

        if not clean:
            # Budget exhausted. Before discarding work, distinguish two modes:
            #   (a) the last *completed* pass left the story finalized + verify-green
            #       (status: done) but kept recommending an independent follow-up
            #       (`refileable_followup`, `clean` stays False). That work is
            #       committable — commit it and re-file the lingering follow-up as a
            #       fresh deferred-work entry instead of rolling everything back (the
            #       failure mode that silently threw away review-passing work).
            #   (b) anything else (non-terminal status, no outstanding follow-up,
            #       verify failing): a genuine failure → defer + roll back as before.
            # A failed *final* review session never reaches here at all: with the
            # budget spent, decide_review_session returns DEFER (not RETRY), so the
            # loop above already deferred — a RETRY only ever loops again. The
            # rescue therefore requires both `refileable_followup` (the last
            # completed pass's own signal) AND _verify_review — the same authoritative
            # gate the converged path uses (frontmatter status==done AND sprint==done
            # AND verify commands pass) — so it can never ship uncompleted work, nor
            # re-file a follow-up the last pass did not actually recommend. Only for
            # the non-isolated path: in worktree isolation a defer already keeps the
            # unit's worktree + patch (no work is lost), so there is nothing to
            # rescue and committing into the main repo would be wrong.
            if refileable_followup and not self._isolated and self._verify_review(task).ok:
                self._record_review_budget_followup(task)
                self._commit(task)
                return
            self._defer(
                task, "review did not converge within budget (still recommending a follow-up pass)"
            )
            return

        self._commit(task)

    def _skip_review_and_commit(self, task: StoryTask) -> None:
        """review.enabled = false: no separate review session runs. The
        bmad-dev-auto session ran its own inline review and finalized the
        story to done. Validate the deterministic gates (verify commands,
        spec/sprint = done) and commit, repairing once if verify is fixable."""
        self.journal.append("review-skipped", story_key=task.story_key)
        outcome = self._verify_review(task)
        if not outcome.ok and outcome.fixable and self._fix_phase(task, outcome.reason):
            outcome = self._verify_review(task)
        if not outcome.ok:
            self._defer(task, f"verify failed with review disabled: {outcome.reason}")
            return
        self._commit(task)

    def _commit(self, task: StoryTask) -> None:
        # pre_commit_gate: the unconditional workflow-injection point before a
        # commit, on every path here (review-converged, skip-review, and the
        # review-budget rescue) — unlike post_review_result, which fires only
        # when the orchestrator review loop runs. Gate sessions (e.g. TEA's
        # trace/nfr/review) evaluate the exact tree about to commit and write
        # the artifacts the pre_commit hook then enforces on. Placed BEFORE
        # advance(COMMITTING): the task is still DEV_VERIFY / REVIEW_VERIFY,
        # both of which may legally defer, so a blocking gate whose session
        # does not complete can unwind cleanly (COMMITTING cannot defer).
        if self._run_workflows("pre_commit_gate", task, task.review_cycle):
            return
        advance(task, Phase.COMMITTING)
        self._save()
        message = self._commit_message(task)
        # pre_commit: a plugin may rewrite the commit message or escalate (pause).
        # A defer/skip veto would have to unwind a COMMITTING task (no legal move
        # to DEFERRED), so only pause is honored here — _escalate sets ESCALATED
        # directly, which COMMITTING does allow.
        ctx = self._emit("pre_commit", task, proposed_commit_message=message)
        if ctx is not None:
            veto = ctx.resolved_veto()
            if veto is not None and veto.action == "pause":
                self._escalate(task, f"plugin {veto.plugin_id!r} vetoed pre_commit: {veto.reason}")
            if ctx.proposed_commit_message:
                message = ctx.proposed_commit_message
        try:
            # bmad-dev-auto commits its own work each iteration; the orchestrator
            # squashes that chain plus its uncommitted bookkeeping back onto the
            # pre-dev baseline as one commit carrying `message`. None means there
            # was nothing to finalize (NO_VCS, or the tree already at baseline).
            sha = verify.finalize_commit(self.workspace.root, task.baseline_commit, message)
            task.commit_sha = sha or task.baseline_commit
            # the corrected spec is now durable in HEAD; later attempts need no
            # special preservation, so drop the re-drive latch.
            task.resolved_redrive = False
        except verify.GitError as e:
            self._escalate(task, f"commit failed: {e}")
        advance(task, Phase.DONE)
        self.journal.append("story-done", story_key=task.story_key, commit=task.commit_sha)
        self._emit("post_commit", task)
        self._save()
        weighted = task.tokens.weighted_total(self.policy.limits.cache_read_weight)
        if weighted > self.policy.limits.max_tokens_per_story:
            self.journal.append(
                "token-budget-exceeded",
                story_key=task.story_key,
                weighted=weighted,
                total=task.tokens.total,
            )

    # ----------------------------------------------------- override seams
    # SweepEngine reuses the dev/review pipeline for deferred-work bundles by
    # overriding these (bundles have no sprint-status entry).

    def _generic_dev(self) -> bool:
        """True when the orchestrator is driving the decoupled `bmad-dev-auto`
        dev skill — currently the only supported dev skill, so always True. Kept
        as the predicate the decoupled-path seams (B2/B4/B6/B7) read through, so
        a future alternative dev skill can re-introduce the legacy branch."""
        return self.policy.dev.skill == "bmad-dev-auto"

    def _dev_review_enabled(self) -> bool:
        """Spec-status/sprint semantics for verify_dev and the sprint sync. The
        generic skill always self-finalizes to ``done`` (no in-review handoff), so
        its dev artifacts are verified as the review-disabled case regardless of
        whether a B3 deep review will later run; the legacy skill follows
        ``policy.review.enabled``."""
        if self._generic_dev():
            return False
        return self.policy.review.enabled

    def _reconcile_generic_terminal_status(self, task: StoryTask, result_json: dict | None) -> None:
        """Repair a generic-skill spec the session finalized in prose but not in
        frontmatter. ``bmad-dev-auto`` sometimes appends a terminal
        ``## Auto Run Result`` (``Status: done``) yet leaves the frontmatter
        ``status`` at the template default. The orchestrator reads ONLY
        frontmatter, so without this the sprint/ledger sync no-ops and the verify
        gate falsely defers completed, tested work.

        When (and only when) the prose terminal Status is ``done`` AND the
        frontmatter sits at a reconcilable non-terminal status, advance the
        frontmatter to the success status the skill should have set. This includes
        the transient ``in-review`` marker, which on the generic path is never a
        deliberate terminal (the legacy review-handoff fork is retired). Never
        reconciles ``blocked`` (it must still route to PAUSE) and never overrides
        an already-``done`` or unknown frontmatter status. Idempotent and
        never-regress: every deterministic verify gate still runs afterward against
        real on-disk/git state, so this repairs bookkeeping only — it cannot pass
        uncompleted work. Runs ahead of ``_post_dev_state_sync`` so both the story
        (sprint) and bundle (ledger) sync, then verify, read the reconciled spec."""
        if not self._generic_dev():
            return
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if not spec_path.is_file():
            return
        # Refuse to mutate a spec the session reported outside the orchestrator-owned
        # roots — reconcile is the only write keyed off a session-supplied path.
        if not verify.spec_within_roots(spec_path, self.workspace.paths):
            self.journal.append(
                "spec-reconcile-skipped-out-of-tree",
                story_key=task.story_key,
                spec=str(spec_path),
            )
            return
        success_status = "in-review" if self._dev_review_enabled() else "done"
        # A YAML-null status (bare `status:` / `status: null`) reads as the string
        # "none" through verify.status_of (str(None)), which would dodge the
        # RECONCILABLE_FROM allowlist; normalize it (and a missing key) to "" so the
        # blank-status case reconciles. A literal `status: none` stays "none".
        fm = verify.read_frontmatter(spec_path)
        raw_status = fm.get("status")
        fm_status = "" if raw_status is None else str(raw_status).strip().lower()
        if fm_status == success_status:
            return  # already finalized — idempotent
        if fm_status not in devcontract.RECONCILABLE_FROM:
            return  # blocked / unknown custom status: never override a deliberate one
        arr = devcontract.parse_auto_run_result(spec_path.read_text(encoding="utf-8"))
        if not arr.present or arr.status != devcontract.DONE:
            return  # no terminal prose, or a blocked outcome: leave for the escalation path
        if not devcontract.reset_spec_status(spec_path, success_status):
            return
        # Keep the in-place result_json the rest of _dev_phase reads consistent with
        # the now-reconciled spec (the followup flag is only carried on a done exit).
        if isinstance(result_json, dict):
            result_json["status"] = success_status
            if success_status == "done":
                result_json["followup_review_recommended"] = bool(
                    verify.read_frontmatter(spec_path).get("followup_review_recommended", False)
                )
        self.journal.append(
            "spec-status-reconciled",
            story_key=task.story_key,
            spec=str(spec_path),
            frm=fm_status,
            to=success_status,
        )

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """Single-writer for the on-disk bookkeeping the generic skill never touches.

        For a story that is sprint-status: the decoupled ``bmad-dev-auto`` skill
        knows nothing of the bmad_loop's sprint board, so the orchestrator writes
        it — and must do so
        before ``verify_dev`` checks the sprint stage. Mirrors ``verify_dev``:
        advance the story to the sprint stage matching the spec status the skill
        actually reached, so a failed or blocked session (spec not at the success
        status) never advances the sprint. No-op for the legacy path; SweepEngine
        overrides this to flip the deferred-work ledger instead (bundles carry no
        sprint-status entry)."""
        if not self._generic_dev():
            return
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if not spec_path.is_file():
            return
        review_enabled = self._dev_review_enabled()  # always False for the generic path
        success_status = "in-review" if review_enabled else "done"
        status = verify.status_of(verify.read_frontmatter(spec_path))
        if status != success_status:
            return
        target = "review" if review_enabled else "done"
        sprint_advance(self.workspace.paths.sprint_status, task.story_key, target)

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev(
            task, self.workspace.paths, result_json, review_enabled=self._dev_review_enabled()
        )

    def _verify_review(self, task: StoryTask):
        return verify.verify_review(task, self.workspace.paths, self.policy)

    def _review_prompt(self, task: StoryTask) -> str:
        # Re-invoking bmad-dev-auto on a `done` spec resets review_loop_iteration
        # and routes to step-04 for a fresh independent review pass (BMAD-METHOD
        # #2508) — so the follow-up review is just another dev-skill run, no
        # separate review skill. task.spec_file is set by verify_dev on success.
        # The ledger instruction is the prevention side of the reclose in
        # SweepEngine._verify_review: a review that rewrites deferred-work.md
        # from a stale snapshot clobbers orchestrator-recorded closures. The
        # ledger is append-only for sessions — new findings are fine, existing
        # entries are orchestrator-owned.
        return (
            f"/bmad-dev-auto {task.spec_file} — If this review defers new "
            f"findings, append them to the deferred-work ledger as NEW entries "
            f"only; do NOT modify, re-open, or rewrite existing ledger entries — "
            f"the orchestrator owns their status and resolution."
        )

    def _render_commit_template(self, task: StoryTask) -> str | None:
        """The configured commit message template with {story_key}/{run_id}
        substituted, or None when no template is set. Used by both the story and
        sweep-bundle commit paths so a filled-out template wins everywhere."""
        template = self.policy.scm.commit_message_template.strip()
        if not template:
            return None
        # literal substitution (not str.format) so stray braces in the
        # template — e.g. a JSON trailer — don't raise.
        return template.replace("{story_key}", task.story_key).replace(
            "{run_id}", self.state.run_id
        )

    def _commit_message(self, task: StoryTask) -> str:
        rendered = self._render_commit_template(task)
        if rendered is not None:
            return rendered
        if self.policy.review.enabled:
            return f"story {task.story_key}: implemented and reviewed via bmad-loop"
        return f"story {task.story_key}: implemented via bmad-loop"

    # ------------------------------------------------------------- helpers

    def _run_session(
        self,
        task: StoryTask,
        role: str,
        prompt: str,
        seq: int,
        session_stage: str | None = None,
        label: str | None = None,
    ) -> SessionResult:
        # ``label`` names a non-standard session (a plugin-provided workflow) so
        # its task_id stays distinct from the role's own dev/review attempts.
        task_id = f"{task.story_key}-{label or role}-{seq}"
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        env = {
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(self.run_dir),
            "BMAD_LOOP_TASK_ID": task_id,
            "BMAD_LOOP_STORY_KEY": task.story_key,
        }
        if task.dw_ids:
            # Deferred-work bundle: the orchestrator owns the bundle→dw-id binding
            # (the generic bmad-dev-auto primitive knows nothing of dw ids). Export
            # them so the generic adapter can stamp them onto the synthesized
            # result.json, keeping verify_dev_bundle's dw_ids cross-check live.
            env["BMAD_LOOP_DW_IDS"] = ",".join(task.dw_ids)
        if role == "dev" and not self.policy.review.enabled:
            # signals that the orchestrator will run no follow-up review session.
            # bmad-dev-auto always self-reviews inline (step-03 → step-04) and
            # commits regardless, so this is a no-op for it; kept for any future
            # dev skill that honors a skip-review mode (cf. the legacy seam).
            env["BMAD_LOOP_SKIP_REVIEW"] = "1"
        # plugin session hooks: a role-specific stage (pre_dev_session / fix /
        # migrate / ...) then the generic pre_session, both able to rewrite the
        # prompt + env or veto the session. A veto synthesizes a `vetoed` result
        # so the existing decide_dev/decide_review_session route it (retry → defer).
        prompt, env, sctx = self._emit_session_gate(
            task, role, prompt, env, session_stage or f"pre_{role}_session"
        )
        if sctx is not None:
            veto = sctx.resolved_veto()
            if veto is not None:
                self.journal.append(
                    "plugin-veto",
                    stage=sctx.stage,
                    action=veto.action,
                    plugin=veto.plugin_id,
                    reason=veto.reason,
                    task_id=task_id,
                    role=role,
                )
                return SessionResult(status="vetoed")
        if label is not None:
            # Injected workflow session: spell out the completion-marker protocol
            # and bound its stall nudges (see WORKFLOW_COMPLETION_CONTRACT).
            # Appended after the session-gate hooks so a pre_workflow_session /
            # pre_session prompt rewrite cannot strip it. The marker path lands in
            # the same implementation-artifacts dir the dev adapter already
            # searches — correct in place and under worktree isolation alike,
            # because spec.cwd is self.workspace.root either way.
            marker_path = (
                self.workspace.paths.implementation_artifacts / f"bmad-dev-auto-result-{task_id}.md"
            )
            prompt += WORKFLOW_COMPLETION_CONTRACT.format(marker_path=marker_path)
        spec = SessionSpec(
            task_id=task_id,
            role=role,
            prompt=prompt,
            cwd=self.workspace.root,
            env=env,
            model=cfg.model,
            timeout_s=self.policy.limits.session_timeout_min * 60,
            stall_nudges_cap=(
                self.policy.limits.workflow_stall_nudges_cap if label is not None else None
            ),
        )
        self.journal.set_active_log(task_id)
        self.journal.append("session-start", task_id=task_id, role=role, prompt=prompt)
        result = adapter.run(spec)
        task.record_session(
            SessionRecord(
                task_id=task_id,
                role=role,
                status=result.status,
                session_id=result.session_id,
                transcript_path=result.transcript_path,
                result_json=result.result_json,
            )
        )
        # Make the completed session durable before the usage read, post-session
        # hooks, and follow-up verification. If the host kills the process in
        # that window, the resume path can see the session instead of a stale
        # dev-running task with no evidence; usage stays best-effort metadata,
        # not a durability gate.
        self._save()
        usage = adapter.read_usage(result)
        task.attach_session_usage(task_id, usage)
        self.journal.append(
            "session-end",
            task_id=task_id,
            status=result.status,
            tokens=usage.total if usage else None,
        )
        self._save()
        self._emit(
            "post_session",
            task,
            role=role,
            session_status=result.status,
            result_json=result.result_json,
        )
        return result

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._generic_dev_prompt(task, feedback)

    def _generic_dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Invocation for the generic `bmad-dev-auto` dev skill, which has no
        `--feedback` flag: feedback is inlined as freeform intent pointing at the
        existing spec. On a repair re-invocation the spec is first re-opened
        (status → `in-progress`) so the skill's step-01 re-enters implement/review
        on it rather than ingesting a finalized spec as mere context."""
        if feedback is None:
            return f"/bmad-dev-auto {task.story_key}"
        self._reset_spec_for_repair(task)
        spec_ref = task.spec_file or task.story_key
        return (
            f"/bmad-dev-auto Resume the autonomous dev session on the in-progress "
            f"spec at `{spec_ref}`. The previous session's work failed deterministic "
            f"verification; repair the working tree so verification passes without "
            f"changing the spec's frozen intent contract. Verification evidence is "
            f"in `{feedback}`."
        )

    def _reset_spec_for_repair(self, task: StoryTask) -> None:
        """Re-open a generic-skill spec before a repair re-invocation. bmad-dev-auto
        self-finalizes to `done` (or `in-review`); its step-01 routes such a spec to
        "ingest as context, do not resume," so a repair must flip the frontmatter
        `status` back to `in-progress` to re-enter implement/review in place against
        the frozen intent contract. No-op when no spec is recorded yet (the prompt
        then falls back to the story key). The stale terminal section is stripped
        too: `find_result_artifact` keys on its heading, so leaving it would let
        the re-driven session's first save of the spec read as a terminal result."""
        if not task.spec_file:
            return
        spec_path = Path(task.spec_file)
        devcontract.reset_spec_status(spec_path, "in-progress")
        devcontract.strip_auto_run_result(spec_path)

    def _write_feedback(self, task: StoryTask, reason: str) -> Path:
        """Persist a verification failure where the next session can read it —
        deterministic evidence must reach the LLM, not just the journal."""
        path = self.run_dir / "feedback" / f"{task.story_key}-{len(task.sessions)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Verification feedback: {task.story_key}\n\n"
            "The previous session's work failed deterministic verification.\n"
            "Repair the working tree so verification passes, without violating\n"
            "the spec's frozen intent.\n\n"
            f"```\n{reason}\n```\n",
            encoding="utf-8",
        )
        return path

    def _fix_phase(self, task: StoryTask, reason: str) -> bool:
        """Feedback-driven repair after a clean review whose verify commands
        failed. Consumes the story's dev-attempt budget; returns True once the
        commands pass so the review loop can re-review the repaired tree."""
        while task.attempt < self.policy.limits.max_dev_attempts:
            task.attempt += 1
            feedback = self._write_feedback(task, reason)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
                session_stage="pre_fix_session",
            )
            advance(task, Phase.DEV_VERIFY)
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from fix session: {details}")
            outcome = None
            if result.status == "completed":
                outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
                if not outcome.ok:
                    reason = outcome.reason
            ok = outcome is not None and outcome.ok
            self.journal.append(
                "fix-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                ok=ok,
            )
            self._save()
            if ok:
                return True
        return False

    def _record_review_budget_followup(self, task: StoryTask) -> None:
        """The review loop exhausted its budget on a *finalized, verify-green*
        story that the pass kept recommending a follow-up for. The work is being
        committed (not rolled back); preserve the lingering recommendation as a
        new open deferred-work entry so a later, deliberate review can pick it up,
        and notify the human. Called immediately before ``_commit`` so the ledger
        edit is squashed into the same commit.

        Re-review cap: if this story itself *originated* from such an entry (a
        sweep bundle closing a ``review-budget-followup`` id), don't re-file again
        — commit + notify only, so a second non-convergence reaches a human
        instead of slowly looping across sweeps."""
        cycles = self.policy.limits.max_review_cycles
        spec = Path(task.spec_file).name if task.spec_file else task.story_key
        ledger = self.workspace.paths.deferred_work
        reason = (
            f"Review budget ({cycles} cycles) was exhausted with the story finalized "
            f"(status: done, verify green) while the review pass kept recommending an "
            f"independent follow-up. The work was committed by bmad-loop run "
            f"{self.state.run_id}; this entry preserves the lingering follow-up "
            f"recommendation for a deliberate later review."
        )
        re_review = False
        if task.dw_ids and ledger.is_file():
            entries = {
                e.id: e for e in deferredwork.parse_ledger(ledger.read_text(encoding="utf-8"))
            }
            re_review = any(
                i in entries
                and deferredwork.field_line_present(
                    entries[i].body, "origin", "review-budget-followup"
                )
                for i in task.dw_ids
            )
        refiled: str | None = None
        if not re_review:
            refiled = deferredwork.append_entry(
                ledger,
                title=f"Follow-up review still recommended for {task.story_key} "
                f"after the review budget was exhausted",
                origin="review-budget-followup",
                source_spec=spec,
                reason=reason,
                severity="low",
            )
        self.journal.append(
            "review-budget-committed",
            story_key=task.story_key,
            cycles=cycles,
            refiled=refiled,
            re_review_capped=re_review,
        )
        note = reason
        if re_review:
            note = (
                f"{reason} This story already came from a review-budget follow-up and "
                f"still won't converge — a human should review whether the recommended "
                f"follow-up is real before sweeping it again."
            )
        gates.notify(
            self.policy,
            self.run_dir,
            f"review budget reached, work committed: {task.story_key}",
            note,
        )

    def _defer(self, task: StoryTask, reason: str) -> None:
        task.defer_reason = reason
        advance(task, Phase.DEFERRED)
        if self._isolated:
            # the failed work lives in the unit's worktree; the diff is captured
            # and the worktree kept/dropped by _integrate_unit. Don't touch the
            # tree here (no reset into the main repo — there's nothing to undo).
            self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
            gates.notify(self.policy, self.run_dir, f"story deferred: {task.story_key}", reason)
            self._save()
            return
        if task.baseline_commit:
            self._stash_deferred_artifacts(task)
            deferred_work = self.workspace.paths.deferred_work
            snapshot = (
                deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
            )
            self._rollback_or_pause(task)
            # reset reverts tracked deferred-work.md edits; restore review-found
            # defer entries — they are real knowledge worth keeping
            if snapshot is not None:
                current = (
                    deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
                )
                if current != snapshot:
                    deferred_work.parent.mkdir(parents=True, exist_ok=True)
                    deferred_work.write_text(snapshot, encoding="utf-8")
        self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"story deferred: {task.story_key}",
            reason,
        )
        self._save()

    def _stash_deferred_artifacts(self, task: StoryTask) -> None:
        """Move the deferred story's spec out of the artifacts dir into the run
        dir: a leftover in-review spec would confuse the next attempt, but the
        work in it is worth keeping for the human."""
        if not task.spec_file:
            return
        spec_path = Path(task.spec_file)
        if not spec_path.is_file():
            return
        dest = self.run_dir / "deferred" / task.story_key
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spec_path), str(dest / spec_path.name))
        self.journal.append(
            "deferred-artifacts-stashed",
            story_key=task.story_key,
            stashed_to=str(dest / spec_path.name),
        )

    def _escalate(self, task: StoryTask, reason: str) -> None:
        advance(task, Phase.ESCALATED)
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _maybe_auto_sweep(self, kind: str, trigger: str) -> None:
        """Run a child deferred-work sweep when policy [sweep].auto matches.
        The child is its own resumable run; a paused or failed child is
        journaled + notified but never interrupts this run."""
        if self.policy.sweep.auto != kind or self.sweep_factory is None:
            return
        if trigger in self.state.sweeps_triggered:
            return  # already fired before a pause/resume of this run
        self.state.sweeps_triggered.append(trigger)
        self._save()
        try:
            clean = verify.worktree_clean(self.workspace.root)
        except verify.GitError:
            clean = False
        if not clean:
            # should not happen at these call sites (everything committed or
            # reset); refuse rather than sweep on top of stray changes
            self.journal.append("sweep-auto-skipped-dirty", trigger=trigger)
            return
        self.journal.append("sweep-auto-trigger", trigger=trigger)
        try:
            self.sweep_factory(trigger)
            self.journal.append("sweep-auto-finished", trigger=trigger)
        except Exception as e:  # noqa: BLE001 — child must never break the parent
            self.journal.append("sweep-auto-failed", trigger=trigger, error=str(e))
            gates.notify(self.policy, self.run_dir, "auto sweep failed", f"{trigger}: {e}")

    def _epic_boundary(self, finished_epic: int, next_epic: int) -> None:
        self.journal.append("epic-boundary", finished=finished_epic, next=next_epic)
        self._emit("pre_epic_boundary", epic=finished_epic)
        self._maybe_auto_sweep("per-epic", f"epic-{finished_epic}")
        if self.policy.gates.retrospective != "never":
            gates.notify(
                self.policy,
                self.run_dir,
                f"epic {finished_epic} stories complete",
                "retrospective suggested: run /bmad-retrospective when convenient",
            )
        self._emit("post_epic_boundary", epic=finished_epic)
        if gates.pause_at_epic_boundary(self.policy):
            self.state.current_epic = next_epic  # don't re-trigger this gate on resume
            self._save()
            raise RunPaused(
                f"epic {finished_epic} boundary — `bmad-loop resume {self.state.run_id}` "
                f"to continue with epic {next_epic}",
                PAUSE_EPIC_BOUNDARY,
            )

    def _save(self) -> None:
        save_state(self.run_dir, self.state)
