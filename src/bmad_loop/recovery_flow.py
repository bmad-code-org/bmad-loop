"""Attempt rollback + recovery-ref preservation flow.

Extracted from :class:`bmad_loop.engine.Engine` (issue #244, PR 2/2): the
rollback/preserve cluster is an independent recovery state machine. It lives
here as a collaborator built from narrow dependencies (repo paths, the policy,
run state, journal, run dir) plus a getter for the engine's swappable active
workspace and a handful of engine callbacks (emit a plugin hook, save state,
escalate a task, and escalation-pause). The collaborator never receives the
whole ``Engine`` — it cannot reach engine internals beyond those callables.

``Engine`` keeps same-name private methods that delegate here, so its tests and
the ``SweepEngine``/``StoriesEngine`` subclasses (which override nothing in this
cluster) see an unchanged surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, NoReturn

from . import gates, verify
from .model import Phase
from .platform_util import atomic_write_bytes, safe_ref_segment
from .statemachine import advance

if TYPE_CHECKING:
    from .bmadconfig import ProjectPaths
    from .journal import Journal
    from .model import RunState, StoryTask
    from .policy import Policy
    from .workspace import Workspace


# How many candidate `refs/attempt-preserve-dirty/*` names one rollback may probe
# before giving up (the base name plus -r2..-r100). Deliberately not a policy
# field: it is a runaway backstop, not a tuning knob — `scm.preserve_keep`
# (default 20) already prunes this namespace at every run start, so needing even
# a dozen candidates for ONE {slug}-{baseline}-{attempt} triple means something
# upstream is wrong. The cost of the bound is one git spawn per candidate on a
# path that only runs while a crashed attempt is being rolled back.
PRESERVE_REF_PROBE_LIMIT = 100


class _OwnedSpecAuthorityError(RuntimeError):
    """A previously canonical owned-spec name became unsafe to restore."""


class RecoveryFlow:
    """Roll back or pause a stopped/abandoned attempt, parking any work it did on
    named recovery refs before the reset.

    Built once per engine from narrow deps + engine callbacks (see module
    docstring). Behavior is identical to the cluster it was carved out of; the
    only structural changes are that engine-owned effects go through injected
    callables: ``emit`` fires a plugin hook (late-bound so a monkeypatched
    ``Engine._emit`` still wins), ``save`` persists run state, ``escalate``
    routes an intent-gap restore failure through the engine's escalation, and
    ``escalation_pause`` raises the engine's ``RunPaused`` (injected so this
    module need not import ``engine`` — that would reintroduce a runtime<->engine
    import cycle). ``workspace_get`` reads the engine's live (worktree-swappable)
    active workspace."""

    def __init__(
        self,
        *,
        paths: ProjectPaths,
        policy: Policy,
        state: RunState,
        journal: Journal,
        run_dir: Path,
        workspace_get: Callable[[], Workspace],
        emit: Callable[..., object],
        save: Callable[[], None],
        escalate: Callable[[StoryTask, str], None],
        escalation_pause: Callable[..., NoReturn],
    ) -> None:
        self.paths = paths
        self.policy = policy
        self.state = state
        self.journal = journal
        self.run_dir = run_dir
        # Read live (a getter, not a captured ref) so a run that swaps the
        # engine's `self.workspace` to a mounted unit worktree is seen here.
        self._workspace_get = workspace_get
        # Injected late-bound so a test patching `engine._emit` still wins
        # (recovery_flow's own binding wouldn't).
        self._emit = emit
        self._save = save
        self._escalate = escalate
        self._pause = escalation_pause

    def protected_relpaths(self) -> tuple[str, ...]:
        """Repo-relative posix paths of the BMAD artifact folders. These are
        preserved through a resolved re-drive's reset so a human correction is
        not reverted. They are deliberately not attempt-dirtiness exclusions;
        only the exact attempt-bound spec can serve that separate recognition
        job. Folders configured outside the repo are skipped — nothing to
        preserve through Git there."""
        workspace = self._workspace_get()
        out: list[str] = []
        for protected in (
            workspace.paths.output_folder,
            workspace.paths.implementation_artifacts,
            workspace.paths.planning_artifacts,
        ):
            try:
                rel = protected.relative_to(workspace.root).as_posix()
            except ValueError:
                continue  # configured outside the repo; nothing to protect here
            # "." (folder == repo root) as a keep/preserve prefix would cover the
            # whole tree — drop it so a misconfig can't disable the reset.
            if rel and rel != ".":
                out.append(rel)
        return tuple(out)

    def _attempt_owned_spec(self, task: StoryTask) -> tuple[Path, str | None] | None:
        """Resolve this attempt's bound spec and its exact Git exclusion.

        Relative persisted paths may name either a project-relative file or a
        basename under the configured implementation-artifacts directory. The
        binding is usable only when exactly one such regular file exists and its
        resolved target is inside a trusted project/artifact root.  An artifact
        root configured outside the Git workspace remains a trusted repair target,
        but cannot contribute a pathspec to a Git command running in the workspace.
        """
        if not task.dispatched_spec_file:
            return None

        workspace = self._workspace_get()
        raw = Path(task.dispatched_spec_file)
        candidates = (
            (raw,)
            if raw.is_absolute()
            else (
                workspace.paths.project / raw,
                workspace.paths.implementation_artifacts / raw,
            )
        )
        resolved_files: list[Path] = []
        for candidate in candidates:
            try:
                # New attempts persist a canonical regular-file path. Refuse a
                # post-launch symlink replacement before resolving it: following
                # the link here would let a failed child retarget snapshot restore
                # into an unrelated file that happens to share a trusted root.
                if candidate.is_symlink():
                    return None
                resolved = candidate.resolve()
                if raw.is_absolute() and resolved != candidate:
                    # Snapshot-bearing bindings are persisted canonically. A
                    # changed result here means a parent component was replaced
                    # by a symlink after launch, which is the same retargeting
                    # hazard as a link at the final component.
                    return None
                is_file = resolved.is_file()
            except (OSError, RuntimeError):
                return None
            if is_file and resolved not in resolved_files:
                resolved_files.append(resolved)
        if len(resolved_files) != 1:
            return None

        spec_path = resolved_files[0]
        if not verify.spec_within_roots(spec_path, workspace.paths):
            return None

        try:
            rel = spec_path.relative_to(workspace.root.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return spec_path, None
        return spec_path, rel if rel and rel != "." else None

    @staticmethod
    def _normalize_attempt_owned_spec(
        spec_path: Path, target_status: str, *, confine_root: Path
    ) -> None:
        """Write and verify the lifecycle route recovery promises to dispatch.

        ``confine_root`` is the project that owns the binding
        (``workspace.paths.project`` — the same root `_attempt_owned_spec`
        resolves candidates under), threaded down rather than re-derived here:
        this is a staticmethod on purpose, and `_workspace_get` is a live getter
        precisely because a unit worktree swaps the root mid-run (``rebased``
        makes ``paths.project`` the worktree root there). It must NOT be
        ``workspace.root``: under the `repo_root` override that is the separate
        code repo, an in-project spec fails its `is_relative_to` test, and the
        chokepoint silently takes the plain arm — dropping the parent walk the
        confinement exists for. It reaches the spec-writer chokepoint rule
        stated in `frontmatter.set_frontmatter_status` — an artifacts folder
        configured outside the project is a trusted repair target here
        (`_attempt_owned_spec`) and keeps the plain no-follow write."""
        verify.set_frontmatter_status(spec_path, target_status, confine_root=confine_root)
        if verify.status_of(verify.read_frontmatter(spec_path)) != target_status:
            raise verify.FrontmatterWriteError(
                f"could not normalize attempt-owned spec {spec_path} "
                f"to status {target_status!r}"
            )

    @staticmethod
    def _restore_attempt_owned_spec_bytes(spec_path: Path, snapshot: bytes) -> None:
        """Restore and verify the byte-exact pre-attempt input."""
        try:
            parent = spec_path.parent
            existing_parent = parent
            while not existing_parent.exists() and not existing_parent.is_symlink():
                if existing_parent == existing_parent.parent:
                    break
                existing_parent = existing_parent.parent
            # `Path.is_absolute()` on purpose, NOT `platform_util.is_absolute_path`
            # (#480 item 4). That family predicate is built for "must stay INSIDE
            # the project" config guards, where the answer must not vary by host.
            # This is the opposite question: a live path this process is about to
            # `resolve(strict=True)` and write through on the host it is running on,
            # so the platform's own notion of absolute is the operative one. They
            # diverge in the direction that matters -- on Windows a POSIX-absolute
            # `/spec.md` reads as NOT absolute, so this REFUSES it and fails CLOSED,
            # while `is_absolute_path` answers True and would let it through. The
            # swap #480 proposes would loosen the only genuine refusal guard it
            # named. Measured on POSIX: the `resolve(strict=True)` fixed-point term
            # below already refuses every relative spelling on its own (a relative
            # path never equals its own resolve), so on this platform the term
            # states the intent rather than carrying it alone -- which is exactly
            # why it needs saying here.
            if (
                not spec_path.is_absolute()
                or not existing_parent.is_dir()
                or existing_parent.is_symlink()
                or existing_parent.resolve(strict=True) != existing_parent
                or spec_path.is_symlink()
            ):
                raise _OwnedSpecAuthorityError(
                    f"attempt-owned spec target became unsafe: {spec_path}"
                )
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.is_dir() or parent.is_symlink() or parent.resolve(strict=True) != parent:
                raise _OwnedSpecAuthorityError(
                    f"attempt-owned spec target became unsafe: {spec_path}"
                )
            if spec_path.exists() and (
                not spec_path.is_file() or spec_path.resolve(strict=True) != spec_path
            ):
                raise _OwnedSpecAuthorityError(
                    f"attempt-owned spec target became unsafe: {spec_path}"
                )
        except _OwnedSpecAuthorityError:
            raise
        except (OSError, RuntimeError) as exc:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target could not be revalidated: {spec_path}"
            ) from exc
        # `require_writable_target=True` (#597): the spec this puts back is
        # operator-editable, and a temp-and-replace write needs write permission on
        # the PARENT DIRECTORY, never on the entry it replaces — so a spec marked
        # read-only was rewritten anyway. NOT the confined writer: the authority
        # walk above is stricter than the cohort walk (it demands
        # `resolve(strict=True)` fixed points for the parent and the target, which
        # refuses a link ANYWHERE above, inside the checkout as well as out), so a
        # confined write would relax this site rather than harden it.
        atomic_write_bytes(spec_path, snapshot, follow_symlinks=False, require_writable_target=True)
        if spec_path.read_bytes() != snapshot:
            raise verify.FrontmatterWriteError(
                f"could not restore pre-attempt contents of owned spec {spec_path}"
            )

    @classmethod
    def _restore_attempt_owned_spec(
        cls,
        spec_path: Path,
        snapshot: bytes,
        target_status: str,
        *,
        confine_root: Path,
    ) -> None:
        """Restore exact pre-attempt bytes, then verify the promised route."""
        cls._restore_attempt_owned_spec_bytes(spec_path, snapshot)
        # The durable snapshot should already carry this route. Keep the status
        # repair as a fail-safe for a legacy or externally edited state record;
        # it is the only permitted difference from the exact snapshot.
        cls._normalize_attempt_owned_spec(spec_path, target_status, confine_root=confine_root)

    def pause_for_owned_spec_recovery(
        self,
        task: StoryTask,
        spec: str,
        problem: str,
    ) -> NoReturn:
        """Pause once on unsafe snapshot authority, with a convergent remedy.

        The current checkout may contain either operator intent, failed-child
        output, or a partially completed reset, so recovery cannot infer which
        paths are safe to mutate next. Clear the unusable authority pair before
        saving: after the operator restores/verifies the intended spec, resume can
        recover the remaining tree as an unbound redrive instead of repeating the
        same impossible snapshot check forever.
        """
        task.dispatched_spec_file = None
        task.dispatched_spec_snapshot = None
        root = self._workspace_get().root
        notice = (
            "**ACTION REQUIRED — attempt-owned spec needs manual recovery**\n"
            f"Story **{task.story_key}** cannot safely restore its pre-attempt spec "
            f"at `{spec}`: {problem}. The working tree at `{root}` now requires "
            "inspection because bmad-loop cannot safely distinguish operator "
            "intent, failed-session output, and any rollback already completed.\n"
            "  1. Save any failed-session work you may want to inspect.\n"
            f"  2. Restore or verify the operator-approved contents of `{spec}` and "
            "remove/reset any other rejected attempt residue.\n"
            f"  3. Run `bmad-loop resume {self.state.run_id}`. The unusable binding "
            "was cleared, so the corrected spec will be observed afresh before the "
            "next child launches."
        )
        self.journal.append(
            "rollback-owned-spec-manual-required",
            story_key=task.story_key,
            spec=spec,
            problem=problem,
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"ACTION REQUIRED: recover attempt-owned spec for {task.story_key}",
            notice,
        )
        self._save()
        self._pause(notice, task.story_key)

    def rollback_or_pause(self, task: StoryTask, *, cause: str = "stopped") -> None:
        """Recover from an attempt that won't proceed.

        No-op when the real tree is proven to be at the attempt's baseline:
        neither a reset nor a pause is needed, and an unchanged bound spec is
        never rewritten. The one recognition exception is the exact regular file
        bound in ``task.dispatched_spec_file`` for this attempt. If the real tree
        is dirty but no debris remains after excluding that file, its lifecycle
        status is normalized and the real checkout is probed again without
        exclusions. A genuinely clean checkout with no commits above the attempt
        baseline emits ``rollback-skipped-clean``. If a lifecycle-only attempt
        committed its flip, recovery parks that commit and resets HEAD before
        retrying instead of mistaking the baseline-shaped worktree for a clean
        branch.
        A resolved re-drive whose authorized human-corrected spec remains dirty
        instead emits ``rollback-owned-spec-normalized`` and continues without
        pretending the checkout is clean. Snapshot-backed changes to a pre-existing
        untracked/ignored owned spec are parked explicitly and restored byte-exactly;
        other plain-attempt substantive residue follows ordinary reset/pause policy.

        The clean outcome also lets manual-recovery instructions terminate —
        after the operator resets and resumes, the now-clean tree skips straight
        through instead of re-pausing on the still-set ``baseline_commit``.

        A ``cause="resolved"`` re-drive is human-initiated (the operator ran the
        resolve workflow and re-armed the story), so it always auto-recovers and
        never pauses, regardless of ``scm.rollback_on_failure``. For the entire
        re-drive (``task.resolved_redrive``, latched at resume and cleared once the
        correction is committed) the BMAD artifact folders are preserved through
        every reset — so a later mid-re-drive retry/defer reset can't silently
        revert the correction. Whole folders never participate in the dirtiness
        decision; sibling artifact residue remains visible there.

        Otherwise (a stopped/abandoned attempt) recovery depends on where the
        attempt ran. Inside a mounted unit worktree it auto-recovers instead of
        pausing on policy: the worktree is disposable, the attempt's work is parked
        on preserve refs before the reset, and ``scm.rollback_on_failure`` gates
        *in-place* (isolation="none") recovery only (#161). In the main checkout the
        flag governs: OFF (default) leaves the working tree untouched and emits a
        bold manual-recovery notice that pauses the run (stop-and-wait); ON does a
        clean reset to baseline. Either way pre-existing untracked files are
        preserved; there is no blanket ``git clean``.

        The preserve steps and unsafe attempt-owned snapshot authority are the only
        things that can still pause a rollback the branching above chose to
        auto-recover, worktree included: when the attempt's
        committed or uncommitted work cannot be parked and the reset would destroy
        it, they refuse rather than reset (#340). That is a preservation failure
        rather than a policy decision, so it does not weaken #161 — the notice
        targets ``workspace.root``, which is the mounted worktree when there is
        one."""
        workspace = self._workspace_get()
        resolved = cause == "resolved"
        # preserve the corrected spec for the whole re-drive, not just the first
        # reset; the auto-recover (pause-vs-reset) decision below is unaffected.
        redrive = resolved or task.resolved_redrive
        # Whole-folder protection is reset-only. Attempt recognition below gets
        # at most one literal regular-file exclusion from the attempt binding.
        protected = self.protected_relpaths() if redrive else ()
        owned_spec = self._attempt_owned_spec(task)
        owned_exclude = (owned_spec[1],) if owned_spec and owned_spec[1] else ()
        # Un-determinable dirty check (git timeout/failure, #156) ⇒ assume dirty:
        # never skip recovery on an unproven "clean", never crash the run. The
        # normal branching below then decides — OFF pauses (worktree kept), ON /
        # resolved auto-recovers behind its preserve steps. A missing, unreadable,
        # or retargeted owned-spec snapshot is a separate fail-closed boundary.
        dirty = True
        dirty_probe_succeeded = False
        normalized_status: str | None = None
        normalized_commits_present = False
        owned_snapshot_changed = False
        owned_snapshot_restored = False
        owned_current_bytes: bytes | None = None
        owned_baseline_bytes: bytes | None = None
        owned_index_changed = False
        snapshot_restore_pending = False
        # ``cause=resolved`` is the initial unwind of the abandoned escalated
        # attempt. Its persisted snapshot predates the operator's correction and
        # must never overwrite that correction. Only a later failed attempt in
        # the latched re-drive owns a pre-launch snapshot that is safe to restore.
        restore_attempt_snapshot = not resolved and task.dispatched_spec_snapshot is not None
        restore_redrive_snapshot = task.resolved_redrive and restore_attempt_snapshot
        if not resolved and task.dispatched_spec_file and owned_spec is None:
            # The bound path was trusted and regular at launch. A child-side
            # deletion, directory/symlink replacement, or later resolution fault
            # must not turn that authority into an unowned generic reset: protected
            # artifact replay could otherwise preserve the replacement or lose the
            # operator's only corrected copy.
            self.journal.append(
                "rollback-owned-spec-unavailable",
                story_key=task.story_key,
            )
            self.pause_for_owned_spec_recovery(
                task,
                task.dispatched_spec_file,
                "the bound path is missing, unreadable, non-regular, or was retargeted",
            )
        if task.baseline_commit:
            try:
                dirty = verify.attempt_dirty(
                    workspace.root,
                    task.baseline_commit,
                    task.baseline_untracked,
                )
                dirty_probe_succeeded = True
            except (verify.GitError, OSError) as exc:
                self.journal.append(
                    "rollback-dirty-check-failed", story_key=task.story_key, error=str(exc)
                )
        if not resolved and owned_spec:
            if task.dispatched_spec_snapshot is None and task.resolved_redrive:
                # A pre-upgrade/incomplete state cannot distinguish the operator's
                # correction from child-authored body edits. Refuse every reset,
                # even when rollback_on_failure is enabled: either choice could
                # silently preserve bad bytes or discard the human correction.
                self.journal.append(
                    "rollback-owned-spec-snapshot-missing",
                    story_key=task.story_key,
                    spec=str(owned_spec[0]),
                )
                self.pause_for_owned_spec_recovery(
                    task,
                    str(owned_spec[0]),
                    "the persisted retry chain predates its required byte snapshot",
                )
            if task.dispatched_spec_snapshot is not None:
                try:
                    owned_current_bytes = owned_spec[0].read_bytes()
                    owned_snapshot_changed = owned_current_bytes != task.dispatched_spec_snapshot
                except OSError as exc:
                    self.journal.append(
                        "rollback-owned-spec-unreadable",
                        story_key=task.story_key,
                        spec=str(owned_spec[0]),
                        error=str(exc),
                    )
                    self.pause_for_owned_spec_recovery(
                        task,
                        str(owned_spec[0]),
                        "its current bytes could not be read for comparison",
                    )
                if task.baseline_commit and owned_exclude:
                    try:
                        owned_baseline_bytes = verify.worktree_file_bytes_at_revision(
                            workspace.root,
                            task.baseline_commit,
                            owned_exclude[0],
                        )
                        owned_index_changed = verify.index_path_changed_since(
                            workspace.root,
                            task.baseline_commit,
                            owned_exclude[0],
                        )
                        if verify.path_has_non_tree_ancestor_at_revision(
                            workspace.root,
                            task.baseline_commit,
                            owned_exclude[0],
                        ) or verify.path_is_non_regular_at_revision(
                            workspace.root,
                            task.baseline_commit,
                            owned_exclude[0],
                        ):
                            self.pause_for_owned_spec_recovery(
                                task,
                                str(owned_spec[0]),
                                "the attempt baseline would replace its canonical path "
                                "or one of its parent directories with an unsafe shape",
                            )
                    except (verify.GitError, OSError) as exc:
                        self.journal.append(
                            "rollback-owned-spec-baseline-read-failed",
                            story_key=task.story_key,
                            spec=str(owned_spec[0]),
                            error=str(exc),
                        )
                        self.pause_for_owned_spec_recovery(
                            task,
                            str(owned_spec[0]),
                            "its baseline tracking state could not be verified",
                        )
                # Git intentionally ignores the contents of names present in
                # baseline_untracked. The byte snapshot is the missing oracle for
                # a child edit to a pre-existing untracked/ignored spec.
                dirty = dirty or owned_snapshot_changed
                if owned_snapshot_changed and not owned_exclude:
                    # An attempt-bound spec under a configured external artifact
                    # root cannot be captured by a repository recovery ref. A
                    # reset or direct redrive restoration would otherwise overwrite
                    # the only failed-child copy with the pre-launch snapshot. Leave
                    # it for explicit operator adoption instead.
                    self.journal.append(
                        "rollback-owned-spec-unpreservable",
                        story_key=task.story_key,
                        spec=str(owned_spec[0]),
                    )
                    self.pause_for_owned_spec_recovery(
                        task,
                        str(owned_spec[0]),
                        "its failed-session bytes are outside Git and cannot be parked",
                    )
        # Establish that this attempt actually changed the checkout before the
        # bound spec can confer any normalization authority. Stories may dispatch
        # an already-resumable draft/in-progress/in-review spec; a session that
        # dies without touching it must not rewrite that clean baseline.
        if task.baseline_commit and dirty_probe_succeeded and not dirty:
            if (
                not resolved
                and owned_spec
                and task.dispatched_spec_file
                and task.dispatched_spec_snapshot is None
            ):
                spec_rel = owned_spec[1]
                try:
                    baseline_status = (
                        verify.frontmatter_status_at_revision(
                            workspace.root,
                            task.baseline_commit,
                            spec_rel,
                        )
                        if spec_rel
                        else None
                    )
                except verify.GitError:
                    baseline_status = None
                if baseline_status is None:
                    self.journal.append(
                        "rollback-owned-spec-snapshot-missing",
                        story_key=task.story_key,
                        spec=str(owned_spec[0]),
                    )
                    self.pause_for_owned_spec_recovery(
                        task,
                        str(owned_spec[0]),
                        "no byte snapshot or tracked baseline can prove it unchanged",
                    )
            self.journal.append("rollback-skipped-clean", story_key=task.story_key)
            return

        # The real tree is dirty. Only now ask whether the exact in-workspace
        # binding accounts for all of it. An out-of-workspace trusted spec has no
        # Git pathspec and therefore cannot manufacture evidence of a lifecycle
        # delta that Git cannot observe.
        if task.baseline_commit and dirty_probe_succeeded and owned_spec and owned_exclude:
            dirty = True
            dirty_probe_succeeded = False
            try:
                dirty = verify.attempt_dirty(
                    workspace.root,
                    task.baseline_commit,
                    task.baseline_untracked,
                    exclude=owned_exclude,
                )
                dirty_probe_succeeded = True
            except (verify.GitError, OSError) as exc:
                self.journal.append(
                    "rollback-dirty-check-failed", story_key=task.story_key, error=str(exc)
                )
        if task.baseline_commit and dirty_probe_succeeded and not dirty and owned_spec:
            spec_path, spec_rel = owned_spec
            try:
                original_spec = spec_path.read_bytes()
            except OSError as exc:
                self.journal.append(
                    "rollback-owned-spec-unreadable",
                    story_key=task.story_key,
                    spec=str(spec_path),
                    error=str(exc),
                )
                self.pause_for_owned_spec_recovery(
                    task,
                    str(spec_path),
                    "its current bytes could not be read before lifecycle repair",
                )
            if redrive:
                target_status = "in-review" if task.restore_patch else "ready-for-dev"
            else:
                try:
                    target_status = (
                        verify.frontmatter_status_at_revision(
                            workspace.root, task.baseline_commit, spec_rel
                        )
                        if spec_rel
                        else None
                    )
                except verify.GitError as exc:
                    self.journal.append(
                        "rollback-owned-spec-baseline-status-failed",
                        story_key=task.story_key,
                        error=str(exc),
                    )
                    target_status = None
            if (
                target_status is None
                and restore_attempt_snapshot
                and owned_snapshot_changed
                and not redrive
            ):
                # A baseline-untracked or ignored spec has no Git blob whose
                # lifecycle can be normalized. Its durable pre-launch bytes are
                # authoritative, but preserve the failed child's current bytes on
                # a recovery ref before restoring them in the auto-recovery arm.
                snapshot_restore_pending = True
                dirty = True
                normalized_status = None
            elif target_status is None:
                # The exclusion proved only that the bound spec accounts for the
                # checkout diff. Without a readable baseline status, that is not
                # authority to mutate it or to call the real checkout clean.
                dirty = True
                normalized_status = None
            else:
                repair_probe_succeeded = True
                if restore_redrive_snapshot:
                    # Preserve refs must capture the failed child's bytes, not the
                    # restored operator snapshot. Detect committed child residue
                    # before mutation and defer restoration until after both
                    # preserve steps when a reset is required.
                    try:
                        normalized_commits_present = bool(
                            verify.commits_above(workspace.root, task.baseline_commit)
                        )
                    except (verify.GitError, OSError) as exc:
                        dirty = True
                        dirty_probe_succeeded = False
                        repair_probe_succeeded = False
                        self.journal.append(
                            "rollback-dirty-check-failed",
                            story_key=task.story_key,
                            error=str(exc),
                        )
                if repair_probe_succeeded:
                    if restore_redrive_snapshot and (owned_snapshot_changed or owned_index_changed):
                        # Even a spec-only failed child must be recoverable before
                        # its body is replaced. Route through the auto arm so both
                        # committed and uncommitted bytes are parked first.
                        snapshot_restore_pending = True
                        dirty = True
                        normalized_status = target_status
                    else:
                        if restore_redrive_snapshot:
                            assert task.dispatched_spec_snapshot is not None
                            self._restore_attempt_owned_spec(
                                spec_path,
                                task.dispatched_spec_snapshot,
                                target_status,
                                confine_root=workspace.paths.project,
                            )
                            owned_snapshot_restored = True
                        else:
                            self._normalize_attempt_owned_spec(
                                spec_path,
                                target_status,
                                confine_root=workspace.paths.project,
                            )
                        normalized_status = target_status

                        # The exclusion answered only whether anything besides the
                        # owned spec changed. Re-probe the real checkout after repair
                        # before calling it clean or choosing a recovery policy.
                        dirty = True
                        dirty_probe_succeeded = False
                        try:
                            dirty = verify.attempt_dirty(
                                workspace.root,
                                task.baseline_commit,
                                task.baseline_untracked,
                            )
                            dirty_probe_succeeded = True
                        except (verify.GitError, OSError) as exc:
                            self.journal.append(
                                "rollback-dirty-check-failed",
                                story_key=task.story_key,
                                error=str(exc),
                            )
                        if dirty_probe_succeeded and not restore_redrive_snapshot:
                            # A plain lifecycle-only attempt can have committed its
                            # flip. The normalized tree then matches baseline even
                            # though HEAD still carries abandoned history.
                            try:
                                normalized_commits_present = bool(
                                    verify.commits_above(workspace.root, task.baseline_commit)
                                )
                            except (verify.GitError, OSError) as exc:
                                dirty = True
                                dirty_probe_succeeded = False
                                self.journal.append(
                                    "rollback-dirty-check-failed",
                                    story_key=task.story_key,
                                    error=str(exc),
                                )
                        if (
                            dirty_probe_succeeded
                            and not dirty
                            and restore_attempt_snapshot
                            and owned_snapshot_changed
                            and not redrive
                        ):
                            # Git-clean after lifecycle normalization means the
                            # failed child left only baseline bytes. If this attempt
                            # began with pre-existing operator dirt, restore that
                            # exact input instead of accepting the child's deletion
                            # as a clean rollback. A commit above baseline must be
                            # parked first; an uncommitted baseline copy is already
                            # durable in HEAD and needs no redundant recovery ref.
                            assert task.dispatched_spec_snapshot is not None
                            if normalized_commits_present:
                                snapshot_restore_pending = True
                                dirty = True
                                normalized_status = None
                            elif spec_path.read_bytes() != task.dispatched_spec_snapshot:
                                self._restore_attempt_owned_spec_bytes(
                                    spec_path, task.dispatched_spec_snapshot
                                )
                                owned_snapshot_restored = True
                                normalized_status = None
                                dirty = True
                                dirty_probe_succeeded = False
                                try:
                                    dirty = verify.attempt_dirty(
                                        workspace.root,
                                        task.baseline_commit,
                                        task.baseline_untracked,
                                    )
                                    dirty_probe_succeeded = True
                                except (verify.GitError, OSError) as exc:
                                    self.journal.append(
                                        "rollback-dirty-check-failed",
                                        story_key=task.story_key,
                                        error=str(exc),
                                    )
                if (
                    dirty
                    and not redrive
                    and not snapshot_restore_pending
                    and not owned_snapshot_restored
                ):
                    # A plain attempt may be inspected or parked by the ordinary
                    # recovery policy below. If baseline-status normalization did
                    # not prove the checkout clean, put its spec back byte-for-byte
                    # before that policy claims the tree was left untouched.
                    try:
                        self._restore_attempt_owned_spec_bytes(spec_path, original_spec)
                    except _OwnedSpecAuthorityError as exc:
                        self.pause_for_owned_spec_recovery(
                            task,
                            str(spec_path),
                            "its path became unsafe while undoing a tentative "
                            f"lifecycle repair ({exc})",
                        )
                    normalized_status = None
        if (
            owned_snapshot_restored
            and not redrive
            and owned_spec
            and task.baseline_commit
            and dirty_probe_succeeded
        ):
            self.journal.append(
                "rollback-owned-spec-restored",
                story_key=task.story_key,
                spec=str(owned_spec[0]),
                checkout_dirty=dirty,
            )
            return
        if task.baseline_commit and not dirty and not normalized_commits_present:
            if owned_snapshot_restored and owned_spec:
                self.journal.append(
                    "rollback-owned-spec-restored",
                    story_key=task.story_key,
                    spec=str(owned_spec[0]),
                    checkout_dirty=False,
                )
            else:
                self.journal.append("rollback-skipped-clean", story_key=task.story_key)
            return
        if (
            task.baseline_commit
            and dirty_probe_succeeded
            and dirty
            and redrive
            and owned_spec
            and normalized_status is not None
            and not normalized_commits_present
            and not snapshot_restore_pending
        ):
            self.journal.append(
                "rollback-owned-spec-normalized",
                story_key=task.story_key,
                spec=str(owned_spec[0]),
                status=normalized_status,
                checkout_dirty=True,
            )
            return
        # A mounted unit worktree is disposable by design: its branch never
        # touches the operator's checkout and the attempt's work is parked on
        # recovery refs before any reset. `scm.rollback_on_failure` gates
        # *in-place* (isolation="none") recovery only — pausing here would emit
        # main-checkout reset instructions for a tree the operator never works
        # in (#161). Compared by path, not by policy: a worktree-mode call that
        # reaches this method *outside* a mounted unit (e.g. resume with no
        # worktree recorded) still targets the main checkout and must pause.
        in_unit_worktree = workspace.root != self.paths.repo_root
        normalized_attempt_commits = (
            task.baseline_commit is not None
            and normalized_status is not None
            and normalized_commits_present
        )
        if (
            normalized_attempt_commits
            or snapshot_restore_pending
            or resolved
            or in_unit_worktree
            or self.policy.scm.rollback_on_failure
        ):
            # `preserve_ref` names where *this* rollback parked the attempt. Clear
            # it first: a later attempt that parks nothing (no commits above
            # baseline, or a preserve failure) must not inherit the previous
            # attempt's ref — the defer notice would then send the operator to work
            # that is not the deferred attempt's. A *clean*-tree rollback never gets
            # here (the `rollback-skipped-clean` return above fires first), so it
            # deliberately keeps the earlier ref: nothing new was parked, and that
            # ref is then the only place the story's work survives.
            task.preserve_ref = None
            task.preserve_partial = False
            self.journal.append(
                "rollback-auto",
                story_key=task.story_key,
                baseline=task.baseline_commit or "",
                note="reverting tracked changes + run-created untracked files",
            )
            # Give a plugin (the Unity engine) a chance to quiesce before the reset
            # rewrites tracked files under it — e.g. save + close open scenes so a
            # git reset --hard can't leave a shared Editor showing a run-freezing
            # "scene changed on disk" modal. Observe-only, like pre_worktree_teardown:
            # the returned ctx is ignored and never routed through _vetoed — a failed
            # quiesce must never block a rollback.
            self._emit("pre_rollback", task)
            force_owned_snapshot = (
                owned_exclude
                if restore_attempt_snapshot and (owned_snapshot_changed or owned_index_changed)
                else ()
            )
            # A re-drive ordinarily preserves best-effort, but restoration of a
            # changed bound spec is destructive unless both its committed and
            # uncommitted child state can be parked first.
            self.preserve_attempt_commits(
                task,
                allow_pause=not redrive or bool(force_owned_snapshot),
            )
            # Park the attempt's uncommitted diff too, so the reset below (and its
            # untracked cleanup) can't silently destroy in-progress work. Runs only
            # if preserve_attempt_commits did not pause (plain-rollback preserve
            # failure), and refuses the reset on the same terms when a failed
            # capture would cost unparked work (#340).
            # After owned-spec normalization proved the checkout byte-equivalent
            # to the baseline, only branch ancestry remains. Capturing the
            # worktree here would park the normalization's inverse diff against
            # the failed HEAD even though the target reset cannot discard any
            # checkout content. The commits were parked above; reset them directly.
            if restore_redrive_snapshot or not (normalized_attempt_commits and not dirty):
                self.preserve_attempt_worktree(
                    task,
                    allow_pause=not redrive or bool(force_owned_snapshot),
                    force_include=force_owned_snapshot,
                )
            if (
                restore_attempt_snapshot
                and (owned_snapshot_changed or owned_index_changed)
                and owned_spec
            ):
                # Both refs now contain the untouched failed attempt. Restore the
                # pre-launch input before reset. Redrives also re-establish the
                # route promised by the next prompt; plain attempts restore exact
                # bytes and repeat that restoration after reset so pre-existing
                # tracked operator dirt is not erased with the child.
                assert task.dispatched_spec_snapshot is not None
                if redrive:
                    target_status = "in-review" if task.restore_patch else "ready-for-dev"
                    self._restore_attempt_owned_spec(
                        owned_spec[0],
                        task.dispatched_spec_snapshot,
                        target_status,
                        confine_root=workspace.paths.project,
                    )
                else:
                    self._restore_attempt_owned_spec_bytes(
                        owned_spec[0], task.dispatched_spec_snapshot
                    )
                owned_snapshot_restored = True
            self.safe_reset(task, preserve=protected)
            if restore_attempt_snapshot and owned_spec and owned_exclude and not redrive:
                assert task.dispatched_spec_snapshot is not None
                try:
                    self._restore_attempt_owned_spec_bytes(
                        owned_spec[0], task.dispatched_spec_snapshot
                    )
                except _OwnedSpecAuthorityError as exc:
                    self.pause_for_owned_spec_recovery(
                        task,
                        str(owned_spec[0]),
                        f"its path became unsafe after the baseline reset ({exc})",
                    )
                owned_snapshot_restored = True
            if redrive and task.baseline_commit and owned_spec:
                # A sibling source/artifact change bypasses the earlier spec-only
                # normalization and reaches this reset. The protected artifact
                # folders deliberately retain the corrected spec, so re-establish
                # the route promised by the next prompt after resetting the
                # sibling residue. Patch restores keep their review route.
                target_status = "in-review" if task.restore_patch else "ready-for-dev"
                if restore_redrive_snapshot and owned_index_changed and owned_spec[1]:
                    # A whole-folder preserve checkout can stage a spec that the
                    # failed child force-added or committed even though the
                    # pre-launch binding was ignored/untracked. Restore baseline
                    # index ownership before writing the operator snapshot back.
                    verify.reset_index_path(
                        workspace.root,
                        task.baseline_commit,
                        owned_spec[1],
                    )
                if restore_redrive_snapshot and task.dispatched_spec_snapshot is not None:
                    try:
                        self._restore_attempt_owned_spec(
                            owned_spec[0],
                            task.dispatched_spec_snapshot,
                            target_status,
                            confine_root=workspace.paths.project,
                        )
                    except _OwnedSpecAuthorityError as exc:
                        self.pause_for_owned_spec_recovery(
                            task,
                            str(owned_spec[0]),
                            f"its path became unsafe after the baseline reset ({exc})",
                        )
                else:
                    self._normalize_attempt_owned_spec(
                        owned_spec[0],
                        target_status,
                        confine_root=workspace.paths.project,
                    )
                try:
                    checkout_dirty = verify.attempt_dirty(
                        workspace.root,
                        task.baseline_commit,
                        task.baseline_untracked,
                    )
                except (verify.GitError, OSError) as exc:
                    self.journal.append(
                        "rollback-dirty-check-failed",
                        story_key=task.story_key,
                        error=str(exc),
                    )
                else:
                    if checkout_dirty:
                        self.journal.append(
                            "rollback-owned-spec-normalized",
                            story_key=task.story_key,
                            spec=str(owned_spec[0]),
                            status=target_status,
                            checkout_dirty=True,
                        )
                    elif owned_snapshot_restored:
                        self.journal.append(
                            "rollback-owned-spec-restored",
                            story_key=task.story_key,
                            spec=str(owned_spec[0]),
                            checkout_dirty=False,
                        )
            elif owned_snapshot_restored and owned_spec and task.baseline_commit:
                try:
                    checkout_dirty = verify.attempt_dirty(
                        workspace.root,
                        task.baseline_commit,
                        task.baseline_untracked,
                    )
                except (verify.GitError, OSError) as exc:
                    self.journal.append(
                        "rollback-dirty-check-failed",
                        story_key=task.story_key,
                        error=str(exc),
                    )
                else:
                    self.journal.append(
                        "rollback-owned-spec-restored",
                        story_key=task.story_key,
                        spec=str(owned_spec[0]),
                        checkout_dirty=checkout_dirty,
                    )
            # Refresh the plugin's view of the now-reset tree (the Unity engine
            # re-imports assets). Observe-only for the same reason as pre_rollback.
            self._emit("post_rollback", task)
            return
        restored_before_pause: str | None = None
        if (
            restore_attempt_snapshot
            and owned_snapshot_changed
            and owned_spec
            and owned_current_bytes is not None
            and owned_baseline_bytes is not None
            and owned_current_bytes == owned_baseline_bytes
        ):
            # The failed child put a tracked, pre-edited spec back at the exact
            # baseline blob. Those child bytes are already durable in Git, so
            # restoring the byte-exact pre-launch operator input cannot destroy
            # evidence even though sibling residue still requires manual policy.
            assert task.dispatched_spec_snapshot is not None
            self._restore_attempt_owned_spec_bytes(owned_spec[0], task.dispatched_spec_snapshot)
            restored_before_pause = str(owned_spec[0])
            self.journal.append(
                "rollback-owned-spec-restored",
                story_key=task.story_key,
                spec=restored_before_pause,
                checkout_dirty=True,
            )
        self.pause_for_manual_recovery(
            task,
            task.baseline_commit or "",
            restored_spec=restored_before_pause,
        )
        return  # unreachable: pause_for_manual_recovery always raises

    def safe_reset(self, task: StoryTask, *, preserve: tuple[str, ...] = ()) -> None:
        """Revert tracked changes to the task baseline and remove only the
        untracked files this run created — never a blanket `git clean`. Used by
        the gated/resolved rollback and by internal ledger recovery (sweep
        migration), which restores the orchestrator's own state and must not
        pause. The BMAD artifact folders are always kept from untracked deletion;
        ``preserve`` (set only on a resolved re-drive) additionally keeps their
        *tracked* content alive through the reset, so a just-corrected spec is not
        reverted. Sweep passes no ``preserve`` — it wants the broken ledger gone.
        A cleanup-preflight refusal is journaled and routed through the injected
        pause before the re-drive can continue."""
        workspace = self._workspace_get()
        try:
            verify.safe_rollback(
                workspace.root,
                task.baseline_commit or "",
                baseline_untracked=task.baseline_untracked,
                keep=(".bmad-loop", *self.protected_relpaths()),
                preserve=preserve,
            )
        except verify.RollbackPreflightError as e:
            self.journal.append("rollback-reset-failed", story_key=task.story_key, error=str(e))
            self._pause(
                f"automatic rollback for {task.story_key} could not safely start: {e}. "
                "Fix the underlying filesystem fault, then resume the run.",
                task.story_key,
                cause=e,
            )

    def restore_patch(self, task: StoryTask) -> None:
        """Re-apply the latched intent-gap patch (BMAD-METHOD #2564) onto the
        baseline tree so the re-driven session resumes review (step-04) on the
        restored diff instead of re-implementing. No-op unless a restore is latched.

        Applied from inside `_dev_phase`'s loop, right before each dispatch that
        runs against a fresh baseline (the first attempt and every non-fixable
        rollback retry — the loop gates this on ``feedback is None``, so a
        fixable-feedback retry that KEEPS the attempt's tree is never double-applied,
        and the patch file's own untracked/tracked content is excluded from
        `baseline_untracked` because that snapshot is taken before the first apply).
        This is the plan's "apply after every baseline reset" seam, placed here
        rather than in `_rollback_or_pause` so the patch always lands after the
        clean baseline_untracked snapshot — avoiding a mid-re-drive reset preserving
        the patch's own new files and then colliding with the re-apply.

        On apply failure we escalate rather than dispatch a session onto a
        half-restored tree; the task is mid-dispatch (DEV_RUNNING), so step it to
        the escalatable DEV_VERIFY phase first (`_escalate` raises RunPaused)."""
        if not task.restore_patch:
            return
        workspace = self._workspace_get()
        patch = verify.resolve_restore_path(task.restore_patch, workspace.root)
        try:
            verify.apply_patch(workspace.root, patch)
        except (verify.GitError, OSError) as e:
            # OSError joins GitError because the patch file is read from disk
            # here, so an ENOENT/EACCES/ENOSPC arrives untyped — a non-spawn FS
            # fault the #343 chokepoint cannot translate. Crashing would skip
            # the escalation this branch exists to perform and leave the tree
            # half-restored with no attention file.
            self.journal.append(
                "attempt-restore-failed",
                story_key=task.story_key,
                patch=task.restore_patch,
                error=str(e),
            )
            # Call-site invariant: `_dev_phase` advances the task to DEV_RUNNING
            # immediately before dispatch, and this runs on that path only — so the
            # step to DEV_VERIFY is unconditional. It is required because `_escalate`
            # cannot transition out of DEV_RUNNING directly.
            advance(task, Phase.DEV_VERIFY)
            self._escalate(task, f"intent-gap restore patch failed to apply: {e}")
        self.journal.append("attempt-restored", story_key=task.story_key, patch=task.restore_patch)

    def prune_preserve_refs(self) -> None:
        """Bounded retention for both recovery-ref families at run start — the
        attempt-preserve/* branches and the refs/attempt-preserve-dirty/*
        worktree snapshots: keep the newest scm.preserve_keep of each by
        committer date, delete the tail (mirrors the runs/cleanup retention
        knobs — without it the refs grow unbounded on a long-lived project).
        Best-effort: a git failure is journalled per family and never blocks or
        pauses the run — the refs are a safety net, not run state — and a
        failure in one family never skips the other. preserve_keep = 0 disables
        pruning entirely."""
        keep = self.policy.scm.preserve_keep
        if keep <= 0:
            return
        workspace = self._workspace_get()
        for family, prune in (
            ("attempt-preserve", verify.prune_preserve_refs),
            ("attempt-preserve-dirty", verify.prune_preserve_dirty_refs),
        ):
            try:
                deleted = prune(workspace.root, keep)
            except Exception as exc:  # housekeeping must never crash the
                # run: a git timeout/OSError here would otherwise escape to the crash
                # handler, so anything beyond the expected GitError is journalled too
                # A partial prune (PrunePreserveError) already deleted refs before one
                # stuck — that destructive half must stay structurally auditable, not
                # buried in the error string.
                partial = getattr(exc, "deleted", [])
                if partial:
                    self.journal.append(f"{family}-pruned", count=len(partial), refs=partial)
                failed = getattr(exc, "failed", [])
                if failed:
                    self.journal.append(f"{family}-prune-failed", error=str(exc), failed=failed)
                else:
                    self.journal.append(f"{family}-prune-failed", error=str(exc))
                continue
            if deleted:
                self.journal.append(f"{family}-pruned", count=len(deleted), refs=deleted)

    def preserve_attempt_commits(self, task: StoryTask, *, allow_pause: bool) -> None:
        """Before an auto-rollback's hard reset, park any commits the attempt made
        above its baseline under a named recovery ref, so `reset --hard baseline`
        can't silently orphan committed work (it survives `git gc` and is
        recoverable by name, not just the reflog). No-op when the attempt added no
        commits — an uncommitted-only revert is the intended, non-destructive case.

        If commits exist but the ref cannot be created — or the range cannot be
        enumerated at all, which is the same thing one step earlier: with
        ``allow_pause`` (a plain rollback) refuse to reset — pause for manual
        recovery rather than destroy the work. Ordinary re-drive preservation uses
        ``allow_pause=False`` and journals before proceeding; a caller that will
        replace a changed owned-spec snapshot passes True because that destructive
        write is unsafe until the child commit is parked. The two failures journal under distinct events
        (``attempt-preserve-enumerate-failed`` vs ``attempt-preserve-failed``) so a
        post-mortem can tell "could not count the work" from "counted it but could
        not park it" — only the latter can report a HEAD."""
        baseline = task.baseline_commit
        if not baseline:
            return
        workspace = self._workspace_get()
        # Enumerating the range is what decides whether the reset is safe, so a
        # fault here is not a no-op: an un-determinable range must read as "there
        # may be work above baseline" and take the preservation-failure path below,
        # never the `not commits` early return (which would let the reset run
        # blind). These two calls carried no guard at all, so until #343 a plain
        # git *timeout* — which `_run_git` does translate, and which every sibling
        # here already treats as routine — crashed the rollback outright; OSError
        # joins it because the translation stops at timeouts. The `not commits`
        # return sits inside the try to keep the original call order: HEAD is still
        # only read once there is something to park.
        try:
            commits = verify.commits_above(workspace.root, baseline)
            if not commits:
                return
            head = verify.rev_parse_head(workspace.root)  # the tip the ref parks at
        except (verify.GitError, OSError) as exc:
            self.journal.append(
                "attempt-preserve-enumerate-failed", story_key=task.story_key, error=str(exc)
            )
            if allow_pause:
                # Same refusal as an un-parked ref: the notice must not tell the
                # operator to `reset --hard` past work we could not even count.
                self.pause_for_manual_recovery(task, baseline, preserve_failed=True)
            return  # re-drive: never pause — proceed to the (human-directed) reset
        # run_id can be an arbitrary user `--run-id`; ref-sanitize it (same
        # identity-for-clean-ids / digest-for-dirty contract as the unit branches) so
        # an exotic/overlong id can't blow the ref-name limit, fail `git branch`, and
        # drop the recovery ref (which on a re-drive would then reset past the work
        # anyway).
        slug = safe_ref_segment(self.state.run_id)
        try:
            ref = verify.preserve_commits(
                workspace.root,
                baseline,
                f"attempt-preserve/{slug}-{head[:8]}",
                commits=commits,
            )
        except (verify.GitError, OSError):
            ref = None  # branch creation failed — treat as a preservation failure
        if ref is None:
            # commits exist (just enumerated) but the ref did not take.
            self.journal.append("attempt-preserve-failed", story_key=task.story_key, head=head)
            if allow_pause:
                # the commits at HEAD could not be parked — the notice must NOT tell
                # the operator to blindly `reset --hard` (that would discard them).
                self.pause_for_manual_recovery(task, baseline, preserve_failed=True)
            return  # re-drive: never pause — proceed to the (human-directed) reset
        task.preserve_ref = ref
        self.journal.append(
            "attempt-commits-preserved", story_key=task.story_key, ref=ref, count=len(commits)
        )

    def preserve_attempt_worktree(
        self,
        task: StoryTask,
        *,
        allow_pause: bool,
        force_include: tuple[str, ...] = (),
    ) -> None:
        """Before an auto-rollback's hard reset, park the attempt's *uncommitted*
        working-tree changes (tracked edits + run-created untracked files) under a
        named recovery ref, so `reset --hard baseline` and its untracked cleanup
        can't silently destroy in-progress work. Complements
        `_preserve_attempt_commits` (which parks *committed* work above baseline);
        together they cover the whole attempt. No-op when the tree is clean vs HEAD
        — the intended non-destructive uncommitted-revert case.

        A capture failure is a gate, not a footnote (#340 — this reverses the
        original best-effort contract). The two preserve steps used to be
        asymmetric: the commits path refused to reset past work it could not park,
        while a failed snapshot journaled and let the reset run. That protected the
        *more* recoverable half — orphaned commits stay in the object store,
        reachable by reflog/`git fsck` until gc, whereas an uncommitted edit a
        `reset --hard` discards is gone permanently. Both paths now refuse on the
        same terms: with ``allow_pause`` (a plain rollback) pause for manual
        recovery rather than reset; ordinary re-drive preservation remains
        best-effort with ``allow_pause=False``. Snapshot-backed Git-invisible specs
        pass ``allow_pause=True`` even on a re-drive because restoration would
        otherwise overwrite the only child copy. Both paths guard
        ``(GitError, OSError)`` too: spawn faults
        arrive typed as ``GitSpawnError`` since #343, but ``snapshot_worktree``'s
        ``TemporaryDirectory`` can still raise a plain ``OSError`` (ENOSPC),
        which would otherwise crash the rollback rather than refuse it.

        The refusal is gated on :meth:`_reset_would_destroy`, so a capture failure
        over a tree with nothing left to lose (commits already parked, nothing
        uncommitted) still resets instead of halting an unattended run. The failure
        is journaled either way, and ``preserve_partial`` is latched either way —
        on the best-effort re-drive path the reset still runs, so the defer notice
        must still downgrade its claim to the committed half (#338)."""
        baseline = task.baseline_commit
        if not baseline:
            return
        workspace = self._workspace_get()
        # Same ref-sanitized slug as preserve_attempt_commits so an exotic/overlong
        # --run-id can't blow the ref-name limit and drop the ref.
        slug = safe_ref_segment(self.state.run_id)
        # ``baseline_commit`` is fixed across the whole dev retry loop, so keying the
        # ref on the baseline alone would make a 2nd dirty rollback reuse the name and
        # orphan the 1st attempt's snapshot. ``task.attempt`` discriminates the
        # retries of one arming but is NOT monotonic across the story's life:
        # runs.rearm_escalation resets it to 0, and a resolve session that commits
        # nothing leaves HEAD == baseline, so the post-resolve re-drive's rollback
        # recomputes the exact {slug}-{baseline}-{attempt} name of the pre-resolve
        # rollback and would overwrite that snapshot, destroying the only copy of
        # the first attempt's work. Probe for a free name instead of trusting the
        # counter: uniqueness is enforced against the refs that actually exist.
        # The probe runs INSIDE the try: `ref_exists` spawns git, and a timeout or
        # spawn fault arrives as GitError/GitSpawnError rather than a return code.
        # Uncaught it would crash the rollback here — the one thing this handler
        # exists to prevent — so a probe that cannot run degrades into the same
        # "preservation is observation" path as a snapshot that cannot be written.
        # The scan is BOUNDED: it terminates on its own (the ref set is finite and
        # `serial` only climbs), but termination is not a bound — the iteration
        # count is whatever the namespace happens to hold, one git spawn apiece, in
        # the middle of a crash-recovery path. PROBE_LIMIT turns "trust the
        # namespace is small" into an enforced invariant, and exhausting it raises
        # rather than reusing the last candidate: falling through to an occupied
        # name is the precise data loss this probe exists to prevent (#349).
        #
        # The probe is check-then-write, not atomic: `snapshot_worktree` finishes on a
        # plain two-arg `update-ref`, which overwrites whatever is there rather than
        # failing if the name were taken in between. Safe here because each name has
        # exactly one possible writer, on two independent grounds — the control loop
        # is sequential (nothing in this path threads, so one run never has two
        # rollbacks in flight), and the name is keyed on `run_id`, so separate runs
        # address disjoint namespaces. Only two processes driving the SAME run could
        # collide, and they would already be racing on run state, worktrees and mux
        # sessions; the remedy for that is run-level exclusion, not a compare-and-swap
        # on this one ref.
        base_ref = f"refs/attempt-preserve-dirty/{slug}-{baseline[:8]}-{task.attempt}"
        ref = base_ref
        serial = 2
        try:
            while verify.ref_exists(workspace.root, ref):
                if serial > PRESERVE_REF_PROBE_LIMIT:
                    # Remedy has to hold at BOTH ends of the preserve_keep range:
                    # "lower it" is impossible at 0, which is precisely the setting
                    # (pruning disabled) that lets this namespace grow far enough to
                    # exhaust the probe in the first place.
                    raise verify.PreserveRefExhaustedError(
                        f"no free snapshot refname for {base_ref}: "
                        f"{PRESERVE_REF_PROBE_LIMIT} candidates through -r{serial - 1} "
                        f"are all taken (prune refs/attempt-preserve-dirty/*, or set "
                        f"scm.preserve_keep to a positive value below that limit — "
                        f"0 disables pruning entirely)"
                    )
                ref = f"{base_ref}-r{serial}"
                serial += 1
            parked = verify.snapshot_worktree(
                workspace.root,
                ref,
                baseline_untracked=task.baseline_untracked,
                force_include=force_include,
            )
        except (verify.GitError, OSError) as exc:
            # OSError alongside GitError: spawn faults arrive typed as GitSpawnError
            # since #343, but `snapshot_worktree`'s `TemporaryDirectory` can raise a
            # plain OSError (ENOSPC) — a non-spawn FS fault the chokepoint cannot
            # translate, so this arm stays load-bearing. Uncaught it crashed the
            # run here — after the commits ref, before the reset — which is the safe
            # outcome reached the loudest possible way. Preservation is observation,
            # so it degrades into the decision below; `safe_reset` is the repair
            # write and still raises.
            # Keep the failure detail (commit-tree/update-ref stderr, or the errno):
            # if the reset that may follow destroys work, this is the only breadcrumb
            # explaining why the safety-net snapshot couldn't be captured.
            self.journal.append(
                "attempt-worktree-preserve-failed", story_key=task.story_key, error=str(exc)
            )
            # Latch the partial marker before deciding pause-vs-reset: on the
            # re-drive path below the reset still runs, so `preserve_ref` may name
            # the commits branch parked just above and the defer notice must offer
            # it as the committed half rather than as the whole attempt (#338). Set
            # unconditionally — snapshot_worktree can raise before it can tell
            # whether the tree was even dirty, so "could not capture" is the only
            # honest state. Harmless when nothing was parked: the notice
            # short-circuits on the ref first.
            task.preserve_partial = True
            if not allow_pause:
                return  # re-drive: never pause — proceed to the (human-directed) reset
            # Refuse the reset rather than destroy what the snapshot failed to save
            # (#340) — but only when something unparked is actually at stake, so a
            # git fault over a harmless reset can't halt an unattended run.
            if force_include or self._reset_would_destroy(task):
                self.pause_for_manual_recovery(task, baseline, snapshot_failed=True)
            return
        if parked:
            # Last writer wins over preserve_attempt_commits' branch on purpose:
            # the snapshot is commit-tree'd parented at the attempt's HEAD
            # (verify.snapshot_worktree), so it already contains the commits that
            # branch points at — one ref recovers the whole attempt. That holds on
            # this success path only; the `except` above records the case where the
            # snapshot failed and the commits branch is all that survived.
            task.preserve_ref = parked
            self.journal.append("attempt-worktree-preserved", story_key=task.story_key, ref=parked)

    def _reset_would_destroy(self, task: StoryTask) -> bool:
        """True when the pending `safe_reset` would still erase uncommitted work —
        the decision input for refusing a rollback whose snapshot failed (#340).

        Probes `verify.attempt_dirty` against *HEAD* rather than the attempt
        baseline. That reports exactly the tracked edits and run-created untracked
        files `safe_rollback` is about to drop, and ignores commits above baseline,
        which `preserve_attempt_commits` has already parked — or paused on — by the
        time this runs. So a capture failure over a tree whose content was all
        committed reads as nothing-to-lose and the reset proceeds: a snapshot fault
        must not halt an unattended run when the reset itself is harmless (#123).

        No ``exclude`` is passed because this only runs on the plain-rollback path,
        where `rollback_or_pause`'s ``protected`` is empty anyway. Fails safe: an
        un-determinable probe reads as work-at-risk, mirroring the dirty check's
        own git-fault doctrine (#156).

        Catches ``OSError`` for the same reason the caller does: spawn faults
        arrive as ``GitSpawnError`` since #343, but this runs immediately after a
        snapshot fault — often an ENOSPC out of ``snapshot_worktree``'s
        ``TemporaryDirectory`` — and a filesystem this broken can fail the probe
        in non-spawn ways too. Guarding only `GitError` would undo the broadening
        one frame up and crash the rollback anyway."""
        workspace = self._workspace_get()
        try:
            head = verify.rev_parse_head(workspace.root)
            return verify.attempt_dirty(workspace.root, head, task.baseline_untracked)
        except (verify.GitError, OSError):
            return True

    def pause_for_manual_recovery(
        self,
        task: StoryTask,
        baseline: str,
        *,
        preserve_failed: bool = False,
        snapshot_failed: bool = False,
        restored_spec: str | None = None,
    ) -> None:
        """Leave the tree untouched, surface bold manual-recovery instructions, and
        pause the run. Always raises RunPaused. Four notice shapes: (a, default)
        the OFF path for a stopped/abandoned in-place attempt with no commits of
        its own — plain manual-rollback steps; (b, ``preserve_failed``) rollback is
        ON/resolved but the attempt's commits above baseline could not be parked on
        a recovery ref, so an automatic ``reset --hard`` would silently discard
        them — a distinct notice that names the at-risk commits and never tells the
        operator to blindly reset; (c) the OFF path but the attempt COMMITTED work
        above its baseline (#100: a completed session whose run died before the
        orchestrator folded the result) — instructing a bare ``reset --hard`` there
        would discard finished, possibly already-pushed commits, so this notice
        tells the operator to save and check integration state first; (d,
        ``snapshot_failed``) the uncommitted-work snapshot could not be captured and
        the reset would have destroyed it (#340) — names the at-risk *working tree*
        rather than commits, and offers a git-free rescue because the fault that
        broke the snapshot may still be breaking git.

        The two flags are mutually exclusive by construction: they are raised from
        different call sites, and `preserve_attempt_commits` pauses before
        `preserve_attempt_worktree` ever runs. The initial ``cause=resolved`` unwind
        never reaches here: it auto-recovers regardless of
        ``scm.rollback_on_failure``. A later latched re-drive may use the
        snapshot-failed shape when exact Git-invisible owned-spec bytes could not
        be parked before recovery overwrites them."""
        workspace = self._workspace_get()
        short = baseline[:12] or "<baseline_commit>"
        # Name the tree every instruction targets. Usually the main checkout,
        # but a preserve-failure pause can fire while a unit worktree is
        # mounted — a bare "current HEAD" / `git reset --hard` there reads as
        # the operator's own checkout (whose HEAD is typically *at* the
        # baseline, making the quoted commit range empty) and invites a
        # destructive reset of a tree the attempt never touched (#161).
        root = workspace.root
        restored_note = (
            f"Before pausing, bmad-loop restored the byte-exact pre-launch operator "
            f"input at `{restored_spec}` because the failed child had put that tracked "
            "file back at its Git baseline. That restored edit is uncommitted; save it "
            "alongside any other work before resetting.\n"
            if restored_spec
            else ""
        )
        commits: list[str] = []
        if baseline:
            # Advisory probe: a git fault here must not block the pause itself —
            # including an untranslated spawn-level OSError, which is *likelier* on
            # the snapshot_failed path than anywhere else (the EMFILE/ENOMEM that
            # broke the capture is still in force when we come to write the notice).
            # Degrading to "no commits" only costs notice shape (c); crashing here
            # would lose the pause the caller already decided to take.
            try:
                commits = verify.commits_above(root, baseline)
            except (verify.GitError, OSError):
                commits = []
        if preserve_failed:
            notice = (
                "**ACTION REQUIRED — commits could not be auto-preserved**\n"
                f"Story **{task.story_key}**'s attempt committed work above its "
                "baseline, but a recovery ref for those commits could not be created, "
                "so the automatic rollback was refused rather than `reset --hard` "
                "past (and discard) them. **Your commits are intact at the current "
                f"HEAD of `{root}`.**\n"
                f'  1. **Save them first** — e.g. `git -C "{root}" branch my-rescue '
                f"HEAD` (the commits are `{short}..HEAD` there).\n"
                "  2. Only once they are safe, discard the attempt if you want to: "
                f'`git -C "{root}" reset --hard {short}`, then review/remove leftover '
                "untracked files.\n"
                f"Then run `bmad-loop resume {self.state.run_id}`."
            )
        elif snapshot_failed:
            # Name the committed half when it survived, so the operator is not left
            # assuming the whole attempt is at risk.
            parked = (
                f"The attempt's *committed* work is already parked at `{task.preserve_ref}`.\n"
                if task.preserve_ref
                else ""
            )
            notice = (
                "**ACTION REQUIRED — uncommitted work could not be auto-preserved**\n"
                f"Story **{task.story_key}**'s attempt left uncommitted changes, but the "
                "recovery snapshot could not be captured, so the automatic rollback was "
                "refused rather than `reset --hard` past (and permanently destroy) them. "
                "Unlike committed work, an uncommitted edit a reset discards is NOT "
                f"recoverable from the reflog. **Your working tree at `{root}` is "
                "untouched.**\n"
                "  1. **Save what you want to keep** — copy the files out, or "
                f'`git -C "{root}" diff > rescue.patch` plus any new untracked files.\n'
                "  2. Check the cause — the journal's `attempt-worktree-preserve-failed` "
                "entry carries git's own error; a full disk is the most common one.\n"
                "  3. Only once your work is safe: "
                f'`git -C "{root}" reset --hard {short}`, then review/remove leftover '
                "untracked files.\n"
                f"{parked}"
                f"Then run `bmad-loop resume {self.state.run_id}`."
            )
        elif commits:
            notice = (
                "**ACTION REQUIRED — manual recovery needed (committed work present)**\n"
                f"Story **{task.story_key}**'s attempt was stopped with auto-rollback "
                "OFF, and it **committed work above its baseline**. **Your commits "
                f"are intact at the current HEAD of `{root}`.** They may already be "
                "integrated or pushed to a remote — do NOT reset before checking.\n"
                f"{restored_note}"
                f'  1. **Save them first** — e.g. `git -C "{root}" branch my-rescue '
                f"HEAD` (the commits are `{short}..HEAD` there).\n"
                "  2. Check whether they are already integrated (merged, pushed to "
                "a remote, referenced by open PRs) before discarding anything.\n"
                "  3. Only if you decide to discard the attempt: "
                f'`git -C "{root}" reset --hard {short}`, then review/remove leftover '
                "untracked files.\n"
                f"Then run `bmad-loop resume {self.state.run_id}`."
            )
        else:
            why = (
                f"Story **{task.story_key}**'s attempt was stopped and auto-rollback "
                f"is OFF, so the working tree at `{root}` was left for you to inspect.\n"
                f"{restored_note}"
            )
            notice = (
                "**ACTION REQUIRED — manual rollback needed**\n"
                f"{why}"
                "To discard this attempt yourself:\n"
                "  1. **BACK UP any untracked files you want to keep** — the reset "
                "below deletes uncommitted work.\n"
                f'  2. `git -C "{root}" reset --hard {short}` then review/remove '
                "leftover untracked files.\n"
                "  3. **Restore the files you backed up in step 1.**\n"
                f"Then run `bmad-loop resume {self.state.run_id}`. To let the "
                "orchestrator do a safe automatic rollback next time, enable "
                "`[scm] rollback_on_failure` (it discards the attempt's uncommitted "
                "work but never deletes pre-existing untracked files)."
            )
        self.journal.append(
            "rollback-manual-required",
            story_key=task.story_key,
            baseline=baseline,
            commits=len(commits),
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"ACTION REQUIRED: manual rollback for {task.story_key}",
            notice,
        )
        self._save()
        self._pause(notice, task.story_key)
