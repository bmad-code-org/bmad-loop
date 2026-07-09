"""Escalation-resolution: context build, re-arm, spec status writer, session."""

import json

import pytest
import yaml
from conftest import git

from bmad_loop import devcontract, resolve, runs, verify
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
)

SPEC = """\
---
title: List command
status: in-review
owner: amelia
---

# Spec

<frozen-after-approval>
Filter notes by workspace name.
</frozen-after-approval>
"""


def _escalated_run(
    project,
    run_id="20260613-111429-6a14",
    *,
    spec_file=None,
    with_session=True,
    source="sprint-status",
    sentinel_kind="",
):
    task = StoryTask(
        story_key="6-4-cli-list-command",
        epic=6,
        phase=Phase.ESCALATED,
        attempt=1,
        review_cycle=1,
        baseline_commit="abc123",
        spec_file=spec_file,
        sentinel_kind=sentinel_kind,
    )
    if with_session:
        task.sessions.append(
            SessionRecord(
                task_id="6-4-cli-list-command-review-1", role="review", status="completed"
            )
        )
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at="2026-06-13T11:14:29",
        paused_reason="CRITICAL escalation from review session: names not unique",
        paused_stage=PAUSE_ESCALATION,
        paused_story_key="6-4-cli-list-command",
        tasks={task.story_key: task},
        source=source,
    )
    run_dir = project / ".bmad-loop" / "runs" / run_id
    save_state(run_dir, state)
    return run_dir, state, task


# ----------------------------------------------------------- set_frontmatter_status


def test_set_frontmatter_status_replaces(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    assert verify.set_frontmatter_status(spec, "ready-for-dev") is True
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    # other fields + the frozen block survive untouched
    text = spec.read_text(encoding="utf-8")
    assert "owner: amelia" in text
    assert "<frozen-after-approval>" in text
    assert "title: List command" in text


def test_set_frontmatter_status_idempotent(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    verify.set_frontmatter_status(spec, "ready-for-dev")
    # second call is a no-op (already at the target)
    assert verify.set_frontmatter_status(spec, "ready-for-dev") is False


def test_set_frontmatter_status_no_frontmatter(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# just a heading\n", encoding="utf-8")
    assert verify.set_frontmatter_status(spec, "ready-for-dev") is False


def test_set_frontmatter_field_replaces_inserts_idempotent(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    assert verify.set_frontmatter_field(spec, "owner", "winston") is True
    assert verify.read_frontmatter(spec)["owner"] == "winston"
    # unlike set_frontmatter_status, a missing key is INSERTED (block's last line)
    assert verify.set_frontmatter_field(spec, "baseline_revision", "abc123") is True
    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == "abc123"
    assert fm["status"] == "in-review" and fm["title"] == "List command"  # untouched
    # idempotent: already at the target value
    assert verify.set_frontmatter_field(spec, "baseline_revision", "abc123") is False
    # no frontmatter block -> no write
    bare = tmp_path / "bare.md"
    bare.write_text("# just a heading\n", encoding="utf-8")
    assert verify.set_frontmatter_field(bare, "baseline_revision", "abc123") is False


# ----------------------------------------------------------- build_context


def test_build_context_gathers_critical_escalations(tmp_path):
    run_dir, state, task = _escalated_run(tmp_path, spec_file="/abs/spec.md")
    task_dir = run_dir / "tasks" / "6-4-cli-list-command-review-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(
        json.dumps(
            {
                "escalations": [
                    {"type": "spec-gap", "severity": "CRITICAL", "detail": "names not unique"},
                    {"type": "nit", "severity": "PREFERENCE", "detail": "ignore me"},
                ]
            }
        ),
        encoding="utf-8",
    )
    path = resolve.build_context(state, run_dir, "6-4-cli-list-command")
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["story_key"] == "6-4-cli-list-command"
    assert ctx["spec_file"] == "/abs/spec.md"
    assert ctx["baseline_commit"] == "abc123"
    details = [e["detail"] for e in ctx["escalations"]]
    assert "names not unique" in details
    assert "ignore me" not in details  # PREFERENCE dropped
    assert ctx["resolution_path"].endswith("resolve/6-4-cli-list-command/resolution.json")
    # serialized via as_posix(): forward slashes only, so the context contract is
    # identical across OSes (no backslashes leak in on Windows).
    assert "\\" not in ctx["resolution_path"]


def test_build_context_no_session_files(tmp_path):
    run_dir, state, _ = _escalated_run(tmp_path, with_session=False)
    path = resolve.build_context(state, run_dir, "6-4-cli-list-command")
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["escalations"] == []
    assert ctx["paused_reason"].startswith("CRITICAL")


def test_build_context_restore_supported_signal(tmp_path):
    """The agent must know up front when a patch-restore can't be honored
    (worktree isolation / a worktree-executed task), so it never negotiates a
    restore the orchestrator will reject after the session."""
    run_dir, state, task = _escalated_run(tmp_path, with_session=False)
    key = "6-4-cli-list-command"

    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is True

    path = resolve.build_context(state, run_dir, key, isolation="worktree")
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False

    task.worktree_path = str(tmp_path / "wt")  # recorded worktree execution
    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False


# ----------------------------------------------------------- rearm_escalation


def test_rearm_flips_phase_and_spec_status(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    key = runs.rearm_escalation(run_dir)
    assert key == "6-4-cli-list-command"
    state = load_state(run_dir)
    task = state.tasks[key]
    assert task.phase == Phase.PENDING
    assert task.attempt == 0
    assert task.review_cycle == 0
    # spec re-armed for a clean re-implement, even though the agent left it in-review
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    # pause is NOT cleared here — resume does that
    assert state.paused_stage == PAUSE_ESCALATION


def test_rearm_strips_stale_terminal_section(tmp_path):
    """Re-arm must drop the escalated attempt's `## Auto Run Result` along with
    the status flip (mirroring engine._reset_spec_for_repair): the heading is
    what find_result_artifact keys on, so leaving it would let the re-driven
    session's first save of the spec parse as the prior attempt's outcome."""
    spec = tmp_path / "spec-6-4.md"
    spec.write_text(
        SPEC + "\n## Auto Run Result\n\nStatus: blocked\nnames not unique\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    runs.rearm_escalation(run_dir)
    text = spec.read_text(encoding="utf-8")
    assert "Auto Run Result" not in text and "names not unique" not in text
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert "<frozen-after-approval>" in text  # intent body untouched
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_rearm_journals_event(tmp_path):
    run_dir, _, _ = _escalated_run(tmp_path)
    runs.rearm_escalation(run_dir)
    journal = (run_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert "story-escalation-resolved" in journal


def test_rearm_advances_baseline_to_resolved_head(project):
    # The resolve session committed work on the project branch (e.g. a fixture
    # the human authorized). Re-arm must adopt that state as the new attempt
    # baseline, or the redrive's reset-to-baseline parks the resolution commit
    # on an attempt-preserve ref and re-drives against the unresolved tree.
    root = project.project
    old_head = git(root, "rev-parse", "HEAD")
    run_dir, _, _ = _escalated_run(root)
    (root / "fixture.txt").write_text("captured baseline\n", encoding="utf-8")
    git(root, "add", "fixture.txt")
    git(root, "commit", "-q", "-m", "resolution: capture fixture")
    # a file the resolve session (or the user) left untracked must enter the
    # snapshot, so the redrive reset treats it as pre-existing, not run-created
    (root / "leftover.txt").write_text("keep me\n", encoding="utf-8")
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == git(root, "rev-parse", "HEAD")
    assert task.baseline_commit != old_head
    assert "leftover.txt" in task.baseline_untracked


def test_rearm_baseline_all_or_nothing_on_partial_git_failure(monkeypatch, project):
    """rev_parse_head succeeding but untracked_files failing must not advance
    baseline_commit while leaving baseline_untracked stale: both locals are
    computed before either field is assigned, so a failure on the second call
    leaves the pair exactly as it was, same as a failure on the first."""
    root = project.project
    run_dir, _, _ = _escalated_run(root)

    def boom(repo):
        raise verify.GitError("simulated failure")

    monkeypatch.setattr(runs.verify, "untracked_files", boom)
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == "abc123"
    assert task.baseline_untracked is None


def test_rearm_keeps_stale_baseline_outside_a_repo(tmp_path):
    # best-effort contract: a project dir that is not a git repo (or a broken
    # one) must not make re-arm fail — the old baseline simply stands
    run_dir, _, _ = _escalated_run(tmp_path)
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == "abc123"


def test_rearm_clears_sentinel_preserving_a_copy(tmp_path):
    """Stories mode: a fixed-slug sentinel (`<id>-unresolved.md`) is cleared by
    deletion, not a status flip — re-arm preserves a copy, journals the blocking
    condition, drops the sentinel, and unsets spec_file so the re-dispatch starts
    clean (PENDING → re-plan from scratch)."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    # sentinel_kind recorded at detection time (StoriesEngine stamps it on the task);
    # re-arm clears by this recorded verdict, not the basename.
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    returned = runs.rearm_escalation(run_dir)
    assert returned == key

    # sentinel deleted from disk, a copy preserved under the run dir
    assert not sentinel.exists()
    preserved = run_dir / "sentinels" / f"{key}-unresolved.md"
    assert preserved.is_file() and "intent too vague" in preserved.read_text(encoding="utf-8")

    state = load_state(run_dir)
    task = state.tasks[key]
    assert task.phase == Phase.PENDING
    assert task.spec_file is None  # cleared → next dispatch resolves to PENDING

    journal = (run_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert "sentinel-cleared" in journal
    cleared = [
        json.loads(line)
        for line in journal.splitlines()
        if json.loads(line).get("kind") == "sentinel-cleared"
    ]
    # the journal carries the fixed slug (sentinel_kind) AND the recorded blocking
    # condition parsed from the sentinel's ## Auto Run Result (not just the slug).
    assert cleared[0]["sentinel_kind"] == "unresolved" and cleared[0]["story_key"] == key
    assert "intent too vague" in cleared[0]["condition"]


def test_rearm_non_sentinel_spec_still_flips_status(tmp_path):
    """C3: a blocked (non-sentinel) story spec IN STORIES MODE is re-opened by the
    status flip, not deleted — the sentinel branch must not swallow the normal re-arm
    path. Runs with source="stories" (the default sprint-status source would skip the
    sentinel branch entirely and never exercise the stories-mode logic)."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"  # a real spec, not a fixed-slug sentinel
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    # source="stories" enters the sentinel-branch code; sentinel_kind="" (never
    # detected as a sentinel) → status-flip, not delete.
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # not deleted
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()


def test_rearm_sentinel_named_spec_never_detected_is_not_deleted(tmp_path):
    """C2: a real stories spec that merely happens to be named `<key>-unresolved.md`
    but was NEVER detected as a sentinel (task.sentinel_kind == "") is status-flipped
    and kept — re-arm must clear by the recorded detection verdict, not the basename,
    so a filename collision can never turn a real spec into data loss."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-unresolved.md"  # sentinel-shaped name, but a real spec
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nreal work\n", encoding="utf-8")
    # stories mode, but sentinel_kind unset — the run never classified it as a sentinel
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # NOT deleted despite the sentinel-shaped name
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()


def test_rearm_sprint_spec_named_like_a_sentinel_is_not_deleted(tmp_path):
    """MINOR-G: the sentinel-clear path is stories-mode-only. A *sprint* spec that
    merely happens to be named `<key>-unresolved.md` must be status-flipped and
    kept like any other spec — never deleted — since the fixed-slug sentinel
    convention exists only in stories mode. (source defaults to sprint-status.)"""
    key = "6-4-cli-list-command"
    spec = tmp_path / f"{key}-unresolved.md"  # sentinel-shaped name, but a sprint spec
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nreal work\n", encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))  # sprint-status source

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # NOT deleted despite the sentinel-shaped name
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"  # flipped like any spec
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()  # no sentinel preservation in sprint mode


def test_rearm_rejects_restore_patch_on_a_sentinel(tmp_path):
    """T1 (stories x patch-restore): a sentinel-wedged story escalated BEFORE
    planning — there is no attempted implementation to restore, and its re-arm
    re-dispatches a planning leg, so laying a patch onto the tree first is never
    safe. Re-arm must reject loudly BEFORE mutating anything: the sentinel stays
    on disk, the task stays ESCALATED, and no latch is persisted."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    with pytest.raises(runs.RearmError, match="sentinel"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    assert sentinel.is_file()  # nothing deleted, copy NOT preserved — no clear happened
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.ESCALATED  # not re-armed; the escalation stays armed
    assert task.restore_patch is None  # no latch persisted
    assert task.sentinel_kind == "unresolved"  # detection verdict intact for a retry
    # nothing was journaled at all — no sentinel-cleared, no story-escalation-resolved
    assert not (run_dir / "journal.jsonl").exists()


def test_rearm_rejects_restore_patch_without_a_spec_file(tmp_path):
    """A restore only works through the spec's in-review flip, so an escalated
    task with NO recorded spec (ambiguous two-file wedge, unknown --story
    selector, session died before naming one) has no routing target: the latch
    would stick, the flip would be skipped, and the engine would lay the patch
    onto the tree before a planning leg. Rejected before any mutation."""
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=None)

    with pytest.raises(runs.RearmError, match="no recorded spec file"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.phase == Phase.ESCALATED  # not re-armed; the escalation stays armed
    assert task.restore_patch is None  # no latch persisted
    assert not (run_dir / "journal.jsonl").exists()  # nothing journaled

    runs.rearm_escalation(run_dir)  # a from-scratch re-arm remains available
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.PENDING


def test_rearm_restore_patch_on_a_real_stories_spec_is_allowed(tmp_path):
    """The T1 guard keys on the recorded sentinel verdict, not on stories mode:
    a review-stage intent gap on a REAL stories spec (sentinel_kind unset) is a
    legitimate restore target and re-arms to in-review like sprint mode."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.PENDING
    assert task.restore_patch == "artifacts/attempt.patch"
    assert verify.read_frontmatter(spec)["status"] == "in-review"  # restore routing


def _resolve_repo(tmp_path):
    """A tiny real repo so rearm's baseline advance has a HEAD to read."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@test")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / ".gitignore").write_text(".bmad-loop/runs/\n")
    (tmp_path / "src.txt").write_text("original\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return git(tmp_path, "rev-parse", "HEAD")


def test_rearm_restore_patch_restamps_spec_baseline(tmp_path):
    """The in-review route skips step-03 — the only step that stamps
    `baseline_revision` — so the patch-restore re-arm re-stamps it to the
    advanced baseline itself. Otherwise the re-driven step-04 would build its
    review diff (and, on an intent-gap/bad-spec re-triage, revert) "since" the
    ORIGINAL pre-attempt sha, clawing back the very resolve-session commits the
    baseline advance blesses as the re-drive's starting point."""
    key = "6-4-cli-list-command"
    old_head = _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(
        f"---\nstatus: blocked\nbaseline_revision: {old_head}\n---\n\n## Intent\n\nx\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    # the resolve session leaves a commit NOT overlapping the patch (blessed input)
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")
    new_head = git(tmp_path, "rev-parse", "HEAD")

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == new_head  # step-04 diffs from the ADVANCED baseline
    assert fm["status"] == "in-review"
    assert load_state(run_dir).tasks[key].baseline_commit == new_head


def test_rearm_from_scratch_leaves_spec_baseline_alone(tmp_path):
    """A from-scratch re-arm routes ready-for-dev -> step-03, which re-stamps
    `baseline_revision` itself — the re-arm must not touch it."""
    old_head = _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(
        f"---\nstatus: blocked\nbaseline_revision: {old_head}\n---\n\n## Intent\n\nx\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")

    runs.rearm_escalation(run_dir)  # no restore

    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == old_head  # untouched; step-03 owns the stamp
    assert fm["status"] == "ready-for-dev"


# -------------------------------------------------- non-UTF-8 robustness (bug class)
# A story spec / sentinel is agent- or human-authored, so it can contain non-UTF-8
# bytes. `read_text(encoding="utf-8")` raises UnicodeDecodeError (a ValueError, NOT an
# OSError), so the stories-mode read paths must tolerate it rather than crash an
# already-degraded escalation-resolution flow. Mirrors install.py's stories-support probe.

_BAD_UTF8 = b"\xff\xfe\x00\x01 not utf-8 \x80\x81"


def test_build_context_tolerates_non_utf8_present_spec(tmp_path):
    """A non-UTF-8 PRESENT story spec makes resolve_story_spec's frontmatter read
    raise UnicodeDecodeError; build_context must degrade to best-effort (folder-only)
    stories context, not crash the resolve command."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / f"{key}-slug.md").write_bytes(_BAD_UTF8)  # a real spec, undecodable
    run_dir, state, _ = _escalated_run(tmp_path, source="stories")

    path = resolve.build_context(state, run_dir, key)  # must not raise
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["stories"]["spec_folder"] == ""  # best-effort context still produced
    assert "sentinel" not in ctx["stories"]  # the undecodable spec yields no sentinel


def test_build_context_tolerates_non_utf8_sentinel(tmp_path):
    """A non-UTF-8 sentinel makes the blocking-condition read raise UnicodeDecodeError;
    build_context still emits the sentinel indicator with an empty condition."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / f"{key}-unresolved.md").write_bytes(_BAD_UTF8)  # undecodable sentinel
    run_dir, state, _ = _escalated_run(tmp_path, source="stories", sentinel_kind="unresolved")

    path = resolve.build_context(state, run_dir, key)  # must not raise
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["stories"]["sentinel"]["kind"] == "unresolved"
    assert ctx["stories"]["sentinel"]["blocking_condition"] == ""  # unreadable → empty


def test_rearm_non_utf8_present_spec_fails_clean_and_stays_armed(tmp_path):
    """The non-sentinel re-arm branch re-reads the spec as UTF-8 to flip its
    status. An undecodable PRESENT spec is a first-class escalation state
    (resolve_story_spec degrades it to a wedge), so it can reach this flip: rearm
    must fail with an actionable RearmError BEFORE anything is persisted — the
    escalation stays armed for a retry once the human fixes/replaces the file —
    never a traceback out of `bmad-loop resolve`."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"  # a real spec, not a fixed-slug sentinel
    spec.write_bytes(_BAD_UTF8)
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    with pytest.raises(runs.RearmError) as exc:
        runs.rearm_escalation(run_dir)
    assert "UTF-8" in str(exc.value) and "resolve" in str(exc.value)
    assert spec.read_bytes() == _BAD_UTF8  # spec untouched
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.ESCALATED  # nothing persisted; still armed for resolve


def test_rearm_tolerates_non_utf8_sentinel(tmp_path):
    """A binary/non-UTF-8 sentinel must not crash rearm_escalation: the sentinel is
    still preserved+deleted, the run re-arms, and the journal records an empty
    blocking condition rather than wedging the run on a decode error."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_bytes(_BAD_UTF8)
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    assert runs.rearm_escalation(run_dir) == key  # must not raise
    assert not sentinel.exists()  # cleared by deletion
    assert (run_dir / "sentinels" / f"{key}-unresolved.md").is_file()  # copy preserved
    assert load_state(run_dir).tasks[key].spec_file is None  # cleared → PENDING re-dispatch

    cleared = [
        json.loads(line)
        for line in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "sentinel-cleared"
    ]
    assert cleared[0]["sentinel_kind"] == "unresolved" and cleared[0]["condition"] == ""


def test_read_resolution_non_utf8_marker_is_resolution_error(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 marker
    must surface as the clean ResolutionError every consumer handles (CLI abort,
    TUI conservative warning), never an uncaught decode crash."""
    marker = resolve.resolution_path(tmp_path, "6-4-cli-list-command")
    marker.parent.mkdir(parents=True)
    marker.write_bytes(_BAD_UTF8)
    with pytest.raises(resolve.ResolutionError, match="unreadable"):
        resolve.read_resolution(tmp_path, "6-4-cli-list-command")


def test_rearm_rejects_non_escalation_stage(tmp_path):
    run_dir = tmp_path / ".bmad-loop" / "runs" / "r1"
    save_state(
        run_dir,
        RunState(
            run_id="r1", project=str(tmp_path), started_at="now", paused_stage="spec-approval"
        ),
    )
    with pytest.raises(runs.RearmError, match="not paused at an escalation"):
        runs.rearm_escalation(run_dir)


def test_rearm_rejects_unescalated_story(tmp_path):
    run_dir, state, task = _escalated_run(tmp_path)
    task.phase = Phase.DONE  # terminal but not escalated
    save_state(run_dir, state)
    with pytest.raises(runs.RearmError, match="not escalated"):
        runs.rearm_escalation(run_dir)


# ----------------------------------------------------------- run_session


class _FakeAdapter:
    def __init__(self, on_run):
        self._on_run = on_run

    def interactive_argv(self, spec):
        return ["fake-agent", spec.prompt]

    def interactive_env(self, spec):
        return dict(spec.env)


def test_run_session_detects_resolution(tmp_path, monkeypatch):
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")

    def fake_subprocess_run(argv, cwd, env):
        # simulate the agent writing the resolution marker
        resolve.resolution_path(run_dir, "6-4-cli-list-command").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(resolve.subprocess, "run", fake_subprocess_run)
    adapter = _FakeAdapter(None)
    assert resolve.run_session(adapter, tmp_path, run_dir, "6-4-cli-list-command") is True


def test_run_session_no_resolution(tmp_path, monkeypatch):
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")
    monkeypatch.setattr(resolve.subprocess, "run", lambda *a, **k: None)
    assert (
        resolve.run_session(_FakeAdapter(None), tmp_path, run_dir, "6-4-cli-list-command") is False
    )


def test_run_session_clears_stale_marker(tmp_path, monkeypatch):
    """A marker left by a previous resolve of this story must not be read as
    this session's output (the agent that says 'already resolved' writes none)."""
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")
    stale = resolve.resolution_path(run_dir, "6-4-cli-list-command")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"from": "last time"}', encoding="utf-8")
    monkeypatch.setattr(resolve.subprocess, "run", lambda *a, **k: None)  # agent records nothing
    assert (
        resolve.run_session(_FakeAdapter(None), tmp_path, run_dir, "6-4-cli-list-command") is False
    )
    assert not stale.exists()  # stale marker was removed, not reused


# ---------------- item 9: build_context stories-mode enrichment --------------


def _stories_manifest(folder, entries):
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def test_build_context_stories_carries_manifest_entry(tmp_path):
    """Stories mode: context.json carries the manifest intent (spec folder + the
    story entry's title/description/checkpoints/invoke_dev_with) for the resolver."""
    key = "6-4-cli-list-command"
    _stories_manifest(
        tmp_path / "epic-1",
        [
            {
                "id": key,
                "title": "List command",
                "description": "list notes",
                "spec_checkpoint": True,
                "invoke_dev_with": "use redis",
            }
        ],
    )
    run_dir, state, _ = _escalated_run(tmp_path, spec_file="/abs/spec.md", source="stories")
    state.spec_folder = "epic-1"

    ctx = json.loads(resolve.build_context(state, run_dir, key).read_text(encoding="utf-8"))
    st = ctx["stories"]
    assert st["spec_folder"] == "epic-1"
    assert st["story"]["title"] == "List command"
    assert st["story"]["spec_checkpoint"] is True
    assert st["story"]["invoke_dev_with"] == "use redis"
    assert "sentinel" not in st  # an ordinary escalation has no sentinel block


def test_build_context_stories_sentinel_indicator(tmp_path):
    """Stories mode: a sentinel-escalated story carries a sentinel indicator with
    its kind and recorded blocking condition (so the resolver knows there is no
    frozen spec to edit)."""
    key = "6-4-cli-list-command"
    folder = tmp_path / "epic-1"
    _stories_manifest(folder, [{"id": key, "title": "t", "description": "d"}])
    sentinel = folder / "stories" / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    run_dir, state, _ = _escalated_run(tmp_path, spec_file=str(sentinel), source="stories")
    state.spec_folder = "epic-1"

    ctx = json.loads(resolve.build_context(state, run_dir, key).read_text(encoding="utf-8"))
    sent = ctx["stories"]["sentinel"]
    assert sent["kind"] == "unresolved"
    assert "intent too vague" in sent["blocking_condition"]


def test_build_context_sprint_mode_has_no_stories_block(tmp_path):
    """Sprint mode leaves the context contract unchanged — no stories block."""
    run_dir, state, _ = _escalated_run(tmp_path, spec_file="/abs/spec.md")  # sprint source
    ctx = json.loads(
        resolve.build_context(state, run_dir, "6-4-cli-list-command").read_text(encoding="utf-8")
    )
    assert "stories" not in ctx
