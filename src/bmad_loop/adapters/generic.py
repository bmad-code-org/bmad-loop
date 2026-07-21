"""Generic coding-CLI driver: interactive sessions in tmux windows, observed via hooks.

Each pipeline step gets a fresh tmux window running the full interactive CLI
with the skill invocation as the initial prompt. Completion is detected
exclusively through hook-written event files (Stop/SessionEnd) plus the
presence of the skill-written result.json — the pane log's *contents* are
never parsed for control flow (only tee'd for human debugging), though its
*growth* (mtime/size, never the bytes — see ``_log_activity_key``) is read as
a liveness signal to re-arm the dev-stall grace window.

Everything CLI-specific (binary, prompt rendering, bypass flags, usage
parser) comes from a declarative CLIProfile; each CLI's hook config registers
the shared relay script under its native event names but passes the canonical
event name as argv, so this adapter only ever sees canonical events. CLIs
without a SessionEnd hook (e.g. Codex) are covered by the window-death
fallback.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import devcontract, gates, runs
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR
from ..model import TokenUsage
from ..policy import Policy
from ..process_host import ProcessHostError, get_process_host
from ..signals import SignalWatcher
from ..tokens import read_usage as tally_usage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec
from .multiplexer import MultiplexerError, TerminalMultiplexer, get_multiplexer
from .profile import CLIProfile

if TYPE_CHECKING:
    from ..process_host import ProcessHost

# Pane geometry for agent windows; mirrored in tui.data for log emulation.
PANE_COLUMNS = 220
PANE_LINES = 50
RESULT_GRACE_S = 15.0
RESULT_POLL_S = 0.5
KILL_POLL_S = 0.5
# min spacing between heartbeat.json overwrites in wait_for_completion; the
# heartbeat's staleness is what makes a frozen orchestrator (#157) diagnosable.
HEARTBEAT_INTERVAL_S = 30.0
EVENT_KINDS = {"SessionStart", "Stop", "SessionEnd"}
NUDGE_TEXT = (
    "You are running in bmad-loop automation mode. Finish the workflow now: "
    "complete any remaining steps and write the result JSON file to "
    "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID/result.json, then end your turn."
)
# Wake an idle dev session whose grace window elapsed with no output. bmad-loop
# has no background-completion re-invocation, so a turn ended to await a slow
# background process (a Unity PlayMode run, a long test) would otherwise wait
# forever; this nudge IS that re-invocation. Skill-agnostic: it must not assume a
# result.json (the bmad-dev-auto skill writes none — see GenericDevAdapter).
STALL_NUDGE_TEXT = (
    "You appear idle in bmad-loop automation mode, which cannot re-invoke you when "
    "a background process finishes. If you are waiting on one (e.g. a Unity PlayMode "
    "run or a long test), check its status now and continue the workflow; if it is "
    "done, finalize the work and end your turn. If you are stuck, say so and stop. "
    "Note: a prose reply cannot end this session — only your workflow's completion "
    "artifact (the spec's terminal status / result file) does; if the work is "
    "already complete, write it before ending your turn."
)
# Wrap-up demand for a session that crossed its token budget (#158, enforce
# mode): the guard arms a bounded grace window right after sending this, so the
# session must converge now — it will be terminated over_budget otherwise.
BUDGET_NUDGE_TEXT = (
    "You have exceeded this session's token budget in bmad-loop automation mode. "
    "Stop exploring and wrap up now: commit whatever is finished, write your "
    "workflow's completion artifact (the spec's terminal status / result file), "
    "and end your turn. Note: a prose reply cannot end this session — only the "
    "completion artifact does; if you cannot finish, mark the work blocked in it "
    "and end your turn."
)


class _ResultFileMixin:
    """Result-file read-back and verdict finalization: acquire the
    skill-written result dict and fold it into the session's final
    ``SessionResult``. Transport-agnostic — shared by the tmux adapters and
    any adapter whose skill writes ``tasks/<task_id>/result.json``; needs
    only ``self.tasks_dir``."""

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        """Acquire this session's result dict. Base behavior: read the
        skill-written ``result.json`` (briefly awaiting it on the Stop event,
        reading once otherwise). Subclasses whose skill writes no result.json
        (GenericDevAdapter) override this to synthesize the dict from another
        on-disk artifact."""
        return self._await_result(handle.task_id) if wait else self._read_result(handle.task_id)

    def _final(
        self,
        handle: SessionHandle,
        spec: SessionSpec,
        fallback: str,
        session_id: str | None,
        transcript: str | None,
        *,
        accept_result: bool = True,
        budget_weighted: int | None = None,
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status. ``accept_result=False``
        (a stall verdict reached under a live window) pins the fallback: an
        artifact that appeared without a Stop or window death is not trusted.
        ``budget_weighted`` (a tripped session-budget guard's sample) rides
        every exit so the engine can journal it whatever the verdict."""
        result_json = self._result_json(handle, spec, wait=False) if accept_result else None
        status = "completed" if result_json is not None else fallback
        return SessionResult(
            status=status,
            result_json=result_json,
            session_id=session_id,
            transcript_path=transcript,
            budget_weighted=budget_weighted,
        )

    def _result_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "result.json"

    def _append_diag_jsonl(self, task_id: str, filename: str, payload: dict) -> None:
        """Append ``payload`` as one JSON line to ``tasks/<task_id>/<filename>``.
        Pure observability, best-effort: an unwritable run dir must never break
        the completion loop."""
        try:
            path = self.tasks_dir / task_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _note_resultless_stop(self, task_id: str, verdict: str, detail: str = "") -> None:
        """Append a diagnostic breadcrumb when a Stop's artifact read-back gives
        up empty: one JSON line ({ts, verdict, detail}) in
        ``tasks/<task_id>/resultless-stops.jsonl`` — the #149 nudge livelock
        was undiagnosable because nothing recorded *why* each Stop read as
        result-less."""
        self._append_diag_jsonl(
            task_id,
            "resultless-stops.jsonl",
            {"ts": time.time_ns(), "verdict": verdict, "detail": detail},
        )

    def _note_lifecycle(self, task_id: str, event: str, **fields) -> None:
        """Append a session-lifecycle breadcrumb ({ts, event, ...}) to
        ``tasks/<task_id>/session-lifecycle.jsonl`` — issue #157's timeout fired
        with zero record of *when* the adapter declared it or which clock had
        elapsed, so a 2h19 journaling gap was unattributable."""
        self._append_diag_jsonl(
            task_id,
            "session-lifecycle.jsonl",
            {"ts": time.time_ns(), "event": event, **fields},
        )

    def _write_heartbeat(self, task_id: str, payload: dict) -> None:
        """Best-effort overwrite of ``tasks/<task_id>/heartbeat.json``: the wait
        loop's proof-of-life. A heartbeat much staler than HEARTBEAT_INTERVAL_S
        under a still-running session means the orchestrator itself was frozen
        (host starvation, macOS sleep — #157), not the CLI."""
        try:
            (self.tasks_dir / task_id / "heartbeat.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _read_result(self, task_id: str) -> dict | None:
        path = self._result_path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _await_result(self, task_id: str, grace_s: float = RESULT_GRACE_S) -> dict | None:
        deadline = time.monotonic() + grace_s
        while True:
            result = self._read_result(task_id)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                self._note_resultless_stop(
                    task_id, "no-result-json", f"no readable {self._result_path(task_id)}"
                )
                return None
            time.sleep(RESULT_POLL_S)


class GenericAdapter(_ResultFileMixin, CodingCLIAdapter):
    injection = "tmux-initial-prompt"
    observation = "hook-signal"
    state = "local-jsonl"

    def __init__(
        self,
        run_dir: Path,
        policy: Policy,
        profile: CLIProfile,
        binary: str | None = None,
        extra_args: tuple[str, ...] | None = None,
        usage_grace_s: float | None = None,
        stop_without_result_nudges: int | None = None,
        mux: TerminalMultiplexer | None = None,
    ):
        self.run_dir = run_dir
        self.policy = policy
        self.profile = profile
        self.mux = mux or get_multiplexer()
        # None = use the profile's default bypass flags; a tuple replaces them
        self.extra_args = extra_args
        # Effective timing knobs: an explicit [adapter]/[adapter.<stage>] override
        # wins, else the CLI profile's shipped default, else the global fallback.
        self._usage_grace_s = usage_grace_s if usage_grace_s is not None else profile.usage_grace_s
        self._stop_nudges = (
            stop_without_result_nudges
            if stop_without_result_nudges is not None
            else (
                profile.stop_without_result_nudges
                if profile.stop_without_result_nudges is not None
                else policy.limits.stop_without_result_nudges
            )
        )
        # Grace for a result-less Stop before declaring a stall. 0 (base default)
        # keeps the fail-fast behavior; the dev adapter raises it so a session
        # that ended its turn awaiting a background process isn't mis-stalled.
        self._stall_grace_s = 0.0
        # Wake-nudges to spend on grace expiry before stalling. 0 here is moot for
        # the base adapter (grace 0 never opens the window); the dev adapter sets
        # it from policy so an idle wait is re-invoked rather than killed outright.
        self._stall_nudges = 0
        self.name = f"{profile.name}-tmux"
        self.binary = binary or profile.binary
        self.session_name = f"bmad-loop-{run_dir.name}"
        self.watcher = SignalWatcher(run_dir / "events")
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / LOGS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------- multiplexer

    def _ensure_session(self, cwd: Path) -> None:
        if not self.mux.has_session(self.session_name):
            self.mux.new_session(self.session_name, cwd, PANE_COLUMNS, PANE_LINES)
            # Tag the session with its project so a cleanup in another project
            # never prunes this run (run_dir = <project>/.bmad-loop/runs/<id>).
            project = self.run_dir.parents[2]
            self.mux.set_session_option(
                self.session_name, runs.PROJECT_OPTION, runs.project_tag(project)
            )

    def interactive_argv(self, spec: SessionSpec) -> list[str]:
        extra = self.extra_args
        if extra is None:
            extra = self.profile.bypass_args
        argv = [
            self.binary,
            *self.profile.launch_args,
            self.profile.render_prompt(spec.prompt),
            *extra,
        ]
        if spec.model:
            argv += [self.profile.model_flag, spec.model]
        return argv

    def interactive_env(self, spec: SessionSpec) -> dict[str, str]:
        return {**self.profile.env, **spec.env}

    def build_command(self, spec: SessionSpec) -> str:
        return " ".join(shlex.quote(a) for a in self.interactive_argv(spec))

    # --------------------------------------------------------------- adapter

    def _prepare_session(self, spec: SessionSpec) -> None:
        task_dir = self.tasks_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "prompt.txt").write_text(spec.prompt + "\n", encoding="utf-8")
        # A re-armed/resumed run reuses task_ids; drop any prior cycle's result
        # so a session that writes nothing can't be read as a stale completion.
        (task_dir / "result.json").unlink(missing_ok=True)

    def _launch_session(self, spec: SessionSpec, command: str) -> SessionHandle:
        self._ensure_session(spec.cwd)
        # Stamped before launch: hook events carry wall-clock ns, and
        # wait_for_completion ignores anything older than this floor so a reused
        # task_id's earlier Stop event cannot replay.
        launched_ns = time.time_ns()
        window_id = self.mux.new_window(
            self.session_name,
            spec.task_id[-40:],
            spec.cwd,
            self.interactive_env(spec),
            command,
        )
        log_file = self.logs_dir / f"{spec.task_id}.log"
        # pipe_pane tolerates the window having already died (a CLI that crashes on
        # launch can take it down before the tee attaches); the dead window is then
        # reported as a crash in wait_for_completion.
        self.mux.pipe_pane(window_id, log_file)
        return SessionHandle(task_id=spec.task_id, native_id=window_id, launched_ns=launched_ns)

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        self._prepare_session(spec)
        return self._launch_session(spec, self.build_command(spec))

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        deadline = time.monotonic() + spec.timeout_s
        # Wall-clock co-bound (#157): a host suspend freezes time.monotonic(),
        # silently extending the monotonic deadline by the nap's length. The
        # wall clock keeps counting through a suspend, so it may EXPIRE the
        # deadline — never extend it; all sub-waits below stay monotonic (a
        # wall clock stepped backward must not stretch the session).
        wall_deadline = time.time() + spec.timeout_s
        session_id: str | None = None
        transcript_path: str | None = None
        nudges_left = self._stop_nudges
        # set when a result-less Stop opens an idle-grace window (dev adapter
        # only); a fresh Stop re-arms it, an elapsed window with no terminal
        # result is a genuine stall. None = no grace pending.
        stall_deadline: float | None = None
        # pane-log activity signature captured when the grace window is armed; a
        # session streaming output (a long productive turn, a streaming subagent)
        # advances it and re-arms the window, so only genuine silence stalls.
        last_activity: tuple[int, int] | None = None
        # wake-nudges left to spend when the grace window elapses in silence: the
        # session likely ended its turn awaiting a background process, so we prod
        # it (bmad-loop has no background re-invocation) instead of stalling. A
        # fresh Stop — proof it woke and acted — restores the budget; only an
        # unresponsive session burns through it. Bounded overall by spec.timeout_s.
        stall_nudges_left = self._stall_nudges
        # monotonic total of stall nudges sent this session — never restored,
        # unlike stall_nudges_left. When spec.stall_nudges_cap is set (the
        # engine sets it for every session it drives), a session that keeps
        # ending its turn without a result cannot ride the fresh-Stop refill
        # forever: after cap total nudges it is declared stalled. cap=None
        # (raw constructor default) skips the check.
        stall_nudges_sent = 0
        # internal observability counter: counts ticks where the liveness probe
        # raised a transport error (e.g. a 30s tmux hang). It deliberately does
        # NOT escalate to "crashed" — a transient transport hiccup is not proof
        # of death; spec.timeout_s already bounds a persistent failure to a
        # timeout.
        probe_failures = 0
        # monotonic ts of the last heartbeat.json overwrite; None = not yet
        # written, so the first tick always stamps one.
        last_heartbeat: float | None = None
        # Session-budget guard (#158): latched on the first cap crossing — the
        # warn/nudge fires at most once per session. budget_deadline is the
        # enforce-mode monotonic grace expiry (None = not armed); checked every
        # tick, unlike the heartbeat-throttled sampling that arms it. The wall
        # deadline is the #157 co-bound: a host suspend freezes
        # time.monotonic(), silently stretching the "bounded" wrap-up window,
        # so the wall clock may EXPIRE the grace — never extend it.
        budget_tripped = False
        budget_weighted: int | None = None
        budget_deadline: float | None = None
        budget_wall_deadline: float | None = None

        while True:
            remaining = deadline - time.monotonic()
            wall_expired = time.time() >= wall_deadline
            if remaining <= 0 or wall_expired:
                if remaining <= 0 and wall_expired:
                    expired = "both"
                elif remaining <= 0:
                    expired = "monotonic"
                else:
                    # wall-only expiry with monotonic time to spare: the
                    # monotonic clock stood still — the suspend signature.
                    expired = "wall"
                self._note_lifecycle(
                    handle.task_id,
                    "timeout-fired",
                    expired_clock=expired,
                    timeout_s=spec.timeout_s,
                    mono_remaining_s=round(remaining, 3),
                )
                return SessionResult(
                    status="timeout",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    timeout_fired_at=time.time(),
                    timeout_expired_clock=expired,
                    budget_weighted=budget_weighted,
                )
            now = time.monotonic()
            if last_heartbeat is None or now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                last_heartbeat = now
                self._write_heartbeat(
                    handle.task_id,
                    {
                        "ts": time.time(),
                        "remaining_s": round(remaining, 3),
                        "stall_armed": stall_deadline is not None,
                        "stall_nudges_sent": stall_nudges_sent,
                    },
                )
                # Budget sampling rides the heartbeat cadence — no extra knob.
                # transcript_path is unknown until the first hook event carries
                # it (SessionStart for claude); until then the guard is inert.
                if (
                    not budget_tripped
                    and spec.token_budget is not None
                    and spec.token_budget_mode in ("warn", "enforce")
                    and transcript_path
                ):
                    weighted = self._sample_weighted_usage(transcript_path, spec)
                    if weighted is not None and weighted > spec.token_budget:
                        budget_tripped = True
                        budget_weighted = weighted
                        self._note_lifecycle(
                            handle.task_id,
                            "budget-tripped",
                            weighted=weighted,
                            budget=spec.token_budget,
                            mode=spec.token_budget_mode,
                        )
                        try:
                            gates.notify(
                                self.policy,
                                self.run_dir,
                                "bmad-loop session over token budget",
                                f"{handle.task_id}: weighted spend {weighted} crossed the "
                                f"{spec.token_budget} per-session cap "
                                f"(mode={spec.token_budget_mode})",
                            )
                        except OSError:
                            # observe-degrade: an unwritable ATTENTION file is
                            # observability, never a reason to break the loop
                            # (the _write_heartbeat doctrine).
                            pass
                        # nosec below: bandit B105 pattern-matches the "token"
                        # in token_budget_mode as a hardcoded-password compare;
                        # it is a mode enum, not a credential.
                        if spec.token_budget_mode == "enforce":  # nosec B105
                            if spec.token_budget_grace_s <= 0:
                                # zero grace = terminate at trip, no nudge — but
                                # window death still wins (artifact honored via
                                # the crash path), exactly like grace expiry; a
                                # transport error is not proof of death.
                                try:
                                    if not self._window_alive(handle):
                                        return self._final(
                                            handle,
                                            spec,
                                            "crashed",
                                            session_id,
                                            transcript_path,
                                            budget_weighted=weighted,
                                        )
                                except MultiplexerError:
                                    pass
                                self._note_lifecycle(
                                    handle.task_id,
                                    "over-budget-fired",
                                    weighted=weighted,
                                    budget=spec.token_budget,
                                    grace_s=spec.token_budget_grace_s,
                                    zero_grace=True,
                                )
                                return SessionResult(
                                    status="over_budget",
                                    session_id=session_id,
                                    transcript_path=transcript_path,
                                    budget_weighted=weighted,
                                )
                            try:
                                self.send_text(handle, BUDGET_NUDGE_TEXT)
                            except MultiplexerError:
                                # a dead/hung window can't take the nudge; the
                                # grace still arms — the next tick's liveness
                                # probe scores a dead window crashed.
                                pass
                            budget_deadline = time.monotonic() + spec.token_budget_grace_s
                            budget_wall_deadline = time.time() + spec.token_budget_grace_s
            if budget_deadline is not None and (
                time.monotonic() >= budget_deadline
                or (budget_wall_deadline is not None and time.time() >= budget_wall_deadline)
            ):
                # Grace expired with no completion (wall co-bound included: a
                # suspend-frozen monotonic clock must not stretch the window,
                # #157). Window death is authoritative (its artifact is honored
                # via the crash path); under a live window the session ends
                # over_budget WITHOUT reading the result file — an artifact
                # under a live window is never trusted (#48/#53). A transport
                # error is not proof of death, so it falls through to the
                # over_budget verdict.
                try:
                    if not self._window_alive(handle):
                        return self._final(
                            handle,
                            spec,
                            "crashed",
                            session_id,
                            transcript_path,
                            budget_weighted=budget_weighted,
                        )
                except MultiplexerError:
                    pass
                self._note_lifecycle(
                    handle.task_id,
                    "over-budget-fired",
                    weighted=budget_weighted,
                    budget=spec.token_budget,
                    grace_s=spec.token_budget_grace_s,
                    zero_grace=False,
                )
                return SessionResult(
                    status="over_budget",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                )
            event = self.watcher.wait_for(
                handle.task_id,
                EVENT_KINDS,
                timeout_s=min(remaining, 5.0),
                since_ns=handle.launched_ns,
            )
            if event is None:
                try:
                    alive = self._window_alive(handle)
                except MultiplexerError:
                    # transport hiccup (e.g. a 30s tmux hang), not proof of
                    # death: never roll back a possibly-working session. Skip the
                    # crash check this tick; hook events still complete it, and
                    # spec.timeout_s bounds a persistent transport failure to an
                    # honest "timeout".
                    probe_failures += 1
                    continue
                probe_failures = 0
                if not alive:
                    # died without a SessionEnd hook (killed, crashed hard)
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript_path,
                        budget_weighted=budget_weighted,
                    )
                if stall_deadline is not None:
                    # No artifact shortcut here: the window is alive on this tick
                    # (a dead one returned "crashed" above), and a terminal
                    # artifact under a live window is advisory only — the agent
                    # may still be mid-turn (or the artifact stale from a prior
                    # drive), and run()'s finally-kill would terminate it before
                    # its remaining work flushes. Only a Stop event or window
                    # death completes the session.
                    # The grace window measures inactivity, not time-since-Stop:
                    # a session still streaming to the tee'd pane log (a long
                    # productive turn building a diff, a streaming subagent) is
                    # working, not stalled. Re-arm on any pane growth so only
                    # genuine silence for the full grace trips the stall below.
                    key = self._log_activity_key(handle.task_id)
                    if key is not None and key != last_activity:
                        last_activity = key
                        stall_deadline = time.monotonic() + self._stall_grace_s
                        continue
                if stall_deadline is not None and time.monotonic() >= stall_deadline:
                    if stall_nudges_left > 0 and (
                        spec.stall_nudges_cap is None or stall_nudges_sent < spec.stall_nudges_cap
                    ):
                        # The wake nudge IS the re-invocation bmad-loop otherwise
                        # lacks: prod the idle session and re-arm. Budget is
                        # restored only by a fresh Stop (a real turn-end), so the
                        # nudge's own echoed keystrokes can't be mistaken for the
                        # agent waking; an unresponsive session keeps draining it.
                        stall_nudges_left -= 1
                        stall_nudges_sent += 1
                        self.send_text(handle, STALL_NUDGE_TEXT)
                        stall_deadline = time.monotonic() + self._stall_grace_s
                        last_activity = self._log_activity_key(handle.task_id)
                        continue
                    # Re-probe liveness before finalizing: this return exits the
                    # loop, so a hard death (no SessionEnd) in the gap since the
                    # top-of-tick probe would otherwise never be caught. Window
                    # death is authoritative — a now-dead window flows through the
                    # crash path (which honors its artifact via accept_result=True)
                    # instead of a stall that discards a just-flushed result. A
                    # transport error is not proof of death (as at the top of the
                    # tick); fall through to the stall — spec.timeout_s bounds a
                    # persistent failure.
                    try:
                        if not self._window_alive(handle):
                            return self._final(
                                handle,
                                spec,
                                "crashed",
                                session_id,
                                transcript_path,
                                budget_weighted=budget_weighted,
                            )
                    except MultiplexerError:
                        pass
                    # Still alive: an artifact on disk cannot upgrade the stall to
                    # completed — it may be stale or mid-write; only a Stop or
                    # window death vouches for it.
                    return self._final(
                        handle,
                        spec,
                        "stalled",
                        session_id,
                        transcript_path,
                        accept_result=False,
                        budget_weighted=budget_weighted,
                    )
                continue
            if (
                event.event == "Stop"
                and self.profile.subagent_stop_without_transcript
                and not event.transcript_path
            ):
                # Copilot fires agentStop for each subagent turn with an empty
                # transcriptPath and a tool-use session id; that is not the main
                # session's turn-end. Ignore it (before accumulating the junk
                # session id) so a subagent's premature Stop is not read as a
                # result-less completion -> false stall, and the main session's
                # real transcript is preserved for usage tallying.
                continue
            session_id = event.session_id or session_id
            transcript_path = event.transcript_path or transcript_path

            if event.event == "SessionStart":
                continue
            if event.event == "Stop":
                result_json = self._result_json(handle, spec, wait=True)
                if result_json is not None:
                    return SessionResult(
                        status="completed",
                        result_json=result_json,
                        session_id=session_id,
                        transcript_path=transcript_path,
                        budget_weighted=budget_weighted,
                    )
                if nudges_left > 0:
                    nudges_left -= 1
                    self.send_text(handle, NUDGE_TEXT)
                    continue
                if self._stall_grace_s <= 0:
                    return self._final(
                        handle,
                        spec,
                        "stalled",
                        session_id,
                        transcript_path,
                        budget_weighted=budget_weighted,
                    )
                # A result-less Stop, but the session may have ended its turn to
                # await a background process (a Unity PlayMode run, a slow test)
                # and expects to be re-invoked on completion. Open/re-arm an idle-
                # grace window — a later Stop lands here again and resets it, so
                # only a genuinely idle gap (handled in the no-event branch above)
                # is a stall. Bounded overall by spec.timeout_s.
                stall_deadline = time.monotonic() + self._stall_grace_s
                last_activity = self._log_activity_key(handle.task_id)
                # a real turn-end proves the session is responsive: restore the
                # wake-nudge budget so a slow-but-cooperative session can keep
                # waiting (up to spec.timeout_s), unlike a truly unresponsive one.
                stall_nudges_left = self._stall_nudges
                continue
            if event.event == "SessionEnd":
                return self._final(
                    handle,
                    spec,
                    "crashed",
                    session_id,
                    transcript_path,
                    budget_weighted=budget_weighted,
                )

    def _log_activity_key(self, task_id: str) -> tuple[int, int] | None:
        """Activity signature of the tee'd pane log: (mtime_ns, size), or None if
        it does not yet exist. The pane is piped via append to a stable inode, so
        a growing size (and advancing mtime) is a reliable signal the session is
        still producing output even when no hook event fires."""
        try:
            st = (self.logs_dir / f"{task_id}.log").stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _window_alive(self, handle: SessionHandle) -> bool:
        return handle.native_id in self.mux.list_window_ids(self.session_name)

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self.mux.send_text(handle.native_id, text)

    def kill(self, handle: SessionHandle) -> None:
        grace = float(self.policy.limits.teardown_grace_s)
        if grace <= 0:
            # Legacy single strike: no harvest, no wait, no pid reads. grace 0 is the
            # documented opt-out — teardown stays exactly today's best-effort kill.
            self.mux.kill_window(handle.native_id)
            return
        try:
            host = get_process_host()
        except ProcessHostError:
            # An explicit-but-bogus BMAD_LOOP_PROCESS_HOST override raises loudly
            # (deliberate doctrine — never silently mis-signal). But the lookup now
            # precedes the first strike, so the window must not be left alive behind
            # the raise: strike once (today's teardown), then re-raise.
            self.mux.kill_window(handle.native_id)
            raise
        # Harvest the pane roots AND their whole descendant tree NOW, while the
        # window is provably alive, stamping a pid-reuse identity per member. This
        # pre-kill snapshot is load-bearing (#183): once the window dies a detached
        # straggler (setsid, a double-fork survivor) reparents to init and is no
        # longer reachable from the pane pids, and a late-read pid risks reuse — so
        # every destructive strike below is identity-guarded via alive_and_ours,
        # never a bare pid. The snapshot is a point-in-time read: a process that
        # detached (double-fork/setsid) BEFORE the harvest — or in the TOCTOU window
        # AFTER it, before/around kill_window — has no pane-pid ancestor to be found
        # by, so it is out of reach by construction (accepted, documented #183 limit).
        # An empty list = the backend offers no pids (herdr) → degrade to the window
        # kill alone.
        # Descendant identities ride along from the enumeration itself — the same
        # /proc read (Linux) or the same psutil Process object, revalidated against
        # its construction-bound ident (macOS/win32), so no post-hoc stamp can race
        # a reuse;
        # only the pane ROOT is stamped separately, which is safe: the live window
        # pins the root pid until kill_window below, so it cannot be recycled here.
        tree: dict[int, float | None] = {}
        for pid in self.mux.window_pane_pids(handle.native_id):
            tree.setdefault(pid, host.identity(pid))
            for child, identity in host.descendants(pid).items():
                tree.setdefault(child, identity)
        # First strike stays the plain best-effort window kill; everything below
        # verifies it landed and chases the harvested tree.
        self.mux.kill_window(handle.native_id)
        deadline = time.monotonic() + grace
        while True:
            try:
                dead = not self._window_alive(handle)
            except MultiplexerError:
                dead = False  # transport hiccup — liveness unknown this tick, keep polling
            if dead:
                # Window died within grace (the normal case): reap any harvested
                # straggler that outlived the pane pgid, sharing this deadline.
                self._reap_straggler_tree(handle, host, tree, deadline)
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(KILL_POLL_S)
        # The window outlived the grace: the kill was ignored (a wedged CLI, a shell
        # that trapped the hangup). Re-read the pane pids (freshest view; the live
        # Popen-free window pins them, so they are safe to force-kill directly) and
        # force-kill them, plus every harvested descendant still alive-and-ours. The
        # descendant force-kill is identity-guarded AND skips members with no recorded
        # identity: alive_and_ours(pid, None) degrades to bare is_alive, so a reused
        # pid would pass — the ProcessHost contract forbids force-killing it, exactly
        # like the clean-path reap below. Then re-strike the window.
        repane = self.mux.window_pane_pids(handle.native_id)
        self._note_lifecycle(handle.task_id, "kill-escalated", pids=repane)
        for pid in repane:
            try:
                host.force_kill(pid)
            except Exception:  # noqa: BLE001  # nosec B110 - already-gone races are fine
                pass
        for pid, identity in tree.items():
            if pid not in repane and identity is not None and host.alive_and_ours(pid, identity):
                try:
                    host.force_kill(pid)
                except Exception:  # noqa: BLE001  # nosec B110 - already-gone races are fine
                    pass
        self.mux.kill_window(handle.native_id)
        try:
            alive: bool | None = self._window_alive(handle)
        except MultiplexerError:
            alive = None  # unknown is not dead — record it honestly
        self._note_lifecycle(handle.task_id, "kill-outcome", alive=alive, escalated=True)

    def _reap_straggler_tree(
        self,
        handle: SessionHandle,
        host: ProcessHost,
        tree: dict[int, float | None],
        deadline: float,
    ) -> None:
        """Reap harvested straggler pids the window death left behind — a
        setsid/double-fork survivor escapes the pane pgid, so the window's death is
        not the tree's. Filter to still-alive-and-ours identity-CONFIRMED members,
        then terminate → poll → force-kill within the SAME grace ``deadline`` (one
        budget, two phases): terminate first so a mid-write process can flush
        before SIGKILL. A member whose recorded identity is None is unconfirmable
        (a possible reuse), so it is never signalled AT ALL — not terminated, not
        polled against the deadline, not force-killed; even a SIGTERM to a recycled
        pid kills an innocent process. It only surfaces in the ``unreaped`` field
        via the bare-liveness degrade. Nothing alive at all → silent return: the
        clean-end path leaves no breadcrumb."""

        def _confirmed_survivors() -> list[int]:
            return [
                pid
                for pid, identity in tree.items()
                if identity is not None and host.alive_and_ours(pid, identity)
            ]

        survivors = _confirmed_survivors()
        unconfirmed = [
            pid for pid, identity in tree.items() if identity is None and host.is_alive(pid)
        ]
        if not survivors and not unconfirmed:
            return
        forced: list[int] = []
        if survivors:
            self._note_lifecycle(handle.task_id, "straggler-reap", pids=survivors)
            for pid in survivors:
                try:
                    host.terminate(pid)
                except OSError:
                    pass  # already-gone race — the poll below settles it
            while True:
                survivors = _confirmed_survivors()
                if not survivors or time.monotonic() >= deadline:
                    break
                time.sleep(KILL_POLL_S)
            for pid in survivors:
                try:
                    host.force_kill(pid)
                    forced.append(pid)
                except Exception:  # noqa: BLE001  # nosec B110 - already-gone races are fine
                    pass
        unreaped = [pid for pid, identity in tree.items() if host.alive_and_ours(pid, identity)]
        # Distinct field name (`unreaped`, a pid list) from the wedged branch's
        # `alive` (bool|None): reusing `alive` for both would give one key an
        # unstable type across kill-outcome lines — a footgun for jsonl tailers.
        self._note_lifecycle(
            handle.task_id, "kill-outcome", reaped=True, forced=forced, unreaped=unreaped
        )

    def _sample_weighted_usage(self, transcript_path: str, spec: SessionSpec) -> int | None:
        """Cumulative weighted spend of the live session's transcript, or None
        when the guard must stay inert this tick (parser "none", nothing
        tallied yet, an unreadable file). Sampling must never break the wait
        loop — the liveness-probe tolerance model, for the usage read. The
        transcript is a LIVE file being appended mid-turn: a flush boundary
        can split a multibyte UTF-8 character, so the torn read raises
        UnicodeDecodeError (a ValueError) — as tolerated as an OSError."""
        try:
            usage = tally_usage(self.profile.usage_parser, Path(transcript_path))
        except (OSError, ValueError):
            return None
        if usage is None:
            return None
        return usage.weighted_total(spec.cache_read_weight)

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        if not result.transcript_path:
            return None
        path = Path(result.transcript_path)
        # Some CLIs flush their token totals only on shutdown (Copilot writes
        # modelMetrics in the trailing session.shutdown line, ~1s after the
        # turn-end hook). Poll up to the effective grace so we don't sample the
        # transcript before the totals land. grace 0 = read once (today's path).
        deadline = time.monotonic() + self._usage_grace_s
        while True:
            usage = tally_usage(self.profile.usage_parser, path)
            if usage is not None or time.monotonic() >= deadline:
                return usage
            time.sleep(RESULT_POLL_S)


class _DevSynthesisMixin(_ResultFileMixin):
    """Result synthesis for the generic ``bmad-dev-auto`` skill, shared by
    every transport that drives it (tmux today; see GenericDevAdapter for the
    skill contract). Locates the terminal spec the skill leaves on disk and
    synthesizes the legacy result dict via :mod:`devcontract`. Hosts provide
    ``self.paths`` (a :class:`ProjectPaths`), the ``self.policy`` knobs read
    by ``_configure_dev_knobs``, and the ``_probe_alive`` liveness seam."""

    def _configure_dev_knobs(self) -> None:
        """Override the base result-file knobs for the bmad-dev-auto contract;
        hosts call this at the end of ``__init__``."""
        # The generic skill never writes result.json, so the base "write the
        # result JSON file" nudge is meaningless — and actively misleading — for
        # it. A Stop without a terminal spec is a stall *unless* the session
        # merely ended its turn to await a background process and will be re-
        # invoked on completion; the idle-grace window distinguishes the two.
        self._stop_nudges = 0
        self._stall_grace_s = float(self.policy.limits.dev_stall_grace_s)
        self._stall_nudges = int(self.policy.limits.dev_stall_nudges)

    def _probe_alive(self, handle: SessionHandle) -> bool | None:
        """Liveness of the session's native surface (tmux window, server
        process) for ``_post_kill_reconcile``: True = alive, False = provably
        dead, None = liveness unknown (a transport hiccup — unknown is not
        dead, so the caller keeps its verdict)."""
        raise NotImplementedError

    def _artifact_dirs(self, cwd: Path) -> list[Path]:
        # In worktree isolation the skill runs with cwd set to the worktree and
        # writes its terminal spec under the worktree's rebased implementation-
        # artifacts dir, not the main checkout's. Resolve the search dir from the
        # live session cwd (a no-op in place, where cwd == the project root, and
        # for artifact dirs configured outside the project tree, which rebased()
        # leaves put). Keep the configured dir as a defensive fallback.
        primary = self.paths.rebased(cwd).implementation_artifacts
        dirs = [primary]
        if self.paths.implementation_artifacts != primary:
            dirs.append(self.paths.implementation_artifacts)
        return dirs

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        sr = self._synth_result(handle, spec, wait=wait)
        return sr.result_json if sr is not None else None

    def _synth_result(
        self, handle: SessionHandle, spec: SessionSpec, *, wait: bool
    ) -> devcontract.SynthResult | None:
        # Stories mode (folder+id dispatch): the story spec lives at a
        # deterministic id-keyed path, so resolve it directly instead of the
        # mtime-floor scan. The engine exports BMAD_LOOP_SPEC_FOLDER only for
        # stories runs, so sprint/sweep runs keep the scan path below unchanged.
        if spec.env.get("BMAD_LOOP_SPEC_FOLDER"):
            return self._stories_synth_result(handle, spec, wait=wait)
        # Mirror the base _await_result poll: the skill's terminal spec may not be
        # flushed to disk the instant the Stop event fires, so briefly await it when
        # wait=True instead of reading once and mis-reporting a stall.
        deadline = time.monotonic() + RESULT_GRACE_S
        search_dirs = self._artifact_dirs(spec.cwd)
        while True:
            for artifacts in search_dirs:
                spec_path = devcontract.find_result_artifact(artifacts, since_ns=handle.launched_ns)
                if spec_path is not None:
                    story_key = spec.env.get("BMAD_LOOP_STORY_KEY") or None
                    # Bundle dev sessions: the orchestrator exports the bundle's
                    # owned dw ids (the generic skill never authors them). Stamp
                    # them onto the result so verify_dev_bundle's cross-check passes.
                    raw_dw_ids = (spec.env.get("BMAD_LOOP_DW_IDS") or "").split(",")
                    dw_ids = [tok for tok in (i.strip() for i in raw_dw_ids) if tok]
                    return devcontract.synthesize_result(
                        spec_path, story_key=story_key, dw_ids=dw_ids or None
                    )
            if not wait or time.monotonic() >= deadline:
                if wait:
                    self._note_resultless_stop(
                        handle.task_id,
                        "no-artifact",
                        "no result artifact newer than session launch under: "
                        + ", ".join(str(d) for d in search_dirs),
                    )
                return None
            time.sleep(RESULT_POLL_S)

    def _stories_synth_result(
        self, handle: SessionHandle, spec: SessionSpec, *, wait: bool
    ) -> devcontract.SynthResult | None:
        """Deterministic stories-mode read-back: resolve ``<spec-folder>/stories/
        <id>-*.md`` by id (never the mtime scan) and synthesize from it.

        ``BMAD_LOOP_SPEC_FOLDER`` carries the project-relative (or absolute) spec
        folder; rebase a relative one against ``spec.cwd`` exactly like
        ``_artifact_dirs`` so worktree isolation resolves inside the live checkout.
        A PRESENT or SENTINEL spec synthesizes (a blocked sentinel becomes a
        CRITICAL escalation → PAUSE, same as any block) — but only when the spec was
        (re)written by THIS session: like the mtime-scan path's ``since_ns`` floor, a
        spec whose mtime predates ``handle.launched_ns`` is a stale prior artifact
        (e.g. the dev's ``done`` spec a follow-up review session re-opens) and must
        not be read as this session's result. A still-PENDING spec, an AMBIGUOUS
        match (>1 file — an anomaly no wait can collapse; ``_pick_next`` re-classifies
        it into an actionable wedge), or a stale terminal spec → None (a result-less
        Stop the dev-stall grace handles).

        On a plan-halt leg (``BMAD_LOOP_PLAN_HALT`` set by the engine for a
        spec_checkpoint story's first dispatch) the skill HALTs at
        ``ready-for-dev``; pass ``plan_halt=True`` so synthesize treats that as a
        successful terminal (marked ``plan_halt``) rather than died-mid-flight."""
        from .. import stories

        story_key = spec.env.get("BMAD_LOOP_STORY_KEY") or ""
        folder = Path(spec.env["BMAD_LOOP_SPEC_FOLDER"])
        base = folder if folder.is_absolute() else Path(spec.cwd) / folder
        plan_halt = bool(spec.env.get("BMAD_LOOP_PLAN_HALT"))
        deadline = time.monotonic() + RESULT_GRACE_S
        while True:
            state = stories.resolve_story_spec(base, story_key)
            if state.kind == stories.KIND_AMBIGUOUS:
                # >1 matching file — waiting can't make it collapse to one. Return now
                # (don't burn the grace); the engine's next _pick_next re-classifies
                # AMBIGUOUS and raises the actionable wedge for resolve.
                if wait:
                    self._note_resultless_stop(
                        handle.task_id,
                        "ambiguous",
                        f"{len(state.paths)} specs match id {story_key!r} under {base}",
                    )
                return None
            # Classify this pass for the result-less breadcrumb; overwritten
            # below when the spec is present but not (yet) this session's
            # terminal output.
            verdict, detail = state.kind, str(state.path or base)
            if state.kind in (stories.KIND_PRESENT, stories.KIND_SENTINEL) and state.path:
                if not self._written_this_session(state.path, handle.launched_ns):
                    verdict = "stale-mtime"
                    detail = f"{state.path} predates session launch"
                else:
                    try:
                        sr = devcontract.synthesize_result(
                            state.path, story_key=story_key or None, plan_halt=plan_halt
                        )
                    except UnicodeDecodeError:
                        # A non-UTF-8 read is either a torn glimpse of a spec still
                        # being written (keep polling — a later pass sees the finished
                        # write) or a genuinely corrupt file: then the grace expires
                        # result-less and the next _pick_next re-classifies it as a
                        # wedge (resolve_story_spec degrades an undecodable PRESENT
                        # spec to status "" → pause for resolve), never a crash of
                        # the read-back poll.
                        sr = None
                    if sr is not None and sr.result_json is not None:
                        return sr
                    verdict = "not-terminal"
                    detail = f"{state.path} has no terminal status (frontmatter {state.status!r})"
            if not wait or time.monotonic() >= deadline:
                if wait:
                    self._note_resultless_stop(handle.task_id, verdict, detail)
                return None
            time.sleep(RESULT_POLL_S)

    @staticmethod
    def _written_this_session(spec_path: Path, launched_ns: int) -> bool:
        """Whether ``spec_path`` was (re)written at/after the session launched — the
        same launch-floor guard ``devcontract.find_result_artifact`` applies on the
        scan path, so a stale terminal spec from a prior step (a dev ``done`` a
        follow-up review re-opens) is not mistaken for this session's output. A spec
        that vanished between resolve and stat is treated as not-yet-written."""
        try:
            return spec_path.stat().st_mtime_ns >= launched_ns
        except OSError:
            return False

    def _post_kill_reconcile(
        self, handle: SessionHandle, spec: SessionSpec, result: SessionResult
    ) -> SessionResult:
        """Rescue a finished-but-unvouched session once its window is dead (#61).

        A session that wrote its terminal spec but whose final Stop event was
        lost ends ``stalled`` (nudge-unresponsive under a live window, where
        the artifact is advisory — the #48/#53 invariant), or ``timeout`` when
        no hook event ever arrived (hook misconfig, events-dir write failure —
        that path never arms the stall grace at all). Both verdicts discard
        the on-disk result solely because the window was alive to distrust;
        ``run()``'s kill has since settled that the way window death already
        vouches for the crash path. So: re-probe, and only on a provably dead
        window re-run the same read-back a delivered Stop would have run.

        The gate is deliberately stricter than the crash path's
        accept-any-terminal: the synthesis must be self-consistent
        (``status_consistent`` — "no active disagreement"; a blank frontmatter
        with prose ``done`` passes, exactly what a delivered Stop would have
        synthesized, and the engine's reconcile repairs the lag) and a
        *successful* terminal — ``done``, or the stories plan-halt leg (a
        deliberate widening of #61's literal done-only wording). A ``blocked``
        terminal is never rescued: it carries no finished work, and
        blocked-plus-nudge-unresponsive is weak evidence of anything. Every
        rescue still runs the engine's full deterministic verify downstream,
        so a bogus upgrade degrades into an ordinary verify-failed retry. A
        cap-exhausted injected-workflow stall whose marker landed before the
        kill is rescued by the same trust model. ``over_budget`` joins the set
        (#158): an artifact the wrap-up nudge flushed at kill-time is honored
        the same way."""
        if (
            result.status not in ("stalled", "timeout", "over_budget")
            or result.result_json is not None
        ):
            return result
        alive = self._probe_alive(handle)
        if alive:
            # The kill silently failed (best-effort teardown): the window
            # is still alive, so the live-window invariant still applies.
            return result
        if alive is None:
            return result  # liveness unknowable: unknown is not dead
        try:
            sr = self._synth_result(handle, spec, wait=False)
        except (OSError, UnicodeDecodeError):
            # An unreadable artifact is not evidence a session finished. This
            # hook runs right after run()'s finally-kill — the moment a spec the
            # CLI was mid-write is truncated, possibly through a multi-byte UTF-8
            # sequence — so a corrupt read is the *expected* fault here, not an
            # anomaly. Keep the verdict: a best-effort rescue must never escalate
            # a clean stall/timeout into an exception, which the engine does not
            # contain per-task (it fails the whole run). UnicodeDecodeError is a
            # ValueError, so both must be named.
            return result
        if sr is None or sr.result_json is None or not sr.status_consistent:
            return result
        rj = sr.result_json
        if rj.get("escalations") or not (
            rj.get("status") == devcontract.DONE or rj.get("plan_halt") is True
        ):
            return result
        rj["post_kill_reconciled"] = True
        return SessionResult(
            status="completed",
            result_json=rj,
            session_id=result.session_id,
            transcript_path=result.transcript_path,
            # a rescued timeout upgrades the outcome, not the timing evidence:
            # the deadline did fire on this session, and that record must
            # survive the rescue (#157). Same for a tripped budget's sample.
            timeout_fired_at=result.timeout_fired_at,
            timeout_expired_clock=result.timeout_expired_clock,
            budget_weighted=result.budget_weighted,
        )


class GenericDevAdapter(_DevSynthesisMixin, GenericAdapter):
    """Dev adapter for Alex Verhovsky's generic ``bmad-dev-auto`` skill.

    That skill writes NO ``result.json`` — its outcome lives in the spec it
    leaves on disk (frontmatter ``status:`` plus an appended ``## Auto Run
    Result``, or, when it never created a spec, a ``bmad-dev-auto-result-*.md``
    fallback). On the Stop event we locate that artifact and synthesize the
    legacy result dict from it via :mod:`devcontract`, so verify/escalation and
    the rest of the pipeline consume it unchanged. Selected by
    ``policy.dev.skill == "bmad-dev-auto"`` (see ``cli._make_adapters``).
    """

    def __init__(self, *args, paths: ProjectPaths, **kwargs):
        super().__init__(*args, **kwargs)
        self.paths = paths
        self._configure_dev_knobs()

    def _probe_alive(self, handle: SessionHandle) -> bool | None:
        try:
            return self._window_alive(handle)
        except MultiplexerError:
            return None


# Back-compat alias: the adapter was ``GenericTmuxAdapter`` before tmux moved
# behind the multiplexer seam. Keeps existing imports stable.
GenericTmuxAdapter = GenericAdapter
