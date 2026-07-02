"""Disk-reclamation tests: run classification, worktree reconcile, retention,
artifact trim, and the `clean` CLI command."""

import argparse
import subprocess

from conftest import install_bmad_config

from automator import cli, runs, verify
from automator.journal import save_state
from automator.model import RunState
from automator.tui import data


def _state_run(project, run_id, **kw):
    run_dir = project / ".automator" / "runs" / run_id
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project), started_at="2026-06-11T10:00:00", **kw),
    )
    return run_dir


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# --------------------------------------------------------------- predicates


def test_is_finished_only_for_finished(tmp_path):
    fin = _state_run(tmp_path, "20260101-000000-aaaa", finished=True)
    stp = _state_run(tmp_path, "20260101-000001-bbbb", stopped=True)
    psd = _state_run(tmp_path, "20260101-000002-cccc", paused_reason="gate")
    plain = _state_run(tmp_path, "20260101-000003-dddd")
    assert runs.is_finished(fin)
    assert not runs.is_finished(stp)
    assert not runs.is_finished(psd)
    assert not runs.is_finished(plain)  # interrupted/unknown — not finished


def test_reclaimable_finished_or_stopped(tmp_path):
    fin = _state_run(tmp_path, "20260101-000000-aaaa", finished=True)
    stp = _state_run(tmp_path, "20260101-000001-bbbb", stopped=True)
    psd = _state_run(tmp_path, "20260101-000002-cccc", paused_reason="gate")
    plain = _state_run(tmp_path, "20260101-000003-dddd")
    assert runs.reclaimable(fin)
    assert runs.reclaimable(stp)  # resumable but explicit-clean eligible
    assert not runs.reclaimable(psd)
    assert not runs.reclaimable(plain)


def test_reclaimable_excludes_live(tmp_path):
    run_dir = _state_run(tmp_path, "20260101-000000-aaaa", stopped=True)
    runs.write_pid(run_dir)  # our own (alive) pid
    assert not runs.reclaimable(run_dir)
    live_finished = _state_run(tmp_path, "20260101-000001-bbbb", finished=True)
    runs.write_pid(live_finished)
    assert not runs.is_finished(live_finished)  # live engine ⇒ not finished-reclaimable


def test_reclaimable_unreadable_state(tmp_path):
    run_dir = tmp_path / ".automator" / "runs" / "20260101-000000-aaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{ not json")
    assert not runs.reclaimable(run_dir)


# ------------------------------------------------------------- reconcile


def test_reconcile_orphan_worktrees(project):
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")
    outside = repo / "elsewhere"
    verify.worktree_add(repo, outside, "other", "main")

    handled = runs.reconcile_orphan_worktrees(repo, run_dir)

    assert [p.name for p in handled] == ["unit"]
    assert not wt.exists()
    assert outside.exists()  # a worktree outside the run dir is never touched
    assert repo not in [p for p in verify.worktree_list(repo)[1:]]  # main checkout intact


def test_reconcile_orphan_worktrees_dry_run(project):
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    handled = runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=True)

    assert [p.name for p in handled] == ["unit"]
    assert wt.exists()  # dry run removes nothing


def test_reconcile_stale_worktrees_finished_only(project):
    repo = project.project
    fin = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    stp = repo / ".automator" / "runs" / "20260101-000001-bbbb"
    fin_wt = fin / "worktrees" / "u"
    stp_wt = stp / "worktrees" / "u"
    fin_wt.parent.mkdir(parents=True)
    stp_wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, fin_wt, "fb", "main")
    verify.worktree_add(repo, stp_wt, "sb", "main")
    save_state(fin, RunState(run_id="f", project=str(repo), started_at="x", finished=True))
    save_state(stp, RunState(run_id="s", project=str(repo), started_at="x", stopped=True))

    handled = runs.reconcile_stale_worktrees(repo, repo)

    assert not fin_wt.exists()  # finished run's worktree reclaimed
    assert stp_wt.exists()  # stopped run is resumable — left intact
    assert {p.name for p in handled} == {"u"} and len(handled) == 1


# ------------------------------------------------------------- retention


def test_runs_past_retention_by_count():
    dirs = [project_dir(f"2026010{i}-000000-aa") for i in range(1, 8)]
    past = runs.runs_past_retention(dirs, keep_n=3)
    assert [p.name for p in past] == [d.name for d in dirs[:4]]


def test_runs_past_retention_keep_all_within_count():
    dirs = [project_dir(f"2026010{i}-000000-aa") for i in range(1, 4)]
    assert runs.runs_past_retention(dirs, keep_n=10) == []


def test_runs_past_retention_zero_keeps_none():
    dirs = [project_dir(f"2026010{i}-000000-aa") for i in range(1, 4)]
    assert len(runs.runs_past_retention(dirs, keep_n=0)) == 3


def test_runs_past_retention_days_boundary():
    # five daily runs; "now" = 2026-01-10, keep 1 by count but also keep <7 days
    dirs = [project_dir(f"2026010{i}-120000-aa") for i in range(1, 6)]
    now = runs._run_started_epoch(project_dir("20260110-120000-aa"))
    past = runs.runs_past_retention(dirs, keep_n=1, keep_days=7, now=now)
    # beyond keep_n = days 1..4; of those, older than 7d before the 10th = days 1,2
    assert [p.name for p in past] == ["20260101-120000-aa", "20260102-120000-aa"]


def project_dir(name):
    from pathlib import Path

    return Path("/runs") / name


# ----------------------------------------------------------------- trim


def test_trim_run_dir_keeps_run_viewable(tmp_path):
    run_dir = _state_run(tmp_path, "20260101-000000-aaaa", finished=True)
    (run_dir / "journal.jsonl").write_text('{"kind":"run-start"}\n')
    (run_dir / "logs").mkdir()
    (run_dir / "worktrees" / "u" / "Library").mkdir(parents=True)
    (run_dir / "worktrees" / "u" / "Library" / "big").write_bytes(b"x" * 1000)

    removed = runs.trim_run_dir(run_dir)

    assert [p.name for p in removed] == ["worktrees"]
    assert not (run_dir / "worktrees").exists()
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "journal.jsonl").is_file()
    # the run still discovers + lists in the dashboard
    infos = data.discover_runs(tmp_path)
    assert [i.run_id for i in infos] == ["20260101-000000-aaaa"]


# ------------------------------------------------------------- cmd_clean


def _clean_args(project, **kw):
    base = dict(project=str(project), dry_run=False, keep=None, retain=None, hard=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_clean_dry_run_removes_nothing(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    rc = cli.cmd_clean(_clean_args(repo, dry_run=True))

    assert rc == 0
    assert wt.exists()  # nothing removed
    assert run_dir.is_dir()
    out = capsys.readouterr().out
    assert "would remove worktree" in out


def test_cmd_clean_warns_unknown_liveness(project, monkeypatch, capsys):
    # warn-only: 'unknown' stays reclaimable (classification unchanged); the
    # frontend re-probes just to say so before removal.
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")

    assert cli.cmd_clean(_clean_args(repo)) == 0
    err = capsys.readouterr().err
    assert "run 20260101-000000-aaaa: engine may still be live (unverifiable pid)" in err


def test_cmd_clean_reclaims_and_keeps_protected(project, capsys):
    install_bmad_config(project)
    repo = project.project
    # one stopped run with a worktree (reclaim), one finished run protected by --keep
    r1 = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    wt = r1 / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(r1, RunState(run_id="r1", project=str(repo), started_at="x", stopped=True))
    r2 = repo / ".automator" / "runs" / "20260101-000001-bbbb"
    save_state(r2, RunState(run_id="r2", project=str(repo), started_at="x", finished=True))

    rc = cli.cmd_clean(_clean_args(repo, keep=["20260101-000001-bbbb"]))

    assert rc == 0
    assert not wt.exists()  # stopped run's worktree torn down
    assert r1.is_dir()  # within retention: trimmed but kept viewable
    assert not (r1 / "worktrees").exists()
    assert r2.is_dir()  # protected run untouched
    assert "left 1 live/resumable run(s) untouched" in capsys.readouterr().out


def test_cmd_clean_archives_past_retention(project):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))

    rc = cli.cmd_clean(_clean_args(repo, retain=0))  # nothing kept by count -> archive

    assert rc == 0
    assert not run_dir.exists()
    assert (repo / ".automator" / "archive" / "20260101-000000-aaaa.tar.gz").is_file()


def test_cmd_clean_hard_deletes_past_retention(project):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".automator" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))

    rc = cli.cmd_clean(_clean_args(repo, retain=0, hard=True))

    assert rc == 0
    assert not run_dir.exists()
    assert not (repo / ".automator" / "archive").exists()
