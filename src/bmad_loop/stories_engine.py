"""Engine variant for "stories mode" — folder+id dispatch (BMAD-METHOD #2549).

Where the default :class:`~bmad_loop.engine.Engine` walks ``sprint-status.yaml``,
``StoriesEngine`` drives a typed ``stories.yaml`` (the Story Breakdown output,
a fixed-name sibling of ``SPEC.md``). Each entry is dispatched by *spec folder +
story id* rather than a spec path: the inner ``bmad-dev-auto`` skill reads its
own entry, creates-or-resumes the story spec at ``<spec-folder>/stories/<id>-<slug>.md``,
and the orchestrator reads that id-keyed path back deterministically — no
mtime-scan, no shared mutable board.

Like ``SweepEngine``, this is a thin override layer over the mature story
pipeline: only the story source (``_pick_next``), the dispatch prompt
(``_dev_prompt``), the (absent) bookkeeping sync (``_post_dev_state_sync``), the
artifact verification (``_verify_dev_artifacts``), the session env
(``_extra_session_env``), and the HITL checkpoints differ. Everything else —
dev/verify/review/commit, crash resume, worktree isolation, gates — is inherited
unchanged.

HITL checkpoints (Phase 3) are per-story and independent (a story may set both
and pause twice):

* ``spec_checkpoint`` — a two-leg dispatch. Leg 1 sends ``Halt after planning.``
  (:meth:`_plan_halt_leg`); the skill HALTs at ``ready-for-dev``, the plan is
  verified, and the run pauses at :data:`PAUSE_PLAN_CHECKPOINT` for human plan
  review. Resume re-drives leg 2 (a plain folder+id dispatch that the skill routes
  straight to implementation) via :meth:`_resume_after_dev_verify`.
* ``done_checkpoint`` — after the story commits, the run pauses at
  :data:`PAUSE_STORY_CHECKPOINT` (:meth:`_after_story`), skipped when the story was
  the last one to dispatch.

A blocked / sentinel / ambiguous story stops the scan and escalates for resolve
(:meth:`_pause_wedged`); sentinel auto-clear on re-arm lives in
``runs.rearm_escalation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gates, stories, verify
from .adapters.base import SessionResult
from .engine import Engine, RunPaused
from .model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_CHECKPOINT,
    Phase,
    StoryTask,
)


@dataclass(frozen=True)
class _StoryRef:
    """The minimal shape :meth:`Engine._loop` reads from a picked story: a key
    and an epic. Stories mode has no epic numbering (a flat list), so ``epic`` is
    always 0 — inert for the epic-boundary logic, which only fires on a change."""

    key: str
    epic: int = 0


class StoriesError(Exception):
    """Raised when stories mode cannot proceed for a structural reason the run
    should surface loudly (unreadable/invalid ``stories.yaml`` mid-run)."""


class StoriesEngine(Engine):
    def __init__(self, *args: Any, spec_folder: str, **kwargs: Any):
        # `--story` is a plain story id here, not a sprint E-S ref — keep it out of
        # the base's sprint-style parse_selector (which would reject a bare "2")
        # and out of the epic filter (a flat list has no epics). We scan by id in
        # _pick_next instead. The CLI still persists story_filter on RunState, so
        # resume restores the id filter unchanged.
        self._story_id_filter = kwargs.get("story_filter") or None
        kwargs["story_filter"] = None
        kwargs["epic_filter"] = None
        super().__init__(*args, **kwargs)
        # Project-relative posix path to the epic spec folder. Anchored on the
        # *current* workspace root at use time (project root in place, the unit
        # worktree under isolation), which keeps _pick_next, _dev_prompt, verify,
        # and the adapter read-back all resolving against the same live checkout.
        self._spec_folder_rel = self._relativize(spec_folder)
        # journal `stories-validated` once per process (re-validated every pick).
        self._validated = False
        # pin the resolved mode into run state so resume/resolve rebuild a
        # StoriesEngine (not the sprint Engine) without re-reading policy.
        self.state.source = "stories"
        self.state.spec_folder = self._spec_folder_rel

    # ------------------------------------------------------------ spec folder

    def _relativize(self, spec_folder: str) -> str:
        """Store the spec folder project-relative when possible (we always
        dispatch project-relative). An absolute path inside the project tree is
        rebased to the project root; anything else is kept verbatim (the contract
        deliberately allows an absolute spec folder, though we never author one)."""
        raw = Path(spec_folder)
        if raw.is_absolute():
            try:
                return raw.resolve().relative_to(self.paths.project.resolve()).as_posix()
            except ValueError:
                return raw.as_posix()  # outside the project tree — leave absolute
        return raw.as_posix()

    def _stories_folder(self) -> Path:
        """The spec folder resolved against the current workspace root (project
        root at pick time, the unit worktree during a driven story)."""
        rel = Path(self._spec_folder_rel)
        return rel if rel.is_absolute() else self.workspace.root / rel

    def _load_stories(self) -> stories.Stories:
        return stories.load_stories(self._stories_folder())

    # --------------------------------------------------------------- picking

    def _compute_schedule(self) -> stories.Schedule:
        """Run the linear scheduler against fresh on-disk state, honoring the
        ``--story`` selector and the within-run skip set (stories already driven
        to a terminal *retirement* this run — mirrors the sprint engine's base_skip).
        Re-loads + re-validates stories.yaml every call (id-stability rule F: a
        between-runs re-derive is safe because pinned ids are stable and un-started
        ids reschedule by list order). Shared by :meth:`_pick_next` and the
        done_checkpoint skip-if-last check."""
        try:
            story_set = self._load_stories()
        except stories.StoriesError as e:
            raise StoriesError(f"stories.yaml no longer usable: {e}") from e
        self._journal_validated(story_set)
        folder = self._stories_folder()
        states = {e.id: stories.resolve_story_spec(folder, e.id) for e in story_set.entries}
        # Skip only stories retired this run — DONE (completed) or DEFERRED
        # (plateaued). Crucially NOT ESCALATED: schedule() consults the skip set
        # *before* classifying disk state, so skipping a wedge would let a bare
        # `bmad-loop resume` (no resolve) leapfrog it and dispatch a later story
        # onto a tree missing the blocked story's work — breaking the linear
        # "a blocked story cannot be leapfrogged" invariant. Left out of the skip
        # set, an unresolved wedge re-classifies from disk (blocked/sentinel) and
        # re-pauses on itself; once `resolve` re-arms it to PENDING it re-dispatches.
        skip = {
            key
            for key, task in self.state.tasks.items()
            if task.phase in (Phase.DONE, Phase.DEFERRED)
        }
        return stories.schedule(story_set, states, selector=self._story_id_filter, skip=skip)

    def _journal_validated(self, story_set: stories.Stories) -> None:
        if self._validated:
            return
        self.journal.append(
            "stories-validated",
            count=len(story_set.entries),
            spec_folder=self._spec_folder_rel,
        )
        self._validated = True

    def _pick_next(self) -> _StoryRef | None:
        sched = self._compute_schedule()
        if sched.outcome == stories.SCHEDULE_NEXT and sched.entry is not None:
            return _StoryRef(key=sched.entry.id)
        if sched.outcome == stories.SCHEDULE_WEDGED and sched.entry is not None:
            self._pause_wedged(sched)
        return None  # SCHEDULE_COMPLETE — every story is done

    def _pause_wedged(self, sched: stories.Schedule) -> None:
        """A blocked / sentinel / ambiguous story stopped the scan before any
        session ran this pick — it was already in that state on disk (a prior
        run, or a resume of one that halted). A blocked story cannot be
        leapfrogged, so pause for resolve.

        Records an ESCALATED task keyed by the story id — the same shape an in-run
        escalation leaves — so ``bmad-loop resolve`` (``runs.rearm_escalation`` +
        ``resolve.build_context``) and the sentinel auto-clear act on it uniformly,
        and the resolved re-drive flows through ``_finish_inflight`` exactly like a
        rearmed escalation. A PRESENT/SENTINEL wedge carries its spec path so
        resolve can open it; an AMBIGUOUS wedge leaves it unset (no single file to
        pick — the human removes the duplicate)."""
        entry = sched.entry
        state = sched.state
        assert entry is not None and state is not None
        task = StoryTask(story_key=entry.id, epic=0)
        task.phase = Phase.ESCALATED  # deliberate: a wedge has no legal advance from PENDING
        if state.kind in (stories.KIND_PRESENT, stories.KIND_SENTINEL) and state.path is not None:
            task.spec_file = str(state.path)
        self.state.tasks[entry.id] = task
        detail = self._wedged_detail(state)
        reason = f"story {entry.id!r} needs resolution before the run can continue: {detail}"
        self.journal.append(
            "stories-wedged",
            story_key=entry.id,
            state_kind=state.kind,
            sentinel_kind=state.sentinel_kind or None,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"story blocked: {entry.id}",
            f"{detail} — resolve, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, entry.id)

    @staticmethod
    def _wedged_detail(state: stories.StoryState) -> str:
        if state.kind == stories.KIND_SENTINEL:
            return f"pre-planning halt sentinel ({state.sentinel_kind}) at {state.path}"
        if state.kind == stories.KIND_AMBIGUOUS:
            names = ", ".join(p.name for p in state.paths)
            return f"ambiguous story file match: {names}"
        # KIND_PRESENT with a blocked / unrecognized status.
        return f"story spec status {state.status!r} at {state.path}"

    # -------------------------------------------------------------- dispatch

    def _extra_session_env(
        self, task: StoryTask, role: str, label: str | None = None
    ) -> dict[str, str]:
        # Only PRIMARY dev/review sessions get the story-spec env. An injected
        # plugin-workflow session (label set — e.g. a TEA pre_commit_gate) runs the
        # generic adapter too; exporting BMAD_LOOP_SPEC_FOLDER would make it
        # short-circuit to story-spec synthesis, so at pre_commit_gate (spec already
        # `done`) a gate session that did nothing would read `completed:done` and
        # bypass the completion-marker + monotonic stall-nudge contract (the
        # TEA-livelock fix). Withhold the env so workflow sessions keep the marker
        # contract; sprint-mode workflow sessions are already env-free here.
        if label is not None:
            return {}
        # Let the dev/review adapter resolve the story spec deterministically by
        # id (skip the mtime scan). Project-relative — the adapter rebases it
        # against spec.cwd, so it is correct in place and under worktree isolation.
        env = {"BMAD_LOOP_SPEC_FOLDER": self._spec_folder_rel}
        # On a plan-halt leg tell the adapter to synthesize the ready-for-dev spec
        # as a *successful* terminal (plan done), not died-mid-flight. Keyed off the
        # same on-disk state as the prompt's `Halt after planning.` leg, so the two
        # never disagree (both read before the session writes the spec).
        if role == "dev" and self._plan_halt_leg(task, self._entry_for(task)):
            env["BMAD_LOOP_PLAN_HALT"] = "1"
        return env

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._stories_dev_prompt(task, feedback)

    def _stories_dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Folder+id dispatch for a fresh (or resumed-to-implement) story; the
        repair leg falls back to the inherited explicit-spec-file resume.

        Fresh dispatch:
            ``/bmad-dev-auto Spec folder: <rel>. Story id: <id>.``
            + (plan-halt leg) `` Halt after planning.``
            + (when ``invoke_dev_with`` non-empty) a newline then its verbatim text.

        The folder is always project-relative (kills the absolute-path concern;
        the contract allows an absolute one but we never emit it). ``invoke_dev_with``
        is appended verbatim — the single planner→dev channel, never interpreted."""
        if feedback is not None:
            # Deterministic-verify repair: re-open the id-keyed story spec and
            # resume on it in place. Identical to the base generic repair leg (an
            # explicit-spec-file invocation, a conforming mode) — the story spec
            # is just a normal spec file.
            self._reset_spec_for_repair(task)
            spec_ref = task.spec_file or task.story_key
            return (
                f"/bmad-dev-auto Resume the autonomous dev session on the in-progress "
                f"spec at `{spec_ref}`. The previous session's work failed deterministic "
                f"verification; repair the working tree so verification passes without "
                f"changing the spec's frozen intent contract. Verification evidence is "
                f"in `{feedback}`."
            )
        entry = self._entry_for(task)
        prompt = f"/bmad-dev-auto Spec folder: {self._spec_folder_rel}. Story id: {task.story_key}."
        if self._plan_halt_leg(task, entry):
            prompt += " Halt after planning."
        if entry is not None and entry.invoke_dev_with:
            prompt += "\n" + entry.invoke_dev_with
        return prompt

    def _entry_for(self, task: StoryTask) -> stories.StoryEntry | None:
        """The manifest entry for the task, re-read fresh so a resume (which does
        not go through ``_pick_next``) still resolves ``invoke_dev_with`` / the
        checkpoint flags. None when the id is absent — a pinned id never
        disappears, so this only happens on a hand-broken manifest, where the
        bare folder+id dispatch (title/description read by the skill) still runs."""
        try:
            return self._load_stories().get(task.story_key)
        except stories.StoriesError:
            return None

    # Statuses that prove a spec_checkpoint story's plan already exists on disk:
    # once the plan reached (or passed) the Ready-for-Development gate the halt leg
    # is spent, so a re-dispatch is the plain implement leg. PENDING / draft (plan
    # not yet produced) and sentinel/ambiguous (a failed pre-planning halt) fall
    # through to a fresh halt leg.
    _PLAN_PRODUCED_STATUSES = frozenset(
        {"ready-for-dev", "in-progress", "in-review", "done", "blocked"}
    )

    def _plan_halt_leg(self, task: StoryTask, entry: stories.StoryEntry | None) -> bool:
        """Whether this dispatch HALTs after planning for a human plan review.

        True only for a ``spec_checkpoint`` story whose plan has not yet reached
        ``ready-for-dev`` on disk — i.e. leg 1. Once the plan exists (leg 2 after
        the plan-checkpoint resume, or a mid-flight repair that reset the spec to
        in-progress) this is False and the story dispatches straight to
        implementation. Reading on-disk state (not a task flag) is what keeps the
        prompt's ``Halt after planning.`` leg and the env's ``BMAD_LOOP_PLAN_HALT``
        in lock-step: both call this before the session writes the spec."""
        if entry is None or not entry.spec_checkpoint:
            return False
        state = stories.resolve_story_spec(self._stories_folder(), task.story_key)
        if state.kind == stories.KIND_PRESENT and state.status in self._PLAN_PRODUCED_STATUSES:
            return False
        return True

    # ---------------------------------------------------------- sync + verify

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """No-op: stories mode has no sprint board (and no deferred-work ledger to
        flip). Honors the contract's "the orchestrator writes nothing" on the
        happy path — the dev skill is the sole writer of each story spec's status."""
        return

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        # The adapter marks a plan-halt leg's synthesized result `plan_halt`; latch
        # it onto the task so _drive_story pauses for plan review (and clears it on
        # the leg-2 re-drive), and switch verify to the ready-for-dev plan gate.
        plan_halt = bool((result_json or {}).get("plan_halt"))
        task.plan_checkpoint_pending = plan_halt
        return verify.verify_dev_stories(
            task,
            self.workspace.paths,
            result_json,
            spec_folder=self._stories_folder(),
            review_enabled=self._dev_review_enabled(),
            plan_halt=plan_halt,
        )

    def _run_verify_commands_after_dev(self, task: StoryTask, result_json: dict | None) -> bool:
        # A plan-halt leg produced only the plan (spec at ready-for-dev); there is
        # no implementation yet, so skip the project build/test gate — it would
        # fail on a half-built tree before the human ever sees the plan.
        return not bool((result_json or {}).get("plan_halt"))

    def _verify_review(self, task: StoryTask):
        # Drop the sprint-status gate (stories mode has no board); the id-keyed
        # story spec's own `done` frontmatter is authoritative.
        return verify.verify_review_stories(task, self.workspace.paths, self.policy)

    # -------------------------------------------------------- HITL checkpoints

    def _drive_story(self, task: StoryTask, dev_resume: SessionResult | None = None) -> None:
        # Journal the plan-halt dispatch of a spec_checkpoint story's leg 1 (the
        # `Halt after planning.` prompt + BMAD_LOOP_PLAN_HALT env are emitted by
        # _dev_prompt / _extra_session_env, both keyed off the same on-disk state).
        # dev_resume None means a fresh drive, not a mid-session crash replay.
        if dev_resume is None and self._plan_halt_leg(task, self._entry_for(task)):
            # Latch the plan-review obligation BEFORE the session runs, so it
            # survives a crash in the post-session window, a non-fixable retry that
            # advanced the on-disk plan status, or a skill that overran the Halt
            # directive — cases where _plan_halt_leg (on-disk-status keyed) and
            # plan_checkpoint_pending (result keyed) both go stale. Saved eagerly so
            # a host death before the durable session record still finds it set.
            task.plan_review_owed = True
            self._save()
            self.journal.append(
                "plan-halt", story_key=task.story_key, spec_folder=self._spec_folder_rel
            )
        if not self._dev_phase(task, resume_result=dev_resume):
            return
        if task.plan_checkpoint_pending:
            self._pause_plan_checkpoint(task)  # always raises RunPaused
        if task.plan_review_owed:
            # A plan review is still owed but this dev leg did NOT itself pause for
            # it: the skill overran `Halt after planning.` and drove to done, or a
            # crash/non-fixable-retry re-drove straight to implementation on an
            # already-planned spec. Pause before commit so a spec_checkpoint story
            # can never commit un-reviewed (distinct "owed but implemented" message).
            self._pause_plan_review_owed(task)  # always raises RunPaused
        # preserve the global spec-approval gate (rarely configured in stories
        # mode, but the plan-checkpoint is not a substitute for it).
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

    def _pause_plan_checkpoint(self, task: StoryTask) -> None:
        """Leg 1 of a spec_checkpoint story verified (plan at ready-for-dev): pause
        for human plan review. The task stays at DEV_VERIFY with
        ``plan_checkpoint_pending`` set, so on resume _finish_inflight routes it to
        :meth:`_resume_after_dev_verify` for the implement leg. Always raises."""
        task.plan_review_owed = False  # discharged: we are pausing for the review now
        self.journal.append(
            "checkpoint-pause", story_key=task.story_key, checkpoint="plan", spec=task.spec_file
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"plan ready for review: {task.story_key}",
            f"review the planned spec {task.spec_file}, then "
            f"`bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(
            f"plan checkpoint for {task.story_key}: review the spec, then resume",
            PAUSE_PLAN_CHECKPOINT,
            task.story_key,
        )

    def _pause_plan_review_owed(self, task: StoryTask) -> None:
        """A spec_checkpoint story reached implementation without ever pausing for
        its plan review (the skill overran ``Halt after planning.``, or a crash /
        non-fixable retry re-drove straight to implementation on an already-planned
        spec). The plan review is still owed, so pause before commit — distinct from
        :meth:`_pause_plan_checkpoint` in that the code is already written.

        ``plan_checkpoint_pending`` is deliberately NOT set: on resume the story is
        already implemented, so :meth:`_resume_after_dev_verify` proceeds to
        review+commit (the human approved) rather than re-driving an implement leg.
        Reuses PAUSE_PLAN_CHECKPOINT so the CLI/TUI resume handling is unchanged.
        Always raises."""
        task.plan_review_owed = False  # discharged: we are pausing for the review now
        self.journal.append(
            "checkpoint-pause",
            story_key=task.story_key,
            checkpoint="plan",
            spec=task.spec_file,
            owed_after_implement=True,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"plan review owed (already implemented): {task.story_key}",
            f"the story was implemented before its plan checkpoint fired — review "
            f"{task.spec_file}, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(
            f"plan review owed for {task.story_key}: the skill implemented before the "
            f"plan checkpoint — review the spec, then resume",
            PAUSE_PLAN_CHECKPOINT,
            task.story_key,
        )

    def _resume_after_dev_verify(self, task: StoryTask) -> None:
        # A plan-checkpoint pause re-drives the implement leg here rather than the
        # base review+commit. The on-disk status is now ready-for-dev, so _drive_story
        # dispatches a plain folder+id leg (implement) and, once it commits, the
        # done_checkpoint fires from _after_story like any other story.
        if task.plan_checkpoint_pending:
            task.plan_checkpoint_pending = False
            task.phase = Phase.PENDING  # deliberate reset for the re-drive
            self.journal.append("checkpoint-resume", story_key=task.story_key, checkpoint="plan")
            self._save()
            self._drive_story(task)
            return
        super()._resume_after_dev_verify(task)

    def _after_story(self, task: StoryTask) -> None:
        # done_checkpoint: after a committed story, pause for review — but skip when
        # this was the last story to dispatch (the run ends here anyway). Fires from
        # both _loop and _finish_inflight, always after any worktree integration.
        if task.phase != Phase.DONE:
            return
        entry = self._entry_for(task)
        if entry is None or not entry.done_checkpoint:
            return
        if self._compute_schedule().is_complete:
            self.journal.append(
                "checkpoint-skip-last", story_key=task.story_key, checkpoint="story"
            )
            return
        self.journal.append(
            "checkpoint-pause", story_key=task.story_key, checkpoint="story", commit=task.commit_sha
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"story committed — review checkpoint: {task.story_key}",
            f"review {task.story_key}, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(
            f"story checkpoint for {task.story_key}: review, then resume",
            PAUSE_STORY_CHECKPOINT,
            task.story_key,
        )
