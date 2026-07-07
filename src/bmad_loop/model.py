"""Core data model: story lifecycle phases, per-task records, run state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Phase(StrEnum):
    PENDING = "pending"
    DEV_RUNNING = "dev-running"
    DEV_VERIFY = "dev-verify"
    REVIEW_RUNNING = "review-running"
    REVIEW_VERIFY = "review-verify"
    COMMITTING = "committing"
    # sweep-only: the triage session classifying open deferred-work entries
    TRIAGE_RUNNING = "triage-running"
    TRIAGE_VERIFY = "triage-verify"
    DONE = "done"
    DEFERRED = "deferred"
    ESCALATED = "escalated"


TERMINAL_PHASES = frozenset({Phase.DONE, Phase.DEFERRED, Phase.ESCALATED})

# Pause stages recorded in RunState.paused_stage
PAUSE_SPEC_APPROVAL = "spec-approval"
PAUSE_EPIC_BOUNDARY = "epic-boundary"
PAUSE_ESCALATION = "escalation"
PAUSE_STORY_GATE = "story-gate"
# stories-mode HITL checkpoints (independent per story). PLAN fires after a
# spec_checkpoint story's plan-halt leg (ready-for-dev, awaiting human plan
# review before implementation); STORY fires after a done_checkpoint story's
# commit (skip-if-last). Both re-arm through the same resume path.
PAUSE_PLAN_CHECKPOINT = "plan-checkpoint"
PAUSE_STORY_CHECKPOINT = "story-checkpoint"


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def weighted_total(self, cache_read_weight: float) -> int:
        """Cost-proportional total: cache reads are billed at ~0.1x base input
        on all supported vendors (Anthropic/OpenAI/Gemini, June 2026), so raw
        totals mostly measure context re-reads; the budget discounts them."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + round(self.cache_read_tokens * cache_read_weight)
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenUsage":
        return cls(
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cache_read_tokens=int(d.get("cache_read_tokens", 0)),
            cache_creation_tokens=int(d.get("cache_creation_tokens", 0)),
        )


@dataclass
class SessionRecord:
    task_id: str
    role: str  # "dev" | "review"
    status: str  # SessionResult.status
    session_id: str | None = None
    transcript_path: str | None = None
    usage: TokenUsage | None = None
    # the session's parsed result payload, persisted so a durably-saved
    # completed session is actionable on resume, not just forensics
    result_json: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "status": self.status,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "usage": self.usage.to_dict() if self.usage else None,
            "result_json": self.result_json,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRecord":
        usage = d.get("usage")
        return cls(
            task_id=d["task_id"],
            role=d["role"],
            status=d["status"],
            session_id=d.get("session_id"),
            transcript_path=d.get("transcript_path"),
            usage=TokenUsage.from_dict(usage) if usage else None,
            result_json=d.get("result_json"),
        )


@dataclass
class StoryTask:
    story_key: str
    epic: int
    phase: Phase = Phase.PENDING
    attempt: int = 0
    review_cycle: int = 0
    # set from the bmad-dev-auto session's `followup_review_recommended`
    # frontmatter (PR #2505): when True and review.trigger = "recommended", the
    # orchestrator runs a follow-up review pass (bmad-dev-auto re-invoked on the
    # done spec); otherwise it skips it.
    followup_review_recommended: bool = False
    baseline_commit: str | None = None
    # untracked, non-ignored paths present at baseline capture (repo-relative
    # posix). On rollback only paths NOT in this set are removed, so files the
    # user already had on disk are never deleted. None = pre-upgrade run (no
    # snapshot); rollback then removes no untracked files at all.
    baseline_untracked: list[str] | None = None
    spec_file: str | None = None
    commit_sha: str | None = None
    defer_reason: str | None = None
    # set by runs.rearm_escalation: this task was re-armed out of ESCALATED for a
    # clean rebuild against the corrected spec (not a failed attempt). Lets the
    # resume-time manual-recovery notice describe the real cause; cleared once the
    # rebuild proceeds. Survives the resume serialization round-trip.
    rearmed: bool = False
    # latched True for the lifetime of a resolved-escalation re-drive (set when
    # _finish_inflight re-drives a `rearmed` task, cleared once the corrected spec
    # is committed). While set, every rollback preserves the BMAD artifact folders'
    # tracked content, so a mid-re-drive retry/defer reset can't silently revert
    # the human correction. Survives the resume serialization round-trip.
    resolved_redrive: bool = False
    # stories mode only: set when a spec_checkpoint story's plan-halt leg verified
    # (spec at ready-for-dev) and the run paused for human plan review. On resume
    # StoriesEngine._resume_after_dev_verify reads it to re-drive the implement leg
    # (rather than the base review+commit) and clears it. Survives the round-trip.
    plan_checkpoint_pending: bool = False
    # stories mode only: the durable "a human plan review is still owed" obligation
    # for a spec_checkpoint story. Latched at the story's first (leg-1) dispatch —
    # BEFORE the session runs and keyed off the entry's spec_checkpoint flag, not
    # the leg's on-disk status or result — so it survives a crash, a non-fixable
    # retry, or a skill that overran `Halt after planning.`, none of which the
    # on-disk-status-keyed _plan_halt_leg / result-keyed plan_checkpoint_pending
    # carry across. Cleared ONLY when a plan-review pause actually raises (the
    # obligation is discharged). While set after a dev leg that did not itself pause,
    # StoriesEngine pauses before commit so the story can never commit un-reviewed.
    plan_review_owed: bool = False
    # stories mode only: the fixed slug ("unresolved" / "ambiguous") of a pre-planning
    # halt sentinel this task was detected as — recorded at detection time (pick-time
    # wedge or post-dev read-back), NOT re-derived from the spec_file basename at
    # re-arm. runs.rearm_escalation deletes a sentinel only when this is set, so a real
    # story spec that merely happens to be named `<key>-unresolved.md`, or a
    # non-sentinel escalation whose spec matches the convention, is status-flipped and
    # kept, never deleted. "" = not a sentinel. Survives the round-trip.
    sentinel_kind: str = ""
    # sweep bundles only: the deferred-work ids this task closes and the
    # rendered intent file handed to dev sessions
    dw_ids: list[str] = field(default_factory=list)
    bundle_file: str | None = None
    # worktree-isolation mode only (scm.isolation = "worktree"): the unit's
    # mounted worktree dir and branch, recorded so a paused/crashed run can
    # reconstruct or discard the in-flight worktree on resume.
    worktree_path: str = ""
    branch: str = ""
    sessions: list[SessionRecord] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def record_session(self, record: SessionRecord) -> None:
        self.sessions.append(record)
        if record.usage:
            self.tokens.add(record.usage)

    def attach_session_usage(self, task_id: str, usage: TokenUsage | None) -> None:
        """Fold usage into the most recent session for `task_id`. Usage is
        best-effort metadata attached after the session itself is saved, so a
        failed usage read never costs the recorded session."""
        if usage is None:
            return
        for record in reversed(self.sessions):
            if record.task_id != task_id:
                continue
            if record.usage is None:
                record.usage = usage
                self.tokens.add(usage)
            return
        raise KeyError(task_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_key": self.story_key,
            "epic": self.epic,
            "phase": str(self.phase),
            "attempt": self.attempt,
            "review_cycle": self.review_cycle,
            "followup_review_recommended": self.followup_review_recommended,
            "baseline_commit": self.baseline_commit,
            "baseline_untracked": self.baseline_untracked,
            "spec_file": self._serialized_spec_file(),
            "commit_sha": self.commit_sha,
            "defer_reason": self.defer_reason,
            "rearmed": self.rearmed,
            "resolved_redrive": self.resolved_redrive,
            "plan_checkpoint_pending": self.plan_checkpoint_pending,
            "plan_review_owed": self.plan_review_owed,
            "sentinel_kind": self.sentinel_kind,
            "dw_ids": self.dw_ids,
            "bundle_file": self.bundle_file,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "sessions": [s.to_dict() for s in self.sessions],
            "tokens": self.tokens.to_dict(),
        }

    def _serialized_spec_file(self) -> str | None:
        """In worktree mode the spec lives inside the unit's worktree; persist it
        relative to the worktree root so a kept-failed run's state.json stays
        portable if the worktree is later moved (and is never a dangling absolute
        path into a since-pruned worktree). In-place mode stores it verbatim."""
        if not self.spec_file or not self.worktree_path:
            return self.spec_file
        try:
            # as_posix: persist the relative path with forward slashes so state.json
            # stays portable across OSes (matches the in-worktree spec layout).
            return Path(self.spec_file).relative_to(self.worktree_path).as_posix()
        except ValueError:
            return self.spec_file  # spec lives outside the worktree; keep absolute

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryTask":
        return cls(
            story_key=d["story_key"],
            epic=int(d["epic"]),
            phase=Phase(d["phase"]),
            attempt=int(d.get("attempt", 0)),
            review_cycle=int(d.get("review_cycle", 0)),
            followup_review_recommended=bool(d.get("followup_review_recommended", False)),
            baseline_commit=d.get("baseline_commit"),
            baseline_untracked=(
                [str(p) for p in d["baseline_untracked"]]
                if d.get("baseline_untracked") is not None
                else None
            ),
            spec_file=d.get("spec_file"),
            commit_sha=d.get("commit_sha"),
            defer_reason=d.get("defer_reason"),
            rearmed=bool(d.get("rearmed", False)),
            resolved_redrive=bool(d.get("resolved_redrive", False)),
            plan_checkpoint_pending=bool(d.get("plan_checkpoint_pending", False)),
            plan_review_owed=bool(d.get("plan_review_owed", False)),
            sentinel_kind=str(d.get("sentinel_kind", "")),
            dw_ids=[str(i) for i in d.get("dw_ids", [])],
            bundle_file=d.get("bundle_file"),
            worktree_path=str(d.get("worktree_path", "")),
            branch=str(d.get("branch", "")),
            sessions=[SessionRecord.from_dict(s) for s in d.get("sessions", [])],
            tokens=TokenUsage.from_dict(d.get("tokens", {})),
        )


@dataclass
class RunState:
    run_id: str
    project: str
    started_at: str
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    current_epic: int | None = None
    # the run's story scope + cap, as passed on the launching CLI (`--epic`,
    # `--story`, `--max-stories`). Persisted so `resume` rebuilds the Engine with
    # the SAME selector — otherwise a resumed `--epic N` run silently widens to
    # every epic and can jump out of its scope at the next pick.
    epic_filter: int | None = None
    story_filter: str | None = None
    max_stories: int | None = None
    paused_reason: str | None = None
    paused_stage: str | None = None
    paused_story_key: str | None = None
    finished: bool = False
    # deliberately stopped (bmad-loop stop / engine SIGTERM); distinct from a
    # crash. Resume clears it via clear_pause(), so a stopped run is resumable.
    stopped: bool = False
    # an unexpected exception escaped Engine.run() and was recorded (crash.txt +
    # run-crash journal). Distinct from `stopped`; resume clears it via
    # clear_pause() so a crashed run re-arms like a stopped one. crash_error is a
    # short "Type: message" for display; the full traceback lives in crash.txt.
    crashed: bool = False
    crash_error: str | None = None
    run_type: str = "story"  # "story" | "sweep" — resume/status dispatch on it
    # story-queue source (policy.StoriesPolicy.source), pinned at run start so
    # resume/resolve rebuild the right engine (StoriesEngine vs the sprint Engine)
    # without re-reading policy — a policy edit mid-run must not switch a live run's
    # mode. `run_type` stays "story" for both; `source` selects the picker.
    source: str = "sprint-status"
    # stories mode only: the project-relative (or absolute) spec folder holding
    # stories.yaml + SPEC.md. Empty under sprint-status.
    spec_folder: str = ""
    # sweep runs only: the triage->bundles cycle in progress; 1 maps to the
    # legacy (unsuffixed) artifact names so old paused runs resume unchanged
    sweep_cycle: int = 1
    # auto-sweep triggers already fired this run (e.g. "epic-1", "run-end");
    # guards re-fire on resume
    sweeps_triggered: list[str] = field(default_factory=list)
    # worktree-isolation mode only: the branch every unit merges back into,
    # resolved once at run start (default = the branch checked out then) and
    # pinned so resume keeps targeting the same branch.
    target_branch: str = ""
    # free-form scratch space shared across plugin hooks (HookContext.shared).
    # Persisted so a plugin's cross-stage state survives pause/resume; values
    # MUST be JSON-serializable. Empty + untouched on a zero-plugin run.
    plugin_shared: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, StoryTask] = field(default_factory=dict)

    @property
    def paused(self) -> bool:
        return self.paused_reason is not None

    def handled_keys(self) -> set[str]:
        """Story keys this run already drove to a terminal phase."""
        return {k for k, t in self.tasks.items() if t.terminal}

    def clear_pause(self) -> None:
        self.paused_reason = None
        self.paused_stage = None
        self.paused_story_key = None
        self.stopped = False
        self.crashed = False
        self.crash_error = None

    def cache_read_weight(self) -> float:
        """The run's cache-read weight from its persisted policy snapshot; the
        product default (policy.LimitsPolicy.cache_read_weight = 0.1) when the
        snapshot predates the field or is malformed. Lets the TUI show the same
        weighted total the engine's budget uses without importing Policy."""
        limits = self.policy_snapshot.get("limits")
        if isinstance(limits, dict):
            try:
                return float(limits["cache_read_weight"])
            except (KeyError, TypeError, ValueError):
                pass
        return 0.1

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "started_at": self.started_at,
            "policy_snapshot": self.policy_snapshot,
            "current_epic": self.current_epic,
            "epic_filter": self.epic_filter,
            "story_filter": self.story_filter,
            "max_stories": self.max_stories,
            "paused_reason": self.paused_reason,
            "paused_stage": self.paused_stage,
            "paused_story_key": self.paused_story_key,
            "finished": self.finished,
            "stopped": self.stopped,
            "crashed": self.crashed,
            "crash_error": self.crash_error,
            "run_type": self.run_type,
            "source": self.source,
            "spec_folder": self.spec_folder,
            "sweep_cycle": self.sweep_cycle,
            "sweeps_triggered": self.sweeps_triggered,
            "target_branch": self.target_branch,
            "plugin_shared": self.plugin_shared,
            "tasks": {k: t.to_dict() for k, t in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        return cls(
            run_id=d["run_id"],
            project=d["project"],
            started_at=d["started_at"],
            policy_snapshot=d.get("policy_snapshot", {}),
            current_epic=d.get("current_epic"),
            epic_filter=d.get("epic_filter"),
            story_filter=d.get("story_filter"),
            max_stories=d.get("max_stories"),
            paused_reason=d.get("paused_reason"),
            paused_stage=d.get("paused_stage"),
            paused_story_key=d.get("paused_story_key"),
            finished=bool(d.get("finished", False)),
            stopped=bool(d.get("stopped", False)),
            crashed=bool(d.get("crashed", False)),
            crash_error=d.get("crash_error"),
            run_type=str(d.get("run_type", "story")),
            source=str(d.get("source", "sprint-status")),
            spec_folder=str(d.get("spec_folder", "")),
            sweep_cycle=int(d.get("sweep_cycle", 1)),
            sweeps_triggered=[str(s) for s in d.get("sweeps_triggered", [])],
            target_branch=str(d.get("target_branch", "")),
            plugin_shared=dict(d.get("plugin_shared", {})),
            tasks={k: StoryTask.from_dict(t) for k, t in d.get("tasks", {}).items()},
        )
