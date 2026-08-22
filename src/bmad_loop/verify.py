"""Deterministic post-session verification. Never trust LLM self-reports.

verify_dev / verify_review check artifacts on disk and git state against
what the session's result.json claims; run_verify_commands executes the
policy's test/lint gates with the orchestrator's own subprocess calls.
"""

from __future__ import annotations

import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

import yaml

from . import deferredwork
from .bmadconfig import ProjectPaths
from .frontmatter import FrontmatterWriteError  # noqa: F401 — re-export
from .frontmatter import set_frontmatter_status  # noqa: F401 — re-export
from .frontmatter import (
    _edit_frontmatter_block,
    _split_frontmatter,
    operator_actions_of,
    read_frontmatter,
    status_of,
)
from .model import StoryTask, VerifyOutcome
from .platform_util import atomic_write_bytes
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


@overload
def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = ...,
    binary: Literal[False] = ...,
    timeout_s: int | None = ...,
) -> subprocess.CompletedProcess[str]: ...


@overload
def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = ...,
    binary: Literal[True],
    timeout_s: int | None = ...,
) -> subprocess.CompletedProcess[bytes]: ...


def _run_git(
    cmd: list[str],
    repo: Path,
    *,
    env: dict[str, str] | None = None,
    binary: bool = False,
    timeout_s: int | None = None,
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

    `timeout_s` overrides the module bound for this one call — the interactive
    callers' seam (#390): a TUI render or install's best-effort probe keeps its
    own short deadline while standing inside the chokepoint."""
    effective_timeout_s = _git_timeout_s if timeout_s is None else timeout_s
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=not binary,
            timeout=effective_timeout_s,
            env={**(env if env is not None else os.environ), "LC_ALL": "C"},
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
    whose records can begin with a space and which `.strip()` would corrupt.

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
    benign here: seven callers gate on it, and `cli.py`'s three refuse the command
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
    BMAD artifact folders (see `artifact_relpaths`), so a session that only
    rewrites its own spec (e.g. the frontmatter-status reconcile) under those
    folders doesn't register as real implementation work. Mirrors
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
    created). Keep it that way."""
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", ".", *_exclude_specs(exclude))
    if rc != 0:
        return True
    if not include_untracked:
        return False
    created = untracked_files(repo)
    if baseline_untracked is not None:
        created -= set(baseline_untracked)
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

    Any non-zero diff result fails open toward "changed", matching the
    authoritative :func:`has_changes_since` gate. The literal pathspec is
    required for operator-configured ledger paths containing Git wildmatch
    characters.
    """
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--", f":(literal){rel}")
    if rc != 0:
        return True
    untracked = untracked_files(repo)
    if rel not in untracked:
        return False
    return baseline_untracked is None or rel not in set(baseline_untracked)


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
    created = untracked_files(repo) - set(baseline_untracked)
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
    exclusion HIDES a diff instead of exposing a file. `has_changes_since` and
    `attempt_dirty` both spend these on `diff --quiet . :(exclude)<dir>`, so a dir
    whose name carries a `*` excludes a sibling tree as well and the attempt reads
    CLEAN when it changed — the same false "no changes" that a dev attempt's dirtiness
    check exists to prevent (#423 item 3).

    It also realigns this half with :func:`_path_under_any`, the Python `startswith`
    that filters the untracked half of the very same `has_changes_since` call. The two
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
    `has_changes_since` answer and must not disagree."""
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def untracked_files(repo: Path) -> set[str]:
    """Untracked, non-ignored paths (repo-relative posix), mirroring what a
    plain `git clean -fd` (no -x) treats as removable. Ignored files are
    excluded, so they are never rollback candidates.

    Reads stdout ALONE (`_git_out`): `ls-files` exits 0 while still writing to
    stderr, and against `_git`'s merged stream that chatter splits into a phantom
    untracked path — a PRISTINE tree answers with one, and because this function's
    contract is what `git clean -fd` would remove, the phantom is a rollback
    candidate. Silent, on every host whose git config warns (#442)."""
    rc, out, detail = _git_out(repo, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        raise GitError(f"git ls-files --others failed in {repo}: {detail}")
    return {line.strip() for line in out.splitlines() if line.strip()}


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
    which would also disarm the `:(exclude)` magic `worktree_clean`, `has_changes_since`
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


def path_tracked_file(repo: Path, rel: str) -> bool:
    """True when repo-relative posix ``rel`` is tracked AND names a regular FILE rather
    than a directory prefix.

    The distinction :func:`path_tracked` deliberately does not draw. Its literal
    pathspec matches a directory prefix too — that is load-bearing there, which is why
    `_bmad/render` answers True for the whole tree beneath it — so a caller that must
    know *which* of the two it holds cannot get it from that boolean.

    The one caller is the worktree git-add shield, where the two cases want OPPOSITE
    treatment (#392). Measured, git 2.55.0:

    * An exclude pattern naming a tracked regular file suppresses NOTHING. git consults
      ignore rules only for untracked paths, so `git add -A` stages a modification to it
      regardless. The pattern's only effect is to make the file answer
      `ls-files -ci --exclude-standard`, i.e. read as tracked-and-ignored — which is a
      state repo-hygiene gates reject, and how a shield meant to keep the orchestrator's
      files OUT of a story commit came to block one instead.
    * The same pattern over a tracked DIRECTORY really does hide new children, so it
      stays. There is no pattern shape that keeps that and clears the `-ci` report:
      `dir/*`, `dir/**` and a trailing negation all measured identical to `dir`, because
      gitignore cannot re-include anything under an excluded parent.

    `-z` and the BYTES accessor, unlike the sibling above. This reads the output's TEXT
    rather than only its emptiness, so the sibling's reason for never looking —
    `core.quotePath` mangling non-ASCII names — becomes this function's problem instead.
    NUL-delimited output is never quoted, and comparing `os.fsencode(rel)` keeps a POSIX
    name that is undecodable in the locale codec comparable rather than raising (#377).

    Raises GitError like every other probe in this module; the shield's caller degrades
    by KEEPING the pattern, since a leaked seed file in a story commit is the worse of
    the two failures."""
    proc = git_bytes(repo, "ls-files", "-z", "--", *_literal_specs([rel]))
    if proc.returncode != 0:
        merged = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        raise GitError(f"git ls-files -z -- {rel} failed in {repo}: {merged}")
    return {entry for entry in proc.stdout.split(b"\0") if entry} == {os.fsencode(rel)}


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


def commits_above(repo: Path, baseline: str) -> list[str]:
    """Commit shas reachable from HEAD but not from ``baseline`` — the commits an
    attempt added on top of its pre-attempt baseline, in ``git rev-list`` order (do
    not assume a strict newest-first / HEAD-first ordering across merges or clock
    skew; callers that need the tip should read HEAD directly). Empty when HEAD is
    at or behind baseline. Raises GitError on a git failure (a bad baseline is a
    real error, never quietly "no commits").

    Reads stdout ALONE (`_git_out`): git exits 0 while still warning on stderr, and
    against the merged stream that warning is a phantom sha handed to
    :func:`preserve_commits` — "Empty when HEAD is at or behind baseline" stops
    holding on any host whose git config warns (#442)."""
    rc, out, detail = _git_out(repo, "rev-list", f"{baseline}..HEAD")
    if rc != 0:
        raise GitError(f"git rev-list {baseline}..HEAD failed in {repo}: {detail}")
    return [line for line in out.splitlines() if line]


def preserve_commits(
    repo: Path, baseline: str, ref_name: str, commits: list[str] | None = None
) -> str | None:
    """Park the commits an attempt made above ``baseline`` under a branch at HEAD
    so a following ``git reset --hard baseline`` cannot orphan them — they survive
    `git gc` and are recoverable by name, not just via the reflog. Returns
    ``ref_name`` on success; ``None`` when there is nothing to preserve (HEAD at/
    below baseline) or the branch could not be created (the caller must then refuse
    to reset rather than silently destroy committed work). ``-f`` because a retry
    within the same run may re-preserve the same head under the same name.

    ``commits`` lets a caller that already ran :func:`commits_above` pass the result
    in to skip a second ``git rev-list`` subprocess; ``None`` self-fetches (keeps the
    helper standalone/testable).

    ``None`` means *nothing to preserve* — never a failure. If commits exist but the
    branch cannot be created this raises :class:`GitError` (consistent with the rest
    of this module), so a caller can never mistake a preservation failure for a
    harmless no-op and reset past committed work."""
    if commits is None:
        commits = commits_above(repo, baseline)
    if not commits:
        return None
    rc, out = _git(repo, "branch", "-f", ref_name, "HEAD")
    if rc != 0:
        raise GitError(f"git branch -f {ref_name} HEAD failed in {repo}: {out}")
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
    when the baseline was captured). This mirrors :func:`safe_rollback`'s scope
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
            new = sorted(untracked_files(repo) - set(baseline_untracked))
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

    created = untracked_files(repo) - set(baseline_untracked)
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
            # `follow_symlinks=False` is a real change here (a bare `write_bytes`
            # opens the name and so writes THROUGH a link), and it is the right
            # one twice over: `policy.write_mux_backend` already replaces this same
            # file by name, so no link at this path survives the orchestrator
            # anyway; and `runsetup` states a driven session can write
            # `.bmad-loop/policy.toml`, so honouring a link planted there would aim
            # a host-side write at a path of that session's choosing.
            atomic_write_bytes(policy_path, policy_content, follow_symlinks=False)
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


def create_branch(repo: Path, name: str, base: str) -> None:
    """Create branch `name` at `base` without checking it out."""
    rc, out = _git(repo, "branch", name, base)
    if rc != 0:
        raise GitError(f"git branch {name} {base} failed in {repo}: {out}")


def delete_branch(repo: Path, name: str, force: bool = False) -> None:
    rc, out = _git(repo, "branch", "-D" if force else "-d", name)
    if rc != 0:
        raise GitError(f"git branch -d {name} failed in {repo}: {out}")


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


def worktree_list(repo: Path) -> list[Path]:
    """Paths of every worktree attached to `repo` (the main checkout first).

    Reads stdout ALONE (`_git_out`) so the record parse does not depend on no
    stderr line ever starting with ``"worktree "``. The advisories measured for
    #442 — an unknown `core.fsyncMethod` value and its family — do NOT start that
    way, so the `startswith` filter screens them out and this parse was correct by
    accident rather than by construction; the filter stays as a second, independent
    screen."""
    rc, out, detail = _git_out(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        raise GitError(f"git worktree list failed in {repo}: {detail}")
    paths = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :]))
    return paths


def dirty_paths(repo: Path) -> dict[str, str]:
    """Repo-relative posix path -> two-char porcelain XY status for every dirty
    entry in `repo`'s working tree. Excludes the orchestrator's own working dir
    (.bmad-loop/) — config, ledger, run state, engine plugins — none of which is
    ever a unit's merged content. NUL-delimited (`-z`) so paths with spaces/unicode
    and rename forms parse without C-quoting; for a rename the *destination* path
    (the one now on disk) is what's recorded. `-uall` lists individual untracked
    files (not a collapsed parent dir) so each entry can be matched 1:1 against a
    branch's incoming paths."""
    rc, out = _git_raw(
        repo, "status", "--porcelain", "-z", "-uall", "--", ".", f":(exclude){AUTOMATOR_DIR_REL}"
    )
    if rc != 0:
        raise GitError(f"git status failed in {repo}")
    tokens = out.split("\0")
    result: dict[str, str] = {}
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
        result[path] = xy
        i += 1
    return result


def branch_incoming_paths(repo: Path, target: str, branch: str) -> set[str]:
    """The set of repo-relative posix paths a merge of `branch` into `target`
    would introduce or modify (`git diff --name-only target branch`)."""
    rc, out = _git_raw(repo, "diff", "--name-only", "-z", target, branch)
    if rc != 0:
        raise GitError(f"git diff --name-only {target} {branch} failed in {repo}")
    return {p for p in out.split("\0") if p}


def clean_incoming_collisions(
    repo: Path,
    target: str,
    branch: str,
    *,
    protected: tuple[str, ...] = (),
    on_tolerated: Callable[[list[str]], None] | None = None,
) -> list[str]:
    """Reconcile a target checkout dirtied by a per-worktree Unity Editor so the
    merge of `branch` can proceed, returning the cleaned paths (empty when the
    tree was already clean).

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
    dirty = dirty_paths(repo)
    if not dirty:
        return []
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
    tolerated = list(stray)
    if tolerated and on_tolerated is not None:
        on_tolerated(tolerated)
    # Resolve every untracked cleanup parent before deleting or checking out any
    # path. A later resolution fault must not leave an earlier collision cleaned
    # and the checkout only partly reconciled.
    repo_res = repo.resolve()
    prune_starts: dict[str, Path] = {}
    for path, xy in sorted(dirty.items()):
        if path not in incoming or not xy.startswith("??"):
            continue
        parent = (repo / path).parent.resolve()
        if parent != repo_res and not parent.is_relative_to(repo_res):
            raise OSError(
                f"refusing to clean incoming collision outside repository {repo_res}: "
                f"{repo / path}"
            )
        prune_starts[path] = parent
    cleaned: list[str] = []
    for path, xy in sorted(dirty.items()):
        if path not in incoming:
            continue  # tolerated untracked stray — never cleaned, never reported (#460)
        if xy.startswith("??"):  # untracked: delete it, then prune emptied dirs
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
) -> None:
    """Merge `branch` into the branch currently checked out in `repo`.

    strategy: "ff" (fast-forward only), "merge" (always a merge commit), or
    "squash" (collapse to one commit). Raises MergeConflictError on conflict and
    MergePreflightError when an ff-only merge can't fast-forward, restoring the
    tree to its pre-merge state.
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
    if strategy == "ff":
        pre_dirty_paths, pre_untracked = _residue_snapshot(repo)
        rc, out = _git(repo, "merge", "--ff-only", branch)
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
        return
    if strategy == "merge":
        msg = message or f"Merge branch '{branch}'"
        pre_dirty_paths, pre_untracked = _residue_snapshot(repo)
        rc, out = _git(repo, "merge", "--no-ff", "-m", msg, branch)
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
        return
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
        rc, out = _git(repo, "merge", "--squash", branch)
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
                return
        msg = message or f"Squash-merge branch '{branch}'"
        rc, out = _git(repo, "commit", "-m", msg)
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
        return
    raise GitError(f"unknown merge strategy: {strategy!r}")


def capture_diff(repo: Path, baseline: str, *, max_file_bytes: int | None = None) -> str:
    """Full unified diff of `repo`'s working tree against `baseline`, including
    untracked (but not ignored) files. Used to preserve a failed unit's changes
    for forensics. Returns "" when there is nothing to capture.

    Unlike `_git`, the tracked diff is read from stdout alone and left verbatim
    (no strip, no stderr merge) so the patch stays applyable, as is the
    `--no-index` spawn below it. The untracked leg now matches those two
    (`_git_out`, #442): its `ls-files` exits 0 while still
    warning on stderr, so against the merged stream the warning splits off as a
    phantom rel. Measured, that phantom is inert here — `diff --no-index` cannot
    access it and exits 1, exactly the code the loop below already tolerates as
    "the files differ", with empty stdout — so this leg is converted for the same
    reason its two neighbours read stdout alone, not on a demonstrated corruption.

    max_file_bytes caps the size of each *untracked* file included: a file larger
    than the cap is skipped and replaced with a one-line marker naming it and its
    size, so a stray build dir or huge log can't balloon the patch. None lifts the
    cap (capture everything regardless of size).
    """
    proc = _run_git(["git", "-C", str(repo), "diff", baseline, "--"], repo)
    if proc.returncode != 0:
        raise GitError(f"git diff {baseline} failed in {repo}: {proc.stderr.strip()}")
    parts = [proc.stdout]

    rc, out, detail = _git_out(repo, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        raise GitError(f"git ls-files --others failed in {repo}: {detail}")
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
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


def set_frontmatter_field(path: Path, key: str, value: str) -> bool:
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

    Atomic on the same terms too (#379) — `platform_util.atomic_write_bytes`,
    ``follow_symlinks=False``, matching what `devcontract._atomic_write_spec`
    already does to the same files. Use the BYTES helper and not the text one:
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
    atomic_write_bytes(path, (before + edited + after).encode("utf-8"), follow_symlinks=False)
    return True


def artifact_relpaths(paths: ProjectPaths) -> tuple[str, ...]:
    """Repo-relative posix prefixes of the orchestrator-owned BMAD artifact
    folders (the output root and the implementation/planning artifact dirs),
    relative to ``paths.project``. Folders configured outside the project tree
    are skipped — nothing to exclude there. The same set as
    ``Engine._protected_relpaths``; the dev/bundle proof-of-work gate passes
    these to ``has_changes_since`` so spec-only edits never count as real work."""
    out: list[str] = []
    for folder in (
        paths.output_folder,
        paths.implementation_artifacts,
        paths.planning_artifacts,
    ):
        try:
            rel = folder.relative_to(paths.project).as_posix()
        except ValueError:
            continue  # configured outside the project tree; nothing to exclude here
        # A folder == project root yields ".", which as an exclude prefix would
        # disable change detection for the whole tree — drop it.
        if rel and rel != ".":
            out.append(rel)
    return tuple(out)


def verify_dev_exclude_relpaths(
    paths: ProjectPaths, spec_path: Path, restore_patch: str | None = None
) -> tuple[str, ...]:
    """Repo-relative posix paths the dev/bundle proof-of-work gate excludes from
    `has_changes_since` — file-granularity, unlike `artifact_relpaths`' whole-folder
    exclusion (still used as-is by `Engine._protected_relpaths` for rollback
    protection, a different job). Deliberately does NOT exclude `output_folder`:
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
    status flip on the session's own spec count as real work."""
    candidates: list[Path] = [paths.sprint_status, spec_path]
    if restore_patch:
        candidates.append(resolve_restore_path(restore_patch, paths.project))
    out: list[str] = []
    project = paths.project
    for path in candidates:
        try:
            rel = path.resolve().relative_to(project).as_posix()
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
    except (OSError, RuntimeError):
        return False


def resolve_spec_path(spec_file: str, paths: ProjectPaths) -> Path:
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


def _verify_shared_gates(
    spec_path: Path,
    rj: dict[str, Any],
    task: StoryTask,
    paths: ProjectPaths,
    *,
    expected_status: str,
    extra_exclude: tuple[str, ...] | None,
    allow_ancestor_baseline: bool = False,
    fm: dict[str, Any] | None = None,
) -> VerifyOutcome | None:
    """The workflow-tag, expected-status, baseline-match, and proof-of-work gates
    shared verbatim by :func:`verify_dev`, :func:`verify_dev_bundle`, and
    :func:`verify_dev_stories` — factored out so the sprint-mode and stories-mode
    gates can't silently drift. Reads frontmatter once; a caller that had to read
    it first to *choose* ``expected_status`` passes what it read as ``fm`` so the
    single-read contract still holds (no caller re-reads it).  Returns a failing
    :class:`VerifyOutcome`, or ``None`` when every gate passes and the caller may
    run its mode-specific tail.

    The proof-of-work exclude is derived here from the `task` this gate already
    receives (`verify_dev_exclude_relpaths`, which needs the latched restore patch);
    ``extra_exclude`` carries only what a mode adds on top — ``()`` for sprint and
    bundle, the story record + manifest for stories. Threading the restore patch in
    from three call sites instead left a default-None foot-gun for a future fourth
    mode, which would silently let a restore re-drive pass proof-of-work on the
    patch file's mere presence. ``extra_exclude=None`` still skips the gate outright
    (a plan-halt leg produced only its own spec)."""
    workflow = rj.get("workflow")
    if workflow != DEV_WORKFLOW:
        return VerifyOutcome.retry(
            f"dev result.json workflow is {workflow!r}, expected {DEV_WORKFLOW!r}"
        )

    if fm is None:
        read = _gate_frontmatter(spec_path)
        if isinstance(read, VerifyOutcome):
            return read
        fm = read
    status = status_of(fm)
    if status != expected_status:
        return VerifyOutcome.retry(
            f"spec status is {status!r}, expected {expected_status!r}: {spec_path}"
        )

    # The generic bmad-build-auto skill stamps `baseline_revision`, never
    # `baseline_commit` — that name exists only in the result.json devcontract
    # synthesizes, which this gate does not consult (it re-reads frontmatter).
    # An absent key skips the check below, so reading `baseline_commit` alone
    # made this gate dead code for every generic-skill session. Read both, the
    # same idiom as `devcontract.synthesize_result`.
    claimed_baseline = str(fm.get("baseline_commit", fm.get("baseline_revision", ""))).strip()
    proof_baseline: str = task.baseline_commit or ""
    include_untracked_proof = True
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        try:
            canonical_claimed = _canonical_commit_oid(paths.project, claimed_baseline)
        except GitError as e:
            return VerifyOutcome.escalate(str(e))
        if canonical_claimed is None:
            return VerifyOutcome.retry(
                f"spec baseline {claimed_baseline[:12]} does not match "
                f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
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
                paths.project, canonical_claimed, task.baseline_commit
            )
            # The other direction needs no opt-in flag: an intervening commit
            # before step-03 stamps `baseline_revision` makes the claim newer
            # than the recorded baseline. Accept it only when this checkout's
            # HEAD reaches that canonical descendant; stale, diverged, unknown,
            # and off-HEAD commits still fail.
            newer_ok = commit_reachable_above_baseline(
                paths.project, canonical_claimed, task.baseline_commit
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
                return VerifyOutcome.retry(
                    f"spec baseline {claimed_baseline[:12]} does not match "
                    f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
                )

    if extra_exclude is not None and task.baseline_commit:
        exclude = verify_dev_exclude_relpaths(paths, spec_path, task.restore_patch) + extra_exclude
        try:
            if not has_changes_since(
                paths.project,
                proof_baseline,
                exclude=exclude,
                baseline_untracked=task.baseline_untracked,
                include_untracked=include_untracked_proof,
            ):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

    return None


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
    follows, ``done`` when review is disabled), records a baseline matching the
    orchestrator's, has produced changes since that baseline, and that the
    story's sprint-status was advanced to the matching stage. Returns a retryable
    VerifyOutcome on any mismatch, escalates on git failure, passes otherwise.

    ``operator_park`` (``[operator] enabled``, engine-supplied) adds one more
    accepted spec/sprint pair: ``(awaiting-operator, awaiting-operator)``, the
    park a dev session declares when the story's remaining work is a human's
    (#335). The OBSERVED spec status selects which pair is demanded — the skill
    decides whether it parked, and the gate then holds it to the matching board
    state and to a non-empty action list. Off by policy, the token is simply not
    a terminal the gate knows, so it fails the ordinary status check and the
    session is retried with that mismatch as feedback.

    ``engine_written`` names project-relative paths the orchestrator itself
    wrote above this gate during the attempt. They compose with the mode's normal
    proof-of-work exclusions so engine bookkeeping cannot masquerade as session
    work; see :meth:`Engine._harvest_gate_exclude`.
    """
    rj = result_json or {}
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
        extra_exclude=engine_written,
        fm=fm,
    )
    if gate is not None:
        return gate

    expected_sprint = AWAITING_OPERATOR if parked else ("review" if review_enabled else "done")
    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != expected_sprint:
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected {expected_sprint!r}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


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

    ``engine_written`` has the same contract as :func:`verify_dev`."""
    rj = result_json or {}
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

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
    )
    if gate is not None:
        return gate

    claimed_ids = {str(i) for i in (rj.get("dw_ids") or [])}
    if claimed_ids and claimed_ids != set(task.dw_ids):
        return VerifyOutcome.retry(
            f"result.json dw_ids {sorted(claimed_ids)} do not match the bundle's "
            f"{sorted(task.dw_ids)}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


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
    ``ready-for-dev`` can't be mistaken for a successful plan.
    """
    # Deferred to avoid a verify<->stories import cycle: stories imports
    # read_frontmatter/status_of from this module at top level, so verify must not
    # import stories at module scope (keep this local on any future refactor).
    from . import stories

    rj = result_json or {}
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

    # A plan-halt leg produced only its own spec (the plan), which proof-of-work
    # already excludes; skip it (extra_exclude=None) and record the plan spec.
    # Otherwise stories mode adds the spec folder's stories/ subdir + stories.yaml
    # on top of the gate's own file-granular exclude — NOT the whole-folder
    # artifact_relpaths, so a story whose entire authorized scope is ledger/spec
    # reconciliation doesn't register as a false "no changes". Engine-written
    # paths compose only on that live-gate leg; ``None`` must remain ``None`` for
    # plan halt rather than being combined with a tuple.
    gate = _verify_shared_gates(
        spec_path,
        rj,
        task,
        paths,
        expected_status=expected,
        extra_exclude=(
            None if plan_halt else _stories_relpaths(paths.project, spec_folder) + engine_written
        ),
    )
    if gate is not None:
        return gate

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


def _stories_relpaths(project: Path, spec_folder: Path) -> tuple[str, ...]:
    """Proof-of-work exclude prefixes for the story record + manifest: the spec
    folder's ``stories/`` subdir and its ``stories.yaml``, project-relative. Empty
    when the spec folder is outside the project tree (nothing to exclude there)."""
    from .stories import STORIES_FILENAME, STORIES_SUBDIR

    try:
        rel = spec_folder.resolve().relative_to(project.resolve()).as_posix()
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
    """

    command: str
    returncode: int
    output_tail: str
    stdout: str = ""
    stderr: str = ""
    stdout_full_bytes: int | None = None
    stderr_full_bytes: int | None = None


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
        # the timeout sentinel: the command ran and hung, so it was found and it
        # was runnable — none of the signals below can apply to it.
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
    the host shell, and sh and cmd signal a broken environment differently."""
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
    ``encoding="utf-8"`` because its child is our own UTF-8 CLI."""
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
        except subprocess.TimeoutExpired as exc:
            # the timeout leg is bounded too: a command killed at COMMAND_TIMEOUT_S
            # is exactly the one that may have been spewing output when it died.
            t_out, t_out_full = byte_tail(_timeout_stream(exc.stdout), MAX_STREAM_MEMORY_BYTES)
            t_err, t_err_full = byte_tail(_timeout_stream(exc.stderr), MAX_STREAM_MEMORY_BYTES)
            results.append(
                CommandResult(command, -1, "timed out", t_out, t_err, t_out_full, t_err_full)
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
            return VerifyOutcome.escalate(
                f"verify environment fault ({reason}): {result.command}\n"
                "command not found / not executable — this is the run environment, "
                "not the story; fix the environment, then re-arm the escalation "
                "(the attempt budget resets on re-arm)\n"
                f"{result.output_tail}",
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


def verify_commands_outcome(policy: Policy, cwd: Path) -> VerifyOutcome:
    """Run the policy's deterministic verify commands and classify the results."""
    return verify_command_results_outcome(run_verify_commands(policy, cwd), cwd)


def verify_review(
    task: StoryTask,
    paths: ProjectPaths,
    policy: Policy,
    *,
    sprint_reached_done: bool = False,
    operator_park: bool = False,
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
    clears — the pair, a non-empty action list, and the verify commands. The
    sign-off-regression arm stays scoped to the ``done`` pair: a board short of
    ``awaiting-operator`` is a stage never reached, not a revoked sign-off.

    ``operator_park`` is the SAME engine-supplied flag ``verify_dev`` takes, not a
    second reading of ``policy.operator.enabled``, so the two gates cannot
    disagree about whether this run parks. They would: the engine's
    ``_operator_park_enabled`` is an override seam, and a mode that opts out of
    parking while still reaching this gate would otherwise find it accepting a
    park the engine itself refuses to take."""
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

    return verify_commands_outcome(policy, paths.project)


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


def verify_review_stories(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    """verify_review for stories mode: same spec-done + verify-commands gates,
    minus the sprint-status gate (stories mode has no sprint board — the story
    spec's own frontmatter status is authoritative). ``task.spec_file`` is the
    id-keyed story spec ``verify_dev_stories`` recorded on the dev pass."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = _gate_frontmatter(Path(task.spec_file))
    if isinstance(fm, VerifyOutcome):
        return fm
    status = status_of(fm)
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")
    return verify_commands_outcome(policy, paths.project)


def verify_review_bundle(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    """verify_review for a deferred-work bundle: no sprint-status check, but
    every dw id the bundle owns must be marked done in the ledger on disk. The
    legacy --dw-bundle skill flips them; on the generic bmad-build-auto path the
    orchestrator flips them after dev and, if review rewrites the ledger diff,
    again immediately before this review gate. Either way this gate is why we
    can trust it happened."""
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
    try:
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    except OSError as exc:
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

    return verify_commands_outcome(policy, paths.project)


def commit_story(repo: Path, message: str) -> str:
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


def finalize_commit(repo: Path, baseline: str | None, message: str) -> str | None:
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

    Mechanics: stage the working tree (`add -A`), move HEAD back to `baseline`
    keeping the index (`reset --soft`), then commit the accumulated index. The
    working tree is never touched, so a failure leaves the chain intact.

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
    rc, out = _git(repo, "reset", "--soft", baseline)
    if rc != 0:
        raise GitError(f"git reset --soft {baseline} failed: {out}")
    # index now holds the cumulative diff vs baseline; nothing staged → no-op
    rc, _ = _git(repo, "diff", "--cached", "--quiet")
    if rc == 0:
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
    return rev_parse_head(repo)


def resolve_restore_path(raw: str, root: Path) -> Path:
    """The latched intent-gap patch (`StoryTask.restore_patch`) as a concrete path:
    absolute values pass through, relative ones are anchored on `root`.

    `model.StoryTask.restore_patch` documents the field as repo-relative-or-absolute,
    and every consumer must resolve it against the base it actually reads the tree
    from — the engine's live workspace root (the unit worktree under isolation),
    `paths.project` for the proof-of-work exclude, the CLI's `--project`. Hence the
    caller-supplied `root` rather than one baked-in base.

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
    the call raises instead of reporting a successful no-op."""
    rels: list[str] = []
    resolution_fault: tuple[Path, OSError | RuntimeError] | None = None
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
            if resolution_fault is None:
                resolution_fault = (Path(p), e)
            continue
        except ValueError:
            continue
    missing = [r for r in rels if not ((repo_root / r).exists() or (repo_root / r).is_symlink())]
    if missing:
        rc, out = _git_raw(repo, "ls-files", "-z", "--", *_literal_specs(missing))
        if rc != 0:
            raise GitError(f"git ls-files failed: {out}")
        tracked = {t for t in out.split("\0") if t}
        rels = [r for r in rels if r not in missing or r in tracked]
    if not rels:
        if resolution_fault is not None:
            failed_path, error = resolution_fault
            raise GitError(
                "no exact commit operand remains after path resolution failed "
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
