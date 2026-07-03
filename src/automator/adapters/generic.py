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

from .. import devcontract, runs
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR
from ..model import TokenUsage
from ..policy import Policy
from ..signals import SignalWatcher
from ..tokens import read_usage as tally_usage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec
from .multiplexer import MultiplexerError, TerminalMultiplexer, get_multiplexer
from .profile import CLIProfile

# Pane geometry for agent windows; mirrored in tui.data for log emulation.
PANE_COLUMNS = 220
PANE_LINES = 50
RESULT_GRACE_S = 15.0
RESULT_POLL_S = 0.5
EVENT_KINDS = {"SessionStart", "Stop", "SessionEnd"}
NUDGE_TEXT = (
    "You are running in bmad-auto automation mode. Finish the workflow now: "
    "complete any remaining steps and write the result JSON file to "
    "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json, then end your turn."
)
# Wake an idle dev session whose grace window elapsed with no output. bmad-auto
# has no background-completion re-invocation, so a turn ended to await a slow
# background process (a Unity PlayMode run, a long test) would otherwise wait
# forever; this nudge IS that re-invocation. Skill-agnostic: it must not assume a
# result.json (the bmad-dev-auto skill writes none — see GenericDevAdapter).
STALL_NUDGE_TEXT = (
    "You appear idle in bmad-auto automation mode, which cannot re-invoke you when "
    "a background process finishes. If you are waiting on one (e.g. a Unity PlayMode "
    "run or a long test), check its status now and continue the workflow; if it is "
    "done, finalize the work and end your turn. If you are stuck, say so and stop."
)


class GenericAdapter(CodingCLIAdapter):
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
        self.session_name = f"bmad-auto-{run_dir.name}"
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
            # never prunes this run (run_dir = <project>/.automator/runs/<id>).
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

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = self.tasks_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "prompt.txt").write_text(spec.prompt + "\n", encoding="utf-8")
        # A re-armed/resumed run reuses task_ids; drop any prior cycle's result
        # so a session that writes nothing can't be read as a stale completion.
        (task_dir / "result.json").unlink(missing_ok=True)

        self._ensure_session(spec.cwd)
        # Stamped before launch: hook events carry wall-clock ns, and
        # wait_for_completion ignores anything older than this floor so a reused
        # task_id's earlier Stop event cannot replay.
        launched_ns = time.time_ns()
        window_id = self.mux.new_window(
            self.session_name,
            spec.task_id[-40:],
            spec.cwd,
            {**self.profile.env, **spec.env},
            self.build_command(spec),
        )
        log_file = self.logs_dir / f"{spec.task_id}.log"
        # pipe_pane tolerates the window having already died (a CLI that crashes on
        # launch can take it down before the tee attaches); the dead window is then
        # reported as a crash in wait_for_completion.
        self.mux.pipe_pane(window_id, log_file)
        return SessionHandle(task_id=spec.task_id, native_id=window_id, launched_ns=launched_ns)

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        deadline = time.monotonic() + spec.timeout_s
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
        # it (bmad-auto has no background re-invocation) instead of stalling. A
        # fresh Stop — proof it woke and acted — restores the budget; only an
        # unresponsive session burns through it. Bounded overall by spec.timeout_s.
        stall_nudges_left = self._stall_nudges
        # monotonic total of stall nudges sent this session — never restored,
        # unlike stall_nudges_left. When spec.stall_nudges_cap is set (injected
        # workflow sessions), a session that keeps ending its turn without a
        # result cannot ride the fresh-Stop refill forever: after cap total
        # nudges it is declared stalled. cap=None (dev/review) skips the check.
        stall_nudges_sent = 0
        # internal observability counter: counts ticks where the liveness probe
        # raised a transport error (e.g. a 30s tmux hang). It deliberately does
        # NOT escalate to "crashed" — a transient transport hiccup is not proof
        # of death; spec.timeout_s already bounds a persistent failure to a
        # timeout.
        probe_failures = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SessionResult(
                    status="timeout",
                    session_id=session_id,
                    transcript_path=transcript_path,
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
                    return self._final(handle, spec, "crashed", session_id, transcript_path)
                if stall_deadline is not None:
                    # A terminal result can land mid-grace without a fresh Stop to
                    # wake us; check non-blocking on each idle tick so completion
                    # isn't deferred to grace expiry (up to dev_stall_grace_s late).
                    result_json = self._result_json(handle, spec, wait=False)
                    if result_json is not None:
                        return SessionResult(
                            status="completed",
                            result_json=result_json,
                            session_id=session_id,
                            transcript_path=transcript_path,
                        )
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
                    # the grace window elapsed with no re-invocation; one last
                    # non-blocking check in case the spec landed without a fresh
                    # Stop.
                    result_json = self._result_json(handle, spec, wait=False)
                    if result_json is not None:
                        return SessionResult(
                            status="completed",
                            result_json=result_json,
                            session_id=session_id,
                            transcript_path=transcript_path,
                        )
                    if stall_nudges_left > 0 and (
                        spec.stall_nudges_cap is None or stall_nudges_sent < spec.stall_nudges_cap
                    ):
                        # The wake nudge IS the re-invocation bmad-auto otherwise
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
                    return self._final(handle, spec, "stalled", session_id, transcript_path)
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
                    )
                if nudges_left > 0:
                    nudges_left -= 1
                    self.send_text(handle, NUDGE_TEXT)
                    continue
                if self._stall_grace_s <= 0:
                    return self._final(handle, spec, "stalled", session_id, transcript_path)
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
                return self._final(handle, spec, "crashed", session_id, transcript_path)

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
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status."""
        result_json = self._result_json(handle, spec, wait=False)
        status = "completed" if result_json is not None else fallback
        return SessionResult(
            status=status,
            result_json=result_json,
            session_id=session_id,
            transcript_path=transcript,
        )

    def _result_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "result.json"

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
            if result is not None or time.monotonic() >= deadline:
                return result
            time.sleep(RESULT_POLL_S)

    def _window_alive(self, handle: SessionHandle) -> bool:
        return handle.native_id in self.mux.list_window_ids(self.session_name)

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self.mux.send_text(handle.native_id, text)

    def kill(self, handle: SessionHandle) -> None:
        self.mux.kill_window(handle.native_id)

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


class GenericDevAdapter(GenericAdapter):
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
        # The generic skill never writes result.json, so the base "write the
        # result JSON file" nudge is meaningless — and actively misleading — for
        # it. A Stop without a terminal spec is a stall *unless* the session
        # merely ended its turn to await a background process and will be re-
        # invoked on completion; the idle-grace window distinguishes the two.
        self._stop_nudges = 0
        self._stall_grace_s = float(self.policy.limits.dev_stall_grace_s)
        self._stall_nudges = int(self.policy.limits.dev_stall_nudges)

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
        # Mirror the base _await_result poll: the skill's terminal spec may not be
        # flushed to disk the instant the Stop event fires, so briefly await it when
        # wait=True instead of reading once and mis-reporting a stall.
        deadline = time.monotonic() + RESULT_GRACE_S
        search_dirs = self._artifact_dirs(spec.cwd)
        while True:
            for artifacts in search_dirs:
                spec_path = devcontract.find_result_artifact(artifacts, since_ns=handle.launched_ns)
                if spec_path is not None:
                    story_key = spec.env.get("BMAD_AUTO_STORY_KEY") or None
                    # Bundle dev sessions: the orchestrator exports the bundle's
                    # owned dw ids (the generic skill never authors them). Stamp
                    # them onto the result so verify_dev_bundle's cross-check passes.
                    raw_dw_ids = (spec.env.get("BMAD_AUTO_DW_IDS") or "").split(",")
                    dw_ids = [tok for tok in (i.strip() for i in raw_dw_ids) if tok]
                    return devcontract.synthesize_result(
                        spec_path, story_key=story_key, dw_ids=dw_ids or None
                    ).result_json
            if not wait or time.monotonic() >= deadline:
                return None
            time.sleep(RESULT_POLL_S)


# Back-compat alias: the adapter was ``GenericTmuxAdapter`` before tmux moved
# behind the multiplexer seam. Keeps existing imports stable.
GenericTmuxAdapter = GenericAdapter
