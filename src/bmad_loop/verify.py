"""Deterministic post-session verification. Never trust LLM self-reports.

verify_dev / verify_review check artifacts on disk and git state against
what the session's result.json claims; run_verify_commands executes the
policy's test/lint gates with the orchestrator's own subprocess calls.
"""

from __future__ import annotations

import hashlib
import io
import locale
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISLNK, S_ISREG
from typing import Any, Literal, assert_never, overload

import yaml

from . import deferredwork
from .bmadconfig import ProjectPaths
from .frontmatter import FrontmatterWriteError  # noqa: F401 — re-export
from .frontmatter import set_frontmatter_status  # noqa: F401 — re-export
from .frontmatter import (
    _edit_frontmatter_block,
    _split_frontmatter,
    auto_dev_baseline_of,
    operator_actions_of,
    read_frontmatter,
    status_of,
)
from .model import StoryTask, VerifyOutcome, result_mapping
from .platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_write_bytes,
    atomic_write_bytes_confined,
    has_parent_ref,
    names_tree_root,
    names_win32_alias,
    open_dir_confined,
)
from .policy import POLICY_FILE, Policy
from .sprintstatus import STATUS_ORDER, story_status

GIT_TIMEOUT_S = 120
COMMAND_TIMEOUT_S = 30 * 60

# The oldest git bmad-loop SUPPORTS — the version this project tests against, writes
# code against, and will help you with. INCLUSIVE: 2.34 itself clears it. (The
# neighbouring multiplexer constant is not the same shape — psmux's
# `_LAST_UNSUPPORTED` names the newest REFUSED build, exclusive. Do not read one as
# the other.)
#
# This is a SUPPORT floor, deliberately above the capability floor. Nothing in this
# module's git argv needs 2.34; the highest capability in use is `git config
# --worktree`, which arrived in 2.20. The floor exists so the project stops carrying
# accommodations for gits nobody here runs — every version claim below may assume it,
# and code need not degrade to reach older git.
#
# Enforced in two places, both fail-closed: `cli._reject_under_floor_git` aborts a
# run/sweep/resume, and `install._shield_enable_worktree_config` refuses the
# git-add shield's permanent repo-format write. `bmad-loop validate` reports it as a
# `git.version` problem, and `bmad-loop diagnose` records the host's version.
GIT_FLOOR = (2, 34)

# Current bound on a single git subprocess. Module state rather than a per-call
# parameter so the ~40 git helpers need no threading; the engine overrides it
# from `limits.git_timeout_s` at startup, everything else keeps the default.
# Interactive callers with a deadline of their own (a TUI render, install's
# best-effort probe) pass `timeout_s=` through `git_bytes` instead — a per-call
# override, never a rebind of this state.
_git_timeout_s = GIT_TIMEOUT_S


def configure_git_timeout(seconds: int) -> None:
    """Set the per-git-call timeout (`limits.git_timeout_s`). Called once by the
    engine when it binds its policy; standalone verify users keep GIT_TIMEOUT_S."""
    global _git_timeout_s
    _git_timeout_s = seconds


# How git's own diff format names the absent side of a creation/deletion. A
# protocol token git emits verbatim on every platform, Windows included — never
# opened, never joined onto. Only `patch_new_files` reads it.
_DIFF_ABSENT = "/dev/null"  # portability: git diff-format token, not a real path

# result.json `workflow` value for the dev pass. A machine contract: the
# orchestrator forges this value in `devcontract` when synthesizing the dev
# result from the spec the bmad-build-auto session leaves on disk; a mismatch
# means the wrong artifacts, so we reject rather than trust them. (Sweep's
# triage/migrate workflows have their
# own constants in sweep.py; the review skill is verified by on-disk artifacts
# only and is not handed its result.json.)
DEV_WORKFLOW = "auto-dev"

# Repo-relative posix path of the orchestrator config, for git pathspecs.
POLICY_FILE_REL = POLICY_FILE.as_posix()
# The orchestrator's own working dir (.bmad-loop/) — config, ledger, run state,
# engine plugins. Excluded wholesale from merge-collision detection: none of it
# is ever a unit branch's merged content, so a dirty .bmad-loop/ must neither
# block a merge as "stray work" nor be auto-cleaned.
AUTOMATOR_DIR_REL = POLICY_FILE.parent.as_posix()


class GitError(Exception):
    pass


class GitSpawnError(GitError):
    """The git child could not be spawned at all (an OSError out of
    `subprocess.run` — EMFILE, ENOMEM, ENOENT on the git binary). A GitError so
    every existing guard treats it like any other git failure; a distinct type
    so the rare caller can tell "git said no" from "the machine is broken"
    (#343). The underlying errno stays reachable via ``exc.__cause__.errno``."""


class GitTimeoutError(GitError):
    """The git child was spawned but never returned inside the deadline (a
    `subprocess.TimeoutExpired` out of `_run_git`). Same shape and same reason as
    `GitSpawnError`: a GitError so every existing guard is unchanged, a distinct
    type so a caller that is about to spawn ANOTHER git can tell "git ran and
    said no" — a non-zero rc, which the next command will answer promptly — from
    "git does not return", which the next command will pay the full timeout for
    all over again. `cmd_validate` is that caller: three probes in a row against
    one hung binary cost three deadlines, and only the first one told the
    operator anything."""


class _GitCommitIndeterminate(GitError):
    """A prepared ref transaction may have committed but lost its acknowledgement."""


@dataclass(frozen=True)
class _PreparedRefUpdate:
    """One direct-ref CAS executed through ``git update-ref --stdin``."""

    ref: str
    new_oid: str
    old_oid: str
    validate_while_prepared: Callable[[float], None]


class RollbackPreflightError(GitError):
    """Rollback cleanup paths could not be proven safe before mutation."""


class MergePreflightError(GitError):
    """Git refused a merge BEFORE starting it: the working tree was never
    touched, no merge is in progress, and there is nothing to resolve. Covers an
    untracked file the merge would overwrite, a staged change on an incoming
    path, a file/directory shape clash, and an `--ff-only` target that cannot
    fast-forward. A GitError so every existing `except verify.GitError` guard is
    unchanged; a distinct type so a caller can stop telling the operator to
    resolve a content conflict that never happened (#619)."""


class MergeConflictError(GitError):
    """The merge ran and the CONTENT collided: unmerged index stages exist (or
    did, before the leg's own rollback), and resolving them by hand is the
    remedy.

    Measured, not inferred: `_index_unmerged` (`ls-files -u`) is what earns this
    class, under both `--no-ff` and `--squash` — the one probe that answers
    content for both, since a conflicted `--squash` writes three unmerged stages
    while creating no MERGE_HEAD at all. A GitError so every existing
    `except verify.GitError` guard is unchanged; a distinct type so the caller's
    LAST arm no longer has to read "bare GitError" as "conflict" — with the
    conflict typed, whatever arrives untyped is a state nothing measured, and
    the caller can say that instead of prescribing conflict resolution for it
    (#619)."""


class MergeCommitRefusedError(GitError):
    """The merge itself ran and resolved; git would not COMMIT the result.

    Measured causes: a `pre-merge-commit` or `commit-msg` hook exiting non-zero,
    and a `commit.gpgsign` that cannot produce a signature. Neither sibling's
    remedy fits — there is no content conflict to resolve and no target state to
    clear, only a policy or a key the operator's own repo configures — which is
    the whole reason this is a third type rather than either of theirs.

    Two legs reach it, through different commits. `--no-ff` is refused at the
    merge's own commit and leaves MERGE_HEAD, which is what parts it from a
    genuine pre-flight refusal — `merge_branch` reads that BEFORE its abort
    rather than only to decide whether to abort at all. (`_index_unmerged`
    cannot see this state: a merge that resolved cleanly leaves no unmerged
    stages whether or not the commit that would have sealed it was allowed.)
    The squash leg is refused at its OWN plain `git commit`, after
    `merge --squash` already staged the result — hooks and signing run there
    like anywhere else — so no MERGE_HEAD is involved and no classification is
    needed: the merge step succeeded, and the failed call identifies the state
    by itself.

    ``restored`` says whether the rollback that follows actually put the
    checkout back — `merge --abort` on the `--no-ff` leg, `reset --hard HEAD`
    on the squash leg (gated on the leg's pre-merge dirtiness snapshot: a
    checkout that already carried uncommitted work is never reset, #619). The
    squash rollback is deliberately whole-tree — it is undoing a SUCCEEDED
    merge whose staged result spans the entire incoming set — which leaves one
    stated ceiling: an operator edit landing after the pre-merge reading found
    the tree clean sits inside the reset's blast radius (see
    `_reset_hard_head`).
    It is an attribute rather than a type of its own because the operator's
    CAUSE is the same either way — a policy declined the commit — and only the first
    step of their remedy differs: a checkout left mid-merge has to be recovered
    before fixing that policy is worth anything, and a resume attempted before then
    fails again on the merge state rather than on the policy. A caller that ignores
    the flag still gets a true statement of the cause; one that reads it can order
    the two steps (#619).

    ``staged`` names WHERE an unrestored checkout stands, because the two legs
    strand differently and the operator's first step differs with them: False
    means mid-merge (MERGE_HEAD set, `git merge --abort` recovers it), True
    means the squash result is still sitting staged (`reset --hard HEAD` clears
    it — after their own uncommitted work, if that is what blocked the rollback,
    is stashed or committed). Always False when ``restored`` is True: a checkout
    that was put back holds nothing."""

    def __init__(self, message: str, *, restored: bool = True, staged: bool = False) -> None:
        super().__init__(message)
        self.restored = restored
        self.staged = staged


class MergeHalfAppliedError(GitError):
    """Git died PART-WAY through checking the merge out: some incoming files are
    already sitting in the target checkout, and no restore removes them.

    A sibling of `MergePreflightError`, never a subclass, because it falsifies
    that class's central claim — the working tree was never touched. Measured
    cause (git 2.55.0, both `--no-ff` and `--squash`): a **required** clean/smudge
    filter that fails. git materializes the incoming paths in index order, so the
    ones sorting before the filtered path are written to the working tree and the
    ones after it are not; git then rolls the INDEX back and exits, leaving the
    written files behind as UNTRACKED. Nothing in the failure's own shape
    distinguishes it from a genuine pre-flight refusal: no unmerged stages, no
    MERGE_HEAD, and — because `git diff --quiet HEAD --` cannot see untracked
    files — a tree that reads clean against HEAD.

    That residue is why this is a type and not a message tweak. It blocks the
    NEXT merge as an untracked-overwrite pre-flight refusal, so a run told its
    checkout was unchanged fails the same way on every resume, over paths the
    error never named.

    The residue has TWO axes and they are not interchangeable. An incoming path
    the target did not already track lands as an untracked file, which no restore
    reaches. An incoming path it DID track is modified in place, which a
    path-scoped `git checkout HEAD --` over exactly the attributed paths does
    undo. So the tracked axis is repaired and the untracked axis is reported, and
    the class carries one field for each — plus ``rewritten``, naming the tracked
    paths the repair covered (or failed to).

    Attribution, on both axes, is a per-path AND of two proofs: the path changed
    during the merge window (a before/after delta, never an absolute reading) AND
    the merge could have written it (the branch's incoming set). Each proof rules
    out the misattribution the other cannot: the delta keeps the operator's
    pre-existing strays and edits out, the intersection keeps their CONCURRENT
    writes out — an edit landing on a bystander path mid-merge was once swept
    into a repo-wide "the tree is dirty now" reading and destroyed by the
    repo-wide reset riding on it. Two ceilings remain, per path: a path already
    dirty before the merge stays unattributable, and a concurrent write to a
    path INSIDE the incoming set is indistinguishable from git's and is restored
    with it.

    ``paths`` carries the untracked residue — what the operator still has to
    clear. Deliberately not cleaned: `reset --hard` and `merge --abort` both
    leave untracked files alone (measured), and deleting them is precisely the
    destruction #619's before-snapshot exists to prevent — the attribution
    proves git wrote *a* path, not that the bytes there are git's.

    ``rewritten`` carries the tracked residue — the paths whose restore
    ``restored`` reports on, so a failed repair can be finished by hand
    path-scoped (`git checkout HEAD -- <path>`) instead of by the repo-wide
    reset whose blast radius the attribution exists to avoid.

    ``restored`` says whether the TRACKED half was rolled back, and is True when
    there was none to roll back. It matters because it changes the operator's
    FIRST step, exactly as its namesake on `MergeCommitRefusedError` does: a
    checkout still holding incoming content on tracked paths refuses the next
    merge over those paths ("Your local changes would be overwritten"), so a resume
    attempted before restoring it fails on the tree rather than on whatever stopped
    the checkout.

    An empty ``paths`` with ``restored`` True is still this class and not
    `MergePreflightError`. The checkout ends up in the same place, but the CAUSE
    the operator has to act on is a different one — something stopped git mid-write,
    not a target-state clash — and sending them to clear a clash that does not
    exist is the #619 defect this taxonomy exists to prevent."""

    def __init__(
        self,
        message: str,
        *,
        paths: tuple[str, ...] = (),
        restored: bool = True,
        rewritten: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.paths = paths
        self.restored = restored
        self.rewritten = rewritten


class MergeResidueUnreadError(GitError):
    """A post-merge reading the classification rests on failed, so the
    checkout's state is UNVERIFIED — neither sibling's claim survives.

    The terminal arm of the classification, present so a probe failure can never
    impersonate a verdict. Unwrapped, the probe's raise escapes between the
    failed merge and its cleanup — stranding a started `--no-ff` mid-merge with
    MERGE_HEAD set — and lands in the caller's content-conflict arm wearing a
    probe error's text, with a remedy that is fiction here. Degraded SILENTLY
    instead, the empty reading routes to `MergePreflightError`, whose
    load-bearing clause — the working tree was never touched — the dead probe
    can no longer back. Both neighbours state what was measured; this class
    states that the measurement is missing.

    Reachable from the corner where nothing else answered. Over dead RESIDUE
    readings, unmerged stages (conflict) and MERGE_HEAD (commit refused) are read
    independently, so either still claims its own class — only the choice
    between "refused before starting" and "failed part-way through checkout"
    rests on that reading, and with it gone the honest answer is neither. The
    index and merge-state readings can die in the same window (#619): a dead
    index reading surrenders every class resting on "did not collide" —
    commit-refused and half-applied as much as pre-flight, since a state it
    cannot rule a conflict out of must not be dressed as either — and a dead
    merge-state reading additionally skips the abort it gates, with the
    message saying so, because uncertainty never authorizes a repair write.
    The squash replay reading joins from the far side of a merge that
    SUCCEEDED: with `allow_empty_squash`'s staged-result reading dead, neither
    the no-op return nor a result to commit can be claimed, so nothing is
    committed, nothing is reset, and the message names the dead reading.

    No repair rides on it: `reset --hard` stays gated on a PROVEN
    tracked-residue attribution, because uncertainty must not be what authorizes
    rewriting the operator's checkout. Their own `git status` is not degraded —
    the message carries the probe's failure alongside git's own and sends them
    there."""


class IntegrationEvidenceError(GitError):
    """The target ref update for an integration cannot be proven safely."""


class IntegrationRestoreError(GitError):
    """A proven integration result could not be restored completely."""


class IntegrationCleanupChangedError(IntegrationEvidenceError):
    """A cleanup operand changed after capture and before its mutation."""

    def __init__(self, cleaned: Iterable[str]) -> None:
        super().__init__("target collision identity changed immediately before cleanup")
        self.cleaned = tuple(cleaned)


@dataclass(frozen=True)
class IntegrationRefUpdate:
    old_revision: str
    new_revision: str


@dataclass(frozen=True)
class IncomingCollisionPlan:
    """One preflight reading whose mutations can be snapshotted before use."""

    cleaned: tuple[str, ...]
    tolerated: tuple[str, ...]
    untracked: tuple[str, ...]


_INTEGRATION_SNAPSHOT_DIR = "integration-snapshots"
_INTEGRATION_SNAPSHOT_CHUNK = 1024 * 1024


def preflight_integration_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Validate the complete prospective Git pathset before target mutation."""
    return tuple(dict.fromkeys(_portable_integration_path(path) for path in paths))


def _index_state(repo: Path, rel: str) -> dict[str, object]:
    """Return the exact persisted index stages for one repository path.

    ``ls-files --stage`` is the plumbing representation accepted by
    ``update-index --index-info``.  The debug flag carries the otherwise invisible
    intent-to-add bit, which must not be mistaken for an ordinary empty blob.
    """
    validated = _portable_integration_path(rel)
    proc = git_bytes(repo, "ls-files", "--stage", "-z", "--", validated)
    if proc.returncode != 0:
        raise IntegrationEvidenceError("target index evidence is unavailable")
    entries: list[dict[str, object]] = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise IntegrationEvidenceError("target index evidence is malformed") from exc
        if os.fsdecode(raw_path) != validated:
            raise IntegrationEvidenceError("target index evidence changed path identity")
        mode_text, oid_text, stage_text = map(os.fsdecode, (mode, oid, stage))
        if (
            not re.fullmatch(r"[0-7]{6}", mode_text)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid_text)
            or stage_text not in {"0", "1", "2", "3"}
        ):
            raise IntegrationEvidenceError("target index evidence is malformed")
        entries.append({"mode": mode_text, "oid": oid_text, "stage": int(stage_text)})
    debug = git_bytes(repo, "ls-files", "--debug", "-z", "--", validated)
    if debug.returncode != 0:
        raise IntegrationEvidenceError("target index flag evidence is unavailable")
    records = _index_debug_records(debug.stdout)
    if len(records) != len(entries) or any(
        os.fsdecode(raw_path) != validated for raw_path, _word in records
    ):
        raise IntegrationEvidenceError("target index flag evidence is malformed")
    for entry, (_raw_path, word) in zip(entries, records, strict=True):
        entry["flags"] = word
    intent = any(int(word, 16) & 0x20000000 for _raw_path, word in records)
    return {"entries": entries, "intent_to_add": intent}


# One `ls-files --debug` record after its path's NUL: the five fixed lines
# git's `show_ce` prints (`%u` decimals, the flag word `%x`), the next path
# beginning right after the fifth newline.
_INDEX_DEBUG_RECORD = re.compile(
    rb"  ctime: [0-9]+:[0-9]+\n"
    rb"  mtime: [0-9]+:[0-9]+\n"
    rb"  dev: [0-9]+\tino: [0-9]+\n"
    rb"  uid: [0-9]+\tgid: [0-9]+\n"
    rb"  size: [0-9]+\tflags: ([0-9a-fA-F]+)\n"
)
# The bits of a `ls-files --debug` flag word the index FILE holds: name length
# (`0fff`), stage (`3000`), CE_EXTENDED (`4000`), assume-unchanged (CE_VALID,
# `8000`), intent-to-add (`20000000`) and skip-worktree (`40000000`). Bits
# 16–28 are git's in-process bookkeeping, printed raw by `show_ce`; on a
# `core.fsmonitor` target CE_FSMONITOR_VALID (`200000`) reads on every entry
# the monitor calls unchanged and is gone from an entry `update-index` wrote
# or the monitor since reported, so a word taken as identity paused every
# integration on such a target (Codex, #796 review). Every reading masks to
# what the file holds — the captured word, the fresh words, the digest.
_INDEX_FILE_FLAG_MASK = 0x6000FFFF


def _index_debug_records(debug: bytes) -> list[tuple[bytes, str]]:
    """``(path, flag word)`` per record of a ``ls-files --debug -z`` reading.

    ``-z`` NUL-terminates the path alone; the debug lines that follow it are
    newline-terminated and fixed in number, so the reading walks records —
    path to its NUL, then exactly the five lines — rather than scanning the
    whole output for ``flags:``, which read a filename holding a newline
    followed by that text as one flag word more than the index has entries
    and paused every integration on this target as malformed (#796 review).
    The flag word is lowercased and masked to the bits the index file holds
    (`_INDEX_FILE_FLAG_MASK`).
    """
    records: list[tuple[bytes, str]] = []
    position = 0
    while position < len(debug):
        nul = debug.find(b"\0", position)
        if nul < 0:
            raise IntegrationEvidenceError("target index flag evidence is malformed")
        match = _INDEX_DEBUG_RECORD.match(debug, nul + 1)
        if match is None:
            raise IntegrationEvidenceError("target index flag evidence is malformed")
        word = int(match.group(1), 16) & _INDEX_FILE_FLAG_MASK
        records.append((debug[position:nul], f"{word:x}"))
        position = match.end()
    return records


def _validated_index_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"entries", "intent_to_add"}:
        raise IntegrationEvidenceError("persisted target index snapshot is malformed")
    entries = value.get("entries")
    intent = value.get("intent_to_add")
    if not isinstance(entries, list) or not isinstance(intent, bool):
        raise IntegrationEvidenceError("persisted target index snapshot is malformed")
    normalized: list[dict[str, object]] = []
    seen_stages: set[int] = set()
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {"mode", "oid", "stage", "flags"}:
            raise IntegrationEvidenceError("persisted target index snapshot is malformed")
        mode, oid, stage, flags = (
            raw.get("mode"),
            raw.get("oid"),
            raw.get("stage"),
            raw.get("flags"),
        )
        if (
            not isinstance(mode, str)
            or not re.fullmatch(r"[0-7]{6}", mode)
            or not isinstance(oid, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
            or not isinstance(stage, int)
            or isinstance(stage, bool)
            or stage not in {0, 1, 2, 3}
            or stage in seen_stages
            or not isinstance(flags, str)
            or not re.fullmatch(r"[0-9a-f]+", flags)
        ):
            raise IntegrationEvidenceError("persisted target index snapshot is malformed")
        seen_stages.add(stage)
        normalized.append(dict(raw))
    if intent and (len(normalized) != 1 or normalized[0]["stage"] != 0):
        raise IntegrationEvidenceError("persisted target index snapshot is malformed")
    return {"entries": normalized, "intent_to_add": intent}


# Whether receipt paths are held to Win32 name rules: the host's own, like
# `DIR_FD_ANCHORED_WRITES`, and monkeypatched by tests to read the other arm.
WIN32_PATH_NAMES = sys.platform == "win32"


def _portable_integration_path(value: object) -> str:
    """Validate a persisted repository-relative operand before any mutation.

    Containment holds on every host: a string, non-empty, no NUL, not
    absolute, no ``.``/``..``/empty segment, never a ``.git`` component or the
    tree root. The Win32 name rules — reserved characters, control characters,
    device aliases, a drive prefix, a backslash separator — hold on a Windows
    host alone (`WIN32_PATH_NAMES`): on POSIX git permits ``:``, ``?``, ``*``,
    ``\\`` and control characters short of NUL in a name, every reading here
    round-trips them NUL-delimited, and holding them everywhere paused each
    modern bundle that touched such a file as malformed (#796 review).
    """
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or value.startswith("/")
        or names_tree_root(value)
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(part.casefold() == ".git" for part in value.split("/"))
    ):
        raise IntegrationEvidenceError("persisted target integration path is malformed")
    if WIN32_PATH_NAMES and (
        PureWindowsPath(value).drive
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in '<>:"|?*' for char in value)
        or names_win32_alias(value)
    ):
        raise IntegrationEvidenceError("persisted target integration path is malformed")
    return value


def _symlink_ancestor(repo: Path, candidate: Path) -> Path | None:
    """The topmost symlink strictly above ``candidate`` on the way down from ``repo``.

    Read top-down with ``lstat`` semantics, so a component beneath a link is
    never dereferenced to answer: git tracks no path through a symlink, and
    whatever stands at ``candidate`` through one is another path's.
    """
    probe = repo
    for part in candidate.relative_to(repo).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            return probe
        if not probe.exists():
            return None
    return None


def _confined_repo_operand(repo: Path, rel: object) -> tuple[str, Path]:
    validated = _portable_integration_path(rel)
    root = repo.resolve(strict=True)
    candidate = repo / validated
    if has_parent_ref(candidate.relative_to(repo)):
        raise IntegrationEvidenceError("persisted target integration path is malformed")
    # a symlink on the way (a tracked `a -> dir`, or a dangling `a -> missing`,
    # the incoming commit replaces with a directory holding `a/b`) is never
    # followed: git tracks no path through one, so the operand beneath it is
    # absent by topology wherever the link points — a resolving link's
    # destination may well hold the leaf's name — and the link's own parent
    # is what confines it (#796 review). The operand itself, when a link, is
    # confined by its parent the same way.
    link = _symlink_ancestor(repo, candidate)
    probe = link.parent if link is not None else candidate
    if link is None and candidate.is_symlink():
        probe = candidate.parent
    while not probe.exists() and probe != repo:
        probe = probe.parent
    try:
        resolved = probe.resolve(strict=True)
    except OSError as exc:
        raise IntegrationEvidenceError(
            "persisted target integration path has an unavailable parent"
        ) from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise IntegrationEvidenceError("persisted target integration path escaped the repository")
    return validated, candidate


def _integration_snapshot_root(run_dir: Path, operation_identity: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_identity):
        raise IntegrationEvidenceError("persisted target integration operation is malformed")
    run_root = run_dir.resolve(strict=True)
    parent = run_dir / _INTEGRATION_SNAPSHOT_DIR
    if parent.is_symlink():
        raise IntegrationEvidenceError("target integration snapshot directory was redirected")
    if not parent.exists():
        parent.mkdir(mode=0o700)
    parent_resolved = parent.resolve(strict=True)
    if parent.is_symlink() or (
        parent_resolved != run_root and not parent_resolved.is_relative_to(run_root)
    ):
        raise IntegrationEvidenceError("target integration snapshot directory was redirected")
    root = parent / operation_identity
    if root.exists() or root.is_symlink():
        raise IntegrationEvidenceError("target integration snapshot operation already exists")
    root.mkdir(mode=0o700)
    try:
        _fsync_directory(parent)
    except OSError:
        root.rmdir()
        raise
    resolved = root.resolve(strict=True)
    if resolved.parent.resolve(strict=True) != parent_resolved:
        raise IntegrationEvidenceError("target integration snapshot directory was redirected")
    if resolved != run_root and not resolved.is_relative_to(run_root):
        raise IntegrationEvidenceError("target integration snapshot directory escaped the run")
    return root


def _stream_snapshot(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None = None,
    source_root: Path | None = None,
) -> tuple[int, str]:
    """Publish one file sidecar atomically while keeping memory usage bounded."""
    if not DIR_FD_ANCHORED_WRITES:
        fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=destination.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as stream, os.fdopen(fd, "wb") as target:
                fd = -1
                while chunk := stream.read(
                    min(
                        _INTEGRATION_SNAPSHOT_CHUNK,
                        (
                            max_bytes - size + 1
                            if max_bytes is not None
                            else _INTEGRATION_SNAPSHOT_CHUNK
                        ),
                    )
                ):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise IntegrationEvidenceError(
                            "target integration recovery snapshots exceed the aggregate artifact payload limit"
                        )
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return size, digest.hexdigest()
    root_fd = os.open(
        destination.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".capture-{os.getpid():x}-{os.urandom(6).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
    source_fd = -1
    source_parent_fd = -1
    digest = hashlib.sha256()
    size = 0
    try:
        if source_root is not None:
            opened = open_dir_confined(source_root, source.parent)
            if opened is None:
                raise IntegrationEvidenceError(
                    "target integration snapshot source parent was redirected"
                )
            source_parent_fd = opened
            source_fd = os.open(
                source.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_parent_fd,
            )
        else:
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(source_fd, "rb") as stream, os.fdopen(fd, "wb") as target:
            source_fd = -1
            fd = -1
            while chunk := stream.read(
                min(
                    _INTEGRATION_SNAPSHOT_CHUNK,
                    max_bytes - size + 1 if max_bytes is not None else _INTEGRATION_SNAPSHOT_CHUNK,
                )
            ):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise IntegrationEvidenceError(
                        "target integration recovery snapshots exceed the aggregate artifact payload limit"
                    )
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600, dir_fd=root_fd, follow_symlinks=False)
        os.replace(temporary, destination.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        anchored = os.fstat(root_fd)
        named = os.stat(destination.parent, follow_symlinks=False)
        if (anchored.st_dev, anchored.st_ino) != (named.st_dev, named.st_ino):
            raise IntegrationEvidenceError(
                "target integration snapshot directory changed during capture"
            )
        _fsync_directory(destination.parent)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)
    return size, digest.hexdigest()


def _snapshot_bytes(data: bytes, destination: Path) -> tuple[int, str]:
    if not DIR_FD_ANCHORED_WRITES:
        atomic_write_bytes_confined(destination, data, confine_root=destination.parent)
        return len(data), hashlib.sha256(data).hexdigest()
    root_fd = os.open(
        destination.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".capture-{os.getpid():x}-{os.urandom(6).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, "wb") as target:
            fd = -1
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600, dir_fd=root_fd, follow_symlinks=False)
        os.replace(temporary, destination.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        anchored = os.fstat(root_fd)
        named = os.stat(destination.parent, follow_symlinks=False)
        if (anchored.st_dev, anchored.st_ino) != (named.st_dev, named.st_ino):
            raise IntegrationEvidenceError(
                "target integration snapshot directory changed during capture"
            )
        _fsync_directory(destination.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)
    return len(data), hashlib.sha256(data).hexdigest()


def _fsync_directory(directory: Path) -> None:
    """Durably publish a sidecar directory entry where directory fsync exists."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    fd = os.open(directory, os.O_RDONLY | directory_flag)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sidecar_path(run_dir: Path, sidecar: object) -> Path:
    rel = _portable_integration_path(sidecar)
    if not rel.startswith(_INTEGRATION_SNAPSHOT_DIR + "/"):
        raise IntegrationEvidenceError("persisted target snapshot path is malformed")
    candidate = run_dir / rel
    try:
        root = run_dir.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise IntegrationEvidenceError("persisted target snapshot is unavailable") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file() or candidate.is_symlink():
        raise IntegrationEvidenceError("persisted target snapshot was redirected")
    for parent in candidate.parents:
        if parent == run_dir:
            break
        if parent.is_symlink():
            raise IntegrationEvidenceError("persisted target snapshot was redirected")
    return resolved


def _stream_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_INTEGRATION_SNAPSHOT_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _indexed_submodules(repo: Path) -> list[str]:
    proc = git_bytes(repo, "ls-files", "--stage", "-z")
    if proc.returncode != 0:
        raise IntegrationEvidenceError("target submodule index evidence is unavailable")
    paths: list[str] = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, _oid, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise IntegrationEvidenceError("target submodule index evidence is malformed") from exc
        if mode == b"160000" and stage == b"0":
            paths.append(_portable_integration_path(os.fsdecode(raw_path)))
    return paths


# `ls-files --debug` flag words a stage-0 index entry the integration writes
# may carry — a gitlink or a file alike: none; or, on a sparse target only,
# skip-worktree alone (CE_SKIP_WORKTREE | CE_EXTENDED, `40004000`), which git
# sets on every entry it writes outside the sparse cone or patterns and
# which hides nothing from the readings here, a checkout being read from disk
# rather than through the index (#796 review). Off a sparse target that word
# is a hook's `update-index --skip-worktree`; on one, git strips a hook's bit
# from an in-pattern entry to `4000` (probed on git 2.55). Intent-to-add,
# assume-unchanged, or anything else is not a fresh entry's shape: a hook
# set it.
_SPARSE_INDEX_FLAG_WORD = "40004000"
# The words a captured gitlink may carry: those, or either with the
# assume-unchanged bit (CE_VALID, `8000`) an operator set before the run —
# `git update-index --assume-unchanged` on a submodule is released index
# configuration, and it too hides nothing from a reading taken from disk.
# The receipt records the word (`flags`) so a reading compares it exactly:
# an operator's bit is preserved, a hook's flip is drift, and the restore
# puts the word back where `git restore` cleared it (#796 review).
_GITLINK_INDEX_FLAGS = frozenset({"0", _SPARSE_INDEX_FLAG_WORD, "8000", "4000c000"})


def _fresh_index_flag_words(repo: Path) -> frozenset[str]:
    """The flag words an index entry the integration wrote may carry on ``repo``."""
    rc, sparse, _detail = _git_out(repo, "config", "--type=bool", "core.sparseCheckout")
    if rc == 0 and sparse == "true":
        return frozenset({"0", _SPARSE_INDEX_FLAG_WORD})
    return frozenset({"0"})


def _gitlink_index_matches(
    current: dict[str, object], oid: str, *, flags: Collection[object]
) -> bool:
    """Whether ``current`` (an `_index_state` reading) is exactly the gitlink
    ``oid`` carrying one of the ``flags`` words."""
    entries = current.get("entries")
    if current.get("intent_to_add") or not isinstance(entries, list) or len(entries) != 1:
        return False
    entry = entries[0]
    return (
        isinstance(entry, dict)
        and entry.get("mode") == "160000"
        and entry.get("oid") == oid
        and entry.get("stage") == 0
        and entry.get("flags") in flags
    )


def _submodule_checkout_owned(root: Path, checkout: Path) -> bool:
    """Whether a populated checkout is this repository's own submodule checkout.

    While the index carries the gitlink, git names the superproject from the
    checkout and it must be ``root``. A checkout whose gitlink an integration
    deleted has no superproject any more — git leaves the populated directory
    behind (``warning: unable to rmdir``) — and is this repository's by its git
    dir living under ``<git-dir>/modules``, where git keeps a submodule's: the
    git dir git reports for ``root``, which is ``root/.git`` for a main
    checkout and ``<common>/.git/worktrees/<id>`` for a target that is itself
    a linked worktree, whose ``.git`` is a file (#796 review). A checkout
    naming some other superproject, or one carrying its own git dir (a fresh
    ``git init`` at the path), is not. Ceiling: a legacy submodule with its git
    dir embedded in the checkout has no orphan proof and reads as foreign once
    its gitlink is gone.
    """
    rc, superproject, _detail = _git_out(checkout, "rev-parse", "--show-superproject-working-tree")
    if rc != 0:
        return False
    if superproject:
        return Path(superproject).resolve(strict=True) == root
    return _submodule_git_dir_in_modules(root, checkout)


def _submodule_git_dir_in_modules(root: Path, checkout: Path) -> bool:
    """Whether ``checkout``'s git dir lives under ``root``'s ``<git-dir>/modules``.

    The proof `submodule update --init` leaves and a fresh ``git init`` at the
    path does not. While the index carries the gitlink git names ``root`` as
    the superproject of either (#796 review), so a receipt that proved the
    gitlink unpopulated — nothing there to have been anyone's — reads
    ownership by this proof alone: only what git cloned as this repository's
    submodule is the restore's to remove.
    """
    rc, git_dir, _detail = _git_out(checkout, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not git_dir:
        return False
    rc, root_git_dir, _detail = _git_out(root, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not root_git_dir:
        return False
    modules = Path(root_git_dir).resolve(strict=True) / "modules"
    resolved = Path(git_dir).resolve(strict=True)
    return resolved != modules and resolved.is_relative_to(modules)


def _validated_submodule_checkout(
    repo: Path,
    entry: dict[str, object],
    *,
    verify_head: bool,
    revision: str | None = None,
    allow_missing: bool = False,
) -> Path:
    rel, checkout = _confined_repo_operand(repo, entry.get("path"))
    expected = entry.get("head")
    gitlink = entry.get("gitlink")
    if expected is not None and (
        not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected)
    ):
        raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
    if not isinstance(gitlink, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", gitlink):
        raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
    if revision is not None:
        proc = git_bytes(repo, "ls-tree", "-z", revision, "--", rel)
        expected_row = (
            b"160000 commit " + gitlink.encode("ascii") + b"\t" + os.fsencode(rel) + b"\0"
        )
        if proc.returncode != 0 or proc.stdout != expected_row:
            raise IntegrationEvidenceError(
                "persisted target submodule is not anchored to the old revision"
            )
    else:
        # the captured word exactly; a receipt without one reads with the
        # fresh words, as before it was recorded
        captured_word = entry.get("flags")
        if not _gitlink_index_matches(
            _index_state(repo, rel),
            gitlink,
            flags=(
                _fresh_index_flag_words(repo) if captured_word is None else (str(captured_word),)
            ),
        ):
            raise IntegrationEvidenceError(
                "persisted target submodule is no longer the captured indexed gitlink"
            )
    if checkout.is_symlink():
        raise IntegrationEvidenceError(
            "persisted target submodule checkout escaped its indexed location"
        )
    if expected is None:
        # captured unpopulated: an empty directory, or none, is the captured
        # shape; a populated one is attempt-era — owned, it is the restore's
        # to remove (`allow_missing`, the restore's own reading); a plain
        # directory, no `.git` entry, is the commit's in the gitlink's place
        # (a tracked directory replacing it), which `git restore` empties
        # ahead of the restore's reading, so that reading judges what is
        # left; a repository of any other kind is foreign; and in any reading
        # asked to verify, populated is changed receipt-owned state
        if not checkout.is_dir():
            return checkout
        root = repo.resolve(strict=True)
        if checkout.resolve(strict=True) != root.joinpath(*rel.split("/")):
            raise IntegrationEvidenceError(
                "persisted target submodule checkout escaped its indexed location"
            )
        if not any(checkout.iterdir()):
            return checkout
        if verify_head or not allow_missing:
            raise IntegrationEvidenceError("target submodule checkout was not restored")
        if not (checkout / ".git").exists():
            return checkout
        # git names the superproject for any repository at an indexed
        # gitlink, a fresh `git init` included; the clone's git dir under
        # `.git/modules` is what marks it this repository's submodule
        if not _submodule_git_dir_in_modules(root, checkout):
            raise IntegrationEvidenceError("persisted target submodule checkout changed ownership")
        return checkout
    if allow_missing and not checkout.is_dir():
        return checkout
    if not checkout.is_dir():
        raise IntegrationEvidenceError("persisted target submodule checkout is unavailable")
    root = repo.resolve(strict=True)
    resolved = checkout.resolve(strict=True)
    lexical = root.joinpath(*rel.split("/"))
    if resolved != lexical:
        raise IntegrationEvidenceError(
            "persisted target submodule checkout escaped its indexed location"
        )
    if not _submodule_checkout_owned(root, checkout):
        raise IntegrationEvidenceError("persisted target submodule checkout changed ownership")
    if verify_head:
        if rev_parse_head(checkout) != expected:
            raise IntegrationEvidenceError("target submodule checkout was not restored")
        # the checkout's own reading, the one the capture required empty: the
        # superproject's `status` reports a submodule's modified and untracked
        # content only as `submodule.<name>.ignore` allows — `dirty` or `all`,
        # set in a tracked `.gitmodules` to quiet exactly that noise, hides
        # it — so a hook's write into a captured checkout the bundle leaves
        # alone was listed by no superproject reading and the run recorded
        # `unit-merged` over it (#796 review); the restore's own reading
        # (`_restore_submodule_checkouts`) is this one too
        if not _submodule_checkout_clean(checkout):
            raise IntegrationEvidenceError("target submodule checkout is not clean")
    return checkout


def _submodule_checkout_clean(checkout: Path) -> bool:
    """Whether a populated checkout reports nothing under the capture's reading."""
    status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall")
    return status.returncode == 0 and not status.stdout


def _restore_submodule_checkouts(
    repo: Path,
    entries: list[dict[str, object]],
    *,
    old_revision: str,
) -> None:
    for entry in entries:
        rel = str(entry["path"])
        if entry.get("head") is None:
            # captured unpopulated: whatever a hook checked out there is
            # attempt-era whole (the receipt proved the directory empty), and
            # git's own shape is the empty directory (#796 review). Ownership
            # was proved before the first mutation; `.git/modules` keeps the
            # clone, exactly as `submodule deinit` would leave it. A directory
            # of any other kind still standing — the commit's own files are
            # already restored away — is the proved-absent directory's
            # doctrine: nothing the restore can attribute is removed.
            checkout = _validated_submodule_checkout(
                repo, entry, verify_head=False, revision=old_revision, allow_missing=True
            )
            if (
                checkout.is_dir()
                and any(checkout.iterdir())
                and not _submodule_git_dir_in_modules(repo.resolve(strict=True), checkout)
            ):
                raise IntegrationRestoreError(
                    f"target submodule directory contains unowned state: {rel}"
                )
            _empty_submodule_directory(repo, checkout)
            _restore_gitlink_index_flags(repo, rel, entry.get("flags"))
            continue
        rc, detail = _git(repo, "submodule", "update", "--init", "--checkout", "--", rel)
        if rc != 0:
            raise IntegrationRestoreError(
                f"target submodule checkout recreation failed for {rel}: {detail}"
            )
        checkout = _validated_submodule_checkout(
            repo, entry, verify_head=False, revision=old_revision
        )
        rc, detail = _git(checkout, "checkout", "--detach", str(entry["head"]))
        if rc != 0:
            raise IntegrationRestoreError(
                f"target submodule checkout restoration failed for {rel}: {detail}"
            )
        # The receipt captured this checkout clean under exactly this reading,
        # so whatever it reports now — a target hook's write into the checkout
        # (#796 review) — is attempt-era and the receipt's to undo: tracked
        # content and index back to the captured HEAD, untracked files out.
        # Ignored files were never read and are never touched.
        status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall")
        if status.returncode == 0 and status.stdout:
            for reset in (("reset", "-q", "--hard"), ("clean", "-q", "-fd")):
                rc, detail = _git(checkout, *reset)
                if rc != 0:
                    raise IntegrationRestoreError(
                        f"target submodule checkout restoration failed for {rel}: {detail}"
                    )
            status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall")
        if status.returncode != 0 or status.stdout:
            raise IntegrationRestoreError(
                f"target submodule checkout restoration is not clean for {rel}"
            )
        _restore_gitlink_index_flags(repo, rel, entry.get("flags"))


def _restore_gitlink_index_flags(repo: Path, rel: str, flags: object) -> None:
    """Put a captured gitlink's index flag word back: `git restore` clears it.

    ``flags`` None is a receipt written before the word was recorded, with
    nothing to put back.
    """
    if flags is None:
        return
    _apply_index_flags(repo, rel, int(str(flags), 16))


def _empty_submodule_directory(repo: Path, checkout: Path) -> None:
    """Leave exactly an empty directory at a captured-unpopulated gitlink."""
    if DIR_FD_ANCHORED_WRITES:
        parent_fd = _open_restore_parent(repo, checkout.parent)
        try:
            _remove_tree_at(parent_fd, checkout.name)
            os.mkdir(checkout.name, 0o755, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return
    if checkout.is_dir() and not checkout.is_symlink():
        shutil.rmtree(checkout)
    checkout.mkdir(parents=True)


def _capture_integration_state_into(
    repo: Path,
    run_dir: Path,
    root: Path,
    paths: Iterable[str],
    *,
    payload_max_bytes: int | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    snapshots: list[dict[str, object]] = []
    total_bytes = 0
    indexed_submodules = set(_indexed_submodules(repo))
    selected = list(dict.fromkeys(paths))
    incoming = {_portable_integration_path(rel) for rel in selected}
    for rel in selected:
        validated, candidate = _confined_repo_operand(repo, rel)
        tracked = path_tracked(repo, validated)
        # a symlink on the way: git tracks no path through one, so the leaf
        # beneath is absent by topology — a dangling link reaches nothing,
        # and a link that resolves (`a -> dir` with `dir/b` standing where
        # `a/b` reads) reaches another path's entry, which `lstat` through
        # the link would have captured as this one and the snapshot stream
        # refused as redirected (#796 review). The link itself is captured
        # under its own path, since the commit that adds `a/b` replaces it;
        # a link the incoming set does not name cannot be the merge's — git
        # refuses `a/b` against a tracked `a` it keeps — and is refused here
        # ahead of any mutation rather than read through.
        link = _symlink_ancestor(repo, candidate)
        if link is not None:
            if link.relative_to(repo).as_posix() not in incoming:
                raise IntegrationEvidenceError(
                    "target integration path lies beneath a symlink the integration "
                    "does not replace"
                )
            snapshots.append(
                {
                    "path": validated,
                    "state": "absent",
                    "tracked": tracked,
                    "index": _index_state(repo, validated),
                    "absent_parents": [],
                    "empty_parents": [],
                }
            )
            continue
        absent_parents: list[str] = []
        parent = candidate.parent
        while parent != repo and not parent.exists() and not parent.is_symlink():
            absent_parents.append(parent.relative_to(repo).as_posix())
            parent = parent.parent
        # the first existing ancestor, when it is an empty directory, is
        # proved empty: git never tracks one, so nothing else reads it, and
        # everything under it after the hooks is attempt-era exactly as
        # under a proved-absent directory (#796 review). A populated one
        # holds what the receipt never read; a file is the entry-type
        # transition's; an unpopulated gitlink the submodule reading's.
        empty_parents: list[str] = []
        if parent != repo and not parent.is_symlink() and parent.is_dir():
            parent_rel = parent.relative_to(repo).as_posix()
            if parent_rel not in indexed_submodules and not any(parent.iterdir()):
                empty_parents.append(parent_rel)
        try:
            mode = candidate.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            # `NotADirectoryError`: an ancestor is a file — git names both
            # sides of a tracked file/directory transition (`a` deleted,
            # `a/b` added), and the leaf beneath the file is absent (#796
            # review); the file itself is captured under its own path.
            index = _index_state(repo, validated)
            snapshots.append(
                {
                    "path": validated,
                    "state": "absent",
                    "tracked": tracked,
                    "index": index,
                    "absent_parents": absent_parents,
                    "empty_parents": empty_parents,
                }
            )
            continue
        if validated in indexed_submodules and candidate.is_dir():
            # The nested checkout is captured below.  Treating it as an ordinary
            # directory would either follow it or reject every populated submodule.
            continue
        if stat.S_ISDIR(mode):
            # Git reports both sides' leaf paths for a tracked directory/file
            # transition.  The leaves carry the reversible bytes and index state;
            # the directory entry itself has no Git identity to snapshot.
            proc = git_bytes(repo, "ls-files", "-z", "--", validated)
            if proc.returncode == 0 and proc.stdout:
                continue
            # an untracked nested repository — the one entry `status -uall`
            # collapses, which the guard tolerates as `vendor` — holds an
            # operator's repository, not a file of the target's: nothing
            # here to snapshot; its `.git` and every entry of its tree are
            # the ignored listing's (`_nested_git_entries`), each at its
            # identity, so a hook's write there is named (#796 review)
            if not tracked and os.path.lexists(candidate / ".git"):
                continue
            raise IntegrationEvidenceError("target integration snapshot operand is not a file")
        index = _index_state(repo, validated)
        name = hashlib.sha256(os.fsencode(validated)).hexdigest() + ".bin"
        sidecar = root / name
        if S_ISLNK(mode):
            target_bytes = os.readlink(os.fsencode(candidate))
            size, digest = _snapshot_bytes(target_bytes, sidecar)
            total_bytes += size
            if payload_max_bytes is not None and total_bytes > payload_max_bytes:
                raise IntegrationEvidenceError(
                    "target integration recovery snapshots exceed the aggregate "
                    f"artifact payload limit ({payload_max_bytes} bytes)"
                )
            if not candidate.is_symlink() or os.readlink(os.fsencode(candidate)) != target_bytes:
                raise IntegrationEvidenceError("target changed during integration snapshot capture")
            snapshots.append(
                {
                    "path": validated,
                    "state": "symlink",
                    "tracked": tracked,
                    "index": index,
                    "absent_parents": absent_parents,
                    "empty_parents": empty_parents,
                    "sidecar": sidecar.relative_to(run_dir).as_posix(),
                    "size": size,
                    "sha256": digest,
                }
            )
            continue
        if not S_ISREG(mode):
            raise IntegrationEvidenceError("target integration snapshot operand is not a file")
        remaining = None if payload_max_bytes is None else payload_max_bytes - total_bytes
        size, digest = _stream_snapshot(
            candidate,
            sidecar,
            max_bytes=remaining,
            source_root=repo,
        )
        total_bytes += size
        if payload_max_bytes is not None and total_bytes > payload_max_bytes:
            raise IntegrationEvidenceError(
                "target integration recovery snapshots exceed the aggregate "
                f"artifact payload limit ({payload_max_bytes} bytes)"
            )
        _observed_rel, observed_candidate = _confined_repo_operand(repo, validated)
        if _symlink_ancestor(repo, observed_candidate) is not None:
            raise IntegrationEvidenceError("target changed during integration snapshot capture")
        try:
            observed_mode = observed_candidate.lstat().st_mode
        except FileNotFoundError as exc:
            raise IntegrationEvidenceError(
                "target changed during integration snapshot capture"
            ) from exc
        if not S_ISREG(observed_mode) or _stream_digest(observed_candidate) != (size, digest):
            raise IntegrationEvidenceError("target changed during integration snapshot capture")
        snapshots.append(
            {
                "path": validated,
                "state": "regular",
                "tracked": tracked,
                "index": index,
                "absent_parents": absent_parents,
                "empty_parents": empty_parents,
                "sidecar": sidecar.relative_to(run_dir).as_posix(),
                "size": size,
                "sha256": digest,
                "mode": mode & 0o7777,
            }
        )

    submodules: list[dict[str, object]] = []
    for rel in _indexed_submodules(repo):
        _validated, checkout = _confined_repo_operand(repo, rel)
        if not checkout.is_dir():
            continue
        repo_root = repo.resolve(strict=True)
        # an unpopulated gitlink — a clone without `--recurse-submodules` —
        # is an empty directory with no `.git`: git's shape, recorded as such
        # (`head` None) so a checkout a hook makes there is the receipt's to
        # remove; a probe from inside it would find the superproject itself
        # and call the submodule foreign (#796 review)
        if not any(checkout.iterdir()):
            if checkout.resolve(strict=True) != repo_root.joinpath(*rel.split("/")):
                raise IntegrationEvidenceError(
                    "target submodule checkout escaped its indexed location"
                )
            submodules.append(
                {"path": rel, "head": None, **_captured_gitlink_entry(_index_state(repo, rel))}
            )
            continue
        if checkout.resolve(strict=True) != repo_root.joinpath(*rel.split("/")):
            raise IntegrationEvidenceError("target submodule checkout escaped its indexed location")
        rc, superproject, _detail = _git_out(
            checkout, "rev-parse", "--show-superproject-working-tree"
        )
        if rc != 0 or not superproject or Path(superproject).resolve(strict=True) != repo_root:
            raise IntegrationEvidenceError("target submodule checkout changed ownership")
        status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall")
        if status.returncode != 0 or status.stdout:
            raise IntegrationEvidenceError(
                "populated target submodule must be clean before integration"
            )
        # the checkout's own ignored entries, sealed like the tree's
        # (`capture_ignored_entries`): that reading never descends into a
        # submodule, and the post-hook checkout reading takes `status`
        # without `--ignored`, so a hook's write the checkout's own
        # `.gitignore` covers was listed by nothing (#796 review). The
        # run's records live in the target alone: a `.bmad-loop/` the
        # checkout ignores is any other ignored path there
        name = hashlib.sha256(os.fsencode(rel)).hexdigest() + _SUBMODULE_IGNORED_SUFFIX
        submodules.append(
            {
                "path": rel,
                "head": rev_parse_head(checkout),
                **_captured_gitlink_entry(_index_state(repo, rel)),
                "ignored": _seal_ignored_entries(checkout, run_dir, root / name, own_records=False),
            }
        )
    return snapshots, submodules


def _captured_gitlink_entry(index: dict[str, object]) -> dict[str, object]:
    """The ``gitlink`` and ``flags`` a submodule receipt entry records from its index reading."""
    entries = index["entries"]
    if (
        index.get("intent_to_add")
        or not isinstance(entries, list)
        or len(entries) != 1
        or entries[0].get("mode") != "160000"
        or entries[0].get("stage") != 0
        or entries[0].get("flags") not in _GITLINK_INDEX_FLAGS
    ):
        raise IntegrationEvidenceError("target submodule index evidence is malformed")
    return {"gitlink": entries[0]["oid"], "flags": entries[0]["flags"]}


def capture_integration_state(
    repo: Path,
    run_dir: Path,
    operation_identity: str,
    paths: Iterable[str],
    *,
    payload_max_bytes: int | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Capture complete reversible non-ref state into streamed run sidecars."""
    root = _integration_snapshot_root(run_dir, operation_identity)
    try:
        return _capture_integration_state_into(
            repo,
            run_dir,
            root,
            paths,
            payload_max_bytes=payload_max_bytes,
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        try:
            _fsync_directory(root.parent)
        except OSError:
            pass
        raise


def validate_integration_state_schema(
    run_dir: Path,
    snapshots: object,
    submodules: object,
    operation_identity: str | None = None,
    *,
    payload_max_bytes: int | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Validate every persisted non-ref operand before Git/filesystem mutation."""
    if not isinstance(snapshots, list) or not isinstance(submodules, list):
        raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
    validated_snapshots: list[dict[str, object]] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in snapshots:
        if not isinstance(raw, dict):
            raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        rel = _portable_integration_path(raw.get("path"))
        state = raw.get("state")
        tracked = raw.get("tracked")
        index = _validated_index_state(raw.get("index"))
        absent_parents = raw.get("absent_parents")
        if (
            rel in seen
            or state not in {"absent", "regular", "symlink"}
            or not isinstance(tracked, bool)
            or not isinstance(absent_parents, list)
            or any(not isinstance(parent, str) for parent in absent_parents)
        ):
            raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        validated_parents = [_portable_integration_path(parent) for parent in absent_parents]
        if len(set(validated_parents)) != len(validated_parents):
            raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        # git's slash hierarchy, the one the capture wrote (`Path.parent`
        # relative to the repository, `as_posix`): under the Windows reading
        # `a:/file`'s parent is the drive root and `a:` is drive-relative, and
        # a name holding a backslash is several segments, so on POSIX — where
        # both are plain names `_portable_integration_path` admits — the
        # receipt the capture had just written was refused as malformed at
        # the replay that needed it (#796 review). The Win32 name rules are
        # that function's, on a Windows host alone.
        expected_parent = PurePosixPath(rel).parent
        for parent in validated_parents:
            if PurePosixPath(parent) != expected_parent:
                raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
            expected_parent = expected_parent.parent
        # `empty_parents`: the first existing ancestor, proved an empty
        # directory at capture — at most one, the one above the topmost
        # absent parent (or the path); a receipt written before the key
        # proved nothing there (#796 review)
        empty_parents = raw.get("empty_parents", [])
        if (
            not isinstance(empty_parents, list)
            or len(empty_parents) > 1
            or any(not isinstance(parent, str) for parent in empty_parents)
        ):
            raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        validated_empty = [_portable_integration_path(parent) for parent in empty_parents]
        for parent in validated_empty:
            if PurePosixPath(parent) != expected_parent or expected_parent == PurePosixPath():
                raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        seen.add(rel)
        expected_keys = {"path", "state", "tracked", "index", "absent_parents"}
        if "empty_parents" in raw:
            expected_keys.add("empty_parents")
        if state in {"regular", "symlink"}:
            expected_keys |= {"sidecar", "size", "sha256"}
            if state == "regular":
                expected_keys.add("mode")
            size = raw.get("size")
            digest = raw.get("sha256")
            mode = raw.get("mode")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or (
                    state == "regular"
                    and (
                        not isinstance(mode, int)
                        or isinstance(mode, bool)
                        or not 0 <= mode <= 0o7777
                    )
                )
            ):
                raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
            total_bytes += size
            if payload_max_bytes is not None and total_bytes > payload_max_bytes:
                raise IntegrationEvidenceError(
                    "target integration recovery snapshots exceed the aggregate "
                    f"artifact payload limit ({payload_max_bytes} bytes)"
                )
            sidecar = _sidecar_path(run_dir, raw.get("sidecar"))
            if operation_identity is not None:
                expected_sidecar = (
                    Path(_INTEGRATION_SNAPSHOT_DIR)
                    / operation_identity
                    / (hashlib.sha256(os.fsencode(rel)).hexdigest() + ".bin")
                ).as_posix()
                if raw.get("sidecar") != expected_sidecar:
                    raise IntegrationEvidenceError(
                        "persisted target integration snapshot is bound to another operation"
                    )
            if _stream_digest(sidecar) != (size, digest):
                raise IntegrationEvidenceError("persisted target integration snapshot is corrupt")
        if set(raw) != expected_keys:
            raise IntegrationEvidenceError("persisted target integration snapshot is malformed")
        normalized = dict(raw)
        normalized["index"] = index
        normalized["absent_parents"] = validated_parents
        normalized["empty_parents"] = validated_empty
        validated_snapshots.append(normalized)

    validated_submodules: list[dict[str, object]] = []
    seen.clear()
    for raw in submodules:
        # `flags`: the gitlink's captured index flag word; a receipt written
        # before it was recorded reads with the fresh-gitlink words.
        # `ignored`: a populated checkout's sealed ignored-entry listing
        # (`capture_integration_state`); a receipt written before it was
        # recorded reads without that reading
        if not isinstance(raw, dict) or set(raw) - {"flags", "ignored"} != {
            "path",
            "head",
            "gitlink",
        }:
            raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
        rel = _portable_integration_path(raw.get("path"))
        head = raw.get("head")
        gitlink = raw.get("gitlink")
        if "flags" in raw and raw["flags"] not in _GITLINK_INDEX_FLAGS:
            raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
        if "ignored" in raw:
            if head is None:
                raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
            validate_ignored_entries_evidence(raw["ignored"])
        # `head` None: the gitlink was unpopulated at capture — an empty
        # directory, git's shape for a clone without `--recurse-submodules`
        # — and the receipt owns that emptiness (#796 review)
        if (
            rel in seen
            or (head is not None and not isinstance(head, str))
            or (isinstance(head, str) and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head))
            or not isinstance(gitlink, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", gitlink)
        ):
            raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
        seen.add(rel)
        validated_submodules.append(dict(raw))
    return validated_snapshots, validated_submodules


def discard_integration_state(run_dir: Path, attempt: object) -> None:
    """Best-effort removal of sidecars after their receipt is durably retired."""
    if not isinstance(attempt, dict):
        return
    operation = attempt.get("operation_identity")
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
        return
    root = run_dir / _INTEGRATION_SNAPSHOT_DIR / operation
    try:
        run_root = run_dir.resolve(strict=True)
        parent = run_dir / _INTEGRATION_SNAPSHOT_DIR
        if parent.is_symlink():
            return
        parent_resolved = parent.resolve(strict=True)
        resolved = root.resolve(strict=True)
        if (
            root.is_symlink()
            or not parent_resolved.is_relative_to(run_root)
            or not resolved.is_relative_to(run_root)
            or resolved.parent != parent_resolved
        ):
            return
        shutil.rmtree(resolved)
    except (FileNotFoundError, OSError):
        return


def reconcile_integration_state_roots(run_dir: Path, attempts: Iterable[object]) -> None:
    """Best-effort garbage collection of capture roots no live receipt owns."""
    live = {
        str(attempt.get("operation_identity"))
        for attempt in attempts
        if isinstance(attempt, dict)
        and isinstance(attempt.get("operation_identity"), str)
        and re.fullmatch(r"[0-9a-f]{32}", str(attempt.get("operation_identity")))
    }
    parent = run_dir / _INTEGRATION_SNAPSHOT_DIR
    try:
        if parent.is_symlink() or not parent.is_dir():
            return
        for child in parent.iterdir():
            if child.name not in live:
                discard_integration_state(run_dir, {"operation_identity": child.name})
    except OSError:
        return


@overload
def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = ...,
    binary: Literal[False] = ...,
    timeout_s: float | None = ...,
    input_data: None = ...,
    prepared_update: None = ...,
) -> subprocess.CompletedProcess[str]: ...


@overload
def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = ...,
    binary: Literal[True],
    timeout_s: float | None = ...,
    input_data: bytes | None = ...,
    prepared_update: None = ...,
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = ...,
    binary: Literal[False] = ...,
    timeout_s: float | None = ...,
    input_data: None = ...,
    prepared_update: _PreparedRefUpdate,
) -> subprocess.CompletedProcess[str]: ...


def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = None,
    binary: bool = False,
    timeout_s: float | None = None,
    input_data: bytes | None = None,
    prepared_update: _PreparedRefUpdate | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Sole spawn point for git subprocesses. Three failures are raised by
    `subprocess.run` *before* any return code exists — a timeout (#156), a
    spawn-level OSError (#343), and a strict-decode fault on the child's output
    (#377) — so left uncaught any of them would bypass every `except GitError`
    guard and crash the run. All are translated here into the GitError taxonomy
    — observation guards degrade, unguarded paths fail typed — with the two that
    mean the binary never answered marked as `GitSpawnError` and
    `GitTimeoutError` for the callers that must distinguish an environment fault
    from git refusing. The decode fault carries no class of its own: git ran and
    returned, so it is a fact about the repository's bytes, not about the host.

    The decode fault is real, not theoretical: POSIX filenames are arbitrary
    bytes, and while `core.quotePath` C-quotes them to ASCII for ordinary
    porcelain, `-z` disables that quoting (`dirty_paths`, `branch_incoming_paths`,
    `commit_paths`), `worktree list --porcelain` never applied it, and `git diff`
    emits file *content* verbatim (`capture_diff`) — so one latin-1 file is
    enough. Translating is deliberately all this does; making such paths usable
    (`errors="surrogateescape"`) is a separate call, since surrogates would then
    flow into the UTF-8 journal and JSON writes downstream.

    `binary=True` skips the decode entirely and hands back the raw
    `CompletedProcess[bytes]` — see `git_bytes`, the public accessor for it.

    Every git child runs with `LC_ALL=C` so messages stay stable English: the one
    place that inspects git *text* rather than a return code — `safe_rollback`'s
    benign "pathspec did not match" tolerance — must not misread a translated
    message under a localized git (#236). Merged last so it wins over both the
    inherited environment and any explicit `env` (the `_git_env` callers' throwaway
    `GIT_INDEX_FILE` / synthetic identity vars are preserved by the spread).

    `prepared_update` is the one interactive mode.  It owns the complete
    ``update-ref --stdin`` process lifecycle: start, queue, prepare, the caller's
    lock-held validation, commit/abort, bounded pipe reads, and termination.  It
    never returns or embeds the child's output, because ref names and object IDs
    in that protocol are migration authority rather than operator diagnostics.

    `timeout_s` overrides the module bound for this one call — the interactive
    callers' seam (#390): a TUI render or install's best-effort probe keeps its
    own short deadline while standing inside the chokepoint."""
    effective_timeout_s = _git_timeout_s if timeout_s is None else timeout_s
    child_env = {**(env if env is not None else os.environ), "LC_ALL": "C"}

    if prepared_update is not None:
        if binary or input_data is not None:
            raise ValueError("prepared git execution is text-only")
        deadline = time.monotonic() + effective_timeout_s
        proc: subprocess.Popen[str] | None = None
        prepared = False
        commit_attempted = False

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def stop_child() -> bool:
            if proc is None:
                return True
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=min(1.0, remaining()))
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            return proc.poll() is not None

        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    env=child_env,
                )
            except OSError as exc:
                raise GitSpawnError(f"git {cmd[3]} failed to spawn in {repo}") from exc
            # `text=True` wraps every pipe in a `TextIOWrapper(newline=None)`,
            # whose WRITE side translates "\n" to `os.linesep` — "\r\n" on
            # Windows — so `update-ref --stdin` would read `start\r` and die with
            # `unknown command` before acknowledging. The protocol's terminator is
            # LF on every host, so pin it on the command stream only: the reply
            # stream keeps universal-newline reading, which folds either ending
            # into the `"<label>: ok\n"` the `expect` below compares against.
            assert isinstance(proc.stdin, io.TextIOWrapper)
            assert proc.stdout is not None
            assert proc.stderr is not None
            proc.stdin.reconfigure(newline="\n")
            child_stdin = proc.stdin
            child_stdout = proc.stdout
            child_stderr = proc.stderr

            responses: queue.Queue[str | None] = queue.Queue()

            def read_responses() -> None:
                # `stop_child` closes the reply stream on every abort, timeout
                # and failure arm; a reader still iterating it then raises on
                # its next line, and a daemon thread's escape lands on
                # `threading.excepthook` as stderr noise over a failure the
                # caller is already handling. Same classes `discard_stderr`
                # swallows; the sentinel still lands so `expect` never hangs.
                try:
                    for line in child_stdout:
                        responses.put(line)
                except (OSError, ValueError):
                    pass
                finally:
                    responses.put(None)

            def discard_stderr() -> None:
                try:
                    while child_stderr.read(8192):
                        pass
                except (OSError, ValueError):
                    pass

            threading.Thread(target=read_responses, daemon=True).start()
            threading.Thread(target=discard_stderr, daemon=True).start()

            def send(line: str, *, begins_commit: bool = False) -> None:
                nonlocal commit_attempted
                if remaining() <= 0:
                    raise GitTimeoutError(
                        f"git update-ref timed out after {effective_timeout_s}s in {repo}"
                    )
                if begins_commit:
                    # A broken pipe after this point cannot prove whether Git read
                    # the command.  Observation/replay, never rollback, decides.
                    commit_attempted = True
                child_stdin.write(line)
                child_stdin.flush()

            def expect(label: str) -> None:
                try:
                    response = responses.get(timeout=remaining())
                except queue.Empty as exc:
                    raise GitTimeoutError(
                        f"git update-ref timed out after {effective_timeout_s}s in {repo}"
                    ) from exc
                if response != f"{label}: ok\n":
                    raise GitError(f"git prepared ref transaction failed in {repo}")

            send("start\n")
            expect("start")
            send("option no-deref\n")
            send(
                f"update {prepared_update.ref} {prepared_update.new_oid} "
                f"{prepared_update.old_oid}\n"
            )
            send("prepare\n")
            expect("prepare")
            prepared = True
            prepared_update.validate_while_prepared(remaining())
            send("commit\n", begins_commit=True)
            expect("commit")
            child_stdin.close()
            try:
                proc.wait(timeout=remaining())
            except subprocess.TimeoutExpired as exc:
                raise GitTimeoutError(
                    f"git update-ref timed out after {effective_timeout_s}s in {repo}"
                ) from exc
            if proc.returncode != 0:
                raise GitError(f"git prepared ref transaction failed in {repo}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        except BaseException as exc:
            if prepared and not commit_attempted and proc is not None and proc.poll() is None:
                try:
                    send("abort\n")
                    expect("abort")
                    child_stdin.close()
                    proc.wait(timeout=remaining())
                except BaseException as abort_exc:
                    stop_child()
                    if not isinstance(exc, GitError):
                        raise exc
                    raise GitError(
                        f"git prepared ref transaction abort failed in {repo}"
                    ) from abort_exc
            stopped = stop_child()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not stopped:
                if not isinstance(
                    exc, (GitError, BrokenPipeError, OSError, UnicodeError, ValueError)
                ):
                    raise
                raise GitError(
                    f"git prepared ref transaction process did not terminate in {repo}"
                ) from exc
            if commit_attempted:
                raise _GitCommitIndeterminate(
                    f"git prepared ref transaction acknowledgement was lost in {repo}"
                ) from exc
            if isinstance(exc, (BrokenPipeError, OSError, UnicodeError, ValueError)):
                raise GitError(f"git prepared ref transaction failed in {repo}") from exc
            raise
        finally:
            stop_child()

    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=not binary and input_data is None,
            input=input_data,
            timeout=effective_timeout_s,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(
            f"git {cmd[3]} timed out after {effective_timeout_s}s in {repo}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise GitError(f"git {cmd[3]} returned undecodable output in {repo}: {exc}") from exc
    except OSError as exc:
        raise GitSpawnError(f"git {cmd[3]} failed to spawn in {repo}: {exc}") from exc


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = _run_git(["git", "-C", str(repo), *args], repo)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git_raw(repo: Path, *args: str) -> tuple[int, str]:
    """Like `_git` but returns stdout verbatim (no strip, no stderr merge) — for
    NUL-delimited (`-z`) output whose records can begin with a space (porcelain
    status codes like ' M'), which `_git`'s strip() would corrupt."""
    proc = _run_git(["git", "-C", str(repo), *args], repo)
    return proc.returncode, proc.stdout


def _git_raw_out(repo: Path, *args: str) -> tuple[int, str, str]:
    """`_git_raw`'s value with `_git_out`'s diagnostic —
    `(returncode, stdout VERBATIM, (stdout + stderr).strip())`.

    The fourth variant, and it exists for the one shape the other three cannot serve
    together: a caller whose ANSWER is a path whose own trailing whitespace is
    significant, and which still has to raise with stderr when git fails. `_git_out`
    strips the value (silently eating that whitespace) and `_git_raw` drops the
    diagnostic (so the failure message loses stderr).

    stdout is handed back with its line terminator still on. Trimming that is the
    caller's job precisely because only the caller knows how much of the tail is
    framing and how much is data — `.strip()` here would rebuild the very hazard this
    helper exists to avoid."""
    proc = _run_git(["git", "-C", str(repo), *args], repo)
    return proc.returncode, proc.stdout, (proc.stdout + proc.stderr).strip()


def _git_out(repo: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Like `_git`, but hands the VALUE and the DIAGNOSTIC back separately —
    `(returncode, stdout.strip(), (stdout + stderr).strip())`.

    For every caller that reads git's text as the ANSWER rather than only checking
    the return code. `_git`'s merge is right for the "raise with a message" callers,
    where stderr is the informative half, and wrong for these: git writes advisories
    to stderr while still exiting 0 — an unknown `core.fsyncMethod` value, a
    `core.fsmonitor` hook that cannot exec, a stale-index advisory, `core.hooksPath`
    pointing at a missing directory — so against the merged stream a warning is
    indistinguishable from data (#442). A sha probe answers "<sha>\\nwarning: ...", an
    emptiness read answers non-empty, and a line-splitting read grows a phantom record.
    None of that is an error path; it is the normal path on a host whose git config the
    orchestrator does not control.

    The third element keeps the error messages unchanged: a caller raises with the
    merged text exactly as `_git` did, so a failure still carries stderr. Reach for
    this whenever the text is the answer; leave `_git` to the rc-only callers.
    `worktree_clean` and `path_tracked` (#441) predate this helper and spell the same
    split inline against `_run_git`; `_git_raw` is the third variant, for `-z` output
    whose records can begin with a space and which `.strip()` would corrupt, and
    `_git_raw_out` the fourth, for a value whose trailing whitespace is significant but
    whose failure message still needs stderr (`branch_checkout_path`).

    `env` mirrors `_git_env`, for the snapshot path's throwaway `GIT_INDEX_FILE` and
    synthetic-identity calls that also read a sha back."""
    proc = _run_git(["git", "-C", str(repo), *args], repo, env=env)
    return proc.returncode, proc.stdout.strip(), (proc.stdout + proc.stderr).strip()


def _git_env(repo: Path, *args: str, env: dict[str, str]) -> tuple[int, str]:
    """Like `_git` but runs with an explicit environment — used to point git at a
    throwaway `GIT_INDEX_FILE` so a snapshot can stage the tree without touching
    the real index."""
    proc = _run_git(["git", "-C", str(repo), *args], repo, env=env)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def git_bytes(
    repo: Path, *args: str, timeout_s: int | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run one `git -C <repo> …` through the chokepoint, capturing raw BYTES.

    For the callers the `(rc, str)` wrappers above cannot serve, on two counts:

    * **bytes, not a strict decode.** POSIX filenames are arbitrary bytes, so a
      repo path invalid in the locale codec reaches a `text=True` call on an
      ordinary box. `_run_git` now translates that into `GitError` (#377) rather
      than letting it escape untyped — but a caller whose contract is "answer the
      question or skip silently" wants the bytes themselves, not a raised
      taxonomy member. Decode at the point of use with `os.fsdecode`.
    * **the returncode is an answer, not a fault.** The `CompletedProcess` is
      returned whatever the rc, never `check=True`: `git config --get` of an unset
      key exits 1, and that *is* the reply. Callers branch on `returncode`.

    Standing inside the chokepoint is what buys the rest: the `LC_ALL=C` pin so
    git's message text stays stable English (#236), and the `_git_timeout_s` bound
    the engine sets from `limits.git_timeout_s` (#156) — or, for an interactive
    caller whose surface must not appear hung, the shorter per-call `timeout_s`
    (#390). The two faults with no rc to return still raise — a timeout as
    `GitError`, a spawn failure as `GitSpawnError` — since neither can be
    expressed as a `CompletedProcess`."""
    return _run_git(["git", "-C", str(repo), *args], repo, binary=True, timeout_s=timeout_s)


def git_version_at_least(reported: str, want: tuple[int, int]) -> bool:
    """Is this `git version …` line at least `want`? Anything unreadable is NO.

    Only `major.minor` is compared, and only from a line that actually starts
    `git version` — the tail is vendor soup (`2.44.0.windows.1`,
    `2.39.5 (Apple Git-154)`) and searching the whole string for the first two
    dotted numbers would happily read a version out of a build tag.

    Refusing an unparseable answer is the point rather than a fallback: both callers
    use this to gate something they must not do optimistically — abort a run, or make
    a PERMANENT repo-format change — so the failure it must not have is the generous
    one. A git that will not say what it is does not clear the floor.

    The minor must END at a delimiter, which is what keeps that promise against
    trailing garbage: without the lookahead `git version 2.34broken` reads as 2.34
    and CLEARS the floor, exactly the generous failure above. A dot or whitespace
    covers every real form — the vendor tails all continue `.` (`2.44.0.windows.1`)
    or break to a space (`2.39.5 (Apple Git-154)`), and a bare `2.34` ends the
    string. Anything else is refused rather than guessed at, which for an unknown
    vendor spelling is a visible refusal instead of a silent pass.
    """
    match = re.match(r"git version (\d+)\.(\d+)(?=[.\s]|$)", reported.strip())
    return match is not None and (int(match[1]), int(match[2])) >= want


def git_below_floor(
    repo: Path, floor: tuple[int, int] = GIT_FLOOR, *, timeout_s: int | None = None
) -> str | None:
    """What git called itself, when that is below `floor` or unreadable — else None.

    Returning the REPORTED TEXT rather than a bool is what lets every caller name the
    version it refused in its own message; `None` is the only "fine" answer, so
    callers test `is not None` and never truthiness (an empty-but-present answer is a
    refusal, not a pass).

    Split from :func:`git_version_at_least` on purpose. This is the WIRING — probe,
    decode, delegate — and that is the PREDICATE. A test that fakes this one proves a
    call site is reached; a test that drives that one proves the comparison is right.
    Ablating either leaves the other's test green (#464), so they need separate seams
    to be separately provable.

    `git version` is safe against any path: it does no repository setup, so it exits
    0 where `rev-parse` fatals 128 on a malformed `.git/config` — the probe answers
    for the git BINARY, never for the repo. A non-zero rc is therefore already a
    fault, and is reported as an unreadable answer rather than swallowed.

    Raises `GitError` untouched when git could not be run at all — absent or
    unspawnable as `GitSpawnError`, hung as `GitTimeoutError`. That is a different
    fact from "too old" and each caller dispositions it differently, so it is
    deliberately not folded in here.

    `timeout_s` is the #390 per-call seam, forwarded verbatim: the CLI gates keep
    the engine bound, while a caller that must not stall — the TUI guard, on the
    event loop — asks with its own short deadline and treats the resulting
    `GitTimeoutError` as "could not look" rather than as a refusal, since a bound
    the CLI does not share must not decide a launch."""
    probed = git_bytes(repo, "version", timeout_s=timeout_s)
    reported = os.fsdecode(probed.stdout).strip()
    if probed.returncode != 0:
        return reported or f"git exited {probed.returncode}"
    return None if git_version_at_least(reported, floor) else (reported or "no version reported")


def git_floor_text(floor: tuple[int, int] = GIT_FLOOR) -> str:
    """`GIT_FLOOR` as operators read it — `"2.34"`. One formatter so the four
    messages that name the floor cannot drift apart from each other or from the
    constant."""
    return f"{floor[0]}.{floor[1]}"


def under_floor_git_message(found: str) -> str:
    """The one wording for "this git is below `GIT_FLOOR`", rendered by every
    surface that says it: `cli._reject_under_floor_git`'s abort, `validate`'s
    `git.version` finding, `--dry-run`'s "NOT runnable" banner, and the TUI's
    pre-launch guard.

    Shared on purpose — those four dispositions (abort, report, preview, toast) are
    verdicts about ONE host fact, and must not read as different findings about it.
    Lives here rather than in `cli` because that is what lets the TUI render it: the
    TUI is an observer over the core modules and importing the CLI into it would
    invert the layering, so the alternative was a second copy of the sentence, which
    is the drift this function exists to make impossible. `GIT_FLOOR`,
    `git_floor_text` and `git_below_floor` are all here too."""
    # `found` is git's own answer, verbatim — usually a whole `git version 2.25.1`
    # line, but also `git exited 127` or `no version reported` when the probe could
    # not read one. Quoted and introduced rather than dropped mid-sentence, so all
    # three shapes read as English (and so "git git version …" cannot happen).
    return (
        f"git reported {found!r}, which is below the floor bmad-loop supports — "
        f"git {git_floor_text()} or newer is required. Install a newer git "
        "and re-run (`git --version` reports what is on PATH)."
    )


def rev_parse_head(repo: Path) -> str:
    """The sha HEAD resolves to. Reads stdout alone (`_git_out`): git exits 0 while
    still warning on stderr, and a warning-suffixed "sha" flows into every commit
    comparison and into persisted run baselines (#442)."""
    rc, out, detail = _git_out(repo, "rev-parse", "HEAD")
    if rc != 0:
        raise GitError(f"git rev-parse HEAD failed in {repo}: {detail}")
    return out


def rev_parse_revision(repo: Path, revision: str) -> str:
    """Resolve ``revision`` to one pinned commit sha.

    Callers that will mutate refs must not carry a moving branch name across the
    mutation boundary. ``^{commit}`` also refuses non-commit objects instead of
    handing a later worktree/reset operation an object with different semantics.
    """
    rc, out, detail = _git_out(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if rc != 0:
        raise GitError(f"git rev-parse --verify {revision} failed in {repo}: {detail}")
    return out


def ref_revision(repo: Path, refname: str) -> str:
    """Resolve one fully-qualified ref without falling back to another name."""
    rc, out, detail = _git_out(repo, "rev-parse", "--verify", refname)
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", out):
        raise IntegrationEvidenceError(f"target ref evidence is unavailable for {refname}")
    return out


def integration_ref_update(
    repo: Path, refname: str, operation_identity: str
) -> IntegrationRefUpdate | None:
    """Read the unique reflog transition coupled to ``operation_identity``.

    Git's reflog supplies the actual old side of the update, unlike a HEAD
    sample taken before the command.  The next older reflog row is exactly that
    old value.  Missing evidence returns ``None``; malformed or ambiguous
    evidence fails closed without exposing object ids.
    """
    action = f"bmad-loop-integrate:{operation_identity}"
    rc, out, _detail = _git_out(repo, "reflog", "show", "--format=%H%x00%gs", refname)
    if rc != 0:
        raise IntegrationEvidenceError(f"target reflog evidence is unavailable for {refname}")
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        try:
            revision, subject = line.split("\0", 1)
        except ValueError as exc:
            raise IntegrationEvidenceError(
                f"target reflog evidence is malformed for {refname}"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise IntegrationEvidenceError(f"target reflog evidence is malformed for {refname}")
        rows.append((revision, subject))
    matches = [
        index
        for index, (_revision, subject) in enumerate(rows)
        if subject == action or subject.startswith(action + ":")
    ]
    if not matches:
        return None
    if len(matches) != 1 or matches[0] + 1 >= len(rows):
        raise IntegrationEvidenceError(f"target reflog evidence is ambiguous for {refname}")
    index = matches[0]
    return IntegrationRefUpdate(old_revision=rows[index + 1][0], new_revision=rows[index][0])


def require_ref_reflog(repo: Path, refname: str) -> None:
    """Fail before integration when ``refname`` has no readable reflog."""
    rc, _out = _git(repo, "reflog", "exists", refname)
    current = ref_revision(repo, refname)
    show_rc, latest, _detail = _git_out(repo, "reflog", "show", "-1", "--format=%H", refname)
    if rc != 0 or show_rc != 0 or latest != current:
        raise IntegrationEvidenceError(
            f"target reflog is unavailable for {refname}; integration was not attempted"
        )


def _nul_git_paths(proc: subprocess.CompletedProcess[bytes], *, unavailable: str) -> list[str]:
    if proc.returncode != 0:
        raise IntegrationRestoreError(unavailable)
    return [os.fsdecode(path) for path in proc.stdout.split(b"\0") if path]


def _integration_restore_paths(
    repo: Path,
    *,
    old_revision: str,
    new_revision: str,
    extra_paths: Iterable[str] = (),
) -> list[str]:
    """Complete receipt-attributable commit/index path inventory.

    Binary output plus ``os.fsdecode`` preserves arbitrary POSIX filenames for
    the literal pathspec round trip.  The index-vs-integrated-tree delta is
    load-bearing: commit-msg hooks can stage deletions or unrelated paths after
    Git has already written the commit tree.
    """
    changed = _nul_git_paths(
        git_bytes(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            old_revision,
            new_revision,
        ),
        unavailable="target integration paths could not be read; no restoration was attempted",
    )
    index_delta = _nul_git_paths(
        git_bytes(
            repo,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            new_revision,
        ),
        unavailable="target post-hook index paths could not be read; no restoration was attempted",
    )
    indexed_extra = [
        path
        for path in dict.fromkeys(extra_paths)
        if path_tracked(repo, _portable_integration_path(path))
    ]
    return list(dict.fromkeys([*changed, *index_delta, *indexed_extra]))


def _restore_paths_from_stdin(repo: Path, old_revision: str, paths: Iterable[str]) -> None:
    """Restore ``paths`` — index and worktree — to ``old_revision``.

    A path beneath another listed path is dropped: git names both sides of a
    tracked file/directory transition (``a`` deleted, ``a/b`` added), and
    ``restore`` refuses the pair — ``pathspec 'a/b' did not match`` once ``a``
    is a file in the source — while the pathspec ``a`` alone restores the
    whole old shape, in either direction, since a pathspec covers its subtree
    (#796 review). A listed ancestor is always a file, symlink, or gitlink on
    one side (``diff --name-only`` names leaves, never trees), so nothing
    beneath it is a separate restore.
    """
    ordered = list(dict.fromkeys(_portable_integration_path(path) for path in paths))
    listed = set(ordered)
    selected = [
        path
        for path in ordered
        if not any(
            "/".join(path.split("/")[:depth]) in listed for depth in range(1, path.count("/") + 1)
        )
    ]
    if not selected:
        return
    payload = b"".join(os.fsencode(_portable_integration_path(path)) + b"\0" for path in selected)
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "--literal-pathspecs",
            "restore",
            f"--source={old_revision}",
            "--staged",
            "--worktree",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ],
        repo,
        binary=True,
        input_data=payload,
    )
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stdout + proc.stderr).strip()
        raise IntegrationRestoreError(
            "target index/worktree restoration failed; the target ref was not moved: " + detail
        )


def _restore_receipt_index(repo: Path, snapshots: list[dict[str, object]]) -> None:
    """Restore receipt paths' exact pre-attempt index stages and intent state."""
    for entry in snapshots:
        rel = _portable_integration_path(entry["path"])
        rc, detail = _git(repo, "update-index", "--force-remove", "--", rel)
        if rc != 0:
            raise IntegrationRestoreError(
                f"target index entry restoration failed for {rel}: {detail}"
            )
    records = bytearray()
    intent_paths: list[str] = []
    extended_flags: list[tuple[str, int]] = []
    for entry in snapshots:
        rel = _portable_integration_path(entry["path"])
        index = _validated_index_state(entry["index"])
        entries = index["entries"]
        assert isinstance(entries, list)
        if index["intent_to_add"]:
            intent_paths.append(rel)
            if entries:
                extended_flags.append((rel, int(str(entries[0]["flags"]), 16)))
            continue
        for item in entries:
            mode = str(item["mode"])
            oid = str(item["oid"])
            stage = int(item["stage"])
            records.extend(f"{mode} {oid} {stage}\t".encode("ascii"))
            records.extend(os.fsencode(rel))
            records.append(0)
            if stage == 0:
                extended_flags.append((rel, int(str(item["flags"]), 16)))
    if records:
        proc = _run_git(
            ["git", "-C", str(repo), "update-index", "-z", "--index-info"],
            repo,
            binary=True,
            input_data=bytes(records),
        )
        if proc.returncode != 0:
            raise IntegrationRestoreError("target index stage restoration failed")
    for rel in intent_paths:
        rc, detail = _git(repo, "add", "-N", "--", rel)
        if rc != 0:
            raise IntegrationRestoreError(
                f"target intent-to-add restoration failed for {rel}: {detail}"
            )
    for rel, flags in extended_flags:
        _apply_index_flags(repo, rel, flags)


def _apply_index_flags(repo: Path, rel: str, flags: int) -> None:
    """Set or clear the assume-unchanged and skip-worktree bits of ``rel`` to ``flags``."""
    for enabled, option in (
        (bool(flags & 0x8000), "assume-unchanged"),
        (bool(flags & 0x40000000), "skip-worktree"),
    ):
        rc, detail = _git(
            repo,
            "update-index",
            f"--{'' if enabled else 'no-'}{option}",
            "--",
            rel,
        )
        if rc != 0:
            raise IntegrationRestoreError(
                f"target index flag restoration failed for {rel}: {detail}"
            )


def _receipt_index_complete(repo: Path, snapshots: list[dict[str, object]]) -> bool:
    return all(
        _index_state(repo, str(entry["path"])) == _validated_index_state(entry["index"])
        for entry in snapshots
    )


def _copy_sidecar_to_target(
    sidecar: Path,
    parent_fd: int,
    name: str,
    size: int,
    digest: str,
    mode: int,
) -> None:
    temporary = f".restore-{os.getpid():x}-{os.urandom(6).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
    source_fd = os.open(sidecar, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    observed = hashlib.sha256()
    measured = 0
    try:
        with os.fdopen(source_fd, "rb") as source, os.fdopen(fd, "wb") as target:
            source_fd = -1
            fd = -1
            while chunk := source.read(_INTEGRATION_SNAPSHOT_CHUNK):
                target.write(chunk)
                observed.update(chunk)
                measured += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if measured != size or observed.hexdigest() != digest:
            raise IntegrationRestoreError("target integration snapshot changed during restoration")
        os.chmod(temporary, mode, dir_fd=parent_fd, follow_symlinks=False)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _restore_symlink_from_sidecar(
    sidecar: Path, parent_fd: int, name: str, size: int, digest: str
) -> None:
    fd = os.open(sidecar, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        target_bytes = stream.read()
    if len(target_bytes) != size or hashlib.sha256(target_bytes).hexdigest() != digest:
        raise IntegrationRestoreError("target integration snapshot changed during restoration")
    temporary = f".restore-link-{os.getpid():x}-{os.urandom(6).hex()}"
    try:
        os.symlink(os.fsdecode(target_bytes), temporary, dir_fd=parent_fd)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _open_restore_parent(repo: Path, parent: Path) -> int:
    """Open/create one target parent beneath a no-follow repository descriptor."""
    if not DIR_FD_ANCHORED_WRITES:
        parent.mkdir(parents=True, exist_ok=True)
        fd = open_dir_confined(repo, parent)
        if fd is None:
            raise IntegrationRestoreError("target restoration parent is redirected")
        return fd
    try:
        relative = parent.relative_to(repo)
    except ValueError as exc:
        raise IntegrationRestoreError("target restoration parent escaped repository") from exc
    root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    fd = root_fd
    try:
        for part in relative.parts:
            try:
                nested = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                os.fsync(fd)
                nested = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            if fd != root_fd:
                os.close(fd)
            fd = nested
        if fd == root_fd:
            root_fd = -1
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        if root_fd >= 0 and root_fd != fd:
            os.close(root_fd)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one receipt-owned entry without following a link below it."""
    try:
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if S_ISREG(mode) or S_ISLNK(mode):
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return
    if not stat.S_ISDIR(mode):
        raise IntegrationRestoreError("target expected-absent path became unsafe")
    child_fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
    )
    try:
        for child in os.listdir(child_fd):
            _remove_tree_at(child_fd, child)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _absent_beneath_a_file(repo: Path, target: Path, captured_links: Collection[str] = ()) -> bool:
    """Whether an expected-absent ``target`` sits beneath an ancestor that is a file.

    The receipt captures the leaf beneath a tracked file/directory transition
    (``a/b`` while ``a`` is a file) as absent; once the file is back — ``git
    restore`` put it there ahead of the snapshot writes — the leaf is absent
    by topology, and there is no parent directory to open, create, or remove
    (#796 review); so is a proved-absent parent beneath it (``a/b`` for the
    leaf ``a/b/c``). A symlink on the way is the redirection probe's to
    refuse, never a file here — unless the receipt captured that very path
    as a symlink (``captured_links``): then ``git restore`` put it back the
    same way, the transition was symlink-to-directory, and the leaf beneath
    it is absent by topology too (#796 review).
    """
    # top-down, so the TOPMOST link or file decides and no component beneath
    # one is ever read through it: `is_symlink()` on `a/b` with `a -> dir`
    # answers for `dir/b`, another path's entry (#796 review)
    ancestor = repo
    for part in target.relative_to(repo).parts[:-1]:
        ancestor = ancestor / part
        if ancestor.is_symlink():
            return ancestor.relative_to(repo).as_posix() in captured_links
        if not ancestor.exists():
            return False
        if not ancestor.is_dir():
            return True
    return False


def _captured_links(snapshots: Iterable[dict[str, object]]) -> frozenset[str]:
    """Paths the receipt captured as symlinks: their own restored shape."""
    return frozenset(str(entry["path"]) for entry in snapshots if entry["state"] == "symlink")


def _expected_absent_directory_is_owned(repo: Path, target: Path) -> bool:
    """Whether a directory at a receipt-proved-absent path is the attempt's to remove.

    The receipt proved nothing was there, so what stands there now arrived
    during the attempt — but the restore removes only what it can attribute
    to the attempt: an empty directory, or a submodule checkout of this
    repository (its git dir under ``.git/modules``, or naming this repository
    as its superproject), the shape a merge that introduces a gitlink and a
    target hook's ``submodule update --init`` leave. Whatever such a checkout
    holds is attempt-era with it — a hook's write into the new checkout (#796
    review) included — so cleanliness is not required. A directory of any
    other kind may be fresh operator state and is never removed.
    """
    try:
        if not any(target.iterdir()):
            return True
    except OSError:
        return False
    try:
        return _submodule_checkout_owned(repo.resolve(strict=True), target)
    except OSError:
        return False


def _restore_receipt_snapshots_unanchored(
    repo: Path, run_dir: Path, snapshots: list[dict[str, object]]
) -> None:
    """Checked path fallback for hosts without descriptor-relative syscalls."""
    prepared = [(entry, _confined_repo_operand(repo, entry["path"])[1]) for entry in snapshots]
    links = _captured_links(snapshots)
    # the anchored restore's ancestry preflight, by path: a symlink on the
    # way to any destination this restore would write or remove through is
    # refused before the first mutation — the confinement reading never
    # follows a link, so this is the reading that sees one (#796 review)
    for entry, target in prepared:
        if entry["state"] == "absent" and _absent_beneath_a_file(repo, target, links):
            continue
        if _symlink_ancestor(repo, target) is not None:
            raise IntegrationRestoreError("target restoration parent is redirected")
    for entry, target in prepared:
        if entry["state"] == "absent":
            if _absent_beneath_a_file(repo, target, links):
                continue
            if target.is_dir() and not target.is_symlink():
                if not _expected_absent_directory_is_owned(repo, target):
                    raise IntegrationRestoreError(
                        "target expected-absent directory contains unowned state"
                    )
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        sidecar = _sidecar_path(run_dir, entry["sidecar"])
        size = entry["size"]
        digest = entry["sha256"]
        assert isinstance(size, int) and isinstance(digest, str)
        if entry["state"] == "symlink":
            target_bytes = sidecar.read_bytes()
            if len(target_bytes) != size or hashlib.sha256(target_bytes).hexdigest() != digest:
                raise IntegrationRestoreError(
                    "target integration snapshot changed during restoration"
                )
            target.unlink(missing_ok=True)
            target.symlink_to(os.fsdecode(target_bytes))
        else:
            temporary = target.with_name(f".restore-{os.getpid():x}-{os.urandom(6).hex()}")
            measured, observed = _stream_snapshot(sidecar, temporary)
            if (measured, observed) != (size, digest):
                temporary.unlink(missing_ok=True)
                raise IntegrationRestoreError(
                    "target integration snapshot changed during restoration"
                )
            os.replace(temporary, target)
            expected_mode = entry["mode"]
            assert isinstance(expected_mode, int)
            os.chmod(target, expected_mode)
    parent_values: list[str] = []
    for entry in snapshots:
        raw_parents = entry.get("absent_parents")
        assert isinstance(raw_parents, list)
        parent_values.extend(str(parent) for parent in raw_parents)
    for rel in sorted(
        set(parent_values),
        key=lambda value: value.count("/"),
        reverse=True,
    ):
        try:
            (repo / rel).rmdir()
        except (FileNotFoundError, OSError):
            pass


def _restore_receipt_snapshots(
    repo: Path, run_dir: Path, snapshots: list[dict[str, object]]
) -> None:
    if not DIR_FD_ANCHORED_WRITES:
        _restore_receipt_snapshots_unanchored(repo, run_dir, snapshots)
        return
    # Retain all destination directory descriptors before the first leaf write.
    prepared: list[tuple[dict[str, object], Path, int]] = []
    try:
        destinations = [
            (entry, _confined_repo_operand(repo, entry["path"])[1]) for entry in snapshots
        ]
        # Validate every lexical destination and its currently existing ancestry
        # before creating a missing parent for any one destination.
        links = _captured_links(snapshots)
        for entry, target in destinations:
            if entry["state"] == "absent" and _absent_beneath_a_file(repo, target, links):
                continue
            probe = target.parent
            while not probe.exists() and not probe.is_symlink() and probe != repo:
                probe = probe.parent
            if probe.is_symlink():
                raise IntegrationRestoreError("target restoration parent is redirected")
        for entry, target in destinations:
            if entry["state"] == "absent" and _absent_beneath_a_file(repo, target, links):
                continue
            prepared.append((entry, target, _open_restore_parent(repo, target.parent)))
        for entry, target, parent_fd in prepared:
            state = entry["state"]
            if state == "absent":
                try:
                    current_mode = os.stat(
                        target.name, dir_fd=parent_fd, follow_symlinks=False
                    ).st_mode
                except FileNotFoundError:
                    current_mode = 0
                if stat.S_ISDIR(current_mode) and not _expected_absent_directory_is_owned(
                    repo, target
                ):
                    raise IntegrationRestoreError(
                        "target expected-absent directory contains unowned state"
                    )
                _remove_tree_at(parent_fd, target.name)
                continue
            sidecar = _sidecar_path(run_dir, entry["sidecar"])
            size = entry["size"]
            digest = entry["sha256"]
            assert isinstance(size, int) and isinstance(digest, str)
            try:
                existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                existing = 0
            if existing and not S_ISREG(existing) and not S_ISLNK(existing):
                raise IntegrationRestoreError(
                    "target snapshot path became non-file; it was preserved"
                )
            if state == "symlink":
                _restore_symlink_from_sidecar(sidecar, parent_fd, target.name, size, digest)
                continue
            expected_mode = entry["mode"]
            assert isinstance(expected_mode, int)
            _copy_sidecar_to_target(sidecar, parent_fd, target.name, size, digest, expected_mode)
        # Remove parent directories proven absent at capture, deepest first.
        parent_values: list[str] = []
        for entry in snapshots:
            raw_parents = entry.get("absent_parents")
            assert isinstance(raw_parents, list)
            parent_values.extend(str(parent) for parent in raw_parents)
        absent_parents = sorted(
            set(parent_values),
            key=lambda value: value.count("/"),
            reverse=True,
        )
        for rel in absent_parents:
            path = repo / rel
            if _absent_beneath_a_file(repo, path, links):
                continue  # absent by topology: the file above it is back
            parent_fd = _open_restore_parent(repo, path.parent)
            try:
                try:
                    os.rmdir(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except (FileNotFoundError, OSError):
                    # Non-empty means it contains state the receipt does not own.
                    pass
            finally:
                os.close(parent_fd)
    finally:
        for _entry, _target, parent_fd in prepared:
            os.close(parent_fd)


def _receipt_snapshots_complete(
    repo: Path, run_dir: Path, snapshots: list[dict[str, object]]
) -> bool:
    links = _captured_links(snapshots)
    for entry in snapshots:
        rel, target = _confined_repo_operand(repo, entry["path"])
        if _index_state(repo, rel) != _validated_index_state(entry["index"]):
            return False
        # a symlink on the way is read under its own entry, never through:
        # beneath a captured one the absent leaf is absent by topology (so
        # is one beneath a file), and `exists()` or a digest read through a
        # link that resolves would answer for another path's entry (#796
        # review); beneath any other link nothing is restored
        beneath = _absent_beneath_a_file(repo, target, links)
        if entry["state"] == "absent" and beneath:
            continue
        if _symlink_ancestor(repo, target) is not None:
            return False
        if entry["state"] == "absent":
            if target.exists() or target.is_symlink():
                return False
            raw_parents = entry.get("absent_parents")
            assert isinstance(raw_parents, list)
            for parent in raw_parents:
                candidate = repo / str(parent)
                if candidate.exists() or candidate.is_symlink():
                    return False
            # a proved-empty parent is restored when it is empty again, or
            # gone — git removes a directory its restore emptied; residue
            # there is attempt-era the restore could not attribute
            raw_empty = entry.get("empty_parents")
            assert isinstance(raw_empty, list)
            for parent in raw_empty:
                candidate = repo / str(parent)
                if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
                    return False
                if candidate.is_dir() and any(candidate.iterdir()):
                    return False
            continue
        if entry["state"] == "symlink":
            if not target.is_symlink():
                return False
            sidecar = _sidecar_path(run_dir, entry["sidecar"])
            expected = sidecar.read_bytes()
            if os.readlink(os.fsencode(target)) != expected:
                return False
            continue
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            return False
        if not S_ISREG(mode):
            return False
        expected_mode = entry["mode"]
        assert isinstance(expected_mode, int)
        if mode & 0o7777 != expected_mode:
            return False
        sidecar = _sidecar_path(run_dir, entry["sidecar"])
        size = entry["size"]
        digest = entry["sha256"]
        assert isinstance(size, int) and isinstance(digest, str)
        expected = (size, digest)
        if _stream_digest(sidecar) != expected or _stream_digest(target) != expected:
            return False
    return True


def integration_nonref_state_unchanged(
    repo: Path,
    run_dir: Path,
    snapshots: object,
    submodules: object,
    *,
    exclude_paths: Iterable[str] = (),
    operation_identity: str | None = None,
) -> bool:
    """Recheck snapshotted state immediately before Git may update the ref."""
    validated_snapshots, validated_submodules = validate_integration_state_schema(
        run_dir, snapshots, submodules, operation_identity
    )
    excluded = set(exclude_paths)
    retained = [entry for entry in validated_snapshots if entry["path"] not in excluded]
    if not _receipt_snapshots_complete(repo, run_dir, retained):
        return False
    for entry in validated_submodules:
        if entry["path"] in excluded:
            continue
        try:
            _validated_submodule_checkout(repo, entry, verify_head=True)
        except IntegrationEvidenceError:
            return False
    return True


def _revision_inventory(repo: Path, revision: str) -> dict[str, tuple[bytes, bytes, str]]:
    """Every blob and gitlink ``revision`` seals, keyed by path: ``(mode, kind, oid)``.

    One whole-tree ``ls-tree -r`` — the shape that keeps a wide incoming set off
    argv — read by both post-integration readings that need to know what the
    integrated commit holds at a path. Trees are not rows; a caller asking
    about a directory asks through its prefix.
    """
    proc = git_bytes(repo, "ls-tree", "-r", "-z", revision)
    if proc.returncode != 0:
        raise IntegrationEvidenceError("the integrated commit's path inventory could not be read")
    inventory: dict[str, tuple[bytes, bytes, str]] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as exc:
            raise IntegrationEvidenceError(
                "the integrated commit's path inventory is malformed"
            ) from exc
        inventory[os.fsdecode(raw_path)] = (mode, kind, os.fsdecode(oid))
    return inventory


def _inventory_held_paths(inventory: dict[str, tuple[bytes, bytes, str]]) -> set[str]:
    """Every path ``inventory`` holds, directly or as a tree above a held row.

    ``ls-tree -r`` names blobs and gitlinks, never the trees above them, so a
    path the commit turned into a directory (``a`` deleted, ``a/b`` added; a
    submodule replaced by a tracked directory) is held through the prefix.
    """
    held: set[str] = set()
    for path in inventory:
        parts = path.split("/")
        held.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return held


def integrated_paths_drift(
    repo: Path,
    revision: str,
    paths: Iterable[str],
    *,
    retained_checkouts: Iterable[str] = (),
) -> tuple[str, ...]:
    """Incoming ``paths`` whose post-hook index or worktree differ from ``revision``.

    The receipt's post-integration check excludes the incoming set from its
    "unchanged since the snapshot" reading — the merge changed those paths by
    design — so nothing there sees a TARGET hook rewrite or stage one of them
    after git resolved the merge (#796 review). The integrated commit is the
    authority for exactly those paths: after every leg the index and the
    checkout must hold each one as ``revision`` has it. Two whole-tree
    ``diff --name-only`` readings (worktree and ``--cached``), the same shape
    as the restore's own inventory, intersected here with the incoming set —
    ``git diff`` takes no stdin pathspec, and a whole-tree read is what keeps
    a wide incoming set off argv. Path-only evidence; an unreadable probe
    raises rather than answering.

    Those readings cover only what the index tracks, so they cannot see an
    incoming path the integrated commit DELETES and a hook then recreates
    without staging: ``status`` shows ``?? path``, both diffs stay empty
    (#796 review). For a deleted incoming path the commit's authority is
    "absent from the checkout", so that leg is a filesystem probe — any
    entry at the path, plain, symlink, or gitignored, is drift. The
    commit's own inventory (``ls-tree -r``, whole-tree for the same argv
    reason) says which incoming paths it deleted — and since that listing
    names blobs and gitlinks, never the trees above them, a path the commit
    turned into a directory (``a`` deleted, ``a/b`` added) counts as held
    through the prefix, not deleted. One deleted shape is git's own and not
    a hook's: the populated checkout a merge leaves behind when it deletes
    a submodule (``warning: unable to rmdir``). `validate_integrated_submodule_state`
    adjudicates those against the receipt and names the ones it accepted in
    ``retained_checkouts``; the probe leaves exactly those to it.
    """
    selected = {_portable_integration_path(path) for path in paths}
    if not selected:
        return ()
    retained = {_portable_integration_path(path) for path in retained_checkouts}
    drift: list[str] = []
    for cached in ((), ("--cached",)):
        proc = git_bytes(repo, "diff", *cached, "--name-only", "--no-renames", "-z", revision)
        if proc.returncode != 0:
            raise IntegrationEvidenceError(
                "target post-hook state on the incoming paths could not be read"
            )
        drift.extend(
            path
            for path in (os.fsdecode(raw) for raw in proc.stdout.split(b"\0") if raw)
            if path in selected
        )
    inventory = _revision_inventory(repo, revision)
    held = _inventory_held_paths(inventory)
    for path in sorted(selected - held - retained):
        _validated, candidate = _confined_repo_operand(repo, path)
        # beneath a symlink the commit holds (`a/b` deleted, `a -> dir`
        # added, `dir/b` standing where `a/b` reads) the leaf is absent by
        # topology: git tracks no path through a link, and `exists()` through
        # this one reads another path's entry (#796 review). The link is an
        # incoming path the diff readings above hold to the commit; a link
        # the commit does not hold is a hook's, and the leaf is drift.
        link = _symlink_ancestor(repo, candidate)
        if link is not None:
            row = inventory.get(link.relative_to(repo).as_posix())
            if row is not None and row[0] == b"120000":
                continue
            drift.append(path)
            continue
        if candidate.exists() or candidate.is_symlink():
            drift.append(path)
    return tuple(dict.fromkeys(drift))


def integrated_index_flags_drift(
    repo: Path,
    run_dir: Path,
    snapshots: object,
    paths: Iterable[str],
    *,
    revision: str,
    operation_identity: str | None = None,
) -> tuple[str, ...]:
    """Incoming ``paths`` whose post-hook index entry carries a flag word a hook
    set, or one git trusts over a file that is not what ``revision`` holds.

    ``update-index --assume-unchanged`` or ``--skip-worktree`` on an incoming
    path changes no blob: both diff readings stay empty, ``status`` shows
    nothing, and the integration retired its receipt over an index that hides
    later edits from git (#796 review). One whole-tree ``ls-files --stage`` +
    ``--debug`` reading (the receipt's own index reading, whole-tree for the
    same argv reason as the diffs) is filtered to the incoming set, and each
    stage-0 file entry there may carry only what a fresh entry may on this
    target (`_fresh_index_flag_words`) or the word the receipt captured for
    the path — whether an operator's assume-unchanged bit survives the leg is
    git's (a fast-forward writes the entry anew on git 2.55), and the receipt
    does not refuse a target for keeping released index configuration.

    An accepted word is not the end of the reading. A word carrying a bit git
    trusts over the file (`_UNREAD_INDEX_FLAG_BITS`) is exactly the shape a
    hook can hide bytes behind: overwrite the incoming file, then put the
    captured bit back — the word matches the receipt, the index entry matches
    the commit, and every git reading of the checkout (`integrated_paths_drift`
    among them) trusts the bit and reads the path clean (#796 review; probed
    on git 2.55, where the bit also hides a missing file, a retargeted link,
    and a flipped exec bit). So each such entry is read from disk here, with
    no git reading in between: what stands at the path must be what the
    integrated commit holds — the same entry type, the same blob id under
    the path's own attributes, the exec bit where ``core.fileMode`` honors
    it — or, under skip-worktree alone, nothing, the checkout a sparse target
    leaves out of the cone. Gitlinks are the submodule reading's, unmerged
    stages the diff readings'. Path-only evidence, sorted; an unreadable
    probe raises rather than answering.
    """
    selected = {_portable_integration_path(path) for path in paths}
    if not selected:
        return ()
    validated, _submodules = validate_integration_state_schema(
        run_dir, snapshots, [], operation_identity
    )
    captured: dict[str, str] = {}
    for entry in validated:
        index = entry["index"]
        assert isinstance(index, dict)
        entries = index["entries"]
        assert isinstance(entries, list)
        for item in entries:
            if item["stage"] == 0:
                captured[str(entry["path"])] = str(item["flags"])
    fresh = _fresh_index_flag_words(repo)
    drift: list[str] = []
    inventory: dict[str, tuple[bytes, bytes, str]] | None = None
    for path, word in _index_file_flag_words(repo):
        if path not in selected:
            continue
        accepted = fresh if path not in captured else fresh | {captured[path]}
        if word not in accepted:
            drift.append(path)
            continue
        if not int(word, 16) & _UNREAD_INDEX_FLAG_BITS:
            continue
        if inventory is None:
            inventory = _revision_inventory(repo, revision)
        if not _unread_entry_holds_revision(repo, path, word, inventory.get(path)):
            drift.append(path)
    return tuple(sorted(drift))


def _unread_entry_holds_revision(
    repo: Path, path: str, word: str, row: tuple[bytes, bytes, str] | None
) -> bool:
    """Whether the checkout at ``path`` — an index entry git trusts unread — is
    what the integrated commit's ``row`` (`_revision_inventory`) holds.

    Read from disk, never through git's index: ``lstat`` for the entry's
    type, the exec bit and presence; the blob id from the file's bytes under
    ``path``'s own attributes (`_blob_oid_for_file`, the clean-filter-aware
    identity every content guard here uses) or from a link's target, which
    git stages unfiltered. A missing entry is the sparse checkout's shape
    under skip-worktree and drift under assume-unchanged alone. A row the
    commit does not hold is the cached diff's to refuse; here it is drift.
    """
    if row is None or row[1] != b"blob":
        return False
    mode, _kind, oid = row
    _validated, candidate = _confined_repo_operand(repo, path)
    try:
        entry = os.lstat(os.fsencode(candidate))
    except FileNotFoundError:
        return bool(int(word, 16) & 0x40000000)
    except OSError as exc:
        raise IntegrationEvidenceError(
            "target post-hook content on an incoming path git trusts unread could not be read"
        ) from exc
    try:
        if stat.S_ISLNK(entry.st_mode):
            if mode != b"120000":
                return False
            with tempfile.TemporaryDirectory() as tmp:
                shadow = Path(tmp) / "target"
                shadow.write_bytes(os.fsencode(os.readlink(candidate)))
                proc = git_bytes(
                    repo, "hash-object", "-t", "blob", "--no-filters", "--", str(shadow)
                )
            if proc.returncode != 0:
                raise IntegrationEvidenceError(
                    "target post-hook content on an incoming path git trusts unread "
                    "could not be read"
                )
            return proc.stdout.decode("ascii", "strict").strip() == oid
        if not stat.S_ISREG(entry.st_mode) or mode not in {b"100644", b"100755"}:
            return False
        if _honors_file_mode(repo) and bool(entry.st_mode & stat.S_IXUSR) != (mode == b"100755"):
            return False
        return _blob_oid_for_file(repo, path, candidate) == oid
    except (OSError, GitError) as exc:
        raise IntegrationEvidenceError(
            "target post-hook content on an incoming path git trusts unread could not be read"
        ) from exc


def _honors_file_mode(repo: Path) -> bool:
    """Whether git on ``repo`` reads the exec bit (``core.fileMode``; git's
    default is true, and ``git init`` writes false where the filesystem
    cannot carry one)."""
    rc, value, _detail = _git_out(repo, "config", "--type=bool", "core.fileMode")
    return rc != 0 or value != "false"


def _index_file_flag_words(repo: Path) -> list[tuple[str, str]]:
    """``(path, flag word)`` of every stage-0 file entry in ``repo``'s index.

    One whole-tree ``ls-files --stage`` + ``--debug`` pair — the receipt's own
    index reading (`_index_state`), whole-tree for the same argv reason as the
    diff readings. Gitlinks are the submodule reading's, unmerged stages the
    diff readings'; neither is listed.
    """
    staged = git_bytes(repo, "ls-files", "--stage", "-z")
    debug = git_bytes(repo, "ls-files", "--debug", "-z")
    if staged.returncode != 0 or debug.returncode != 0:
        raise IntegrationEvidenceError("target index flag evidence is unavailable")
    debug_records = _index_debug_records(debug.stdout)
    records = [record for record in staged.stdout.split(b"\0") if record]
    if len(debug_records) != len(records):
        raise IntegrationEvidenceError("target index flag evidence is malformed")
    words: list[tuple[str, str]] = []
    for record, (debug_path, word) in zip(records, debug_records, strict=True):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, _oid, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise IntegrationEvidenceError("target index flag evidence is malformed") from exc
        # the two readings are the same index in the same order, or neither is read
        if raw_path != debug_path:
            raise IntegrationEvidenceError("target index flag evidence is malformed")
        if stage != b"0" or mode == b"160000":
            continue
        words.append((os.fsdecode(raw_path), word))
    return words


def _index_flags_digest(words: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for path, word in sorted(words):
        digest.update(os.fsencode(path))
        digest.update(b"\0")
        digest.update(word.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


# The flag bits under which git trusts the index over the worktree —
# assume-unchanged (CE_VALID) and skip-worktree: neither `status` nor `diff`
# stats such an entry's file, so what the file holds is read by no git
# reading at all.
_UNREAD_INDEX_FLAG_BITS = 0x8000 | 0x40000000


def capture_index_flags(repo: Path, *, exclude: Iterable[str]) -> dict[str, object]:
    """The receipt's evidence for the flag words of the index OUTSIDE the snapshot set.

    A target hook's ``update-index --assume-unchanged`` on a clean tracked file
    outside the incoming set changes no blob and leaves ``status`` empty, so no
    other reading sees it (#796 review). ``digest`` is over ``(path, word)`` of
    every stage-0 file entry not in ``exclude`` (the snapshot paths: those are
    the incoming reading's), which proves the rest of the index unchanged
    after the hooks without persisting it; ``marked`` maps the entries among
    them carrying a word no fresh entry may (neither none nor skip-worktree),
    so a flip can be NAMED — a typical index holds none, and a sparse target's
    out-of-cone entries, all skip-worktree, stay out of it. And ``unread``
    maps every entry among them git trusts over its file — assume-unchanged
    or skip-worktree already set when the receipt is armed — to the file's
    ``lstat`` identity (`_lstat_identity`; ``None`` for one not on disk, a
    sparse target's out-of-cone entries among them): a hook overwriting such
    a file changes no word and no blob, and ``status`` and ``diff`` both trust
    the flag and read it clean, so the digest and the map held and the
    integration recorded ``unit-merged`` over the hook's bytes (#796 review).
    The identity is what names it, as it names an ignored entry's overwrite.
    """
    excluded = {_portable_integration_path(path) for path in exclude}
    words = [(path, word) for path, word in _index_file_flag_words(repo) if path not in excluded]
    return {
        "digest": _index_flags_digest(words),
        "marked": {
            path: word for path, word in words if word not in {"0", _SPARSE_INDEX_FLAG_WORD}
        },
        "unread": {
            path: _lstat_identity(repo, path)
            for path, word in words
            if int(word, 16) & _UNREAD_INDEX_FLAG_BITS
        },
    }


_IGNORED_ENTRIES_SIDECAR = "ignored.lst"
_SUBMODULE_IGNORED_SUFFIX = ".ignored"


def _owned_integration_snapshot_root(run_dir: Path, operation_identity: str) -> Path:
    """The existing capture root of ``operation_identity``, confined to the run."""
    if not re.fullmatch(r"[0-9a-f]{32}", operation_identity):
        raise IntegrationEvidenceError("persisted target integration operation is malformed")
    run_root = run_dir.resolve(strict=True)
    parent = run_dir / _INTEGRATION_SNAPSHOT_DIR
    root = parent / operation_identity
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise IntegrationEvidenceError("target integration snapshot root is missing") from exc
    if (
        parent.is_symlink()
        or root.is_symlink()
        or not resolved.is_relative_to(run_root)
        or resolved.parent != parent.resolve(strict=True)
    ):
        raise IntegrationEvidenceError("target integration snapshot directory was redirected")
    return root


def ignored_entries(repo: Path, *, own_records: bool = True) -> dict[str, str]:
    """Every entry of the whole tree git lists nowhere, with its ``lstat`` identity.

    Two readings. One whole-tree ``ls-files --others --ignored
    --exclude-standard`` (no ``--directory``: a file inside an ignored
    directory is an entry of its own, so a hook's write there is named too).
    Submodule checkouts are their own reading's; nested repositories list as
    one entry. And the walk `_nested_git_entries` makes for what that listing
    cannot hold: a ``.git`` entry — directory, gitfile or symlink — under any
    directory but the top, which git names in no ``status`` or ``ls-files``
    reading at all, ignored or not, so a hook's ``git init`` in a populated
    tracked directory, or a repository it puts in an ignored one, is listed
    by nothing else (#796 review) — and every entry of a nested repository
    git tracks nothing beneath: the untracked one the collision guard
    tolerates as ``vendor``, the ignored one ``ls-files`` collapses to
    ``node_modules/pkg/``, which no git reading descends into and no reading
    of its own captures, so a hook's write over a file there was listed by
    nothing (#796 review). The automator directory's run records —
    this receipt's own sidecars and the run's worktrees among them — are
    left out exactly as `automator_dirty_paths` leaves them, when
    ``own_records``: that is the target's reading. A captured submodule
    checkout holds no record of the run, so a ``.bmad-loop/cache/x`` its
    own rules ignore is any other ignored path there, and a target hook's
    write to it is read like one (#796 review). The identity is
    the entry's ``lstat`` — size, mtime, ctime, inode, device, mode — never
    its bytes: a hook overwriting or truncating an ignored file that was
    already there leaves the path set unchanged and ``status`` and ``diff``
    silent, and the identity is what names it (#796 review). An entry gone
    between the listing and its ``lstat`` is a removal, not read.
    """
    proc = git_bytes(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    if proc.returncode != 0:
        raise IntegrationEvidenceError("target ignored-entry evidence is unavailable")
    entries: dict[str, str] = {}
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        path = os.fsdecode(raw)
        if own_records and _automator_record_path(path):
            continue
        identity = _lstat_identity(repo, path)
        if identity is not None:
            entries[path] = identity
    entries.update(_nested_git_entries(repo, own_records=own_records))
    return dict(sorted(entries.items()))


def _automator_record_path(path: str) -> bool:
    """Whether ``path`` is one of the run's own records under the automator directory."""
    prefix = f"{AUTOMATOR_DIR_REL}/"
    if not path.startswith(prefix):
        return False
    below = path.removeprefix(prefix)
    return below.startswith(_AUTOMATOR_RECORD_PREFIXES) or below in _AUTOMATOR_RECORD_FILES


def _nested_git_entries(repo: Path, *, own_records: bool = True) -> dict[str, str]:
    """Every ``.git`` entry below the top of ``repo``'s tree, with its ``lstat``
    identity — and every entry of a nested repository git tracks nothing under.

    Walked on disk, symlinks never followed, because git lists none of them:
    ``.git`` is administrative, not a path, and ``status --ignored`` and
    ``ls-files --others --ignored`` alike say nothing about ``dir/.git/config``
    under a populated tracked ``dir`` — nor about a new ``dir/x`` holding
    nothing but a ``.git`` (#796 review). Each ``.git`` is one entry, never
    walked into. What stands beside it is read by where git stands. A
    boundary git tracks something at or beneath — a populated submodule
    checkout at its gitlink, a hook's ``git init`` over a tracked directory
    — is that reading's (the checkout's own capture; the diff readings), and
    the walk stops there. A boundary git tracks nothing under — an untracked
    nested repository the collision guard tolerates as ``vendor``, an ignored
    one ``ls-files`` collapses to ``node_modules/pkg/`` — is an operator's
    repository no reading captures and no git listing descends into, so
    every entry of its tree is listed here at its identity, exactly as an
    ignored file is (a symlink as itself, never through; a repository nested
    deeper on the same terms), and a hook's write over ``vendor/tool.py`` is
    named after the hooks like a write over any ignored file (#796 review).
    The leftover git could not remove when the commit deleted a captured
    checkout's gitlink tracks nothing after the merge and is walked; it is
    the captured checkout's reading's, and the caller leaves it out by
    prefix (`integrated_ignored_additions`). The run's records under the
    automator directory, its worktrees among them, are left out when
    ``own_records`` (`ignored_entries`).
    """
    entries: dict[str, str] = {}
    # roots of the nested repositories the walk descended into: beneath one,
    # every entry is listed, not only a `.git`
    within: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        base = Path(dirpath).relative_to(repo).as_posix()
        if base == ".":
            if ".git" in dirnames:
                dirnames.remove(".git")
            continue
        if own_records and _automator_record_path(f"{base}/"):
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            identity = _lstat_identity(repo, f"{base}/.git")
            if identity is not None:
                entries[f"{base}/.git"] = identity
            if _tracked_beneath(repo, base):
                dirnames[:] = []
                continue
            if ".git" in dirnames:
                dirnames.remove(".git")
            filenames = [name for name in filenames if name != ".git"]
            within.append(base)
        if not any(base == root or base.startswith(f"{root}/") for root in within):
            continue
        # os.walk lists a symlink to a directory among the directories and,
        # unfollowed, never enters it: an entry of its own here, like a file
        links = [name for name in dirnames if (Path(dirpath) / name).is_symlink()]
        for name in (*filenames, *links):
            identity = _lstat_identity(repo, f"{base}/{name}")
            if identity is not None:
                entries[f"{base}/{name}"] = identity
    return entries


def _tracked_beneath(repo: Path, rel: str) -> bool:
    """Whether git tracks anything at or beneath repo-relative posix ``rel`` —
    a gitlink standing at it included."""
    proc = git_bytes(repo, "ls-files", "-z", "--", *_literal_specs([rel]))
    if proc.returncode != 0:
        raise IntegrationEvidenceError("target tracked-path evidence is unavailable")
    return bool(proc.stdout)


# The shape `_lstat_identity` writes: six integers, colon-joined.
_LSTAT_IDENTITY = re.compile(r"-?[0-9]+(:-?[0-9]+){5}")


def _lstat_identity(repo: Path, path: str) -> str | None:
    """``lstat`` identity of a tree entry — size, mtime, ctime, inode, device,
    mode — or ``None`` for one that is gone, an ancestor of it that is no
    longer a directory included."""
    try:
        entry = os.lstat(os.fsencode(repo / path))
    except (FileNotFoundError, NotADirectoryError):
        return None
    return ":".join(
        str(value)
        for value in (
            entry.st_size,
            entry.st_mtime_ns,
            entry.st_ctime_ns,
            entry.st_ino,
            entry.st_dev,
            entry.st_mode,
        )
    )


def capture_ignored_entries(
    repo: Path, run_dir: Path, operation_identity: str
) -> dict[str, object]:
    """The receipt's evidence for the ignored entries of the whole tree.

    A target hook's gitignored write beside an incoming path in a directory
    the target already held populated is listed by no other reading: the diff
    readings cover tracked paths, the stray reading takes ``status`` without
    ``--ignored``, and the introduced-directory walk roots only where the
    receipt proved nothing, an empty directory, or a non-directory stood
    (#796 review). The whole tree's ignored entries with their identities
    (`ignored_entries`) are sealed into a NUL-delimited sidecar of
    ``path, identity`` pairs under the operation's capture root — the listing
    can be wide, and the receipt in ``state.json`` records only its location,
    size and digest — so that after the hooks every ignored entry not on it,
    or on it under another identity, can be NAMED
    (`integrated_ignored_additions`).
    """
    root = _owned_integration_snapshot_root(run_dir, operation_identity)
    return _seal_ignored_entries(repo, run_dir, root / _IGNORED_ENTRIES_SIDECAR)


def _seal_ignored_entries(
    repo: Path, run_dir: Path, sidecar: Path, *, own_records: bool = True
) -> dict[str, object]:
    """Seal `ignored_entries` of ``repo`` into ``sidecar``; the receipt's record of it."""
    data = b"\0".join(
        os.fsencode(path) + b"\0" + identity.encode("ascii")
        for path, identity in ignored_entries(repo, own_records=own_records).items()
    )
    size, digest = _snapshot_bytes(data, sidecar)
    return {
        "sidecar": sidecar.relative_to(run_dir).as_posix(),
        "size": size,
        "sha256": digest,
    }


def validate_ignored_entries_evidence(value: object) -> dict[str, object]:
    """Validate a persisted `capture_ignored_entries` record; the same dict back."""
    if not isinstance(value, dict) or set(value) != {"sidecar", "size", "sha256"}:
        raise IntegrationEvidenceError("persisted target ignored-entry evidence is malformed")
    sidecar, size, digest = value["sidecar"], value["size"], value["sha256"]
    if (
        not isinstance(sidecar, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise IntegrationEvidenceError("persisted target ignored-entry evidence is malformed")
    _portable_integration_path(sidecar)
    return value


def _recorded_ignored_entries(run_dir: Path, evidence: dict[str, object]) -> dict[str, str]:
    sidecar = _sidecar_path(run_dir, evidence["sidecar"])
    data = sidecar.read_bytes()
    if len(data) != evidence["size"] or hashlib.sha256(data).hexdigest() != evidence["sha256"]:
        raise IntegrationEvidenceError("persisted target ignored-entry evidence changed")
    fields = data.split(b"\0") if data else []
    if len(fields) % 2:
        raise IntegrationEvidenceError("persisted target ignored-entry evidence is malformed")
    recorded: dict[str, str] = {}
    for raw_path, raw_identity in zip(fields[::2], fields[1::2], strict=True):
        identity = raw_identity.decode("ascii", errors="strict")
        if not raw_path or not re.fullmatch(r"-?\d+(:-?\d+){5}", identity):
            raise IntegrationEvidenceError("persisted target ignored-entry evidence is malformed")
        recorded[os.fsdecode(raw_path)] = identity
    return recorded


def integrated_ignored_additions(
    repo: Path,
    run_dir: Path,
    evidence: object,
    *,
    tolerated: Iterable[str] = (),
    incoming: Iterable[str] = (),
    introduced_checkouts: Iterable[str] = (),
    retained_checkouts: Iterable[str] = (),
    own_records: bool = True,
    removals: bool = True,
) -> tuple[str, ...]:
    """Ignored entries after the hooks the receipt did not record, recorded
    otherwise, or recorded and gone.

    The listing `capture_ignored_entries` sealed, read back against its
    digest, against the same reading now: an entry it does not hold arrived
    during the attempt, one it holds under another identity was written
    during it, and one it holds that is no longer on disk was removed during
    it — a target hook's write, wherever it stands, an ignored file it
    overwrote or truncated in place included, a nested ``.git`` it made,
    which git lists nowhere, and an ignored file that was already there
    which it deleted, which ``status`` and ``diff`` are as silent about as
    they are about its overwrite (#796 review). A ``tolerated`` stray an
    incoming ``.gitignore`` change turned ignored, which the pre-merge guard
    already read, is left out; so is every ``incoming`` path — the commit's
    own, the diff readings' and the absent-path probe's — and, from the
    removal reading alone, every recorded entry beneath an incoming path —
    an ignored ``d/x`` where the commit put the file ``d``, which git
    clobbers without a word, ignored entries being its to overwrite (the
    reverse, the commit's ``p/y`` where an ignored file ``p`` stood, leaves
    the commit's directory at ``p``, present, and needs no rule); so is the
    ``.git`` of each ``introduced_checkouts`` gitlink path
    (`integrated_introduced_gitlinks`), a checkout the submodule reading
    accepted where the receipt recorded none; and so is everything under
    each ``retained_checkouts`` leftover — a captured checkout git could not
    remove when the commit deleted its gitlink, which the walk descends into
    now that git tracks nothing there and which is the captured checkout's
    own reading's (`integrated_submodule_ignored_additions`), as
    `integrated_stray_paths` leaves it. A file inside a tolerated nested
    repository (``vendor/tool.py`` under the tolerated ``vendor``) is NOT
    left out: the tolerance covers the repository's presence, which the
    guard read, not its contents, which no reading captured — the listing
    holds each at its identity, so a hook's write over one, or its removal,
    is named like that of any ignored file (#796 review). A recorded entry
    that left the ignored listing but still stands on disk is not a removal:
    an incoming ``.gitignore`` change uncovered it and the stray reading
    holds it at its recorded identity, or a hook staged it and the stray
    reading names it. The restore leaves what this names as it found it,
    like unstaged and untracked dirt: the receipt never held ignored bytes
    and cannot put a removed entry back. Without ``removals`` the recorded
    side is not read: the refused receipt's residue reading
    (`refused_integration_residue`) guards a re-arm over a hook's OUTPUT,
    and an absence has none to seal — while the operator's one way to clear
    a named rewrite, whose identity nothing can put back, is to delete the
    file, and a removal the pause named is theirs to weigh before they
    resume. Path-only evidence, sorted.
    ``own_records`` as `ignored_entries` takes it, and as the listing was
    sealed: the target's reading leaves the run's own records out; a
    captured checkout's (`integrated_submodule_ignored_additions`) holds
    none and reads a ``.bmad-loop/`` there like any ignored path. Ceiling:
    the identity is ``lstat``'s, so a writer that puts size, times and inode
    back is not read.
    """
    validated = validate_ignored_entries_evidence(evidence)
    recorded = _recorded_ignored_entries(run_dir, validated)
    incoming_set = {_portable_integration_path(path) for path in incoming}
    excluded = {_portable_integration_path(path) for path in tolerated} | incoming_set
    excluded.update(f"{_portable_integration_path(path)}/.git" for path in introduced_checkouts)
    prefixes = tuple(
        f"{_portable_integration_path(path)}/" for path in dict.fromkeys(retained_checkouts)
    )

    def left_out(path: str) -> bool:
        # `vendor/`: a tolerated nested repository (`plan_incoming_collisions`,
        # tolerated as `vendor`) an incoming `.gitignore` change turned ignored
        return (
            path in excluded
            or path.rstrip("/") in excluded
            or any(path.startswith(prefix) for prefix in prefixes)
        )

    current = ignored_entries(repo, own_records=own_records)
    named = {
        path
        for path, identity in current.items()
        if not left_out(path) and recorded.get(path) != identity
    }
    for path in recorded if removals else ():
        if path in current or left_out(path):
            continue
        # beneath an incoming path: git's own clobber, the file the commit
        # put at `d` leaving no `d/x`; the commit's directory where an ignored
        # file stood is present, and needs no rule
        parts = path.rstrip("/").split("/")
        if any("/".join(parts[:depth]) in incoming_set for depth in range(1, len(parts))):
            continue
        if _lstat_identity(repo, path.rstrip("/")) is None:
            named.add(path)
    return tuple(sorted(named))


def integrated_submodule_ignored_additions(
    repo: Path, run_dir: Path, submodules: object, *, revision: str, removals: bool = True
) -> tuple[str, ...]:
    """Ignored entries in a captured checkout the receipt did not record,
    recorded otherwise, or recorded and gone.

    `integrated_ignored_additions` for each populated submodule the receipt
    captured, against the listing `capture_integration_state` sealed beside
    its HEAD (``ignored``; an older receipt's entry has none and reads as it
    did): the tree's listing never descends into a submodule, and the
    checkout readings take ``status`` without ``--ignored`` for a captured
    checkout, so a target hook writing a file the checkout's own
    ``.gitignore`` covers — into a submodule the incoming commit rewrites
    or leaves alone, or into the leftover git could not remove — was listed
    by nothing (#796 review). Read wherever the captured checkout still
    stands at its lexical location as a repository; a path in its place
    that is not one is the other readings' — the commit's own directory,
    or a file. Inside the leftover a tracked directory replaced, the paths
    ``revision`` holds under it are the commit's, not a hook's, however the
    leftover's rules read them. The run's records are the target's: a
    ``.bmad-loop/cache/x`` the checkout's own rules ignore is read like any
    other ignored path there, as it was sealed (#796 review). Named under
    the submodule path, sorted; left as found by the restore, like the
    tree's own ignored dirt. ``removals`` as `integrated_ignored_additions`
    takes it.
    """
    _snapshots, validated = validate_integration_state_schema(run_dir, [], submodules)
    inventory: dict[str, tuple[bytes, bytes, str]] | None = None
    root = repo.resolve(strict=True)
    named: list[str] = []
    for entry in validated:
        evidence = entry.get("ignored")
        if evidence is None:
            continue
        rel, checkout = _confined_repo_operand(repo, entry["path"])
        if checkout.is_symlink() or not checkout.is_dir() or not (checkout / ".git").exists():
            continue
        if checkout.resolve(strict=True) != root.joinpath(*rel.split("/")):
            raise IntegrationEvidenceError("integrated target submodule checkout was redirected")
        if inventory is None:
            inventory = _revision_inventory(repo, revision)
        prefix = f"{rel}/"
        held_below = [path.removeprefix(prefix) for path in inventory if path.startswith(prefix)]
        named.extend(
            f"{rel}/{path}"
            for path in integrated_ignored_additions(
                checkout,
                run_dir,
                evidence,
                incoming=held_below,
                own_records=False,
                removals=removals,
            )
        )
    return tuple(sorted(named))


def integrated_introduced_gitlinks(
    repo: Path, run_dir: Path, submodules: object, *, revision: str
) -> tuple[str, ...]:
    """Every gitlink ``revision`` holds that the receipt did not capture populated.

    The tree's ignored-entry reading lists every nested ``.git``, and a
    checkout a hook made at a gitlink the commit introduced — or at one the
    receipt captured unpopulated — stands where the receipt recorded none;
    `validate_integrated_submodule_state` is its reading (owned, clean with
    ``--ignored``, at the gitlink) and accepts it, so its ``.git`` is
    tolerated there (`integrated_ignored_additions`), not named twice.
    Sorted.
    """
    _snapshots, validated = validate_integration_state_schema(run_dir, [], submodules)
    populated = {str(entry["path"]) for entry in validated if entry.get("head") is not None}
    return tuple(
        sorted(
            path
            for path, (mode, _kind, _oid) in _revision_inventory(repo, revision).items()
            if mode == b"160000" and path not in populated
        )
    )


def validate_index_flags_evidence(value: object) -> dict[str, object]:
    """Validate a persisted `capture_index_flags` record; the same dict back.

    ``unread`` is optional: a receipt armed before it was recorded reads
    without that reading.
    """
    if not isinstance(value, dict) or not {"digest", "marked"} <= set(value) <= {
        "digest",
        "marked",
        "unread",
    }:
        raise IntegrationEvidenceError("persisted target index flag evidence is malformed")
    digest = value["digest"]
    marked = value["marked"]
    unread = value.get("unread", {})
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(marked, dict)
        or any(
            not isinstance(word, str) or not re.fullmatch(r"[0-9a-f]{1,8}", word)
            for word in marked.values()
        )
        or not isinstance(unread, dict)
        or any(
            identity is not None
            and (not isinstance(identity, str) or not re.fullmatch(_LSTAT_IDENTITY, identity))
            for identity in unread.values()
        )
    ):
        raise IntegrationEvidenceError("persisted target index flag evidence is malformed")
    for path in (*marked, *unread):
        _portable_integration_path(path)
    return value


def integrated_index_flags_outside_drift(
    repo: Path, evidence: object, *, exclude: Iterable[str], require_named: bool = True
) -> tuple[str, ...]:
    """Paths outside the snapshot set whose index flag word a hook changed —
    or whose file, trusted unread by the index, it wrote.

    The digest of `capture_index_flags`, recomputed over the same set, proves
    the rest of the index unchanged; on a mismatch each entry is read against
    its captured word (``marked``) or, unmarked, against what a fresh entry
    may carry on this target. Read after the stray reading, which owns an
    entry a hook added or removed, so what is left to a mismatch is a word
    flip — and a flip no entry can be named for (skip-worktree toggled on a
    sparse target, where both words are fresh) is reported as such rather
    than passed, unless ``require_named`` is off: the re-arm reading
    (`refused_integration_residue`) takes the index as the operator left it
    and asks only what it can name. Whatever the digest reads, every entry
    the receipt recorded as trusted unread (``unread``: assume-unchanged or
    skip-worktree when armed) is read at its file's ``lstat`` identity against
    the captured one — the one reading of a file git itself never stats
    (#796 review). Path-only evidence, sorted.
    """
    validated = validate_index_flags_evidence(evidence)
    marked = validated["marked"]
    assert isinstance(marked, dict)
    unread = validated.get("unread", {})
    assert isinstance(unread, dict)
    excluded = {_portable_integration_path(path) for path in exclude}
    # the file behind an entry git trusts unread: overwritten, truncated,
    # removed or put on disk, its word and blob unchanged and status silent
    rewritten = {
        path
        for path, identity in unread.items()
        if path not in excluded and _lstat_identity(repo, path) != identity
    }
    words = [(path, word) for path, word in _index_file_flag_words(repo) if path not in excluded]
    if _index_flags_digest(words) == validated["digest"]:
        return tuple(sorted(rewritten))
    fresh = _fresh_index_flag_words(repo)
    drift = sorted(
        path for path, word in words if word != marked.get(path, word if word in fresh else None)
    )
    if not drift and not rewritten and require_named:
        raise IntegrationEvidenceError(
            "target hook changed index flag words outside the incoming set; no path named"
        )
    return tuple(sorted({*drift, *rewritten}))


def refused_integration_residue(
    repo: Path, run_dir: Path, attempt: dict[str, Any], *, revision: str
) -> tuple[str, ...]:
    """What a refused attempt's outside-set readings still name, before a re-arm.

    A refusal over a target hook's write outside the incoming set restores
    the receipt-owned paths and leaves the write where it is, named for the
    operator (`integrated_stray_paths`, `integrated_index_flags_outside_drift`,
    `integrated_ignored_additions`, `integrated_submodule_ignored_additions`).
    A resume that found the restore complete then re-armed over the target
    as it stood: the ignored write became the new listing's, the untracked
    one a tolerated stray, the flipped word a marked entry — and the retry
    recorded ``unit-merged`` with the refused output still in place (#796
    review). The same four readings, taken again against the refused
    receipt's baseline with the receipt's own snapshot set left to the
    restore's reading: a path they name is that residue, or work of the
    operator's since — the readings cannot tell the two apart, and say so —
    and the receipt keeps its authority until it is cleared. A recorded
    ignored entry that is gone is not residue: the guard is against a
    re-arm over a hook's output, an absence seals nothing, deleting a named
    rewrite is how the operator clears it, and a removal the pause named
    is theirs to weigh. Read at
    ``revision``, the target's current tip: the refused epoch after a
    complete restore, or a commit the operator made since, whose new
    entries the flag reading takes as it finds them. Path-only evidence,
    sorted.
    """
    validated_snapshots, _validated_submodules = validate_integration_state_schema(
        run_dir, attempt["snapshots"], attempt["submodules"], attempt["operation_identity"]
    )
    snapshot_paths = [str(entry["path"]) for entry in validated_snapshots]
    cleanup_plan = attempt.get("cleanup_plan") or {}
    tolerated = tuple(cleanup_plan.get("tolerated", ()))
    ignored = attempt.get("ignored")
    named: set[str] = set(
        integrated_stray_paths(
            repo,
            tolerated=tolerated,
            incoming=snapshot_paths,
            run_dir=run_dir,
            ignored=ignored,
        )
    )
    if attempt.get("index_flags") is not None:
        named.update(
            integrated_index_flags_outside_drift(
                repo, attempt["index_flags"], exclude=snapshot_paths, require_named=False
            )
        )
    if ignored is not None:
        # the receipt's own paths were rewritten by the restore, which
        # `_receipt_snapshots_complete` reads by digest, not by identity
        named.update(
            integrated_ignored_additions(
                repo,
                run_dir,
                ignored,
                tolerated=tolerated,
                incoming=snapshot_paths,
                introduced_checkouts=integrated_introduced_gitlinks(
                    repo, run_dir, attempt["submodules"], revision=revision
                ),
                removals=False,
            )
        )
    named.update(
        integrated_submodule_ignored_additions(
            repo, run_dir, attempt["submodules"], revision=revision, removals=False
        )
    )
    return tuple(sorted(named))


# The run's own records under `.bmad-loop/`: per-run and archived state,
# engine plugins' caches, the decision store, operator-action records. Never
# a hook's to write and never merged content — everything else there (the
# hook relay script, a committed `policy.toml`, profile overlays, user
# plugins) is the operator's tracked configuration and is read like any
# other path (#796 review).
_AUTOMATOR_RECORD_PREFIXES = ("runs/", "archive/", "cache/", "operator/")
_AUTOMATOR_RECORD_FILES = frozenset({"decisions.json", "operator-actions.json"})


def automator_dirty_paths(repo: Path) -> dict[str, str]:
    """`dirty_paths` for the automator directory alone, the run's own records left out."""
    rc, out = _git_raw(repo, "status", "--porcelain", "-z", "-uall", "--", AUTOMATOR_DIR_REL)
    if rc != 0:
        raise GitError(f"git status failed in {repo}")
    prefix = f"{AUTOMATOR_DIR_REL}/"
    result: dict[str, str] = {}
    for path, xy in _porcelain_entries(out):
        below = path.removeprefix(prefix)
        if below.startswith(_AUTOMATOR_RECORD_PREFIXES) or below in _AUTOMATOR_RECORD_FILES:
            continue
        result[path] = xy
    return result


def collision_dirty_paths(repo: Path) -> dict[str, str]:
    """The one dirty reading the collision plan, its application, and the
    post-hook stray reading share: the tree outside the automator directory
    (`dirty_paths`) plus the automator directory with the run's own records
    left out (`automator_dirty_paths`). A plan read one way and applied
    another refused every cleanup that named a `.bmad-loop/` path — planned
    from the combined reading, then missing from the exclusion-only re-read
    — and again on every resume (#796 review)."""
    return {**dirty_paths(repo), **automator_dirty_paths(repo)}


def integrated_stray_paths(
    repo: Path,
    *,
    tolerated: Iterable[str],
    incoming: Iterable[str],
    retained_checkouts: Iterable[str] = (),
    run_dir: Path | None = None,
    ignored: object = None,
) -> tuple[str, ...]:
    """Paths dirty after the target's hooks that no receipt reading owns.

    The receipt snapshots the incoming set, the paths that were dirty before
    the merge (cleaned or tolerated), and the declared artifacts; a clean
    tracked file outside all of them has no baseline, and the readings above
    are each scoped to their own set — so a TARGET hook editing, staging,
    deleting or renaming such a file, or writing a new one beside it, went
    unseen and the run recorded ``unit-merged`` over it (#796 review). The
    integrated target may hold exactly one kind of dirt: the strays the guard
    tolerated before the merge, which the snapshot proves unchanged. This is
    the whole-tree ``status`` reading (`dirty_paths`: ``-uall``) — with the
    automator directory read the same way, the run's own records left out
    (`automator_dirty_paths`; #796 review) — minus what other readings
    own — the ``tolerated`` set, the ``incoming`` set (the diff readings' and
    the absent-path probe's), and every ``retained_checkouts`` leftover with
    everything under it (the submodule reading's) — and minus an untracked
    entry the receipt's sealed ``ignored`` listing (`capture_ignored_entries`,
    read from ``run_dir``) recorded at the identity it has now: an ignored
    file that was already there, which the pre-merge guard never listed
    because it was ignored, and which an incoming ``.gitignore`` change
    uncovered, so ``status`` lists it ``??`` after the hooks; it predates
    the attempt and stays where it is, while one the listing holds under
    another identity was written during the attempt and is named (#796
    review). Path-only evidence, in sorted order. Ceilings: ignored entries
    are not read here either, and the reading cannot tell a hook from a
    writer that raced the merge — a per-worktree Editor leaking into the
    main checkout in that window — and names both; the restore reverts what
    a hook STAGED (the post-hook index delta is in its inventory) and leaves
    unstaged and untracked entries where they are, for the operator the
    pause names them to.
    """
    excluded = {_portable_integration_path(path) for path in (*tolerated, *incoming)}
    prefixes = tuple(
        f"{_portable_integration_path(path)}/" for path in dict.fromkeys(retained_checkouts)
    )
    recorded: dict[str, str] = {}
    if ignored is not None and run_dir is not None:
        recorded = _recorded_ignored_entries(run_dir, validate_ignored_entries_evidence(ignored))
    strays: list[str] = []
    for path, xy in collision_dirty_paths(repo).items():
        # `vendor/`: an untracked nested repository, tolerated as `vendor`
        if path in excluded or path.rstrip("/") in excluded:
            continue
        if any(path == prefix or path.startswith(prefix) for prefix in prefixes):
            continue
        if xy == "??" and path in recorded and _lstat_identity(repo, path) == recorded[path]:
            continue
        strays.append(path)
    return tuple(sorted(strays))


def integrated_introduced_directories_drift(
    repo: Path,
    revision: str,
    run_dir: Path,
    snapshots: object,
    *,
    submodules: object = None,
    operation_identity: str | None = None,
) -> tuple[str, ...]:
    """Entries under a directory the integrated commit created that it does not hold.

    The receipt proved every ``absent_parents`` directory absent before the
    attempt, so whatever stands in one after the hooks is attempt-era — and the
    readings above see only what git lists: the diff readings cover tracked
    paths, `integrated_stray_paths` takes ``status`` without ``--ignored``,
    which also never names a ``.git``. So a target hook writing a gitignored
    file into the new directory, or initialising a repository inside it, went
    unseen (#796 review). A directory the commit put where a tracked file or
    symlink stood (``a`` deleted, ``a/b`` added) is the commit's just the
    same, and ``absent_parents`` never names it — the ancestor existed — but
    the receipt captured the entry under its own path as ``regular`` or
    ``symlink``, so a captured non-directory the commit now holds only as a
    prefix is a root too (#796 review) — as is a gitlink the receipt recorded
    unpopulated (``submodules``, ``head`` None: an empty directory, nothing
    snapshotted) that the commit replaced with a tracked directory, which
    the submodule reading accepts as the commit's own by its lack of a
    ``.git`` and reads no further (#796 review) — and as is a directory the
    receipt proved EMPTY (``empty_parents``: the first existing ancestor of
    an incoming path, which git never tracks and no reading lists), whose
    every entry after the hooks is attempt-era the same way (#796 review).
    Each topmost such directory is walked
    on disk, symlinks never followed, against the commit's inventory: a file or
    symlink must be a path the commit holds, a directory a prefix it holds —
    or a gitlink, whose populated checkout is `validate_integrated_submodule_state`'s
    to read (with ``--ignored``, the path having been proved absent) and is
    not descended into. Anything else is reported by path, an unheld
    directory as itself rather than its contents. A directory that is not
    there is nothing to walk; a symlink in its place is drift. Path-only
    evidence, sorted.
    """
    validated, validated_submodules = validate_integration_state_schema(
        run_dir, snapshots, [] if submodules is None else submodules, operation_identity
    )
    roots: set[str] = set()
    replaced: set[str] = set()
    for entry in validated:
        parents = entry.get("absent_parents")
        assert isinstance(parents, list)
        if parents:
            roots.add(str(parents[-1]))  # captured from the path upward: last is topmost
        empty = entry.get("empty_parents")
        assert isinstance(empty, list)
        roots.update(str(parent) for parent in empty)  # proved empty: the topmost root
        if entry["state"] in {"regular", "symlink"}:
            replaced.add(str(entry["path"]))
    for entry in validated_submodules:
        if entry.get("head") is None:
            replaced.add(str(entry["path"]))
    if not roots and not replaced:
        return ()
    inventory = _revision_inventory(repo, revision)
    held = _inventory_held_paths(inventory)
    # a captured file, or unpopulated gitlink, the commit holds only as a
    # prefix stands where the commit made a directory; one it still holds,
    # or deleted, is no root
    roots.update(rel for rel in replaced if rel in held and rel not in inventory)
    if not roots:
        return ()
    drift: list[str] = []
    for root in sorted(roots):
        if any(root.startswith(f"{other}/") for other in roots):
            continue
        _validated, top = _confined_repo_operand(repo, root)
        if top.is_symlink() or _symlink_ancestor(repo, top) is not None:
            drift.append(root)
            continue
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            base = Path(dirpath).relative_to(repo).as_posix()
            for name in sorted(dirnames):
                rel = f"{base}/{name}"
                child = Path(dirpath) / name
                if child.is_symlink():
                    if rel not in inventory:
                        drift.append(rel)
                    continue
                row = inventory.get(rel)
                if row is not None and row[0] == b"160000":
                    dirnames.remove(name)
                    continue
                if rel not in held:
                    drift.append(rel)
                    dirnames.remove(name)
            for name in filenames:
                rel = f"{base}/{name}"
                if rel not in inventory:
                    drift.append(rel)
    return tuple(sorted(drift))


def revision_tree_oid(repo: Path, revision: str) -> str:
    """The tree object id ``revision`` seals (``rev-parse <revision>^{tree}``)."""
    rc, tree, detail = _git_out(repo, "rev-parse", f"{revision}^{{tree}}")
    if rc != 0:
        raise IntegrationEvidenceError(
            f"git rev-parse {revision}^{{tree}} failed in {repo}: {detail}"
        )
    return tree


_FILE_BLOB_MODES = frozenset({b"100644", b"100755"})


def _blob_bytes(repo: Path, oid: str) -> bytes:
    """One blob byte-exactly, for a reading that already knows its object id."""
    proc = git_bytes(repo, "cat-file", "blob", oid)
    if proc.returncode != 0:
        raise IntegrationEvidenceError(f"blob {oid[:12]} could not be read in {repo}")
    return proc.stdout


def _blob_is_binary(data: bytes) -> bool:
    """Git's own heuristic (``buffer_is_binary``): a NUL in the first 8000 bytes."""
    return b"\0" in data[:8000]


def _three_way_folds(repo: Path, base: str | None, held: str, incoming: str) -> bool:
    """Whether blob ``held`` already holds ``incoming``'s change over ``base``:
    the three-way merge of ``incoming`` into ``held`` over ``base`` is clean
    and yields ``held`` byte for byte, so replaying the change would stage
    nothing.

    ``git merge-file``, the same xdiff three-way a ``merge --squash`` resolved
    the file with; ``base`` is ``None`` for a path the change added. A binary
    blob on any side is never folded this way — git merges no binary content,
    so a divergent binary is the target's own later change — and is read as
    unfolded before the probe rather than as a probe fault. Conflicts read
    as unfolded; a probe that could not run raises.
    """
    held_bytes = _blob_bytes(repo, held)
    base_bytes = b"" if base is None else _blob_bytes(repo, base)
    incoming_bytes = _blob_bytes(repo, incoming)
    if any(_blob_is_binary(data) for data in (held_bytes, base_bytes, incoming_bytes)):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        (shadow / "held").write_bytes(held_bytes)
        (shadow / "base").write_bytes(base_bytes)
        (shadow / "incoming").write_bytes(incoming_bytes)
        proc = git_bytes(
            repo,
            "merge-file",
            "-p",
            "--",
            str(shadow / "held"),
            str(shadow / "base"),
            str(shadow / "incoming"),
        )
    if proc.returncode == 0:
        return proc.stdout == held_bytes
    # the exit status is the conflict count, truncated to 127; a fault is negative
    if 0 < proc.returncode < 128:
        return False
    raise IntegrationEvidenceError(
        f"the three-way reading of blob {held[:12]} against {incoming[:12]} could not be "
        f"taken in {repo}"
    )


def unfolded_changes(repo: Path, baseline: str, source: str, revision: str) -> tuple[str, ...]:
    """Paths ``baseline..source`` changed that ``revision``'s tree does not hold
    folded.

    The reading a consumed integration stands on when ancestry cannot answer:
    a squash seals a commit of its own, so the unit's commit is never in the
    target's history, but every change it made over its baseline is in the
    target's tree — each added or modified path held with ``source``'s mode,
    and with ``source``'s object id or, where the target had itself moved
    the file before the squash resolved it, with the blob a three-way merge
    of the unit's change over the baseline into the held one leaves as it is
    (the squash result holds both sides' edits, so blob equality alone read
    every such file as unfolded and paused a landed, validated integration
    for ever — #796 review); each deleted path absent. One whole-tree
    ``diff-tree`` between the unit's own commits and one whole-tree inventory
    of ``revision``, the shape that keeps a wide change set off argv, and one
    per-file three-way probe only where the object ids differ; renames read
    as their two sides so a moved path's source is checked absent. A symlink
    or gitlink folds only at ``source``'s exact object id: git three-way
    merges neither. Empty when the tree folds the whole change set; an
    unreadable probe raises.
    """
    proc = git_bytes(repo, "diff-tree", "-r", "-z", "--no-renames", baseline, source)
    if proc.returncode != 0:
        raise IntegrationEvidenceError(
            f"the unit's change set {baseline[:12]}..{source[:12]} could not be read in {repo}"
        )
    held = _revision_inventory(repo, revision)
    unfolded: list[str] = []
    fields = proc.stdout.split(b"\0")
    # ``:<old mode> <new mode> <old oid> <new oid> <status>`` then the path, NUL-separated.
    for metadata, raw_path in zip(fields[0::2], fields[1::2], strict=False):
        try:
            old_mode, new_mode, old_oid, new_oid, status = metadata.lstrip(b":").split(b" ", 4)
        except ValueError as exc:
            raise IntegrationEvidenceError("the unit's change set is malformed") from exc
        path = os.fsdecode(raw_path)
        row = held.get(path)
        if status.startswith(b"D"):
            if row is not None:
                unfolded.append(path)
            continue
        incoming = os.fsdecode(new_oid)
        if row is None or row[0] != new_mode:
            unfolded.append(path)
            continue
        if row[2] == incoming:
            continue
        if new_mode not in _FILE_BLOB_MODES:
            unfolded.append(path)
            continue
        # the unit's own base for the file: absent (an add, or a path that was
        # something other than a file) reads as empty
        base = os.fsdecode(old_oid) if old_mode in _FILE_BLOB_MODES else None
        if not _three_way_folds(repo, base, row[2], incoming):
            unfolded.append(path)
    return tuple(sorted(unfolded))


def integration_cleanup_state_recoverable(
    repo: Path,
    run_dir: Path,
    snapshots: object,
    *,
    cleaned: Iterable[str],
    untracked: Iterable[str],
    revision: str,
    operation_identity: str,
) -> bool:
    """Prove each cleanup operand is either pre-clean or the planned result.

    Anything else may be fresh operator state and must never be overwritten by a
    crash replay merely because a ``cleanup-pending`` receipt exists. The
    planned result of a tracked operand is its worktree at the index — the
    cleanup is ``checkout -- path``, which never writes the index entry — so
    the index it carries is the captured one, flag word included; the content
    probes alone cannot say so, since ``diff`` trusts an assume-unchanged or
    skip-worktree entry and reads clean over whatever the worktree holds, and
    a flag an operator set after the host died is exactly what the restore
    would flatten (#796 review).
    """
    validated, _submodules = validate_integration_state_schema(
        run_dir, snapshots, [], operation_identity
    )
    by_path = {str(entry["path"]): entry for entry in validated}
    untracked_set = set(preflight_integration_paths(untracked))
    for rel in preflight_integration_paths(cleaned):
        entry = by_path.get(rel)
        if entry is None:
            return False
        if _receipt_snapshots_complete(repo, run_dir, [entry]):
            continue
        if rel in untracked_set:
            _validated, candidate = _confined_repo_operand(repo, rel)
            if (
                _symlink_ancestor(repo, candidate) is not None
                or candidate.exists()
                or candidate.is_symlink()
                or _index_state(repo, rel)["entries"]
            ):
                return False
            continue
        if _index_state(repo, rel) != _validated_index_state(entry["index"]):
            return False
        worktree = git_bytes(repo, "diff", "--quiet", revision, "--", rel)
        index = git_bytes(repo, "diff", "--cached", "--quiet", revision, "--", rel)
        if worktree.returncode != 0 or index.returncode != 0:
            return False
    return True


def _integrated_submodule_checkout_unchanged(
    repo: Path, rel: str, checkout: Path, *, allowed_heads: set[str], introduced: bool
) -> None:
    """A populated checkout at an incoming submodule path, after the target's hooks.

    Owned by this repository at its lexical location, clean, and at a HEAD the
    integrated commit or the receipt vouches for; anything else is drift. A
    captured checkout is read as the receipt captured it, ignored files never
    read; a checkout at a gitlink the commit ``introduced`` stands where the
    receipt proved nothing was, so everything in it is attempt-era and the
    reading asks for ignored entries too — a hook's write the submodule's own
    ``.gitignore`` covers is still its output (#796 review).
    """
    root = repo.resolve(strict=True)
    if checkout.resolve(strict=True) != root.joinpath(*rel.split("/")):
        raise IntegrationEvidenceError("integrated target submodule checkout was redirected")
    ignored = ("--ignored",) if introduced else ()
    status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall", *ignored)
    if (
        not _submodule_checkout_owned(root, checkout)
        or status.returncode != 0
        or status.stdout
        or rev_parse_head(checkout) not in allowed_heads
    ):
        raise IntegrationEvidenceError("target hook changed an integrated submodule checkout")


def _integrated_replaced_submodule_checkout_unchanged(
    repo: Path,
    rel: str,
    checkout: Path,
    *,
    allowed_heads: set[str],
    held: Iterable[str],
) -> None:
    """The leftover checkout inside a tracked directory that replaced its gitlink.

    Owned by this repository at its lexical location (no gitlink names the
    superproject any more, so ownership is its git dir under ``.git/modules``),
    at a HEAD the receipt vouches for, and — read as its own repository, the
    only reading that sees past the superproject's index — reporting no entry
    but the integrated tree's own writes into it: every status row, joined
    under ``rel``, must be a path ``held`` names. Anything else is a hook's.
    """
    root = repo.resolve(strict=True)
    if checkout.resolve(strict=True) != root.joinpath(*rel.split("/")):
        raise IntegrationEvidenceError("integrated target submodule checkout was redirected")
    status = git_bytes(checkout, "status", "--porcelain", "-z", "-uall")
    if (
        not _submodule_checkout_owned(root, checkout)
        or status.returncode != 0
        or rev_parse_head(checkout) not in allowed_heads
    ):
        raise IntegrationEvidenceError("target hook changed an integrated submodule checkout")
    held_paths = set(held)
    for nested in _porcelain_paths(os.fsdecode(status.stdout)):
        if f"{rel}/{nested}" not in held_paths:
            raise IntegrationEvidenceError("target hook changed an integrated submodule checkout")


def validate_integrated_submodule_state(
    repo: Path,
    submodules: object,
    *,
    prospective_paths: Iterable[str],
    revision: str,
) -> tuple[str, ...]:
    """Validate legitimate incoming gitlink changes without ignoring checkout drift.

    The integrated commit is the authority for every incoming path it holds
    as a gitlink, captured by the receipt or introduced by the commit: the
    post-hook index must carry exactly that gitlink and a populated checkout
    must be this repository's, clean, and at the gitlink (or, for a captured
    one, at the captured HEAD). Read here rather than left to the diff
    readings because an incoming ``.gitmodules`` can set
    ``submodule.<name>.ignore = all``, under which ``git diff`` — worktree
    and ``--cached`` alike — reports nothing about that submodule: not a
    hook's ``submodule update --init`` with files written into the new
    checkout, not a moved HEAD, not even a rewritten gitlink (#796 review).

    For a captured submodule the commit no longer holds as a gitlink: a blob
    in its place is the diff readings' business; nothing at all means the
    incoming commit deleted the submodule, and git itself leaves the
    populated checkout behind (``warning: unable to rmdir``, then
    ``?? path/``), so a leftover is not a hook's doing. It is accepted only
    as the exact captured checkout — owned, clean, at the captured HEAD —
    and every leftover so accepted is returned for `integrated_paths_drift`
    and `integrated_stray_paths` to leave to this reading. A checkout git
    could remove is simply absent; a file or foreign directory in its place
    is drift. A tree in its place (the path held through a prefix — a
    submodule replaced by a tracked directory) is the same leftover with the
    commit's files written INTO it: its ``.git`` and old payload sit beside
    the new tracked files, which the superproject's diff readings own, so the
    leftover is read through its own status and may hold nothing but paths
    the integrated tree holds under it (#796 review). A captured checkout's
    ignored entries — a hook's write its own ``.gitignore`` covers, in a
    checkout or either leftover — are `integrated_submodule_ignored_additions`'s
    reading, against the listing the receipt sealed beside its HEAD.
    """
    if not isinstance(submodules, list):
        raise IntegrationEvidenceError("persisted target submodule evidence is malformed")
    prospective = set(preflight_integration_paths(prospective_paths))
    if not prospective:
        return ()
    captured: dict[str, dict[str, object]] = {}
    unpopulated: set[str] = set()
    captured_flags: dict[str, object] = {}
    for raw in submodules:
        if not isinstance(raw, dict) or raw.get("path") not in prospective:
            continue
        rel = _portable_integration_path(raw.get("path"))
        captured_flags[rel] = raw.get("flags")
        # captured unpopulated: the receipt proved an empty directory, so a
        # checkout there after the hooks is attempt-era in full and reads
        # as one the commit introduced — ignored entries counted, no
        # captured HEAD to allow; and where the commit holds no gitlink any
        # more, anything standing there is a hook's (#796 review)
        if raw.get("head") is None:
            unpopulated.add(rel)
            continue
        captured[rel] = raw
    inventory = _revision_inventory(repo, revision)
    held_paths = _inventory_held_paths(inventory)
    fresh_words = _fresh_index_flag_words(repo)
    retained: list[str] = []
    for rel in sorted(prospective):
        held = inventory.get(rel)
        raw = captured.get(rel)
        if held is None:
            if raw is None:
                checkout = repo / rel
                if rel in unpopulated and checkout.is_dir() and any(checkout.iterdir()):
                    if rel in held_paths and not (checkout / ".git").exists():
                        # the commit's own directory in its place — walked
                        # against the commit by the introduced-directory reading
                        continue
                    raise IntegrationEvidenceError(
                        "target hook changed an integrated submodule checkout"
                    )
                continue
            checkout = repo / rel
            allowed_heads = {str(raw.get("head")), str(raw.get("gitlink"))}
            if rel in held_paths:
                # A tracked directory in the submodule's place: git wrote the
                # commit's files INTO the checkout it could not remove, so a
                # leftover is one with its `.git` still there. Its descendants
                # the commit holds are the diff readings'; the reading here is
                # the leftover's own status, which may name only those.
                if not checkout.is_dir() or not (checkout / ".git").exists():
                    continue
                _integrated_replaced_submodule_checkout_unchanged(
                    repo,
                    rel,
                    checkout,
                    allowed_heads=allowed_heads,
                    held=inventory.keys(),
                )
                retained.append(rel)
                continue
            if not checkout.is_dir():
                continue
            _integrated_submodule_checkout_unchanged(
                repo, rel, checkout, allowed_heads=allowed_heads, introduced=False
            )
            retained.append(rel)
            continue
        mode, kind, oid = held
        if mode != b"160000":
            continue
        if kind != b"commit":
            raise IntegrationEvidenceError("integrated target submodule evidence is malformed")
        # a captured gitlink the commit rewrote may carry its captured word
        # or a fresh one — `git merge` writes the entry anew, clearing an
        # assume-unchanged bit that a fast-forward or squash keeps — and a
        # word that is neither is a hook's (#796 review)
        captured_word = captured_flags.get(rel)
        accepted_words = (
            fresh_words if captured_word is None else fresh_words | {str(captured_word)}
        )
        if not _gitlink_index_matches(_index_state(repo, rel), oid, flags=accepted_words):
            raise IntegrationEvidenceError("target hook changed an integrated submodule gitlink")
        checkout = repo / rel
        # an unpopulated gitlink is an empty directory: git's shape, nothing to read
        if not checkout.is_dir() or not any(checkout.iterdir()):
            continue
        allowed_heads = {oid} if raw is None else {str(raw.get("head")), oid}
        _integrated_submodule_checkout_unchanged(
            repo, rel, checkout, allowed_heads=allowed_heads, introduced=raw is None
        )
    return tuple(retained)


def restore_integration_nonref_state(
    repo: Path,
    refname: str,
    *,
    revision: str,
    run_dir: Path,
    snapshots: object,
    submodules: object,
    operation_identity: str,
    include_paths: Iterable[str] | None = None,
) -> None:
    """Restore receipt-owned cleanup after a typed operation made no ref update."""
    validated_snapshots, validated_submodules = validate_integration_state_schema(
        run_dir, snapshots, submodules, operation_identity
    )
    if include_paths is not None:
        selected = set(preflight_integration_paths(include_paths))
        validated_snapshots = [entry for entry in validated_snapshots if entry["path"] in selected]
        validated_submodules = [
            entry for entry in validated_submodules if entry["path"] in selected
        ]
    for entry in validated_submodules:
        _validated_submodule_checkout(
            repo,
            entry,
            verify_head=False,
            revision=revision,
            allow_missing=True,
        )
    if ref_revision(repo, refname) != revision:
        raise IntegrationRestoreError("target moved after typed integration refusal")
    _restore_receipt_snapshots(repo, run_dir, validated_snapshots)
    _restore_receipt_index(repo, validated_snapshots)
    _restore_submodule_checkouts(repo, validated_submodules, old_revision=revision)
    if not integration_restoration_complete(
        repo,
        refname,
        old_revision=revision,
        new_revision=revision,
        run_dir=run_dir,
        snapshots=validated_snapshots,
        submodules=validated_submodules,
        operation_identity=operation_identity,
    ):
        raise IntegrationRestoreError("target non-ref restoration is incomplete")


def restore_integration_ref(
    repo: Path,
    refname: str,
    *,
    old_revision: str,
    new_revision: str,
    extra_paths: Iterable[str] = (),
    run_dir: Path | None = None,
    snapshots: object = (),
    submodules: object = (),
    operation_identity: str | None = None,
) -> None:
    """Prepare a refused checkout, then CAS its target ref back exactly once.

    The path-scoped ``git restore`` does not move a ref.  It restores every path
    changed by the receipt-owned commit plus declared artifact paths (the latter
    catches post-commit index-only hook drift), while leaving unrelated unstaged
    target dirt alone.  ``update-ref`` is then the sole ref-moving command and
    atomically checks ownership.  If a concurrent commit wins after checkout
    preparation, the CAS fails and that later commit remains the target tip;
    checkout repair is left explicit rather than risking a second ref update.
    """
    if run_dir is None:
        if snapshots not in ((), []) or submodules not in ((), []):
            raise IntegrationEvidenceError("target integration snapshot root is missing")
        validated_snapshots: list[dict[str, object]] = []
        validated_submodules: list[dict[str, object]] = []
    else:
        validated_snapshots, validated_submodules = validate_integration_state_schema(
            run_dir, snapshots, submodules, operation_identity
        )
    # Validate every submodule operand and its superproject ownership before the
    # first restore can mutate either repository.
    for entry in validated_submodules:
        _validated_submodule_checkout(
            repo,
            entry,
            verify_head=False,
            revision=old_revision,
            allow_missing=True,
        )
    rc, symbolic, _detail = _git_out(repo, "symbolic-ref", "-q", "HEAD")
    if rc != 0 or symbolic != refname:
        raise IntegrationRestoreError("target checkout no longer owns the integration ref")
    if ref_revision(repo, refname) != new_revision:
        raise IntegrationRestoreError(
            "target ref moved after the refused integration; no restoration was attempted"
        )
    restore_paths = _integration_restore_paths(
        repo,
        old_revision=old_revision,
        new_revision=new_revision,
        extra_paths=extra_paths,
    )
    snapshot_paths = [str(entry["path"]) for entry in validated_snapshots]
    currently_indexed = [path for path in snapshot_paths if path_tracked(repo, path)]
    _restore_paths_from_stdin(
        repo,
        old_revision,
        [*restore_paths, *currently_indexed],
    )
    if run_dir is not None:
        _restore_receipt_snapshots(repo, run_dir, validated_snapshots)
        _restore_receipt_index(repo, validated_snapshots)
    _restore_submodule_checkouts(repo, validated_submodules, old_revision=old_revision)
    if run_dir is not None and not integration_nonref_state_unchanged(
        repo,
        run_dir,
        validated_snapshots,
        validated_submodules,
        operation_identity=operation_identity,
    ):
        raise IntegrationRestoreError(
            "target non-ref restoration changed before the target ref could be restored"
        )
    rc, _out = _git(
        repo,
        "update-ref",
        "-m",
        "bmad-loop integration validation refused",
        refname,
        old_revision,
        new_revision,
    )
    if rc != 0:
        raise IntegrationRestoreError(
            "target ref moved during integration restoration; its later commit was preserved, "
            "but the prepared checkout requires manual recovery"
        )
    if ref_revision(repo, refname) != old_revision:
        raise IntegrationRestoreError("target ref changed during integration restoration")
    if not integration_restoration_complete(
        repo,
        refname,
        old_revision=old_revision,
        new_revision=new_revision,
        extra_paths=extra_paths,
        run_dir=run_dir,
        snapshots=validated_snapshots,
        submodules=validated_submodules,
        operation_identity=operation_identity,
    ):
        raise IntegrationRestoreError("target integration restoration is incomplete")


def integration_restoration_complete(
    repo: Path,
    refname: str,
    *,
    old_revision: str,
    new_revision: str,
    extra_paths: Iterable[str] = (),
    run_dir: Path | None = None,
    snapshots: object = (),
    submodules: object = (),
    operation_identity: str | None = None,
) -> bool:
    """Whether a persisted refused transition is fully restored and re-armable."""
    if run_dir is None:
        if snapshots not in ((), []) or submodules not in ((), []):
            raise IntegrationEvidenceError("target integration snapshot root is missing")
        validated_snapshots: list[dict[str, object]] = []
        validated_submodules: list[dict[str, object]] = []
    else:
        validated_snapshots, validated_submodules = validate_integration_state_schema(
            run_dir, snapshots, submodules, operation_identity
        )
    if ref_revision(repo, refname) != old_revision:
        return False
    paths = _integration_restore_paths(
        repo,
        old_revision=old_revision,
        new_revision=new_revision,
        extra_paths=extra_paths,
    )
    if run_dir is not None and not _receipt_snapshots_complete(repo, run_dir, validated_snapshots):
        return False
    index_delta = _nul_git_paths(
        git_bytes(repo, "diff", "--cached", "--name-only", "-z", old_revision, "--"),
        unavailable="restored target index evidence is unavailable",
    )
    snapshot_paths = {str(entry["path"]) for entry in validated_snapshots}
    if set(index_delta) - snapshot_paths:
        return False
    for entry in validated_submodules:
        try:
            _validated_submodule_checkout(repo, entry, verify_head=True)
        except IntegrationEvidenceError:
            return False
    if not paths:
        return True
    # The worktree reading is scoped to the receipt-attributable inventory
    # (`paths`: commit delta, post-hook index delta, accepted artifact paths)
    # — on the legacy arm as a pathspec, on the receipt arm as a whole-tree
    # read filtered here, since `git diff` takes no stdin pathspec and a wide
    # inventory stays off argv. A tracked file the OPERATOR edited, unstaged,
    # after the receipt was armed sits outside that inventory: it is theirs,
    # the restore never touched it, and it must not turn a completed restore
    # into "incomplete" — at refusal time, and again on every replay until
    # they clear it (#796 review).
    path_args = () if run_dir is not None else tuple(_literal_specs(paths))
    worktree_delta = _nul_git_paths(
        git_bytes(
            repo,
            "diff",
            "--name-only",
            "-z",
            old_revision,
            "--",
            *path_args,
        ),
        unavailable="restored target worktree evidence is unavailable",
    )
    submodule_paths = {str(entry["path"]) for entry in validated_submodules}
    return not (set(worktree_delta) & set(paths) - snapshot_paths - submodule_paths)


def last_commit_for(repo: Path, path: Path) -> str:
    """Sha of the most recent commit touching ``path``, or ``""`` when no commit
    does (an untracked or deleted-without-history file) or the path lies outside
    the repo. Backs the derived provenance of an operator park record, which is
    written into the very commit it rides and so cannot store its own sha. Git
    failures raise :class:`GitError` like every sibling; only the path relation
    degrades silently, mirroring `commit_paths`' outside-the-repo contract. Reads
    stdout alone (`_git_out`), since a git that warns at rc 0 would otherwise make
    this answer a warning-suffixed "sha" (#442)."""
    try:
        rel = Path(path).resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return ""
    rc, out, detail = _git_out(repo, "log", "-n", "1", "--format=%H", "--", rel)
    if rc != 0:
        raise GitError(f"git log failed in {repo}: {detail}")
    return out


def worktree_clean(repo: Path) -> bool:
    """True when the tree holds no change git would report.

    The orchestrator's own config file (.bmad-loop/policy.toml) is excluded: the TUI
    settings editor rewrites it, and a tracked config edit must not count as a "dirty
    tree" that blocks run/sweep/validate or forces a commit. Scope is policy.toml only
    — the deferred-work ledger also lives under .bmad-loop/ and is meant to be
    committed (see sweep._commit_ledger).

    Reads `stdout` ALONE rather than `_git`'s stdout+stderr merge, for the reason
    :func:`path_tracked` spells out: `status` exits 0 while still writing to stderr (a
    `core.fsmonitor` hook that cannot exec, an unknown `core.fsyncMethod`, a stale
    index advisory), and against the merged stream that chatter is indistinguishable
    from a porcelain record — a pristine tree answers DIRTY. That direction is not
    benign here: six callers gate on it, and `cli.py`'s three refuse the command
    outright, so a host with a noisy git config could never start a run and the
    message would name no file. The error path keeps the merge, where stderr is the
    only informative half."""
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain",
            "--",
            ".",
            f":(exclude){POLICY_FILE_REL}",
        ],
        repo,
    )
    if proc.returncode != 0:
        merged = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git status failed in {repo}: {merged}")
    return proc.stdout.strip() == ""


def path_clean(repo: Path, rel: str) -> bool:
    """True when nothing under the single pathspec `rel` (relative to `repo`)
    differs from HEAD — the NARROW sibling of :func:`worktree_clean`.

    It exists because :func:`worktree_clean` answers about a whole subtree while its
    caller acts on ONE file: `sweep._commit_ledger` asks "is the file I just
    published dirty?" and commits that file alone via :func:`commit_paths`
    (DW-183/DW-185/DW-187). The write half needs no narrow sibling — `commit_paths`
    already commits an exact path list — but the DECISION to write does, and taking
    it here is what keeps an already-clean publish from reaching `git add` at all.

    No `:(exclude)<policy.toml>` here, unlike the wide sibling. That exclusion is
    about a whole-tree scan sweeping in an operator's config edit; a single
    pathspec naming one published file cannot reach `policy.toml` at all, so the
    exclusion would be inert and only obscure what is being asked.

    Reads `stdout` ALONE for the reason :func:`worktree_clean` spells out: `status`
    exits 0 while still writing to stderr (a `core.fsmonitor` hook that cannot
    exec, an unknown `core.fsyncMethod`, a stale index advisory), and against a
    merged stream that chatter is indistinguishable from a porcelain record — a
    clean path would answer DIRTY on a noisy host, and every already-clean publish
    would then stage and re-interrogate a file it had nothing to say about. The
    error path keeps the merge, where stderr is the informative half."""
    # A resolved symlink target may have any basename, including pathspec magic.
    # Match commit_paths' literal scope and include new publications even when
    # the operator hides untracked files in their interactive status display.
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *_literal_specs([rel]),
        ],
        repo,
    )
    if proc.returncode != 0:
        merged = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git status failed in {repo}: {merged}")
    return proc.stdout.strip() == ""


def _artifact_dir_entries(repo: Path, artifact_dir: Path) -> list[str] | None:
    """The IGNORED entries (`!!` porcelain records) git lists under `artifact_dir`,
    as repo-relative posix paths — the listing behind the bundle path's
    artifact-only receipt (DW-273) and its attempt-start snapshot.

    `None` when the receipt cannot be consulted at all: `artifact_dir` resolves
    outside `repo` (an artifacts dir configured beside the checkout holds nothing
    git in `repo` can list, and a pathspec that escapes the tree would be a git
    error rather than an answer), `artifact_dir` IS `repo` (a `.` pathspec would
    list every ignored file in the tree — `.venv`, caches — and accept the receipt
    trivially), or git refuses the listing (rc != 0). A `[]` is the OTHER answer —
    git ran and listed no ignored entry — and callers that relax a gate on this
    must treat both as "no receipt". A `GitError` propagates: the chokepoint's
    environment faults (timeout, spawn) are the caller's to escalate, exactly as
    they are for the ordinary proof-of-work probe.

    ONLY `!!` records count. `status --ignored` also lists the tracked (` M`) and
    untracked-not-ignored (`??`) records under the dir, and those are exactly what
    the ordinary probe already measured — and, for the bundle's own spec, already
    EXCLUDED. Under the `bmad-loop init` default layout (`_bmad-output/` is not
    gitignored) a bundle whose only residue is its own spec's status flip or its
    own newly written spec would otherwise be accepted on a listing of one; the
    receipt exists for the gitignored layout alone, so it reads only what that
    layout produces.

    Why `status --ignored` and not a baseline diff: ignored paths never enter the
    index, so there is no commit to diff them against. The listing answers only
    "the artifacts dir holds ignored content under the code tree" — it cannot say
    which entry a session wrote, which is why :func:`artifact_dir_snapshot` and
    :func:`_artifact_dir_owned_entries` exist: the ATTEMPT's own start-of-attempt
    fingerprint of this listing is the baseline ignored paths otherwise lack.
    `--untracked-files=all` makes git enumerate the individual files inside an
    ignored directory rather than collapsing the directory to one record, so the
    entries are files that can be fingerprinted, not prefixes.

    `-z` bytes, decoded with `os.fsdecode`, for the reason `dirty_paths` and
    `commit_paths` read them so: ordinary porcelain C-quotes a non-ASCII name
    under `core.quotePath`, and a quoted record is not a path this module can
    `stat`. Reads `stdout` ALONE, for the reason :func:`path_clean` spells out:
    `status` exits 0 while still writing advisories to stderr, and against a
    merged stream that chatter would be one phantom entry — enough, on its own,
    to accept a receipt over an empty directory. Literal pathspec, like every
    other directory-scoped operand here (`_exclude_specs`): a configured
    artifacts dir may carry glob magic in a segment."""
    try:
        rel = artifact_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    except (OSError, RuntimeError):
        try:
            rel = artifact_dir.relative_to(repo)
        except ValueError:
            return None
    if rel == Path("."):
        return None
    proc = git_bytes(
        repo,
        "status",
        "--porcelain",
        "-z",
        "--ignored",
        "--untracked-files=all",
        "--",
        *_literal_specs([rel.as_posix()]),
    )
    if proc.returncode != 0:
        return None
    # `-z` records are `XY<space>path\0`; a rename carries a second `\0`-terminated
    # operand, but only tracked records rename, and this reads `!!` alone.
    return [
        os.fsdecode(record[3:]) for record in proc.stdout.split(b"\0") if record.startswith(b"!! ")
    ]


# One ignored entry's fingerprint in an attempt-start snapshot: `[st_mtime_ns,
# st_size]`, or `None` when the entry was listed but could not be measured
# (`lstat` refused, or it vanished between the listing and the probe). JSON-shaped
# on purpose — it persists on `StoryTask.baseline_artifacts` through state.json.
ArtifactFingerprint = list[int] | None


def _artifact_fingerprint(repo: Path, rel: str) -> ArtifactFingerprint:
    """`[st_mtime_ns, st_size]` of `repo/rel` via `lstat`, or `None` on any
    `OSError` — the entry stays in the snapshot as "present, unmeasurable", which
    :func:`_artifact_dir_owned_entries` never credits."""
    try:
        st = (repo / rel).lstat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def artifact_dir_snapshot(repo: Path, artifact_dir: Path) -> dict[str, ArtifactFingerprint] | None:
    """The attempt-start fingerprint of every ignored entry under `artifact_dir`,
    keyed by repo-relative posix path — the baseline the artifact-only receipt
    (DW-273) measures ownership against, stamped by `Engine._dev_phase` beside
    `baseline_commit` and `baseline_untracked` at every genuinely new attempt.

    `None` on exactly :func:`_artifact_dir_entries`' "cannot be consulted" answer
    (outside the tree, IS the tree, git refused), and a `GitError` propagates for
    the caller to degrade. An empty dict is a real answer: nothing was there, so
    everything the attempt leaves is its own."""
    entries = _artifact_dir_entries(repo, artifact_dir)
    if entries is None:
        return None
    return {rel: _artifact_fingerprint(repo, rel) for rel in entries}


def _artifact_dir_owned_entries(
    repo: Path, artifact_dir: Path, baseline: dict[str, ArtifactFingerprint]
) -> list[str] | None:
    """The ignored entries under `artifact_dir` this ATTEMPT created or changed:
    listed now and either absent from `baseline` or carrying a different
    fingerprint than the one :func:`artifact_dir_snapshot` recorded there.

    The attempt-ownership half of the receipt. Without it any pre-existing
    ignored residue — a spec from an earlier bundle, an erratum note from last
    week — satisfied the listing, and a session that asserted `artifact_only`
    and wrote nothing cleared the proof-of-work gate on it. Uncertainty keeps the
    gate strict in both directions: an entry whose baseline fingerprint is `None`
    (unmeasurable at attempt start) is never credited even if it measures now,
    and an entry unmeasurable NOW is not credited either — a fingerprint that
    cannot be taken proves no change. Deleted entries are not deliverables and
    are not counted. `None` and `[]` are :func:`_artifact_dir_entries`' two
    answers, unchanged in meaning: no listing at all, and a listing with nothing
    this attempt owns (which the caller distinguishes from an empty directory by
    the listing's own size)."""
    entries = _artifact_dir_entries(repo, artifact_dir)
    if entries is None:
        return None
    owned: list[str] = []
    for rel in entries:
        current = _artifact_fingerprint(repo, rel)
        if current is None:
            continue
        if rel not in baseline:
            owned.append(rel)
            continue
        before = baseline[rel]
        if before is not None and before != current:
            owned.append(rel)
    return owned


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is an ancestor of (or equal to) `descendant`.

    Any git failure — unknown ref, shallow history, not a repo, a timeout
    (surfacing as GitError since `_run_git` translates it, #156) — reads as
    False: callers use this to *relax* a gate, so uncertainty must keep the
    gate strict."""
    try:
        code, _ = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    except (OSError, GitError):
        return False
    return code == 0


# A `baseline_revision` that is a Git revision expression rather than an object id
# (`HEAD`, a branch or tag name, `main~2`) resolves at verification time, not when the
# session stamped it. Hex spelling is necessary but insufficient: Git also permits
# all-hex ref names. The stamp is `git rev-parse HEAD` output by contract, so requiring
# a uniquely disambiguated direct commit costs a well-behaved session nothing. Length
# floor is git's shortest auto abbreviation: with `core.abbrev` unset the length scales
# with the repository's object count and clamps upward to 7 only for small repos, so
# nothing git abbreviates on its own is shorter. Ceiling admits sha256.
_OBJECT_ID = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")


def _canonical_commit_oid(repo: Path, claim: str) -> str | None:
    """Resolve one immutable commit object id, independent of Git refs.

    ``claim`` must be 7–64 hexadecimal characters and uniquely identify one
    object through ``rev-parse --disambiguate``. The object itself must be a
    direct commit: blob, tree, and annotated-tag objects are refused rather
    than peeled. The returned value is Git's canonical full object id.

    Invalid, unresolved, ambiguous, and non-commit claims read as ``None``.
    Operational Git failures retain their typed :class:`GitError` so the
    verification boundary can escalate instead of misreporting a mismatch.
    """
    if not _OBJECT_ID.fullmatch(claim):
        return None
    rc, out, _ = _git_out(repo, "rev-parse", f"--disambiguate={claim}")
    objects = out.splitlines()
    if rc != 0 or len(objects) != 1:
        return None
    oid = objects[0]
    rc, object_type, _ = _git_out(repo, "cat-file", "-t", oid)
    return oid if rc == 0 and object_type == "commit" else None


def commit_reachable_above_baseline(repo: Path, claimed_oid: str, baseline: str) -> bool:
    """Whether a canonical claimed commit descends from ``baseline`` and is
    reachable from this checkout's current ``HEAD``.

    The caller handles equality first, so a successful call represents the
    strictly newer shape needed when step-03 stamps ``baseline_revision`` after
    an intervening commit. Older, diverged, and off-HEAD commits stay refused.

    With worktree isolation, the isolated history preserves provenance for the
    unit. In the default shared checkout, reachability cannot identify which
    session produced a commit; the caller therefore re-anchors proof to this
    claim and requires a later tracked, staged, or committed change. The shared
    mode proves only that such work exists after the claim, not who made it.

    Any Git failure reads as ``False`` because this result relaxes a gate and
    uncertainty must keep the stricter path.
    """
    if not is_ancestor(repo, baseline, claimed_oid):
        return False  # older, diverged, or unknown -> not an accepted descendant
    try:
        head = rev_parse_head(repo)
    except (OSError, GitError):
        return False
    return is_ancestor(repo, claimed_oid, head)


def has_changes_since(
    repo: Path,
    baseline: str,
    exclude: tuple[str, ...] = (),
    *,
    baseline_untracked: list[str] | None = None,
    include_untracked: bool = True,
) -> bool:
    """True if tracked changes since baseline, or allowed untracked files exist.

    `exclude` is repo-relative posix dir prefixes whose changes don't count —
    used by the dev/bundle proof-of-work gate to ignore the orchestrator-owned
    BMAD artifacts (composed by `verify_dev_exclude_relpaths`, relative to the same
    root this is invoked against), so a session that only rewrites its own spec
    (e.g. the frontmatter-status reconcile) under them doesn't register as real
    implementation work. Mirrors
    `attempt_dirty`'s exclusion. Default `()` keeps the unscoped behavior.

    `baseline_untracked` is the untracked-file snapshot taken when the baseline
    was recorded; when given, those files already existed before the session ran
    and are subtracted, so pre-session residue (e.g. an earlier halt's saved
    intent-gap patch, which `_protected_relpaths` shields from every reset) can
    never masquerade as this session's work.

    ``include_untracked=False`` restricts proof to tracked, staged, or committed
    changes. It is used when verification adopts a later descendant baseline:
    the launch-time snapshot cannot establish whether an untracked file appeared
    before or after that later commit. The default preserves every established
    caller and exact-baseline proof.

    `None` means count EVERY untracked file — deliberately the *opposite* of
    `attempt_dirty`'s `None` = ignore-all, and not an oversight. The two gates
    fail open in opposite directions: a proof-of-work gate must fail open toward
    "work happened" (a pre-snapshot run must not have its gate silently
    weakened into never seeing new files), while a rollback gate must fail open
    toward "nothing to remove" (never delete a file it cannot prove this attempt
    created). Keep it that way.

    Every non-zero `git diff` result reads as "changed" here, INCLUDING a refusal
    (rc 128 — an unresolvable baseline, a repo git will not read). That is the
    fail-open above, and it is deliberate for a gate. A caller that needs to tell
    "git said there are changes" from "git would not answer" calls
    :func:`_changes_since`, whose tri-state this function collapses; the collapse
    lives in one place so the gate and any observer share one body."""
    answer = _changes_since(
        repo,
        baseline,
        exclude,
        baseline_untracked=baseline_untracked,
        include_untracked=include_untracked,
    )
    # unanswerable -> the stricter reading for a gate: assume work happened
    return True if answer is None else answer


def _changes_since(
    repo: Path,
    baseline: str,
    exclude: tuple[str, ...] = (),
    *,
    literal_path: str | None = None,
    baseline_untracked: list[str] | None = None,
    include_untracked: bool = True,
) -> bool | None:
    """:func:`has_changes_since` before its fail-open is applied: ``True`` /
    ``False`` when git answered, and ``None`` when git REFUSED to answer at all.

    `git diff --quiet` reports "no differences" as rc 0 and "differences" as rc 1;
    anything else is the command failing rather than answering (rc 128 for a
    baseline it cannot resolve or a directory that is not a repository). The gate
    above cannot act on that distinction — uncertainty there must keep the
    stricter path — but a pure OBSERVATION must, because recording an
    unanswerable probe as a confident ``False`` (`_verify_shared_gates`'
    ``observe_skipped_proof`` arm) files "the gate would have found changes"
    about a question git never answered.

    ``literal_path`` selects the exact-path form used by
    :func:`path_changed_since`; ``None`` selects the whole-tree form. Both forms
    share this one quiet-diff invocation and the same untracked-fault handling,
    while preserving their established pathspec and baseline-snapshot semantics.

    This is the body BOTH proof arms reach, and by only one route: the
    `proof_of_work_probe` closure in :func:`_verify_shared_gates`, which is what
    actually makes "the observation measures exactly what the gate would have"
    structural. The guarantee is the closure's, not this function's — one closure
    over one `proof_baseline` / `include_untracked_proof` / exclusion set, so the
    gate arm and the observation arm cannot be given different inputs. All this
    body decides is what an unanswerable git call looks like; each arm then reads
    that `None` under its own policy.

    :func:`has_changes_since` and :func:`path_changed_since` are the fail-open
    COLLAPSES of this tri-state — each folds `None` into `True` at its public
    boolean boundary."""
    pathspecs = (
        (f":(literal){literal_path}",)
        if literal_path is not None
        else (".", *_exclude_specs(exclude))
    )
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", *pathspecs)
    if rc not in (0, 1):
        return None
    if rc != 0:
        return True
    if not include_untracked:
        return False
    try:
        created = untracked_files(repo)
    except GitError:
        return None
    if baseline_untracked is not None:
        created -= set(baseline_untracked)
    if literal_path is not None:
        return literal_path in created
    created = {p for p in created if not _path_under_any(p, exclude)}
    return bool(created)


def path_changed_since(
    repo: Path,
    baseline: str,
    rel: str,
    *,
    baseline_untracked: list[str] | None = None,
) -> bool:
    """Whether one literal repo-relative path changed since ``baseline``.

    This is the single-path form of :func:`has_changes_since`: tracked content
    is compared to the recorded commit, while an ordinary untracked path counts
    only when the attempt's baseline snapshot did not already contain it.
    ``baseline_untracked=None`` keeps the proof gate's legacy behavior of
    counting every ordinary untracked path. Ignored paths are absent from
    :func:`untracked_files` and therefore cannot become proof of work here.

    Both a diff refusal and an untracked-enumeration fault fail open toward
    "changed", matching :func:`has_changes_since`. The literal pathspec is
    required for operator-configured ledger paths containing Git wildmatch
    characters. The tri-state body owns that pathspec so this caller cannot drift
    from whole-tree proof handling.
    """
    answer = _changes_since(
        repo,
        baseline,
        literal_path=rel,
        baseline_untracked=baseline_untracked,
    )
    return True if answer is None else answer


def attempt_dirty(
    repo: Path,
    baseline: str,
    baseline_untracked: list[str] | None,
    exclude: tuple[str, ...] = (),
) -> bool:
    """True if a `safe_rollback` to `baseline` would change anything: tracked
    changes since baseline, or untracked files created since the baseline
    snapshot. `baseline_untracked=None` (a pre-snapshot run) means untracked
    files are never this attempt's to remove, so only tracked diff counts. This
    mirrors `safe_rollback`'s notion of what *this attempt* touched, so callers
    can skip a no-op reset/pause when the tree is already at baseline.

    `exclude` is repo-relative posix dir prefixes (e.g. the BMAD artifact
    folders) whose changes are orchestrator-owned and never count as a dev
    attempt's dirtiness — they pair with `safe_rollback`'s `preserve`, so a
    change confined to those folders reads as clean.

    `policy.toml` (the operator's orchestration config) is *always* excluded: it
    is never a dev attempt's change, `safe_rollback` always restores it, and a
    lone policy edit must not read as dirtiness — otherwise the manual-recovery
    loop could never terminate. Mirrors `worktree_clean`'s exclusion."""
    exclude = (POLICY_FILE_REL, *exclude)
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", ".", *_exclude_specs(exclude))
    if rc != 0:
        return True
    if baseline_untracked is None:
        return False
    created = _created_untracked(repo, baseline_untracked)
    created = {p for p in created if not _path_under_any(p, exclude)}
    return bool(created)


def _entry_at_revision(repo: Path, revision: str, rel: str) -> tuple[str, str, str] | None:
    """Return ``(mode, type, oid)`` for one literal path at ``revision``.

    ``ls-tree`` gives absence as an empty successful result while keeping an
    invalid revision or object-database fault as a non-zero command. That
    distinction is load-bearing for recovery: absence is a proven baseline
    ownership state; a Git failure is not authority to reset.
    """
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "ls-tree",
            "-z",
            "--full-tree",
            revision,
            "--",
            *_literal_specs([rel]),
        ],
        repo,
        binary=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git ls-tree {revision[:12]} -- {rel} failed in {repo}: {detail}")
    records = [record for record in proc.stdout.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1 or b"\t" not in records[0]:
        raise GitError(f"git ls-tree returned an ambiguous entry for {rel!r} in {repo}")
    header, path = records[0].split(b"\t", 1)
    if path != os.fsencode(rel):
        raise GitError(f"git ls-tree returned the wrong literal path for {rel!r} in {repo}")
    fields = header.decode("ascii", "strict").split()
    if len(fields) != 3:
        raise GitError(f"git ls-tree returned a malformed entry for {rel!r} in {repo}")
    return fields[0], fields[1], fields[2]


def file_bytes_at_revision(repo: Path, revision: str, rel: str) -> bytes | None:
    """Read one blob byte-exactly from ``revision``.

    ``None`` means the literal path is absent or names a non-blob (for example a
    directory) at that revision. Git command/object failures raise, so recovery
    can distinguish a proven absent baseline file from an unproven observation.
    Symlinks are blobs too; callers that care about path authority separately
    validate the live regular file and its ancestors.
    """
    entry = _entry_at_revision(repo, revision, rel)
    if entry is None or entry[1] != "blob":
        return None
    oid = entry[2]
    proc = _run_git(
        ["git", "-C", str(repo), "cat-file", "blob", oid],
        repo,
        binary=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git cat-file blob {oid[:12]} failed in {repo}: {detail}")
    return proc.stdout


def worktree_file_bytes_at_revision(repo: Path, revision: str, rel: str) -> bytes | None:
    """Materialize one revision blob with the path's working-tree filters.

    Unlike :func:`file_bytes_at_revision`, this applies Git's smudge, EOL, and
    working-tree-encoding conversions. Recovery uses it only when comparing a
    live checkout file to its baseline: on Git for Windows, a byte-exact LF blob
    may legitimately be a CRLF working-tree file under ``core.autocrlf=true``.
    Absence and non-blobs return ``None``; observation failures raise so callers
    cannot mistake an unproven baseline for restoration authority.
    """
    entry = _entry_at_revision(repo, revision, rel)
    if entry is None or entry[1] != "blob":
        return None
    oid = entry[2]
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "--filters",
            f"--path={rel}",
            oid,
        ],
        repo,
        binary=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git cat-file --filters {oid[:12]} for {rel!r} failed in {repo}: {detail}")
    return proc.stdout


def path_has_non_tree_ancestor_at_revision(repo: Path, revision: str, rel: str) -> bool:
    """Whether a parent of ``rel`` is a tracked non-directory at ``revision``.

    Resetting such a baseline can replace a currently real directory with a
    symlink, file, or submodule. A pre-reset canonical child path is therefore no
    longer safe restoration authority after the reset.
    """
    parts = Path(rel).parts
    for end in range(1, len(parts)):
        entry = _entry_at_revision(repo, revision, Path(*parts[:end]).as_posix())
        if entry is not None and entry[1] != "tree":
            return True
    return False


def path_is_non_regular_at_revision(repo: Path, revision: str, rel: str) -> bool:
    """Whether ``rel`` exists at ``revision`` but is not a regular Git file.

    An absent final path is safe for snapshot restoration: reset may remove the
    live file and recovery recreates it. A regular blob (mode ``100644`` or
    ``100755``) is safe for the same reason. Trees, symlinks, gitlinks, and any
    unknown mode would change the meaning of the canonical live path during a
    reset, so recovery must refuse before mutating the checkout.
    """
    entry = _entry_at_revision(repo, revision, rel)
    return entry is not None and (entry[1] != "blob" or entry[0] not in {"100644", "100755"})


def index_path_changed_since(repo: Path, revision: str, rel: str) -> bool:
    """Whether one literal index entry differs from ``revision``.

    This sees index-only ownership mutations that a byte snapshot cannot: a
    failed child force-adding an ignored input, removing a tracked input from the
    index, or staging different content before restoring the working-tree bytes.
    """
    rc, out = _git(
        repo,
        "diff",
        "--cached",
        "--quiet",
        revision,
        "--",
        *_literal_specs([rel]),
    )
    if rc not in (0, 1):
        raise GitError(f"git diff --cached {revision[:12]} -- {rel} failed in {repo}: {out}")
    return rc == 1


def frontmatter_status_at_revision(repo: Path, revision: str, rel: str) -> str | None:
    """Read one tracked file's normalized frontmatter status from ``revision``.

    This is the baseline oracle for attempt-owned lifecycle recovery. The file
    content is read through Git's object database, not the mutable checkout, and
    decoded from bytes so an undecodable historical blob degrades to no usable
    status instead of escaping the git subprocess chokepoint. Missing files,
    malformed YAML, non-mapping frontmatter, and missing/blank statuses likewise
    return ``None``; a caller must not repair from an unproven baseline.
    """
    raw = file_bytes_at_revision(repo, revision, rel)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    split = _split_frontmatter(text)
    if split is None:
        return None
    try:
        doc = yaml.safe_load(split[1])
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or "status" not in doc:
        return None
    status = status_of(doc)
    return status or None


def reset_index_path(repo: Path, revision: str, rel: str) -> None:
    """Restore one literal path's index ownership from ``revision`` only.

    The working-tree file is deliberately left in place. Recovery uses this
    after a preserved-folder checkout when the attempt baseline had no blob at
    ``rel``: the pre-launch ignored/untracked spec must not become a staged add
    merely because a failed child force-added or committed that same name.
    """
    proc = _run_git(
        [
            "git",
            "-C",
            str(repo),
            "reset",
            "--quiet",
            revision,
            "--",
            *_literal_specs([rel]),
        ],
        repo,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git reset {revision[:12]} -- {rel} failed in {repo}: {detail}")


def _exclude_specs(dirs: tuple[str, ...]) -> list[str]:
    """git pathspec `:(exclude,literal)<dir>` args for each repo-relative dir prefix.

    `literal` for the same reason as :func:`_literal_specs` — git reads a positional
    operand as a PATHSPEC, so `[`, `]`, `*` and `?` in an operator-configured dir are
    wildmatch metacharacters — but the harm here runs the other way: an over-matching
    exclusion HIDES a diff instead of exposing a file. `_changes_since` (the
    proof-of-work probe's body, which `has_changes_since` collapses) and
    `attempt_dirty` both spend these on `diff --quiet . :(exclude)<dir>`, so a dir
    whose name carries a `*` excludes a sibling tree as well and the attempt reads
    CLEAN when it changed — the same false "no changes" that a dev attempt's dirtiness
    check exists to prevent (#423 item 3).

    It also realigns this half with :func:`_path_under_any`, the Python `startswith`
    that filters the untracked half of the very same `_changes_since` call. The two
    disagreed on exactly the shapes that glob (#423 item 4): the tracked half excluded
    a path the untracked half still counted, so one function's two branches answered
    differently about what "under the artifact dir" means. Literal is the reading
    `_path_under_any` already implements, which is why fixing the git half is the right
    direction and not a coin flip.

    `*` and `?` both cross `/` here — `FNM_PATHNAME` is opt-in via `:(glob)`, which this
    never sets — but only a `*` reaches a sibling DIRECTORY's contents; a same-length
    `[…]`/`?` collision reaches a sibling FILE only, because the pathspec has to match
    the whole path. Both magic words go in ONE comma-separated `:(...)` prefix — the
    global `--literal-pathspecs` / `GIT_LITERAL_PATHSPECS` form would disarm the
    `:(exclude)` magic too and silently stop excluding anything at all. Stable across
    the supported range, `GIT_FLOOR` to current."""
    return [f":(exclude,literal){d}" for d in dirs]


def _literal_specs(rels: list[str]) -> list[str]:
    """git pathspec `:(literal)<rel>` for each repo-relative posix path — the operand
    form that means "this path", not "this glob". See `path_tracked` for why."""
    return [f":(literal){r}" for r in rels]


def _path_under_any(path: str, prefixes: tuple[str, ...]) -> bool:
    """True if repo-relative posix `path` equals or sits under any `prefixes` dir.

    The literal reading of "under", and since #423 item 4 the one `_exclude_specs`
    agrees with — the two filter the tracked and untracked halves of a single
    `_changes_since` answer and must not disagree."""
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def untracked_files(repo: Path) -> set[str]:
    """Untracked, non-ignored paths (repo-relative posix), mirroring what a
    plain `git clean -fd` (no -x) treats as removable. Ignored files are
    excluded, so they are never rollback candidates.

    NUL-delimited and read VERBATIM (#783), because these names are operands:
    `snapshot_worktree` hands them to `git add --` and the rollback cleanup
    deletes them. The line-based read this replaces got `core.quotePath`'s
    C-quoted spelling of every non-ASCII name — not a path, so the snapshot's
    `add` failed and the rollback refused — and `.strip()` ate edge spaces.
    Bytes rather than text mode, whose universal-newline read turns a `\\r` in a
    name into `\\n`; each record is decoded strictly with the filesystem codec,
    so it names the file `Path` and argv will reach.

    A record that codec cannot decode keeps its pre-#783 answer, the quoted
    spelling (`_c_quote_path`): an inert token that matches itself across the
    baseline and later reads, rather than a GitError that would fail every
    baseline capture in a repo holding one such stray, or surrogates that would
    leak into the state and journal JSON (see `_run_git`).

    stdout ALONE, as before (#442): `ls-files` exits 0 while still writing to
    stderr, and a merged stream would turn that chatter into a phantom path."""
    proc = git_bytes(repo, "ls-files", "-z", "--others", "--exclude-standard")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git ls-files --others failed in {repo}: {detail}")
    return {_decode_git_path(rel) for rel in proc.stdout.split(b"\0") if rel}


# quote.c `cq_lookup`: the bytes git escapes by letter; every other byte it must
# quote is written as a three-digit octal escape.
_CQ_LETTER_ESCAPES = {
    0x07: b"a",
    0x08: b"b",
    0x09: b"t",
    0x0A: b"n",
    0x0B: b"v",
    0x0C: b"f",
    0x0D: b"r",
    0x22: b'"',
    0x5C: b"\\",
}


def _c_quote_path(raw: bytes, *, quote_fully: bool) -> bytes:
    """git's `quote_c_style` of one path, as non-`-z` porcelain prints it.

    Control bytes, DEL, `"` and `\\` always force quoting; bytes >= 0x80 do only
    under ``quote_fully`` (`core.quotePath`, default true). A name needing none
    comes back unchanged, otherwise double-quoted with every such byte escaped."""

    def must_quote(b: int) -> bool:
        return b < 0x20 or b == 0x7F or b in (0x22, 0x5C) or (quote_fully and b >= 0x80)

    if not any(must_quote(b) for b in raw):
        return raw
    out = bytearray(b'"')
    for b in raw:
        if not must_quote(b):
            out.append(b)
        elif b in _CQ_LETTER_ESCAPES:
            out += b"\\" + _CQ_LETTER_ESCAPES[b]
        else:
            out += b"\\%03o" % b
    out += b'"'
    return bytes(out)


def _decode_git_path(raw: bytes) -> str:
    """One `-z` path record as `untracked_files` answers it — see there."""
    try:
        return raw.decode(sys.getfilesystemencoding())
    except UnicodeDecodeError:
        return _c_quote_path(raw, quote_fully=True).decode("ascii")


def _pre_783_spellings(name: str) -> set[str]:
    """Every record the pre-#783 line-based `untracked_files` could have
    persisted for the file ``name``, under either `core.quotePath` setting: the
    C-quoted rendering, split on lines and stripped as that reader did."""
    raw = name.encode(sys.getfilesystemencoding(), "surrogateescape")
    spellings: set[str] = set()
    for quote_fully in (True, False):
        try:
            rendered = _c_quote_path(raw, quote_fully=quote_fully).decode(
                sys.getfilesystemencoding()
            )
        except UnicodeDecodeError:
            continue  # that reader raised on this name, so persisted nothing for it
        spellings.update(r.strip() for r in rendered.splitlines() if r.strip())
    return spellings


def _created_untracked(repo: Path, baseline_untracked: list[str]) -> set[str]:
    """The untracked files this attempt created, for the ROLLBACK side —
    `snapshot_worktree`, `_rollback_cleanup_plan` and the `attempt_dirty` gate
    that mirrors them.

    A path counts only when neither its exact name nor any `_pre_783_spellings`
    of it is in the baseline. A baseline persisted before #783 holds the quoted
    or stripped records of the old reader, so an exact difference alone would
    turn every pre-existing non-ASCII or space-edged file of a resumed run into
    a deletion target. Matching the old spellings is exact where it matters:
    each file's old records ARE its spellings, so every file a legacy baseline
    listed stays protected. The only cost is spurious matches (a new ` a` beside
    a baseline `a`), and those fail toward leaving a file alone, the direction
    the rollback gates fail in. The proof-of-work gate (`_changes_since`) keeps
    the exact difference: it fails open toward "work happened" instead."""
    baseline = set(baseline_untracked)
    return {
        path
        for path in untracked_files(repo) - baseline
        if baseline.isdisjoint(_pre_783_spellings(path))
    }


def path_tracked(repo: Path, rel: str) -> bool:
    """True when repo-relative posix ``rel`` has an index entry — i.e. git OWNS the
    path, so a `reset --hard` restores it and no caller should delete it by hand.

    The single-path complement of :func:`untracked_files`, and the pair is what makes
    the third state legible: neither tracked nor in that set means IGNORED, which no
    rollback step touches at all (`reset --hard` skips it, and this module never runs
    `git clean`). A caller that reasons only over "tracked vs untracked" silently
    files every ignored path under whichever branch it wrote last.

    Only the output's EMPTINESS is read, never its text: `core.quotePath` mangles
    non-ASCII names, and a tracked-but-deleted-from-the-worktree path still lists (the
    index entry outlives the file), which is exactly the state a caller must not
    mistake for "not git's". Not `--error-unmatch`, which reports "not tracked" and
    "git blew up" with the same non-zero rc; not `check-ignore`, which answers whether
    a RULE matches rather than whether git owns the path — a `git add -f`'d file under
    an ignore rule has to read tracked here.

    Reads `stdout` ALONE, not `_git`'s stdout+stderr merge: `ls-files` exits 0 while
    still writing to stderr — a `core.fsmonitor` hook that cannot exec, an unknown
    `core.fsyncMethod` — and against the merged stream that chatter reads as an index
    entry for a path git does not track at all. The failure is silent and inverted
    (untracked answers "tracked"), so callers act on the opposite of the truth. The
    error path keeps the merge, where stderr is the only informative half.

    The pathspec is forced LITERAL, because git reads a positional operand as a
    PATHSPEC and not as a path: `[`, `]`, `*` and `?` in ``rel`` are wildmatch
    metacharacters, so a probe for an ABSENT path answers True the moment some OTHER
    tracked path happens to match the glob. The error is one-directional —
    `match_pathspec_item` compares literally before it falls through to fnmatch, so a
    genuinely tracked path never reads untracked — and it runs toward the answer that
    authorizes leaving a file alone, which is how a harvested ledger under an
    operator-named `implementation_artifacts` (`bmadconfig._resolve` takes that key
    verbatim, metacharacters and all) outlived the rollback that discarded the code it
    described. Not the global `--literal-pathspecs` / `GIT_LITERAL_PATHSPECS` form,
    which would also disarm the `:(exclude)` magic `worktree_clean`, `_changes_since`
    and `attempt_dirty` are built on; the per-operand prefix is scoped to this call. It
    costs the callers nothing: that same literal comparison is what matches a DIRECTORY
    prefix, so `_bmad/render` still lists everything beneath it (`cmd_validate`'s
    render-tracked warning), and it additionally disarms a ``rel`` that itself begins
    with `:`, which git would otherwise parse as magic and answer the empty set for.
    Stable across the supported range, `GIT_FLOOR` to current;
    below a version that understands the prefix it would read as a literal FILENAME,
    match nothing and answer False, which is the one direction that authorizes a
    delete.

    Raises GitError like every other probe in this module. Callers inside a rollback
    `finally` catch it and degrade toward leaving the file alone: uncertainty must
    never authorize a delete. The message keeps the BARE ``rel``: the operator's path is
    its informative half and the magic prefix is our own plumbing.

    Three live callers, all reached through this one chokepoint: `git.render-tracked`
    (`cmd_validate`), `_ledger_is_gits_to_restore` (the harvest revert) and
    `_harvest_carry_commit_may_degrade` (the isolation carry)."""
    proc = _run_git(["git", "-C", str(repo), "ls-files", "--", *_literal_specs([rel])], repo)
    if proc.returncode != 0:
        merged = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git ls-files -- {rel} failed in {repo}: {merged}")
    return bool(proc.stdout.strip())


def path_tracked_kind(repo: Path, rel: str) -> Literal["untracked", "file", "dir"]:
    """Which of three states repo-relative posix ``rel`` holds in the index: absent from
    it, a tracked regular FILE, or a tracked DIRECTORY prefix.

    The distinction :func:`path_tracked` deliberately does not draw. Its literal
    pathspec matches a directory prefix too — that is load-bearing there, which is why
    `_bmad/render` answers True for the whole tree beneath it — so a caller that must
    know *which* of the three it holds cannot get it from that boolean.

    ONE `ls-files` spawn answers all three, because the pathspec's literal comparison is
    itself what separates them: a tracked file lists exactly the name asked for, a
    tracked directory lists the entries BENEATH it (never the directory's own name), and
    a path with no index entry lists nothing. So the empty set is "untracked", the
    singleton `{rel}` is "file", and any other non-empty set is "dir". A D/F-conflicted
    index — one name carrying both a file entry and entries beneath it — therefore
    answers "dir", which degrades toward substituting per-file patterns for what
    provisioning actually wrote: still a shield over our own files, and wrong in the
    spare-a-pattern direction rather than the leaking one.

    The worktree git-add shield is what needs the three apart, because they want three
    different treatments (#392, #484). Measured, git 2.55.0:

    * An exclude pattern naming a tracked regular FILE suppresses NOTHING. git consults
      ignore rules only for untracked paths, so `git add -A` stages a modification to it
      regardless. The pattern's only effect is to make the file answer
      `ls-files -ci --exclude-standard`, i.e. read as tracked-and-ignored — which is a
      state repo-hygiene gates reject, and how a shield meant to keep the orchestrator's
      files OUT of a story commit came to block one instead. The pattern is dropped.
    * The same pattern over a tracked DIRECTORY really does hide new children, and no
      pattern shape keeps that AND clears the `-ci` report: `dir/*`, `dir/**` and a
      trailing negation all measured identical to `dir`, because gitignore cannot
      re-include anything under an excluded parent. That measurement stands; the verdict
      it once carried — keep the dir pattern, accept the report — is REVERSED (#484).
      Over a tracked directory the protection is already mostly inert, since
      modifications to tracked children stage regardless, so the pattern was buying only
      new-child coverage at the price of a false tracked-and-ignored report across the
      whole tree. It is replaced by one pattern per untracked file provisioning wrote.
      The residual — a session-created NEW child under a tracked tool directory can be
      staged — is accepted, and matches the project's own decision to track that tree.
    * An UNTRACKED path keeps its pattern unchanged: full protection, and nothing
      beneath it can answer `-ci` in the first place.

    `-z` and the BYTES accessor, unlike :func:`path_tracked`. This reads the output's
    TEXT rather than only its emptiness, so that function's reason for never looking —
    `core.quotePath` mangling non-ASCII names — becomes this one's problem instead.
    NUL-delimited output is never quoted, and comparing `os.fsencode(rel)` keeps a POSIX
    name that is undecodable in the locale codec comparable rather than raising (#377).

    The pathspec is forced LITERAL for BOTH directions of the metacharacter hazard, not
    just the sibling's one. Reading the text already refuses a glob's false positive on
    an ABSENT ``rel``: the stray match comes back under the NEIGHBOUR'S name, which is
    not the name asked for, so the set differs whatever the pathspec. What needs
    `:(literal)` is the opposite direction — when ``rel`` carries `[`, `]`, `*` or `?`
    and a glob-colliding neighbour is tracked too, a bare pathspec returns BOTH names.
    The set then exceeds the singleton and a genuine tracked FILE reads "dir", so the
    shield substitutes patterns for a tree it never wrote, for any project whose hook
    config or skill tree carries a metacharacter.

    Raises GitError like every other probe in this module; the shield's caller degrades
    by KEEPING the pattern it already holds, since a leaked seed file in a story commit
    is the worse of the two failures."""
    proc = git_bytes(repo, "ls-files", "-z", "--", *_literal_specs([rel]))
    if proc.returncode != 0:
        merged = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git ls-files -z -- {rel} failed in {repo}: {merged}")
    entries = {entry for entry in proc.stdout.split(b"\0") if entry}
    if not entries:
        return "untracked"
    return "file" if entries == {os.fsencode(rel)} else "dir"


def path_tracked_file(repo: Path, rel: str) -> bool:
    """True when repo-relative posix ``rel`` is tracked AND names a regular FILE rather
    than a directory prefix.

    The two-state read of :func:`path_tracked_kind`, which owns the mechanics and the
    doctrine. Kept for `_pin_tracked_config_rewrite` (`worktree_flow`), whose question
    really is yes/no: only a tracked file can carry the skip-worktree bit the pin
    depends on, and both other kinds mean there is nothing to pin. A caller that has to
    tell a tracked DIRECTORY from an untracked path asks the tri-state probe itself."""
    return path_tracked_kind(repo, rel) == "file"


def _blob_oid_for_file(repo: Path, rel: str, path: Path) -> str:
    """The object id git would record for the bytes in ``path``, taken as content for
    repo-relative posix ``rel``.

    `--path=` is what makes ``path`` and ``rel`` separable: it drives the attribute
    lookup, so a file living anywhere — a shadow copy outside the repo included — is
    hashed under the rules that govern ``rel``. Verified load-bearing at git 2.55.0:
    with `board.yaml text eol=crlf`, a CRLF twin named something else hashes to HEAD's
    id with the flag and to a different id without it.

    Raises rather than answering a sentinel. The one caller gates a repair write on the
    comparison, and an id it could not compute must not read as "these differ" any more
    than as "these match"."""
    proc = git_bytes(repo, "hash-object", "-t", "blob", f"--path={rel}", "--", str(path))
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git hash-object --path={rel} for {path} failed in {repo}: {detail}")
    return proc.stdout.decode("ascii", "strict").strip()


def git_normalized_blob_oid(repo: Path, rel: str, path: Path) -> str:
    """Return the blob id Git would stage for ``path`` at literal ``rel``.

    This public seam lets publication binding use the same clean-filter-aware
    identity as the existing content guards without reproducing Git mechanics.
    """
    return _blob_oid_for_file(repo, rel, path)


def git_normalized_blob_oid_for_bytes(repo: Path, rel: str, data: bytes) -> str:
    """Return Git's clean-filter-normalized blob id for a confined byte snapshot."""
    return _blob_oid_for_bytes(repo, rel, data)


def _valid_object_id(value: bytes) -> bool:
    return len(value) in (40, 64) and all(byte in b"0123456789abcdef" for byte in value)


def staged_blob_oids(repo: Path, rels: Iterable[str]) -> dict[str, str]:
    """Read one strict snapshot of stage-zero regular-file blobs for ``rels``.

    Requested paths absent from the index are omitted so callers can distinguish
    accepted ignored paths (which must stay absent) from accepted tracked paths
    (which must be present). Ambiguous, unmerged, malformed, or non-blob index
    evidence raises a path-only ``GitError``. Object ids and Git output are
    deliberately omitted because callers surface this at the publication
    integrity boundary.
    """
    ordered = tuple(dict.fromkeys(rels))
    if not ordered:
        return {}
    try:
        proc = git_bytes(repo, "ls-files", "-s", "-z", "--", *_literal_specs(list(ordered)))
    except (GitError, OSError) as exc:
        raise GitError(f"git index blob probe failed for declared paths in {repo}") from exc
    if proc.returncode != 0:
        raise GitError(f"git index blob probe failed for declared paths in {repo}")
    requested = {os.fsencode(rel): rel for rel in ordered}
    observed: dict[str, str] = {}
    for record in (item for item in proc.stdout.split(b"\0") if item):
        try:
            header, actual_path = record.split(b"\t", 1)
            mode, oid, stage = header.split()
            rel = requested[actual_path]
            oid_text = oid.decode("ascii", "strict")
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise GitError(f"git index evidence is malformed for declared paths in {repo}") from exc
        if (
            stage != b"0"
            or mode not in {b"100644", b"100755"}
            or not _valid_object_id(oid)
            or rel in observed
        ):
            raise GitError(f"git index evidence is not a regular stage-zero blob in {repo}")
        observed[rel] = oid_text
    return observed


def staged_blob_oid(repo: Path, rel: str) -> str:
    """Return the exact regular stage-zero blob id for one literal path."""
    observed = staged_blob_oids(repo, (rel,))
    if rel not in observed:
        raise GitError(f"git index has no exact unambiguous entry for {rel!r} in {repo}")
    return observed[rel]


def revision_blob_oids(repo: Path, revision: str, rels: Iterable[str]) -> dict[str, str]:
    """Read exact regular-file blob identities from one committed tree snapshot."""
    ordered = tuple(dict.fromkeys(rels))
    if not ordered:
        return {}
    try:
        proc = git_bytes(repo, "ls-tree", "-rz", revision, "--", *_literal_specs(list(ordered)))
    except (GitError, OSError) as exc:
        raise GitError(f"git tree blob probe failed for declared paths in {repo}") from exc
    if proc.returncode != 0:
        raise GitError(f"git tree blob probe failed for declared paths in {repo}")
    requested = {os.fsencode(rel): rel for rel in ordered}
    observed: dict[str, str] = {}
    for record in (item for item in proc.stdout.split(b"\0") if item):
        try:
            header, actual_path = record.split(b"\t", 1)
            mode, kind, oid = header.split()
            rel = requested[actual_path]
            oid_text = oid.decode("ascii", "strict")
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise GitError(f"git tree evidence is malformed for declared paths in {repo}") from exc
        if (
            kind != b"blob"
            or mode not in {b"100644", b"100755"}
            or not _valid_object_id(oid)
            or rel in observed
        ):
            raise GitError(f"git tree evidence is not a regular blob in {repo}")
        observed[rel] = oid_text
    return observed


def file_holds_content(repo: Path, rel: str, path: Path, data: bytes) -> bool:
    """Whether the file at ``path`` holds ``data``, as GIT counts sameness for ``rel``.

    Asks git's own question instead of guessing a domain. A byte compare has to know
    which end of the checkin/checkout round trip the file on disk sits at, and there is
    no answer that holds: under `core.autocrlf=true` — Git for Windows' system default,
    which the suite deliberately leaves reachable — a freshly checked-out board is CRLF
    while one an editor or a byte-writing tool left is LF, and git calls the tree clean
    either way. Measured both ways at git 2.55.0: a baseline read raw refuses the CRLF
    checkout, a baseline read through the smudge refuses the LF one, and the two
    failures are the same mistake pointing opposite directions. Hashing both sides
    through the CLEAN filter collapses that distinction, because it is precisely the
    distinction git itself does not draw.

    What it does NOT collapse is content: an operator's added row survives cleaning and
    still answers False, which is the only discrimination the caller wants.

    ``data`` is hashed from a shadow file rather than stdin because the git chokepoint
    spawns without one, and widening it for a single caller would put a stdin path
    through every git call in the module."""
    return _blob_oid_for_file(repo, rel, path) == _blob_oid_for_bytes(repo, rel, data)


def _blob_oid_for_bytes(repo: Path, rel: str, data: bytes) -> str:
    """``_blob_oid_for_file`` for bytes that are not on disk, via a shadow copy — the
    git chokepoint spawns without a stdin, and widening it for one caller would put a
    stdin path through every git call in the module."""
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp) / "intended"
        shadow.write_bytes(data)
        return _blob_oid_for_file(repo, rel, shadow)


def index_holds_no_foreign_content(repo: Path, rel: str, data: bytes) -> bool:
    """Whether the INDEX entry for ``rel`` is safe to overwrite with ``data``.

    Git holds a path in two places and `commit_paths` writes both: `git add` copies the
    WORKING TREE into the commit and overwrites the INDEX in place. A staged version
    distinct from both HEAD and ``data`` therefore exists nowhere afterwards — not in
    the commit, which took the working tree, and not on disk — so proving only the
    working tree is not proving the carry destroys nothing. Measured: with the operator's
    edit staged and the working tree restored, the carry commits, the row is absent from
    HEAD and from disk, and the tree reads clean, the bytes surviving only as a dangling
    blob (#618).

    True when the index holds HEAD's own content, or already holds ``data``, or holds
    no entry for a path HEAD does not carry either — the first loses nothing git cannot
    still reach, the second is the write itself, and the third is empty in both places
    at once. Unmerged stages raise rather than answer: a half-resolved index is not a
    state this can prove anything about.

    An absent entry is NOT by itself "nothing to overwrite", which is why HEAD is
    consulted before accepting one. With HEAD carrying the path, no index entry is a
    staged DELETION — `git rm --cached`, the operator untracking a board they are
    about to gitignore, which is a shape this project documents rather than an exotic
    one — and the carry's `git add` restores the entry, leaving that intent nowhere:
    not in HEAD, which never had it, and not in the index that just lost it. Measured.

    CONTENT is the whole of what this proves, and that ceiling is deliberate.
    ``ls-files -s`` reports the entry's MODE too and this reads only the oid beside
    it, so an operator who stages nothing but an exec-bit flip
    (``update-index --chmod=+x``) leaves the blob identical, is approved here, and has
    that staged mode reset by the carry's ``git add``. 100644/100755 is the only pair
    that can reach it — a symlink entry (120000) carries a different blob, which the
    oid compare already catches — so the exposure is exactly an exec bit on a YAML
    data file that nothing reads and nothing runs. Widening the proof to index
    metadata no workflow here sets would protect nothing and hand the carry one more
    way to refuse an ordinary board. A caller must not read this as "the index entry
    is untouched"; it says "the index holds no content this write would lose".
    """
    proc = git_bytes(repo, "ls-files", "-s", "-z", "--", *_literal_specs([rel]))
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git ls-files -s -- {rel} failed in {repo}: {detail}")
    # Read once, up here: BOTH branches below need HEAD, because whether an index
    # state is safe to overwrite is never answerable from the index alone.
    head = _entry_at_revision(repo, "HEAD", rel)
    head_oid = head[2] if head is not None and head[1] == "blob" else None
    records = [r for r in proc.stdout.split(b"\0") if r]
    if not records:
        return head_oid is None  # empty in both places; otherwise a staged deletion
    if len(records) != 1:
        raise GitError(f"git ls-files -s returned unmerged stages for {rel!r} in {repo}")
    fields = records[0].split(b"\t", 1)[0].decode("ascii", "strict").split()
    if len(fields) != 3:
        raise GitError(f"git ls-files -s returned a malformed entry for {rel!r} in {repo}")
    staged = fields[1]
    return staged in {head_oid, _blob_oid_for_bytes(repo, rel, data)}


def path_ignored(repo: Path, path: Path) -> bool:
    """True when `git add` would REFUSE ``path`` for being ignored — i.e. an ignore
    rule matches it AND git does not already track it (#577).

    Both halves matter, and `check-ignore` answers both in one call: it consults the
    INDEX unless told not to (`--no-index` exists precisely to turn that off), so a
    tracked file under a matching rule reads NOT ignored here — rc 1 — which is
    exactly what `git add` does with it. That asymmetry is the #392 one seen from the
    other side: a rule over a tracked regular file suppresses nothing, because git
    consults ignore rules only for untracked paths. A probe that read the RULE alone
    would answer "ignored" for a `git add -f`'d board and drop it from a commit git
    would have taken. Measured, git 2.55.0.

    Takes an absolute `Path` rather than a repo-relative rel like its two siblings
    above, because its caller holds a CONFIGURED path (`ProjectPaths.sprint_status`,
    resolved out of the operator's `_bmad/bmm/config.yaml`) rather than a git-derived
    relpath, and the answer has to be about the same rel :func:`commit_paths` will
    derive from that same `Path`. Deriving it twice in two modules is how the two
    drift apart. Out-of-repo answers False for the same reason `commit_paths` skips
    such a path: there is nothing here to omit from a commit that will not contain it.

    The one probe in this module that CANNOT force a literal operand: `check-ignore`
    parses pathspec magic and then rejects it outright — `:(literal)x` exits 128 with
    "pathspec magic not supported by this command" (measured, git 2.55.0) — so the
    hardening `path_tracked` documents at length is unavailable. It is also not
    needed for the glob half: gitignore matching makes the PATTERN the wildmatch and
    the pathname a literal, so `[`, `]`, `*` and `?` in ``rel`` are inert (verified
    against a repo holding both `d[a]/b.yaml` and `da/b.yaml` under a `da/` rule —
    the metacharacter path correctly reads not-ignored).

    The leading `./` is what stands in for it, and it is load-bearing: magic is
    recognized only on an operand that STARTS with `:`, so a prefix that is a no-op
    as a path is enough to make git read the whole rel as a pathname. Without it a
    ``rel`` beginning with `:` fails TWO ways, not one, and the quiet way is the
    reason this is not merely cosmetic (both measured, git 2.55.0):

    - UNSUPPORTED magic (`:(literal)`, `:(icase)`, `:!`, `:^`) exits 128, which
      surfaces as `GitError` and degrades to "not ignored" — a gitignored board then
      stays in `confirm`'s operand list and `git add` refuses the WHOLE list with it,
      losing the spec and park-record writes that #577 exists to keep.
    - SUPPORTED magic (`:(top)`, `:/`) is accepted, and git answers about the path
      the magic DENOTES rather than the file named on disk: `:(top)board.yaml` reads
      rc 0 whenever a plain `board.yaml` is ignored, even though nothing matches the
      real file. That is a silent wrong answer in the drop direction — `confirm`
      would omit a perfectly committable board and never commit its advance.

    Verified that `./` costs neither contract above: the index consultation still
    reads a tracked-and-ignored board as not-ignored, and `d[a]/f.md` still reads
    literally beside an ignored `da/`.

    Raises GitError like every other probe in this module. Its caller degrades by
    treating the path as NOT ignored, which keeps it in the commit — the behavior
    before this function existed, and the direction that cannot lose a write."""
    try:
        repo_root = repo.resolve()
        rel = Path(path).resolve().relative_to(repo_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return False
    # `./` disarms pathspec magic on a rel beginning with `:` — see the docstring;
    # it is not decoration. `rel` is already posix-separated and relative, so the
    # prefix is a pure no-op as a path on every platform.
    operand = f"./{rel}"
    proc = _run_git(["git", "-C", str(repo), "check-ignore", "-q", "--", operand], repo)
    # 0 = ignored, 1 = not; anything else is git failing rather than answering, and
    # `-q` means there is no output to misread either way.
    if proc.returncode not in (0, 1):
        merged = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git check-ignore -- {operand} failed in {repo}: {merged}")
    return proc.returncode == 0


def commits_above(repo: Path, baseline: str, revision: str = "HEAD") -> list[str]:
    """Commit shas reachable from ``revision`` but not from ``baseline`` — the
    commits an attempt added on top of its pre-attempt baseline, in ``git rev-list``
    order (do not assume a strict newest-first ordering across merges or clock skew;
    callers that need the tip should resolve it directly). Empty when the revision
    is at or behind baseline. Raises GitError on a git failure (a bad baseline is a
    real error, never quietly "no commits").

    Reads stdout ALONE (`_git_out`): git exits 0 while still warning on stderr, and
    against the merged stream that warning is a phantom sha handed to
    :func:`preserve_commits` — "empty when the revision is at/below baseline" stops
    holding on any host whose git config warns (#442)."""
    rc, out, detail = _git_out(repo, "rev-list", f"{baseline}..{revision}")
    if rc != 0:
        raise GitError(f"git rev-list {baseline}..{revision} failed in {repo}: {detail}")
    return [line for line in out.splitlines() if line]


def preserve_commits(
    repo: Path,
    baseline: str,
    ref_name: str,
    commits: list[str] | None = None,
    *,
    revision: str = "HEAD",
) -> str | None:
    """Park the commits an attempt made above ``baseline`` under a branch at ``revision``
    so a following ``git reset --hard baseline`` cannot orphan them — they survive
    `git gc` and are recoverable by name, not just via the reflog. Returns
    ``ref_name`` on success; ``None`` when there is nothing to preserve (the
    revision is at/below baseline). Creation failures raise, so the caller must
    refuse to reset rather than silently destroy committed work. ``-f`` because a
    retry within the same run may re-preserve the same tip under the same name.

    ``commits`` lets a caller that already ran :func:`commits_above` pass the result
    in to skip a second ``git rev-list`` subprocess; ``None`` self-fetches (keeps the
    helper standalone/testable).

    ``None`` means *nothing to preserve* — never a failure. If commits exist but the
    branch cannot be created this raises :class:`GitError` (consistent with the rest
    of this module), so a caller can never mistake a preservation failure for a
    harmless no-op and reset past committed work."""
    if commits is None:
        commits = commits_above(repo, baseline, revision)
    if not commits:
        return None
    rc, out = _git(repo, "branch", "-f", ref_name, revision)
    if rc != 0:
        raise GitError(f"git branch -f {ref_name} {revision} failed in {repo}: {out}")
    return ref_name


class PrunePreserveError(GitError):
    """Partial :func:`prune_preserve_refs` / :func:`prune_preserve_dirty_refs`
    failure. The prune is per-ref best-effort, so refs may already be gone when
    a later one sticks — the ``deleted`` list keeps that destructive half
    structurally auditable (a caller can journal it, not just grep the message)
    and ``failed`` names each stuck ref with its git detail."""

    def __init__(self, message: str, *, deleted: list[str], failed: list[str]) -> None:
        super().__init__(message)
        self.deleted = deleted
        self.failed = failed


def _prune_refs(
    repo: Path,
    keep: int,
    prefix: str,
    *,
    label: str,
    strip: str,
    delete: Callable[[str], None],
) -> list[str]:
    """Shared retention loop behind the per-family pruners: list the refs under
    ``prefix``, keep the ``keep`` newest by committer date, best-effort delete
    the tail via ``delete``, and return the deleted names. ``strip`` is removed
    from each refname before it is deleted/reported (``refs/heads/`` for the
    branch family, nothing for bare refs). ``keep <= 0`` means "never prune" —
    returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` — after attempting every tail ref — when any
    individual delete failed. One stuck ref must not wedge the retention for
    everything behind it, so deletes are per-ref best-effort and the error
    carries both what was deleted and what was not.

    Reads stdout ALONE (`_git_out`): `for-each-ref` exits 0 while still warning on
    stderr, and against `_git`'s merged stream that warning enters ``refs``, lands
    in the ``refs[keep:]`` tail and is handed to ``delete`` — which fails, so
    retention raises :class:`PrunePreserveError` on every host whose git config
    warns (#442)."""
    if keep <= 0:
        return []
    rc, out, detail = _git_out(
        repo,
        "for-each-ref",
        # ties on committerdate (same-second rollbacks) break by ascending
        # refname — an explicit, observable order rather than git's implicit
        # stable-sort fallback. Last --sort key is the primary one.
        "--sort=refname",
        "--sort=-committerdate",
        # full refname, not :short — a tag or remote ref sharing the name would
        # make :short emit an ambiguous form the deleter can't use
        "--format=%(refname)",
        prefix,
    )
    if rc != 0:
        raise GitError(f"git for-each-ref {label} failed in {repo}: {detail}")
    refs = [line.removeprefix(strip) for line in out.splitlines() if line]
    deleted: list[str] = []
    failed: list[str] = []
    for name in refs[keep:]:
        try:
            delete(name)
        except Exception as exc:  # a git timeout/OSError on one ref
            # must not wedge the tail behind it any more than a GitError does; the
            # per-ref best-effort contract holds for the whole subprocess surface
            failed.append(f"{name} ({exc})")
            continue
        deleted.append(name)
    if failed:
        raise PrunePreserveError(
            f"{label} prune in {repo}: deleted {deleted or 'nothing'}, "
            f"could not delete {'; '.join(failed)}",
            deleted=deleted,
            failed=failed,
        )
    return deleted


def prune_preserve_refs(repo: Path, keep: int) -> list[str]:
    """Bounded retention for the ``attempt-preserve/*`` recovery branches that
    :func:`preserve_commits` parks before an auto-rollback reset: keep the
    ``keep`` most recent refs by committer date, force-delete the rest, and
    return the deleted branch names (empty when nothing is over budget). Only
    ``refs/heads/attempt-preserve/`` is ever listed, so branches outside that
    prefix and the ``refs/attempt-preserve-dirty/*`` snapshot refs are
    untouchable by construction — but the prefix itself is owned by the pruner:
    anything parked under it, however it got there, is subject to deletion.
    ``keep <= 0`` means "never prune" — returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` — after attempting every tail ref — when any
    individual delete failed (e.g. the ref is checked out here or in a
    worktree); see :func:`_prune_refs` for the best-effort contract."""
    return _prune_refs(
        repo,
        keep,
        "refs/heads/attempt-preserve/",
        label="attempt-preserve",
        strip="refs/heads/",
        delete=lambda name: delete_branch(repo, name, force=True),
    )


def prune_preserve_dirty_refs(repo: Path, keep: int) -> list[str]:
    """Bounded retention for the ``refs/attempt-preserve-dirty/*`` worktree
    snapshots that :func:`snapshot_worktree` parks before an auto-rollback
    reset: keep the ``keep`` most recent by committer date (the snapshot
    commit's committer date is its park time), delete the rest via
    ``git update-ref -d``, and return the deleted names. These refs live
    outside ``refs/heads/`` — they are not branches, so ``branch -D`` cannot
    touch them and the reported names are full refnames (there is no
    ``refs/heads/`` to strip). Only ``refs/attempt-preserve-dirty/`` is ever
    listed, so branches and every other ref are untouchable by construction.
    ``keep <= 0`` means "never prune" — returns ``[]`` without running git.

    Raises :class:`GitError` when the listing fails, or
    :class:`PrunePreserveError` on a partial delete failure; see
    :func:`_prune_refs` for the best-effort contract."""

    def _delete(refname: str) -> None:
        rc, out = _git(repo, "update-ref", "-d", refname)
        if rc != 0:
            raise GitError(f"git update-ref -d {refname} failed in {repo}: {out}")

    return _prune_refs(
        repo,
        keep,
        "refs/attempt-preserve-dirty/",
        label="attempt-preserve-dirty",
        strip="",
        delete=_delete,
    )


def snapshot_worktree(
    repo: Path,
    ref_name: str,
    *,
    baseline_untracked: list[str] | None,
    force_include: tuple[str, ...] = (),
) -> str | None:
    """Park the current *uncommitted* working-tree state — tracked edits/deletions
    AND run-created untracked files — under ``ref_name`` as a commit object, so a
    following ``git reset --hard`` (whose post-reset cleanup in
    :func:`safe_rollback` also deletes run-created untracked files) cannot
    silently destroy an attempt's in-progress work. The snapshot survives
    ``git gc`` and is recoverable by name (``git checkout <ref> -- .`` or
    ``git diff HEAD <ref>``).

    Captured through a throwaway temp index so the real index and working tree
    are left untouched: seed the temp index from HEAD, ``add -u`` the tracked
    edits/deletions, then stage only the untracked files *this run* created —
    ``untracked_files(repo)`` minus ``baseline_untracked`` (the snapshot taken
    when the baseline was captured; :func:`_created_untracked`). This mirrors :func:`safe_rollback`'s scope
    exactly: the snapshot holds precisely what the reset would destroy and never
    a pre-existing user untracked file. When ``baseline_untracked`` is ``None`` (a
    pre-upgrade/resumed run with no snapshot) no untracked file is staged — matching
    :func:`safe_rollback`, which then deletes none — so tracked edits are still
    parked but untracked files are left untouched. Ignored files are excluded
    throughout unless a caller supplies a trusted literal ``force_include`` path.
    Recovery uses that narrow exception for an attempt-bound spec whose durable
    byte snapshot proves the child changed a baseline-untracked or ignored file;
    ``git add -f`` then parks the child bytes before recovery restores the input.
    The caller must validate those paths as trusted regular files first. A tree is
    written and ``commit-tree``'d parented at HEAD
    under a synthetic ``bmad-loop`` identity so the snapshot commit succeeds even
    when no local/global git ``user.name``/``user.email`` is configured, then
    ``ref_name`` is pointed at the result. Compares only against HEAD — committed
    work above baseline is already parked by :func:`preserve_commits`, so this
    captures exactly what is not yet committed.

    Returns ``ref_name`` on success, or ``None`` when the tree is clean relative
    to HEAD (nothing to preserve — the intended non-destructive uncommitted-revert
    case). Raises :class:`GitError` on any git failure — the raise *surfaces* the
    capture failure so the caller can decide. Since #340 the recovery ref is a
    gate, not a safety net: the worktree caller's old best-effort "journal the
    failure and proceed" contract is gone, and a plain rollback now refuses to
    reset past work it could not park (only a re-drive, whose caller contract
    forbids pausing, still journals and lets the human-directed reset run).

    The three reads whose text IS the answer — ``write-tree``,
    ``rev-parse <head>^{tree}`` and ``commit-tree`` — take stdout ALONE
    (``_git_out``, #442). Git exits 0 while still warning on stderr, and against
    the merged stream all three answer a warning-suffixed "sha": the
    ``tree == head_tree`` comparison then reads unequal on a tree identical to
    HEAD, and ``update-ref`` is handed a non-ref. Because of the gate above, that
    leaves a host whose git config warns with no working rollback at all. The
    rc-only ``_git_env`` calls stay on the merge — they spend their output on a
    raise and never read it as a value.

    Not every failure here is a :class:`GitError`: spawn faults arrive typed as
    :class:`GitSpawnError` since #343, but the ``TemporaryDirectory`` below can
    raise a plain ``OSError`` outright (ENOSPC/EMFILE) — a filesystem fault no
    git chokepoint can translate. Callers keep guarding ``(GitError, OSError)``."""
    head = rev_parse_head(repo)
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
        for args in (("read-tree", head), ("add", "-u")):
            rc, out = _git_env(repo, *args, env=env)
            if rc != 0:
                raise GitError(f"git {args[0]} (snapshot) failed in {repo}: {out}")
        # None baseline (pre-upgrade/resumed run, no snapshot): safe_rollback deletes
        # no untracked files, so park none either — coercing None to [] would instead
        # stage every current untracked file, including the user's pre-existing ones.
        if baseline_untracked is None:
            new: list[str] = []
        else:
            new = sorted(_created_untracked(repo, baseline_untracked))
        if new:
            rc, out = _git_env(repo, "add", "--", *new, env=env)
            if rc != 0:
                raise GitError(f"git add (snapshot untracked) failed in {repo}: {out}")
        if force_include:
            rc, out = _git_env(
                repo,
                "add",
                "-f",
                "--",
                *_literal_specs(list(force_include)),
                env=env,
            )
            if rc != 0:
                raise GitError(f"git add (snapshot forced path) failed in {repo}: {out}")
        rc, tree, detail = _git_out(repo, "write-tree", env=env)
        if rc != 0:
            raise GitError(f"git write-tree (snapshot) failed in {repo}: {detail}")
    rc, head_tree, detail = _git_out(repo, "rev-parse", f"{head}^{{tree}}")
    if rc != 0:
        raise GitError(f"git rev-parse {head}^{{tree}} failed in {repo}: {detail}")
    if tree == head_tree:
        return None  # working tree identical to HEAD — nothing uncommitted to park
    # A synthetic identity (merged over os.environ) so the snapshot commit succeeds
    # with no git user.name/user.email configured — else the best-effort caller would
    # catch the GitError and reset past the very work this ref exists to preserve.
    ident = {
        **os.environ,
        "GIT_AUTHOR_NAME": "bmad-loop",
        "GIT_AUTHOR_EMAIL": "bmad-loop@localhost",
        "GIT_COMMITTER_NAME": "bmad-loop",
        "GIT_COMMITTER_EMAIL": "bmad-loop@localhost",
    }
    rc, snap, detail = _git_out(
        repo, "commit-tree", tree, "-p", head, "-m", "attempt worktree snapshot", env=ident
    )
    if rc != 0:
        raise GitError(f"git commit-tree (snapshot) failed in {repo}: {detail}")
    rc, out = _git(repo, "update-ref", ref_name, snap)
    if rc != 0:
        raise GitError(f"git update-ref {ref_name} {snap[:12]} failed in {repo}: {out}")
    return ref_name


class PreserveRefExhaustedError(GitError):
    """Every candidate snapshot refname in a probe's bounded range was already
    taken. A GitError so the preservation handlers that already guard
    ``(GitError, OSError)`` degrade instead of crashing; a distinct type so the
    caller can tell "the namespace is full" from "git said no" — the two want
    different remedies (prune the namespace, or re-enable pruning by setting
    ``scm.preserve_keep`` to a positive value, vs. fix the repo). Note the
    remedy is *not* "lower ``preserve_keep``": 0 means never prune, so the
    setting most likely to exhaust a probe is the one that cannot go lower.

    Raised rather than falling through to the last candidate on purpose: reusing
    an occupied name is the exact data loss the probe exists to prevent (#349)."""


def ref_exists(repo: Path, refname: str) -> bool:
    """Whether ``refname`` — a FULL refname, e.g. ``refs/attempt-preserve-dirty/…``
    — currently exists. Sibling of :func:`branch_exists`, which prepends
    ``refs/heads/`` and so cannot see the snapshot refs that live outside it.

    A non-zero exit reads as "absent" — `show-ref --verify` returns 1 for both a
    missing ref and a malformed name, and the caller's subsequent ref write
    surfaces any real error. Spawn and timeout faults are NOT swallowed: they
    arrive typed from `_run_git` as GitError/GitSpawnError and propagate, so a
    caller that must not overwrite an existing ref cannot mistake "git could not
    run" for "the name is free"."""
    rc, _ = _git(repo, "show-ref", "--verify", "--quiet", refname)
    return rc == 0


@dataclass(frozen=True)
class _RollbackCleanupTarget:
    """One canonical, confined untracked path and its canonical prune bounds."""

    path: Path
    prune_start: Path
    prune_stop: Path


@dataclass(frozen=True)
class _RollbackCleanupPlan:
    """Canonical cleanup inputs computed before rollback mutates the checkout."""

    repo_root: Path | None
    keep_roots: tuple[Path, ...]
    targets: tuple[_RollbackCleanupTarget, ...]


def _rollback_cleanup_plan(
    repo: Path,
    *,
    baseline_untracked: list[str] | None,
    keep: tuple[str, ...],
) -> _RollbackCleanupPlan:
    """Resolve every later cleanup operand before the rollback mutation boundary."""
    if baseline_untracked is None:
        return _RollbackCleanupPlan(repo_root=None, keep_roots=(), targets=())

    created = _created_untracked(repo, baseline_untracked)
    try:
        repo_root = repo.resolve()
        keep_roots = tuple((repo_root / rel).resolve() for rel in keep)
        targets: list[_RollbackCleanupTarget] = []
        for rel in sorted(created):
            path = (repo_root / rel).resolve()
            # A created path reached through a symlinked parent can canonicalize
            # outside the checkout. Never turn that uncertainty into an external
            # deletion; the rollback cleanup is confined to descendants of root.
            if path == repo_root or not path.is_relative_to(repo_root):
                continue
            if any(path == root or path.is_relative_to(root) for root in keep_roots):
                continue
            targets.append(
                _RollbackCleanupTarget(
                    path=path,
                    prune_start=path.parent,
                    prune_stop=repo_root,
                )
            )
    except (OSError, RuntimeError) as e:
        raise RollbackPreflightError(
            f"cannot preflight rollback cleanup paths safely in {repo}: {e}"
        ) from e
    return _RollbackCleanupPlan(
        repo_root=repo_root,
        keep_roots=keep_roots,
        targets=tuple(targets),
    )


def safe_rollback(
    repo: Path,
    baseline: str,
    *,
    baseline_untracked: list[str] | None,
    keep: tuple[str, ...] = (".bmad-loop",),
    preserve: tuple[str, ...] = (),
) -> None:
    """Undo a failed attempt WITHOUT a blanket `git clean`.

    Reverts tracked changes to `baseline` (the dev attempt's commits/edits),
    then removes only untracked files that appeared since `baseline` — i.e.
    files this run created. Untracked files already present at baseline, every
    ignored file, and anything under a `keep` dir are preserved. The orchestrator
    therefore never runs `git clean -fd`, so it can't eat a user's pre-existing
    untracked work. `baseline_untracked` is the snapshot taken when the baseline
    was captured; None (a pre-upgrade run with no snapshot) removes nothing.

    `preserve` is repo-relative posix dir prefixes (the BMAD artifact folders)
    whose *tracked* content must survive the hard reset — e.g. a frozen spec the
    resolve workflow just corrected, or a sentinel it deliberately deleted. The
    `git reset --hard` would otherwise revert them (keep only guards untracked
    deletion). We snapshot the current tree with `git stash create`, enumerate
    tracked deletions inside the preserved prefixes, reset, then restore the
    snapshot's present paths and replay its deletions. If the snapshot or deletion
    inventory cannot be read we raise *before* the reset rather than proceed with
    an incomplete restore: that would revert exactly what `preserve` names, and it
    would do so silently. Untracked artifacts need no special handling: the reset
    leaves them alone and the cleanup below skips `keep` dirs.

    `policy.toml` (the operator's orchestration config) is *always* restored,
    regardless of `preserve`. It lives inside the kept `.bmad-loop` dir but is
    *tracked*, so a plain `git reset --hard` would silently revert it — an
    uncommitted edit (e.g. a freshly enabled `scm.rollback_on_failure`, gone
    before it ever takes effect) or a change committed after `baseline`. `keep`
    only guards untracked deletion, not tracked reverts. We can't ride the stash
    snapshot for it: `git stash create` emits an empty snapshot for a clean tree,
    so a policy change living in a *commit* (with no other working-tree dirt)
    would skip the restore and be lost. Instead we read policy.toml's on-disk
    content before the reset and write it straight back after — independent of
    the snapshot, covering both the uncommitted and committed cases.

    Before either stash creation or reset, every path needed by the later
    untracked cleanup is canonicalized into a confined plan, including its prune
    bounds. Resolution uncertainty raises ``RollbackPreflightError`` with the
    filesystem fault as its cause, so the caller can pause while the tree is
    untouched; the post-reset cleanup consumes the plan without resolving again.
    """
    # policy.toml: capture on-disk content now, restore unconditionally below.
    policy_path = repo / POLICY_FILE_REL
    policy_content = policy_path.read_bytes() if policy_path.is_file() else None
    cleanup = _rollback_cleanup_plan(
        repo,
        baseline_untracked=baseline_untracked,
        keep=keep,
    )

    rc, out, detail = _git_out(repo, "stash", "create")
    # A failed `stash create` silently empties `snapshot`, which disables the whole
    # preserve restore below — the reset would then revert the very paths the caller
    # asked to keep (a resolved re-drive's corrected spec), with no error anywhere.
    # Raise before the reset, and only when a restore was actually requested: with
    # no `preserve` the snapshot is unused, so the degrade stays correct there (both
    # sweep callers rely on it). A clean tree is not a failure — it exits rc 0 with
    # empty output, which the line below keeps handling as "nothing to restore from".
    # Reading stdout ALONE is what makes that last sentence true on a host whose git
    # warns at rc 0 (#442): against the merge the warning IS the "snapshot", so the
    # restore below ran `checkout warning:… -- <dir>` after the reset had already
    # destroyed the preserved content — destructive first, then loud.
    if rc != 0 and preserve:
        raise GitError(f"git stash create failed in {repo}: {detail}")
    snapshot = out if rc == 0 else ""
    deleted_preserve_paths: tuple[str, ...] = ()
    if snapshot and preserve:
        proc = _run_git(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--no-renames",
                "--name-only",
                "--diff-filter=D",
                "-z",
                baseline,
                snapshot,
                "--",
                *_literal_specs(list(preserve)),
            ],
            repo,
            binary=True,
        )
        if proc.returncode != 0:
            detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
            raise GitError(
                f"git diff {baseline[:12]}..{snapshot[:12]} for preserved deletions "
                f"failed in {repo}: {detail}"
            )
        deleted_preserve_paths = tuple(
            os.fsdecode(path) for path in proc.stdout.split(b"\0") if path
        )
        if deleted_preserve_paths:
            # A blob-to-tree replacement is reported as deletion of the baseline
            # blob plus additions below that same path. The later snapshot
            # checkout already installs the replacement tree; replaying the blob
            # deletion with `git rm -f` would then fail because recursive removal
            # was neither intended nor authorized. Ask the snapshot which deleted
            # names still exist there (as a tree or another replacement object)
            # and leave those exact paths to checkout. This inventory is also
            # pre-reset so an observation failure remains non-destructive.
            proc = _run_git(
                [
                    "git",
                    "-C",
                    str(repo),
                    "ls-tree",
                    "--name-only",
                    "-z",
                    snapshot,
                    "--",
                    *_literal_specs(list(deleted_preserve_paths)),
                ],
                repo,
                binary=True,
            )
            if proc.returncode != 0:
                detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
                raise GitError(
                    f"git ls-tree {snapshot[:12]} for preserved replacements "
                    f"failed in {repo}: {detail}"
                )
            snapshot_replacements = frozenset(
                os.fsdecode(path) for path in proc.stdout.split(b"\0") if path
            )
            deleted_preserve_paths = tuple(
                path for path in deleted_preserve_paths if path not in snapshot_replacements
            )
    rc, out = _git(repo, "reset", "--hard", baseline)
    if rc != 0:
        raise GitError(f"git reset --hard {baseline} failed: {out}")
    if snapshot:
        # Restore each preserve dir's pre-reset content from the snapshot tree. A
        # path with no tracked content in the snapshot makes `git checkout` exit
        # non-zero ("pathspec did not match") — benign (a preserve dir holding
        # only untracked files). Any other failure means a protected path wasn't
        # restored: raise instead of silently dropping a resolved re-drive's
        # corrected spec (which would regress the re-drive into a recovery loop).
        # `_run_git` pins LC_ALL=C, so this English substring is stable under a
        # localized git (#236) — never translated out from under the match.
        #
        # The operand is LITERAL (#423 item 5). `preserve` carries operator-configured
        # dirs, and this is a WRITE: a glob-matching neighbour is not merely restored
        # alongside the target, it is reverted to the snapshot — measured, a plain
        # `checkout <snap> -- 'doc*'` reverts an unrelated `docsa/f.md` edit, which is
        # the operator's own uncommitted work destroyed by a step whose entire job is
        # to preserve. `:(literal)` still matches a directory prefix, so each preserve
        # dir restores everything beneath it exactly as before.
        for d in preserve:
            rc, out = _git(repo, "checkout", snapshot, "--", *_literal_specs([d]))
            # Deliberately still the MERGED read (#442), unlike the `stash create`
            # above: this inspects text on the ERROR path, where git's "did not
            # match" lands on STDERR — stdout alone can never contain it, so the
            # substring would stop matching and a benign empty preserve dir would
            # raise. INVERSE ablation: convert this `_git` to `_git_out` and read
            # the stdout half here, and `test_safe_rollback_tolerates_empty_preserve_dir`
            # fails on an unexpected GitError.
            if rc != 0 and "did not match" not in out:
                raise GitError(f"git checkout {snapshot[:12]} -- {d} failed: {out}")
        # `checkout <tree> -- <dir>` writes every path PRESENT in that tree but
        # does not remove a baseline path ABSENT from it. Replay those exact,
        # pre-reset-inventoried deletions so preservation reproduces the snapshot
        # rather than resurrecting a deliberately cleared sentinel. The operands
        # remain literal for the same reason as the checkout above.
        if deleted_preserve_paths:
            proc = _run_git(
                [
                    "git",
                    "-C",
                    str(repo),
                    "rm",
                    "-f",
                    "--ignore-unmatch",
                    "--",
                    *_literal_specs(list(deleted_preserve_paths)),
                ],
                repo,
                binary=True,
            )
            if proc.returncode != 0:
                detail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
                raise GitError(f"git rm of preserved snapshot deletions failed in {repo}: {detail}")
    if policy_content is not None:
        current = policy_path.read_bytes() if policy_path.is_file() else None
        if current != policy_content:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic, and by NAME (#379). This is a put-back after a rollback has
            # already discarded the run's work, so a torn write costs the operator
            # their orchestration config on top of it — and a truncated policy.toml
            # is not a smaller config but a parse error the next `bmad-loop run`
            # refuses on, which is the failure the whole restore exists to avoid.
            # Refusing to follow a link was a real change here (a bare
            # `write_bytes` opens the name and so writes THROUGH one), and it is
            # the right one twice over: `policy.write_mux_backend` already replaces
            # this same file by name, so no link at this path survives the
            # orchestrator anyway; and `runsetup` states a driven session can write
            # `.bmad-loop/policy.toml`, so honouring a link planted there would aim
            # a host-side write at a path of that session's choosing. Confined to
            # `repo` (#593) because that refusal stopped at the final component:
            # `policy_path` is built lexically from `repo` at the capture above, so
            # the walk re-derives exactly the components that join was spelled
            # from, and a link planted at `.bmad-loop/` no longer redirects the
            # restore out of the repo. require_writable_target (#597) gives back
            # the PermissionError a bare `write_bytes` raised on an operator's
            # read-only policy.toml — this is their config, not machine state.
            atomic_write_bytes_confined(
                policy_path,
                policy_content,
                confine_root=repo,
                require_writable_target=True,
            )
    for target in cleanup.targets:
        try:
            target.path.unlink(missing_ok=True)
        except OSError:
            continue
        _prune_empty_parents(target.prune_start, target.prune_stop)


def _prune_empty_parents(start: Path, repo: Path) -> None:
    """Prune canonical parents supplied by the pre-mutation cleanup plan."""
    d = start
    while d != repo and d.is_relative_to(repo):
        try:
            d.rmdir()  # succeeds only when empty
        except OSError:
            break
        d = d.parent


# --------------------------------------------------------------------------
# git worktree / branch / merge / diff primitives (Phase 2)
#
# Low-level helpers for the worktree-isolation pipeline. Each raises GitError
# on failure. No engine wiring yet — these are unit-tested in isolation and
# wired into open/close_unit_workspace + merge-back in Phase 3.
# --------------------------------------------------------------------------


def current_branch(repo: Path) -> str:
    """The branch name HEAD points at, or "HEAD" when detached. Reads stdout alone
    (`_git_out`): git exits 0 while still warning on stderr, and the merged stream
    would answer a branch name with the warning appended (#442)."""
    rc, out, detail = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        raise GitError(f"git rev-parse --abbrev-ref HEAD failed in {repo}: {detail}")
    return out


def branch_exists(repo: Path, name: str) -> bool:
    rc, _ = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    return rc == 0


def branch_checkout_path(repo: Path, branch: str) -> Path | None:
    """The worktree that has ``refs/heads/<branch>`` checked out, or ``None``.

    ``git for-each-ref --format=%(worktreepath)`` (git 2.23; the support floor is
    2.34) prints the registered path of the worktree whose HEAD is attached to the
    ref — the MAIN checkout's path when the main checkout holds it — and an empty
    line when no worktree has it attached (a detached HEAD at the same commit does
    not count). A ref that does not exist also prints nothing; callers that need
    the distinction check `branch_exists` first. The path is git's registered
    spelling, un-canonicalized: compare it the way the caller compares its own.
    Reads stdout alone (`_git_raw_out`): the value is the answer (#442).

    That "un-canonicalized" promise is why this reader does NOT go through `_git_out`,
    which returns `stdout.strip()`. A worktree registered at a path with TRAILING
    WHITESPACE — `<mount> ` — came back stripped to `<mount>`, which compares EQUAL to
    a unit's own mount path, so the occupancy guard exempted a foreign checkout as if
    it were the unit's own. The ref then moved under a live foreign worktree, its tree
    went spuriously dirty, and `worktree add` failed anyway: exactly the harm the guard
    exists to prevent, WITH the guard present. The error can only go that unsafe way —
    `safe_segment` rstrips `". "` from every segment we compose, so our own mount path
    can never end in whitespace and a spurious REFUSE is unreachable.

    Only the single trailing `\n` that `for-each-ref` frames each record with is
    removed, never arbitrary whitespace; an empty answer (`""` or a bare `"\n"`) still
    means "no worktree has it attached" and returns `None`.

    Accepted bound: `_run_git` runs with `text=True` (universal newlines), so a
    registered path ending in `\r` arrives already translated and stays
    indistinguishable from one that does not. Closing that needs a bytes read, which is
    out of scope here.
    """
    rc, out, detail = _git_raw_out(
        repo, "for-each-ref", "--format=%(worktreepath)", f"refs/heads/{branch}"
    )
    if rc != 0:
        raise GitError(f"git for-each-ref refs/heads/{branch} failed in {repo}: {detail}")
    path = out.removesuffix("\n")
    return Path(path) if path else None


def create_branch(repo: Path, name: str, base: str) -> None:
    """Create branch `name` at `base` without checking it out."""
    rc, out = _git(repo, "branch", name, base)
    if rc != 0:
        raise GitError(f"git branch {name} {base} failed in {repo}: {out}")


def delete_branch(repo: Path, name: str, force: bool = False) -> None:
    rc, out = _git(repo, "branch", "-D" if force else "-d", name)
    if rc != 0:
        raise GitError(f"git branch -d {name} failed in {repo}: {out}")


def reset_branch_if_tip(repo: Path, name: str, revision: str, expected_tip: str) -> None:
    """Move a branch to a pinned revision only while its tip is unchanged.

    ``git update-ref <ref> <new> <old>`` is the compare-and-swap primitive: a
    concurrently advanced branch makes the command fail rather than losing the
    rival commit. The caller resolves both shas before destructive follow-up.
    """
    ref = f"refs/heads/{name}"
    # A refs/heads name can itself be symbolic.  The default update-ref behavior
    # dereferences it, which could reset the target branch (including main) instead
    # of the attempt-local story ref.  --no-deref replaces that name itself while
    # preserving the expected-old CAS for ordinary and symbolic refs.
    rc, out = _git(repo, "update-ref", "--no-deref", ref, revision, expected_tip)
    if rc != 0:
        raise GitError(f"git update-ref {ref} {revision} {expected_tip} failed in {repo}: {out}")


def worktree_add(
    repo: Path, path: Path, branch: str, base: str | None = None, *, create: bool = True
) -> None:
    """Check `branch` out in a new worktree at `path` (which must not exist).

    create=True (default) cuts a fresh `branch` at `base`, or from HEAD when
    `base` is None (git's own default start-point). create=False mounts an
    existing `branch` (used to re-mount a shared run branch across serial units);
    `base` is ignored. Either way the branch must not already be checked out in
    another worktree — git refuses that.
    """
    if create:
        # `git worktree add -b <branch> <path> [<base>]`: cut the new branch at the
        # caller's start-point, or from HEAD when none is given (git's own default).
        cmd = ["worktree", "add", "-b", branch, str(path)]
        if base is not None:
            cmd.append(base)
        rc, out = _git(repo, *cmd)
    else:
        rc, out = _git(repo, "worktree", "add", str(path), branch)
    if rc != 0:
        raise GitError(f"git worktree add {path} ({branch} from {base}) failed: {out}")


def checkout_branch(repo: Path, name: str) -> None:
    """Switch the repo's checkout to `name`. Requires a clean tree."""
    rc, out = _git(repo, "checkout", name)
    if rc != 0:
        raise GitError(f"git checkout {name} failed in {repo}: {out}")


def checkout_detach(repo: Path) -> None:
    """Detach HEAD at its current commit, leaving working tree + index untouched.

    Frees a shared branch name held by a kept worktree so a sibling worktree can
    check that branch out (git refuses a branch checked out in another worktree).
    """
    rc, out = _git(repo, "checkout", "--detach")
    if rc != 0:
        raise GitError(f"git checkout --detach failed in {repo}: {out}")


def worktree_remove(repo: Path, path: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    rc, out = _git(repo, *args)
    if rc != 0:
        raise GitError(f"git worktree remove {path} failed: {out}")


def worktree_prune(repo: Path) -> None:
    """Drop administrative entries for worktrees whose directories are gone.
    Best-effort housekeeping — never raises. The return code is already ignored,
    but `_git` can *raise* — GitError on a timeout (#156) or GitSpawnError on a
    spawn fault (#343) — which would bypass this never-raise contract (and the
    teardown degrade paths that lean on it — close_unit_workspace /
    discard_worktree call prune from inside their own GitError guards). The
    OSError in the net predates the #343 translation and stays as the belt for
    any non-spawn fault: the contract holds at its source."""
    try:
        _git(repo, "worktree", "prune")
    except (GitError, OSError):
        pass


# `git worktree list --porcelain -z` arrived in git 2.36; the 2.34 support floor
# (Ubuntu 22.04's stock git) rejects the switch outright — `error: unknown switch
# `z'`, exit 129 (measured in an ubuntu:22.04 container, git 2.34.1). The floor is
# documented as a SUPPORT floor, not a capability one: no command bmad-loop issues
# may need more than it, so the NUL parse is gated and the newline parse kept
# beneath it rather than the floor raised.
_WORKTREE_LIST_NUL_GIT = (2, 36)


def worktree_list(repo: Path) -> list[Path]:
    """Paths of every worktree attached to `repo` (the main checkout first).

    Reads stdout alone, through NUL-delimited porcelain where git offers it
    (`_WORKTREE_LIST_NUL_GIT`), so paths may contain newlines and the record parse
    does not depend on no stderr line ever starting with ``"worktree "``. Below that
    version — and when git will not say what it is — the newline-delimited parse the
    floor supports is used instead: the one thing it cannot represent is a newline
    inside a worktree path, which then reads as a truncated record for that entry
    alone. The advisories measured for #442 — an unknown `core.fsyncMethod` value
    and its family — do NOT start with ``"worktree "``, so the `startswith` filter
    screens them out and this parse was correct by accident rather than by
    construction; the filter stays as a second, independent screen."""
    nul = git_below_floor(repo, _WORKTREE_LIST_NUL_GIT) is None
    proc = _run_git(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain", *(["-z"] if nul else [])],
        repo,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        raise GitError(f"git worktree list failed in {repo}: {detail}")
    paths = []
    for field in proc.stdout.split("\0" if nul else "\n"):
        if field.startswith("worktree "):
            paths.append(Path(field[len("worktree ") :]))
    return paths


def worktree_is_registered(repo: Path, path: Path) -> bool:
    """Whether ``path`` is this repository's exact live linked worktree.

    Directory existence is insufficient for recovery: a deleted ``.git`` marker
    below the main checkout makes git silently discover the parent repository,
    while a replacement repository at the same path can have its own valid
    toplevel. Require all three identities to agree: the persisted path is not a
    symlink, the main repository still lists it, and git invoked there reports
    both that exact toplevel and the main repository's common git directory.

    Ordinary git refusal reads as ``False`` so the recovery caller can escalate
    with its recorded-mount message. Spawn/timeout faults raised by ``_git_out``
    remain typed and fail loud.
    """
    if path.is_symlink():
        return False
    try:
        candidate = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    registered = False
    for listed in worktree_list(repo):
        try:
            if listed.resolve(strict=True) == candidate:
                registered = True
                break
        except (OSError, RuntimeError):
            continue
    if not registered:
        return False

    def git_path(root: Path, raw: str) -> Path:
        value = Path(raw)
        return (value if value.is_absolute() else root / value).resolve(strict=True)

    def path_out(root: Path, *args: str) -> tuple[int, str]:
        proc = _run_git(["git", "-C", str(root), *args], root)
        # Git terminates this scalar with one newline.  Removing exactly that
        # delimiter preserves whitespace/newlines that belong to the path itself.
        return proc.returncode, proc.stdout.removesuffix("\n")

    rc, top = path_out(candidate, "rev-parse", "--show-toplevel")
    if rc != 0:
        return False
    rc, mounted_common = path_out(candidate, "rev-parse", "--git-common-dir")
    if rc != 0:
        return False
    rc, repo_common = path_out(repo, "rev-parse", "--git-common-dir")
    if rc != 0:
        return False
    try:
        return Path(top).resolve(strict=True) == candidate and git_path(
            candidate, mounted_common
        ) == git_path(repo, repo_common)
    except (OSError, RuntimeError):
        return False


def dirty_paths(repo: Path) -> dict[str, str]:
    """Repo-relative posix path -> two-char porcelain XY status for every dirty
    entry in `repo`'s working tree. Excludes the orchestrator's own working dir
    (.bmad-loop/) — config, ledger, run state, engine plugins — none of which is
    ever a unit's merged content. NUL-delimited (`-z`) so paths with spaces/unicode
    and rename forms parse without C-quoting; for a rename the *destination* path
    (the one now on disk) is what's recorded. `-uall` lists individual untracked
    files (not a collapsed parent dir) so each entry can be matched 1:1 against a
    branch's incoming paths — but one entry per untracked nested repository,
    spelled with a trailing slash (`vendor/`), which git never descends into;
    `plan_incoming_collisions` tolerates it as `vendor`."""
    rc, out = _git_raw(
        repo, "status", "--porcelain", "-z", "-uall", "--", ".", f":(exclude){AUTOMATOR_DIR_REL}"
    )
    if rc != 0:
        raise GitError(f"git status failed in {repo}")
    return dict(_porcelain_entries(out))


def _porcelain_entries(out: str) -> list[tuple[str, str]]:
    """``(path, XY)`` per record of a NUL-delimited ``status --porcelain -z`` read."""
    tokens = out.split("\0")
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        xy, path = tok[:2], tok[3:]
        # rename/copy entries carry the original path as the next NUL field; the
        # destination (`path` above) is what's on disk, so consume and skip it.
        if "R" in xy or "C" in xy:
            i += 1
        result.append((path, xy))
        i += 1
    return result


def _porcelain_paths(out: str) -> list[str]:
    """Every path a NUL-delimited ``status --porcelain -z`` read names, on disk."""
    return [path for path, _xy in _porcelain_entries(out)]


def branch_incoming_paths(repo: Path, target: str, branch: str) -> set[str]:
    """The set of repo-relative posix paths a merge of `branch` into `target`
    would introduce, modify or delete (`git diff --name-only target branch`).

    ``--no-renames``: rename detection is on by default and names a rename by
    its destination alone, and the source — which the merge deletes — is as
    incoming as anything else: snapshotted by the receipt, cleaned or
    tolerated by the guard, excluded from the digest of the index outside the
    incoming set, and read by the restore's own inventory the same way (#796
    review)."""
    rc, out = _git_raw(repo, "diff", "--name-only", "--no-renames", "-z", target, branch)
    if rc != 0:
        raise GitError(f"git diff --name-only {target} {branch} failed in {repo}")
    return {p for p in out.split("\0") if p}


def plan_incoming_collisions(
    repo: Path,
    target: str,
    branch: str,
    *,
    protected: tuple[str, ...] = (),
    on_tolerated: Callable[[list[str]], None] | None = None,
) -> IncomingCollisionPlan:
    """Read and classify target collision cleanup without mutating the checkout.

    Background: with engine `editor_mode = "per_worktree"`, a competing Editor
    can leak asset writes (`.cs.meta` GUIDs, asmdef auto-edits) into the *main*
    checkout. The merge then aborts pre-flight ("local changes / untracked files
    would be overwritten"). Those leaked copies are Editor-generated duplicates of
    content already committed on `branch`, so cleaning them is safe — the merge
    re-creates the canonical versions.

    Guard: only paths that lie within the branch's incoming set are cleaned. A dirty
    path *outside* that set is real operator work and is never touched. Whether it also
    BLOCKS the merge is decided per path by the INDEX column, not by trackedness
    (#618): the merge writes only paths that differ between `target` and `branch`, so
    a stray git has nothing staged for — untracked, or tracked and edited in the
    working tree only — can be neither overwritten by the merge nor written into its
    commit. Measured on both topologies and both strategies: rc 0, the edit survives
    uncommitted, and it is absent from the resulting commit. A STAGED stray is the
    real hazard, and it is one under both strategies — `merge --no-ff` refuses it
    outright, and so does `merge --squash` against a diverged target; only a
    fast-forwardable `--squash` accepts it, and that one FOLDS the operator's staged
    work into the story's commit.

    ``protected`` names repo-relative posix paths the ORCHESTRATOR itself commits
    after the merge, and a stray among them blocks whatever its index column says.
    The merge's own inertness does not protect them, because the merge is not what
    would commit them: ``commit_paths`` runs `git add -- :(literal)<path>` and then a
    pathspec commit, so ANY working-tree change to a named path is committed no
    matter who wrote it. The run's carry bookkeeping passes the sprint board and the
    deferred-work ledger through that call — ``_carry_board_advance``
    unconditionally — so an operator's private unstaged edit to one of those files
    lands in git history under a `chore(sprint-status): carry ...` message, leaving
    the tree clean and no trace of the substitution. The blast radius is strictly
    SAME-PATH: `git commit -- <pathspec>` is implicitly `--only`, so dirt on any
    other path is never swept in, which is why this list is a set of exact paths and
    not a policy. Default empty — the wiring is the caller's.

    ``on_tolerated``, when given, is called once with the sorted list of stray paths
    the guard walked past — the exact complement of the blocking ones within the
    strays, and the mirror of the returned ``cleaned`` list — so a merge that
    proceeded over operator dirt leaves the same kind of trace as one that cleaned a
    leak. Not called when there are no such paths.
    """
    # the automator directory is read too, the run's own records left out:
    # a stray there is tolerated or blocking on the same terms as any other,
    # and the tolerated set is what the post-hook stray reading leaves alone
    # (#796 review)
    dirty = collision_dirty_paths(repo)
    if not dirty:
        return IncomingCollisionPlan((), (), ())
    incoming = branch_incoming_paths(repo, target, branch)
    stray = sorted(p for p in dirty if p not in incoming)
    # Trackedness was the wrong axis (#618). What a merge can write into its commit is
    # what git has STAGED, so the index column alone decides: a stray with nothing
    # staged is inert under both strategies and both topologies, and refusing over one
    # stopped unattended runs with no hazard to point at. Everything else this method
    # can see is a hazard the operator has to resolve first — a staged change, and the
    # unmerged stages of a half-resolved conflict, which carry a letter in that column
    # for every one of git's seven combinations.
    #
    # `protected` is the second half, and it is not about the merge at all: the run's
    # own post-merge carry commits those paths by pathspec, sweeping in whatever the
    # working tree holds. Inert-under-merge and safe-to-proceed stopped being the same
    # question the moment a path was on both lists.
    guarded = set(protected)
    staged = [p for p in stray if dirty[p][0] not in " ?"]
    blocking = [p for p in stray if dirty[p][0] not in " ?" or p in guarded]
    if blocking:
        # One raise, two remedies: staged work has to be committed or unstaged, while
        # dirt on a carried path has to leave the path entirely. A single undifferentiated
        # list would send the operator to the wrong one.
        clauses: list[str] = []
        if staged:
            clauses.append(
                "staged changes to tracked files outside this branch's files "
                f"(not introduced by the merge): {', '.join(staged)}"
            )
        # Membership in `guarded`, NOT the `staged` complement. A path can be both,
        # and the two clauses carry DIFFERENT remedies — so subtracting the staged ones
        # here would name a staged-and-carried path under "commit or unstage it" alone,
        # which does not remove the hazard: the carry stages whatever the working tree
        # holds either way. Overlap means it is listed twice, which is the honest answer.
        swept = [p for p in blocking if p in guarded]
        if swept:
            clauses.append(
                "uncommitted changes to paths this run commits for itself after the "
                f"merge, which it would sweep into its own bookkeeping commit: {', '.join(swept)}"
            )
        raise GitError("the target checkout has " + "; and ".join(clauses))
    # Every stray that survives the raise above is tolerated — the exact complement of
    # `blocking`, not a second independent predicate. Recomputing one here is how the
    # two lists drift: an unstaged tracked stray answering neither test would proceed
    # with no journal trace at all, which is the silent half of #618.
    #
    # One entry `-uall` still collapses: an untracked nested repository, which
    # `status` spells `vendor/`. It stays `vendor/` in `dirty` — no incoming
    # file path is spelled so, which is what keeps it out of `cleaned`: it is
    # an operator's repository, never an Editor leak, and a shape clash with
    # an incoming `vendor` is git's pre-flight to refuse — and is tolerated as
    # `vendor`, a path every receipt reading admits (`_portable_integration_path`
    # refuses the empty segment, and the journalled plan is validated the same
    # way at replay), so a target holding one no longer paused every modern
    # integration before the merge as malformed, and again at each resume
    # (#796 review). The receipt has no file to snapshot for it
    # (`capture_integration_state` passes it over); its `.git` and every entry
    # of its tree are the ignored listing's (`_nested_git_entries`), each at
    # its identity, which is what proves it unchanged after the hooks — the
    # tolerance is for the repository's presence, not its contents.
    tolerated = [path.rstrip("/") for path in stray]
    if tolerated and on_tolerated is not None:
        on_tolerated(tolerated)
    cleaned = tuple(sorted(path for path in dirty if path in incoming))
    return IncomingCollisionPlan(
        cleaned=cleaned,
        tolerated=tuple(tolerated),
        untracked=tuple(path for path in cleaned if dirty[path].startswith("??")),
    )


def apply_incoming_collision_plan(
    repo: Path,
    plan: IncomingCollisionPlan,
    *,
    before_mutate: Callable[[str], bool] | None = None,
    progress: list[str] | None = None,
) -> list[str]:
    """Apply an already snapshotted collision plan without widening its paths.

    ``progress``, when given, receives each path as it is TAKEN UP — after its
    ``before_mutate`` reading passed and before its first mutation — so a caller
    whose restore must reach exactly what this touched reads it after any
    failure: the paths already cleaned plus the one in flight, never the ones
    still ahead. Those may carry fresh operator state by the time the failure
    lands, and restoring them from the snapshot would flatten it (#796 review);
    the ``cleaned`` an `IntegrationCleanupChangedError` carries is the same
    inventory minus the in-flight path, which that error proved untouched.
    """
    if not plan.cleaned:
        return []
    # the plan's own reading, or a cleaned `.bmad-loop/` path — planned from
    # the automator reading — is missing from the re-read and every
    # integration refuses before its merge (#796 review)
    current = collision_dirty_paths(repo)
    expected_cleaned = set(plan.cleaned)
    if any(path not in current for path in expected_cleaned):
        raise IntegrationEvidenceError("target collision classification changed before cleanup")
    expected_untracked = set(plan.untracked)
    if any(
        current[path].startswith("??") != (path in expected_untracked) for path in expected_cleaned
    ):
        raise IntegrationEvidenceError("target collision classification changed before cleanup")
    # Resolve every untracked cleanup parent before deleting or checking out any
    # path. A later resolution fault must not leave an earlier collision cleaned
    # and the checkout only partly reconciled.
    repo_res = repo.resolve()
    prune_starts: dict[str, Path] = {}
    untracked = set(plan.untracked)
    for path in plan.cleaned:
        if path not in untracked:
            continue
        parent = (repo / path).parent.resolve()
        if parent != repo_res and not parent.is_relative_to(repo_res):
            raise OSError(
                f"refusing to clean incoming collision outside repository {repo_res}: "
                f"{repo / path}"
            )
        prune_starts[path] = parent
    cleaned: list[str] = []
    for path in plan.cleaned:
        if before_mutate is not None and not before_mutate(path):
            raise IntegrationCleanupChangedError(cleaned)
        if progress is not None:
            progress.append(path)
        if path in untracked:  # untracked: delete it, then prune emptied dirs
            fp = repo / path
            fp.unlink(missing_ok=True)
            parent = prune_starts[path]
            while parent != repo_res and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        else:  # tracked-modified: restore to the target's committed version
            rc, out = _git(repo, "checkout", "--", path)
            if rc != 0:
                raise GitError(f"git checkout -- {path} failed in {repo}: {out}")
        cleaned.append(path)
    return cleaned


def clean_incoming_collisions(
    repo: Path,
    target: str,
    branch: str,
    *,
    protected: tuple[str, ...] = (),
    on_tolerated: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Compatibility wrapper that plans and immediately applies cleanup."""
    plan = plan_incoming_collisions(
        repo,
        target,
        branch,
        protected=protected,
        on_tolerated=on_tolerated,
    )
    return apply_incoming_collision_plan(repo, plan)


def _merge_in_progress(repo: Path) -> tuple[bool, GitError | None]:
    """`(a merge is mid-flight — MERGE_HEAD exists, the probe failure when the
    reading itself failed)`. A merge git refused at pre-flight (e.g. untracked
    files would be overwritten) leaves no MERGE_HEAD, so there is nothing to
    `--abort`.

    `-q --verify` spends rc 1 on exactly "the name does not resolve" — the
    legitimate no — and the environment-fault family lands at 128 (measured),
    so rc 0 and rc 1 are the answers and anything else is an unread, as are
    the three faults `_run_git` raises with no rc at all (#343/#377/#156).
    Read in the same post-mutation window as `_index_unmerged`, and degrading
    for the same reason: a raise here escapes `merge_branch` between the
    failed merge and its cleanup. False WITH the marker set means unmeasured,
    not "no merge" — and the caller must not let it authorize the abort this
    value gates: `merge --abort` is a repair write, and uncertainty never
    authorizes one, so the unread case skips the abort and says so."""
    try:
        rc, out = _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    except GitError as unread:
        return False, unread
    if rc in (0, 1):
        return rc == 0, None
    return False, GitError(f"git rev-parse --verify MERGE_HEAD failed in {repo}: {out}")


def _dirty_tracked_paths(repo: Path) -> frozenset[str]:
    """The tracked paths whose working-tree content differs from HEAD.

    A TREE-STATE probe, not a did-the-merge-act probe. A path carrying a
    pre-existing unstaged edit is in this set whether or not git touched it,
    so a single post-merge reading cannot tell a refused merge from one that
    half-applied — which is why `merge_branch` samples it BEFORE the merge and
    differences after, per PATH (#619). The set form is what makes the answer
    per-path at all: the boolean this used to be (`git diff --quiet HEAD`)
    attributed a concurrent operator edit to ANY tracked file to git, and the
    repo-wide `reset --hard` riding on that attribution destroyed it.

    `--no-renames` so both samples spell a rename as its delete and its add —
    two names, each usable as a pathspec — rather than whichever single name
    rename detection happens to keep. NUL-delimited verbatim stdout for
    `_untracked_paths`'s reason: these names are compared as sets, handed to
    the operator, and passed back to git as pathspecs, so a C-quoted or
    stripped name is one the restore cannot act on. RAISES on a failed read
    like its snapshot sibling; the post-merge caller (`_merge_residue`)
    catches and degrades."""
    proc = _run_git(
        ["git", "-C", str(repo), "diff", "--name-only", "-z", "--no-renames", "HEAD", "--"], repo
    )
    if proc.returncode != 0:
        raise GitError(f"git diff --name-only HEAD failed in {repo}: {proc.stderr.strip()}")
    return frozenset(rel for rel in proc.stdout.split("\0") if rel)


def _index_dirty_vs_head(repo: Path) -> bool:
    """True when the INDEX differs from HEAD — i.e. a squash actually staged
    something to commit. Blind, unlike `_dirty_tracked_paths`, to pre-existing
    UNSTAGED edits in the checkout, which are none of a replay's business: with
    such an edit present the tree probe reports dirt a valid `allow_empty_squash`
    replay never staged, so the clean early return is skipped and the ensuing
    `git commit` fails with "no changes added to commit" (#619).

    `--quiet` rides `--exit-code`'s contract, which spends rc 1 on exactly
    "there are differences" — so rc 0 and rc 1 are the answers and anything
    else RAISES, like the snapshot siblings: read as "dirty", an unreadable
    index skipped the replay's no-op return, and the doomed `git commit` that
    followed dressed the probe failure as a commit refusal, with
    `_reset_hard_head`'s rollback riding on the fiction. The one caller reads
    post-merge and catches, degrading to `MergeResidueUnreadError` — nothing
    committed, nothing reset."""
    rc, out = _git(repo, "diff", "--cached", "--quiet", "HEAD")
    if rc in (0, 1):
        return rc == 1
    raise GitError(f"git diff --cached HEAD failed in {repo}: {out}")


def _untracked_paths(repo: Path) -> frozenset[str]:
    """The repo's untracked, non-ignored paths — the one dirt axis both
    `_dirty_tracked_paths` and `_index_dirty_vs_head` are blind to.

    Neither of those is a substitute: a merge that dies part-way through checkout
    rolls its INDEX back but leaves the files it already wrote in the working
    tree, and an untracked file is by definition absent from HEAD and from the
    index, so both diffs read CLEAN over it. Sampled before and after the merge
    and differenced, so the answer is "git wrote this", not "this is here" — the
    same before/after discipline the tracked half uses, and for the same reason:
    an absolute post-merge reading would attribute the operator's own strays to
    git.

    `--exclude-standard` deliberately keeps ignored files out. They are not
    residue this can act on: an ignored path is invisible to the next merge's
    pre-flight too, so it cannot produce the resume failure this probe exists to
    name, and reporting one would send the operator after a file their own
    `.gitignore` says is theirs.

    NUL-delimited, and read through `_run_git` directly for the same reason
    `capture_diff` does: this needs stdout VERBATIM and stderr for the error text,
    which no single `_git*` wrapper hands back together. `ls-files --others`
    applies `core.quotePath` C-quoting to non-ASCII paths, and a line-splitting
    read with `.strip()` also eats leading and trailing spaces from a filename —
    harmless to the delta, since both samples would be mangled identically, but
    not to the ANSWER: these names are handed to the operator with an instruction
    to clear them, and a quoted or trimmed name is one they cannot act on. Two
    distinct paths can also strip to the same string, which would let a
    pre-existing stray mask a real materialization. `dirty_paths` and
    `branch_incoming_paths` already read path lists this way.

    Taking stdout verbatim also covers the #442 advisory hazard the merged stream
    has: git writes warnings to stderr at rc 0, and against `_git`'s merge a
    warning line would become a phantom path in the set.

    RAISES on a failed read rather than degrading the way its neighbours
    `_index_unmerged` and `_merge_in_progress` do (each hands back an unread
    marker), and the difference is position, not importance: this is one half
    of a PAIR, so a silent empty set is not a neutral answer — an empty BEFORE
    against a real AFTER reports every stray already in the checkout as
    something git just wrote, and the message tells the operator to clear what
    it names. Failing the merge outright is the smaller harm, and the
    before-read that would produce that asymmetry runs while nothing has been
    mutated yet.

    That argument covers only the BEFORE reading, which is why the AFTER reading
    goes through `_merge_residue`: post-merge, this same raise would bypass the
    cleanup it stands ahead of, so that caller catches it and hands it back as
    an unread marker instead of letting it escape or answer empty."""
    proc = _run_git(
        ["git", "-C", str(repo), "ls-files", "-z", "--others", "--exclude-standard"], repo
    )
    if proc.returncode != 0:
        raise GitError(f"git ls-files --others failed in {repo}: {proc.stderr.strip()}")
    return frozenset(rel for rel in proc.stdout.split("\0") if rel)


def _incoming_paths(repo: Path, branch: str) -> frozenset[str]:
    """The paths a merge of `branch` could have WRITTEN: everything that differs
    between HEAD and `branch`'s tip (`git diff --name-only HEAD <branch>`).

    The attribution boundary `_merge_residue` intersects its deltas with: a merge
    updates a working-tree path only where the merge result differs from HEAD,
    and the result can differ from HEAD only where `branch` does — a path both
    sides changed identically is already at the result — so this two-dot diff is
    a superset of what any of the three strategies can touch, with no merge base
    consulted (criss-cross topologies and `branch_incoming_paths`'s two-dot
    precedent both argue for tips over a base). A concurrent operator edit landing
    on a path INSIDE this set during the merge window is indistinguishable from
    git's own write — that residual ceiling is `_merge_residue`'s to state.

    `--no-renames` and NUL-delimited verbatim stdout for `_dirty_tracked_paths`'s
    reasons: a rename must contribute BOTH its names (the deleted old name is
    exactly the kind of tracked residue a mid-checkout death leaves), and these
    names gate a repair write. Read only through `_merge_residue`'s catch — this
    runs post-merge, where a raise would bypass the cleanup — and only LAZILY,
    when there is a non-empty delta to attribute: a refusal that left no new dirt
    never consults `branch` at all, so an unresolvable ref (a raced-away branch,
    unrelated histories) still classifies as the pre-flight refusal it is."""
    proc = _run_git(
        ["git", "-C", str(repo), "diff", "--name-only", "-z", "--no-renames", "HEAD", branch, "--"],
        repo,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git diff --name-only HEAD {branch} failed in {repo}: {proc.stderr.strip()}"
        )
    return frozenset(rel for rel in proc.stdout.split("\0") if rel)


def _residue_snapshot(repo: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The pre-merge reading every leg takes: `(dirty tracked paths, untracked
    paths)`.

    Two axes because a failed checkout leaves two kinds of residue, and neither
    probe sees the other's. Taken BEFORE the merge for the reason #619 established
    on the squash leg: a checkout already carrying an unstaged edit reads dirty
    whether or not git touched a byte, so only a before/after comparison can say
    what GIT did — and only paths proven clean beforehand may be restored. Both
    halves are path SETS so that comparison is per path: one operator edit
    anywhere no longer surrenders the whole tracked axis, and — the other way
    around — a concurrent edit is no longer swept into a repo-wide attribution."""
    return _dirty_tracked_paths(repo), _untracked_paths(repo)


def _merge_residue(
    repo: Path, branch: str, pre_dirty_paths: frozenset[str], pre_untracked: frozenset[str]
) -> tuple[tuple[str, ...], tuple[str, ...], GitError | None]:
    """What a failed merge left behind: `(untracked paths git wrote, tracked
    paths git rewrote, the probe failure when a reading itself failed)`.

    Attribution is a per-path AND of two proofs, and both are load-bearing. The
    before/after DELTA proves a path changed during the merge window — without
    it every pre-existing stray and edit is git's. The INCOMING-set intersection
    proves the merge could have written it — without it a concurrent operator
    edit landing during the window is git's too, and the restore riding on the
    tracked half destroys it (the seventh mislabeled state; measured on all
    three legs). Both readings fail to the same, safe side: a path this cannot
    attribute is reported to nobody and restored over never.

    Two ceilings survive, stated rather than patched around. A path already
    dirty BEFORE the merge stays unattributable — per path now, not per tree —
    because the delta cannot say which bytes are whose. And a concurrent edit
    to a path INSIDE the incoming set is indistinguishable from git's own
    write, so it is attributed and restored; an edit racing the very paths a
    merge is rewriting has no safe reading at all, and the alternative —
    trusting nothing — would strand genuine residue on every half-applied
    checkout.

    The POST-mutation reading this is, a probe failure here must DEGRADE, never
    raise: the raise would escape `merge_branch` between the failed merge and its
    cleanup — stranding a started merge mid-flight with MERGE_HEAD set — and
    reach `merge_local`'s content-conflict arm wearing a probe error's text.
    Degrading does not mean going silent, which would route the empty reading to
    `MergePreflightError` and claim a tree nothing verified: the caught error is
    handed back so the terminal arm raises `MergeResidueUnreadError` instead,
    and both residue axes fail to the no-action side — nothing reported, nothing
    restored. The incoming read shares the catch and is taken LAZILY, only when
    a delta exists to attribute (see `_incoming_paths`). The BEFORE reading
    (`_residue_snapshot`) keeps its raise: it runs while nothing has been
    mutated yet, where failing the merge outright is the smaller harm."""
    try:
        materialized = _untracked_paths(repo) - pre_untracked
        rewritten = _dirty_tracked_paths(repo) - pre_dirty_paths
        if materialized or rewritten:
            incoming = _incoming_paths(repo, branch)
            materialized &= incoming
            rewritten &= incoming
    except GitError as unread:
        return (), (), unread
    return tuple(sorted(materialized)), tuple(sorted(rewritten)), None


def _restore_rewritten_paths(repo: Path, paths: tuple[str, ...]) -> tuple[bool, str]:
    """`git checkout HEAD -- <paths>` over the tracked paths a merge rewrote.
    Returns `(restored, note)`, the note being the clause to append to the raised
    message when it failed — so the error never claims a repair that did not
    happen. Path-scoped ON PURPOSE, never `reset --hard`: the attribution that
    authorizes this write is per path, so the write must be too — a repo-wide
    reset would flatten the operator's own dirt on every path the attribution
    deliberately left alone. `:(literal)` because these names came out of git
    verbatim and go back in as pathspecs — a `*` or leading `:` in a filename
    must match itself, not glob. Restores the index entry along with the
    working-tree bytes, which is the same path-scoped claim `reset --hard HEAD`
    made repo-wide, and resolves an unmerged entry to HEAD's version exactly as
    the reset did (measured; a path HEAD does not carry fails the whole write
    instead, and the note carries that). Callers must gate this on
    `_merge_residue`'s proven attribution; it is unconditional here on purpose,
    so the gate lives at one readable place per leg rather than inside the
    write."""
    rc, out = _git(repo, "checkout", "HEAD", "--", *(f":(literal){p}" for p in paths))
    if rc != 0:
        return False, (
            f"; AND git checkout HEAD -- <paths> failed (tracked residue not restored): {out}"
        )
    return True, ""


def _reset_hard_head(repo: Path) -> tuple[bool, str]:
    """`reset --hard HEAD`, the squash COMMIT step's rollback — the one remaining
    caller, and the one place a whole-tree write is still the right shape: the
    thing being rolled back is a SUCCEEDED `merge --squash`, whose staged result
    spans the entire incoming set, and the gate is the leg's pre-merge clean
    reading. Returns `(restored, note)` like its path-scoped sibling above.

    The ceiling that gate leaves, stated: a concurrent operator edit landing
    AFTER the pre-merge reading found the tree clean — during the squash or its
    commit attempt — sits inside this reset's blast radius and is flattened with
    the staged result. Narrowing that would mean unpicking a successful merge
    path by path against an attribution the commit step never takes; the bound
    is the deliberate trade, not an oversight."""
    rc, out = _git(repo, "reset", "--hard", "HEAD")
    if rc != 0:
        return False, f"; AND git reset --hard HEAD failed (tree not restored): {out}"
    return True, ""


def _index_unmerged(repo: Path) -> tuple[bool, GitError | None]:
    """`(the index carries unmerged stages — i.e. a merge really ran and left a
    content conflict to resolve, the probe failure when the reading itself
    failed)`.

    Deliberately NOT `.git/MERGE_HEAD`: a conflicted `git merge --squash` writes
    three unmerged stages and conflict markers while creating no MERGE_HEAD at
    all, so MERGE_HEAD would call every squash conflict a pre-flight refusal.
    `ls-files -u` discriminates across the whole matrix — empty for every
    pre-flight refusal and for success, three stages for a content conflict under
    both `--no-ff` and `--squash`. Neither git's exit code nor its wording can
    stand in: the same refusal shape yields rc 2 or rc 1 depending on whether the
    merge was fast-forwardable, rc 1 is also a content conflict, and one message
    line covers three distinct causes and is fully translated (#619).

    Read from stdout alone (#442): this is an emptiness read, and git writes
    advisories to stderr while still exiting 0, which against `_git`'s merged
    stream would read as unmerged entries.

    A failed reading DEGRADES to `(False, the failure)` rather than raising or
    silently answering "no conflict". The raise would escape `merge_branch`
    between the failed merge and its cleanup — the same post-mutation window
    `_merge_residue` already degrades in — and the silent False this used to
    give is no better: every NEGATIVE arm of the classification (commit-
    refused, half-applied, pre-flight) rests on `not unmerged`, so an
    unmeasured "no" lets a probe failure pick one of their classes. rc != 0 is
    the same unread — an `ls-files` that failed printed nothing, and an
    emptiness read over nothing is not a measurement. False WITH the marker
    set means unmeasured; callers claim no class resting on this reading and
    route to `MergeResidueUnreadError` instead."""
    try:
        rc, value, diag = _git_out(repo, "ls-files", "-u")
    except GitError as unread:
        return False, unread
    if rc != 0:
        return False, GitError(f"git ls-files -u failed in {repo}: {diag}")
    return bool(value), None


def merge_branch(
    repo: Path,
    branch: str,
    *,
    strategy: str = "merge",
    message: str | None = None,
    allow_empty_squash: bool = False,
    reflog_action: str | None = None,
) -> str | None:
    """Merge `branch` into the branch currently checked out in `repo`.

    strategy: "ff" (fast-forward only), "merge" (always a merge commit), or
    "squash" (collapse to one commit). Raises MergeConflictError on conflict and
    MergePreflightError when an ff-only merge can't fast-forward, restoring the
    tree to its pre-merge state.

    Returns the tree the squash leg STAGED before its own ``git commit`` sealed
    it (``write-tree``, read after ``merge --squash`` resolved and before any
    hook ran) — ``None`` on the other legs and on a no-op replay. That commit
    re-reads the index after ``pre-commit``, so a target hook can rewrite and
    re-add an incoming path into the commit itself, where no index or checkout
    probe can tell hook output from the resolved merge (#796 review); the
    receipt-owned caller proves the sealed commit's tree against this value
    instead. The `--no-ff` merge commit is written from the tree git already
    resolved, and `ff` creates no commit, so neither leg has the exposure.
    Expects the target checkout to be clean; the worktree pipeline reconciles
    Editor-induced dirt first via `clean_incoming_collisions`.

    A failure git raised at PRE-FLIGHT — an untracked file the merge would
    overwrite, a staged change on an incoming path, a file/directory shape clash,
    an `--ff-only` target that cannot fast-forward — never started a merge and
    left the tree untouched, so it raises the `MergePreflightError` subclass with
    git's own text passed through verbatim (#619). "git said no" is not enough to
    earn that class, though; see the fourth state below, where git says no after
    having already written part of the incoming tree.

    Four measured states, one terminal fallback, and no one probe orders them.
    `_index_unmerged` leads and answers
    CONTENT: unmerged stages mean the merge ran and collided (raised as
    `MergeConflictError`, so the caller never has to read "bare GitError" as
    "conflict"). Its absence does not
    mean the merge never ran — a `--no-ff` whose commit was refused leaves a
    cleanly merged index, no unmerged stages, and MERGE_HEAD set (measured for
    `pre-merge-commit`, `commit-msg`, and an unsignable `commit.gpgsign`).
    MERGE_HEAD is the second question and parts those two, which is why it is read
    BEFORE the abort that erases it rather than only to decide whether to abort at
    all.

    MERGE_HEAD alone would be wrong in the other direction — the error the old
    "no MERGE_HEAD created" framing made — because a conflicted `--squash` leaves
    unmerged stages and no MERGE_HEAD. The squash MERGE INVOCATION cannot be
    commit-refused — `--squash` stops before committing by design (measured:
    rc 0 with a rejecting `pre-merge-commit` hook installed) — which is why only
    the `merge` leg reads MERGE_HEAD below. The LEG still reaches that state:
    it seals the staged result with its own plain `git commit`, where hooks and
    `commit.gpgsign` run like anywhere else, and a refusal there raises the same
    `MergeCommitRefusedError` — rolled back by `reset --hard HEAD` under the
    leg's pre-merge dirtiness gate rather than by an abort, and reported
    ``staged`` when that gate or the reset itself declines. The scope error to
    not repeat: "no hook runs" was measured of the merge invocation and is false
    of the leg, whose commit step is a second, later place git can say no.

    The fourth state is the one ALL THREE legs reach and no index probe can see:
    git dying part-way through the CHECKOUT. It leaves no unmerged stages, no
    MERGE_HEAD, and an index rolled back to HEAD, so every index- and HEAD-based
    reading calls it "refused before starting" and tells the operator their
    checkout is untouched — while the residue blocks the next merge's pre-flight,
    and the run fails identically on every resume over paths nothing named.
    `--ff-only` is not exempt, and the "it never starts a merge" premise this
    module used to carry was simply wrong: `--ff-only` declines the TOPOLOGY
    question only, and once the fast-forward is possible it checks the incoming
    tree out like any other merge (measured under a required smudge filter, all
    three strategies; HEAD does not move).

    The residue has two axes, which is why `_residue_snapshot` reads two probes
    and not one — and attribution on both is per PATH: a before/after delta
    intersected with the branch's incoming set, so neither the operator's
    pre-existing dirt nor a CONCURRENT edit of theirs landing during the merge
    window is ever called git's (the latter was the seventh mislabeled state: a
    repo-wide dirtiness boolean attributed the bystander edit to git and the
    repo-wide reset riding on it destroyed the edit — measured on all three
    legs). An incoming path the target did not already track lands as an
    UNTRACKED file, which no restore reaches — `reset --hard` and `merge --abort`
    both leave untracked files alone, and the latter exits 128 here besides, there
    being no merge to abort — so it is reported for the operator to clear. An
    incoming path the target DID track is modified in place, which a path-scoped
    `git checkout HEAD --` over exactly the attributed paths does undo.
    Either axis raises `MergeHalfAppliedError` — a SIBLING of `MergePreflightError`,
    since the two are mutually exclusive by construction (a refusal git makes at
    pre-flight is made before any file is written).

    The terminal fallback is a post-merge reading that itself FAILED. Every
    probe in that window degrades to an unread marker rather than raising —
    the raise would escape between the failed merge and its cleanup — and a
    verdict may only stand on readings that are live. Dead residue readings —
    either delta's, or the incoming set's that attributes them — surrender
    only the choice between "refused before starting" and "failed
    part-way" (conflict and commit-refused stand on their own measurements
    over it). A dead index reading surrenders every claim resting on "did not
    collide" — commit-refused, half-applied, and pre-flight alike. A dead
    merge-state reading additionally skips the abort, which that reading
    gates: `merge --abort` is a repair write, and uncertainty never
    authorizes one, so the message says none was attempted. Whatever cannot
    be claimed raises `MergeResidueUnreadError`, and every dead probe is
    named in the message whichever class raises.

    ``allow_empty_squash`` is recovery-only: re-running a squash that committed
    before a host loss stages nothing because the target already has the merged
    tree. That clean result confirms the replay without manufacturing an empty
    commit; ordinary squash calls keep commit failures strict.
    """

    def run_git(*args: str) -> tuple[int, str]:
        if reflog_action is None:
            return _git(repo, *args)
        return _git_env(
            repo,
            *args,
            env={**os.environ, "GIT_REFLOG_ACTION": reflog_action},
        )

    if strategy == "ff":
        pre_dirty_paths, pre_untracked = _residue_snapshot(repo)
        rc, out = run_git("merge", "--ff-only", branch)
        if rc != 0:
            # "--ff-only either fast-forwards or declines, so it never touches the
            # tree" was the standing premise here, and it is FALSE: `--ff-only`
            # declines only the topology question. Once the fast-forward IS possible
            # it checks the incoming tree out, and a failure during that write —
            # measured under a required smudge filter — leaves HEAD where it was and
            # the residue behind. There are no index stages and no MERGE_HEAD to read,
            # so the residue snapshot is this leg's ONLY discriminator.
            materialized, rewritten, unread = _merge_residue(
                repo, branch, pre_dirty_paths, pre_untracked
            )
            half_applied = bool(materialized or rewritten)
            if half_applied:
                kind = "failed part-way through checkout"
            elif unread is not None:
                kind = "checkout state unverified"
            else:
                kind = "refused before starting"
            detail = f"git merge --ff-only {branch} failed in {repo} ({kind}): {out}"
            restored = True
            if rewritten:
                restored, note = _restore_rewritten_paths(repo, rewritten)
                detail += note
            if materialized:
                detail += f"; left untracked in {repo}: {', '.join(materialized)}"
            if half_applied:
                raise MergeHalfAppliedError(
                    detail, paths=materialized, restored=restored, rewritten=rewritten
                )
            if unread is not None:
                raise MergeResidueUnreadError(f"{detail}; AND the residue probe failed: {unread}")
            raise MergePreflightError(detail)
        return None
    if strategy == "merge":
        msg = message or f"Merge branch '{branch}'"
        pre_dirty_paths, pre_untracked = _residue_snapshot(repo)
        rc, out = run_git("merge", "--no-ff", "-m", msg, branch)
        if rc != 0:
            # All three questions BEFORE the abort, which erases the evidence for each.
            # The index stages say whether content collided; MERGE_HEAD says whether
            # a merge started at all, and it is asked here rather than inline below
            # so that one reading serves both the classification and the abort. The
            # residue deltas answer a question neither can: whether git wrote any
            # incoming file to the tree before dying (#619). All three degrade to an
            # unread marker rather than raising — they stand between the failed
            # merge and its cleanup, where an escape strands a started merge.
            unmerged, index_unread = _index_unmerged(repo)
            started, head_unread = _merge_in_progress(repo)
            materialized, rewritten, unread = _merge_residue(
                repo, branch, pre_dirty_paths, pre_untracked
            )
            # Only a failure that neither collided nor started can be a half-applied
            # checkout: a conflict and a refused commit both leave residue too, but
            # each already has a restore of its own below (`merge --abort`) and a
            # remedy of its own, so neither may be re-routed through this arm. The
            # two unread gates are that same rule asked negatively: with either
            # reading dead, "neither collided nor started" is a claim nothing
            # measured.
            half_applied = bool(
                index_unread is None
                and head_unread is None
                and not unmerged
                and not started
                and (materialized or rewritten)
            )
            if unmerged:
                kind = "conflict"
            elif index_unread is None and started:
                # `started` alone cannot claim this over a dead index reading: a
                # `--no-ff` conflict sits mid-merge too, and parting the two is
                # exactly the reading that failed.
                kind = "merged, but git refused the commit"
            elif half_applied:
                kind = "failed part-way through checkout"
            elif index_unread is not None or head_unread is not None or unread is not None:
                kind = "checkout state unverified"
            else:
                kind = "refused before starting"
            detail = f"git merge --no-ff {branch} failed in {repo} ({kind}): {out}"
            restored = True
            if half_applied and rewritten:
                # This leg has no `--abort` to reach for — that needs a MERGE_HEAD,
                # and there is none — so the restore is the path-scoped checkout,
                # over exactly the paths `_merge_residue` attributed.
                restored, note = _restore_rewritten_paths(repo, rewritten)
                detail += note
            if materialized and half_applied:
                detail += f"; left untracked in {repo}: {', '.join(materialized)}"
            if started:  # only abort a merge that actually started
                abort_rc, abort_out = _git(repo, "merge", "--abort")  # restore pre-merge HEAD
                if abort_rc != 0:
                    # The repair write failed, so the claim the caller's message would
                    # otherwise make — "the checkout is back as it was" — is now false.
                    # Carry that, rather than letting the classification imply it.
                    restored = False
                    detail += f"; AND git merge --abort failed (repo left mid-merge): {abort_out}"
            # Every dead probe is named in whatever raises, not only in the unread
            # class: a verdict standing on its own live measurement still owes the
            # operator which reading it does NOT have.
            if index_unread is not None:
                detail += f"; AND the index probe failed: {index_unread}"
            if head_unread is not None:
                # The abort is gated on the reading that just died, and uncertainty
                # must not authorize a repair write — so none was attempted, and the
                # message says that instead of implying a restore.
                detail += (
                    "; AND the merge-state probe failed, so no `git merge --abort` was"
                    " attempted — if `git status` shows a merge in progress, run it by"
                    f" hand: {head_unread}"
                )
            if unread is not None:
                detail += f"; AND the residue probe failed: {unread}"
            if unmerged:
                raise MergeConflictError(detail)
            if index_unread is None and started:
                raise MergeCommitRefusedError(detail, restored=restored)
            if half_applied:
                raise MergeHalfAppliedError(
                    detail, paths=materialized, restored=restored, rewritten=rewritten
                )
            if index_unread is not None or head_unread is not None or unread is not None:
                raise MergeResidueUnreadError(detail)
            raise MergePreflightError(detail)
        return None
    if strategy == "squash":
        # `--squash` has no `--abort`, so the restore is a path-scoped
        # `checkout HEAD --` over whatever the residue deltas attribute to git.
        # A single post-merge dirtiness reading cannot say whether the squash
        # caused the dirt it sees, so both axes are read BEFORE and differenced
        # after: a checkout already carrying an unstaged edit reads dirty even
        # when git refused and touched nothing, and a restore fired on that
        # reading destroyed it (#619). The untracked half covers the axis the
        # tracked one cannot see: a part-way merge rolls the index back and the
        # files it already wrote are untracked, so they are in neither HEAD nor
        # the index — which is exactly how such a failure came to be labelled
        # "refused before starting".
        pre_dirty_paths, pre_untracked = _residue_snapshot(repo)
        rc, out = run_git("merge", "--squash", branch)
        if rc != 0:
            unmerged, index_unread = _index_unmerged(repo)  # before any restore clears the stages
            materialized, rewritten, unread = _merge_residue(
                repo, branch, pre_dirty_paths, pre_untracked
            )
            # A conflict keeps its own class and its own remedy even though it leaves
            # residue too — its attributed paths are still restored below, exactly as
            # before. The unread gate is the conflict question asked negatively: with
            # the index reading dead, "did not collide" is a claim nothing measured.
            half_applied = bool(
                index_unread is None and not unmerged and (materialized or rewritten)
            )
            if unmerged:
                kind = "conflict"
            elif half_applied:
                kind = "failed part-way through checkout"
            elif index_unread is not None or unread is not None:
                kind = "checkout state unverified"
            else:
                kind = "refused before starting"
            detail = f"git merge --squash {branch} failed in {repo} ({kind}): {out}"
            if materialized and half_applied:
                detail += f"; left untracked in {repo}: {', '.join(materialized)}"
            restored = True
            if rewritten:
                # Gated on the proven per-path attribution, not on the class — so it
                # still runs when the index reading died and the class degraded: the
                # same restore a classified conflict gets, authorized by the same
                # measurement.
                restored, note = _restore_rewritten_paths(repo, rewritten)
                detail += note
            if index_unread is not None:
                detail += f"; AND the index probe failed: {index_unread}"
            if unread is not None:
                detail += f"; AND the residue probe failed: {unread}"
            if unmerged:
                raise MergeConflictError(detail)
            if half_applied:
                raise MergeHalfAppliedError(
                    detail, paths=materialized, restored=restored, rewritten=rewritten
                )
            if index_unread is not None or unread is not None:
                raise MergeResidueUnreadError(detail)
            raise MergePreflightError(detail)
        if allow_empty_squash:
            # Post-mutation read on the far side of SUCCESS: an escaping raise
            # would land in the caller's unclassified arm, and pressing on with
            # the reading dead manufactures a "nothing to commit" refusal plus
            # its rollback. Neither the no-op return nor the commit can be
            # claimed; say so and stop.
            try:
                staged = _index_dirty_vs_head(repo)
            except GitError as unread:
                raise MergeResidueUnreadError(
                    f"git merge --squash {branch} succeeded in {repo}, but the index"
                    f" reading that tells a no-op replay from a result to commit failed"
                    f" (index state unverified): nothing was committed and nothing was"
                    f" reset — any staged squash result is left in place; run"
                    f" `git status`: {unread}"
                ) from unread
            if not staged:
                return None
        rc, staged_tree, detail = _git_out(repo, "write-tree")
        if rc != 0:
            # Same window as the no-op reading above: the squash SUCCEEDED and its
            # result is staged, so the read failing must not be dressed as a commit
            # refusal by pressing on, nor escape to the unclassified arm.
            raise MergeResidueUnreadError(
                f"git merge --squash {branch} succeeded in {repo}, but the staged"
                f" result's tree could not be read (index state unverified): nothing"
                f" was committed and nothing was reset — the staged squash result is"
                f" left in place; run `git status`: {detail}"
            )
        msg = message or f"Squash-merge branch '{branch}'"
        rc, out = run_git("commit", "-m", msg)
        if rc != 0:
            # The leg's own commit — hooks and commit.gpgsign run HERE, not at the
            # `merge --squash` above, so this is where the squash reaches the
            # commit-refused state. No probe is needed to classify it: the merge
            # step already succeeded, so the failed call names the state by
            # itself. What needs deciding is the rollback — the deliberate undo
            # of a merge that SUCCEEDED, whose staged result spans the whole
            # incoming set, so it stays `reset --hard HEAD` rather than the
            # failure arm's path-scoped restore — and the gate is the snapshot's
            # tracked half: the staged result sits in a tree that reset would
            # flatten, so only a tree proven clean beforehand may be reset —
            # over a dirty one the operator's own uncommitted work is in the
            # blast radius, and the result is left staged and SAID so instead
            # (#619). The ceiling that gate leaves — an edit landing AFTER the
            # clean reading rides the reset — is `_reset_hard_head`'s to state.
            detail = (
                f"git commit (squash {branch}) failed in {repo} "
                f"(merged, but git refused the commit): {out}"
            )
            if pre_dirty_paths:
                restored = False
                detail += (
                    "; the squash result is left staged (not rolled back: the "
                    "checkout already carried uncommitted work, which "
                    "`reset --hard` would destroy with it)"
                )
            else:
                restored, note = _reset_hard_head(repo)
                detail += note
                if not restored:
                    detail += "; the squash result is left staged"
            raise MergeCommitRefusedError(detail, restored=restored, staged=not restored)
        return staged_tree
    raise GitError(f"unknown merge strategy: {strategy!r}")


def capture_diff(repo: Path, baseline: str, *, max_file_bytes: int | None = None) -> str:
    """Full unified diff of `repo`'s working tree against `baseline`, including
    untracked (but not ignored) files. Used to preserve a failed unit's changes
    for forensics. Returns "" when there is nothing to capture.

    Unlike `_git`, the tracked diff is read from stdout alone and left verbatim
    (no strip, no stderr merge) so the patch stays applyable, as is the
    `--no-index` spawn below it. The untracked names come from `untracked_files`,
    which inherits the same stdout-alone read (#442) and returns each name
    verbatim (#783). The line-based read it replaces got `core.quotePath`'s
    C-quoted spelling of a non-ASCII name; `--no-index` cannot open that
    spelling and exits 1 with empty stdout, the code the loop tolerates as "the
    files differ", so the file silently dropped out of the patch. Residual: a
    name the filesystem codec cannot decode still arrives as `untracked_files`'
    quoted token, `--no-index` cannot open it, and it stays omitted — the same
    residual #783 left.

    max_file_bytes caps the size of each *untracked* file included: a file larger
    than the cap is skipped and replaced with a one-line marker naming it and its
    size, so a stray build dir or huge log can't balloon the patch. None lifts the
    cap (capture everything regardless of size).
    """
    proc = _run_git(["git", "-C", str(repo), "diff", baseline, "--"], repo)
    if proc.returncode != 0:
        raise GitError(f"git diff {baseline} failed in {repo}: {proc.stderr.strip()}")
    parts = [proc.stdout]

    for rel in sorted(untracked_files(repo)):
        if max_file_bytes is not None:
            try:
                size = (repo / rel).stat().st_size
            except OSError:
                size = 0
            if size > max_file_bytes:
                parts.append(
                    f"# bmad-loop: skipped untracked file {rel!r} — "
                    f"{size / 1_048_576:.1f} MB exceeds the {max_file_bytes / 1_048_576:.1f} MB "
                    "cap (raise scm.failed_diff_max_mb or set scm.failed_diff_unlimited = true)\n"
                )
                continue
        # --no-index synthesizes an add-from-empty diff for the untracked file;
        # it exits 1 precisely because the files differ — expected here. Any other
        # non-zero code is a real failure (bad path, internal error), not "files
        # differ", so don't silently fold it into the patch.
        u = _run_git(["git", "-C", str(repo), "diff", "--no-index", "--", os.devnull, rel], repo)
        if u.returncode not in (0, 1):
            raise GitError(
                f"git diff --no-index for untracked {rel!r} failed in {repo}: {u.stderr.strip()}"
            )
        parts.append(u.stdout)
    return "".join(parts)


def set_frontmatter_field(path: Path, key: str, value: str, *, confine_root: Path) -> bool:
    """Rewrite (or insert) a scalar ``<key>:`` line in a spec's `---`…`---`
    frontmatter block.

    Same verified in-place line surgery as `set_frontmatter_status` (no YAML
    round-trip) so the spec's formatting, comments, and field order survive, and
    the same three-way return: True on a landed rewrite, False for **nothing to
    change** (no file, no frontmatter block, already at the value), and
    `FrontmatterWriteError` when the reader can see the key in a shape no line
    edit can safely move. "Comments survive" includes a trailing inline comment
    on the edited line itself: this shares `frontmatter._replace_value` with the
    status helper, so it inherits that renderer's certified-boundary carry (and
    its quote drop) rather than restating either.

    Unlike the status helper, a missing key is INSERTED as the block's last
    line: callers assert a field's value whether or not the skill wrote one
    (the patch-restore re-arm re-stamps ``baseline_revision``, which only the
    skill's step-03 writes). The insert is now gated on what `read_frontmatter`
    SEES rather than on a line-scan miss, which was a defect of its own — a
    quoted ``"baseline_revision":`` was missed by the scan and a second one
    appended, so the spec carried the key twice and the reader resolved the
    wrong one.

    Byte-preserving on the same terms as its sibling: ``read_bytes().decode`` in
    and bytes out, so a CRLF spec is not relaid to LF (nor an LF one to CRLF on
    Windows) by a write contracted to move one field. The INSERTED line takes the
    block's own ending, not a bare ``\\n``.

    Atomic on the same terms too (#379), and CONFINED on the same terms (#593):
    the spec-writer chokepoint rule — confined write in-tree, plain no-follow
    write for an artifacts folder configured outside the checkout — is stated
    once, in `frontmatter.set_frontmatter_status`, and this site implements it
    identically. ``confine_root`` is required for the reason it is required
    there. So is ``require_writable_target=True`` (#597): this rewrites an
    operator-editable spec, and a read-only one is answered rather than routed
    around by a replace that only needs the directory writable.

    Use the BYTES helper and not the text one:
    `atomic_write_text` keeps ``Path.write_text``'s translating newline default,
    which would relay ``\\n``→``\\r\\n`` on Windows and undo the paragraph above.
    """
    if not path.is_file():
        return False
    text = path.read_bytes().decode("utf-8")
    split = _split_frontmatter(text)
    if split is None:
        return False
    before, block, after = split
    edited = _edit_frontmatter_block(block, key, value, insert=True)
    if edited is None:
        return False
    payload = (before + edited + after).encode("utf-8")
    if path.is_relative_to(confine_root):
        atomic_write_bytes_confined(
            path, payload, confine_root=confine_root, require_writable_target=True
        )
    else:
        atomic_write_bytes(path, payload, follow_symlinks=False, require_writable_target=True)
    return True


def verify_dev_exclude_relpaths(
    paths: ProjectPaths,
    spec_path: Path,
    restore_patch: str | None = None,
    *,
    root: Path,
) -> tuple[str, ...]:
    """Repo-relative posix paths the dev/bundle proof-of-work gate excludes from
    its probe (`_changes_since`, via `_verify_shared_gates.proof_of_work_probe`) —
    deliberately file-granular. Rollback protection is a separate concern: it
    builds its own list in `RecoveryFlow.protected_relpaths` against
    `workspace.root`, and `Engine._protected_relpaths` merely delegates there.
    Deliberately does NOT exclude `output_folder`:
    in the standard layout it is the parent directory of `implementation_artifacts`/
    `planning_artifacts`, so excluding it as a directory prefix would swallow those
    two folders' content right back out of view via the same git-pathspec prefix
    match this function exists to avoid.

    Excludes only what a session rewrites regardless of whether it did any real
    work: `paths.sprint_status` (every session advances it as routine bookkeeping)
    and the session's own claimed `spec_path` (so a bare frontmatter status flip on
    it doesn't count). Sibling content under the implementation/planning artifact
    dirs — the deferred-work ledger, other stories' specs — is deliberately left
    un-excluded, so a story whose entire authorized scope is ledger/spec
    reconciliation registers as real work instead of a permanent false "no changes
    since baseline".

    `restore_patch` (the task's latched intent-gap patch file, BMAD-METHOD #2564)
    is excluded too when set: the patch is untracked halt residue under the
    protected artifact dirs that survives every reset, so counting it would let a
    restore re-drive whose session produced nothing pass the gate on the patch
    file's mere presence — the gate must key on the APPLIED work (the tracked diff
    from baseline), not on the orchestrator-owned patch that carried it.

    `spec_path` comes from a session-reported (untrusted) `spec_file` string, so
    it is `.resolve()`d before deriving the relpath, same as `spec_within_roots`:
    an un-normalized `..`/`.` segment would still resolve to the real on-disk
    file (the OS resolves it), but as a raw string it wouldn't match git's own
    normalized path output, silently defeating this exclude and letting a bare
    status flip on the session's own spec count as real work.

    ``root`` is the tree the resulting pathspecs are relative to, and MUST be the
    same root the caller invokes git against — `paths.repo_root` for the
    proof-of-work gate, which is where the probe runs. REQUIRED, with no
    default: an implicit `paths.project` anchor is #716's own root cause, and the
    two roots collapse in every configuration but the `repo_root` override, so a
    defaulted caller would look correct everywhere it was tested and be wrong only
    on the one config that matters. Requiring it turns OMITTING the root into a
    type error; it does not police a WRONG one — ``root=paths.project`` type-checks
    cleanly and silently excludes nothing, which is the failure the next paragraph
    describes. The requirement buys a caller who must think about the root, not a
    checker that knows the right answer.

    The wrong-root symptom depends on topology. With disjoint sibling project and
    code roots, a code-root relative artifact path collapses to ``()`` and a
    project-root spelling is non-empty but still matches nothing in the code tree.
    In a nested monorepo both spellings are non-empty: omitting the project prefix
    can select a plausible outer-tree file instead of the nested artifact. The
    latched `restore_patch` is anchored on the SAME root for the same reason (a
    relative latch names a path in the tree it will be applied to)."""
    candidates: list[Path] = [paths.sprint_status, spec_path]
    if restore_patch:
        candidates.append(resolve_restore_path(restore_patch, root))
    out: list[str] = []
    for path in candidates:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            continue  # outside or uncertain; nothing safe to exclude here
        if rel and rel != ".":
            out.append(rel)
    return tuple(out)


def spec_within_roots(spec_path: Path, paths: ProjectPaths) -> bool:
    """True if ``spec_path`` is, or sits under, an orchestrator-owned root (the
    project root or an artifact dir). A mutating repair (the frontmatter-status
    reconcile) must refuse a session-reported ``spec_file`` that resolves outside
    these roots, so a surprising path can never be silently rewritten. Artifact
    dirs configured outside ``project`` are roots too, so a legitimately
    out-of-project spec is still allowed."""
    try:
        sp = spec_path.resolve()
        roots = (
            paths.project,
            paths.output_folder,
            paths.implementation_artifacts,
            paths.planning_artifacts,
        )
        return any(sp == r.resolve() or sp.is_relative_to(r.resolve()) for r in roots)
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_spec_path(spec_file: str, paths: ProjectPaths) -> Path:
    """Probe a session-reported ``spec_file`` candidate into a concrete path.

    This lookup binds a reported or persisted spelling inside the current active
    ``ProjectPaths``. The spelling may come directly from a disposable session or be
    read from a task and rebound for a current or fresh workspace. An absolute value
    passes through untouched. A relative value — including a bare basename — is probed
    against ``paths.project`` first and falls back under
    ``paths.implementation_artifacts``. When an operation must instead address the tree
    recorded by the task, use :func:`runs.task_spec_path`, which anchors a bare basename
    directly on that tree without this fallback. Recovery uses
    ``recovery_flow.RecoveryFlow._attempt_owned_spec`` to bind restoration to exactly
    one trusted regular-file candidate after probing both locations.

    Neither branch promises the result exists — the fallback is returned unprobed
    when the project candidate is not a file — so every caller re-tests
    ``.is_file()`` itself. Deliberately does NOT ``.resolve()``: callers needing
    symlink and ``..`` normalization get it from :func:`spec_within_roots`, which
    resolves both sides itself.

    The rule its call sites follow: a caller that goes on to REWRITE the spec must
    pair this with :func:`spec_within_roots` first. The value is session-reported
    and this function hands back whatever it spells, so the containment check is
    what stands between an untrusted string and a write to it. The frontmatter
    reconcile, the marker repair and the repair/review spec resets all pair it; so
    do the two attempt-binding observations, which write nothing themselves but
    establish the binding ``recovery_flow`` later restores bytes through — the
    check belongs at the site conferring the authority, not only at the write.

    The rule is about writes to the SPEC, not writes in general, and two callers sit
    outside it deliberately: the post-dev board sync and the sweep bundle's ledger
    close each read a ``status:`` from an unchecked path and then write to a
    deterministic orchestrator-owned target of their own (the sprint board, the
    deferred-work ledger). An out-of-tree spec can influence what those write, never
    where. A caller that only reads — the ``--json`` read-model, the dev-verify
    gates — pairs it with nothing."""
    p = Path(spec_file)
    if p.is_absolute():
        return p
    candidate = paths.project / p
    if candidate.is_file():
        return candidate
    return paths.implementation_artifacts / p


def _gate_frontmatter(spec_path: Path) -> dict[str, Any] | VerifyOutcome:
    """Read a spec's frontmatter for a verify gate, degrading an unreadable spec
    to a retryable :class:`VerifyOutcome` instead of a whole-run crash.

    Every verify gate reads the spec back while the dev skill may still be
    rewriting it, so an OSError here (a TOCTOU truncation, a transient lock, a
    momentarily unsearchable parent) is a fault with a *designed* transient
    producer — not a broken orchestrator. `read_frontmatter` itself keeps raising
    (repair callers depend on that); only these observation gates degrade.

    The reason is deliberately distinct from a status mismatch: returning ``{}``
    here would read as status ``""`` and let a read fault masquerade as "the
    skill forgot to set the status", sending a repair session after a bug that
    is not there. Retries are not silent — the reason lands in the journal via
    `dev-decision` / `review-verify-failed`, and a persistent fault is bounded
    into DEFER (or PAUSE) by `escalation.decide_dev` / `decide_review_session`.
    """
    try:
        return read_frontmatter(spec_path)
    except OSError as e:
        return VerifyOutcome.retry(f"spec unreadable ({e.__class__.__name__}: {e}): {spec_path}")


@dataclass(frozen=True)
class _SharedGateResult:
    """What :func:`_verify_shared_gates` answers: the failing outcome (``None``
    when every gate passed and the caller may run its mode-specific tail), plus
    whatever the gate OBSERVED on the way through that no gate acted on.

    ``skipped_proof_zero_diff`` is the second kind: on a leg that skipped
    proof-of-work and asked to be told anyway (``observe_skipped_proof``), it is
    ``True`` when the tree held no changes the gate would have counted, ``False``
    when it held some, and ``None`` when nothing was observed — no skip, no
    request, no baseline, or a probe that could not answer (a ``GitError``, or a
    git refusal such as an unresolvable baseline). Note what ``False`` does and
    does not say: the gate would have found changes it counts, measured under the
    gate's own exclusions. It does not say who wrote them — in a shared checkout
    the gate itself cannot attribute residue to a session, and this observation
    inherits exactly that limit. It is deliberately a return value and
    not a gate input: the observation must be made HERE because the baseline it
    measures from is derived here (the newer-claim branch can re-anchor
    ``proof_baseline`` and drop untracked evidence), and no caller can reproduce
    that derivation. A caller re-probing from ``task.baseline_commit`` would count
    a commit that arrived in a shared ``isolation = "none"`` checkout from outside
    the session as this attempt's work — the exact false negative the observation
    exists to expose.

    ``artifact_only_residue`` is the third kind, on the bundle leg alone: the
    number of IGNORED entries the artifact-only receipt's listing held when the ordinary
    proof-of-work probe positively answered "nothing changed" and the receipt was
    then ACCEPTED in its place (DW-273). ``None`` everywhere else — no assertion,
    no ``artifact_only_dir``, a probe that found real changes (the ordinary arm
    passed and no receipt was consulted), or a receipt that was refused (the leg
    then carries the retry outcome, not a count)."""

    outcome: VerifyOutcome | None = None
    skipped_proof_zero_diff: bool | None = None
    artifact_only_residue: int | None = None


def _verify_shared_gates(
    spec_path: Path,
    rj: dict[str, Any],
    task: StoryTask,
    paths: ProjectPaths,
    *,
    expected_status: str,
    extra_exclude: tuple[str, ...] | None,
    observe_skipped_proof: tuple[str, ...] | None = None,
    allow_ancestor_baseline: bool = False,
    fm: dict[str, Any] | None = None,
    artifact_only_dir: Path | None = None,
) -> _SharedGateResult:
    """The workflow-tag, expected-status, baseline-match, and proof-of-work gates
    shared verbatim by :func:`verify_dev`, :func:`verify_dev_bundle`, and
    :func:`verify_dev_stories` — factored out so the sprint-mode and stories-mode
    gates can't silently drift. Reads frontmatter once; a caller that had to read
    it first to *choose* ``expected_status`` passes what it read as ``fm`` so the
    single-read contract still holds (no caller re-reads it).  Returns a
    :class:`_SharedGateResult` whose ``outcome`` is a failing
    :class:`VerifyOutcome`, or ``None`` when every gate passes and the caller may
    run its mode-specific tail.

    The proof-of-work exclude is derived here from the `task` this gate already
    receives (`verify_dev_exclude_relpaths`, which needs the latched restore patch);
    ``extra_exclude`` carries only what a mode adds on top — the engine-written
    paths for sprint and bundle, those plus the story record + manifest for
    stories, and ``None`` on the two legs that skip the gate outright (sprint's
    park, stories' plan halt). Threading the restore patch in
    from three call sites instead left a default-None foot-gun for a future fourth
    mode, which would silently let a restore re-drive pass proof-of-work on the
    patch file's mere presence. ``extra_exclude=None`` still skips the gate
    outright, and two callers now spell it for two different reasons: a plan-halt
    leg produced only its own spec (structurally spec-only), and a park may
    legitimately have produced no code at all because its remaining work is a
    human's (#676). Both mean "there is no diff to demand here"; neither
    generalizes to the other's leg, so keep them named separately.

    ``observe_skipped_proof`` is the same exclusion tuple the caller WOULD have
    passed as ``extra_exclude`` had it not skipped the gate. When set on a skipped
    leg the probe still runs — against the baseline derived above, not the raw
    ``task.baseline_commit`` — purely to answer whether there was in fact a diff,
    and the answer rides out on ``_SharedGateResult.skipped_proof_zero_diff``.
    Nothing branches on it here: a fault degrades to ``None`` rather than
    escalating, and the leg's outcome is identical either way. It exists so an
    accepted park's skipped gate stops being silent (#676) — a park the waived
    gate would have passed and one it would have refused are otherwise
    indistinguishable after the fact.

    Both skipping legs ask for it. Sprint mode's park and stories mode's plan halt
    have independent selectors — the park's session-authored assertion and the
    plan halt's strict ``result_json`` marker — while the observation records only
    what each waived gate would have found. It never replaces either selector and
    never changes acceptance.

    The two parameters are MUTUALLY EXCLUSIVE by construction: ``extra_exclude``
    gates and ``observe_skipped_proof`` observes, and the arms below are ``if`` /
    ``elif`` on that order. Passing both is not a richer mode, it is a caller
    error that silently drops the observation — the gate arm wins and the leg was
    never skipped, so there was nothing to observe. Pass ``extra_exclude`` OR
    ``observe_skipped_proof``, never both.

    ``artifact_only_dir`` is the bundle leg's artifact-only RECEIPT (DW-273), and
    it composes onto the gate arm only: when the ordinary probe positively answers
    "nothing changed" (``is False`` — a refusal or a fault never reaches it) and
    the caller passed a directory, :func:`_artifact_dir_owned_entries` lists that
    directory's IGNORED entries (`!!` records only — tracked and untracked ones
    are what the ordinary probe already measured) and keeps those the ATTEMPT
    created or changed against ``task.baseline_artifacts``, the fingerprint
    snapshot ``Engine._dev_phase`` stamped at its start; a positive owned set is
    accepted as proof of work with its count on
    ``_SharedGateResult.artifact_only_residue``. No owned entry (an empty
    directory, or one holding only residue that predates the attempt), no
    snapshot on the task, a directory outside ``paths.repo_root`` (or equal to
    it) or a git refusal keep the ordinary retry, with the receipt's refusal
    appended to the verbatim reason; a ``GitError`` escalates through the same
    ``except`` as the ordinary probe's. Only :func:`verify_dev_bundle` passes it — ``verify_dev`` and
    ``verify_dev_stories`` never do, so a story result asserting
    ``artifact_only`` still owes the ordinary diff — and the caller passes it only
    when the session's synthesized result carries the strict ``artifact_only:
    True`` boolean, so the decision to consult the receipt is the caller's and
    this gate never reads ``rj`` for it. It lives HERE rather than after the fact
    for the reason ``skipped_proof_zero_diff`` does: the ordinary probe's baseline
    can be re-anchored by the newer-claim branch above, and "the gate found
    nothing" is known at exactly one point."""
    workflow = rj.get("workflow")
    if workflow != DEV_WORKFLOW:
        return _SharedGateResult(
            VerifyOutcome.retry(
                f"dev result.json workflow is {workflow!r}, expected {DEV_WORKFLOW!r}"
            )
        )

    if fm is None:
        read = _gate_frontmatter(spec_path)
        if isinstance(read, VerifyOutcome):
            return _SharedGateResult(read)
        fm = read
    status = status_of(fm)
    if status != expected_status:
        return _SharedGateResult(
            VerifyOutcome.retry(
                f"spec status is {status!r}, expected {expected_status!r}: {spec_path}"
            )
        )

    # The generic bmad-build-auto skill stamps `baseline_revision`, never
    # `baseline_commit` — that name exists only in the result.json devcontract
    # synthesizes, which this gate does not consult (it re-reads frontmatter).
    # An absent key skips the check below, so reading `baseline_commit` alone
    # made this gate dead code for every generic-skill session. Both keys are read
    # through the one shared reader `devcontract.synthesize_result` also calls, so
    # the value this gate judges and the value the result.json reports are the same
    # value by construction rather than by two expressions agreeing (#716).
    claimed_baseline = auto_dev_baseline_of(fm)
    proof_baseline: str = task.baseline_commit or ""
    include_untracked_proof = True
    # Every probe below runs against `paths.repo_root`, the CODE tree, never
    # `paths.project`. Both baseline writers stamp `workspace.root`
    # (`Engine._dev_phase`, `SweepEngine`'s migration task) and re-arm now does the
    # same, and `Workspace.default` sets `root = paths.repo_root` while
    # `ProjectPaths.rebased` sets both roots to the worktree — so `repo_root` is
    # the one root that names the same repository as the recorded baseline in every
    # configuration. Under the `repo_root` override (`isolation = "none"` plus a
    # `repo_root:` config key, the only shape where the two differ —
    # `bmadconfig.worktree_isolation_conflict` refuses the other) the session's cwd
    # IS the code tree, so a `project`-anchored probe judged a tree the session never
    # touched. WHICH probe burned the attempt depends on the layout, and the burn is
    # not the proof-of-work probe in both: `_changes_since` answers `None` when git
    # will not run, and the gate arm below accepts anything that is not a positive
    # "nothing changed" (`is False`), so wherever `project` is not a checkout the
    # failing git call PASSES that gate. Nested
    # (`project` a subdirectory of the code tree) the call succeeds but is scoped to
    # that subdirectory, and the "no changes" forever-burn is real. Disjoint
    # (`project` beside the checkout) git fails and the burn moves to the probes that
    # fail CLOSED: `_canonical_commit_oid` returns None -> "does not match", and
    # `is_ancestor` / `commit_reachable_above_baseline` read the failure as False.
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        try:
            canonical_claimed = _canonical_commit_oid(paths.repo_root, claimed_baseline)
        except GitError as e:
            return _SharedGateResult(VerifyOutcome.escalate(str(e)))
        if canonical_claimed is None:
            return _SharedGateResult(
                VerifyOutcome.retry(
                    f"spec baseline {claimed_baseline[:12]} does not match "
                    f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
                )
            )
        if canonical_claimed != task.baseline_commit:
            # A deferred-work bundle may legitimately adopt a pre-existing story
            # spec: bmad-build-auto routes a "follow-up review of story X" bundle
            # into that story's done spec, whose baseline_revision is the
            # story's original dev baseline — necessarily older than the unit
            # worktree cut for the bundle (#161). An *ancestor* baseline means
            # the session diffed from an earlier commit on the unit's own
            # history (a superset of the unit's changes), which is sound; a
            # diverged or unknown baseline still fails.
            older_ok = allow_ancestor_baseline and is_ancestor(
                paths.repo_root, canonical_claimed, task.baseline_commit
            )
            # The other direction needs no opt-in flag: an intervening commit
            # before step-03 stamps `baseline_revision` makes the claim newer
            # than the recorded baseline. Accept it only when this checkout's
            # HEAD reaches that canonical descendant; stale, diverged, unknown,
            # and off-HEAD commits still fail.
            newer_ok = commit_reachable_above_baseline(
                paths.repo_root, canonical_claimed, task.baseline_commit
            )
            # Accepting a newer claim moves the proof-of-work reference onto it:
            # under `isolation = "none"` the claimed commit may have arrived in
            # the shared checkout from outside the session, and measuring from
            # the recorded baseline would let that commit satisfy proof-of-work
            # on its own — passing an attempt that implemented nothing. Ignore
            # untracked proof here because the launch snapshot cannot establish
            # whether it appeared before or after this later claimed commit.
            proof_baseline = canonical_claimed if newer_ok else proof_baseline
            include_untracked_proof = not newer_ok
            if not (older_ok or newer_ok):
                return _SharedGateResult(
                    VerifyOutcome.retry(
                        f"spec baseline {claimed_baseline[:12]} does not match "
                        f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
                    )
                )

    def proof_of_work_probe(mode_exclude: tuple[str, ...]) -> bool | None:
        """The one place proof-of-work is measured, called by BOTH arms below.

        The gate arm and the observation arm differ in exactly one input — which
        mode-supplied tuple composes onto the gate's own exclusions — and in
        nothing else. They were briefly two spelled-out copies of the same five
        arguments, and every property the docstrings claim for the observation
        (that it excludes the mode's paths, that it keeps the newer-claim
        ``proof_baseline``, that it inherits ``include_untracked_proof``) was
        silently droppable in the copy while the gate stayed correct and the suite
        stayed green. A shared body makes the two unable to disagree by
        construction, which is stronger than any test over the copies: divergence
        is no longer a thing a reader can express here.

        The exclude pathspecs are rooted where git is invoked: `repo_root` here
        and `repo_root` in every producer that composes into them
        (`Engine._harvest_gate_exclude`, `_stories_relpaths`). A pathspec relative
        to a different root is not merely wrong, it is SILENTLY wrong — git
        matches nothing and the exclusion evaporates.

        Tri-state on purpose: ``None`` means git REFUSED to answer — any rc outside
        the two that ARE answers, rc 128 being the everyday one — which the two arms
        below must read differently. The gate treats it as the
        stricter "there are changes" — exactly `has_changes_since`'s fail-open,
        which this function used to call and whose behavior the gate arm keeps
        byte-for-byte — while the observation arm records it as unknown rather
        than as a confident answer it never got.
        """
        return _changes_since(
            paths.repo_root,
            proof_baseline,
            exclude=verify_dev_exclude_relpaths(
                paths, spec_path, task.restore_patch, root=paths.repo_root
            )
            + mode_exclude,
            baseline_untracked=task.baseline_untracked,
            include_untracked=include_untracked_proof,
        )

    if extra_exclude is not None and task.baseline_commit:
        try:
            # `is False` is the gate's fail-open spelled out: only a probe that
            # positively answered "nothing changed" refuses the attempt, so a git
            # REFUSAL (`None`) keeps the stricter path exactly as it did when this
            # arm called `has_changes_since` and let that function collapse it.
            if proof_of_work_probe(extra_exclude) is False:
                reason = "no changes in worktree since baseline commit"
                if artifact_only_dir is None:
                    return _SharedGateResult(VerifyOutcome.retry(reason))
                # The receipt (DW-273): consulted only here, after the ordinary
                # probe positively found nothing, and only on the leg whose caller
                # asked. `None` (outside the tree, or git refused) and `[]` (git
                # listed no ignored entry) both refuse it — the retry keeps its
                # verbatim prefix so existing readers still match, and names the
                # cause.
                # Ownership, not presence: only entries this attempt created or
                # changed since its start-of-attempt snapshot count, so residue
                # left by an earlier bundle (or by last week) proves nothing. No
                # snapshot at all — a pre-upgrade task, or a capture that degraded
                # at dispatch — refuses too: uncertainty keeps the gate strict.
                baseline = task.baseline_artifacts
                owned = (
                    None
                    if baseline is None
                    else _artifact_dir_owned_entries(paths.repo_root, artifact_only_dir, baseline)
                )
                if owned:
                    return _SharedGateResult(artifact_only_residue=len(owned))
                if owned is None:
                    # Two refusals share this arm; the listing's own answer names
                    # which, and spawns git only on the way to a refusal a dir
                    # outside the tree never reaches (it answers `None` first).
                    listable = (
                        baseline is None
                        and _artifact_dir_entries(paths.repo_root, artifact_only_dir) is not None
                    )
                    cause = (
                        "artifact-only receipt refused: no attempt-start snapshot "
                        "of implementation_artifacts to measure ownership against"
                        if listable
                        else "artifact-only receipt refused: implementation_artifacts "
                        "is outside the code tree or git refused to list it"
                    )
                else:
                    listed = _artifact_dir_entries(paths.repo_root, artifact_only_dir) or []
                    cause = (
                        "artifact-only receipt refused: implementation_artifacts "
                        "lists no ignored entries"
                        if not listed
                        else "artifact-only receipt refused: implementation_artifacts "
                        f"lists {len(listed)} ignored entries, none created or "
                        "changed by this attempt"
                    )
                return _SharedGateResult(VerifyOutcome.retry(f"{reason} ({cause})"))
        except GitError as e:
            return _SharedGateResult(VerifyOutcome.escalate(str(e)))
    elif observe_skipped_proof is not None and task.baseline_commit:
        # The gate was skipped; run its probe anyway and report, never refuse.
        #
        # Unanswerable is recorded as unanswerable, in BOTH of the ways a probe
        # can fail to answer: a `GitError` (timeout, spawn or decode fault) and a
        # git REFUSAL (any rc that is not one of the two real answers — rc 128 for
        # an unresolvable baseline is the everyday one), which the tri-state
        # probe reports as `None` rather than collapsing into the gate's
        # fail-open. Collapsing it would file "the gate would have found changes"
        # about a question git never answered — the one reading a reader cannot
        # correct, because nothing downstream re-asks. A non-git bug still
        # surfaces: only `GitError` is caught.
        try:
            observed = proof_of_work_probe(observe_skipped_proof)
        except GitError:
            observed = None
        return _SharedGateResult(None, None if observed is None else not observed)

    return _SharedGateResult()


# The terminal spec status of a story whose agent-doable work is finished but
# whose acceptance criteria include external actions only a human can perform
# (#335). Mirrors devcontract.AWAITING_OPERATOR, kept literal here for the same
# reason PLAN_HALT_STATUS is: devcontract imports verify, never the reverse.
AWAITING_OPERATOR = "awaiting-operator"


def _operator_actions_gate(fm: dict[str, Any], story_key: str) -> VerifyOutcome | None:
    """Refuse a park that enumerates nothing, with feedback a repair session can
    act on. ``None`` when the spec declares at least one usable action.

    A park is *defined* by owing external work: a spec at ``awaiting-operator``
    with no readable ``operator_actions:`` names no obligation, so confirming it
    later would be a human acknowledging a blank. Every malformed shape reaches
    here as an empty reading (:func:`frontmatter.operator_actions_of`), and all
    of them have the same remedy, so one message covers them: name the actions,
    or finalize the status that matches reality. ``fixable=True`` — the tree is
    real work and the defect is one frontmatter block, so the reason goes to a
    repair session as feedback rather than throwing the attempt away.
    """
    if operator_actions_of(fm):
        return None
    return VerifyOutcome.retry(
        f"spec for {story_key} is 'awaiting-operator' but declares no usable "
        f"operator_actions: add a YAML list of strings naming each external "
        f"action a human must perform, or finalize the status the work actually "
        f"reached ('done' when nothing is owed, 'blocked' when the story cannot "
        f"proceed)",
        fixable=True,
    )


def verify_dev(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
    *,
    operator_park: bool = False,
    engine_written: tuple[str, ...] = (),
) -> VerifyOutcome:
    """Verify a dev session's on-disk artifacts against its result.json claims.

    Checks the claimed spec exists, carries the fixed ``auto-dev`` workflow tag,
    sits at the expected status (``in-review`` when a separate review session
    follows, ``done`` when review is disabled), has produced changes (every leg
    but the park — see ``operator_park`` below), and that the story's
    sprint-status was advanced to the matching stage. Returns a retryable
    VerifyOutcome on any mismatch, escalates on git failure, passes otherwise.

    The spec's baseline frontmatter is an OPTIONAL attestation: a usable
    ``baseline_revision`` or legacy ``baseline_commit`` claim is checked against
    the accepted orchestrator baseline, while absence of both claims is accepted.
    Absence does not waive proof-of-work; without a claim, changes are still
    measured from the orchestrator-recorded ``task.baseline_commit``.

    ``operator_park`` (``[operator] enabled``, engine-supplied) adds one more
    accepted spec/sprint pair: ``(awaiting-operator, awaiting-operator)``, the
    park a dev session declares when the story's remaining work is a human's
    (#335). The OBSERVED spec status selects which pair is demanded — the skill
    decides whether it parked, and the gate then holds it to the matching board
    state and to a non-empty action list. Off by policy, the token is simply not
    a terminal the gate knows, so it fails the ordinary status check and the
    session is retried with that mismatch as feedback.

    The proof-of-work gate is skipped only when the observed park intersects a
    strict current-session result assertion — ``skip_proof = parked and
    rj.get("park_asserted") is True``. ``parked`` comes from the independently
    observed spec status plus policy. ``park_asserted`` is minted by
    :func:`devcontract.synthesize_result` only from the last genuine, non-fenced
    ``## Auto Run Result`` marker whose status is ``awaiting-operator``. Both
    halves are load-bearing. The skip exists
    because a park's whole output can legitimately be its own spec's park
    declaration plus the board sync, both of which proof-of-work already excludes,
    so demanding a diff read a correct park as "no changes since baseline commit"
    and refused it (#676) — costing the attempt, and with it the park declaration:
    reverted outright under ``isolation = "worktree"`` or
    ``scm.rollback_on_failure = true``, and a paused run with manual-recovery steps
    on the default in-place config. What is still pending here is the
    ORCHESTRATOR's commit — the squash plus the park record land only after this
    gate passes — not the session's own work: ``bmad-build-auto`` commits each
    iteration, so a skill commit chain usually already sits above baseline
    (``Engine._finalize_commit_phase``), and a reset discards that too, onto an
    ``attempt-preserve/*`` ref.

    The assertion establishes attempt ownership, not honesty or a second status
    authority. A frontmatter-only fallback, a legacy result, a malformed value,
    or a marker carrying the orchestrator's missing-marker repair note cannot
    authorize the waiver. Those parks are not otherwise refused: they take the
    ordinary proof-of-work arm, so one carrying a real diff still passes. Crash
    and fixable-retry replay preserve the already synthesized result rather than
    deriving authority from retained frontmatter or ``operator_actions``.

    Nothing else relaxes on the asserted leg either — the ``operator_actions``
    gate above still refuses a park that enumerates nothing, and the workflow-tag,
    status, baseline-match and sprint-pair gates all still run. Two of those four
    are not independent evidence on this leg, and saying so is the point: the
    status check is tautological here (the same ``fm`` that selected ``parked`` is
    threaded in as ``fm=fm``, so the shared gate compares it against an
    ``expected_status`` derived from itself), and the sprint pair was written from
    that same frontmatter by ``Engine._post_dev_state_sync`` a dozen lines before
    this gate runs, so it confirms the orchestrator's own write landed rather than
    anything the session did. What still binds a park to the attempt the
    orchestrator actually launched is the workflow tag, the baseline match, the
    non-empty actions list and the independent result-marker assertion.
    Baseline-match also
    accepts a claim NEWER than the recorded baseline whenever it is a
    HEAD-reachable descendant, and the comment guarding that branch names the
    compensating control: such a commit "may have arrived in the shared checkout
    from outside the session", so the check re-anchors proof-of-work onto the
    claimed commit rather than trusting the match alone. Proof-of-work is precisely
    what this leg skips, so on a park that re-anchoring still gates nothing — but
    it is no longer inert: the observation below inherits it, so a foreign commit
    cannot be credited as this attempt's work in the record either.

    The accepted skip is no longer silent, and it is recorded on TWO fields
    because one cannot carry both facts. ``VerifyOutcome.park_proof_skipped`` is
    the waiver itself — ``skip_proof``, ``False`` on every other leg. When it
    fires, the shared gate additionally runs the proof-of-work probe as a pure
    OBSERVATION (``observe_skipped_proof=engine_written``) and what that probe
    found rides out on ``VerifyOutcome.park_zero_diff``: ``True`` when the waived
    gate would have found nothing it counts, ``False`` when it would have found
    something, ``None`` when the probe could not answer. Read ``False`` as exactly
    that and no further — the residue the gate counts is not attributed to a
    session, here or in the gate itself, because under a shared checkout it cannot
    be (see the newer-claim paragraph above, and `docs/FEATURES.md` on
    ``isolation``). What separates "unknown" from "no skip happened" is
    ``park_proof_skipped``, not this field — collapsing the two into
    ``park_zero_diff is not None`` would make a park whose probe faulted look like
    a leg that never waived anything, and it would go unrecorded — the silence
    this record exists to end. ``None`` means "the probe could not answer", and
    reaches here three ways: a ``GitError`` (timeout, spawn or decode fault), a
    git REFUSAL such as an unresolvable baseline (any rc that is not one of git's
    two real answers, rc 128 being the everyday one — the gate arm folds that into
    its fail-open, the observation arm keeps it as unknown), and an attempt
    carrying no ``task.baseline_commit`` to measure from (the shared gate runs
    neither arm without one). Neither field changes an outcome: an unanswerable
    probe degrades rather than escalating, and an eligible park verifies
    identically either way. Their consumer is
    :meth:`Engine._verify_dev_artifacts`, which journals
    ``park-proof-of-work-skipped`` for a waived gate that this function then
    PASSED, and carries the observation as that record's ``zero_diff`` field, so a
    park the waived gate would have passed and one it would have refused stop
    being indistinguishable afterwards (#676). Both ends of that scope are set here: a
    waiver refused by a later check in this function (the sprint pair) never
    reaches the record, and a record that IS written asserts only that this gate
    was cleared with proof-of-work waived — the configured ``[verify]`` commands,
    the review loop and the commit all run afterwards and may still reject the
    attempt, which is then retried or deferred with its record already written.

    ``engine_written`` names paths the orchestrator itself wrote above this gate
    during the attempt, relative to ``paths.repo_root`` — the tree the gate invokes
    git in, and therefore the root every pathspec composed into this exclusion set
    must share (#716). They compose with the mode's normal proof-of-work exclusions
    so engine bookkeeping cannot masquerade as session work; see
    :meth:`Engine._harvest_gate_exclude`, which is their producer and states what a
    ledger outside the code tree resolves to. On the skipped park leg they are
    passed as ``observe_skipped_proof`` instead of ``extra_exclude``: no gate
    consumes them there, but the zero-diff observation must exclude exactly what
    the gate would have, or the orchestrator's own bookkeeping writes would be
    counted as residue on the park's record.
    """
    rj = result_mapping(result_json)
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    fm = _gate_frontmatter(spec_path)
    if isinstance(fm, VerifyOutcome):
        return fm
    parked = operator_park and status_of(fm) == AWAITING_OPERATOR
    if parked:
        actions = _operator_actions_gate(fm, task.story_key)
        if actions is not None:
            return actions
    # The two-part selector: the independently observed park AND the strict
    # current-session marker assertion. Every other park gate below still keys on
    # `parked` alone; the result assertion authorizes only this waiver (#335, #676).
    skip_proof = parked and rj.get("park_asserted") is True

    # With review disabled, the dev session runs its own internal review and
    # finalizes straight to done; otherwise it hands off at in-review. A park
    # short-circuits both: the story is finished as far as any agent can take it.
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status=(
            AWAITING_OPERATOR if parked else ("in-review" if review_enabled else "done")
        ),
        # Proof-of-work is the one gate an ASSERTED park skips (``extra_exclude=None``,
        # the callee-blessed spelling): such a park's whole residue can legitimately
        # be the spec and the board, both already excluded (#676). The park paragraph
        # in this function's docstring carries the reasoning and, more importantly,
        # what the skip does NOT relax. An unasserted park takes the ordinary arm
        # and owes a diff like every other terminal.
        extra_exclude=None if skip_proof else engine_written,
        # Same tuple, no gate: when the skip fires the probe still runs, purely so
        # the accepted park's zero-diff answer can be journaled (#676).
        observe_skipped_proof=engine_written if skip_proof else None,
        fm=fm,
    )
    if gate.outcome is not None:
        return gate.outcome

    expected_sprint = AWAITING_OPERATOR if parked else ("review" if review_enabled else "done")
    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != expected_sprint:
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected {expected_sprint!r}"
        )

    task.spec_file = str(spec_path)
    # Two facts, deliberately on two fields: `park_proof_skipped` says this leg
    # WAIVED proof-of-work (False on every other leg), `park_zero_diff` says what
    # the waived gate would have found — and `None` there now means only "the
    # probe could not answer", because the first field already carries the waiver.
    # Both are carried to the journal; neither is a gate (#676).
    return VerifyOutcome.passed(
        park_proof_skipped=skip_proof,
        park_zero_diff=gate.skipped_proof_zero_diff,
    )


def verify_dev_bundle(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
    *,
    engine_written: tuple[str, ...] = (),
) -> VerifyOutcome:
    """verify_dev for a deferred-work bundle: bundles have no sprint-status
    entry. The orchestrator owns the bundle→dw-id binding (``task.dw_ids``,
    marked done by ``SweepEngine``'s ledger sync); the generic ``bmad-build-auto``
    primitive never authors dw ids. So the dw_ids cross-check is enforced only
    when the session actually claims them — an empty/absent claim is the normal
    generic path and passes.

    ``engine_written`` has the same contract as :func:`verify_dev`.

    The artifact-only RECEIPT (DW-273) is this leg's alone. A bundle whose only
    permitted deliverable lives under a gitignored ``implementation_artifacts``
    (a spec-only erratum) can never satisfy the ordinary proof-of-work probe —
    it measures tracked and untracked-not-ignored paths only — and a bundle has
    no ``awaiting-operator`` park to fall back on (``_operator_park_enabled`` is
    False for bundles). So when the session's synthesized result carries the
    strict ``artifact_only: True`` boolean — minted by ``devcontract`` from the
    current session's genuine marker, never from frontmatter, exactly as
    ``park_asserted`` is — and the ordinary probe positively found nothing, the
    gate accepts, in its place, the ignored entries of a ``git status --ignored``
    listing scoped to ``paths.implementation_artifacts`` that THIS attempt
    created or changed — measured against the fingerprint snapshot
    ``Engine._dev_phase`` stamped on ``task.baseline_artifacts`` at the attempt's
    start — and the acceptance rides out as ``artifact_only_accepted`` /
    ``artifact_only_residue`` (the owned count) for the sweep engine to journal. A bundle with a real change passes the ordinary arm and
    records no receipt; a loose truthy value (``"true"``, ``1``) is no assertion.

    What the receipt does NOT relax: the workflow tag, the expected status, the
    baseline match, the dw_ids cross-check below, the configured ``[verify]``
    commands, and the review gate — ``verify_review_bundle`` still requires every
    bundle id ``status: done``. And what the receipt's ownership check does not
    reach: ignored paths carry no git baseline, so "created or changed" is read
    off ``lstat`` fingerprints (mtime and size) rather than content — a rewrite
    that lands byte-identical with a preserved mtime is invisible to it, as it
    is to the ordinary probe. The assertion selects the receipt; the snapshot is
    what makes it proof."""
    rj = result_mapping(result_json)
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # The strict boolean, never a truthy string/int — the same selector shape as
    # `park_asserted`'s (`is True`).
    artifact_only = rj.get("artifact_only") is True

    # With review disabled, the dev session finalizes the bundle straight to done.
    # allow_ancestor_baseline: a bundle that adopts a pre-existing story spec
    # (follow-up review) carries that spec's older-but-ancestral baseline (#161).
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status="in-review" if review_enabled else "done",
        extra_exclude=engine_written,
        allow_ancestor_baseline=True,
        artifact_only_dir=paths.implementation_artifacts if artifact_only else None,
    )
    if gate.outcome is not None:
        return gate.outcome

    claimed_ids = {str(i) for i in (rj.get("dw_ids") or [])}
    if claimed_ids and claimed_ids != set(task.dw_ids):
        return VerifyOutcome.retry(
            f"result.json dw_ids {sorted(claimed_ids)} do not match the bundle's "
            f"{sorted(task.dw_ids)}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed(
        artifact_only_accepted=gate.artifact_only_residue is not None,
        artifact_only_residue=gate.artifact_only_residue,
    )


# A spec_checkpoint story's plan-halt leg leaves the spec at this status (the
# skill HALTs after the Ready-for-Development gate); mirrors
# devcontract.PLAN_HALT_STATUS, kept literal here to avoid a verify<-devcontract
# import cycle (devcontract imports verify).
PLAN_HALT_STATUS = "ready-for-dev"


def verify_dev_stories(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    *,
    spec_folder: Path,
    review_enabled: bool = True,
    plan_halt: bool = False,
    engine_written: tuple[str, ...] = (),
) -> VerifyOutcome:
    """verify_dev for stories mode: the story spec lives at the id-keyed path
    ``<spec-folder>/stories/<id>-<slug>.md`` and there is no sprint-status entry.

    Same gates as :func:`verify_dev` — workflow tag, expected frontmatter status,
    baseline match, proof-of-work since baseline — with two differences: the spec
    is resolved **deterministically by id** (``task.story_key``) via
    ``stories.resolve_story_spec`` rather than trusting the session-claimed path,
    and the sprint-status gate is dropped (stories mode has no sprint board).
    A resolution that is pending / ambiguous / a sentinel is a retryable failure,
    and the resolved filename's id prefix is asserted to equal the task id.

    ``plan_halt`` verifies a spec_checkpoint story's plan-halt leg instead of an
    implementation: the expected status is ``ready-for-dev`` (the plan is done,
    not the code) and the proof-of-work gate is skipped — a plan writes only its
    own spec, which proof-of-work already excludes, so requiring code changes
    would spuriously fail every plan leg. The spec-resolution, id-prefix, workflow,
    and baseline gates still run, and ``task.spec_file`` is still recorded. A
    ``plan_halt`` leg also requires the ``result_json`` to carry the ``plan_halt``
    marker ``devcontract`` emits on a clean plan-halt, so a died-mid-flight
    ``ready-for-dev`` can't be mistaken for a successful plan. A passing halt
    returns what the skipped proof gate would have found as
    ``VerifyOutcome.plan_halt_zero_diff``; that observation never affects the
    marker cross-check or the outcome.
    """
    # Deferred to avoid a verify<->stories import cycle: stories imports
    # read_frontmatter/status_of from this module at top level, so verify must not
    # import stories at module scope (keep this local on any future refactor).
    from . import stories

    rj = result_mapping(result_json)
    story_id = str(task.story_key).strip()
    state = stories.resolve_story_spec(spec_folder, story_id)
    if state.kind == stories.KIND_PENDING:
        return VerifyOutcome.retry(f"no story spec found for id {story_id!r} under {spec_folder}")
    if state.kind == stories.KIND_AMBIGUOUS:
        names = ", ".join(p.name for p in state.paths)
        return VerifyOutcome.retry(f"ambiguous story file match for id {story_id!r}: {names}")
    if state.kind == stories.KIND_SENTINEL:
        return VerifyOutcome.retry(
            f"story {story_id!r} resolved to a {state.sentinel_kind} sentinel: {state.path}"
        )
    spec_path = state.path
    # The glob is `<id>-*.md`, so this holds by construction — assert it anyway as
    # a defensive gate against a future resolver change silently widening the match.
    if spec_path is None or not spec_path.name.startswith(f"{story_id}-"):
        return VerifyOutcome.retry(
            f"resolved story spec {spec_path} does not match id {story_id!r}"
        )
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # Generic path always self-finalizes to done (no in-review handoff); the
    # review_enabled arm mirrors verify_dev for symmetry. A plan-halt leg instead
    # expects the ready-for-dev plan gate (the plan is done, not the code).
    if plan_halt:
        # A clean plan-halt also carries devcontract's plan_halt marker; a
        # died-mid-flight ready-for-dev (synthesized without plan_halt) never
        # does. Cross-check the verify-side flag against the synth-side result so
        # a caller can't unilaterally promote a mid-flight spec to a "successful
        # plan" — mirrors the defensive id-prefix gate above.
        if rj.get("plan_halt") is not True:
            return VerifyOutcome.retry(
                "plan_halt verification requested but result.json carries no plan_halt marker"
            )
        expected = PLAN_HALT_STATUS
    else:
        expected = "in-review" if review_enabled else "done"

    # Stories mode adds the spec folder's stories/ subdir + stories.yaml on top of
    # the gate's own file-granular exclude — NOT a whole-folder artifact exclusion,
    # so a story whose entire authorized scope is ledger/spec reconciliation
    # doesn't register as a false "no changes". A plan-halt leg produced only its
    # own spec (the plan), so it skips the gate but passes this same tuple to the
    # observer: the journal answer must measure exactly the gate that was waived,
    # including engine-written paths.
    stories_exclude = _stories_relpaths(paths.repo_root, spec_folder) + engine_written
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status=expected,
        # Rooted where the proof-of-work gate invokes git (`paths.repo_root`), not
        # on `paths.project`: a pathspec relative to the other root matches nothing
        # and the exclusion evaporates without an error (#716).
        extra_exclude=None if plan_halt else stories_exclude,
        observe_skipped_proof=stories_exclude if plan_halt else None,
    )
    if gate.outcome is not None:
        return gate.outcome

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed(
        plan_halt_zero_diff=(gate.skipped_proof_zero_diff if plan_halt else None)
    )


def _stories_relpaths(root: Path, spec_folder: Path) -> tuple[str, ...]:
    """Proof-of-work exclude prefixes for the story record + manifest: the spec
    folder's ``stories/`` subdir and its ``stories.yaml``, relative to ``root``.
    Empty when the spec folder is outside that tree (nothing to exclude there).

    ``root`` is the tree git is invoked against — `paths.repo_root` at the one
    production call site. Under a disjoint sibling `repo_root` override the spec
    folder sits outside the code tree and this correctly returns ``()``. Under a
    nested-monorepo override it remains inside that tree and returns non-empty
    paths carrying the project prefix; dropping that prefix would instead name a
    plausible outer-tree location."""
    from .stories import STORIES_FILENAME, STORIES_SUBDIR

    try:
        rel = spec_folder.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return ()
    base = "" if rel == "." else f"{rel}/"
    return (f"{base}{STORIES_SUBDIR}", f"{base}{STORIES_FILENAME}")


# A hard ceiling on how much of one verifier stream is held in memory, separate
# from and far above `[verify] stream_capture_kb` (which bounds what reaches
# disk). `subprocess.run(capture_output=True)` already materialises a command's
# whole output, but before this bound the full streams were then RETAINED in the
# results list while every later command ran, so peak memory grew with the number
# of configured verify commands rather than with the largest one. Plugins are
# meant to see the streams essentially whole, so this is a backstop against a
# pathologically chatty suite, not a tuning knob — deliberately a constant, and
# deliberately high enough that ordinary suites never reach it.
#
# It bounds retention, not capture: while command N runs, memory still holds the
# capped earlier results plus whatever N itself emits.
MAX_STREAM_MEMORY_BYTES = 32 * 1024 * 1024


def byte_tail(text: str, max_bytes: int) -> tuple[str, int]:
    """``(tail, full_bytes)`` — ``text`` cut to its last ``max_bytes`` UTF-8 bytes.

    The one implementation of a rule this feature applies at two different
    bounds (this in-memory ceiling and the engine's `stream_capture_kb` disk
    cap), because the subtle half is easy to get wrong twice: a byte cut can
    land mid-character, and the leading partial is DROPPED rather than decoded
    into a ``\ufffd`` this function would be inventing. Decoding with
    ``errors="replace"`` instead would also break the cap it is enforcing —
    ``\ufffd`` is three UTF-8 bytes standing in for the one it replaces, so the
    result can exceed ``max_bytes``.

    ``full_bytes`` always measures the input, so a caller can report what was
    emitted even after keeping less of it. The TAIL is kept: a failing suite
    puts its failure at the end. ``max_bytes <= 0`` needs no branch — the slice
    is empty by construction, which is exactly "keep nothing".
    """
    encoded = text.encode("utf-8")
    full_bytes = len(encoded)
    if full_bytes <= max_bytes:
        return text, full_bytes
    return encoded[full_bytes - max_bytes :].decode("utf-8", errors="ignore"), full_bytes


@dataclass(frozen=True)
class CommandResult:
    """One verifier subprocess result.

    ``output_tail`` remains the merged, bounded compatibility field used by the
    existing failure classifiers and repair feedback.  ``stdout`` and ``stderr``
    retain the separate streams observed at the subprocess boundary so the
    engine can expose them to trusted plugins and retain them by journal pointer.

    ``*_full_bytes`` is what the command EMITTED, which is only interesting when
    it differs from the stream beside it — i.e. when ``MAX_STREAM_MEMORY_BYTES``
    cut one. ``None`` means nothing was cut and the stream is the whole of it, so
    the many callers that build a result from three fields stay correct without
    knowing this exists.

    ``spawn_error`` is the discriminator for the one shape that has no return
    code at all: the child was never started. The typical cause is the ``cwd``
    it was to run in — missing, not a directory, or unsearchable — and the
    message names that directory as context, but the fault is caught as any
    spawn-time ``OSError`` or ``ValueError`` and the set is not closed: a missing
    shell, EMFILE, ENOMEM, or an embedded NUL reach the same field, and the
    wrapped exception is what says which. ``None`` on every result that came from
    a process that actually ran — including a timeout, which ran and hung. It is
    LAST and defaulted because the construction sites pass three to seven
    POSITIONAL arguments; a field inserted anywhere else would silently re-bind
    them.
    """

    command: str
    returncode: int
    output_tail: str
    stdout: str = ""
    stderr: str = ""
    stdout_full_bytes: int | None = None
    stderr_full_bytes: int | None = None
    spawn_error: str | None = None


# The synthetic return code on a result whose child never started.
#
# The magnitude is the load-bearing part. On POSIX ``subprocess`` reports ``-N``
# for a child KILLED BY signal N, so every small negative integer is a real
# return code some child can produce: ``-2`` is SIGINT, ``-9`` SIGKILL, ``-15``
# SIGTERM. A sentinel inside that range would be indistinguishable from a
# verify command the operator (or an OOM killer) had just killed. 1000 is far
# above the largest real-time signal any platform defines, so this value cannot
# be minted by a child that ran.
#
# Negative because two live arms depend on the sign: the win32 probe's
# ``returncode < 0`` early-out, and the ordinary ``returncode != 0`` failure arm
# that must still read it as a failure if anything ever reaches that far. And
# distinct from the timeout leg's ``-1``, because both are "no exit status
# exists" sentinels and a reader that conflated them would read a child that
# never started as one that ran and hung.
#
# ``spawn_error`` — not this code — is what the classifiers key on; the code
# exists so the journal record and the plugin payload carry an rc that no real
# child could have produced.
SPAWN_FAULT_RC = -1000

# The sink a caller hands :func:`verify_commands_outcome` to observe the results
# it is about to classify — the engine journals review-gate results through it.
CommandSink = Callable[[tuple[CommandResult, ...]], None]


# sh launcher convention (verify commands run shell=True): 126 = command found
# but not executable, 127 = command not found. Both are environment faults —
# deterministic for a given tree, unfixable by a repair session (issue #126:
# seeded worktrees that lost +x burned dev attempts on no-op repairs).
ENV_FAULT_RCS = frozenset({126, 127})

# cmd has no such convention (issue #302): `cmd /c
# <missing tool>` exits 1 — the same code an ordinary test failure uses — and
# 9009 surfaces only as %ERRORLEVEL% *inside* a batch file, so it reaches us
# only when the verify command is itself a .cmd/.bat propagating it. Worse,
# handing cmd a file it cannot execute (extension not in PATHEXT: a .sh, a
# .txt) exits 0 without running it, so the check silently "passes". The win32
# arm therefore classifies on three independent signals instead of the rc.
_CMD_ENV_FAULT_RC = 9009

# Matched against the tail's last two non-empty lines only, because cmd writes its
# message to stderr and ``run_verify_commands`` builds the tail as stdout + stderr —
# so the message lands at the end, and it wraps ("… is not recognized as an internal
# or external command,\noperable program or batch file."). Note what that ordering
# does *not* buy: stderr is appended wholesale, not interleaved, so a command whose
# own stderr ends with the phrase is read as a fault too. That is the accepted edge —
# a verify command whose last stderr line is "X is not recognized" has a missing X
# either way. Localized Windows prints neither phrase; the token probe covers those.
_CMD_NOT_RECOGNIZED = "is not recognized as an internal or external command"
_CMD_ACCESS_DENIED = "access is denied"
_CMD_MESSAGE_LINES = 2

# cmd's *internal* commands: shutil.which cannot resolve them, so without this
# allowlist every failing `if exist …` / `exit 1` would classify as an env
# fault. External tools (findstr, robocopy, …) resolve through which as usual.
_CMD_BUILTINS = frozenset(
    "assoc break call cd chdir cls color copy date del dir echo endlocal erase exit for"
    " ftype goto if md mkdir mklink move path pause popd prompt pushd rd rem ren rename"
    " rmdir set setlocal shift start time title type ver verify vol".split()
)


_CMD_METACHARS = ("%", "!", "<", ">", "&", "|", ";", "^")


def _leading_token(command: str) -> str | None:
    """The executable part of a shell command string, or None when nothing about
    it can be probed. ``posix=False`` keeps Windows backslashes intact (posix mode
    eats them), at the cost of leaving quotes on the token — hence the strip.

    Returning None is the safe answer: it drops the probe, which can only ever
    *add* an env fault. A token cmd would expand before running (``%VAR%``,
    delayed ``!VAR!``) is unprobeable for that reason — resolving the literal
    would report "not found" for a tool that is right there."""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:  # unbalanced quotes — no reliable token to probe
        return None
    if not parts:
        return None
    # `(pytest -q)` tokenizes to `(pytest`, `(pytest)` to `(pytest)` — cmd's
    # grouping parens and echo-suppressing `@` are shell syntax, not the name.
    token = parts[0].strip('"').strip("()@")
    # Anything the shell would still act on is not a name to probe: expansion
    # (%VAR%, delayed !VAR!), redirection, or an operator shlex left attached
    # (`pytest|findstr x` splits to one token). Probing those reports "not found"
    # for a tool that is right there, so drop the probe instead.
    if not token or any(char in token for char in _CMD_METACHARS):
        return None
    return token


def _cmd_executable(path: Path) -> bool:
    """Whether cmd resolves this path directly or by appending PATHEXT."""
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    extensions = tuple(ext.strip().lower() for ext in pathext.split(";") if ext.strip())
    return (path.is_file() and path.suffix.lower() in extensions) or any(
        Path(f"{path}{ext}").is_file() for ext in extensions
    )


def _win32_env_fault_reason(result: CommandResult, cwd: Path) -> str | None:
    """Windows env-fault evidence, cheapest signal first, or None. Each signal is
    independently sufficient; see the _CMD_* constants for why the rc alone isn't."""
    if result.returncode < 0:
        # One of the two "no exit status" sentinels, or a signal-killed child.
        # None of the signals below can apply to any of them, though for opposite
        # reasons: a timeout (`-1`) and a signal death mean the command WAS found
        # and WAS runnable, while a spawn fault (`SPAWN_FAULT_RC`) means no child
        # existed to probe — and that one is already answered by `spawn_error`,
        # ahead of this function being called at all (see `env_fault_reason`).
        return None
    if result.returncode == _CMD_ENV_FAULT_RC:
        return f"rc={_CMD_ENV_FAULT_RC} — cmd reported the command as not found"
    lines = [line.strip() for line in result.output_tail.splitlines() if line.strip()]
    if result.returncode != 0 and lines:
        closing = " ".join(lines[-_CMD_MESSAGE_LINES:])
        if _CMD_NOT_RECOGNIZED in closing.lower():
            return f"cmd: {closing}"
        if _CMD_ACCESS_DENIED in lines[-1].lower():
            return f"cmd: {lines[-1]}"
    token = _leading_token(result.command)
    if token is None:
        return None
    token_path = cwd / token
    try:
        names_a_file = token_path.is_file()
        local_executable = _cmd_executable(token_path)
    except OSError:  # a token no filesystem call can even ask about
        return None
    if names_a_file and not local_executable and token.lower() not in _CMD_BUILTINS:
        # The builtin guard is the same one the PATH branch carries, for the other
        # half of cmd's resolution order: an *unqualified* internal name is answered
        # internally, before any directory is searched, so a file that happens to be
        # named `echo` / `set` / `start` is never what runs and must not be read as a
        # broken one. Only the bare name is exempt — `.\echo`, `C:\…\echo` are file
        # references cmd does try to run, and they still classify below.
        #
        # PATHEXT is cmd's contract for what it *runs*; anything else it hands to
        # the file association. For the extensions this is about (.sh, .txt) that
        # association executes nothing and returns 0, so a green rc means nothing
        # was verified. An extension with a console association outside PATHEXT
        # (.rb, .pl on a host that registered them) does run — and is escalated
        # here anyway, deliberately: what an association returns is the *app's*
        # convention, not the script's, so it is not an exit code to gate on.
        # The message names the token, so the fix ("ruby check.rb") is obvious.
        return f"{token} is not executable by cmd (extension not in PATHEXT)"
    if (
        result.returncode != 0
        and not local_executable  # cmd searches the run's own directory before PATH
        and token.lower() not in _CMD_BUILTINS
        and shutil.which(token) is None
    ):
        return f"{token} not found on PATH"
    return None


def env_fault_reason(result: CommandResult, cwd: Path) -> str | None:
    """Why this verify command is an environment fault rather than a story
    failure, or None if it is not one. Per-shell: verify commands run through
    the host shell, and sh and cmd signal a broken environment differently.

    ``spawn_error`` is answered FIRST and unconditionally, before any rc reading
    and before the win32 probe. Not merely an ordering preference: the probe
    resolves a command's leading token as ``cwd / token`` to decide whether the
    tool exists, and on this leg no child was started, so that lookup is about a
    directory nothing ever entered and cannot speak to why. The result also
    carries no exit status to read (see :data:`SPAWN_FAULT_RC`), which is why
    the rc arms cannot classify it either."""
    if result.spawn_error is not None:
        return result.spawn_error
    if result.returncode in ENV_FAULT_RCS:
        return f"rc={result.returncode}"
    if sys.platform != "win32":
        return None
    return _win32_env_fault_reason(result, cwd)


def _timeout_stream(value: str | bytes | None) -> str:
    """Normalize optional timeout output into what the completed path would give.

    ``subprocess.run``'s timeout leg is not uniform, so three shapes arrive:

    * ``bytes`` — POSIX. ``Popen._communicate`` raises ``TimeoutExpired`` from
      ``_check_timeout`` with the raw chunks joined, *before* the text-mode
      decode that ends the loop, so ``text=True`` never touched them.
    * ``str`` — Windows, where ``run`` calls ``communicate()`` after ``kill()``
      and the text wrapper has already decoded. Load-bearing: on that platform
      this branch is the only way the output arrives at all.
    * ``None`` — POSIX again, when nothing had been buffered on that stream.

    So the bytes branch has to reproduce what text mode would have done to them,
    which is exactly ``Popen._translate_newlines``: decode, then collapse ``\\r\\n``
    and lone ``\\r`` to ``\\n``. Doing neither made the same bytes read back
    differently depending on which path produced them — under an ASCII locale
    ``b"caf\\xc3\\xa9\\r\\n"`` completed as ``"caf\\ufffd\\ufffd\\n"`` but timed out
    as ``"café\\r\\n"``. The codec half also contradicted
    :func:`run_verify_commands`' own rule (#378) that host-tool output stays on
    the locale codec: ``locale.getpreferredencoding(False)`` is what ``text=True``
    resolves for an unset ``encoding`` — deliberately not ``locale.getencoding()``,
    which disagrees with it under UTF-8 mode (PEP 540), a mode the C/POSIX locale
    enables by itself. ``errors="replace"`` for the reason the completed path uses
    it: one undecodable byte must not raise and lose every result.

    The str branch is left alone: its newlines were translated by the text
    wrapper the reader thread read through, so there is nothing left to collapse."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        decoded = value.decode(locale.getpreferredencoding(False), errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")
    return value


def run_verify_commands(policy: Policy, cwd: Path) -> list[CommandResult]:
    """Run each of the policy's verify commands, one CommandResult apiece.

    Output decodes with ``errors="replace"`` (#378): the children are arbitrary
    operator tools whose bytes are not ours to constrain, the captured tail is
    display-only feedback for a human or a repair session (already lossy at
    ``[-2000:]``), and one undecodable byte must not raise mid-loop and lose
    *every* command's result. Decoding stays on the locale codec (``text=True``)
    precisely because these are host tools — contrast tui/launch.py, which pins
    ``encoding="utf-8"`` because its child is our own UTF-8 CLI.

    "One apiece" holds across all three legs: a completed child, a timeout, and a
    child that could never be spawned each append exactly one result and the loop
    goes on to the next command. The three are told apart on the result itself —
    an rc for the first, ``rc=-1``/``"timed out"`` for the second,
    ``spawn_error`` plus :data:`SPAWN_FAULT_RC` for the third."""
    results = []
    for command in policy.verify.commands:
        try:
            # Verify commands are operator-authored shell strings from the project's
            # policy (e.g. "pytest -q && ruff check"); shell=True is intentional here.
            proc = subprocess.run(  # nosec B602
                command,
                shell=True,  # portability: operator-authored verify command — sanctioned shell-out (see plan out-of-scope)
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            # the timeout leg is bounded too: a command killed at COMMAND_TIMEOUT_S
            # is exactly the one that may have been spewing output when it died.
            t_out, t_out_full = byte_tail(_timeout_stream(exc.stdout), MAX_STREAM_MEMORY_BYTES)
            t_err, t_err_full = byte_tail(_timeout_stream(exc.stderr), MAX_STREAM_MEMORY_BYTES)
            results.append(
                CommandResult(command, -1, "timed out", t_out, t_err, t_out_full, t_err_full)
            )
            continue
        except (OSError, ValueError) as exc:
            # The child was never started, so no exit status exists to classify:
            # `subprocess.run` raises out of the fork/exec (or CreateProcess)
            # itself when `cwd` is unusable — FileNotFoundError (missing),
            # NotADirectoryError (a regular file, or a path beneath one),
            # PermissionError (a directory without +x) — or raises ValueError
            # before spawn when the command or cwd contains an embedded NUL.
            # The OSError arm uses the base class rather than the three names
            # because they are the reachable OS shapes TODAY, not a closed set:
            # the base class is what the platform actually guarantees, and one
            # uncaught sibling here crashes the whole run.
            #
            # Translated instead of raised, the same doctrine `_run_git` follows
            # for the faults that land before a return code exists (#343): left
            # uncaught this escapes every `except` in the engine's verification
            # path and ends the run as a crash, when the fact it reports — a cwd
            # no command can run in — is a textbook environment fault, identical
            # for every story and unfixable by a repair session.
            #
            # A result is APPENDED and the loop CONTINUES, honouring this
            # function's documented "one CommandResult apiece": a caller zipping
            # results against `policy.verify.commands` must not silently lose the
            # tail of the list to the first broken spawn.
            results.append(
                CommandResult(
                    command,
                    SPAWN_FAULT_RC,
                    f"{type(exc).__name__}: {exc}",
                    # What was OBSERVED, not a diagnosis. `except OSError` is
                    # wider than the cwd shapes that motivated it — a missing
                    # `/bin/sh`, EMFILE, ENOMEM all land here — so the cwd is
                    # named as context ("cwd was X") rather than blamed, and the
                    # exception carries whatever the real cause was. No "could
                    # not run" phrasing: `cli._reverify` prefixes its own
                    # ("<cmd>' could not run: ..."), and the two stuttered.
                    spawn_error=(f"child not started; cwd was {cwd}; {type(exc).__name__}: {exc}"),
                )
            )
            continue

        # Keep result processing outside the spawn-fault handler. A ValueError
        # here is a programmer defect, not rejected process configuration, and
        # must remain fail-loud rather than being mislabeled as an environment
        # fault.
        stdout, stdout_full = byte_tail(proc.stdout, MAX_STREAM_MEMORY_BYTES)
        stderr, stderr_full = byte_tail(proc.stderr, MAX_STREAM_MEMORY_BYTES)
        # merged from the ceilinged streams, not the raw pair: 2000 chars sits
        # far below the ceiling, so the tail is identical while the full
        # concatenation — a transient copy of both whole streams — is not built.
        output = (stdout + stderr)[-2000:]
        results.append(
            CommandResult(
                command, proc.returncode, output, stdout, stderr, stdout_full, stderr_full
            )
        )
    return results


def verify_command_results_outcome(results: list[CommandResult], cwd: Path) -> VerifyOutcome:
    """Classify already-observed verifier results without discarding them.

    Kept separate from :func:`verify_commands_outcome` so the engine can retain
    and expose exactly the same results it asks core to classify. Failures are fixable:
    the captured output is concrete feedback a repair session can act on —
    except environment faults (see env_fault_reason), which escalate so the run
    pauses for an environment fix instead of burning story budgets. An env
    fault anywhere in the run wins over earlier ordinary failures: a repair
    session dispatched for the ordinary failure would still run in the
    broken environment. Note the first loop inspects rc=0 results too — on
    Windows an unrunnable command is a silent pass, not a failure (#302)."""
    for result in results:
        reason = env_fault_reason(result, cwd)
        if reason is not None:
            # The explanatory clause branches on WHICH fault this is, because the
            # rc-based one is a claim about the command and the spawn one is not:
            # a child that never started was never looked for, so "command not
            # found / not executable" would send the reader hunting for a binary
            # when the directory is what is broken. Everything after the dash is
            # shared — the remedy (fix the environment, re-arm) is the same.
            clause = (
                "the command could not be started at all"
                if result.spawn_error is not None
                else "command not found / not executable"
            )
            output = "" if result.spawn_error is not None else f"\n{result.output_tail}"
            return VerifyOutcome.escalate(
                f"verify environment fault ({reason}): {result.command}\n"
                f"{clause} — this is the run environment, "
                "not the story; fix the environment, then re-arm the escalation "
                f"(the attempt budget resets on re-arm){output}",
                env_fault=True,
            )
    for result in results:
        if result.returncode != 0:
            return VerifyOutcome.retry(
                f"verify command failed (rc={result.returncode}): {result.command}\n"
                f"{result.output_tail}",
                fixable=True,
            )
    return VerifyOutcome.passed()


def verify_commands_outcome(
    policy: Policy, cwd: Path, *, on_results: CommandSink | None = None
) -> VerifyOutcome:
    """Run the policy's deterministic verify commands and classify the results.

    ``on_results`` observes the results BEFORE they are classified, which is the
    same order ``Engine._verify_commands_with_results`` uses on the dev side:
    journal first, decide second, so the record exists whatever the classifier
    then does with it — including an escalation that ends the run. It is called
    exactly once per invocation, with an empty tuple when no commands are
    configured, because "the pass ran and executed nothing" and "no pass ran" are
    different facts and only the second one is signalled by never getting here.

    The contract on the sink is that IT must not raise; this function adds no
    guard of its own, deliberately. The engine's sink
    (``_journal_verify_command_results``) degrades on stream-capture faults — an
    ``OSError`` from a ``verify/`` write becomes a ``capture_error`` field — but
    the ``Journal.append`` beneath it has no handler, so ENOSPC or a read-only run
    dir still propagates. That is the same fail-loud boundary the dev leg already
    stands on, and wrapping the call here would trade it for silence: a lost
    journal write is a lost audit record, which is exactly the class of failure
    that must not pass quietly."""
    results = run_verify_commands(policy, cwd)
    if on_results is not None:
        on_results(tuple(results))
    return verify_command_results_outcome(results, cwd)


def _verify_review_commands(
    policy: Policy, paths: ProjectPaths, *, on_results: CommandSink | None = None
) -> VerifyOutcome:
    """Run a review gate's ``[verify] commands`` in ``paths.repo_root``.

    The two roots split by what is being addressed, and the split is deliberate:
    the artifacts these gates read — the claimed spec, ``paths.sprint_status``,
    ``paths.deferred_work`` — are BMAD output and stay project-rooted, while
    ``[verify] commands`` are the operator's build/test verbs and belong in the
    git root the code lives in. Every other caller of these commands already
    resolves them that way: the dev side runs them in ``Workspace.root``
    (``Engine._verify_commands_with_results``), which ``Workspace.default`` sets
    from ``paths.repo_root``, and ``cli._reverify`` is handed ``paths.repo_root``
    at both of its call sites. The three review gates were the sole outlier
    (#695).

    The two roots are the same path in the default layout and under worktree
    isolation (``ProjectPaths.rebased`` sets both); they diverge only under an
    explicit ``repo_root:`` with ``isolation = "none"``. One helper rather than
    three edited lines so the three gates cannot drift apart on the split.

    On win32 the cwd carries one more thing with it, so the split is not purely a
    subprocess concern: ``verify_commands_outcome`` forwards ``cwd`` a second time
    into ``env_fault_reason`` -> ``_win32_env_fault_reason``, which resolves a
    command's leading token as ``cwd / token`` to tell "tool missing" from "command
    failed" — and an env fault escalates where a plain failure retries. So a
    RELATIVE verify command is now classified against ``repo_root`` on these legs
    too. That is the correct direction (classification should follow execution, and
    the dev side already classifies against the same root), but it is a second
    consequence of the move rather than a restatement of the first.

    ``paths.repo_root`` is the ONLY member of ``paths`` this reads — it takes the
    whole dataclass to keep the three call sites uniform, not because it consults
    anything else. A future caller must not infer that artifact paths reach here.

    ``on_results`` is forwarded, not consumed: an engine-supplied sink is how
    review-gate results reach the journal, which the dev side has always had and
    these gates had not. Optional, so the gates stay callable from core (and from
    tests) with no engine at all — no sink simply means nothing is recorded,
    which is what every direct caller got before.

    This is also the ONLY sanctioned caller of ``verify_commands_outcome``; a
    fourth gate reaching past it would re-open #695. Enforced, not merely stated
    — see ``tests/test_portability_guard.py``.
    """
    return verify_commands_outcome(policy, paths.repo_root, on_results=on_results)


def verify_review(
    task: StoryTask,
    paths: ProjectPaths,
    policy: Policy,
    *,
    sprint_reached_done: bool = False,
    operator_park: bool = False,
    on_results: CommandSink | None = None,
) -> VerifyOutcome:
    """Gate a completed review pass: spec at ``done``, sprint-status at ``done``,
    deterministic verify commands green.

    ``sprint_reached_done`` tells the gate that the orchestrator had already
    advanced this story's sprint-status to ``done`` before the review ran (it is
    the sole ``sprint_advance`` caller, ``verify_dev`` asserted the write landed,
    and ``advance`` never regresses). A board now sitting *earlier* than ``done``
    is therefore not a stage the story never reached — it is a review session
    deliberately revoking the sign-off. Nothing in the review loop re-advances
    the board, so retrying only replays the same failure until the budget runs
    out and the work is rolled back; under
    ``review.on_status_contradiction = "escalate"`` (the default) the gate
    escalates instead, naming both sides. See #334.

    ``(awaiting-operator, awaiting-operator)`` is the second accepted pair, on
    the same observed-spec-status selection ``verify_dev`` uses: this is the gate
    the park path runs before committing (``Engine._park_awaiting_operator``), so
    parked work clears exactly the deterministic checks every other commit path
    clears *at this gate* — the pair, a non-empty action list, and the verify
    commands. The scope is load-bearing: a ``done`` story additionally clears
    proof-of-work at the dev gate, which a park no longer does (#676), so this
    gate is not evidence that a park faced every check a ``done`` story faced. The
    sign-off-regression arm stays scoped to the ``done`` pair: a board short of
    ``awaiting-operator`` is a stage never reached, not a revoked sign-off.

    ``operator_park`` is the SAME engine-supplied flag ``verify_dev`` takes, not a
    second reading of ``policy.operator.enabled``, so the two gates cannot
    disagree about whether this run parks. They would: the engine's
    ``_operator_park_enabled`` is an override seam, and a mode that opts out of
    parking while still reaching this gate would otherwise find it accepting a
    park the engine itself refuses to take.

    ``on_results`` is handed straight to ``_verify_review_commands`` and is the
    engine's hook for journalling this gate's verifier results; see there. It is
    invoked only if the gate reaches its commands — an earlier refusal ran
    nothing, so there is nothing to record."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    expected = AWAITING_OPERATOR if (operator_park and status == AWAITING_OPERATOR) else "done"
    if status != expected:
        return VerifyOutcome.retry(f"spec status is {status!r}, expected {expected!r}")
    if expected == AWAITING_OPERATOR:
        actions = _operator_actions_gate(fm, task.story_key)
        if actions is not None:
            return actions

    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != expected:
        if expected == "done" and _is_signoff_regression(sprint, sprint_reached_done, policy):
            return VerifyOutcome.escalate(
                f"review revoked the sprint sign-off for {task.story_key}: the "
                f"orchestrator advanced the board to 'done' after dev verified, "
                f"and the review session wrote it back to {sprint!r} while leaving "
                f"the spec frontmatter at 'done'. The two sides disagree about "
                f"whether the story is finished, and no further review cycle can "
                f"reconcile them — the review loop never re-advances the board, so "
                f"the remaining cycles would burn down onto a defer that rolls the "
                f"work back. Resolve by either completing the outstanding work and "
                f"re-arming the escalation (the attempt budget resets on re-arm), "
                f"or accepting the story and advancing the board yourself; set "
                f'review.on_status_contradiction = "retry" to restore the legacy '
                f"retry-until-budget behavior.",
                contradiction=True,
            )
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected {expected!r}"
        )

    return _verify_review_commands(policy, paths, on_results=on_results)


def _is_signoff_regression(sprint: str | None, sprint_reached_done: bool, policy: Policy) -> bool:
    """Whether a non-``done`` sprint status is a review deliberately walking the
    board backward, as opposed to a stage the story simply never reached.

    Conservative on every uncertainty: without the launch-time guarantee, with
    the knob set to ``retry``, or when the fresh read yields no status at all
    (missing story entry) or a token outside the known lifecycle (a hand-edited
    or future board), the caller falls through to the ordinary retry — a wrong
    escalation halts an otherwise healthy run."""
    if not sprint_reached_done or policy.review.on_status_contradiction != "escalate":
        return False
    if sprint is None or sprint not in STATUS_ORDER:
        return False
    return STATUS_ORDER.index(sprint) < STATUS_ORDER.index("done")


def verify_review_stories(
    task: StoryTask,
    paths: ProjectPaths,
    policy: Policy,
    *,
    on_results: CommandSink | None = None,
) -> VerifyOutcome:
    """verify_review for stories mode: same spec-done + verify-commands gates,
    minus the sprint-status gate (stories mode has no sprint board — the story
    spec's own frontmatter status is authoritative). ``task.spec_file`` is the
    id-keyed story spec ``verify_dev_stories`` recorded on the dev pass.

    ``on_results`` is handed straight to ``_verify_review_commands`` and is the
    engine's hook for journalling this gate's verifier results; see there. It is
    invoked only if the gate reaches its commands — an earlier refusal ran
    nothing, so there is nothing to record."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")
    return _verify_review_commands(policy, paths, on_results=on_results)


def verify_review_bundle(
    task: StoryTask,
    paths: ProjectPaths,
    policy: Policy,
    *,
    on_results: CommandSink | None = None,
) -> VerifyOutcome:
    """verify_review for a deferred-work bundle: no sprint-status check, but
    every dw id the bundle owns must be marked done in the ledger on disk. The
    legacy --dw-bundle skill flips them; on the generic bmad-build-auto path the
    orchestrator flips them after dev and, if review rewrites the ledger diff,
    again immediately before this review gate. Either way this gate is why we
    can trust it happened.

    ``on_results`` is handed straight to ``_verify_review_commands`` and is the
    engine's hook for journalling this gate's verifier results; see there. It is
    invoked only if the gate reaches its commands — an earlier refusal ran
    nothing, so there is nothing to record."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    ledger = paths.deferred_work
    # Same TOCTOU class as the spec read above: the ledger is rewritten by the
    # orchestrator's own mark_done between the dev and review gates.
    # OBSERVATION arm of the ledger-read contract (DW-146): this check writes
    # nothing and already degrades into the `retry` it returns. `ValueError` is in
    # the tuple because neither of its two arrivals is an `OSError`: DW-146 added
    # `UnicodeDecodeError` (a `ValueError` subclass) when undecodable bytes escaped
    # this arm entirely and aborted the verify instead of retrying it, and the
    # `stat` probe below raises a plain `ValueError` for an embedded NUL in the
    # configured path and a `UnicodeEncodeError` for a lone surrogate, which
    # `is_file()` had answered False for — an observation arm attributes those as
    # a fault, never as absence, so they take the same retry.
    # The presence probe is `stat` + `S_ISREG` INSIDE the `try` (DW-267), so a
    # refused probe is the "unreadable" retry below and not the "entries not
    # marked done" one: the `is_file()` it replaced suppresses every OS error on
    # Python 3.14 and answers False, so a refused ledger read as an empty one and
    # the verify retried, fixable, with a misleading verdict naming every id.
    try:
        try:
            text = ledger.read_text(encoding="utf-8") if S_ISREG(ledger.stat().st_mode) else ""
        except (FileNotFoundError, NotADirectoryError):
            text = ""
    except (OSError, ValueError) as exc:
        return VerifyOutcome.retry(
            f"deferred-work ledger unreadable ({exc.__class__.__name__}: {exc}): {ledger}"
        )
    entries = {e.id: e for e in deferredwork.parse_ledger(text)}
    not_done = sorted(
        i for i in task.dw_ids if i not in entries or not entries[i].status.startswith("done")
    )
    if not_done:
        return VerifyOutcome.retry(
            "deferred-work entries not marked done in "
            f"{ledger}: {', '.join(not_done)} — set each to `status: done <date>` "
            "with a `resolution:` line",
            fixable=True,
        )

    return _verify_review_commands(policy, paths, on_results=on_results)


def commit_story(repo: Path, message: str) -> str:
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


def finalize_commit(
    repo: Path,
    baseline: str | None,
    message: str,
    *,
    staged_validator: Callable[[], object] | None = None,
    committed_validator: Callable[[str, object], None] | None = None,
) -> str | None:
    """Collapse everything since `baseline` into ONE commit with `message`.

    bmad-build-auto now commits its own work at the end of each iteration (one
    commit for the dev pass, one for each follow-up review pass), while the
    orchestrator still writes its own bookkeeping (sprint-status.yaml for
    stories, the deferred-work ledger for sweep bundles) into the working tree
    uncommitted. This squashes that whole chain — the skill's per-iteration
    commits PLUS the orchestrator's uncommitted writes — back onto `baseline`
    as a single commit carrying the orchestrator's message, so the one-commit-
    per-story invariant and the message template / pre_commit hook stay
    authoritative regardless of how many times the skill committed.

    Mechanics: stage the working tree (`add -A`), invoke the optional exact-index
    validator, move HEAD back to `baseline` keeping that same index (`reset
    --soft`), then commit the accumulated index without restaging. A post-commit
    HEAD probe and optional committed-tree validator detect an uncertain commit
    identity, hook mutation, or concurrent-index mutation and roll HEAD back to
    the original chain before refusing. The rollback leaves working-tree contents
    untouched, including changes made by hooks before the refusal.

    The no-op arm is validated the same way. "Nothing staged" is read off the
    index AFTER the staged validator returned, so an index reset to `baseline`
    inside that window (a concurrent writer — the same class the committed-tree
    validator exists for) reads as a clean no-op while the validated snapshot
    says a deliverable was staged. Returning `None` there would leave HEAD at
    `baseline` with the accepted chain orphaned and let the caller record
    `baseline` as the commit — a bundle closing without the pending-tracked
    deliverable it was accepted on (#795 review). So when a committed-tree
    validator is given, the no-op arm runs it against `baseline` itself: the
    snapshot must already be IN the baseline tree for "nothing to commit" to be
    true, and a disagreement restores the original chain and index (`reset
    --mixed`) before the refusal propagates.

    Residual-artifacts note (BMAD-METHOD #2563): the skill now commits every file
    of the reviewed diff and deliberately leaves unrelated `git status` residue
    uncommitted (files outside the change's scope). The `add -A` here sweeps that
    residue into the story commit too — an intentional divergence from the skill's
    scoped commit. The loop must end each story on a clean tree because story
    N+1's step-01 HALTs on a dirty tree, so the orchestrator squashes EVERYTHING
    since baseline (skill commits + its own bookkeeping + any residue) into the one
    story commit rather than leaving the tree dirty for the next story to trip on.

    Returns the new HEAD sha, or None when there is nothing to finalize: no
    version control (`baseline` falsy or NO_VCS) or the tree already equals
    `baseline` (no skill commits and no bookkeeping delta)."""
    if not baseline or baseline == "NO_VCS":
        return None
    original_head = rev_parse_head(repo)
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    staged_snapshot = staged_validator() if staged_validator is not None else None
    rc, out = _git(repo, "reset", "--soft", baseline)
    if rc != 0:
        raise GitError(f"git reset --soft {baseline} failed: {out}")
    # index now holds the cumulative diff vs baseline; nothing staged → no-op
    rc, _ = _git(repo, "diff", "--cached", "--quiet")
    if rc == 0:
        if committed_validator is not None:
            try:
                committed_validator(baseline, staged_snapshot)
            except BaseException as exc:
                # HEAD already sits at `baseline` and the index is whatever the
                # concurrent writer left; put both back on the accepted chain.
                restore_rc, restore_out = _git(repo, "reset", "--mixed", original_head)
                if restore_rc != 0:
                    raise GitError(
                        "no-op tree validation failed; additionally failed to restore "
                        f"HEAD to {original_head[:12]}: {restore_out}"
                    ) from exc
                raise
        return None
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        # The soft reset already rewound HEAD to baseline; a failed commit would
        # otherwise leave the branch pointer there, dropping the skill commit chain
        # from HEAD. Restore HEAD (the working tree is untouched) before raising.
        restore_rc, restore_out = _git(repo, "reset", "--soft", original_head)
        if restore_rc != 0:
            raise GitError(
                f"git commit failed: {out}; additionally failed to restore HEAD "
                f"to {original_head[:12]}: {restore_out}"
            )
        raise GitError(f"git commit failed: {out}")
    try:
        committed_head = rev_parse_head(repo)
        if committed_validator is not None:
            committed_validator(committed_head, staged_snapshot)
    except BaseException as exc:
        # Restore the accepted skill chain and its index while leaving the
        # working tree untouched.  A soft reset would retain an ignored path
        # that a hook force-added, making every replay fail staged validation
        # even after the accepted bytes were restored.
        try:
            restore_rc, restore_out = _git(repo, "reset", "--mixed", original_head)
        except GitError as restore_exc:
            restore_rc, restore_out = 1, str(restore_exc)
        if restore_rc != 0:
            raise GitError(
                f"post-commit finalization failed ({type(exc).__name__}: {exc}); "
                "additionally failed to restore "
                f"HEAD to {original_head[:12]}: {restore_out}"
            ) from exc
        raise
    return committed_head


def resolve_restore_path(raw: str, root: Path) -> Path:
    """The latched intent-gap patch (`StoryTask.restore_patch`) as a concrete path:
    absolute values pass through, relative ones are anchored on `root`.

    `model.StoryTask.restore_patch` documents the field as repo-relative-or-absolute,
    and every consumer must resolve it against the base it actually reads the tree
    from — the engine's live workspace root (the unit worktree under isolation),
    `paths.repo_root` for the proof-of-work exclude (which is where the gate's own
    probe runs, so the latch has to name a path in that tree; #716),
    the CLI's `--project`. Hence the caller-supplied `root` rather than one
    baked-in base.

    In practice `cli._resolve_restore_patch` always latches an already-`.resolve()`d
    absolute path, so the relative branch is exercised only by a hand-written state
    file or a future non-CLI latcher; it is kept because the field's contract
    promises it. Deliberately does NOT `.resolve()` the result — callers that need
    symlink/`..` normalization (path containment checks) do it themselves, and the
    apply/exclude paths match the pre-existing behavior byte-for-byte without it.
    """
    p = Path(raw)
    return p if p.is_absolute() else root / p


def apply_patch(repo: Path, patch_path: Path) -> None:
    """Apply a saved patch to `repo`'s working tree (`git apply`), raising on failure.

    The intent-gap patch-restore re-drive (BMAD-METHOD #2564) uses this to re-lay
    the attempted change bmad-build-auto saved before reverting. New files in the
    patch are created (they land untracked, matching how the original attempt sat
    before its revert).

    A clean apply is likely but NOT guaranteed: the patch was diffed from the
    story's ORIGINAL baseline, while re-arm advances the re-drive's baseline to the
    project's post-resolve HEAD (runs.rearm_escalation) — so the apply holds only
    while the resolve session left the patched files untouched. A resolve session
    that committed changes to those files makes `git apply` fail, deliberately
    loudly: silently merging the human's resolution with the stale attempt could
    reproduce the very gap being resolved. A non-zero `git apply` — that overlap, a
    missing/corrupt patch, any other drift — raises `GitError` with git's output;
    the caller escalates rather than dispatch a session onto a half-applied tree,
    and the human re-resolves (typically re-arming without a restore, since the
    resolution commits already carry the overlapping work).
    """
    if not patch_path.is_file():
        raise GitError(f"restore patch not found: {patch_path}")
    rc, out = _git(repo, "apply", str(patch_path))
    if rc != 0:
        raise GitError(f"git apply {patch_path} failed: {out}")


def patch_new_files(patch_path: Path) -> set[str]:
    """Repo-relative posix paths the saved patch *creates* — the untracked residue
    an `apply_patch` leaves behind (see `runs.rearm_escalation`).

    Text-parse, not `git apply --numstat`: the caller runs after the tree has moved
    on, so the patch may no longer apply, and a creation list must still come back.
    Within each `diff --git` block, an old-side `---` header naming `_DIFF_ABSENT`
    marks a creation, and the `+++ <prefix>/<path>` after it names the file. The
    prefix is stripped by mirroring what `apply_patch`'s plain `git apply` (default
    -p1) did when it laid the residue down: drop the first path component whatever
    it is — `b/` standard, `w/`/`i/`/`c/` under diff.mnemonicPrefix, `2/` from
    --no-index. A target -p1 cannot strip (no `/`, e.g. --no-prefix output) is
    skipped: that apply failed outright, so no residue exists. Deletions (the
    absent token on the *new* side) are never returned — the caller feeds this to an
    *exclusion* set, and excluding a path the human later re-created would make the
    next rollback delete their file. For the same reason every ambiguous entry is
    skipped rather than guessed: quoted paths (`+++ "b/wéird"`, core.quotePath),
    renames, and non-`git diff` unified diffs with no `diff --git` header yield fewer
    results, never wrong ones. Under-reporting degrades to the pre-#90 behavior;
    over-reporting deletes user data.

    Raises OSError / UnicodeDecodeError when the patch cannot be read; the caller
    decides (rearm treats it as best-effort and journals `stale-restore-unparseable`).
    """
    new_files: set[str] = set()
    in_hunk = False  # past the first `@@`, a `--- x` line is content, not a header
    creating = False
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git "):
            in_hunk = creating = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            continue
        elif line.startswith("--- "):
            creating = line[4:].strip() == _DIFF_ABSENT
        elif line.startswith("+++ ") and creating:
            creating = False
            target = line[4:].split("\t", 1)[0].strip()
            if target == _DIFF_ABSENT or target.startswith('"') or "/" not in target:
                continue  # delete-then-create pair, quoted path, or un-strippable target
            rel = target.split("/", 1)[1]  # mirror `git apply`'s default -p1
            if rel:
                new_files.add(rel)
    return new_files


def unpublishable_target(target: Path, family: Literal["ledger", "store"]) -> (
    tuple[
        Literal["target-absent", "target-unreadable", "target-not-a-file", "target-undecodable"],
        str | None,
    ]
    | None
):
    """Why `target` must not be published, or `None` when it may be. Returns
    `(refuse_cause, error)` — the two fields a refusal carries beyond the
    caller's own identifying ones.

    TWO publishers share it, which is why it is a module-level seam rather than
    sweep machinery: `sweep._commit_ledger`'s nine call sites (the seven ledger
    publishers and the two pre-answer prunes), and `decisions.apply_pre_answer`'s
    out-of-band commit (DW-209/213). It lives HERE, beside `commit_paths`, because
    the hazard it guards is a property of that function — the missing-but-TRACKED
    path `commit_paths` deliberately keeps as a DELETION to stage — so the guard
    and the contract it gates read as one thing. `deferredwork` is already
    imported above and sits below this module, so the layering
    (`deferredwork` < `verify` < `sweep` < `decisions`) is unchanged and no cycle
    is created.

    The FAMILY is declared by the caller, never derived here. A `path ==
    paths.deferred_work` test would be exactly the "chosen by role" test
    `_commit_ledger`'s own naming rule refuses, and it would answer wrongly for a
    publisher whose ledger is symlinked (the argument is the RESOLVED target) or
    for any file a later caller publishes.

    LEDGER: `deferredwork.read_for_write`, because the ledger's own read
    contract (DW-146/DW-279) answers `None` for absence, `LedgerReadFault`
    for OS metadata/text-read faults, and its parent `LedgerReadError` for
    bytes nobody can decode. Here read faults do not propagate, because
    every caller is best-effort bookkeeping whose whole degrade discipline
    exists so a publication fault never aborts the work that wrote the file. The
    two faults are NOT folded into one cause, though (DW-237): the
    `LedgerReadFault` subclass is handled first as `target-unreadable`, as is
    a raw probe `OSError`; the remaining `LedgerReadError` returns
    `target-undecodable`, because they differ in the one way a
    caller holding a retry obligation has to know about. Undecodable bytes are a
    DURABLE content shape — a replay re-reads the same file and refuses it
    identically, exactly like an absence or a directory — while an `OSError` a
    probe RAISED (an EACCES parent, a WinError 64 from a
    registered-but-not-serving UNC provider) is a TRANSIENT host answer the next
    pass may well not see. So the four causes split three DURABLE
    (`target-absent`, `target-not-a-file`, `target-undecodable`) against one
    TRANSIENT (`target-unreadable`), and the split is drawn HERE, in the
    classifier, rather than at a call site re-reading the ledger or matching the
    fault text. The caller that needs it is `Engine._carry_harvested_deferrals`,
    the one publisher carrying a durable `harvest_carry_commit_pending` latch
    (DW-195/#552): it refuses every durable cause outright and hands only the
    transient one back to `commit_paths`, where a `GitError` keeps the latch for
    the replay. Before the split, both faults arrived as `target-unreadable`
    and that fall-through published the undecodable bytes — git accepts any
    bytes — so the corrupt ledger reached HEAD. The store leg never produces
    `target-undecodable`: it asks nothing about bytes. No lock is taken: this is
    a read the writer above already took. A later disappearance or replacement
    can still change what git publishes, as `_commit_ledger` documents.

    A ledger read of `None` means absence or a non-regular file. Re-probe with
    `stat` to distinguish `target-absent` from `target-not-a-file`; unlike
    `exists`, it exposes OS refusals on every supported interpreter. Metadata
    may change between probes, so this second probe must independently fold
    non-absence errors into `target-unreadable`.

    STORE: a regular file must be there, except for the resolved symlink-loop
    entry described below. Nothing is asked about its bytes.
    The writer emits valid UTF-8 JSON, but this guard does not check whether
    those bytes were replaced after the write, so a present, non-UTF-8 regular
    file stays publishable. `_prune_pre_answers`' own DW-176 absence refusal is
    about the LEDGER it reads, not the store.

    The TYPE test is the DW-211/228 half, and it is not decoration: existence
    alone let a store replaced by a DIRECTORY (or by a symlink to one) through
    the guard, and `commit_paths` hands the literal pathspec to `git add`, which
    stages a directory's descendants RECURSIVELY — an unrelated tree published
    under a `chore(sweep):`/`chore(decisions):` message. The test is ONE guarded
    `lstat()` on the argument (DW-257): `S_ISREG` publishes a regular file,
    `S_ISLNK` publishes a link entry (see the resolved-argument paragraph for
    which link that is), and any other present type (directory, FIFO, device,
    socket) is refused `target-not-a-file`, which is neither absent nor
    unreadable and names a different operator repair than either. A store
    symlinked to a regular file still publishes: the callers hand over the
    RESOLVED path, so this guard sees the regular target, and a caller that
    hands over the link itself lands on the `S_ISLNK` arm.

    The probe can also FAIL rather than answer, and `lstat` is the probe that
    REPORTS that on every supported interpreter. Until DW-257 this leg asked
    `is_file()`/`is_symlink()`/`exists()`: on Python 3.11–3.13 those absorb only
    the `ENOENT`/`ENOTDIR`/`ELOOP` class of errnos and RAISE the rest, so an
    `EACCES` arriving after a successful write used to escape a best-effort
    publisher (DW-227) — aborting `bmad-loop decisions`' walk or undercounting a
    TUI answer — and DW-227 folded that raise into `target-unreadable`, for the
    same reason the ledger leg folds its own `OSError`. But Python 3.14
    suppresses ALL OS errors inside those three probes, so there the same
    `EACCES` never reached the `except` at all: every probe answered False and
    the store degraded to `target-absent` — a present file reported as gone,
    where this guard's own ledger leg said `target-unreadable`. `lstat`
    suppresses nothing, so what its fault MEANS is decided by
    `deferredwork.probe_absence`, the one classification the ledger's
    repair/write reader and this guard's ledger leg also ask (DW-256/DW-268):
    `FileNotFoundError`/`NotADirectoryError`, pathlib's ignored winerrors
    (`deferredwork.ABSENCE_WINERRORS` — 21/123/1921, a disconnected mapped
    drive or a lexically invalid Windows path) and the `ValueError` a
    non-encodable path raises are the absence the old probes answered False
    for, and every other `OSError` — the refusal included — folds into
    `target-unreadable` on 3.11 through 3.14 alike. The `ValueError` half
    matters because this GUARD's callers hold only an `except OSError` around
    it: absorbed here, it cannot escape the guard. (The publishers' own
    `resolve()` arms, which run first, are unchanged and still raise the same
    `ValueError` for a NUL on POSIX, where `realpath` does not tolerate it —
    pre-existing and outside DW-268.) The fold is
    DW-227's; the probe that lets it fire everywhere is DW-257's; the absorbed
    set is the one `is_file()` had before DW-221 and is owned by the helper.

    The probe is taken on the RESOLVED argument, which is what decides what the
    `S_ISLNK` arm actually buys — the same entry the old `is_symlink()`
    disjunct bought, and not what the spelling suggests. A DANGLING link does
    not survive the resolve as a link: non-strict `Path.resolve` collapses it to
    the plain non-existent path it points at, so `lstat` raises `ENOENT` and the
    store is refused `target-absent`. That is the right answer for it (the
    prune's writer, `atomic_write_text_confined`, REFUSES to write through a
    link at the store's own name, so a dangling one holds no write of ours to
    publish), but it means the arm is doing a different job: on Python 3.13+, a
    symlink LOOP resolves to the link ITSELF, which `lstat` — declining to
    follow the last component — reports as a link where `stat` would raise
    `ELOOP`. The `S_ISLNK` arm preserves publication of that link entry. Python
    3.11–3.12 instead raise during resolve, which each caller handles on its
    own — `_commit_ledger` takes its existing `sweep-ledger-commit-unavailable`
    arm before this helper runs, and `apply_pre_answer` folds the fault into a
    `target-unreadable` refusal.

    Returns the `refuse_cause` token as a `Literal` rather than a bare `str`,
    which is what makes the closed four-value claim
    `tests/test_portability_guard.py` declares `refuse_cause` benign on a
    typechecked property rather than a comment. The union is spelled identically
    in `decisions.PublishRefusal.cause`; pyright rejects producer tokens the
    receiving union does not accept, but does not enforce equality of the unions."""
    if family == "ledger":
        try:
            if deferredwork.read_for_write(target) is None:
                # The re-probe asks the SAME classification the reader just
                # answered `None` for (DW-256/DW-268), so a fault the reader
                # absorbed is `target-absent` here. A fault the helper refuses
                # folds to `target-unreadable` IN PLACE rather than re-raising:
                # the enclosing `except OSError` would not catch a refused
                # `ValueError`, and nothing out of this re-probe may escape.
                try:
                    target.stat()
                except (OSError, ValueError) as e:
                    if deferredwork.probe_absence(e):
                        return ("target-absent", None)
                    return ("target-unreadable", str(e))
                return ("target-not-a-file", None)
        except (OSError, deferredwork.LedgerReadFault) as e:
            if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                e = e.__cause__  # Preserve the original OS attribution.
            # TRANSIENT: a probe RAISED, which the next pass may not see.
            return ("target-unreadable", str(e))
        except deferredwork.LedgerReadError as e:
            # DURABLE: the bytes on disk are what nobody can decode, and a replay
            # re-reads them identically. Kept apart from the `OSError` arm above so
            # a latch-holding caller can refuse this and retry only the other.
            return ("target-undecodable", str(e))
        return None
    if family == "store":
        # ONE `lstat`, never `is_file()`/`is_symlink()`/`exists()` (DW-257): those
        # suppress every OS error on Python 3.14, so a refused store degraded to
        # `target-absent` there. `lstat` reports the refusal on every interpreter
        # and, declining to follow the last component, keeps the one entry the
        # old `is_symlink()` disjunct bought — the 3.13+ symlink LOOP, which
        # survives the caller's resolve as a link — publishable through `S_ISLNK`.
        # What a fault out of it MEANS is `deferredwork.probe_absence`'s call
        # (DW-256/DW-268): absence for the reader's absorbed set, `target-
        # unreadable` for everything else — a `ValueError` included in the
        # tuple so a non-encodable store path can never escape this best-effort
        # guard.
        try:
            st = target.lstat()
        except (OSError, ValueError) as e:
            if deferredwork.probe_absence(e):
                return ("target-absent", None)
            return ("target-unreadable", str(e))
        if S_ISREG(st.st_mode) or S_ISLNK(st.st_mode):
            return None
        return ("target-not-a-file", None)
    # Spelled as an exhaustive dispatch, not `if ledger / else store`: a THIRD
    # family added to the `Literal` would otherwise typecheck at every call site
    # and fall silently through to existence-only validation — precisely the
    # "inherit a validation it does not want" failure the required keyword-only
    # argument at the sweep's call sites exists to prevent. This reds under
    # pyright the moment the union grows, before any run.
    assert_never(family)


def commit_paths(repo: Path, message: str, paths: list[Path]) -> str | None:
    """Commit exactly `paths` (and nothing else), leaving any unrelated working
    or staged changes untouched. Unlike commit_story's `add -A`, this is safe to
    call out of band (e.g. `bmad-loop decisions`) when the tree may hold the
    user's own uncommitted work. Returns the new HEAD sha, or None when the
    given paths had no changes to commit. Paths outside the repo are ignored —
    and so is a path git has never seen (absent from both the working tree and
    the index): `git add` hard-fails on a pathspec matching nothing, and one
    optional path would otherwise sink the whole commit (a swallowed `GitError`
    in `confirm` silently losing the spec+board commit over a park record that
    was never committed). A missing-but-TRACKED path stays in: that is a
    deletion to stage. An uncertain repo root raises before staging; uncertainty
    in one candidate omits only that candidate, preserving the partial-path
    contract for healthy siblings. If no usable operand survives that uncertainty,
    the call raises instead of reporting a successful no-op. TWO things can make a
    candidate uncertain and both take that one path: its `resolve()` can fail, and
    so can the presence probe below it, which is a direct `Path.lstat()` for
    exactly that reason (DW-239). `Path.exists()`/`is_symlink()` stood here until
    a truthful probe was needed on every interpreter: on Python 3.11–3.13 they
    absorb only `pathlib`'s ignored errnos (`ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP`,
    plus the `ERROR_NOT_READY`/`ERROR_INVALID_NAME`/`ERROR_CANT_RESOLVE_FILENAME`
    winerrors) and RAISE the rest, so an `EACCES` under one operand escaped as a
    bare `OSError` into best-effort publishers that have no handler for it
    (DW-227); Python 3.14 suppresses ALL OS errors inside them, so there the same
    fault never arrived at all — both probes answered False and a TRACKED candidate
    under an unsearchable parent was ruled MISSING, taking the missing-but-tracked
    arm below and offering its DELETION to `git add`. `lstat` suppresses nothing on
    any interpreter: `EACCES`, an `ELOOP` on an intermediate component and
    `ENAMETOOLONG` all REPORT into the uncertainty slot instead of answering False,
    so that slot is now reachable everywhere. So does every errno the pair absorbed
    but `ENOENT`/`ENOTDIR` — `EBADF` and those three winerrors included — and that
    is a deliberate widening rather than a side effect: an operand a Windows host
    calls not-ready or unspellable is a path this cannot say anything about, and
    saying so is the whole of DW-239. The cost is disclosed: as a SOLE operand such
    a path used to be a clean no-op and now raises `GitError`, which the best-effort
    publishers above already handle and which is the honest answer.

    It otherwise answers the same PRESENCE question the pair answered — success for
    every directory entry that exists, and `ENOENT`/`ENOTDIR` for the absence the
    pair reported as False. That equivalence includes the one entry the
    `is_symlink()` disjunct was actually there to buy, and it is not the dangling
    link the spelling suggests: every operand here is `resolve()`d first, and
    non-strict resolve collapses a dangling link to the plain non-existent path it
    points at, so `lstat` raises `FileNotFoundError` for it exactly as both probes
    answered False. What survives the resolve AS a link is a symlink LOOP, which
    Python 3.13+ resolves to the link itself — `exists()` False, `is_symlink()`
    True — and `lstat` succeeds on it because it does not follow the last component.
    `unpublishable_target`'s RESOLVED-argument paragraph makes the same distinction
    for the same reason. The guard still handles what a probe raises, not what it
    suppresses; there is simply nothing left here that suppresses."""
    rels: list[str] = []
    # The single per-candidate uncertainty slot, shared by BOTH sources (a failed
    # `resolve()` and a failed presence probe) because they have one contract: omit
    # the candidate, and raise only if nothing survives. FIRST fault wins, so the
    # message names a real cause rather than the last one seen. The third element
    # is the STAGE, so the raise below can say which probe failed while keeping the
    # `no exact commit operand remains` prefix two test suites match on.
    candidate_fault: tuple[Path, OSError | RuntimeError, str] | None = None
    try:
        repo_root = repo.resolve()
    except (OSError, RuntimeError) as e:
        raise GitError(
            f"cannot resolve repository root for exact commit safely ({repo}): {e}"
        ) from e
    for p in paths:
        try:
            # `.as_posix()`, not `str()`: every rel here becomes a git pathspec and is
            # compared against git-derived output, and git speaks posix separators on
            # every platform. `str()` yields backslashes on Windows, which git reads as
            # wildmatch ESCAPES rather than separators.
            rels.append(Path(p).resolve().relative_to(repo_root).as_posix())
        except (OSError, RuntimeError) as e:
            if candidate_fault is None:
                candidate_fault = (Path(p), e, "path resolution")
            continue
        except ValueError:
            continue
    # Each presence probe is guarded on its own: a fault under ONE candidate must
    # not decide the fate of its healthy siblings, and it must not escape either —
    # this is the last probe before `git add`, and both publishers above it treat a
    # publication fault as bookkeeping. A faulted candidate leaves BOTH lists: it is
    # not staged, and it is not offered to `ls-files` as a possible deletion, since
    # nothing here can tell "removed" from "cannot say".
    survivors: list[str] = []
    missing: list[str] = []
    for r in rels:
        candidate = repo_root / r
        try:
            # `lstat` DIRECTLY, never `exists()`/`is_symlink()`: those suppress every
            # OS error on Python 3.14, so an EACCES parent ruled a TRACKED candidate
            # MISSING and staged its deletion (DW-239). It does not follow the last
            # component, which keeps the one entry the `is_symlink()` disjunct bought
            # — a symlink LOOP, the only link that survives the resolve above as a
            # link — PRESENT; `ENOENT`/`ENOTDIR` are the absence the pair answered
            # False for, a resolved-away dangling link among them.
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            present = False
        except OSError as e:
            if candidate_fault is None:
                candidate_fault = (candidate, e, "a presence probe")
            continue
        else:
            present = True
        survivors.append(r)
        if not present:
            missing.append(r)
    rels = survivors
    if missing:
        rc, out = _git_raw(repo, "ls-files", "-z", "--", *_literal_specs(missing))
        if rc != 0:
            raise GitError(f"git ls-files failed: {out}")
        tracked = {t for t in out.split("\0") if t}
        rels = [r for r in rels if r not in missing or r in tracked]
    if not rels:
        if candidate_fault is not None:
            failed_path, error, stage = candidate_fault
            raise GitError(
                f"no exact commit operand remains after {stage} failed "
                f"for {failed_path}: {error}"
            ) from error
        return None
    # Every operand is forced LITERAL: git reads a positional operand as a PATHSPEC,
    # and `implementation_artifacts` reaches here verbatim out of the operator's
    # `_bmad/bmm/config.yaml` (`bmadconfig._resolve` substitutes and resolves it, and
    # nothing sanitizes it), so a `[`, `]`, `*` or `?` in a configured path is a
    # wildmatch metacharacter. Unescaped, this function breaks its own first promise —
    # "commit exactly `paths` (and nothing else)": `add -- docs[a]/f.md` also stages
    # `docsa/f.md`, and the operator's unrelated edit is committed under a story's
    # name. The operand is a SUPERSET (git compares literally before falling through
    # to fnmatch), which is why the over-match direction is the only one possible.
    #
    # A second, quieter harm with the ledger GITIGNORED — the shape
    # `_carry_harvested_deferrals` is built to hit: `git add` refuses an explicitly
    # named ignored path (rc 1) but SKIPS a globbed one, so the plain form could exit
    # rc 0 having staged nothing, `status` find no change, and the carry report success
    # having committed no ledger and journalled no `harvest-carry-uncommitted` — a
    # silent loss where the literal form leaves a record. Note the `ls-files` operands
    # above are literalised too: that leg reads membership rather than acting, so its
    # failure direction is to DROP a rel (the glob returns the neighbour's name, which
    # never equals `r`), but leaving one bare operand beside three fixed ones is the
    # next reader's trap. It is also why the `r.replace("\\", "/")` that used to guard
    # this comparison is gone: `.as_posix()` above fixes the separator at the source,
    # and a dead normalizer beside a glob operand reads as protection that isn't there.
    specs = _literal_specs(rels)
    rc, out = _git(repo, "add", "--", *specs)
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    # Stdout ALONE (`_git_out`, #442): this is an EMPTINESS test, and `status` exits 0
    # while still warning on stderr — against the merge an unchanged path set reads
    # non-empty, the early-out below is skipped, and `git commit` runs with nothing
    # staged and raises where the contract says return None. The `add` above and the
    # `commit` below stay on `_git`: both are rc-only, and stderr is their diagnostic.
    rc, out, detail = _git_out(repo, "status", "--porcelain", "--", *specs)
    if rc != 0:
        raise GitError(f"git status failed: {detail}")
    if not out:
        return None  # nothing changed in these paths
    # pathspec form commits only `rels`, ignoring any other staged changes
    rc, out = _git(repo, "commit", "-m", message, "--", *specs)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


@dataclass(frozen=True)
class _BoundLiveLedger:
    """One raw observation of the live ledger: its bytes beside the stat fields an
    in-place rewrite of the same inode moves."""

    data: bytes
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _bound_live_ledger_stat(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _bound_live_ledger_identity(
    live_path: Path,
    target: Path,
    accepted_text: str,
    *,
    observed: _BoundLiveLedger | None = None,
) -> _BoundLiveLedger:
    """Prove the live ledger is the accepted regular target without disclosing it.

    The first observation reads the target's RAW bytes and accepts them only when
    they decode, under the universal-newline reading every ledger reader uses
    (`deferredwork.read_for_write`), to `accepted_text`: the sweep validated that
    text, and on Windows `atomic_write_text` renders it with CRLF, so a byte-exact
    comparison against `accepted_text.encode()` would refuse every Windows host
    while a text comparison would let the bytes on disk and the bytes committed
    drift apart. Those observed bytes are what the candidate carries. Every later
    call re-reads and holds the target to that exact observation — bytes, inode,
    size, mtime and ctime — so a rewrite in place, the same inode carrying rival
    bytes (or the same text under other line endings), is refused rather than
    published beside a candidate built from the earlier reading. The read itself
    is bracketed by two stats of those fields, so a write that lands during it is
    refused too, not read half-old and half-new.
    """
    try:
        resolved = live_path.resolve(strict=True)
        before = target.lstat()
        if resolved != target or not S_ISREG(before.st_mode):
            raise GitError("accepted publication target changed shape")
        data = live_path.read_bytes()
        after = target.lstat()
        if (
            not S_ISREG(after.st_mode)
            or _bound_live_ledger_stat(before) != _bound_live_ledger_stat(after)
            or live_path.resolve(strict=True) != target
        ):
            raise GitError("accepted publication target changed during validation")
        if observed is None:
            decoded = io.IncrementalNewlineDecoder(None, translate=True).decode(
                data.decode("utf-8"), final=True
            )
            if decoded != accepted_text:
                raise GitError("accepted publication target changed during validation")
    except GitError:
        raise
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        raise GitError("accepted publication target could not be validated") from exc
    current = _BoundLiveLedger(data, *_bound_live_ledger_stat(after))
    if observed is not None and current != observed:
        if (current.dev, current.ino) != (observed.dev, observed.ino):
            raise GitError("accepted publication target identity changed")
        raise GitError("accepted publication target changed during validation")
    return current


@dataclass(frozen=True)
class _BoundCheckoutIdentity:
    immediate_ref: str | None
    terminal_ref: str | None
    oid: str


_BOUND_IDENTITY_PROBE_LIMIT = 3
_BOUND_INDEX_RECONCILE_LIMIT = 3


def _bound_symbolic_ref(
    repo: Path, ref: str, *, recurse: bool, required: bool = True
) -> str | None:
    args = ["symbolic-ref", "--quiet"]
    if not recurse:
        args.append("--no-recurse")
    args.append(ref)
    rc, value, _detail = _git_out(repo, *args)
    if rc == 1:
        if required:
            raise GitError("exact-path publication requires an attached direct branch")
        return None
    if rc != 0 or not value:
        raise GitError("exact-path publication branch identity could not be validated")
    return value


def _bound_direct_ref_probe(repo: Path, ref: str, *, timeout_s: float | None = None) -> None:
    proc = _run_git(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--no-recurse", ref],
        repo,
        timeout_s=timeout_s,
    )
    rc = proc.returncode
    if rc == 0:
        raise GitError("exact-path publication branch changed ref kind")
    if rc != 1:
        raise GitError("exact-path publication branch kind could not be validated")


def _bound_head_oid(repo: Path) -> str:
    try:
        return rev_parse_head(repo)
    except GitError as exc:
        raise GitError("exact-path publication branch value could not be validated") from exc


def _bound_checkout_identity(repo: Path, *, require_branch: bool = True) -> _BoundCheckoutIdentity:
    """Capture one stable checkout identity without disclosing its ref names."""
    for _attempt in range(_BOUND_IDENTITY_PROBE_LIMIT):
        immediate = _bound_symbolic_ref(repo, "HEAD", recurse=False, required=require_branch)
        terminal = _bound_symbolic_ref(repo, "HEAD", recurse=True, required=require_branch)
        if (immediate is None) != (terminal is None):
            continue
        if terminal is not None and not terminal.startswith("refs/heads/"):
            raise GitError("exact-path publication requires a terminal branch")
        if terminal is not None:
            _bound_direct_ref_probe(repo, terminal)
        oid = _bound_head_oid(repo)
        if (
            _bound_symbolic_ref(repo, "HEAD", recurse=False, required=require_branch) == immediate
            and _bound_symbolic_ref(repo, "HEAD", recurse=True, required=require_branch) == terminal
            and _bound_head_oid(repo) == oid
        ):
            return _BoundCheckoutIdentity(immediate, terminal, oid)
    raise GitError("exact-path publication checkout changed during identity capture")


def _bound_ref_oid(repo: Path, ref: str) -> str:
    _bound_direct_ref_probe(repo, ref)
    rc, oid, _detail = _git_out(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if rc != 0 or not oid:
        raise GitError("exact-path publication branch value could not be validated")
    _bound_direct_ref_probe(repo, ref)
    return oid


@dataclass(frozen=True)
class _BoundGitEntry:
    mode: str
    kind: str
    oid: str


def _bound_tree_entry(repo: Path, revision: str, rel: str) -> _BoundGitEntry | None:
    try:
        entry = _entry_at_revision(repo, revision, rel)
    except GitError as exc:
        raise GitError(f"committed publication target could not be observed in {repo}") from exc
    if entry is None:
        return None
    return _BoundGitEntry(*entry)


def _bound_tree_blob(repo: Path, revision: str, rel: str) -> str | None:
    entry = _bound_tree_entry(repo, revision, rel)
    if entry is None:
        return None
    if entry.kind != "blob" or entry.mode not in {"100644", "100755"}:
        raise GitError("committed publication target is not a regular file")
    return entry.oid


def _preflight_bound_tree_blob(
    current_oid: str | None, baseline_oid: str, accepted_oid: str
) -> None:
    if current_oid not in (None, baseline_oid, accepted_oid):
        raise GitError("committed publication target holds rival content")


def _bound_baseline_blob(
    repo: Path, baseline_commit: str | None, rel: str, baseline_text: str
) -> str | None:
    """The baseline's committed blob id, or `None` when no commit tracks it.

    `baseline_text` is a universal-newline reading of the ledger beside
    `baseline_commit`, so re-encoding it names the LF blob and never the CRLF
    one Git preserves under `core.autocrlf=false` or a `-text` attribute — the
    shape every Windows-written ledger takes, since `atomic_write_text` renders
    CRLF there. Hashing that re-encoding misread an unchanged committed
    baseline as rival content and left the migration in COMMITTING for good.
    The committed blob is the authority instead, held to the text under the
    same reading (`deferredwork.read_for_write`) so a baseline record that no
    longer describes its commit refuses rather than lending that commit's blob
    its name.
    """
    if baseline_commit is None:
        return None
    entry = _bound_tree_entry(repo, baseline_commit, rel)
    if entry is None:
        return None
    if entry.kind != "blob" or entry.mode not in {"100644", "100755"}:
        raise GitError("accepted baseline is not a regular file at its commit")
    proc = git_bytes(repo, "cat-file", "blob", entry.oid)
    if proc.returncode != 0:
        raise GitError(f"accepted baseline blob could not be read in {repo}")
    try:
        committed = io.IncrementalNewlineDecoder(None, translate=True).decode(
            proc.stdout.decode("utf-8"), final=True
        )
    except UnicodeDecodeError as exc:
        raise GitError("accepted baseline blob is not valid UTF-8") from exc
    if committed != baseline_text:
        raise GitError("accepted baseline does not match the committed baseline")
    return entry.oid


def _preflight_bound_absence(
    repo: Path,
    head: str,
    baseline_commit: str | None,
    rel: str,
    accepted_oid: str,
    baseline_oid: str,
) -> None:
    """Accept an absent committed target only on proof it was never tracked.

    The baseline text was read beside `baseline_commit`. A target that commit
    carried and the captured HEAD no longer does was deleted by a commit that
    landed after the baseline was taken, and building the candidate on that
    commit would silently re-add the ledger over a rival's committed decision —
    the committed twin of the staged deletion the real-index check below
    refuses. Absence at the baseline commit is the one durable proof the
    ledger was originally untracked; no baseline commit is no authority.

    An originally untracked ledger has a second way to go absent: this very
    transition published it and a later commit removed it, which a COMMITTING
    replay would otherwise re-add. The accepted transition is still in
    first-parent ancestry then, so its presence beneath an absent HEAD is the
    same rival decision and refuses the same way.
    """
    if baseline_commit is None:
        raise GitError("committed publication target absence has no baseline authority")
    if _bound_tree_entry(repo, baseline_commit, rel) is not None:
        raise GitError("committed publication target was deleted after the accepted baseline")
    if _accepted_bound_transition(repo, head, rel, accepted_oid, baseline_oid) is not None:
        raise GitError("committed publication target was deleted after its accepted publication")


def _bound_changed_paths(repo: Path, parent: str, revision: str) -> set[str]:
    proc = git_bytes(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-z",
        "-r",
        parent,
        revision,
        "--",
    )
    if proc.returncode != 0:
        raise GitError(f"git candidate scope probe failed in {repo}")
    return {os.fsdecode(item) for item in proc.stdout.split(b"\0") if item}


class _BoundCandidateMismatch(GitError):
    """A candidate was fully observed and structurally rejected."""


def _bound_parent(repo: Path, revision: str) -> str:
    rc, lineage, _detail = _git_out(repo, "rev-list", "--parents", "--max-count=1", revision)
    if rc != 0:
        raise GitError(f"git candidate parent probe failed in {repo}")
    parts = lineage.split()
    if not parts:
        raise GitError(f"git candidate parent probe returned no evidence in {repo}")
    if parts[0] != revision:
        raise GitError(f"git candidate parent probe returned malformed evidence in {repo}")
    if len(parts) != 2:
        raise _BoundCandidateMismatch("exact-path candidate does not have exactly one parent")
    return parts[1]


def _validate_bound_candidate(
    repo: Path,
    revision: str,
    parent: str,
    rel: str,
    accepted_oid: str,
    baseline_oid: str,
) -> None:
    if _bound_parent(repo, revision) != parent:
        raise _BoundCandidateMismatch("exact-path candidate has an unexpected parent")
    if _bound_changed_paths(repo, parent, revision) != {rel}:
        raise _BoundCandidateMismatch(
            "exact-path candidate changed paths outside its declared scope"
        )
    committed = _bound_tree_entry(repo, revision, rel)
    if committed is None or committed.kind != "blob" or committed.oid != accepted_oid:
        raise _BoundCandidateMismatch("exact-path candidate does not contain the accepted ledger")
    parent_entry = _bound_tree_entry(repo, parent, rel)
    if parent_entry is not None and (
        parent_entry.kind != "blob"
        or parent_entry.mode not in {"100644", "100755"}
        or parent_entry.oid != baseline_oid
    ):
        raise _BoundCandidateMismatch(
            "exact-path candidate parent does not contain the accepted baseline"
        )
    expected_mode = "100644" if parent_entry is None else parent_entry.mode
    if committed.mode != expected_mode:
        raise _BoundCandidateMismatch("exact-path candidate changed the publication target mode")


def _accepted_bound_transition(
    repo: Path,
    head: str,
    rel: str,
    accepted_oid: str,
    baseline_oid: str,
) -> str | None:
    """Find the newest accepted ledger transition in first-parent ancestry."""
    rc, out, _detail = _git_out(
        repo,
        "rev-list",
        "--first-parent",
        head,
        "--",
        *_literal_specs([rel]),
    )
    if rc != 0:
        raise GitError(f"git accepted-transition probe failed in {repo}")
    if not out:
        return None
    # `rev-list` is newest-first. A later commit may touch the ledger as part of
    # a wider change (for example a mode-only edit beside an unrelated file)
    # without invalidating the earlier exact one-path migration transition.
    for candidate in out.splitlines():
        try:
            parent = _bound_parent(repo, candidate)
            _validate_bound_candidate(repo, candidate, parent, rel, accepted_oid, baseline_oid)
        except _BoundCandidateMismatch:
            continue
        return candidate
    return None


def _bound_index_entry(repo: Path, rel: str) -> _BoundGitEntry | None:
    try:
        proc = git_bytes(repo, "ls-files", "-s", "-z", "--", *_literal_specs([rel]))
    except GitError as exc:
        raise GitError(f"publication target index could not be observed in {repo}") from exc
    if proc.returncode != 0:
        raise GitError(f"publication target index could not be observed in {repo}")
    records = [record for record in proc.stdout.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1 or b"\t" not in records[0]:
        raise GitError("publication target index is not one exact entry")
    header, actual_path = records[0].split(b"\t", 1)
    if actual_path != os.fsencode(rel):
        raise GitError("publication target index is not one exact entry")
    try:
        mode, oid, stage = header.decode("ascii", "strict").split()
    except (UnicodeDecodeError, ValueError) as exc:
        raise GitError("publication target index evidence is malformed") from exc
    if stage != "0" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise GitError("publication target index is not an unambiguous stage-zero entry")
    kind = "commit" if mode == "160000" else "blob"
    return _BoundGitEntry(mode, kind, oid)


def _synchronize_bound_index(
    repo: Path,
    expected_checkout: _BoundCheckoutIdentity,
    rel: str,
    observed_index_entry: _BoundGitEntry | None,
) -> None:
    """Align only the target entry to a stably observed checkout tree.

    A checkout can move independently of the captured publication branch.  Each
    reset is therefore bracketed by a complete object-plus-ref observation.  A
    move is repaired toward the newest observation but still refuses the attempt,
    leaving commit-only replay to decide authority.  The explicit bound prevents
    a hostile ref mover from turning housekeeping into a livelock.
    """
    expected_index_entry = observed_index_entry
    moved = False
    newest = _bound_checkout_identity(repo, require_branch=False)
    if newest != expected_checkout:
        moved = True

    for _attempt in range(_BOUND_INDEX_RECONCILE_LIMIT):
        if _bound_index_entry(repo, rel) != expected_index_entry:
            raise GitError("real index target changed during exact-path publication")
        target = newest
        target_entry = _bound_tree_entry(repo, target.oid, rel)
        if target_entry is not None and target_entry.kind == "tree":
            raise GitError("committed publication target became a directory")
        rc, _out = _git(repo, "reset", target.oid, "--", *_literal_specs([rel]))
        if rc != 0:
            raise GitError(f"git target-local index synchronization failed in {repo}")
        expected_index_entry = target_entry
        if _bound_index_entry(repo, rel) != target_entry:
            raise GitError("real index target synchronization did not match committed content")
        newest = _bound_checkout_identity(repo, require_branch=False)
        if newest == target:
            if moved:
                raise GitError("checkout changed during target index reconciliation")
            return
        moved = True

    # One final target-local repair makes the index correspond to the latest
    # observation even when the movement never settled inside the retry bound.
    if _bound_index_entry(repo, rel) != expected_index_entry:
        raise GitError("real index target changed during exact-path publication")
    newest_entry = _bound_tree_entry(repo, newest.oid, rel)
    if newest_entry is not None and newest_entry.kind == "tree":
        raise GitError("committed publication target became a directory")
    rc, _out = _git(repo, "reset", newest.oid, "--", *_literal_specs([rel]))
    if rc != 0:
        raise GitError(f"git target-local index synchronization failed in {repo}")
    if _bound_index_entry(repo, rel) != newest_entry:
        raise GitError("real index target synchronization did not match committed content")
    raise GitError("checkout did not stabilize during target index reconciliation")


def _publish_bound_candidate(
    repo: Path,
    captured: _BoundCheckoutIdentity,
    candidate: str,
    rel: str,
    accepted_oid: str,
    baseline_oid: str,
) -> None:
    terminal_ref = captured.terminal_ref
    assert terminal_ref is not None

    def validate_terminal_kind(remaining_s: float) -> None:
        _bound_direct_ref_probe(
            repo,
            terminal_ref,
            timeout_s=remaining_s,
        )

    update = _PreparedRefUpdate(
        ref=terminal_ref,
        new_oid=candidate,
        old_oid=captured.oid,
        validate_while_prepared=validate_terminal_kind,
    )
    try:
        _run_git(
            ["git", "-C", str(repo), "update-ref", "--stdin"],
            repo,
            prepared_update=update,
        )
    except _GitCommitIndeterminate as exc:
        observed = _bound_ref_oid(repo, terminal_ref)
        if observed == candidate:
            _validate_bound_candidate(
                repo,
                candidate,
                captured.oid,
                rel,
                accepted_oid,
                baseline_oid,
            )
            return
        if observed == captured.oid:
            raise GitError(
                "exact-path publication acknowledgement was lost before ref movement"
            ) from exc
        raise GitError("captured branch changed during exact-path publication") from exc


def commit_path_bound(
    repo: Path,
    message: str,
    path: Path,
    *,
    accepted_text: str,
    baseline_text: str,
    baseline_commit: str | None = None,
    live_path: Path | None = None,
) -> str | None:
    """Publish one accepted ledger transition through a validated candidate.

    The candidate is committed in a detached temporary worktree, so ordinary Git
    hooks run without moving the authoritative checkout.  It carries the live
    target's own bytes, observed once they are proven to decode to
    `accepted_text`, so the blob it commits is the one `git add` of the validated
    file would stage under any line-ending configuration and the checkout reads
    clean after publication.  Its parent, exact path delta, Git-clean-filtered
    blob, live bytes and stat identity, and path identity are all validated
    before a prepared transaction publishes it to the originally captured
    terminal direct branch.  Once that transaction commits, target-only
    real-index reconciliation is replayable housekeeping: no later fault rolls the
    truthful commit back.

    `baseline_commit` is the HEAD beside which `baseline_text` was read. The
    blob that commit holds at the target is the baseline's identity — the text
    is a universal-newline reading, so its own encoding cannot name a CRLF blob
    Git preserved — and a text that no longer decodes to that blob is refused.
    The commit is also what tells an absent captured target apart: one it
    tracked has since been deleted by a rival commit, and the publication
    refuses rather than re-adding it. Without it an absent target has no
    authority and is refused the same way.
    """
    try:
        rc, top, _detail = _git_out(repo, "rev-parse", "--show-toplevel")
        if rc != 0:
            raise GitError(f"git repository root probe failed in {repo}")
        repo_root = Path(top).resolve()
        target = path.resolve(strict=True)
        rel = target.relative_to(repo_root).as_posix()
    except GitError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitError("exact-path publication target could not be resolved safely") from exc

    lexical = live_path if live_path is not None else path
    observed = _bound_live_ledger_identity(lexical, target, accepted_text)
    accepted_bytes = observed.data
    # The baseline's identity is the blob its commit holds; only a baseline no
    # commit tracks is named by its own text, and that name is then consulted
    # solely as an index allowance beneath an absent committed target.
    baseline_oid = _bound_baseline_blob(repo_root, baseline_commit, rel, baseline_text)
    try:
        accepted_oid = git_normalized_blob_oid_for_bytes(repo_root, rel, accepted_bytes)
        if baseline_oid is None:
            baseline_oid = git_normalized_blob_oid_for_bytes(
                repo_root, rel, baseline_text.encode("utf-8")
            )
    except GitError as exc:
        raise GitError("publication target content could not be normalized by Git") from exc
    captured = _bound_checkout_identity(repo_root)

    head_entry = _bound_tree_entry(repo_root, captured.oid, rel)
    head_blob = _bound_tree_blob(repo_root, captured.oid, rel)
    _preflight_bound_tree_blob(head_blob, baseline_oid, accepted_oid)
    if head_entry is None:
        _preflight_bound_absence(
            repo_root, captured.oid, baseline_commit, rel, accepted_oid, baseline_oid
        )
    accepted = (
        _accepted_bound_transition(repo_root, captured.oid, rel, accepted_oid, baseline_oid)
        if head_blob == accepted_oid
        else None
    )
    authority_parent = _bound_parent(repo_root, accepted) if accepted is not None else captured.oid
    parent_has_target = _bound_tree_entry(repo_root, authority_parent, rel) is not None
    observed_index_entry = _bound_index_entry(repo_root, rel)
    expected_mode = "100644" if head_entry is None else head_entry.mode
    allowed_index_entries: set[_BoundGitEntry | None] = {
        _BoundGitEntry(expected_mode, "blob", accepted_oid),
        _BoundGitEntry(expected_mode, "blob", baseline_oid),
    }
    if not parent_has_target:
        # Original absence is the one accepted no-entry shape.  A tracked
        # parent's absent real-index entry is a foreign staged deletion and is
        # refused rather than silently re-added.
        allowed_index_entries.add(None)
    if observed_index_entry not in allowed_index_entries:
        raise GitError("real index holds foreign content at the publication target")

    if accepted is not None:
        _bound_live_ledger_identity(lexical, target, accepted_text, observed=observed)
        _synchronize_bound_index(repo_root, captured, rel, observed_index_entry)
        return accepted

    # Preserve generic clean/ignored behavior when no migration transition needs
    # replay.  In particular, an ignored ledger never earns a synthetic commit.
    try:
        clean = path_clean(repo_root, rel)
        ignored_untracked = clean and head_blob is None and path_ignored(repo_root, target)
    except GitError as exc:
        raise GitError("publication target cleanliness could not be validated") from exc
    if clean and (head_blob == accepted_oid or ignored_untracked):
        _bound_live_ledger_identity(lexical, target, accepted_text, observed=observed)
        return None

    active_error: BaseException | None = None
    candidate: str | None = None
    try:
        has_non_tree_parent = path_has_non_tree_ancestor_at_revision(repo_root, captured.oid, rel)
    except GitError as exc:
        raise GitError("candidate publication parent shape could not be validated") from exc
    if has_non_tree_parent:
        raise GitError("candidate publication path has a non-directory committed parent")
    with tempfile.TemporaryDirectory() as td:
        candidate_root = Path(td) / "candidate"
        rc, _out = _git(
            repo_root,
            "worktree",
            "add",
            "--detach",
            str(candidate_root),
            captured.oid,
        )
        if rc != 0:
            raise GitError(f"git detached candidate checkout failed in {repo_root}")
        try:
            candidate_path = candidate_root / rel
            try:
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                candidate_path.write_bytes(accepted_bytes)
            except (OSError, RuntimeError, ValueError) as exc:
                raise GitError("exact-path candidate content could not be written") from exc
            rc, _out = _git(candidate_root, "add", "--", *_literal_specs([rel]))
            if rc != 0:
                raise GitError(f"git exact-path candidate staging failed in {repo_root}")
            staged_entry = _bound_index_entry(candidate_root, rel)
            if staged_entry != _BoundGitEntry(expected_mode, "blob", accepted_oid):
                raise GitError("exact-path candidate staging changed accepted content")
            _bound_live_ledger_identity(lexical, target, accepted_text, observed=observed)
            if _bound_checkout_identity(repo_root) != captured:
                raise GitError("checkout changed before exact-path candidate hooks")
            rc, _out = _git(candidate_root, "commit", "-m", message)
            if rc != 0:
                raise GitError(f"git exact-path candidate commit failed in {repo_root}")
            try:
                candidate = rev_parse_head(candidate_root)
            except GitError as exc:
                raise GitError("exact-path candidate identity could not be validated") from exc
            _validate_bound_candidate(
                repo_root,
                candidate,
                captured.oid,
                rel,
                accepted_oid,
                baseline_oid,
            )
            _bound_live_ledger_identity(lexical, target, accepted_text, observed=observed)
            _publish_bound_candidate(
                repo_root,
                captured,
                candidate,
                rel,
                accepted_oid,
                baseline_oid,
            )
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            rc, _out = _git(
                repo_root,
                "worktree",
                "remove",
                "--force",
                str(candidate_root),
            )
            if rc != 0 and active_error is None:
                raise GitError(f"git detached candidate cleanup failed in {repo_root}")

    assert candidate is not None
    _bound_live_ledger_identity(lexical, target, accepted_text, observed=observed)
    expected_checkout = _BoundCheckoutIdentity(
        captured.immediate_ref,
        captured.terminal_ref,
        candidate,
    )
    _synchronize_bound_index(repo_root, expected_checkout, rel, observed_index_entry)
    return candidate
