"""Disk-reclamation tests: run classification, worktree reconcile, retention,
artifact trim, and the `clean` CLI command."""

import argparse
import json
import os
from pathlib import Path

import pytest
from conftest import install_bmad_config, machine_json

from bmad_loop import cli, platform_util, runs, verify
from bmad_loop.journal import STATE_FILE, VERIFY_DIR, save_state
from bmad_loop.model import RunState


@pytest.fixture(autouse=True)
def _no_ambient_sessions(monkeypatch):
    """`clean` reads the live-session listing, and the real one answers from
    whatever server holds the default socket — here, another xdist worker's. With
    none running, tmux reports the socket file absent, which the backend warns
    about by design (DW-458), and every `--json` test's empty-stderr assertion
    fails on scheduling alone. Pin an empty, fault-free listing; tests about a
    live session install their own."""
    from test_runs import _LivenessMux

    monkeypatch.setattr(runs, "get_multiplexer", lambda: _LivenessMux([]))


def _state_run(project, run_id, **kw):
    run_dir = project / ".bmad-loop" / "runs" / run_id
    # Recorded identity, as `test_runs._make_state_run` does: pinned writers stay silent.
    run_dir.mkdir(parents=True, exist_ok=True)
    kw.setdefault("run_dir_identity", platform_util.root_identity_record(run_dir))
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project), started_at="2026-06-11T10:00:00", **kw),
    )
    return run_dir


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
    run_dir = tmp_path / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{ not json")
    assert not runs.reclaimable(run_dir)


# ------------------------------------------------------------- reconcile


def test_reconcile_orphan_worktrees(project):
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")
    outside = repo / "elsewhere"
    verify.worktree_add(repo, outside, "other", "main")

    handled, fault = runs.reconcile_orphan_worktrees(repo, run_dir)

    assert [p.name for p in handled] == ["unit"]
    assert fault is None
    assert not wt.exists()
    assert outside.exists()  # a worktree outside the run dir is never touched
    assert repo not in [p for p in verify.worktree_list(repo)[1:]]  # main checkout intact


def test_reconcile_orphan_worktrees_dry_run(project):
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    handled, fault = runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=True)

    assert [p.name for p in handled] == ["unit"]
    assert fault is None
    assert wt.exists()  # dry run removes nothing


def test_reconcile_orphan_worktrees_spawn_fault_degrades_to_noop(project, monkeypatch):
    """#343 acceptance, observation-degrades class: a spawn-level OSError out of
    `git worktree list` arrives typed as GitSpawnError and reads as "no orphans"
    — reclaim is housekeeping, so a broken environment degrades it to a no-op
    rather than crashing the caller. Injected at `subprocess.run` itself to
    prove the whole chain: the chokepoint translation is what lets the
    `except GitError` guard hold.

    Ablation target: delete the `except OSError` arm in `verify._run_git` and
    this fails with the raw OSError."""
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    def cannot_spawn(cmd, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(verify.subprocess, "run", cannot_spawn)
    handled, fault = runs.reconcile_orphan_worktrees(repo, run_dir)
    assert handled == []
    assert wt.exists()  # nothing was reclaimed, nothing was rmtree'd
    # DW-470: visibly — the fault is not "nothing orphaned".
    assert fault is not None and "cannot list git worktrees" in fault


def test_reconcile_orphan_worktrees_names_a_worktree_the_fallback_left(project, monkeypatch):
    """DW-470: `git worktree remove` failed and the `rmtree` fallback left the tree
    on disk. It is named in the fault and kept out of `handled`, which `clean`
    prints as "removed worktree" — a claim that would be false.

    Ablation: drop the `lexists` check and `handled` names the worktree again with
    no fault."""
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    def refuse(*_args, **_kwargs):
        raise verify.GitError("locked")

    monkeypatch.setattr(verify, "worktree_remove", refuse)
    monkeypatch.setattr(runs.shutil, "rmtree", lambda *a, **k: None)

    handled, fault = runs.reconcile_orphan_worktrees(repo, run_dir)

    assert handled == []
    assert wt.exists()
    assert fault is not None and str(wt) in fault and "still on disk" in fault


def test_reconcile_stale_worktrees_reports_a_listing_fault_once(project, monkeypatch):
    """DW-470: a `git worktree list` fault is repo-wide, so every finished run
    reports the same one; the run-start warning carries it once, not once per run.
    Ablate the fault return in `reconcile_orphan_worktrees` and `fault` is None."""
    repo = project.project
    for name in ("20260101-000000-aaaa", "20260101-000001-bbbb"):
        save_state(
            repo / ".bmad-loop" / "runs" / name,
            RunState(run_id=name, project=str(repo), started_at="x", finished=True),
        )

    def cannot_list(_repo):
        raise verify.GitError("git worktree list failed in repo: boom")

    monkeypatch.setattr(verify, "worktree_list", cannot_list)

    handled, fault = runs.reconcile_stale_worktrees(repo, repo)

    assert handled == []
    assert fault is not None and fault.count("cannot list git worktrees") == 1


def test_reconcile_stale_worktrees_finished_only(project):
    repo = project.project
    fin = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    stp = repo / ".bmad-loop" / "runs" / "20260101-000001-bbbb"
    fin_wt = fin / "worktrees" / "u"
    stp_wt = stp / "worktrees" / "u"
    fin_wt.parent.mkdir(parents=True)
    stp_wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, fin_wt, "fb", "main")
    verify.worktree_add(repo, stp_wt, "sb", "main")
    save_state(fin, RunState(run_id="f", project=str(repo), started_at="x", finished=True))
    save_state(stp, RunState(run_id="s", project=str(repo), started_at="x", stopped=True))

    handled, fault = runs.reconcile_stale_worktrees(repo, repo)

    assert not fin_wt.exists()  # finished run's worktree reclaimed
    assert stp_wt.exists()  # stopped run is resumable — left intact
    assert {p.name for p in handled} == {"u"} and len(handled) == 1
    assert fault is None


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
    infos, _ = runs.discover_runs(tmp_path)
    assert [i.run_id for i in infos] == ["20260101-000000-aaaa"]


def test_trim_run_dir_reclaims_the_verifier_stream_store(tmp_path):
    """The retained verifier stdout/stderr store is trimmed with the worktrees,
    and trimming it does not cost the run its place in the dashboard.

    It qualifies as heavy on the same measure a worktree checkout does:
    `[verify] stream_capture_kb` defaults to 256 KiB per stream, so a run
    accumulates up to 512 KiB per verify command per attempt. Nothing else ever
    reclaimed it — the store outlived every trim and survived for as long as the
    run dir did. What it costs is re-reading the streams the journal's
    `stdout_path`/`stderr_path` still name, which is the bargain `worktrees`
    already makes: a trimmed run is one you can still see and resume, not one you
    can still open every artifact of.

    Ablation: drop VERIFY_DIR from `_HEAVY_RUN_ENTRIES` and `removed` comes back
    `["worktrees"]` with the store still on disk. Verified.
    """
    run_dir = _state_run(tmp_path, "20260101-000000-aaaa", finished=True)
    (run_dir / "journal.jsonl").write_text('{"kind":"run-start"}\n')
    (run_dir / "logs").mkdir()
    (run_dir / "worktrees" / "u").mkdir(parents=True)
    store = run_dir / VERIFY_DIR
    store.mkdir()
    (store / "verify-1-1-a-dev-1-1-0.stdout.log").write_bytes(b"o" * 2048)
    (store / "verify-1-1-a-dev-1-1-0.stderr.log").write_bytes(b"e" * 1024)

    removed = runs.trim_run_dir(run_dir)

    assert [p.name for p in removed] == ["worktrees", VERIFY_DIR]
    assert not store.exists()
    # the TUI-visible core the trim exists to preserve
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "journal.jsonl").is_file()
    infos, _ = runs.discover_runs(tmp_path)
    assert [i.run_id for i in infos] == ["20260101-000000-aaaa"]


@pytest.mark.skipif(
    os.name != "posix", reason="planting a directory symlink needs privilege on win32"
)
def test_trim_run_dir_removes_a_planted_redirect_without_following_it(tmp_path):
    """A trimmed entry that is a LINK is removed as a link, and its target is not.

    A session is handed the writable run dir (`BMAD_LOOP_RUN_DIR`) and can plant a
    redirect at `verify/` — the same escape the write path was hardened against.
    The reclaim path had the mirror-image hole: `shutil.rmtree` REFUSES a directory
    symlink by design (following it would delete the target's contents), and under
    `ignore_errors=True` that refusal is silent, so the trim appended the entry to
    `removed` and left the link exactly where it was.

    Both halves are graded, because the obvious over-correction is worse than the
    bug: the redirect goes, and what it pointed at stays. POSIX-only because
    PLANTING the link needs privilege on win32, not because the fix is — the
    junction arm rides on `is_link_like`, graded in tests/test_platform_util.py.

    Ablation: restore the bare `shutil.rmtree(p, ignore_errors=True)` and the link
    is still on disk after the trim, with `removed` still naming it. Verified.
    """
    run_dir = _state_run(tmp_path, "20260101-000000-aaaa", finished=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"x" * 5000)
    link = run_dir / VERIFY_DIR
    link.symlink_to(outside, target_is_directory=True)

    removed = runs.trim_run_dir(run_dir)

    assert [p.name for p in removed] == [VERIFY_DIR]
    assert not link.is_symlink() and not link.exists()  # the redirect really went
    assert outside.is_dir() and (outside / "keep.txt").is_file()  # the target did not


# ------------------------------------------------------------- cmd_clean


def _clean_args(project, **kw):
    base = dict(project=str(project), dry_run=False, keep=None, retain=None, hard=False, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_clean_dry_run_removes_nothing(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
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
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")

    assert cli.cmd_clean(_clean_args(repo)) == 0
    err = capsys.readouterr().err
    assert "run 20260101-000000-aaaa: engine may still be live (unverifiable pid)" in err


def test_cmd_clean_warns_for_an_unreadable_pid_file(project, capsys):
    """DW-465, end to end through the real liveness read: an engine.pid that
    exists but cannot be read (a directory at the name) used to read as a dead
    engine, so `clean` reclaimed the run without its unverifiable-pid warning.

    ABLATION: fold `read_named_pid_identity`'s read fault back into `(None, None)`
    and the warning is gone."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    (run_dir / runs.PID_FILE).mkdir()

    assert cli.cmd_clean(_clean_args(repo)) == 0
    err = capsys.readouterr().err
    assert "run 20260101-000000-aaaa: engine may still be live (unverifiable pid)" in err


def _deny_stat_under(monkeypatch, root: Path) -> None:
    """Every `stat()` of `root` or below raises EACCES — an unreadable dir, as
    3.14's `is_dir`/`is_file` would fold it into False."""
    real = Path.stat

    def stat(self, *args, **kwargs):
        if self == root or root in self.parents:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)


def test_cmd_clean_names_an_unreadable_runs_dir(project, monkeypatch, capsys):
    """DW-468: "nothing to reclaim" over an unreadable runs dir is not the answer.
    Text warns on stderr; --json carries `listing_fault` with stderr empty.
    Ablate the listing fault in `list_run_dirs` and both fail."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    _deny_stat_under(monkeypatch, run_dir.parent)

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0
    err = capsys.readouterr().err
    assert "run listing incomplete" in err and "PermissionError" in err

    assert cli.cmd_clean(_clean_args(repo, dry_run=True, json=True)) == 0
    captured = capsys.readouterr()
    assert "PermissionError" in json.loads(captured.out)["listing_fault"]
    assert captured.err == ""


def test_cmd_clean_readable_runs_dir_is_silent(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0
    assert "run listing incomplete" not in capsys.readouterr().err
    assert cli.cmd_clean(_clean_args(repo, dry_run=True, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["listing_fault"] is None


def _stopped_run(repo: Path) -> Path:
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    return run_dir


def test_cmd_clean_names_a_worktree_reconcile_git_could_not_list(project, monkeypatch, capsys):
    """DW-470: a run whose worktrees git would not list is not "no orphaned
    worktrees". Text warns on stderr and `clean` still finishes; --json carries
    `worktree_faults` with stderr empty. Ablate the listing fault in
    `reconcile_orphan_worktrees` and both fail."""
    install_bmad_config(project)
    repo = project.project
    run_dir = _stopped_run(repo)

    def cannot_list(_repo):
        raise verify.GitError("git worktree list failed in repo: boom")

    monkeypatch.setattr(verify, "worktree_list", cannot_list)

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0
    err = capsys.readouterr().err
    assert f"warning: run {run_dir.name}: worktree reconcile incomplete" in err
    assert "boom" in err

    assert cli.cmd_clean(_clean_args(repo, dry_run=True, json=True)) == 0
    captured = capsys.readouterr()
    faults = json.loads(captured.out)["worktree_faults"]
    assert len(faults) == 1 and faults[0].startswith(f"{run_dir.name}: cannot list git worktrees")
    assert captured.err == ""


def test_cmd_clean_names_a_state_dir_sweep_that_could_not_run(project, monkeypatch, capsys):
    """DW-470: a state-dir sweep that could not name its root is not "swept 0".
    Ablate the fault return in `reconcile_orphan_state_dirs` and both fail."""
    install_bmad_config(project)
    repo = project.project
    _stopped_run(repo)

    def no_root(_project):
        raise runs.StateRootError("no usable base")

    monkeypatch.setattr(runs, "project_state_root", no_root)

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0
    err = capsys.readouterr().err
    assert "warning: orphaned state-dir sweep incomplete" in err and "StateRootError" in err

    assert cli.cmd_clean(_clean_args(repo, dry_run=True, json=True)) == 0
    captured = capsys.readouterr()
    assert "StateRootError" in json.loads(captured.out)["state_dir_fault"]
    assert captured.err == ""


def test_cmd_clean_healthy_reconcile_reports_no_fault(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = _stopped_run(repo)
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0
    captured = capsys.readouterr()
    assert "incomplete" not in captured.err
    assert f"would remove worktree {wt}" in captured.out

    assert cli.cmd_clean(_clean_args(repo, dry_run=True, json=True)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["worktree_faults"] == [] and doc["state_dir_fault"] is None
    assert doc["worktrees"] == [str(wt)]


def test_cmd_clean_reclaims_and_keeps_protected(project, capsys):
    install_bmad_config(project)
    repo = project.project
    # one stopped run with a worktree (reclaim), one finished run protected by --keep
    r1 = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = r1 / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(r1, RunState(run_id="r1", project=str(repo), started_at="x", stopped=True))
    r2 = repo / ".bmad-loop" / "runs" / "20260101-000001-bbbb"
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
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))

    rc = cli.cmd_clean(_clean_args(repo, retain=0))  # nothing kept by count -> archive

    assert rc == 0
    assert not run_dir.exists()
    assert (repo / ".bmad-loop" / "archive" / "20260101-000000-aaaa.tar.gz").is_file()


def test_cmd_clean_hard_deletes_past_retention(project):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))

    rc = cli.cmd_clean(_clean_args(repo, retain=0, hard=True))

    assert rc == 0
    assert not run_dir.exists()
    assert not (repo / ".bmad-loop" / "archive").exists()


def test_cmd_clean_protects_a_run_whose_agent_session_is_still_live(project, monkeypatch, capsys):
    """`reclaimable` is keyed on engine pid liveness, so an orphan — engine dead,
    agent session still live — is past retention and would be reclaimed. That takes
    the only ownership proof an untagged session has, leaking it for the life of the
    machine (#419), so the run is protected instead and the operator is told.

    The run is given a real worktree and put past retention so every mutation in the
    loop is in play at once: the guard has to sit ahead of `reconcile_orphan_worktrees`
    and `trim_run_dir`, not just ahead of the archive. Half-reclaiming a protected run
    would strip the tree the live session may still be working in and still report it
    untouched."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))
    from test_runs import _LivenessMux

    monkeypatch.setattr(
        runs, "get_multiplexer", lambda: _LivenessMux(["bmad-loop-20260101-000000-aaaa"])
    )

    assert cli.cmd_clean(_clean_args(repo, retain=0)) == 0

    assert run_dir.is_dir()  # not archived out from under the session
    assert not (repo / ".bmad-loop" / "archive").exists()
    assert wt.exists()  # worktree not reconciled away either
    out, err = capsys.readouterr()
    assert "20260101-000000-aaaa: agent session still live — left untouched" in err
    assert "removed worktree" not in out


def test_cmd_clean_survives_a_session_appearing_mid_clean(project, monkeypatch, capsys):
    """The loop-top guard is one sample, so a resume racing this clean can start a
    session after it and before the removal. The chokepoint refuses there, and that
    refusal must not abort the whole invocation — one racing run is recorded and the
    rest of the reclaim continues.

    The race is simulated by making the ownership read flip between the two calls;
    a stateless fake would answer the same both times and test nothing. The wider
    race — every mutation in the loop against a concurrent resume — predates this
    guard (`reclaimable` is sampled once and never re-read) and is out of scope."""
    install_bmad_config(project)
    repo = project.project
    racer = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    other = repo / ".bmad-loop" / "runs" / "20260101-000001-bbbb"
    for d in (racer, other):
        save_state(d, RunState(run_id=d.name, project=str(repo), started_at="x", finished=True))
    seen: list[str] = []

    def racing(_project, run_id, **_kw):
        if run_id != racer.name:
            return False  # only one run races; the other must still be reclaimed
        seen.append(run_id)
        # dead at the loop-top guard, live by the time the removal asks again
        return len(seen) > 1

    monkeypatch.setattr(runs, "live_session_may_be_ours", racing)

    assert cli.cmd_clean(_clean_args(repo, retain=0)) == 0  # not an aborted clean

    out, err = capsys.readouterr()
    assert racer.is_dir()  # refused at the chokepoint, not removed
    assert not other.is_dir()  # the racing run did not stop the rest
    assert "20260101-000000-aaaa: agent session appeared mid-clean — not removed" in err
    # nothing reached this run before the refusal, so "untouched" is the honest
    # classification here — the sibling test pins the other side, where it is not
    assert "left 1 live/resumable run(s) untouched" in out


def test_cmd_clean_reports_a_mid_clean_racer_by_what_it_actually_did(project, monkeypatch, capsys):
    """A run the race caught *after* its worktree was reconciled and its artifacts
    trimmed is not "left untouched", so it must not land in `protected` — that field
    is documented as exactly that, and a consumer would read a partially reclaimed
    run as a preserved one. It ends in the trimmed state, so that is what is
    reported. The sibling test above covers the other side: a run nothing reached
    before the refusal really is protected."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))
    seen: list[str] = []

    def racing(_project, run_id, **_kw):
        seen.append(run_id)
        return len(seen) > 1

    monkeypatch.setattr(runs, "live_session_may_be_ours", racing)

    doc = _clean_json(repo, capsys, "--retain", "0")

    assert run_dir.is_dir() and not (run_dir / "worktrees").exists()  # the racy state
    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert doc["protected"] == []
    assert doc["archived"] == [] and doc["deleted"] == []


@pytest.mark.parametrize(
    "hard, helper_name, result_key, other_key",
    [
        (False, "archive_run", "archived", "deleted"),
        (True, "delete_run", "deleted", "archived"),
    ],
)
def test_cmd_clean_json_records_a_resumed_engine_and_continues_siblings(
    project, monkeypatch, capsys, hard, helper_name, result_key, other_key
):
    """A runs-layer lifecycle refusal is per-run data, never a partial JSON
    document or an abort that prevents later candidates from being reclaimed."""
    install_bmad_config(project)
    repo = project.project
    racer = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    sibling = repo / ".bmad-loop" / "runs" / "20260101-000001-bbbb"
    for run_dir in (racer, sibling):
        save_state(
            run_dir,
            RunState(run_id=run_dir.name, project=str(repo), started_at="x", finished=True),
        )
    real_cleanup = getattr(runs, helper_name)

    def racing_cleanup(project_path, run_dir, **kw):
        if run_dir == racer:
            raise runs.LiveEngineError("engine resumed")
        return real_cleanup(project_path, run_dir, **kw)

    monkeypatch.setattr(runs, helper_name, racing_cleanup)

    extra = ("--hard",) if hard else ()
    doc = _clean_json(repo, capsys, "--retain", "0", *extra)

    assert doc["protected"] == [racer.name]
    assert doc[result_key] == [sibling.name]
    assert doc[other_key] == []
    assert racer.is_dir() and not sibling.exists()


def test_cmd_clean_text_identifies_a_resumed_engine(project, monkeypatch, capsys):
    """Text mode distinguishes an engine resume from an agent-session race."""
    install_bmad_config(project)
    repo = project.project
    racer = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(
        racer,
        RunState(run_id=racer.name, project=str(repo), started_at="x", finished=True),
    )

    def racing_archive(_project, _run_dir, **_kw):
        raise runs.LiveEngineError("engine resumed")

    monkeypatch.setattr(runs, "archive_run", racing_archive)

    assert cli.cmd_clean(_clean_args(repo, retain=0)) == 0

    _out, err = capsys.readouterr()
    assert f"run {racer.name}: engine resumed mid-clean — not removed" in err
    assert "agent session appeared mid-clean" not in err


def _terminal_runs(repo, *names):
    """Terminal (finished) run dirs, created in the order given."""
    made = []
    for name in names:
        run_dir = repo / ".bmad-loop" / "runs" / name
        save_state(
            run_dir,
            RunState(run_id=run_dir.name, project=str(repo), started_at="x", finished=True),
        )
        made.append(run_dir)
    return made


@pytest.mark.parametrize(
    "hard, result_key, other_key",
    [(True, "deleted", "archived"), (False, "archived", "deleted")],
)
def test_cmd_clean_json_records_a_lock_held_run_and_still_sweeps_its_siblings(
    project, capsys, hard, result_key, other_key
):
    """A run whose state lock someone else holds is per-run data, exactly as the
    lifecycle refusals above it are — the sweep must not abort on it.

    THIS is the assertion that characterises the defect, not the single-run one:
    every list `clean` reports reaches the operator only in the post-loop
    emission, so an acquisition error escaping the per-candidate handler costs
    the report of the whole invocation, including siblings it had ALREADY
    deleted. The contended run therefore sits BETWEEN two siblings
    (`list_run_dirs` is sorted oldest-first), so a passing run proves the sweep
    continued past it rather than merely started before it.

    The contention is real — the run's own canonical sidecar, held across the
    whole invocation — and carries no timing dependence: `cmd_clean` acquires
    with `wait_for_lock=False`, so the rival attempt is refused at once instead
    of waiting. It is taken through `platform_util.file_lock` rather than
    `journal.state_lock` deliberately: `state_lock`'s reentrancy guard is
    thread-local, so acquiring it here would make `cmd_clean` RE-ENTER the lock
    it already holds and reclaim the run, passing for the wrong reason.
    """
    install_bmad_config(project)
    repo = project.project
    early, held, late = _terminal_runs(
        repo,
        "20260101-000000-aaaa",
        "20260101-000001-bbbb",
        "20260101-000002-cccc",
    )
    lock_path = runs.lock_path_for(held / STATE_FILE, follow_final_symlink=False)
    extra = ("--hard",) if hard else ()

    with platform_util.file_lock(lock_path):
        doc = _clean_json(repo, capsys, "--retain", "0", *extra)

    # `_clean_json` is the document assertion: rc 0, stdout parses whole, stderr empty
    assert doc["protected"] == [held.name]
    assert doc[result_key] == [early.name, late.name]  # the run AFTER it too
    assert doc[other_key] == []
    assert held.is_dir() and not early.exists() and not late.exists()


def test_cmd_clean_text_names_a_lock_held_run_and_keeps_its_siblings(project, capsys):
    """Text mode tells a held lock apart from the two races it is classified
    with, and still reports the siblings it reclaimed around it."""
    install_bmad_config(project)
    repo = project.project
    early, held, late = _terminal_runs(
        repo,
        "20260101-000000-aaaa",
        "20260101-000001-bbbb",
        "20260101-000002-cccc",
    )
    lock_path = runs.lock_path_for(held / STATE_FILE, follow_final_symlink=False)

    with platform_util.file_lock(lock_path):
        rc = cli.cmd_clean(_clean_args(repo, retain=0, hard=True))

    assert rc == 0  # not an aborted clean
    out, err = capsys.readouterr()
    assert f"run {held.name}: run state locked by another process — not removed" in err
    assert "engine resumed mid-clean" not in err
    assert "agent session appeared mid-clean" not in err
    assert held.is_dir() and not early.exists() and not late.exists()
    assert "2 deleted" in out


def test_cmd_clean_never_files_a_confinement_refusal_as_protected(project, monkeypatch, capsys):
    """The typed arm's whole reason for existing, and the ablation that grades it.

    `platform_util.UnconfinedWriteError` is an `OSError` SUBCLASS, so the bare
    `except OSError` that would also have stopped the abort would swallow a
    containment refusal — "this write escaped its root" — and file it as a benign
    `protected`/"left untouched". A refusal that security-relevant must keep
    escaping the per-candidate handler, so widening the arm reddens here.
    """
    install_bmad_config(project)
    repo = project.project
    (run_dir,) = _terminal_runs(repo, "20260101-000000-aaaa")

    def unconfined(_project, _run_dir, **_kw):
        raise platform_util.UnconfinedWriteError("run dir escaped the runs root")

    monkeypatch.setattr(runs, "delete_run", unconfined)

    with pytest.raises(platform_util.UnconfinedWriteError):
        cli.cmd_clean(_clean_args(repo, retain=0, hard=True))

    assert run_dir.is_dir()  # nothing removed, and nothing reported as reclaimed
    capsys.readouterr()


def test_cmd_clean_reclaims_past_a_session_proven_to_be_another_project_s(
    project, monkeypatch, capsys
):
    """Same run id, but the session's tag proves it belongs elsewhere — it carries
    its own ownership proof and does not need this run dir, so reclaiming strands
    nothing. Refusing would wedge `clean` for as long as the other project's run
    lives, and `clean` has no `--force`."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))
    from test_runs import _LivenessMux

    monkeypatch.setattr(
        runs,
        "get_multiplexer",
        lambda: _LivenessMux(
            ["bmad-loop-20260101-000000-aaaa"],
            tags={"bmad-loop-20260101-000000-aaaa": runs.project_tag(repo / "someone-else")},
        ),
    )

    assert cli.cmd_clean(_clean_args(repo, retain=0)) == 0

    assert not run_dir.exists()
    assert (repo / ".bmad-loop" / "archive" / "20260101-000000-aaaa.tar.gz").is_file()
    assert "still live" not in capsys.readouterr().err


# -------------------------------------------------------- cmd_clean --json


def _clean_json(repo, capsys, *extra):
    return machine_json(["clean", "--project", str(repo), "--json", *extra], capsys)


def test_cmd_clean_json_dry_run_plans_without_mutating(project, capsys):
    # the whole point of --dry-run --json: a caller inspects the plan before
    # committing, so the document must name the work AND leave the disk alone.
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    doc = _clean_json(repo, capsys, "--dry-run")

    assert doc["schema_version"] == cli.CLEAN_SCHEMA_VERSION
    assert doc["dry_run"] is True
    assert doc["worktrees"] == [str(wt)]
    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert wt.exists() and run_dir.is_dir()  # provably non-mutating
    assert (run_dir / "worktrees").is_dir()


def test_cmd_clean_json_real_run_reports_what_it_did(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "u"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "fb", "main")
    (wt / "big").write_bytes(b"x" * 4096)
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    doc = _clean_json(repo, capsys)

    assert doc["dry_run"] is False
    assert doc["worktrees"] == [str(wt)]
    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert not wt.exists()  # the real path really ran
    # a raw int, never the _human_bytes string the text mode renders
    assert isinstance(doc["freed_bytes"], int)
    assert doc["freed_bytes"] >= 4096


def test_cmd_clean_counts_the_verifier_stream_store_it_reclaimed(project, capsys):
    """The reclaim estimate is sized over what the trim actually takes.

    `freed_bytes` is what an operator reads to decide whether `clean` was worth
    running, and for a trimmed run it used to sum `worktrees/` alone. That was
    exactly right while `worktrees/` was the only heavy entry and silently wrong
    the moment the verifier stream store joined it: `clean` would remove up to
    512 KiB per verify command per attempt and report reclaiming nothing.

    Seeded with no `worktrees/` at all, so the removal and the accounting are
    graded independently and neither can ride on the other's bytes.

    Ablation, two axes reddening different assertions: drop VERIFY_DIR from
    `_HEAVY_RUN_ENTRIES` and `trimmed` empties — the trim finds nothing to take.
    Restore it but size the estimate over `worktrees/` alone again and the store
    is gone with `freed_bytes` at 0 — a reclaim that happened and went unreported.
    Verified.
    """
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    store = run_dir / VERIFY_DIR
    store.mkdir(parents=True)
    (store / "verify-1-1-a-dev-1-1-0.stdout.log").write_bytes(b"o" * 4096)
    (store / "verify-1-1-a-dev-1-1-0.stderr.log").write_bytes(b"e" * 2048)
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    doc = _clean_json(repo, capsys)

    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert not store.exists()
    assert doc["freed_bytes"] == 4096 + 2048
    assert (run_dir / "state.json").is_file()  # trimmed, not removed


@pytest.mark.skipif(
    os.name != "posix", reason="planting a directory symlink needs privilege on win32"
)
def test_cmd_clean_does_not_bill_the_reclaim_for_bytes_behind_a_redirect(project, capsys):
    """`freed_bytes` counts what the trim freed, never what a planted link points at.

    `os.walk` does not descend into links, but it does follow the top path it is
    handed — so sizing a redirected entry bills the reclaim for out-of-run bytes
    that are demonstrably still on disk when `clean` returns. That is the estimate
    an operator reads to decide whether the command was worth running, and it is
    the one number here a session can inflate from outside the run.

    Ablation: drop the `is_link_like` refusal from `_dir_size` and `freed_bytes`
    comes back 5000 — bytes the assertion below proves were never freed. Verified.
    """
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    run_dir.mkdir(parents=True, exist_ok=True)
    outside = repo.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_bytes(b"x" * 5000)
    (run_dir / VERIFY_DIR).symlink_to(outside, target_is_directory=True)
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))

    doc = _clean_json(repo, capsys)

    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert doc["freed_bytes"] == 0  # nothing inside the run was actually freed
    assert (outside / "keep.txt").is_file()  # and the 5000 bytes are still there


def test_cmd_clean_json_names_every_item_the_text_enumerates(project, capsys):
    # protected is a bare count in the text ("left N ... untouched") and
    # archived/deleted are per-line; the document names all of them.
    install_bmad_config(project)
    repo = project.project
    old = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(old, RunState(run_id="r1", project=str(repo), started_at="x", finished=True))
    kept = repo / ".bmad-loop" / "runs" / "20260101-000001-bbbb"
    save_state(kept, RunState(run_id="r2", project=str(repo), started_at="x", finished=True))
    live = repo / ".bmad-loop" / "runs" / "20260101-000002-cccc"
    save_state(live, RunState(run_id="r3", project=str(repo), started_at="x"))  # not terminal

    doc = _clean_json(repo, capsys, "--retain", "0", "--keep", "20260101-000001-bbbb")

    assert doc["archived"] == ["20260101-000000-aaaa"]
    # --keep-listed and non-terminal runs alike
    assert sorted(doc["protected"]) == ["20260101-000001-bbbb", "20260101-000002-cccc"]
    assert doc["policy"]["retain"] == 0  # the effective value: --retain wins over policy
    assert doc["policy"]["archive_old"] is True


def test_cmd_clean_json_hard_deletes_and_keeps_policy_archive_old(project, capsys):
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))

    doc = _clean_json(repo, capsys, "--retain", "0", "--hard")

    assert doc["deleted"] == ["20260101-000000-aaaa"]
    assert doc["archived"] == []
    # --hard overrides per invocation; the configured policy is reported as-is
    assert doc["policy"]["archive_old"] is True


def test_cmd_clean_json_carries_unverifiable_pid_with_empty_stderr(project, monkeypatch, capsys):
    # the text mode's stderr warning becomes a document field; machine_json's
    # default asserts stderr is empty, which is the contract being tested.
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")

    doc = _clean_json(repo, capsys)

    assert doc["unverifiable_pid"] == ["20260101-000000-aaaa"]


def test_cmd_clean_json_reports_a_live_session_run_as_protected(project, monkeypatch, capsys):
    # the live-session backstop's JSON half: the run is classified, not silently
    # dropped, and (like every other warning) stderr stays empty in JSON mode.
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))
    from test_runs import _LivenessMux

    monkeypatch.setattr(
        runs, "get_multiplexer", lambda: _LivenessMux(["bmad-loop-20260101-000000-aaaa"])
    )

    doc = _clean_json(repo, capsys, "--retain", "0")

    assert doc["protected"] == ["20260101-000000-aaaa"]
    assert doc["archived"] == [] and doc["deleted"] == []
    assert run_dir.is_dir()


def test_cmd_clean_json_nothing_to_reclaim_is_a_valid_empty_document(project, capsys):
    install_bmad_config(project)

    doc = _clean_json(project.project, capsys)

    assert doc["schema_version"] == cli.CLEAN_SCHEMA_VERSION
    assert doc["freed_bytes"] == 0
    assert doc["state_dirs_swept"] == 0
    for key in ("worktrees", "trimmed", "archived", "deleted", "protected", "unverifiable_pid"):
        assert doc[key] == [], key


# ------------------------------------------- cmd_clean: out-of-tree state (#494)


def _seed_state_dir(project, run_id):
    events = runs.events_dir_for(project, run_id)
    events.mkdir(parents=True)
    (events / "1700000000-t1-Stop.json").write_text("{}")
    return runs.state_dir_for(project, run_id)


def test_cmd_clean_removes_the_state_counterpart_of_a_deleted_run(project):
    """The whole reason `clean` needed changing: past the retention window it
    removes the run dir, and since #494 the run's control plane is no longer
    inside it. Asserted through the command rather than `delete_run` alone, since
    `clean` is the path an operator schedules and the only one that runs
    unattended."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", finished=True))
    state_dir = _seed_state_dir(repo, "20260101-000000-aaaa")

    assert cli.cmd_clean(_clean_args(repo, retain=0, hard=True)) == 0

    assert not run_dir.exists()
    assert not state_dir.exists()


def test_cmd_clean_keeps_the_state_counterpart_of_a_run_it_only_trimmed(project):
    """A trimmed run is still on disk and still resumable, so its control plane
    has to survive its scaffolding. Two paths could take it: `trim_run_dir` (which
    deliberately does not) and the orphan sweep (whose live-name check is what
    keeps it). This grades them together, at the level where a mistake in either
    would strand a resumable run's completion channel."""
    install_bmad_config(project)
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    (run_dir / "worktrees" / "u").mkdir(parents=True)
    save_state(run_dir, RunState(run_id="r", project=str(repo), started_at="x", stopped=True))
    state_dir = _seed_state_dir(repo, "20260101-000000-aaaa")

    assert cli.cmd_clean(_clean_args(repo)) == 0

    assert run_dir.is_dir() and not (run_dir / "worktrees").exists()  # trimmed, not removed
    assert state_dir.is_dir()


def test_cmd_clean_sweeps_an_orphaned_state_dir_and_reports_it(project, capsys):
    """The backstop reaching a leak `clean` did not create: a run dir removed by
    hand leaves a control plane nothing else will ever collect. The text mode says
    so on its own line — the reclaim summary above it is a byte estimate these
    dirs are deliberately outside of."""
    install_bmad_config(project)
    repo = project.project
    orphan = _seed_state_dir(repo, "20260101-000000-aaaa")

    assert cli.cmd_clean(_clean_args(repo)) == 0

    assert not orphan.exists()
    out = capsys.readouterr().out
    assert "swept 1 orphaned run state dir(s)" in out
    assert "nothing to reclaim" not in out  # something WAS reclaimed


def test_cmd_clean_dry_run_plans_the_sweep_without_removing(project, capsys):
    """Plan and outcome share one shape: the preview names the same work the real
    run would do, and provably leaves the disk alone."""
    install_bmad_config(project)
    repo = project.project
    orphan = _seed_state_dir(repo, "20260101-000000-aaaa")

    assert cli.cmd_clean(_clean_args(repo, dry_run=True)) == 0

    assert orphan.is_dir()
    assert "would sweep 1 orphaned run state dir(s)" in capsys.readouterr().out


def test_cmd_clean_json_carries_the_sweep_count_in_both_modes(project, capsys):
    """The additive field, on the existing schema version (nothing a v1 consumer
    already read changed shape). Populated under `--dry-run` too, or a caller
    pre-flighting a reclaim would see the sweep appear only after committing."""
    install_bmad_config(project)
    repo = project.project
    orphan = _seed_state_dir(repo, "20260101-000000-aaaa")

    plan = _clean_json(repo, capsys, "--dry-run")
    assert plan["schema_version"] == cli.CLEAN_SCHEMA_VERSION
    assert plan["state_dirs_swept"] == 1
    assert orphan.is_dir()

    outcome = _clean_json(repo, capsys)
    assert outcome["state_dirs_swept"] == 1
    assert not orphan.exists()


# ---------------------------------------- runs holding unlanded sweep closes (DW-571)


def _held_and_plain_stopped_runs(v7):
    """Two stopped runs past any retention window. One is an isolated sweep run
    still holding its shared-store close of DW-1, the other a plain run. Returns
    (held engine, paths, plain run dir)."""
    import test_sweep as ts

    engine, paths, *_ = ts._held_sweep_run(v7)
    engine.state.paused_stage = None
    engine.state.paused_story_key = None
    engine.state.stopped = True
    engine._save()
    plain = paths.project / ".bmad-loop" / "runs" / "20260102-000000-bbbb"
    save_state(
        plain, RunState(run_id="p", project=str(paths.project), started_at="x", stopped=True)
    )
    assert runs.reclaimable(engine.run_dir) and runs.reclaimable(plain)  # premise
    return engine, paths, plain


def test_cmd_clean_hard_skips_a_run_holding_unlanded_sweep_closes(project_v7_store_repo, capsys):
    """`clean --hard` checks the delete arm's runs for held sweep closes before it
    mutates anything. The holder is left whole (state, ledger, no trim) with a
    stderr line naming DW-1 and `bmad-loop resume`, and its sibling is still
    deleted.

    Ablation: delete the pre-check in `cmd_clean` and this fails. The sibling
    still goes, but the holder is trimmed, and the refusal surfaces only from
    `delete_run` as a mid-clean skip."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    heavy = engine.run_dir / VERIFY_DIR
    heavy.mkdir()
    (heavy / "verify.log").write_text("kept\n", encoding="utf-8")
    assert heavy in runs.heavy_run_entries(engine.run_dir)  # premise: a trim takes it
    state_before = (engine.run_dir / "state.json").read_bytes()
    ledger_before = paths.deferred_work.read_bytes()

    assert cli.cmd_clean(_clean_args(paths.project, retain=0, hard=True)) == 0

    err = capsys.readouterr().err
    assert not plain.exists()
    assert engine.run_dir.is_dir() and heavy.is_dir()  # untouched, not trimmed
    assert (engine.run_dir / "state.json").read_bytes() == state_before
    assert paths.deferred_work.read_bytes() == ledger_before
    line = next(ln for ln in err.splitlines() if ln.startswith(f"run {engine.run_dir.name}:"))
    assert "DW-1" in line and f"bmad-loop resume {engine.run_dir.name}" in line
    assert line.endswith("— not removed")


def test_cmd_clean_names_the_fault_when_a_run_could_not_be_checked(project_v7_store_repo, capsys):
    """A delete-arm run whose ledger cannot be read is skipped with a line naming
    the fault, never with the held-ids wording and an empty id list. Its sibling
    is still deleted.

    Ablation: make `_held_closes_reason` always return the held-ids text and this
    fails (the line reads `holds sweep ledger closes whose code never landed ()`)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    paths.deferred_work.write_bytes(b"\xff\xfe not utf-8\n")

    assert cli.cmd_clean(_clean_args(paths.project, retain=0, hard=True)) == 0

    err = capsys.readouterr().err
    assert engine.run_dir.is_dir() and not plain.exists()
    line = next(ln for ln in err.splitlines() if ln.startswith(f"run {engine.run_dir.name}:"))
    assert "could not be checked" in line and str(paths.deferred_work) in line
    assert "holds sweep ledger closes" not in line


def test_cmd_clean_json_reports_a_held_closes_run_as_protected(project_v7_store_repo, capsys):
    """In `--json` mode the holder lands in the existing `protected` list and the
    sibling in `deleted`.

    Green ablation: deleting the pre-check in `cmd_clean` leaves this green,
    because with nothing to trim the removal's refusal handler also files the
    holder as protected. `test_cmd_clean_hard_skips_a_run_holding_unlanded_sweep_closes`
    pins the pre-check."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)

    doc = _clean_json(paths.project, capsys, "--hard", "--retain", "0")

    assert doc["protected"] == [engine.run_dir.name]
    assert doc["deleted"] == [plain.name]
    assert engine.run_dir.is_dir() and not plain.exists()


def test_cmd_clean_reports_a_held_closes_refusal_from_delete_run(
    project_v7_store_repo, monkeypatch, capsys
):
    """A run that passed the pre-check can still be refused by `delete_run`, whose
    under-lock check is authoritative. `clean` records it beside the other
    mid-clean refusals and goes on, rather than aborting.

    Ablation: drop `runs.HeldSweepClosesError` from the removal's except tuple and
    this fails (the refusal escapes `cmd_clean`)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    real = runs.held_sweep_closes
    calls: list[Path] = []

    def held(project, run_dir):
        calls.append(run_dir)
        if run_dir == engine.run_dir and calls.count(run_dir) == 1:
            return []  # the pre-check sees nothing held
        return real(project, run_dir)

    monkeypatch.setattr(runs, "held_sweep_closes", held)

    assert cli.cmd_clean(_clean_args(paths.project, retain=0, hard=True)) == 0

    err = capsys.readouterr().err
    assert calls.count(engine.run_dir) == 2  # premise: pre-check, then delete_run
    assert engine.run_dir.is_dir() and not plain.exists()
    assert f"run {engine.run_dir.name}: holds sweep ledger closes" in err


def test_cmd_clean_policy_delete_arm_skips_a_held_closes_run(project_v7_store_repo, capsys):
    """Without `--hard`, `[cleanup] archive_old = false` also puts past-retention
    runs on the delete arm, and the holder is skipped there the same way: left
    whole and untrimmed, while its sibling is deleted.

    Ablation: narrow the pre-check in `cmd_clean` to `args.hard` and this fails
    (the holder is trimmed)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    policy = paths.project / ".bmad-loop" / "policy.toml"
    policy.write_text("[cleanup]\narchive_old = false\n", encoding="utf-8")
    heavy = engine.run_dir / VERIFY_DIR
    heavy.mkdir()
    (heavy / "verify.log").write_text("kept\n", encoding="utf-8")
    state_before = (engine.run_dir / "state.json").read_bytes()
    ledger_before = paths.deferred_work.read_bytes()

    assert cli.cmd_clean(_clean_args(paths.project, retain=0)) == 0

    err = capsys.readouterr().err
    assert not plain.exists()
    assert not (paths.project / ".bmad-loop" / "archive").exists()  # premise: delete arm
    assert engine.run_dir.is_dir() and heavy.is_dir()
    assert (engine.run_dir / "state.json").read_bytes() == state_before
    assert paths.deferred_work.read_bytes() == ledger_before
    assert f"run {engine.run_dir.name}: holds sweep ledger closes" in err


def test_cmd_clean_dry_run_plans_a_held_closes_run_as_protected(project_v7_store_repo, capsys):
    """`--dry-run --hard --json` predicts the skip: the holder is in `protected`,
    not in `deleted`, and nothing is removed.

    Ablation: gate the pre-check in `cmd_clean` on `not dry` and this fails (the
    plan lists the holder as deleted)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)

    doc = _clean_json(paths.project, capsys, "--dry-run", "--hard", "--retain", "0")

    assert engine.run_dir.name in doc["protected"]
    assert engine.run_dir.name not in doc["deleted"]
    assert doc["deleted"] == [plain.name]
    assert engine.run_dir.is_dir() and plain.is_dir()


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
def test_cmd_archive_refuses_a_run_holding_unlanded_sweep_closes(
    project_v7_store_repo, monkeypatch, capsys, force
):
    """DW-572: `archive` on a held run exits FAILURE with the refusal naming DW-1
    and `bmad-loop resume`, and writes no tarball. Under `--force` a live holder
    is refused before the live-engine stop, as on `delete`.

    Ablation: delete the `refuse_held_sweep_closes` call in `archive_run` and the
    plain row fails (the run is archived); delete the `--force` pre-check in
    `cmd_archive` and the force row fails (the engine is stopped first)."""
    engine, paths, _plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    stopped: list[Path] = []
    monkeypatch.setattr(runs, "stop_run", lambda rd, *_a, **_k: stopped.append(rd))
    if force:
        monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    args = argparse.Namespace(project=str(paths.project), run_id=engine.run_dir.name, force=force)

    assert cli.cmd_archive(args) == cli.ExitCode.FAILURE

    err = capsys.readouterr().err
    assert f"run {engine.run_dir.name}: refusing to archive it" in err
    assert "DW-1" in err and f"bmad-loop resume {engine.run_dir.name}" in err
    assert engine.run_dir.is_dir() and not (paths.project / runs.ARCHIVE_DIR).exists()
    assert stopped == []


@pytest.mark.parametrize("mode", ["text", "json"])
def test_cmd_clean_archive_arm_skips_a_held_closes_run(project_v7_store_repo, capsys, mode):
    """DW-572: the default archive arm leaves a held run whole and untrimmed and
    reports it protected, while its sibling is archived.

    Ablation: narrow the pre-check in `cmd_clean` back to the delete arm and this
    fails (the holder is trimmed, then `archive_run` refuses it)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    heavy = engine.run_dir / VERIFY_DIR
    heavy.mkdir()
    (heavy / "verify.log").write_text("kept\n", encoding="utf-8")
    state_before = (engine.run_dir / "state.json").read_bytes()
    archive_dir = paths.project / runs.ARCHIVE_DIR

    if mode == "json":
        doc = _clean_json(paths.project, capsys, "--retain", "0")
        assert doc["protected"] == [engine.run_dir.name]
        assert doc["archived"] == [plain.name]
        assert doc["trimmed"] == []
    else:
        assert cli.cmd_clean(_clean_args(paths.project, retain=0)) == 0
        err = capsys.readouterr().err
        line = next(ln for ln in err.splitlines() if ln.startswith(f"run {engine.run_dir.name}:"))
        assert "DW-1" in line and line.endswith("— not removed")

    assert not plain.exists() and (archive_dir / f"{plain.name}.tar.gz").is_file()
    assert not (archive_dir / f"{engine.run_dir.name}.tar.gz").exists()
    assert engine.run_dir.is_dir() and heavy.is_dir()
    assert (engine.run_dir / "state.json").read_bytes() == state_before


def _held_run_unit_worktree(engine, paths):
    """Mount a real git worktree under the held run's `worktrees/` dir, the shape a
    paused isolated unit leaves, plus a trimmable verifier store. Returns both."""
    wt = engine.run_dir / "worktrees" / "u"
    wt.parent.mkdir()
    verify.worktree_add(paths.repo_root, wt, "held-unit")
    heavy = engine.run_dir / VERIFY_DIR
    heavy.mkdir()
    (heavy / "verify.log").write_text("kept\n", encoding="utf-8")
    assert {wt.parent, heavy} <= set(runs.heavy_run_entries(engine.run_dir))  # premise
    return wt, heavy


@pytest.mark.parametrize(
    "trim, extra",
    [
        (True, ()),
        (True, ("--dry-run",)),
        (False, ()),
    ],
    ids=["trim", "trim-dry-run", "reconcile-only"],
)
def test_cmd_clean_leaves_a_held_runs_worktree_and_artifacts_alone(
    project_v7_store_repo, capsys, trim, extra
):
    """DW-573: inside retention, `clean` neither force-removes a held run's unit
    worktree (`reconcile_orphan_worktrees`) nor trims it (`trim_run_dir`), with
    `[cleanup] trim_artifacts` on or off and under `--dry-run`. The run is
    reported protected, and its sibling is still trimmed.

    Ablation: narrow the pre-check in `cmd_clean` back to the delete arm and
    every row fails (the worktree is reconciled and the run listed as such)."""
    engine, paths, plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    wt, heavy = _held_run_unit_worktree(engine, paths)
    (plain / VERIFY_DIR).mkdir()
    if not trim:
        policy = paths.project / ".bmad-loop" / "policy.toml"
        policy.write_text("[cleanup]\ntrim_artifacts = false\n", encoding="utf-8")

    doc = _clean_json(paths.project, capsys, *extra)

    assert doc["protected"] == [engine.run_dir.name]
    assert doc["worktrees"] == []
    assert doc["trimmed"] == ([plain.name] if trim else [])
    assert doc["archived"] == [] and doc["deleted"] == []
    assert wt.is_dir() and heavy.is_dir()
    assert wt.resolve() in [p.resolve() for p in verify.worktree_list(paths.repo_root)]


def test_cmd_clean_text_reports_a_held_run_inside_retention_as_untouched(
    project_v7_store_repo, capsys
):
    """The text line for a held run kept inside retention names DW-1 and says it
    was left untouched (nothing of it was due for removal)."""
    engine, paths, _plain = _held_and_plain_stopped_runs(project_v7_store_repo)
    wt, heavy = _held_run_unit_worktree(engine, paths)

    assert cli.cmd_clean(_clean_args(paths.project)) == 0

    out, err = capsys.readouterr()
    assert str(wt) not in out
    line = next(ln for ln in err.splitlines() if ln.startswith(f"run {engine.run_dir.name}:"))
    assert "DW-1" in line and line.endswith("— left untouched")
    assert wt.is_dir() and heavy.is_dir()
