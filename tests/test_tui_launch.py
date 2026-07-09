"""tui.launch builds exact tmux/CLI argv — verified against monkeypatched
subprocess so no real tmux server is touched, plus one real-subprocess
sanity check of the captured path.

The tmux invocations now live in the multiplexer backend (launch drives the
seam), so the tmux subprocess/which seams are patched on ``tmux_base`` (the
shared backend base where the spawn primitive lives); the captured read-only
path still shells out from ``launch`` itself."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from bmad_loop.adapters import tmux_base
from bmad_loop.tui import launch


class FakeRun:
    """Records argv; scripts the returncode of `tmux has-session`."""

    def __init__(self, has_session_rc: int = 1):
        self.calls: list[list[str]] = []
        self.has_session_rc = has_session_rc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc = self.has_session_rc if argv[1] == "has-session" else 0
        out = "@7\n" if argv[1] == "new-window" else ""
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def by_verb(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == verb]


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    return fake


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
    assert [c[1] for c in fake.calls] == ["has-session", "new-window", "set-option"]


def test_launch_without_tmux_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    assert not launch.tmux_available()
    with pytest.raises(launch.LaunchError, match="tmux not found"):
        launch.start_run_detached(tmp_path, "RID")


def test_new_window_failure_raises(monkeypatch, tmp_path: Path):
    def failing_run(argv, **kwargs):
        rc = 1 if argv[1] in ("has-session", "new-window") else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    monkeypatch.setattr(tmux_base.subprocess, "run", failing_run)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(launch.LaunchError, match="new-window.*failed: boom"):
        launch.start_run_detached(tmp_path, "RID")


def test_session_exists(monkeypatch):
    fake = FakeRun(has_session_rc=0)
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.session_exists("bmad-loop-x")
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=bmad-loop-x"]


def test_ctl_window_matches_run_id_suffix(monkeypatch):
    def fake(argv, **kwargs):
        out = "run-AAAA\nsweep-RID\nresume-BBBB\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window("RID") == "sweep-RID"
    assert launch.ctl_window("CCCC") is None


def test_ctl_window_no_session_or_tmux(monkeypatch):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no session")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window("RID") is None
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    assert launch.ctl_window("RID") is None  # no subprocess call attempted


def test_select_ctl_window_argv(fake_run):
    launch.select_ctl_window("sweep-RID")
    assert fake_run.calls == [["tmux", "select-window", "-t", "=bmad-loop-ctl:sweep-RID"]]


def test_set_return_pane_argv(fake_run):
    launch.set_return_pane("=bmad-loop-ctl:sweep-RID", "%9")
    assert fake_run.calls == [
        ["tmux", "set-option", "-w", "-t", "=bmad-loop-ctl:sweep-RID", "@bmad_return_pane", "%9"]
    ]


def test_current_pane_id_reads_pane(monkeypatch):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="%9\n", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.current_pane_id() == "%9"


def test_current_pane_id_none_outside_tmux(monkeypatch):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no server")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert launch.current_pane_id() is None


def test_start_detached_returns_window_id(fake_run, tmp_path: Path):
    assert launch.start_resolve_detached(tmp_path, "RID") == "@7"


def test_prune_ctl_windows(monkeypatch, tmp_path: Path):
    from bmad_loop import runs

    mine = runs.project_tag(tmp_path)
    # one live run (this process's pid); the others have no run dir
    live = tmp_path / ".bmad-loop" / "runs" / "20260101-000000-live"
    live.mkdir(parents=True)
    (live / "state.json").write_text("{}")
    runs.write_pid(live)

    # window format is window_id\twindow_name\t@bmad_project
    windows = (
        "@1\t0\t\n"  # the session's initial shell — not a run window
        f"@2\trun-20260101-000000-live\t{mine}\n"  # live run, ours — keep
        f"@3\tsweep-20260101-000000-dead\t{mine}\n"  # tagged-ours orphan — kill
        "@5\tsweep-20260101-000000-other\t/some/other/project\n"  # another project — skip
        f"@4\tresume-20260101-000000-cur\t{mine}\n"  # matches, but is the current window
    )
    killed: list[list[str]] = []

    def fake(argv, **kwargs):
        verb = argv[1]
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == "display-message":  # we are sitting in @4
            return subprocess.CompletedProcess(argv, 0, stdout="@4\n", stderr="")
        if verb == "list-windows":
            return subprocess.CompletedProcess(argv, 0, stdout=windows, stderr="")
        if verb == "kill-window":
            killed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert launch.prunable_ctl_windows(tmp_path) == ["sweep-20260101-000000-dead"]
    assert killed == []  # dry-run view kills nothing
    assert launch.prune_ctl_windows(tmp_path) == ["sweep-20260101-000000-dead"]
    assert killed == [["tmux", "kill-window", "-t", "@3"]]


def test_prune_ctl_windows_no_session(monkeypatch, tmp_path: Path):
    def fake(argv, **kwargs):  # has-session reports the ctl session is gone
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.prune_ctl_windows(tmp_path) == []


def test_select_ctl_window_id_argv(fake_run):
    launch.select_ctl_window_id("@7")
    assert fake_run.calls == [["tmux", "select-window", "-t", "@7"]]


def test_in_ctl_session(monkeypatch):
    monkeypatch.setattr(launch, "current_session", lambda: "bmad-loop-ctl")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert launch.in_ctl_session() is True
    monkeypatch.setattr(launch, "current_session", lambda: "some-other-session")
    assert launch.in_ctl_session() is False
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "current_session", lambda: "bmad-loop-ctl")
    assert launch.in_ctl_session() is False  # not inside tmux


def test_detach_client_argv(fake_run):
    launch.detach_client()
    assert fake_run.calls == [["tmux", "detach-client"]]


def _return_fake(monkeypatch, *, win="@5", option="%9", switch_rc=0):
    """Script tmux for return_attached_client: display-message -> window id,
    show-options -> the recorded RETURN_OPTION, switch-client -> switch_rc."""
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        verb = argv[1]
        if verb == "display-message":
            out, rc = (f"{win}\n", 0) if win is not None else ("", 1)
        elif verb == "show-options":
            out, rc = (f"{option}\n" if option else "", 0)
        elif verb == "switch-client" and argv[2] == "-t":
            out, rc = "", switch_rc
        else:
            out, rc = "", 0
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_return_attached_client_switches_to_pane(monkeypatch):
    calls = _return_fake(monkeypatch, option="%9")
    assert launch.return_attached_client() is True
    assert ["tmux", "switch-client", "-t", "%9"] in calls
    assert ["tmux", "set-option", "-wu", "-t", "@5", "@bmad_return_pane"] in calls
    assert ["tmux", "switch-client", "-l"] not in calls  # no fallback when -t works
    assert not any(c[1] == "detach-client" for c in calls)


def test_return_attached_client_switch_fallback(monkeypatch):
    calls = _return_fake(monkeypatch, option="%9", switch_rc=1)
    assert launch.return_attached_client() is True
    assert ["tmux", "switch-client", "-l"] in calls


def test_return_attached_client_detaches(monkeypatch):
    calls = _return_fake(monkeypatch, option="detach")
    assert launch.return_attached_client() is True
    assert ["tmux", "detach-client"] in calls
    assert ["tmux", "set-option", "-wu", "-t", "@5", "@bmad_return_pane"] in calls
    assert not any(c[1] == "switch-client" for c in calls)


def test_return_attached_client_noop_when_unset(monkeypatch):
    calls = _return_fake(monkeypatch, option="")
    assert launch.return_attached_client() is False
    assert not any(c[1] in ("switch-client", "detach-client", "set-option") for c in calls)


def test_return_attached_client_noop_without_tmux(monkeypatch):
    ran: list = []
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    monkeypatch.setattr(tmux_base.subprocess, "run", lambda *a, **k: ran.append(a))
    assert launch.return_attached_client() is False
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
    monkeypatch.setattr(launch, "ctl_window", lambda rid: "sweep-RID")
    monkeypatch.setattr(launch, "session_exists", lambda s: True)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: True)
    selected: list[str] = []
    monkeypatch.setattr(launch, "select_ctl_window", lambda w: selected.append(w))
    argv, return_window = launch.attach_plan(Path("/proj"), "RID")
    assert argv == ["tmux", "attach", "-t", "=bmad-loop-ctl"]
    assert return_window == "=bmad-loop-ctl:sweep-RID"
    assert selected == ["sweep-RID"]


def test_attach_plan_prefers_ctl_when_no_agent_session(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "ctl_window", lambda rid: "sweep-RID")
    monkeypatch.setattr(launch, "session_exists", lambda s: False)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    monkeypatch.setattr(launch, "select_ctl_window", lambda w: None)
    argv, return_window = launch.attach_plan(Path("/proj"), "RID")
    assert argv == ["tmux", "attach", "-t", "=bmad-loop-ctl"]
    assert return_window == "=bmad-loop-ctl:sweep-RID"


def test_attach_plan_agent_session_when_no_decision(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(launch, "ctl_window", lambda rid: None)
    monkeypatch.setattr(launch, "session_exists", lambda s: True)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    assert launch.attach_plan(Path("/proj"), "RID") == (
        ["tmux", "attach", "-t", "=bmad-loop-RID"],
        None,
    )


def test_attach_plan_none_when_nothing_to_attach(monkeypatch):
    monkeypatch.setattr(launch, "ctl_window", lambda rid: None)
    monkeypatch.setattr(launch, "session_exists", lambda s: False)
    monkeypatch.setattr(launch, "decision_pending", lambda rd: False)
    assert launch.attach_plan(Path("/proj"), "RID") is None


def test_run_captured_merges_streams(monkeypatch):
    def fake(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "bmad_loop.cli"]
        assert argv[3:] == ["validate", "--project", "/p"]
        assert kwargs.get("capture_output") and kwargs.get("text")
        return subprocess.CompletedProcess(argv, 1, stdout="ok line", stderr="FAIL line\n")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    rc, out = launch.run_captured(["validate", "--project", "/p"])
    assert rc == 1
    assert out == "ok line\nFAIL line\n"


def test_run_captured_real_subprocess():
    """End-to-end: the module really is invocable as `python -m bmad_loop.cli`."""
    rc, out = launch.run_captured(["--version"])
    assert rc == 0
    assert "bmad-loop" in out
