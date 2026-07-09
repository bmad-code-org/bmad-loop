"""Phase 3: isolation="worktree" — each unit runs in its own git worktree and
merges back into the target branch locally. Sessions run inside the worktree
(spec.cwd), so the effects here write artifacts rebased onto that checkout.

Exercised end-to-end against the conftest `project` sandbox with the mock
adapter (no tmux, no LLM).
"""

from __future__ import annotations

from conftest import (
    _OK,
    _exists_run,
    _seeded_then_touch,
    _spec_baseline,
    _touch_run,
    git,
    set_sprint,
    write_spec,
    write_sprint,
)

from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.engine import Engine
from bmad_loop.journal import Journal, load_state
from bmad_loop.model import Phase, RunState, StoryTask, TokenUsage
from bmad_loop.policy import GatesPolicy, NotifyPolicy, Policy, ScmPolicy
from bmad_loop.verify import (
    branch_exists,
    current_branch,
    rev_parse_head,
    worktree_clean,
    worktree_list,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def wt_policy(**scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree", **scm),
    )


def commit_sprint(project, statuses: dict[str, str]) -> None:
    """Worktrees are checkouts of a commit, so the sprint board (and artifact
    dirs) must be committed before the run, not left untracked."""
    write_sprint(project, statuses)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sprint")


def wt_dev_effect(project, story_key, *, final_status="done", followup_review=True):
    """Dev session running inside the unit worktree (spec.cwd). Mirrors the
    bmad-dev-auto skill: self-finalizes the spec to done, never writes the sprint
    board (the orchestrator advances it via the B2 seam, inside the worktree).
    ``followup_review`` mirrors the skill's `followup_review_recommended` signal;
    defaults True so the review runs under the default trigger = "recommended"."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + f"change for {story_key}\n")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        write_spec(sp, final_status, baseline)
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


def wt_review_effect(project, story_key, clean: bool, patched: int = 0):
    """Follow-up review pass in a worktree — a bmad-dev-auto re-invocation on the
    done spec. ``clean=True`` converges; ``clean=False`` keeps recommending."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline)
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
    """Dev succeeds, then review never converges → plateau defer."""
    return [wt_dev_effect(project, key)] + [
        wt_review_effect(project, key, clean=False, patched=1) for _ in range(3)
    ]


def test_worktree_defer_keeps_failed_unit(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _defer_script(project, "1-1-a"))
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


def test_worktree_defer_without_keep_drops_worktree_but_saves_patch(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, _defer_script(project, "1-1-a"), policy=wt_policy(keep_failed=False)
    )
    summary = engine.run()

    assert summary.deferred == 1
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file() and "change for 1-1-a" in patch.read_text()
    # not kept → worktree removed, branch deleted
    assert not branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_worktree_defer_then_next_story_succeeds(project):
    """A deferred (kept) unit must not block the next story's worktree/merge."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 1
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()


def test_branch_per_run_kept_failure_defers_next_unit_gracefully(project):
    """branch_per=run shares one branch; a kept-failed unit holds it, so the
    next unit can't mount it. That degrades to a deferral, never a crash."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [wt_dev_effect(project, "1-2-b")]
    engine, _ = make_engine(project, script, policy=wt_policy(branch_per="run"))
    summary = engine.run()

    assert summary.deferred == 2 and summary.done == 0 and not summary.paused
    assert "could not open worktree" in engine.state.tasks["1-2-b"].defer_reason
    assert "worktree-open-failed" in journal_kinds(engine)


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


# ----------------------------------------------------------------- resume


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


def test_merge_stray_dirt_escalates_with_clear_message(project):
    """Dirt in the main checkout that is NOT part of the branch's incoming files
    (possible real operator work) is never cleaned: the unit escalates with the
    Editor-leak message and keeps its branch."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            _leaking_dev_effect(project, "1-1-a", leak_name="stray.txt", in_branch_set=False),
            wt_review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    reason = engine.state.paused_reason or ""
    assert "not part of this branch" in reason and "stray.txt" in reason
    # branch kept for manual merge; the stray file was left untouched
    assert branch_exists(project.project, "bmad-loop/test-run/1-1-a")
    assert (project.project / "stray.txt").read_text() == "editor leaked\n"
    assert "merge-target-cleaned" not in journal_kinds(engine)


def test_spec_file_serialized_relative_to_worktree():
    """A worktree task persists spec_file relative to its worktree so a kept run's
    state stays portable (no dangling absolute path into a pruned worktree)."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/.bmad-loop/runs/run/worktrees/1-1-a"
    task.spec_file = "/repo/.bmad-loop/runs/run/worktrees/1-1-a/_out/spec.md"
    assert task.to_dict()["spec_file"] == "_out/spec.md"
    # a spec living outside the worktree stays absolute
    task.spec_file = "/elsewhere/spec.md"
    assert task.to_dict()["spec_file"] == "/elsewhere/spec.md"
    # in-place mode (no worktree) is unchanged
    task.worktree_path = ""
    task.spec_file = "/repo/_out/spec.md"
    assert task.to_dict()["spec_file"] == "/repo/_out/spec.md"


def test_spec_file_serialized_with_posix_separators():
    """The relative spec_file is persisted with forward slashes (as_posix) so a
    state.json written under one OS reads back identically under another — no
    backslashes leak into the cross-OS state contract."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/wt"
    task.spec_file = "/repo/wt/_out/sub/spec.md"
    serialized = task.to_dict()["spec_file"]
    assert serialized == "_out/sub/spec.md"
    assert "\\" not in serialized
