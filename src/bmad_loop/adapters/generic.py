"""Generic coding-CLI driver: interactive sessions in tmux windows, observed via hooks.

Each pipeline step gets a fresh tmux window running the full interactive CLI
with the skill invocation as the initial prompt. Completion is detected
exclusively through hook-written event files (Stop/SessionEnd) plus the
presence of the skill-written result.json — the pane log's *contents* never
drive the wait loop (only tee'd for human debugging), though its *growth*
(mtime/size, never the bytes — see ``_log_activity_key``) is read as a liveness
signal to re-arm the dev-stall grace window. The one exception is post-mortem:
after the verdict and reconcile have settled, a single tail read of the log
classifies a transport-failure environment fault (#194, see
``_classify_env_fault``) — it labels the result, it never drives the wait loop.

Everything CLI-specific (binary, prompt rendering, bypass flags, usage
parser) comes from a declarative CLIProfile; each CLI's hook config registers
the shared relay script under its native event names but passes the canonical
event name as argv, so this adapter only ever sees canonical events. CLIs
without a SessionEnd hook (e.g. Codex) are covered by the window-death
fallback.
"""

from __future__ import annotations

import copy
import enum
import hashlib
import json
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from .. import devcontract, gates, runs
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR, TASK_CYCLE_ARTIFACTS
from ..model import TokenUsage
from ..policy import Policy
from ..process_host import ProcessHostError, get_process_host
from ..signals import SignalWatcher
from ..tokens import read_usage as tally_usage
from ..verify import read_frontmatter, status_of
from .base import (
    CodingCLIAdapter,
    SessionHandle,
    SessionResult,
    SessionSpec,
    SpecSnapshot,
    reset_task_prompt,
    validate_adapter_artifact_paths,
    validated_task_directory,
)

# Re-exported for importers that predate the env_fault module split (#194 landed
# these names on this module); the definitions now live in .env_fault. The
# redundant `X as X` form is the explicit-re-export spelling — it tells the linter
# these are deliberate pass-throughs, without an `__all__` that would read as a
# statement of this module's public API and understate it (callers also import
# GenericTmuxAdapter, the *_NUDGE_TEXT constants and HEARTBEAT_INTERVAL_S).
#
# READ-ONLY. An import copies the object binding, so these names are aliases, not
# a window onto env_fault's globals: reading them is exact, but REBINDING one here
# (`monkeypatch.setattr(generic, "ENV_FAULT_MATCH_TIMEOUT_S", ...)`) is invisible to
# the classifier, which resolves the constant from its own module at call time. That
# is not hypothetical — it silently defused the pathological-regex test the split
# inherited. Override at the definition site (`env_fault.<NAME>`) instead.
from .env_fault import _ANSI_RE as _ANSI_RE
from .env_fault import ENV_FAULT_EVIDENCE_MAX as ENV_FAULT_EVIDENCE_MAX
from .env_fault import ENV_FAULT_MATCH_TIMEOUT_S as ENV_FAULT_MATCH_TIMEOUT_S
from .env_fault import ENV_FAULT_STATUSES as ENV_FAULT_STATUSES
from .env_fault import ENV_FAULT_TAIL_BYTES as ENV_FAULT_TAIL_BYTES
from .env_fault import EnvFaultMixin
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
# Missing-marker fallback (#224): how many consecutive resultless-Stop
# observations of an IDENTICAL (path, mtime, status) fingerprint a marker-less
# terminal spec must survive before it is synthesized as this session's result.
# One observation is not enough: right after a review launch the spec still
# carries the dev pass's `done` frontmatter, and the review's first write can
# bump its mtime past the launch floor before the status flips to `in-review` —
# harvesting on that single sighting would score a review that never ran (#261).
# Two stable sightings bracket a full stall-grace + nudge with zero writes, which
# a session mid-edit cannot produce. A dead window skips the counter entirely:
# the kill settled liveness, so the frontmatter is as final as it will ever get.
FM_FALLBACK_MIN_OBS = 2
# Proof-of-work gate (#261): pane-log size, in bytes, above which a session counts
# as having produced SOMETHING — the floor a dead session must clear before a
# read-back artifact may upgrade its verdict to `completed`. Not zero: the three
# wedged sessions in #261 left logs of 0 and 2 bytes, so `size > 0` would have
# cleared one of them. The separation is wide in the observed data — that run's
# working dev session logged 1.4 MB against the wedged reviews' 0 and 2 — so the
# exact value is not load-bearing; it only has to sit above the noise a pane can
# accumulate without the CLI rendering anything. Note the floor measures the CLI's
# OWN output: the orchestrator's prompt is delivered by send-keys and a program
# that never echoes it leaves the log empty (measured), so this is not a proxy for
# "the session was launched" — only for "the CLI rendered something".
PROOF_OF_WORK_MIN_LOG_BYTES = 256


class _SnapVerdict(enum.Enum):
    """Launch-snapshot (#276 M1/M2) decision, shared by the mtime-scan fallback and
    the stories read-back so the two completion paths can never drift.

    NEUTRAL — no snapshot, a different file, or bytes changed since launch: fall
    through to the path's normal accept logic.
    PROVEN  — a mid-session status transition (M2) was observed for this spec:
    single-sighting harvest, and it OUTRANKS a byte-identical hash (a clean review
    can round-trip back to the launch bytes yet provably ran).
    REFUSE  — bytes still byte-identical to the review-launch snapshot AND no
    transition was observed (M1): the documented dead-window false positive
    (a `done` spec re-opened for review, mtime-bumped but never re-driven).
    """

    NEUTRAL = "neutral"
    PROVEN = "proven"
    REFUSE = "refuse"


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
# result.json (the bmad-build-auto skill writes none — see GenericDevAdapter).
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
# Targeted contract-repair nudge (#276 M4): a Stop found the spec at
# {spec_path} finalized to terminal frontmatter status {status} but WITHOUT the
# `## Auto Run Result` section bmad-loop's harvest scan keys on. Ask the skill to
# append that section itself so the omission is fixed at the source (a compliant
# append is then harvested by the normal scan; harness-side frontmatter synthesis
# stays the backstop). Sent at most once per session and never re-armed, so it is
# safe to be specific and directive. Guarded ("if this spec is not yours or the
# work is unfinished") so a session legitimately mid-workflow is not derailed.
CONTRACT_NUDGE_TEXT = (
    "You are running in bmad-loop automation mode. The spec at {spec_path} now "
    "carries a terminal frontmatter `status: {status}`, but it is missing the "
    "`## Auto Run Result` section your contract requires — bmad-loop harvests "
    "that section, not the frontmatter, so without it this finished story looks "
    "unfinished. If this spec is yours and the work is done, append the section "
    "to the spec now — the `## Auto Run Result` heading, a `Status: {status}` "
    "line matching the frontmatter, and a brief summary — then end your turn. If "
    "this spec is not yours, or the work is not actually finished, ignore this "
    "and continue your workflow instead."
)


class _ResultFileMixin:
    """Result-file read-back and verdict finalization: acquire the
    skill-written result dict and fold it into the session's final
    ``SessionResult``. Transport-agnostic — shared by the tmux adapters and
    any adapter whose skill writes ``tasks/<task_id>/result.json``; needs
    only ``self.tasks_dir`` and ``self.run_dir``."""

    # Set by the concrete adapter's __init__; bare annotations (no runtime
    # effect) tell the type checker the host attributes this mixin reads.
    tasks_dir: Path
    run_dir: Path

    # Whether `_final` applies the #261 proof-of-work gate to its read-back. False
    # here, and that is not a conservative default — it is the correct answer for
    # this mixin's own read-back. `tasks/<task_id>/result.json` is task-unique and
    # `start_session` unlinks it before launch, so its presence is already proof
    # THIS session wrote it; a foreign writer cannot reach it. Gating it could only
    # ever downgrade an authoritative completion. Overridden True by
    # `_DevSynthesisMixin`, whose read-back scans a directory shared with every
    # concurrent run — the one place a result can belong to somebody else.
    _READBACK_NEEDS_PROOF_OF_WORK = False

    def _hard_stop_requested(self) -> bool:
        """Has an operator lodged a *hard* stop request that this session must
        honor (#319)? Either this run's own, or the owning run's.

        Polled twice per wait-loop iteration by both real adapters — on either
        side of the loop's own blocking wait — so a
        ``bmad-loop stop`` is honored mid-session on platforms where the
        engine's SIGTERM path is unreachable. Read-only by contract: the
        adapter never unlinks ``stop-request.json`` — the engine consumes it
        when it raises, and must still see it to attribute the stop. A torn or
        modeless read already leans ``"graceful"`` inside
        ``read_stop_request_mode``, so this can never abort a session
        spuriously.

        Both dirs are read because a nested auto-sweep is a first-class run *and*
        somebody else's child: it mints its own id and appears in ``list``, so
        ``stop <child-id>`` must still reach it, while ``stop <parent-id>`` lodges
        in a dir this adapter would otherwise never look at. The owner leg is
        hard-only, like this whole predicate — a graceful request already
        suppresses a child sweep from *starting*, and letting one already in flight
        finish is exactly what graceful means."""
        if runs.read_stop_request_mode(self.run_dir) == "hard":
            return True
        owner = runs.owner_run_dir()
        # `!=` is a cheap dedupe for the common top-level case, not a correctness
        # dependency: two spellings of one dir cost a redundant read, same answer.
        return (
            owner is not None
            and owner != self.run_dir
            and runs.read_stop_request_mode(owner) == "hard"
        )

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        """Acquire this session's result dict. Base behavior: read the
        skill-written ``result.json`` (briefly awaiting it on the Stop event,
        reading once otherwise). Subclasses whose skill writes no result.json
        (GenericDevAdapter) override this to synthesize the dict from another
        on-disk artifact."""
        return self._await_result(handle.task_id) if wait else self._read_result(handle.task_id)

    def _produced_work(self, handle: SessionHandle, stop_seen: bool) -> bool:
        """Whether this session shows ANY evidence it actually ran, for the #261
        proof-of-work gate. Deliberately a very low bar — it separates "the CLI
        wedged before it did anything" from "the CLI worked", not good work from bad.

        Two independent signals, ORed, because each has a known blind spot: a `Stop`
        event having arrived covers an adapter whose pane sink is misbound (#254/#217,
        where a HEALTHY session still logs zero bytes), and pane-log growth covers a
        profile whose hooks never fire. Requiring both to be absent is what makes the
        gate safe to apply to a `completed` upgrade.

        The hook signal is `Stop` specifically — a turn that ENDED — not "a hook
        event arrived". Of the three canonical events, `SessionStart` fires before
        the session does anything and `SessionEnd` fires when it stops being one;
        both are emitted by a CLI that launched and wedged, so accepting either
        would leave the gate satisfied in exactly the case it exists to catch. The
        #254/#217 rationale is unaffected: a healthy session ends its turn.

        Unknown never blocks: `_log_evidence` returns None when there is no signal at
        all (no pane log — the opencode-http transport, and every unit-test fixture),
        and that reads as evidence-present, preserving current behavior exactly."""
        if stop_seen:
            return True
        evidence = self._log_evidence(handle)
        return True if evidence is None else evidence

    def _log_evidence(self, handle: SessionHandle) -> bool | None:
        """Tristate pane-log proof-of-work signal: True = the log grew past a
        trivial floor, False = the log exists and did not, None = no such signal for
        this transport. Base: None (inert). Overridden by `GenericAdapter`, which
        tees a pane log."""
        return None

    def _session_vanished(self) -> bool:
        """Whether the whole multiplexer session is gone, asked only once a
        crash verdict has already been reached (#489). Base: False — an adapter
        with no session to lose (opencode-http) never vanishes. Overridden by
        `GenericAdapter`.

        Same failure convention as `_window_alive`: `MultiplexerError` is the
        seam's declared "couldn't ask" and the override swallows it to False.
        Anything else propagates, exactly as it does from the liveness probe —
        this is a label on a verdict already made, so it degrades rather than
        second-guessing the verdict, but it does not swallow unknown faults."""
        return False

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
        stop_seen: bool = False,
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status. ``accept_result=False``
        (a stall verdict reached under a live window) pins the fallback: an
        artifact that appeared without a Stop or window death is not trusted.
        ``budget_weighted`` (a tripped session-budget guard's sample) rides
        every exit so the engine can journal it whatever the verdict.
        ``stop_seen`` is the proof-of-work hook signal, threaded separately from
        ``session_id``/``transcript`` because those are also set by a mere launch."""
        result_json = self._result_json(handle, spec, wait=False) if accept_result else None
        if (
            result_json is not None
            and self._READBACK_NEEDS_PROOF_OF_WORK
            and not self._produced_work(handle, stop_seen)
        ):
            # Proof-of-work gate (#261): this session is gone and produced no
            # observable output at all — no turn ever ended AND its pane log never
            # grew. A read-back artifact is then not evidence THIS session finished;
            # it is evidence that SOMETHING wrote a qualifying file in a directory we
            # share. Keep the fallback verdict rather than upgrade a dead-on-arrival
            # session to `completed`.
            self._note_lifecycle(
                handle.task_id,
                "readback-refused-no-proof-of-work",
                fallback=fallback,
                spec=str(result_json.get("spec_file", "")),
                status=str(result_json.get("status", "")),
            )
            result_json = None
        status = "completed" if result_json is not None else fallback
        # Diagnose the crash verdict only (#489) — see `_session_vanished`. A
        # read-back upgrade to `completed` is deliberately not diagnosed: a
        # session reaped AFTER flushing its result did produce something, and the
        # verdict it earned is the honest one. `crashed` also covers the
        # `SessionEnd` arm of `GenericAdapter.run()`, where the CLI announced
        # its own exit rather than the window dying — the label stays truthful
        # there because it reports what the mux answered, not how the window
        # ended.
        vanished = status == "crashed" and self._session_vanished()
        if vanished:
            # Evidence rides along like every neighbouring crumb: which session
            # went missing (several runs share a host) and what verdict it lands.
            # getattr because the mixin does not declare `session_name` (opencode-
            # http has none) and only a mux-backed adapter can reach this branch
            # (the base `_session_vanished` is a constant False). No default — an
            # override on an adapter without a session name must fail loud here,
            # not write evidence-free crumbs.
            self._note_lifecycle(
                handle.task_id,
                "session-vanished",
                session=getattr(self, "session_name"),
                status=status,
            )
        return SessionResult(
            status=status,
            result_json=result_json,
            session_id=session_id,
            transcript_path=transcript,
            budget_weighted=budget_weighted,
            stop_seen=stop_seen,
            session_vanished=vanished,
        )

    def _result_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "result.json"

    def _append_diag_jsonl(self, task_id: str, filename: str, payload: dict) -> None:
        """Append ``payload`` as one JSON line to ``tasks/<task_id>/<filename>``.
        Pure observability, best-effort: an unwritable run dir must never break
        the completion loop. ``ensure_ascii=False`` is why the guard names more
        than OSError: it leaves a lone surrogate — what a POSIX filename holding
        a non-UTF-8 byte becomes, surrogate-escaped — in the dumped str, which
        then hits the UTF-8 encode inside ``fh.write`` as a UnicodeEncodeError.
        That is a ValueError, not an OSError; ``UnicodeError`` covers it and the
        decode direction both (#380)."""
        try:
            path = self.tasks_dir / task_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except (OSError, UnicodeError):
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
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            # Plugin HookContext makes this same defensive copy before exposing
            # result data, so reject a shape that would recurse there while the
            # artifact is still inside the shared observation boundary.
            copy.deepcopy(data)
            # JSON accepts escaped lone surrogates, but the default ATTENTION
            # sink writes reasons as UTF-8. Validate every parsed string without
            # imposing stricter numeric semantics on completed session results.
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        except (OSError, ValueError, RecursionError):
            return None
        return data

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


class GenericAdapter(_ResultFileMixin, EnvFaultMixin, CodingCLIAdapter):
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
        events_dir: Path | None = None,
    ):
        self.run_dir = run_dir
        self.policy = policy
        self.profile = profile
        # env-fault patterns compile lazily off self.profile — see EnvFaultMixin.
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
        # The run's hook-event channel (#494): the out-of-tree directory the run
        # bootstrap resolved, plus the legacy in-tree one kept under poll so a
        # project whose installed relay predates the move still completes its
        # sessions. `events_dir` is handed in rather than derived here because
        # deriving it needs the PROJECT, and the only project this class can
        # reach is `run_dir.parents[2]` — a shape real run dirs have and test run
        # dirs do not, so a derivation would key the watcher off a directory that
        # is not the project (see `_ensure_session`, which accepts exactly that
        # weakness for a session tag but must not for the completion channel).
        # Defaulting to the legacy dir keeps direct construction (tests, any
        # caller outside `runsetup.make_adapters`) working unchanged; the
        # bootstrap always passes one, pinned by a test.
        self.watcher = SignalWatcher(events_dir or run_dir / "events", run_dir / "events")
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
        # The pin chokepoint (runs.pin_state_root): the profile's [env] table
        # must not be able to move a session off this process's state root —
        # including when no root derives, where there is no pin key for a mere
        # spread ordering to protect. `start_session`'s window merge applies
        # the same rule.
        return runs.pin_state_root({**self.profile.env, **spec.env})

    def build_command(self, spec: SessionSpec) -> str:
        return " ".join(shlex.quote(a) for a in self.interactive_argv(spec))

    # --------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = validated_task_directory(self.tasks_dir, spec.task_id)
        validate_adapter_artifact_paths(
            task_dir,
            tuple(
                task_dir / name
                for name in (
                    "heartbeat.json",
                    "resultless-stops.jsonl",
                    "session-lifecycle.jsonl",
                )
            ),
        )
        validate_adapter_artifact_paths(
            self.logs_dir,
            (self.logs_dir / f"{spec.task_id}.log",),
        )
        task_dir.mkdir(parents=True, exist_ok=True)
        reset_task_prompt(task_dir, spec.prompt)
        # Task ids are supplied by the caller, so defensively reset cycle-scoped
        # outputs if one is reused. A silent session must not inherit a stale result.
        # The list is `journal.TASK_CYCLE_ARTIFACTS` rather than two literals here:
        # `resolve._gather_escalations` reads the same names back, so a third
        # artifact must not be able to reach the reader while missing this adapter.
        for artifact in TASK_CYCLE_ARTIFACTS:
            (task_dir / artifact).unlink(missing_ok=True)

        self._ensure_session(spec.cwd)
        # Stamped before launch: hook events carry wall-clock ns, and
        # wait_for_completion ignores anything older than this floor so a reused
        # task_id's earlier Stop event cannot replay.
        launched_ns = time.time_ns()
        log_file = self.logs_dir / f"{spec.task_id}.log"
        # A re-armed run reuses task_ids and both mux backends append; drop the prior
        # cycle's tee so the #194 tail scan can't match a stale transport error (mirrors
        # the result.json unlink above; journal.py already assumes "next session replaces it").
        log_file.unlink(missing_ok=True)
        # ...then create it EMPTY, before the window exists. `pipe_pane` below tolerates
        # a window that already died and then attaches no tee, so without this a
        # dead-on-arrival session leaves NO log at all — and an absent log is the
        # `_log_evidence` "this transport has no pane signal" state, which the #261
        # proof-of-work gate treats as inert. The gate would fail OPEN in exactly the
        # case it exists to catch. A 0-byte file says something truer and stronger:
        # this transport does tee a pane, and this session rendered nothing into it.
        # (Both backends append, so pre-creating cannot truncate a live tee. Stall
        # detection is unaffected: `_log_activity_key` reports (mtime, 0) instead of
        # None, and every reader compares signatures rather than testing existence.)
        log_file.touch()
        window_id = self.mux.new_window(
            self.session_name,
            spec.task_id[-40:],
            spec.cwd,
            # Same merge as interactive_env, same pin chokepoint: the profile's
            # [env] table must not move the window off this process's state root.
            runs.pin_state_root({**self.profile.env, **spec.env}),
            self.build_command(spec),
        )
        # pipe_pane tolerates the window having already died (a CLI that crashes on
        # launch can take it down before the tee attaches); the dead window is then
        # reported as a crash in wait_for_completion.
        self.mux.pipe_pane(window_id, log_file)
        return SessionHandle(task_id=spec.task_id, native_id=window_id, launched_ns=launched_ns)

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
        # Positive grace arms at launch for dev/review sessions, so a CLI that
        # goes silent before its first Stop cannot burn the full wall timeout. A
        # fresh Stop or later pane growth re-arms it; None = grace disabled.
        stall_deadline = time.monotonic() + self._stall_grace_s if self._stall_grace_s > 0 else None
        # pane-log activity signature captured when the grace window is armed; a
        # session streaming output (a long productive turn, a streaming subagent)
        # advances it and re-arms the window, so only genuine silence stalls.
        last_activity = (
            self._log_activity_key(handle.task_id) if stall_deadline is not None else None
        )
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
        # latched on the first accepted `Stop`: the hook half of the #261 proof-of-work
        # gate. Tracked apart from session_id/transcript_path — those are populated by
        # SessionStart and SessionEnd too, which a CLI that launched and wedged emits
        # without doing any work. Rides out on every exit (see SessionResult.stop_seen)
        # so `_post_kill_reconcile` reads the same signal after run() kills the window.
        stop_seen = False
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
                    stop_seen=stop_seen,
                )
            # Hard-stop poll (#319), per-iteration and deliberately NOT inside
            # the heartbeat throttle below: the loop's own wait is capped at 5s
            # (`watcher.wait_for(..., timeout_s=min(remaining, 5.0))`), so a stop
            # normally lands well inside `stop_run`'s 10s grace window, while riding
            # the 30s HEARTBEAT_INTERVAL_S would be worse than the status quo. Read
            # that as the common case, not a bound: an iteration that goes on to
            # wait RESULT_GRACE_S for an artifact, or to block on a tmux call under
            # TMUX_TIMEOUT_S, exceeds the grace window on its own. See the second
            # poll after the wait below for how the interval is split, and why it
            # still cannot be made unconditionally short. Return the verdict — never raise `RunStopped` here: that would
            # skip `run()`'s finally-kill + `_post_kill_reconcile`. The file is
            # left on disk for the engine to consume and attribute the stop.
            if self._hard_stop_requested():
                self._note_lifecycle(handle.task_id, "stop-abort-fired")
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
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
                # Mid-session spec-status transition sampling (#276 M2) rides the
                # same heartbeat cadence — a no-op unless this adapter drives the
                # generic skill and the engine threaded a launch snapshot.
                self._observe_tick(handle, spec)
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
                                            stop_seen=stop_seen,
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
                                    stop_seen=stop_seen,
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
                            stop_seen=stop_seen,
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
                    stop_seen=stop_seen,
                )
            event = self.watcher.wait_for(
                handle.task_id,
                EVENT_KINDS,
                timeout_s=min(remaining, 5.0),
                since_ns=handle.launched_ns,
            )
            # Second poll, and the reason there are two (#319). The arm at the top of
            # the loop is separated from its next run by everything between: the 5s
            # wait above, plus whichever dispatch leg the event selects — a
            # `_window_alive` or `send_text` bounded only by TMUX_TIMEOUT_S (30s), or
            # a `_result_json(wait=True)` that waits RESULT_GRACE_S (15s) for an
            # artifact. The last of those alone outlasts `stop_run`'s 10s grace on a
            # perfectly healthy box, with no transport fault anywhere. Polling here
            # splits the iteration so at most one leg sits between two checks. It
            # cannot make the interval unconditionally short — an in-flight
            # subprocess is not interruptible from this thread — so a leg that does
            # outlast the window still degrades to `stop_run`'s force-kill backstop:
            # the pre-#319 outcome, never a worse one.
            if self._hard_stop_requested():
                self._note_lifecycle(handle.task_id, "stop-abort-fired")
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
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
                        stop_seen=stop_seen,
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
                        try:
                            self.send_text(handle, STALL_NUDGE_TEXT)
                        except MultiplexerError:
                            # A dead/hung window cannot take the nudge. The
                            # bounded attempt is still spent, and the next tick's
                            # ordinary liveness probe owns the verdict.
                            pass
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
                                stop_seen=stop_seen,
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
                        stop_seen=stop_seen,
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
                # A turn ENDED — the one canonical event that proves the CLI did
                # something, and so the hook half of the #261 proof-of-work gate.
                # Latched after the subagent filter above, which rejects a stop that
                # is not the main session's turn-end. Never cleared.
                stop_seen = True
                result_json = self._result_json(handle, spec, wait=True)
                if result_json is not None:
                    return SessionResult(
                        status="completed",
                        result_json=result_json,
                        session_id=session_id,
                        transcript_path=transcript_path,
                        budget_weighted=budget_weighted,
                        stop_seen=stop_seen,
                    )
                if nudges_left > 0:
                    nudges_left -= 1
                    try:
                        self.send_text(handle, NUDGE_TEXT)
                    except MultiplexerError:
                        # The next deterministic liveness probe decides whether
                        # the un-nudgeable window is dead or merely unavailable.
                        pass
                    continue
                if self._stall_grace_s <= 0:
                    return self._final(
                        handle,
                        spec,
                        "stalled",
                        session_id,
                        transcript_path,
                        budget_weighted=budget_weighted,
                        stop_seen=stop_seen,
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
                    stop_seen=stop_seen,
                )

    def _log_evidence(self, handle: SessionHandle) -> bool | None:
        """Pane-log half of the #261 proof-of-work gate (see
        `_ResultFileMixin._produced_work`). The pane is tee'd to a stable inode, so
        its size is a direct measure of how much the session emitted.

        True iff the log exceeded `PROOF_OF_WORK_MIN_LOG_BYTES` (see that constant for
        why the floor is not zero, and for what it does and does not prove). This
        measures rendering, not liveness, which is why `_produced_work` ORs it with
        the turn-ended signal rather than trusting it alone.

        None when the log does not exist — no signal, gate inert. `start_session`
        creates it empty before the window exists, precisely so a session that died
        on arrival reports False (rendered nothing) rather than None (no such signal):
        the DOA case is the gate's whole purpose and must not read as unknown. What
        is left in the None state is a handle this adapter never launched — unit
        fixtures — for which "unknown never blocks" is the right and only answer."""
        try:
            size = (self.logs_dir / f"{handle.task_id}.log").stat().st_size
        except OSError:
            return None
        return size > PROOF_OF_WORK_MIN_LOG_BYTES

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

    def _session_vanished(self) -> bool:
        # The disambiguating probe (#489): `list_window_ids` returns [] for a
        # dead window AND for a session that no longer exists, so a plain
        # window-death verdict cannot tell an exited CLI from a session destroyed
        # under the run. Only `has_session` separates them.
        #
        # The destroyer is NOT necessarily foreign. Candidates: an external
        # reaper (psmux/psmux#546), a concurrent
        # `runs.kill_session` from this tool's own prune/stop/crash paths or the
        # TUI, an operator `kill-session`, a mux server crash, the host sleeping.
        # The reason text stays neutral about which, because this probe cannot
        # tell them apart — it reports that the mux no longer answers for the
        # session, nothing more.
        #
        # Safe to ask this late: `run()`'s teardown kills the WINDOW, never the
        # session, so our own kill cannot fake a vanishing, and a session once
        # gone stays gone.
        try:
            return not self.mux.has_session(self.session_name)
        except MultiplexerError:
            # Unknown is not vanished — the same rule the liveness probe follows.
            return False

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
            except Exception:  # nosec B110 - already-gone races are fine
                pass
        for pid, identity in tree.items():
            if pid not in repane and identity is not None and host.alive_and_ours(pid, identity):
                try:
                    host.force_kill(pid)
                except Exception:  # nosec B110 - already-gone races are fine
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
                except Exception:  # nosec B110 - already-gone races are fine
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


class _SessionStarter(Protocol):
    """Next concrete adapter in the dev mixin's cooperative MRO."""

    def start_session(self, spec: SessionSpec) -> SessionHandle: ...


class _DevSynthesisMixin(_ResultFileMixin):
    """Result synthesis for the generic ``bmad-build-auto`` skill, shared by
    every transport that drives it (tmux today; see GenericDevAdapter for the
    skill contract). Locates the terminal spec the skill leaves on disk and
    synthesizes the legacy result dict via :mod:`devcontract`. Hosts provide
    ``self.paths`` (a :class:`ProjectPaths`), the ``self.policy`` knobs read
    by ``_configure_dev_knobs``, and the ``_probe_alive`` liveness seam."""

    # Set by the concrete adapter's __init__ (see docstring); bare annotations
    # (no runtime effect) tell the type checker the host attributes this reads.
    paths: ProjectPaths
    policy: Policy
    # The concrete adapter's real transport (GenericDevAdapter/OpencodeDevAdapter
    # both define `def send_text`). Declared here as a BARE annotation, never a
    # `def`: the mixin precedes the concrete adapter in MRO, so a stub method
    # would shadow the real one on both adapters. The contract nudge (#276 M4)
    # sends through it.
    send_text: Callable[[SessionHandle, str], None]

    # This mixin's read-back is where #261 lives: the skill writes no task-scoped
    # result.json, so a result is synthesized from a *.md in an implementation-
    # artifacts dir shared with every concurrent run and with the human. A pinned
    # `expected_spec` closes that for the sessions the orchestrator can name, but
    # dev attempt 1 (no spec exists yet) and the labeled-workflow marker still scan.
    # There a qualifying file can belong to someone else, so a dead session must
    # show it ran before its "result" is honored. See `_produced_work`.
    _READBACK_NEEDS_PROOF_OF_WORK = True

    def _configure_dev_knobs(self) -> None:
        """Override the base result-file knobs for the bmad-build-auto contract;
        hosts call this at the end of ``__init__``."""
        # The generic skill never writes result.json, so the base "write the
        # result JSON file" nudge is meaningless — and actively misleading — for
        # it. A Stop without a terminal spec is a stall *unless* the session
        # merely ended its turn to await a background process and will be re-
        # invoked on completion; the idle-grace window distinguishes the two.
        self._stop_nudges = 0
        self._stall_grace_s = float(self.policy.limits.dev_stall_grace_s)
        self._stall_nudges = int(self.policy.limits.dev_stall_nudges)
        # Missing-marker fingerprint observations (#224):
        # task_id -> (path, mtime_ns, frontmatter status, observation count).
        # Task ids are unique per session, so entries never need resetting
        # between sessions; the dict lives for the adapter's lifetime.
        self._fm_fallback_obs: dict[str, tuple[str, int, str, int]] = {}
        # First mid-session spec-status transition observed per session (#276 M2):
        # task_id -> normalized status. Recorded by `_observe_tick` when the spec's
        # frontmatter first moves off its launch status to a non-terminal state (in
        # practice `in-review`), which makes a later terminal frontmatter proof THIS
        # session wrote it. Same lifetime doctrine as `_fm_fallback_obs` — task_ids
        # are unique per session, so entries are recorded once and never cleared.
        self._fm_transition_obs: dict[str, str] = {}
        # Targeted contract-nudge budget (#276 M4): task_ids that have already been
        # sent the one CONTRACT_NUDGE_TEXT nudge. A set, never cleared, so the nudge
        # fires at most once per session even though an mtime bump resets the
        # `_fm_fallback_obs` observation counter to 1 (#149's refill hazard cannot
        # apply — this budget is not a counter and touches no stall counters).
        self._contract_nudge_sent: set[str] = set()
        self._contract_nudge_enabled = self.policy.limits.dev_contract_nudge
        # Marker identities present immediately before each real session launch.
        # The adapter, not whole-file mtime, owns this attempt-relative evidence:
        # touching another part of a parked spec must not make its retained marker
        # look session-authored. A task-level None means directory enumeration was
        # incomplete; a path-level None means that one launch file was unreadable.
        # Both fail closed at the affected scope without letting an unrelated bad
        # Markdown file suppress a newly created, readable story spec.
        self._launch_auto_run_results: dict[str, dict[str, tuple[int, str] | None] | None] = {}

    @staticmethod
    def _marker_path_key(path: Path) -> str:
        # `(OSError, RuntimeError)`, like every other `resolve()` guard in this
        # package: on the 3.11 support floor a symlink LOOP raises RuntimeError,
        # not an OSError (3.13 resolves it silently), and a bare `except OSError`
        # let one looped `*.md` under an artifact dir abort the launch capture —
        # and with it every unpinned dev session — before the transport started.
        try:
            return str(path.resolve())
        except (OSError, RuntimeError):
            return str(path.absolute())

    def _capture_launch_auto_run_results(self, spec: SessionSpec) -> None:
        """Snapshot real result markers before the child can write its spec."""
        paths: list[Path] = []
        complete = True
        if spec.expected_spec:
            expected = Path(spec.expected_spec)
            paths = [expected if expected.is_absolute() else Path(spec.cwd) / expected]
        else:
            for artifacts in self._artifact_dirs(spec.cwd):
                try:
                    paths.extend(artifacts.glob("*.md"))
                except OSError:
                    complete = False

        captured: dict[str, tuple[int, str] | None] = {}
        for path in paths:
            key = self._marker_path_key(path)
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError):
                captured[key] = None
                continue
            fingerprint = devcontract.auto_run_result_fingerprint(text)
            if fingerprint[0]:
                captured[key] = fingerprint
        self._launch_auto_run_results[spec.task_id] = captured if complete else None

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        self._capture_launch_auto_run_results(spec)
        # The mixin is shared by two unrelated concrete transports. Keep the
        # cooperative MRO dispatch rather than naming either host explicitly;
        # the protocol gives Pyright the host contract without adding a runtime
        # base that could alter method resolution.
        return cast(_SessionStarter, super()).start_session(spec)

    def _park_marker_session_authored(self, spec_path: Path, spec: SessionSpec) -> bool:
        """Whether the live marker differs from this session's launch marker."""
        if spec.task_id not in self._launch_auto_run_results:
            # Production always enters through start_session. A direct diagnostic
            # read-back has no attempt-relative evidence and therefore fails closed.
            return False
        captured = self._launch_auto_run_results[spec.task_id]
        if captured is None:
            return False
        try:
            current = devcontract.auto_run_result_fingerprint(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return False
        key = self._marker_path_key(spec_path)
        if key in captured:
            launch = captured[key]
            if launch is None:
                return False
            # Appending another marker is authorship even when its text repeats;
            # an in-place rewrite is authorship when the final section changes.
            # Deleting older sections while retaining the same final marker is not.
            return current[0] > launch[0] or (current[0] == launch[0] and current[1] != launch[1])

        # A marker moved or copied from another launch path is inherited evidence,
        # not a marker authored by this attempt. A genuinely new marker whose text
        # happens to collide also fails closed; byte identity cannot prove authorship.
        if current in (fingerprint for fingerprint in captured.values() if fingerprint):
            return False
        return current[0] > 0

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
        self, handle: SessionHandle, spec: SessionSpec, *, wait: bool, dead_window: bool = False
    ) -> devcontract.SynthResult | None:
        # Stories mode (folder+id dispatch): the story spec lives at a
        # deterministic id-keyed path, so resolve it directly instead of the
        # mtime-floor scan. The engine exports BMAD_LOOP_SPEC_FOLDER only for
        # stories runs, so sprint/sweep runs keep the scan path below unchanged.
        # (dead_window is a scan-path refinement — stories read-back is already
        # frontmatter-authoritative, so it needs no missing-marker fallback.)
        if spec.env.get("BMAD_LOOP_SPEC_FOLDER"):
            return self._stories_synth_result(handle, spec, wait=wait)
        # Authoritative-path read-back (#261): when the orchestrator already knows
        # which spec this session owes — every review leg and every dev retry, see
        # SessionSpec.expected_spec — read THAT file and never the directory scan
        # below. The scan asks "what is the newest qualifying *.md here" of a
        # directory shared with every concurrent run; the question is "what did THIS
        # session write for THIS story", and here we were told the answer at launch.
        if spec.expected_spec:
            # The engine always threads an absolute path (StoryTask.spec_file is
            # re-absolutized against the worktree on resume). Rebase a relative one
            # against spec.cwd anyway, the same way the stories read-back handles
            # BMAD_LOOP_SPEC_FOLDER: a path resolved against the process CWD would
            # simply miss and read as "the session wrote nothing", turning this
            # guard into the silent work-losing failure it exists to avoid.
            owed = Path(spec.expected_spec)
            if not owed.is_absolute():
                owed = Path(spec.cwd) / owed
            return self._known_spec_synth_result(
                handle, spec, owed, wait=wait, dead_window=dead_window
            )
        # Dev attempt 1 only: no spec exists yet (the skill creates it), so the
        # mtime-floor scan is the sole way to find it.
        # Mirror the base _await_result poll: the skill's terminal spec may not be
        # flushed to disk the instant the Stop event fires, so briefly await it when
        # wait=True instead of reading once and mis-reporting a stall.
        deadline = time.monotonic() + RESULT_GRACE_S
        search_dirs = self._artifact_dirs(spec.cwd)
        while True:
            for artifacts in search_dirs:
                spec_path = devcontract.find_result_artifact(artifacts, since_ns=handle.launched_ns)
                if spec_path is not None:
                    return self._synthesize_from(spec_path, spec)
            if not wait or time.monotonic() >= deadline:
                return self._frontmatter_fallback(
                    handle, spec, search_dirs, wait=wait, dead_window=dead_window
                )
            time.sleep(RESULT_POLL_S)

    def _known_spec_synth_result(
        self,
        handle: SessionHandle,
        spec: SessionSpec,
        path: Path,
        *,
        wait: bool,
        dead_window: bool,
    ) -> devcontract.SynthResult | None:
        """Read back from the ONE spec the session was required to write (#261).

        Structurally the scan path with the candidate source replaced: poll the
        marker predicate on this single file over the same ``RESULT_GRACE_S`` flush
        window, then hand the same file to the missing-marker fallback (#224) via
        its ``only`` seam, so the stability fingerprint, the M2 transition
        observation and the M1 launch-snapshot gate all still apply — scoped to the
        one legitimate path instead of a shared directory.

        No whole-file launch-snapshot gate is needed on the marker branch itself: the
        pre-review-launch strip (`Engine._reset_spec_for_review`) REMOVES the
        marker, so a spec carrying one again has necessarily changed bytes since the
        snapshot and the gate would be a no-op (`_snapshot_verdict` → NEUTRAL).
        Marker-level launch capture still runs for every real session: it prevents
        an unrelated post-launch touch from lending a retained park marker to the
        new attempt.

        Note this deliberately does NOT fall back to the scan when the expected spec
        yields nothing: a session that did not write the spec it owed produced no
        result, and any other qualifying file in that directory belongs to someone
        else. Returning None routes to the dev-stall grace / crashed verdict — the
        safe direction, and the verdict the two control stories in #261 received."""
        deadline = time.monotonic() + RESULT_GRACE_S
        while True:
            if devcontract.is_result_artifact(path, since_ns=handle.launched_ns):
                return self._synthesize_from(path, spec)
            if not wait or time.monotonic() >= deadline:
                return self._frontmatter_fallback(
                    handle, spec, [], wait=wait, dead_window=dead_window, only=path
                )
            time.sleep(RESULT_POLL_S)

    def _synthesize_from(self, spec_path: Path, spec: SessionSpec) -> devcontract.SynthResult:
        """Shared synthesis call for the marker scan and the missing-marker
        fallback, so both stamp the session's story key and — for bundle dev
        sessions, where the orchestrator exports the bundle's owned dw ids (the
        generic skill never authors them) — the dw_ids verify_dev_bundle
        cross-checks."""
        story_key = spec.env.get("BMAD_LOOP_STORY_KEY") or None
        raw_dw_ids = (spec.env.get("BMAD_LOOP_DW_IDS") or "").split(",")
        dw_ids = [tok for tok in (i.strip() for i in raw_dw_ids) if tok]
        return devcontract.synthesize_result(
            spec_path,
            story_key=story_key,
            dw_ids=dw_ids or None,
            park_marker_session_authored=self._park_marker_session_authored(spec_path, spec),
        )

    def _observe_tick(self, handle: SessionHandle, spec: SessionSpec) -> None:
        """Mid-session status-transition observation (#276 M2), called each
        heartbeat tick (~every HEARTBEAT_INTERVAL_S; the first tick fires too).
        Records the FIRST spec frontmatter status this session drives off its
        launch state to a live, non-terminal value (in practice ``in-review``)
        into ``_fm_transition_obs``. That single sighting is what lets
        ``_frontmatter_fallback`` treat a later terminal frontmatter as
        deterministic proof THIS session wrote it (``transition_proven``), so it
        can synthesize on ONE terminal sighting instead of the 2-observation
        fingerprint.

        A pure sampling path, never a verdict path: it needs a launch snapshot to
        observe against, fires at most once per session (task_ids are unique;
        entries are never cleared), and any unreadable/torn read is a skipped
        sample (silent OSError return), never evidence. Blank/torn parses (``s ==
        ""``) and terminal states (``done``/``blocked``) are NOT recorded — a
        terminal frontmatter is the Stop harvest's business, and the launch status
        itself (the ``done`` a review re-opens) is not a transition. A transition
        that flips entirely between two ticks is simply missed, and the fallback
        keeps its conservative 2-observation path.

        Both sides of the comparison read through ``status_of``, so a blank/
        YAML-null ``status:`` normalizes to ``""`` here AND in the snapshot
        ``_reset_spec_for_review`` captures. That pairing is load-bearing and must
        stay symmetric: normalizing only the snapshot leaves a bare-status spec at
        ``snap.fm_status == ""`` while this tick reads the stringified ``"none"``,
        which is neither blank nor terminal nor equal to the snapshot — a fabricated
        transition, hence a false ``transition_proven`` and a premature
        single-sighting frontmatter synthesis. A blank is also not an observed live
        status in its own right: a skill that ERASES a previously-set status
        mid-session records nothing (the ``s != ""`` guard), where the old
        ``"none"`` reading slipped past as if it were a value."""
        task_id = handle.task_id
        snap = spec.spec_snapshot
        if snap is None or task_id in self._fm_transition_obs:
            return
        try:
            s = status_of(read_frontmatter(Path(snap.path)))
        except OSError:
            return
        if s != "" and s not in (devcontract.DONE, devcontract.BLOCKED) and s != snap.fm_status:
            self._fm_transition_obs[task_id] = s
            self._note_lifecycle(
                task_id, "spec-status-transition-observed", spec=snap.path, status=s
            )

    @staticmethod
    def _same_spec(candidate: Path, snap_path: str) -> bool:
        """Whether ``candidate`` and the snapshot's recorded path are the SAME file
        by filesystem identity (#276 M1), not raw string spelling. ``snap_path`` is
        the engine's ``str(task.spec_file)``; a ``..`` segment, a symlinked artifacts
        dir, or a case-variant alias makes an equivalent path compare unequal
        lexically and would silently disable the hash/transition gate. ``resolve()``
        (the repo's identity convention, non-strict) collapses those; an unresolvable
        path degrades to "not the same file" — conservative, the gate stays inert
        rather than ever falsely refusing."""
        try:
            return candidate.resolve() == Path(snap_path).resolve()
        except OSError:
            return False

    def _snapshot_verdict(
        self,
        *,
        same_file: bool,
        snap: SpecSnapshot | None,
        task_id: str,
        digest: str | None,
    ) -> _SnapVerdict:
        """Shared M1/M2 launch-snapshot decision (see ``_SnapVerdict``). Pure logic,
        no I/O: each caller precomputes ``same_file`` (via ``_same_spec``) and, only
        when it holds, ``digest`` (so an unrelated spec is never hashed), preserving
        each path's own read-error semantics. A recorded transition (M2) outranks the
        content hash (M1); the hash gate refuses only when no transition was observed
        and the bytes are still identical to the launch snapshot."""
        if snap is None or not same_file:
            return _SnapVerdict.NEUTRAL
        if task_id in self._fm_transition_obs:
            return _SnapVerdict.PROVEN
        if digest is not None and digest == snap.sha256:
            return _SnapVerdict.REFUSE
        return _SnapVerdict.NEUTRAL

    def _frontmatter_fallback(
        self,
        handle: SessionHandle,
        spec: SessionSpec,
        search_dirs: list[Path],
        *,
        wait: bool,
        dead_window: bool,
        only: Path | None = None,
    ) -> devcontract.SynthResult | None:
        """Missing-marker rescue (#224): synthesize from a spec this session
        finalized to a terminal frontmatter ``status:`` without appending the
        ``## Auto Run Result`` marker the scan keys on. Without this, such a
        spec is invisible to the harvest: every Stop reads ``no-artifact``, the
        stall nudges re-invoke a skill that has already exited its workflow, and
        a finished story rides to timeout — where the engine's review RETRY
        strips the spec and reproduces the omission until the story defers.

        Trust model: a terminal frontmatter under a live window is weaker
        evidence than the marker, so the live path harvests only a fingerprint
        (path, mtime, status) that held stable across ``FM_FALLBACK_MIN_OBS``
        resultless Stops; a dead window (post-kill reconcile) harvests on one
        sighting, liveness having been settled by the kill. A recorded mid-session
        transition (#276 M2) is a third single-sighting route, live or dead: having
        observed this session drive the spec off its launch ``status:`` to a live
        non-terminal state (``in-review``) proves the terminal frontmatter it now
        carries is this session's own write, not a stale prior ``done``, so one
        terminal sighting suffices — the ``transition=`` flag on the synthesized
        crumb marks it. A transition that flips entirely between two ticks is simply
        missed, and this stays on the conservative 2-observation fingerprint.
        Several candidates mean the scan cannot know which spec is this session's —
        refuse to guess. The launch-snapshot hash (#276 M1) and the transition (M2)
        interact via ``_snapshot_verdict``: when the engine threaded a
        ``spec_snapshot`` (review sessions) and the candidate's bytes still hash
        equal to it, synthesis is deterministically REFUSED in every mode, including
        the dead window (the ``unmodified-since-launch`` verdict /
        ``frontmatter-unmodified-refused`` crumb) — the ``done`` spec re-opened for
        review, mtime-bumped but never re-driven. A recorded transition OUTRANKS the
        hash, though: a clean review can round-trip ``done -> in-review -> done`` back
        to the launch bytes while still omitting its marker, and the observed
        ``in-review`` proves it ran — so REFUSE fires only when NO transition was
        seen. Every synthesized result still runs the engine's full
        deterministic verify downstream, the same #61 trust model as the
        post-kill rescue. With this in place a marker-less ``done`` spec
        completes here and never reaches the review-timeout path, so the
        ``review.on_timeout`` salvage (#271) only ever sees the complementary
        case: a review that died with a NON-terminal frontmatter.

        Owns the give-up breadcrumb: exactly one of ``no-artifact``,
        ``ambiguous-frontmatter``, ``unmodified-since-launch``, or
        ``terminal-frontmatter-pending`` per wait=True pass (none on a harvest).
        A plain wait=False read (the crash path) is compare-only — it may harvest
        an already-stable fingerprint but never records observations or
        breadcrumbs; the hash gate is the one wait=False path that leaves a crumb,
        and only under a dead window (``frontmatter-unmodified-refused``).

        On the FIRST ``terminal-frontmatter-pending`` observation (wait=True, one
        candidate, not the hash-gate refusal, transition not yet proven) it also
        fires the #276 M4 contract nudge when ``limits.dev_contract_nudge`` is on:
        one ``CONTRACT_NUDGE_TEXT`` send asking the skill to append the marker it
        owed, then repair at the source rather than only synthesizing here. It is
        bounded by the never-cleared ``_contract_nudge_sent`` set (marked before
        the send, ``MultiplexerError`` swallowed) — exactly once per session,
        touching no stall counters, so an mtime bump that resets ``observations``
        to 1 never re-nudges. A compliant append is harvested by the ordinary
        marker scan on a later Stop, leaving synthesis as the backstop.

        ``only`` (#261) replaces the directory scan with the single spec the
        orchestrator knows this session owed: the candidate set becomes that file
        if it qualifies, else empty, and ``search_dirs`` is unused. Every gate
        below is unchanged — the point is purely that a foreign story's
        marker-less terminal spec, sitting in the same shared artifacts dir, is no
        longer a candidate at all, so the "refuse to guess between several" branch
        becomes unreachable.
        """
        task_id = handle.task_id
        candidates: list[Path] = []
        if only is not None:
            # Authoritative-path mode (#261): the caller knows the ONE spec this
            # session owed, so the candidate set is that file if it qualifies and
            # nothing otherwise. `len(candidates) > 1` is unreachable here — the
            # "refuse to guess" branch exists for the scan, which cannot know which
            # of several specs is this session's; with a known path there is nothing
            # to guess between.
            if devcontract.is_frontmatter_candidate(only, since_ns=handle.launched_ns):
                candidates.append(only)
            where = str(only)
        else:
            for artifacts in search_dirs:
                candidates.extend(
                    devcontract.find_frontmatter_candidates(artifacts, since_ns=handle.launched_ns)
                )
            where = ", ".join(str(d) for d in search_dirs)
        if not candidates:
            # No marker-less terminal spec either (the common resultless Stop —
            # e.g. a review that flipped to `in-review` and is mid-work): clear
            # any stale fingerprint so a later terminal state starts over.
            self._fm_fallback_obs.pop(task_id, None)
            if wait:
                self._note_resultless_stop(
                    task_id,
                    "no-artifact",
                    "no result artifact newer than session launch under: " + where,
                )
            return None
        if len(candidates) > 1:
            if wait:
                self._note_resultless_stop(
                    task_id,
                    "ambiguous-frontmatter",
                    f"{len(candidates)} terminal marker-less candidates: "
                    + ", ".join(str(p) for p in candidates),
                )
            return None
        path = candidates[0]
        snap = spec.spec_snapshot
        same_file = snap is not None and self._same_spec(path, snap.path)
        try:
            mtime_ns = path.stat().st_mtime_ns
            fm_status = status_of(read_frontmatter(path))
            # Content hash only when the candidate IS the snapshotted spec (compared
            # by filesystem identity) — an unrelated marker-less spec under the same
            # artifacts dir shares no launch state, so hashing it is meaningless work.
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if same_file else None
        except OSError:
            # Torn mid-write read: not evidence of anything — same degrade as
            # the read-back doctrine everywhere else on this path.
            if wait:
                self._note_resultless_stop(
                    task_id, "no-artifact", f"unreadable marker-less candidate {path}"
                )
            return None
        # Launch-snapshot verdict (#276 M1/M2), shared with the stories read-back.
        # REFUSE (M1) — bytes byte-identical to the review-launch snapshot with NO
        # transition observed — is the documented dead-window false positive (a
        # `done` spec re-opened for review, mtime-bumped but never re-driven);
        # refuse in EVERY mode, including `dead_window`. A PROVEN transition (M2)
        # outranks it and falls through to synthesis below. No observation is
        # recorded or popped on REFUSE — an unchanged spec is neither progress nor a
        # stall.
        verdict = self._snapshot_verdict(
            same_file=same_file, snap=snap, task_id=task_id, digest=digest
        )
        if verdict is _SnapVerdict.REFUSE:
            assert snap is not None  # REFUSE is returned only for a matched snapshot
            if wait:
                self._note_resultless_stop(
                    task_id,
                    "unmodified-since-launch",
                    f"{path} byte-identical to review-launch snapshot "
                    f"(snapshot mtime_ns={snap.mtime_ns}, candidate mtime_ns={mtime_ns}); "
                    "refusing frontmatter synthesis",
                )
            elif dead_window:
                self._note_lifecycle(
                    task_id,
                    "frontmatter-unmodified-refused",
                    spec=str(path),
                    status=fm_status,
                    dead_window=True,
                )
            return None
        fingerprint = (str(path), mtime_ns, fm_status)
        prev = self._fm_fallback_obs.get(task_id)
        stable = prev is not None and prev[:3] == fingerprint
        observations = (prev[3] + 1) if (stable and prev is not None) else 1
        # A recorded mid-session transition (#276 M2) proves the terminal frontmatter
        # is this session's write → single-sighting harvest, like a dead window.
        transition_proven = verdict is _SnapVerdict.PROVEN
        if dead_window or transition_proven or (stable and observations >= FM_FALLBACK_MIN_OBS):
            sr = self._synthesize_from(path, spec)
            if sr.result_json is not None:
                sr.result_json["synthesized_from_frontmatter"] = True
                self._note_lifecycle(
                    task_id,
                    "frontmatter-synthesized",
                    spec=str(path),
                    status=fm_status,
                    dead_window=dead_window,
                    transition=transition_proven,
                )
            return sr
        if wait:
            self._fm_fallback_obs[task_id] = (*fingerprint, observations)
            self._note_resultless_stop(
                task_id,
                "terminal-frontmatter-pending",
                f"{path} frontmatter status={fm_status!r} with no '## Auto Run Result'"
                f" marker; observation {observations}/{FM_FALLBACK_MIN_OBS} before synthesis",
            )
            # Contract nudge (#276 M4): at the FIRST pending observation, ask the
            # skill to append the `## Auto Run Result` section it owed so the
            # omission is repaired at the source (a compliant append is then
            # harvested by the normal marker scan on a later Stop; synthesis stays
            # the backstop). Exactly once per session: the task_id is marked BEFORE
            # the send so a raising transport still satisfies exactly-once, and the
            # never-cleared set — not the mtime-resettable observation counter — is
            # the budget, so the #149 refill hazard cannot apply. Touches no stall
            # counters.
            if (
                self._contract_nudge_enabled
                and observations == 1
                and task_id not in self._contract_nudge_sent
            ):
                self._contract_nudge_sent.add(task_id)
                self._note_lifecycle(
                    task_id, "contract-nudge-sent", spec=str(path), status=fm_status
                )
                try:
                    self.send_text(
                        handle,
                        CONTRACT_NUDGE_TEXT.format(spec_path=path, status=fm_status),
                    )
                except MultiplexerError:
                    pass
        return None

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
        successful terminal (marked ``plan_halt``) rather than died-mid-flight.

        A review session also carries a launch ``spec_snapshot``, so before
        synthesizing this applies the shared ``_snapshot_verdict`` gate (#276 M1/M2):
        an unmodified-since-launch ``done`` spec with no observed transition is
        REFUSED (the ``unmodified-since-launch`` verdict), closing the same
        false-positive completion the mtime-scan fallback closes — a review that only
        bumped the mtime of the stripped launch spec no longer reads as done. A dev
        leg carries no snapshot → the gate is inert (mtime-floor accept). Identity is
        filesystem-based (``_same_spec``): under worktree isolation, if ``base``
        resolves into the worktree but the snapshot path is the main checkout the two
        differ and the gate stays inert — conservative (no false accept, just no
        extra protection)."""
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
                    # Launch-snapshot gate (#276 M1/M2), shared with the mtime-scan
                    # fallback so the stories read-back can't false-complete on an
                    # unmodified `done` spec. Only bites on a review session (the
                    # engine threads `spec_snapshot` there); a dev leg leaves it None
                    # → NEUTRAL → the mtime-floor accept below.
                    snap = spec.spec_snapshot
                    same_file = snap is not None and self._same_spec(state.path, snap.path)
                    digest = None
                    if same_file:
                        try:
                            digest = hashlib.sha256(state.path.read_bytes()).hexdigest()
                        except OSError:
                            digest = None  # torn read → NEUTRAL; synthesize keeps its degrade
                    snap_verdict = self._snapshot_verdict(
                        same_file=same_file, snap=snap, task_id=handle.task_id, digest=digest
                    )
                    if snap_verdict is _SnapVerdict.REFUSE:
                        assert snap is not None  # REFUSE implies a matched snapshot
                        # Byte-identical to the review-launch snapshot with no
                        # transition observed — the same dead-window false positive
                        # the scan path refuses. Fall through to keep polling the
                        # grace (a real mid-grace write flips the verdict), then
                        # breadcrumb + None on the deadline, like `stale-mtime`.
                        verdict = "unmodified-since-launch"
                        detail = (
                            f"{state.path} byte-identical to review-launch snapshot "
                            f"(snapshot mtime_ns={snap.mtime_ns}); refusing stories synthesis"
                        )
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
                        detail = (
                            f"{state.path} has no terminal status (frontmatter {state.status!r})"
                        )
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
        the same way.

        ``aborted`` joins it too (#319): an operator's hard stop kills the
        window mid-wait, so a Stop event that had already landed — or was one
        tick away — is never read, leaving exactly the same evidence problem.
        The same trust model settles it: a provably dead window plus a
        self-consistent *successful* terminal plus proof-of-work means the
        session did finish, and discarding that work would misreport what
        happened rather than be cautious about it. The upgrade to
        ``completed`` does NOT resume the run — the engine re-reads the
        hard-stop file after saving the rescued session and stops there, so a
        rescue records the finished work and still honors the stop."""
        if (
            result.status not in ("stalled", "timeout", "over_budget", "aborted")
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
            # dead_window: the probe above settled liveness, so the missing-
            # marker fallback (#224) may synthesize from a terminal frontmatter
            # on a single sighting — the gates below still refuse anything but
            # a self-consistent, escalation-free successful terminal.
            sr = self._synth_result(handle, spec, wait=False, dead_window=True)
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
        # Proof-of-work gate (#261), the same one the crash path applies in `_final`:
        # this rescue exists for a session that finished but lost its Stop, not for
        # one that never ran. A session that ended no turn and whose pane log never
        # grew produced nothing, so a qualifying artifact is not its output — keep
        # the stall/timeout verdict. This is the call path the incident's second
        # occurrence took.
        if not self._produced_work(handle, result.stop_seen):
            self._note_lifecycle(
                handle.task_id,
                "readback-refused-no-proof-of-work",
                fallback=result.status,
                spec=str(rj.get("spec_file", "")),
                status=str(rj.get("status", "")),
                dead_window=True,
            )
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
            stop_seen=result.stop_seen,
        )


class GenericDevAdapter(_DevSynthesisMixin, GenericAdapter):
    """Dev adapter for Alex Verhovsky's generic ``bmad-build-auto`` skill.

    That skill writes NO ``result.json`` — its outcome lives in the spec it
    leaves on disk (frontmatter ``status:`` plus an appended ``## Auto Run
    Result``, or, when it never created a spec, a ``bmad-build-auto-result-*.md``
    — ``bmad-dev-auto-result-*.md`` pre-rename — fallback). On the Stop event we
    locate that artifact and synthesize the legacy result dict from it via
    :mod:`devcontract`, so verify/escalation and the rest of the pipeline
    consume it unchanged. Selected by
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
