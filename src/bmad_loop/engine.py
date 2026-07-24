"""The deterministic control loop.

Per story: dev session -> artifact verification -> bounded review loop
-> deterministic verify commands -> orchestrator commit. The engine never
edits sprint-status.yaml or spec files; it re-reads them to decide and
verify. All creative work happens inside disposable adapter sessions.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import shutil
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NoReturn, Sequence

from . import deferredwork, devcontract, envvars, gates, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec, SpecSnapshot
from .bmadconfig import ProjectPaths
from .escalation import (
    Action,
    critical_escalations,
    decide_dev,
    decide_review_session,
    env_fault_pause_reason,
    preference_escalations,
    review_retry_or_exhaust,
)
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
from .platform_util import atomic_replace, atomic_write_text, retrying_unlink, safe_segment
from .plugins import HookBus, HookContext, PluginRegistry
from .policy import Policy
from .recovery_flow import RecoveryFlow
from .runs import clear_graceful_stop, graceful_stop_requested, kill_session
from .sprintstatus import ACTIONABLE_STATUSES
from .sprintstatus import advance as sprint_advance
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable, parse_selector
from .statemachine import advance
from .workspace import UnitWorkspace, Workspace, discard_worktree, open_unit_workspace
from .worktree_flow import WorktreeFlow
from .worktree_flow import _setup_mcp_agent_id as _setup_mcp_agent_id  # re-export for tests

if TYPE_CHECKING:
    # Type-only: the worktree-provisioning helpers speak in CLI profiles.
    from .adapters.profile import CLIProfile


class RunPaused(Exception):
    def __init__(self, reason: str, stage: str, story_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.story_key = story_key


class RunStopped(Exception):
    """Raised to unwind the loop cleanly so the engine can mark the run `stopped`
    (a deliberate stop, distinct from a crash).

    Two flavors, distinguished by ``graceful``:

    - ``graceful=False`` (default) — a *hard* stop from the SIGTERM/SIGINT handler.
      The loop is interrupted mid-session, so the in-flight agent window is still
      live and must be torn down unconditionally.
    - ``graceful=True`` — a stop requested via the ``stop-request.json`` control
      file and detected at an item boundary (:meth:`Engine._check_graceful_stop`).
      The in-flight item already completed through commit, so ``run()`` runs the
      wanted subset of the clean-finish path (worktree GC + ``post_run`` +
      policy-gated session teardown) rather than a hard kill, and the run stays
      resumable."""

    def __init__(self, graceful: bool = False):
        super().__init__("graceful stop" if graceful else "stopped")
        self.graceful = graceful


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    done: int
    deferred: int
    escalated: int
    paused: bool
    paused_reason: str
    # Raw: cache reads at full weight. Kept as the historical field name.
    total_tokens: int
    # Cost-proportional: cache reads discounted by limits.cache_read_weight, the
    # same total every budget judges (#129). Deliberately has NO default — a 0
    # would render "0 weighted tokens (716M raw)", silently wrong in exactly the
    # direction this field exists to fix. There is one construction site; a
    # TypeError beats a plausible zero.
    weighted_tokens: int
    crashed: bool = False
    crash_error: str | None = None

    def render(self) -> str:
        # Lead with weighted (what spend actually costs) and name both units:
        # raw is 5-8x higher on agentic workloads, where cache reads are 80-95%
        # of the count, and an unlabeled raw figure reads as a cost overrun.
        if self.total_tokens:
            tokens = (
                f"{self.weighted_tokens:,} weighted tokens "
                f"({self.total_tokens:,} raw incl. cache reads)"
            )
        else:
            # No usage tracked at all (usage_parser = "none", or Copilot's
            # shutdown-only flush). Splitting this into "0 weighted (0 raw)"
            # would assert free work twice over; one plain zero is honest.
            tokens = "0 tokens"
        lines = [
            f"run {self.run_id}: {self.done} done, {self.deferred} deferred, "
            f"{self.escalated} escalated, {tokens}"
        ]
        if self.crashed:
            lines.append(f"CRASHED: {self.crash_error}")
        if self.paused:
            lines.append(f"PAUSED: {self.paused_reason}")
        return "\n".join(lines)


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


def _session_task_id(story_key: str, part: str, seq: int) -> str:
    """Single composition point for session task ids. Sanitize the whole
    composition, not the parts: two individually capped parts can still compose
    past a Windows filename segment limit, and ``safe_segment``'s digest suffix
    differs between the two orders. ``_resumable_session``'s resume match must
    be byte-identical to what ``_run_session`` stored, so both MUST call this."""
    return safe_segment(f"{story_key}-{part}-{seq}")


# Call-stack nesting depth for engine runs. A nested auto-sweep runs synchronously
# in the same thread as its parent (see _maybe_auto_sweep), so a ContextVar carries
# the parent's depth into the child. Tracked independently of signal ownership so an
# off-main-thread top-level run (which cannot own signals) is still seen as depth-0.
_run_depth: contextvars.ContextVar[int] = contextvars.ContextVar("bmad_loop_run_depth", default=0)


class Engine:
    # The engine that installed the process-wide stop handlers. Signal handling is
    # single-owner per process; only this engine reinstalls/restores them. Run
    # nesting is tracked separately via _run_depth (see run()).
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
        verify.configure_git_timeout(policy.limits.git_timeout_s)
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
        # Set authoritatively at run() entry from the _run_depth call-stack counter:
        # True iff this engine runs nested inside another engine's run() (a nested
        # auto-sweep). Independent of _owns_signals — a top-level run off the main
        # thread owns no signals yet is still non-nested. Defaults False pre-run.
        self._is_nested = False
        self._stopping = False
        self._prev_handlers: dict[int, object] = {}
        # Set by run()'s graceful-stop arm so the trailing notify (which fires for
        # every exit path) can word itself for a graceful stop and quote how many
        # stories a resume would still have to run. _graceful_remaining is a
        # best-effort hint (None when the estimate could not be computed).
        self._graceful_stopped = False
        self._graceful_remaining: int | None = None
        # Per-unit worktree isolation + integration flow (issue #244 F-3/F-9a).
        # Built from narrow deps + engine callbacks; the same-name Engine._* worktree
        # methods below delegate to it. `emit` is late-bound (a lambda, not the bound
        # method) so a test's monkeypatched `_emit` still wins; workspace get/set read
        # and swap the engine's live `self.workspace`; `_escalation_pause` raises
        # RunPaused for it (injected so worktree_flow need not import engine).
        self._worktree_flow = WorktreeFlow(
            paths=self.paths,
            policy=self.policy,
            state=self.state,
            journal=self.journal,
            run_dir=self.run_dir,
            registry=self._registry,
            adapters_get=lambda: self.adapters,
            open_unit_workspace=lambda *a, **k: open_unit_workspace(*a, **k),
            emit=lambda *a, **k: self._emit(*a, **k),
            save=self._save,
            gate_unit=self._gate_unit,
            escalation_pause=self._escalation_pause,
            workspace_get=lambda: self.workspace,
            workspace_set=lambda ws: setattr(self, "workspace", ws),
            on_integrated=lambda t: self._flush_pending_deferred_closes(t),
        )
        # Attempt rollback + recovery-ref preservation flow (issue #244 PR 2/2).
        # Same narrow-deps + engine-callbacks pattern as _worktree_flow: `emit` is
        # late-bound (a lambda, not the bound method) so a test's monkeypatched
        # `_emit` still wins; `workspace_get` reads the engine's worktree-swappable
        # active workspace; `escalate` routes an intent-gap restore failure through
        # the engine's escalation; `escalation_pause` raises RunPaused for it
        # (injected so recovery_flow need not import engine — that would reintroduce
        # a runtime<->engine cycle).
        self._recovery_flow = RecoveryFlow(
            paths=self.paths,
            policy=self.policy,
            state=self.state,
            journal=self.journal,
            run_dir=self.run_dir,
            workspace_get=lambda: self.workspace,
            emit=lambda *a, **k: self._emit(*a, **k),
            save=self._save,
            escalate=self._escalate,
            escalation_pause=self._escalation_pause,
        )

    def _escalation_pause(
        self, reason: str, story_key: str = "", *, cause: BaseException | None = None
    ) -> NoReturn:
        """Raise the engine's ``RunPaused`` (PAUSE_ESCALATION) on behalf of the
        worktree collaborator. Injected as a callable so ``worktree_flow`` need not
        import ``RunPaused`` — that would reintroduce a runtime<->engine cycle."""
        raise RunPaused(reason, PAUSE_ESCALATION, story_key) from cause

    # ------------------------------------------------------------- top level

    def _warn_desktop_notifier_inert(self) -> None:
        """One-time run-start alert for #231: ``notify.desktop`` is requested but
        this platform has no notifier, so every ``gates.notify`` desktop sink is a
        silent no-op. Journalled + stderr so an unattended launch that skips
        ``validate`` still surfaces it."""
        if not (self.policy.notify.desktop and gates.desktop_notifier_kind() is None):
            return
        self.journal.append("notify-desktop-unavailable", platform=sys.platform)
        # The ATTENTION file only exists as a fallback when notify.file is on; with
        # it off there is no human channel left, so point at that rather than a file
        # that is never written.
        channel = (
            f"watch the ATTENTION file in {self.run_dir}"
            if self.policy.notify.file
            else "notify.file is also off, so no alert channel is configured"
        )
        print(
            f"warning: notify.desktop is set but no desktop notifier is available "
            f"on {sys.platform}; desktop alerts are silently skipped — {channel}.",
            file=sys.stderr,
        )

    def run(self) -> RunSummary:
        # Establish call-stack nesting depth before anything else: _is_nested is read
        # by the warning gate and the stop/crash re-raise arms below. Reset in the
        # outermost finally so a nested child's re-raise still decrements the depth.
        depth = _run_depth.get()
        self._is_nested = depth > 0
        token = _run_depth.set(depth + 1)
        try:
            return self._run_inner()
        finally:
            _run_depth.reset(token)

    def _run_inner(self) -> RunSummary:
        self._install_stop_signals()
        try:
            try:
                # Warn once per top-level run before pre_run: a plugin `pause` veto in
                # _emit_run_boundary("pre_run") raises RunPaused, which would otherwise
                # skip the promised inert-notifier warning + journal event. Gated on
                # `not _is_nested` (call-stack depth), not `_owns_signals`: a top-level
                # run that could not install signal handlers (off the main thread) owns
                # no signals yet is not nested, and must still surface the warning.
                if not self._is_nested:
                    self._warn_desktop_notifier_inert()
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
            except RunStopped as stop:
                if stop.graceful:
                    # Graceful stop: the request was consumed at an item boundary
                    # (_check_graceful_stop), so the in-flight item already ran to
                    # completion through commit — nothing mid-session to kill. Run
                    # the wanted subset of the clean-finish path so a resumable
                    # `stopped` run is finalized as tidily as a finished one.
                    self.state.stopped = True
                    self._graceful_stopped = True
                    try:
                        # These run plugin code (post_run) + git worktree admin;
                        # an exception raised inside an except arm escapes run()
                        # uncaught, so guard them inline and journal the failure
                        # rather than let it mask the stop.
                        self._gc_run_worktrees()
                        self._emit("post_run")
                    except Exception as finalize_exc:  # see comment above
                        self.journal.append("run-stop-finalize-error", error=str(finalize_exc))
                    remaining = self._remaining_estimate()
                    self._graceful_remaining = remaining
                    self.journal.append("run-stop", graceful=True, remaining=remaining)
                    # Session teardown follows the same policy gate the clean-finish
                    # path uses (mirrors the finished-run branch), NOT the hard
                    # stop's unconditional kill. Deliberately NO _is_nested re-raise:
                    # a gracefully stopped child sweep is a clean completion from the
                    # parent's perspective (the parent journals sweep-auto-finished).
                    if self._owns_signals and self.policy.adapter.cleanup_session_on_finish:
                        kill_session(self.state.run_id)
                else:
                    # Hard stop: the loop was interrupted inside adapter.run(), so
                    # the agent window is still live — tear the whole run session
                    # down.
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
                ):  # nosec B110 - best-effort teardown; the stop must still record
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
                ):  # nosec B110 - best-effort teardown; a crashing run must still record
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
                ):  # nosec B110 - journal write is best-effort; crash.txt + state flag already persisted
                    pass
            finally:
                # Any pending stop-request control file that outlived this run
                # (the run finished/paused/crashed, or a hard stop superseded it,
                # before an item boundary consumed it) is discarded here so a later
                # resume does not re-honor a stale request. The graceful arm already
                # consumed its own file, so this only fires for a superseded one.
                if clear_graceful_stop(self.run_dir):
                    with contextlib.suppress(Exception):
                        self.journal.append("stop-request-discarded")
                self._save()
        finally:
            self._restore_stop_signals()
        summary = self.summary()
        if self._graceful_stopped:
            body = [summary.render()]
            if self._graceful_remaining is not None:
                stories_word = "story" if self._graceful_remaining == 1 else "stories"
                body.append(f"{self._graceful_remaining} {stories_word} remaining")
            body.append(f"resume with `bmad-loop resume {self.state.run_id}`")
            gates.notify(
                self.policy,
                self.run_dir,
                "bmad-loop run stopped gracefully",
                "\n".join(body),
            )
        else:
            gates.notify(self.policy, self.run_dir, "bmad-loop run finished", summary.render())
        return summary

    # ---------------------------------------------------------- stop signals

    def _install_stop_signals(self) -> None:
        """Make SIGTERM/SIGINT unwind the loop as a RunStopped. Only the
        outermost engine in the process owns the handlers (nested auto-sweep
        runs let the exception propagate up to it); install is best-effort and
        silently skipped off the main thread (signal.signal raises there)."""
        # Signal ownership is process-global and independent of run nesting (tracked
        # via _run_depth in run()): a non-None owner means an outer engine already
        # installed the handlers, so this engine must not reinstall them.
        if Engine._stop_signals_owner is not None:
            return

        windows_ctrl_signals = {signal.SIGINT}
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            windows_ctrl_signals.add(sigbreak)

        def handler(signum, frame):  # stdlib signal signature
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
                # prev is a prior OS handler from signal.signal(); the dict is
                # object-typed to avoid importing the private stdlib _HANDLER alias.
                signal.signal(sig, prev)  # pyright: ignore[reportArgumentType]
            except (ValueError, TypeError):
                pass
        self._prev_handlers.clear()
        if Engine._stop_signals_owner is self:
            Engine._stop_signals_owner = None
        self._owns_signals = False

    # ----------------------------------------------------- worktree isolation

    # Same-name delegators onto the WorktreeFlow collaborator (issue #244 F-3):
    # the isolation/integration cluster moved to worktree_flow.py; these keep the
    # Engine surface (and its tests + SweepEngine/StoriesEngine subclasses)
    # byte-compatible, so `self._<method>` monkeypatches and calls still resolve here.

    @property
    def _isolated(self) -> bool:
        return self._worktree_flow.isolated

    def _ensure_target_branch(self) -> None:
        self._worktree_flow.ensure_target_branch()

    def _worktree_profiles(self) -> list[CLIProfile]:
        return self._worktree_flow.worktree_profiles()

    def _engine_agent_ids(self) -> list[str]:
        return self._worktree_flow.engine_agent_ids()

    def _run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        self._worktree_flow.run_isolated(task, drive)

    def _failed_diff_max_bytes(self) -> int | None:
        return self._worktree_flow.failed_diff_max_bytes()

    def _integrate_unit(self, task: StoryTask, unit: UnitWorkspace) -> None:
        self._worktree_flow.integrate_unit(task, unit)

    def _merge_local(self, task: StoryTask, unit: UnitWorkspace) -> None:
        self._worktree_flow.merge_local(task, unit)

    def _keep_branch_and_escalate(self, task: StoryTask, unit: UnitWorkspace, reason: str) -> None:
        self._worktree_flow.keep_branch_and_escalate(task, unit, reason)

    def _escalate_unit(self, task: StoryTask, reason: str) -> None:
        self._worktree_flow.escalate_unit(task, reason)

    def _merge_message(self, task: StoryTask) -> str:
        return self._worktree_flow.merge_message(task)

    def _gc_run_worktrees(self) -> None:
        self._worktree_flow.gc_run_worktrees()

    def _reopen_unit(self, task: StoryTask) -> UnitWorkspace:
        return self._worktree_flow.reopen_unit(task)

    def summary(self) -> RunSummary:
        tasks = self.state.tasks.values()
        # Weight from the run's persisted snapshot, NOT self.policy — every
        # display surface must be reproducible from state.json alone, which is
        # all the TUI, `bmad-loop status` and `diagnose` can see; sourcing this
        # from live policy would make them print different totals for the same
        # run. Reading the snapshot is safe precisely because every engine start
        # (run, sweep, resume) stamps it with the policy that process enforces
        # (#189) — so it agrees with self.policy rather than substituting for it.
        # Do not "unify" these.
        weight = self.state.cache_read_weight()
        return RunSummary(
            run_id=self.state.run_id,
            done=sum(1 for t in tasks if t.phase == Phase.DONE),
            deferred=sum(1 for t in tasks if t.phase == Phase.DEFERRED),
            escalated=sum(1 for t in tasks if t.phase == Phase.ESCALATED),
            paused=self.state.paused,
            paused_reason=self.state.paused_reason or "",
            total_tokens=sum(t.tokens.total for t in tasks),
            # Sum PER TASK, not over one aggregated TokenUsage: weighted_total
            # rounds internally, so sum-of-rounds != round-of-sum (they drift by
            # a few tokens under banker's rounding). Per-task summation is what
            # tui/widgets.py does, which is what makes the CLI and the TUI agree
            # to the token. This is not a redundant loop — do not collapse it.
            weighted_tokens=sum(t.tokens.weighted_total(weight) for t in tasks),
            crashed=self.state.crashed,
            crash_error=self.state.crash_error,
        )

    def _remaining_estimate(self) -> int | None:
        """Best-effort count of stories a resume would still have to run, for the
        graceful-stop journal + notify. Sprint mode: actionable sprint-status
        stories this run has not already picked up (mirrors ``cmd_status``'s sprint
        backlog count, minus anything already in ``state.tasks``). It is a hint,
        never a contract — the whole body is guarded so an unreadable/invalid
        sprint-status file returns None rather than derailing a graceful stop.
        StoriesEngine overrides this against the manifest scheduler."""
        try:
            ss = load_sprint_status(self.paths.sprint_status)
            return sum(
                1
                for s in ss.stories
                if s.status in ACTIONABLE_STATUSES and s.key not in self.state.tasks
            )
        except Exception:  # a hint must never break the stop
            return None

    def _check_graceful_stop(self) -> None:
        """Honor a pending graceful-stop request at an item boundary.

        Consumes (deletes) the ``stop-request.json`` control file and raises
        :class:`RunStopped` with ``graceful=True`` so ``run()`` unwinds into the
        clean-finalization arm. An exception, not a sentinel return, because the
        sweep check fires two frames below ``_loop`` (inside ``_cycle``) where a
        return could not stop the loop. Called as the first statement of the loop
        body (and, in the sweep engine, before each bundle): by the time control
        reaches here the in-flight item has already completed through commit, so
        the stop takes effect cleanly at the next boundary and the run stays
        resumable."""
        if graceful_stop_requested(self.run_dir):
            clear_graceful_stop(self.run_dir)
            raise RunStopped(graceful=True)

    def _loop(self) -> None:
        self._finish_inflight()
        while True:
            # First statement of the loop body: one site covers every story
            # boundary this base loop reaches — between stories, right after
            # _finish_inflight on resume, and the epic boundary + run-end (the
            # StoriesEngine has no _loop override, so it is covered here too).
            self._check_graceful_stop()
            if self.max_stories is not None and self._dispatched_count() >= self.max_stories:
                self.journal.append("max-stories-reached", count=self._dispatched_count())
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
            self._run_story(task)
            self._after_story(task)

    def _dispatched_count(self) -> int:
        """Stories this run has dispatched, counted durably from run state so the
        ``--max-stories`` bound survives a pause/resume (a story checkpoint, an
        escalation) — unlike a ``_loop``-local counter that resets to 0 on every
        re-entry. Every picked story is recorded in ``state.tasks`` before its
        session runs (and a wedge/selector pause records its task too), the same
        "touched this run" set ``_pick_next`` keys ``base_skip`` on, so the task
        count is the durable dispatch tally. Without this, a checkpoint pause then
        resume would reset the counter and let the run dispatch past its cap."""
        return len(self.state.tasks)

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

    # --------------------------------------- attempt rollback / recovery refs

    # Same-name delegators onto the RecoveryFlow collaborator (issue #244 PR 2/2):
    # the rollback/preserve cluster moved to recovery_flow.py; these keep the
    # Engine surface (its tests + the SweepEngine/StoriesEngine subclasses)
    # byte-compatible, so `self._<method>` calls still resolve here.

    def _protected_relpaths(self) -> tuple[str, ...]:
        return self._recovery_flow.protected_relpaths()

    def _rollback_or_pause(self, task: StoryTask, *, cause: str = "stopped") -> None:
        self._recovery_flow.rollback_or_pause(task, cause=cause)

    def _safe_reset(self, task: StoryTask, *, preserve: tuple[str, ...] = ()) -> None:
        self._recovery_flow.safe_reset(task, preserve=preserve)

    def _restore_patch(self, task: StoryTask) -> None:
        self._recovery_flow.restore_patch(task)

    def _prune_preserve_refs(self) -> None:
        self._recovery_flow.prune_preserve_refs()

    def _preserve_attempt_commits(self, task: StoryTask, *, allow_pause: bool) -> None:
        self._recovery_flow.preserve_attempt_commits(task, allow_pause=allow_pause)

    def _preserve_attempt_worktree(self, task: StoryTask) -> None:
        self._recovery_flow.preserve_attempt_worktree(task)

    def _pause_for_manual_recovery(
        self, task: StoryTask, baseline: str, *, preserve_failed: bool = False
    ) -> None:
        self._recovery_flow.pause_for_manual_recovery(
            task, baseline, preserve_failed=preserve_failed
        )

    def _reconcile_pending_deferred_closes(self) -> None:
        """Finish external-ledger closures a previous process left parked (#234).

        These sit outside the phase machine the rest of `_finish_inflight` walks:
        the write happens after the task is already DONE (after `merge_local`
        under isolation), so a crash in that window leaves a terminal task holding
        an unsatisfied obligation — and terminal tasks are exactly what that loop
        skips. `unit_merged` is the durable proof the work landed; without it the
        unit never reached the target branch, and the ledger reading `open` is the
        truthful answer, so the obligation is reported rather than discharged.

        `unit_merged` is saved the statement after the merge itself returns, so
        the window in which a host death reports a merge that did land as
        abandoned is that save alone. It used to span `merge_local`'s whole tail —
        a journal append, the `post_merge` emit, the worktree teardown — none of
        which is evidence about whether the merge happened.

        That residual direction is deliberate: the entries stay open (which a
        sweep re-verifies against the codebase, and which `deferred-close-abandoned`
        names for the operator), where recording the intent before the merge would
        instead close entries for a merge that never happened. Proving it from the
        target branch is not available as a tiebreaker either — `squash` rewrites
        the commit, so the unit's sha is not on the target under every strategy."""
        for task in list(self.state.tasks.values()):
            if not task.pending_deferred_closes:
                continue
            if task.phase != Phase.DONE:
                continue  # still re-drivable; the commit phase applies it
            if task.unit_merged or not self._isolated:
                self._flush_pending_deferred_closes(task)
            else:
                self.journal.append(
                    "deferred-close-abandoned",
                    story_key=task.story_key,
                    dw_ids=list(task.pending_deferred_closes),
                    reason="story is done but its unit never merged; entries stay open",
                )

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        self._reconcile_pending_deferred_closes()
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            isolated = self._isolated and task.worktree_path
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                # paused at the spec-approval gate (or, in stories mode, a
                # plan-checkpoint awaiting implementation — _resume_after_dev_verify
                # dispatches the right leg): dev verified on disk.
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._resume_after_dev_verify(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._resume_after_dev_verify(task)
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
            elif task.phase == Phase.COMMITTING:
                # the host died in the commit window: the gate+advance save
                # landed (pre_commit_gate ran clean) but the DONE save that
                # stamps commit_sha did not. Finish the commit instead of
                # rolling verified work back — the gates are deliberately NOT
                # re-run (see _finalize_commit_phase), the pre_commit hook IS
                # re-emitted (message regenerated; pause veto still honored),
                # and finalize_commit tolerates both the pre- and post-squash
                # crash states (#115).
                self.journal.append("resume-commit", story_key=task.story_key)
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._finalize_commit_phase(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._finalize_commit_phase(task)
            else:
                self.journal.append(
                    "resume-restart", story_key=task.story_key, phase=str(task.phase)
                )
                if isolated:
                    # drop the half-built worktree; _run_story mounts a fresh one
                    discard_worktree(
                        self.paths.repo_root, task.worktree_path, task.branch, run_dir=self.run_dir
                    )
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
            # a resumed story that just reached DONE gets the same post-story hook
            # the _loop path fires (e.g. the stories-mode done_checkpoint pause),
            # after any worktree integration above — no-op in the base engine.
            self._after_story(task)

    def _resumable_session(self, task: StoryTask) -> tuple[str, SessionResult] | None:
        """The in-flight session's durably-recorded result, when complete enough
        to act on: the task died mid-phase (``*_RUNNING``) or in the post-verify
        decision window (``*_VERIFY`` — persisted by the save right after the
        verify/decide pass, before the decision's action completed) but its
        current attempt/cycle record is ``completed`` and carries the parsed
        result. Consumes only evidence the adapter vouched for at session end —
        no artifact re-scan, no loosening of completion authority. Anything less
        returns None and the caller falls through to resume-restart (#100: that
        restart used to discard a completed-``done`` attempt's commits).

        DEV_VERIFY reaches this matcher only when ``task.spec_file`` is empty
        (verify did not fully pass before the death): _finish_inflight checks
        the spec-approval-gate arm first, so a DEV_VERIFY task WITH a verified
        spec keeps its _resume_after_dev_verify recovery."""
        if task.phase in (Phase.DEV_RUNNING, Phase.DEV_VERIFY):
            role, seq = "dev", task.attempt
        elif task.phase in (Phase.REVIEW_RUNNING, Phase.REVIEW_VERIFY):
            role, seq = "review", task.review_cycle
        else:
            return None
        task_id = _session_task_id(task.story_key, role, seq)
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
            wf_extras: dict = {"env_fault": result.env_fault}
            if result.env_fault_evidence:
                wf_extras["env_fault_evidence"] = result.env_fault_evidence
            self.journal.append(
                "workflow-end",
                plugin=lp.name,
                workflow=wf.name,
                status=result.status,
                story_key=task.story_key,
                **wf_extras,
            )
            if wf.blocking and result.status != "completed":
                if result.env_fault:
                    # A blocking workflow session that lost its API connection (#194)
                    # never ran — escalate (re-arm restores the budget) instead of
                    # deferring the story on a transport failure. Non-blocking
                    # workflows keep continuing (journaled only): the next story
                    # session will classify and pause if the outage persists.
                    self._escalate(
                        task,
                        env_fault_pause_reason(
                            f"blocking workflow {wf.name!r} ({lp.name})", result
                        ),
                    )
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
                # intent-gap patch-restore (#2564): re-lay the saved attempt onto
                # the baseline before dispatch so the re-driven session resumes
                # review on the restored diff. `feedback is None` ⇒ the tree is at
                # baseline (fresh attempt or a non-fixable rollback below), NOT a
                # fixable-feedback retry that kept the attempt's tree — so this
                # never double-applies. No-op unless a restore is latched; escalates
                # (never dispatches) if the patch fails to apply.
                if feedback is None:
                    self._restore_patch(task)
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
                # A present key is authoritative (folded from the frontmatter, or
                # the legacy skill's own result.json); an absent one is a resumed
                # pre-reconcile snapshot whose re-fold may have been dropped by a
                # spec read fault — re-derive from the spec instead of defaulting
                # a recommended review away.
                rj = result.result_json or {}
                if "followup_review_recommended" in rj:
                    task.followup_review_recommended = bool(rj["followup_review_recommended"])
                else:
                    task.followup_review_recommended = self._followup_from_spec(task, rj)
                outcome = self._verify_dev_artifacts(task, result.result_json)
                if outcome.ok:
                    # the spec is verified and on disk: the one moment its
                    # `closes_deferred:` declaration is known readable (#234).
                    # The commit boundary re-reads it — this capture is what that
                    # read falls back on when it faults.
                    self._capture_declared_deferred(task, site="dev-verify")
                if outcome.ok and self._run_verify_commands_after_dev(task, result.result_json):
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
                # env_fault from EITHER the verify path (rc 126/127) or the
                # session-transport classification (#194); decide_dev PAUSEs on
                # the latter, so the fall-through below preserves the worktree.
                env_fault=bool((outcome is not None and outcome.env_fault) or result.env_fault),
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
        # the skill already self-reviews inline every story and recommends an
        # independent pass from a severity-weighted score over its patched
        # findings (upstream #2580). When it didn't, skip the separate session
        # and let the deterministic gates + commit run (_skip_review_and_commit
        # still validates them). "always" keeps the pre-#2505 behavior of
        # reviewing every story. Either way the loop below is bounded by
        # limits.max_review_cycles (the hard outer cap) and damped by
        # limits.max_followup_reviews — the guard against a finalized round that
        # keeps recommending its own follow-up (structural before upstream #2580
        # scored the flag; kept as the orchestrator-side bound): once the damping
        # grant is spent, such a round converges + refiles instead of burning
        # cycles to the outer cap.
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
        # The last *completed* pass's parsed frontmatter status, so the exhaustion
        # defer reason can name what actually happened instead of the fixed
        # follow-up wording (issue #160). None until a pass reaches the parse below
        # (a crash/stall that DEFERs never gets there).
        last_status: str | None = None
        # A resumed result must enter the loop even when the crash landed in the
        # post-session window of the *final* allowed cycle (review_cycle already
        # == max_review_cycles): its recorded pass was already counted, and the
        # replay branch below skips the re-increment, so no extra budget is
        # burned. resume_result is nulled after it is consumed, so every later
        # iteration falls back to the normal budget guard.
        while resume_result is not None or task.review_cycle < self.policy.limits.max_review_cycles:
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
                # Strip the prior pass's stale `## Auto Run Result` before launch:
                # the review re-invokes bmad-dev-auto on the done spec, and the
                # session's own entry write would otherwise lift that leftover
                # marker past the adapter's launch-mtime floor and end the review
                # on its first result-less Stop (issue #160). Non-replay branch
                # only — the replay path above launches no session.
                snapshot = self._reset_spec_for_review(task)
                result = self._run_session(
                    task,
                    role="review",
                    prompt=self._review_prompt(task),
                    seq=task.review_cycle,
                    spec_snapshot=snapshot,
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
            if decision.action == Action.SALVAGE:
                # review.on_timeout = "salvage-if-done" (#271): the session hit a
                # timeout-like verdict, but the dev product may already be
                # finalized and verify-green — converge (commit + refile the
                # outstanding follow-up) instead of burning another review cycle
                # on an empty delta. When salvage is not applicable, fall back
                # through the default retry/exhaust routing.
                if self._salvage_review_timeout(task, result):
                    return
                fallback = review_retry_or_exhaust(
                    task, self.policy, f"{decision.reason}; salvage not applicable"
                )
                if fallback.action == Action.PAUSE:
                    self._escalate(task, fallback.reason)
                if fallback.action == Action.DEFER:
                    self._defer(task, fallback.reason)
                    return
                self.journal.append(
                    "review-retry", story_key=task.story_key, reason=fallback.reason
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
            # A review pass can die between writing terminal prose (## Auto Run
            # Result: done) and flipping frontmatter off the transient `in-review`
            # marker. Mirror the dev leg (engine.py:1541): repair the spec BEFORE
            # reading status/followup below — otherwise the stale `in-review`
            # frontmatter burns a review cycle re-reviewing already-finished work.
            # On the generic path this advances `in-review`→`done` and re-folds the
            # frontmatter's followup flag into `rj` (only when present), so the
            # convergence/damping gate below sees the finalized state.
            self._reconcile_generic_terminal_status(task, rj)
            status = str(rj.get("status", "")).strip()
            last_status = status  # remember the last completed pass for the defer reason
            followup = bool(rj.get("followup_review_recommended", False))
            task.followup_review_recommended = followup  # latest pass wins
            refileable_followup = status == "done" and followup
            # Damping: a finalized round that still recommends its own follow-up is
            # honored only while the story has damping grants left. Once
            # followup_reviews_spent has reached limits.max_followup_reviews, such a
            # round force-converges (verify → refile the recommendation → commit)
            # instead of burning another cycle on a runaway recommendation
            # (pre-#2580, every review pass patched findings and recommended
            # another pass; the upstream severity-scored flag has since made
            # that the exception). max_review_cycles stays the hard outer bound.
            damped = refileable_followup and (
                task.followup_reviews_spent >= self.policy.limits.max_followup_reviews
            )
            self.journal.append(
                "review-result",
                story_key=task.story_key,
                cycle=task.review_cycle,
                status=status,
                followup_review_recommended=followup,
                followup_damped=damped,
            )
            self._emit("post_review_result", task, role="review", result_json=rj)
            if self._run_workflows("post_review_result", task, task.review_cycle):
                return
            if status == "done" and (not followup or damped):
                outcome = self._verify_review(task)
                if outcome.ok:
                    if damped:
                        # refile BEFORE break so the ledger edit squashes into the
                        # same story commit (mirrors the exhaustion rescue ordering).
                        # Verify-green here is the same authority as the converged /
                        # rescue paths — never ships uncompleted work.
                        self._record_review_budget_followup(task, damped=True)
                    clean = True
                    break
                self.journal.append(
                    "review-verify-failed",
                    story_key=task.story_key,
                    reason=outcome.reason,
                    env_fault=outcome.env_fault,
                )
                if not outcome.retryable:
                    # escalate-grade failure (environment fault, git error): a
                    # repair session cannot fix it and another review cycle
                    # would replay it — pause the run instead of burning budget
                    self._escalate(task, outcome.reason)
                if outcome.fixable and task.review_cycle < self.policy.limits.max_review_cycles:
                    # failing verify commands are dev work, not review work: a
                    # re-review of the same tree cannot make them pass. Repair
                    # with the failing output as feedback, then re-review. This
                    # verify-repair round never spends the damping cap.
                    if not self._fix_phase(task, outcome.reason):
                        self._defer(task, "verify commands kept failing after clean review")
                        return
                continue
            if refileable_followup:
                # Spend one damping grant for honoring this pass's own follow-up
                # recommendation. Deliberately AFTER the
                # _run_workflows("post_review_result") gate: the increment is
                # persisted only by the NEXT cycle's _save(), by which point
                # _resumable_session can no longer replay this result — so a
                # crash-replay re-derives the spend exactly once instead of
                # double-counting it. (A non-terminal status or a non-followup
                # done — the two other ways to reach here — never sets
                # refileable_followup, so neither spends the cap.)
                task.followup_reviews_spent += 1
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
            # Name the last completed pass's real outcome (issue #160): the fixed
            # follow-up wording is only correct when a finalized pass actually left
            # a refileable recommendation. "did not converge" stays in every variant
            # (callers grep it).
            if refileable_followup:
                detail = "still recommending a follow-up pass"
            elif last_status == "done":
                detail = "last review pass finalized but its verification failed"
            elif last_status is not None:
                # A pass completed but its status is non-terminal — or "" when the
                # observe-degrade paths left `rj` with no status (spec read fault,
                # out-of-tree spec, or a result.json with no spec_file so the
                # reconcile bailed). Render "" honestly rather than mislabeling it as
                # "no pass ran" (which is what `last_status is None` below means).
                detail = (
                    f"last review pass ended at non-terminal status {(last_status or 'unknown')!r}"
                )
            else:
                detail = "no review pass completed"
            self._defer(task, f"review did not converge within budget ({detail})")
            return

        self._commit(task)

    def _salvage_review_timeout(self, task: StoryTask, result: SessionResult) -> bool:
        """``review.on_timeout = "salvage-if-done"`` (#271): try to converge a
        timed-out review by committing the already-finalized dev product instead
        of burning another review cycle. Returns True when the story committed;
        False means salvage was not applicable and the caller falls back to the
        default retry/exhaust routing. The cycle the timed-out session charged is
        deliberately not refunded — salvage changes what the *next* cycle costs,
        not what this one did.

        Applicability, all deterministic: not worktree-isolated (a defer there
        already keeps the unit's worktree + diff, and committing into the main
        repo would be wrong — same scoping as the budget-exhaustion rescue); a
        spec is recorded and its frontmatter reads ``done`` (the review never got
        far enough to touch it — rare once the adapter's missing-marker fallback
        (#224) completes those sessions, but a review that never wrote the spec
        at all still lands here) or ``in-review`` (the mid-review interrupt: the
        dying pass flipped the transient marker and died — reset it forward,
        stripping any partial terminal section so the next launch's mtime-floor
        scan can't misread it). Anything else — ``blocked``, ``in-progress``, a
        custom token — was set deliberately or means unfinished dev work: never
        salvage over it. The commit is gated on the same authoritative
        ``_verify_review`` as every other converge path, so salvage can never
        ship unverified work; a timeout that produced no review result neither
        re-arms ``followup_review_recommended`` nor spends a damping grant — the
        outstanding recommendation is refiled to deferred work instead."""
        if self._isolated or not task.spec_file:
            return False
        spec_path = Path(task.spec_file)
        fm = self._observed_frontmatter(spec_path, task.story_key, "review-timeout-salvage")
        if fm is None:
            return False
        fm_status = str(fm.get("status", "")).strip().lower()
        if fm_status not in ("done", "in-review"):
            return False
        reset_from: str | None = None
        if fm_status == "in-review":
            # Repair-write doctrine: these raise on an unreadable spec rather
            # than silently proceeding stale (see _reset_spec_for_repair).
            reset_from = fm_status
            devcontract.reset_spec_status(spec_path, "done")
            devcontract.strip_auto_run_result(spec_path)
        outcome = self._verify_review(task)
        if not outcome.ok:
            self.journal.append(
                "review-timeout-salvage-failed",
                story_key=task.story_key,
                cycle=task.review_cycle,
                reason=outcome.reason,
                env_fault=outcome.env_fault,
            )
            if not outcome.retryable:
                # escalate-grade failure (environment fault, git error): another
                # review cycle would replay it — pause the run (mirrors the
                # review loop's own verify-failed routing).
                self._escalate(task, outcome.reason)
            return False
        refiled: str | None = None
        if task.followup_review_recommended:
            # Refile BEFORE _commit so the ledger edit squashes into the story
            # commit (mirrors _record_review_budget_followup's ordering). A new
            # origin string: the review-budget-followup origin's wording and
            # re-review cap are load-bearing for that path and must not blur.
            refiled = deferredwork.append_entry(
                self.workspace.paths.deferred_work,
                title=(
                    f"Follow-up review still outstanding for {task.story_key}"
                    " after a review timeout"
                ),
                origin="review-timeout-salvage",
                source_spec=spec_path.name if task.spec_file else task.story_key,
                reason=(
                    f"The review session ended {result.status} with the story already "
                    f"finalized (status: done, verify green). Per review.on_timeout = "
                    f"'salvage-if-done' the work was committed by bmad-loop run "
                    f"{self.state.run_id} without another review pass; this entry "
                    f"preserves the outstanding follow-up recommendation for a "
                    f"deliberate later review."
                ),
                severity="low",
            )
            task.followup_review_recommended = False
        self.journal.append(
            "review-timeout-salvage",
            story_key=task.story_key,
            cycle=task.review_cycle,
            session_status=result.status,
            reset_from=reset_from,
            refiled=refiled,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"review timeout salvaged, work committed: {task.story_key}",
            f"review session {result.status}; the finalized, verify-green dev product "
            f"was committed and any outstanding follow-up refiled to deferred work.",
        )
        self._commit(task)
        return True

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
            # same event kind as the review-enabled loop so journal consumers
            # see the structured env_fault flag on this path too
            self.journal.append(
                "review-verify-failed",
                story_key=task.story_key,
                reason=outcome.reason,
                env_fault=outcome.env_fault,
            )
            if not outcome.retryable:
                # escalate-grade failure (environment fault, git error): a
                # defer would just replay it on the next story — pause the run
                self._escalate(task, outcome.reason)
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
        self._finalize_commit_phase(task)

    def _finalize_commit_phase(self, task: StoryTask) -> None:
        """Drive an already-COMMITTING task to DONE: regenerate the message,
        emit ``pre_commit`` (rewrite honored; a pause veto escalates —
        COMMITTING→ESCALATED is legal), squash via ``finalize_commit``, stamp
        ``commit_sha``, advance to DONE.

        Precondition: ``task.phase == COMMITTING`` and that phase is PERSISTED
        (the gate+advance+save in ``_commit``, or a resume that found it on
        disk). The persisted phase is durable proof the ``pre_commit_gate``
        workflows already ran and passed — the COMMITTING save lands only
        after the gate loop returns clean — which is why the resume arm calls
        this WITHOUT re-running them: a re-run would double-charge the session
        budget, and a blocking failure would need an illegal
        COMMITTING→DEFERRED move (#115).

        Re-drive contract: safe to call again after a host death anywhere
        inside it. ``finalize_commit`` is content-idempotent across both crash
        states — pre-squash (skill commit chain above baseline) squashes
        normally; post-squash (squashed commit at HEAD, clean tree)
        re-squashes to an identical-content commit, orphaning the pre-crash
        squash (harmless). ``commit_sha`` is stamped only here and is
        write-only (never routing), so the empty persisted value is
        harmless."""
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
        # The success boundary for story-declared ledger closure (#234): every
        # verify gate, checkpoint, review cycle and pre-commit workflow is behind
        # us, and a pre_commit pause veto has already raised out of _escalate
        # above — but finalize_commit's `git add -A` is still ahead, so an in-repo
        # annotation lands in this story's own commit. It is not final until that
        # commit is: the snapshot armed below unwinds it if the commit fails.
        #
        # The close runs INSIDE this try, and reports its snapshot through `armed`
        # rather than through the return value, because the window needing a
        # rollback opens before it returns: `mark_done_many` publishes the ledger
        # atomically and the journal append recording that publication can still
        # raise (a full disk), as can the SIGTERM handler `run()` installed, from
        # anywhere in between. Binding the snapshot only on return would leave
        # every one of those raises with nothing to undo — the entry left reading
        # `done` for a commit that never happened, the one outcome this whole
        # path exists to prevent.
        armed: list[tuple[Path, str, list[str]]] = []
        try:
            self._close_declared_deferred(task, armed)
            # bmad-dev-auto commits its own work each iteration; the orchestrator
            # squashes that chain plus its uncommitted bookkeeping back onto the
            # pre-dev baseline as one commit carrying `message`. None means there
            # was nothing to finalize (NO_VCS, or the tree already at baseline).
            sha = verify.finalize_commit(self.workspace.root, task.baseline_commit, message)
            task.commit_sha = sha or task.baseline_commit
            # the corrected spec is now durable in HEAD; later attempts need no
            # special preservation, so drop the re-drive latch. The restored diff
            # is likewise committed, so clear its latch too — a subsequent re-arm
            # (if any) decides afresh whether to restore again.
            task.resolved_redrive = False
            task.restore_patch = None
        except verify.GitError as e:
            self._restore_deferred_closes(task, armed[-1] if armed else None)
            self._escalate(task, f"commit failed: {e}")
        except BaseException:
            # A failed commit is not the only way out of this window. The signal
            # handler installed by `_run` raises RunStopped from wherever the main
            # thread is standing — including inside finalize_commit — and `_run_git`
            # translates only TimeoutExpired, so a raw OSError on spawn escapes as
            # itself. Both leave the ledger flipped for a commit that does not
            # exist, and nothing above unwinds it: RunStopped is caught by `run()`
            # to finalize a *stopped* run, not to repair bookkeeping. Restore, then
            # re-raise untouched — the caller's disposition is not ours to change.
            self._restore_deferred_closes(task, armed[-1] if armed else None)
            raise
        if not self._isolated:
            # An out-of-repo ledger could not ride the commit, so it was parked;
            # the commit has now landed, which in place is the whole of "durably
            # landed" (there is no integration step). Deliberately BEFORE the DONE
            # advance: a crash in this window leaves the task COMMITTING, which
            # the resume arm re-drives — and both finalize_commit and the close
            # are idempotent. After DONE it would be terminal, and unreachable.
            self._flush_pending_deferred_closes(task)
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

    # the date stamped into ledger edits; isolated for tests
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _observed_frontmatter(self, spec_path: Path, story_key: str, site: str) -> dict | None:
        """Read a spec's frontmatter on a *bookkeeping* path, degrading an
        unreadable spec to ``None`` (journaled) instead of a whole-run crash.

        These reads observe what the dev skill left behind so the orchestrator can
        sync the sprint board / ledger. They race the skill's own writes, so an
        OSError is a designed transient, not a broken orchestrator. Returning None
        tells the caller to skip its bookkeeping pass entirely: skipping is safe
        because everything a skipped pass would have derived is re-supplied later —
        the spec *status* by the deterministic verify gate's own re-read (which
        turns a still-unrepaired spec into a retry), and the review-routing flag by
        ``_followup_from_spec`` at the point ``_dev_phase`` consumes it. Silent it
        is not — every skip lands a ``spec-read-failed`` event in the journal.

        Repair writes (``reset_spec_status``, ``mark_done``) deliberately do the
        opposite and let OSError raise: silently skipping a rewrite would leave the
        spec in a state the caller believes it fixed. Observation degrades, repair
        raises.
        """
        try:
            return verify.read_frontmatter(spec_path)
        except OSError as e:
            self._journal_spec_read_failed(spec_path, story_key, site, e)
            return None

    def _journal_spec_read_failed(
        self, spec_path: Path, story_key: str, site: str, e: OSError
    ) -> None:
        self.journal.append(
            "spec-read-failed",
            story_key=story_key,
            spec=str(spec_path),
            site=site,
            error=f"{e.__class__.__name__}: {e}",
        )

    def _followup_from_spec(self, task: StoryTask, rj: dict) -> bool:
        """Review-routing fallback for a result that carries no
        ``followup_review_recommended`` key: re-derive it from the finalized spec
        frontmatter — the source ``devcontract.synthesize_result`` and the
        reconcile folds read it from, so a readable spec can never disagree.

        The key is absent exactly when the result is a *resumed* pre-reconcile
        snapshot (``synthesize_result`` only writes it on a ``done`` synth, and the
        durable record is persisted before reconcile mutates the live dict). The
        reconcile re-fold normally restores it on replay, but a spec read fault
        skips that fold — and the verify gate only re-supplies *status*, not this
        flag — so without this fallback a recommended follow-up review would be
        silently skipped. Gates on the frontmatter's own status (mirroring the
        fold): a faulted replay leaves ``rj["status"]`` at the stale snapshot
        value, so the result status must not decide. Degrades to False on a read
        fault (journaled) — the pre-existing absent-key default.
        """
        if not self._generic_dev():
            return False
        spec_file = rj.get("spec_file")
        if not spec_file:
            return False
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if not spec_path.is_file():
            return False
        fm = self._observed_frontmatter(spec_path, task.story_key, "followup-routing")
        if fm is None:
            return False
        return verify.status_of(fm) == "done" and bool(fm.get("followup_review_recommended", False))

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
        fm = self._observed_frontmatter(spec_path, task.story_key, "reconcile")
        if fm is None:
            return
        raw_status = fm.get("status")
        fm_status = "" if raw_status is None else str(raw_status).strip().lower()
        if fm_status == success_status:
            # Already finalized — idempotent for the spec. But a *resumed* result
            # is the pre-reconcile snapshot persisted before the original run's
            # reconcile mutated its in-memory dict (the durable record is now a
            # defensive copy, so it never saw that mutation). Re-fold the derived
            # keys from the frontmatter we just read so the replay's
            # `followup_review_recommended` gate matches the finalized spec
            # instead of the stale template default. Only fold followup when the
            # frontmatter actually carries it: on the generic path the frontmatter
            # is the source `devcontract.synthesize_result` already reads it from,
            # so a present key can never disagree — but when it is absent, the
            # value the session put in result.json is authoritative and must not
            # be clobbered to a phantom False.
            if isinstance(result_json, dict):
                result_json["status"] = success_status
                if success_status == "done" and "followup_review_recommended" in fm:
                    result_json["followup_review_recommended"] = bool(
                        fm.get("followup_review_recommended")
                    )
            return
        if fm_status not in devcontract.RECONCILABLE_FROM:
            return  # blocked / unknown custom status: never override a deliberate one
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError as e:
            self._journal_spec_read_failed(spec_path, task.story_key, "reconcile-prose", e)
            return
        arr = devcontract.parse_auto_run_result(text)
        if not arr.present or arr.status != devcontract.DONE:
            return  # no terminal prose, or a blocked outcome: leave for the escalation path
        if not devcontract.reset_spec_status(spec_path, success_status):
            return
        # Keep the in-place result_json the rest of _dev_phase reads consistent with
        # the now-reconciled spec (the followup flag is only carried on a done exit).
        # `reset_spec_status` rewrites only the status line, so `fm` (read above)
        # still holds every other key — a re-read here could only return the same
        # followup flag, at the cost of a second racy read that can now fail.
        if isinstance(result_json, dict):
            result_json["status"] = success_status
            if success_status == "done":
                result_json["followup_review_recommended"] = bool(
                    fm.get("followup_review_recommended", False)
                )
        self.journal.append(
            "spec-status-reconciled",
            story_key=task.story_key,
            spec=str(spec_path),
            frm=fm_status,
            to=success_status,
        )

    def _repair_spec_marker(self, task: StoryTask, rj: dict) -> None:
        """Append the ``## Auto Run Result`` marker a missing-marker synthesis
        (#224) proved the session owed but never wrote — #276 Mechanism 3, the
        artifact-repair leg. Called at the ``session-synthesized-from-frontmatter``
        journal site, which fires for live-Stop, crash-path, and post-kill
        dead-window synthesis alike (all carry the ``synthesized_from_frontmatter``
        flag), so this ONE call site covers every synthesis path. After the append
        the spec leaves `find_frontmatter_candidates`' territory (zero real
        markers) and enters `find_result_artifact`'s (>= 1), so a later re-read is
        harvested on the normal marker path and the next review launch strips it
        exactly like a skill-written marker.

        Best-effort by doctrine: the result was already synthesized, so a failed
        or skipped repair only leaves the spec non-compliant — it never loses work.
        Guards mirror `_reconcile_generic_terminal_status`, the sibling
        session-path spec writer: the generic path only; the session-supplied
        ``spec_file`` must resolve to a real file inside the orchestrator-owned
        roots (else `spec-marker-repair-skipped`, reason ``out-of-tree`` — this is
        a write keyed off a session-reported path); and a FRESH frontmatter re-read
        must be terminal (``done``/``blocked``) AND agree with the synthesized
        ``rj["status"]`` (else reason ``fm-mismatch``). Never author a marker whose
        ``Status:`` disagrees with the frontmatter the synthesis trusted — that
        would trip `synthesize_result`'s consistency cross-check on the next read.

        Non-interference: `_reconcile_generic_terminal_status` only acts when the
        frontmatter LAGS the prose, so once this append lands (frontmatter already
        terminal) reconcile hits its idempotent / refusal branches;
        `_salvage_review_timeout` reads the frontmatter fresh and stays disjoint.
        The append is engine-side ONLY — an adapter-side write would perturb the
        adapter's own mtime/hash observation state (#276 M1/M2)."""
        if not self._generic_dev():
            return
        spec_file = (rj or {}).get("spec_file")
        if not spec_file:
            return
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if not spec_path.is_file():
            return
        if not verify.spec_within_roots(spec_path, self.workspace.paths):
            self.journal.append(
                "spec-marker-repair-skipped",
                story_key=task.story_key,
                spec=str(spec_path),
                reason="out-of-tree",
            )
            return
        fm = self._observed_frontmatter(spec_path, task.story_key, "marker-repair")
        if fm is None:
            return
        fm_status = str(fm.get("status", "")).strip().lower()
        rj_status = str(rj.get("status", "")).strip().lower()
        if fm_status not in (devcontract.DONE, devcontract.BLOCKED) or fm_status != rj_status:
            self.journal.append(
                "spec-marker-repair-skipped",
                story_key=task.story_key,
                spec=str(spec_path),
                reason="fm-mismatch",
            )
            return
        detail = (
            f"Synthesized by the bmad-loop orchestrator from frontmatter status "
            f"`{fm_status}` for story `{task.story_key}` (session finalized the spec "
            f"without appending its marker)."
        )
        try:
            repaired = devcontract.append_auto_run_result(spec_path, fm_status, detail=detail)
        except (OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError as well as OSError: the writer reads the spec's raw
            # bytes and, by contract, raises on an undecodable spec (the same
            # torn-mid-write hazard `_post_kill_reconcile` guards — a spec truncated
            # through a multi-byte UTF-8 sequence between `_observed_frontmatter`'s
            # read and this one). This repair is pure best-effort forensics; it must
            # never turn a synthesized-and-recorded result into a run crash.
            self.journal.append(
                "spec-marker-repair-failed",
                story_key=task.story_key,
                spec=str(spec_path),
                error=f"{e.__class__.__name__}: {e}",
            )
            return
        if repaired:
            self.journal.append(
                "spec-marker-repaired",
                story_key=task.story_key,
                spec=str(spec_path),
                status=fm_status,
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
        fm = self._observed_frontmatter(spec_path, task.story_key, "post-dev-sync")
        if fm is None:
            return
        if verify.status_of(fm) != success_status:
            return
        target = "review" if review_enabled else "done"
        sprint_advance(self.workspace.paths.sprint_status, task.story_key, target)

    def _manifest_closes_deferred(self, task: StoryTask) -> tuple[str, ...]:
        """Deferred-work ids declared for this story by a *manifest* the
        orchestrator reads, as opposed to the story spec's own frontmatter.

        Empty here: sprint mode has no manifest, so frontmatter is its only
        channel. ``StoriesEngine`` overrides this with its ``stories.yaml``
        entry — the channel that matters for an unattended run, since the spec is
        generated later by a dev skill that knows nothing of the ledger."""
        return ()

    def _capture_declared_deferred(self, task: StoryTask, *, site: str) -> bool:
        """Read the spec's ``closes_deferred:`` declaration onto the task (#234).

        Called from two sites, and which one is authoritative matters.

        **The commit boundary is the authority.** What the spec says at the
        moment of the close is what the story declares; a declaration edited
        after the dev artifacts verified — a review session rewriting the
        frontmatter, a human editing the spec while the review loop runs — must
        not be closed against a stale snapshot, because the stale half of that
        can close an entry the final spec no longer names.

        **The verify-time capture is what makes re-reading safe to attempt.**
        ``_observed_frontmatter`` degrades an unreadable spec to None, and at the
        close site that is indistinguishable from a spec declaring nothing — so a
        transient read fault there would let the story commit with its declared
        entry still open, leaving a journal line as the only trace. A failed read
        leaves the last good capture standing to be closed against instead. It is
        the same value the COMMITTING resume arm leans on when its own re-read
        faults; that arm finishes a commit WITHOUT re-verifying, so a persisted
        declaration is its only other source.

        The path is a verified one — ``task.spec_file`` is recorded only by a
        passing ``verify_dev``/``verify_dev_stories`` gate, and stories mode
        resolves it by id rather than trusting the session's claim. Sprint mode's
        path still came from the session, so it is held to the same
        root-containment rule the frontmatter-status reconcile applies: a
        surprising absolute path must not be able to steer a ledger write.

        A wrong-container declaration (``closes_deferred: DW-5``) is journaled
        rather than silently read as empty: it names real intent that would
        otherwise close nothing and say nothing. Both reads report it, tagged with
        the ``site`` that found it — two lines for one mistake is the honest
        record of two reads, and suppressing the second would need per-read error
        state to avoid also suppressing a spec that only turned malformed after
        verification.

        A failed read leaves any earlier capture standing — a later attempt's
        silence is not a retraction. False only when there IS a spec to read and
        reading it failed; the caller decides whether that costs anything.

        A spec that is **gone** or has moved **out of the roots** is a different
        answer from a spec that could not be read, and it clears the capture. Both
        used to return success without touching it, so a `DW-1` captured at
        dev-verify was still closed after a pre-commit workflow deleted, renamed
        or redirected the spec — closing against a declaration no readable spec
        makes, which is the stale half this re-read exists to eliminate. Only a
        genuine read fault keeps the fallback standing, and the withdrawal is
        journaled rather than inferred silently (#284 round-5 review, finding 2).

        The path is not re-derived here: ``task.spec_file`` is recorded only by a
        passing verify gate, and the root-containment rule below still holds at
        both sites."""
        spec_path = Path(task.spec_file) if task.spec_file else None
        if spec_path is None or not spec_path.is_file():
            if task.declared_deferred:
                self.journal.append(
                    "deferred-close-declaration-absent",
                    story_key=task.story_key,
                    spec=str(spec_path) if spec_path else None,
                    site=site,
                    dw_ids=list(task.declared_deferred),
                )
            task.declared_deferred = []
            return True
        if not verify.spec_within_roots(spec_path, self.workspace.paths):
            self.journal.append(
                "deferred-close-skipped-out-of-tree",
                story_key=task.story_key,
                spec=str(spec_path),
                site=site,
                dw_ids=list(task.declared_deferred or []),
            )
            task.declared_deferred = []
            return True
        fm = self._observed_frontmatter(spec_path, task.story_key, f"deferred-close-{site}")
        if fm is None:
            return False
        declared, error = deferredwork.parse_declaration(fm.get("closes_deferred"))
        if error:
            self.journal.append(
                "deferred-close-malformed",
                story_key=task.story_key,
                spec=str(spec_path),
                error=f"closes_deferred {error}",
                site=site,
            )
        task.declared_deferred = list(declared)
        return True

    def _declared_deferred_ids(self, task: StoryTask) -> tuple[str, ...]:
        """The ids this story declares it closes, unioned across both channels
        and order-preserving (a story that names the same id in the manifest and
        in its spec must be marked once and reported once).

        Both halves are re-read here, because the spec and the manifest on disk at
        the commit are what the story declares — a declaration edited after the
        dev artifacts verified would otherwise be closed against a snapshot the
        final spec no longer agrees with, and closing an id the author took back
        is the one failure this whole path exists to prevent.

        A read that FAILS falls back rather than to "declares nothing": the spec
        half to the last good capture (``StoryTask.declared_deferred``, which is
        also all a COMMITTING resume has when its own re-read faults), the
        manifest half to nothing at all — it is not persisted, so there is nothing
        to fall back to. Either way the loss is the one place a failed read can
        still cost a closure, so both say so
        (``deferred-close-declaration-unreadable``) rather than passing in
        silence."""
        ids: list[str] = list(self._manifest_closes_deferred(task))
        if task.spec_file and not self._capture_declared_deferred(task, site="commit-boundary"):
            self.journal.append(
                "deferred-close-declaration-unreadable",
                story_key=task.story_key,
                source="spec",
                spec=task.spec_file,
                note=(
                    "closing against the last good capture"
                    if task.declared_deferred
                    else "no captured declaration to fall back on"
                ),
            )
        ids += task.declared_deferred or []
        return tuple(dict.fromkeys(ids))

    def _close_declared_deferred(
        self, task: StoryTask, rollback: list[tuple[Path, str, list[str]]] | None = None
    ) -> tuple[Path, str, list[str]] | None:
        """At the commit boundary, flip every ledger entry the story declares via
        ``closes_deferred:`` to ``status: done <date>`` + a ``resolution:`` note
        (#234) — the regular-story counterpart of the sweep bundle close at
        ``SweepEngine._close_bundle_ledger_when_spec_status``.

        Declaration is the only signal: closure is never inferred from a diff.

        **Placement is the whole safety story.** This runs from
        ``_finalize_commit_phase`` — after artifact verification, the verify
        commands, every checkpoint, the review loop, the ``pre_commit_gate``
        workflows and the ``pre_commit`` veto — and still *before*
        ``finalize_commit``, whose ``git add -A`` stages the annotation into the
        story's own commit. Marking at dev-sync time instead (where this first
        landed) let a story that later failed verification, was rejected by
        review, or escalated leave the ledger permanently claiming its work
        resolved: the in-place defer path snapshots the ledger *after* the
        mutation and restores it over the rollback, and ``_escalate`` rolls back
        nothing at all.

        Where the write lands depends on where the ledger lives.

        **Inside the repo** it is written here so it rides the story's own commit
        — and the caller must hand the returned snapshot back to
        ``_restore_deferred_closes`` if that commit then fails, because
        ``_escalate`` rolls nothing back and the annotation would otherwise stand
        in the operator's checkout claiming work that was never committed.

        **Outside the repo** (``ProjectPaths.rebased`` deliberately shares an
        external artifact dir between worktrees, and ``add -A`` can never stage
        it) nothing is written here at all: no pre-commit write can ride a commit
        that cannot carry the file, so the ids are stashed on the task and applied
        only once the work is durably landed — after ``finalize_commit`` in place,
        after ``merge_local`` under isolation (see
        ``_flush_pending_deferred_closes``).

        Returns the pre-close ledger text — with its path and the ids actually
        flipped — when an in-repo write marked something, else None.

        ``rollback`` is the same snapshot, reported through a list the CALLER
        owns and **before the ledger is touched**, because the return value
        cannot cover the window that needs covering: the atomic write publishes,
        and then the journal append recording it can raise, as can the stop
        signal, before this function ever returns. A caller that only had the
        return value would meet those raises holding nothing to undo. The slot is
        cleared again when nothing was flipped — ``mark_done_many`` writes only
        when it marks — so an empty close never arms a pointless restore.

        Never a gate: an unmatched or malformed id is journaled, never fatal.
        Idempotent, so the resume arm may re-drive the commit phase freely — ids
        are classified against a ledger snapshot rather than from ``mark_done``'s
        return value, which conflates "already done" (a resume re-running a close
        that landed — must stay silent) with "absent" (a typo — worth saying)."""
        ids = self._declared_deferred_ids(task)
        if not ids:
            # A prior attempt may have parked ids for an out-of-repo ledger and
            # then failed its commit; that obligation deliberately survives, to be
            # retried by the re-drive. But the re-drive re-reads the declaration,
            # and if it is gone the obligation goes with it — every other path
            # recomputes the parked list wholesale from the fresh read, and
            # flushing a withdrawn one after the commit would be exactly the false
            # close the re-read exists to prevent, on the one ledger that has no
            # rollback behind it. Journaled, because an unreadable spec or
            # manifest reaches here the same way (reported by
            # `_declared_deferred_ids`, but indistinguishable once it returns).
            if task.pending_deferred_closes:
                self.journal.append(
                    "deferred-close-withdrawn",
                    story_key=task.story_key,
                    dw_ids=list(task.pending_deferred_closes),
                    reason="the story no longer declares them; the parked closure is dropped",
                )
                task.pending_deferred_closes = []
            return None
        ledger = self.workspace.paths.deferred_work
        # Decided on RESOLVED paths. `is_relative_to` is lexical while the write
        # below follows symlinks (`atomic_write_text` resolves first, so a
        # symlinked ledger stays a symlink instead of being replaced by a regular
        # file) — so an in-repo link pointing at a shared external ledger would
        # otherwise be treated as committable: the external target gets flipped
        # here, `add -A` stages only the unchanged link, and under isolation a
        # merge that never lands leaves shared work marked done with nothing left
        # to roll it back. Only the last path component can differ: ProjectPaths
        # resolves the artifact dir at construction and again in `rebased`.
        if not ledger.resolve().is_relative_to(self.workspace.root.resolve()):
            task.pending_deferred_closes = list(ids)
            self.journal.append(
                "deferred-close-pending-integration",
                story_key=task.story_key,
                dw_ids=list(ids),
                ledger=str(ledger),
            )
            return None
        before = ledger.read_text(encoding="utf-8") if ledger.is_file() else None
        if rollback is not None and before is not None:
            # Armed here, not on the way out. Restoring text nothing changed is a
            # no-op rewrite, so arming early costs an over-broad window nothing,
            # while arming late would miss the whole publication window.
            rollback.append((ledger, before, []))
        marked = self._apply_deferred_closes(task, ids, ledger, rollback)
        snapshot = (ledger, before, marked) if marked and before is not None else None
        if rollback is not None:
            # `mark_done_many` writes only when it marks, so an empty close left
            # the ledger byte-identical: disarm rather than record a restore that
            # would journal a rollback of nothing.
            rollback[:] = [snapshot] if snapshot is not None else []
        return snapshot

    def _restore_deferred_closes(
        self, task: StoryTask, snapshot: tuple[Path, str, list[str]] | None
    ) -> None:
        """Put the ledger back the way ``_close_declared_deferred`` found it, after
        the commit those closures were written for failed (#234).

        The close is written *before* ``finalize_commit`` precisely so an in-repo
        annotation lands in the story's own commit — but a commit can still fail
        (a rejecting native pre-commit hook, a full disk), and ``_escalate`` then
        raises without unwinding anything. Left alone the entry reads ``done`` for
        work that is not in any commit, and the most likely recovery makes it
        permanent: a human-resolved re-drive sets ``resolved_redrive``, which has
        ``safe_reset`` preserve the artifact folders' tracked content through the
        rollback, so the false close survives the reset that would otherwise have
        reverted it.

        Only the working tree is restored. The failed ``finalize_commit`` leaves
        its ``add -A`` staged, but every path that commits again starts with
        another ``add -A`` (restaging from the tree) and every rollback path goes
        through ``safe_reset``, so the index is not authoritative here."""
        if snapshot is None:
            return
        ledger, before, marked = snapshot
        try:
            atomic_write_text(ledger, before)
        except OSError as e:
            # bookkeeping repair must not mask the commit failure being escalated
            self.journal.append(
                "deferred-close-rollback-failed",
                story_key=task.story_key,
                ledger=str(ledger),
                error=str(e),
            )
            return
        self.journal.append(
            "deferred-close-rolled-back",
            story_key=task.story_key,
            # what was actually flipped and is now un-flipped, NOT everything the
            # story declared: an id that was already done stays done either way.
            # Empty means the close was interrupted before it could report what it
            # had flipped (a stop signal landing inside the write). The restore is
            # by content, not by id, so it is complete either way — only the record
            # of which entries it touched is missing.
            dw_ids=list(marked),
            ledger=str(ledger),
        )

    def _apply_deferred_closes(
        self,
        task: StoryTask,
        ids: Sequence[str],
        ledger: Path,
        rollback: list[tuple[Path, str, list[str]]] | None = None,
    ) -> list[str]:
        """Write the closure for `ids`, journal exactly what landed, and return
        the ids actually flipped.

        ``rollback`` is the caller's already-armed restore slot (see
        ``_close_declared_deferred``); the ids are written into it the moment they
        are known, which is the statement after the write and the one before the
        first thing here that can raise. Refining it costs a line and buys the
        operator a rolled-back record that names entries instead of an empty
        list."""
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        declared = deferredwork.classify(text, ids)
        marked = deferredwork.mark_done_many(
            ledger, declared.open_ids, self._today(), f"resolved by story {task.story_key}"
        )
        if rollback and marked:
            rollback[0] = (ledger, text, marked)
        if marked:
            self.journal.append("story-deferred-closed", story_key=task.story_key, dw_ids=marked)
        if declared.unknown:
            self.journal.append(
                "deferred-close-unmatched", story_key=task.story_key, dw_ids=list(declared.unknown)
            )
        if declared.malformed:
            # Present in the ledger but carrying neither an `open` nor a `done`
            # status: nothing was marked, and staying quiet would read to the
            # operator exactly like a successful close.
            self.journal.append(
                "deferred-close-malformed",
                story_key=task.story_key,
                dw_ids=list(declared.malformed),
                error="ledger entry status is neither open nor done",
            )
        return marked

    def _flush_pending_deferred_closes(self, task: StoryTask) -> None:
        """Apply closures held back for an out-of-repo ledger, once the story's
        work is durably landed: after ``finalize_commit`` in place, from the
        integration chokepoint under isolation. A story whose commit escalates or
        whose merge fails never reaches either call, and the shared ledger keeps
        reading ``open``. No-op in every in-repo configuration.

        **Clear only after the write succeeds.** Clearing first looks harmless —
        the flush is about to happen — but a raising ledger write then unwinds to
        ``run()``'s crash handler, whose ``finally: self._save()`` persists the
        emptied list: the obligation is destroyed by the very failure that made it
        worth retrying.

        **An unavailable location is not an answer about the entries.** A read
        that raises keeps the obligation by unwinding, but an absent file does
        not raise: ``_apply_deferred_closes`` reads a missing ledger as empty
        text, every id classifies as unknown, and the ids were then cleared on
        the strength of a ledger nobody could see. That is a real risk here and
        only here — this path exists *because* the ledger is out of the repo, so
        it can live on a mount that is temporarily gone. The artifact directory
        is what distinguishes the two: gone means the location is unavailable and
        the obligation is retryable; present-but-no-ledger means there is
        genuinely nothing to close, which is the same answer the in-repo path
        gives and stays a discharge rather than an obligation nothing could ever
        satisfy (#284 round-5 review, finding 4)."""
        ids = tuple(task.pending_deferred_closes)
        if not ids:
            return
        ledger = self.workspace.paths.deferred_work
        if not ledger.parent.is_dir():
            self.journal.append(
                "deferred-close-ledger-unavailable",
                story_key=task.story_key,
                dw_ids=list(ids),
                ledger=str(ledger),
                note="the artifact directory is not present; the closure stays owed and is retried",
            )
            return
        self.journal.append(
            "deferred-close-external-ledger",
            story_key=task.story_key,
            dw_ids=list(ids),
            ledger=str(ledger),
            note="ledger is outside the repo; the annotation is not part of any commit",
        )
        self._apply_deferred_closes(task, ids, ledger)
        task.pending_deferred_closes = []
        self._save()

    def _extra_session_env(
        self, task: StoryTask, role: str, label: str | None = None
    ) -> dict[str, str]:
        """Engine-variant additions to a session's environment. Base: none.
        StoriesEngine overrides this to export BMAD_LOOP_SPEC_FOLDER for the
        adapter's deterministic id-keyed read-back. ``label`` is None for the
        primary dev/review session and set for an injected plugin-workflow session,
        so a variant can scope its env to primary sessions only."""
        return {}

    def _run_verify_commands_after_dev(self, task: StoryTask, result_json: dict | None) -> bool:
        """Whether the deterministic verify commands run after a completed dev
        pass. Base: always. StoriesEngine skips them on a plan-halt leg — a plan
        (spec at ready-for-dev) has no implementation to build/test, so a project
        build/test gate would spuriously fail before the plan review."""
        return True

    def _resume_after_dev_verify(self, task: StoryTask) -> None:
        """Resume a task the run paused at DEV_VERIFY (dev verified, spec on disk).
        Base: the spec-approval-gate resume — run the review loop + commit.
        StoriesEngine overrides this to re-drive the implement leg of a
        plan-checkpoint-paused story (leg-2) instead."""
        self.journal.append("resume-review", story_key=task.story_key)
        self._review_and_commit(task)

    def _after_story(self, task: StoryTask) -> None:
        """Hook fired once a story is fully processed and (under isolation)
        integrated — from _loop after _run_story and from _finish_inflight after a
        resumed task completes. Base: no-op. StoriesEngine uses it for the
        done_checkpoint pause, which must land after integration so a committed
        unit is merged before the run stops."""
        return

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

    def _session_end_extras(self, result: SessionResult) -> dict:
        """Timeout forensics for the session-end entry (#157). ``teardown_s``
        is the timeout-fire → journal gap — the window the incident showed
        could silently stretch for hours. A tripped session-budget guard
        (#158) adds its sample plus the cap/mode it was judged against."""
        extras: dict = {}
        if result.timeout_fired_at is not None:
            extras.update(
                fired_at=result.timeout_fired_at,
                teardown_s=max(0.0, round(time.time() - result.timeout_fired_at, 3)),
                expired_clock=result.timeout_expired_clock,
            )
        if result.budget_weighted is not None:
            extras.update(
                budget_weighted=result.budget_weighted,
                budget=self.policy.limits.max_tokens_per_session,
                budget_mode=self.policy.limits.session_budget_mode,
            )
        # transport-failure classification (#194): rides both session-end emit
        # sites so the evidence line is on the record wherever the session ended.
        if result.env_fault:
            extras["env_fault"] = True
            if result.env_fault_evidence:
                extras["env_fault_evidence"] = result.env_fault_evidence
        return extras

    @staticmethod
    def _session_timeout_s(default_s: float) -> float:
        """Per-session wall-clock budget in seconds. Normally
        ``limits.session_timeout_min * 60``; ``BMAD_LOOP_SESSION_TIMEOUT_S``
        overrides it (test / override hook, à la ``BMAD_LOOP_PROCESS_HOST``).
        The policy floor is 1 minute — too coarse to exercise the #157
        timeout-teardown path in a deterministic sub-minute E2E, and a real
        binary run can't be monkeypatched. A non-positive or unparseable
        override is ignored, so a fat-fingered value can never silently shorten
        a real run's budget."""
        override = envvars.session_timeout_s()
        return override if override is not None else default_s

    def _run_session(
        self,
        task: StoryTask,
        role: str,
        prompt: str,
        seq: int,
        session_stage: str | None = None,
        label: str | None = None,
        spec_snapshot: SpecSnapshot | None = None,
    ) -> SessionResult:
        # ``label`` names a non-standard session (a plugin-provided workflow) so
        # its task_id stays distinct from the role's own dev/review attempts.
        task_id = _session_task_id(task.story_key, label if label else role, seq)
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        env = {
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(self.run_dir),
            "BMAD_LOOP_TASK_ID": task_id,
            "BMAD_LOOP_STORY_KEY": task.story_key,
        }
        # engine-variant env seam: StoriesEngine adds BMAD_LOOP_SPEC_FOLDER so the
        # dev adapter resolves the story spec deterministically by id instead of
        # mtime-scanning. Base returns {} — sprint/sweep runs stay byte-identical.
        # ``label`` (set only for injected plugin-workflow sessions) is passed so the
        # variant can withhold that env from non-primary sessions.
        env.update(self._extra_session_env(task, role, label=label))
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
            timeout_s=self._session_timeout_s(self.policy.limits.session_timeout_min * 60),
            stall_nudges_cap=(
                self.policy.limits.workflow_stall_nudges_cap
                if label is not None
                else self.policy.limits.dev_stall_nudges_cap
            ),
            # mid-session token-budget guard (#158): every session the engine
            # drives (dev/review/labeled workflow) gets the same policy caps.
            token_budget=self.policy.limits.max_tokens_per_session,
            token_budget_mode=self.policy.limits.session_budget_mode,
            token_budget_grace_s=float(self.policy.limits.session_budget_grace_s),
            cache_read_weight=self.policy.limits.cache_read_weight,
            # Launch-state snapshot of a review session's spec (#276 M1); None for
            # every other session and on a crash-resume (process-transient).
            spec_snapshot=spec_snapshot,
        )
        self.journal.set_active_log(task_id)
        self.journal.append(
            "session-start",
            task_id=task_id,
            role=role,
            adapter=cfg.name,
            model=cfg.model,
            story_key=task.story_key,
            prompt=prompt,
        )
        # Every session-start must be paired with a session-end, whatever path
        # leaves this method: on an abort (RunStopped / KeyboardInterrupt / a
        # transport error out of adapter.run) the top-level handlers record
        # run-stop/run-crash but know nothing of the open session, and the
        # journal would show it running forever (#157).
        result: SessionResult | None = None
        ended = False
        try:
            result = adapter.run(spec)
            # A post-kill rescue (#61) is otherwise indistinguishable from a normal
            # completion in the journal; leave a breadcrumb for forensics.
            if result.result_json is not None and result.result_json.get("post_kill_reconciled"):
                self.journal.append("session-rescued-post-kill", task_id=task_id, role=role)
            # Same forensics need for a missing-marker synthesis (#224): the
            # result is real, but the marker-append the skill owes was skipped.
            if result.result_json is not None and result.result_json.get(
                "synthesized_from_frontmatter"
            ):
                self.journal.append(
                    "session-synthesized-from-frontmatter", task_id=task_id, role=role
                )
                # #276 M3: the marker the skill owed was never appended. Repair the
                # on-disk spec (best-effort) so the next re-read is harvested on the
                # normal marker path. Covers live-Stop, crash-path, and post-kill
                # dead-window synthesis — every path that sets this flag.
                self._repair_spec_marker(task, result.result_json)
            # Only dev/review sessions are resumable — `_resumable_session` matches
            # exactly those task ids under DEV_RUNNING/REVIEW_RUNNING. For everything
            # else (triage/sweep, labeled plugin-workflow sessions) the payload is
            # never consumed on resume, so persisting it is pure state.json bloat.
            # For resumable sessions, store a defensive copy: `result.result_json`
            # is mutated in place downstream (`_reconcile_generic_terminal_status`),
            # and the durable record must stay a stable snapshot of what the adapter
            # returned rather than aliasing a later, half-mutated dict. Shallow is
            # enough — reconcile only touches top-level keys.
            resumable = label is None and role in ("dev", "review")
            task.record_session(
                SessionRecord(
                    task_id=task_id,
                    role=role,
                    status=result.status,
                    adapter=cfg.name,
                    model=cfg.model,
                    session_id=result.session_id,
                    transcript_path=result.transcript_path,
                    result_json=(
                        dict(result.result_json)
                        if resumable and result.result_json is not None
                        else None
                    ),
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
                # Weighted rides every usage-bearing session-end, not just
                # budget-tripped ones (#129): `tokens` alone is a bare scalar
                # from which the weighted figure cannot be recovered, so
                # per-session spend was unreconstructible after the fact.
                # `None`, never 0, when usage is untracked — a zeroed
                # TokenUsage weighs 0, and untracked != free (see tokens.py).
                # Distinct from `budget_weighted`, which the extras add only on
                # a trip and which means the guard's mid-session sample.
                tokens_weighted=(
                    usage.weighted_total(self.state.cache_read_weight()) if usage else None
                ),
                **self._session_end_extras(result),
            )
            ended = True
        finally:
            if not ended:
                # Best-effort: a journal IO error here must never mask the
                # exception that is unwinding this frame.
                try:
                    if result is not None:
                        # A post-run step raised (e.g. read_usage): the session
                        # itself finished — journal its real status, sans usage.
                        # Both token fields are hardcoded None: `usage` may be
                        # UNBOUND here (read_usage itself is a candidate raiser),
                        # so referencing it would raise NameError on a path that
                        # is already unwinding an exception.
                        self.journal.append(
                            "session-end",
                            task_id=task_id,
                            status=result.status,
                            tokens=None,
                            tokens_weighted=None,
                            **self._session_end_extras(result),
                        )
                    else:
                        exc = sys.exc_info()[1]
                        self.journal.append(
                            "session-end",
                            task_id=task_id,
                            status="aborted",
                            error=type(exc).__name__ if exc is not None else None,
                        )
                except Exception:  # nosec B110
                    pass
        self._save()
        self._emit(
            "post_session",
            task,
            role=role,
            # Reaching here means `adapter.run` returned (a non-None SessionResult)
            # rather than raising past the finally, but pyright can't prove that
            # through the try/finally, so it keeps `result` widened to | None.
            session_status=result.status,  # pyright: ignore[reportOptionalMemberAccess]
            result_json=result.result_json,  # pyright: ignore[reportOptionalMemberAccess]
        )
        return result  # pyright: ignore[reportReturnType]

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._generic_dev_prompt(task, feedback)

    def _generic_dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Invocation for the generic `bmad-dev-auto` dev skill, which has no
        `--feedback` flag: feedback is inlined as freeform intent pointing at the
        existing spec. On a repair re-invocation the spec is first re-opened
        (status → `in-progress`) so the skill's step-01 re-enters implement/review
        on it rather than ingesting a finalized spec as mere context.

        A patch-restore re-drive (#2564) must point at the spec explicitly: only
        step-01's spec-pointer intent check EARLY EXITs on the `in-review` status
        the re-arm set — and it exits before step-01's version-control sanity
        check, which would otherwise HALT `blocked` on the very diff
        `_restore_patch` just laid onto the tree. A bare story key takes the
        freeform/epic path instead, where that dirty-tree check runs first."""
        if feedback is None:
            if task.restore_patch and task.spec_file:
                return (
                    f"/bmad-dev-auto Resume review of the in-review spec at "
                    f"`{task.spec_file}`. The attempted change was restored onto "
                    f"the working tree after an intent-gap resolution; review it "
                    f"against the amended spec."
                )
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

    def _reset_spec_for_review(self, task: StoryTask) -> SpecSnapshot | None:
        """Strip the prior pass's stale `## Auto Run Result` before a review launch,
        then capture a launch-state snapshot of the spec (#276 M1).

        A follow-up review session re-invokes bmad-dev-auto on the FINALIZED spec,
        which still carries the dev pass's terminal `## Auto Run Result` section.
        The review's own step-04 entry write (it stamps the transient `in-review`
        status) bumps the spec's mtime past `find_result_artifact`'s launch floor,
        so the review's first result-less Stop reads that stale marker as this
        session's terminal result and kills the session mid-flight — the #109 stall
        grace can never arm on the review leg (issue #160). Cycle 2+ carries the
        PREVIOUS review pass's own marker, so this runs before every review launch,
        never on the crash-resume replay branch (no session launches there). Unlike
        `_reset_spec_for_repair` the frontmatter is left untouched: `status: done` is
        what routes the re-invocation's step-01 to a fresh step-04 review pass — the
        HARD CONSTRAINT that the review-launch frontmatter status is NEVER mutated
        (it is load-bearing skill routing), so every #276 mechanism observes only.

        Returns a `SpecSnapshot` of the on-disk spec as it stood at launch — its
        content hash, mtime, and normalized frontmatter status — so the generic
        adapter's missing-marker fallback can refuse to synthesize from a candidate
        whose bytes never changed this session (a `done` spec re-opened for review,
        never re-written). Snapshot capture is best-effort: a torn/unreadable read
        degrades to `None` (journaled, `review-launch-snapshot`), and the fallback
        then keeps its conservative 2-observation fingerprint path. Only the capture
        is guarded — the strip keeps its raise-on-unreadable repair doctrine (see
        `devcontract.strip_auto_run_result`): skipping the strip recreates the exact
        #160 bug state, so it must surface.

        No-op (returns `None`) when the dev skill is not the generic one or no spec
        is recorded yet."""
        if not self._generic_dev() or not task.spec_file:
            return None
        spec_path = Path(task.spec_file)
        devcontract.strip_auto_run_result(spec_path)
        try:
            raw = spec_path.read_bytes()
            mtime_ns = spec_path.stat().st_mtime_ns
            fm_status = str(verify.read_frontmatter(spec_path).get("status", "")).strip().lower()
        except OSError as e:
            self._journal_spec_read_failed(spec_path, task.story_key, "review-launch-snapshot", e)
            return None
        return SpecSnapshot(
            path=str(spec_path),
            mtime_ns=mtime_ns,
            sha256=hashlib.sha256(raw).hexdigest(),
            fm_status=fm_status,
        )

    def _write_feedback(self, task: StoryTask, reason: str) -> Path:
        """Persist a verification failure where the next session can read it —
        deterministic evidence must reach the LLM, not just the journal."""
        path = self.run_dir / "feedback" / f"{safe_segment(task.story_key)}-{len(task.sessions)}.md"
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
                env_fault=bool((outcome is not None and outcome.env_fault) or result.env_fault),
            )
            if result.status != "completed" and result.env_fault:
                # A fix session whose CLI lost its API connection (#194) did no
                # repair work — another attempt cannot fix the run environment, so
                # pause (re-arm restores the budget) instead of burning the dev
                # budget. A completed-but-env-fault-grade verify failure is handled
                # by the retryable check just below (its own escalate path).
                self._escalate(
                    task,
                    env_fault_pause_reason("fix", result),
                )
            if outcome is not None and not outcome.ok and not outcome.retryable:
                # escalate-grade failure (environment fault): another repair
                # session cannot fix the run environment — stop spending the
                # dev budget and pause for a human instead
                self._escalate(task, outcome.reason)
            self._save()
            if ok:
                return True
        return False

    def _record_review_budget_followup(self, task: StoryTask, damped: bool = False) -> None:
        """A *finalized, verify-green* story that the review pass kept recommending
        a follow-up for is being committed (not rolled back); preserve the lingering
        recommendation as a new open deferred-work entry so a later, deliberate
        review can pick it up. Called immediately before ``_commit`` so the ledger
        edit is squashed into the same commit.

        Two callers, distinguished by ``damped``:
          * ``damped=False`` — the review loop *exhausted* its ``max_review_cycles``
            budget while still recommending a follow-up. A noteworthy event: always
            notify the human.
          * ``damped=True`` — the follow-up-review damping cap
            (``limits.max_followup_reviews``) was spent, so the orchestrator
            force-converged this finalized round instead of burning another cycle.
            The expected steady state: stay quiet (no ATTENTION notice) unless the
            re-review cap also fires.

        Re-review cap: if this story itself *originated* from such an entry (a
        sweep bundle closing a ``review-budget-followup`` id), don't re-file again
        — commit + notify only, so a second non-convergence reaches a human
        instead of slowly looping across sweeps. The loud re-review notice fires on
        both paths (a capped story that still won't converge must reach a human even
        under damping)."""
        cycles = self.policy.limits.max_review_cycles
        cap = self.policy.limits.max_followup_reviews
        spec = Path(task.spec_file).name if task.spec_file else task.story_key
        ledger = self.workspace.paths.deferred_work
        if damped:
            reason = (
                f"The follow-up-review damping cap (limits.max_followup_reviews = {cap}) "
                f"was spent with the story finalized (status: done, verify green) while "
                f"the review pass still recommended an independent follow-up. The work "
                f"was committed by bmad-loop run {self.state.run_id}; this entry "
                f"preserves the lingering recommendation for a deliberate later review."
            )
        else:
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
            tail = "the damping cap was spent" if damped else "the review budget was exhausted"
            title = f"Follow-up review still recommended for {task.story_key} after {tail}"
            refiled = deferredwork.append_entry(
                ledger,
                title=title,
                origin="review-budget-followup",  # verbatim: re-review cap + replay dedupe key on it
                source_spec=spec,
                reason=reason,
                severity="low",
            )
        if damped:
            self.journal.append(
                "review-followup-damped",
                story_key=task.story_key,
                cycle=task.review_cycle,
                cap=cap,
                refiled=refiled,
                re_review_capped=re_review,
            )
        else:
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
        # Exhaustion always notifies. Damped convergence is the expected steady
        # state and stays quiet — EXCEPT when the re-review cap fires: a story that
        # itself originated from a review-budget-followup entry still won't converge
        # and must reach a human even under damping.
        if not damped or re_review:
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
        work in it is worth keeping for the human.

        A story that defers twice re-stashes the same filename, so the target may
        exist. `shutil.move` survived that on Windows only by accident: `os.rename`
        raises FileExistsError over an existing target, `move` catches *any* OSError
        and falls back to `copy2` + `unlink`. Two real hazards ride on that fallback
        (#101) — it re-fails outright when an AV/indexer handle turns the rename into
        a sharing violation (WinError 5/32) and `copy2` then cannot open the same
        locked target, and it is non-atomic, so a crash mid-copy leaves a truncated
        stash. Staging a copy inside `dest` and `atomic_replace`-ing it onto the
        target overwrites in one step, carries #98's win32 retry, and — because the
        staging copy lives in `dest` — keeps the replace same-filesystem, preserving
        `shutil.move`'s cross-device tolerance.

        Both halves of the move are retried: Windows denies a delete against an open
        handle just as it denies a rename-over, so an unretried `unlink` would fail
        the run on the very hazard the replace now rides out. The order is
        replace-then-unlink because `_defer` calls this before the rollback and the
        `story-deferred` journal append — a failure here aborts the deferral, so it
        must be able to leave a duplicate spec, never a hole where the work was."""
        if not task.spec_file:
            return
        spec_path = Path(task.spec_file)
        if not spec_path.is_file():
            return
        dest = self.run_dir / "deferred" / safe_segment(task.story_key)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / spec_path.name
        tmp = dest / (spec_path.name + ".tmp")
        shutil.copy2(spec_path, tmp)
        try:
            atomic_replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):  # the copy is disposable; keep the real error
                tmp.unlink(missing_ok=True)
            raise
        retrying_unlink(spec_path)
        self.journal.append(
            "deferred-artifacts-stashed",
            story_key=task.story_key,
            stashed_to=str(target),
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
        if graceful_stop_requested(self.run_dir):
            # A pending graceful stop suppresses new child sweeps. Return (not
            # raise): at the run-end call site the story queue is already empty,
            # so finishing this run as `finished` is truthful — the finally clears
            # the superseded control file. Crucially this precedes the append
            # below, so the trigger stays UNrecorded and a later resume (after the
            # request is cleared) can still fire the sweep it would have run.
            self.journal.append("sweep-auto-suppressed", trigger=trigger)
            return
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
        except Exception as e:  # child must never break the parent
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
