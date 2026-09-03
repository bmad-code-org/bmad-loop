"""Phase 3: isolation="worktree" — each unit runs in its own git worktree and
merges back into the target branch locally. Sessions run inside the worktree
(spec.cwd), so the effects here write artifacts rebased onto that checkout.

Exercised end-to-end against the conftest `project` sandbox with the mock
adapter (no tmux, no LLM).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from conftest import (
    _OK,
    _exists_run,
    _seeded_then_touch,
    _spec_baseline,
    _touch_run,
    attach_profile,
    crash_at_merge_back,
    git,
    ignore_before_commit,
    install_build_auto_skill,
    refuse_to_resolve,
    set_sprint,
    write_gated_ledger,
    write_ledger,
    write_spec,
    write_sprint,
)

from bmad_loop import deferredwork, runs, sprintstatus, verify, worktree_flow
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.engine import Engine, _story_label_stripped
from bmad_loop.install import (
    BMAD_SCRIPTS_SEED_REL,
    CENTRAL_CONFIG_REL,
    DEV_PRIMITIVE_NEW,
    MODULE_SKILLS,
)
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy
from bmad_loop.verify import (
    branch_exists,
    current_branch,
    rev_parse_head,
    worktree_clean,
    worktree_list,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def wt_policy(*, limits: LimitsPolicy | None = None, **scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree", **scm),
        limits=limits if limits is not None else LimitsPolicy(),
    )


def commit_sprint(project, statuses: dict[str, str]) -> None:
    """Worktrees are checkouts of a commit, so the sprint board (and artifact
    dirs) must be committed before the run, not left untracked."""
    write_sprint(project, statuses)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sprint")


def wt_dev_effect(
    project,
    story_key,
    *,
    final_status="done",
    followup_review=True,
    write_src=True,
    closes_deferred=None,
    operator_actions=None,
    deferred=None,
):
    """Dev session running inside the unit worktree (spec.cwd). Mirrors the
    bmad-dev-auto skill: self-finalizes the spec to done, never writes the sprint
    board (the orchestrator advances it via the B2 seam, inside the worktree).
    ``followup_review`` mirrors the skill's `followup_review_recommended` signal;
    defaults True so the review runs under the default trigger = "recommended"."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        if write_src:
            src = cwd / "src.txt"
            src.write_text(src.read_text() + f"change for {story_key}\n")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        # A real dev session creates its artifacts dir. Most rows here commit the
        # board, so the checkout delivers the dir and this is a no-op — but a row
        # over a GITIGNORED board has nothing tracked in there at all, and without
        # this the session would die on the spec write before reaching the gate the
        # row is about (`wt_bundle_dev` in test_sweep.py says the same).
        sp.parent.mkdir(parents=True, exist_ok=True)
        write_spec(
            sp,
            final_status,
            baseline,
            closes_deferred=closes_deferred,
            operator_actions=operator_actions,
            deferred=deferred,
        )
        # NO set_sprint: the orchestrator is the single sprint-status writer
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "followup_review_recommended": followup_review,
            },
        )

    return effect


def wt_bad_dev(project, story_key):
    """Dev session that commits inside the unit worktree and then claims a foreign
    baseline. `verify_dev` rejects that NON-fixably, so the retry routes through
    `_rollback_or_pause` — which, inside a mounted worktree, auto-recovers and parks
    the attempt on an `attempt-preserve/*` ref (#161). The one composable way to
    reach an in-worktree rollback; `wt_dev_effect` never does."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + "bad attempt\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", "bad attempt work")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        write_spec(sp, "in-review", "0" * 40)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "escalations": [],
            },
        )

    return effect


def wt_review_effect(project, story_key, clean: bool, patched: int = 0, deferred=None):
    """Follow-up review pass in a worktree — a bmad-dev-auto re-invocation on the
    done spec. ``clean=True`` converges; ``clean=False`` keeps recommending."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline, deferred=deferred)
        set_sprint(wt, story_key, "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect


def make_engine(project, script, policy=None, run_id="test-run", **kwargs):
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id=run_id, project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy or wt_policy(),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def journal_kinds(engine):
    return [e["kind"] for e in engine.journal.entries()]


# ----------------------------------------------------------------- happy path


def test_worktree_happy_path_merges_to_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    # the unit's work landed on the target branch (main, checked out in the repo)
    assert engine.state.target_branch == "main"
    assert rev_parse_head(project.project) != head_before
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # worktree cleaned up, branch deleted (delete_branch default), tree clean
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "worktree-opened" in kinds and "unit-merged" in kinds
    # a clean teardown degrades nothing (gh-139): no warning event is emitted
    assert "worktree-teardown-degraded" not in kinds


def test_local_absolute_ignored_accepted_spec_is_seeded_and_bound_in_mount(project):
    """Relativizing an accepted spec also delivers it to a tracked-only checkout."""
    rel = "_bmad-output/implementation-artifacts/accepted-untracked.md"
    ignore_before_commit(project, rel)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    accepted = project.project / rel
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_bytes(b"accepted operator bytes\n")
    engine, _ = make_engine(project, [], policy=wt_policy(keep_failed=False))
    engine.state.target_branch = "main"
    task = StoryTask("1-1-a", 1, spec_file=str(accepted))
    engine.state.tasks[task.story_key] = task
    observed: dict[str, object] = {}

    def bind_then_defer(current):
        engine._bind_dispatched_spec_for_attempt(current)
        observed["path"] = current.dispatched_spec_file
        observed["snapshot"] = current.dispatched_spec_snapshot
        observed["root"] = engine.workspace.root
        current.phase = Phase.DEFERRED
        current.defer_reason = "test complete"

    engine._run_isolated(task, bind_then_defer)

    mounted_root = observed["root"]
    assert isinstance(mounted_root, Path)
    assert observed["path"] == str(mounted_root / rel)
    assert observed["snapshot"] == b"accepted operator bytes\n"


def test_prior_dispatch_does_not_block_fresh_mounted_spec_binding(project):
    """A none-to-worktree re-drive relocates accepted input, then replaces old authority."""
    rel = "_bmad-output/implementation-artifacts/accepted-rearm.md"
    ignore_before_commit(project, rel)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    accepted = project.project / rel
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_bytes(b"ignored accepted bytes\n")
    engine, _ = make_engine(project, [], policy=wt_policy(keep_failed=False))
    engine.state.target_branch = "main"
    old_dispatch = str(project.project / ".bmad-loop" / "runs" / "old" / "spec.md")
    old_snapshot = b"prior dispatch bytes\x00"
    task = StoryTask(
        "1-1-a",
        1,
        spec_file=str(accepted),
        dispatched_spec_file=old_dispatch,
        dispatched_spec_snapshot=old_snapshot,
    )
    engine.state.tasks[task.story_key] = task
    observed: dict[str, object] = {}

    def bind_fresh_attempt(current):
        observed["prior_path"] = current.dispatched_spec_file
        observed["prior_snapshot"] = current.dispatched_spec_snapshot
        engine._bind_dispatched_spec_for_attempt(current)
        observed["accepted"] = current.spec_file
        observed["bound"] = current.dispatched_spec_file
        observed["snapshot"] = current.dispatched_spec_snapshot
        observed["root"] = engine.workspace.root
        current.phase = Phase.DEFERRED
        current.defer_reason = "test complete"

    engine._run_isolated(task, bind_fresh_attempt)

    mounted_root = observed["root"]
    assert isinstance(mounted_root, Path)
    assert observed["prior_path"] == old_dispatch
    assert observed["prior_snapshot"] == old_snapshot
    assert observed["accepted"] == rel
    assert observed["bound"] == str(mounted_root / rel)
    assert observed["snapshot"] == b"ignored accepted bytes\n"


def test_relocated_accepted_spec_disappearance_does_not_bind_fallback(project):
    """A normalized absolute spec keeps its exact project-path authority.

    Ablation: resolve the seed through the ordinary relative fallback and omit
    the mounted-file gate; the nested fallback is bound after the accepted source
    disappears between normalization and seeding.
    """
    from bmad_loop.engine import RunPaused

    rel = "_bmad-output/implementation-artifacts/accepted-race.md"
    ignore_before_commit(project, rel)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    accepted = project.project / rel
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_bytes(b"accepted operator bytes\n")
    fallback = project.implementation_artifacts / rel
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_bytes(b"unrelated fallback bytes\n")
    engine, _ = make_engine(project, [], policy=wt_policy(keep_failed=False))
    engine.state.target_branch = "main"
    task = StoryTask("1-1-a", 1, spec_file=str(accepted))
    engine.state.tasks[task.story_key] = task
    real_open = engine._worktree_flow._open_unit_workspace

    def open_then_remove_source(*args, **kwargs):
        unit = real_open(*args, **kwargs)
        accepted.unlink()
        return unit

    engine._worktree_flow._open_unit_workspace = open_then_remove_source
    drove: list[bool] = []

    with pytest.raises(RunPaused, match="accepted spec.*disappeared"):
        engine._run_isolated(task, lambda _task: drove.append(True))

    assert drove == []
    assert task.phase == Phase.ESCALATED


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_missing_upstream_skill_seed_escalates_before_dispatch_and_records_mount(project, tmp_path):
    """A shared install outside the repo passes main's through-link resolution but
    cannot be copied into the worktree. The flow pauses before invoking the adapter,
    with the mount journaled early enough for the operator to inspect it."""
    tree = ".claude/skills"
    ignore_before_commit(project, ".claude/")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    shared_skills = install_build_auto_skill(tmp_path / "shared", tree)
    linked_skill = project.project / tree / DEV_PRIMITIVE_NEW
    linked_skill.parent.mkdir(parents=True)
    linked_skill.symlink_to(shared_skills / DEV_PRIMITIVE_NEW, target_is_directory=True)

    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)

    summary = engine.run()

    assert summary.paused and adapter.sessions == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert f"{tree}/{DEV_PRIMITIVE_NEW}" in (engine.state.paused_reason or "")
    entries = engine.journal.entries()
    kinds = [entry["kind"] for entry in entries]
    assert kinds.index("worktree-opened") < kinds.index("story-escalated")
    opened = next(entry for entry in entries if entry["kind"] == "worktree-opened")
    assert opened["path"] == task.worktree_path
    assert Path(task.worktree_path).is_dir(), "an escalated worktree stays mounted"


def _install_short_renderer_case(project, tmp_path, *, renderer_stub):
    """Install a complete primitive beside renderer scripts the seed cannot follow."""
    tree = ".claude/skills"
    ignore_before_commit(project, ".claude/", f"{BMAD_SCRIPTS_SEED_REL}", CENTRAL_CONFIG_REL)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    install_build_auto_skill(project.project, tree, renderer_stub=renderer_stub)
    shared_scripts = tmp_path / "shared-scripts"
    shared_scripts.mkdir()
    (shared_scripts / "render_skill.py").write_text("import config_utils\n", encoding="utf-8")
    (shared_scripts / "config_utils.py").write_text("# config\n", encoding="utf-8")
    scripts_link = project.project / BMAD_SCRIPTS_SEED_REL
    scripts_link.parent.mkdir(parents=True, exist_ok=True)
    scripts_link.symlink_to(shared_scripts, target_is_directory=True)
    central = project.project / CENTRAL_CONFIG_REL
    central.write_text("[core]\n", encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_short_renderer_seed_escalates_in_worktree_flow(project, tmp_path):
    _install_short_renderer_case(project, tmp_path, renderer_stub=True)
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)

    summary = engine.run()

    assert summary.paused and adapter.sessions == []
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    assert BMAD_SCRIPTS_SEED_REL in (engine.state.paused_reason or "")
    assert Path(task.worktree_path).is_dir()
    assert "worktree-opened" in journal_kinds(engine)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_short_renderer_seed_does_not_escalate_an_inline_skill(project, tmp_path):
    """The exact consumer-side era conjunct: a pre-#2601 inline SKILL.md never
    reads the renderer surface, even when the provisioning predicate emits a sentinel."""
    _install_short_renderer_case(project, tmp_path, renderer_stub=False)
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    attach_profile(adapter)

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert "story-escalated" not in journal_kinds(engine)
    skipped = [
        entry for entry in engine.journal.entries() if entry["kind"] == "worktree-seed-skipped"
    ]
    assert skipped and BMAD_SCRIPTS_SEED_REL in skipped[0]["entries"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_undelivered_arbitrary_seed_is_journaled_never_escalated(project, tmp_path):
    ignore_before_commit(project, ".mcp.json")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    shared = tmp_path / "shared-mcp.json"
    shared.write_text("{}\n", encoding="utf-8")
    (project.project / ".mcp.json").symlink_to(shared)
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(worktree_seed=(".mcp.json",)),
    )

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    dropped = [
        entry for entry in engine.journal.entries() if entry["kind"] == "worktree-seed-dropped"
    ]
    assert len(dropped) == 1 and dropped[0]["entries"] == [".mcp.json"]
    assert "story-escalated" not in journal_kinds(engine)


def test_undelivered_module_skill_is_journaled_never_escalated(project):
    """#464 — a wheel-bundled MODULE_SKILL whose content never reached the unit
    worktree is journaled under its own kind, and the run still completes.

    The obstruction is the real copier's, not a mock's: a plain FILE tracked at the
    skill's destination rel is carried into the checkout by `git worktree add`, and
    `_copy_traversable`'s no-clobber then prunes the whole subtree at its root (a
    non-directory squatting a directory target wins, and mkdir must never replace
    it). So the copy loop lands nothing and the real predicate reports it under the
    real engine.

    `attach_profile` is REQUIRED, not decoration: MockAdapter deliberately carries
    no profile, `worktree_profiles` then yields no skill trees at all, and the
    MODULE_SKILLS copy loop and this predicate never run — the test would pass
    while asserting nothing.

    Ablation: delete the `module_skills_seed_undelivered` call (or its journal
    append) in `WorktreeFlow.run_isolated` and the entry assertion fails; route its
    result into `escalate_unit` instead and the `summary.done == 1` assertion
    fails. Both halves of "journaled, never escalated" are load-bearing."""
    tree = ".claude/skills"
    squatted = "bmad-loop-sweep"
    assert squatted in MODULE_SKILLS  # the precondition that makes this bite
    squatter = project.project / tree / squatted
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("a file where the wheel has a directory\n", encoding="utf-8")
    # NOT gitignored and committed by `git add -A`: only a TRACKED squatter rides
    # the checkout into the worktree, where the copier meets it.
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(),
    )
    attach_profile(adapter)

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    dropped = [
        entry
        for entry in engine.journal.entries()
        if entry["kind"] == "worktree-module-skills-dropped"
    ]
    assert len(dropped) == 1 and dropped[0]["entries"] == [f"{tree}/{squatted}"]
    assert "story-escalated" not in journal_kinds(engine)
    # Provenance stays observable: the wheel's own skills report under their own
    # kind, never folded into the arbitrary-seed one.
    assert "worktree-seed-dropped" not in journal_kinds(engine)


def test_hook_config_is_seeded_for_every_non_hookless_profile(project, monkeypatch):
    """#471 — the seed list and the shield list were built from two unreconciled
    sources, and `hooks.config_path` was only ever in the SHIELD one. Whether a
    profile's hook config got seeded therefore depended on that profile happening to
    name the path twice: claude's `seed_files` carries `.claude/settings.json`, which
    is also its `config_path`, so claude worked by coincidence; codex's carries
    `.codex/config.toml` and NOT `.codex/hooks.json`, so a codex stage ran without the
    project's own hook config.

    ⚠️ THE FIXTURE MUST BE CODEX, and this test ablated GREEN when it was written with
    claude — claude's `config_path` is already one of its `seed_files`, so the new
    derivation is a no-op there and the test could not tell the fix from the bug. The
    coincidence #471 reports is the same thing that makes claude useless as a fixture.

    Pinned on the resolved `config_path` rather than on a literal path, because the
    point of deriving it is that a future profile cannot regress the same way.

    Ablation: drop the `seeds.append(profile.hooks.config_path)` arm and the gitignored
    hook config is absent from the worktree's seed set."""
    from bmad_loop.adapters.profile import get_profile

    codex = get_profile("codex")
    hook_rel = codex.hooks.config_path
    assert not codex.hookless and hook_rel
    assert hook_rel not in codex.seed_files  # the precondition that makes this bite
    ignore_before_commit(project, hook_rel)
    (project.project / hook_rel).parent.mkdir(parents=True, exist_ok=True)
    (project.project / hook_rel).write_text('{"marker": "from-the-main-repo"}\n', encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    seen: list[list[str]] = []
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(),
    )
    # `worktree_profiles` reads `adapter.profile` and the mock has none, so the seed
    # list would be empty for reasons unrelated to this behavior. Give it the real
    # codex profile: the derivation under test is per-profile, so a profile is the
    # fixture, not a mock of one.
    monkeypatch.setattr(adapter, "profile", codex, raising=False)
    real = worktree_flow.provision_worktree

    def spy(worktree, profiles, repo_root, **kwargs):
        seen.append(list(kwargs.get("seed_files") or ()))
        return real(worktree, profiles, repo_root, **kwargs)

    monkeypatch.setattr(worktree_flow, "provision_worktree", spy)
    summary = engine.run()

    assert summary.done == 1
    assert seen and all(hook_rel in seed_list for seed_list in seen)


@pytest.mark.parametrize("merge_strategy", ["merge", "ff"])
def test_worktree_parked_unit_merges_like_a_done_one(project, merge_strategy):
    """`integrate_unit` branches on DONE-vs-everything-else, and a park is the one
    non-DONE terminal that CARRIES A COMMIT. Left on the else arm the unit is torn
    down as failed and finished work is stranded on a deleted branch — over an
    obligation that lives outside the repo entirely.

    The `ff` leg is the #356 acceptance regression guard: the committed park
    record must never cost a fast-forward merge-back — the failure every
    commit-something-on-the-target sketch died on, and the reason the record is
    written inside the unit's own commit window instead.

    Ablation: narrow the merge test back to `== Phase.DONE` and this fails with
    the story's change absent from the target branch. For the record's placement,
    root `_write_park_record` at `self.paths.project` instead of the workspace
    and both legs fail on the ls-tree assertion — the record sits untracked at
    the target instead of riding the merge."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    actions = ["publish the _acme-challenge TXT record"]
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=actions
            )
        ],
        policy=wt_policy(merge_strategy=merge_strategy),
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.AWAITING_OPERATOR and summary.awaiting_operator == 1
    # the merge happened: the unit's work is on the target branch, not stranded
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds and "story-awaiting-operator" in kinds
    assert "unit-closed" not in kinds  # the failed-unit teardown arm never ran
    # the park record rode the unit's own commit through the merge (#356): it is
    # tracked at the target root — written into the WORKTREE, not the main root,
    # where it would have sat untracked beside the merge instead of inside it —
    # and `confirm` can resolve the story from the project alone
    assert ".bmad-loop/operator/1-1-a.json" in git(
        project.project, "ls-tree", "-r", "--name-only", "HEAD"
    )
    from bmad_loop import operatoractions

    (story,) = operatoractions.resolve(project.project, project)
    assert story.confirmable, story.drift()
    assert story.commit  # derived from the record's history on the target branch


def test_a_parked_story_confirms_on_a_fresh_clone(project, tmp_path):
    """The #356 acceptance criterion end to end: a story parks under worktree
    isolation, its record rides the unit's commit through the merge-back, and a
    clone that never ran the orchestrator — no run state, no journal, nothing
    machine-local — lists and confirms it from the committed files alone.

    Ablation: delete the `_write_park_record` call in `_finalize_commit_phase`
    and this fails at the confirm — the clone sees a parked board it cannot
    resolve a spec for."""
    from conftest import install_bmad_config

    from bmad_loop import bmadconfig, cli, operatoractions, sprintstatus

    install_bmad_config(project)  # `confirm` resolves the clone's paths from it
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    actions = ["publish the _acme-challenge TXT record"]
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project, "1-1-a", final_status="awaiting-operator", operator_actions=actions
            )
        ],
    )
    summary = engine.run()
    assert summary.awaiting_operator == 1

    clone = tmp_path / "fresh-clone"
    git(tmp_path, "clone", "-q", str(project.project), str(clone))
    # a clone carries no local identity; the confirm commit needs one
    git(clone, "config", "user.email", "operator@test")
    git(clone, "config", "user.name", "operator")

    assert cli.main(["confirm", "--project", str(clone), "1-1-a", "--yes"]) == 0
    clone_paths = bmadconfig.load_paths(clone)
    assert sprintstatus.story_status(clone_paths.sprint_status, "1-1-a") == "done"
    assert "confirm 1-1-a" in git(clone, "log", "-1", "--format=%s")
    assert operatoractions.load(clone) == {}  # the record's deletion rode the commit
    assert worktree_clean(clone)


def test_worktree_run_dir_is_outside_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    opened = []
    orig = Journal.append

    def spy(self, kind, **kw):
        if kind == "worktree-opened":
            opened.append(kw["path"])
        return orig(self, kind, **kw)

    Journal.append = spy
    try:
        engine.run()
    finally:
        Journal.append = orig

    assert opened, "expected a worktree-opened event"
    wt = opened[0]
    # run state lives in the main repo, never inside the worktree
    assert str(engine.run_dir.resolve()).startswith(str(project.project.resolve()))
    assert not str(engine.run_dir.resolve()).startswith(str(wt))


def test_worktree_multiple_stories_serialize_onto_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
            wt_dev_effect(project, "1-2-b"),
            wt_review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 2
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "change for 1-2-b" in src
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


# ----------------------------------------------------------------- branch naming


def test_branch_per_story_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="story", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "bmad-loop/test-run/1-1-a"
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_branch_per_run_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="run", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "bmad-loop/test-run"
    assert branch_exists(project.project, "bmad-loop/test-run")


def test_dirty_unit_key_branch_is_created_by_real_git(project):
    """#102: a unit key carrying ref-illegal sequences reached `git branch` raw and
    blew up at worktree-mount time. `unit_branch_name` now ref-sanitizes both
    segments, so real git accepts the name — while the worktree dir (safe_segment)
    and the branch (safe_ref_segment) are each sanitized on their own alphabet."""
    from bmad_loop.workspace import open_unit_workspace, unit_branch_name

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    key = "story/1:2..3@{now}.lock"
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = open_unit_workspace(project.project, project, "test-run", key, "main", "story", run_dir)

    assert unit.branch == unit_branch_name("test-run", key, "story")
    assert unit.branch.startswith("bmad-loop/test-run/story_1_2__3_{now}.lock-")
    assert branch_exists(project.project, unit.branch)  # real git accepted the name
    assert unit.path.is_dir() and unit.path.name != key  # dir sanitized separately


# ----------------------------------------------------------------- merge strategies


def test_worktree_squash_merge_linear_history(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="squash"),
    )
    summary = engine.run()
    assert summary.done == 1
    assert git(project.project, "log", "--oneline", "--merges") == ""  # squash → linear


# ----------------------------------------------------------------- failure preservation


def _defer_script(project, key):
    """Dev succeeds, then review never converges → plateau defer. Consumers must
    pin ``limits=LimitsPolicy(max_followup_reviews=99)`` so the default damping cap
    (1) doesn't force-converge the second review pass — this script tests the
    exhaustion/defer plateau, not damping."""
    return [wt_dev_effect(project, key)] + [
        wt_review_effect(project, key, clean=False, patched=1) for _ in range(3)
    ]


# damping pinned high so _defer_script's 3 non-clean rounds reach the exhaustion
# plateau instead of force-converging at the cap
_NO_DAMP = LimitsPolicy(max_followup_reviews=99)


def test_worktree_defer_keeps_failed_unit(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, _defer_script(project, "1-1-a"), policy=wt_policy(limits=_NO_DAMP)
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    # the failed unit's diff is preserved for forensics
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file()
    assert "change for 1-1-a" in patch.read_text()
    # keep_failed default → worktree + branch remain mounted for inspection
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    listed = [p.resolve() for p in worktree_list(project.project)]
    assert project.project.resolve() in listed and len(listed) == 2
    # the main repo is untouched by the failed unit
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    assert worktree_clean(project.project)
    # #333: this script never rolls back (dev succeeds; the reviews plateau), so
    # nothing was parked and the notice points at the kept branch alone. Pin that
    # premise — a bare `is None` would also pass if isolation simply never parked,
    # which is false: see test_isolated_defer_names_the_earlier_rolled_back_attempt.
    assert "rollback-auto" not in journal_kinds(engine)
    assert task.preserve_ref is None
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    assert "failed work kept on branch `bmad-loop/test-run/1-1-a`" in attention


def test_worktree_defer_without_keep_drops_worktree_but_saves_patch(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        _defer_script(project, "1-1-a"),
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file() and "change for 1-1-a" in patch.read_text()
    # not kept → worktree removed, branch deleted
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    # #333: the branch is gone, so the notification must not name it — the patch
    # in the run dir is the only surviving artifact.
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention and "kept on branch" not in attention


_HARVEST_CARRY = {
    "summary": "Retry loop has no ceiling",
    "evidence": "the backoff doubles forever with no cap",
    "location": "src/retry.py:88",
    "severity": "medium",
}

_HARVEST_CARRY_LATER = {
    "summary": "Timeout path drops the cancellation reason",
    "evidence": "the timeout handler replaces the original exception",
    "location": "src/timeout.py:41",
    "severity": "high",
}


def _harvest_record(finding=None):
    finding = finding or _HARVEST_CARRY
    return {
        "origin": "spec-deferred abc123",
        "title": finding["summary"],
        "reason": finding["evidence"],
        "location": finding["location"],
        "severity": finding["severity"],
        "source_spec": "spec-1-1-a.md",
    }


def _main_harvest_entries(project):
    from bmad_loop import deferredwork

    text = project.deferred_work.read_text(encoding="utf-8")
    return deferredwork.parse_ledger(text)


def _harvest_carry_events(engine):
    return [entry for entry in engine.journal.entries() if entry["kind"] == "harvest-carried"]


def test_deferred_isolated_unit_carries_harvest_before_terminal_save(project):
    """A dropped failed worktree cannot be the only durable home of its finding."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a", deferred=[_HARVEST_CARRY])] + [
        wt_review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)
    ]
    engine, _ = make_engine(
        project,
        script,
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    terminal_saves: list[bool] = []
    real_save = engine._save

    def observe_terminal_save() -> None:
        task = engine.state.tasks.get("1-1-a")
        if task is not None and task.phase == Phase.DEFERRED:
            terminal_saves.append(project.deferred_work.is_file())
        real_save()

    engine._save = observe_terminal_save
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused and not summary.crashed
    assert terminal_saves and terminal_saves[0] is True
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [path.resolve() for path in worktree_list(project.project)] == [
        project.project.resolve()
    ]
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_deferred_carry_commit_failure_resumes_before_terminal_integration(project, monkeypatch):
    """A carry fault cannot strand a terminal task before defer teardown."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a", deferred=[_HARVEST_CARRY])] + [
        wt_review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)
    ]
    engine, _ = make_engine(
        project,
        script,
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    real_commit = verify.commit_paths
    failures = 0

    def commit_fails_once(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise verify.GitError("commit hook rejects deferred carry")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(verify, "commit_paths", commit_fails_once)

    assert engine.run().crashed

    failed = load_state(engine.run_dir).tasks["1-1-a"]
    assert failed.phase == Phase.REVIEW_VERIFY
    assert failed.harvest_carry_commit_pending is True
    assert Path(failed.worktree_path).is_dir()
    assert "story-deferred" not in journal_kinds(engine)
    assert "unit-closed" not in journal_kinds(engine)

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    restored = load_state(resumed.run_dir).tasks["1-1-a"]
    assert summary.deferred == 1 and not summary.crashed and not summary.paused
    assert restored.phase == Phase.DEFERRED
    assert restored.harvest_carry_commit_pending is False
    assert adapter.sessions == []
    assert "story-deferred" in journal_kinds(resumed)
    assert "unit-closed" in journal_kinds(resumed)
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [path.resolve() for path in worktree_list(project.project)] == [
        project.project.resolve()
    ]
    assert not branch_exists(project.project, failed.branch)
    assert worktree_clean(project.project)


def test_dev_defer_carry_failure_resumes_the_rejected_decision(project, monkeypatch):
    """A carry fault cannot turn a rejected dev result into a verified spec."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                write_src=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
        policy=wt_policy(keep_failed=False, limits=LimitsPolicy(max_dev_attempts=1)),
    )
    real_commit = verify.commit_paths
    failures = 0

    def commit_fails_once(*args, **kwargs):
        nonlocal failures
        message = str(args[1]) if len(args) > 1 else str(kwargs.get("message", ""))
        if message.startswith("chore(deferred-work): carry harvested findings") and failures == 0:
            failures += 1
            raise verify.GitError("commit hook rejects deferred carry")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(verify, "commit_paths", commit_fails_once)

    assert engine.run().crashed

    failed = load_state(engine.run_dir).tasks["1-1-a"]
    assert failed.phase == Phase.DEV_VERIFY
    assert failed.spec_file
    assert failed.harvest_carry_commit_pending is True
    assert "no changes" in (failed.defer_reason or "")
    assert Path(failed.worktree_path).is_dir()
    assert "story-deferred" not in journal_kinds(engine)
    assert "unit-closed" not in journal_kinds(engine)

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    restored = load_state(resumed.run_dir).tasks["1-1-a"]
    assert summary.deferred == 1 and not summary.done
    assert not summary.crashed and not summary.paused
    assert restored.phase == Phase.DEFERRED
    assert restored.harvest_carry_commit_pending is False
    assert adapter.sessions == []
    decisions = [event for event in resumed.journal.entries() if event["kind"] == "dev-decision"]
    assert len(decisions) == 1 and decisions[0]["action"] == "defer"
    assert "resume-defer" in journal_kinds(resumed)
    assert "resume-review" not in journal_kinds(resumed)
    assert "story-deferred" in journal_kinds(resumed)
    assert "unit-closed" in journal_kinds(resumed)
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "ready-for-dev"
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [path.resolve() for path in worktree_list(project.project)] == [
        project.project.resolve()
    ]
    assert not branch_exists(project.project, failed.branch)
    assert worktree_clean(project.project)


def test_done_isolated_unit_carries_a_gitignored_harvest_after_merge(project):
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert summary.done == 1 and task.phase == Phase.DONE
    assert task.isolated_ledger_carried
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [event["dw_ids"] for event in _harvest_carry_events(engine)] == [["DW-1"]]
    uncommitted = [
        entry for entry in engine.journal.entries() if entry["kind"] == "harvest-carry-uncommitted"
    ]
    assert len(uncommitted) == 1 and uncommitted[0]["dw_ids"] == ["DW-1"]
    assert [path.resolve() for path in worktree_list(project.project)] == [
        project.project.resolve()
    ]


def test_done_isolated_unit_carries_gitignored_harvests_from_every_successful_pass(project):
    """A later review payload cannot erase a retained dev finding before carry."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=[_HARVEST_CARRY]),
            wt_review_effect(
                project,
                "1-1-a",
                clean=True,
                deferred=[_HARVEST_CARRY_LATER],
            ),
        ],
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    expected = [_HARVEST_CARRY["summary"], _HARVEST_CARRY_LATER["summary"]]
    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert [item["title"] for item in task.harvested_deferrals] == expected
    assert [entry.title for entry in _main_harvest_entries(project)] == expected
    assert [event["dw_ids"] for event in _harvest_carry_events(engine)] == [["DW-1", "DW-2"]]


def test_carry_harvest_dedupe_stays_status_agnostic(project):
    """A finding the sweep has since CLOSED must not be re-filed by the carry.

    The carry's own pre-scan is the whole on-disk guard, and it has to stay
    status-agnostic, because the batch writer's idempotence scan deliberately is
    NOT: a closed entry with the same marker does not suppress an append there,
    the work having come back. That is right for a fresh defer and wrong for this
    caller, which is re-filing rows an isolated unit already filed once — a row
    the sweep resolved between the unit's write and the merge would come back
    from the dead as a second open entry, and nothing downstream would ever
    reconcile the twins.

    The mid-loop `parse_ledger` re-read this replaced kept the SAME-call dedupe
    status-agnostic too; that half needs no guard here, since every row the batch
    appends is open and its evolving scan therefore sees it.

    Ablation: delete the `if any(... field_line_present ...): continue` pre-filter
    and lean on the batch's open-only scan — a second row appears and this reddens
    on the id list."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    record = _harvest_record()
    dw_id = deferredwork.append_entry(
        project.deferred_work,
        title=record["title"],
        origin=record["origin"],
        location=record["location"],
        source_spec=record["source_spec"],
        reason=record["reason"],
        severity=record["severity"],
    )
    assert dw_id is not None
    assert deferredwork.mark_done(project.deferred_work, dw_id, "2026-06-01", "fixed upstream")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(story_key="1-1-a", epic=1, harvested_deferrals=[record])
    engine.state.tasks[task.story_key] = task

    engine._carry_harvested_deferrals(task)

    entries = _main_harvest_entries(project)
    assert [entry.id for entry in entries] == [dw_id]  # the done twin, and nothing beside it
    assert not entries[0].open
    (carried,) = _harvest_carry_events(engine)
    assert carried["dw_ids"] == []
    assert task.harvest_carry_commit_pending is False  # nothing novel, so no latch


def _in_place_policy(*, limits: LimitsPolicy | None = None):
    """`wt_policy`'s mirror: the live mode a mid-pause `isolation = "none"` edit
    leaves behind, with everything else identical so the two rows differ in one
    field only. ``limits`` mirrors `wt_policy`'s own knob, for the row that has to
    reach `max_review_cycles` exhaustion instead of the damped force-converge."""
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
        limits=limits if limits is not None else LimitsPolicy(),
    )


def test_defer_under_a_recorded_mount_carries_the_harvest_after_an_isolation_flip(
    project, monkeypatch
):
    """`_defer` routes on the tree in hand, not on live policy alone.

    `_finish_inflight` picks its arms on `mounted = bool(task.worktree_path)` and
    reopens a recorded mount REGARDLESS of live policy — an accepted continuation owns
    the verified work in that tree. So a run whose `scm.isolation` was edited
    `"worktree" -> "none"` while it was paused re-enters this decision with the
    workspace swapped onto a mount while `self._isolated` answers False. Gated on
    policy alone, the in-place arm then reset the MAIN repo and skipped
    `_carry_harvested_deferrals` entirely, and `_integrate_unit` deleted the mount on
    the way out: the unit's harvested findings had no durable home left, and a ledger
    row nothing will re-file is invisible to every later sweep.

    `rolled` is the positive control and the discriminator — this row would also pass
    on an engine that simply did nothing, so it pins WHICH arm ran, not merely that the
    harvest survived.

    Ablation: restore the bare `if self._isolated:` gate and this reddens on an empty
    ledger, with the main-repo rollback recorded instead."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_in_place_policy())
    assert engine._isolated is False  # MEASURED: live policy really says in place
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,  # the phase the budget-exhausted defer fires from
        worktree_path=str(project.project / ".bmad-loop" / "runs" / "test-run" / "wt" / "1-1-a"),
        baseline_commit=rev_parse_head(project.project),
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    rolled: list[str] = []
    monkeypatch.setattr(engine, "_rollback_or_pause", lambda t: rolled.append(t.story_key))

    engine._defer(task, "review did not converge within budget")

    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [event["dw_ids"] for event in _harvest_carry_events(engine)] == [["DW-1"]]
    assert rolled == []  # no reset into the main repo under a live mount
    assert task.phase == Phase.DEFERRED


def test_defer_recovery_note_under_a_recorded_mount_names_the_branch_not_a_merge(project):
    """The notice `_record_defer` emits two lines after `_defer` picked its arm must
    pick the SAME arm. Selected on live policy alone, the flipped-policy defer above
    printed the in-place `git -C <mount> merge --ff-only <ref>` — a command aimed at
    a directory `_integrate_unit` deletes on the way out with `keep_failed` off, and
    with it on, a notice that never names the branch holding the latest failed work.
    An earlier in-worktree dev-retry rollback parks `preserve_ref` on the shared
    refs (#333: the ref is not isolation-scoped), so both facts are live at once.

    Ablation: restore the bare `if self._isolated:` gate in `_defer_recovery_note`
    and this reddens on the merge line, then on the missing branch."""
    engine, _ = make_engine(project, [], policy=_in_place_policy())
    assert engine._isolated is False  # MEASURED: live policy really says in place
    assert engine.policy.scm.keep_failed is True
    ref = "attempt-preserve/test-run-0badc0de"
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        worktree_path=str(project.project / ".bmad-loop" / "runs" / "test-run" / "wt" / "1-1-a"),
        branch="bmad-loop/1-1-a",
        preserve_ref=ref,
    )

    note = engine._defer_recovery_note(task)

    assert "merge --ff-only" not in note
    assert "failed work kept on branch `bmad-loop/1-1-a`" in note
    assert f"an earlier rolled-back attempt is parked at `{ref}`" in note


def test_defer_with_no_recorded_mount_still_takes_the_in_place_arm(project, monkeypatch):
    """The other half of the widened gate. `or task.worktree_path` must not swallow the
    ordinary in-place defer, whose whole job is the rollback the isolated arm skips —
    an in-place task carries `""`, and nothing else about this row differs from its
    sibling above.

    Ablation: widen the gate to an unconditional `True` (or drop the `worktree_path`
    truthiness test so `Path("")` logic creeps back in) and this reddens on an
    un-rolled-back tree and a harvest carried where none should be."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=_in_place_policy())
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,
        baseline_commit=rev_parse_head(project.project),
        harvested_deferrals=[_harvest_record()],
    )
    assert task.worktree_path == ""  # MEASURED: the discriminator is the empty one
    engine.state.tasks[task.story_key] = task
    rolled: list[str] = []
    monkeypatch.setattr(engine, "_rollback_or_pause", lambda t: rolled.append(t.story_key))

    engine._defer(task, "review did not converge within budget")

    assert rolled == ["1-1-a"]  # the in-place reset DID run
    assert _harvest_carry_events(engine) == []  # and no unit ledger was carried
    assert task.phase == Phase.DEFERRED


def test_defer_under_live_isolation_with_no_mount_yet_keeps_the_isolated_arm(project, monkeypatch):
    """Isolation can be LIVE with no mount recorded — a defer reached before
    `run_isolated` stores the path. That shape must keep the isolated arm rather than
    fall through to a main-repo reset, which is why the gate is
    `self._isolated OR worktree_path` and not the path alone.

    Ablation: narrow the gate to `if task.worktree_path:` and this reddens on a
    rollback that should never have run."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])  # wt_policy: isolation IS live
    assert engine._isolated is True
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,
        baseline_commit=rev_parse_head(project.project),
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    rolled: list[str] = []
    monkeypatch.setattr(engine, "_rollback_or_pause", lambda t: rolled.append(t.story_key))

    engine._defer(task, "review did not converge within budget")

    assert rolled == []
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]


def test_review_timeout_salvage_refused_under_a_recorded_mount_after_an_isolation_flip(
    project,
):
    """`_salvage_review_timeout` routes on the tree in hand, not on live policy alone.

    The third member of the pair `_defer` and `_run_story` already select on. An
    accepted continuation reopens the recorded mount REGARDLESS of live policy —
    `_finish_inflight` swaps `self.workspace` onto it, and `self._isolated` is a
    read-only property over LIVE policy with no setter anywhere — so a run whose
    `scm.isolation` was edited `"worktree" -> "none"` while it was paused reaches this
    decision mounted with `self._isolated` False. Gated on policy alone, salvage then
    committed the mounted work for `_integrate_unit` to merge out: a timed-out review
    landing DONE-and-merged, where the identical work under unchanged policy defers
    with the unit's worktree and diff kept for review.

    The bytes assertion is the discriminator, not the return value. The `in-review`
    arm performs REPAIR WRITES the mounted path never performs — `reset_spec_status`
    to `done` and `strip_auto_run_result` — and they fire before any later verify gate
    could turn the answer back to False on its own; a return-value-only row would pass
    for that unrelated reason.

    Ablation: restore `if self._isolated or not task.spec_file:` and this reddens on a
    spec rewritten to `done` with its terminal marker stripped."""
    write_sprint(project, {"1-1-a": "done"})
    sp = project.implementation_artifacts / "spec-1-1-a.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    # `in-review` is the mid-review interrupt the salvage arm repairs forward; the
    # terminal marker is the second thing it strips.
    write_spec(sp, "in-review", rev_parse_head(project.project), prose_status="done")
    before = sp.read_text()
    engine, _ = make_engine(project, [], policy=_in_place_policy())
    assert engine._isolated is False  # MEASURED: live policy really says in place
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.REVIEW_VERIFY,  # the phase the review loop calls salvage from
        spec_file=str(sp),
        worktree_path=str(project.project / ".bmad-loop" / "runs" / "test-run" / "wt" / "1-1-a"),
    )

    assert engine._salvage_review_timeout(task, SessionResult(status="timeout")) is False
    assert sp.read_text() == before  # no repair write into a story the mount owns


def test_budget_exhausted_rescue_defers_under_a_recorded_mount_after_an_isolation_flip(
    project,
):
    """The budget-exhaustion rescue picks the same arm as the defer it replaces.

    `test_budget_exhausted_finalized_work_commits`'s harness, with one field added: the
    task carries the attempt's mount while live policy answers "in place" — exactly
    what `_finish_inflight` leaves after an `isolation` flip across a resume, since it
    sets only `self.workspace` and `self._isolated` keeps reading live policy. Selected
    on policy alone, the rescue committed a story that never converged; `_commit` lands
    in the mount and `_integrate_unit` merges that unit branch out to the target, so
    the outcome inverts — DONE-and-merged instead of DEFERRED with the unit's worktree
    and patch preserved.

    The gate is inline in `_review_and_commit`, so a full sandbox run is the lowest
    layer that reaches it: the loop has to actually exhaust `max_review_cycles` with a
    finalized, verify-green tree still recommending a follow-up (`max_followup_reviews`
    pinned high, or the damping converges the story before exhaustion and this row
    never reaches the branch under test).

    `review_cycle == 3` is the premise control — without it a story that deferred for
    any earlier reason would satisfy the outcome assertions.

    Ablation: restore `if refileable_followup and not self._isolated:` and this reddens
    on a DONE task with a fresh commit at HEAD."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    mount = project.project / ".bmad-loop" / "runs" / "test-run" / "wt" / "1-1-a"
    box: list[Engine] = []
    dev = wt_dev_effect(project, "1-1-a")

    def dev_then_record_the_mount(spec):
        # The reopened mount, recorded on the task, is all `_finish_inflight` leaves
        # behind for the review loop to read; recording it after dispatch keeps
        # `_run_story` out of the row so the gate under test is the only selector.
        result = dev(spec)
        box[0].state.tasks["1-1-a"].worktree_path = str(mount)
        return result

    engine, _ = make_engine(
        project,
        [dev_then_record_the_mount]
        + [wt_review_effect(project, "1-1-a", clean=False) for _ in range(3)],
        policy=_in_place_policy(limits=LimitsPolicy(max_followup_reviews=99)),
    )
    box.append(engine)
    assert engine._isolated is False  # MEASURED: live policy really says in place

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 3  # MEASURED: the budget really was exhausted
    assert task.worktree_path == str(mount)  # and the mount really was in hand
    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    assert task.phase == Phase.DEFERRED
    assert not task.commit_sha
    assert rev_parse_head(project.project) == head_before  # nothing was committed
    assert "review-budget-committed" not in journal_kinds(engine)


def test_tracked_harvest_carry_commit_failure_propagates(project, monkeypatch):
    """A tracked ledger persistence fault cannot be reported as a completed carry."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task

    def commit_fails(*args, **kwargs):
        raise verify.GitError("index lock blocks tracked ledger carry")

    monkeypatch.setattr(verify, "commit_paths", commit_fails)

    with pytest.raises(verify.GitError, match="index lock"):
        engine._carry_harvested_deferrals(task)

    assert "harvest-carried" not in journal_kinds(engine)
    assert "harvest-carry-uncommitted" not in journal_kinds(engine)
    assert load_state(engine.run_dir).tasks[task.story_key].harvest_carry_commit_pending


def test_uncertain_harvest_ledger_keeps_its_pending_commit(project, monkeypatch):
    """Resolution uncertainty cannot turn a tracked carry into advisory success."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    record = _harvest_record()
    deferredwork.append_entry(
        project.deferred_work,
        title=record["title"],
        origin=record["origin"],
        location=record["location"],
        source_spec=record["source_spec"],
        reason=record["reason"],
        severity=record["severity"],
    )
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[record],
        harvest_carry_commit_pending=True,
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    refuse_to_resolve(monkeypatch, project.deferred_work)

    with pytest.raises(verify.GitError, match="no exact commit operand remains"):
        engine._carry_harvested_deferrals(task)

    assert load_state(engine.run_dir).tasks[task.story_key].harvest_carry_commit_pending
    assert "harvest-carried" not in journal_kinds(engine)
    assert "harvest-carry-uncommitted" not in journal_kinds(engine)


def test_untracked_nonignored_harvest_carry_commit_failure_propagates(project, monkeypatch):
    """An ordinary new ledger is committable, so its git failure is fatal too."""
    engine, _ = make_engine(project, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task

    def commit_fails(*args, **kwargs):
        raise verify.GitError("status fails for untracked ledger carry")

    monkeypatch.setattr(verify, "commit_paths", commit_fails)

    with pytest.raises(verify.GitError, match="untracked ledger"):
        engine._carry_harvested_deferrals(task)

    rel = project.deferred_work.relative_to(project.project).as_posix()
    assert rel in verify.untracked_files(project.project)
    assert load_state(engine.run_dir).tasks[task.story_key].harvest_carry_commit_pending
    assert "harvest-carry-uncommitted" not in journal_kinds(engine)


def test_external_harvest_carry_commit_failure_degrades(project, tmp_path, monkeypatch):
    """A configured external ledger remains an advisory, non-git artifact."""
    external_paths = ProjectPaths(
        project=project.project,
        implementation_artifacts=tmp_path / "external-artifacts",
        planning_artifacts=project.planning_artifacts,
        output_folder=project.output_folder,
        repo_root=project.repo_root,
    )
    engine, _ = make_engine(external_paths, [])
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task

    def commit_fails(*args, **kwargs):
        raise verify.GitError("external ledger cannot be committed")

    monkeypatch.setattr(verify, "commit_paths", commit_fails)

    engine._carry_harvested_deferrals(task)

    restored = load_state(engine.run_dir).tasks[task.story_key]
    assert restored.harvest_carry_commit_pending is False
    assert [entry.title for entry in _main_harvest_entries(external_paths)] == [
        _HARVEST_CARRY["summary"]
    ]
    uncommitted = [
        event for event in engine.journal.entries() if event["kind"] == "harvest-carry-uncommitted"
    ]
    assert len(uncommitted) == 1 and uncommitted[0]["dw_ids"] == ["DW-1"]


def test_host_loss_after_harvest_append_replays_the_pending_commit(project, monkeypatch):
    """The commit intent is durable before the append can mutate the ledger.

    The carry files its whole batch in ONE `append_entries` call (#286/#469), so
    that is where a host loss lands; `append_entry` is no longer on this path and
    patching it would inject nothing."""
    from bmad_loop import deferredwork

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    engine.journal.append(
        "unit-merged",
        story_key=task.story_key,
        branch=task.branch,
        target="main",
    )
    real_append = deferredwork.append_entries

    def append_then_host_dies(*args, **kwargs):
        real_append(*args, **kwargs)
        raise SystemExit("host died after ledger append")

    monkeypatch.setattr(deferredwork, "append_entries", append_then_host_dies)
    with pytest.raises(SystemExit, match="host died"):
        engine._carry_harvested_deferrals(task)

    failed = load_state(engine.run_dir).tasks[task.story_key]
    assert failed.harvest_carry_commit_pending is True
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]

    monkeypatch.setattr(deferredwork, "append_entries", real_append)
    failed_state = load_state(engine.run_dir)
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=failed_state,
    )
    after_story: list[str] = []
    monkeypatch.setattr(
        resumed,
        "_after_story",
        lambda restored_task: after_story.append(restored_task.story_key),
    )
    resumed._replay_unlatched_ledger_carries()

    restored = load_state(resumed.run_dir).tasks[task.story_key]
    assert failed_state.tasks[task.story_key].isolated_ledger_carried is True
    assert restored.harvest_carry_commit_pending is False
    assert restored.isolated_ledger_carried is True
    assert after_story == [task.story_key]
    assert worktree_clean(project.project)
    assert "carry harvested findings from 1-1-a" in git(project.project, "log", "-1", "--format=%s")


@pytest.mark.parametrize("phase", [Phase.DONE, Phase.AWAITING_OPERATOR, Phase.DEFERRED])
def test_tracked_harvest_carry_commit_failure_retries_its_pending_commit(
    project, monkeypatch, phase
):
    """A failed commit remains replayable after provenance dedupes its append."""
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=phase,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    if phase in (Phase.DONE, Phase.AWAITING_OPERATOR):
        engine.journal.append(
            "unit-merged",
            story_key=task.story_key,
            branch=task.branch,
            target="main",
        )
    real_commit = verify.commit_paths

    def commit_fails(*args, **kwargs):
        raise verify.GitError("commit hook rejects tracked carry")

    monkeypatch.setattr(verify, "commit_paths", commit_fails)
    with pytest.raises(verify.GitError, match="commit hook"):
        engine._carry_harvested_deferrals(task)

    failed = load_state(engine.run_dir).tasks[task.story_key]
    assert failed.harvest_carry_commit_pending is True
    assert failed.isolated_ledger_carried is False

    monkeypatch.setattr(verify, "commit_paths", real_commit)
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=load_state(engine.run_dir),
    )
    resumed._replay_unlatched_ledger_carries()

    restored = load_state(resumed.run_dir).tasks[task.story_key]
    assert restored.harvest_carry_commit_pending is False
    assert restored.isolated_ledger_carried is (phase != Phase.DEFERRED)
    assert "resume-ledger-carry" in journal_kinds(resumed)
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert "carry harvested findings from 1-1-a" in git(project.project, "log", "-1", "--format=%s")


def test_unmerged_terminal_unit_does_not_replay_harvest_carry(project):
    """A terminal phase and live directory alone are not durable merge evidence."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task

    engine._replay_unlatched_ledger_carries()

    assert not project.deferred_work.exists()
    assert task.isolated_ledger_carried is False
    assert "resume-ledger-carry" not in journal_kinds(engine)


def test_started_merge_replay_failure_does_not_carry_harvest(project, monkeypatch):
    """Write-ahead merge intent cannot stand in for successful merge proof."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    source = rev_parse_head(unit.path)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(unit.path),
        branch=unit.branch,
        baseline_commit=unit.baseline,
        commit_sha=source,
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    engine.journal.append(
        "unit-merge-started",
        story_key=task.story_key,
        branch=task.branch,
        target="main",
        strategy="merge",
        source=source,
    )

    def replay_fails(*args, **kwargs):
        raise verify.GitError("replayed merge still conflicts")

    monkeypatch.setattr(engine, "_merge_local", replay_fails)

    with pytest.raises(verify.GitError, match="still conflicts"):
        engine._replay_unlatched_ledger_carries()

    assert not project.deferred_work.exists()
    assert task.isolated_ledger_carried is False
    assert "resume-unit-merge" in journal_kinds(engine)
    assert "resume-ledger-carry" not in journal_kinds(engine)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("story_key", "1-2-other"),
        ("branch", "bmad-loop/test-run/other-branch"),
        ("target", "release"),
    ],
)
def test_mismatched_unit_merge_evidence_does_not_replay_harvest_carry(project, field, value):
    """Replay requires the merged story, unit branch, and target to all match."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
        harvested_deferrals=[_harvest_record()],
    )
    engine.state.tasks[task.story_key] = task
    evidence = {
        "story_key": task.story_key,
        "branch": task.branch,
        "target": "main",
    }
    evidence[field] = value
    engine.journal.append("unit-merged", **evidence)

    engine._replay_unlatched_ledger_carries()

    assert not project.deferred_work.exists()
    assert task.isolated_ledger_carried is False
    assert "resume-ledger-carry" not in journal_kinds(engine)


def test_done_isolated_unit_dedupes_a_tracked_closed_harvest_after_merge(project):
    from bmad_loop import deferredwork

    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)

    def close_harvest_then_review(spec):
        wt = project.rebased(spec.cwd)
        assert deferredwork.mark_done(
            wt.deferred_work,
            "DW-1",
            "2026-08-03",
            "fixed before the unit merged",
        )
        return wt_review_effect(project, "1-1-a", clean=True)(spec)

    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a", deferred=[_HARVEST_CARRY]),
            close_harvest_then_review,
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    entries = _main_harvest_entries(project)
    assert len(entries) == 1 and entries[0].title == _HARVEST_CARRY["summary"]
    assert not entries[0].open
    assert [event["dw_ids"] for event in _harvest_carry_events(engine)] == [[]]
    subjects = git(project.project, "log", "--format=%s", f"{head_before}..HEAD").splitlines()
    assert not [
        subject
        for subject in subjects
        if subject.startswith("chore(deferred-work): carry harvested findings")
    ]


def test_awaiting_operator_isolated_unit_carries_a_gitignored_harvest(project):
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                final_status="awaiting-operator",
                followup_review=False,
                operator_actions=["publish the DNS record"],
                deferred=[_HARVEST_CARRY],
            )
        ],
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert summary.awaiting_operator == 1 and task.phase == Phase.AWAITING_OPERATOR
    assert task.isolated_ledger_carried
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert [event["dw_ids"] for event in _harvest_carry_events(engine)] == [["DW-1"]]


@pytest.mark.parametrize(
    ("merge_strategy", "resumed_strategy"),
    [("merge", "ff"), ("ff", "squash"), ("squash", "merge")],
)
def test_host_loss_after_merge_before_evidence_replays_gitignored_harvest(
    project, monkeypatch, merge_strategy, resumed_strategy
):
    """Replay uses durable merge intent even when the live policy changed."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
        policy=wt_policy(merge_strategy=merge_strategy),
    )
    real_append = engine.journal.append

    def host_dies_before_merge_evidence(kind, **fields):
        if kind == "unit-merged":
            raise SystemExit("host died before merge evidence")
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", host_dies_before_merge_evidence)
    with pytest.raises(SystemExit, match="host died before merge evidence"):
        engine.run()

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    landed_head = rev_parse_head(project.project)
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert Path(crashed.worktree_path).is_dir()
    assert not project.deferred_work.exists()
    assert "unit-merged" not in journal_kinds(engine)

    monkeypatch.setattr(engine.journal, "append", real_append)
    replay_collision_refs: list[str] = []
    replay_protected: list[object] = []
    replay_merge_refs: list[str] = []
    replay_strategies: list[str] = []
    real_clean = verify.clean_incoming_collisions
    real_merge = verify.merge_branch

    def record_collision_ref(repo, target, merge_ref, **kwargs):
        replay_collision_refs.append(merge_ref)
        # forward the keywords rather than dropping them: dropping `on_tolerated`
        # would silently disable the journal event on the replay path (#460), and
        # dropping `protected` would silently disable the carry-path guard there
        # (#618). Recorded, not just forwarded, so the assertion below catches both
        # a stub that swallows the keyword and a call site that stops passing it.
        replay_protected.append(kwargs.get("protected"))
        return real_clean(repo, target, merge_ref, **kwargs)

    def record_merge_ref(repo, merge_ref, **kwargs):
        replay_merge_refs.append(merge_ref)
        replay_strategies.append(kwargs["strategy"])
        return real_merge(repo, merge_ref, **kwargs)

    monkeypatch.setattr(verify, "clean_incoming_collisions", record_collision_ref)
    monkeypatch.setattr(verify, "merge_branch", record_merge_ref)
    resumed = Engine(
        paths=project,
        policy=wt_policy(merge_strategy=resumed_strategy),
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=load_state(engine.run_dir),
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert rev_parse_head(project.project) == landed_head
    assert replay_collision_refs == [crashed.commit_sha]
    # The replay reaches `merge_local` by its own route, so the carry-path guard has
    # to be wired inside it rather than at the live-run caller. Pinned as the exact
    # tuple: a `protected=()` that reached here would satisfy "was passed" while
    # guarding nothing. The board alone, not the ledger — this row gitignores the
    # ledger, and an artifact git does not track has no committed baseline for the
    # carry to diverge from, so the wiring omits it (`_carried_artifact_rels`).
    assert replay_protected == [(project.sprint_status.relative_to(project.project).as_posix(),)]
    assert replay_merge_refs == [crashed.commit_sha]
    assert replay_strategies == [merge_strategy]
    assert "unit-merge-started" in journal_kinds(resumed)
    assert "unit-merged" in journal_kinds(resumed)
    assert [
        (entry["strategy"], entry["source"])
        for entry in resumed.journal.entries()
        if entry["kind"] == "resume-unit-merge"
    ] == [(merge_strategy, crashed.commit_sha)]
    assert [
        (entry["strategy"], entry["source"])
        for entry in resumed.journal.entries()
        if entry["kind"] == "unit-merged"
    ] == [(merge_strategy, crashed.commit_sha)]
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


@pytest.mark.parametrize("merge_strategy", ["merge", "ff", "squash"])
def test_merge_replay_rejects_a_unit_branch_advanced_after_recorded_source(
    project, monkeypatch, merge_strategy
):
    """Recovery preserves and refuses commits outside the completed session."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
        policy=wt_policy(merge_strategy=merge_strategy),
    )
    real_append = engine.journal.append

    def host_dies_before_merge_evidence(kind, **fields):
        if kind == "unit-merged":
            raise SystemExit("host died before merge evidence")
        return real_append(kind, **fields)

    monkeypatch.setattr(engine.journal, "append", host_dies_before_merge_evidence)
    with pytest.raises(SystemExit, match="host died before merge evidence"):
        engine.run()

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    landed_head = rev_parse_head(project.project)
    unit_path = Path(crashed.worktree_path)
    (unit_path / "late.txt").write_text("not part of the completed session\n", encoding="utf-8")
    git(unit_path, "add", "late.txt")
    git(unit_path, "commit", "-q", "-m", "late unverified commit")
    advanced_head = rev_parse_head(unit_path)
    assert advanced_head != crashed.commit_sha

    monkeypatch.setattr(engine.journal, "append", real_append)
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=load_state(engine.run_dir),
    )
    summary = resumed.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert rev_parse_head(project.project) == landed_head
    assert not (project.project / "late.txt").exists()
    assert unit_path.is_dir()
    assert branch_exists(project.project, crashed.branch)
    assert rev_parse_head(unit_path) == advanced_head
    assert "unit-merged" not in journal_kinds(resumed)
    assert not project.deferred_work.exists()
    restored = load_state(resumed.run_dir).tasks["1-1-a"]
    assert restored.phase == Phase.ESCALATED
    assert not restored.isolated_ledger_carried


def test_crashed_post_merge_harvest_carry_replays_and_persists_its_latch(project):
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
    )

    def crash_before_carry(_task) -> None:
        raise RuntimeError("host died after merge and teardown")

    # The WorktreeFlow callback must look this method up when invoked, not capture
    # the original bound method at Engine construction time.
    engine._carry_isolated_ledger_writes = crash_before_carry
    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert crashed.harvested_deferrals
    assert not Path(crashed.worktree_path).exists()
    assert not project.deferred_work.exists()

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert adapter.sessions == []
    assert "resume-ledger-carry" in journal_kinds(resumed)
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_crashed_post_merge_harvest_carry_replays_when_teardown_leaves_directory(
    project, monkeypatch
):
    """A stale teardown directory is not evidence that the merge never landed."""
    import bmad_loop.workspace as workspace_mod

    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                followup_review=False,
                deferred=[_HARVEST_CARRY],
            )
        ],
    )
    real_remove = verify.worktree_remove
    real_rmtree = workspace_mod._rmtree_confined

    def teardown_fails(*args, **kwargs):
        raise verify.GitError("worktree teardown blocked")

    def leave_directory(*args, **kwargs):
        return True

    def crash_before_carry(_task) -> None:
        raise RuntimeError("host died after degraded teardown")

    monkeypatch.setattr(verify, "worktree_remove", teardown_fails)
    monkeypatch.setattr(workspace_mod, "_rmtree_confined", leave_directory)
    engine._carry_isolated_ledger_writes = crash_before_carry
    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert Path(crashed.worktree_path).is_dir()
    assert "unit-merged" in journal_kinds(engine)
    assert "worktree-teardown-degraded" in journal_kinds(engine)
    assert not project.deferred_work.exists()

    monkeypatch.setattr(verify, "worktree_remove", real_remove)
    monkeypatch.setattr(workspace_mod, "_rmtree_confined", real_rmtree)
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert adapter.sessions == []
    assert "resume-ledger-carry" in journal_kinds(resumed)
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_worktree_defer_then_next_story_succeeds(project):
    """A deferred (kept) unit must not block the next story's worktree/merge."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script, policy=wt_policy(limits=_NO_DAMP))
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 1
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()


def test_branch_per_run_kept_failure_detaches_so_next_unit_runs(project):
    """branch_per=run shares one branch; keeping a kept-failed unit's worktree
    checked out on it would block every later unit's mount and cascade the whole
    run into never-attempted deferrals. close_unit_workspace detaches the kept
    worktree's HEAD, freeing the shared branch so the next unit gets a genuine
    attempt instead of insta-deferring on a collision (issue #138)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script, policy=wt_policy(branch_per="run", limits=_NO_DAMP))
    summary = engine.run()

    # 1-1-a defers (kept), but 1-2-b actually runs and lands — no collision cascade
    assert summary.deferred == 1 and summary.done == 1 and not summary.paused
    assert "worktree-open-failed" not in journal_kinds(engine)
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE
    assert not engine.state.tasks["1-2-b"].defer_reason
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    # the kept 1-1-a worktree is detached (freeing the shared run branch), while
    # the branch ref itself survives for inspection
    assert branch_exists(project.project, "bmad-loop/test-run")
    kept = [p for p in worktree_list(project.project) if p.resolve() != project.project.resolve()]
    assert len(kept) == 1 and current_branch(kept[0]) == "HEAD"


def test_worktree_followup_damped_commits_and_integrates(project):
    """Damping fires the same in worktree isolation (default cap 1, no _isolated
    guard): a finalized unit whose review keeps recommending a follow-up converges
    after one honored round and the work MERGES into the main repo. No ledger
    entry is filed anywhere — the spent budget is journal-only. Exempting
    isolation would leave isolated runs non-convergent AND deferred (strictly
    worse), which this locks out."""
    from bmad_loop import deferredwork

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a")] + [
        wt_review_effect(project, "1-1-a", clean=False) for _ in range(3)
    ]
    engine, _ = make_engine(project, script)  # default wt_policy() → cap 1
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2 and task.followup_reviews_spent == 1
    # the unit's work merged into the main repo (target branch checkout)
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    kinds = journal_kinds(engine)
    assert "review-followup-damped" in kinds and "unit-merged" in kinds
    assert "story-deferred" not in kinds
    # no refiled follow-up anywhere — the spent budget is journal-only
    ledger = (
        project.deferred_work.read_text(encoding="utf-8") if project.deferred_work.exists() else ""
    )
    assert not any(
        e.open and "origin: review-budget-followup" in e.body
        for e in deferredwork.parse_ledger(ledger)
    )


# The review-budget follow-up carry (#425) is retired with the ticket class it
# delivered: a spent review budget is journaled, never filed as a ledger entry,
# so an isolated merge has nothing to strand. One row locks that in on the
# gitignored shape — the one whose row NEEDED the retired carry.


def test_a_damped_isolated_story_journals_without_writing_any_ledger(project):
    """Gitignored ledger — the shape whose row needed the retired #425 carry. A
    damped force-converge journals the spent budget and writes NO ledger entry:
    not in the unit worktree, not in the main checkout, nothing to carry."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    script = [wt_dev_effect(project, "1-1-a")] + [
        wt_review_effect(project, "1-1-a", clean=False) for _ in range(3)
    ]
    engine, _ = make_engine(project, script)  # default wt_policy() → cap 1
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    damped = [e for e in engine.journal.entries() if e["kind"] == "review-followup-damped"]
    assert len(damped) == 1 and damped[0]["re_review_capped"] is False
    assert "refiled" not in damped[0]
    assert not project.deferred_work.exists()  # no main-checkout row
    assert "review-followup-carried" not in journal_kinds(engine)


# ----------------------------------------------------------------- configured target


def test_configured_target_branch_created_and_checked_out(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(target_branch="integration"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert engine.state.target_branch == "integration"
    assert current_branch(project.project) == "integration"
    assert branch_exists(project.project, "integration")
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_worktree_merge_conflict_escalates_and_keeps_branch(project):
    """A unit whose ff-only merge can't fast-forward (target diverged) escalates
    cleanly without an illegal DONE->ESCALATED transition, keeping its branch."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="ff"),
    )
    # diverge the target right after the worktree is cut so ff-only cannot apply
    import bmad_loop.engine as eng

    real_open = eng.open_unit_workspace

    def diverging_open(*a, **k):
        unit = real_open(*a, **k)
        (project.project / "diverge.txt").write_text("target moved\n")
        git(project.project, "add", "-A")
        git(project.project, "commit", "-q", "-m", "target diverges")
        return unit

    eng.open_unit_workspace = diverging_open
    try:
        summary = engine.run()
    finally:
        eng.open_unit_workspace = real_open

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the unit branch is kept for manual merge
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_branch_per_run_escalation_pauses_without_dispatching_next_unit(project):
    """Issue #138 scoping guard: the shared-branch collision cascade is a property
    of the DEFER path, which *returns* and lets the loop dispatch the next unit
    into the held branch. A merge-conflict escalation instead *pauses* the run
    (RunPaused), so under branch_per=run no sibling is ever dispatched while the
    kept worktree holds the shared branch — there is nothing to detach here, and
    on resume the re-armed unit's worktree is freed by the resume-restart discard
    (see test_worktree_crash_restart_discards_stale_worktree) before any mount."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="run", merge_strategy="ff"),
    )
    # diverge the target right after the (shared) worktree is cut so ff-only merge
    # of 1-1-a cannot fast-forward → escalate + pause
    import bmad_loop.engine as eng

    real_open = eng.open_unit_workspace

    def diverging_open(*a, **k):
        unit = real_open(*a, **k)
        if not (project.project / "diverge.txt").exists():
            (project.project / "diverge.txt").write_text("target moved\n")
            git(project.project, "add", "-A")
            git(project.project, "commit", "-q", "-m", "target diverges")
        return unit

    eng.open_unit_workspace = diverging_open
    try:
        summary = engine.run()
    finally:
        eng.open_unit_workspace = real_open

    assert summary.paused and summary.escalated == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    # the run halted at the escalation: 1-2-b was never dispatched, so the
    # shared-branch collision that cascades the DEFER path cannot arise here
    assert "1-2-b" not in engine.state.tasks
    assert "worktree-open-failed" not in journal_kinds(engine)


# ----------------------------------------------------------------- resume


def test_worktree_reopen_reabsolutizes_both_spec_ownership_paths(project, tmp_path):
    """Portable relative spec ownership is rebound to the live mounted worktree.

    Ablation: remove ``dispatched_spec_file`` from reopen_unit's rebase fields and
    this test fails alone on the attempt-owned path while accepted spec rebasing stays green.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1, phase=Phase.DEV_VERIFY)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.spec_file = "_bmad-output/accepted.md"
    task.dispatched_spec_file = "_bmad-output/dispatched.md"

    reopened = engine._reopen_unit(task)

    assert reopened.path == unit.path
    assert task.spec_file == str(unit.path / "_bmad-output/accepted.md")
    assert task.dispatched_spec_file == str(unit.path / "_bmad-output/dispatched.md")

    outside_spec = str(tmp_path / "outside-accepted.md")
    outside_dispatched = str(tmp_path / "outside-dispatched.md")
    task.spec_file = outside_spec
    task.dispatched_spec_file = outside_dispatched
    engine._reopen_unit(task)
    assert task.spec_file == outside_spec
    assert task.dispatched_spec_file == outside_dispatched


def test_reopen_rejects_existing_directory_that_is_not_the_recorded_worktree(project, monkeypatch):
    """A plain directory below main must not make git fall back to main on resume."""
    from bmad_loop.engine import RunPaused

    engine, _ = make_engine(project, [], policy=wt_policy())
    fake = engine.run_dir / "worktrees" / "plain-directory"
    fake.mkdir(parents=True)
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        worktree_path=str(fake),
        branch="bmad-loop/test-run/1-1-a",
    )
    engine.state.tasks[task.story_key] = task
    finalized: list[Path] = []
    monkeypatch.setattr(
        engine, "_finalize_commit_phase", lambda _task: finalized.append(engine.workspace.root)
    )

    with pytest.raises(RunPaused, match="gone or unopenable"):
        engine._finish_inflight()

    assert finalized == []
    assert task.phase == Phase.ESCALATED
    assert engine.workspace.root == project.project


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_reopen_rejects_symlink_alias_to_recorded_worktree(project, monkeypatch):
    """A retargetable alias is not the exact persisted mount ownership claim."""
    from bmad_loop.engine import RunPaused
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=wt_policy())
    engine.state.target_branch = "main"
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    alias = unit.path.parent / "recorded-alias"
    alias.symlink_to(unit.path, target_is_directory=True)
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        worktree_path=str(alias),
        branch=unit.branch,
        baseline_commit=unit.baseline,
    )
    engine.state.tasks[task.story_key] = task
    finalized: list[Path] = []
    monkeypatch.setattr(
        engine, "_finalize_commit_phase", lambda _task: finalized.append(engine.workspace.root)
    )

    with pytest.raises(RunPaused, match="gone or unopenable"):
        engine._finish_inflight()

    assert finalized == []
    assert task.phase == Phase.ESCALATED


@pytest.mark.parametrize("checkout", ["wrong-branch", "detached"])
def test_reopen_rejects_registered_mount_on_wrong_recorded_branch(project, monkeypatch, checkout):
    """Registration is not ownership when the linked checkout changed branch."""
    from bmad_loop.engine import RunPaused
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=wt_policy())
    engine.state.target_branch = "main"
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    if checkout == "wrong-branch":
        git(unit.path, "checkout", "-q", "-b", "operator-branch")
    else:
        git(unit.path, "checkout", "-q", "--detach")
    assert verify.worktree_is_registered(project.project, unit.path)
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        worktree_path=str(unit.path),
        branch=unit.branch,
        baseline_commit=unit.baseline,
    )
    engine.state.tasks[task.story_key] = task
    finalized: list[Path] = []
    monkeypatch.setattr(
        engine, "_finalize_commit_phase", lambda _task: finalized.append(engine.workspace.root)
    )

    with pytest.raises(RunPaused, match="not recorded branch"):
        engine._finish_inflight()

    assert finalized == []
    assert task.phase == Phase.ESCALATED


@pytest.mark.parametrize("damage", ["missing-marker", "foreign-repository"])
def test_reopen_rejects_corrupted_but_registered_mount(project, monkeypatch, damage):
    """Toplevel and common-dir identity fail closed before any continuation."""
    from bmad_loop.engine import RunPaused
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [], policy=wt_policy())
    engine.state.target_branch = "main"
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    if damage == "missing-marker":
        (unit.path / ".git").unlink()
    else:
        shutil.rmtree(unit.path)
        unit.path.mkdir()
        git(unit.path, "init", "-q")
        git(unit.path, "config", "user.email", "test@example.com")
        git(unit.path, "config", "user.name", "Test User")
        git(unit.path, "commit", "--allow-empty", "-q", "-m", "foreign root")
    assert unit.path.resolve() in [path.resolve() for path in worktree_list(project.project)]
    assert not verify.worktree_is_registered(project.project, unit.path)
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        worktree_path=str(unit.path),
        branch=unit.branch,
        baseline_commit=unit.baseline,
    )
    engine.state.tasks[task.story_key] = task
    finalized: list[Path] = []
    monkeypatch.setattr(
        engine, "_finalize_commit_phase", lambda _task: finalized.append(engine.workspace.root)
    )

    with pytest.raises(RunPaused, match="gone or unopenable"):
        engine._finish_inflight()

    assert finalized == []
    assert task.phase == Phase.ESCALATED


def test_restart_arm_anchors_spec_ownership_before_it_discards_the_mount(project, monkeypatch):
    """The restart arm destroys the only tree that can resolve the persisted spelling.

    `_finish_inflight`'s restart arm is the one arm that never calls `reopen_unit`:
    it discards the worktree, clears `task.worktree_path` and saves. Both spec paths
    are persisted RELATIVE to that mount (`model._serialized_worktree_path`), so
    without a re-anchor the save leaves a worktree-relative spelling beside an empty
    `worktree_path`, and the next resume resolves it against the MAIN checkout — which
    carries the same layout, so `recovery_flow._attempt_owned_spec` finds exactly one
    candidate and `spec_within_roots` accepts it. The snapshot restore then rewrites
    the operator's own copy. Anchored on the mount instead, the binding names a tree
    that no longer exists and recovery refuses it loudly.

    Graded at the discard rather than after it: the ordering is the whole property, and
    `_run_story` rebinds the field moments later, so a post-hoc assertion would pass
    with the re-anchor deleted.

    Ablation: drop `task.rebase_spec_paths_on(...)` from `_finish_inflight` and both
    assertions fail with the bare relative spellings.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1, phase=Phase.DEV_RUNNING)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.spec_file = "_bmad-output/accepted.md"
    task.dispatched_spec_file = "_bmad-output/dispatched.md"
    engine.state.tasks["1-1-a"] = task

    seen: dict[str, str | None] = {}

    class _StopAtDiscard(Exception):
        pass

    def _spy(*_args, **_kwargs):
        seen["spec_file"] = task.spec_file
        seen["dispatched_spec_file"] = task.dispatched_spec_file
        raise _StopAtDiscard

    monkeypatch.setattr("bmad_loop.engine.discard_worktree", _spy)

    with pytest.raises(_StopAtDiscard):
        engine._finish_inflight()

    assert seen["spec_file"] == str(unit.path / "_bmad-output/accepted.md")
    assert seen["dispatched_spec_file"] == str(unit.path / "_bmad-output/dispatched.md")


def test_restart_arm_clears_the_baseline_it_measured_in_the_discarded_mount(project, monkeypatch):
    """`baseline_commit`/`baseline_untracked` describe the mount and must die with it.

    `_dev_phase` stamps both from `self.workspace.root` — the unit worktree under
    isolation. The restart arm discards that mount and saves, so leaving them set
    persists two operands that describe a tree which no longer exists. Any later
    resume finding `worktree_path` empty takes the `elif task.baseline_commit:` leg
    into `recovery_flow.rollback_or_pause` against the MAIN checkout, and neither
    operand fails loud there: linked worktrees share the object database, so the
    baseline still resolves and a reset onto it succeeds, while a fresh worktree's
    empty untracked snapshot makes `verify._rollback_cleanup_plan` treat every
    untracked file in the operator's checkout as this attempt's debris.

    Asserted on the DURABLE state, not the in-memory task: state.json is what the
    next resume reads, and the save happens between the discard and the re-run.

    Ablation: drop the two `= None` clears from `_discard_unit_for_restart` and the
    baseline assertions fail while the `worktree_path` one stays green — which is
    precisely the split that made this reachable.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1, phase=Phase.DEV_RUNNING)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = rev_parse_head(unit.path)
    task.baseline_untracked = []  # a fresh mount is a tracked-only checkout
    engine.state.tasks["1-1-a"] = task

    class _StopBeforeRerun(Exception):
        pass

    def _stop(*_args, **_kwargs):
        raise _StopBeforeRerun

    monkeypatch.setattr(engine, "_run_story", _stop)

    with pytest.raises(_StopBeforeRerun):
        engine._finish_inflight()

    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.worktree_path == ""
    assert saved.branch == ""
    assert saved.baseline_commit is None
    assert saved.baseline_untracked is None


def test_restart_arm_leaves_a_spec_the_replacement_mount_can_bind(project, monkeypatch):
    """The property the re-anchor broke: after the discard, the fresh mount BINDS.

    `_finish_inflight` re-anchors `spec_file` onto the mount (correct — recovery must
    not resolve it against the main checkout), and the restart arm then DELETES that
    mount. Left absolute, the value names a tree that no longer exists:
    `verify.resolve_spec_path` passes an absolute path through untouched,
    `_dispatched_spec_for_attempt` resolves it `strict=True` and swallows the
    `FileNotFoundError`, and the fresh attempt starts unbound on a story whose spec is
    sitting in the replacement mount at the same relative place. Nothing downstream
    repairs it — `_record_dev_spec` no-ops while `spec_file` is set — so the repair
    prompt keeps naming the deleted path.

    Graded on the DURABLE state and then on the resolution itself, because the
    spelling is only a proxy: what matters is that the replacement mount answers with
    ITS copy, and neither the dead path nor the main checkout's identical layout.

    Ablation: drop `task.release_spec_paths_from_mount()` from
    `_discard_unit_for_restart` and this reddens on the durable-spelling assertion —
    the saved `spec_file` comes back absolute into the deleted mount, which is the
    state that shipped. That assertion fires before the binding one, so the binding
    assertion is not what the ablation proves; it is what states the CONSEQUENCE, and
    it holds the row to the replacement mount's copy rather than merely to some
    resolvable path (the main checkout carries the identical layout and would answer
    a relative value too, from the wrong tree).
    """
    from bmad_loop import verify
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    rel = "_bmad-output/implementation-artifacts/spec-1-1-a.md"
    (unit.path / rel).parent.mkdir(parents=True, exist_ok=True)
    (unit.path / rel).write_text("---\nstatus: ready-for-dev\n---\n", encoding="utf-8")

    task = StoryTask("1-1-a", 1, phase=Phase.DEV_RUNNING)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    # persisted RELATIVE, exactly as `_serialized_worktree_path` writes it — the
    # re-anchor inside `_finish_inflight` is what makes it absolute
    task.spec_file = rel
    task.dispatched_spec_file = rel
    task.dispatched_spec_snapshot = b"pre-launch bytes"
    engine.state.tasks["1-1-a"] = task

    class _StopBeforeRerun(Exception):
        pass

    monkeypatch.setattr(
        engine, "_run_story", lambda *a, **k: (_ for _ in ()).throw(_StopBeforeRerun())
    )

    with pytest.raises(_StopBeforeRerun):
        engine._finish_inflight()

    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.worktree_path == ""
    assert saved.spec_file == rel  # relative again, not absolute into the deleted tree
    assert saved.dispatched_spec_file is None  # the attempt died with its tree
    assert saved.dispatched_spec_snapshot is None

    # the replacement mount `_run_story` would have opened, carrying the same spec
    replacement = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    (replacement.path / rel).parent.mkdir(parents=True, exist_ok=True)
    (replacement.path / rel).write_text("---\nstatus: ready-for-dev\n---\n", encoding="utf-8")

    # the binding `_dispatched_spec_for_attempt` makes, against the live workspace
    bound = verify.resolve_spec_path(saved.spec_file, replacement.workspace.paths).resolve(
        strict=True
    )
    assert bound == (replacement.path / rel).resolve()


def test_finish_inflight_anchors_on_the_persisted_mount_not_the_live_isolation_policy(project):
    """The relative spelling is persisted state; `isolated` is re-read policy.

    `model._serialized_worktree_path` relativizes whenever `task.worktree_path` is
    set, but `_finish_inflight` gates its `reopen_unit` arms on
    `self._isolated and task.worktree_path` — and `self._isolated` comes from a policy
    file re-read on every resume, where an `isolation` change is journaled and never
    refused. Flip `[scm] isolation` to "none" between a crash and a resume and every
    arm runs without `reopen_unit` on a task whose paths are still mount-relative.

    The story gate stops the restart arm before it mutates anything, so what is graded
    is the re-anchor alone — and it must have happened despite `isolated` being false.

    Ablation: move `task.rebase_spec_paths_on(...)` inside the `if isolated:` arm (or
    delete it) and both assertions fail with the bare relative spellings. Note the
    sibling test above stays GREEN under that first ablation, which is why this row
    exists separately.
    """
    from bmad_loop.engine import RunPaused

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    assert not engine._isolated  # the premise: live policy says in-place

    mount = project.project / ".bmad-loop" / "runs" / "test-run" / "worktrees" / "1-1-a"
    task = StoryTask("1-1-a", 1, phase=Phase.DEV_RUNNING)
    task.worktree_path = str(mount)  # ...but the persisted task still carries one
    task.spec_file = "_bmad-output/accepted.md"
    task.dispatched_spec_file = "_bmad-output/dispatched.md"
    engine.state.tasks["1-1-a"] = task
    write_gated_ledger(project, {"DW-1": ("open", ["gate: 1-1"])})

    with pytest.raises(RunPaused):
        engine._finish_inflight()

    assert task.spec_file == str(mount / "_bmad-output/accepted.md")
    assert task.dispatched_spec_file == str(mount / "_bmad-output/dispatched.md")


def test_isolation_flip_releases_the_units_baseline_before_the_in_place_rollback(
    project, monkeypatch
):
    """The mount-measured operands must not reach a rollback of the MAIN checkout.

    `[scm] isolation` is re-read on every resume and a change is journaled, never
    refused, so `worktree -> none` reaches the restart arm with a mount still recorded.
    That arm re-runs in place, and the leg below it hands `baseline_commit` /
    `baseline_untracked` to `recovery_flow.rollback_or_pause` — but both were stamped
    from `self.workspace.root`, the UNIT, and the workspace is now the main checkout.

    Neither operand fails loud there. Linked worktrees share the object database, so
    the unit baseline still resolves and a reset onto it succeeds; and a fresh
    worktree is a tracked-only checkout, so its empty `baseline_untracked` makes
    `verify._rollback_cleanup_plan` compute `untracked_files(repo) -
    baseline_untracked` as EVERY untracked file in the operator's own checkout. Under
    an auto-recovering cause those are deleted outright — the operator's own files,
    for a story that merely changed isolation mode.

    Graded on the rollback leg not being entered at all, rather than only on the
    cleared fields: the fields are the mechanism, the un-entered leg is the property.

    The mount's CLAIM and the mount's DIRECTORY are separated, and both are asserted.
    `worktree_path` names a directory and is also how `runs` answers the RETROSPECTIVE
    question — which tree owns the state this task already persisted (`task_spec_root`,
    `task_stories_root`) — so a task that keeps the field set while executing in the
    main checkout makes those readers answer for a tree the run has left. The directory
    itself stays: this arm did not build it, and a policy change is not an instruction
    to delete the operator's tree. The orphan is journaled so that is not silent.

    The PROSPECTIVE readers are deliberately absent from that list and from the
    assertions below. `redrive_base_ref` and `spec_reaches_the_redrive` describe the
    re-drive that has not happened yet, and they take the live isolation mode as a
    parameter rather than inferring it here — because `bmad-loop resolve` asks them in
    a separate process BEFORE this resume runs, where no amount of claim-clearing is
    visible. Asserting them here would grade the argument this test passes them, not
    the field it is about; `test_redrive_base_ref_reads_live_policy_not_the_recorded_mount`
    (tests/test_runs.py) carries that direction against both flips.

    Ablation: narrow the arm back to `release_spec_paths_from_mount()` and this
    reddens on the spy — the leg fires with the unit's operands, which is the state
    that would have deleted them. Separately, drop the `worktree_path = ""` and the
    claim assertions redden while the spy stays green, which is why both are here.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    assert not engine._isolated  # the premise: live policy says in-place

    mount = project.project / ".bmad-loop" / "runs" / "test-run" / "worktrees" / "1-1-a"
    (mount / "_bmad-output").mkdir(parents=True, exist_ok=True)
    (mount / "_bmad-output" / "accepted.md").write_text("# spec\n", encoding="utf-8")

    task = StoryTask("1-1-a", 1, phase=Phase.DEV_RUNNING)
    task.worktree_path = str(mount)  # the persisted mount the live policy ignores
    task.spec_file = "_bmad-output/accepted.md"
    task.dispatched_spec_file = "_bmad-output/accepted.md"
    task.dispatched_spec_snapshot = b"pre-launch bytes"
    # measured INSIDE the unit by `_dev_phase`; a fresh mount is tracked-only, which
    # is what makes the untracked half so destructive against another tree
    task.baseline_commit = rev_parse_head(project.project)
    task.baseline_untracked = []
    engine.state.tasks["1-1-a"] = task

    rolled: list[str] = []
    monkeypatch.setattr(engine, "_rollback_or_pause", lambda t, cause: rolled.append(cause))

    class _StopBeforeRerun(Exception):
        pass

    def _stop(*_a, **_k):
        raise _StopBeforeRerun

    monkeypatch.setattr(engine, "_run_story", _stop)

    with pytest.raises(_StopBeforeRerun):
        engine._finish_inflight()

    assert rolled == []  # the leg never ran, so the unit operands never travelled

    saved = load_state(engine.run_dir).tasks["1-1-a"]
    assert saved.baseline_commit is None
    assert saved.baseline_untracked is None
    assert saved.spec_file == "_bmad-output/accepted.md"  # relative again
    assert saved.dispatched_spec_file is None
    assert saved.dispatched_spec_snapshot is None

    # the CLAIM is dropped: the retrospective readers must now answer the main checkout
    assert saved.worktree_path == ""
    assert saved.branch == ""
    assert runs.task_stories_root(saved, engine.state) == project.project
    assert runs.task_spec_root(saved, engine.state) == project.project

    # ...but the DIRECTORY is not deleted, and the orphan is on the record
    assert mount.is_dir()  # left standing: this arm did not build it
    assert "isolation-flip-orphaned-worktree" in journal_kinds(engine)


def test_isolation_flip_keeps_the_mount_for_an_accepted_continuation(project, monkeypatch):
    """Recorded ownership wins over live policy until accepted work is integrated.

    The worktree path is persisted state while ``self._isolated`` is live policy for
    the next attempt. A verified DEV_VERIFY continuation must therefore reopen the
    exact mount after ``worktree -> none`` instead of releasing its accepted spec and
    running in main.

    Ablation: gate the DEV_VERIFY reopen on ``self._isolated`` and the observed paths
    are released to main (and ``isolation-flip-orphaned-worktree`` is journaled).
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    assert not engine._isolated  # the premise: live policy says in-place

    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    mount = unit.path
    rel = "_bmad-output/accepted.md"
    (mount / rel).parent.mkdir(parents=True, exist_ok=True)
    (mount / rel).write_text("# spec\n", encoding="utf-8")

    task = StoryTask("1-1-a", 1, phase=Phase.DEV_VERIFY)
    task.worktree_path = str(mount)  # persisted ownership survives the policy flip
    task.branch = unit.branch
    task.spec_file = rel  # persisted RELATIVE, as `_serialized_worktree_path` writes it
    task.dispatched_spec_file = rel
    task.dispatched_spec_snapshot = b"pre-launch bytes"
    task.baseline_commit = unit.baseline
    task.baseline_untracked = []
    engine.state.tasks["1-1-a"] = task

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        engine,
        "_resume_after_dev_verify",
        lambda t: seen.update(
            spec_file=t.spec_file,
            dispatched=t.dispatched_spec_file,
            worktree_path=t.worktree_path,
            baseline_commit=t.baseline_commit,
        ),
    )

    engine._finish_inflight()

    assert seen, "the DEV_VERIFY continuation arm never ran"
    assert seen["spec_file"] == str(mount / rel)
    assert seen["dispatched"] == str(mount / rel)
    assert seen["worktree_path"] == str(mount)
    assert seen["baseline_commit"] == rev_parse_head(project.project)
    assert "isolation-flip-orphaned-worktree" not in journal_kinds(engine)


def test_open_unit_workspace_reclaims_the_orphan_holding_its_mount_path(project):
    """A flip back to `worktree` is not blocked by the orphan the flip left behind.

    `unit_branch_name` and the mount path are both DETERMINISTIC in
    (run_id, unit_key, run_dir), so a re-mount targets the exact directory a previous
    mount used. `engine._release_orphaned_mount` deliberately leaves that directory
    standing when live policy drops isolation — a policy change is not an instruction
    to delete the tree — so a later flip BACK re-derives the same path and met a
    `git worktree add` that refuses both an existing target and a branch checked out
    elsewhere, deferring the task instead of resuming it.

    The BRANCH is deliberately spared by the reclaim: under `branch_per=run` this name
    is the SHARED run branch carrying commits earlier units already landed, so a
    force-delete would drop real work. The reclaim drops only the worktree, and the
    `branch_exists` fork re-mounts the branch from its own HEAD — which is what the
    committed file below grades.

    Ablation: delete the `discard_worktree(...)` call in `open_unit_workspace` and the
    second mount raises `GitError`; swap its `""` back to `branch` and `landed.txt` is
    gone because the shared branch was force-deleted.
    """
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "run", run_dir)

    first = open_unit_workspace(*args)
    (first.path / "landed.txt").write_text("earlier unit\n", encoding="utf-8")
    git(first.path, "add", "landed.txt")
    git(first.path, "commit", "-m", "landed on the shared run branch")

    # the orphan: nothing tore this down, exactly as the isolation flip leaves it
    assert first.path.is_dir()

    second = open_unit_workspace(*args)

    assert second.path == first.path  # the same deterministic mount point
    assert second.branch == first.branch
    # the branch was NOT force-deleted: the earlier unit's commit is still on it
    assert (second.path / "landed.txt").read_text(encoding="utf-8") == "earlier unit\n"


def test_story_remount_preserves_named_tip_and_restarts_from_pinned_base(project):
    """Story branches are attempt-local while the preserve ref keeps abandoned work."""
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    (first.path / "abandoned.txt").write_text("old attempt\n", encoding="utf-8")
    git(first.path, "add", "abandoned.txt")
    git(first.path, "commit", "-q", "-m", "abandoned story attempt")
    old_tip = rev_parse_head(first.path)

    (project.project / "advanced.txt").write_text("new base\n", encoding="utf-8")
    git(project.project, "add", "advanced.txt")
    git(project.project, "commit", "-q", "-m", "advance requested base")
    pinned_base = rev_parse_head(project.project)

    second = open_unit_workspace(*args)

    preserve_ref = f"attempt-preserve/test-run-{old_tip[:8]}"
    assert rev_parse_head(second.path) == pinned_base
    assert not (second.path / "abandoned.txt").exists()
    assert (second.path / "advanced.txt").read_text(encoding="utf-8") == "new base\n"
    assert git(project.project, "rev-parse", preserve_ref) == old_tip


def test_story_remount_without_unique_commits_still_moves_to_advanced_base(project):
    """Reset is unconditional for story scope, not coupled to preservation work.

    Ablation: nest ``reset_branch_if_tip`` under ``if commits`` and this leaves the
    replacement at the original base because there is no abandoned commit to park.
    """
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    old_base = rev_parse_head(first.path)

    (project.project / "advanced-without-abandoned.txt").write_text("new base\n")
    git(project.project, "add", "advanced-without-abandoned.txt")
    git(project.project, "commit", "-q", "-m", "advance requested base")
    advanced = rev_parse_head(project.project)

    second = open_unit_workspace(*args)

    assert old_base != advanced
    assert rev_parse_head(second.path) == advanced
    assert (second.path / "advanced-without-abandoned.txt").read_text() == "new base\n"


def test_new_story_workspace_uses_base_sha_pinned_before_ref_moves(project, monkeypatch):
    """A moving requested branch cannot change the operation's selected snapshot."""
    from bmad_loop.workspace import open_unit_workspace

    real_resolve = verify.rev_parse_revision
    observed: dict[str, str] = {}

    def resolve_then_advance(repo, revision):
        pinned = real_resolve(repo, revision)
        if revision == "main" and "pinned" not in observed:
            observed["pinned"] = pinned
            (project.project / "late-base-move.txt").write_text("too late\n")
            git(project.project, "add", "late-base-move.txt")
            git(project.project, "commit", "-q", "-m", "concurrent base move")
            observed["advanced"] = rev_parse_head(project.project)
        return pinned

    monkeypatch.setattr(verify, "rev_parse_revision", resolve_then_advance)
    unit = open_unit_workspace(
        project.project,
        project,
        "test-run",
        "1-1-pinned",
        "main",
        "story",
        project.project / ".bmad-loop" / "runs" / "test-run",
    )

    assert observed["pinned"] != observed["advanced"]
    assert rev_parse_head(unit.path) == observed["pinned"]
    assert not (unit.path / "late-base-move.txt").exists()


def test_story_remount_preservation_failure_keeps_old_tip_and_mount(project, monkeypatch):
    """Preservation must complete before either the story ref or mount is discarded.

    INVERSE ablation: catch the preservation error and continue to reset/discard;
    then the old branch tip and mounted directory assertions fail.
    """
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    git(first.path, "commit", "--allow-empty", "-q", "-m", "abandoned story attempt")
    old_tip = rev_parse_head(first.path)
    monkeypatch.setattr(
        verify,
        "preserve_commits",
        lambda *_a, **_k: (_ for _ in ()).throw(verify.GitError("preserve refused")),
    )

    with pytest.raises(verify.GitError, match="preserve refused"):
        open_unit_workspace(*args)

    assert git(project.project, "rev-parse", first.branch) == old_tip
    assert first.path.is_dir()


def test_story_remount_cas_failure_keeps_concurrent_tip_and_mount(project, monkeypatch):
    """A rival branch move after preservation is never overwritten by reclaim.

    INVERSE ablation: remove the expected-old operand from ``update-ref`` and the
    concurrently created tip is reset to main instead of surviving.
    """
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    git(first.path, "commit", "--allow-empty", "-q", "-m", "abandoned story attempt")
    old_tip = rev_parse_head(first.path)
    real_reset = verify.reset_branch_if_tip
    rival: dict[str, str] = {}

    def move_then_reset(repo, name, revision, expected_tip):
        git(first.path, "commit", "--allow-empty", "-q", "-m", "concurrent story move")
        rival["tip"] = rev_parse_head(first.path)
        real_reset(repo, name, revision, expected_tip)

    monkeypatch.setattr(verify, "reset_branch_if_tip", move_then_reset)

    with pytest.raises(verify.GitError, match="update-ref"):
        open_unit_workspace(*args)

    assert git(project.project, "rev-parse", first.branch) == rival["tip"]
    assert git(project.project, "rev-parse", f"attempt-preserve/test-run-{old_tip[:8]}") == old_tip
    assert first.path.is_dir()


def test_story_remount_rejects_branch_move_after_reset_before_checkout(project, monkeypatch):
    """The second race window cannot smuggle a rival tip into the replacement."""
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    git(first.path, "commit", "--allow-empty", "-q", "-m", "abandoned story attempt")
    old_tip = rev_parse_head(first.path)
    pinned = rev_parse_head(project.project)
    real_add = verify.worktree_add

    def move_then_add(repo, path, branch, base=None, *, create=True):
        if not create:
            git(repo, "update-ref", f"refs/heads/{branch}", old_tip, pinned)
        real_add(repo, path, branch, base, create=create)

    monkeypatch.setattr(verify, "worktree_add", move_then_add)

    with pytest.raises(verify.GitError, match="moved after reclaim reset"):
        open_unit_workspace(*args)

    assert git(project.project, "rev-parse", first.branch) == old_tip
    assert not first.path.exists()


def test_story_remount_does_not_reread_head_after_tip_validation(project, monkeypatch):
    """A final moving HEAD read cannot replace the already-validated baseline.

    Ablation: restore ``baseline = rev_parse_head(wt)`` and the injected rival
    move lands between validation and that read, returning the rival as baseline.

    Only reads of the MOUNTED checkout count: the orphan reclaim legitimately reads
    the first mount's HEAD (at the same path) before ``worktree_add`` to park its
    uncommitted state, so the spy arms itself on the mount call.
    """
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    args = (project.project, project, "test-run", "1-1-a", "main", "story", run_dir)
    first = open_unit_workspace(*args)
    git(first.path, "commit", "--allow-empty", "-q", "-m", "abandoned story attempt")
    old_tip = rev_parse_head(first.path)
    pinned = rev_parse_head(project.project)
    real_head = verify.rev_parse_head
    real_add = verify.worktree_add
    reads = 0
    mounted = False

    def arm_on_mount(*a, **k):
        nonlocal mounted
        real_add(*a, **k)
        mounted = True

    def move_on_redundant_head_read(repo):
        nonlocal reads
        if mounted and Path(repo).resolve() == first.path.resolve():
            reads += 1
            if reads == 2:
                git(project.project, "update-ref", f"refs/heads/{first.branch}", old_tip, pinned)
        return real_head(repo)

    monkeypatch.setattr(verify, "worktree_add", arm_on_mount)
    monkeypatch.setattr(verify, "rev_parse_head", move_on_redundant_head_read)

    second = open_unit_workspace(*args)

    assert reads == 1
    assert second.baseline == pinned


def test_restart_discard_retains_shared_run_branch_history(project):
    """Restart teardown frees the mount without deleting a cumulative run ref."""
    from bmad_loop.workspace import open_unit_workspace

    engine, _ = make_engine(project, [], policy=wt_policy(branch_per="run"))
    args = (
        project.project,
        project,
        "test-run",
        "1-1-a",
        "main",
        "run",
        engine.run_dir,
    )
    first = open_unit_workspace(*args)
    (first.path / "landed-before-restart.txt").write_text("retained\n")
    git(first.path, "add", "landed-before-restart.txt")
    git(first.path, "commit", "-q", "-m", "landed before restart")
    landed_tip = rev_parse_head(first.path)
    task = StoryTask(
        "1-1-a",
        1,
        worktree_path=str(first.path),
        branch=first.branch,
        baseline_commit=first.baseline,
    )

    engine._discard_unit_for_restart(task)
    second = open_unit_workspace(*args)

    assert rev_parse_head(second.path) == landed_tip
    assert (second.path / "landed-before-restart.txt").read_text() == "retained\n"


def test_story_remount_bad_base_fails_before_discard(project):
    """An unresolvable requested base leaves the existing story workspace intact."""
    from bmad_loop.workspace import open_unit_workspace

    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    first = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", run_dir
    )
    tip = rev_parse_head(first.path)

    with pytest.raises(verify.GitError, match="missing-base"):
        open_unit_workspace(
            project.project,
            project,
            "test-run",
            "1-1-a",
            "missing-base",
            "story",
            run_dir,
        )

    assert rev_parse_head(first.path) == tip
    assert first.path.is_dir()


def test_worktree_spec_approval_pause_resumes_in_same_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(
        gates=GatesPolicy(mode="per-story-spec-approval"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
    )
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    task = saved.tasks["1-1-a"]
    assert task.phase == Phase.DEV_VERIFY and task.worktree_path and task.branch
    # the worktree stays mounted across the pause so resume can review in it
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert len(worktree_list(project.project)) == 2

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([wt_review_effect(project, "1-1-a", clean=True)])
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    resumed = Engine(
        paths=project,
        policy=in_place,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary2 = resumed.run()

    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


@pytest.mark.parametrize("role", ["dev", "review"])
def test_isolation_flip_replays_recorded_session_in_mount_and_lands_it(project, monkeypatch, role):
    """Completed dev/review results retain their recorded workspace through merge.

    Ablation: gate the recorded-result reopen on live isolation and each row writes
    the continuation marker in main directly, leaving no unit merge evidence.
    """
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    engine.state.target_branch = "main"
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    phase = Phase.DEV_RUNNING if role == "dev" else Phase.REVIEW_RUNNING
    task = StoryTask(
        "1-1-a",
        1,
        phase=phase,
        attempt=1,
        review_cycle=1,
        baseline_commit=unit.baseline,
        worktree_path=str(unit.path),
        branch=unit.branch,
    )
    task.record_session(
        SessionRecord(
            task_id=f"1-1-a-{role}-1",
            role=role,
            status="completed",
            result_json={"workflow": f"recorded-{role}"},
        )
    )
    engine.state.tasks[task.story_key] = task
    observed: list[Path] = []

    def continue_in_recorded_workspace(*_args, **_kwargs):
        observed.append(engine.workspace.root)
        marker = engine.workspace.root / f"continued-{role}.txt"
        marker.write_text(f"{role}\n", encoding="utf-8")
        git(engine.workspace.root, "add", marker.name)
        git(engine.workspace.root, "commit", "-q", "-m", f"continue {role}")
        task.phase = Phase.DONE

    target = "_drive_story" if role == "dev" else "_review_and_commit"
    monkeypatch.setattr(engine, target, continue_in_recorded_workspace)

    engine._finish_inflight()

    assert observed == [unit.path]
    assert (project.project / f"continued-{role}.txt").read_text(encoding="utf-8") == f"{role}\n"
    assert "unit-merged" in journal_kinds(engine)
    assert "isolation-flip-orphaned-worktree" not in journal_kinds(engine)
    assert not unit.path.exists()


def test_isolation_flip_finishes_mounted_defer_before_teardown(project):
    """A persisted defer decision is completed where its rejected work lives."""
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none", keep_failed=False),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    engine.state.target_branch = "main"
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.DEV_VERIFY,
        baseline_commit=unit.baseline,
        worktree_path=str(unit.path),
        branch=unit.branch,
        defer_reason="accepted rejection",
    )
    engine.state.tasks[task.story_key] = task
    rejected = unit.path / "rejected-work.txt"
    rejected.write_text("preserve in failed patch\n", encoding="utf-8")

    engine._finish_inflight()

    assert task.phase == Phase.DEFERRED
    assert "resume-defer" in journal_kinds(engine)
    assert "isolation-flip-orphaned-worktree" not in journal_kinds(engine)
    assert not unit.path.exists()
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert "rejected-work.txt" in patch.read_text(encoding="utf-8")
    assert "preserve in failed patch" in patch.read_text(encoding="utf-8")


def test_isolation_flip_missing_mount_escalates_before_continuation(project, monkeypatch):
    """Missing recorded ownership never falls back to a continuation in main.

    Ablation: select reopening from live isolation and the finalizer spy runs in the
    main checkout instead of the task escalating.
    """
    from bmad_loop.engine import RunPaused

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    engine, _ = make_engine(project, [], policy=in_place)
    engine.state.target_branch = "main"
    task = StoryTask(
        "1-1-a",
        1,
        phase=Phase.COMMITTING,
        worktree_path=str(engine.run_dir / "worktrees" / "gone"),
        branch="bmad-loop/test-run/1-1-a",
    )
    engine.state.tasks[task.story_key] = task
    finalized: list[Path] = []
    monkeypatch.setattr(
        engine, "_finalize_commit_phase", lambda _task: finalized.append(engine.workspace.root)
    )

    with pytest.raises(RunPaused, match="is gone"):
        engine._finish_inflight()

    assert finalized == []
    assert task.phase == Phase.ESCALATED
    assert engine.workspace.root == project.project


def test_worktree_crash_restart_discards_stale_worktree(project):
    """A unit interrupted before the spec gate is restarted fresh: the stale
    worktree is discarded and a new one mounted, not stacked on top."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")])
    # simulate an interrupted unit left mid-flight (DEV_RUNNING, worktree mounted)
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1)
    engine.state.tasks["1-1-a"] = task
    task.phase = Phase.DEV_RUNNING
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    engine._save()

    # resume with a full dev+review script → restart should succeed
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_worktree_resume_committing_finishes_and_merges(project):
    """#115, isolated flavor: a unit persisted at COMMITTING (gate+advance save
    landed, DONE save did not) is finished inside its still-mounted worktree
    and merged back — not discarded as a stale worktree by resume-restart."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    from bmad_loop.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    # the attempt committed its work inside the unit (only the work file —
    # the sprint board is the orchestrator's dev-time write, still uncommitted)
    src = unit.path / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(unit.path, "add", "src.txt")
    git(unit.path, "commit", "-q", "-m", "attempt work for 1-1-a")
    wt = project.rebased(unit.path)
    sp = wt.implementation_artifacts / "spec-1-1-a.md"
    write_spec(sp, "done", unit.baseline)
    set_sprint(wt, "1-1-a", "done")

    task = StoryTask("1-1-a", 1, phase=Phase.COMMITTING, attempt=1)
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    task.spec_file = str(sp)
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": unit.baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks["1-1-a"] = task
    engine._save()

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    in_place = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="none"),
    )
    resumed = Engine(
        paths=project,
        policy=in_place,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    assert adapter.sessions == []  # commit finished from persisted state alone
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)
    kinds = journal_kinds(resumed)
    assert "resume-commit" in kinds and "unit-merged" in kinds
    assert "resume-restart" not in kinds


# ----------------------------------------------------------------- regression guard


def test_isolation_none_leaves_no_worktrees(project):
    """The default (isolation=none) path must not create branches/worktrees."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=Policy(gates=GatesPolicy(mode="none"), notify=QUIET),  # isolation defaults to none
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.target_branch == ""  # never resolved in none mode
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert "worktree-opened" not in journal_kinds(engine)


# ----------------------------------------------------------------- new guards (review hardening)


def test_detached_head_pauses_instead_of_landing_on_unreferenced_commit(project):
    """isolation=worktree with no configured target on a detached HEAD has no
    branch to merge into; the run must pause rather than commit onto a nameless
    detached HEAD that the next checkout would orphan."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "checkout", "--detach")
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()
    assert summary.paused
    assert "detached HEAD" in (engine.state.paused_reason or "")
    # nothing was isolated into a worktree
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_commit_message_template_applied(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(commit_message_template="feat({story_key}): via {run_id}"),
    )
    summary = engine.run()
    assert summary.done == 1
    # the story's commit message (not the merge commit) used the template
    log = git(project.project, "log", "--format=%s")
    assert "feat(1-1-a): via test-run" in log
    assert "implemented" not in log  # built-in default was not used


def test_commit_message_template_story_title(project):
    """{story_title} renders the spec's `title:` frontmatter — where a bmad-loop
    spec's title actually lives — with the "Story <id>:" label dropped; a
    template without the placeholder never pays the spec read."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    review = wt_review_effect(project, "1-1-a", clean=True)

    def review_with_title(spec):
        # the review pass is the spec's last writer, so the title the commit-time
        # read sees must be stamped after it
        result = review(spec)
        sp = project.rebased(spec.cwd).implementation_artifacts / "spec-1-1-a.md"
        sp.write_text(
            sp.read_text().replace("title: 'test'", "title: 'Story 1.1: Wire the Frobnicator'")
        )
        return result

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), review_with_title],
        policy=wt_policy(commit_message_template="chore(bmad): {story_key}\n\n{story_title}"),
    )
    summary = engine.run()
    assert summary.done == 1
    log = git(project.project, "log", "--format=%B")
    assert "chore(bmad): 1-1-a" in log
    assert "Wire the Frobnicator" in log
    assert "Story 1.1:" not in log  # the label would just repeat the key


def test_commit_message_template_story_title_neutralizes_control_chars(project):
    """A NUL in the title reaches `git commit -m` as an argv element, where
    `subprocess.run` raises a bare ValueError. `_run_git` translates
    TimeoutExpired/UnicodeDecodeError/OSError but not that, so it would escape as
    itself into `_finalize_commit_phase`'s `except BaseException`, which restores
    and re-raises — crashing the run with the task already persisted as
    COMMITTING, so every later resume re-renders the same title and re-crashes.
    No exotic file bytes are needed to get there: `title: "\\0"` is an ordinary
    double-quoted YAML scalar.

    Ablation target: drop the `_TITLE_CONTROL_RE` substitution and this fails
    with `ValueError: embedded null byte` instead of committing."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    review = wt_review_effect(project, "1-1-a", clean=True)

    def review_nul_title(spec):
        result = review(spec)
        sp = project.rebased(spec.cwd).implementation_artifacts / "spec-1-1-a.md"
        sp.write_text(sp.read_text().replace("title: 'test'", 'title: "Wire\\0the Frobnicator"'))
        return result

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), review_nul_title],
        policy=wt_policy(commit_message_template="chore(bmad): {story_title}"),
    )
    summary = engine.run()
    assert summary.done == 1  # committed rather than wedged mid-COMMITTING
    assert "chore(bmad): Wire the Frobnicator" in git(project.project, "log", "--format=%s")


def test_commit_message_template_story_title_neutralizes_surrogates(project):
    """The other unspawnable class, and the one a C0/DEL-only filter misses. A
    lone surrogate has no UTF-8 encoding, so `subprocess.run` raises
    UnicodeEncodeError while encoding the argv — a ValueError, but *not* the
    UnicodeDecodeError `_run_git` translates, so it wedges the run exactly as an
    embedded NUL does. `title: "\\uD800"` is an ordinary YAML escape and PyYAML
    hands the unpaired code point straight back.

    Ablation target: drop `\\ud800-\\udfff` from `_TITLE_CONTROL_RE` and this
    fails with `UnicodeEncodeError: surrogates not allowed` instead of
    committing."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    review = wt_review_effect(project, "1-1-a", clean=True)

    def review_surrogate_title(spec):
        result = review(spec)
        sp = project.rebased(spec.cwd).implementation_artifacts / "spec-1-1-a.md"
        sp.write_text(
            sp.read_text().replace("title: 'test'", 'title: "Wire\\uD800the Frobnicator"')
        )
        return result

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), review_surrogate_title],
        policy=wt_policy(commit_message_template="chore(bmad): {story_title}"),
    )
    summary = engine.run()
    assert summary.done == 1  # committed rather than wedged mid-COMMITTING
    assert "chore(bmad): Wire the Frobnicator" in git(project.project, "log", "--format=%s")


def test_commit_message_template_story_title_falls_back_to_h1(project):
    """A spec written without a `title:` — i.e. not from this project's
    template — still yields a title from a first markdown H1."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    review = wt_review_effect(project, "1-1-a", clean=True)

    def review_h1_only(spec):
        result = review(spec)
        sp = project.rebased(spec.cwd).implementation_artifacts / "spec-1-1-a.md"
        sp.write_text(sp.read_text().replace("title: 'test'\n", "") + "\n# Heading Sourced\n")
        return result

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), review_h1_only],
        policy=wt_policy(commit_message_template="chore(bmad): {story_title}"),
    )
    summary = engine.run()
    assert summary.done == 1
    assert "chore(bmad): Heading Sourced" in git(project.project, "log", "--format=%s")


def test_commit_message_template_story_title_falls_back_to_key(project):
    """Neither a `title:` nor an H1 → the placeholder renders the story key,
    never an empty string."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    review = wt_review_effect(project, "1-1-a", clean=True)

    def review_titleless(spec):
        result = review(spec)
        sp = project.rebased(spec.cwd).implementation_artifacts / "spec-1-1-a.md"
        sp.write_text(sp.read_text().replace("title: 'test'\n", ""))
        return result

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), review_titleless],
        policy=wt_policy(commit_message_template="chore(bmad): {story_title}"),
    )
    summary = engine.run()
    assert summary.done == 1
    log = git(project.project, "log", "--format=%s")
    assert "chore(bmad): 1-1-a" in log


def test_story_title_undecodable_spec_falls_back_to_key(project):
    """A spec that is no longer valid UTF-8 takes the same fallback as an
    unreadable one. Unit-level because the whole-file decode failure is masked
    upstream on the normal path (read_frontmatter degrades it to a retry) but
    live on the resume-into-COMMITTING arm, which renders the message without
    re-reading frontmatter first."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_bytes(b"---\nstatus: done\n---\n\n# Story 1.1: caf\xe9 latte\n")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == "1-1-a"


@pytest.mark.parametrize(
    ("indent", "expected"),
    [
        ("", "Indented Heading"),
        ("   ", "Indented Heading"),  # CommonMark allows up to three
        ("    ", "1-1-a"),  # a fourth space is an indented code block, not an H1
    ],
)
def test_story_title_h1_indent_bound(project, indent, expected):
    """The H1 fallback follows CommonMark's indentation rule, so a heading a
    hand-authored spec indented still yields a title instead of silently
    degrading to the story key — while a code block at four spaces stays a code
    block."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text(f"---\nstatus: done\n---\n\n{indent}# Indented Heading\n")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Wire the Frobnicator", "Wire the Frobnicator"),
        # `#` may be followed by a tab as well as a space...
        ("#\tWire the Frobnicator", "Wire the Frobnicator"),
        # ...but by something else it is not a heading at all, and two hashes
        # are an H2. Both fall back rather than yielding a title.
        ("#Wire the Frobnicator", "1-1-a"),
        ("## Wire the Frobnicator", "1-1-a"),
        # A closing hash run is syntax, not title. This is the only one of these
        # that would otherwise render a WRONG subject rather than fall back.
        ("# Wire the Frobnicator ###", "Wire the Frobnicator"),
        ("# Wire the Frobnicator #", "Wire the Frobnicator"),
        # ...and it takes whitespace to make one, so a hash fused to the last
        # word stays part of the title.
        ("# Wire it in C#", "Wire it in C#"),
        # Setext is a valid CommonMark H1 and is refused ON PURPOSE: accepting it
        # would make any prose line above a `===` divider the commit subject,
        # trading a safe fallback for a confidently wrong title.
        ("Wire the Frobnicator\n===", "1-1-a"),
        ("Some ordinary prose\n=====", "1-1-a"),
    ],
)
def test_story_title_h1_atx_forms(project, body, expected):
    """Which H1 spellings the fallback honors, and which it declines. The
    declines are the load-bearing half: each is a documented narrowing, not an
    oversight, so a later "conformance" patch has to argue with these cases."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text(f"---\nstatus: done\n---\n\n{body}\n")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == expected


@pytest.mark.parametrize(
    ("fence", "content"),
    [
        # The shape that makes this common: any fenced snippet whose first line
        # is a comment. A spec showing setup steps before its heading is
        # ordinary, and `#` opens a comment in sh, python, yaml, toml, ruby...
        ("```bash", "# Install the dependencies"),
        ("```python", "# TODO: wire this up"),
        # ...and the documentation-flavored shape, where the fenced heading is
        # a deliberate example of the very syntax being scanned for.
        ("````markdown", "# Example Heading"),
        # Tildes open a fence too, and a fence may be indented up to three.
        ("~~~yaml", "# generated - do not edit"),
        ("   ```sh", "# nested under a list item"),
    ],
)
def test_story_title_h1_ignores_fenced_blocks(project, fence, content):
    """A `#` line inside a fenced block is a comment or an example, not this
    spec's heading — CommonMark agrees the first H1 here is the one after the
    fence. Getting this wrong is the bad kind of wrong: it renders a
    confidently incorrect commit subject rather than falling back to the key."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    close = fence.lstrip(" ")[: 4 if fence.lstrip(" ").startswith("````") else 3]
    spec.write_text(
        f"---\nstatus: done\n---\n\n{fence}\n{content}\n{close}\n\n# Wire the Frobnicator\n"
    )
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == "Wire the Frobnicator"


@pytest.mark.parametrize(
    "body",
    [
        # A shorter run of the same character does not close a longer fence —
        # this is why a ```` block may quote ``` at all.
        "````markdown\n```\n# Fenced Heading\n```\n````\n\n# Wire the Frobnicator\n",
        # ...and neither does a run of the *other* fence character.
        "```markdown\n~~~\n# Fenced Heading\n~~~\n```\n\n# Wire the Frobnicator\n",
        # A closing run must have nothing but whitespace after it, so a fence
        # line carrying an info string is an opener, never a close.
        "~~~\n# Fenced Heading\n~~~ still-open\n~~~\n\n# Wire the Frobnicator\n",
    ],
)
def test_story_title_h1_nested_fence_does_not_close_early(project, body):
    """The closing rule is same-character, at-least-as-long, nothing after it.
    Relax any of those three and an inner fence ends the block early, putting a
    fenced `# ...` back in scope as the title — which is the whole bug."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text(f"---\nstatus: done\n---\n\n{body}")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == "Wire the Frobnicator"


def test_story_title_h1_unclosed_fence_falls_back(project):
    """An unclosed fence swallows the rest of the file, so there is no heading
    left to find and the story key is the answer. Pinned because the tempting
    "reset at EOF" repair would resurrect exactly the comment-as-title bug this
    scan exists to prevent."""
    engine, _ = make_engine(project, [])
    spec = project.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("---\nstatus: done\n---\n\n```bash\n# Install the dependencies\n")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(spec)

    assert engine._story_title(task) == "1-1-a"


def test_render_commit_template_without_placeholder_skips_the_spec_read(project, monkeypatch):
    """A template that never names {story_title} must not pay the spec read —
    the claim the policy docs and CHANGELOG both make. Pinned by making the read
    itself fatal, so the assertion cannot pass just because the title happened to
    go unused."""
    engine, _ = make_engine(project, [])
    monkeypatch.setattr(
        Engine, "_story_title", lambda self, task: pytest.fail("spec read for a template without")
    )
    engine.policy = wt_policy(commit_message_template="chore(bmad): {story_key}")
    task = StoryTask(story_key="1-1-a", epic=1)
    task.spec_file = str(project.implementation_artifacts / "spec-1-1-a.md")

    assert engine._render_commit_template(task) == "chore(bmad): 1-1-a"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A story id is a dash/dot composite here, so the label strip must survive
        # every shape this project actually issues...
        ("Story 1.1: Wire the Frobnicator", "Wire the Frobnicator"),
        ("Story 3-2: Dash composite", "Dash composite"),
        ("Story 1-1-a: Trailing letter", "Trailing letter"),
        ("story 1.1: lowercased label", "lowercased label"),
        # ...while never eating a real title that merely opens with "Story".
        # The label must start at a DIGIT: `\S+` would strip "Points:" here, and
        # `\d+\.\d+` would stop matching the dash composites above.
        ("Story Points: Add estimates", "Story Points: Add estimates"),
        ("Storybook: Add a knob", "Storybook: Add a knob"),
        ("  Plain title  ", "Plain title"),
        # YAML hands back whatever an unquoted scalar looked like. A blank
        # `title:` is None and must fall back; a bool is a typo, not a title; a
        # number still beats rendering the bare story key.
        (None, ""),
        ("", ""),
        ("Story 1.1:", ""),
        (True, ""),
        (1.1, "1.1"),
        # Control characters are neutralized, not passed through: a NUL reaching
        # `git commit -m` argv raises a bare ValueError out of subprocess, which
        # _run_git does not translate — it would crash a task already persisted
        # as COMMITTING and wedge every resume. `title: "\0"` is plain YAML.
        ("\x00", ""),
        ("Wire the\x00Frobnicator", "Wire the Frobnicator"),
        ("Story 1.1:\x00Wire it", "Wire it"),
        ("Story\x001.1: Split label", "Split label"),
        # Lone surrogates go with them: `title: "\uD800"` is the same ordinary
        # YAML escape, and one in the argv raises UnicodeEncodeError out of
        # subprocess — a ValueError _run_git does not translate either.
        ("\ud800", ""),
        ("Wire\ud800the Frobnicator", "Wire the Frobnicator"),
        ("Story 1.1:\udfffWire it", "Wire it"),
        ("Two\nlines", "Two lines"),
        ("Tabbed\ttitle", "Tabbed title"),
        ("Collapse   the    runs", "Collapse the runs"),
    ],
)
def test_story_label_stripped_cases(raw, expected):
    """The label strip sits between two wrong answers: too loose eats the title,
    too tight stops matching this project's ids. Pinned per-case rather than
    derived, so a regex retune has to restate its intent here."""
    assert _story_label_stripped(raw) == expected


@pytest.mark.parametrize(
    ("raw", "story_key", "expected"),
    [
        # stories.ID_RE admits alphabetic ids ("auth", "oauth-setup"), which the
        # digit-led heuristic cannot recognize — so for the id we actually hold,
        # match it exactly rather than guessing its shape.
        ("Story auth: Add login", "auth", "Add login"),
        ("Story oauth-setup: Wire the callback", "oauth-setup", "Wire the callback"),
        ("story AUTH: case folds", "auth", "case folds"),
        # The exact match is an ADDITION to the heuristic, never a replacement:
        # a sprint spec labels itself "Story 1.1:" while its key is "1-1-a", so
        # keying only off the task id would stop stripping the common case.
        ("Story 1.1: Wire the Frobnicator", "1-1-a", "Wire the Frobnicator"),
        # ...and it must not turn into a licence to eat real titles: a title
        # whose first word merely follows "Story" is not this task's id.
        ("Story Points: Add estimates", "auth", "Story Points: Add estimates"),
        ("Story authentication: Add login", "auth", "Story authentication: Add login"),
        # A key carrying regex metacharacters is matched literally, not compiled.
        ("Story a.b: Escaped", "a.b", "Escaped"),
        ("Story axb: Escaped", "a.b", "Story axb: Escaped"),
    ],
)
def test_story_label_stripped_matches_the_task_id(raw, story_key, expected):
    """Stories mode inherits this renderer and issues alphabetic ids, which the
    digit-led pattern cannot match. Where the task's own id is known it is the
    ground truth; the heuristic stays for the labels that do not repeat the key
    verbatim."""
    assert _story_label_stripped(raw, story_key) == expected


# ------------------------------------------------ per_worktree engine plugin


def _write_stub_plugin(project, name, *, ready=_OK, setup=_OK, teardown=_OK, seed_globs=None):
    """A project-local *declarative* plugin whose lifecycle hooks are shell stubs
    (no real Unity) — proving a generic data-only plugin can gate the engine's
    per_worktree flow. A blocking hook's non-zero exit vetoes (defers) the unit.
    Commands are TOML literal strings, so they may embed double quotes but not
    single quotes. No [python], so it loads on folder-drop (no [plugins] enabled)."""
    plug_dir = project.project / ".bmad-loop" / "plugins" / name
    plug_dir.mkdir(parents=True)
    lines = ["[plugin]", f'name = "{name}"', "api_version = 1"]
    if seed_globs:
        globs = ", ".join(f'"{g}"' for g in seed_globs)
        lines.append(f"seed_globs = [{globs}]")
    lines += [
        "[hooks.pre_worktree_setup]",
        f"cmd = '{setup}'",
        "blocking = true",
        "[hooks.pre_ready_gate]",
        f"cmd = '{ready}'",
        "blocking = true",
        "[hooks.pre_worktree_teardown]",
        f"cmd = '{teardown}'",
    ]
    (plug_dir / "plugin.toml").write_text("\n".join(lines) + "\n")


def _pw_policy(**gates):
    return Policy(
        gates=GatesPolicy(mode=gates.get("mode", "none")),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
    )


def _hook_stages(engine):
    """The stages of every plugin-hook the bus journaled, in order."""
    return [e.get("stage") for e in engine.journal.entries() if e["kind"] == "plugin-hook"]


def test_per_worktree_setup_then_gate_then_teardown_and_seed(project):
    """Happy path: the worktree is seeded, the setup hook runs, the ready gate
    waits (and only passes because setup ran first), the agent runs, teardown
    fires. Ordering is proven by the gate depending on a setup marker."""
    # The MCP-generated skill tree really is gitignored in a per_worktree project
    # (docs/FEATURES.md), and this fixture has to say so itself: until #384 the
    # git-add shield wrote its patterns into the repo-wide `.git/info/exclude`, so
    # the untracked dir below was hidden in the MAIN checkout too and the pre-merge
    # cleanliness gate never saw it. The shield is per-worktree now, so the gitignore
    # line below is what keeps the dir out of `verify.dirty_paths` entirely — the gate
    # never sees it. (Since #460 an untracked file the operator leaves in their own
    # checkout is tolerated at merge rather than blocking it, so this fixture no longer
    # depends on that refusal either way.) Committed before the dir exists.
    gitignore = project.project / ".gitignore"
    gitignore.write_text(gitignore.read_text() + ".claude/skills/\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # a gitignored MCP skill dir present in the main repo (untracked) to be seeded
    skill = project.project / ".claude" / "skills" / "gameobject-create"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("tool", encoding="utf-8")
    # setup asserts the seed reached its cwd (the worktree) before marking ready;
    # the gate fails unless that marker exists -> proves seed+setup precede the gate.
    _write_stub_plugin(
        project,
        "stub",
        setup=_seeded_then_touch(".claude/skills/gameobject-create/SKILL.md", "setup-done"),
        ready=_exists_run("setup-done"),
        teardown=_touch_run("teardown-done"),
        seed_globs=[".claude/skills/*"],
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.done == 1
    assert (engine.run_dir / "setup-done").is_file()
    assert (engine.run_dir / "teardown-done").is_file()
    # setup gated the ready gate gated teardown, in order, all via the bus
    stages = _hook_stages(engine)
    assert "pre_worktree_setup" in stages
    assert stages.index("pre_worktree_setup") < stages.index("pre_ready_gate")
    assert stages.index("pre_ready_gate") < stages.index("pre_worktree_teardown")
    # the dev + review sessions actually ran (gate let them through)
    assert [s.role for s in adapter.sessions] == ["dev", "review"]


def test_per_worktree_setup_failure_defers_and_skips_session(project):
    """A setup failure (Editor wouldn't launch) vetoes -> defers the unit, never
    starts a session, still tears down best-effort, and closes the (empty) worktree."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        setup="exit 3",
        teardown=_touch_run("teardown-done"),
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "pre_worktree_setup" in task.defer_reason  # the setup-stage veto deferred it
    assert adapter.sessions == []  # gate/setup ran before any dev session
    kinds = journal_kinds(engine)
    assert "plugin-veto" in kinds and "story-deferred" in kinds
    # the ready gate never ran (setup vetoed first)
    assert "pre_ready_gate" not in _hook_stages(engine)
    # teardown still ran; the deferred unit's worktree is kept (keep_failed default)
    # for inspection, exactly like any other deferral.
    assert (engine.run_dir / "teardown-done").is_file()
    assert len(worktree_list(project.project)) == 2


def test_per_worktree_ready_gate_failure_defers(project):
    """Setup succeeds but the Editor never reports ready -> defer + teardown."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        ready="exit 1",
        teardown=_touch_run("teardown-done"),
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy(),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "pre_ready_gate" in task.defer_reason  # the ready-stage veto deferred it
    assert adapter.sessions == []
    stages = _hook_stages(engine)
    assert "pre_worktree_setup" in stages and "pre_ready_gate" in stages
    assert (engine.run_dir / "teardown-done").is_file()


def test_per_worktree_teardown_runs_on_pause(project):
    """A spec-approval pause leaves the worktree mounted, but the teardown hook is
    still fired (teardown runs in the finally, even as RunPaused unwinds)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_stub_plugin(
        project,
        "stub",
        teardown=_touch_run("teardown-done"),
    )
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy(mode="per-story-spec-approval"),
    )
    summary = engine.run()

    assert summary.paused
    # the worktree stays up for resume, but teardown fired
    assert len(worktree_list(project.project)) == 2
    assert (engine.run_dir / "teardown-done").is_file()
    assert "pre_worktree_teardown" in _hook_stages(engine)


def _leaking_dev_effect(project, story_key, *, leak_name, in_branch_set):
    """A dev effect that does the normal worktree work AND simulates a per_worktree
    Unity Editor leaking an asset write into the *main* checkout before merge.
    When in_branch_set the branch also commits `leak_name` (so the leaked main-tree
    copy collides with an incoming file — the recoverable case); otherwise the leak
    is stray work the merge does not introduce."""
    base = wt_dev_effect(project, story_key)

    def effect(spec):
        if in_branch_set:
            (spec.cwd / leak_name).write_text(f"branch content for {story_key}\n")
        result = base(spec)
        # the competing main-repo Editor writes the asset into the main checkout
        (project.project / leak_name).write_text("editor leaked\n")
        return result

    return effect


def test_merge_auto_recovers_editor_dirtied_target(project):
    """A unit whose own incoming file was leaked (untracked) into the main checkout
    by a per_worktree Editor merges successfully after auto-clean, journaling
    merge-target-cleaned."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _leaking_dev_effect(project, "1-1-a", leak_name="Leak.cs", in_branch_set=True),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # the branch's version of the leaked file landed on target
    assert (project.project / "Leak.cs").read_text() == "branch content for 1-1-a\n"
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "merge-target-cleaned" in kinds and "unit-merged" in kinds
    cleaned = next(e for e in engine.journal.entries() if e["kind"] == "merge-target-cleaned")
    assert cleaned["paths"] == ["Leak.cs"]


def _operator_edit_dev_effect(project, story_key, *, rel_path, marker, stage):
    """A dev effect that does the normal worktree work AND appends `marker` to a
    TRACKED file in the *main* checkout that the branch never touches — the operator
    editing their own working copy mid-run. Appends rather than overwrites so the
    edit stays inert in whatever file it lands on.

    ``stage`` picks which half of #618's split the fixture grades, and callers must
    pass it deliberately: STAGED is the refusal (git can fold a staged stray into a
    fast-forwardable squash), UNSTAGED is the tolerance (an edit git holds only in
    the working tree can reach no merge commit at all). A caller that wants one and
    writes the other grades the opposite path and still goes green, which is how the
    pre-#618 version of this helper came to pin the wrong row.
    """
    base = wt_dev_effect(project, story_key)

    def effect(spec):
        result = base(spec)
        fp = project.project / rel_path
        fp.write_text(fp.read_text(encoding="utf-8") + marker, encoding="utf-8")
        if stage:
            git(project.project, "add", "--", rel_path)
        return result

    return effect


def _committed_versions(project, rel: str) -> list[str]:
    """Every committed version of `rel` reachable from HEAD, read out of git history.

    The working tree cannot answer "did this land in a commit?". A pathspec carry
    that swept an operator's edit into its own commit leaves the tree CLEAN and the
    file's bytes unchanged on disk — the substitution is invisible from there, and
    that invisibility is the whole hazard. `rev-list -- <rel>` names the commits that
    touched the path; `show <sha>:<rel>` reads the blob each one recorded.
    """
    shas = git(project.project, "rev-list", "HEAD", "--", rel).splitlines()
    return [git(project.project, "show", f"{sha}:{rel}") for sha in shas]


def test_merge_stray_dirt_escalates_with_clear_message(project):
    """Dirt in the main checkout that is NOT part of the branch's incoming files
    (possible real operator work) is never cleaned: the unit escalates and keeps its
    branch, with a message that names tracked dirt as the hazard and offers the two
    SAFE resolutions rather than blaming a Unity Editor and saying "clean them" (#460).

    Since #460 that refusal is scoped to dirt a merge could actually commit, and since
    #618 the axis is the index rather than trackedness — an unstaged tracked stray is
    inert and tolerated, like an untracked one
    (`test_merge_tolerates_untracked_stray_in_main_checkout`). So the stray here is an
    appended comment line in the repo's tracked `.gitignore`, STAGED by the effect:
    the branch never touches the file, and a trailing comment changes no ignore
    behavior."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # The target must really be tracked, or this test silently degrades into the
    # tolerated case it is no longer about. `git` raises on a nonzero rc.
    git(project.project, "ls-files", "--error-unmatch", ".gitignore")
    engine, _ = make_engine(
        project,
        [
            _operator_edit_dev_effect(
                project, "1-1-a", rel_path=".gitignore", marker="# operator edit\n", stage=True
            ),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    # Each promise gets its own assertion so a failure names which one broke.
    # The two NEGATIVE assertions are the whole of #460's second complaint and must
    # not be "simplified" away by a later session: the old message asserted a Unity
    # Editor as the likely cause on every isolated merge — including repos with no
    # Unity anywhere — and told the operator to "clean" their own uncommitted work,
    # which is the exact verb (`unlink`) this guard performs on incoming strays.
    assert "Unity" not in reason
    assert "clean them" not in reason
    assert "Commit, stash or revert" in reason  # the two SAFE resolutions, named
    assert ".gitignore" in reason  # the inner GitError still names the exact path
    # The composed message says which half of the dirt actually blocks a merge. Note
    # the inner GitError carries "tracked" too, so this one does not by itself pin the
    # OUTER wording — "Commit, stash or revert" above is the assertion that does.
    assert "tracked" in reason
    # branch kept for manual merge; the operator's edit was left untouched
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert (project.project / ".gitignore").read_text().endswith("# operator edit\n")
    assert "merge-target-cleaned" not in journal_kinds(engine)


def test_merge_tolerates_untracked_stray_in_main_checkout(project):
    """#460's headline row. An untracked file the operator left in the MAIN
    checkout, unrelated to the run, no longer stops it: a merge writes only paths
    that differ between target and branch, and git never stages an untracked file
    into a merge or squash commit, so the file cannot be overwritten or swept in.
    Before #460 this exact run ended `done=0 paused=True escalated=1` — one stray
    `notes.txt` halted an unattended loop at its first story.

    The `merge-preflight-refused` assertion at the end is a GREEN-ABLATION record:
    no mutation of #623's code reddens it, because it asserts an absence on a path
    that never raises. It is here to stop a later change firing the corrective event
    unconditionally — pairing every tolerated stray with a refusal that did not
    happen — and its positive counterpart is
    `test_merge_shape_clash_journals_the_corrective_refusal`, which is the row that
    goes red if the event stops firing."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _leaking_dev_effect(
                project, "1-1-a", leak_name="operator-notes.txt", in_branch_set=False
            ),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and summary.escalated == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds and "story-escalated" not in kinds
    # tolerated, NOT cleaned: the guard skips the stray entirely rather than
    # deleting the operator file it just decided to let through.
    assert (project.project / "operator-notes.txt").read_text() == "editor leaked\n"
    assert "merge-target-cleaned" not in kinds
    # ...and walking past it is not silent. A merge that proceeded over operator dirt
    # leaves the same kind of trace as one that cleaned a leak, so an operator reading
    # the journal can see which of their files the run merged around.
    assert "merge-target-tolerated" in kinds
    tolerated = next(e for e in engine.journal.entries() if e["kind"] == "merge-target-tolerated")
    assert tolerated["paths"] == ["operator-notes.txt"]
    assert tolerated["story_key"] == "1-1-a"
    assert tolerated["branch"] == "bmad-loop/test-run/1-1-a"
    # ...and nothing corrects it, because nothing went wrong: the merge landed.
    assert "merge-preflight-refused" not in kinds


def _shape_clash_dev_effect(project, story_key, *, incoming_path, stray_path):
    """A dev effect whose branch commits `incoming_path` while an untracked stray
    lands at `stray_path` in the MAIN checkout. Neither path is a member of the
    other's set, so #460's tolerance walks past the stray — but the two collide
    STRUCTURALLY, so git refuses the merge at its own pre-flight. The two shapes are
    the ones pinned at the verify layer by
    `test_clean_incoming_collisions_shape_clash_stops_at_gits_own_preflight`."""
    base = wt_dev_effect(project, story_key)

    def effect(spec):
        incoming = spec.cwd / incoming_path
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_text(f"branch content for {story_key}\n")
        result = base(spec)
        stray = project.project / stray_path
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("operator\n")
        return result

    return effect


@pytest.mark.parametrize(
    ("incoming_path", "stray_path"),
    [
        ("Assets/Leak.cs", "Assets"),  # untracked FILE where the merge needs a DIR
        ("notes", "notes/keep.txt"),  # untracked DIR where the merge needs a FILE
    ],
    ids=["file-where-dir-needed", "dir-where-file-needed"],
)
def test_merge_shape_clash_journals_the_corrective_refusal(project, incoming_path, stray_path):
    """#623. `merge-target-tolerated` is written from inside `clean_incoming_collisions`'s
    callback, strictly BEFORE `merge_branch` runs, so it can only ever record what the
    GUARD decided. A stray outside the incoming set by PATH can still clash with it by
    SHAPE, and git then refuses the merge over the very path that event called harmless
    — leaving the journal asserting the run tolerated something that in fact stopped it.

    The fix is corrective, not a rewrite: the pre-merge event stays (emitting it only on
    success would lose the trace in exactly the run worth debugging) and the pre-flight
    arm appends `merge-preflight-refused` carrying the same paths plus git's own text.
    Order is the whole claim — a reader scanning the journal top-down must meet the
    correction after the assertion it corrects, not before it.

    Real git, no monkeypatch: the wiring axis is
    `test_merge_failure_escalation_tells_a_preflight_refusal_from_a_conflict`, which
    injects the exception; this row proves the two shapes really do reach that arm.

    Ablation: delete the corrective `journal.append` from `merge_local` and both rows
    fail here while `test_merge_tolerates_untracked_stray_in_main_checkout` stays green
    — disjoint sets, which is what makes the negative pin there meaningful."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _shape_clash_dev_effect(
                project, "1-1-a", incoming_path=incoming_path, stray_path=stray_path
            ),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    entries = engine.journal.entries()
    kinds = [e["kind"] for e in entries]
    # both land, and in the order that makes the second one a correction of the first
    assert kinds.index("merge-target-tolerated") < kinds.index("merge-preflight-refused")
    assert kinds.index("merge-preflight-refused") < kinds.index("story-escalated")
    tolerated = next(e for e in entries if e["kind"] == "merge-target-tolerated")
    refused = next(e for e in entries if e["kind"] == "merge-preflight-refused")
    assert tolerated["paths"] == [stray_path]  # the guard really did wave it through
    assert refused["tolerated"] == tolerated["paths"]  # same list, so they can be paired
    assert refused["story_key"] == tolerated["story_key"] == "1-1-a"
    assert refused["branch"] == tolerated["branch"] == "bmad-loop/test-run/1-1-a"
    # git's raw text rides along: it is the only thing that names WHICH path clashed,
    # and "refused before starting" pins that this came off the pre-flight arm rather
    # than the content-conflict one, which must never write this event.
    assert "refused before starting" in refused["error"]
    assert stray_path.split("/")[0] in refused["error"]
    # the operator's bytes and shape survive, and the branch is kept for manual merge
    stray = project.project / stray_path
    assert stray.is_file() and stray.read_text() == "operator\n"
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert "merge-target-cleaned" not in kinds


def test_merge_tolerates_unstaged_tracked_stray_in_main_checkout(project):
    """#618's headline row, and the engine-layer twin of
    `test_merge_stray_dirt_escalates_with_clear_message`: the SAME file, the SAME
    edit, differing only in whether git holds it in the index.

    Before #618 an unstaged tracked stray escalated the story and paused an
    unattended run. It should not: a merge writes only paths that differ between
    target and branch, and it commits only what is STAGED, so an edit living solely
    in the working tree can be neither overwritten by the merge nor written into its
    commit. Measured across both topologies and both strategies (#618).

    The history assertion is the half the working tree cannot make. `worktree_clean`
    is False here either way — the operator's edit is still uncommitted, which is the
    point — so "the bytes are still on disk" would pass just as well if a commit had
    also taken a copy of them. Reading every committed version of the path is what
    pins that no commit on the target branch carries the edit.

    The run must reach `done`, not merely avoid raising: `escalated == 0` and a DONE
    phase are what separate a tolerated stray from one that quietly deferred the unit.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # The target must really be tracked, or this row silently degrades into the
    # untracked case above. `git` raises on a nonzero rc.
    git(project.project, "ls-files", "--error-unmatch", ".gitignore")
    engine, _ = make_engine(
        project,
        [
            _operator_edit_dev_effect(
                project, "1-1-a", rel_path=".gitignore", marker="# operator edit\n", stage=False
            ),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and summary.escalated == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds and "story-escalated" not in kinds
    # tolerated, NOT cleaned: the guard never touches a stray it decided to let through
    assert (project.project / ".gitignore").read_text().endswith("# operator edit\n")
    assert "merge-target-cleaned" not in kinds
    # ...and no commit on the target branch took a copy of it on the way past. The
    # non-empty check is not decoration: `any()` over an empty history is False, so
    # a read that silently found no commits would pass this line for the wrong reason.
    versions = _committed_versions(project, ".gitignore")
    assert versions and not any("# operator edit" in v for v in versions)
    # walking past operator dirt is journaled, exactly as cleaning a leak is
    tolerated = next(e for e in engine.journal.entries() if e["kind"] == "merge-target-tolerated")
    assert tolerated["paths"] == [".gitignore"]
    assert tolerated["story_key"] == "1-1-a"
    assert tolerated["branch"] == "bmad-loop/test-run/1-1-a"
    assert "merge-preflight-refused" not in kinds


def test_merge_refuses_dirt_on_a_path_the_run_commits_for_itself(project):
    """The data-safety half of #618. An unstaged edit is inert for the MERGE and is
    tolerated by the row above — but not when it sits on a path the RUN itself
    commits after the merge, and the sprint board is one of those.

    `_carry_board_advance` calls `verify.commit_paths`, which runs
    `git add -- :(literal)<board>` and then a pathspec commit, so it takes whatever
    the working tree holds at that path no matter who wrote it. Left tolerated, the
    operator's private edit rides out under `chore(sprint-status): carry 1-1-a to
    done` — their bytes, the run's name, and a CLEAN tree afterwards, which is
    exactly why nothing surfaces it.

    Reaching that requires the board to be dirty in the main checkout AND outside the
    branch's incoming set, and this row's setup is the shape that produces it rather
    than decoration:

    * the board is committed with the row ALREADY at the target, so the unit
      worktree checks that out, `_post_dev_state_sync`'s advance writes nothing there
      and the board never enters `finalize_commit`'s `git add -A`. It is a stray, not
      an incoming collision — the ordinary tracked board rides the merge instead, and
      a stray inside the incoming set would be restored rather than swept.
    * the operator reopens the row in their own checkout WITHOUT committing, which is
      what `_pick_next` (main board, on disk) reads to pick the story at all, and
      appends a private note beside it.

    The last assertion reads git history, not the working tree, and that is the whole
    point: the measured failure leaves the tree clean and the file's bytes unchanged
    on disk, so "the edit is still there" is true in BOTH the safe and the unsafe
    outcome. Only the committed blobs tell them apart.
    """
    marker = "# operator: reopened locally, do not ship\n"
    commit_sprint(project, {"1-1-a": "done"})
    board = project.sprint_status
    rel = board.relative_to(project.project).as_posix()
    set_sprint(project, "1-1-a", "ready-for-dev")
    board.write_text(board.read_text(encoding="utf-8") + marker, encoding="utf-8")
    before = board.read_text(encoding="utf-8")
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])

    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and summary.done == 0
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    # the carry clause, not the staged-changes one: unstaging is no remedy for a path
    # this run is going to commit either way.
    assert "bookkeeping commit" in reason and "staged changes" not in reason
    assert rel in reason
    # the operator's bytes are byte-intact — the guard refuses, it never repairs
    assert board.read_text(encoding="utf-8") == before
    # ...and no commit on the target branch carries them. `_committed_versions` is
    # non-empty here (commit_sprint committed the board), so this is not the vacuous
    # pass an empty history would give.
    versions = _committed_versions(project, rel)
    assert versions and not any(marker.strip() in v for v in versions)
    assert not any(
        "chore(sprint-status)" in s for s in git(project.project, "log", "--format=%s").splitlines()
    )
    # branch kept for manual merge; nothing was cleaned or walked past
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    kinds = journal_kinds(engine)
    assert "merge-target-cleaned" not in kinds and "merge-target-tolerated" not in kinds


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: OSError(13, "Permission denied"),
        lambda: RuntimeError("Permission denied while resolving collision cleanup"),
        lambda: verify.GitSpawnError("git status failed to spawn: Permission denied"),
    ],
    ids=["fs-oserror", "fs-runtimeerror", "git-spawn"],
)
def test_merge_env_fault_during_target_clean_keeps_branch_and_escalates(
    project, monkeypatch, make_exc
):
    """#343: `clean_incoming_collisions` mutates the checkout directly
    (resolve/unlink/rmdir), so non-spawn FS faults arrive as plain OSError or
    RuntimeError values no chokepoint can translate — and its git reads can raise
    a typed GitSpawnError. The guard must treat all three like any other reconcile
    failure: keep the branch and escalate rather than crash a DONE unit
    mid-merge — and the escalation must name the environment fault, not
    claim stray uncommitted files that may not exist.

    Ablation targets: remove `RuntimeError` only from `merge_local`'s catch and the
    fs-runtimeerror row fails because the run crashes. Keep that catch but remove
    `RuntimeError` only from its environmental `isinstance` arm and the same row
    fails on stray-dirt guidance; the underlying-fault guidance is required."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = make_exc()

    def env_fault(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "clean_incoming_collisions", env_fault)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    assert "Permission denied" in reason
    # environment fault, not the stray-dirt refusal. The second assertion is the
    # discriminator and must name a phrase only the stray-dirt arm carries — there
    # may be no stray files at all here, so that arm's remediation must not leak in.
    assert "could not reconcile" in reason
    assert "Commit, stash or revert" not in reason
    # branch kept for manual merge — the unit's work is not stranded
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


# One phrase per escalation shape. The test asserts its own row's phrase present and
# every OTHER row's absent, so the set is written once here rather than as a per-row
# exclusion list that silently stops covering a shape the moment one is added.
@pytest.mark.parametrize(
    "paths, restored, present, absent",
    [
        (("Assets/Gen.cs",), True, "Clear those first", "needs nothing from you"),
        ((), True, "needs nothing from you", "Clear those first"),
        ((), False, "could NOT be rolled back", "needs nothing from you"),
    ],
    ids=["untracked-residue", "tracked-restored", "tracked-restore-failed"],
)
def test_half_applied_escalation_asks_only_for_the_residue_that_survives(
    project, monkeypatch, paths, restored, present, absent
):
    """A half-applied checkout leaves residue on two axes, and only one of them is
    ever the operator's job — so this escalation composes its middle instead of
    stating both every time.

    An incoming path the target did not track lands untracked and no restore
    reaches it, so they clear it. One it DID track was rewritten in place and
    `merge_branch` has already reset it, so asking them to clear anything would
    send them to a checkout that is already correct. The third row is that same
    tracked case with the reset ALSO failed, which inverts the instruction: the
    tree is still holding incoming content and has to be restored before the cause
    is worth fixing, exactly as `MergeCommitRefusedError.restored` does for its own
    neighbour.

    Each row asserts its phrase present AND another row's absent, because presence
    alone passes for a message that simply says everything unconditionally — which
    is the failure mode a composed message has and a fixed one does not.

    Ablation: drop the `e.restored` branch and keep only the `e.paths` clause, and
    the two tracked rows fail on the presence half; make every clause
    unconditional instead and all three fail on the absence half."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = verify.MergeHalfAppliedError(
        "git merge --ff-only feat failed in /repo (failed part-way through checkout): "
        "fatal: smudge filter boom failed",
        paths=paths,
        restored=restored,
    )

    def refuse_merge(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "merge_branch", refuse_merge)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    reason = engine.state.paused_reason or ""
    assert present in reason
    assert absent not in reason


def test_half_applied_escalation_with_both_residues_orders_restore_first(project, monkeypatch):
    """When BOTH residue axes survive — untracked paths to clear AND a tracked
    rewrite whose rollback failed — the two asks must agree on an order. The
    restore leads: a resume dies on the tracked residue first, and its clause
    says "before anything else" and has to mean it. The untracked clause then
    defers ("Then clear those") instead of also claiming first place — the
    composed message used to say "Clear those first" and "before anything else"
    about two different steps in the same breath.

    Both `.index` calls double as presence asserts (ValueError = red), so the
    row pins composition AND order in one place.

    Ablation: swap the two `steps.append` blocks back and this row fails on the
    order; make the untracked clause unconditional "Clear those first" and it
    fails on the phrase. The three matrix rows above stay green — none of them
    stages both residues at once, which is why this row exists."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = verify.MergeHalfAppliedError(
        "git merge --ff-only feat failed in /repo (failed part-way through checkout): "
        "fatal: smudge filter boom failed; AND git reset --hard HEAD failed "
        "(tree not restored): fatal: could not reset",
        paths=("Assets/Gen.cs",),
        restored=False,
    )

    def refuse_merge(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "merge_branch", refuse_merge)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    reason = engine.state.paused_reason or ""
    assert "Clear those first" not in reason  # the restore is first; this may not claim it
    restore_at = reason.index("could NOT be rolled back")
    clear_at = reason.index("Then clear those")
    assert restore_at < clear_at
    assert "Assets/Gen.cs" in reason


def test_half_applied_escalation_prescribes_a_path_scoped_restore(project, monkeypatch):
    """The failed-restore clause hands the operator the SAME path-scoped write the
    run itself attempted — `git checkout HEAD --` over `e.rewritten` — never the
    repo-wide `git reset --hard HEAD` it used to prescribe. That advice went stale
    the moment the restore became per-path: the restore can now fail with the
    operator's own uncommitted work elsewhere in the tree (per-path attribution is
    what allows it to run over such a tree at all), so following the old
    prescription would flatten exactly the work the attribution spared.

    Ablation: put the `git reset --hard HEAD` wording back in the clause and this
    row fails on both command assertions; drop the `e.rewritten` interpolation
    and it fails on the path."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = verify.MergeHalfAppliedError(
        "git merge --ff-only feat failed in /repo (failed part-way through checkout): "
        "fatal: smudge filter boom; AND git checkout HEAD -- <paths> failed "
        "(tracked residue not restored): fatal: could not restore",
        restored=False,
        rewritten=("boards/sprint.yaml",),
    )

    def refuse_merge(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "merge_branch", refuse_merge)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    reason = engine.state.paused_reason or ""
    assert "`git checkout HEAD -- boards/sprint.yaml`" in reason
    assert "reset --hard HEAD` in" not in reason  # the repo-wide advice is gone
    assert "never a repo-wide `git reset --hard`" in reason


def test_half_applied_merge_escalation_names_the_residue_from_the_exception_paths(
    project, monkeypatch
):
    """The half-applied arm names the leftover files from the exception's `paths`
    attribute, not by echoing git's text.

    Both channels normally carry the same names, which is exactly why the matrix
    row above cannot test this: its pass-through assertion (`git's own text is in
    the reason`) stays true even if the arm ignores `paths` entirely. So this row
    stages an exception whose MESSAGE never mentions the file and whose `paths`
    does — the only shape where the two channels disagree — and asserts the name
    reaches the operator anyway.

    It is worth an arm of its own because the name is the actionable half. The
    residue blocks every subsequent attempt as a pre-flight refusal, so an
    escalation that says "some files were left behind" without saying WHICH sends
    the operator to diff a checkout the run has been told to tolerate strays in.

    Ablation: replace `residue` with a fixed phrase, or build it from `str(e)`
    instead of `e.paths`, and this row fails alone — every matrix row above stays
    green, since there the two channels agree."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = verify.MergeHalfAppliedError(
        "git merge --squash feat failed in /repo (failed part-way through checkout): "
        "fatal: smudge filter boom failed",  # deliberately names no path
        paths=("Assets/Generated.cs", "notes.txt"),
    )

    def refuse_merge(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "merge_branch", refuse_merge)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    reason = engine.state.paused_reason or ""
    assert "Assets/Generated.cs" in reason and "notes.txt" in reason


_MERGE_FAILURE_PHRASES = (
    "refused by git before it started",
    "the target checkout is back as it was",
    "left MID-MERGE",
    "left STAGED",
    "content conflict",
    "failed PART-WAY THROUGH",
    "UNVERIFIED",
    "was not classified",
)


@pytest.mark.parametrize(
    "make_exc, present",
    [
        (
            lambda: verify.MergePreflightError(
                "git merge --squash feat failed in /repo (refused before starting): "
                "error: The following untracked working tree files would be "
                "overwritten by merge:\n\tleak.cs"
            ),
            "refused by git before it started",
        ),
        (
            lambda: verify.MergeCommitRefusedError(
                "git merge --no-ff feat failed in /repo (merged, but git refused the "
                "commit): error: gpg failed to sign the data"
            ),
            "the target checkout is back as it was",
        ),
        (
            lambda: verify.MergeCommitRefusedError(
                "git merge --no-ff feat failed in /repo (merged, but git refused the "
                "commit): error: gpg failed to sign the data; AND git merge --abort "
                "failed (repo left mid-merge): fatal: could not abort",
                restored=False,
            ),
            "left MID-MERGE",
        ),
        (
            lambda: verify.MergeCommitRefusedError(
                "git commit (squash feat) failed in /repo (merged, but git refused "
                "the commit): error: gpg failed to sign the data; the squash result "
                "is left staged (not rolled back: the checkout already carried "
                "uncommitted work, which `reset --hard` would destroy with it)",
                restored=False,
                staged=True,
            ),
            "left STAGED",
        ),
        (
            lambda: verify.MergeConflictError(
                "git merge --no-ff feat failed in /repo (conflict): "
                "CONFLICT (content): Merge conflict in src.txt"
            ),
            "content conflict",
        ),
        (
            lambda: verify.MergeHalfAppliedError(
                "git merge --squash feat failed in /repo (failed part-way through "
                "checkout): fatal: zzz.dat: smudge filter boom failed; left untracked "
                "in /repo: aaa.txt",
                paths=("aaa.txt",),
            ),
            "failed PART-WAY THROUGH",
        ),
        (
            lambda: verify.MergeResidueUnreadError(
                "git merge --squash feat failed in /repo (checkout state unverified): "
                "fatal: zzz.dat: smudge filter boom failed; AND the residue probe "
                "failed: git ls-files --others failed in /repo: probe boom"
            ),
            "UNVERIFIED",
        ),
        (
            lambda: verify.GitError(
                "git merge --no-ff feat failed in /repo: fatal: some state no probe measured"
            ),
            "was not classified",
        ),
    ],
    ids=[
        "preflight-refusal",
        "commit-refused",
        "commit-refused-unrestored",
        "commit-refused-staged",
        "content-conflict",
        "half-applied",
        "residue-unread",
        "unclassified",
    ],
)
def test_merge_failure_escalation_tells_a_preflight_refusal_from_a_conflict(
    project, monkeypatch, make_exc, present
):
    """#619: `merge_local` caught every `verify.GitError` out of `merge_branch` and
    told the operator to resolve "a content conflict against the target". Most of
    those failures are git declining at pre-flight — nothing merged, the checkout
    untouched, no markers anywhere — so the guidance sent them looking for a
    conflict that does not exist. `MergePreflightError` is a GitError subclass, so
    the two arms must be ordered subclass-first for the split to exist at all.

    Each row asserts its own phrase PRESENT and every OTHER row's phrase ABSENT. The
    absence half is the discriminator: a single catch-all arm still makes each row's
    own phrase appear on one of them, and only the cross-check catches the collapse.
    Every other rather than one neighbour, because past two arms a pair can collapse
    into each other while the rest stay distinct.

    `commit-refused` is the third state (#619): git merged cleanly and then declined
    to COMMIT — a `pre-merge-commit`/`commit-msg` hook, or a signing step. Its arm
    exists because neither neighbour's remedy fits it.

    `commit-refused-unrestored` is that state with the abort ALSO failed. It is a
    row and not a footnote because the two differ in what the operator must do
    FIRST: a resume over a mid-merge checkout dies on the merge state however well
    they fix the hook, so a message claiming the checkout was restored costs them
    the one step that unblocks it.

    `commit-refused-staged` is the squash leg's strand of that same state: its
    commit is the leg's own `git commit` after the merge already staged the
    result, so there is no MERGE_HEAD and "recover the merge" would be fiction —
    the squash result is sitting STAGED and clearing it is the first step. The
    `staged` flag is what parts the two unrestored wordings, which is the row's
    whole point.

    `content-conflict` pins the TYPED conflict arm: the conflict is measured
    (unmerged stages) and raised as `MergeConflictError`, so the resolve-by-hand
    wording rides the measurement rather than the absence of a better match.

    `unclassified` pins the demoted catch-all. A bare `GitError` is a state
    nothing measured, and the arm now says so — run `git status`, git's text
    names the cause — instead of prescribing conflict resolution for it. Six
    mislabeled git states in a row reached operators through the old wording;
    this row is what turns a hypothetical seventh into a vague-but-true message
    instead of a precise fiction.

    `half-applied` is the fourth state: git died part-way through the checkout and
    left incoming files behind, untracked. It reaches `merge_local` as a SIBLING of
    the pre-flight error rather than a subclass, so it needs an arm of its own —
    and it is the row the cross-check matters most for, since the phrase it must
    never carry is the pre-flight arm's "the target checkout is unchanged" claim,
    which is false here in the exact clause the operator acts on.

    `residue-unread` is the terminal state: the merge failed and the post-merge
    residue reading failed too, so neither the pre-flight claim (the checkout is
    unchanged) nor the half-applied one (these files were left) is available. Its
    arm says the state is UNVERIFIED and sends the operator to their own
    `git status` — the reading the run could not take. Without the arm it falls
    to the content-conflict catch-all, which is the #619 defect wearing a probe
    error's text.

    Ablation: delete either subclass arm from `merge_local` and its rows fail on
    both halves while the others stay green; collapse the `e.restored` branch to the
    restored wording and only `commit-refused-unrestored` reddens. Every verify-layer
    row stays green throughout, because this is the wiring axis and those are the
    predicate axis."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    exc = make_exc()

    def refuse_merge(*a, **kw):
        raise exc

    monkeypatch.setattr(verify, "merge_branch", refuse_merge)
    summary = engine.run()

    assert summary.paused and summary.escalated == 1 and not summary.crashed
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    assert present in reason
    for phrase in _MERGE_FAILURE_PHRASES:
        if phrase != present:
            assert phrase not in reason
    assert str(exc).splitlines()[0] in reason  # git's own text is passed through
    # branch kept for manual merge — the unit's work is not stranded either way
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")


def test_spec_paths_serialize_relative_to_worktree_or_preserve_absolute_paths():
    """Both spec paths stay portable without rewriting paths the worktree does not own."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/.bmad-loop/runs/run/worktrees/1-1-a"
    task.spec_file = "/repo/.bmad-loop/runs/run/worktrees/1-1-a/_out/spec.md"
    task.dispatched_spec_file = "/repo/.bmad-loop/runs/run/worktrees/1-1-a/_out/dispatched.md"
    assert task.to_dict()["spec_file"] == "_out/spec.md"
    assert task.to_dict()["dispatched_spec_file"] == "_out/dispatched.md"
    # specs living outside the worktree stay absolute
    task.spec_file = "/elsewhere/spec.md"
    task.dispatched_spec_file = "/elsewhere/dispatched.md"
    assert task.to_dict()["spec_file"] == "/elsewhere/spec.md"
    assert task.to_dict()["dispatched_spec_file"] == "/elsewhere/dispatched.md"
    # in-place mode (no worktree) is unchanged
    task.worktree_path = ""
    task.spec_file = "/repo/_out/spec.md"
    task.dispatched_spec_file = "/repo/_out/dispatched.md"
    assert task.to_dict()["spec_file"] == "/repo/_out/spec.md"
    assert task.to_dict()["dispatched_spec_file"] == "/repo/_out/dispatched.md"


# ---------------------------------------------- gh-139 resilient teardown


def _open_unit(project, key="1-1-a", branch_per="story"):
    """Mount a real unit worktree (commits the sprint board first, like every
    direct-open test) and return (unit, run_dir)."""
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {key: "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = open_unit_workspace(
        project.project, project, "test-run", key, "main", branch_per, run_dir
    )
    return unit, run_dir


def _drop_admin_entry(project):
    """Delete git's worktree admin dir under the main repo, reproducing the
    gh-139 post-ENOTEMPTY state where both `git worktree remove` calls fail with
    'is not a working tree'. Exactly one linked worktree is open at call time."""
    admin = list((project.project / ".git" / "worktrees").iterdir())
    assert len(admin) == 1
    shutil.rmtree(admin[0])


def test_close_after_admin_entry_dropped_degrades_not_crashes(project):
    """gh-139 fingerprint: a process the just-ended session left running keeps
    `git worktree remove` from clearing the tree (ENOTEMPTY), and by then git has
    already dropped its admin entry — so the force=True retry fails with 'is not a
    working tree' and the second GitError used to crash the whole run after the
    merge already landed. Teardown now degrades: rmtree+prune reclaim the dir, the
    branch is still deleted, and the failure is reported, not raised."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )

    assert not unit.path.exists()  # rmtree reclaimed the stuck dir
    assert not branch_exists(project.project, unit.branch)  # prune freed it → deleted
    assert len(reports) == 1 and "is not a working tree" in reports[0]


def test_close_degrades_when_branch_delete_fails(project, monkeypatch):
    """The branch-delete tail is the second crash door: a `delete_branch` GitError
    is degraded to a report, not raised, so a merged unit's run still completes."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)

    def boom(*a, **k):
        raise verify.GitError("branch is checked out elsewhere")

    monkeypatch.setattr(verify, "delete_branch", boom)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()  # the worktree itself removed cleanly
    assert len(reports) == 1 and "branch delete failed" in reports[0]


def test_close_dirty_tree_force_retry_is_not_degraded(project):
    """A stray untracked file makes the plain `git worktree remove` refuse; the
    force=True retry clears it. That is the ordinary dirty-tree case, not a
    degradation — no report is emitted and behavior matches today."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    (unit.path / "stray.txt").write_text("dirty\n")  # untracked → plain remove refuses

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()  # force retry handled the dirty tree
    assert not branch_exists(project.project, unit.branch)
    assert reports == []  # NOT a degradation


def test_close_deferred_without_keep_degrades(project, monkeypatch):
    """The DEFERRED, no-keep teardown (success=False) runs the same fallback chain:
    the patch is already captured, so a dropped admin entry degrades to a report
    while the worktree is reclaimed via rmtree+prune — the run continues. (In the
    real gh-139 sequence capture runs before the remove drops the admin entry;
    dropping it up front here would break capture too and flip the close into the
    capture-failure preserve path, so pin capture to its real-life outcome.)"""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)
    monkeypatch.setattr(verify, "capture_diff", lambda *a, **k: "")

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert not unit.path.exists()
    assert not branch_exists(project.project, unit.branch)
    assert len(reports) == 1 and "is not a working tree" in reports[0]


def test_close_capture_failure_preserves_worktree_and_branch(project, monkeypatch):
    """The teardown tail's premise is that a dropped unit's changes are already
    patch-captured — a failed capture (e.g. a #156 git timeout) breaks it, and
    tearing down anyway would destroy the only copy of the unit's work. The
    close must instead preserve the worktree + branch (as if keep_failed) and
    report the degradation."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)

    def boom(*a, **k):
        raise verify.GitError("git diff timed out")

    monkeypatch.setattr(verify, "capture_diff", boom)

    reports: list[str] = []
    patch = close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )

    assert patch is None
    assert unit.path.exists()  # preserved: the worktree holds the only copy
    assert branch_exists(project.project, unit.branch)
    assert len(reports) == 1 and "diff capture failed" in reports[0]


def test_close_capture_failure_frees_shared_branch(project, monkeypatch):
    """branch_per=run: a worktree preserved by a failed capture holds the shared
    run branch, which would collide with every later unit's mount (gh-138). The
    detach_kept handling must apply to this preserve path exactly as it does to
    keep_failed: HEAD detaches, so the branch is mountable elsewhere."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project, branch_per="run")

    def boom(*a, **k):
        raise verify.GitError("git diff timed out")

    monkeypatch.setattr(verify, "capture_diff", boom)

    close_unit_workspace(
        unit,
        success=False,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        detach_kept=True,
        on_teardown_degraded=lambda _msg: None,
    )

    assert unit.path.exists()  # preserved for recovery
    # the shared branch is free again: a sibling worktree can mount it
    sibling = run_dir / "worktrees" / "1-1-b"
    verify.worktree_add(project.project, sibling, unit.branch, create=False)


def test_close_notes_leftover_path_when_rmtree_loses_race(project, monkeypatch):
    """If the writing process recreates files faster than rmtree(ignore_errors)
    can clear them, the dir survives the fallback. The degraded report then names
    the leftover path — the dir lives under the gitignored run dir and is reclaimed
    later by trim_run_dir / clean, so the run still continues."""
    from bmad_loop import workspace
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)  # force both worktree_remove calls to fail
    # rmtree loses the race: the dir survives the fallback (deterministic no-op)
    monkeypatch.setattr(workspace.shutil, "rmtree", lambda *a, **k: None)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert unit.path.exists()  # the no-op rmtree left it in place
    assert len(reports) == 1
    assert str(unit.path) in reports[0] and "still present" in reports[0]


def test_close_double_degradation_reports_both(project, monkeypatch):
    """Both teardown doors can fail in one close: a dropped admin entry degrades
    the worktree removal AND a raising delete_branch degrades the branch deletion.
    Both are reported, in order (worktree first, branch second), no raise."""
    from bmad_loop.workspace import close_unit_workspace

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    def boom(*a, **k):
        raise verify.GitError("branch is checked out elsewhere")

    monkeypatch.setattr(verify, "delete_branch", boom)

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        on_teardown_degraded=reports.append,
    )
    assert len(reports) == 2
    assert "fell back to rmtree+prune" in reports[0]  # worktree-remove degradation first
    assert "branch delete failed" in reports[1]  # branch-delete degradation second


def test_discard_worktree_falls_back_to_rmtree_and_prunes(project):
    """Resume-restart discard: if `git worktree remove` can't clear a stale unit
    worktree (gh-139-style dropped admin entry), fall back to rmtree + prune so the
    same path is free to re-mount on resume, without raising."""
    from bmad_loop.workspace import discard_worktree

    unit, run_dir = _open_unit(project)
    _drop_admin_entry(project)

    discard_worktree(project.project, str(unit.path), unit.branch, run_dir=run_dir)  # no raise

    assert not unit.path.exists()  # rmtree reclaimed the stuck dir
    assert not branch_exists(project.project, unit.branch)  # pruned → deletable


def test_discard_refuses_rmtree_outside_run_worktrees_dir(project, tmp_path):
    """`task.worktree_path` arrives from persisted state (state.json), which can
    be corrupt or hand-edited. git itself refuses to remove a dir that is not a
    worktree — but that very refusal used to hand the path to the rmtree
    fallback, which validates nothing. The fallback must decline any path that
    does not resolve under this run's worktrees dir."""
    from bmad_loop.workspace import discard_worktree

    victim = tmp_path / "not-a-worktree"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n")
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"

    discard_worktree(project.project, str(victim), "", run_dir=run_dir)  # no raise

    assert (victim / "precious.txt").exists()  # confinement guard refused the rmtree


def test_close_refuses_rmtree_outside_run_worktrees_dir(project, tmp_path):
    """close_unit_workspace's rmtree fallback can receive a persisted path too
    (_reopen_unit rebuilds the UnitWorkspace from task.worktree_path on resume).
    A path outside the run's worktrees dir is never rmtree'd, and the degraded
    report says the fallback was refused."""
    from bmad_loop.workspace import UnitWorkspace, Workspace, close_unit_workspace

    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n")
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    unit = UnitWorkspace(
        workspace=Workspace(root=victim, paths=project.rebased(victim)),
        repo_root=project.project,
        branch="bmad-loop/test-run/1-1-a",
        path=victim,
        baseline="",
    )

    reports: list[str] = []
    close_unit_workspace(
        unit,
        success=True,
        keep_failed=False,
        run_dir=run_dir,
        unit_key="1-1-a",
        delete_branch=False,
        on_teardown_degraded=reports.append,
    )

    assert (victim / "precious.txt").exists()  # confinement guard refused the rmtree
    assert len(reports) == 1 and "rmtree refused" in reports[0]


def test_engine_run_completes_when_worktree_remove_always_fails(project, monkeypatch):
    """gh-139 end-to-end: with `git worktree remove` failing on every call, a
    worktree-isolation run still merges the unit to the target and reaches
    run-complete — teardown degrades to a journaled warning instead of crashing
    the run after the work already landed."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)

    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    # the merge still landed on the target branch (main, checked out in the repo)
    assert rev_parse_head(project.project) != head_before
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # admin entry is INTACT here (only the remove call fails), so prune's
    # branch-freeing is load-bearing: after rmtree+prune the branch is deletable
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    kinds = journal_kinds(engine)
    assert "unit-merged" in kinds and "run-complete" in kinds
    assert "worktree-teardown-degraded" in kinds


def test_engine_deferred_teardown_degrades_are_journaled(project, monkeypatch):
    """The DEFERRED (no-keep) close site must wire on_teardown_degraded too: with
    `git worktree remove` always failing, the deferral still finishes and its
    teardown degradation is journaled — so dropping the kwarg from the deferral
    call site would be caught here (only the success path is asserted E2E above)."""

    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    engine, _ = make_engine(
        project,
        _defer_script(project, "1-1-a"),
        policy=wt_policy(keep_failed=False, limits=_NO_DAMP),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    assert "worktree-teardown-degraded" in journal_kinds(engine)


def test_isolated_exclude_degrade_is_journaled_and_notified(project, monkeypatch):
    """#359: `provision_worktree`'s local-exclude write is best-effort, so the sole
    caller has to give the degrade somewhere to land — otherwise a swallowed fault
    silently lets the unit's `git add -A` commit the provisioned tool files.

    The helper is patched at `worktree_flow`'s own `from .install import` binding
    (worktree_flow.py:35) — patching `install._worktree_local_exclude` would not be
    seen here.

    BOTH CHANNELS are asserted. A journal line alone will not do: the shield's
    degrade policy is to SKIP rather than widen (activating over patterns it could
    not copy would shadow the operator's own excludes), and a skip is only
    defensible if the operator finds out — a journal line nobody reads is how the
    provisioned tool files reach a story's merge unnoticed. The path is reachable
    from a transient `GitError` as well as from a write fault.

    Ablations, one per channel: delete the `on_degraded=` lambda at the
    `provision_worktree` call in `run_isolated` and both assertions fail; drop the
    `gates.notify` from `_exclude_degraded` and only the ATTENTION one does."""
    repo = project.project
    gitignore = repo / ".gitignore"
    gitignore.write_text(gitignore.read_text() + ".mcp.json\n", encoding="utf-8")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # gitignored, so the worktree checkout lacks it and provisioning really seeds
    # it — without a seed of some kind provision_worktree short-circuits before the
    # exclude step and the wiring under test is never reached.
    (repo / ".mcp.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        worktree_flow, "_worktree_local_exclude", lambda *a, **k: "boom: read-only .git"
    )

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(worktree_seed=(".mcp.json",)),
    )
    summary = engine.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    entry = next(e for e in engine.journal.entries() if e["kind"] == "worktree-exclude-degraded")
    assert entry["story_key"] == "1-1-a" and entry["error"] == "boom: read-only .git"
    attention = (engine.run_dir / "ATTENTION").read_text(encoding="utf-8")
    assert "worktree exclude degraded: 1-1-a" in attention
    assert "boom: read-only .git" in attention
    # the degrade is a warning, not a stop: the unit still merged back
    assert "unit-merged" in journal_kinds(engine)


def test_resume_remount_survives_discard_remove_failure(project, monkeypatch):
    """The discard fallback's prune is load-bearing: if `git worktree remove` can't
    clear a stale unit worktree on resume-restart (admin entry INTACT — the dir is
    stuck, not the entry), rmtree drops the dir but only the prune clears git's
    admin entry so `git worktree add` can re-mount at the same path. Without the
    prune the re-mount would collide and the unit would defer instead of finishing."""
    from bmad_loop.workspace import open_unit_workspace

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")])
    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1)
    engine.state.tasks["1-1-a"] = task
    task.phase = Phase.DEV_RUNNING
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    engine._save()

    # `git worktree remove` always fails, admin entry left intact → only
    # worktree_prune can free the path for the resume re-mount
    def always_fail(*a, **k):
        raise verify.GitError("worktree remove boom")

    monkeypatch.setattr(verify, "worktree_remove", always_fail)

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1  # re-mounted at the same path and finished
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert "worktree-open-failed" not in journal_kinds(resumed)


def test_spec_paths_serialize_with_posix_separators():
    """Relative spec paths persist with forward slashes (as_posix) so a
    state.json written under one OS reads back identically under another — no
    backslashes leak into the cross-OS state contract."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/wt"
    task.spec_file = "/repo/wt/_out/sub/spec.md"
    task.dispatched_spec_file = "/repo/wt/_out/sub/dispatched.md"
    serialized = task.to_dict()
    assert serialized["spec_file"] == "_out/sub/spec.md"
    assert serialized["dispatched_spec_file"] == "_out/sub/dispatched.md"
    assert "\\" not in serialized["spec_file"]
    assert "\\" not in serialized["dispatched_spec_file"]


# ------------------------------------------------- retry recovery (issue #161)


def test_dev_retry_in_worktree_auto_recovers_instead_of_pausing(project):
    """#161: a mid-drive dev retry inside a unit worktree must auto-recover the
    disposable worktree (parking the attempt's commits on a preserve ref) even
    with rollback_on_failure OFF — never pause with in-place manual-recovery
    instructions aimed at the operator's checkout, which the attempt never
    touched. rollback_on_failure gates isolation="none" recovery only."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})

    engine, _ = make_engine(
        project,
        [
            wt_bad_dev(project, "1-1-a"),
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
        policy=wt_policy(rollback_on_failure=False),
    )
    summary = engine.run()

    assert not summary.paused  # the old behavior: paused for manual recovery
    assert summary.done == 1
    kinds = journal_kinds(engine)
    assert "rollback-manual-required" not in kinds
    assert "rollback-auto" in kinds
    # the bad attempt's commits were parked on a recovery ref, not lost
    preserved = [e for e in engine.journal.entries() if e["kind"] == "attempt-commits-preserved"]
    assert preserved and preserved[0]["ref"].startswith("attempt-preserve/")
    # only the successful attempt's work merged to the target branch
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "bad attempt" not in src
    assert worktree_clean(project.project)


def test_isolated_defer_names_the_earlier_rolled_back_attempt(project):
    """#333: an isolated defer is NOT the only place a story's work can live. The
    first attempt's non-fixable retry rolled back *inside* the unit worktree and
    parked its commits on a shared `attempt-preserve/*` ref; the second attempt
    exhausts the budget and defers with its own work kept on the unit branch. Both
    survive, so the notice must name both — `status --json` already reports the ref,
    and a notice that mentioned only the branch is exactly the "hunt with
    `git log --all`" failure #333 was filed for.

    No `merge --ff-only` line here on purpose: that ref is not fast-forwardable from
    either tree, and offering it would land a discarded attempt on the operator's
    branch.

    Ablations: drop the `if task.preserve_ref:` clause in `_defer_recovery_note`'s
    isolated arm → the both-named assertion fails; drop `preserve_ref=` from the
    isolated `story-deferred` emit → the journal assertion fails; route the isolated
    arm through the in-place tail → the no-command assertion fails."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_bad_dev(project, "1-1-a"), wt_bad_dev(project, "1-1-a")],
        policy=wt_policy(),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    # falsifies the "an isolated unit parks nothing" reading: the in-worktree
    # rollback wrote a real ref, and it is reachable from the MAIN repo because
    # linked worktrees share the common git dir's ref namespace
    ref = task.preserve_ref
    assert ref and ref.startswith(("attempt-preserve/", "refs/attempt-preserve-dirty/"))
    git(project.project, "rev-parse", "--verify", ref)

    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "story deferred: 1-1-a" in attention
    assert "failed work kept on branch `bmad-loop/test-run/1-1-a`" in attention
    assert f"an earlier rolled-back attempt is parked at `{ref}`" in attention
    assert "merge --ff-only" not in attention

    deferred_entry = [e for e in engine.journal.entries() if e["kind"] == "story-deferred"][-1]
    assert deferred_entry["preserve_ref"] == ref


# ------------------------------------ story-declared deferred-work closure (#234)


def _ledger_entry(paths, dw_id: str):
    from bmad_loop import deferredwork

    text = paths.deferred_work.read_text(encoding="utf-8")
    return next(e for e in deferredwork.parse_ledger(text) if e.id == dw_id)


def test_worktree_in_repo_ledger_closure_reaches_the_target_branch(project):
    """An in-repo ledger is rebased into the unit worktree, so the closure rides
    the unit's own commit and arrives on the target branch with the merge (#234)."""
    from conftest import write_ledger

    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )

    summary = engine.run()

    assert summary.done == 1
    entry = _ledger_entry(project, "DW-1")
    assert entry.status.startswith("done") and not entry.open
    assert "resolution: resolved by story 1-1-a" in entry.body
    assert worktree_clean(project.project)
    # #458's carry runs here too, and finds nothing to do: `_apply_done` returns
    # None for a row that is already `done`, so `_mark_done_many` writes nothing at
    # all and no commit is attempted. That is what lets the carry be unconditional
    # rather than needing a tracked/ignored predicate.
    carried = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-close-carried"]
    assert [e["dw_ids"] for e in carried] == [[]]
    assert "story-deferred-close-carry-uncommitted" not in journal_kinds(engine)
    # exactly one annotation — a second close would append a second resolution line
    assert entry.body.count("resolution:") == 1
    # ...and exactly one undo marker beside it. This line read `not in` until #286
    # made the story close REOPENABLE: the commit-boundary rollback now undoes these
    # entries through their own markers instead of restoring the whole document over
    # a concurrent writer, so the marker is permanent and rides the unit's commit to
    # the target branch like the resolution line does. A second marker here would
    # mean the carry re-closed a row the merge had already delivered — the very
    # byte-identity the carry shares `_story_close_operation_id` to preserve.
    assert entry.body.count("resolution-undo:") == 1


def test_gitignored_declared_closure_reaches_the_main_ledger(project):
    """#458 — the fourth producer in the `git add -A` family, and the one whose
    failure now reads as a SUCCESS.

    `_close_declared_deferred` writes `self.workspace.paths.deferred_work`, which
    under isolation is the unit worktree's ledger. With the ledger GITIGNORED —
    the default shape, since the ledger lives in the BMAD artifacts dir — the flip
    lands in the worktree, is skipped in silence by `finalize_commit`'s `git add
    -A` (a worktree-scoped exclude shields every seeded rel), brings nothing over
    with the merge, and dies with the worktree at `close_unit_workspace`. Nothing
    in `_carry_isolated_ledger_writes` carries it: that hook owns the harvest and
    the review-budget follow-up, both APPENDS, and a declared close is neither.

    The fixture encodes that false success: the worktree ledger ends `status: done`
    with the resolution annotation, and the journal carries `story-deferred-closed
    dw_ids=['DW-1']` with no `deferred-close-unmatched`. Without the carry the main
    checkout's ledger still reads `status: open` — the run reports a close it never
    delivered. Ablating the seed (`_ledger_seed` -> `()`) puts the pre-seed behavior
    back: the worktree ledger is ABSENT, `classify` reports the id unmatched, and the
    run journals `deferred-close-unmatched dw_ids=['DW-1']`. Louder, equally lost.

    Tracked is the contrast, not a second defect: the sibling above measures the
    same story against a TRACKED ledger and the close rides the unit commit into
    the merge — no seed is made, so no exclude shields it. The fix therefore needs
    no tracked/ignored predicate, only an idempotent carry (re-applying a close
    the merge already delivered is a no-op, exactly as
    `test_tracked_damped_followup_is_not_refiled_twice_by_the_carry` relies on).

    RED here is the last three assertions: the main checkout must end up with the
    annotation the worktree got. Everything above them holds today and must keep
    holding — the fix delivers the close, it does not stop reporting one.
    """
    ignore_before_commit(project, "deferred-work.md")
    write_ledger(project, {"DW-1": "open"})
    rel = project.deferred_work.relative_to(project.project).as_posix()
    # `check-ignore` is the oracle: a rule that is present is not necessarily
    # effective, and a row that reads the tracked shape by accident is vacuous.
    assert git(project.project, "check-ignore", rel).strip() == rel
    assert not verify.path_tracked(project.project, rel)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )

    summary = engine.run()

    # the false success, in the order an operator meets it
    assert summary.done == 1 and not summary.paused and not summary.crashed
    closed = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-closed"]
    assert [e["dw_ids"] for e in closed] == [["DW-1"]]
    assert "deferred-close-unmatched" not in journal_kinds(engine)
    # the only checkout that ever held the flip is gone
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    entry = _ledger_entry(project, "DW-1")
    assert entry.status.startswith("done") and not entry.open
    assert "resolution: resolved by story 1-1-a" in entry.body


# ------------------------------------------------------- gitignored sprint board


def ignored_sprint(project, statuses: dict[str, str]) -> str:
    """Gitignore the board, then write and commit in that order — `ignored_ledger`'s
    shape (test_sweep.py) for the other seeded artifact.

    Order matters both ways: committing the rule first leaves the `git add -A` below
    with nothing to stage (empty-index commit failure), and writing the board first
    would TRACK it, which is the shape that needs no seed at all. `check-ignore` is
    the oracle — a pattern that is present is not necessarily effective.

    The deliberate opposite of `commit_sprint`, which every other row in this file
    uses: that helper tracks the board, which is precisely why #350 had no coverage.
    The `add -A` is safe here only because the rule is already in the working-tree
    .gitignore, so the board cannot be swept into the commit; a later `add -A` over
    an untracked board would be, and a baseline reset would then delete it.
    """
    ignore_before_commit(project, "sprint-status.yaml")
    write_sprint(project, statuses)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "gitignore the board")
    rel = project.sprint_status.relative_to(project.project).as_posix()
    assert git(project.project, "check-ignore", rel).strip() == rel
    assert not verify.path_tracked(project.project, rel)
    return rel


def test_gitignored_board_under_isolation_survives_verify(project):
    """#350 — the board's absence from a unit worktree is a CRASH, not a lost write.

    `git worktree add` checks out tracked files only, so a gitignored board reaches
    no unit; `_post_dev_state_sync` then advances `self.workspace.paths.sprint_status`
    — that missing file — where `sprintstatus.advance` returns None in silence, and
    `verify_dev` reads the SAME missing file through `story_status`, where
    `sprintstatus.load` raises `SprintStatusError`. cli, operatoractions and the TUI
    all catch that class; engine.py and verify.py do not, so it escapes to `run()`'s
    catch-all and the story takes the whole run down with it.

    Seeding the board removes that structurally: the gate reads the orchestrator's
    own write. Ablating the seed (`_board_seed` -> `()`) puts the pre-fix behavior
    back — `summary.crashed`, `state.crash_error` naming `SprintStatusError`.

    Scoped to no-crash plus a passing verify ON PURPOSE. The worktree copy is
    canonical for the duration of the story (#350's maintainer decision) and the
    main board is advanced separately, by the post-merge carry — which has its own
    rows below. Keeping the two halves on separate oracles is what lets an ablation
    of either redden only its own.
    """
    rel = ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])

    summary = engine.run()

    # crash_error first: it is the field that NAMES the exception class, so an
    # ablation of the seed reddens this row with `SprintStatusError` in the failure
    # message rather than a bare `crashed=True` that any fault would produce.
    assert engine.state.crash_error is None and not summary.crashed
    assert summary.done == 1 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    # the seed was delivered, not merely requested: an undelivered or no-op seed
    # entry names itself in one of these two journals.
    seeded = [
        e
        for e in engine.journal.entries()
        if e["kind"] in ("worktree-seed-skipped", "worktree-seed-dropped")
        and rel in e.get("entries", [])
    ]
    assert seeded == []
    # the unit's work landed and the board stayed the orchestrator's own file
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert not verify.path_tracked(project.project, rel)
    assert worktree_clean(project.project)
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def _board_carry_events(engine, kind="board-advance-carried"):
    return [e for e in engine.journal.entries() if e["kind"] == kind]


def _sprint_carry_commits(project) -> list[str]:
    """Every commit the board carry authored, by subject. Empty is the assertion
    that ``commit_paths`` found nothing to commit — a claim `worktree_clean` alone
    cannot make, since a carry that DID commit also leaves a clean tree."""
    subjects = git(project.project, "log", "--format=%s").splitlines()
    return [s for s in subjects if s.startswith("chore(sprint-status)")]


def test_done_isolated_unit_carries_its_board_advance_after_merge(project):
    """#350's carry half: the story's advance reaches the MAIN board.

    The advance lands on the seeded worktree board, which is shielded from the
    unit's `git add -A` with every other seeded rel, so nothing about it rides the
    merge. Without the carry the main board keeps the story at `ready-for-dev` —
    inside ACTIONABLE_STATUSES — and `_pick_next`, which reads the MAIN board, hands
    finished work back to the next run.

    The `board-advance-carry-uncommitted` row is the expected outcome here, not a
    fault: `git add` refuses an ignored pathspec with rc 1 every time, which is why
    the carry commits best effort. The status on disk is the value.
    """
    rel = ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert summary.done == 1 and task.phase == Phase.DONE and not summary.crashed
    assert task.board_advance_intended == "done"
    assert task.isolated_ledger_carried
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert [(e["target"], e["status"]) for e in _board_carry_events(engine)] == [("done", "done")]
    assert len(_board_carry_events(engine, "board-advance-carry-uncommitted")) == 1
    # the carry writes the board, never tracks it: an ignored path stays ignored
    assert not verify.path_tracked(project.project, rel)
    assert _sprint_carry_commits(project) == []
    assert worktree_clean(project.project)


def test_awaiting_operator_isolated_unit_carries_its_board_advance(project):
    """A park is the other terminal `_post_dev_state_sync` records, and
    `integrate_unit` merges it beside DONE — so it carries beside DONE too.

    `awaiting-operator` sits immediately below `done` in STATUS_ORDER, so this is an
    ordinary forward advance the later `bmad-loop confirm` finishes.
    """
    ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                final_status="awaiting-operator",
                followup_review=False,
                operator_actions=["publish the DNS record"],
            )
        ],
    )

    summary = engine.run()

    task = engine.state.tasks["1-1-a"]
    assert summary.awaiting_operator == 1 and task.phase == Phase.AWAITING_OPERATOR
    assert task.board_advance_intended == "awaiting-operator"
    assert task.isolated_ledger_carried
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert [(e["target"], e["status"]) for e in _board_carry_events(engine)] == [
        ("awaiting-operator", "awaiting-operator")
    ]


def test_tracked_board_carry_is_a_no_op_that_still_reports_itself(project):
    """The common shape: a TRACKED board needs no carry and must not get a commit.

    The worktree's advance is an ordinary modification of a tracked file, which no
    ignore rule masks, so it rides the unit commit through the merge and the main
    board is already at the target when the carry runs. `advance` then returns the
    current status without writing, `commit_paths` finds nothing to commit, and
    `board-advance-carried` names a status the carry did not itself write — an
    ordinary outcome, and the reason the journal carries the landed status rather
    than a wrote/did-not flag it cannot honestly produce.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert [(e["target"], e["status"]) for e in _board_carry_events(engine)] == [("done", "done")]
    # nothing to commit and nothing refused: the merge already delivered the flip
    assert _board_carry_events(engine, "board-advance-carry-uncommitted") == []
    assert _sprint_carry_commits(project) == []
    assert worktree_clean(project.project)


# `advance` cannot report whether it WROTE — a never-regress echo returns the target
# too, which is why the row above is a legitimate ("done", "done"). It can report that
# the row did not REACH the target, and these two rows are that answer's two shapes.
# Both matter because the run tears down the worktree holding the advanced copy on the
# strength of the carry's record: latched as carried, the advance is lost AND the
# journal says it landed. Ablation for both: drop the `_at_or_past` guard and each row
# fails on the `board-advance-carried` assertion, the false success it exists to stop.


def test_board_carry_over_a_vanished_main_row_is_not_journalled_as_carried(project):
    """Shape one: `advance` returns `None` because the story's row is gone. Reachable
    while an isolated session runs — the worktree holds its own seeded copy, so the
    story completes normally and only the carry finds nothing to write to.

    The ROW rather than the whole FILE, and a second row left standing: a main board
    that is missing outright — or left with an empty `development_status` — raises
    `SprintStatusError` out of `advance` itself, which ends the run over the carry's
    shoulder and proves nothing about the carry's own record. `1-1-b` is parked at
    `done` so it holds the map open without being actionable."""
    ignored_sprint(project, {"1-1-a": "ready-for-dev", "1-1-b": "done"})
    inner = wt_dev_effect(project, "1-1-a", followup_review=False)

    def effect(spec):
        result = inner(spec)
        board = project.sprint_status  # the MAIN board, not the worktree's
        kept = [
            ln
            for ln in board.read_text(encoding="utf-8").splitlines(keepends=True)
            if "1-1-a" not in ln
        ]
        board.write_text("".join(kept), encoding="utf-8")
        return result

    engine, _ = make_engine(project, [effect])

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") is None  # premise
    assert _board_carry_events(engine) == []  # no success filed for a carry that isn't
    assert [
        (e["target"], e["status"])
        for e in _board_carry_events(engine, "board-advance-carry-failed")
    ] == [("done", None)]
    assert _sprint_carry_commits(project) == []


def test_board_carry_that_cannot_rewrite_the_row_is_not_journalled_as_carried(project):
    """Shape two, and the one a `None` check alone would miss: the row is THERE and
    `advance` still leaves it below target. `story_status` resolves a quoted key
    through a full YAML parse, `_set_mapping_value`'s line regex then declines it,
    and `advance` returns the row's current status rather than falsely claiming the
    target — a distinction this method has to carry through to its journal."""
    ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    inner = wt_dev_effect(project, "1-1-a", followup_review=False)

    def effect(spec):
        result = inner(spec)
        board = project.sprint_status
        text = board.read_text(encoding="utf-8").replace("1-1-a:", '"1-1-a":')
        board.write_text(text, encoding="utf-8")
        return result

    engine, _ = make_engine(project, [effect])

    summary = engine.run()

    assert summary.done == 1 and not summary.crashed
    assert _board_carry_events(engine) == []
    assert [
        (e["target"], e["status"])
        for e in _board_carry_events(engine, "board-advance-carry-failed")
    ] == [("done", "ready-for-dev")]
    # the premise, stated: the row is readable and still did not move
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "ready-for-dev"
    assert _sprint_carry_commits(project) == []


def test_crashed_post_merge_board_advance_replays_from_its_record(project):
    """The merge-to-carry window, for the payload that reaches it most often.

    A generic story usually records a board advance and NOTHING else, so the resume
    pass reaches it only because the eligibility disjunct names this field — the
    strand the comment above that disjunct warns about, on the ordinary case rather
    than a rare one.
    """
    ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    # durable, and the ONLY payload that can reach the carry for this story
    assert crashed.board_advance_intended == "done"
    assert not crashed.harvested_deferrals
    assert not crashed.story_closes_intended and not crashed.bundle_closes_intended
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "ready-for-dev"

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert adapter.sessions == []  # replayed, not re-driven
    assert "resume-ledger-carry" in journal_kinds(resumed)
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_replayed_board_carry_leaves_an_operators_edit_out_of_its_commit(project):
    """#618's carry hazard, on the one leg its merge pre-flight cannot reach.

    `merge_local` refuses a stray on a protected artifact BEFORE it merges, and that
    refusal is the whole of what keeps `_carry_board_advance`'s pathspec commit from
    taking bytes the run never wrote. `_replay_unlatched_ledger_carries` skips it:
    the re-merge block is guarded on `merged_key not in merged_units`, so a unit whose
    `unit-merged` was already journaled falls straight through to the carry with no
    merge — and therefore no pre-flight — in front of it. The operator's window is the
    crash itself: the host is down, they edit their own checkout, the run comes back.

    `unit-merged` in the crashed run's journal is that leg's precondition and is
    asserted rather than assumed. Without it the resume takes the OTHER branch,
    re-runs the merge, and the pre-flight would have caught the edit after all —
    which is exactly how this row stays disjoint from #618's own witnesses.

    A tracked board's flip rides the merge, so by the time the carry runs it has
    nothing of its own left to write: every byte its commit could take belongs to
    somebody else. That is asserted too, because it is what makes the sweep total
    rather than partial.

    The last assertions read git HISTORY, not the working tree, for the reason
    `_committed_versions` exists: a pathspec carry that swept the edit in leaves the
    tree clean and the file's bytes unchanged on disk, so "the edit is still there"
    passes in the unsafe outcome just as well as in the safe one.
    """
    marker = "# operator: reopened locally, do not ship\n"
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    board = project.sprint_status
    rel = board.relative_to(project.project).as_posix()
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed
    assert "unit-merged" in journal_kinds(engine)
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert crashed.board_advance_intended == "done"
    assert sprintstatus.story_status(board, "1-1-a") == "done"
    assert rel not in verify.dirty_paths(project.project)

    board.write_text(board.read_text(encoding="utf-8") + marker, encoding="utf-8")
    before = board.read_text(encoding="utf-8")

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    # The DAMAGE assertions lead, so that an ablation of the guard reddens this row on
    # the operator's bytes reaching a commit and not on a journal kind going missing.
    # Non-empty first: `any()` over an empty history is False, so a read that found no
    # commits at all would pass the next line for the wrong reason.
    versions = _committed_versions(project, rel)
    assert versions and not any(marker.strip() in v for v in versions)
    assert not any(
        "chore(sprint-status)" in s for s in git(project.project, "log", "--format=%s").splitlines()
    )
    # refused, never repaired: the operator's bytes and the row's status both survive
    assert board.read_text(encoding="utf-8") == before
    assert sprintstatus.story_status(board, "1-1-a") == "done"
    kinds = journal_kinds(resumed)
    assert "resume-ledger-carry" in kinds and "board-advance-carry-foreign-dirt" in kinds


def test_replayed_board_carry_refuses_before_it_overwrites_an_operators_row_edit(project):
    """The same hazard on the one row the commit proof cannot be asked about in time:
    the story's own.

    That proof guards the COMMIT, and `sprint_advance` runs first — so for this row it
    arrives after the evidence it would have judged is already overwritten, and then
    agrees, the board holding exactly HEAD's bytes plus this advance because that is
    what `advance` just made of them. Refusing the commit at that point saves nothing
    either: the operator's status is gone from disk, which is the value `_pick_next`
    reads and the value the next run schedules from. Hence a row check BEFORE the
    write, additive to the proof that still guards every other row.

    The reopened status is `awaiting-operator` for two independent reasons. It sits
    BELOW `done` in STATUS_ORDER, so `advance` really writes over it rather than
    handing back the never-regress echo a same-or-later status would — no write, no
    hazard, nothing to pin. And it is outside ACTIONABLE_STATUSES, so the resumed
    engine does not re-pick the story and drive a MockAdapter with no sessions left.

    `before` is captured AFTER the operator's write, so the row asserts survival of
    exactly their bytes and stays indifferent to how the board happens to be
    serialized. Ablation: drop the pre-advance check and the row is `done` on disk
    with `board-advance-carried` filed over it.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    board = project.sprint_status
    rel = board.relative_to(project.project).as_posix()
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed
    assert "unit-merged" in journal_kinds(engine)
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert crashed.board_advance_intended == "done"
    # the tracked flip rode the merge, so HEAD already holds the target this carry
    # would re-apply: whatever the row says now, somebody else put there.
    assert sprintstatus.story_status(board, "1-1-a") == "done"
    assert rel not in verify.dirty_paths(project.project)

    set_sprint(project, "1-1-a", "awaiting-operator")
    before = board.read_bytes()

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    # The DAMAGE assertions lead: an ablation must redden on the operator's status
    # being overwritten, not on a journal kind going missing.
    assert sprintstatus.story_status(board, "1-1-a") == "awaiting-operator"
    assert board.read_bytes() == before
    kinds = journal_kinds(resumed)
    assert "board-advance-carry-foreign-dirt" in kinds
    # nothing was written, so the event that says the status is on disk would lie
    assert "board-advance-carried" not in kinds
    assert _sprint_carry_commits(project) == []


def test_replayed_board_carry_still_commits_a_crashed_passs_own_advance(project):
    """The regression the row above could cause, and why the guard compares BYTES
    rather than refusing on dirt.

    A pass that advanced the board and died before its commit leaves that advance as
    uncommitted dirt on exactly the path the guard watches — and finishing it is what
    the replay leg is for. A guard that refused on dirt alone would strand it: the
    row's status would keep being right on disk and wrong in every commit, for ever.

    So the state is built, not raced for, and built through `sprintstatus.advance` —
    the same call the carry itself makes — so the bytes under test are the carry's own
    and not a hand-rolled imitation of them. HEAD is moved below the target first,
    because that is what makes the advance a real write rather than the never-regress
    echo a merged tracked board gives.

    Green with the guard ablated as well as with it in place: this row exists to pin
    that the guard costs nothing here, so it is deliberately NOT part of the ablation
    set. The row above is.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    board = project.sprint_status
    rel = board.relative_to(project.project).as_posix()
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed
    assert "unit-merged" in journal_kinds(engine)
    assert not load_state(engine.run_dir).tasks["1-1-a"].isolated_ledger_carried

    # HEAD below the target, so the carry has real work; then the dead pass's own
    # write on top of it, uncommitted — the exact shape a crash leaves behind.
    set_sprint(project, "1-1-a", "ready-for-dev")
    git(project.project, "commit", "-q", "-m", "operator reopens the row", "--", rel)
    sprintstatus.advance(board, "1-1-a", "done")
    assert rel in verify.dirty_paths(project.project)

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed
    # As above, the substantive assertions lead: a guard that refused on dirt alone
    # has to redden this row on the carry never reaching a commit, not on a journal
    # kind. Non-empty first, for the reason the sibling row spells out.
    versions = _committed_versions(project, rel)
    assert versions and "1-1-a: done" in versions[0]
    assert any(
        s == "chore(sprint-status): carry 1-1-a to done"
        for s in git(project.project, "log", "--format=%s").splitlines()
    )
    assert rel not in verify.dirty_paths(project.project)
    assert sprintstatus.story_status(board, "1-1-a") == "done"
    kinds = journal_kinds(resumed)
    assert "board-advance-carried" in kinds
    assert "board-advance-carry-foreign-dirt" not in kinds
    assert "board-advance-carry-uncommitted" not in kinds


def test_replayed_board_carry_with_a_deleted_board_journals_failed_not_a_crash(project):
    """A tracked board DELETED while the host was down, on the replay leg — the
    shape where the carry's own docstring promise (`board-advance-carry-failed`
    for "a board that is gone") and its probes' behavior used to disagree.

    Deletion is dirt (` D` in `dirty_paths`), so it turns proving ON — and the
    pre-advance row probe's live read (`sprint_story_status` → `load`) RAISES
    `SprintStatusError` over a missing file, where `advance`, whose behavior the
    no-catch rationale was written against, returns None. That raise escaped
    `_replay_unlatched_ledger_carries` (which catches only `RunPaused`), so every
    resume died before `_loop()` — the one caller every resume runs through, on a
    shape a retry cannot repair. The carry now refuses a missing board up front,
    on the journal row already named for it.

    Driven through `_replay_unlatched_ledger_carries` itself so the raise, when
    the guard is ablated, is the failure graded — a full `resumed.run()` would
    also trip over the missing board in `_pick_next` and muddy the axis.

    Ablation: drop the `board.is_file()` guard and this row dies on
    `SprintStatusError: sprint status file not found` at the replay call — the
    measured pre-fix behavior — while the foreign-dirt and own-advance siblings
    above stay green, their boards being present in every scene."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    board = project.sprint_status
    rel = board.relative_to(project.project).as_posix()
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed
    assert "unit-merged" in journal_kinds(engine)
    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert crashed.board_advance_intended == "done"

    board.unlink()  # the operator's window is the crash itself
    assert verify.dirty_paths(project.project).get(rel, "").strip() == "D"  # proving turns ON

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    resumed._replay_unlatched_ledger_carries()  # must not raise

    kinds = journal_kinds(resumed)
    assert "board-advance-carry-failed" in kinds
    assert "board-advance-carried" not in kinds
    assert not board.exists()  # refused, never recreated or half-written


def test_board_advance_carried_twice_by_a_crash_before_its_latch_is_a_no_op(project):
    """The carry-to-latch window: the resume replays a carry that already ran.

    That is safe for the board because `advance` never regresses — the second
    application reads `done` and returns it unwritten. This is the window that pins
    call-site latching: a latch moved inside the hook would already be durable here
    and the resume would never replay at all.
    """
    ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a", followup_review=False)])
    crash_at_merge_back(engine, after="carry")

    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    # the carry itself completed before the host died
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    before = project.sprint_status.read_bytes()

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert "resume-ledger-carry" in journal_kinds(resumed)
    # byte-identical: a re-applied advance rewrites nothing, comments included
    assert project.sprint_status.read_bytes() == before
    assert [(e["target"], e["status"]) for e in _board_carry_events(resumed)] == [
        ("done", "done"),
        ("done", "done"),
    ]
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_unmerged_terminal_unit_does_not_replay_a_board_advance(project):
    """Merge evidence still gates the replay now that nearly every story has a
    payload.

    Before #350 a DONE story with no ledger write fell out of the eligibility
    disjunct and never reached the merge-evidence check at all; the board record
    puts it there on the ordinary path, so the guard that used to be shadowed is now
    the only thing standing between a terminal phase and a carry onto a branch that
    never landed. A tracked board makes the refusal legible: the carry would advance
    it, so `ready-for-dev` is proof the body did not run.
    """
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [])
    engine.state.target_branch = "main"
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
        board_advance_intended="done",
    )
    engine.state.tasks[task.story_key] = task

    engine._replay_unlatched_ledger_carries()

    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "ready-for-dev"
    assert task.isolated_ledger_carried is False
    assert "resume-ledger-carry" not in journal_kinds(engine)
    assert _board_carry_events(engine) == []


def test_a_park_confirms_only_after_its_board_advance_is_carried(project):
    """`confirm` reads the COMMITTED board, so the crash window is visible to it.

    In the merge-to-carry window the park record and its spec have landed on the
    target while the main board still says `ready-for-dev`, and `confirm` refuses
    on exactly that disagreement rather than flipping a board on the record's word.
    The replay is what makes the story confirmable — this is the operator-facing
    consequence of stranding the carry, and it lives here rather than in
    test_operatoractions.py because only the engine can reach the window.
    """
    from bmad_loop import operatoractions

    ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(
                project,
                "1-1-a",
                final_status="awaiting-operator",
                followup_review=False,
                operator_actions=["publish the DNS record"],
            )
        ],
    )
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed

    (parked,) = operatoractions.resolve(project.project, project)
    assert parked.spec_status == "awaiting-operator"  # the spec rode the merge
    assert parked.board_status == "ready-for-dev"  # the board did not
    assert not parked.confirmable
    assert parked.committed_drift() == "the board now says ready-for-dev"

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    assert resumed.run().awaiting_operator == 1

    (parked,) = operatoractions.resolve(project.project, project)
    assert parked.board_status == "awaiting-operator"
    assert parked.committed_drift() is None
    assert parked.confirmable


def test_a_gitignored_board_story_finished_by_one_run_is_not_re_picked_by_the_next(project):
    """#350 end to end, across the run boundary that is the only place it shows.

    Both halves have to hold for this to pass, and neither can stand in for the
    other. WITHOUT THE SEED run 1 does not finish at all: the worktree has no board,
    `verify_dev` reads that missing file through `story_status`, and
    `SprintStatusError` takes the run down. WITHOUT THE CARRY run 1 finishes
    perfectly and the damage is invisible until run 2 — inside a single run
    `state.tasks` shields a finished story from `_pick_next` no matter what the board
    says, so a fresh RunState reading the MAIN board is the only thing that can tell
    a carried advance from a lost one.

    That makes run 2 the discriminating assertion of the whole bundle: it is
    `_pick_next`'s own reader, against `ACTIONABLE_STATUSES`, over the file the
    orchestrator actually kept. A lost advance leaves `ready-for-dev` there, and the
    next unattended run hands finished work back to a dev session.

    The board's FULL text is asserted, not just the story's status: the carry runs
    through `_set_mapping_value`, so this doubles as #366's oracle at the top layer —
    one value moved, `last_updated`'s unquoted `01-06-2026 10:00` (spaces and all)
    untouched, no line fabricated. A `yaml.safe_load` comparison would see none of
    that.
    """
    rel = ignored_sprint(project, {"1-1-a": "ready-for-dev"})
    parked_board = project.sprint_status.read_text()
    first_engine, first_adapter = make_engine(
        project, [wt_dev_effect(project, "1-1-a", followup_review=False)]
    )

    first = first_engine.run()

    # run 1: the seed's half — it completes rather than crashing on the missing board
    assert first_engine.state.crash_error is None and not first.crashed
    assert first.done == 1 and not first.paused
    assert first_engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert len(first_adapter.sessions) == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # run 1: the carry's half — the main board moved, and ONLY where it should have
    assert project.sprint_status.read_text() == parked_board.replace(
        "1-1-a: ready-for-dev", "1-1-a: done"
    )
    assert not verify.path_tracked(project.project, rel)  # still git's to refuse
    assert worktree_clean(project.project)

    # Run 2 is a fresh RunState over the same project: nothing shields the story now
    # except the board itself.
    second_engine, second_adapter = make_engine(project, [], run_id="test-run-2")

    second = second_engine.run()

    assert second_adapter.sessions == []  # never re-picked, so never re-driven
    assert second.done == 0 and not second.crashed and not second.paused
    assert second_engine.state.tasks == {}
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert project.sprint_status.read_text() == parked_board.replace(
        "1-1-a: ready-for-dev", "1-1-a: done"
    )


def test_crashed_post_merge_story_close_replays_from_its_record(project):
    """A story whose ONLY ledger write is a declared close has every other carry
    payload empty, so the resume pass has to name this one to reach it."""
    ignore_before_commit(project, "deferred-work.md")
    write_ledger(project, {"DW-1": "open"})
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    # only the new disjunct can reach the carry
    assert crashed.story_closes_intended == ["DW-1"]
    assert not crashed.harvested_deferrals
    assert _ledger_entry(project, "DW-1").open

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([])
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    assert adapter.sessions == []
    assert "resume-ledger-carry" in journal_kinds(resumed)
    entry = _ledger_entry(project, "DW-1")
    assert entry.status.startswith("done") and not entry.open
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_a_re_armed_story_does_not_carry_a_withdrawn_declaration(project, monkeypatch):
    """The record is re-derived at every commit boundary, and that is the whole of
    its staleness guard.

    `_close_declared_deferred` reads `closes_deferred:` LIVE and reassigns
    `story_closes_intended` before its own early return, so it needs no
    `_dev_phase` clear: DONE is reachable only
    through `_finalize_commit_phase`, which always re-enters the producer. A human
    who resolves an escalation by WITHDRAWING the declaration must not have the
    abandoned attempt's ids closed on their behalf — the exact stale-snapshot case
    `_declared_deferred_ids` reads live to avoid.
    """
    ignore_before_commit(project, "deferred-work.md")
    write_ledger(project, {"DW-1": "open"})
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )
    real_finalize = verify.finalize_commit

    def commit_fails(*_a, **_k):
        raise verify.GitError("simulated commit failure")

    monkeypatch.setattr(verify, "finalize_commit", commit_fails)

    assert engine.run().paused

    escalated = load_state(engine.run_dir).tasks["1-1-a"]
    assert escalated.phase == Phase.ESCALATED
    assert escalated.story_closes_intended == ["DW-1"]  # recorded, and now stale
    # `_restore_deferred_closes` put the worktree ledger back; main never had it
    assert _ledger_entry(project, "DW-1").open

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    assert (
        runs.rearm_escalation(
            engine.run_dir, "1-1-a", isolated_redrive=True, resolution_recorded=True
        )
        == "1-1-a"
    )

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter(
            # the resolve outcome: the story no longer claims to close anything
            [wt_dev_effect(project, "1-1-a", followup_review=False)]
        ),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    assert _ledger_entry(project, "DW-1").open  # the withdrawn id stays open
    assert not resumed.state.tasks["1-1-a"].story_closes_intended
    carried = [e for e in resumed.journal.entries() if e["kind"] == "story-deferred-close-carried"]
    assert [e["dw_ids"] for e in carried] == []


def test_a_replayed_commit_still_records_the_story_close(project, monkeypatch):
    """Record the DECLARED ids, never the ones `mark_done_many` actually flipped.

    A host loss after `_close_declared_deferred` wrote the close but before the
    DONE save leaves the phase at the COMMITTING that was already persisted, and
    the resume arm re-enters `_finalize_commit_phase` — which re-runs the producer
    against a worktree ledger that ALREADY reads `done`. `classify` then reports
    every id `already_done` and `marked` is EMPTY. A record derived from `marked`
    is therefore never made on that replay, the carry finds an empty payload, and
    `close_unit_workspace` deletes the only copy — the exact defect `e88776a`
    fixed for the damped follow-up, arriving through a different door.

    Non-unwinding is the whole point: `_restore_deferred_closes` is neutralised
    (a SIGKILL runs no except arm) so the close survives on disk, and the durable
    state.json is put back over whatever `run()`'s unwind-`finally` wrote.
    """
    ignore_before_commit(project, "deferred-work.md")
    write_ledger(project, {"DW-1": "open"})
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a", followup_review=False, closes_deferred=["DW-1"])],
    )
    snap: dict = {}

    def host_loss(*_a, **_k):
        # nothing has saved since the COMMITTING advance, so these bytes ARE the
        # durable state at the instant of the kill — the record is memory-only
        snap["state"] = (engine.run_dir / "state.json").read_bytes()
        raise RuntimeError("host died after the declared close, before the DONE save")

    monkeypatch.setattr(verify, "finalize_commit", host_loss)
    monkeypatch.setattr(type(engine), "_restore_deferred_closes", lambda self, task, s: None)

    assert engine.run().crashed

    monkeypatch.undo()  # the host is back: the resume commits and unwinds normally

    worktree = Path(engine.state.tasks["1-1-a"].worktree_path)
    # the close exists, but only inside the unit worktree's gitignored ledger
    assert not _ledger_entry(project.rebased(worktree), "DW-1").open
    assert _ledger_entry(project, "DW-1").open

    (engine.run_dir / "state.json").write_bytes(snap["state"])
    durable = load_state(engine.run_dir).tasks["1-1-a"]
    assert durable.phase == Phase.COMMITTING
    assert not durable.story_closes_intended  # never reached disk — the replay re-derives it

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter([]),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.crashed and not summary.paused
    # the replay's own close flipped nothing (already done in the worktree) and the
    # carry still delivered, because the record is the declaration, not the receipt
    assert not worktree.is_dir()
    entry = _ledger_entry(project, "DW-1")
    assert entry.status.startswith("done") and not entry.open
    assert "resolution: resolved by story 1-1-a" in entry.body


# ------------------------------------------- remount reclaim: orphan preservation


def _open_args(project, key="1-1-a", branch_per="story"):
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    return (project.project, project, "test-run", key, "main", branch_per, run_dir)


def _dirty_refs(project) -> list[str]:
    out = git(
        project.project, "for-each-ref", "--format=%(refname)", "refs/attempt-preserve-dirty/"
    )
    return out.splitlines()


def _commit_project(project, message: str) -> str:
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", message)
    return rev_parse_head(project.project)


def test_remount_parks_dirty_orphan_before_reclaim(project):
    """An orphan the isolation flip left standing is force-removed by the remount's
    reclaim; its UNCOMMITTED state — a tracked edit and a run-created untracked file
    — is parked under ``refs/attempt-preserve-dirty/<run>-<head>-orphan`` first and
    the callback names the ref. A second orphaning of the same HEAD probes to
    ``-r2`` rather than overwriting the first snapshot.

    Ablation: drop the ``_preserve_orphan_state`` call and the remount still
    succeeds but ``preserved == []`` and no ref holds either file.
    """
    from bmad_loop.workspace import open_unit_workspace

    (project.project / "tracked.txt").write_text("v1\n")
    first, _ = _open_unit(project)  # commits tracked.txt with the sprint board
    (first.path / "tracked.txt").write_text("edited in the orphan\n")
    (first.path / "created.txt").write_text("run-created\n")
    orphan_head = rev_parse_head(first.path)

    preserved: list[tuple[str, str]] = []
    second = open_unit_workspace(
        *_open_args(project), on_orphan_preserved=lambda p, r: preserved.append((p, r))
    )

    assert second.path == first.path and second.path.is_dir()
    assert (second.path / "tracked.txt").read_text() == "v1\n"  # a fresh checkout
    assert not (second.path / "created.txt").exists()
    ref = f"refs/attempt-preserve-dirty/test-run-{orphan_head[:8]}-orphan"
    assert preserved == [(str(first.path), ref)]
    assert verify.ref_exists(project.project, ref)
    assert git(project.project, "show", f"{ref}:tracked.txt") == "edited in the orphan"
    assert git(project.project, "show", f"{ref}:created.txt") == "run-created"
    assert git(project.project, "rev-parse", f"{ref}^") == orphan_head  # parented at HEAD
    # the family scm.preserve_keep bounds — one ref, keep=1, nothing over budget
    assert verify.prune_preserve_dirty_refs(project.project, 1) == []

    (second.path / "created.txt").write_text("second orphaning\n")
    preserved.clear()
    open_unit_workspace(
        *_open_args(project), on_orphan_preserved=lambda p, r: preserved.append((p, r))
    )
    assert preserved == [(str(first.path), f"{ref}-r2")]
    assert git(project.project, "show", f"{ref}:created.txt") == "run-created"  # untouched
    assert git(project.project, "show", f"{ref}-r2:created.txt") == "second orphaning"
    assert len(_dirty_refs(project)) == 2  # both in the family preserve_keep bounds


def test_remount_over_clean_orphan_parks_nothing(project):
    """A clean orphan (tree == HEAD) is reclaimed silently: no ref, no callback."""
    from bmad_loop.workspace import open_unit_workspace

    first, _ = _open_unit(project)
    preserved: list[tuple[str, str]] = []
    second = open_unit_workspace(
        *_open_args(project), on_orphan_preserved=lambda p, r: preserved.append((p, r))
    )
    assert second.path == first.path and second.path.is_dir()
    assert preserved == []
    assert _dirty_refs(project) == []


def test_remount_over_plain_directory_never_snapshots_the_project_tree(project):
    """The run dir lives INSIDE the project checkout, so a plain (non-worktree)
    directory at the mount path must not have git run in it: `status`/`add` there
    would address the PROJECT's own working tree and park the operator's edits as
    the orphan's. The guard is ``verify.worktree_is_registered``; a failing guard
    falls through to the reclaim exactly as before.

    Ablation: drop the ``worktree_is_registered`` half of the guard and the dirty
    project checkout is snapshotted — ``_dirty_refs`` is non-empty and holds
    ``operator.txt``.
    """
    from bmad_loop.workspace import open_unit_workspace

    (project.project / "tracked.txt").write_text("v1\n")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    run_dir = project.project / ".bmad-loop" / "runs" / "test-run"
    plain = run_dir / "worktrees" / "1-1-a"
    plain.mkdir(parents=True)
    (plain / "leftover.txt").write_text("rmtree residue\n")
    # the operator's own uncommitted work in the project checkout
    (project.project / "tracked.txt").write_text("operator edit\n")
    (project.project / "operator.txt").write_text("operator untracked\n")

    preserved: list[tuple[str, str]] = []
    unit = open_unit_workspace(
        *_open_args(project), on_orphan_preserved=lambda p, r: preserved.append((p, r))
    )

    assert unit.path == plain.resolve() and unit.path.is_dir()
    assert not (unit.path / "leftover.txt").exists()  # reclaimed as before
    assert preserved == []
    assert _dirty_refs(project) == []
    # the project tree was neither read as the orphan nor touched
    assert (project.project / "tracked.txt").read_text() == "operator edit\n"
    assert (project.project / "operator.txt").read_text() == "operator untracked\n"


def test_remount_refuses_when_orphan_snapshot_fails(project, monkeypatch):
    """A capture failure over a dirty orphan is a gate (#340): the remount raises
    ``GitError`` and the orphan is left standing, dirty files intact, for manual
    recovery — never force-removed past work that could not be parked.

    Ablation: swallow the ``snapshot_worktree`` failure in ``_preserve_orphan_state``
    (``except GitError: return``) and the remount succeeds over the orphan — no
    ``GitError``, ``created.txt`` gone.
    """
    from bmad_loop.workspace import open_unit_workspace

    first, _ = _open_unit(project)
    (first.path / "created.txt").write_text("run-created\n")

    def boom(*a, **k):
        raise verify.GitError("commit-tree: disk says no")

    monkeypatch.setattr(verify, "snapshot_worktree", boom)

    with pytest.raises(verify.GitError, match="left standing.*disk says no"):
        open_unit_workspace(*_open_args(project))

    assert first.path.is_dir()
    assert (first.path / "created.txt").read_text() == "run-created\n"
    assert verify.worktree_is_registered(project.project, first.path)
    assert _dirty_refs(project) == []


def test_engine_remount_journals_orphan_preservation(project):
    """Engine wiring: the orphan snapshot the open parks is journaled as
    ``isolation-flip-orphan-preserved`` with the worktree and ref, so an operator
    reading the run's journal can find the recovery ref next to the
    ``isolation-flip-orphaned-worktree`` record that named the orphan."""
    orphan, _ = _open_unit(project)
    (orphan.path / "created.txt").write_text("run-created\n")

    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1
    entries = [
        e for e in engine.journal.entries() if e["kind"] == "isolation-flip-orphan-preserved"
    ]
    assert len(entries) == 1
    assert entries[0]["story_key"] == "1-1-a"
    assert entries[0]["worktree"] == str(orphan.path)
    assert entries[0]["ref"].startswith("refs/attempt-preserve-dirty/test-run-")
    assert git(project.project, "show", f"{entries[0]['ref']}:created.txt") == "run-created"


# ------------------------------------------- run-branch remount catches up to base


def test_run_branch_remount_fast_forwards_to_an_advanced_base(project):
    """branch_per=run: a run branch whose tip is an ancestor of the (advanced)
    pinned base is fast-forwarded to the base before the mount — the isolation-flip
    shape where a story landed in place on the target while the run branch was
    unmounted. The mount and the baseline come up at the base.

    Ablation: drop the ``reset_branch_if_tip`` fast-forward arm and the mount comes
    up at the stale run tip (``HEAD == old_tip``, not the advanced base).
    """
    from bmad_loop.workspace import discard_worktree, open_unit_workspace

    first, run_dir = _open_unit(project, branch_per="run")
    old_tip = rev_parse_head(first.path)
    discard_worktree(project.project, str(first.path), "", run_dir=run_dir)  # flip away
    (project.project / "landed-in-place.txt").write_text("story committed on main\n")
    advanced = _commit_project(project, "story landed in place")
    assert advanced != old_tip

    second = open_unit_workspace(*_open_args(project, branch_per="run"))

    assert rev_parse_head(second.path) == advanced
    assert git(project.project, "rev-parse", f"refs/heads/{second.branch}") == advanced
    assert second.baseline == advanced
    assert (second.path / "landed-in-place.txt").exists()


def test_run_branch_remount_merges_a_diverged_base(project):
    """branch_per=run, diverged: the run branch carries a unit the target lacks
    AND the target advanced without the run tip. The remount mounts at the tip and
    merges the base into the run branch inside the fresh mount; HEAD is a merge
    commit whose parents are exactly the run tip and the base, and the returned
    baseline is that merge commit (the tree the session starts from).

    Ablation: drop the ``catch_up_base`` merge and HEAD stays at the run tip with
    a single parent and no ``target.txt``.
    """
    from bmad_loop.workspace import discard_worktree, open_unit_workspace

    first, run_dir = _open_unit(project, branch_per="run")
    (first.path / "unit.txt").write_text("landed on the run branch\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-q", "-m", "unit on run branch")
    run_tip = rev_parse_head(first.path)
    discard_worktree(project.project, str(first.path), "", run_dir=run_dir)
    (project.project / "target.txt").write_text("advanced without the run tip\n")
    base = _commit_project(project, "target advances")

    second = open_unit_workspace(*_open_args(project, branch_per="run"))

    head = rev_parse_head(second.path)
    parents = git(second.path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert set(parents) == {run_tip, base}
    assert second.baseline == head
    assert git(project.project, "rev-parse", f"refs/heads/{second.branch}") == head
    assert (second.path / "unit.txt").exists() and (second.path / "target.txt").exists()
    assert worktree_clean(second.path)


def test_run_branch_remount_refuses_a_conflicting_base(project):
    """branch_per=run, diverged with a content conflict: the catch-up merge is
    aborted, the just-created mount is dropped, the run branch tip is unchanged,
    and ``GitError`` names the refusal — an operator must reconcile.

    Ablation: swallow the merge failure (``except GitError: pass`` around the
    catch-up) and the open returns a mounted worktree — no ``GitError``.
    """
    from bmad_loop.workspace import discard_worktree, open_unit_workspace

    (project.project / "conflict.txt").write_text("base\n")
    first, run_dir = _open_unit(project, branch_per="run")
    (first.path / "conflict.txt").write_text("run branch\n")
    git(first.path, "commit", "-q", "-am", "run side")
    run_tip = rev_parse_head(first.path)
    discard_worktree(project.project, str(first.path), "", run_dir=run_dir)
    (project.project / "conflict.txt").write_text("target\n")
    _commit_project(project, "target side")

    with pytest.raises(verify.GitError, match="diverged from main"):
        open_unit_workspace(*_open_args(project, branch_per="run"))

    assert not first.path.exists()
    assert first.path not in [p.resolve() for p in worktree_list(project.project)]
    assert git(project.project, "rev-parse", f"refs/heads/{first.branch}") == run_tip


def test_run_branch_remount_after_squash_integration_merges_clean(project):
    """The diverged arm is the NORMAL serial-unit shape under
    ``merge_strategy = "squash"``: the target receives a squash commit that does not
    contain the run tip. Identical content on both sides merges clean, so the next
    unit mounts on a merge commit holding both histories with no conflict."""
    from bmad_loop.workspace import discard_worktree, open_unit_workspace

    first, run_dir = _open_unit(project, branch_per="run")
    (first.path / "unit.txt").write_text("landed on the run branch\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-q", "-m", "unit on run branch")
    run_tip = rev_parse_head(first.path)
    discard_worktree(project.project, str(first.path), "", run_dir=run_dir)
    git(project.project, "merge", "--squash", "-q", run_tip)
    squashed = _commit_project(project, "squash of the unit")

    second = open_unit_workspace(*_open_args(project, branch_per="run"))

    parents = git(second.path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert set(parents) == {run_tip, squashed}
    assert worktree_clean(second.path)
    assert git(second.path, "diff", "--stat", squashed, "HEAD") == ""  # same tree


@pytest.mark.parametrize("strategy", ["ff", "merge", "squash"])
def test_run_branch_serial_units_stay_green_under_every_strategy(project, strategy):
    """Regression fence for the catch-up: two serial units under ``branch_per=run``
    integrate under all three strategies and main ends with both changes."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
            wt_dev_effect(project, "1-2-b"),
            wt_review_effect(project, "1-2-b", clean=True),
        ],
        policy=wt_policy(branch_per="run", merge_strategy=strategy),
    )
    summary = engine.run()

    assert summary.done == 2 and not summary.paused and not summary.crashed
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "change for 1-2-b" in src
    assert "worktree-open-failed" not in journal_kinds(engine)


# --------------------------------------- remount refuses a branch held elsewhere


def test_story_remount_refuses_a_branch_checked_out_at_a_foreign_path(project, tmp_path):
    """`reset_branch_if_tip` is ``update-ref`` — a ref compare-and-swap that does not
    care which worktree has the branch checked out. When the story branch is held
    by a worktree OTHER than this unit's deterministic mount path (here: the
    operator ``git worktree move``d the retained recovery mount), the reset would
    move the ref under that checkout — its files and index still at the old tip —
    and the following ``worktree_add`` would fail on the held branch anyway. The
    remount refuses BEFORE any mutation: ``GitError`` names the branch and the
    foreign path, the branch tip is unchanged, the foreign checkout's HEAD still
    equals it, no preserve ref was written, and nothing was mounted at ``wt``.

    The branch held only by the orphan AT the mount path keeps remounting fine —
    `test_open_unit_workspace_reclaims_the_orphan_holding_its_mount_path` and
    `test_story_remount_preserves_named_tip_and_restarts_from_pinned_base` grade
    that arm.

    Ablation: drop the `_refuse_foreign_checkout` call in `open_unit_workspace` and
    this reddens — `GitError` still arrives (from ``worktree add``), but the branch
    ref has already been reset to the pinned base and the preserve ref written.
    """
    from bmad_loop.workspace import open_unit_workspace

    first, _run_dir = _open_unit(project, branch_per="story")
    (first.path / "attempt.txt").write_text("committed on the attempt\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-q", "-m", "story attempt")
    tip = rev_parse_head(first.path)
    foreign = tmp_path / "moved-recovery-mount"
    git(project.project, "worktree", "move", str(first.path), str(foreign))
    assert not first.path.exists()
    (project.project / "advanced.txt").write_text("new base\n")
    pinned = _commit_project(project, "base advances")

    with pytest.raises(verify.GitError, match=rf"{first.branch}.*checked out at .*moved-recovery"):
        open_unit_workspace(*_open_args(project, branch_per="story"))

    assert git(project.project, "rev-parse", f"refs/heads/{first.branch}") == tip
    assert rev_parse_head(foreign) == tip
    assert tip != pinned
    assert git(project.project, "for-each-ref", "refs/attempt-preserve/") == ""
    assert not first.path.exists()
    assert first.path not in [p.resolve() for p in worktree_list(project.project)]


def test_run_branch_remount_refuses_a_fast_forward_under_a_foreign_checkout(project, tmp_path):
    """Same hazard on the `branch_per=run` arm: the fast-forward would fire (run tip
    is an ancestor of the advanced base) but the run branch is checked out at a
    path other than this unit's mount. The single occupancy check refuses before
    the ref move: the run branch stays at its tip, the foreign checkout's HEAD
    still equals it, and nothing was mounted.

    Ablation: drop the `_refuse_foreign_checkout` call and the run branch is
    fast-forwarded to the advanced base before ``worktree add`` refuses — the
    ``rev-parse`` assertion reddens.
    """
    from bmad_loop.workspace import open_unit_workspace

    first, _run_dir = _open_unit(project, branch_per="run")
    old_tip = rev_parse_head(first.path)
    foreign = tmp_path / "moved-recovery-mount"
    git(project.project, "worktree", "move", str(first.path), str(foreign))
    (project.project / "landed-in-place.txt").write_text("story committed on main\n")
    advanced = _commit_project(project, "story landed in place")
    assert advanced != old_tip

    with pytest.raises(verify.GitError, match=rf"{first.branch}.*checked out at .*moved-recovery"):
        open_unit_workspace(*_open_args(project, branch_per="run"))

    assert git(project.project, "rev-parse", f"refs/heads/{first.branch}") == old_tip
    assert rev_parse_head(foreign) == old_tip
    assert not first.path.exists()
    assert first.path not in [p.resolve() for p in worktree_list(project.project)]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="win32 strips trailing spaces at the API layer, so the shape cannot be registered",
)
def test_branch_checkout_path_keeps_a_foreign_checkouts_trailing_space(project):
    """The occupancy guard exempts the unit's OWN mount, so the reader must not hand it
    a foreign path that has been trimmed INTO that spelling.

    `branch_checkout_path` read `for-each-ref --format=%(worktreepath)` through
    `_git_out`, which returns `stdout.strip()`. A worktree registered at the unit's
    deterministic mount path PLUS a trailing space came back as the bare mount path,
    compared EQUAL to `wt` in `_refuse_foreign_checkout`, and was exempted as if it were
    this unit's own orphan. The ref then moved under a live foreign checkout — files and
    index left at the old tip — its tree went spuriously dirty, and `worktree add` failed
    on the held branch anyway: precisely the harm the guard was added to prevent, WITH the
    guard present. The function's own docstring already promised the opposite ("git's
    registered spelling, un-canonicalized"), so this is the promise being kept.

    The error could only ever go that unsafe way. `safe_segment` rstrips `". "` from every
    segment we compose, so our own mount path can never end in whitespace and a spurious
    REFUSE is unreachable; the negative control below pins that half.

    The shape is produced the same way the two sibling rows above produce a foreign
    checkout — a deliberate operator `git worktree move` — with a destination one space
    longer than the mount. git registers and reports that spelling verbatim.

    Three claims, because the first two alone cannot show the harm: the returned spelling
    keeps its space, it is therefore NOT equal to the mount path, and the remount refuses
    BEFORE any mutation — the branch tip is unchanged and the foreign checkout still
    holds it. Pre-fix a `GitError` still arrived (from `worktree add`), which is why the
    `match=` names the guard's own sentence and the tip assertion stands behind it.

    Ablation: revert the read to `_git_out` and this reddens on the returned spelling,
    with the unchanged-tip assertion reddening behind it (the exempted branch is reset to
    the pinned base before `worktree add` refuses).
    """
    from bmad_loop.workspace import open_unit_workspace

    first, _run_dir = _open_unit(project, branch_per="story")
    (first.path / "attempt.txt").write_text("committed on the attempt\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-q", "-m", "story attempt")
    tip = rev_parse_head(first.path)
    # the unit's own deterministic mount path plus ONE trailing space: the whole point is
    # that `.strip()` collapses this spelling onto the path the guard exempts
    foreign = Path(f"{first.path} ")
    git(project.project, "worktree", "move", str(first.path), str(foreign))
    assert foreign.is_dir() and not first.path.exists()
    (project.project / "advanced.txt").write_text("new base\n")
    pinned = _commit_project(project, "base advances")
    assert tip != pinned

    holder = verify.branch_checkout_path(project.project, first.branch)

    assert holder is not None
    assert str(holder) == f"{first.path} "  # git's registered spelling, verbatim
    assert holder != first.path  # ...so it is not mistaken for this unit's own mount

    with pytest.raises(verify.GitError, match="would move the branch under that checkout"):
        open_unit_workspace(*_open_args(project, branch_per="story"))

    assert git(project.project, "rev-parse", f"refs/heads/{first.branch}") == tip
    assert rev_parse_head(foreign) == tip


def test_branch_checkout_path_answers_an_ordinary_mount_path_exactly(project):
    """Negative control for the row above: the un-stripped read must not OVER-refuse.

    Only the single `\\n` that `for-each-ref` frames each record with is removed, never
    arbitrary whitespace — so an ordinary registered path (the overwhelming majority, and
    the only shape `safe_segment` can compose) still round-trips to exactly the mount path
    the guard compares against, and the unit's own checkout stays EXEMPT. A reader that
    trimmed too little would leave the framing newline on, make every path unequal to its
    own mount, and turn the guard into a refusal on every ordinary remount.

    `_refuse_foreign_checkout` is called directly rather than through a remount because
    the exemption is the claim: it returns None on the unit's own mount and raises
    otherwise, so the call itself is the assertion.

    Ablated TWICE, because "does not raise" passes for every reason:

    * Return `Path(out)` un-trimmed (keep the framing `\\n`) and the equality assertion
      reddens — the spelling gains a newline.
    * With that equality assertion ALSO removed, the same ablation still reddens, now on
      `_refuse_foreign_checkout` raising `GitError` over the unit's own mount. So the
      negative half is not vacuous: it fails when the function over-refuses.
    """
    from bmad_loop.workspace import _refuse_foreign_checkout

    first, _run_dir = _open_unit(project, branch_per="story")

    holder = verify.branch_checkout_path(project.project, first.branch)

    assert holder == first.path  # exact round-trip: no framing left on, nothing eaten

    # the exemption still holds — this raises if the guard over-refuses its own mount
    _refuse_foreign_checkout(project.project, first.branch, first.path)
