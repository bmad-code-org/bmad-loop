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

import contextlib
import errno
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple, NoReturn, Protocol

from . import gates, verify
from .model import Phase
from .platform_util import (
    AT_NOFOLLOW,
    AT_NONBLOCK,
    HANDLE_ANCHORED_WRITES,
    atomic_write_bytes_at,
    open_at,
    open_dir_confined,
    require_root_pinned,
    safe_ref_segment,
    stat_at,
)
from .runs import mount_root_identity
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


def deferred_reverify_hint(run_id: str) -> str:
    """The pointer every pause on a DEFERRED story appends (DW-522): when only the
    environment failed, `resolve --reverify` keeps the attempt's work and replays
    verification on it instead of the steps that discard it."""
    return (
        "If the attempt's work is good and only the environment was broken (a service, "
        f"container or database was down), fix the environment and run `bmad-loop resolve "
        f"{run_id} --reverify` instead of resetting: it replays verification on the kept "
        "work and, when it passes, reviews and commits it without a new dev session."
    )


def attempt_preserve_ref_name(run_id: str, tip: str) -> str:
    """Canonical commits-only recovery branch for one run and pinned tip."""
    return f"attempt-preserve/{safe_ref_segment(run_id)}-{tip[:8]}"


def retry_preserve_paragraph(repo: Path, task: StoryTask, run_id: str) -> str:
    """The retry dev prompt's pointer at an earlier attempt's parked work (#777),
    or "" when the evidence does not support one. Informational only: the
    orchestrator never replays, merges or cherry-picks the ref, and the paragraph
    never asks the session to.

    Shared by every dev-prompt builder (``Engine._generic_dev_prompt``,
    ``StoriesEngine._stories_dev_prompt``, ``SweepEngine._generic_bundle_prompt``)
    through ``Engine._retry_preserve_notice``; each appends it, as its own
    paragraph, to its fresh-baseline legs only — a repair leg keeps the rejected
    attempt's tree, and a patch-restore leg has already laid that attempt back
    onto it. ``task.preserve_ref`` is set only by an auto-rollback of this task,
    so a set ref already means this dispatch follows a rolled-back attempt.

    Offered only when git confirms the claim, through the verify chokepoint:

    - the name is one this run's rollback mints — ``refs/attempt-preserve-dirty/
      <run>-<baseline8>-<attempt>[-rN]`` with ``<baseline8>`` this task's baseline,
      or the ``attempt-preserve/<run>-<tip8>`` commits branch whose ``<tip8>`` is
      the commit it still resolves to;
    - it resolves to a commit (``rev-parse --verify <ref>^{commit}``) — a ref the
      run-start retention pruned before a resume does not;
    - that commit descends from, and differs from, ``task.baseline_commit`` — so
      the offered ``git diff`` is this task's work over this task's tree. A
      baseline re-stamped past the work (another unit merged first) fails it.

    ``preserve_from_attempt`` must be set: git can show that the ref is this
    run's and sits on this baseline, but not that a dispatched attempt produced
    it. A resolve re-drive parks a tree no attempt wrote, and a sweep bundle
    replacement keeps the superseded bundle's ref on the same key and baseline;
    both leave the flag False.

    Says "an earlier attempt", never "the previous" one: a clean rollback keeps
    an older attempt's ref (see ``rollback_or_pause``), and ``task.attempt`` is
    re-armed to 0 by a resolve, so neither the ref nor the counter proves the
    work is the immediately preceding attempt's. ``preserve_partial`` narrows the
    claim to the commits alone. A failed check omits the paragraph and leaves the
    ref and task untouched: the ref may still be the only copy of that work."""
    ref = task.preserve_ref
    baseline = task.baseline_commit
    if not ref or not baseline or not task.preserve_from_attempt:
        return ""
    slug = re.escape(safe_ref_segment(run_id))
    dirty = re.fullmatch(rf"refs/attempt-preserve-dirty/{slug}-([0-9a-f]{{8}})-\d+(?:-r\d+)?", ref)
    commits = re.fullmatch(rf"attempt-preserve/{slug}-([0-9a-f]{{8}})", ref)
    if dirty is not None:
        if dirty.group(1) != baseline[:8]:
            return ""
        refname = ref
    elif commits is not None:
        # Fully qualified so neither the check nor the offered commands can fall
        # back to a same-named tag or remote ref.
        refname = f"refs/heads/{ref}"
    else:
        return ""
    try:
        tip = verify.rev_parse_revision(repo, refname)
    except (verify.GitError, OSError):
        return ""
    if commits is not None and tip[:8] != commits.group(1):
        return ""
    if tip == baseline or not verify.is_ancestor(repo, baseline, tip):
        return ""
    if task.preserve_partial:
        held = (
            f"only its commits were preserved, at `{refname}` — its uncommitted "
            f"changes were not captured there"
        )
    else:
        held = f"its work is preserved at `{refname}`"
    return (
        f"An earlier attempt at this work was rolled back; {held}. Inspect it with "
        f"`git log --oneline {baseline}..{refname}` and `git diff {baseline} {refname}`. "
        f"That work is unverified and has not been applied to this working tree: "
        f"judge anything you take from it against the spec, and every gate must "
        f"pass fresh on this attempt."
    )


class _FailedPark(NamedTuple):
    """A preserve leg that failed and fell through to the reset (DW-481).

    ``leg`` is ``commits-enumerate``, ``commits-park`` or ``worktree-snapshot``;
    ``head`` is the attempt HEAD observed at the failure, ``""`` when it could not
    be read (the fault is journaled beside it)."""

    leg: str
    head: str


class _OwnedSpecAuthorityError(RuntimeError):
    """A previously canonical owned spec lost trustworthy restore authority."""

    def __init__(
        self,
        message: str,
        *,
        safe_restoration_unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.safe_restoration_unavailable = safe_restoration_unavailable


class OwnedPlanRefused(Exception):
    """An :class:`OwnedPlanWriter` could not restore or re-route the plan; nothing
    the writer refused was written, and recovery pauses for the operator."""


class OwnedPlanWriter(Protocol):
    """The work source's own writer for an attempt-owned spec that is a work
    item's plan: its byte restore and its lifecycle route replace the recovery
    flow's anchored writers, so a plan is restored and re-routed through its own
    chokepoint (tickets mode: ``TicketTree.restore_preimage`` and ``mark``).
    Both raise :class:`OwnedPlanRefused` for a write they refuse or cannot
    make."""

    def owns(self, task: StoryTask, spec_path: Path) -> bool: ...

    def restore(self, task: StoryTask, spec_path: Path, data: bytes) -> None: ...

    def normalize(self, task: StoryTask, spec_path: Path, target_status: str) -> None: ...


def _target_stat_version(observed: os.stat_result) -> tuple[int, int, int]:
    """Mutation-sensitive fields subordinate to an already-bound target inode."""
    return observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns


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
    active workspace. ``dev_attempt_dispatched`` answers whether a dev session of
    the task's current attempt was dispatched — the provenance a rollback stamps
    on the ref it parks (``StoryTask.preserve_from_attempt``).

    ``spec_confine_root`` answers the root an attempt-owned spec write for a task
    is confined to (default: the active workspace's project, where a v6 spec
    lives; a ticket plan's is its store root), and ``owned_plan`` is the work
    source's own writer for a spec that is a work item's plan (default: none,
    the anchored writers here restore every spec)."""

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
        dev_attempt_dispatched: Callable[[StoryTask], bool],
        spec_confine_root: Callable[[StoryTask], Path] | None = None,
        owned_plan: OwnedPlanWriter | None = None,
        ledger_rel: Callable[[], tuple[str | None, Exception | None]] | None = None,
    ) -> None:
        self.paths = paths
        self.policy = policy
        self.state = state
        self.journal = journal
        self.run_dir = run_dir
        # Read live (a getter, not a captured ref) so a run that swaps the
        # engine's `self.workspace` to a mounted unit worktree is seen here.
        self._workspace_get = workspace_get
        # The active ledger relative to the workspace root (`Engine._ledger_rel`),
        # read live for the same reason. None: no bookkeeping filter (#772).
        self._ledger_rel = ledger_rel
        # Injected late-bound so a test patching `engine._emit` still wins
        # (recovery_flow's own binding wouldn't).
        self._emit = emit
        self._save = save
        self._escalate = escalate
        self._escalation_pause = escalation_pause
        # Tally of pauses raised through `_pause`. `rollback_or_pause` compares it
        # across its reset arm to tell a propagating pause ("paused") from any
        # other escaping error ("failed") for `post_rollback` without importing
        # the engine's `RunPaused` (DW-322).
        self._pauses_raised = 0
        self._dev_attempt_dispatched = dev_attempt_dispatched
        self._spec_confine_root_for = spec_confine_root
        self._owned_plan = owned_plan

    def _spec_confine_root(self, task: StoryTask) -> Path:
        """The root every attempt-owned spec write for ``task`` is confined to."""
        if self._spec_confine_root_for is not None:
            return self._spec_confine_root_for(task)
        return self._workspace_get().paths.project

    def _pause(self, reason: str, story_key: str = "", **kwargs: object) -> NoReturn:
        """Raise the injected escalation pause, marking that this flow paused.

        Every pause raised inside ``rollback_or_pause``'s reset arm routes through
        here, so it can label the paired ``post_rollback`` emit. (``restore_patch``
        escalates through ``self._escalate`` instead, outside any rollback.)"""
        self._pauses_raised += 1
        self._escalation_pause(reason, story_key, **kwargs)

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
        """Bind recovery restoration to this attempt's spec and exact Git exclusion.

        This is the restore authority for the dispatched attempt, not an ordinary
        path lookup. Operations on the tree recorded by a task use
        ``runs.task_spec_path``, which anchors a bare basename directly on that tree.
        Binding a reported or persisted spelling inside the current active
        ``ProjectPaths`` uses ``verify.resolve_spec_path``, which chooses an existing
        project candidate or falls back under implementation artifacts. Here a bare
        basename probes both locations and is accepted only when exactly one trusted
        regular-file candidate exists.

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
            except (OSError, RuntimeError, ValueError):
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
        spec_path: Path,
        target_status: str,
        *,
        confine_root: Path,
        expected: verify.FileIdentity | None = None,
        root_identity: os.stat_result | None = None,
    ) -> None:
        """Write and verify the lifecycle route recovery promises to dispatch.

        ``confine_root`` is the project that owns the binding
        (``workspace.paths.project`` — the same root `_attempt_owned_spec`
        resolves candidates under), threaded down rather than re-derived here:
        this is a staticmethod on purpose, and `_workspace_get` is a live getter
        precisely because a unit worktree swaps the root mid-run (``rebased``
        makes ``paths.project`` the mount project there — the worktree root, or
        ``<worktree>/<offset>`` for a project nested in ``repo_root``). It must NOT be
        ``workspace.root``: under the `repo_root` override that is the separate
        code repo, an in-project spec fails its `is_relative_to` test, and the
        chokepoint silently takes the plain arm — dropping the parent walk the
        confinement exists for. An artifacts folder configured outside the
        project is a trusted repair target here (`_attempt_owned_spec`) when
        handle-anchored writes are available.

        ``root_identity`` pins ``confine_root`` (DW-445): the callers pass
        `_mount_root_identity` of it — `runs.mount_root_identity` when the
        workspace is a unit mount, ``None`` for the operator's project — so a
        mount swapped for a link, before or after the spec path was bound,
        refuses with `platform_util.UnconfinedWriteError` (an ``OSError``, raised
        as any unreachable parent is) and nothing lands outside the repository.
        The pin compares the unit worktree against its mint-time record
        (``task.worktree_identity``, DW-446) and walks ``O_NOFOLLOW`` down to
        ``confine_root``, so an ancestor swapped for a link refuses too.

        The write is `frontmatter.set_frontmatter_status_anchored` (DW-323), the
        fifth spec writer: the confinement rule stated in
        `frontmatter.set_frontmatter_status` in-project, a canonical
        filesystem-root walk (not the plain path write) for an external target,
        and one bound target identity checked at the read, before staging and
        immediately before the replace. ``expected`` is the identity
        `_restore_attempt_owned_spec_bytes` just published (DW-319); when the
        file no longer matches it — an operator edit or swap landed between
        restoration and this normalization — nothing is written and the refusal
        becomes `_OwnedSpecAuthorityError`, so the ``*_or_pause`` wrappers clear
        the authority pair and pause with the operator's bytes untouched. The
        final check is not a compare-and-swap: an in-place edit of the old inode
        in the instant between it and the replace is not detected. Acceptance
        reads the returned identity's bytes, never the path again."""
        # A path-based fallback cannot retain publication authority across the
        # final replace. Refuse before any repair write so a substituted parent
        # or target cannot redirect staging, publication, or cleanup. Both the
        # POSIX `dir_fd` arm and the Windows handle-relative arm anchor
        # (`platform_util.HANDLE_ANCHORED_WRITES`); only a host with neither
        # reaches this refusal.
        if not HANDLE_ANCHORED_WRITES:
            raise _OwnedSpecAuthorityError(
                "safe automatic attempt-owned spec restoration is unavailable because "
                "it cannot be verified without handle-anchored writes: "
                f"{spec_path}",
                safe_restoration_unavailable=True,
            )
        try:
            published = verify.set_frontmatter_status_anchored(
                spec_path,
                target_status,
                confine_root=confine_root,
                expected=expected,
                root_identity=root_identity,
            )
        except verify.FrontmatterTargetChangedError as exc:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target changed during normalization ({exc}): {spec_path}"
            ) from exc
        text = published.data.decode("utf-8")  # the writer already decoded it
        if verify.status_of(verify.parse_frontmatter(text)) != target_status:
            raise verify.FrontmatterWriteError(
                f"could not normalize attempt-owned spec {spec_path} to status {target_status!r}"
            )

    @staticmethod
    def _restore_attempt_owned_spec_bytes(
        spec_path: Path,
        snapshot: bytes,
        *,
        confine_root: Path | None = None,
        root_identity: os.stat_result | None = None,
    ) -> verify.FileIdentity:
        """Restore and verify the byte-exact pre-attempt input.

        Returns the published inode's identity — a stable anchored read taken
        after the writer released it, required to be that same inode holding
        exactly ``snapshot`` — so a following normalization can require the
        file it rewrites to be the one restored here (DW-319).

        ``root_identity`` pins ``confine_root`` (DW-445) by a pre-check before
        anything is probed, created or written (`platform_util.require_root_pinned`).
        The canonical filesystem-root walk below already refuses a link anywhere on
        the path it is handed, but a path bound through a unit mount swapped BEFORE
        binding is canonical outside the mount — only the mount no longer being the
        pinned directory tells it apart. The pre-check's refusal surfaces like every
        other unsafe target here, as `_OwnedSpecAuthorityError` (so the
        ``*_or_pause`` wrappers pause), never as a raw `UnconfinedWriteError`.
        ``None`` (the default) skips the pre-check."""
        if root_identity is not None and confine_root is None:
            raise ValueError("root_identity pins confine_root; pass both")
        parent = spec_path.parent
        try:
            if root_identity is not None:
                assert confine_root is not None
                require_root_pinned(confine_root, root_identity)
            # Validate the full spelling before creating any missing component.
            # The nearest-existing-parent walk proves the live prefix separately;
            # this non-strict probe catches an invalid unresolved suffix first.
            if parent.resolve() != parent or spec_path.resolve() != spec_path:
                raise _OwnedSpecAuthorityError(
                    f"attempt-owned spec target became unsafe: {spec_path}"
                )
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
        except _OwnedSpecAuthorityError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target could not be revalidated: {spec_path}"
            ) from exc

        if not HANDLE_ANCHORED_WRITES:
            raise _OwnedSpecAuthorityError(
                "safe automatic attempt-owned spec restoration is unavailable because "
                "it cannot be verified without handle-anchored writes: "
                f"{spec_path}",
                safe_restoration_unavailable=True,
            )

        # Creation is a repair write. Preserve the established typed translation
        # for OS/symlink-loop failures, but let a ValueError from mkdir itself
        # escape raw rather than misclassifying it as an authority probe failure.
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError) as exc:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target could not be revalidated: {spec_path}"
            ) from exc

        try:
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
        except (OSError, RuntimeError, ValueError) as exc:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target could not be revalidated: {spec_path}"
            ) from exc
        authority_message = f"attempt-owned spec target became unsafe: {spec_path}"
        mismatch_message = f"could not restore pre-attempt contents of owned spec {spec_path}"

        def verify_parent_authority(parent_fd: int) -> None:
            probe_fd = open_dir_confined(Path(spec_path.anchor), parent, search_only=True)
            if probe_fd is None:
                raise _OwnedSpecAuthorityError(authority_message)
            try:
                if not os.path.samestat(os.fstat(parent_fd), os.fstat(probe_fd)):
                    raise _OwnedSpecAuthorityError(authority_message)
            finally:
                os.close(probe_fd)

        def target_stat_at(parent_fd: int) -> os.stat_result | None:
            try:
                observed = stat_at(parent_fd, spec_path.name)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(observed.st_mode):
                raise _OwnedSpecAuthorityError(authority_message)
            return observed

        def read_target_at(
            parent_fd: int,
        ) -> tuple[os.stat_result, bytes] | None:
            observed = target_stat_at(parent_fd)
            if observed is None:
                return None
            flags = os.O_RDONLY | AT_NOFOLLOW | AT_NONBLOCK
            try:
                target_fd = open_at(parent_fd, spec_path.name, flags)
            except OSError as exc:
                if exc.errno in {
                    errno.ELOOP,
                    errno.ENOENT,
                    errno.ENOTDIR,
                    errno.ENXIO,
                    errno.ENODEV,
                }:
                    raise _OwnedSpecAuthorityError(authority_message) from exc
                raise
            try:
                before = os.fstat(target_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or not os.path.samestat(observed, before)
                    or _target_stat_version(observed) != _target_stat_version(before)
                ):
                    raise _OwnedSpecAuthorityError(authority_message)

                os.lseek(target_fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                remaining = before.st_size + 1
                while remaining:
                    chunk = os.read(target_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                contents = b"".join(chunks)

                after = os.fstat(target_fd)
                named = target_stat_at(parent_fd)
                if (
                    len(contents) != before.st_size
                    or not os.path.samestat(before, after)
                    or _target_stat_version(before) != _target_stat_version(after)
                    or named is None
                    or not os.path.samestat(after, named)
                    or _target_stat_version(after) != _target_stat_version(named)
                ):
                    raise _OwnedSpecAuthorityError(authority_message)
                return after, contents
            finally:
                os.close(target_fd)

        def require_same_target_at(
            parent_fd: int, expected: tuple[os.stat_result, bytes] | None
        ) -> None:
            observed = read_target_at(parent_fd)
            if expected is None:
                if observed is not None:
                    raise _OwnedSpecAuthorityError(authority_message)
                return
            if observed is None:
                raise _OwnedSpecAuthorityError(authority_message)
            expected_stat, expected_bytes = expected
            observed_stat, observed_bytes = observed
            if (
                not os.path.samestat(expected_stat, observed_stat)
                or _target_stat_version(expected_stat) != _target_stat_version(observed_stat)
                or expected_bytes != observed_bytes
            ):
                raise _OwnedSpecAuthorityError(authority_message)

        def verify_published_inode(parent_fd: int, published_fd: int) -> None:
            # The writer keeps this exact staged inode open across publication.
            # Opening the live name no-follow/nonblocking proves it still names
            # that inode without following a link or waiting on a planted FIFO.
            flags = os.O_RDONLY | AT_NOFOLLOW | AT_NONBLOCK
            try:
                live_fd = open_at(parent_fd, spec_path.name, flags)
            except OSError as exc:
                if exc.errno in {
                    errno.ELOOP,
                    errno.ENOENT,
                    errno.ENOTDIR,
                    errno.ENXIO,
                    errno.ENODEV,
                }:
                    raise _OwnedSpecAuthorityError(authority_message) from exc
                raise
            try:
                before = os.fstat(published_fd)
                live = os.fstat(live_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or not stat.S_ISREG(live.st_mode)
                    or not os.path.samestat(before, live)
                ):
                    raise _OwnedSpecAuthorityError(authority_message)

                os.lseek(published_fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                remaining = len(snapshot) + 1
                while remaining:
                    chunk = os.read(published_fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)

                after = os.fstat(published_fd)
                stable_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                stable_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                if not os.path.samestat(before, after) or stable_before != stable_after:
                    raise _OwnedSpecAuthorityError(authority_message)

                try:
                    named = stat_at(parent_fd, spec_path.name)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
                        raise _OwnedSpecAuthorityError(authority_message) from exc
                    raise
                if not stat.S_ISREG(named.st_mode) or not os.path.samestat(live, named):
                    raise _OwnedSpecAuthorityError(authority_message)
            finally:
                os.close(live_fd)

            if b"".join(chunks) != snapshot:
                raise verify.FrontmatterWriteError(mismatch_message)

            # This fresh filesystem-root walk is deliberately the last
            # acceptance action. It observes the canonical spelling without
            # replacing the retained descriptor as authority.
            verify_parent_authority(parent_fd)

        # `require_writable_target=True` (#597): the spec this puts back is
        # operator-editable, and a temp-and-replace write needs write permission
        # on the parent directory, never on the entry it replaces. Anchor from
        # the filesystem root rather than the project so configured external
        # artifact roots remain valid repair targets.
        parent_fd = open_dir_confined(Path(spec_path.anchor), parent, search_only=True)
        if parent_fd is None:
            raise _OwnedSpecAuthorityError(
                f"attempt-owned spec target could not be revalidated: {spec_path}"
            )
        published: list[os.stat_result] = []
        try:
            expected = read_target_at(parent_fd)

            def validate_target() -> None:
                require_same_target_at(parent_fd, expected)

            def verify_published(published_fd: int) -> None:
                verify_published_inode(parent_fd, published_fd)
                published.append(os.fstat(published_fd))

            atomic_write_bytes_at(
                parent_fd,
                spec_path.name,
                snapshot,
                _require_writable_target=True,
                _before_staging=validate_target,
                _before_replace=validate_target,
                _after_replace=verify_published,
            )
            # The identity handed to normalization is read back only now, after
            # the writer released the published inode, through the same
            # anchored read normalization will bind with — so it compares like
            # with like even if a host settles timestamps when the writing
            # handle closes. It must still be the published inode holding
            # exactly the snapshot.
            settled = read_target_at(parent_fd)
            if (
                settled is None
                or not os.path.samestat(settled[0], published[0])
                or settled[1] != snapshot
            ):
                raise _OwnedSpecAuthorityError(authority_message)
        finally:
            os.close(parent_fd)
        return verify.FileIdentity(*settled)

    @classmethod
    def _restore_attempt_owned_spec(
        cls,
        spec_path: Path,
        snapshot: bytes,
        target_status: str,
        *,
        confine_root: Path,
        root_identity: os.stat_result | None = None,
    ) -> None:
        """Restore exact pre-attempt bytes, then verify the promised route.

        ``root_identity`` pins ``confine_root`` for both transactions (DW-445):
        the byte restore's pre-check and the normalization's anchored write."""
        restored = cls._restore_attempt_owned_spec_bytes(
            spec_path, snapshot, confine_root=confine_root, root_identity=root_identity
        )
        # The durable snapshot should already carry this route. Keep the status
        # repair as a fail-safe for a legacy or externally edited state record;
        # it is the only permitted difference from the exact snapshot. The
        # restored identity rides along (DW-319): an edit landing between the
        # two transactions is the operator's, and it pauses rather than being
        # overwritten by a normalization that never saw it.
        cls._normalize_attempt_owned_spec(
            spec_path,
            target_status,
            confine_root=confine_root,
            expected=restored,
            root_identity=root_identity,
        )

    @staticmethod
    def _owned_spec_restore_problem(
        exc: _OwnedSpecAuthorityError,
        *,
        unsafe_context: str,
        expected_status: str | None = None,
    ) -> str:
        if exc.safe_restoration_unavailable:
            status_guidance = (
                f"; the adopted spec must have lifecycle status {expected_status!r}"
                if expected_status is not None
                else ""
            )
            return (
                f"safe automatic restoration is unavailable {unsafe_context} because "
                "this platform lacks handle-anchored writes"
                f"{status_guidance}; manual adoption is required"
            )
        return f"its path became unsafe {unsafe_context} ({exc})"

    def _restore_attempt_owned_spec_bytes_or_pause(
        self,
        task: StoryTask,
        spec_path: Path,
        snapshot: bytes,
        *,
        unsafe_context: str,
        confine_root: Path | None = None,
        root_identity: os.stat_result | None = None,
    ) -> None:
        if self._route_owned_plan(task, spec_path, data=snapshot, unsafe_context=unsafe_context):
            return
        try:
            self._restore_attempt_owned_spec_bytes(
                spec_path, snapshot, confine_root=confine_root, root_identity=root_identity
            )
        except _OwnedSpecAuthorityError as exc:
            self.pause_for_owned_spec_recovery(
                task,
                str(spec_path),
                self._owned_spec_restore_problem(exc, unsafe_context=unsafe_context),
            )

    def _restore_attempt_owned_spec_or_pause(
        self,
        task: StoryTask,
        spec_path: Path,
        snapshot: bytes,
        target_status: str,
        *,
        confine_root: Path,
        unsafe_context: str,
        root_identity: os.stat_result | None = None,
    ) -> None:
        if self._route_owned_plan(
            task,
            spec_path,
            data=snapshot,
            target_status=target_status,
            unsafe_context=unsafe_context,
        ):
            return
        try:
            self._restore_attempt_owned_spec(
                spec_path,
                snapshot,
                target_status,
                confine_root=confine_root,
                root_identity=root_identity,
            )
        except _OwnedSpecAuthorityError as exc:
            self.pause_for_owned_spec_recovery(
                task,
                str(spec_path),
                self._owned_spec_restore_problem(
                    exc,
                    unsafe_context=unsafe_context,
                    expected_status=target_status,
                ),
            )

    def _route_owned_plan(
        self,
        task: StoryTask,
        spec_path: Path,
        *,
        data: bytes | None = None,
        target_status: str | None = None,
        unsafe_context: str,
    ) -> bool:
        """Restore ``data`` and/or route the plan to ``target_status`` through the
        work source's own writer when it owns ``spec_path``; False when it does
        not, and the caller's anchored writers run instead.

        The plan must resolve inside :meth:`_spec_confine_root` (the store root):
        one that does not is never handed to the writer, and recovery pauses as
        for any unsafe owned spec — so is a write the writer refuses. The check is
        made on the resolved path before the write; the writer's own lock covers
        the write itself."""
        writer = self._owned_plan
        if writer is None or not writer.owns(task, spec_path):
            return False
        root = self._spec_confine_root(task)
        try:
            confined = spec_path.resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError):
            confined = False
        if not confined:
            self.pause_for_owned_spec_recovery(
                task,
                str(spec_path),
                f"it lies outside the store root {root} {unsafe_context}",
            )
        try:
            if data is not None:
                writer.restore(task, spec_path, data)
            if target_status is not None:
                writer.normalize(task, spec_path, target_status)
        except OwnedPlanRefused as exc:
            self.pause_for_owned_spec_recovery(
                task,
                str(spec_path),
                f"its plan could not be put back through its own chokepoint "
                f"{unsafe_context} ({exc})",
            )
        return True

    def _mount_root_identity(self, task: StoryTask, workspace: Workspace) -> os.stat_result | None:
        """The ``root_identity`` pinning :meth:`_spec_confine_root` (the active
        workspace's project for a v6 spec) — the ``confine_root`` every
        attempt-owned normalization passes — for one write
        (DW-445). Take it at the call, beside that ``confine_root``.

        ``None`` when ``workspace`` is not a unit mount (``workspace.root ==
        self.paths.repo_root``, the ``in_unit_worktree`` idiom): the operator's
        project stays unpinned. Otherwise `runs.mount_root_identity` against
        ``task.worktree_identity``, the mount's MINT-TIME record (DW-446), which
        answers a never-matching identity for a mount that cannot be pinned — a
        missing record, a mount or ancestor (``worktrees/``, ``runs/<id>/``)
        swapped for a link — so the refusal lands at the write. The live
        ``workspace`` decides mountedness: it is the tree the write opens.
        ``paths.project`` is reached from the recorded mount by an ``O_NOFOLLOW``
        walk (``<worktree>/<offset>`` under a nested project, DW-486)."""
        if workspace.root == self.paths.repo_root:
            return None
        return mount_root_identity(
            self._spec_confine_root(task), mount=workspace.root, recorded=task.worktree_identity
        )

    def _normalize_attempt_owned_spec_or_pause(
        self,
        task: StoryTask,
        spec_path: Path,
        target_status: str,
        *,
        confine_root: Path,
        unsafe_context: str,
        root_identity: os.stat_result | None = None,
    ) -> None:
        if self._route_owned_plan(
            task, spec_path, target_status=target_status, unsafe_context=unsafe_context
        ):
            return
        try:
            self._normalize_attempt_owned_spec(
                spec_path,
                target_status,
                confine_root=confine_root,
                root_identity=root_identity,
            )
        except _OwnedSpecAuthorityError as exc:
            self.pause_for_owned_spec_recovery(
                task,
                str(spec_path),
                self._owned_spec_restore_problem(
                    exc,
                    unsafe_context=unsafe_context,
                    expected_status=target_status,
                ),
            )

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
        saving so resume does not repeat the same impossible snapshot check
        forever.

        Clearing the pair does not move ``task.baseline_commit``: resume re-runs
        ``rollback_or_pause`` against that same baseline with no binding left to
        recognize the spec. On a plain attempt a Git-tracked spec differing from
        its baseline blob — uncommitted, or committed on top of the baseline — is
        then ordinary attempt residue (auto-rollback parks and resets it; with
        rollback off the run pauses again), so the notice's convergent step is
        returning the checkout to the recorded baseline, and approved edits that
        differ from it cannot reach the next attempt (DW-321). A latched re-drive
        (``task.resolved_redrive``) differs only when resume will auto-recover it
        (the ``cause="resolved"`` unwind is still armed, or auto-rollback is on)
        and the spec lies under the BMAD artifact folders: that reset preserves
        those folders, so an approved spec kept there survives and only the other
        residue needs resetting. Any other re-drive gets the plain-attempt step —
        with rollback off, a dirty kept spec would only pause again, and a spec
        outside the folders is reset like any other file.
        """
        task.dispatched_spec_file = None
        task.dispatched_spec_snapshot = None
        task.dispatched_spec_absent = False
        root = self._workspace_get().root
        # The paths are folded to one segment of their line wherever the notice
        # shows them (DW-492), like `problem` below (DW-417); the journal row keeps
        # `spec` raw, and the recovery logic reads the raw values.
        shown_spec = gates.notice_line(spec)
        shown_root = gates.notice_line(str(root))
        short = (task.baseline_commit or "")[:12]
        if short:
            baseline_name = f"the attempt baseline `{short}`"
            reset_step = f'`git -C "{shown_root}" reset --hard {short}`'
        else:
            baseline_name = "the commit the attempt started from (not recorded)"
            reset_step = "reset tracked files to that commit"
        save_step = (
            "  1. Save any failed-session work you may want to inspect — commits "
            f'too, e.g. `git -C "{shown_root}" branch my-rescue HEAD`'
        )
        spec_rel = ""
        if Path(spec).is_absolute():
            with contextlib.suppress(ValueError):
                spec_rel = Path(spec).relative_to(root).as_posix()
        redrive_keeps_spec = (
            task.resolved_redrive
            and (task.rearmed or self.policy.scm.rollback_on_failure)
            and any(spec_rel.startswith(f"{rel}/") for rel in self.protected_relpaths())
        )
        if redrive_keeps_spec:
            contract = (
                f"Resume re-checks this checkout against {baseline_name}, and the "
                f"cleared binding no longer protects `{shown_spec}`. On this re-drive, "
                "resume rolls the checkout back automatically, and an approved spec "
                "kept under the BMAD artifact folders survives resume's reset.\n"
            )
            steps = (
                f"{save_step}.\n"
                f"  2. Keep the approved contents of `{shown_spec}` in place and return the "
                f"other residue in `{shown_root}` to {baseline_name}, then review/remove "
                "leftover untracked files.\n"
                f"  3. Run `bmad-loop resume {self.state.run_id}`."
            )
        else:
            contract = (
                f"Resume re-checks this checkout against {baseline_name}, and the "
                f"cleared binding no longer protects `{shown_spec}`: if it is Git-tracked, "
                "edits that differ from its baseline version — uncommitted or "
                "committed on top of the baseline — count as attempt residue "
                "(automatic rollback parks and resets them; with it off the run "
                "pauses again). **Resume will not adopt them.**\n"
            )
            steps = (
                f"{save_step} — and keep a copy of any approved edits to `{shown_spec}` "
                "that differ from its baseline version.\n"
                f"  2. Return `{shown_root}` to {baseline_name}: {reset_step}, then "
                "review/remove leftover untracked files.\n"
                f"  3. Run `bmad-loop resume {self.state.run_id}`. If `{shown_spec}` is "
                "Git-tracked, the next attempt starts from its baseline version: "
                "resume cannot carry the kept edits into it; they can only be "
                "re-applied afterwards (e.g. as a later correction)."
            )
        # `problem` is folded to one segment of its line (DW-417); the journal
        # row below keeps it raw.
        notice = (
            "**ACTION REQUIRED — attempt-owned spec needs manual recovery**\n"
            f"Story **{task.story_key}** cannot safely restore its pre-attempt spec "
            f"at `{shown_spec}`: {gates.notice_line(problem)}. The working tree at `{shown_root}` now requires "
            "inspection because bmad-loop cannot safely distinguish operator "
            "intent, failed-session output, and any rollback already completed.\n"
            f"{contract}{steps}"
        )
        if task.phase == Phase.DEFERRED:
            notice += f"\n{deferred_reverify_hint(self.state.run_id)}"
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
            multiline=True,
        )
        self._save()
        self._pause(notice, task.story_key)

    def rollback_or_pause(
        self, task: StoryTask, *, cause: str = "stopped", restart: bool = False
    ) -> None:
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
        resolve workflow and re-armed the story), so it bypasses the policy pause
        and selects auto-recovery regardless of ``scm.rollback_on_failure``.
        Unsafe attempt-owned authority can still require manual recovery. For the
        entire re-drive (``task.resolved_redrive``, latched at resume and cleared
        once the correction is committed) the BMAD artifact folders are preserved
        through every reset — so a later mid-re-drive retry/defer reset can't
        silently revert the correction. Whole folders never participate in the
        dirtiness decision; sibling artifact residue remains visible there.

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
        one.

        Plugin hooks bracket only the auto-recover arm: it emits ``pre_rollback``
        before parking/resetting and exactly one paired ``post_rollback`` on every
        exit from it (DW-322), carrying ``rollback_outcome`` — ``"completed"``,
        ``"paused"`` when one of the pauses above propagates, or ``"failed"`` when
        any other exception escapes (it propagates unchanged after the emit). The
        clean short-circuits, the early owned-spec pauses and the policy-OFF manual
        pause emit neither stage.

        ``restart`` marks the call as a resume restart arm (``Engine._finish_inflight``
        or the sweep bundle restart leg) rather than an in-run retry/defer rollback.
        Only then, and only once commits above the baseline or uncommitted changes
        were parked AND ``safe_reset`` completed, does the rollback send a
        parked-work notice naming every ref it parked (DW-371, DW-480, DW-482): on
        resume those commits may have been made while the run was down, and a
        file-only journal entry is too quiet for that. A re-drive's best-effort
        preserve leg that fails still falls through to the reset, but the same
        post-reset notice then names the failed leg and the attempt HEAD, on any
        re-drive reset, restart or not (DW-481)."""
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
                            self._restore_attempt_owned_spec_or_pause(
                                task,
                                spec_path,
                                task.dispatched_spec_snapshot,
                                target_status,
                                confine_root=self._spec_confine_root(task),
                                root_identity=self._mount_root_identity(task, workspace),
                                unsafe_context="while restoring the pre-attempt retry input",
                            )
                            owned_snapshot_restored = True
                        else:
                            self._normalize_attempt_owned_spec_or_pause(
                                task,
                                spec_path,
                                target_status,
                                confine_root=self._spec_confine_root(task),
                                root_identity=self._mount_root_identity(task, workspace),
                                unsafe_context=(
                                    "while restoring the attempt-owned lifecycle status"
                                ),
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
                                self._restore_attempt_owned_spec_bytes_or_pause(
                                    task,
                                    spec_path,
                                    task.dispatched_spec_snapshot,
                                    unsafe_context=(
                                        "while restoring the pre-launch operator input"
                                    ),
                                    confine_root=self._spec_confine_root(task),
                                    root_identity=self._mount_root_identity(task, workspace),
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
                    self._restore_attempt_owned_spec_bytes_or_pause(
                        task,
                        spec_path,
                        original_spec,
                        unsafe_context="while undoing a tentative lifecycle repair",
                        confine_root=self._spec_confine_root(task),
                        root_identity=self._mount_root_identity(task, workspace),
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
            # Provenance for whatever this rollback parks (#777): the retry
            # prompt names the ref only when a dev session of this attempt ran.
            task.preserve_from_attempt = self._dev_attempt_dispatched(task)
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
            # Pair every emitted pre_rollback with exactly one post_rollback (DW-322):
            # any pause (owned-spec recovery, preserve failure, reset refusal) or error
            # escaping the reset below still gives a quiescing plugin its release, with
            # `rollback_outcome` saying how the rollback ended. While a pause/error is
            # propagating, a failure of the emit itself (context build, journal
            # write) is suppressed so it cannot replace the original exception.
            pauses_before = self._pauses_raised
            outcome = "failed"
            try:
                force_owned_snapshot = (
                    owned_exclude
                    if restore_attempt_snapshot and (owned_snapshot_changed or owned_index_changed)
                    else ()
                )
                # A re-drive ordinarily preserves best-effort, but restoration of a
                # changed bound spec is destructive unless both its committed and
                # uncommitted child state can be parked first. The best-effort legs
                # report what they could not park here, for the notice after the
                # reset (DW-481).
                failed_parks: list[_FailedPark] = []
                parked = self.preserve_attempt_commits(
                    task,
                    allow_pause=not redrive or bool(force_owned_snapshot),
                    failed_parks=failed_parks,
                )
                snapshot: str | None = None
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
                    snapshot = self.preserve_attempt_worktree(
                        task,
                        allow_pause=not redrive or bool(force_owned_snapshot),
                        force_include=force_owned_snapshot,
                        failed_parks=failed_parks,
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
                        self._restore_attempt_owned_spec_or_pause(
                            task,
                            owned_spec[0],
                            task.dispatched_spec_snapshot,
                            target_status,
                            confine_root=self._spec_confine_root(task),
                            root_identity=self._mount_root_identity(task, workspace),
                            unsafe_context="before the baseline reset",
                        )
                    else:
                        self._restore_attempt_owned_spec_bytes_or_pause(
                            task,
                            owned_spec[0],
                            task.dispatched_spec_snapshot,
                            unsafe_context="before the baseline reset",
                            confine_root=self._spec_confine_root(task),
                            root_identity=self._mount_root_identity(task, workspace),
                        )
                    owned_snapshot_restored = True
                self.safe_reset(task, preserve=protected)
                # After the reset, never inside the preserve steps: the notice must
                # not claim a reset a later snapshot pause refused. One line per
                # reset, naming every ref it parked and every leg that failed.
                if failed_parks or (restart and (parked is not None or snapshot)):
                    self._notify_reset_preservation(
                        task,
                        restart=restart,
                        parked=parked,
                        snapshot=snapshot,
                        failed_parks=failed_parks,
                    )
                if restore_attempt_snapshot and owned_spec and owned_exclude and not redrive:
                    assert task.dispatched_spec_snapshot is not None
                    self._restore_attempt_owned_spec_bytes_or_pause(
                        task,
                        owned_spec[0],
                        task.dispatched_spec_snapshot,
                        unsafe_context="after the baseline reset",
                        confine_root=self._spec_confine_root(task),
                        root_identity=self._mount_root_identity(task, workspace),
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
                        self._restore_attempt_owned_spec_or_pause(
                            task,
                            owned_spec[0],
                            task.dispatched_spec_snapshot,
                            target_status,
                            confine_root=self._spec_confine_root(task),
                            root_identity=self._mount_root_identity(task, workspace),
                            unsafe_context="after the baseline reset",
                        )
                    else:
                        self._normalize_attempt_owned_spec_or_pause(
                            task,
                            owned_spec[0],
                            target_status,
                            confine_root=self._spec_confine_root(task),
                            root_identity=self._mount_root_identity(task, workspace),
                            unsafe_context="after the baseline reset",
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
                outcome = "completed"
            finally:
                if outcome != "completed" and self._pauses_raised != pauses_before:
                    outcome = "paused"
                # Refresh the plugin's view of the (possibly partially) reset tree (the
                # Unity engine re-imports assets). Observe-only, like pre_rollback.
                if outcome == "completed":
                    self._emit("post_rollback", task, rollback_outcome=outcome)
                else:
                    with contextlib.suppress(Exception):
                        self._emit("post_rollback", task, rollback_outcome=outcome)
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
            self._restore_attempt_owned_spec_bytes_or_pause(
                task,
                owned_spec[0],
                task.dispatched_spec_snapshot,
                unsafe_context="before the ordinary manual-recovery pause",
                confine_root=self._spec_confine_root(task),
                root_identity=self._mount_root_identity(task, workspace),
            )
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

    def retry_preserve_notice(self, task: StoryTask) -> str:
        """:func:`retry_preserve_paragraph` against the active workspace — recovery
        refs are shared by every worktree of the repository — and this run."""
        return retry_preserve_paragraph(self._workspace_get().root, task, self.state.run_id)

    def prune_preserve_refs(self) -> None:
        """Bounded retention for the three recovery-ref families at run start —
        the attempt-preserve/* branches, the refs/attempt-preserve-dirty/*
        rollback worktree snapshots, and the refs/merge-preflight-preserve/*
        snapshots of operator edits the merge pre-flight restored (DW-356): keep
        the newest scm.preserve_keep of each by committer date, delete the tail
        (mirrors the runs/cleanup retention knobs — without it the refs grow
        unbounded on a long-lived project). Per family, so per-merge pre-flight
        refs never crowd rollback evidence out of its budget.
        Best-effort: a git failure is journalled per family and never blocks or
        pauses the run — the refs are a safety net, not run state — and a
        failure in one family never skips the others. preserve_keep = 0 disables
        pruning entirely."""
        keep = self.policy.scm.preserve_keep
        if keep <= 0:
            return
        workspace = self._workspace_get()
        for family, prune in (
            ("attempt-preserve", verify.prune_preserve_refs),
            ("attempt-preserve-dirty", verify.prune_preserve_dirty_refs),
            ("merge-preflight-preserve", verify.prune_merge_preflight_preserve_refs),
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

    def preserve_attempt_commits(
        self,
        task: StoryTask,
        *,
        allow_pause: bool,
        failed_parks: list[_FailedPark] | None = None,
    ) -> tuple[str, int] | None:
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
        not park it" — only the latter can report a HEAD.

        A failure that does not pause journals ``attempt-preserve-fallthrough``
        (``leg``, ``head``) and is appended to ``failed_parks``, so the caller's
        post-reset notice can name the HEAD a reflog rescue needs (DW-481).

        Returns ``(ref, count)`` — the commits branch and how many commits it
        parked — on a successful park, else None (nothing above baseline, or a
        non-pausing preservation failure). ``task.preserve_ref`` may later be
        overwritten by the worktree snapshot; the returned branch still exists."""
        baseline = task.baseline_commit
        if not baseline:
            return None
        workspace = self._workspace_get()
        # Enumerating the range is what decides whether the reset is safe, so a
        # fault here is not a no-op: an un-determinable range must read as "there
        # may be work above baseline" and take the preservation-failure path below,
        # never the `not commits` early return (which would let the reset run
        # blind). These two calls carried no guard at all, so until #343 a plain
        # git *timeout* — which `_run_git` does translate, and which every sibling
        # here already treats as routine — crashed the rollback outright; OSError
        # joins it because the translation stops at timeouts. Pin HEAD before
        # enumerating so the range and the recovery ref describe the same observed
        # tip even if the checkout moves between those operations.
        head = ""
        try:
            head = verify.rev_parse_head(workspace.root)
            commits = verify.commits_above(workspace.root, baseline, head)
            # The bookkeeping filter decides only WHETHER to park (#772): a range of
            # nothing but the engine's ledger commits parks no ref. A mixed range
            # parks the branch at `head`, which still carries those commits, so the
            # journaled and noticed count stays the unfiltered range — the count the
            # notice's `git log baseline..ref` shows the operator.
            if not commits or not self._without_engine_bookkeeping(task, workspace.root, commits):
                return None
        except (verify.GitError, OSError) as exc:
            self.journal.append(
                "attempt-preserve-enumerate-failed", story_key=task.story_key, error=str(exc)
            )
            if allow_pause:
                # Same refusal as an un-parked ref: the notice must not tell the
                # operator to `reset --hard` past work we could not even count.
                self.pause_for_manual_recovery(task, baseline, preserve_failed=True)
            # re-drive: never pause — proceed to the (human-directed) reset. `head`
            # stays "" when rev-parse itself was the fault journaled just above.
            self._record_fallthrough(task, "commits-enumerate", head, failed_parks)
            return None
        # run_id can be an arbitrary user `--run-id`; ref-sanitize it (same
        # identity-for-clean-ids / digest-for-dirty contract as the unit branches) so
        # an exotic/overlong id can't blow the ref-name limit, fail `git branch`, and
        # drop the recovery ref (which on a re-drive would then reset past the work
        # anyway).
        try:
            ref = verify.preserve_commits(
                workspace.root,
                baseline,
                attempt_preserve_ref_name(self.state.run_id, head),
                commits=commits,
                revision=head,
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
            # re-drive: never pause — proceed to the (human-directed) reset
            self._record_fallthrough(task, "commits-park", head, failed_parks)
            return None
        task.preserve_ref = ref
        self.journal.append(
            "attempt-commits-preserved", story_key=task.story_key, ref=ref, count=len(commits)
        )
        return ref, len(commits)

    def _without_engine_bookkeeping(
        self, task: StoryTask, root: Path, commits: list[str]
    ) -> list[str]:
        """``commits`` minus the engine's own ledger bookkeeping commits (#772):
        engine bytes, not attempt work, so a range holding only those parks no
        ``attempt-preserve`` ref, points no retry prompt at one, and cannot turn a
        clean reset into a ``preserve_failed`` pause.

        A commit is dropped only when BOTH the persisted record names its sha (for
        this baseline — never matched by subject, which a session can write) AND
        :func:`verify.bookkeeping_only_commits` re-reads it as touching the ledger
        alone. A filter that cannot answer — the ledger's scope unknown, or the
        diff-tree probe faulting — degrades to the unfiltered range and journals
        ``attempt-preserve-filter-failed``: an over-eager park is recoverable, a
        reset past unparked work is not."""
        record = task.bookkeeping_for_baseline()
        if record is None or self._ledger_rel is None:
            return commits
        recorded = {str(sha) for sha in record["shas"]}
        candidates = [sha for sha in commits if sha in recorded]
        if not candidates:
            return commits
        rel, fault = self._ledger_rel()
        error: Exception | None = fault
        bookkeeping: set[str] = set()
        if fault is None:
            if rel is None:
                # An external ledger was never committed here; a recorded sha that
                # still shows up is not one this filter can vouch for.
                return commits
            try:
                bookkeeping = verify.bookkeeping_only_commits(root, candidates, rel)
            except (verify.GitError, OSError) as exc:
                error = exc
        if error is not None:
            self.journal.append(
                "attempt-preserve-filter-failed",
                story_key=task.story_key,
                error=f"{type(error).__name__}: {error}",
            )
            return commits
        return [sha for sha in commits if sha not in bookkeeping]

    def _record_fallthrough(
        self,
        task: StoryTask,
        leg: str,
        head: str,
        failed_parks: list[_FailedPark] | None,
        *,
        error: str | None = None,
    ) -> None:
        """Journal a best-effort preserve leg that failed and falls through to the
        re-drive reset (``attempt-preserve-fallthrough``, DW-481), and hand it to
        the caller's post-reset notice. ``error`` is set only when reading HEAD
        for this very row failed; the preserve fault itself is already journaled
        by the leg's own ``*-failed`` row."""
        if error is None:
            self.journal.append(
                "attempt-preserve-fallthrough", story_key=task.story_key, leg=leg, head=head
            )
        else:
            self.journal.append(
                "attempt-preserve-fallthrough",
                story_key=task.story_key,
                leg=leg,
                head=head,
                error=error,
            )
        if failed_parks is not None:
            failed_parks.append(_FailedPark(leg, head))

    def _notify_reset_preservation(
        self,
        task: StoryTask,
        *,
        restart: bool,
        parked: tuple[str, int] | None,
        snapshot: str | None,
        failed_parks: list[_FailedPark],
    ) -> None:
        """One ATTENTION line for a completed rollback reset, naming every ref it
        parked and every best-effort preserve leg that failed — so one reset never
        yields two lines for the same ref.

        Sent for a resume restart that parked commits (DW-371), uncommitted
        changes (``preserve_attempt_worktree``'s snapshot, DW-480) or both
        (DW-482); and for any re-drive reset whose preserve leg fell through
        (DW-481), naming the leg and the attempt HEAD a reflog rescue needs.
        Attribution-neutral on purpose: ``commits_above`` is a pure range park, so
        the commits may be the interrupted attempt's or ones a human made while
        the run was down. Best-effort (``gates.notify`` never raises); the
        ``attempt-*`` journal rows are the durable record. Nothing here
        interpolates error text: the message is one line, and ``notify`` shapes
        it through ``gates.notice_line`` (DW-417/419)."""
        root = self._workspace_get().root
        short = (task.baseline_commit or "")[:12]
        key = task.story_key
        lead = "resume" if restart else "rollback"
        sentences = [f"{lead} reset {key} to its baseline {short}"]
        if parked is not None:
            ref, count = parked
            plural, verb = ("commit", "was") if count == 1 else ("commits", "were")
            whose = (
                " (the interrupted attempt's, or commits made while the run was down)"
                if restart
                else ""
            )
            sentences.append(
                f"{count} {plural} above it{whose} {verb} parked on `{ref}` first. Inspect: "
                f'`git -C "{root}" log --oneline {short}..refs/heads/{ref}`'
            )
        if snapshot:
            sentences.append(
                f"its uncommitted changes were parked on `{snapshot}` first, a snapshot "
                "commit on top of the attempt's HEAD. Inspect: "
                f'`git -C "{root}" diff {snapshot}~1 {snapshot}`'
            )
        head = next((f.head for f in failed_parks if f.head), "")
        commit_leg = next((f.leg for f in failed_parks if f.leg != "worktree-snapshot"), "")
        if commit_leg:
            failed = "counted" if commit_leg == "commits-enumerate" else "parked"
            rescue = (
                f"recover them from the attempt HEAD {head} with "
                f'`git -C "{root}" branch <name> {head}` before `git gc` prunes them'
                if head
                else f'the attempt HEAD could not be read; find it in `git -C "{root}" reflog`'
            )
            carried = " (the snapshot above sits on that HEAD and carries them)" if snapshot else ""
            sentences.append(
                f"the commits above the baseline could not be {failed} (leg {commit_leg}), "
                f"and the reset ran anyway{carried}: {rescue}"
            )
        if any(f.leg == "worktree-snapshot" for f in failed_parks):
            where = (
                f"the attempt HEAD was {head}"
                if head
                else f'the attempt HEAD could not be read; see `git -C "{root}" reflog`'
            )
            sentences.append(
                "its uncommitted changes could not be snapshotted (leg worktree-snapshot), "
                f"so uncommitted work was NOT preserved and the reset discarded it; {where}"
            )
        if failed_parks:
            sentences.append("see `attempt-preserve-fallthrough` in the run journal")
        if parked is not None or snapshot:
            restore = []
            if parked is not None:
                restore.append(f'`git -C "{root}" cherry-pick <sha>`')
            if snapshot:
                restore.append(f'`git -C "{root}" restore --source={snapshot} -- <path>`')
            busy = (
                "it is re-running the story in this checkout now"
                if restart
                else "it keeps working in this checkout"
            )
            sentences.append(
                "To restore what you want to keep once this run has stopped or finished "
                f"({busy}): " + " or ".join(restore)
            )
        if restart and parked is not None:
            sentences.append(
                f"Next time, `bmad-loop resume {self.state.run_id} --accept-baseline` keeps "
                "everything at HEAD as the new baseline instead (check the log first)"
            )
        if failed_parks:
            title = f"reset of {key} ran without full preservation"
        else:
            what = " and ".join(
                w
                for w, present in (
                    ("commits", parked is not None),
                    ("uncommitted changes", bool(snapshot)),
                )
                if present
            )
            title = f"{what} parked on resume for {key}"
        gates.notify(self.policy, self.run_dir, title, "; ".join(sentences) + ".")

    def accept_current_baseline(self, task: StoryTask) -> None:
        """Adopt the current checkout as ``task``'s baseline (``bmad-loop resume
        --accept-baseline``, DW-371): the restart arm calls this right before its
        rollback, so the reset targets HEAD instead of rewinding past commits the
        operator made while the run was down.

        Both fields are re-stamped together, from values measured before either is
        assigned (the `_dev_phase` / `runs.rearm_escalation` pattern), and journaled
        ``baseline-accepted``. A git fault fails loud: ``baseline-accept-failed`` is
        journaled and the run pauses with a notice, the baseline unchanged — never
        a fall-through to the reset the operator explicitly asked to avoid."""
        root = self._workspace_get().root
        previous_baseline = task.baseline_commit or ""
        try:
            head = verify.rev_parse_head(root)
            untracked = sorted(verify.untracked_files(root))
        except (verify.GitError, OSError) as exc:
            self.journal.append("baseline-accept-failed", story_key=task.story_key, error=str(exc))
            # The fault text is folded to one segment of its line (DW-417); the
            # row above keeps it raw.
            notice = (
                "**ACTION REQUIRED — could not accept the current baseline**\n"
                f"`--accept-baseline` was requested for story **{task.story_key}**, but "
                f"reading the current HEAD / untracked files of `{root}` failed "
                f"({gates.notice_line(str(exc))}). The baseline was left unchanged and no rollback ran, so "
                "nothing was reset.\n"
                "Fix the git fault, then run "
                f"`bmad-loop resume {self.state.run_id} --accept-baseline` again. A "
                f"plain `bmad-loop resume {self.state.run_id}` (without the flag) "
                "rolls the story back to the old baseline instead."
            )
            gates.notify(
                self.policy,
                self.run_dir,
                f"ACTION REQUIRED: baseline accept failed for {task.story_key}",
                notice,
                multiline=True,
            )
            self._save()
            self._pause(notice, task.story_key)
        task.baseline_commit = head
        task.baseline_untracked = untracked
        self.journal.append(
            "baseline-accepted",
            story_key=task.story_key,
            previous_baseline=previous_baseline,
            baseline=head,
        )

    def preserve_attempt_worktree(
        self,
        task: StoryTask,
        *,
        allow_pause: bool,
        force_include: tuple[str, ...] = (),
        failed_parks: list[_FailedPark] | None = None,
    ) -> str | None:
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
        must still downgrade its claim to the committed half (#338). That
        best-effort fall-through also journals ``attempt-preserve-fallthrough``
        with the attempt HEAD and appends to ``failed_parks``, so the caller's
        post-reset notice says uncommitted work was not preserved (DW-481).

        Returns the snapshot ref this call parked, or ``None`` when it parked
        nothing — a clean tree, no baseline, or a capture failure that did not
        pause. Rollback callers ignore it; the sweep's kept-rival migration reset
        reads the ledger back from it to detect a third writer (DW-435)."""
        baseline = task.baseline_commit
        if not baseline:
            return None
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
                # re-drive: never pause — proceed to the (human-directed) reset, but
                # name the HEAD the discarded work sat on for a reflog rescue.
                try:
                    head = verify.rev_parse_head(workspace.root)
                except (verify.GitError, OSError) as head_exc:
                    self._record_fallthrough(
                        task, "worktree-snapshot", "", failed_parks, error=str(head_exc)
                    )
                else:
                    self._record_fallthrough(task, "worktree-snapshot", head, failed_parks)
                return None
            # Refuse the reset rather than destroy what the snapshot failed to save
            # (#340) — but only when something unparked is actually at stake, so a
            # git fault over a harmless reset can't halt an unattended run.
            if force_include or self._reset_would_destroy(task):
                self.pause_for_manual_recovery(task, baseline, snapshot_failed=True)
            return None
        if parked:
            # Last writer wins over preserve_attempt_commits' branch on purpose:
            # the snapshot is commit-tree'd parented at the attempt's HEAD
            # (verify.snapshot_worktree), so it already contains the commits that
            # branch points at — one ref recovers the whole attempt. That holds on
            # this success path only; the `except` above records the case where the
            # snapshot failed and the commits branch is all that survived.
            task.preserve_ref = parked
            self.journal.append("attempt-worktree-preserved", story_key=task.story_key, ref=parked)
        return parked

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
                f"Then run `bmad-loop resume {self.state.run_id}`. Alternatively, "
                f"`bmad-loop resume {self.state.run_id} --accept-baseline` (no reset "
                "needed) adopts EVERYTHING at HEAD as the new baseline — any commits "
                "the attempt made included — and every current untracked file as "
                "pre-existing (never cleaned); uncommitted tracked changes still "
                "follow the normal rollback. Check "
                f'`git -C "{root}" log --oneline {short}..HEAD` first.'
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
        if task.phase == Phase.DEFERRED:
            notice += f"\n{deferred_reverify_hint(self.state.run_id)}"
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
            multiline=True,
        )
        self._save()
        self._pause(notice, task.story_key)
