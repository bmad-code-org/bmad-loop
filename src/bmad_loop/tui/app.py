"""`bmad-loop tui` application shell.

Observer/launcher only: the TUI never runs engines in-process. Run control
(r/s/e) launches detached bmad-loop processes in the control session via
tui.launch (bmad-loop-ctl on tmux; a per-registry name on psmux, which the
launch toasts print). Dry runs are captured into a text modal; validate
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
from typing import Any, TypeVar

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
    StoryTask,
)
from ..platform_util import resolve_or_lexical
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
    PauseReasonModal,
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
        self.project = resolve_or_lexical(project)
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
        """Pre-launch guard mirroring the CLI: the git support floor refused first,
        then the #414 isolation/repo_root conflict, then a clean worktree required,
        plus a confirm when another engine is already live."""
        # First, in `cmd_run`'s own order, and for the reason that order exists: this
        # is a fact about the HOST, so every other answer here would be advice about
        # the wrong thing. Without it the operator got the generic "launch may have
        # failed — attach to control session" toast the dashboard raises 10s later,
        # which names neither git nor the floor and points at a pane to go read.
        #
        # `timeout_s` because this runs on the event loop — the same reason the
        # commit-subject probe below carries one (`_commit_subject`) — and
        # that bound is exactly why a `GitError` here FALLS THROUGH to launch instead
        # of refusing: 5s is not the deadline the detached CLI applies, so a merely
        # slow git would otherwise be refused by a toast on a host the CLI would run
        # on. The guard never authorizes anything — `_reject_under_floor_git` fails
        # closed on that same fault a moment later, in the process that matters —
        # so declining to PRE-EMPT a refusal costs nothing but a slower message.
        # Same disposition, same reasoning, as the unreadable-policy fall-through
        # below: the guard cannot tell "fine" from "could not look".
        try:
            found = verify.git_below_floor(self.project, timeout_s=5)
        except verify.GitError:
            found = None
        if found is not None:
            self.notify(verify.under_floor_git_message(found), severity="error")
            return
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
        letting a raw `UnicodeDecodeError` past this handler.

        `ParseError` is not in the tuple: `policy.load` parses with `tomllib`, so
        the tomlkit error can only arrive from the `PolicyDoc` path in
        :meth:`action_settings`, which catches it there."""
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
            self.notify(
                f"run {run_id} launched (control session {launch.ctl_session(self.project)})"
            )
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
            self.notify(
                f"sweep {run_id} launched (control session {launch.ctl_session(self.project)})"
            )
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
        # decision/option cross the widget boundary as `object`; their runtime types
        # are the Decision/DecisionOption that apply_pre_answer and `.id` expect.
        try:
            decisions.apply_pre_answer(
                self.project,
                decision,  # pyright: ignore[reportArgumentType]
                option,  # pyright: ignore[reportArgumentType]
                date=time.strftime("%Y-%m-%d"),
            )
        except (OSError, bmadconfig.BmadConfigError, ValueError, runs.StateRootError) as e:
            # ValueError is the ledger writers' date precondition; it cannot fire
            # from the strftime above. StateRootError is reachable: the ledger
            # write now takes a cross-process lock whose sidecar lives under the
            # state root (#286/#469), and an environment that names no usable root
            # raises it — it is NOT an OSError, so the tuple has to say so.
            # OSError covers the acquisition itself failing against a live holder.
            # Every one of them uncaught here escapes into the Textual event loop
            # and takes the dashboard down mid-walk — the per-decision
            # notification is the right degradation for a modal the human is still
            # stepping through, and the walk continues to the next decision.
            self.notify(
                f"failed to record {decision.id}: {e}",  # pyright: ignore[reportAttributeAccessIssue]
                severity="error",
            )
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
            # Re-check liveness at confirm time via the shared guard — the modal's
            # warning is display-only and sampled at open time, so route through
            # _do_resume (like the e/viewer and re-arm paths) rather than launching
            # blind; a newly-live engine is caught even if the user confirmed.
            if ok:
                self._do_resume(run_id)

        self.push_screen(ConfirmResumeModal(run_id, state, engine_alive), done)

    def action_attach(self) -> None:
        if self._mux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        session = runs.session_name(run_id)
        win_id = launch.ctl_window_id(self.project, run_id)
        ok, agent_live = self._mux_guarded(lambda: launch.session_exists(session))
        if not ok:
            return
        # A sweep blocked on a decision prompt has no agent session — the
        # human answers in the orchestrator's ctl window. Otherwise prefer the
        # live agent session, falling back to the ctl window between sessions.
        if win_id is not None and (self._dashboard.decision_pending is not None or not agent_live):
            launch.select_ctl_window_id(win_id)
            self._attach_to_target(launch.ctl_target(self.project), return_window=win_id)
            return
        elif agent_live:
            target = runs.session_target(run_id)
        else:
            self.notify(
                f"nothing to attach: no live agent session ({session}) and no "
                f"{launch.ctl_session(self.project)} window for this run (runs started outside "
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
        # argv is None only when `ok` is False (they are a pair); past this guard it
        # is a real argv, but pyright can't correlate the two — hence the
        # reportArgumentType ignores on the subprocess.call/shlex.join uses below.
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
            subprocess.call(argv)  # pyright: ignore[reportArgumentType]
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
                subprocess.call(argv)  # pyright: ignore[reportArgumentType]
        except SuspendNotSupported:
            self.notify(
                f"cannot suspend here — run manually: {shlex.join(argv)}",  # pyright: ignore[reportArgumentType]
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
        if not launch.ctl_window_recorded(self.project, run_id, win_id):
            # Not an error and not a reason to abort: this attach targets the id
            # in hand, so the resolve session itself is reached correctly. What
            # is lost is the record *later* verbs read, so `a`/`x` after this
            # window is minted may answer an older one (#482's symptom).
            self.notify(
                "resolve launched but its window id was not recorded — "
                "later attach/stop may target an older window for this run",
                severity="warning",
            )
        launch.select_ctl_window_id(win_id)
        self._attach_to_target(launch.ctl_target(self.project), return_window=win_id)

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
        spec_path, spec_text, readable = self._paused_spec(state)
        modal = SpecReviewModal(
            title="plan checkpoint — review the planned spec before implementation",
            subtitle=self._story_subtitle(state),
            spec_path=spec_path,
            spec_text=spec_text,
            unreadable=not readable,
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
                self._do_replan(run_id, spec_path, self._paused_spec_root(state))

        self.push_screen(modal, done)

    def _review_gate(self, run_id: str, run_dir: Path, state: RunState) -> None:
        label = widgets.pause_label(state.paused_stage or "")[0] or "gate"
        spec_path, spec_text, readable = self._paused_spec(state)

        def done(verb: str | None) -> None:
            if verb == "resume":
                self._do_resume(run_id)

        if spec_path is None:
            # Spec-less gates: story-gate fires before the story is registered in
            # state.tasks (deliberate, so a resume re-picks and re-asks the ledger)
            # and epic-boundary has no story key. The pause reason is the payload.
            subtitle = (
                self._story_subtitle(state)
                if state.paused_story_key
                else Text(f"run {run_id}", style="bold")
            )
            self.push_screen(
                PauseReasonModal(
                    title=f"{label} — pause reason",
                    subtitle=subtitle,
                    reason=state.paused_reason or "",
                ),
                done,
            )
            return
        modal = SpecReviewModal(
            title=f"{label} — review the finalized spec",
            subtitle=self._story_subtitle(state),
            spec_path=spec_path,
            spec_text=spec_text,
            unreadable=not readable,
            actions=[("resume", "Approve & resume", "primary")],
        )
        self.push_screen(modal, done)

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
        spec_path, spec_text, readable = self._paused_spec(state)
        title, description = self._story_context(state, story_key)
        restore_recorded = self._restore_recorded(run_dir, story_key)
        modal = EscalationModal(
            story_key=story_key,
            title=title,
            description=description,
            # `_blocking_condition` reduces the read-failure body to "" like any
            # other text without a halt block, so an unreadable spec would render
            # "(no blocking condition recorded)" — indistinguishable from a spec that
            # was read fine and simply halted without one. The verdict has to be
            # carried in, and it also REFUSES both verbs: re-arm flips the spec's
            # frontmatter, strips its result and re-stamps the baseline, which is not
            # an action to take on evidence nobody could read.
            blocking=self._blocking_condition(spec_text),
            unreadable=not readable,
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
        possibly-live engine so an approve/continue can't double-drive. No
        control-alias gate here: this path mutates nothing before the launch,
        and the launcher itself refuses at the mutation's chokepoint
        (`launch.start_detached`) — the LaunchError lands in the except below."""
        if self._mux_missing():
            return
        run_dir = self.project / RUNS_DIR / run_id
        if _engine_possibly_live(run_dir):
            self.notify(f"run {run_id} may still be live — stop it first", severity="warning")
            return
        try:
            win_id = launch.resume_detached(self.project, run_id)
        except launch.LaunchError as e:
            self.notify(str(e), severity="error")
            return
        if not win_id:
            # The resume itself is running; only the disambiguation record is
            # lost, so `a`/`x` may target an older same-run_id window (#482's
            # symptom). Warn instead of masking it behind the success toast.
            # "not recorded", not "not captured": resume_detached reports the
            # uncaptured id and the unwritten record through this one signal
            # because they leave the operator in the same place.
            self.notify(
                "resume launched but its window id was not recorded — "
                "attach/stop may target an older window for this run",
                severity="warning",
            )
        self.notify(
            f"resume of {run_id} launched (control session {launch.ctl_session(self.project)})"
        )

    def _do_replan(self, run_id: str, spec_path: Path, confine_root: Path) -> None:
        """Request-replan: reset the planned spec to draft + strip its Auto Run
        Result, then resume — the next dispatch re-enters step-02 planning. Uses
        the same devcontract primitives the engine's repair path uses.

        `confine_root` arrives from the caller (`_paused_spec_root`) rather than being
        `self.project` here: this method has no task in scope, and the root these two
        writers validate against must be the SAME claim about which tree owns the spec
        that `_paused_spec` anchored the path on. `runs.task_spec_root`'s docstring
        carries the rationale — a `confine_root` that disagrees with the anchor is not
        REFUSED, it silently drops both writes to the plain no-follow arm and loses the
        confined arm's O_NOFOLLOW walk (#593) with no signal at all."""
        # Guard a possibly-live engine BEFORE mutating the spec — a draft-reset +
        # strip under a still-running session would race its writes (the rearm path
        # already checks liveness first; match it so replan can't corrupt a live
        # drive, and only then does _do_resume re-check before relaunching).
        # The control-alias gate sits equally early: the child `bmad-loop resume`
        # would refuse such a run anyway, and a spec rewritten ahead of that
        # refusal is the mutate-then-refuse shape the CLI entry gates closed.
        if self._blocked_by_control_alias(run_id):
            return
        run_dir = self.project / RUNS_DIR / run_id
        if self._resolve_blocked_by_liveness(run_id, run_dir):
            return
        if not spec_path.is_file():
            # `reset_spec_status` returns False for an ABSENT spec and for one with no
            # frontmatter status alike, and the shared notice below blamed the
            # frontmatter for both. Now that the path is re-anchored on the run's own
            # tree, an absent spec is the signal that the ANCHORING is wrong, so it
            # earns its own message naming the path actually consulted.
            self.notify(f"replan: no spec at {spec_path} — not resuming", severity="error")
            return
        try:
            reset = devcontract.reset_spec_for_replan(spec_path, confine_root=confine_root)
        except (OSError, UnicodeDecodeError, verify.FrontmatterWriteError) as e:
            # FrontmatterWriteError is not an OSError: a spec whose `status:` is a
            # block scalar or a flow mapping reads fine and fails the WRITE. It
            # lands in the same notice as a permissions failure because it has the
            # same shape for the operator — the replan did not happen and the run
            # is not resumed — and because an uncaught raise inside a Textual
            # worker takes the dashboard down instead of saying so.
            #
            # UnicodeDecodeError is a ValueError, so neither sibling arm caught it and
            # `reset_spec_status` decodes STRICTLY (`read_bytes().decode("utf-8")`).
            # That raise became reachable when `_paused_spec` started degrading a
            # non-UTF-8 spec in place instead of raising at render: the operator can now
            # open the modal on one and press replan, which is precisely the event-loop
            # crash the read-side fix exists to prevent.
            self.notify(f"replan failed: {e}", severity="error")
            return
        if not reset:
            # honor the reset bool: nothing was flipped (the spec has no frontmatter
            # status, or is already draft), so the next dispatch would NOT re-enter
            # planning. Surface it instead of a misleading "reset" notice + resume.
            self.notify(
                "replan: could not reset the plan to draft (no frontmatter status?) — not resuming",
                severity="error",
            )
            return
        self.notify("plan reset to draft — the next dispatch re-plans")
        self._do_resume(run_id)

    def _echo_rearm_events(self, run_dir: Path, before: list[dict[str, Any]] | None) -> None:
        """Toast the re-arm records `cli._echo_rearm_events` prints, same table.

        Reads through `runs.journal_entries_or_none`, shared with the CLI so the two
        surfaces cannot drift on robustness the way they drifted on routing. Both ends
        of the diff must be readable: a failed FIRST read degraded to `[]` would set the
        watermark to zero and replay every historical record as a fresh toast, so an
        unreadable journal costs the echo and keeps the gesture.

        The table's `next_step` is deliberately dropped: it reads "... before
        resuming", and this path resumes in the same gesture.

        This is abort-only diagnostic recovery. A raised call has no authoritative
        outcome, so this path must not infer a hold from partial journal residue.
        """
        after = runs.journal_entries_or_none(run_dir)
        if before is None or after is None:
            return
        for entry in after[len(before) :]:
            notice = runs.rearm_event_notice(entry)
            if notice is None:
                continue
            severity, message, _next_step = notice
            self.notify(message, severity="warning" if severity == "warning" else "information")

    def _echo_rearm_notices(self, notices: tuple[runs.RearmNotice, ...]) -> None:
        """Toast a successful re-arm's authoritative notices in append order."""
        for notice in notices:
            self.notify(
                notice.message,
                severity="warning" if notice.severity == "warning" else "information",
            )

    def _do_rearm(
        self, run_id: str, run_dir: Path, story_key: str, *, restore_recorded: bool = False
    ) -> None:
        """Re-arm a resolved escalation + resume — the `resolve --no-interactive`
        path (rearm_escalation handles sentinel auto-delete-with-preservation)."""
        # Ahead of rearm_escalation for the same reason cmd_resolve gates at
        # entry: a run left re-armed-but-not-running by the child's refusal.
        if self._blocked_by_control_alias(run_id):
            return
        if self._resolve_blocked_by_liveness(run_id, run_dir):
            return
        # The LIVE isolation mode, read once and used twice below. `runs.rearm_escalation`
        # requires it: how the re-drive WILL run is a policy question, and the recorded
        # `task.worktree_path` answers only how the escalated attempt ran — the two part
        # company on exactly the mid-run policy edit the conflict check below is also
        # about.
        #
        # Unreadable REFUSES here, unlike the launch guard above and unlike this block's
        # own previous disposition. That fall-through was correct while the policy fed
        # one optional CHECK: "no conflict" and "could not look" are different answers
        # and neither blocks a launch the detached CLI will re-read the same file for.
        # It is not correct for an INPUT to a repair write. Without the mode this
        # gesture cannot say which ref the re-drive reads, so it would flip the spec and
        # then tell the operator to put the correction in a tree picked by a default —
        # silently, and unrecoverably, since a re-arm consumes the escalation.
        # `cli.cmd_resolve` raises on the same unreadable file before it re-arms.
        try:
            isolation = policy.load(self.project / POLICY_FILE).scm.isolation
        except (policy.PolicyError, OSError) as e:
            self.notify(
                f"cannot read policy.toml to determine the re-drive's isolation mode "
                f"({e}) — fix it, then re-arm; the story is still escalated",
                severity="error",
            )
            return
        # Same seam as `cli.cmd_resolve`, for the same reason and at the same moment:
        # `runs.rearm_escalation` reads the persisted code root back out of the run
        # state, and only a process that has just read config.yaml can tell whether a
        # `repo_root:` edit made while the run was paused has moved it. Resume re-stamps
        # it, but this gesture re-arms BEFORE it resumes, so the mirror has to be aimed
        # here or the re-arm advances the baseline in the tree the run has left.
        try:
            paths = bmadconfig.load_paths(self.project)
        except (bmadconfig.BmadConfigError, OSError) as e:
            self.notify(
                f"cannot read the project config to confirm the code root ({e}) — "
                "re-arming against the root this run recorded",
                severity="warning",
            )
        else:
            # Same hoist as `cli.cmd_resolve`, for the same reason: this gesture
            # re-arms and THEN resumes, so the isolation refusal the detached CLI makes
            # in `_resume_paused_run` landed after the re-stamp had persisted the
            # unsupported root and `rearm_escalation` had advanced the attempt baseline
            # against it. The operator saw "re-armed <story>" and then a pane that
            # refused, with the story no longer escalated for `resolve` to correct.
            #
            # Reads the mode hoisted above rather than loading policy.toml a second
            # time: two reads of one file in one gesture can disagree under a concurrent
            # edit, and the refusal must be about the same mode the re-arm is told.
            conflict = bmadconfig.worktree_isolation_conflict(paths, isolation)
            if conflict is not None:
                self.notify(conflict, severity="error")
                return
            if (moved := runs.restamp_code_root(run_dir, paths.repo_root)) is not None:
                self.notify(moved, severity="warning")
        before_entries = runs.journal_entries_or_none(run_dir)
        outcome: runs.RearmOutcome | None = None
        try:
            outcome = runs.rearm_escalation(
                run_dir,
                story_key,
                isolated_redrive=isolation == "worktree",
                # The live project this dashboard was launched against, matching
                # `cli.cmd_resolve`: the re-arm's spec writes have to land in the tree
                # the operator is looking at, not in the one the run recorded at launch.
                # `self.project` rather than `paths.project` because the `load_paths`
                # arm above may have degraded without binding `paths` at all.
                project_root=self.project,
                # DW-11. This gesture runs no resolve session, so it accepted nothing:
                # the escalation watermark must not advance. A `resolution.json` on
                # disk is NOT evidence to the contrary here — `_restore_recorded`
                # already records the governing fact for this surface, that a stale
                # marker is indistinguishable from a fresh one, which is why this path
                # declines the restore latch too. Stamping on its presence would bury
                # escalations raised since the marker was written.
                resolution_recorded=False,
            )
        except RearmError as e:
            self.notify(f"re-arm failed: {e}", severity="error")
            return
        finally:
            # In the `finally`, matching `cli.cmd_resolve`. `_stale_restore_residue`
            # journals BEFORE the re-stamp block that raises `RearmError`, so on that
            # path the records were already written and returning early threw them
            # away — including the commits PAIR (`stale-restore-commits` when the probe
            # answered, `rearm-commits-probe-failed` when it could not), whose whole
            # point is that nothing else will tell the human. This surface used to
            # `return` there while the CLI echoed, so the two DID drift on the abort
            # path even after they were unified on routing — and an abort is when the
            # residue matters most: the re-arm half-ran and the operator has to decide
            # what to do with the tree.
            if outcome is None:
                self._echo_rearm_events(run_dir, before_entries)
        assert outcome is not None
        self._echo_rearm_notices(outcome.notices)
        if restore_recorded:
            self.notify(
                "recorded restore patch NOT honored — this re-arm re-drives from "
                "scratch (only `bmad-loop resolve` applies a restore)",
                severity="warning",
            )
        self.notify(f"re-armed {story_key}")
        if outcome.hold_resume:
            # The half of the gesture that still worked is kept: the story IS re-armed
            # and persisted. What stops is the resume this surface folds in behind it,
            # because the warning above proved the re-drive would read a spec it cannot
            # route on and burn the escalation. Worded for a surface that drops
            # `next_step`, and worded as an instruction the operator can finish here —
            # the run stays paused and resumable from this same screen.
            self.notify(
                "not resuming: commit the corrected spec, then resume this run",
                severity="warning",
            )
            return
        self._do_resume(run_id)

    def _resolve_blocked_by_liveness(self, run_id: str, run_dir: Path) -> bool:
        if _engine_possibly_live(run_dir):
            self.notify(f"run {run_id} may still be live — stop it first", severity="warning")
            return True
        return False

    def _blocked_by_control_alias(self, run_id: str) -> bool:
        """Refuse to mutate persisted state for a run whose id aliases a
        control session (`ctl`, `ctl-<16 hex>` — the CLI's resume/resolve
        gates, mirrored): the launch it would end in is refused at the
        mutation chokepoint (`launch.start_detached`), so a spec reset or an
        escalation re-arm performed FIRST would strand the run in the mutated
        state. Only the paths that mutate before launching need this —
        `_do_replan` (spec draft-reset/strip) and `_do_rearm`
        (rearm_escalation); plain resume/resolve mutate nothing early and are
        covered by the launcher's own gate."""
        if runs.run_id_aliases_control_session(run_id):
            self.notify(
                f"run {run_id}: its agent session name is the control session's own — "
                "cannot be driven. Recover its work by hand, then `bmad-loop delete "
                f"{run_id}`",
                severity="error",
            )
            return True
        return False

    # ---------------------------------------------------- pause-context readers

    def _paused_task(self, state: RunState) -> StoryTask | None:
        """The paused story's task, or None when nothing is paused.

        One lookup for both `_paused_spec` (which anchors the READ) and
        `_paused_spec_root` (which supplies the destructive write's `confine_root`).
        The whole point of routing both through `runs.task_spec_path`/`task_spec_root`
        is that the anchor and the root must name one tree; two copies of the lookup
        would let them drift on the very state that decides it."""
        return state.tasks.get(state.paused_story_key) if state.paused_story_key else None

    def _paused_spec(self, state: RunState) -> tuple[Path | None, str, bool]:
        """(spec path, spec text, readable) for the paused story, or (None, "", True)
        when the task has no spec file (e.g. an ambiguous-match escalation).

        `readable` is False only when the spec could not be READ at the anchored path,
        which is the signal that the anchoring is wrong. It is returned rather than left
        for the renderer to infer, because the alternative is sniffing the body for the
        failure sentence — the failure text and a spec that merely opens with the same
        words are not distinguishable after the fact, and one of them must not disable
        an operator's approve button.

        The path is re-anchored through `runs.task_spec_path`, never `Path(...)` on the
        raw value: `model.StoryTask._serialized_worktree_path` persists an isolated
        unit's spec RELATIVE to the mounted worktree root and `from_dict` reads it back
        raw, so a bare `Path(task.spec_file)` resolves against the TUI process cwd —
        where the main checkout carries the same implementation-artifacts-relative
        path and answers with the WRONG tree's copy of the story spec."""
        task = self._paused_task(state)
        if task is None or not task.spec_file:
            return None, "", True
        path = runs.task_spec_path(task, state)
        try:
            # `errors="replace"` for the same reason `_commit_subject` uses it: a story
            # spec is agent- or human-authored, so an odd byte is a fact about the file,
            # not a reason to withhold it. Decoding strictly here cost the reviewer the
            # WHOLE document at a gate whose only purpose is reading it — and, because
            # every review surface calls this from the Textual event loop, an escaping
            # UnicodeDecodeError (a ValueError, so no OSError arm catches it) took the
            # dashboard down rather than rendering the fault.
            return path, path.read_bytes().decode("utf-8", errors="replace"), True
        except OSError as e:
            # An absent spec at the ANCHORED path is the signal that the anchoring is
            # wrong, so it must not reduce to "" — SpecReviewModal renders that as
            # "(empty spec)", which is also what a present-but-blank spec renders as.
            # Report the failure as the body so the two cases read differently, and
            # keep this arm to ABSENCE now that a decode fault degrades in place.
            return path, f"(spec could not be read — {e})", False

    def _paused_spec_root(self, state: RunState) -> Path:
        """The tree the paused story's spec is anchored on — and confined to.

        The mirror of `_paused_spec`'s anchor, kept as a sibling so the three read-only
        consumers keep the untouched two-value read. `_do_replan` WRITES the path
        `_paused_spec` returned, and `runs.task_spec_root` is the single definition
        backing both halves: an anchor and a `confine_root` that name different trees do
        not refuse, they silently degrade the write (#593).

        The no-task arm is `Path(state.project)`, NOT `self.project`, so both arms make
        one claim: the delegate answers from the state the run persisted at launch,
        while `self.project` is the constructor's `resolve_or_lexical` of the operator's
        argument, and the two can differ. That arm is currently unreachable from the
        write path — `_review_plan_checkpoint`'s `done()` refuses a `None` `spec_path`
        before calling `_do_replan`, and `_paused_spec` returns `None` on BOTH of its
        arms (no task, and a task carrying no `spec_file`) — so this is about not
        leaving a second claim lying around for a future caller, not a live bug. The
        no-task arm is the only one reachable here: a task with an empty `spec_file`
        still answers from `task_spec_root`, which needs no spec to name a tree."""
        task = self._paused_task(state)
        return runs.task_spec_root(task, state) if task else Path(state.project)

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
        # `task_stories_root`, not `self.project`, for the reason `_sentinel_kind`
        # states below: BOTH feed one `EscalationModal` — this supplies its title and
        # description, that its sentinel indicator — so a manifest read from the main
        # checkout beside a sentinel read from the mount is the same one-surface-two-trees
        # defect the anchor exists to close. `self.project` is also the wrong VALUE for
        # the no-task arm: it is the constructor's `resolve_or_lexical` of the operator's
        # argument, while every other anchored read here answers from `state.project`,
        # the path the run persisted at launch.
        root = runs.task_stories_root(state.tasks.get(key), state)
        try:
            folder = stories.resolve_spec_folder(root, state.spec_folder)
            entry = stories.load_stories(folder).get(key)
        except stories.StoriesError:
            return "", ""
        return (entry.title, entry.description) if entry else ("", "")

    def _sentinel_kind(self, state: RunState, key: str) -> str:
        if state.source != "stories" or not state.spec_folder:
            return ""
        # Anchored on the tree the RUN owns, for the same reason `_paused_spec` is: the
        # sentinel the engine wrote lives in the unit's mount under isolation
        # (`stories_engine._stories_folder` IS the worktree during a driven story),
        # while the main checkout carries the same layout and holds a stale twin or
        # nothing. Both values feed ONE `EscalationModal` — the spec text through
        # `_blocking_condition`, this through `sentinel_kind` — so anchoring them on
        # different trees let a single modal disagree with itself and rendered a
        # pre-planning sentinel wedge as an ordinary escalation.
        #
        # `task_stories_root`, not `task_spec_root`: the folder is located from the
        # workspace root, and the latter's out-of-mount arm answers a confinement
        # question about `spec_file` that would send this read to the main checkout
        # while `_stories_folder` stayed on the mount. It also takes `None`, so the
        # no-task fallback is not re-spelled here.
        root = runs.task_stories_root(state.tasks.get(key), state)
        # resolve_story_spec globs + reads frontmatter; a file removed mid-scan (a
        # re-arm clearing the sentinel while the viewer refreshes) can raise OSError.
        # Degrade to "" rather than let a race-window read crash the render.
        try:
            folder = stories.resolve_spec_folder(root, state.spec_folder)
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
        # Through the chokepoint (#390): a timeout or failed spawn arrives as
        # GitError/GitSpawnError instead of the raw subprocess pair, and taking
        # bytes closes the strict-decode hole — text=True raised
        # UnicodeDecodeError (a ValueError, caught by neither arm of the old
        # guard) on a subject undecodable in the run's codec, crashing the
        # checkpoint modal. Subject bytes are git's logOutputEncoding (UTF-8
        # unless configured); replace so an odd byte degrades one label,
        # never raises mid-render.
        # timeout_s=5 keeps the pre-#390 deadline: this runs on the event loop
        # (the checkpoint modal's build path), so a stalled git must surface as
        # a missing subject in seconds, not a 120s frozen UI.
        try:
            proc = verify.git_bytes(self.project, "log", "-1", "--format=%s", sha, timeout_s=5)
        except verify.GitError:
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", errors="replace").strip()

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
            launch.kill_ctl_window(self.project, run_id)
        except (OSError, StopRunError, ProcessHostError) as e:
            self.call_from_thread(self.notify, f"stop failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, f"run {run_id} stopped")

    def action_graceful_stop_run(self) -> None:
        """Ask the selected live run to stop *gracefully*: finish the in-flight item
        (story dev/review/commit, or a sweep bundle through commit), then finalize
        cleanly and stop — resumable, unlike the hard stop `x` delivers, which
        abandons the in-flight item.

        Deliberately no `_mux_missing` gate: unlike `x` (which kills the agent
        window) this touches no multiplexer — the request rides the same control
        file a hard stop uses, in its graceful mode, read by the engine at item
        boundaries — so it must work even with the backend down. The liveness gate is also deliberately looser than `x`'s: it rejects
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
                "finalizes and stops (resumable). `x` instead abandons the "
                "in-flight item.",
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
        except OSError as e:
            # Mirrors the CLI's `stop --graceful` arm: the lodge does not roll
            # back a failed write (see _create_stop_request), so a part-way
            # failure can leave a graceful request standing — and a confined
            # refusal (`UnconfinedWriteError`, #593) wrote nothing at all. Either
            # way the worker must not raise: Textual workers default to
            # exit_on_error=True, so an uncaught OSError here would take the
            # whole dashboard down instead of reporting the refusal.
            self.call_from_thread(
                self.notify,
                f"run {run_id}: stop request could not be written ({e}) — a graceful "
                f"request may still be pending; check `bmad-loop status {run_id}` and "
                f"use `bmad-loop stop {run_id} --cancel-graceful` to withdraw it",
                severity="error",
            )
            return
        if outcome == "already-pending":
            # Mode-neutral: the pending request may be a hard one, and this token
            # cannot tell (#319) — same wording as the CLI's `stop --graceful`.
            self.call_from_thread(self.notify, f"run {run_id} already has a stop request pending")
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
            runs.delete_run(self.project, run_dir)
        except (OSError, runs.LiveSessionError) as e:
            # LiveSessionError is the #419 backstop: the confirm above gates on engine
            # liveness, which an orphaned session passes. Surface it like any other
            # failed removal rather than letting it kill the worker thread.
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
        except (OSError, runs.LiveSessionError) as e:
            # see _delete_run_worker: the confirm's guard is engine-keyed, this one
            # is session-keyed (#419).
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
        # so the warning below only ever names sessions that were actually pruned.
        #
        # Guarded for the same reason as the ctl-window arm below, with the
        # opposite conclusion. This half is raiser-side too — the psmux backend
        # refuses a registry root that would fail its pre-spawn absoluteness gate,
        # and that raise is thrown before the tolerant listing wrapper can degrade
        # it — and an escape from a worker thread takes the whole dashboard down
        # (Textual's exit_on_error). Every CLI surface turns that same raise into
        # one named error through main()'s backstop; a worker thread has none.
        # But nothing has been killed yet, so there is no completed work to
        # protect: toast and stop, rather than carry on reporting a sweep that
        # never ran.
        try:
            killed, _live, unknown = runs.prune_sessions(self.project)
        except (MultiplexerError, UnicodeError) as e:
            self.call_from_thread(self.notify, f"session prune failed: {e}", severity="error")
            return
        # prune_ctl_windows probes has_session on the shared ctl session, a
        # raiser-side call; on a worker thread the toast must be marshalled, and
        # notify() must not be called directly (see _mux_guarded — foreground only).
        try:
            windows, survived, unverifiable = launch.prune_ctl_windows(self.project)
        except (MultiplexerError, UnicodeError) as e:
            # UnicodeError: a strict-POSIX decode fault from a scan probe that
            # does not normalize it to the seam type (#380) — the cli cleanup
            # arm's twin; an escape here kills the worker thread instead.
            # prune_sessions already killed the agent sessions above; surface the
            # ctl-window failure but keep reporting that completed work (and the
            # unknown-pid warning) rather than swallowing it on an early return.
            # Named: a bare transport message next to a "removed N session(s), 0
            # window(s)" toast reads as a successful window sweep.
            self.call_from_thread(self.notify, f"ctl window prune failed: {e}", severity="error")
            windows, survived, unverifiable = [], [], []
        if unknown:
            self.call_from_thread(
                self.notify,
                f"{len(unknown)} pruned session(s) had an unverifiable engine pid "
                f"(may still be live): {', '.join(sorted(unknown))}",
                severity="warning",
            )
        # The cli cleanup arm's stderr line, as a toast: the removal count below
        # excludes sessions the migration pass declined to claim in a legacy
        # registry, and a count that quietly excludes them reads as "all clean".
        # Read after the prune, so it describes what is left standing. Silent on
        # every platform without a registry namespace.
        # One toast per registry, naming it: there is more than one legacy
        # registry (psmux's default, and any root this process displaced), and
        # the operator's next action is to open the one holding these.
        for registry, names in runs.legacy_registry_leftovers(self.project).items():
            self.call_from_thread(
                self.notify,
                f"{len(names)} session(s) left in {registry} (not migrated): "
                f"{', '.join(names)} — see docs/multiplexer-backends.md before "
                "removing any of them",
                severity="warning",
            )
        # A kill that did not verifiably land gets its own toast rather than a
        # silent subtraction from the count below (#435) — the count now reports
        # only verified removals, so without this the windows would just vanish
        # from the report. Kept apart because they are different claims: one is
        # positive evidence the window is still there, the other is the absence
        # of any evidence at all. Both are retried by the next cleanup.
        if survived:
            self.call_from_thread(
                self.notify,
                f"{len(survived)} ctl window(s) still open after the kill: {', '.join(survived)}",
                severity="warning",
            )
        if unverifiable:
            self.call_from_thread(
                self.notify,
                f"{len(unverifiable)} ctl window(s) kill attempted, outcome unverifiable: "
                f"{', '.join(unverifiable)}",
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
        except Exception:  # a JSON-leg failure degrades, never kills the app
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
        except Exception as exc:  # a failed spawn is a modal, not a crash
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
