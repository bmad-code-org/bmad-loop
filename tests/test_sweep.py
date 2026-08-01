"""Sweep engine scenario tests against the mock adapter — no tmux, no LLM."""

import json
import re
from pathlib import Path

import pytest
from conftest import (
    bundle_dev_effect,
    bundle_dev_escalates,
    bundle_review_effect,
    fault_read_text,
    git,
    migrate_effect,
    passes_once,
    triage_effect,
    write_ledger,
    write_legacy_ledger,
    write_spec,
)

from bmad_loop import deferredwork, runs, verify
from bmad_loop.adapters.base import SessionResult
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import Phase, RunState, StoryTask, TokenUsage
from bmad_loop.policy import (
    AdapterPolicy,
    DevPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ReviewPolicy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    VerifyPolicy,
)
from bmad_loop.sweep import DecisionPrompter, SweepEngine, validate_migration, validate_triage
from bmad_loop.verify import worktree_clean

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
    run_dir = project.project / ".bmad-loop" / "runs" / "sweep-run"
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

    from bmad_loop.verify import branch_exists, rev_parse_head, worktree_list

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
    assert not branch_exists(project.project, "bmad_loop/sweep-run/dw-fix")
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
    assert "sweep dw-fix-things: DW-2, DW-3 via bmad-loop" in log

    # dev session was invoked in bundle mode with the rendered intent file
    dev_spec = adapter.sessions[1]
    assert "Implement the deferred-work bundle" in dev_spec.prompt
    intent_path = re.findall(r"`([^`]*)`", dev_spec.prompt)[0]
    intent = open(intent_path).read()
    assert "fix both" in intent and "DW-2" in intent and "### DW-3" in intent


def test_generic_skill_bundle_orchestrator_closes_ledger(project):
    """B4: on the generic bmad-dev-auto path the bundle session never edits the
    ledger; the orchestrator marks each owned dw id done (in
    _post_dev_accepted_sync) and verify_review_bundle confirms its own write. The
    invocation is freeform."""
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


def test_bundle_ledger_close_withheld_on_a_non_fixable_retry(project):
    """The RETRY leg of the accepted-only close. The orchestrator closes a bundle's
    dw ids in `_post_dev_accepted_sync`, below the artifact gate that can still send
    the attempt back — so a close can never outlive the code it claims to resolve.

    A close is the most expensive engine-side ledger write to leave behind:
    `deferredwork.open_ids` only ever re-bundles `open` entries and there is no
    reopen primitive, so a `done` id with no code behind it is invisible to every
    future sweep, where a stray harvest entry merely reads as noise.

    The ledger here is TRACKED, so `reset --hard` would revert a close by itself —
    which is what makes this leg the WEAK one and the DEFER leg
    (`..._when_a_completed_attempt_defers`) the load-bearing one. Kept because it
    pins the mid-run state no other test sees: attempt 2 opens on a ledger that was
    never rewritten, rather than on one rewritten and then put back."""
    write_ledger(project, {"DW-1": "open"})  # committed ⇒ tracked
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    seen = []

    def capture_then_die(spec):
        seen.append(project.deferred_work.read_text(encoding="utf-8"))
        return SessionResult(status="died")  # budget spent ⇒ DEFER, the run ends

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_dev_attempts=2),
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            # reports dw_ids the task does not own, so the artifact gate fails on the
            # CLAIM — after a spec status (`done`) that would otherwise authorize the
            # close. The attempt is discarded; the ledger must never have been touched.
            bundle_dev_effect(project, "fix", ["DW-99"], mark_ledger=False, followup_review=False),
            capture_then_die,
        ],
        policy=pol,
    )
    summary = engine.run()

    # the scenario really is: spec finalized, gate failed, rolled back
    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert decisions[0]["action"] == "retry" and "do not match" in decisions[0]["reason"]
    assert "rollback-auto" in journal_text(engine)
    assert "sweep-bundle-closed" not in {e["kind"] for e in engine.journal.entries()}

    # attempt 2 opens on a ledger the discarded attempt never rewrote …
    assert "status: open" in seen[0]
    assert "resolved by sweep bundle dw-fix" not in seen[0]
    # … and that is where the run leaves it, so a later sweep can re-bundle DW-1
    assert summary.deferred == 1 and not summary.paused
    assert ledger_entries(project)["DW-1"].open


def test_bundle_ledger_close_withheld_when_a_completed_attempt_defers(project):
    """The DEFER terminus — the leg that made the close's position a CORRECTNESS
    question rather than a tidiness one, and the leg no ledger snapshot in
    `_dev_phase` could ever have covered.

    Differs from `..._on_a_non_fixable_retry` above by exactly one thing: attempt 2
    is a second COMPLETED session rather than a corpse. That is enough. A completed
    attempt runs the engine-side ledger writes; the gate then fails on the dw_ids
    claim with the budget spent, so `decide_dev` returns DEFER — and `_defer` takes
    its own snapshot of the ledger AFTER those writes, resets, and writes the
    snapshot back on purpose, so harvested findings survive a defer. A close written
    above the gate rode that write-back over the top of the rollback that had just
    correctly reverted it: `status: done` on an id whose code no longer exists, and
    `open_ids` never re-bundles it, so the work is silently LOST. Closing only for an
    accepted attempt means there is nothing for `_defer` to resurrect.

    The `dev-decision` assert is load-bearing: with `max_dev_attempts = 1` the
    budget is already spent at attempt 1 and the run DEFERS without ever reaching a
    RETRY, so the ledger is never rewritten and the test would pass vacuously."""
    write_ledger(project, {"DW-1": "open"})  # committed ⇒ tracked
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )

    def mismatching_attempt():
        return bundle_dev_effect(
            project, "fix", ["DW-99"], mark_ledger=False, followup_review=False
        )

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_dev_attempts=2),
    )
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), mismatching_attempt(), mismatching_attempt()],
        policy=pol,
    )
    summary = engine.run()

    actions = [e["action"] for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert actions == ["retry", "defer"]
    assert summary.deferred == 1 and not summary.paused
    text = project.deferred_work.read_text(encoding="utf-8")
    assert "resolved by sweep bundle dw-fix" not in text
    assert deferredwork.open_ids(text) == {"DW-1"}
    assert "change for dw-fix" not in (project.project / "src.txt").read_text()
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "sweep-bundle-closed" not in kinds
    # ...and the defer's own reopen stayed silent rather than journalling an empty
    # list: withholding the close is what fixes this leg, not undoing one
    assert "sweep-bundle-reopened" not in kinds


def test_bundle_ledger_close_withheld_when_a_critical_escalation_preempts_the_gate(project):
    """The close is gated on the DECISION being PROCEED, not on `outcome.ok`, and
    this is the one scenario that can tell the two apart.

    `escalation.decide_dev` checks `critical_escalations(result_json)` before it
    looks at the outcome at all, so a session that finalized its spec, produced a
    matching `dw_ids` claim and passed every artifact gate still PAUSEs when it
    raises a CRITICAL. Gating on `outcome.ok` would close the ids on the way to a
    human escalation — recording as resolved a bundle whose whole point is that the
    orchestrator could not resolve it.

    Distinct from `test_escalated_bundle_resume_skips_it_and_runs_rest`, which uses
    `bundle_dev_escalates`: that session writes its spec `blocked`, so
    `_close_bundle_ledger_when_spec_status`'s own status guard already refuses and
    the escalation proves nothing about the decision gate."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    clean = bundle_dev_effect(project, "fix", ["DW-1"], mark_ledger=False, followup_review=False)

    def clean_but_critical(spec):
        result = clean(spec)
        result.result_json["escalations"] = [
            {"type": "bundle-item-blocked", "severity": "CRITICAL", "detail": "intent gap"}
        ]
        return result

    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_sweep(project, [triage_effect(plan), clean_but_critical], policy=pol)
    summary = engine.run()

    # the gate really did pass — the PAUSE comes from the escalation, not a failure
    decisions = [e for e in engine.journal.entries() if e["kind"] == "dev-decision"]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "pause" and "CRITICAL" in decisions[0]["reason"]
    assert summary.paused
    assert ledger_entries(project)["DW-1"].open
    assert "sweep-bundle-closed" not in {e["kind"] for e in engine.journal.entries()}


def test_bundle_ledger_close_reopened_when_the_review_leg_defers(project):
    """The REVIEW-leg counterpart, which withholding the close cannot reach.

    By review time the close is not premature — the dev attempt WAS accepted, and
    `verify_review_bundle` requires the entries `done`, so they have to be on disk.
    Only the later review failure makes them wrong, and then `_defer` writes its own
    post-close snapshot back over the `reset --hard` that had correctly reverted
    them. Deliberately, because a harvested finding's ledger entry is its only
    surviving record once `_stash_deferred_artifacts` moves the spec away — but the
    restore replays the whole file, so the closes ride it too and name code that no
    longer exists. `_reopen_ledger_after_defer` takes back exactly this run's own.

    Route: the final review session dies with the cycle budget spent, so
    `decide_review_session` returns DEFER."""
    write_ledger(project, {"DW-1": "open"})  # committed ⇒ tracked
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=True, trigger="always"),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_review_cycles=1),
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "fix", ["DW-1"], mark_ledger=False),
            lambda spec: SessionResult(status="died"),
        ],
        policy=pol,
    )
    summary = engine.run()

    kinds = [e["kind"] for e in engine.journal.entries()]
    # the scenario really is: closed at dev, then discarded by a review-leg defer
    assert "sweep-bundle-closed" in kinds
    assert summary.deferred == 1 and not summary.paused
    assert "change for dw-fix" not in (project.project / "src.txt").read_text()

    text = project.deferred_work.read_text(encoding="utf-8")
    assert deferredwork.open_ids(text) == {"DW-1"}
    assert "resolved by sweep bundle dw-fix" not in text
    reopened = [e for e in engine.journal.entries() if e["kind"] == "sweep-bundle-reopened"]
    assert len(reopened) == 1 and reopened[0]["dw_ids"] == ["DW-1"]
    # the tree is clean: the reopen restored the ledger byte-for-byte, so the defer
    # leaves nothing staged for the next cycle's baseline
    assert worktree_clean(project.project)


def test_bundle_ledger_close_reopened_when_review_disabled_defers_at_the_gate(project, tmp_path):
    """The second route into the same `_defer`: with `review.enabled = false` there is
    no review session to decide anything, and `_skip_review_and_commit` defers
    directly when its own verify fails.

    `passes_once` is GREEN at the dev gate — letting the attempt be accepted and the
    ids closed — and RED at the review gate. Nothing else can produce that ordering:
    every other input to `verify_review_bundle` is already satisfied by the accepted
    dev attempt. It latches in the run dir and is written for cmd as well as sh,
    because the Windows CI lane runs this test too and mis-PARSES a POSIX command
    rather than failing on it."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
        verify=VerifyPolicy(commands=(passes_once(tmp_path / "verify-ran"),)),
        # no dev budget left for `_skip_review_and_commit`'s repair phase, so the
        # fixable verify failure goes straight to the defer this test is about
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "fix", ["DW-1"], mark_ledger=False, followup_review=False),
        ],
        policy=pol,
    )
    summary = engine.run()

    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-bundle-closed" in kinds
    assert summary.deferred == 1 and not summary.paused
    assert "change for dw-fix" not in (project.project / "src.txt").read_text()
    assert deferredwork.open_ids(project.deferred_work.read_text(encoding="utf-8")) == {"DW-1"}
    assert "sweep-bundle-reopened" in kinds


def test_bundle_ledger_close_skips_on_unreadable_spec(project, monkeypatch):
    """The bundle counterpart of the sprint-board sync: an unreadable bundle spec
    must not close any dw id (the ledger write is a repair — it must never fire off
    an observation the orchestrator could not make) and must not crash the sweep."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
        dev=DevPolicy(skill="bmad-dev-auto"),
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_sweep(project, [], policy=pol)
    sp = project.implementation_artifacts / "spec-dw-fix-things.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_spec(sp, "done", "abc123")
    task = StoryTask(story_key="dw-fix-things", epic=0, dw_ids=["DW-1", "DW-2"])
    fault_read_text(monkeypatch, sp)

    engine._post_dev_accepted_sync(task, {"spec_file": str(sp)})

    entries = ledger_entries(project)
    assert entries["DW-1"].status == "open" and entries["DW-2"].status == "open"
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-bundle-closed" not in kinds
    events = [e for e in engine.journal.entries() if e["kind"] == "spec-read-failed"]
    assert len(events) == 1 and events[0]["site"] == "bundle-ledger-close"
    assert events[0]["story_key"] == "dw-fix-things"


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
        "bmad_loop.tui.launch.return_attached_client",
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
    monkeypatch.setattr("bmad_loop.tui.launch.return_attached_client", lambda: False)
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


def test_triage_session_stamps_resolved_adapter_identity(project):
    """#153 phase 1: the sweep triage session's session-start entry and persisted
    SessionRecord carry the resolved triage adapter profile + model. A triage-stage
    name override switches the profile and resets the model to the CLI default ""."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"],
        already_resolved=[{"id": "DW-1", "evidence": "already guarded at src.txt:1"}],
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            triage=StageAdapterPolicy(name="gemini"),
        ),
    )
    engine, adapter = make_sweep(project, [triage_effect(plan)], policy=policy)
    summary = engine.run()
    assert not summary.paused
    assert [s.role for s in adapter.sessions] == ["triage"]

    expected = policy.adapter.resolved("triage")
    assert (expected.name, expected.model) == ("gemini", "")  # client switch resets model

    entries = [json.loads(line) for line in journal_text(engine).splitlines()]
    starts = [e for e in entries if e["kind"] == "session-start" and e.get("role") == "triage"]
    assert len(starts) == 1
    assert starts[0]["adapter"] == "gemini"
    assert starts[0]["model"] == ""
    assert starts[0]["story_key"] == "sweep-triage"

    saved = load_state(engine.run_dir)
    rec = saved.tasks["sweep-triage"].sessions[-1]
    assert rec.role == "triage"
    assert (rec.adapter, rec.model) == ("gemini", "")


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
    from bmad_loop import decisions
    from bmad_loop.sweep import DecisionOption

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
    from bmad_loop import decisions
    from bmad_loop.sweep import DecisionOption

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


# ------------------------------ intent-gap patch-restore re-drive (#75)


def _run_to_dev_escalation(project, policy=None):
    """Drive a one-bundle sweep until its dev session escalates on an intent gap.
    Returns the paused engine; the bundle task is ESCALATED with spec_file set and
    DW-1 still open (blocked spec is not synced done)."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_escalates(project, "fix", ["DW-1"])],
        policy=policy,
    )
    summary = engine.run()
    assert summary.paused
    task = engine.state.tasks["dw-fix"]
    assert task.phase == Phase.ESCALATED
    assert task.spec_file  # latched by _record_dev_spec on the dev escalation
    assert ledger_entries(project)["DW-1"].open  # blocked spec not synced done
    return engine


def test_generic_bundle_prompt_restore_branch_points_at_spec(project):
    # T-A: the restore-aware prompt (Change A) — no run needed.
    engine, _ = make_sweep(project, [])
    spec = str(project.implementation_artifacts / "spec-dw-fix.md")
    task = StoryTask(
        story_key="dw-fix",
        epic=0,
        dw_ids=["DW-1"],
        bundle_file="/run/bundles/fix/intent.md",
        spec_file=spec,
        restore_patch="/run/artifacts/attempt-dw-fix.patch",
    )
    restore_prompt = engine._generic_bundle_prompt(task, None)
    assert "Resume review of the in-review spec" in restore_prompt
    assert spec in restore_prompt
    assert "Do NOT edit the deferred-work ledger" in restore_prompt
    # without a latched patch the fresh-implement prompt is unchanged
    task.restore_patch = None
    fresh_prompt = engine._generic_bundle_prompt(task, None)
    assert "Implement the deferred-work bundle" in fresh_prompt
    assert "Resume review of the in-review spec" not in fresh_prompt


def test_sweep_bundle_restore_redrive_reaches_done_and_clears_latch(project, monkeypatch):
    # T-C + T-D: rearm with a restore patch, resume, land done; assert the dispatched
    # prompt pointed at the in-review spec, the patch apply seam fired, the dw id
    # closed, and both latches cleared on commit (inherited Engine._commit).
    monkeypatch.setattr(verify, "apply_patch", lambda repo, patch: None)
    engine = _run_to_dev_escalation(project)
    patch = project.implementation_artifacts / "attempt-dw-fix.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("dummy\n")

    runs.rearm_escalation(engine.run_dir, "dw-fix", restore_patch=str(patch))

    resumed, adapter = resume_sweep(
        project,
        engine,
        [bundle_dev_effect(project, "fix", ["DW-1"]), bundle_review_effect(project, "fix")],
    )
    summary = resumed.run()

    assert not summary.paused
    task = resumed.state.tasks["dw-fix"]
    assert task.phase == Phase.DONE
    # Change A: the re-drive dev session was pointed at the in-review spec
    spec = str(project.implementation_artifacts / "spec-dw-fix.md")
    assert "Resume review of the in-review spec" in adapter.sessions[0].prompt
    assert spec in adapter.sessions[0].prompt
    # the restore apply seam fired
    assert "attempt-restored" in journal_text(resumed)
    # latches cleared on commit
    assert task.restore_patch is None
    assert task.resolved_redrive is False
    # the deferred-work id was closed
    assert ledger_entries(project)["DW-1"].status.startswith("done")


def test_sweep_restore_redrive_exhaustion_pauses_not_defers(project, monkeypatch):
    # T-B (restore): a non-critical exhaustion mid-restore-re-drive must PAUSE
    # (re-escalate for the human), not silently DEFER the resolved escalation.
    monkeypatch.setattr(verify, "apply_patch", lambda repo, patch: None)
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine = _run_to_dev_escalation(project, policy=policy)
    patch = project.implementation_artifacts / "attempt-dw-fix.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("dummy\n")
    runs.rearm_escalation(engine.run_dir, "dw-fix", restore_patch=str(patch))

    resumed, _ = resume_sweep(project, engine, [lambda spec: SessionResult(status="died")])
    summary = resumed.run()

    assert summary.paused
    assert resumed.state.tasks["dw-fix"].phase == Phase.ESCALATED


def test_sweep_from_scratch_redrive_exhaustion_pauses_not_defers(project):
    # T-B (from-scratch twin): the resolved_redrive latch also protects a plain
    # `resolve` re-drive (no --restore-patch) — the pre-existing sweep defer bug
    # Change B closes. Without the fix this exhaustion would DEFER, not PAUSE.
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine = _run_to_dev_escalation(project, policy=policy)
    runs.rearm_escalation(engine.run_dir, "dw-fix")  # from-scratch, no restore

    resumed, _ = resume_sweep(project, engine, [lambda spec: SessionResult(status="died")])
    summary = resumed.run()

    assert summary.paused
    assert resumed.state.tasks["dw-fix"].phase == Phase.ESCALATED


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
    # pin the damping cap high so this exercises the max_review_cycles exhaustion
    # path (the damped force-converge has its own sweep test below).
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_effect(project, "fix-it", ["DW-1"])]
        + [bundle_review_effect(project, "fix-it", clean=False) for _ in range(3)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
        ),
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
    # pin the damping cap high so this exercises the max_review_cycles exhaustion
    # re-review cap (the damped re-review cap has its own sweep test below).
    engine, _ = make_sweep(
        project,
        [triage_effect(plan), bundle_dev_effect(project, "fix-it", ["DW-1"])]
        + [bundle_review_effect(project, "fix-it", clean=False) for _ in range(3)],
        policy=Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            scm=ScmPolicy(rollback_on_failure=True),
            limits=LimitsPolicy(max_followup_reviews=99),
        ),
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


def test_sweep_bundle_followup_damped_commits_and_refiles(project):
    """Default damping cap (1): a bundle whose review keeps recommending a follow-up
    converges after ONE honored round instead of burning the whole review budget.
    The lingering follow-up is re-filed once, the work is committed, and — the
    steady state — the damped converge stays quiet (no review-budget ATTENTION)."""
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
    task = engine.state.tasks["dw-fix-it"]
    assert task.phase == Phase.DONE
    assert task.review_cycle == 2  # converged after one honored follow-up (3rd review unused)
    assert task.followup_reviews_spent == 1
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")  # the worked item closed
    refiled = [e for e in entries.values() if e.open and "origin: review-budget-followup" in e.body]
    assert len(refiled) == 1
    kinds = {e["kind"] for e in engine.journal.entries()}
    assert "review-followup-damped" in kinds
    assert "review-budget-committed" not in kinds and "story-deferred" not in kinds
    attention = engine.run_dir / "ATTENTION"
    assert not attention.exists() or "review budget reached" not in attention.read_text()


def test_sweep_bundle_damped_re_review_capped_notifies_not_refiles(project):
    """Re-review cap survives damping: when a bundle itself closes a
    `review-budget-followup` entry and still won't converge, the damped force-
    converge commits but does NOT re-file again — and, unlike an ordinary quiet
    damped converge, it raises an ATTENTION notice so a human sees the repeat."""
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
    capped = [e for e in engine.journal.entries() if e["kind"] == "review-followup-damped"]
    assert len(capped) == 1 and capped[0]["re_review_capped"] is True
    # the loud re-review path fires even under damping — a human must see it
    assert "review budget reached" in (engine.run_dir / "ATTENTION").read_text(encoding="utf-8")


# ------------------------------ in-flight bundle recovery on resume (#94)


def _lose_triage(run_dir, corruption="missing"):
    """Make the cached triage plan unusable the three ways a real run can: the
    file vanished, it was truncated mid-write, or it holds something that is not
    a triage result."""
    path = run_dir / "triage.json"
    if corruption == "missing":
        path.unlink()
    elif corruption == "invalid-json":
        path.write_text("{{{", encoding="utf-8")
    else:
        path.write_text("{}", encoding="utf-8")


def _run_two_bundle_dev_escalation(project):
    """Drive a two-bundle sweep until the first bundle's dev session escalates.
    Returns the paused engine; `dw-fix` is ESCALATED, `dw-other` never started,
    both dw ids still open."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"},
            {"name": "other", "dw_ids": ["DW-2"], "intent": "resolve DW-2"},
        ],
    )
    engine, _ = make_sweep(
        project, [triage_effect(plan), bundle_dev_escalates(project, "fix", ["DW-1"])]
    )
    assert engine.run().paused
    assert engine.state.tasks["dw-fix"].phase == Phase.ESCALATED
    assert "dw-other" not in engine.state.tasks
    return engine


def _redrive_script(project):
    return [bundle_dev_effect(project, "fix", ["DW-1"]), bundle_review_effect(project, "fix")]


def test_rearmed_bundle_redrives_when_triage_json_lost(project):
    # The regression: a human-resolved bundle used to re-drive only because the
    # cached triage plan reloaded and re-emitted its name. Recovery now keys on
    # the persisted task, so losing the cache changes nothing.
    engine = _run_to_dev_escalation(project)
    runs.rearm_escalation(engine.run_dir, "dw-fix")
    _lose_triage(engine.run_dir)

    resumed, adapter = resume_sweep(project, engine, _redrive_script(project))
    summary = resumed.run()

    assert not summary.paused
    assert resumed.state.tasks["dw-fix"].phase == Phase.DONE
    # no triage session: the re-drive never consulted a plan
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    journal = journal_text(resumed)
    assert "sweep-inflight-redrive" in journal
    assert "sweep-nothing-open" in journal  # recovery closed the only open id
    assert ledger_entries(project)["DW-1"].status.startswith("done")


@pytest.mark.parametrize("corruption", ["missing", "invalid-json", "wrong-shape"])
def test_fresh_triage_different_bundle_name_no_double_drive(project, corruption):
    # The fresh triage renames the surviving bundle, so a name-matched recovery
    # would orphan the re-armed one. It must re-drive by identity, and its ids
    # must have left the open set before the fresh triage sees them.
    engine = _run_two_bundle_dev_escalation(project)
    runs.rearm_escalation(engine.run_dir, "dw-fix")
    _lose_triage(engine.run_dir, corruption)

    fresh = triage_result(
        ["DW-2"], bundles=[{"name": "renamed-fix", "dw_ids": ["DW-2"], "intent": "resolve DW-2"}]
    )
    resumed, adapter = resume_sweep(
        project,
        engine,
        [
            *_redrive_script(project),
            triage_effect(fresh),
            bundle_dev_effect(project, "renamed-fix", ["DW-2"]),
            bundle_review_effect(project, "renamed-fix"),
        ],
    )
    summary = resumed.run()

    assert not summary.paused
    assert [s.role for s in adapter.sessions] == ["dev", "review", "triage", "dev", "review"]
    assert resumed.state.tasks["dw-fix"].phase == Phase.DONE
    assert resumed.state.tasks["dw-renamed-fix"].phase == Phase.DONE
    # each id was closed exactly once, by the bundle that owned it
    closed = [e for e in resumed.journal.entries() if e["kind"] == "sweep-bundle-closed"]
    owners = [(i, e["story_key"]) for e in closed for i in e["dw_ids"]]
    assert sorted(owners) == [("DW-1", "dw-fix"), ("DW-2", "dw-renamed-fix")]
    if corruption != "missing":
        # a truncated / wrong-shape cache degrades to a fresh triage, never a crash
        assert "sweep-triage-reload-failed" in journal_text(resumed)


def test_restore_patch_latch_honored_when_triage_json_lost(project, monkeypatch):
    monkeypatch.setattr(verify, "apply_patch", lambda repo, patch: None)
    engine = _run_to_dev_escalation(project)
    patch = project.implementation_artifacts / "attempt-dw-fix.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("dummy\n")
    runs.rearm_escalation(engine.run_dir, "dw-fix", restore_patch=str(patch))
    _lose_triage(engine.run_dir)

    resumed, adapter = resume_sweep(project, engine, _redrive_script(project))
    summary = resumed.run()

    assert not summary.paused
    task = resumed.state.tasks["dw-fix"]
    assert task.phase == Phase.DONE
    # the recovery pass preserved the restore semantics _run_bundle used to own
    assert "Resume review of the in-review spec" in adapter.sessions[0].prompt
    assert "attempt-restored" in journal_text(resumed)
    assert task.restore_patch is None and task.resolved_redrive is False
    assert ledger_entries(project)["DW-1"].status.startswith("done")


def test_escalated_unresolved_still_skipped_when_triage_json_lost(project):
    # An escalation nobody resolved is terminal: recovery must not touch it, and
    # the fresh triage's overlapping bundle is still dropped by the failed-ids filter.
    engine = _run_two_bundle_dev_escalation(project)
    _lose_triage(engine.run_dir)

    fresh = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "retry-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "other", "dw_ids": ["DW-2"], "intent": "b"},
        ],
    )
    resumed, adapter = resume_sweep(
        project,
        engine,
        [
            triage_effect(fresh),
            bundle_dev_effect(project, "other", ["DW-2"]),
            bundle_review_effect(project, "other"),
        ],
    )
    summary = resumed.run()

    assert not summary.paused
    assert "sweep-inflight-redrive" not in journal_text(resumed)
    assert [s.role for s in adapter.sessions] == ["triage", "dev", "review"]
    assert resumed.state.tasks["dw-fix"].phase == Phase.ESCALATED
    assert "dw-retry-fix" not in resumed.state.tasks
    assert "sweep-bundle-skipped" in journal_text(resumed)
    entries = ledger_entries(project)
    assert entries["DW-1"].open  # escalated bundle untouched
    assert entries["DW-2"].status.startswith("done")


def test_interrupted_bundle_redrives_by_identity_after_triage_loss(project):
    # Not a re-arm: the host just died mid-dev. The restart arm rolls the attempt
    # back against its own baseline (cause="stopped") and re-runs the bundle.
    engine = _run_to_dev_escalation(project)
    state = load_state(engine.run_dir)
    task = state.tasks["dw-fix"]
    assert task.baseline_commit
    task.phase = Phase.DEV_RUNNING
    save_state(engine.run_dir, state)
    _lose_triage(engine.run_dir)

    resumed, adapter = resume_sweep(project, engine, _redrive_script(project))
    summary = resumed.run()

    assert not summary.paused
    assert resumed.state.tasks["dw-fix"].phase == Phase.DONE
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    redrive = [e for e in resumed.journal.entries() if e["kind"] == "sweep-inflight-redrive"]
    assert len(redrive) == 1
    assert redrive[0]["phase"] == "dev-running" and redrive[0]["rearmed"] is False
    journal = journal_text(resumed)
    assert "rollback-auto" in journal  # a stopped attempt, rolled back to baseline
    assert "attempt-restored" not in journal  # not a resolved re-drive
    assert ledger_entries(project)["DW-1"].status.startswith("done")


def test_resume_committing_bundle_finishes_commit(project):
    """#115, sweep flavor: a bundle whose host died in the commit window
    (COMMITTING persisted, DONE save never landed) is finished on resume —
    the recovery arm mirrors the base engine's resume-commit, not the
    rollback+restart the recovery used to apply to every non-DEV_VERIFY phase."""
    write_ledger(project, {"DW-1": "open"})
    plan = triage_result(
        ["DW-1"], bundles=[{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}]
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "fix", ["DW-1"]),
            bundle_review_effect(project, "fix"),
        ],
    )

    def crashing_emit(stage, *args, **kwargs):
        if stage == "pre_commit":
            raise RuntimeError("host died in the commit window")
        return original_emit(stage, *args, **kwargs)

    original_emit = engine._emit
    engine._emit = crashing_emit
    assert engine.run().crashed
    crashed = load_state(engine.run_dir).tasks["dw-fix"]
    assert crashed.phase == Phase.COMMITTING
    assert not crashed.commit_sha  # stamped only by the DONE save that never ran

    resumed, adapter = resume_sweep(project, engine, [])
    summary = resumed.run()

    assert not summary.paused and not summary.crashed
    task = resumed.state.tasks["dw-fix"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert adapter.sessions == []  # no triage, dev, review, or gate session re-run
    journal = journal_text(resumed)
    assert "resume-commit" in journal
    assert "resume-restart" not in journal
    assert ledger_entries(project)["DW-1"].status.startswith("done")


def test_regenerated_intent_when_bundle_file_missing(project):
    # The triage session's authored prose is the one unrecoverable piece; the
    # verbatim ledger entries are re-attached and become the contract.
    engine = _run_to_dev_escalation(project)
    runs.rearm_escalation(engine.run_dir, "dw-fix")
    _lose_triage(engine.run_dir)
    intent = Path(engine.state.tasks["dw-fix"].bundle_file)
    intent.unlink()

    resumed, adapter = resume_sweep(project, engine, _redrive_script(project))
    summary = resumed.run()

    assert not summary.paused
    assert resumed.state.tasks["dw-fix"].phase == Phase.DONE
    regen = [e for e in resumed.journal.entries() if e["kind"] == "sweep-intent-regenerated"]
    assert len(regen) == 1 and regen[0]["dw_ids"] == ["DW-1"]
    assert regen[0]["path"] == str(intent)
    text = intent.read_text(encoding="utf-8")
    assert "bundle_name: fix" in text
    assert "### DW-1" in text and "reason: test entry." in text  # verbatim ledger entry
    assert "authoritative" in text
    assert str(intent) in adapter.sessions[0].prompt  # the dev session got the rebuilt file


def test_stranded_bundle_task_warns_loudly(project):
    write_ledger(project, {"DW-1": "open"})
    engine, _ = make_sweep(project, [])
    engine.state.tasks["dw-ghost"] = StoryTask(
        story_key="dw-ghost", epic=0, dw_ids=["DW-1"], phase=Phase.DEV_RUNNING
    )

    engine._warn_stranded_bundles()

    stranded = [e for e in engine.journal.entries() if e["kind"] == "sweep-inflight-stranded"]
    assert len(stranded) == 1 and stranded[0]["story_keys"] == ["dw-ghost"]
    assert "dw-ghost" in (engine.run_dir / "ATTENTION").read_text(encoding="utf-8")

    # a terminal bundle and a non-bundle task are not stranded
    engine.state.tasks["dw-ghost"].phase = Phase.DONE
    engine.state.tasks["sweep-triage"] = StoryTask(
        story_key="sweep-triage", epic=0, phase=Phase.TRIAGE_RUNNING
    )
    engine._warn_stranded_bundles()
    assert len([e for e in engine.journal.entries() if e["kind"] == "sweep-inflight-stranded"]) == 1


# ------------------------------------------------------------- graceful stop
#
# A graceful stop is delivered out-of-band via the stop-request.json control
# file (Phase 1's runs helpers). SweepEngine overrides _loop, so it carries its
# own boundary checks: the first statement of the while body, and before every
# _run_bundle. These tests lodge the control file directly (an existence read is
# all the engine checks) so the request lands at a chosen boundary, then prove
# the in-flight item still runs to completion and the next never starts.


def _lodge_stop_request(run_dir: Path) -> None:
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        '{"requested_at": "2026-07-20T00:00:00", "mode": "graceful"}', encoding="utf-8"
    )


def _lodge_after(inner, run_dir: Path):
    """Wrap a scripted effect so the graceful-stop request lands as ``inner``
    returns — proving the in-flight item still runs to completion, because the
    boundary check only fires at the next loop head / next bundle."""

    def effect(spec):
        result = inner(spec)
        _lodge_stop_request(run_dir)
        return result

    return effect


def test_graceful_stop_between_bundles_finishes_current_then_stops(project):
    """A request lodged while bundle 1 runs lets it finish through commit; the
    boundary check before bundle 2 raises, so bundle 2 never starts and the run
    ends `stopped` + resumable. A re-run completes bundle 2."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    run_dir = project.project / ".bmad-loop" / "runs" / "sweep-run"
    plan = triage_result(
        ["DW-1", "DW-2"],
        bundles=[
            {"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"},
            {"name": "second-fix", "dw_ids": ["DW-2"], "intent": "b"},
        ],
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan),
            bundle_dev_effect(project, "first-fix", ["DW-1"]),
            _lodge_after(bundle_review_effect(project, "first-fix"), run_dir),
        ],
    )
    summary = engine.run()

    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["dw-first-fix"].phase == Phase.DONE and tasks["dw-first-fix"].commit_sha
    assert "dw-second-fix" not in tasks  # bundle 2 never dispatched
    saved = load_state(engine.run_dir)
    assert saved.stopped is True and saved.finished is False
    assert not runs.graceful_stop_requested(engine.run_dir)  # control file consumed
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "run-complete" not in kinds
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    assert stops[-1]["remaining"] == 1  # DW-2 still open, never bundled

    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done") and entries["DW-2"].open

    # resume: bundle 2 runs to completion and the run finishes
    resumed, adapter = resume_sweep(
        project,
        engine,
        [
            bundle_dev_effect(project, "second-fix", ["DW-2"]),
            bundle_review_effect(project, "second-fix"),
        ],
    )
    summary = resumed.run()
    assert not summary.paused
    assert resumed.state.tasks["dw-second-fix"].phase == Phase.DONE
    assert len(adapter.sessions) == 2  # triage reloaded from cache, bundle 1 skipped
    assert ledger_entries(project)["DW-2"].status.startswith("done")
    assert worktree_clean(project.project)


def test_graceful_stop_during_triage_runs_zero_bundles(project):
    """A request landing during the triage session completes triage (artifacts
    written, resolved entries closed) but the very first bundle-loop check raises,
    so zero bundles run."""
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    run_dir = project.project / ".bmad-loop" / "runs" / "sweep-run"
    plan = triage_result(
        ["DW-1", "DW-2"],
        already_resolved=[{"id": "DW-1", "evidence": "already guarded at src.txt:1"}],
        bundles=[{"name": "fix-it", "dw_ids": ["DW-2"], "intent": "fix"}],
    )
    engine, _ = make_sweep(project, [_lodge_after(triage_effect(plan), run_dir)])
    summary = engine.run()

    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["sweep-triage"].phase == Phase.DONE
    assert (engine.run_dir / "triage.json").is_file()  # triage completed
    assert "dw-fix-it" not in tasks  # zero bundles started
    saved = load_state(engine.run_dir)
    assert saved.stopped is True and saved.finished is False
    assert not runs.graceful_stop_requested(engine.run_dir)
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done")  # resolved-close still ran
    assert entries["DW-2"].open
    assert stops[-1]["remaining"] == 1  # only DW-2 left open


def test_graceful_stop_between_cycles_skips_next_triage(project):
    """In repeat mode a request lodged during cycle 1 lets that cycle finish, then
    the while-head check fires before cycle 2 re-triages — cycle 2 never runs."""
    write_ledger(project, {"DW-1": "open"})
    run_dir = project.project / ".bmad-loop" / "runs" / "sweep-run"
    plan1 = triage_result(
        ["DW-1"], bundles=[{"name": "first-fix", "dw_ids": ["DW-1"], "intent": "a"}]
    )
    engine, _ = make_sweep(
        project,
        [
            triage_effect(plan1),
            appending_dev(project, bundle_dev_effect(project, "first-fix", ["DW-1"]), "DW-2"),
            _lodge_after(bundle_review_effect(project, "first-fix"), run_dir),
        ],
        policy=repeat_policy(),
    )
    summary = engine.run()

    assert not summary.paused
    tasks = engine.state.tasks
    assert tasks["dw-first-fix"].phase == Phase.DONE
    assert "sweep-triage-2" not in tasks  # cycle 2 never triaged
    saved = load_state(engine.run_dir)
    assert saved.stopped is True and saved.finished is False
    assert not runs.graceful_stop_requested(engine.run_dir)
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "sweep-cycle" not in kinds  # the cycle-2 header never printed
    stops = [e for e in engine.journal.entries() if e["kind"] == "run-stop"]
    assert stops and stops[-1]["graceful"] is True
    entries = ledger_entries(project)
    assert entries["DW-1"].status.startswith("done") and entries["DW-2"].open
    assert stops[-1]["remaining"] == 1  # DW-2, generated in cycle 1, still open


def test_generic_bundle_prompt_spells_the_post_rename_primitive(project):
    """#405: all three bundle legs (restore, fresh implement, repair) invoke the
    primitive resolved from the dev adapter's skill tree, not a hardcoded name."""
    from conftest import attach_profile, install_build_auto_skill

    install_build_auto_skill(project.project, ".claude/skills")
    engine, adapter = make_sweep(project, [])
    attach_profile(adapter)
    spec = str(project.implementation_artifacts / "spec-dw-fix.md")
    task = StoryTask(
        story_key="dw-fix",
        epic=0,
        dw_ids=["DW-1"],
        bundle_file="/run/bundles/fix/intent.md",
        spec_file=spec,
        restore_patch="/run/artifacts/attempt-dw-fix.patch",
    )

    assert engine._generic_bundle_prompt(task, None).startswith(
        "/bmad-build-auto Resume review of the in-review spec"
    )
    task.restore_patch = None
    assert engine._generic_bundle_prompt(task, None).startswith(
        "/bmad-build-auto Implement the deferred-work bundle"
    )
    feedback = project.implementation_artifacts / "feedback.md"
    assert engine._generic_bundle_prompt(task, feedback).startswith(
        "/bmad-build-auto Resume the autonomous dev session"
    )


def test_generic_bundle_harvests_spec_deferrals_alongside_its_closures(project):
    """A bundle session can defer NEW findings while resolving the ones it owns
    (BMAD-METHOD #2640 puts both in the bundle spec's frontmatter). Both ledger
    writes must land: the bundle's own dw ids close, the new finding opens as a
    fresh entry — and `verify_review_bundle` stays green, because it gates on the
    bundle's own ids, not on the ledger having no open entries at all."""
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
            bundle_dev_effect(
                project,
                "fix-things",
                ["DW-1", "DW-2"],
                mark_ledger=False,
                deferred=[
                    {
                        "summary": "The retry cap should be configurable",
                        "evidence": "hardcoded while fixing DW-1",
                        "location": "src/retry.py:88",
                        "severity": "low",
                    }
                ],
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
    assert entries["DW-3"].open
    assert entries["DW-3"].title == "The retry cap should be configurable"
    assert "origin: spec-deferred " in entries["DW-3"].body
    assert "source_spec: `spec-dw-fix-things.md`" in entries["DW-3"].body
    assert "location: src/retry.py:88" in entries["DW-3"].body
    # the task's bundle binding is untouched — verify_dev_bundle/verify_review_bundle
    # compare dw_ids for equality, so a harvested id leaking in would fail both gates
    assert engine.state.tasks["dw-fix-things"].dw_ids == ["DW-1", "DW-2"]
    harvested = [e for e in engine.journal.entries() if e["kind"] == "spec-deferrals-harvested"]
    assert len(harvested) == 1 and harvested[0]["dw_ids"] == ["DW-3"]
    assert worktree_clean(project.project)
