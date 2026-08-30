"""tui.launch builds exact tmux/CLI argv — verified against monkeypatched
subprocess so no real tmux server is touched, plus one real-subprocess
sanity check of the captured path.

The tmux invocations now live in the multiplexer backend (launch drives the
seam), so the tmux subprocess/which seams are patched on ``tmux_base`` (the
shared backend base where the spawn primitive lives); the captured read-only
path still shells out from ``launch`` itself."""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from bmad_loop import runs
from bmad_loop.adapters import tmux_base
from bmad_loop.adapters.multiplexer import MultiplexerError, get_multiplexer
from bmad_loop.tui import launch

# Every test here asserts tmux-specific argv/behaviour through the multiplexer
# seam. An installed external backend can match win32 (the herdr adapter does),
# where tmux does not — get_multiplexer() would then not bottom-fall-back to
# tmux — so pin tmux by name (a no-op on a stock POSIX box).
pytestmark = pytest.mark.usefixtures("force_tmux_backend")


class FakeRun:
    """Records argv; scripts the returncode of `tmux has-session` and the rows
    `list-windows` answers. The listing defaults to showing the window
    `new-window` just minted, which is what a real backend does — and what
    ctl_window_recorded re-proves the record against."""

    def __init__(self, has_session_rc: int = 1, windows: str = "@7\tresume-RID\n"):
        self.calls: list[list[str]] = []
        self.has_session_rc = has_session_rc
        self.windows = windows

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc = self.has_session_rc if argv[1] == "has-session" else 0
        out = ""
        if argv[1] == "new-window":
            out = "@7\n"
        elif argv[1] == "list-windows":
            out = self.windows
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def by_verb(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == verb]


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    # These tests pin the POSIX tmux argv shapes; force that backend so they
    # hold on hosts where platform selection would pick another (win32 → psmux).
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    get_multiplexer.cache_clear()
    yield fake
    get_multiplexer.cache_clear()


def expected_cli(*tail: str) -> str:
    return shlex.join([sys.executable, "-m", "bmad_loop.cli", *tail])


def test_start_run_detached_argv(fake_run, tmp_path: Path):
    launch.start_run_detached(tmp_path, "RID", epic=2, story="1-2-x", max_stories=3)

    nw0 = fake_run.by_verb("new-window")[0]
    assert nw0[nw0.index("-F") + 1] == "#{window_id}"

    # control session was missing: has-session, new-session, new-window, then
    # the project tag is stamped on the new window so cross-project cleanup
    # never closes it
    assert [c[1] for c in fake_run.calls] == [
        "has-session",
        "new-session",
        "new-window",
        "set-option",
    ]
    from bmad_loop import runs

    assert fake_run.by_verb("set-option")[0] == [
        "tmux",
        "set-option",
        "-w",
        "-t",
        "@7",
        runs.PROJECT_OPTION,
        runs.project_tag(tmp_path),
    ]
    ns = fake_run.by_verb("new-session")[0]
    assert ns == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "bmad-loop-ctl",
        "-c",
        str(tmp_path),
        # no `-e` pairs: session env is not part of the released verb, and on
        # tmux this ONE ctl session is shared by every project on the machine,
        # so no single project's value could be right for its window 0 anyway.
    ]

    nw = fake_run.by_verb("new-window")[0]
    assert nw[:2] == ["tmux", "new-window"]
    assert "-d" in nw
    assert nw[nw.index("-t") + 1] == "=bmad-loop-ctl:"
    assert nw[nw.index("-n") + 1] == "run-RID"
    assert nw[nw.index("-c") + 1] == str(tmp_path)
    assert nw[-3:-1] == ["sh", "-c"]
    shell = nw[-1]
    assert (
        expected_cli(
            "run",
            "--project",
            str(tmp_path),
            "--run-id",
            "RID",
            "--epic",
            "2",
            "--story",
            "1-2-x",
            "--max-stories",
            "3",
        )
        in shell
    )
    assert "read -r" in shell  # window stays open showing the exit status
    # after the read, return the attached client to where it came from: switch a
    # same-tmux client back to its pane, or detach a throwaway external client
    assert "@bmad_return_pane" in shell
    assert "switch-client" in shell
    assert "detach-client" in shell


def test_start_run_detached_argv_stories(fake_run, tmp_path: Path):
    launch.start_run_detached(tmp_path, "RID", spec="_bmad-output/epic-1")
    shell = fake_run.by_verb("new-window")[0][-1]
    assert (
        expected_cli(
            "run",
            "--project",
            str(tmp_path),
            "--run-id",
            "RID",
            "--spec",
            "_bmad-output/epic-1",
        )
        in shell
    )


def test_start_run_omits_blank_filters(fake_run, tmp_path: Path):
    launch.start_run_detached(tmp_path, "RID")
    shell = fake_run.by_verb("new-window")[0][-1]
    assert expected_cli("run", "--project", str(tmp_path), "--run-id", "RID") in shell
    for flag in ("--epic", "--story", "--max-stories"):
        assert flag not in shell


def test_start_sweep_detached_flags(fake_run, tmp_path: Path):
    launch.start_sweep_detached(tmp_path, "RID", no_prompt=True, decisions_only=True, max_bundles=2)
    nw = fake_run.by_verb("new-window")[0]
    assert nw[nw.index("-n") + 1] == "sweep-RID"
    shell = nw[-1]
    assert (
        expected_cli(
            "sweep",
            "--project",
            str(tmp_path),
            "--run-id",
            "RID",
            "--no-prompt",
            "--decisions-only",
            "--max-bundles",
            "2",
        )
        in shell
    )


def test_resume_detached_argv(fake_run, tmp_path: Path):
    launch.resume_detached(tmp_path, "RID")
    nw = fake_run.by_verb("new-window")[0]
    assert nw[nw.index("-n") + 1] == "resume-RID"
    assert expected_cli("resume", "--project", str(tmp_path), "RID") in nw[-1]


def test_existing_ctl_session_reused(monkeypatch, tmp_path: Path):
    fake = FakeRun(has_session_rc=0)
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    launch.resume_detached(tmp_path, "RID")
    # No new-session: the ctl session already answered has-session. The trailing
    # list-windows is resume's own check that the lookup now names the window it
    # minted — the one launch that mints a second window under a run id pays for
    # the answer it warns on.
    assert [c[1] for c in fake.calls] == [
        "has-session",
        "new-window",
        "set-option",
        "list-windows",
    ]


def test_launch_without_mux_raises(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    get_multiplexer.cache_clear()
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    assert not launch.mux_available()
    with pytest.raises(launch.LaunchError, match="multiplexer backend unavailable"):
        launch.start_run_detached(tmp_path, "RID")


def test_forced_launch_bypasses_availability(fake_run, monkeypatch, capsys, tmp_path: Path):
    from bmad_loop.adapters import multiplexer as mux_mod

    monkeypatch.setattr(mux_mod, "_FORCED_UNUSABLE_WARNED", False)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    launch.start_run_detached(tmp_path, "RID")
    assert fake_run.by_verb("new-window")
    # trusted, but not silently: the bypass names itself once on stderr
    assert "forced multiplexer backend" in capsys.readouterr().err


def test_observers_follow_forced_backend(fake_run, monkeypatch):
    """The observer gates (mux_available feeds attach/ctl-window/prune) must
    share the launch preflight's forced-aware rule — launch working while
    attach reports "nothing to attach to" would be a silent split."""
    from bmad_loop.adapters import multiplexer as mux_mod

    monkeypatch.setattr(mux_mod, "_usable", lambda mux: False)
    assert launch.mux_available() is True  # fake_run's fixture forces tmux by env


def test_new_window_failure_raises(monkeypatch, tmp_path: Path):
    def failing_run(argv, **kwargs):
        rc = 1 if argv[1] in ("has-session", "new-window") else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    monkeypatch.setattr(tmux_base.subprocess, "run", failing_run)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(launch.LaunchError, match="new-window.*failed: boom"):
        launch.start_run_detached(tmp_path, "RID")


def test_ensure_ctl_session_probe_failure_raises_launch_error(monkeypatch, tmp_path: Path):
    # has_session is raiser-side: a transport failure (timeout / missing binary) on
    # the ctl-session probe must convert to LaunchError so the TUI's launch/resume/
    # resolve handlers (which catch LaunchError) surface a toast instead of crashing
    # on the raw MultiplexerError that would otherwise slip past their except clause.
    def failing_run(argv, **kwargs):
        if argv[1] == "has-session":
            raise OSError("backend server not reachable")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", failing_run)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(launch.LaunchError, match="ctl-session setup failed"):
        launch.start_run_detached(tmp_path, "RID")


def test_session_exists(monkeypatch):
    fake = FakeRun(has_session_rc=0)
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.session_exists("bmad-loop-x")
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=bmad-loop-x"]


def _ctl_listing(monkeypatch, rows: str, project: Path | None = None) -> list[list[str]]:
    """Script the ctl-session window listing; returns the recorded argv.

    Rows are written as `<id>\\tab<name>`, and every row that does not already
    carry a third field is tagged for `project` — the state start_detached
    leaves behind, since it stamps PROJECT_OPTION on every window it mints. Pass
    a row with its own third field to script another project's window (or an
    empty one for the untagged, pre-tag-write case).
    """
    if project is not None:
        tag = runs.project_tag(project)
        rows = "".join(
            (line if line.count("\t") >= 2 else f"{line}\t{tag}") + "\n"
            for line in rows.splitlines()
        )
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        out = rows if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def _write_record(project: Path, run_id: str, win_id: str) -> Path:
    """Stand in for a launch having minted `win_id` for this run."""
    run_dir = runs.run_dir_for(project, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = run_dir / launch._CTL_WINDOW_FILE
    record.write_text(win_id, encoding="utf-8")
    return record


def test_ctl_window_id_matches_run_id_suffix(monkeypatch, tmp_path: Path):
    # The id, not the name: consumers replay the value as select/kill/option
    # targets, where a by-name resolve can land on a duplicate. With no record
    # of what the run's last launch minted, the answer is the first match.
    _ctl_listing(monkeypatch, "@1\trun-AAAA\n@2\tsweep-RID\n@3\tresume-BBBB\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"
    assert launch.ctl_window_id(tmp_path, "CCCC") is None


def test_ctl_window_id_requires_the_whole_run_id(monkeypatch, tmp_path: Path):
    # `--run-id` is caller-supplied and RUN_ID_RE admits `-`, so one run id can
    # be a suffix of another. A suffix test on `-RID` admits `run-other-RID`,
    # which sorts first, so `x` would kill the LIVE other-RID orchestrator —
    # the same wrong-window class as #482, one run over. The name is parsed and
    # the captured id compared whole.
    _ctl_listing(monkeypatch, "@1\trun-other-RID\n@2\tresume-RID\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"
    # Positive control: the neighbour is still reachable under its own id, so
    # this pins whole-id matching rather than merely refusing the collision.
    assert launch.ctl_window_id(tmp_path, "other-RID") == "@1"


def test_ctl_window_id_prefers_the_window_the_last_launch_minted(monkeypatch, tmp_path: Path):
    # #482: `e` over a parked run leaves `run-RID` in front of the live
    # `resume-RID`, and the scan alone answers the parked corpse. The recorded
    # id names the window we actually created.
    _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2")
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def test_ctl_window_id_ignores_a_record_the_listing_no_longer_shows(monkeypatch, tmp_path: Path):
    # The recorded window was killed (`x`) or pruned. Replaying a target that no
    # longer resolves is the dangerous kind of stale — an unresolvable `-t`
    # lands on the *active* window — so fall back to a window that exists.
    _ctl_listing(monkeypatch, "@1\trun-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2")
    assert launch.ctl_window_id(tmp_path, "RID") == "@1"


def test_ctl_window_id_ignores_a_record_that_now_names_another_run(monkeypatch, tmp_path: Path):
    # A backend that reuses a freed window id must not let a stale record hand
    # back a foreign run's window: the record is re-proved against the name too.
    _ctl_listing(monkeypatch, "@2\trun-OTHER\n@5\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2")
    assert launch.ctl_window_id(tmp_path, "RID") == "@5"


def test_ctl_window_id_ignores_another_projects_window(monkeypatch, tmp_path: Path):
    # The ctl session is shared across projects and `--run-id` is caller-supplied,
    # so the same run id can name a window next door. Matching on the name alone
    # makes that a legal answer — and `x` would kill a LIVE orchestrator in the
    # other project. Only this project's tag counts.
    other = runs.project_tag(tmp_path / "elsewhere")
    _ctl_listing(monkeypatch, f"@1\trun-RID\t{other}\n@2\tresume-RID\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def test_ctl_window_id_ignores_a_record_naming_another_projects_window(monkeypatch, tmp_path: Path):
    # And the record cannot smuggle one back in: it is re-proved against the
    # scoped matches, so a record naming the neighbour's window is ignored
    # rather than replayed as a kill/select target.
    other = runs.project_tag(tmp_path / "elsewhere")
    _ctl_listing(monkeypatch, f"@1\trun-RID\t{other}\n@2\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@1")
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def test_ctl_window_id_accepts_a_legacy_path_tag(monkeypatch, tmp_path: Path):
    # The ctl session is long-lived and shared across projects, so it survives the
    # upgrade that changes the tag's spelling from a path to a digest. Comparing
    # against the current digest alone strands this project's OWN orchestrator:
    # _ctl_window_candidates accepts the legacy tag and would prune the window,
    # while `a` and `x` resolve through here and could no longer reach it.
    legacy = str(tmp_path.resolve())
    _ctl_listing(monkeypatch, f"@1\trun-RID\t{legacy}\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") == "@1"

    # Still scoped: another project's legacy path tag stays foreign, so accepting
    # the legacy spelling does not widen the boundary a stop must not cross.
    other = str((tmp_path / "elsewhere").resolve())
    _ctl_listing(monkeypatch, f"@1\trun-RID\t{other}\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") is None


def test_ctl_window_id_admits_an_untagged_window_the_record_names(monkeypatch, tmp_path: Path):
    # The tag is written by a best-effort set_window_option that can fail, and a
    # window whose tag never landed must stay reachable by its own project
    # rather than by nobody. start_detached records BEFORE it tags, so the
    # record still names the window — and a record is a claim this project
    # wrote, where a run dir is only a coincidence of the id.
    _ctl_listing(monkeypatch, "@4\tresume-RID\t\n", tmp_path)
    _make_run(tmp_path)  # _record_ctl_window refuses to write without one
    _write_record(tmp_path, "RID", "@4")
    assert launch.ctl_window_id(tmp_path, "RID") == "@4"


def test_ctl_window_id_reads_the_record_after_the_listing(monkeypatch, tmp_path: Path):
    # Listing and record are two reads of a state a concurrent relaunch moves
    # between them — it mints its window, records it, then tags it — so their
    # order is load-bearing. Read the record FIRST and this call holds the id the
    # relaunch just superseded while the listing already carries both rows, and
    # `recorded in matches` replays the corpse. Hoist the read above the
    # list_windows call and this answers "@1".
    tag = runs.project_tag(tmp_path)
    _make_run(tmp_path)
    _write_record(tmp_path, "RID", "@1")

    def fake(argv, **kwargs):
        out = ""
        if argv[1] == "list-windows":
            # the relaunch lands here: its window is listed and its record written
            _write_record(tmp_path, "RID", "@2")
            out = f"@1\trun-RID\t{tag}\n@2\tresume-RID\t{tag}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def test_ctl_window_id_refuses_an_untagged_window_without_a_record(monkeypatch, tmp_path: Path):
    # A fresh `run`: the run dir exists but _record_ctl_window skipped it, so
    # nothing proves the untagged window is ours. Fail closed — restore the old
    # gate (`runs.is_run(run_dir_for(...))`) and this answers "@4", a window
    # that may belong to any project on the box.
    _ctl_listing(monkeypatch, "@4\tresume-RID\t\n", tmp_path)
    _make_run(tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") is None


def test_ctl_window_id_refuses_untagged_windows_the_record_does_not_name(
    monkeypatch, tmp_path: Path
):
    # A record that resolves to nothing must not license the *other* untagged
    # rows: drop the per-row equality and the bucket fills by listing order, so
    # `a` and `x` land on whatever sorted first.
    _ctl_listing(monkeypatch, "@1\trun-RID\t\n@2\tresume-RID\t\n", tmp_path)
    _make_run(tmp_path)
    _write_record(tmp_path, "RID", "@9")  # killed, pruned, or never in this listing
    assert launch.ctl_window_id(tmp_path, "RID") is None


def test_ctl_window_id_refuses_an_untagged_neighbour_on_a_run_id_collision(
    monkeypatch, tmp_path: Path
):
    # #531: `--run-id` is caller-supplied, so two projects can hold a run dir
    # for the same id, and the control session is shared across them. Ownership
    # inferred from that collision let this project attach to, return-stamp and
    # kill the neighbour's LIVE orchestrator window.
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    _make_run(mine)  # the collision: both projects hold a run dir for RID
    _make_run(theirs)
    _write_record(theirs, "RID", "@4")  # theirs minted it; its tag write failed
    _ctl_listing(monkeypatch, "@4\tresume-RID\t\n")

    assert launch.ctl_window_id(mine, "RID") is None

    # Positive control: the same row, resolved by the project that recorded it,
    # so the None above is the record gate refusing rather than a listing that
    # parsed to nothing or a run id that never matched.
    assert launch.ctl_window_id(theirs, "RID") == "@4"


def test_ctl_window_id_prefers_a_tagged_window_over_an_untagged_one(monkeypatch, tmp_path: Path):
    # Untagged is a fallback, not a peer, even now that it takes a record to get
    # in. Merged into one listing-ordered list the recorded untagged row beats
    # this project's correctly tagged one on index — and for `x` that
    # closes the wrong window. The tag is the stronger of the two proofs, so it
    # wins.
    _ctl_listing(monkeypatch, "@1\trun-RID\t\n@2\trun-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@1")  # recorded: the untagged row is otherwise admitted
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def test_ctl_window_id_none_when_no_window_carries_the_run_id(monkeypatch, tmp_path: Path):
    # A record can never resurrect a run whose windows are all gone.
    _ctl_listing(monkeypatch, "@1\trun-OTHER\n@3\tshell\n", tmp_path)
    _write_record(tmp_path, "RID", "@1")
    assert launch.ctl_window_id(tmp_path, "RID") is None


def test_ctl_window_id_unreadable_record_falls_back(monkeypatch, tmp_path: Path):
    # An unreadable hint is not an error — it just leaves the name scan.
    _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    run_dir = runs.run_dir_for(tmp_path, "RID")
    (run_dir / launch._CTL_WINDOW_FILE).mkdir(parents=True)  # a dir, not a file
    assert launch.ctl_window_id(tmp_path, "RID") == "@1"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_read_record_does_not_block_on_a_fifo(tmp_path: Path):
    # A session can replace its own workspace-writable record with a FIFO, and
    # opening one for reading blocks until somebody writes. action_attach reads
    # this on Textual's event loop, so that freezes the dashboard on a keypress.
    # O_NONBLOCK returns immediately; the S_ISREG check on the opened descriptor
    # then rejects it. Under an alarm because a regression here HANGS the suite —
    # and with a handler that RAISES, so the ablation fails this test rather than
    # letting the default SIGALRM disposition kill the whole pytest process.
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    os.mkfifo(run_dir / launch._CTL_WINDOW_FILE)

    # NOT TimeoutError: that is a subclass of OSError, so _read_ctl_window's own
    # `except OSError` swallows it and the ablated code still returns None — the
    # first version of this test passed against the bug, five seconds slower.
    class Blocked(Exception):
        pass

    def _blocked(_signum, _frame):
        raise Blocked("_read_ctl_window blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _blocked)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        assert launch._read_ctl_window(tmp_path, "RID") is None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")
def test_read_record_rejects_a_fifo_that_already_has_data(tmp_path: Path):
    # The case only the S_ISREG check catches, and the reason it is not redundant
    # with the other two guards: a writer holding the FIFO open with bytes queued
    # means the open does not block (so O_NONBLOCK is not what refuses it) and
    # the path is not a link (so O_NOFOLLOW is not either). Without the check the
    # queued bytes are simply read, letting a session forge the record through a
    # pipe it controls rather than a file. Ablate S_ISREG and this returns "@2".
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    fifo = run_dir / launch._CTL_WINDOW_FILE
    os.mkfifo(fifo)

    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # RDWR: no peer needed
    try:
        os.write(writer, b"@2")
        assert launch._read_ctl_window(tmp_path, "RID") is None
    finally:
        os.close(writer)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX device nodes")
def test_read_record_rejects_a_non_regular_file(tmp_path: Path):
    # O_NOFOLLOW, against the worst target a link can name. An endless source
    # is what bites hardest — reading one raises MemoryError, not an OSError, so
    # it would escape this function's "never raises" promise out through
    # action_attach, which has no handler at all — but the open refuses the link
    # before any of that, so this pins the refusal rather than the cap.
    #
    # NOT the S_ISREG check, despite reaching a device: O_NOFOLLOW fails the
    # open first, so the descriptor never exists to fstat. Ablating S_ISREG
    # leaves this test green (verified) — the queued-FIFO case above is the one
    # that pins it, because there the open succeeds.
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    (run_dir / launch._CTL_WINDOW_FILE).symlink_to("/dev/zero")

    assert launch._read_ctl_window(tmp_path, "RID") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_read_record_does_not_follow_a_symlink(tmp_path: Path):
    # O_NOFOLLOW: the name is read, not wherever it points. The target here is a
    # perfectly ordinary file holding a perfectly plausible window id, so every
    # other guard passes it — only the no-follow refuses. Symmetry with the
    # write side, which replaces the name rather than the link's target.
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.write_text("@99", encoding="utf-8")
    (run_dir / launch._CTL_WINDOW_FILE).symlink_to(elsewhere)

    assert launch._read_ctl_window(tmp_path, "RID") is None


def test_read_record_is_bounded(tmp_path: Path):
    # The cap stands on its own, without the flags: a plain regular file can be
    # arbitrarily large, and a hint is at most a window id either way.
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    (run_dir / launch._CTL_WINDOW_FILE).write_text("@" + "9" * 5_000_000, encoding="utf-8")

    recorded = launch._read_ctl_window(tmp_path, "RID")
    assert recorded is not None and len(recorded) <= launch._MAX_RECORD_BYTES


def test_ctl_window_id_invalid_utf8_record_falls_back(monkeypatch, tmp_path: Path):
    _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    record = _write_record(tmp_path, "RID", "@2")
    record.write_bytes(b"\xff")
    assert launch.ctl_window_id(tmp_path, "RID") == "@1"


def test_ctl_window_id_skips_empty_id_rows(monkeypatch, tmp_path: Path):
    # An empty id must never be returned as a target — an empty `-t` resolves
    # against the current window. psmux's qualifier passes a falsy id through.
    _ctl_listing(monkeypatch, "\tsweep-RID\n@7\tsweep-RID\n", tmp_path)
    assert launch.ctl_window_id(tmp_path, "RID") == "@7"


def test_kill_ctl_window_kills_by_resolved_id_not_a_name_token(monkeypatch, tmp_path: Path):
    # The kill replays the id this listing resolved, never a `=session:name`
    # token the backend would resolve again. With no record the scan picks the
    # first match (`@7`); what the id buys is that a rename or a new window
    # between two verbs cannot re-point the second.
    calls = _ctl_listing(monkeypatch, "@2\trun-x\n@7\tsweep-RID\n@9\tsweep-RID\n", tmp_path)
    launch.kill_ctl_window(tmp_path, "RID")
    assert ["tmux", "kill-window", "-t", "@7"] in calls


def test_attach_plan_selects_and_returns_the_recorded_window(monkeypatch, tmp_path: Path):
    # #482's first two consequences: the window the attach lands on, and the one
    # its return_window stamps @bmad_return_pane on, are the same live window.
    calls = _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2")
    monkeypatch.setattr(launch, "session_exists", lambda s: False)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    plan = launch.attach_plan(tmp_path, "RID")
    assert plan is not None
    _argv, return_window = plan
    assert return_window == "@2"
    assert ["tmux", "select-window", "-t", "@2"] in calls


def test_kill_ctl_window_follows_the_record(monkeypatch, tmp_path: Path):
    # #482's third consequence: `x` must not close the parked window and leave
    # the live one running.
    calls = _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2")
    launch.kill_ctl_window(tmp_path, "RID")
    assert ["tmux", "kill-window", "-t", "@2"] in calls


def test_ctl_window_id_no_session_or_tmux(monkeypatch, tmp_path: Path):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no session")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window_id(tmp_path, "RID") is None
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    assert launch.ctl_window_id(tmp_path, "RID") is None  # no subprocess call attempted


def test_set_return_pane_argv(fake_run):
    launch.set_return_pane("=bmad-loop-ctl:sweep-RID", "%9")
    assert fake_run.calls == [
        ["tmux", "set-option", "-w", "-t", "=bmad-loop-ctl:sweep-RID", "@bmad_return_pane", "%9"]
    ]


def test_current_return_target_bare_pane_on_tmux(monkeypatch):
    # The launch helper delegates to the backend; on tmux the seam default
    # answers the bare pane id — globally unique under the one-server model,
    # and the only form tmux's switch-client actually resolves (its window
    # resolver rejects a pane id in the `session:%N` slot). The qualified
    # composition is a psmux override, pinned in test_psmux_backend.
    def fake(argv, **kwargs):
        assert argv[-1] == "#{pane_id}"  # exactly one probe, no session probe
        return subprocess.CompletedProcess(argv, 0, stdout="%9\n", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")  # inside tmux
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.current_return_target() == "%9"


def test_current_return_target_none_outside_tmux(monkeypatch):
    # Outside tmux the TMUX guard answers None WITHOUT shelling out: against a
    # live server, display-message would answer for some OTHER client's session
    # and misreport a plain shell as being inside tmux.
    def boom(*_a, **_k):
        raise AssertionError("outside tmux, current_* must not shell out")

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    assert launch.current_return_target() is None
    assert launch.current_session() is None


def test_current_return_target_none_on_transport_failure(monkeypatch):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no server")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.current_return_target() is None


def test_current_return_target_none_on_empty_pane(monkeypatch):
    # rc-0 empty stdout from the pane probe must answer None, not "" — the
    # seam default's `or None` guard, which callers map to RETURN_DETACH.
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="\n", stderr=""),
    )
    assert launch.current_return_target() is None


def test_start_detached_uses_the_per_registry_ctl_name(tmp_path: Path, monkeypatch):
    """On a namespacing transport the launcher creates and parks into the
    per-registry control session (runs.ctl_session_for), never the fixed name:
    psmux's duplicate-server mutex is keyed on the session name machine-wide,
    so a second project minting the fixed `bmad-loop-ctl` is rejected as a
    duplicate and its launch fails. The tmux half — the fixed name,
    byte-identical argv — is pinned by test_start_run_detached_argv.

    Ablate `runs.ctl_session_for` at `_ensure_ctl_session` / the parked-window
    call (hardcode CTL_SESSION) and this fails."""

    class _NamespacedStub:
        def __init__(self):
            self.created = []
            self.parked = []

        def has_registry_namespace(self):
            return True

        def has_session(self, name):
            return False

        def new_session(self, name, cwd, cols=None, lines=None):
            self.created.append(name)

        def new_parked_window(self, session, name, cwd, argv, return_opt):
            self.parked.append((session, name))
            return "@7"

        def set_window_option(self, window, option, value):
            pass

    stub = _NamespacedStub()
    monkeypatch.setattr(launch, "get_multiplexer", lambda: stub)
    monkeypatch.setattr(launch, "mux_usable", lambda _m: True)

    assert launch.start_detached(tmp_path, ["run"], "RID", "run") == "@7"
    expected = runs.ctl_session_for(tmp_path, stub)
    assert expected.startswith(runs.CTL_SESSION + "-")
    assert stub.created == [expected]
    assert stub.parked == [(expected, "run-RID")]


@pytest.mark.parametrize(
    "drive",
    [
        lambda p: launch.resume_detached(p, "ctl"),
        lambda p: launch.start_resolve_detached(p, "ctl-0123456789abcdef"),
        lambda p: launch.start_detached(p, ["resume"], "CTL", "resume"),
    ],
)
def test_start_detached_refuses_a_control_alias_run(tmp_path: Path, drive):
    """The convergence gate: every drive path — resume, resolve, and any
    future button — mints its window and overwrites the ctl-window record
    through `start_detached`, so the control-alias refusal lives there, not
    per button (gating buttons kept finding the ungated fourth: resolve).
    First, ahead of every mux probe, so no window is minted, no record
    overwritten, and no child is launched only to bounce off the CLI gate.

    Ablate the gate in `start_detached` and all three fail (with no mux
    stubbed, the next probe raises a different LaunchError text)."""
    with pytest.raises(launch.LaunchError, match="control session's own"):
        drive(tmp_path)


def test_start_detached_returns_window_id(fake_run, tmp_path: Path):
    assert launch.start_resolve_detached(tmp_path, "RID") == "@7"


def _make_run(project: Path, run_id: str = "RID") -> Path:
    """A run dir runs.is_run accepts — the state a resume/resolve launches over."""
    run_dir = runs.run_dir_for(project, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text("{}", encoding="utf-8")
    return run_dir


def test_start_detached_records_the_window_it_minted(fake_run, tmp_path: Path):
    run_dir = _make_run(tmp_path)
    launch.resume_detached(tmp_path, "RID")
    assert (run_dir / launch._CTL_WINDOW_FILE).read_text(encoding="utf-8") == "@7"


def test_start_detached_records_nothing_without_a_run(fake_run, tmp_path: Path):
    # A fresh `run` mints the only window carrying its run id — nothing to
    # disambiguate — and the record must never conjure a directory that
    # runs.is_run would then report as not a run. The explicit skip keeps this
    # expected case out of the OSError swallow; this test pins the outcome.
    launch.start_run_detached(tmp_path, "RID")
    assert not runs.run_dir_for(tmp_path, "RID").exists()


def test_no_record_into_a_dir_that_is_not_a_run(fake_run, tmp_path: Path):
    # The case the is_run guard actually gates (the missing-dir sibling above is
    # also covered by the OSError swallow — deleting the guard leaves it green):
    # a run-dir-shaped directory without state.json (pruned, partial). Here the
    # write would *succeed*, so only the guard keeps the sidecar out.
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.mkdir(parents=True)
    launch.resume_detached(tmp_path, "RID")
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


def test_start_detached_survives_an_unwritable_record(fake_run, tmp_path: Path, monkeypatch):
    # The window is already running by the time the record is written, so a
    # failed write degrades to the name scan rather than failing the launch.
    from bmad_loop import platform_util

    run_dir = _make_run(tmp_path)
    (run_dir / launch._CTL_WINDOW_FILE).mkdir()  # a dir, not a file
    # On win32 the replace-over-a-directory denial looks like the transient
    # sharing violation atomic_replace retries; skip the ~5s backoff.
    monkeypatch.setattr(platform_util, "_REPLACE_ATTEMPTS", 1)
    assert launch.start_resolve_detached(tmp_path, "RID") == "@7"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlinked_run_dir_is_refused(fake_run, tmp_path: Path):
    # follow_symlinks=False refuses a link at the FINAL component only. Swap an
    # ancestor — the run dir itself — for a link to an external directory that
    # holds a state.json, and runs.is_run follows it, then mkstemp/os.replace
    # land the record inside the linked-to directory. Narrower than the
    # final-component escape (the name written is always `ctl-window`) but the
    # same shape, so the path has to be confined before the write.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}", encoding="utf-8")  # looks like a run
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside)

    # The launch still succeeds, and the lookup is not warned about: only one
    # window carries the run id, so the scan answers it correctly with no record.
    assert launch.resume_detached(tmp_path, "RID") == "@7"
    assert not (outside / launch._CTL_WINDOW_FILE).exists()  # nothing escaped


@pytest.mark.skipif(not launch.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_record_write_is_anchored_against_an_ancestor_swap(fake_run, tmp_path, monkeypatch):
    """The race a path check cannot close: the session re-plants the run dir as
    a link *after* confinement is established. A preflight check answers about a
    path and is stale the moment it returns, so the write follows the new link;
    the descriptor `open_dir_confined` hands back is bound to the directory it
    actually walked, so the swap renames something the write no longer consults.

    The swap is forced rather than raced with threads: hooking the helper is the
    exact interleaving an attacker who wins the window achieves, and it is
    deterministic. The positive control is the second assertion — the record
    must actually LAND (in the real, now-renamed-aside directory), so this
    cannot pass by the write simply having failed."""
    run_dir = _make_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = launch.open_dir_confined

    def swap_after_the_walk(project: Path, target: Path):
        fd = real_open(project, target)
        # attacker wins: the name now points outside, the fd still points home
        target.rename(tmp_path / "moved-aside")
        target.symlink_to(outside)
        return fd

    monkeypatch.setattr(launch, "open_dir_confined", swap_after_the_walk)
    launch.resume_detached(tmp_path, "RID")

    assert not (outside / launch._CTL_WINDOW_FILE).exists()  # nothing escaped
    landed = tmp_path / "moved-aside" / launch._CTL_WINDOW_FILE
    assert landed.read_text(encoding="utf-8") == "@7"  # and the write did happen
    assert run_dir.is_symlink()  # the swap really was in place for the write


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_record_falls_back_to_the_confinement_check_without_dir_fd(fake_run, tmp_path, monkeypatch):
    # win32 has no *at() family to anchor against, so it keeps check-then-write.
    # Exercised here from POSIX so the fallback is not left to the Windows legs
    # alone: it still has to refuse an ancestor link, just with the weaker
    # (racy, and documented as such) guarantee.
    monkeypatch.setattr(launch, "DIR_FD_ANCHORED_WRITES", False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}", encoding="utf-8")
    run_dir = runs.run_dir_for(tmp_path, "RID")
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside)

    assert launch.resume_detached(tmp_path, "RID") == "@7"
    assert not (outside / launch._CTL_WINDOW_FILE).exists()

    # Positive control: the `@7` above only says the lookup fell back to the one
    # listed window, which it would do whether or not the write was attempted.
    # With a regular run dir the same fallback branch really does write, so the
    # refusal is a refusal rather than a write that never got as far as trying.
    run_dir.unlink()
    _make_run(tmp_path)
    assert launch.resume_detached(tmp_path, "RID") == "@7"
    assert (run_dir / launch._CTL_WINDOW_FILE).read_text(encoding="utf-8") == "@7"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_forget_refuses_a_linked_run_dir(tmp_path: Path):
    """The forget path is a *delete*, so it needs no race to be redirected.

    `unlink` leaves a link at the final component alone, but the ancestors
    resolve normally: a run dir standing as a link to an external directory
    makes `run_dir / ctl-window` name a file over there, and dropping the hint
    drops that instead. Unlike the write's escape there is no window to win —
    the link can be planted whenever and simply waits for the next launch that
    fails to capture a window id.

    What this pins is the *refusal*: the standing link is caught by the
    confinement walk (`open_dir_confined` answers None), so deleting the whole
    guard is what reddens it. The residual race — a swap landing after that walk
    — is not covered here at all; `test_forget_is_anchored_against_an_ancestor_swap`
    is the one that pins the anchoring."""
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "other-project"
    outside.mkdir()
    victim = outside / launch._CTL_WINDOW_FILE
    victim.write_text("@99", encoding="utf-8")  # another project's live record

    run_dir = runs.run_dir_for(project, "RID")
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside)

    launch._forget_ctl_window(project, "RID")
    assert victim.read_text(encoding="utf-8") == "@99"  # the neighbour survived

    # Positive control: with the link gone the removal still happens, so this
    # cannot pass by _forget_ctl_window having quietly become a no-op.
    run_dir.unlink()
    run_dir.mkdir()
    (run_dir / launch._CTL_WINDOW_FILE).write_text("@2", encoding="utf-8")
    launch._forget_ctl_window(project, "RID")
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


@pytest.mark.skipif(not launch.DIR_FD_ANCHORED_WRITES, reason="dir-fd anchoring is POSIX-only")
def test_forget_is_anchored_against_an_ancestor_swap(tmp_path: Path, monkeypatch):
    """The window the confinement walk above cannot close: the session re-plants
    the run dir as a link *after* the walk and before the removal. A path-based
    unlink resolves the new link and drops the neighbour's record; the unlink
    relative to the walked descriptor names no path, so the swap renames
    something it no longer consults.

    Forced by hooking the helper rather than raced with threads — the same
    deterministic interleaving as the write's anchoring test."""
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "other-project"
    outside.mkdir()
    victim = outside / launch._CTL_WINDOW_FILE
    victim.write_text("@99", encoding="utf-8")  # another project's live record

    run_dir = runs.run_dir_for(project, "RID")
    run_dir.parent.mkdir(parents=True)
    run_dir.mkdir()
    (run_dir / launch._CTL_WINDOW_FILE).write_text("@2", encoding="utf-8")
    real_open = launch.open_dir_confined

    def swap_after_the_walk(proj: Path, target: Path):
        fd = real_open(proj, target)
        # attacker wins: the name now points next door, the fd still points home
        target.rename(tmp_path / "moved-aside")
        target.symlink_to(outside)
        return fd

    monkeypatch.setattr(launch, "open_dir_confined", swap_after_the_walk)
    launch._forget_ctl_window(project, "RID")

    assert victim.read_text(encoding="utf-8") == "@99"  # the neighbour survived
    # Positive control: the real record was still dropped, through the
    # descriptor — so this cannot pass by the removal simply not happening.
    assert not (tmp_path / "moved-aside" / launch._CTL_WINDOW_FILE).exists()
    assert run_dir.is_symlink()  # the swap really was in place for the unlink


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_forget_falls_back_to_the_confinement_check_without_dir_fd(tmp_path: Path, monkeypatch):
    # The win32 branch of the same refusal, exercised from POSIX rather than
    # left to the Windows legs: no *at() family there, so it check-then-deletes.
    monkeypatch.setattr(launch, "DIR_FD_ANCHORED_WRITES", False)
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "other-project"
    outside.mkdir()
    victim = outside / launch._CTL_WINDOW_FILE
    victim.write_text("@99", encoding="utf-8")

    run_dir = runs.run_dir_for(project, "RID")
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside)

    launch._forget_ctl_window(project, "RID")
    assert victim.read_text(encoding="utf-8") == "@99"

    # Positive control: the same branch still removes a record it can vouch for,
    # so the refusal above is not this branch having quietly become a no-op.
    run_dir.unlink()
    run_dir.mkdir()
    (run_dir / launch._CTL_WINDOW_FILE).write_text("@2", encoding="utf-8")
    launch._forget_ctl_window(project, "RID")
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="tab/newline are legal POSIX name bytes")
@pytest.mark.parametrize(
    "odd_name",
    ["my\tproj", "my\nproj", "my\rproj", "my\vproj", "my\x85proj", "my\u2028proj"],
    ids=["tab", "LF", "CR", "VT", "NEL", "LS"],
)
def test_a_delimiter_in_the_project_path_does_not_hide_its_own_window(
    monkeypatch, tmp_path: Path, odd_name: str
):
    """The project tag rides the same tab-delimited, line-per-window listing it
    is compared against, and a resolved project path can legally hold any of
    these bytes. A tab truncated the tag; every separator `splitlines()` knows
    split the row. Either way the tag read back was not the tag written, so this
    project's own window looked like a *neighbour's* and was discarded — and
    `a`/`x` then could not reach a run the pre-tag lookup found. Worse than a
    missed match, because the fallthrough for an unknown tag is exclusion.

    Parametrized over all six on purpose: each is a byte a resolved project path
    can legally hold, and project_tag hashes the path rather than carrying any
    spelling of it, so none of them reaches the listing. The matrix pins that the
    digest is the single mechanism — return a raw path here and the tab and the
    separators fail again, by two different routes."""
    project = tmp_path / odd_name
    project.mkdir()
    _make_run(project)
    tag = runs.project_tag(project)
    _ctl_listing(monkeypatch, f"@7\tresume-RID\t{tag}\n")

    assert launch.ctl_window_id(project, "RID") == "@7"


@pytest.mark.skipif(sys.platform == "win32", reason="separators are illegal in win32 names")
def test_a_separator_in_the_project_path_does_not_admit_a_foreign_window(
    monkeypatch, tmp_path: Path
):
    """The other half of the delimiter story, and the dangerous half.

    An earlier fix restored reach for these projects by *not comparing* tags
    when its own could not survive the listing. That admits every row carrying
    the run id — including one tagged for another project — and `x` resolves
    through here, so a stop could kill a neighbouring project's orchestrator.
    Reach and scoping are not a trade: project_tag hashes the resolved path, so
    the tag is listing-safe by construction, the comparison stays exact, and this
    row is simply not ours.

    The two assertions differ only in whose tag the row carries, which is what
    makes the refusal about the tag rather than about the listing being
    unusable."""
    mine = tmp_path / "my\nproj"
    theirs = tmp_path / "theirproj"
    mine.mkdir()
    theirs.mkdir()
    _make_run(mine)  # a run dir here, which the pre-#531 untagged gate accepted

    _ctl_listing(monkeypatch, f"@9\trun-RID\t{runs.project_tag(theirs)}\n")
    assert launch.ctl_window_id(mine, "RID") is None

    # Positive control: the identical row tagged for THIS project is found, so
    # the None above is the tag comparison refusing, not a listing that parsed
    # to nothing or a run id that never matched.
    _ctl_listing(monkeypatch, f"@9\trun-RID\t{runs.project_tag(mine)}\n")
    assert launch.ctl_window_id(mine, "RID") == "@9"


def test_a_skipped_record_forgets_the_previous_one(fake_run, tmp_path: Path):
    """Skipping the record because there is no run must still drop the old one.

    `_record_ctl_window` returns early when `runs.is_run` says no, and the
    rationale for that is a fresh `run`/`sweep`, where nothing shares the id yet.
    But the same early return is reachable with a *superseded* window live: the
    TUI reads state, shows a confirm modal, and launches from the callback, so
    anything that removes `state.json` during that human-length window (an
    external cleanup, a concurrent prune) lands here with a previous launch's
    record still on disk. That record names a window this launch just
    superseded, and `ctl_window_id` prefers a record that still resolves — so
    `a` attaches to and `x` kills the parked predecessor while the orchestrator
    this launch minted keeps running. #482's exact symptom.

    The listing puts the live window first on purpose: the fix has to be visible
    as *the record no longer steering*, not as the record happening to agree
    with first-match order."""
    tag = runs.project_tag(tmp_path)
    fake_run.windows = f"@7\tresume-RID\t{tag}\n@2\trun-RID\t{tag}\n"
    run_dir = _write_record(tmp_path, "RID", "@2")  # a previous launch's record
    assert not runs.is_run(run_dir)  # premise: no state.json, so recording skips

    assert launch.resume_detached(tmp_path, "RID") == "@7"
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()
    assert launch.ctl_window_id(tmp_path, "RID") == "@7"  # not the parked @2


def test_confinement_check_refuses_a_reparse_point_ancestor(tmp_path: Path, monkeypatch):
    """win32's junction, reachable from POSIX by faking the attribute.

    `is_symlink()` answers for the symlink reparse tag only, so a directory
    junction — which redirects traversal identically, and needs neither
    elevation nor Developer Mode to create — used to walk straight past this
    check. There is no way to make a real junction on POSIX, so the win32-only
    `st_file_attributes` field is what gets faked; `test_confinement_check_
    refuses_a_real_junction` is the same assertion against `mklink /J` and runs
    on the Windows legs."""
    project = tmp_path / "proj"
    run_dir = runs.run_dir_for(project, "RID")
    run_dir.mkdir(parents=True)
    real_lstat = os.lstat

    def lstat_with_a_reparse_bit(path, **kwargs):
        info = real_lstat(path, **kwargs)
        if Path(path) != run_dir:
            return info
        # A junction: the reparse bit is set, but S_ISLNK stays False — which is
        # exactly why is_symlink() missed it.
        return type(
            "FakeStat",
            (),
            {
                "st_mode": info.st_mode,
                "st_file_attributes": stat.FILE_ATTRIBUTE_REPARSE_POINT,
            },
        )()

    monkeypatch.setattr(launch.os, "lstat", lstat_with_a_reparse_bit)
    assert not launch._run_dir_is_confined(project, run_dir)
    # Positive control: the same dir without the bit is confined, so this cannot
    # pass by the walk having become a blanket refusal.
    monkeypatch.setattr(launch.os, "lstat", real_lstat)
    assert launch._run_dir_is_confined(project, run_dir)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are win32-only")
def test_confinement_check_refuses_a_real_junction(tmp_path: Path):
    # The unfaked version of the test above, on the platform that has junctions.
    # `mklink /J` needs no elevation, unlike `mklink /D`, so this is the cheap
    # plant a coding session can actually make.
    project = tmp_path / "proj"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = runs.run_dir_for(project, "RID")
    run_dir.parent.mkdir(parents=True)
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run_dir), str(outside)],
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr  # never silently skip the point
    assert not run_dir.is_symlink()  # the blindness this covers: not a symlink
    assert not launch._run_dir_is_confined(project, run_dir)


def test_confinement_check_refuses_an_unprobeable_ancestor(tmp_path: Path, monkeypatch):
    # `Path.is_symlink()` swallows OSError and answers False, so an ancestor
    # that cannot be probed used to be walked past as "not a link" — the
    # opposite of what the docstring promised. The probe raises now.
    project = tmp_path / "proj"
    run_dir = runs.run_dir_for(project, "RID")
    run_dir.mkdir(parents=True)
    real_lstat = os.lstat

    def lstat_denied(path, **kwargs):
        if Path(path) == run_dir:
            raise PermissionError("cannot probe")
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(launch.os, "lstat", lstat_denied)
    assert not launch._run_dir_is_confined(project, run_dir)
    # Positive control: the same directory, once it can be probed again, IS
    # confined. Without this the refusal above is satisfied by the walk having
    # become a blanket no — which is every reason a negative assertion can pass.
    monkeypatch.setattr(launch.os, "lstat", real_lstat)
    assert launch._run_dir_is_confined(project, run_dir)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlinked_record_is_replaced_not_followed(fake_run, tmp_path: Path):
    # `atomic_write_text` follows a symlink under its default contract, and the
    # run dir lives under the project root every coding session can write — so a
    # session that plants a link here would aim this *host-side* write at any
    # path the user can write, reach a workspace-confined adapter otherwise
    # denies it. The write must land on the name, never on the link's target.
    run_dir = _make_run(tmp_path)
    outside = tmp_path / "pyproject.toml"
    outside.write_text("[project]\n", encoding="utf-8")
    record = run_dir / launch._CTL_WINDOW_FILE
    record.symlink_to(outside)

    assert launch.resume_detached(tmp_path, "RID") == "@7"  # the launch still succeeds
    assert outside.read_text(encoding="utf-8") == "[project]\n"  # not redirected
    # Clobbered, not refused: the record self-heals into a plain file, so the
    # next launch does not trip over a link left in place.
    assert not record.is_symlink()
    assert record.read_text(encoding="utf-8") == "@7"


def _fail_the_record(monkeypatch, exc: BaseException) -> None:
    """Make the record write raise, whichever writer this platform records with.

    POSIX anchors the write at a directory descriptor (`atomic_write_text_at`)
    and win32 falls back to the path-based `atomic_write_text`; patching both
    keeps these tests about the degradation rather than about which branch ran.

    The two forget tests write a record FIRST and assert it is gone afterwards,
    so a patch that reached neither writer would leave that record in place and
    fail — their assertions are not satisfiable by the write simply never
    happening. `test_resume_reports_a_record_that_did_not_survive` does not use
    that shape: its control is its sibling
    `test_resume_returns_the_id_when_the_record_survives`, which runs the same
    two-window listing unpatched and gets `@7`, so the `None` here can only come
    from the record failing to land."""

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(launch, "atomic_write_text", boom)
    monkeypatch.setattr(launch, "atomic_write_text_at", boom)


def test_resume_reports_a_record_that_did_not_survive(fake_run, tmp_path: Path, monkeypatch):
    # The window id was captured, so start_detached returns it — but the record
    # did not land, which leaves ctl_window_id on the same ambiguous scan an
    # uncaptured id does. One signal for both, or the rest of the degradation
    # hides behind the success toast.
    #
    # #482's actual shape, not a bare fake: the parked `run-RID` is still listed
    # in front of the live `resume-RID`, so without the record the scan answers
    # the corpse (`@1`) and the degradation is real rather than notional.
    #
    # Tagged as start_detached leaves them, which FakeRun does not fold into its
    # scripted listing: untagged rows have needed the record to be candidates at
    # all since #531, so they would answer None here for the wrong reason — the
    # bucket being empty rather than the scan preferring the corpse.
    tag = runs.project_tag(tmp_path)
    fake_run.windows = f"@1\trun-RID\t{tag}\n@7\tresume-RID\t{tag}\n"
    _make_run(tmp_path)

    _fail_the_record(monkeypatch, OSError("read-only file system"))
    assert launch.resume_detached(tmp_path, "RID") is None


def test_resume_returns_the_id_when_the_record_survives(fake_run, tmp_path: Path):
    # The other half of the signal: over the same two-window listing, a landed
    # record makes the lookup answer the live window, so the launch is reported
    # plainly and the warning stays specific to real degradation.
    tag = runs.project_tag(tmp_path)  # the same listing, tagged for the same reason
    fake_run.windows = f"@1\trun-RID\t{tag}\n@7\tresume-RID\t{tag}\n"
    _make_run(tmp_path)
    assert launch.resume_detached(tmp_path, "RID") == "@7"


def test_resume_does_not_warn_when_the_scan_is_unambiguous(fake_run, tmp_path: Path, monkeypatch):
    # No record, but only one window carries the run id, so the scan answers the
    # right one anyway. The question is whether targeting is sound, not whether
    # a file was written — warning here would cry wolf on every launch that has
    # nothing to disambiguate.
    #
    # Carrying the tag start_detached stamps, which FakeRun does not fold into
    # its scripted listing: with the record write failing, the tag is the only
    # proof of ownership left, and an untagged row would answer None for that
    # reason rather than for the unambiguous scan this is about.
    fake_run.windows = f"@7\tresume-RID\t{runs.project_tag(tmp_path)}\n"
    _make_run(tmp_path)

    _fail_the_record(monkeypatch, OSError("read-only file system"))
    assert launch.resume_detached(tmp_path, "RID") == "@7"


def test_recorded_probe_degrades_when_the_listing_is_unreachable(tmp_path: Path, monkeypatch):
    # The probe is observation, so it degrades rather than raising into the
    # launchers — neither _do_resume nor _launch_resolve handles a
    # MultiplexerError, so an uncaught one crashes the TUI after a launch that
    # already succeeded. "Could not confirm" warns, matching the toast's hedge.
    def boom(*_a, **_k):
        raise MultiplexerError("backend server not reachable")

    monkeypatch.setattr(launch, "ctl_window_id", boom)
    assert launch.ctl_window_recorded(tmp_path, "RID", "@7") is False


def test_resume_reports_a_record_the_listing_does_not_carry(fake_run, tmp_path: Path):
    # The divergence the seam tolerates: a backend whose new_parked_window id is
    # shaped differently from its list_windows window_id column. The record
    # round-trips intact, so file equality would call this sound — but
    # ctl_window_id rejects it against the listing and falls through to the
    # first match, which is the ambiguity the warning exists for.
    # Tagged, so the fallthrough this is about has candidates: an untagged row
    # is admitted only by an id-equal record, which is the very thing diverging
    # here, so both rows would drop out and the None would be about the empty
    # bucket rather than about the shape.
    tag = runs.project_tag(tmp_path)
    fake_run.windows = f"@1\trun-RID\t{tag}\nctl:@7\tresume-RID\t{tag}\n"
    _make_run(tmp_path)
    assert launch.resume_detached(tmp_path, "RID") is None
    # The record itself landed — the divergence is in the id's shape, not the write.
    assert (runs.run_dir_for(tmp_path, "RID") / launch._CTL_WINDOW_FILE).read_text() == "@7"


def test_failed_record_forgets_the_previous_one(fake_run, tmp_path: Path, monkeypatch):
    # A launch that cannot record the window it minted must not leave the
    # *previous* launch's id authoritative — that id names a window this launch
    # just superseded, so the honest state is no record at all.
    run_dir = _make_run(tmp_path)
    _write_record(tmp_path, "RID", "@2")

    _fail_the_record(monkeypatch, OSError("disk full"))
    launch.resume_detached(tmp_path, "RID")
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


def test_failed_record_survives_a_non_oserror(fake_run, tmp_path: Path, monkeypatch):
    """`OSError` was too narrow to keep the docstring's promise that a failed
    write must not fail the launch. `atomic_write_text` resolves the path before
    its own try, and below 3.13 `Path.resolve` reports a symlink loop as
    `RuntimeError` — so a run dir reached through a looping link crashed the
    launch of a window that is *already running*, on the 3.11/3.12 legs.

    The fault is injected rather than built from a real symlink loop on purpose:
    3.13+ resolves loops without raising, so a loop-based version would pass on
    the interpreter this suite usually runs and only ever fail on the older legs
    — green here, red in CI, for a guard that was never exercised. Same reasoning
    as tests/test_engine.py's `test_failed_rollback_does_not_displace_the_commit_failure`.
    """
    run_dir = _make_run(tmp_path)
    _write_record(tmp_path, "RID", "@2")

    _fail_the_record(monkeypatch, RuntimeError("Symlink loop from '/x'"))
    launch.resume_detached(tmp_path, "RID")  # must not raise
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


def test_record_survives_a_raising_window_tag(fake_run, tmp_path: Path, monkeypatch):
    # Record-before-tag ordering: the seam declares set_window_option
    # best-effort, but a non-conforming backend raising from it must not cost
    # the record — swap the two calls in start_detached and this fails.
    run_dir = _make_run(tmp_path)

    def boom(self, *_a, **_k):
        raise MultiplexerError("tag failed")

    monkeypatch.setattr(type(get_multiplexer()), "set_window_option", boom)
    with pytest.raises(MultiplexerError):
        launch.resume_detached(tmp_path, "RID")
    assert (run_dir / launch._CTL_WINDOW_FILE).read_text(encoding="utf-8") == "@7"


def test_uncaptured_window_id_forgets_the_previous_record(monkeypatch, tmp_path: Path):
    # new-window answered no id: nothing to record, and the stale record must go.
    run_dir = _make_run(tmp_path)
    _write_record(tmp_path, "RID", "@2")

    def fake(argv, **kwargs):
        # rc 0 throughout, incl. has-session: the ctl session exists, and
        # new-window succeeds but answers no id on stdout.
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.start_resolve_detached(tmp_path, "RID") is None
    assert not (run_dir / launch._CTL_WINDOW_FILE).exists()


def test_record_round_trips_a_session_qualified_id(monkeypatch, tmp_path: Path):
    # The re-prove is a pure string match, so any qualified form works as long
    # as the mint and the window_id column agree (multiplexer's symmetry note);
    # `session:@N` is the shape psmux actually emits on both sides.
    _ctl_listing(
        monkeypatch,
        "bmad-loop-ctl:@1\trun-RID\nbmad-loop-ctl:@2\tresume-RID\n",
        tmp_path,
    )
    _write_record(tmp_path, "RID", "bmad-loop-ctl:@2")
    assert launch.ctl_window_id(tmp_path, "RID") == "bmad-loop-ctl:@2"


def test_record_with_trailing_newline_still_matches(monkeypatch, tmp_path: Path):
    # A newline-terminated record (hand-edited, foreign writer) must not fail
    # the `recorded in matches` check and silently answer the parked corpse.
    _ctl_listing(monkeypatch, "@1\trun-RID\n@2\tresume-RID\n", tmp_path)
    _write_record(tmp_path, "RID", "@2\n")
    assert launch.ctl_window_id(tmp_path, "RID") == "@2"


def _ctl_prune_fake(
    monkeypatch, tmp_path: Path, *, kill: str = "lands", kill_boom: str | None = None
) -> tuple[list[list[str]], list[int]]:
    """Stand a fake ctl session up for the prune; returns (kill-argv log, liveness
    probe log) — the second is what proves the verdict costs ONE listing.

    Two tagged-ours orphans (`@3`, `@6`) are the candidates — two, so a wrong
    one-probe-per-window implementation cannot pass the probe-count assertion.
    ``kill`` picks what the
    post-kill liveness listing then shows: `lands` (gone), `fails` (still there),
    `unknowable` (the listing itself dies in transport), `undecodable` (its
    capture defeats the strict POSIX decode — the same transport verdict),
    `session-gone` (empty —
    the session died with its last window). Those are the prune's whole verdict
    space (#435), and the listing is the only thing that distinguishes them —
    `kill-window` exits 0 in all of them.

    ``kill_boom`` names a window id whose kill-window call raises a strict-POSIX
    decode fault AFTER the command is recorded — the command may have reached
    the server, so the kill is "attempted" like any other and the listing still
    owns the verdict (#380 tracks the seam guard it escapes).
    """
    from bmad_loop import runs

    mine = runs.project_tag(tmp_path)
    # one live run (this process's pid); the others have no run dir
    live = tmp_path / ".bmad-loop" / "runs" / "20260101-000000-live"
    live.mkdir(parents=True)
    (live / "state.json").write_text("{}")
    runs.write_pid(live)

    # window format is window_id\twindow_name\t@bmad_project
    rows = [
        ("@1", "0", ""),  # the session's initial shell — not a run window
        ("@2", "run-20260101-000000-live", mine),  # live run, ours — keep
        ("@3", "sweep-20260101-000000-dead", mine),  # tagged-ours orphan — kill
        ("@5", "sweep-20260101-000000-other", "/some/other/project"),  # not ours — skip
        ("@4", "resume-20260101-000000-cur", mine),  # matches, but is the current window
        ("@6", "run-20260101-000000-dead2", mine),  # a SECOND orphan — kill
    ]
    killed: list[list[str]] = []
    probes: list[int] = []

    def fake(argv, **kwargs):
        verb = argv[1]
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == "display-message":  # we are sitting in @4
            return subprocess.CompletedProcess(argv, 0, stdout="@4\n", stderr="")
        if verb == "list-windows":
            if argv[-1] == "#{window_id}":  # the post-kill liveness probe
                # The session it asks about is half the verdict: tmux exits
                # nonzero on a session it cannot find, which list_window_ids
                # folds to [] — so a probe aimed at the wrong session reads every
                # candidate as removed, the pre-#435 optimism restored silently.
                assert argv[argv.index("-t") + 1] == f"={launch.CTL_SESSION}"
                probes.append(len(killed))
                if kill == "unknowable":
                    raise OSError("server gone")
                if kill == "undecodable":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                if kill == "session-gone":
                    # rc 1, not rc 0 with empty stdout: real tmux answers a
                    # vanished session with a nonzero exit and list_window_ids
                    # folds it to [] — same verdict, and the path the transport
                    # actually takes.
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
                gone = {a[-1] for a in killed} if kill == "lands" else set()
                return subprocess.CompletedProcess(
                    argv, 0, stdout="\n".join(r[0] for r in rows if r[0] not in gone), stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 0, stdout="".join("\t".join(r) + "\n" for r in rows), stderr=""
            )
        if verb == "kill-window":
            killed.append(list(argv))
            if kill_boom is not None and argv[-1] == kill_boom:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")  # we sit in a pane of @4
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    return killed, probes


def test_prune_ctl_windows(monkeypatch, tmp_path: Path):
    killed, probes = _ctl_prune_fake(monkeypatch, tmp_path)

    both = ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"]
    assert launch.prunable_ctl_windows(tmp_path) == both
    assert killed == []  # dry-run view kills nothing
    assert probes == []  # ...and asks nothing about liveness either
    assert launch.prune_ctl_windows(tmp_path) == (both, [], [])
    assert killed == [
        ["tmux", "kill-window", "-t", "@3"],
        ["tmux", "kill-window", "-t", "@6"],
    ]
    # ONE listing for BOTH windows, and only after every kill: the recorded value
    # is the kill count at probe time, so a per-window implementation would read
    # [1, 2] and a probe-before-kill 0.
    assert probes == [2]


def test_prune_ctl_windows_kill_decode_fault_does_not_abort_the_fan_out(
    monkeypatch, tmp_path: Path
):
    """kill_window is best-effort and reports nothing; a strict-POSIX decode
    fault of its own capture is more of the same nothing (#380), not a scan
    failure. The fan-out must continue past it and the one post-kill listing
    still hands down the verdict — a kill that landed is reported removed,
    never surfaced to the cleanup callers as an empty-armed scan failure that
    denies the kills just fired."""
    killed, probes = _ctl_prune_fake(monkeypatch, tmp_path, kill_boom="@3")

    both = ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"]
    assert launch.prune_ctl_windows(tmp_path) == (both, [], [])
    assert [argv[-1] for argv in killed] == ["@3", "@6"]  # the fault did not stop @6
    assert probes == [2]  # and the verdict still cost ONE listing, after both


def test_prune_ctl_windows_reports_a_survivor_separately(monkeypatch, tmp_path: Path):
    """kill-window is best-effort and exits 0 either way, so a window still in the
    post-kill listing must land in `survived`, never in `removed` (#435) — the
    whole point is that the report stops being optimistic."""
    _ctl_prune_fake(monkeypatch, tmp_path, kill="fails")

    assert launch.prune_ctl_windows(tmp_path) == (
        [],
        ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"],
        [],
    )


def test_prune_ctl_windows_unprobeable_liveness_claims_nothing(monkeypatch, tmp_path: Path):
    """A transport failure on the liveness listing says nothing about the kill —
    it may well have landed — so the candidate is neither removed nor survived,
    and the raise must not escape a prune that already fired its kills."""
    _ctl_prune_fake(monkeypatch, tmp_path, kill="unknowable")

    assert launch.prune_ctl_windows(tmp_path) == (
        [],
        [],
        ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"],
    )


def test_prune_ctl_windows_undecodable_liveness_is_a_transport_fault(monkeypatch, tmp_path: Path):
    """The strict POSIX decode raising on the liveness capture is the listing
    dying in transport by another name: same verdict — unverifiable, receipt
    intact — not a raw UnicodeDecodeError escaping a prune that already fired
    its kills (the seam folds it to MultiplexerError; #380 tracks the rest)."""
    _ctl_prune_fake(monkeypatch, tmp_path, kill="undecodable")

    assert launch.prune_ctl_windows(tmp_path) == (
        [],
        [],
        ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"],
    )


def test_prune_ctl_windows_reads_an_empty_listing_as_the_session_going_with_it(
    monkeypatch, tmp_path: Path
):
    """`[]` is the seam's "no windows", not a failed probe (only a transport fault
    raises) — a ctl session that died with its last window really did take the
    candidate, so pessimism here would report a phantom survivor forever."""
    _ctl_prune_fake(monkeypatch, tmp_path, kill="session-gone")

    assert launch.prune_ctl_windows(tmp_path) == (
        ["sweep-20260101-000000-dead", "run-20260101-000000-dead2"],
        [],
        [],
    )


def test_prune_ctl_windows_with_no_candidates_never_probes(monkeypatch, tmp_path: Path):
    """The listing is a real round trip; a prune with nothing to kill must not
    pay for it (and must not read an empty ctl session as anything at all)."""
    _killed, probes = _ctl_prune_fake(monkeypatch, tmp_path)
    # no runs dir for this project => every window is another project's / untagged
    other = tmp_path / "elsewhere"
    other.mkdir()

    assert launch.prune_ctl_windows(other) == ([], [], [])
    assert probes == []


def test_prune_ctl_windows_accepts_legacy_path_tag(monkeypatch, tmp_path: Path):
    """A ctl window carrying a pre-digest tag remains owned after upgrade."""
    from bmad_loop import runs

    legacy = str(tmp_path.resolve())
    windows = (
        f"@2\trun-20260101-000000-dead\t{legacy}\n"  # our own pre-upgrade window — kill
        "@3\trun-20260101-000000-alien\t/some/other/project\n"  # foreign — skip
    )

    def fake(argv, **kwargs):
        verb = argv[1]
        if verb == "list-windows":
            return subprocess.CompletedProcess(argv, 0, stdout=windows, stderr="")
        if verb == "display-message":
            return subprocess.CompletedProcess(argv, 0, stdout="@9\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert launch.prunable_ctl_windows(tmp_path) == ["run-20260101-000000-dead"]
    assert runs.project_tag(tmp_path) != legacy  # the shapes really are different


def test_prune_ctl_windows_skips_invalid_run_ids(monkeypatch, tmp_path: Path):
    """A ctl-window name is untrusted input (anyone can rename a tmux window).
    Stripping the kind prefix off `run-../../x` would hand run_dir_for a
    traversing id, steering the liveness read — and, for an untagged window,
    the run-dir ownership fallback — at a path outside the runs dir. Reject
    before recomposing (mirrors runs.prunable_sessions)."""
    from bmad_loop import runs

    mine = runs.project_tag(tmp_path)
    # a real runs dir, so the traversal has an existing anchor to climb from
    (tmp_path / ".bmad-loop" / "runs").mkdir(parents=True)
    # where the un-gated recomposition of `run-../../planted` would land: an
    # outside dir whose state.json would otherwise claim the untagged window
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "state.json").write_text("{}")

    windows = (
        f"@2\tsweep-20260101-000000-dead\t{mine}\n"  # legit orphan — still killed
        f"@3\trun-../../x\t{mine}\n"  # traversal — skipped
        f"@5\tsweep-a.b\t{mine}\n"  # invalid charset — skipped
        "@6\trun-../../planted\t\n"  # untagged — outside state.json must not claim it
    )
    killed: list[list[str]] = []

    def fake(argv, **kwargs):
        verb = argv[1]
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == "display-message":  # current window is none of the rows
            return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
        if verb == "list-windows":
            if argv[-1] == "#{window_id}":  # post-kill liveness: the kill landed
                gone = {a[-1] for a in killed}
                ids = [line.split("\t")[0] for line in windows.splitlines()]
                return subprocess.CompletedProcess(
                    argv, 0, stdout="\n".join(i for i in ids if i not in gone), stderr=""
                )
            return subprocess.CompletedProcess(argv, 0, stdout=windows, stderr="")
        if verb == "kill-window":
            killed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert launch.prunable_ctl_windows(tmp_path) == ["sweep-20260101-000000-dead"]
    assert launch.prune_ctl_windows(tmp_path) == (["sweep-20260101-000000-dead"], [], [])
    assert killed == [["tmux", "kill-window", "-t", "@2"]]


def test_prune_ctl_windows_reads_a_pre_upgrade_ctl_shaped_run_id(monkeypatch, tmp_path: Path):
    """The sweep asks the PARSE question about a window that already exists,
    never the mint's.

    `--run-id ctl-foo` was accepted before the control-session shape was
    reserved, so `run-ctl-foo` windows are parked in real control sessions
    right now. `is_valid_run_id` — the mint-side predicate — refuses that id,
    so borrowing it here leaked every such window out of `cleanup` and its
    `--dry-run` forever: never listed, never closed, and no error anywhere.
    `runs.is_parsable_run_id` asks what the name IS instead.

    What stays excluded is the narrow alias shape (`ctl`, `ctl-<16 hex>`):
    those ids are the ones a control session's own name can be, and the read
    paths keep them out of run-shaped handling everywhere.

    Ablate to `runs.is_valid_run_id` and the `run-ctl-foo` assertions fail;
    drop the alias half of `is_parsable_run_id` and the `run-ctl` /
    digest-shaped rows are pruned, failing the killed-argv assertion."""
    from bmad_loop import runs

    mine = runs.project_tag(tmp_path)
    windows = (
        f"@2\trun-ctl-foo\t{mine}\n"  # pre-upgrade run: a genuine parked window
        f"@3\trun-ctl\t{mine}\n"  # aliases the fixed control session — skipped
        f"@4\tsweep-ctl-0123456789abcdef\t{mine}\n"  # aliases a per-registry name
        f"@5\trun-ctl-0123456789abcde\t{mine}\n"  # 15 hex: not a mintable ctl name
    )
    killed: list[list[str]] = []

    def fake(argv, **kwargs):
        verb = argv[1]
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == "display-message":  # current window is none of the rows
            return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
        if verb == "list-windows":
            if argv[-1] == "#{window_id}":  # post-kill liveness: the kills landed
                gone = {a[-1] for a in killed}
                ids = [line.split("\t")[0] for line in windows.splitlines()]
                return subprocess.CompletedProcess(
                    argv, 0, stdout="\n".join(i for i in ids if i not in gone), stderr=""
                )
            return subprocess.CompletedProcess(argv, 0, stdout=windows, stderr="")
        if verb == "kill-window":
            killed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")

    expected = ["run-ctl-foo", "run-ctl-0123456789abcde"]
    assert launch.prunable_ctl_windows(tmp_path) == expected
    assert launch.prune_ctl_windows(tmp_path) == (expected, [], [])
    assert killed == [
        ["tmux", "kill-window", "-t", "@2"],
        ["tmux", "kill-window", "-t", "@5"],
    ]


class _NamespacedMux:
    """Duck-typed namespacing backend recording every session name the launch
    layer addresses. Only what the three ctl-name sites and their pre-gates
    consult — a psmux-shaped transport with no psmux."""

    def __init__(self, rows):
        self._rows = rows
        self.sessions: list[str] = []
        self.killed: list[str] = []

    def available(self):
        return True

    def has_registry_namespace(self):
        return True

    def has_session(self, session):
        self.sessions.append(session)
        return True

    def target(self, session):
        self.sessions.append(session)
        return f"={session}"

    def current_window_id(self):
        return "@1"

    def list_windows(self, session, fields):
        self.sessions.append(session)
        return list(self._rows)

    def list_window_ids(self, session):
        self.sessions.append(session)
        return [w for w, _n, _t in self._rows if w not in self.killed]

    def kill_window(self, win_id):
        self.killed.append(win_id)


def test_launch_addresses_the_per_registry_control_session(monkeypatch, tmp_path: Path):
    """Every launch-layer read of the control session resolves its name through
    `runs.ctl_session_for`, never the `CTL_SESSION` constant.

    On a namespacing transport the name carries the registry digest, so a site
    still spelling the constant addresses a session that does not exist there:
    `list_windows` answers empty and attach reports "nothing to attach", while
    the post-kill listing reads every candidate as removed — cleanup claims
    windows it never closed. All three fail silently, which is why the constant
    survived at these sites at all.

    Ablate any one of `ctl_window_id`'s listing, `ctl_target`'s token, or
    `prune_ctl_windows`' post-kill listing back to `runs.CTL_SESSION` and the
    final assertion fails naming that call."""
    from bmad_loop import runs

    mine = runs.project_tag(tmp_path)
    mux = _NamespacedMux([("@2", "run-20260101-000000-dead", mine)])
    monkeypatch.setattr(launch, "get_multiplexer", lambda: mux)

    expected = runs.ctl_session_for(tmp_path, mux)
    # premise: the digest name is what this project's control session is called,
    # and it is NOT the constant — without this the assertion below is vacuous
    assert expected.startswith(runs.CTL_SESSION + "-") and expected != runs.CTL_SESSION

    assert launch.ctl_window_id(tmp_path, "20260101-000000-dead") == "@2"
    assert launch.ctl_target(tmp_path) == f"={expected}"
    assert launch.prune_ctl_windows(tmp_path) == (["run-20260101-000000-dead"], [], [])

    assert mux.sessions and set(mux.sessions) == {expected}


def test_prune_ctl_windows_no_session(monkeypatch, tmp_path: Path):
    def fake(argv, **kwargs):  # has-session reports the ctl session is gone
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.prune_ctl_windows(tmp_path) == ([], [], [])


def test_select_ctl_window_id_argv(fake_run):
    launch.select_ctl_window_id("@7")
    assert fake_run.calls == [["tmux", "select-window", "-t", "@7"]]


def test_in_ctl_session(monkeypatch):
    # in_ctl_session is backend-honest: it trusts current_session(), which is
    # None whenever this process is not inside the selected multiplexer (the
    # old direct TMUX sniff lives in the tmux backend's _display_message now —
    # see test_in_ctl_session_outside_tmux).
    monkeypatch.setattr(launch, "current_session", lambda: "bmad-loop-ctl")
    assert launch.in_ctl_session() is True
    # ...and a per-registry name (runs.ctl_session_for on a namespacing
    # transport): the question is "am I in A control session".
    monkeypatch.setattr(launch, "current_session", lambda: "bmad-loop-ctl-0123456789abcdef")
    assert launch.in_ctl_session() is True
    monkeypatch.setattr(launch, "current_session", lambda: "some-other-session")
    assert launch.in_ctl_session() is False
    monkeypatch.setattr(launch, "current_session", lambda: None)
    assert launch.in_ctl_session() is False  # not inside the multiplexer


def test_in_ctl_session_outside_tmux(monkeypatch):
    # End-to-end through the real tmux backend: outside tmux (no TMUX env) the
    # backend's current_session() is None without shelling out, even when a
    # live server would answer display-message for some other client.
    def boom(*_a, **_k):
        raise AssertionError("outside tmux, in_ctl_session must not shell out")

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    assert launch.in_ctl_session() is False


def test_detach_client_argv(fake_run):
    launch.detach_client()
    assert fake_run.calls == [["tmux", "detach-client"]]


def _return_fake(
    monkeypatch,
    *,
    win="@5",
    option="=main:%9",
    switch_rc=0,
    fallback_rc=0,
    detach_rc=0,
    switch_exc=None,
    attached="1",
):
    """Script tmux for return_attached_client: display-message -> window id,
    show-options -> the recorded RETURN_OPTION, switch-client -t -> switch_rc,
    switch-client -l -> fallback_rc, detach-client -> detach_rc.
    return_attached_client runs inside a ctl window, so TMUX is set (the
    backend's current_window_id answers None otherwise).

    ``attached`` answers `#{session_attached}`, which switch_client's FAILURE
    path reads to tell "that target is unreachable" from "there is no client
    here at all" — tmux spends one nonzero rc on both. None makes the read
    itself fail. It defaults to "1" because a client sitting in this window is
    the premise of every case that expects ATTENDED."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        verb = argv[1]
        if verb == "display-message" and argv[-1] == "#{session_attached}":
            out, rc = (f"{attached}\n", 0) if attached is not None else ("", 1)
        elif verb == "display-message":
            out, rc = (f"{win}\n", 0) if win is not None else ("", 1)
        elif verb == "show-options":
            out, rc = (f"{option}\n" if option else "", 0)
        elif verb == "switch-client" and argv[2] == "-t":
            if switch_exc is not None:
                raise switch_exc
            out, rc = "", switch_rc
        elif verb == "switch-client" and argv[2] == "-l":
            out, rc = "", fallback_rc
        elif verb == "detach-client":
            out, rc = "", detach_rc
        else:
            out, rc = "", 0
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_return_attached_client_switches_to_pane(monkeypatch):
    calls = _return_fake(monkeypatch, option="=main:%9")
    assert launch.return_attached_client() is launch.ReturnOutcome.RETURNED
    assert ["tmux", "switch-client", "-t", "=main:%9"] in calls
    assert ["tmux", "set-option", "-wu", "-t", "@5", "@bmad_return_pane"] in calls
    assert ["tmux", "switch-client", "-l"] not in calls  # no fallback when -t works
    assert not any(c[1] == "detach-client" for c in calls)


def test_return_attached_client_switch_fallback(monkeypatch):
    calls = _return_fake(monkeypatch, option="=main:%9", switch_rc=1)
    assert launch.return_attached_client() is launch.ReturnOutcome.RETURNED
    assert ["tmux", "switch-client", "-l"] in calls
    # the fallback returned a client too, so the option is consumed — without
    # this the unset could regress to primary-success-only and stay green
    assert ["tmux", "set-option", "-wu", "-t", "@5", "@bmad_return_pane"] in calls


def test_return_attached_client_switch_fails_stays_attended(monkeypatch):
    """Stale target plus no last client, with a client still attached here: the
    refusal is a real one, the client never left this window, and the human is
    in front of it — ATTENDED, and RETURN_OPTION stays set or the post-exit
    trailer loses its retry.

    The attached count is what earns that claim rather than assuming it; the two
    tests below are the same rc with the count answering differently."""
    calls = _return_fake(monkeypatch, option="=main:%9", switch_rc=1, fallback_rc=1, attached="1")
    assert launch.return_attached_client() is launch.ReturnOutcome.ATTENDED
    assert ["tmux", "switch-client", "-l"] in calls  # fallback was attempted
    assert not any(c[1] == "set-option" for c in calls)  # option survives


def test_return_attached_client_switch_fails_with_no_client_is_unreachable(monkeypatch):
    """tmux spends ONE nonzero rc on two different facts. Measured on 3.7c from
    inside a pane whose server had no attached client, `-t <live session>`,
    `-t <other session>`, `-l` and `-t <nonexistent>` all exit 1 with "no current
    client" — so a bare rc reads "nobody is here" as "the client is still here".
    That is #659's hazard on the DEFAULT backend: the sweep keeps prompting a
    window no one is viewing and a later --repeat cycle blocks on input().

    Same rc as the test above; only the count differs, which is the whole point.
    The option survives — nothing was handed back, so a real return is still
    owed."""
    calls = _return_fake(monkeypatch, option="=main:%9", switch_rc=1, fallback_rc=1, attached="0")
    assert launch.return_attached_client() is launch.ReturnOutcome.UNREACHABLE
    assert ["tmux", "switch-client", "-l"] in calls  # the fallback still ran
    assert not any(c[1] == "set-option" for c in calls)


def test_return_attached_client_switch_fails_with_an_unreadable_count_is_unreachable(monkeypatch):
    """The count probe itself fails, so the rc stays two facts wide and neither
    can be ruled out. Unreadable and zero are different facts that meet at the
    same verdict: neither vouches that a human is still in front of this
    window."""
    calls = _return_fake(monkeypatch, option="=main:%9", switch_rc=1, fallback_rc=1, attached=None)
    assert launch.return_attached_client() is launch.ReturnOutcome.UNREACHABLE
    assert not any(c[1] == "set-option" for c in calls)


def test_return_attached_client_unvouched_switch_is_unreachable(monkeypatch):
    """A switch whose answer never arrived: the server may already have put the
    client on the target, so this must not report that a human is still in front
    of this window. UNREACHABLE stops the prompting a later --repeat cycle would
    block on, and RETURN_OPTION survives so a real hand-back is still owed.

    ATTENDED here is #659's hazard one seam up, and the surviving option is no
    rescue for it: the parked trailer sits behind the same blocking read the
    stuck cycle never reaches. The `-l` leg must stay unreached too — firing it
    at a client that already went where it was asked is the drag itself."""
    calls = _return_fake(
        monkeypatch,
        option="=main:%9",
        switch_exc=subprocess.TimeoutExpired(["tmux"], 30),
    )
    assert launch.return_attached_client() is launch.ReturnOutcome.UNREACHABLE
    assert ["tmux", "switch-client", "-t", "=main:%9"] in calls
    assert ["tmux", "switch-client", "-l"] not in calls
    assert not any(c[1] == "set-option" for c in calls)  # option survives


def test_return_attached_client_detach_fails_is_unreachable(monkeypatch):
    """`detach-client` fails only when there is no current client, so a failed
    detach is positive evidence that nobody is watching — the opposite of a
    failed switch, and NOT the same answer. RETURN_OPTION still survives."""
    calls = _return_fake(monkeypatch, option="detach", detach_rc=1)
    assert launch.return_attached_client() is launch.ReturnOutcome.UNREACHABLE
    assert ["tmux", "detach-client"] in calls
    assert not any(c[1] == "set-option" for c in calls)


def test_return_attached_client_detaches(monkeypatch):
    calls = _return_fake(monkeypatch, option="detach")
    assert launch.return_attached_client() is launch.ReturnOutcome.RETURNED
    assert ["tmux", "detach-client"] in calls
    assert ["tmux", "set-option", "-wu", "-t", "@5", "@bmad_return_pane"] in calls
    assert not any(c[1] == "switch-client" for c in calls)


def test_return_attached_client_noop_when_unset(monkeypatch):
    """No return target recorded — a plain foreground sweep. Nothing was
    attempted, so nothing can be concluded about who is at the terminal: the
    conservative ATTENDED, never UNREACHABLE."""
    calls = _return_fake(monkeypatch, option="")
    assert launch.return_attached_client() is launch.ReturnOutcome.ATTENDED
    assert not any(c[1] in ("switch-client", "detach-client", "set-option") for c in calls)


def test_return_attached_client_noop_without_tmux(monkeypatch):
    # This is a NEGATIVE gate test: the module-wide force_tmux_backend pin makes
    # mux_usable trust the backend regardless of available(), so drop the pin
    # (and the pinned selection) or — inside a real tmux session, TMUX set —
    # the trusted path reaches display-message and shells out after all.
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    get_multiplexer.cache_clear()
    ran: list = []
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    monkeypatch.setattr(tmux_base.subprocess, "run", lambda *a, **k: ran.append(a))
    assert launch.return_attached_client() is launch.ReturnOutcome.ATTENDED
    assert ran == []  # never shells out when tmux is missing


def test_decision_pending_true(tmp_path: Path):
    from bmad_loop.journal import Journal

    rd = tmp_path / "run"
    j = Journal(rd)
    j.append("triage-done")
    j.append("decision-pending", dw_id="DW-90", question="?")
    assert launch.decision_pending(rd) is True


def test_decision_pending_false_after_answer(tmp_path: Path):
    from bmad_loop.journal import Journal

    rd = tmp_path / "run"
    j = Journal(rd)
    j.append("decision-pending", dw_id="DW-90", question="?")
    j.append("decision-answered", dw_id="DW-90", key="1")
    assert launch.decision_pending(rd) is False


def test_decision_pending_false_when_empty(tmp_path: Path):
    assert launch.decision_pending(tmp_path / "missing") is False


def test_attach_plan_prefers_ctl_when_decision_pending(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, rid: "@2")
    monkeypatch.setattr(launch, "session_exists", lambda s: True)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: True)
    selected: list[str] = []
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    argv, return_window = launch.attach_plan(Path("/proj"), "RID")
    assert argv == ["tmux", "attach", "-t", "=bmad-loop-ctl"]
    assert return_window == "@2"
    assert selected == ["@2"]


def test_attach_plan_prefers_ctl_when_no_agent_session(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, rid: "@2")
    monkeypatch.setattr(launch, "session_exists", lambda s: False)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: None)
    argv, return_window = launch.attach_plan(Path("/proj"), "RID")
    assert argv == ["tmux", "attach", "-t", "=bmad-loop-ctl"]
    assert return_window == "@2"


def test_attach_plan_agent_session_when_no_decision(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, rid: None)
    monkeypatch.setattr(launch, "session_exists", lambda s: True)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    assert launch.attach_plan(Path("/proj"), "RID") == (
        ["tmux", "attach", "-t", "=bmad-loop-RID"],
        None,
    )


def test_attach_plan_none_when_nothing_to_attach(monkeypatch):
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, rid: None)
    monkeypatch.setattr(launch, "session_exists", lambda s: False)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    assert launch.attach_plan(Path("/proj"), "RID") is None


def test_run_captured_merges_streams(monkeypatch):
    def fake(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "bmad_loop.cli"]
        assert argv[3:] == ["validate", "--project", "/p"]
        # encoding= puts subprocess in text mode without setting the `text`
        # kwarg, so assert on the decoding that is actually pinned. UTF-8 at
        # errors="replace" is the point: text=True would decode with the
        # locale encoding at errors="strict" (the #200 failure family).
        assert kwargs.get("capture_output")
        assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace"
        return subprocess.CompletedProcess(argv, 1, stdout="ok line", stderr="FAIL line\n")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    rc, out = launch.run_captured(["validate", "--project", "/p"])
    assert rc == 1
    assert out == "ok line\nFAIL line\n"


def test_run_captured_streams_keeps_stderr_off_stdout(monkeypatch):
    """The reason the seam exists: a caller parsing stdout as one JSON document
    must not receive a dependency's stderr warning appended to it. Merged, this
    is exactly the input that makes json.loads raise "Extra data"."""
    payload = '{"schema_version": 1, "ok": true}'

    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=payload, stderr="DeprecationWarning: whatever\n"
        )

    monkeypatch.setattr(launch.subprocess, "run", fake)
    rc, out, err = launch.run_captured_streams(["validate", "--project", "/p", "--json"])
    assert rc == 0
    assert out == payload
    assert "DeprecationWarning" in err
    assert json.loads(out) == {"schema_version": 1, "ok": True}
    # and the merging caller still gets the blob it wants, from the same call
    assert launch.run_captured(["validate", "--project", "/p"])[1] == (
        payload + "\nDeprecationWarning: whatever\n"
    )


def test_run_captured_real_subprocess():
    """End-to-end: the module really is invocable as `python -m bmad_loop.cli`."""
    rc, out = launch.run_captured(["--version"])
    assert rc == 0
    assert "bmad-loop" in out


def test_run_captured_streams_real_subprocess():
    """The separated form against the real CLI: a `--json` document parses off
    stdout alone, with stderr empty (the machine.py purity contract)."""
    rc, out, err = launch.run_captured_streams(["--version"])
    assert rc == 0
    assert "bmad-loop" in out
    assert err == ""
