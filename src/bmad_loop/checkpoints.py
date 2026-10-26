"""Shared plan / story checkpoint flow for the per-unit work-source engines.

:class:`CheckpointFlowMixin` holds the HITL checkpoint machinery that is the same
whatever source the units come from: the two-leg plan checkpoint, the
done-checkpoint pause after a committed unit, the pick-time wedge pause, and the
re-pause guard for an in-run escalation. ``StoriesEngine`` composes it as
``class StoriesEngine(CheckpointFlowMixin, Engine)``; the mixin goes first so its
overrides of the ``Engine`` hooks (``_resume_after_dev_verify``, ``_after_story``,
``_unresolved_escalation_key``) win, and its ``super()`` calls reach ``Engine``.

What differs per source is behind a small seam the engine provides:

* :meth:`CheckpointFlowMixin._entry_checkpoint_flags` — a unit's
  :class:`CheckpointFlags`, read fresh from the source (None: no such unit).
* :meth:`CheckpointFlowMixin._plan_produced` — whether the unit's plan is already
  on disk at ``ready-for-dev`` or later.
* :meth:`CheckpointFlowMixin._schedule_complete` — whether no further unit will
  dispatch (the done_checkpoint's skip-if-last check).

The two-leg plan checkpoint:

* Leg 1 sends ``Halt after planning.`` (:meth:`CheckpointFlowMixin._plan_halt_leg`);
  the skill HALTs at ``ready-for-dev``, the plan is verified, and the run pauses at
  :data:`~bmad_loop.model.PAUSE_PLAN_CHECKPOINT` for human plan review
  (:meth:`CheckpointFlowMixin._pause_plan_checkpoint`).
* Resume re-drives leg 2, a plain dispatch the skill routes straight to
  implementation, via :meth:`CheckpointFlowMixin._resume_after_dev_verify`.
* A plan review still owed when implementation is reached pauses before commit
  (:meth:`CheckpointFlowMixin._pause_plan_review_owed`).

The engine's ``_drive_story`` owns the dispatch side (the ``plan_review_owed``
latch and its journal) and calls the pauses above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from . import gates
from .engine import RunPaused
from .escalation import unresolved_escalation_keys
from .model import (
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_STORY_CHECKPOINT,
    Phase,
    StoryTask,
)
from .runs import graceful_stop_requested

if TYPE_CHECKING:
    from .engine import Engine

    _MixinBase = Engine
else:
    _MixinBase = object


class CheckpointFlags(NamedTuple):
    """One unit's checkpoint settings, as its source declares them.

    ``refined`` marks a unit whose plan was approved before dispatch, so it never
    takes the two-leg plan halt even when ``plan_checkpoint`` is set. Stories
    entries are never refined."""

    plan_checkpoint: bool
    done_checkpoint: bool
    refined: bool = False


class CheckpointFlowMixin(_MixinBase):
    # ------------------------------------------------------------------ seam

    def _entry_checkpoint_flags(self, task: StoryTask) -> CheckpointFlags | None:
        """The task's unit flags, read fresh from the source at call time; None when
        the source has no such unit.

        An unreadable source is the implementation's to journal; it then answers
        ``done_checkpoint=True``, because :meth:`_after_story` would rather pause
        for a review than drop one the source may well set."""
        raise NotImplementedError

    def _plan_produced(self, task: StoryTask) -> bool:
        """Whether the task's plan is on disk at ``ready-for-dev`` or later, so
        the next dispatch is the implement leg, not a plan-halt leg."""
        raise NotImplementedError

    def _schedule_complete(self) -> bool:
        """Whether no further unit will dispatch. An unreadable source must answer
        False, so the done_checkpoint still fires."""
        raise NotImplementedError

    # ------------------------------------------------------------ escalations

    def _repause_inrun_escalation(self) -> None:
        """Re-pause on a story that escalated *after a session ran* (``attempt > 0``)
        instead of re-deriving its fate from disk.

        A pick-time wedge / unknown-selector task (attempt == 0) is a pure disk
        projection, and re-classifying it every pick is its designed lifecycle:
        fix the blocked spec / duplicate file / manifest by hand and a bare
        resume proceeds. An in-run escalation is not disk-derivable — a CRITICAL
        verify outcome (e.g. a proof-of-work GitError) or a resolved-redrive
        exhaustion pause escalates with the spec at a *resumable or even done*
        status, so the disk scan would silently re-dispatch the story (a fresh
        StoryTask overwrites the escalated one, destroying its escalation record
        and ``resolved_redrive`` guard — a later exhaustion then DEFERs the
        human-resolved work) or, at ``done``, leapfrog the unresolved escalation
        entirely. Only ``runs.rearm_escalation`` (`bmad-loop resolve`) discharges
        it: the re-arm resets the task to PENDING, after which this guard no
        longer matches and the normal re-drive proceeds. Never mutates the task —
        the escalation context must survive for resolve to act on."""
        for key, task in self.state.tasks.items():
            if task.phase == Phase.ESCALATED and task.attempt > 0:
                reason = (
                    f"story {key!r} escalated during this run and was not resolved — "
                    f"run `bmad-loop resolve {self.state.run_id}`, then resume"
                )
                self.journal.append("stories-escalation-unresolved", story_key=key)
                gates.notify(self.policy, self.run_dir, f"unresolved escalation: {key}", reason)
                raise RunPaused(reason, PAUSE_ESCALATION, key)

    def _unresolved_escalation_key(self) -> str | None:
        """Only an in-run escalation (``attempt > 0``) blocks ``finished``. A
        pick-time wedge / unknown-selector task (``attempt == 0``) is re-classified
        from disk every pick (see ``_repause_inrun_escalation``), so reaching run end
        means the human fixed it by hand; its stale ESCALATED record must not
        re-pause the run, whose only way past would be a re-arm that re-drives the
        fixed story. ``escalation.unresolved_escalation_keys`` holds the narrowing
        for the stories and tickets sources, so resolve's admission shares it."""
        return next(iter(unresolved_escalation_keys(self.state)), None)

    def _pause_wedged(
        self,
        key: str,
        *,
        state_kind: str,
        detail: str,
        spec_file: str | None = None,
        sentinel_kind: str = "",
    ) -> None:
        """A blocked / sentinel / ambiguous unit stopped the scan before any
        session ran this pick — it was already in that state on disk (a prior
        run, or a resume of one that halted). A blocked unit cannot be
        leapfrogged, so pause for resolve.

        Records an ESCALATED task keyed by ``key`` — the same shape an in-run
        escalation leaves — so ``bmad-loop resolve`` (``runs.rearm_escalation`` +
        ``resolve.build_context``) and the sentinel auto-clear act on it uniformly,
        and the resolved re-drive flows through ``_finish_inflight`` exactly like a
        rearmed escalation. ``spec_file`` is the file resolve opens (None when there
        is no single file to pick — the human removes the duplicate);
        ``sentinel_kind`` is recorded on the task so ``rearm_escalation`` clears a
        sentinel by this recorded kind, not by re-deriving it from the spec_file
        basename. Always raises."""
        task = StoryTask(story_key=key, epic=0)
        task.phase = Phase.ESCALATED  # deliberate: a wedge has no legal advance from PENDING
        if spec_file is not None:
            task.spec_file = spec_file
        if sentinel_kind:
            task.sentinel_kind = sentinel_kind
        self.state.tasks[key] = task
        reason = f"story {key!r} needs resolution before the run can continue: {detail}"
        self.journal.append(
            "stories-wedged",
            story_key=key,
            state_kind=state_kind,
            sentinel_kind=sentinel_kind or None,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"story blocked: {key}",
            f"{detail} — resolve, then `bmad-loop resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, key)

    # -------------------------------------------------------- plan checkpoint

    def _plan_halt_leg(self, task: StoryTask, flags: CheckpointFlags | None) -> bool:
        """Whether this dispatch HALTs after planning for a human plan review.

        True only for an unrefined ``plan_checkpoint`` unit whose plan has not yet
        reached ``ready-for-dev`` on disk — i.e. leg 1. Once the plan exists (leg 2
        after the plan-checkpoint resume, or a mid-flight repair that reset the spec
        to in-progress) this is False and the unit dispatches straight to
        implementation. Reading on-disk state (not a task flag) is what keeps the
        prompt's ``Halt after planning.`` leg and the env's ``BMAD_LOOP_PLAN_HALT``
        in lock-step: both call this before the session writes the spec.

        ``flags`` None (no such unit) answers False without reading. Any present
        unit reads the disk whatever its flags, so a read fault surfaces the same
        way for every unit."""
        if flags is None:
            return False
        produced = self._plan_produced(task)
        return flags.plan_checkpoint and not flags.refined and not produced

    def _pause_plan_checkpoint(self, task: StoryTask) -> None:
        """Leg 1 of a spec_checkpoint story verified (plan at ready-for-dev): pause
        for human plan review. The task stays at DEV_VERIFY with
        ``plan_checkpoint_pending`` set, so on resume _finish_inflight routes it to
        :meth:`_resume_after_dev_verify` for the implement leg. Always raises."""
        task.plan_review_owed = False  # discharged: we are pausing for the review now
        self.journal.append(
            "checkpoint-pause",
            story_key=task.story_key,
            checkpoint="plan",
            spec=self._operator_spec_path(task),
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"plan ready for review: {task.story_key}",
            f"review the planned spec {self._operator_spec_path(task)}, then "
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
            spec=self._operator_spec_path(task),
            owed_after_implement=True,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"plan review owed (already implemented): {task.story_key}",
            f"the story was implemented before its plan checkpoint fired — review "
            f"{self._operator_spec_path(task)}, then `bmad-loop resume {self.state.run_id}`",
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
        # dispatches a plain implement leg and, once it commits, the done_checkpoint
        # fires from _after_story like any other story.
        if task.plan_checkpoint_pending:
            task.plan_checkpoint_pending = False
            task.phase = Phase.PENDING  # deliberate reset for the re-drive
            self.journal.append("checkpoint-resume", story_key=task.story_key, checkpoint="plan")
            self._save()
            self._drive_story(task)
            return
        super()._resume_after_dev_verify(task)

    # -------------------------------------------------------- done checkpoint

    def _after_story(self, task: StoryTask) -> None:
        # done_checkpoint: after a committed story, pause for review — but skip when
        # this was the last story to dispatch (the run ends here anyway). Fires from
        # both _loop and _finish_inflight, always after any worktree integration.
        if task.phase != Phase.DONE:
            return
        # An unreadable source answers done_checkpoint=True (see the seam): the run
        # cannot proceed past it anyway (the next pick would halt on it loud), so
        # the only thing a skip could save is the human review itself.
        flags = self._entry_checkpoint_flags(task)
        if flags is None or not flags.done_checkpoint:
            return
        # skip-if-last: no further story will dispatch. Either the schedule is
        # complete OR this committed story is the run's --max-stories-th (the bound
        # stops the loop next iteration). Honoring the cap here — durably, from
        # committed-story count, not the resume-reset _loop counter — is what keeps
        # a done_checkpoint pause from letting a resume leapfrog past the bound (the
        # pause+resume would otherwise reset the local counter and dispatch more).
        # A pending graceful stop also skips the done_checkpoint pause: the
        # loop-head check will end the run as `stopped` on the next iteration, so
        # pausing here would strand it `paused` instead — the stop must win (see
        # the plan's "done_checkpoint + graceful after same story" disposition).
        graceful = graceful_stop_requested(self.run_dir)
        if self._schedule_complete() or self._max_stories_reached() or graceful:
            fields: dict[str, Any] = {"story_key": task.story_key, "checkpoint": "story"}
            if graceful:
                # tag the cause so the journal distinguishes a graceful-stop skip
                # from a natural last-story / --max-stories skip.
                fields["reason"] = "graceful-stop"
            self.journal.append("checkpoint-skip-last", **fields)
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

    def _max_stories_reached(self) -> bool:
        """Whether the run has dispatched its ``--max-stories`` allotment. Consults the
        same durable :meth:`Engine._dispatched_count` the loop's dispatch gate uses, so
        the done_checkpoint's skip-if-last fires exactly when the loop will stop next
        iteration — no drift between the two guards across a pause/resume."""
        if self.max_stories is None:
            return False
        return self._dispatched_count() >= self.max_stories
