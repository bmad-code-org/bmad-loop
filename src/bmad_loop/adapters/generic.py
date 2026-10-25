"""Generic coding-CLI driver: interactive sessions in tmux windows, observed via hooks.

Each pipeline step gets a fresh tmux window running the full interactive CLI
with the skill invocation as the initial prompt. Completion is detected
exclusively through hook-written event files (Stop/SessionEnd) plus the
presence of the skill-written result.json — the pane log's *contents* never
drive the wait loop (only tee'd for human debugging), though its *growth*
(mtime/size, never the bytes — see ``_log_activity_key``) is read as a liveness
signal to re-arm the dev-stall grace window and, on a separate timeline, as the
#727 no-work verdict (``SessionResult.produced_work`` — see ``_work_verdict``);
the live transcript's growth — and its profile's subagent transcript tree's
(``subagent_transcript_dir``) — is likewise stat'ed, never parsed, for the #680
idle notice (``_sample_transcript_idle``). The one exception is post-mortem:
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
import dataclasses
import enum
import hashlib
import json
import os
import shlex
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import regex

from .. import devcontract, gates, runs, tokens
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR, TASK_CYCLE_ARTIFACTS
from ..model import TokenUsage
from ..policy import Policy
from ..process_host import ProcessHostError, get_process_host
from ..signals import REBIND_SOURCES, SessionAttribution, SignalWatcher
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
from .env_fault import EnvFaultMixin, _excerpt
from .multiplexer import MultiplexerError, TerminalMultiplexer, get_multiplexer
from .profile import PARKED_EVENTS, CLIProfile

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


@dataclass
class _IdleTracker:
    """Per-session state of the #680 transcript idle detector, owned by one
    `wait_for_completion` call and advanced by `_sample_transcript_idle` on the
    heartbeat cadence.

    `last_key` is the transcript's (mtime_ns, size) as of the last successful
    sample; None means no sample yet, and it is the ONLY "have we sampled"
    sentinel — the two `last_change_*` clocks read as 0.0 until then and are never
    consulted before it is set. `idle_s` is the latest measured age (what
    `heartbeat.json` reports), None until the first sample. `open_since` is the
    wall time the currently open idle stretch began — the `since_ts` its
    `session-idle` carried — or None between stretches: the latch that makes the
    pair one-per-stretch.

    `last_key` stays the PARENT transcript's key alone even when the profile
    declares a subagent tree: the wait loop's `sample_transcript` reads
    `last_key[1]` as the parent's byte size — the #727 growth test and the scan
    offset for `_transcript_has_assistant_activity` — so a folded key would credit
    subagent bytes as parent growth and scan from a wrong offset. The tree gets
    its own `sub_key`, OR-ed only into the idle clock's change test.

    `tool_scan_failures` is the streak of `session-idle` `in_tool` reads that
    raised; it never moves the clock, only the `tool-scan-*` crumbs."""

    # The transcript path the samples below belong to. A later hook event that
    # names a DIFFERENT path rebaselines the tracker (`_sample_transcript_idle`):
    # a key measured on one file says nothing about another.
    path: str | None = None
    last_key: tuple[int, int] | None = None
    last_change_mono: float = 0.0
    last_change_wall: float = 0.0
    idle_s: float | None = None
    open_since: float | None = None
    # A close event whose best-effort journal write failed. Retry it on later
    # samples so the TUI does not keep showing an idle session as stuck.
    pending_active_s: float | None = None
    # A sample ran while the named transcript could not be stat'ed (not yet
    # created). The next successful sample is a change from absence, not a
    # pre-existing file's baseline.
    seen_absent: bool = False
    # The subagent tree's (count, total_bytes, newest_mtime_ns) at the last clean
    # walk; None = not walked on this path yet, or the profile declares no tree.
    # Reset on a re-point; kept through a failed walk, so writes made during a
    # fault count as movement once the walk recovers.
    sub_key: tuple[int, int, int] | None = None
    # The running streak of subagent-tree walks that raised: crumbed at its
    # transitions (`subagent-scan-failed` / `subagent-scan-recovered`). NOT reset
    # on a re-point, so a streak open across one still gets its recovery crumb.
    sub_scan_failures: int = 0
    # The running streak of `in_tool` reads that raised (`_idle_tool_label`):
    # crumbed at its transitions (`tool-scan-failed` / `tool-scan-recovered`).
    # NOT reset on a re-point, for the same reason as `sub_scan_failures`.
    tool_scan_failures: int = 0


@dataclass
class _QuotaWait:
    """Per-session state of the usage-limit quota wait (DW-396), owned by one
    `wait_for_completion` call.

    `waiting` is the open-wait flag. While it is set the wait loop re-bases both
    session deadlines to `frozen_remaining` on every tick, so the timeout stands
    still. `started_mono` / `started_wall` are the open wait's start on each
    clock. An open wait's length is the LONGER of the two elapsed spans, so a
    host suspend (monotonic frozen, wall running) cannot stretch the cap.
    `closed_s` sums the waits that already ended; `evidence` is the pane line
    that admitted the open one."""

    waiting: bool = False
    started_mono: float = 0.0
    started_wall: float = 0.0
    frozen_remaining: float = 0.0
    closed_s: float = 0.0
    evidence: str | None = None

    def open_s(self) -> float:
        if not self.waiting:
            return 0.0
        return max(time.monotonic() - self.started_mono, time.time() - self.started_wall, 0.0)

    def total_s(self) -> float:
        return self.closed_s + self.open_s()


# min spacing between heartbeat.json overwrites in wait_for_completion; the
# heartbeat's staleness is what makes a frozen orchestrator (#157) diagnosable.
HEARTBEAT_INTERVAL_S = 30.0
# Usage-limit quota wait (DW-396): how long the live transcript must have held
# still (three heartbeats) before a visible `usage_wait_patterns` line may freeze
# the session timeout. A CLI still writing its transcript is working, not
# waiting out a reset.
QUOTA_WAIT_IDLE_S = 90.0
# The floor on the timeout a quota wait freezes, and so on what the session
# resumes with once the CLI continues: a wait admitted with seconds to spare
# must leave the auto-continued turn room to finish and Stop.
QUOTA_WAIT_MIN_REMAINING_S = 120.0
# The parked-latch kinds that block a quota wait (DW-396): a CLI asking a human
# for permission or a quota decision is not going to continue by itself. An
# `IdlePrompt` is deliberately absent — claude sends `idle_prompt` ~60 s after
# every finished turn, including the one the usage limit ended.
QUOTA_WAIT_BLOCKING_KINDS = frozenset({"QuotaPrompt", "PermissionPrompt"})
# Startup-frame window for the #727 no-work verdict: pane-log growth detected on a
# tick later than this many seconds after the wait loop started counts as work;
# growth inside it is the CLI painting its first frame — a banner, a menu, a
# permission dialog — which a parked session does exactly once and a working one
# streams past for minutes. Seconds, because a launch paint lands in seconds, and
# an order of magnitude under the 600 s default `dev_stall_grace_s`, so a working
# session has the whole grace to prove itself past the window. A CLI slower than
# this to paint at all retries as it does today (its first frame reads as work).
# Not a policy knob: the value separates two regimes an order of magnitude apart,
# so its exact position is not load-bearing.
FIRST_FRAME_S = 30.0
# `Notification` and the parked kinds (DW-348) never complete a session: the wait
# loop only LATCHES them, and the one place the latch acts is the stall-grace
# expiry, where it withholds the wake nudge and ends the session parked (see
# `_parked_hook_evidence`). With claude's `idle_prompt` mapped, that is the usual
# end of a result-less turn: the wake nudge is replaced by a pause.
EVENT_KINDS = {"SessionStart", "Stop", "SessionEnd", "Notification"} | set(PARKED_EVENTS)
# Wall-clock bound on EACH `parked_prompt_patterns` search over a captured pane
# line (DW-350) — the `regex` module's per-search timeout, the env-fault
# classifier's doctrine (`env_fault.ENV_FAULT_MATCH_TIMEOUT_S`). A pattern that
# blows it declines the match, so the nudge goes out exactly as it would have.
PARKED_PROMPT_MATCH_TIMEOUT_S = 2.0
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


# Every task-directory leaf `_ResultFileMixin` writes during a session, beyond the
# cycle artifacts in `journal.TASK_CYCLE_ARTIFACTS` and the prompt. Both adapters
# that inherit the mixin hand this tuple to `validate_adapter_artifact_paths`
# before their first write: a reused task directory carrying a symlink, hardlink,
# FIFO or device under one of these names would otherwise have the heartbeat
# overwrite truncate a linked external file, or a breadcrumb append block on or
# redirect into it. One tuple, so a fourth mixin write cannot reach one adapter's
# validation and miss the other's.
RESULT_FILE_ARTIFACTS: tuple[str, ...] = (
    "heartbeat.json",
    "resultless-stops.jsonl",
    "session-lifecycle.jsonl",
)


def _result_path(tasks_dir: Path, task_id: str) -> Path:
    """Where THIS task's result.json lives under an adapter's ``tasks_dir``."""
    return tasks_dir / task_id / "result.json"


def load_result_document(tasks_dir: Path, task_id: str) -> dict | None:
    """Read one task's skill-written result document exactly as the completion
    read-back does: ``None`` when no regular file is there; ``OSError``,
    ``ValueError`` (unparseable JSON, a non-object top level, an unencodable
    string) or ``RecursionError`` for a present document the read-back refuses.
    `_ResultFileMixin._read_result` folds every raise into ``None``; the sweep
    session-failure diagnostic (#752) keeps "missing" and "malformed" apart."""
    path = _result_path(tasks_dir, task_id)
    # `stat()` + `S_ISREG`, not `is_file()` (the DW-224 shape): 3.14's `is_file()`
    # swallows a metadata fault as False, which would report a refused document
    # as absent; 3.11-3.13 raise. Only absence and a non-directory component are
    # ``None`` on every runtime — any other fault raises as a present refusal.
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(mode):
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"result.json is not a JSON object: {type(data).__name__}")
    # Plugin HookContext makes this same defensive copy before exposing
    # result data, so reject a shape that would recurse there while the
    # artifact is still inside the shared observation boundary.
    copy.deepcopy(data)
    # JSON accepts escaped lone surrogates, but the default ATTENTION
    # sink writes reasons as UTF-8. Validate every parsed string without
    # imposing stricter numeric semantics on completed session results.
    json.dumps(data, ensure_ascii=False).encode("utf-8")
    return data


def _is_regular_file(path: str | None) -> bool:
    """Whether a hook-reported transcript path names a regular file. Any fault
    reads as False: the caller's answer to False is a by-id lookup whose miss is
    crumbed, so a metadata fault cannot hide."""
    if not path:
        return False
    try:
        return stat.S_ISREG(Path(path).stat().st_mode)
    except (OSError, ValueError):
        return False


def _subagent_tree_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    """Every subagent transcript (``*.jsonl``) under ``root``, recursively, with
    its stat — the stat-only walk behind the idle notice's subagent fold.

    Recursive because workflow agents nest under ``workflows/wf_<id>/`` (the
    reason ``tokens.tally`` rglobs the same anchor). Symlinks are neither
    descended nor counted; each file is ``os.stat``'ed by path (a cached
    ``DirEntry.stat()`` can be stale on Windows) and never opened, so a FIFO
    cannot block the wait loop. ``FileNotFoundError``/``NotADirectoryError`` —
    a root or entry that is not there, or not a directory — are skipped, so a
    missing tree is empty; every other ``OSError`` propagates for the caller to
    crumb. Uncapped: a cap would silently truncate the signal."""
    files: list[tuple[Path, os.stat_result]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                try:
                    st = os.stat(entry.path)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                files.append((Path(entry.path), st))
    return files


def _subagent_tree_key(root: Path) -> tuple[int, int, int]:
    """Activity signature of a subagent transcript tree: (count, total_bytes,
    newest_mtime_ns) over `_subagent_tree_files`; (0, 0, 0) when it is absent. An
    aggregate rather than the newest file's key, so an append to a non-newest
    file inside the same mtime tick still moves `total_bytes`."""
    files = _subagent_tree_files(root)
    return (
        len(files),
        sum(st.st_size for _, st in files),
        max((st.st_mtime_ns for _, st in files), default=0),
    )


# The tool names `session-idle`'s `in_tool` may carry verbatim: Claude Code's
# built-ins. A closed slug chosen by code, so the journal never carries
# transcript text or MCP server names — `_idle_tool_slug` maps every other name
# to "mcp" or "other".
_IDLE_TOOL_SLUGS = frozenset(
    {
        "Bash",
        "PowerShell",
        "Agent",
        "Task",
        "Workflow",
        "SendMessage",
        "TaskOutput",
        "TaskStop",
        "Monitor",
        "AskUserQuestion",
        "ExitPlanMode",
        "EnterPlanMode",
        "WebFetch",
        "WebSearch",
        "Read",
        "Write",
        "Edit",
        "NotebookEdit",
        "Grep",
        "Glob",
        "Skill",
        "ToolSearch",
    }
)


def _idle_tool_slug(name: str) -> str:
    """The closed `in_tool` slug for a transcript's tool name: a built-in in
    `_IDLE_TOOL_SLUGS` passes through, an `mcp__<server>__<tool>` name is "mcp"
    (the server name stays out of the journal), anything else is "other"."""
    if name in _IDLE_TOOL_SLUGS:
        return name
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


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
        on-disk artifact.

        A present document the read-back refuses still reads as None — the
        verdict is unchanged — but not silently (DW-451): ``wait=True`` names it
        in the grace-expiry resultless-stop record (see `_await_result`), and this
        one-shot read, reached only from `_final`, leaves a
        ``result-json-refused`` crumb (``error``)."""
        if wait:
            return self._await_result(handle.task_id)
        result, refusal = self._probe_result(handle.task_id)
        if refusal is not None:
            self._note_lifecycle(handle.task_id, "result-json-refused", error=refusal)
        return result

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

    def _work_verdict(self, handle: SessionHandle, stop_seen: bool, activity_seen: bool) -> bool:
        """`SessionResult.produced_work` for a non-completed exit (#727): did this
        session do anything at all before it ended?

        Three ways to answer True, ORed like `_produced_work`'s halves and for the
        same reasons: a `Stop` arrived (a turn ended — the hook half, immune to a
        misbound pane sink); there is no pane log to read (`_log_evidence` is None —
        opencode-http, unit fixtures — and unknown never blocks); or the wait loop
        saw activity (`activity_seen`): the pane log changed on a tick later than
        `FIRST_FRAME_S` after it started and before the first stall wake nudge (the
        timeline half), OR a pre-nudge transcript change carried a model-side
        record, OR pre-nudge usage reported model spend. These last two signals
        survive a misbound pane sink; setup-only and post-nudge writes do not
        supply proof.

        The timeline half is what separates this from `_produced_work`, and why the
        #261 gate is reused for its tristate only, not its verdict: that gate's
        256-byte floor was calibrated for wedged windows that logged 0 and 2 bytes,
        and a permission dialog rendered once is ~2 KB — it clears the floor, so the
        floor alone files a parked CLI as one that worked. The question the operator
        asks is "did the pane ever change after its first frame?", and only the loop
        that watched it tick by tick can answer. Growth after the first stall wake
        nudge attempt is excluded by construction (the caller never flips
        `activity_seen` once a stall nudge was attempted, delivered or not — a
        raising send may still have pasted its text, DW-449): the nudge's `send-keys … Enter`
        confirms a dialog's default and the pane grows with the echo and the exit
        text, which is the loop's own keystrokes, not work — a session that
        genuinely woke proves it with a `Stop`, the doctrine the nudge-budget refill
        already follows. Only `STALL_NUDGE_TEXT` counts as that nudge; the budget
        wrap-up nudge (`BUDGET_NUDGE_TEXT`) and the #276 contract nudge do not
        close the window, and are out of this verdict's scope."""
        if stop_seen or activity_seen:
            return True
        return self._log_evidence(handle) is None

    @staticmethod
    def _transcript_has_assistant_activity(transcript_path: str, since_size: int) -> bool:
        """A model-side JSONL record written after a sampled baseline.

        The idle detector remains stat-only. Reading content here is solely for
        the no-work verdict. A line crossing the old EOF is included so a torn
        line completed by the latest append can still prove work. Gemini's
        `$set.messages` snapshots can replay older messages, so a model message
        with an ID only proves work when it is new or changed. Copilot's metrics
        only prove work when output or reasoning tokens are positive; input
        tokens alone can be a submitted prompt. Codex token counts are cumulative,
        so only an increase in output tokens proves new work.

        A read fault is NOT folded into "no activity" here (DW-455): the
        ``OSError`` propagates, because this staticmethod cannot crumb and the
        caller has already moved the growth baseline past the bytes it could not
        read. The wait loop's ``sample_transcript`` treats it as no evidence —
        the verdict it would have reached — and crumbs the streak.
        """
        seen_messages: dict[str, dict] = {}
        codex_output_seen = 0
        # Hook paths are external observations. A FIFO can be stat'ed but opening
        # it for a JSONL scan would block the deterministic wait loop indefinitely.
        if not Path(transcript_path).is_file():
            return False
        with Path(transcript_path).open("rb") as stream:
            while line := stream.readline():
                after_baseline = stream.tell() > since_size
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                messages = [entry]
                set_patch = entry.get("$set")
                if isinstance(set_patch, dict):
                    snapshot = set_patch.get("messages")
                    if isinstance(snapshot, list):
                        messages.extend(
                            message for message in snapshot if isinstance(message, dict)
                        )
                for message in messages:
                    if message.get("type") not in ("assistant", "gemini") and message.get(
                        "role"
                    ) not in ("assistant", "model"):
                        continue
                    message_id = message.get("id")
                    if isinstance(message_id, str):
                        previous = seen_messages.get(message_id)
                        seen_messages[message_id] = message
                        if after_baseline and message != previous:
                            return True
                    elif after_baseline:
                        return True
                payload = entry.get("payload")
                if (
                    after_baseline
                    and isinstance(payload, dict)
                    and payload.get("type") == "agent_message"
                ):
                    return True
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        totals = info.get("total_token_usage")
                        usage = totals if isinstance(totals, dict) else info
                        output = usage.get("output_tokens")
                        if type(output) is int:
                            if after_baseline and output > codex_output_seen:
                                return True
                            codex_output_seen = max(codex_output_seen, output)
                if after_baseline:
                    data = entry.get("data")
                    metrics = data.get("modelMetrics") if isinstance(data, dict) else None
                    if isinstance(metrics, dict):
                        for model in metrics.values():
                            usage = model.get("usage") if isinstance(model, dict) else None
                            if isinstance(usage, dict) and any(
                                type(usage.get(key)) is int and usage[key] > 0
                                for key in ("outputTokens", "reasoningTokens")
                            ):
                                return True
        return False

    @staticmethod
    def _open_tool_call(paths: list[Path]) -> str | None:
        """The raw name of the newest unanswered Claude tool call across
        `paths`, or None when every call has its result.

        A `tool_use` content item opens a call under its `id`; a `tool_result`
        whose `tool_use_id` names it closes it in EVERY file, whatever the read
        order: a forked subagent's transcript copies the parent's still-open
        `Agent` call under the same id, and the parent's later result must close
        that copy too. Of the calls still open, the one
        whose entry carries the newest `timestamp` wins, across ALL files
        (ISO-8601 strings compare chronologically; a missing one sorts oldest), so
        a parent's open `Agent` loses to its subagent's later open `Bash`: the
        innermost wait. Content-reading by design, unlike the stat-only idle
        clock — only `_idle_tool_label` calls it, once per opened stretch.
        Tolerant like the usage parsers: a torn, undecodable or non-dict line is
        skipped. A path that is not a regular file is skipped (a FIFO would
        block the wait loop): `is_file` filters it before the open, and the
        non-blocking open is re-checked with `fstat`, so one swapped in between
        is skipped too. An ``OSError`` propagates for the caller to crumb."""
        calls: dict[str, tuple[str, str]] = {}
        closed: set[str] = set()
        for path in paths:
            if not path.is_file():
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                while line := stream.readline():
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    message = entry.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        continue
                    timestamp = entry.get("timestamp")
                    if not isinstance(timestamp, str):
                        timestamp = ""
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_use":
                            call_id = item.get("id")
                            name = item.get("name")
                            if isinstance(call_id, str):
                                calls[call_id] = (
                                    name if isinstance(name, str) else "",
                                    timestamp,
                                )
                        elif item.get("type") == "tool_result":
                            call_id = item.get("tool_use_id")
                            if isinstance(call_id, str):
                                closed.add(call_id)
        open_calls = {call_id: call for call_id, call in calls.items() if call_id not in closed}
        if not open_calls:
            return None
        name, _ = max(open_calls.values(), key=lambda call: call[1])
        return name

    def _session_vanished(self, task_id: str) -> bool:
        """Whether the whole multiplexer session is gone, asked only once a
        crash verdict has already been reached (#489). Base: False — an adapter
        with no session to lose (opencode-http) never vanishes. Overridden by
        `GenericAdapter`.

        Same failure convention as `_window_alive`: `MultiplexerError` is the
        seam's declared "couldn't ask" and the override degrades it to False —
        visibly, with a ``session-probe-failed`` crumb under ``task_id``
        (DW-382), so "not vanished" and "could not ask" stay distinguishable.
        A negative ``has_session`` that the override cannot confirm as a proved-
        gone session degrades the same way (DW-459): True means proved gone.
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
        produced_work: bool = True,
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status. ``accept_result=False``
        (a stall verdict reached under a live window) pins the fallback: an
        artifact that appeared without a Stop or window death is not trusted.
        ``budget_weighted`` (a tripped session-budget guard's sample) rides
        every exit so the engine can journal it whatever the verdict.
        ``stop_seen`` is the proof-of-work hook signal, threaded separately from
        ``session_id``/``transcript`` because those are also set by a mere launch.
        ``produced_work`` is the wait loop's `_work_verdict` (#727), stamped on
        every NON-completed result; a read-back upgrade to ``completed`` resets it
        to True, because the flag is scoped to non-completed exits and a
        ``completed`` session-end must not carry a no-work stamp."""
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
        vanished = status == "crashed" and self._session_vanished(handle.task_id)
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
            produced_work=True if status == "completed" else produced_work,
        )

    def _result_path(self, task_id: str) -> Path:
        return _result_path(self.tasks_dir, task_id)

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

    def _note_nudge_send_failed(self, handle: SessionHandle, nudge: str, e: Exception) -> None:
        """Crumb a nudge whose send raised (DW-449): ``nudge`` names which one
        (``stall``/``budget``/``stop``/``contract``), ``error`` the fault in the
        ``session-probe-failed`` shape. The caller's verdict path is unchanged —
        this only keeps an undelivered nudge from reading as a delivered one."""
        self._note_lifecycle(
            handle.task_id, "nudge-send-failed", nudge=nudge, error=f"{type(e).__name__}: {e}"
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

    def _probe_result(self, task_id: str) -> tuple[dict | None, str | None]:
        """`load_result_document` with its refusal kept apart from absence: the
        document (or None when absent) and None, or None and the refusal as
        ``"<ExcType>: <message>"`` for a present document the read-back refuses."""
        try:
            return load_result_document(self.tasks_dir, task_id), None
        except (OSError, ValueError, RecursionError) as e:
            return None, f"{type(e).__name__}: {e}"

    def _read_result(self, task_id: str) -> dict | None:
        """The result dict, or None for absent AND refused alike. The silent
        primitive: callers that must tell the two apart use `_probe_result`."""
        return self._probe_result(task_id)[0]

    def _await_result(self, task_id: str, grace_s: float = RESULT_GRACE_S) -> dict | None:
        deadline = time.monotonic() + grace_s
        while True:
            result, refusal = self._probe_result(task_id)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                # One record per give-up, never one per poll. It reflects the
                # final poll (DW-451): a present-but-refused document is
                # `malformed-result-json` with the refusal, so it no longer reads
                # as a document that was never written.
                path = self._result_path(task_id)
                if refusal is not None:
                    self._note_resultless_stop(
                        task_id, "malformed-result-json", f"{path}: {refusal}"
                    )
                else:
                    self._note_resultless_stop(task_id, "no-result-json", f"no readable {path}")
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
        # Threshold for the #680 transcript-idle notice, in seconds: the policy's
        # `dev_stall_grace_s` read directly, NOT `_stall_grace_s`, because the base
        # adapter leaves that at 0 (no stall detection for triage / plugin-workflow
        # / non-synthesizing sessions) while the idle notice is observation only
        # and belongs to every pane-driven session the same. Same knob, no new
        # policy field; 0 disables the notice along with the stall timer.
        self._idle_threshold_s = float(policy.limits.dev_stall_grace_s)
        # Grace for the one-shot `session-start-missing` journal notice
        # (`_note_session_start_missing`). Observation only, like the idle notice,
        # so every adapter reads it, the plain one included; 0 disables it.
        self._session_start_grace_s = float(policy.limits.session_start_grace_s)
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
        # weakness for a session's tag and cwd but must not for the completion
        # channel).
        # Defaulting to the legacy dir keeps direct construction (tests, any
        # caller outside `runsetup.make_adapters`) working unchanged; the
        # bootstrap always passes one, pinned by a test.
        self.watcher = SignalWatcher(events_dir or run_dir / "events", run_dir / "events")
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / LOGS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        # task_id -> launched_ns of the session whose `log-evidence-failed` crumb
        # is already written (DW-450): one exit can consult `_log_evidence` up to
        # three times (the #727 verdict, `_final`'s #261 gate, the post-kill
        # rescue), and a fault must read as one record, not three. Keyed by
        # launch, not task id alone, because a re-armed run reuses task ids.
        self._log_evidence_faulted: dict[str, int] = {}

    # --------------------------------------------------------- multiplexer

    def _ensure_session(self) -> None:
        # run_dir = <project>/.bmad-loop/runs/<id>.
        project = self.run_dir.parents[2]
        if self.mux.has_session(self.session_name):
            # Reusing it is right for this run's own session (a resume), and
            # wrong for a same-named one of another project's in a shared
            # registry (#729): every window would open inside it, under its tag.
            refusal = runs.foreign_session_refusal(self.session_name, self.mux, project)
            if refusal is not None:
                raise MultiplexerError(
                    f"refusing to launch into the existing session {self.session_name}: "
                    f"{refusal} — stop or rename that run, or turn off "
                    "[mux] honor_ambient_psmux_data_dir"
                )
        else:
            # Minted at the project root, never at the first session's cwd: the
            # session's server and initial window hold it as their cwd for the
            # whole run, and on Windows that blocks removing a unit's worktree
            # (#902). Every window gets its own cwd through `new_window`.
            self.mux.new_session(self.session_name, project, PANE_COLUMNS, PANE_LINES)
            # Tag the session with its project so a cleanup in another project
            # never prunes this run.
            try:
                self.mux.set_session_option(
                    self.session_name, runs.PROJECT_OPTION, runs.project_tag(project)
                )
            except Exception as tag_fault:
                # Left standing, the untagged session would block this run id
                # for good in a shared registry (#729): the ownership gate reads
                # it as foreign, and the kill and cleanup paths refuse it. It is
                # the one this call just minted, so tear it down by that exact
                # name — straight through the backend, since the gate would
                # refuse an untagged session by construction — and re-raise.
                # The backend kill is best-effort and silent by contract, so
                # whether it landed is read back rather than assumed.
                listing_faults: list[str] = []
                try:
                    self.mux.kill_session(self.session_name)
                    key = self.mux.session_name_key(self.session_name)
                    survived = any(
                        self.mux.session_name_key(n) == key
                        for n in self.mux.list_sessions_reporting(on_fault=listing_faults.append)
                    )
                except Exception as kill_fault:
                    listing_faults.append(str(kill_fault))
                    survived = True
                if survived or listing_faults:
                    why = (
                        f"could not be confirmed gone ({listing_faults[0]})"
                        if listing_faults
                        else "is still there after tearing it down"
                    )
                    raise MultiplexerError(
                        f"tagging the new session {self.session_name} failed ({tag_fault}), "
                        f"and the untagged session {why} — remove it by hand before "
                        "resuming this run"
                    ) from tag_fault
                raise

    def interactive_argv(self, spec: SessionSpec) -> list[str]:
        extra = self.extra_args
        if extra is None:
            extra = self.profile.bypass_args
        binary = self.binary
        if self.profile.hooks.dialect == "codex-hooks-json":
            from ..codex_trust import resolved_codex_binary

            binary = resolved_codex_binary(binary, self.profile.env) or binary
        argv = [
            binary,
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

    def build_command(self, spec: SessionSpec, *, session_id: str | None = None) -> str:
        """The launched window's command line. ``session_id`` (DW-505) is the id
        ``start_session`` minted for a profile declaring ``session_id_flag``; it is
        appended at the END, like ``model_flag``, so a ``launch_args`` ending in a
        value-taking option (gemini's ``-i``) cannot swallow it. Deliberately not
        in ``interactive_argv``: resolve's human-present sessions do no attribution."""
        argv = self.interactive_argv(spec)
        if session_id and self.profile.session_id_flag:
            argv += [self.profile.session_id_flag, session_id]
        return " ".join(shlex.quote(a) for a in argv)

    # --------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = validated_task_directory(self.tasks_dir, spec.task_id)
        validate_adapter_artifact_paths(
            task_dir,
            tuple(task_dir / name for name in RESULT_FILE_ARTIFACTS),
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

        self._ensure_session()
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
        # DW-505: a profile that can take a caller-chosen session id gets a fresh
        # one per launch, so wait_for_completion knows the parent before any hook
        # event arrives (signals.SessionAttribution pinning).
        pinned_session_id = str(uuid.uuid4()) if self.profile.session_id_flag else None
        window_id = self.mux.new_window(
            self.session_name,
            spec.task_id[-40:],
            spec.cwd,
            # Same merge as interactive_env, same pin chokepoint: the profile's
            # [env] table must not move the window off this process's state root.
            runs.pin_state_root({**self.profile.env, **spec.env}),
            self.build_command(spec, session_id=pinned_session_id),
        )
        # Pin the launched pane now, before anything else targets it: a split an
        # operator or a hook makes becomes the ACTIVE pane, and a window target
        # would type every nudge into it (#948) and tee its output instead. A
        # failed pin degrades to the window target, crumbed; a window already
        # dead fails here too, and the wait loop reports that crash. A
        # duck-typed backend predating the method keeps the window target, as
        # for capture_pane.
        pin = getattr(self.mux, "pane_target", None)
        try:
            pane_target = pin(window_id) if pin is not None else None
        except MultiplexerError as e:
            pane_target = None
            self._note_lifecycle(
                spec.task_id, "pane-pin-failed", window=window_id, error=f"{type(e).__name__}: {e}"
            )
        # pipe_pane tolerates the window having already died (a CLI that crashes on
        # launch can take it down before the tee attaches); the dead window is then
        # reported as a crash in wait_for_completion.
        self.mux.pipe_pane(pane_target or window_id, log_file)
        return SessionHandle(
            task_id=spec.task_id,
            native_id=window_id,
            launched_ns=launched_ns,
            pinned_session_id=pinned_session_id,
            pane_target=pane_target,
        )

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        """`_wait_loop`, with the quota wait's total carried onto whatever it
        returns (DW-396). A wait still open at the exit (a Stop with a result,
        window death, a hard stop, the cap) is closed here, so every
        `quota-wait-started` gets its `quota-wait-ended`."""
        quota = _QuotaWait()
        result = self._wait_loop(handle, spec, quota)
        if quota.waiting:
            self._end_quota_wait(handle, quota, "session-exit")
        if quota.closed_s > 0:
            result = dataclasses.replace(result, quota_wait_s=round(quota.closed_s, 3))
        return result

    def _wait_loop(
        self, handle: SessionHandle, spec: SessionSpec, quota: _QuotaWait
    ) -> SessionResult:
        deadline = time.monotonic() + spec.timeout_s
        # Wall-clock co-bound (#157): a host suspend freezes time.monotonic(),
        # silently extending the monotonic deadline by the nap's length. The
        # wall clock keeps counting through a suspend, so it may EXPIRE the
        # deadline — never extend it; all sub-waits below stay monotonic (a
        # wall clock stepped backward must not stretch the session). The one
        # sanctioned exception is the usage-limit quota wait (DW-396), which
        # re-bases BOTH deadlines on every tick; see the freeze below.
        wall_deadline = time.time() + spec.timeout_s
        session_id: str | None = None
        # Pinned to the launch-time id when the profile declared one (DW-505);
        # never seeds `session_id` below — that still comes from the events.
        attribution = SessionAttribution(pinned_id=handle.pinned_session_id)
        # events dropped as a nested CLI's (heartbeat + timeout-fired carry the
        # count); the crumb fires once per foreign id, not once per event — and
        # once for all id-less drops, under the None key (DW-507).
        foreign_hook_events = 0
        crumbed_foreign: set[str | None] = set()
        # DW-507: the one-shot crumb for a lineage the first SessionStart could
        # not calibrate ("miscalibrated"/"unavailable"), so the degrade to the
        # #767 rules alone is visible in session-lifecycle.jsonl.
        lineage_crumbed = False
        # DW-509: whether the CLI honoured the launch-time pin is settled by the
        # first identified non-rebind SessionStart that is not a rejected
        # "mismatch"-tagged (nested) one; unpinned, there is nothing to
        # check. A mismatch leaves the launched session foreign to attribution
        # (its Stop is dropped), so it is crumbed once instead of staying silent.
        pin_checked = handle.pinned_session_id is None
        transcript_path: str | None = None
        # A codex-rollout transcript can be found by session id when the hook's
        # path is missing or names no file (a Windows relay decoding stdin with
        # the locale code page garbles it). `resolved` is that path once found:
        # it sticks against a later unusable hook path. `hook_transcript_path` is
        # the last raw value a hook sent, for the crumbs.
        resolvable = self.profile.usage_parser == "codex-rollout"
        resolved: str | None = None
        hook_transcript_path: str | None = None
        unresolved_crumbed = False
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
        # monotonic total of stall nudges DELIVERED this session — never
        # restored, unlike stall_nudges_left. A send that raised is counted
        # apart in stall_nudges_failed (DW-449), so heartbeat.json never reports
        # an undelivered nudge as sent. Both counters feed the ATTEMPT total the
        # cap check and the #727 pre-nudge activity window read: a raise may
        # follow a delivered paste (`send_text` pastes and presses Enter in
        # separate calls), so a failed attempt closes the window exactly as a
        # delivered one does — its echo must never be credited as work. When
        # spec.stall_nudges_cap is set (the engine sets it for every session it
        # drives), a session that keeps ending its turn without a result cannot
        # ride the fresh-Stop refill forever: after cap total ATTEMPTS it is
        # declared stalled, so a transport that always fails still hits the cap.
        # cap=None (raw constructor default) skips the check.
        stall_nudges_sent = 0
        stall_nudges_failed = 0
        # latched on the first accepted `Stop`: the hook half of the #261 proof-of-work
        # gate. Tracked apart from session_id/transcript_path — those are populated by
        # SessionStart and SessionEnd too, which a CLI that launched and wedged emits
        # without doing any work. Rides out on every exit (see SessionResult.stop_seen)
        # so `_post_kill_reconcile` reads the same signal after run() kills the window.
        stop_seen = False
        # Parked-session latch (DW-348): the evidence string of the latest hook
        # event saying the CLI is waiting on a human (a permission/idle/quota
        # prompt), or None. Set by a parked kind — directly, or a `Notification`
        # whose subtype the profile maps — and cleared by a `Stop`, since a turn
        # that ended proves the prompt was answered. It completes nothing and
        # re-arms nothing: it is consulted only where the grace expiry would type
        # the stall wake nudge, which it withholds (the nudge's Enter can answer
        # the prompt, #727), ending the session `stalled` + `parked`.
        parked_evidence: str | None = None
        # The canonical parked kind behind `parked_evidence` (`_parked_hook_kind`),
        # latched and cleared with it: a QuotaPrompt/PermissionPrompt latch blocks
        # or ends a usage-limit quota wait, an IdlePrompt one never does (DW-396).
        parked_kind: str | None = None
        # Whether this session can enter a usage-limit quota wait at all
        # (DW-396): a cap and a profile that names the wait line. Off, the
        # heartbeat stays byte-identical and nothing below is consulted.
        quota_enabled = spec.quota_wait_max_s > 0 and bool(self._usage_wait_patterns)
        # the running streak of no-event ticks whose liveness probe raised a
        # transport error (e.g. a 30s tmux hang); reset by the next clean probe.
        # It deliberately does NOT escalate to "crashed" — a transient transport
        # hiccup is not proof of death; spec.timeout_s already bounds a
        # persistent failure to a timeout. Surfaced (DW-447): heartbeat.json
        # carries the live streak, `timeout-fired` the final one, and the streak's
        # transitions are crumbed — `liveness-probe-failed` at its first failure,
        # `liveness-probe-recovered` (with its length) at the first clean probe —
        # never once per failed tick, so a persistent hang cannot flood the file.
        probe_failures = 0
        # the running streak of budget usage samples whose transcript read raised
        # (DW-452) — the probe_failures model for the usage read: the guard skips
        # the tick, heartbeat.json carries the count (as of the previous sample —
        # sampling follows the heartbeat write), and only the streak's transitions
        # are crumbed (`usage-sample-failed` / `usage-sample-recovered`).
        usage_failures = 0
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
        # No-work verdict (#727), the timeline half of `_work_verdict`. `frame_key`
        # is the pane log's (mtime_ns, size) as of the last tick, sampled once at
        # the top of EVERY tick — a sibling of `last_activity`, never the same
        # variable: that one drives the stall re-arm and is re-baselined on Stop
        # and nudge, which is exactly the accounting this must not share. It flips
        # `activity_seen` when the key changes on a tick later than FIRST_FRAME_S
        # after the loop started and before the first stall wake nudge attempt
        # (delivered or not, DW-449); growth after it is the loop's own
        # keystrokes echoing (see
        # `_work_verdict`). Latched: once seen, the session worked.
        loop_started = time.monotonic()
        frame_key = self._log_activity_key(handle.task_id)
        activity_seen = False

        def sample_frame() -> None:
            # One pane-frame sample: flip `activity_seen` on growth that lands later
            # than FIRST_FRAME_S after the loop started and before the first stall
            # wake nudge. Called at the top of every tick and again by
            # `produced_work()` right before each exit verdict, because output that
            # arrives during `watcher.wait_for` and is followed by window death or a
            # `SessionEnd` in the same iteration would otherwise be judged on the
            # previous tick's key. The post-nudge exclusion holds on the re-sample
            # too: the attempt total is already > 0 on every tick after the nudge
            # attempt, delivered or not (DW-449).
            nonlocal frame_key, activity_seen
            tick_key = self._log_activity_key(handle.task_id)
            if tick_key is not None and tick_key != frame_key:
                if (
                    not activity_seen
                    and stall_nudges_sent + stall_nudges_failed == 0
                    and time.monotonic() - loop_started > FIRST_FRAME_S
                ):
                    activity_seen = True
                frame_key = tick_key

        # Idle detection (#680): the live transcript's (mtime_ns, size) — plus the
        # profile's subagent transcript tree, whose writes also count as activity
        # — sampled on the heartbeat cadence from the first tick that knows
        # `transcript_path`; see `_sample_transcript_idle`. Observes only: nothing here nudges,
        # stalls or kills (#680 item 2 stays open), and `stall_deadline` is
        # neither consulted nor touched.
        idle = _IdleTracker()
        # An admitted SessionStart arrived, and the one-shot `session-start-missing`
        # notice was journaled; see `_note_session_start_missing`. Observes only.
        saw_session_start = False
        session_start_noted = False

        # A transcript can grow for an initial user prompt or the loop's own
        # wake nudge, neither of which proves model work. Latch model-side
        # evidence only while the pre-nudge window remains open.
        transcript_work_seen = False
        usage_seen = False
        # The running streak of transcript activity scans that raised (DW-455).
        # A raising scan is no evidence — what the old in-scan fold returned —
        # but the baseline has already moved past the bytes it could not read,
        # so the streak is crumbed at its transitions, as probe_failures is:
        # `transcript-scan-failed` at failure 1, `transcript-scan-recovered` at
        # the next clean scan.
        scan_failures = 0

        def sample_transcript(path: str, now: float) -> None:
            nonlocal transcript_work_seen, scan_failures
            same_path = idle.path == path
            prior_key = idle.last_key if same_path else None
            was_absent = idle.seen_absent if same_path else False
            self._sample_transcript_idle(handle.task_id, path, idle, now)
            current_key = idle.last_key
            # A same-size rewrite or truncation does not append a new record.
            # Scanning from byte zero in that case would credit an old assistant
            # record as fresh work merely because mtime changed.
            grew = current_key is not None and (
                (prior_key is not None and current_key[1] > prior_key[1])
                or (prior_key is None and was_absent and current_key[1] > 0)
            )
            if grew and stall_nudges_sent + stall_nudges_failed == 0 and not transcript_work_seen:
                start = prior_key[1] if prior_key is not None else 0
                try:
                    transcript_work_seen = self._transcript_has_assistant_activity(path, start)
                except OSError as e:
                    scan_failures += 1
                    if scan_failures == 1:
                        self._note_lifecycle(
                            handle.task_id,
                            "transcript-scan-failed",
                            error=f"{type(e).__name__}: {e}",
                        )
                    return
                if scan_failures:
                    self._note_lifecycle(
                        handle.task_id, "transcript-scan-recovered", failures=scan_failures
                    )
                scan_failures = 0

        def try_resolve() -> None:
            """Resolve the transcript by session id when no readable one is known.
            One lookup per call — each hook event, each heartbeat, and before
            each exit that no hook event just preceded; a hit becomes the loop's transcript (so every
            SessionResult and the budget guard see it) and is crumbed once — the
            ``resolved is None`` gate makes the hit itself one-shot."""
            nonlocal transcript_path, resolved
            if not resolvable or not session_id or resolved is not None:
                return
            if _is_regular_file(transcript_path):
                return
            # The pane's env as launch() builds it, over this process's own: the
            # CLI reads CODEX_HOME from there. Known limit: a tmux pane inherits
            # the tmux SERVER's env, so a server started under another CODEX_HOME
            # is missed — and the miss is crumbed at Stop.
            hit = tokens.resolve_transcript(
                self.profile.usage_parser,
                session_id,
                {**os.environ, **self.profile.env, **spec.env},
            )
            if hit is None:
                return
            sample_transcript(str(hit), time.monotonic())
            transcript_path = resolved = str(hit)
            self._note_lifecycle(
                handle.task_id,
                "transcript-resolved",
                session_id=session_id,
                hook_transcript_path=hook_transcript_path,
                path=resolved,
            )

        def produced_work() -> bool:
            # Read at call time, after a final frame sample, so every exit below
            # reports the loop's final view of the pane rather than the last tick's.
            sample_frame()
            if transcript_path:
                # Same for the transcript: a write inside the final heartbeat
                # interval has not been sampled yet. A full sample, not a bare
                # compare, so an idle stretch that ended in that interval is closed
                # with its `session-active` before `session-end` lands, and the
                # #727 transcript evidence check sees the write.
                sample_transcript(transcript_path, time.monotonic())
            return self._work_verdict(
                handle, stop_seen, activity_seen or transcript_work_seen or usage_seen
            )

        def stall_exit(parked_now: str | None, usage_now: str | None) -> SessionResult:
            """The non-completion exit of a stalled session (DW-348, #610):
            resolve, then re-probe liveness. A now-dead window is ``crashed``
            (never parked or usage-limited: nothing waits on a human once the CLI
            is gone). Otherwise ``stalled`` with ``accept_result=False``: an
            artifact on disk cannot upgrade the stall to completed (it may be
            stale or mid-write; only a Stop or window death vouches for it), and
            ``parked_now`` / ``usage_now`` label it. Both exits carry the
            rollout."""
            try_resolve()
            if self._probe_liveness(handle, "stall") is False:
                return self._final(
                    handle,
                    spec,
                    "crashed",
                    session_id,
                    transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
                    produced_work=produced_work(),
                )
            stalled = self._final(
                handle,
                spec,
                "stalled",
                session_id,
                transcript_path,
                accept_result=False,
                budget_weighted=budget_weighted,
                stop_seen=stop_seen,
                produced_work=produced_work(),
            )
            if parked_now is not None:
                return dataclasses.replace(
                    stalled, parked=True, parked_evidence=parked_now, usage_limited=usage_now
                )
            return stalled

        while True:
            # Top-of-tick pane-frame sample for the no-work verdict (#727). Before
            # the timeout check so growth on the final tick still counts; before the
            # nudge arm so a tick that both sees growth and sends a nudge scores the
            # growth (the nudge cannot have caused what preceded it).
            sample_frame()
            if quota.waiting:
                # Usage-limit quota wait (DW-396): the CLI sits behind a usage
                # window it will continue from by itself, so the session clock
                # stands still. The cap is checked first, on the longer of the
                # two clocks' spans (`_QuotaWait.open_s`), so a host suspend
                # cannot stretch it; at the cap the session ends stalled + parked
                # with the usage evidence, never completed (only a Stop or window
                # death completes a session).
                waited = quota.total_s()
                if waited >= spec.quota_wait_max_s:
                    self._note_quota_wait_cap(
                        handle, quota.evidence, round(waited, 3), spec.quota_wait_max_s
                    )
                    evidence = quota.evidence
                    self._end_quota_wait(handle, quota, "cap")
                    return stall_exit(evidence, evidence)
                # The freeze runs on EVERY tick, not only on heartbeats: a wait
                # admitted with less than a heartbeat interval left would
                # otherwise time out between samples. This is the one sanctioned
                # exception to #157's "the wall clock may expire the deadline,
                # never extend it": both deadlines are re-based to now plus the
                # remaining time frozen at admission. The wait's own cap above
                # is what bounds it.
                deadline = time.monotonic() + quota.frozen_remaining
                wall_deadline = time.time() + quota.frozen_remaining
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
                    # nonzero = the deadline landed while liveness was unknown
                    probe_failures=probe_failures,
                    # nonzero = a nested CLI's events were dropped (the parent's
                    # own Stop may have been read as foreign)
                    foreign_hook_events=foreign_hook_events,
                )
                # Every exit resolves first (a SessionStart that came before its
                # rollout existed, then no further hook event): the exit's
                # `transcript_path` argument is read before `produced_work()`.
                try_resolve()
                # One look at the visible pane for a usage limit (#610): a
                # session whose clock ran out while it sat behind one is not a
                # failed attempt. A dead window is never usage-limited (the CLI
                # is gone, though a remain-on-exit pane still captures), so a
                # match is confirmed alive first; a probe fault reads "not dead".
                (usage_hit,) = self._pane_evidence(handle, self._usage_pane_patterns)
                usage_now = None
                if usage_hit is not None and self._probe_liveness(handle, "timeout") is not False:
                    usage_now = usage_hit[0]
                return SessionResult(
                    status="timeout",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    timeout_fired_at=time.time(),
                    timeout_expired_clock=expired,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
                    produced_work=produced_work(),
                    usage_limited=usage_now,
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
                try_resolve()
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
                    produced_work=produced_work(),
                )
            now = time.monotonic()
            if last_heartbeat is None or now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                last_heartbeat = now
                # A rollout created after SessionStart, in a turn with no further
                # hook event, is found here rather than at Stop — so the budget
                # guard below is not blind for the whole turn. Bounded by the
                # heartbeat cadence; a no-op once a readable transcript is known.
                try_resolve()
                # Transcript idle sample (#680), ahead of the heartbeat write so
                # the payload carries this tick's age. Inert until a hook event
                # has named the transcript.
                if transcript_path:
                    sample_transcript(transcript_path, now)
                if quota.waiting:
                    # The transcript moved: the CLI continued after the reset.
                    # The timeout resumes from the remaining time frozen at
                    # admission. A pane that merely stopped matching does not
                    # end the wait: the line can scroll away.
                    if idle.idle_s is not None and idle.idle_s < QUOTA_WAIT_IDLE_S:
                        self._end_quota_wait(handle, quota, "transcript-active")
                elif (
                    quota_enabled
                    and transcript_path
                    and idle.idle_s is not None
                    and idle.idle_s >= QUOTA_WAIT_IDLE_S
                    and parked_kind not in QUOTA_WAIT_BLOCKING_KINDS
                    and quota.closed_s < spec.quota_wait_max_s
                ):
                    # Admission (DW-396): an idle transcript, the wait line on
                    # the visible pane, and no unanswered tool call (a long
                    # foreground tool is a working session, whatever the pane
                    # still shows). The pane look comes first: it is the
                    # cheaper read, and a non-match skips the transcript scan.
                    (wait_hit,) = self._pane_evidence(handle, self._usage_wait_patterns)
                    if wait_hit is not None and not self._quota_wait_tool_open(
                        handle, transcript_path
                    ):
                        frozen = max(
                            min(remaining, wall_deadline - time.time()),
                            QUOTA_WAIT_MIN_REMAINING_S,
                        )
                        self._note_quota_wait_started(
                            handle,
                            wait_hit[0],
                            round(frozen, 3),
                            round(quota.closed_s, 3),
                            spec.quota_wait_max_s,
                        )
                        quota.waiting = True
                        quota.started_mono = time.monotonic()
                        quota.started_wall = time.time()
                        quota.frozen_remaining = frozen
                        quota.evidence = wait_hit[0]
                if not saw_session_start and not session_start_noted:
                    session_start_noted = self._note_session_start_missing(
                        handle, now - loop_started
                    )
                self._write_heartbeat(
                    handle.task_id,
                    {
                        "ts": time.time(),
                        "remaining_s": round(remaining, 3),
                        "stall_armed": stall_deadline is not None,
                        "stall_nudges_sent": stall_nudges_sent,
                        # stall nudges whose send raised (DW-449)
                        "stall_nudges_failed": stall_nudges_failed,
                        # seconds since the live transcript (or its profile's
                        # subagent tree) last changed (#680); null until a hook
                        # event has named the transcript.
                        "transcript_idle_s": idle.idle_s,
                        # the running liveness-probe failure streak (DW-447)
                        "probe_failures": probe_failures,
                        # the running budget usage-sample failure streak (DW-452)
                        "usage_sample_failures": usage_failures,
                        # hook events dropped as a nested CLI's (#767)
                        "foreign_hook_events": foreign_hook_events,
                        # the usage-limit quota wait (DW-396): present only when
                        # this session can enter one.
                        **(
                            {
                                "quota_waiting": quota.waiting,
                                "quota_wait_s": round(quota.total_s(), 3),
                            }
                            if quota_enabled
                            else {}
                        ),
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
                    weighted, usage_fault = self._sample_weighted_usage(transcript_path, spec)
                    # A raising read is still "no sample" this tick; its streak is
                    # crumbed at the transitions only (DW-452), like the liveness
                    # probe's — one torn mid-append read is a failed/recovered pair,
                    # a persistent fault one open streak the heartbeat counts.
                    if usage_fault is not None:
                        usage_failures += 1
                        if usage_failures == 1:
                            self._note_lifecycle(
                                handle.task_id, "usage-sample-failed", error=usage_fault
                            )
                    else:
                        if usage_failures:
                            self._note_lifecycle(
                                handle.task_id, "usage-sample-recovered", failures=usage_failures
                            )
                        usage_failures = 0
                    if (
                        weighted is not None
                        and weighted > 0
                        and stall_nudges_sent + stall_nudges_failed == 0
                    ):
                        usage_seen = True
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
                                # transport error is not proof of death, and
                                # the crumb records the verdict was reached
                                # with liveness unknown (DW-454).
                                alive = self._probe_liveness(handle, "over-budget")
                                if alive is False:
                                    return self._final(
                                        handle,
                                        spec,
                                        "crashed",
                                        session_id,
                                        transcript_path,
                                        budget_weighted=weighted,
                                        stop_seen=stop_seen,
                                        produced_work=produced_work(),
                                    )
                                self._note_lifecycle(
                                    handle.task_id,
                                    "over-budget-fired",
                                    weighted=weighted,
                                    budget=spec.token_budget,
                                    grace_s=spec.token_budget_grace_s,
                                    zero_grace=True,
                                    liveness_unknown=alive is None,
                                )
                                return SessionResult(
                                    status="over_budget",
                                    session_id=session_id,
                                    transcript_path=transcript_path,
                                    budget_weighted=weighted,
                                    stop_seen=stop_seen,
                                    produced_work=produced_work(),
                                )
                            try:
                                self.send_text(handle, BUDGET_NUDGE_TEXT)
                            except MultiplexerError as e:
                                # a dead/hung window can't take the nudge; the
                                # grace still arms — the next tick's liveness
                                # probe scores a dead window crashed. The
                                # undelivered nudge leaves a crumb (DW-449).
                                self._note_nudge_send_failed(handle, "budget", e)
                            budget_deadline = time.monotonic() + spec.token_budget_grace_s
                            budget_wall_deadline = time.time() + spec.token_budget_grace_s
            # The token-budget grace is deliberately NOT frozen by a usage-limit
            # quota wait (DW-396): a crossed budget is real spend, and a session
            # waiting out a reset after crossing it has nothing left to earn.
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
                # over_budget verdict, flagged `liveness_unknown` (DW-454).
                alive = self._probe_liveness(handle, "over-budget")
                if alive is False:
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript_path,
                        budget_weighted=budget_weighted,
                        stop_seen=stop_seen,
                        produced_work=produced_work(),
                    )
                self._note_lifecycle(
                    handle.task_id,
                    "over-budget-fired",
                    weighted=budget_weighted,
                    budget=spec.token_budget,
                    grace_s=spec.token_budget_grace_s,
                    zero_grace=False,
                    liveness_unknown=alive is None,
                )
                return SessionResult(
                    status="over_budget",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
                    produced_work=produced_work(),
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
                try_resolve()
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    budget_weighted=budget_weighted,
                    stop_seen=stop_seen,
                    produced_work=produced_work(),
                )
            if event is None:
                try:
                    alive = self._window_alive(handle)
                except MultiplexerError as e:
                    # transport hiccup (e.g. a 30s tmux hang), not proof of
                    # death: never roll back a possibly-working session. Skip the
                    # crash check this tick; hook events still complete it, and
                    # spec.timeout_s bounds a persistent transport failure to an
                    # honest "timeout". Crumbed on the streak's first failure
                    # only (DW-447) — the heartbeat carries the running count.
                    probe_failures += 1
                    if probe_failures == 1:
                        self._note_lifecycle(
                            handle.task_id,
                            "liveness-probe-failed",
                            site="tick",
                            error=f"{type(e).__name__}: {e}",
                        )
                    continue
                if probe_failures:
                    self._note_lifecycle(
                        handle.task_id, "liveness-probe-recovered", failures=probe_failures
                    )
                probe_failures = 0
                if not alive:
                    # died without a SessionEnd hook (killed, crashed hard). A
                    # SessionStart that came before its rollout existed, then no
                    # Stop: resolve once more so the crash still carries the
                    # transcript. Before the return, not inside `produced_work()`:
                    # the exit's `transcript_path` argument is read first.
                    try_resolve()
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript_path,
                        budget_weighted=budget_weighted,
                        stop_seen=stop_seen,
                        produced_work=produced_work(),
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
                    if quota.waiting:
                        # Stall hold (DW-396): a CLI waiting out a usage-window
                        # reset is silent by design, and a nudge typed into it
                        # can only reproduce the limit line. Re-arm the grace,
                        # spend nothing. A QuotaPrompt/PermissionPrompt latch has
                        # already ended the wait, so it still parks here.
                        stall_deadline = time.monotonic() + self._stall_grace_s
                        last_activity = self._log_activity_key(handle.task_id)
                        continue
                    # The cap bounds ATTEMPTS, delivered or not (DW-449).
                    nudge_due = stall_nudges_left > 0 and (
                        spec.stall_nudges_cap is None
                        or stall_nudges_sent + stall_nudges_failed < spec.stall_nudges_cap
                    )
                    # Parked gate (DW-348/DW-350), the one decision point: a CLI
                    # waiting on a human must not be typed at, because the nudge's
                    # trailing Enter can ANSWER its prompt (#727). The hook latch
                    # first; failing that, one look at the visible screen — on
                    # EVERY expiry, not only a nudging one, so the final stall
                    # (nudges spent, or none configured) is labelled parked too
                    # and pauses instead of retrying (DW-433). Either way no
                    # nudge is typed and the session ends through the tail below.
                    # The same single capture is matched against the usage-limit
                    # lists (#610): a CLI stopped behind a usage limit is parked
                    # too (a nudge can only reproduce the limit line), and the
                    # match is recorded as `usage_limited` even when the hook
                    # latch already supplied the parked evidence, since claude's
                    # idle_prompt latches on exactly this kind of idle.
                    parked_now = parked_evidence
                    parked_hit, usage_hit = self._pane_evidence(
                        handle,
                        self._parked_prompt_patterns if parked_now is None else (),
                        self._usage_pane_patterns,
                    )
                    if parked_now is None and parked_hit is not None:
                        parked_now = parked_hit[0]
                    usage_now = usage_hit[0] if usage_hit is not None else None
                    if parked_now is None:
                        parked_now = usage_now
                    if parked_now is not None:
                        self._note_lifecycle(
                            handle.task_id,
                            "stall-nudge-withheld",
                            evidence=parked_now,
                            nudge_due=nudge_due,
                            **({"usage_limited": usage_now} if usage_now is not None else {}),
                        )
                    elif nudge_due:
                        # The wake nudge IS the re-invocation bmad-loop otherwise
                        # lacks: prod the idle session and re-arm. Budget is
                        # restored only by a fresh Stop (a real turn-end), so the
                        # nudge's own echoed keystrokes can't be mistaken for the
                        # agent waking; an unresponsive session keeps draining it.
                        stall_nudges_left -= 1
                        try:
                            self.send_text(handle, STALL_NUDGE_TEXT)
                        except MultiplexerError as e:
                            # A dead/hung window cannot take the nudge. The
                            # bounded attempt is still spent (the cap counts
                            # it), and the next tick's ordinary liveness probe
                            # owns the verdict — but it is not counted as sent
                            # and it leaves a crumb (DW-449).
                            stall_nudges_failed += 1
                            self._note_nudge_send_failed(handle, "stall", e)
                        else:
                            stall_nudges_sent += 1
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
                    # persistent failure. A dead window is never labelled parked:
                    # nothing is waiting on a human once the CLI is gone. The
                    # probe fault is crumbed (`site="stall"`, DW-454).
                    return stall_exit(parked_now, usage_now)
                continue
            # Drop a nested CLI's events before they can re-point the identity
            # or transcript, set stop_seen, spend nudges or re-arm the stall
            # timer. Deny-list (signals.SessionAttribution): only an id that
            # announced its own SessionStart after the launched session's first
            # one is foreign — and, when the launch pinned the id (DW-505,
            # `session_id_flag`), any never-own id's SessionEnd even unannounced
            # (DW-508); other unannounced events and id-less events pass. Copilot toolu_
            # subagent Stops never announce, so they pass here and stay owned by
            # the subagent filter below. Once the relay-side lineage is trusted
            # (DW-507), a "mismatch"-tagged event is foreign too, id-less or not.
            admitted = attribution.admit(event)
            if (
                not lineage_crumbed
                and attribution.lineage_state is not None
                and attribution.lineage_state != "trusted"
            ):
                lineage_crumbed = True
                self._note_lifecycle(
                    handle.task_id,
                    "hook-lineage-untrusted",
                    reason=attribution.lineage_state,
                    lineage=event.lineage,
                )
            if (
                not pin_checked
                and event.event == "SessionStart"
                and event.session_id
                and event.source not in REBIND_SOURCES
                and (admitted or event.lineage != "mismatch")
            ):
                # Observation only: attribution has already judged the event. A
                # rejected start the relay tagged "mismatch" is a nested CLI's
                # (one launched by a parallel SessionStart hook can win the race
                # to the events dir), not the CLI's answer to the pin, so it
                # defers the check to the next start.
                pin_checked = True
                if event.session_id != handle.pinned_session_id:
                    self._note_lifecycle(
                        handle.task_id,
                        "pinned-session-id-mismatch",
                        pinned_session_id=handle.pinned_session_id,
                        reported_session_id=event.session_id,
                        source=event.source,
                    )
            if not admitted:
                foreign_hook_events += 1
                # Identified drops crumb once per id; id-less ones (only a trusted
                # lineage mismatch drops those; "" counts as id-less) share the
                # None key.
                foreign_key = event.session_id or None
                if foreign_key not in crumbed_foreign:
                    crumbed_foreign.add(foreign_key)
                    self._note_lifecycle(
                        handle.task_id,
                        "foreign-hook-event-ignored",
                        hook_event=event.event,
                        foreign_session_id=foreign_key,
                        dropped_so_far=foreign_hook_events,
                    )
                continue
            if event.payload_error:
                # The relay could not read this hook's payload (events._read_payload),
                # so the event carries nulls. Completion handling is unchanged;
                # the crumb keeps the degrade visible.
                self._note_lifecycle(
                    handle.task_id,
                    "hook-payload-unreadable",
                    hook_event=event.event,
                    payload_error=event.payload_error,
                )
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
            hook_transcript_path = event.transcript_path or hook_transcript_path
            hook_path = event.transcript_path
            if resolved and hook_path and not _is_regular_file(hook_path):
                # A rollout resolved by id sticks: an unusable hook path on a
                # later event (the same garbled path a Stop repeats) must not
                # overwrite it.
                hook_path = None
            if hook_path and hook_path != transcript_path:
                # Take the idle baseline as soon as a hook names a new transcript,
                # including a later re-point (#680). A write before the next
                # heartbeat is then a change for the #727 work check rather than
                # being absorbed into the baseline at exit.
                sample_transcript(hook_path, time.monotonic())
            transcript_path = hook_path or transcript_path
            try_resolve()

            if event.event == "SessionStart":
                saw_session_start = True
                continue
            if event.event == "Notification" or event.event in PARKED_EVENTS:
                # Parked-session signal (DW-348): latch it, never act on it here.
                # The stall-grace expiry is the one decision point, so the grace
                # keeps its timing (pane growth still re-arms it). What changes
                # is the expiry: claude sends `idle_prompt` ~60 s after every
                # finished turn, so a result-less Stop followed by idle silence
                # re-latches and the session ends PARKED (a pause) instead of
                # receiving the stall wake nudge — the intended DW-348 decision.
                # A project overlay that drops `idle_prompt` from
                # `[hooks.notification_types]` restores the nudge. An unmapped
                # Notification subtype is ignored.
                evidence = self._parked_hook_evidence(event.event, event.notification_type)
                if evidence is not None:
                    parked_evidence = evidence
                    parked_kind = self._parked_hook_kind(event.event, event.notification_type)
                    self._note_lifecycle(handle.task_id, "parked-signal", evidence=evidence)
                    # A CLI now asking a human is no longer waiting on a reset
                    # (DW-396): end the wait; the next stall expiry parks it.
                    # An IdlePrompt never ends one.
                    if quota.waiting and parked_kind in QUOTA_WAIT_BLOCKING_KINDS:
                        self._end_quota_wait(handle, quota, "latch")
                continue
            if event.event == "Stop":
                # A completed turn proves any prompt the CLI was parked on got
                # answered: drop the parked latch before anything else (DW-348).
                parked_evidence = None
                parked_kind = None
                # A turn ENDED — the one canonical event that proves the CLI did
                # something, and so the hook half of the #261 proof-of-work gate.
                # Latched after the subagent filter above, which rejects a stop that
                # is not the main session's turn-end. Never cleared.
                stop_seen = True
                if resolvable and not unresolved_crumbed and not _is_regular_file(transcript_path):
                    # Not gated on session_id: a Stop whose payloads all came
                    # through as `{}` has none, and that miss must show too.
                    unresolved_crumbed = True
                    self._note_lifecycle(
                        handle.task_id,
                        "transcript-unresolved",
                        session_id=session_id,
                        hook_transcript_path=hook_transcript_path,
                    )
                result_json = self._result_json(handle, spec, wait=True)
                if result_json is not None:
                    if transcript_path:
                        # The one exit that does not go through `produced_work()`:
                        # sample once more so an idle stretch that ended inside the
                        # final interval is closed before `session-end` (#680).
                        sample_transcript(transcript_path, time.monotonic())
                    return SessionResult(
                        status="completed",
                        result_json=result_json,
                        session_id=session_id,
                        transcript_path=transcript_path,
                        budget_weighted=budget_weighted,
                        stop_seen=stop_seen,
                    )
                if quota.waiting:
                    # A result-less Stop while the CLI waits out a usage-window
                    # reset (DW-396): typing the stop nudge into it can only
                    # reproduce the limit line. Withhold it and keep waiting.
                    self._note_lifecycle(
                        handle.task_id,
                        "stop-nudge-withheld",
                        reason="quota-wait",
                        nudge_due=nudges_left > 0,
                    )
                    continue
                if nudges_left > 0:
                    nudges_left -= 1
                    try:
                        self.send_text(handle, NUDGE_TEXT)
                    except MultiplexerError as e:
                        # The next deterministic liveness probe decides whether
                        # the un-nudgeable window is dead or merely unavailable.
                        self._note_nudge_send_failed(handle, "stop", e)
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
                        produced_work=produced_work(),
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
                    produced_work=produced_work(),
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
        fixtures — for which "unknown never blocks" is the right and only answer.

        Any other stat fault also returns None — the verdict is unchanged — but it
        opens the gate on a log this adapter created, so it is not silent (DW-450):
        a ``log-evidence-failed`` crumb (``error``), once per session however many
        verdict sites ask."""
        try:
            size = (self.logs_dir / f"{handle.task_id}.log").stat().st_size
        except FileNotFoundError:
            return None
        except OSError as e:
            if self._log_evidence_faulted.get(handle.task_id) != handle.launched_ns:
                self._log_evidence_faulted[handle.task_id] = handle.launched_ns
                self._note_lifecycle(
                    handle.task_id, "log-evidence-failed", error=f"{type(e).__name__}: {e}"
                )
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

    @staticmethod
    def _transcript_activity_key(transcript_path: str) -> tuple[int, int] | None:
        """Activity signature of the live transcript the hooks named: (mtime_ns,
        size), or None when it cannot be stat'ed this tick (not yet created, torn
        by a rename, unreadable). The stat-only sibling of `_log_activity_key` for
        the #680 idle detector: the transcript is what the CLI appends to when it
        is actually doing something — a tool result, a model turn — where the pane
        log also grows for a spinner repaint. Deliberately never parsed:
        `_sample_weighted_usage` yields no sample for `usage_parser = "none"`, and idle
        detection has to work for that profile too. None is "no sample", never
        "idle" — the caller skips the tick. Parent-only by design: the subagent
        tree is keyed separately (`_sample_subagent_tree`), because the wait
        loop reads this key's size as the #727 scan offset."""
        try:
            st = Path(transcript_path).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _subagent_root(self, transcript_path: str) -> Path | None:
        """The subagent transcript tree the profile declares beside
        `transcript_path` (`CLIProfile.subagent_transcript_root`, "{stem}" = the
        transcript's filename stem), or None when the profile declares none or the
        transcript is not a regular file — a FIFO or directory has no tree."""
        template = self.profile.subagent_transcript_root
        if not template or not _is_regular_file(transcript_path):
            return None
        path = Path(transcript_path)
        return path.parent / template.format(stem=path.stem)

    def _sample_subagent_tree(
        self, task_id: str, transcript_path: str, idle: _IdleTracker
    ) -> tuple[int, int, int] | None:
        """The subagent tree's `_subagent_tree_key` this tick, or None when there
        is no tree to watch or the walk raised. A raising walk degrades the tick
        to parent-only (the pre-subagent behaviour) and is crumbed at the streak's
        transitions, as the wait loop's transcript scan is:
        `subagent-scan-failed` (`path`, `error`) at failure 1,
        `subagent-scan-recovered` (`failures`) at the next clean walk."""
        root = self._subagent_root(transcript_path)
        if root is None:
            return None
        try:
            key = _subagent_tree_key(root)
        except OSError as e:
            idle.sub_scan_failures += 1
            if idle.sub_scan_failures == 1:
                self._note_lifecycle(
                    task_id,
                    "subagent-scan-failed",
                    path=str(root),
                    error=f"{type(e).__name__}: {e}",
                )
            return None
        if idle.sub_scan_failures:
            self._note_lifecycle(
                task_id, "subagent-scan-recovered", failures=idle.sub_scan_failures
            )
            idle.sub_scan_failures = 0
        return key

    def _idle_tool_label(
        self, task_id: str, transcript_path: str, idle: _IdleTracker
    ) -> str | None:
        """The `in_tool` slug a `session-idle` about to open carries: what the
        silence is waiting on, read once per stretch. A silent transcript holds
        still, so the state read here holds for the whole stretch — a
        `tool_result` landing moves the file and closes it.

        None (the field is omitted) unless the profile parses `claude-jsonl`:
        the tool_use/tool_result shape is Claude's, so an absent field means
        "this CLI cannot tell". Otherwise `_idle_tool_slug` of the newest open
        call across the parent and its subagent tree (`_open_tool_call`), or
        "none" when nothing is open. A read or tree-walk fault is "unknown" —
        never folded into "none" — crumbed at the streak's transitions:
        `tool-scan-failed` (`error`) at failure 1, `tool-scan-recovered`
        (`failures`) at the next clean read."""
        if self.profile.usage_parser != "claude-jsonl":
            return None
        try:
            paths = [Path(transcript_path)]
            root = self._subagent_root(transcript_path)
            if root is not None:
                paths.extend(path for path, _ in _subagent_tree_files(root))
            name = self._open_tool_call(paths)
        except OSError as e:
            idle.tool_scan_failures += 1
            if idle.tool_scan_failures == 1:
                self._note_lifecycle(task_id, "tool-scan-failed", error=f"{type(e).__name__}: {e}")
            return "unknown"
        if idle.tool_scan_failures:
            self._note_lifecycle(task_id, "tool-scan-recovered", failures=idle.tool_scan_failures)
            idle.tool_scan_failures = 0
        return "none" if name is None else _idle_tool_slug(name)

    def _sample_transcript_idle(
        self, task_id: str, transcript_path: str, idle: _IdleTracker, now: float
    ) -> None:
        """One heartbeat-cadence sample of the #680 idle detector: advance `idle`
        from the transcript's current stat key and journal the stretch boundaries.

        A None key (not yet created, torn by a rename, unreadable) skips the tick
        and leaves the stretch as it was — `_sample_weighted_usage`'s tolerance,
        for a stat — but remembers that the named path was absent, so the file's
        later appearance is a change, not the baseline. A key that moved closes any open stretch with one
        `session-active` carrying the stretch's full length; a key that has not
        moved for `_idle_threshold_s` opens one with one `session-idle` (`idle_s`,
        `since_ts`, `threshold_s`), latched until the key moves again. The
        threshold is `limits.dev_stall_grace_s` on purpose — read from policy, so
        the plain adapter (triage, plugin workflows) honours it although it arms
        no stall timer: on a dev/review session the event fires exactly when the
        session WOULD have stalled had its pane not kept repainting, so the two
        records are directly comparable, and `0` disables both. No journal
        attached (`resolve.run_session`, `probe`, fixtures) means no events; the
        age is still measured for `heartbeat.json`. Every write is best-effort —
        an unwritable journal must not end a session that is, by this very
        evidence, alive.

        The profile's subagent transcript tree (`_sample_subagent_tree`) counts
        too: a parent blocked on a foreground subagent writes nothing to its own
        transcript, so a tree that moved since the last clean walk is a change
        like a parent key that moved. Its first walk on a path is a baseline. A
        walk that raises degrades the tick to parent-only, crumbed as
        `subagent-scan-failed` / `subagent-scan-recovered`; `idle.last_key` stays
        the parent's key either way.

        On a `claude-jsonl` profile, `session-idle` also carries `in_tool`
        (`_idle_tool_label`), read once as the stretch opens: the closed slug of
        the unanswered tool call the silence is waiting on — a Claude built-in
        name, "mcp", "other", "none" (nothing open) or "unknown" (the read
        raised, crumbed as `tool-scan-failed` / `tool-scan-recovered`). It
        labels, never suppresses: a running tool and a hung one leave identical
        transcripts. Other parsers omit the field."""

        def flush_active() -> None:
            if idle.pending_active_s is None or self.journal is None:
                return
            try:
                self.journal.append("session-active", task_id=task_id, idle_s=idle.pending_active_s)
            except OSError:
                return
            idle.pending_active_s = None

        if idle.path != transcript_path:
            # A different transcript than the one sampled so far (a hook event
            # re-pointed it): start over on this file — its first key is a
            # baseline, not a change. An open stretch belonged to the old file
            # and is closed here, since nothing else can close it: the TUI would
            # otherwise read the old `session-idle` for as long as the new file
            # keeps moving. Any earlier work verdict remains latched by the caller.
            if idle.open_since is not None:
                idle.pending_active_s = round(now - idle.last_change_mono, 3)
            idle.path = transcript_path
            idle.last_key = None
            idle.idle_s = None
            idle.open_since = None
            idle.seen_absent = False
            idle.sub_key = None
        key = self._transcript_activity_key(transcript_path)
        flush_active()
        if key is None:
            if idle.last_key is None:
                idle.seen_absent = True
            return
        sub = self._sample_subagent_tree(task_id, transcript_path, idle)
        sub_moved = sub is not None and idle.sub_key is not None and sub != idle.sub_key
        if sub is not None:
            idle.sub_key = sub
        if key != idle.last_key or sub_moved:
            if idle.open_since is not None:
                idle.pending_active_s = round(now - idle.last_change_mono, 3)
                flush_active()
            idle.open_since = None
            idle.last_key = key
            idle.last_change_mono = now
            idle.last_change_wall = time.time()
        idle.idle_s = round(now - idle.last_change_mono, 3)
        if (
            idle.open_since is None
            and idle.pending_active_s is None
            and self.journal is not None
            and self._idle_threshold_s > 0
            and idle.idle_s >= self._idle_threshold_s
        ):
            label = self._idle_tool_label(task_id, transcript_path, idle)
            extra = {} if label is None else {"in_tool": label}
            try:
                self.journal.append(
                    "session-idle",
                    task_id=task_id,
                    idle_s=idle.idle_s,
                    since_ts=idle.last_change_wall,
                    threshold_s=self._idle_threshold_s,
                    **extra,
                )
            except OSError:
                pass
            else:
                idle.open_since = idle.last_change_wall

    def _note_session_start_missing(self, handle: SessionHandle, waited_s: float) -> bool:
        """Journal one `session-start-missing` (`task_id`, `waited_s`, `grace_s`,
        `cli`) for a session whose profile maps SessionStart but that has run
        `limits.session_start_grace_s` without one; True once written, so the
        caller latches it.

        A CLI whose hooks run without the window's BMAD_LOOP_* env (a Codex
        session attached to a shared app-server daemon) fires its hooks into a
        relay that exits silently, so the session would otherwise look healthy
        until `session_timeout_min`. A `codex-hooks-json` profile adds `hint`,
        naming that cause. Observation only: no deadline, nudge or verdict reads
        it, and it fires on the plain adapter too, which arms no stall timer. No
        journal, grace 0, a window that is not alive (or a liveness probe that
        raised) or an unwritable journal writes nothing and leaves it unlatched,
        and the session runs on."""
        if (
            self.journal is None
            or "SessionStart" not in self.profile.hooks.events.values()
            or self._session_start_grace_s <= 0
            or waited_s < self._session_start_grace_s
        ):
            return False
        try:
            if not self._window_alive(handle):
                return False
        except MultiplexerError:
            return False
        extra: dict[str, str] = {}
        if self.profile.hooks.dialect == "codex-hooks-json":
            from ..codex_trust import DAEMON_ENV_HINT

            extra["hint"] = DAEMON_ENV_HINT
        try:
            self.journal.append(
                "session-start-missing",
                task_id=handle.task_id,
                waited_s=int(waited_s),
                grace_s=self._session_start_grace_s,
                cli=self.profile.name,
                **extra,
            )
        except OSError:
            return False
        return True

    def _window_alive(self, handle: SessionHandle) -> bool:
        return self.mux.window_alive(self.session_name, handle.native_id)

    def _window_listed(self, handle: SessionHandle) -> bool:
        # Teardown verification asks whether the window is GONE, not whether its
        # CLI runs: under `remain-on-exit on` a dead pane is still a window to
        # kill, so a kill that missed it must not read as done (#950).
        return handle.native_id in self.mux.list_window_ids(self.session_name)

    def _probe_liveness(self, handle: SessionHandle, site: str) -> bool | None:
        """`_window_alive` for a one-shot verdict site (DW-454): True/False as
        answered, or None when the probe raised ``MultiplexerError`` — after a
        ``liveness-probe-failed`` crumb (``site``, ``error``) records that the
        verdict about to be reached was reached with liveness unknown. Callers
        treat None as "not dead", exactly as before. The wait loop's per-tick
        probe stays inline: it crumbs streak transitions, not every fault."""
        try:
            return self._window_alive(handle)
        except MultiplexerError as e:
            self._note_lifecycle(
                handle.task_id,
                "liveness-probe-failed",
                site=site,
                error=f"{type(e).__name__}: {e}",
            )
            return None

    def _parked_hook_evidence(self, kind: str, notification_type: str | None) -> str | None:
        """The parked-signal evidence a hook event carries, or None (DW-348).

        A canonical parked kind (a profile mapped its CLI's native event straight
        to one) is its own evidence. The ``Notification`` carrier is parked only
        when the profile's ``hooks.notification_types`` maps its forwarded
        subtype — anything else (``auth_success``, a missing or non-string
        subtype, an older relay that forwards none) is not a wait on a human."""
        if kind in PARKED_EVENTS:
            return kind
        if kind != "Notification" or notification_type is None:
            return None
        mapped = self.profile.hooks.notification_types.get(notification_type)
        if mapped is None:
            return None
        return f"Notification({notification_type}) -> {mapped}"

    def _parked_hook_kind(self, kind: str, notification_type: str | None) -> str | None:
        """The canonical parked kind behind `_parked_hook_evidence`'s evidence:
        the kind itself, or the one the profile maps a `Notification` subtype
        to. None exactly when that evidence is None."""
        if kind in PARKED_EVENTS:
            return kind
        if kind != "Notification" or notification_type is None:
            return None
        return self.profile.hooks.notification_types.get(notification_type)

    def _quota_wait_tool_open(self, handle: SessionHandle, transcript_path: str) -> bool:
        """Whether an unanswered tool call blocks a usage-limit quota wait
        (DW-396): `_open_tool_call` over the transcript and its subagent tree.
        A CLI blocked on a foreground tool is working, whatever its pane still
        shows. Only `claude-jsonl` has the tool_use/tool_result shape; other
        parsers cannot tell and read as "none open". A read fault refuses the
        wait (a scan that cannot tell must not freeze the clock), crumbed as
        `quota-wait-tool-scan-failed`."""
        if self.profile.usage_parser != "claude-jsonl":
            return False
        try:
            paths = [Path(transcript_path)]
            root = self._subagent_root(transcript_path)
            if root is not None:
                paths.extend(path for path, _ in _subagent_tree_files(root))
            return self._open_tool_call(paths) is not None
        except OSError as e:
            self._note_lifecycle(
                handle.task_id, "quota-wait-tool-scan-failed", error=f"{type(e).__name__}: {e}"
            )
            return True

    # The quota-wait records (DW-396): a lifecycle crumb each, plus, best-effort,
    # a run-journal event with `task_id` and measured fields alone (the pane
    # evidence stays in the crumb), the `session-idle` pattern, so the TUI can
    # read it. An unwritable journal must not end a session; the crumb keeps
    # the record.

    def _note_quota_wait_started(
        self,
        handle: SessionHandle,
        evidence: str,
        remaining_s: float,
        waited_s: float,
        cap_s: float,
    ) -> None:
        self._note_lifecycle(
            handle.task_id,
            "quota-wait-started",
            evidence=evidence,
            remaining_s=remaining_s,
            waited_s=waited_s,
            cap_s=cap_s,
        )
        if self.journal is None:
            return
        try:
            self.journal.append(
                "quota-wait-started",
                task_id=handle.task_id,
                remaining_s=remaining_s,
                waited_s=waited_s,
                cap_s=cap_s,
            )
        except OSError:
            pass

    def _note_quota_wait_cap(
        self, handle: SessionHandle, evidence: str | None, waited_s: float, cap_s: float
    ) -> None:
        self._note_lifecycle(
            handle.task_id, "quota-wait-cap", evidence=evidence, waited_s=waited_s, cap_s=cap_s
        )
        if self.journal is None:
            return
        try:
            self.journal.append(
                "quota-wait-cap", task_id=handle.task_id, waited_s=waited_s, cap_s=cap_s
            )
        except OSError:
            pass

    def _end_quota_wait(self, handle: SessionHandle, quota: _QuotaWait, how: str) -> None:
        """Close an open quota wait: fold its length into `closed_s` and record
        `quota-wait-ended` (`waited_s` = this wait, `total_s` = the session's,
        `how` = `transcript-active` | `latch` | `cap` | `session-exit`)."""
        waited = round(quota.open_s(), 3)
        quota.closed_s += waited
        quota.waiting = False
        total = round(quota.closed_s, 3)
        self._note_lifecycle(
            handle.task_id, "quota-wait-ended", waited_s=waited, total_s=total, how=how
        )
        if self.journal is None:
            return
        try:
            self.journal.append(
                "quota-wait-ended", task_id=handle.task_id, waited_s=waited, total_s=total, how=how
            )
        except OSError:
            pass

    @cached_property
    def _parked_prompt_patterns(self) -> tuple[regex.Pattern[str], ...]:
        """``profile.parked_prompt_patterns``, compiled once per adapter on first
        use. The profile validated each with this same engine at parse time, so
        compiling cannot raise here. Same caching CONSTRAINT as
        ``EnvFaultMixin._env_fault_patterns``: swap ``self.profile`` before the
        first stall-expiry, or assign this attribute directly."""
        return tuple(regex.compile(p) for p in self.profile.parked_prompt_patterns)

    def _pane_evidence(
        self, handle: SessionHandle, *pattern_sets: tuple[regex.Pattern[str], ...]
    ) -> tuple[tuple[str, str] | None, ...]:
        """One look at the VISIBLE pane, matched against each pattern set: per
        set, ``(evidence, matched_line)`` for its first match (lines outer,
        patterns inner; evidence = pattern plus quoted line), else None.

        The shared matcher behind the parked gate (DW-350:
        ``parked_prompt_patterns``) and the usage-limit gates (#610:
        ``usage_limit_patterns`` + ``usage_wait_patterns``), so a decision point
        that consults several sets captures the pane ONCE. Observation that
        degrades: every set empty, or a backend that cannot capture (the seam
        default raises ``MultiplexerError``; a duck-typed backend may lack the
        method), reads as "no match" for every set; a capture fault does too;
        a search that blows ``PARKED_PROMPT_MATCH_TIMEOUT_S`` reads as "no
        match" for its own set. The two faults are not silent (DW-448): a
        ``parked-probe-failed`` crumb (``reason`` = ``capture-failed`` or
        ``match-timeout`` + its ``pattern``; ``error``) records that the look at
        the pane failed. Empty sets and a backend without ``capture_pane`` are
        configuration, not faults: no capture, no crumb."""
        none: tuple[tuple[str, str] | None, ...] = tuple(None for _ in pattern_sets)
        if not any(pattern_sets):
            return none
        capture = getattr(self.mux, "capture_pane", None)
        if capture is None:
            return none
        try:
            screen = capture(handle.pane_target or handle.native_id)
        except MultiplexerError as e:
            self._note_lifecycle(
                handle.task_id,
                "parked-probe-failed",
                reason="capture-failed",
                error=f"{type(e).__name__}: {e}",
            )
            return none
        if not isinstance(screen, str):
            return none
        lines = _ANSI_RE.sub("", screen.replace("\r", "\n")).split("\n")
        return tuple(self._first_pane_match(handle, lines, pats) for pats in pattern_sets)

    def _first_pane_match(
        self, handle: SessionHandle, lines: list[str], patterns: tuple[regex.Pattern[str], ...]
    ) -> tuple[str, str] | None:
        """``_pane_evidence``'s search of one pattern set over captured lines."""
        for line in lines:
            for pat in patterns:
                try:
                    hit = pat.search(line, timeout=PARKED_PROMPT_MATCH_TIMEOUT_S)
                except TimeoutError as e:
                    self._note_lifecycle(
                        handle.task_id,
                        "parked-probe-failed",
                        reason="match-timeout",
                        pattern=pat.pattern,
                        error=f"{type(e).__name__}: {e}",
                    )
                    return None
                if hit is not None:
                    return (f"pane matched {pat.pattern!r}: {_excerpt(line, hit.start())}", line)
        return None

    @property
    def _usage_pane_patterns(self) -> tuple[regex.Pattern[str], ...]:
        """Both usage-limit lists (#610) as the live gates consult them: disjoint
        by contract, so either one matching means usage-limited."""
        return self._usage_limit_patterns + self._usage_wait_patterns

    def _session_vanished(self, task_id: str) -> bool:
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
        #
        # has_session's False is weak by contract (#489): every non-zero exit maps
        # to it, including psmux's "Invalid session key" and "connection timed
        # out", which a LIVE session answers (DW-459). So a False is only a lead,
        # confirmed through list_window_ids, whose [] is a positive claim (the
        # session was listed empty or proved gone — for the tmux family, a stderr
        # in _SESSION_GONE_STDERR) and which raises when the listing proves
        # nothing. A raise or a listing that still finds windows is "not proved
        # gone": crumbed as a probe failure, never as a vanishing.
        try:
            if self.mux.has_session(self.session_name):
                return False
            windows = self.mux.list_window_ids(self.session_name)
        except MultiplexerError as e:
            # Unknown is not vanished — the same rule the liveness probe follows —
            # but the degrade leaves a crumb (DW-382): without it a mux that could
            # not answer reads exactly like a session that answered "still here".
            self._note_session_probe_failed(task_id, f"{type(e).__name__}: {e}")
            return False
        if windows:
            self._note_session_probe_failed(
                task_id,
                f"has_session denied the session but list_window_ids listed "
                f"{len(windows)} window(s)",
            )
            return False
        return True

    def _note_session_probe_failed(self, task_id: str, error: str) -> None:
        self._note_lifecycle(
            task_id, "session-probe-failed", session=self.session_name, error=error
        )

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self.mux.send_text(handle.pane_target or handle.native_id, text)

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
        # Whether the LAST poll raised (DW-454): that poll's "not dead" reading is
        # what sends the kill to escalation, so `kill-escalated` records it.
        liveness_unknown = False
        while True:
            try:
                dead = not self._window_listed(handle)
                liveness_unknown = False
            except MultiplexerError:
                dead = False  # transport hiccup — liveness unknown this tick, keep polling
                liveness_unknown = True
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
        self._note_lifecycle(
            handle.task_id, "kill-escalated", pids=repane, liveness_unknown=liveness_unknown
        )
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
            alive: bool | None = self._window_listed(handle)
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

    def _sample_weighted_usage(
        self, transcript_path: str, spec: SessionSpec
    ) -> tuple[int | None, str | None]:
        """``(weighted, fault)``: the cumulative weighted spend of the live
        session's transcript, or None when the guard must stay inert this tick
        (parser "none", nothing tallied yet, an unreadable file); ``fault`` is
        ``"<ExcType>: <message>"`` when the read raised, else None. Sampling must
        never break the wait loop — the liveness-probe tolerance model, for the
        usage read. The transcript is a LIVE file being appended mid-turn: a
        flush boundary can split a multibyte UTF-8 character, so the torn read
        raises UnicodeDecodeError (a ValueError) — as tolerated as an OSError.

        A fault still reads as "no sample" (the budget block skips the tick), but
        it is returned apart from a clean None (DW-452) so the wait loop can crumb
        the streak: a persistent one leaves `token_budget_mode = "enforce"` off
        for the rest of the session, which must not look like a quiet transcript."""
        try:
            usage = tally_usage(self.profile.usage_parser, Path(transcript_path))
        except (OSError, ValueError) as e:
            return None, f"{type(e).__name__}: {e}"
        if usage is None:
            return None, None
        return usage.weighted_total(spec.cache_read_weight), None

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


class _SessionHost(Protocol):
    """Next concrete adapter in the dev mixin's cooperative MRO.

    The mixin dispatches two lifecycle methods through it: ``start_session``
    (to take the launch snapshot before the transport starts) and ``run`` (to
    bound that snapshot's retention to the session's lifetime). Both hosts —
    GenericDevAdapter and OpencodeDevAdapter — inherit ``run`` from
    ``CodingCLIAdapter``, so this is the base implementation in both MROs."""

    def start_session(self, spec: SessionSpec) -> SessionHandle: ...

    def run(self, spec: SessionSpec) -> SessionResult: ...


class _DevSynthesisMixin(_ResultFileMixin):
    """Result synthesis for the generic ``bmad-build-auto`` skill, shared by
    every transport that drives it (tmux today; see GenericDevAdapter for the
    skill contract). Locates the terminal spec the skill leaves on disk and
    synthesizes the legacy result dict via :mod:`devcontract`. Hosts provide
    ``self.paths`` (a :class:`ProjectPaths`), the ``self.policy`` knobs read
    by ``_configure_dev_knobs``, and the ``_probe_alive`` liveness seam. It also
    owns the session-scoped lifetime of every per-task store it creates: its
    ``run()`` override calls ``_evict_task_state`` once the lifecycle ends, which
    drops the task's entries from all of them (DW-96, DW-107). Hosts that own a
    per-task store the mixin cannot reach override that seam and delegate up."""

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
        # between sessions; the entry is never cleared *within* the session
        # (except the in-flight stale-fingerprint clear in `_frontmatter_fallback`,
        # which deliberately restarts the count) and is evicted at the end of the
        # session by `run()`'s `finally` (see `_evict_task_state`).
        self._fm_fallback_obs: dict[str, tuple[str, int, str, int]] = {}
        # First mid-session spec-status transition observed per session (#276 M2):
        # task_id -> normalized status. Recorded by `_observe_tick` when the spec's
        # frontmatter first moves off its launch status to a non-terminal state (in
        # practice `in-review`), which makes a later terminal frontmatter proof THIS
        # session wrote it. Same lifetime doctrine as `_fm_fallback_obs` — task_ids
        # are unique per session, so an entry is recorded once and never cleared
        # *within* the session, then evicted by `run()`'s `finally`.
        self._fm_transition_obs: dict[str, str] = {}
        # Targeted contract-nudge budget (#276 M4): task_ids that have already been
        # sent the one CONTRACT_NUDGE_TEXT nudge. A set, never cleared *within* the
        # session (eviction is `run()`'s `finally`, past every reader), so the nudge
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
        #
        # The heaviest of the five stores, and the reason the eviction seam exists:
        # an unpinned launch captures one entry per `*.md` in the artifacts dir, so
        # retaining a snapshot per session would grow O(sessions x files) for the
        # adapter's lifetime (DW-96) where the other four stores grow O(sessions).
        # All five are evicted the same way, by `_evict_task_state` from `run()`'s
        # `finally`; the bound is in-flight scope, not a cap or an LRU.
        self._launch_auto_run_results: dict[str, dict[str, tuple[int, str] | None] | None] = {}
        # Last resultless-stop crumb per session (#780): task_id -> (verdict,
        # detail), overwritten by every `_note_resultless_stop` so it always holds
        # the latest. Read once, by `run()` folding it onto a non-completed result
        # before its `finally` evicts it (`_evict_task_state`); same lifetime
        # doctrine as the stores above.
        self._last_resultless: dict[str, tuple[str, str]] = {}

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
        return cast(_SessionHost, super()).start_session(spec)

    def _note_resultless_stop(self, task_id: str, verdict: str, detail: str = "") -> None:
        super()._note_resultless_stop(task_id, verdict, detail)
        self._last_resultless[task_id] = (verdict, detail)

    def run(self, spec: SessionSpec) -> SessionResult:
        try:
            result = cast(_SessionHost, super()).run(spec)
            # Surface why the read-back came up empty (#780) on the result the
            # engine journals; the crumb file alone has no reader. Here, after
            # `_post_kill_reconcile`, a rescued result is already `completed` and
            # stays unannotated. Diagnostic only — the status is untouched.
            last = self._last_resultless.get(spec.task_id)
            if result.status != "completed" and last is not None:
                verdict, detail = last
                return dataclasses.replace(
                    result, resultless_verdict=verdict, resultless_detail=detail
                )
            return result
        finally:
            self._evict_task_state(spec.task_id)

    def _evict_task_state(self, task_id: str) -> None:
        """Retention bound for the five per-task stores named below (DW-96,
        DW-106, DW-107). One documented eviction site rather than a pop scattered
        per store; hosts owning a store this mixin cannot reach (OpencodeDev-
        Adapter's `_server_procs`) override this and delegate up.

        NOT every per-session store on every host: `OpencodeHttpAdapter._usage`
        deliberately stays out. It is keyed by `session_id` rather than
        `task_id`, and `read_usage(result)` is called by the engine AFTER `run()`
        returns — so evicting it here would not just be out of scope, it would
        zero token accounting for every session. It is instead bounded by a
        capacity cap at its own write site (`USAGE_STASH_CAP`, DW-117), which
        needs no lifecycle hook at all; do not add it to this seam.

        Every in-lifecycle reader lives inside `run()` — `wait_for_completion`'s
        read-back, `_observe_tick`'s sampling and the nudge budget, and
        `_post_kill_reconcile`'s post-teardown rescue, which really does call
        `_park_marker_session_authored` (and `_probe_alive`) after the kill — so
        `run()`'s `finally` is the first point where these entries are provably
        dead evidence. A later direct read has no attempt-relative evidence and
        already fails closed on the missing key. Eviction sits at the END of the
        lifecycle because that rescue genuinely reads after the kill; it does NOT
        rest on today's rescue gates happening to make that verdict unobservable
        in the result (they do — a rescue requires a consistent `done`,
        `park_asserted` requires an `awaiting-operator` marker, and
        `synthesize_result` makes those mutually exclusive — but that is a
        coincidence of the current gates, not a reason to evict earlier).

        Called from a `finally` (not a post-return line) so a raising
        `wait_for_completion` evicts too, and scoped to the one task id so a
        concurrent in-flight session keeps its entries. `pop(..., None)` /
        `discard` (never `del`) so a `start_session` that raised before anything
        was recorded cannot replace the real exception with a KeyError."""
        self._launch_auto_run_results.pop(task_id, None)
        self._fm_fallback_obs.pop(task_id, None)
        self._fm_transition_obs.pop(task_id, None)
        self._contract_nudge_sent.discard(task_id)
        self._last_resultless.pop(task_id, None)

    def _park_marker_session_authored(self, spec_path: Path, spec: SessionSpec) -> bool:
        """Whether the live marker differs from this session's launch marker."""
        if spec.task_id not in self._launch_auto_run_results:
            # Two ways to land here, both answered the same: a direct diagnostic
            # read-back that never entered through start_session, and a read after
            # `run()` evicted the entry (DW-96). Neither has attempt-relative
            # evidence to answer from, so both fail closed.
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
        # In worktree isolation the skill runs with cwd set to the mount project
        # and writes its terminal spec under the worktree's rebased implementation-
        # artifacts dir, not the main checkout's. Resolve the search dir from the
        # live session cwd, which IS the mount project (DW-484: the worktree, or
        # `<worktree>/<offset>` under a nested `repo_root`; the project itself in
        # place, where this is a no-op, as it is for an artifact dir configured
        # outside the project tree). Only that dir: the main checkout's is never
        # where a worktree session writes, so a marker landing there after launch
        # is another writer's and must not be read back as this session's result.
        return [self.paths.artifact_at(self.paths.implementation_artifacts, Path(cwd))]

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
        safe direction, and the verdict the two control stories in #261 received.

        The one widening is ``spec.fallback_result_dirs`` (a v7 ticket session):
        while the owed plan carries no terminal signal at all — no marker and no
        terminal frontmatter — those directories are probed for the skill's
        no-spec fallback result by NAME only
        (`devcontract.find_fallback_result_artifact`). A build that could not
        resolve its ticket HALTs there instead of into the plan, and would
        otherwise ride stall-nudges to timeout. Any other spec in those
        directories still belongs to someone else and is never read."""
        deadline = time.monotonic() + RESULT_GRACE_S
        terminal = self._terminal_statuses(spec)
        while True:
            if devcontract.is_result_artifact(path, since_ns=handle.launched_ns):
                return self._synthesize_from(path, spec)
            if spec.fallback_result_dirs and not devcontract.is_frontmatter_candidate(
                path, since_ns=handle.launched_ns, terminal=terminal
            ):
                fallback = self._fallback_result(handle, spec)
                if fallback is not None:
                    return self._synthesize_from(fallback, spec)
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
        cross-checks. It also hands over the session's terminal contract:
        ``spec.success_statuses`` and the plan-halt leg (``BMAD_LOOP_PLAN_HALT``,
        the same signal the stories read-back reads), both inert at their v6
        defaults on this path."""
        story_key = spec.env.get("BMAD_LOOP_STORY_KEY") or None
        raw_dw_ids = (spec.env.get("BMAD_LOOP_DW_IDS") or "").split(",")
        dw_ids = [tok for tok in (i.strip() for i in raw_dw_ids) if tok]
        return devcontract.synthesize_result(
            spec_path,
            story_key=story_key,
            dw_ids=dw_ids or None,
            plan_halt=bool(spec.env.get("BMAD_LOOP_PLAN_HALT")),
            park_marker_session_authored=self._park_marker_session_authored(spec_path, spec),
            success_statuses=spec.success_statuses,
        )

    @staticmethod
    def _terminal_statuses(spec: SessionSpec) -> frozenset[str]:
        """The frontmatter statuses that end ``spec``'s session — the set
        `_synthesize_from` will accept, so the candidate scans never qualify a
        file synthesis would then call non-terminal (or miss one it would take).
        ``devcontract.DEFAULT_TERMINAL`` for every v6 session."""
        return devcontract.terminal_statuses(
            spec.success_statuses, plan_halt=bool(spec.env.get("BMAD_LOOP_PLAN_HALT"))
        )

    @staticmethod
    def _fallback_result(handle: SessionHandle, spec: SessionSpec) -> Path | None:
        """The newest no-spec fallback result this session may have written under
        ``spec.fallback_result_dirs`` (relative entries rebased on ``spec.cwd``,
        as `expected_spec` is), or None."""
        best: tuple[int, Path] | None = None
        for directory in spec.fallback_result_dirs:
            base = directory if directory.is_absolute() else Path(spec.cwd) / directory
            found = devcontract.find_fallback_result_artifact(base, since_ns=handle.launched_ns)
            if found is None:
                continue
            try:
                mtime_ns = found.stat().st_mtime_ns
            except OSError:
                continue
            if best is None or mtime_ns > best[0]:
                best = (mtime_ns, found)
        return best[1] if best else None

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
        the entry is never cleared within the session — ``run()``'s ``finally``
        evicts it afterwards), and any unreadable/torn read is a skipped
        sample (silent OSError return), never evidence. Blank/torn parses (``s ==
        ""``) and terminal states (``blocked`` and the session's
        ``success_statuses``: ``done``, or a ticket's ``built``) are NOT recorded — a
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
        if (
            s != ""
            and s not in (*spec.success_statuses, devcontract.BLOCKED)
            and s != snap.fm_status
        ):
            self._fm_transition_obs[task_id] = s
            self._note_lifecycle(
                task_id, "spec-status-transition-observed", spec=snap.path, status=s
            )

    @staticmethod
    def _same_spec(candidate: Path, snap_path: str) -> tuple[bool, str | None]:
        """``(same, fault)``: whether ``candidate`` and the snapshot's recorded path
        are the SAME file by filesystem identity (#276 M1), not raw string spelling.
        ``snap_path`` is the engine's ``str(task.spec_file)``; a ``..`` segment, a
        symlinked artifacts dir, or a case-variant alias makes an equivalent path
        compare unequal lexically and would silently disable the hash/transition
        gate. ``resolve()`` (the repo's identity convention, non-strict) collapses
        those; an unresolvable path degrades to "not the same file" — conservative,
        the gate stays inert rather than ever falsely refusing.

        That degrade disables the M1 refuse gate, so it is returned as ``fault``
        (``"<ExcType>: <message>"``) for the caller to crumb
        (``spec-identity-unreadable``, DW-456) — this staticmethod cannot.
        ``RuntimeError`` is caught beside ``OSError`` like every other ``resolve()``
        guard here: on the 3.11 floor a symlink loop raises it."""
        try:
            return candidate.resolve() == Path(snap_path).resolve(), None
        except (OSError, RuntimeError) as e:
            return False, f"{type(e).__name__}: {e}"

    def _snapshot_identity(self, task_id: str, candidate: Path, snap: SpecSnapshot | None) -> bool:
        """``_same_spec`` against the launch snapshot, crumbing an identity fault
        (DW-456): ``spec-identity-unreadable`` (``spec``, ``snapshot``, ``error``).
        No snapshot is no comparison and no crumb. The verdict is unchanged — a
        fault is still "not the same file" (NEUTRAL)."""
        if snap is None:
            return False
        same, fault = self._same_spec(candidate, snap.path)
        if fault is not None:
            self._note_lifecycle(
                task_id,
                "spec-identity-unreadable",
                spec=str(candidate),
                snapshot=snap.path,
                error=fault,
            )
        return same

    def _note_spec_read_fault(
        self, task_id: str, verdict: str, spec_path: Path, error: str, *, wait: bool
    ) -> None:
        """Record a spec read-back that gave up on a FAULT (DW-457) — ``stat-failed``
        or ``unreadable-spec`` — apart from the genuine answers (``no-artifact``,
        ``stale-mtime``, ``not-terminal``) it used to be filed under. A Stop
        read-back (``wait=True``) writes it as its give-up resultless-stop verdict
        (``<path>: <error>``); a one-shot ``wait=False`` read (`_final`, the
        dead-window `_post_kill_reconcile`), which otherwise leaves no record,
        writes ``spec-readback-failed`` (``reason`` = the verdict, ``spec``,
        ``error``). The read-back's answer — no result — is unchanged."""
        if wait:
            self._note_resultless_stop(task_id, verdict, f"{spec_path}: {error}")
        else:
            self._note_lifecycle(
                task_id, "spec-readback-failed", reason=verdict, spec=str(spec_path), error=error
            )

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

    @staticmethod
    def _nested_spec_hint(search_dirs: list[Path], *, since_ns: int) -> str:
        """The unpinned ``no-artifact`` crumb's #780 suffix: name specs one level
        below a searched dir that would have qualified directly in it (a
        ``stories/`` layout rides to timeout otherwise, with nothing saying why).
        Diagnosis only — the paths are named, never read back as a result. A probe
        fault is reported as such rather than read as "nothing nested". Returns ""
        when there is nothing to say."""
        hits: list[Path] = []
        faults: list[str] = []
        for d in dict.fromkeys(search_dirs):
            found, fault = devcontract.find_nested_result_hints(d, since_ns=since_ns)
            hits += [p for p in found if p not in hits]
            if fault is not None:
                faults.append(fault)
        hint = ""
        if hits:
            hint += (
                "; qualifying spec(s) found in subdirectories, which are never read back: "
                + ", ".join(str(p) for p in hits)
                + " — move the spec directly under the artifacts dir, or use stories"
                " mode ([stories] source) for a stories/ layout"
            )
        if faults:
            hint += "; subdirectory probe failed: " + "; ".join(faults)
        return hint

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
        ``ambiguous-frontmatter``, ``unreadable-spec`` (the candidate's read
        faulted, DW-457), ``unmodified-since-launch``, or
        ``terminal-frontmatter-pending`` per wait=True pass (none on a harvest).
        A plain wait=False read (the crash path) is compare-only — it may harvest
        an already-stable fingerprint but never records observations or
        breadcrumbs; the wait=False paths that leave a crumb are the hash gate,
        only under a dead window (``frontmatter-unmodified-refused``), and a
        fault: an unreadable candidate (``spec-readback-failed``, DW-457). An
        identity fault against the launch snapshot is crumbed in every mode
        (``spec-identity-unreadable``, DW-456).

        On the FIRST ``terminal-frontmatter-pending`` observation (wait=True, one
        candidate, not the hash-gate refusal, transition not yet proven) it also
        fires the #276 M4 contract nudge when ``limits.dev_contract_nudge`` is on:
        one ``CONTRACT_NUDGE_TEXT`` send asking the skill to append the marker it
        owed, then repair at the source rather than only synthesizing here. It is
        bounded by ``_contract_nudge_sent``, a set never cleared within the session
        and evicted afterwards by ``run()``'s ``finally`` (marked before the send;
        a raising send is crumbed ``nudge-send-failed``, not ``contract-nudge-sent``,
        and never retried) — exactly once per session,
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
        terminal = self._terminal_statuses(spec)
        if only is not None:
            # Authoritative-path mode (#261): the caller knows the ONE spec this
            # session owed, so the candidate set is that file if it qualifies and
            # nothing otherwise. `len(candidates) > 1` is unreachable here — the
            # "refuse to guess" branch exists for the scan, which cannot know which
            # of several specs is this session's; with a known path there is nothing
            # to guess between.
            if devcontract.is_frontmatter_candidate(
                only, since_ns=handle.launched_ns, terminal=terminal
            ):
                candidates.append(only)
            where = str(only)
        else:
            for artifacts in search_dirs:
                candidates.extend(
                    devcontract.find_frontmatter_candidates(
                        artifacts, since_ns=handle.launched_ns, terminal=terminal
                    )
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
                    (
                        f"no result artifact newer than session launch at: {where}"
                        if only is not None
                        else "no result artifact newer than session launch directly under: "
                        + where
                        + " (subdirectories are not searched)"
                        + self._nested_spec_hint(search_dirs, since_ns=handle.launched_ns)
                    ),
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
        same_file = self._snapshot_identity(task_id, path, snap)
        try:
            mtime_ns = path.stat().st_mtime_ns
            fm_status = status_of(read_frontmatter(path))
            # Content hash only when the candidate IS the snapshotted spec (compared
            # by filesystem identity) — an unrelated marker-less spec under the same
            # artifacts dir shares no launch state, so hashing it is meaningless work.
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if same_file else None
        except OSError as e:
            # Torn mid-write read: not evidence of anything — same degrade as
            # the read-back doctrine everywhere else on this path. Named as the
            # fault it is (DW-457), not as `no-artifact`, and crumbed on the
            # wait=False reads (`_final`, the dead-window reconcile) too.
            self._note_spec_read_fault(
                task_id, "unreadable-spec", path, f"{type(e).__name__}: {e}", wait=wait
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
            # set — never cleared within the session, and not the mtime-resettable
            # observation counter — is the budget, so the #149 refill hazard cannot
            # apply. Touches no stall counters. `contract-nudge-sent` is written
            # only once the send succeeded; a raising send leaves
            # `nudge-send-failed` instead and is not retried (DW-449).
            if (
                self._contract_nudge_enabled
                and observations == 1
                and task_id not in self._contract_nudge_sent
            ):
                self._contract_nudge_sent.add(task_id)
                try:
                    self.send_text(
                        handle,
                        CONTRACT_NUDGE_TEXT.format(spec_path=path, status=fm_status),
                    )
                except MultiplexerError as e:
                    self._note_nudge_send_failed(handle, "contract", e)
                else:
                    self._note_lifecycle(
                        task_id, "contract-nudge-sent", spec=str(path), status=fm_status
                    )
        return None

    def _stories_synth_result(
        self, handle: SessionHandle, spec: SessionSpec, *, wait: bool
    ) -> devcontract.SynthResult | None:
        """Deterministic stories-mode read-back: resolve ``<spec-folder>/stories/
        <id>-*.md`` by id (never the mtime scan) and synthesize from it.

        ``BMAD_LOOP_SPEC_FOLDER`` carries the project-relative (or absolute) spec
        folder; anchor a relative one on ``spec.cwd``, which is the mount project
        (DW-484), so worktree isolation resolves inside the live checkout.
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
        # Project-relative, so anchored on the project's place in the live checkout,
        # which is `spec.cwd` itself: sessions run in the mount project (DW-484),
        # and `StoriesEngine._stories_folder` joins the same folder on the project
        # (DW-379).
        base = folder if folder.is_absolute() else Path(spec.cwd) / folder
        plan_halt = bool(spec.env.get("BMAD_LOOP_PLAN_HALT"))
        deadline = time.monotonic() + RESULT_GRACE_S
        # The launch-snapshot faults (DW-456) are crumbed once per read-back call,
        # not once per grace poll: a persistent fault re-occurs every RESULT_POLL_S.
        snapshot_faults_noted: set[str] = set()

        def note_snapshot_fault(event: str, **fields: str) -> None:
            if event not in snapshot_faults_noted:
                snapshot_faults_noted.add(event)
                self._note_lifecycle(handle.task_id, event, **fields)

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
            # terminal output. `fault` is set instead when the pass gave up on a
            # read FAULT (DW-457) — `stat-failed` / `unreadable-spec`, never filed
            # as the genuine `stale-mtime` / `not-terminal` answers it used to be.
            verdict, detail = state.kind, str(state.path or base)
            if state.kind == stories.KIND_PENDING:
                # Name the glob and its flatness (#780): a spec one folder deeper
                # is never resolved, and a bare dir left that unsaid.
                detail = (
                    f"no {story_key}-*.md directly under {base / stories.STORIES_SUBDIR}"
                    " (subdirectories are not searched)"
                )
            fault: tuple[Path, str] | None = None
            if state.kind in (stories.KIND_PRESENT, stories.KIND_SENTINEL) and state.path:
                try:
                    fresh = self._written_this_session(state.path, handle.launched_ns)
                except OSError as e:
                    fresh = False
                    verdict, fault = "stat-failed", (state.path, f"{type(e).__name__}: {e}")
                if fault is not None:
                    pass
                elif not fresh:
                    verdict = "stale-mtime"
                    detail = f"{state.path} predates session launch"
                else:
                    # Launch-snapshot gate (#276 M1/M2), shared with the mtime-scan
                    # fallback so the stories read-back can't false-complete on an
                    # unmodified `done` spec. Only bites on a review session (the
                    # engine threads `spec_snapshot` there); a dev leg leaves it None
                    # → NEUTRAL → the mtime-floor accept below. Both of its faults
                    # still read NEUTRAL, crumbed (DW-456).
                    snap = spec.spec_snapshot
                    same_file = False
                    if snap is not None:
                        same_file, identity_fault = self._same_spec(state.path, snap.path)
                        if identity_fault is not None:
                            note_snapshot_fault(
                                "spec-identity-unreadable",
                                spec=str(state.path),
                                snapshot=snap.path,
                                error=identity_fault,
                            )
                    digest = None
                    if same_file:
                        try:
                            digest = hashlib.sha256(state.path.read_bytes()).hexdigest()
                        except OSError as e:
                            # torn read → NEUTRAL; synthesize keeps its degrade
                            digest = None
                            note_snapshot_fault(
                                "spec-digest-unreadable",
                                spec=str(state.path),
                                error=f"{type(e).__name__}: {e}",
                            )
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
                        sr = None
                        read_fault: str | None = None
                        try:
                            sr = devcontract.synthesize_result(
                                state.path, story_key=story_key or None, plan_halt=plan_halt
                            )
                        except UnicodeDecodeError as e:
                            # A non-UTF-8 read is either a torn glimpse of a spec still
                            # being written (keep polling — a later pass sees the finished
                            # write) or a genuinely corrupt file: then the grace expires
                            # result-less and the next _pick_next re-classifies it as a
                            # wedge (resolve_story_spec degrades an undecodable PRESENT
                            # spec to status "" → pause for resolve), never a crash of
                            # the read-back poll.
                            read_fault = f"{type(e).__name__}: {e}"
                        if sr is not None and sr.result_json is not None:
                            return sr
                        # `synthesize_result` folds its own read faults into "not
                        # terminal yet"; re-read to tell the two apart (DW-457).
                        read_fault = read_fault or self._spec_read_fault(state.path)
                        if read_fault is not None:
                            verdict, fault = "unreadable-spec", (state.path, read_fault)
                        else:
                            verdict = "not-terminal"
                            detail = (
                                f"{state.path} has no terminal status"
                                f" (frontmatter {state.status!r})"
                            )
            if not wait or time.monotonic() >= deadline:
                if fault is not None:
                    self._note_spec_read_fault(handle.task_id, verdict, *fault, wait=wait)
                elif wait:
                    self._note_resultless_stop(handle.task_id, verdict, detail)
                return None
            time.sleep(RESULT_POLL_S)

    @staticmethod
    def _spec_read_fault(spec_path: Path) -> str | None:
        """``"<ExcType>: <message>"`` when ``spec_path`` cannot be read as UTF-8
        text right now, else None. Classification only (DW-457): the stories
        read-back asks it after a synthesis that found nothing, because
        ``synthesize_result`` folds an unreadable or undecodable spec into "not
        terminal yet" — this tells that fault apart from a genuinely non-terminal
        spec for the record, without changing the answer."""
        try:
            spec_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"{type(e).__name__}: {e}"
        return None

    @staticmethod
    def _written_this_session(spec_path: Path, launched_ns: int) -> bool:
        """Whether ``spec_path`` was (re)written at/after the session launched — the
        same launch-floor guard ``devcontract.find_result_artifact`` applies on the
        scan path, so a stale terminal spec from a prior step (a dev ``done`` a
        follow-up review re-opens) is not mistaken for this session's output.

        A stat fault — a spec that vanished between resolve and stat included —
        raises ``OSError`` (DW-457): the caller still reads it as not-yet-written,
        but records it as ``stat-failed`` rather than as a spec that predates the
        launch."""
        return spec_path.stat().st_mtime_ns >= launched_ns

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
        *successful* terminal — ``done`` (or another of ``spec.success_statuses``,
        a ticket's ``built``), or the stories plan-halt leg (a
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
            # liveness unknowable: unknown is not dead — but the abandoned
            # rescue leaves a crumb (DW-453). The generic adapter's probe has
            # already crumbed its fault (`liveness-probe-failed`, `post-kill`).
            self._note_lifecycle(
                handle.task_id,
                "post-kill-rescue-abandoned",
                reason="liveness-unknown",
                status=result.status,
            )
            return result
        try:
            # dead_window: the probe above settled liveness, so the missing-
            # marker fallback (#224) may synthesize from a terminal frontmatter
            # on a single sighting — the gates below still refuse anything but
            # a self-consistent, escalation-free successful terminal.
            sr = self._synth_result(handle, spec, wait=False, dead_window=True)
        except (OSError, UnicodeDecodeError) as e:
            # An unreadable artifact is not evidence a session finished. This
            # hook runs right after run()'s finally-kill — the moment a spec the
            # CLI was mid-write is truncated, possibly through a multi-byte UTF-8
            # sequence — so a corrupt read is the *expected* fault here, not an
            # anomaly. Keep the verdict: a best-effort rescue must never escalate
            # a clean stall/timeout into an exception, which the engine does not
            # contain per-task (it fails the whole run). UnicodeDecodeError is a
            # ValueError, so both must be named. The abandon is crumbed (DW-453).
            self._note_lifecycle(
                handle.task_id,
                "post-kill-rescue-abandoned",
                reason="unreadable-artifact",
                error=f"{type(e).__name__}: {e}",
                status=result.status,
            )
            return result
        if sr is None or sr.result_json is None or not sr.status_consistent:
            return result
        rj = sr.result_json
        if rj.get("escalations") or not (
            rj.get("status") in spec.success_statuses or rj.get("plan_halt") is True
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
            quota_wait_s=result.quota_wait_s,
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
        # A raising probe is crumbed `liveness-probe-failed` (`site="post-kill"`,
        # `error`) before the caller abandons the rescue (DW-453).
        return self._probe_liveness(handle, "post-kill")


# Back-compat alias: the adapter was ``GenericTmuxAdapter`` before tmux moved
# behind the multiplexer seam. Keeps existing imports stable.
GenericTmuxAdapter = GenericAdapter
