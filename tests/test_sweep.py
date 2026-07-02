"""Sweep engine scenario tests against the mock adapter — no tmux, no LLM."""

import json
import re

from conftest import (
    bundle_dev_effect,
    bundle_review_effect,
    git,
    migrate_effect,
    triage_effect,
    write_ledger,
    write_legacy_ledger,
    write_spec,
)

from automator import deferredwork
from automator.adapters.base import SessionResult
from automator.adapters.mock import MockAdapter
from automator.journal import Journal, load_state
from automator.model import Phase, RunState, StoryTask, TokenUsage
from automator.policy import (
    DevPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    SweepPolicy,
)
from automator.sweep import DecisionPrompter, SweepEngine, validate_migration, validate_triage
from automator.verify import worktree_clean

QUIET = NotifyPolicy(desktop=False, file=True)


def triage_result(open_ids, **sections):
    return {
        "workflow": "deferred-sweep-triage",
        "open_ids": list(open_ids),
        "already_resolved": sections.get("already_resolved", []),
        "bundles": sections.get("bundles", []),
        "blocked": sections.get("blocked", []),
        "skip": sections.get("skip", []),
        "decisions": sections.get("decisions", []),
        "escalations": [],
    }


def make_sweep(project, script, policy=None, answers=(), prompting=False, **kwargs):
    run_dir = project.project / ".automator" / "runs" / "sweep-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="sweep-run", project=str(project.project), started_at="now")
    inputs = iter(answers)
    prompter = DecisionPrompter(input_fn=lambda _: next(inputs), print_fn=lambda _line: None)
    engine = SweepEngine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
        ),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        prompting=prompting,
        prompter=prompter,
        **kwargs,
    )
    return engine, adapter


def resume_sweep(project, engine, script, answers=(), prompting=False, **kwargs):
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(script)
    inputs = iter(answers)
    prompter = DecisionPrompter(input_fn=lambda _: next(inputs), print_fn=lambda _line: None)
    new_engine = SweepEngine(
        paths=project,
        policy=engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        prompting=prompting,
        prompter=prompter,
        **kwargs,
    )
    return new_engine, adapter


def journal_text(engine) -> str:
    return (engine.run_dir / "journal.jsonl").read_text()


def ledger_entries(project) -> dict:
    return {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }


# ------------------------------------------------------- validate_triage


def test_validate_triage_happy():
    rj = triage_result(
        ["DW-1", "DW-2", "DW-3", "DW-4", "DW-5"],
        already_resolved=[{"id": "DW-1", "evidence": "fixed in abc123"}],
        bundles=[{"name": "fix-strings", "dw_ids": ["DW-2", "DW-3"], "intent": "harden it"}],
        blocked=[{"id": "DW-4", "blocker": "story 5-2"}],
        decisions=[
            {
                "id": "DW-5",
                "question": "renegotiate?",
                "context": "ctx",
                "options": [
                    {
                        "key": "1",
                        "label": "build it",
                        "effect": "build",
                        "intent": "do x",
                    },
                    {"key": "2", "label": "keep", "effect": "keep-open"},
                ],
                "recommendation": "1",
            }
        ],
    )
    plan, errors = validate_triage(rj, {"DW-1", "DW-2", "DW-3", "DW-4", "DW-5"})
    assert errors == []
    assert plan.bundles[0].dw_ids == ("DW-2", "DW-3")
    assert plan.decisions[0].option("1").effect == "build"


def test_validate_triage_open_ids_mismatch():
    rj = triage_result(["DW-1", "DW-9"], bundles=[])
    plan, errors = validate_triage(rj, {"DW-1", "DW-2"})
    assert plan is None
    assert "DW-2" in errors[0] and "DW-9" in errors[0]


def test_validate_triage_partition_errors():
    rj = triage_result(
        ["DW-1", "DW-2"],
        already_resolved=[{"id": "DW-1", "evidence": "x"}],
        bundles=[{"name": "b", "dw_ids": ["DW-1"], "intent": "dup claim"}],
    )
    plan, errors = validate_triage(rj, {"DW-1", "DW-2"})
    assert plan is None
    joined = "; ".join(errors)
    assert "DW-1 appears in both" in joined  # double-counted
    assert "not triaged: DW-2" in joined  # missed


def test_validate_triage_bad_fields():
    rj = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "Bad_Name", "dw_ids": ["DW-1"], "intent": ""}],
        decisions=[
            {
                "id": "DW-2",
                "question": "q",
                "options": [
                    {"key": "1", "label": "a", "effect": "build"},  # build w/o intent
                ],
                "recommendation": "7",
            }
        ],
    )
    plan, errors = validate_triage(rj, {"DW-1", "DW-2"})
    assert plan is None
    joined = "; ".join(errors)
    assert "Bad_Name" in joined
    assert "no intent" in joined
    assert "needs intent" in joined
    assert "at least 2 options" in joined
    assert "recommendation" in joined


def test_validate_triage_unknown_id():
    rj = triage_result(
        ["DW-1"],
        skip=[{"id": "DW-1", "reason": "moot"}, {"id": "DW-42", "reason": "ghost"}],
    )
    plan, errors = validate_triage(rj, {"DW-1"})
    assert plan is None
    assert any("DW-42" in e for e in errors)


# ---------------------------------------------------- validate_migration

LEGACY_LEDGER = (
    "# Deferred Work\n\n"
    "## Deferred from: epic 1 review (2026-04-06)\n\n"
    "- ~~**Old fixed thing** — was broken, then repaired~~ → fixed in 1.3\n"
    "- **Open legacy thing here** — `src.txt` mishandles em-dashes\n"
)


def legacy_manifest(text: str = LEGACY_LEDGER) -> list[dict]:
    return [
        {
            "key": e.key,
            "id": e.id,
            "title": e.title,
            "section": e.section,
            "done": e.done,
            "severity": e.severity,
        }
        for e in deferredwork.parse_legacy(text)
    ]


def migrated_ledger(first_id: int = 1) -> str:
    return (
        "# Deferred Work\n\n"
        f"### DW-{first_id}: Old fixed thing\n\n"
        "origin: migrated from legacy ledger, 2026-06-12\nlocation: n/a\n"
        "reason: was broken, then repaired.\nstatus: done 2026-04-06\n\n"
        f"### DW-{first_id + 1}: Open legacy thing here\n\n"
        "origin: migrated from legacy ledger, 2026-06-12\nlocation: src.txt\n"
        "reason: mishandles em-dashes.\nstatus: open\n"
    )


def migrate_result(mapping) -> dict:
    return {"workflow": "deferred-sweep-migrate", "mapping": list(mapping), "escalations": []}


def test_validate_migration_happy():
    manifest = legacy_manifest()
    done_key, open_key = manifest[0]["key"], manifest[1]["key"]
    rj = migrate_result([{"key": done_key, "dw_id": "DW-1"}, {"key": open_key, "dw_id": "DW-2"}])
    assert validate_migration(rj, manifest, {}, migrated_ledger()) == []


def test_validate_migration_rejects_leftover_legacy():
    manifest = legacy_manifest()
    half_done = migrated_ledger() + "\n## Deferred from: leftovers\n\n- still freeform item\n"
    rj = migrate_result(
        [{"key": manifest[0]["key"], "dw_id": "DW-1"}, {"key": manifest[1]["key"], "dw_id": "DW-2"}]
    )
    errors = validate_migration(rj, manifest, {}, half_done)
    assert any("still parse as legacy" in e and "still freeform item" in e for e in errors)


def test_validate_migration_guards_pre_existing_canonical():
    manifest = legacy_manifest()
    pre = {"DW-1": "open", "DW-9": "open"}  # DW-1 regressed to done; DW-9 vanished
    rj = migrate_result(
        [{"key": manifest[0]["key"], "dw_id": "DW-1"}, {"key": manifest[1]["key"], "dw_id": "DW-2"}]
    )
    errors = validate_migration(rj, manifest, pre, migrated_ledger())
    joined = "; ".join(errors)
    assert "DW-1 status changed" in joined
    assert "DW-9 disappeared" in joined
    # and the new DW-2 does not continue numbering past DW-9
    assert "does not continue numbering past DW-9" in joined


def test_validate_migration_mapping_errors():
    manifest = legacy_manifest()
    done_key = manifest[0]["key"]
    rj = migrate_result(
        [
            {"key": done_key, "dw_id": "DW-2"},  # done-ness mismatch (DW-2 is open)
            {"key": "no-such-key", "dw_id": "DW-1"},  # invented
            {"key": done_key, "dw_id": "DW-77"},  # repeated key + missing entry
        ]
    )
    errors = validate_migration(rj, manifest, {}, migrated_ledger())
    joined = "; ".join(errors)
    assert "manifest says done, ledger disagrees" in joined
    assert "invents unknown key" in joined
    assert "repeats key" in joined
    assert "DW-77: no such entry" in joined
    assert "not mapped" in joined  # the open item's key never appeared


def test_validate_migration_allows_dedupe_merge():
    # two legacy items of equal done-ness may merge into one DW entry
    text = (
        "## Deferred from: review A (2026-04-06)\n\n- same thing, worded one way\n"
        "## Deferred from: review B (2026-04-07)\n\n- same thing, worded another way\n"
    )
    manifest = legacy_manifest(text)
    merged = (
        "# Deferred Work\n\n### DW-1: same thing\n\n"
        "origin: migrated from legacy ledger, 2026-06-12\nlocation: n/a\n"
        "reason: seen in review A and review B.\nstatus: open\n"
    )
    rj = migrate_result([{"key": m["key"], "dw_id": "DW-1"} for m in manifest])
    assert validate_migration(rj, manifest, {}, merged) == []


def test_validate_migration_wrong_workflow():
    errors = validate_migration({"workflow": "quick-dev"}, [], {}, "")
    assert errors and "workflow" in errors[0]


# ------------------------------------------------------------ engine flow


def test_sweep_nothing_open(project):
    write_ledger(project, {"DW-1": "done 2026-06-01"})
    engine, adapter = make_sweep(project, [])
    summary = engine.run()
    assert summary.done == 0 and not summary.paused
    assert adapter.sessions == []
    assert "sweep-nothing-open" in journal_text(engine)


def test_sweep_worktree_bundle_merges_to_target(project):
    """A sweep bundle runs in its own worktree and merges back: the ledger
    closes land on the target branch and the worktree is cleaned up."""
    from conftest import _spec_baseline, write_spec

    from automator.verify import branch_exists, rev_parse_head, worktree_list

    write_ledger(project, {"DW-1": "open"})  # committed → visible in the worktree
    plan = triage_result(
        ["DW-1"],
        bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "fix it"}],
    )

    def wt_bundle_dev(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + "change for dw-fix\n")
        sp = wt.implementation_artifacts / "spec-dw-fix.md"
        # mirror bmad-dev-auto: self-finalize the bundle spec to done, leave the
        # ledger to the orchestrator (single writer, marks inside the worktree)
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "dw-fix",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "dw_ids": ["DW-1"],
            },
        )

    def wt_bundle_review(spec):
        wt = project.rebased(spec.cwd)
        sp = wt.implementation_artifacts / "spec-dw-fix.md"
        write_spec(sp, "done", _spec_baseline(sp))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "code-review",
                "clean": True,
                "patched": 0,
                "deferred": 0,
                "dismissed": 0,
                "escalations": [],
            },
        )

    pol = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, scm=ScmPolicy(isolation="worktree"))
    engine, _ = make_sweep(
        project, [triage_effect(plan), wt_bundle_dev, wt_bundle_review], policy=pol
    )
    summary = engine.run()

    assert not summary.paused
    assert engine.state.tasks["dw-fix"].phase == Phase.DONE
    # the ledger close landed on the target branch (main, in the main repo)
    assert ledger_entries(project)["DW-1"].status.startswith("done")
    assert "change for dw-fix" in (project.project / "src.txt").read_text()
    # worktree cleaned up, branch deleted
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert not branch_exists(project.project, "automator/sweep-run/dw-fix")
    assert worktree_clean(project.project)


def test_sweep_happy_path(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open", "DW-3": "open"})
    plan = triage_result(
        ["DW-1", "DW-2", "DW-3"],
        already_resolved=[{"id": "DW-1", "evidence": "already guarded at src.txt:1"}],
        bundles=[{"name": "fix-things", "dw_ids": ["DW-2", "DW-3"], "intent": "fix both"}],
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "fix-things", ["DW-2", "DW-3"]),
            bundle_review_effect(project, "fix-things"),
        ],
    )
    summary = engine.run()

    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["sweep-triage"].phase == Phase.DONE
    assert tasks["dw-fix-things"].phase == Phase.DONE
    assert tasks["dw-fix-things"].commit_sha

    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert "already resolved: already guarded" in entries["DW-1"].body
    assert entries["DW-2"].status.startswith("done")
    assert entries["DW-3"].status.startswith("done")
    assert worktree_clean(project.project)

    log = git(project.project, "log", "--oneline")
    assert "chore(sweep): close resolved deferred-work entries" in log
    assert "sweep dw-fix-things: DW-2, DW-3 via bmad-auto" in log

    # dev session was invoked in bundle mode with the rendered intent file
    dev_spec = adapter.sessions[1]
    assert "Implement the deferred-work bundle" in dev_spec.prompt
    intent_path = re.findall(r"`([^`]*)`", dev_spec.prompt)[0]
    intent = open(intent_path).read()
    assert "fix both" in intent and "DW-2" in intent and "### DW-3" in intent


def test_generic_skill_bundle_orchestrator_closes_ledger(project):
    """B4: on the generic bmad-dev-auto path the bundle session never edits the
    ledger; the orchestrator marks each owned dw id done (in _post_dev_state_sync)
    and verify_review_bundle confirms its own write. The invocation is freeform."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "fix-things", "dw_ids": ["DW-1", "DW-2"], "intent": "fix both"}],
    )
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, adapter = make_sweep(
        project,
        # mark_ledger=False: the decoupled skill does NOT touch the ledger
        [
            triage_effect(plan),
            bundle_dev_effect(project, "fix-things", ["DW-1", "DW-2"], mark_ledger=False),
        ],
        policy=pol,
    )
    summary = engine.run()

    assert not summary.paused
    assert engine.state.tasks["dw-fix-things"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")
    assert "resolved by sweep bundle dw-fix-things" in entries["DW-1"].body
    # freeform generic invocation pointing at the rendered intent.md, no --dw-bundle flag
    dev_prompt = adapter.sessions[1].prompt
    assert dev_prompt.startswith("/bmad-dev-auto Implement the deferred-work bundle")
    assert "--dw-bundle" not in dev_prompt
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "sweep-bundle-closed" in kinds


def test_generic_bundle_review_verify_recloses_ledger_after_review_rewrites_it(project):
    """A follow-up review can rewrite deferred-work.md from its own snapshot and
    re-open entries the orchestrator already closed after dev. The review gate
    should re-apply the orchestrator-owned ledger closure before verification,
    otherwise the sweep launches a spurious repair/dev pass for complete work."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    baseline = git(project.project, "rev-parse", "HEAD")
    spec = project.implementation_artifacts / "spec-dw-fix-things.md"
    write_spec(spec, "done", baseline)
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="always"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_sweep(project, [], policy=pol)
    task = StoryTask(
        story_key="dw-fix-things",
        epic=0,
        dw_ids=["DW-1", "DW-2"],
        spec_file=str(spec),
    )

    out = engine._verify_review(task)

    assert out.ok
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")
    assert "resolved by sweep bundle dw-fix-things" in entries["DW-1"].body
    assert "sweep-bundle-reclosed" in {e["kind"] for e in engine.journal.entries()}


def test_generic_bundle_reconcile_closes_ledger_on_stale_frontmatter(project):
    """Regression for the DW-159/160/162 false-defer: the bundle session finalized
    in prose (## Auto Run Result: Status done) but left the bundle spec frontmatter
    at the template default `draft`. The orchestrator reconciles the frontmatter
    before the ledger sync, so the bundle CLOSES — its dw ids are marked done and
    not stranded in failed_ids — instead of falsely deferring completed work."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "fix-things", "dw_ids": ["DW-1", "DW-2"], "intent": "fix both"}],
    )
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            # the skill leaves frontmatter at draft but writes prose Status: done
            bundle_dev_effect(
                project,
                "fix-things",
                ["DW-1", "DW-2"],
                mark_ledger=False,
                final_status="draft",
                prose_status="done",
            ),
        ],
        policy=pol,
    )
    summary = engine.run()

    assert not summary.paused
    assert engine.state.tasks["dw-fix-things"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "draft" and recon[0]["to"] == "done"
    assert "sweep-bundle-closed" in {e["kind"] for e in engine.journal.entries()}


def test_generic_bundle_reconcile_closes_ledger_on_in_review_frontmatter(project):
    """The Lights-Out DW-153 symptom on the bundle path: the session finalized in
    prose (## Auto Run Result: Status done) but left the bundle spec frontmatter at
    the transient `in-review` marker. in-review is never a deliberate terminal on
    the generic path (the legacy review-handoff fork is retired), so the
    orchestrator reconciles it to done before the ledger sync — the bundle CLOSES
    instead of false-deferring + rolling back into an endless re-sweep loop."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "fix-things", "dw_ids": ["DW-1", "DW-2"], "intent": "fix both"}],
    )
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            # the skill leaves frontmatter at the transient in-review marker but
            # writes prose Status: done
            bundle_dev_effect(
                project,
                "fix-things",
                ["DW-1", "DW-2"],
                mark_ledger=False,
                final_status="in-review",
                prose_status="done",
            ),
        ],
        policy=pol,
    )
    summary = engine.run()

    assert not summary.paused
    assert engine.state.tasks["dw-fix-things"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")
    recon = [e for e in engine.journal.entries() if e["kind"] == "spec-status-reconciled"]
    assert len(recon) == 1
    assert recon[0]["frm"] == "in-review" and recon[0]["to"] == "done"
    assert "sweep-bundle-closed" in {e["kind"] for e in engine.journal.entries()}


def test_triage_validation_failure_retries_with_feedback_then_escalates(project):
    write_ledger(project, {"DW-1": "open"})
    bad = triage_result(["DW-1"])  # DW-1 not triaged anywhere
    engine, adapter = make_sweep(project, [triage_effect(bad), triage_effect(bad)])
    summary = engine.run()

    assert summary.paused
    assert engine.state.tasks["sweep-triage"].phase == Phase.ESCALATED
    prompts = [s.prompt for s in adapter.sessions]
    assert len(prompts) == 2
    assert "--feedback" not in prompts[0] and "--feedback" in prompts[1]
    feedback_path = prompts[1].split("--feedback ", 1)[1]
    assert "not triaged: DW-1" in open(feedback_path).read()


def test_triage_escalation_resume_retries_triage(project):
    write_ledger(project, {"DW-1": "open"})
    bad = triage_result(["DW-1"])
    engine, _ = make_sweep(project, [triage_effect(bad), triage_effect(bad)])
    assert engine.run().paused

    good = triage_result(["DW-1"], skip=[{"id": "DW-1", "reason": "moot"}])
    resumed, adapter = resume_sweep(project, engine, [triage_effect(good)])
    summary = resumed.run()
    assert not summary.paused
    assert resumed.state.tasks["sweep-triage"].phase == Phase.DONE
    assert len(adapter.sessions) == 1


def test_interactive_decisions_build_and_close(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        decisions=[
            {
                "id": "DW-1",
                "question": "build the widening?",
                "context": "ctx",
                "options": [
                    {
                        "key": "1",
                        "label": "Widen it",
                        "effect": "build",
                        "intent": "widen the field",
                    },
                    {"key": "2", "label": "Keep as is", "effect": "keep-open"},
                ],
                "recommendation": "1",
            },
            {
                "id": "DW-2",
                "question": "close as moot?",
                "context": "",
                "options": [
                    {
                        "key": "1",
                        "label": "Close it",
                        "effect": "close",
                        "resolution": "superseded by v2",
                    },
                    {"key": "2", "label": "Keep open", "effect": "keep-open"},
                ],
                "recommendation": "1",
            },
        ],
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "decision-dw-1", ["DW-1"]),
            bundle_review_effect(project, "decision-dw-1"),
        ],
        # DW-1: invalid input, then empty (= recommendation "1" -> build);
        # DW-2: explicit "1" (close)
        answers=["9", "", "1"],
        prompting=True,
    )
    summary = engine.run()
    assert not summary.paused

    journal = journal_text(engine)
    assert journal.count('"decision-pending"') == 2  # announced before each prompt
    assert journal.index('"decision-pending"') < journal.index('"decision-answered"')
    attention = (engine.run_dir / "ATTENTION").read_text()
    assert "decision needed: DW-1" in attention

    answers = json.loads((engine.run_dir / "decisions.json").read_text())
    assert answers["DW-1"]["effect"] == "build"
    assert answers["DW-2"]["effect"] == "close"

    entries = ledger_entries(project)
    assert "decision:" in entries["DW-1"].body
    assert entries["DW-1"].status.startswith("done")  # closed by the built bundle
    assert entries["DW-2"].status.startswith("done")  # closed by the decision
    assert "closed by human decision: superseded by v2" in entries["DW-2"].body
    assert engine.state.tasks["dw-decision-dw-1"].phase == Phase.DONE
    assert "chore(sweep): record deferred-work decisions" in git(
        project.project, "log", "--oneline"
    )


def _close_decision_plan():
    return triage_result(
        ["DW-1"],
        decisions=[
            {
                "id": "DW-1",
                "question": "close as moot?",
                "context": "",
                "options": [
                    {"key": "1", "label": "Close it", "effect": "close", "resolution": "moot"},
                    {"key": "2", "label": "Keep open", "effect": "keep-open"},
                ],
                "recommendation": "1",
            }
        ],
    )


def test_interactive_decisions_return_client_goes_unattended(project, monkeypatch):
    """When a client was attached to answer, the sweep hands the terminal back
    after the decisions and goes unattended so later cycles don't block on a
    detached window."""
    returned: list[bool] = []
    monkeypatch.setattr(
        "automator.tui.launch.return_attached_client",
        lambda: bool(returned.append(True)) or True,
    )
    write_ledger(project, {"DW-1": "open"})
    engine, _adapter = make_sweep(
        project,
        [triage_effect(_close_decision_plan())],
        answers=["1"],
        prompting=True,
    )
    summary = engine.run()
    assert not summary.paused
    assert returned == [True]  # asked exactly once, after the decisions phase
    assert engine.prompting is False
    assert '"sweep-returned-after-decisions"' in journal_text(engine)


def test_interactive_decisions_no_attach_stays_attended(project, monkeypatch):
    """A plain foreground sweep (nobody attached, no return target) keeps
    prompting and never emits the return event."""
    monkeypatch.setattr("automator.tui.launch.return_attached_client", lambda: False)
    write_ledger(project, {"DW-1": "open"})
    engine, _adapter = make_sweep(
        project,
        [triage_effect(_close_decision_plan())],
        answers=["1"],
        prompting=True,
    )
    summary = engine.run()
    assert not summary.paused
    assert engine.prompting is True
    assert '"sweep-returned-after-decisions"' not in journal_text(engine)


def test_unattended_skips_decisions(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "safe-fix", "dw_ids": ["DW-2"], "intent": "fix it"}],
        decisions=[
            {
                "id": "DW-1",
                "question": "q",
                "context": "",
                "options": [
                    {"key": "1", "label": "a", "effect": "build", "intent": "x"},
                    {"key": "2", "label": "b", "effect": "keep-open"},
                ],
                "recommendation": "2",
            }
        ],
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "safe-fix", ["DW-2"]),
            bundle_review_effect(project, "safe-fix"),
        ],
        prompting=False,
    )
    summary = engine.run()
    assert not summary.paused
    assert "decision-skipped-unattended" in journal_text(engine)
    entries = ledger_entries(project)
    assert entries["DW-1"].open  # untouched, waits for an interactive sweep
    assert entries["DW-2"].status.startswith("done")
    assert not (engine.run_dir / "decisions.json").is_file()


def test_bundle_review_disabled_skips_review_session(project):
    """review.enabled = false: a bundle's dev session finalizes to done and the
    sweep commits with no separate bundle-review session."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"],
        bundles=[{"name": "some-fix", "dw_ids": ["DW-1"], "intent": "fix it"}],
    )
    engine, adapter = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_effect(project, "some-fix", ["DW-1"])],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            review=ReviewPolicy(enabled=False),
        ),
    )
    summary = engine.run()
    assert not summary.paused
    assert [s.role for s in adapter.sessions] == ["triage", "dev"]  # no review
    assert engine.state.tasks["dw-some-fix"].phase == Phase.DONE
    assert ledger_entries(project)["DW-1"].status.startswith("done")
    assert "review-skipped" in journal_text(engine)


def test_decisions_only_runs_no_bundles(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "some-fix", "dw_ids": ["DW-2"], "intent": "fix it"}],
        decisions=[
            {
                "id": "DW-1",
                "question": "q",
                "context": "",
                "options": [
                    {"key": "1", "label": "Close", "effect": "close"},
                    {"key": "2", "label": "Keep", "effect": "keep-open"},
                ],
                "recommendation": "1",
            }
        ],
    )
    engine, adapter = make_sweep(
        project,
        [triage_effect(plan)],
        answers=["1"],
        prompting=True,
        decisions_only=True,
    )
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 1  # triage only
    assert "sweep-decisions-only" in journal_text(engine)
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].open  # bundle not run


def _decision(dw_id, options, recommendation="1", question="q"):
    return {
        "id": dw_id,
        "question": question,
        "context": "",
        "options": options,
        "recommendation": recommendation,
    }


def test_preanswered_build_materializes_bundle_unattended(project):
    """A build pre-answered out of band is consumed by a later unattended sweep
    even though triage re-surfaced it as a decision — and the stored intent is
    used when the triage option keys no longer match (option renumbered)."""
    from automator import decisions
    from automator.sweep import DecisionOption

    write_ledger(project, {"DW-1": "open"})
    # answered out of band against an earlier triage: stored key "9" is NOT one
    # of this triage's option keys, so the sweep must fall back to stored intent
    decisions.record_pre_answer(
        project.project,
        "DW-1",
        DecisionOption(key="9", label="Widen", effect="build", intent="widen the field"),
        date="2026-06-12",
    )
    plan = triage_result(
        ["DW-1"],
        decisions=[
            _decision(
                "DW-1",
                [
                    {"key": "1", "label": "Widen", "effect": "build", "intent": "fresh intent"},
                    {"key": "2", "label": "Keep", "effect": "keep-open"},
                ],
            )
        ],
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "decision-dw-1", ["DW-1"]),
            bundle_review_effect(project, "decision-dw-1"),
        ],
        prompting=False,  # unattended: without the pre-answer this would be skipped
    )
    summary = engine.run()
    assert not summary.paused

    journal = journal_text(engine)
    assert '"decision-preanswered"' in journal
    assert "decision-skipped-unattended" not in journal
    assert engine.state.tasks["dw-decision-dw-1"].phase == Phase.DONE
    assert ledger_entries(project)["DW-1"].status.startswith("done")
    # consumed: the entry left the open set, so its pre-answer is pruned
    assert decisions.load_pre_answers(project.project) == {}
    assert '"decision-preanswers-pruned"' in journal


def test_preanswered_keep_open_suppresses_prompt_and_persists(project):
    """A keep-open pre-answer is adopted (no skip, no re-prompt) and, since the
    entry stays open, the store keeps it for the next sweep too."""
    from automator import decisions
    from automator.sweep import DecisionOption

    write_ledger(project, {"DW-1": "open"})
    decisions.record_pre_answer(
        project.project,
        "DW-1",
        DecisionOption(key="2", label="Keep", effect="keep-open"),
        date="2026-06-12",
    )
    plan = triage_result(
        ["DW-1"],
        decisions=[
            _decision(
                "DW-1",
                [
                    {"key": "1", "label": "Build", "effect": "build", "intent": "x"},
                    {"key": "2", "label": "Keep", "effect": "keep-open"},
                ],
                recommendation="2",
            )
        ],
    )
    engine, adapter = make_sweep(project, [triage_effect(plan)], prompting=False)
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 1  # triage only — no bundle, no prompt
    journal = journal_text(engine)
    assert '"decision-preanswered"' in journal
    assert "decision-skipped-unattended" not in journal
    assert ledger_entries(project)["DW-1"].open
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "keep-open"


def test_max_bundles_truncation(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open", "DW-3": "open"})
    plan = triage_result(
        ["DW-1", "DW-2", "DW-3"],
        bundles=[
            {"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "second-fix", "dw_ids": ["DW-2"], "intent": "b"},
            {"name": "third-fix", "dw_ids": ["DW-3"], "intent": "c"},
        ],
    )
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(max_bundles=1))
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "first-fix", ["DW-1"]),
            bundle_review_effect(project, "first-fix"),
        ],
        policy=policy,
    )
    summary = engine.run()
    assert not summary.paused
    assert "sweep-bundles-truncated" in journal_text(engine)
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].open and entries["DW-3"].open


def test_escalated_bundle_resume_skips_it_and_runs_rest(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "bad-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "good-fix", "dw_ids": ["DW-2"], "intent": "b"},
        ],
    )

    def escalating_dev(spec):
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "escalations": [
                    {
                        "type": "bundle-item-blocked",
                        "severity": "CRITICAL",
                        "detail": "no",
                    }
                ],
            },
        )

    engine, _ = make_sweep(project, [triage_effect(plan), escalating_dev])
    summary = engine.run()
    assert summary.paused
    assert engine.state.tasks["dw-bad-fix"].phase == Phase.ESCALATED

    resumed, adapter = resume_sweep(
        project,
        engine,
        [
            bundle_dev_effect(project, "good-fix", ["DW-2"]),
            bundle_review_effect(project, "good-fix"),
        ],
    )
    summary = resumed.run()
    assert not summary.paused
    assert resumed.state.tasks["dw-good-fix"].phase == Phase.DONE
    # triage was NOT re-run: only the two bundle sessions
    assert len(adapter.sessions) == 2
    assert ledger_entries(project)["DW-1"].open  # escalated bundle untouched


# ----------------------------------------------------------- repeat cycles


def repeat_policy(**kw):
    return Policy(
        gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(repeat=True, **kw)
    )


def appending_dev(project, inner, dw_id):
    """Wrap a bundle dev effect so the session also appends a new open ledger
    entry — the 'sweep generated new deferred work' scenario."""

    def effect(spec):
        result = inner(spec)
        ledger = project.deferred_work
        ledger.write_text(
            ledger.read_text(encoding="utf-8")
            + f"\n### {dw_id}: item {dw_id}\n\norigin: test, 2026-06-11\n"
            f"location: src.txt:1\nreason: follow-up from bundle.\nstatus: open\n",
            encoding="utf-8",
        )
        return result

    return effect


def test_repeat_off_is_single_cycle(project):
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "one-fix", "dw_ids": ["DW-1"], "intent": "fix"}]
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan),
            appending_dev(project, bundle_dev_effect(project, "one-fix", ["DW-1"]), "DW-2"),
            bundle_review_effect(project, "one-fix"),
        ],
    )
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 3  # no second triage
    journal = journal_text(engine)
    assert "sweep-cycle" not in journal and "sweep-repeat-done" not in journal
    assert ledger_entries(project)["DW-2"].open  # waits for the next sweep


def test_repeat_two_cycles_then_no_open(project):
    write_ledger(project, {"DW-1": "open"})
    plan1 = triage_result(
        ["DW-1"], bundles=[{"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"}]
    )
    plan2 = triage_result(
        ["DW-2"], bundles=[{"name": "follow-up", "dw_ids": ["DW-2"], "intent": "b"}]
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan1),
            appending_dev(project, bundle_dev_effect(project, "first-fix", ["DW-1"]), "DW-2"),
            bundle_review_effect(project, "first-fix"),
            triage_effect(plan2),
            bundle_dev_effect(project, "follow-up", ["DW-2"]),
            bundle_review_effect(project, "follow-up"),
        ],
        policy=repeat_policy(),
    )
    summary = engine.run()
    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["sweep-triage"].phase == Phase.DONE
    assert tasks["dw-first-fix"].phase == Phase.DONE
    assert tasks["sweep-triage-2"].phase == Phase.DONE
    assert tasks["dw2-follow-up"].phase == Phase.DONE
    journal = journal_text(engine)
    assert "sweep-cycle" in journal
    assert "sweep-repeat-done" in journal and "no-open" in journal
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")
    assert worktree_clean(project.project)
    # cycle-2 dev got the cycle-scoped intent file
    intent_path = re.findall(r"`([^`]*)`", adapter.sessions[4].prompt)[0]
    assert "c2-follow-up" in intent_path


def test_repeat_stops_on_no_progress(project):
    write_ledger(project, {"DW-1": "open"})
    plan1 = triage_result(
        ["DW-1"], bundles=[{"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"}]
    )
    plan2 = triage_result(["DW-2"], blocked=[{"id": "DW-2", "blocker": "story 9-9"}])
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan1),
            appending_dev(project, bundle_dev_effect(project, "first-fix", ["DW-1"]), "DW-2"),
            bundle_review_effect(project, "first-fix"),
            triage_effect(plan2),
        ],
        policy=repeat_policy(),
    )
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 4  # the cycle-2 triage confirmed nothing addressable
    assert "no-progress" in journal_text(engine)
    assert ledger_entries(project)["DW-2"].open


def test_repeat_max_cycles_cap(project):
    write_ledger(project, {"DW-1": "open"})
    plan1 = triage_result(
        ["DW-1"], bundles=[{"name": "fix-one", "dw_ids": ["DW-1"], "intent": "a"}]
    )
    plan2 = triage_result(
        ["DW-2"], bundles=[{"name": "fix-two", "dw_ids": ["DW-2"], "intent": "b"}]
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan1),
            appending_dev(project, bundle_dev_effect(project, "fix-one", ["DW-1"]), "DW-2"),
            bundle_review_effect(project, "fix-one"),
            triage_effect(plan2),
            appending_dev(project, bundle_dev_effect(project, "fix-two", ["DW-2"]), "DW-3"),
            bundle_review_effect(project, "fix-two"),
        ],
        policy=repeat_policy(max_cycles=2),
    )
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 6  # no cycle-3 triage despite DW-3 open
    assert "max-cycles" in journal_text(engine)
    assert ledger_entries(project)["DW-3"].open


def test_repeat_failed_bundle_not_rebuilt(project):
    """A bundle that deferred in cycle 1 must not be re-materialized when a
    later triage re-proposes its ids — that would loop until max_cycles."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan1 = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "bad-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "good-fix", "dw_ids": ["DW-2"], "intent": "b"},
        ],
    )
    plan2 = triage_result(
        ["DW-1"], bundles=[{"name": "bad-fix-again", "dw_ids": ["DW-1"], "intent": "a2"}]
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        sweep=SweepPolicy(repeat=True),
        limits=LimitsPolicy(max_review_cycles=1, max_dev_attempts=1),
        scm=ScmPolicy(rollback_on_failure=True),  # exercise defer-and-continue
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan1),
            # bad-fix: spec never reaches in-review -> dev verify fails -> deferred
            lambda spec: SessionResult(
                status="completed", result_json={"workflow": "auto-dev", "escalations": []}
            ),
            bundle_dev_effect(project, "good-fix", ["DW-2"]),
            bundle_review_effect(project, "good-fix"),
            triage_effect(plan2),
        ],
        policy=policy,
    )
    summary = engine.run()
    assert not summary.paused
    assert engine.state.tasks["dw-bad-fix"].phase == Phase.DEFERRED
    assert "sweep-bundle-skipped" in journal_text(engine)
    assert not any(k.startswith("dw2-") for k in engine.state.tasks)
    assert "no-progress" in journal_text(engine)
    assert ledger_entries(project)["DW-1"].open


def test_repeat_keep_open_answer_blocks_rebundle(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    decision = {
        "id": "DW-1",
        "question": "build it?",
        "context": "",
        "options": [
            {"key": "1", "label": "Build", "effect": "build", "intent": "x"},
            {"key": "2", "label": "Keep open", "effect": "keep-open"},
        ],
        "recommendation": "2",
    }
    plan1 = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "safe-fix", "dw_ids": ["DW-2"], "intent": "fix"}],
        decisions=[decision],
    )
    # cycle 2: triage tries to bundle the kept-open entry directly
    plan2 = triage_result(
        ["DW-1"], bundles=[{"name": "sneaky-fix", "dw_ids": ["DW-1"], "intent": "y"}]
    )
    engine, adapter = make_sweep(
        project,
        [
            triage_effect(plan1),
            bundle_dev_effect(project, "safe-fix", ["DW-2"]),
            bundle_review_effect(project, "safe-fix"),
            triage_effect(plan2),
        ],
        policy=repeat_policy(),
        answers=["2"],
        prompting=True,
    )
    summary = engine.run()
    assert not summary.paused
    journal = journal_text(engine)
    assert "sweep-bundle-skipped" in journal and "human-chose-keep-open" in journal
    assert not any("sneaky-fix" in k for k in engine.state.tasks)
    assert ledger_entries(project)["DW-1"].open


def test_repeat_resume_mid_cycle_two(project):
    write_ledger(project, {"DW-1": "open"})
    plan1 = triage_result(
        ["DW-1"], bundles=[{"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"}]
    )
    plan2 = triage_result(
        ["DW-2"], bundles=[{"name": "follow-up", "dw_ids": ["DW-2"], "intent": "b"}]
    )

    def escalating_dev(spec):
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "escalations": [
                    {"type": "bundle-item-blocked", "severity": "CRITICAL", "detail": "no"}
                ],
            },
        )

    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan1),
            appending_dev(project, bundle_dev_effect(project, "first-fix", ["DW-1"]), "DW-2"),
            bundle_review_effect(project, "first-fix"),
            triage_effect(plan2),
            escalating_dev,
        ],
        policy=repeat_policy(),
    )
    summary = engine.run()
    assert summary.paused
    assert load_state(engine.run_dir).sweep_cycle == 2
    assert engine.state.tasks["dw2-follow-up"].phase == Phase.ESCALATED

    resumed, adapter = resume_sweep(project, engine, [])
    summary = resumed.run()
    assert not summary.paused
    # resume re-enters cycle 2 directly: triage-2.json reloads (no session),
    # the escalated bundle is dropped by the failed-ids filter, and the cycle
    # reports no progress
    assert adapter.sessions == []
    journal = journal_text(resumed)
    assert "sweep-bundle-skipped" in journal and "no-progress" in journal
    assert ledger_entries(project)["DW-2"].open  # escalated bundle untouched


def test_repeat_decisions_only_single_cycle(project):
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "one-fix", "dw_ids": ["DW-1"], "intent": "fix"}]
    )
    engine, adapter = make_sweep(
        project, [triage_effect(plan)], policy=repeat_policy(), decisions_only=True
    )
    summary = engine.run()
    assert not summary.paused
    assert len(adapter.sessions) == 1
    journal = journal_text(engine)
    assert "sweep-decisions-only" in journal and "sweep-cycle" not in journal


def test_repeat_unattended_decision_notifies_once(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    decision = {
        "id": "DW-1",
        "question": "q",
        "context": "",
        "options": [
            {"key": "1", "label": "a", "effect": "build", "intent": "x"},
            {"key": "2", "label": "b", "effect": "keep-open"},
        ],
        "recommendation": "2",
    }
    plan1 = triage_result(
        ["DW-1", "DW-2"],
        bundles=[{"name": "safe-fix", "dw_ids": ["DW-2"], "intent": "fix"}],
        decisions=[decision],
    )
    plan2 = triage_result(["DW-1"], decisions=[decision])
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan1),
            bundle_dev_effect(project, "safe-fix", ["DW-2"]),
            bundle_review_effect(project, "safe-fix"),
            triage_effect(plan2),
        ],
        policy=repeat_policy(),
        prompting=False,
    )
    summary = engine.run()
    assert not summary.paused
    assert journal_text(engine).count("decision-skipped-unattended") == 1
    assert "no-progress" in journal_text(engine)


def test_repeat_truncated_bundles_picked_up_next_cycle(project):
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan1 = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "second-fix", "dw_ids": ["DW-2"], "intent": "b"},
        ],
    )
    plan2 = triage_result(
        ["DW-2"], bundles=[{"name": "second-fix", "dw_ids": ["DW-2"], "intent": "b"}]
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan1),
            bundle_dev_effect(project, "first-fix", ["DW-1"]),
            bundle_review_effect(project, "first-fix"),
            triage_effect(plan2),
            bundle_dev_effect(project, "second-fix", ["DW-2"]),
            bundle_review_effect(project, "second-fix"),
        ],
        policy=repeat_policy(max_bundles=1),
    )
    summary = engine.run()
    assert not summary.paused
    assert "sweep-bundles-truncated" in journal_text(engine)
    # same bundle name across cycles lands under distinct task keys
    assert engine.state.tasks["dw-first-fix"].phase == Phase.DONE
    assert engine.state.tasks["dw2-second-fix"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")


# ------------------------------------------------------- legacy migration


def test_sweep_migrates_legacy_then_triages_and_runs_bundle(project):
    write_legacy_ledger(project, LEGACY_LEDGER)
    manifest = legacy_manifest()
    mapping = [
        {"key": manifest[0]["key"], "dw_id": "DW-1"},
        {"key": manifest[1]["key"], "dw_id": "DW-2"},
    ]
    plan = triage_result(
        ["DW-2"],
        bundles=[{"name": "fix-emdash", "dw_ids": ["DW-2"], "intent": "guard em-dashes"}],
    )
    engine, adapter = make_sweep(
        project,
        [
            migrate_effect(project, migrated_ledger(), mapping),
            triage_effect(plan),
            bundle_dev_effect(project, "fix-emdash", ["DW-2"]),
            bundle_review_effect(project, "fix-emdash"),
        ],
    )
    summary = engine.run()

    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["sweep-migrate"].phase == Phase.DONE
    assert tasks["sweep-triage"].phase == Phase.DONE
    assert tasks["dw-fix-emdash"].phase == Phase.DONE

    text = project.deferred_work.read_text(encoding="utf-8")
    assert not deferredwork.has_legacy(text)
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")
    assert entries["DW-2"].status.startswith("done")  # bundle closed it

    log = git(project.project, "log", "--oneline")
    assert "chore(sweep): migrate legacy deferred-work entries to DW format" in log
    journal = journal_text(engine)
    assert "sweep-migrated" in journal and "sweep-nothing-open" not in journal

    # the migration session was prompted with the manifest path
    assert "--migrate" in adapter.sessions[0].prompt
    manifest_path = adapter.sessions[0].prompt.split("--migrate ", 1)[1].split()[0]
    written = json.loads(open(manifest_path).read())
    assert [m["key"] for m in written] == [m["key"] for m in manifest]
    # triage ran against the post-migration open set, strict check intact
    assert "--migrate" not in adapter.sessions[1].prompt


def test_migration_validation_failure_restores_ledger_then_escalates(project):
    write_legacy_ledger(project, LEGACY_LEDGER)
    manifest = legacy_manifest()
    # converts only the done item; the open one remains legacy -> invalid
    half = (
        "# Deferred Work\n\n"
        "### DW-1: Old fixed thing\n\norigin: migrated, 2026-06-12\nlocation: n/a\n"
        "reason: repaired.\nstatus: done 2026-04-06\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- **Open legacy thing here** — `src.txt` mishandles em-dashes\n"
    )
    bad = migrate_effect(project, half, [{"key": manifest[0]["key"], "dw_id": "DW-1"}])
    engine, adapter = make_sweep(project, [bad, bad])
    summary = engine.run()

    assert summary.paused
    assert engine.state.tasks["sweep-migrate"].phase == Phase.ESCALATED
    # the broken rewrite never sticks: original ledger text restored
    assert project.deferred_work.read_text(encoding="utf-8") == LEGACY_LEDGER
    assert worktree_clean(project.project)
    prompts = [s.prompt for s in adapter.sessions]
    assert len(prompts) == 2
    assert "--feedback" not in prompts[0] and "--feedback" in prompts[1]
    feedback = open(prompts[1].split("--feedback ", 1)[1]).read()
    assert "still parse as legacy" in feedback and "not mapped" in feedback


def test_migration_escalation_resume_retries(project):
    write_legacy_ledger(project, LEGACY_LEDGER)
    manifest = legacy_manifest()
    bad = migrate_effect(project, LEGACY_LEDGER, [])  # no conversion at all
    engine, _ = make_sweep(project, [bad, bad])
    assert engine.run().paused

    mapping = [
        {"key": manifest[0]["key"], "dw_id": "DW-1"},
        {"key": manifest[1]["key"], "dw_id": "DW-2"},
    ]
    plan = triage_result(["DW-2"], skip=[{"id": "DW-2", "reason": "moot"}])
    resumed, adapter = resume_sweep(
        project,
        engine,
        [migrate_effect(project, migrated_ledger(), mapping), triage_effect(plan)],
    )
    summary = resumed.run()
    assert not summary.paused
    assert resumed.state.tasks["sweep-migrate"].phase == Phase.DONE
    assert resumed.state.tasks["sweep-triage"].phase == Phase.DONE
    assert len(adapter.sessions) == 2


def test_no_legacy_skips_migration(project):
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(["DW-1"], skip=[{"id": "DW-1", "reason": "moot"}])
    engine, adapter = make_sweep(project, [triage_effect(plan)])
    assert not engine.run().paused
    assert "sweep-migrate" not in engine.state.tasks
    assert "--migrate" not in adapter.sessions[0].prompt


def test_mixed_ledger_migration_preserves_canonical_open_set(project):
    mixed = (
        "# Deferred Work\n\n"
        "### DW-1: item DW-1\n\norigin: test, 2026-06-01\nlocation: src.txt:1\n"
        "reason: test entry.\nstatus: open\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- **Open legacy thing here** — `src.txt` mishandles em-dashes\n"
    )
    write_legacy_ledger(project, mixed)
    manifest = legacy_manifest(mixed)
    assert len(manifest) == 1  # the canonical entry is not a legacy item
    migrated = (
        "# Deferred Work\n\n"
        "### DW-1: item DW-1\n\norigin: test, 2026-06-01\nlocation: src.txt:1\n"
        "reason: test entry.\nstatus: open\n\n"
        "### DW-2: Open legacy thing here\n\n"
        "origin: migrated from legacy ledger, 2026-06-12\nlocation: src.txt\n"
        "reason: mishandles em-dashes.\nstatus: open\n"
    )
    plan = triage_result(
        ["DW-1", "DW-2"],
        skip=[{"id": "DW-1", "reason": "moot"}, {"id": "DW-2", "reason": "moot"}],
    )
    engine, _ = make_sweep(
        project,
        [
            migrate_effect(project, migrated, [{"key": manifest[0]["key"], "dw_id": "DW-2"}]),
            triage_effect(plan),
        ],
    )
    summary = engine.run()
    assert not summary.paused
    assert engine.state.tasks["sweep-migrate"].phase == Phase.DONE
    assert engine.state.tasks["sweep-triage"].phase == Phase.DONE
    assert ledger_entries(project)["DW-1"].open  # skipped, untouched


# ------------------------------------------ review-budget commit-instead-of-rollback


def test_sweep_bundle_budget_exhausted_commits_and_refiles(project):
    """A bundle whose review keeps recommending a follow-up but is finalized
    (spec done, owned dw ids closed, verify green) is COMMITTED when the review
    budget is exhausted — not rolled back. The lingering follow-up is re-filed as
    a fresh open deferred-work entry."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"],
        bundles=[{"name": "fix-it", "dw_ids": ["DW-1"], "intent": "fix it"}],
    )
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_effect(project, "fix-it", ["DW-1"])]
        + [bundle_review_effect(project, "fix-it", clean=False) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["dw-fix-it"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")  # the worked item closed
    refiled = [e for e in entries.values() if e.open and "origin: review-budget-followup" in e.body]
    assert len(refiled) == 1
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-budget-committed" in kinds and "story-deferred" not in kinds


def test_sweep_bundle_budget_followup_not_refiled_twice(project):
    """Re-review cap: when a bundle itself closes a `review-budget-followup` entry
    and still won't converge, the work is committed but NOT re-filed again — a
    second non-convergence should reach a human, not loop across sweeps."""
    ledger = (
        "# Deferred Work\n\n"
        "### DW-1: follow-up still recommended for dw-prior\n"
        "origin: review-budget-followup\nsource_spec: `spec-dw-fix-it.md`\n"
        "severity: low\nreason: a prior budget exhaustion.\nstatus: open\n"
    )
    project.deferred_work.write_text(ledger, encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "ledger")
    plan = triage_result(
        ["DW-1"],
        bundles=[{"name": "fix-it", "dw_ids": ["DW-1"], "intent": "fix it"}],
    )
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_effect(project, "fix-it", ["DW-1"])]
        + [bundle_review_effect(project, "fix-it", clean=False) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.deferred == 0 and not summary.paused
    assert engine.state.tasks["dw-fix-it"].phase == Phase.DONE
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")  # the worked entry still closes
    open_followups = [
        e for e in entries.values() if e.open and "origin: review-budget-followup" in e.body
    ]
    assert open_followups == []  # no second follow-up entry created
    capped = [e for e in engine.journal.entries() if e["kind"] == "review-budget-committed"]
    assert len(capped) == 1 and capped[0]["re_review_capped"] is True
