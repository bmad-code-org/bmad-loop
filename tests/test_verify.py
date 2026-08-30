import dataclasses
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    _FAIL,
    _OK,
    MISSING_TOOL_CMD,
    OMIT,
    PROJECT_MARKER_CMD,
    REPO_ROOT_MARKER_CMD,
    UNRESOLVABLE,
    _Omit,
    fault_read_text,
    git,
    make_git_noisy,
    nested_repo_root_paths,
    plant_root_markers,
    refuse_to_resolve,
    spec_path,
    write_spec,
    write_sprint,
)

from bmad_loop import platform_util, verify
from bmad_loop.model import StoryTask
from bmad_loop.policy import Policy, ReviewPolicy, VerifyPolicy


def make_task(paths, story_key="1-1-a"):
    task = StoryTask(story_key=story_key, epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def dev_result(sp):
    return {"workflow": "auto-dev", "spec_file": str(sp)}


def _codec_rejects_bad_byte() -> bool:
    """Whether byte 0xff is undecodable in the codec ``text=True`` will use.

    Probes ``TextIOWrapper`` with ``encoding=None`` rather than naming a codec,
    because that is the very default ``subprocess``'s text mode resolves for the
    child's streams — asking the machinery beats predicting it from the locale.
    """
    try:
        io.TextIOWrapper(io.BytesIO(b"\xff")).read()
    except UnicodeDecodeError:
        return True
    return False


def _write_ambiguous_commit_prefix(repo: Path) -> tuple[str, tuple[str, str]]:
    """Write two valid commit objects sharing a seven-hex prefix.

    Generate candidate object bytes in memory so the fixture pays only two Git
    subprocesses rather than the birthday search's roughly 20,000 attempts.
    """
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    parent = git(repo, "rev-parse", "HEAD")
    fixed = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        "author Test <test@example.com> 0 +0000\n"
        "committer Test <test@example.com> 0 +0000\n\n"
    )
    seen: dict[str, tuple[str, bytes]] = {}
    pair: tuple[tuple[str, bytes], tuple[str, bytes]] | None = None
    for nonce in range(500_000):
        body = f"{fixed}ambiguous-prefix-{nonce}\n".encode()
        serialized = f"commit {len(body)}\0".encode() + body
        oid = hashlib.sha1(serialized, usedforsecurity=False).hexdigest()
        prefix = oid[:7]
        previous = seen.get(prefix)
        if previous is not None and previous[0] != oid:
            pair = (previous, (oid, body))
            break
        seen[prefix] = (oid, body)
    if pair is None:  # pragma: no cover - collision probability is effectively 1
        raise AssertionError("failed to generate a seven-hex commit collision")

    for expected_oid, body in pair:
        proc = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
            input=body,
            capture_output=True,
            check=True,
        )
        assert proc.stdout.decode().strip() == expected_oid
    return pair[0][0][:7], (pair[0][0], pair[1][0])


# Guard for every test whose subject is a STRICT DECODE of subprocess output —
# the #378 verify-command pair below and the #377 git-chokepoint test above them.
# Byte 0xff is undecodable only in UTF-8/ASCII; every ISO-8859-x and cp125x codec
# maps all 256 byte values, so under such a locale the strict decode never raises
# and those tests pass *with the bug restored* — a silent vacuity rather than a
# failure (verified: under LC_ALL=et_EE.iso885915 the #378 ablation passes). No
# single byte is undecodable everywhere, so this skips instead, leaving each test
# either exercising the fault or saying plainly that it did not. CI is always on
# the exercising side: the Linux legs run UTF-8 and the Windows legs set
# PYTHONUTF8=1 (.github/workflows/ci.yml).
needs_strict_codec = pytest.mark.skipif(
    not _codec_rejects_bad_byte(),
    reason="host codec decodes 0xff (e.g. an ISO-8859-x locale), so nothing here "
    "would exercise the strict decode this fix is about",
)


def test_attempt_dirty_clean_tree(project):
    """At baseline with no changes — nothing for a rollback to undo."""
    baseline = verify.rev_parse_head(project.project)
    assert verify.attempt_dirty(project.project, baseline, []) is False


def test_attempt_dirty_tracked_change(project):
    """A modified tracked file is a tracked diff vs baseline."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("changed\n")
    assert verify.attempt_dirty(project.project, baseline, []) is True


def test_file_bytes_at_revision_distinguishes_blob_absence_tree_and_git_failure(project):
    """The baseline oracle returns only proven blob bytes, never tree listings.

    Ablation: drop either side of ``entry is None or entry[1] != "blob"`` from
    either oracle — four mutations, each reddening exactly one of the four ``is
    None`` assertions. The absence side raises ``TypeError`` on the ``missing.bin``
    case; the type side reddens the ``oracle`` case, and only there do the two
    oracles differ. Plain ``cat-file blob`` is refused by git on a tree oid, so
    that mutation still fails loudly; ``cat-file --filters`` instead renders the
    tree's listing and hands it back as file content, which nothing but this
    clause keeps out of a baseline comparison.
    """
    repo = project.project
    nested = repo / "oracle" / "spec.bin"
    nested.parent.mkdir()
    expected = b"\x00byte-exact\xff\r\n"
    nested.write_bytes(expected)
    git(repo, "add", "oracle")
    git(repo, "commit", "-q", "-m", "binary oracle fixture")
    baseline = verify.rev_parse_head(repo)

    assert verify.file_bytes_at_revision(repo, baseline, "oracle/spec.bin") == expected
    assert verify.worktree_file_bytes_at_revision(repo, baseline, "oracle/spec.bin") == expected
    assert verify.file_bytes_at_revision(repo, baseline, "missing.bin") is None
    assert verify.worktree_file_bytes_at_revision(repo, baseline, "missing.bin") is None
    assert verify.file_bytes_at_revision(repo, baseline, "oracle") is None
    assert verify.worktree_file_bytes_at_revision(repo, baseline, "oracle") is None
    assert not verify.path_is_non_regular_at_revision(repo, baseline, "oracle/spec.bin")
    assert not verify.path_is_non_regular_at_revision(repo, baseline, "missing.bin")
    assert verify.path_is_non_regular_at_revision(repo, baseline, "oracle")
    with pytest.raises(verify.GitError, match="ls-tree"):
        verify.file_bytes_at_revision(repo, "not-a-revision", "oracle/spec.bin")
    with pytest.raises(verify.GitError, match="ls-tree"):
        verify.worktree_file_bytes_at_revision(repo, "not-a-revision", "oracle/spec.bin")


def test_worktree_file_bytes_at_revision_applies_checkout_eol_filters(project):
    """The live-baseline oracle materializes bytes as Git checkout would.

    Ablation: replace ``cat-file --filters --path`` with raw ``cat-file blob``
    and the filtered assertion returns LF instead of the CRLF checkout form.
    """
    repo = project.project
    git(repo, "config", "core.autocrlf", "true")
    rel = "filtered-baseline.md"
    path = repo / rel
    blob_bytes = b"line one\nline two\n"
    path.write_bytes(blob_bytes)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", "filtered baseline fixture")
    baseline = verify.rev_parse_head(repo)
    path.unlink()
    git(repo, "checkout", "--", rel)

    assert path.read_bytes() == b"line one\r\nline two\r\n"
    assert verify.file_bytes_at_revision(repo, baseline, rel) == blob_bytes
    assert verify.worktree_file_bytes_at_revision(repo, baseline, rel) == path.read_bytes()


@pytest.mark.parametrize("baseline_present", [False, True], ids=["absent", "tracked"])
def test_reset_index_path_restores_baseline_ownership_without_rewriting_bytes(
    project, baseline_present
):
    repo = project.project
    path = repo / "index-owned.md"
    if baseline_present:
        path.write_text("baseline\n")
        git(repo, "add", "index-owned.md")
        git(repo, "commit", "-q", "-m", "tracked index baseline")
    baseline = verify.rev_parse_head(repo)
    path.write_text("working bytes\n")
    if baseline_present:
        git(repo, "rm", "--cached", "index-owned.md")
    else:
        git(repo, "add", "index-owned.md")
    assert verify.index_path_changed_since(repo, baseline, "index-owned.md")

    verify.reset_index_path(repo, baseline, "index-owned.md")

    assert not verify.index_path_changed_since(repo, baseline, "index-owned.md")
    assert verify.path_tracked(repo, "index-owned.md") is baseline_present
    assert path.read_text() == "working bytes\n"


def test_attempt_dirty_run_created_untracked(project):
    """An untracked file absent from the baseline snapshot was created by this
    attempt → dirty."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "new.txt").write_text("fresh\n")
    assert verify.attempt_dirty(project.project, baseline, []) is True


def test_attempt_dirty_preexisting_untracked_ignored(project):
    """An untracked file already in the baseline snapshot is the user's, not this
    attempt's — clean."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "keep.txt").write_text("mine\n")
    assert verify.attempt_dirty(project.project, baseline, ["keep.txt"]) is False


def test_attempt_dirty_none_snapshot_ignores_untracked(project):
    """No snapshot (pre-upgrade run): untracked files never count, only tracked
    diff does."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "new.txt").write_text("fresh\n")
    assert verify.attempt_dirty(project.project, baseline, None) is False
    (project.project / "src.txt").write_text("changed\n")
    assert verify.attempt_dirty(project.project, baseline, None) is True


def test_path_changed_since_detects_one_tracked_path(project):
    baseline = verify.rev_parse_head(project.project)

    assert verify.path_changed_since(project.project, baseline, "src.txt") is False

    (project.project / "src.txt").write_text("changed\n", encoding="utf-8")
    assert verify.path_changed_since(project.project, baseline, "src.txt") is True


def test_path_changed_since_respects_the_untracked_baseline(project):
    baseline = verify.rev_parse_head(project.project)
    (project.project / "ledger[1].md").write_text("finding\n", encoding="utf-8")

    assert verify.path_changed_since(
        project.project,
        baseline,
        "ledger[1].md",
        baseline_untracked=[],
    )
    assert not verify.path_changed_since(
        project.project,
        baseline,
        "ledger[1].md",
        baseline_untracked=["ledger[1].md"],
    )


def test_attempt_dirty_excludes_untracked_artifact(project):
    """A new untracked spec under an orchestrator-owned artifact folder is not the
    dev attempt's dirtiness when that folder is excluded — but counts otherwise."""
    repo = project.project
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()
    baseline = verify.rev_parse_head(repo)
    (project.implementation_artifacts / "spec-1-1-a.md").write_text("corrected\n")
    assert verify.attempt_dirty(repo, baseline, [], exclude=(artifact_rel,)) is False
    assert verify.attempt_dirty(repo, baseline, []) is True


def test_attempt_dirty_excludes_tracked_artifact(project):
    """A tracked edit confined to the artifact folder reads as clean when excluded;
    a source edit alongside it still counts."""
    repo = project.project
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("orig\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    spec.write_text("corrected by resolve\n")  # tracked artifact edit
    assert verify.attempt_dirty(repo, baseline, [], exclude=(artifact_rel,)) is False

    (repo / "src.txt").write_text("dev work\n")  # real source change
    assert verify.attempt_dirty(repo, baseline, [], exclude=(artifact_rel,)) is True


def _timing_out_run(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))


def test_git_timeout_becomes_git_error(project, monkeypatch):
    """#156: a git call exceeding the timeout must surface as GitError — the type
    every guard already handles — never as a raw TimeoutExpired, which bypassed
    them all and crashed the run from the rollback path."""
    baseline = verify.rev_parse_head(project.project)
    monkeypatch.setattr(verify.subprocess, "run", _timing_out_run)
    with pytest.raises(verify.GitError, match=r"git diff timed out after \d+s"):
        verify.attempt_dirty(project.project, baseline, [])


def test_capture_diff_timeout_becomes_git_error(project, monkeypatch):
    """capture_diff's inline git spawns (verbatim-stdout diff) share the same
    translation as the `_git` helpers."""
    baseline = verify.rev_parse_head(project.project)
    monkeypatch.setattr(verify.subprocess, "run", _timing_out_run)
    with pytest.raises(verify.GitError, match="timed out"):
        verify.capture_diff(project.project, baseline)


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: FileNotFoundError(2, "No such file or directory"),
        lambda: OSError(24, "Too many open files"),
        lambda: OSError(12, "Cannot allocate memory"),
    ],
    ids=["enoent", "emfile", "enomem"],
)
def test_git_spawn_oserror_becomes_git_spawn_error(project, monkeypatch, make_exc):
    """#343: a spawn-level OSError (EMFILE/ENOMEM, git gone from PATH) is raised
    by `subprocess.run` before any return code exists, so left untranslated it
    bypassed every `except GitError` guard and crashed the run. It must surface
    as GitSpawnError — a GitError to every existing guard, a distinct type for
    the callers that must tell an environment fault from git refusing.

    Ablation target: delete the `except OSError` arm in `_run_git` and this
    fails with the raw OSError."""
    exc = make_exc()

    def failing_run(cmd, **kwargs):
        raise exc

    monkeypatch.setattr(verify.subprocess, "run", failing_run)
    with pytest.raises(verify.GitError, match="git rev-parse failed to spawn") as excinfo:
        verify.rev_parse_head(project.project)
    assert isinstance(excinfo.value, verify.GitSpawnError)
    assert excinfo.value.__cause__ is exc  # errno stays reachable for callers


def test_configure_git_timeout_overrides_bound(project, monkeypatch):
    """The engine-applied `limits.git_timeout_s` value is what reaches
    subprocess.run; the module default stays GIT_TIMEOUT_S for standalone users."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spying_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", spying_run)
    verify.configure_git_timeout(7)
    try:
        verify.rev_parse_head(project.project)
    finally:
        verify.configure_git_timeout(verify.GIT_TIMEOUT_S)
    assert seen["timeout"] == 7


def test_git_bytes_per_call_timeout_overrides_module_state(project, monkeypatch):
    """`timeout_s=` wins over the engine-configured module bound for one call —
    the interactive callers' seam (#390): the TUI keeps its 5s modal deadline and
    install its 10s probe bound while standing inside the chokepoint, and a call
    that passes nothing keeps the module default. Ablation: ignore the param in
    `_run_git` (or drop `git_bytes`' passthrough) and the spied timeout is the
    120s default on the first call."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spying_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", spying_run)
    verify.git_bytes(project.project, "rev-parse", "HEAD", timeout_s=7)
    assert seen["timeout"] == 7
    verify.git_bytes(project.project, "rev-parse", "HEAD")
    assert seen["timeout"] == verify.GIT_TIMEOUT_S


def test_run_git_forces_c_locale(project, monkeypatch):
    """Every git child runs with LC_ALL=C so message text stays stable English —
    the chokepoint fix for #236 (safe_rollback's "did not match" tolerance must not
    be translated out from under it by a localized parent git). Spy on the actual
    subprocess env rather than the parent LANG, so the guarantee is pinned even on a
    CI box with no non-English locale catalogs installed (where a plain LANG=it_IT
    test would emit English anyway and pass without the fix)."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spying_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", spying_run)
    verify.rev_parse_head(project.project)
    assert seen["env"] is not None
    assert seen["env"]["LC_ALL"] == "C"


def test_run_git_locale_merge_preserves_explicit_env(project, monkeypatch):
    """The LC_ALL=C merge must not clobber a caller-supplied env — the `_git_env`
    callers pass a throwaway GIT_INDEX_FILE / synthetic commit identity that has to
    survive (else snapshot_worktree breaks). Assert both the forced LC_ALL and the
    caller's own key reach subprocess.run together."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spying_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", spying_run)
    verify._git_env(project.project, "status", env={**os.environ, "SENTINEL_X": "1"})
    assert seen["env"] is not None
    assert seen["env"]["LC_ALL"] == "C"
    assert seen["env"]["SENTINEL_X"] == "1"  # caller's env preserved


# ---- the chokepoint's third pre-returncode fault, and its bytes mode (#377)


@needs_strict_codec
@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows filenames are UTF-16; no undecodable path exists"
)
def test_z_output_undecodable_path_becomes_git_error(project):
    """#377: a filename carrying bytes invalid in the run's codec makes `_run_git`'s
    strict decode raise UnicodeDecodeError — a third fault raised before any return
    code exists, and the one the taxonomy missed. Being a ValueError it matched
    neither `except` arm, so it sailed past every `except GitError` guard: here
    `dirty_paths`, reached from `clean_incoming_collisions`' merge pre-flight, whose
    callers guard exactly that type.

    The file is real and so is git. Monkeypatching `subprocess.run` would hand the
    code str objects and never run the stdlib's decoding at all, so that version
    passes identically with the fix ablated (tests/test_install.py:1996 documents
    the same trap).

    Ablation: delete the `except UnicodeDecodeError` arm in `_run_git` and this
    fails with the raw UnicodeDecodeError."""
    repo = project.project
    # POSIX filenames are arbitrary bytes, so build the path as bytes — a str path
    # would have to carry the byte as a surrogate and re-encode on the way out.
    with open(os.fsencode(repo) + b"/weird-\xff-name.txt", "wb") as fh:
        fh.write(b"content\n")

    with pytest.raises(verify.GitError, match="git status returned undecodable output"):
        verify.dirty_paths(repo)

    # Why only the `-z` (and raw-diff) sites were ever exposed: plain porcelain
    # C-quotes the same path down to ASCII. This pins git's `core.quotePath`
    # default, not our fix — adding `-z` to one of the safe callers would
    # reintroduce the fault silently, and this is what would notice.
    rc, out = verify._git(repo, "status", "--porcelain")
    assert rc == 0 and r"weird-\377-name.txt" in out


def test_git_bytes_returns_bytes_and_reads_rc_as_an_answer(project):
    """`git_bytes` hands back the CompletedProcess whatever the rc, with stdout as
    raw bytes. Both halves are load-bearing for the bytes-mode callers: `git config
    --get` of an unset key exits 1 and that non-zero rc *is* the reply (it is what
    tells the git-add shield to enable the extension), so a chokepoint that raised
    on it could not serve them; and bytes are what let a path invalid in the locale
    codec through to an `os.fsdecode` at the point of use.

    Ablation: drop `binary=True` from `git_bytes` and the bytes assertions fail
    against str."""
    unset = verify.git_bytes(project.project, "config", "--get", "bmadloop.definitelyunset")
    assert unset.returncode == 1  # an answer, not a fault — and not raised
    assert unset.stdout == b""

    answered = verify.git_bytes(project.project, "rev-parse", "--abbrev-ref", "HEAD")
    assert answered.returncode == 0
    assert answered.stdout.strip() == b"main"  # bytes, not str


def test_git_bytes_inherits_locale_pin_and_timeout(project, monkeypatch):
    """What standing inside the chokepoint buys the bytes callers, asserted rather
    than assumed: the LC_ALL=C pin (#236) and the engine-set `limits.git_timeout_s`
    bound (#156) reach subprocess.run unchanged, with text mode off."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spying_run(cmd, **kwargs):
        seen.update(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", spying_run)
    verify.configure_git_timeout(9)
    try:
        verify.git_bytes(project.project, "rev-parse", "HEAD")
    finally:
        verify.configure_git_timeout(verify.GIT_TIMEOUT_S)

    assert not seen["text"]
    assert seen["timeout"] == 9
    assert seen["env"]["LC_ALL"] == "C"


def test_git_bytes_timeout_still_becomes_git_error(project, monkeypatch):
    """Bytes mode skips the decode, not the taxonomy: a timeout has no return code
    to hand back, so it still raises rather than becoming a CompletedProcess.

    Ablation: delete the `except subprocess.TimeoutExpired` arm and this fails with
    the raw TimeoutExpired."""
    monkeypatch.setattr(verify.subprocess, "run", _timing_out_run)
    with pytest.raises(verify.GitError, match=r"git config timed out after \d+s"):
        verify.git_bytes(project.project, "config", "--get", "core.excludesFile")


def test_git_timeout_carries_its_own_type(project, monkeypatch):
    """A hung git raises `GitTimeoutError` — a `GitError` like every other fault, so
    no existing guard changes, and a distinct type so the one caller that is about
    to spawn ANOTHER git can tell "git answered non-zero" (the next command answers
    just as fast) from "git does not return" (the next command pays the whole
    deadline again). `cli.cmd_validate` is that caller; the class is the only thing
    that separates the two, since both arrive as a bare `GitError` message today.

    Both halves are asserted deliberately: the subclass relation is what keeps the
    taxonomy backward compatible, and losing it would silently break every
    `except GitError` guard in the tree.

    Ablation: raise a bare `GitError` from `_run_git`'s `TimeoutExpired` arm and the
    type assertion fails while the `isinstance(..., GitError)` one stays green."""
    monkeypatch.setattr(verify.subprocess, "run", _timing_out_run)
    with pytest.raises(verify.GitTimeoutError, match=r"git config timed out after \d+s") as excinfo:
        verify.git_bytes(project.project, "config", "--get", "core.excludesFile")
    assert isinstance(excinfo.value, verify.GitError)


def test_git_bytes_spawn_oserror_still_becomes_git_spawn_error(project, monkeypatch):
    """Same for a spawn-level OSError — GitSpawnError, errno still reachable.

    Ablation: delete the `except OSError` arm and this fails with the raw OSError."""
    exc = OSError(24, "Too many open files")

    def failing_run(cmd, **kwargs):
        raise exc

    monkeypatch.setattr(verify.subprocess, "run", failing_run)
    with pytest.raises(verify.GitSpawnError, match="git config failed to spawn") as excinfo:
        verify.git_bytes(project.project, "config", "--get", "core.excludesFile")
    assert excinfo.value.__cause__ is exc


@pytest.mark.parametrize(
    "fm,expected",
    [
        ({"status": "in-review"}, "in-review"),
        ({"status": "  in-review  "}, "in-review"),
        ({"status": "In-Review"}, "in-review"),
        ({"status": "DONE"}, "done"),
        ({}, ""),  # missing key
        ({"status": None}, ""),  # YAML-null (bare `status:`) reads like a missing key
        ({"status": "none"}, "none"),  # a literal token stays itself
        ({"status": 123}, "123"),
    ],
)
def test_status_of_normalizes(fm, expected):
    assert verify.status_of(fm) == expected


def test_verify_dev_happy(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_spawn_fault_escalates(project, monkeypatch):
    """#343 acceptance, escalate class: with the OSError injected at
    `subprocess.run` itself, the shared change-gate's `except GitError` guard
    catches the translated GitSpawnError and escalates (CRITICAL, not
    retryable) instead of crashing. Baseline canonicalization now performs
    fail-closed Git reads first, so the injection targets the proof-of-work diff
    explicitly.

    Ablation target: delete the `except OSError` arm in `verify._run_git` and
    this fails with the raw OSError."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    real_run = verify.subprocess.run

    def cannot_spawn_diff(cmd, **kwargs):
        if cmd[3] == "diff" and "--quiet" in cmd:
            raise OSError(12, "Cannot allocate memory")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", cannot_spawn_diff)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and not out.retryable
    assert out.severity == "CRITICAL"
    assert "failed to spawn" in out.reason


@pytest.mark.parametrize("subcommand", ["rev-parse", "cat-file"])
def test_verify_dev_canonical_baseline_spawn_fault_escalates(project, monkeypatch, subcommand):
    """Baseline canonicalization is a validation boundary, not a best-effort
    ancestry probe. A machine fault while resolving or typing the claimed object
    must escalate instead of masquerading as a retryable baseline mismatch.

    Ablation: catch ``GitError`` in ``_canonical_commit_oid`` and return ``None``;
    both parameters become retryable mismatches and fail the severity assertions.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    real_run = verify.subprocess.run
    fault_args = (
        ["rev-parse", f"--disambiguate={task.baseline_commit}"]
        if subcommand == "rev-parse"
        else ["cat-file", "-t", task.baseline_commit]
    )

    def cannot_spawn_canonicalization(cmd, **kwargs):
        if cmd[3:] == fault_args:
            raise OSError(12, "Cannot allocate memory")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", cannot_spawn_canonicalization)
    out = verify.verify_dev(task, project, dev_result(sp))

    assert not out.ok and not out.retryable
    assert out.severity == "CRITICAL"
    assert f"git {subcommand} failed to spawn" in out.reason


def test_verify_dev_status_is_case_insensitive(project):
    # A hand-edited spec with a stray-cased status must still pass the gate —
    # the spec template emits lowercase, but casing must never decide it.
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "In-Review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_verify_dev_missing_spec_file_claim(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, {})
    assert not out.ok and out.retryable and "missing spec_file" in out.reason


def test_verify_dev_spec_does_not_exist(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(project.project / "ghost.md"))
    assert not out.ok and "does not exist" in out.reason


def test_verify_dev_wrong_status(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "draft", task.baseline_commit)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "expected 'in-review'" in out.reason


def test_verify_dev_wrong_workflow(project):
    # A result.json that exists and points at a real spec but reports the wrong
    # workflow means the wrong skill produced it — reject as retryable.
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "quick-dev", "spec_file": str(sp)}
    out = verify.verify_dev(task, project, rj)
    assert not out.ok and out.retryable and "auto-dev" in out.reason


def test_verify_dev_review_disabled_expects_done(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)
    assert out.ok
    # the in-review handoff status is now rejected
    write_spec(sp, "in-review", task.baseline_commit)
    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)
    assert not out.ok and "expected 'done'" in out.reason


# ------------------------------------------- awaiting-operator pair (#335)


def _park(project, *, sprint="awaiting-operator", actions=("publish the TXT record",)):
    """A dev session that finished its agent-doable work and parked the rest."""
    write_sprint(project, {"1-1-a": sprint})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "awaiting-operator", task.baseline_commit, operator_actions=list(actions))
    (project.project / "src.txt").write_text("changed\n")
    return task, sp


def test_verify_dev_accepts_the_park_pair(project):
    """The third accepted pair, selected by the OBSERVED spec status: the skill
    decides whether it parked, and the gate then holds it to the matching board
    state. `review_enabled` is irrelevant here — a park short-circuits both the
    in-review handoff and the straight-to-done finish."""
    task, sp = _park(project)

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False, operator_park=True)

    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_park_needs_the_matching_board(project):
    """Pair, not either half: a parked spec over a board the orchestrator never
    advanced is a sync that did not land, and committing on the spec's word alone
    would leave `bmad-loop confirm` advancing a board that never reached the
    token."""
    task, sp = _park(project, sprint="in-progress")

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False, operator_park=True)

    assert not out.ok and out.retryable
    assert "expected 'awaiting-operator'" in out.reason


@pytest.mark.parametrize(
    "actions, why",
    [
        ([], "an empty list declares nothing"),
        (["", "   "], "blanks are not actions"),
        ("buy the domain", "a bare string is the wrong container"),
    ],
)
def test_verify_dev_park_without_usable_actions_retries_fixable(project, actions, why):
    """A park is DEFINED by owing at least one action, so a spec at the status
    with nothing readable under `operator_actions:` is refused — confirming it
    later would be a human acknowledging a blank.

    `fixable=True` is the load-bearing half: the tree is real work and the defect
    is one frontmatter block, so the reason goes to a repair session as feedback
    instead of throwing the attempt away. This is the ablation detector for the
    non-empty gate — delete it and the malformed park verifies green."""
    write_sprint(project, {"1-1-a": "awaiting-operator"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "awaiting-operator", task.baseline_commit, operator_actions=actions)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False, operator_park=True)

    assert not out.ok and out.retryable, why
    assert out.fixable is True
    assert "operator_actions" in out.reason and "YAML list of strings" in out.reason


def test_verify_dev_park_unknown_when_the_policy_is_off(project):
    """`[operator] enabled = false` does not make the token mean something else —
    it makes it mean nothing. The ordinary status gate then rejects it, and the
    session is retried with that mismatch as feedback rather than committing."""
    task, sp = _park(project)

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)

    assert not out.ok and out.retryable
    assert "'awaiting-operator'" in out.reason and "expected 'done'" in out.reason


def _residue_free(project, *, status, sprint, baseline: str | _Omit | None = None):
    """A dev attempt whose ONLY residue is the spec and the sprint board — the two
    paths proof-of-work already excludes.

    Deliberately does NOT write `src.txt` the way `_park` does: that one line is
    what gives every other park row a non-empty proof diff, which is why none of
    them can see #676.

    What the park row and its control share is the SET OF PATHS touched — exactly
    the spec and the board, and nothing else — not the bytes in them, which differ
    by the two status tokens `status`/`sprint` select. The set is what
    proof-of-work keys on, so it is the property that makes the control a control:
    both trees are residue-free by the gate's own measure, and only the terminal
    differs.

    `operator_actions:` is written on the park terminal ONLY, because that is the
    only status the product ever emits it on: `devcontract.synthesize_result`
    folds the field from `status == AWAITING_OPERATOR`, and its comment gives the
    reason — carrying it on another terminal would let a story register
    obligations the verify gates never held it to. The park call sites pass
    `verify.AWAITING_OPERATOR` rather than the bare literal so they move with the
    branch above on a rename: were the two to drift, this helper would quietly stop
    writing the field and every park row would fail on "declares no usable
    operator_actions" instead of on the thing it tests.

    `baseline` has three meanings, and the third is not a special case of the
    second. ``None`` (the default) claims the task's own recorded baseline — the
    matching pair every ordinary row wants. A STRING overrides what the spec
    claims, for the row that probes the baseline-match gate. ``OMIT`` writes no
    `baseline_revision` key at all, which is the only way to reach the
    proof-of-work probe with a baseline git cannot resolve: with a claim present
    the baseline-match gate refuses first and the probe is never asked, so the
    git-refusal rows would pass for the wrong reason. That third meaning rides on
    ``OMIT`` being truthy in the `baseline or task.baseline_commit` expression
    below — deliberate, but load-bearing, so do not "simplify" that expression to
    an ``is None`` test without giving ``OMIT`` its own branch."""
    write_sprint(project, {"1-1-a": sprint})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(
        sp,
        status,
        baseline or task.baseline_commit,
        operator_actions=(
            ["publish the TXT record"] if status == verify.AWAITING_OPERATOR else None
        ),
    )
    return task, sp


@pytest.mark.parametrize("review_enabled", [False, True])
def test_verify_dev_park_with_no_code_residue_passes(project, review_enabled):
    """A park may legitimately have produced no code — the story's remaining work
    is a human's, and the session's whole output can be the spec's own park
    declaration plus the board sync. Both are excluded from proof-of-work, so the
    gate used to read a correct park as "no changes", retry it, and roll the park
    commit back (#676). The parked leg now skips proof-of-work outright.

    Parametrized over `review_enabled` because every other park row in this file
    passes `False`, and the skip is now the only thing standing between a park and
    this gate. A park short-circuits both terminals — the pair demanded is
    (awaiting-operator, awaiting-operator) either way — so the flag must not reach
    the outcome, and the `True` leg is what would catch a future edit that let it.

    `park_eligible=True` is the engine-side half of the selector the skip now
    needs: the orchestrator's answer, recorded at dispatch, that this phase could
    newly ELECT a park rather than inherit one (DW-1). Without it this row fails
    on proof-of-work — which is exactly what
    `test_verify_dev_ineligible_park_with_no_residue_owes_proof_of_work` asserts.
    `park_zero_diff` is the accepted skip's record: the tree really was residue-free,
    and the outcome says so instead of the skip passing silently (DW-6)."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=review_enabled,
        operator_park=True,
        park_eligible=True,
    )

    assert out.ok
    assert task.spec_file == str(sp)
    assert out.park_proof_skipped is True and out.park_zero_diff is True


@pytest.mark.parametrize("park_eligible", [False, True])
@pytest.mark.parametrize("operator_park", [False, True])
@pytest.mark.parametrize(
    "status, sprint, review_enabled",
    [("in-review", "review", True), ("done", "done", False)],
)
def test_verify_dev_residue_free_non_park_still_fails_proof_of_work(
    project, status, sprint, review_enabled, operator_park, park_eligible
):
    """The control for the row above, and the reason that row proves anything: the
    SAME residue-free tree at an ordinary terminal must still be refused. Without
    this, the park row would pass for any tree that merely happened not to be
    empty, and the skip could have widened past `parked` unnoticed.

    Both non-park terminals are covered, not just `in-review`: the skip is spelled
    `None if parked else engine_written`, and a widening that reached the
    review-disabled `done` leg instead would redden nothing if this row only ever
    ran the handoff terminal.

    `operator_park` is parametrized because it is the ONE input that separates the
    two halves of `parked = operator_park and status_of(fm) == AWAITING_OPERATOR`,
    and the `True` leg is the only thing in this file pinning the skip's SCOPE. Its
    absence was a real hole: rewriting the skip as `None if operator_park` — the
    policy flag alone, ignoring the observed status — left every row in this file
    green, and reddened only incidental `write_src=False` rows over in
    `test_engine.py` that are about harvest, not about park. A run with parking
    enabled but a session that finished ordinarily must still owe a diff.

    `park_eligible` is parametrized for the identical reason, one selector later:
    the skip is now `parked and park_eligible`, so the engine-side half is the
    other input that could widen it past the park. Rewriting it as
    `None if park_eligible` — the dispatch-time expectation alone, ignoring the
    observed status — is green everywhere without this dimension, and it would let
    every ordinary session on a story that had never parked skip proof-of-work
    entirely. Neither half selects the skip on its own.

    Ablation: delete the `if extra_exclude is not None and task.baseline_commit:`
    proof-of-work block in `_verify_shared_gates` and all four rows fail on
    `assert not out.ok` — the residue-free tree then verifies clean at every
    terminal, which is the #676 behavior generalized past the park."""
    task, sp = _residue_free(project, status=status, sprint=sprint)

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=review_enabled,
        operator_park=operator_park,
        park_eligible=park_eligible,
    )

    assert not out.ok and out.retryable
    assert out.reason == "no changes in worktree since baseline commit"
    # neither half of the record: no gate was waived, so there is nothing observed
    assert out.park_proof_skipped is False and out.park_zero_diff is None


def test_verify_dev_ineligible_park_with_no_residue_owes_proof_of_work(project):
    """DW-1, and the reason the row above needs its new argument: the skip used to
    be selected entirely by state a fresh session can INHERIT — the policy flag
    plus the spec's own status. A spec an earlier attempt left at
    `awaiting-operator` still reads `awaiting-operator` to a session that did
    nothing at all, so a re-drive over it selected #676's relaxation and verified
    green on someone else's park declaration.

    `park_eligible=False` is the orchestrator saying "the bound spec was ALREADY
    parked when I dispatched this". The park is not refused for being inherited —
    it is merely held to proof-of-work like every other terminal, and this tree has
    none to show. Note the reason: the ordinary proof-of-work message, not a
    park-specific refusal, because the eligibility flag gates the SKIP and nothing
    else.

    This row and `test_verify_dev_park_with_no_code_residue_passes` differ in
    exactly one argument over byte-identical state, which is what makes either one
    evidence. Ablation: rewrite the selector as `skip_proof = parked` (drop the
    `and park_eligible`) and this fails on `assert not out.ok` while its twin stays
    green — the pre-DW-1 behavior exactly."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=False,
    )

    assert not out.ok and out.retryable
    assert out.reason == "no changes in worktree since baseline commit"
    # no gate was waived here, so neither field carries anything — and the pair is
    # asserted in both directions, because `park_zero_diff is None` alone is also
    # what a WAIVED gate whose probe faulted looks like
    assert out.park_proof_skipped is False and out.park_zero_diff is None


def test_verify_dev_ineligible_park_with_a_real_diff_still_passes(project):
    """The bound on DW-1: ineligibility gates the proof-of-work SKIP, never the
    park itself. An inherited park that carried real work satisfies proof-of-work
    on its own and passes — status pair, actions list, workflow tag, baseline match
    and sprint pair all still select on the OBSERVED status exactly as before.

    This is the row that would catch the over-correction: making `park_eligible`
    select the park's status pair as well (rather than only the skip) turns a
    legitimate repair-then-park into a status mismatch, and refuses work that was
    actually done. `park_zero_diff` stays None because no skip fired — a passing
    park is not automatically a recorded one."""
    task, sp = _park(project)

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=False,
    )

    assert out.ok
    assert task.spec_file == str(sp)
    # a PASSING park that owed and produced its diff: no waiver, nothing observed
    assert out.park_proof_skipped is False and out.park_zero_diff is None


def test_verify_dev_elected_park_with_code_residue_records_a_non_zero_diff(project):
    """DW-6's discriminator, and the half a zero-diff-only record could never
    prove: the skip fires for EVERY elected park, including one that wrote real
    code, and the record has to tell the two apart. `_park` writes `src.txt`, so
    the waived gate would have passed — and the observation says so.

    Ablation: make the observation arm return a constant `True` and this row fails
    while `test_verify_dev_park_with_no_code_residue_passes` stays green, because
    that one cannot distinguish a real probe from a hardcoded answer."""
    task, sp = _park(project)

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
    )

    assert out.ok
    assert out.park_proof_skipped is True and out.park_zero_diff is False


def test_verify_dev_park_zero_diff_observation_degrades_to_unknown(project, monkeypatch):
    """The observation must never change an outcome. The proof-of-work probe can
    raise `GitError` (timeout, spawn or decode fault), and on the gated legs that
    escalates the attempt — here the same fault has to leave the park accepted and
    the answer honestly unknown.

    Load-bearing because a fault swallowed at the wrong level would be recorded as
    a confident answer about a question git never answered, which is worse than no
    record at all.

    This is the row that separates the two reasons `park_zero_diff` can be `None`:
    the probe could not answer, versus no gate was ever waived. They are different
    facts and they live on different fields — `park_proof_skipped` stays True here.
    Collapsing them would make this park look like an ordinary leg and drop its
    journal record, which is the exact silence DW-6 exists to end.

    The patch target is `_changes_since`, the tri-state body BOTH proof arms share
    (`has_changes_since` is its fail-open collapse and no longer what the gate
    calls) — patching the wrapper would leave the probe untouched and this row
    would pass for no reason at all.

    Ablation: drop the `except GitError` in the observation arm and this fails with
    the GitError propagating out of `verify_dev`, turning a bookkeeping probe into
    a failed attempt."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )

    def boom(*_a, **_kw):
        raise verify.GitError("git diff exploded")

    monkeypatch.setattr(verify, "_changes_since", boom)

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
    )

    assert out.ok
    assert task.spec_file == str(sp)
    assert out.park_zero_diff is None
    # the gate WAS waived — unknown is not the same fact as "no waiver"
    assert out.park_proof_skipped is True


def test_verify_dev_park_zero_diff_is_unknown_when_git_refuses_the_probe(project):
    """The THIRD cause of `zero_diff: null`, and the one that used to be recorded
    as a confident answer: git refusing the diff outright.

    `git diff --quiet` reports rc 0 for "no differences" and rc 1 for
    "differences"; anything else — rc 128 for a baseline it cannot resolve — is the
    command failing rather than answering. The gate that this observation stands in
    for reads every non-zero rc as "there are changes", which is right for a gate
    (uncertainty must keep the stricter path) and wrong for a record: it filed "the
    waived gate would have found changes" about a question git never answered, and
    nothing downstream ever re-asks.

    No monkeypatch: the refusal is REAL, produced the way production produces one —
    a recorded baseline that does not resolve in this repository, with the spec
    carrying no `baseline_revision` claim so the baseline-match gate has nothing to
    compare and the attempt reaches the probe. rc 128 is asserted directly first, so
    a future git that answered differently would fail here rather than silently
    turning this row into a duplicate of its `GitError` sibling.

    Two ablations, both measured, and they fail this row to DIFFERENT values —
    which is the point, because only one of them is the defect that shipped. Point
    `proof_of_work_probe` back at `has_changes_since` (the pre-fix spelling, where
    the wrapper folds the refusal into its fail-open) and this fails with
    `park_zero_diff is False`: the record asserting the gate would have found
    changes. Collapse the observation arm's mapping to `not observed` instead and
    it fails with `True`, because `not None` is True — a different wrong answer
    from a different place, and the reason the arm maps the unknown explicitly
    rather than negating it."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR, baseline=OMIT
    )
    task.baseline_commit = "0" * 40
    rc, _ = verify._git(project.repo_root, "diff", "--quiet", task.baseline_commit, "--", ".")
    assert rc == 128, "the premise: git REFUSES this baseline rather than answering"

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
    )

    assert out.ok
    # the waiver is recorded; only the observation is honestly unknown
    assert out.park_proof_skipped is True
    assert out.park_zero_diff is None


def test_verify_dev_proof_of_work_gate_still_fails_open_on_a_refused_probe(project):
    """The control for the row above, and the reason it can be trusted to have
    changed only the record: the GATE's reading of the identical refusal is
    unchanged. An ordinary terminal on a residue-free tree whose baseline git will
    not resolve still PASSES proof-of-work, because uncertainty at a gate keeps the
    stricter path — the same answer the arm gave when it called `has_changes_since`
    and let that function collapse the refusal.

    Without this row the tri-state could have been introduced by narrowing the gate
    too (refusing on `None`), which would turn every unresolvable baseline into a
    burned attempt, and only this residue-free tree — where the refusal is the ONLY
    thing standing between the attempt and a "no changes" retry — can tell the two
    spellings apart."""
    task, sp = _residue_free(project, status="done", sprint="done", baseline=OMIT)
    task.baseline_commit = "0" * 40

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)

    assert out.ok
    assert out.park_proof_skipped is False and out.park_zero_diff is None


def test_verify_dev_park_zero_diff_is_unknown_without_a_recorded_baseline(project):
    """The SECOND documented cause of `zero_diff: null`, and the one a reader is
    likeliest to mistake for the first: not a git fault, but an attempt carrying
    no `baseline_commit` at all. The shared gate runs neither proof arm without
    one, so there is nothing to measure from and the observation never happens —
    yet the waiver did, and the record still has to say so.

    All three causes are named in `verify_dev`'s docstring, in `VerifyOutcome`'s
    field comment and in `docs/FEATURES.md`; the sibling rows cover the `GitError`
    and the git refusal, and this one covers the missing baseline, so no claim
    rests on prose.

    Ablation, measured rather than assumed, and the measurement changed when the
    refusal fix landed: dropping `and task.baseline_commit` from the observation
    arm's guard ALONE now leaves this row green, because the probe then runs
    against an empty baseline, git REFUSES it, and `_changes_since` reports that
    refusal as the same `None` the guard was suppressing. That convergence is the
    point of the refusal fix, not a hole — the guard is now a spared git spawn
    rather than the only thing standing between this attempt and a confident
    answer. What does redden the row is the pre-fix PAIR: drop the guard and
    collapse the arm's unknown mapping to `not observed`, and an attempt with
    nothing to measure is filed `zero_diff: true`."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )
    # the spec keeps its real `baseline_revision` claim; what is missing is the
    # ORCHESTRATOR's recorded baseline, which is what both proof arms measure from
    task.baseline_commit = ""

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
    )

    assert out.ok
    # the gate was waived and is recorded as such; only the observation is unknown
    assert out.park_proof_skipped is True
    assert out.park_zero_diff is None


def test_verify_dev_park_zero_diff_excludes_the_orchestrators_own_writes(project):
    """The observation must exclude exactly what the waived gate would have, and
    this is the misattribution most likely to be audited: the orchestrator appends
    a harvested deferral to the ledger DURING the attempt, so a park whose session
    wrote nothing still leaves that file changed. Counted, the record would read
    `zero_diff: false` — "the waived gate would have found changes" — about a diff
    the orchestrator itself produced, and an audit of which parks got in without
    proving work would quietly exonerate exactly the wrong ones.

    `engine_written` is what `Engine._harvest_gate_exclude` supplies for this, and
    on the waived leg it is routed to `observe_skipped_proof` rather than
    `extra_exclude` — same tuple, no gate.

    Ablation: drop `+ mode_exclude` from `proof_of_work_probe`'s exclusion (or
    stop passing `observe_skipped_proof` at the call site) and this fails with
    `park_zero_diff is False`, while every other park row stays green — they have
    no orchestrator residue to misattribute."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )
    (project.repo_root / "ledger.md").write_text("- DW-9 harvested by the orchestrator\n")

    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
        engine_written=("ledger.md",),
    )

    assert out.ok
    assert out.park_proof_skipped is True
    # the ONLY residue is the orchestrator's own write, so the park really is
    # zero-diff and the record has to say so
    assert out.park_zero_diff is True


def test_verify_dev_park_still_faces_the_workflow_tag_gate(project):
    """Proof-of-work is the ONLY gate the park leg skips. The tree is residue-free,
    so nothing else can account for a refusal here — with the skip in place, a
    foreign `workflow` is the only thing left to refuse on, and it must.

    This is the gate the park-leg docstrings promise "still runs" and that no row
    asserted: every other park row hands in a well-formed `dev_result(sp)`, so
    deleting the whole shared-gate block on the parked leg left the suite green.

    Ablation: delete the `if workflow != DEV_WORKFLOW:` refusal at the top of
    `_verify_shared_gates` and this fails on `assert not out.ok`. With
    proof-of-work already skipped for the park, that gate is the only thing left
    to refuse the foreign tag, so the outcome goes straight to ok."""
    task, sp = _residue_free(
        project, status=verify.AWAITING_OPERATOR, sprint=verify.AWAITING_OPERATOR
    )
    rj = {"workflow": "quick-dev", "spec_file": str(sp)}

    # park_eligible=True so the skip really is in place: without it proof-of-work
    # would also refuse this tree and the row would pass for a compound reason.
    out = verify.verify_dev(
        task, project, rj, review_enabled=False, operator_park=True, park_eligible=True
    )

    assert not out.ok and out.retryable
    assert "auto-dev" in out.reason


def test_verify_dev_park_still_faces_the_baseline_match_gate(project):
    """The sibling of the row above, and the more load-bearing half: proof-of-work
    was the park's last diff-based tie to the attempt, so baseline-match is now
    what remains binding a park to the attempt the orchestrator actually launched.
    A residue-free park claiming a baseline the orchestrator never recorded must
    still be refused.

    Ablation: delete the `if task.baseline_commit and claimed_baseline not in
    ("", "NO_VCS"):` block in `_verify_shared_gates` and this fails on
    `assert not out.ok`. Ablate that whole block, NOT the inner `canonical_claimed
    is None` early return on its own: the arms below it consume
    `canonical_claimed`, so the narrower cut sends `None` on into
    `commit_reachable_above_baseline` and reddens this row on a subprocess
    `TypeError` instead — an ablation grading itself rather than the gate."""
    task, sp = _residue_free(
        project,
        status=verify.AWAITING_OPERATOR,
        sprint=verify.AWAITING_OPERATOR,
        baseline="deadbeef" * 5,
    )

    # Same reason as the workflow-tag row above: with park_eligible left False the
    # tree would also owe proof-of-work, and baseline-match would stop being the
    # only thing that could refuse here.
    out = verify.verify_dev(
        task,
        project,
        dev_result(sp),
        review_enabled=False,
        operator_park=True,
        park_eligible=True,
    )

    assert not out.ok and out.retryable
    assert "does not match" in out.reason


def test_verify_review_accepts_the_park_pair(project):
    """The gate the park path runs before committing: at THIS gate parked work
    clears the same deterministic checks `done` work clears. Scoped on purpose —
    a `done` story additionally clears proof-of-work at the dev gate, which a park
    no longer does (#676)."""
    task, sp = _park(project)
    task.spec_file = str(sp)

    out = verify.verify_review(task, project, Policy(), operator_park=True)

    assert out.ok


def test_verify_review_park_refused_without_the_engine_flag(project):
    """The flag is the SAME one `verify_dev` takes, not a second reading of
    `policy.operator.enabled`, so the two gates cannot disagree about whether
    this run parks — `Engine._operator_park_enabled` is an override seam, and a
    mode that opts out (stories, sweep) while still reaching this gate would
    otherwise find it accepting a park the engine itself refuses to take.

    Conservative default, like `sprint_reached_done`: a caller that says nothing
    gets the pre-#335 behavior."""
    task, sp = _park(project)
    task.spec_file = str(sp)

    out = verify.verify_review(task, project, Policy())  # operator_park defaults False

    assert not out.ok and out.retryable
    assert "'awaiting-operator'" in out.reason and "expected 'done'" in out.reason


def test_verify_review_park_requires_actions(project):
    task, sp = _park(project, actions=[])
    task.spec_file = str(sp)

    out = verify.verify_review(task, project, Policy(), operator_park=True)

    assert not out.ok and out.retryable and out.fixable is True
    assert "operator_actions" in out.reason


def test_verify_review_park_board_short_is_a_plain_retry_not_a_contradiction(project):
    """The sign-off-regression arm (#334) stays scoped to the `done` pair. A board
    short of `awaiting-operator` is a stage never reached — escalating it would
    halt a run over a sync that simply has not landed."""
    task, sp = _park(project, sprint="in-progress")
    task.spec_file = str(sp)

    out = verify.verify_review(
        task, project, Policy(), sprint_reached_done=True, operator_park=True
    )

    assert not out.ok and out.retryable and out.contradiction is False
    assert out.reason == "sprint-status for 1-1-a is 'in-progress', expected 'awaiting-operator'"


def test_verify_dev_review_disabled_rejects_review_sprint(project):
    # Skip-review finalizes the sprint to 'done'; a run that left it at 'review'
    # must not slip through the sprint-status gate.
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)
    assert not out.ok and "sprint-status" in out.reason and "expected 'done'" in out.reason


def test_verify_dev_lying_baseline(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "deadbeef" * 5)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_short_hash_baseline(project):
    # Sessions sometimes write `git rev-parse --short HEAD`; an abbreviation
    # of the recorded baseline is the same commit, not a lie.
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit[:7])
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_canonical_commit_oid_accepts_full_uppercase_and_unique_abbreviation(project):
    oid = verify.rev_parse_head(project.project)

    assert verify._canonical_commit_oid(project.project, oid) == oid
    assert verify._canonical_commit_oid(project.project, oid.upper()) == oid
    assert verify._canonical_commit_oid(project.project, oid[:7]) == oid


@pytest.mark.parametrize("object_kind", ["blob", "tree", "tag"])
def test_canonical_commit_oid_refuses_non_commit_objects(project, object_kind):
    """Ablation: returning the sole disambiguated object without the
    ``cat-file -t`` direct-commit check makes every parameter fail."""
    if object_kind == "blob":
        oid = git(project.project, "hash-object", "-w", "src.txt")
    elif object_kind == "tree":
        oid = git(project.project, "rev-parse", "HEAD^{tree}")
    else:
        git(project.project, "tag", "-a", "object-tag", "-m", "tag object", "HEAD")
        oid = git(project.project, "rev-parse", "object-tag^{tag}")

    assert verify._canonical_commit_oid(project.project, oid) is None


def test_canonical_commit_oid_refuses_an_ambiguous_prefix(project):
    """Ablation: accepting the first disambiguated object instead of requiring
    ``len(objects) == 1`` makes this assertion fail."""
    prefix, oids = _write_ambiguous_commit_prefix(project.project)

    assert set(git(project.project, "rev-parse", f"--disambiguate={prefix}").splitlines()) == set(
        oids
    )
    assert verify._canonical_commit_oid(project.project, prefix) is None


def test_canonical_commit_oid_accepts_sha256_when_git_supports_it(project):
    repo = project.project / "sha256-repo"
    proc = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(repo)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"Git does not support SHA-256 repositories: {proc.stderr.strip()}")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "file.txt").write_text("sha256 fixture\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "sha256 commit")
    oid = verify.rev_parse_head(repo)

    assert len(oid) == 64
    assert verify._canonical_commit_oid(repo, oid[:12].upper()) == oid


def test_verify_dev_accepts_a_reachable_descendant_baseline(project):
    """A session that commits inside the unit before step-03 stamps
    `baseline_revision` makes that stamp a DESCENDANT of the baseline the
    orchestrator recorded — the shape no branch of the gate could accept, so a
    finished attempt was refused at the door. The immutable descendant is
    accepted when this checkout's HEAD reaches it and later work is proven."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "spec for 1-1-a")
    spec_commit = verify.rev_parse_head(project.project)

    # step-03 stamps "current HEAD before making any changes" — the spec commit
    write_spec(sp, "in-review", spec_commit)
    (project.project / "src.txt").write_text("changed\n")

    assert verify.is_ancestor(project.project, task.baseline_commit, spec_commit)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_verify_dev_uses_canonical_oid_after_same_named_ref_moves(project, monkeypatch):
    """Once the claim is disambiguated, later ref movement cannot retarget any
    ancestry or proof-of-work operation back onto the mutable ref name."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "descendant baseline")
    descendant = verify.rev_parse_head(project.project)
    claimed_ref = descendant[:12]
    git(project.project, "branch", claimed_ref, descendant)

    write_spec(sp, "in-review", claimed_ref)
    (project.project / "src.txt").write_text("work after the descendant\n")

    real_is_ancestor = verify.is_ancestor
    calls: list[tuple[str, str]] = []

    def move_ref_then_check(repo, ancestor, candidate):
        if not calls:
            git(repo, "branch", "-f", claimed_ref, task.baseline_commit)
        calls.append((ancestor, candidate))
        return real_is_ancestor(repo, ancestor, candidate)

    monkeypatch.setattr(verify, "is_ancestor", move_ref_then_check)
    out = verify.verify_dev(task, project, dev_result(sp))

    assert out.ok
    assert calls == [
        (task.baseline_commit, descendant),
        (descendant, descendant),
    ]
    assert git(project.project, "rev-parse", f"refs/heads/{claimed_ref}") == task.baseline_commit


def test_verify_dev_still_refuses_a_stale_ancestor_baseline(project):
    """The stale-premise case the gate exists for is untouched: an OLDER
    baseline outside a deferred-work bundle still fails.

    Ablation: force ``commit_reachable_above_baseline`` to return ``True`` and
    the attempt passes, failing the refusal assertion.
    """
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "prior.txt").write_text("work that landed first\n")
    git(project.project, "add", "prior.txt")
    git(project.project, "commit", "-q", "-m", "prior work")
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)  # baseline = the newer HEAD

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", ancestor)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_still_refuses_a_diverged_commit_baseline(project):
    """Ablation: force ``commit_reachable_above_baseline`` to return ``True``
    and this unrelated root passes, failing the refusal assertion."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    tree = git(project.project, "rev-parse", "HEAD^{tree}")
    diverged = git(project.project, "commit-tree", tree, "-m", "unrelated root")

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", diverged)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_refuses_a_descendant_off_this_worktree(project):
    """The half that keeps the widening narrow: a commit above the baseline that
    this checkout's HEAD does not reach is outside the accepted history.

    Ablation: force ``commit_reachable_above_baseline`` to return ``True``
    (bypassing its final HEAD-ancestry call) and the refusal assertion fails.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    git(project.project, "checkout", "-q", "-b", "elsewhere")
    (project.project / "foreign.txt").write_text("another branch\n")
    git(project.project, "add", "foreign.txt")
    git(project.project, "commit", "-q", "-m", "foreign work")
    foreign = verify.rev_parse_head(project.project)
    git(project.project, "checkout", "-q", "-")

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", foreign)
    (project.project / "src.txt").write_text("changed\n")

    assert verify.is_ancestor(project.project, task.baseline_commit, foreign)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_descendant_baseline_needs_work_above_it(project):
    """Accepting a newer claim re-anchors proof-of-work onto it. Under the default
    `isolation = "none"` the unit works in the shared checkout, so a commit can
    arrive from outside the session and still be reachable from HEAD; measuring
    from the recorded baseline would let that commit satisfy proof-of-work on its
    own, passing an attempt that implemented nothing.

    Ablation: leave ``proof_baseline`` at the recorded baseline after accepting
    the descendant and this no-work attempt passes, failing the assertion.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "someone-elses-work.txt").write_text("not this session\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "a commit from outside the session")
    foreign_but_reachable = verify.rev_parse_head(project.project)

    # the spec claims it, and the session then implements NOTHING
    write_spec(sp, "in-review", foreign_but_reachable)

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok, "a session that implemented nothing passed the gate"


def test_verify_dev_descendant_baseline_refuses_untracked_only_residue(project):
    """The launch-time untracked snapshot says when residue appeared relative
    to the recorded baseline, not relative to a later claimed descendant. It
    therefore cannot prove that an untracked file was made after that claim.

    Ablation: keep ``include_untracked_proof`` true for the accepted descendant
    and the residue makes the attempt pass, failing the refusal assertion.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    task.baseline_untracked = []

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    residue = project.project / "intervening-untracked.txt"
    residue.write_text("created before the descendant claim\n")
    git(
        project.project,
        "add",
        project.sprint_status.relative_to(project.project).as_posix(),
        sp.relative_to(project.project).as_posix(),
    )
    git(project.project, "commit", "-q", "-m", "descendant baseline")
    descendant = verify.rev_parse_head(project.project)

    write_spec(sp, "in-review", descendant)
    out = verify.verify_dev(task, project, dev_result(sp))

    assert residue.is_file()
    assert not out.ok and "no changes" in out.reason


@pytest.mark.parametrize("proof_kind", ["tracked", "staged", "committed"])
def test_verify_dev_descendant_baseline_accepts_tracked_proof(project, proof_kind):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "descendant baseline")
    descendant = verify.rev_parse_head(project.project)
    write_spec(sp, "in-review", descendant)

    if proof_kind == "tracked":
        (project.project / "src.txt").write_text("tracked modification\n")
    elif proof_kind == "staged":
        (project.project / "staged-proof.txt").write_text("staged new file\n")
        git(project.project, "add", "staged-proof.txt")
    else:
        (project.project / "src.txt").write_text("committed work\n")
        git(project.project, "add", "src.txt")
        git(project.project, "commit", "-q", "-m", "work after descendant")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_verify_dev_equal_baseline_still_accepts_new_untracked_file(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    task.baseline_untracked = []
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "untracked-proof.txt").write_text("new after exact baseline\n")

    assert verify.verify_dev(task, project, dev_result(sp)).ok


def test_verify_dev_refuses_a_symbolic_baseline_revision(project):
    """A spec naming a Git revision expression instead of an immutable object id
    (`baseline_revision: HEAD`) resolves at verification time, so every ancestry
    question about it answers yes. It must not buy the relaxation.

    Ablation: restore the old lexical-hex check plus raw-ref ancestry calls and
    ``HEAD`` makes the attempt pass, failing the refusal assertion.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "spec for 1-1-a")

    write_spec(sp, "in-review", "HEAD")
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


@pytest.mark.parametrize("ref_kind", ["branch", "tag"])
def test_verify_dev_refuses_an_all_hex_ref_baseline(project, ref_kind):
    """Hex spelling alone does not make a claim immutable. Git resolves an
    all-hex branch or tag when no object has that prefix, so the baseline gate
    must disambiguate the object id independently of the ref namespace.

    Ablation: restore the old ``_OBJECT_ID``-only gate and raw-ref ancestry
    calls; both parameters pass verification and fail this refusal assertion.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)

    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "reachable descendant")

    claimed_ref = "deadbeef"
    if ref_kind == "branch":
        git(project.project, "branch", claimed_ref, "HEAD")
    else:
        git(project.project, "tag", claimed_ref, "HEAD")
    assert git(project.project, "rev-parse", claimed_ref) == verify.rev_parse_head(project.project)
    assert git(project.project, "rev-parse", f"--disambiguate={claimed_ref}") == ""

    write_spec(sp, "in-review", claimed_ref)
    (project.project / "src.txt").write_text("changed after the claim\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_no_changes(project):
    # Spec claims NO_VCS baseline (skips the mismatch check); everything is
    # committed, so there are no changes since the orchestrator's baseline.
    write_sprint(project, {"1-1-a": "review"})
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "artifacts")
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason


def test_verify_dev_sprint_not_synced(project):
    write_sprint(project, {"1-1-a": "in-progress"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "sprint-status" in out.reason


def test_verify_review_happy_and_commands(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    # The host-shell verbs, not POSIX `true`/`false`: cmd has neither, so those
    # read as a broken environment on Windows rather than pass/fail (#302).
    ok_policy = Policy(verify=VerifyPolicy(commands=(_OK,)))
    assert verify.verify_review(task, project, ok_policy).ok

    fail_policy = Policy(verify=VerifyPolicy(commands=(_OK, _FAIL)))
    out = verify.verify_review(task, project, fail_policy)
    assert not out.ok and "verify command failed" in out.reason


def test_verify_review_spec_not_done(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_review_sprint_not_done(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "sprint-status" in out.reason


# ------------------------------------- review revokes the sprint sign-off (#334)


def _signoff_regression_task(project, sprint_status="in-progress"):
    """A story the orchestrator advanced to `done` whose board a review then
    wrote back, leaving the spec frontmatter at `done`."""
    write_sprint(project, {"1-1-a": sprint_status})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    return task


def test_verify_review_signoff_regression_escalates(project):
    """The default knob turns a review's board regression into a CRITICAL pause
    that names both sides, instead of a retry no later cycle can satisfy."""
    task = _signoff_regression_task(project)
    out = verify.verify_review(task, project, Policy(), sprint_reached_done=True)

    assert not out.ok and not out.retryable
    assert out.severity == "CRITICAL" and out.contradiction is True
    assert not out.fixable  # no repair session can reconcile a disagreement
    # both sides are named, plus the two ways out and the opt-out knob
    assert "'in-progress'" in out.reason and "'done'" in out.reason
    assert "1-1-a" in out.reason
    assert 'review.on_status_contradiction = "retry"' in out.reason


def test_verify_review_signoff_regression_retry_mode_is_legacy(project):
    """`retry` restores the pre-#334 routing verbatim: an ordinary retryable
    sprint-status failure that burns review cycles and ends in a defer."""
    task = _signoff_regression_task(project)
    pol = Policy(review=ReviewPolicy(on_status_contradiction="retry"))
    out = verify.verify_review(task, project, pol, sprint_reached_done=True)

    assert not out.ok and out.retryable and out.contradiction is False
    assert out.reason == "sprint-status for 1-1-a is 'in-progress', expected 'done'"


def test_verify_review_signoff_regression_needs_the_launch_flag(project):
    """Without the caller's guarantee that the board actually reached `done`, an
    earlier status is a stage never reached — the gate must not escalate. Sweep
    and stories callers never pass the flag."""
    task = _signoff_regression_task(project)
    out = verify.verify_review(task, project, Policy())  # sprint_reached_done defaults False

    assert not out.ok and out.retryable and out.contradiction is False
    assert "sprint-status" in out.reason


@pytest.mark.parametrize(
    "board, why",
    [
        ({"1-2-b": "done"}, "story absent from the board -> story_status returns None"),
        ({"1-1-a": "needs-signoff"}, "token outside STATUS_ORDER (hand-edited board)"),
    ],
)
def test_verify_review_unrecognized_sprint_status_retries(project, board, why):
    """Conservative on every uncertainty: a status the lifecycle does not know
    cannot be *ordered* against `done`, so it is never called a regression — a
    wrong escalation halts an otherwise healthy run."""
    write_sprint(project, board)
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    out = verify.verify_review(task, project, Policy(), sprint_reached_done=True)

    assert not out.ok and out.retryable and out.contradiction is False, why


def test_verify_review_awaiting_operator_board_is_a_regression(project):
    """`awaiting-operator` joined STATUS_ORDER below `done`, so a review writing
    it onto a board the orchestrator had already signed off is now ORDERED, and
    therefore a deliberate regression — not the unknown token it used to be.

    This is the one behavior the vocabulary PR changes, and it is the intended
    one: until the park path exists, nothing legitimately writes this token, so a
    review that writes it has revoked a sign-off the same way `in-progress` does.
    When the park path lands, `operator.on_review_demotion` is what re-routes it.
    """
    task = _signoff_regression_task(project, sprint_status="awaiting-operator")

    out = verify.verify_review(task, project, Policy(), sprint_reached_done=True)

    assert not out.ok and not out.retryable
    assert out.contradiction is True and out.severity == "CRITICAL"
    assert "'awaiting-operator'" in out.reason


# --------------------------------------------------- verify command exit codes (issue #126)


@pytest.mark.parametrize("rc", [126, 127])
def test_verify_commands_env_fault_rc_escalates(tmp_path, rc):
    """The shell's environment faults (126 = not executable, 127 = not found)
    escalate instead of retrying: a repair session cannot fix the environment,
    so they must never charge the story's attempt budget.

    Runs on both platforms: 126/127 are sh's *convention*, but the rc arm is
    checked ahead of the per-shell branch, so `cmd /c "exit 126"` classifies
    the same way — nothing about the win32 arm narrows it (issue #302)."""
    policy = Policy(verify=VerifyPolicy(commands=(f"exit {rc}",)))
    out = verify.verify_commands_outcome(policy, tmp_path)
    assert not out.ok
    assert out.severity == "CRITICAL"
    assert out.env_fault
    assert not out.retryable and not out.fixable
    assert f"rc={rc}" in out.reason


def test_verify_commands_missing_binary_is_env_fault(tmp_path):
    """Realism check on the host shell: a command no host has is an env fault on
    both — sh exits 127, cmd exits 1 and says "is not recognized" (issue #302)."""
    policy = Policy(verify=VerifyPolicy(commands=(MISSING_TOOL_CMD,)))
    out = verify.verify_commands_outcome(policy, tmp_path)
    assert out.env_fault and not out.fixable
    assert MISSING_TOOL_CMD in out.reason


# --------------------------------------------------- cmd env-fault signals (issue #302)
#
# cmd has no 126/127 convention, so the win32 arm classifies on three other
# signals. These drive verify.env_fault_reason directly with synthetic results:
# actually executing an unrunnable path on Windows would hand it to the file
# association (see #292) — an interactive picker mid-suite, not a test.

WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="cmd-specific classification")
NOT_RECOGNIZED = (
    "'ruff' is not recognized as an internal or external command,\noperable program or batch file."
)


@WIN32_ONLY
@pytest.mark.parametrize(
    "rc, tail",
    [
        (9009, ""),  # a .cmd/.bat wrapper propagating %ERRORLEVEL%
        (1, NOT_RECOGNIZED),  # cmd's own message, alone in the tail
        # The shape production actually builds: run_verify_commands concatenates
        # stdout + stderr, and cmd's message is on stderr — so a compound command
        # whose earlier half printed to stdout still puts the message last.
        (1, f"1 failed, 3 passed\n{NOT_RECOGNIZED}"),
    ],
)
def test_win32_shell_signals_are_env_faults(tmp_path, rc, tail):
    result = verify.CommandResult("ruff check", rc, tail)
    assert verify.env_fault_reason(result, tmp_path) is not None


@WIN32_ONLY
def test_win32_unresolvable_token_is_env_fault_without_the_message(tmp_path):
    """The localized-Windows path: cmd's message is not English, so the leading
    token is probed against PATH instead."""
    result = verify.CommandResult(f"{MISSING_TOOL_CMD} -q", 1, "nije prepoznat kao naredba")
    assert "not found on PATH" in (verify.env_fault_reason(result, tmp_path) or "")


@WIN32_ONLY
def test_win32_unexecutable_file_is_env_fault_despite_rc_zero(tmp_path):
    """cmd hands a file whose extension is not in PATHEXT to the file association
    and returns 0 without running it — a silent false pass, not a green verify."""
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    result = verify.CommandResult(f'"{script}"', 0, "")
    assert "not executable by cmd" in (verify.env_fault_reason(result, tmp_path) or "")


@WIN32_ONLY
def test_win32_file_named_after_a_builtin_is_not_a_false_fault(tmp_path):
    """cmd answers an unqualified internal name internally, before searching any
    directory, so a file that merely shares the name is never what runs. Without
    the builtin guard on the file probe this escalates at rc 0 — verify could
    then never pass in a repo that happens to carry such a file."""
    (tmp_path / "echo").write_text("not a program\n", encoding="utf-8")
    passing = verify.CommandResult("echo verifying", 0, "")
    assert verify.env_fault_reason(passing, tmp_path) is None
    failing = verify.CommandResult("echo verifying", 1, "checks failed")
    assert verify.env_fault_reason(failing, tmp_path) is None
    # The exemption is the bare name only: a qualified reference to that same file
    # is something cmd does try to run, so it must still classify.
    qualified = verify.CommandResult(str(tmp_path / "echo"), 0, "")
    assert "not executable by cmd" in (verify.env_fault_reason(qualified, tmp_path) or "")


@WIN32_ONLY
@pytest.mark.parametrize(
    "command, rc, tail",
    [
        ('if exist "x" (exit 1)', 1, ""),  # a cmd builtin: which cannot resolve it
        ("pytest -q", 1, "1 failed, 3 passed"),  # the tool exists; the tests failed
        ('pytest "unbalanced', 1, ""),  # untokenizable: probe skipped, not raised
        # Pins the window, not a production shape: cmd's message rides on stderr,
        # which is appended last, so it cannot be pushed out of the closing lines
        # by a real run — only by a command echoing the phrase itself.
        ("ruff check", 1, f"{NOT_RECOGNIZED}\n1 file reformatted"),
        ("pytest -q", 1, "Access is denied\n1 test failed"),
        ("2>nul pytest -q", 1, ""),
        ("(pytest)", 1, "1 failed"),  # grouping parens on both ends, no argument
        ("(pytest -q) && (ruff check)", 1, "1 failed"),
        ("pytest|findstr FAIL", 1, "1 failed"),  # shlex leaves the pipe attached
        ("pytest -q&&ruff check", 1, "1 failed"),
        ("& pytest -q", 1, "1 failed"),  # a leading operator is not a tool name
    ],
)
def test_win32_ordinary_failures_are_not_env_faults(tmp_path, command, rc, tail, monkeypatch):
    # Pin PATH resolution to a host that has exactly pytest and ruff: otherwise
    # every row here silently asserts this interpreter's PATH layout (a
    # `python -m pytest` from an env whose Scripts\ dir is not on PATH reports
    # "pytest not found on PATH" and fails for the wrong reason). Resolving only
    # those two keeps each row load-bearing — `(pytest)` still proves the parens
    # are stripped, `if exist` still proves the builtin allowlist.
    monkeypatch.setattr(
        verify.shutil,
        "which",
        lambda name: rf"C:\tools\{name}.exe" if name in {"pytest", "ruff"} else None,
    )
    assert verify.env_fault_reason(verify.CommandResult(command, rc, tail), tmp_path) is None


@WIN32_ONLY
def test_win32_passing_command_is_never_an_env_fault(tmp_path):
    """rc 0 classifies only via the file probe — a token that is not a file on
    disk must never escalate a green run."""
    assert verify.env_fault_reason(verify.CommandResult("pytest -q", 0, ""), tmp_path) is None


@WIN32_ONLY
@pytest.mark.parametrize(
    "command",
    [
        "%TOOLCHAIN%\\pytest.exe -q",  # cmd expands it; the literal never resolves
        "!DELAYED!\\pytest.exe -q",
    ],
)
def test_win32_expandable_token_skips_the_probe(tmp_path, command):
    """A token cmd expands before running is unprobeable: resolving the literal
    would report "not found" for a tool that is right there."""
    assert verify.env_fault_reason(verify.CommandResult(command, 1, ""), tmp_path) is None


@WIN32_ONLY
def test_win32_failing_script_in_the_run_dir_is_not_a_false_fault(tmp_path, monkeypatch):
    """cmd searches the run's own directory before PATH, so a runnable check that
    simply *fails* there keeps the fixable-retry classification — `which` would
    miss it, since it searches this process's directory instead."""
    (tmp_path / "check.cmd").write_text("@exit /b 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    failing = verify.CommandResult("check.cmd", 1, "checks failed")
    assert verify.env_fault_reason(failing, tmp_path) is None
    assert (
        verify.env_fault_reason(verify.CommandResult("check", 1, "checks failed"), tmp_path) is None
    )


@WIN32_ONLY
def test_win32_relative_script_under_a_foreign_cwd_is_not_a_false_fault(tmp_path, monkeypatch):
    """The run's cwd is not the process cwd under worktree isolation, so
    executability is decided by PATHEXT rather than by resolving the relative
    path against whatever directory this process happens to sit in."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check.cmd").write_text("@exit /b 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    passing = verify.CommandResult("scripts\\check.cmd", 0, "")
    assert verify.env_fault_reason(passing, tmp_path) is None


@WIN32_ONLY
def test_win32_timeout_is_never_an_env_fault(tmp_path):
    """A timed-out command (rc sentinel -1) ran and hung, so it was found and
    runnable — it keeps the charged fixable-retry classification even when its
    tool happens to be unresolvable from this process."""
    timed_out = verify.CommandResult(f"{MISSING_TOOL_CMD} -q", -1, "timed out")
    assert verify.env_fault_reason(timed_out, tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="win32 has its own classifier")
def test_posix_ignores_the_cmd_signals(tmp_path):
    """The cmd signals stay win32-only: on sh, 9009 is an ordinary exit code."""
    assert verify.env_fault_reason(verify.CommandResult("ruff", 9009, ""), tmp_path) is None
    assert (
        verify.env_fault_reason(verify.CommandResult("ruff", 1, NOT_RECOGNIZED), tmp_path) is None
    )


def test_env_fault_takes_precedence_over_earlier_ordinary_failure(tmp_path):
    """A mixed run must not classify by the first failure it sees: an env
    fault later in the command list still escalates — a repair session
    dispatched for the earlier rc=1 would run in the broken environment."""
    policy = Policy(verify=VerifyPolicy(commands=(_FAIL, "exit 127")))
    out = verify.verify_commands_outcome(policy, tmp_path)
    assert not out.ok
    assert out.env_fault
    assert not out.retryable and not out.fixable
    assert "rc=127" in out.reason


def test_verify_commands_rc1_stays_fixable_retry(tmp_path):
    """Ordinary failures (tests failing) keep the fixable-retry classification."""
    policy = Policy(verify=VerifyPolicy(commands=(_FAIL,)))
    out = verify.verify_commands_outcome(policy, tmp_path)
    assert not out.ok and out.fixable and out.retryable and not out.env_fault


# ---- unusable `cwd`: the spawn fault has no return code (DW-2) ----------------

_unsearchable_dir_skips = (
    pytest.mark.skipif(os.name == "nt", reason="Windows chmod only toggles the read-only flag"),
    pytest.mark.skipif(
        os.geteuid() == 0 if hasattr(os, "geteuid") else False,
        reason="root searches a 000 directory",
    ),
)


def _unusable_cwd(request, tmp_path, shape: str) -> Path:
    """One of the ``cwd`` shapes ``subprocess.run`` refuses before the target
    program starts — the reachable OSError subclasses of the spawn leg.

    Each is a real filesystem state, never a monkeypatched raise: what is being
    pinned is that the OS's own refusal is caught, and a synthetic
    ``FileNotFoundError`` would pass just as well against a handler that only
    named that one class (the narrowing this change exists to avoid).

    The 000 directory's mode is restored by a finalizer — ``tmp_path``'s own
    cleanup cannot remove a directory it may not search, and the leftover turns
    into an rm_rf warning on every later session sharing the tmp root."""
    if shape == "missing":
        return tmp_path / "nowhere"  # FileNotFoundError
    if shape == "file":
        target = tmp_path / "a-file"
        target.write_text("x\n", encoding="utf-8")
        return target  # NotADirectoryError
    if shape == "under-file":
        target = tmp_path / "a-file-2"
        target.write_text("x\n", encoding="utf-8")
        return target / "beneath"  # NotADirectoryError, one level down
    unsearchable = tmp_path / "locked"
    unsearchable.mkdir()
    request.addfinalizer(lambda: unsearchable.chmod(0o700))
    unsearchable.chmod(0o000)
    return unsearchable  # PermissionError


@pytest.mark.parametrize(
    "shape",
    [
        "missing",
        "file",
        "under-file",
        pytest.param("unsearchable", marks=_unsearchable_dir_skips),
    ],
)
def test_unusable_cwd_becomes_a_result_instead_of_an_exception(request, tmp_path, shape):
    """A `cwd` no command can run in yields a RESULT, not a raised OSError.

    Before this, `run_verify_commands`' only handler was `except
    subprocess.TimeoutExpired`, so all three shapes escaped every guard in the
    engine's verification path and ended the run as a crash (`crash.txt` +
    `state.crashed`) — over a fact that is a textbook environment problem.

    All three shapes are driven, not just the first: `except FileNotFoundError`
    would be a perfectly plausible fix and would leave two of them uncaught, so a
    single-shape row could not tell the narrow handler from the right one.

    Ablation: remove the `except OSError` arm and every parametrization fails with
    the raw OSError, not with a wrong-message assertion."""
    cwd = _unusable_cwd(request, tmp_path, shape)
    policy = Policy(verify=VerifyPolicy(commands=(_OK,)))

    (result,) = verify.run_verify_commands(policy, cwd)

    assert result.command == _OK
    assert result.spawn_error is not None
    assert str(cwd) in result.spawn_error  # the failing cwd, which is the finding
    assert result.returncode == verify.SPAWN_FAULT_RC
    assert result.returncode != -1  # NOT the timeout sentinel: no child ran at all
    assert result.output_tail  # names the exception, for the human reading it


def test_unusable_cwd_yields_one_result_per_command(tmp_path):
    """The documented "one CommandResult apiece" holds on the spawn leg too: the
    loop appends and CONTINUES rather than aborting on the first refusal.

    A caller zipping results against `policy.verify.commands` — or merely counting
    them — must not silently lose the tail of the list, and the engine journals one
    record per result, so a short list is a short audit trail.

    Ablation: `break` (or `raise`) instead of `continue` in the new arm and the
    length assertion fails at 1."""
    commands = ("first-check", "second-check", "third-check")
    policy = Policy(verify=VerifyPolicy(commands=commands))

    results = verify.run_verify_commands(policy, tmp_path / "nowhere")

    assert [r.command for r in results] == list(commands)
    assert all(r.spawn_error is not None for r in results)
    outcome = verify.verify_command_results_outcome(results, tmp_path / "nowhere")
    assert "first-check" in outcome.reason
    assert "second-check" not in outcome.reason and "third-check" not in outcome.reason


def test_a_spawn_fault_unrelated_to_the_cwd_translates_too(tmp_path, monkeypatch):
    """The handler is `except OSError`, not three named cwd classes — and the
    record must not describe every one of them as a directory problem.

    A missing `/bin/sh`, EMFILE from a descriptor-exhausted host, ENOMEM from a
    fork that could not allocate: all reach the same arm, none is a fact about
    the working directory. The message therefore states what was OBSERVED (the
    child was not started) and names the cwd as context only, leaving the wrapped
    exception to say why.

    Injected, because a real ENOMEM cannot be provoked from a test without
    breaking the host running it. What that costs is honest: this row grades the
    message and the classification, while the sibling rows above drive the OS's
    own refusals for real.

    Ablation: restore a message hardcoding the cwd as the cause (`could not run
    in {cwd}: ...`) and the "does not blame the directory" assertion fails."""
    real_run = subprocess.run

    def out_of_memory(*args, **kwargs):
        if kwargs.get("shell"):
            raise OSError(12, "Cannot allocate memory")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", out_of_memory)
    policy = Policy(verify=VerifyPolicy(commands=(_OK,)))

    (result,) = verify.run_verify_commands(policy, tmp_path)

    assert result.spawn_error is not None
    assert "Cannot allocate memory" in result.spawn_error  # the real cause survives
    # the cwd is context, not a verdict: it appears, but not as the diagnosis
    assert str(tmp_path) in result.spawn_error
    assert "could not run in" not in result.spawn_error

    out = verify.verify_command_results_outcome([result], tmp_path)
    assert not out.ok and out.env_fault and not out.retryable


def test_unusable_cwd_escalates_as_an_environment_fault(tmp_path):
    """Classified, the spawn fault escalates and PAUSES rather than retrying.

    Same channel as rc 126/127 and for the same reason: an unusable `cwd` is
    deterministic for a given tree, identical for every story, and unfixable by a
    repair session — which is what `env_fault=True` means. A `retryable` outcome
    would burn the attempt budget re-running the same refusal.

    The explanatory clause is asserted from BOTH directions. The rc-based leg's
    fixed "command not found / not executable" is a claim about the command, and
    on this leg no command was ever looked for — so it must be gone, not merely
    joined by better text."""
    cwd = tmp_path / "nowhere"
    policy = Policy(verify=VerifyPolicy(commands=(_OK,)))

    out = verify.verify_commands_outcome(policy, cwd)

    assert not out.ok and out.env_fault
    assert not out.retryable and not out.fixable
    assert "verify environment fault" in out.reason
    assert str(cwd) in out.reason
    assert "could not be started" in out.reason
    assert "command not found / not executable" not in out.reason
    # The exception already rides `spawn_error`, which is interpolated as the
    # environment-fault reason. Repeating `output_tail` would print it twice.
    assert out.reason.count("FileNotFoundError") == 1


def test_rc_env_fault_keeps_its_own_explanatory_clause(tmp_path):
    """The complement, so the branch is pinned from both sides: rc 127 still says
    "command not found / not executable" — that leg IS a claim about the command,
    and branching must not have quietly rewritten it for everyone."""
    policy = Policy(verify=VerifyPolicy(commands=("exit 127",)))

    out = verify.verify_commands_outcome(policy, tmp_path)

    assert not out.ok and out.env_fault
    assert "command not found / not executable" in out.reason
    assert "could not be started" not in out.reason


def test_spawn_fault_rc_cannot_collide_with_a_real_return_code():
    """The sentinel sits outside every value a child that RAN can report.

    On POSIX `subprocess` reports `-N` for a child killed by signal N, so the
    small negatives are all real return codes: `-2` is SIGINT, `-9` SIGKILL. A
    sentinel in that range would make "the verify command was killed" and "the
    verify command never started" the same observation to anything keying on the
    rc — and the journal record invites exactly that, since it ships the rc to
    out-of-process readers.

    Asserted against `signal.Signals` rather than a hardcoded ceiling, so a
    platform with higher real-time signals grades this honestly instead of
    against this test's idea of the range.

    Ablation: set `SPAWN_FAULT_RC = -2` — the value this shipped with first — and
    the collision assertion fails naming SIGINT."""
    import signal as signal_mod

    assert verify.SPAWN_FAULT_RC < 0  # the win32 early-out and the failure arm
    assert verify.SPAWN_FAULT_RC != -1  # not the timeout leg's sentinel
    collisions = [s for s in signal_mod.Signals if -s.value == verify.SPAWN_FAULT_RC]
    assert not collisions, f"SPAWN_FAULT_RC is a signal death: {collisions}"
    # nor an ordinary exit status, which is what the positive range holds
    assert verify.SPAWN_FAULT_RC not in verify.ENV_FAULT_RCS


def test_spawn_fault_is_answered_before_any_rc_or_win32_probe(tmp_path):
    """`env_fault_reason` reads `spawn_error` FIRST, ahead of the rc arms and the
    win32 token probe.

    Not a style preference. The probe resolves a command's leading token as `cwd /
    token` to tell "tool missing" from "command failed" — and on this leg `cwd` is
    exactly what could not be used, so it has nothing true to say about a directory
    the child never entered. Driven through `env_fault_reason` directly so the row
    holds on POSIX, where the probe is not reached at all.

    The result carries `SPAWN_FAULT_RC`, which is in neither `ENV_FAULT_RCS` nor
    `{0}` — so if the ordering ever regressed, the rc arms could not answer for it
    and the reason would come back None on POSIX."""
    result = verify.CommandResult(
        "pytest -q", verify.SPAWN_FAULT_RC, "NotADirectoryError: ...", spawn_error="cwd is a file"
    )

    assert verify.env_fault_reason(result, tmp_path) == "cwd is a file"
    # and a result from a child that really ran is untouched by the new arm
    assert verify.env_fault_reason(verify.CommandResult("pytest -q", 1, "F"), tmp_path) is None


def test_timeout_stays_an_ordinary_fixable_retry_with_no_spawn_error(tmp_path, monkeypatch):
    """The two "no exit status" shapes must not collapse into one.

    A timed-out command RAN — it was found, it was executable, it hung — so it
    stays a fixable retry a repair session can act on. Only a child that never
    started is an environment fault. Sharing a sentinel between them (or letting
    the new arm swallow the timeout) would pause runs over slow test suites.

    Ablation: set `SPAWN_FAULT_RC = -1` and the sentinel assertion below stops
    discriminating; set `spawn_error` on the timeout leg and the classification
    flips to `env_fault`."""
    monkeypatch.setattr(verify, "COMMAND_TIMEOUT_S", 0.5)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    policy = Policy(verify=VerifyPolicy(commands=(f'"{sys.executable}" "{sleeper}"',)))

    (result,) = verify.run_verify_commands(policy, tmp_path)

    assert result.spawn_error is None
    assert result.returncode == -1 and result.output_tail == "timed out"

    out = verify.verify_command_results_outcome([result], tmp_path)
    assert not out.ok and out.retryable and out.fixable and not out.env_fault


def test_verify_commands_bound_a_stream_instead_of_holding_it_whole(tmp_path, monkeypatch):
    """A chatty command's stream is cut to `MAX_STREAM_MEMORY_BYTES` as it is
    collected, and what it emitted is recorded rather than lost.

    `capture_output=True` always materialises one command's whole output; what
    this bounds is RETENTION — before it, every command's full streams stayed in
    the results list while all the later commands ran, so peak memory scaled with
    the number of configured verify commands instead of with the largest one.
    Plugins are meant to see streams essentially whole, so the ceiling sits far
    above `stream_capture_kb` and is a backstop, not a knob; the test lowers it
    rather than emitting 32 MiB to prove the same branch.

    Ablation: hand the raw `proc.stdout` to CommandResult again and `stdout` comes
    back 5000 bytes with `stdout_full_bytes` None. Verified.
    """
    script = tmp_path / "chatty.py"
    script.write_text("import sys\nsys.stdout.write('o' * 5000)\n", encoding="utf-8")
    policy = Policy(verify=VerifyPolicy(commands=(f'"{sys.executable}" "{script}"',)))
    monkeypatch.setattr(verify, "MAX_STREAM_MEMORY_BYTES", 64)

    (result,) = verify.run_verify_commands(policy, tmp_path)

    assert result.stdout == "o" * 64  # the TAIL survives, as at every other bound
    assert result.stdout_full_bytes == 5000  # and the emitted size is not lost
    assert result.stderr == "" and result.stderr_full_bytes == 0
    assert result.output_tail == "o" * 64  # merged view built from the bounded pair


def test_verify_commands_preserve_separate_stdout_and_stderr(tmp_path):
    """The merged bounded tail remains compatible while the raw streams stay
    distinguishable for engine-owned journal pointers and plugin observation."""
    script = tmp_path / "streams.py"
    script.write_text(
        "import sys\nprint('stdout proof')\nprint('stderr proof', file=sys.stderr)\n",
        encoding="utf-8",
    )
    policy = Policy(verify=VerifyPolicy(commands=(f'"{sys.executable}" "{script}"',)))

    (result,) = verify.run_verify_commands(policy, tmp_path)

    assert result.returncode == 0
    assert result.stdout == "stdout proof\n"
    assert result.stderr == "stderr proof\n"
    assert result.output_tail == "stdout proof\nstderr proof\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, ""), ("already decoded", "already decoded")],
    ids=["none", "str-passthrough"],
)
def test_timeout_stream_shapes_that_carry_no_decode(value, expected):
    """The two non-bytes shapes of a timeout payload, asserted directly because
    neither reaches a codec — there is no stdlib decoding for a real child to
    exercise, so driving one would add cost without adding evidence.

    ``None`` is POSIX's answer when nothing had been buffered on that stream
    (``_check_timeout`` passes None, not ``b""``, for an empty chunk list); the
    CommandResult must still carry a str. The str arm is Windows, where
    ``subprocess.run`` re-collects through ``communicate()`` after ``kill()`` and
    the text wrapper has already decoded — dropping it would lose that
    platform's output entirely. The bytes shape, the only one that picks a
    codec, is covered by the real-child test below."""
    assert verify._timeout_stream(value) == expected


# ---- a timed-out child's output reads like a completed one's (#378, follow-on)
#
# Both divergences pinned here are invisible on the hosts the suite usually runs
# on: `bytes.decode()`'s hardcoded UTF-8 equals the locale codec wherever the
# locale is UTF-8, and LF-only output has no carriage returns to collapse. Every
# CI leg is UTF-8 (Linux by locale, Windows by PYTHONUTF8=1), so gating on the
# host codec — the `needs_strict_codec` shape used above — would skip precisely
# where the guard is wanted, the inverse of what that marker buys its own tests.
# The work is therefore driven inside a child interpreter pinned to an ASCII
# locale. Everything below that boundary is genuine: one real grandchild script
# emits the bytes on both paths, and CPython's own timeout leg is what hands the
# hung one over. Monkeypatching `subprocess.run` instead would supply str objects
# directly and never run the stdlib's decoding at all (see the #378 block below).
_TIMEOUT_RAW = b"caf\xc3\xa9\r\nsecond\rthird\n"
"""Undecodable as ASCII and carrying both newline forms, so a single payload
exercises the codec choice, the CRLF pair and the lone CR at once."""


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the bytes arm is unreachable on Windows (run() re-collects via "
    "communicate() after kill(), which returns str, already decoded and "
    "newline-translated), and LC_ALL is not how Windows resolves the codec",
)
def test_verify_commands_timeout_output_matches_the_completed_path(tmp_path):
    """The same bytes must read back the same whether the command finished or
    timed out — the tail a human or a repair session sees cannot depend on that.

    POSIX raises TimeoutExpired from ``_check_timeout`` with the raw chunks
    joined, *before* the text-mode conversion at the end of ``_communicate``, so
    the timeout arm has to redo that conversion itself. It did neither half:
    ``bytes.decode()`` hardcoded UTF-8 against run_verify_commands' own rule
    (#378) that host-tool output stays on the locale codec, and nothing
    collapsed the newlines that ``Popen._translate_newlines`` collapses.

    The completed result is the reference rather than a literal, so the assertion
    is against what the stdlib actually does, not against this test's idea of it.

    Ablation, two axes, and each reddens a different assertion: drop the
    ``locale.getpreferredencoding(False)`` argument and the codec half fails;
    drop the ``replace`` chain and the newline half does. Note that ``LC_ALL=C``
    alone does NOT redden the codec axis — the C locale auto-enables UTF-8 mode
    (PEP 540), putting both spellings back on one codec — so ``PYTHONUTF8=0``
    below is load-bearing, and the anti-vacuity checks fail loudly if it is
    ever lost rather than letting the test pass empty."""
    emit = tmp_path / "emit_timeout.py"
    emit.write_text(
        "import sys, time\n"
        f"sys.stdout.buffer.write({_TIMEOUT_RAW!r})\n"
        "sys.stdout.buffer.flush()\n"
        "if sys.argv[1] == 'hang':\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )
    driver = tmp_path / "drive_timeout.py"
    # One script, two modes: the completed and timed-out runs are byte-identical
    # by construction, so comparing their results compares only the two paths.
    # Interpreter is sys.executable, never a bare `python` — the suite runs under
    # uv, where no `python` need be on PATH. json defaults to ensure_ascii, so the
    # report survives the ASCII stdout it is printed on.
    driver.write_text(
        "import json, locale, sys\n"
        "from pathlib import Path\n"
        "from bmad_loop import verify\n"
        "from bmad_loop.policy import Policy, VerifyPolicy\n"
        "verify.COMMAND_TIMEOUT_S = 1.0\n"
        "def run(mode):\n"
        '    cmd = \'"%s" "%s" %s\' % (sys.executable, sys.argv[1], mode)\n'
        "    (r,) = verify.run_verify_commands(\n"
        "        Policy(verify=VerifyPolicy(commands=(cmd,))), Path(sys.argv[2])\n"
        "    )\n"
        "    return r\n"
        "done, hung = run('exit'), run('hang')\n"
        "json.dump({'encoding': locale.getpreferredencoding(False),\n"
        "           'completed_rc': done.returncode, 'completed_stdout': done.stdout,\n"
        "           'timeout_rc': hung.returncode, 'timeout_tail': hung.output_tail,\n"
        "           'timeout_stdout': hung.stdout, 'timeout_stderr': hung.stderr},\n"
        "          sys.stdout)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "LANG", "LC_CTYPE")}
    env["LC_ALL"] = "C"
    env["PYTHONUTF8"] = "0"  # without this the C locale would resolve to UTF-8 (PEP 540)

    proc = subprocess.run(
        [sys.executable, str(driver), str(emit), str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    decoded = _TIMEOUT_RAW.decode(observed["encoding"], errors="replace")
    # One anti-vacuity check per divergence: if the child ever stops resolving a
    # non-UTF-8 codec, or the payload loses its carriage returns, the equality
    # below would hold with the bug in place. These fail instead of going quiet.
    assert decoded != _TIMEOUT_RAW.decode("utf-8", errors="replace")
    assert "\r" in decoded

    assert observed["completed_rc"] == 0
    assert observed["completed_stdout"] == decoded.replace("\r\n", "\n").replace("\r", "\n")

    assert observed["timeout_rc"] == -1
    assert observed["timeout_tail"] == "timed out"
    assert observed["timeout_stdout"] == observed["completed_stdout"]
    # The child wrote nothing to stderr, so POSIX handed _timeout_stream None.
    assert observed["timeout_stderr"] == ""


def test_verify_commands_timeout_stays_charged(tmp_path, monkeypatch):
    """A timeout is plausibly the story's own tests hanging — it keeps the
    fixable-retry classification, not the env-fault escalate."""
    monkeypatch.setattr(
        verify,
        "run_verify_commands",
        lambda policy, cwd: [verify.CommandResult("pytest", -1, "timed out")],
    )
    out = verify.verify_commands_outcome(Policy(), tmp_path)
    assert not out.ok and out.fixable and not out.env_fault


# ---- undecodable verify output (issue #378)
#
# Both tests drive a REAL child process. Monkeypatching subprocess.run hands the
# code str objects directly and never runs the stdlib's decoding at all, so such
# a test passes identically with the bug restored — see the #374 regression
# test's docstring (tests/test_install.py) for the same distinction.
#
# They are also codec-conditional, which is the subtler way they could go quiet;
# `needs_strict_codec` (top of file) is what keeps them honest about it.


def _undecodable_cmd(tmp_path: Path, rc: int) -> str:
    """A shell string running a child that emits byte 0xff on stdout and exits
    with `rc`. Interpreter is `sys.executable`, never a bare `python`: the tests
    run under uv, where no `python` need be on PATH. The double quotes are
    honored by both sh and cmd."""
    script = tmp_path / "emit378.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'before \\xff after\\n')\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


@needs_strict_codec
def test_verify_commands_undecodable_output_keeps_every_result(tmp_path):
    """A child emitting a byte invalid in the run's encoding is decoded with
    replacement, not strictly: the strict decode raised UnicodeDecodeError
    *before* any result was appended, so the offender's exit code AND every
    later command's result were lost, and the dev phase crashed instead of
    classifying a failure.

    U+FFFD is asserted, not merely "did not raise": under `needs_strict_codec`
    the codec provably cannot decode the byte, so `errors="replace"` must have
    produced exactly that character. Its *count* stays unasserted — that is what
    varies by codec — and the text on both sides pins the rest of the tail.

    Ablation: remove `errors="replace"` from run_verify_commands and this fails
    with UnicodeDecodeError."""
    policy = Policy(verify=VerifyPolicy(commands=(_undecodable_cmd(tmp_path, 5), _OK)))

    results = verify.run_verify_commands(policy, tmp_path)

    assert [r.returncode for r in results] == [5, 0]
    assert "before" in results[0].output_tail and "after" in results[0].output_tail
    assert "\ufffd" in results[0].output_tail  # the replacement, not a survivor


@needs_strict_codec
def test_verify_commands_undecodable_failure_stays_fixable_retry(tmp_path):
    """Through the outcome path: an undecodable tail still classifies as an
    ordinary fixable failure and still reaches the repair session's feedback.

    What the env-fault probe contributes is platform-split, so this does not
    claim more than it runs: on POSIX `verify.env_fault_reason` returns at its
    `sys.platform != "win32"` guard without ever reading the tail, and only the
    Windows leg takes `_win32_env_fault_reason` over the replaced text. The
    classification asserted here holds on both."""
    policy = Policy(verify=VerifyPolicy(commands=(_undecodable_cmd(tmp_path, 1),)))

    out = verify.verify_commands_outcome(policy, tmp_path)

    assert not out.ok and out.fixable and out.retryable and not out.env_fault
    assert "rc=1" in out.reason
    assert "after" in out.reason


def make_bundle_task(paths, dw_ids=("DW-1", "DW-2")):
    task = StoryTask(story_key="dw-test-bundle", epic=0, dw_ids=list(dw_ids))
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def bundle_ledger(paths, statuses: dict[str, str]) -> None:
    parts = []
    for dw_id, status in statuses.items():
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test\nlocation: n/a\n"
            f"reason: test\nstatus: {status}\n"
        )
    paths.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")


def test_verify_dev_bundle_happy_skips_sprint(project):
    # no sprint-status entry for the bundle key — must still pass
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-2", "DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_bundle_dw_ids_mismatch(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "dw_ids" in out.reason


@pytest.mark.parametrize(
    "claim",
    [{}, {"dw_ids": []}, {"dw_ids": None}],
    ids=["missing-key", "empty-list", "null"],
)
def test_verify_dev_bundle_absent_dw_ids_passes(project, claim):
    # Generic bmad-dev-auto path: the primitive authors no dw ids, so result.json
    # omits them (missing key), carries an empty list, or an explicit null. The
    # orchestrator owns the bundle→dw-id binding, so verify must pass on an
    # unclaimed bundle without crashing. The empty list is the literal payload
    # that defered in production ("dw_ids []").
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), **claim}
    out = verify.verify_dev_bundle(task, project, rj)
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_bundle_ancestor_baseline_passes(project):
    """#161: a bundle that adopts a pre-existing story spec (bmad-dev-auto
    routes a "follow-up review of story X" bundle into that story's done spec)
    carries the story's original baseline — an *ancestor* of the unit baseline,
    never equal to it. The bundle gate accepts the ancestor instead of failing
    the whole attempt after the work is done."""
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))  # baseline = new HEAD
    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", ancestor)  # adopted spec: the older baseline
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_bundle_ancestor_baseline_retains_untracked_proof(project):
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("story work\n")
    git(project.project, "add", "story-work.txt")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))
    task.baseline_untracked = []
    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", ancestor)
    (project.project / "untracked-bundle-proof.txt").write_text("new bundle work\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    assert verify.verify_dev_bundle(task, project, rj).ok


def test_verify_dev_bundle_symbolic_baseline_is_refused(project):
    """The ancestor relaxation must not be buyable with a claim that pins
    nothing. `baseline_revision: HEAD` is resolved when the gate runs, not when
    the session stamped it, so it reads as an ancestor of the unit baseline
    whenever the checkout's HEAD still equals the recorded baseline — exactly
    the stale premise this gate exists to refuse. Canonicalization has to happen
    before the leg, not after.

    Ablation: bypass ``_canonical_commit_oid`` in the ``allow_ancestor_baseline``
    leg only, letting it consume the raw ``claimed_baseline``, and the attempt
    passes, failing the refusal assertion. The dev-path twin
    (``test_verify_dev_refuses_a_symbolic_baseline_revision``) stays green under
    it: it never sets the flag, and ``_OBJECT_ID`` rejects ``HEAD`` ahead of the
    newer-baseline leg either way.
    """
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))  # baseline = new HEAD
    assert verify.is_ancestor(project.project, "HEAD", task.baseline_commit)

    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", "HEAD")
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    assert verify._canonical_commit_oid(project.project, "HEAD") is None
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "does not match" in out.reason


@pytest.mark.parametrize("ref_kind", ["branch", "tag"])
def test_verify_dev_bundle_all_hex_ref_baseline_is_refused(project, ref_kind):
    """Hex spelling does not make a claim immutable on the bundle path either.
    An all-hex branch or tag pointing at a genuine ancestor would satisfy the
    relaxation through the ref namespace, so the gate must disambiguate the
    object id independently of refs before the leg is reached.

    Ablation: bypass ``_canonical_commit_oid`` in the ``allow_ancestor_baseline``
    leg only and both parameters pass verification. The dev-path twin
    (``test_verify_dev_refuses_an_all_hex_ref_baseline``) stays green under that
    mutation, because it never sets the flag. It does redden under a *full*
    pre-#645 restore, which strips canonicalization from the newer-baseline leg
    as well — a strictly larger mutation than the one named here.
    """
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))

    claimed_ref = "abcdef0"
    if ref_kind == "branch":
        git(project.project, "branch", claimed_ref, ancestor)
    else:
        git(project.project, "tag", claimed_ref, ancestor)
    assert git(project.project, "rev-parse", claimed_ref) == ancestor
    assert git(project.project, "rev-parse", f"--disambiguate={claimed_ref}") == ""
    assert verify.is_ancestor(project.project, claimed_ref, task.baseline_commit)

    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", claimed_ref)
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    assert verify._canonical_commit_oid(project.project, claimed_ref) is None
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_bundle_single_char_ref_baseline_is_refused(project):
    """A ref short enough to be a prefix of its own target defeats any test that
    reasons about spelling: a branch named for its target's first character does
    begin with its own name. The claim is refused because it cannot be
    canonicalized at all — one character is below ``_OBJECT_ID``'s floor and
    below the four ``rev-parse --disambiguate`` requires — so the ref namespace
    is never consulted, and the self-prefix property never gets a chance to
    matter.

    Ablation: bypass ``_canonical_commit_oid`` in the ``allow_ancestor_baseline``
    leg only and the ref resolves through the namespace to a genuine ancestor,
    so the attempt passes. Relaxing ``_OBJECT_ID``'s length floor does NOT redden
    this one — a one-character claim is below every floor git itself will
    resolve.
    """
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))

    claimed_ref = ancestor[0]
    git(project.project, "branch", claimed_ref, ancestor)
    assert git(project.project, "rev-parse", claimed_ref) == ancestor
    assert ancestor.startswith(claimed_ref)  # the target DOES begin with the name
    assert verify.is_ancestor(project.project, claimed_ref, task.baseline_commit)

    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", claimed_ref)
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    assert verify._canonical_commit_oid(project.project, claimed_ref) is None
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_bundle_below_floor_abbreviation_is_refused(project):
    """Characterizes the deliberate 7-character floor on the bundle path: an
    abbreviation git itself resolves is still refused when it is shorter than
    ``_OBJECT_ID``'s floor. That 7 is the gate's own constant, set where git's
    auto abbreviation bottoms out (``core.abbrev`` defaults to ``auto``, which
    scales with repository size and clamps upward to 7 only for small repos, so
    there is no fixed default to mirror). The stamp is ``git rev-parse HEAD`` output by
    contract, so the floor costs a well-behaved session nothing, and
    accepting shorter claims would re-admit prefix collisions the gate cannot
    distinguish from drift. The refusal is the floor's doing, not an
    unresolvable string — the premise assertion below pins that.

    Ablation: relax ``_OBJECT_ID``'s length floor from 7 to 4 and the claim
    canonicalizes to the ancestor, buys the relaxation leg, and the attempt
    passes. That mutation reddens no other test in the suite, and none of the
    ref-refusal tests above redden under it.
    """
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))

    claimed = ancestor[:6]
    assert git(project.project, "rev-parse", claimed) == ancestor  # git resolves it
    assert verify.is_ancestor(project.project, claimed, task.baseline_commit)

    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", claimed)
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    assert verify._canonical_commit_oid(project.project, claimed) is None
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_bundle_foreign_baseline_still_fails(project):
    """The bundle relaxation is ancestor-only: a baseline unknown to (or
    diverged from) the unit's history still fails the gate."""
    task = make_bundle_task(project, dw_ids=("DW-1",))
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", "0" * 40)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "baseline" in out.reason


def test_verify_dev_bundle_ancestor_probe_git_failure_stays_strict(project, monkeypatch):
    """A git failure inside the ancestor probe (e.g. a timeout, which surfaces
    as GitError since #156's `_run_git` translation) must read as
    not-an-ancestor and fail the gate closed — never propagate out of
    `_verify_shared_gates` and crash the run. Baseline canonicalization is an
    earlier escalation boundary, so the injection targets ``merge-base``.

    Ablation: delete the ``except (OSError, GitError)`` guard in ``is_ancestor``;
    the timeout propagates instead of reading as false and this test errors.
    """
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("stories 1.1-1.3\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "story work")
    task = make_bundle_task(project, dw_ids=("DW-1",))
    sp = project.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "in-review", ancestor)
    (project.project / "src.txt").write_text("review fixes\n")
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}

    real_run = verify.subprocess.run

    def timing_out_merge_base(cmd, **kwargs):
        if cmd[3] == "merge-base":
            return _timing_out_run(cmd, **kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(verify.subprocess, "run", timing_out_merge_base)
    assert verify.is_ancestor(project.project, ancestor, task.baseline_commit) is False
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "baseline" in out.reason


def test_verify_dev_sprint_ancestor_baseline_still_fails(project):
    """The ancestor relaxation is bundle-only: a sprint-mode dev spec is
    authored by this session, so its baseline must match the orchestrator's
    exactly — an older ancestor means the session planned from a stale tree."""
    ancestor = verify.rev_parse_head(project.project)
    (project.project / "story-work.txt").write_text("advance\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "advance")
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)  # baseline = new HEAD
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", ancestor)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "baseline" in out.reason


def test_verify_dev_bundle_ledger_only_counts_as_real_work(project):
    """Same false-negative as verify_dev (KNOWN-BUG-ledger-only-story-false-no-
    changes.md), on the bundle path: a dw-bundle's entire authorized diff is
    the ledger reconciliation itself, with no sprint-status entry to touch."""
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
    rj = {"workflow": "auto-dev", "spec_file": str(sp)}
    out = verify.verify_dev_bundle(task, project, rj)
    assert out.ok


# ------------------------------------------------------------ verify_dev_stories


def make_stories_task(paths, story_key="1"):
    task = StoryTask(story_key=story_key, epic=0)
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def write_story(spec_folder, story_id, slug, status, baseline):
    d = spec_folder / "stories"
    d.mkdir(parents=True, exist_ok=True)
    sp = d / f"{story_id}-{slug}.md"
    write_spec(sp, status, baseline)
    return sp


def test_verify_dev_stories_happy_no_sprint_gate(project):
    # No sprint-status file exists at all — stories mode has no sprint board, so
    # the sprint-status gate that verify_dev enforces is dropped here.
    assert not project.sprint_status.is_file()
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "user-auth", "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out.ok
    assert task.spec_file == str(sp)  # set to the id-keyed resolution


def test_verify_dev_stories_composite_id(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "3-2")
    sp = write_story(spec_folder, "3-2", "user-auth", "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out.ok and task.spec_file == str(sp)


def test_verify_dev_stories_pending_retry(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    out = verify.verify_dev_stories(
        task, project, dev_result(spec_folder / "ghost.md"), spec_folder=spec_folder
    )
    assert not out.ok and out.retryable and "no story spec found" in out.reason


def test_verify_dev_stories_ambiguous_retry(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    write_story(spec_folder, "1", "one", "done", task.baseline_commit)
    write_story(spec_folder, "1", "two", "done", task.baseline_commit)
    out = verify.verify_dev_stories(
        task, project, {"workflow": "auto-dev"}, spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "ambiguous story file match" in out.reason


def test_verify_dev_stories_sentinel_retry(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    write_story(spec_folder, "1", "unresolved", "blocked", task.baseline_commit)
    out = verify.verify_dev_stories(
        task, project, {"workflow": "auto-dev"}, spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "sentinel" in out.reason


def test_verify_dev_stories_wrong_status(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "draft", task.baseline_commit)
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_dev_stories_review_enabled_expects_in_review(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=True
    )
    assert not out.ok and "expected 'in-review'" in out.reason


def test_verify_dev_stories_wrong_workflow(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "quick-dev", "spec_file": str(sp)}
    out = verify.verify_dev_stories(
        task, project, rj, spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "auto-dev" in out.reason


def test_verify_dev_stories_lying_baseline(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", "deadbeef" * 5)
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_stories_no_changes(project):
    # NO_VCS baseline skips the mismatch check; everything committed -> proof-of-
    # work fails since there are no changes vs the orchestrator baseline.
    spec_folder = project.planning_artifacts / "epic-a"
    sp = write_story(spec_folder, "1", "x", "done", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "artifacts")
    task = make_stories_task(project, "1")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "no changes" in out.reason


def test_verify_dev_stories_whitespace_story_key(project):
    # A story_key with stray whitespace must resolve identically to its trimmed id:
    # the resolver normalizes via str().strip(), and the filename-prefix check must
    # use the same normalized id (else a spurious "does not match id" retry).
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, " 1 ")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out.ok and task.spec_file == str(sp)


def test_verify_dev_stories_ledger_only_counts_as_real_work(project):
    """T3 regression: a stories-mode story whose entire authorized diff is
    ledger/spec reconciliation under implementation_artifacts (e.g. deferred-work.md)
    must pass proof-of-work, not false-negative "no changes". Guards the file-granular
    exclude port off #79 — the old whole-folder `artifact_relpaths` exclusion
    swallowed the ledger, re-introducing KNOWN-BUG-ledger-only-story-false-no-
    changes.md in stories mode (verify_dev_exclude_relpaths excludes only the
    session's own spec + sprint-status, so sibling ledger content counts)."""
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    # The ONLY real change since baseline is the ledger under implementation_artifacts;
    # the story's own spec (under the spec folder's stories/) is excluded either way.
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("### DW-1: reconciled\n\nstatus: done\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out.ok


def test_verify_dev_stories_spec_only_change_outside_artifacts_is_not_work(project):
    # spec folder OUTSIDE the artifact dirs: the story record + stories.yaml must
    # still not count as implementation work (the _stories_relpaths exclusion),
    # so a story that only wrote its spec fails proof-of-work.
    spec_folder = project.project / "docs" / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    (spec_folder / "stories.yaml").write_text("- id: '1'\n  title: t\n  description: d\n")
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "no changes" in out.reason
    # real code alongside the spec -> proof-of-work passes
    (project.project / "src.txt").write_text("real work\n")
    out2 = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out2.ok


@pytest.mark.parametrize("refused", ["project", "spec_folder"])
def test_stories_relpaths_is_empty_when_resolution_is_uncertain(project, monkeypatch, refused):
    """An uncertain observation supplies no story excludes. Ablation target: narrow
    `_stories_relpaths`' resolution guard back to `ValueError`, and either refusal
    row raises instead of returning the documented empty tuple."""
    spec_folder = project.planning_artifacts / "epic-a"
    target = project.project if refused == "project" else spec_folder
    refuse_to_resolve(monkeypatch, target)

    assert verify._stories_relpaths(project.project, spec_folder) == ()


def test_verify_dev_stories_plan_halt_expects_ready_for_dev(project):
    # plan-halt leg: the spec is at ready-for-dev (the plan), not done, and there
    # is NO code change — proof-of-work is skipped and the plan spec is recorded.
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "ready-for-dev", task.baseline_commit)
    out = verify.verify_dev_stories(
        task,
        project,
        {"workflow": "auto-dev", "plan_halt": True},
        spec_folder=spec_folder,
        review_enabled=False,
        plan_halt=True,
    )
    assert out.ok  # no code change required for a plan
    assert task.spec_file == str(sp)


def test_verify_dev_stories_plan_halt_rejects_non_plan_status(project):
    # a plan-halt leg that did not reach ready-for-dev (still draft) is a retry
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    write_story(spec_folder, "1", "x", "draft", task.baseline_commit)
    out = verify.verify_dev_stories(
        task,
        project,
        {"workflow": "auto-dev", "plan_halt": True},
        spec_folder=spec_folder,
        review_enabled=False,
        plan_halt=True,
    )
    assert not out.ok and "expected 'ready-for-dev'" in out.reason


def test_verify_dev_stories_plan_halt_requires_marker(project):
    # plan_halt=True but the result.json carries NO plan_halt marker: a
    # died-mid-flight ready-for-dev must not pass as a successful plan leg.
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    write_story(spec_folder, "1", "x", "ready-for-dev", task.baseline_commit)
    out = verify.verify_dev_stories(
        task,
        project,
        {"workflow": "auto-dev"},  # no plan_halt marker
        spec_folder=spec_folder,
        review_enabled=False,
        plan_halt=True,
    )
    assert not out.ok and "no plan_halt marker" in out.reason


def test_plan_halt_status_matches_devcontract():
    # verify keeps PLAN_HALT_STATUS as a literal to avoid a verify<-devcontract
    # import cycle; guard the two copies from drifting.
    from bmad_loop import devcontract

    assert verify.PLAN_HALT_STATUS == devcontract.PLAN_HALT_STATUS


def test_verify_review_stories_no_sprint_gate(project):
    # verify_review_stories checks spec == done + verify commands, no sprint gate.
    assert not project.sprint_status.is_file()
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    task.spec_file = str(sp)
    assert verify.verify_review_stories(task, project, Policy()).ok


def test_verify_review_stories_non_done_retries(project):
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "in-review", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review_stories(task, project, Policy())
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_review_stories_non_utf8_spec_retries(project):
    """A spec that became undecodable mid-run must produce a clean retry (status
    reads as ""), not a UnicodeDecodeError crash of review verification."""
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    sp.write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")
    task.spec_file = str(sp)
    out = verify.verify_review_stories(task, project, Policy())
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_review_bundle_ledger_gate(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "open"})
    out = verify.verify_review_bundle(task, project, Policy())
    assert not out.ok and out.fixable and "DW-2" in out.reason and "DW-1" not in out.reason

    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
    assert verify.verify_review_bundle(task, project, Policy()).ok


def test_verify_review_bundle_missing_entry_fails(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    bundle_ledger(project, {"DW-1": "done 2026-06-11"})  # DW-2 absent entirely
    out = verify.verify_review_bundle(task, project, Policy())
    assert not out.ok and out.fixable and "DW-2" in out.reason


def test_verify_shared_gates_oserror_degrades_to_retry(project, monkeypatch):
    """An unreadable spec at the dev gate is a retryable outcome, not a whole-run
    crash: the dev skill is still rewriting the spec this gate reads back, so a
    transient OSError has a designed producer. The reason must name the read fault
    — degrading to frontmatter {} would read as status "" and send a repair
    session after a status bug that does not exist."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    fault_read_text(monkeypatch, sp)

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and out.retryable and not out.fixable
    assert "spec unreadable" in out.reason and "PermissionError" in out.reason
    assert "spec status is" not in out.reason  # never masquerades as a status mismatch


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_oserror_degrades_to_retry(project, monkeypatch, mode):
    """Same degrade at all three review gates."""
    if mode == "review":
        write_sprint(project, {"1-1-a": "done"})
        task = make_task(project)
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "done", task.baseline_commit)
        gate = verify.verify_review
    elif mode == "review_stories":
        task = make_stories_task(project, "1")
        sp = write_story(
            project.planning_artifacts / "epic-a", "1", "x", "done", task.baseline_commit
        )
        gate = verify.verify_review_stories
    else:
        task = make_bundle_task(project)
        sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
        write_spec(sp, "done", task.baseline_commit)
        bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
        gate = verify.verify_review_bundle
    task.spec_file = str(sp)
    fault_read_text(monkeypatch, sp)

    out = gate(task, project, Policy())
    assert not out.ok and out.retryable
    assert "spec unreadable" in out.reason and "PermissionError" in out.reason
    assert "expected 'done'" not in out.reason


def _review_gate_at_done(project, mode):
    """A task+spec each review gate accepts, so the only thing left to decide the
    outcome is the verify commands. Mirrors the mode fan-out beside it."""
    if mode == "review":
        write_sprint(project, {"1-1-a": "done"})
        task = make_task(project)
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "done", task.baseline_commit)
        gate = verify.verify_review
    elif mode == "review_stories":
        task = make_stories_task(project, "1")
        sp = write_story(
            project.planning_artifacts / "epic-a", "1", "x", "done", task.baseline_commit
        )
        gate = verify.verify_review_stories
    else:
        task = make_bundle_task(project)
        sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
        write_spec(sp, "done", task.baseline_commit)
        bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
        gate = verify.verify_review_bundle
    task.spec_file = str(sp)
    return task, gate


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_run_commands_in_repo_root(project, tmp_path, mode):
    """`[verify] commands` run in the git root, not the BMAD project root (#695).

    The two are the same path everywhere except under an explicit `repo_root:`
    with `isolation = "none"` — `ProjectPaths.rebased` sets both, so worktree
    isolation never diverges — and the `project` fixture sets no `repo_root`, so
    no pre-existing row can tell the two apart. These three gates were the sole
    callers running the commands in `paths.project`; the dev side and
    `cli._reverify` both already used `repo_root`.

    Pinned from BOTH directions on purpose: a marker only the repo root holds
    must pass AND a marker only the project holds must fail. The positive probe
    identifies `repo_root`; the negative probe rules out `project` explicitly.

    It does NOT pin the other half of the split. The artifact reads resolve
    through `paths.sprint_status` / `paths.deferred_work` (derived from
    `implementation_artifacts`) and an absolute `task.spec_file`, none of which
    `dataclasses.replace(..., repo_root=...)` moves — so no ablation here can
    redden on them, and this row must not be read as evidence they stayed
    project-rooted.

    `repo_root` is a bare directory, not a git repo, deliberately: these three
    gates run no git in that root at all — only `subprocess.run(cwd=...)` — so a
    plain dir is the honest fixture. Turning it into a real repo would let a
    regression that started shelling out to git there pass unnoticed.

    The two markers and their RELATIVE probes come from `conftest`
    (`plant_root_markers`, `REPO_ROOT_MARKER_CMD`, `PROJECT_MARKER_CMD`) rather
    than being built here: the four other unpinned `[verify] commands` callers
    (both `Engine._verify_commands_with_results` stages, both `cli._reverify`
    call sites) are graded by the same two-direction probe, and a re-derived
    fixture would let one of those rows quietly ask a different question.

    INVERSE ablation: restore the pre-#695 root — `verify_commands_outcome(policy,
    paths.project)` in `_verify_review_commands` — and all three modes fail on the
    FIRST assertion, the repo-root marker going missing, before the refusal leg is
    reached. The gate here is a cwd choice rather than a check, so deleting code
    cannot reproduce the bug; only putting the old root back does."""
    repo_root = tmp_path / "code-root"
    repo_root.mkdir()
    plant_root_markers(repo_root=repo_root, project=project.project)
    paths = dataclasses.replace(project, repo_root=repo_root)
    task, gate = _review_gate_at_done(project, mode)

    assert gate(task, paths, Policy(verify=VerifyPolicy(commands=(REPO_ROOT_MARKER_CMD,)))).ok

    out = gate(task, paths, Policy(verify=VerifyPolicy(commands=(PROJECT_MARKER_CMD,))))
    assert not out.ok and "verify command failed" in out.reason


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_classify_against_the_root_they_run_in(
    project, tmp_path, monkeypatch, mode
):
    """The cwd is forwarded TWICE, and the row above pins only the first hop.

    `verify_commands_outcome` hands its `cwd` to `run_verify_commands` (execution)
    and again to `verify_command_results_outcome` -> `env_fault_reason` ->
    `_win32_env_fault_reason`, which resolves a command's leading token as
    `cwd / token` to tell "tool missing" from "command failed". The two outcomes
    are not interchangeable: an env fault ESCALATES and pauses the run, while an
    ordinary failure is `fixable` and routes a repair session.

    The sibling row cannot see the second hop. Both its assertions are rc-based —
    `.ok`, then `"verify command failed"`, which `verify_command_results_outcome`
    reaches only AFTER `env_fault_reason` returned None for every result — so
    re-rooting the classifier alone leaves it green. Confirmed by ablation: rewriting
    the helper as `verify_command_results_outcome(run_verify_commands(policy,
    paths.repo_root), paths.project)` passed all of `test_verify.py` +
    `test_engine.py` at 818 passed / 23 skipped. On Windows that split would probe a
    relative token such as `check.cmd`, living in the code root, against the project
    dir instead, miss it, fall through to the PATH branch and escalate — pausing the
    run over a command that had merely failed.

    Spying both hops rather than asserting on `env_fault` keeps this cross-platform:
    the classifier's own behavior is already pinned by the rows beside
    `_win32_env_fault_reason`, and what was unpinned is only WHICH ROOT reaches it
    from these three gates."""
    repo_root = tmp_path / "code-root"
    repo_root.mkdir()
    paths = dataclasses.replace(project, repo_root=repo_root)
    task, gate = _review_gate_at_done(project, mode)

    seen: dict[str, Path] = {}
    real_run = verify.run_verify_commands
    real_classify = verify.verify_command_results_outcome

    def spy_run(policy, cwd):
        seen["run"] = cwd
        return real_run(policy, cwd)

    def spy_classify(results, cwd):
        seen["classify"] = cwd
        return real_classify(results, cwd)

    monkeypatch.setattr(verify, "run_verify_commands", spy_run)
    monkeypatch.setattr(verify, "verify_command_results_outcome", spy_classify)

    assert gate(task, paths, Policy(verify=VerifyPolicy(commands=(_OK,)))).ok

    # both hops, not just execution: the classifier decides escalate-vs-retry
    assert seen["run"] == repo_root
    assert seen["classify"] == repo_root


def _break_the_check_before_the_commands(project, task, mode) -> None:
    """Fail the LAST gate check that precedes the verify commands, per mode.

    Deliberately the last one rather than the first: every gate opens on the spec
    status, so breaking that would prove only that the earliest check
    short-circuits and would leave the sprint and ledger checks — the ones that
    sit immediately in front of the commands — unexercised in all three modes.
    ``review_stories`` has no later check to break, so its spec status is the
    honest subject there."""
    if mode == "review":
        write_sprint(project, {"1-1-a": "in-progress"})
    elif mode == "review_stories":
        write_spec(Path(task.spec_file), "in-progress", task.baseline_commit)
    else:
        bundle_ledger(project, {"DW-1": "open", "DW-2": "open"})


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_hand_their_results_to_the_sink(project, mode):
    """Every review gate offers its verifier results to `on_results` — the seam
    the engine journals review-leg `verify-command-result` records through.

    Before this the three gates discarded their `CommandResult`s inside core, so a
    review pass left no per-command record and no `verify/` stream files, unlike
    the dev side. The results are handed over BEFORE classification (the order
    `Engine._verify_commands_with_results` already used), so the record exists
    whatever the classifier then decides — including an escalation that ends the
    run.

    Both commands are asserted, not just the count: the sink receives the whole
    tuple in configured order, which is what makes a record-per-command possible."""
    task, gate = _review_gate_at_done(project, mode)
    seen: list[tuple[verify.CommandResult, ...]] = []
    policy = Policy(verify=VerifyPolicy(commands=(_OK, _FAIL)))

    out = gate(task, project, policy, on_results=seen.append)

    assert not out.ok and out.fixable  # the classification is unchanged by the sink
    (results,) = seen  # called exactly once per gate invocation
    assert [r.command for r in results] == [_OK, _FAIL]
    assert [r.returncode for r in results] == [0, 1]


@pytest.mark.parametrize(
    "subject",
    [
        verify.verify_commands_outcome,
        verify._verify_review_commands,
        verify.verify_review,
        verify.verify_review_stories,
        verify.verify_review_bundle,
    ],
)
def test_review_result_sinks_are_keyword_only_with_a_default(subject):
    """The additive observation seam cannot silently bind a new positional
    argument at any layer; every existing call shape remains valid."""
    parameter = inspect.signature(subject).parameters["on_results"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_skip_the_sink_when_they_short_circuit(project, mode):
    """A gate that refuses before reaching its commands offers nothing: nothing
    ran, so there is nothing to record.

    The distinction is load-bearing for the journal — a record claims a verifier
    pass happened — and it is why the sink is threaded through the composition
    rather than fired at the top of each gate.

    Ablation: fire the sink at the top of each gate — necessarily with an empty
    tuple, since no results exist there yet — and every mode fails here on
    `seen == []`."""
    task, gate = _review_gate_at_done(project, mode)
    _break_the_check_before_the_commands(project, task, mode)
    seen: list[tuple[verify.CommandResult, ...]] = []

    out = gate(task, project, Policy(verify=VerifyPolicy(commands=(_OK,))), on_results=seen.append)

    assert not out.ok
    assert seen == []


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_call_the_sink_with_no_commands_configured(project, mode):
    """With `[verify] commands` empty the sink is still called, with `()`.

    "The pass ran and executed nothing" and "no pass ran" are different facts, and
    only the second is signalled by never calling the sink — which is precisely
    what the short-circuit row above asserts. The engine's sink then records
    nothing and allocates no sequence for an empty tuple, so this costs no journal
    entry; what it buys is that the two cases stay distinguishable at the seam."""
    task, gate = _review_gate_at_done(project, mode)
    seen: list[tuple[verify.CommandResult, ...]] = []

    assert gate(task, project, Policy(), on_results=seen.append).ok

    assert seen == [()]


@pytest.mark.parametrize("mode", ["review", "review_stories", "review_bundle"])
def test_verify_review_gates_are_unchanged_with_no_sink(project, mode):
    """Called from core with no sink — as every pre-existing caller does — the
    gates behave exactly as before: `on_results` is keyword-with-default, and its
    absence is not a second code path.

    Both verdicts, so the row cannot be satisfied by a gate that started refusing
    (or accepting) everything."""
    task, gate = _review_gate_at_done(project, mode)

    assert gate(task, project, Policy(verify=VerifyPolicy(commands=(_OK,)))).ok

    refused = gate(task, project, Policy(verify=VerifyPolicy(commands=(_FAIL,))))
    assert not refused.ok and refused.fixable and "verify command failed" in refused.reason


@pytest.mark.parametrize("mode", ["review", "review_bundle"])
def test_verify_review_gates_read_artifacts_from_the_project_root(project, tmp_path, mode):
    """The other half of the split `_verify_review_commands` states, and the half
    the row above deliberately cannot reach: only the command `cwd` moved to
    `repo_root` — the artifacts these gates read stay project-rooted.

    It has to be a decoy rather than an ablation. `dataclasses.replace(...,
    repo_root=...)` moves nothing else, and both `paths.sprint_status` and
    `paths.deferred_work` derive from `implementation_artifacts`, so no ablation
    of the root can redden on them. Instead plant a complete artifact tree at the
    same RELATIVE path under `repo_root`, carrying statuses that would fail the
    gate, and require the gate to pass anyway: a regression that re-derived either
    artifact from `repo_root` reads the decoy and reddens here.

    `review_stories` is absent on purpose — it reads neither artifact, so there is
    nothing for a decoy to shadow.

    The decoy is followed by a positive control, because on its own it asserts only
    that a gate PASSED, which is what it would also do if the gate had stopped
    reading these artifacts altogether. Planting the same statuses in the project's
    own tree and requiring a refusal is what establishes they are load-bearing —
    the discrimination is then built here rather than borrowed from the rows that
    happen to cover each failure separately.

    The control is ablated per mode, since each mode's refusal is carried by its
    own artifact. ABLATION A1: delete the `if sprint != expected:` refusal in
    `verify_review` and `[review]` fails on `assert not refused.ok` while
    `[review_bundle]` still passes. ABLATION A2: delete the `if not_done:` refusal
    in `verify_review_bundle` and `[review_bundle]` fails there instead, `[review]`
    passing. Reddening DISJOINT params is the point — it is what shows neither
    mode's control is being carried by the other mode's gate."""
    repo_root = tmp_path / "code-root"
    rel = project.implementation_artifacts.relative_to(project.project)
    decoy_paths = dataclasses.replace(project, implementation_artifacts=repo_root / rel)
    decoy_paths.implementation_artifacts.mkdir(parents=True)

    task, gate = _review_gate_at_done(project, mode)

    # both decoys carry the status that WOULD fail this gate, so either artifact
    # resolving off repo_root is a red test rather than a silent pass
    write_sprint(decoy_paths, {"1-1-a": "in-progress"})
    bundle_ledger(decoy_paths, {"DW-1": "open", "DW-2": "open"})

    paths = dataclasses.replace(project, repo_root=repo_root)

    assert gate(task, paths, Policy()).ok

    # positive control: the SAME statuses in the project's own artifacts must
    # refuse. Without this the row above passes for a gate that reads neither file.
    write_sprint(project, {"1-1-a": "in-progress"})
    bundle_ledger(project, {"DW-1": "open", "DW-2": "open"})

    refused = gate(task, paths, Policy())

    assert not refused.ok and refused.retryable
    assert ("in-progress" if mode == "review" else "DW-1") in refused.reason


def test_verify_review_bundle_ledger_oserror_degrades_to_retry(project, monkeypatch):
    """The ledger read is the same TOCTOU class as the spec read beside it — the
    orchestrator's own `mark_done` rewrites it between the dev and review gates."""
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
    fault_read_text(monkeypatch, project.deferred_work)  # spec reads fine

    out = verify.verify_review_bundle(task, project, Policy())
    assert not out.ok and out.retryable and not out.fixable
    assert "deferred-work ledger unreadable" in out.reason and "PermissionError" in out.reason
    assert "DW-1" not in out.reason  # not the "entries not marked done" verdict


def test_safe_rollback_reverts_tracked_and_removes_run_created(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))  # snapshot before the attempt
    (repo / "src.txt").write_text("dirty\n")  # tracked edit
    (repo / "junk.txt").write_text("run-created\n")  # untracked, created now
    keep = repo / ".bmad-loop" / "runs" / "r1"
    keep.mkdir(parents=True)
    (keep / "state.json").write_text("{}")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert (repo / "src.txt").read_text() == "original\n"  # tracked reverted
    assert not (repo / "junk.txt").exists()  # run-created removed
    assert (keep / "state.json").exists()  # .bmad-loop preserved


def test_safe_rollback_preserves_preexisting_untracked(project):
    repo = project.project
    (repo / "_bmad-output").mkdir(exist_ok=True)
    (repo / "_bmad-output" / "project-context.md").write_text("keep me\n")
    (repo / ".design-build").mkdir()
    (repo / ".design-build" / "x").write_text("keep me too\n")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))  # includes the two files above
    (repo / "junk.txt").write_text("run-created\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert (repo / "_bmad-output" / "project-context.md").read_text() == "keep me\n"
    assert (repo / ".design-build" / "x").read_text() == "keep me too\n"
    assert not (repo / "junk.txt").exists()  # only run-created file removed


def test_safe_rollback_keep_dir_protects_run_created(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    out = repo / "_bmad-output"
    out.mkdir(exist_ok=True)
    (out / "fresh-artifact.md").write_text("generated this run\n")  # run-created

    verify.safe_rollback(
        repo, baseline, baseline_untracked=snap, keep=(".bmad-loop", "_bmad-output")
    )
    assert (out / "fresh-artifact.md").exists()  # protected by keep even though new


def test_safe_rollback_none_snapshot_removes_nothing(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "src.txt").write_text("dirty\n")
    (repo / "junk.txt").write_text("untracked\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".bmad-loop",))
    assert (repo / "src.txt").read_text() == "original\n"  # tracked still reverted
    assert (repo / "junk.txt").exists()  # no snapshot => never delete untracked


def test_safe_rollback_prunes_emptied_dirs(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    nested = repo / "tmpdir" / "sub"
    nested.mkdir(parents=True)
    (nested / "f.txt").write_text("x\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert not (repo / "tmpdir").exists()  # emptied parent dirs pruned


def test_safe_rollback_resolution_failure_precedes_every_mutation(project, monkeypatch):
    """A cleanup target that cannot be canonicalized fails typed before stash,
    reset, unlink, or directory pruning, preserving both the checkout and the
    original path fault as the diagnostic cause.

    Ablation target: move `_rollback_cleanup_plan` below `stash create` or
    `reset --hard`, and this test fails on the recorded destructive git call before
    the injected resolution failure; narrow its exception translation and the
    `GitError`/cause assertions fail instead.
    """
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    created = repo / "uncertain" / "created.txt"
    created.parent.mkdir()
    created.write_text("run-created\n")
    (repo / "src.txt").write_text("tracked attempt\n")
    refuse_to_resolve(monkeypatch, created)

    git_calls: list[tuple[str, ...]] = []
    removals: list[tuple[str, Path]] = []
    real_git = verify._git
    real_git_out = verify._git_out
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir

    def spy_git(r, *args):
        git_calls.append(args)
        return real_git(r, *args)

    def spy_git_out(r, *args, env=None):
        git_calls.append(args)
        return real_git_out(r, *args, env=env)

    def spy_unlink(self, *args, **kwargs):
        removals.append(("unlink", self))
        return real_unlink(self, *args, **kwargs)

    def spy_rmdir(self, *args, **kwargs):
        removals.append(("rmdir", self))
        return real_rmdir(self, *args, **kwargs)

    monkeypatch.setattr(verify, "_git", spy_git)
    monkeypatch.setattr(verify, "_git_out", spy_git_out)
    monkeypatch.setattr(Path, "unlink", spy_unlink)
    monkeypatch.setattr(Path, "rmdir", spy_rmdir)

    with pytest.raises(verify.GitError, match="preflight rollback cleanup") as caught:
        verify.safe_rollback(repo, baseline, baseline_untracked=snap)

    assert isinstance(caught.value.__cause__, OSError)
    assert UNRESOLVABLE in str(caught.value.__cause__)
    assert not any(
        args[:2] in {("stash", "create"), ("reset", "--hard")} for args in git_calls
    )  # the read-only untracked probe ran, but no git mutation crossed the boundary
    assert removals == []  # no unlink/rmdir ran on an uncertain plan
    assert created.read_text() == "run-created\n"
    assert (repo / "src.txt").read_text() == "tracked attempt\n"


def test_safe_rollback_consumes_only_the_precomputed_confined_plan(project, tmp_path, monkeypatch):
    """A healthy plan removes and prunes a confined created path, preserves a kept
    path and an external symlink target, and performs no second resolution after
    `reset --hard` crosses the mutation boundary.

    INVERSE ablation: restore the post-reset `repo.resolve()`/per-target resolve
    loop or make `_prune_empty_parents` resolve its start again, and the exact-path
    refusal installed by the reset spy makes this test fail before the planned
    cleanup is consumed.
    """
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    created = repo / "tmpdir" / "sub" / "created.txt"
    created.parent.mkdir(parents=True)
    created.write_text("remove me\n")
    kept = repo / ".bmad-loop" / "keep.txt"
    kept.parent.mkdir()
    kept.write_text("keep me\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("operator data\n")
    external_link = repo / "external-link.txt"
    external_link.symlink_to(outside)
    (repo / "src.txt").write_text("tracked attempt\n")

    real_git = verify._git

    def refuse_after_reset(r, *args):
        result = real_git(r, *args)
        if args[:2] == ("reset", "--hard"):
            refuse_to_resolve(
                monkeypatch,
                repo,
                repo / ".bmad-loop",
                created,
                created.parent,
                external_link,
            )
        return result

    monkeypatch.setattr(verify, "_git", refuse_after_reset)

    verify.safe_rollback(repo, baseline, baseline_untracked=snap)

    assert (repo / "src.txt").read_text() == "original\n"
    assert not created.exists()
    assert not (repo / "tmpdir").exists()  # precomputed parents were pruned
    assert kept.read_text() == "keep me\n"
    assert external_link.is_symlink()  # external canonical targets are never removed
    assert outside.read_text() == "operator data\n"


def test_safe_rollback_preserves_tracked_artifact(project):
    """`preserve` keeps a *tracked* artifact edit (the resolve workflow's corrected
    spec) alive through the hard reset, while a tracked source edit is still
    reverted — `keep` alone only guards untracked deletion, not the reset."""
    repo = project.project
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    (repo / "src.txt").write_text("dev attempt\n")  # tracked source edit
    spec.write_text("frozen: corrected\n")  # tracked artifact edit (resolve)

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )
    assert (repo / "src.txt").read_text() == "original\n"  # source reverted
    assert spec.read_text() == "frozen: corrected\n"  # spec correction preserved


def test_safe_rollback_preserves_tracked_artifact_deletion(project):
    """A protected snapshot is authoritative when it deletes a tracked artifact.

    Restoring only paths present in the snapshot resurrects the baseline file and
    can re-wedge a resolved Stories sentinel. Ablation: delete the replay of
    ``deleted_preserve_paths`` after the snapshot checkout and this test fails
    because the deleted spec exists again.
    """
    repo = project.project
    spec = project.implementation_artifacts / "1-unresolved.md"
    spec.write_text("blocked sentinel\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sentinel")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    spec.unlink()  # the resolve workflow deliberately cleared this sentinel
    (repo / "src.txt").write_text("dev attempt\n")

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )

    assert not spec.exists()  # the protected deletion survives the hard reset
    assert (repo / "src.txt").read_text() == "original\n"  # source still reverts


def test_safe_rollback_preserves_file_to_directory_replacement(project):
    """A snapshot tree at a deleted baseline file path already replaces that file.

    Ablation: delete the snapshot-present filter from the preserved-deletion
    inventory and this test fails when ``git rm -f`` is handed the restored
    replacement directory without ``-r``.
    """
    repo = project.project
    artifact = project.implementation_artifacts / "result"
    artifact.write_text("baseline file\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "artifact file")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    artifact.unlink()
    artifact.mkdir()
    nested = artifact / "payload.md"
    nested.write_text("preserved replacement\n")
    git(repo, "add", "-A", "--", artifact_rel)
    (repo / "src.txt").write_text("dev attempt\n")

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )

    assert artifact.is_dir()
    assert nested.read_text() == "preserved replacement\n"
    assert (repo / "src.txt").read_text() == "original\n"


@needs_strict_codec
@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows filenames are UTF-16; no undecodable path exists"
)
@pytest.mark.parametrize("replace_with_directory", [False, True], ids=["deleted", "replaced"])
def test_safe_rollback_reads_preserved_deletion_inventory_as_bytes(project, replace_with_directory):
    """Protected deletion inventories keep POSIX filenames as bytes until use.

    The plain-deletion row exercises ``git diff --name-only -z`` and the later
    ``git rm`` replay; the file-to-directory row also exercises
    ``git ls-tree --name-only -z``.  All three commands can emit the raw ``0xff``
    byte in the filename; strict text mode turns that into ``GitError`` either
    before reset or, for ``git rm``, after the deletion has already been replayed.

    Ablation: remove ``binary=True`` or ``os.fsdecode`` from the corresponding
    inventory read, or restore text mode for the replay, and one or both rows fail.
    """
    repo = project.project
    artifact_dir = project.implementation_artifacts
    artifact_rel = artifact_dir.relative_to(repo).as_posix()
    raw_artifact = os.fsencode(artifact_dir) + b"/result-\xff"
    assert os.fsencode(os.fsdecode(b"result-\xff")) == b"result-\xff"
    with open(raw_artifact, "wb") as fh:
        fh.write(b"baseline file\n")
    git(repo, "add", "-A", "--", artifact_rel)
    git(repo, "commit", "-q", "-m", "non-UTF-8 artifact")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))

    os.unlink(raw_artifact)
    raw_nested = raw_artifact + b"/payload.md"
    if replace_with_directory:
        os.mkdir(raw_artifact)
        with open(raw_nested, "wb") as fh:
            fh.write(b"preserved replacement\n")
        git(repo, "add", "-A", "--", artifact_rel)
    (repo / "src.txt").write_text("dev attempt\n")

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )

    if replace_with_directory:
        assert os.path.isdir(raw_artifact)
        with open(raw_nested, "rb") as fh:
            assert fh.read() == b"preserved replacement\n"
    else:
        assert not os.path.exists(raw_artifact)
    assert (repo / "src.txt").read_text() == "original\n"


def test_safe_rollback_raises_on_genuine_restore_failure(project, monkeypatch):
    """A non-benign `git checkout` failure while restoring a `preserve` path must
    raise — not silently drop the correction (which would loop the re-drive). The
    benign 'pathspec did not match' case is tolerated; anything else is loud."""
    repo = project.project
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()
    spec.write_text("frozen: corrected\n")

    real_git = verify._git

    def fake_git(r, *args):
        if args[:1] == ("checkout",):  # the restore step only
            return 1, "fatal: unable to read tree (something broke)"
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", fake_git)
    with pytest.raises(verify.GitError, match="git checkout"):
        verify.safe_rollback(
            repo,
            baseline,
            baseline_untracked=snap,
            keep=(".bmad-loop", artifact_rel),
            preserve=(artifact_rel,),
        )


def test_safe_rollback_raises_when_the_preserve_snapshot_cannot_be_taken(project, monkeypatch):
    """A failed `git stash create` used to be swallowed into an empty snapshot, which
    silently disabled the whole restore — so the reset reverted exactly the paths
    `preserve` names (a resolved re-drive's corrected spec) with no error anywhere.
    Raise before the reset instead.

    Ablation target: drop the `if rc != 0 and preserve: raise` from safe_rollback
    and this fails — the spec comes back reverted, quietly.

    The fake stands on `_git_out`, which is the helper the `stash create` site reads
    through since #442 — on `_git` it would simply never fire, and the test would go
    green because nothing raised anywhere. The `pytest.raises` below is what proves
    it fired."""
    repo = project.project
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()
    spec.write_text("frozen: corrected\n")

    real_git_out = verify._git_out

    def fake_git_out(r, *args, env=None):
        if args[:2] == ("stash", "create"):
            return 1, "", "fatal: unable to write temporary index"
        return real_git_out(r, *args, env=env)

    monkeypatch.setattr(verify, "_git_out", fake_git_out)
    with pytest.raises(verify.GitError, match="git stash create"):
        verify.safe_rollback(
            repo,
            baseline,
            baseline_untracked=snap,
            keep=(".bmad-loop", artifact_rel),
            preserve=(artifact_rel,),
        )
    # raised BEFORE the reset, so the correction is still on disk to be re-tried
    assert spec.read_text() == "frozen: corrected\n"


def test_safe_rollback_degrades_stash_failure_when_nothing_to_preserve(project, monkeypatch):
    """The raise is scoped to callers that asked for a restore. With no `preserve`
    the snapshot is unused, so a `stash create` failure stays a non-event — both
    sweep call sites reset with no preserve and must not start failing.

    INVERSE ablation: drop the `and preserve` guard and this fails.

    The fake stands on `_git_out`, the helper the `stash create` site reads through
    since #442. `fired` is not decoration: this test's whole assertion is that
    NOTHING happened, so a fake patched onto the wrong helper would leave it green
    while proving nothing at all — the failure it exists to catch and the fake going
    inert are indistinguishable from the outside."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    (repo / "src.txt").write_text("dev attempt\n")

    real_git_out = verify._git_out
    fired: list[str] = []

    def fake_git_out(r, *args, env=None):
        if args[:2] == ("stash", "create"):
            fired.append("stash")
            return 1, "", "fatal: unable to write temporary index"
        return real_git_out(r, *args, env=env)

    monkeypatch.setattr(verify, "_git_out", fake_git_out)
    verify.safe_rollback(repo, baseline, baseline_untracked=snap)  # no preserve

    assert fired == ["stash"]  # the injected failure actually reached the site
    assert (repo / "src.txt").read_text() == "original\n"  # reset still happened


def test_safe_rollback_tolerates_empty_preserve_dir(project):
    """A `preserve` dir with no tracked content in the snapshot makes checkout exit
    non-zero ('did not match') — benign, must NOT raise."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    (repo / "src.txt").write_text("dev attempt\n")

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", "_bmad-output"),
        preserve=("_bmad-output",),  # no tracked files here at snapshot time
    )
    assert (repo / "src.txt").read_text() == "original\n"  # source still reverted


def test_safe_rollback_restores_a_preserve_dir_under_host_noise(project):
    """REAL-GIT axis (#442), on the family's most destructive row. `git stash create`
    exits 0 while still warning on stderr, so against `_git`'s merge `snapshot` became
    "<sha>\\nwarning: …" — a non-ref. The restore below then ran
    `git checkout '<non-ref>' -- <dir>`, which fails "fatal: invalid reference" (NOT
    the tolerated "did not match"), so safe_rollback raised — after the
    `git reset --hard` had already discarded the content `preserve` names. Destructive
    first, then loud.

    Both halves are load-bearing: the absent raise, and the corrected content on disk.
    A fix that merely stopped raising would leave the spec reverted just as silently
    as the failure this restore exists to prevent.

    Ablation target: put the `stash create` back on `_git` (the merge) and this fails,
    on `GitError: git checkout … fatal: invalid reference` — together with
    `test_safe_rollback_degrades_on_a_clean_tree_under_host_noise`, which grades the
    same site from its empty-stdout side. Measured, that revert reddens two further
    rows: `…_raises_when_the_preserve_snapshot_cannot_be_taken` and
    `…_degrades_stash_failure_when_nothing_to_preserve`, whose injected failures are
    patched onto `_git_out` because the site reads through it. Four rows, one site."""
    repo = project.project
    make_git_noisy(repo)
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    spec.write_text("frozen: corrected\n")  # the resolve workflow's correction
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec correction")  # committed above baseline
    (repo / "src.txt").write_text("dev attempt\n")  # dirt, so `stash create` has a tree

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )

    assert (repo / "src.txt").read_text() == "original\n"  # the reset ran
    assert spec.read_text() == "frozen: corrected\n"  # and the restore ran after it


def test_safe_rollback_degrades_on_a_clean_tree_under_host_noise(project):
    """The empty-stdout row of that same site. `git stash create` on a clean tree exits
    0 with EMPTY stdout, so under host noise the merged read was the warning and
    nothing else — making `snapshot` a non-ref exactly where the code's own comment
    promises "nothing to restore from", and turning the documented degrade into a
    raise (after the reset).

    "Treated as empty" is observed through behavior rather than by reaching into the
    function: with no restore attempted the preserve dir comes back at BASELINE
    content — the clean-tree degrade policy.toml is restored separately to work
    around. A restore driven by a corrupt snapshot cannot produce that; pre-fix it
    raises before it could.

    Ablation target: put the `stash create` back on `_git` (the merge) and this fails,
    on `GitError: git checkout … fatal: invalid reference` — together with
    `test_safe_rollback_restores_a_preserve_dir_under_host_noise`."""
    repo = project.project
    make_git_noisy(repo)
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    artifact_rel = project.implementation_artifacts.relative_to(repo).as_posix()

    spec.write_text("frozen: corrected\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spec correction")  # committed: no working-tree dirt
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""  # nothing to stash

    verify.safe_rollback(
        repo,
        baseline,
        baseline_untracked=snap,
        keep=(".bmad-loop", artifact_rel),
        preserve=(artifact_rel,),
    )

    assert spec.read_text() == "frozen: original\n"  # empty snapshot => no restore


def test_safe_rollback_preserves_uncommitted_policy_edit(project):
    """A hand-edited, tracked but *uncommitted* .bmad-loop/policy.toml (e.g. a
    freshly enabled scm.rollback_on_failure) must survive the hard reset — it is
    operator config, not the dev attempt's work. Regression: a `git reset --hard`
    used to silently revert it, so the very setting that gates auto-rollback was
    gone before it could fire."""
    repo = project.project
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("[scm]\nrollback_on_failure = false\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "track policy")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))

    pol.write_text("[scm]\nrollback_on_failure = true\n")  # operator enables it, uncommitted
    (repo / "src.txt").write_text("dev attempt\n")  # a real dev-attempt change

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert (repo / "src.txt").read_text() == "original\n"  # attempt reverted
    assert pol.read_text() == "[scm]\nrollback_on_failure = true\n"  # edit preserved


def test_safe_rollback_restores_policy_deleted_by_reset(project):
    """policy.toml added/committed *after* the baseline would be deleted by a
    reset to that older baseline; it is still restored from the pre-reset on-disk
    capture (the dirty src.txt here keeps the stash snapshot non-empty — the
    clean-tree, empty-snapshot path is covered by the test below)."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)  # baseline predates policy.toml
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("[scm]\nrollback_on_failure = true\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "add policy after baseline")
    snap = sorted(verify.untracked_files(repo))
    (repo / "src.txt").write_text("dev attempt\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert (repo / "src.txt").read_text() == "original\n"
    assert pol.read_text() == "[scm]\nrollback_on_failure = true\n"  # survived the reset


def test_safe_rollback_restores_committed_policy_on_clean_tree(project):
    """policy.toml committed AFTER the baseline, with an otherwise-clean tree:
    `git stash create` is empty, so the old stash-gated restore skipped it and
    `git reset --hard` reverted the operator's config. It must still survive."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)  # baseline predates policy.toml
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("[scm]\nrollback_on_failure = true\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "add policy after baseline")
    snap = sorted(verify.untracked_files(repo))
    # NOTE: no other working-tree change — tree is clean -> empty stash snapshot

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",))
    assert pol.read_text() == "[scm]\nrollback_on_failure = true\n"  # survived


def test_safe_rollback_restores_the_policy_through_the_atomic_helper(project, monkeypatch):
    """#379. The put-back was a bare `policy_path.write_bytes`, which truncates
    the file to zero and refills it in place: a host lost mid-write comes back
    with the operator's config neither reverted nor restored, and a truncated
    TOML is not a smaller config but a parse error the next `bmad-loop run`
    refuses on — arriving immediately after a rollback that already discarded the
    attempt's work. The helper fsyncs before the replace, so the crash window
    yields the old file or the whole new one.

    Graded by WRAPPING the binding rather than replacing it: the real write still
    lands, so the content assertion below is not measuring the stub. Recording
    the call also pins "exactly one write", which the guard above it is there to
    guarantee — a retry loop or a second unconditional write could not creep in
    unnoticed.

    Ablation: revert the call to `policy_path.write_bytes(policy_content)` and
    this reddens on `len(seen) == 1` — the helper is never reached."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)  # baseline predates policy.toml
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_bytes(b"[scm]\nrollback_on_failure = true\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "add policy after baseline")
    seen: list[tuple[Path, bytes]] = []
    real = verify.atomic_write_bytes_confined

    def record(path, data, *, confine_root, require_writable_target=False):
        seen.append((Path(path), data))
        real(path, data, confine_root=confine_root, require_writable_target=require_writable_target)

    # the CONFINED binding (#593). `verify.atomic_write_bytes` still exists — the
    # frontmatter writer keeps it — so patching that name would not raise, it would
    # simply record nothing; `len(seen) == 1` below is what catches that.
    monkeypatch.setattr(verify, "atomic_write_bytes_confined", record)

    verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".bmad-loop",))

    assert len(seen) == 1
    assert seen[0] == (pol, b"[scm]\nrollback_on_failure = true\n")
    assert pol.read_bytes() == b"[scm]\nrollback_on_failure = true\n"  # and it landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_safe_rollback_replaces_a_policy_symlink_by_name(project):
    """#379. The one row that grades this SITE's choice of a writer that replaces
    the NAME — since #593 the put-back calls `atomic_write_bytes_confined`, which
    is no-follow by construction; the row below grades the confinement half of
    that same call. Unlike the other writers moved to the helper on this branch,
    the expression replaced here was a direct `write_bytes` — which opens the name
    and so writes THROUGH a planted link — so replacing the name is a genuine
    change of behaviour, not a preservation of what `os.replace` already did.

    It is still the right change. `policy.write_mux_backend` already replaces this
    same file by name, so a link at `.bmad-loop/policy.toml` does not survive the
    orchestrator anyway; and `runsetup` states a driven session can write that
    path, which makes the link a session-chosen redirect for a host-side write.
    The reachable exploit is narrow but real, and it is exactly what this row
    builds: the restore only fires when the reset CHANGED what the name reads, so
    the redirect has to aim at a tracked in-repo file — which the reset then
    reverts and this write immediately clobbers with policy bytes.

    Ablation: swap the put-back's writer for `atomic_write_bytes(policy_path, ...)`
    at its follow-the-link default and this reddens on the link surviving and
    `shared.toml` rewritten (the confined-refusal row below reddens with it, on
    its `DID NOT RAISE`). It is still the one row here that plants a link at the
    policy's own name."""
    repo = project.project
    shared = repo / "shared.toml"  # tracked, and NOT the operator's policy
    shared.write_bytes(b"[scm]\nrollback_on_failure = false\n")
    git(repo, "add", str(shared))
    git(repo, "commit", "-q", "-m", "a tracked file a session could aim at")
    baseline = verify.rev_parse_head(repo)
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.symlink_to(Path("..") / "shared.toml")
    # the capture reads THROUGH the link, and the reset reverts what it points at,
    # so the put-back is reached with the link still standing
    shared.write_bytes(b"[scm]\nrollback_on_failure = true\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".bmad-loop",))

    assert not pol.is_symlink()  # the NAME was replaced
    assert pol.read_bytes() == b"[scm]\nrollback_on_failure = true\n"
    assert shared.read_bytes() == b"[scm]\nrollback_on_failure = false\n"  # not through


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_safe_rollback_policy_putback_refuses_a_symlinked_bmad_loop_dir(project, tmp_path):
    """The escape #593 names, at this site. The `follow_symlinks=False` the row
    above grades stops at the FINAL component: `.bmad-loop/` itself was still
    resolved by name, so a link planted there aimed both the temp and the
    published policy.toml out of the repo entirely. The put-back's own
    `mkdir(parents=True, exist_ok=True)` ACCEPTS a symlink-to-a-directory, so the
    planted parent survives the setup step instead of being replaced by it.

    Reaching the writer at all is the hard part, and it is asserted rather than
    assumed: the put-back only fires when the capture read a real file
    (`policy_content is not None`) AND the reset CHANGED what the name reads. So
    the redirect is built the way a session would have to build it — `.bmad-loop/`
    to a directory it owns, and `policy.toml` inside that directory back at a
    TRACKED in-repo file the reset reverts. `UnconfinedWriteError` can only come
    from that one confined call, so the raise itself is the proof the write was
    reached; the pre-assert pins the capture half independently.

    The last two assertions are the load-bearing ones: refusing loudly is worth
    nothing if the operator's config already landed outside the project.

    Ablation: revert the call to
    `atomic_write_bytes(policy_path, policy_content, follow_symlinks=False)` and
    this fails `DID NOT RAISE`, with the planted link in `outside/` replaced by a
    real policy.toml holding the operator's config."""
    repo = project.project
    outside = tmp_path / "outside"  # sibling of the sandbox, genuinely outside it
    outside.mkdir()
    shared = repo / "shared.toml"  # tracked, and NOT the operator's policy
    shared.write_bytes(b"[scm]\nrollback_on_failure = false\n")
    git(repo, "add", str(shared))
    git(repo, "commit", "-q", "-m", "a tracked file a session could aim at")
    baseline = verify.rev_parse_head(repo)

    (repo / ".bmad-loop").symlink_to(outside, target_is_directory=True)
    (outside / "policy.toml").symlink_to(shared)
    shared.write_bytes(b"[scm]\nrollback_on_failure = true\n")
    pol = repo / ".bmad-loop" / "policy.toml"
    # precondition: the capture reads a real file through both hops, so
    # `policy_content` is non-None and the reset below makes `current` differ
    assert pol.read_bytes() == b"[scm]\nrollback_on_failure = true\n"

    with pytest.raises(platform_util.UnconfinedWriteError):
        verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".bmad-loop",))

    assert shared.read_bytes() == b"[scm]\nrollback_on_failure = false\n"  # reset, not rewritten
    assert (outside / "policy.toml").is_symlink()  # no file was published out here
    assert sorted(x.name for x in outside.iterdir()) == ["policy.toml"]  # nor staged


def test_safe_rollback_policy_putback_is_confined_to_the_repo(project, monkeypatch):
    """The positive control for the refusal above, and the row that grades the two
    keywords this site passes rather than the behaviour they buy.

    `confine_root` is the one component the anchored walk never checks — it is
    where the walk STARTS — so rooting this call at `policy_path.parent` would be
    lexically confined and still admit the whole #593 escape, silently. It has to
    be the repo, which is exactly what `policy_path` was spelled from at the
    capture. `require_writable_target` is #597: policy.toml is operator config, so
    a read-only one is refused with the kernel's `PermissionError` instead of
    being routed around by a replace that only needs the directory writable.

    The wrap keeps the real write, so this is a control and not a stub
    measurement: the restore below actually lands on disk.

    Ablation: change `confine_root=repo` to `confine_root=policy_path.parent` and
    this reddens alone (every behavioural row above stays green, which is the
    point of grading the keyword); drop `require_writable_target=True` and it
    reddens on the flag row."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)  # baseline predates policy.toml
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_bytes(b"[scm]\nrollback_on_failure = true\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "add policy after baseline")
    seen: list[dict[str, object]] = []
    real = verify.atomic_write_bytes_confined

    def record(path, data, *, confine_root, require_writable_target=False):
        seen.append({"root": Path(confine_root), "writable": require_writable_target})
        real(path, data, confine_root=confine_root, require_writable_target=require_writable_target)

    monkeypatch.setattr(verify, "atomic_write_bytes_confined", record)

    verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".bmad-loop",))

    assert len(seen) == 1  # the writer was reached
    assert seen[0]["root"] == repo  # the REPO, not `.bmad-loop/` — see above
    assert seen[0]["writable"] is True
    assert pol.read_bytes() == b"[scm]\nrollback_on_failure = true\n"  # and it landed


def test_attempt_dirty_ignores_lone_policy_edit(project):
    """A diff confined to policy.toml is operator config, not the attempt's
    dirtiness — so a stopped attempt whose only residue is a policy edit reads as
    clean and the manual-recovery loop can terminate."""
    repo = project.project
    pol = repo / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("[scm]\nrollback_on_failure = false\n")
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "track policy")
    baseline = verify.rev_parse_head(repo)

    pol.write_text("[scm]\nrollback_on_failure = true\n")  # lone policy edit
    assert verify.attempt_dirty(repo, baseline, []) is False
    (repo / "src.txt").write_text("real change\n")  # plus a real change
    assert verify.attempt_dirty(repo, baseline, []) is True


def test_worktree_clean_ignores_policy_file(project):
    # A tracked-but-modified .bmad-loop/policy.toml (rewritten by the TUI
    # settings editor) must not count as a dirty tree, or every settings edit
    # would force a commit before run/sweep/validate.
    pol = project.project / ".bmad-loop" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text('[gates]\nmode = "none"\n')
    git(project.project, "add", "-f", str(pol))
    git(project.project, "commit", "-q", "-m", "track policy")
    assert verify.worktree_clean(project.project)

    pol.write_text('[gates]\nmode = "per-epic"\n')  # edit the tracked config
    assert verify.worktree_clean(project.project)  # still "clean"

    (project.project / "src.txt").write_text("real change\n")  # any other edit
    assert not verify.worktree_clean(project.project)


def test_worktree_clean_flags_untracked_non_policy(project):
    (project.project / "stray.txt").write_text("untracked\n")
    assert not verify.worktree_clean(project.project)


def _porcelain(repo) -> set[str]:
    """`git status --porcelain` as a set of RAW records.

    Not conftest's `git()`: that returns `stdout.strip()`, which eats the leading
    status space of the FIRST line only — so ` M path` arrives as `M path` or not,
    depending on sort order. These assertions are about the exact two-character
    status field, so they need the bytes git actually emitted."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(proc.stdout.splitlines())


def test_worktree_clean_flags_a_stranded_policy_temp(project):
    """#363's PREMISE, not its fix — nothing here exercises the atomic-write
    change. It pins the reason the issue matters: a `.bmad-loop/policy.toml.tmp`
    left behind by a failed replace is an UNTRACKED file that no ignore rule
    covers, so `worktree_clean` goes False and stays False until a human deletes
    it. Deleting the temp is what flips the verdict back, with the policy edit
    still on disk.

    Every porcelain assertion below is a PRECONDITION, deliberately spelled as an
    exhaustive set rather than a fixture. `conftest._isolate_ambient_git_ignores`
    is session-scoped and autouse, so it is inherited with no opt-in — but it
    deliberately does NOT set `GIT_CONFIG_NOSYSTEM` (that would suppress
    Git-for-Windows' system `core.autocrlf`), which leaves a system
    `core.excludesFile`, a system `status.showUntrackedFiles=no`, and
    `.git/info/exclude` all reachable. Step 6's single line closes every one of
    them at once — plus untracked-dir collapsing — and does it as a LOUD red on
    the precondition rather than a silent green on the verdict.

    Ablation A10: change verify.py's `:(exclude){POLICY_FILE_REL}` to
    `:(exclude){POLICY_FILE_REL}*` and step 7 reddens ALONE — the trailing glob
    swallows the `.tmp` sibling too, which is the fake-green this test exists to
    catch. `test_worktree_clean_ignores_policy_file` and
    `test_worktree_clean_flags_untracked_non_policy` stay green.

    Ablation A11: delete the `:(exclude)` element entirely and the OTHER direction
    reddens — `test_worktree_clean_ignores_policy_file` goes red, and here it is
    step 5's CONTROL that fires, ABORTING this test before step 7 is ever reached.
    Read that precisely: under A11 step 7 does not "stay green", it does not run.
    The pair is graded by WHICH STEP each ablation reddens — 7 for A10, 5 for A11 —
    because steps 4-5 and 6-8 present git-visible states identical but for one
    file and demand OPPOSITE verdicts. That is what makes this grade which NAME the
    exclude covers, in both directions, rather than merely that it has one."""
    repo = project.project
    pol = repo / ".bmad-loop" / "policy.toml"
    tmp = repo / ".bmad-loop" / "policy.toml.tmp"

    # 1. track the policy file — this also makes `.bmad-loop/` a TRACKED directory,
    #    so git can never collapse the untracked record below to `?? .bmad-loop/`
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text('[gates]\nmode = "none"\n')
    git(repo, "add", "-f", str(pol))
    git(repo, "commit", "-q", "-m", "track policy")

    assert _porcelain(repo) == set()  # 2. no ambient dirt
    assert verify.worktree_clean(repo)  # 3. baseline verdict

    # 4. PRECONDITION: git DOES see the edit
    pol.write_text('[gates]\nmode = "per-epic"\n')
    assert _porcelain(repo) == {" M .bmad-loop/policy.toml"}

    assert verify.worktree_clean(repo)  # 5. CONTROL: policy.toml itself reads CLEAN

    # 6. PRECONDITION: untracked reporting is ON, at FILE granularity, and nothing
    #    ignores `*.tmp` — one line closing showUntrackedFiles=no, a system
    #    excludesFile, .git/info/exclude and dir-collapsing together
    tmp.write_text('[gates]\nmode = "per-epic"\n')
    assert _porcelain(repo) == {
        " M .bmad-loop/policy.toml",
        "?? .bmad-loop/policy.toml.tmp",
    }

    assert not verify.worktree_clean(repo)  # 7. THE GRADED ASSERTION

    # 8. differential: the verdict flips back with the policy edit still on disk
    tmp.unlink()
    assert verify.worktree_clean(repo)


# ------------------------------------------------- git pathspec hardening (#423)
#
# Every fixture below is built in PYTHON, never through a shell: fish and zsh
# glob-expand `docs[a]` / `a*b` / `q?r` into non-existence, and `git rm --cached
# '<glob>'` takes its argument as a pathspec too — both produced void fixtures
# (a test that passes because it tested nothing) on the 0.9.1 hotfix.
#
# `*`, `?` and `:` are all reserved in Windows FILENAMES (`:` is the drive
# separator), so a fixture that must CREATE a directory carrying one is
# Linux-only — `mkdir` raises WinError 123 before the test can assert anything.
# `[` and `]` are legal everywhere, and per #423's reachability analysis they are
# also the realistic carrier since `implementation_artifacts` comes out of the
# operator's config, so the bracket rows stay unmarked and cover both CI
# platforms. Note this constrains only fixture FILENAMES: a pathspec STRING
# containing `*` is just a string, so the rows that merely pass `doc*` as a
# configured prefix run everywhere.
RESERVED_IN_WINDOWS_FILENAMES = pytest.mark.skipif(
    sys.platform == "win32", reason="`*`, `?` and `:` are reserved in Windows filenames"
)


def _metachar_pair(repo, meta: str, victim: str):
    """A metachar dir and the sibling its glob collides with, both committed.

    Returns the two `f.md` paths. `doc*` is the shape that matters most: `*`
    crosses `/`, so it is the only one that reaches a sibling DIRECTORY's
    contents — `[…]`/`?` are same-length and reach a sibling FILE only.
    """
    for d in (meta, victim):
        (repo / d).mkdir(parents=True, exist_ok=True)
        (repo / d / "f.md").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "metachar fixture")
    return repo / meta / "f.md", repo / victim / "f.md"


def test_file_holds_content_accepts_a_pristine_board_in_either_eol_domain(project):
    """Sameness here is GIT's question, and a byte compare cannot ask it (#618).

    A board git checked out under a normalizing config is CRLF; one an editor or a
    byte-writing tool left is LF; git reports the tree clean for both. A byte compare
    against HEAD therefore has to guess which end of the round trip the file sits at,
    and each guess refuses a pristine board on the hosts the other guess serves — a raw
    baseline refuses (a), a smudged baseline refuses (b). Hashing both sides through the
    path's clean filter draws exactly the distinction git draws.

    (c) is what keeps that from being a rubber stamp: cleaning collapses terminators,
    never content, so an operator's added row is still not this run's advance.
    """
    repo = project.project
    # `core.autocrlf=true` rather than an `eol=crlf` attribute: it is Git-for-Windows'
    # system default, and it is the config under which BOTH spellings below read clean.
    # An explicit `eol=crlf` is stricter and reports the LF file as ` M`, so case (a)
    # could not arise there at all.
    git(repo, "config", "core.autocrlf", "true")
    (repo / "board.yaml").write_bytes(b"development_status:\n  1-1-a: backlog\n")
    git(repo, "add", "--", "board.yaml")
    git(repo, "commit", "-q", "-m", "an eol-normalizing board")
    head = verify.file_bytes_at_revision(repo, "HEAD", "board.yaml")
    assert head is not None and b"\r\n" not in head  # git stores the LF twin
    board = repo / "board.yaml"

    # (a) the LF form a byte-writing tool leaves. Ordered FIRST because it is only
    # reachable before a checkout has smudged the path: git re-reads the file through
    # the clean filter here and calls it identical, where overwriting an already-CRLF
    # checkout with LF instead reports ` M` and never reaches this comparison at all.
    assert b"\r\n" not in board.read_bytes()
    git(repo, "update-index", "--refresh")  # no stat-cache false green
    assert not verify.dirty_paths(repo)
    assert verify.file_holds_content(repo, "board.yaml", board, head)

    # (b) the CRLF form git itself materializes on checkout — equally clean, and the
    # byte compare that serves (a) refuses this one.
    board.unlink()
    git(repo, "checkout", "--", "board.yaml")
    assert b"\r\n" in board.read_bytes()
    assert not verify.dirty_paths(repo)
    assert verify.file_holds_content(repo, "board.yaml", board, head)

    # (c) content is still compared: an operator's row is not this pass's advance
    board.write_bytes(board.read_bytes() + b"  9-9-operator: backlog\r\n")
    assert not verify.file_holds_content(repo, "board.yaml", board, head)


def test_index_holds_no_foreign_content_guards_the_half_the_working_tree_cannot(project):
    """A staged edit is destroyed by the carry, not committed by it (#618).

    `commit_paths` runs `git add` for the path, which copies the WORKING TREE into the
    commit and overwrites the INDEX in place. So an operator who staged an edit and then
    restored the working copy loses those bytes to a carry that proved only the working
    tree: absent from the commit, which took the other content, and absent from disk.
    Nothing surfaces it — the tree reads clean afterwards.

    HEAD's own content and the advance itself are both safe to overwrite: the first is
    still in history, the second is the write being authorized.
    """
    repo = project.project
    head_bytes = b"development_status:\n  1-1-a: ready-for-dev\n"
    (repo / "board.yaml").write_bytes(head_bytes)
    git(repo, "add", "--", "board.yaml")
    git(repo, "commit", "-q", "-m", "board")
    advance = head_bytes.replace(b"ready-for-dev", b"done")

    assert verify.index_holds_no_foreign_content(repo, "board.yaml", advance)  # HEAD's own

    (repo / "board.yaml").write_bytes(advance)
    git(repo, "add", "--", "board.yaml")
    assert verify.index_holds_no_foreign_content(repo, "board.yaml", advance)  # the advance

    # the operator's own bytes, staged — reachable nowhere else once `git add` runs
    (repo / "board.yaml").write_bytes(head_bytes + b"  9-9-operator: backlog\n")
    git(repo, "add", "--", "board.yaml")
    (repo / "board.yaml").write_bytes(advance)  # ...working tree restored over them
    assert not verify.index_holds_no_foreign_content(repo, "board.yaml", advance)

    # an untracked, unstaged path has nothing to overwrite
    assert verify.index_holds_no_foreign_content(repo, "never-staged.yaml", advance)


def test_index_holds_no_foreign_content_refuses_a_staged_untracking(project):
    """An ABSENT index entry is not by itself "nothing to overwrite".

    With HEAD carrying the path, no entry is a staged DELETION — `git rm --cached`,
    which is how an operator untracks a board they are about to gitignore, and this
    project documents both board shapes rather than treating that as exotic. The
    carry's `git add` restores the entry, and the intent then exists nowhere: not in
    HEAD, which never had it, and not in the index that just lost it. `git status`
    reads clean afterwards, so nothing surfaces the reversal.

    Reachable in earnest, unlike a bare truth-table hole: the probe that gates this
    check reports the path (`??`, the file being on disk and no longer in the index),
    so the ownership proof really is asked and really does answer.

    The sibling row above ends on `never-staged.yaml` — no entry and no HEAD blob —
    which is the case this must NOT break, and is why the answer keys on HEAD rather
    than on refusing every empty index.

    Ablation: restore `if not records: return True` and this row fails while that
    sibling stays green."""
    repo = project.project
    head_bytes = b"development_status:\n  1-1-a: ready-for-dev\n"
    (repo / "board.yaml").write_bytes(head_bytes)
    git(repo, "add", "--", "board.yaml")
    git(repo, "commit", "-q", "-m", "board")
    advance = head_bytes.replace(b"ready-for-dev", b"done")

    git(repo, "rm", "--cached", "-q", "--", "board.yaml")  # untracked, still on disk
    assert git(repo, "ls-files", "-s", "--", "board.yaml") == ""  # no entry at all
    assert "board.yaml" in verify.dirty_paths(repo)  # ...and the gate above still asks

    assert not verify.index_holds_no_foreign_content(repo, "board.yaml", advance)


def test_file_holds_content_raises_rather_than_answering_when_git_cannot_hash(project):
    """An id it could not compute must not read as a verdict either way.

    The caller gates a repair write on this, so the answer has to be git's or nobody's.
    """
    repo = project.project

    with pytest.raises(verify.GitError, match="hash-object"):
        verify.file_holds_content(repo, "board.yaml", repo / "never-written.yaml", b"x")


def test_path_tracked_reports_index_membership(project):
    """The basic contract: an index entry is True, an untracked file is False."""
    repo = project.project
    assert verify.path_tracked(repo, "src.txt")
    (repo / "stray.txt").write_text("untracked\n")
    assert not verify.path_tracked(repo, "stray.txt")
    assert not verify.path_tracked(repo, "never_existed.txt")


@pytest.mark.parametrize(
    ("meta", "neighbour"),
    [
        ("docs[a]", "docsa"),
        pytest.param("a*b", "axb", marks=RESERVED_IN_WINDOWS_FILENAMES),
        pytest.param("q?r", "qxr", marks=RESERVED_IN_WINDOWS_FILENAMES),
    ],
)
def test_path_tracked_refuses_a_glob_match_from_a_neighbour(project, meta, neighbour):
    """An ABSENT path whose name carries pathspec metacharacters must not read as
    tracked just because some OTHER tracked path matches it as a glob.

    This is the inverted, silent failure: `_ledger_is_gits_to_restore` answers
    "git owns it" for a ledger git has never seen, so the harvest revert skips
    its `unlink()` and a finding about discarded code stays open. Ablating the
    `:(literal)` prefix flips the first assertion to True."""
    repo = project.project
    _metachar_pair(repo, meta, neighbour)
    # Untrack ONLY the metachar path; its glob-colliding neighbour stays tracked.
    git(repo, "rm", "-r", "-q", "--cached", "--", f":(literal){meta}")
    assert not verify.path_tracked(repo, f"{meta}/f.md")
    assert verify.path_tracked(repo, f"{neighbour}/f.md")


def test_path_tracked_still_matches_a_directory_prefix(project):
    """`:(literal)` must not cost the callers the prefix match they rely on —
    `cmd_validate`'s render-tracked warning probes a DIRECTORY, not a file."""
    repo = project.project
    (repo / "_bmad" / "render").mkdir(parents=True, exist_ok=True)
    (repo / "_bmad" / "render" / "render_skill.py").write_text("# renderer\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "render")
    assert verify.path_tracked(repo, "_bmad/render")


def test_path_tracked_file_separates_a_file_from_a_directory_prefix(project):
    """Why this predicate is narrower than its sibling: `path_tracked` answers True for
    BOTH a tracked file and a tracked directory, and its callers have to tell those
    apart. Since #484 the shield asks `path_tracked_kind` for that — three answers, not
    two — and this boolean is kept for `_pin_tracked_config_rewrite`, where only a
    tracked FILE can carry the skip-worktree bit the pin depends on (#392, #484).

    Ablation: return `bool(entries)` instead of comparing the set — the mechanics now
    live in `path_tracked_kind`, which this delegates to — and the directory assertion
    flips to True. The shield would then read a tracked skill tree as a tracked FILE and
    drop its pattern outright, where #484 has it SUBSTITUTE one pattern per file
    provisioning actually wrote; nothing would shield that tree at all."""
    repo = project.project
    (repo / "_bmad" / "render").mkdir(parents=True, exist_ok=True)
    (repo / "_bmad" / "render" / "render_skill.py").write_text("# renderer\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "render")

    # The sibling cannot tell these apart; this one must.
    assert verify.path_tracked(repo, "src.txt")
    assert verify.path_tracked(repo, "_bmad/render")
    assert verify.path_tracked_file(repo, "src.txt")
    assert not verify.path_tracked_file(repo, "_bmad/render")
    assert verify.path_tracked_file(repo, "_bmad/render/render_skill.py")


def test_path_tracked_file_is_false_for_untracked_and_absent(project):
    """An untracked file and a path git has never seen both answer False — the
    shield keeps their patterns, which is the whole point of the shield."""
    repo = project.project
    (repo / "stray.txt").write_text("untracked\n")
    assert not verify.path_tracked_file(repo, "stray.txt")
    assert not verify.path_tracked_file(repo, "never_existed.txt")


def test_path_tracked_file_reads_a_metachar_name_past_its_glob_neighbour(project):
    """The literal-pathspec hazard, in the ONE direction this function can get wrong.

    ⚠️ Written first as the sibling's assertion — an ABSENT metachar path must not read
    as tracked — and that ablated GREEN, because the set comparison already refuses it
    for a different reason: a glob match answers with the NEIGHBOUR'S name, which is not
    the name asked for, so the set differs and the result is False regardless of the
    pathspec. Emptiness-reading `path_tracked` needs `:(literal)` for that direction;
    this one does not.

    What it does need it for is the opposite direction. When the metachar path IS a
    tracked file and its glob neighbour is tracked too, the bare pathspec returns BOTH
    names, the set comparison fails, and a genuine tracked file reads False — the shield
    then keeps an inert pattern and the #392 hygiene failure survives for any project
    whose hook config or skill tree carries `[`, `*` or `?`.

    Ablation: drop the `:(literal)` prefix — the spec now lives in `path_tracked_kind`,
    which this delegates to — and the first assertion flips to False."""
    repo = project.project
    _metachar_pair(repo, "docs[a]", "docsa")  # both committed, and the glob collides
    assert verify.path_tracked_file(repo, "docs[a]/f.md")
    assert verify.path_tracked_file(repo, "docsa/f.md")
    # The absent-path direction still holds; it is just not what pins the pathspec.
    git(repo, "rm", "-r", "-q", "--cached", "--", ":(literal)docs[a]")
    assert not verify.path_tracked_file(repo, "docs[a]/f.md")


def test_path_tracked_file_raises_on_git_failure(project, monkeypatch):
    """Contracted to raise like every other probe here, so the shield's caller can
    make its own keep-the-pattern decision rather than inherit a silent False —
    which would drop a pattern on a fault and leak seeded files into a story commit."""
    repo = project.project

    def boom(_repo, *_args):
        raise verify.GitError("ls-files exploded")

    monkeypatch.setattr(verify, "git_bytes", boom)
    with pytest.raises(verify.GitError):
        verify.path_tracked_file(repo, "src.txt")


def test_path_tracked_kind_separates_untracked_file_and_dir(project):
    """The tri-state the shield reads (#484). A tracked FILE, a tracked DIRECTORY prefix
    and a path with no index entry want three different pattern treatments — drop,
    substitute one pattern per file provisioning wrote, keep — and the boolean sibling
    collapses the last two into a single False.

    Ablation: in `path_tracked_kind`, discriminate on `bool(entries)` instead of
    comparing the set against `{os.fsencode(rel)}`, and the directory case answers
    "file" — the shield would read a tracked skill tree as a tracked file, drop its
    pattern outright, and put nothing at all in its place."""
    repo = project.project
    (repo / "_bmad" / "render").mkdir(parents=True, exist_ok=True)
    (repo / "_bmad" / "render" / "render_skill.py").write_text("# renderer\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "render")
    (repo / "stray.txt").write_text("untracked\n")

    assert verify.path_tracked_kind(repo, "src.txt") == "file"
    assert verify.path_tracked_kind(repo, "_bmad/render/render_skill.py") == "file"
    # A directory lists the entries BENEATH it, never its own name — at either depth.
    assert verify.path_tracked_kind(repo, "_bmad/render") == "dir"
    assert verify.path_tracked_kind(repo, "_bmad") == "dir"
    # Both untracked directions, which `path_tracked` cannot separate either.
    assert verify.path_tracked_kind(repo, "stray.txt") == "untracked"
    assert verify.path_tracked_kind(repo, "never_existed.txt") == "untracked"


def test_path_tracked_kind_reads_a_metachar_name_past_its_glob_neighbour(project):
    """The literal-pathspec hazard, in the direction reading the output's TEXT cannot
    refuse on its own.

    When the metachar path IS a tracked file and its glob neighbour is tracked too, a
    bare pathspec returns BOTH names. The set then exceeds the singleton and a genuine
    tracked FILE reads "dir", so the shield substitutes per-file patterns for a tree it
    never wrote — for any project whose hook config or skill tree carries `[`, `*` or
    `?`.

    Ablation: drop the `:(literal)` prefix from `path_tracked_kind`'s spec alone (inline
    the bare rel in place of `_literal_specs`, so the sibling probes that share that
    helper stay untouched) and the first assertion flips to "dir". The absent-path
    assertion below flips too, to "dir" on the neighbour's name alone — where the
    boolean sibling's own record notes that direction ablates GREEN there, because
    `{neighbour} != {rel}` reads False whichever way it got there. Widening the answer
    from two states to three is what gives the second direction teeth."""
    repo = project.project
    _metachar_pair(repo, "docs[a]", "docsa")  # both committed, and the glob collides
    assert verify.path_tracked_kind(repo, "docs[a]/f.md") == "file"
    assert verify.path_tracked_kind(repo, "docsa/f.md") == "file"
    git(repo, "rm", "-r", "-q", "--cached", "--", ":(literal)docs[a]")
    assert verify.path_tracked_kind(repo, "docs[a]/f.md") == "untracked"


def test_path_tracked_kind_raises_on_git_failure(project, monkeypatch):
    """Contracted to raise like every other probe here, so the shield's caller makes its
    own keep-the-pattern decision rather than inheriting a silent answer. Of the three,
    a silent "file" drops the pattern and leaks seeded files into a story commit and a
    silent "dir" substitutes patterns for a tree provisioning never wrote; only
    "untracked" happens to coincide with the degrade, and a probe must not depend on
    which fault it draws.

    Pins the rc≠0 branch specifically: `git_bytes` returns the returncode as an ANSWER
    and never raises on it, so nothing but this check turns a failed spawn into a fault.

    Ablation: delete the `if proc.returncode != 0` raise and the call returns
    "untracked" — a failed probe reading as a clean answer. That same mutation leaves
    `test_path_tracked_file_raises_on_git_failure` GREEN, because it monkeypatches
    `git_bytes` to RAISE rather than to return a bad rc: the sibling pins propagation,
    not the branch that manufactures the fault, and only this test covers the latter."""
    repo = project.project

    def failed(_repo, *_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout=b"", stderr=b"fatal: bad index file"
        )

    monkeypatch.setattr(verify, "git_bytes", failed)
    with pytest.raises(verify.GitError) as excinfo:
        verify.path_tracked_kind(repo, "src.txt")
    # The BARE rel is the operator's informative half; the magic prefix is our plumbing.
    assert "src.txt" in str(excinfo.value)
    assert "fatal: bad index file" in str(excinfo.value)


@RESERVED_IN_WINDOWS_FILENAMES
def test_path_tracked_reads_a_rel_beginning_with_a_colon(project):
    """A leading `:` is pathspec magic. Bare, git parses it as an (unknown) magic
    word and answers the empty set — i.e. "untracked", the direction that
    authorizes a delete. The literal prefix disarms it."""
    repo = project.project
    odd = repo / ":weird"
    odd.mkdir(exist_ok=True)
    (odd / "f.md").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "colon dir")
    assert verify.path_tracked(repo, ":weird/f.md")


def test_path_tracked_keeps_a_tracked_but_deleted_path(project):
    """The index entry outlives the file, and that is exactly the state a caller
    must not mistake for "not git's" — `reset --hard` will bring it back."""
    repo = project.project
    (repo / "src.txt").unlink()
    assert verify.path_tracked(repo, "src.txt")


def test_path_tracked_ignores_stderr_chatter_on_success(project, monkeypatch):
    """`ls-files` exits 0 while still writing to stderr (an unexecutable
    `core.fsmonitor` hook, an unknown `core.fsyncMethod`). Read against `_git`'s
    stdout+stderr MERGE that chatter is indistinguishable from an index entry, so
    an untracked path answers "tracked" — silent and inverted. Only stdout counts."""
    real = verify._run_git

    def noisy(cmd, repo, **kw):
        proc = real(cmd, repo, **kw)
        if "ls-files" in cmd:
            proc.stderr = "warning: unable to access '.git/hooks/fsmonitor': no such file\n"
        return proc

    monkeypatch.setattr(verify, "_run_git", noisy)
    assert not verify.path_tracked(project.project, "never_existed.txt")


def test_path_tracked_raises_on_git_failure(project):
    """Raises GitError like every other probe here — callers inside a rollback
    `finally` degrade toward leaving the file alone."""
    with pytest.raises(verify.GitError, match="ls-files"):
        verify.path_tracked(project.project / "not-a-repo", "src.txt")


# ------------------------------------------------------------- path_ignored (#577)
#
# The predicate `confirm` uses to keep a gitignored board OUT of its commit list.
# Contracted to answer git's own `add` refusal, not the ignore rules on their own —
# which is a different question wherever the index disagrees with a pattern.


def _ignore_rule(repo, *patterns: str) -> None:
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    gitignore.write_text(prefix + "".join(f"{p}\n" for p in patterns), encoding="utf-8")
    git(repo, "add", "--", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore rule")


def test_path_ignored_answers_gits_own_add_refusal(project):
    """The contract, stated as the two calls agreeing: whatever this says, `git add`
    does. An ignored untracked path is the one shape `add` refuses (rc 1) — and it
    refuses the whole operand list with it, which is the loss #577 is about."""
    repo = project.project
    _ignore_rule(repo, "board.yaml")
    (repo / "board.yaml").write_text("a: 1\n")

    assert verify.path_ignored(repo, repo / "board.yaml")
    assert not verify.path_ignored(repo, repo / "src.txt")
    # a path git has never seen, and one under no rule at all: both addable
    assert not verify.path_ignored(repo, repo / "never_existed.txt")
    with pytest.raises(verify.GitError):  # the refusal this predicate exists to avoid
        verify.commit_paths(repo, "would fail", [repo / "board.yaml"])


def test_path_ignored_is_false_for_a_tracked_path_under_a_matching_rule(project):
    """#392 seen from the other side: an ignore rule over a TRACKED regular file
    suppresses nothing, because git consults ignore rules only for untracked paths.
    `git add` stages it regardless, so a probe that read the RULE alone would drop a
    perfectly committable board from the commit list.

    `check-ignore` consults the index unless told not to, which is what makes one
    call answer both halves. Ablation: pass `--no-index` in `path_ignored` and this
    flips to True while the `commit_paths` assertion below still lands the commit —
    the probe and the action would then disagree."""
    repo = project.project
    _ignore_rule(repo, "board.yaml")
    (repo / "board.yaml").write_text("a: 1\n")
    git(repo, "add", "-f", "--", "board.yaml")
    git(repo, "commit", "-q", "-m", "force-track the board")

    assert verify.path_tracked(repo, "board.yaml")
    assert not verify.path_ignored(repo, repo / "board.yaml")
    # and git agrees: a modification to it commits like any other tracked file
    (repo / "board.yaml").write_text("a: 2\n")
    assert verify.commit_paths(repo, "tracked board moves", [repo / "board.yaml"])


def test_path_ignored_reads_a_metachar_name_literally(project):
    """A gitignore PATTERN is the wildmatch and the pathname is the literal, so `[`,
    `]`, `*` and `?` in the operand are inert here — unlike `path_tracked`, which
    needs `:(literal)` to survive them and which this function cannot use at all
    (`check-ignore` rejects pathspec magic outright).

    The collision is live: a `da/` rule DOES ignore `da/new.md`, and the question is
    whether asking about `d[a]/new.md` borrows that answer. It must not.

    Both operands have to be UNTRACKED for that question to be asked at all. This
    function consults the index, so a tracked pair reads not-ignored twice over for a
    reason that has nothing to do with globbing — the row would pass whether or not
    `d[a]` borrowed its neighbour's answer, and the rule would be inert against both.
    The `da/new.md` assertion is the positive control that proves it is not."""
    repo = project.project
    literal, victim = _metachar_pair(repo, "d[a]", "da")  # committed, glob-colliding
    _ignore_rule(repo, "da/")
    for parent in (literal.parent, victim.parent):
        (parent / "new.md").write_text("untracked\n")

    assert not verify.path_ignored(repo, literal.parent / "new.md")
    assert verify.path_ignored(repo, victim.parent / "new.md")  # the rule does reach
    # and the committed pair is the OTHER asymmetry: a rule over a tracked file is
    # inert, so both of those read not-ignored however the operand globs
    assert not verify.path_ignored(repo, literal)
    assert not verify.path_ignored(repo, victim)


# A board rel beginning with `:` is read as pathspec magic, and it fails two ways
# that redden APART — which is why these are two rows rather than one: the loud row's
# assertion never runs if the quiet row's has already failed the test. Exotic names,
# but `sprint_status` is resolved out of the operator's `_bmad/bmm/config.yaml` and
# nothing sanitizes it — the premise that makes `commit_paths` force every operand
# literal. Ablation for both: drop the `./` in `path_ignored`.


@RESERVED_IN_WINDOWS_FILENAMES
def test_path_ignored_disarms_supported_pathspec_magic(project):
    """The QUIET half, and the reason the `./` is not cosmetic. `:(top)` is magic git
    SUPPORTS, so it answers about what the magic denotes — plain `board.yaml`, which
    the rule here does ignore — and a bare operand reads rc 0 for a file no rule
    matches. No error, just the wrong answer, in the direction that drops a
    committable board from `confirm`'s operand list so its advance never commits."""
    repo = project.project
    _ignore_rule(repo, "board.yaml")
    (repo / "board.yaml").write_text("a: 1\n")
    (repo / ":(top)board.yaml").write_text("a: 1\n")

    assert verify.path_ignored(repo, repo / "board.yaml")  # the magic's TARGET
    assert not verify.path_ignored(repo, repo / ":(top)board.yaml")


@RESERVED_IN_WINDOWS_FILENAMES
def test_path_ignored_disarms_unsupported_pathspec_magic(project):
    """The LOUD half. `:(literal)` is magic `check-ignore` rejects outright — rc 128,
    so an un-prefixed operand raises `GitError` here instead of answering. The caller
    degrades to not-ignored, keeping a genuinely ignored board in the operand list,
    and `git add` refuses the whole list along with it: the #577 loss of the spec and
    park-record writes. Under the ablation this row ERRORS on the raise rather than
    failing an assertion, which is how it stays distinct from its sibling above."""
    repo = project.project
    _ignore_rule(repo, ":(literal)board.yaml")
    (repo / ":(literal)board.yaml").write_text("a: 1\n")

    assert verify.path_ignored(repo, repo / ":(literal)board.yaml")


def test_path_ignored_is_false_outside_the_repo(project, tmp_path):
    """Nothing to omit from a commit that will not contain it: `commit_paths` drops
    an out-of-repo path itself, so answering False here keeps the two agreeing."""
    assert not verify.path_ignored(project.project, tmp_path / "elsewhere" / "board.yaml")


@pytest.mark.parametrize("refused", ["repo", "candidate"])
def test_path_ignored_is_false_when_resolution_is_uncertain(project, monkeypatch, refused):
    """Resolution uncertainty keeps the path eligible for the exact commit and never
    fabricates a lexical git operand. Ablation target: move the repo resolve outside
    the guard or narrow it back to `ValueError`, and the matching row raises instead
    of returning False before the empty git-call assertion."""
    repo = project.project
    candidate = repo / "board.yaml"
    refuse_to_resolve(monkeypatch, repo if refused == "repo" else candidate)
    git_calls = []

    def spy_git(*args, **kwargs):
        git_calls.append((args, kwargs))

    monkeypatch.setattr(verify, "_run_git", spy_git)

    assert verify.path_ignored(repo, candidate) is False
    assert git_calls == []


def test_path_ignored_raises_on_git_failure(project):
    """Raises GitError like every other probe here. Its caller degrades by keeping
    the path IN the commit list — uncertainty must not silently drop a write.

    The path has to sit UNDER the bogus root: an out-of-repo one answers False
    without ever spawning git, which is the other branch's contract."""
    missing = project.project / "not-a-repo"
    with pytest.raises(verify.GitError, match="check-ignore"):
        verify.path_ignored(missing, missing / "src.txt")


def test_worktree_clean_ignores_stderr_chatter_on_success(project, monkeypatch):
    """A pristine tree must not read DIRTY because git wrote to stderr while
    exiting 0. Seven callers gate on this and `cli.py`'s three refuse the command
    outright, so the merged-stream read made a noisy git config unable to start a
    run — with no file named in the message."""
    real = verify._run_git

    def noisy(cmd, repo, **kw):
        proc = real(cmd, repo, **kw)
        if "status" in cmd:
            proc.stderr = "warning: core.fsyncMethod unknown value\n"
        return proc

    monkeypatch.setattr(verify, "_run_git", noisy)
    assert verify.worktree_clean(project.project)
    (project.project / "stray.txt").write_text("real change\n")
    assert not verify.worktree_clean(project.project)  # a genuine change still shows


# ------------------------------------------ probes that return git's text (#442)
#
# The rest of the family #441 documented and did not widen to. A real git config
# does the reddening here rather than a synthetic stderr, so the row stands on
# git's own behavior: `make_git_noisy` sets an unknown VALUE for a known KEY,
# which warns on stderr at rc 0 — the normal path on a host whose git config the
# orchestrator does not control.


def test_rev_parse_head_reads_stdout_alone_under_host_noise(project):
    """A warning-suffixed "sha" is not a sha. It reaches every commit comparison
    and the baselines persisted in run state, so a resume grades a warning-carrying
    string against a clean one and reads "moved" — silent, with a plausible-looking
    value.

    The third assertion is not implied by the first: equality alone would also hold
    if the conftest oracle were corrupted the same way, and it reads stdout alone.

    Ablation target: put `rev_parse_head` back on `_git` (the stdout+stderr merge)
    and this fails alone, on a return carrying the warning — while
    `test_last_commit_for_reads_stdout_alone_under_host_noise` and
    `test_current_branch_reads_stdout_alone_under_host_noise` stay green, since each
    site is converted separately."""
    repo = project.project
    warning = make_git_noisy(repo)

    head = verify.rev_parse_head(repo)

    assert head == git(repo, "rev-parse", "HEAD")  # conftest git() = stdout.strip()
    assert len(head) == 40 and all(c in "0123456789abcdef" for c in head)
    assert warning not in head


def test_last_commit_for_reads_stdout_alone_under_host_noise(project):
    """Same shape as its `rev-parse` sibling, one caller further out: this sha backs
    an operator-park record's provenance (operatoractions.py), which is written into
    the very commit it rides and so cannot be re-derived later.

    Ablation target: put `last_commit_for` back on `_git` and this fails alone."""
    repo = project.project
    warning = make_git_noisy(repo)

    sha = verify.last_commit_for(repo, repo / "src.txt")

    assert sha == git(repo, "log", "-n", "1", "--format=%H", "--", "src.txt")
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    assert warning not in sha


def test_rev_parse_head_failure_still_carries_git_stderr(tmp_path):
    """The other direction of the split: reading stdout alone for the VALUE must not
    cost the ERROR path its diagnostic. git puts "not a git repository" on stderr, so
    a message built from stdout alone would name the directory and say nothing about
    why — which is the whole content of this failure.

    Ablation target: in `rev_parse_head`, swap the raise's `detail` back to `out`
    (leaving `_git_out` and the `return out` in place) and this fails alone, on an
    empty message — while `test_rev_parse_head_reads_stdout_alone_under_host_noise`
    stays green. That disjointness is what proves this is not a restatement of it."""
    with pytest.raises(verify.GitError) as excinfo:
        verify.rev_parse_head(tmp_path)

    assert "not a git repository" in str(excinfo.value)


def test_untracked_files_reads_stdout_alone_under_host_noise(project):
    """REAL-GIT axis: the reddening comes from git's own rc-0 stderr
    (`make_git_noisy`), not a synthetic line. Against `_git`'s merge that warning
    splits off as a phantom untracked path on a PRISTINE tree, and this probe's
    contract is what a plain `git clean -fd` would remove — so the phantom is a
    ROLLBACK CANDIDATE. Silent, on every host whose git config warns.

    Both halves matter: the empty set proves no phantom, and the second proves the
    fix did not simply blank the probe.

    Ablation target: put `untracked_files` back on `_git` (the merge) and this fails
    alone, on a set carrying the warning — the four sibling #442 rows added with it
    (`commits_above`, `prune_preserve_refs`, `worktree_list`, `capture_diff`) stay
    green, since each site is converted separately."""
    repo = project.project
    make_git_noisy(repo)

    assert verify.untracked_files(repo) == set()  # pristine tree, no phantom

    (repo / "new.txt").write_text("real untracked\n")
    assert verify.untracked_files(repo) == {"new.txt"}  # and it still sees real ones


def test_commits_above_reads_stdout_alone_under_host_noise(project):
    """REAL-GIT axis, same `make_git_noisy` shape. Against the merge the warning is
    a phantom SHA, handed to `preserve_commits` as an attempt commit — and the
    docstring's "Empty when HEAD is at or behind baseline" stops holding. Silent.

    `rev_parse_head` is the oracle here because it already reads stdout alone
    (converted with the same helper), so the baseline it hands back is clean.

    Ablation target: put `commits_above` back on `_git` and this fails alone, on
    `["warning: ignoring unknown core.fsyncMethod value …"]` where `[]` was
    expected."""
    repo = project.project
    make_git_noisy(repo)
    baseline = verify.rev_parse_head(repo)

    assert verify.commits_above(repo, baseline) == []  # HEAD at baseline

    (repo / "impl.txt").write_text("work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")

    assert verify.commits_above(repo, baseline) == [verify.rev_parse_head(repo)]


def _inject_git_stderr(monkeypatch, line: str) -> None:
    """Prepend `line` to the stderr of every git spawn verify.py makes, leaving the
    return code and stdout untouched — the house `monkeypatch.setattr(verify.subprocess,
    "run", …)` seam (see `test_capture_diff_timeout_becomes_git_error`), wrapping the
    real `subprocess.run` rather than scripting it.

    A synthetic shape is used only where the real `core.fsyncMethod` warning CANNOT
    redden the row; each caller's docstring says why. Bytes and `None` stderr pass
    through untouched, so a `binary=True` spawn is not corrupted into a type error."""
    real_run = verify.subprocess.run

    def noisy_run(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if not isinstance(proc.stderr, str):
            return proc
        return verify.subprocess.CompletedProcess(
            proc.args, proc.returncode, proc.stdout, line + proc.stderr
        )

    monkeypatch.setattr(verify.subprocess, "run", noisy_run)


def test_capture_diff_untracked_leg_reads_stdout_alone(project, monkeypatch):
    """SEAM axis, deliberately — the real warning cannot redden this row, and a test
    that cannot redden is not evidence. Measured at git 2.55.0: the phantom rel that
    the `core.fsyncMethod` warning splits off reaches
    `git diff --no-index -- /dev/null "<phantom>"`, which exits **1** ("could not
    access") with EMPTY stdout — and rc 1 is exactly the code this caller already
    tolerates as "the files differ", so nothing is appended and the patch is
    unchanged. #442's claim that the phantom "becomes a file that cannot be opened"
    and corrupts the patch does not hold for that shape.

    The injected line is instead the name of a file that IS tracked and unmodified,
    so it can never legitimately appear in `ls-files --others` output. Pre-fix that
    rel is real on disk, `diff --no-index` succeeds against it, and the patch gains
    an add-from-empty `new file mode` hunk for an already-tracked file.

    Ablation target: put the untracked leg back on `_git` (the merge) and this fails
    alone, on a patch carrying that hunk for `src.txt` where `""` was expected."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    _inject_git_stderr(monkeypatch, "src.txt\n")

    assert verify.capture_diff(repo, baseline) == ""


def test_commit_paths_refuses_to_stage_a_glob_neighbour(project):
    """`commit_paths` promises "exactly `paths` (and nothing else)". Unescaped, a
    configured artifacts dir carrying `[` also stages the operator's unrelated
    edit in the colliding sibling — committed under a story's name."""
    repo = project.project
    target, victim = _metachar_pair(repo, "docs[a]", "docsa")
    target.write_text("the artifact this commit is for\n")
    victim.write_text("the operator's own uncommitted work\n")

    sha = verify.commit_paths(repo, "chore: artifact only", [target])

    assert sha is not None
    status = git(repo, "status", "--porcelain")
    assert "docsa/f.md" in status  # the neighbour is STILL uncommitted
    assert "docs[a]" not in status  # the named path did land
    assert victim.read_text() == "the operator's own uncommitted work\n"


def test_commit_paths_leaves_a_record_for_a_gitignored_path(project):
    """With the ledger gitignored — the shape `_carry_harvested_deferrals` hits —
    `git add` REFUSES an explicitly named ignored path (rc 1) but SKIPS a globbed
    one. Bare, the call could exit rc 0 having staged nothing and report success
    having committed no ledger; the literal operand raises instead, so the caller
    still has a record."""
    repo = project.project
    gitignore = repo / ".gitignore"
    gitignore.write_text(gitignore.read_text() + "ignored-dir/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "ignored-dir").mkdir(exist_ok=True)
    ledger = repo / "ignored-dir" / "deferred-work.md"
    ledger.write_text("# ledger\n")

    with pytest.raises(verify.GitError, match="git add failed"):
        verify.commit_paths(repo, "chore: ledger", [ledger])


def test_exclude_specs_does_not_hide_a_siblings_diff(project):
    """An artifacts dir whose name carries `*` must not also exclude the sibling
    tree it glob-matches: `has_changes_since` would answer "no changes" for a dev
    attempt that changed real code, the false-green direction (#423 item 3)."""
    repo = project.project
    _metachar_pair(repo, "doc-x", "doc-y")  # committed, both under the `doc*` glob
    baseline = verify.rev_parse_head(repo)
    (repo / "doc-y" / "f.md").write_text("a real change outside the artifacts dir\n")

    assert verify.has_changes_since(repo, baseline, exclude=("doc*",))


def test_exclude_specs_agrees_with_path_under_any(project):
    """The two halves of ONE `has_changes_since` answer — the git exclusion over
    tracked changes and `_path_under_any` over untracked ones — must not disagree
    about what "under the artifact dir" means (#423 item 4)."""
    for prefix, path in (("docs[a]", "docsa"), ("q?r", "qxr"), ("doc*", "docsa/f.md")):
        git_excludes = verify._exclude_specs((prefix,)) == [f":(exclude,literal){prefix}"]
        python_under = verify._path_under_any(path, (prefix,))
        assert git_excludes and not python_under, f"{prefix} vs {path}"


def test_safe_rollback_preserve_does_not_restore_a_glob_neighbour(project):
    """`preserve` restores operator-configured dirs from the snapshot, and this leg
    WRITES. Bare, `checkout <snap> -- 'doc*'` also restores every sibling the glob
    reaches — and because the snapshot is a `stash create` of the DIRTY tree, that
    hands the dev attempt's change back after the reset was supposed to discard it.
    The rollback silently under-reverts, and the next preflight sees a tree the
    engine believes it rolled back (#423 item 5)."""
    repo = project.project
    # Only the NEIGHBOUR exists on disk; `doc*` is the operator's configured
    # preserve dir, which the literal reading matches nothing for (a benign
    # "pathspec did not match"). Keeping it absent is what makes the assertion
    # discriminating AND lets the row run on Windows, where `*` cannot be a
    # filename: bare, the glob reaches `docsa` and hands its change back.
    (repo / "docsa").mkdir(parents=True, exist_ok=True)
    neighbour = repo / "docsa" / "f.md"
    neighbour.write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "neighbour")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    neighbour.write_text("the dev attempt's change\n")  # outside preserve — must revert

    verify.safe_rollback(
        repo, baseline, baseline_untracked=snap, keep=(".bmad-loop",), preserve=("doc*",)
    )

    assert neighbour.read_text() == "v1\n"  # reverted, not resurrected by the glob


def test_commit_story(project):
    task = make_task(project)
    (project.project / "src.txt").write_text("done work\n")
    sha = verify.commit_story(project.project, f"story {task.story_key}: via bmad-loop")
    assert sha != task.baseline_commit
    assert verify.worktree_clean(project.project)


def test_finalize_commit_squashes_chain_to_one(project):
    """The skill commits each iteration; finalize_commit collapses the whole
    chain since baseline (plus the orchestrator's uncommitted bookkeeping) into
    ONE commit carrying the orchestrator's message."""
    baseline = verify.rev_parse_head(project.project)
    # two "skill" commits since baseline (a dev pass + a review pass)
    (project.project / "src.txt").write_text("dev work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "skill: implement")
    (project.project / "src.txt").write_text("dev work\nreview fix\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "skill: review fix")
    # an uncommitted orchestrator bookkeeping write (e.g. sprint-status)
    (project.project / "sprint.txt").write_text("done\n")

    sha = verify.finalize_commit(project.project, baseline, "story 1-1-a: via bmad-loop")

    assert sha is not None and sha != baseline
    assert verify.worktree_clean(project.project)
    # exactly one commit on top of baseline, with the orchestrator's message
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert log.splitlines() == ["story 1-1-a: via bmad-loop"]
    # all the content (skill commits + bookkeeping) is in that single commit
    assert (project.project / "src.txt").read_text() == "dev work\nreview fix\n"
    assert (project.project / "sprint.txt").read_text() == "done\n"


def test_finalize_commit_restores_head_when_commit_fails(project):
    """If `git commit` fails after the soft reset (e.g. a rejecting pre-commit hook),
    HEAD must be restored to the skill commit chain — not left rewound to baseline
    with the chain dropped from the branch pointer."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("dev work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "skill: implement")
    head_before = verify.rev_parse_head(project.project)
    # a pre-commit hook that always fails makes finalize's commit step fail
    hook = project.project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    with pytest.raises(verify.GitError, match="git commit failed"):
        verify.finalize_commit(project.project, baseline, "story: via bmad-loop")

    assert verify.rev_parse_head(project.project) == head_before  # chain preserved


def test_finalize_commit_no_vcs_or_missing_baseline_returns_none(project):
    assert verify.finalize_commit(project.project, None, "msg") is None
    assert verify.finalize_commit(project.project, "NO_VCS", "msg") is None


def test_finalize_commit_nothing_to_finalize_returns_none(project):
    """Tree already equals baseline (no skill commits, no bookkeeping delta)."""
    baseline = verify.rev_parse_head(project.project)
    assert verify.finalize_commit(project.project, baseline, "msg") is None
    assert verify.rev_parse_head(project.project) == baseline


def test_finalize_commit_only_uncommitted_bookkeeping(project):
    """No skill commits, just the orchestrator's uncommitted writes → one commit."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("uncommitted change\n")

    sha = verify.finalize_commit(project.project, baseline, "story: via bmad-loop")

    assert sha is not None and sha != baseline
    assert verify.worktree_clean(project.project)
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert log.splitlines() == ["story: via bmad-loop"]


def test_finalize_commit_rerun_is_content_idempotent(project):
    """The #115 resume re-drive may run finalize_commit on a post-squash tree
    (the first finalize completed just before a host death). The re-run must
    converge on exactly ONE commit above baseline with an identical tree —
    never stack a second squash or raise. (No sha1 != sha2 assertion: same
    tree/parent/message within one second re-mints the same sha.)"""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("dev work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "skill: implement")

    sha1 = verify.finalize_commit(project.project, baseline, "story 1-1-a: via bmad-loop")
    sha2 = verify.finalize_commit(project.project, baseline, "story 1-1-a: via bmad-loop")

    assert sha1 is not None and sha2 is not None
    tree1 = git(project.project, "rev-parse", f"{sha1}^{{tree}}")
    tree2 = git(project.project, "rev-parse", f"{sha2}^{{tree}}")
    assert tree1 == tree2
    assert verify.worktree_clean(project.project)
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert log.splitlines() == ["story 1-1-a: via bmad-loop"]


def test_commit_paths_commits_only_listed(project):
    base = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("ledger-ish edit\n")  # the "tracked" target
    (project.project / "other.txt").write_text("unrelated work\n")  # must be left alone

    sha = verify.commit_paths(project.project, "chore: targeted", [project.project / "src.txt"])
    assert sha is not None and sha != base
    # only src.txt landed in the commit; other.txt is still uncommitted
    status = git(project.project, "status", "--porcelain")
    assert "other.txt" in status
    assert "src.txt" not in status


def test_commit_paths_repo_resolution_failure_precedes_staging(project, monkeypatch):
    """An uncertain repository root is a typed exact-write failure before any
    candidate can be staged; it is never converted into an empty successful commit.

    Ablation target: remove the repo-root translation guard and this test fails on
    the raw OSError instead of `GitError`; replace it with a lexical/empty fallback
    and the no-staging/no-success assertions fail.
    """
    repo = project.project
    target = repo / "src.txt"
    target.write_text("exact write\n")
    refuse_to_resolve(monkeypatch, repo)
    git_calls: list[tuple[str, ...]] = []
    real_git = verify._git

    def spy_git(r, *args):
        git_calls.append(args)
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", spy_git)

    with pytest.raises(verify.GitError, match="repository root for exact commit") as caught:
        verify.commit_paths(repo, "chore: exact", [target])

    assert isinstance(caught.value.__cause__, OSError)
    assert not any(args[:1] == ("add",) for args in git_calls)
    assert target.read_text() == "exact write\n"


def test_commit_paths_omits_only_an_uncertain_candidate(project, monkeypatch):
    """Per-path uncertainty drops that candidate while a healthy sibling retains
    its input order and is the only path staged and committed.

    Ablation target: narrow the candidate guard back to `ValueError` and this test
    fails on the injected OSError before the healthy sibling can be committed.
    """
    repo = project.project
    uncertain = repo / "src.txt"
    uncertain.write_text("operator edit\n")
    healthy = repo / "healthy.txt"
    healthy.write_text("commit me\n")
    refuse_to_resolve(monkeypatch, uncertain)

    sha = verify.commit_paths(repo, "chore: healthy only", [uncertain, healthy])

    assert sha is not None
    assert git(repo, "show", "--format=", "--name-only", sha).splitlines() == ["healthy.txt"]
    status = git(repo, "status", "--porcelain")
    assert "src.txt" in status  # uncertain path stayed uncommitted
    assert "healthy.txt" not in status


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_commit_paths_raises_when_no_operand_survives_resolution(project, monkeypatch, error_type):
    """A sole uncertain candidate is a typed exact-write failure, not a no-op.

    The harvested-deferral carry clears its durable commit-pending latch after a
    successful return, so returning ``None`` here would permanently suppress the
    retry even though the ledger could still be dirty. Ablation target: remove
    the no-operands resolution guard and both rows return ``None`` instead of
    raising before staging.
    """
    repo = project.project
    uncertain = repo / "src.txt"
    uncertain.write_text("uncommitted exact write\n")
    _refuse_resolution_as(monkeypatch, uncertain, error_type)
    git_calls: list[tuple[str, ...]] = []
    real_git = verify._git

    def spy_git(r, *args):
        git_calls.append(args)
        return real_git(r, *args)

    monkeypatch.setattr(verify, "_git", spy_git)

    with pytest.raises(verify.GitError, match="no exact commit operand remains") as caught:
        verify.commit_paths(repo, "chore: exact", [uncertain])

    assert isinstance(caught.value.__cause__, error_type)
    assert not any(args[:1] == ("add",) for args in git_calls)
    assert uncertain.read_text() == "uncommitted exact write\n"


def test_commit_paths_noop_when_unchanged(project):
    assert verify.commit_paths(project.project, "noop", [project.project / "src.txt"]) is None
    # a path outside the repo is ignored, not an error
    assert verify.commit_paths(project.project, "noop", [project.project.parent / "x"]) is None


def test_commit_paths_noops_on_unchanged_paths_under_host_noise(project):
    """REAL-GIT axis (#442), the same no-op contract as its sibling above, on a host
    whose git warns. `status --porcelain` exits 0 while writing that warning to
    stderr, so against `_git`'s merge an UNCHANGED path set reads NON-EMPTY: the
    `if not out: return None` early-out is skipped and `git commit` runs with nothing
    staged, exiting 1. The function then raised
    `GitError("git commit failed: … nothing to commit …")` precisely where its
    contract promises None — and every caller committing an optional artifact (an
    operator park record, a ledger carry) took that raise.

    The HEAD assertion is the second half: a fix that returned None while still
    committing would satisfy the first alone.

    Ablation target: put the `status` read back on `_git` (the merge) and this fails
    alone, on that GitError."""
    repo = project.project
    make_git_noisy(repo)
    head = verify.rev_parse_head(repo)

    assert verify.commit_paths(repo, "noop", [repo / "src.txt"]) is None

    assert verify.rev_parse_head(repo) == head  # and nothing was committed


def test_apply_patch_replays_saved_diff(project):
    """A patch saved off the baseline re-applies cleanly onto that same baseline —
    tracked edits AND new (untracked) files — reproducing the reverted attempt."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    # the "attempt": edit a tracked file + add a new file, then capture the diff
    (repo / "src.txt").write_text("original\nattempted change\n")
    (repo / "new_module.py").write_text("print('hi')\n")
    patch = project.implementation_artifacts / "attempt.patch"
    # git diff HEAD includes new files with --binary-safe text; -N stages intent so
    # untracked files appear in the diff (mirrors how the skill saves the attempt)
    git(repo, "add", "-N", "new_module.py")
    patch.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")
    # revert the attempt back to baseline (as the skill does before halting)
    git(repo, "reset", "-q", "--hard", baseline)
    (repo / "new_module.py").unlink(missing_ok=True)
    assert (repo / "src.txt").read_text() == "original\n"

    verify.apply_patch(repo, patch)

    assert (repo / "src.txt").read_text() == "original\nattempted change\n"
    assert (repo / "new_module.py").read_text() == "print('hi')\n"


def test_apply_patch_missing_file_raises(project):
    with pytest.raises(verify.GitError, match="restore patch not found"):
        verify.apply_patch(project.project, project.implementation_artifacts / "nope.patch")


def test_apply_patch_conflict_raises(project):
    """A patch that does not apply against the current tree raises GitError with
    git's output — the caller escalates rather than dispatch onto a broken tree."""
    repo = project.project
    patch = project.implementation_artifacts / "bad.patch"
    # a diff against content the tree does not have
    patch.write_text(
        "--- a/src.txt\n+++ b/src.txt\n@@ -1 +1 @@\n-something-else\n+patched\n",
        encoding="utf-8",
    )
    with pytest.raises(verify.GitError, match="git apply"):
        verify.apply_patch(repo, patch)


def _saved_patch(project, name="attempt.patch"):
    """Capture the working tree as a restore patch, the way the skill saves one."""
    repo = project.project
    git(repo, "add", "-AN")  # intent-to-add so new files appear in `git diff HEAD`
    patch = project.implementation_artifacts / name
    patch.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")
    return patch


def test_patch_new_files_names_created_files(project):
    repo = project.project
    (repo / "new_module.py").write_text("print('hi')\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "deep.txt").write_text("nested\n")
    assert verify.patch_new_files(_saved_patch(project)) == {"new_module.py", "pkg/deep.txt"}


def test_patch_new_files_ignores_modifications(project):
    repo = project.project
    (repo / "src.txt").write_text("original\nedited\n")
    assert verify.patch_new_files(_saved_patch(project)) == set()


def test_patch_new_files_never_returns_deletions(project):
    """A deleted file must not land in the exclusion set: the human may re-create
    that path, and excluding it would make the next rollback delete their copy."""
    repo = project.project
    git(repo, "rm", "-q", "src.txt")
    assert verify.patch_new_files(_saved_patch(project)) == set()


def test_patch_new_files_multi_file_patch(project):
    """One patch carrying a creation, a modification and a deletion — only the
    creation comes back."""
    repo = project.project
    (repo / "created.txt").write_text("new\n")
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "rm", "-q", "src.txt")
    assert verify.patch_new_files(_saved_patch(project)) == {"created.txt"}


def test_patch_new_files_ignores_header_lookalikes_in_hunk_bodies(project):
    """Hunk *content* that reads like a file header (a removed `-- /dev/null` line
    renders as `--- /dev/null`) must not be mistaken for a creation."""
    patch = project.implementation_artifacts / "tricky.patch"
    patch.write_text(
        "diff --git a/real.txt b/real.txt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/real.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
        "diff --git a/mod.txt b/mod.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/mod.txt\n"
        "+++ b/mod.txt\n"
        "@@ -1 +1 @@\n"
        "--- /dev/null\n"  # a removed line whose content is `-- /dev/null`
        "+++ b/evil.txt\n"  # an added line whose content is `++ b/evil.txt`
        "\\ No newline at end of file\n",
        encoding="utf-8",
    )
    assert verify.patch_new_files(patch) == {"real.txt"}


def test_patch_new_files_skips_quoted_paths(project):
    """core.quotePath output is skipped rather than guessed — under-reporting is
    safe, a wrong path would get a user file deleted."""
    patch = project.implementation_artifacts / "quoted.patch"
    patch.write_text(
        'diff --git "a/w\\303\\251ird.txt" "b/w\\303\\251ird.txt"\n'
        "new file mode 100644\n"
        "--- /dev/null\n"
        '+++ "b/w\\303\\251ird.txt"\n'
        "@@ -0,0 +1 @@\n"
        "+x\n",
        encoding="utf-8",
    )
    assert verify.patch_new_files(patch) == set()


def test_patch_new_files_strips_mnemonic_prefixes(project):
    """`diff.mnemonicPrefix=true` in the user's config makes `git diff HEAD` emit
    `c/`/`w/` instead of `a/`/`b/`. `apply_patch`'s plain `git apply` (-p1) strips
    the first component whatever it is, so the parser must mirror that or the
    recorded residue is `w/<path>` and the exclusion silently no-ops."""
    repo = project.project
    (repo / "new_module.py").write_text("print('hi')\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "deep.txt").write_text("nested\n")
    git(repo, "add", "-AN")
    patch = project.implementation_artifacts / "mnemonic.patch"
    patch.write_text(
        git(repo, "-c", "diff.mnemonicPrefix=true", "diff", "HEAD") + "\n", encoding="utf-8"
    )
    assert "+++ w/new_module.py" in patch.read_text(encoding="utf-8")  # fixture sanity
    assert verify.patch_new_files(patch) == {"new_module.py", "pkg/deep.txt"}


def test_patch_new_files_skips_unstrippable_targets(project):
    """A prefixless single-component target (`git diff --no-prefix`) cannot survive
    `git apply`'s -p1 strip — the apply would have failed, so no residue can exist.
    Recording it verbatim could exclude (and later delete) a same-named file the
    human created; skip it instead."""
    patch = project.implementation_artifacts / "noprefix.patch"
    patch.write_text(
        "diff --git newfile.txt newfile.txt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ newfile.txt\n"
        "@@ -0,0 +1 @@\n"
        "+x\n",
        encoding="utf-8",
    )
    assert verify.patch_new_files(patch) == set()


def test_patch_new_files_missing_patch_raises_oserror(project):
    """The caller (rearm) turns this into a journaled best-effort degrade."""
    with pytest.raises(OSError):
        verify.patch_new_files(project.implementation_artifacts / "gone.patch")


def test_read_frontmatter_tolerates_garbage(project):
    p = project.project / "x.md"
    p.write_text("no frontmatter here")
    assert verify.read_frontmatter(p) == {}
    p.write_text("---\n: : :\nbroken yaml [\n---\nbody")
    assert verify.read_frontmatter(p) == {}


def test_read_frontmatter_tolerates_non_utf8(project):
    """UnicodeDecodeError is a ValueError, so it slipped past every caller's
    except-OSError guard. An undecodable file now degrades exactly like
    unparseable YAML — {} → status "" → the status gates return a clean retry
    instead of crashing verify (stories dev/review gates and the pre-existing
    sprint/bundle paths alike)."""
    p = project.project / "x.md"
    p.write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")
    assert verify.read_frontmatter(p) == {}


def test_read_frontmatter_oserror_still_raises(project, monkeypatch):
    """`read_frontmatter` degrades a *decode* fault to {} but must keep RAISING on
    OSError. Repair callers (`reset_spec_status`, `mark_done`) read through it and
    depend on the raise: silently skipping a rewrite leaves the spec in a state the
    caller believes it fixed. The observation callers wrap it instead —
    `verify._gate_frontmatter`, `Engine._observed_frontmatter`,
    `devcontract.synthesize_result`. Widening the except here would erase that
    distinction everywhere at once."""
    p = project.project / "x.md"
    p.write_text("---\nstatus: done\n---\n")
    fault_read_text(monkeypatch, p)
    with pytest.raises(PermissionError):
        verify.read_frontmatter(p)


def test_read_frontmatter_ignores_triple_dash_in_value(project):
    """A `---` inside a scalar value must NOT be read as the closing delimiter.
    A plain `split("---", 2)` truncates the block at the first substring match →
    YAMLError → {}, silently zeroing status; the closing boundary is a standalone
    `---` line only."""
    p = project.project / "x.md"
    p.write_text("---\ntitle: 'restore --- review'\nstatus: done\n---\nbody\n")
    fm = verify.read_frontmatter(p)
    assert fm["status"] == "done"
    assert fm["title"] == "restore --- review"


# ------------------------------------------- repo_root override (divergent roots)
#
# `isolation = "none"` plus a `repo_root:` key in _bmad/bmm/config.yaml is the ONE
# supported shape where `paths.project` and `paths.repo_root` name different
# directories (`bmadconfig.worktree_isolation_conflict` refuses the other). The
# `project` fixture sets no override, so `repo_root == project` and no pre-existing
# row here can tell the two apart — which is why the wrong-root bug survived.


def _repo_root_override(project, tmp_path):
    """ProjectPaths for the override: BMAD artifacts under a `project` directory
    that is not a checkout, code + git under a separate `repo_root`.

    The session's cwd under this config IS `repo_root` (`Workspace.default` sets
    `root = paths.repo_root`), so the dev writer already stamps its baseline there.
    Only the readers were anchored on `project`."""
    art = tmp_path / "artifacts-root"
    impl = art / "_bmad-output" / "implementation-artifacts"
    plan = art / "_bmad-output" / "planning-artifacts"
    impl.mkdir(parents=True)
    plan.mkdir(parents=True)
    return dataclasses.replace(
        project,
        project=art,
        implementation_artifacts=impl,
        planning_artifacts=plan,
        output_folder=art / "_bmad-output",
        repo_root=project.project,
    )


def test_verify_dev_measures_proof_of_work_in_the_code_tree(project, tmp_path):
    """The baseline is written in `repo_root` (by `Engine._dev_phase`, off
    `workspace.root`) and must be READ there too. Anchored on `paths.project` the
    gate asked a directory that is not the code checkout about a commit only the
    code checkout has.

    Ablation: put the canonical-oid probe back on `paths.project` and this reddens
    with "does not match" — `artifacts-root` is not a repo, so the claimed commit
    cannot be resolved there.

    The proof-of-work probe deliberately is NOT graded by this row, and cannot be:
    the gate arm refuses only on a positive "nothing changed" (`is False` over
    `_changes_since`'s tri-state), so pointing the probe at a non-repo yields the
    unanswerable `None`, the gate PASSES, and a green row proves nothing. Measured:
    re-anchor `proof_of_work_probe` on `paths.project` and this row stays green
    while its sibling below reddens. That sibling is what grades the probe, which
    is why it asserts the exact reason rather than `not out.ok`.
    """
    paths = _repo_root_override(project, tmp_path)
    write_sprint(paths, {"1-1-a": "review"})
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    # the session's work lands where the session's cwd is: the CODE tree
    (paths.repo_root / "src.txt").write_text("real work\n")

    out = verify.verify_dev(task, paths, dev_result(sp))
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_refuses_proof_of_work_only_the_project_tree_holds(project, tmp_path):
    """The other half of the same anchor: residue under `project` is not evidence
    that anything was implemented, because no session writes code there.

    The assertion is on the exact refusal REASON, not merely on `not out.ok` — but
    NOT for the reason a reader might assume. Re-anchoring the probe on
    `paths.project` does not make the gate fault: pointing `_changes_since` at a
    directory that is not a git repository yields its unanswerable `None`, which the
    gate arm folds toward "there are changes" (`is False` is the only refusal), so
    the gate PASSES. The exact-reason assertion is still the right call, for the
    neighbouring row's sake — that one cannot grade this probe at all, precisely
    because the fail-open answer is also the answer a correct run gives.

    Ablation names the surface actually reached, not `has_changes_since`: that
    wrapper has no production caller left, so substituting it would prove nothing.
    Change `proof_of_work_probe`'s first argument from `paths.repo_root` to
    `paths.project` (verify.py, in `_verify_shared_gates`) and this row reddens,
    measured, on `assert not out.ok` with `ok=True` — the neighbouring row staying
    green in the same run.
    """
    paths = _repo_root_override(project, tmp_path)
    write_sprint(paths, {"1-1-a": "review"})
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    # a marker ONLY the project tree holds; the code tree is untouched
    (paths.project / "marker.txt").write_text("not implementation work\n")

    out = verify.verify_dev(task, paths, dev_result(sp))
    assert not out.ok
    assert out.reason == "no changes in worktree since baseline commit"


def test_verify_dev_exclude_relpaths_follows_the_root_it_is_given(project, tmp_path):
    """The pathspecs handed to git must be relative to the root git is invoked
    against. A relpath computed against the other root does not raise — git simply
    matches nothing — so the exclusion vanishes silently, which is why this is
    pinned rather than left to the gate's behavior.

    Ablation: make the helper ignore `root` (pin `base = paths.project`) and the
    second half reddens — the board and spec come back as `project`-relative entries
    that name nothing inside the code tree, which is the silent-no-op shape.
    """
    paths = _repo_root_override(project, tmp_path)
    sp = spec_path(paths, "1-1-a")
    sp.write_text("---\nstatus: in-review\n---\n", encoding="utf-8")

    # project-rooted (the default): the board and the spec are both inside it
    default = verify.verify_dev_exclude_relpaths(paths, sp, root=paths.project)
    assert any(r.endswith("sprint-status.yaml") for r in default)
    assert any(r.endswith("spec-1-1-a.md") for r in default)
    # code-tree-rooted: neither artifact lives there, so there is nothing to exclude
    assert verify.verify_dev_exclude_relpaths(paths, sp, root=paths.repo_root) == ()


def test_verify_dev_accepts_a_newer_reachable_claim_in_the_code_tree(project, tmp_path):
    """The `newer_ok` leg asks its ancestry question in the CODE tree.

    An intervening commit before step-03 stamps `baseline_revision` makes the claim a
    descendant of the recorded baseline. That reachability is a fact about the
    checkout the code lives in; asked of `paths.project` — which under this override
    is not a repository at all — `commit_reachable_above_baseline` reads the Git
    failure as False and a correct attempt is refused forever. That is the
    burn-every-attempt shape #716 exists to close, and no pre-existing row could see
    it: every other ancestry row runs on the `project` fixture where the two roots
    are the same object, and both divergent-root rows above claim a baseline EQUAL to
    the recorded one, so neither enters this branch.

    Adopting a newer claim sets `include_untracked_proof = False`, so the proof here
    is a TRACKED modification rather than a new untracked file.

    Ablation: re-anchor `commit_reachable_above_baseline` on `paths.project` and this
    reddens with "does not match".
    """
    paths = _repo_root_override(project, tmp_path)
    write_sprint(paths, {"1-1-a": "review"})
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)

    # the session commits inside the unit before step-03 stamps its baseline
    (paths.repo_root / "prior.txt").write_text("prior work\n")
    git(paths.repo_root, "add", "-A")
    git(paths.repo_root, "commit", "-q", "-m", "intervening commit in the code tree")
    descendant = verify.rev_parse_head(paths.repo_root)

    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "in-review", descendant)
    (paths.repo_root / "src.txt").write_text("real work\n")

    out = verify.verify_dev(task, paths, dev_result(sp))
    assert out.ok


def test_verify_dev_bundle_accepts_an_ancestor_claim_in_the_code_tree(project, tmp_path):
    """The `older_ok` leg — #161's bundle-only relaxation — asks the same question in
    the same tree.

    `allow_ancestor_baseline` is set at exactly one site (`verify_dev_bundle`), so
    this is the only route into `is_ancestor`. Anchored on `paths.project` the call
    lands on a non-repository, `is_ancestor` reads any Git failure as False, and a
    legitimate bundle adopting a story's older spec baseline is refused.

    Ablation: re-anchor `is_ancestor` on `paths.project` and this reddens with
    "does not match".
    """
    paths = _repo_root_override(project, tmp_path)
    ancestor = verify.rev_parse_head(paths.repo_root)

    # the unit worktree is cut after the story's own baseline
    (paths.repo_root / "prior.txt").write_text("unit history\n")
    git(paths.repo_root, "add", "-A")
    git(paths.repo_root, "commit", "-q", "-m", "unit worktree cut")

    task = StoryTask(story_key="dw-test-bundle", epic=0)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    sp = paths.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", ancestor)
    (paths.repo_root / "src.txt").write_text("real work\n")

    out = verify.verify_dev_bundle(task, paths, dev_result(sp))
    assert out.ok


def test_verify_dev_exclude_relpaths_anchors_the_restore_patch_on_the_root(project, tmp_path):
    """The latched `restore_patch` follows `root` like every other candidate.

    `base` feeds TWO steps — `resolve_restore_path`'s join and the `relative_to`
    that follows — and only a RELATIVE latch can tell them apart. Joined under
    `project` but measured against `repo_root`, it is not relative to that root at
    all, raises `ValueError`, and drops out of the exclude set silently. A missing
    exclusion does not raise — it just stops excluding, letting a restore re-drive
    whose session produced nothing pass proof-of-work on the patch file's mere
    presence, the hazard this function's own docstring names.

    An ABSOLUTE latch deliberately is not the probe, even though it is what
    `cli._resolve_restore_patch` stores: `resolve_restore_path` ignores its base for
    an absolute input, so both halves agree however the join is anchored and the row
    would pass with the anchor ablated.

    The existing root row passes no `restore_patch`, and both restore-patch rows run
    on the `project` fixture where the roots are the same object, so this line was
    unpinned in both directions.

    Ablation: pin the `resolve_restore_path` base to `paths.project` and the first
    assertion reddens — the latch is no longer relative to the code tree and drops.
    """
    paths = _repo_root_override(project, tmp_path)
    sp = spec_path(paths, "1-1-a")
    sp.write_text("---\nstatus: in-review\n---\n", encoding="utf-8")
    (paths.repo_root / "restore.patch").write_text("a saved intent-gap patch\n")

    out = verify.verify_dev_exclude_relpaths(paths, sp, "restore.patch", root=paths.repo_root)
    assert "restore.patch" in out


def test_verify_dev_stories_roots_its_exclude_on_the_code_tree(project, tmp_path, monkeypatch):
    """`_stories_relpaths` is one of the four exclude sources that had to move with
    the gate's git root, and it is pinned at the SEAM rather than by outcome — on
    purpose.

    Under the SIBLING fixture this row uses (`_repo_root_override`: an artifact
    tree disjoint from the code tree) the story record and manifest sit outside the
    code tree whichever root is used, so both spellings end in "nothing was
    excluded" and no outcome assertion over THIS fixture can separate them. What
    the wrong root actually costs is invisible in a passing gate: a
    `project`-relative pathspec is resolved by git against the CODE tree, silently
    excluding whatever happens to live at that relative path there. So the
    contract here is the root itself.

    The claim is scoped to the fixture, not to the function. Under the NESTED
    (monorepo) shape the artifacts are inside the code tree and the two spellings
    differ by value and by outcome — see
    `test_stories_relpaths_separates_the_two_roots_in_a_monorepo` for the value
    and, for THIS gate's outcome,
    `test_verify_dev_stories_refuses_a_bare_spec_flip_under_the_monorepo_shape`
    (whose sprint-mode twin drops the `_stories` infix).

    Ablation: pass `paths.project` at the call site and the recorded root reddens.
    """
    paths = _repo_root_override(project, tmp_path)
    spec_folder = paths.planning_artifacts / "epic-a"
    # built by hand rather than via `make_stories_task`, which reads HEAD of
    # `paths.project` — under this override that directory is not a checkout
    task = StoryTask(story_key="1", epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    sp = write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    (paths.repo_root / "src.txt").write_text("real work\n")

    seen = []
    real = verify._stories_relpaths
    monkeypatch.setattr(
        verify,
        "_stories_relpaths",
        lambda root, folder: (seen.append(root), real(root, folder))[1],
    )
    out = verify.verify_dev_stories(
        task, paths, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )

    assert out.ok
    assert seen == [paths.repo_root]
    # the premise the assertion above rests on: under this override the two roots are
    # genuinely different directories. Should the fixture ever collapse them, the
    # recorded-root assertion stops separating the two spellings and this reddens.
    assert paths.project != paths.repo_root


def test_stories_relpaths_follows_the_root_it_is_given(project, tmp_path):
    """Same rule for the stories-mode exclude: rooted where git runs."""
    paths = _repo_root_override(project, tmp_path)
    spec_folder = paths.implementation_artifacts / "spec-x"
    spec_folder.mkdir(parents=True)
    assert verify._stories_relpaths(paths.project, spec_folder) == (
        "_bmad-output/implementation-artifacts/spec-x/stories",
        "_bmad-output/implementation-artifacts/spec-x/stories.yaml",
    )
    # outside the code tree: nothing to exclude there, and no exception
    assert verify._stories_relpaths(paths.repo_root, spec_folder) == ()


# ------------------------------------- the MONOREPO shape of the same override
#
# `_repo_root_override` above builds the SIBLING shape: `artifacts-root` beside
# `sandbox`, so every artifact sits outside the code tree. The code-root spelling
# collapses to `()` while the project-root spelling is a non-empty tail that still
# matches nothing in the code tree. Values can differ, but both spellings have the
# same gate outcome, so the seam rows assert on the recorded root instead.
#
# `conftest.nested_repo_root_paths` is the shape that CAN separate them: the BMAD
# project at `<repo>/app`, `repo_root` its ancestor. There the wrong pathspec is
# not empty, it is *plausible* — `_bmad-output/implementation-artifacts/...`
# resolved against the code root names the OUTER project's real artifact dir.
# That is the "not merely wrong, it is SILENTLY wrong" failure the production
# docstrings describe, and the sibling fixture could never exhibit it. An
# ADDITIONAL variant, not a replacement: the sibling rows grade the disjoint
# layout, which is a supported configuration in its own right.


def test_verify_dev_exclude_relpaths_separates_the_two_roots_in_a_monorepo(project):
    """Both spellings are non-empty and unequal, and the code-tree one is prefixed.

    The silently-wrong value assertion the sibling shape cannot make. There the
    wrong non-empty tail matches nothing in the code tree; here it names a real
    outer artifact. The two spellings differ by exactly the `app/` prefix, so the
    equality pins WHICH root the relpaths were measured against rather than merely
    that something was measured.

    The last assertion is the point of the whole shape: the `project`-rooted
    spelling, handed to git in the CODE tree, resolves onto a real file that is
    NOT the one it meant to exclude. A silently-wrong exclusion, not an absent
    one.

    Ablation: pin `base = paths.project` inside `verify_dev_exclude_relpaths` and
    the prefix assertions redden — both spellings come back identical.
    """
    paths = nested_repo_root_paths(project)
    # the premise every assertion below rests on: genuinely divergent, and
    # genuinely NESTED (the sibling shape satisfies the first and not the second)
    assert paths.project != paths.repo_root
    assert paths.project.parent == paths.repo_root
    sp = spec_path(paths, "1-1-a")
    sp.write_text("---\nstatus: in-review\n---\n", encoding="utf-8")
    # the OUTER project's board — the file a `project`-rooted pathspec silently
    # names when git resolves it in the code tree
    write_sprint(project, {"1-1-a": "review"})

    from_code_root = verify.verify_dev_exclude_relpaths(paths, sp, root=paths.repo_root)
    from_project = verify.verify_dev_exclude_relpaths(paths, sp, root=paths.project)

    assert from_code_root and from_project and from_code_root != from_project
    assert from_code_root == tuple(f"app/{rel}" for rel in from_project)
    assert from_code_root == (
        "app/_bmad-output/implementation-artifacts/sprint-status.yaml",
        "app/_bmad-output/implementation-artifacts/spec-1-1-a.md",
    )
    # silently wrong, not empty: the wrong spelling names the outer board
    assert (paths.repo_root / from_project[0]).is_file()
    assert (paths.repo_root / from_project[0]) != paths.sprint_status


def test_stories_relpaths_separates_the_two_roots_in_a_monorepo(project):
    """Same rule, same shape, for the stories-mode exclude.

    Its sibling row above asserts `() `for the code-tree spelling because under the
    disjoint layout there is genuinely nothing to exclude. Nested, both spellings
    produce two pathspecs and only the prefix tells them apart.

    Ablation: drop the `relative_to(root)` rebase in `_stories_relpaths` (return
    the project-relative tail whatever the root) and the prefix assertion reddens.
    """
    paths = nested_repo_root_paths(project)
    assert paths.project != paths.repo_root
    assert paths.project.parent == paths.repo_root
    spec_folder = paths.implementation_artifacts / "spec-x"
    spec_folder.mkdir(parents=True)

    from_code_root = verify._stories_relpaths(paths.repo_root, spec_folder)
    from_project = verify._stories_relpaths(paths.project, spec_folder)

    assert from_code_root and from_project and from_code_root != from_project
    assert from_code_root == tuple(f"app/{rel}" for rel in from_project)
    assert from_code_root == (
        "app/_bmad-output/implementation-artifacts/spec-x/stories",
        "app/_bmad-output/implementation-artifacts/spec-x/stories.yaml",
    )


def test_verify_dev_refuses_a_bare_spec_flip_under_the_monorepo_shape(project):
    """The OUTCOME assertion the sibling shape cannot make (DW-9).

    An attempt whose only residue is its own spec's status flip and the board has
    produced nothing, and the proof-of-work gate must say so. That refusal is only
    reachable when the exclusions actually MATCH: git has to recognise
    `app/_bmad-output/.../sprint-status.yaml` and the spec as excluded before its
    tracked-diff probe can come back empty.

    Under the sibling shape this is unaskable — the artifacts sit outside the code
    tree, so a `project`-rooted exclude names nothing there, but so does a correct
    one, and `_changes_since` answers False either way. The two seam rows above
    say exactly that about their own fixture; this row is the counterexample their
    prose now points at.

    Ablation: force `root=paths.project` at the `verify_dev_exclude_relpaths` call
    site inside `_verify_shared_gates.proof_of_work_probe` and this reddens with
    `ok=True` — the exclusions stop matching the two tracked bookkeeping changes,
    which then count as the work the attempt never did.

    Deliberately asserts the SPECIFIC reason: `not out.ok` alone passes for every
    other gate this function runs (workflow tag, status, baseline match, sprint
    pair), none of which is what this row is about.
    """
    paths = nested_repo_root_paths(project)
    # the premise the refusal rests on: genuinely divergent, and genuinely NESTED.
    # `!=` alone is satisfied by the sibling shape, under which the exclusions
    # match nothing and this row would grade a different question entirely.
    assert paths.project != paths.repo_root
    assert paths.project.parent == paths.repo_root
    # Seed the bookkeeping as tracked content first. The attempt below then
    # exercises git's exclude pathspec branch, not only the separate untracked
    # filtering branch in `_changes_since`.
    initial_baseline = verify.rev_parse_head(paths.repo_root)
    write_sprint(paths, {"1-1-a": "ready-for-dev"})
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "ready-for-dev", initial_baseline)
    git(
        paths.repo_root,
        "add",
        paths.sprint_status.relative_to(paths.repo_root).as_posix(),
        sp.relative_to(paths.repo_root).as_posix(),
    )
    git(paths.repo_root, "commit", "-q", "-m", "seed tracked BMAD bookkeeping")

    task = StoryTask(story_key="1-1-a", epic=1)
    # the baseline is stamped where the session's cwd is: the CODE tree
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    write_sprint(paths, {"1-1-a": "review"})
    write_spec(sp, "in-review", task.baseline_commit)
    # ...and no source edit at all: the spec flip and the board ARE the residue

    out = verify.verify_dev(task, paths, dev_result(sp))

    assert not out.ok
    assert out.reason == "no changes in worktree since baseline commit"
    # the same attempt with one real source edit passes, so the refusal above is
    # about the missing work and not about the fixture being unusable
    (paths.project / "src.txt").write_text("real work\n", encoding="utf-8")
    assert verify.verify_dev(task, paths, dev_result(sp)).ok


def test_verify_dev_stories_refuses_bookkeeping_only_changes_under_the_monorepo_shape(project):
    """The stories-mode twin of the outcome row above (DW-9).

    `_stories_relpaths` had a VALUE row and a SEAM row but no outcome row, so its
    production caller — the stories-mode proof-of-work gate — was still graded
    solely by `test_verify_dev_stories_roots_its_exclude_on_the_code_tree`, whose
    own amended docstring now says that fixture cannot separate the two roots. A
    value row proves the helper computes the right string; only this proves the
    gate ACTS on it.

    Same construction as `test_verify_dev_refuses_a_bare_spec_flip_under_the_monorepo_shape`,
    driving `verify_dev_stories` instead: the attempt's residue is only bookkeeping
    git must recognise as excluded before the tracked-diff probe can come back empty.
    Nested, the exclude is `app/_bmad-output/planning-artifacts/epic-a/...`; rooted
    on `project` it loses the `app/` prefix, matches nothing in the code tree, and
    the bookkeeping then counts as the work the attempt never did.

    The residue is deliberately NOT the driven story's own spec. That file is
    already excluded by `verify_dev_exclude_relpaths` (the gate's own
    `spec_path` exclusion), so a row whose only residue was the spec refuses
    identically whichever root `_stories_relpaths` was given — it grades a
    different exclude and passes the relevant ablation. The residue is therefore
    the two paths ONLY `_stories_relpaths` covers, one per element of its returned
    tuple: the `stories.yaml` manifest, and a sibling record under `stories/`.

    Ablation: pass `paths.project` to `_stories_relpaths` at its call site in
    `verify_dev_stories` and this reddens with `ok=True`.

    Asserts the SPECIFIC reason for the reason the dev twin gives: `not out.ok`
    alone is reachable from every other gate this function runs (spec resolution,
    id prefix, workflow tag, status, baseline match).
    """
    paths = nested_repo_root_paths(project)
    assert paths.project != paths.repo_root
    assert paths.project.parent == paths.repo_root
    spec_folder = paths.planning_artifacts / "epic-a"
    initial_baseline = verify.rev_parse_head(paths.repo_root)
    sp = write_story(spec_folder, "1", "x", "ready-for-dev", initial_baseline)
    manifest = spec_folder / "stories.yaml"
    manifest.write_text("stories: [1]\n", encoding="utf-8")
    sibling = write_story(spec_folder, "2", "y", "draft", initial_baseline)
    git(
        paths.repo_root,
        "add",
        spec_folder.relative_to(paths.repo_root).as_posix(),
    )
    git(paths.repo_root, "commit", "-q", "-m", "seed tracked stories bookkeeping")

    task = StoryTask(story_key="1", epic=1)
    # stamped where the session's cwd is: the CODE tree
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    write_story(spec_folder, "1", "x", "done", task.baseline_commit)
    # The manifest and sibling record are tracked modifications, so git itself
    # must honor both pathspecs returned by `_stories_relpaths`.
    manifest.write_text("stories: [1, 2]\n", encoding="utf-8")
    write_spec(sibling, "done", task.baseline_commit)

    out = verify.verify_dev_stories(
        task, paths, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )

    assert not out.ok
    assert out.reason == "no changes in worktree since baseline commit"
    # the same attempt with one real source edit passes, so the refusal above is
    # about the missing work and not about the fixture being unusable
    (paths.project / "src.txt").write_text("real work\n", encoding="utf-8")
    assert verify.verify_dev_stories(
        task, paths, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    ).ok


def test_artifact_relpaths_returns_in_repo_folders(project):
    """The orchestrator-owned artifact folders, repo-relative posix."""
    rels = verify.artifact_relpaths(project)
    assert "_bmad-output/implementation-artifacts" in rels
    assert "_bmad-output/planning-artifacts" in rels
    assert all(r and r != "." for r in rels)


def test_artifact_relpaths_drops_dot_when_folder_is_project_root(project):
    """A folder configured == project root yields "."; it must be dropped so it
    can't become a whole-tree exclude that disables the proof-of-work gate."""
    paths = dataclasses.replace(project, output_folder=project.project)
    rels = verify.artifact_relpaths(paths)
    assert "." not in rels and "" not in rels
    # the real sub-dirs are still excluded; only the root-collapsing "." is dropped
    assert "_bmad-output/implementation-artifacts" in rels


def test_has_changes_since_excludes_artifact_only_edit(project):
    """A change confined to the artifact folders is not proof of dev work."""
    baseline = verify.rev_parse_head(project.project)
    # root-level _bmad-output edit (bundle/ledger) + nested impl-artifact edit:
    # both must be excluded, proving artifact_relpaths covers output_folder too.
    (project.output_folder / "ledger.json").write_text("bookkeeping\n")
    (project.implementation_artifacts / "spec-x.md").write_text("bookkeeping\n")
    assert verify.has_changes_since(project.project, baseline) is True  # unscoped
    assert (
        verify.has_changes_since(
            project.project, baseline, exclude=verify.artifact_relpaths(project)
        )
        is False
    )
    # a real source edit still counts
    (project.project / "src.txt").write_text("real\n")
    assert (
        verify.has_changes_since(
            project.project, baseline, exclude=verify.artifact_relpaths(project)
        )
        is True
    )


def test_changes_since_reports_a_git_refusal_and_has_changes_since_collapses_it(project):
    """The two-function split, at its own layer: `_changes_since` answers the
    tri-state and `has_changes_since` is its fail-open collapse.

    `git diff --quiet` uses rc 0 / rc 1 for its two real answers, so any other rc
    is git failing rather than answering. A GATE must read that as "there are
    changes" — uncertainty keeps the stricter path, which is the long-standing
    behavior this asserts unchanged — while an OBSERVER (the parked leg's skipped
    proof-of-work record) must be able to say "unknown" rather than file a
    confident answer git never gave.

    Both are asserted against ONE refusal so the collapse is pinned as a collapse:
    the same call that answers `None` here answers `True` there. Ablation: make
    `has_changes_since` return the tri-state unchanged and its assertion fails on
    `None is True`; make `_changes_since` fold rc 128 back into `True` and its own
    assertion fails.

    The refusal is real rather than injected — an all-zero oid no repository
    resolves — so this row also carries the premise the park observation rows rest
    on."""
    baseline = "0" * 40

    assert verify._changes_since(project.project, baseline) is None
    assert verify.has_changes_since(project.project, baseline) is True

    # and a resolvable baseline is untouched by the split: both answer the same
    # real question, `False` on a tree that has not moved
    head = verify.rev_parse_head(project.project)
    assert verify._changes_since(project.project, head) is False
    assert verify.has_changes_since(project.project, head) is False


def test_has_changes_since_subtracts_baseline_untracked(project):
    """Untracked files already on disk when the baseline snapshot was taken are
    not this session's work. `None` deliberately keeps counting all of them —
    the opposite of `attempt_dirty`'s ignore-all — so a pre-snapshot run's
    proof-of-work gate is never silently weakened."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "residue.txt").write_text("left by an earlier halt\n")

    assert verify.has_changes_since(project.project, baseline) is True
    assert verify.has_changes_since(project.project, baseline, baseline_untracked=None) is True
    assert verify.has_changes_since(project.project, baseline, include_untracked=False) is False
    assert (
        verify.has_changes_since(project.project, baseline, baseline_untracked=["residue.txt"])
        is False
    )

    # a file created after the snapshot is this session's
    (project.project / "fresh.txt").write_text("this session\n")
    assert (
        verify.has_changes_since(project.project, baseline, baseline_untracked=["residue.txt"])
        is True
    )
    # and a tracked edit counts regardless of how complete the snapshot is
    (project.project / "fresh.txt").unlink()
    (project.project / "src.txt").write_text("real\n")
    assert (
        verify.has_changes_since(project.project, baseline, baseline_untracked=["residue.txt"])
        is True
    )
    assert verify.has_changes_since(project.project, baseline, include_untracked=False) is True


def test_verify_dev_exclude_relpaths_is_file_granular(project):
    """Unlike artifact_relpaths (whole-folder), this excludes only the
    sprint-status ledger and the session's own claimed spec file — sibling
    artifact-folder content (deferred-work.md, other stories' specs) is left
    un-excluded so it can register as real work."""
    sp = spec_path(project, "1-1-a")
    rels = verify.verify_dev_exclude_relpaths(project, sp, root=project.repo_root)
    assert "_bmad-output/implementation-artifacts/sprint-status.yaml" in rels
    assert "_bmad-output/implementation-artifacts/spec-1-1-a.md" in rels
    assert "_bmad-output/implementation-artifacts" not in rels
    # output_folder itself is NOT excluded here — it is the parent dir of
    # implementation_artifacts in the standard layout, so excluding it as a
    # prefix would swallow the artifact dirs' content right back out of view.
    assert "_bmad-output" not in rels
    assert "_bmad-output/implementation-artifacts/deferred-work.md" not in rels


def test_verify_dev_exclude_relpaths_includes_latched_restore_patch(project):
    """T4 (patch-restore x #79): a latched intent-gap patch file joins the
    file-granular excludes — absolute or project-relative, both derive the same
    repo-relative entry; no latch leaves the excludes unchanged."""
    sp = spec_path(project, "1-1-a")
    patch = project.implementation_artifacts / "attempt.patch"
    rel = "_bmad-output/implementation-artifacts/attempt.patch"
    assert rel in verify.verify_dev_exclude_relpaths(
        project, sp, str(patch), root=project.repo_root
    )
    assert rel in verify.verify_dev_exclude_relpaths(project, sp, rel, root=project.repo_root)
    assert rel not in verify.verify_dev_exclude_relpaths(project, sp, root=project.repo_root)


def test_verify_dev_exclude_relpaths_omits_only_an_uncertain_candidate(project, monkeypatch):
    """A refused exclude is dropped while healthy siblings retain order and normalized
    relpaths, using the canonical project snapshot without resolving it again.

    ABLATION A1: restore `paths.project.resolve()` and this test raises on the scoped
    project-root refusal before producing excludes. ABLATION A2: narrow the candidate
    guard back to `ValueError` and it raises on the refused spec instead of omitting it.
    """
    refused_spec = spec_path(project, "1-1-a")
    patch = project.implementation_artifacts / "attempt.patch"
    refuse_to_resolve(monkeypatch, project.project, refused_spec)

    assert verify.verify_dev_exclude_relpaths(
        project, refused_spec, str(patch), root=project.repo_root
    ) == (
        "_bmad-output/implementation-artifacts/sprint-status.yaml",
        "_bmad-output/implementation-artifacts/attempt.patch",
    )


def test_verify_dev_latched_restore_patch_is_not_proof_of_work(project):
    """T4 (patch-restore x #79): the latched patch file is untracked halt residue
    under the protected artifact dirs — it survives every reset, so counting it
    would let a restore re-drive whose session produced nothing pass the
    proof-of-work gate on the patch's mere presence. The gate must key on the
    APPLIED work (tracked diff from baseline), not the patch that carried it."""
    write_sprint(project, {"1-1-a": "review"})
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "baseline")
    task = make_task(project)
    patch = project.implementation_artifacts / "attempt.patch"
    patch.write_text("stale attempt diff\n", encoding="utf-8")  # untracked residue

    # control: unlatched, the residue is indistinguishable from session work and
    # passes the gate — exactly the vacuous pass the latch exclusion prevents
    assert verify.verify_dev(task, project, dev_result(sp)).ok

    task.restore_patch = str(patch)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason


def test_verify_dev_stories_latched_restore_patch_is_not_proof_of_work(project):
    """The same T4 exclusion, reached through the stories gate. `_verify_shared_gates`
    derives it from `task.restore_patch` for every mode, so a mode that forgets to
    thread the latch through can no longer regress silently (#91)."""
    spec_folder = project.planning_artifacts / "epic-a"
    task = make_stories_task(project, "1")
    sp = write_story(spec_folder, "1", "x", "done", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "baseline")
    task.baseline_commit = verify.rev_parse_head(project.project)
    patch = project.implementation_artifacts / "attempt.patch"
    patch.write_text("stale attempt diff\n", encoding="utf-8")  # untracked residue

    # control: unlatched, the residue passes for real session work
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert out.ok

    task.restore_patch = str(patch)
    out = verify.verify_dev_stories(
        task, project, dev_result(sp), spec_folder=spec_folder, review_enabled=False
    )
    assert not out.ok and "no changes" in out.reason


def test_resolve_restore_path_joins_only_relative_latches(tmp_path):
    """The one normalizer behind engine/verify/cli/runs (#91): absolute latches pass
    through untouched, relative ones anchor on the caller's root, and nothing is
    `.resolve()`d (the CLI adds that itself, for its containment check)."""
    absolute = tmp_path / "sub" / "attempt.patch"
    assert verify.resolve_restore_path(str(absolute), tmp_path / "other") == absolute
    assert verify.resolve_restore_path("art/attempt.patch", tmp_path) == tmp_path / "art" / (
        "attempt.patch"
    )
    # no normalization: the `..` survives, so a containment check still sees it
    assert ".." in str(verify.resolve_restore_path("../escape.patch", tmp_path))


def test_verify_dev_baseline_era_untracked_is_not_proof_of_work(project):
    """#88: the from-scratch case the T4 latch exclusion cannot reach. After an
    intent-gap halt the saved patch is untracked residue under the protected
    artifact dirs, and a from-scratch re-arm (`restore_patch=None`) never learns
    its path — but the re-arm's snapshot captured it. Subtracting
    `baseline_untracked` is the only mechanical close: a re-driven session that
    produced nothing but a status flip must not pass the gate on that residue."""
    write_sprint(project, {"1-1-a": "review"})
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "baseline")
    task = make_task(project)
    residue = project.implementation_artifacts / "attempt.patch"
    residue.write_text("stale intent-gap diff\n", encoding="utf-8")
    rel = residue.relative_to(project.project).as_posix()

    # no latch (from-scratch re-arm) — only the snapshot can rule the residue out
    assert task.restore_patch is None
    task.baseline_untracked = [rel]
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason

    # None (a pre-snapshot run) still counts every untracked file: the gate fails
    # open toward "work happened", never toward a silently disabled gate
    task.baseline_untracked = None
    assert verify.verify_dev(task, project, dev_result(sp)).ok

    # real work passes with the snapshot in place
    task.baseline_untracked = [rel]
    (project.project / "src.txt").write_text("real work\n")
    assert verify.verify_dev(task, project, dev_result(sp)).ok


def test_verify_dev_baseline_gate_reads_the_skills_baseline_revision_key(project):
    """#89: the generic bmad-dev-auto skill's step-03 stamps `baseline_revision`;
    `baseline_commit` exists only in the orchestrator's synthesized result.json.
    Reading just the latter made the baseline-match gate dead code in production
    (an absent key skips the check), so a spec claiming a foreign baseline sailed
    through. Masked for years by fixtures that stamped the key the skill never
    writes."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")

    write_spec(sp, "in-review", "0" * 40)  # a foreign baseline, skill-style key
    body = sp.read_text()
    assert "baseline_revision:" in body and "baseline_commit:" not in body
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason

    # matching baseline + real work → the gate passes
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("real work\n")
    assert verify.verify_dev(task, project, dev_result(sp)).ok


def test_verify_dev_baseline_gate_prefers_the_fresh_revision_over_a_stale_legacy_key(project):
    """#716: a spec carrying BOTH keys is what `runs.rearm_escalation` produces —
    it inserts `baseline_revision` and never removes a pre-existing
    `baseline_commit`. The gate must judge the value the skill just wrote, not the
    leftover.

    Ablation: swap `_BASELINE_KEYS` back to ("baseline_commit", "baseline_revision")
    and this row reddens with "does not match", which is precisely the attempt this
    bug burned — everything the session did was correct.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit, legacy_baseline="0" * 40)
    body = sp.read_text()
    assert "baseline_revision:" in body and "baseline_commit:" in body  # the dual-key shape
    (project.project / "src.txt").write_text("real work\n")

    assert verify.verify_dev(task, project, dev_result(sp)).ok


@pytest.mark.parametrize("legacy", ["", None])
def test_verify_dev_baseline_gate_skips_an_unusable_legacy_key(project, legacy):
    """An EMPTY (`baseline_commit: ''`) or YAML-null (bare `baseline_commit:`) legacy
    key must not shadow the fresh claim. `dict.get`'s default fires only on a MISSING
    key, so the empty value used to be selected and read back as "no claim" — which
    skips the baseline-match check entirely, on the very spec shape the re-arm writes.

    Ablation: drop the `if value:` / `if raw is None` guards in `auto_dev_baseline_of`
    and the gate stops checking (the empty case) or fails on the token "None" (the
    null case); either way the pairing below no longer holds.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    (project.project / "src.txt").write_text("real work\n")

    # the fresh key still decides: matching passes ...
    write_spec(sp, "in-review", task.baseline_commit, legacy_baseline=legacy)
    assert verify.verify_dev(task, project, dev_result(sp)).ok
    # ... and a foreign fresh key is still REFUSED (the gate is live, not skipped)
    write_spec(sp, "in-review", "0" * 40, legacy_baseline=legacy)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_baseline_gate_refuses_a_stale_revision_beside_a_matching_legacy_key(project):
    """The reverse-mismatch row, and a DELIBERATE tightening: `baseline_revision`
    wins whenever it is non-empty, so a stale fresh key is refused even though the
    legacy key names the right commit. That is the point of a precedence rule —
    the stale field cannot override in EITHER direction, and a spec whose two keys
    disagree is not silently rescued by whichever one happens to match.

    Ablation: make the reader prefer whichever key matches and this row passes,
    which is the "reads as green for the wrong reason" outcome it exists to refuse.
    """
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "0" * 40, legacy_baseline=task.baseline_commit)
    (project.project / "src.txt").write_text("real work\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_baseline_gate_reads_a_legacy_only_spec(project):
    """Back-compat: a spec predating the `baseline_revision` rename claims only
    `baseline_commit`, and the gate must still read it."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", OMIT, legacy_baseline=task.baseline_commit)
    body = sp.read_text()
    assert "baseline_revision:" not in body and "baseline_commit:" in body
    (project.project / "src.txt").write_text("real work\n")

    assert verify.verify_dev(task, project, dev_result(sp)).ok


def test_verify_dev_exclude_relpaths_normalizes_dotdot_segments(project):
    """A spec_path with a lexical '..' hop (as an un-normalized session-reported
    spec_file could produce) must resolve to the same exclude entry as the plain
    path — otherwise the raw string wouldn't match git's own normalized path
    output, silently defeating the exclude for that spec."""
    sp = spec_path(project, "1-1-a")
    messy = (
        project.output_folder / "planning-artifacts" / ".." / "implementation-artifacts" / sp.name
    )
    assert messy != sp  # genuinely a different (messier) Path object
    assert verify.verify_dev_exclude_relpaths(
        project, sp, root=project.repo_root
    ) == verify.verify_dev_exclude_relpaths(project, messy, root=project.repo_root)


def test_verify_dev_own_spec_status_flip_via_dotdot_path_is_not_real_work(project):
    """End-to-end regression: a dev result.json claiming its own spec through a
    '..'-laden path (but pointing at the same on-disk file) must not let a bare
    status flip slip past the proof-of-work gate."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    messy = (
        project.output_folder / "planning-artifacts" / ".." / "implementation-artifacts" / sp.name
    )
    assert messy.is_file()  # same on-disk file as sp, reached via a messier path

    out = verify.verify_dev(task, project, dev_result(messy))
    assert not out.ok and "no changes" in out.reason


def test_has_changes_since_ledger_content_counts_with_narrow_exclude(project):
    """Reproduces KNOWN-BUG-ledger-only-story-false-no-changes.md: a story whose
    entire authorized diff is sibling ledger content must not read as 'no
    changes', while a bare own-spec + sprint-status bookkeeping edit still does."""
    baseline = verify.rev_parse_head(project.project)
    sp = spec_path(project, "1-1-a")
    exclude = verify.verify_dev_exclude_relpaths(project, sp, root=project.repo_root)

    sp.write_text("bookkeeping\n")
    project.sprint_status.write_text("bookkeeping\n")
    assert verify.has_changes_since(project.project, baseline, exclude=exclude) is False

    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("### DW-1: reconciled\n\nstatus: done\n")
    assert verify.has_changes_since(project.project, baseline, exclude=exclude) is True


def test_verify_dev_ledger_only_story_counts_as_real_work(project):
    """A 'paper-trail reconciliation only' story (KNOWN-BUG-ledger-only-story-
    false-no-changes.md) whose entire real diff sits under implementation_artifacts
    (e.g. deferred-work.md) must pass, not false-negative 'no changes'."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("### DW-1: reconciled\n\nstatus: done\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_verify_dev_own_spec_status_flip_alone_is_not_real_work(project):
    """Guards the original loophole the exclusion targets: flipping only the
    session's own spec status (plus routine sprint-status bookkeeping) must
    still retry as 'no changes', even with the narrower file-level exclude."""
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)

    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason


def test_spec_within_roots(project, tmp_path):
    """Specs under the project / artifact roots are writable; an out-of-tree
    absolute path is refused (guards the reconcile mutation)."""
    assert verify.spec_within_roots(project.implementation_artifacts / "spec-x.md", project)
    assert verify.spec_within_roots(project.project / "anywhere.md", project)
    assert verify.spec_within_roots(project.output_folder, project)  # the root itself
    # an artifact root configured OUTSIDE project is still a valid root
    external_impl = tmp_path / "external-artifacts"
    external_impl.mkdir()
    external = dataclasses.replace(project, implementation_artifacts=external_impl)
    assert verify.spec_within_roots(external_impl / "spec-x.md", external)
    outside = tmp_path / "outside" / "spec.md"
    assert verify.spec_within_roots(outside, project) is False
    assert verify.spec_within_roots(Path("/etc/passwd"), project) is False


def _refuse_resolution_as(monkeypatch, target: Path, error_type: type[Exception]) -> None:
    if error_type is OSError:
        refuse_to_resolve(monkeypatch, target)
        return
    real_resolve = Path.resolve

    def stub(self, strict: bool = False):
        if str(self) == str(target):
            raise error_type("injected resolution uncertainty")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", stub)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_spec_within_roots_refuses_uncertain_reported_path(
    project, tmp_path, monkeypatch, error_type
):
    """A session-reported spec is untrusted when it cannot be resolved. Ablation
    target: move the reported-path resolve above the centralized guard and each
    error row raises instead of returning the fail-closed False."""
    reported = tmp_path / "reported" / "spec.md"
    _refuse_resolution_as(monkeypatch, reported, error_type)

    assert verify.spec_within_roots(reported, project) is False


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
@pytest.mark.parametrize(
    "root_name",
    ["project", "output_folder", "implementation_artifacts", "planning_artifacts"],
)
def test_spec_within_roots_refuses_uncertain_trusted_root(
    project, tmp_path, monkeypatch, error_type, root_name
):
    """Every trusted root must resolve before containment can be trusted. Ablation
    target: move trusted-root resolution outside the centralized guard and the
    corresponding root/error row raises instead of returning fail-closed False."""
    reported = tmp_path / "outside" / "spec.md"
    _refuse_resolution_as(monkeypatch, getattr(project, root_name), error_type)

    assert verify.spec_within_roots(reported, project) is False


def test_commits_above_empty_at_baseline(project):
    """HEAD sitting at baseline has no attempt commits to preserve."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    assert verify.commits_above(repo, baseline) == []


def test_commits_above_lists_attempt_commits_newest_first(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "impl.txt").write_text("work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    head = verify.rev_parse_head(repo)
    commits = verify.commits_above(repo, baseline)
    assert commits == [head]


def test_preserve_commits_survives_reset_and_gc(project):
    """The parked ref keeps committed attempt work reachable through the exact
    destructive sequence safe_rollback performs (reset --hard baseline) and a gc."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "impl.txt").write_text("committed work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    head = verify.rev_parse_head(repo)

    ref = verify.preserve_commits(repo, baseline, "attempt-preserve/run-abc12345")
    assert ref == "attempt-preserve/run-abc12345"

    git(repo, "reset", "--hard", baseline)
    git(repo, "gc", "--prune=now")

    assert verify.rev_parse_head(repo) == baseline  # reset landed
    assert git(repo, "rev-parse", ref).strip() == head  # work still reachable by name
    assert (repo / "impl.txt").exists() is False  # gone from the working tree...
    git(repo, "checkout", ref, "--", "impl.txt")  # ...but recoverable
    assert (repo / "impl.txt").read_text() == "committed work\n"


def test_preserve_commits_noop_without_commits(project):
    """An uncommitted-only attempt (HEAD at baseline) creates no ref and returns
    None — the caller then resets as before."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "dirty.txt").write_text("uncommitted\n")  # dirty but never committed
    assert verify.preserve_commits(repo, baseline, "attempt-preserve/run-none") is None
    assert git(repo, "branch", "--list", "attempt-preserve/run-none").strip() == ""


def test_preserve_commits_raises_on_branch_failure(project):
    """When commits exist but the branch cannot be created (here, an illegal ref
    name), raise GitError — never return None, so a caller can't mistake a
    preservation failure for a harmless no-op and reset past committed work."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "impl.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    with pytest.raises(verify.GitError):
        verify.preserve_commits(repo, baseline, "bad..ref")  # ".." is an illegal git ref name


def _dated_commit(repo, message, date):
    """Empty commit with a forced committer date, so ref-age ordering across
    branches is deterministic (back-to-back commits share a same-second date)."""
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )


def _preserve_ref_names(repo):
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/attempt-preserve/")
    return sorted(line for line in out.splitlines() if line)


def test_prune_preserve_refs_deletes_oldest_beyond_keep(project):
    """5 preserve refs, keep=3: the 2 oldest by committer date are deleted (and
    returned); the 3 newest survive. Dates deliberately disagree with creation/
    name order, so this proves committer-date ordering — not name or creation
    order."""
    repo = project.project
    # ref index -> date rank: run-2 is oldest, run-4 second-oldest, run-1 newest
    for i, day in ((0, 3), (1, 5), (2, 1), (3, 4), (4, 2)):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{day}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")

    deleted = verify.prune_preserve_refs(repo, keep=3)

    assert sorted(deleted) == ["attempt-preserve/run-2", "attempt-preserve/run-4"]
    assert _preserve_ref_names(repo) == [f"attempt-preserve/run-{i}" for i in (0, 1, 3)]


def test_prune_preserve_refs_at_or_under_budget_noop(project):
    """At/under budget (refs <= keep) nothing is deleted."""
    repo = project.project
    for i in range(3):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{i + 1}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")
    assert verify.prune_preserve_refs(repo, keep=3) == []
    assert len(_preserve_ref_names(repo)) == 3


def test_prune_preserve_refs_keep_zero_never_prunes(project):
    """keep=0 means "never prune" — no ref is deleted no matter how many exist."""
    repo = project.project
    for i in range(4):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{i + 1}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")
    assert verify.prune_preserve_refs(repo, keep=0) == []
    assert len(_preserve_ref_names(repo)) == 4


def test_prune_preserve_refs_ignores_other_branches(project):
    """Only attempt-preserve/* refs are considered or deleted: user and unit
    branches alongside them never count against the budget and are never
    touched, however old they are."""
    repo = project.project
    _dated_commit(repo, "old user work", "2025-06-01T12:00:00")
    git(repo, "branch", "-f", "feature/user-branch")
    git(repo, "branch", "-f", "bmad-loop/test-run/1-1-a")
    for i in range(2):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{i + 1}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")

    deleted = verify.prune_preserve_refs(repo, keep=1)

    assert deleted == ["attempt-preserve/run-0"]  # only the older preserve ref
    assert _preserve_ref_names(repo) == ["attempt-preserve/run-1"]
    assert git(repo, "branch", "--list", "feature/user-branch").strip() != ""
    assert git(repo, "branch", "--list", "bmad-loop/test-run/1-1-a").strip() != ""


def test_prune_preserve_refs_ties_break_by_refname(project):
    """Equal committer dates (same-second rollbacks, or two refs parked on the
    same commit) break by ascending refname — an explicit deterministic order,
    so the same repo state always prunes the same ref."""
    repo = project.project
    _dated_commit(repo, "tied attempts", "2026-01-01T12:00:00")
    git(repo, "branch", "-f", "attempt-preserve/tie-a")  # same commit ⇒ same date
    git(repo, "branch", "-f", "attempt-preserve/tie-b")
    _dated_commit(repo, "fresh attempt", "2026-01-02T12:00:00")
    git(repo, "branch", "-f", "attempt-preserve/newer")

    deleted = verify.prune_preserve_refs(repo, keep=2)

    # newest survives outright; within the tie, ascending refname wins (tie-a kept)
    assert deleted == ["attempt-preserve/tie-b"]
    assert _preserve_ref_names(repo) == ["attempt-preserve/newer", "attempt-preserve/tie-a"]


def test_prune_preserve_refs_continues_past_undeletable_ref(project):
    """A tail ref that can't be deleted (here: checked out) must not wedge the
    prune — the rest of the tail is still deleted, and the error raised at the
    end names both what was deleted and what was not."""
    repo = project.project
    for i in range(3):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{i + 1}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")
    git(repo, "checkout", "-q", "attempt-preserve/run-0")  # oldest tail ref is checked out

    with pytest.raises(verify.GitError) as excinfo:
        verify.prune_preserve_refs(repo, keep=1)

    # run-1 (the deletable tail ref) is gone; run-0 survived only because git
    # refuses to delete the checked-out branch; run-2 (newest) was kept
    assert _preserve_ref_names(repo) == ["attempt-preserve/run-0", "attempt-preserve/run-2"]
    assert "attempt-preserve/run-1" in str(excinfo.value)  # deleted, still auditable
    assert "attempt-preserve/run-0" in str(excinfo.value)  # the stuck ref is named
    # the destructive half is carried structurally, not just in the message
    assert isinstance(excinfo.value, verify.PrunePreserveError)
    assert excinfo.value.deleted == ["attempt-preserve/run-1"]
    assert len(excinfo.value.failed) == 1 and "run-0" in excinfo.value.failed[0]


def test_prune_preserve_refs_survives_host_noise(project):
    """REAL-GIT axis (#442): `make_git_noisy` gives the sandbox a git that warns on
    stderr at rc 0, which is the normal path on a host whose git config the
    orchestrator does not control. Against `_git`'s stdout+stderr merge the warning
    enters `_prune_refs`' ref list, lands in the `refs[keep:]` tail and is handed to
    `delete_branch`, which fails — so retention is WEDGED with a PrunePreserveError
    on every such host. The loud member of the family.

    The absence of the raise is the assertion, not `pytest.raises`: pre-fix this is
    the row that blows up. The kept refs are then re-read through `branch_exists`,
    because "did not raise" alone would also hold for a prune that deleted nothing.

    Ablation target: put `_prune_refs`' listing back on `_git` (the merge) and this
    fails alone, on the PrunePreserveError naming the warning line as an undeletable
    ref — the four sibling #442 rows stay green."""
    repo = project.project
    make_git_noisy(repo)
    for i in range(4):
        _dated_commit(repo, f"attempt {i}", f"2026-01-0{i + 1}T12:00:00")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")

    deleted = verify.prune_preserve_refs(repo, 2)

    assert sorted(deleted) == ["attempt-preserve/run-0", "attempt-preserve/run-1"]
    assert verify.branch_exists(repo, "attempt-preserve/run-2")
    assert verify.branch_exists(repo, "attempt-preserve/run-3")


def _dirty_ref(repo, name):
    """Point a refs/attempt-preserve-dirty/* snapshot ref at HEAD — the same
    plain `update-ref` write snapshot_worktree uses (these are not branches)."""
    git(repo, "update-ref", f"refs/attempt-preserve-dirty/{name}", "HEAD")


def _dirty_ref_names(repo):
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/")
    return sorted(line for line in out.splitlines() if line)


def test_prune_preserve_dirty_refs_deletes_oldest_beyond_keep(project):
    """5 dirty snapshot refs, keep=3: the 2 whose commits are oldest by
    committer date are deleted (and returned as full refnames); the 3 newest
    survive. Dates disagree with creation order, so this proves committer-date
    ordering. A second call at budget is then a no-op."""
    repo = project.project
    for i, day in ((0, 3), (1, 5), (2, 1), (3, 4), (4, 2)):
        _dated_commit(repo, f"snapshot {i}", f"2026-01-0{day}T12:00:00")
        _dirty_ref(repo, f"run-{i}")

    deleted = verify.prune_preserve_dirty_refs(repo, keep=3)

    assert sorted(deleted) == [f"refs/attempt-preserve-dirty/run-{i}" for i in (2, 4)]
    assert _dirty_ref_names(repo) == [f"refs/attempt-preserve-dirty/run-{i}" for i in (0, 1, 3)]
    assert verify.prune_preserve_dirty_refs(repo, keep=3) == []  # at budget now


def test_prune_preserve_dirty_refs_keep_zero_never_runs_git(project, monkeypatch):
    """keep=0 means "never prune" — return [] without even invoking git.

    Both helpers are patched, not just `_git`: the `for-each-ref` LISTING moved to
    `_git_out` (#442) while the deletes still route via `_git`, so a `_git`-only
    guard stays green against a regression that lists and then deletes nothing.
    Measured, not assumed — with the early return deleted and no dirty ref present,
    the `_git`-only form passes and this form fails on the listing."""
    repo = project.project
    _dirty_ref(repo, "run-0")

    def _boom(*a, **k):
        raise AssertionError("git must not run when keep=0")

    monkeypatch.setattr(verify, "_git", _boom)
    monkeypatch.setattr(verify, "_git_out", _boom)
    assert verify.prune_preserve_dirty_refs(repo, keep=0) == []
    monkeypatch.undo()
    assert _dirty_ref_names(repo) == ["refs/attempt-preserve-dirty/run-0"]


def test_prune_preserve_dirty_refs_ignores_branches_and_other_refs(project):
    """Only refs/attempt-preserve-dirty/* is considered or deleted: user
    branches, attempt-preserve/* branches, and tags never count against the
    budget and are never touched, however old they are."""
    repo = project.project
    _dated_commit(repo, "old work", "2025-06-01T12:00:00")
    git(repo, "branch", "-f", "feature/user-branch")
    git(repo, "branch", "-f", "attempt-preserve/run-old")
    git(repo, "tag", "old-tag")
    for i in range(2):
        _dated_commit(repo, f"snapshot {i}", f"2026-01-0{i + 1}T12:00:00")
        _dirty_ref(repo, f"run-{i}")

    deleted = verify.prune_preserve_dirty_refs(repo, keep=1)

    assert deleted == ["refs/attempt-preserve-dirty/run-0"]
    assert _dirty_ref_names(repo) == ["refs/attempt-preserve-dirty/run-1"]
    assert git(repo, "branch", "--list", "feature/user-branch").strip() != ""
    assert git(repo, "branch", "--list", "attempt-preserve/run-old").strip() != ""
    assert git(repo, "tag", "--list", "old-tag").strip() != ""


def test_prune_preserve_dirty_refs_ties_break_by_refname(project):
    """Equal committer dates (two snapshots parked on the same commit) break by
    ascending refname — the same repo state always prunes the same ref."""
    repo = project.project
    _dated_commit(repo, "tied snapshots", "2026-01-01T12:00:00")
    _dirty_ref(repo, "tie-a")  # same commit ⇒ same date
    _dirty_ref(repo, "tie-b")
    _dated_commit(repo, "fresh snapshot", "2026-01-02T12:00:00")
    _dirty_ref(repo, "newer")

    deleted = verify.prune_preserve_dirty_refs(repo, keep=2)

    # newest survives outright; within the tie, ascending refname wins (tie-a kept)
    assert deleted == ["refs/attempt-preserve-dirty/tie-b"]
    assert _dirty_ref_names(repo) == [
        "refs/attempt-preserve-dirty/newer",
        "refs/attempt-preserve-dirty/tie-a",
    ]


def test_prune_preserve_dirty_refs_continues_past_undeletable_ref(project):
    """A tail ref that can't be deleted (here: a stale .lock blocks update-ref)
    must not wedge the prune — the rest of the tail is still deleted, and the
    error raised at the end names both what was deleted and what was not."""
    repo = project.project
    for i in range(3):
        _dated_commit(repo, f"snapshot {i}", f"2026-01-0{i + 1}T12:00:00")
        _dirty_ref(repo, f"run-{i}")
    lock = repo / ".git" / "refs" / "attempt-preserve-dirty" / "run-0.lock"
    lock.write_text("")  # stale lock: update-ref -d on run-0 now fails

    try:
        with pytest.raises(verify.GitError) as excinfo:
            verify.prune_preserve_dirty_refs(repo, keep=1)
    finally:
        lock.unlink(missing_ok=True)  # let the fixture's teardown git calls run
        # unimpeded even when the block above fails in an unexpected way
    # run-1 (the deletable tail ref) is gone; run-0 survived only because of the
    # lock; run-2 (newest) was kept
    assert _dirty_ref_names(repo) == [
        "refs/attempt-preserve-dirty/run-0",
        "refs/attempt-preserve-dirty/run-2",
    ]
    assert isinstance(excinfo.value, verify.PrunePreserveError)
    assert excinfo.value.deleted == ["refs/attempt-preserve-dirty/run-1"]
    assert len(excinfo.value.failed) == 1 and "run-0" in excinfo.value.failed[0]
    assert "run-1" in str(excinfo.value)  # deleted, still auditable
    assert "run-0" in str(excinfo.value)  # the stuck ref is named


def test_snapshot_worktree_survives_reset_and_gc(project):
    """The parked ref keeps an attempt's *uncommitted* work — both a tracked edit
    and a run-created untracked file — reachable through the exact destructive
    sequence a rollback performs (reset --hard baseline, which does not delete
    untracked files, followed by safe_rollback's untracked cleanup / a gc). This
    is what `git stash create` alone cannot do: it never captures the untracked
    add."""
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "src.txt").write_text("tracked edit\n")  # modify a tracked file
    (repo / "new_test.txt").write_text("untracked new file\n")  # run-created untracked

    ref = verify.snapshot_worktree(
        repo, "refs/attempt-preserve-dirty/run-abc12345", baseline_untracked=[]
    )
    assert ref == "refs/attempt-preserve-dirty/run-abc12345"
    # parked at a commit whose parent is HEAD (recoverable via `git diff HEAD <ref>`)
    assert git(repo, "rev-parse", f"{ref}^").strip() == baseline

    git(repo, "reset", "--hard", baseline)  # revert the tracked edit
    (repo / "new_test.txt").unlink()  # simulate safe_rollback's untracked cleanup
    git(repo, "gc", "--prune=now")

    # both the tracked edit and the untracked add are recoverable by name
    # (conftest `git` strips, so compare against the newline-free blob content)
    assert git(repo, "show", f"{ref}:src.txt") == "tracked edit"
    assert git(repo, "show", f"{ref}:new_test.txt") == "untracked new file"


def test_ref_exists_sees_full_refnames_outside_heads(project):
    """`ref_exists` must resolve FULL refnames in both families the engine probes:
    the refs/attempt-preserve-dirty/* snapshots (outside refs/heads/, invisible to
    `branch_exists`) and ordinary branches given as refs/heads/<name>. Absent refs
    — and git failures, per the best-effort contract — read as False."""
    repo = project.project
    ref = "refs/attempt-preserve-dirty/run-abc12345-1"
    assert verify.ref_exists(repo, ref) is False
    git(repo, "update-ref", ref, "HEAD")
    assert verify.ref_exists(repo, ref) is True
    assert verify.ref_exists(repo, "refs/heads/main") is True
    assert verify.ref_exists(repo, "refs/heads/no-such-branch") is False
    assert verify.ref_exists(repo / "no-such-repo", ref) is False  # git failure -> absent


def test_snapshot_worktree_noop_clean_tree(project):
    """A clean tree (identical to HEAD) has nothing uncommitted to park: returns
    None and creates no ref, so a plain reset proceeds unchanged."""
    repo = project.project
    ref_name = "refs/attempt-preserve-dirty/run-clean"
    assert verify.snapshot_worktree(repo, ref_name, baseline_untracked=[]) is None
    assert git(repo, "for-each-ref", ref_name).strip() == ""


def test_snapshot_worktree_excludes_gitignored(project):
    """The snapshot honours .gitignore (`untracked_files` uses --exclude-standard),
    so ignored build output (e.g. a Unity Library/) is never dragged into the
    recovery snapshot."""
    repo = project.project
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add gitignore")
    (repo / "src.txt").write_text("tracked edit\n")  # a real change so the tree isn't clean
    (repo / "ignored.txt").write_text("build artifact\n")  # ignored — must not be snapshotted

    ref = verify.snapshot_worktree(
        repo, "refs/attempt-preserve-dirty/run-ignore", baseline_untracked=[]
    )
    assert ref is not None
    tree = git(repo, "ls-tree", "-r", "--name-only", ref)
    assert "src.txt" in tree
    assert "ignored.txt" not in tree


@pytest.mark.parametrize("kind", ["baseline-untracked", "ignored"])
def test_snapshot_worktree_force_includes_one_trusted_git_invisible_path(project, kind):
    """Recovery can park a byte-snapshotted spec that normal staging excludes."""
    repo = project.project
    rel = f"artifacts/{kind}.md"
    path = repo / rel
    path.parent.mkdir()
    if kind == "ignored":
        (repo / ".gitignore").write_text(f"/{rel}\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore recovery input")
    path.write_text("failed child bytes\n")
    baseline_untracked = [rel] if kind == "baseline-untracked" else []

    ref = verify.snapshot_worktree(
        repo,
        f"refs/attempt-preserve-dirty/forced-{kind}",
        baseline_untracked=baseline_untracked,
        force_include=(rel,),
    )

    assert ref is not None
    assert git(repo, "show", f"{ref}:{rel}") == "failed child bytes"


def test_snapshot_worktree_succeeds_without_git_identity(project, monkeypatch, tmp_path):
    """The snapshot commit uses a synthetic `bmad-loop` identity, so it succeeds even
    with no git user.name/user.email configured — otherwise the best-effort caller
    would catch the GitError and reset past the very work this ref preserves. Locks
    the fix machine-independently by isolating ambient git config and unsetting the
    repo's local identity."""
    repo = project.project
    # isolate from any global/system identity so the local unset actually bites
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-gitconfig"))
    git(repo, "config", "--local", "--unset", "user.email")
    git(repo, "config", "--local", "--unset", "user.name")
    (repo / "src.txt").write_text("edit with no identity\n")  # a real change to capture

    ref = verify.snapshot_worktree(
        repo, "refs/attempt-preserve-dirty/run-noident", baseline_untracked=[]
    )
    assert ref == "refs/attempt-preserve-dirty/run-noident"
    assert git(repo, "show", "-s", "--format=%an", ref) == "bmad-loop"  # synthetic author used
    assert git(repo, "show", f"{ref}:src.txt") == "edit with no identity"  # work captured


def test_snapshot_worktree_scopes_to_run_created_untracked(project):
    """A pre-existing untracked file (present in `baseline_untracked`) is NOT baked
    into the snapshot — safe_rollback never removes it, so capturing it would be a
    scope mismatch and a privacy leak — while a run-created untracked file IS."""
    repo = project.project
    (repo / "preexisting.txt").write_text("user's own untracked file\n")  # present at baseline
    baseline_untracked = ["preexisting.txt"]
    (repo / "run_created.txt").write_text("this run's new file\n")  # appeared after baseline

    ref = verify.snapshot_worktree(
        repo, "refs/attempt-preserve-dirty/run-scope", baseline_untracked=baseline_untracked
    )
    assert ref is not None
    tree = git(repo, "ls-tree", "-r", "--name-only", ref)
    assert "run_created.txt" in tree  # what a rollback would destroy — captured
    assert "preexisting.txt" not in tree  # the user's own file — never captured


def test_snapshot_worktree_unknown_baseline_skips_untracked(project):
    """`baseline_untracked=None` (a pre-upgrade/resumed run with no snapshot) means the
    baseline is *unknown*, not empty: safe_rollback deletes no untracked files in that
    case, so the snapshot must park none either — coercing None to [] would bake every
    current untracked file (incl. the user's own) into the recovery ref. Tracked edits
    are still parked."""
    repo = project.project
    (repo / "src.txt").write_text("tracked edit\n")  # a real change so the tree isn't clean
    (repo / "user_untracked.txt").write_text("user's own untracked file\n")  # unknown provenance

    ref = verify.snapshot_worktree(
        repo, "refs/attempt-preserve-dirty/run-unknown", baseline_untracked=None
    )
    assert ref is not None  # tracked edit still parked
    tree = git(repo, "ls-tree", "-r", "--name-only", ref)
    assert "src.txt" in tree  # tracked edit captured
    assert "user_untracked.txt" not in tree  # unknown-baseline untracked left untouched


def test_snapshot_worktree_parks_a_dirty_tree_under_host_noise(project):
    """REAL-GIT axis (#442): `write-tree` and `commit-tree` exit 0 while still warning
    on stderr, so against `_git`'s merge both answered a warning-suffixed "sha" —
    `commit-tree` was handed a non-object, or `update-ref` a non-commit, and
    snapshot_worktree raised. Since #340 that ref is a GATE, not a safety net: a plain
    rollback refuses to reset past work it could not park. So a host whose git config
    warns had no working rollback at all, on every attempt.

    The tree comparison is what stops this passing on a ref that exists but points at
    nothing useful — a snapshot whose tree equals HEAD's has parked none of the work
    it was called to save.

    Ablation targets, reverted singly and measured — the two are NOT symmetric:

    * `write-tree` back on `_git_env` (the merge): this fails on
      `GitError: git commit-tree (snapshot) failed … fatal: not a valid object
      name`, the corrupt tree carried into the next call. It reddens
      `test_snapshot_worktree_still_noops_on_a_clean_tree_under_host_noise` too, and
      not incidentally: a corrupt `tree` cannot equal a clean `head_tree`, so the
      clean-tree no-op stops returning early and walks into the same call. Recorded
      because the overlap is real, and because miscounting it would credit that row
      with coverage of this site.
    * `commit-tree` back on `_git_env`: this fails ALONE, on
      `GitError: git update-ref refs/attempt-preserve-dirty/run-noisy … not a valid
      SHA1` — a clean tree returns before ever reaching it."""
    repo = project.project
    make_git_noisy(repo)
    ref_name = "refs/attempt-preserve-dirty/run-noisy"
    (repo / "src.txt").write_text("attempt work\n")  # the uncommitted edit to park

    ref = verify.snapshot_worktree(repo, ref_name, baseline_untracked=[])

    assert ref == ref_name
    assert verify.ref_exists(repo, ref_name) is True
    # and it parked the work: the snapshot's tree is not just a copy of HEAD's
    assert git(repo, "rev-parse", f"{ref_name}^{{tree}}") != git(repo, "rev-parse", "HEAD^{tree}")
    assert git(repo, "show", f"{ref_name}:src.txt") == "attempt work"


def test_snapshot_worktree_still_noops_on_a_clean_tree_under_host_noise(project):
    """The comparison row. `rev-parse <head>^{tree}` also exits 0 while warning on
    stderr, so against the merge `head_tree` carried the warning and
    `tree == head_tree` read UNEQUAL on a tree identical to HEAD: the clean-tree no-op
    became a snapshot ref parking nothing, created on every non-destructive rollback.

    Only a test that demands `None` can catch a comparison corrupted on one side. The
    ref it wrongly creates is well-formed and its commit is real — there is nothing
    malformed downstream for another assertion to trip over.

    Ablation target: put the `rev-parse <head>^{tree}` read back on `_git` and restore
    its `head_tree.strip()`, and this fails alone, on the ref name where None was
    expected — `test_snapshot_worktree_parks_a_dirty_tree_under_host_noise` stays
    green, since a dirty tree differs from HEAD's under either read. Reverting
    `write-tree` reddens this row as well, for a different reason; that test's record
    carries the measurement."""
    repo = project.project
    make_git_noisy(repo)
    ref_name = "refs/attempt-preserve-dirty/run-clean-noisy"

    assert verify.snapshot_worktree(repo, ref_name, baseline_untracked=[]) is None

    assert verify.ref_exists(repo, ref_name) is False  # and no ref was created


def test_engine_written_is_keyword_only_on_all_dev_verifiers():
    """The three mode gates share one call contract; stories was positional in 0.9.x."""
    import inspect

    for fn in (verify.verify_dev, verify.verify_dev_bundle, verify.verify_dev_stories):
        parameter = inspect.signature(fn).parameters["engine_written"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert "operator_park" in inspect.signature(verify.verify_dev).parameters
    # The park skip's second selector (DW-1). Keyword-only for the same reason
    # `engine_written` is: `verify_dev`'s positional tail is `review_enabled`, and
    # a positional eligibility flag would be one transposed argument away from
    # silently authorizing the skip on every leg.
    park_eligible = inspect.signature(verify.verify_dev).parameters["park_eligible"]
    assert park_eligible.kind is inspect.Parameter.KEYWORD_ONLY
    assert park_eligible.default is False


# --------------------------------------------------- the git support floor (GIT_FLOOR)


@pytest.mark.parametrize(
    ("reported", "supported"),
    [
        ("git version 2.34.0\n", True),  # the boundary itself
        ("git version 2.33.8\n", False),  # one minor below it
        ("git version 2.9.5\n", False),  # numeric, not lexicographic: "9" > "34" as text
        ("git version 2.44.0.windows.1\n", True),
        # The four-component fork spelling `_shield_home_git_ignore` gates its
        # `%APPDATA%/Git/ignore` preference on (#403). The lookahead has to accept the
        # `.` that continues into `0.windows.1` for that gate to read 2.46 at all.
        ("git version 2.46.0.windows.1\n", True),
        ("git version 2.39.5 (Apple Git-154)\n", True),
        ("git version 3.0\n", True),
        ("git version 2.34\n", True),  # bare major.minor: the end-of-string arm
        # Trailing garbage where a delimiter belongs. Load-bearing because the DIGITS
        # clear the floor: without the lookahead this parses as 2.34 and authorizes
        # both a run and the shield's permanent repo-format write off an answer no
        # git ever produced. The fail-closed doctrine has to reach it.
        ("git version 2.34broken\n", False),
        ("", False),  # nothing at all: a spawn that produced no stdout
        ("fatal: not a git repository\n", False),
        # No `git version` prefix. Refused deliberately: a bare-number answer is not
        # this program's output, and the callers read False as "abort the run" and
        # "do not touch this repository".
        ("2.55.0\n", False),
    ],
)
def test_git_version_at_least_reads_only_a_git_version_line(reported, supported):
    """The PREDICATE behind every floor refusal. Unreadable answers must come back
    False: both callers read False as a refusal, so an optimistic parse is the only
    failure mode that costs anything."""
    assert verify.git_version_at_least(reported, (2, 34)) is supported


def test_git_version_at_least_is_inclusive_of_the_floor():
    """`GIT_FLOOR` is INCLUSIVE — the constant's own docstring says so, because the
    neighbouring psmux `_LAST_UNSUPPORTED` is exclusive and reads identically."""
    floor = f"git version {verify.GIT_FLOOR[0]}.{verify.GIT_FLOOR[1]}.0\n"
    assert verify.git_version_at_least(floor, verify.GIT_FLOOR) is True


def test_git_floor_text_renders_the_constant():
    """One formatter, so the four messages naming the floor cannot drift from it."""
    assert verify.git_floor_text() == f"{verify.GIT_FLOOR[0]}.{verify.GIT_FLOOR[1]}"
    assert verify.git_floor_text((2, 7)) == "2.7"


def _fake_git_version(monkeypatch, stdout, returncode=0):
    """Drive `git_below_floor`'s WIRING without touching the real git (2.55 here)."""

    def fake(repo, *args, timeout_s=None):
        assert args == ("version",)
        return subprocess.CompletedProcess(
            args=["git", "version"], returncode=returncode, stdout=stdout.encode(), stderr=b""
        )

    monkeypatch.setattr(verify, "git_bytes", fake)


def test_git_below_floor_passes_a_current_git(project, monkeypatch):
    _fake_git_version(monkeypatch, "git version 2.55.0\n")
    assert verify.git_below_floor(project.project) is None


def test_git_below_floor_reports_the_version_it_refused(project, monkeypatch):
    """The REPORTED TEXT, not a bool — every caller names the version in its own
    message, and a bool would leave them saying only "too old"."""
    _fake_git_version(monkeypatch, "git version 2.25.1\n")
    assert verify.git_below_floor(project.project) == "git version 2.25.1"


def test_git_below_floor_refuses_an_unparseable_answer(project, monkeypatch):
    """Fail closed. A git that will not say what it is does not clear the floor —
    this is the arm that still fires on a perfectly current host."""
    _fake_git_version(monkeypatch, "2.55.0\n")
    assert verify.git_below_floor(project.project) == "2.55.0"


def test_git_below_floor_refuses_a_non_zero_rc(project, monkeypatch):
    """`git version` does no repository setup, so a bad rc is a broken binary rather
    than a repo answer — and an unanswerable probe must refuse, not pass."""
    _fake_git_version(monkeypatch, "", returncode=127)
    assert verify.git_below_floor(project.project) == "git exited 127"


def test_git_below_floor_refuses_an_empty_answer(project, monkeypatch):
    """rc 0 with no stdout is still no answer. Tested apart from the rc arm because
    they reach the refusal down different branches."""
    _fake_git_version(monkeypatch, "\n")
    assert verify.git_below_floor(project.project) == "no version reported"


def test_git_below_floor_lets_a_spawn_failure_through(project, monkeypatch):
    """ "Too old" and "could not be run" are different facts and each caller
    dispositions them differently, so the raise is deliberately not folded in."""

    def boom(repo, *args, timeout_s=None):
        raise verify.GitSpawnError("git failed to spawn")

    monkeypatch.setattr(verify, "git_bytes", boom)
    with pytest.raises(verify.GitSpawnError):
        verify.git_below_floor(project.project)


def test_git_below_floor_forwards_a_per_call_timeout(project, monkeypatch):
    """The #390 seam, forwarded rather than swallowed. The CLI gates keep the engine
    bound; the TUI guard runs on the event loop and must ask with its own short
    deadline, which it cannot do if this drops the argument on the floor.

    Both rows are here because a signature that ACCEPTS `timeout_s` and ignores it
    reads identically at the call site: the None row pins the default the CLI gates
    depend on, and would stay green on a hard-coded 5.

    Ablation: drop `timeout_s=timeout_s` from the `git_bytes` call and the second
    row fails."""
    seen = []

    def fake(repo, *args, timeout_s=None):
        seen.append(timeout_s)
        return subprocess.CompletedProcess(
            args=["git", "version"], returncode=0, stdout=b"git version 2.55.0\n", stderr=b""
        )

    monkeypatch.setattr(verify, "git_bytes", fake)

    assert verify.git_below_floor(project.project) is None
    assert verify.git_below_floor(project.project, timeout_s=5) is None
    assert seen == [None, 5]


def test_under_floor_git_message_names_the_floor_and_the_answer(project):
    """One sentence for four surfaces — the CLI's abort, `validate`'s finding, the
    dry-run banner and the TUI toast — so they cannot read as different findings
    about one host. Pinned to the constant rather than to "2.34": the floor is
    allowed to move, the drift is not."""
    message = verify.under_floor_git_message("git version 2.25.1")
    assert "git version 2.25.1" in message
    assert verify.git_floor_text() in message


def test_git_below_floor_honours_the_floor_argument(project, monkeypatch):
    """The floor is a parameter so the predicate and the wiring can be ablated
    separately (#464) — and so this test does not have to move when GIT_FLOOR does."""
    _fake_git_version(monkeypatch, "git version 2.30.0\n")
    assert verify.git_below_floor(project.project, (2, 20)) is None
    assert verify.git_below_floor(project.project, (2, 40)) == "git version 2.30.0"


def test_verify_dev_roots_its_exclude_on_the_code_tree(project, tmp_path, monkeypatch):
    """The gate's OWN exclude composition, pinned at the seam.

    `_stories_relpaths` has carried a seam pin since this wave landed; the sprint
    gate's call did not, and no outcome row OVER THE SIBLING FIXTURE can supply one.
    Under `_repo_root_override` the artifact tree is disjoint from the code tree, so
    a `project`-rooted exclude yields pathspecs git matches nothing against — and
    `has_changes_since` fails OPEN (`rc != 0 -> return True`), so the passing row
    stays green and the refusal row reddens for its own unrelated reason. Reverting
    `root=paths.repo_root` to `root=paths.project` left the entire suite green before
    this row existed, which is how the anchor this wave exists to establish could be
    silently undone.

    The contract here is therefore the ROOT itself, exactly as the stories-mode row
    states it: a pathspec relative to the wrong root is not merely wrong, it is
    SILENTLY wrong.

    "No outcome row can supply one" was true of this fixture and is no longer true
    of the function: `test_verify_dev_refuses_a_bare_spec_flip_under_the_monorepo_shape`
    is that outcome row, built on the NESTED shape where the artifacts live inside
    the code tree and the wrong pathspec is plausible rather than empty. Both rows
    are kept — the seam pin grades the disjoint layout, which is a supported
    configuration the outcome row does not cover.

    Ablation: pass `root=paths.project` at the call site and the recorded root reddens.
    """
    paths = _repo_root_override(project, tmp_path)
    write_sprint(paths, {"1-1-a": "review"})
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.repo_root)
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    # the session's work lands where the session's cwd is: the CODE tree
    (paths.repo_root / "src.txt").write_text("real work\n")

    seen = []
    real = verify.verify_dev_exclude_relpaths
    monkeypatch.setattr(
        verify,
        "verify_dev_exclude_relpaths",
        lambda *a, **kw: (seen.append(kw.get("root")), real(*a, **kw))[1],
    )
    out = verify.verify_dev(task, paths, dev_result(sp))

    assert out.ok
    assert seen == [paths.repo_root]
    # the premise the assertion above rests on: under this override the two roots are
    # genuinely different directories. Should the fixture ever collapse them, the
    # recorded-root assertion stops separating the two spellings and this reddens.
    assert paths.project != paths.repo_root
