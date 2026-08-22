"""Engine scenario tests against the mock adapter — no tmux, no LLM."""

import dataclasses
import hashlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest
from conftest import (
    _FAIL,
    _OK,
    _disarm_check_script,
    _file_exists_cmd,
    _self_disarming_cmd,
    _spec_baseline,
    _write_check_script,
    committing_crash_state,
    dev_effect,
    fault_read_text,
    generic_dev_effect,
    git,
    review_effect,
    set_sprint,
    spec_path,
    write_gated_ledger,
    write_ledger,
    write_spec,
    write_sprint,
)

from bmad_loop import deferredwork, platform_util, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine, RunPaused, RunStopped, _digest_of, _run_depth
from bmad_loop.journal import LOGS_DIR, VERIFY_DIR, Journal, load_state
from bmad_loop.model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_GATE,
    SWEEP_REFUSED_DIRTY,
    SWEEP_REFUSED_FAILED,
    SWEEP_REFUSED_NOT_STARTED,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
    TokenUsage,
    VerifyOutcome,
)
from bmad_loop.policy import (
    AdapterPolicy,
    DevPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    OperatorPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    VerifyPolicy,
)
from bmad_loop.runs import STOP_REQUEST_FILE, graceful_stop_requested, rearm_escalation
from bmad_loop.sprintstatus import story_status
from bmad_loop.verify import (
    GitError,
    PrunePreserveError,
    read_frontmatter,
    rev_parse_head,
    worktree_clean,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def make_engine(project, script, policy=None, **kwargs) -> tuple[Engine, MockAdapter]:
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="test-run", project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            # in-place tests exercise the retry/defer continuation path, which
            # needs auto-rollback on; the OFF (pause) default is covered by its
            # own tests.
            scm=ScmPolicy(rollback_on_failure=True),
        ),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def resume_engine(project, engine, script, policy=None) -> tuple[Engine, MockAdapter]:
    state = load_state(engine.run_dir)
    # `cli._resume_paused_run` refuses a finished run outright. Without the same
    # refusal here a test can "resume" what the CLI never would, and prove a
    # recovery path that does not exist (#284 round-6 review, finding 1).
    assert not state.finished, "cli._resume_paused_run refuses a finished run"
    state.clear_pause()
    adapter = MockAdapter(script)
    new_engine = Engine(
        paths=project,
        policy=policy or engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        # mirror cli._resume_paused_run: the run's scope + cap are restored from
        # persisted state so a resumed `--epic N` run keeps its selector.
        epic_filter=state.epic_filter,
        story_filter=state.story_filter,
        max_stories=state.max_stories,
    )
    return new_engine, adapter


class _PostDevVerifyCaptureBus:
    """Small hook-bus double for testing the engine-to-plugin public seam."""

    def __init__(self):
        self.contexts = []

    def active(self, stage):
        return stage == "post_dev_verify"

    def emit(self, stage, ctx):
        self.contexts.append(ctx)
        return ctx


def test_post_dev_verify_exposes_journaled_command_results(project, monkeypatch):
    """A normal dev verification retains the exact result for the existing hook
    and journals stream pointers instead of unbounded JSON payloads."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
        ),
    )
    capture = _PostDevVerifyCaptureBus()
    engine._bus = capture
    result = verify.CommandResult("pytest -q", 0, "out\nerr\n", "out\n", "err\n")
    monkeypatch.setattr(verify, "run_verify_commands", lambda policy, cwd: [result])

    summary = engine.run()

    assert summary.done == 1
    (ctx,) = capture.contexts
    assert ctx.command_results == (result,)
    (entry,) = [e for e in engine.journal.entries() if e["kind"] == "verify-command-result"]
    assert entry["verification_stage"] == "dev"
    assert entry["verification_sequence"] == 1
    assert entry["command_index"] == 0 and entry["returncode"] == 0
    assert (engine.run_dir / entry["stdout_path"]).read_text(encoding="utf-8") == "out\n"
    assert (engine.run_dir / entry["stderr_path"]).read_text(encoding="utf-8") == "err\n"
    # Pointers are run-relative and land in the verifier's own store: logs/ is the
    # adapters' task-id namespace, which the TUI resolves as pane logs.
    assert entry["stdout_path"].startswith(f"{VERIFY_DIR}/")
    assert entry["stderr_path"].startswith(f"{VERIFY_DIR}/")
    assert not list((engine.run_dir / LOGS_DIR).glob("verify-*"))


def test_verify_stream_filenames_sanitize_the_whole_composition(project):
    """A long story key cannot push a composed filename past the segment cap.

    ``_session_task_id`` states the rule these filenames follow verbatim:
    sanitize the whole composition, not the parts. Capping ``story_key`` alone
    spends the entire budget on it and then appends the stage/attempt/sequence/
    index tail unchecked, so the segment overshoots by the length of that tail.
    """
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-" + "k" * platform_util.MAX_SEGMENT, epic=1)

    engine._journal_verify_command_results(
        task, "dev", (verify.CommandResult("pytest -q", 0, "tail", "out", "err"),)
    )

    (entry,) = [e for e in engine.journal.entries() if e["kind"] == "verify-command-result"]
    for pointer, suffix in ((entry["stdout_path"], "stdout"), (entry["stderr_path"], "stderr")):
        stem = pointer.rsplit("/", 1)[-1].removesuffix(f".{suffix}.log")
        assert len(stem) <= platform_util.MAX_SEGMENT
        assert (engine.run_dir / pointer).is_file()
    # the untruncated key still reaches the reader — through the record, not the name
    assert entry["story_key"] == task.story_key


def _capture_engine(project, stream_capture_kb):
    """An engine whose only interesting policy is the verifier stream cap."""
    return make_engine(
        project, [], policy=Policy(verify=VerifyPolicy(stream_capture_kb=stream_capture_kb))
    )[0]


def _sole_verify_record(engine):
    (entry,) = [e for e in engine.journal.entries() if e["kind"] == "verify-command-result"]
    return entry


def test_verify_stream_capture_retains_a_bounded_tail(project):
    """A chatty command is cut to `verify.stream_capture_kb`, and the record says so.

    COMMAND_TIMEOUT_S is 30 minutes, so an uncapped retain is hundreds of MB per
    attempt with no GC behind it. The cut keeps the TAIL — the direction every
    other bound on this output takes, and where a failing suite puts its failure.

    Ablation: have `_bounded_stream_tail` return `(text, full, full)`
    unconditionally and the file grows back to the full stream, reddening both
    the size and the truncation-flag assertions.
    """
    engine = _capture_engine(project, 1)  # 1 KiB per stream
    stdout = "".join(f"chatty line {i}\n" for i in range(1000))
    full = len(stdout.encode("utf-8"))
    assert full > 1024, "fixture must exceed the cap or it proves nothing"

    engine._journal_verify_command_results(
        StoryTask(story_key="1-1-a", epic=1),
        "dev",
        (verify.CommandResult("pytest -q", 1, "tail", stdout, ""),),
    )

    entry = _sole_verify_record(engine)
    retained = (engine.run_dir / entry["stdout_path"]).read_text(encoding="utf-8")
    assert len(retained.encode("utf-8")) == 1024
    assert retained == stdout[-len(retained) :]  # a tail, not a head
    # The record stays honest about the cut: a silently short file reads as a
    # complete one, so the FULL size and an explicit flag both travel with it.
    assert entry["stdout_bytes"] == full
    assert entry["stdout_captured_bytes"] == 1024
    assert entry["stdout_truncated"] is True
    # an under-cap stream is kept whole and flagged as such
    assert entry["stderr_bytes"] == 0
    assert entry["stderr_captured_bytes"] == 0
    assert entry["stderr_truncated"] is False
    assert entry["capture_error"] is None


def test_a_ceilinged_stream_still_reports_what_the_command_emitted(project):
    """When the in-memory ceiling already cut a stream, the record reports what
    the COMMAND emitted — not what the engine still holds.

    `MAX_STREAM_MEMORY_BYTES` bounds retention in the results list, so by the time
    a record is built the string in hand can be far smaller than what ran. Sizing
    the record off that string would under-report emission and, worse, compute
    `*_truncated` against a false baseline — calling a cut stream whole, which is
    the single thing that flag exists to prevent. Only the result knows the real
    figure, so it carries it.

    Ablation: drop the `emitted` override in `_journal_verify_command_results` and
    `stdout_bytes` comes back 100 with `stdout_truncated` False — a stream cut
    twice over, reported as complete. Verified.
    """
    engine = _capture_engine(project, 1)
    held = "o" * 100  # what survived the ceiling

    engine._journal_verify_command_results(
        StoryTask(story_key="1-1-a", epic=1),
        "dev",
        (verify.CommandResult("pytest -q", 1, "tail", held, "", 9_000_000, 0),),
    )

    entry = _sole_verify_record(engine)
    assert entry["stdout_bytes"] == 9_000_000  # emitted
    assert entry["stdout_captured_bytes"] == 100  # retained
    assert entry["stdout_truncated"] is True
    # the untouched stream keeps the ordinary meaning: emitted == retained
    assert entry["stderr_bytes"] == 0 and entry["stderr_truncated"] is False


def test_verify_stream_capture_cut_lands_on_a_character_boundary(project):
    """A byte cap cutting a multi-byte character drops the partial lead, it does
    not decode it into a replacement char.

    The stream already carries whatever U+FFFD its own `errors="replace"` decode
    produced (#378); minting another one here would put a corruption marker at a
    boundary WE chose, and a reader cannot tell the two apart.

    Ablation: switch `_bounded_stream_tail`'s decode to `errors="replace"` and
    the tail both breaks the cap it was just given (U+FFFD is 3 bytes standing in
    for the 1 it replaced, so 1024 in yields 1026 out) and carries an invented
    corruption marker. The bound assertion is the one that fires first.
    """
    engine = _capture_engine(project, 1)
    stdout = "\u20ac" * 1000  # 3 bytes apiece
    full = len(stdout.encode("utf-8"))
    assert (full - 1024) % 3 != 0, "fixture must cut mid-character or it proves nothing"

    engine._journal_verify_command_results(
        StoryTask(story_key="1-1-a", epic=1),
        "dev",
        (verify.CommandResult("pytest -q", 1, "tail", stdout, ""),),
    )

    entry = _sole_verify_record(engine)
    retained = (engine.run_dir / entry["stdout_path"]).read_text(encoding="utf-8")
    assert entry["stdout_bytes"] == full and entry["stdout_truncated"] is True
    assert entry["stdout_captured_bytes"] == len(retained.encode("utf-8"))
    # within the cap, and short of it by at most the one character that was cut
    assert 1024 - 3 <= entry["stdout_captured_bytes"] <= 1024
    assert retained == stdout[-len(retained) :]
    assert "\ufffd" not in retained


def test_verify_stream_capture_disabled_writes_no_files_and_still_journals(project):
    """`stream_capture_kb = 0` retains nothing — and still records what was emitted.

    "Nothing was retained" and "the command was silent" are different facts, so
    the byte counts survive the opt-out even though the pointers are null.

    Ablation: delete the `if max_bytes > 0:` guard in
    `_journal_verify_command_results` and the writer is called with an empty
    tail, which creates `verify/` and two empty files — reddening the
    directory-absence and null-pointer assertions.
    """
    engine = _capture_engine(project, 0)

    engine._journal_verify_command_results(
        StoryTask(story_key="1-1-a", epic=1),
        "dev",
        (verify.CommandResult("pytest -q", 1, "tail", "out\n", "err\n"),),
    )

    assert not (engine.run_dir / VERIFY_DIR).exists()  # not even the directory
    entry = _sole_verify_record(engine)
    assert entry["stdout_path"] is None and entry["stderr_path"] is None
    assert entry["stdout_captured_bytes"] == 0 and entry["stderr_captured_bytes"] == 0
    assert entry["stdout_bytes"] == 4 and entry["stderr_bytes"] == 4
    assert entry["stdout_truncated"] is True and entry["stderr_truncated"] is True
    assert entry["capture_error"] is None  # opting out is not a failure
    # the bounded merged feedback a repair session acts on is untouched by the knob
    assert entry["output_tail"] == "tail"


def test_verify_stream_capture_oserror_degrades_instead_of_killing_the_run(project, monkeypatch):
    """A failed retain is an observation loss, never a lost run (AGENTS.md).

    ENOSPC / a read-only run dir / ENAMETOOLONG used to propagate out of the
    writer and take the dev phase with it — a diagnostic killing the run it
    exists to diagnose, on a story whose verify commands PASSED.

    Ablation: delete the `except OSError` arm in
    `_journal_verify_command_results` and `engine.run()` raises OSError, so the
    run never reaches `summary.done == 1`.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
        ),
    )
    monkeypatch.setattr(
        verify,
        "run_verify_commands",
        lambda policy, cwd: [verify.CommandResult("pytest -q", 0, "tail", "out\n", "err\n")],
    )

    def _enospc(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    # BOTH writers, because which one runs is platform-dependent: POSIX anchors
    # the write at a directory descriptor (`atomic_write_text_at`) to refuse a
    # symlinked `verify/`, win32 keeps the path-based `atomic_write_text`.
    # Patching only the latter left this test green on POSIX for the wrong
    # reason — no write was intercepted, so the degrade arm never ran.
    monkeypatch.setattr("bmad_loop.journal.atomic_write_text", _enospc)
    monkeypatch.setattr("bmad_loop.journal.atomic_write_text_at", _enospc)

    summary = engine.run()

    assert summary.done == 1  # the run survives its own logging
    entry = _sole_verify_record(engine)
    assert entry["capture_error"] is not None
    assert "stdout" in entry["capture_error"] and "No space left" in entry["capture_error"]
    assert entry["stdout_path"] is None and entry["stderr_path"] is None
    # nothing was published, so 0 retained is the literal truth ...
    assert entry["stdout_captured_bytes"] == 0 and entry["stderr_captured_bytes"] == 0
    # ... while what the command emitted, and its verdict, still reach the reader
    assert entry["stdout_bytes"] == 4 and entry["stderr_bytes"] == 4
    assert entry["returncode"] == 0 and entry["output_tail"] == "tail"


def test_fix_verification_emits_post_dev_verify_with_command_results(project, monkeypatch):
    """The repair leg emits the same existing hook after it re-runs verification."""
    capture = _PostDevVerifyCaptureBus()
    engine, summary = _dev_then_fix_run(project, monkeypatch, capture)

    assert summary.done == 1
    assert [ctx.command_results[0].stdout for ctx in capture.contexts] == ["first-out", "fixed-out"]
    entries = [e for e in engine.journal.entries() if e["kind"] == "verify-command-result"]
    assert [
        (e["verification_stage"], e["verification_sequence"], e["command_index"]) for e in entries
    ] == [
        ("dev", 1, 0),
        ("fix", 2, 0),
    ]


def _one_result(command="pytest -q"):
    return (verify.CommandResult(command, 0, "tail", "out", "err"),)


def _journalled_sequences(engine):
    return [
        (e["story_key"], e["verification_stage"], e["verification_sequence"])
        for e in engine.journal.entries()
        if e["kind"] == "verify-command-result"
    ]


def test_verification_sequence_survives_a_resume(project):
    """A NEW engine over the same run dir keeps counting up, it does not restart.

    The ordinal is a public journal field AND the `post_dev_verify` join key, so
    a resumed process re-issuing 1 for a story already at 2 mints a second record
    claiming an ordinal the pre-pause run used — two different verify passes,
    indistinguishable to anything correlating on it. Re-deriving the ordinal from
    the journal on every verification is what used to buy this; the seeded
    counter has to buy it once, and this is the part a naive counter breaks.

    Ablation: seed eagerly to empty instead of lazily from the journal — replace
    `_next_verification_sequence`'s seed call with
    `self._verification_sequences = {}` — and the resumed engine re-issues 1 and
    2, reddening both the return values and the journalled sequence list.
    """
    first, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    assert first._journal_verify_command_results(task, "dev", _one_result()) == 1
    assert first._journal_verify_command_results(task, "fix", _one_result()) == 2

    # what a resume is: a fresh Engine (so a fresh counter) and a fresh Journal
    # over the run dir the paused process left behind.
    resumed, _ = make_engine(project, [])
    assert resumed.journal.path == first.journal.path, "the fixture must reuse the run dir"
    assert resumed._journal_verify_command_results(task, "fix", _one_result()) == 3
    # and from there it increments in memory — the seed is not re-read per pass
    assert resumed._journal_verify_command_results(task, "fix", _one_result()) == 4

    assert _journalled_sequences(resumed) == [
        ("1-1-a", "dev", 1),
        ("1-1-a", "fix", 2),
        ("1-1-a", "fix", 3),
        ("1-1-a", "fix", 4),
    ]


def test_verification_sequence_counts_each_story_separately(project):
    """The ordinal is per story, and the resume seed has to keep it that way.

    A run drives many stories through one Engine and one journal. A counter (or a
    seed) shared across them makes the ordinal a run-wide clock, so a plugin
    joining on (story_key, stage, sequence) finds the record it wants only by
    accident of ordering.

    Ablation: make the ordinal a run-wide clock — key BOTH the seed and the
    allocator on one constant instead of `story_key` — and `1-1-a`'s post-resume
    pass lands at 4 instead of 3, because `1-2-b`'s spent one of its numbers.
    Ablating the seed alone is NOT enough and does not redden this: an unseeded
    story falls back to 0 either way, so the run-wide bug only shows once both
    halves share the key.
    """
    first, _ = make_engine(project, [])
    a, b = StoryTask(story_key="1-1-a", epic=1), StoryTask(story_key="1-2-b", epic=1)
    assert first._journal_verify_command_results(a, "dev", _one_result()) == 1
    assert first._journal_verify_command_results(a, "fix", _one_result()) == 2

    resumed, _ = make_engine(project, [])
    # `b` has no records at all, so its seed is absent, not "the run's highest"
    assert resumed._journal_verify_command_results(b, "dev", _one_result()) == 1
    assert resumed._journal_verify_command_results(a, "dev", _one_result()) == 3

    assert _journalled_sequences(resumed) == [
        ("1-1-a", "dev", 1),
        ("1-1-a", "fix", 2),
        ("1-2-b", "dev", 1),
        ("1-1-a", "dev", 3),
    ]


def test_verification_sequence_does_not_rescan_the_journal_per_verification(project, monkeypatch):
    """Allocating an ordinal reads the journal ONCE per engine, not once per pass.

    `Journal.entries()` read_text()s the whole file and json.loads every line — a
    file this same writer keeps appending to, so a per-verification rescan costs
    more the longer the run gets, for a number the writer already knows.

    Ablation: restore the rescan (derive the ordinal from
    `max(... for entry in self.journal.entries() ...)`) and the count is 5, one
    per verification, instead of the single seeding read.
    """
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    reads = []
    real_entries = engine.journal.entries

    def counting_entries():
        reads.append(len(reads))
        return real_entries()

    monkeypatch.setattr(engine.journal, "entries", counting_entries)

    sequences = [
        engine._journal_verify_command_results(task, "dev", _one_result()) for _ in range(5)
    ]

    assert sequences == [1, 2, 3, 4, 5]  # still correct, just not re-derived
    assert len(reads) == 1, "the journal is read once to seed the counter, never per verification"


def test_verification_sequence_is_not_spent_by_a_pass_that_records_nothing(project):
    """A pass with no configured commands journals nothing and burns no ordinal.

    The rescan this replaced could not observe an ordinal it had not written, so
    an empty pass left the numbering untouched. A counter that increments anyway
    would number a run's passes differently depending on WHERE it was resumed,
    which is exactly the drift the seed exists to prevent.

    Ablation: allocate before the `if not results` guard and the second pass
    lands at 2, with a gap where the empty pass silently spent 1.
    """
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)

    assert engine._journal_verify_command_results(task, "dev", ()) is None
    assert not _journalled_sequences(engine)
    assert engine._journal_verify_command_results(task, "dev", _one_result()) == 1


def _dev_then_fix_run(project, monkeypatch, capture):
    """Drive one story through a dev verification and a repair verification.

    The first review-time verify fails, which routes the story into `_fix_phase`;
    the repair session's verify passes and the story commits. Both legs emit
    `post_dev_verify`, which is what the callers need.

    FOUR scripted returns, TWO journalled sequences — deliberately, and the
    inequality is the documented scope boundary, not a miscount to "fix". Returns
    1 and 3 are the dev and repair verifications, which this PR journals. Returns
    2 and 4 are the two `_skip_review_and_commit` review gates (the second runs
    after the repair), and the review leg is neither journalled nor published to
    any hook — see the boundary section in `docs/plugin-authoring-guide.md` and
    issue #656. The count is load-bearing, not padding: dropping the fourth value
    leaves the post-repair gate with nothing to consume and the run ends
    `crashed=True, crash_error='StopIteration: '` (measured), so a reader who
    trims the list finds out immediately.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False), dev_effect(project, "1-1-a")],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
            limits=LimitsPolicy(max_dev_attempts=2),
        ),
    )
    engine._bus = capture
    calls = iter(
        [
            [verify.CommandResult("pytest -q", 0, "first", "first-out", "")],
            [verify.CommandResult("pytest -q", 1, "review fail", "", "review fail")],
            [verify.CommandResult("pytest -q", 0, "fixed", "fixed-out", "")],
            [verify.CommandResult("pytest -q", 0, "final", "final-out", "")],
        ]
    )
    monkeypatch.setattr(verify, "run_verify_commands", lambda policy, cwd: next(calls))
    return engine, engine.run()


def test_post_dev_verify_discriminates_a_dev_emit_from_a_fix_emit(project, monkeypatch):
    """A plugin can tell which leg it is on, and find its own journal records.

    Both emits carry stage `post_dev_verify` from `Phase.DEV_VERIFY` off one
    shared `attempt` counter, so `ctx.stage` / `ctx.phase` / `ctx.attempt` cannot
    separate a dev verification from a repair one. `verification_stage` is the
    only thing that does, and `verification_sequence` is what joins the context
    back to the `verify-command-result` entries it is about — which is the point
    of exposing the results at all.

    Ablation: pass a constant (say `"dev"`) as `verification_stage` at both emit
    sites and the discriminator assertion reddens; drop `verification_sequence`
    from the emits and the journal join below finds no matching record.
    """
    capture = _PostDevVerifyCaptureBus()
    engine, summary = _dev_then_fix_run(project, monkeypatch, capture)

    assert summary.done == 1
    dev_ctx, fix_ctx = capture.contexts
    # what a plugin CANNOT discriminate on: identical stage and phase, plus one
    # per-story `attempt` counter the repair leg continues rather than restarts,
    # so a bare 2 never says whether it was a dev retry or a repair.
    assert dev_ctx.stage == fix_ctx.stage == "post_dev_verify"
    assert dev_ctx.phase == fix_ctx.phase == str(Phase.DEV_VERIFY)
    assert (dev_ctx.attempt, fix_ctx.attempt) == (1, 2)
    # ... and what now separates them
    assert (dev_ctx.verification_stage, dev_ctx.verification_sequence) == ("dev", 1)
    assert (fix_ctx.verification_stage, fix_ctx.verification_sequence) == ("fix", 2)

    # the join a correlating plugin performs: story + stage + sequence names
    # exactly this context's records, one per command, in command_index order.
    for ctx in (dev_ctx, fix_ctx):
        matched = [
            e
            for e in engine.journal.entries()
            if e["kind"] == "verify-command-result"
            and e["story_key"] == ctx.story_key
            and e["verification_stage"] == ctx.verification_stage
            and e["verification_sequence"] == ctx.verification_sequence
        ]
        assert [e["command_index"] for e in matched] == list(range(len(ctx.command_results)))
        assert [e["returncode"] for e in matched] == [r.returncode for r in ctx.command_results]
        assert [e["command"] for e in matched] == [r.command for r in ctx.command_results]


def _critical(inner):
    """Wrap a session effect so its result reports a CRITICAL escalation."""

    def effect(spec):
        result = inner(spec)
        result.result_json["escalations"] = [
            {"type": "missing-config", "severity": "CRITICAL", "detail": "operator needed"}
        ]
        return result

    return effect


@pytest.mark.parametrize("leg", ["dev", "fix"])
def test_a_critical_session_emits_post_dev_verify_on_both_legs(project, monkeypatch, leg):
    """CRITICAL is one event class, so both legs must expose it identically.

    The dev leg reaches `decide_dev` — which tests `critical_escalations` first —
    AFTER emitting `post_dev_verify`, so a CRITICAL dev session publishes its own
    verify pass to plugins on the way to the pause. The repair leg used to
    escalate ahead of its emit, and `_escalate` raises `RunPaused`: the same
    event class fired the hook on one leg and nothing at all on the other, which
    silently withholds half of a correlating plugin's verify passes.

    Both cases assert the same thing — the escalating session's OWN pass reached
    a plugin — which is the parity claim itself.

    Ablation: restore the old ordering by moving `_fix_phase`'s `crits` block
    back above `outcome = None` / `if result.status == "completed":`. The `fix`
    case then reddens (one context, not two; no `"fix"` stage ever reaches a
    plugin) while `dev` still passes — precisely the asymmetry.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    clean = dev_effect(project, "1-1-a", followup_review=False)
    escalating = _critical(dev_effect(project, "1-1-a", followup_review=False))
    engine, _ = make_engine(
        project,
        [escalating] if leg == "dev" else [clean, escalating],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
            limits=LimitsPolicy(max_dev_attempts=2),
        ),
    )
    capture = _PostDevVerifyCaptureBus()
    engine._bus = capture
    # the dev leg's own pass; then, on the `fix` case, the commit-time failure
    # that routes the story into `_fix_phase`, then the repair session's pass
    calls = iter(
        [
            [verify.CommandResult("pytest -q", 0, "dev", "dev-out", "")],
            [verify.CommandResult("pytest -q", 1, "commit fail", "", "commit fail")],
            [verify.CommandResult("pytest -q", 0, "fix", "fix-out", "")],
        ]
    )
    monkeypatch.setattr(verify, "run_verify_commands", lambda policy, cwd: next(calls))

    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    (escalated,) = [e for e in engine.journal.entries() if e["kind"] == "story-escalated"]
    assert escalated["reason"] == f"CRITICAL escalation from {leg} session: operator needed"
    # ... and the escalating session's verification is on the hook either way
    assert len(capture.contexts) == (1 if leg == "dev" else 2)
    ctx = capture.contexts[-1]
    assert ctx.verification_stage == leg
    assert [r.stdout for r in ctx.command_results] == [f"{leg}-out"]


_ONE_ATTEMPT = Policy(
    gates=GatesPolicy(mode="none"),
    notify=QUIET,
    review=ReviewPolicy(enabled=False),
    limits=LimitsPolicy(max_dev_attempts=1),
    scm=ScmPolicy(rollback_on_failure=True),
)


def _post_dev_verify_contexts(project, script, policy=_ONE_ATTEMPT):
    """Run one story and return (engine, summary, the post_dev_verify contexts)."""
    engine, _ = make_engine(project, script, policy)
    capture = _PostDevVerifyCaptureBus()
    engine._bus = capture
    return engine, engine.run(), capture.contexts


def test_post_dev_verify_marks_a_pass_that_ran_and_executed_nothing(project):
    """No `[verify] commands` configured: the pass RAN, and recorded nothing.

    `command_results == ()` alone cannot say that — it is equally what a plugin
    sees when no pass happened at all. The stage says the pass ran; the null
    sequence says there is no journal record to join to, which is the truth,
    because a pass with no results writes none.

    Two independent gates, each verified to redden this on its own. Ablation
    (stage): set it only when the pass recorded something —
    `stage=verification_stage if sequence is not None else None` — and this pass
    reports `None`, collapsing back into "no pass ran". Ablation (sequence):
    return the allocated ordinal from `_journal_verify_command_results` even with
    no results, and the context advertises a join key that the
    `verify-command-result` assertion below proves no record answers. NOTE that
    simply dropping `verification_sequence` from the emit does NOT redden this —
    the field defaults to `None`, which is the value under test.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, summary, contexts = _post_dev_verify_contexts(
        project, [dev_effect(project, "1-1-a", followup_review=False)]
    )

    assert summary.done == 1  # an empty verify config is a pass, not a failure
    (ctx,) = contexts
    assert ctx.command_results == () and ctx.verification_stage == "dev"
    assert ctx.verification_sequence is None
    assert not [e for e in engine.journal.entries() if e["kind"] == "verify-command-result"]


def test_post_dev_verify_marks_an_attempt_that_never_reached_verification(project):
    """The dev-artifact gate failed first, so no verify pass ran — stage is None.

    This is the other side of the empty tuple, and the one a plugin must not
    misread as "the commands ran and passed". Four causes reach here (session did
    not complete, an earlier gate failed, the fix leg's harvest short-circuited,
    or the engine variant suppressed the pass); the stage separates the CLASS,
    and `session_status` / `verify_reason` name the cause within it.

    Ablation: hoist `verification_stage` out of the records and pass the literal
    `"dev"` at the emit site — the gate-failure attempt then claims a pass that
    never ran, reddening both `is None` assertions.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _, summary, contexts = _post_dev_verify_contexts(
        project, [dev_effect(project, "1-1-a", final_status="in-progress")]
    )

    assert summary.done == 0
    (ctx,) = contexts
    assert ctx.command_results == ()
    assert ctx.verification_stage is None and ctx.verification_sequence is None
    # what the empty tuple cannot carry travels on the fields that can
    assert ctx.session_status == "completed" and ctx.verify_reason


def _notify_engine(project):
    return make_engine(
        project,
        [],
        policy=Policy(gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=True, file=True)),
    )[0]


def test_warn_desktop_notifier_inert_journals_and_prints(project, monkeypatch, capsys):
    """#231: at run start, notify.desktop requested + no platform notifier ->
    one journal event and a stderr warning, so an unattended launch that skipped
    `validate` still learns the desktop channel is dead."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" in kinds
    assert "warning: notify.desktop is set" in capsys.readouterr().err


def test_warn_desktop_notifier_inert_noop_when_available(project, monkeypatch, capsys):
    """A resolvable notifier means notify.desktop works here — no warning, no event."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: "osascript")

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


def test_warn_desktop_notifier_inert_noop_when_desktop_off(project, monkeypatch, capsys):
    """notify.desktop off → nothing to warn about, even with no platform notifier."""
    engine = make_engine(
        project,
        [],
        policy=Policy(
            gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=False, file=True)
        ),
    )[0]
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


def test_warn_desktop_notifier_inert_no_file_channel_guidance(project, monkeypatch, capsys):
    """desktop requested + no notifier + notify.file off → the warning says no alert
    channel is configured rather than pointing at an ATTENTION file never written."""
    engine = make_engine(
        project,
        [],
        policy=Policy(
            gates=GatesPolicy(mode="none"), notify=NotifyPolicy(desktop=True, file=False)
        ),
    )[0]
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)

    engine._warn_desktop_notifier_inert()

    err = capsys.readouterr().err
    assert "no alert channel is configured" in err
    assert "ATTENTION file" not in err


def _stub_run_side_effects(engine, monkeypatch):
    """No-op the git/loop/session side effects of Engine.run() so a test can drive
    the pre_run -> warn -> post_run path in isolation."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    for name in ("_loop", "_ensure_target_branch", "_prune_preserve_refs", "_gc_run_worktrees"):
        monkeypatch.setattr(engine, name, lambda *a, **k: None)


def test_run_warns_when_toplevel_owns_no_signals(project, monkeypatch, capsys):
    """#231/finding-4: a top-level run that owns no signals (e.g. off the main
    thread, where signal handlers cannot install) is still depth-0 / non-nested, so
    the inert-notifier warning fires. Guards the fix that keys the gate on _run_depth
    rather than signal ownership (which was wrongly silencing off-main-thread runs)."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)
    _stub_run_side_effects(engine, monkeypatch)
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)  # owns no signals

    engine.run()

    assert engine._owns_signals is False
    assert engine._is_nested is False
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" in kinds
    assert "warning: notify.desktop is set" in capsys.readouterr().err


def test_run_suppresses_warning_when_nested(project, monkeypatch, capsys):
    """A nested run (call-stack depth > 0) suppresses the once-per-run warning even
    with no notifier — it belongs to the owning top-level run, not the child sweep.
    Nesting is depth-based, so it holds even when the child owns no signals."""
    engine = _notify_engine(project)
    monkeypatch.setattr("bmad_loop.gates.desktop_notifier_kind", lambda: None)
    _stub_run_side_effects(engine, monkeypatch)
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)

    token = _run_depth.set(1)  # simulate an outer engine's run() frame
    try:
        engine.run()
    finally:
        _run_depth.reset(token)

    assert engine._is_nested is True
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "notify-desktop-unavailable" not in kinds
    assert capsys.readouterr().err == ""


def test_run_session_saves_completed_session_checkpoint(project):
    """The completed session must already be on disk when post_session fires:
    a host kill inside the hooks cannot lose it."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    original_emit = engine._emit
    on_disk_at_post_session = []

    def spying_emit(stage, *args, **kwargs):
        if stage == "post_session":
            on_disk = load_state(engine.run_dir)
            on_disk_at_post_session.append(bool(on_disk.tasks["1-1-a"].sessions))
        return original_emit(stage, *args, **kwargs)

    engine._emit = spying_emit
    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    saved = load_state(engine.run_dir)
    saved_task = saved.tasks["1-1-a"]
    assert len(saved_task.sessions) == 1
    assert saved_task.sessions[0].status == "completed"
    assert saved_task.sessions[0].usage is not None
    assert saved_task.sessions[0].usage.total == 15
    assert saved_task.tokens.total == 15
    assert on_disk_at_post_session == [True]


def test_run_session_persists_session_when_usage_read_raises(project):
    """A failed usage read propagates, but the completed session is already
    saved — usage is metadata, not a durability gate."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [SessionResult(status="completed", session_id="sess-1", transcript_path="events.jsonl")],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    def boom(result):
        raise RuntimeError("usage read failed")

    adapter.read_usage = boom
    with pytest.raises(RuntimeError):
        engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    saved = load_state(engine.run_dir)
    saved_task = saved.tasks["1-1-a"]
    assert len(saved_task.sessions) == 1
    assert saved_task.sessions[0].status == "completed"
    assert saved_task.sessions[0].session_id == "sess-1"
    assert saved_task.sessions[0].transcript_path == "events.jsonl"
    assert saved_task.sessions[0].usage is None
    # the session still ends in the journal, with its real status — only the
    # usage total is lost with the failed read
    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "completed"
    assert ends[0]["tokens"] is None
    # null, never 0: a zeroed TokenUsage weighs 0, and untracked != free. This
    # is also the path where `usage` is unbound in the finally, so a weighted
    # figure derived from it there would raise NameError instead of journaling.
    assert ends[0]["tokens_weighted"] is None


def test_run_session_journals_exactly_one_session_end(project):
    """The finally-fallback must not double-journal a session that already
    ended on the happy path."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "completed"
    assert "fired_at" not in ends[0]  # no timeout → no forensics fields


def test_adapter_crash_journals_aborted_session_end(project, monkeypatch):
    """adapter.run raising must not leave the session open forever in the
    journal: run-crash records the run, the aborted session-end the session."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)

    def explode(_spec):
        raise RuntimeError("transport died")

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [explode])

    summary = engine.run()

    assert summary.crashed
    entries = engine.journal.entries()
    crashes = [e for e in entries if e["kind"] == "run-crash"]
    assert crashes and crashes[0]["error"] == "RuntimeError"
    ends = [e for e in entries if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert "1-1-a" in ends[0]["task_id"]
    assert ends[0]["status"] == "aborted"
    assert ends[0]["error"] == "RuntimeError"
    # No usage read ever happened, so neither token field appears at all.
    # The invariant is `tokens_weighted` present iff `tokens` present — a lone
    # null weighted here would imply a read that came back empty.
    assert "tokens" not in ends[0]
    assert "tokens_weighted" not in ends[0]


def test_timeout_session_end_carries_fire_forensics(project):
    """A timed-out session's end entry records when the timeout fired, the
    fire→journal teardown gap, and which clock had expired."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    fired = time.time() - 5.0
    engine, _ = make_engine(
        project,
        [
            SessionResult(
                status="timeout",
                timeout_fired_at=fired,
                timeout_expired_clock="monotonic",
            )
        ],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert ends[0]["status"] == "timeout"
    assert ends[0]["fired_at"] == fired
    assert ends[0]["teardown_s"] >= 0
    assert ends[0]["expired_clock"] == "monotonic"


def test_session_timeout_s_env_override(monkeypatch):
    """BMAD_LOOP_SESSION_TIMEOUT_S overrides limits.session_timeout_min*60 — the
    deterministic-E2E seam for the #157 timeout path, whose 1-minute policy floor
    is too coarse to exercise in a fast real-binary run. Only a positive, parseable
    value wins; anything else falls back so a fat-fingered env can never silently
    shorten a real run's budget.

    Contract parity: `test_envvars.py` grades the reader this delegates to. Here
    the claim is that the policy default SURVIVES a bad override; there it is
    what the parse returns. A behavior change lands in both or records the
    divergence."""
    monkeypatch.delenv("BMAD_LOOP_SESSION_TIMEOUT_S", raising=False)
    assert Engine._session_timeout_s(5400.0) == 5400.0  # unset -> policy default
    monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", "2.5")
    assert Engine._session_timeout_s(5400.0) == 2.5  # positive -> override wins
    # "inf"/"1e999" ride along because they are the rows that reach *this* seam
    # with teeth: they parse and are positive, so before the finiteness guard they
    # became the session's budget and the deadline built from it never expired.
    for bad in ("0", "-1", "nonsense", "", "inf", "1e999"):
        monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", bad)
        assert Engine._session_timeout_s(5400.0) == 5400.0  # ignored -> fall back


def test_session_timeout_env_override_flows_into_spec(project, monkeypatch):
    """The override reaches the SessionSpec the adapter actually receives, not
    just the helper — so a sub-minute E2E can drive the real timeout path."""
    monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", "3")
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(
                status="timeout", timeout_fired_at=time.time(), timeout_expired_clock="both"
            )
        ],
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)

    assert adapter.sessions[0].timeout_s == 3.0  # not the 90-min policy default


def test_keyboard_interrupt_records_stopped_run(project, monkeypatch):
    """A raw KeyboardInterrupt (Windows console-ctrl bypassing the signal
    handler) records a controlled stop, not a crash."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))

    def interrupt(_spec):
        raise KeyboardInterrupt()

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [interrupt])

    summary = engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped
    assert not summary.crashed
    assert not saved.crashed
    assert killed == ["test-run"]
    entries = engine.journal.entries()
    stops = [i for i, e in enumerate(entries) if e["kind"] == "run-stop"]
    assert stops and entries[stops[0]]["reason"] == "KeyboardInterrupt"
    # the interrupted session is closed out in the journal before the stop
    ends = [i for i, e in enumerate(entries) if e["kind"] == "session-end"]
    assert len(ends) == 1
    assert entries[ends[0]]["status"] == "aborted"
    assert entries[ends[0]]["error"] == "KeyboardInterrupt"
    assert ends[0] < stops[0]


def test_nested_engine_reraises_keyboard_interrupt(project, monkeypatch):
    """A nested engine re-raises KeyboardInterrupt for the outer (owning)
    engine to record — it still tears down its own agent session."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise KeyboardInterrupt()

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    finally:
        _run_depth.reset(token)
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).stopped is False  # owner records it, not us
    assert killed == ["test-run"]


def test_resume_continues_from_completed_dev_session(project, monkeypatch):
    """A host kill inside the post-session window of a completed dev session
    must not roll the work back: resume consumes the durably-recorded result
    and drives verify/decide as if the session had just returned."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")])

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    crashed_task = saved.tasks["1-1-a"]
    assert crashed_task.phase == Phase.DEV_RUNNING
    assert crashed_task.sessions[0].result_json is not None
    assert crashed_task.attempt == 1

    # Model the attempt-owned path that was already durable when the host died.
    # Replay must never resolve or replace it, even though verification later
    # records the accepted/result spec separately.
    old_binding = str(project.project / "attempt-1-owned-spec.md")
    engine.state.tasks["1-1-a"].dispatched_spec_file = old_binding
    engine._save()
    crashed_task = load_state(engine.run_dir).tasks["1-1-a"]

    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])

    def must_not_rebind(_task):
        raise AssertionError("recorded-result replay must not resolve a new dispatched spec")

    monkeypatch.setattr(resumed, "_dispatched_spec_for_attempt", must_not_rebind)
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    # the replay stays the attempt it was recorded under, against the persisted
    # baseline — re-capturing either would shift the rollback/squash reference
    # and desync the counter from the recorded session's task_id
    assert final.attempt == 1
    assert final.baseline_commit == crashed_task.baseline_commit
    # The replay retained the persisted binding through verification (the spy
    # above proves it was never rebound), then successful commit retired the
    # retry-chain authority so final state does not retain full spec contents.
    assert final.dispatched_spec_file is None
    assert final.dispatched_spec_snapshot is None
    assert [s.role for s in adapter.sessions] == ["review"]  # dev NOT re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert not any(k.startswith("rollback") for k in kinds)


def test_fresh_dev_attempt_persists_resolved_spec_binding_before_launch(project, monkeypatch):
    """A fresh sprint attempt replaces stale ownership with the live recorded spec.

    Ablation: replace the dispatched-spec assignment with ``None`` before
    DEV_RUNNING and this test fails on the missing path observed at adapter launch.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    expected_snapshot = recorded.read_bytes()
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(recorded),
        dispatched_spec_file="stale/from/attempt-0.md",
    )
    engine.state.tasks[task.story_key] = task
    observed: list[tuple[str | None, bytes | None]] = []
    original_start = adapter.start_session

    def start_after_durable_binding(session_spec):
        saved = load_state(engine.run_dir).tasks[task.story_key]
        observed.append((saved.dispatched_spec_file, saved.dispatched_spec_snapshot))
        return original_start(session_spec)

    monkeypatch.setattr(adapter, "start_session", start_after_durable_binding)

    assert engine._dev_phase(task)

    assert observed == [(str(recorded), expected_snapshot)]
    assert task.dispatched_spec_file == str(recorded)
    assert task.dispatched_spec_snapshot == expected_snapshot


def test_fresh_dev_attempt_clears_stale_binding_when_recorded_spec_is_invalid(project, monkeypatch):
    """A new attempt with no valid recorded spec stays bare and unpinned.

    Ablations: retain the old binding in `_dev_phase`, or remove the current-attempt
    binding gate in `_generic_dev_prompt`; either makes this test quote and pin the
    vanished path instead of falling back to a bare story key.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(project.project / "missing-spec.md"),
        dispatched_spec_file="stale/from/attempt-0.md",
        dispatched_spec_snapshot=b"stale attempt bytes",
    )
    engine.state.tasks[task.story_key] = task
    observed: list[tuple[str | None, bytes | None]] = []
    original_start = adapter.start_session

    def start_after_durable_clear(session_spec):
        saved = load_state(engine.run_dir).tasks[task.story_key]
        observed.append((saved.dispatched_spec_file, saved.dispatched_spec_snapshot))
        return original_start(session_spec)

    monkeypatch.setattr(adapter, "start_session", start_after_durable_clear)

    assert engine._dev_phase(task)

    assert observed == [(None, None)]
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    dev_session = adapter.sessions[0]
    assert dev_session.prompt.startswith(f"/bmad-dev-auto {task.story_key} —")
    assert str(project.project / "missing-spec.md") not in dev_session.prompt
    assert dev_session.expected_spec is None


def test_bare_prompt_path_substring_does_not_require_snapshot(project):
    """A stale short path cannot turn a bare-key fallback into an explicit route.

    Ablation: match ``spec_file`` as a raw prompt substring and the story key's
    leading ``1`` makes this unbound attempt abort before the adapter launches.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file="1")
    engine.state.tasks[task.story_key] = task

    assert engine._dev_phase(task)

    session = adapter.sessions[0]
    assert session.prompt.startswith(f"/bmad-dev-auto {task.story_key} —")
    assert session.expected_spec is None
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_final_snapshot_fault_aborts_before_explicit_child_launch(project, monkeypatch):
    """A prompt that names a spec may not launch after its final snapshot fails.

    Ablation: delete the post-bind refusal in ``_dev_phase`` and the adapter starts
    despite the failed final observation. The last trusted pair remains durable so
    crash recovery can refuse a vanished or retargeted file.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(recorded))
    engine.state.tasks[task.story_key] = task
    real_refresh = engine._refresh_dispatched_spec_snapshot
    calls = 0

    def fail_final_snapshot(bound_task, **kwargs):
        nonlocal calls
        calls += 1
        refreshed = real_refresh(bound_task, **kwargs)
        if calls == 2:
            return False
        return refreshed

    monkeypatch.setattr(engine, "_refresh_dispatched_spec_snapshot", fail_final_snapshot)

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert calls == 2
    assert adapter.sessions == []
    saved = load_state(engine.run_dir).tasks[task.story_key]
    assert saved.dispatched_spec_file == str(recorded)
    assert saved.dispatched_spec_snapshot == recorded.read_bytes()


def test_transient_initial_binding_fault_does_not_promote_after_bare_prompt(project, monkeypatch):
    """Prompt construction and recovery ownership remain one observation.

    Ablation: re-run the full binder after building the prompt and the second
    observation promotes this attempt even though the child launched by bare key.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(recorded))
    engine.state.tasks[task.story_key] = task
    real_resolve = engine._dispatched_spec_for_attempt
    observations = 0

    def transient_first_fault(bound_task):
        nonlocal observations
        observations += 1
        if observations == 1:
            return None
        return real_resolve(bound_task)

    monkeypatch.setattr(engine, "_dispatched_spec_for_attempt", transient_first_fault)

    assert engine._dev_phase(task)

    assert observations == 1
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    assert adapter.sessions[0].prompt.startswith(f"/bmad-dev-auto {task.story_key} —")
    assert adapter.sessions[0].expected_spec is None


def test_unbound_patch_restore_prompt_aborts_before_child_launch(project, monkeypatch):
    """Every explicit dev route requires durable recoverable input bytes."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "in-review", rev_parse_head(project.project))
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(recorded),
        restore_patch="intent-gap.patch",
    )
    engine.state.tasks[task.story_key] = task
    monkeypatch.setattr(engine, "_dispatched_spec_for_attempt", lambda _task: None)
    monkeypatch.setattr(engine, "_restore_patch", lambda _task: None)

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert adapter.sessions == []
    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None


def test_pre_session_hook_mutation_refreshes_durable_snapshot(project, monkeypatch):
    """The adapter inherits the post-hook bytes recorded for later recovery."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(recorded))
    engine.state.tasks[task.story_key] = task
    hook_bytes = b"---\nstatus: ready-for-dev\n---\n\npre-session hook intent\n"
    real_gate = engine._emit_session_gate
    original_start = adapter.start_session
    observed: list[bytes | None] = []

    def mutate_before_launch(*args, **kwargs):
        prompt, env, ctx = real_gate(*args, **kwargs)
        recorded.write_bytes(hook_bytes)
        return prompt, env, ctx

    def start_after_hook_snapshot(session_spec):
        observed.append(load_state(engine.run_dir).tasks[task.story_key].dispatched_spec_snapshot)
        return original_start(session_spec)

    monkeypatch.setattr(engine, "_emit_session_gate", mutate_before_launch)
    monkeypatch.setattr(adapter, "start_session", start_after_hook_snapshot)

    assert engine._dev_phase(task)

    assert observed == [hook_bytes]


def test_pre_session_hook_deletion_aborts_before_session_start(project, monkeypatch):
    """A hook cannot launch a child or erase the last trusted recovery authority."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(recorded))
    engine.state.tasks[task.story_key] = task
    real_gate = engine._emit_session_gate

    def delete_before_launch(*args, **kwargs):
        prompt, env, ctx = real_gate(*args, **kwargs)
        recorded.unlink()
        return prompt, env, ctx

    monkeypatch.setattr(engine, "_emit_session_gate", delete_before_launch)

    with pytest.raises(RuntimeError, match="after pre-session hooks"):
        engine._dev_phase(task)

    assert adapter.sessions == []
    assert not [e for e in engine.journal.entries() if e["kind"] == "session-start"]
    saved = load_state(engine.run_dir).tasks[task.story_key]
    assert saved.dispatched_spec_file == str(recorded)
    assert saved.dispatched_spec_snapshot is not None


def test_snapshot_read_rejects_atomic_regular_file_replacement(project, monkeypatch):
    """Bytes from an opened old inode cannot be bound to its replacement name."""
    engine, _ = make_engine(project, [])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    replacement = recorded.with_suffix(".replacement")
    replacement.write_bytes(b"---\nstatus: ready-for-dev\n---\n\nreplacement input\n")
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        dispatched_spec_file=str(recorded.resolve()),
    )
    real_open = Path.open

    def open_then_replace(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if path == recorded.resolve():
            replacement.replace(recorded)
        return stream

    monkeypatch.setattr(Path, "open", open_then_replace)

    assert engine._read_dispatched_spec_snapshot(task) is None


def test_snapshot_read_rejects_in_place_change_after_read(project, monkeypatch):
    """The pathname contents must still match the bytes read from its inode."""
    engine, _ = make_engine(project, [])
    recorded = spec_path(project, "1-1-a")
    write_spec(recorded, "ready-for-dev", rev_parse_head(project.project))
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        dispatched_spec_file=str(recorded.resolve()),
    )
    real_fstat = os.fstat
    observations = 0

    def mutate_after_final_fstat(fd):
        nonlocal observations
        observed = real_fstat(fd)
        observations += 1
        if observations == 2:
            recorded.write_bytes(b"---\nstatus: ready-for-dev\n---\n\nchanged after read\n")
        return observed

    monkeypatch.setattr(os, "fstat", mutate_after_final_fstat)

    assert engine._read_dispatched_spec_snapshot(task) is None


@pytest.mark.parametrize(
    "fault",
    [OSError(36, "File name too long"), RuntimeError("symlink loop")],
    ids=["oserror", "runtime-error"],
)
def test_dispatched_spec_observation_fault_leaves_attempt_unbound(project, monkeypatch, fault):
    """A filesystem observation fault cannot abort before DEV_RUNNING is saved.

    Ablation: delete the typed guard in ``_dispatched_spec_for_attempt`` and both
    rows raise instead of returning the deliberately unbound fallback.
    """
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file="recorded-spec.md")

    def fail_observation(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr(verify, "resolve_spec_path", fail_observation)

    assert engine._dispatched_spec_for_attempt(task) is None


def test_resume_continues_from_completed_review_session(project):
    """A host kill inside the post-session window of a completed review session
    resumes into the review decision path — the dev phase is not re-entered."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # the review session's post_session
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.REVIEW_RUNNING

    resumed, adapter = resume_engine(project, engine, [])
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # replay does not burn a review-budget slot
    assert adapter.sessions == []  # neither dev nor review re-run
    entries = resumed.journal.entries()
    verifies = [e for e in entries if e["kind"] == "resume-verify"]
    assert verifies and verifies[-1]["role"] == "review"
    kinds = [e["kind"] for e in entries]
    assert "resume-restart" not in kinds


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="stalled"),
        # completed but without a recorded result (legacy state.json shape)
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed"),
    ],
)
def test_resume_restart_when_session_record_incomplete(project, record):
    """A dev-running task whose current-attempt record is not a completed
    session with a recorded result still takes today's resume-restart."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.record_session(record)
    engine.state.tasks[task.story_key] = task
    engine._save()

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-restart" in kinds
    assert "resume-verify" not in kinds


def _final_review_cycle_policy() -> Policy:
    # max_review_cycles=1 → the first (and only) review cycle IS the final one,
    # so a crash in its post-session window lands the resume with
    # review_cycle already == the budget ceiling.
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_review_cycles=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )


def test_resume_final_review_cycle_replays_clean_result(project):
    """A host death in the post-session window of the *last* allowed review
    cycle must still consume the durably-recorded clean pass: the resume
    continuation enters the loop even though review_cycle already == the budget,
    so the story reaches DONE instead of dropping a recorded clean review to
    defer. Regression guard for the final-cycle replay edge from #62."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=_final_review_cycle_policy(),
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # the (final) review session's post_session
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_RUNNING
    assert crashed.review_cycle == 1  # already at the budget ceiling
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # the replay did not burn an extra cycle
    assert adapter.sessions == []  # nothing re-run — the recorded pass was replayed
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds


def test_resume_final_review_cycle_dirty_replay_defers_without_extra_budget(project):
    """The same final-cycle replay for a non-convergent review consumes the
    recorded pass, then the loop exits on the normal budget guard — no fresh
    session, no extra cycle — and the story defers. Proves the relaxed guard
    burns no extra budget once the replayed result is consumed."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, finalized=False),
        ],
        policy=_final_review_cycle_policy(),
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].review_cycle == 1

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DEFERRED
    assert final.review_cycle == 1  # replay consumed; no extra budget burned
    assert adapter.sessions == []  # loop exited without a fresh session
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds


def _dev_verify_crash_state(project, engine, spec_status: str) -> tuple[str, Path]:
    """Persist the exact state.json shape from issue #100: a task at DEV_VERIFY
    with no verified spec (verify had not passed when the host died), whose
    completed dev session record — result on disk, commits above baseline — is
    durable. Returns (baseline, spec_path)."""
    baseline = rev_parse_head(project.project)
    # the attempt committed its work above baseline, as the reporter's session
    # did (only the work — sweeping the still-untracked sprint board into the
    # commit would make a later baseline reset delete it)
    src = project.project / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(project.project, "add", "src.txt")
    git(project.project, "commit", "-q", "-m", "attempt work for 1-1-a")
    sp = spec_path(project, "1-1-a")
    write_spec(sp, spec_status, baseline)

    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY, attempt=1)
    task.baseline_commit = baseline
    task.baseline_untracked = []
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    return baseline, sp


def test_resume_dev_verify_replays_recorded_dev_result_to_done(project):
    """#100: the host died after persisting DEV_VERIFY but before the decision's
    action completed — spec_file empty, completed/done dev record on disk,
    commits above baseline. Resume must replay that record through the normal
    verify/decide pipeline instead of demanding a manual rollback (`git reset
    --hard <baseline>`) of finished, possibly already-pushed work."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # the issue's environment: the production default, where the old
        # resume-restart arm paused with the destructive reset instruction
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    baseline, _sp = _dev_verify_crash_state(project, engine, "done")
    src = project.project / "src.txt"

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 1  # the replay burned no attempt budget
    assert final.commit_sha  # the field the issue found null — now stamped
    assert adapter.sessions == []  # nothing re-run — the record was replayed
    # the attempt's committed work survived (squashed into the story commit)
    assert "change for 1-1-a" in src.read_text()
    assert rev_parse_head(project.project) != baseline
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_dev_verify_replay_verify_still_failing_retries_normally(project):
    """When the replayed record's verify failure reproduces (the spec is still
    short of done), the replay re-enters the normal retry path — commits parked
    on a recovery ref, reset, then a fresh budgeted attempt — instead of
    resume-restart's evidence-blind discard."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])  # helper default: rollback ON
    _dev_verify_crash_state(project, engine, "in-progress")

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 2  # the replay burned no budget; the fresh attempt did
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds
    assert "resume-restart" not in kinds
    assert "attempt-commits-preserved" in kinds  # committed work parked, not orphaned


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="stalled"),
        # completed but without a recorded result (legacy state.json shape)
        SessionRecord(task_id="1-1-a-dev-1", role="dev", status="completed"),
    ],
)
def test_resume_dev_verify_record_incomplete_still_restarts(project, record):
    """DEV_VERIFY without a verified spec joins the replay matcher only for a
    completed record WITH a recorded result — anything less keeps today's
    resume-restart (no artifact re-scan, no loosening of completion authority)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY, attempt=1)
    task.record_session(record)
    engine.state.tasks[task.story_key] = task
    engine._save()

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-restart" in kinds
    assert "resume-verify" not in kinds


def test_resume_review_verify_replays_recorded_review(project):
    """A host death in the post-review-verify decision window (REVIEW_VERIFY
    persisted by the save right after the review session, decision not yet
    acted on) replays the recorded review pass instead of resume-restart's
    rollback — the same #100 window one phase later."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_review_result":
            raise RuntimeError("host died in the review decision window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_VERIFY
    assert crashed.review_cycle == 1
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 1  # the replay burned no review-budget slot
    assert adapter.sessions == []  # neither dev nor review re-run
    entries = resumed.journal.entries()
    verifies = [e for e in entries if e["kind"] == "resume-verify"]
    assert verifies and verifies[-1]["role"] == "review"
    kinds = [e["kind"] for e in entries]
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_committing_finishes_commit_to_done(project):
    """#115: the host died after _commit persisted COMMITTING but before the
    DONE save stamped commit_sha. That phase matched no resume arm and fell
    through to resume-restart, rolling back (or pausing over) fully-verified
    work. Resume must finish the commit in place — without re-charging the
    pre_commit_gate workflows (the persisted phase is durable proof they
    passed) and without any fresh session."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # the issue's environment: the production default, where the old
        # resume-restart arm paused with the manual-recovery notice
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    baseline = committing_crash_state(project, engine)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.attempt == 1 and final.review_cycle == 0  # no budget burned
    assert final.commit_sha == rev_parse_head(project.project) != baseline
    assert adapter.sessions == []  # no session re-run — gates included
    assert len(final.sessions) == 1  # only the pre-crash dev record
    # the whole attempt squashed into exactly one story commit above baseline
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert len(log.splitlines()) == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert worktree_clean(project.project)  # sprint board swept into the squash
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds and "story-done" in kinds
    assert "resume-restart" not in kinds
    assert "rollback-manual-required" not in kinds


def test_resume_committing_post_squash_still_one_commit(project):
    """The other #115 crash state: finalize_commit completed just before the
    death (squashed commit at HEAD, clean tree) but the DONE save never landed.
    The re-drive must converge on exactly ONE commit above baseline — not stack
    a second squash — and stamp commit_sha at HEAD."""
    engine, _ = make_engine(project, [])
    baseline = committing_crash_state(project, engine, post_squash=True)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.commit_sha == rev_parse_head(project.project)
    assert adapter.sessions == []
    log = git(project.project, "log", "--format=%s", f"{baseline}..HEAD")
    assert len(log.splitlines()) == 1  # re-squash, not a stacked second commit
    assert worktree_clean(project.project)
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds
    assert "resume-restart" not in kinds


def test_resume_committing_git_error_escalates(project):
    """A commit failure during the re-drive escalates (COMMITTING→ESCALATED is
    the legal failure move) with the attempt chain intact at HEAD — never a
    silent rollback through resume-restart."""
    engine, _ = make_engine(project, [])
    committing_crash_state(project, engine)
    head_before = rev_parse_head(project.project)
    # a rejecting pre-commit hook makes finalize_commit's commit step fail
    hook = project.project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    resumed, _ = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.paused and summary.escalated == 1
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.ESCALATED
    # finalize's HEAD-restore preserved the attempt chain on the branch
    assert rev_parse_head(project.project) == head_before
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds
    assert "resume-restart" not in kinds


def test_reconcile_early_return_heals_stale_resumed_dict(project):
    """On the idempotent early-return path (frontmatter already at the success
    status), the reconcile still syncs a stale *resumed* result dict from the
    frontmatter — a resumed record is the pre-reconcile snapshot, so without the
    heal its `followup_review_recommended` gate would read the template default
    and silently skip the follow-up review."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        "---\ntitle: 'test'\ntype: 'feature'\nstatus: 'done'\n"
        "followup_review_recommended: true\nbaseline_commit: 'abc'\n---\n\n## Intent\n\ntest\n"
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    # the pre-reconcile snapshot persisted before the original run mutated its
    # in-memory dict: frontmatter template default status, no followup key.
    stale = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    engine._reconcile_generic_terminal_status(task, stale)

    assert stale["status"] == "done"  # synced from the finalized frontmatter
    assert stale["followup_review_recommended"] is True  # folded from the frontmatter
    # the idempotent path never rewrites the spec, so nothing is journaled
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]


def test_run_session_record_result_json_isolated_from_later_mutation(project):
    """The durable SessionRecord holds a defensive copy of result_json, so the
    in-place mutation `_reconcile_generic_terminal_status` performs on the live
    result after the session is recorded cannot retroactively rewrite the
    persisted snapshot."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, [SessionResult(status="completed", result_json={"workflow": "auto-dev"})]
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    result = engine._run_session(task, role="dev", prompt="/bmad-dev-auto 1-1-a", seq=1)
    # reconcile-style in-place mutation of the live result AFTER it was recorded
    result.result_json["status"] = "done"
    result.result_json["followup_review_recommended"] = True

    assert task.sessions[-1].result_json == {"workflow": "auto-dev"}  # in-memory record
    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.sessions[-1].result_json == {"workflow": "auto-dev"}  # on-disk snapshot


def test_run_session_persists_result_json_only_for_resumable_roles(project):
    """Only dev/review sessions (never a label) are consumed on resume, so only
    they persist result_json; triage/sweep and labeled plugin-workflow records
    store None — the payload would be pure state.json bloat otherwise."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="completed", result_json={"role": "dev"}),
            SessionResult(status="completed", result_json={"role": "review"}),
            SessionResult(status="completed", result_json={"role": "triage"}),
            SessionResult(status="completed", result_json={"role": "labeled"}),
        ],
    )
    engine.adapters["triage"] = adapter  # SweepEngine registers this; wire it here
    task = StoryTask(story_key="1-1-a", epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1)
    engine._run_session(task, role="review", prompt="p", seq=1)
    engine._run_session(task, role="triage", prompt="p", seq=1)
    engine._run_session(task, role="dev", prompt="p", seq=1, label="tea-trace")

    by_id = {r.task_id: r.result_json for r in task.sessions}
    assert by_id["1-1-a-dev-1"] == {"role": "dev"}  # resumable → persisted
    assert by_id["1-1-a-review-1"] == {"role": "review"}  # resumable → persisted
    assert by_id["1-1-a-triage-1"] is None  # role not resumable → None
    assert by_id["1-1-a-tea-trace-1"] is None  # labeled → None


def test_run_session_labeled_task_id_capped_as_a_whole(project):
    """Two individually legal parts (story_key, plugin label) can compose past
    the Windows filename segment cap; the task_id is sanitized as one segment."""
    long_key = "k" * 110
    write_sprint(project, {long_key: "ready-for-dev"})
    engine, _ = make_engine(project, [SessionResult(status="completed")])
    task = StoryTask(story_key=long_key, epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1, label="l" * 110)

    (record,) = task.sessions
    assert len(record.task_id) <= platform_util.MAX_SEGMENT


@pytest.mark.parametrize("key", ["6-4:cli?list", "k" * 130])
def test_resumable_session_matches_sanitized_task_id(project, key):
    """Resume matching must compose the task_id byte-identically to what
    _run_session stored. A story key that sanitization actually changes (dirty
    chars, or a clean key whose composed id overflows the segment cap) would
    otherwise never match, and _finish_inflight would fall through to the
    destructive resume-restart instead of consuming the recorded result."""
    write_sprint(project, {key: "ready-for-dev"})
    engine, _ = make_engine(
        project, [SessionResult(status="completed", result_json={"status": "done"})]
    )
    task = StoryTask(story_key=key, epic=1)
    engine.state.tasks[task.story_key] = task
    engine._save()

    engine._run_session(task, role="dev", prompt="p", seq=1)
    # the post-save crash window: session recorded, phase still mid-dev
    task.phase = Phase.DEV_RUNNING
    task.attempt = 1

    resumable = engine._resumable_session(task)
    assert resumable is not None
    role, result = resumable
    assert role == "dev"
    assert result.result_json == {"status": "done"}


def test_token_budget_discounts_cache_reads(project):
    """Raw totals dominated by cache reads must not trip the budget; the
    weighted total (cache reads at 0.1x) is what's checked."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # per session: raw = 620k (would bust 1.2M over 2 sessions), weighted = 80k
    usage = TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_tokens_per_story=1_200_000),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    # Both units ride the summary (#129). total_tokens stays raw; weighted is
    # the same figure the budget just declined to trip on, 7.75x smaller here.
    # Per task, not per session: 30k in + 10k out + round(1.2M * 0.1).
    assert summary.total_tokens == 2 * 620_000
    assert summary.weighted_tokens == 160_000
    journal_text = (run_dir / "journal.jsonl").read_text()
    assert "token-budget-exceeded" not in journal_text


def _cache_heavy_engine(project, *, snapshot_weight, live_weight, usage):
    """A one-story run whose live policy weight deliberately differs from the
    weight in its persisted policy snapshot, so tests can prove which one a
    display surface read."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(cache_read_weight=live_weight),
    )
    state = RunState(
        run_id="test-run",
        project=str(project.project),
        started_at="now",
        policy_snapshot={"limits": {"cache_read_weight": snapshot_weight}},
    )
    return Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
    )


def test_summary_weighted_reads_the_run_snapshot_not_live_policy(project):
    """Displays must be reproducible from state.json alone, because that is all
    the TUI, `bmad-loop status` and `diagnose` can see — sourcing the summary
    from live policy would make them disagree for the same run.

    The engine keeps the two in agreement by stamping the snapshot at every
    start (#189); this test forces them apart to prove which one is read, so it
    must keep constructing the divergence by hand rather than via resume.

    The two weights differ here precisely so the number identifies the source:
    0.5 (snapshot) -> 1,320; 1.0 (live policy) -> 2,320, i.e. raw.
    """
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=1.0,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    summary = engine.run()

    assert summary.total_tokens == 2 * 1160
    assert summary.weighted_tokens == 1320  # 200 + 100 + 20 + round(2000 * 0.5)

    # ...and the number a TUI observer computes from the persisted state, by the
    # same per-task route tui/widgets.py takes, is identical.
    reloaded = load_state(engine.run_dir)
    weight = reloaded.cache_read_weight()
    assert weight == 0.5
    assert sum(t.tokens.weighted_total(weight) for t in reloaded.tasks.values()) == 1320


def test_summary_weights_per_task_not_over_the_aggregate(project):
    """weighted_total rounds internally, so summing per task is NOT the same as
    weighting one aggregated TokenUsage — and the TUI sums per task (its header
    is the sum of the rows it shows). Weighting the aggregate instead would make
    the CLI and the TUI disagree, which is the bug class #129 exists to remove.

    Three tasks at cache_read=5, weight 0.1: per task round(0.5) = 0 under
    banker's rounding, so 30. Aggregated first: round(15 * 0.1) = 2, so 32.
    """
    engine = _cache_heavy_engine(project, snapshot_weight=0.1, live_weight=0.1, usage=TokenUsage())
    for key in ("1-1-a", "1-1-b", "1-1-c"):
        task = StoryTask(story_key=key, epic=1)
        task.tokens = TokenUsage(input_tokens=10, cache_read_tokens=5)
        engine.state.tasks[key] = task

    summary = engine.summary()

    assert summary.total_tokens == 45  # 3 x (10 + 5)
    assert summary.weighted_tokens == 30  # NOT 32


def test_summary_counts_parked_stories_apart_from_done(project):
    """A story that committed but owes human external actions is neither `done`
    (its acceptance criteria are not met) nor `deferred`/`escalated` (nothing
    failed). Without its own count it would land in none of the three and vanish
    from a summary that still counts it in the task total.

    Constructed rather than driven: PR-1 ships the vocabulary with no writer, so
    COMMITTING -> AWAITING_OPERATOR is unreachable through the engine loop.
    """
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    for key, phase in (
        ("1-1-a", Phase.DONE),
        ("1-2-b", Phase.AWAITING_OPERATOR),
        ("1-3-c", Phase.AWAITING_OPERATOR),
    ):
        engine.state.tasks[key] = StoryTask(story_key=key, epic=1, phase=phase)

    summary = engine.summary()

    assert summary.awaiting_operator == 2
    # and it did NOT inflate any of the three existing counts
    assert summary.done == 1 and summary.deferred == 0 and summary.escalated == 0


def test_run_summary_render_names_parked_stories_only_when_there_are_any(project):
    """The clause earns its space by being absent on the runs it would be 0 on —
    a standing ", 0 awaiting operator" on every run trains readers to skip the
    one clause that matters on the run where it is not zero."""
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    assert "awaiting operator" not in engine.summary().render()

    engine.state.tasks["1-2-b"] = StoryTask(
        story_key="1-2-b", epic=1, phase=Phase.AWAITING_OPERATOR
    )

    assert "1 awaiting operator" in engine.summary().render()


def test_run_summary_projects_and_renders_a_refused_auto_sweep(project):
    """#501's closing note: a refused auto-sweep was journal-only, so the run's
    terminal output was byte-identical to a run that swept. Under
    `[sweep] auto = "run-end"` there is one trigger per run and it is not re-asked
    once the run finishes, so this line is the whole remedy.

    The clean-worktree clause is not decoration: `cmd_sweep` hard-refuses an
    unclean tree, so a `dirty` refusal whose follow-up omitted it would walk the
    operator straight into a second refusal.

    Ablation, three disjoint axes: (a) drop `sweeps_refused=` from `summary()` and
    both the projection and the render assert fail while the absence assert stays
    green; (b) delete the `if self.sweeps_refused:` block in `render()` and only
    the render asserts fail; (c) delete the `sweep` clause from that block's text
    and only the last assert fails."""
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    assert "SWEEP NOT RUN" not in engine.summary().render()  # absent when nothing refused

    engine.state.sweeps_refused["run-end"] = SWEEP_REFUSED_DIRTY

    summary = engine.summary()
    assert summary.sweeps_refused == (("run-end", SWEEP_REFUSED_DIRTY),)
    rendered = summary.render()
    assert "SWEEP NOT RUN: run-end (dirty)" in rendered
    assert "bmad-loop sweep" in rendered and "clean worktree" in rendered


def test_run_summary_snapshots_rather_than_aliases_the_refusal_record(project):
    """`summary()` is a pure projection of `self.state` (see its docstring), and a
    snapshot that keeps mutating with the engine is not one.

    `RunSummary` is `frozen=True`, so the field is a tuple of pairs rather than
    the dict `RunState` holds — which buys two things a dict cannot. The copy is
    structural, not a `dict(...)` convention a later edit could quietly drop; and
    the class stays hashable, since frozen+eq synthesizes `__hash__` over the
    fields and a dict field makes that raise. Nothing hashes a RunSummary today —
    precisely why that regression would ship unnoticed — so it is pinned here.

    Ablation: change `sweeps_refused=tuple(self.state.sweeps_refused.items())` to
    `sweeps_refused=self.state.sweeps_refused`. This reddens EIGHT tests, not one
    — recorded as measured, not as first guessed. Only two fail on the snapshot
    claim; the other six die in `render()` with `ValueError: too many values to
    unpack`, because iterating a dict yields bare keys and the line unpacks pairs.
    That is the same "a dict yields keys" degradation that keeps this field out of
    `sweeps_triggered`, and it means the ablation is loud rather than subtle. This
    test is the one that grades the snapshot semantics specifically."""
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    engine.state.sweeps_refused["run-end"] = SWEEP_REFUSED_DIRTY
    summary = engine.summary()

    engine.state.sweeps_refused["epic-1"] = SWEEP_REFUSED_FAILED

    assert summary.sweeps_refused == (("run-end", SWEEP_REFUSED_DIRTY),)
    assert hash(summary)  # frozen means hashable; a dict field would TypeError


# ---------------------------------------- awaiting-operator park path (#335)


def _park_policy(**kw):
    """review.trigger = "always" so the park has a review loop to SKIP. Under the
    "recommended" default a story that recommends no follow-up already skips it,
    and the review-skip branch could be deleted without any test noticing."""
    return Policy(
        **{
            "gates": GatesPolicy(mode="none"),
            "notify": QUIET,
            "review": ReviewPolicy(trigger="always"),
            "dev": DevPolicy(skill="bmad-dev-auto"),
            "scm": ScmPolicy(rollback_on_failure=True),
            **kw,
        }
    )


ACTIONS = ["buy example.com at the registrar", "publish the _acme-challenge TXT record"]


def test_dev_declared_park_commits_and_the_run_continues(project):
    """The whole point, end to end: a session finishes what an agent can do and
    hands the rest to a human. The story COMMITS (that is what separates a park
    from a defer), the board reaches the token, the journal records what is owed,
    and the run moves on to the next story rather than halting — one story owing
    a DNS record must never block the ones behind it.

    Two ablation targets. Delete the `_park_awaiting_operator` branch in
    `_review_and_commit` and this fails by requesting an unscripted review
    session (trigger="always"). Delete the final-phase rule in
    `_finalize_commit_phase` and the story lands DONE with its actions unrecorded
    — a false green, the exact defect the state exists to prevent."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            ),
            generic_dev_effect(project, "1-2-b"),
            review_effect(project, "1-2-b", clean=True),
        ],
        policy=_park_policy(),
    )

    summary = engine.run()

    parked = engine.state.tasks["1-1-a"]
    assert parked.phase == Phase.AWAITING_OPERATOR
    assert parked.operator_actions == ACTIONS
    assert parked.commit_sha and parked.commit_sha != parked.baseline_commit
    assert summary.awaiting_operator == 1 and not summary.paused
    # the run continued: the next story was driven and finished
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE
    assert summary.done == 1 and summary.deferred == 0 and summary.escalated == 0
    # no review session for the parked story — only its dev pass and 1-2-b's
    assert [(s.role, s.task_id.split("-dev")[0]) for s in adapter.sessions if s.role == "dev"] == [
        ("dev", "1-1-a"),
        ("dev", "1-2-b"),
    ]
    assert not any(s.role == "review" and "1-1-a" in s.task_id for s in adapter.sessions)
    # the board reached the token — a forward advance through the sole writer
    assert story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    # the work is really in HEAD, not stashed on a recovery ref
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert parked.preserve_ref is None and parked.defer_reason is None

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-skipped-awaiting-operator" in kinds and "story-awaiting-operator" in kinds
    assert "story-deferred" not in kinds and "story-done" in kinds  # 1-2-b's
    # the journal entry carries the actions themselves, not a count: with the
    # registry not yet shipped (part 3), it and the spec are the only records
    parked_entry = next(
        e for e in engine.journal.entries() if e["kind"] == "story-awaiting-operator"
    )
    assert parked_entry["actions"] == ACTIONS and parked_entry["commit"] == parked.commit_sha


def test_park_commit_message_marks_the_story_awaiting_operator(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            )
        ],
        policy=_park_policy(),
    )

    engine.run()

    # one commit, the story's own, marked so `git log` still says what the run
    # summary said long after the summary scrolled past
    assert git(project.project, "log", "-1", "--format=%s").strip().endswith("(awaiting operator)")
    assert engine.state.tasks["1-1-a"].commit_sha == rev_parse_head(project.project)
    assert worktree_clean(project.project)


def test_park_without_usable_actions_is_repaired_not_committed(project):
    """A spec at the park status declaring nothing is not a park yet. verify's
    non-empty gate is fixable, so a repair session runs with the reason as
    feedback; here it finalizes `done` honestly and the story commits as DONE.

    Ablation: delete the non-empty gate in `_operator_actions_gate` and this
    fails — the blank park verifies green and commits as AWAITING_OPERATOR with
    an empty obligation nobody can ever confirm."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=[]
            ),
            generic_dev_effect(project, "1-1-a", followup_review=False),
        ],
        policy=_park_policy(review=ReviewPolicy(enabled=False)),
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.operator_actions == []
    assert summary.done == 1 and summary.awaiting_operator == 0
    assert len(adapter.sessions) == 2  # the park attempt, then the repair
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-awaiting-operator" not in kinds and "story-done" in kinds


def test_park_disabled_by_policy_never_commits_the_token(project):
    """`[operator] enabled = false` does not reinterpret the token — it makes it
    unknown. The gate rejects it, the attempt budget runs out, and the story
    defers with its work preserved rather than committing a phase the operator
    turned off."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    script = [
        generic_dev_effect(
            project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
        )
    ] * 3
    engine, _ = make_engine(
        project, script, policy=_park_policy(operator=OperatorPolicy(enabled=False))
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED and summary.awaiting_operator == 0
    assert story_status(project.sprint_status, "1-1-a") != "awaiting-operator"
    assert "story-awaiting-operator" not in [e["kind"] for e in engine.journal.entries()]


def test_resume_through_committing_re_derives_the_park(project):
    """The crash-resume contract (#115) needs no park-specific code: the actions
    are latched BEFORE `advance(COMMITTING)`, so the resume arm re-enters
    `_finalize_commit_phase` and its final-phase rule reaches the same verdict the
    pre-crash run would have. Ablation: latch the actions AFTER the advance and
    the resumed story lands DONE, silently dropping the obligation."""
    engine, _ = make_engine(project, [])
    baseline = committing_crash_state(project, engine, operator_actions=ACTIONS)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.AWAITING_OPERATOR
    assert final.operator_actions == ACTIONS
    assert final.commit_sha == rev_parse_head(project.project) != baseline
    assert summary.awaiting_operator == 1 and not summary.crashed
    assert adapter.sessions == []  # no session re-run, gates included
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-commit" in kinds and "story-awaiting-operator" in kinds
    assert "story-done" not in kinds
    # the re-driven commit carries the record (#356): `_write_park_record` is
    # regenerated from persisted state inside the same window the resume re-enters
    assert ".bmad-loop/operator/1-1-a.json" in git(
        project.project, "ls-tree", "-r", "--name-only", "HEAD"
    )


def test_park_commits_a_record_for_confirm(project):
    """The park writes the per-story record `bmad-loop confirm` reads — INTO the
    story's own commit (#356), so a fresh clone can confirm what this machine
    parked. Without it the obligation exists only in a journal nobody greps and
    a spec nobody re-reads, and there is no way to find the parked story's spec
    from its key.

    Ablation: delete the `_write_park_record` call in `_finalize_commit_phase`
    and this fails — the story parks with nothing to confirm it from."""
    from bmad_loop import operatoractions

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            )
        ],
        policy=_park_policy(),
    )

    engine.run()

    entry = operatoractions.load(project.project)["1-1-a"]
    assert entry["actions"] == ACTIONS
    assert entry["run_id"] == engine.state.run_id
    assert "commit" not in entry  # the record rides the commit it would name
    # the record is IN the park commit, not untracked beside it: that is what a
    # fresh clone receives, and what keeps the tree clean for the next story
    assert ".bmad-loop/operator/1-1-a.json" in git(
        project.project, "ls-tree", "-r", "--name-only", "HEAD"
    )
    assert worktree_clean(project.project)
    # the recorded spec path resolves from the PROJECT, which is what `confirm`
    # has — a worktree-absolute path would be dead before a human read it
    (story,) = operatoractions.resolve(project.project, project)
    assert story.spec_path is not None and story.spec_path.is_file()
    assert story.confirmable, story.drift()
    # provenance is derived from the record's own history: the park commit
    assert story.commit == engine.state.tasks["1-1-a"].commit_sha


def test_park_record_failure_never_blocks_the_story(project, monkeypatch):
    """The only best-effort write on the park path. Since #356 it runs BEFORE
    `finalize_commit` (the record rides the story's commit), so raising here
    would now abort the commit of a story that genuinely finished — it degrades
    to a journal line instead, the story parks recordless, and `validate`
    reports the board-parked-but-recordless drift."""
    from bmad_loop import operatoractions

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            )
        ],
        policy=_park_policy(),
    )

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr("bmad_loop.operatoractions.record_park", boom)
    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert summary.awaiting_operator == 1 and not summary.paused
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "operator-index-failed" in kinds and "story-awaiting-operator" in kinds
    assert operatoractions.load(project.project) == {}
    # the commit landed recordless and CLEAN — no record, no half-written .tmp
    assert ".bmad-loop/operator" not in git(project.project, "ls-tree", "-r", "--name-only", "HEAD")
    assert worktree_clean(project.project)


def _park_engine(project):
    """A run whose single story parks with actions owed."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            )
        ],
        policy=_park_policy(),
    )
    return engine


def test_a_failed_commit_restores_the_park_record(project):
    """The record is written just BEFORE `finalize_commit` so it rides the
    story's own commit — but that commit can still fail, and `_escalate` unwinds
    nothing. Left alone, the record claims a park that is in no commit, sitting
    untracked in the tree the run-start preflight refuses and the next story's
    `git add -A` would sweep into an unrelated commit — the two hazards the old
    machine-local index dodged with a git exclude, owed a restore now that the
    record is deliberately visible.

    Ablation: delete the `_restore_park_record` calls in `_finalize_commit_phase`
    and this fails — the record survives a commit that does not exist."""
    from bmad_loop import operatoractions

    engine = _park_engine(project)
    _reject_commits(project)

    summary = engine.run()

    assert summary.awaiting_operator == 0 and summary.paused  # the commit really did fail
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert operatoractions.load(project.project) == {}
    # emptied on the way out: no record, no .tmp, no husk of a directory
    assert not operatoractions.records_dir(project.project).exists()


def _park_over_an_earlier_record(project, actions):
    """A story that has been parked BEFORE, its record committed. Reaches the
    restore's other branch: with a prior on disk `_write_park_record` captures
    it, so the failure arms take the put-back write rather than the unlink the
    row above covers."""
    from bmad_loop import operatoractions

    operatoractions.record_park(
        project.project,
        "1-1-a",
        actions=actions,
        spec_file="docs/spec-1-1-a.md",
        run_id="run-0",
        parked_at="2026-07-01",
    )
    record = operatoractions.record_path(project.project, "1-1-a")
    git(project.project, "add", str(record))
    git(project.project, "commit", "-q", "-m", "an earlier park's record")
    engine = _park_engine(project)
    _reject_commits(project)
    return engine, record


def test_a_failed_commit_puts_an_earlier_park_record_back(project):
    """The restore's OTHER branch, and it was untested: a re-park over a record
    that already exists must put the PRIOR text back, not just delete what this
    attempt wrote. Left alone the committed record claims this run's actions for
    a commit that does not exist, so `confirm` would discharge a park against a
    tracked file whose content no history carries.

    Byte-for-byte, and the put-back is atomic (#379): a torn restore leaves the
    record neither version, and `load` reads a truncated record as an entry
    owing nothing — the park silently discharged by the rollback.

    Ablation: replace the `atomic_write_text` call with `pass` and this fails —
    the record keeps this attempt's actions."""
    from bmad_loop import operatoractions

    prior_actions = ["the earlier park's action"]
    engine, record = _park_over_an_earlier_record(project, prior_actions)
    before = record.read_text(encoding="utf-8")

    summary = engine.run()

    assert summary.awaiting_operator == 0 and summary.paused  # the commit really did fail
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert record.read_text(encoding="utf-8") == before  # byte-for-byte
    assert operatoractions.load(project.project)["1-1-a"]["actions"] == prior_actions
    # and it did not raise on the way: the restore reports its own failures
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "park-record-rollback-failed" not in kinds


def test_a_failed_park_record_rollback_is_journaled_not_raised(project, monkeypatch):
    """The restore runs INSIDE the commit window's except arms, so anything it
    raises displaces what those arms exist to carry: the `GitError` arm would
    skip `_escalate` and strand the story in COMMITTING with no diagnosis, and
    the `BaseException` arm would skip its bare `raise` and swap a graceful
    `RunStopped` for a write complaint.

    The oracle is NOT a `pytest.raises` — the whole property is that nothing
    escapes. It is the run's disposition (the failed COMMIT's, not the failed
    rollback's) plus the journal line, because a bare suppress would leave this
    invisible: `validate` reports a board parked with no record, never a record
    left over for a park in no commit.

    `OSError` is the injected type deliberately. Unlike `_restore_deferred_closes`
    this site passes `follow_symlinks=False`, so no `Path.resolve` runs and the
    pre-3.13 `RuntimeError`-on-symlink-loop cannot arise — widening the guard here
    would be copying a fix for a call this one does not make.

    Patched as bound in `engine` and FILTERED BY FILENAME: the module has four
    `atomic_write_text` sites on one binding, so a module-wide boom survives a
    revert of this site and would go green by crashing from a different one.

    Ablation: delete the `self.journal.append` and this fails on the journal row
    alone; delete the whole `except OSError` arm and it fails on `not
    summary.crashed` — the escaping OSError preempts the escalation."""
    from bmad_loop import operatoractions

    engine, record = _park_over_an_earlier_record(project, ["the earlier park's action"])
    real = platform_util.atomic_write_text

    def boom(path, text, *, follow_symlinks=True):
        if Path(path).name == record.name:
            raise OSError(30, "Read-only file system")
        return real(path, text, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("bmad_loop.engine.atomic_write_text", boom)

    summary = engine.run()

    # the disposition is the failed COMMIT's, not the failed rollback's
    assert summary.paused and not summary.crashed
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    reasons = [e["reason"] for e in engine.journal.entries() if e["kind"] == "story-escalated"]
    assert len(reasons) == 1 and reasons[0].startswith("commit failed:")
    # the rollback's own failure is on the record, naming the type — `str(e)`
    # alone cannot say whether the disk or the path was at fault
    failed = [e for e in engine.journal.entries() if e["kind"] == "park-record-rollback-failed"]
    assert len(failed) == 1 and failed[0]["error"].startswith("OSError: ")
    # honest about what did NOT happen: the restore never landed, so the record
    # still claims a park no commit carries. Advisory, journaled, human-attended.
    assert operatoractions.load(project.project)["1-1-a"]["actions"] == ACTIONS


def test_an_unresolvable_spec_path_still_records_the_park(project, monkeypatch):
    """`_park_spec_relpath` resolves inside a try catching `(OSError, ValueError)`,
    and below 3.13 `Path.resolve` reports a symlink loop as `RuntimeError` —
    which is neither. Guarding it INSIDE `_park_spec_relpath` rather than
    leaving it to `_write_park_record`'s outer `(OSError, RuntimeError)` guard
    is what preserves the documented fallback: the record is still written, with
    `spec_file` recorded verbatim, instead of degrading to no record at all.
    Per #356 that matters — a story key does not yield a spec path, so losing
    the record loses the only committed route back to the spec.

    ⚠️ The fault is INJECTED. 3.13/3.14 resolve real symlink loops without
    raising, so a loop-based test is green on this box and red only on the
    3.11/3.12 CI legs. Ablation: revert `_park_spec_relpath`'s except tuple to
    `(OSError, ValueError)` and this fails — the RuntimeError then lands in
    `_write_park_record`'s outer guard, which journals `operator-index-failed`
    and writes nothing. Run it on 3.11 too, since a real-loop version would not
    bite here at all.

    Scoped to the `_write_park_record` window rather than the whole run because
    the spec is resolved on the verify path too: a fault live from the start
    stops the story before it ever parks, which tests nothing about this guard.
    `_park_spec_relpath` has exactly one caller, so the window IS its resolve."""
    from bmad_loop import operatoractions

    engine = _park_engine(project)
    real_resolve = Path.resolve
    real_write = engine._write_park_record

    def unresolvable(self, *a, **kw):
        if self.name.startswith("spec-"):
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real_resolve(self, *a, **kw)

    def write_with_the_fault_live(task):
        with monkeypatch.context() as m:
            m.setattr(Path, "resolve", unresolvable)
            return real_write(task)

    monkeypatch.setattr(engine, "_write_park_record", write_with_the_fault_live)
    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert summary.awaiting_operator == 1 and not summary.crashed and not summary.paused
    # the record survives with the verbatim fallback path — a wrong path a human
    # can see beats a missing one they cannot
    entry = operatoractions.load(project.project)["1-1-a"]
    assert entry["spec_file"] == task.spec_file and entry["actions"] == ACTIONS
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.AWAITING_OPERATOR
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-awaiting-operator" in kinds and "operator-index-failed" not in kinds


def test_a_runtime_error_writing_the_record_degrades_like_an_oserror(project, monkeypatch):
    """Not a duplicate of the guard above: this pins the OUTER except tuple in
    `_write_park_record`. `RuntimeError` stays in it for `_park_spec_relpath`'s
    reason (a symlink-loop `resolve` below 3.13, reachable through any path the
    record write touches) — and the stakes moved with #356: the write now runs
    INSIDE the commit window, so an escaping non-OSError no longer just skips
    the park bookkeeping, it lands in the window's `BaseException` arm and
    aborts the commit of a story that genuinely finished.

    Degrades to the same journal line its `OSError` sibling does. Ablation:
    revert `_write_park_record`'s except tuple to `except OSError` and this
    fails — the commit is aborted and the story never parks."""
    from bmad_loop import operatoractions

    engine = _park_engine(project)

    def boom(*a, **k):
        raise RuntimeError("Symlink loop from '/w/.git'")

    monkeypatch.setattr("bmad_loop.operatoractions.record_park", boom)
    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.AWAITING_OPERATOR and task.commit_sha
    assert summary.awaiting_operator == 1 and not summary.paused and not summary.crashed
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "operator-index-failed" in kinds and "story-awaiting-operator" in kinds
    assert operatoractions.load(project.project) == {}
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.AWAITING_OPERATOR


def test_park_records_a_worktree_absolute_spec_relative_to_the_workspace(project):
    """The branch the guards above fall back FROM, and it was untested. Under
    worktree isolation `task.spec_file` has been rewritten to an absolute path
    inside the unit worktree, and that worktree is torn down moments later —
    indexing it verbatim records a path that is dead before a human reads it.

    Ablation: return `spec_file` unconditionally and this fails — the entry holds
    an absolute path under a directory that no longer exists, and `confirm`
    reports the spec as missing."""
    engine = _park_engine(project)
    task = StoryTask(story_key="1-1-a", epic=1)

    task.spec_file = str(project.project / ".worktrees" / "unit-1" / "docs" / "spec-1-1-a.md")
    assert engine._park_spec_relpath(task) == ".worktrees/unit-1/docs/spec-1-1-a.md"
    # a spec genuinely outside the workspace is recorded as-is, not dropped: a
    # wrong path a human can see beats a missing one they cannot
    task.spec_file = str(project.project.parent / "elsewhere" / "spec-1-1-a.md")
    assert engine._park_spec_relpath(task) == task.spec_file
    # and no spec at all stays empty rather than becoming "."
    task.spec_file = None
    assert engine._park_spec_relpath(task) == ""


def test_park_notifies_with_the_actions_and_the_confirm_command(project):
    """The one artifact that reaches someone who is not looking at the repo. It
    enumerates the actions rather than counting them, and names the command that
    ends the park — a park is non-blocking, so nothing else will ask again.

    Ablation: delete the `_notify_park` call and this fails — the run moves on and
    the human is never told a story is waiting on them."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=ACTIONS
            )
        ],
        policy=_park_policy(),
    )

    engine.run()

    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story awaiting operator: 1-1-a" in attention
    for action in ACTIONS:
        assert action in attention
    assert "bmad-loop confirm 1-1-a" in attention
    # not an escalation: nothing here may read as "the run stopped for you"
    assert "CRITICAL" not in attention


def test_run_summary_render_labels_both_units(project):
    """render() feeds stdout, the ATTENTION file and the desktop notification
    from one place, so this covers all three."""
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=0.5,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    rendered = engine.run().render()

    assert "1,320 weighted tokens (2,320 raw incl. cache reads)" in rendered
    # the bare, unlabeled figure the issue reported must be gone
    assert "2,320 tokens" not in rendered


def test_run_summary_render_untracked_usage_stays_one_plain_zero(project):
    """usage_parser = "none" profiles never report usage. Splitting that into
    "0 weighted tokens (0 raw incl. cache reads)" asserts free work twice."""
    engine = _cache_heavy_engine(project, snapshot_weight=0.5, live_weight=0.5, usage=TokenUsage())
    rendered = engine.run().render()

    assert "0 tokens" in rendered
    assert "weighted" not in rendered
    assert "raw" not in rendered


def test_session_end_journals_weighted_beside_raw(project):
    """Per-session weighted spend must be recoverable from the journal after
    the fact — `tokens` alone is a scalar the weight cannot be backed out of."""
    engine = _cache_heavy_engine(
        project,
        snapshot_weight=0.5,
        live_weight=0.5,
        usage=TokenUsage(
            input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
        ),
    )
    engine.run()

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 2
    for end in ends:
        assert end["tokens"] == 1160
        assert end["tokens_weighted"] == 660  # 100 + 50 + 10 + round(1000 * 0.5)


def _budget_engine(project, script, *, budget, usage=None):
    """A one-story run whose weighted per-session spend is 80k, against `budget`."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    adapter = MockAdapter(
        script,
        usage_per_session=usage
        or TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000),
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # the deferring-story case needs the retry/defer continuation path
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_tokens_per_story=budget),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    return engine, policy


def _budget_entries(engine):
    return [e for e in engine.journal.entries() if e["kind"] == "token-budget-exceeded"]


def test_token_budget_exceeded_journals_weighted(project):
    """The crossing is judged at the session boundary that crossed it — here the
    second of two sessions — and warns exactly once with the story-cumulative
    figure, the cap it was compared against, and an operator-facing notice."""
    engine, _ = _budget_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        budget=100_000,  # < 2 x 80k weighted, > 1 x 80k
    )
    engine.run()

    entries = _budget_entries(engine)
    assert len(entries) == 1
    assert entries[0]["weighted"] == 160_000
    assert entries[0]["total"] == 2 * 620_000
    assert entries[0]["budget"] == 100_000
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story token budget exceeded: 1-1-a" in attention
    assert "160000 weighted tokens" in attention
    assert "advisory" in attention
    assert engine.state.tasks["1-1-a"].token_budget_warned is True


def test_token_budget_warns_on_a_story_that_never_commits(project):
    """The reported overrun (#336) was on a story that DEFERRED. The predecessor
    check ran past `advance(task, Phase.DONE)`, so only committing stories were
    ever judged and this one produced no record at all."""
    engine, _ = _budget_engine(
        project,
        [SessionResult(status="stalled"), SessionResult(status="stalled")],
        budget=100_000,
    )
    summary = engine.run()

    assert summary.deferred == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    entries = _budget_entries(engine)
    assert len(entries) == 1
    assert entries[0]["weighted"] == 160_000


def test_token_budget_warns_once_per_story(project):
    """The latch is per story, not per session: every session AFTER the crossing
    is over the cap too, so without it an overrunning story would re-notify for
    the rest of its life."""
    engine, _ = _budget_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        budget=1,  # every session boundary is over
    )
    summary = engine.run()

    assert summary.done == 1
    entries = _budget_entries(engine)
    assert len(entries) == 1
    # the FIRST crossing, not the run total — the notice is not re-sent per session
    assert entries[0]["weighted"] == 80_000


def test_token_budget_latch_survives_resume(project):
    """The latch is persisted, so the crossing stays a one-time event per story
    rather than per process — a resumed run inherits "already warned"."""
    engine, policy = _budget_engine(project, [dev_effect(project, "1-1-a")], budget=1)

    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        # die in the post-session window, just past the budget note
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].token_budget_warned is True

    resumed, _ = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=True)], policy=policy
    )
    assert resumed.run().done == 1

    # the resumed review session is over the cap on the story's persisted totals;
    # only the inherited latch keeps it quiet.
    assert len(_budget_entries(resumed)) == 1


def test_token_budget_latch_waits_for_the_durable_record(project):
    """The latch is a pure suppression bit — it must never outlive a failed
    journal write. `Journal.append` is unguarded IO, and the run-level `finally`
    persists whatever the task carries on every abort arm, so latching before the
    record would leave a story that overran suppressed forever with nothing
    written anywhere: #336 again, from an ordinary OSError."""
    engine, policy = _budget_engine(project, [dev_effect(project, "1-1-a")], budget=1)

    real_append = engine.journal.append

    def failing_append(kind, **fields):
        if kind == "token-budget-exceeded":
            raise OSError("journal.jsonl is not writable")
        return real_append(kind, **fields)

    engine.journal.append = failing_append
    assert engine.run().crashed
    # the record never landed, so the bit that would suppress it must not have
    assert load_state(engine.run_dir).tasks["1-1-a"].token_budget_warned is False

    engine.journal.append = real_append  # the resumed engine reuses this Journal
    resumed, _ = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=True)], policy=policy
    )
    assert resumed.run().done == 1
    # still owed, so still delivered — one entry, from the resumed session
    assert len(_budget_entries(resumed)) == 1


# ------------------------------ mid-session token-budget guard (#158)


def _with_budget_weighted(effect, weighted):
    """Wrap a scripted effect so its result carries a tripped budget sample —
    the adapter sets budget_weighted on every post-trip exit, completed ones
    (a warn-mode trip, a wrap-up inside the grace) included."""

    def wrapper(spec):
        return dataclasses.replace(effect(spec), budget_weighted=weighted)

    return wrapper


def test_dev_over_budget_retries_then_defers(project):
    """over_budget rides the ordinary non-completed dev arm — RETRY while
    attempts remain, plateau-DEFER once exhausted — with zero escalation.py
    changes (#158)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            SessionResult(status="over_budget", budget_weighted=6_000_000),
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [d["action"] for d in decisions] == ["retry", "defer"]
    assert all("dev session over_budget" in d["reason"] for d in decisions)


def test_review_over_budget_retries_then_defers(project):
    """over_budget from a review session rides the same non-completed arm as
    stalled/crashed: review-retry while cycles remain, DEFER once the review
    budget is spent — zero escalation.py changes (#158)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),  # finalizes spec to done, recommends follow-up
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            SessionResult(status="over_budget", budget_weighted=5_500_000),
            SessionResult(status="over_budget", budget_weighted=6_000_000),  # budget spent
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "review session over_budget" in task.defer_reason
    retries = [e for e in engine.journal.entries() if e["kind"] == "review-retry"]
    assert len(retries) == 2
    assert all("review session over_budget" in r["reason"] for r in retries)


def test_session_end_journals_budget_extras_when_tripped(project):
    """A tripped session's session-end entry carries budget_weighted plus the
    cap and mode it was judged against (policy defaults: 4M / warn);
    untripped sessions carry none."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            _with_budget_weighted(dev_effect(project, "1-1-a"), 4_100_000),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1

    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert [e["status"] for e in ends] == ["over_budget", "completed", "completed"]
    assert ends[0]["budget_weighted"] == 5_000_000
    assert ends[0]["budget"] == 4_000_000
    assert ends[0]["budget_mode"] == "warn"
    # a tripped-but-completed session (warn mode / wrap-up in grace) carries
    # the extras too
    assert ends[1]["budget_weighted"] == 4_100_000
    # the untripped review session carries none
    assert "budget_weighted" not in ends[2]
    # ...but the ordinary usage fields are unconditional, which is the whole
    # distinction: budget_weighted = the guard's sample at trip time (only when
    # tripped); tokens_weighted = the end-of-session total (always, when usage
    # was read). They can legitimately differ on the same entry.
    assert "tokens_weighted" in ends[2]


def test_engine_threads_budget_policy_into_session_spec(project):
    """Every session the engine drives gets the [limits] budget knobs on its
    SessionSpec — the stall_nudges_cap threading pattern."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(
            session_budget_mode="warn",
            max_tokens_per_session=123_456,
            session_budget_grace_s=7,
            cache_read_weight=0.25,
        ),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()

    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    for spec in adapter.sessions:
        assert spec.token_budget == 123_456
        assert spec.token_budget_mode == "warn"
        assert spec.token_budget_grace_s == 7.0
        assert spec.cache_read_weight == 0.25


def test_happy_path(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    assert worktree_clean(project.project)
    assert summary.total_tokens == 30  # 2 sessions x 15
    # No cache reads in the default fixture usage, so weighting is a no-op —
    # pins that the weighted path doesn't distort the ordinary case.
    assert summary.weighted_tokens == 30
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    assert adapter.sessions[0].env["BMAD_LOOP_MODE"] == "1"
    assert adapter.sessions[1].prompt.startswith("/bmad-dev-auto ")


def test_session_env_names_the_out_of_tree_events_dir(project):
    """#494 producer side: every engine-driven session is told where to write its
    hook events, and the answer is the out-of-tree channel — not `<run_dir>/events`,
    which a branch switch, a worktree mount or a rollback can take away mid-run.

    `Engine._run_session`'s env dict is the ONE required producer site: dev,
    review, sweep bundles, stories and injected plugin-workflow sessions are all
    dispatched through it, so both roles below carry the variable from one edit.

    The value is `runs.events_dir_for(project, run_id)` — the same call
    `runsetup.make_adapters` points this run's SignalWatcher at (see
    `test_the_generic_watcher_polls_the_state_root_first_and_the_legacy_dir_too`).
    That agreement is the invariant: a producer and consumer that disagreed would
    leave every Stop unobserved and stall the run to `session_timeout_min`.

    Ablation guard: delete the env entry and this fails; point it at
    `self.run_dir / "events"` and it fails on the value."""
    from bmad_loop import runs

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()

    expected = str(runs.events_dir_for(project.project, engine.run_dir.name))
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    assert {s.env["BMAD_LOOP_EVENTS_DIR"] for s in adapter.sessions} == {expected}
    assert not Path(expected).is_relative_to(project.project)


def test_post_kill_rescued_result_flows_and_journals(project):
    """A result rescued by the adapter's post-kill reconcile (#61) reaches the
    engine as an ordinary completed result — it must flow the completed path
    (reconcile/verify/commit) unchanged, with the extra breadcrumb key riding
    along harmlessly — plus one forensic journal entry, since the rescue is
    otherwise indistinguishable from a live completion."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    inner = dev_effect(project, "1-1-a", followup_review=False)

    def rescued_dev(spec):
        result = inner(spec)
        # what GenericDevAdapter._post_kill_reconcile returns: a synthesized
        # result (status included) stamped with the rescue breadcrumb
        result.result_json["status"] = "done"
        result.result_json["post_kill_reconciled"] = True
        return result

    engine, _ = make_engine(project, [rescued_dev])
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "session-rescued-post-kill" in kinds


def test_inplace_ready_gate_veto_defers_before_any_session(project):
    """A plugin gating pre_ready_gate in non-isolated (in-place) mode — e.g. a
    shared-mode Unity engine waiting on the live Editor — defers the unit via the
    bus veto path before any dev session runs. Proves the engine emits the ready
    gate + honors a veto outside the worktree path, with no engine-specific code."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # a declarative plugin whose blocking pre_ready_gate hook fails -> defer veto
    plug = project.project / ".bmad-loop" / "plugins" / "gate"
    plug.mkdir(parents=True)
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "gate"\napi_version = 1\n'
        "[hooks.pre_ready_gate]\ncmd = 'exit 1'\nblocking = true\n"
    )
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert adapter.sessions == []  # gate vetoed before the dev session
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-deferred" in kinds


def test_review_disabled_skips_review_session(project):
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
    )
    # only a dev session is scripted — no review_effect at all
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.review_cycle == 0
    # exactly one session, and it carries the skip-review signal
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert adapter.sessions[0].env["BMAD_LOOP_SKIP_REVIEW"] == "1"
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-skipped" in kinds
    msg = _head_commit_message(project.project)
    assert "implemented via bmad-loop" in msg and "reviewed" not in msg


def test_review_not_recommended_skips_review_session(project):
    """Default review.trigger = "recommended": when the dev session does NOT set
    followup_review_recommended, the orchestrator skips the separate review
    session, validates the deterministic gates, and commits."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # review.enabled stays True (default); only the trigger gate skips it. No
    # review_effect scripted — the dev session must not provoke a review.
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a", followup_review=False)])
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.followup_review_recommended is False
    assert task.review_cycle == 0
    assert [s.role for s in adapter.sessions] == ["dev"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" in kinds and "review-skipped" in kinds


def test_review_recommended_runs_review_session(project):
    """followup_review_recommended True under the default trigger runs the
    follow-up review pass (bmad-dev-auto re-invoked on the done spec)."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=True),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # dev recommended review → the follow-up pass ran; it converged (the latest
    # pass no longer recommends a further follow-up, so the flag is now False)
    assert engine.state.tasks["1-1-a"].followup_review_recommended is False
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" not in kinds


def test_review_trigger_always_runs_without_recommendation(project):
    """review.trigger = "always" runs the review even when the dev session did
    not recommend a follow-up (pre-#2505 behavior)."""
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        review=ReviewPolicy(enabled=True, trigger="always"),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-not-recommended" not in kinds


def test_generic_dev_path_orchestrator_advances_sprint(project):
    """On the generic bmad-dev-auto path the skill self-finalizes the spec but
    never writes the bmad_loop's sprint board; the orchestrator (B2 seam) is the
    single sprint-status writer and advances the story to match verify_dev."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(project, [generic_dev_effect(project, "1-1-a")], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # the orchestrator advanced sprint-status, not the skill
    assert story_status(project.sprint_status, "1-1-a") == "done"
    # the generic dev invocation form, plus the engine-injected awaiting-operator
    # contract (#335) every dev prompt carries while [operator] enabled
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert adapter.sessions[0].prompt.startswith("/bmad-dev-auto 1-1-a")
    assert "status: awaiting-operator" in adapter.sessions[0].prompt


def test_generic_dev_path_no_sprint_advance_when_spec_unfinalized(project):
    """The sprint write is gated on the spec actually reaching the success
    status. A session that completes but leaves the spec short of done must not
    advance the sprint, and the story defers (verify_dev fails on spec status)."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="in-progress")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert story_status(project.sprint_status, "1-1-a") == "ready-for-dev"


def test_generic_reconcile_advances_stale_frontmatter_done(project):
    """bmad-dev-auto finalized in prose (## Auto Run Result: Status done) but left
    the frontmatter at the template default. The orchestrator reconciles the
    frontmatter before the sprint sync + verify, so completed, tested work reaches
    DONE instead of falsely deferring — and the repair is journaled loudly."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="draft", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert story_status(project.sprint_status, "1-1-a") == "done"
    # the frontmatter on disk was repaired to the success status
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    # and the repair is recorded loudly so the upstream skill quirk stays visible
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "draft" and recon[0]["to"] == "done"


def test_generic_reconcile_advances_in_review_frontmatter_done(project):
    """A session that dies in its step-04 Finalize tail leaves the frontmatter at
    the transient `in-review` marker while the prose `## Auto Run Result` already
    says done (the Lights-Out DW-153 symptom). On the sole generic path in-review is
    never a deliberate terminal — the legacy review-handoff fork is retired — so the
    orchestrator reconciles it to done before the gates, closing the false-defer +
    rollback re-sweep loop instead of discarding completed, tested work."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="in-review", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert story_status(project.sprint_status, "1-1-a") == "done"
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_in_review_preserves_followup_review_true(project):
    """The follow-up review pass MUST still run when a reconciled-from-in-review
    spec carries `followup_review_recommended: true` in its frontmatter. synth drops
    the flag for a non-done spec, so the frontmatter is the only source — reconcile
    re-reads it when advancing to done, so the recommended-trigger gate still sees it
    and re-invokes bmad-dev-auto on the done spec."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def dev(spec):
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        sp = spec_path(project, "1-1-a")
        # Finalize tail died: frontmatter stuck at the transient in-review marker,
        # but the skill wrote the followup flag + terminal prose done first.
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'in-review'\n"
            f"followup_review_recommended: true\nbaseline_commit: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="recommended"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project, [dev, review_effect(project, "1-1-a", clean=True)], policy=pol
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # reconcile advanced in-review → done AND re-attached the frontmatter flag, so
    # the follow-up review pass ran (dev then review session)
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-not-recommended" not in kinds
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_in_review_followup_false_skips_review(project):
    """The mirror case (DW-153's actual shape): a reconciled-from-in-review spec
    with `followup_review_recommended: false` in frontmatter skips the follow-up
    review and commits with the dev session only."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def dev(spec):
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        sp = spec_path(project, "1-1-a")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'in-review'\n"
            f"followup_review_recommended: false\nbaseline_commit: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="recommended"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    # No review_effect scripted: the recommended-trigger gate must skip the review.
    engine, adapter = make_engine(project, [dev], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].followup_review_recommended is False
    assert [s.role for s in adapter.sessions] == ["dev"]
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-not-recommended" in kinds
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"


def test_generic_reconcile_advances_bare_null_frontmatter_status(project):
    """The skill left a bare `status:` (YAML null) but finalized in prose with real
    code. status_of reads that as "" — the same as a missing key — so it is in
    RECONCILABLE_FROM and still advances to done, and the filled line is valid YAML."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy
    from bmad_loop.sprintstatus import story_status
    from bmad_loop.verify import read_frontmatter, rev_parse_head, status_of

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def effect(spec):
        baseline = rev_parse_head(project.project)
        # a real source change so the proof-of-work gate passes
        src = project.project / "src.txt"
        src.write_text(src.read_text() + "real work\n")
        # spec finalized in prose, but frontmatter left at a bare YAML-null status
        sp = spec_path(project, "1-1-a")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus:\nbaseline_revision: '{baseline}'\n---\n\n"
            "## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [effect], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    assert story_status(project.sprint_status, "1-1-a") == "done"
    assert status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "" and recon[0]["to"] == "done"


def test_generic_reconcile_leaves_unknown_custom_status(project):
    """The RECONCILABLE_FROM allowlist's design point: a status token nothing in the
    project writes is a status somebody set on purpose, so a prose `done` never
    overrides it. `reset_spec_status`'s line regex WOULD happily rewrite it, so the
    allowlist is the only thing standing between the deliberate token and the
    repair write — hence the assertion on the untouched bytes."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy

    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        "---\ntitle: 'x'\nstatus: needs-triage\n---\n\n## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    before = sp.read_bytes()

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [generic_dev_effect(project, "1-1-a")], policy=pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "status": "needs-triage"}

    engine._reconcile_generic_terminal_status(task, rj)

    assert sp.read_bytes() == before  # the deliberate token survives
    assert rj["status"] == "needs-triage"  # result dict untouched
    assert "spec-status-reconciled" not in [e["kind"] for e in engine.journal.entries()]


def test_generic_reconcile_skips_out_of_tree_spec(project, tmp_path):
    """Reconcile refuses to mutate a spec the session reports outside the
    orchestrator-owned roots: the file is left untouched and the skip is journaled,
    so a surprising `spec_file` can never be silently rewritten."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy

    outside = tmp_path / "outside" / "spec.md"
    outside.parent.mkdir(parents=True)
    original = "---\ntitle: 'x'\nstatus:\n---\n\n## Auto Run Result\n\n- Status: done\n"
    outside.write_text(original, encoding="utf-8")

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [generic_dev_effect(project, "1-1-a")], policy=pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    engine._reconcile_generic_terminal_status(task, {"spec_file": str(outside)})

    assert outside.read_text() == original  # never written
    kinds = [e["kind"] for e in engine.journal.entries()]
    skipped = [
        e for e in engine.journal.entries() if e["kind"] == "spec-reconcile-skipped-out-of-tree"
    ]
    assert len(skipped) == 1 and skipped[0]["spec"] == str(outside)
    assert "spec-status-reconciled" not in kinds  # no reconcile happened


def test_reconcile_skips_and_journals_on_unreadable_spec(project, monkeypatch):
    """Reconcile is a bookkeeping *observation* pass over a spec the dev skill may
    still be writing. An OSError there used to crash the whole run; it now skips the
    pass and journals `spec-read-failed`. Skipping is safe: the deterministic verify
    gate re-reads the spec straight after and supplies the retry ladder."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "---\ntitle: 'x'\nstatus: 'in-progress'\n---\n\n## Auto Run Result\n\n- Status: done\n"
    )
    sp.write_text(original, encoding="utf-8")
    before = sp.read_bytes()  # snapshot: text-mode write newline-translates on Windows
    task = StoryTask(story_key="1-1-a", epic=1)
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    fault_read_text(monkeypatch, sp)

    engine._reconcile_generic_terminal_status(task, rj)

    assert sp.read_bytes() == before  # never written (repair skipped)
    assert rj["status"] == "in-progress"  # result dict untouched
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1
    assert events[0]["site"] == "reconcile"
    assert events[0]["story_key"] == "1-1-a" and events[0]["spec"] == str(sp)
    assert "PermissionError" in events[0]["error"]
    assert "spec-status-reconciled" not in [e["kind"] for e in engine.journal.entries()]


def test_reconcile_folds_followup_without_reread(project, monkeypatch):
    """`reset_spec_status` rewrites only the frontmatter status line, so a re-read
    after it could only return the followup flag the first read already carried —
    at the cost of a second racy read that can now fail. Exactly one frontmatter
    read, and the flag still folds into the live result dict."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        "---\ntitle: 'x'\nstatus: 'in-progress'\nfollowup_review_recommended: true\n---\n\n"
        "## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    calls, real = [], verify.read_frontmatter

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(verify, "read_frontmatter", counting)
    task = StoryTask(story_key="1-1-a", epic=1)
    rj = {"workflow": "auto-dev", "spec_file": str(sp), "status": "in-progress"}
    engine._reconcile_generic_terminal_status(task, rj)

    assert len(calls) == 1  # the deleted re-read stays deleted
    assert rj["status"] == "done"
    assert rj["followup_review_recommended"] is True  # folded from the single read
    assert verify.status_of(real(sp)) == "done"  # the repair write still happened


def test_post_dev_state_sync_skips_on_unreadable_spec(project, monkeypatch):
    """Same degrade for the sprint-board sync: an unreadable spec must not advance
    the board, and must not crash the run."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123")
    before = project.sprint_status.read_bytes()
    fault_read_text(monkeypatch, sp)

    engine._post_dev_state_sync(StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)})

    assert project.sprint_status.read_bytes() == before  # board not advanced
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "post-dev-sync"
    assert events[0]["story_key"] == "1-1-a"


# ------------------------------------------- closes_deferred auto-resolve (#234)


def _ledger_entries(project) -> dict:
    return {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }


def _vetoing_emit(engine, stage: str, action: str):
    """Wrap `engine._emit` so `stage` resolves to a plugin veto of `action`,
    without a real plugin on disk — `_emit` returns None on the zero-plugin fast
    path, so the context is built the same way the bus would."""
    from bmad_loop.plugins.context import Veto

    original = engine._emit

    def emit(s, task=None, **fields):
        ctx = original(s, task, **fields)
        if s != stage:
            return ctx
        ctx = ctx or engine._make_context(s, task, **fields)
        ctx.add_veto(Veto(action, "test veto", "test-plugin"))
        return ctx

    return emit


def _closes_deferred_run(project, dw_ids, *, ledger=None, **dev_kwargs):
    """A whole sprint-mode story run whose dev session declares `closes_deferred:
    dw_ids`, over a committed ledger. `followup_review=False` so the story takes
    the skip-review path straight to commit — these assert the *lifecycle*
    placement of the close, not the review loop.

    Everything is committed up front and the run is driven end to end, because
    the defect this covers (#234 review) only appears downstream of the dev
    session: the close used to land at dev-sync time, before verification, review
    and commit could still reject the story."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, ledger or {"DW-1": "open"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, closes_deferred=dw_ids, **dev_kwargs)],
    )
    return engine


def test_closes_deferred_marks_declared_entries_done(project):
    """A story spec declaring `closes_deferred:` flips each referenced ledger
    entry to `status: done <date>` + a `resolution:` note when the story commits —
    the write the loop never made, forcing retros to reconstruct closure by hand
    (#234). Closure is declared, never inferred from the diff."""
    engine = _closes_deferred_run(project, ["DW-1"])

    summary = engine.run()

    assert summary.done == 1
    entry = _ledger_entries(project)["DW-1"]
    assert entry.status.startswith("done") and not entry.open
    assert "resolution: resolved by story 1-1-a" in entry.body
    closed = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-closed"]
    assert len(closed) == 1
    assert closed[0]["dw_ids"] == ["DW-1"] and closed[0]["story_key"] == "1-1-a"


def test_closes_deferred_annotation_rides_the_story_commit(project):
    """The annotation must be IN the story's own commit, not left dirty behind it:
    the loop has to end each story on a clean tree (story N+1's step-01 HALTs on a
    dirty one), which is why closure happens just before `finalize_commit`'s
    `git add -A` rather than after the commit."""
    engine = _closes_deferred_run(project, ["DW-1"])

    engine.run()

    committed = git(project.project, "show", "HEAD", "--", str(project.deferred_work))
    assert "+status: done" in committed
    assert "+resolution: resolved by story 1-1-a" in committed
    assert worktree_clean(project.project)


def test_closes_deferred_stays_open_when_verify_defers_the_story(project):
    """The reviewer's repro (#234 review, finding 1). The dev session finalizes
    its spec — declaration and all — but the story then fails deterministic
    verification and defers. Closing at dev-sync time made that permanent: the
    in-place defer path snapshots the ledger AFTER the mutation and restores it
    over the rollback, so a rejected story left the ledger claiming its work
    resolved. At the commit boundary there is nothing to undo."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    # write_src=False: no diff since baseline, so the proof-of-work gate fails and
    # every attempt defers — the spec still reaches `done` with its declaration.
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", write_src=False, closes_deferred=["DW-1"])] * 3,
    )

    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert _ledger_entries(project)["DW-1"].open  # never claimed resolved
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "story-deferred-closed" not in kinds
    assert "story-deferred" in kinds  # the story really did defer


def test_closes_deferred_stays_open_when_the_story_escalates(project):
    """`_escalate` rolls nothing back — it advances to ESCALATED and pauses. A
    close written before that point survives into the operator's checkout, where
    the NEXT story's `git add -A` sweeps it into an unrelated commit."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(
        project, [dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])]
    )
    engine._emit = _vetoing_emit(engine, "pre_commit", "pause")

    summary = engine.run()

    assert summary.paused and summary.done == 0
    assert _ledger_entries(project)["DW-1"].open
    assert "story-deferred-closed" not in {e["kind"] for e in engine.journal.entries()}


def test_closes_deferred_idempotent_on_resume(project):
    """A host death in the commit window re-drives `_finalize_commit_phase` on
    resume, so the close runs twice. The second pass must write nothing and stay
    *silent*: an id that is present-but-already-done is a satisfied declaration,
    not an unmatched one."""
    engine = _closes_deferred_run(project, ["DW-1"])
    engine.run()
    task = load_state(engine.run_dir).tasks["1-1-a"]

    # re-drive the exact commit-phase step a resume performs
    engine._close_declared_deferred(task)

    body = _ledger_entries(project)["DW-1"].body
    assert body.count("resolution: resolved by story 1-1-a") == 1  # not doubled
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert kinds.count("story-deferred-closed") == 1  # only the first pass wrote
    assert "deferred-close-unmatched" not in kinds  # already-done is NOT unknown


def test_closes_deferred_warns_on_unknown_id(project):
    """An id absent from the ledger (a typo, or an entry since reworded) is
    journaled and dropped — never a story failure, and never a ledger write."""
    engine = _closes_deferred_run(project, ["DW-99"])
    before = project.deferred_work.read_bytes()

    summary = engine.run()

    assert summary.done == 1  # the annotation is traceability, not a gate
    assert project.deferred_work.read_bytes() == before  # ledger untouched
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-unmatched"]
    assert len(events) == 1
    assert events[0]["dw_ids"] == ["DW-99"] and events[0]["story_key"] == "1-1-a"
    assert "story-deferred-closed" not in [e["kind"] for e in engine.journal.entries()]


def test_closes_deferred_reports_a_malformed_ledger_entry(project):
    """An entry that exists but carries neither an `open` nor a `done` status
    cannot be marked. Reporting only present-vs-absent left that case in silence,
    which reads to the operator exactly like a successful close (#234 review)."""
    engine = _closes_deferred_run(project, ["DW-1"], ledger={"DW-1": "in-progress"})

    engine.run()

    assert not _ledger_entries(project)["DW-1"].status.startswith("done")
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-malformed"]
    assert len(events) == 1 and events[0]["dw_ids"] == ["DW-1"]


@pytest.mark.parametrize(
    ("first", "second", "closed"),
    [("open", "done 2026-06-01", True), ("done 2026-06-01", "open", False)],
    ids=["open-first", "done-first"],
)
def test_closes_deferred_reports_a_duplicated_ledger_id(project, first, second, closed):
    """One id, two entries: only the first is read and only the first can be
    written, so the second is neither. Both orders used to pass in total silence —
    `classify` indexed the LAST entry while the mutation took the first, so a
    done-first ledger classified the id open and then marked nothing at all
    (#284 round-6 review, finding 4).

    The close itself still behaves as the first entry dictates. What changes is
    that the operator is told the ledger names one id twice (#286)."""
    engine = _closes_deferred_run(project, ["DW-1"])
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        f"### DW-1: the first copy\nstatus: {first}\n\n"
        f"### DW-1: the second copy\nstatus: {second}\n",
        encoding="utf-8",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "duplicate id")

    summary = engine.run()

    assert summary.done == 1  # never a gate
    kinds = [e["kind"] for e in engine.journal.entries()]
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-duplicate-id"]
    assert len(events) == 1 and events[0]["dw_ids"] == ["DW-1"]
    # the first entry decides, and the close is reported only when it happened
    assert ("story-deferred-closed" in kinds) is closed
    parsed = deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    assert [e.status.split()[0] for e in parsed] == (
        ["done", "done"] if closed else ["done", "open"]
    )


def test_closes_deferred_reports_a_wrong_container_declaration(project):
    """`closes_deferred: DW-1` (a scalar, not a list) is a real declaration of
    intent. Reading it as an empty list closed nothing and said nothing, while
    the same mistake in `stories.yaml` was a hard schema error — the two channels
    now agree that it is a mistake worth surfacing."""
    engine = _closes_deferred_run(project, "DW-1")  # bare scalar
    before = project.deferred_work.read_bytes()

    summary = engine.run()

    assert summary.done == 1  # still never a gate
    assert project.deferred_work.read_bytes() == before
    events = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-malformed"]
    assert len(events) == 1  # the one live read, at the commit boundary
    assert "must be a list" in events[0]["error"]


def test_closes_deferred_noop_when_field_absent(project):
    """The default spec declares nothing: no ledger write and no journal noise on
    the close path every ordinary story takes."""
    engine = _closes_deferred_run(project, None)
    before = project.deferred_work.read_bytes()

    engine.run()

    assert project.deferred_work.read_bytes() == before
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert not kinds & {
        "story-deferred-closed",
        "deferred-close-unmatched",
        "deferred-close-malformed",
    }


def test_closes_deferred_refuses_an_out_of_tree_spec(project, tmp_path):
    """The sprint-mode spec path comes from the session's own result.json. A
    stale or hostile absolute path carrying `status: done` + `closes_deferred`
    must not be able to steer a ledger write — the same root-containment rule the
    frontmatter-status reconcile already applies to its one session-supplied
    path (#234 review, finding 4)."""
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(project, [])
    outside = tmp_path / "elsewhere" / "spec.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    write_spec(outside, "done", "abc123", closes_deferred=["DW-1"])
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(outside)

    engine._close_declared_deferred(task)

    assert _ledger_entries(project)["DW-1"].open
    events = [
        e for e in engine.journal.entries() if e["kind"] == "deferred-close-skipped-out-of-tree"
    ]
    assert len(events) == 1 and events[0]["story_key"] == "1-1-a"


def _reject_commits(project):
    """Install a native `pre-commit` hook that rejects every commit — the
    real-world shape of a `finalize_commit` failure (a lint/secret hook saying no)
    that no amount of orchestrator correctness prevents."""
    hooks = project.project / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_closes_deferred_rolls_back_when_the_commit_fails(project):
    """The close is written just BEFORE `finalize_commit` so an in-repo annotation
    rides the story's own commit — but that commit can still fail, and `_escalate`
    unwinds nothing. Left alone the entry claims work that is in no commit, and
    the usual recovery makes it permanent: a resolved re-drive preserves the
    artifact folders' tracked content through `safe_reset` (#284 review, finding 1).
    """
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()
    _reject_commits(project)

    summary = engine.run()

    assert summary.done == 0 and summary.paused  # the commit really did fail
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before  # byte-identical, not just re-opened
    kinds = [e["kind"] for e in engine.journal.entries()]
    # the write happened and was undone: both are on the record, in that order
    assert kinds.index("story-deferred-closed") < kinds.index("deferred-close-rolled-back")


def test_closes_deferred_lands_once_when_a_failed_commit_is_re_driven(project):
    """The rollback must leave the story re-drivable: once the hook is gone, the
    resumed commit phase re-applies the close exactly once (no doubled
    `resolution:` line) and carries it in the commit it belongs to."""
    engine = _closes_deferred_run(project, ["DW-1"])
    hook = _reject_commits(project)
    engine.run()
    hook.unlink()
    # the resolve workflow's re-arm: a resolved re-drive, which is precisely the
    # recovery that PRESERVES the artifact folders' tracked content through
    # `safe_reset` — so a close left standing here would never be reverted.
    rearm_escalation(engine.run_dir)

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )
    summary = resumed.run()

    assert summary.done == 1
    entry = _ledger_entries(project)["DW-1"]
    assert not entry.open
    assert entry.body.count("resolution: resolved by story 1-1-a") == 1
    committed = git(project.project, "show", "HEAD", "--", str(project.deferred_work))
    assert "status: done" in committed


def _external_ledger_paths(project, tmp_path):
    """`project` with its artifact dir configured OUTSIDE the repo — the shared,
    uncommittable ledger configuration `ProjectPaths.rebased` leaves in place."""
    external = tmp_path / "shared-artifacts"
    external.mkdir(exist_ok=True)
    return dataclasses.replace(project, implementation_artifacts=external)


def test_closes_deferred_external_ledger_is_written_and_journaled(project, tmp_path):
    """A ledger outside the repo can ride no commit (`ProjectPaths.rebased`
    deliberately shares an external artifact dir between worktrees, and
    `git add -A` can never stage it). The advisory contract writes it at the
    same commit-boundary moment anyway, and tells the operator the annotation
    is part of no commit (`deferred-close-external-ledger`)."""
    paths = _external_ledger_paths(project, tmp_path)
    write_sprint(paths, {"1-1-a": "ready-for-dev"})
    write_ledger(paths, {"DW-1": "open"}, commit=False)
    engine, _ = make_engine(
        paths,
        [dev_effect(paths, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )

    summary = engine.run()

    assert summary.done == 1
    entry = next(
        e
        for e in deferredwork.parse_ledger(paths.deferred_work.read_text(encoding="utf-8"))
        if e.id == "DW-1"
    )
    assert not entry.open and "resolution: resolved by story 1-1-a" in entry.body
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "deferred-close-external-ledger" in kinds  # told it rides no commit
    assert "story-deferred-closed" in kinds


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("shape", ["dangling", "looping"])
def test_closes_deferred_in_repo_broken_ledger_link_is_an_outage_not_a_typo(project, shape):
    """A broken ledger link must read as an outage, not as an empty ledger: an
    empty read classifies every declared id `unknown`, and the story then
    reports a typo (`unmatched`) for a mount that went away.

    Both shapes land in the same answer by different routes: the loop is
    refused at the read (ELOOP), the dangling link by the symlink check behind
    `FileNotFoundError` — the link existing at all is the evidence a ledger is
    expected there."""
    engine = _closes_deferred_run(project, ["DW-1"])
    ledger = project.deferred_work
    ledger.unlink()
    if shape == "dangling":
        ledger.symlink_to(project.project / "gone-mount" / "deferred-work.md")
    else:
        other = project.project / "ledger-loop"
        ledger.symlink_to(other)
        other.symlink_to(ledger)

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed  # the story still lands
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "deferred-close-ledger-unavailable" in kinds
    assert "deferred-close-unmatched" not in kinds
    assert "story-deferred-closed" not in kinds


def test_closes_deferred_re_reads_the_declaration_at_the_commit_boundary(project, monkeypatch):
    """The spec on disk at the commit is what the story declares. Closing against
    any earlier snapshot instead lets an id the author has since withdrawn be
    marked resolved — a false close, the one outcome this whole path exists to
    prevent — and drops one added in the same edit (#284 follow-up review,
    finding 3). Both directions must follow the final spec."""
    engine = _closes_deferred_run(project, ["DW-1"], ledger={"DW-1": "open", "DW-3": "open"})
    sp = spec_path(project, "1-1-a")
    finalize = engine._finalize_commit_phase

    def edited_after_verification(task):
        # the shape of a review session rewriting the frontmatter, or a human
        # editing the spec while the review loop runs: DW-1 withdrawn, DW-3 added
        write_spec(sp, "done", task.baseline_commit, closes_deferred=["DW-3"])
        return finalize(task)

    monkeypatch.setattr(engine, "_finalize_commit_phase", edited_after_verification)

    summary = engine.run()

    assert summary.done == 1
    entries = _ledger_entries(project)
    assert entries["DW-1"].open  # withdrawn before the commit: never closed
    assert not entries["DW-3"].open  # named by the final spec: closed
    closed = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-closed"]
    assert [e["dw_ids"] for e in closed] == [["DW-3"]]


def test_closes_deferred_rolls_back_when_a_signal_stops_the_commit(project, monkeypatch):
    """A failed commit is not the only way out of the close→commit window. The
    handler `run()` installs raises RunStopped from wherever the main thread is
    standing, and `run()` catches it to finalize a *stopped* run — not to repair
    bookkeeping. Left alone the ledger claims work that is in no commit, exactly
    as a rejecting pre-commit hook did, but past `except verify.GitError`
    (#284 follow-up review, finding 2)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()
    real_finalize = verify.finalize_commit

    def sigterm_mid_commit(*a, **kw):
        # in-process, catchable, and routed through the REAL installed handler —
        # this is the mechanism, not a stand-in for it
        signal.raise_signal(signal.SIGTERM)
        return real_finalize(*a, **kw)  # unreachable: the handler raises first

    monkeypatch.setattr(verify, "finalize_commit", sigterm_mid_commit)

    summary = engine.run()

    assert summary.done == 0
    assert load_state(engine.run_dir).stopped is True  # the stop really landed
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before  # byte-identical
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert kinds.index("story-deferred-closed") < kinds.index("deferred-close-rolled-back")


def test_closes_deferred_rolls_back_when_the_commit_raises_a_non_git_error(project, monkeypatch):
    """An OSError raised *outside* `_run_git` — an FS fault, here the patched
    callee itself — is not the spawn class #343 translates, so it reaches the
    commit boundary as itself and slips past the GitError arm."""
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()

    def cannot_spawn(*a, **kw):
        raise OSError("cannot allocate memory")

    monkeypatch.setattr(verify, "finalize_commit", cannot_spawn)

    summary = engine.run()

    assert summary.crashed  # re-raised untouched: the disposition is not ours
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before


def test_closes_deferred_restores_and_escalates_when_the_commit_spawn_fails(project, monkeypatch):
    """#343: a spawn fault during finalize arrives typed as GitSpawnError (a
    GitError), so the commit window restores the ledger and escalates — the
    same disposition as a git timeout there — instead of crashing the run. The
    raw-OSError crash above remains for the non-spawn class only.

    Ablation target: delete the `except verify.GitError` arm in
    `_finalize_commit_phase` and this fails — the fault falls through to the
    BaseException arm and the run crashes."""
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()

    def cannot_spawn(*a, **kw):
        raise verify.GitSpawnError("git add failed to spawn: [Errno 12] Cannot allocate memory")

    monkeypatch.setattr(verify, "finalize_commit", cannot_spawn)

    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    reasons = [e["reason"] for e in engine.journal.entries() if e["kind"] == "story-escalated"]
    assert len(reasons) == 1 and reasons[0].startswith("commit failed:")
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before  # byte-identical restore


def test_closes_deferred_rolls_back_when_the_journal_fails_after_the_ledger_write(
    project, monkeypatch
):
    """The ledger publishes atomically and the journal line recording it is
    written *after*. A full disk between the two used to leave the entry reading
    `done` with the rollback un-armed: the snapshot was bound from the close's
    RETURN value, which a raise inside it never produces, so the restore had
    nothing to work from and `finalize_commit` never ran either — a resolution
    claimed for a commit that does not exist (#284 round-5 review, finding 1).

    The snapshot is armed before the write now, so the failure unwinds whether it
    lands in the commit or inside the close itself."""
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()
    real_append = engine.journal.append

    def full_disk_recording_the_close(kind, **fields):
        # fault ONLY the close record: the rollback's own journal line has to
        # survive, or the test could not tell a restore from a crash
        if kind == "story-deferred-closed":
            raise OSError("No space left on device")
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", full_disk_recording_the_close)

    summary = engine.run()

    assert summary.crashed
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before  # byte-identical
    rolled = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-rolled-back"]
    assert len(rolled) == 1


def test_closes_deferred_rolls_back_when_a_signal_stops_the_close_itself(project, monkeypatch):
    """The stop signal can land INSIDE the close, not only inside the commit: the
    handler raises from wherever the main thread stands, and the ledger is already
    published by the time `mark_done_many` returns. Nothing downstream runs — no
    close record, no commit — so without an early-armed snapshot the entry stayed
    `done` for work that never landed (#284 round-5 review, finding 1)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine = _closes_deferred_run(project, ["DW-1"])
    before = project.deferred_work.read_bytes()
    real_mark = deferredwork.mark_done_many

    def sigterm_after_publication(*a, **kw):
        marked = real_mark(*a, **kw)  # the flip is on disk now
        signal.raise_signal(signal.SIGTERM)
        return marked  # unreachable: the handler raises first

    monkeypatch.setattr(deferredwork, "mark_done_many", sigterm_after_publication)

    summary = engine.run()

    assert summary.done == 0
    assert load_state(engine.run_dir).stopped is True  # the stop really landed
    assert _ledger_entries(project)["DW-1"].open
    assert project.deferred_work.read_bytes() == before
    kinds = {e["kind"] for e in engine.journal.entries()}
    # the close never got to report itself, and the restore still happened
    assert "story-deferred-closed" not in kinds
    assert "deferred-close-rolled-back" in kinds


def test_failed_rollback_does_not_displace_the_commit_failure(project, monkeypatch):
    """The restore runs INSIDE the commit window's except arms, so anything it
    raises replaces the exception those arms exist to carry: the `GitError` arm
    would skip `_escalate` and strand the story in COMMITTING with no diagnosis,
    and the `BaseException` arm would skip its bare `raise` and swap a graceful
    `RunStopped` for a write complaint.

    `OSError` was too narrow to hold that. `atomic_write_text` resolves the path
    before its own try, and below 3.13 `Path.resolve` reports a symlink loop as
    `RuntimeError` — for a ledger the helper explicitly supports being a symlink,
    and whose OTHER resolve (`_ledger_in_repo`) already catches that type.

    The fault is injected rather than built from a real symlink loop on purpose:
    3.13+ resolves loops without raising, so a loop-based version would pass on
    the interpreter this suite usually runs and only ever fail on the 3.11/3.12
    legs — green here, red in CI, for a guard that was never exercised."""
    engine = _closes_deferred_run(project, ["DW-1"])
    _reject_commits(project)  # the commit fails, so the restore is reached

    def unresolvable(*a, **kw):
        raise RuntimeError("Symlink loop from '/w/deferred-work.md'")

    # patched as bound in `engine` (its sole call site is the restore), NOT in
    # `deferredwork` — the forward close must still publish, or there would be
    # nothing for the rollback to fail at.
    monkeypatch.setattr("bmad_loop.engine.atomic_write_text", unresolvable)

    summary = engine.run()

    # the disposition is the failed COMMIT's, not the failed rollback's
    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.ESCALATED
    reasons = [e["reason"] for e in engine.journal.entries() if e["kind"] == "story-escalated"]
    assert len(reasons) == 1 and reasons[0].startswith("commit failed:")
    # and the rollback's own failure is on the record, naming the type — `str(e)`
    # alone cannot say whether the disk or the path was at fault
    failed = [e for e in engine.journal.entries() if e["kind"] == "deferred-close-rollback-failed"]
    assert len(failed) == 1 and failed[0]["error"].startswith("RuntimeError: ")
    # honest about what did NOT happen: the restore never landed, so the entry
    # still reads `done` for a commit that does not exist. Advisory, journaled,
    # human-attended — not silently papered over.
    assert not _ledger_entries(project)["DW-1"].open


def test_transient_spec_read_fault_does_not_crash_run(project, monkeypatch):
    """Integration capstone for #97. A single transient OSError on the spec — a
    TOCTOU truncation while the dev skill rewrites the file the orchestrator is
    reading back — used to escape to `engine.run()`'s `except Exception` and mark
    the WHOLE RUN crashed, abandoning every remaining story.

    The run now absorbs it: the first read (the reconcile bookkeeping pass) skips
    and journals, every later read succeeds against the real spec, and the story
    lands DONE. One fault, one journal event, no crash."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [generic_dev_effect(project, "1-1-a", followup_review=False)])
    sp = spec_path(project, "1-1-a")
    real, fired = Path.read_text, []

    def raise_once_then_delegate(self, *a, **kw):
        if self == sp and not fired:
            fired.append(self)
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", raise_once_then_delegate)
    summary = engine.run()

    assert fired  # the fault really fired (a green run proves nothing otherwise)
    assert not summary.crashed and summary.done == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "reconcile"


def _crash_replay_setup(project):
    """A host death that leaves the replay-fold's exact preconditions on disk.

    The dev session runs in the reconcile scenario (prose done, frontmatter
    lagging), so its durable record is the pre-reconcile snapshot
    `devcontract.synthesize_result` produces there: status "in-progress" and NO
    `followup_review_recommended` key (only written on a done synth). The host
    dies in the post-session window — phase persists as DEV_RUNNING — but only
    AFTER the original run's reconcile repaired the spec on disk (that write
    lands before the next state save), so the resumed reconcile enters the
    already-finalized branch whose re-fold is the sole carrier of the followup
    flag back onto the replay."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", final_status="in-progress", prose_status="done")

    def snapshot_effect(spec):
        result = inner(spec)
        result.result_json["status"] = "in-progress"
        del result.result_json["followup_review_recommended"]
        return result

    engine, _ = make_engine(project, [snapshot_effect])
    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.phase == Phase.DEV_RUNNING
    assert "followup_review_recommended" not in saved.sessions[0].result_json

    sp = spec_path(project, "1-1-a")
    sp.write_text(
        "---\ntitle: 'test'\ntype: 'feature'\nstatus: 'done'\n"
        f"baseline_revision: '{rev_parse_head(project.project)}'\n"
        "followup_review_recommended: true\n---\n\n## Intent\n\ntest spec\n"
        "\n## Auto Run Result\n\n- Status: done\n\nSummary: test.\n",
        encoding="utf-8",
    )
    return engine, sp


def test_resume_replay_fault_still_routes_recommended_review(project, monkeypatch):
    """The resume counterpart of the capstone above. A replayed dev result is a
    pre-reconcile snapshot with no followup key, and the reconcile re-fold is
    what restores it — a transient read fault used to drop that fold silently.
    The verify gate re-supplies only *status*, so the story committed with its
    recommended follow-up review skipped. Routing now re-derives from the
    finalized spec at consumption (`_followup_from_spec`): the fault costs one
    journal event, not the review."""
    engine, sp = _crash_replay_setup(project)
    real, fired = Path.read_text, []

    def raise_once_then_delegate(self, *a, **kw):
        if self == sp and not fired:
            fired.append(self)
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])
    monkeypatch.setattr(Path, "read_text", raise_once_then_delegate)
    summary = resumed.run()

    assert fired  # the reconcile read really faulted
    assert not summary.crashed and summary.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]  # routed, dev not re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "review-not-recommended" not in kinds
    events = [e for e in resumed.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "reconcile"


def test_resume_replay_persistent_fault_degrades_and_defers(project, monkeypatch):
    """When the fault outlives the routing fallback too, the degrade stays the
    decided one: routing falls back to False (journaled at site
    `followup-routing`), the verify gate's own faulted read turns each attempt
    into a retry, and the attempt budget lands the story in DEFERRED — never a
    crash, never a phantom review."""
    engine, sp = _crash_replay_setup(project)
    resumed, adapter = resume_engine(project, engine, [dev_effect(project, "1-1-a")])
    fault_read_text(monkeypatch, sp)
    summary = resumed.run()

    assert not summary.crashed and summary.done == 0
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DEFERRED
    assert [s.role for s in adapter.sessions] == ["dev"]  # the one budgeted retry
    sites = [e["site"] for e in resumed.journal.entries() if e["kind"] == "spec-read-failed"]
    assert "reconcile" in sites and "followup-routing" in sites


def test_generic_reconcile_idempotent_when_already_done(project):
    """When the skill DID advance the frontmatter to done, reconcile is a no-op:
    no second write, no `spec-status-reconciled` journal entry."""
    from bmad_loop.policy import DevPolicy, ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="done", prose_status="done")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 1
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert recon == []


def test_reset_spec_for_repair_strips_stale_terminal_section(project):
    """Re-arming a self-finalized spec must remove the stale `## Auto Run Result`
    section along with the status flip — find_result_artifact keys on that
    heading, so leaving it would let the re-driven session's first save of the
    spec qualify as a terminal result mid-turn."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        "---\nstatus: done\n---\n\n## Intent\n\nbody\n\n"
        "## Auto Run Result\n\nStatus: done\nAll done.\n",
        encoding="utf-8",
    )
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec))

    engine._reset_spec_for_repair(task)

    text = spec.read_text(encoding="utf-8")
    assert "status: in-progress\n" in text  # re-opened
    assert "Auto Run Result" not in text  # stale terminal section gone
    assert "## Intent\n\nbody\n" in text  # frozen intent untouched


def test_reset_spec_for_repair_lets_an_unwritable_status_raise(project):
    """Pins 283a410: the engine's `reset_spec_status` call sites deliberately let
    `FrontmatterWriteError` propagate rather than swallowing it. Swallowing here
    would dispatch a repair at a charged attempt against a spec still reading
    `done` — step-01 ingests such a spec as context and does not resume, so the
    story re-wedges with nothing on the record. That is exactly why the sibling
    writer in `runs.rearm_escalation` aborts instead of degrading."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    # A block-scalar `status:` reads fine and is unmovable by a line edit — the
    # shape pinned at tests/test_devcontract.py::test_reset_status_refuses_the_
    # shapes_its_own_regex_misreads.
    original = (
        "---\nstatus: |\n  done\n---\n\n## Intent\n\nbody\n\n"
        "## Auto Run Result\n\nStatus: done\nAll done.\n"
    ).encode("utf-8")
    spec.write_bytes(original)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec))

    with pytest.raises(verify.FrontmatterWriteError):
        engine._reset_spec_for_repair(task)

    # The refusal is total: the status flip did not land, and the strip on the very
    # next line never ran, so the stale terminal section is still there.
    assert spec.read_bytes() == original


def test_review_launch_snapshot_threaded_into_session_spec(project):
    """The engine captures a launch-state SpecSnapshot right after the review
    marker strip and threads it onto the review SessionSpec; the dev session that
    precedes it carries none (#276 M1)."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    captured: dict[str, str] = {}

    def capturing_review(spec):
        # The engine captured the snapshot just before launching this session; the
        # spec on disk is still the dev pass's bytes until this effect rewrites it,
        # so hashing it here recomputes exactly what the snapshot recorded.
        sp = spec_path(project, "1-1-a")
        captured["digest"] = hashlib.sha256(sp.read_bytes()).hexdigest()
        return review_effect(project, "1-1-a", clean=True)(spec)

    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a"), capturing_review])
    summary = engine.run()

    assert summary.done == 1
    dev_spec, review_spec = adapter.sessions
    assert dev_spec.role == "dev" and dev_spec.spec_snapshot is None
    snap = review_spec.spec_snapshot
    assert snap is not None
    assert snap.path == str(spec_path(project, "1-1-a"))
    assert snap.fm_status == "done"
    assert snap.sha256 == captured["digest"]


def test_expected_spec_threaded_onto_review_session(project):
    """#261: the engine pins the review session's read-back to the spec it recorded
    on dev success — the same path it hands the session in its prompt — so the
    adapter never mtime-scans the shared artifacts dir. The dev leg that precedes it
    has no recorded spec yet and carries none."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1
    dev_spec, review_spec = adapter.sessions
    assert dev_spec.role == "dev" and dev_spec.expected_spec is None
    assert review_spec.expected_spec == str(spec_path(project, "1-1-a"))
    # It must not silently ride on the snapshot, which degrades to None on a torn
    # read — the two are captured independently.
    assert review_spec.expected_spec == review_spec.spec_snapshot.path


def _pin_probe(project, prompt: str, *, spec_file: str | None, role="dev", label=None):
    """The `expected_spec` a session dispatched with ``prompt`` would carry, for a
    task whose recorded spec is ``spec_file``. Drives the real `_run_session` seam
    and reads what actually reached the adapter."""
    engine, adapter = make_engine(project, [SessionResult(status="crashed")])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=spec_file)
    engine._run_session(task, role=role, prompt=prompt, seq=1, label=label)
    return adapter.sessions[-1].expected_spec


def test_expected_spec_pinned_only_when_the_prompt_names_the_spec(project):
    """#261 pins the read-back to the spec the session owes — and the ONLY thing
    that makes a session owe one is having been pointed at it. Generic sprint
    re-drives now point at their recorded `task.spec_file`; fresh tasks without a
    recorded path remain free to create one, while labeled workflows remain outside
    this read-back contract.

    Both directions are asserted against the REAL prompt builders, so the rule and
    the contract it reads cannot drift apart.

    Ablation: delete the known-spec arm in `_generic_dev_prompt` and the normal
    re-drive prompt becomes bare, so its expected-spec assertion fails with None.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    owed_path = spec_path(project, "1-1-a")
    write_spec(owed_path, "done", "abc123")  # the repair leg re-opens it in place
    owed = str(owed_path)
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed, dispatched_spec_file=owed)
    feedback = project.project / "feedback.md"

    # Pinned: every dispatch that hands the session the path.
    assert _pin_probe(project, engine._review_prompt(task), spec_file=owed) == owed
    assert _pin_probe(project, engine._dev_prompt(task, feedback), spec_file=owed) == owed
    restoring = dataclasses.replace(task, restore_patch="/tmp/attempt.patch")
    assert _pin_probe(project, engine._dev_prompt(restoring, None), spec_file=owed) == owed
    redrive = engine._dev_prompt(task, None)
    assert redrive.startswith(
        f"/bmad-dev-auto Resume the autonomous dev session on the ready-for-dev spec at `{owed}`."
    )
    assert _pin_probe(project, redrive, spec_file=owed) == owed


def test_fresh_sprint_prompt_without_recorded_spec_stays_bare_and_unpinned(project):
    """T19: a fresh sprint task has no spec path to route or pin.

    INVERSE ablation: fabricate a spec filename from the story key before the
    bare-key fallback and this test fails because the prompt names that invented
    path instead of dispatching the bare story key.
    """
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)

    prompt = engine._dev_prompt(task, None)

    assert prompt.startswith(f"/bmad-dev-auto 1-1-a — {BOARD_OWNED}")
    assert "ready-for-dev spec at" not in prompt
    assert _pin_probe(project, prompt, spec_file=None) is None


def test_expected_spec_withheld_from_labeled_workflow_session(project):
    """An injected plugin-workflow session (a TEA pre_commit_gate) runs the generic
    adapter but owes the completion MARKER, not the story spec — and its prompt gets
    the spec path appended to it by nothing, so the naming rule alone would already
    withhold the pin. The explicit `label is None` guard is what keeps that true if a
    plugin's workflow prompt ever quotes the spec path as context.

    Ablation: delete the `label is None` guard and this test fails because the
    labeled session is pinned to the story spec instead of its completion marker.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    owed = str(spec_path(project, "1-1-a"))
    pinned = _pin_probe(
        project,
        f"Run the pre-commit gate against `{owed}`.",
        spec_file=owed,
        label="tea.pre_commit_gate",
    )
    assert pinned is None


# --------------------------------- orchestrator-owned sprint board in prompts (#437)

# The board advance lands right after dev verifies; the story's single commit lands
# only after the review loop — so every session dispatched in between opens on an
# uncommitted, unattributed board change. One read it as a spec violation, reverted
# it, and #334 escalated a story both sessions agreed was finished. These pin the
# clause that names the owner, and its split: the prohibition rides dev prompts too,
# the `blocked` hand-back rides review prompts ONLY (blocked → CRITICAL → run halt).
#
# EVERY presence, ordering and junction constant below is a test-local LITERAL,
# never a call to the builder under test. `"" in s` is True and `s.index("")` is 0,
# so `board in prompt` / `prompt.index(board) < …` / `f"{board} {park}" in prompt`
# all pass vacuously under exactly the ablation they exist to catch. The three
# junction literals each span the END of one clause and the START of the next —
# that is the only shape that can detect a changed separator.

BOARD_OWNED = "sprint-status.yaml is owned by the orchestrator"
BLOCKED_INVITE = "status: blocked and say why"
LEDGER_SENTENCE = (
    "do NOT modify, re-open, or rewrite existing deferred-work ledger entries; "
    "the orchestrator owns their status and resolution."
)
PARK_HEAD = "If this story's acceptance criteria include actions only a HUMAN"
PARK_NEVER_BLOCKED = "Never use the blocked status"
PARK_TAIL = "blocked halts the whole run, and this story is finished as far as you can take it."
LEDGER_BOARD_JOIN = "their status and resolution. sprint-status.yaml is owned by the orchestrator"
BOARD_REDIRECT_JOIN = "not proof that the work is verified. If the story cannot be finished"
BOARD_PARK_JOIN = (
    "not proof that the work is verified. If this story's acceptance criteria "
    "include actions only a HUMAN"
)


def test_sprint_board_instruction_is_a_bare_backtick_free_prohibition(project):
    """The backtick ban is load-bearing: the clause rides AFTER the repair prompt's
    feedback-file pointer, and the last backtick-wrapped token in a dev prompt is by
    convention that path (`_operator_park_instruction` docstring). Non-empty is the
    ablation guard — an empty clause satisfies "no backticks", "no leading
    separator" and every downstream `in prompt` check for entirely the wrong
    reason."""
    engine, _ = make_engine(project, [])
    clause = engine._sprint_board_instruction()

    assert clause.strip()
    assert "`" not in clause
    assert BOARD_OWNED in clause
    # forbids both directions
    assert "never write it" in clause and "never revert a change to it" in clause
    # `_post_dev_state_sync` writes awaiting-operator too, so a clause defending only
    # `done` would leave a parked row reading as an unattributed edit
    assert "done or awaiting-operator" in clause
    # bookkeeping, and explicitly NOT evidence: the row is written before
    # `_verify_dev_artifacts` runs, and a repair session reads this prompt with a red
    # tree under a `done` row
    assert "not a defect to fix" in clause
    assert "not proof that the work is verified" in clause
    # the shared half never says the word: on a dev prompt an invitation to `blocked`
    # would hand a repair session a run-halting early exit
    assert "blocked" not in clause
    # no leading separator — each call site supplies its own (em dash after a bare
    # story key, plain space after a sentence)
    assert clause == clause.strip()
    # the whole-prompt ban at the review seam (`"append" not in prompt.lower()`)
    assert "append" not in clause.lower()


def test_board_handback_redirect_names_blocked_and_stays_a_bare_sentence(project):
    """The review-only half. `blocked` is named on purpose: it is the one status that
    both withholds the commit and reaches a human, where any other non-terminal status
    retries until the budget exhausts into a defer that rolls the work back — and a
    board revert is the #334 dead end itself. The trigger is deliberately narrow: a
    review pass is itself a dev-primitive run whose job is to fix or defer what it
    finds, so "looks unfinished" would be a run-halting early exit on cycle 1 of 3."""
    engine, _ = make_engine(project, [])
    redirect = engine._board_handback_redirect()

    assert redirect.strip()
    assert "`" not in redirect
    assert BLOCKED_INVITE in redirect
    assert "cannot be finished without a human decision" in redirect
    assert "the board is not" in redirect
    assert redirect == redirect.strip()
    assert "append" not in redirect.lower()


def test_board_handback_redirect_is_empty_when_there_is_no_board(project, monkeypatch):
    """The gate lives inside the redirect, not in `_review_prompt`, so the invariant
    "no board ⇒ no redirect away from it" is local and directly assertable rather than
    an emergent property of one caller. `StoriesEngine` is the production instance of
    this; the monkeypatch is the unit form."""
    engine, _ = make_engine(project, [])
    assert engine._board_handback_redirect().strip()  # else this passes for free

    monkeypatch.setattr(engine, "_sprint_board_instruction", lambda: "")

    assert engine._board_handback_redirect() == ""


def test_review_prompt_carries_both_halves_and_keeps_the_ledger_sentence(project):
    """A review session is the one that read the orchestrator's board write as a spec
    violation. It now gets told, and told where to go instead. The deferred-work
    sentence — the same shape of injected ownership clause — must survive verbatim in
    its post-#433 neutral form, and the #261 pin still reads the path out of the
    prompt."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed)

    prompt = engine._review_prompt(task)

    assert prompt.startswith(f"/bmad-dev-auto {owed} — ")
    assert LEDGER_SENTENCE in prompt
    assert BOARD_OWNED in prompt
    assert BLOCKED_INVITE in prompt
    # prohibition first, redirect second: "never do X", then "do Y instead"
    assert prompt.index(BOARD_OWNED) < prompt.index(BLOCKED_INVITE)
    # #433 deleted the affirmative append instruction (the primitive files its own
    # `defer` findings now); nothing injected here may reintroduce the substring
    assert "append" not in prompt.lower()
    assert _pin_probe(project, prompt, spec_file=owed) == owed


def test_review_prompt_joins_both_halves_as_prose(project):
    """Two separators live at this seam, not one — the caller-owned ledger↔board join
    and the board↔redirect join — and every clause here ends in a full stop, so both
    are plain spaces. The dev prompt's em dash in either slot would render
    `...their status and resolution. — sprint-status.yaml is owned by...`, punctuation
    noise in the one prompt this whole change exists for.

    The junction literals are what make this load-bearing: `f"{LEDGER} {BOARD}"` built
    from the builders survives an emptied clause AND survives a changed separator at
    the *other* seam."""
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))

    prompt = engine._review_prompt(StoryTask(story_key="1-1-a", epic=1, spec_file=owed))

    assert LEDGER_BOARD_JOIN in prompt
    assert BOARD_REDIRECT_JOIN in prompt
    assert ". — " not in prompt
    assert "  " not in prompt
    assert prompt == prompt.strip()


def test_stories_review_prompt_shape_is_reached_when_both_clauses_empty(project, monkeypatch):
    """The `if tail else ""` guard on the REVIEW seam is live, not defensive:
    `StoriesEngine` inherits `_review_prompt` and empties both clauses, so production
    reaches the empty-tail branch. Pinned here too at the unit layer — an
    unconditional `f" {tail}"` leaves a trailing space on every stories review
    prompt."""
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed)
    assert BOARD_OWNED in engine._review_prompt(task)  # else the ablation is invisible

    monkeypatch.setattr(engine, "_sprint_board_instruction", lambda: "")

    prompt = engine._review_prompt(task)
    assert prompt == (
        f"/bmad-dev-auto {owed} — do NOT modify, re-open, or rewrite existing "
        f"deferred-work ledger entries; the orchestrator owns their status and "
        f"resolution."
    )
    assert prompt.endswith("their status and resolution.")


def test_board_clause_rides_every_dev_leg_ahead_of_the_park_clause(project, tmp_path):
    """All four `_generic_dev_prompt` legs carry the prohibition, and none of them
    lets it displace the park contract from the end of the prompt or the feedback path
    from the last backticked token. The fresh bare-key and known-spec legs need it as
    much as the others: `rearm_escalation` never touches the board, so a story
    re-dispatched after a resolved escalation may find its row still at `done`.

    Ablation: delete the known-spec prompt arm and the exact ready-for-dev invocation
    fails by falling back to the bare-key head.
    Ablations: bypass the restore arm and its in-review assertion fails; replace the
    repair arm's in-progress wording or delete its evidence sentence and the matching
    repair assertions fail; reverse the clause list and the board-before-park order
    assertion fails.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_spec(spec_path(project, "1-1-a"), "done", "abc123")  # the repair leg re-opens it
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed, dispatched_spec_file=owed)
    assert engine._operator_park_instruction()  # else every ordering check is vacuous
    feedback = tmp_path / "feedback.md"
    feedback.write_text("verification evidence")

    bare = engine._dev_prompt(StoryTask(story_key="1-1-a", epic=1), None)
    explicit = engine._dev_prompt(task, None)
    restore = engine._dev_prompt(dataclasses.replace(task, restore_patch="/tmp/a.patch"), None)
    repair = engine._dev_prompt(task, feedback)

    # after a bare story key the em dash IS the right separator — the one seam
    # neither sentence-joined leg nor the review prompt uses
    assert bare.startswith(f"/bmad-dev-auto 1-1-a — {BOARD_OWNED}")
    assert explicit == (
        f"/bmad-dev-auto Resume the autonomous dev session on the ready-for-dev "
        f"spec at `{owed}`. {engine._sprint_board_instruction()} "
        f"{engine._operator_park_instruction()}"
    )
    assert restore.startswith(f"/bmad-dev-auto Resume review of the in-review spec at `{owed}`.")
    assert "ready-for-dev spec" not in restore
    assert repair.startswith(
        f"/bmad-dev-auto Resume the autonomous dev session on the in-progress spec at `{owed}`."
    )
    assert f"Verification evidence is in `{feedback}`." in repair
    for prompt in (bare, explicit, restore, repair):
        assert BOARD_OWNED in prompt
        assert PARK_HEAD in prompt
        assert prompt.index(BOARD_OWNED) < prompt.index(PARK_HEAD)
        assert BOARD_PARK_JOIN in prompt  # the board→park separator itself
        assert prompt.endswith(PARK_TAIL)  # nothing may be appended after the park
        assert ". — " not in prompt
        assert "  " not in prompt
        assert prompt == prompt.strip()
        assert "append" not in prompt.lower()
    # the last backticked token stays the feedback path
    assert re.findall(r"`([^`]*)`", repair)[-1] == str(feedback)
    assert "`" not in engine._sprint_board_instruction()


def test_no_dev_leg_invites_blocked(project, tmp_path):
    """The redirect is review-only. `blocked` synthesizes a CRITICAL that halts the
    whole run, so on a dev prompt it would hand a repair or `_fix_phase` session a
    sanctioned early exit out of a run that otherwise retries or defers and keeps
    going — and it would contradict the park clause's "Never use the blocked status
    for this" a sentence later in the same prompt.

    The literal is the primary assertion and is provably absent today: the park clause
    says `status: awaiting-operator` and "the blocked status", never `status: blocked`.
    The count is the backstop, and is derived on BOTH sides so it moves with any park
    rewording rather than pinning it."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_spec(spec_path(project, "1-1-a"), "done", "abc123")
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed, dispatched_spec_file=owed)
    park = engine._operator_park_instruction()
    assert park.count("blocked") == 2  # else the count comparison below is vacuous
    feedback = tmp_path / "feedback.md"
    feedback.write_text("verification evidence")

    for prompt in (
        engine._dev_prompt(StoryTask(story_key="1-1-a", epic=1), None),
        engine._dev_prompt(task, None),
        engine._dev_prompt(dataclasses.replace(task, restore_patch="/tmp/a.patch"), None),
        engine._dev_prompt(task, feedback),
    ):
        assert BOARD_OWNED in prompt  # the prohibition IS there — not an empty tail
        assert "status: blocked" not in prompt
        assert BLOCKED_INVITE not in prompt
        # every surviving mention of the word is the park clause forbidding it
        assert prompt.count("blocked") == park.count("blocked")


def test_no_assembled_prompt_mixes_the_park_contract_with_the_blocked_redirect(project, tmp_path):
    """Nothing structural stops a later edit from adding the park clause to
    `_review_prompt` or the redirect to a dev leg, and the two flatly contradict each
    other — park says never use `blocked`, the redirect points at it. Their triggers
    overlap in vocabulary too ("a human decision" vs "actions only a HUMAN can
    perform"), so the separation is worth an assertion of its own."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_spec(spec_path(project, "1-1-a"), "done", "abc123")
    engine, _ = make_engine(project, [])
    owed = str(spec_path(project, "1-1-a"))
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=owed, dispatched_spec_file=owed)
    feedback = tmp_path / "feedback.md"
    feedback.write_text("verification evidence")

    prompts = [
        engine._review_prompt(task),
        engine._dev_prompt(StoryTask(story_key="1-1-a", epic=1), None),
        engine._dev_prompt(task, None),
        engine._dev_prompt(dataclasses.replace(task, restore_patch="/tmp/a.patch"), None),
        engine._dev_prompt(task, feedback),
    ]

    assert any(PARK_NEVER_BLOCKED in p for p in prompts)  # both halves exist somewhere
    assert any(BLOCKED_INVITE in p for p in prompts)
    for prompt in prompts:
        assert not (PARK_NEVER_BLOCKED in prompt and BLOCKED_INVITE in prompt)


def test_board_clause_stands_alone_when_parking_is_disabled(project):
    """`[operator] enabled = false` empties the park clause; the prohibition is
    unconditional and becomes the tail on its own — with no dangling separator where
    the park clause used to start, and still no mention of `blocked` (the park clause
    was the only thing that ever said the word on a dev prompt)."""
    engine, _ = make_engine(
        project, [], policy=_park_policy(operator=OperatorPolicy(enabled=False))
    )

    prompt = engine._dev_prompt(StoryTask(story_key="1-1-a", epic=1), None)

    assert engine._operator_park_instruction() == ""
    assert prompt.startswith(f"/bmad-dev-auto 1-1-a — {BOARD_OWNED}")
    assert prompt.endswith("not proof that the work is verified.")
    assert prompt == prompt.strip()  # no trailing space, no orphaned em dash
    assert "  " not in prompt
    assert "blocked" not in prompt


def test_dev_prompt_carries_no_separator_when_every_clause_is_empty(project, monkeypatch):
    """The `if tail else ""` guard on the DEV seam, pinned. Unlike the review seam's,
    this branch is UNREACHABLE in production: `_generic_dev_prompt` has exactly one
    caller (`Engine._dev_prompt`), both subclasses override `_dev_prompt`, and the
    plugin seam rewrites `proposed_prompt` rather than overriding the method — so on
    the one class that reaches here the prohibition is unconditionally non-empty and
    `tail` is never falsy. `[operator] enabled = false` alone therefore proves nothing
    about the guard; only emptying the board clause too reaches it."""
    engine, _ = make_engine(
        project, [], policy=_park_policy(operator=OperatorPolicy(enabled=False))
    )
    monkeypatch.setattr(engine, "_sprint_board_instruction", lambda: "")

    prompt = engine._dev_prompt(StoryTask(story_key="1-1-a", epic=1), None)

    assert prompt == "/bmad-dev-auto 1-1-a"


@pytest.mark.parametrize("prefix", ["bmad-build-auto-result-", "bmad-dev-auto-result-"])
def test_record_dev_spec_refuses_the_no_spec_fallback_marker(project, prefix):
    """`<primitive>-result-*` is the skill's "intent too unclear to even create a
    spec" artifact. Recording it as the story's spec misroutes every consumer: the
    escalation re-arm flips frontmatter on a marker nothing reads, the repair leg
    re-opens it as the frozen intent contract, and the #261 read-back then pins to
    it — polling a stale marker while the re-drive's real spec goes unread.

    Both eras: this refusal reads `FALLBACK_RESULT_PREFIXES`, and pinning only the
    legacy spelling left the post-rename marker — the one a current project
    actually writes — free to be recorded as the story's spec."""
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    marker = project.implementation_artifacts / f"{prefix}1-1-a-dev-1.md"
    marker.write_text("---\nstatus: blocked\n---\n\nIntent unclear.\n")

    engine._record_dev_spec(task, {"spec_file": str(marker)})
    assert task.spec_file is None

    # A real spec on the same call path is still recorded — this is a filter, not
    # a disabling of the capture escalation resolution depends on.
    real = spec_path(project, "1-1-a")
    write_spec(real, "blocked", "abc123")
    engine._record_dev_spec(task, {"spec_file": str(real)})
    assert task.spec_file == str(real)


def test_review_launch_snapshot_degrades_on_unreadable_spec(project, monkeypatch):
    """A post-strip snapshot fault degrades to None and is journaled.

    The first read belongs to the required stale-result strip and must still
    raise if it fails; fault only the second read, which is the best-effort
    launch snapshot. Ablation: remove that capture's OSError guard and this test
    fails with the injected PermissionError.
    """
    engine, _ = make_engine(project, [])
    bad = project.implementation_artifacts / "spec-1-1-a.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nstatus: done\n---\n\nreview input\n")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(bad))
    real_read_bytes = Path.read_bytes
    reads = 0

    def fail_snapshot_read(path):
        nonlocal reads
        if path == bad:
            reads += 1
            if reads == 2:
                raise PermissionError("snapshot read denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_snapshot_read)

    snap = engine._reset_spec_for_review(task)

    assert snap is None
    assert reads == 2
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert events and events[-1]["site"] == "review-launch-snapshot"
    assert events[-1]["story_key"] == "1-1-a"
    assert "PermissionError" in events[-1]["error"]


def test_review_launch_missing_spec_degrades_like_snapshot_read_failure(project):
    """Strict resolution preserves the documented missing-spec degradation.

    Ablation: remove the FileNotFoundError arm around ``resolve(strict=True)``
    and this test raises the unsafe-path RuntimeError without journaling the read.
    """
    engine, _ = make_engine(project, [])
    missing = project.implementation_artifacts / "missing-review-spec.md"
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(missing))

    assert engine._reset_spec_for_review(task) is None

    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert events and events[-1]["site"] == "review-launch-snapshot"
    assert events[-1]["story_key"] == "1-1-a"
    assert events[-1]["spec"] == str(missing)
    assert "FileNotFoundError" in events[-1]["error"]


def test_review_launch_refuses_directory_before_snapshot_capture(project):
    """Only a trusted regular file can become the review prompt's spec.

    Ablation: remove the review path's ``resolved.is_file()`` guard and this test
    degrades to a snapshot-less launch instead of refusing the directory.
    """
    engine, _ = make_engine(project, [])
    directory = project.implementation_artifacts / "directory-review-spec"
    directory.mkdir(parents=True)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(directory))

    with pytest.raises(RuntimeError, match="before review prompt construction"):
        engine._reset_spec_for_review(task)

    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]


def test_review_launch_snapshot_reads_bare_status_as_blank(project):
    """The snapshot's `fm_status` goes through `status_of` like every other spec
    status gate, so a bare `status:` (YAML null) is captured as "" — not the
    stringified "none".

    The other half of a two-sided contract: the generic adapter's `_observe_tick`
    compares its own `status_of` read against this field. A snapshot carrying
    "none" while the tick reads "" (or the reverse) makes every tick on a
    bare-status spec look like a transition off the launch state — a false
    `transition_proven`, hence a premature single-sighting frontmatter synthesis.
    Tick side pinned by `test_observe_tick_ignores_bare_null_status`."""
    engine, _ = make_engine(project, [])
    spec = spec_path(project, "1-1-a")
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        "---\nstatus:\nbaseline_revision: abc123\n---\n\n# Story\n\nbody\n", encoding="utf-8"
    )
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec))

    snap = engine._reset_spec_for_review(task)

    assert snap is not None
    assert snap.fm_status == ""


def test_generic_reconcile_skips_blocked_prose(project):
    """A blocked outcome (prose Status: blocked) is NEVER reconciled: the
    frontmatter stays non-terminal, no `spec-status-reconciled` is emitted, and the
    story does not falsely pass (it defers via the unfinalized-spec gate)."""
    from bmad_loop.policy import DevPolicy, LimitsPolicy, ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [generic_dev_effect(project, "1-1-a", final_status="draft", prose_status="blocked")],
        policy=pol,
    )
    summary = engine.run()

    assert summary.done == 0  # blocked prose never rides reconcile to a pass
    # reconcile never fired (no journal entry); the unfinalized spec defers as before
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert recon == []


def test_generic_reconcile_does_not_bypass_no_change_gate(project):
    """Reconcile repairs a bookkeeping field, never the proof-of-work gate. A
    session that finalizes in prose (Status: done) but produced NO real code change
    is reconciled to done on disk yet still DEFERS — has_changes_since backstops it,
    so empty work cannot ride the prose marker to PROCEED."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, LimitsPolicy, ReviewPolicy
    from bmad_loop.verify import rev_parse_head

    # Real projects do NOT gitignore the BMAD output tree (`bmad-loop init` only
    # ignores .bmad-loop/runs|cache), so the spec file the skill writes is tracked.
    # The proof-of-work gate excludes the orchestrator-owned artifact folders, so a
    # spec-only edit — including the reconcile rewrite — still reads as "no changes".
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def effect(spec):
        # finalize in prose only; touch NO source file
        baseline = rev_parse_head(project.project)
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "draft", baseline, prose_status="done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [effect], policy=pol)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    # reconcile DID fire (the spec was advanced to done; recorded before the
    # deferral relocates the spec to the archive) ...
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["to"] == "done"
    # ... but the deterministic diff gate still deferred the empty work
    assert "no changes" in (engine.state.tasks["1-1-a"].defer_reason or "")


def test_generic_repair_reopens_spec_before_reinvocation(project):
    """B6: bmad-dev-auto self-finalizes to `done`; its step-01 would route a done
    spec to "ingest as context, don't resume." So before a verify-failure repair
    re-invocation the orchestrator flips the spec back to `in-progress` — the
    repair session must SEE an open spec on entry."""
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.policy import DevPolicy, ReviewPolicy, VerifyPolicy
    from bmad_loop.verify import read_frontmatter, rev_parse_head

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    sp = spec_path(project, "1-1-a")
    marker = project.project / "marker.txt"
    seen_status: list[str] = []
    repair_snapshots: list[bytes] = []
    calls = {"n": 0}

    def effect(spec):
        calls["n"] += 1
        if sp.is_file():  # status the repair session sees on entry
            seen_status.append(str(read_frontmatter(sp).get("status", "")).strip())
            snapshot = load_state(engine.run_dir).tasks["1-1-a"].dispatched_spec_snapshot
            assert snapshot is not None
            assert snapshot == sp.read_bytes()
            repair_snapshots.append(snapshot)
        baseline = rev_parse_head(project.project)
        src = project.project / "src.txt"
        src.write_text(src.read_text() + f"change {calls['n']}\n")
        sp.write_text(
            f"---\ntitle: 'x'\nstatus: 'done'\nbaseline_commit: '{baseline}'\n---\n\n## Intent\n",
            encoding="utf-8",
        )
        if calls["n"] >= 2:  # second pass satisfies the verify command
            marker.write_text("ok\n")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
            },
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        verify=VerifyPolicy(commands=(_file_exists_cmd("marker.txt"),)),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(project, [effect, effect], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    # the repair session saw an in-progress spec, not the finalized `done`
    assert seen_status == ["in-progress"]
    assert len(repair_snapshots) == 1
    # and it was driven by the freeform resume prompt, not /bmad-dev-auto <key>
    assert adapter.sessions[1].prompt.startswith("/bmad-dev-auto Resume the autonomous")


def _head_commit_message(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_finish_kills_session_when_enabled(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    assert engine.state.finished
    assert killed == ["test-run"]


def test_finish_keeps_session_when_disabled(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(cleanup_session_on_finish=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()
    assert engine.state.finished
    assert killed == []


def test_per_stage_adapter_and_model_dispatch(project):
    """Dev and review sessions go to their own adapters with per-stage models."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    dev_mock = MockAdapter([dev_effect(project, "1-1-a")])
    review_mock = MockAdapter([review_effect(project, "1-1-a", clean=True)])
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            review=StageAdapterPolicy(name="codex", model="gpt-5-codex"),
        ),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=dev_mock,
        review_adapter=review_mock,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert [s.role for s in dev_mock.sessions] == ["dev"]
    assert [s.role for s in review_mock.sessions] == ["review"]
    assert dev_mock.sessions[0].model == "opus"
    assert review_mock.sessions[0].model == "gpt-5-codex"


def test_review_loop_converges_within_budget(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, patched=2),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2
    # round 1 (clean=False, still recommends) spent one damping grant; round 2
    # converged on its own (clean=True), so damping never fired — this is normal
    # early convergence, not a damped force-converge.
    assert task.followup_reviews_spent == 1
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_review_strips_stale_auto_run_result_before_each_launch(project):
    """The review leg strips the prior pass's `## Auto Run Result` before every
    launch (issue #160). The dev pass leaves a real terminal marker on the done
    spec; left in place, the review's own entry write lifts it past the adapter's
    launch-mtime floor and the first result-less Stop reads it as this session's
    result — killing the review mid-flight. Each review effect asserts the on-disk
    spec carries no marker at ENTRY; both passes finalize with their own marker, so
    the cycle-2 entry assertion proves the strip runs per launch (not just once)
    while the final pass's marker legitimately survives on the committed spec (no
    later launch strips it)."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def review_cycle1(spec):
        sp = spec_path(project, "1-1-a")
        # the dev pass's marker must be gone by the time this session runs
        assert not devcontract.parse_auto_run_result(sp.read_text()).present
        baseline = _spec_baseline(sp)
        # finalize like review_effect, but leave our OWN terminal marker behind so
        # cycle 2 can prove it too is stripped before the next launch
        write_spec(sp, "done", baseline, prose_status="done")
        set_sprint(project, "1-1-a", "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": True,  # non-clean -> a cycle 2 runs
                "escalations": [],
            },
        )

    def review_cycle2(spec):
        sp = spec_path(project, "1-1-a")
        # cycle 1's own marker must be stripped too — proves per-launch stripping
        assert not devcontract.parse_auto_run_result(sp.read_text()).present
        baseline = _spec_baseline(sp)
        # this converging pass finalizes with its OWN marker, exactly as a real
        # bmad-dev-auto finalize does — nothing strips it after the last launch
        write_spec(sp, "done", baseline, prose_status="done")
        set_sprint(project, "1-1-a", "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": False,  # converges
                "escalations": [],
            },
        )

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", prose_status="done"), review_cycle1, review_cycle2],
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2
    # the FINAL pass's own marker legitimately survives — nothing launches after it,
    # so no strip runs; the strip is a before-launch guard, not a scrub-on-commit
    assert devcontract.parse_auto_run_result(spec_path(project, "1-1-a").read_text()).present


def test_budget_exhausted_finalized_work_commits(project):
    """A finalized story (status: done, sprint done, verify green) whose review
    pass keeps recommending an independent follow-up is COMMITTED when the review
    budget is exhausted — not rolled back. The lingering recommendation is
    re-filed as a fresh open deferred-work entry, and the run records the event."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # pin the damping cap high so this test exercises the max_review_cycles
    # exhaustion path (the damped force-converge has its own tests below).
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
        ),
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 3
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    # the finalized work is committed, not reverted
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-budget-committed" in kinds and "story-deferred" not in kinds
    # the lingering follow-up is preserved as a new open deferred-work entry
    open_entries = [
        e for e in deferredwork.parse_ledger(project.deferred_work.read_text()) if e.open
    ]
    assert any("origin: review-budget-followup" in e.body for e in open_entries)


def test_budget_exhausted_unfinalized_defers(project):
    """Genuine non-convergence: the review never finalizes the spec (status stays
    in-progress, so the post-budget verify gate fails). Budget exhaustion defers
    and rolls the tree back, exactly as before the commit-instead-of-rollback
    safeguard."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "did not converge" in task.defer_reason
    # repo rolled back for the next story
    assert (project.project / "src.txt").read_text() == "original\n"
    assert rev_parse_head(project.project) == task.baseline_commit
    # the in-review spec is stashed out of artifacts into the run dir so a
    # leftover can't confuse the next attempt — the work is kept for the human
    from conftest import spec_path

    assert not spec_path(project, "1-1-a").exists()
    stashed = engine.run_dir / "deferred" / "1-1-a" / "spec-1-1-a.md"
    assert stashed.is_file() and "status: 'in-progress'" in stashed.read_text()
    # #333: the rollback parked the attempt on a recovery ref — run state names it
    # and the notification hands the operator the one command that restores it,
    # instead of leaving them to hunt through `git log --all`.
    ref = task.preserve_ref
    assert ref and ref.startswith("refs/attempt-preserve-dirty/")
    git(project.project, "rev-parse", "--verify", ref)
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    assert f"attempt work parked at `{ref}`" in attention
    assert f'git -C "{project.project}" merge --ff-only {ref}' in attention
    deferred_entry = [e for e in engine.journal.entries() if e["kind"] == "story-deferred"][-1]
    assert deferred_entry["preserve_ref"] == ref


def test_defer_notification_stays_bare_when_nothing_was_parked(project):
    """Ablation target: make `_defer_recovery_note`'s tail unconditional and this
    fails. Over-budget sessions touch no files, so the exhaustion defer's rollback
    finds a clean tree and parks nothing — there is no ref to name, and the notice
    must not advertise one (a `merge --ff-only` onto a ref that was never created
    is worse than silence)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            SessionResult(status="over_budget", budget_weighted=5_000_000),
            SessionResult(status="over_budget", budget_weighted=6_000_000),
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED and task.preserve_ref is None
    assert "rollback-skipped-clean" in [e["kind"] for e in engine.journal.entries()]
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    assert "attempt work parked" not in attention and "merge --ff-only" not in attention


def test_defer_recovery_note_is_uniform_across_ref_families(project):
    """A commits branch and a dirty snapshot both fast-forward, so the recovery
    line is the same command either way — the caller never has to know which
    family parked the attempt."""
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    assert engine._defer_recovery_note(task) == ""
    for ref in ("attempt-preserve/test-run-0badc0de", "refs/attempt-preserve-dirty/test-run-de-1"):
        task.preserve_ref = ref
        note = engine._defer_recovery_note(task)
        assert f"parked at `{ref}`" in note
        assert f'git -C "{project.project}" merge --ff-only {ref}' in note


def test_defer_recovery_note_flags_a_commits_only_park(project):
    """When the dirty snapshot failed, `preserve_ref` names the commits branch
    alone and the reset discarded the rest — the notice must say so. It still
    prints the merge command: the committed half IS recoverable, and scaring the
    operator off a valid recovery would be its own defect.

    Ablation targets: delete the `if task.preserve_partial:` branch and the first
    half fails; make it unconditional and the second half fails."""
    engine, _ = make_engine(project, [])
    ref = "attempt-preserve/test-run-0badc0de"
    task = StoryTask(story_key="1-1-a", epic=1, preserve_ref=ref, preserve_partial=True)

    note = engine._defer_recovery_note(task)
    assert f"attempt COMMITS parked at `{ref}`" in note
    assert "did not survive the rollback" in note
    assert "attempt-worktree-preserve-failed" in note  # names the breadcrumb
    assert f'git -C "{project.project}" merge --ff-only {ref}' in note

    task.preserve_partial = False  # a complete park keeps the unqualified wording
    note = engine._defer_recovery_note(task)
    assert f" — attempt work parked at `{ref}`" in note
    assert "did not survive" not in note and "COMMITS" not in note


def test_budget_exhausted_defer_reason_names_last_status(project):
    """The exhaustion defer reason reflects the last completed pass's real status
    (issue #160). Every review pass leaves the spec non-terminal (in-progress), so
    it never finalizes: the reason must name that status, not the fixed
    'still recommending a follow-up pass' text (which is only true of a finalized
    pass that keeps recommending one)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "in-progress" in task.defer_reason
    assert "recommending a follow-up" not in task.defer_reason


def test_budget_exhausted_refileable_followup_keeps_followup_wording(project):
    """Exhaustion with a refileable follow-up whose rescue verify FAILS still uses
    the 'still recommending a follow-up pass' wording (issue #160). Every pass
    finalizes done + recommends a follow-up (refileable_followup), but the last
    pass's tree breaks the verify gate, so the exhaustion rescue's _verify_review
    fails and the commit-instead-of-rollback is skipped → defer. Because the last
    completed pass really did leave a lingering recommendation, the reason keeps the
    follow-up wording (the in-loop verify never runs for a followup-recommending
    pass, so the broken gate only surfaces in the rescue)."""
    marker = project.project / "review-budget.marker"
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_final_review(spec):
        marker.unlink()  # the last pass's tree no longer passes the verify gate
        return review_effect(project, "1-1-a", clean=False, patched=1)(spec)

    engine, _ = make_engine(
        project,
        [
            dev_with_marker,
            review_effect(project, "1-1-a", clean=False, patched=1),
            review_effect(project, "1-1-a", clean=False, patched=1),
            breaking_final_review,
        ],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
            verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        ),
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "still recommending a follow-up pass" in task.defer_reason


def test_budget_exhausted_finalized_but_verify_failed_wording(project):
    """Exhaustion where the last pass finalized (status: done, no follow-up) but its
    verify gate fails names the finalized-but-verification-failed mode (issue #160).
    The single review pass converges (done, no follow-up), so the in-loop verify
    runs and fails; with max_review_cycles == 1 there is no cycle left to run a fix
    session, so the loop exits and the exhaustion reason reflects last_status 'done'
    with no follow-up claim (refileable_followup is False here)."""
    marker = project.project / "review-verify.marker"
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def converged_but_broken_review(spec):
        marker.unlink()  # converges done, but the tree fails the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    engine, _ = make_engine(
        project,
        [dev_with_marker, converged_but_broken_review],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_review_cycles=1),
            verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        ),
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "finalized but its verification failed" in task.defer_reason
    assert "recommending a follow-up" not in task.defer_reason


def test_budget_exhausted_unreconciled_status_reads_as_unknown(project):
    """A completed pass whose status could not be resolved defers as 'unknown', not
    'no review pass completed' (issue #160). When a review result.json carries no
    `spec_file`, `_reconcile_generic_terminal_status` bails and leaves `rj` with no
    `status`, so `last_status` parses as "" — an empty-but-not-None value that means
    'a pass ran, its status was unreadable'. The defer reason must render that
    honestly (`''` != None), distinguishing it from the no-pass-ran case."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def statusless_review(spec):
        sp = spec_path(project, "1-1-a")
        # deliberately NO "status" and NO "spec_file": the reconcile returns early,
        # so `rj` never gains a status and the loop parses "" (a completed pass with
        # an unreadable/unreconciled status), not None (no pass ran)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "baseline_commit": _spec_baseline(sp),
                "escalations": [],
            },
        )

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), statusless_review, statusless_review, statusless_review],
    )
    summary = engine.run()

    assert summary.deferred == 1
    task = engine.state.tasks["1-1-a"]
    assert "did not converge" in task.defer_reason
    assert "'unknown'" in task.defer_reason
    assert "no review pass completed" not in task.defer_reason


def test_budget_exhausted_failed_review_sessions_defer_not_commit(project):
    """A *failed* final review session must never trigger the commit-instead-of-
    rollback rescue. Dev finalizes the story (status: done, recommends a follow-up),
    but every review session crashes/stalls. On the last cycle the budget is spent,
    so decide_review_session returns DEFER (not RETRY) and the loop rolls the tree
    back — it does not reach (or fire) the budget-exhaustion rescue commit. Locks in
    the invariant that makes a 'final-iteration RETRY commits un-reviewed work' path
    unreachable."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),  # finalizes spec to done, recommends follow-up
            SessionResult(status="crashed"),
            SessionResult(status="stalled"),
            SessionResult(status="crashed"),  # final cycle: budget spent -> DEFER
        ],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "review session" in task.defer_reason  # decide_review_session's DEFER reason
    # rolled back, not committed — and the rescue commit never ran
    assert (project.project / "src.txt").read_text() == "original\n"
    assert rev_parse_head(project.project) == task.baseline_commit
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" in kinds and "review-budget-committed" not in kinds


# -------------------------------- review revokes the sprint sign-off (#334)


def _signoff_revoking_review(project, story_key, *, clean, board="in-progress"):
    """A review pass that finalizes the spec to `done` but writes the sprint board
    back off `done` — the reviewer judging the story unfinished while the spec
    (which the dev pass owns) still claims otherwise."""

    def effect(spec):
        result = review_effect(project, story_key, clean=clean)(spec)
        set_sprint(project, story_key, board)
        return result

    return effect


def test_review_signoff_regression_escalates(project):
    """The reported livelock (#334): the review revokes the sprint sign-off the
    orchestrator recorded at dev time. Nothing re-advances the board, so the
    remaining cycles would replay the same failure and end in a defer + rollback.
    The run pauses on the first one instead — two sessions total.

    Ablation: with the escalate branch deleted from verify_review, the loop
    `continue`s onto review cycle 2, requests a 3rd session the script does not
    have, and the run crashes — `not summary.crashed` and the session-role list
    both fail."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            _signoff_revoking_review(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert not summary.crashed
    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review"]  # no cycle 2
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED and task.review_cycle == 1
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_ESCALATION
    # the paused reason travels to `bmad-loop resolve` — it must name both sides
    assert "revoked the sprint sign-off" in saved.paused_reason
    assert "'in-progress'" in saved.paused_reason
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"][-1]
    assert failed["contradiction"] is True
    # the escalation path never rolls back or defers
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "story-deferred" not in kinds


def test_review_signoff_regression_retry_mode_keeps_legacy_defer(project):
    """`review.on_status_contradiction = "retry"` is the compatibility opt-out:
    the same regression burns all three review cycles and defers + rolls back,
    exactly as the released build does."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    legacy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        review=ReviewPolicy(on_status_contradiction="retry"),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [_signoff_revoking_review(project, "1-1-a", clean=True) for _ in range(3)],
        policy=legacy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.escalated == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED and task.review_cycle == 3
    assert "did not converge" in task.defer_reason
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review", "review"]
    assert (project.project / "src.txt").read_text() == "original\n"  # rolled back
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"]
    assert failed and all(e["contradiction"] is False for e in failed)


def test_review_verify_failure_without_regression_still_retries(project):
    """The new gate must not swallow ordinary review-verify failures: with the
    board still at `done`, a failing verify command keeps its fixable-retry
    routing (repair session, then a fresh review) under the default knob."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # the review's patch broke the gate; the board stays done
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix(spec):
        marker.write_text("ok\n")
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    engine, adapter = make_engine(
        project,
        [dev_with_marker, breaking_review, fix, review_effect(project, "1-1-a", clean=True)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        ),
    )
    summary = engine.run()

    assert summary.done == 1 and summary.escalated == 0 and not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev", "review"]
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"]
    assert failed and all(e["contradiction"] is False for e in failed)


def test_review_signoff_regression_at_rescue_gate_escalates(project):
    """When every cycle recommends its own follow-up, the in-loop verify never
    runs — the budget-exhaustion rescue is the first read of the board. A
    revoked sign-off there must escalate too, not fall through to the "did not
    converge" defer that rolls the work back naming neither side.

    max_followup_reviews=5 keeps damping from firing, so all three cycles stay
    on the recommend-a-follow-up arm and the loop exits by its own bound."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [_signoff_revoking_review(project, "1-1-a", clean=False) for _ in range(3)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=5),
        ),
    )
    summary = engine.run()

    assert not summary.crashed
    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED and task.followup_reviews_spent == 3
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review", "review"]
    assert "revoked the sprint sign-off" in load_state(engine.run_dir).paused_reason
    # journaled under the same kind as the in-loop gates: a consumer keying on
    # `contradiction` must see this path too, not just `story-escalated`
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"]
    assert len(failed) == 1 and failed[0]["contradiction"] is True
    kinds = [e["kind"] for e in engine.journal.entries()]
    # neither the rescue commit nor the exhaustion defer
    assert "review-budget-committed" not in kinds and "story-deferred" not in kinds
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def _damp_policy(max_followup_reviews: int) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_followup_reviews=max_followup_reviews),
    )


def test_followup_damping_converges_at_cap(project):
    """Default damping cap (1): a finalized story whose review keeps recommending
    an independent follow-up converges after honoring exactly ONE self-recommended
    follow-up. Round 1 spends the grant; round 2 (still recommending) is damped →
    verify, refile, commit. The 3rd scripted review never runs, and — being the
    expected steady state — the damped converge stays quiet (no ATTENTION)."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2  # dev + 2 review rounds; the 3rd scripted review unused
    assert task.followup_reviews_spent == 1  # exactly one grant honored
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" in kinds
    assert "review-budget-committed" not in kinds  # not the exhaustion path
    assert "story-deferred" not in kinds
    # the lingering follow-up is preserved as exactly one open DW entry
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1
    # damped convergence is the steady state — no review-budget ATTENTION notice
    # (the always-on run-finished notice is the only thing in the file).
    attention = engine.run_dir / "ATTENTION"
    assert not attention.exists() or "review budget reached" not in attention.read_text()
    # only dev + 2 review sessions ran (3rd scripted review never consumed)
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review"]


def test_followup_damping_cap_zero_converges_immediately(project):
    """Cap 0: the orchestrator never honors a pass's own follow-up. The first
    finalized round that still recommends one is damped immediately — verify,
    refile, commit — after a single review round, with nothing spent."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=False)],
        policy=_damp_policy(0),
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 1  # damped on the very first review round
    assert task.followup_reviews_spent == 0  # cap 0 grants nothing to spend
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" in kinds
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1


def test_nonterminal_rounds_do_not_spend_damping_cap(project):
    """A review round that does NOT finalize the spec (status stays non-terminal)
    consumes a review cycle but never spends a damping grant — only a finalized
    round that still recommends its own follow-up does. A later clean round then
    converges normally with the cap untouched."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, finalized=False),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2  # the non-terminal round still consumed a cycle
    assert task.followup_reviews_spent == 0  # but spent no damping grant
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_verify_fix_rounds_do_not_spend_damping_cap(project):
    """A clean review whose patch breaks the verify gate routes to a dev fix
    session and a fresh review cycle. Those verify-repair cycles are dev work, not
    honored follow-ups — they never spend the damping cap. Convergence lands with
    followup_reviews_spent == 0."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # the review's "patch" broke the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix(spec):
        marker.write_text("ok\n")
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [dev_with_marker, breaking_review, fix, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2 and task.attempt == 2
    assert task.followup_reviews_spent == 0  # verify-repair never spends the cap
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "review-followup-damped" not in kinds


def test_followup_damping_resume_replay_does_not_double_count(project):
    """A host death in the post-session window of the grant-spending review round
    must not double-count the damping spend on resume. The recorded round-1 result
    replays (re-deriving the spend), then round 2 damps and converges: the story
    reaches DONE with followup_reviews_spent == 1 (not 2) and exactly one refiled
    entry — append_entry's open-dedupe keeps a replayed refile from duplicating.
    Modeled on test_resume_final_review_cycle_replays_clean_result."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # cap 1, max_review_cycles 2: round 1 spends the grant, round 2 is damped.
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_review_cycles=2, max_followup_reviews=1),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False),  # round 1: spends the grant
            review_effect(project, "1-1-a", clean=False),  # round 2 (runs on resume): damped
        ],
        policy=policy,
    )
    post_sessions = []

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # crash in round 1's review post-session window
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.REVIEW_RUNNING
    # the round-1 spend had not persisted yet (increment is after the workflow gate,
    # saved only by the next cycle) — so the resume must re-derive it exactly once.
    assert crashed.followup_reviews_spent == 0
    assert crashed.sessions[-1].result_json is not None

    resumed, adapter = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=False)]
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.phase == Phase.DONE
    assert final.review_cycle == 2
    assert final.followup_reviews_spent == 1  # re-derived once, never double-counted
    assert len(adapter.sessions) == 1  # only round 2 re-ran; round 1 was replayed
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text())
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1  # exactly one, even across the crash/replay


def _tail_death_review_effect(paths, story_key, *, followup: bool):
    """A review session that dies between writing terminal prose (## Auto Run
    Result: done) and flipping the frontmatter off the transient ``in-review``
    marker. The spec is left at ``in-review`` with the followup flag written but
    the prose already finalized; the synthesized result the orchestrator sees for
    such a non-done spec reports the (unflipped) frontmatter status and drops the
    followup key. Exercises the review-leg terminal-status reconcile."""

    def effect(spec):
        sp = spec_path(paths, story_key)
        baseline = _spec_baseline(sp)
        flag = "true" if followup else "false"
        sp.write_text(
            f"---\ntitle: 'test'\ntype: 'feature'\nstatus: 'in-review'\n"
            f"baseline_revision: '{baseline}'\nfollowup_review_recommended: {flag}\n---\n\n"
            "## Intent\n\ntest spec\n\n## Auto Run Result\n\n- Status: done\n",
            encoding="utf-8",
        )
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "in-review",  # frontmatter not yet flipped; synth reports it
                # NO followup_review_recommended key: synth drops it for a non-done spec
                "escalations": [],
            },
        )

    return effect


def test_review_leg_reconciles_finalize_tail_death_followup_false(project):
    """A review session that dies in its finalize tail — terminal prose says done
    but the frontmatter is stuck at the transient ``in-review`` marker — is repaired
    by the review leg's terminal-status reconcile (mirroring the dev leg at
    engine.py:1541). Without it the stale ``in-review`` frontmatter would fail the
    review-verify gate and burn a review cycle re-reviewing already-finished work.
    Here the finalized spec no longer recommends a follow-up (frontmatter followup
    false), so the reconciled ``done`` converges the loop on that first review
    round: one cycle, spec repaired to done on disk, one spec-status-reconciled
    (in-review -> done), nothing damped."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), _tail_death_review_effect(project, "1-1-a", followup=False)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 1  # converged on the reconciled first review round
    assert task.followup_reviews_spent == 0  # followup false -> no grant honored
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"
    # the spec on disk was repaired to done
    assert verify.status_of(read_frontmatter(spec_path(project, "1-1-a"))) == "done"
    assert "review-followup-damped" not in [e["kind"] for e in engine.journal.entries()]


def test_review_leg_reconciles_finalize_tail_death_followup_true(project):
    """Finalize-tail death on a review pass that still recommends a follow-up: the
    reconcile advances in-review -> done AND re-attaches the frontmatter's followup
    flag (folded because the key is present), so the pass is treated as a
    finalized-but-still-recommending round rather than being re-reviewed from a
    stale ``in-review``. Under the default damping cap (1) that round spends the
    grant and loops; a second, clean review then converges normally. Three sessions
    total, exactly one spec-status-reconciled (only the tail-death round needed
    repair), and — converging on a clean pass — nothing is damped or refiled."""
    from bmad_loop import deferredwork

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            _tail_death_review_effect(project, "1-1-a", followup=True),  # spends the grant
            review_effect(project, "1-1-a", clean=True),  # round 2 converges
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2
    assert task.followup_reviews_spent == 1  # the reconciled followup-true round spent it
    assert [s.role for s in adapter.sessions] == ["dev", "review", "review"]
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1 and recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"
    kinds = [e["kind"] for e in engine.journal.entries()]
    # normal early convergence on round 2 — not a damped/exhausted force-converge
    assert "review-followup-damped" not in kinds and "review-budget-committed" not in kinds
    ledger = project.deferred_work.read_text() if project.deferred_work.exists() else ""
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(ledger)
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert not open_refiled  # a clean convergence refiles nothing


def test_defer_preserves_deferred_work_additions(project):
    """Review sessions append real knowledge to deferred-work.md; a plateau
    defer's git reset must not erase it."""
    from conftest import git
    from conftest import review_effect as make_review

    project.deferred_work.write_text("# Deferred Work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "seed deferred-work")
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def reviewing_with_defer(spec):
        with project.deferred_work.open("a") as f:
            f.write("\n### DW-1: pre-existing flaky retry\n\nstatus: open\n")
        return make_review(project, "1-1-a", clean=False, patched=1, finalized=False)(spec)

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")] + [reviewing_with_defer for _ in range(3)],
    )
    summary = engine.run()
    assert summary.deferred == 1
    assert "DW-1: pre-existing flaky retry" in project.deferred_work.read_text()


def test_rollback_off_pauses_with_manual_notice(project):
    """Production default (rollback_on_failure=False): a would-be defer reset
    never touches the tree — it pauses with bold manual-recovery instructions."""

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
        policy=policy,
    )
    precious = project.project / "keep-me.txt"
    precious.write_text("precious\n")  # an untracked file the user wants kept
    summary = engine.run()

    assert summary.paused
    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_ESCALATION
    reason = state.paused_reason.lower()
    assert "manual rollback" in reason and "back up" in reason
    assert "failed" not in reason  # a stopped attempt is not described as "failed"
    # the orchestrator left the tree exactly as-is — no reset, nothing deleted
    assert not worktree_clean(project.project)
    assert precious.read_text() == "precious\n"
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-manual-required" in kinds


def test_defer_record_survives_rollback_pause(project):
    """#342: a defer whose rollback pauses (production default: rollback OFF +
    dirty tree) must still land the story-deferred journal entry and the defer
    notification — the pause persists terminal DEFERRED, so resume never
    re-enters _defer and the record is emitted now or never.

    Ablation target: delete the `except RunPaused` arm in Engine._defer and
    this fails (no story-deferred entry, no defer line in ATTENTION)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_ESCALATION
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    kinds = [e["kind"] for e in engine.journal.entries()]
    # the pause's manual-recovery record, then the defer record, then the
    # run-paused entry _run_inner appends while unwinding
    assert (
        kinds.index("rollback-manual-required")
        < kinds.index("story-deferred")
        < kinds.index("run-paused")
    )
    deferred_entry = next(e for e in engine.journal.entries() if e["kind"] == "story-deferred")
    assert deferred_entry["preserve_ref"] == ""  # rollback OFF parks nothing
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    # the louder manual-recovery notice still comes first
    assert attention.index("ACTION REQUIRED") < attention.index("story deferred: 1-1-a")


def test_defer_record_on_snapshot_failure_pause_stays_honest(project, monkeypatch):
    """#342 on the #340 path: rollback ON, commits parked, then the uncommitted
    snapshot fails and the reset is refused. The defer record must land, name
    the parked commits ref, and must NOT reuse _defer_recovery_note's partial
    wording — 'did not survive the rollback' is false here: the reset never ran
    and the tree is untouched.

    Ablation target: delete the `except RunPaused` arm in Engine._defer and
    this fails."""

    def dev_with_commit(spec):
        result = dev_effect(project, "1-1-a")(spec)
        (project.project / "impl.txt").write_text("committed work\n")
        git(project.project, "add", "impl.txt")
        git(project.project, "commit", "-q", "-m", "attempt work")
        return result

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_with_commit]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
    )

    def _fail(*a, **k):
        raise GitError("simulated write-tree failure")

    monkeypatch.setattr("bmad_loop.verify.snapshot_worktree", _fail)
    summary = engine.run()

    assert summary.paused
    deferred_entry = next(e for e in engine.journal.entries() if e["kind"] == "story-deferred")
    # the committed half IS parked — the record names it factually
    assert deferred_entry["preserve_ref"].startswith("attempt-preserve/")
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    assert "uncommitted work could not be auto-preserved" in attention
    # the pause-aware note, not the standard one: nothing was destroyed
    assert "the tree was NOT rolled back" in attention
    assert "did not survive the rollback" not in attention


def test_rollback_on_preserves_preexisting_untracked(project):
    """With rollback_on_failure=True the auto-rollback reverts tracked changes
    but never deletes untracked files that predate the attempt."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [
            review_effect(project, "1-1-a", clean=False, patched=1, finalized=False)
            for _ in range(3)
        ],
        policy=policy,
    )
    precious = project.project / "user-notes.txt"
    precious.write_text("keep me\n")  # untracked, present before baseline capture
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert rev_parse_head(project.project) == task.baseline_commit  # tracked reverted
    assert precious.read_text() == "keep me\n"  # pre-existing untracked survives


def test_rollback_or_pause_skips_clean_tree(project):
    """When the attempt left nothing in the tree (HEAD == baseline, no run-created
    untracked files), there is nothing to roll back: even with auto-rollback OFF
    the orchestrator neither pauses nor touches the tree — it just journals and
    returns, so resume can proceed."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)  # clean tree at baseline
    task.baseline_untracked = []

    engine._rollback_or_pause(task)  # must NOT raise RunPaused

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-skipped-clean" in kinds
    assert "rollback-manual-required" not in kinds


def test_rollback_gate_git_fault_pauses_instead_of_crashing(project, monkeypatch):
    """#156: the dirty check timing out (now a GitError) must degrade to
    assume-dirty, so with rollback OFF the run pauses on the manual-recovery
    notice with the tree untouched — it must never crash the run."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    head = rev_parse_head(repo)

    def timing_out(*args, **kwargs):
        raise GitError(f"git diff timed out after 120s in {repo}")

    monkeypatch.setattr(verify, "attempt_dirty", timing_out)

    with pytest.raises(RunPaused):
        engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == head  # tree untouched
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-dirty-check-failed" in kinds
    assert "rollback-skipped-clean" not in kinds


def test_rollback_gate_git_fault_still_auto_recovers_when_on(project, monkeypatch):
    """rollback ON: an un-determinable dirty check still takes the auto-recover
    branch (preserve + reset) — the degrade must not turn the pause-free path
    into a pause."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed implementation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")

    def timing_out(*args, **kwargs):
        raise GitError(f"git diff timed out after 120s in {repo}")

    monkeypatch.setattr(verify, "attempt_dirty", timing_out)

    engine._rollback_or_pause(task)  # must not raise

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-dirty-check-failed" in kinds
    assert "rollback-auto" in kinds


def test_engine_applies_git_timeout_from_policy(project):
    """Engine construction pushes limits.git_timeout_s into the verify module so
    every git helper honors the operator's bound."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(git_timeout_s=7),
    )
    try:
        make_engine(project, [], policy=policy)
        assert verify._git_timeout_s == 7
    finally:
        verify.configure_git_timeout(verify.GIT_TIMEOUT_S)


def test_manual_recovery_wording_stopped(project):
    """Only the stopped/abandoned path reaches the manual-recovery notice now (a
    resolved escalation auto-recovers instead). The notice never claims the story
    'failed' and names the real cause."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    baseline = rev_parse_head(project.project)

    with pytest.raises(RunPaused) as stopped:
        engine._pause_for_manual_recovery(task, baseline)

    assert "failed" not in stopped.value.reason
    assert "manual rollback" in stopped.value.reason.lower()
    assert "attempt was stopped" in stopped.value.reason


def test_manual_recovery_notice_names_committed_work(project):
    """#100: rollback OFF + an attempt that COMMITTED above baseline. The pause
    notice must lead with saving/checking the commits — which may already be
    pushed — never with a bare `git reset --hard` that would discard them."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)
    task.baseline_untracked = []
    src = project.project / "src.txt"
    src.write_text(src.read_text() + "committed attempt work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "attempt commit")
    attempt_head = rev_parse_head(project.project)

    with pytest.raises(RunPaused) as paused:
        engine._rollback_or_pause(task)

    assert rev_parse_head(project.project) == attempt_head  # tree untouched
    reason = paused.value.reason
    assert "failed" not in reason  # a stopped attempt is not described as "failed"
    assert f"{task.baseline_commit[:12]}..HEAD" in reason
    assert "intact" in reason
    assert "pushed" in reason
    # save-the-commits comes before any reset instruction
    assert reason.index("branch my-rescue") < reason.index("reset --hard")
    manual = [e for e in engine.journal.entries() if e["kind"] == "rollback-manual-required"]
    assert manual and manual[-1]["commits"] == 1


def test_manual_recovery_notice_probe_failure_falls_back(project, monkeypatch):
    """The commits probe is advisory: a git fault must neither block the pause
    nor change the classic no-commits notice."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1)
    baseline = rev_parse_head(project.project)

    def boom(repo, base):
        raise GitError("probe failed")

    monkeypatch.setattr(verify, "commits_above", boom)
    with pytest.raises(RunPaused) as paused:
        engine._pause_for_manual_recovery(task, baseline)

    assert "attempt was stopped" in paused.value.reason
    assert "manual rollback" in paused.value.reason.lower()


def test_rollback_preserves_committed_attempt_work(project):
    """rollback_on_failure ON + an attempt that committed its work: the hard reset
    parks those commits under a recovery ref instead of orphaning them."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed implementation\n")  # attempt commits its work
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-commits-preserved")
    assert git(repo, "rev-parse", entry["ref"]).strip() == attempt_head  # reachable by name


def test_rollback_emits_pre_and_post_around_reset(project):
    """A plugin (the Unity engine) hooks pre_rollback / post_rollback so it can
    quiesce a live Editor before the hard reset rewrites tracked files under it, and
    refresh after. pre_rollback must fire while the attempt tree is still checked out
    (HEAD == attempt); post_rollback only after the reset landed (HEAD == baseline)."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed implementation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    seen: list[tuple[str, str]] = []
    original_emit = engine._emit

    def spying_emit(stage, *args, **kwargs):
        if stage in ("pre_rollback", "post_rollback"):
            seen.append((stage, rev_parse_head(repo)))
        return original_emit(stage, *args, **kwargs)

    engine._emit = spying_emit
    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit
    # pre fires before the reset (attempt tree still open), post after it landed
    assert seen == [
        ("pre_rollback", attempt_head),
        ("post_rollback", task.baseline_commit),
    ]


def test_rollback_emits_are_observe_only(project):
    """The rollback emits are observe-only, like pre_worktree_teardown: the returned
    ctx is never routed through ``_vetoed``, so a failed Editor quiesce can never
    block or pause the rollback."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted tracked edit\n")

    routed: list = []
    original_vetoed = engine._vetoed

    def spying_vetoed(ctx, t):
        routed.append(ctx)
        return original_vetoed(ctx, t)

    engine._vetoed = spying_vetoed
    engine._rollback_or_pause(task)  # must not raise / pause

    assert rev_parse_head(repo) == task.baseline_commit  # reset still happened
    assert routed == []  # the rollback emits were never handed to the veto router


def test_rollback_preserves_uncommitted_attempt_worktree(project):
    """rollback_on_failure ON + an attempt that left work UNcommitted: before the
    hard reset (and its untracked cleanup) the engine parks the uncommitted diff —
    both the tracked edit and the run-created untracked file — under a recovery ref,
    so a re-drive never restarts from zero and nothing is silently destroyed."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted tracked edit\n")  # tracked, never committed
    (repo / "new_test.txt").write_text("uncommitted new file\n")  # run-created untracked

    engine._rollback_or_pause(task)  # rollback ON: resets to baseline

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    assert (repo / "src.txt").read_text() == "original\n"  # tracked edit reverted...
    assert (repo / "new_test.txt").exists() is False  # ...untracked cleanup removed the new file
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved")
    ref = entry["ref"]  # ...but both are recoverable from the parked snapshot
    # (conftest `git` strips, so compare against the newline-free blob content)
    assert git(repo, "show", f"{ref}:src.txt") == "uncommitted tracked edit"
    assert git(repo, "show", f"{ref}:new_test.txt") == "uncommitted new file"


def test_rollback_preserves_distinct_refs_across_repeated_dirty_rollbacks(project):
    """Two dirty rollbacks against the SAME baseline_commit (mimicking the dev retry
    loop, where baseline_commit is fixed) must each park their uncommitted work under
    a DISTINCT recovery ref — keyed on task.attempt — so the 2nd rollback cannot
    orphan the 1st attempt's snapshot. Both parked snapshots stay recoverable by name
    with their own attempt's edit."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []

    # cycle 1 (attempt 0): dirty the tree, roll back
    task.attempt = 0
    (repo / "src.txt").write_text("attempt 0 edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    assert (repo / "src.txt").read_text() == "original\n"

    # cycle 2 (attempt 1): SAME baseline, dirty again, roll back
    task.attempt = 1
    (repo / "src.txt").write_text("attempt 1 edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit
    assert (repo / "src.txt").read_text() == "original\n"

    refs = [e["ref"] for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved"]
    assert len(refs) == 2
    assert len(set(refs)) == 2  # distinct — the 2nd rollback did not overwrite the 1st
    # both snapshots remain reachable and carry their own attempt's uncommitted edit
    assert git(repo, "show", f"{refs[0]}:src.txt") == "attempt 0 edit"
    assert git(repo, "show", f"{refs[1]}:src.txt") == "attempt 1 edit"


def test_rollback_preserve_ref_unique_when_attempt_counter_repeats(project):
    """runs.rearm_escalation resets task.attempt to 0, and a resolve session that
    commits nothing leaves HEAD at the same baseline — so the post-resolve re-drive's
    rollback recomputes the exact {slug}-{baseline}-{attempt} ref name of the
    pre-resolve rollback. The engine must probe for a free name instead of trusting
    the counter: the 2nd rollback may never overwrite the 1st attempt's snapshot."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []

    # pre-resolve rollback: attempt 1 escalates dirty
    task.attempt = 1
    (repo / "src.txt").write_text("first arming edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit

    # rearm_escalation resets the counter; the re-drive's first retry lands on the
    # SAME attempt number against the SAME baseline (resolve committed nothing)
    task.attempt = 1
    (repo / "src.txt").write_text("re-drive edit\n")
    engine._rollback_or_pause(task)
    assert rev_parse_head(repo) == task.baseline_commit

    refs = [e["ref"] for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved"]
    assert len(refs) == 2
    assert len(set(refs)) == 2  # the colliding name was suffixed, not overwritten
    assert git(repo, "show", f"{refs[0]}:src.txt") == "first arming edit"
    assert git(repo, "show", f"{refs[1]}:src.txt") == "re-drive edit"
    assert refs[1] == f"{refs[0]}-r2"  # deterministic probe-and-suffix shape

    # a third collision keeps escalating the serial instead of clobbering -r2
    task.attempt = 1
    (repo / "src.txt").write_text("third edit\n")
    engine._rollback_or_pause(task)
    refs = [e["ref"] for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved"]
    assert refs[2] == f"{refs[0]}-r3"
    assert git(repo, "show", f"{refs[2]}:src.txt") == "third edit"


def test_rollback_preserve_ref_slug_survives_a_ref_illegal_run_id(project):
    """A `--run-id` carrying ref-illegal sequences must not drop the recovery ref.
    Characterization for the safe_ref_segment swap — the old inline alnum/`_-` slug
    also kept the ref legal; what this pins is the invariant (real git accepts the
    slugged ref and the snapshot stays reachable) plus the new digest-suffixed shape."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    engine.state.run_id = "story/1:2..3@{now}.lock"
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted edit\n")

    engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == task.baseline_commit  # reset happened
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserved")
    ref = entry["ref"]
    assert ref.startswith("refs/attempt-preserve-dirty/story_1_2__3_{now}.lock-")
    assert git(repo, "show", f"{ref}:src.txt") == "uncommitted edit"  # real git resolves it


def test_run_start_prunes_excess_preserve_refs(project):
    """Run start with scm.preserve_keep set and more attempt-preserve/* refs than
    the budget: the tail is deleted before the loop, only preserve_keep refs
    survive, and the deletions are journalled."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    for i in range(3):
        (repo / "impl.txt").write_text(f"parked attempt {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"attempt {i}")
        git(repo, "branch", "-f", f"attempt-preserve/run-{i}")
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    remaining = git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/attempt-preserve/"
    ).splitlines()
    assert len(remaining) == 2  # tail pruned down to the budget
    entry = next(e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-pruned")
    assert entry["count"] == 1
    assert set(entry["refs"]) | set(remaining) == {f"attempt-preserve/run-{i}" for i in range(3)}


def test_run_start_prune_failure_journals_and_run_proceeds(project, monkeypatch):
    """A failing prune at run start is journalled and never blocks the run —
    the recovery refs are a housekeeping concern, not run state."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _fail(*a, **k):
        raise GitError("simulated for-each-ref failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "simulated for-each-ref failure" in entry["error"]
    assert engine.state.finished  # the prune failure never blocked or crashed the run


def test_run_start_partial_prune_journals_deletions_and_failure(project, monkeypatch):
    """A partial prune (some refs deleted before one stuck) journals BOTH the
    structured deletions and the failure — the destructive half of a stuck
    prune must never be auditable only via the error string."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=1),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _partial(*a, **k):
        raise PrunePreserveError(
            "one ref stuck",
            deleted=["attempt-preserve/gone"],
            failed=["attempt-preserve/stuck (checked out)"],
        )

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _partial)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    pruned = next(e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-pruned")
    assert pruned["count"] == 1 and pruned["refs"] == ["attempt-preserve/gone"]
    failed = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "one ref stuck" in failed["error"]
    assert engine.state.finished


def _park_dirty_snapshots(repo, count):
    """Write `count` refs/attempt-preserve-dirty/* snapshot refs, each on its
    own commit so committer-date ordering is well defined."""
    for i in range(count):
        (repo / "impl.txt").write_text(f"snapshot {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"snapshot {i}")
        git(repo, "update-ref", f"refs/attempt-preserve-dirty/run-{i}", "HEAD")


def test_run_start_prunes_excess_dirty_snapshot_refs(project):
    """Run start with scm.preserve_keep set and more attempt-preserve-dirty
    snapshot refs than the budget: the tail is deleted before the loop, only
    preserve_keep refs survive, and the deletions are journalled under the
    family's own event kind."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    _park_dirty_snapshots(repo, 3)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    remaining = git(
        repo, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/"
    ).splitlines()
    assert len(remaining) == 2  # tail pruned down to the budget
    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-pruned"
    )
    assert entry["count"] == 1
    assert set(entry["refs"]) | set(remaining) == {
        f"refs/attempt-preserve-dirty/run-{i}" for i in range(3)
    }


def test_run_start_dirty_prune_failure_journals_and_run_proceeds(project, monkeypatch):
    """A failing dirty-snapshot prune at run start is journalled under its own
    event kind and never blocks the run."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=2),
    )
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only

    def _fail(*a, **k):
        raise GitError("simulated dirty for-each-ref failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_dirty_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-prune-failed"
    )
    assert "simulated dirty for-each-ref failure" in entry["error"]
    assert engine.state.finished  # the prune failure never blocked or crashed the run


def test_run_start_branch_prune_failure_does_not_skip_dirty_prune(project, monkeypatch):
    """A failing branch-family prune must not skip the dirty family: with excess
    dirty snapshot refs present, the branch failure is journalled AND the dirty
    tail is still pruned and journalled."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True, preserve_keep=1),
    )
    repo = project.project
    write_sprint(project, {"epic-1": "backlog"})  # nothing actionable: run start + finish only
    _park_dirty_snapshots(repo, 2)

    def _fail(*a, **k):
        raise GitError("simulated branch prune failure")

    monkeypatch.setattr("bmad_loop.verify.prune_preserve_refs", _fail)
    engine, _ = make_engine(project, [], policy=policy)

    engine.run()

    failed = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-prune-failed"
    )
    assert "simulated branch prune failure" in failed["error"]
    pruned = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-preserve-dirty-pruned"
    )
    remaining = git(
        repo, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/"
    ).splitlines()
    assert pruned["count"] == 1 and len(remaining) == 1  # pruned down to the budget
    assert set(pruned["refs"]) | set(remaining) == {
        f"refs/attempt-preserve-dirty/run-{i}" for i in range(2)
    }
    assert engine.state.finished


def test_rollback_worktree_preserve_failure_journals_git_error(project, monkeypatch):
    """When the uncommitted-work snapshot can't be captured, the re-drive path still
    resets (pause-free by contract) but journals the underlying git error, so a
    post-mortem can see WHY preservation failed — not just that it did.

    Driven through `cause="resolved"` since #340: a plain rollback now refuses this
    reset rather than destroying the uncommitted edit, so the re-drive is where the
    journal-and-proceed behavior survives."""
    engine, _ = make_engine(project, [])
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted edit\n")  # dirty but uncommitted; no commits

    def _fail(*a, **k):
        raise GitError("simulated commit-tree failure")

    monkeypatch.setattr("bmad_loop.verify.snapshot_worktree", _fail)

    engine._rollback_or_pause(task, cause="resolved")  # journals + proceeds, never raises

    assert rev_parse_head(repo) == task.baseline_commit  # reset still happened
    entry = next(
        e for e in engine.journal.entries() if e["kind"] == "attempt-worktree-preserve-failed"
    )
    assert "simulated commit-tree failure" in entry["error"]  # underlying git detail preserved


def test_rollback_refuses_reset_when_the_dirty_snapshot_fails(project, monkeypatch):
    """#340 end-to-end through a real Engine: an in-place auto-rollback whose dirty
    snapshot fails pauses instead of resetting, leaving the uncommitted work on disk
    for the operator to rescue. The engine-level twin of the RecoveryFlow seam test.

    Ablation target: delete the `pause_for_manual_recovery(..., snapshot_failed=True)`
    call from preserve_attempt_worktree's `except` and this fails."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "src.txt").write_text("uncommitted edit\n")

    def _fail(*a, **k):
        raise GitError("simulated write-tree failure")

    monkeypatch.setattr("bmad_loop.verify.snapshot_worktree", _fail)

    with pytest.raises(RunPaused) as paused:
        engine._rollback_or_pause(task)

    assert (repo / "src.txt").read_text() == "uncommitted edit\n"  # NOT reset
    reason = paused.value.reason
    assert "uncommitted work could not be auto-preserved" in reason
    assert "rollback-manual-required" in [e["kind"] for e in engine.journal.entries()]


def test_rollback_pauses_when_preserve_fails(project, monkeypatch):
    """Safety invariant: if the recovery ref can't be created while commits exist,
    the engine pauses for manual recovery rather than resetting past the work — and
    the notice names the at-risk commits instead of the misleading rollback-OFF
    'just reset --hard' wording."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("committed work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attempt work")
    attempt_head = rev_parse_head(repo)

    def _fail(*a, **k):
        raise GitError("simulated branch failure")

    monkeypatch.setattr("bmad_loop.verify.preserve_commits", _fail)

    with pytest.raises(RunPaused) as paused:
        engine._rollback_or_pause(task)

    assert rev_parse_head(repo) == attempt_head  # NOT reset — work left intact
    reason = paused.value.reason.lower()
    assert "commit" in reason  # notice names the at-risk committed work
    assert "auto-rollback is off" not in reason  # never the misleading OFF wording (rollback is ON)


def test_resolved_redrive_never_pauses_when_preserve_fails(project, monkeypatch):
    """A resolved re-drive is contractually pause-free. Even if the recovery ref
    can't be created for the attempt's commits, it journals the failure and lets
    the reset proceed — unlike the general rollback path, which pauses."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # OFF: only the resolved path resets here
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    (repo / "impl.txt").write_text("failed attempt work\n")  # committed, outside artifacts
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "failed attempt")

    def _fail(*args, **kwargs):
        raise GitError("simulated branch failure")

    monkeypatch.setattr("bmad_loop.verify.preserve_commits", _fail)

    engine._rollback_or_pause(task, cause="resolved")  # must NOT raise RunPaused

    assert rev_parse_head(repo) == task.baseline_commit  # reset proceeded, not paused
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "attempt-preserve-failed" in kinds
    assert "rollback-manual-required" not in kinds


def test_rollback_or_pause_resolved_auto_recovers(project):
    """A resolved escalation re-drive (human-initiated) auto-recovers even with
    rollback_on_failure OFF: it reverts the failed attempt's source change but
    preserves the corrected spec under the BMAD artifact folder, and never pauses
    for manual rollback."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # OFF: stopped attempts would pause
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []  # clean fixture at baseline

    (repo / "src.txt").write_text("partial dev work\n")  # failed attempt's source
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: corrected by resolve\n")  # spec under the artifact folder

    engine._rollback_or_pause(task, cause="resolved")  # must NOT raise RunPaused

    assert (repo / "src.txt").read_text() == "original\n"  # source reverted
    assert spec.read_text() == "frozen: corrected by resolve\n"  # spec preserved
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-auto" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_redrive_preserves_spec_on_later_rollback(project):
    """Regression: once a resolved re-drive latches `resolved_redrive`, a *later*
    mid-re-drive rollback (default cause="stopped", rollback_on_failure ON) must
    still preserve the corrected spec under the artifact folder — not just the
    first resume-time reset. Without the latch this reset ran with preserve=()
    and silently reverted the human correction, looping the re-drive."""
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),  # ON: a stopped attempt resets
    )
    engine, _ = make_engine(project, [], policy=policy)
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    task.baseline_untracked = []
    task.resolved_redrive = True  # latched by _finish_inflight on the re-drive

    (repo / "src.txt").write_text("re-drive dev work\n")  # this attempt's source
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("frozen: corrected by resolve\n")  # the human correction

    engine._rollback_or_pause(task)  # default cause="stopped"; must NOT pause

    assert (repo / "src.txt").read_text() == "original\n"  # source reverted
    assert spec.read_text() == "frozen: corrected by resolve\n"  # correction kept
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-auto" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_escalation_resume_skips_clean_rollback(project):
    """End-to-end regression for the resume loop: a CRITICAL escalation that left
    a clean tree, once resolved (re-armed) and resumed, must NOT demand a manual
    rollback — it skips the no-op rollback and re-drives the corrected story."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "auto-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # production default
    )
    engine, _ = make_engine(project, [escalating], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.ESCALATED

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.paused  # re-drove, no manual-rollback loop
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "rollback-skipped-clean" in kinds
    assert "rollback-manual-required" not in kinds


def test_resolved_escalation_resume_dirty_tree_auto_recovers(project):
    """End-to-end regression for the reported loop: a CRITICAL escalation that left
    the tree dirty (a partial source edit plus a spec under the artifact folder),
    once resolved (re-armed) and resumed with rollback_on_failure OFF, must
    auto-recover — NOT demand a manual rollback — and re-drive the story to done."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def escalate_dirty(spec):
        baseline = rev_parse_head(project.project)
        (project.project / "src.txt").write_text("partial dev work\n")  # source debris
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "blocked", baseline)  # spec under the artifact folder
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "escalations": [{"type": "blocked", "severity": "CRITICAL", "detail": "boom"}],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),  # production default
    )
    engine, _ = make_engine(project, [escalate_dirty], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.done == 1 and not summary2.paused  # no manual-rollback loop
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "rollback-auto" in kinds  # auto-recovered despite OFF
    assert "rollback-manual-required" not in kinds


def test_resolved_redrive_owned_dirty_spec_routes_explicitly_and_converges(project):
    """T22: #123 ownership recovery and #630 explicit routing converge together.

    The first pass records the existing sprint spec, then a feedback repair owns
    that exact path and escalates after cleaning its other attempt residue. A human
    corrects the frozen intent without committing it and re-arms from scratch. The
    rollback-off resume must classify the still-dirty corrected spec honestly,
    dispatch that named ready-for-dev spec with pinned read-back, and finish without
    a manual rollback. MockAdapter proves the orchestrator dispatch contract only;
    it does not stand in for build-auto's upstream route execution.

    ABLATION A: replace the first recovery probe's exact owned-spec exclusion with
    `()` and this test fails because `rollback-owned-spec-normalized` is absent.
    ABLATION B: delete the known-spec arm in `_generic_dev_prompt` and this test
    fails because the resumed dev prompt is bare and `expected_spec` is None.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sprint baseline")
    baseline = rev_parse_head(repo)
    marker = repo / ".bmad-loop" / "runs" / "test-run" / "verify-fixed.marker"

    def escalate_bound_repair(session):
        # The first successful artifact pass changed source + board before its
        # deterministic command failed. Leave only this repair attempt's owned
        # spec behind, matching the resolved-redrive field report.
        (repo / "src.txt").write_text("original\n")
        set_sprint(project, "1-1-a", "ready-for-dev")
        write_spec(sp, "blocked", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [
                    {
                        "type": "intent-needs-human",
                        "severity": "CRITICAL",
                        "detail": "correct the frozen intent",
                    }
                ],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), escalate_bound_repair],
        policy=policy,
    )
    first = engine.run()

    assert first.paused and first.escalated == 1
    escalated = load_state(engine.run_dir).tasks["1-1-a"]
    assert escalated.phase == Phase.ESCALATED
    assert escalated.spec_file == str(sp)
    assert escalated.dispatched_spec_file == str(sp)
    assert escalated.attempt == 2  # the feedback repair was the path-owning attempt

    corrected = sp.read_text().replace("test spec", "human corrected frozen intent")
    sp.write_text(corrected)
    head_before_rearm = rev_parse_head(repo)
    rearm_escalation(engine.run_dir)

    assert rev_parse_head(repo) == head_before_rearm  # no correction commit at re-arm
    assert read_frontmatter(sp)["status"] == "ready-for-dev"
    assert "human corrected frozen intent" in sp.read_text()
    assert git(repo, "status", "--porcelain")  # corrected spec deliberately remains dirty

    seen_at_dev: list[str] = []
    dirty_at_dev: list[str] = []

    def finish_corrected_spec(session):
        seen_at_dev.append(sp.read_text())
        dirty_at_dev.append(git(repo, "status", "--porcelain"))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("fixed\n")
        return dev_effect(project, "1-1-a")(session)

    resumed, adapter = resume_engine(
        project,
        engine,
        [finish_corrected_spec, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    second = resumed.run()

    assert second.done == 1 and not second.paused
    assert "human corrected frozen intent" in seen_at_dev[0]
    assert "_bmad-output/implementation-artifacts/spec-1-1-a.md" in dirty_at_dev[0]
    dev_session = adapter.sessions[0]
    assert dev_session.role == "dev"
    assert dev_session.prompt.startswith(
        f"/bmad-dev-auto Resume the autonomous dev session on the ready-for-dev spec at `{sp}`."
    )
    assert dev_session.expected_spec == str(sp)

    events = resumed.journal.entries()
    owned = [event for event in events if event["kind"] == "rollback-owned-spec-normalized"]
    assert {
        key: owned[-1][key] for key in ("kind", "story_key", "spec", "status", "checkout_dirty")
    } == {
        "kind": "rollback-owned-spec-normalized",
        "story_key": "1-1-a",
        "spec": str(sp.resolve()),
        "status": "ready-for-dev",
        "checkout_dirty": True,
    }
    kinds = [event["kind"] for event in events]
    assert "rollback-skipped-clean" not in kinds
    assert "rollback-manual-required" not in kinds


@pytest.mark.parametrize("resolved_redrive", [False, True], ids=["plain", "resolved-redrive"])
def test_bound_fixable_chain_restores_first_snapshot_before_fresh_retry(project, resolved_redrive):
    """A repair child cannot replace the correction retained for chain rollback.

    Child A leaves a fixable tree, so child B correctly inherits A's work for its
    repair pass. When B then crashes, the non-fixable rollback resets the whole
    chain: child C must receive the operator's original corrected spec and baseline
    source, not either failed child's body. Ablation: refresh the durable snapshot
    for child B and A's body survives the rollback into child C.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    source = repo / "src.txt"
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked redrive baseline")
    baseline = rev_parse_head(repo)
    sp.write_text(sp.read_text().replace("test spec", "operator corrected intent"))
    operator_snapshot = sp.read_bytes()
    marker = repo / "verify-fixed.marker"
    successful_effect = dev_effect(project, "1-1-a", followup_review=False)
    repair_inputs: list[bytes] = []
    repair_snapshots: list[bytes | None] = []
    fresh_retry_inputs: list[tuple[bytes, str]] = []

    def child_a(session):
        result = successful_effect(session)
        sp.write_text(sp.read_text().replace("test spec", "failed child A intent"))
        return result

    def child_b(_session):
        repair_inputs.append(sp.read_bytes())
        repair_snapshots.append(load_state(engine.run_dir).tasks["1-1-a"].dispatched_spec_snapshot)
        source.write_text(source.read_text() + "failed child B source\n")
        write_spec(sp, "in-progress", baseline)
        sp.write_text(sp.read_text().replace("test spec", "failed child B intent"))
        return SessionResult(status="crashed")

    def child_c(session):
        fresh_retry_inputs.append((sp.read_bytes(), source.read_text()))
        marker.write_text("fixed\n")
        return successful_effect(session)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd("verify-fixed.marker"),)),
        limits=LimitsPolicy(max_dev_attempts=3),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [child_a, child_b, child_c], policy=policy)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(sp),
        resolved_redrive=resolved_redrive,
    )
    engine.state.tasks[task.story_key] = task

    assert engine._dev_phase(task)

    assert b"failed child A intent" in repair_inputs[0]
    assert repair_snapshots == [operator_snapshot]
    assert fresh_retry_inputs == [(operator_snapshot, "original\n")]
    assert b"failed child A intent" not in fresh_retry_inputs[0][0]
    assert b"failed child B intent" not in fresh_retry_inputs[0][0]
    assert "rollback-auto" in [event["kind"] for event in engine.journal.entries()]


def test_resolved_redrive_fixable_retry_never_rebinds_missing_chain_snapshot(project):
    """A legacy/missing operator snapshot cannot be replaced with child A bytes."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))

    def child_a(session):
        result = dev_effect(project, "1-1-a", followup_review=False)(session)
        engine.state.tasks["1-1-a"].dispatched_spec_snapshot = None
        return result

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd("never-created.marker"),)),
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(project, [child_a], policy=policy)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(sp),
        resolved_redrive=True,
    )
    engine.state.tasks[task.story_key] = task

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert len(adapter.sessions) == 1
    assert task.phase == Phase.DEV_RUNNING
    assert task.dispatched_spec_file == str(sp.resolve())
    assert task.dispatched_spec_snapshot is None


@pytest.mark.skipif(sys.platform == "win32", reason="file symlink creation may need elevation")
def test_dev_repair_validates_retained_binding_before_prompt_mutation(project, monkeypatch):
    """A child-retargeted spec cannot be rewritten while building a repair prompt."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    sp = spec_path(project, "1-1-a")
    victim = project.implementation_artifacts / "victim.md"
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))
    victim_bytes = b"---\nstatus: ready-for-dev\n---\n\nvictim input\n"
    victim.write_bytes(victim_bytes)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd("never-created.marker"),)),
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=policy,
    )
    real_emit = engine._emit

    def retarget_after_verification(stage, task=None, **fields):
        if stage == "post_dev_verify":
            sp.unlink()
            sp.symlink_to(victim)
        return real_emit(stage, task, **fields)

    monkeypatch.setattr(engine, "_emit", retarget_after_verification)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(sp),
        resolved_redrive=True,
    )
    engine.state.tasks[task.story_key] = task

    with pytest.raises(RuntimeError, match="pre-launch snapshot"):
        engine._dev_phase(task)

    assert len(adapter.sessions) == 1
    assert sp.is_symlink()
    assert victim.read_bytes() == victim_bytes


@pytest.mark.skipif(sys.platform == "win32", reason="file symlink creation may need elevation")
def test_review_fix_validates_retained_binding_before_prompt_mutation(project):
    """The separate review-fix entry point validates before resetting the spec."""
    repo = project.project
    sp = spec_path(project, "1-1-a")
    victim = project.implementation_artifacts / "victim.md"
    write_spec(sp, "in-review", rev_parse_head(repo))
    victim_bytes = b"---\nstatus: in-review\n---\n\nvictim input\n"
    victim.write_bytes(victim_bytes)
    snapshot = sp.read_bytes()
    canonical_sp = str(sp.resolve())
    sp.unlink()
    sp.symlink_to(victim)
    engine, adapter = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,
        attempt=1,
        spec_file=str(sp),
        dispatched_spec_file=canonical_sp,
        dispatched_spec_snapshot=snapshot,
        resolved_redrive=True,
    )
    engine.state.tasks[task.story_key] = task

    with pytest.raises(RuntimeError, match="before repair prompt construction"):
        engine._fix_phase(task, "verification failed")

    assert adapter.sessions == []
    assert task.phase == Phase.DEV_RUNNING
    assert task.attempt == 2
    assert sp.is_symlink()
    assert victim.read_bytes() == victim_bytes


def _spec_beneath_symlinked_parent(project, *, status: str) -> tuple[Path, Path]:
    """Return one trusted regular spec through alias and canonical spellings."""
    real_parent = project.project / "canonical-spec-parent"
    real_parent.mkdir()
    alias_parent = project.project / "aliased-spec-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    real = real_parent / "spec-1-1-a.md"
    write_spec(real, status, rev_parse_head(project.project))
    return alias_parent / real.name, real


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlink creation may need elevation")
def test_retained_snapshot_validation_accepts_symlinked_ancestor(project):
    """A canonical leaf may be named through a trusted symlinked ancestor.

    Ablation: compare the resolved leaf directly with its unresolved accepted
    spelling and this test rejects the retained authority.
    """
    alias, real = _spec_beneath_symlinked_parent(project, status="ready-for-dev")
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(alias),
        dispatched_spec_file=str(real.resolve()),
        dispatched_spec_snapshot=real.read_bytes(),
    )

    assert engine._validate_dispatched_spec_snapshot(task)


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlink creation may need elevation")
def test_repair_reset_accepts_symlinked_ancestor(project):
    """Repair mutates the regular leaf, not the spelling of its parent.

    Ablation: restore the raw resolved-vs-spec_path comparison and this test
    raises before reopening the trusted target.
    """
    alias, real = _spec_beneath_symlinked_parent(project, status="done")
    real.write_text(real.read_text() + "\n## Auto Run Result\n\n- Status: done\n")
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(alias))

    engine._reset_spec_for_repair(task)

    assert verify.status_of(verify.read_frontmatter(real)) == "in-progress"
    assert "## Auto Run Result" not in real.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlink creation may need elevation")
def test_review_reset_accepts_symlinked_ancestor(project):
    """Review snapshots the canonical regular leaf through a parent alias.

    Ablation: restore the raw resolved-vs-spec_path comparison and this test
    raises before stripping or snapshotting the trusted target.
    """
    alias, real = _spec_beneath_symlinked_parent(project, status="done")
    real.write_text(real.read_text() + "\n## Auto Run Result\n\n- Status: done\n")
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(alias))

    snapshot = engine._reset_spec_for_review(task)

    assert snapshot is not None
    assert snapshot.path == str(real.resolve())
    assert "## Auto Run Result" not in real.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="file symlink creation may need elevation")
def test_review_launch_refuses_retargeted_spec_before_stripping_marker(project):
    """A review cannot adopt intent through a child-retargeted accepted path."""
    repo = project.project
    sp = spec_path(project, "1-1-a")
    victim = project.implementation_artifacts / "review-victim.md"
    write_spec(sp, "done", rev_parse_head(repo))
    victim_bytes = b"---\nstatus: done\n---\n\nvictim input\n## Auto Run Result\ndone\n"
    victim.write_bytes(victim_bytes)
    snapshot = sp.read_bytes()
    canonical_sp = str(sp.resolve())
    sp.unlink()
    sp.symlink_to(victim)
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_RUNNING,
        spec_file=str(sp),
        dispatched_spec_file=canonical_sp,
        dispatched_spec_snapshot=snapshot,
    )

    with pytest.raises(RuntimeError, match="before review prompt construction"):
        engine._reset_spec_for_review(task)

    assert sp.is_symlink()
    assert victim.read_bytes() == victim_bytes


def test_review_launch_refuses_distinct_spec_from_retained_authority(project):
    """A safe alternate file cannot borrow another spec's byte authority."""
    repo = project.project
    owned = spec_path(project, "1-1-a")
    accepted = project.implementation_artifacts / "spec-1-2-b.md"
    write_spec(owned, "done", rev_parse_head(repo))
    accepted_bytes = b"---\nstatus: done\n---\n\nalternate\n## Auto Run Result\ndone\n"
    accepted.write_bytes(accepted_bytes)
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(accepted),
        dispatched_spec_file=str(owned.resolve()),
        dispatched_spec_snapshot=owned.read_bytes(),
    )

    with pytest.raises(RuntimeError, match="before review prompt construction"):
        engine._reset_spec_for_review(task)

    assert accepted.read_bytes() == accepted_bytes


def test_review_fix_refuses_distinct_spec_from_retained_authority(project):
    """Repair validation happens before the alternate spec can be reopened."""
    repo = project.project
    owned = spec_path(project, "1-1-a")
    accepted = project.implementation_artifacts / "spec-1-2-b.md"
    write_spec(owned, "in-progress", rev_parse_head(repo))
    accepted_bytes = b"---\nstatus: done\n---\n\nalternate input\n"
    accepted.write_bytes(accepted_bytes)
    engine, adapter = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,
        attempt=1,
        spec_file=str(accepted),
        dispatched_spec_file=str(owned.resolve()),
        dispatched_spec_snapshot=owned.read_bytes(),
    )
    engine.state.tasks[task.story_key] = task

    with pytest.raises(RuntimeError, match="before repair prompt construction"):
        engine._fix_phase(task, "verification failed")

    assert adapter.sessions == []
    assert task.phase == Phase.DEV_RUNNING
    assert accepted.read_bytes() == accepted_bytes
    persisted = load_state(engine.run_dir).tasks[task.story_key]
    assert persisted.phase == Phase.DEV_RUNNING
    assert persisted.attempt == task.attempt


def test_review_launch_refuses_path_only_retained_authority(project):
    """An incomplete legacy authority pair cannot authorize another spec."""
    repo = project.project
    owned = spec_path(project, "1-1-a")
    accepted = project.implementation_artifacts / "spec-1-2-b.md"
    write_spec(owned, "done", rev_parse_head(repo))
    accepted_bytes = b"---\nstatus: done\n---\n\nalternate\n## Auto Run Result\ndone\n"
    accepted.write_bytes(accepted_bytes)
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(accepted),
        dispatched_spec_file=str(owned.resolve()),
        dispatched_spec_snapshot=None,
    )

    with pytest.raises(RuntimeError, match="before review prompt construction"):
        engine._reset_spec_for_review(task)

    assert accepted.read_bytes() == accepted_bytes


@pytest.mark.skipif(sys.platform == "win32", reason="file symlink creation may need elevation")
def test_unbound_repair_refuses_retargeted_spec_before_shared_reset(project):
    """Fresh repair binding cannot sanitize a symlink by rewriting through it."""
    sp = spec_path(project, "1-1-a")
    victim = project.implementation_artifacts / "victim.md"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim_bytes = b"---\nstatus: done\n---\n\nvictim input\n"
    victim.write_bytes(victim_bytes)
    sp.symlink_to(victim)
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(sp))

    with pytest.raises(RuntimeError, match="unsafe before repair prompt construction"):
        engine._reset_spec_for_repair(task)

    assert task.dispatched_spec_file is None
    assert task.dispatched_spec_snapshot is None
    assert sp.is_symlink()
    assert victim.read_bytes() == victim_bytes


@pytest.mark.parametrize("resolved_redrive", [False, True], ids=["plain", "resolved-redrive"])
def test_review_fix_phase_retains_bound_chain_snapshot(project, resolved_redrive):
    """Review-verification repair retains the first bound chain input."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked spec baseline")
    sp.write_text(sp.read_text().replace("test spec", "operator corrected intent"))
    operator_snapshot = sp.read_bytes()
    marker = repo / "review-verify-fixed.marker"
    observed: list[bytes | None] = []

    def initial_dev(session):
        marker.write_text("present\n")
        return dev_effect(project, "1-1-a")(session)

    def breaking_review(session):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(session)

    def repair(session):
        observed.append(load_state(engine.run_dir).tasks["1-1-a"].dispatched_spec_snapshot)
        marker.write_text("fixed\n")
        return dev_effect(project, "1-1-a")(session)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [initial_dev, breaking_review, repair, review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(sp),
        resolved_redrive=resolved_redrive,
    )
    engine.state.tasks[task.story_key] = task

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed
    assert observed == [operator_snapshot]


def test_review_fix_phase_never_rebinds_missing_redrive_snapshot(project):
    """A review repair cannot promote the preceding child's bytes to authority."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    repo = project.project
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "ready-for-dev", rev_parse_head(repo))
    marker = repo / "review-verify-never-fixed.marker"

    def initial_dev(session):
        marker.write_text("present\n")
        return dev_effect(project, "1-1-a")(session)

    def breaking_review(session):
        marker.unlink()
        result = review_effect(project, "1-1-a", clean=True)(session)
        engine.state.tasks["1-1-a"].dispatched_spec_snapshot = None
        return result

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project,
        [initial_dev, breaking_review, dev_effect(project, "1-1-a")],
        policy=policy,
    )
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        spec_file=str(sp),
        resolved_redrive=True,
    )
    engine.state.tasks[task.story_key] = task

    summary = engine.run()

    assert summary.crashed and "before repair prompt construction" in summary.crash_error
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert task.phase == Phase.DEV_RUNNING
    assert task.dispatched_spec_file == str(sp.resolve())
    assert task.dispatched_spec_snapshot is None


def test_dev_escalation_records_spec_for_rearm(project):
    """A dev session that HALTs with a `blocked` spec still records task.spec_file,
    so rearm_escalation can flip the spec to `ready-for-dev` for the re-drive.
    Without it (verify_dev only records the spec on success) the re-drive HALTs
    again on the stale `blocked` status — the loop seen in the live run."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    sp = spec_path(project, "1-1-a")

    def halt_blocked(spec):
        write_spec(sp, "blocked", rev_parse_head(project.project))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "escalations": [
                    {"type": "blocked", "severity": "CRITICAL", "detail": "blocked spec supplied"}
                ],
            },
        )

    engine, _ = make_engine(project, [halt_blocked])
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    task = load_state(engine.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.spec_file and Path(task.spec_file).name == sp.name  # recorded despite HALT

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step
    assert read_frontmatter(sp)["status"] == "ready-for-dev"  # re-drive will not HALT


# ------------------------------------------------------ deferred-artifact stash


def test_stash_deferred_artifacts_moves_spec_into_run_dir(project):
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("first attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    engine._stash_deferred_artifacts(task)

    dest = engine.run_dir / "deferred" / "1-1-a"
    assert (dest / sp.name).read_text(encoding="utf-8") == "first attempt\n"
    assert not sp.exists()
    stashed = [e for e in engine.journal.entries() if e["kind"] == "deferred-artifacts-stashed"]
    assert len(stashed) == 1 and stashed[0]["stashed_to"] == str(dest / sp.name)


def test_stash_deferred_artifacts_overwrites_a_prior_stash(project):
    """A story that defers a second time re-stashes the same filename: the newest
    spec wins, leaving no staging residue. Characterization — `shutil.move` also
    overwrote here (via its copy2 fallback); the tests below pin what changed."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("second attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    dest = engine.run_dir / "deferred" / "1-1-a"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / sp.name).write_text("first attempt\n", encoding="utf-8")

    engine._stash_deferred_artifacts(task)

    assert (dest / sp.name).read_text(encoding="utf-8") == "second attempt\n"
    assert not sp.exists()
    assert list(dest.iterdir()) == [dest / sp.name]  # the staging copy is gone


def test_stash_deferred_artifacts_survives_a_win32_sharing_violation(project, monkeypatch):
    """The real #101 hazard: an AV/indexer handle on the destination denies the
    rename. `shutil.move` caught that OSError and fell back to `copy2`, which opens
    that same locked target and fails too — crashing the defer flow. Routing through
    `atomic_replace` retries the replace until the handle clears."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("second attempt\n", encoding="utf-8")
    task.spec_file = str(sp)

    dest = engine.run_dir / "deferred" / "1-1-a"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / sp.name).write_text("first attempt\n", encoding="utf-8")

    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)
    calls, real_replace = {"n": 0}, os.replace

    def sharing_violation_once(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(platform_util.os, "replace", sharing_violation_once)
    engine._stash_deferred_artifacts(task)

    assert calls["n"] == 2  # denied once, retried, landed
    assert (dest / sp.name).read_text(encoding="utf-8") == "second attempt\n"
    assert not sp.exists()
    assert list(dest.iterdir()) == [dest / sp.name]


def test_stash_deferred_artifacts_retries_a_locked_source_spec(project, monkeypatch):
    """Second half of the staged move. Windows denies the source *delete* against an
    AV/indexer handle just as it denies the rename-over, and `_defer` calls this
    before the rollback and the `story-deferred` journal append — so an unretried
    unlink would abort the whole deferral after the stash had already landed."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("work\n", encoding="utf-8")
    task.spec_file = str(sp)

    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)
    calls, real_unlink = {"n": 0}, os.unlink

    def locked_once(path, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        real_unlink(path)

    monkeypatch.setattr(platform_util.os, "unlink", locked_once)
    engine._stash_deferred_artifacts(task)

    assert calls["n"] == 2  # denied once, retried, removed
    assert not sp.exists()
    dest = engine.run_dir / "deferred" / "1-1-a"
    assert (dest / sp.name).read_text(encoding="utf-8") == "work\n"


def test_stash_deferred_artifacts_keeps_source_and_cleans_tmp_on_replace_failure(
    project, monkeypatch
):
    """The replace is the only step that can fail after staging: the source spec
    must survive it (the stash is forensic — losing the work is worse than a crash)
    and the staging copy must not linger."""
    engine, _ = make_engine(project, [])
    task = StoryTask("1-1-a", 1)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("work\n", encoding="utf-8")
    task.spec_file = str(sp)

    def boom(_tmp, _target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("bmad_loop.engine.atomic_replace", boom)
    with pytest.raises(PermissionError):
        engine._stash_deferred_artifacts(task)

    assert sp.read_text(encoding="utf-8") == "work\n"
    assert list((engine.run_dir / "deferred" / "1-1-a").iterdir()) == []
    assert "deferred-artifacts-stashed" not in [e["kind"] for e in engine.journal.entries()]


# -------------------------------------------------- intent-gap patch-restore (#2564)


def _escalate_with_patch(project, story_key, patch_path):
    """A dev effect that halts on an intent gap the way bmad-dev-auto #2564 does:
    it saves the attempted change as a patch under the protected artifacts, reverts
    the tree, and escalates blocked."""

    def effect(spec):
        repo = project.project
        baseline = rev_parse_head(repo)
        src = repo / "src.txt"
        src.write_text("original\nattempted reading\n")  # the attempted change
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")  # save it
        src.write_text("original\n")  # revert before halting (tree back at baseline)
        sp = spec_path(project, story_key)
        write_spec(sp, "blocked", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "escalations": [
                    {
                        "type": "blocked",
                        "severity": "CRITICAL",
                        "detail": f"intent gap; saved patch: {patch_path}",
                    }
                ],
            },
        )

    return effect


def _restoring_dev_effect(project, story_key, seen):
    """A re-driven dev effect that records what the tree looked like when it ran (so a
    test can assert the restored diff was present) and leaves `src.txt` exactly as the
    restore laid it down — the applied patch IS this session's proof of work."""
    return dev_effect(project, story_key, followup_review=False, seen=seen, write_src=False)


def test_restore_patch_applies_onto_baseline(project):
    """_restore_patch re-lays the saved diff onto the baseline tree and journals
    attempt-restored, leaving the phase untouched on success."""
    engine, _ = make_engine(project, [])
    repo = project.project
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(repo)
    src = repo / "src.txt"
    src.write_text("original\nattempted\n")
    patch = project.implementation_artifacts / "attempt.patch"
    patch.write_text(git(repo, "diff", "HEAD") + "\n", encoding="utf-8")
    src.write_text("original\n")  # tree at baseline
    task.restore_patch = str(patch)
    task.phase = Phase.DEV_RUNNING

    engine._restore_patch(task)

    assert src.read_text() == "original\nattempted\n"
    assert task.phase == Phase.DEV_RUNNING  # success does not advance the phase
    assert "attempt-restored" in [e["kind"] for e in engine.journal.entries()]


def test_restore_patch_failure_escalates_without_dispatch(project):
    """A patch that will not apply escalates (never dispatches onto a half-restored
    tree): the task ends ESCALATED, attempt-restore-failed is journaled, and the
    success marker is not."""
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1)
    task.baseline_commit = rev_parse_head(project.project)
    patch = project.implementation_artifacts / "bad.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("--- a/src.txt\n+++ b/src.txt\n@@ -1 +1 @@\n-nope\n+x\n", encoding="utf-8")
    task.restore_patch = str(patch)
    task.phase = Phase.DEV_RUNNING

    with pytest.raises(RunPaused):
        engine._restore_patch(task)

    assert task.phase == Phase.ESCALATED
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "attempt-restore-failed" in kinds
    assert "attempt-restored" not in kinds


def test_intent_gap_restore_redrive_applies_patch_and_lands_done(project):
    """End-to-end: a resolved escalation with a restore patch re-applies the
    attempted change onto the baseline before the re-driven session runs, so the
    session resumes on the restored diff and the story lands done; the latch clears
    on commit."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    rearm_escalation(engine.run_dir, restore_patch=str(patch))  # human confirmed the reading
    sp = spec_path(project, "1-1-a")
    assert read_frontmatter(sp)["status"] == "in-review"  # routes step-01 -> step-04
    assert load_state(engine.run_dir).tasks["1-1-a"].restore_patch == str(patch)

    seen: list[str] = []
    resumed, _ = resume_engine(project, engine, [_restoring_dev_effect(project, "1-1-a", seen)])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert seen == ["original\nattempted reading\n"]  # the session saw the RESTORED code
    assert "attempt-restored" in [e["kind"] for e in resumed.journal.entries()]
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.restore_patch is None  # latch cleared on commit


def test_restore_redrive_prompt_points_at_the_spec(project):
    """The sprint-mode restore re-drive must dispatch an explicit spec-file
    pointer: only step-01's spec-pointer intent check EARLY EXITs on the
    `in-review` status (to step-04) BEFORE its version-control sanity check — a
    bare story key takes the freeform/epic path, whose dirty-tree check HALTs
    `blocked` on the very diff _restore_patch just laid onto the tree."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    rearm_escalation(engine.run_dir, restore_patch=str(patch))
    seen: list[str] = []
    resumed, adapter = resume_engine(
        project, engine, [_restoring_dev_effect(project, "1-1-a", seen)]
    )
    assert resumed.run().done == 1

    prompt = adapter.sessions[0].prompt
    spec = load_state(resumed.run_dir).tasks["1-1-a"].spec_file
    assert spec and f"`{spec}`" in prompt  # explicit pointer -> step-01 EARLY EXIT
    assert prompt != "/bmad-dev-auto 1-1-a"  # never the bare key on a restore


def test_intent_gap_restore_reapplies_after_mid_redrive_rollback(project):
    """A non-fixable retry inside the restore re-drive resets to baseline (clearing
    the restored code), so the patch is re-applied before the next dispatch; the
    saved patch file under the protected artifacts survives the reset."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1
    rearm_escalation(engine.run_dir, restore_patch=str(patch))

    seen: list[str] = []
    resumed, _ = resume_engine(
        project,
        engine,
        [SessionResult(status="stalled"), _restoring_dev_effect(project, "1-1-a", seen)],
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert seen == ["original\nattempted reading\n"]  # the surviving retry ran on the restored tree
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert kinds.count("attempt-restored") == 2  # applied, rolled back on stall, re-applied
    assert patch.is_file()  # protected patch file survived the reset


def test_intent_gap_restore_escalates_when_resolution_commits_overlap(project):
    """T2 (patch-restore x #78 baseline advance): re-arm adopts the resolve
    session's commits as the re-drive's baseline, but the saved patch was diffed
    from the ORIGINAL baseline — so a resolution commit that rewrote the patched
    lines makes the restore's `git apply` fail. The engine must escalate loudly
    with no session dispatched (never silently merge the human's resolution with
    the stale attempt), and the resolution commit must survive untouched."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    patch = project.implementation_artifacts / "attempt.patch"
    engine, _ = make_engine(project, [_escalate_with_patch(project, "1-1-a", patch)])
    assert engine.run().escalated == 1

    # the resolve session commits its own fix REWRITING the line the patch's
    # context expects, then the human latches the restore anyway
    repo = project.project
    (repo / "src.txt").write_text("corrected by resolution\n")
    git(repo, "add", "src.txt")
    git(repo, "commit", "-q", "-m", "resolution: overlapping fix")
    rearm_escalation(engine.run_dir, restore_patch=str(patch))

    seen: list[str] = []
    resumed, _ = resume_engine(project, engine, [_restoring_dev_effect(project, "1-1-a", seen)])
    summary = resumed.run()

    assert summary.paused
    assert seen == []  # no session ever dispatched onto a half-restored tree
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "attempt-restore-failed" in kinds
    assert "attempt-restored" not in kinds
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED  # re-escalated: the human re-resolves
    # git apply is all-or-nothing: the overlapping resolution commit is untouched
    assert (repo / "src.txt").read_text() == "corrected by resolution\n"


def test_dev_stall_retries_then_succeeds(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="stalled"),
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.tasks["1-1-a"].attempt == 2


def test_dev_exhausted_defers_and_run_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="timeout"),
            SessionResult(status="crashed"),
            dev_effect(project, "1-2-b"),
            review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE


def test_critical_escalation_pauses_and_resume_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "auto-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    engine, _ = make_engine(project, [escalating])
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    saved = load_state(engine.run_dir)
    assert saved.paused_reason and "boom" in saved.paused_reason
    assert saved.tasks["1-1-a"].phase == Phase.ESCALATED

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-2-b"), review_effect(project, "1-2-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 1 and not summary2.paused
    assert resumed.state.finished


def test_dispatch_refuses_a_story_an_unlanded_entry_gates(project):
    """The enforcing half of `gate:`. Before this, `_pick_next` read the board
    alone: the ledger could say a story was blocked and `run` drove it anyway, and
    the gate was discovered afterwards in the diff of work built on a leg nobody
    had wired. `validate` refuses the same story, but only if someone ran it."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_STORY_GATE
    assert saved.paused_story_key == "1-1-a"
    # the refusal has to precede the work, not follow it
    assert adapter.sessions == []
    # ...and precede the *record* of the work: the story is deliberately NOT in
    # state.tasks, which is what `_pick_next`'s base_skip keys on. Registering it
    # first would fire the gate once and then retire the story for this run and
    # every resume of it — a gate that drops the work it was protecting.
    assert saved.tasks == {}
    events = [e for e in engine.journal.entries() if e["kind"] == "story-gated"]
    assert len(events) == 1 and events[0]["dw_ids"] == ["DW-1"]
    assert "DW-1" in saved.paused_reason and "bmad-loop sweep" in saved.paused_reason


def test_the_gate_still_holds_when_a_resume_has_not_closed_the_entry(project):
    """A resume that fixed nothing must not get the story through.

    This is what the placement buys, stated as behavior. Recording the task before
    the check — the obvious placement, next to `_run_story` — leaves a non-terminal
    task behind, and `_finish_inflight` runs *before* the loop and drives exactly
    those: the resume would dispatch the gated story without ever consulting the
    ledger again. Refusing before the story is recorded is what makes the gate a
    standing condition rather than a one-shot speed bump.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})
    engine, _ = make_engine(project, [])
    assert engine.run().paused

    resumed, adapter = resume_engine(project, engine, [dev_effect(project, "1-1-a")])
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert adapter.sessions == []  # the entry is still open; nothing may run
    assert load_state(resumed.run_dir).paused_stage == PAUSE_STORY_GATE


def test_a_gated_story_runs_once_the_entry_lands(project):
    """Closing the entry is the primary remedy the pause names, so it has to be
    the one that clears it — a gate nobody can get past is a wedge, not a gate.
    (That the refusal survives a resume which changed nothing is the test above;
    this one is the release.)"""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})
    engine, _ = make_engine(project, [])
    assert engine.run().paused

    write_gated_ledger(project, {"DW-1": ("done 2026-08-01", ["gate: 1-1"])})
    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused


def test_dispatch_gate_holds_on_a_status_the_format_cannot_read(project):
    """`status: opne` is not evidence the work landed. The check keys on an
    explicit `done` rather than on `not open` precisely so a one-character typo
    cannot disable the gate — the silent no-op the whole field exists to end."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("opne", ["gate: 1-1"])})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])

    summary = engine.run()

    assert summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_STORY_GATE
    assert adapter.sessions == []


def test_dispatch_gate_does_not_fire_for_a_story_it_does_not_name(project):
    """A false refusal wedges a run, which is worse than the prose gate this
    replaced. An entry gating 2-1 must let 1-1-a through untouched."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 2-1"])})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )

    summary = engine.run()

    assert summary.done == 1 and not summary.paused


def test_dispatch_pauses_when_the_ledger_cannot_be_read(project, monkeypatch):
    """Degrading to "not gated" would let a broken file disable the one deferred
    check that refuses, and "does this project use gates?" is answerable only from
    the file that will not open. `validate` reports the same fault as a problem."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 9-9"])})
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    fault_read_text(monkeypatch, project.deferred_work)

    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_STORY_GATE
    assert adapter.sessions == []
    assert "cannot be read" in saved.paused_reason
    assert [e["kind"] for e in engine.journal.entries()].count("story-gate-unreadable") == 1


def test_resume_re_gates_a_story_registered_but_never_started(project):
    """`_loop` saves the task and *then* calls `_run_story`, so a host death in
    that window — or anywhere before `_dev_phase`'s advance, which spans the
    isolated worktree mount — persists a PENDING task no session ever touched.
    `_pick_next` skips it (it is in `base_skip`), so only `_finish_inflight`
    drives it, and its restart arm calls `_run_story` directly. A gate that
    landed while the run was down would never be asked.

    The exemption below is for work already *in flight*; this task is not. Its
    state is byte-identical to one the loop would have re-picked and re-gated a
    microsecond earlier, and the run's own crash is not a reason to skip it.
    """
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    # exactly what _loop persists between `state.tasks[key] = task` and _run_story
    engine.state.tasks["1-1-a"] = StoryTask(story_key="1-1-a", epic=1)
    engine._save()
    # the gate lands while the run is down
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert adapter.sessions == []
    saved = load_state(resumed.run_dir)
    assert saved.paused_stage == PAUSE_STORY_GATE
    assert saved.paused_story_key == "1-1-a"


def test_resume_re_gates_a_story_whose_attempt_never_reached_a_session(project):
    """The second window, and why the arm asks unconditionally rather than testing
    the attempt counter: `_dev_phase` persists `attempt == 1` with the DEV_RUNNING
    advance, but the session does not launch until `adapter.run()` — past
    `_restore_patch`, the prompt build and the pre_session plugin gate, any of which
    can be slow or can pause. A host death in there records an attempt no session
    ever backed, and the restart arm rolls the task back and re-runs it from
    scratch, so this is a start too."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    # attempt counted, no session record: the state _dev_phase saves before launch
    engine.state.tasks["1-1-a"] = StoryTask(
        story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1
    )
    engine._save()
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert adapter.sessions == []
    assert load_state(resumed.run_dir).paused_stage == PAUSE_STORY_GATE


def test_restart_arm_gate_re_asks_until_the_entry_lands(project):
    """The restart arm's refusal must be a standing condition, not a one-shot.

    `_finish_inflight` drives every non-terminal task, and a refused task stays
    non-terminal — so each resume re-reads the ledger, and closing the entry is
    what releases it. Were the refusal to retire the story instead, the gate would
    drop the very work it was protecting; that is the same property the `_loop`
    side buys by refusing before it records the task."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.tasks["1-1-a"] = StoryTask(story_key="1-1-a", epic=1)
    engine._save()
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    first, adapter1 = resume_engine(project, engine, [dev_effect(project, "1-1-a")])
    assert first.run().paused and adapter1.sessions == []

    # a resume that changed nothing must not get the story through
    second, adapter2 = resume_engine(project, first, [dev_effect(project, "1-1-a")])
    assert second.run().paused and adapter2.sessions == []
    assert load_state(second.run_dir).paused_stage == PAUSE_STORY_GATE

    # ...and closing the entry releases it, so the gate is not a wedge
    write_gated_ledger(project, {"DW-1": ("done 2026-08-01", ["gate: 1-1"])})
    third, _ = resume_engine(
        project,
        second,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = third.run()

    assert summary.done == 1 and not summary.paused


def test_resume_reads_the_gate_before_the_restart_rollback_rewinds_the_ledger(project):
    """Order matters, not just placement. The restart arm's in-place rollback is
    `git reset --hard <baseline>`, and `keep=(".bmad-loop",)` guards only untracked
    deletion — tracked content under it is reverted anyway, which is why
    `verify.safe_rollback` restores `policy.toml` by hand. A tracked ledger has no
    such rescue: a `gate:` committed while the run was down lives in a commit
    *after* the baseline, so a rollback that ran first would rewind the ledger and
    the gate would read a file the human never wrote. Ask before the arm mutates
    anything — the same rule `_loop` follows."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "board")  # board predates the baseline
    engine, _ = make_engine(project, [])  # default test policy: rollback_on_failure=True
    baseline = rev_parse_head(project.project)
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_RUNNING, attempt=1)
    task.baseline_commit = baseline
    task.baseline_untracked = []
    engine.state.tasks["1-1-a"] = task
    engine._save()
    # the gate is committed while the run is down — i.e. after the task's baseline
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})
    assert rev_parse_head(project.project) != baseline  # the gate is a later commit

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert adapter.sessions == []
    assert load_state(resumed.run_dir).paused_stage == PAUSE_STORY_GATE


def test_resume_still_finishes_a_story_whose_session_already_completed(project):
    """The other side of the line, and the exemption stated as behavior. It belongs
    to `_finish_inflight`'s *finishing* arms, not to every non-terminal task: here
    the review session completed and its result is on disk, so the resume replays
    that record straight into the decision path. Gating it would abandon a verified
    session's work over an entry whose remedy is a later story — and the story is
    not starting, it is ending. The restart arm is the opposite case: it discards
    the work and re-runs, so it re-asks (the tests above)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    post_sessions = []
    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            post_sessions.append(stage)
            if len(post_sessions) == 2:  # the review session's post_session window
                raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.REVIEW_RUNNING
    # the gate lands while the run is down, on a story whose work is already done
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused
    assert adapter.sessions == []  # replayed the recorded result; nothing re-run
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "resume-verify" in kinds and "resume-restart" not in kinds


def test_resume_re_gates_a_human_armed_re_drive(project):
    """A resolved escalation re-drives through the restart arm — the escalated
    attempt is rolled back and the story re-runs from scratch — so it is a start,
    and the gate is asked. Resolving an escalation is not evidence that the gating
    entry landed, and `validate` refuses this story on the same ledger no matter
    what its task record remembers; the two surfaces have to agree.

    `rearmed` is also the signal a "has this story ever run a session?" test would
    most want to trust, and it cannot be trusted: `StoriesEngine._pause_wedged`
    reaches ESCALATED with `attempt == 0` and no session at all, so exempting
    re-drives would wave through a wedged story's very first dispatch."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "auto-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    engine, _ = make_engine(project, [escalating])
    assert engine.run().escalated == 1
    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step
    assert load_state(engine.run_dir).tasks["1-1-a"].attempt == 0  # the confusable state
    # a gate lands on the story while the operator is resolving it
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    resumed, adapter = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = resumed.run()

    assert summary.paused and summary.done == 0
    assert adapter.sessions == []  # the re-drive is a start, and it was refused
    assert load_state(resumed.run_dir).paused_stage == PAUSE_STORY_GATE


def test_epic_boundary_gate_pause_and_resume(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    gated = Policy(gates=GatesPolicy(mode="per-epic"), notify=QUIET)
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=gated,
    )
    summary = engine.run()
    assert summary.done == 1 and summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_EPIC_BOUNDARY

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 2 and not summary2.paused


def test_spec_approval_gate_pause_then_resume_reviews(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(gates=GatesPolicy(mode="per-story-spec-approval"), notify=QUIET)
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_SPEC_APPROVAL
    assert saved.tasks["1-1-a"].phase == Phase.DEV_VERIFY
    assert saved.tasks["1-1-a"].spec_file

    resumed, adapter = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=True)], policy=gated
    )
    summary2 = resumed.run()
    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]


def test_dev_verify_command_failure_routes_feedback_fix(project):
    """A broken build never reaches review: the dev-stage gate fails, the tree
    is kept, and the next dev session gets the failing output as feedback."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def fix(spec):
        marker.write_text("ok\n")
        sp = spec_path(project, "1-1-a")
        baseline = rev_parse_head(project.project)
        # the repair session re-finalizes the re-opened spec to done, as the real
        # bmad-dev-auto resume does (the orchestrator flipped it to in-progress)
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [{"command": _file_exists_cmd(marker), "ok": True}],
                "escalations": [],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.attempt == 2
    prompts = [s.prompt for s in adapter.sessions]
    # the repair re-invocation is the freeform resume prompt (no --feedback flag);
    # the feedback file is referenced as the last backtick-wrapped path.
    assert "Resume the autonomous" not in prompts[0] and "Resume the autonomous" in prompts[1]
    feedback = Path(re.findall(r"`([^`]*)`", prompts[1])[-1])
    assert _file_exists_cmd(marker) in feedback.read_text()
    # the first attempt's work survived: no reset between attempts
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_review_verify_failure_routes_fix_session_then_rereview(project):
    """Verify commands failing after a clean review route to a feedback-driven
    dev fix session and a fresh review cycle — not a blind re-review."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # the review's "patch" broke the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix(spec):
        marker.write_text("ok\n")
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2 and task.attempt == 2
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev", "review"]
    assert "Resume the autonomous" in adapter.sessions[2].prompt


def test_review_verify_failure_without_fix_budget_defers(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine, adapter = make_engine(project, [dev_with_marker, breaking_review], policy=policy)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert "kept failing" in engine.state.tasks["1-1-a"].defer_reason


def test_verify_commands_never_pass_defers_at_dev(project):
    """Unfixable verify failures exhaust the dev budget and defer before any
    review session is spent."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_FAIL,)),  # host-shell fail verb, not `false` (#302)
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), dev_effect(project, "1-1-a")],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]
    assert "Resume the autonomous" in adapter.sessions[1].prompt


# --------------------------------------------------- verify env faults (issue #126)


def test_verify_env_fault_pauses_dev_without_burning_budget(project):
    """rc 127 at the dev gate is the run environment's fault, not the story's:
    the run pauses at the first story instead of spending max_dev_attempts on
    repair sessions that cannot fix the environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=("exit 127",)),
    )
    # only one dev session scripted: a repair session must never be requested
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=policy)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.attempt == 1  # budget untouched beyond the one real session
    assert engine.state.paused_stage == PAUSE_ESCALATION
    assert "rc=127" in engine.state.paused_reason
    decision = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"][-1]
    assert decision["env_fault"] is True


def test_review_verify_env_fault_escalates_instead_of_fix_session(project):
    """An env fault at the review gate pauses the run — no fix session is
    dispatched and no review cycles are burned re-verifying a broken environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    check = _self_disarming_cmd(project.project)
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(check,)),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review"]  # no fix session
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "verify environment fault" in engine.state.paused_reason
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"][-1]
    assert failed["env_fault"] is True


def test_skip_review_env_fault_escalates_not_defers(project):
    """review.enabled = false: an env fault at the commit gates pauses the run
    instead of deferring the story as if its code were broken."""
    from bmad_loop.policy import ReviewPolicy

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    check = _self_disarming_cmd(project.project)
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        verify=VerifyPolicy(commands=(check,)),
    )
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=policy)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]  # no fix session either
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "verify environment fault" in engine.state.paused_reason
    failed = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"][-1]
    assert failed["env_fault"] is True


def test_fix_phase_env_fault_escalates_instead_of_looping(project):
    """An env fault surfacing mid-fix-loop stops the loop — the remaining dev
    budget is not spent on repair sessions against an unfixable environment."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"
    script, check = _write_check_script(project.project)

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # ordinary fixable failure: routes to a fix session
        return review_effect(project, "1-1-a", clean=True)(spec)

    def env_breaking_fix(spec):
        marker.write_text("ok\n")
        _disarm_check_script(script)  # the re-verify now hits an env fault
        return SessionResult(
            status="completed", result_json={"workflow": "auto-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), check)),
        limits=LimitsPolicy(max_dev_attempts=3),  # budget left — must not be spent
    )
    engine, adapter = make_engine(
        project, [dev_with_marker, breaking_review, env_breaking_fix], policy=policy
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev"]  # one fix, then stop
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "verify environment fault" in engine.state.paused_reason
    fix = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"][-1]
    assert fix["env_fault"] is True


# ---------------------------- session-transport environment faults (#194) ----
#
# A dev/review/fix session whose coding CLI lost its API connection is classified
# env_fault by the adapter (part 1) and stamped on the SessionResult. These pin
# the story-pipeline consumption (part 2): the run PAUSEs — evidence journaled,
# worktree preserved — instead of charging the attempt, and re-arm restores the
# budget. Guard pins confirm plain (non-classified) failures still retry/defer.


def test_lost_session_is_journaled_structurally_on_dev_decision(project):
    """#489: the diagnosis has to be greppable, not only readable. The flag
    rides every role's `session-end` entry via `_session_end_extras` (so "how
    often did this host destroy my sessions" is a query over the journal rather
    than a reading exercise over reason strings) and `dev-decision` pairs it
    with the routing it fed. Routing stays the ordinary retry."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="crashed", session_vanished=True),
            SessionResult(status="crashed", session_vanished=True),
        ],
    )
    engine.run()

    assert len(adapter.sessions) == 2  # both attempts spent: the retry actually ran
    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert len(decisions) == 2 and all(d["session_vanished"] is True for d in decisions)
    assert decisions[0]["action"] == "retry"  # diagnosis, not routing
    assert "multiplexer no longer reports the session" in decisions[0]["reason"]
    ends = [e for e in engine.journal.entries() if e["kind"] == "session-end"]
    assert len(ends) == 2 and all(e["session_vanished"] is True for e in ends)
    # guard pin: an ordinary crash records the field as False rather than omitting
    # it on the decision, and omits it on session-end (the env_fault convention
    # there). The journal is per-project and this second run appends to the same
    # file, so only its own last entries may be inspected.
    engine2, _ = make_engine(project, [SessionResult(status="crashed")])
    engine2.run()
    plain = [e for e in engine2.journal.entries() if e["kind"] == "dev-decision"][-1]
    assert plain["session_vanished"] is False
    assert "multiplexer" not in plain["reason"]
    plain_end = [e for e in engine2.journal.entries() if e["kind"] == "session-end"][-1]
    assert "session_vanished" not in plain_end


def test_session_env_fault_pauses_dev_without_burning_budget(project):
    """A dev session classified an environment fault (#194) pauses the run at the
    first story rather than charging the attempt; the decision + session-end carry
    the evidence, and re-arm restores the budget (attempt -> 0)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    evidence = "API Error: Unable to connect (ECONNREFUSED)"
    engine, adapter = make_engine(
        project,
        [SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence)],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev"]  # no retry session burned
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.attempt == 1  # the one real session, not a spent budget
    assert engine.state.paused_stage == PAUSE_ESCALATION
    assert "environment fault: dev session timeout" in engine.state.paused_reason
    assert evidence in engine.state.paused_reason

    dec = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"][-1]
    assert dec["action"] == "pause"
    assert dec["env_fault"] is True
    end = [e for e in engine.journal.entries() if e["kind"] == "session-end"][-1]
    assert end["env_fault"] is True
    assert end["env_fault_evidence"] == evidence

    # the resolve workflow's re-arm step restores the attempt budget
    rearm_escalation(engine.run_dir)
    assert load_state(engine.run_dir).tasks["1-1-a"].attempt == 0


def test_two_plain_timeouts_still_defer(project):
    """Guard: NON-env-fault timeouts keep today's flow — two of them exhaust the
    dev budget and defer the story (the env-fault pause must not intercept them)."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_engine(
        project,
        [SessionResult(status="timeout"), SessionResult(status="timeout")],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.escalated == 0 and not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]  # both attempts spent
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    dec = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert all(d["env_fault"] is False for d in dec)


def test_fix_phase_lost_session_defers_naming_the_mux(project):
    """#489 on the repair path: a fix session the mux destroyed must not be filed
    as the verification failure that sent it there. `_fix_phase` returns an empty
    reason on budget exhaustion and both callers substitute verify-centric text
    for it, so the operator read "verify commands kept failing" about repairs
    that never ran — the #489 misdiagnosis one layer further out, and blaming the
    tree rather than even the agent. Routing is unchanged: still a defer."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # ordinary fixable failure -> routes to a fix session
        return review_effect(project, "1-1-a", clean=True)(spec)

    lost = SessionResult(status="crashed", session_vanished=True)  # not an env fault
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), _OK)),
        limits=LimitsPolicy(max_dev_attempts=3),  # dev + two fix attempts
    )
    engine, adapter = make_engine(
        project, [dev_with_marker, breaking_review, lost, lost], policy=policy
    )
    summary = engine.run()

    assert summary.deferred == 1
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev", "dev"]
    reason = engine.state.tasks["1-1-a"].defer_reason
    assert "fix session crashed" in reason
    assert "multiplexer no longer reports the session" in reason
    fixes = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"]
    assert len(fixes) == 2 and all(f["session_vanished"] is True for f in fixes)


def test_fix_phase_exhaustion_keeps_the_verify_reason_when_the_repair_ran(project):
    """Guard pin for the case above: when the last repair session actually ran and
    only its verify failed, the caller's verify-centric wording is the honest one
    and must survive. The session-failure reason is carried only for a session
    that did not complete, so this path is left exactly as it was."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), _OK)),
        limits=LimitsPolicy(max_dev_attempts=3),
    )
    # the fix sessions complete; the marker stays missing, so verify keeps failing
    ran = SessionResult(status="completed")
    engine, _ = make_engine(project, [dev_with_marker, breaking_review, ran, ran], policy=policy)
    summary = engine.run()

    assert summary.deferred == 1
    reason = engine.state.tasks["1-1-a"].defer_reason
    assert "multiplexer" not in reason
    assert "verify commands kept failing after clean review" in reason


def test_fix_phase_session_env_fault_escalates(project):
    """A fix session whose CLI lost its API connection (#194) escalates instead of
    burning the remaining dev budget on repair sessions that never ran."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # ordinary fixable failure -> routes to a fix session
        return review_effect(project, "1-1-a", clean=True)(spec)

    evidence = "API Error: Connection reset by peer"
    env_fault_fix = SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        # `_OK`, not a script file (#292): verify commands run through the host shell,
        # and cmd hands a `.sh` path to ShellExecute instead of running it. Only the
        # marker command carries signal here — the escalation is the session's.
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker), _OK)),
        limits=LimitsPolicy(max_dev_attempts=3),  # budget left -> must not be spent
    )
    engine, adapter = make_engine(
        project, [dev_with_marker, breaking_review, env_fault_fix], policy=policy
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev"]  # one fix, then stop
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "environment fault: fix session timeout" in engine.state.paused_reason
    assert evidence in engine.state.paused_reason
    fix = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"][-1]
    assert fix["env_fault"] is True


def test_max_stories_limit(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        max_stories=1,
    )
    summary = engine.run()
    assert summary.done == 1
    assert "1-2-b" not in engine.state.tasks


def test_max_stories_survives_a_pause_resume(project):
    """A5 regression on the SPRINT path: the --max-stories dispatch gate consults
    the durable _dispatched_count(), which replaced a _loop-local counter that
    reset to 0 on every resume (the stories-mode fix rewired the shared base
    gate). With cap=2 and one story committed before an epic-boundary pause, a
    resume must dispatch exactly ONE more story — never re-fill the whole cap."""
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
            "2-2-c": "ready-for-dev",
        },
    )
    gated = Policy(gates=GatesPolicy(mode="per-epic"), notify=QUIET)
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=gated,
        max_stories=2,
    )
    engine.state.max_stories = 2  # cli.py persists this on RunState (helper gap: issue #84)
    summary = engine.run()
    assert summary.done == 1 and summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_EPIC_BOUNDARY

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 2 and not summary2.paused
    final = load_state(resumed.run_dir)
    assert set(final.tasks) == {"1-1-a", "2-1-b"}  # 2-2-c never dispatched — cap durable


def recording_factory(calls: list):
    """An `engine.SweepFactory` double for a child sweep that composes fine:
    records the trigger and signals `started`, the way `runsetup.compose_sweep`
    does once the child owns a published run dir.

    It signals deliberately rather than leaning on the engine's latch-on-plain-
    return arm — a double that never called `started` would quietly measure the
    nothing-was-launched path in every test that uses it. The plain-return arm has
    its own test (`test_auto_sweep_latches_a_factory_that_never_signalled`)."""

    def factory(trigger: str, *, started) -> None:
        started()
        calls.append(trigger)

    return factory


def test_run_end_auto_sweep_fires_once(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=recording_factory(calls),
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert calls == ["run-end"]
    assert load_state(engine.run_dir).sweeps_triggered == ["run-end"]


def test_per_epic_auto_sweep_fires_at_boundary(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="per-epic")
    )
    calls = []
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
            dev_effect(project, "2-1-b"),
            review_effect(project, "2-1-b", clean=True),
        ],
        policy=policy,
        sweep_factory=recording_factory(calls),
    )
    summary = engine.run()
    assert summary.done == 2
    assert calls == ["epic-1"]  # boundary only; run-end mode not set


def test_auto_sweep_no_refire_on_resume(project):
    """The per-epic trigger is recorded before the gate pause, so resuming the run
    must not fire the same sweep again.

    Green-ablation record, so this row is not miscounted as coverage of the latch:
    it stays green with `sweeps_triggered` never written at all, because
    `_epic_boundary` also advances `state.current_epic` before pausing and the
    resumed run therefore never re-detects the epic-1 boundary. Both mechanisms
    hold here; only one of them is the latch.
    `test_auto_sweep_that_started_then_failed_keeps_the_trigger_spent` isolates
    it."""
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="per-epic"),
        notify=QUIET,
        sweep=SweepPolicy(auto="per-epic"),
    )
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=recording_factory(calls),
    )
    assert engine.run().paused
    assert calls == ["epic-1"]

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        sweep_factory=recording_factory(calls),
    )
    assert resumed.run().done == 2
    assert calls == ["epic-1"]  # not re-fired


def test_auto_sweep_failure_does_not_pause_parent(project):
    """A child that raises before signalling `started` never composed, so the
    parent journals `sweep-auto-not-started` and leaves the trigger unspent —
    read back off disk, because the whole subject is durable run state and an
    in-memory list can agree with a state.json that does not.

    Ablation: make the `except (Exception, SystemExit)` arm call `latch()` before
    its `if latched:` branch — spending the trigger on any raise, which is what
    #501 changed — and the reloaded-state assert fails. Not alone: it shares that
    mutation with the three other never-started rows (`..._re_asks_...`,
    `..._system_exit_...`, `..._config_digest_refusal_...`), which is coverage
    rather than duplication, since each names a different way a child declines."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def exploding(trigger, *, started):
        raise RuntimeError("child sweep blew up")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=exploding,
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert engine.state.finished
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-not-started" in journal and "child sweep blew up" in journal
    assert "sweep-auto-failed" not in journal  # nothing ran, so nothing failed
    assert load_state(engine.run_dir).sweeps_triggered == []


def test_auto_sweep_latches_a_factory_that_never_signalled(project):
    """The at-most-once control on the other side: a factory that returns
    normally has run a child sweep, whether or not it bothered to call `started`
    — the thunk exists to classify a *raise*. Without this the plain-return arm
    could be dropped and every remaining test would still pass, because the
    product's own factory signals.

    Ablation: delete the `latch()` call from `_maybe_auto_sweep`'s `else` arm and
    this test fails alone."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []

    def silent(trigger, *, started):
        calls.append(trigger)  # deliberately never calls `started`

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=silent,
    )
    engine.run()

    assert calls == ["run-end"]
    assert load_state(engine.run_dir).sweeps_triggered == ["run-end"]
    assert "sweep-auto-finished" in (engine.run_dir / "journal.jsonl").read_text()


def _re_ask(project, engine, policy, factory) -> Engine:
    """A second engine over the run's state as it was persisted — the shape a
    resume rebuilds, and the only way an already-answered trigger gets asked
    again (see `_maybe_auto_sweep`'s crash-window verdict). Returned unrun so the
    caller drives `_maybe_auto_sweep` directly: reaching the same trigger through
    a whole second `run()` would depend on `finished`/`current_epic`, which is
    exactly what these two tests must not measure."""
    return Engine(
        paths=project,
        policy=policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=load_state(engine.run_dir),
        sweep_factory=factory,
    )


def test_auto_sweep_that_started_then_failed_keeps_the_trigger_spent(project):
    """The at-most-once control, without which the fix is indistinguishable from
    "never latch on a failure". Once `started` fires the child owns a published,
    resumable run dir, so a failure after that point must still spend the trigger
    — and durably, since the re-ask arrives through a rebuilt engine.

    Ablation: empty out `latch()` (keep the def, drop its body) and this test
    fails — the re-ask fires a second child. Not alone: that mutation stops the
    trigger being recorded at all, so it also reddens `test_run_end_auto_sweep_fires_once`,
    `..._latches_a_factory_that_never_signalled` and `..._run_stopped_stops_the_parent`.
    Those three grade "it is recorded"; only this one grades "it survives a
    failure that came after."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []

    def started_then_failed(trigger, *, started):
        started()
        calls.append(trigger)
        raise RuntimeError("child sweep died after its run dir was published")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=started_then_failed,
    )
    summary = engine.run()

    assert summary.done == 1 and engine.state.finished  # parent unaffected
    assert calls == ["run-end"]
    saved = load_state(engine.run_dir)
    assert saved.sweeps_triggered == ["run-end"]
    # The one shape that lands in BOTH records, and the reason `sweeps_refused`
    # is a sibling of the latch rather than a widening of it: the trigger really
    # was spent (a resumable child exists), and the sweep really did not deliver.
    assert saved.sweeps_refused == {"run-end": SWEEP_REFUSED_FAILED}
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-failed" in journal  # it ran, and then it failed
    assert "sweep-auto-not-started" not in journal

    _re_ask(project, engine, policy, started_then_failed)._maybe_auto_sweep("run-end", "run-end")
    assert calls == ["run-end"]  # refused by the persisted latch


def test_auto_sweep_re_asks_a_trigger_whose_child_never_started(project):
    """The twin, and the narrow thing the reordering actually buys: a trigger the
    factory refused before composing is still askable. Not a retry the product
    schedules — `_maybe_auto_sweep`'s docstring establishes that both call sites
    close their own boundary within a few statements — but it is what makes the
    crash window recoverable rather than a permanently spent trigger, and it is
    the observable that grades the whole change.

    Two ablations, and the second is the sharper one. Restore the pre-#501
    ordering — insert `sweeps_triggered.append(trigger)` + `_save()` above the
    `verify.worktree_clean` block — and this test fails, along with nine other
    rows, because that mutation also double-records every successful sweep. Make
    the `except (Exception, SystemExit)` arm call `latch()` unconditionally
    instead, which is the same semantics without the noise, and it fails with
    four (see `test_auto_sweep_failure_does_not_pause_parent`). Neither is
    "alone"; what is unique to this row is the second ask."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []

    def refusing(trigger, *, started):
        calls.append(trigger)
        raise RuntimeError("policy.toml changed under a running loop")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=refusing,
    )
    engine.run()
    assert calls == ["run-end"]
    assert load_state(engine.run_dir).sweeps_triggered == []

    _re_ask(project, engine, policy, refusing)._maybe_auto_sweep("run-end", "run-end")
    assert calls == ["run-end", "run-end"]  # asked again, because nothing was spent


def test_auto_sweep_not_started_still_notifies_with_its_own_wording(project, monkeypatch):
    """`sweep-auto-not-started` keeps a `gates.notify`, and keeps a distinct one.
    Recoverable is not the same as unremarkable: the loudest raise that reaches
    this arm is #461 point 4's config-integrity refusal — a session rewrote the
    verify commands, the launch binary or the plugin allowlist under a running
    loop — and that is a security event whether or not the trigger survived it.
    Losing the alert while gaining the retry would be a bad trade.

    Ablation: delete the `gates.notify` call from the not-started arm and this
    test fails alone. Ablate the WORDING instead — pass the failed arm's "auto
    sweep failed" — and it fails alone again, on the title assert, which is the
    point of asserting the title at all: the two arms describe different things to
    a human deciding whether to intervene."""
    notes = []
    monkeypatch.setattr(
        "bmad_loop.gates.notify",
        lambda policy, rd, title, message: notes.append((title, message)),
    )
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def refusing(trigger, *, started):
        raise RuntimeError("policy.toml/profiles changed under a running loop")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=refusing,
    )
    engine.run()

    # filtered off the run-finished notice the clean-finish path also emits
    sweep_notes = [n for n in notes if "sweep" in n[0]]
    assert len(sweep_notes) == 1
    title, message = sweep_notes[0]
    assert title == "auto sweep did not start"
    assert title != "auto sweep failed"  # the failed arm's wording, deliberately not reused
    assert "run-end" in message and "changed under a running loop" in message


def _sweep_gate_engine(project, sweep_factory):
    """An engine parked at the auto-sweep gate: `run-end` policy, a published
    state.json (so the reload asserts below read a real file rather than error),
    and no story loop — the two refusals under test are driven by calling
    `_maybe_auto_sweep` directly, which is where they are decided."""
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    engine, _ = make_engine(project, [], policy=policy, sweep_factory=sweep_factory)
    engine._save()
    return engine


def _skipped_dirty(engine) -> list[dict]:
    return [e for e in engine.journal.entries() if e["kind"] == "sweep-auto-skipped-dirty"]


def test_auto_sweep_skips_a_dirty_tree_and_keeps_the_trigger(project):
    """First test to reach `sweep-auto-skipped-dirty` at all. A stray uncommitted
    change should not happen at either call site — both sit after a commit or a
    reset — so this arm is a backstop, and until #501 it was a backstop that spent
    the run's one sweep trigger on its way out.

    Ablation: delete the `if not clean:` block and this test fails alone — the
    factory runs on top of the stray change. Second axis, for the ordering rather
    than the refusal: restore the pre-#501 `sweeps_triggered.append` above the
    check and the trigger assert fails (in a set of ten — see
    `test_auto_sweep_re_asks_a_trigger_whose_child_never_started`)."""
    calls = []
    (project.project / "stray.txt").write_text("uncommitted\n", encoding="utf-8")
    engine = _sweep_gate_engine(project, recording_factory(calls))

    engine._maybe_auto_sweep("run-end", "run-end")

    assert calls == []
    assert [e["reason"] for e in _skipped_dirty(engine)] == ["dirty"]
    saved = load_state(engine.run_dir)
    assert saved.sweeps_triggered == []
    # ...and, since #501's visibility phase, the refusal is durable rather than
    # journal-only. Third ablation axis: delete the `_record_sweep_refusal` call
    # from the `if not clean:` block and this line fails while the two above stay
    # green — they grade the refusal, this one grades the record of it.
    assert saved.sweeps_refused == {"run-end": SWEEP_REFUSED_DIRTY}


def test_auto_sweep_skips_a_git_fault_and_keeps_the_trigger(project, monkeypatch):
    """The arm that motivated the reordering, and the one a `reason` field now
    tells apart from a genuinely dirty tree. `verify.worktree_clean` fails closed
    on a `GitError` — right, since an unknown tree state is no basis for a sweep —
    but `_run_git` reports a `subprocess.TimeoutExpired` as exactly that, so a
    `git status` that merely ran long used to spend this run's only sweep trigger,
    permanently and with nothing in the journal to say a *fault* had happened.

    Driven through a real `TimeoutExpired` out of `subprocess.run` rather than a
    stubbed `worktree_clean`, because the translation IS the claim: stubbing the
    GitError would assume the very step that makes this arm transient-reachable.

    Ablation: delete the `except verify.GitError` arm and this test fails alone —
    the GitError escapes `_maybe_auto_sweep` and crashes the run. Second axis, as
    for the dirty twin: restore the pre-#501 `sweeps_triggered.append` above the
    check and the trigger assert fails (in a set of ten)."""
    calls = []
    engine = _sweep_gate_engine(project, recording_factory(calls))

    def timing_out(cmd, **kwargs):
        raise verify.subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(verify.subprocess, "run", timing_out)

    engine._maybe_auto_sweep("run-end", "run-end")

    assert calls == []
    skipped = _skipped_dirty(engine)
    assert [e["reason"] for e in skipped] == ["git-error"]
    assert "timed out" in skipped[0]["error"]  # the fault is named, not swallowed
    saved = load_state(engine.run_dir)
    assert saved.sweeps_triggered == []
    # The durable record folds this arm in with the dirty twin: the journal keeps
    # `git-error` vs `dirty` apart for forensics, while the operator-facing slug
    # answers only "the sweep did not run" — and its next action (`bmad-loop
    # sweep` on a clean tree) is the same either way.
    assert saved.sweeps_refused == {"run-end": SWEEP_REFUSED_DIRTY}


def test_auto_sweep_system_exit_does_not_kill_the_parent(project):
    """#501: a child sweep that dies on `SystemExit` is a failed child like any
    other, and the "never interrupts this run" contract has to cover it. It is a
    `BaseException`, so a guard written over `Exception` missed it here, in every
    arm of `_run_inner`, and in `cli.main`: it unwound to process exit 1 with the
    parent left neither `finished` nor `crashed`, no `run-complete`, and an
    orphaned agent session.

    Not a hypothetical shape — `runsetup.make_adapters` raises exactly this for
    an unresolvable profile, an unknown/unloadable adapter kind, a failed adapter
    construction, an adapter class that rejects a bootstrap keyword, and an
    unusable multiplexer; that last gate re-probes live (`mux_usable` bottoms out
    in a bare `shutil.which`) on every call, so a child sweep can hit it in a
    parent run that launched fine.

    Every one of those six sites is inside `compose_sweep`, ahead of the
    `on_started` boundary, so this models the raise WITHOUT signalling and the
    record is `sweep-auto-not-started`: no child run dir survives an adapter build
    that exits.

    Ablation: drop `SystemExit` from the `except (Exception, SystemExit)` tuple
    in `_maybe_auto_sweep` and this test fails alone — the SystemExit escapes
    `engine.run()` instead of being journaled."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def exiting(trigger, *, started):
        raise SystemExit("error: multiplexer backend is not usable on this host")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=exiting,
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert engine.state.finished
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-not-started" in journal and "not usable on this host" in journal
    assert "run-complete" in journal
    saved = load_state(engine.run_dir)
    assert saved.sweeps_triggered == []
    # The slug, never `str(e)`: the SystemExit message above is free-form operator
    # text and could carry a home path, which `sanitize.guard` refuses to redact —
    # it raises, taking the whole `diagnose` dump down with it. See model.py.
    assert saved.sweeps_refused == {"run-end": SWEEP_REFUSED_NOT_STARTED}
    assert "not usable on this host" not in json.dumps(saved.to_dict())


def test_auto_sweep_run_stopped_stops_the_parent(project, monkeypatch):
    """#501, the mirror image: `RunStopped` subclasses `Exception` but is not a
    failed child at all. The child's hard-stop arm re-raises it *so the owner
    records the stop*, so swallowing it as `sweep-auto-failed` was doubly wrong —
    the parent ran on to `finished`, and it became unstoppable, because the
    signal handler latches `_stopping = True` before raising and every later
    SIGTERM then returns at that latch.

    Signals `started` first, as the real shape does: a child only reaches its own
    stop arm by running, which is well past `compose_sweep`'s boundary. So the
    trigger stays spent here — the stop arm is the one raise that neither
    classifies as a failure nor un-spends anything.

    Ablation: delete the `except RunStopped: raise` arm from `_maybe_auto_sweep`
    and this test fails alone — the stop is swallowed and the parent finishes."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def stopping(trigger, *, started):
        started()
        raise RunStopped()  # hard (graceful=False), as the child's stop arm re-raises

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=stopping,
    )
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped is True
    assert not saved.finished  # the whole point: a stop must not read as a finish
    assert saved.sweeps_triggered == ["run-end"]  # it ran; the stop does not un-spend it
    # And it is not a refusal either — the `except RunStopped: raise` arm is ahead
    # of both recording arms. INVERSE ablation (an absence): add a
    # `_record_sweep_refusal(trigger, SWEEP_REFUSED_FAILED)` to that arm and this
    # line fails, while the journal asserts below stay green.
    assert saved.sweeps_refused == {}
    assert killed == ["test-run"]
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "run-stop" in journal
    assert "sweep-auto-failed" not in journal  # a stop is not a failure
    assert "sweep-auto-not-started" not in journal
    assert "run-complete" not in journal


def test_auto_sweep_keyboard_interrupt_still_propagates(project, monkeypatch):
    """#501, the control on the fix's shape: the two arms above must never be
    widened to a bare `BaseException`. `KeyboardInterrupt` has to keep escaping
    `_maybe_auto_sweep`, because `_run_inner`'s own KeyboardInterrupt arm is what
    records the controlled stop — and, for a nested child, re-raises it for the
    owning engine.

    INVERSE ablation (the guard here is an *absence* — deleting code cannot
    reproduce the bug): widen the clause to `except (BaseException) as e:` and
    this test fails alone — the interrupt is swallowed as `sweep-auto-failed`
    and the parent finishes instead of stopping."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def interrupting(trigger, *, started):
        started()  # Ctrl-C reaches a child that is already running
        raise KeyboardInterrupt()

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=interrupting,
    )
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped is True
    assert not saved.finished
    assert killed == ["test-run"]
    entries = engine.journal.entries()
    stops = [e for e in entries if e["kind"] == "run-stop"]
    assert stops and stops[0]["reason"] == "KeyboardInterrupt"
    assert not [e for e in entries if e["kind"] in ("sweep-auto-failed", "sweep-auto-not-started")]


def test_auto_sweep_config_digest_refusal_journals_and_spares_the_parent(project):
    """#461 point 4, end to end through the REAL factory rather than a stand-in
    exploder: the config-integrity gate's raise has to land on the journal +
    notify path every other unlaunched child takes, and the parent story loop has
    to finish anyway.

    Pinning it here rather than trusting the exploder test above is the point —
    that one proves `_maybe_auto_sweep` catches *something*; this proves the gate
    raises (rather than returning quietly, which the engine would record as
    `sweep-auto-finished`: a child sweep that ran when none was ever launched).

    The gate sits ahead of `_start_sweep`, so it cannot have signalled `started`
    and the trigger survives the refusal — #501. A security refusal is still the
    loudest thing here, which is why `sweep-auto-not-started` keeps its own
    `gates.notify` (pinned by
    `test_auto_sweep_not_started_still_notifies_with_its_own_wording`)."""
    from bmad_loop import cli

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    # A baseline no on-disk config can match — the shape of "a session rewrote
    # policy.toml/profiles between launch and this trigger".
    factory = cli._sweep_factory(project.project, project, "not-the-launch-digest")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=factory,
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert engine.state.finished
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-not-started" in journal
    assert "changed under a running loop before an auto-sweep" in journal
    assert "sweep-auto-finished" not in journal
    assert load_state(engine.run_dir).sweeps_triggered == []


def test_no_auto_sweep_by_default(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        sweep_factory=recording_factory(calls),
    )
    engine.run()
    assert calls == []


def test_journal_records_decisions(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    kinds = [e["kind"] for e in engine.journal.entries()]
    for expected in (
        "story-start",
        "session-start",
        "dev-decision",
        "review-result",
        "story-done",
        "run-complete",
    ):
        assert expected in kinds


def test_sessions_stamp_resolved_adapter_identity(project):
    """#153 phase 1: every session's session-start journal entry and persisted
    SessionRecord carry the resolved adapter profile + model. A review-stage name
    override switches the profile and (per AdapterPolicy.resolved) resets the
    model to the CLI default "" because model is client-specific."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            review=StageAdapterPolicy(name="gemini"),
        ),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()

    starts = {e["role"]: e for e in engine.journal.entries() if e["kind"] == "session-start"}
    assert set(starts) == {"dev", "review"}
    expected_dev = policy.adapter.resolved("dev")
    expected_review = policy.adapter.resolved("review")
    # base model rides the dev stage; the review name switch resets it to ""
    assert (expected_dev.name, expected_dev.model) == ("claude", "opus")
    assert (expected_review.name, expected_review.model) == ("gemini", "")
    for role, expected in (("dev", expected_dev), ("review", expected_review)):
        entry = starts[role]
        assert entry["adapter"] == expected.name
        assert entry["model"] == expected.model
        assert entry["story_key"] == "1-1-a"

    saved = load_state(engine.run_dir)
    records = {r.role: r for r in saved.tasks["1-1-a"].sessions}
    assert (records["dev"].adapter, records["dev"].model) == ("claude", "opus")
    assert (records["review"].adapter, records["review"].model) == ("gemini", "")


def test_journal_stamps_log_position(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.set_active_log("t-dev-1")
    journal.append("session-start", task_id="t-dev-1")  # log file not created yet
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-dev-1.log").write_bytes(b"x" * 37)
    journal.append("dev-decision", story_key="1-1-a")
    journal.append("custom", log_task="elsewhere", log_pos=5)  # caller fields win

    entries = journal.entries()
    assert "log_task" not in entries[0] and "log_pos" not in entries[0]
    assert entries[1]["log_task"] == "t-dev-1" and entries[1]["log_pos"] == 0
    assert entries[2]["log_task"] == "t-dev-1" and entries[2]["log_pos"] == 37
    assert entries[3]["log_task"] == "elsewhere" and entries[3]["log_pos"] == 5


def test_journal_log_position_covers_post_session_entries(project):
    """The active log is set at session-start and deliberately not cleared:
    post-session entries (decisions, story-done) point at the end of the log
    of the session they are about."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    entries = engine.journal.entries()
    starts = [e for e in entries if e["kind"] == "session-start"]
    assert len(starts) == 2  # dev + review
    assert all(e["log_task"] == e["task_id"] for e in starts)
    assert all(isinstance(e["log_pos"], int) for e in starts)
    story_start = next(e for e in entries if e["kind"] == "story-start")
    assert "log_task" not in story_start  # written before any session
    dev_decision = next(e for e in entries if e["kind"] == "dev-decision")
    assert dev_decision["log_task"] == starts[0]["task_id"]
    story_done = next(e for e in entries if e["kind"] == "story-done")
    assert story_done["log_task"] == starts[-1]["task_id"]


# ----------------------------------------------------------- stop / SIGTERM


@pytest.mark.parametrize(
    ("signal_name", "fallback_signum"),
    [("SIGINT", signal.SIGINT), ("SIGBREAK", 21)],
)
def test_windows_console_ctrl_signal_is_ignored(project, monkeypatch, signal_name, fallback_signum):
    import bmad_loop.engine as engine_mod

    signum = getattr(signal, signal_name, fallback_signum)
    if signal_name == "SIGBREAK":
        monkeypatch.setattr(signal, "SIGBREAK", signum, raising=False)

    installed = {}
    restored = {}
    previous = {}

    def fake_signal(sig, handler):
        previous.setdefault(sig, object())
        if callable(handler):
            installed[sig] = handler
        else:
            restored[sig] = handler
        return previous[sig]

    monkeypatch.setattr(engine_mod.sys, "platform", "win32")
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: None)

    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_loop", lambda: installed[signum](signum, None))

    summary = engine.run()

    assert summary is not None
    assert load_state(engine.run_dir).stopped is False
    assert "console-ctrl-ignored" in (engine.run_dir / "journal.jsonl").read_text()
    assert restored[signal.SIGTERM] is previous[signal.SIGTERM]
    assert restored[signal.SIGINT] is previous[signal.SIGINT]
    assert restored[signum] is previous[signum]
    assert Engine._stop_signals_owner is None


def test_non_windows_sigint_still_stops_run(project, monkeypatch):
    import bmad_loop.engine as engine_mod

    installed = {}
    previous = {}

    def fake_signal(sig, handler):
        previous.setdefault(sig, object())
        if callable(handler):
            installed[sig] = handler
        return previous[sig]

    killed = []
    monkeypatch.setattr(engine_mod.sys, "platform", "linux")
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))

    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_loop", lambda: installed[signal.SIGINT](signal.SIGINT, None))

    engine.run()

    assert load_state(engine.run_dir).stopped is True
    assert killed == ["test-run"]
    assert "run-stop" in (engine.run_dir / "journal.jsonl").read_text()
    assert Engine._stop_signals_owner is None


def test_run_stopped_via_real_signal(project, monkeypatch):
    """SIGTERM unwinds the loop as RunStopped: the run is marked stopped, the
    agent session is torn down, and the prior signal handlers are restored."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])
    # raise_signal delivers an in-process, catchable SIGTERM via C raise() — the
    # portable "signal myself" primitive. os.kill(getpid(), SIGTERM) is POSIX-only
    # here: on Windows it maps to TerminateProcess (uncatchable, kills the runner).
    monkeypatch.setattr(engine, "_loop", lambda: signal.raise_signal(signal.SIGTERM))

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    summary = engine.run()

    assert summary is not None
    assert load_state(engine.run_dir).stopped is True
    assert killed == ["test-run"]
    assert "run-stop" in (engine.run_dir / "journal.jsonl").read_text()
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int
    assert Engine._stop_signals_owner is None


def test_nested_engine_reraises_runstopped(project, monkeypatch):
    """A nested auto-sweep engine does not own the handlers, so it re-raises
    RunStopped for the outer (owning) engine to record — it still tears down
    its own agent session."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RunStopped()

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(RunStopped):
            engine.run()
    finally:
        _run_depth.reset(token)
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).stopped is False  # owner records it, not us
    assert killed == ["test-run"]


# ----------------------------------------------------------- crash safety-net


def test_run_crash_records_diagnostics(project, monkeypatch):
    """An unexpected exception out of the loop is recorded (state flag, journal,
    persisted traceback) instead of crashing the orchestrator: the orphaned
    agent session is torn down and a crashed summary is returned."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    summary = engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crashed is True
    assert state.crash_error.startswith("RuntimeError")
    assert state.finished is False
    assert killed == ["test-run"]
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    crash_txt = (engine.run_dir / "crash.txt").read_text()
    assert "Traceback" in crash_txt
    assert "boom" in crash_txt
    assert summary.crashed is True
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int
    assert Engine._stop_signals_owner is None


def test_nested_engine_reraises_crash(project, monkeypatch):
    """A nested auto-sweep engine does not own the handlers, so an unexpected
    exception re-raises for the outer engine to record — it still persists its
    own traceback and tears down its agent session, but records no run-crash."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # outer engine owns signals (child won't install)
    token = _run_depth.set(1)  # ...and this run is nested inside the outer run() frame
    try:
        with pytest.raises(RuntimeError):
            engine.run()
    finally:
        _run_depth.reset(token)
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).crashed is False  # owner records it, not us
    assert killed == ["test-run"]
    assert (engine.run_dir / "crash.txt").read_text()  # traceback still persisted
    journal = engine.run_dir / "journal.jsonl"
    assert not journal.exists() or "run-crash" not in journal.read_text()


def test_run_crash_after_finish_clears_finished(project, monkeypatch):
    """A post-loop step that throws after finished=True is recorded as a crash
    and the finished flag is cleared, so status classification reads CRASHED
    rather than FINISHED (which it checks first)."""
    from bmad_loop import runs

    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])  # loop completes → sets finished=True

    def boom():
        raise RuntimeError("post-run boom")

    monkeypatch.setattr(engine, "_gc_run_worktrees", boom)

    summary = engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crashed is True
    assert state.finished is False  # the masking flag was cleared
    assert "Traceback" in (engine.run_dir / "crash.txt").read_text()
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    assert summary.crashed is True
    # the real payoff: it classifies as CRASHED, not FINISHED
    assert (
        runs._classify(state.finished, state.paused, state.stopped, state.crashed, engine.run_dir)
        == runs.CRASHED
    )


def test_top_level_crash_without_signal_handlers_still_records(project, monkeypatch):
    """A top-level engine that could not install signal handlers (e.g. off the
    main thread) is not nested, so an unexpected exception is recorded rather
    than re-raised — the crash-gap stays closed in non-CLI usage paths."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])
    # simulate signal.signal failing: no handlers installed, no owner, not nested
    monkeypatch.setattr(engine, "_install_stop_signals", lambda: None)

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", boom)

    summary = engine.run()  # must NOT raise even though _owns_signals is False

    state = load_state(engine.run_dir)
    assert engine._is_nested is False
    assert engine._owns_signals is False
    assert state.crashed is True
    assert "run-crash" in (engine.run_dir / "journal.jsonl").read_text()
    assert summary.crashed is True


def test_crash_message_fallback_when_str_raises(project, monkeypatch):
    """If the exception's own __str__ raises, the fallback uses the bare type
    name (not its repr) so crash_error reads 'BadStr: BadStr', not quoted."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    engine, _ = make_engine(project, [])

    class BadStr(Exception):
        def __str__(self):
            raise ValueError("nope")

    monkeypatch.setattr(engine, "_loop", lambda: (_ for _ in ()).throw(BadStr()))

    engine.run()  # does not raise

    state = load_state(engine.run_dir)
    assert state.crash_error == "BadStr: BadStr"
    assert "'" not in state.crash_error


def _escalate_blocked(project, story_key):
    """A dev session that HALTs `blocked` with a spec on disk (so rearm can flip
    it) — the environmental-block shape from the live Epic-9 run."""

    def effect(spec):
        sp = spec_path(project, story_key)
        write_spec(sp, "blocked", rev_parse_head(project.project))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "escalations": [
                    {"type": "blocked", "severity": "CRITICAL", "detail": "unity bridge wedged"}
                ],
            },
        )

    return effect


def test_resume_with_epic_filter_stays_in_scoped_epic(project):
    """Regression for the Epic-9 jump: a `--epic 9` run whose first story (9-0,
    story index 0) escalates, is resolved, and resumes must keep picking WITHIN
    epic 9 — not widen to every epic and bounce to an earlier-in-file epic. The
    fixture is document-ordered (epic 5 before epic 9), not numeric, exactly like
    the real sprint board."""
    write_sprint(
        project,
        {
            "epic-5": "backlog",
            "5-1-map": "ready-for-dev",
            "epic-9": "backlog",
            "9-0-test-infra": "ready-for-dev",  # story numbered 0, leads the epic
            "9-1-keystone": "ready-for-dev",
        },
    )
    engine, _ = make_engine(project, [_escalate_blocked(project, "9-0-test-infra")], epic_filter=9)
    engine.state.epic_filter = 9  # cmd_run persists the launch scope; mirror it here
    summary = engine.run()
    assert summary.paused and summary.escalated == 1
    assert engine.state.current_epic == 9

    rearm_escalation(engine.run_dir)  # the resolve workflow's re-arm step
    resumed, _ = resume_engine(
        project,
        engine,
        [
            dev_effect(project, "9-0-test-infra"),
            review_effect(project, "9-0-test-infra", clean=True),
            dev_effect(project, "9-1-keystone"),
            review_effect(project, "9-1-keystone", clean=True),
        ],
    )
    summary2 = resumed.run()

    # both epic-9 stories completed; epic 5 never touched; no false boundary
    assert summary2.done == 2 and not summary2.paused
    assert resumed.state.tasks["9-0-test-infra"].phase == Phase.DONE
    assert resumed.state.tasks["9-1-keystone"].phase == Phase.DONE
    assert "5-1-map" not in resumed.state.tasks
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "epic-boundary" not in kinds


def test_pick_next_prefers_current_epic_over_earlier_file_position(project):
    """Fix B (hardening): selection exhausts the current epic before advancing,
    even when an actionable story of another epic sits earlier in file order.
    Then, once the epic is exhausted, it falls back to file order — preserving
    document-order epic execution."""
    write_sprint(
        project,
        {
            "5-1-e5": "backlog",  # earlier in file, actionable, but NOT current epic
            "9-0-x": "ready-for-dev",
            "9-1-y": "backlog",
        },
    )
    engine, _ = make_engine(project, [])
    engine.state.current_epic = 9
    engine.state.tasks["9-0-x"] = StoryTask(story_key="9-0-x", epic=9, phase=Phase.DEFERRED)

    assert engine._pick_next().key == "9-1-y"  # stays in epic 9, not 5-1-e5

    # exhaust epic 9 → fallback returns the earlier-in-file epic (doc order kept)
    engine.state.tasks["9-1-y"] = StoryTask(story_key="9-1-y", epic=9, phase=Phase.DONE)
    assert engine._pick_next().key == "5-1-e5"


def test_resolved_redrive_reescalates_instead_of_deferring(project):
    """Fix C (Bug 1): a story from a human-resolved CRITICAL escalation whose
    re-drive still can't converge must RE-ESCALATE (pause for the human), not
    silently plateau-defer + roll back the work. The live run downgraded an
    environmental block to a deferral this way."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_dev_attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(project, [_escalate_blocked(project, "1-1-a")], policy=policy)
    summary = engine.run()
    assert summary.paused and summary.escalated == 1

    rearm_escalation(engine.run_dir)  # human resolved; re-drive re-armed
    # re-drive never reaches `done` (env still blocked): both attempts land at
    # in-progress with no escalation — the exact non-convergence that used to defer
    resumed, _ = resume_engine(
        project,
        engine,
        [
            dev_effect(project, "1-1-a", final_status="in-progress"),
            dev_effect(project, "1-1-a", final_status="in-progress"),
        ],
        policy=policy,
    )
    summary2 = resumed.run()

    assert summary2.paused and summary2.escalated == 1 and summary2.deferred == 0
    task = load_state(resumed.run_dir).tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert task.defer_reason is None
    kinds = [e["kind"] for e in resumed.journal.entries()]
    assert "story-deferred" not in kinds
    saved = load_state(resumed.run_dir)
    assert "re-escalating instead of deferring" in saved.paused_reason


# --------------------------------------------------------------- graceful stop
#
# A graceful stop is delivered out-of-band via the stop-request.json control file
# (Phase 1's runs helpers): the engine consumes it at an item boundary and unwinds
# into a clean-finalization arm, leaving the run `stopped` + resumable. These tests
# lodge the control file directly (an existence read is all the engine checks) and
# drive it through the mock adapter — no signals, no live requester process.


def _lodge_stop_request(run_dir: Path) -> None:
    """Drop the graceful-stop control file the way a CLI/TUI requester would — the
    engine only checks its existence, so a minimal body suffices."""
    (run_dir / STOP_REQUEST_FILE).write_text(
        '{"requested_at": "2026-07-20T00:00:00", "mode": "graceful"}', encoding="utf-8"
    )


def _lodge_after(inner, run_dir: Path):
    """Wrap a scripted effect so the graceful-stop request lands mid-story (right
    as ``inner`` returns), proving the in-flight item still runs to completion —
    the boundary check only fires at the next loop head."""

    def effect(spec):
        result = inner(spec)
        _lodge_stop_request(run_dir)
        return result

    return effect


def test_graceful_stop_finishes_current_story_then_stops(project, monkeypatch):
    """The in-flight story runs to DONE (dev→review→commit), the next story is never
    dispatched, and the run ends `stopped` + resumable — not `finished`."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.DONE
    assert "1-2-b" not in saved.tasks  # story 2 never dispatched
    assert saved.stopped is True and saved.finished is False
    assert not graceful_stop_requested(run_dir)  # control file consumed at the boundary
    assert summary.done == 1 and not summary.paused
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-complete" not in kinds
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    assert stops[-1]["remaining"] == 1  # 1-2-b still actionable, never picked


def test_graceful_stop_runs_clean_finalization_and_notifies(project, monkeypatch):
    """Unlike a hard stop, the graceful arm runs worktree GC + the post_run hook +
    the policy-gated session teardown, and the trailing notify is worded for a
    graceful stop with a resume hint."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    notes = []
    monkeypatch.setattr(
        "bmad_loop.gates.notify",
        lambda policy, rd, title, message: notes.append((title, message)),
    )
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )

    gc_calls = []
    original_gc = engine._gc_run_worktrees
    monkeypatch.setattr(
        engine, "_gc_run_worktrees", lambda: (gc_calls.append(True), original_gc())[1]
    )
    post_run_stages = []
    original_emit = engine._emit

    def spy_emit(stage, *args, **kwargs):
        if stage == "post_run":
            post_run_stages.append(stage)
        return original_emit(stage, *args, **kwargs)

    engine._emit = spy_emit

    engine.run()

    assert gc_calls == [True]  # worktree GC ran on the graceful path
    assert post_run_stages == ["post_run"]  # post_run hook fired
    assert killed == ["test-run"]  # session torn down (owns_signals + cleanup_on_finish)
    assert notes and notes[-1][0] == "bmad-loop run stopped gracefully"
    assert "bmad-loop resume test-run" in notes[-1][1]
    assert "1 story remaining" in notes[-1][1]


def test_epic_boundary_auto_sweep_suppressed_by_graceful_stop(project, monkeypatch):
    """A graceful stop lodged during epic 1 ends the run at the next loop head,
    before the epic-2 pick — so the per-epic child sweep never fires and epic 2 is
    never dispatched."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="per-epic")
    )
    calls = []
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
        sweep_factory=recording_factory(calls),
    )
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped and "2-1-b" not in saved.tasks
    assert calls == []  # no child sweep started
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-auto-trigger" not in kinds


def test_maybe_auto_sweep_suppressed_when_graceful_stop_pending(project):
    """The run-end race: a request landing after the loop-head check reaches
    _maybe_auto_sweep, which suppresses the sweep (return, not raise) and leaves
    the trigger unspent.

    Unspent is honest bookkeeping, NOT a promised retry — at this call site the
    return lands in `_loop`'s exit and then `finished = True`, which
    `cli._resume_paused_run` refuses to resume. See `_maybe_auto_sweep`'s
    docstring for the same verdict at both call sites."""
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [], policy=policy, sweep_factory=recording_factory(calls))
    _lodge_stop_request(run_dir)

    engine._maybe_auto_sweep("run-end", "run-end")

    assert calls == []  # no child sweep started
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-auto-suppressed" in kinds
    assert "sweep-auto-trigger" not in kinds
    assert "run-end" not in engine.state.sweeps_triggered  # nothing started, nothing spent
    # ...and deliberately NOT in `sweeps_refused` either, unlike every other
    # non-delivering arm. This return sits AHEAD of the latch, so a resume can
    # still fire the trigger — recording a refusal here would go stale the moment
    # it does, which is exactly the dishonesty the record exists to prevent. The
    # operator already has a louder signal: they asked for the stop.
    #
    # INVERSE ablation (the guard is an absence — deleting code cannot reproduce
    # it): add `self._record_sweep_refusal(trigger, SWEEP_REFUSED_DIRTY)` above
    # the suppressed journal append and this line fails alone.
    assert engine.state.sweeps_refused == {}


def test_pause_wins_over_pending_graceful_stop(project, monkeypatch):
    """A pause raised while a stop is pending wins; the finally discards the stale
    control file and journals stop-request-discarded (no run-stop)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    def pausing_loop():
        _lodge_stop_request(run_dir)
        raise RunPaused("epic gate", PAUSE_EPIC_BOUNDARY, None)

    monkeypatch.setattr(engine, "_loop", pausing_loop)
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.paused and not saved.stopped
    assert not graceful_stop_requested(run_dir)  # discarded in finally
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "stop-request-discarded" in kinds
    assert "run-stop" not in kinds


def test_hard_stop_wins_over_pending_graceful_stop(project, monkeypatch):
    """A hard RunStopped (SIGTERM) supersedes a pending graceful request: the run
    records run-stop WITHOUT the graceful flag, tears the session down
    unconditionally, and the finally discards the stale file. Contrast the graceful
    arm: the hard path does not emit post_run."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    post_run_stages = []
    original_emit = engine._emit

    def spy_emit(stage, *args, **kwargs):
        if stage == "post_run":
            post_run_stages.append(stage)
        return original_emit(stage, *args, **kwargs)

    engine._emit = spy_emit

    def stopping_loop():
        _lodge_stop_request(run_dir)
        raise RunStopped()  # hard (graceful=False), as the signal handler raises

    monkeypatch.setattr(engine, "_loop", stopping_loop)
    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped
    assert not graceful_stop_requested(run_dir)  # finally discarded it
    assert killed == ["test-run"]  # hard stop's unconditional teardown
    assert post_run_stages == []  # hard path skips the clean-finish subset
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and "graceful" not in stops[-1]
    assert "stop-request-discarded" in [e["kind"] for e in engine.journal.entries()]


def test_crash_wins_over_pending_graceful_stop(project, monkeypatch):
    """An unexpected crash while a stop is pending wins; the crash arm records and
    the finally discards the stale control file (no run-stop)."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [])

    def crashing_loop():
        _lodge_stop_request(run_dir)
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_loop", crashing_loop)
    summary = engine.run()

    assert summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.crashed and not saved.stopped
    assert not graceful_stop_requested(run_dir)  # discarded in finally
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-crash" in kinds and "stop-request-discarded" in kinds
    assert "run-stop" not in kinds


def test_graceful_stop_finalize_error_still_records_stop(project, monkeypatch):
    """A post_run hook that raises during graceful finalization is caught inline
    (an except-arm raise would escape run() uncaught): the run still records
    run-stop + run-stop-finalize-error and never crashes."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    original_emit = engine._emit

    def boom_on_post_run(stage, *args, **kwargs):
        if stage == "post_run":
            raise RuntimeError("post_run plugin exploded")
        return original_emit(stage, *args, **kwargs)

    engine._emit = boom_on_post_run

    summary = engine.run()  # must NOT raise

    assert not summary.crashed
    saved = load_state(engine.run_dir)
    assert saved.stopped and not saved.finished and not saved.crashed
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-stop-finalize-error" in kinds
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True


def test_resume_after_graceful_stop_completes_remaining(project, monkeypatch):
    """A graceful-stopped run is resumable with zero special handling: the resume
    dispatches the story that never ran and finishes."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(
        project,
        [
            _lodge_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    engine.run()
    assert load_state(engine.run_dir).stopped
    assert not graceful_stop_requested(run_dir)

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-2-b"), review_effect(project, "1-2-b", clean=True)],
    )
    summary2 = resumed.run()

    assert summary2.done == 2 and not summary2.paused
    final = load_state(resumed.run_dir)
    assert set(final.tasks) == {"1-1-a", "1-2-b"}
    assert final.finished and not final.stopped


def test_graceful_stop_on_resume_finishes_inflight_then_stops(project, monkeypatch):
    """A request pending when a resume starts is honored AFTER _finish_inflight: the
    in-flight item completes, then the first loop-head check stops the run before
    any new story is picked."""
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: None)
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")])
    original_emit = engine._emit

    def crashing_emit(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died in the post-session window")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit
    assert engine.run().crashed
    assert load_state(engine.run_dir).tasks["1-1-a"].phase == Phase.DEV_RUNNING

    # a graceful stop is lodged before the resume even starts
    _lodge_stop_request(run_dir)
    resumed, adapter = resume_engine(project, engine, [review_effect(project, "1-1-a", clean=True)])
    resumed.run()

    final = load_state(resumed.run_dir)
    assert final.tasks["1-1-a"].phase == Phase.DONE  # in-flight item finished
    assert "1-2-b" not in final.tasks  # no NEW pick after the boundary
    assert final.stopped and not final.finished
    assert not graceful_stop_requested(run_dir)  # consumed at the first loop head
    assert [s.role for s in adapter.sessions] == ["review"]  # only the in-flight review ran
    stops = [e for e in resumed.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True


# ----------------------------------------------------------- hard stop (#319)
#
# The same stop-request.json control file, written with `mode: "hard"` by
# `runs.stop_run`, carries a stop the signal path cannot deliver: a native-Windows
# engine never receives an inter-process SIGTERM. The engine honors it in three
# places — the item boundary (`_check_stop_request`) and the two `_run_session`
# raise sites: an `"aborted"` result handed back by the adapter's in-session poll
# (site A), and a hard file still on disk once a session has been recorded and
# saved (site B, the `_post_kill_reconcile`-rescued case). All three take the HARD
# arm: unconditional teardown, `run-stop` carrying `via="stop-request"` and no
# `graceful` flag. These tests lodge the control file directly, exactly as the
# graceful ones do.


def _lodge_hard_stop_request(run_dir: Path) -> None:
    """Drop the control file `runs.stop_run` writes before it signals — the hard
    sibling of :func:`_lodge_stop_request`."""
    (run_dir / STOP_REQUEST_FILE).write_text(
        '{"requested_at": "2026-07-20T00:00:00", "mode": "hard"}', encoding="utf-8"
    )


def _lodge_hard_after(inner, run_dir: Path):
    """Wrap a scripted effect so a HARD request lands as ``inner`` returns — i.e.
    in the window between the adapter's last poll and the engine's post-session
    check, the gap raise site B exists to cover."""

    def effect(spec):
        result = inner(spec)
        _lodge_hard_stop_request(run_dir)
        return result

    return effect


def test_session_abort_status_unwinds_run_stopped(project, monkeypatch):
    """RAISE SITE A. An adapter whose wait loop saw the hard file tears its window
    down and hands back `status="aborted"`; the engine unwinds that into a hard
    RunStopped *before* any SessionRecord exists, so an abort is never mistaken for
    a session outcome and no further session is launched. The paired session-end is
    still journaled — through the `finally`, which is why the raise sits inside the
    try.

    Ablation: delete the `result.status == "aborted"` gate in `_run_session` and the
    abort is recorded as an ordinary session; the run drives on into the review leg
    and this test fails (verified)."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [SessionResult(status="aborted"), review_effect(project, "1-1-a", clean=True)],
    )

    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped is True and saved.finished is False
    assert killed == ["test-run"]  # hard arm's unconditional teardown
    assert len(adapter.sessions) == 1  # nothing launched after the abort...
    assert len(adapter.script) == 1  # ...the review entry is still unspent
    assert saved.tasks["1-1-a"].sessions == []  # no SessionRecord for the abort
    entries = engine.journal.entries()
    assert "run-complete" not in [e["kind"] for e in entries]
    starts = [e for e in entries if e["kind"] == "session-start"]
    ends = [e for e in entries if e["kind"] == "session-end"]
    assert len(starts) == 1 and len(ends) == 1  # paired through the finally
    assert ends[0]["task_id"] == starts[0]["task_id"]
    assert ends[0]["status"] == "aborted"
    stops = [e for e in entries if e["kind"] == "run-stop"]
    assert stops and stops[-1]["via"] == "stop-request"
    assert "graceful" not in stops[-1]


def test_hard_stop_after_completed_session_stops_before_next_leg(project, monkeypatch):
    """RAISE SITE B. A hard request landing too late for the in-session poll — here
    as the dev session returns `completed` — still stops the run. This is also the
    rescued-completion shape: `_post_kill_reconcile` can upgrade an aborted session
    back to `completed`, and without this site the run would carry straight on into
    verify/review on the strength of that rescue. The session is fully recorded and
    saved first, so the run stays resumable from a complete record.

    Ablation: delete raise site B (the post-`_save()` hard-file check in
    `_run_session`) and the run drives the review leg — `adapter.sessions` grows to
    2 and this test fails (verified)."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, adapter = make_engine(
        project,
        [
            _lodge_hard_after(dev_effect(project, "1-1-a"), run_dir),
            review_effect(project, "1-1-a", clean=True),
        ],
    )

    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.stopped is True and saved.finished is False
    assert killed == ["test-run"]
    assert len(adapter.sessions) == 1  # the review leg never started...
    assert len(adapter.script) == 1  # ...its script entry is still unspent
    # the completed session IS on the record — durable before the stop unwound
    records = saved.tasks["1-1-a"].sessions
    assert len(records) == 1 and records[0].status == "completed"
    assert not graceful_stop_requested(run_dir)  # consumed before the raise
    entries = engine.journal.entries()
    kinds = [e["kind"] for e in entries]
    assert "run-complete" not in kinds
    assert "stop-request-discarded" not in kinds  # honored, not discarded as stale
    ends = [e for e in entries if e["kind"] == "session-end"]
    assert len(ends) == 1 and ends[0]["status"] == "completed"
    stops = [e for e in entries if e["kind"] == "run-stop"]
    assert stops and stops[-1]["via"] == "stop-request"
    assert "graceful" not in stops[-1]


def test_boundary_hard_file_takes_hard_arm(project, monkeypatch):
    """A hard request that lands after the story's last session — lodged at
    `post_story`, past raise site B — is honored at the next item boundary and takes
    the HARD arm there: unconditional teardown, no clean-finish subset, `run-stop`
    with `via="stop-request"` and no `graceful` flag. The in-flight story still ran
    to completion, exactly as the graceful sibling does; only the arm differs."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )

    post_run_stages = []
    original_emit = engine._emit

    def spy_emit(stage, *args, **kwargs):
        if stage == "post_run":
            post_run_stages.append(stage)
        if stage == "post_story":
            # the operator's `bmad-loop stop` lands between story 1 and story 2
            _lodge_hard_stop_request(run_dir)
        return original_emit(stage, *args, **kwargs)

    engine._emit = spy_emit

    engine.run()

    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.DONE  # in-flight story finished
    assert "1-2-b" not in saved.tasks  # the next story was never dispatched
    assert saved.stopped is True and saved.finished is False
    assert len(adapter.sessions) == 2  # dev + review, nothing after the boundary
    assert not graceful_stop_requested(run_dir)  # consumed at the boundary
    assert killed == ["test-run"]  # hard arm, not the policy-gated graceful teardown
    assert post_run_stages == []  # hard path skips the clean-finish subset
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-complete" not in kinds
    assert "stop-request-discarded" not in kinds  # honored, not discarded as stale
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["via"] == "stop-request"
    assert "graceful" not in stops[-1] and "remaining" not in stops[-1]


def test_boundary_modeless_file_reads_graceful(project, monkeypatch):
    """BACK-COMPAT PIN. A bare `{}` stop-request body — what every pre-#319 writer
    and fixture lodged — still takes the GRACEFUL arm: `read_stop_request_mode`
    reads any present-but-modeless file as graceful, so raise site B ignores it
    mid-story and the boundary finalizes cleanly with `graceful=True` and no
    `via`."""
    killed = []
    monkeypatch.setattr("bmad_loop.engine.kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"

    def lodge_modeless(inner):
        def effect(spec):
            result = inner(spec)
            (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
            return result

        return effect

    engine, adapter = make_engine(
        project,
        [
            lodge_modeless(dev_effect(project, "1-1-a")),
            review_effect(project, "1-1-a", clean=True),
        ],
    )

    engine.run()

    saved = load_state(engine.run_dir)
    # the modeless file did NOT abort mid-story: the review leg still ran
    assert len(adapter.sessions) == 2
    assert saved.tasks["1-1-a"].phase == Phase.DONE
    assert "1-2-b" not in saved.tasks
    assert saved.stopped is True and saved.finished is False
    assert not graceful_stop_requested(run_dir)
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    assert "via" not in stops[-1]


# ------------------------------- review.on_timeout = "salvage-if-done" (#271)


def _salvage_policy(**limits_kw) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(**limits_kw),
        review=ReviewPolicy(on_timeout="salvage-if-done"),
    )


def test_review_timeout_salvage_commits_and_refiles(project):
    """A review timeout over an already-finalized, verify-green dev product
    converges: the work commits, the outstanding follow-up refiles to deferred
    work, and no further review cycle burns."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), SessionResult(status="timeout")],
        policy=_salvage_policy(),
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    task = engine.state.tasks["1-1-a"]
    (salvage,) = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]
    assert salvage["session_status"] == "timeout"
    assert salvage["cycle"] == 1
    assert salvage["reset_from"] is None  # the review never touched the done spec
    assert salvage["refiled"] and salvage["refiled"].startswith("DW-")
    ledger = project.deferred_work.read_text(encoding="utf-8")
    assert "origin: review-timeout-salvage" in ledger
    assert "1-1-a" in ledger
    # the timeout neither re-arms the recommendation nor spends a damping grant
    assert task.followup_review_recommended is False
    assert task.followup_reviews_spent == 0
    assert not [e for e in engine.journal.entries() if e["kind"] == "review-retry"]


def test_review_timeout_salvage_resets_in_review_spec(project):
    """The mid-review interrupt: the dying pass flipped the transient in-review
    marker. Salvage resets it forward to done and commits."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def timeout_mid_review(spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-review", _spec_baseline(sp))
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_mid_review],
        policy=_salvage_policy(),
    )
    summary = engine.run()

    assert summary.done == 1
    (salvage,) = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]
    assert salvage["reset_from"] == "in-review"
    fm = read_frontmatter(spec_path(project, "1-1-a"))
    assert str(fm.get("status")) == "done"


def test_review_timeout_salvage_verify_fail_falls_back(project):
    """A failing verify gate refuses the salvage (journaled) and falls back to
    the default retry/exhaust routing — never ships unverified work."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # green at dev-verify time; the dying review breaks it, so only the salvage
    # gate (not the dev gate) sees the failure
    marker = project.project / "verify-marker.txt"
    marker.write_text("ok", encoding="utf-8")
    policy = dataclasses.replace(
        _salvage_policy(max_review_cycles=1),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )

    def timeout_and_break_verify(spec):
        marker.unlink()
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_and_break_verify],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    (failed,) = [
        e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage-failed"
    ]
    assert failed["cycle"] == 1
    task = engine.state.tasks["1-1-a"]
    assert "salvage not applicable" in task.defer_reason
    assert not [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage"]


def test_review_timeout_salvage_nonterminal_spec_falls_back(project):
    """A spec at a non-terminal status (unfinished dev work) is never salvaged
    over — no salvage event at all, straight to the fallback routing."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def timeout_mid_work(spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-progress", _spec_baseline(sp))
        return SessionResult(status="timeout")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), timeout_mid_work],
        policy=_salvage_policy(max_review_cycles=1),
    )
    summary = engine.run()

    assert summary.deferred == 1
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-timeout-salvage" not in kinds
    assert "review-timeout-salvage-failed" not in kinds
    assert "salvage not applicable" in engine.state.tasks["1-1-a"].defer_reason


def test_review_timeout_salvage_skipped_under_isolation(project):
    """Worktree isolation: a defer already preserves the unit's worktree + diff,
    and committing into the main repo would be wrong — salvage never applies."""
    policy = dataclasses.replace(
        _salvage_policy(), scm=ScmPolicy(rollback_on_failure=True, isolation="worktree")
    )
    engine, _ = make_engine(project, [], policy=policy)
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec_path(project, "1-1-a")))
    assert engine._salvage_review_timeout(task, SessionResult(status="timeout")) is False


def test_review_timeout_salvage_requires_spec_file(project):
    engine, _ = make_engine(project, [], policy=_salvage_policy())
    task = StoryTask(story_key="1-1-a", epic=1)  # no spec recorded yet
    assert engine._salvage_review_timeout(task, SessionResult(status="timeout")) is False


def test_session_synthesized_from_frontmatter_journaled(project):
    """The adapter's missing-marker synthesis flag (#224) leaves the same style
    of forensic breadcrumb as a post-kill rescue."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a")

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        return result

    engine, _ = make_engine(
        project,
        [synthesized_dev, review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()
    assert summary.done == 1
    (crumb,) = [
        e for e in engine.journal.entries() if e["kind"] == "session-synthesized-from-frontmatter"
    ]
    assert crumb["role"] == "dev"


def test_synthesized_result_repairs_spec_marker(project):
    """#276 M3: when the fallback synthesizes a dev result the engine appends the
    marker the skill owed onto the on-disk spec — with provenance — and journals
    `spec-marker-repaired`. The story still completes; verify/commit are unaffected
    (the marker is prose; the frontmatter status the gates read is unchanged)."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # No follow-up review, so nothing rewrites the spec after the repair lands.
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"  # what synthesize_result would carry
        return result

    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    text = spec_path(project, "1-1-a").read_text()
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert devcontract.ORCHESTRATOR_SYNTH_NOTE in text
    (repaired,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert repaired["status"] == "done"
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]


def test_marker_repair_failure_is_best_effort(project, monkeypatch):
    """An OSError out of the append is swallowed (journaled `spec-marker-repair-
    failed`): the result was already synthesized, so the story still completes —
    only the on-disk spec is left non-compliant."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("bmad_loop.devcontract.append_auto_run_result", boom)
    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    (failed,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-failed"]
    assert "OSError" in failed["error"]


def test_synthesized_review_result_repairs_marker_and_story_completes(project):
    """#276 M3, review path: when the fallback synthesizes a REVIEW result the engine
    appends the marker onto the on-disk spec (with provenance) and the story still
    completes. Prior engine coverage only exercised a synthesized DEV result with no
    follow-up review — this covers the primary review-repair path."""
    from bmad_loop import devcontract

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    dev = dev_effect(project, "1-1-a", followup_review=True)
    review = review_effect(project, "1-1-a", clean=True)

    def synthesized_review(spec):
        result = review(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    engine, _ = make_engine(project, [dev, synthesized_review])
    summary = engine.run()

    assert summary.done == 1
    text = spec_path(project, "1-1-a").read_text()
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert devcontract.ORCHESTRATOR_SYNTH_NOTE in text
    (repaired,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert repaired["status"] == "done"
    synth = [
        e for e in engine.journal.entries() if e["kind"] == "session-synthesized-from-frontmatter"
    ]
    assert [e["role"] for e in synth] == ["review"]  # the REVIEW session synthesized


def test_synthesized_review_repair_survives_followup_rewrite(project):
    """The finding's core case: a synthesized review cycle repairs the marker, then a
    subsequent clean review cycle rewrites the whole spec (dropping the marker). The
    story still converges to done, the run does not livelock, and exactly one repair
    fired (the synthesized cycle) — the repair never fights the follow-up rewrite."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    dev = dev_effect(project, "1-1-a", followup_review=True)
    review1 = review_effect(project, "1-1-a", clean=False)  # recommends another review

    def synthesized_review1(spec):
        result = review1(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    review2 = review_effect(project, "1-1-a", clean=True)  # converges; rewrites the spec
    engine, _ = make_engine(project, [dev, synthesized_review1, review2])
    summary = engine.run()

    assert summary.done == 1
    repaired = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repaired"]
    assert len(repaired) == 1  # only the synthesized cycle repaired


def test_marker_repair_undecodable_spec_is_best_effort(project, monkeypatch):
    """`append_auto_run_result` reads raw bytes and raises `UnicodeDecodeError`
    (a `ValueError`, not an `OSError`) on an undecodable spec — a spec torn mid-
    write through a multi-byte UTF-8 sequence between the frontmatter read and the
    append. The best-effort repair must swallow it too (journaled
    `spec-marker-repair-failed`) so the run still completes rather than crashing."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = dev_effect(project, "1-1-a", followup_review=False)

    def synthesized_dev(spec):
        result = inner(spec)
        result.result_json["synthesized_from_frontmatter"] = True
        result.result_json["status"] = "done"
        return result

    def boom(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("bmad_loop.devcontract.append_auto_run_result", boom)
    engine, _ = make_engine(project, [synthesized_dev])
    summary = engine.run()

    assert summary.done == 1
    (failed,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-failed"]
    assert "UnicodeDecodeError" in failed["error"]


def test_marker_repair_skips_out_of_tree_spec(project, tmp_path):
    """A session-reported spec_file outside the orchestrator-owned roots is never
    written — the repair skips with reason `out-of-tree`, like the reconcile."""
    engine, _ = make_engine(project, [])
    outside = tmp_path / "outside-spec.md"  # sibling of the sandbox root, not under it
    outside.write_text("---\nstatus: done\n---\n\nbody\n", encoding="utf-8")
    task = StoryTask(story_key="1-1-a", epic=1)

    engine._repair_spec_marker(task, {"spec_file": str(outside), "status": "done"})

    (skipped,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]
    assert skipped["reason"] == "out-of-tree"
    assert "## Auto Run Result" not in outside.read_text()


def test_marker_repair_skips_on_fm_mismatch(project):
    """A fresh frontmatter read that no longer agrees with the synthesized status
    (here still `in-progress` while `rj` claims `done`) is refused with reason
    `fm-mismatch` and no marker is appended — never author an inconsistent one."""
    engine, _ = make_engine(project, [])
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-progress", "abc123")
    task = StoryTask(story_key="1-1-a", epic=1, spec_file=str(sp))

    engine._repair_spec_marker(task, {"spec_file": str(sp), "status": "done"})

    (skipped,) = [e for e in engine.journal.entries() if e["kind"] == "spec-marker-repair-skipped"]
    assert skipped["reason"] == "fm-mismatch"
    assert "## Auto Run Result" not in sp.read_text()


# ------------------------------------ dev-primitive name resolution (BMAD-METHOD #2651)
# Upstream renamed the dev primitive `bmad-dev-auto` → `bmad-build-auto`, leaving a
# forwarding shim behind. The orchestrator therefore spells the invoked name from what
# is actually on disk (Engine._dev_skill) instead of hardcoding it, and must keep
# working against BOTH eras. The rest of this file's ~49 `/bmad-dev-auto` assertions
# pin the profile-less fallback (see the no-profile test below), not the resolution.


def _prompt_task(**kw) -> StoryTask:
    return StoryTask(story_key="1-1-a", epic=1, **kw)


def test_dev_prompts_spell_the_post_rename_primitive(project):
    """Every generic-dev leg (fresh, known spec, restore, repair) invokes the name
    resolved from the dev adapter's skill tree — here post-rename bmad-build-auto."""
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills")
    engine, adapter = make_engine(project, [])
    attach_profile(adapter)

    # The engine-injected awaiting-operator contract (#335) rides on the tail of
    # every dev prompt, so the invocation is asserted as the head plus the
    # explicit absence of the legacy spelling anywhere in the string.
    fresh = engine._generic_dev_prompt(_prompt_task(), None)
    assert fresh.startswith("/bmad-build-auto 1-1-a")
    assert "bmad-dev-auto" not in fresh

    spec = str(spec_path(project, "1-1-a"))
    explicit = engine._generic_dev_prompt(
        _prompt_task(spec_file=spec, dispatched_spec_file=spec), None
    )
    assert explicit.startswith(
        "/bmad-build-auto Resume the autonomous dev session on the ready-for-dev spec"
    )

    restore = engine._generic_dev_prompt(
        _prompt_task(spec_file=spec, restore_patch="/run/attempt.patch"), None
    )
    assert restore.startswith("/bmad-build-auto Resume review of the in-review spec")

    feedback = project.implementation_artifacts / "feedback.md"
    repair = engine._generic_dev_prompt(_prompt_task(), feedback)
    assert repair.startswith("/bmad-build-auto Resume the autonomous dev session")


def test_dev_prompt_falls_back_to_the_legacy_name_without_a_profile(project):
    """The no-profile shape (test fakes, and any adapter that carries no skill
    tree) resolves to the pre-rename name. Pinned rather than incidental: it is
    what keeps the rest of this suite — and a pre-rename target project whose
    resolution fails open — dispatching a name that exists."""
    from conftest import install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills")  # present but unreachable
    engine, adapter = make_engine(project, [])
    assert getattr(adapter, "profile", None) is None

    assert engine._generic_dev_prompt(_prompt_task(), None).startswith("/bmad-dev-auto 1-1-a")
    # the None tree IS the fallback path, not an unresolved tree that happened to miss
    assert engine._dev_skill_cache == {(project.project, None): "bmad-dev-auto"}


def test_review_prompt_resolves_through_the_review_adapters_own_tree(project):
    """A run can mix skill trees (dev=claude → .claude/skills, review=gemini →
    .agents/skills) and the two trees can sit on different upstream eras. Each
    prompt must spell the primitive ITS adapter would actually find, so the
    per-role lookup and the per-tree memo are both load-bearing."""
    from conftest import attach_profile, install_build_auto_skill, install_dev_base_skills

    install_build_auto_skill(project.project, ".claude/skills")
    install_dev_base_skills(project.project, ".agents/skills", folder_id=False)
    review = attach_profile(MockAdapter([]), "gemini")
    engine, dev = make_engine(project, [], review_adapter=review)
    attach_profile(dev, "claude")
    spec = str(spec_path(project, "1-1-a"))

    assert engine._generic_dev_prompt(_prompt_task(), None).startswith("/bmad-build-auto 1-1-a")
    assert engine._review_prompt(_prompt_task(spec_file=spec)).startswith(
        f"/bmad-dev-auto {spec} —"
    )
    # each tree resolved independently — one memo entry per (workspace, tree), not
    # one per run
    assert engine._dev_skill_cache == {
        (project.project, ".claude/skills"): "bmad-build-auto",
        (project.project, ".agents/skills"): "bmad-dev-auto",
    }


# ------------------------- frontmatter `deferred:` harvest (BMAD-METHOD #2640)

HARVEST_A = {
    "summary": "Retry loop has no ceiling",
    "evidence": "the backoff doubles forever: no cap",
    "location": "src/retry.py:88",
    "severity": "medium",
}
HARVEST_B = {
    "summary": "Timeout is not configurable",
    "evidence": "hardcoded 30s",
    "location": "src/net.py:12",
    "severity": "low",
}


def _harvest_policy(*, review: bool = False, attempts: int = 3) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=review, trigger="always"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        limits=LimitsPolicy(max_dev_attempts=attempts),
        scm=ScmPolicy(rollback_on_failure=True),
    )


def _harvest_entries(project):
    text = (
        project.deferred_work.read_text(encoding="utf-8") if project.deferred_work.is_file() else ""
    )
    return deferredwork.parse_ledger(text)


# A well-formed sha that is not any commit in the sandbox repo. The dev artifact
# gate rejects it non-fixably after the completed session has been harvested,
# which drives the snapshot restore path rather than the fixable-feedback path.
LYING_BASELINE = "deadbeef" * 5


def _baseline_liar_effect(project, story_key: str = "1-1-a", *, deferred=None):
    def effect(_spec):
        source = project.project / "src.txt"
        source.write_text(source.read_text() + f"change for {story_key}\n")
        sp = spec_path(project, story_key)
        write_spec(sp, "done", LYING_BASELINE, deferred=deferred)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": LYING_BASELINE,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [],
                "escalations": [],
                "followup_review_recommended": False,
            },
        )

    return effect


def _gitignore_harvest_ledger(project) -> str:
    """Ignore and commit the ledger rule without losing the template rules."""
    rel = project.deferred_work.relative_to(project.project).as_posix()
    gitignore = project.project / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + rel + "\n", encoding="utf-8")
    git(project.project, "add", ".gitignore")
    git(project.project, "commit", "-q", "-m", "ignore deferred-work ledger")
    assert git(project.project, "check-ignore", rel).strip() == rel
    return rel


def _crash_after_harvest(engine) -> None:
    """Crash after the ledger write but before the attempt decision acts.

    `post_dev_verify` names TWO points in the loop — the dev leg's emit and the
    repair leg's — so an unqualified raise would fire inside `_fix_phase` too,
    for any caller whose scenario reaches a review->fix route. The dev emit is
    always the first of the two (a repair leg runs only after a dev leg
    PROCEEDed, and emitted), so latching on the first one pins the crash to the
    dev attempt this helper is named for rather than to whichever emit the
    scenario happens to reach.
    """
    original_emit = engine._emit
    crashed = False

    def crashing_emit(stage, *args, **kwargs):
        nonlocal crashed
        if stage == "post_dev_verify" and not crashed:
            crashed = True
            raise RuntimeError("host died after harvest")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crashing_emit


def _pause_harvest_policy(*, attempts: int = 2) -> Policy:
    return dataclasses.replace(
        _harvest_policy(attempts=attempts),
        scm=ScmPolicy(rollback_on_failure=False),
    )


def _run_harvest_to_pause(project, *, attempts: int = 2):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=_pause_harvest_policy(attempts=attempts),
    )
    assert engine.run().paused
    return engine


def _fixable_harvest_chain_policy(marker: Path, *, attempts: int = 3) -> Policy:
    return dataclasses.replace(
        _harvest_policy(attempts=attempts),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )


def _fail_nth_harvest(engine, monkeypatch, nth: int) -> None:
    """Inject the retry outcome produced by a one-shot harvest read fault."""
    real_harvest = engine._harvest_spec_deferrals
    calls = 0

    def fail_once(task, result_json):
        nonlocal calls
        calls += 1
        if calls == nth:
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals (PermissionError: transient)"
            )
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", fail_once)


def _review_with_deferrals(project, findings):
    def effect(_spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "done", _spec_baseline(sp), deferred=findings)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "status": "done",
                "followup_review_recommended": False,
                "escalations": [],
            },
        )

    return effect


def test_review_prompt_does_not_ask_the_session_to_double_file_deferrals(project):
    engine, _ = make_engine(project, [])
    spec = str(project.implementation_artifacts / "spec-1-1-a.md")

    prompt = engine._review_prompt(_prompt_task(spec_file=spec))

    assert prompt.startswith(f"/bmad-dev-auto {spec} —")
    assert "append" not in prompt.lower()
    assert "do NOT modify, re-open, or rewrite existing deferred-work ledger" in prompt
    assert "the orchestrator owns their status and resolution" in prompt


def test_spec_frontmatter_deferrals_harvested_into_ledger(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )

    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    (entry,) = _harvest_entries(project)
    assert entry.title == HARVEST_A["summary"] and entry.open
    assert re.search(r"^origin: spec-deferred [0-9a-f]{12}$", entry.body, re.M)
    assert "location: src/retry.py:88\nsource_spec: `spec-1-1-a.md`" in entry.body
    assert "severity: medium" in entry.body
    assert "reason: the backoff doubles forever: no cap" in entry.body
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(events) == 1
    assert events[0]["dw_ids"] == [entry.id]
    assert events[0]["deduped"] == 0 and events[0]["malformed"] == 0


def test_spec_deferrals_use_na_when_no_location_was_recorded(project):
    finding = {"summary": "No location", "evidence": "the report omitted it"}
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[finding])],
        policy=_harvest_policy(),
    )

    assert engine.run().done == 1

    (entry,) = _harvest_entries(project)
    assert "location: n/a\nsource_spec: `spec-1-1-a.md`" in entry.body


def test_spec_deferrals_absent_field_writes_no_ledger(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=_harvest_policy(),
    )

    assert engine.run().done == 1
    assert not project.deferred_work.exists()
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]


def test_spec_deferrals_skip_out_of_tree_session_spec(project, tmp_path):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    outside = tmp_path / "outside-spec.md"
    write_spec(outside, "done", "abc123", deferred=[HARVEST_A])

    engine._harvest_spec_deferrals(task, {"spec_file": str(outside)})

    assert not project.deferred_work.exists()
    assert task.harvested_deferrals == []
    assert task.harvest_wrote_ledger is False
    skipped = [
        e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-skipped-out-of-tree"
    ]
    assert len(skipped) == 1
    assert skipped[0]["story_key"] == task.story_key
    assert skipped[0]["spec"] == str(outside)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_spec_deferrals_skip_when_containment_probe_faults(project, monkeypatch, error_type):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    sp = spec_path(project, task.story_key)
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])

    def containment_fault(*args, **kwargs):
        raise error_type("injected containment fault")

    monkeypatch.setattr(verify, "spec_within_roots", containment_fault)
    engine._harvest_spec_deferrals(task, {"spec_file": str(sp)})

    assert not project.deferred_work.exists()
    assert task.harvested_deferrals == []
    assert task.harvest_wrote_ledger is False
    skipped = [
        e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-skipped-out-of-tree"
    ]
    assert len(skipped) == 1 and skipped[0]["spec"] == str(sp)


def test_spec_deferrals_harvest_across_the_disk_resolved_skill_rename(project):
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills")
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )
    attach_profile(adapter, "claude")

    assert engine.run().done == 1

    assert adapter.sessions[0].prompt.startswith("/bmad-build-auto 1-1-a")
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]


def test_spec_deferrals_require_the_generic_dev_seam(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    monkeypatch.setattr(engine, "_generic_dev", lambda: False)
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])

    engine._harvest_spec_deferrals(StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)})

    assert not project.deferred_work.exists()


def test_spec_deferrals_plain_replay_does_not_double_append_or_mutate_frontmatter(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )
    assert engine.run().done == 1
    task = engine.state.tasks["1-1-a"]
    sp = Path(task.spec_file)
    before = sp.read_bytes()

    engine._harvest_spec_deferrals(task, task.sessions[0].result_json)

    assert sp.read_bytes() == before
    assert len(_harvest_entries(project)) == 1
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [e["deduped"] for e in events] == [0, 1]
    assert events[-1]["dw_ids"] == []
    assert len(task.harvested_deferrals) == 1
    record = task.harvested_deferrals[0]
    assert re.fullmatch(r"spec-deferred [0-9a-f]{12}", record["origin"])
    assert {key: value for key, value in record.items() if key != "origin"} == {
        "title": HARVEST_A["summary"],
        "reason": HARVEST_A["evidence"],
        "location": HARVEST_A["location"],
        "severity": HARVEST_A["severity"],
        "source_spec": "spec-1-1-a.md",
    }


def test_spec_deferral_provenance_is_durable_before_append_for_crash_replay(project, monkeypatch):
    """A host loss after append must not persist an authorless engine write."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    engine.state.tasks[task.story_key] = task
    sp = spec_path(project, task.story_key)
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    result_json = {"spec_file": str(sp)}
    real_append = deferredwork.append_entry

    class PowerLoss(BaseException):
        pass

    def append_then_die(*args, **kwargs):
        real_append(*args, **kwargs)
        raise PowerLoss

    monkeypatch.setattr(deferredwork, "append_entry", append_then_die)
    with pytest.raises(PowerLoss):
        engine._harvest_spec_deferrals(task, result_json)

    assert project.deferred_work.is_file()
    saved = load_state(engine.run_dir)
    saved_task = saved.tasks[task.story_key]
    assert saved_task.harvest_wrote_ledger is True

    monkeypatch.setattr(deferredwork, "append_entry", real_append)
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=saved,
    )
    resumed._harvest_spec_deferrals(saved_task, result_json)

    assert len(_harvest_entries(project)) == 1
    rel = project.deferred_work.relative_to(project.project).as_posix()
    assert resumed._harvest_gate_exclude(saved_task) == (rel,)


def test_expanded_harvest_records_are_durable_before_a_later_append(project, monkeypatch):
    """A prior ledger-write latch cannot suppress the stable-union checkpoint."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    engine.state.tasks[task.story_key] = task
    sp = spec_path(project, task.story_key)
    sp.parent.mkdir(parents=True, exist_ok=True)
    result_json = {"spec_file": str(sp)}
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    engine._harvest_spec_deferrals(task, result_json)
    assert task.harvest_wrote_ledger is True

    write_spec(sp, "done", "abc123", deferred=[HARVEST_A, HARVEST_B])
    real_append = deferredwork.append_entry

    class PowerLoss(BaseException):
        pass

    def append_then_die(*args, **kwargs):
        real_append(*args, **kwargs)
        raise PowerLoss

    monkeypatch.setattr(deferredwork, "append_entry", append_then_die)
    with pytest.raises(PowerLoss):
        engine._harvest_spec_deferrals(task, result_json)

    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]
    saved_task = load_state(engine.run_dir).tasks[task.story_key]
    assert [record["title"] for record in saved_task.harvested_deferrals] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]


def test_spec_deferrals_dedup_sees_already_done_entries(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )
    assert engine.run().done == 1
    task = engine.state.tasks["1-1-a"]
    (entry,) = _harvest_entries(project)
    assert deferredwork.mark_done(project.deferred_work, entry.id, "2026-08-03", "fixed")

    engine._harvest_spec_deferrals(task, task.sessions[0].result_json)

    entries = _harvest_entries(project)
    assert len(entries) == 1
    assert entries[0].status.startswith("done")


def test_spec_deferrals_not_harvested_when_spec_is_short_of_success(project):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "in-progress", "abc123", deferred=[HARVEST_A])

    engine._harvest_spec_deferrals(StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)})

    assert not project.deferred_work.exists()
    assert not [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]


def test_spec_deferrals_reconcile_same_result_after_observation_fault(project, monkeypatch):
    """A recovered strict harvest read finishes the same session's reconcile.

    Otherwise a transient failure in the preceding observation leaves the spec at
    ``in-progress``; harvest mistakes that for a legitimate no-op, and a later
    attempt can replace the completed session's unfiled findings.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                final_status="in-progress",
                followup_review=False,
                prose_status="done",
                deferred=[HARVEST_A],
            )
        ],
        policy=_harvest_policy(attempts=2),
    )
    observed = engine._observed_frontmatter
    failed = False

    def fail_first_reconcile(path, story_key, site):
        nonlocal failed
        if site == "reconcile" and not failed:
            failed = True
            engine._journal_spec_read_failed(
                path, story_key, site, PermissionError(13, "transient")
            )
            return None
        return observed(path, story_key, site)

    monkeypatch.setattr(engine, "_observed_frontmatter", fail_first_reconcile)

    summary = engine.run()

    sp = spec_path(project, "1-1-a")
    assert failed and summary.done == 1 and not summary.deferred and not summary.crashed
    assert [session.role for session in adapter.sessions] == ["dev"]
    assert verify.status_of(verify.read_frontmatter(sp)) == "done"
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    failures = [event for event in engine.journal.entries() if event["kind"] == "spec-read-failed"]
    assert len(failures) == 1 and failures[0]["site"] == "reconcile"


def test_spec_deferrals_retry_unproven_reconcilable_status(project):
    """A nonterminal spec with findings is never a successful harvest no-op."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    sp = spec_path(project, task.story_key)
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "in-review", "abc123", deferred=[HARVEST_A])

    outcome = engine._harvest_spec_deferrals(task, {"spec_file": str(sp)})

    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "reconcilable nonterminal status 'in-review'" in outcome.reason
    assert not project.deferred_work.exists()
    assert task.harvested_deferrals == []


def test_review_pass_deferrals_harvested_and_deduped_across_both_sites(project):
    review_with_b = _review_with_deferrals(project, [HARVEST_A, HARVEST_B])

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a", deferred=[HARVEST_A]), review_with_b],
        policy=_harvest_policy(review=True),
    )

    assert engine.run().done == 1

    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [e["dw_ids"] for e in events] == [["DW-1"], ["DW-2"]]
    assert events[-1]["deduped"] == 1


def test_ledger_digest_collapses_absent_and_empty_only():
    assert _digest_of(None) == _digest_of("")
    assert _digest_of("# Deferred Work\n") != _digest_of(None)


def test_persisted_ledger_restore_is_gated_by_capture_flag(project):
    """None text is active only with the independent captured flag."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("operator-owned\n", encoding="utf-8")

    task.pre_harvest_ledger = None
    task.pre_harvest_ledger_captured = False
    engine._restore_persisted_ledger(task, replayed=False)
    assert project.deferred_work.read_text(encoding="utf-8") == "operator-owned\n"

    task.pre_harvest_ledger_captured = True
    engine._restore_persisted_ledger(task, replayed=False)
    assert not project.deferred_work.exists()
    # Restore never prunes the harmless orchestrator-owned parent.
    assert project.deferred_work.parent.is_dir()


def test_unarmed_replay_journals_missing_snapshot_without_touching_ledger(project):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("operator-owned\n", encoding="utf-8")

    engine._restore_persisted_ledger(task, replayed=True)

    assert project.deferred_work.read_text(encoding="utf-8") == "operator-owned\n"
    assert "ledger-snapshot-missing" in [e["kind"] for e in engine.journal.entries()]


def test_ledger_classifier_reports_external_as_not_gits(project, tmp_path):
    outside = dataclasses.replace(
        project,
        implementation_artifacts=tmp_path / "shared" / "implementation-artifacts",
    )
    outside.implementation_artifacts.mkdir(parents=True)
    engine, _ = make_engine(outside, [], policy=_harvest_policy())

    assert engine._ledger_is_gits_to_restore(StoryTask(story_key="1-1-a", epic=1)) is False


def test_ledger_classifier_reports_tracked_inside_workspace_as_gits(project):
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "track deferred-work")
    engine, _ = make_engine(project, [], policy=_harvest_policy())

    assert engine._ledger_is_gits_to_restore(StoryTask(story_key="1-1-a", epic=1)) is True


@pytest.mark.skipif(
    sys.platform == "win32", reason="creating a directory symlink needs elevation on Windows"
)
def test_ledger_classifier_retries_lexical_outside_with_resolved_paths(project, tmp_path):
    from bmad_loop.workspace import Workspace

    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "track deferred-work")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(project.project, target_is_directory=True)
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    engine.workspace = Workspace(root=linked_root, paths=project)
    with pytest.raises(ValueError):
        project.deferred_work.relative_to(linked_root)

    assert engine._ledger_is_gits_to_restore(StoryTask(story_key="1-1-a", epic=1)) is True


def test_ledger_tracked_probe_failure_keeps_file_and_journals(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("uncertain ownership\n", encoding="utf-8")

    def fail_probe(*args, **kwargs):
        raise GitError("injected tracked probe failure")

    monkeypatch.setattr(verify, "path_tracked", fail_probe)
    engine._restore_ledger(task, None)

    assert project.deferred_work.read_text(encoding="utf-8") == "uncertain ownership\n"
    (event,) = [e for e in engine.journal.entries() if e["kind"] == "ledger-tracked-probe-failed"]
    assert event["story_key"] == task.story_key


def test_ledger_scope_probe_failure_keeps_file_and_journals(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("uncertain scope\n", encoding="utf-8")
    ledger = project.deferred_work
    real_relative_to = Path.relative_to
    real_resolve = Path.resolve

    def outside_relative_to(self, *other, **kwargs):
        if self == ledger:
            raise ValueError("injected lexical outside")
        return real_relative_to(self, *other, **kwargs)

    def failed_resolve(self, *args, **kwargs):
        if self == ledger:
            raise OSError("injected scope probe failure")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "relative_to", outside_relative_to)
    monkeypatch.setattr(Path, "resolve", failed_resolve)
    engine._restore_ledger(task, None)

    assert project.deferred_work.read_text(encoding="utf-8") == "uncertain scope\n"
    (event,) = [e for e in engine.journal.entries() if e["kind"] == "ledger-scope-probe-failed"]
    assert event["story_key"] == task.story_key


def test_pre_harvest_ledger_restore_is_atomic_on_publication_failure(project, monkeypatch):
    """A failed rollback publish leaves the current ledger byte-intact."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    task = StoryTask(story_key="1-1-a", epic=1)
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    current = "# Deferred Work\n\ncurrent harvested row\n"
    project.deferred_work.write_text(current, encoding="utf-8")

    def publication_fails(*args, **kwargs):
        raise OSError("atomic replace blocked")

    monkeypatch.setattr(platform_util, "atomic_replace", publication_fails)

    with pytest.raises(OSError, match="atomic replace blocked"):
        engine._restore_ledger(task, "# Deferred Work\n\npre-harvest snapshot\n")

    assert project.deferred_work.read_text(encoding="utf-8") == current
    assert list(project.deferred_work.parent.glob("deferred-work.md.*.tmp")) == []


def test_nonfixable_retry_leaves_tracked_ledger_at_its_baseline_bytes(project):
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    before = "# Deferred Work\n\ntracked baseline\n"
    project.deferred_work.write_text(before, encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "track deferred-work")
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _baseline_liar_effect(project, deferred=[HARVEST_A]),
            dev_effect(project, "1-1-a", followup_review=False),
        ],
        policy=_harvest_policy(attempts=2),
    )

    assert engine.run().done == 1

    assert project.deferred_work.read_text(encoding="utf-8") == before
    assert HARVEST_A["summary"] not in before
    persisted = load_state(engine.run_dir).tasks["1-1-a"]
    assert persisted.pre_harvest_ledger_captured is False
    assert persisted.pre_harvest_ledger is None


@pytest.mark.parametrize(
    "ledger_state",
    [
        "created-untracked",
        "existing-untracked",
        "created-ignored",
        "existing-ignored",
        "tracked-deleted",
    ],
)
def test_crash_replay_restores_the_rejected_attempts_ledger_state(project, ledger_state):
    """Snapshots cover Git-blind states; reset owns the tracked-deleted row."""
    before: str | None = None
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    if "ignored" in ledger_state:
        _gitignore_harvest_ledger(project)
    if ledger_state in ("existing-untracked", "existing-ignored"):
        project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
        before = "# Deferred Work\n\noperator-owned bytes\n"
        project.deferred_work.write_text(before, encoding="utf-8")
    elif ledger_state == "tracked-deleted":
        project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
        before = "# Deferred Work\n\ncommitted bytes\n"
        project.deferred_work.write_text(before, encoding="utf-8")
        git(project.project, "add", "-A")
        git(project.project, "commit", "-q", "-m", "track then delete deferred-work")
        project.deferred_work.unlink()
        assert verify.path_tracked(project.project, ledger_rel)

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(attempts=2),
    )
    _crash_after_harvest(engine)
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.pre_harvest_ledger_captured is True
    assert HARVEST_A["summary"] in project.deferred_work.read_text(encoding="utf-8")
    if "ignored" in ledger_state:
        assert ledger_rel not in verify.untracked_files(project.project)
        assert not verify.path_tracked(project.project, ledger_rel)

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=_harvest_policy(attempts=2),
    )
    assert resumed.run().done == 1

    if before is None:
        assert not project.deferred_work.exists()
    else:
        assert project.deferred_work.read_text(encoding="utf-8") == before
    assert "resume-verify" in [e["kind"] for e in resumed.journal.entries()]


def test_nonfixable_retry_reverts_harvest_from_external_ledger(project, tmp_path):
    paths = dataclasses.replace(
        project,
        implementation_artifacts=tmp_path / "shared" / "implementation-artifacts",
    )
    paths.implementation_artifacts.mkdir(parents=True)
    write_sprint(paths, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        paths,
        [
            _baseline_liar_effect(paths, deferred=[HARVEST_A]),
            dev_effect(paths, "1-1-a", followup_review=False),
        ],
        policy=_harvest_policy(attempts=2),
    )

    assert engine.run().done == 1
    assert not paths.deferred_work.exists()


def test_nonfixable_retry_reverts_harvest_before_the_next_attempt(project, monkeypatch):
    """A rejected attempt's finding is removed and the retry files it afresh."""
    ledger_present_at_retry: list[bool] = []
    ledger_present_after_reset: list[bool] = []

    def retry_with_the_same_finding(spec):
        ledger_present_at_retry.append(project.deferred_work.exists())
        return dev_effect(
            project,
            "1-1-a",
            followup_review=False,
            deferred=[HARVEST_A],
        )(spec)

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
            retry_with_the_same_finding,
        ],
        policy=_harvest_policy(attempts=2),
    )
    real_restore = engine._restore_persisted_ledger

    def probing_restore(task, *, replayed):
        # `_safe_reset`'s protected keep roots—not reset-hard—leave an ordinary
        # untracked artifacts ledger standing until the explicit restore unlinks
        # it. Patching RecoveryFlow.protected_relpaths to () makes this False.
        ledger_present_after_reset.append(project.deferred_work.exists())
        return real_restore(task, replayed=replayed)

    monkeypatch.setattr(engine, "_restore_persisted_ledger", probing_restore)

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert "no changes" in decisions[0]["reason"]
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [event["dw_ids"] for event in harvests] == [["DW-1"], ["DW-1"]]
    assert ledger_present_after_reset == [True]
    assert ledger_present_at_retry == [False]
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    assert len(adapter.sessions) == 2
    assert summary.done == 1 and not summary.crashed and not summary.paused


def test_nonfixable_retry_resets_harvest_records_to_the_fresh_attempt(project):
    """A rolled-back finding cannot survive in the next attempt's carry set."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[HARVEST_B],
            ),
        ],
        policy=_harvest_policy(attempts=2),
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert [item["title"] for item in task.harvested_deferrals] == [HARVEST_B["summary"]]
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_B["summary"]]


def test_pre_harvest_snapshot_is_on_disk_before_harvest(project, monkeypatch):
    """Probe the arm's own save before run()'s ambient finally can mask it."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )
    seen: list[tuple[bool, str | None]] = []
    real_harvest = engine._harvest_spec_deferrals

    def probing_harvest(task, result_json):
        persisted = load_state(engine.run_dir).tasks[task.story_key]
        seen.append((persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger))
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", probing_harvest)
    assert engine.run().done == 1
    assert seen[0] == (True, None)


def test_proceed_disarm_is_on_disk_before_review_handoff(project, monkeypatch):
    """Probe PROCEED's explicit save before any later commit/finally save."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("operator-owned snapshot bytes\n", encoding="utf-8")
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False)],
        policy=_harvest_policy(),
    )
    seen: list[tuple[bool, str | None]] = []
    real_emit = engine._emit

    def probing_emit(stage, task=None, **fields):
        if stage == "post_dev_phase" and task is not None:
            persisted = load_state(engine.run_dir).tasks[task.story_key]
            seen.append((persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger))
        return real_emit(stage, task, **fields)

    monkeypatch.setattr(engine, "_emit", probing_emit)
    assert engine.run().done == 1
    assert seen == [(False, None)]


def test_dev_defer_keeps_harvest_and_disarms_snapshot(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(attempts=1),
    )

    assert engine.run().deferred == 1

    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    persisted = load_state(engine.run_dir).tasks["1-1-a"]
    assert persisted.phase == Phase.DEFERRED
    assert (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger) == (False, None)


def test_defer_ledger_restore_write_failure_propagates_and_keeps_the_ledger(project, monkeypatch):
    """#328. The post-rollback ledger restore inside `_defer` is a repair write:
    it must raise rather than degrade, and a failed attempt must never be the
    thing that empties the ledger it exists to put back. Under a bare
    `Path.write_text` the truncate lands before the failure does, so the run
    crashed AND the ledger it was restoring went to zero bytes.

    The patch is module-wide but lands on exactly one call — probed on this
    harness, `_defer`'s restore is the only `engine.atomic_write_text` this path
    reaches; the pre-harvest restore and the deferred-close rollback both need
    state this scenario never builds. Committing the ledger is what makes the
    restore fire at all: `git reset` reverts the harvest's append to a *tracked*
    file, so the snapshot and the bytes on disk then differ."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(attempts=1),
    )

    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr("bmad_loop.engine.atomic_write_text", boom)

    summary = engine.run()

    assert summary.crashed and "disk full" in str(summary.crash_error)
    # the restore never landed, but the ledger is the committed one `git reset`
    # put back — not a zero-byte file the failed write truncated on its way out
    assert project.deferred_work.read_bytes()
    assert _ledger_entries(project)["DW-1"].open


def test_dev_escalation_keeps_harvest_and_disarms_snapshot(project):
    inner = dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])

    def escalating(spec):
        result = inner(spec)
        result.result_json["escalations"] = [
            {"type": "missing-config", "severity": "CRITICAL", "detail": "operator needed"}
        ]
        return result

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [escalating], policy=_harvest_policy())

    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    persisted = load_state(engine.run_dir).tasks["1-1-a"]
    assert persisted.phase == Phase.ESCALATED
    assert (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger) == (False, None)


def test_failed_auto_reset_disarms_snapshot_before_ambient_crash_save(project, monkeypatch):
    """#420: rollback-on plus a reset failure must still spend the snapshot."""
    policy = dataclasses.replace(
        _harvest_policy(attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    assert policy.scm.rollback_on_failure is True
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=policy,
    )

    def failing_reset(*args, **kwargs):
        raise GitError("injected git reset --hard failure")

    monkeypatch.setattr(verify, "safe_rollback", failing_reset)
    seen_before_ambient_save: list[tuple[bool, str | None]] = []
    real_append = engine.journal.append

    def probing_append(kind, **fields):
        if kind == "run-crash":
            persisted = load_state(engine.run_dir).tasks["1-1-a"]
            seen_before_ambient_save.append(
                (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger)
            )
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", probing_append)

    summary = engine.run()

    assert summary.crashed and summary.crash_error.startswith("GitError")
    assert "rollback-auto" in [e["kind"] for e in engine.journal.entries()]
    assert seen_before_ambient_save == [(False, None)]
    assert not project.deferred_work.exists()


@pytest.mark.parametrize(
    ("rollback_on_failure", "terminal_event"),
    [(False, "run-paused"), (True, "run-crash")],
)
def test_restore_failure_preserves_existing_unwind_and_durably_disarms(
    project, monkeypatch, rollback_on_failure, terminal_event
):
    """A secondary restore fault cannot replace a pause or reset failure."""
    policy = dataclasses.replace(
        _harvest_policy(attempts=2),
        scm=ScmPolicy(rollback_on_failure=rollback_on_failure),
    )
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=policy,
    )

    if rollback_on_failure:

        def failing_reset(*args, **kwargs):
            raise GitError("primary reset failure")

        monkeypatch.setattr(verify, "safe_rollback", failing_reset)

    def failing_restore(*args, **kwargs):
        raise OSError("secondary ledger restore failure")

    monkeypatch.setattr(engine, "_restore_persisted_ledger", failing_restore)
    seen_before_ambient_save: list[tuple[bool, str | None]] = []
    real_append = engine.journal.append

    def probing_append(kind, **fields):
        if kind == terminal_event:
            persisted = load_state(engine.run_dir).tasks["1-1-a"]
            seen_before_ambient_save.append(
                (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger)
            )
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", probing_append)

    summary = engine.run()

    if rollback_on_failure:
        assert summary.crashed and summary.crash_error.startswith("GitError")
    else:
        assert summary.paused and not summary.crashed
    (restore_event,) = [e for e in engine.journal.entries() if e["kind"] == "ledger-restore-failed"]
    assert restore_event["error"] == "OSError: secondary ledger restore failure"
    assert seen_before_ambient_save == [(False, None)]
    assert project.deferred_work.exists()


def test_restore_failure_after_returned_rollback_raises_after_durable_disarm(project, monkeypatch):
    """With no prior unwind, the repair failure stays fail-loud after cleanup."""
    policy = dataclasses.replace(
        _harvest_policy(attempts=2),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=policy,
    )

    def failing_restore(*args, **kwargs):
        raise OSError("sole ledger restore failure")

    monkeypatch.setattr(engine, "_restore_persisted_ledger", failing_restore)
    seen_before_ambient_save: list[tuple[bool, str | None]] = []
    real_append = engine.journal.append

    def probing_append(kind, **fields):
        if kind == "run-crash":
            persisted = load_state(engine.run_dir).tasks["1-1-a"]
            seen_before_ambient_save.append(
                (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger)
            )
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", probing_append)

    summary = engine.run()

    assert summary.crashed and summary.crash_error.startswith("OSError")
    (restore_event,) = [e for e in engine.journal.entries() if e["kind"] == "ledger-restore-failed"]
    assert restore_event["error"] == "OSError: sole ledger restore failure"
    assert seen_before_ambient_save == [(False, None)]


def test_awaiting_operator_resume_spends_snapshot_before_commit_window(project, monkeypatch):
    """Main-only DEV_VERIFY resume bypasses `_dev_phase`'s PROCEED disarm."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project,
                "1-1-a",
                final_status="awaiting-operator",
                operator_actions=ACTIONS,
                deferred=[HARVEST_A],
            )
        ],
        policy=_park_policy(),
    )
    real_disarm = engine._disarm_ledger_snapshot

    def crash_at_proceed_disarm(task):
        if task.pre_harvest_ledger_captured:
            raise RuntimeError("host died before the accepted-attempt disarm")
        return real_disarm(task)

    monkeypatch.setattr(engine, "_disarm_ledger_snapshot", crash_at_proceed_disarm)
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DEV_VERIFY and crashed.spec_file
    assert crashed.pre_harvest_ledger_captured is True

    resumed, _ = resume_engine(project, engine, [], policy=_park_policy())
    seen_before_commit: list[tuple[bool, str | None]] = []
    real_skip = resumed._skip_review_and_commit

    def probing_skip(task, **kwargs):
        persisted = load_state(resumed.run_dir).tasks[task.story_key]
        seen_before_commit.append(
            (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger)
        )
        return real_skip(task, **kwargs)

    monkeypatch.setattr(resumed, "_skip_review_and_commit", probing_skip)

    assert resumed.run().awaiting_operator == 1
    assert seen_before_commit == [(False, None)]
    persisted = load_state(resumed.run_dir).tasks["1-1-a"]
    assert persisted.phase == Phase.AWAITING_OPERATOR


@pytest.mark.parametrize(
    "ledger_state",
    ["created-during-pause", "existing-untracked", "existing-tracked", "existing-ignored"],
)
def test_operator_ledger_edits_survive_pause_and_resume(project, ledger_state):
    before = ""
    if ledger_state == "existing-ignored":
        _gitignore_harvest_ledger(project)
    if ledger_state != "created-during-pause":
        project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
        before = f"# Deferred Work\n\n{ledger_state}\n"
        project.deferred_work.write_text(before, encoding="utf-8")
    if ledger_state == "existing-tracked":
        git(project.project, "add", "-A")
        git(project.project, "commit", "-q", "-m", "track deferred-work")
        rel = project.deferred_work.relative_to(project.project).as_posix()
        assert verify.path_tracked(project.project, rel)

    engine = _run_harvest_to_pause(project)
    operator_text = before + "\noperator edit while paused\n"
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text(operator_text, encoding="utf-8")
    resumed, _ = resume_engine(project, engine, [], policy=engine.policy)

    assert resumed.run().paused
    assert project.deferred_work.read_text(encoding="utf-8") == operator_text


def test_pause_disarm_is_on_disk_before_run_paused_event(project, monkeypatch):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_baseline_liar_effect(project, deferred=[HARVEST_A])],
        policy=_pause_harvest_policy(),
    )
    seen_before_ambient_save: list[tuple[bool, str | None]] = []
    real_append = engine.journal.append

    def probing_append(kind, **fields):
        if kind == "run-paused":
            persisted = load_state(engine.run_dir).tasks["1-1-a"]
            seen_before_ambient_save.append(
                (persisted.pre_harvest_ledger_captured, persisted.pre_harvest_ledger)
            )
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", probing_append)
    assert engine.run().paused
    assert seen_before_ambient_save == [(False, None)]


def test_fixable_retry_chain_snapshot_reaches_phase_baseline(project, tmp_path):
    marker = tmp_path / "fixed.marker"
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def repairing_liar(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return _baseline_liar_effect(project)(spec)

    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            repairing_liar,
            dev_effect(project, "1-1-a", followup_review=False),
        ],
        policy=_fixable_harvest_chain_policy(marker),
    )

    assert engine.run().done == 1
    actions = [e["action"] for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert actions == ["retry", "retry", "proceed"]
    assert [e["kind"] for e in engine.journal.entries()].count("rollback-auto") == 1
    assert not project.deferred_work.exists()


def test_nonfixable_chain_rollback_rebases_ledger_proof_reference(project, tmp_path):
    marker = tmp_path / "fixed.marker"
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def repairing_liar(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return _baseline_liar_effect(project)(spec)

    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            repairing_liar,
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
        ],
        policy=_fixable_harvest_chain_policy(marker),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert summary.deferred == 1 and summary.done == 0
    assert [d["action"] for d in decisions] == ["retry", "retry", "defer"]
    assert "no changes" in decisions[-1]["reason"]
    harvests = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [event["dw_ids"] for event in harvests] == [["DW-1"], ["DW-1"]]


def test_awaiting_operator_spec_deferrals_are_harvested_before_park(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            generic_dev_effect(
                project,
                "1-1-a",
                final_status="awaiting-operator",
                operator_actions=ACTIONS,
                deferred=[HARVEST_A],
            )
        ],
        policy=_park_policy(),
    )

    summary = engine.run()

    assert summary.awaiting_operator == 1 and summary.done == 0
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    assert engine.state.tasks["1-1-a"].phase == Phase.AWAITING_OPERATOR


def test_review_timeout_salvage_harvests_new_frontmatter_deferrals(project):
    def timeout_with_new_deferral(_spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-review", _spec_baseline(sp), deferred=[HARVEST_A, HARVEST_B])
        return SessionResult(status="timeout")

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            timeout_with_new_deferral,
        ],
        policy=_salvage_policy(),
    )

    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0
    harvested = [
        entry
        for entry in _harvest_entries(project)
        if re.search(r"^origin: spec-deferred [0-9a-f]{12}$", entry.body, re.M)
    ]
    assert [entry.title for entry in harvested] == [HARVEST_A["summary"], HARVEST_B["summary"]]
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [event["deduped"] for event in events] == [0, 1]
    assert events[-1]["dw_ids"] == [harvested[-1].id]


def test_fix_phase_harvests_findings_before_followup_review_replaces_them(project):
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a", deferred=[HARVEST_A])(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix_with_new_deferral(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(
            project,
            "1-1-a",
            final_status="in-progress",
            prose_status="done",
            deferred=[HARVEST_B],
        )(spec)

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = dataclasses.replace(
        _harvest_policy(review=True),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix_with_new_deferral,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert [session.role for session in adapter.sessions] == ["dev", "review", "dev", "review"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert [event["dw_ids"] for event in events] == [["DW-1"], ["DW-2"]]


def test_fix_harvest_read_failure_retries_before_rereview(project, monkeypatch):
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix_with_deferral(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a", deferred=[HARVEST_A, HARVEST_B])(spec)

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = dataclasses.replace(
        _harvest_policy(review=True),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix_with_deferral,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    _fail_nth_harvest(engine, monkeypatch, 3)

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert [session.role for session in adapter.sessions] == ["dev", "review", "dev", "review"]
    decisions = [e for e in engine.journal.entries() if e["kind"] == "fix-decision"]
    assert [decision["ok"] for decision in decisions] == [True]
    failures = [e for e in engine.journal.entries() if e["kind"] == "fix-harvest-failed"]
    assert [event["harvest_attempt"] for event in failures] == [1]
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]


def test_persistent_fix_harvest_failure_defers_without_replacement_session(project, monkeypatch):
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix_with_findings(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a", deferred=[HARVEST_A, HARVEST_B])(spec)

    def replacement_fix(spec):
        marker.write_text("ok\n", encoding="utf-8")
        return dev_effect(project, "1-1-a", deferred=[HARVEST_A])(spec)

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = dataclasses.replace(
        _harvest_policy(review=True),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix_with_findings,
            replacement_fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    real_harvest = engine._harvest_spec_deferrals
    calls = 0

    def fail_fix_pair(task, result_json):
        nonlocal calls
        calls += 1
        if calls in (3, 4):
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals (PermissionError: persistent)"
            )
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", fail_fix_pair)

    summary = engine.run()

    assert summary.deferred == 1 and not summary.done and not summary.crashed
    assert [session.role for session in adapter.sessions] == ["dev", "review", "dev"]
    assert calls == 4
    failures = [e for e in engine.journal.entries() if e["kind"] == "fix-harvest-failed"]
    assert [event["harvest_attempt"] for event in failures] == [1, 2]
    task = engine.state.tasks["1-1-a"]
    assert "harvest remained unreadable after 2 attempts" in task.defer_reason


def test_spec_deferrals_malformed_siblings_file_valid_and_one_aggregated_entry(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[HARVEST_A, "a bare string", {"evidence": "no summary"}],
            )
        ],
        policy=_harvest_policy(),
    )

    assert engine.run().done == 1

    entries = _harvest_entries(project)
    assert len(entries) == 2
    assert entries[0].title == HARVEST_A["summary"]
    meta = entries[1]
    assert meta.title == "Unreadable `deferred:` items in spec-1-1-a.md"
    assert re.search(r"^origin: spec-deferred-malformed [0-9a-f]{12}$", meta.body, re.M)
    assert "location: n/a" in meta.body
    assert "item 2" in meta.body and "item 3" in meta.body
    malformed = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-malformed"]
    assert len(malformed) == 1 and len(malformed[0]["items"]) == 2


def test_spec_deferrals_unreadable_spec_returns_retry_without_harvest(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    fault_read_text(monkeypatch, sp)

    outcome = engine._harvest_spec_deferrals(
        StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)}
    )

    assert not project.deferred_work.exists()
    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "spec unreadable while harvesting deferrals" in outcome.reason
    failures = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(failures) == 1 and failures[0]["site"] == "spec-deferrals"


def test_spec_deferrals_transient_missing_probe_returns_retry_without_harvest(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    real_is_file = Path.is_file
    first_probe = True

    def transient_missing(path, *args, **kwargs):
        nonlocal first_probe
        if path == sp and first_probe:
            first_probe = False
            return False
        return real_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", transient_missing)
    task = StoryTask(story_key="1-1-a", epic=1)

    outcome = engine._harvest_spec_deferrals(task, {"spec_file": str(sp)})

    assert not project.deferred_work.exists()
    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "spec missing while harvesting deferrals" in outcome.reason

    assert engine._harvest_spec_deferrals(task, {"spec_file": str(sp)}) is None
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]


def test_spec_deferrals_transient_inner_missing_probe_returns_retry(project, monkeypatch):
    """The reader's own file probe must not collapse a race to a clean no-op."""
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    real_is_file = Path.is_file
    probes = 0

    def transient_inner_missing(path, *args, **kwargs):
        nonlocal probes
        if path == sp:
            probes += 1
            if probes == 2:
                return False
        return real_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", transient_inner_missing)
    task = StoryTask(story_key="1-1-a", epic=1)

    outcome = engine._harvest_spec_deferrals(task, {"spec_file": str(sp)})

    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "empty or invalid frontmatter" in outcome.reason
    assert not project.deferred_work.exists()

    assert engine._harvest_spec_deferrals(task, {"spec_file": str(sp)}) is None
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]


@pytest.mark.parametrize(
    "broken_frontmatter",
    [
        b"\xff\xfe\x00\x00",
        b"---\nstatus: done\ndeferred: [unterminated\n---\nbody\n",
    ],
    ids=["invalid-utf8", "invalid-yaml"],
)
def test_spec_deferrals_degraded_frontmatter_returns_retry_then_recovers(
    project, broken_frontmatter
):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_bytes(broken_frontmatter)
    task = StoryTask(story_key="1-1-a", epic=1)

    outcome = engine._harvest_spec_deferrals(task, {"spec_file": str(sp)})

    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "empty or invalid frontmatter" in outcome.reason
    assert not project.deferred_work.exists()

    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    assert engine._harvest_spec_deferrals(task, {"spec_file": str(sp)}) is None
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]


def test_spec_deferrals_stat_failure_returns_retry_without_harvest(project, monkeypatch):
    engine, _ = make_engine(project, [], policy=_harvest_policy())
    sp = spec_path(project, "1-1-a")
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123", deferred=[HARVEST_A])
    real_is_file = Path.is_file

    def fault_is_file(path, *args, **kwargs):
        if path == sp:
            raise PermissionError("transient stat fault")
        return real_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", fault_is_file)

    outcome = engine._harvest_spec_deferrals(
        StoryTask(story_key="1-1-a", epic=1), {"spec_file": str(sp)}
    )

    assert not project.deferred_work.exists()
    assert outcome is not None and outcome.retryable and not outcome.fixable
    assert "spec unreadable while harvesting deferrals" in outcome.reason
    failures = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(failures) == 1 and failures[0]["site"] == "spec-deferrals"


def test_dev_harvest_read_failure_retries_before_accepting(project, monkeypatch):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
        ],
        policy=_harvest_policy(attempts=2),
    )
    _fail_nth_harvest(engine, monkeypatch, 1)

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert "spec unreadable while harvesting deferrals" in decisions[0]["reason"]
    assert [session.role for session in adapter.sessions] == ["dev", "dev"]
    assert [entry.title for entry in _harvest_entries(project)] == [HARVEST_A["summary"]]
    assert summary.done == 1 and not summary.crashed and not summary.paused


def test_review_harvest_read_failure_retries_same_result_before_another_session(
    project, monkeypatch
):
    """A deterministic read retry must not let another reviewer replace the source."""
    review_with_findings = _review_with_deferrals(project, [HARVEST_A, HARVEST_B])

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            review_with_findings,
            _review_with_deferrals(project, [HARVEST_A]),
        ],
        policy=_harvest_policy(review=True),
    )
    _fail_nth_harvest(engine, monkeypatch, 2)

    summary = engine.run()

    failures = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"]
    assert len(failures) == 1
    assert "spec unreadable while harvesting deferrals" in failures[0]["reason"]
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]
    assert summary.done == 1 and not summary.crashed and not summary.paused


def test_persistent_review_harvest_read_failure_defers_without_another_session(
    project, monkeypatch
):
    """Persistent repair-input failure terminates without overwriting its spec."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            _review_with_deferrals(project, [HARVEST_A, HARVEST_B]),
        ],
        policy=_harvest_policy(review=True),
    )
    real_harvest = engine._harvest_spec_deferrals
    calls = 0

    def fail_review_harvest(task, result_json):
        nonlocal calls
        calls += 1
        if calls > 1:
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals (PermissionError: persistent)"
            )
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", fail_review_harvest)

    summary = engine.run()

    assert summary.deferred == 1 and not summary.done and not summary.crashed
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert calls == 3  # dev harvest, then the bounded pair of review reads
    failures = [e for e in engine.journal.entries() if e["kind"] == "review-verify-failed"]
    assert len(failures) == 2


def test_persistent_review_harvest_failure_reescalates_resolved_redrive(project, monkeypatch):
    """A resolved CRITICAL re-drive may never be downgraded to a defer."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})

    def resolved_review(spec):
        engine.state.tasks["1-1-a"].resolved_redrive = True
        return _review_with_deferrals(project, [HARVEST_A, HARVEST_B])(spec)

    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            resolved_review,
        ],
        policy=_harvest_policy(review=True),
    )
    real_harvest = engine._harvest_spec_deferrals
    calls = 0

    def fail_review_harvest(task, result_json):
        nonlocal calls
        calls += 1
        if calls > 1:
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals (PermissionError: persistent)"
            )
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", fail_review_harvest)

    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.deferred == 0
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert calls == 3
    saved = load_state(engine.run_dir)
    assert saved.tasks["1-1-a"].phase == Phase.ESCALATED
    assert "re-escalating instead of deferring" in saved.paused_reason


def test_timeout_salvage_harvest_read_failure_falls_back_before_commit(project, monkeypatch):
    def timeout_with_findings(_spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-review", _spec_baseline(sp), deferred=[HARVEST_A, HARVEST_B])
        return SessionResult(status="timeout")

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    replacement_review = _review_with_deferrals(project, [HARVEST_A])
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            timeout_with_findings,
            replacement_review,
        ],
        policy=_salvage_policy(),
    )
    _fail_nth_harvest(engine, monkeypatch, 2)

    summary = engine.run()

    failures = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage-failed"]
    assert len(failures) == 1
    assert "spec unreadable while harvesting deferrals" in failures[0]["reason"]
    assert failures[0]["harvest_attempt"] == 1
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    harvested = [
        entry
        for entry in _harvest_entries(project)
        if re.search(r"^origin: spec-deferred [0-9a-f]{12}$", entry.body, re.M)
    ]
    assert [entry.title for entry in harvested] == [
        HARVEST_A["summary"],
        HARVEST_B["summary"],
    ]
    assert summary.done == 1 and not summary.crashed and not summary.paused


def test_persistent_timeout_salvage_harvest_failure_defers_without_reviewer(project, monkeypatch):
    def timeout_with_findings(_spec):
        sp = spec_path(project, "1-1-a")
        write_spec(sp, "in-review", _spec_baseline(sp), deferred=[HARVEST_A, HARVEST_B])
        return SessionResult(status="timeout")

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", deferred=[HARVEST_A]),
            timeout_with_findings,
            _review_with_deferrals(project, [HARVEST_A]),
        ],
        policy=_salvage_policy(),
    )
    real_harvest = engine._harvest_spec_deferrals
    calls = 0

    def fail_salvage_pair(task, result_json):
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            return VerifyOutcome.retry(
                "spec unreadable while harvesting deferrals (PermissionError: persistent)"
            )
        return real_harvest(task, result_json)

    monkeypatch.setattr(engine, "_harvest_spec_deferrals", fail_salvage_pair)

    summary = engine.run()

    assert summary.deferred == 1 and not summary.done and not summary.crashed
    assert [session.role for session in adapter.sessions] == ["dev", "review"]
    assert calls == 3
    failures = [e for e in engine.journal.entries() if e["kind"] == "review-timeout-salvage-failed"]
    assert [event["harvest_attempt"] for event in failures] == [1, 2]
    task = engine.state.tasks["1-1-a"]
    assert "harvest remained unreadable after 2 attempts" in task.defer_reason


def test_harvest_alone_is_not_proof_of_work(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
            dev_effect(project, "1-1-a", followup_review=False),
        ],
        policy=_harvest_policy(attempts=2),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [d["action"] for d in decisions] == ["retry", "proceed"]
    assert "no changes" in decisions[0]["reason"]
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]
    assert summary.done == 1


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_harvest_gate_resolve_fault_fails_proof_conservatively_without_crashing(
    project, monkeypatch, error_type
):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
            dev_effect(project, "1-1-a", followup_review=False),
        ],
        policy=_harvest_policy(attempts=2),
    )
    real_gate = engine._harvest_gate_exclude
    real_resolve = Path.resolve

    def resolve_fault(self, *args, **kwargs):
        if self == project.deferred_work:
            raise error_type("injected ledger resolve fault")
        return real_resolve(self, *args, **kwargs)

    def gate_with_fault(task):
        with monkeypatch.context() as m:
            m.setattr(Path, "resolve", resolve_fault)
            return real_gate(task)

    monkeypatch.setattr(engine, "_harvest_gate_exclude", gate_with_fault)

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert "no changes" in decisions[0]["reason"]
    assert len(adapter.sessions) == 2
    assert summary.done == 1 and not summary.crashed and not summary.paused


def test_real_work_plus_a_harvest_still_proceeds(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [d["action"] for d in decisions] == ["proceed"]
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert summary.done == 1


def _session_authored_ledger_effect(project, *, deferred=None):
    inner = dev_effect(
        project,
        "1-1-a",
        followup_review=False,
        write_src=False,
        deferred=deferred,
    )

    def effect(spec):
        deferredwork.append_entry(
            project.deferred_work,
            title="Session's own ledger work",
            origin="the session itself",
            source_spec="specs/older.md",
            reason="filed by the session, not by the harvest",
        )
        return inner(spec)

    return effect


def _fixable_harvest_policy(marker):
    """Fail attempt 1 after artifact verification, then pass once repaired."""
    return dataclasses.replace(
        _harvest_policy(),
        scm=ScmPolicy(rollback_on_failure=False),
        verify=VerifyPolicy(commands=(_file_exists_cmd(marker),)),
    )


def _repairing_effect(project, marker, inner):
    """Fix the command gate while reverting attempt 1's source change."""

    def effect(spec):
        marker.write_text("ok\n", encoding="utf-8")
        (project.project / "src.txt").write_text("original\n", encoding="utf-8")
        return inner(spec)

    return effect


def test_session_ledger_edit_is_proof_of_work_even_when_the_attempt_harvests(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [_session_authored_ledger_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [d["action"] for d in decisions] == ["proceed"]
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert summary.done == 1
    assert [entry.title for entry in _harvest_entries(project)] == [
        "Session's own ledger work",
        HARVEST_A["summary"],
    ]


def test_replay_before_harvest_recovers_session_ledger_attribution(project):
    """A crash after session persistence but before harvest has no latched
    engine write. Its session-authored ledger attribution must already be
    durable so replay harvesting cannot hide the attempt's only work.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_session_authored_ledger_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(),
    )

    original_emit = engine._emit

    def crash_after_session_save(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died before ledger attribution")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crash_after_session_save
    assert engine.run().crashed

    crashed_task = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed_task.phase == Phase.DEV_RUNNING
    assert crashed_task.sessions[0].status == "completed"
    assert crashed_task.harvest_wrote_ledger is False
    assert crashed_task.ledger_changed_before_harvest is True

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.ledger_changed_before_harvest is True
    assert adapter.sessions == []
    decisions = [e for e in resumed.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["proceed"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        "Session's own ledger work",
        HARVEST_A["summary"],
    ]


def _downgrade_harvest_state_to_legacy(engine):
    """Remove every harvest field as an older state.json loader would."""
    legacy = engine.state.tasks["1-1-a"]
    legacy.baseline_ledger_digest = None
    legacy.pre_harvest_ledger = None
    legacy.pre_harvest_ledger_captured = False
    legacy.harvest_wrote_ledger = False
    legacy.ledger_changed_before_harvest = False
    legacy.harvested_deferrals = []
    legacy.bundle_closes_intended = []
    legacy.harvest_carry_commit_pending = False
    legacy.isolated_ledger_carried = False
    engine._save()


def test_legacy_replay_derives_session_ledger_attribution_before_harvest(project):
    """A pre-harvest checkpoint from before the attribution fields still resumes.

    Git still carries the attempt baseline, so recovery can distinguish the
    session's existing ledger diff from the engine append it is about to make.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [_session_authored_ledger_effect(project, deferred=[HARVEST_A])],
        policy=_harvest_policy(attempts=1),
    )

    original_emit = engine._emit

    def crash_after_session_save(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died before the legacy engine could verify")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crash_after_session_save
    assert engine.run().crashed

    # Exact defaults loaded from a state.json written before the harvest fields
    # existed. The completed SessionRecord and baseline_commit predate them and
    # remain sufficient to resume the result without launching another session.
    _downgrade_harvest_state_to_legacy(engine)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.deferred
    assert adapter.sessions == []
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.ledger_changed_before_harvest is True
    decisions = [e for e in resumed.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["proceed"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        "Session's own ledger work",
        HARVEST_A["summary"],
    ]


def test_legacy_replay_does_not_credit_its_new_harvest_as_session_work(project):
    """Missing attribution must not let the upgrade's own append satisfy proof."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            )
        ],
        policy=_harvest_policy(attempts=1),
    )

    original_emit = engine._emit

    def crash_after_session_save(stage, *args, **kwargs):
        if stage == "post_session":
            raise RuntimeError("host died before the legacy engine could verify")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crash_after_session_save
    assert engine.run().crashed
    _downgrade_harvest_state_to_legacy(engine)

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.deferred == 1 and not summary.done and not summary.crashed
    assert adapter.sessions == []
    final = load_state(resumed.run_dir).tasks["1-1-a"]
    assert final.ledger_changed_before_harvest is False
    decisions = [e for e in resumed.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["defer"]
    assert "no changes in worktree" in decisions[0]["reason"]


def test_retry_replay_recovers_session_ledger_attribution_after_prior_harvest(project):
    """A rejected harvest is reverted but its stale latch survives until attempt 2.

    Attempt 2's completed result must persist its own attribution before replay
    can consume it, rather than mistaking the old latch for proof that attempt 2
    already harvested.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[HARVEST_A],
            ),
            _session_authored_ledger_effect(project, deferred=[HARVEST_B]),
        ],
        policy=_harvest_policy(),
    )

    original_emit = engine._emit
    post_sessions = 0

    def crash_after_retry_session_save(stage, *args, **kwargs):
        nonlocal post_sessions
        if stage == "post_session":
            post_sessions += 1
            if post_sessions == 2:
                raise RuntimeError("host died before retry ledger attribution")
        return original_emit(stage, *args, **kwargs)

    engine._emit = crash_after_retry_session_save
    assert engine.run().crashed

    crashed_task = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed_task.phase == Phase.DEV_RUNNING
    assert crashed_task.attempt == 2
    assert crashed_task.sessions[-1].status == "completed"
    assert crashed_task.harvest_wrote_ledger is True
    assert crashed_task.ledger_changed_before_harvest is True

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    assert adapter.sessions == []
    decisions = [e for e in resumed.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        "Session's own ledger work",
        HARVEST_B["summary"],
    ]


def test_retry_replay_persists_post_session_hook_ledger_attribution(project, tmp_path):
    """A hook-only repair remains visible across the post-session crash window."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    marker = tmp_path / "repair-complete"
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            _repairing_effect(
                project,
                marker,
                dev_effect(project, "1-1-a", followup_review=False, write_src=False),
            ),
        ],
        policy=_fixable_harvest_policy(marker),
    )
    original_emit = engine._emit
    post_sessions = 0

    def append_from_second_post_session(stage, *args, **kwargs):
        nonlocal post_sessions
        if stage == "post_session":
            post_sessions += 1
            if post_sessions == 2:
                deferredwork.append_entry(
                    project.deferred_work,
                    title="Repair hook's ledger work",
                    origin="post-session repair hook",
                    source_spec="spec-1-1-a.md",
                    reason="the retained repair recorded its only work here",
                )
        return original_emit(stage, *args, **kwargs)

    engine._emit = append_from_second_post_session
    original_run_session = engine._run_session

    def crash_after_second_session_checkpoint(*args, **kwargs):
        result = original_run_session(*args, **kwargs)
        if kwargs.get("seq") == 2:
            raise RuntimeError("host died after post-session hooks")
        return result

    engine._run_session = crash_after_second_session_checkpoint

    assert engine.run().crashed

    crashed_task = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed_task.phase == Phase.DEV_RUNNING
    assert crashed_task.attempt == 2
    assert crashed_task.sessions[-1].status == "completed"
    assert crashed_task.harvest_wrote_ledger is True
    assert crashed_task.ledger_changed_before_harvest is True

    resumed, adapter = resume_engine(project, engine, [])
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert adapter.sessions == []
    decisions = [e for e in resumed.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        "Repair hook's ledger work",
    ]


def test_fixable_retry_kept_harvest_is_not_the_repair_sessions_work(project, tmp_path):
    """A fixable retry keeps attempt 1's tree, including its harvest.

    Attempt 2 reverts the source change and writes nothing. The retained ledger
    entry is still engine-authored, so it must not let the empty repair proceed.
    """
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    marker = tmp_path / "repair-complete"
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            _repairing_effect(
                project,
                marker,
                dev_effect(project, "1-1-a", followup_review=False, write_src=False),
            ),
        ],
        policy=_fixable_harvest_policy(marker),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "retry"]
    assert "no changes" in decisions[-1]["reason"]
    assert summary.paused and summary.done == 0


def test_fixable_retry_unions_harvest_records_across_the_retained_chain(project, tmp_path):
    """A repair pass adds to, rather than replaces, its kept predecessor's records."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    marker = tmp_path / "repair-complete"
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            _repairing_effect(
                project,
                marker,
                dev_effect(
                    project,
                    "1-1-a",
                    followup_review=False,
                    deferred=[HARVEST_B],
                ),
            ),
        ],
        policy=_fixable_harvest_policy(marker),
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    expected = [HARVEST_A["summary"], HARVEST_B["summary"]]
    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert [item["title"] for item in task.harvested_deferrals] == expected
    assert [entry.title for entry in _harvest_entries(project)] == expected


def test_fixable_retry_recomputes_attribution_for_repairs_own_ledger_work(project, tmp_path):
    """The repair's own ledger edit must stand down the path-wide exclusion."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    marker = tmp_path / "repair-complete"
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a", followup_review=False, deferred=[HARVEST_A]),
            _repairing_effect(project, marker, _session_authored_ledger_effect(project)),
        ],
        policy=_fixable_harvest_policy(marker),
    )

    summary = engine.run()

    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert [decision["action"] for decision in decisions] == ["retry", "proceed"]
    assert summary.done == 1 and not summary.paused
    assert [entry.title for entry in _harvest_entries(project)] == [
        HARVEST_A["summary"],
        "Session's own ledger work",
    ]


def test_workflow_marker_is_named_for_the_workflows_own_role_tree(project):
    """The completion-marker filename (WORKFLOW_COMPLETION_CONTRACT) is PRODUCED
    from ``_dev_skill(role)`` — the injected workflow's OWN role, not the dev
    default. A plugin workflow declares `role = "dev" | "review"` (WORKFLOW_ROLES)
    and runs on THAT adapter, whose skill tree can sit at a different upstream era
    than dev's: here review=gemini on a pre-rename `.agents/skills` beside
    dev=claude on a post-rename `.claude/skills`. Resolving off the dev tree would
    name the marker after a primitive the session's own tree does not carry.

    Asserted at the `_run_session` seam — the lowest layer that runs the producer —
    by reading the prompt that actually reached the review adapter."""
    from conftest import attach_profile, install_build_auto_skill, install_dev_base_skills

    install_build_auto_skill(project.project, ".claude/skills")  # dev tree: post-rename
    install_dev_base_skills(project.project, ".agents/skills", folder_id=False)  # review: legacy
    review = attach_profile(MockAdapter([SessionResult(status="completed")]), "gemini")
    engine, dev = make_engine(project, [], review_adapter=review)
    attach_profile(dev, "claude")
    task = StoryTask(story_key="1-1-a", epic=1)

    engine._run_session(task, role="review", prompt="p", seq=1, label="tea.pre_commit_gate")

    (dispatched,) = review.sessions
    assert not dev.sessions  # the workflow ran on the role's adapter, not dev's
    marker = project.implementation_artifacts / f"bmad-dev-auto-result-{dispatched.task_id}.md"
    assert str(marker) in dispatched.prompt  # the REVIEW tree's era
    assert "bmad-build-auto-result-" not in dispatched.prompt  # never the dev tree's
    # ...and the dev tree genuinely resolves to the other era, so the two trees
    # disagreeing is what the assertions above are reading — not two names that
    # happen to coincide.
    assert engine._dev_skill() == "bmad-build-auto"
    assert engine._dev_skill_cache == {
        (project.project, ".agents/skills"): "bmad-dev-auto",
        (project.project, ".claude/skills"): "bmad-build-auto",
    }


def test_dev_prompt_resolves_in_the_reopened_worktree_not_the_main_checkout(project, tmp_path):
    """A resumed unit dispatches the name ITS OWN worktree carries.

    `reopen_unit` re-mounts an existing worktree without re-provisioning it (only
    the fresh-mount path in `run_isolated` calls `provision_worktree`), so a main
    checkout upgraded across the pause — the operator updated bmm while the run sat
    at an escalation — leaves the worktree on the old era while the resume preflight
    passes against the upgraded checkout. Resolving off the main checkout would
    spell `/bmad-build-auto` into a worktree carrying only `bmad-dev-auto`: the
    session runs with `cwd=self.workspace.root`, HALTs on an unknown command having
    written nothing for verify to read, and burns its dev attempts through to DEFER.

    The second half pins the MEMO, which is the half a workspace-rooted resolution
    alone gets wrong: one Engine drives every unit of a run, so the reopened
    worktree's answer must not be served to the fresh worktrees mounted after it."""
    from conftest import attach_profile, install_build_auto_skill, install_dev_base_skills

    from bmad_loop.workspace import Workspace

    install_build_auto_skill(project.project, ".claude/skills")  # main checkout: upgraded
    worktree = tmp_path / "wt"
    worktree.mkdir()
    install_dev_base_skills(worktree, ".claude/skills", folder_id=False)  # worktree: legacy
    engine, adapter = make_engine(project, [])
    attach_profile(adapter)
    default = engine.workspace

    engine.workspace = Workspace(root=worktree, paths=engine.paths.rebased(worktree))
    assert engine._generic_dev_prompt(_prompt_task(), None).startswith("/bmad-dev-auto 1-1-a")

    engine.workspace = default
    assert engine._generic_dev_prompt(_prompt_task(), None).startswith("/bmad-build-auto 1-1-a")
    assert engine._dev_skill_cache == {
        (worktree.resolve(), ".claude/skills"): "bmad-dev-auto",
        (project.project, ".claude/skills"): "bmad-build-auto",
    }
