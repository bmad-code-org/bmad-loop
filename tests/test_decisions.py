"""Pre-answer store, discovery of missed decisions, and out-of-band apply."""

import json

from conftest import install_bmad_config, write_ledger

from bmad_loop import decisions, deferredwork
from bmad_loop.sweep import DecisionOption


def _decision(dw_id, *, question="q", options=None, recommendation="1"):
    options = options or [
        {"key": "1", "label": "Build it", "effect": "build", "intent": "do it"},
        {"key": "2", "label": "Keep as is", "effect": "keep-open"},
    ]
    return {
        "id": dw_id,
        "question": question,
        "context": "ctx",
        "options": options,
        "recommendation": recommendation,
    }


def _triage(open_ids, decisions_):
    return {
        "workflow": "deferred-sweep-triage",
        "open_ids": list(open_ids),
        "already_resolved": [],
        "bundles": [],
        "blocked": [],
        "skip": [],
        "decisions": decisions_,
        "escalations": [],
    }


def _make_run(project, run_id, triage_rj, cycle=1):
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text("{}", encoding="utf-8")  # so list_run_dirs sees it
    name = "triage.json" if cycle == 1 else f"triage-{cycle}.json"
    (run_dir / name).write_text(json.dumps(triage_rj), encoding="utf-8")
    return run_dir


# ------------------------------------------------------------- store I/O


def test_store_round_trip_and_prune(project):
    opt = DecisionOption(key="1", label="Build it", effect="build", intent="do it")
    decisions.record_pre_answer(project.project, "DW-7", opt, date="2026-06-13")
    loaded = decisions.load_pre_answers(project.project)
    assert loaded["DW-7"]["effect"] == "build"
    assert loaded["DW-7"]["intent"] == "do it"
    assert loaded["DW-7"]["answered_at"] == "2026-06-13"

    # only entries whose id is still open survive a prune
    dropped = decisions.prune_pre_answers(project.project, {"DW-9"})
    assert dropped == ["DW-7"]
    assert decisions.load_pre_answers(project.project) == {}


def test_load_pre_answers_tolerates_garbage(project):
    decisions.store_path(project.project).parent.mkdir(parents=True, exist_ok=True)
    decisions.store_path(project.project).write_text("not json", encoding="utf-8")
    assert decisions.load_pre_answers(project.project) == {}


# ------------------------------------------------------- discovery


def test_pending_missed_decisions_most_recent_wins_and_filters(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open", "DW-3": "done 2026-06-01"})
    # older run: DW-1 with stale wording; newer run: DW-1 (fresh wording) + DW-2;
    # DW-3 surfaces too but is closed in the ledger
    _make_run(
        project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1", question="old")])
    )
    _make_run(
        project,
        "20260102-000000-bbbb",
        _triage(
            ["DW-1", "DW-2", "DW-3"],
            [
                _decision("DW-1", question="new"),
                _decision("DW-2"),
                _decision("DW-3"),
            ],
        ),
    )
    # DW-2 already pre-answered out of band -> excluded
    decisions.record_pre_answer(
        project.project,
        "DW-2",
        DecisionOption(key="2", label="x", effect="keep-open"),
        date="2026-06-13",
    )

    pending = decisions.pending_missed_decisions(project.project)
    ids = [d.id for d in pending]
    assert ids == ["DW-1"]  # DW-2 answered, DW-3 closed
    assert pending[0].question == "new"  # newest run's wording


def test_pending_missed_decisions_empty_when_nothing_open(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "done 2026-06-01"})
    _make_run(project, "20260101-000000-aaaa", _triage([], []))
    assert decisions.pending_missed_decisions(project.project) == []


# ------------------------------------------------------- apply


def test_apply_pre_answer_build_records_store_and_ledger(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Build", effect="build", intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert "decision: 2026-06-13 Build — widen field" in entries["DW-1"].body
    assert entries["DW-1"].open  # build stays open until a sweep builds it
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)


def test_apply_pre_answer_close_marks_done_no_store(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    d = Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1")
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert entries["DW-1"].status.startswith("done")
    assert "closed by human decision: superseded" in entries["DW-1"].body
    assert decisions.load_pre_answers(project.project) == {}  # close needs no carry-forward


def test_apply_pre_answer_commit_leaves_unrelated_changes(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    (project.project / "src.txt").write_text("user edit, uncommitted\n")  # unrelated work
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="x")
    d = Decision(id="DW-1", question="?", context="", options=(opt,), recommendation="1")
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")
    # the unrelated change is still uncommitted (commit_paths staged only the ledger)
    assert "src.txt" in _git_status(project)


def _git_log(project):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project.project), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_status(project):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project.project), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
