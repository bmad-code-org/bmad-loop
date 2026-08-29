"""Disk-reclamation tests: run classification, worktree reconcile, retention,
artifact trim, and the `clean` CLI command."""

import argparse
import os

import pytest
from conftest import install_bmad_config, machine_json

from bmad_loop import cli, runs, verify
from bmad_loop.journal import VERIFY_DIR, save_state
from bmad_loop.model import RunState


def _state_run(project, run_id, **kw):
    run_dir = project / ".bmad-loop" / "runs" / run_id
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

    handled = runs.reconcile_orphan_worktrees(repo, run_dir)

    assert [p.name for p in handled] == ["unit"]
    assert not wt.exists()
    assert outside.exists()  # a worktree outside the run dir is never touched
    assert repo not in [p for p in verify.worktree_list(repo)[1:]]  # main checkout intact


def test_reconcile_orphan_worktrees_dry_run(project):
    repo = project.project
    run_dir = repo / ".bmad-loop" / "runs" / "20260101-000000-aaaa"
    wt = run_dir / "worktrees" / "unit"
    wt.parent.mkdir(parents=True)
    verify.worktree_add(repo, wt, "feat", "main")

    handled = runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=True)

    assert [p.name for p in handled] == ["unit"]
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
    assert runs.reconcile_orphan_worktrees(repo, run_dir) == []
    assert wt.exists()  # nothing was reclaimed, nothing was rmtree'd


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
    infos = runs.discover_runs(tmp_path)
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
    infos = runs.discover_runs(tmp_path)
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

    def racing(_project, run_id):
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

    def racing(_project, run_id):
        seen.append(run_id)
        return len(seen) > 1

    monkeypatch.setattr(runs, "live_session_may_be_ours", racing)

    doc = _clean_json(repo, capsys, "--retain", "0")

    assert run_dir.is_dir() and not (run_dir / "worktrees").exists()  # the racy state
    assert doc["trimmed"] == ["20260101-000000-aaaa"]
    assert doc["protected"] == []
    assert doc["archived"] == [] and doc["deleted"] == []


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
