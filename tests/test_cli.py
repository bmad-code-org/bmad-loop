"""CLI command tests — init policy-derived profiles and per-stage dry-run."""

import argparse
import json

import pytest
from conftest import install_bmad_config, write_sprint

from automator import cli
from automator import policy as policy_mod

DUAL_CLIENT_POLICY = """\
[adapter]
name = "claude"
model = "opus"
[adapter.review]
name = "codex"
model = "gpt-5-codex"
"""


def _write_policy(project, text=DUAL_CLIENT_POLICY) -> None:
    automator_dir = project / ".automator"
    automator_dir.mkdir(parents=True, exist_ok=True)
    (automator_dir / "policy.toml").write_text(text)


def test_init_registers_hooks_for_all_policy_profiles(tmp_path):
    _write_policy(tmp_path)
    assert cli.main(["init", "--project", str(tmp_path)]) == 0
    assert "Stop" in json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]
    assert "Stop" in json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]


def test_init_without_policy_defaults_to_claude(tmp_path):
    assert cli.main(["init", "--project", str(tmp_path)]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".codex").exists()
    # init installs the bundled skills by default
    assert (tmp_path / ".claude" / "skills" / "bmad-auto-sweep" / "SKILL.md").is_file()


def test_init_no_skills_flag(tmp_path):
    assert cli.main(["init", "--project", str(tmp_path), "--no-skills"]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_init_force_skills_flag(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-auto-sweep" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    assert cli.main(["init", "--project", str(tmp_path), "--force-skills"]) == 0
    assert skill_md.read_text() != "CUSTOM"


def test_dry_run_renders_per_stage_commands(project, capsys):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    dev_line = next(line for line in out.splitlines() if "dev:" in line)
    review_line = next(line for line in out.splitlines() if "review:" in line)
    assert "claude" in dev_line and "--model opus" in dev_line
    assert review_line.split("review:")[1].strip().startswith("codex ")
    assert "--model gpt-5-codex" in review_line


@pytest.mark.parametrize(
    "epic,story",
    [(None, "3-1"), (None, "3.1"), (3, "1"), (None, "user-auth"), (None, "3-1-user-auth")],
)
def test_dry_run_selects_story_by_short_ref(project, capsys, epic, story):
    write_sprint(
        project,
        {"3-1-user-auth": "ready-for-dev", "3-2-foo": "backlog", "4-1-bar": "backlog"},
    )
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    args = argparse.Namespace(epic=epic, story=story, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    assert "3-1-user-auth" in out
    assert "3-2-foo" not in out and "4-1-bar" not in out


def test_dry_run_reports_targeted_not_actionable(project, capsys):
    write_sprint(project, {"3-1-user-auth": "ready-for-dev", "3-2-foo": "done"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    args = argparse.Namespace(epic=None, story="3-2", max_stories=None)

    assert cli._dry_run(project, pol, args) == 1
    err = capsys.readouterr().err
    assert "3-2 matched 3-2-foo" in err and "not actionable" in err


def _make_run_with_decision(project, run_id="20260101-000000-aaaa", dw_id="DW-1"):
    run_dir = project.project / ".automator" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": str(project.project),
                "started_at": "now",
                "run_type": "sweep",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "triage.json").write_text(
        json.dumps(
            {
                "workflow": "deferred-sweep-triage",
                "open_ids": [dw_id],
                "already_resolved": [],
                "bundles": [],
                "blocked": [],
                "skip": [],
                "decisions": [
                    {
                        "id": dw_id,
                        "question": "build the widening?",
                        "context": "ctx",
                        "options": [
                            {"key": "1", "label": "Widen", "effect": "build", "intent": "widen it"},
                            {"key": "2", "label": "Keep", "effect": "keep-open"},
                        ],
                        "recommendation": "1",
                    }
                ],
                "escalations": [],
            }
        ),
        encoding="utf-8",
    )


def test_decisions_none_pending(project, capsys):
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "done 2026-06-01"})
    assert cli.main(["decisions", "--project", str(project.project)]) == 0
    assert "no unanswered decisions" in capsys.readouterr().out


def test_decisions_list(project, capsys):
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project)
    assert cli.main(["decisions", "--project", str(project.project), "--list"]) == 0
    out = capsys.readouterr().out
    assert "1 unanswered decision" in out
    assert "DW-1: build the widening?" in out
    assert "[1] Widen — build  (recommended)" in out


def test_decisions_answer_records_and_carries_forward(project, capsys, monkeypatch):
    from conftest import write_ledger

    from automator import decisions

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project)

    class _StubPrompter:
        def ask(self, decision):
            return decision.option("1")  # choose build

    monkeypatch.setattr("automator.sweep.DecisionPrompter", lambda *a, **k: _StubPrompter())
    assert cli.main(["decisions", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "DW-1: queued" in out
    stored = decisions.load_pre_answers(project.project)
    assert stored["DW-1"]["effect"] == "build"
    # and it no longer shows as pending
    assert decisions.pending_missed_decisions(project.project) == []


def test_status_surfaces_missed_decision_count(project, capsys):
    from conftest import write_ledger, write_sprint

    install_bmad_config(project)
    write_sprint(project, {})
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project, run_id="20260102-000000-bbbb")
    # status needs a run to report; the decision run dir doubles as one
    assert cli.main(["status", "--project", str(project.project)]) == 0
    assert "decisions awaiting an answer: 1" in capsys.readouterr().out


def test_status_resolves_partial_ref(project, capsys):
    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    # the trailing segment alone resolves to the full run
    assert cli.main(["status", "--project", str(project.project), "aaaa"]) == 0
    assert "run 20260101-000000-aaaa" in capsys.readouterr().out


def test_status_unknown_ref_errors(project, capsys):
    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    assert cli.main(["status", "--project", str(project.project), "zzzz"]) == 1
    assert "no such run: zzzz" in capsys.readouterr().err


def test_status_ambiguous_ref_errors(project, capsys):
    _make_run_with_decision(project, run_id="20260101-000000-aa11")
    _make_run_with_decision(project, run_id="20260102-000000-aa22")
    assert cli.main(["status", "--project", str(project.project), "aa"]) == 1
    assert "ambiguous run ref 'aa' matches 2 runs" in capsys.readouterr().err


def test_list_shows_short_refs(project, capsys):
    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    _make_run_with_decision(project, run_id="20260102-000000-bbbb")
    assert cli.main(["list", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "REF" in out
    assert "aaaa" in out and "bbbb" in out
    assert "20260101-000000-aaaa" in out


def test_list_no_runs(project, capsys):
    assert cli.main(["list", "--project", str(project.project)]) == 0
    assert "no runs found" in capsys.readouterr().out


def test_attach_records_return_pane_inside_tmux(project, monkeypatch):
    from automator.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (
            ["tmux", "switch-client", "-t", "=bmad-auto-ctl"],
            "=bmad-auto-ctl:sweep-RID",
        ),
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setattr(launch, "current_pane_id", lambda: "%3")
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    called: list = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == [("=bmad-auto-ctl:sweep-RID", "%3")]
    assert called == [["tmux", "switch-client", "-t", "=bmad-auto-ctl"]]


def test_attach_records_detach_outside_tmux(project, monkeypatch):
    from automator.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (
            ["tmux", "attach", "-t", "=bmad-auto-ctl"],
            "=bmad-auto-ctl:sweep-RID",
        ),
    )
    monkeypatch.delenv("TMUX", raising=False)
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == [("=bmad-auto-ctl:sweep-RID", launch.RETURN_DETACH)]


def test_attach_agent_session_records_no_return(project, monkeypatch):
    from automator.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (["tmux", "attach", "-t", "=bmad-auto-20260101-000000-aaaa"], None),
    )
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    called: list = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == []
    assert called == [["tmux", "attach", "-t", "=bmad-auto-20260101-000000-aaaa"]]


def test_attach_nothing_to_attach(project, monkeypatch, capsys):
    from automator.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(launch, "attach_plan", lambda proj, rid: None)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 1
    assert "nothing to attach" in capsys.readouterr().err


def test_sweep_dry_run_lists_open_entries(project, capsys):
    from conftest import write_ledger

    write_ledger(project, {"DW-1": "open", "DW-2": "done 2026-06-01"}, commit=False)
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    out = capsys.readouterr().out
    assert "1 open" in out
    assert "DW-1" in out and "DW-2" not in out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert "bmad-auto-sweep" in triage_line


def test_sweep_dry_run_reports_legacy_entries(project, capsys):
    from conftest import write_legacy_ledger

    write_legacy_ledger(
        project,
        "# Deferred Work\n\n## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — repaired~~ → fixed in 1.3\n"
        "- **Open legacy thing here** — still pending\n",
        commit=False,
    )
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    out = capsys.readouterr().out
    assert "0 open" in out  # canonical view
    assert "2 legacy (pre-DW-format) entries, 1 open" in out
    assert "would first migrate them" in out
    assert "Open legacy thing here" in out and "Old fixed thing" not in out
    assert "triage:" in out  # a sweep still runs even with zero canonical opens


def test_sweep_dry_run_renders_triage_adapter_from_policy(project, capsys):
    from conftest import write_ledger

    write_ledger(project, {"DW-1": "open"}, commit=False)
    _write_policy(
        project.project,
        '[adapter]\nmodel = "opus"\n[adapter.triage]\nname = "gemini"\n',
    )
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    assert cli._sweep_dry_run(project, pol) == 0
    out = capsys.readouterr().out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert triage_line.split("triage:")[1].strip().startswith("gemini ")
    # client switch: base model is claude-specific, must not leak into gemini
    assert "--model" not in triage_line


def test_sweep_dry_run_no_ledger(project, capsys):
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    assert "no deferred-work ledger" in capsys.readouterr().out


def test_make_adapters_review_synthesizes_from_spec(project):
    """Both dev AND review are bmad-dev-auto runs that write no result.json, so
    both roles must get the spec-synthesizing GenericDevAdapter; triage (a real
    result.json skill) stays a plain GenericAdapter."""
    from automator.adapters.generic import GenericAdapter, GenericDevAdapter

    install_bmad_config(project)
    adapters = cli._make_adapters(
        project.project, project.project / ".automator" / "runs" / "r", policy_mod.load(None)
    )
    assert isinstance(adapters["dev"], GenericDevAdapter)
    assert isinstance(adapters["review"], GenericDevAdapter)
    assert isinstance(adapters["triage"], GenericAdapter)
    assert not isinstance(adapters["triage"], GenericDevAdapter)


class _StubEngine:
    def __init__(self, **kwargs):
        pass

    def run(self):
        class Summary:
            paused = False

            def render(self):
                return "stub summary"

        return Summary()


def test_run_honors_preassigned_run_id_and_writes_pid(project, monkeypatch):
    import os

    from conftest import git, install_base_skills

    install_bmad_config(project)
    install_base_skills(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "setup")
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    run_id = "20990101-000000-beef"
    assert cli.main(["run", "--project", str(project.project), "--run-id", run_id]) == 0
    run_dir = project.project / ".automator" / "runs" / run_id
    assert json.loads((run_dir / "state.json").read_text())["run_id"] == run_id
    # engine.pid is "<pid>" or "<pid> <identity>" (identity persisted on platforms
    # that provide one) — assert on the pid token, not the whole line.
    assert (run_dir / "engine.pid").read_text().split()[0] == str(os.getpid())


def test_run_aborts_when_base_skills_missing(project, monkeypatch, capsys):
    """The orchestrator depends on the non-bundled upstream skills (bmad-dev-auto
    + the review hunters); a run must fail loudly at preflight (not stall mid-run)
    when they are absent."""
    from conftest import git

    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "setup")
    # deliberately do NOT install_base_skills
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    assert cli.main(["run", "--project", str(project.project)]) == 1
    err = capsys.readouterr().err
    assert "bmad-dev-auto" in err


def _stub_run_tui(monkeypatch):
    import automator.tui.app as tui_app

    monkeypatch.setattr(tui_app, "run_tui", lambda _project: 0)


def test_tui_low_frame_rate_flag_sets_textual_env(tmp_path, monkeypatch):
    import os

    _stub_run_tui(monkeypatch)
    monkeypatch.delenv("TEXTUAL_FPS", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    assert cli.main(["tui", "--project", str(tmp_path), "--low-frame-rate"]) == 0
    assert os.environ["TEXTUAL_FPS"] == "15"
    assert os.environ["TEXTUAL_ANIMATIONS"] == "none"


def test_tui_low_frame_rate_policy_sets_textual_env(tmp_path, monkeypatch):
    import os

    _write_policy(tmp_path, "[tui]\nlow_frame_rate = true\n")
    _stub_run_tui(monkeypatch)
    monkeypatch.delenv("TEXTUAL_FPS", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    assert cli.main(["tui", "--project", str(tmp_path)]) == 0
    assert os.environ["TEXTUAL_FPS"] == "15"
    assert os.environ["TEXTUAL_ANIMATIONS"] == "none"


def test_tui_low_frame_rate_off_leaves_env_untouched(tmp_path, monkeypatch):
    import os

    _stub_run_tui(monkeypatch)
    monkeypatch.delenv("TEXTUAL_FPS", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    assert cli.main(["tui", "--project", str(tmp_path)]) == 0
    assert "TEXTUAL_FPS" not in os.environ
    assert "TEXTUAL_ANIMATIONS" not in os.environ


def test_tui_low_frame_rate_preserves_explicit_env(tmp_path, monkeypatch):
    import os

    _stub_run_tui(monkeypatch)
    monkeypatch.setenv("TEXTUAL_FPS", "30")  # user's explicit value wins (setdefault)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    assert cli.main(["tui", "--project", str(tmp_path), "--low-frame-rate"]) == 0
    assert os.environ["TEXTUAL_FPS"] == "30"
    assert os.environ["TEXTUAL_ANIMATIONS"] == "none"


def _make_run_with_state(project, run_id, **state_kwargs):
    from automator.journal import save_state
    from automator.model import RunState

    run_dir = project / ".automator" / "runs" / run_id
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project), started_at="now", **state_kwargs),
    )
    return run_dir


def test_stop_no_such_run(tmp_path, capsys):
    assert cli.main(["stop", "--project", str(tmp_path), "missing"]) == 1
    assert "no such run" in capsys.readouterr().err


def test_stop_marks_stopped(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_run_with_state(tmp_path, "r1")  # no pid -> fallback marks stopped
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 0
    assert "r1 stopped" in capsys.readouterr().out
    from automator.journal import load_state

    assert load_state(run_dir).stopped is True


def test_stop_already_finished(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    _make_run_with_state(tmp_path, "r1", finished=True)
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 1
    assert "already finished" in capsys.readouterr().err


def test_delete_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def test_delete_force_stops_then_removes(tmp_path, monkeypatch, capsys):
    from automator import runs

    stopped = []
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    monkeypatch.setattr(runs, "stop_run", lambda rd: stopped.append(rd) or True)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1", "--force"]) == 0
    assert "r1 deleted" in capsys.readouterr().out
    assert stopped == [run_dir]
    assert not run_dir.exists()


def test_delete_force_stop_error_blocks(tmp_path, monkeypatch, capsys):
    # a failed --force stop must propagate, never fall through to deletion
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")

    def _raise(_rd):
        raise runs.StopRunError("boom")

    monkeypatch.setattr(runs, "stop_run", _raise)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1", "--force"]) == 1
    assert "boom" in capsys.readouterr().err
    assert run_dir.exists()


def test_archive_force_stop_error_blocks(tmp_path, monkeypatch, capsys):
    from automator import runs
    from automator.process_host import ProcessHostError

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")

    def _raise(_rd):
        raise ProcessHostError("host probe failed")

    monkeypatch.setattr(runs, "stop_run", _raise)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["archive", "--project", str(tmp_path), "r1", "--force"]) == 1
    assert "host probe failed" in capsys.readouterr().err
    assert run_dir.exists()


def test_delete_dead_run(tmp_path, capsys):
    run_dir = _make_run_with_state(tmp_path, "r1")  # no pid -> not alive
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 0
    assert not run_dir.exists()


def test_delete_unknown_warns_but_proceeds(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 0
    assert "unverifiable pid" in capsys.readouterr().err
    assert not run_dir.exists()


def test_archive_unknown_warns_but_proceeds(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    run_dir = _make_run_with_state(tmp_path, "20260611-100000-aaaa")
    assert cli.main(["archive", "--project", str(tmp_path), "20260611-100000-aaaa"]) == 0
    assert "unverifiable pid" in capsys.readouterr().err
    assert not run_dir.exists()


def test_archive_creates_tarball_and_removes_run(tmp_path, capsys):
    run_dir = _make_run_with_state(tmp_path, "20260611-100000-aaaa")
    assert cli.main(["archive", "--project", str(tmp_path), "20260611-100000-aaaa"]) == 0
    out = capsys.readouterr().out
    dest = tmp_path / ".automator" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert "archived to" in out
    assert dest.is_file()
    assert not run_dir.exists()


def test_archive_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["archive", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def _escalated_run(project, run_id="r1", *, story="s1", spec_file=None):
    from automator.model import Phase, StoryTask

    task = StoryTask(story_key=story, epic=1, phase=Phase.ESCALATED, attempt=1, spec_file=spec_file)
    return _make_run_with_state(
        project,
        run_id,
        paused_reason="CRITICAL escalation",
        paused_stage="escalation",
        paused_story_key=story,
        tasks={story: task},
    )


def test_resolve_no_such_run(tmp_path, capsys):
    assert cli.main(["resolve", "--project", str(tmp_path), "missing"]) == 1
    assert "no such run" in capsys.readouterr().err


def test_resolve_rejects_non_escalation_stage(tmp_path, capsys):
    _make_run_with_state(tmp_path, "r1", paused_stage="spec-approval", paused_reason="x")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    assert "not paused at an escalation" in capsys.readouterr().err


# resolve refuses 'unknown' too, not just 'alive' — re-driving a possibly-live engine.
@pytest.mark.parametrize(
    "liveness,msg",
    [("alive", "is still live — stop it first"), ("unknown", "unverifiable pid")],
)
def test_resolve_refuses_live_run(tmp_path, monkeypatch, capsys, liveness, msg):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: liveness)
    _escalated_run(tmp_path, "r1")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    err = capsys.readouterr().err
    assert msg in err
    if liveness == "unknown":
        assert "--force" in err  # the refusal carries the recovery instructions


def test_resolve_force_alive_still_refuses(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    _escalated_run(tmp_path, "r1")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1", "--force"]) == 1
    assert "is still live — stop it first" in capsys.readouterr().err


def test_resolve_force_unknown_proceeds(tmp_path, monkeypatch, capsys):
    # --force is the only escape from a squatted/unverifiable pid: `stop` cannot
    # clear 'unknown' (engine.pid is never deleted), so without it resolve would
    # refuse forever.
    from automator import runs
    from automator.journal import load_state
    from automator.model import Phase

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    run_dir = _escalated_run(tmp_path, "r1")
    resumed = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: resumed.append(rd) or 0)
    rc = cli.main(
        ["resolve", "--project", str(tmp_path), "r1", "--force", "--no-interactive", "--resume"]
    )
    assert rc == 0
    assert "proceeding anyway (--force)" in capsys.readouterr().err
    assert resumed == [run_dir]
    assert load_state(run_dir).tasks["s1"].phase == Phase.PENDING  # past the gate, re-armed


def test_resolve_no_escalated_story(tmp_path, capsys):
    _make_run_with_state(
        tmp_path, "r1", paused_stage="escalation", paused_reason="x", paused_story_key="ghost"
    )
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    assert "no escalated story" in capsys.readouterr().err


def test_resolve_no_interactive_rearms_and_resumes(tmp_path, monkeypatch, capsys):
    from automator.journal import load_state
    from automator.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: in-review\n---\n", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    resumed = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: resumed.append(rd) or 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--resume"])
    assert rc == 0
    assert resumed == [run_dir]
    # re-armed: task flipped out of ESCALATED, spec status re-armed
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.PENDING
    assert "ready-for-dev" in spec.read_text()


def test_resolve_interactive_runs_session_then_rearms(tmp_path, monkeypatch):
    from automator import resolve
    from automator.journal import load_state
    from automator.model import Phase

    _escalated_run(tmp_path, "r1")
    calls = {}
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: calls.setdefault("ctx", True))
    monkeypatch.setattr(
        resolve, "run_session", lambda *a, **k: calls.setdefault("session", True) or True
    )
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    run_dir = tmp_path / ".automator" / "runs" / "r1"
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 0
    assert calls == {"ctx": True, "session": True}
    assert load_state(run_dir).tasks["s1"].phase == Phase.PENDING


def test_resolve_interactive_unsupported_adapter(tmp_path, monkeypatch, capsys):
    from automator import resolve

    _escalated_run(tmp_path, "r1")
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: None)

    def boom(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(resolve, "run_session", boom)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1"])
    assert rc == 1
    assert "no interactive session mode" in capsys.readouterr().err


def test_resolve_in_ctl_session_detaches_before_resume(tmp_path, monkeypatch, capsys):
    from automator.tui import launch

    _escalated_run(tmp_path, "r1")
    order = []
    monkeypatch.setattr(launch, "in_ctl_session", lambda: True)
    monkeypatch.setattr(launch, "detach_client", lambda: order.append("detach"))
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: order.append("resume") or 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--resume"])
    assert rc == 0
    assert order == ["detach", "resume"]  # hand terminal back, then run the engine
    assert "in the background" in capsys.readouterr().out


def test_resolve_rearm_only_skips_resume(tmp_path, monkeypatch, capsys):
    _escalated_run(tmp_path, "r1")
    monkeypatch.setattr(
        cli, "_resume_paused_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resumed"))
    )
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--no-resume"])
    assert rc == 0
    assert "resume when ready" in capsys.readouterr().out


def test_sweep_command_parses_flags():
    parser_args = [
        "sweep",
        "--project",
        ".",
        "--no-prompt",
        "--decisions-only",
        "--max-bundles",
        "3",
        "--repeat",
        "--max-cycles",
        "4",
        "--dry-run",
    ]
    # exercise argparse wiring only: dry-run path needs a valid project, so
    # just confirm parsing reaches cmd_sweep with the expected namespace
    import argparse as ap

    captured = {}

    def fake_cmd(args: ap.Namespace) -> int:
        captured.update(vars(args))
        return 0

    original = cli.cmd_sweep
    cli.cmd_sweep = fake_cmd
    try:
        # rebuild the parser so it binds the patched function
        assert cli.main(parser_args) == 0
    finally:
        cli.cmd_sweep = original
    assert captured["no_prompt"] is True
    assert captured["decisions_only"] is True
    assert captured["max_bundles"] == 3
    assert captured["repeat"] is True
    assert captured["max_cycles"] == 4
    assert captured["dry_run"] is True


# ------------------------------------------------------------------- cleanup


def test_cleanup_dry_run_lists_without_pruning(tmp_path, monkeypatch, capsys):
    from automator import runs
    from automator.tui import launch

    monkeypatch.setattr(launch, "prunable_ctl_windows", lambda _proj: ["sweep-fin-1"])
    dry_runs: list[bool] = []
    monkeypatch.setattr(
        runs,
        "prune_sessions",
        lambda _proj, dry_run=False: dry_runs.append(dry_run) or (["fin-1"], ["live-1"], set()),
    )

    assert cli.main(["cleanup", "--project", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would kill session bmad-auto-fin-1" in out
    assert "would close ctl window sweep-fin-1" in out
    assert "leaving 1 live session(s) untouched" in out
    assert dry_runs == [True]  # one partition sample, with the kill suppressed


def test_cleanup_prunes_sessions_and_windows(tmp_path, monkeypatch, capsys):
    from automator import runs
    from automator.tui import launch

    monkeypatch.setattr(runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1"], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: ["sweep-fin-1"])

    assert cli.main(["cleanup", "--project", str(tmp_path)]) == 0
    assert "removed 1 session(s), 1 ctl window(s)" in capsys.readouterr().out


def test_cleanup_warns_per_unknown_session(tmp_path, monkeypatch, capsys):
    from automator import runs
    from automator.tui import launch

    monkeypatch.setattr(
        runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1", "odd-1"], [], {"odd-1"})
    )
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: [])

    assert cli.main(["cleanup", "--project", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "run odd-1: engine may still be live (unverifiable pid)" in captured.err
    assert "fin-1: engine may still be live" not in captured.err  # only unknown ids warn
    assert "removed 2 session(s), 0 ctl window(s)" in captured.out


def test_resume_kills_stale_session_before_running(project, monkeypatch):
    from conftest import install_base_skills

    from automator import runs

    install_bmad_config(project)
    install_base_skills(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    run_dir = _make_run_with_state(
        project.project,
        "20990101-000000-beef",
        paused_reason="spec approval",
        paused_stage="spec-approval",
    )
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    assert cli._resume_paused_run(project.project, run_dir) == 0
    assert killed == ["20990101-000000-beef"]


def test_resume_restores_persisted_run_scope(project, monkeypatch):
    """Regression: resume must rebuild the Engine with the run's persisted
    `--epic`/`--story`/`--max-stories`, else a scoped run silently widens and
    can jump out of its epic (the Epic-9 boundary bug)."""
    from conftest import install_base_skills

    from automator import runs

    install_bmad_config(project)
    install_base_skills(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    run_dir = _make_run_with_state(
        project.project,
        "20990101-000000-beef",
        paused_reason="escalation",
        paused_stage="escalation",
        epic_filter=9,
        story_filter="9-0",
        max_stories=4,
    )
    captured: dict = {}

    class _CapturingEngine(_StubEngine):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runs, "kill_session", lambda rid: None)
    monkeypatch.setattr(cli, "Engine", _CapturingEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    assert cli._resume_paused_run(project.project, run_dir) == 0
    assert captured["epic_filter"] == 9
    assert captured["story_filter"] == "9-0"
    assert captured["max_stories"] == 4


def test_resume_refuses_live_run(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")

    def _fail(*_a, **_k):
        raise AssertionError("resumed a live run")

    monkeypatch.setattr(cli, "_resume_paused_run", _fail)
    _make_run_with_state(tmp_path, "r1", paused_reason="x", paused_stage="spec-approval")
    assert cli.main(["resume", "--project", str(tmp_path), "r1"]) == 1
    assert "double-drive" in capsys.readouterr().err


def test_resume_unknown_warns_but_proceeds(project, monkeypatch, capsys):
    # resume must remain the unknown-recovery path: it warns, then rewrites
    # engine.pid via runs.write_pid — blocking here would make a squatted pid
    # permanently unrecoverable (resolve refuses 'unknown' without --force).
    from conftest import install_base_skills

    from automator import runs

    install_bmad_config(project)
    install_base_skills(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    run_dir = _make_run_with_state(
        project.project,
        "20990101-000000-beef",
        paused_reason="escalation",
        paused_stage="escalation",
    )
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    rc = cli.main(["resume", "--project", str(project.project), "20990101-000000-beef"])
    assert rc == 0
    assert "may still be live (unverifiable pid)" in capsys.readouterr().err
    assert (run_dir / "engine.pid").is_file()  # pid rewritten — recovery happened


def test_diagnose_default_latest_and_out(project, tmp_path, capsys):
    """diagnose resolves the latest run, writes a clean dump, exits 0."""
    from test_diagnostics import CANARIES, _seed_run

    _seed_run(project.project)
    out_file = tmp_path / "diag.md"
    rc = cli.main(["diagnose", "--project", str(project.project), "--json", "--out", str(out_file)])
    assert rc == 0
    report = out_file.read_text()
    assert "diagnostic dump (sanitized)" in report
    for canary in CANARIES:
        assert canary not in report, f"LEAK via CLI: {canary!r}"


def test_diagnose_no_runs(tmp_path, capsys):
    assert cli.main(["diagnose", "--project", str(tmp_path)]) == 1
    assert "no runs found" in capsys.readouterr().err


def test_diagnose_legend_written_locally(project, tmp_path):
    from test_diagnostics import STORY_KEY, _seed_run

    _seed_run(project.project)
    legend_file = tmp_path / "legend.json"
    out_file = tmp_path / "diag.md"
    rc = cli.main(
        [
            "diagnose",
            "--project",
            str(project.project),
            "--out",
            str(out_file),
            "--legend",
            str(legend_file),
        ]
    )
    assert rc == 0
    legend = json.loads(legend_file.read_text())
    assert STORY_KEY in legend.values()  # legend reverses pseudonyms locally
    assert STORY_KEY not in out_file.read_text()  # but the dump never carries it


# ---- validate platform preflight (routes through the multiplexer + host seams) ----


class _FakeBackend:
    def __init__(self, ok, version=None):
        self._ok, self._version = ok, version

    def available(self):
        return self._ok

    def version(self):
        return self._version


class _FakeHost:
    pass


def _patch_preflight(monkeypatch, backend):
    from automator import process_host as ph_mod
    from automator.adapters import multiplexer as mux_mod

    monkeypatch.setattr(mux_mod, "get_multiplexer", lambda: backend)
    monkeypatch.setattr(ph_mod, "get_process_host", lambda: _FakeHost())


def test_platform_preflight_reports_available_backend(monkeypatch):
    # An available backend reports through available()/version() — no sys.platform
    # branch — and the selected process host is named for visibility.
    _patch_preflight(monkeypatch, _FakeBackend(ok=True, version="tmux 3.4"))
    notes, problems = cli._platform_preflight()
    assert not problems
    assert any("_FakeBackend" in n and "tmux 3.4" in n for n in notes)
    assert any("process host" in n and "_FakeHost" in n for n in notes)


def test_platform_preflight_flags_unavailable_backend(monkeypatch):
    # A backend whose transport binary is absent surfaces here as a problem, so a
    # new OS registers a backend rather than inlining a win32 block in validate.
    _patch_preflight(monkeypatch, _FakeBackend(ok=False))
    notes, problems = cli._platform_preflight()
    assert any("unavailable" in p for p in problems)


def test_platform_preflight_reports_multiplexer_selection_error(monkeypatch):
    # A bad BMAD_AUTO_MUX_BACKEND makes get_multiplexer() raise; preflight must report
    # it as a problem (so `validate` exits cleanly) rather than let it abort the command.
    from automator.adapters import multiplexer as mux_mod
    from automator.adapters.multiplexer import MultiplexerError

    def _boom():
        raise MultiplexerError("BMAD_AUTO_MUX_BACKEND='bogus' matches no registered backend")

    monkeypatch.setattr(mux_mod, "get_multiplexer", _boom)
    notes, problems = cli._platform_preflight()  # must not raise
    assert any("bogus" in p for p in problems)


def test_platform_preflight_reports_process_host_selection_error(monkeypatch):
    # A bad BMAD_AUTO_PROCESS_HOST makes get_process_host() raise; preflight must report
    # it as a problem, and an otherwise-healthy multiplexer still gets its note.
    from automator import process_host as ph_mod
    from automator.process_host import ProcessHostError

    _patch_preflight(monkeypatch, _FakeBackend(ok=True, version="tmux 3.4"))

    def _boom():
        raise ProcessHostError("BMAD_AUTO_PROCESS_HOST='bogus' matches no registered host")

    monkeypatch.setattr(ph_mod, "get_process_host", _boom)
    notes, problems = cli._platform_preflight()  # must not raise
    assert any("bogus" in p for p in problems)
    assert any("_FakeBackend" in n for n in notes)  # the healthy seam still reported
