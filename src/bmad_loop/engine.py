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
import os
import re
import shutil
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple, NoReturn, Protocol, Sequence

from . import deferredwork, devcontract, envvars, gates, operatoractions, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec, SpecSnapshot
from .bmadconfig import ProjectPaths
from .escalation import (
    Action,
    Decision,
    critical_escalations,
    decide_dev,
    decide_review_session,
    env_fault_pause_reason,
    preference_escalations,
    review_exhausted,
    review_retry_or_exhaust,
    session_failure_reason,
)
from .install import dev_primitive_or_default
from .journal import Journal, save_state
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_GATE,
    SWEEP_REFUSED_DIRTY,
    SWEEP_REFUSED_FAILED,
    SWEEP_REFUSED_NOT_STARTED,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
    VerifyOutcome,
)
from .platform_util import (
    atomic_replace,
    atomic_write_text,
    atomic_write_text_confined,
    retrying_unlink,
    safe_segment,
)
from .plugins import HookBus, HookContext, PluginRegistry
from .policy import Policy
from .recovery_flow import RecoveryFlow
from .runs import (
    clear_graceful_stop,
    consume_stop_request,
    events_dir_for,
    graceful_stop_requested,
    kill_session,
    owner_run_dir,
    read_stop_request_mode,
    reset_owner_run_dir,
    set_owner_run_dir,
)
from .sprintstatus import ACTIONABLE_STATUSES, STATUS_ORDER, SprintStatusError
from .sprintstatus import advance as sprint_advance
from .sprintstatus import advanced_bytes as sprint_advanced_bytes
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable, parse_selector
from .sprintstatus import status_in_bytes as sprint_status_in_bytes
from .sprintstatus import story_status as sprint_story_status
from .statemachine import advance
from .workspace import UnitWorkspace, Workspace, discard_worktree, open_unit_workspace
from .worktree_flow import WorktreeFlow

if TYPE_CHECKING:
    # Type-only: the worktree-provisioning helpers speak in CLI profiles.
    from .adapters.profile import CLIProfile


# `origin:` marker prefix for ledger entries harvested out of a spec's
# frontmatter `deferred:` list (BMAD-METHOD #2640). The full marker is
# `<prefix> <fingerprint>` — matched verbatim on every later harvest, so it is
# the dedup key and must never be reworded; `<prefix>-malformed <fingerprint>`
# marks the aggregated unparseable-items meta-entry.
HARVEST_ORIGIN = "spec-deferred"
# A completed session owns the spec bytes it just wrote. Give a transient repair
# read one immediate retry, but never dispatch another session over an unread
# source: that later pass is allowed to replace the frontmatter list.
HARVEST_REPAIR_READ_ATTEMPTS = 2


def _digest_of(text: str | None) -> str:
    """Hash ledger text for attribution; absent and empty are equivalent."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _bounded_stream_tail(text: str, max_bytes: int) -> tuple[str, int, int]:
    """Cut a verifier stream down to what ``verify.stream_capture_kb`` retains.

    Returns ``(tail, full_bytes, retained_bytes)``. Both counts measure the
    DECODED STREAM encoded as UTF-8 — never the file the caller writes it to,
    whose size differs on Windows because text mode translates ``\\n``. Keeping
    the counts on one side of that boundary is what makes the journal record
    unambiguous: ``full_bytes`` is what the command emitted, ``retained_bytes``
    is how much of it survived the cap, and their inequality IS the truncation.

    The TAIL is kept, the direction every other bound on this output takes
    (``run_verify_commands``' merged ``[-2000:]``): a failing suite puts its
    failure at the end.

    A byte cut can land mid-character, so the leading partial is dropped rather
    than decoded into a ``\\ufffd`` this function would be inventing — the stream
    already carries whatever replacement chars its own decode produced, and
    minting one here would put a corruption marker at a boundary WE chose.
    ``max_bytes <= 0`` needs no branch of its own: the slice is empty by
    construction, which is exactly "capture nothing".
    """
    tail, full_bytes = verify.byte_tail(text, max_bytes)
    return tail, full_bytes, len(tail.encode("utf-8"))


@dataclass(frozen=True)
class VerifyCommandRecords:
    """What one verify-command pass published to ``post_dev_verify``.

    The records themselves plus the two keys that say WHICH pass they are:
    ``stage`` (``"dev"`` | ``"fix"``) and the story's ``sequence`` ordinal. Both
    already ride the journal's ``verify-command-result`` entries; carrying them
    on the hook context too is what lets a plugin tell the two legs apart and
    join back to those entries — neither of which the results alone can do,
    since both legs emit the same stage from the same phase on one shared
    ``attempt`` counter.

    The default instance (:data:`NO_VERIFY_COMMANDS`) is the "no pass ran" value
    the callers start from, so a leg that never reaches verification publishes
    three explicit ``None``/empty fields rather than three unexplained defaults.
    ``sequence`` stays ``None`` when the pass ran but recorded nothing (no
    ``[verify] commands`` configured) — nothing was journalled, so there is no
    ordinal to join on. See ``HookContext.command_results`` for the full
    taxonomy a reader has to apply.
    """

    results: tuple[verify.CommandResult, ...] = ()
    stage: str | None = None
    sequence: int | None = None


NO_VERIFY_COMMANDS = VerifyCommandRecords()


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

    - ``graceful=False`` (default) — a *hard* stop: the SIGTERM/SIGINT handler, or
      a ``mode: "hard"`` stop request the engine honored at an item boundary
      (:meth:`Engine._check_stop_request`) or on either side of a session
      (:meth:`Engine._run_session`). The loop may have been interrupted
      mid-session, so the in-flight agent window can still be live and must be
      torn down unconditionally.
    - ``graceful=True`` — a stop requested via the ``stop-request.json`` control
      file in its default ``graceful`` mode and detected at an item boundary
      (:meth:`Engine._check_stop_request`). The in-flight item already completed
      through commit, so ``run()`` runs the wanted subset of the clean-finish path
      (worktree GC + ``post_run`` + policy-gated session teardown) rather than a
      hard kill, and the run stays resumable.

    ``via`` names the channel a hard stop arrived on — ``"stop-request"`` for the
    control file, ``None`` for a signal — and rides the ``run-stop`` journal entry.
    It is the only evidence that separates the two on a native-Windows run, where
    the signal path cannot fire at all (#319)."""

    def __init__(self, graceful: bool = False, via: str | None = None):
        super().__init__("graceful stop" if graceful else "stopped")
        self.graceful = graceful
        self.via = via


class SweepFactory(Protocol):
    """Call shape of the child-sweep launcher :meth:`Engine._maybe_auto_sweep`
    drives — in the product, the inner function ``cli._sweep_factory`` returns,
    injected so this module need not import ``cli``, ``runsetup`` or ``sweep``.

    Spelled as a Protocol rather than a ``Callable[[str], None]`` alias, mirroring
    :class:`runsetup.MakeAdapters`, only because the keyword-only ``started``
    thunk is part of the contract and a positional callable alias cannot say so.

    ``started`` fires once the child sweep is composed and its run dir published
    (:func:`runsetup.compose_sweep`), and is what lets the engine spend the run's
    trigger on a child that actually started. It is **required, with no default**:
    the engine reads "raised without calling it" as "no child was ever launched"
    and leaves the trigger unspent, so a defaulted no-op would let an un-updated
    implementation make that claim for a child that ran — the one direction that
    costs a duplicate sweep. It is idempotent, so an implementation in doubt
    should call it."""

    def __call__(self, trigger: str, *, started: Callable[[], None]) -> None: ...


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
    # stories that committed but owe human-only external actions (Phase
    # AWAITING_OPERATOR). Defaulted, unlike weighted_tokens: a miscounted 0 here
    # is a count that is genuinely 0 on every run that never parks a story, so
    # the default is honest rather than silently wrong.
    awaiting_operator: int = 0
    crashed: bool = False
    crash_error: str | None = None
    # auto-sweep triggers this run did not deliver, as (trigger, SWEEP_REFUSED_*)
    # pairs (#501). Defaulted for the same reason as awaiting_operator: empty is
    # the honest value on every run that refused nothing.
    #
    # A tuple of pairs, NOT the dict `RunState` holds it in, because this class is
    # `frozen=True`: a dict field leaves the "snapshot" mutable through its own
    # container, and — silently — makes every RunSummary unhashable, since
    # frozen+eq synthesizes `__hash__` from the field tuple. Nothing hashes one
    # today, which is exactly why that would go unnoticed. `summary()` converts.
    sweeps_refused: tuple[tuple[str, str], ...] = ()

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
        # Appended only when non-zero: a run that parks nothing is the norm, and
        # a standing ", 0 awaiting operator" would train readers to skip the very
        # clause that matters on the run where it is not zero.
        parked = f", {self.awaiting_operator} awaiting operator" if self.awaiting_operator else ""
        lines = [
            f"run {self.run_id}: {self.done} done, {self.deferred} deferred, "
            f"{self.escalated} escalated{parked}, {tokens}"
        ]
        if self.crashed:
            lines.append(f"CRASHED: {self.crash_error}")
        if self.paused:
            lines.append(f"PAUSED: {self.paused_reason}")
        # Appended only when it fired, like `parked` above. Under
        # `[sweep] auto = "run-end"` there is exactly one trigger per run and it
        # is never re-asked once the run finishes (see `_maybe_auto_sweep`), so
        # this line IS the remedy — the refusal is otherwise journal-only, and
        # the operator never learns the deferred work went untouched. The clean
        # worktree is named because `cmd_sweep` hard-refuses an unclean tree:
        # without it the follow-up lands the operator in a second refusal.
        if self.sweeps_refused:
            detail = ", ".join(f"{trigger} ({why})" for trigger, why in self.sweeps_refused)
            lines.append(
                f"SWEEP NOT RUN: {detail} — deferred work is untouched; "
                "run `bmad-loop sweep` with a clean worktree"
            )
        return "\n".join(lines)


# Appended to an injected plugin-workflow session prompt AHEAD of the completion
# contract below, whenever the run has a sprint board (`_sprint_board_instruction`
# non-empty; StoriesEngine empties it and this section disappears with it). Same
# words as the dev/review seams inject, so the three surfaces cannot drift apart —
# `{clause}` is that method's return value verbatim, not a second copy of it.
#
# A workflow session is dispatched in exactly the window `_sprint_board_instruction`
# describes: post_dev_phase and post_review_result run after `_post_dev_state_sync`
# has advanced sprint-status.yaml, pre_commit_gate runs before `finalize_commit`
# lands the story's single commit — so all three open on the same uncommitted,
# unattributed board change that #437's review session reverted.
#
# Carries the prohibition ONLY. The `blocked` hand-back redirect stays review-only
# for the reason its own docstring gives: it synthesizes a CRITICAL that halts the
# whole run, which is the wrong trade for a session that is not the sign-off
# authority. A workflow that genuinely cannot proceed already has a channel — the
# completion contract's own `status: blocked` marker, which the orchestrator reads
# as a non-completion and routes through the blocking/advisory decision instead.
#
# A section rather than a bare sentence because a workflow prompt is plugin-authored
# markdown of unknown shape; a trailing sentence would read as a clause of whatever
# it happens to land after. The heading names the owner, matching the clause's
# opening words.
WORKFLOW_BOARD_CONTRACT = """

## Sprint board (orchestrator-owned)

{clause}"""


# Appended to every injected plugin-workflow session prompt. The dev/review
# skills carry their own result conventions, but a workflow prompt is arbitrary
# text from a plugin manifest — without an explicit protocol the session has to
# *infer* the completion-marker convention, and one that finishes its work but
# never writes the marker leaves the orchestrator waiting (a completion-signal
# livelock, bounded only by session_timeout_min). The orchestrator's adapter
# discovers the marker by its `<dev primitive>-result-` filename prefix and
# mtime, not by exact name (devcontract.FALLBACK_RESULT_PREFIXES accepts both
# the pre- and post-rename spellings, so either resolution reads back).
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


def _at_or_past(landed: str | None, target: str) -> bool:
    """Whether ``sprintstatus.advance``'s return means the row REACHED ``target``.

    Mirrors ``advance``'s own never-regress comparison so the two agree on what
    "already there" means; `None` (missing file, or absent row) is never reached.
    A status outside ``STATUS_ORDER`` is unorderable rather than late, so it counts
    only on an exact match — the same direction ``advance`` takes when it declines
    to compare an unknown current against a known target.
    """
    if landed is None:
        return False
    if landed == target:
        return True
    if landed not in STATUS_ORDER or target not in STATUS_ORDER:
        return False
    return STATUS_ORDER.index(landed) >= STATUS_ORDER.index(target)


# A story id in this repo is a dash/dot composite ("1.1", "3-2", "1-1-a"), so the
# leading-label strip must start at a DIGIT and may then run on through letters.
# Both looser and tighter patterns get this wrong in opposite directions: `\S+`
# eats the real title in "Story Points: Add estimates", while `\d+\.\d+` stops
# matching the dash composites this project actually issues.
_STORY_LABEL_RE = re.compile(r"^story\s+\d[\w.\-]*:\s*", re.IGNORECASE)
# C0 controls + DEL + unpaired surrogates. Not cosmetic: this title reaches
# `git commit -m` as an argv element, and both classes are unspawnable there —
# `subprocess.run` rejects an embedded NUL with a plain ValueError, and a lone
# surrogate has no UTF-8 encoding, so encoding the argv raises UnicodeEncodeError.
# Neither is in `_run_git`'s translated set (TimeoutExpired, UnicodeDecodeError,
# OSError — and UnicodeEncodeError is a sibling of the decode class, not a
# subclass), so either escapes as itself, hits `_finalize_commit_phase`'s
# `except BaseException` re-raise, and crashes the run with the task already
# persisted as COMMITTING — wedging every later resume on the same spec. YAML
# reaches both without any exotic file bytes: `title: "\0"` and `title: "\uD800"`
# are ordinary double-quoted scalars that PyYAML hands back verbatim. The rest of
# the control class goes along because a newline or CR in a commit subject is
# mangling, not a title. Translating these at the `_run_git` chokepoint instead
# of here is the general fix, tracked as #506.
_TITLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]+")
# An ATX heading's optional closing hash run, which CommonMark requires to be
# preceded by whitespace — so "# C#" keeps its trailing hash and "# Wire it ###"
# does not.
_ATX_CLOSING_RE = re.compile(r"[ \t]+#+[ \t]*$")


def _story_label_stripped(value: object, story_key: str = "") -> str:
    """A commit-ready story title from a frontmatter value or heading text:
    coerced to str, control characters neutralized, label dropped, whitespace
    collapsed. Returns "" for anything that leaves no title behind, which is
    every caller's signal to fall back.

    ``story_key`` is the task's own id, matched literally as a second way to
    recognize the label. `StoriesEngine` inherits this renderer and `stories`
    issues alphabetic ids ("auth", "oauth-setup") that the digit-led pattern
    cannot see. It is an ADDITION to that pattern and not a replacement: a
    sprint spec labels itself "Story 1.1:" while its key is "1-1-a", so keying
    only off the task id would stop stripping the common case.

    Coerces rather than type-checks because the value arrives from YAML, where
    an unquoted title parses to whatever it looks like — ``title: 2026-01-01``
    is a ``date``, ``title: 1.1`` a float — and a rendered date still beats
    falling back to the bare story key. A ``None`` (blank ``title:``) coerces to
    "" and falls back, matching how `status_of` treats a null status."""
    if value is None or isinstance(value, bool):  # `title: yes` is a typo, not a title
        return ""
    text = _TITLE_CONTROL_RE.sub(" ", str(value)).strip()
    stripped = _STORY_LABEL_RE.sub("", text)
    if stripped == text and story_key:
        # The id is escaped, so a key carrying regex metacharacters ("a.b") is
        # matched literally rather than compiled into a wildcard.
        stripped = re.sub(rf"^story\s+{re.escape(story_key)}\s*:\s*", "", text, flags=re.IGNORECASE)
    # Collapse after the label strip, so a label split by control characters
    # ("Story\x001.1:") is still recognized rather than surviving into the subject.
    return " ".join(stripped.split())


# Call-stack nesting depth for engine runs. A nested auto-sweep runs synchronously
# in the same thread as its parent (see _maybe_auto_sweep), so a ContextVar carries
# the parent's depth into the child. Tracked independently of signal ownership so an
# off-main-thread top-level run (which cannot own signals) is still seen as depth-0.
_run_depth: contextvars.ContextVar[int] = contextvars.ContextVar("bmad_loop_run_depth", default=0)


class _ArmedClose(NamedTuple):
    """One armed story-close rollback: what ``Engine._restore_deferred_closes``
    undoes if the commit the close was written for never lands (#234, #286).

    ``ids`` is entry-scoped, never a whole-document snapshot: the restore reopens
    exactly these ledger entries through their operation-specific undo markers, so
    anything a concurrent writer appended, closed or decided inside the commit
    window survives the rollback untouched.

    ``exact`` says which set ``ids`` is. Armed BEFORE the write it is the INTENDED
    set (``exact=False``) — a raise inside the close itself must still be undoable,
    and reopening an id that never flipped is a safe no-op. Replaced after a normal
    return by the set actually marked (``exact=True``), which is the only form
    where an id that fails to reopen means something: its undo marker was there
    moments ago and is not now, so a foreign edit displaced it, and that is worth
    journaling rather than repairing around.

    Lives at module scope, not beside its method, because ``SweepEngine`` annotates
    the same parameter — a class body cannot host a module-level type.
    """

    ledger: Path
    ids: tuple[str, ...]
    exact: bool


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
        sweep_factory: SweepFactory | None = None,
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
        # dev-primitive name resolved from disk, memoized per (workspace project
        # root, skill tree) — see _dev_skill. By tree because one run can mix them
        # (dev=claude reads .claude/skills, review=codex reads .agents/skills), with
        # None for an adapter that carries no profile at all; by project root
        # because under isolation each unit resolves against its OWN worktree and
        # one Engine drives every unit of a run.
        self._dev_skill_cache: dict[tuple[Path, str | None], str] = {}
        # story_key -> the highest `verify-command-result` sequence allocated so
        # far. None until the first verify pass seeds it from the journal — see
        # _next_verification_sequence, which owns the whole invariant.
        self._verification_sequences: dict[str, int] | None = None
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
            carry_isolated_ledger_writes=lambda task: self._carry_isolated_ledger_writes(task),
            escalation_pause=self._escalation_pause,
            workspace_get=lambda: self.workspace,
            workspace_set=lambda ws: setattr(self, "workspace", ws),
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
        # Publish this run dir as the owner for everything below, so a nested
        # auto-sweep's adapters poll the file an operator can actually write to
        # (#319): `stop <parent-id>` lodges here, while the child's own dir stays
        # empty. Gated on depth, not on `_owns_signals` — a top-level run off the
        # main thread installs no handlers yet still owns the channel. Reset by
        # token in the same finally, ahead of the depth, so the nested re-raise arms
        # unwind through both.
        owner_token = None if self._is_nested else set_owner_run_dir(self.run_dir)
        try:
            return self._run_inner()
        finally:
            if owner_token is not None:
                reset_owner_run_dir(owner_token)
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
                self._replay_unlatched_ledger_carries()
                self._loop()
                # A hard request that landed after `_loop`'s head check reaches none
                # of the raise sites on an exhausted-queue return: sites A and B live
                # inside `_run_session`, which the `story is None` branch never
                # enters, and the run-end auto-sweep predicate is mode-blind, so it
                # *suppresses* and returns rather than raising. Without this the run
                # would record `finished` — which `documents.py` ranks above
                # `stopped` — while the operator's hard stop went unhonored, and
                # `stop_run`'s fallback would then journal `fallback=True` against a
                # perfectly responsive engine, contradicting what that flag now means.
                # Covering it here rather than at the suppression site closes every
                # `_loop` return path at once (including `max-stories-reached`) and
                # keeps the per-epic sweep caller untouched. Mode-exact on purpose: a
                # *graceful* request at an exhausted queue finishes truthfully, which
                # is long-documented, separately tested behavior this must not disturb.
                if read_stop_request_mode(self.run_dir) == "hard":
                    clear_graceful_stop(self.run_dir)
                    raise RunStopped(via="stop-request")
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
                    # (_check_stop_request), so the in-flight item already ran to
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
                    # Hard stop: the loop was interrupted inside adapter.run() (a
                    # signal), or unwound on either side of it because a hard stop
                    # request was honored — so the agent window may still be live.
                    # Tear the whole run session down.
                    kill_session(self.state.run_id)
                    if self._is_nested:
                        raise  # nested auto-sweep: let the owner record the stop
                    self.state.stopped = True
                    # The signal path consumes nothing on its way here, and `stop_run`
                    # now lodges a hard request *before* it signals — so on POSIX the
                    # file is still on disk for every routine stop. `run()`'s finally
                    # would then discard it as *stale* and journal
                    # `stop-request-discarded`, misreporting the very request this
                    # stop delivers. Consume it here, on the same rule the boundary and
                    # in-session sites already follow. Mode-exact: a pending *graceful*
                    # request really is superseded by a hard stop, so it is left for
                    # the finally to discard and journal, as it always has been.
                    if read_stop_request_mode(self.run_dir) == "hard":
                        clear_graceful_stop(self.run_dir)
                    # `via` rides only when the control file delivered the stop;
                    # the signal path keeps journaling a bare `run-stop` (precedent:
                    # the KeyboardInterrupt arm's `reason=` extra below).
                    extras = {"via": stop.via} if stop.via is not None else {}
                    self.journal.append("run-stop", **extras)
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
                # Any pending stop-request control file that outlived this run is
                # discarded here so a later resume does not re-honor a stale request.
                # Every arm that *honors* a request consumes its own file first — the
                # boundary and in-session sites, and the hard arm above, which has to
                # because `stop_run` lodges before it signals and the signal path
                # reads nothing. So this fires only for a request no arm honored: the
                # run finished, paused or crashed with one pending, or a hard stop
                # superseded a *graceful* one. Journaling those as discarded is
                # accurate; journaling a request that just stopped the run would not
                # be, which is the whole reason the honoring arms consume.
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

    def _merge_local(
        self,
        task: StoryTask,
        unit: UnitWorkspace,
        *,
        replay: bool = False,
        replay_strategy: str | None = None,
    ) -> None:
        self._worktree_flow.merge_local(
            task,
            unit,
            replay=replay,
            replay_strategy=replay_strategy,
        )

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
            awaiting_operator=sum(1 for t in tasks if t.phase == Phase.AWAITING_OPERATOR),
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
            # Snapshotted, not aliased: this dict is still live on the engine's
            # state, and the tuple makes the copy structural rather than a
            # convention a later edit could drop.
            sweeps_refused=tuple(self.state.sweeps_refused.items()),
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

    def _check_stop_request(self) -> None:
        """Honor a pending stop request at an item boundary, in the mode it asks for.

        Consumes (deletes) the ``stop-request.json`` control file and raises
        :class:`RunStopped` — ``graceful=True`` for a ``graceful`` request (the
        default mode, and every pre-#319 modeless body, which
        :func:`runs.read_stop_request_mode` deliberately reads as graceful) so
        ``run()`` unwinds into the clean-finalization arm; ``via="stop-request"``
        for a ``hard`` one so it takes the hard arm instead. An exception, not a
        sentinel return, because the sweep check fires two frames below ``_loop``
        (inside ``_cycle``) where a return could not stop the loop. Called as the
        first statement of the loop body (and, in the sweep engine, before each
        bundle): by the time control reaches here the in-flight item has already
        completed through commit, so the stop takes effect cleanly at the next
        boundary and the run stays resumable.

        A *hard* request that reaches a boundary is honored right here rather than
        deferred to the adapter's in-session poll — aborting at the boundary is
        both faster and cleaner than launching the next session only to abort it
        mid-flight."""
        # One atomic take, never a read then an unlink: a `stop` escalating to
        # "hard" between the two would be deleted unread while this engine routed
        # on the stale graceful mode it already held. Consuming on BOTH arms is
        # still required — `run()`'s finally discards any surviving file as *stale*
        # and journals `stop-request-discarded`, which would misreport a request
        # this engine just honored.
        mode = consume_stop_request(self.run_dir)
        if mode is None:
            return
        if mode == "hard":
            raise RunStopped(via="stop-request")
        raise RunStopped(graceful=True)

    def _loop(self) -> None:
        self._finish_inflight()
        while True:
            # First statement of the loop body: one site covers every story
            # boundary this base loop reaches — between stories, right after
            # _finish_inflight on resume, and the epic boundary + run-end (the
            # StoriesEngine has no _loop override, so it is covered here too).
            self._check_stop_request()
            if self.max_stories is not None and self._dispatched_count() >= self.max_stories:
                self.journal.append("max-stories-reached", count=self._dispatched_count())
                return
            self._emit("pre_pick_next")
            story = self._pick_next()
            self._emit("post_pick_next", story_key=(story.key if story is not None else None))
            if story is None:
                self._maybe_auto_sweep("run-end", "run-end")
                return
            # Before ANY state mutation for this story, and deliberately so — see
            # _refuse_gated_story. The story is not in state.tasks yet, so a resume
            # re-picks it and re-asks the ledger.
            self._refuse_gated_story(story.key)
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

    def _refuse_gated_story(self, story_key: str) -> None:
        """Pause the run rather than dispatch a story an unlanded ledger entry gates.

        The enforcing half of ``gate:``. ``bmad-loop validate`` refuses the same
        story at preflight, but a preflight is only as strong as the operator's
        habit — ``run`` never called it, and ``_pick_next`` reads the board alone,
        so before this the field's whole promise rested on someone remembering to
        type a second command.

        **Placement is load-bearing.** Called from ``_loop`` before
        ``state.tasks[key] = task``, so the gated story is *not* recorded as
        touched by this run. That is what makes the refusal re-askable: a resume
        re-picks the same story and re-reads the ledger, so closing the entry and
        resuming runs it. Registering the task first — the obvious placement, next
        to ``_run_story`` — would put the key in ``_pick_next``'s ``base_skip``,
        and the gate would fire once and then silently retire the story for the
        rest of the run and every resume of it. A gate that drops the work it was
        protecting is worse than no gate.

        **Pause, not skip.** ``validate`` fails the whole preflight over one gated
        story, and the two surfaces have to agree or the operator learns to
        distrust both. It raises the reserved :data:`PAUSE_STORY_GATE` stage, which
        the TUI already renders and routes to its gate viewer.

        The ledger is re-read here rather than carried from preflight: a sweep (or
        a human) may have closed the entry since, and a gate answering from a stale
        snapshot would refuse work that has landed.

        **Unreadable ledger pauses too.** Degrading to "not gated" would let the
        one deferred check that is a refusal be disabled by a broken file, and the
        question "does this project use gates?" is answerable only from the file
        that will not open. ``deferred.ledger-unreadable`` is a ``validate``
        problem for the same reason.

        Two exemptions, both deliberate. ``SweepEngine`` overrides ``_loop`` and so
        never reaches this call — it must not, because the sweep is the only
        automated closer of the gating entry (``sweep.py`` `_close_resolved` /
        bundle close), and gating the sweep would deadlock the gate against its own
        remedy. And a resumed story *finishes* rather than stranding a half-done
        session with a live worktree; the gate applies to work that must not
        *start*, which is the same line ``validate`` draws when it passes a story
        the board has already finished.

        That second exemption belongs to ``_finish_inflight``'s finishing arms —
        the defer replay, the spec-approval continuation, the recorded-session
        replay, the commit completion — and not to its restart arm, which finishes
        nothing: it discards the worktree (or resets to baseline) and re-runs the
        story from scratch. So the restart arm re-asks this gate, and
        unconditionally. ``_pick_next`` cannot ask for it, having skipped the key as
        touched, and the run's own crash must not be what disables the one deferred
        check that refuses.

        Both call sites ask **before** their caller mutates anything, and that is
        one rule rather than two coincidences. In ``_loop`` it keeps the refusal
        re-askable; in the restart arm it keeps the ledger readable, because that
        arm's in-place rollback is ``git reset --hard <baseline>`` and a gate
        committed while the run was down is a commit *after* that baseline.

        Deliberately no "but did a session really run?" test there. Every available
        signal is wrong somewhere: ``attempt`` is bumped before the session launches,
        ``sessions`` is written only after one returns, and ``rearmed`` covers a
        stories-mode wedge (``StoriesEngine._pause_wedged``) that reaches ESCALATED
        with no session at all. The arm's own unwinding is the stronger guarantee.
        """
        ledger = self.paths.deferred_work
        try:
            text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        except (OSError, UnicodeDecodeError) as e:
            self.journal.append("story-gate-unreadable", story_key=story_key, error=str(e))
            reason = (
                f"{ledger} cannot be read ({e}), so the `gate:` hard gates protecting "
                f"{story_key} could not be evaluated — fix the file, then "
                f"`bmad-loop resume {self.state.run_id}`"
            )
            gates.notify(self.policy, self.run_dir, f"story gated: {story_key}", reason)
            raise RunPaused(reason, PAUSE_STORY_GATE, story_key) from e
        blocking = [
            (entry.id, hits)
            for entry, hits in (
                (
                    entry,
                    [
                        token
                        for token in deferredwork.gates(entry).tokens
                        if deferredwork.gates_story(token, story_key)
                    ],
                )
                # `done`, not `not open`: an entry whose status the format cannot
                # read is not evidence the work landed, and reading it as closed
                # would let a one-character typo disable the gate.
                for entry in deferredwork.parse_ledger(text)
                if not entry.done
            )
            if hits
        ]
        if not blocking:
            return
        named = ", ".join(f"{dw_id} (gate: {', '.join(hits)})" for dw_id, hits in blocking)
        reason = (
            f"{story_key} is gated by unlanded deferred work: {named} — close the "
            f"entry in {ledger.name} (`status: done <date>`) or run `bmad-loop sweep`, "
            f"then `bmad-loop resume {self.state.run_id}`"
        )
        self.journal.append(
            "story-gated",
            story_key=story_key,
            dw_ids=[dw_id for dw_id, _ in blocking],
        )
        gates.notify(self.policy, self.run_dir, f"story gated: {story_key}", reason)
        raise RunPaused(reason, PAUSE_STORY_GATE, story_key)

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

    def _preserve_attempt_worktree(self, task: StoryTask, *, allow_pause: bool) -> None:
        self._recovery_flow.preserve_attempt_worktree(task, allow_pause=allow_pause)

    def _pause_for_manual_recovery(
        self,
        task: StoryTask,
        baseline: str,
        *,
        preserve_failed: bool = False,
        snapshot_failed: bool = False,
    ) -> None:
        self._recovery_flow.pause_for_manual_recovery(
            task,
            baseline,
            preserve_failed=preserve_failed,
            snapshot_failed=snapshot_failed,
        )

    def _replay_unlatched_ledger_carries(self) -> None:
        """Replay a successful isolated unit's carry after merge-time host loss.

        Called from ``run()`` and required to PRECEDE ``_loop``. ``SweepEngine``
        replaces ``_loop`` wholesale and does not override ``run()``, so this frame
        is the only one both engines share — and a replayed bundle close has to
        leave the open set before ``_loop`` reads ``deferredwork.open_ids``, or the
        resumed sweep re-triages and re-drives work that already landed.
        """
        entries = self.journal.entries()
        merged_units = {
            (
                str(entry.get("story_key", "")),
                str(entry.get("branch", "")),
                str(entry.get("target", "")),
            )
            for entry in entries
            if entry.get("kind") == "unit-merged"
        }
        started_units = {
            (
                str(entry.get("story_key", "")),
                str(entry.get("branch", "")),
                str(entry.get("target", "")),
                str(entry.get("source", "")),
            ): str(entry.get("strategy", ""))
            for entry in entries
            if entry.get("kind") == "unit-merge-started"
        }
        for task in list(self.state.tasks.values()):
            if (
                task.phase == Phase.DEFERRED
                and task.worktree_path
                and task.harvest_carry_commit_pending
            ):
                self.journal.append("resume-ledger-carry", story_key=task.story_key)
                # The HARVEST alone, exactly as `_defer`'s isolated arm calls it.
                # Replaying a defer through the composite hook would let a mode
                # override carry writes the defer deliberately withheld — a sweep
                # bundle's close names work whose code this defer discarded, and
                # `open_ids` re-bundles only `open` entries, so a wrong `done` is
                # invisible to every later sweep.
                self._carry_harvested_deferrals(task)
                continue
            if task.isolated_ledger_carried or task.phase not in (
                Phase.DONE,
                Phase.AWAITING_OPERATOR,
            ):
                continue
            merged_key = (task.story_key, task.branch, self.state.target_branch)
            # `not task.worktree_path` is the in-place guard, and a truthiness test
            # is the whole of it: an in-place task carries "", and `Path("")` is
            # `PosixPath(".")` whose `is_dir()` is True, so any probe placed ahead of
            # this one absorbs the in-place case and leaves it unfalsifiable. Merge
            # evidence comes from the journal below, never from a mounted worktree.
            # A close-only payload replays too: a sweep bundle whose ledger writes
            # are all closures has an empty `harvested_deferrals`, and skipping it
            # here would strand the close and re-triage resolved work for ever.
            # Every payload the hook carries has to be named here — a story whose
            # only ledger write was a damped review-budget follow-up, or a declared
            # `closes_deferred:` flip, has every other list empty, and omitting it
            # strands that write in a deleted worktree. The board advance (#350) is
            # the ordinary case of exactly that: nearly every generic story records
            # one and nothing else, so leaving it out here would strand the write
            # that keeps `_pick_next` from re-picking the finished story.
            if not task.worktree_path or not (
                task.harvested_deferrals
                or task.bundle_closes_intended
                or task.refiled_followups
                or task.story_closes_intended
                or task.board_advance_intended
            ):
                continue
            if merged_key not in merged_units:
                source = task.commit_sha or ""
                started_key = (*merged_key, source)
                replay_strategy = started_units.get(started_key)
                if not source or replay_strategy is None:
                    continue
                # The write-ahead record is intent, never merge proof. Re-run the
                # exact merge and latch completion only after git confirms it;
                # merge/ff are naturally idempotent, while squash enables its
                # recovery-only clean-tree success arm.
                self.journal.append(
                    "resume-unit-merge",
                    story_key=task.story_key,
                    branch=task.branch,
                    target=self.state.target_branch,
                    strategy=replay_strategy,
                    source=source,
                )
                unit = self._reopen_unit(task)
                self._merge_local(
                    task,
                    unit,
                    replay=True,
                    replay_strategy=replay_strategy,
                )
                merged_units.add(merged_key)
            self.journal.append("resume-ledger-carry", story_key=task.story_key)
            self._carry_isolated_ledger_writes(task)
            task.isolated_ledger_carried = True
            self._save()
            # The failed integration unwound before _run_story returned, so the
            # loop never reached its normal post-integration continuation.
            self._after_story(task)

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            isolated = self._isolated and task.worktree_path
            if isolated and task.defer_reason is not None:
                # _defer records its reason before carrying harvested findings.
                # A read/commit fault (or host loss before the terminal advance)
                # can therefore leave a rejected result in the same DEV_VERIFY +
                # spec_file shape as a verified spec-approval pause. A persisted
                # defer reason on a nonterminal isolated task is that interrupted
                # decision's intent; finish it before any session-replay arm.
                self.journal.append("resume-defer", story_key=task.story_key)
                unit = self._reopen_unit(task)
                prev = self.workspace
                self.workspace = unit.workspace
                try:
                    self._defer(task, task.defer_reason)
                finally:
                    self.workspace = prev
                self._integrate_unit(task, unit)
            elif task.phase == Phase.DEV_VERIFY and task.spec_file:
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
                # This arm is the one that does not finish work: it discards the
                # worktree or resets the tree to baseline and re-runs the story
                # from scratch, so what follows is a *start* and gets the same
                # question `_loop` asks. Unconditionally — any test for "did a
                # session really run?" is wrong somewhere: `attempt` is bumped
                # before the session launches, `sessions` is written only after one
                # returns, and `rearmed` covers a stories-mode wedge that reached
                # ESCALATED with no session at all.
                #
                # Asked BEFORE the unwinding below, for the same reason `_loop`
                # asks before it registers the task: the in-place rollback is
                # `git reset --hard <baseline>`, and a `gate:` committed while the
                # run was down lives in a commit *after* that baseline. Rolling
                # back first would rewind a tracked ledger and put the question to
                # a file the human never wrote — `keep=(".bmad-loop",)` guards only
                # untracked deletion, which is exactly why `verify.safe_rollback`
                # has to restore `policy.toml` by hand. It also keeps the pause
                # honest under the default `rollback_on_failure = false`, where
                # `_rollback_or_pause` would otherwise pause for manual recovery
                # and never reach the gate. The cost is that a refused isolated
                # task keeps its half-built worktree mounted until a resume gets
                # past the gate — the same thing an escalation pause does, and the
                # cheaper of the two mistakes.
                self._refuse_gated_story(task.story_key)
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

        DEV_VERIFY reaches this matcher only when no persisted defer identifies
        an interrupted decision and ``task.spec_file`` is empty (verify did not
        fully pass before the death): _finish_inflight checks the defer-replay
        and spec-approval-gate arms first, so a DEV_VERIFY task WITH a verified
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

    def _current_dev_session_index(self, task: StoryTask) -> int | None:
        """Index of the newest primary dev record for the current attempt."""
        task_id = _session_task_id(task.story_key, "dev", task.attempt)
        for index in range(len(task.sessions) - 1, -1, -1):
            if task.sessions[index].task_id == task_id:
                return index
        return None

    def _accept_current_dev_session(self, task: StoryTask) -> None:
        """Latch the current dev or repair record as the accepted tree owner."""
        accepted_index = self._current_dev_session_index(task)
        if accepted_index is None:
            raise RuntimeError(f"accepted dev decision for {task.story_key} has no session record")
        task.accepted_dev_session_index = accepted_index

    def _accepted_dev_session_matches(self, task: StoryTask) -> bool:
        """Whether the current primary dev record owns the PROCEED receipt."""
        accepted = task.accepted_dev_session_index
        return accepted is not None and accepted == self._current_dev_session_index(task)

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
                    session_failure_reason(f"blocking workflow {wf.name!r} ({lp.name})", result),
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

    def _dispatched_spec_for_attempt(self, task: StoryTask) -> str | None:
        """Resolve the recorded sprint spec this dev attempt will own.

        The result is an observation made immediately before the attempt's
        durable DEV_RUNNING save. Missing, stale, and non-file paths deliberately
        leave the attempt unbound. Persist the canonical regular-file name rather
        than a symlink spelling, so a child cannot retarget the binding after
        launch and make recovery restore the snapshot into another trusted file.
        """
        if not task.spec_file:
            return None
        try:
            spec_path = verify.resolve_spec_path(task.spec_file, self.workspace.paths)
            if spec_path.is_symlink():
                return None
            resolved = spec_path.resolve(strict=True)
            if not resolved.is_file() or not verify.spec_within_roots(
                resolved, self.workspace.paths
            ):
                return None
            return str(resolved)
        except (OSError, RuntimeError):
            return None

    def _read_dispatched_spec_snapshot(self, task: StoryTask) -> tuple[str, bytes] | None:
        """Read stable bytes from the already-authoritative canonical path.

        This deliberately never resolves ``task.spec_file`` anew: after prompt
        construction, promoting a transiently unbound attempt would let recovery
        claim a file the launched bare-key prompt never named. The open-file and
        post-read pathname identities must agree, so an atomic regular-file
        replacement cannot pair bytes from the old inode with the new name.

        This observer never mutates the task. In particular, validating a retained
        retry-chain snapshot must not temporarily install child-authored bytes: an
        asynchronous stop in that window would make those bytes durable.
        """
        if not task.dispatched_spec_file:
            return None
        spec_path = Path(task.dispatched_spec_file)
        try:
            if spec_path.is_symlink():
                raise RuntimeError("attempt-owned spec became a symlink")
            resolved = spec_path.resolve(strict=True)
            if (
                resolved != spec_path
                or not resolved.is_file()
                or not verify.spec_within_roots(resolved, self.workspace.paths)
            ):
                raise RuntimeError("attempt-owned spec is no longer a trusted regular file")
            with resolved.open("rb") as stream:
                before = os.fstat(stream.fileno())
                snapshot = stream.read()
                after = os.fstat(stream.fileno())
            current = resolved.stat(follow_symlinks=False)

            def identity(st):
                return (st.st_dev, st.st_ino)

            def contents(st):
                return (st.st_size, st.st_mtime_ns)

            if (
                identity(before) != identity(after)
                or identity(after) != identity(current)
                or contents(before) != contents(after)
                or contents(after) != contents(current)
                or resolved.is_symlink()
                or resolved.resolve(strict=True) != resolved
            ):
                raise RuntimeError("attempt-owned spec changed identity while being read")
        except (OSError, RuntimeError):
            return None
        return str(resolved), snapshot

    def _refresh_dispatched_spec_snapshot(
        self,
        task: StoryTask,
        *,
        clear_on_failure: bool = True,
    ) -> bool:
        """Refresh both halves of a fresh attempt's ownership authority.

        Initial observation may degrade to an unbound bare-key attempt, so its
        failure clears stale authority. Once a child has been promised an explicit
        spec, callers pass ``clear_on_failure=False``: preserving the last trusted
        path and bytes lets crash recovery refuse a vanished or retargeted file
        instead of forgetting that the unsafe binding existed.
        """
        observed = self._read_dispatched_spec_snapshot(task)
        if observed is None:
            if clear_on_failure:
                task.dispatched_spec_file = None
                task.dispatched_spec_snapshot = None
            return False
        task.dispatched_spec_file, task.dispatched_spec_snapshot = observed
        return True

    def _validate_dispatched_spec_snapshot(self, task: StoryTask) -> bool:
        """Validate the bound path without replacing the retry-chain bytes.

        A fixable retry deliberately inherits the previous child's working tree,
        but a later non-fixable retry still resets the whole chain to the phase
        baseline. The retained snapshot must therefore remain the bound input from
        that chain's first launch, not a body edit authored by an intermediate
        repair session. During a resolved re-drive that input is the operator's
        correction.
        """
        if task.dispatched_spec_snapshot is None:
            return False
        observed = self._read_dispatched_spec_snapshot(task)
        if observed is None:
            return False
        if not task.spec_file:
            return True
        try:
            accepted = verify.resolve_spec_path(task.spec_file, self.workspace.paths)
            if accepted.is_symlink():
                return False
            resolved = accepted.resolve(strict=True)
            accepted_target = accepted.parent.resolve(strict=True) / accepted.name
            if (
                resolved != accepted_target
                or not resolved.is_file()
                or not verify.spec_within_roots(resolved, self.workspace.paths)
            ):
                return False
        except (OSError, RuntimeError):
            return False
        return str(resolved) == observed[0]

    def _bind_dispatched_spec_for_attempt(self, task: StoryTask) -> None:
        """Atomically observe this attempt's regular spec and pre-launch bytes.

        The path and snapshot are one authority pair: a read fault leaves both
        unbound, so recovery can never restore bytes that belong to a stale path.
        Called once before DEV_RUNNING becomes durable. Later orchestrator and
        hook mutations refresh only this established path through
        ``_refresh_dispatched_spec_snapshot``.
        """
        task.dispatched_spec_snapshot = None
        task.dispatched_spec_file = self._dispatched_spec_for_attempt(task)
        self._refresh_dispatched_spec_snapshot(task)

    @staticmethod
    def _prompt_names_recorded_spec(task: StoryTask, prompt: str) -> bool:
        """Whether the prompt contains an engine-authored explicit-spec token."""
        if not task.spec_file:
            return False
        spec = str(task.spec_file)
        return f"`{spec}`" in prompt or bool(
            re.match(rf"^/\S+\s+{re.escape(spec)}(?:\s|$)", prompt)
        )

    def _requires_dispatched_spec_snapshot(self, task: StoryTask, prompt: str) -> bool:
        """Whether this prompt makes the recorded spec attempt-owned input.

        Sprint and Stories repair routes name the spec they will mutate, so they
        may launch only with a recoverable byte snapshot. Engine variants whose
        explicit spec pointer has different ownership semantics override this
        predicate rather than being identified here by type or task shape.
        """
        return self._prompt_names_recorded_spec(task, prompt)

    def _retains_dispatched_spec_snapshot_on_repair(self) -> bool:
        """Whether fixable repairs remain in the current spec-input chain."""
        return True

    def _preserves_dispatched_spec_snapshot_for_repair(self, task: StoryTask) -> bool:
        """Whether this repair must retain (or fail closed on) chain authority."""
        return self._retains_dispatched_spec_snapshot_on_repair() and (
            task.resolved_redrive
            or task.dispatched_spec_file is not None
            or task.dispatched_spec_snapshot is not None
        )

    def _dev_phase(self, task: StoryTask, resume_result: SessionResult | None = None) -> bool:
        if resume_result is None:
            # A fresh invocation cannot consume a snapshot armed by an earlier,
            # non-replayable invocation. Keep crash replay's snapshot intact.
            self._disarm_ledger_snapshot(task)
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
            # Start the ledger attribution reference under the same fresh-entry
            # rule. The harvest exclusion compares each attempt's pre-harvest
            # ledger with this reference so a session-authored ledger edit is
            # never hidden along with the orchestrator's own append. A fixable
            # retry rebases it onto the tree that retry deliberately keeps.
            task.baseline_ledger_digest = self._ledger_digest()
        feedback: Path | None = None
        while True:
            replayed = resume_result is not None
            if resume_result is None:
                # a resumed result replays the attempt it was recorded under, so
                # the counter (and the session task_id derived from it) must not
                # advance; a second host death then still finds the record and
                # re-enters this continuation instead of falling back to restart.
                task.attempt += 1
                # A genuinely new attempt starts from a rolled-back tree and owes
                # nothing to the rejected attempt's harvest. A fixable retry keeps
                # that tree and ledger intentionally, so preserve its payload for a
                # later isolated carry. Crash replay never enters this branch.
                if feedback is None:
                    task.harvested_deferrals = []
                    # Same rule, and the abandoned attempt's ledger row is already
                    # gone: an escalation leaves the unit worktree mounted and
                    # unmerged, and the re-drive that reaches here discarded it
                    # (`_finish_inflight`'s resume-restart arm), taking the only
                    # copy of that row. Carrying the record anyway files a
                    # follow-up against the attempt that COMMITTED, whose review
                    # recommended none — and `append_entry` has nothing to dedupe
                    # it against, so a tracked ledger commits the wrong row rather
                    # than absorbing it (#457). `rearm_escalation` already
                    # voids the history behind the record by resetting
                    # `followup_reviews_spent` for a fresh damping budget.
                    #
                    # The fixable-retry exemption above is inherited, not reasoned:
                    # this field's one producer fires on a finalized, verify-green
                    # story immediately before `_commit`, and no path leads from
                    # there back into a `feedback is not None` iteration, so such an
                    # iteration can never hold a record to preserve.
                    task.refiled_followups = []
                # A fresh-baseline dispatch replaces stale ownership. A fixable
                # repair inherits the current working tree, but retains the chain's
                # first bound snapshot because a later non-fixable retry resets all
                # the way to the phase baseline. For a resolved re-drive those are
                # the operator-corrected bytes.
                # Recorded-result replay never enters this branch and therefore
                # retains the persisted binding unchanged.
                preserve_chain_snapshot = (
                    feedback is not None
                    and self._preserves_dispatched_spec_snapshot_for_repair(task)
                )
                if not preserve_chain_snapshot:
                    self._bind_dispatched_spec_for_attempt(task)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            if (
                resume_result is None
                and preserve_chain_snapshot
                and not self._validate_dispatched_spec_snapshot(task)
            ):
                # Persist the no-session attempt before failing. Resume must enter
                # rollback/recovery from DEV_RUNNING, not mistake the preceding
                # DEV_VERIFY state for a completed spec-approval pause.
                raise RuntimeError(
                    "attempt-owned spec became unreadable during pre-launch snapshot"
                )
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
                prompt = self._dev_prompt(task, feedback)
                # Capture the exact bytes a fresh-baseline child will inherit after
                # orchestrator-owned pre-launch mutations. A fixable repair validates
                # that same path but retains the chain's first snapshot, because a
                # later non-fixable retry resets the whole chain.
                had_binding = task.dispatched_spec_file is not None
                snapshot_required = (
                    preserve_chain_snapshot
                    or had_binding
                    or self._requires_dispatched_spec_snapshot(task, prompt)
                )
                snapshot_ok = not snapshot_required
                if had_binding:
                    if preserve_chain_snapshot:
                        snapshot_ok = self._validate_dispatched_spec_snapshot(task)
                    else:
                        snapshot_ok = self._refresh_dispatched_spec_snapshot(
                            task, clear_on_failure=False
                        )
                self._save()
                if snapshot_required and not snapshot_ok:
                    raise RuntimeError(
                        "attempt-owned spec became unreadable during pre-launch snapshot"
                    )
                result = self._run_session(
                    task,
                    role="dev",
                    prompt=prompt,
                    seq=task.attempt,
                    preserve_dispatched_spec_snapshot=preserve_chain_snapshot,
                )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            verified = NO_VERIFY_COMMANDS
            if result.status == "completed":
                # Everything below this point that appends to the ledger is the
                # orchestrator, not the session. Preserve attribution on crash
                # replay: the replayed harvest dedupes against the dead attempt's
                # on-disk append.
                # Before the first harvest write, a replay can still reconstruct
                # session authorship from the persisted baseline digest. Once an
                # engine write is latched, the current ledger includes that write
                # and replay must preserve the attribution saved before harvest.
                if not replayed and feedback is None:
                    task.harvest_wrote_ledger = False
                if (
                    replayed
                    and task.baseline_ledger_digest is None
                    and not task.harvest_wrote_ledger
                ):
                    # A state written before ledger attribution has no digest to
                    # compare, but it still has the attempt's Git baseline. Use
                    # the pre-harvest path diff to preserve legitimate ledger-only
                    # session work without letting the append below become its
                    # own proof. A later replay with harvest_wrote_ledger already
                    # latched preserves the attribution saved by this first one.
                    task.ledger_changed_before_harvest = self._legacy_ledger_changed_before_harvest(
                        task
                    )
                    self._save()
                elif not replayed or not task.harvest_wrote_ledger:
                    if task.baseline_ledger_digest is not None:
                        task.ledger_changed_before_harvest = (
                            self._ledger_digest() != task.baseline_ledger_digest
                        )
                    self._save()
                # Snapshot after the session has finished, preserving any ledger
                # edits it authored, and before reconcile/state-sync/harvest can
                # write on the engine's behalf. The harvest is the engine-side
                # ledger write this snapshot currently protects; the sweep bundle
                # close lives below the acceptance gate instead. Keep the chain's
                # first snapshot
                # across fixable retries because a later non-fixable reset returns
                # all the way to the phase baseline. An unarmed replay may safely
                # arm from disk; an armed replay must retain the dead attempt's
                # pre-harvest bytes.
                if (not replayed and feedback is None) or not task.pre_harvest_ledger_captured:
                    task.pre_harvest_ledger = self._ledger_text()
                    task.pre_harvest_ledger_captured = True
                    self._save()
                # bmad-build-auto sometimes finalizes the spec in prose (## Auto Run
                # Result: Status done) but leaves the frontmatter status at the
                # template default. Repair it BEFORE any frontmatter reader runs —
                # the sync below, verify_dev, and the review-verify gate all key
                # off the on-disk frontmatter status.
                self._reconcile_generic_terminal_status(task, result.result_json)
                # Harvest the session's frontmatter `deferred:` findings into the
                # ledger before the artifact gate so an accepted attempt carries
                # them in its story commit. `_harvest_gate_exclude` keeps this
                # engine-authored append from becoming proof of session work. This
                # strict read can also finish a reconciliation whose preceding
                # observation faulted, so state sync follows it and sees the
                # repaired terminal status.
                harvest_outcome = self._harvest_spec_deferrals(task, result.result_json)
                # Generic-path single-writer for the sprint bookkeeping the
                # decoupled skill never touches, before verify reads that state.
                # Sweep bundles override this pre-gate seam to a no-op; their
                # ledger close belongs to the accepted-only seam below.
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
                outcome = harvest_outcome or self._verify_dev_artifacts(task, result.result_json)
                if outcome.ok and self._run_verify_commands_after_dev(task, result.result_json):
                    # deterministic gates run here too: a broken build must not
                    # reach the (far more expensive) review loop
                    outcome, verified = self._verify_commands_with_results(task, "dev")
            self._emit(
                "post_dev_verify",
                task,
                session_status=result.status,
                result_json=result.result_json,
                verify_reason=(outcome.reason if outcome is not None else None),
                command_results=verified.results,
                # The dev-vs-repair discriminator + the journal join key. Left at
                # NO_VERIFY_COMMANDS' Nones on every arm that never reached
                # verification, which `session_status`/`verify_reason` name.
                verification_stage=verified.stage,
                verification_sequence=verified.sequence,
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
                # The all-roles greppable record rides session-end via
                # `_session_end_extras` (#489); here the flag pairs the
                # diagnosis with the decision it fed.
                session_vanished=result.session_vanished,
            )
            if decision.action == Action.PROCEED:
                # DEV_VERIFY + spec_file is not itself proof of acceptance: this
                # save also precedes every rejecting decision branch. Persist an
                # explicit transaction latch so recovery may replay accepted-only
                # bookkeeping without closing work from a PAUSE/RETRY/DEFER.
                self._accept_current_dev_session(task)
            self._save()
            if decision.action == Action.PROCEED:
                self._finish_post_dev_accepted_sync(task, result.result_json)
                self._emit("post_dev_phase", task)
                if self._run_workflows("post_dev_phase", task, task.attempt):
                    return False
                return True
            if decision.action == Action.RETRY:
                if outcome is not None and outcome.fixable:
                    # work exists and the failure is concrete: keep the tree,
                    # hand the failing output to a repair session
                    feedback = self._write_feedback(task, decision.reason)
                    # The repair session is judged against the kept chain. Move
                    # the ledger reference onto that tree so the retained harvest
                    # is accounted for, while a new session-authored ledger edit
                    # still makes `ledger_changed_before_harvest` true.
                    task.baseline_ledger_digest = self._ledger_digest()
                else:
                    feedback = None
                    try:
                        self._rollback_or_pause(task)
                    finally:
                        # An exception from rollback means this attempt is leaving
                        # the decision pipeline just as surely as a returned
                        # rollback. Detect the active unwind without limiting it to
                        # RunPaused: reset/preserve failures are the #420 gap.
                        unwinding = sys.exc_info()[0] is not None
                        restore_error: OSError | None = None
                        # Recovery resets code/spec state but protects artifact
                        # directories through `_safe_reset`'s
                        # keep=(".bmad-loop", *self._protected_relpaths()) shield.
                        # Consequently an ordinary untracked harvest-created ledger
                        # survives unless restored explicitly; `git reset --hard`
                        # performs the ledger revert only when the ledger is tracked.
                        # Run this for stop-and-wait too: the operator's eventual
                        # reset would not remove an untracked or ignored file either.
                        try:
                            self._restore_persisted_ledger(task, replayed=replayed)
                        except OSError as e:
                            # Preserve an exception already in flight; replacing a
                            # RunPaused/reset fault would misclassify the run and
                            # skip the stale-arm cleanup below. The journal keeps
                            # this secondary repair failure visible.
                            restore_error = e
                            self.journal.append(
                                "ledger-restore-failed",
                                story_key=task.story_key,
                                error=f"{type(e).__name__}: {e}",
                            )
                        # Rebase from the snapshot already in hand so a filesystem
                        # fault cannot replace a RunPaused while unwinding.
                        if task.pre_harvest_ledger_captured:
                            task.baseline_ledger_digest = _digest_of(task.pre_harvest_ledger)
                        # Leaving this attempt spends the chain snapshot even when
                        # its repair failed: retaining stale bytes across a pause or
                        # crash would let resume overwrite later operator work. A
                        # fixable retry never enters this branch and keeps its arm.
                        self._disarm_ledger_snapshot(task)
                        if unwinding or restore_error is not None:
                            # A pause or rollback failure skips the next loop/save;
                            # persist before it can ride a human window or #420's
                            # crash path. A restore failure must likewise persist
                            # the disarm before it is raised. On a cleanly returned
                            # rollback the next attempt's ordinary save supplies
                            # durability.
                            self._save()
                        if restore_error is not None and not unwinding:
                            # Repair writes fail loud when there is no earlier
                            # exception whose identity must be preserved.
                            raise restore_error
                continue
            # DEFER and escalation preserve their current tree/knowledge rather
            # than rejecting the attempt, so neither may later consume this arm.
            self._disarm_ledger_snapshot(task)
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
        spec is absent.

        The skill's no-spec fallback artifact (``bmad-build-auto-result-*``, or
        ``bmad-dev-auto-result-*`` pre-rename — ``FALLBACK_RESULT_PREFIXES`` matches
        both; written when intent was too unclear to even CREATE a spec) is refused:
        it is not the story's spec, so every consumer here misreads it.
        ``rearm_escalation`` would flip frontmatter on a marker no re-drive reads,
        ``_reset_spec_for_repair`` would re-open it as if it were the frozen intent
        contract, and the repair prompt would point the session at it — which the
        #261 read-back then pins to, polling a stale marker while the re-drive's
        real spec goes unread."""
        if task.spec_file:
            return
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        spec_path = verify.resolve_spec_path(str(spec_file), self.workspace.paths)
        if spec_path.name.startswith(devcontract.FALLBACK_RESULT_PREFIXES):
            return
        if spec_path.is_file():
            task.spec_file = str(spec_path)

    def _review_and_commit(
        self, task: StoryTask, resume_result: SessionResult | None = None
    ) -> None:
        if self._park_awaiting_operator(task):
            return
        if not self.policy.review.enabled:
            # review.enabled = false: the bmad-build-auto session's own inline
            # review is the only review; verify the deterministic gates + commit.
            self._skip_review_and_commit(task)
            return
        # review.enabled = true (default): run a follow-up review session by
        # re-invoking bmad-build-auto on the done spec (BMAD-METHOD #2508 routes a
        # `done` spec to a fresh step-04 review pass). The dev session self-
        # finalizes the spec to done (no in-review handoff) and the orchestrator
        # advances sprint-status at dev time (_post_dev_state_sync), so this runs
        # as an independent second-opinion pass on a done spec before commit.
        #
        # review.trigger = "recommended" (default) gates that loop per-story on the
        # bmad-build-auto session's `followup_review_recommended` signal (PR #2505):
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
                # the review re-invokes bmad-build-auto on the done spec, and the
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
            # A review pass is itself a bmad-build-auto run: it produces a spec
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
            # A follow-up review is another full dev-primitive pass and may add
            # findings to the same frontmatter list. Harvest after reconciliation
            # makes a prose-finalized pass visible; the dev-leg entries dedupe.
            harvest_outcome = None
            for harvest_attempt in range(1, HARVEST_REPAIR_READ_ATTEMPTS + 1):
                harvest_outcome = self._harvest_spec_deferrals(task, rj)
                if harvest_outcome is None:
                    break
                self.journal.append(
                    "review-verify-failed",
                    story_key=task.story_key,
                    reason=harvest_outcome.reason,
                    env_fault=harvest_outcome.env_fault,
                    contradiction=harvest_outcome.contradiction,
                    harvest_attempt=harvest_attempt,
                )
                if not harvest_outcome.retryable:
                    self._escalate(task, harvest_outcome.reason)
            if harvest_outcome is not None:
                # The bounded deterministic retry could not recover the source.
                # Do not launch another reviewer: a new pass may replace the
                # unread `deferred:` list and silently erase a finding. Route as
                # exhausted without spending a session so a resolved CRITICAL
                # re-drive re-escalates instead of being downgraded to a defer.
                exhausted = review_exhausted(
                    task,
                    "review deferral harvest remained unreadable after "
                    f"{HARVEST_REPAIR_READ_ATTEMPTS} attempts: {harvest_outcome.reason}",
                )
                if exhausted.action == Action.PAUSE:
                    self._escalate(task, exhausted.reason)
                self._defer(task, exhausted.reason)
                return
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
                    contradiction=outcome.contradiction,
                )
                if not outcome.retryable:
                    # escalate-grade failure (environment fault, git error, a
                    # review revoking the sprint sign-off): a repair session
                    # cannot fix it and another review cycle would replay it —
                    # pause the run instead of burning budget
                    self._escalate(task, outcome.reason)
                if outcome.fixable and task.review_cycle < self.policy.limits.max_review_cycles:
                    # failing verify commands are dev work, not review work: a
                    # re-review of the same tree cannot make them pass. Repair
                    # with the failing output as feedback, then re-review. This
                    # verify-repair round never spends the damping cap.
                    fix = self._fix_phase(task, outcome.reason)
                    if fix.action == Action.PAUSE:
                        self._escalate(task, fix.reason)
                    if fix.action == Action.DEFER:
                        self._defer(
                            task,
                            fix.reason or "verify commands kept failing after clean review",
                        )
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
            if refileable_followup and not self._isolated:
                rescue = self._verify_review(task)
                if rescue.ok:
                    self._record_review_budget_followup(task)
                    self._commit(task)
                    return
                if rescue.contradiction:
                    # The rescue gate is the first place this story's sprint
                    # sign-off was re-read (every in-loop cycle recommended its own
                    # follow-up, so none of them verified). A defer here would roll
                    # the work back under a "did not converge" reason that names
                    # neither side of the disagreement — pause with both instead.
                    # Journaled under the same kind as the two in-loop gates so a
                    # consumer keying on `contradiction` sees all three escalating
                    # paths. The non-contradiction arm keeps its existing silence:
                    # its story is told by the defer reason below.
                    self.journal.append(
                        "review-verify-failed",
                        story_key=task.story_key,
                        reason=rescue.reason,
                        env_fault=rescue.env_fault,
                        contradiction=rescue.contradiction,
                    )
                    self._escalate(task, rescue.reason)
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
        of burning another review cycle. Returns True when salvage either committed
        or terminally routed an unreadable harvest; False means salvage was not
        applicable and the caller falls back to the default retry/exhaust routing.
        The cycle the timed-out session charged is deliberately not refunded —
        salvage changes what the *next* cycle costs, not what this one did.

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
        fm_status = verify.status_of(fm)
        if fm_status not in ("done", "in-review"):
            return False
        reset_from: str | None = None
        if fm_status == "in-review":
            # Repair-write doctrine: these raise on an unreadable spec rather
            # than silently proceeding stale (see _reset_spec_for_repair).
            reset_from = fm_status
            confine_root = self.workspace.paths.project
            devcontract.reset_spec_status(spec_path, "done", confine_root=confine_root)
            devcontract.strip_auto_run_result(spec_path, confine_root=confine_root)
        # A timed-out review can still have recorded new frontmatter findings.
        # Normalize first so the success-status gate sees `done`, then mirror the
        # normal review path before deterministic verification and commit.
        harvest_outcome = None
        for harvest_attempt in range(1, HARVEST_REPAIR_READ_ATTEMPTS + 1):
            harvest_outcome = self._harvest_spec_deferrals(task, {"spec_file": str(spec_path)})
            if harvest_outcome is None:
                break
            self.journal.append(
                "review-timeout-salvage-failed",
                story_key=task.story_key,
                cycle=task.review_cycle,
                reason=harvest_outcome.reason,
                env_fault=harvest_outcome.env_fault,
                harvest_attempt=harvest_attempt,
            )
            if not harvest_outcome.retryable:
                self._escalate(task, harvest_outcome.reason)
        if harvest_outcome is not None:
            exhausted = review_exhausted(
                task,
                "review timeout deferral harvest remained unreadable after "
                f"{HARVEST_REPAIR_READ_ATTEMPTS} attempts: {harvest_outcome.reason}",
            )
            if exhausted.action == Action.PAUSE:
                self._escalate(task, exhausted.reason)
            self._defer(task, exhausted.reason)
            return True

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

    def _park_awaiting_operator(self, task: StoryTask) -> bool:
        """Take a dev-declared ``awaiting-operator`` park down the commit path,
        skipping the review loop. Returns True when it did (the caller is done).

        The review loop is skipped because there is nothing for it to converge
        ON. A review pass is bmad-build-auto re-invoked on the spec to second-guess
        the diff and finalize `done`; a park's outstanding work is not in the diff
        at all — it is outside the repo, in a human's hands — so every cycle would
        either re-park (no progress, budget burned) or "fix" the park away by
        finalizing `done`, which is the exact false-green the state exists to
        prevent. What the loop DOES contribute, the deterministic gate, is kept:
        this delegates to the same skip-review commit path, whose `_verify_review`
        now holds the park to its (awaiting-operator, awaiting-operator) pair, a
        non-empty action list, and the project's verify commands. Parked work
        clears every check `done` work clears — no commit path skips verification.

        No `_defer` machinery: a park is a SUCCESS that commits, so there is no
        stash or rollback, and the ordinary path has no ledger snapshot to
        unwind. A crash-resumed DEV_VERIFY park can inherit an arm from before
        PROCEED's disarm; that exceptional snapshot is spent below. (A park that
        fails verification is not a park yet; it defers like any other unverified
        work, through the shared path below.)"""
        if not self._operator_park_enabled():
            return False
        spec_path = Path(task.spec_file) if task.spec_file else None
        if spec_path is None or not spec_path.is_file():
            return False
        fm = self._observed_frontmatter(spec_path, task.story_key, "operator-park")
        if fm is None or verify.status_of(fm) != verify.AWAITING_OPERATOR:
            return False
        # A crash can resume a persisted DEV_VERIFY task directly into this
        # main-only path, bypassing `_dev_phase`'s accepted-attempt disarm. Spend
        # any inherited snapshot before the commit/awaiting-human window, and
        # persist it now so a host loss inside that window cannot strand stale
        # pre-harvest bytes on the terminal task.
        if task.pre_harvest_ledger_captured:
            self._disarm_ledger_snapshot(task)
            self._save()
        # Latched BEFORE _commit's advance(COMMITTING) + _save, so a host death
        # anywhere in the commit window resumes into _finalize_commit_phase with
        # the actions already on the persisted task — and that arm re-derives
        # AWAITING_OPERATOR from them with no change of its own (#115).
        task.operator_actions = list(verify.operator_actions_of(fm))
        self._skip_review_and_commit(task, kind="review-skipped-awaiting-operator")
        return True

    def _skip_review_and_commit(self, task: StoryTask, *, kind: str = "review-skipped") -> None:
        """review.enabled = false: no separate review session runs. The
        bmad-build-auto session ran its own inline review and finalized the
        story to done. Validate the deterministic gates (verify commands,
        spec/sprint = done) and commit, repairing once if verify is fixable.

        Also the commit path for an ``awaiting-operator`` park, which reaches
        here with ``kind="review-skipped-awaiting-operator"`` — same gates, same
        repair-once, same commit; only the reason the review was skipped differs,
        and the journal says which."""
        self.journal.append(kind, story_key=task.story_key)
        outcome = self._verify_review(task)
        if not outcome.ok and outcome.fixable:
            fix = self._fix_phase(task, outcome.reason)
            if fix.action == Action.PAUSE:
                self._escalate(task, fix.reason)
            if fix.action == Action.DEFER:
                self._defer(
                    task,
                    fix.reason or f"verify failed with review disabled: {outcome.reason}",
                )
                return
            outcome = self._verify_review(task)
        if not outcome.ok:
            # same event kind as the review-enabled loop so journal consumers
            # see the structured env_fault flag on this path too
            self.journal.append(
                "review-verify-failed",
                story_key=task.story_key,
                reason=outcome.reason,
                env_fault=outcome.env_fault,
                contradiction=outcome.contradiction,
            )
            if not outcome.retryable:
                # escalate-grade failure (environment fault, git error, a review
                # revoking the sprint sign-off): a defer would just replay it on
                # the next story — pause the run
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
        re-squashes to an equivalent-content commit, orphaning the pre-crash
        squash (harmless; "equivalent" rather than "identical" because a park
        record regenerated across midnight carries a new ``parked_at``).
        ``commit_sha`` is stamped only here and is write-only (never routing),
        so the empty persisted value is harmless."""
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
        # us, and finalize_commit's `git add -A` is still ahead, so an in-repo
        # annotation rides this story's own commit. `snapshot` is armed inside
        # the close, before its write, so both failure arms below hold the ids to
        # reopen no matter where in the window a raise lands.
        snapshot: list[_ArmedClose] = []
        park_record: tuple[Path, str | None] | None = None
        try:
            self._close_declared_deferred(task, snapshot)
            # A parked story's record rides this same commit (#356): written into
            # the workspace ahead of the `git add -A`, it reaches every clone the
            # story's commit does — including through the worktree merge-back.
            park_record = self._write_park_record(task)
            # bmad-build-auto commits its own work each iteration; the orchestrator
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
            task.dispatched_spec_file = None
            task.dispatched_spec_snapshot = None
        except verify.GitError as e:
            self._restore_deferred_closes(task, snapshot)
            self._restore_park_record(task, park_record)
            self._escalate(task, f"commit failed: {e}")
        except BaseException:
            # A failed commit is not the only way out of this window: the signal
            # handler `_run` installed raises RunStopped from wherever the main
            # thread is standing, and an OSError raised outside `_run_git` (an
            # FS fault; the spawn class arrives as GitSpawnError since #343 and
            # takes the arm above) passes through as itself. Either leaves the
            # ledger flipped — and the park record written — for a commit that
            # does not exist. Restore, then re-raise untouched.
            self._restore_deferred_closes(task, snapshot)
            self._restore_park_record(task, park_record)
            raise
        # Final-phase rule: AWAITING_OPERATOR iff the task carries actions,
        # otherwise DONE. Derived from PERSISTED task state, never from a local
        # flag, so the crash-resume arm that re-enters this method reaches the
        # same verdict the pre-crash run would have — the park latch is the only
        # thing that has to survive, and it does.
        if task.operator_actions:
            advance(task, Phase.AWAITING_OPERATOR)
            # The durable record of what a human owes: the actions themselves,
            # not just a count, because the journal and the spec frontmatter are
            # what survive the run.
            self.journal.append(
                "story-awaiting-operator",
                story_key=task.story_key,
                commit=task.commit_sha,
                actions=list(task.operator_actions),
            )
            self._notify_park(task)
        else:
            advance(task, Phase.DONE)
            self.journal.append("story-done", story_key=task.story_key, commit=task.commit_sha)
        self._emit("post_commit", task)
        self._save()

    def _park_spec_relpath(self, task: StoryTask) -> str:
        """The parked story's spec as a path relative to the WORKSPACE root, so
        it re-roots onto the project when read back.

        Under worktree isolation `task.spec_file` has been rewritten to an
        absolute path inside the unit worktree, and that worktree is torn down
        moments later — indexing it verbatim would record a path that is dead
        before the human ever reads it. Relative-to-workspace is the same string
        in both isolation modes, and `verify.resolve_spec_path` resolves it
        against the project. A spec that somehow lies outside the workspace is
        recorded as-is rather than dropped: a wrong path a human can see beats a
        missing one they cannot.

        `RuntimeError` joins the guard because `Path.resolve` reports a symlink
        loop that way below 3.13 — the same non-OSError `_ledger_in_repo` already
        catches, and `requires-python` is 3.11. Catching it HERE rather than only
        around the call is what keeps the fallback: the park record is still
        written, with `spec_file` recorded verbatim, and per #356 the record is
        load-bearing (a story key does not yield a spec path)."""
        spec_file = task.spec_file or ""
        if not spec_file:
            return ""
        try:
            return Path(spec_file).resolve().relative_to(self.workspace.root.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return spec_file

    def _write_park_record(self, task: StoryTask) -> tuple[Path, str | None] | None:
        """Write the parked story's committed record — the store `bmad-loop
        confirm` reads (#356) — returning ``(path, prior_text)`` for the restore
        arm, or None when nothing was written (not a park, or the write failed).

        Runs inside the commit window, into the WORKSPACE (the unit worktree
        under isolation, like the sprint-status mirror and the deferred-close
        annotations), so `finalize_commit`'s `git add -A` folds it into the
        story's own commit and the merge-back carries it to the target. Rooted
        at `self.workspace.paths.project`, NOT `self.paths.project`: the main
        root is the wrong tree here — a record written there would sit
        untracked beside the commit instead of inside it.

        Best-effort by design, and the only write on this path that is. The
        park itself is defined by the spec frontmatter and the board token;
        raising here would abort a story that genuinely succeeded over its
        bookkeeping, so an unwritable record degrades to a journal line — and
        `validate` reports the board-parked-but-recordless drift. The kind
        stays `operator-index-failed` so nothing greping journals re-learns a
        name. `RuntimeError` stays in the guard for `_park_spec_relpath`'s
        reason (symlink-loop `resolve` below 3.13) and as insurance: the cost
        of degrading a non-OSError here is a missing record, the cost of NOT
        catching it is an aborted commit for a finished story."""
        if not task.operator_actions:
            return None
        path = operatoractions.record_path(self.workspace.paths.project, task.story_key)
        try:
            prior = path.read_text(encoding="utf-8") if path.is_file() else None
            operatoractions.record_park(
                self.workspace.paths.project,
                task.story_key,
                actions=list(task.operator_actions),
                spec_file=self._park_spec_relpath(task),
                run_id=self.state.run_id,
                parked_at=self._today(),
            )
        except (OSError, RuntimeError) as e:
            self.journal.append("operator-index-failed", story_key=task.story_key, error=str(e))
            return None
        return (path, prior)

    def _restore_park_record(self, task: StoryTask, record: tuple[Path, str | None] | None) -> None:
        """Put the park record back the way `_write_park_record` found it, for
        the failure arms of the commit window: a `GitError` escalation or a
        pass-through raise must not leave a record — untracked, in a tree the
        next story's `git add -A` would sweep — for a commit that does not
        exist. Best-effort like `_restore_deferred_closes`, and for the same two
        reasons that arm imposes: in the `BaseException` arm an exception is
        genuinely travelling and this must not replace it, while in the
        `GitError` arm the error is already HANDLED and `_escalate` raises a
        fresh `RunPaused` — so the hazard there is preempting the escalation,
        which would strand the story in COMMITTING with no diagnosis on the
        record. Either way, this never raises.

        The put-back is atomic (#379). A torn restore leaves the record neither
        as the park wrote it nor as it was found, and `load` reads a truncated
        record as an entry owing nothing — a park silently discharged by a
        rollback of the commit it was written for.

        `OSError` stays the guard, and stays wide enough BECAUSE the put-back
        never resolves: the confined writer walks the components below the
        project root with `O_NOFOLLOW` and never calls `Path.resolve`, so the
        pre-3.13 `RuntimeError`-on-symlink-loop that forced
        `_restore_deferred_closes` (and `tui.launch`) to widen to `Exception`
        cannot arise on this path. That widening is a property of the resolve,
        not of the helper — do not copy it back here. `UnconfinedWriteError` is
        an `OSError` subclass precisely so a refusal arrives in this guard and
        gets journaled rather than escaping as a type nothing catches.

        Confined to `self.workspace.paths.project` — the WORKTREE root when one
        is mounted, matching `_write_park_record`, since that is the tree this
        record was written into — for the reason `operatoractions.record_park`
        is (#593): refusing a link at the record itself left the directories
        above it resolved by name, so a link planted at `.bmad-loop/` redirected
        the put-back out of the project entirely. `require_writable_target=True`
        (#597) keeps this writer's semantics identical to the other two writers
        of this same file; a read-only record is answered, not routed around.

        A failure is journaled rather than dropped, matching the model above:
        `validate` reports a board parked with no record but never a record left
        over for a park that is in no commit, so nothing else would ever surface
        this. The journal call is itself suppressed — a restore that must not
        raise cannot be allowed to raise on the way to saying it failed."""
        if record is None:
            return
        path, prior = record
        try:
            if prior is None:
                path.unlink(missing_ok=True)
                parent = path.parent
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            else:
                atomic_write_text_confined(
                    path,
                    prior,
                    confine_root=self.workspace.paths.project,
                    require_writable_target=True,
                )
        except OSError as e:
            with contextlib.suppress(Exception):
                self.journal.append(
                    "park-record-rollback-failed",
                    story_key=task.story_key,
                    record=str(path),
                    # named, not bare `str(e)`: an errno message alone cannot say
                    # whether the disk or the path was at fault. Matches
                    # `deferred-close-rollback-failed`.
                    error=f"{e.__class__.__name__}: {e}",
                )

    def _notify_park(self, task: StoryTask) -> None:
        """Tell the human a story is waiting on them, and exactly what for.

        Notify-only: a park is non-blocking by design, so unlike an escalation it
        must not read as "the run stopped for you". The run has already moved on.
        The actions are enumerated in the body rather than counted because this
        notification is the one artifact that reaches someone who is not looking
        at the repo."""
        actions = "\n".join(f"  {i}. {a}" for i, a in enumerate(task.operator_actions, 1))
        gates.notify(
            self.policy,
            self.run_dir,
            f"story awaiting operator: {task.story_key}",
            f"committed, but {len(task.operator_actions)} action(s) are owed outside the repo:\n"
            f"{actions}\n"
            f"run `bmad-loop confirm {task.story_key}` once they are done.",
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

    def _dev_skill(self, role: str = "dev") -> str:
        """The dev-primitive skill NAME to spell in ``role``'s session prompt.

        Upstream renamed the primitive ``bmad-dev-auto`` → ``bmad-build-auto``
        (BMAD-METHOD #2651), so the invoked name is resolved from what is
        actually on disk rather than hardcoded: a target project can be on
        either era. This is NOT ``policy.dev.skill`` — that stays the adapter
        discriminator ``_generic_dev`` reads; only the spelled name moves.

        Resolution is per skill tree because one run can mix them (dev=claude →
        ``.claude/skills``, review=codex → ``.agents/skills``), and memoized
        because every prompt build would otherwise re-stat the tree. An adapter
        with no ``profile`` (test fakes) yields tree None, which
        ``dev_primitive_or_default`` maps to the legacy name.

        Resolved against the WORKSPACE, never the main checkout: the session runs
        with ``cwd=self.workspace.root``, so the tree deciding whether the spelled
        name is a command at all is the worktree's. The two agree on a freshly
        provisioned unit — ``provision_worktree`` copies the primitive in from the
        main repo — but NOT on resume: ``reopen_unit`` re-mounts an existing
        worktree without re-provisioning it, so a main checkout upgraded across the
        pause would resolve the new name into a worktree carrying only the legacy
        one, and the session HALTs on an unknown command having written nothing.
        The cache is keyed on the workspace root for the same reason: one Engine
        drives every unit of a run, so a resumed unit's worktree must not answer
        for the fresh worktrees mounted after it."""
        adapter = self.adapters.get(role)
        tree = getattr(getattr(adapter, "profile", None), "skill_tree", None)
        project = self.workspace.paths.project
        if (project, tree) not in self._dev_skill_cache:
            self._dev_skill_cache[project, tree] = dev_primitive_or_default(project, tree)
        return self._dev_skill_cache[project, tree]

    def _operator_park_enabled(self) -> bool:
        """Whether a dev session may park a story at ``awaiting-operator`` (#335).

        Sprint mode only. Stories mode has no sprint board, so the pair the
        verify gates hold a park to does not exist there
        (``verify_review_stories`` still demands ``done``); sweep bundles carry
        no board entry either, and a deferred-work bundle owing a human action is
        a different question from a story owing one. Both subclasses override
        this to False rather than inheriting a path their verify tail cannot
        gate — support for either is a follow-up, not an accident of where the
        branch happens to sit."""
        return self.policy.operator.enabled

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
        raises."""
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
        frontmatter. ``bmad-build-auto`` sometimes appends a terminal
        ``## Auto Run Result`` (``Status: done``) yet leaves the frontmatter
        ``status`` at the template default. The orchestrator reads ONLY
        frontmatter, so without this the sprint sync and accepted bundle close
        no-op and the verify gate falsely defers completed, tested work.

        When (and only when) the prose terminal Status is ``done`` AND the
        frontmatter sits at a reconcilable non-terminal status, advance the
        frontmatter to the success status the skill should have set. This includes
        the transient ``in-review`` marker, which on the generic path is never a
        deliberate terminal (the legacy review-handoff fork is retired). Never
        reconciles ``blocked`` (it must still route to PAUSE) and never overrides
        an already-``done`` or unknown frontmatter status. Idempotent and
        never-regress: every deterministic verify gate still runs afterward against
        real on-disk/git state, so this repairs bookkeeping only — it cannot pass
        uncompleted work. Runs ahead of ``_post_dev_state_sync`` and the later
        ``_post_dev_accepted_sync`` so the story board, bundle ledger, and verify
        all read the reconciled spec."""
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
        fm = self._observed_frontmatter(spec_path, task.story_key, "reconcile")
        if fm is None:
            return
        self._reconcile_generic_terminal_frontmatter(task, result_json, spec_path, fm)

    def _reconcile_generic_terminal_frontmatter(
        self,
        task: StoryTask,
        result_json: dict | None,
        spec_path: Path,
        fm: dict,
    ) -> str:
        """Apply generic terminal reconciliation from an already-read mapping.

        The bookkeeping path supplies an observed mapping; required deferral
        harvesting supplies a strict one. Sharing the decision/write step lets a
        recovered harvest read finish a reconciliation whose earlier observation
        faulted, without trusting result JSON or launching a replacement session
        over the first session's findings. Returns the effective status.
        """
        success_status = "in-review" if self._dev_review_enabled() else "done"
        # A blank `status:` reads "" here (status_of normalizes YAML-null), so the
        # blank-status template shape reconciles like a missing key does.
        fm_status = verify.status_of(fm)
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
            return success_status
        if fm_status not in devcontract.RECONCILABLE_FROM:
            return fm_status  # blocked / unknown custom status: never override a deliberate one
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError as e:
            self._journal_spec_read_failed(spec_path, task.story_key, "reconcile-prose", e)
            return fm_status
        arr = devcontract.parse_auto_run_result(text)
        if not arr.present or arr.status != devcontract.DONE:
            return fm_status  # no terminal prose, or blocked: leave for escalation
        # Repair-write doctrine: the False arm is "nothing to change" only. A status
        # the reader can see but no line edit can move raises instead, and that raise
        # is deliberately left uncaught (see _reset_spec_for_repair).
        if not devcontract.reset_spec_status(
            spec_path, success_status, confine_root=self.workspace.paths.project
        ):
            return fm_status
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
        return success_status

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
        fm_status = verify.status_of(fm)
        rj_status = str(rj.get("status", "")).strip().lower()
        # Same terminal set `devcontract.is_frontmatter_candidate` scans for: that
        # scan is what FINDS a marker-less finalized spec, and this repair is what
        # brings it back into contract. A status accepted by one and refused by
        # the other would leave a park harvested but permanently un-markered.
        terminal = (devcontract.DONE, devcontract.BLOCKED, devcontract.AWAITING_OPERATOR)
        if fm_status not in terminal or fm_status != rj_status:
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
            repaired = devcontract.append_auto_run_result(
                spec_path,
                fm_status,
                confine_root=self.workspace.paths.project,
                detail=detail,
            )
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

        For a story that is sprint-status: the decoupled ``bmad-build-auto`` skill
        knows nothing of the bmad_loop's sprint board, so the orchestrator writes
        it — and must do so
        before ``verify_dev`` checks the sprint stage. Mirrors ``verify_dev``:
        advance the story to the sprint stage matching the spec status the skill
        actually reached, so a failed or blocked session (spec not at the success
        status) never advances the sprint. No-op for the legacy path.

        This runs above the artifact gate because ``verify_dev`` reads the board it
        writes. That ordering does not generalize to bookkeeping a gate does not
        consume: ``SweepEngine`` makes this a no-op and closes bundle ledger entries
        from ``_post_dev_accepted_sync`` instead.

        Each advance also records ``board_advance_intended`` for
        ``_carry_board_advance`` (#350). The board written here is
        ``self.workspace.paths``', which under isolation is the unit worktree's copy
        — seeded and shielded for a gitignored board, so it never rides the merge —
        and the record is what lets the post-merge carry re-apply the same stage to
        the main checkout.

        The requested TARGET, never ``advance``'s return: that return is the status
        the board LANDED at, which a never-regress echo makes equal to the CURRENT
        status rather than to the intent. They can diverge only when the board was
        already at or past ``target``, and ``verify_dev`` reads that same board
        immediately after and rejects the attempt for the mismatch — so an attempt
        whose record and board disagree never reaches integration.

        Unconditional and latest-wins. Both writes sit inside the ``_generic_dev()``
        arm of the one method ``SweepEngine`` and ``StoriesEngine`` override to a
        no-op, so a run type with no sprint board cannot leave a value here and the
        record IS the carry's guard — no second predicate downstream. Re-entering
        this on a DEV_VERIFY replay re-records the same value, and an attempt that
        ends at the other terminal overwrites it, because the phase that selects the
        terminal is the phase that writes the record.

        Not saved here, deliberately: the write it describes lands in a unit
        worktree that a host loss discards whole, and the re-drive re-derives the
        intent from the spec. Only the merge makes that write survivable, and every
        path to a merge persists the task before reaching it."""
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
        status = verify.status_of(fm)
        # A park is the other terminal a session can reach, so it gets the same
        # mirror: the board moves to `awaiting-operator`, which sits immediately
        # below `done` in STATUS_ORDER and is therefore an ordinary FORWARD
        # advance through the sole writer — no exception to never-regress, and
        # `bmad-loop confirm` later advances the same board the same way. Mirrored
        # on the STATUS alone, before the actions are validated: verify_dev owns
        # the "declared nothing" retry, and its feedback reads far better against
        # a board that already agrees with the spec than against one two stages
        # behind it.
        if self._operator_park_enabled() and status == verify.AWAITING_OPERATOR:
            sprint_advance(
                self.workspace.paths.sprint_status, task.story_key, verify.AWAITING_OPERATOR
            )
            task.board_advance_intended = verify.AWAITING_OPERATOR
            return
        if status != success_status:
            return
        target = "review" if review_enabled else "done"
        sprint_advance(self.workspace.paths.sprint_status, task.story_key, target)
        task.board_advance_intended = target

    def _post_dev_accepted_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """Write bookkeeping that is valid only after a dev attempt is accepted.

        This is the accepted-only counterpart to ``_post_dev_state_sync``. It is
        called only after ``decide_dev`` returns PROCEED, not merely when
        ``outcome.ok`` is true: CRITICAL escalations preempt a passing outcome, and
        a failing verify command can replace the artifact outcome before the
        decision. The base path has nothing to write; ``SweepEngine`` closes bundle
        ledger entries here so a rejected attempt cannot leave resolved work hidden
        from later sweeps.

        Do not combine this seam with ``_close_declared_deferred``. That regular-
        story mechanism already runs at the commit boundary and its caller takes a
        snapshot and restores it on commit failure, so it already has the acceptance
        property this seam supplies for sweep bundles through a different, correct
        transaction boundary.
        """
        return

    def _finish_post_dev_accepted_sync(
        self, task: StoryTask, result_json: dict | None = None
    ) -> None:
        """Finish and durably acknowledge accepted-only bookkeeping.

        ``accepted_dev_session_index`` is persisted only for a PROCEED decision. A
        crash before or during the mode-owned write therefore replays this
        transaction without mistaking an arbitrary DEV_VERIFY park for an accepted
        attempt. The append-only record index prevents a reused attempt number from
        authorizing a later re-arm. Mode writes must be idempotent; the sweep close
        uses a stable run/task operation identity for exactly this replay.
        """
        if not self._accepted_dev_session_matches(task):
            return
        if result_json is None and task.spec_file:
            result_json = {"spec_file": task.spec_file}
        self._post_dev_accepted_sync(task, result_json)
        # Pre-snapshot runs may replay an accepted dev result carrying only the
        # old path half of attempt ownership. It can still guide rollback before
        # acceptance, but it cannot authorize later review mutation. Retire an
        # incomplete pair here; complete authority intentionally survives review
        # repair/rollback and is retired only after commit.
        if (task.dispatched_spec_file is None) != (task.dispatched_spec_snapshot is None):
            task.dispatched_spec_file = None
            task.dispatched_spec_snapshot = None
        # The attempt is accepted; no later path may restore the pre-harvest
        # ledger snapshot. Attempt-owned spec authority intentionally survives
        # through review repair/rollback and is retired only after commit.
        self._disarm_ledger_snapshot(task)
        self._save()

    def _harvest_spec_path(self, task: StoryTask, result_json: dict | None) -> Path | None:
        """Resolve the spec whose frontmatter this mode may harvest."""
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return None
        return verify.resolve_spec_path(str(spec_file), self.workspace.paths)

    def _harvest_spec_deferrals(
        self, task: StoryTask, result_json: dict | None
    ) -> VerifyOutcome | None:
        """Carry a successful dev primitive's ``deferred:`` findings into the ledger.

        BMAD-METHOD #2640 moved defer-triaged review findings from
        ``deferred-work.md`` into the spec frontmatter. The orchestrator owns the
        follow-up policy, so without this bridge the recorded findings are
        invisible to every sweep reader downstream.

        This is era-agnostic: the gates are the generic-dev seam and the field's
        presence, never the skill name resolved on disk. A pre-#2640 spec simply
        takes the no-op path. The spec must also be at the current dev success
        status, matching the append-on-success behavior of the former writer.

        Replays are idempotent even after an entry is closed. ``append_entry``
        intentionally dedupes open entries only, so the pre-scan checks the
        fingerprinted ``origin:`` plus ``source_spec:`` across entries of every
        status. The spec frontmatter is never mutated; the ledger watermark is
        sufficient and avoids unsafe YAML block-scalar surgery.

        Reading the finding source is part of this required repair. A transiently
        missing or unreadable source returns a retry outcome rather than degrading
        like optional bookkeeping observation: a later verify read must not accept
        the session while silently dropping its recorded work. Ledger writes remain
        unguarded so a failed repair write raises.
        """
        if not self._generic_dev():
            return
        spec_path = self._harvest_spec_path(task, result_json)
        if spec_path is None:
            return
        # A session supplies `spec_file`; a mode may replace its untrusted value
        # with a deterministic artifact path. Like reconcile, marker repair, and
        # declared closes, harvesting must not let any readable path outside the
        # orchestrator-owned roots steer a ledger write.
        try:
            within = verify.spec_within_roots(spec_path, self.workspace.paths)
        except (OSError, RuntimeError):
            # resolve() faulted (a symlink loop, an unreadable component):
            # containment can vouch for nothing, so refuse the same way.
            within = False
        if not within:
            self.journal.append(
                "spec-deferrals-skipped-out-of-tree",
                story_key=task.story_key,
                spec=str(spec_path),
            )
            return
        try:
            is_file = spec_path.is_file()
        except OSError as e:
            self._journal_spec_read_failed(spec_path, task.story_key, "spec-deferrals", e)
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals "
                f"({e.__class__.__name__}: {e}): {spec_path}"
            )
        if not is_file:
            return VerifyOutcome.retry(f"spec missing while harvesting deferrals: {spec_path}")
        try:
            fm = verify.read_frontmatter(spec_path)
        except OSError as e:
            self._journal_spec_read_failed(spec_path, task.story_key, "spec-deferrals", e)
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals "
                f"({e.__class__.__name__}: {e}): {spec_path}"
            )
        # The shared observation reader deliberately collapses a second missing-
        # file probe, invalid UTF-8, malformed YAML, and a non-mapping document to
        # `{}`. That is safe for status observation, whose verifier re-reads and
        # retries, but not for this required repair input: a later clean verifier
        # read could otherwise accept the session after the harvest silently
        # skipped every recorded finding. A successful spec always has at least a
        # status mapping, so this does not reject the valid pre-`deferred:` era.
        if not fm:
            self.journal.append(
                "spec-read-failed",
                story_key=task.story_key,
                spec=str(spec_path),
                site="spec-deferrals",
                error="empty or invalid frontmatter",
            )
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals "
                f"(empty or invalid frontmatter): {spec_path}"
            )
        if devcontract.DEFERRED_FIELD not in fm:
            return
        status = verify.status_of(fm)
        success_status = "in-review" if self._dev_review_enabled() else "done"
        parked = self._operator_park_enabled() and status == verify.AWAITING_OPERATOR
        if status != success_status and not parked:
            # Stories mode's explicit plan checkpoint is a verified terminal for
            # that leg, not a half-finalized implementation. Its deferred notes
            # remain in the spec until the post-checkpoint implementation pass.
            if (
                status == devcontract.PLAN_HALT_STATUS
                and (result_json or {}).get("plan_halt") is True
            ):
                return
            if status not in devcontract.RECONCILABLE_FROM:
                return
            # Reconcile's preceding bookkeeping observation may have faulted even
            # though this required read recovered. Reuse the strict mapping to
            # finish the same session's terminal-prose repair before any later
            # session can replace its `deferred:` list.
            status = self._reconcile_generic_terminal_frontmatter(task, result_json, spec_path, fm)
            parked = self._operator_park_enabled() and status == verify.AWAITING_OPERATOR
            if status != success_status and not parked:
                shown_status = status or "<blank>"
                return VerifyOutcome.retry(
                    "spec remained at reconcilable nonterminal status "
                    f"{shown_status!r} while harvesting deferrals: {spec_path}"
                )
        findings, malformed = devcontract.parse_deferred_findings(fm)
        if not findings and not malformed:
            return

        spec_name = spec_path.name
        # (origin, title, reason, location, severity), one row per entry this
        # harvest may file. The malformed loss is aggregated per spec so a bad
        # sibling never suppresses a valid finding and never disappears silently.
        pending: list[tuple[str, str, str, str | None, str | None]] = [
            (
                f"{HARVEST_ORIGIN} {finding.fingerprint}",
                finding.summary,
                finding.evidence or finding.summary,
                finding.location or None,
                finding.severity or None,
            )
            for finding in findings
        ]
        if malformed:
            self.journal.append(
                "spec-deferrals-malformed",
                story_key=task.story_key,
                spec=spec_name,
                items=malformed,
            )
            pending.append(
                (
                    f"{HARVEST_ORIGIN}-malformed "
                    f"{devcontract.harvest_fingerprint(spec_name, *malformed)}",
                    f"Unreadable `deferred:` items in {spec_name}",
                    "The dev session recorded deferred findings the orchestrator could not "
                    "parse, so they were NOT filed as entries: "
                    + "; ".join(malformed)
                    + f". Read `{spec_name}`'s frontmatter and re-file them by hand.",
                    None,
                    "low",
                )
            )

        # Persist the full intended set, not only newly-filed rows. A replay can
        # dedupe every append while a later isolation carry still needs the data.
        # Keep a stable union across a retained retry/review chain: a later pass
        # may replace the frontmatter list, but every earlier accepted finding is
        # still present in an ignored unit ledger and must survive final carry.
        current_records = [
            {
                "origin": origin,
                "title": title,
                "reason": reason,
                "location": location,
                "severity": severity,
                "source_spec": spec_name,
            }
            for origin, title, reason, location, severity in pending
        ]
        known = {
            (str(item.get("origin", "")), str(item.get("source_spec", "")))
            for item in task.harvested_deferrals
        }
        records_changed = False
        for record in current_records:
            key = (str(record["origin"]), str(record["source_spec"]))
            if key not in known:
                task.harvested_deferrals.append(record)
                known.add(key)
                records_changed = True
        if records_changed:
            # The isolation carry reads only persisted records after a hard
            # loss. Checkpoint every stable-union expansion before a ledger
            # append, including later passes where harvest_wrote_ledger is
            # already latched and its separate pre-write save will be skipped.
            self._save()

        ledger = self.workspace.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        seen = deferredwork.parse_ledger(text)
        specs: list[deferredwork.EntrySpec] = []
        deduped = 0
        for origin, title, reason, location, severity in pending:
            if any(
                deferredwork.field_line_present(entry.body, "origin", origin)
                and deferredwork.field_line_present(entry.body, "source_spec", spec_name)
                for entry in seen
            ):
                deduped += 1
                continue
            # Persist the provenance before the write. A hard host loss after
            # append but before the later dev-decision save leaves the ledger on
            # disk; replay then dedupes and cannot reconstruct authorship from a
            # newly-filed id. Pre-latching is conservative if the append itself
            # fails: excluding an unchanged path cannot create proof of work.
            if not task.harvest_wrote_ledger:
                task.harvest_wrote_ledger = True
                self._save()
            specs.append(
                deferredwork.EntrySpec(
                    title=title,
                    origin=origin,
                    location=location or "n/a",
                    source_spec=spec_name,
                    reason=reason,
                    severity=severity,
                )
            )
        # One locked read->edit->write for the whole harvest (#286/#469) rather
        # than one per finding: a concurrent mutator can no longer interleave
        # between two of this spec's own rows, and the ids stay sequential
        # because each spec is applied to the text the previous one produced.
        # The scan above already ran, and the latch above already fired, so the
        # durability ordering the comment there describes is unchanged.
        minted = deferredwork.append_entries(ledger, specs)
        filed = [dw_id for dw_id in minted if dw_id is not None]
        # The writer's open-entry guard can catch two frontmatter items with
        # the same clamped fingerprint inside this one pre-scan snapshot.
        deduped += sum(1 for dw_id in minted if dw_id is None)
        # The flag is set-only within an attempt and was latched durably before
        # the first append. A crash replay dedupes to an empty `filed` list while
        # the dead attempt's engine-authored ledger diff is still on disk, so it
        # must never be cleared from the return values here.
        self.journal.append(
            "spec-deferrals-harvested",
            story_key=task.story_key,
            spec=spec_name,
            dw_ids=filed,
            deduped=deduped,
            malformed=len(malformed),
        )

    def _manifest_closes_deferred(self, task: StoryTask) -> tuple[str, ...]:
        """Deferred-work ids declared for this story by a *manifest* the
        orchestrator reads, as opposed to the story spec's own frontmatter.

        Empty here: sprint mode has no manifest, so frontmatter is its only
        channel. ``StoriesEngine`` overrides this with its ``stories.yaml``
        entry — the channel that matters for an unattended run, since the spec is
        generated later by a dev skill that knows nothing of the ledger."""
        return ()

    def _declared_deferred_ids(self, task: StoryTask) -> tuple[str, ...]:
        """The ids this story declares it closes, unioned across both channels
        and order-preserving (a story that names the same id in the manifest and
        in its spec must be marked once and reported once).

        Both halves are read live here, at the commit boundary: what the spec
        and the manifest say at the moment of the close is what the story
        declares — a declaration edited after the dev artifacts verified must
        not be closed against a stale snapshot, because the stale half of that
        can close an entry the final spec no longer names.

        Every degraded reading declares nothing, which is the safe direction
        for an advisory annotation: the entries stay ``open`` — a miss the next
        sweep or retro re-verifies — rather than closed on evidence nobody
        could read. An unreadable spec is journaled by
        ``_observed_frontmatter``; an unparseable one flattens to ``{}`` there
        like every other status gate; and a session-supplied path outside the
        orchestrator's roots is refused under the same containment rule the
        frontmatter-status reconcile applies, so a surprising absolute path can
        never steer a ledger write."""
        ids: list[str] = list(self._manifest_closes_deferred(task))
        spec_path = Path(task.spec_file) if task.spec_file else None
        if spec_path is not None:
            try:
                within = verify.spec_within_roots(spec_path, self.workspace.paths)
            except (OSError, RuntimeError):
                # resolve() faulted (a symlink loop, an unreadable component):
                # containment can vouch for nothing, so refuse the same way.
                within = False
            if not within:
                self.journal.append(
                    "deferred-close-skipped-out-of-tree",
                    story_key=task.story_key,
                    spec=str(spec_path),
                )
            else:
                fm = self._observed_frontmatter(spec_path, task.story_key, "deferred-close")
                declared, error = deferredwork.parse_declaration((fm or {}).get("closes_deferred"))
                if error:
                    self.journal.append(
                        "deferred-close-malformed",
                        story_key=task.story_key,
                        spec=str(spec_path),
                        error=f"closes_deferred {error}",
                    )
                ids += declared
        return tuple(dict.fromkeys(ids))

    def _close_declared_deferred(
        self, task: StoryTask, snapshot: list[_ArmedClose] | None = None
    ) -> None:
        """At the commit boundary, flip every ledger entry the story declares
        via ``closes_deferred:`` to ``status: done <date>`` + a ``resolution:``
        note (#234) — the regular-story counterpart of the sweep bundle close
        at ``SweepEngine._close_bundle_ledger_when_spec_status``.

        Declaration is the only signal: closure is never inferred from a diff.

        **Advisory by contract.** The annotation is traceability for the next
        sweep or retro, never a gate: no reading of the spec, the manifest or
        the ledger can fail the story, and every degraded reading leaves the
        entries ``open`` — the direction a sweep re-verifies and a retro can
        repair. That contract is the design: closure needs no transactional
        coupling to git, because the ledger's consumers re-verify entries
        against the codebase anyway.

        **Placement.** Runs from ``_finalize_commit_phase`` — after artifact
        verification, the verify commands, every checkpoint, the review loop,
        the ``pre_commit_gate`` workflows and the ``pre_commit`` veto — and
        still before ``finalize_commit``, whose ``git add -A`` stages an
        in-repo annotation into the story's own commit. Marking at dev-sync
        time instead let a story that later failed verification or review leave
        the ledger permanently claiming its work resolved.

        **In-repo is not stageable, and under isolation that is the difference
        between a delivered close and a lost one (#458).** This paragraph used
        to promise the opposite — "worktree isolation included: the unit's
        ledger rides the unit commit and reaches the target branch with the
        ordinary merge" — which holds only for a TRACKED ledger. A gitignored
        one (the default shape, since the ledger lives under the BMAD artifacts
        dir) is skipped by that ``git add -A`` in silence, brings nothing over
        with the merge, and dies with the worktree, while this method has
        already journaled ``story-deferred-closed``. ``story_closes_intended``
        + ``_carry_story_deferred_closes`` is the delivery path; ``_ledger_in_repo``
        below is a containment test and answers a different question.

        A ledger outside the repo (``ProjectPaths.rebased`` deliberately
        shares an external artifact dir between worktrees) is written at the
        same moment; the annotation is simply part of no commit, which is
        journaled (``deferred-close-external-ledger``). A merge that later
        fails leaves that annotation standing — visible, journaled and
        human-attended, the accepted advisory trade-off.

        ``snapshot``, when given, receives ``(ledger, pre-close text)`` the
        statement before the write, so the caller's failure arms can undo the
        edit no matter where inside the window a raise lands — including a
        journal append failing after the write published. Cleared again when
        nothing was flipped: an empty close needs no restore.

        Idempotent, so the COMMITTING resume arm may re-drive the commit phase
        freely — ids are classified against the ledger text, and an entry
        already ``done`` is a satisfied declaration, not an unmatched one."""
        ids = self._declared_deferred_ids(task)
        # Reset before the early return, never after it. This method is the sole
        # producer of the record and runs unconditionally at every commit boundary,
        # and DONE/AWAITING_OPERATOR are reachable only through the caller — so a
        # re-drive that reaches the carry has always re-entered here first, and this
        # assignment IS the staleness guard (a `_dev_phase` clear like
        # `refiled_followups`' would be a second branch saying the same thing, which
        # no test could redden). The live read is authoritative: a resolve session
        # that WITHDREW `closes_deferred:` must not have the abandoned attempt's
        # declaration carried on its behalf.
        task.story_closes_intended = []
        if not ids:
            return
        ledger = self.workspace.paths.deferred_work
        try:
            text = ledger.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Nothing at the path — but a dangling link IS something: the link
            # names a ledger on a mount that is not answering, and blaming a
            # typo (`unmatched`) would misreport an outage.
            try:
                dangling = ledger.is_symlink()
            except OSError:
                dangling = True
            if dangling:
                self._journal_ledger_unavailable(task, ids, ledger, "dangling symlink")
                return
            text = ""  # no ledger: classify reports every id unmatched; nothing is written
        except (OSError, UnicodeDecodeError) as e:
            # An unavailable or undecodable location is not an answer about the
            # entries: close nothing and say so, rather than read an outage as
            # "no such entries".
            self._journal_ledger_unavailable(task, ids, ledger, f"{e.__class__.__name__}: {e}")
            return
        # Classified ONCE, here, and handed down: the arm below and the write in
        # `_apply_deferred_closes` have to name the same set, and a second
        # `classify` over the same text would only make that a coincidence rather
        # than a fact. The "one document" contract the write already kept now
        # covers the rollback too.
        declared = deferredwork.classify(text, ids)
        if snapshot is not None:
            # Armed BEFORE the write, with the INTENDED set (#284): a raise inside
            # the close itself must still be undoable, and `mark_open_many` skips an
            # id that never flipped, so an over-broad arm costs nothing. `exact` is
            # False precisely because these ids are a plan, not an outcome — the
            # unmatched journal below must not fire for an id the write never
            # reached.
            snapshot.append(_ArmedClose(ledger, declared.open_ids, False))
        # The DECLARED set, never `marked` — the transposed lesson of `e88776a`. A
        # host loss in this window resumes into `_finalize_commit_phase` again, and
        # by then the worktree ledger already reads `done`, so `classify` returns
        # every id as `already_done` and `marked` is EMPTY. A record derived from it
        # would never be made on that replay, and the carry would find nothing while
        # `close_unit_workspace` deletes the only copy. Recorded after the snapshot
        # arm so the degraded read paths above, which write nothing, record nothing.
        #
        # No `_save()` here, unlike the follow-up producer: every crash path re-enters
        # this method, which re-derives the ids from the spec and the manifest. A
        # persisted record would instead SURVIVE a declaration edited across the
        # crash, which is the stale snapshot `_declared_deferred_ids` reads live to
        # avoid.
        task.story_closes_intended = list(ids)
        marked = self._apply_deferred_closes(task, declared, ledger)
        if snapshot is not None:
            if marked:
                # Narrow the plan to the outcome. `exact` from here on: every id
                # carries an undo marker this method wrote moments ago, so one that
                # will not reopen has had it displaced by somebody else.
                snapshot[-1] = _ArmedClose(ledger, tuple(marked), True)
            else:
                # The close writes only when it marks: the ledger is byte-identical,
                # so a restore would record a rollback of nothing.
                snapshot.clear()
        if marked and not self._ledger_in_repo(ledger):
            self.journal.append(
                "deferred-close-external-ledger",
                story_key=task.story_key,
                dw_ids=list(marked),
                ledger=str(ledger),
                note="ledger is outside the repo; the annotation is not part of any commit",
            )

    def _journal_ledger_unavailable(
        self, task: StoryTask, ids: Sequence[str], ledger: Path, error: str
    ) -> None:
        self.journal.append(
            "deferred-close-ledger-unavailable",
            story_key=task.story_key,
            dw_ids=list(ids),
            ledger=str(ledger),
            error=error,
        )

    def _ledger_in_repo(self, ledger: Path) -> bool:
        """Whether the ledger's annotation rides the story's commit. Decided on
        RESOLVED paths: an in-repo symlink to a shared external ledger must
        count as external. A path that cannot be resolved reports as external —
        the write has already happened either way, and "part of no commit" is
        the claim that stays true when nothing else is known."""
        try:
            return ledger.resolve().is_relative_to(self.workspace.root.resolve())
        except (OSError, RuntimeError):
            return False

    def _restore_deferred_closes(self, task: StoryTask, snapshot: list[_ArmedClose]) -> None:
        """Undo the closes ``_close_declared_deferred`` wrote, after the commit
        they were written for failed (#234): left alone the entries read ``done``
        for work that is in no commit, and the likeliest recovery makes that
        permanent — a human-resolved re-drive sets ``resolved_redrive``, which has
        ``safe_reset`` preserve the artifact folders' tracked content through the
        rollback.

        **Entry-scoped, through the closes' own undo markers (#286).** This used to
        rewrite the whole document from the pre-close text and call the collateral
        an accepted advisory trade-off: within the commit window the engine was
        held to be the only writer worth knowing about, so whatever anyone else had
        added was restored away with the close. That contract is overturned. The
        window spans `finalize_commit`'s git spawns and, on the escalation leg, an
        operator-blocking pause, so a second orchestrator process, a sweep, the TUI
        decision modal or `sweep --archive` can and does write inside it — and a
        lock cannot be held across a window shaped like that (#286's own acceptance
        criterion). So the rollback reopens exactly the armed ids through the
        operation-specific markers ``mark_done_many_reopenable`` wrote, in ONE
        locked read-edit-write: a concurrent append, an unrelated close, a recorded
        human decision are each left standing, and a row this run never closed is
        never touched.

        An armed id that will not reopen is reported, never worked around. It only
        means anything for an ``exact`` arm — one narrowed to the ids actually
        marked — where the marker was on disk moments ago: something has since
        broken the ``resolution:``/``resolution-undo:`` adjacency the undo matches
        on (a foreign ``decision:`` line inserted after the status line does
        exactly this), so the entry stays ``done`` and the foreign content is
        preserved, with ``deferred-close-reopen-unmatched`` naming the ids. A
        pre-write arm carries the INTENDED set instead, where an id that never
        flipped is an ordinary silence rather than a signal, and nothing is
        reported.

        Advisory itself, twice over: a failed restore is journaled, never
        raised, and the journaling is suppressed rather than allowed to become
        the crash — this runs inside except arms whose exception must travel
        unchanged. Skipping the `raise` would replace a `RunStopped` with a
        symlink complaint; skipping the `GitError` arm's `_escalate` would
        strand the story in COMMITTING with no diagnosis on the record.

        The guard is type-agnostic on purpose, and `OSError` is not wide enough
        to hold it: the write under `mark_open_many` resolves the path before its
        own try, and below 3.13 `Path.resolve` reports a symlink loop as
        `RuntimeError` — the same non-OSError `_ledger_in_repo` already catches for
        this very path. Deriving the lock's own sidecar path can raise
        `runs.StateRootError`, which is likewise no `OSError`. Catching `Exception`
        and not `BaseException` is the other half: `RunStopped` is an `Exception`,
        so a second stop signal landing inside the restore is absorbed while the
        first still travels, and a genuine KeyboardInterrupt still gets out."""
        if not snapshot:
            return
        ledger, ids, exact = snapshot[-1]
        try:
            reopened = deferredwork.mark_open_many(
                ledger,
                list(ids),
                self._story_close_note(task),
                self._story_close_operation_id(task),
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                self.journal.append(
                    "deferred-close-rollback-failed",
                    story_key=task.story_key,
                    ledger=str(ledger),
                    # named, not bare `str(e)`: now that every type is admitted,
                    # a message alone cannot say whether the disk or the path
                    # was the fault. Matches `_journal_ledger_unavailable`.
                    error=f"{e.__class__.__name__}: {e}",
                )
            return
        if reopened:
            with contextlib.suppress(Exception):
                self.journal.append(
                    "deferred-close-rolled-back",
                    story_key=task.story_key,
                    ledger=str(ledger),
                    dw_ids=reopened,
                )
        failed = [i for i in ids if i not in reopened] if exact else []
        if failed:
            with contextlib.suppress(Exception):
                self.journal.append(
                    "deferred-close-reopen-unmatched",
                    story_key=task.story_key,
                    ledger=str(ledger),
                    dw_ids=failed,
                    error="the close's undo marker is gone; the entry is left done",
                )

    def _story_close_note(self, task: StoryTask) -> str:
        """Resolution note shared by the commit-boundary close and its isolation
        carry, so a carried row cannot drift from one the merge delivered."""
        return f"resolved by story {task.story_key}"

    def _story_close_operation_id(self, task: StoryTask) -> str:
        """Owner of the undo markers a story's declared closes write, shared by the
        close, its rollback and its isolation carry.

        Recomputed from already-persisted identity — never minted — so the rollback
        arm reaches the same owner the write used even across a crash and replay,
        which is `mark_done_many_reopenable`'s stated requirement of its callers.

        The ``/closes-deferred`` suffix is what keeps it disjoint from
        ``SweepEngine._bundle_close_operation_id``, which is this string's prefix
        exactly. The two never coexist on one task — a bundle has no
        ``closes_deferred:`` declaration and ``SweepEngine`` no-ops this whole hook
        — but a shared ledger holds rows from both, and an undo must not reach
        across."""
        return f"{self.state.run_id}/{task.story_key}/closes-deferred"

    def _apply_deferred_closes(
        self, task: StoryTask, declared: deferredwork.Declared, ledger: Path
    ) -> list[str]:
        """Write the closure `declared` describes, journal exactly what landed, and
        return the ids actually flipped.

        ``declared`` is classified by the caller from the ledger snapshot it already
        read, never re-read here: classification, the rollback arm and the write all
        have to describe the same document, and a second read is a second chance for
        the location to have gone away underneath them.

        The REOPENABLE close (#286): each flipped row gains a ``resolution-undo:``
        line owned by this story's close operation, which is what lets
        ``_restore_deferred_closes`` undo these entries and only these — rather than
        restoring the whole document over a concurrent writer's work. The marker is
        permanent and rides the story's own commit; the sweep bundle close has
        published the same format since #284, so this is an extension of the ledger
        format, not an invention (user decision, 2026-08-25)."""
        marked = deferredwork.mark_done_many_reopenable(
            ledger,
            declared.open_ids,
            self._today(),
            self._story_close_note(task),
            self._story_close_operation_id(task),
        )
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
        if declared.duplicates:
            # One id, two entries: only the first was classified and only the
            # first can be marked, so whatever the second says about this work is
            # neither read nor written. A corrupt ledger (#286) must not close
            # quietly — the operator has to know which id to go look at.
            self.journal.append(
                "deferred-close-duplicate-id",
                story_key=task.story_key,
                dw_ids=list(declared.duplicates),
                error="the ledger carries more than one entry for this id; only the first was read",
            )
        return marked

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

    def _verify_commands_with_results(
        self, task: StoryTask, verification_stage: str
    ) -> tuple[VerifyOutcome, VerifyCommandRecords]:
        """Execute, retain, and classify verifier results as one engine action.

        Core alone executes and classifies commands.  The returned immutable
        records are only journalled and exposed to ``post_dev_verify`` plugins.

        ``stage`` is set on the returned records whenever this method ran at all,
        including the zero-command case: "the pass ran and executed nothing" and
        "no pass ran" are different facts, and only the caller that never reaches
        here may publish the second one.
        """
        results = tuple(verify.run_verify_commands(self.policy, self.workspace.root))
        sequence = self._journal_verify_command_results(task, verification_stage, results)
        outcome = verify.verify_command_results_outcome(list(results), self.workspace.root)
        return outcome, VerifyCommandRecords(
            results=results, stage=verification_stage, sequence=sequence
        )

    def _next_verification_sequence(self, story_key: str) -> int:
        """Allocate this story's next ``verify-command-result`` sequence.

        The ordinal is a public journal field and a ``post_dev_verify``
        correlation key, so it has to stay monotonic per story ACROSS A RESUME —
        a fresh process must not restart at 1 and mint a second record claiming
        an ordinal an earlier one already used. That property is the whole reason
        this used to re-derive the ordinal by rescanning the journal on every
        verification, which read and JSON-parsed the entire file each time — a
        file this same method keeps appending to, so the cost grew with the run
        that was paying it.

        The rescan survives here, once: the first allocation of an engine's life
        seeds the per-story map from the journal, and every later one is an
        in-memory increment. One scan, not one per verification, and the resume
        property is unchanged because a resumed run's seed reads the same journal
        the rescan did.

        Seeding EVERY story in one pass (rather than lazily per story) is sound
        because :meth:`_journal_verify_command_results` is the sole writer of
        this record kind and one Engine drives every unit of a run, so after the
        seed the map — not the file — is authoritative. A nested auto-sweep is
        not an exception: a child run composes its own run dir and ``Journal``.

        Deliberately an ``Engine`` field and not a ``StoryTask`` one: the value
        is recoverable from the journal on every resume, so persisting it would
        add a ``state.json`` field that can only disagree with the record it
        duplicates. It is also NOT ``attempt`` — a human re-arm reuses attempt
        numbers, which is exactly why this counter exists beside it.
        """
        if self._verification_sequences is None:
            self._verification_sequences = self._seed_verification_sequences()
        allocated = self._verification_sequences.get(story_key, 0) + 1
        self._verification_sequences[story_key] = allocated
        return allocated

    def _seed_verification_sequences(self) -> dict[str, int]:
        """The highest sequence already journalled per story — the resume seed.

        Tolerant by design, like every other journal read-back: a truncated or
        hand-edited line that lost either key is skipped rather than raising, and
        the worst case is an ordinal reused in a run whose journal was already
        corrupt. Missing story = 0, so the first allocation is 1.
        """
        highest: dict[str, int] = {}
        for entry in self.journal.entries():
            if entry.get("kind") != "verify-command-result":
                continue
            story_key = entry.get("story_key")
            sequence = entry.get("verification_sequence")
            if isinstance(story_key, str) and isinstance(sequence, int):
                highest[story_key] = max(highest.get(story_key, 0), sequence)
        return highest

    def _journal_verify_command_results(
        self,
        task: StoryTask,
        verification_stage: str,
        results: tuple[verify.CommandResult, ...],
    ) -> int | None:
        """Record each verifier subprocess result plus bounded log pointers, and
        return the sequence they were recorded under — ``None`` when there was
        nothing to record.

        ``attempt`` and ``verification_stage`` make the public journal records
        correlate to a concrete dev or repair verification pass.  The filenames
        contain only engine-derived ordinal values; command text never becomes a
        filesystem path.  Sanitize the whole composition, not the parts, for the
        reason :func:`_session_task_id` gives: two individually capped parts can
        still compose past a filename segment limit, and ``safe_segment``'s digest
        suffix differs between the two orders.

        Retention is bounded by ``verify.stream_capture_kb`` per stream, and the
        record says so rather than leaving the reader to guess: ``*_bytes`` is
        what the command emitted, ``*_captured_bytes`` how much of that reached
        disk, ``*_truncated`` their inequality.  Both counts are UTF-8 lengths of
        the decoded stream, NOT file sizes — see :func:`_bounded_stream_tail`.  A
        zero cap writes no file at all and leaves the pointer null; the record
        still lands, still carrying the full byte count, because "nothing was
        retained" and "the command was silent" are different facts.

        This is observation, so it degrades and never raises (AGENTS.md).  An
        ``OSError`` from the write — ENOSPC, a read-only run dir, ENAMETOOLONG on
        a path this composition did not shorten enough — is journalled as
        ``capture_error`` beside a null pointer and the verification continues.
        The alternative is a lost log killing a dev pass whose commands passed,
        which trades a diagnostic for the run it was there to diagnose.

        No results means no records, and therefore no sequence: the ordinal is
        allocated only when at least one record lands, so it never runs ahead of
        the journal it indexes. That is also the pre-existing behaviour — the
        max-of-journalled rescan this replaced could not observe an ordinal it
        had not written — and keeping it is what makes a resumed run number its
        passes identically to an uninterrupted one.
        """
        if not results:
            return None
        verification_sequence = self._next_verification_sequence(task.story_key)
        max_bytes = self.policy.verify.stream_capture_kb * 1024
        for command_index, result in enumerate(results):
            stem = safe_segment(
                f"verify-{task.story_key}-"
                f"{verification_stage}-{task.attempt}-{verification_sequence}-{command_index}"
            )
            streams: dict[str, str | int | bool | None] = {}
            capture_error: str | None = None
            for kind, text, emitted in (
                ("stdout", result.stdout, result.stdout_full_bytes),
                ("stderr", result.stderr, result.stderr_full_bytes),
            ):
                tail, full_bytes, captured_bytes = _bounded_stream_tail(text, max_bytes)
                # `full_bytes` is what we still HOLD; when the in-memory ceiling
                # already cut this stream, what the command EMITTED is larger and
                # only the result knows it. Reporting the held size would quietly
                # under-report emission and, worse, could call a truncated stream
                # whole — the one thing `*_truncated` exists to prevent.
                full_bytes = full_bytes if emitted is None else emitted
                path: str | None = None
                if max_bytes > 0:
                    try:
                        path = self.journal.write_verify_stream(f"{stem}.{kind}.log", tail)
                    except OSError as exc:
                        # Nothing published: atomic_write_text removes its temp and
                        # leaves the target absent, so 0 retained is the literal truth.
                        captured_bytes = 0
                        capture_error = capture_error or f"{kind}: {exc}"
                streams[f"{kind}_path"] = path
                streams[f"{kind}_bytes"] = full_bytes
                streams[f"{kind}_captured_bytes"] = captured_bytes
                streams[f"{kind}_truncated"] = captured_bytes < full_bytes
            self.journal.append(
                "verify-command-result",
                story_key=task.story_key,
                attempt=task.attempt,
                verification_stage=verification_stage,
                verification_sequence=verification_sequence,
                command_index=command_index,
                command=result.command,
                returncode=result.returncode,
                output_tail=result.output_tail,
                capture_error=capture_error,
                **streams,
            )
        return verification_sequence

    def _resume_after_dev_verify(self, task: StoryTask) -> None:
        """Resume a task the run paused at DEV_VERIFY (dev verified, spec on disk).
        Base: the spec-approval-gate resume — run the review loop + commit.
        StoriesEngine overrides this to re-drive the implement leg of a
        plan-checkpoint-paused story (leg-2) instead."""
        self.journal.append("resume-review", story_key=task.story_key)
        self._finish_post_dev_accepted_sync(task)
        self._review_and_commit(task)

    def _after_story(self, task: StoryTask) -> None:
        """Hook fired once a story is fully processed and (under isolation)
        integrated — from _loop after _run_story and from _finish_inflight after a
        resumed task completes. Base: no-op. StoriesEngine uses it for the
        done_checkpoint pause, which must land after integration so a committed
        unit is merged before the run stops."""
        return

    def _ledger_text(self) -> str | None:
        """Return the active workspace ledger text, preserving absence."""
        ledger = self.workspace.paths.deferred_work
        return ledger.read_text(encoding="utf-8") if ledger.is_file() else None

    def _ledger_digest(self) -> str:
        """Digest the current ledger text for proof-of-work attribution.

        An absent ledger and an empty one intentionally hash alike: neither
        carries an entry, and this comparison never restores or unlinks the file.
        Reads stay fail-loud because guessing either equality answer can misjudge
        the session's work.
        """
        return _digest_of(self._ledger_text())

    def _legacy_ledger_changed_before_harvest(self, task: StoryTask) -> bool:
        """Recover pre-feature ledger attribution from the attempt's Git baseline.

        Old resumable state has no ``baseline_ledger_digest``. The completed
        session can still have authored ledger-only work, and path-granular Git
        evidence distinguishes that existing diff from the engine harvest about
        to run. External ledgers cannot satisfy the project proof gate. An
        attribution probe that raises keeps the path excluded, so uncertainty
        never credits the engine's own append as session work.
        """
        if not task.baseline_commit:
            return False
        ledger = self.workspace.paths.deferred_work
        root = self.workspace.root
        try:
            rel = ledger.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, RuntimeError):
            try:
                rel = ledger.relative_to(root).as_posix()
            except ValueError:
                return False
        except ValueError:
            return False
        try:
            return verify.path_changed_since(
                root,
                task.baseline_commit,
                rel,
                baseline_untracked=task.baseline_untracked,
            )
        except (verify.GitError, OSError, RuntimeError) as e:
            self.journal.append(
                "legacy-ledger-attribution-failed",
                story_key=task.story_key,
                error=str(e),
            )
            return False

    def _ledger_is_gits_to_restore(self, task: StoryTask) -> bool:
        """Whether git owns the active ledger and reset is responsible for it.

        Probe failures degrade toward keeping the file: uncertainty must never
        authorize deleting a tracked ledger that ``reset --hard`` restored.
        """
        ledger = self.workspace.paths.deferred_work
        root = self.workspace.root
        try:
            rel = ledger.relative_to(root).as_posix()
        except ValueError:
            try:
                rel = ledger.resolve().relative_to(root.resolve()).as_posix()
            except (OSError, RuntimeError) as e:
                self.journal.append(
                    "ledger-scope-probe-failed",
                    story_key=task.story_key,
                    error=str(e),
                )
                return True
            except ValueError:
                # An external ledger was outside the reset's reach. A None
                # snapshot means this harvest created it, so it remains ours to
                # unlink.
                return False
        try:
            return verify.path_tracked(root, rel)
        except (verify.GitError, OSError, RuntimeError) as e:
            self.journal.append(
                "ledger-tracked-probe-failed",
                story_key=task.story_key,
                error=str(e),
            )
            return True

    def _restore_ledger(self, task: StoryTask, snapshot: str | None) -> None:
        """Restore the active ledger to a pre-harvest filesystem snapshot."""
        ledger = self.workspace.paths.deferred_work
        if self._ledger_text() == snapshot:
            return
        if snapshot is None:
            # The harvest created an untracked/ignored ledger. A tracked ledger
            # absent at snapshot time is different: reset restored its committed
            # bytes, which must never be deleted here.
            if not self._ledger_is_gits_to_restore(task):
                ledger.unlink(missing_ok=True)
            return
        ledger.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(ledger, snapshot)

    def _restore_persisted_ledger(self, task: StoryTask, *, replayed: bool) -> None:
        """Restore the snapshot durably armed before this attempt's engine writes."""
        if not task.pre_harvest_ledger_captured:
            if replayed:
                self.journal.append("ledger-snapshot-missing", story_key=task.story_key)
            return
        self._restore_ledger(task, task.pre_harvest_ledger)

    def _disarm_ledger_snapshot(self, task: StoryTask) -> None:
        """Drop the chain-scoped pre-harvest ledger snapshot."""
        task.pre_harvest_ledger = None
        task.pre_harvest_ledger_captured = False

    def _harvest_gate_exclude(self, task: StoryTask) -> tuple[str, ...]:
        """Exclude only this attempt's engine-authored ledger append from proof of work.

        ``_harvest_spec_deferrals`` runs above the artifact gate. Without this
        exclusion a session that wrote no code can pass because the orchestrator
        itself changed the ledger. The persisted flag survives crash replay, when
        the repeated harvest dedupes and reports no newly-filed ids.

        The exclusion is path-granular, so it stands down when the ledger had
        already changed from the current attribution reference before harvesting. That preserves
        a session-authored ledger-only change as valid work even when the same
        attempt also records a frontmatter deferral. Standing down exposes more
        of the tree to the gate, which is the conservative direction.
        """
        if not task.harvest_wrote_ledger or task.ledger_changed_before_harvest:
            return ()
        paths = self.workspace.paths
        try:
            rel = paths.deferred_work.resolve().relative_to(paths.project.resolve())
        except ValueError:
            # The proof-of-work gate only sees the project tree, so an external
            # ledger cannot satisfy it and needs no exclusion.
            return ()
        except (OSError, RuntimeError):
            # ProjectPaths are normalized when loaded. If filesystem resolution
            # nevertheless faults, keep a lexically in-project ledger excluded:
            # uncertainty must not turn the engine's append into session proof.
            try:
                rel = paths.deferred_work.relative_to(paths.project)
            except ValueError:
                return ()
        return (rel.as_posix(),)

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev(
            task,
            self.workspace.paths,
            result_json,
            review_enabled=self._dev_review_enabled(),
            operator_park=self._operator_park_enabled(),
            engine_written=self._harvest_gate_exclude(task),
        )

    def _verify_review(self, task: StoryTask):
        # `not _dev_review_enabled()` is exactly the case where _post_dev_state_sync
        # targeted "done" and verify_dev asserted the board got there, so a board
        # now short of done is a review revoking that sign-off, not a stage never
        # reached (#334).
        return verify.verify_review(
            task,
            self.workspace.paths,
            self.policy,
            sprint_reached_done=not self._dev_review_enabled(),
            operator_park=self._operator_park_enabled(),
        )

    def _review_prompt(self, task: StoryTask) -> str:
        # Re-invoking bmad-build-auto on a `done` spec resets review_loop_iteration
        # and routes to step-04 for a fresh independent review pass (BMAD-METHOD
        # #2508) — so the follow-up review is just another dev-skill run, no
        # separate review skill. task.spec_file is set by verify_dev on success.
        # The ledger instruction is the prevention side of the reclose in
        # SweepEngine._verify_review: a review that rewrites deferred-work.md
        # from a stale snapshot clobbers orchestrator-recorded closures.
        # Existing entries are orchestrator-owned; new ones are simply not asked
        # for. Post-BMAD-METHOD#2640 the primitive records its own `defer`
        # findings in the spec frontmatter and `_harvest_spec_deferrals` files
        # them. An affirmative append instruction would therefore file each
        # finding twice. Keep this neutral for pre-#2640 skills, whose flat append
        # may still be the only record. `tests/test_engine.py` pins the ban as
        # `"append" not in prompt.lower()` over the WHOLE assembled prompt, board
        # clauses included — nothing injected below may spell that substring.
        #
        # Two separators live here, both plain spaces because every clause ends in
        # a full stop and the dev seam's em dash would render a `. — `: the
        # caller-owned one at the ledger↔board seam, and the join at the
        # board↔redirect seam. The `if tail else ""` guard is live, not defensive —
        # `StoriesEngine` inherits this method and empties both clauses.
        clauses = [
            c for c in (self._sprint_board_instruction(), self._board_handback_redirect()) if c
        ]
        tail = " ".join(clauses)
        return (
            f"/{self._dev_skill('review')} {task.spec_file} — do NOT modify, "
            f"re-open, or rewrite existing deferred-work ledger entries; the "
            f"orchestrator owns their status and resolution."
        ) + (f" {tail}" if tail else "")

    def _render_commit_template(self, task: StoryTask) -> str | None:
        """The configured commit message template with {story_key}/{run_id}/
        {story_title} substituted, or None when no template is set. Used by both
        the story and sweep-bundle commit paths so a filled-out template wins
        everywhere."""
        template = self.policy.scm.commit_message_template.strip()
        if not template:
            return None
        # literal substitution (not str.format) so stray braces in the
        # template — e.g. a JSON trailer — don't raise. The spec read behind
        # {story_title} is skipped entirely for templates that don't ask for it.
        title = self._story_title(task) if "{story_title}" in template else ""
        # {story_title} substituted LAST: it is the only value here drawn from
        # agent-written spec prose, so a title that itself contains "{run_id}"
        # must land in the message as written instead of being re-substituted.
        return (
            template.replace("{story_key}", task.story_key)
            .replace("{run_id}", self.state.run_id)
            .replace("{story_title}", title)
        )

    def _story_title(self, task: StoryTask) -> str:
        """The story's human-readable title for {story_title}: the spec's
        ``title:`` frontmatter, falling back to a first **ATX** H1, then to the
        story key. Any leading ``Story <id>:`` label is dropped either way (the
        template already carries the key, so the label would just repeat it).

        Frontmatter first because that is where a bmad-loop spec's title
        actually lives — `spec-template.md` opens with ``title:`` and writes no
        H1 at all. Keying on the heading alone left the placeholder inert on
        every canonical spec, silently rendering the story key in place of a
        title. The H1 branch stays for specs authored outside that template.

        ATX only, deliberately: the setext form (a line underlined by ``===``)
        is a valid CommonMark H1, but recognizing it would make *any* prose line
        sitting above a ``===`` divider the commit subject. That trades this
        method's one safe failure mode — falling back to the story key — for a
        confidently wrong title, so the narrower contract is the right one and
        this docstring is the place it is stated.

        Falls back to the story key when there is no spec, no title, or the spec
        is unreadable — the placeholder must never render empty, and a
        commit-time read failure must not fail the commit."""
        if not task.spec_file:
            return task.story_key
        spec = Path(task.spec_file)
        try:
            # `read_frontmatter` already degrades a missing, undecodable or
            # malformed-YAML spec to {}; the guard below is for the reads that
            # still raise past it (EACCES here, a torn multi-byte spec in the
            # H1 fallback's own read_text).
            title = _story_label_stripped(
                verify.read_frontmatter(spec).get("title"), task.story_key
            )
            if title:
                return title
            lines = spec.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError is a ValueError, not an OSError, so a spec torn
            # mid-write through a multi-byte sequence would slip past an
            # except-OSError guard. This render happens before
            # _finalize_commit_phase's try, so an escape crashes the run rather
            # than escalating the story.
            return task.story_key
        # Skip a leading YAML frontmatter block (standalone --- delimiter lines,
        # same rule as frontmatter._split_frontmatter) so a YAML comment inside
        # it can't be mistaken for the H1.
        start = 0
        if lines and lines[0].rstrip() == "---":
            for i in range(1, len(lines)):
                if lines[i].rstrip() == "---":
                    start = i + 1
                    break
        # Fenced blocks are skipped, because `#` opens a comment in most of the
        # languages a spec quotes: a setup snippet whose first line is
        # "# Install the dependencies" is not this story's heading, and taking it
        # would render a confidently wrong commit subject rather than falling
        # back. An unclosed fence deliberately swallows the rest of the file
        # (CommonMark says it runs to EOF) — resetting at EOF to "rescue" a
        # heading would resurrect exactly the bug this skip exists to prevent.
        fence_char, fence_len = "", 0
        for line in lines[start:]:
            # CommonMark allows up to three spaces of indentation before an ATX
            # heading; a fourth makes the line an indented code block, so that is
            # the bound rather than a plain lstrip. Getting this wrong only ever
            # costs a silent fall back to the story key, which is the same way
            # the placeholder was inert on every canonical spec before it read
            # `title:` — so the extraction stays as permissive as the syntax is.
            head = line.lstrip(" ")
            # A fence opens on 3+ backticks or tildes and closes only on a run of
            # the SAME character, at least as long, with nothing but whitespace
            # after it — so a ``` inside a ```` block does not close it early.
            run = 0
            if len(line) - len(head) <= 3 and head[:1] in ("`", "~"):
                run = len(head) - len(head.lstrip(head[0]))
            if fence_char:
                if head[:1] == fence_char and run >= fence_len and not head[run:].strip():
                    fence_char, fence_len = "", 0
                continue
            if run >= 3:
                fence_char, fence_len = head[0], run
                continue
            # The opener is `#` followed by a space OR A TAB — `#Title` is not a
            # heading at all, and `## Title` is an H2, so both stay rejected.
            if len(line) - len(head) <= 3 and head[:1] == "#" and head[1:2] in (" ", "\t"):
                # An ATX heading may carry an optional closing run of hashes,
                # which is syntax rather than title ("# Wire it ###" is "Wire
                # it"). Unlike the two misses above this one renders a *wrong*
                # subject rather than falling back, so it is the one worth
                # stripping. Whitespace before the run is what makes it a
                # closing sequence; "# C#" keeps its hash.
                title = _story_label_stripped(_ATX_CLOSING_RE.sub("", head[2:]), task.story_key)
                if title:
                    return title
                break
        return task.story_key

    def _commit_message(self, task: StoryTask) -> str:
        # The park suffix is appended to a rendered template too. The template
        # governs the message's SHAPE; whether the story is finished is a fact
        # about the commit, and `git log` is where an operator looks for it long
        # after the notification scrolled past. A plugin that wants the suffix
        # gone can still rewrite the whole message at pre_commit.
        return self._base_commit_message(task) + (
            " (awaiting operator)" if task.operator_actions else ""
        )

    def _base_commit_message(self, task: StoryTask) -> str:
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
        # lost-session diagnosis (#489): rides the same chokepoint so EVERY
        # role — dev, review, fix, migration, triage, injected workflows —
        # leaves the greppable record, not only the dev decision. The boolean
        # inherits the probe's weak False (`TerminalMultiplexer.has_session`):
        # a lookup the backend failed for any reason counts as vanished,
        # accepted because the window-death verdict proved the transport
        # healthy moments before the probe asked.
        if result.session_vanished:
            extras["session_vanished"] = True
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
        preserve_dispatched_spec_snapshot: bool = False,
    ) -> SessionResult:
        # ``label`` names a non-standard session (a plugin-provided workflow) so
        # its task_id stays distinct from the role's own dev/review attempts.
        task_id = _session_task_id(task.story_key, label if label else role, seq)
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        env = {
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(self.run_dir),
            # Where this session's hook relay writes its events (#494). The one
            # required producer site: every engine-driven session — dev/review,
            # sweep bundles, stories, injected plugin workflows — is dispatched
            # through this dict. The deliberate non-sites all fail closed without
            # it: `resolve.py` sets no BMAD_LOOP_TASK_ID, so the relay no-ops and
            # an interactive resolve session produces no events at all; `probe.py`
            # captures through BMAD_LOOP_PROBE_CAPTURE_DIR and its own probe relay;
            # `plugins/bus.py` spawns plain shells with no task id; the Unity
            # plugin's helper scripts are not CLI sessions.
            #
            # Keyed on the run's project and id, not on `self.run_dir`, so the
            # value cannot drift from the directory `runsetup.make_adapters`
            # pointed this run's SignalWatcher at — the producer and the consumer
            # of one channel, derived by one function from the same two inputs.
            "BMAD_LOOP_EVENTS_DIR": str(events_dir_for(self.paths.project, self.run_dir.name)),
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
            # (the generic bmad-build-auto primitive knows nothing of dw ids). Export
            # them so the generic adapter can stamp them onto the synthesized
            # result.json, keeping verify_dev_bundle's dw_ids cross-check live.
            env["BMAD_LOOP_DW_IDS"] = ",".join(task.dw_ids)
        if role == "dev" and not self.policy.review.enabled:
            # signals that the orchestrator will run no follow-up review session.
            # bmad-build-auto always self-reviews inline (step-03 → step-04) and
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
        if label is None and role == "dev" and task.phase == Phase.DEV_RUNNING:
            # Session-gate hooks run after `_dev_phase`'s last observation and may
            # legitimately mutate the workspace. Refresh a fresh-baseline snapshot,
            # or only validate an already-armed fixable chain, after those hooks;
            # fail before `session-start` / adapter.run if the trusted regular file
            # vanished or became unreadable. Launching an explicit prompt without
            # recoverable input bytes would make a later rollback unable to
            # distinguish operator intent from child output.
            had_binding = task.dispatched_spec_file is not None
            snapshot_required = (
                preserve_dispatched_spec_snapshot
                or had_binding
                or self._requires_dispatched_spec_snapshot(task, prompt)
            )
            snapshot_ok = not snapshot_required
            if had_binding:
                if preserve_dispatched_spec_snapshot:
                    snapshot_ok = self._validate_dispatched_spec_snapshot(task)
                else:
                    snapshot_ok = self._refresh_dispatched_spec_snapshot(
                        task, clear_on_failure=False
                    )
            elif snapshot_required and not preserve_dispatched_spec_snapshot:
                # A hook may introduce an explicit task-spec route into an
                # originally bare prompt. Bind it now because the actual child
                # prompt and the ownership record then agree.
                self._bind_dispatched_spec_for_attempt(task)
                snapshot_ok = (
                    task.dispatched_spec_file is not None
                    and task.dispatched_spec_snapshot is not None
                )
            if snapshot_required and not snapshot_ok:
                self._save()
                raise RuntimeError("attempt-owned spec became unreadable after pre-session hooks")
            if had_binding or snapshot_required:
                self._save()
        if label is not None:
            # Injected workflow session: name the sprint board's owner, then spell
            # out the completion-marker protocol and bound its stall nudges (see
            # WORKFLOW_BOARD_CONTRACT / WORKFLOW_COMPLETION_CONTRACT).
            #
            # Both are appended after the session-gate hooks so a
            # pre_workflow_session / pre_session prompt rewrite cannot strip them —
            # the property the dev/review seams do NOT have, because there the
            # orchestrator authors the whole prompt and the plugin body is the
            # rewrite. Here the prompt IS plugin text, so post-gate is the only
            # place the orchestrator can say anything at all.
            #
            # Board first, completion contract LAST: the marker protocol is the
            # load-bearing tail (a session that ends its turn without the marker
            # livelocks the orchestrator until session_timeout_min), and nothing may
            # come between its "end your turn" imperative and the end of the prompt.
            board_clause = self._sprint_board_instruction()
            if board_clause:
                prompt += WORKFLOW_BOARD_CONTRACT.format(clause=board_clause)

            # The marker path lands in the same implementation-artifacts dir the
            # dev adapter already searches — correct in place and under worktree
            # isolation alike, because spec.cwd is self.workspace.root either way.
            # This is the PRODUCER of the marker name. ``role``, not the default:
            # a workflow declares its own role (WORKFLOW_ROLES = dev | review) and
            # runs on THAT adapter, whose skill tree can be a different one at a
            # different era — dev=claude on .claude/skills post-rename, review=codex
            # on .agents/skills pre-rename. Resolving off the dev tree would name
            # the session a primitive its own tree does not carry. The reader still
            # accepts the legacy spelling because devcontract.FALLBACK_RESULT_PREFIXES
            # matches both eras unconditionally, so discovery survives either
            # resolution — the two halves agreeing, not a broken read-back.
            marker_path = (
                self.workspace.paths.implementation_artifacts
                / f"{self._dev_skill(role)}-result-{task_id}.md"
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
            # The spec this session is required to write, when already known (#261),
            # so the adapter reads THAT path back instead of mtime-scanning a
            # directory shared with every concurrent run. Same `_generic_dev()` guard
            # as the snapshot above, so the two can never disagree about whether the
            # devcontract read-back is in play.
            #
            # Pinned ONLY when the dispatched prompt NAMES that path — the read-back
            # may demand a file back solely because the session was told to write it.
            # Knowing a spec exists is not the same as having pointed a session at it:
            # a generic sprint re-drive names a recorded `task.spec_file` only when
            # `_dev_phase` also bound that regular file to the current attempt. A
            # fresh or stale-path task has no binding, a sweep bundle dispatches
            # `intent.md`, and StoriesEngine dispatches folder+id. Pinning any of
            # those modes would poll a path the session never promised to rewrite
            # and score its real output as "wrote nothing" — trading #261's unsafe
            # failure for a work-LOSING one, the exact trade this fix exists to avoid.
            # Testing the prompt keeps the pin and the contract that justifies it in
            # one place across all three engines (each builds its own prompt), and
            # reads the post-gate text, so a plugin rewrite cannot desynchronize them.
            # A miss simply falls back to the scan — the pre-#261 behavior.
            #
            # Withheld from an injected plugin-workflow session (`label` set — e.g. a
            # TEA pre_commit_gate): it runs the generic adapter but owes the
            # WORKFLOW_COMPLETION_CONTRACT marker above, not the story spec, so
            # pinning it to task.spec_file would point the read-back at the wrong
            # file. Same doctrine as StoriesEngine withholding BMAD_LOOP_SPEC_FOLDER
            # from labeled sessions.
            expected_spec=(
                task.spec_file
                if (
                    label is None
                    and self._generic_dev()
                    and task.spec_file
                    and self._prompt_names_recorded_spec(task, prompt)
                )
                else None
            ),
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
            # Make a completed dev result and its proof-of-work attribution
            # durable in the same state save. A host death after this save makes
            # the result replayable, so leaving the comparison until _dev_phase
            # resumes would lose attempt identity when a prior retry already
            # latched harvest_wrote_ledger. The ordinary-path comparison remains
            # after post_session so later hook-side changes are still observed.
            if (
                resumable
                and role == "dev"
                and result.status == "completed"
                and result.result_json is not None
                and task.baseline_ledger_digest is not None
            ):
                task.ledger_changed_before_harvest = (
                    self._ledger_digest() != task.baseline_ledger_digest
                )
            # A hard stop honored *inside* the session: the adapter's wait loop
            # saw a `mode: "hard"` stop-request.json, tore its window down and
            # returned this abort verdict. Position is load-bearing at both ends.
            # Inside the `try`, so the `finally` below journals the paired
            # session-end with status="aborted" — the same literal the exception
            # path writes there. Before `record_session`, so NO SessionRecord is
            # written: an abort is not a session outcome, and this matches the
            # signal-path hard stop, which interrupts inside `adapter.run()` and
            # records nothing either. "aborted" therefore never escapes this
            # method — no downstream status set (env-fault, retry, escalation)
            # needs to learn it.
            if result.status == "aborted":
                clear_graceful_stop(self.run_dir)
                raise RunStopped(via="stop-request")
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
        self._note_story_token_budget(task)
        # A hard stop request that raise site A could not see as an abort: it
        # landed in the gap after the wait loop's last poll, or the session DID
        # abort and `_post_kill_reconcile` rescued it back to `completed` (the
        # abort tore the window down before a landed Stop event was read). This
        # check fires regardless of status, and that rescue is exactly why:
        # without it a hard-stopped run would silently carry on into verify /
        # review / retry on the strength of a rescued result. The session is fully
        # recorded, saved and accounted for first, leaving the run byte-equivalent
        # to the replayable host-death-after-save state documented above — a
        # resume picks up from a complete session record, not a torn one.
        if read_stop_request_mode(self.run_dir) == "hard":
            clear_graceful_stop(self.run_dir)
            raise RunStopped(via="stop-request")
        # The same check against the *owning* run, for a nested auto-sweep child
        # whose own dir is empty because the operator stopped the parent. Without
        # it the fix above is inert on exactly the shape it exists for: the child's
        # adapter aborts off the parent's file, `_post_kill_reconcile` rescues that
        # `aborted` back to `completed`, so raise site A never fires — and the child
        # would carry on into verify/review on the strength of the rescued result.
        # Deliberately does NOT consume: the file is the parent's, and the parent's
        # own hard arm must still find it to record and attribute the stop. The
        # nested re-raise below hands this exception up before that arm consumes
        # anything, and `via` rides the exception rather than the file.
        if self._is_nested:
            owner = owner_run_dir()
            if owner is not None and read_stop_request_mode(owner) == "hard":
                raise RunStopped(via="stop-request")
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
        # A post-session hook may be the session's last writer. The completed
        # result was checkpointed before hooks so it is replayable, but a retained
        # retry can already carry `harvest_wrote_ledger=True`; replay then
        # deliberately preserves the saved attribution instead of comparing an
        # engine-modified ledger again. Close that crash window immediately after
        # hooks finish. Save only when the comparison changed, keeping the
        # zero-plugin/no-ledger-change path free of a redundant state rewrite.
        if (
            resumable
            and role == "dev"
            and result.status == "completed"  # pyright: ignore[reportOptionalMemberAccess]
            and result.result_json is not None  # pyright: ignore[reportOptionalMemberAccess]
            and task.baseline_ledger_digest is not None
        ):
            ledger_changed = self._ledger_digest() != task.baseline_ledger_digest
            if ledger_changed != task.ledger_changed_before_harvest:
                task.ledger_changed_before_harvest = ledger_changed
                self._save()
        return result  # pyright: ignore[reportReturnType]

    def _note_story_token_budget(self, task: StoryTask) -> None:
        """Warn once when a story's cost-weighted spend crosses
        ``limits.max_tokens_per_story``, at the session boundary that crossed it.

        The cap is advisory by contract (docs/FEATURES.md) — this warns, it never
        terminates. Enforcement is the separate mid-session guard (#158,
        ``max_tokens_per_session``), a different cap with its own latch, untouched
        here.

        Placement is the whole point of #336. The predecessor check sat in
        ``_finalize_commit_phase`` past ``advance(task, Phase.DONE)``, so it saw
        only stories that committed and only after their last token was spent: the
        reported 4x overrun happened on a story that DEFERRED, which produced no
        record at all — total silence, not late silence. Every session of every run
        type funnels through ``_run_session``, so judging here covers the deferring
        and escalating stories too, and SweepEngine/StoriesEngine inherit it
        unchanged.

        Called after the tail ``_save()``, where ``task.tokens`` is both fresh
        (``attach_session_usage`` folded this session in above) and
        story-cumulative (it is the task's running total, not the session's). A
        session that aborted never reaches here — the exception propagates past the
        ``finally`` — so no story is judged on a usage read that did not happen.

        Weight comes from live ``self.policy``, not ``state.cache_read_weight()``:
        this is the enforcement side of the split documented at ``summary()`` —
        displays must reproduce from state.json, budgets bind at the policy the
        running process enforces.

        Emit order is load-bearing at both ends — see the comments below. The
        invariant: never latch a suppression bit before the record it suppresses
        is durable."""
        if task.token_budget_warned:
            return
        budget = self.policy.limits.max_tokens_per_story
        weighted = task.tokens.weighted_total(self.policy.limits.cache_read_weight)
        if weighted <= budget:
            return
        self.journal.append(
            "token-budget-exceeded",
            story_key=task.story_key,
            weighted=weighted,
            total=task.tokens.total,
            budget=budget,
        )
        # Latch only once the record it suppresses is durable. Unlike the phase
        # this method's siblings set before their own journal write
        # (`_record_defer`, `_escalate`), `token_budget_warned` is rendered on no
        # surface — it is a pure suppression bit, so "latch persisted, record
        # lost" is a story that overran in total silence, i.e. #336 itself. The
        # append above is unguarded IO (journal.py) and the run-level `finally`
        # in `_run_inner` persists whatever the task carries on EVERY abort arm,
        # so latching first would bury the overrun for good on an OSError there.
        # Leaving it unlatched costs at most a duplicate notice next boundary.
        task.token_budget_warned = True
        gates.notify(
            self.policy,
            self.run_dir,
            f"story token budget exceeded: {task.story_key}",
            f"{weighted} weighted tokens ({task.tokens.total} raw incl. cache reads) "
            f"past the {budget} `limits.max_tokens_per_story` cap — advisory: the "
            f"story continues. Stop the run with `bmad-loop stop` if the spend is "
            f"no longer worth it.",
        )
        # ...and `_save()` stays AFTER the notice, the emit order every
        # record-a-decision site shares. That same run-level `finally` already
        # makes the latch durable on any in-process abort in this window, so only
        # a SIGKILL can re-warn — and at-least-once is the right bias for a
        # warning whose whole purpose is to not be silent: a duplicate ATTENTION
        # line is cosmetic, a lost one is the bug. Do not hoist this above the
        # emit pair; that trades the cosmetic failure for the silent one.
        self._save()

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._generic_dev_prompt(task, feedback)

    def _generic_dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Invocation for the generic `bmad-build-auto` dev skill, which has no
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
        # Both injected clauses ride every leg, in this order — the park clause
        # stays LAST because its docstring's backtick argument depends on nothing
        # following it. Both are bare sentences, so this seam owns every separator:
        # an em dash after the bare story key (the one leg whose text carries no
        # terminal punctuation), a plain space after a sentence. A full stop
        # followed by an em dash is punctuation noise and must never be assembled.
        #
        # `if tail else ""` is unreachable on the one class that reaches here
        # (`Engine`, whose prohibition is unconditional; both subclasses override
        # `_dev_prompt`), unlike the live guard on the review seam. Kept for
        # symmetry and pinned with a monkeypatch.
        clauses = [
            c for c in (self._sprint_board_instruction(), self._operator_park_instruction()) if c
        ]
        tail = " ".join(clauses)
        after_sentence = f" {tail}" if tail else ""
        after_key = f" — {tail}" if tail else ""
        if feedback is None:
            if task.restore_patch and task.spec_file:
                return (
                    f"/{self._dev_skill()} Resume review of the in-review spec at "
                    f"`{task.spec_file}`. The attempted change was restored onto "
                    f"the working tree after an intent-gap resolution; review it "
                    f"against the amended spec."
                ) + after_sentence
            # The attempt binding was resolved in the active workspace immediately
            # before DEV_RUNNING became durable. A retained `spec_file` alone may
            # name a discarded unit worktree, so it cannot authorize this route or
            # the matching deterministic read-back pin.
            if task.spec_file and task.dispatched_spec_file:
                return (
                    f"/{self._dev_skill()} Resume the autonomous dev session on the "
                    f"ready-for-dev spec at `{task.spec_file}`."
                ) + after_sentence
            return f"/{self._dev_skill()} {task.story_key}" + after_key
        self._reset_spec_for_repair(task)
        spec_ref = task.spec_file or task.story_key
        return (
            f"/{self._dev_skill()} Resume the autonomous dev session on the in-progress "
            f"spec at `{spec_ref}`. The previous session's work failed deterministic "
            f"verification; repair the working tree so verification passes without "
            f"changing the spec's frozen intent contract. Verification evidence is "
            f"in `{feedback}`."
        ) + after_sentence

    def _sprint_board_instruction(self) -> str:
        """The board-ownership PROHIBITION — never write the board, never revert it
        — injected into `_review_prompt` and `_generic_dev_prompt`, the two prompt
        seams `Engine` itself builds, the way the deferred-work sentence beside it
        is. It says nothing about what a session should do *instead*; that half is
        `_board_handback_redirect`, and it rides the review prompt only.

        Injection surface, exactly: story dev sessions (`_generic_dev_prompt`), the
        review sessions of sprint and sweep runs (both inherit `_review_prompt`),
        and every injected plugin-workflow session (`_run_session` wraps this in
        `WORKFLOW_BOARD_CONTRACT` post-gate — those run in the same window, at
        `post_dev_phase` / `post_review_result` / `pre_commit_gate`). `SweepEngine`
        and `StoriesEngine` override `_dev_prompt`, so no bundle or stories dev
        prompt reaches this; `StoriesEngine` overrides this method to "" as well,
        which drops it — the redirect gated on it, and the workflow section — from
        that mode's prompts too. Only the interactive resolve agent gets nothing
        from this seam, which costs nothing: `bmad-loop-resolve`'s own skill already
        forbids touching the board outright.

        The three injection points deliberately share ONE wording, so a reviewer, a
        dev session and a plugin workflow cannot be told three different things
        about who owns the board.

        They do NOT share unstrippability, and that asymmetry was weighed rather
        than overlooked. The workflow section is post-gate because it has to be —
        the prompt there is plugin text end to end, so there is no pre-gate string
        to inject into. The dev/review injections stay pre-gate: moving only this
        clause post-gate would assemble the review prompt in two files while the
        deferred-work sentence it was written to sit beside stayed strippable, and
        a plugin that rewrites `proposed_prompt` wholesale already discards the spec
        path, the ledger sentence and the #261 `expected_spec` pin with it — the
        codebase's existing reading is that such a rewrite means the plugin owns
        that session. If unstrippability is ever wanted here, move the whole
        orchestrator-authored appendix as one unit, not this sentence alone.

        `_post_dev_state_sync` advances sprint-status.yaml right after the dev
        session — to `done`, or to `awaiting-operator` on a park; those two values
        are exhaustive there — but the story's single commit lands only after the
        review loop, so every session dispatched in that window opens on an
        uncommitted, unattributed board change with nothing in the repo naming its
        author. A review session read the orchestrator's own write as a violation
        of its spec's Boundaries section, reverted it, and the #334 contradiction
        gate correctly escalated a story both sessions agreed was finished (#437).

        The row is called BOOKKEEPING and explicitly NOT proof of verification,
        because it is not: `_post_dev_state_sync` writes the board before
        `_verify_dev_artifacts` runs, and a repair session is dispatched precisely
        when that verification failed — it opens on a red tree under a `done` row.

        This reverses the "#334: review prompts are unchanged by design" rationale,
        which assumed forbidding the revert would let an unfinished story commit.
        #334's own code refutes it: `verify.verify_review` gates the SPEC
        frontmatter first and returns `retry` there, before it ever reads the
        board. A reviewer withholding sign-off through the spec already blocks the
        commit; the board revert was never the load-bearing channel.

        Returned as a bare sentence with NO leading separator, because the call
        sites need different ones. Deliberately backtick-free, for the reason
        `_operator_park_instruction` documents, and injected BEFORE that clause so
        the park contract stays the last thing a dev prompt says. Phrased as a
        prohibition rather than a goal so the skill's intent-alignment auditor
        cannot raise it as an `intent_gap`."""
        return (
            "sprint-status.yaml is owned by the orchestrator: never write it, and "
            "never revert a change to it. A row at done or awaiting-operator is the "
            "orchestrator's own bookkeeping — not a defect to fix, and not proof "
            "that the work is verified."
        )

    def _board_handback_redirect(self) -> str:
        """Where a review session that disagrees with the board should go instead.
        Rides `_review_prompt` only, and only while the prohibition it follows is
        non-empty — the gate lives HERE rather than in the caller so the invariant
        is local and directly assertable (`StoriesEngine()._board_handback_redirect()
        == ""`), and `_review_prompt` stays structurally identical to the dev seam.

        `blocked` is named deliberately: it is the only spec status that both
        withholds the commit and reaches a human (`devcontract` synthesizes a
        CRITICAL from it and `decide_review_session` pauses). Any other non-terminal
        status merely retries until the cycle budget exhausts and `_defer` rolls the
        work back to baseline — honest dissent discarded in silence. "say why" is
        load-bearing: the `## Auto Run Result` detail becomes the escalation text a
        human reads.

        The trigger is deliberately NARROW — "cannot be finished without a human
        decision", not "looks unfinished". A review pass is itself a dev-primitive
        run whose job is to fix what it finds or defer it; a broad trigger would
        hand it a run-halting early exit on cycle 1 of 3.

        Review-only for the same reason it exists: that CRITICAL halts the whole
        run, which is the right trade for a review session and the wrong one for a
        dev session, where it would flatly contradict `_operator_park_instruction`'s
        "Never use the blocked status for this" a sentence later in the same prompt.

        The vocabulary overlap with the park clause ("a human decision" vs "actions
        only a HUMAN can perform outside the repo") was checked and is unreachable,
        not merely unlikely: the two never co-occur because each rides a different
        builder, and more strongly a parked story is never dispatched a review
        session at all — `_review_and_commit` early-returns on `_park_awaiting_operator`.
        The only residual is a story the dev pass should have parked and finalized
        `done` instead, and there this opens no new path (`blocked` is the skill's
        native escape) and displaces nothing better (park is dev-only, so a review
        session cannot park either way). Pause-and-reach-a-human beats a false-green
        `done`.

        Bare sentence, no leading separator, backtick-free — same contract as the
        clause it follows."""
        if not self._sprint_board_instruction():
            return ""
        return (
            "If the story cannot be finished without a human decision, finalize the "
            "spec to status: blocked and say why. That is the hand-back channel; "
            "the board is not."
        )

    def _operator_park_instruction(self) -> str:
        """The park contract, injected into every dev prompt while
        ``[operator] enabled``. "" when the feature is off.

        Engine-injected rather than skill-owned because the durable home for it is
        upstream — bmad-build-auto's spec template and step-03/04 finalize rules —
        and that PR is not landed. This is the shipped interim: the same words the
        skill will eventually carry, said by the orchestrator so the state is
        reachable now. When upstream lands, this method is what goes away.

        The "never blocked" clause is the load-bearing half. `blocked` is the
        skill's existing escape for "I cannot finish", and a human-only action is
        exactly the shape that tempts it — but blocked HALTS the run, which is the
        original defect: one story owing a DNS record stops every story behind
        it.

        Deliberately backtick-free. It is appended AFTER the repair prompt's
        feedback-file pointer, and the last backtick-wrapped token in a dev prompt
        is by convention that path — a backticked word here would quietly become
        the "feedback file" to anything reading the prompt back.

        Returned as a bare sentence with NO leading separator. The board clause now
        always precedes it and ends in a full stop, where the em dash this used to
        carry would render a `. — `; its sole caller owns every separator."""
        if not self._operator_park_enabled():
            return ""
        return (
            "If this story's acceptance criteria include actions only a HUMAN "
            "can perform outside the repo (buy a domain, publish a DNS record, "
            "grant an API key, click through a vendor console): complete every "
            "part an agent CAN do, commit it, then finalize the spec frontmatter "
            "to status: awaiting-operator and enumerate what is owed under an "
            "operator_actions: key — a YAML list of strings, one imperative "
            "instruction each, non-empty. Never use the blocked status for this: "
            "blocked halts the whole run, and this story is finished as far as "
            "you can take it."
        )

    def _reset_spec_for_repair(self, task: StoryTask) -> None:
        """Re-open a generic-skill spec before a repair re-invocation. bmad-build-auto
        self-finalizes to `done` (or `in-review`); its step-01 routes such a spec to
        "ingest as context, do not resume," so a repair must flip the frontmatter
        `status` back to `in-progress` to re-enter implement/review in place against
        the frozen intent contract. No-op when no spec is recorded yet (the prompt
        then falls back to the story key). The stale terminal section is stripped
        too: `find_result_artifact` keys on its heading, so leaving it would let
        the re-driven session's first save of the spec read as a terminal result."""
        if not task.spec_file:
            return
        spec_path = verify.resolve_spec_path(task.spec_file, self.workspace.paths)
        try:
            if spec_path.is_symlink():
                raise RuntimeError("repair spec became a symlink")
            resolved = spec_path.resolve(strict=True)
            expected = spec_path.parent.resolve(strict=True) / spec_path.name
            if (
                resolved != expected
                or not resolved.is_file()
                or not verify.spec_within_roots(resolved, self.workspace.paths)
            ):
                raise RuntimeError("repair spec is no longer a trusted regular file")
        except FileNotFoundError:
            # Preserve the existing missing-result behavior: there is no path to
            # mutate, and ownership-aware Sprint/Stories dispatch will still fail
            # its later explicit-route snapshot gate. Sweep deliberately keeps its
            # accepted-spec routing separate from snapshot ownership.
            return
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "recorded spec became unsafe before repair prompt construction"
            ) from exc
        # Repair-write doctrine: raising beats dispatching a repair at a charged
        # attempt against a spec still reading `done` — step-01 would ingest it as
        # context and not resume, re-wedging silently (cf. runs.rearm_escalation).
        confine_root = self.workspace.paths.project
        devcontract.reset_spec_status(resolved, "in-progress", confine_root=confine_root)
        devcontract.strip_auto_run_result(resolved, confine_root=confine_root)

    def _reset_spec_for_review(self, task: StoryTask) -> SpecSnapshot | None:
        """Strip the prior pass's stale `## Auto Run Result` before a review launch,
        then capture a launch-state snapshot of the spec (#276 M1).

        A follow-up review session re-invokes bmad-build-auto on the FINALIZED spec,
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
        never re-written). "Normalized" means through `status_of`, the same reading
        the adapter's `_observe_tick` compares against: a blank/YAML-null `status:`
        must be `""` on BOTH sides or the tick fabricates a transition off it (see
        that method's docstring — the two reads are one contract).

        Snapshot capture is best-effort: a torn/unreadable read
        degrades to `None` (journaled, `review-launch-snapshot`), and the fallback
        then keeps its conservative 2-observation fingerprint path. Only the capture
        is guarded — the strip keeps its raise-on-unreadable repair doctrine (see
        `devcontract.strip_auto_run_result`): skipping the strip recreates the exact
        #160 bug state, so it must surface.

        No-op (returns `None`) when the dev skill is not the generic one or no spec
        is recorded yet."""
        if not self._generic_dev() or not task.spec_file:
            return None
        retained_authority = (
            task.dispatched_spec_file is not None or task.dispatched_spec_snapshot is not None
        )
        if (
            self._retains_dispatched_spec_snapshot_on_repair()
            and retained_authority
            and not self._validate_dispatched_spec_snapshot(task)
        ):
            raise RuntimeError(
                "attempt-owned spec became unreadable before review prompt construction"
            )
        spec_path = verify.resolve_spec_path(task.spec_file, self.workspace.paths)
        try:
            if spec_path.is_symlink():
                raise RuntimeError("review spec became a symlink")
            resolved = spec_path.resolve(strict=True)
            expected = spec_path.parent.resolve(strict=True) / spec_path.name
            if (
                resolved != expected
                or not resolved.is_file()
                or not verify.spec_within_roots(resolved, self.workspace.paths)
            ):
                raise RuntimeError("review spec is no longer a trusted regular file")
        except FileNotFoundError as exc:
            self._journal_spec_read_failed(
                spec_path,
                task.story_key,
                "review-launch-snapshot",
                exc,
            )
            return None
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "recorded spec became unsafe before review prompt construction"
            ) from exc
        devcontract.strip_auto_run_result(resolved, confine_root=self.workspace.paths.project)
        try:
            raw = resolved.read_bytes()
            mtime_ns = resolved.stat().st_mtime_ns
            fm_status = verify.status_of(verify.read_frontmatter(resolved))
        except OSError as e:
            self._journal_spec_read_failed(resolved, task.story_key, "review-launch-snapshot", e)
            return None
        return SpecSnapshot(
            path=str(resolved),
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

    def _fix_phase(self, task: StoryTask, reason: str) -> Decision:
        """Feedback-driven repair after a clean review whose verify commands
        failed. Consumes the story's dev-attempt budget; returns PROCEED once
        commands pass, or terminal review routing when repair cannot continue."""
        # Why the LAST attempt failed, when it failed without completing (#489).
        # Empty whenever the last attempt completed: the verify failure `reason`
        # already describes that, and the callers' own wording is the honest one.
        session_failure = ""
        while task.attempt < self.policy.limits.max_dev_attempts:
            preserve_chain_snapshot = self._preserves_dispatched_spec_snapshot_for_repair(task)
            task.attempt += 1
            feedback = self._write_feedback(task, reason)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            if preserve_chain_snapshot and not self._validate_dispatched_spec_snapshot(task):
                # The new attempt/phase is the durable recovery identity. Leaving
                # REVIEW_VERIFY here would replay the preceding review result on
                # resume instead of recovering this never-launched repair.
                raise RuntimeError(
                    "attempt-owned spec became unreadable before repair prompt construction"
                )
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
                session_stage="pre_fix_session",
                preserve_dispatched_spec_snapshot=preserve_chain_snapshot,
            )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            verified = NO_VERIFY_COMMANDS
            terminal = None
            if result.status == "completed":
                # A repair is another generic dev-primitive pass: it can leave
                # terminal prose ahead of frontmatter and record fresh deferred
                # findings just like the initial dev and review legs. Normalize
                # and harvest before accepting its verify-green tree, because the
                # next review pass may replace the frontmatter list.
                self._reconcile_generic_terminal_status(task, result.result_json)
                harvest_outcome = None
                for harvest_attempt in range(1, HARVEST_REPAIR_READ_ATTEMPTS + 1):
                    harvest_outcome = self._harvest_spec_deferrals(task, result.result_json)
                    if harvest_outcome is None:
                        break
                    self.journal.append(
                        "fix-harvest-failed",
                        story_key=task.story_key,
                        attempt=task.attempt,
                        reason=harvest_outcome.reason,
                        env_fault=harvest_outcome.env_fault,
                        harvest_attempt=harvest_attempt,
                    )
                    if not harvest_outcome.retryable:
                        break
                if harvest_outcome is not None and harvest_outcome.retryable:
                    outcome = harvest_outcome
                    terminal = review_exhausted(
                        task,
                        "fix-session deferral harvest remained unreadable after "
                        f"{HARVEST_REPAIR_READ_ATTEMPTS} attempts: "
                        f"{harvest_outcome.reason}",
                    )
                else:
                    terminal = None
                if harvest_outcome is not None:
                    outcome = harvest_outcome
                else:
                    outcome, verified = self._verify_commands_with_results(task, "fix")
                if not outcome.ok:
                    reason = outcome.reason
            ok = outcome is not None and outcome.ok
            session_failure = (
                "" if result.status == "completed" else session_failure_reason("fix", result)
            )
            self._emit(
                "post_dev_verify",
                task,
                session_status=result.status,
                result_json=result.result_json,
                verify_reason=(outcome.reason if outcome is not None else None),
                command_results=verified.results,
                # Stage "fix" is the only thing separating this emit from the dev
                # one: same stage, same DEV_VERIFY phase, same `attempt` counter.
                # Stays None when the harvest short-circuited above and the
                # commands never ran — `verify_reason` carries that reason.
                verification_stage=verified.stage,
                verification_sequence=verified.sequence,
            )
            self.journal.append(
                "fix-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                ok=ok,
                env_fault=bool((outcome is not None and outcome.env_fault) or result.env_fault),
                # parity with `dev-decision`: pair the diagnosis with the routing
                # it fed, so the fix path is greppable the same way (#489).
                session_vanished=result.session_vanished,
            )
            # CRITICAL routing, deliberately AFTER the emit and the journal record
            # above, and deliberately AHEAD of the env-fault/retryable arms below.
            # Both halves mirror `decide_dev`, which the dev leg reaches at the
            # same point in its own loop: it tests `critical_escalations` FIRST,
            # so a CRITICAL outranks an env fault there too, and its caller has
            # already emitted `post_dev_verify` and journalled `dev-decision` by
            # then. Escalating here before the emit — as this leg used to — made
            # one event class observable on the dev leg and invisible on the
            # repair leg: `_escalate` raises `RunPaused`, so a repair session
            # reporting CRITICAL fired no `post_dev_verify` at all, while a dev
            # session reporting the same thing fired one. The hook is named for
            # the verification, the verification ran, and a plugin correlating
            # verify passes cannot have half of them silently withheld.
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from fix session: {details}")
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
            if ok:
                # A verify-green repair supersedes the original accepted dev
                # record as the owner of the tree now parked at DEV_VERIFY. Make
                # that receipt durable in the same save as the fix decision so a
                # sweep crash resumes the repaired tree instead of restarting it.
                self._accept_current_dev_session(task)
            self._save()
            if terminal is not None:
                return terminal
            if ok:
                return Decision(Action.PROCEED)
        # Budget spent. Carry the last session's own failure so a repair the mux
        # destroyed is not filed as the verification failure that sent it here —
        # the callers substitute verify-centric text for an empty reason, which
        # is right only when the repair actually ran (#489).
        return Decision(Action.DEFER, session_failure)

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
            entry = {
                "title": title,
                "origin": "review-budget-followup",  # verbatim: re-review cap + replay dedupe key
                "source_spec": spec,
                "reason": reason,
                "severity": "low",
            }
            # Persist the intent BEFORE the write, never after it succeeds. This
            # writes the ACTIVE workspace's ledger, so under isolation the row is
            # inside a unit worktree that `close_unit_workspace` deletes, and a
            # gitignored one is skipped by `finalize_commit`'s `git add -A` in
            # silence — the run journals `refiled: DW-n` having filed nothing a
            # later sweep can reach (#425). The DONE-leg carry is the delivery
            # path, and it reads only PERSISTED records.
            #
            # Recording after the append instead loses the row outright on a hard
            # host loss: nothing saves between here and `_commit`'s COMMITTING
            # save, and that window spans every blocking `pre_commit_gate`
            # workflow (the shipped TEA plugin binds three, each a live session).
            # The resumed run replays this same review result in the same
            # worktree, `append_entry` dedupes the already-open row to None, the
            # record is never made, and the carry finds an empty payload.
            #
            # Keyed dedupe, not an `if refiled:` gate, for the same reason
            # `_harvest_spec_deferrals` keeps a stable union: a replay must not
            # append a second copy, but it must not need a NEW id to record
            # authorship either. Safe to pre-latch — `refiled_followups` is a
            # record, not a suppression bit: both consumers (replay eligibility,
            # the carry) only ever make the engine do more, and `append_entry`
            # dedupes an already-open row, so recording an append that then fails
            # costs at most a carry that files the row the operator was owed.
            known = {
                (str(item.get("origin", "")), str(item.get("source_spec", "")))
                for item in task.refiled_followups
            }
            if (entry["origin"], entry["source_spec"]) not in known:
                task.refiled_followups.append(entry)
                self._save()
            refiled = deferredwork.append_entry(ledger, **entry)
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

    def _defer_recovery_note(self, task: StoryTask) -> str:
        """Message tail naming where the deferred attempt's work survives (#333).

        A bare reason leaves the operator to find the rolled-back work themselves
        (the reporter used `git log --all`). In-place, the auto-rollback parked the
        attempt on `task.preserve_ref`, which fast-forwards straight back. Isolated,
        *this* defer rolls nothing back — a kept-failed unit's branch stays mounted —
        but an EARLIER attempt's in-worktree dev-retry rollback parks on the same
        shared refs, so `preserve_ref` can be set here too. Both are named; the
        isolated arm deliberately prints no `merge --ff-only` line, because that ref
        is not fast-forwardable from either tree (the unit branch has since moved on
        with the deferred attempt's commits, and fast-forwarding it into the
        operator's checkout would land a *discarded* attempt on their branch).

        Empty when there is nothing to point at — a clean-tree defer that parked
        nothing, a commits-preserve failure, rollback off, or `keep_failed` off with
        no earlier ref — so the notice can never advertise a ref that was not
        created. A *worktree*-snapshot failure is the one case that still points
        somewhere: `preserve_partial` then downgrades the claim to the committed
        half instead of hiding a ref that does exist. The pointer is a name, not a
        promise: `scm.preserve_keep` prunes the oldest refs at a later run's start,
        and nothing here re-validates it (this must stay git-free — `status` reads
        `state.json` only)."""
        if self._isolated:
            note = ""
            if self.policy.scm.keep_failed and task.branch:
                note = f" — failed work kept on branch `{task.branch}`"
            if task.preserve_ref:
                half = " (commits only)" if task.preserve_partial else ""
                note += (
                    f" — an earlier rolled-back attempt is parked at `{task.preserve_ref}`{half}"
                )
            return note
        if not task.preserve_ref:
            return ""
        recover = (
            f'; recover with `git -C "{self.workspace.root}" '
            f"merge --ff-only {task.preserve_ref}`"
        )
        if task.preserve_partial:
            return (
                f" — attempt COMMITS parked at `{task.preserve_ref}`{recover}; the "
                f"uncommitted changes could not be captured (journal: "
                f"attempt-worktree-preserve-failed) and did not survive the rollback"
            )
        return f" — attempt work parked at `{task.preserve_ref}`{recover}"

    def _record_defer(self, task: StoryTask, reason: str, note: str | None = None) -> None:
        """Journal + notify + persist the defer decision — the one record shape all
        three emit sites share (#342). ``note`` overrides the recovery-note tail on
        the rollback-pause path, where the reset never ran and the standard note's
        parked/destroyed claims would be wrong."""
        self.journal.append(
            "story-deferred",
            story_key=task.story_key,
            reason=reason,
            preserve_ref=task.preserve_ref or "",
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"story deferred: {task.story_key}",
            reason + (self._defer_recovery_note(task) if note is None else note),
        )
        self._save()

    def _defer(self, task: StoryTask, reason: str) -> None:
        task.defer_reason = reason
        if self._isolated:
            # the failed work lives in the unit's worktree; the diff is captured
            # and the worktree kept/dropped by _integrate_unit. Don't touch the
            # tree here (no reset into the main repo — there's nothing to undo).
            # Harvested findings are the exception: re-file them into the main
            # checkout before the terminal save makes this defer durable. Keep
            # the pre-terminal phase until that repair write succeeds: if its git
            # commit fails, ordinary inflight recovery must re-enter this decision
            # and then finish defer recording + unit teardown/integration.
            self._carry_harvested_deferrals(task)
            advance(task, Phase.DEFERRED)
            self._record_defer(task, reason)
            return
        advance(task, Phase.DEFERRED)
        if task.baseline_commit:
            self._stash_deferred_artifacts(task)
            deferred_work = self.workspace.paths.deferred_work
            snapshot = (
                deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
            )
            try:
                self._rollback_or_pause(task)
            except RunPaused:
                # Narrow, deliberate catch of the unwind-to-the-top pause (#342):
                # the pause already persisted Phase.DEFERRED (terminal), so resume
                # will never re-enter this method — the defer record is emitted
                # now or never. Every pause path fires BEFORE safe_reset, so the
                # tree is untouched: the standard recovery note's parked/destroyed
                # claims would be wrong here — the ACTION REQUIRED notice just
                # above this one in ATTENTION is the authoritative pointer.
                # Re-raised untouched: the run still pauses.
                self._record_defer(
                    task,
                    reason,
                    note=" — the tree was NOT rolled back: the run paused for manual "
                    "recovery first (see the ACTION REQUIRED notice for where the "
                    "attempt's work is)",
                )
                raise
            # reset reverts tracked deferred-work.md edits; restore review-found
            # defer entries — they are real knowledge worth keeping
            if snapshot is not None:
                current = (
                    deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
                )
                if current != snapshot:
                    deferred_work.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(deferred_work, snapshot)
            # The restore deliberately keeps review-found ledger knowledge, but
            # it also replays this bundle's accepted close after the code was
            # discarded. Let the mode undo only the close it can identify as its
            # own; the base path has no bundle close and is a no-op.
            self._reopen_ledger_after_defer(task)
        self._record_defer(task, reason)

    def _reopen_ledger_after_defer(self, task: StoryTask) -> None:
        """Undo mode-owned ledger closes after a defer discarded their code.

        No-op on the base path; only ``SweepEngine`` writes reopenable bundle
        closes. This is reached on the in-place branch after a reset was attempted
        and the ledger was restored. An isolated defer returns earlier because the
        unit worktree, including its close, is discarded without merging.
        """
        return

    def _carry_isolated_ledger_writes(self, task: StoryTask) -> None:
        """Apply Engine-owned ledger writes that an isolated merge may omit.

        APPENDS FIRST, THEN CLOSES — the ordering is a correctness contract, not a
        style. ``append_entry``'s idempotence scan is open-only, so a close that
        ran first would hide an already-filed row from it and mint a duplicate
        under a fresh id. That is why the two appends lead, why the story close
        below trails them, and why ``SweepEngine``'s override runs its bundle
        close strictly after ``super()``.

        The collision is reachable, not theoretical: a story may declare
        ``closes_deferred:`` on the very ``review-budget-followup`` row that
        ``_carry_review_budget_followups`` is about to dedupe against. Close it
        first and the follow-up is re-filed as a second entry.

        The two closes never coexist on one task — ``SweepEngine`` overrides the
        story producer to a no-op, and a story run has no bundle — so their
        relative order is unobservable.

        ``_carry_board_advance`` trails all three and is ordered freely: it writes
        sprint-status.yaml, which shares no state with the deferred-work ledger, so
        the appends-before-closes contract has nothing to say about it.
        """
        self._carry_harvested_deferrals(task)
        self._carry_review_budget_followups(task)
        self._carry_story_deferred_closes(task)
        self._carry_board_advance(task)

    def _harvest_carry_commit_may_degrade(self, ledger: Path) -> bool:
        """Whether a carry may remain uncommitted because git cannot own its path.

        Only a path proven external may degrade. Resolution uncertainty must keep
        the durable commit-pending latch set rather than guess that git cannot own
        a possibly tracked ledger and silently disable its retry.
        """
        repo = self.paths.repo_root
        try:
            rel = ledger.resolve().relative_to(repo.resolve()).as_posix()
        except (OSError, RuntimeError):
            return False
        except ValueError:
            return True  # a proven external ledger is an advisory artifact
        if verify.path_tracked(repo, rel):
            return False
        return rel not in verify.untracked_files(repo)

    def _carry_harvested_deferrals(self, task: StoryTask) -> None:
        """Re-file an isolated unit's harvested findings into the main ledger."""
        if not task.harvested_deferrals:
            return
        ledger = self.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        seen = deferredwork.parse_ledger(text)
        specs: list[deferredwork.EntrySpec] = []
        for item in task.harvested_deferrals:
            origin = str(item["origin"])
            source_spec = str(item["source_spec"])
            # Status-agnostic, and it has to be: a row this unit's finding already
            # earned and that the sweep has since CLOSED must not be re-filed,
            # and the batch writer's own idempotence scan is open-only by design
            # (a closed entry means the work came back). This one fresh
            # `parse_ledger` read is therefore the whole on-disk guard; the
            # batch's evolving scan covers only twins minted inside this call,
            # which it does see, every row it appends being open.
            if any(
                deferredwork.field_line_present(entry.body, "origin", origin)
                and deferredwork.field_line_present(entry.body, "source_spec", source_spec)
                for entry in seen
            ):
                continue
            # Persist the commit obligation before the filesystem write. A host
            # loss after the append writes the rows but before it returns must
            # still make replay commit the now-deduplicated tracked/untracked row.
            # Latch only once a novel provenance is known: when every row already
            # arrived through the merge, committing here could sweep unrelated
            # operator edits to the same ledger into the carry commit.
            if not task.harvest_carry_commit_pending:
                task.harvest_carry_commit_pending = True
                self._save()
            location = item.get("location")
            severity = item.get("severity")
            specs.append(
                deferredwork.EntrySpec(
                    title=str(item["title"]),
                    origin=origin,
                    location=str(location) if location else "n/a",
                    source_spec=source_spec,
                    reason=str(item["reason"]),
                    severity=str(severity) if severity else None,
                )
            )
        carried = [dw_id for dw_id in deferredwork.append_entries(ledger, specs) if dw_id]
        commit_needed = bool(carried) or task.harvest_carry_commit_pending
        if commit_needed:
            # The pre-append latch also covers every git observation/write below.
            # A failed add/status/commit can leave the row staged or dirty; replay
            # must retry even when the provenance scan dedupes it to `carried == []`.
            may_degrade = self._harvest_carry_commit_may_degrade(ledger)
            try:
                verify.commit_paths(
                    self.paths.repo_root,
                    f"chore(deferred-work): carry harvested findings from {task.story_key}",
                    [ledger],
                )
            except verify.GitError as e:
                if not may_degrade:
                    raise
                self.journal.append(
                    "harvest-carry-uncommitted",
                    story_key=task.story_key,
                    dw_ids=carried,
                    error=str(e),
                )
            task.harvest_carry_commit_pending = False
            self._save()
        self.journal.append("harvest-carried", story_key=task.story_key, dw_ids=carried)

    def _carry_review_budget_followups(self, task: StoryTask) -> None:
        """Re-file an isolated unit's review-budget follow-ups into the main ledger.

        The third producer in the ``git add -A`` family (#425).
        ``_record_review_budget_followup`` runs on a finalized, verify-green story
        the review pass would not stop recommending a follow-up for; under
        isolation that write is correct but is silently dropped when the main
        ledger is gitignored. Hence a carry rather than a guard, which would
        suppress a legitimate entry on every isolated run.

        No ``_isolated`` predicate: ``refiled_followups`` is populated by that one
        producer and this hook is reached only from the isolated DONE leg and its
        replay, so the record IS the guard.

        Unconditional and idempotent — ``append_entry`` dedupes an OPEN row with
        the same ``origin:`` + ``source_spec:``, while an already-CLOSED row with
        that provenance earns a fresh entry, exactly as a recurrence does in
        place. That is what lets the producer record its intent BEFORE its own
        append, which durability requires, so ``review-followup-carried`` with
        ``dw_ids == []`` is an ordinary outcome and not a carry that ran on
        nothing.

        A TRACKED ledger is safe unconditionally: no exclude or ignore rule masks a
        tracked file's MODIFICATION, so the unit's own write always rides the merge
        and its row arrives already deduped — yielding an empty ``carried`` and no
        commit.

        The commit is best effort — unlike ``_carry_harvested_deferrals``, whose
        re-raise is backed by ``harvest_carry_commit_pending`` — because it can
        only ever FAIL: the sole ledger shape that reaches it is a gitignored one,
        and ``git add`` refuses an ignored path with rc 1 every time, so a
        commit-pending latch would just retry a refusal. Nor can it leave the tree
        dirty, the shape it writes being the one git does not see. The row on disk
        is the value; the commit is bookkeeping.

        Every record must belong to the attempt now being committed — a premise
        ``_dev_phase`` enforces, not this frame. A record left over from an
        ABANDONED attempt died with its discarded worktree, so it has nothing
        upstream to dedupe against and the carry would append AND commit a row
        about work that never landed; the fresh-attempt clear beside
        ``harvested_deferrals`` is what prevents that (#457).
        """
        if not task.refiled_followups:
            return
        ledger = self.paths.deferred_work
        specs = [
            deferredwork.EntrySpec(
                title=str(item["title"]),
                origin=str(item["origin"]),
                source_spec=str(item["source_spec"]),
                reason=str(item["reason"]),
                severity=str(item["severity"]) if item.get("severity") else None,
            )
            for item in task.refiled_followups
        ]
        carried = [dw_id for dw_id in deferredwork.append_entries(ledger, specs) if dw_id]
        if carried:
            try:
                verify.commit_paths(
                    self.paths.repo_root,
                    f"chore(deferred-work): carry {task.story_key}'s review follow-up",
                    [ledger],
                )
            except verify.GitError as e:
                self.journal.append(
                    "review-followup-carry-uncommitted",
                    story_key=task.story_key,
                    dw_ids=carried,
                    error=str(e),
                )
        self.journal.append("review-followup-carried", story_key=task.story_key, dw_ids=carried)

    def _carry_story_deferred_closes(self, task: StoryTask) -> None:
        """Re-apply a story's declared ledger CLOSES to the main checkout (#458).

        The fourth and last producer in the ``git add -A`` family.
        ``_close_declared_deferred`` writes the ACTIVE workspace's ledger, so under
        isolation a gitignored one is flipped inside a unit worktree, skipped by
        ``finalize_commit``'s ``git add -A`` in silence, and deleted with the
        worktree — while the run has already journaled ``story-deferred-closed``.
        Left uncarried the entry stays ``open``, so ``deferredwork.open_ids``
        re-bundles resolved work on every later sweep: unbounded re-triage, not a
        one-time drop.

        ``mark_done_many_reopenable``, the same variant the commit-boundary close
        now uses, under the same ``_story_close_operation_id`` and the same
        ``_story_close_note``: byte-identity between a carried row and one the merge
        delivered is the point, and both halves of that comparison carry the
        ``resolution-undo:`` line since #286 made the story close entry-scoped. This
        paragraph used to say the opposite — no operation id, no undo marker — which
        was the byte-identity argument against the old close, and inverts with it.
        Only the date can differ, and only across a midnight boundary — the same
        accepted drift the park record carries.

        Unconditional and idempotent, with no tracked/ignored predicate. Idempotence
        here is stronger than the appends': ``_apply_done`` returns None for a row
        that is absent OR already ``done``, and ``_mark_done_many`` then writes
        nothing at all — not even a byte-identical rewrite. So a tracked ledger,
        whose close rode the merge, yields an empty ``carried`` and no commit, and a
        replay re-applying a landed carry is a no-op. ``story-deferred-close-carried``
        with ``dw_ids == []`` is an ordinary outcome.

        Best effort, like ``SweepEngine``'s close and unlike
        ``_carry_harvested_deferrals`` — and, as with
        ``_carry_review_budget_followups``, because the commit can only ever FAIL
        here rather than because failure is rare. Of the three shapes the main
        ledger can take, a tracked one is already closed by the merge; a
        gitignored one reaches ``commit_paths`` and ``git add`` refuses that
        ignored path with rc 1 every time, so a commit-pending latch would only
        retry a refusal. An untracked-but-not-ignored one used to be unreachable
        — ``clean_incoming_collisions`` refused the merge over it — and since
        #460 it reaches here too, where ``git add`` accepts it and the carry
        commits it, which is the leg that issue reported as never happening.

        ``self.paths``, not ``self.workspace.paths``, states the intent — the MAIN
        checkout's ledger. The two are the same path at every call site that
        reaches here: the workspace is swapped back before
        ``integrate_unit`` runs the hook, and before the resume replay does. So no
        test can tell the two apart, and none pretends to; the explicit form is
        kept because the intent stops being obvious the moment that stops holding.
        """
        if not task.story_closes_intended:
            return
        ledger = self.paths.deferred_work
        carried = deferredwork.mark_done_many_reopenable(
            ledger,
            task.story_closes_intended,
            self._today(),
            self._story_close_note(task),
            self._story_close_operation_id(task),
        )
        if carried:
            try:
                verify.commit_paths(
                    self.paths.repo_root,
                    f"chore(deferred-work): close {task.story_key}'s declared ids",
                    [ledger],
                )
            except verify.GitError as e:
                self.journal.append(
                    "story-deferred-close-carry-uncommitted",
                    story_key=task.story_key,
                    dw_ids=carried,
                    error=str(e),
                )
        self.journal.append(
            "story-deferred-close-carried", story_key=task.story_key, dw_ids=carried
        )

    def _board_carry_must_prove_ownership(self, board: Path) -> bool:
        """Whether anyone other than this pass may already have written ``board``.

        Asked by ``_carry_board_advance`` BEFORE its own advance, which is the whole of
        why it is a separate frame: a moment later this run's write is on the path and
        "was anybody else here" has stopped being answerable.

        ``dirty_paths`` — git's own answer — and nothing else decides whether EITHER
        comparison below runs at all. That ordering is load-bearing rather than an
        optimization: it is what keeps them from being asked about a board nobody has
        written, where the only honest answer is git's. It is NOT what makes the byte
        comparison safe on a repo that normalizes line endings — ``file_holds_content``
        hashes both sides through the path's clean filter for that, so no eol domain
        has to be guessed at either end.

        Fail CLOSED. A probe that could not run has not ruled an operator out, and the
        writes it gates are the ones that leave no trace of what they took. What the
        conservative answer costs depends on which check then answers, and neither cost
        is the destructive one: the sibling guarding the COMMIT costs a no-op commit,
        ``advance`` having already put the status on disk; the row check that PRECEDES
        ``advance`` costs the carry itself, and with it the next run re-picking the
        story — the #350 behavior, minus the false claim that it was fixed.

        A board outside the repo is the one False the failure paths do not share: git
        cannot commit it either way, so there is nothing here to protect and no
        baseline for either comparison below, and answering True would trade a no-op
        commit for a real refusal.

        A GITIGNORED board is the ceiling, and it is git's rather than this probe's.
        ``dirty_paths`` never reports one — ``git status --porcelain`` needs
        ``--ignored`` to spell it ``!!`` at all — so both comparisons are skipped
        here. Reporting it would change NOTHING, which is why the probe is left
        alone: an ignored board is untracked, HEAD carries no blob for it, and both
        comparisons accept a path HEAD does not carry precisely because there is no
        baseline to compare against (the #460 boundary). Measured. Nor can the COMMIT
        half of the hazard arise there — ``git add`` refuses an ignored path with rc 1
        every time. What is genuinely unprotected is the ADVANCE: a replayed carry can
        still overwrite a row an operator edited on an ignored board while the host
        was down, and nothing git holds could prove otherwise."""
        repo = self.paths.repo_root
        try:
            rel = board.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return False  # external board — never git's to commit in the first place
        except (OSError, RuntimeError):
            return True
        try:
            return rel in verify.dirty_paths(repo)
        except (verify.GitError, OSError, RuntimeError):
            return True

    def _board_carry_foreign_row_status(
        self, board: Path, story_key: str, target: str
    ) -> str | None:
        """The status ``story_key``'s row holds for somebody OTHER than this pass.

        ``None`` means the row is this pass's to write. Anything else is a status
        ``advance`` would overwrite that this run did not put there, and it is handed
        back rather than a bare False because it is the whole of what the refusal has
        to report: nothing lands, so there is no ``landed`` to journal in its place.

        The one question the sibling below cannot be asked in time. That one guards the
        COMMIT and runs after ``advance``, which for the story's OWN row is after the
        evidence is gone — ``advance`` has replaced the operator's status with the
        target, so the board then holds precisely HEAD's bytes plus this advance and
        the proof rightly says so. Nor would refusing the commit at that point have
        saved anything: the status on disk is the value ``_pick_next`` schedules from.
        Hence a check that runs BEFORE the write.

        ADDITIVE, and about one ROW rather than the board. A stray on some OTHER row is
        not this write's to refuse — ``advance`` cannot reach it, and today's outcome
        there (the advance lands, the sibling declines the commit, ``_pick_next`` stays
        honest) is the right one. Refusing on a whole-board difference would trade that
        for a finished story re-picked by every run until a human intervenes.

        Two shapes are this pass's own. A row still holding HEAD's status was written by
        nobody since the commit ``advance`` recomputes from. A row already AT or PAST
        ``target`` is the replay leg's reason to exist — a crashed pass's landed advance
        — and never-regress means ``advance`` writes nothing over it either way, so that
        shape is settled first and without asking git anything, which keeps a replay off
        the fail-closed path entirely. An ABSENT row accepts too, and is not this
        frame's to judge: ``advance`` returns None over it and writes nothing, and
        ``board-advance-carry-failed`` already names that outcome.

        Fail CLOSED on git, like both siblings and for their reason. The board's own
        parse is deliberately NOT caught: for a board that carries no
        ``development_status`` map (or is not YAML at all) the live read here raises
        ``SprintStatusError`` exactly as ``advance`` would have raised it one call
        later, and quietly converting that into a refusal would dress a corrupt
        board as an operator's edit. A MISSING board is a different case and never
        reaches this frame: over one, ``advance`` returns None where this read's
        ``load`` raises — the two disagree, which is why the caller refuses the
        shape up front (``board-advance-carry-failed``) rather than letting the
        probe die on a file the writer would have shrugged at. A path HEAD does not
        carry accepts — the #460 boundary the sibling draws, drawn once for both.
        """
        live = sprint_story_status(board, story_key)
        if live is None or _at_or_past(live, target):
            return None
        repo = self.paths.repo_root
        try:
            rel = board.resolve().relative_to(repo.resolve()).as_posix()
            head = verify.file_bytes_at_revision(repo, "HEAD", rel)
            if head is None:
                return None
            return None if live == sprint_status_in_bytes(head, story_key) else live
        except (verify.GitError, OSError, RuntimeError, ValueError, SprintStatusError):
            return live

    def _board_carry_holds_only_this_advance(
        self, board: Path, story_key: str, target: str
    ) -> bool:
        """Whether ``board``'s bytes are HEAD's plus this pass's advance and no more.

        The discrimination the replay leg needs. Refusing on DIRT alone would break the
        recovery that leg exists for: a crashed pass's own advance IS uncommitted dirt
        on exactly this path, and finishing it is the point. So the question asked is
        not whether the board is dirty but whether what is on it is what this pass
        intends — recomputed from HEAD's blob through ``advance`` itself, then compared
        byte for byte. A crashed pass's write matches, ``advance`` being deterministic
        and never-regressing, so replaying a landed one lands on the same bytes; an
        operator's edit does not.

        HEAD's blob, not a snapshot taken earlier in the run: the baseline has to
        predate every writer, and only git holds one that does.

        A path HEAD does not carry answers True, leaving an untracked board committed
        exactly as before (#460). That is the boundary ``merge_local`` already draws —
        ``_carried_artifact_rels`` filters ``protected`` to TRACKED paths, because
        protecting an untracked artifact would halt every run whose project never
        committed its board — and a second frame drawing it elsewhere would make the
        pair unreadable.

        BOTH of the places git holds this path are proved, because `commit_paths`
        overwrites both: the working tree it copies into the commit, and the index it
        stages over. A staged edit distinct from HEAD and from this advance survives
        neither, so proving the working tree alone would authorize destroying it.

        Sameness is GIT's question here, not a byte compare's (``file_holds_content``).
        The baseline is HEAD's raw blob, and the board on disk may be its CRLF twin or
        its LF one depending on nothing the run controls — git calls the tree clean
        either way, so a byte compare would have to guess, and either guess refuses a
        pristine board on the hosts the other guess serves. Both sides hashed through
        the path's clean filter answers the only question worth asking, and still parts
        an operator's added row from this pass's advance.

        Fail CLOSED, like its sibling and for its reason, and that covers
        ``advanced_bytes`` returning None: a row missing from HEAD's board leaves nothing
        to compare against, and "I could not compute the intended content" must not read
        as "the tree is mine". A row the writer declines to rewrite is NOT that case — it
        hands HEAD's bytes back unchanged, and the compare then rightly accepts a board
        nobody touched."""
        repo = self.paths.repo_root
        try:
            rel = board.resolve().relative_to(repo.resolve()).as_posix()
            head = verify.file_bytes_at_revision(repo, "HEAD", rel)
            if head is None:
                return True
            intended = sprint_advanced_bytes(head, story_key, target)
            if intended is None or not verify.file_holds_content(repo, rel, board, intended):
                return False
            # The working tree is only half of what the carry overwrites: `commit_paths`
            # stages it OVER the index, so a staged version distinct from both HEAD and
            # this advance is destroyed rather than committed.
            return verify.index_holds_no_foreign_content(repo, rel, intended)
        except (verify.GitError, OSError, RuntimeError, ValueError):
            return False

    def _carry_board_advance(self, task: StoryTask) -> None:
        """Re-apply the story's sprint-board advance to the main checkout (#350).

        The one member of the ``git add -A`` family that is not about the
        deferred-work ledger. ``_post_dev_state_sync`` advances
        ``self.workspace.paths.sprint_status``, which under isolation is the unit
        worktree's board: for a GITIGNORED board that is ``_board_seed``'s copy,
        shielded from ``finalize_commit``'s ``git add -A`` with every other seeded
        rel, so the advance never rides the unit branch and dies with the worktree
        at teardown. Uncarried, the main board keeps the story at
        ``ready-for-dev`` — inside ``ACTIONABLE_STATUSES`` — and ``_pick_next``,
        which reads the MAIN board, re-picks finished work on the next run.

        Through ``sprintstatus.advance``, the orchestrator's sole write path to this
        file, never a copy of the worktree's: that keeps the never-regress rule and
        the comment-preserving line edit in one place, and a copy would also
        overwrite rows the worktree's board knows nothing about.

        ``self.paths``, not ``self.workspace.paths`` — the MAIN checkout's board, for
        the reason spelled out in ``_carry_story_deferred_closes``: the workspace is
        swapped back before every call site reaches here, so no test can tell them
        apart, and the explicit form states an intent that stops being obvious the
        moment that stops holding.

        Unconditional and idempotent, with no tracked/ignored predicate. ``advance``
        never regresses, so re-applying a landed carry is a no-op, and so is the
        whole call for a TRACKED board: the worktree's flip is an ordinary
        modification that no ignore rule masks, so it rides the merge and the main
        board is already at the target when this runs. ``board-advance-carried``
        naming a status the carry did not itself write is an ordinary outcome.

        No ``now=``: refreshing ``last_updated`` is ``bmad-loop confirm``'s to do,
        and passing it here would rewrite a second line — a whole-file relay on a
        usually-tracked file (#576) — for bookkeeping the story's own advance
        already declined to touch.

        Best effort, like the two carries above it, and for their reason: of the
        board shapes that reach this frame, a gitignored one is the only one with
        anything to write, and ``git add`` refuses an ignored path with rc 1 every
        time — a commit-pending latch would only retry a refusal. The status on disk
        is the value that keeps ``_pick_next`` honest; the commit is bookkeeping.
        The commit is not gated on evidence of a WRITE, because ``advance`` cannot
        report whether it wrote (a never-regress echo returns the target too) — and it
        does not need to be: an unchanged board simply gives ``commit_paths`` nothing
        to commit.

        What the commit must NOT be given is somebody else's bytes, and on the LIVE
        merge path ``clean_incoming_collisions`` has already accounted for those:
        inside the branch's incoming set unrelated dirt was restored, and outside that
        set it REFUSED the merge, this frame among everything else it precedes (the
        board is one of the two paths ``merge_local`` passes as ``protected``,
        precisely because the pathspec stage below would otherwise commit it). That
        pre-flight does not precede every caller. ``_replay_unlatched_ledger_carries``
        falls straight through to the carry for a unit whose ``unit-merged`` was
        already journaled — it re-runs no merge on that leg, so no pre-flight runs on
        it either — and an edit the operator made while the host was down would ride
        out under this method's own message, tree clean behind it. Hence
        ``_board_carry_must_prove_ownership``, which asks there what the pre-flight
        asks here.

        Ownership is then asked TWICE, on either side of ``advance``, because the two
        questions have different deadlines. What the COMMIT must not be handed is
        answerable afterwards, about the whole board. What ``advance`` ITSELF must not
        overwrite is answerable only before it, and only about this story's row — so
        ``_board_carry_foreign_row_status`` leads, and a refusal there returns without
        journaling ``board-advance-carried``: nothing reached the disk, and that event's
        claim is precisely that the status did.

        What ``advance`` CAN report is that the row did not REACH ``target``, and
        that is a different question from whether it wrote — the one this method has
        to ask before naming its outcome ``board-advance-carried``. It answers
        below-target in two shapes, both of them a carry that did not happen: `None`
        when the story's row is gone (deleted or renamed while the isolated session
        held its own copy, or before a merge-to-carry replay), and
        the current status when the row is there but ``_set_mapping_value``'s line
        regex could not rewrite it — a quoted or block-scalar key, which
        ``story_status``'s full YAML parse resolves and the writer then declines.
        A whole board that is gone is the shape ``advance`` cannot be allowed to
        answer for at all: it returns None over a missing file, but the pre-advance
        row probe's own read raises ``SprintStatusError`` there — so the caller
        refuses it before either runs, on the same journal row. Latching any of
        these as carried would file a success for a main board still
        sitting in ``ACTIONABLE_STATUSES``, and the run would tear the worktree
        holding the advanced copy down on the strength of that record. It journals
        ``board-advance-carry-failed`` instead and skips the commit, which has
        nothing to carry. Still best effort, not a raise: the shapes that get here
        are ones a retry cannot repair (a board that is gone stays gone), and the
        cost of the honest record is the next run re-picking the story — the #350
        behavior, minus the false claim that it was fixed.
        """
        target = task.board_advance_intended
        if not target:
            return
        board = self.paths.sprint_status
        if not board.is_file():
            # A board that is GONE is refused here, before the ownership probes:
            # `advance` answers None over a missing file, but the foreign-row
            # probe's own live read (`sprint_story_status` → `load`) raises
            # `SprintStatusError` for it — and a deleted TRACKED board is exactly
            # the shape that turns proving on (` D` in `dirty_paths`), so on the
            # replay leg that raise escaped `_replay_unlatched_ledger_carries`
            # and killed every resume before `_loop()`. The is_file-to-advance
            # window this leaves is #686's TOCTOU, not this guard's.
            self.journal.append(
                "board-advance-carry-failed",
                story_key=task.story_key,
                target=target,
                status=None,
            )
            return
        prove = self._board_carry_must_prove_ownership(board)
        if prove:
            # `is not None`, not truthiness: an empty status is still somebody's edit.
            foreign = self._board_carry_foreign_row_status(board, task.story_key, target)
            if foreign is not None:
                self.journal.append(
                    "board-advance-carry-foreign-dirt",
                    story_key=task.story_key,
                    target=target,
                    status=foreign,
                )
                return
        landed = sprint_advance(board, task.story_key, target)
        if not _at_or_past(landed, target):
            self.journal.append(
                "board-advance-carry-failed",
                story_key=task.story_key,
                target=target,
                status=landed,
            )
            return
        if prove and not self._board_carry_holds_only_this_advance(board, task.story_key, target):
            self.journal.append(
                "board-advance-carry-foreign-dirt",
                story_key=task.story_key,
                target=target,
                status=landed,
            )
        else:
            try:
                verify.commit_paths(
                    self.paths.repo_root,
                    f"chore(sprint-status): carry {task.story_key} to {target}",
                    [board],
                )
            except verify.GitError as e:
                self.journal.append(
                    "board-advance-carry-uncommitted",
                    story_key=task.story_key,
                    target=target,
                    error=str(e),
                )
        self.journal.append(
            "board-advance-carried",
            story_key=task.story_key,
            target=target,
            status=landed,
        )

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

    def _record_sweep_refusal(self, trigger: str, reason: str) -> None:
        """Record, durably, that this trigger's auto-sweep did not deliver.

        The remedy for #501's closing note: every refusal below was journal-only,
        so a run whose one sweep trigger was refused finished looking exactly like
        a run that swept — nothing in ``summary().render()``, ``status`` or
        ``status --json`` said otherwise, and under ``auto = "run-end"`` there is
        no later ask to notice the gap.

        ``reason`` must be one of the ``SWEEP_REFUSED_*`` slugs, never a formatted
        exception — see their definition in ``model.py`` for why a free-form
        string breaks ``bmad-loop diagnose`` outright rather than being redacted.

        Written before the arm's journal/notify calls, mirroring ``latch``: the
        durable record is the point, and it must not be lost to an OSError from a
        journal append."""
        self.state.sweeps_refused[trigger] = reason
        self._save()

    def _maybe_auto_sweep(self, kind: str, trigger: str) -> None:
        """Run a child deferred-work sweep when policy [sweep].auto matches.
        The child is its own resumable run; a paused or failed child is
        journaled + notified but never interrupts this run — "failed" including a
        ``SystemExit`` (#600). A stop or a ``KeyboardInterrupt`` delivered through
        the child is the deliberate exception and propagates to the owner (#601);
        the arms below carry why, in both directions.

        ``state.sweeps_triggered`` spends the trigger only once a child sweep has
        actually started. The ``started`` thunk handed to the factory
        (:class:`SweepFactory`) fires at ``compose_sweep``'s success boundary, and
        a plain return latches too — so the thunk's only job is to classify a
        *raise*, and a raise that never reached that boundary leaves the trigger
        unspent.

        What that is worth, stated precisely, because the intuitive answer is
        wrong. It is NOT "the trigger gets retried later": at both call sites the
        retry closes within a few statements of an un-latched return.

        - ``run-end`` fires from :meth:`_loop`, whose return lands directly on
          ``self.state.finished = True`` in :meth:`_run_inner`; a finished run is
          refused outright by ``cli._resume_paused_run``. There is no later ask.
        - ``per-epic`` fires from :meth:`_epic_boundary`, and that boundary is
          detected as ``state.current_epic != story.epic`` — a field that advances
          within the same frame, either before the gate's ``RunPaused`` or on
          return into ``_loop``. Nothing in between can pause the run: ``gates``
          notification never raises (it swallows its own ``OSError``), and
          :meth:`_emit` isolates both hook kinds (``_HookError`` for a declarative
          hook, ``except Exception`` for an in-process one) and *returns* vetoes
          rather than raising them — which this caller does not even resolve.

        So the retry survives only in a crash window: a process death, or a
        ``BaseException``, between the un-latched return and the state write that
        closes the boundary. Do not widen that claim in a comment or a changelog.
        What the ordering buys on every non-crashing run is that
        ``sweeps_triggered`` is *true* — it is durable run state, rendered by
        ``bmad-loop diagnose`` (``diagnostics.py``), and a record's whole value is
        that it does not claim work that never happened.

        The worktree check therefore sits AHEAD of the latch rather than behind
        it. ``verify.worktree_clean`` fails closed on a ``GitError`` and
        ``_run_git`` maps a ``subprocess.TimeoutExpired`` onto exactly that, so a
        `git status` that merely timed out used to spend the run's one and only
        sweep trigger, permanently and silently. Both refusals carry a ``reason``
        so the journal separates a genuinely dirty tree from a git fault.

        That contract is stated over "a paused or failed child" while the guard
        below was written over ``Exception``, and the two sets differ in BOTH
        directions — which is why the arms are shaped the way they are:

        - ``SystemExit`` is a failed child the guard did not cover. It is a
          ``BaseException``, so it was missed here, by every arm of
          :meth:`_run_inner`, and by ``cli.main`` — it unwound through the
          ``finally`` (persisting this trigger's already-burned latch) and
          killed the process at exit 1, leaving the parent neither ``finished``
          nor ``crashed``, with no ``run-complete`` and an orphaned session.
          ``runsetup.make_adapters`` raises it, reachably: the unusable-mux
          refusal sits behind ``mux_usable``, a live uncached ``shutil.which``
          re-run on every call.
        - ``RunStopped`` is the reverse — an ``Exception`` that is not a failure
          at all. The child's hard-stop arm re-raises it precisely so the owner
          records the stop; swallowing it as ``sweep-auto-failed`` let the
          parent run on to ``finished`` AND left it unstoppable, since the
          signal handler latches ``_stopping`` before raising and every later
          SIGTERM then returns early.

        Deliberately NOT ``BaseException``: ``KeyboardInterrupt`` must keep
        escaping, because the nested-child re-raise in :meth:`_run_inner`
        depends on it reaching the owner."""
        if self.policy.sweep.auto != kind or self.sweep_factory is None:
            return
        if trigger in self.state.sweeps_triggered:
            return  # already fired before a pause/resume of this run
        if graceful_stop_requested(self.run_dir):
            # A pending graceful stop suppresses new child sweeps. Return (not
            # raise): at the run-end call site the story queue is already empty,
            # so finishing this run as `finished` is truthful — the finally clears
            # the superseded control file. The trigger stays unspent, which is
            # honest bookkeeping rather than a deferral: see the docstring's
            # crash-window verdict, which covers this return like the two below.
            self.journal.append("sweep-auto-suppressed", trigger=trigger)
            return
        try:
            clean = verify.worktree_clean(self.workspace.root)
        except verify.GitError as e:
            # Fails closed — but ahead of the latch, because unlike the dirty-tree
            # arm this one is transient-reachable: `_run_git` reports a
            # `subprocess.TimeoutExpired` as GitError (verify.py), so a slow
            # `git status` used to permanently spend this run's sweep trigger.
            self._record_sweep_refusal(trigger, SWEEP_REFUSED_DIRTY)
            self.journal.append(
                "sweep-auto-skipped-dirty", trigger=trigger, reason="git-error", error=str(e)
            )
            return
        if not clean:
            # should not happen at these call sites (everything committed or
            # reset); refuse rather than sweep on top of stray changes
            self._record_sweep_refusal(trigger, SWEEP_REFUSED_DIRTY)
            self.journal.append("sweep-auto-skipped-dirty", trigger=trigger, reason="dirty")
            return

        latched = False

        def latch() -> None:
            """Spend this run's trigger. Idempotent, so the factory may call it
            without knowing whether the plain-return arm below already will. The
            in-memory flag and the state list are set BEFORE the write: a `_save`
            that fails must not re-open a trigger whose child is already composed
            and resumable."""
            nonlocal latched
            if latched:
                return
            latched = True
            # Clear any refusal this trigger carries from an earlier ask, so the
            # two records cannot contradict each other. Reachable only through the
            # narrow crash window the docstring above bounds — a per-epic trigger
            # refused, the process dying before the boundary closed, and a resume
            # re-asking it. Not a retry path; a guard against a stale claim if one
            # happens. The `failed` arm re-records after this, by design.
            self.state.sweeps_refused.pop(trigger, None)
            self.state.sweeps_triggered.append(trigger)
            self._save()

        self.journal.append("sweep-auto-trigger", trigger=trigger)
        try:
            self.sweep_factory(trigger, started=latch)
        except RunStopped:
            raise  # a stop is not a failed child — let the owner record it
        except (Exception, SystemExit) as e:  # child must never break the parent
            if latched:
                self._record_sweep_refusal(trigger, SWEEP_REFUSED_FAILED)
                self.journal.append("sweep-auto-failed", trigger=trigger, error=str(e))
                gates.notify(self.policy, self.run_dir, "auto sweep failed", f"{trigger}: {e}")
            else:
                # The raise beat composition, so there is no child run dir and
                # nothing to resume — recording the trigger would be a claim about
                # work that does not exist. Still notified, and with its own
                # wording rather than none: the loudest raise that lands here is
                # the #461 config-integrity refusal, a security event that must not
                # go quiet just because it stopped being permanent.
                self._record_sweep_refusal(trigger, SWEEP_REFUSED_NOT_STARTED)
                self.journal.append("sweep-auto-not-started", trigger=trigger, error=str(e))
                gates.notify(
                    self.policy, self.run_dir, "auto sweep did not start", f"{trigger}: {e}"
                )
        else:
            # A plain return is a child that ran, whether or not the factory
            # bothered with the thunk — the thunk exists to classify raises.
            # Outside the try on purpose: `latch` writes the PARENT's state, and a
            # failure there is this run's, not a child failure to swallow.
            latch()
            self.journal.append("sweep-auto-finished", trigger=trigger)

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
