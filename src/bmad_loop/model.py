"""Core data model: story lifecycle phases, per-task records, run state."""

# Strict-checked under #245 Stage 2, with the two rules below relaxed for this
# file only. `reportUnknownVariableType`: the dataclass collection fields use the
# idiomatic `field(default_factory=list|dict)`, which pyright can only infer as
# `list[Unknown]` / `dict[Unknown, Unknown]` (it does not fold the declared
# annotation back into the factory) though the fields are correctly typed.
# `reportUnknownArgumentType`: `from_dict` / snapshot readers pull values out of
# run-persisted `dict[str, Any]`, so isinstance-narrowing an `Any` value yields
# Unknown at that boundary. Both are inherent to the persistence edge, not
# annotation drift; every other strict rule stays on.
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import base64
from copy import deepcopy
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
    # the story's agent-doable work is finished and COMMITTED, but its acceptance
    # criteria include external actions only a human can perform (buy a domain,
    # publish a DNS record). Terminal like DONE — the run moves on rather than
    # halting — but not DONE: the outstanding actions are recorded on the task.
    # Deliberately NOT a pause: an operator-owed story must never block the
    # stories behind it (contrast the spec's `Block If:` -> blocked -> CRITICAL
    # -> pause channel, which is run-halting by design).
    AWAITING_OPERATOR = "awaiting-operator"


# Terminal = the orchestrator will not drive this story further in this run. It
# says nothing about success: DONE and AWAITING_OPERATOR carry a commit, DEFERRED
# and ESCALATED do not.
TERMINAL_PHASES = frozenset({Phase.DONE, Phase.DEFERRED, Phase.ESCALATED, Phase.AWAITING_OPERATOR})

# Pause stages recorded in RunState.paused_stage
PAUSE_SPEC_APPROVAL = "spec-approval"
PAUSE_EPIC_BOUNDARY = "epic-boundary"
PAUSE_ESCALATION = "escalation"
# Raised by Engine._refuse_gated_story: the picked story is named by the `gate:`
# line of a deferred-work entry that has not landed. Produced before the story is
# recorded in state.tasks, so a resume re-picks it and re-reads the ledger.
PAUSE_STORY_GATE = "story-gate"
# stories-mode HITL checkpoints (independent per story). PLAN fires after a
# spec_checkpoint story's plan-halt leg (ready-for-dev, awaiting human plan
# review before implementation); STORY fires after a done_checkpoint story's
# commit (skip-if-last). Both re-arm through the same resume path.
PAUSE_PLAN_CHECKPOINT = "plan-checkpoint"
PAUSE_STORY_CHECKPOINT = "story-checkpoint"

# Reasons recorded in RunState.sweeps_refused (trigger -> reason). A CLOSED
# vocabulary of short slugs, deliberately not a formatted exception: `bmad-loop
# diagnose` renders run state through `sanitize.guard`, which *raises*
# LeakDetected on a home-path hit rather than redacting it (genuine PII never
# auto-repairs — sanitize.py). A free-form `str(e)` here would therefore make the
# dump fail outright on exactly the runs worth dumping, and the per-value
# `looks_like_identifier` filter would blank it anyway. Add a slug, never a
# message.
SWEEP_REFUSED_NOT_STARTED = "not-started"  # the launch raised before a child existed
SWEEP_REFUSED_FAILED = "failed"  # a child started, then failed
SWEEP_REFUSED_DIRTY = "dirty"  # the worktree was unclean, or `git status` faulted


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
    # resolved adapter identity for the session (#153 phase 1). adapter "" means
    # the record predates identity stamping; adapter set with model "" means the
    # session ran the CLI profile's default model (no explicit model override).
    adapter: str = ""
    model: str = ""
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
            "adapter": self.adapter,
            "model": self.model,
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
            adapter=str(d.get("adapter", "")),
            model=str(d.get("model", "")),
            session_id=d.get("session_id"),
            transcript_path=d.get("transcript_path"),
            usage=TokenUsage.from_dict(usage) if usage else None,
            result_json=d.get("result_json"),
        )


def _rebased_on(path: str | None, root: Path) -> str | None:
    """One persisted spec path, re-anchored on `root`; absolute values pass through.

    Split out so `StoryTask.rebase_spec_paths_on` states the rule once per field
    without repeating the guard, and so the guard itself is unmissable: the
    is-absolute test is what keeps an out-of-mount spec (persisted verbatim by
    `_serialized_worktree_path`) from being joined onto a root that does not
    contain it.
    """
    if not path or Path(path).is_absolute():
        return path
    return str(root / path)


@dataclass
class StoryTask:
    story_key: str
    epic: int
    phase: Phase = Phase.PENDING
    attempt: int = 0
    review_cycle: int = 0
    # count of review rounds granted *solely* because a completed round finalized
    # the story (status: done) yet still set `followup_review_recommended: true`.
    # Bounded by limits.max_followup_reviews: once spent, the next such round
    # force-converges (verify → refile the recommendation to the ledger → commit)
    # rather than burning another cycle. Reset to 0 by runs.rearm_escalation so a
    # human-resolved re-drive gets a fresh damping budget. Survives the round-trip.
    followup_reviews_spent: int = 0
    # How many times a human re-arm (`runs.rearm_escalation`) has re-opened this
    # task. Re-arm resets `attempt` to 0 and the next dispatch bumps it back to 1,
    # so without a discriminator the re-minted session task_id is byte-equal to a
    # record the ABANDONED attempt already appended to the append-only `sessions`
    # list — and `Engine._resumable_session`, which matches on that id, replays the
    # abandoned attempt's verdict for the fresh one (#705). Feeds
    # `engine._session_task_id`, which emits the suffix only above zero, so every
    # id already on disk stays byte-identical across the upgrade. `task.sessions`
    # is deliberately NOT cleared at re-arm: the run-dir audit trail it indexes is
    # read by a second resolve cycle.
    generation: int = 0
    # set from the bmad-build-auto session's `followup_review_recommended`
    # frontmatter (PR #2505): when True and review.trigger = "recommended", the
    # orchestrator runs a follow-up review pass (bmad-build-auto re-invoked on the
    # done spec); otherwise it skips it.
    followup_review_recommended: bool = False
    baseline_commit: str | None = None
    # untracked, non-ignored paths present at baseline capture (repo-relative
    # posix). On rollback only paths NOT in this set are removed, so files the
    # user already had on disk are never deleted. None = pre-upgrade run (no
    # snapshot); rollback then removes no untracked files at all.
    baseline_untracked: list[str] | None = None
    # Deferred-work bookkeeping is persisted before its readers land so an older
    # state.json remains resumable throughout the forward-port.  The nullable
    # snapshot text and its captured flag are deliberately separate: None means
    # "no ledger existed", while False means "no snapshot was taken".
    baseline_ledger_digest: str | None = None
    pre_harvest_ledger: str | None = None
    pre_harvest_ledger_captured: bool = False
    # Digest of the last ledger state THIS engine left on disk: the snapshot's
    # own text at capture, refreshed to the post-append bytes once the harvest
    # writes. It is the compare-and-set anchor `_restore_ledger` uses to tell
    # its own retractable write from a concurrent writer's (#286), which is why
    # it is persisted rather than kept in memory: a crash replay must be able to
    # recognize the dead attempt's append still sitting on disk.
    post_engine_ledger_digest: str | None = None
    harvest_wrote_ledger: bool = False
    ledger_changed_before_harvest: bool = False
    # JSON-native containers only; callers persist these through state.json.
    harvested_deferrals: list[dict[str, Any]] = field(default_factory=list)
    bundle_closes_intended: list[str] = field(default_factory=list)
    # `append_entry` kwargs for review-budget follow-ups this task filed into the
    # ACTIVE workspace's ledger, which under isolation is the unit worktree's.
    refiled_followups: list[dict[str, Any]] = field(default_factory=list)
    # Deferred-work ids a story DECLARED it closes (`closes_deferred:`), recorded at
    # the commit boundary. Same ledger, same isolation problem: a gitignored path
    # never merges out of the unit worktree, so the flip has to be re-applied.
    story_closes_intended: list[str] = field(default_factory=list)
    # The sprint-status stage `_post_dev_state_sync` REQUESTED for this story, or
    # None when it never ran (sweep bundles, stories mode, the legacy path). Same
    # isolation problem as the ledger payloads above, one file over: under
    # `isolation = "worktree"` that advance lands on the unit worktree's board,
    # which for a gitignored board is a seeded copy shielded from the unit commit,
    # so the post-merge carry has to re-apply it. Latest-wins — a scalar, not a
    # list, because the board holds one stage per story and only the accepted
    # attempt's stage can ever be carried.
    board_advance_intended: str | None = None
    # Index of the append-only primary dev SessionRecord whose initial decision or
    # later verify-repair result durably returned PROCEED. Attempt numbers can be
    # reused after a human re-arm, so the exact record occurrence is the acceptance
    # identity; None is legacy/unarmed.
    accepted_dev_session_index: int | None = None
    harvest_carry_commit_pending: bool = False
    isolated_ledger_carried: bool = False
    spec_file: str | None = None
    # The spec owned by the current/last dispatched dev attempt. Unlike
    # ``spec_file`` (the accepted/result artifact), this is bound before launch
    # so recovery can identify an attempt's lifecycle-only residue after a crash.
    dispatched_spec_file: str | None = None
    # Byte-exact input contents of ``dispatched_spec_file`` for the current retry
    # chain. The JSON representation is base64, so CRLF and non-UTF-8 bytes survive
    # a crash/resume round-trip. The chain's first bound input is retained across
    # fixable repairs, then restored before a fresh-baseline retry; in a resolved
    # re-drive this is the operator-corrected spec, so child-authored body edits can
    # never become the retained correction. Cleared after successful commit. None =
    # unbound attempt, retired chain, or legacy state.
    dispatched_spec_snapshot: bytes | None = None
    commit_sha: str | None = None
    # the external, human-only actions this story still owes when it parks at
    # Phase.AWAITING_OPERATOR — one free-text instruction per entry, as the dev
    # session enumerated them in the spec's `operator_actions:` frontmatter.
    # Plain strings in v1: a per-action deterministic `check:` command is a
    # deliberate v2 question, and strings-now/objects-later is the cheaper
    # migration than the reverse. Empty on every other phase: owing at least one
    # action is what selects AWAITING_OPERATOR over DONE when the park path picks
    # a committing story's final phase. That choice is the only enforcement —
    # nothing validates this field on load or on write, so a task carrying the
    # phase with no actions is unreachable rather than rejected. Survives the
    # resume serialization round-trip (it is the durable record of what the human
    # owes, and nothing re-derives it once the session that wrote the spec is
    # gone).
    operator_actions: list[str] = field(default_factory=list)
    # Whether THIS dev phase was in a position to newly elect a park: captured
    # once, on the fresh entry into `Engine._dev_phase` (`resume_result is None`),
    # from the same instant and the same condition as `baseline_commit` — so the
    # expectation and the diff it guards share one anchor. False when the bound
    # spec was ALREADY at `awaiting-operator` on entry (an earlier attempt's park
    # is on disk, so a park observed afterwards may be inherited rather than
    # elected), when parking is disabled, or when the spec could not be read at
    # all (fail closed). It gates exactly one thing: `verify_dev`'s proof-of-work
    # skip on the park leg (#335, #676). Every other park gate still selects on the
    # observed status alone, so an ineligible park with a real diff still passes.
    # Deliberately per-PHASE, not per-attempt: a fixable repair keeps the previous
    # session's tree, so re-observing would make every repair of a malformed park
    # ineligible and fail it on the gate it just re-armed.
    park_eligible: bool = False
    defer_reason: str | None = None
    # the recovery ref this attempt's work was parked on by the last auto-rollback
    # — an `attempt-preserve/*` branch (commits above baseline) or, when the tree
    # was also dirty, the `refs/attempt-preserve-dirty/*` snapshot, which is
    # parented at the attempt's HEAD and therefore subsumes the branch (last
    # writer wins, so one `git merge --ff-only <ref>` recovers the whole attempt
    # — unless `preserve_partial` is set). Set by RecoveryFlow, cleared at the top
    # of every auto-rollback so it can never name a *previous* attempt's ref; read
    # by `_defer` (notification) and projected into `status`. None = the last
    # auto-rollback parked nothing (no commits above baseline and a clean or
    # uncapturable tree, or the ref failed to take). Isolation-INDEPENDENT: a unit
    # worktree's own dev-retry rollback parks on the same shared refs, so a
    # deferred isolated unit can carry BOTH a kept-failed branch (the final
    # attempt) and a preserve_ref (an earlier, rolled-back one) — `_defer` names
    # both. The unit branch itself is never written here: a live branch is not a
    # parked snapshot. Not cleared on success — a mid-retry rollback's breadcrumb
    # stays readable. Survives the resume serialization round-trip.
    preserve_ref: str | None = None
    # set when the auto-rollback's *worktree* snapshot was attempted and raised
    # (journalled `attempt-worktree-preserve-failed`), so `preserve_ref` names an
    # `attempt-preserve/*` commits branch ALONE and the reset that followed
    # discarded the uncommitted half. False both when the snapshot succeeded (the
    # dirty ref subsumes the branch) and when the tree was clean (nothing to
    # capture, so the commits branch IS the whole attempt) — the ref name alone
    # cannot tell those apart, which is why this is recorded rather than derived.
    # Cleared with `preserve_ref`. Survives the resume serialization round-trip.
    preserve_partial: bool = False
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
    # intent-gap patch-restore re-drive (BMAD-METHOD #2564): a repo-relative-or-
    # absolute path to the patch file bmad-build-auto saved of the reverted attempt.
    # Latched by runs.rearm_escalation when the human confirms the attempted reading
    # was correct; the engine re-applies it onto the baseline after every reset of
    # the re-drive so the re-driven session resumes review (step-04) on the restored
    # diff, and clears it once the corrected work commits. None = ordinary
    # from-scratch re-drive. Survives the resume serialization round-trip.
    restore_patch: str | None = None
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
    # latched the first time this story's cost-weighted spend crossed
    # limits.max_tokens_per_story at a session boundary, so the advisory notice
    # fires once per STORY rather than once per session after the crossing
    # (every later session of an overrunning story is over the cap too). Never
    # cleared: the crossing is a fact about the story's spend, and the raw
    # counts it was computed from stay in `tokens`. Persisted precisely so a
    # resumed run does not re-notify what the pre-pause process already did.
    token_budget_warned: bool = False

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
            "followup_reviews_spent": self.followup_reviews_spent,
            "generation": self.generation,
            "followup_review_recommended": self.followup_review_recommended,
            "baseline_commit": self.baseline_commit,
            "baseline_untracked": self.baseline_untracked,
            "baseline_ledger_digest": self.baseline_ledger_digest,
            "pre_harvest_ledger": self.pre_harvest_ledger,
            "pre_harvest_ledger_captured": self.pre_harvest_ledger_captured,
            "post_engine_ledger_digest": self.post_engine_ledger_digest,
            "harvest_wrote_ledger": self.harvest_wrote_ledger,
            "ledger_changed_before_harvest": self.ledger_changed_before_harvest,
            "harvested_deferrals": self.harvested_deferrals,
            "bundle_closes_intended": self.bundle_closes_intended,
            "refiled_followups": self.refiled_followups,
            "story_closes_intended": self.story_closes_intended,
            "board_advance_intended": self.board_advance_intended,
            "accepted_dev_session_index": self.accepted_dev_session_index,
            "harvest_carry_commit_pending": self.harvest_carry_commit_pending,
            "isolated_ledger_carried": self.isolated_ledger_carried,
            "spec_file": self._serialized_worktree_path(self.spec_file),
            "dispatched_spec_file": self._serialized_worktree_path(self.dispatched_spec_file),
            "dispatched_spec_snapshot": (
                base64.b64encode(self.dispatched_spec_snapshot).decode("ascii")
                if self.dispatched_spec_snapshot is not None
                else None
            ),
            "commit_sha": self.commit_sha,
            "operator_actions": self.operator_actions,
            "park_eligible": self.park_eligible,
            "defer_reason": self.defer_reason,
            "preserve_ref": self.preserve_ref,
            "preserve_partial": self.preserve_partial,
            "rearmed": self.rearmed,
            "resolved_redrive": self.resolved_redrive,
            "plan_checkpoint_pending": self.plan_checkpoint_pending,
            "plan_review_owed": self.plan_review_owed,
            "sentinel_kind": self.sentinel_kind,
            "restore_patch": self.restore_patch,
            "dw_ids": self.dw_ids,
            "bundle_file": self.bundle_file,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "sessions": [s.to_dict() for s in self.sessions],
            "tokens": self.tokens.to_dict(),
            "token_budget_warned": self.token_budget_warned,
        }

    def _serialized_worktree_path(self, path: str | None) -> str | None:
        """Persist a worktree-local spec path relative to its mounted root.

        Both the accepted/result spec and the attempt-owned dispatched spec use
        this one normalization path so their state.json representations cannot
        drift. In-place and outside-worktree paths remain verbatim.
        """
        if not path or not self.worktree_path:
            return path
        try:
            # as_posix: persist the relative path with forward slashes so state.json
            # stays portable across OSes (matches the in-worktree spec layout).
            return Path(path).relative_to(self.worktree_path).as_posix()
        except ValueError:
            return path  # spec lives outside the worktree; keep absolute

    def release_spec_paths_from_mount(self) -> None:
        """Give up the spec ownership a mount being DISCARDED carried.

        The counterpart to :meth:`rebase_spec_paths_on`, and deliberately not its
        exact inverse — the two fields part company here because their roles do:

        * `dispatched_spec_file` / `dispatched_spec_snapshot` are the ATTEMPT's
          binding, the pair `recovery_flow` restores bytes through. The attempt died
          with its tree, so the binding has nothing left to name; clearing both
          together keeps the authority pair whole (a path without its snapshot is the
          one shape `_bind_dispatched_spec_for_attempt` never persists).
        * `spec_file` is the ACCEPTED artifact and outlives the attempt. The
          replacement mount will carry the same story's spec at the same
          mount-relative place, so the relative spelling is the one that re-resolves
          onto it — `verify.resolve_spec_path` probes a relative value against the
          live workspace and passes an absolute one through untouched.

        Leaving `spec_file` absolute into the deleted mount is what made the fresh
        attempt start UNBOUND: `_dispatched_spec_for_attempt` resolves it
        `strict=True`, the dead path raises, and the miss is silent because an
        unbound attempt is a legal state. `_record_dev_spec` cannot repair it either
        — it no-ops while `spec_file` is set.

        Uses the same relativization as `to_dict`, so the discarded-mount spelling
        and the persisted one cannot drift, which also means a spec OUTSIDE the mount
        stays verbatim: it was never the mount's to give up. MUST be called while
        `worktree_path` still names the mount.
        """
        self.dispatched_spec_file = None
        self.dispatched_spec_snapshot = None
        self.spec_file = self._serialized_worktree_path(self.spec_file)

    def release_mount_owned_state(self) -> None:
        """Give up EVERYTHING a mount owned: its spec ownership and the measurements
        taken inside it.

        One method because the two callers that stop using a mount — the restart
        discard and the isolation-flip arm — must give up the same set, and the second
        was written releasing only the spec half. That half-release is not a smaller
        version of the same thing, it is a different bug: `baseline_commit` and
        `baseline_untracked` are stamped from `self.workspace.root` (the unit under
        isolation), so leaving them set hands unit-mount operands to
        `recovery_flow.rollback_or_pause` running against the MAIN checkout. Neither
        fails loud there — linked worktrees share the object database, so the baseline
        still resolves and a reset onto it succeeds, while a fresh worktree is a
        tracked-only checkout whose empty untracked snapshot makes
        `verify._rollback_cleanup_plan` compute `untracked_files(repo) -
        baseline_untracked` as every untracked file in the operator's own checkout.
        Under an auto-recovering cause those are DELETED.

        Costs the re-run nothing: `_dev_phase` re-stamps both from whatever workspace
        it re-enters with, so clearing turns the `baseline_commit` leg into a correct
        no-op instead of a probe of the wrong tree.

        MUST be called while `worktree_path` still names the mount — the spec
        relativization is measured against it.
        """
        self.release_spec_paths_from_mount()
        self.baseline_commit = None
        self.baseline_untracked = None

    def rebase_spec_paths_on(self, root: Path) -> None:
        """Re-absolutize both spec-ownership paths against the tree that owns them.

        The read-side inverse of :meth:`_serialized_worktree_path`, and the single
        implementation of that rule: `to_dict` persists a worktree-local spec
        RELATIVE to the mount and `from_dict` reads it back raw, so a consumer that
        resolves the raw value against anything else names the wrong tree. The main
        checkout carries the same `_bmad-output/...` layout, so that wrong tree
        answers `is_file()` and passes containment — the failure is silent, not an
        error.

        Both fields move together because they are one asymmetry: `spec_file` is the
        accepted/result artifact and `dispatched_spec_file` the attempt-owned input,
        and a caller re-anchoring one and not the other leaves a task naming two
        trees at once.

        Idempotent: an absolute value is already anchored (a spec outside the mount
        is persisted verbatim) and passes through untouched, so re-running this
        against the same root cannot double-join. `root` is the tree the values were
        persisted relative to — `task.worktree_path` — never the caller's cwd or
        project.
        """
        self.spec_file = _rebased_on(self.spec_file, root)
        self.dispatched_spec_file = _rebased_on(self.dispatched_spec_file, root)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryTask":
        dispatched_spec_snapshot = d.get("dispatched_spec_snapshot")
        if dispatched_spec_snapshot is not None:
            try:
                dispatched_spec_snapshot = base64.b64decode(
                    str(dispatched_spec_snapshot).encode("ascii"),
                    validate=True,
                )
            except ValueError as exc:
                raise ValueError(
                    f"story {d.get('story_key')!r}: dispatched_spec_snapshot " "is not valid base64"
                ) from exc
        return cls(
            story_key=d["story_key"],
            epic=int(d["epic"]),
            phase=Phase(d["phase"]),
            attempt=int(d.get("attempt", 0)),
            review_cycle=int(d.get("review_cycle", 0)),
            followup_reviews_spent=int(d.get("followup_reviews_spent", 0)),
            generation=int(d.get("generation", 0)),
            followup_review_recommended=bool(d.get("followup_review_recommended", False)),
            baseline_commit=d.get("baseline_commit"),
            baseline_untracked=(
                [str(p) for p in d["baseline_untracked"]]
                if d.get("baseline_untracked") is not None
                else None
            ),
            baseline_ledger_digest=(
                str(d.get("baseline_ledger_digest"))
                if d.get("baseline_ledger_digest") is not None
                else None
            ),
            pre_harvest_ledger=(
                str(d.get("pre_harvest_ledger"))
                if d.get("pre_harvest_ledger") is not None
                else None
            ),
            pre_harvest_ledger_captured=bool(d.get("pre_harvest_ledger_captured", False)),
            post_engine_ledger_digest=(
                str(d.get("post_engine_ledger_digest"))
                if d.get("post_engine_ledger_digest") is not None
                else None
            ),
            harvest_wrote_ledger=bool(d.get("harvest_wrote_ledger", False)),
            ledger_changed_before_harvest=bool(d.get("ledger_changed_before_harvest", False)),
            harvested_deferrals=[deepcopy(dict(item)) for item in d.get("harvested_deferrals", [])],
            bundle_closes_intended=[str(i) for i in d.get("bundle_closes_intended", [])],
            refiled_followups=[deepcopy(dict(item)) for item in d.get("refiled_followups", [])],
            story_closes_intended=[str(i) for i in d.get("story_closes_intended", [])],
            board_advance_intended=(
                str(d["board_advance_intended"])
                if d.get("board_advance_intended") is not None
                else None
            ),
            accepted_dev_session_index=(
                int(d["accepted_dev_session_index"])
                if d.get("accepted_dev_session_index") is not None
                else None
            ),
            harvest_carry_commit_pending=bool(d.get("harvest_carry_commit_pending", False)),
            isolated_ledger_carried=bool(d.get("isolated_ledger_carried", False)),
            spec_file=d.get("spec_file"),
            dispatched_spec_file=d.get("dispatched_spec_file"),
            dispatched_spec_snapshot=dispatched_spec_snapshot,
            commit_sha=d.get("commit_sha"),
            operator_actions=[str(a) for a in d.get("operator_actions", [])],
            park_eligible=bool(d.get("park_eligible", False)),
            defer_reason=d.get("defer_reason"),
            preserve_ref=d.get("preserve_ref"),
            preserve_partial=bool(d.get("preserve_partial", False)),
            rearmed=bool(d.get("rearmed", False)),
            resolved_redrive=bool(d.get("resolved_redrive", False)),
            plan_checkpoint_pending=bool(d.get("plan_checkpoint_pending", False)),
            plan_review_owed=bool(d.get("plan_review_owed", False)),
            sentinel_kind=str(d.get("sentinel_kind", "")),
            restore_patch=d.get("restore_patch"),
            dw_ids=[str(i) for i in d.get("dw_ids", [])],
            bundle_file=d.get("bundle_file"),
            worktree_path=str(d.get("worktree_path", "")),
            branch=str(d.get("branch", "")),
            sessions=[SessionRecord.from_dict(s) for s in d.get("sessions", [])],
            tokens=TokenUsage.from_dict(d.get("tokens", {})),
            token_budget_warned=bool(d.get("token_budget_warned", False)),
        )


@dataclass
class RunState:
    run_id: str
    project: str
    started_at: str
    # The git root this run's code work happens in — `paths.repo_root`, which is
    # `paths.project` unless `_bmad/bmm/config.yaml` sets a `repo_root:` override.
    # Persisted because `runs.rearm_escalation` runs OUT OF PROCESS from the engine
    # and had only `project` to reach for, so it advanced the attempt baseline by
    # reading HEAD of a repo the proof-of-work gate never measures. Empty means a
    # state.json written before this field existed; `code_root` then falls back to
    # `project`, which is exactly the pre-upgrade behavior and the correct answer
    # for every run without the override.
    repo_root: str = ""
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    # SECONDARY copy of the host-exec baseline (#498) — runsetup.config_digest over
    # the agent-writable config that reaches HOST code execution: verify commands,
    # the resolved launch binary/args/env, the plugin allowlist (#461 point 4).
    #
    # The one resume TRUSTS is out of the tree (`runs.write_trusted_config_digest`),
    # because a baseline whose whole job is to police the agent-writable tree cannot
    # live in it — a session that rewrote policy.toml could blank this field in the
    # same breath and silence the warning `resume` owes the operator. So this copy is
    # never preferred: `_resume_paused_run` consults it ONLY when the state root
    # holds no file for the run, which is what keeps rewriting it pointless (the
    # #498 attack test asserts exactly that).
    #
    # It is still written, and must be, for the two cases where the out-of-tree file
    # is honestly absent rather than tampered away — in both, this copy is the run's
    # only surviving pin:
    #   * the state root is keyed by the project's RESOLVED PATH (`runs.project_tag`),
    #     so moving or renaming the project keys the run somewhere new and orphans
    #     its state subtree (FEATURES.md documents the GC half of this). state.json
    #     lives in the run dir and travels with it. Same for a BMAD_LOOP_STATE_DIR
    #     that changes between launch and resume.
    #   * a run PAUSED before #498 has its baseline here and nowhere else; the first
    #     resume under this code reads it and mints the out-of-tree file.
    # Dropping it would turn "this run has a pin" into "this run has none" in all of
    # them. Empty means what it always did — no prior pin, hence no warning — which
    # is why the resume compare is guarded on non-emptiness. The auto-sweep gate has
    # never read this field: it compares against its own in-memory closure baseline,
    # which no session can reach.
    trusted_config_digest: str = ""
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
    # auto-sweep triggers this run did NOT deliver, trigger -> SWEEP_REFUSED_*.
    # Kept apart from sweeps_triggered rather than folded into it: that list is
    # the re-fire latch, and widening it to a mapping would silently degrade the
    # per-element sanitizer loop in diagnostics.py. A trigger may appear in both
    # (SWEEP_REFUSED_FAILED = a child that started and then failed).
    sweeps_refused: dict[str, str] = field(default_factory=dict)
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

    @property
    def code_root(self) -> Path:
        """The tree git runs against for this run — ``repo_root`` when the run
        recorded one, else ``project``.

        The single reader of the pair, so an out-of-process consumer
        (``runs.rearm_escalation``) cannot pick the wrong one, and a pre-upgrade
        state.json (empty ``repo_root``) degrades to precisely what it did before
        rather than to a path that does not exist."""
        return Path(self.repo_root or self.project)

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
        weighted total the engine's budget uses without importing Policy.

        The snapshot is re-stamped at every engine start (run, sweep, resume), so
        on a resumed run this is the *resuming* process's weight, matching what
        that process enforces. Edit the weight and resume and the run's whole
        accumulated history re-weights — totals are recomputed from raw counts,
        and the budget has always judged cumulative counts at the live weight."""
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
            "repo_root": self.repo_root,
            "started_at": self.started_at,
            "policy_snapshot": self.policy_snapshot,
            "trusted_config_digest": self.trusted_config_digest,
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
            "sweeps_refused": self.sweeps_refused,
            "target_branch": self.target_branch,
            "plugin_shared": self.plugin_shared,
            "tasks": {k: t.to_dict() for k, t in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        return cls(
            run_id=d["run_id"],
            project=d["project"],
            repo_root=str(d.get("repo_root", "")),
            started_at=d["started_at"],
            policy_snapshot=d.get("policy_snapshot", {}),
            trusted_config_digest=str(d.get("trusted_config_digest", "")),
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
            sweeps_refused={str(k): str(v) for k, v in d.get("sweeps_refused", {}).items()},
            target_branch=str(d.get("target_branch", "")),
            plugin_shared=dict(d.get("plugin_shared", {})),
            tasks={k: StoryTask.from_dict(t) for k, t in d.get("tasks", {}).items()},
        )


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    reason: str = ""
    severity: str = ""  # "" | "CRITICAL" | "PREFERENCE" — set when not retryable
    # fixable failures carry concrete evidence (failing command output) that a
    # feedback-driven repair session can act on; non-fixable retries start over
    fixable: bool = False
    # the failure is the run environment's, not the story's (verify command
    # not found / not executable): no repair session can fix it and every
    # story shares the same commands, so it must never charge attempt budgets
    env_fault: bool = False
    # a session deliberately contradicted a state the orchestrator had already
    # established (a review revoking the sprint sign-off it advanced at dev
    # time): no further session can reconcile it, so it routes to a pause with
    # both sides named rather than to another cycle (#334)
    contradiction: bool = False
    # Whether this ACCEPTED outcome waived the dev gate's proof-of-work check on
    # the park leg (`verify_dev`'s two-part park selector fired). The fact of the
    # waiver, not its result: `Engine._verify_dev_artifacts` journals exactly the
    # attempts this is True for, so an accepted park's waived gate always leaves a
    # trace (#676).
    #
    # Scoped to ACCEPTED deliberately, and it is the whole guarantee: this rides
    # only the `passed()` return, so a leg that waived proof-of-work and then
    # failed a LATER gate — the sprint pair is the reachable one — records nothing.
    # That is the intended bound, not a gap: the record answers "which accepted
    # parks got in without proving work", and a park that was refused did not get
    # in. Anything wider would need the flag on the failing constructors too.
    park_proof_skipped: bool = False
    # An OBSERVATION, never a gate: on that same waived leg, whether the tree was
    # in fact free of code residue since the attempt's baseline. `True` = the
    # accepted park wrote nothing beyond what proof-of-work already excludes,
    # `False` = it carried a real diff, `None` = the probe could not answer. Two
    # things produce that `None`: a git fault (it degrades rather than escalating)
    # and an attempt with no `baseline_commit` to measure from. Nothing branches
    # on it. Note also that `False` is the weaker of the two definite answers:
    # the probe inherits `has_changes_since`'s fail-open, so a git REFUSAL (rc 128,
    # e.g. an unresolvable baseline) reads as "there are changes" rather than
    # raising, and is recorded as `False`.
    #
    # The two fields are deliberately separate, and collapsing them is the bug
    # this pair exists to prevent: one says a gate was waived, the other says what
    # that gate would have found. Keyed on the observation alone, an unanswerable
    # probe is indistinguishable from no waiver at all — so a park whose probe
    # faulted would go unrecorded, re-creating exactly the silence this pair ends.
    # A waived gate is recorded whatever the probe managed to say; `None` is a
    # truthful field value, not a reason to withhold the record.
    park_zero_diff: bool | None = None

    @classmethod
    def passed(
        cls,
        *,
        park_proof_skipped: bool = False,
        park_zero_diff: bool | None = None,
    ) -> "VerifyOutcome":
        return cls(
            ok=True,
            park_proof_skipped=park_proof_skipped,
            park_zero_diff=park_zero_diff,
        )

    @classmethod
    def retry(cls, reason: str, fixable: bool = False) -> "VerifyOutcome":
        return cls(ok=False, reason=reason, fixable=fixable)

    @classmethod
    def escalate(
        cls,
        reason: str,
        severity: str = "CRITICAL",
        env_fault: bool = False,
        contradiction: bool = False,
    ) -> "VerifyOutcome":
        return cls(
            ok=False,
            reason=reason,
            severity=severity,
            env_fault=env_fault,
            contradiction=contradiction,
        )

    @property
    def retryable(self) -> bool:
        return not self.ok and not self.severity
