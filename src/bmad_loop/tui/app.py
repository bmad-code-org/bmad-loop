"""`bmad-loop tui` application shell.

Observer/launcher only: the TUI never runs engines in-process. Run control
(r/s/e) launches detached bmad-loop processes in the bmad-loop-ctl tmux
session via tui.launch. Dry runs are captured into a text modal; validate
renders its `--json` document into a findings modal (falling back to the text
one), so the verdict is the document's `ok` rather than an exit code.
The g binding opens the policy.toml settings editor.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from rich.text import Text
from textual import work
from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from tomlkit.exceptions import ParseError

from .. import bmadconfig, decisions, devcontract, policy, resolve, runs, stories, verify
from ..adapters.multiplexer import MultiplexerError, mux_usable
from ..journal import load_state
from ..model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_CHECKPOINT,
    PAUSE_STORY_GATE,
    RunState,
)
from ..policy import POLICY_FILE
from ..process_host import ProcessHostError
from ..runs import RUNS_DIR, RearmError, StopRunError
from . import data, launch, widgets
from .screens.dashboard import DashboardScreen
from .screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    DecisionModal,
    EscalationModal,
    SpecReviewModal,
    StartRunModal,
    StartSweepModal,
    StoryCheckpointModal,
    TextOutputModal,
    ValidateFindingsModal,
)
from .screens.settings_screen import SettingsScreen
from .settings import PolicyDoc


def _engine_possibly_live(run_dir: Path) -> bool:
    live = data.liveness(run_dir)
    if live == "alive":  # provably live, pid-backed or via a legacy session
        return True
    # 'unknown' means possibly-live only for a pid-backed run (a win32 engine
    # whose pid exists but is unreadable). A legacy pid-less run's 'unknown' just
    # means no session was found — it must not flag every old finished run.
    return live == "unknown" and runs.read_pid(run_dir) is not None


_T = TypeVar("_T")


class BmadLoopApp(App[None]):
    TITLE = "bmad-loop"

    CSS = """
    #left {
        width: 34;
        /* the divider to #detail is the draggable #split-main bar, not a border */
    }
    #runs {
        height: 2fr;
        min-height: 4;
        border-top: solid $primary-darken-2;
    }
    #runs {
        border-title-color: $text;
        border-title-style: bold;
    }
    #sprint-tree, #stories-table {
        /* the dividers above these panes are the draggable splitter bars, which
           also carry the section title that used to ride the border-top */
        height: 3fr;
        min-height: 4;
    }
    #deferred {
        height: 2fr;
        min-height: 4;
        /* strip OptionList's default tall border + padding so the pane sits
           flush with the splitter bar above it */
        border: none;
        padding: 0;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #detail {
        width: 1fr;
    }
    #runheader {
        height: auto;
        padding: 0 1;
        background: $boost;
        border-bottom: solid $primary-darken-2;
    }
    #tasks {
        height: auto;
        max-height: 35%;
    }
    #tabs {
        height: 1fr;
    }
    #journal {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "start_run", "run"),
        Binding("s", "start_sweep", "sweep"),
        Binding("e", "resume_run", "resume"),
        Binding("p", "review_pause", "review"),
        Binding("R", "resolve_run", "resolve"),
        Binding("d", "answer_decisions", "decisions"),
        Binding("a", "attach", "attach"),
        Binding("x", "stop_run", "stop"),
        Binding("S", "graceful_stop_run", "soft-stop"),
        Binding("D", "delete_run", "delete"),
        Binding("A", "archive_run", "archive"),
        Binding("c", "cleanup_sessions", "cleanup"),
        Binding("v", "validate", "validate"),
        Binding("g", "settings", "settings"),
        Binding("M", "toggle_dark", "mode"),
    ]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project.resolve()
        self.sub_title = str(self.project)
        self._dashboard = DashboardScreen(self.project)

    def on_mount(self) -> None:
        self.push_screen(self._dashboard)

    def action_toggle_dark(self) -> None:
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"

    # ------------------------------------------------------------ run control

    def _mux_missing(self) -> bool:
        if launch.mux_available():
            return False
        self.notify("multiplexer backend unavailable — launch/attach disabled", severity="error")
        return True

    def _mux_guarded(self, probe: Callable[[], _T]) -> tuple[bool, _T | None]:
        """Run a raiser-side multiplexer *read* probe from a foreground action
        handler, converting a transport failure into an error toast. Returns
        (ok, value); when ok is False a MultiplexerError was caught and toasted
        and the handler must abort — a backend hiccup after the availability
        pre-gate fails the action soft instead of crashing the TUI. Foreground
        only: worker threads marshal notify() via call_from_thread (see
        _cleanup_sessions_worker), and launch-layer failures convert to
        LaunchError (see launch._ensure_ctl_session)."""
        try:
            return True, probe()
        except MultiplexerError as e:
            self.notify(str(e), severity="error")
            return False, None

    def _guarded(self, go: Callable[[], None]) -> None:
        """Pre-launch guard mirroring the CLI: the #414 isolation/repo_root conflict
        refused first, then a clean worktree required, plus a confirm when another
        engine is already live."""
        # The detached CLI refuses this combination too, and it is the authority —
        # this only turns a pane that dies immediately into a toast. Ordered ahead of
        # the clean-tree gate for the same reason `cmd_run` orders it ahead: this one
        # says the configuration cannot run at all, so answering "commit or stash
        # first" would send the operator to fix something that is not the problem.
        #
        # An unreadable config or policy falls through to launch rather than
        # blocking: the guard cannot tell "no conflict" from "could not look", so it
        # defers to the CLI, which reads the same two files and fails loudly on
        # whichever one it cannot parse. Both loaders convert an undecodable file
        # into their own typed error, so the two named here are the whole surface;
        # a raw `UnicodeDecodeError` would be a ValueError and escape.
        try:
            conflict = bmadconfig.worktree_isolation_conflict(
                bmadconfig.load_paths(self.project),
                policy.load(self.project / POLICY_FILE).scm.isolation,
            )
        except (bmadconfig.BmadConfigError, policy.PolicyError, OSError):
            conflict = None
        if conflict is not None:
            self.notify(conflict, severity="error")
            return
        try:
            if not verify.worktree_clean(self.project):
                self.notify(
                    "git worktree is not clean — commit or stash first",
                    severity="error",
                )
                return
        except verify.GitError as e:
            self.notify(f"git check failed: {e}", severity="error")
            return
        live = [
            r.run_id for r in data.discover_runs(self.project) if _engine_possibly_live(r.run_dir)
        ]
        if live:
            self.push_screen(
                ConfirmModal(
                    "another run may be live",
                    f"live or unknown: {', '.join(live)}\n"
                    "launching another engine on the same project may conflict.",
                    confirm_label="launch anyway",
                ),
                lambda ok: go() if ok else None,
            )
        else:
            go()

    def action_start_run(self) -> None:
        if self._mux_missing():
            return
        source, spec_folder = self._stories_defaults()
        self.push_screen(
            StartRunModal(self.project, default_source=source, default_spec_folder=spec_folder),
            self._start_run_result,
        )

    def _stories_defaults(self) -> tuple[str, str]:
        """The [stories] policy source + spec_folder to prefill the start-run
        modal, or the sprint-mode default when policy is unreadable — including
        undecodable, which `policy.load` reports as a `PolicyError` rather than
        letting a raw `UnicodeDecodeError` past this handler."""
        try:
            pol = policy.load(self.project / POLICY_FILE)
        except (policy.PolicyError, OSError):
            return "sprint-status", ""
        return pol.stories.source, pol.stories.spec_folder

    def _start_run_result(self, result: dict | None) -> None:
        if not result:
            return
        stories_on = result["source"] == "stories"
        spec_folder = result["spec_folder"] if stories_on else ""
        if stories_on and not spec_folder:
            self.notify("stories mode needs a spec folder", severity="error")
            return
        if result["dry_run"]:
            tail = ["run", "--project", str(self.project), "--dry-run"]
            if stories_on:
                tail += ["--spec", spec_folder]
            if result["epic"] is not None:
                tail += ["--epic", str(result["epic"])]
            if result["story"]:
                tail += ["--story", result["story"]]
            if result["max_stories"] is not None:
                tail += ["--max-stories", str(result["max_stories"])]
            self._show_captured("run --dry-run", tail)
            return

        def go() -> None:
            run_id = runs.new_run_id()
            try:
                launch.start_run_detached(
                    self.project,
                    run_id,
                    spec=spec_folder or None,
                    epic=result["epic"],
                    story=result["story"],
                    max_stories=result["max_stories"],
                )
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"run {run_id} launched (control session {launch.CTL_SESSION})")
            self._dashboard.expect_run(run_id)

        self._guarded(go)

    def action_start_sweep(self) -> None:
        if self._mux_missing():
            return
        self.push_screen(StartSweepModal(), self._start_sweep_result)

    def _start_sweep_result(self, result: dict | None) -> None:
        if not result:
            return
        if result["dry_run"]:
            self._show_captured(
                "sweep --dry-run",
                ["sweep", "--project", str(self.project), "--dry-run"],
            )
            return

        def go() -> None:
            run_id = runs.new_run_id()
            try:
                launch.start_sweep_detached(
                    self.project,
                    run_id,
                    no_prompt=result["no_prompt"],
                    decisions_only=result["decisions_only"],
                    max_bundles=result["max_bundles"],
                )
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"sweep {run_id} launched (control session {launch.CTL_SESSION})")
            self._dashboard.expect_run(run_id)

        self._guarded(go)

    def action_answer_decisions(self) -> None:
        """Walk the deferred-work decisions past sweeps left unanswered, one
        modal at a time. Each answer is recorded so the next sweep acts on it
        (build -> bundle, close -> closed, keep-open -> recorded) without asking
        again. No tmux/engine needed — this only edits the ledger and store."""
        pending = data.pending_missed_decisions(self.project)
        if not pending:
            self.notify("no unanswered decisions from past sweeps")
            return
        self._walk_decisions(list(pending), 0, 0)

    def _walk_decisions(self, pending: list, idx: int, answered: int) -> None:
        if idx >= len(pending):
            if answered:
                self.notify(f"recorded {answered} decision(s) — run a sweep to act on any builds")
                self._dashboard._tick(force_rescan=True)
            return
        decision = pending[idx]

        def on_choice(option: object | None) -> None:
            if option is None:  # skipped this one: stop, keep the rest pending
                if answered:
                    self.notify(f"recorded {answered} decision(s)")
                    self._dashboard._tick(force_rescan=True)
                return
            ok = self._record_decision(decision, option)
            self._walk_decisions(pending, idx + 1, answered + (1 if ok else 0))

        self.push_screen(DecisionModal(decision), on_choice)

    def _record_decision(self, decision: object, option: object) -> bool:
        try:
            decisions.apply_pre_answer(
                self.project, decision, option, date=time.strftime("%Y-%m-%d")
            )
        except (OSError, bmadconfig.BmadConfigError) as e:
            self.notify(f"failed to record {decision.id}: {e}", severity="error")
            return False
        return True

    def action_resume_run(self) -> None:
        if self._mux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        run_dir = self.project / RUNS_DIR / run_id
        try:
            state = load_state(run_dir)
        except (OSError, KeyError, ValueError):
            self.notify(f"state for run {run_id} is unreadable", severity="error")
            return
        if state.finished:
            self.notify(f"run {run_id} already finished", severity="warning")
            return
        engine_alive = _engine_possibly_live(run_dir)

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                launch.resume_detached(self.project, run_id)
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"resume of {run_id} launched (control session {launch.CTL_SESSION})")

        self.push_screen(ConfirmResumeModal(run_id, state, engine_alive), done)

    def action_attach(self) -> None:
        if self._mux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        session = runs.session_name(run_id)
        window = launch.ctl_window(run_id)
        ok, agent_live = self._mux_guarded(lambda: launch.session_exists(session))
        if not ok:
            return
        # A sweep blocked on a decision prompt has no agent session — the
        # human answers in the orchestrator's ctl window. Otherwise prefer the
        # live agent session, falling back to the ctl window between sessions.
        if window is not None and (self._dashboard.decision_pending is not None or not agent_live):
            launch.select_ctl_window(window)
            self._attach_to_target(
                launch.ctl_target(),
                return_window=launch.ctl_target(window),
            )
            return
        elif agent_live:
            target = runs.session_target(run_id)
        else:
            self.notify(
                f"nothing to attach: no live agent session ({session}) and no "
                f"{launch.CTL_SESSION} window for this run (runs started outside "
                "the TUI have none)",
                severity="warning",
                timeout=10,
            )
            return
        self._attach_to_target(target)

    def _attach_to_target(self, target: str, return_window: str | None = None) -> None:
        ok, argv = self._mux_guarded(lambda: runs.attach_target_argv(target))
        if not ok:
            return
        # Backend-honest inside-the-multiplexer probe (current_return_target()
        # is None outside): inside, attach_target_argv returned the
        # fire-and-forget switch/focus form, so no suspend is needed.
        ret = launch.current_return_target()
        if ret is not None:
            # Record our own pane target (session-qualified when resolvable)
            # on the ctl window so its trailing shell switches the client back
            # here when it exits, instead of stranding the user in the control
            # session.
            if return_window is not None:
                launch.set_return_pane(return_window, ret)
            subprocess.call(argv)
            return
        # Outside tmux we attach a throwaway client (under suspend). The ctl
        # session keeps its own shell window, so a closed run window would leave
        # that client parked on the shell rather than ending the attach; tell the
        # window to detach the client on exit so `tmux attach` returns and the
        # TUI resumes where the user left it.
        if return_window is not None:
            launch.set_return_pane(return_window, launch.RETURN_DETACH)
        try:
            with self.suspend():
                subprocess.call(argv)
        except SuspendNotSupported:
            self.notify(
                f"cannot suspend here — run manually: {shlex.join(argv)}",
                severity="warning",
                timeout=10,
            )

    def action_resolve_run(self) -> None:
        if self._mux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        run_dir = self.project / RUNS_DIR / run_id
        try:
            state = load_state(run_dir)
        except (OSError, KeyError, ValueError):
            self.notify(f"state for run {run_id} is unreadable", severity="error")
            return
        if state.paused_stage != "escalation":
            self.notify(
                "resolve is only available for a run paused at an escalation",
                severity="warning",
            )
            return
        if _engine_possibly_live(run_dir):
            self.notify(f"run {run_id} may still be live — stop it first", severity="warning")
            return
        story = state.paused_story_key or "?"

        self.push_screen(
            ConfirmModal(
                "resolve escalation",
                f"open the resolve agent for {story}?\n"
                "converse to fix the frozen spec, then confirm re-arm + resume in that window.",
                confirm_label="resolve",
            ),
            lambda ok: self._launch_resolve(run_id) if ok else None,
        )

    def _launch_resolve(self, run_id: str) -> None:
        """Open the interactive resolve agent for run_id in a ctl window and
        attach — the same path `bmad-loop resolve` drives. The caller has already
        confirmed and (for the escalation viewer) gated on liveness."""
        try:
            win_id = launch.start_resolve_detached(self.project, run_id)
        except launch.LaunchError as e:
            self.notify(str(e), severity="error")
            return
        if not win_id:
            self.notify("resolve launched but its window id was not captured", severity="error")
            return
        launch.select_ctl_window_id(win_id)
        self._attach_to_target(launch.ctl_target(), return_window=win_id)

    # -------------------------------------------------------- HITL pause review

    def action_review_pause(self) -> None:
        """Open the stage-appropriate review viewer for the selected paused run.
        Each viewer's actions call the exact code paths the CLI uses (resume,
        reset-to-draft + resume, rearm + resume, resolve, stop) — no duplicated
        logic. Pause kind is read from RunState.paused_stage."""
        selected = self._paused_selection()
        if selected is None:
            return
        run_id, run_dir, state = selected
        stage = state.paused_stage
        if stage == PAUSE_PLAN_CHECKPOINT:
            self._review_plan_checkpoint(run_id, run_dir, state)
        elif stage == PAUSE_STORY_CHECKPOINT:
            self._review_story_checkpoint(run_id, run_dir, state)
        elif stage == PAUSE_ESCALATION:
            self._review_escalation(run_id, run_dir, state)
        elif stage in (PAUSE_SPEC_APPROVAL, PAUSE_EPIC_BOUNDARY, PAUSE_STORY_GATE):
            self._review_gate(run_id, run_dir, state)
        else:
            self.notify(f"no review viewer for pause stage {stage!r}", severity="warning")

    def _paused_selection(self) -> tuple[str, Path, RunState] | None:
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return None
        run_dir = self.project / RUNS_DIR / run_id
        try:
            state = load_state(run_dir)
        except (OSError, KeyError, ValueError):
            self.notify(f"state for run {run_id} is unreadable", severity="error")
            return None
        if not state.paused:
            self.notify("run is not paused — nothing to review", severity="warning")
            return None
        return run_id, run_dir, state

    def _review_plan_checkpoint(self, run_id: str, run_dir: Path, state: RunState) -> None:
        spec_path, spec_text = self._paused_spec(state)
        modal = SpecReviewModal(
            title="plan checkpoint — review the planned spec before implementation",
            subtitle=self._story_subtitle(state),
            spec_path=spec_path,
            spec_text=spec_text,
            actions=[
                ("approve", "Approve & resume", "primary"),
                ("replan", "Request replan", "warning"),
            ],
        )

        def done(verb: str | None) -> None:
            if verb == "approve":
                self._do_resume(run_id)
            elif verb == "replan":
                if spec_path is None:
                    self.notify("no spec file to reset for replan", severity="error")
                    return
                self._do_replan(run_id, spec_path)

        self.push_screen(modal, done)

    def _review_gate(self, run_id: str, run_dir: Path, state: RunState) -> None:
        labels = {
            PAUSE_SPEC_APPROVAL: "spec-approval gate",
            PAUSE_EPIC_BOUNDARY: "epic gate",
            PAUSE_STORY_GATE: "story gate",
        }
        spec_path, spec_text = self._paused_spec(state)
        modal = SpecReviewModal(
            title=f"{labels.get(state.paused_stage, 'gate')} — review the finalized spec",
            subtitle=self._story_subtitle(state),
            spec_path=spec_path,
            spec_text=spec_text,
            actions=[("resume", "Approve & resume", "primary")],
        )
        self.push_screen(modal, lambda verb: self._do_resume(run_id) if verb == "resume" else None)

    @staticmethod
    def _checkpoint_gate_line(review_cycle: int) -> str:
        """The story-checkpoint card's gate line, derived from real task state.

        A done_checkpoint fires only after the story's verify + review gates
        passed and it committed, so the pass is backed by the commit's existence
        — but we do not persist per-command verify output, so we state the gates
        cleared plus the follow-up review-cycle count the task actually records,
        never a blanket hardcoded "verification passed" claim."""
        if review_cycle == 0:
            note = "no follow-up review cycles"
        elif review_cycle == 1:
            note = "1 follow-up review cycle"
        else:
            note = f"{review_cycle} follow-up review cycles"
        return f"verify + review gates passed · {note}"

    def _review_story_checkpoint(self, run_id: str, run_dir: Path, state: RunState) -> None:
        story_key = state.paused_story_key or "?"
        task = state.tasks.get(story_key)
        commit = ""
        tokens = "-"
        # Defensive default: a done_checkpoint implies a commit, but if none is
        # recorded say so rather than assert a verify outcome we cannot back.
        verify_line = "no commit recorded for this story"
        if task is not None:
            if task.commit_sha:
                subject = self._commit_subject(task.commit_sha)
                commit = f"{task.commit_sha[:12]} {subject}".strip()
                verify_line = self._checkpoint_gate_line(task.review_cycle)
            weight = state.cache_read_weight()
            raw = task.tokens.total
            if raw:
                tokens = f"{task.tokens.weighted_total(weight):,} ({raw:,} raw)"
        modal = StoryCheckpointModal(
            story_key=story_key,
            title=self._story_context(state, story_key)[0],
            commit=commit,
            verify_line=verify_line,
            tokens=tokens,
        )

        def done(verb: str | None) -> None:
            if verb == "continue":
                self._do_resume(run_id)
            elif verb == "stop":
                self._stop_run_worker(run_id, run_dir)

        self.push_screen(modal, done)

    def _review_escalation(self, run_id: str, run_dir: Path, state: RunState) -> None:
        story_key = state.paused_story_key or "?"
        spec_path, spec_text = self._paused_spec(state)
        title, description = self._story_context(state, story_key)
        restore_recorded = self._restore_recorded(run_dir, story_key)
        modal = EscalationModal(
            story_key=story_key,
            title=title,
            description=description,
            blocking=self._blocking_condition(spec_text),
            sentinel_kind=self._sentinel_kind(state, story_key),
            resolution_ready=resolve.resolution_path(run_dir, story_key).is_file(),
            engine_live=_engine_possibly_live(run_dir),
            restore_recorded=restore_recorded,
        )

        def done(verb: str | None) -> None:
            if verb == "resolve":
                if self._mux_missing() or self._resolve_blocked_by_liveness(run_id, run_dir):
                    return
                self._launch_resolve(run_id)
            elif verb == "rearm":
                self._do_rearm(run_id, run_dir, story_key, restore_recorded=restore_recorded)

        self.push_screen(modal, done)

    @staticmethod
    def _restore_recorded(run_dir: Path, story_key: str) -> bool:
        """True when resolution.json records — or, being unreadable, MAY record —
        a restore_patch. The TUI re-arm path is a plain from-scratch re-drive
        (only the CLI resolve flow honors the latch, because a stale marker is
        indistinguishable from a fresh one here), so a recorded restore must be
        surfaced rather than silently dropped."""
        if not resolve.resolution_path(run_dir, story_key).is_file():
            return False
        try:
            doc = resolve.read_resolution(run_dir, story_key)
        except resolve.ResolutionError:
            return True  # can't prove it carries no restore — surface the warning
        return bool(doc and doc.get("restore_patch"))

    # --------------------------------------------------- shared pause code paths

    def _do_resume(self, run_id: str) -> None:
        """Resume a paused run — the `bmad-loop resume` / `e` path, minus the
        confirm modal (the viewer was the confirmation). Guards tmux + a
        possibly-live engine so an approve/continue can't double-drive."""
        if self._mux_missing():
            return
        run_dir = self.project / RUNS_DIR / run_id
        if _engine_possibly_live(run_dir):
            self.notify(f"run {run_id} may still be live — stop it first", severity="warning")
            return
        try:
            launch.resume_detached(self.project, run_id)
        except launch.LaunchError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"resume of {run_id} launched (control session {launch.CTL_SESSION})")

    def _do_replan(self, run_id: str, spec_path: Path) -> None:
        """Request-replan: reset the planned spec to draft + strip its Auto Run
        Result, then resume — the next dispatch re-enters step-02 planning. Uses
        the same devcontract primitives the engine's repair path uses."""
        # Guard a possibly-live engine BEFORE mutating the spec — a draft-reset +
        # strip under a still-running session would race its writes (the rearm path
        # already checks liveness first; match it so replan can't corrupt a live
        # drive, and only then does _do_resume re-check before relaunching).
        run_dir = self.project / RUNS_DIR / run_id
        if self._resolve_blocked_by_liveness(run_id, run_dir):
            return
        try:
            reset = devcontract.reset_spec_status(spec_path, "draft")
            devcontract.strip_auto_run_result(spec_path)
        except OSError as e:
            self.notify(f"replan failed: {e}", severity="error")
            return
        if not reset:
            # honor the reset bool: nothing was flipped (the spec has no frontmatter
            # status, or is already draft), so the next dispatch would NOT re-enter
            # planning. Surface it instead of a misleading "reset" notice + resume.
            self.notify(
                "replan: could not reset the plan to draft (no frontmatter status?) "
                "— not resuming",
                severity="error",
            )
            return
        self.notify("plan reset to draft — the next dispatch re-plans")
        self._do_resume(run_id)

    def _do_rearm(
        self, run_id: str, run_dir: Path, story_key: str, *, restore_recorded: bool = False
    ) -> None:
        """Re-arm a resolved escalation + resume — the `resolve --no-interactive`
        path (rearm_escalation handles sentinel auto-delete-with-preservation)."""
        if self._resolve_blocked_by_liveness(run_id, run_dir):
            return
        try:
            runs.rearm_escalation(run_dir, story_key)
        except RearmError as e:
            self.notify(f"re-arm failed: {e}", severity="error")
            return
        if restore_recorded:
            self.notify(
                "recorded restore patch NOT honored — this re-arm re-drives from "
                "scratch (only `bmad-loop resolve` applies a restore)",
                severity="warning",
            )
        self.notify(f"re-armed {story_key}")
        self._do_resume(run_id)

    def _resolve_blocked_by_liveness(self, run_id: str, run_dir: Path) -> bool:
        if _engine_possibly_live(run_dir):
            self.notify(f"run {run_id} may still be live — stop it first", severity="warning")
            return True
        return False

    # ---------------------------------------------------- pause-context readers

    def _paused_spec(self, state: RunState) -> tuple[Path | None, str]:
        """(spec path, spec text) for the paused story, or (None, "") when the
        task has no spec file (e.g. an ambiguous-match escalation)."""
        task = state.tasks.get(state.paused_story_key) if state.paused_story_key else None
        if task is None or not task.spec_file:
            return None, ""
        path = Path(task.spec_file)
        try:
            return path, path.read_text(encoding="utf-8")
        except OSError:
            return path, ""

    def _story_subtitle(self, state: RunState) -> Text:
        key = state.paused_story_key or "?"
        title = self._story_context(state, key)[0]
        text = Text(key, style="bold")
        if title:
            text.append(f" — {title}")
        return text

    def _story_context(self, state: RunState, key: str) -> tuple[str, str]:
        """(title, description) from stories.yaml in stories mode, else ("", "")."""
        if state.source != "stories" or not state.spec_folder:
            return "", ""
        try:
            folder = stories.resolve_spec_folder(self.project, state.spec_folder)
            entry = stories.load_stories(folder).get(key)
        except stories.StoriesError:
            return "", ""
        return (entry.title, entry.description) if entry else ("", "")

    def _sentinel_kind(self, state: RunState, key: str) -> str:
        if state.source != "stories" or not state.spec_folder:
            return ""
        # resolve_story_spec globs + reads frontmatter; a file removed mid-scan (a
        # re-arm clearing the sentinel while the viewer refreshes) can raise OSError.
        # Degrade to "" rather than let a race-window read crash the render.
        try:
            folder = stories.resolve_spec_folder(self.project, state.spec_folder)
            st = stories.resolve_story_spec(folder, key)
        except OSError:
            return ""
        return st.sentinel_kind if st.kind == stories.KIND_SENTINEL else ""

    @staticmethod
    def _blocking_condition(spec_text: str) -> str:
        """The `## Auto Run Result` block a blocked spec records its halt in."""
        idx = spec_text.find("## Auto Run Result")
        return spec_text[idx:].strip() if idx != -1 else ""

    def _commit_subject(self, sha: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.project), "log", "-1", "--format=%s", sha],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # ------------------------------------------------------ stop / delete / archive

    def _selected_run_dir(self) -> tuple[str, Path] | None:
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return None
        return run_id, self.project / RUNS_DIR / run_id

    def action_stop_run(self) -> None:
        if self._mux_missing():
            return
        selected = self._selected_run_dir()
        if selected is None:
            return
        run_id, run_dir = selected
        if not data.liveness(run_dir) == "alive":
            self.notify(f"run {run_id} is not live", severity="warning")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._stop_run_worker(run_id, run_dir)

        self.push_screen(
            ConfirmModal("stop run", f"stop run {run_id}?", confirm_label="stop"), done
        )

    @work(thread=True, group="lifecycle")
    def _stop_run_worker(self, run_id: str, run_dir: Path) -> None:
        try:
            runs.stop_run(run_dir)
            launch.kill_ctl_window(run_id)
        except (OSError, StopRunError, ProcessHostError) as e:
            self.call_from_thread(self.notify, f"stop failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, f"run {run_id} stopped")

    def action_graceful_stop_run(self) -> None:
        """Ask the selected live run to stop *gracefully*: finish the in-flight item
        (story dev/review/commit, or a sweep bundle through commit), then finalize
        cleanly and stop — resumable, unlike the hard SIGTERM `x` delivers.

        Deliberately no `_mux_missing` gate: unlike `x` (which kills the agent
        window) this touches no multiplexer — the request is a control file the
        engine polls at item boundaries — so it must work even with the backend
        down. The liveness gate is also deliberately looser than `x`'s: it rejects
        only a *provably dead* engine, so an unverifiable (`unknown`) pid — a win32
        access-denied pid, a psmux backend, a run on another host — still lodges the
        request, matching `runs.request_graceful_stop`'s `requested-unverifiable`
        path (the request stands and fires if an engine is in fact running)."""
        selected = self._selected_run_dir()
        if selected is None:
            return
        run_id, run_dir = selected
        if data.liveness(run_dir) == "dead":
            self.notify(f"run {run_id} is not live", severity="warning")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._graceful_stop_worker(run_id, run_dir)

        self.push_screen(
            ConfirmModal(
                "graceful stop",
                f"stop run {run_id} after the current item finishes?\n"
                "the in-flight story/bundle completes through commit, then the run "
                "finalizes and stops (resumable). `x` stops immediately instead.",
                confirm_label="graceful stop",
            ),
            done,
        )

    @work(thread=True, group="lifecycle")
    def _graceful_stop_worker(self, run_id: str, run_dir: Path) -> None:
        # The TUI is an observer: it only writes the control file via the runs
        # helper (atomic tmp + replace) — it never signals the engine, shells out,
        # or writes the journal. request_graceful_stop returns a status token to
        # message on; every UI update from this thread marshals through
        # call_from_thread (worker threads must not touch widgets directly).
        try:
            outcome = runs.request_graceful_stop(run_dir)
        except runs.GracefulStopError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        if outcome == "already-pending":
            self.call_from_thread(self.notify, f"run {run_id} already has a graceful stop pending")
            return
        if outcome == "requested-unverifiable":
            self.call_from_thread(
                self.notify,
                f"run {run_id}: could not confirm a live engine (unverifiable pid) — "
                "the request stands and fires if one is running",
                severity="warning",
            )
            return
        self.call_from_thread(
            self.notify,
            f"graceful stop requested — run {run_id} will stop after the current item "
            f"completes; continue later with `bmad-loop resume {run_id}`",
        )

    def action_delete_run(self) -> None:
        selected = self._selected_run_dir()
        if selected is None:
            return
        run_id, run_dir = selected
        # 'unknown' (a live-but-unreadable pid) does not block cleanup — see the
        # deliberate runs.engine_alive invariant — but the irreversible confirm
        # must not imply the run is safely dead, so it says so.
        live = data.liveness(run_dir)
        if live == "alive":
            self.notify(f"run {run_id} is live — stop it first", severity="warning")
            return
        warning = "this cannot be undone"
        if live == "unknown":
            warning = f"engine may still be live (unverifiable pid) — {warning}"

        def done(ok: bool | None) -> None:
            if ok:
                self._delete_run_worker(run_id, run_dir)

        self.push_screen(
            ConfirmModal(
                "delete run",
                f"permanently delete run {run_id}?",
                confirm_label="delete",
                warning=warning,
            ),
            done,
        )

    @work(thread=True, group="lifecycle")
    def _delete_run_worker(self, run_id: str, run_dir: Path) -> None:
        try:
            runs.delete_run(run_dir)
        except OSError as e:
            self.call_from_thread(self.notify, f"delete failed: {e}", severity="error")
            return
        self.call_from_thread(self._dashboard.forget_run, run_id)
        self.call_from_thread(self.notify, f"run {run_id} deleted")

    def action_archive_run(self) -> None:
        selected = self._selected_run_dir()
        if selected is None:
            return
        run_id, run_dir = selected
        live = data.liveness(run_dir)
        if live == "alive":
            self.notify(f"run {run_id} is live — stop it first", severity="warning")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._archive_run_worker(run_id, run_dir)

        self.push_screen(
            ConfirmModal(
                "archive run",
                f"archive run {run_id} to .bmad-loop/archive?",
                confirm_label="archive",
                warning=(
                    "engine may still be live (unverifiable pid)" if live == "unknown" else None
                ),
            ),
            done,
        )

    @work(thread=True, group="lifecycle")
    def _archive_run_worker(self, run_id: str, run_dir: Path) -> None:
        try:
            dest = runs.archive_run(self.project, run_dir)
        except OSError as e:
            self.call_from_thread(self.notify, f"archive failed: {e}", severity="error")
            return
        self.call_from_thread(self._dashboard.forget_run, run_id)
        self.call_from_thread(self.notify, f"run {run_id} archived to {dest}")

    def action_cleanup_sessions(self) -> None:
        if self._mux_missing():
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._cleanup_sessions_worker()

        self.push_screen(
            ConfirmModal(
                "cleanup sessions",
                "remove tmux sessions/windows for finished & stopped runs?",
                confirm_label="cleanup",
            ),
            done,
        )

    @work(thread=True, group="lifecycle")
    def _cleanup_sessions_worker(self) -> None:
        # killed and unknown come from prune_sessions' single partition sample,
        # so the warning below only ever names sessions that were actually pruned
        killed, _live, unknown = runs.prune_sessions(self.project)
        # prune_ctl_windows probes has_session on the shared ctl session, a
        # raiser-side call; on a worker thread the toast must be marshalled, and
        # notify() must not be called directly (see _mux_guarded — foreground only).
        try:
            windows = launch.prune_ctl_windows(self.project)
        except MultiplexerError as e:
            # prune_sessions already killed the agent sessions above; surface the
            # ctl-window failure but keep reporting that completed work (and the
            # unknown-pid warning) rather than swallowing it on an early return.
            self.call_from_thread(self.notify, str(e), severity="error")
            windows = []
        if unknown:
            self.call_from_thread(
                self.notify,
                f"{len(unknown)} pruned session(s) had an unverifiable engine pid "
                f"(may still be live): {', '.join(sorted(unknown))}",
                severity="warning",
            )
        self.call_from_thread(
            self.notify,
            f"removed {len(killed)} session(s), {len(windows)} window(s)",
        )

    def action_validate(self) -> None:
        self._show_validate()

    @work(thread=True, exclusive=True, group="captured")
    def _show_validate(self) -> None:
        """Preflight in a findings modal, degrading to the text one (#210).

        A sibling of _show_captured rather than a change to it: that worker still
        serves the two dry runs, which have no document to parse.

        The transport is the subprocess and `--json`, not documents.py's builders
        in-process, which its module docstring otherwise asks a non-CLI frontend
        to prefer. Knowing exception: cmd_validate imports third-party mux entry
        points and probes httpx, so the subprocess quarantines a broken plugin's
        import side effects and leaves the TUI's own lru_cached mux selection
        undisturbed. Extracting an in-process builder is a follow-up.

        The body is guarded because @work(thread=True) defaults to
        exit_on_error=True: a JSONDecodeError or a KeyError escaping here would
        take the whole app down, not just this modal. exit_on_error=False is not
        the fix — that trades the crash for pressing `v` and nothing happening.

        The degrade **re-runs validate in text mode** rather than showing the
        captured JSON. Dumping the document would withhold a perfectly good human
        rendering at the exact moment the structural one failed, and hand the
        reader a wall of `{"schema_version": ...}` instead. One sub-second
        subprocess on a path that should never fire buys a degrade that is
        byte-for-byte the pre-#210 behavior.

        That re-run goes through _run_captured_guarded rather than calling
        run_captured directly: the except above does not cover it, and the two
        legs spawn the same subprocess, so a failure to spawn at all is not a
        JSON-leg failure a text re-run recovers from — it is the same failure
        twice, the second one escaping into exit_on_error.
        """
        tail = ["validate", "--project", str(self.project)]
        try:
            _rc, out, _err = launch.run_captured_streams([*tail, "--json"])
            doc = widgets.validate_document(out)
        except Exception:  # noqa: BLE001 — a JSON-leg failure degrades, never kills the app
            doc = None
        if doc is None:
            rc, merged = self._run_captured_guarded(tail)
            screen = TextOutputModal("validate", rc, merged)
        else:
            screen = ValidateFindingsModal(doc)
        self.call_from_thread(self.push_screen, screen)

    def _run_captured_guarded(self, tail: list[str]) -> tuple[int, str]:
        """run_captured, with a failure to spawn rendered as output, not raised.

        Every caller is a @work(thread=True) body, and that decorator defaults to
        exit_on_error=True: an OSError out of subprocess.run — a deleted venv
        under sys.executable, EAGAIN off a loaded process table — would escape
        the worker and take the whole app down rather than this one modal.

        The reason goes in the body rather than a notify() because the modal is
        already opening; a blank panel over an `exit 1` header would say only
        that something went wrong. The header carries which command it was, so
        the body does not repeat it.
        """
        try:
            return launch.run_captured(tail)
        except Exception as exc:  # noqa: BLE001 — a failed spawn is a modal, not a crash
            return 1, f"could not run: {exc}"

    @work(thread=True, exclusive=True, group="captured")
    def _show_captured(self, title: str, tail: list[str]) -> None:
        rc, out = self._run_captured_guarded(tail)
        self.call_from_thread(self.push_screen, TextOutputModal(title, rc, out))

    def action_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            return
        try:
            doc = PolicyDoc.load(self.project / POLICY_FILE)
        except ParseError as e:
            self.notify(f"policy.toml is not valid TOML: {e}", severity="error")
            return
        self.push_screen(SettingsScreen(self.project, doc))


def run_tui(project: Path) -> int:
    # Trip the once-per-process forced-backend warning while stderr is still the
    # real terminal: Textual captures sys.stderr for the app's whole run, so a
    # first firing inside the app (any observer gate) would consume the single
    # emission invisibly. Selection errors stay loud at their real call sites.
    try:
        mux_usable()
    except MultiplexerError:
        pass
    BmadLoopApp(project).run()
    return 0
