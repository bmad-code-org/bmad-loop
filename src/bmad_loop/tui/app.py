"""`bmad-loop tui` application shell.

Observer/launcher only: the TUI never runs engines in-process. Run control
(r/s/e) launches detached bmad-loop processes in the bmad-loop-ctl tmux
session via tui.launch; validate and dry runs are captured into a modal.
The g binding opens the policy.toml settings editor.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from textual import work
from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from tomlkit.exceptions import ParseError

from .. import bmadconfig, decisions, runs, verify
from ..journal import load_state
from ..policy import POLICY_FILE
from ..process_host import ProcessHostError
from ..runs import RUNS_DIR, StopRunError
from . import data, launch
from .screens.dashboard import DashboardScreen
from .screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    DecisionModal,
    StartRunModal,
    StartSweepModal,
    TextOutputModal,
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


class BmadLoopApp(App[None]):
    TITLE = "bmad-loop"

    CSS = """
    #left {
        width: 34;
        border-right: solid $primary-darken-2;
    }
    #runs {
        height: 2fr;
        min-height: 4;
        border-top: solid $primary-darken-2;
    }
    #runs, #sprint-tree, #deferred {
        border-title-color: $text;
        border-title-style: bold;
    }
    #sprint-tree {
        height: 3fr;
        min-height: 4;
        border-top: solid $primary-darken-2;
    }
    #deferred {
        height: 2fr;
        min-height: 4;
        /* strip OptionList's default tall border + padding so the pane sits
           flush with the runs table and sprint tree above it */
        border: none;
        border-top: solid $primary-darken-2;
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
        Binding("R", "resolve_run", "resolve"),
        Binding("d", "answer_decisions", "decisions"),
        Binding("a", "attach", "attach"),
        Binding("x", "stop_run", "stop"),
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

    def _tmux_missing(self) -> bool:
        if launch.tmux_available():
            return False
        self.notify("tmux not found on PATH — launch/attach disabled", severity="error")
        return True

    def _guarded(self, go: Callable[[], None]) -> None:
        """Pre-launch guard mirroring the CLI: clean worktree required, plus a
        confirm when another engine is already live."""
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
        if self._tmux_missing():
            return
        self.push_screen(StartRunModal(), self._start_run_result)

    def _start_run_result(self, result: dict | None) -> None:
        if not result:
            return
        if result["dry_run"]:
            tail = ["run", "--project", str(self.project), "--dry-run"]
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
                    epic=result["epic"],
                    story=result["story"],
                    max_stories=result["max_stories"],
                )
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"run {run_id} launched (tmux session {launch.CTL_SESSION})")
            self._dashboard.expect_run(run_id)

        self._guarded(go)

    def action_start_sweep(self) -> None:
        if self._tmux_missing():
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
            self.notify(f"sweep {run_id} launched (tmux session {launch.CTL_SESSION})")
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
        if self._tmux_missing():
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
            self.notify(f"resume of {run_id} launched (tmux session {launch.CTL_SESSION})")

        self.push_screen(ConfirmResumeModal(run_id, state, engine_alive), done)

    def action_attach(self) -> None:
        if self._tmux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        session = runs.session_name(run_id)
        window = launch.ctl_window(run_id)
        agent_live = launch.session_exists(session)
        # A sweep blocked on a decision prompt has no agent session — the
        # human answers in the orchestrator's ctl window. Otherwise prefer the
        # live agent session, falling back to the ctl window between sessions.
        if window is not None and (self._dashboard.decision_pending is not None or not agent_live):
            launch.select_ctl_window(window)
            self._attach_to_target(
                f"={launch.CTL_SESSION}",
                return_window=f"={launch.CTL_SESSION}:{window}",
            )
            return
        elif agent_live:
            target = f"={session}"
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
        argv = runs.attach_target_argv(target)
        if os.environ.get("TMUX"):
            # switch-client is fire-and-forget; record our own pane on the ctl
            # window so its trailing shell switches the client back here when it
            # exits, instead of stranding the user in the control session.
            if return_window is not None:
                pane = launch.current_pane_id()
                if pane is not None:
                    launch.set_return_pane(return_window, pane)
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
        if self._tmux_missing():
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

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                win_id = launch.start_resolve_detached(self.project, run_id)
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            if not win_id:
                self.notify(
                    "resolve launched but its window id was not captured",
                    severity="error",
                )
                return
            launch.select_ctl_window_id(win_id)
            self._attach_to_target(f"={launch.CTL_SESSION}", return_window=win_id)

        self.push_screen(
            ConfirmModal(
                "resolve escalation",
                f"open the resolve agent for {story}?\n"
                "converse to fix the frozen spec, then confirm re-arm + resume in that window.",
                confirm_label="resolve",
            ),
            done,
        )

    # ------------------------------------------------------ stop / delete / archive

    def _selected_run_dir(self) -> tuple[str, Path] | None:
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return None
        return run_id, self.project / RUNS_DIR / run_id

    def action_stop_run(self) -> None:
        if self._tmux_missing():
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
        if self._tmux_missing():
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
        windows = launch.prune_ctl_windows(self.project)
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
        self._show_captured("validate", ["validate", "--project", str(self.project)])

    @work(thread=True, exclusive=True, group="captured")
    def _show_captured(self, title: str, tail: list[str]) -> None:
        rc, out = launch.run_captured(tail)
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
    BmadLoopApp(project).run()
    return 0
