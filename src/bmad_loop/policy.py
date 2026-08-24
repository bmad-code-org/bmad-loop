"""Policy-as-data: .bmad-loop/policy.toml -> immutable Policy dataclasses."""

# Strict-checked under #245 Stage 2, with the three "expression fully known"
# rules below relaxed for this file only: the snapshot/TOML readers rebuild typed
# Policy objects from loosely-typed persisted data — `tomllib.load` and a run's
# json-round-tripped `asdict(Policy)` both hand back `dict[str, Any]`, and
# isinstance-narrowing an `Any` yields `dict[Unknown, Unknown]` — so `.get(...)`
# chains off those dicts read as Unknown. Clearing them would take a TypedDict
# per persisted shape or runtime coercions (out of scope for a no-runtime-change
# gate); every other strict rule stays on and still catches annotation drift.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import re
import tomllib
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .platform_util import (
    atomic_write_bytes_confined,
    has_parent_ref,
    is_absolute_path,
    names_tree_root,
)

POLICY_FILE = Path(".bmad-loop") / "policy.toml"

GATE_MODES = {"none", "per-epic", "per-story-spec-approval"}
RETRO_MODES = {"never", "notify", "auto"}
SESSION_BUDGET_MODES = {"off", "warn", "enforce"}
SWEEP_AUTO_MODES = {"never", "per-epic", "run-end"}
REVIEW_TRIGGER_MODES = {"always", "recommended"}
REVIEW_ON_TIMEOUT_MODES = {"retry", "salvage-if-done", "defer"}
REVIEW_ON_STATUS_CONTRADICTION_MODES = {"escalate", "retry"}
# Session stages, in run order. Lives here rather than in the TUI because
# settings_schema's expand_stages loop fans a template section out over it.
STAGES = ("dev", "review", "triage")
# Where the run gets its story queue. "sprint-status" (default) is the classic
# flow — bmad-sprint-planning writes sprint-status.yaml from prose epics.
# "stories" is the opt-in folder+id dispatch flow (BMAD-METHOD #2549): a typed,
# human-reviewed stories.yaml sibling of SPEC.md drives the loop.
STORIES_SOURCES = {"sprint-status", "stories"}
ISOLATION_MODES = {"none", "worktree"}
BRANCH_PER_MODES = {"story", "run"}
MERGE_STRATEGIES = {"ff", "merge", "squash"}
DEV_SKILLS = {"bmad-dev-auto"}

# Backend names are registry keys (adapters/multiplexer.py), never paths or
# shell input; the alphabet mirrors what built-in and plugin backends use.
_MUX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# write_mux_backend's line targets: a [section] header, and the (possibly
# commented) `backend =` anchor line inside [mux]. Template prose comments must
# never start with `backend =` or the anchor match would hit them first.
_TOML_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(?:#.*)?$")
_MUX_KEY_RE = re.compile(r"^\s*#?\s*backend\s*=")

# Deprecated [engine] keys, folded into [plugins.unity] at load time. The
# game-engine layer is now a plugin; [engine] is a one-release compatibility
# alias (see _fold_deprecated_engine).
_ENGINE_SETTING_KEYS = ("editor_mode", "mcp", "unity_path", "ready_timeout_sec", "ready_grace_sec")


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class GatesPolicy:
    mode: str = "per-epic"
    on_escalation: str = "pause"  # CRITICAL escalations always pause; field reserved
    retrospective: str = "notify"


@dataclass(frozen=True)
class LimitsPolicy:
    max_review_cycles: int = 3
    max_dev_attempts: int = 2
    # additional review rounds the orchestrator grants *solely* because a
    # completed round finalized the story (status: done) yet still set
    # `followup_review_recommended: true`. Once this many such self-recommended
    # follow-ups have been honored, the next finalized-but-still-recommending
    # round force-converges instead of burning another cycle: verify → refile the
    # lingering recommendation to the deferred-work ledger → commit. Damps the
    # structurally non-convergent step-04 rule (every review pass patches findings
    # and therefore recommends another pass). max_review_cycles stays the hard
    # outer bound. 0 = never honor a pass's own follow-up recommendation
    # (converge + refile on the first finalized round that still recommends one).
    max_followup_reviews: int = 1
    session_timeout_min: int = 90
    # hard bound on any single git subprocess the orchestrator spawns (diff,
    # reset, snapshot, …). The default is a sane normal-case ceiling, but a
    # loaded host or a very large worktree can legitimately exceed it (#156) —
    # exceeding it is a handled GitError, never a run crash, and raising the
    # bound here is the fix when it fires spuriously.
    git_timeout_s: int = 120
    # bounded grace for verified session teardown (#157): after the first
    # best-effort window kill the adapter polls liveness up to this many
    # seconds; a window that survives it gets its pane pids force-killed and
    # the window killed again. 0 = the old single unverified best-effort kill
    # (the rollback lever if escalation ever misfires).
    teardown_grace_s: int = 20
    stop_without_result_nudges: int = 1
    # how long a dev/review session may stay silent before it is declared
    # stalled. The grace starts at session launch and re-arms on transport
    # activity (pane-log output or parent/child OpenCode SSE frames) and fresh
    # Stop/idle evidence, so productive work keeps extending it. Bounded by
    # session_timeout_min. 0 disables the launch timer while retaining fail-fast
    # handling when a turn ends without a terminal spec/result.
    dev_stall_grace_s: int = 600
    # how many best-effort wake nudges a silent dev/review session receives on
    # dev_stall_grace_s expiry before it is called stalled. Transport activity
    # re-arms the grace without spending a nudge; fresh Stop/idle evidence also
    # restores this per-silence budget. The monotonic cap below still bounds the
    # total, because an accepted nudge is not proof that the session woke. 0 =
    # stall on grace expiry.
    dev_stall_nudges: int = 2
    # monotonic (never-restored) cap on total stall wake-nudges for a dev/review
    # session (SessionSpec.stall_nudges_cap). The per-silence dev_stall_nudges
    # budget is restored on every fresh Stop so a cooperative session awaiting a
    # slow background process can keep waiting — but the wake nudge is itself a
    # submitted turn, so a session that merely *answers* it ends in another
    # result-less Stop and re-earns the budget: without this cap the loop rides
    # the refill until session_timeout_min, burning a turn per cycle (#149).
    # After this many total nudges the session is declared stalled instead
    # (post-kill reconcile still rescues a finished one whose artifact is on
    # disk). 0 = stall on first grace expiry.
    dev_stall_nudges_cap: int = 6
    # same monotonic cap for injected plugin-workflow sessions: one that keeps
    # ending its turn without writing its completion marker is declared stalled
    # after this many total nudges (non-blocking workflows then advance the
    # phase).
    workflow_stall_nudges_cap: int = 3
    # One targeted nudge per session (#276 M4) when a Stop finds a marker-less
    # terminal-frontmatter spec (a review that finalized `status: done`/`blocked`
    # without appending its required `## Auto Run Result` section): ask the skill
    # to append that section now, then end its turn — repairing the omission at
    # the source so the normal marker scan harvests it. Sent exactly once per
    # session (a never-cleared set), never refilled, and touching no stall
    # counters; the harness-side frontmatter synthesis (#224) stays the backstop
    # for a session that never complies. True enables it; False keeps the pre-M4
    # behavior (synthesis only).
    dev_contract_nudge: bool = True
    # advisory cost-weighted cap on a whole STORY's cumulative spend, re-checked
    # at every session boundary and warned about once per story (#336). Nothing
    # is terminated on the crossing — enforcement is max_tokens_per_session below.
    max_tokens_per_story: int = 2_000_000
    # weight of cache-read tokens in the budget check (1.0 = count raw)
    cache_read_weight: float = 0.1
    # Mid-session token-budget guard (#158). "off" = no sampling; "warn" =
    # ATTENTION + lifecycle breadcrumb once, session runs to its natural end;
    # "enforce" = warn actions + wrap-up nudge + grace window, then the session
    # ends over_budget (rides the ordinary retry→defer arm). Adapters with no
    # mid-session usage signal (usage_parser "none", copilot's shutdown-only
    # flush) leave the guard inert regardless of mode.
    session_budget_mode: str = "warn"
    # weighted (cache_read_weight-discounted) per-SESSION cap the adapter wait
    # loops sample cumulative usage against every ~30s heartbeat tick. Distinct
    # from the advisory per-story max_tokens_per_story: this one is per session
    # and can end it; that one spans a story's sessions and only warns.
    max_tokens_per_session: int = 4_000_000
    # enforce mode: seconds a tripped session gets to wrap up after the nudge
    # before it is terminated over_budget. 0 = terminate at trip, no nudge.
    session_budget_grace_s: int = 240


@dataclass(frozen=True)
class VerifyPolicy:
    commands: tuple[str, ...] = ()
    # stream_capture_kb bounds, per stream, the verifier stdout/stderr retained
    # under the run's `verify/` directory for plugins and post-mortems (#641).
    # A tail is kept, matching every other bound on this output — the merged
    # `output_tail` is `[-2000:]`, and the end of a failing suite is where the
    # failure is. The journal record stays honest about the cut: it carries the
    # FULL byte count beside the retained one and an explicit truncation flag,
    # because a silently short file reads as a complete one.
    #
    # 256 KiB is chosen against what the store is FOR: a repair session or a
    # plugin reading a failing suite's tail. A verbose pytest/ruff failure runs
    # tens of KB, so the cap is generous enough that the realistic case is never
    # cut, while a chatty command under COMMAND_TIMEOUT_S (30 minutes) can no
    # longer emit hundreds of MB per attempt. Worst case is bounded and small:
    # commands x 2 streams x attempts x 256 KiB. It sits far under the file-store
    # precedent it is modelled on (scm.failed_diff_max_mb = 5) and far above the
    # inline-journal caps, which is the right side of both.
    #
    # 0 = capture nothing: no files are written at all, and the record still
    # lands with null pointers and the full byte counts, so the journal keeps
    # saying what the command emitted even when none of it is retained.
    stream_capture_kb: int = 256


@dataclass(frozen=True)
class NotifyPolicy:
    desktop: bool = True
    file: bool = True


@dataclass(frozen=True)
class ReviewPolicy:
    # When False, the orchestrator runs no follow-up review session; the
    # bmad-build-auto session's own inline review is the only review and it
    # finalizes the story straight to done.
    enabled: bool = True
    # When (and only when) enabled is True, decides when the follow-up review
    # session (a bmad-build-auto re-invocation on the done spec) actually runs:
    #   "recommended" (default) — only when the bmad-build-auto session set
    #       `followup_review_recommended: true` in the spec frontmatter. The
    #       skill self-reviews inline on every story and flags this when its
    #       review-driven changes were significant enough to warrant an
    #       independent second opinion. Otherwise the deterministic gates run
    #       and the story commits without a second review session.
    #   "always" — run the second-opinion review on every story (pre-PR-#2505
    #       behavior). The skill's recommendation flag is recorded but ignored.
    # Either way the review loop is bounded by two limits: limits.max_review_cycles
    # is the hard outer cap on cycles, and limits.max_followup_reviews damps the
    # structurally non-convergent case — a round that finalizes the story yet keeps
    # recommending an independent follow-up — by converging + refiling once the
    # damping grant is spent instead of looping to the outer cap.
    trigger: str = "recommended"
    # What a timeout-like review verdict (timeout / stalled / over_budget — the
    # same set the post-kill reconcile treats as rescue-eligible) costs (#271):
    #   "retry" (default) — today's behavior: burn a review cycle per timeout
    #       until limits.max_review_cycles, then defer.
    #   "salvage-if-done" — when the spec's frontmatter shows the dev product
    #       already finalized (`done`, or the `in-review` mid-review interrupt,
    #       reset forward) and the deterministic verify gate passes, commit the
    #       work and refile any outstanding follow-up review to deferred work
    #       instead of burning another full review pass on an empty delta.
    #   "defer" — give up on the first timeout-like verdict (no retries).
    # `crashed` and env-fault (#194) verdicts keep their own routing in every mode.
    on_timeout: str = "retry"
    # What a review that revokes the story's sprint sign-off costs (#334). The
    # orchestrator advances sprint-status to `done` at dev time; a review session
    # that judges the story unfinished and writes the board back to an earlier
    # stage contradicts that — and nothing in the review loop re-advances the
    # board, so every remaining cycle re-reads the same failure and the story
    # ends deferred + rolled back.
    #   "escalate" (default) — pause the run naming both sides of the
    #       disagreement, so a human resolves it instead of the budget burning
    #       down onto a rollback.
    #   "retry" — legacy behavior: treat it as an ordinary verify failure, burn
    #       review cycles to limits.max_review_cycles, then defer.
    # Keys on sprint-status only: the spec's own frontmatter status legitimately
    # cycles (in-review/in-progress) while a review patches, and `status: blocked`
    # remains the sanctioned way for a review to hand a story back to a human.
    on_status_contradiction: str = "escalate"


@dataclass(frozen=True)
class StoriesPolicy:
    """Story-queue source selection. Default reproduces sprint mode exactly.

    ``source = "stories"`` opts a run into folder+id dispatch: the loop reads a
    typed ``stories.yaml`` (Story Breakdown output, sibling of ``SPEC.md``) under
    ``spec_folder`` and dispatches each entry by folder+id instead of walking
    ``sprint-status.yaml``. ``spec_folder`` is the project-relative (or absolute)
    path to the epic's spec folder; required and must parse when
    ``source = "stories"``. There is deliberately **no** ``continue_independent``
    knob — the manifest is strictly serial (no ``depends_on``), so a blocked
    story always pauses the run for resolve rather than leapfrogging to later
    work."""

    source: str = "sprint-status"
    spec_folder: str = ""


@dataclass(frozen=True)
class DevPolicy:
    # Which inner dev skill the orchestrator drives. The sole supported value is
    # "bmad-dev-auto", the generic upstream dev primitive (BMAD-METHOD PR #2500):
    # it writes no result.json — the GenericDevAdapter synthesizes one from the
    # spec the session leaves on disk. The field is retained (rather than inlined)
    # as the seam for a future alternative dev skill; see DEV_SKILLS.
    #
    # NOT the name a session is dispatched with. Upstream renamed the primitive
    # bmad-dev-auto -> bmad-build-auto (BMAD-METHOD#2651), so the invoked name is
    # resolved from what is actually on disk (Engine._dev_skill, via
    # install.dev_primitive_or_default) and a project on either era works with
    # this field untouched. This value is the ADAPTER DISCRIMINATOR — it selects
    # the decoupled generic-dev behaviour seams (engine._generic_dev,
    # runsetup.py's result-synthesis switch) — so it keeps the pre-rename
    # spelling as a stable key rather than tracking the upstream directory name.
    skill: str = "bmad-dev-auto"


@dataclass(frozen=True)
class TuiPolicy:
    # low_frame_rate caps Textual to 15fps and disables animations (sets
    # TEXTUAL_FPS / TEXTUAL_ANIMATIONS before the app imports textual). Fixes
    # repaint tearing/garbage when driving the TUI over a slow/high-latency
    # link (SSH, Tailscale) where a 60fps update stream can't drain in time.
    low_frame_rate: bool = False
    # Persisted dashboard pane geometry, in terminal cells. 0 = unset: the layout
    # keeps its built-in default proportions (so a fresh project looks unchanged
    # and only user-resized panes land in the file). The TUI writes these when a
    # pane is resized by mouse-drag or the Ctrl+W resize mode, and seeds them back
    # on the next launch. Per-project, since policy.toml is project-scoped.
    left_width: int = 0  # sidebar (#left) width, columns
    runs_height: int = 0  # Runs pane height, rows (top of the left column)
    deferred_height: int = 0  # Deferred pane height, rows (bottom of the left column)
    tasks_height: int = 0  # Tasks table height, rows (detail column)


@dataclass(frozen=True)
class OperatorPolicy:
    # Whether a dev session may park a story at `awaiting-operator` — the
    # terminal state for a story whose agent-doable work is finished and
    # committed but whose acceptance criteria include external actions only a
    # human can perform (#335). Default-on: without it such a story has no honest
    # outcome, and the two it would otherwise take are both wrong — `done` hides
    # the outstanding work behind a green board, `blocked` halts a run over work
    # the loop was never going to do. Off, the engine injects no park
    # instruction, never targets the sprint token, and the verify gates do not
    # know the status, so a session that writes it anyway is retried with that
    # mismatch as feedback rather than silently committing.
    enabled: bool = True


@dataclass(frozen=True)
class MuxPolicy:
    # Terminal-multiplexer backend for THIS machine (the transport axis — which
    # tmux-like program hosts sessions; independent of [adapter], the coding-CLI
    # axis). "" = auto-select. Whether the name is actually registered is checked
    # at selection time (adapters/multiplexer.py), not here — policy stays
    # data-only, and a plugin backend may not be importable in every context that
    # parses policy. Machine-specific: `bmad-loop init` gitignores policy.toml.
    backend: str = ""


@dataclass(frozen=True)
class SweepPolicy:
    auto: str = "never"  # never | per-epic | run-end
    max_bundles: int = 5  # bundles executed per sweep; triage excess is truncated
    max_triage_attempts: int = 2
    max_migration_attempts: int = 2  # legacy-ledger migration retries before escalating
    repeat: bool = False  # re-triage after a cycle completes; continue on new deferred work
    max_cycles: int = 5  # total cycles per sweep run when repeat is on


@dataclass(frozen=True)
class CleanupPolicy:
    """Disk reclamation for `.bmad-loop/runs`. Worktree reconcile + artifact
    trim only ever touch terminal (finished/stopped) runs — paused/interrupted
    runs stay intact so they remain resumable."""

    run_retention: int = 10  # newest concluded runs kept whole; older ones trimmed/archived
    retention_days: int = 0  # 0 = disabled; else also keep runs newer than N days
    trim_artifacts: bool = True  # drop the worktrees/ tree from concluded runs (keeps run viewable)
    archive_old: bool = True  # archive (vs hard-delete) runs past the window
    auto_clean_on_finish: bool = True  # reconcile stale worktrees + retention at clean finish
    clean_tmp: bool = True  # let engine plugins clean their /tmp scratch (e.g. Unity MCP zips)


@dataclass(frozen=True)
class StageAdapterPolicy:
    """Per-stage overrides; None = inherit from [adapter]."""

    name: str | None = None
    model: str | None = None
    extra_args: tuple[str, ...] | None = None
    # None = inherit from [adapter] (which itself falls back to the CLI profile)
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None


@dataclass(frozen=True)
class ResolvedAdapter:
    name: str
    model: str
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None
    # None = fall back to the CLI profile's default (usage_grace_s) / the global
    # limits.stop_without_result_nudges respectively
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None


@dataclass(frozen=True)
class AdapterPolicy:
    name: str = "claude"  # CLI profile name; "claude-code-tmux" kept as legacy alias
    model: str = ""
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None = None
    # kill the run's bmad-loop-<id> tmux session when it finishes (False keeps
    # it around for post-run inspection)
    cleanup_session_on_finish: bool = True
    # None = inherit from the selected CLI profile / global limits (see
    # ResolvedAdapter); a value overrides the profile's shipped default.
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None
    dev: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    review: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    triage: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)

    def resolved(self, role: str) -> ResolvedAdapter:
        stage = {"dev": self.dev, "review": self.review, "triage": self.triage}.get(role)
        if stage is None:
            return ResolvedAdapter(
                self.name,
                self.model,
                self.extra_args,
                self.usage_grace_s,
                self.stop_without_result_nudges,
            )
        name = stage.name if stage.name is not None else self.name
        # model and extra_args are client-specific: inherit from the base only
        # when the stage runs the same client; a client switch falls back to
        # that profile's defaults (CLI default model, profile bypass flags).
        same_client = name == self.name
        # usage_grace_s / stop_without_result_nudges are benign timing knobs that
        # mean "fall back to the profile default" when None, so plain stage ??
        # base inheritance is safe regardless of a client switch.
        return ResolvedAdapter(
            name=name,
            model=(stage.model if stage.model is not None else (self.model if same_client else "")),
            extra_args=(
                stage.extra_args
                if stage.extra_args is not None
                else (self.extra_args if same_client else None)
            ),
            usage_grace_s=(
                stage.usage_grace_s if stage.usage_grace_s is not None else self.usage_grace_s
            ),
            stop_without_result_nudges=(
                stage.stop_without_result_nudges
                if stage.stop_without_result_nudges is not None
                else self.stop_without_result_nudges
            ),
        )


def _snapshot_extra_args(raw: Any) -> tuple[str, ...] | None:
    # asdict() turns the extra_args tuple into a list and json keeps it a list;
    # rebuild the tuple so a reconstructed AdapterPolicy compares equal to a
    # freshly-parsed one (the #189 tuple-vs-list trap). None stays None.
    #
    # Deliberately keeps the lenient coercion `_typed_str_tuple` replaced in
    # `loads()`: the input here is not user TOML but a json round-tripped
    # `asdict(Policy)` whose extra_args was already validated on the way in, and
    # the caller wraps this in `except Exception: return None` because it feeds
    # status/TUI display surfaces that must never crash. Raising a PolicyError
    # here would only be swallowed, at the cost of blanking the display.
    if raw is None:
        return None
    return tuple(str(a) for a in raw)


def _stage_from_snapshot(raw: Any) -> StageAdapterPolicy:
    # A missing or non-dict stage entry rebuilds to the all-inherit default,
    # matching StageAdapterPolicy()'s field defaults.
    if not isinstance(raw, dict):
        return StageAdapterPolicy()
    name = raw.get("name")
    model = raw.get("model")
    return StageAdapterPolicy(
        name=None if name is None else str(name),
        model=None if model is None else str(model),
        extra_args=_snapshot_extra_args(raw.get("extra_args")),
        usage_grace_s=raw.get("usage_grace_s"),
        stop_without_result_nudges=raw.get("stop_without_result_nudges"),
    )


def adapter_policy_from_snapshot(snapshot: dict[str, Any] | None) -> AdapterPolicy | None:
    """Rebuild an :class:`AdapterPolicy` from a run's persisted policy snapshot.

    ``snapshot`` is ``RunState.policy_snapshot`` — the json-round-tripped
    ``asdict(Policy)``. This reconstructs the ``[adapter]`` sub-tree (the base
    plus the dev/review/triage :class:`StageAdapterPolicy` stages) so display
    paths can reuse the canonical :meth:`AdapterPolicy.resolved` instead of
    re-deriving its stage-inheritance / client-switch rules against a raw dict.

    Returns ``None`` when there is nothing trustworthy to rebuild: ``snapshot``
    is ``None`` or not a dict, ``snapshot["adapter"]`` is not a dict, or it lacks
    a non-empty string ``name`` — an all-defaults reconstruction would falsely
    display "claude" for a run that predates adapter stamping. The rebuild is
    wrapped so a malformed snapshot yields ``None`` rather than raising: this
    feeds status/TUI display surfaces that must never crash.
    """
    try:
        if not isinstance(snapshot, dict):
            return None
        adapter_d = snapshot.get("adapter")
        if not isinstance(adapter_d, dict):
            return None
        name = adapter_d.get("name")
        if not isinstance(name, str) or not name:
            return None
        return AdapterPolicy(
            name=name,
            model=str(adapter_d.get("model", AdapterPolicy.model)),
            extra_args=_snapshot_extra_args(adapter_d.get("extra_args")),
            cleanup_session_on_finish=bool(
                adapter_d.get("cleanup_session_on_finish", AdapterPolicy.cleanup_session_on_finish)
            ),
            usage_grace_s=adapter_d.get("usage_grace_s"),
            stop_without_result_nudges=adapter_d.get("stop_without_result_nudges"),
            dev=_stage_from_snapshot(adapter_d.get("dev")),
            review=_stage_from_snapshot(adapter_d.get("review")),
            triage=_stage_from_snapshot(adapter_d.get("triage")),
        )
    except Exception:
        return None


@dataclass(frozen=True)
class ScmPolicy:
    # isolation = none  -> work happens in place on the checked-out branch
    #                      (today's behavior; no branches, no merge-back).
    # isolation = worktree -> each unit runs in its own git worktree/branch and
    #                      merges back into target_branch locally (Phase 3).
    isolation: str = "none"  # none | worktree
    branch_per: str = "story"  # story | run (worktree mode only)
    target_branch: str = ""  # "" = the branch checked out at run start
    merge_strategy: str = "merge"  # ff | merge | squash
    delete_branch: bool = True  # delete the unit branch after a successful merge
    keep_failed: bool = True  # keep a failed unit's worktree+branch for inspection
    # rollback_on_failure governs in-place (isolation = "none") recovery after a
    # failed attempt / rejected review. Default OFF: the orchestrator never
    # touches the working tree — it pauses the run with manual recovery
    # instructions, so a half-finished attempt is left for you to inspect. ON:
    # the orchestrator auto-reverts the attempt's tracked changes and removes the
    # untracked files THIS run created (never a blanket `git clean`; pre-existing
    # untracked files and the whole _bmad-output/ are preserved) — convenient but
    # it discards the attempt's uncommitted work, so a warning is journalled when
    # it fires. Worktree isolation sidesteps this entirely (failed work stays in
    # its worktree), so this knob only matters for isolation = "none". This flag
    # governs unattended/stopped attempts only: a human-initiated escalation
    # resolve re-drive always auto-recovers regardless — it reverts the failed
    # attempt's source but preserves the corrected spec under the BMAD artifact
    # folders, which it treats as orchestrator-owned.
    rollback_on_failure: bool = False
    # preserve_keep bounds both recovery-ref families auto-rollback parks before
    # its hard reset — the attempt-preserve/* branches and the
    # refs/attempt-preserve-dirty/* worktree snapshots: each run start keeps only
    # the N most recent per family (by committer date) and deletes the tail, so a
    # long-lived project with rollback_on_failure on doesn't accumulate them
    # forever. 0 = never prune (maximum safety).
    preserve_keep: int = 20
    # failed_diff_max_mb caps the per-file size (MB) of untracked files captured
    # into a kept-failed unit's forensic changes.patch, so a stray build dir or
    # huge log can't blow it up; oversized files are skipped with a labelled
    # marker in the patch. failed_diff_unlimited lifts the cap entirely (capture
    # everything regardless of size) — convenient but may produce very large
    # patches, so a warning is journalled when it's active.
    failed_diff_max_mb: int = 5
    failed_diff_unlimited: bool = False
    # commit_message_template, when non-empty, is the commit message dev sessions
    # use for a story's commit (placeholders {story_key}, {run_id} and
    # {story_title} — the spec's `title:` frontmatter, else a first `#` heading, minus any
    # "Story <id>:" label, falling back to the key — are substituted). Empty = the
    # built-in default message.
    commit_message_template: str = ""
    # max_parallel: units in flight at once. Parallel fan-out (Phase 5) is not
    # built yet, so any value > 1 is clamped to 1 in loads() — the knob exists
    # but is inert until the parallel scheduler lands.
    max_parallel: int = 1
    # A `git worktree add` checks out tracked files only, so gitignored MCP/CLI
    # configs are missing from every fresh worktree and isolated sessions can't
    # reach their MCP server. seed_adapter_defaults copies each loaded adapter's
    # own seed_files (e.g. claude -> .mcp.json/.claude/settings.json) into the
    # worktree; worktree_seed adds extra project-specific paths on top.
    seed_adapter_defaults: bool = True
    worktree_seed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # branch_per="run" shares a single branch across every unit in the run;
        # deleting it after the first unit's merge would defeat that (the next
        # unit would re-cut a fresh branch). Coerce delete_branch off so the
        # shared-branch semantics actually hold, regardless of how this policy
        # was constructed.
        if self.branch_per == "run" and self.delete_branch:
            object.__setattr__(self, "delete_branch", False)


@dataclass(frozen=True)
class PluginsPolicy:
    # Trust allowlist for the plugin system. A plugin folder dropped under
    # .bmad-loop/plugins/ (or shipped under bmad_loop/data/plugins/) loads its
    # declarative manifest — settings + out-of-process shell hooks — regardless.
    # A plugin that declares an in-process [python] module is NEVER imported or
    # executed unless its name appears here. Absent table = no plugins trusted,
    # which reproduces today's behavior exactly.
    enabled: tuple[str, ...] = ()
    # Per-plugin settings, parsed from the [plugins.<name>] sub-tables. Each
    # value is the raw settings dict for that plugin; the plugin's own schema
    # gives the keys meaning. Read through Policy.plugin_setting(). A plugin
    # need not be in `enabled` to carry settings here (settings are data, only
    # in-process [python] is trust-gated), but the settings UI renders a
    # plugin's section only when it is enabled.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    gates: GatesPolicy = field(default_factory=GatesPolicy)
    limits: LimitsPolicy = field(default_factory=LimitsPolicy)
    verify: VerifyPolicy = field(default_factory=VerifyPolicy)
    notify: NotifyPolicy = field(default_factory=NotifyPolicy)
    review: ReviewPolicy = field(default_factory=ReviewPolicy)
    stories: StoriesPolicy = field(default_factory=StoriesPolicy)
    dev: DevPolicy = field(default_factory=DevPolicy)
    adapter: AdapterPolicy = field(default_factory=AdapterPolicy)
    sweep: SweepPolicy = field(default_factory=SweepPolicy)
    scm: ScmPolicy = field(default_factory=ScmPolicy)
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy)
    plugins: PluginsPolicy = field(default_factory=PluginsPolicy)
    tui: TuiPolicy = field(default_factory=TuiPolicy)
    operator: OperatorPolicy = field(default_factory=OperatorPolicy)
    mux: MuxPolicy = field(default_factory=MuxPolicy)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def plugin_setting(self, name: str, key: str, default: Any = None) -> Any:
        """A single setting for plugin ``name`` from its [plugins.<name>] table,
        or ``default`` when unset. The plugin's schema supplies the real default
        when this is called with the schema default as ``default``."""
        return self.plugins.settings.get(name, {}).get(key, default)


def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{name}] must be a table")
    return value


def _opt_grace(d: dict[str, Any], where: str) -> float | None:
    """An optional per-stage override; ``None`` means "inherit", which is why this
    cannot take `_typed_float`'s default. The type guard is the same one, inline:
    a TOML int is a legal number here (``usage_grace_s = 30``), a bool is not."""
    raw = d.get("usage_grace_s")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise PolicyError(f"{where}.usage_grace_s must be a number: got {raw!r}")
    value = float(raw)
    if value < 0:
        raise PolicyError(f"{where}.usage_grace_s must be >= 0: got {value}")
    return value


def _opt_nudges(d: dict[str, Any], where: str) -> int | None:
    """The `_opt_grace` shape for the integer knob; see there for why it is inline."""
    raw = d.get("stop_without_result_nudges")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PolicyError(f"{where}.stop_without_result_nudges must be an integer: got {raw!r}")
    value = int(raw)
    if value < 0:
        raise PolicyError(f"{where}.stop_without_result_nudges must be >= 0: got {value}")
    return value


def _tui_dim(d: dict[str, Any], key: str) -> int:
    """A persisted TUI pane dimension (cells). 0 = unset; negatives are rejected.
    Strict like scm.max_parallel: a TOML bool or float would coerce silently and
    corrupt the saved geometry, so require a real int."""
    raw = d.get(key, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PolicyError(f"tui.{key} must be a non-negative integer: got {raw!r}")
    if raw < 0:
        raise PolicyError(f"tui.{key} must be >= 0: got {raw}")
    return raw


def _stage_adapter(adapter_d: dict[str, Any], key: str) -> StageAdapterPolicy:
    raw = adapter_d.get(key, {})
    if not isinstance(raw, dict):
        raise PolicyError(f"[adapter.{key}] must be a table")
    return StageAdapterPolicy(
        name=_opt_typed_str(raw, f"adapter.{key}", "name"),
        model=_opt_typed_str(raw, f"adapter.{key}", "model"),
        extra_args=_typed_str_tuple(raw, f"adapter.{key}", "extra_args"),
        usage_grace_s=_opt_grace(raw, f"adapter.{key}"),
        stop_without_result_nudges=_opt_nudges(raw, f"adapter.{key}"),
    )


def _validate_plugin_settings(name: str, raw: dict[str, Any], specs: Any) -> None:
    """Validate a [plugins.<name>] table against its plugin's setting specs
    (objects exposing key/type/options). Unknown keys and type/option mismatches
    raise PolicyError; a None schema means the plugin isn't loaded here, skip."""
    if specs is None:
        return
    by_key = {s.key: s for s in specs}
    for key, value in raw.items():
        spec = by_key.get(key)
        if spec is None:
            raise PolicyError(f"plugins.{name}: unknown setting {key!r}")
        kind = spec.type
        if kind == "bool" and not isinstance(value, bool):
            raise PolicyError(f"plugins.{name}.{key} must be a boolean")
        # bool is a subclass of int; reject it explicitly for numeric kinds.
        if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise PolicyError(f"plugins.{name}.{key} must be an integer")
        if kind == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise PolicyError(f"plugins.{name}.{key} must be a number")
        if kind == "str" and not isinstance(value, str):
            raise PolicyError(f"plugins.{name}.{key} must be a string")
        if kind == "select" and value not in spec.options:
            raise PolicyError(
                f"plugins.{name}.{key} must be one of {list(spec.options)}: got {value!r}"
            )


# The typed readers every user-TOML section coerces through. `where` is the
# section label the message names ("limits", "scm", "adapter.dev", ...), so one
# definition serves every section instead of a per-section family; a bare
# `int()`/`bool()` in their place raises a raw ValueError/TypeError, which is
# neither a PolicyError nor an OSError and so escapes every degrade handler in
# the codebase (#440, and #278 for the [limits] leg these grew out of).


def _typed_int(d: dict[str, Any], where: str, key: str, default: int) -> int:
    value = d.get(key, default)
    # bool is a subclass of int; a TOML `true` would read as 1 and silently
    # rewrite the knob, so reject it before the isinstance check passes it.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{where}.{key} must be an integer: got {value!r}")
    return value


def _typed_float(d: dict[str, Any], where: str, key: str, default: float) -> float:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{where}.{key} must be a number: got {value!r}")
    return float(value)


def _typed_bool(d: dict[str, Any], where: str, key: str, default: bool) -> bool:
    value = d.get(key, default)
    if not isinstance(value, bool):
        raise PolicyError(f"{where}.{key} must be a boolean: got {value!r}")
    return value


def _typed_str(d: dict[str, Any], where: str, key: str, default: str) -> str:
    value = d.get(key, default)
    if not isinstance(value, str):
        raise PolicyError(f"{where}.{key} must be a string: got {value!r}")
    return value


def _opt_typed_str(d: dict[str, Any], where: str, key: str) -> str | None:
    """The `_typed_str` shape for a key whose unset state is None rather than a
    default — the per-stage `[adapter.<stage>]` overrides inherit from the parent
    `[adapter]` table when absent, so they cannot express "unset" as a value."""
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError(f"{where}.{key} must be a string: got {value!r}")
    return value


def _typed_str_tuple(d: dict[str, Any], where: str, key: str) -> tuple[str, ...] | None:
    """An optional TOML array of strings, or None when the key is absent.

    Shape before entries, for the reason `scm.worktree_seed` states below: a bare
    string is iterable, so `tuple(str(a) for a in raw)` explodes it per character
    into one-character entries that each look like a valid argument, and a scalar
    raises a bare TypeError out of `loads` where every other malformed value here
    raises PolicyError."""
    raw = d.get(key)
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise PolicyError(f"{where}.{key} must be an array of strings: got {raw!r}")
    if not all(isinstance(a, str) for a in raw):
        raise PolicyError(f"{where}.{key} must be an array of strings: got {list(raw)!r}")
    return tuple(raw)


def load(path: Path | None) -> Policy:
    """Load policy from a TOML file; a missing file yields all defaults.

    An undecodable file is a `PolicyError` like a malformed one. `read_text` raises
    `UnicodeDecodeError`, which is a `ValueError` and not an `OSError`, so left raw it
    escapes every `except (PolicyError, OSError)` handler in the codebase — and those
    handlers are the ones whose whole job is to degrade to defaults rather than take
    the process down. Converting here fixes them all at once instead of asking each
    to name a second exception type it has no other reason to know about."""
    if path is None or not path.is_file():
        return loads("")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise PolicyError(f"{path}: not valid UTF-8: {e}") from e
    try:
        return loads(text)
    except PolicyError as e:
        raise PolicyError(f"{path}: {e}") from e


def loads(text: str, plugin_schemas: dict[str, Any] | None = None) -> Policy:
    """Parse and validate policy TOML text; empty text yields all defaults.

    ``plugin_schemas`` optionally maps a plugin name to its sequence of setting
    specs (objects with ``key``/``type``/``options`` attributes). When given,
    every present ``[plugins.<name>]`` table whose plugin is in the mapping is
    validated against that schema: unknown keys and type/option mismatches raise
    PolicyError. Plugin tables without a supplied schema pass through untouched
    (a plugin may not be loaded in every context that reads policy)."""
    try:
        doc: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"invalid policy TOML: {e}") from e

    gates_d = _section(doc, "gates")
    limits_d = _section(doc, "limits")
    verify_d = _section(doc, "verify")
    notify_d = _section(doc, "notify")
    review_d = _section(doc, "review")
    stories_d = _section(doc, "stories")
    dev_d = _section(doc, "dev")
    adapter_d = _section(doc, "adapter")
    sweep_d = _section(doc, "sweep")
    scm_d = _section(doc, "scm")
    cleanup_d = _section(doc, "cleanup")
    engine_d = _section(doc, "engine")  # deprecated; folded into [plugins] below
    plugins_d = _section(doc, "plugins")
    tui_d = _section(doc, "tui")
    operator_d = _section(doc, "operator")
    mux_d = _section(doc, "mux")

    gates = GatesPolicy(
        mode=_typed_str(gates_d, "gates", "mode", GatesPolicy.mode),
        on_escalation=_typed_str(gates_d, "gates", "on_escalation", GatesPolicy.on_escalation),
        retrospective=_typed_str(gates_d, "gates", "retrospective", GatesPolicy.retrospective),
    )
    if gates.mode not in GATE_MODES:
        raise PolicyError(f"gates.mode must be one of {sorted(GATE_MODES)}: got {gates.mode!r}")
    if gates.retrospective not in RETRO_MODES:
        raise PolicyError(
            f"gates.retrospective must be one of {sorted(RETRO_MODES)}: got {gates.retrospective!r}"
        )

    limits = LimitsPolicy(
        max_review_cycles=_typed_int(
            limits_d, "limits", "max_review_cycles", LimitsPolicy.max_review_cycles
        ),
        max_dev_attempts=_typed_int(
            limits_d, "limits", "max_dev_attempts", LimitsPolicy.max_dev_attempts
        ),
        max_followup_reviews=_typed_int(
            limits_d, "limits", "max_followup_reviews", LimitsPolicy.max_followup_reviews
        ),
        session_timeout_min=_typed_int(
            limits_d, "limits", "session_timeout_min", LimitsPolicy.session_timeout_min
        ),
        git_timeout_s=_typed_int(limits_d, "limits", "git_timeout_s", LimitsPolicy.git_timeout_s),
        teardown_grace_s=_typed_int(
            limits_d, "limits", "teardown_grace_s", LimitsPolicy.teardown_grace_s
        ),
        stop_without_result_nudges=_typed_int(
            limits_d,
            "limits",
            "stop_without_result_nudges",
            LimitsPolicy.stop_without_result_nudges,
        ),
        dev_stall_grace_s=_typed_int(
            limits_d, "limits", "dev_stall_grace_s", LimitsPolicy.dev_stall_grace_s
        ),
        dev_stall_nudges=_typed_int(
            limits_d, "limits", "dev_stall_nudges", LimitsPolicy.dev_stall_nudges
        ),
        dev_stall_nudges_cap=_typed_int(
            limits_d, "limits", "dev_stall_nudges_cap", LimitsPolicy.dev_stall_nudges_cap
        ),
        workflow_stall_nudges_cap=_typed_int(
            limits_d, "limits", "workflow_stall_nudges_cap", LimitsPolicy.workflow_stall_nudges_cap
        ),
        dev_contract_nudge=_typed_bool(
            limits_d, "limits", "dev_contract_nudge", LimitsPolicy.dev_contract_nudge
        ),
        max_tokens_per_story=_typed_int(
            limits_d, "limits", "max_tokens_per_story", LimitsPolicy.max_tokens_per_story
        ),
        cache_read_weight=_typed_float(
            limits_d, "limits", "cache_read_weight", LimitsPolicy.cache_read_weight
        ),
        session_budget_mode=_typed_str(
            limits_d, "limits", "session_budget_mode", LimitsPolicy.session_budget_mode
        ),
        max_tokens_per_session=_typed_int(
            limits_d, "limits", "max_tokens_per_session", LimitsPolicy.max_tokens_per_session
        ),
        session_budget_grace_s=_typed_int(
            limits_d, "limits", "session_budget_grace_s", LimitsPolicy.session_budget_grace_s
        ),
    )
    if limits.max_review_cycles < 1 or limits.max_dev_attempts < 1:
        raise PolicyError("limits.max_review_cycles and limits.max_dev_attempts must be >= 1")
    if limits.max_followup_reviews < 0:
        raise PolicyError(
            f"limits.max_followup_reviews must be >= 0: got {limits.max_followup_reviews}"
        )
    if limits.session_timeout_min < 1:
        raise PolicyError(
            f"limits.session_timeout_min must be >= 1: got {limits.session_timeout_min}"
        )
    if limits.git_timeout_s < 1:
        raise PolicyError(f"limits.git_timeout_s must be >= 1: got {limits.git_timeout_s}")
    if limits.teardown_grace_s < 0:
        raise PolicyError(f"limits.teardown_grace_s must be >= 0: got {limits.teardown_grace_s}")
    if limits.stop_without_result_nudges < 0:
        raise PolicyError(
            "limits.stop_without_result_nudges must be >= 0: "
            f"got {limits.stop_without_result_nudges}"
        )
    if not 0.0 <= limits.cache_read_weight <= 1.0:
        raise PolicyError(
            f"limits.cache_read_weight must be between 0 and 1: got {limits.cache_read_weight}"
        )
    if limits.dev_stall_grace_s < 0:
        raise PolicyError(f"limits.dev_stall_grace_s must be >= 0: got {limits.dev_stall_grace_s}")
    if limits.dev_stall_nudges < 0:
        raise PolicyError(f"limits.dev_stall_nudges must be >= 0: got {limits.dev_stall_nudges}")
    if limits.dev_stall_nudges_cap < 0:
        raise PolicyError(
            f"limits.dev_stall_nudges_cap must be >= 0: got {limits.dev_stall_nudges_cap}"
        )
    if limits.workflow_stall_nudges_cap < 0:
        raise PolicyError(
            f"limits.workflow_stall_nudges_cap must be >= 0: got {limits.workflow_stall_nudges_cap}"
        )
    if limits.session_budget_mode not in SESSION_BUDGET_MODES:
        raise PolicyError(
            f"limits.session_budget_mode must be one of {sorted(SESSION_BUDGET_MODES)}: "
            f"got {limits.session_budget_mode!r}"
        )
    if limits.max_tokens_per_story < 1:
        raise PolicyError(
            f"limits.max_tokens_per_story must be >= 1: got {limits.max_tokens_per_story}"
        )
    if limits.max_tokens_per_session < 1:
        raise PolicyError(
            f"limits.max_tokens_per_session must be >= 1: got {limits.max_tokens_per_session}"
        )
    if limits.session_budget_grace_s < 0:
        raise PolicyError(
            f"limits.session_budget_grace_s must be >= 0: got {limits.session_budget_grace_s}"
        )

    verify = VerifyPolicy(
        commands=_typed_str_tuple(verify_d, "verify", "commands") or (),
        stream_capture_kb=_typed_int(
            verify_d, "verify", "stream_capture_kb", VerifyPolicy.stream_capture_kb
        ),
    )
    if verify.stream_capture_kb < 0:
        raise PolicyError(f"verify.stream_capture_kb must be >= 0: got {verify.stream_capture_kb}")
    notify = NotifyPolicy(
        desktop=_typed_bool(notify_d, "notify", "desktop", NotifyPolicy.desktop),
        file=_typed_bool(notify_d, "notify", "file", NotifyPolicy.file),
    )
    review = ReviewPolicy(
        enabled=_typed_bool(review_d, "review", "enabled", ReviewPolicy.enabled),
        trigger=_typed_str(review_d, "review", "trigger", ReviewPolicy.trigger).strip(),
        on_timeout=_typed_str(review_d, "review", "on_timeout", ReviewPolicy.on_timeout).strip(),
        on_status_contradiction=_typed_str(
            review_d, "review", "on_status_contradiction", ReviewPolicy.on_status_contradiction
        ).strip(),
    )
    if review.trigger not in REVIEW_TRIGGER_MODES:
        raise PolicyError(
            f"review.trigger must be one of {sorted(REVIEW_TRIGGER_MODES)}: got {review.trigger!r}"
        )
    if review.on_timeout not in REVIEW_ON_TIMEOUT_MODES:
        raise PolicyError(
            f"review.on_timeout must be one of {sorted(REVIEW_ON_TIMEOUT_MODES)}:"
            f" got {review.on_timeout!r}"
        )
    if review.on_status_contradiction not in REVIEW_ON_STATUS_CONTRADICTION_MODES:
        raise PolicyError(
            "review.on_status_contradiction must be one of "
            f"{sorted(REVIEW_ON_STATUS_CONTRADICTION_MODES)}:"
            f" got {review.on_status_contradiction!r}"
        )
    stories = StoriesPolicy(
        source=_typed_str(stories_d, "stories", "source", StoriesPolicy.source).strip(),
        spec_folder=_typed_str(
            stories_d, "stories", "spec_folder", StoriesPolicy.spec_folder
        ).strip(),
    )
    if stories.source not in STORIES_SOURCES:
        raise PolicyError(
            f"stories.source must be one of {sorted(STORIES_SOURCES)}: got {stories.source!r}"
        )
    # source="stories" needs a spec_folder to read stories.yaml from; the reverse
    # (a spec_folder set under sprint-status mode) is a harmless leftover, ignored
    # at run time — no error, so switching source back and forth keeps the path.
    if stories.source == "stories" and not stories.spec_folder:
        raise PolicyError('stories.source = "stories" requires stories.spec_folder to be set')
    dev = DevPolicy(skill=_typed_str(dev_d, "dev", "skill", DevPolicy.skill))
    if dev.skill not in DEV_SKILLS:
        raise PolicyError(
            f"dev.skill must be one of {sorted(DEV_SKILLS)}: got {dev.skill!r}. This is the "
            f"adapter discriminator, not the invoked skill name — the name a session is "
            f"dispatched with is resolved from the skill tree on disk, so a project on the "
            f"post-rename bmad-build-auto needs no change here"
        )
    for legacy, replacement in (
        ("model_dev", "[adapter.dev] model"),
        ("model_review", "[adapter.review] model"),
    ):
        if legacy in adapter_d:
            raise PolicyError(f"adapter.{legacy} was removed — use {replacement} instead")
    adapter = AdapterPolicy(
        name=_typed_str(adapter_d, "adapter", "name", AdapterPolicy.name),
        model=_typed_str(adapter_d, "adapter", "model", AdapterPolicy.model),
        extra_args=_typed_str_tuple(adapter_d, "adapter", "extra_args"),
        cleanup_session_on_finish=_typed_bool(
            adapter_d,
            "adapter",
            "cleanup_session_on_finish",
            AdapterPolicy.cleanup_session_on_finish,
        ),
        usage_grace_s=_opt_grace(adapter_d, "adapter"),
        stop_without_result_nudges=_opt_nudges(adapter_d, "adapter"),
        dev=_stage_adapter(adapter_d, "dev"),
        review=_stage_adapter(adapter_d, "review"),
        triage=_stage_adapter(adapter_d, "triage"),
    )
    sweep = SweepPolicy(
        auto=_typed_str(sweep_d, "sweep", "auto", SweepPolicy.auto),
        max_bundles=_typed_int(sweep_d, "sweep", "max_bundles", SweepPolicy.max_bundles),
        max_triage_attempts=_typed_int(
            sweep_d, "sweep", "max_triage_attempts", SweepPolicy.max_triage_attempts
        ),
        max_migration_attempts=_typed_int(
            sweep_d, "sweep", "max_migration_attempts", SweepPolicy.max_migration_attempts
        ),
        repeat=_typed_bool(sweep_d, "sweep", "repeat", SweepPolicy.repeat),
        max_cycles=_typed_int(sweep_d, "sweep", "max_cycles", SweepPolicy.max_cycles),
    )
    if sweep.auto not in SWEEP_AUTO_MODES:
        raise PolicyError(
            f"sweep.auto must be one of {sorted(SWEEP_AUTO_MODES)}: got {sweep.auto!r}"
        )
    if (
        min(
            sweep.max_bundles,
            sweep.max_triage_attempts,
            sweep.max_migration_attempts,
            sweep.max_cycles,
        )
        < 1
    ):
        raise PolicyError(
            "sweep.max_bundles, sweep.max_triage_attempts, "
            "sweep.max_migration_attempts and sweep.max_cycles must be >= 1"
        )
    requested_parallel = _typed_int(scm_d, "scm", "max_parallel", ScmPolicy.max_parallel)
    if requested_parallel < 1:
        raise PolicyError(f"scm.max_parallel must be >= 1: got {requested_parallel}")
    # This one was strict before its sibling int knobs were (a TOML `true`, with
    # int(True) == 1, or a `1.9` coercing through int() would silently shrink a
    # safety-net budget); `_typed_int` is that same guard, message included.
    preserve_keep = _typed_int(scm_d, "scm", "preserve_keep", ScmPolicy.preserve_keep)
    if preserve_keep < 0:
        raise PolicyError(f"scm.preserve_keep must be >= 0: got {preserve_keep}")
    # Shape before entries, because `tuple(str(s) for s in raw)` silently accepts
    # things that are not a list of paths. Measured: `worktree_seed = ""` yields an
    # empty tuple (the config reads as applied and seeds nothing), `= "foo"` yields
    # ('f','o','o') — three bogus one-character entries that each pass the
    # per-entry guard below — and `= 5` raises a bare TypeError out of `loads`,
    # untyped, where every other malformed value here raises PolicyError.
    raw_seed = scm_d.get("worktree_seed", ())
    if isinstance(raw_seed, (str, bytes)) or not isinstance(raw_seed, (list, tuple)):
        raise PolicyError(f"scm.worktree_seed must be a list of paths: got {raw_seed!r}")
    if not all(isinstance(s, str) for s in raw_seed):
        raise PolicyError(f"scm.worktree_seed entries must be strings: got {list(raw_seed)!r}")
    scm = ScmPolicy(
        isolation=_typed_str(scm_d, "scm", "isolation", ScmPolicy.isolation),
        branch_per=_typed_str(scm_d, "scm", "branch_per", ScmPolicy.branch_per),
        target_branch=_typed_str(scm_d, "scm", "target_branch", ScmPolicy.target_branch),
        merge_strategy=_typed_str(scm_d, "scm", "merge_strategy", ScmPolicy.merge_strategy),
        delete_branch=_typed_bool(scm_d, "scm", "delete_branch", ScmPolicy.delete_branch),
        keep_failed=_typed_bool(scm_d, "scm", "keep_failed", ScmPolicy.keep_failed),
        rollback_on_failure=_typed_bool(
            scm_d, "scm", "rollback_on_failure", ScmPolicy.rollback_on_failure
        ),
        preserve_keep=preserve_keep,
        failed_diff_max_mb=_typed_int(
            scm_d, "scm", "failed_diff_max_mb", ScmPolicy.failed_diff_max_mb
        ),
        failed_diff_unlimited=_typed_bool(
            scm_d, "scm", "failed_diff_unlimited", ScmPolicy.failed_diff_unlimited
        ),
        commit_message_template=_typed_str(
            scm_d, "scm", "commit_message_template", ScmPolicy.commit_message_template
        ),
        # Phase 5 parallel fan-out is unbuilt: clamp to 1 so the knob is inert.
        max_parallel=min(requested_parallel, 1),
        seed_adapter_defaults=_typed_bool(
            scm_d, "scm", "seed_adapter_defaults", ScmPolicy.seed_adapter_defaults
        ),
        worktree_seed=tuple(raw_seed),
    )
    if scm.isolation not in ISOLATION_MODES:
        raise PolicyError(
            f"scm.isolation must be one of {sorted(ISOLATION_MODES)}: got {scm.isolation!r}"
        )
    if scm.branch_per not in BRANCH_PER_MODES:
        raise PolicyError(
            f"scm.branch_per must be one of {sorted(BRANCH_PER_MODES)}: got {scm.branch_per!r}"
        )
    if scm.merge_strategy not in MERGE_STRATEGIES:
        raise PolicyError(
            f"scm.merge_strategy must be one of {sorted(MERGE_STRATEGIES)}: "
            f"got {scm.merge_strategy!r}"
        )
    if scm.failed_diff_max_mb < 1:
        raise PolicyError(f"scm.failed_diff_max_mb must be >= 1: got {scm.failed_diff_max_mb}")
    # The same rule the other two seed sources already apply to their own entries
    # (adapters/profile.py `seed_files`, plugins/manifest.py `_check_relative_paths`).
    # All three feed one list into provision_worktree's seed loop, and this was the
    # only one arriving unvalidated.
    #
    # A ROOT-NAMING entry is why this is a guard rather than a tidy-up: it makes that
    # loop resolve src to the repo ROOT and dst to the worktree, both of which pass
    # its `is_relative_to` containment checks — a path IS relative to itself — so it
    # copies the entire repo into the worktree, gitignored and untracked files
    # included. And because a worktree mounts UNDER the repo (.bmad-loop/runs/...),
    # that copy walks into its own destination: measured at 744 levels of nesting
    # before the path length failed, silently, since the seed copy suppresses errors.
    #
    # `names_tree_root` and not a `not seed` emptiness check, because "" is only one
    # spelling of the root. Measured: "" and "." produce a byte-identical
    # (src, raw, dst) triple in that loop, and a run seeded with ["."] copied an
    # untracked secret.env in and self-recursed until the path length failed, with
    # provision_worktree returning no skip at all.
    #
    # The "/" it then renders is INERT, not a blanket exclusion — git strips a bare
    # slash to a zero-length pattern that matches nothing (worktree_flow.py says the
    # same at the write side). That is what makes this worth guarding rather than
    # shrugging at: none of the copied surplus is shielded, so the unit's
    # `git add -A` stages the main checkout's untracked files into the story.
    #
    # Absolute and `..` entries are already contained by those same checks (silently
    # skipped); they are rejected here for consistency with the sibling sources, and
    # because a silently-inert seed entry reads as applied configuration when it is
    # not.
    for seed in scm.worktree_seed:
        if names_tree_root(seed) or is_absolute_path(seed) or has_parent_ref(seed):
            raise PolicyError(
                f"scm.worktree_seed entries must be project-relative paths: got {seed!r}"
            )
    cleanup = CleanupPolicy(
        run_retention=_typed_int(
            cleanup_d, "cleanup", "run_retention", CleanupPolicy.run_retention
        ),
        retention_days=_typed_int(
            cleanup_d, "cleanup", "retention_days", CleanupPolicy.retention_days
        ),
        trim_artifacts=_typed_bool(
            cleanup_d, "cleanup", "trim_artifacts", CleanupPolicy.trim_artifacts
        ),
        archive_old=_typed_bool(cleanup_d, "cleanup", "archive_old", CleanupPolicy.archive_old),
        auto_clean_on_finish=_typed_bool(
            cleanup_d, "cleanup", "auto_clean_on_finish", CleanupPolicy.auto_clean_on_finish
        ),
        clean_tmp=_typed_bool(cleanup_d, "cleanup", "clean_tmp", CleanupPolicy.clean_tmp),
    )
    if cleanup.run_retention < 0:
        raise PolicyError(f"cleanup.run_retention must be >= 0: got {cleanup.run_retention}")
    if cleanup.retention_days < 0:
        raise PolicyError(f"cleanup.retention_days must be >= 0: got {cleanup.retention_days}")
    raw_enabled = plugins_d.get("enabled", ())
    if isinstance(raw_enabled, str) or not isinstance(raw_enabled, (list, tuple)):
        raise PolicyError("plugins.enabled must be a list of plugin names")
    enabled: list[str] = []
    for n in raw_enabled:
        if not isinstance(n, str):
            raise PolicyError(f"plugins.enabled entries must be strings: got {n!r}")
        enabled.append(n)
    # Every key under [plugins] other than `enabled` that is a table is a
    # per-plugin settings sub-table ([plugins.<name>]).
    plugin_settings = {
        str(k): dict(v) for k, v in plugins_d.items() if k != "enabled" and isinstance(v, dict)
    }
    # The game-engine layer is now a plugin. Fold a deprecated [engine] block into
    # [plugins] (enable the named plugin + map its keys to [plugins.<name>]) so
    # existing Unity configs keep working for one release; explicit [plugins.*]
    # values win over the folded ones.
    _fold_deprecated_engine(engine_d, enabled, plugin_settings)
    if plugin_schemas:
        for name, raw_settings in plugin_settings.items():
            _validate_plugin_settings(name, raw_settings, plugin_schemas.get(name))
    plugins = PluginsPolicy(enabled=tuple(enabled), settings=plugin_settings)
    tui = TuiPolicy(
        low_frame_rate=_typed_bool(tui_d, "tui", "low_frame_rate", TuiPolicy.low_frame_rate),
        left_width=_tui_dim(tui_d, "left_width"),
        runs_height=_tui_dim(tui_d, "runs_height"),
        deferred_height=_tui_dim(tui_d, "deferred_height"),
        tasks_height=_tui_dim(tui_d, "tasks_height"),
    )
    operator = OperatorPolicy(
        enabled=_typed_bool(operator_d, "operator", "enabled", OperatorPolicy.enabled)
    )
    mux = MuxPolicy(backend=_typed_str(mux_d, "mux", "backend", MuxPolicy.backend).strip())
    if mux.backend and not _MUX_NAME_RE.match(mux.backend):
        raise PolicyError(
            f"mux.backend must be a backend name (letters, digits, . _ -): got {mux.backend!r}"
        )
    return Policy(
        gates=gates,
        limits=limits,
        verify=verify,
        notify=notify,
        review=review,
        stories=stories,
        dev=dev,
        adapter=adapter,
        sweep=sweep,
        scm=scm,
        cleanup=cleanup,
        plugins=plugins,
        tui=tui,
        operator=operator,
        mux=mux,
    )


def _fold_deprecated_engine(
    engine_d: dict[str, Any], enabled: list[str], plugin_settings: dict[str, dict[str, Any]]
) -> None:
    """Translate a legacy ``[engine]`` block into the plugin surface in place.

    ``[engine] name = "unity"`` becomes ``[plugins] enabled = ["unity"]`` plus a
    ``[plugins.unity]`` table carrying editor_mode/mcp/unity_path/ready_*; the
    editor_mode↔scm.isolation coupling is now validated by the plugin itself
    (``UnityPlugin.validate``), not here. A no-op when ``[engine]`` is absent or
    its ``name`` is empty (the old "disabled" state)."""
    if not engine_d:
        return
    warnings.warn(
        "[engine] in policy.toml is deprecated; the game-engine layer is now a "
        'plugin. Use [plugins] enabled = ["unity"] with a [plugins.unity] table. '
        "[engine] will be removed in a future release.",
        DeprecationWarning,
        stacklevel=3,
    )
    name = _typed_str(engine_d, "engine", "name", "").strip()
    if not name:
        return
    if name not in enabled:
        enabled.append(name)
    folded = {k: engine_d[k] for k in _ENGINE_SETTING_KEYS if k in engine_d}
    # explicit [plugins.<name>] values take precedence over the folded [engine] ones
    plugin_settings[name] = {**folded, **plugin_settings.get(name, {})}


POLICY_TEMPLATE = """\
# bmad-loop orchestration policy. All keys optional; defaults shown.

[gates]
mode = "per-epic"            # none | per-epic | per-story-spec-approval
retrospective = "notify"     # never | notify | auto (auto unsupported in v1)

[limits]
max_review_cycles = 3
max_dev_attempts = 2
max_followup_reviews = 1     # additional review rounds granted solely because a finalized (status: done) round still recommended a follow-up; once spent, such a round converges + refiles the recommendation instead of burning another cycle. 0 = never honor a pass's own recommendation
session_timeout_min = 90
git_timeout_s = 120          # bound on any single git subprocess; exceeding it pauses/degrades (never crashes the run) — raise on a loaded host or a very large worktree
teardown_grace_s = 20        # verified teardown: poll a killed session window up to this long, then force-kill its pane pids and re-kill (#157). 0 = single unverified best-effort kill
stop_without_result_nudges = 1
dev_stall_grace_s = 600      # silence grace armed at dev/review launch and re-armed by transport activity or fresh Stop/idle evidence before bounded recovery. 0 = no launch timer, but a result-less turn end still fails fast
dev_stall_nudges = 2         # best-effort wake nudges per silent grace before stalling; fresh Stop/idle evidence restores this budget. 0 = stall on grace expiry
dev_stall_nudges_cap = 6     # total (never-restored) nudge bound per dev/review session; bounds launch-time recovery and Stop/idle budget refills because an accepted nudge does not guarantee a wake. 0 = stall on first grace expiry
workflow_stall_nudges_cap = 3 # total (never-restored) stall nudges for an injected plugin-workflow session before it is called stalled; bounds a session that finished its work but never wrote its completion marker. 0 = stall on first grace expiry
dev_contract_nudge = true    # true: one targeted nudge per session (#276) when a Stop finds a spec finalized to a terminal frontmatter status but missing its `## Auto Run Result` marker, asking the skill to append it and end its turn; sent exactly once, never refilled, touches no stall counters. false: rely only on harness-side frontmatter synthesis
max_tokens_per_story = 2000000
cache_read_weight = 0.1      # cache reads bill at ~0.1x input on all vendors; 1.0 = count raw
session_budget_mode = "warn"  # off | warn | enforce — weighted per-SESSION cap sampled every ~30s mid-session; warn = one ATTENTION + breadcrumb; enforce = wrap-up nudge (best-effort courtesy; the kill is the guarantee) then over_budget termination (retry→defer). Live-verified on claude; other transcript parsers sample best-effort (mid-turn flush unverified); inert where no mid-session usage signal exists (usage_parser "none", copilot's shutdown-only flush)
max_tokens_per_session = 4000000 # weighted cap a single session may spend before the guard trips; healthy sessions run ~1-2.5M weighted, so the default trips only true runaways (#158)
session_budget_grace_s = 240 # enforce mode: seconds a tripped session gets to wrap up after the nudge before over_budget termination. 0 = terminate at trip, no nudge

[verify]
# Deterministic gates run by the orchestrator after a clean review, before commit.
commands = []                # e.g. ["pytest -q", "ruff check ."]
stream_capture_kb = 256      # per-stream cap (KiB) on the verifier stdout/stderr retained under the run's verify/ directory; the TAIL is kept and the journal records the full byte count plus a truncation flag. 0 = capture nothing (records still land, with null pointers)

[notify]
desktop = true               # notify-send (Linux) / osascript (macOS) / PowerShell toast (Windows), best-effort
file = true                  # ATTENTION file in the run dir

[review]
# enabled = true  -> run a follow-up review session (the dev primitive re-invoked
#                    on the done spec for a fresh review pass) after a dev pass.
# enabled = false -> skip that session; the dev pass's own inline review
#                    is the only review and it finalizes the story straight to done.
enabled = true
# trigger (only consulted when enabled = true) decides WHEN that session runs:
#   "recommended" -> only when the dev pass flags the story with
#                    `followup_review_recommended: true` (it self-reviews inline
#                    and flags this when its changes warrant an independent pass).
#   "always"      -> run the second-opinion review on every story.
# The loop is bounded by limits.max_review_cycles (hard cap) and damped by
# limits.max_followup_reviews (a round that finalizes the story yet keeps
# recommending a follow-up converges + refiles once the grant is spent) either way.
trigger = "recommended"
# What a timeout-like review verdict (timeout/stalled/over_budget) costs:
#   "retry"           -> burn a review cycle per timeout until max_review_cycles (default).
#   "salvage-if-done" -> if the dev product is already finalized and verify passes,
#                        commit it and refile the outstanding follow-up to deferred work.
#   "defer"           -> give up on the first timeout-like verdict.
on_timeout = "retry"
# What a review that writes sprint-status back off `done` (the sign-off the
# orchestrator recorded at dev time) costs. Nothing in the review loop
# re-advances the board, so every remaining cycle re-reads the same failure:
#   "escalate" -> pause the run naming both sides of the disagreement (default).
#   "retry"    -> legacy: burn review cycles to max_review_cycles, then defer.
on_status_contradiction = "escalate"

[stories]
# Story-queue source. "sprint-status" (default) walks sprint-status.yaml written
# by bmad-sprint-planning. "stories" opts into folder+id dispatch: the loop reads
# a typed stories.yaml (Story Breakdown output, sibling of SPEC.md) and dispatches
# each entry by spec-folder + story id. `bmad-loop run --spec <folder>` forces
# stories mode for a single run regardless of this setting.
source = "sprint-status"     # sprint-status | stories
# Required (and must parse) when source = "stories": the project-relative path to
# the epic's spec folder holding stories.yaml + SPEC.md. Ignored under sprint-status.
spec_folder = ""

[adapter]
name = "claude"              # claude | codex | gemini | copilot | antigravity | opencode-http (alias: opencode) | <custom .bmad-loop/profiles/*.toml>
model = ""                   # empty = CLI default model (opencode-http wants "provider/model")
cleanup_session_on_finish = true  # kill the run's tmux session when it finishes (false keeps it for inspection)
# extra_args replaces the profile's default permission-bypass flags when set:
# extra_args = ["--permission-mode", "bypassPermissions"]
# Optional overrides of the CLI profile's own defaults (unset = inherit the
# profile's shipped value; copilot ships usage_grace_s = 8 and
# stop_without_result_nudges = 5):
# usage_grace_s = 8.0                # seconds to poll the transcript for token usage after a session ends
# stop_without_result_nudges = 5     # result-less Stop signals tolerated before a session is called stalled

# Per-stage overrides for the dev, review and sweep-triage passes. Unset keys
# inherit from [adapter] when the stage runs the same client; a stage that
# switches client falls back to that profile's defaults instead (model and
# extra_args are client-specific). Stage tables must come after the [adapter]
# keys above.
# [adapter.dev]
# model = "opus"
# [adapter.review]
# name = "codex"
# model = "gpt-5-codex"
# stop_without_result_nudges = 5     # e.g. a multi-turn review needs more nudges than dev
# [adapter.triage]
# model = "opus"

[sweep]
# Deferred-work sweep: triage + execute open deferred-work.md entries.
auto = "never"               # never | per-epic | run-end (auto-triggered sweeps never prompt)
max_bundles = 5              # bundles executed per sweep; triage excess is truncated
max_triage_attempts = 2      # triage validation retries before escalating
max_migration_attempts = 2   # legacy-ledger migration retries before escalating
repeat = false               # after a cycle completes, re-triage and continue on newly deferred work
max_cycles = 5               # safety cap on total cycles per sweep run when repeat = true

[cleanup]
# Disk reclamation for .bmad-loop/runs. Only terminal (finished/stopped) runs are
# ever touched; paused/interrupted runs stay intact so they remain resumable.
# `bmad-loop clean` applies these, and every run/sweep start reconciles worktrees
# leaked by a mid-flight stop.
run_retention = 10           # newest concluded runs kept whole; older ones are trimmed/archived (0 keeps none)
retention_days = 0           # 0 = disabled; else also keep runs newer than N days regardless of count
trim_artifacts = true        # drop the heavy worktrees/ tree from concluded runs (run stays viewable in the TUI)
archive_old = true           # archive (.bmad-loop/archive/<id>.tar.gz) rather than hard-delete runs past the window
auto_clean_on_finish = true  # reconcile stale worktrees + apply retention when a run finishes cleanly
clean_tmp = true             # let engine plugins clean their /tmp scratch on finish (e.g. Unity MCP server zips)

[scm]
# Source-control isolation + merge-back. Defaults reproduce today's behavior:
# work happens in place on the checked-out branch, with no branches.
isolation = "none"           # none | worktree
branch_per = "story"         # story | run (worktree mode only; "run" forces delete_branch = false)
target_branch = ""           # "" = the branch checked out at run start
merge_strategy = "merge"     # ff | merge | squash (worktree mode merges the unit branch into target locally)
delete_branch = true         # delete the unit branch after a successful merge
keep_failed = true           # keep a failed unit's worktree+branch for inspection
rollback_on_failure = false  # in-place (isolation="none") recovery after a failed attempt. false = never touch the tree; pause with manual recovery steps. true = auto-revert the attempt's tracked changes + remove only the untracked files this run created (WARNING: discards the attempt's uncommitted work; never a blanket git clean). Governs unattended/stopped attempts only: a resolved escalation's re-drive always auto-recovers regardless (reverts the failed source, keeps the corrected spec). Prefer isolation="worktree" to avoid touching your main checkout.
preserve_keep = 20           # attempt-preserve/* branches and attempt-preserve-dirty/* snapshots kept at run start (per family), newest by committer date; the tail is deleted (0 = never prune)
failed_diff_max_mb = 5       # per-file size cap (MB) for untracked files in a kept-failed unit's changes.patch; oversized files are skipped with a marker
failed_diff_unlimited = false # true = capture the failed-unit diff with no size cap (may produce very large patches; warns when active)
# commit_message_template: when set, the commit message dev sessions use for a
# story's commit. {story_key}, {run_id} and {story_title} (the spec's `title:`
# frontmatter, else a first `#` heading, minus any "Story <id>:" label; falls back to the
# key) are substituted.
# Empty = built-in default.
commit_message_template = ""
max_parallel = 1             # units in flight at once (parallel fan-out unbuilt; values > 1 clamp to 1)
# A git worktree checks out tracked files only, so gitignored MCP/CLI configs are
# absent from every fresh worktree and isolated sessions can't reach their MCP
# server. seed_adapter_defaults copies each loaded adapter's own config files
# (claude -> .mcp.json/.claude/settings.json, codex -> .codex/config.toml, etc.)
# into the worktree; worktree_seed adds extra project-specific gitignored paths.
seed_adapter_defaults = true # seed each loaded adapter's default gitignored configs into worktrees
worktree_seed = []           # extra gitignored files to copy into each worktree, on top of adapter defaults

[plugins]
# Plugin trust allowlist. A plugin dropped under .bmad-loop/plugins/<name>/ loads
# its declarative manifest (settings + out-of-process shell hooks) automatically.
# A plugin that ships an in-process [python] module is NEVER imported or run
# unless its name is listed here. Empty = no plugins trusted (today's behavior).
enabled = []                 # e.g. ["unity", "my-lint-plugin"]

# The game-engine layer is a plugin. For a Unity project whose dev/sweep cycle
# drives a live Editor via an Editor MCP, enable it above and configure it here:
#   [plugins.unity]
#   editor_mode = "shared"       # shared (live Editor; requires scm.isolation = "none")
#                                # | per_worktree (one Editor per worktree; requires
#                                #   scm.isolation = "worktree")
#   mcp = "ivanmurzak"           # which Editor MCP the scripts target: ivanmurzak | coplaydev
#   unity_path = ""              # Editor binary for a per_worktree launch ("" = auto-detect)
#   ready_timeout_sec = 600      # how long the readiness gate waits for the Editor + MCP
#   ready_grace_sec = -1         # delay before the first probe (-1 = auto: per_worktree waits)
# (The legacy [engine] block still loads with a deprecation warning, folded into
#  [plugins.unity] — migrate to [plugins] when convenient.)

[tui]
# low_frame_rate = true caps Textual to 15fps and disables animations (sets
# TEXTUAL_FPS=15 / TEXTUAL_ANIMATIONS=none at launch). Fixes repaint tearing
# over slow/high-latency links (SSH, Tailscale). Equivalent to launching with
# `bmad-loop tui --low-frame-rate`. Takes effect the next time the TUI starts.
low_frame_rate = false
# Persisted dashboard pane sizes, in terminal cells. 0 = unset (use the built-in
# default proportions). The TUI writes these when you resize a pane by mouse-drag
# or the Ctrl+W resize mode; they are re-applied on the next launch. Usually you
# won't hand-edit these — resize in the TUI instead.
# left_width = 0        # sidebar width, columns
# runs_height = 0       # Runs pane height, rows
# deferred_height = 0   # Deferred pane height, rows
# tasks_height = 0      # Tasks table height, rows

[operator]
# Let a dev session park a story at `awaiting-operator`: its agent-doable work is
# finished and COMMITTED, but its acceptance criteria include external actions
# only a human can perform (buy a domain, publish a DNS record, grant an API
# key). The story commits, the run moves on to the next one, and what is owed is
# recorded in the story spec's `operator_actions:` frontmatter. Complete such a
# story with `bmad-loop confirm <story-key>` once you have done those actions.
# Turn this off to hold sessions to the two older outcomes (done / blocked).
enabled = true

[mux]
# Terminal-multiplexer backend for this machine (the transport axis — which
# tmux-like program hosts sessions; independent of [adapter], the coding-CLI
# axis). Machine-specific: `bmad-loop init` gitignores policy.toml, so this
# choice never travels to teammates on other machines or OSes.
# Unset = auto-select: the BMAD_LOOP_MUX_BACKEND env var wins, then this key,
# then the platform default (win32: psmux, elsewhere: tmux) when installed,
# then the first registered backend that matches this platform and is
# available. Naming a backend that is not registered fails loudly at launch.
# `bmad-loop mux` lists backends and shows the selection; `bmad-loop mux set
# <name>` writes this key. Takes effect on the next bmad-loop invocation.
# backend = "tmux"
"""


def write_mux_backend(path: Path, name: str | None, *, confine_root: Path) -> None:
    """Persist (``name``) or clear (``None``) the ``[mux] backend`` key in the
    policy file at ``path``, preserving every other byte — devs hand-edit
    policy.toml, and the core install has no comment-preserving TOML writer
    (tomlkit ships only with the [tui] extra). A missing file is created from
    :data:`POLICY_TEMPLATE` so the written file keeps the full documentation.

    The template anchors the key as a single ``# backend = "tmux"`` line under
    ``[mux]`` with all prose comments *above* it, so the rewrite is a targeted
    line replace: the first (possibly commented) ``backend =`` line inside
    ``[mux]`` is swapped for the new value, or re-commented on clear. A file
    predating the ``[mux]`` table gets the table appended at EOF (TOML tables
    are order-free, so appending is always safe).

    ``confine_root`` is a REQUIRED keyword (#593). This function is handed a
    ``path`` and has no project of its own to derive a root from, so requiring
    the tree the policy file belongs to makes a caller that has not decided one
    a type error rather than a write that resolves ``.bmad-loop/`` by name."""
    if name is not None and not _MUX_NAME_RE.match(name):
        raise PolicyError(
            f"mux.backend must be a backend name (letters, digits, . _ -): got {name!r}"
        )
    # bytes in / bytes out: text mode would translate a CRLF file's endings.
    text = path.read_bytes().decode("utf-8") if path.is_file() else POLICY_TEMPLATE
    new_line = f'backend = "{name}"' if name is not None else '# backend = "tmux"'

    section = ""
    replaced = False
    mux_header_at: int | None = None
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        header = _TOML_SECTION_RE.match(line)
        if header:
            section = header.group("name").strip()
            out.append(line)
            if section == "mux" and mux_header_at is None:
                mux_header_at = len(out) - 1
            continue
        if not replaced and section == "mux" and _MUX_KEY_RE.match(line):
            stripped = line.rstrip("\r\n")
            ending = line[len(stripped) :] or "\n"
            # backend names never contain '#' (_MUX_NAME_RE), so any '#' after
            # '=' on this line is a hand-added trailing comment worth keeping.
            hash_idx = stripped.find("#", stripped.index("="))
            trailing = ("  " + stripped[hash_idx:]) if hash_idx != -1 else ""
            out.append(new_line + trailing + ending)
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if mux_header_at is not None:  # [mux] table present but the key line was deleted
            out.insert(mux_header_at + 1, new_line + "\n")
        else:  # policy file predating the [mux] table
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f"\n[mux]\n{new_line}\n")
    result = "".join(out)

    # Round-trip guard: never write a file this module can't read back to the
    # intended value (catches an anchor/regex drift before it corrupts config).
    parsed = loads(result)
    if parsed.mux.backend != (name or ""):
        raise PolicyError(
            f"internal error: rewriting {path} would read back "
            f"mux.backend = {parsed.mux.backend!r}, expected {(name or '')!r}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # #363: the helper removes its temp on any raise — the hand-rolled tmp here was
    # the fixed name `.bmad-loop/policy.toml.tmp`, which nothing gitignores (the
    # ignore line is the anchored literal `.bmad-loop/policy.toml`) and which
    # `tui.settings.PolicyDoc.save` built identically, so the two could collide.
    # The BYTES helper, not the text one: this function reads bytes and writes bytes
    # on purpose (see the decode above) so a CRLF policy.toml keeps its endings.
    # Confined, and the BYTES arm of it: replacing the name (as the bare replace
    # did) clobbers a link planted at policy.toml, which `runsetup` says a driven
    # session can write — but it left every directory above resolved by name, so a
    # link at `.bmad-loop/` aimed this write out of the project entirely (#593).
    # require_writable_target restores the PermissionError this raised on an
    # operator's read-only policy.toml before the write went atomic (#597).
    atomic_write_bytes_confined(
        path,
        result.encode("utf-8"),
        confine_root=confine_root,
        require_writable_target=True,
    )
