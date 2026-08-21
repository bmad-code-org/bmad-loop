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
    """The commit intent is durable before append_entry can mutate the ledger."""
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
    real_append = deferredwork.append_entry

    def append_then_host_dies(*args, **kwargs):
        real_append(*args, **kwargs)
        raise SystemExit("host died after ledger append")

    monkeypatch.setattr(deferredwork, "append_entry", append_then_host_dies)
    with pytest.raises(SystemExit, match="host died"):
        engine._carry_harvested_deferrals(task)

    failed = load_state(engine.run_dir).tasks[task.story_key]
    assert failed.harvest_carry_commit_pending is True
    assert [entry.title for entry in _main_harvest_entries(project)] == [_HARVEST_CARRY["summary"]]

    monkeypatch.setattr(deferredwork, "append_entry", real_append)
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
    worktree = engine.run_dir / "worktrees" / "1-1-a"
    worktree.mkdir(parents=True)
    source = rev_parse_head(project.project)
    task = StoryTask(
        story_key="1-1-a",
        epic=1,
        phase=Phase.DONE,
        worktree_path=str(worktree),
        branch="bmad-loop/test-run/1-1-a",
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
    after one honored round, the work MERGES into the main repo, and the refiled
    follow-up lands in the MAIN repo's ledger — not stranded inside the discarded
    unit worktree. Exempting isolation would leave isolated runs non-convergent AND
    deferred (strictly worse), which this locks out."""
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
    # the refiled follow-up is in the MAIN repo ledger, integrated from the worktree
    open_refiled = [
        e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
        if e.open and "origin: review-budget-followup" in e.body
    ]
    assert len(open_refiled) == 1


# --------------------------------------------- review-budget follow-up carry (#425)
#
# The third producer in the `git add -A` family. Every row here GITIGNORES the
# ledger: with a tracked one the entry rides the unit commit and the merge
# delivers it, which is why `test_worktree_followup_damped_commits_and_integrates`
# passed all along while the reported defect was live.


def _refiled_followups(project):
    """Open `review-budget-followup` rows in the MAIN checkout's ledger."""
    if not project.deferred_work.is_file():
        return []
    return [
        entry
        for entry in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
        if entry.open and "origin: review-budget-followup" in entry.body
    ]


def _damping_script(project, story_key="1-1-a"):
    """Dev, then three passes that keep recommending — the default cap 1 damps."""
    return [wt_dev_effect(project, story_key)] + [
        wt_review_effect(project, story_key, clean=False) for _ in range(3)
    ]


def test_gitignored_damped_followup_reaches_the_main_ledger(project):
    """#425: `_record_review_budget_followup` writes `self.workspace.paths`, which
    under isolation is the unit worktree's ledger. `finalize_commit`'s `git add -A`
    skips a gitignored path in silence, the merge brings nothing over, and
    `close_unit_workspace(success=True)` then deletes the worktree — the DONE leg
    takes no `capture_diff`, so not even a `changes.patch` survives. Without the
    carry the run journals `refiled: DW-1` while the main checkout has no ledger at
    all.

    `check-ignore` is the oracle, not the presence of the rule: a row that reads
    the tracked-ledger shape by accident is the vacuity this whole block exists to
    avoid."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))

    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    rel = project.deferred_work.relative_to(project.project).as_posix()
    assert git(project.project, "check-ignore", rel).strip() == rel
    assert not verify.path_tracked(project.project, rel)
    damped = [e for e in engine.journal.entries() if e["kind"] == "review-followup-damped"]
    assert len(damped) == 1 and damped[0]["refiled"]
    assert [e.title for e in _refiled_followups(project)] == [
        "Follow-up review still recommended for 1-1-a after the damping cap was spent"
    ]
    carried = [e for e in engine.journal.entries() if e["kind"] == "review-followup-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == [_refiled_followups(project)[0].id]
    # `git add -- <ignored path>` refuses with rc 1: the row lands, the commit
    # cannot, and that is recorded rather than raised.
    uncommitted = [
        e for e in engine.journal.entries() if e["kind"] == "review-followup-carry-uncommitted"
    ]
    assert len(uncommitted) == 1


def test_tracked_damped_followup_is_not_refiled_twice_by_the_carry(project):
    """A tracked ledger delivers the row through the merge, so the carry re-reads
    its own provenance and appends nothing. `append_entry` dedupes on `origin:` +
    `source_spec:` against OPEN entries, which is what makes running the carry
    unconditionally safe rather than needing a tracked/ignored predicate."""
    write_ledger(project, {})
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert verify.path_tracked(
        project.project, project.deferred_work.relative_to(project.project).as_posix()
    )
    assert len(_refiled_followups(project)) == 1
    carried = [e for e in engine.journal.entries() if e["kind"] == "review-followup-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == []


def test_a_deduped_followup_is_still_recorded_for_the_carry(project):
    """The record is the INTENT, not a receipt for a new id. A story whose
    follow-up was already open appends nothing, and the producer still records it,
    because the record has to be durable before the append that may dedupe it —
    otherwise a replay after a host loss can never reconstruct authorship. The
    carry then dedupes in turn and journals an empty `dw_ids`, which the tracked
    path already produces routinely."""
    write_ledger(project, {})
    deferredwork.append_entry(
        project.deferred_work,
        title="already recommended",
        origin="review-budget-followup",
        source_spec="spec-1-1-a.md",
        reason="filed by an earlier run",
        severity="low",
    )
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "prior follow-up")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))

    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    damped = [e for e in engine.journal.entries() if e["kind"] == "review-followup-damped"]
    assert len(damped) == 1 and damped[0]["refiled"] is None
    assert len(engine.state.tasks["1-1-a"].refiled_followups) == 1
    carried = [e for e in engine.journal.entries() if e["kind"] == "review-followup-carried"]
    assert len(carried) == 1 and carried[0]["dw_ids"] == []
    # no duplicate: the pre-existing open row is the only one
    assert len(_refiled_followups(project)) == 1


def _host_loss_before_the_commit_save(engine, snap):
    """A host loss inside `_commit`, before its `advance(COMMITTING)` + `_save()`.

    `snap` captures the bytes that were DURABLE at that instant. Restoring them
    over whatever `run()`'s unwind-`finally` wrote is what makes this a SIGKILL
    rather than a SIGINT: the engine's own teardown save would otherwise persist
    the very record whose durability is under test, and the row would arrive for
    a reason the fix has nothing to do with.
    """

    def commit_with_host_loss(_task):
        snap["state"] = (engine.run_dir / "state.json").read_bytes()
        raise RuntimeError("host died between the ledger write and the COMMITTING save")

    engine._commit = commit_with_host_loss


def test_host_loss_before_the_commit_save_still_carries_the_followup(project):
    """The producer's record must be persisted BEFORE its ledger append.

    Nothing saves between `_record_review_budget_followup` and `_commit`'s
    COMMITTING save, and that window spans every blocking `pre_commit_gate`
    workflow — the shipped TEA plugin binds three, each a live session. Recording
    after a successful append loses the row for good: the resumed run replays the
    same review result in the same worktree, `append_entry` dedupes the row it
    already wrote there to None, so the record is never made, the carry finds an
    empty payload, and `close_unit_workspace` deletes the worktree holding the
    only copy.
    """
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))
    snap: dict = {}
    _host_loss_before_the_commit_save(engine, snap)

    assert engine.run().crashed

    worktree = Path(engine.state.tasks["1-1-a"].worktree_path)
    # the row exists, but only inside the unit worktree's gitignored ledger
    assert [e.id for e in _refiled_followups(project.rebased(worktree))] == ["DW-1"]
    assert not project.deferred_work.exists()

    (engine.run_dir / "state.json").write_bytes(snap["state"])
    durable = load_state(engine.run_dir).tasks["1-1-a"]
    assert durable.phase == Phase.REVIEW_VERIFY
    assert durable.refiled_followups

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
    assert not worktree.is_dir()
    assert [e.title for e in _refiled_followups(project)] == [
        "Follow-up review still recommended for 1-1-a after the damping cap was spent"
    ]


def test_a_replayed_review_result_records_the_followup_once(project):
    """The record is keyed on `origin:` + `source_spec:`, so the replay that
    re-enters the damped path with the record already persisted appends no second
    copy — and files no second ledger row."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))
    _host_loss_before_the_commit_save(engine, {})

    assert engine.run().crashed
    # the unwind-finally persisted the record, so the replay re-enters the damped
    # path with it already in hand — the case the keyed dedupe exists for
    assert len(load_state(engine.run_dir).tasks["1-1-a"].refiled_followups) == 1

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
    assert len(resumed.state.tasks["1-1-a"].refiled_followups) == 1
    assert len(_refiled_followups(project)) == 1


def test_crashed_post_merge_followup_carry_replays_from_its_record(project):
    """A story whose ONLY ledger write is a damped follow-up has both other carry
    payloads empty, so the resume pass has to name this one to reach it: crash in
    the merge-to-latch window and the row is otherwise stranded in a worktree that
    is already gone."""
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))
    crash_at_merge_back(engine, after="merge")

    assert engine.run().crashed

    crashed = load_state(engine.run_dir).tasks["1-1-a"]
    assert crashed.phase == Phase.DONE and not crashed.isolated_ledger_carried
    assert crashed.refiled_followups and not crashed.harvested_deferrals
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
    assert len(_refiled_followups(project)) == 1
    assert load_state(resumed.run_dir).tasks["1-1-a"].isolated_ledger_carried


def test_a_re_armed_story_does_not_carry_the_abandoned_attempt_s_followup(project, monkeypatch):
    """The record is scoped to the attempt that made it, and `_dev_phase`'s
    fresh-attempt clear is what enforces that.

    An escalation between the damped record and the commit leaves the unit
    worktree mounted and unmerged; the re-drive then DISCARDS it, taking the only
    copy of the row with it. Without the clear the record outlives its attempt,
    and the next attempt's DONE leg files a follow-up against the commit that
    actually landed — one whose review recommended nothing. `rearm_escalation`
    already resets `followup_reviews_spent` for a fresh damping budget, so keeping
    the record contradicts the re-arm on its own terms.

    Nothing upstream absorbs it either: the abandoned attempt's ledger write never
    reached the main checkout, so `append_entry` has no open row to dedupe against
    and a TRACKED ledger would commit the stale row rather than swallow it.
    """
    ignore_before_commit(project, "deferred-work.md")
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _damping_script(project))

    real_finalize = verify.finalize_commit

    def commit_fails(*_a, **_k):
        raise verify.GitError("simulated commit failure")

    monkeypatch.setattr(verify, "finalize_commit", commit_fails)

    assert engine.run().paused

    escalated = load_state(engine.run_dir).tasks["1-1-a"]
    assert escalated.phase == Phase.ESCALATED
    assert escalated.refiled_followups  # persisted by the producer, pre-append
    assert not project.deferred_work.exists()  # the row is only in the doomed worktree

    monkeypatch.setattr(verify, "finalize_commit", real_finalize)
    assert runs.rearm_escalation(engine.run_dir, "1-1-a") == "1-1-a"

    state = load_state(engine.run_dir)
    state.clear_pause()
    resumed = Engine(
        paths=project,
        policy=engine.policy,
        adapter=MockAdapter(
            [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
        ),
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1 and not summary.paused and not summary.crashed
    # the re-drive converged on its own review: only the ABANDONED attempt damped
    assert journal_kinds(resumed).count("review-followup-damped") == 1
    # the harm, asserted before its cause: no follow-up row for the commit that
    # landed, and no carry claiming to have filed one
    assert [e.title for e in _refiled_followups(project)] == []
    assert "review-followup-carried" not in journal_kinds(resumed)
    assert not resumed.state.tasks["1-1-a"].refiled_followups


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
    resumed = Engine(
        paths=project,
        policy=gated,
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
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
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


@pytest.mark.parametrize(
    "make_exc, present, absent",
    [
        (
            lambda: verify.MergePreflightError(
                "git merge --squash feat failed in /repo (refused before starting): "
                "error: The following untracked working tree files would be "
                "overwritten by merge:\n\tleak.cs"
            ),
            "refused by git before it started",
            "content conflict",
        ),
        (
            lambda: verify.GitError(
                "git merge --no-ff feat failed in /repo (conflict): "
                "CONFLICT (content): Merge conflict in src.txt"
            ),
            "content conflict",
            "refused by git before it started",
        ),
    ],
    ids=["preflight-refusal", "content-conflict"],
)
def test_merge_failure_escalation_tells_a_preflight_refusal_from_a_conflict(
    project, monkeypatch, make_exc, present, absent
):
    """#619: `merge_local` caught every `verify.GitError` out of `merge_branch` and
    told the operator to resolve "a content conflict against the target". Most of
    those failures are git declining at pre-flight — nothing merged, the checkout
    untouched, no markers anywhere — so the guidance sent them looking for a
    conflict that does not exist. `MergePreflightError` is a GitError subclass, so
    the two arms must be ordered subclass-first for the split to exist at all.

    Both rows assert one phrase PRESENT and the other's phrase ABSENT. The absence
    half is the discriminator: a single catch-all arm still makes each row's own
    phrase appear on one of them, and only the cross-check catches the collapse.

    Ablation: delete the `except verify.MergePreflightError` arm from `merge_local`
    and the preflight-refusal row fails on both halves while content-conflict stays
    green — and every verify-layer row stays green too, because this is the wiring
    axis and those are the predicate axis."""
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
    assert absent not in reason
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
    # None for a row that is already `done`, so `mark_done_many` writes nothing at
    # all and no commit is attempted. That is what lets the carry be unconditional
    # rather than needing a tracked/ignored predicate.
    carried = [e for e in engine.journal.entries() if e["kind"] == "story-deferred-close-carried"]
    assert [e["dw_ids"] for e in carried] == [[]]
    assert "story-deferred-close-carry-uncommitted" not in journal_kinds(engine)
    # exactly one annotation — a second close would append a second resolution line
    assert entry.body.count("resolution:") == 1
    assert "resolution-undo:" not in entry.body


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
    assert not crashed.harvested_deferrals and not crashed.refiled_followups
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
    payload empty, so the resume pass has to name this one to reach it — the same
    reachability the damped follow-up needed, for the fourth producer."""
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
    assert not crashed.harvested_deferrals and not crashed.refiled_followups
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
    `_dev_phase` clear the way `refiled_followups` does: DONE is reachable only
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
    assert runs.rearm_escalation(engine.run_dir, "1-1-a") == "1-1-a"

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
