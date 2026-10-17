"""Engine variant for "stories mode" — folder+id dispatch (BMAD-METHOD #2549).

Where the default :class:`~bmad_loop.engine.Engine` walks ``sprint-status.yaml``,
``StoriesEngine`` drives a typed ``stories.yaml`` (the Story Breakdown output,
a fixed-name sibling of ``SPEC.md``). Each entry is dispatched by *spec folder +
story id* rather than a spec path: the inner ``bmad-build-auto`` skill reads its
own entry, creates-or-resumes the story spec at ``<spec-folder>/stories/<id>-<slug>.md``,
and the orchestrator reads that id-keyed path back deterministically — no
mtime-scan, no shared mutable board.

Like ``SweepEngine``, this is a thin override layer over the mature story
pipeline: only the story source (``_make_work_source``, a
:class:`~bmad_loop.stories_source.StoriesWorkSource`, and the pauses
``_pick_next`` turns its halts into), the dispatch prompt
(``_dev_prompt``), the (absent) bookkeeping sync (``_post_dev_state_sync``), the
deferral-harvest source (``_harvest_spec_path``), the board-ownership clause the
inherited review and plugin-workflow prompts inject (``_sprint_board_instruction``,
empty here — there is no board), artifact verification
(``_verify_dev_artifacts``), the session env
(``_extra_session_env``), and the HITL checkpoints differ. (That list is
illustrative and has run stale before — read the class, not it.) Everything else —
dev/verify/review/commit, crash resume, worktree isolation, gates — is inherited
unchanged.

HITL checkpoints (Phase 3) are per-story and independent (a story may set both
and pause twice). Their flow is shared with other per-unit sources through
:class:`~bmad_loop.checkpoints.CheckpointFlowMixin`; this class supplies the seam
(``_entry_checkpoint_flags``, ``_plan_produced``, ``_schedule_complete``):

* ``spec_checkpoint`` — a two-leg dispatch. Leg 1 sends ``Halt after planning.``
  (``_plan_halt_leg``); the skill HALTs at ``ready-for-dev``, the plan is
  verified, and the run pauses at ``PAUSE_PLAN_CHECKPOINT`` for human plan
  review. Resume re-drives leg 2 (a plain folder+id dispatch that the skill routes
  straight to implementation) via ``_resume_after_dev_verify``.
* ``done_checkpoint`` — after the story commits, the run pauses at
  ``PAUSE_STORY_CHECKPOINT`` (``_after_story``), skipped when the story was
  the last one to dispatch.

A blocked / sentinel / ambiguous story stops the scan and escalates for resolve
(:meth:`_pause_wedged_story` → ``_pause_wedged``); sentinel auto-clear on re-arm
lives in ``runs.rearm_escalation``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from . import gates, stories, verify
from .adapters.base import SessionResult
from .checkpoints import CheckpointFlags, CheckpointFlowMixin
from .engine import Engine, RunPaused
from .model import (
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    Phase,
    StoryTask,
    result_mapping,
)
from .stories_source import (
    HALT_UNKNOWN_SELECTOR,
    HALT_WEDGED,
    StoriesModeError,
    StoriesWorkSource,
)
from .worksource import Halt, WorkRef, WorkSource


def _checkpoint_flags(entry: stories.StoryEntry | None) -> CheckpointFlags | None:
    """A manifest entry's checkpoint flags; None for an absent entry. Stories
    entries are never refined."""
    if entry is None:
        return None
    return CheckpointFlags(
        plan_checkpoint=entry.spec_checkpoint, done_checkpoint=entry.done_checkpoint
    )


class StoriesEngine(CheckpointFlowMixin, Engine):
    def __init__(self, *args: Any, spec_folder: str, **kwargs: Any):
        # `--story` is a plain story id here, not a sprint E-S ref — keep it out of
        # the base's sprint-style parse_selector (which would reject a bare "2")
        # and out of the epic filter (a flat list has no epics). We scan by id in
        # the stories source instead. The CLI still persists story_filter on
        # RunState, so resume restores the id filter unchanged. Set before
        # super().__init__, which builds the source (_make_work_source).
        self._story_id_filter = kwargs.get("story_filter") or None
        kwargs["story_filter"] = None
        kwargs["epic_filter"] = None
        super().__init__(*args, **kwargs)
        # Project-relative posix path to the epic spec folder. Anchored on the
        # *current* workspace root at use time (project root in place, the unit
        # worktree under isolation), which keeps _pick_next, _dev_prompt, verify,
        # and the adapter read-back all resolving against the same live checkout.
        # The source built inside super().__init__ reads it through a getter.
        self._spec_folder_rel = self._relativize(spec_folder)
        # journal `stories-validated` once per process (re-validated every pick).
        self._validated = False
        # story keys already warned about an unreadable manifest — journal once
        # each (``_entry_for`` is called several times per dispatch).
        self._warned_unreadable: set[str] = set()
        # pin the resolved mode into run state so resume/resolve rebuild a
        # StoriesEngine (not the sprint Engine) without re-reading policy.
        self.state.source = "stories"
        self.state.spec_folder = self._spec_folder_rel

    # ------------------------------------------------------------ spec folder

    def _make_work_source(self) -> WorkSource:
        """The manifest source. Both getters are live: the folder anchors on the
        current ``self.workspace`` project, and ``_spec_folder_rel`` is set only
        after ``Engine.__init__`` has called this."""
        return StoriesWorkSource(
            lambda: self.workspace, lambda: self._spec_folder_rel, self._story_id_filter
        )

    @property
    def _stories_source(self) -> StoriesWorkSource:
        source = self.source
        assert isinstance(source, StoriesWorkSource)
        return source

    def _relativize(self, spec_folder: str) -> str:
        return StoriesWorkSource.relativize(self.paths.project, spec_folder)

    def _stories_folder(self) -> Path:
        return self._stories_source.folder()

    def _load_stories(self) -> stories.Stories:
        return self._stories_source.load()

    # --------------------------------------------------------------- picking

    def _compute_schedule(self) -> stories.Schedule:
        """The source's linear schedule against fresh on-disk state, journaling
        ``stories-validated`` once the manifest has loaded. Raises
        :class:`StoriesModeError` on an unusable manifest or an unknown ``--story``
        id. Used by the done_checkpoint skip-if-last check (:meth:`_schedule_complete`)."""
        story_set = self._stories_source.load_checked()
        self._journal_validated(len(story_set.entries))
        return self._stories_source.compute_schedule(story_set, self._phases())

    def _journal_validated(self, count: int | None) -> None:
        if self._validated:
            return
        self.journal.append(
            "stories-validated",
            count=count,
            spec_folder=self._spec_folder_rel,
        )
        self._validated = True

    def _pick_next(self) -> WorkRef | None:
        self._repause_inrun_escalation()  # raises when one is live
        pick = self.source.pick(self._phases(), self.state.current_epic)
        self._journal_validated(pick.loaded_count)
        if pick.halt is not None:
            self._halt_pick(pick.halt)
        return pick.ref  # None: SCHEDULE_COMPLETE — every story is done

    def _halt_pick(self, halt: Halt) -> NoReturn:
        if halt.cause == HALT_UNKNOWN_SELECTOR:
            # A bad/removed --story id: pause for resolve (fix the manifest, then
            # resume) rather than crash the run. Keyed on the selector id so the
            # escalation viewer + rearm act on the same key.
            self._pause_unknown_selector(halt.key)  # always raises RunPaused
        if halt.cause == HALT_WEDGED and isinstance(halt.payload, stories.Schedule):
            self._pause_wedged_story(halt.payload)  # always raises RunPaused
        super()._halt_pick(halt)

    def _pause_unknown_selector(self, selector: str) -> None:
        """The ``--story`` selector resolves to no manifest entry: pause for
        resolve keyed on the selector, the same ESCALATED shape a wedge leaves, so
        the CLI/TUI surface + rearm handle it uniformly. Always raises."""
        task = StoryTask(story_key=selector, epic=0)
        task.phase = Phase.ESCALATED
        self.state.tasks[selector] = task
        reason = (
            f"--story id {selector!r} is not in stories.yaml — fix the id or the "
            f"manifest, then `bmad-loop resume {self.state.run_id}`"
        )
        self.journal.append("stories-selector-unknown", story_key=selector)
        gates.notify(self.policy, self.run_dir, f"unknown --story id: {selector}", reason)
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, selector)

    def _pause_wedged_story(self, sched: stories.Schedule) -> None:
        """Pause for resolve on the story a wedged schedule stopped at, through the
        shared :meth:`_pause_wedged`. A PRESENT/SENTINEL wedge carries its spec path
        so resolve can open it; an AMBIGUOUS wedge leaves it unset (no single file
        to pick — the human removes the duplicate). A sentinel is journaled
        (``sentinel-detected``) before the wedge record. Always raises."""
        entry = sched.entry
        state = sched.state
        assert entry is not None and state is not None
        spec_file = None
        if state.kind in (stories.KIND_PRESENT, stories.KIND_SENTINEL) and state.path is not None:
            spec_file = str(state.path)
        sentinel_kind = ""
        if state.kind == stories.KIND_SENTINEL:
            sentinel_kind = state.sentinel_kind
            self._journal_sentinel_detected(entry.id, state)
        self._pause_wedged(
            entry.id,
            state_kind=state.kind,
            detail=self._wedged_detail(state),
            spec_file=spec_file,
            sentinel_kind=sentinel_kind,
        )

    @staticmethod
    def _wedged_detail(state: stories.StoryState) -> str:
        if state.kind == stories.KIND_SENTINEL:
            return f"pre-planning halt sentinel ({state.sentinel_kind}) at {state.path}"
        if state.kind == stories.KIND_AMBIGUOUS:
            names = ", ".join(p.name for p in state.paths)
            return f"ambiguous story file match: {names}"
        # KIND_PRESENT with a blocked / unrecognized status.
        return f"story spec status {state.status!r} at {state.path}"

    def _journal_sentinel_detected(self, story_key: str, state: stories.StoryState) -> None:
        """Record that a read-back resolved a story to a fixed-slug pre-planning-halt
        sentinel, carrying its recorded blocking condition (the reason planning
        halted, parsed from ``## Auto Run Result``). Fires at pick-time (a sentinel
        left by a prior run/resume) and post-dev (this run's session HALTed), so the
        journal always has a distinct trace before the escalation/wedge event."""
        condition = ""
        if state.path is not None:
            try:
                condition = stories.recorded_blocking_condition(
                    state.path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                condition = ""
        self.journal.append(
            "sentinel-detected",
            story_key=story_key,
            sentinel_kind=state.sentinel_kind or None,
            condition=condition,
        )

    # -------------------------------------------------------------- dispatch

    def _dispatched_spec_for_attempt(self, task: StoryTask) -> str | None:
        """Bind only the unique readable id-keyed spec present at dispatch."""
        try:
            state = stories.resolve_story_spec(self._stories_folder(), task.story_key)
            if state.kind != stories.KIND_PRESENT or state.path is None:
                return None
            # Resolution degrades a read fault to PRESENT with an unknown status;
            # ownership must be stricter because recovery may later repair this
            # exact file. Re-read now and refuse a vanished/non-regular/unreadable
            # candidate rather than persisting an ownership claim we did not see.
            if not state.path.is_file():
                return None
            state.path.read_text(encoding="utf-8")
            return str(state.path)
        except (OSError, RuntimeError, UnicodeDecodeError):
            return None

    def _requires_dispatched_spec_snapshot(self, task: StoryTask, prompt: str) -> bool:
        """Require authority when folder+id dispatch targets an existing spec.

        A pending, ambiguous, or sentinel story does not claim file authority
        through this seam; normal Stories scheduling handles those states. Once
        resolution identifies one PRESENT spec, however, a transient binding/read
        fault must abort rather than let the folder+id child mutate an input
        recovery cannot restore. An uncertain second observation fails closed for
        the same reason.
        """
        try:
            state = stories.resolve_story_spec(self._stories_folder(), task.story_key)
        except (OSError, RuntimeError):
            return True
        return state.kind == stories.KIND_PRESENT and state.path is not None

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
        # id (skip the mtime scan). Project-relative — the adapter anchors it on
        # spec.cwd, the mount project (DW-484; at the offset a nested `repo_root:`
        # adds, DW-379), matching `_stories_folder`, so it is correct in place and
        # under worktree isolation.
        env = {"BMAD_LOOP_SPEC_FOLDER": self._spec_folder_rel}
        # On a plan-halt leg tell the adapter to synthesize the ready-for-dev spec
        # as a *successful* terminal (plan done), not died-mid-flight. Keyed off the
        # same on-disk state as the prompt's `Halt after planning.` leg, so the two
        # never disagree (both read before the session writes the spec).
        if role == "dev" and self._plan_halt_leg(task, _checkpoint_flags(self._entry_for(task))):
            env["BMAD_LOOP_PLAN_HALT"] = "1"
        return env

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._stories_dev_prompt(task, feedback)

    def _stories_dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Folder+id dispatch for a fresh (or resumed-to-implement) story; the
        repair leg falls back to the inherited explicit-spec-file resume.

        Fresh dispatch:
            ``/<dev primitive> Spec folder: <rel>. Story id: <id>.``
              (the primitive is disk-resolved — see ``Engine._dev_skill``)
            + (plan-halt leg) `` Halt after planning.``
            + (when ``invoke_dev_with`` non-empty) a newline then its verbatim text.
            + (on a retry with verified parked work) a blank line then
              ``recovery_flow.retry_preserve_paragraph`` (#777).

        The folder is always project-relative (kills the absolute-path concern;
        the contract allows an absolute one but we never emit it). ``invoke_dev_with``
        is appended verbatim — the single planner→dev channel, never interpreted."""
        # The environment-claim clause (DW-523) ends the invocation line on both
        # legs — "" without probes, so the default prompt is unchanged. This
        # engine has no park clause to keep last.
        env_claim = self._environment_claim_instruction()
        env_sentence = f" {env_claim}" if env_claim else ""
        if feedback is not None:
            # Deterministic-verify repair: re-open the id-keyed story spec and
            # resume on it in place. Identical to the base generic repair leg (an
            # explicit-spec-file invocation, a conforming mode) — the story spec
            # is just a normal spec file.
            self._reset_spec_for_repair(task)
            spec_ref = task.spec_file or task.story_key
            return (
                f"/{self._dev_skill()} Resume the autonomous dev session on the in-progress "
                f"spec at `{spec_ref}`. The previous session's work failed deterministic "
                f"verification; repair the working tree so verification passes without "
                f"changing the spec's frozen intent contract. Verification evidence is "
                f"in `{feedback}`."
            ) + env_sentence
        entry = self._entry_for(task)
        prompt = (
            f"/{self._dev_skill()} Spec folder: {self._spec_folder_rel}. "
            f"Story id: {task.story_key}."
        )
        if self._plan_halt_leg(task, _checkpoint_flags(entry)):
            prompt += " Halt after planning."
        prompt += env_sentence
        if entry is not None and entry.invoke_dev_with:
            prompt += "\n" + entry.invoke_dev_with
        # A retry after a rolled-back attempt names its verified parked work (#777),
        # after the planner's verbatim text so that channel stays untouched.
        preserved = self._retry_preserve_notice(task)
        if preserved:
            prompt += "\n\n" + preserved
        return prompt

    def _entry_for(self, task: StoryTask) -> stories.StoryEntry | None:
        """The manifest entry for the task, re-read fresh so a resume (which does
        not go through ``_pick_next``) still resolves ``invoke_dev_with`` / the
        checkpoint flags. None when the id is absent — a pinned id never
        disappears, so this only happens on a hand-broken manifest, where the
        bare folder+id dispatch (title/description read by the skill) still runs.

        A parse failure journals a one-time ``stories-manifest-unreadable`` warning
        per story (this is called several times per dispatch) so the silent
        fallback leaves a trace, then returns None."""
        try:
            return self._load_stories().get(task.story_key)
        except stories.StoriesError as e:
            if task.story_key not in self._warned_unreadable:
                self._warned_unreadable.add(task.story_key)
                self.journal.append(
                    "stories-manifest-unreadable", story_key=task.story_key, error=str(e)
                )
            return None

    # ------------------------------------------------------ checkpoint seam

    def _plan_produced(self, task: StoryTask) -> bool:
        """Whether the id-keyed story spec is on disk at a plan-produced status —
        the predicate the CLI dry-run shares (``stories.is_plan_halt_leg``)."""
        state = stories.resolve_story_spec(self._stories_folder(), task.story_key)
        return not stories.is_plan_halt_leg(True, state)

    def _entry_checkpoint_flags(self, task: StoryTask) -> CheckpointFlags | None:
        """The manifest entry's flags, re-read fresh at the commit boundary for the
        done_checkpoint. Not ``_entry_for``: that dispatch helper warns once per
        story and falls back to "no checkpoints", while here an unreadable manifest
        must pause for review rather than silently drop a checkpoint it may well set
        (mirroring :meth:`_schedule_complete`'s conservative default), and says so
        every time."""
        try:
            entry = self._load_stories().get(task.story_key)
        except stories.StoriesError as e:
            self.journal.append(
                "stories-manifest-unreadable", story_key=task.story_key, error=str(e)
            )
            return CheckpointFlags(plan_checkpoint=False, done_checkpoint=True)
        return _checkpoint_flags(entry)

    # ---------------------------------------------------------- sync + verify

    def _harvest_spec_path(self, task: StoryTask, result_json: dict | None) -> Path | None:
        """Resolve the same id-keyed story spec that verification will accept."""
        if not result_mapping(result_json).get("spec_file"):
            return None
        state = stories.resolve_story_spec(self._stories_folder(), task.story_key)
        return state.path if state.kind == stories.KIND_PRESENT else None

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """No-op: stories mode has no sprint board. Honors the contract's "the
        orchestrator writes nothing" on the happy path — the dev skill is the sole
        writer of each story spec's status.

        Deferred-work closure is deliberately NOT here. It is project-wide state
        (a story's ``closes_deferred:`` declaration applies in this mode too), but
        it belongs at the commit boundary rather than at dev-sync time, so a story
        that later fails verification or review never leaves the ledger claiming
        its work resolved — see ``Engine._close_declared_deferred`` (#234)."""
        return

    def _manifest_closes_deferred(self, task: StoryTask) -> tuple[str, ...]:
        """The ``stories.yaml`` entry's ``closes_deferred`` ids.

        This is the channel that makes story-declared closure work unattended:
        ``bmad-build-auto`` writes the story spec and knows nothing of the ledger,
        so with the spec frontmatter alone a human would have to hand-edit every
        generated spec. The breakdown, by contrast, is authored while the ledger
        is in view. Both channels compose — the base hook unions them.

        Read here rather than through ``_entry_for`` because the two want opposite
        things from an unreadable manifest. ``_entry_for`` is a dispatch helper: it
        warns once per story and falls back to a bare folder+id dispatch that still
        works. Here the fallback is silence — the ids are not persisted anywhere,
        unlike the spec half's capture, so a parse failure at the commit boundary
        drops a declared closure with no trace beyond a generic one-time warning
        that may already have been spent earlier in the run. Say what was actually
        lost, at the site that lost it."""
        try:
            entry = self._load_stories().get(task.story_key)
        except stories.StoriesError as e:
            self.journal.append(
                "deferred-close-declaration-unreadable",
                story_key=task.story_key,
                source="stories.yaml",
                error=str(e),
                note="manifest-declared ids cannot be read; nothing is closed for them",
            )
            return ()
        return entry.closes_deferred if entry else ()

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        # The adapter marks a plan-halt leg's synthesized result `plan_halt`; latch
        # it onto the task so _drive_story pauses for plan review (and clears it on
        # the leg-2 re-drive), and switch verify to the ready-for-dev plan gate.
        plan_halt = bool(result_mapping(result_json).get("plan_halt"))
        task.plan_checkpoint_pending = plan_halt
        # Read-back detection: the just-run dev session HALTed pre-planning and left
        # a fixed-slug sentinel. Journal it (with its recorded blocking condition)
        # before verify turns it into a retryable failure → escalation, so the
        # sentinel has a distinct trace at read-back, not only the later escalation.
        folder = self._stories_folder()
        state = stories.resolve_story_spec(folder, task.story_key)
        if state.kind == stories.KIND_SENTINEL:
            # record the sentinel verdict on the task (mirrors _pause_wedged) so a later
            # rearm clears it by recorded kind, not by re-deriving from the basename.
            task.sentinel_kind = state.sentinel_kind
            self._journal_sentinel_detected(task.story_key, state)
        outcome = verify.verify_dev_stories(
            task,
            self.workspace.paths,
            result_json,
            spec_folder=folder,
            review_enabled=self._dev_review_enabled(),
            plan_halt=plan_halt,
            engine_written=self._harvest_gate_exclude(task),
        )
        # The marker remains the independent authority that this was a deliberate
        # plan halt. Journal the proof waiver only after every artifact gate passes;
        # `zero_diff` is an observation of the skipped gate, never an input to it.
        waiver_already_recorded = (
            plan_halt
            and outcome.ok
            and any(
                entry.get("kind") == "plan-halt-proof-of-work-skipped"
                and entry.get("story_key") == task.story_key
                and entry.get("attempt") == task.attempt
                and entry.get("generation", 0) == task.generation
                for entry in self.journal.entries()
            )
        )
        if plan_halt and outcome.ok and not waiver_already_recorded:
            self.journal.append(
                "plan-halt-proof-of-work-skipped",
                story_key=task.story_key,
                attempt=task.attempt,
                generation=task.generation,
                zero_diff=outcome.plan_halt_zero_diff,
            )
        return outcome

    def _run_verify_commands_after_dev(self, task: StoryTask, result_json: dict | None) -> bool:
        # A plan-halt leg produced only the plan (spec at ready-for-dev); there is
        # no implementation yet, so skip the project build/test gate — it would
        # fail on a half-built tree before the human ever sees the plan.
        return not bool(result_mapping(result_json).get("plan_halt"))

    def _verify_review(self, task: StoryTask):
        # Drop the sprint-status gate (stories mode has no board); the id-keyed
        # story spec's own `done` frontmatter is authoritative. The sink is the
        # base engine's: stories mode runs the same verifier commands and its
        # results belong in the same journal record kind (see
        # `Engine._review_command_sink`), so a mode-specific one would only be a
        # way for the three gates to drift apart on what they record.
        return verify.verify_review_stories(
            task,
            self.workspace.paths,
            self.policy,
            on_results=self._review_command_sink(task),
            on_probes=self._review_probe_sink(task),
        )

    def _sprint_board_instruction(self) -> str:
        # Stories mode has no sprint-status.yaml: `_post_dev_state_sync` is a no-op
        # here and `verify_review_stories` reads the id-keyed story spec alone. The
        # base clause would tell a session that a file it never sees is
        # orchestrator-owned — a false claim, not a harmless one. Dropped for the
        # same reason `_operator_park_enabled` is False below. This also drops the
        # inherited review prompt's `blocked` hand-back redirect, which gates itself
        # on this clause, and the `## Sprint board` section `_run_session` appends to
        # an injected plugin-workflow prompt: the board they speak about does not
        # exist here, so both stay byte-identical to their pre-#437 text in this mode.
        return ""

    def _operator_park_enabled(self) -> bool:
        # Stories mode has no sprint board, so the (awaiting-operator,
        # awaiting-operator) pair the park is verified against does not exist
        # here and `verify_review_stories` still demands `done`. Parking without
        # that gate would commit on the spec's word alone. Support is a follow-up
        # (spec-status + registry only); until then the token is simply not a
        # terminal this mode knows.
        return False

    # -------------------------------------------------------- HITL checkpoints

    def _drive_story(self, task: StoryTask, dev_resume: SessionResult | None = None) -> None:
        # Journal the plan-halt dispatch of a spec_checkpoint story's leg 1 (the
        # `Halt after planning.` prompt + BMAD_LOOP_PLAN_HALT env are emitted by
        # _dev_prompt / _extra_session_env, both keyed off the same on-disk state).
        # dev_resume None means a fresh drive, not a mid-session crash replay.
        if dev_resume is None:
            # DW-523: the dispatch gate `_dev_phase` runs, asked before the plan-halt
            # latch and journal below so an `environment` pause writes neither (a
            # passing probe is fresh, so `_dev_phase` does not probe twice).
            self._gate_dispatch(task, "dev")
        if dev_resume is None and self._plan_halt_leg(
            task, _checkpoint_flags(self._entry_for(task))
        ):
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
                f"review {self._operator_spec_path(task)}, then "
                f"`bmad-loop resume {self.state.run_id}`",
            )
            raise RunPaused(
                f"awaiting spec approval for {task.story_key}",
                PAUSE_SPEC_APPROVAL,
                task.story_key,
            )
        self._review_and_commit(task)

    def _schedule_complete(self) -> bool:
        """Whether the schedule has no more actionable stories. Guarded: a manifest
        that went unreadable (or a --story id that vanished) between the commit and
        this skip-if-last check must not crash the after-commit path — treat an
        error as "not complete" so the done_checkpoint still fires (the safe default
        is to pause for review, never to silently swallow it)."""
        try:
            return self._compute_schedule().is_complete
        except (StoriesModeError, stories.StoriesError):
            return False
