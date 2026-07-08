import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
from conftest import git, spec_path, write_spec, write_sprint

from bmad_loop import verify
from bmad_loop.model import StoryTask
from bmad_loop.policy import Policy, VerifyPolicy


def make_task(paths, story_key="1-1-a"):
    task = StoryTask(story_key=story_key, epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def dev_result(sp):
    return {"workflow": "auto-dev", "spec_file": str(sp)}


def test_attempt_dirty_clean_tree(project):
    """At baseline with no changes — nothing for a rollback to undo."""
    baseline = verify.rev_parse_head(project.project)
    assert verify.attempt_dirty(project.project, baseline, []) is False


def test_attempt_dirty_tracked_change(project):
    """A modified tracked file is a tracked diff vs baseline."""
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("changed\n")
    assert verify.attempt_dirty(project.project, baseline, []) is True


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("in-review", "in-review"),
        ("  in-review  ", "in-review"),
        ("In-Review", "in-review"),
        ("DONE", "done"),
        (None, ""),
        (123, "123"),
    ],
)
def test_status_of_normalizes(raw, expected):
    assert verify.status_of({"status": raw} if raw is not None else {}) == expected


def test_verify_dev_happy(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok
    assert task.spec_file == str(sp)


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

    ok_policy = Policy(verify=VerifyPolicy(commands=("true",)))
    assert verify.verify_review(task, project, ok_policy).ok

    fail_policy = Policy(verify=VerifyPolicy(commands=("true", "false")))
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


def test_commit_paths_noop_when_unchanged(project):
    assert verify.commit_paths(project.project, "noop", [project.project / "src.txt"]) is None
    # a path outside the repo is ignored, not an error
    assert verify.commit_paths(project.project, "noop", [project.project.parent / "x"]) is None


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


def test_verify_dev_exclude_relpaths_is_file_granular(project):
    """Unlike artifact_relpaths (whole-folder), this excludes only the
    sprint-status ledger and the session's own claimed spec file — sibling
    artifact-folder content (deferred-work.md, other stories' specs) is left
    un-excluded so it can register as real work."""
    sp = spec_path(project, "1-1-a")
    rels = verify.verify_dev_exclude_relpaths(project, sp)
    assert "_bmad-output/implementation-artifacts/sprint-status.yaml" in rels
    assert "_bmad-output/implementation-artifacts/spec-1-1-a.md" in rels
    assert "_bmad-output/implementation-artifacts" not in rels
    # output_folder itself is NOT excluded here — it is the parent dir of
    # implementation_artifacts in the standard layout, so excluding it as a
    # prefix would swallow the artifact dirs' content right back out of view.
    assert "_bmad-output" not in rels
    assert "_bmad-output/implementation-artifacts/deferred-work.md" not in rels


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
    assert verify.verify_dev_exclude_relpaths(project, sp) == verify.verify_dev_exclude_relpaths(
        project, messy
    )


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
    exclude = verify.verify_dev_exclude_relpaths(project, sp)

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
    """keep=0 means "never prune" — return [] without even invoking git."""
    repo = project.project
    _dirty_ref(repo, "run-0")

    def _boom(*a, **k):
        raise AssertionError("git must not run when keep=0")

    monkeypatch.setattr(verify, "_git", _boom)
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
